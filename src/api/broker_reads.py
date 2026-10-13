"""Read-only Alpaca broker access for the Mission Control API.

CRITICAL SAFETY INVARIANT: this module must NEVER call, import, or reference
any broker method capable of creating, modifying, or cancelling anything —
no `submit_order`, `cancel_order`, `cancel_order_by_id`, `cancel_open_orders`,
`cancel_protective_stops`, `cancel_snapshotted_stops`, `close_position`,
`place_entry_protection`, or similar. This is a paper-trading production
system's live read surface; the only broker calls made here are
`get_account()`, `get_positions()`, `is_trading_day()` (all pre-existing
read-only methods on `AlpacaBroker`) and a fresh, locally-implemented
`client.get_orders(...)` read (mirroring the pattern at
`src/execution/broker.py:905-961`, `list_recent_orders`, but scoped to "all
open/closed/recent orders" rather than one symbol/side).

Every public function here is designed to NEVER raise: each wraps its own
broker call in try/except and returns a plain dict (or bool/None) carrying
an `error` field on failure, so route handlers in `routes_live.py` can
always build a valid response model instead of crashing into a 500 that
might leak internal details.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from functools import lru_cache

from src.api.deps import (
    INVERSE_ETF_SYMBOLS,
    get_alpaca_credentials,
    get_alpaca_paper,
    get_cash_sweep_symbol,
    get_risk_limits,
)
from src.api.broker_reads_record import record_read_fault
from src.data.live_price import resolve_live_price
from src.execution.broker import AlpacaBroker, _internal_symbol
from src.trading_calendar import live_price_is_today, utc_now

logger = logging.getLogger(__name__)


def _position_direction(symbol: str, sweep_symbol: str, qty: float | None) -> str:
    """Display-labeling ONLY — see `PositionItem.direction` in
    `src/api/schemas.py`. Computes no exposure/risk math; a wrong label here
    used to make every short position read as "long" on the wire (docs/WORK.md
    item 176), because this never looked at the sign of `qty`.

    Precedence, in order:

    1. `cash_equivalent` — identity-based (the configured sweep vehicle),
       independent of quantity. Checked first because it names what the
       position IS (parked cash), not which way it is pointed.
    2. `short` — `qty < 0`, whatever the symbol. This takes priority over
       the inverse-ETF check below: `bearish_hedge` specifically means "a
       LONG position in an inverse ETF, held as the desk's mechanism for
       bearish exposure without shorting" (see `INVERSE_ETF_SYMBOLS` and
       `src.quantities.inverse_etf_symbols()`). An actual SHORT position in
       an inverse ETF is not that — it is an unusual, doubly-inverted bet
       (economically closer to long the underlying) and must not be hidden
       under the `bearish_hedge` label, which every consumer reads as "long
       an inverse ETF."
    3. `bearish_hedge` — `qty > 0` (or unknown-sign, see below) in a symbol
       already in the trading universe's inverse-ETF set.
    4. `long` — the default for everything else.

    `qty` of exactly `0` or `None` carries no directional sign, so it falls
    through step 2 unchanged (never raises on `None < 0`) and is labeled by
    steps 3/4 exactly as before this fix — a flat/unknown-quantity position
    was never a case this label tried to distinguish, and a zero-quantity
    position never carries risk either way.
    """
    if symbol == sweep_symbol:
        return "cash_equivalent"
    if qty is not None and qty < 0:
        return "short"
    if symbol in INVERSE_ETF_SYMBOLS:
        return "bearish_hedge"
    return "long"


def _utc_now() -> datetime:
    """Seam for tests — real code never patches `datetime` itself."""
    return utc_now()


def _iso_or_none(value) -> str | None:
    """Best-effort ISO-8601 rendering of a provider timestamp for the wire.

    `value` is whatever the SDK handed back (a real `datetime`, or already
    a string in some SDK shapes) — never invents a timestamp when `value`
    is `None`.
    """
    if value is None:
        return None
    iso = getattr(value, "isoformat", None)
    if callable(iso):
        try:
            return iso()
        except Exception:
            pass
    return str(value)


def _as_utc_datetime(value) -> datetime | None:
    """Normalize a provider timestamp (datetime or ISO string) to aware UTC.

    Returns `None` on anything unparsable rather than raising — a
    freshness check must degrade to "unknown", never crash the read.
    """
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _quote_freshness(market_as_of, session_open) -> str:
    """Classify a live quote as `"current"` / `"stale"` / `"unknown"` from
    an EXCHANGE SESSION BOUNDARY — never an invented elapsed-time cutoff
    (docs/WORK.md item 15; no-arbitrary-numbers doctrine).

    `session_open` is today's regular-session open per Alpaca's own trading
    calendar (`AlpacaBroker.get_session_open`) — a fact the exchange
    publishes months in advance, not fitted from this desk's own data. A
    last-trade timestamp from BEFORE that boundary means nothing has traded
    for this symbol since the market opened (or today isn't a trading day
    at all, or the calendar lookup failed) — in every one of those cases we
    do not know the quote is current, so "stale" (not "current") is the
    honest, safe-by-default answer. `market_as_of` is missing (`None`) only
    when the read supplied no timestamp at all, which is genuinely
    "unknown", not "stale".
    """
    observed = _as_utc_datetime(market_as_of)
    if observed is None:
        return "unknown"
    if session_open is None:
        # Not a trading day, or the calendar lookup failed — cannot
        # honestly derive a boundary. Do not guess "current".
        return "unknown"
    return "current" if observed >= session_open else "stale"


@lru_cache(maxsize=1)
def _get_broker() -> AlpacaBroker:
    """Lazily-built, process-wide singleton `AlpacaBroker`.

    Built once from the narrow `deps.get_alpaca_credentials()` /
    `deps.get_alpaca_paper()` accessors — never from the full `AppConfig` —
    so this module never holds a reference to `ApiKeysConfig` or any other
    plaintext-secret-bearing object beyond the two strings needed to
    construct the SDK client.
    """
    key, secret = get_alpaca_credentials()
    paper = get_alpaca_paper()
    return AlpacaBroker(api_key=key, secret_key=secret, paper=paper)


def read_account() -> dict:
    """Best-effort read of `{cash, portfolio_value, last_equity}`.

    Never raises. On success: `{"cash": ..., "portfolio_value": ...,
    "last_equity": ..., "error": None}`. On failure: all three numeric
    fields `None`, `"error"` set to a short message.
    """
    try:
        broker = _get_broker()
        acct = broker.get_account()
        return {
            "cash": acct.get("cash"),
            "portfolio_value": acct.get("portfolio_value"),
            "last_equity": acct.get("last_equity"),
            "error": None,
        }
    except Exception as exc:
        record_read_fault("read_account", exc)
        return {
            "cash": None,
            "portfolio_value": None,
            "last_equity": None,
            "error": str(exc),
        }


def read_positions() -> dict:
    """Best-effort read of all open positions as plain dicts.

    Never raises. `{"positions": [...], "error": None|str}`.
    """
    try:
        broker = _get_broker()
        positions = broker.get_positions()
        try:
            sweep_symbol = get_cash_sweep_symbol()
        except Exception as exc:
            # A config-read failure must degrade only the direction/
            # is_cash_equivalent labeling, never the whole positions read —
            # same "one subsystem's failure never masks the rest" posture
            # as every other broker_reads function.
            record_read_fault("read_positions.cash_sweep", exc)
            sweep_symbol = None
        retrieved_at = _utc_now().isoformat()
        out = []
        for p in positions:
            out.append(
                {
                    "symbol": p.symbol,
                    "qty": p.qty,
                    "avg_entry": p.avg_entry,
                    "current_price": p.current_price,
                    # Alpaca's position response is a broker MARK, not a
                    # market-data quote, and supplies no mark timestamp — so
                    # market_as_of is always None and freshness is always
                    # "unknown" here by construction (docs/WORK.md item 15).
                    # retrieved_at is real: when THIS read happened, not when
                    # the market observed the price — never conflate the two.
                    "position_mark": {
                        "value": p.current_price,
                        "price_kind": "broker_position_mark",
                        "provider": "alpaca",
                        "feed": "broker_position",
                        "market_as_of": None,
                        "retrieved_at": retrieved_at,
                        "freshness": "unknown",
                    },
                    "market_value": p.market_value,
                    "unrealized_pnl": p.unrealized_pnl,
                    "unrealized_intraday_pnl": getattr(p, "unrealized_intraday_pnl", None),
                    "sector": getattr(p, "sector", None),
                    "is_cash_equivalent": p.symbol == sweep_symbol,
                    "direction": _position_direction(p.symbol, sweep_symbol, p.qty),
                }
            )
        return {"positions": out, "error": None}
    except Exception as exc:
        record_read_fault("read_positions", exc)
        return {"positions": [], "error": str(exc)}


def read_margin_interest(cash: float | None) -> dict:
    """Margin interest ESTIMATE (spec §11.2) — MEASURES only, never a risk
    decision, and never gates anything in this read-only module.

    Takes the account's already-read `cash` figure (the caller — `/account`
    — has just fetched it via `read_account()`; this avoids a second broker
    round-trip for the same number). Returns
    `{"debit_balance", "rate_pct", "daily_usd", "annual_usd", "label",
    "broker_check_note", "days_charged", "period_usd", "error"}`. Never
    raises.

    `days_charged`/`period_usd` mirror the Telegram alert's own multi-day
    carry (`src.notifier._margin_interest_lines`): Alpaca charges for every
    calendar day a debit is carried, so a Friday overnight is 3 days
    (weekend), not 1. Computed the same way the Telegram path does — via
    `src.margin_interest.days_charged_until_next_trading_day`, fed the
    broker's own `is_trading_day` calendar check — so the dashboard cannot
    drift from the alert. Degrades to 1 (the pre-existing flat figure) on
    any calendar read failure, same as the Telegram path.

    THREE distinct shapes, and the caller must keep them apart (owner
    decision 2026-09-18 — see `margin_interest.format_daily_line`):

      * a real debit balance: every field populated, `label` carrying the
        ESTIMATE framing verbatim, `error` `None`;
      * nothing borrowed: an EXPLICIT zero — `debit_balance`/`daily_usd`/
        `annual_usd` all `0.0`, the real configured `rate_pct`, `label`
        `None` (a certain zero is not an estimate) and `error` `None`.
        This used to be an all-`None` dict that the cockpit rendered as
        nothing at all, which was indistinguishable from a dead tracker;
      * a fault (broker read failed, or no rate configured): numeric
        fields `None` and `error` set. `error` is what tells a genuine
        zero apart from a failure — never read a `None` figure as zero.

    Deliberately does NOT gate on `limits.allow_margin` before looking at
    `cash` — interest is a broker-side fact about the account's actual
    overnight cash balance, not a consequence of QAMC's own risk toggle.
    `cash_only` (src/risk/rules.py) hard-blocks a plain BUY from taking
    cash negative when `allow_margin` is `False`, but a COVER is exempt
    from that rule by design (D10 — a COVER can never be hard-blocked;
    see `src/agents/portfolio_manager.py`'s DE-LEVER MANDATE, which
    already handles "cash is negative AND allow_margin is False" as a
    real, live state). `overnight_debit_balance()` reads `cash` alone and
    already degrades cleanly to `0.0` when nothing was borrowed, so it is
    always safe — and now correct in the COVER-driven case too — to call
    unconditionally.
    """
    empty = {
        "debit_balance": None,
        "rate_pct": None,
        "daily_usd": None,
        "annual_usd": None,
        "label": None,
        "broker_check_note": None,
        "days_charged": None,
        "period_usd": None,
        "error": None,
        "cumulative": None,
    }
    try:
        rate_pct = get_risk_limits().margin_interest_rate_pct
    except Exception as exc:
        record_read_fault("margin.config", exc)
        return {**empty, "error": str(exc)}

    try:
        from src.margin_interest import (
            build_estimate,
            days_charged_until_next_trading_day,
            overnight_debit_balance,
        )

        debit_balance = overnight_debit_balance(cash)
        # Same calendar lookahead the Telegram alert uses — a broker/calendar
        # hiccup degrades to 1 (the flat per-day figure shown before this
        # existed) and must never turn a readable cash balance into a fault.
        try:
            from src.util.time import et_today

            days_charged = days_charged_until_next_trading_day(
                _get_broker().is_trading_day,
                et_today(),
            )
        except Exception as exc:
            record_read_fault("margin.calendar", exc)
            days_charged = 1
        estimate = build_estimate(debit_balance, rate_pct, days_charged)
    except Exception as exc:
        record_read_fault("margin.estimate", exc)
        return {**empty, "error": str(exc)}

    if estimate is None:
        # OWNER DECISION 2026-09-18, verbatim: "Yes, every day, even if
        # it's zero, that way I know it's still working." An explicit,
        # honest zero — NOT the all-`None` silence this used to return,
        # which the cockpit rendered as nothing at all and was therefore
        # indistinguishable from a dead tracker. This deliberately
        # overrides the spec's original noise policy.
        #
        # `rate_pct` is the real configured rate (already read above) and
        # the three dollar figures are a true zero: nothing was borrowed,
        # so nothing is owed at any rate. `label` stays `None` because
        # this is not an estimate — see `margin_interest.ZERO_DEBIT_LINE`.
        # `error` stays `None`, which is how a caller tells a real zero
        # apart from a failed read.
        #
        # `build_estimate` returns `None` for a missing/zero RATE as well
        # as for a zero debit, so that case is separated out FIRST — a
        # mis-keyed or deleted `risk.margin_interest_rate_pct` must reach
        # the cockpit as a fault, never as a reassuring $0.00 on the one
        # day the owner most needs to know the tracker is broken.
        if not rate_pct or rate_pct <= 0:
            return {**empty, "error": "no borrowing rate is configured"}
        return {
            **empty,
            "debit_balance": 0.0,
            "rate_pct": rate_pct,
            "daily_usd": 0.0,
            "annual_usd": 0.0,
            "days_charged": days_charged,
            "period_usd": 0.0,
            "cumulative": _compute_cumulative(),
        }

    broker_check_note = None
    activities: list[dict] = []
    try:
        from src.margin_interest import compare_estimate_to_broker_activity

        broker = _get_broker()
        activities = broker.get_margin_interest_activities()
        comparison = compare_estimate_to_broker_activity(estimate, activities)
        if comparison is not None:
            broker_check_note = comparison.note
    except Exception as exc:
        # The INT-activity check is a nicety layered on top of the
        # ESTIMATE — its failure must never hide the estimate itself.
        record_read_fault("margin.int_check", exc)

    return {
        "debit_balance": estimate.debit_balance,
        "rate_pct": estimate.rate_pct,
        "daily_usd": estimate.daily_usd,
        "annual_usd": estimate.annual_usd,
        "label": estimate.label,
        "broker_check_note": broker_check_note,
        "days_charged": estimate.days_charged,
        "period_usd": estimate.period_usd,
        "error": None,
        "cumulative": _compute_cumulative(),
    }


def _compute_cumulative() -> dict | None:
    """The owner-facing cumulative view — this week / current month / up to
    6 months / all-time (`src.margin_interest.compute_cumulative_margin_interest`).

    Prefers the broker's own FULL `INT` activity history — a fresh,
    unfiltered fetch, deliberately NOT the same `activities` list the
    caller's own overnight broker-check may have just pulled: a bucketed
    "all-time" needs the whole ledger, not one night's slice.
    Falls back to our own persisted daily-accrual ESTIMATE rows
    (`margin_interest_daily`) only when the broker has never posted a real
    `INT` row. Returns `None` — never a fabricated result — on any read
    failure; the cockpit/Telegram render that as "not available", same as
    every other fault in this module.
    """
    try:
        from src.api.db_reads import get_margin_interest_daily_all
        from src.margin_interest import compute_cumulative_margin_interest
        from src.util.time import et_today

        broker = _get_broker()
        full_history = broker.get_margin_interest_activities()
        estimate_rows = get_margin_interest_daily_all()
        result = compute_cumulative_margin_interest(full_history, estimate_rows, et_today())
        return {
            "this_week_usd": result.this_week_usd,
            "current_month_usd": result.current_month_usd,
            "current_month_label": result.current_month_label,
            "prior_months": result.prior_months,
            "all_time_usd": result.all_time_usd,
            "all_time_since": result.all_time_since,
            "is_estimate": result.is_estimate,
            "source": result.source,
        }
    except Exception as exc:
        record_read_fault("compute_cumulative", exc)
        return None


_STATUS_MAP_NAMES = {"open": "OPEN", "closed": "CLOSED", "all": "ALL"}


def _extract_order_field(getter, default=None):
    """Run a single per-field extraction; on any error return `default`
    instead of propagating — one malformed field must never take down the
    whole order line item (and one malformed order must never take down
    the whole response)."""
    try:
        return getter()
    except Exception:
        return default


def _order_to_dict(o) -> dict:
    """Defensively flatten one Alpaca order SDK object into a plain dict.

    Mirrors the enum-vs-string / TypeError-vs-ValueError defensive style
    already used at `src/execution/broker.py:905-961`
    (`AlpacaBroker.list_recent_orders`), reimplemented fresh here rather
    than imported since this module must not touch write-capable broker
    internals. Every field is extracted independently so one bad field
    degrades to `None` rather than raising out of the whole row.
    """

    def _str_or_none(val):
        if val is None:
            return None
        return str(val)

    def _float_or_none(val):
        if val is None:
            return None
        try:
            return float(val)
        except (TypeError, ValueError):
            return None

    def _enum_value(attr_name):
        raw = getattr(o, attr_name, None)
        return str(getattr(raw, "value", raw)).lower() if raw is not None else None

    def _dt_str(attr_name):
        raw = getattr(o, attr_name, None)
        if raw is None:
            return None
        iso = getattr(raw, "isoformat", None)
        if callable(iso):
            try:
                return iso()
            except Exception:
                return str(raw)
        return str(raw)

    order_id = _extract_order_field(lambda: _str_or_none(getattr(o, "id", None)))
    # Alpaca's order objects carry ITS class-share spelling (e.g. "BRK.B"),
    # while `read_positions` (via `AlpacaBroker.get_positions`) already
    # normalizes to the QAMC/yfinance canonical form ("BRK-B") the rest of
    # the cockpit and universe use. Left untranslated here, the cockpit's
    # symbol-string equality checks (e.g. `findPositionStop` matching a
    # resting stop order to its position) silently fail for every
    # punctuated ticker — Berkshire's chart showed no stop line for exactly
    # this reason even though a live protective stop existed at the broker.
    symbol = _extract_order_field(
        lambda: (
            _internal_symbol(_str_or_none(getattr(o, "symbol", None)))
            if getattr(o, "symbol", None) is not None
            else None
        )
    )
    return {
        "id": order_id,
        "symbol": symbol,
        "side": _extract_order_field(lambda: _enum_value("side")),
        "qty": _extract_order_field(lambda: _float_or_none(getattr(o, "qty", None))),
        "order_type": _extract_order_field(lambda: _enum_value("order_type") or _enum_value("type")),
        "status": _extract_order_field(lambda: _enum_value("status")),
        "limit_price": _extract_order_field(lambda: _float_or_none(getattr(o, "limit_price", None))),
        "stop_price": _extract_order_field(lambda: _float_or_none(getattr(o, "stop_price", None))),
        "filled_qty": _extract_order_field(lambda: _float_or_none(getattr(o, "filled_qty", None))),
        "filled_avg_price": _extract_order_field(lambda: _float_or_none(getattr(o, "filled_avg_price", None))),
        "submitted_at": _extract_order_field(lambda: _dt_str("submitted_at")),
        "filled_at": _extract_order_field(lambda: _dt_str("filled_at")),
    }


def read_orders(status: str = "open", limit: int = 50) -> dict:
    """Best-effort read of recent/open orders across all symbols.

    `status` should be one of "open" / "closed" / "all" (route-level
    validation is expected to happen before this is called, but an
    unrecognized value here just falls back to "open" rather than raising).

    Never raises. `{"orders": [...], "error": None|str}`. A single
    malformed order object is skipped/degraded rather than aborting the
    whole read.
    """
    try:
        from alpaca.trading.enums import QueryOrderStatus
        from alpaca.trading.requests import GetOrdersRequest

        status_name = _STATUS_MAP_NAMES.get((status or "").lower(), "OPEN")
        query_status = getattr(QueryOrderStatus, status_name)

        broker = _get_broker()
        raw_orders = broker.client.get_orders(filter=GetOrdersRequest(status=query_status, limit=limit, nested=False))

        out = []
        for o in raw_orders or []:
            try:
                out.append(_order_to_dict(o))
            except Exception as exc:
                record_read_fault("read_orders.row", exc)
                continue
        return {"orders": out, "error": None}
    except Exception as exc:
        record_read_fault("read_orders", exc)
        return {"orders": [], "error": str(exc)}


def read_price_bars(symbol: str, lookback_days: int = 120, timeframe: str = "1d") -> dict:
    """Best-effort read of chart OHLCV bars for one symbol/timeframe.

    Wraps `AlpacaBroker.get_bars` / `get_intraday_chart_bars` — market-data
    reads (Alpaca's `StockHistoricalDataClient.get_stock_bars`), never a
    trading/account call. Both already never raise and return `[]` on any
    failure; this wrapper only adds the same `{"bars": [...], "error":
    None|str}` degradation contract every other function in this module
    uses, so a chart panel can distinguish "no data" from "read failed."

    Each bar also carries `close_price`, a `PriceObservation`-shaped dict
    (docs/WORK.md item 15): `market_as_of` is the bar's OWN date/timestamp
    as Alpaca reports it (never our retrieval time standing in for it) and
    `freshness` is always `"historical"` — a completed/forming OHLCV bar is
    a look-back structure by construction, not a claim about "right now."
    `feed` is only ever set to `"iex"` when the request explicitly asked
    for it (5m/15m/1h, `get_intraday_chart_bars`); daily bars don't select
    a feed explicitly, so `feed` is left `None` there rather than guessed.
    """
    try:
        broker = _get_broker()
        retrieved_at = _utc_now().isoformat()
        if timeframe == "1d":
            bars = broker.get_bars(symbol, lookback_days=lookback_days)
        else:
            bars = broker.get_intraday_chart_bars(symbol, timeframe=timeframe, lookback_days=lookback_days)
        out = []
        for b in bars:
            date_str = b.date.isoformat() if timeframe == "1d" else b["date"]
            timestamp = None if timeframe == "1d" else b["timestamp"]
            close = b.close if timeframe == "1d" else b["close"]
            out.append(
                {
                    "date": date_str,
                    "timestamp": timestamp,
                    "open": b.open if timeframe == "1d" else b["open"],
                    "high": b.high if timeframe == "1d" else b["high"],
                    "low": b.low if timeframe == "1d" else b["low"],
                    "close": close,
                    "volume": b.volume if timeframe == "1d" else b["volume"],
                    "close_price": {
                        "value": close,
                        "price_kind": ("historical_daily_close" if timeframe == "1d" else "historical_intraday_close"),
                        "provider": "alpaca",
                        "feed": None if timeframe == "1d" else "iex",
                        "market_as_of": timestamp if timestamp is not None else date_str,
                        "retrieved_at": retrieved_at,
                        "freshness": "historical",
                    },
                }
            )
        return {"bars": out, "error": None}
    except Exception as exc:
        record_read_fault("read_price_bars", exc, symbol=symbol, timeframe=timeframe)
        return {"bars": [], "error": str(exc)}


def read_live_quotes(symbols: list[str]) -> dict:
    """Best-effort read of current-session quote facts for one or more
    symbols — last trade price, previous session close, and today's
    still-forming session range.

    Wraps `AlpacaBroker.get_intraday_snapshots` — the SAME read-only bulk
    Alpaca snapshot call (`StockHistoricalDataClient.get_stock_snapshot`,
    a market-data read, never an account/order call) the already-accepted,
    already-enabled intraday opportunity scanner uses (`src/pipeline.py`'s
    `_run_intraday_opportunity_scan`). No new broker capability, no new
    external dependency — this only gives Mission Control's read side a
    second consumer of an existing, already-safety-reviewed method.

    Distinct from `read_positions`' `current_price` (held positions only,
    from the trading client) and `read_price_bars`' historical daily bars
    (`/prices`) — this is what lets a chart/candidate view label a true
    current price instead of a historical close implying "now" (2026-08-21
    Mission Control correctness finding).

    Never raises. `{"quotes": {SYMBOL: {...}}, "error": None|str}`. A
    symbol Alpaca couldn't price at all still comes back with every field
    `None` (never dropped), so the caller can tell "no data for this
    symbol" from "didn't ask."

    `quote` (each symbol's `PriceObservation`-shaped provenance for
    `last_price`, docs/WORK.md item 15) uses Alpaca's own `latest_trade
    .timestamp` as `market_as_of` — a real per-trade exchange timestamp
    the SDK's `Trade` model actually carries (verified against the
    installed SDK, 2026-09-13; an existing docstring elsewhere claiming
    the snapshot object exposes no such timestamp was wrong and is being
    corrected alongside this). `freshness` is derived from today's regular
    -session open (`AlpacaBroker.get_session_open`, an exchange session
    boundary), never an invented elapsed-minutes cutoff — see
    `_quote_freshness`.
    """
    try:
        broker = _get_broker()
        raw = broker.get_intraday_snapshots(symbols)
        retrieved_at = _utc_now()
        try:
            session_open = broker.get_session_open()
        except Exception as exc:
            record_read_fault("live_quotes.session_open", exc)
            session_open = None
        quotes = {}
        any_data = False
        for sym in symbols:
            snap = raw.get(sym) or {}
            last_price = snap.get("last_price")
            if last_price is not None or snap.get("prev_close") is not None:
                any_data = True
            last_trade_at = snap.get("last_trade_at")
            # item 120: Alpaca returns the PREVIOUS session's daily bar in
            # the `daily_bar` slot for a name that has not printed today, so
            # these three were capable of showing yesterday's range on the
            # cockpit as this session's. Blank them unless the bar's own
            # timestamp says today.
            session_is_today = live_price_is_today(snap.get("session_bar_at"))
            # item 169: `last_price` above is the RAW provider last trade,
            # never freshness-checked (see `get_intraday_snapshots`'
            # docstring) — a thin name can carry a stale print here while
            # still resolving to a real today price via the minute/session
            # bar. `resolved_price` is `src.data.live_price.resolve_live_price`
            # run over the SAME snapshot: `None` unless a real print from
            # THIS session exists, so a chart consumer never draws a stale
            # last trade as today's close.
            resolved_price = resolve_live_price(snap).price
            quotes[sym] = {
                "last_price": last_price,
                "resolved_price": resolved_price,
                "quote": {
                    "value": last_price,
                    "price_kind": "current_quote",
                    "provider": "alpaca",
                    "feed": None,  # snapshot request doesn't explicitly select a feed
                    "market_as_of": _iso_or_none(last_trade_at),
                    "retrieved_at": retrieved_at.isoformat(),
                    "freshness": _quote_freshness(last_trade_at, session_open),
                }
                if last_price is not None
                else None,
                "prev_close": snap.get("prev_close"),
                "session_bar_is_today": session_is_today,
                "session_open": snap.get("session_open") if session_is_today else None,
                "session_high": snap.get("session_high") if session_is_today else None,
                "session_low": snap.get("session_low") if session_is_today else None,
            }
        # get_intraday_snapshots itself never raises — a total read failure
        # (bad/absent credentials, market-data outage) degrades to `{}` for
        # every symbol, identical on the wire to "Alpaca genuinely has no
        # snapshot for any of these." That ambiguity is a real degraded
        # state ("Preserve explicit stale/error states" — do not let it
        # render as a silent absence of a live-quote caption): every
        # requested symbol coming back completely empty is the honest
        # signal to surface, since one bad symbol in an otherwise-healthy
        # batch would not zero out every other symbol too.
        error = None if any_data or not symbols else "no quote data returned for any requested symbol"
        return {"quotes": quotes, "error": error}
    except Exception as exc:
        record_read_fault("live_quotes", exc, symbols=len(symbols))
        return {"quotes": {sym: {} for sym in symbols}, "error": str(exc)}


def check_broker_reachable() -> bool | None:
    """Best-effort connectivity ping.

    Returns `True`/`False` when credentials are present (successful vs.
    failed `get_account()` call), or `None` when credentials are entirely
    absent/empty so a caller can distinguish "not configured" from
    "configured but currently down".
    """
    try:
        key, secret = get_alpaca_credentials()
    except Exception as exc:
        record_read_fault("reachable.credentials", exc)
        return None
    if not key or not secret:
        return None
    try:
        broker = _get_broker()
        broker.get_account()
        return True
    except Exception as exc:
        record_read_fault("reachable.account", exc)
        return False
