"""Live (broker-backed) read-only routes for the Mission Control API.

`/health`, `/account`, `/positions`, `/orders` — everything here reads
either the Alpaca broker (via `src.api.broker_reads`, which never raises
and never touches a write-capable broker call) or, for `/health`'s
`db_reachable`/`sessions_logged_today` fields and `/account`'s `history`
field, the SQLite-backed helpers the other Stage 2 worker exposes from
`src.api.db_reads`.

GET-only by construction: this router registers nothing but `@router.get`
handlers. That is a hard project invariant for Stage 2 (verified by an
automated test) — do not add POST/PUT/PATCH/DELETE routes here.

Every handler is defense-in-depth: the functions it calls
(`broker_reads.*`) are already designed to never raise, but each handler
still wraps its body in `try/except Exception` and degrades to a response
model with `error` set rather than ever letting FastAPI surface an
unhandled 500 (which could leak an internal stack trace / file path).
"""

from __future__ import annotations

import logging
import math
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, HTTPException, Query

from src.api.loud_reads import record_dashboard_fault
from src.api.drift_state import deploy_drift_state as _deploy_drift_state
from src.api.margin_interest_view import _compute_margin_interest
from src.api.broker_reads import (
    check_broker_reachable,
    read_account,
    read_live_quotes,
    read_orders,
    read_positions,
    read_price_bars,
)

# `src.quantities` holds the single definition of every number this
# dashboard shares with the trading engine. It is dependency-free and lives
# OUTSIDE `src.risk`, so importing it here does not breach the structural
# guardrail in tests/test_api_safety.py — that is precisely the seam the
# §11.2 note further down asked for.
from src.quantities import (
    cash_above_reserve,
    deployable_cash,
    net_exposure_pct,
    net_exposure_usd,
    sweep_reserve_usd,
)
from src.api.deps import (
    get_alpaca_paper,
    get_cash_sweep_enabled,
    get_cash_reserve_pct,
    get_cash_sweep_symbol,
    get_risk_limits,
)
from src.api.schemas import (
    AccountResponse,
    CompanyIdentityResponse,
    DailyPnlPoint,
    ExposureBreakdown,
    HealthResponse,
    MutedBacklogResponse,
    SuppressedAlertsResponse,
    LiquidityBreakdown,
    LiveQuote,
    LiveQuotesResponse,
    OrderItem,
    OrdersResponse,
    PositionItem,
    PositionsResponse,
    PriceBar,
    PriceBarsResponse,
    RiskLimits,
    DividendEvent,
    EarningsEvent,
    SymbolEventsResponse,
)

# Market-data read only (yfinance) — same dependency `/prices`' underlying
# stop-adjustment/risk-prompt code already uses for the single-next-value
# `get_upcoming_ex_dividend`/`get_next_earnings_date` calls; this route uses
# the separate, chart-scoped `get_price_chart_events` (see its docstring).
# `src.data.market` imports only `src.models`/`src.util.time` — it is not
# `src.pipeline`/`src.pipeline_stages`/`src.risk.*`, so this import does not
# breach the Stage 2 isolation invariant `tests/test_api_isolation.py` /
# `tests/test_api_safety.py` enforce for this router.
from src.data.market import MarketDataProvider
from src.data.company import CompanyProfileStore

_MAX_QUOTE_SYMBOLS = 25

logger = logging.getLogger(__name__)

router = APIRouter()

# One shared instance for the /events route below. Stateless for this call
# (get_price_chart_events opens its own yf.Ticker per request; the instance
# just carries the unused `fallback_bars` hook get_ohlcv would need, which
# this route never calls) — no different from constructing it per-request,
# just avoids doing so on every call.
_market_data = MarketDataProvider()

_LAST_RUN_MODES = [
    "morning",
    "midday",
    "close",
    "evening",
    "intra_check",
    "earnings_preprocess",
]

_ORDER_STATUS_VALUES = {"open", "closed", "all"}


def _cache_dir() -> Path:
    # Same directory scripts/run_if_et_window.sh writes last-run files and
    # the cross-mode session lock into — see CLAUDE.md "时区" section.
    return Path(os.path.expanduser("~/.cache/quant-agent"))


def _last_run_files() -> dict[str, str | None]:
    out: dict[str, str | None] = {mode: None for mode in _LAST_RUN_MODES}
    try:
        base = _cache_dir()
        for mode in _LAST_RUN_MODES:
            try:
                p = base / f"last-{mode}"
                if p.exists():
                    mtime = p.stat().st_mtime
                    out[mode] = datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat()
            except OSError:
                out[mode] = None
    except Exception:
        record_dashboard_fault("last_run_files")
        # Cache dir missing entirely, or some other unexpected OS-level
        # failure — never raise, just report "nothing known" for every mode.
        return {mode: None for mode in _LAST_RUN_MODES}
    return out


def _session_lock_active() -> bool | None:
    try:
        return os.path.isdir(_cache_dir() / "active-session.lock")
    except OSError:
        return None


def _llm_balance_state() -> dict:
    """Paid-model credit runway (src/llm_balance_runway.py). Never raises."""
    try:
        from src.api.deps import get_config, get_db_path
        from src.llm_balance_runway import read_state

        cc = get_config().llm_cost_circuit
        return read_state(
            get_db_path(),
            topup_usd=cc.openrouter_topup_usd,
            topup_date=cc.openrouter_topup_date,
        )
    except Exception:  # noqa: BLE001
        record_dashboard_fault("llm_balance_state")
        return {"status": "unknown", "message": "The credit check could not run."}


@router.get("/alerts/suppressed", response_model=SuppressedAlertsResponse)
def get_suppressed_alerts(limit: int = 50) -> SuppressedAlertsResponse:
    """Item 211 — everything the desk decided not to say twice.

    Alerts can be muted; the board cannot. A suppressed alert that is
    readable nowhere is a lost alert, so this endpoint exists to make the
    suppression record visible without a Telegram channel.
    """

    from src.api.db_reads import get_suppressed_alerts as _read

    try:
        payload = _read(limit=limit)
    except Exception:
        record_dashboard_fault("get_suppressed_alerts")
        return SuppressedAlertsResponse()
    return SuppressedAlertsResponse(**payload)


@router.get("/alerts/muted-backlog", response_model=MutedBacklogResponse)
def get_muted_backlog() -> MutedBacklogResponse:
    """Item 211 — what the global mute has swallowed, for the dashboard.

    A muted desk cannot page the owner about its own muting, so the backlog
    lives here, where he reads it at his leisure. This endpoint reads the
    record and nothing else: it does not un-mute, does not send, and does
    not touch `TELEGRAM_DISABLED` or any other configuration.
    """

    from src.api.db_reads import get_muted_backlog as _read

    try:
        payload = _read()
    except Exception:
        record_dashboard_fault("get_muted_backlog")
        return MutedBacklogResponse()
    return MutedBacklogResponse(**payload)


@router.get("/health", response_model=HealthResponse)
def get_health() -> HealthResponse:
    try:
        try:
            from src.api.db_reads import get_llm_circuit_health, session_prefixes_logged_on

            sessions_logged_today = session_prefixes_logged_on()
            llm_health = get_llm_circuit_health()
            db_reachable = True
        except Exception:
            record_dashboard_fault("get_health.1")
            sessions_logged_today = []
            llm_health = None
            db_reachable = False

        # Read separately from the block above: the alert channel's history
        # living in a database that predates this feature is a perfectly
        # ordinary state, and must not be reported as "database unreachable".
        try:
            from src.api.db_reads import get_alert_channel_health

            alert_channel = get_alert_channel_health()
        except Exception:
            record_dashboard_fault("get_health.2")
            alert_channel = {"status": "unknown", "error": "health read failed"}

        # Deploy drift: read the on-box snapshot, never computed here (no
        # git call on a request path). A snapshot that is missing or stale
        # is reported as such rather than as healthy.
        deploy_drift = _deploy_drift_state()
        llm_balance = _llm_balance_state()

        broker_reachable = check_broker_reachable()
        recent_pm_status = (llm_health or {}).get("recent_pm_status")
        circuit_suspended = bool((llm_health or {}).get("suspended"))
        circuit_available = bool((llm_health or {}).get("available"))
        scoped_quota_holds = bool((llm_health or {}).get("active_quota_holds"))

        def _failed_status(value) -> bool:
            if not value:
                return False
            status = str(value).lower()
            return (
                status.startswith("pm_")
                or status in {"agent_failure", "failed"}
                or "parse_error" in status
                or "analysis_error" in status
            )

        recent_agent_statuses = (llm_health or {}).get("recent_agent_statuses") or {}
        failed_agents = sorted(
            name for name, item in recent_agent_statuses.items() if _failed_status((item or {}).get("status"))
        )
        # Compatibility with a database populated before the per-agent health
        # map existed in this API process.
        if _failed_status(recent_pm_status) and "portfolio_manager" not in failed_agents:
            failed_agents.append("portfolio_manager")
        if not db_reachable:
            decision_path_status = "unknown_database_unreachable"
        elif not circuit_available:
            decision_path_status = "degraded_cost_circuit_unavailable"
        elif circuit_suspended:
            decision_path_status = "paid_analysis_suspended"
        elif scoped_quota_holds:
            decision_path_status = "paid_analysis_scoped_quota_hold"
        elif failed_agents:
            decision_path_status = "degraded_recent_agent_failure:" + ",".join(failed_agents)
        else:
            decision_path_status = "ok"
        # A desk that cannot raise an alarm is degraded whatever else is
        # green: every other fault on this board is reported to the operator
        # over the same Telegram path, so this one hides all the others.
        # `unknown` (no check recorded yet — a fresh database, a deploy that
        # has not run a session) is surfaced in the payload and rendered
        # amber, but does not flip the whole board red: a missing
        # measurement is not a detected fault, and a permanently-red board
        # teaches the operator to ignore red.
        alert_channel_degraded = str((alert_channel or {}).get("status") or "unknown") in ("broken", "stale", "muted")
        # A box running code that was never merged-then-deployed is degraded:
        # every fix believed to be live is not. `unknown` (never checked) is
        # shown in the payload but does not flip the board, for the same
        # reason the alert channel's `unknown` does not — a missing
        # measurement is not a detected fault.
        deploy_drift_degraded = str((deploy_drift or {}).get("status") or "unknown") in ("behind", "stale")
        degraded_causes: list[str] = []
        if not db_reachable:
            degraded_causes.append("database unreachable")
        if broker_reachable is False:
            degraded_causes.append("broker unreachable")
        if decision_path_status != "ok":
            degraded_causes.append(f"decision path: {decision_path_status}")
        if alert_channel_degraded:
            degraded_causes.append("alert channel " + str((alert_channel or {}).get("status") or "unknown"))
        if deploy_drift_degraded:
            degraded_causes.append("deploy drift " + str((deploy_drift or {}).get("status") or "unknown"))
        if (llm_balance or {}).get("status") == "low":
            degraded_causes.append(llm_balance["message"])
        overall_status = (
            "degraded"
            if (
                (llm_balance or {}).get("status") == "low"
                or not db_reachable
                or broker_reachable is False
                or decision_path_status != "ok"
                or alert_channel_degraded
                or deploy_drift_degraded
            )
            else "ok"
        )

        return HealthResponse(
            status=overall_status,
            reason="; ".join(degraded_causes) or None,
            db_reachable=db_reachable,
            broker_reachable=broker_reachable,
            paper=get_alpaca_paper(),
            sessions_logged_today=list(sessions_logged_today or []),
            last_run_files=_last_run_files(),
            session_lock_active=_session_lock_active(),
            decision_path_status=decision_path_status,
            llm_circuit=llm_health,
            alert_channel=alert_channel,
            deploy_drift=deploy_drift,
            llm_balance=llm_balance,
            timestamp=datetime.now(timezone.utc).isoformat(),
        )
    except Exception:
        record_dashboard_fault("get_health.3")
        # Outermost guard: even /health itself must never 500. Report the
        # process as up but degraded; everything else is explicitly unknown.
        # unknown, rather than leaking a stack trace.
        return HealthResponse(
            status="degraded",
            reason="the health read itself failed",
            db_reachable=False,
            broker_reachable=None,
            paper=None,
            sessions_logged_today=[],
            last_run_files={mode: None for mode in _LAST_RUN_MODES},
            session_lock_active=None,
            decision_path_status="unknown_health_exception",
            llm_circuit=None,
            alert_channel={"status": "unknown", "error": "health read failed"},
            deploy_drift={"status": "unknown", "error": "health read failed"},
            timestamp=datetime.now(timezone.utc).isoformat(),
        )


def _compute_liquidity(
    cash: float | None,
    portfolio_value: float | None,
    positions_result: dict | None = None,
) -> LiquidityBreakdown:
    """Honest raw-cash / sweep-parked / deployable split (2026-08-18 soak
    finding: SGOV must never read like an ordinary position or an invented
    risk posture). Reads positions independently (or reuses the caller's
    already-fetched payload) so a positions-read failure degrades only the
    sweep_parked_value/deployable_cash fields, never silently zeroes them.
    A config-read failure degrades this whole breakdown to an honest empty
    object — it must never take down the rest of the /account response
    (see AccountResponse.cash etc).

    `deployable_cash` is the ENGINE'S figure, computed by the same
    `src.quantities.deployable_cash` the pre-trade cash gate uses. The
    reserve-adjusted figure this field used to hold is still reported, as
    `cash_above_reserve`."""
    try:
        sweep_enabled = get_cash_sweep_enabled()
        sweep_symbol = get_cash_sweep_symbol()
        reserve_pct = get_cash_reserve_pct()
    except Exception as exc:
        record_dashboard_fault("compute_liquidity", exc)
        return LiquidityBreakdown()

    sweep_parked_value: float | None = None
    if positions_result is None:
        positions_result = read_positions()
    if positions_result.get("error") is None:
        sweep_parked_value = sum(
            p.get("market_value") or 0.0 for p in positions_result.get("positions", []) if p.get("is_cash_equivalent")
        )

    reserve_usd = sweep_reserve_usd(portfolio_value, reserve_pct) if portfolio_value is not None else None
    # One definition, shared with the engine: cash + the sweep vehicle the
    # BUY phase liquidates on demand. Unknown until BOTH halves are known —
    # a positions-read failure must not print raw cash as if it were the
    # whole deployable figure.
    #
    # The parked vehicle only counts when the sweep is ENABLED. With
    # `cash_sweep.enabled: false` the engine's `_compute_deployable_cash`
    # takes the `_sweeper() is None` branch and adds 0.0, because
    # `CashSweeper.fund_buys` returns 0.0 on the first line and nothing
    # else converts the vehicle to cash for the BUY phase. This view used
    # to add it unconditionally, so a vehicle held under a retired sweep
    # would have made the operator's "Deployable" tile read above the
    # figure the PM actually sizes against — exactly the drift the test
    # beside it guards. Latent, not an incident: no cash-equivalent
    # position is held (production DB, read-only, 2026-10-01).
    deployable = (
        deployable_cash(cash, sweep_parked_value if sweep_enabled else 0.0)
        if cash is not None and sweep_parked_value is not None
        else None
    )
    above_reserve = cash_above_reserve(cash, reserve_usd) if cash is not None and reserve_usd is not None else None

    return LiquidityBreakdown(
        sweep_enabled=sweep_enabled,
        sweep_symbol=sweep_symbol,
        raw_cash=cash,
        sweep_parked_value=sweep_parked_value,
        reserve_usd=reserve_usd,
        deployable_cash=deployable,
        cash_above_reserve=above_reserve,
        # Deprecated alias — assigned, not recomputed.
        total_liquidity=deployable,
    )


def _compute_exposure(
    portfolio_value: float | None,
    positions_result: dict | None = None,
) -> ExposureBreakdown:
    """Net exposure by the ENGINE'S definition, so the cockpit's "%
    deployed" gauge and the ceiling it is drawn against finally measure the
    same thing.

    `src.quantities.net_exposure_usd/_pct` is what `src.risk.rules`'
    `max_total_position_pct` rule calls; the cockpit used to re-derive the
    number from position `direction` labels instead (hedges ADDED, leverage
    ignored). Same fail-soft posture as `_compute_liquidity`: any unusable
    input degrades to an all-None object, never a fabricated 0."""
    try:
        sweep_symbol = get_cash_sweep_symbol()
    except Exception as exc:
        record_dashboard_fault("compute_exposure", exc)
        sweep_symbol = None

    if positions_result is None:
        positions_result = read_positions()
    if positions_result.get("error") is not None:
        return ExposureBreakdown()

    # The engine splits the sweep vehicle out of `positions` before the risk
    # gate sees them; this payload is unsplit, so exclude it by symbol here.
    net_usd = net_exposure_usd(
        positions_result.get("positions", []),
        cash_park_symbol=sweep_symbol,
    )
    return ExposureBreakdown(
        net_exposure_usd=net_usd,
        net_exposure_pct=net_exposure_pct(net_usd, portfolio_value),
    )


def _compute_risk_limits() -> RiskLimits:
    """Degrades to an honest empty RiskLimits() on any config read
    failure — never a guessed/default limit standing in for the real
    configured one. Mirrors _compute_liquidity's fail-closed-to-empty
    posture."""
    try:
        limits = get_risk_limits()
    except Exception as exc:
        record_dashboard_fault("compute_risk_limits", exc)
        return RiskLimits()
    return RiskLimits(
        max_position_pct=limits.max_position_pct,
        max_total_position_pct=limits.max_total_position_pct,
        # Spec §11.2 — the standing gross-exposure cap. Distinct from
        # max_total_position_pct, which bounds NET exposure.
        max_gross_exposure_x=getattr(limits, "max_gross_exposure_x", None),
    )


# NOTE (§11.2): Mission Control still does NOT compute the de-levering
# ladder or distance-to-forced-liquidation — those need `src.risk.rules`,
# and `src/api/` is forbidden by a ratified structural guardrail
# (tests/test_api_safety.py) from importing the trading/risk stack at all.
# Re-implementing that arithmetic here was, and remains, rejected: a second
# definition of "how much does the book own" is the exact sprawl §12.2
# cleaned up, and a dashboard that drifts from the gate is worse than one
# that stays quiet.
#
# The condition this note named as the way out has now been met. The pure
# measurement functions live in `src.quantities`, a dependency-free module
# outside `src.risk` that `src.risk.rules` itself imports, so
# `_compute_exposure` above reports NET exposure from the engine's own
# definition rather than a lookalike. The standing cap is still reported on
# `RiskLimits.max_gross_exposure_x` (config read, no risk import) and the
# live ladder state still reaches the operator on the session alert
# (`src/notifier.py::_append_leverage_line`); the ladder is session state,
# not arithmetic, so it stays off this endpoint.


@router.get("/account", response_model=AccountResponse)
def get_account() -> AccountResponse:
    try:
        acct = read_account()
        cash = acct.get("cash")
        portfolio_value = acct.get("portfolio_value")
        last_equity = acct.get("last_equity")

        daily_pnl = None
        daily_pnl_pct = None
        if portfolio_value is not None and last_equity is not None and last_equity != 0:
            daily_pnl = portfolio_value - last_equity
            daily_pnl_pct = daily_pnl / last_equity * 100

        history: list[DailyPnlPoint] = []
        try:
            from src.api.db_reads import get_recent_daily_pnl

            rows = get_recent_daily_pnl(limit=30) or []
            for row in rows:
                history.append(
                    DailyPnlPoint(
                        date=row.get("date"),
                        total_value=row.get("total_value"),
                        daily_pnl=row.get("daily_pnl"),
                        daily_return_pct=row.get("daily_return_pct"),
                        equity_close=row.get("equity_close"),
                    )
                )
        except Exception:
            record_dashboard_fault("get_account.1")
            history = []

        # Total P&L since the board's own tracked start — deliberately NOT
        # derived from `history` above, which is capped at 30 rows and may
        # be truncated. Reads the table's own earliest row directly, same
        # baseline `TradingPipeline._total_pnl_since_reset` already uses for
        # the Telegram feed's "Total P&L since <date>" line (see
        # `get_earliest_daily_pnl`'s docstring). Degrades to `None` on any
        # read failure or missing/non-positive baseline — never a
        # fabricated total.
        total_pnl = None
        total_pnl_pct = None
        total_pnl_since = None
        try:
            from src.api.db_reads import get_earliest_daily_pnl

            earliest = get_earliest_daily_pnl()
            if earliest and portfolio_value is not None:
                baseline = float(earliest["total_value"]) - float(earliest["daily_pnl"])
                tv = float(portfolio_value)
                if baseline > 0 and math.isfinite(baseline) and math.isfinite(tv):
                    total_pnl = tv - baseline
                    total_pnl_pct = total_pnl / baseline * 100
                    total_pnl_since = str(earliest.get("date") or "") or None
        except Exception as exc:
            record_dashboard_fault("get_account.2", exc)
            total_pnl = None
            total_pnl_pct = None
            total_pnl_since = None

        # One positions read serves both the liquidity split and the
        # exposure gauge — they used to be independent round-trips, and an
        # /account response built from two different broker snapshots is
        # its own way of making two numbers disagree.
        positions_result = read_positions() if acct.get("error") is None else None
        liquidity = _compute_liquidity(cash, portfolio_value, positions_result) if acct.get("error") is None else None
        exposure = _compute_exposure(portfolio_value, positions_result) if acct.get("error") is None else None
        risk_limits = _compute_risk_limits()
        margin_interest = _compute_margin_interest(cash) if acct.get("error") is None else None

        return AccountResponse(
            cash=cash,
            portfolio_value=portfolio_value,
            last_equity=last_equity,
            daily_pnl=daily_pnl,
            daily_pnl_pct=daily_pnl_pct,
            total_pnl=total_pnl,
            total_pnl_pct=total_pnl_pct,
            total_pnl_since=total_pnl_since,
            paper=get_alpaca_paper(),
            history=history,
            liquidity=liquidity,
            exposure=exposure,
            risk_limits=risk_limits,
            margin_interest=margin_interest,
            error=acct.get("error"),
        )
    except Exception as exc:
        record_dashboard_fault("get_account.3", exc)
        return AccountResponse(error=str(exc))


@router.get("/positions", response_model=PositionsResponse)
def get_positions() -> PositionsResponse:
    try:
        result = read_positions()
        items = [
            PositionItem(
                symbol=p.get("symbol"),
                qty=p.get("qty"),
                avg_entry=p.get("avg_entry"),
                current_price=p.get("current_price"),
                position_mark=p.get("position_mark"),
                market_value=p.get("market_value"),
                unrealized_pnl=p.get("unrealized_pnl"),
                unrealized_intraday_pnl=p.get("unrealized_intraday_pnl"),
                sector=p.get("sector"),
                is_cash_equivalent=p.get("is_cash_equivalent", False),
                direction=p.get("direction", "long"),
            )
            for p in result.get("positions", [])
        ]
        return PositionsResponse(positions=items, error=result.get("error"))
    except Exception as exc:
        record_dashboard_fault("get_positions", exc)
        return PositionsResponse(positions=[], error=str(exc))


@router.get("/orders", response_model=OrdersResponse)
def get_orders(
    status: Literal["open", "closed", "all"] = Query("open"),
    limit: int = Query(50, ge=1, le=500),
) -> OrdersResponse:
    try:
        if status not in _ORDER_STATUS_VALUES:
            # Defense in depth: FastAPI's Literal type already rejects
            # anything else at the request-validation layer, but never let
            # an unvalidated status string reach the broker call.
            raise HTTPException(status_code=400, detail=f"invalid status: {status!r}")

        result = read_orders(status=status, limit=limit)
        items = [
            OrderItem(
                id=o.get("id"),
                symbol=o.get("symbol"),
                side=o.get("side"),
                qty=o.get("qty"),
                order_type=o.get("order_type"),
                status=o.get("status"),
                limit_price=o.get("limit_price"),
                stop_price=o.get("stop_price"),
                filled_qty=o.get("filled_qty"),
                filled_avg_price=o.get("filled_avg_price"),
                submitted_at=o.get("submitted_at"),
                filled_at=o.get("filled_at"),
            )
            for o in result.get("orders", [])
            if o.get("id") is not None and o.get("symbol") is not None
        ]
        return OrdersResponse(orders=items, error=result.get("error"))
    except HTTPException:
        raise
    except Exception as exc:
        record_dashboard_fault("get_orders", exc)
        return OrdersResponse(orders=[], error=str(exc))


@router.get("/quotes", response_model=LiveQuotesResponse)
def get_quotes(symbols: str = Query(..., description="Comma-separated symbols, e.g. AAPL,MSFT")) -> LiveQuotesResponse:
    """Current-session quote facts (last trade, previous close, today's
    still-forming session range) for one or more symbols — market-data
    read only, never account/order/trading state. Distinct from
    `/positions`' broker-marked current_price (held positions only) and
    `/prices`' historical daily bars; lets a chart/candidate view label a
    true current price instead of implying a historical bar is "now."
    """
    now = datetime.now(timezone.utc).isoformat()
    try:
        syms = [s.strip().upper() for s in symbols.split(",") if s.strip()]
        syms = syms[:_MAX_QUOTE_SYMBOLS]
        if not syms:
            return LiveQuotesResponse(quotes=[], as_of=now, error="no symbols requested")
        result = read_live_quotes(syms)
        result_quotes = result.get("quotes", {})
        quotes = [LiveQuote(symbol=sym, **(result_quotes.get(sym) or {})) for sym in syms]
        return LiveQuotesResponse(quotes=quotes, as_of=now, error=result.get("error"))
    except Exception as exc:
        record_dashboard_fault("get_quotes", exc)
        return LiveQuotesResponse(quotes=[], as_of=now, error=str(exc))


@router.get("/prices/{symbol}", response_model=PriceBarsResponse)
def get_prices(
    symbol: str,
    # No upper bound reflecting a real limit: Alpaca provides decades of
    # daily history for this account/plan. `le=36500` (100y) is only an
    # input-sanity guard against a pathological request, not a claimed
    # data-availability ceiling.
    lookback_days: int = Query(120, ge=1, le=36500),
    timeframe: Literal["5m", "15m", "1h", "1d"] = Query("1d"),
) -> PriceBarsResponse:
    """OHLCV bars for one symbol/timeframe — market-data read only (Alpaca's
    historical data client), never account/order/trading state. Powers
    the cockpit's price chart panel; never places, cancels or references
    an order."""
    try:
        symbol = symbol.strip().upper()
        result = read_price_bars(symbol, lookback_days=lookback_days, timeframe=timeframe)
        bars = [PriceBar(**b) for b in result.get("bars", [])]
        return PriceBarsResponse(
            symbol=symbol,
            timeframe=timeframe,
            bars=bars,
            error=result.get("error"),
        )
    except Exception as exc:
        record_dashboard_fault("get_prices", exc)
        return PriceBarsResponse(symbol=symbol, timeframe=timeframe, bars=[], error=str(exc))


@router.get("/events/{symbol}", response_model=SymbolEventsResponse)
def get_symbol_events(
    symbol: str,
    lookback_days: int = Query(400, ge=1, le=3650),
) -> SymbolEventsResponse:
    """Dividend ex-dates and earnings-report dates (past + upcoming) for one
    symbol — market-data read only (yfinance), never account/order/trading
    state. Powers the price chart's dividend/earnings markers; distinct from
    `/prices`' OHLCV bars. `lookback_days` bounds how far back past events
    are returned; yfinance/earnings_dates typically also surfaces a handful
    of upcoming estimated dates regardless of this bound."""
    try:
        symbol = symbol.strip().upper()
        result = _market_data.get_price_chart_events(symbol, lookback_days=lookback_days)
        dividends = [DividendEvent(**d) for d in result.get("dividends", [])]
        earnings = [EarningsEvent(**e) for e in result.get("earnings", [])]
        return SymbolEventsResponse(
            symbol=symbol,
            dividends=dividends,
            earnings=earnings,
            earnings_degraded=result.get("earnings_degraded"),
        )
    except Exception as exc:
        record_dashboard_fault("get_symbol_events", exc)
        return SymbolEventsResponse(symbol=symbol, error=str(exc))


def _peek_company(symbol: str):
    """Cache-only identity. Isolated so tests can stub the disk read
    without constructing a store. Never fetches."""
    return CompanyProfileStore().peek_cached(symbol)


@router.get("/company/{symbol}", response_model=CompanyIdentityResponse)
def get_company_identity(symbol: str) -> CompanyIdentityResponse:
    """Cached company name for the cockpit chart header.

    Reads `data/company_profiles.json` only. A missing name is `null`,
    never a guessed title. This path does not call yfinance.
    """
    try:
        symbol = symbol.strip().upper()
        if not symbol:
            return CompanyIdentityResponse(symbol="", name=None, error="empty symbol")
        profile = _peek_company(symbol)
        return CompanyIdentityResponse(symbol=profile.symbol, name=profile.name)
    except Exception as exc:
        record_dashboard_fault("get_company_identity", exc)
        return CompanyIdentityResponse(symbol=symbol.strip().upper(), name=None, error=str(exc))
