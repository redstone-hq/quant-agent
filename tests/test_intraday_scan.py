"""Intraday opportunity-discovery (2026-08-19 fix) invariants.

The full opportunity-generation chain (macro/news/tech/earnings -> PM -> RM
-> deterministic gate -> execution) previously ran once each morning;
`intra_check` (every ~30 min) was loss-protection only, so a material move
developing after the morning run could not generate a new trade.

This suite covers `TradingPipeline._run_intraday_opportunity_scan` and its
wiring into `run_intra_check`:
  1. Disabled by default — a config without `intraday_scan.enabled: true`
     leaves intra_check's existing behavior completely unchanged.
  2. A material move (bullish OR via an inverse ETF, bearish) that clears
     the threshold reaches the SAME DecisionStage -> RiskStage ->
     ExecutionStage chain morning uses.
  3. A move below threshold, or a symbol still in cooldown, is skipped.
  4. A daily-loss breach suppresses the scan even when there was nothing
     to force-liquidate.
  5. The scan backs off while another session is mid-flight. `intra_check`
     is deliberately exempt from the wrapper's cross-mode session lock
     (`scripts/run_if_et_window.sh`) so the circuit breaker always fires —
     an exemption justified on the grounds that intra_check's actions are
     all idempotent. Opening a new position is not, so this path needs its
     own fail-closed concurrency guard.
"""

import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from tests.pipeline_factory import build_pipeline, test_config
from src.config import IntradayScanConfig
from src.cost_circuit import PaidAnalysisSuspended
from src.models import MacroNarrative, NewsIntelligenceReport, Position, TechAnalysisResult, TechReasoningChain
from src.pipeline import TradingPipeline
from src.pipeline_context import RunContext
from src.trading_calendar import et_today


def _ta_result(symbol, rating="buy"):
    return TechAnalysisResult(
        symbol=symbol,
        rating=rating,
        conviction="medium",
        entry_price=100.0,
        stop_loss=95.0,
        reference_target=110.0,
        support_levels=[95.0],
        resistance_levels=[110.0],
        setup_type="range",
        expected_horizon_sessions=10,
        reasoning_chain=TechReasoningChain(
            trend="x",
            momentum="x",
            volatility="x",
            volume="x",
            support_resistance="x",
        ),
        reasoning="test",
        thesis_invalid_if="closes below support",
    )


def _todays_macro_state():
    """A same-day snapshot that still validates as MacroAnalysis.

    A chain-less trim is no longer usable remembered macro (silent broken
    into PM). Tests that mean 'today's good carry' must look like the
    persisted live shape.
    """
    from src.models import MacroAnalysis, MacroPositionGuidance, MacroReasoningChain

    return {
        "date": str(et_today()),
        **MacroAnalysis(
            reasoning_chain=MacroReasoningChain(
                volatility_analysis="vix ok",
                yield_curve_analysis="curve ok",
                monetary_policy_analysis="fed ok",
                inflation_labor_credit="cpi ok",
                cross_signal_synthesis="together ok",
                sector_implications="tech ow",
            ),
            regime="risk-on",
            confidence="medium",
            equity_outlook="bullish",
            position_guidance=MacroPositionGuidance(
                target_invested_pct=70,
                cash_recommendation_pct=30,
                reasoning="stay invested",
            ),
            summary="risk on",
            sector_guidance=[],
        ).model_dump(),
    }


def _todays_news_dump():
    return NewsIntelligenceReport(
        macro_narrative=MacroNarrative(
            last_updated=str(et_today()),
            era_themes=["test"],
            current_regime="risk-on, test",
        ),
        state_changes=[],
        stock_news={},
        pm_briefing="test",
        market_sentiment="bullish",
        confidence="medium",
    ).model_dump()


def _intraday_pipeline(
    universe=("SPY", "SQQQ", "AAPL"),
    enabled=True,
    cooldown_hours=3.0,
    atr=2.0,
    cooldown_rows=None,
    db_path=None,
):
    # REAL constructor, production settings; the scan's advisory flock gets a per-test temp dir.
    config = test_config(
        trading={"universe": list(universe), "lookback_days": 100},
        storage={"db_path": str(db_path or (Path(tempfile.mkdtemp()) / "t.db"))},
        intraday_scan=IntradayScanConfig(
            enabled=enabled,
            cooldown_hours=cooldown_hours,
        ).model_dump(),
    )
    broker = MagicMock()
    broker.get_account.return_value = {
        "cash": 10_000.0,
        "portfolio_value": 10_100.0,
        "last_equity": 10_000.0,
        "non_marginable_buying_power": 10_000.0,
    }
    broker.get_positions.return_value = []
    db = MagicMock(**{"get_trades.return_value": cooldown_rows or []})
    market = MagicMock(**{"get_ohlcv.return_value": [MagicMock()]})  # non-empty; indicators patched
    macro_store = MagicMock(**{"load_last_state.return_value": _todays_macro_state()})
    news_store = MagicMock(**{"load_daily_report.return_value": _todays_news_dump()})
    tech_store = MagicMock(**{"load.return_value": {}, "compute_ages.return_value": {}})
    p = build_pipeline(
        config,
        broker=broker,
        db=db,
        market=market,
        macro_store=macro_store,
        news_store=news_store,
        tech_store=tech_store,
        tech_analyst=MagicMock(),
        decision_stage=MagicMock(),
        risk_stage=MagicMock(),
        execution_stage=MagicMock(),
        sec_form4_provider=MagicMock(name="sec_form4_provider"),
        earnings_provider=MagicMock(name="earnings_provider"),
        news_provider=MagicMock(name="news_provider"),
    )
    # The scan ranks by move / the name's own ATR; a name with no ATR is
    # dropped (`no_atr`). Default: every name has an ATR of `atr` (prev=100 -> atr%);
    # a dict gives per-name ATRs, and a name absent from it has none.
    p._atr_for_symbol = MagicMock(side_effect=atr.get) if isinstance(atr, dict) else MagicMock(return_value=atr)
    return p


def _snapshot(last, prev, *, trade_at="today"):
    """A snapshot whose last trade is from TODAY unless a test says otherwise.

    board item 120: the mover scan now resolves the price through
    `src.data.live_price.resolve_live_price`, so a payload with no timestamp
    at all is correctly not-today and buys no paid look. Every test here
    means "this name traded today and moved", so the default stamps it
    inside today's regular session; pass `trade_at=<datetime>` or
    `trade_at=None` to say otherwise.

    The default used to be `et_now()`, which made every one of these tests
    fail between midnight and 09:30 ET — the resolver requires a stamp at or
    after today's open, not merely on today's date. See
    `tests/session_clock.py` for the whole story.
    """
    from tests.session_clock import todays_session_stamp

    if trade_at == "today":
        trade_at = todays_session_stamp()
    return {"last_price": last, "prev_close": prev, "last_trade_at": trade_at}


# ---------- disabled by default ----------


def test_intraday_scan_config_defaults_disabled():
    cfg = IntradayScanConfig()
    assert cfg.enabled is False


def test_scan_is_inert_when_disabled():
    p = _intraday_pipeline(enabled=False)
    ctx = RunContext.start("intra_check")
    result = p._run_intraday_opportunity_scan(ctx)
    # 2026-08-31 visibility fix: an explicit status, not a bare None — see
    # tests/test_intraday_scan_crash_visibility.py for the full coverage of
    # this and the other two "everyday, healthy, no new activity" statuses.
    assert result == {"status": "intraday_scan_disabled", "run_id": ctx.run_id}
    p.broker.get_intraday_snapshots.assert_not_called()


# ---------- material move reaches the shared decision chain ----------


@patch("src.pipeline_intraday.compute_indicators")
def test_material_bullish_move_reaches_decision_chain(mock_compute_indicators):
    """A symbol that moved well past threshold since the last close must
    flow through tech_analyst and then the SAME DecisionStage -> RiskStage
    -> ExecutionStage objects morning uses — no separate/duplicated PM/RM
    logic for the intraday path."""
    mock_compute_indicators.return_value = MagicMock()
    p = _intraday_pipeline(universe=["SPY", "AAPL"], atr={"AAPL": 2.0})
    p.broker.get_intraday_snapshots.return_value = {
        "SPY": _snapshot(last=500.0, prev=499.0),  # no ATR — dropped as no_atr
        "AAPL": _snapshot(last=110.0, prev=100.0),  # 10% on a 2% ATR — ranked
    }
    analysis = _ta_result("AAPL", rating="buy")
    p.tech_analyst.analyze_batch.return_value = (
        {"AAPL": analysis},
        MagicMock(
            user_message="m", raw_text="{}", tokens_used=1, input_tokens=1, output_tokens=1, cost_usd=0.0, model="t"
        ),
    )
    p.decision_stage.run.side_effect = lambda ctx: setattr(
        ctx,
        "portfolio_decision",
        SimpleNamespace(decisions=[SimpleNamespace(action="BUY", symbol="AAPL")]),
    )
    p.risk_stage.run.return_value = None  # no early-exit -> proceed to execution
    p.execution_stage.run.return_value = [{"id": "o1", "action": "BUY", "symbol": "AAPL"}]

    ctx = RunContext.start("intra_check")
    result = p._run_intraday_opportunity_scan(ctx)

    assert result["status"] == "intraday_executed"
    assert result["candidates"] == ["AAPL"]
    # tech_analyst was only asked about the qualifying symbol, not SPY.
    submitted_symbols = [s["symbol"] for s in p.tech_analyst.analyze_batch.call_args.args[0]]
    assert submitted_symbols == ["AAPL"]
    # The exact same stage objects morning uses were invoked — proves reuse,
    # not a parallel decision path.
    p.decision_stage.run.assert_called_once_with(ctx)
    p.risk_stage.run.assert_called_once_with(ctx)
    p.execution_stage.run.assert_called_once_with(ctx)
    assert ctx.analyses == [analysis]
    # Intentional skip (earnings not re-fetched) is not a lost answer —
    # the gate must still let the PM run. Empty/failed carry-forward is
    # a different word and is tested separately.
    # A real earnings provider with nothing cached reports "chose_not_to_refetch" (was "not_run_intraday").
    assert ctx.data_status["earnings"] == "chose_not_to_refetch"
    assert ctx.data_status["macro"] == "carried_from_morning"
    assert ctx.data_status["news"] == "carried_from_morning"
    from src import evidence_gate

    assert [k for k, v in ctx.data_status.items() if evidence_gate.counts_as_degraded(v)] == []


@patch("src.pipeline_intraday.compute_indicators")
def test_bearish_move_surfaces_through_inverse_etf(mock_compute_indicators):
    """A broad-market decline shows up as a qualifying move in an approved
    inverse ETF (SQQQ) the same way a rally shows up in a long candidate —
    no separate bearish code path, no direct shorting."""
    mock_compute_indicators.return_value = MagicMock()
    p = _intraday_pipeline(universe=["SPY", "SQQQ"], atr={"SQQQ": 2.0})
    p.broker.get_intraday_snapshots.return_value = {
        "SPY": _snapshot(last=498.0, prev=500.0),  # -0.4%, below threshold
        "SQQQ": _snapshot(last=112.0, prev=100.0),  # +12% (3x inverse Nasdaq)
    }
    analysis = _ta_result("SQQQ", rating="buy")
    p.tech_analyst.analyze_batch.return_value = (
        {"SQQQ": analysis},
        MagicMock(
            user_message="m", raw_text="{}", tokens_used=1, input_tokens=1, output_tokens=1, cost_usd=0.0, model="t"
        ),
    )
    p.decision_stage.run.side_effect = lambda ctx: setattr(
        ctx,
        "portfolio_decision",
        SimpleNamespace(decisions=[SimpleNamespace(action="BUY", symbol="SQQQ")]),
    )
    p.risk_stage.run.return_value = None
    p.execution_stage.run.return_value = [{"id": "o1", "action": "BUY", "symbol": "SQQQ"}]

    ctx = RunContext.start("intra_check")
    result = p._run_intraday_opportunity_scan(ctx)

    assert result["status"] == "intraday_executed"
    assert result["candidates"] == ["SQQQ"]
    submitted_symbols = [s["symbol"] for s in p.tech_analyst.analyze_batch.call_args.args[0]]
    assert submitted_symbols == ["SQQQ"], "SPY has no ATR, so it is not ranked"


# ---------- cooldown / dedup ----------


@patch("src.pipeline_intraday.compute_indicators")
def test_cooldown_suppresses_repeat_scan_of_same_symbol(mock_compute_indicators):
    """A symbol already evaluated by an intra_check-triggered scan within
    the cooldown window must not be re-submitted to tech_analyst on the
    very next tick, even though it's still moved past threshold —
    otherwise a slow-developing move churns the same setup every 30 min."""
    from datetime import datetime, timedelta, timezone

    mock_compute_indicators.return_value = MagicMock()
    # Aged 30 min (not "now"): `_recently_intraday_evaluated`'s
    # legacy-fallback cooldown lookup reads `db.get_trades`. 30 min stays
    # well inside the 3h cooldown window under test.
    aged_ts = (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat()
    p = _intraday_pipeline(
        universe=["AAPL"],
        cooldown_hours=3.0,
        cooldown_rows=[
            {
                "symbol": "AAPL",
                "action": "HOLD",
                "run_id": "intra_check-abcd1234",
                "timestamp": aged_ts,
                "reasoning": "prior intraday scan",
            }
        ],
    )
    p.broker.get_intraday_snapshots.return_value = {
        "AAPL": _snapshot(last=110.0, prev=100.0),  # still 10% — well past threshold
    }

    ctx = RunContext.start("intra_check")
    result = p._run_intraday_opportunity_scan(ctx)

    assert result == {"status": "intraday_scan_no_opportunity", "run_id": ctx.run_id}
    p.tech_analyst.analyze_batch.assert_not_called()


@patch("src.pipeline_intraday.compute_indicators")
def test_cooldown_expired_allows_rescan(mock_compute_indicators):
    """A prior intraday-scan row OLDER than the cooldown window must not
    suppress a fresh scan — the dedup is time-bounded, not permanent."""
    from datetime import datetime, timedelta, timezone

    mock_compute_indicators.return_value = MagicMock()
    stale_ts = (datetime.now(timezone.utc) - timedelta(hours=6)).isoformat()
    p = _intraday_pipeline(
        universe=["AAPL"],
        cooldown_hours=3.0,
        cooldown_rows=[
            {
                "symbol": "AAPL",
                "action": "HOLD",
                "run_id": "intra_check-abcd1234",
                "timestamp": stale_ts,
                "reasoning": "prior intraday scan",
            }
        ],
    )
    p.broker.get_intraday_snapshots.return_value = {
        "AAPL": _snapshot(last=110.0, prev=100.0),
    }
    p.tech_analyst.analyze_batch.return_value = ({}, None)

    ctx = RunContext.start("intra_check")
    p._run_intraday_opportunity_scan(ctx)

    p.tech_analyst.analyze_batch.assert_called_once()


@patch("src.pipeline_intraday.compute_indicators")
def test_non_intra_check_trades_do_not_count_toward_cooldown(mock_compute_indicators):
    """A morning-run HOLD/BUY for the symbol must not suppress the
    intraday scan — the symbol cooldown is specifically about repeat
    INTRADAY scans, not about any historical trade row.

    The row is aged 30 min so it sits OUTSIDE the 15-min concurrent-session
    guard but well INSIDE the 3h symbol cooldown window — isolating the
    cooldown behavior under test from the separate concurrency guard."""
    from datetime import datetime, timedelta, timezone

    mock_compute_indicators.return_value = MagicMock()
    morning_ts = (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat()
    p = _intraday_pipeline(
        universe=["AAPL"],
        cooldown_rows=[
            {
                "symbol": "AAPL",
                "action": "HOLD",
                "run_id": "run-abcd1234",
                "timestamp": morning_ts,
                "reasoning": "morning run",
            }
        ],
    )
    p.broker.get_intraday_snapshots.return_value = {
        "AAPL": _snapshot(last=110.0, prev=100.0),
    }
    p.tech_analyst.analyze_batch.return_value = ({}, None)

    ctx = RunContext.start("intra_check")
    p._run_intraday_opportunity_scan(ctx)

    p.tech_analyst.analyze_batch.assert_called_once()


def _held_position(symbol: str) -> Position:
    return Position(
        symbol=symbol,
        qty=10.0,
        avg_entry=100.0,
        current_price=100.2,
        market_value=1_002.0,
        unrealized_pnl=2.0,
        sector="Technology",
    )


# ---------- held names join the Tech batch (produce Tech, don't drop) ----------


@patch("src.pipeline_intraday.compute_indicators")
def test_held_quiet_name_joins_intraday_tech_batch(mock_compute_indicators):
    """A name we already hold, even if it did not move 3%, must be in the
    paid Tech batch. Otherwise an increase on that hold fails
    'lacks a current-run Technical analysis' and voids the whole PM
    decision after the paid call. Missing Tech is a producing-step
    defect; this is that step."""
    mock_compute_indicators.return_value = MagicMock()
    p = _intraday_pipeline(universe=["SPY", "AAPL", "MSFT"], atr={"AAPL": 2.0})
    p.broker.get_intraday_snapshots.return_value = {
        "SPY": _snapshot(last=500.0, prev=499.0),  # 0.2% — below threshold
        "AAPL": _snapshot(last=110.0, prev=100.0),  # 10% — qualifies as mover
        "MSFT": _snapshot(last=100.2, prev=100.0),  # 0.2% — quiet hold
    }
    mover = _ta_result("AAPL", rating="buy")
    held = _ta_result("MSFT", rating="buy")
    p.tech_analyst.analyze_batch.return_value = (
        {"AAPL": mover, "MSFT": held},
        MagicMock(
            user_message="m", raw_text="{}", tokens_used=1, input_tokens=1, output_tokens=1, cost_usd=0.0, model="t"
        ),
    )
    p.decision_stage.run.side_effect = lambda ctx: setattr(
        ctx,
        "portfolio_decision",
        SimpleNamespace(decisions=[SimpleNamespace(action="BUY", symbol="AAPL")]),
    )
    p.risk_stage.run.return_value = None
    p.execution_stage.run.return_value = []

    ctx = RunContext.start("intra_check")
    ctx.positions = [_held_position("MSFT")]
    result = p._run_intraday_opportunity_scan(ctx)

    submitted = [s["symbol"] for s in p.tech_analyst.analyze_batch.call_args.args[0]]
    assert "AAPL" in submitted
    assert "MSFT" in submitted
    assert "SPY" not in submitted
    assert result["candidates"] == ["AAPL"], "quiet holds are coverage, not movers"
    assert {a.symbol for a in ctx.analyses} == {"AAPL", "MSFT"}
    ledgered = [call.kwargs["symbol"] for call in p.db.record_intraday_evaluation.call_args_list]
    assert "MSFT" not in ledgered, "hold coverage must not consume mover cooldown"


@patch("src.pipeline_intraday.compute_indicators")
def test_quiet_book_without_movers_reads_holds_and_skips_the_pm(
    mock_compute_indicators,
):
    """Owner rulings 2026-10-01 / 2026-10-12 (reverses the old "no movers ->
    no Tech" pin): every intraday check re-reads the held names so the
    below-entry-bar sale can run, and with no movers the PM is deliberately
    skipped (named on the context, never a PM failure)."""
    mock_compute_indicators.return_value = MagicMock()
    p = _intraday_pipeline(universe=["MSFT"], atr={})
    p.broker.get_intraday_snapshots.return_value = {
        "MSFT": _snapshot(last=100.2, prev=100.0),  # no ATR — not rankable
    }
    p.tech_analyst.analyze_batch.return_value = ({}, None)
    ctx = RunContext.start("intra_check")
    ctx.positions = [_held_position("MSFT")]
    p._run_intraday_opportunity_scan(ctx)
    submitted = [s["symbol"] for s in p.tech_analyst.analyze_batch.call_args.args[0]]
    assert submitted == ["MSFT"]


@patch("src.pipeline_intraday.compute_indicators")
def test_held_mover_is_not_submitted_twice(mock_compute_indicators):
    """A hold that also cleared the move threshold is one Tech name, not two."""
    mock_compute_indicators.return_value = MagicMock()
    p = _intraday_pipeline(universe=["AAPL"])
    p.broker.get_intraday_snapshots.return_value = {
        "AAPL": _snapshot(last=110.0, prev=100.0),
    }
    p.tech_analyst.analyze_batch.return_value = ({}, None)
    ctx = RunContext.start("intra_check")
    ctx.positions = [_held_position("AAPL")]
    p._run_intraday_opportunity_scan(ctx)
    submitted = [s["symbol"] for s in p.tech_analyst.analyze_batch.call_args.args[0]]
    assert submitted == ["AAPL"]


# ---------- bounded cost ----------


# ---------- run_intra_check wiring ----------


def test_run_intra_check_runs_the_scan():
    """Renamed 2026-09-20 (retired item 32). The `..._when_no_breach` half of
    the old name referred to a daily-loss gate that used to skip the scan;
    that gate and its sibling test are gone, so the name promised a
    distinction this test could no longer draw."""
    p = build_pipeline()
    p.config = SimpleNamespace(
        trading=SimpleNamespace(universe=["AAPL"], lookback_days=100),
        intraday_scan=IntradayScanConfig(enabled=True),
    )
    p.broker = MagicMock()
    p.broker.is_trading_day.return_value = True
    p.broker.get_account.return_value = {
        "cash": 10000.0,
        "portfolio_value": 10100.0,
        "last_equity": 10000.0,
        "non_marginable_buying_power": 10000.0,
    }
    p.broker.get_positions.return_value = []
    p.db = MagicMock()
    p._drain_pending_protection_restores = MagicMock()
    p._reconcile_stop_coverage = MagicMock(return_value=[])
    p._reconcile_orphan_pending_submits = MagicMock()
    p._is_trading_day = MagicMock(return_value=True)
    p.risk_engine = MagicMock()
    p._run_intraday_opportunity_scan = MagicMock(
        return_value={"status": "intraday_no_trades", "candidates": []},
    )

    result = p.run_intra_check()

    assert result["status"] == "ok"
    assert result["intraday_scan"]["status"] == "intraday_no_trades"
    p._run_intraday_opportunity_scan.assert_called_once()


def test_run_intra_check_scan_crash_does_not_fail_the_tick():
    """A scan exception must never turn a routine intra_check run into a
    failed/errored result — the loss-protection check it wraps around
    already succeeded — but it must NOT disappear silently either.

    2026-08-30 operator-honesty fix: before this, the exception handler set
    scan_result to None, which meant no `intraday_scan` key was attached at
    all — byte-identical to a scan that ran and correctly found nothing.
    Now a crash attaches an explicit error-status dict (see
    tests/test_intraday_scan_crash_visibility.py for the full coverage of
    that fix); this test only re-asserts the still-non-negotiable half of
    the contract — the tick itself completes as "ok", never as a failure.
    """
    p = build_pipeline()
    p.config = SimpleNamespace(
        trading=SimpleNamespace(universe=["AAPL"], lookback_days=100),
        intraday_scan=IntradayScanConfig(enabled=True),
    )
    p.broker = MagicMock()
    p.broker.is_trading_day.return_value = True
    p.broker.get_account.return_value = {
        "cash": 10000.0,
        "portfolio_value": 10100.0,
        "last_equity": 10000.0,
        "non_marginable_buying_power": 10000.0,
    }
    p.broker.get_positions.return_value = []
    p.db = MagicMock()
    p._drain_pending_protection_restores = MagicMock()
    p._reconcile_stop_coverage = MagicMock(return_value=[])
    p._reconcile_orphan_pending_submits = MagicMock()
    p._is_trading_day = MagicMock(return_value=True)
    p.risk_engine = MagicMock()
    p._run_intraday_opportunity_scan = MagicMock(side_effect=RuntimeError("boom"))

    result = p.run_intra_check()

    assert result["status"] == "ok"
    assert result["intraday_scan"]["status"] == "intraday_scan_crashed"
    assert "boom" in result["intraday_scan"]["error"]


# ---------- concurrency guard (intra_check is session-lock exempt) ----------


@patch("src.pipeline_intraday.compute_indicators")
def test_scan_skips_when_owner_lock_still_held_at_window_end(mock_compute_indicators):
    """Wait, don't skip on a live morning — but if the calendar window is
    already over, paid discovery cannot run this tick. Movers are named
    on the skip so they do not vanish silently."""
    mock_compute_indicators.return_value = MagicMock()
    p = _intraday_pipeline(universe=["AAPL"])
    p._blocking_owner_session = MagicMock(return_value="morning")
    p._intra_window_remaining_s = MagicMock(return_value=0.0)
    p.broker.get_intraday_snapshots.return_value = {
        "AAPL": _snapshot(last=110.0, prev=100.0),
    }

    ctx = RunContext.start("intra_check")
    result = p._run_intraday_opportunity_scan(ctx)

    assert result["status"] == "intraday_scan_lock_contended"
    assert result["run_id"] == ctx.run_id
    assert result["movers"] == ["AAPL"]
    assert "AAPL" in result["reason"]
    p.broker.get_intraday_snapshots.assert_called()
    p.tech_analyst.analyze_batch.assert_not_called()


@patch("src.pipeline_intraday.compute_indicators")
def test_scan_waits_then_runs_when_midday_lock_releases(mock_compute_indicators):
    """The 13:00 tick skipped paid discovery because midday held the owner
    lock. Wait for that process to finish instead of sleeping until the
    next 30-minute fire. Midday is a different cadence than this fire, so
    running paid discovery on release (rather than skipping to the next
    tick) is correct here — unlike morning (see the item 121 test below)."""
    mock_compute_indicators.return_value = MagicMock()
    p = _intraday_pipeline(universe=["AAPL"])
    p._blocking_owner_session = MagicMock(side_effect=["midday", "midday", None])
    p._intra_window_remaining_s = MagicMock(return_value=60.0)
    p.broker.get_intraday_snapshots.return_value = {
        "AAPL": _snapshot(last=110.0, prev=100.0),
    }
    p.tech_analyst.analyze_batch.return_value = ({}, None)

    ctx = RunContext.start("intra_check")
    with patch("time.sleep"):
        p._run_intraday_opportunity_scan(ctx)

    p.tech_analyst.analyze_batch.assert_called_once()
    # Post-wait refresh so we do not size against the pre-fill snapshot.
    p.broker.get_account.assert_called()
    p.broker.get_positions.assert_called()


@patch("src.pipeline_intraday.compute_indicators")
def test_scan_skips_instead_of_hunting_when_morning_lock_releases(
    mock_compute_indicators,
):
    """Item 121: the 09:30 intra_check fire shares SESSION_WINDOWS start
    with morning. Waiting for morning to release then running paid
    discovery on this SAME tick was still the 09:30 open, sold to the
    owner a second time as INTRADAY OPPORTUNITY (measured leftover at
    09:37). It must stay skipped; the next existing half-hour fire is the
    first true paid INTRADAY look, with no invented offset."""
    mock_compute_indicators.return_value = MagicMock()
    p = _intraday_pipeline(universe=["AAPL"])
    p._blocking_owner_session = MagicMock(side_effect=["morning", "morning", None])
    p._intra_window_remaining_s = MagicMock(return_value=60.0)
    p.broker.get_intraday_snapshots.return_value = {
        "AAPL": _snapshot(last=110.0, prev=100.0),
    }
    p.tech_analyst.analyze_batch.return_value = ({}, None)

    ctx = RunContext.start("intra_check")
    with patch("time.sleep"):
        result = p._run_intraday_opportunity_scan(ctx)

    assert result["status"] == "intraday_scan_open_overlap"
    assert result["movers"] == ["AAPL"]
    p.tech_analyst.analyze_batch.assert_not_called()


@patch("src.pipeline_intraday.compute_indicators")
def test_finished_session_trade_rows_do_not_skip_the_scan(mock_compute_indicators):
    """A morning fill sitting in trades is not an in-flight session. The
    15-minute trade-row heuristic was what slept the 09:30 and 13:00
    scans after morning/midday had already finished writing."""
    from datetime import datetime, timezone

    mock_compute_indicators.return_value = MagicMock()
    recent_ts = datetime.now(timezone.utc).isoformat()
    p = _intraday_pipeline(universe=["AAPL"])
    p.db.get_trades.return_value = [
        {
            "symbol": "MSFT",
            "action": "BUY",
            "run_id": "run-morning1",
            "timestamp": recent_ts,
            "reasoning": "morning already filled",
        }
    ]
    p.broker.get_intraday_snapshots.return_value = {
        "AAPL": _snapshot(last=110.0, prev=100.0),
    }
    p.tech_analyst.analyze_batch.return_value = ({}, None)

    ctx = RunContext.start("intra_check")
    p._run_intraday_opportunity_scan(ctx)

    p.tech_analyst.analyze_batch.assert_called_once()


@patch("src.pipeline_intraday.compute_indicators")
def test_scan_proceeds_when_other_session_activity_is_old(mock_compute_indicators):
    """The concurrency guard is time-bounded — a morning run that finished
    hours ago must not suppress the scan for the rest of the day (a resting
    order or an old trade row is not an in-flight session)."""
    from datetime import datetime, timedelta, timezone

    mock_compute_indicators.return_value = MagicMock()
    old_ts = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    p = _intraday_pipeline(universe=["AAPL"])
    p.db.get_trades.return_value = [
        {
            "symbol": "MSFT",
            "action": "BUY",
            "run_id": "run-morning1",
            "timestamp": old_ts,
            "reasoning": "morning run, long finished",
        }
    ]
    p.broker.get_intraday_snapshots.return_value = {
        "AAPL": _snapshot(last=110.0, prev=100.0),
    }
    p.tech_analyst.analyze_batch.return_value = ({}, None)

    ctx = RunContext.start("intra_check")
    p._run_intraday_opportunity_scan(ctx)

    p.tech_analyst.analyze_batch.assert_called_once()


def test_unreadable_owner_lock_fails_closed():
    """An unknowable owner file must skip paid discovery, never proceed.
    Movers still get named on the skip."""
    p = _intraday_pipeline(universe=["AAPL"])
    p._blocking_owner_session = MagicMock(return_value="unreadable")
    p.broker.get_intraday_snapshots.return_value = {
        "AAPL": _snapshot(last=110.0, prev=100.0),
    }

    ctx = RunContext.start("intra_check")
    assert p._another_session_recently_active(ctx.run_id) is True
    assert p._await_paid_scan_slot(ctx.run_id) is True
    result = p._run_intraday_opportunity_scan(ctx)
    assert result["status"] == "intraday_scan_lock_contended"
    assert result["movers"] == ["AAPL"]
    p.tech_analyst.analyze_batch.assert_not_called()


def test_get_trades_error_does_not_skip_paid_discovery():
    """Trade-row query failure is not an in-flight owner lock."""
    p = _intraday_pipeline(universe=["AAPL"])
    p.db.get_trades.side_effect = RuntimeError("db locked")
    p.broker.get_intraday_snapshots.return_value = {}

    ctx = RunContext.start("intra_check")
    assert p._another_session_recently_active(ctx.run_id) is False
    assert p._run_intraday_opportunity_scan(ctx) == {
        "status": "intraday_scan_no_opportunity",
        "run_id": ctx.run_id,
    }


def test_own_run_id_rows_do_not_trip_the_concurrency_guard():
    """The scan's own writes (e.g. a HOLD recorded earlier this same tick)
    must not make it think a different session is running."""
    from datetime import datetime, timezone

    p = _intraday_pipeline(universe=["AAPL"])
    ctx = RunContext.start("intra_check")
    p.db.get_trades.return_value = [
        {
            "symbol": "AAPL",
            "action": "HOLD",
            "run_id": ctx.run_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "reasoning": "self",
        }
    ]

    assert p._another_session_recently_active(ctx.run_id) is False


@patch("src.pipeline_intraday.compute_indicators")
def test_process_lock_excludes_a_second_concurrent_scan(mock_compute_indicators, tmp_path):
    """Independent-review finding: the DB-row concurrency guard can only see
    a rival session AFTER it has written a trade row, so two intra_check
    processes starting at nearly the same instant could both slip through
    and size against the same pre-fill snapshot.

    `scripts/run_if_et_window.sh` makes that impossible in practice (1800s
    ticks vs a ~1230s hard kill), but that lives in a deployment config
    this code cannot read. The advisory flock closes the class outright:
    while one scan holds it, a second pipeline sharing the same db_path
    must decline rather than scan.
    """
    mock_compute_indicators.return_value = MagicMock()
    db_path = tmp_path / "shared.db"

    holder = _intraday_pipeline(universe=["AAPL"], db_path=db_path)
    rival = _intraday_pipeline(universe=["AAPL"], db_path=db_path)
    rival.broker.get_intraday_snapshots.return_value = {
        "AAPL": _snapshot(last=110.0, prev=100.0),
    }

    ctx = RunContext.start("intra_check")
    with holder._intraday_scan_process_lock() as acquired:
        assert acquired is True, "first holder must acquire the lock"
        # A second process on the same db_path must decline...
        with rival._intraday_scan_process_lock() as second:
            assert second is False
        # ...and the full scan entrypoint must therefore do nothing at all.
        assert rival._run_intraday_opportunity_scan(ctx) == {
            "status": "intraday_scan_lock_contended",
            "run_id": ctx.run_id,
        }
        rival.broker.get_intraday_snapshots.assert_not_called()
        rival.tech_analyst.analyze_batch.assert_not_called()


def test_process_lock_is_released_for_the_next_tick(tmp_path):
    """The lock must not wedge: once a scan finishes, the next 30-min tick
    (a fresh process on the same db_path) must be able to acquire it."""
    db_path = tmp_path / "shared.db"
    first = _intraday_pipeline(universe=["AAPL"], db_path=db_path)
    second = _intraday_pipeline(universe=["AAPL"], db_path=db_path)

    with first._intraday_scan_process_lock() as acquired:
        assert acquired is True
    # First released on context exit; the next tick gets it.
    with second._intraday_scan_process_lock() as acquired_next:
        assert acquired_next is True


def test_process_lock_propagates_scan_exception_and_releases(tmp_path):
    """The lock manager owns acquisition only; it must never swallow or
    replace an exception raised by the protected scan body."""
    db_path = tmp_path / "shared.db"
    first = _intraday_pipeline(universe=["AAPL"], db_path=db_path)
    second = _intraday_pipeline(universe=["AAPL"], db_path=db_path)
    suspended = PaidAnalysisSuspended("prelatched", {"suspended": True})

    with pytest.raises(PaidAnalysisSuspended) as caught:
        with first._intraday_scan_process_lock() as acquired:
            assert acquired is True
            raise suspended

    assert caught.value is suspended
    with second._intraday_scan_process_lock() as acquired_next:
        assert acquired_next is True


@patch("src.pipeline_intraday.compute_indicators")
def test_prelatched_real_intraday_scan_reports_suspension_before_agent_call(
    mock_compute_indicators,
    tmp_path,
):
    """Regression for the 2026-08-25 production tick: a suspension thrown
    inside the real process-lock wrapper must reach run_intra_check's specific
    handler and remain visible in the structured result."""
    mock_compute_indicators.return_value = MagicMock()
    p = _intraday_pipeline(universe=["AAPL"], db_path=tmp_path / "shared.db")
    p._is_trading_day = MagicMock(return_value=True)
    p._drain_pending_protection_restores = MagicMock()
    p._reconcile_stop_coverage = MagicMock(return_value=[])
    p._reconcile_orphan_pending_submits = MagicMock()
    p._await_paid_scan_slot = MagicMock(return_value=False)
    p.risk_engine = MagicMock()
    p.broker.get_account.return_value = {
        "cash": 10_000.0,
        "portfolio_value": 10_100.0,
        "last_equity": 10_000.0,
        "non_marginable_buying_power": 10_000.0,
    }
    p.broker.get_positions.return_value = []
    p.broker.get_intraday_snapshots.return_value = {
        "AAPL": _snapshot(last=110.0, prev=100.0),
    }
    p.cost_circuit = MagicMock()
    p.cost_circuit.activate_session.return_value = {"suspended": True}
    p.cost_circuit.require_paid_analysis.side_effect = PaidAnalysisSuspended(
        "prelatched",
        {"suspended": True},
    )

    result = p.run_intra_check()

    assert result["status"] == "ok"
    assert result["intraday_scan"]["status"] == "paid_analysis_suspended"
    assert result["intraday_scan"]["suspended"] == "intraday opportunity discovery only"
    p.tech_analyst.analyze_batch.assert_not_called()
    p.cost_circuit.require_paid_analysis.assert_called_once_with(
        "intraday_tech_analyst",
    )


# ---------- Blocker 2: Tech receives truthful CURRENT-SESSION evidence ----------
#
# The scan detects candidates on live current-session prices. Before this
# fix Tech was then handed only COMPLETED daily bars ending at yesterday's
# close — so the very move that triggered the scan was invisible to the
# analyst asked to judge it. Tech now also receives an explicitly-labelled
# INCOMPLETE current-session block. An incomplete day is never presented
# as a finished daily bar.


def _session_snapshot(last, prev, o=None, h=None, lo=None, v=None, *, trade_at="today", bar_at="today"):
    """Today's snapshot for a name that has genuinely traded today.

    board item 120: `session_bar_at` dates the `session_*` block, which
    Alpaca fills with the PREVIOUS session's daily bar for a name that has
    not printed. Defaults stamp both as today; pass `bar_at=None` to model
    a name whose session bar is a prior session's.

    The trade stamp lands inside today's regular session, not at `et_now()`:
    the resolver's point-in-time test is date-equality AND at/after 09:30 ET,
    so an `et_now()` default was silently not-today for the fourteen hours a
    day outside the session. The BAR stamp is 00:00 ET and always was —
    a daily bar is dated, not stamped. See `tests/session_clock.py`.
    """
    from tests.session_clock import todays_session_snapshot_stamps

    # ONE clock read for both, so a run crossing ET midnight cannot date the
    # trade and the bar to different sessions.
    today_trade_at, today_bar_at = todays_session_snapshot_stamps()
    if trade_at == "today":
        trade_at = today_trade_at
    if bar_at == "today":
        bar_at = today_bar_at
    return {
        "last_price": last,
        "prev_close": prev,
        "last_trade_at": trade_at,
        "session_bar_at": bar_at,
        "session_open": o,
        "session_high": h,
        "session_low": lo,
        "session_volume": v,
    }


@patch("src.pipeline_intraday.compute_indicators")
def test_todays_move_is_passed_to_tech_as_current_session_context(mock_compute_indicators):
    """The live figures the scan triggered on must reach tech_analyst."""
    mock_compute_indicators.return_value = MagicMock()
    p = _intraday_pipeline(universe=["AAPL"])
    snap = _session_snapshot(last=110.0, prev=100.0, o=101.0, h=111.0, lo=100.5, v=9_100_000)
    p.broker.get_intraday_snapshots.return_value = {"AAPL": snap}
    p.tech_analyst.analyze_batch.return_value = ({}, None)

    p._run_intraday_opportunity_scan(RunContext.start("intra_check"))

    ctx_arg = p.tech_analyst.analyze_batch.call_args.kwargs["intraday_context"]
    assert ctx_arg["AAPL"]["live_price"] == 110.0
    assert ctx_arg["AAPL"]["prev_close"] == 100.0
    assert ctx_arg["AAPL"]["session_volume"] == 9_100_000


def test_tech_prompt_renders_todays_move_without_faking_a_daily_bar():
    """The rendered prompt must show today's price and % move, label the
    session INCOMPLETE, and keep it out of the completed-bar series."""
    from datetime import date
    from src.agents.tech_analyst import TechAnalystAgent
    from src.models import OHLCV, TechnicalIndicators
    from src.pipeline import TradingPipeline

    bars = [OHLCV(date=date(2026, 8, 18), open=99.0, high=101.0, low=98.0, close=100.0, volume=5_000_000)]
    indicators = TechnicalIndicators(
        symbol="AAPL",
        ma_20=99.0,
        ma_50=98.0,
        ma_200=95.0,
        rsi_14=55.0,
        macd=0.5,
        macd_signal=0.4,
        macd_hist=0.1,
        bb_upper=104.0,
        bb_middle=100.0,
        bb_lower=96.0,
        atr_14=2.0,
        volume_change_pct=5.0,
    )
    with patch("anthropic.Anthropic"):
        agent = TechAnalystAgent(api_key="t", model="claude-sonnet-4-6-20250514")
        msg = agent.build_user_message(
            symbols_data=[{"symbol": "AAPL", "bars": bars, "indicators": indicators}],
            # Resolved exactly as the scan resolves it (item 120) — the
            # prompt is never handed a raw, unchecked snapshot.
            intraday_context=TradingPipeline._resolve_live_context(
                {
                    "AAPL": _session_snapshot(
                        last=110.0,
                        prev=100.0,
                        o=101.0,
                        h=111.0,
                        lo=100.5,
                        v=9_100_000,
                    )
                },
                ["AAPL"],
            )[0],
        )

    assert "CURRENT SESSION" in msg and "INCOMPLETE" in msg
    assert "110.00" in msg  # today's live price
    assert "+10.00%" in msg  # move vs prior close
    # 100.0 now renders as "100" — see _px in src/agents/tech_analyst.py.
    # Same number, fewer tokens; assert the value, not its spelling.
    assert "Last completed close: 100\n" in msg or "Last completed close: 100" in msg
    # The completed-bar series must still contain ONLY the finished day.
    completed = msg.split("CURRENT SESSION")[0]
    assert "2026-08-18" in completed
    assert "110.0" not in completed, "today's live price must not appear as a daily bar"
    # And Tech must be told the indicators predate the move.
    assert "do NOT yet reflect this move" in msg


def test_tech_prompt_omits_session_block_when_no_intraday_context():
    """Morning runs pass no intraday context — the prompt must be unchanged
    for them (no empty/misleading session block)."""
    from datetime import date
    from src.agents.tech_analyst import TechAnalystAgent
    from src.models import OHLCV, TechnicalIndicators

    bars = [OHLCV(date=date(2026, 8, 18), open=99.0, high=101.0, low=98.0, close=100.0, volume=5_000_000)]
    indicators = TechnicalIndicators(
        symbol="AAPL",
        ma_20=99.0,
        ma_50=98.0,
        ma_200=95.0,
        rsi_14=55.0,
        macd=0.5,
        macd_signal=0.4,
        macd_hist=0.1,
        bb_upper=104.0,
        bb_middle=100.0,
        bb_lower=96.0,
        atr_14=2.0,
        volume_change_pct=5.0,
    )
    with patch("anthropic.Anthropic"):
        agent = TechAnalystAgent(api_key="t", model="claude-sonnet-4-6-20250514")
        msg = agent.build_user_message(
            symbols_data=[{"symbol": "AAPL", "bars": bars, "indicators": indicators}],
        )
    assert "CURRENT SESSION" not in msg


@patch("src.pipeline_intraday.compute_indicators")
def test_todays_move_propagates_through_the_full_decision_chain(mock_compute_indicators):
    """End-to-end for Blocker 2: a move that develops TODAY is detected on
    live prices, handed to Tech WITH that live evidence, and flows through
    Tech -> PM -> Risk -> deterministic gate -> execution."""
    mock_compute_indicators.return_value = MagicMock()
    p = _intraday_pipeline(universe=["AAPL"])
    p.broker.get_intraday_snapshots.return_value = {
        "AAPL": _session_snapshot(last=110.0, prev=100.0, o=101.0, h=111.0, lo=100.5, v=9_100_000),
    }
    analysis = _ta_result("AAPL", rating="buy")
    p.tech_analyst.analyze_batch.return_value = (
        {"AAPL": analysis},
        MagicMock(
            user_message="m", raw_text="{}", tokens_used=1, input_tokens=1, output_tokens=1, cost_usd=0.0, model="t"
        ),
    )
    p.decision_stage.run.side_effect = lambda ctx: setattr(
        ctx,
        "portfolio_decision",
        SimpleNamespace(decisions=[SimpleNamespace(action="BUY", symbol="AAPL")]),
    )
    p.risk_stage.run.return_value = None
    p.execution_stage.run.return_value = [{"id": "o1", "action": "BUY", "symbol": "AAPL"}]

    ctx = RunContext.start("intra_check")
    result = p._run_intraday_opportunity_scan(ctx)

    # Tech saw today's live move...
    tech_ctx = p.tech_analyst.analyze_batch.call_args.kwargs["intraday_context"]["AAPL"]
    assert tech_ctx["live_price"] == 110.0
    # item 120: the RAW provider field is not republished beside the
    # resolved one — what no consumer can reach, no consumer can misread.
    assert "last_price" not in tech_ctx
    # ...and it reached execution through the shared chain.
    assert ctx.analyses == [analysis]
    p.decision_stage.run.assert_called_once_with(ctx)
    p.risk_stage.run.assert_called_once_with(ctx)
    p.execution_stage.run.assert_called_once_with(ctx)
    assert result["status"] == "intraday_executed"
    lifecycle = [
        call.kwargs
        for call in p.db.insert_specialist_evidence.call_args_list
        if call.kwargs.get("kind") == "pipeline_event"
    ]
    assert any('"stage": "opportunity"' in row["evidence_json"] for row in lifecycle)
    assert any('"stage": "specialist"' in row["evidence_json"] for row in lifecycle)


# ---------- symbol snapshot-health tracking / owner alert (2026-09-10) ----


def test_a_symbol_missing_snapshot_data_is_tracked_but_not_alerted_once():
    """A single miss must not page the owner — see
    `Database.record_intraday_symbol_snapshot_result`'s threshold reasoning."""
    from src.storage.db import Database

    p = _intraday_pipeline(universe=["SPY", "BADTIX"], atr={})
    real_db = Database(str(Path(tempfile.mkdtemp()) / "t.db"))
    real_db.initialize()
    p.db = real_db
    p.db.get_trades = lambda *a, **k: []
    p.broker.get_intraday_snapshots.return_value = {
        "SPY": _snapshot(last=500.0, prev=499.0),
        # BADTIX: no entry at all -> falls back to {} -> not (last, prev) numeric
    }
    with patch("src.notifier.send_owner_alert") as mock_alert:
        ctx = RunContext.start("intra_check")
        p._run_intraday_opportunity_scan(ctx)
        mock_alert.assert_not_called()
    row = real_db.conn.execute("SELECT consecutive_misses FROM intraday_symbol_health WHERE symbol='BADTIX'").fetchone()
    assert row["consecutive_misses"] == 1
    real_db.close()


def test_three_consecutive_missing_snapshots_pages_the_owner_once():
    """Three ticks in a row with no snapshot data for the same symbol must
    fire exactly one owner alert (2026-09-10 fix for the residual BRK-B-
    shaped gap: a persistently broken symbol used to fail silently
    forever)."""
    from src.storage.db import Database

    # BADTIX absent from every reply while SPY is present and healthy —
    # the real shape of `get_intraday_snapshots` when one symbol in an
    # otherwise-working universe keeps failing (see that function: it
    # returns an entry for every requested symbol as long as AT LEAST ONE
    # batch succeeds; only a TOTAL fetch failure returns {} outright, which
    # is the separate, already-handled "intraday_scan_no_opportunity" path
    # and out of scope for this fix).
    p = _intraday_pipeline(universe=["SPY", "BADTIX"], atr={})
    real_db = Database(str(Path(tempfile.mkdtemp()) / "t.db"))
    real_db.initialize()
    p.db = real_db
    p.db.get_trades = lambda *a, **k: []
    p.broker.get_intraday_snapshots.return_value = {
        "SPY": _snapshot(last=500.0, prev=499.0),
    }

    with patch("src.notifier.send_owner_alert") as mock_alert:
        for _ in range(3):
            ctx = RunContext.start("intra_check")
            p._run_intraday_opportunity_scan(ctx)
        assert mock_alert.call_count == 1
        assert "BADTIX" in mock_alert.call_args.args[0]
        assert "3 consecutive" in mock_alert.call_args.args[0]
    real_db.close()


def test_a_recovered_symbol_resets_its_miss_streak():
    """A symbol that comes back healthy must not carry its miss count into
    a future, unrelated outage."""
    from src.storage.db import Database

    p = _intraday_pipeline(universe=["SPY", "BADTIX"], atr={})
    real_db = Database(str(Path(tempfile.mkdtemp()) / "t.db"))
    real_db.initialize()
    p.db = real_db
    p.db.get_trades = lambda *a, **k: []

    p.broker.get_intraday_snapshots.return_value = {
        "SPY": _snapshot(last=500.0, prev=499.0),
    }
    for _ in range(2):
        ctx = RunContext.start("intra_check")
        p._run_intraday_opportunity_scan(ctx)

    p.broker.get_intraday_snapshots.return_value = {
        "SPY": _snapshot(last=500.0, prev=499.0),
        "BADTIX": _snapshot(last=50.0, prev=49.0),
    }
    ctx = RunContext.start("intra_check")
    p._run_intraday_opportunity_scan(ctx)

    row = real_db.conn.execute("SELECT consecutive_misses FROM intraday_symbol_health WHERE symbol='BADTIX'").fetchone()
    assert row["consecutive_misses"] == 0
    real_db.close()


# ---------- item 20: empty/failed carry-forward refuses; intentional skip does not


def _qualifying_move_pipeline():
    """A scan that has found a usable tech analysis and would otherwise
    pay for the Portfolio Manager — the moment the evidence gate must
    run, before that call."""
    p = _intraday_pipeline(universe=["AAPL"])
    p.broker.get_intraday_snapshots.return_value = {
        "AAPL": _snapshot(last=110.0, prev=100.0),
    }
    analysis = _ta_result("AAPL", rating="buy")
    p.tech_analyst.analyze_batch.return_value = (
        {"AAPL": analysis},
        MagicMock(
            user_message="m", raw_text="{}", tokens_used=1, input_tokens=1, output_tokens=1, cost_usd=0.0, model="t"
        ),
    )
    p.decision_stage.run.side_effect = lambda ctx: setattr(
        ctx,
        "portfolio_decision",
        SimpleNamespace(decisions=[SimpleNamespace(action="BUY", symbol="AAPL")]),
    )
    p.risk_stage.run.return_value = None
    p.execution_stage.run.return_value = [{"id": "o1", "action": "BUY", "symbol": "AAPL"}]
    return p


@patch("src.pipeline_intraday.compute_indicators")
@patch("src.notifier.send_owner_alert", return_value=True)
def test_empty_morning_carry_forward_is_advisory_and_is_disclosed(
    mock_alert,
    mock_compute_indicators,
):
    """This morning's macro/news never arrived. The SPLIT STATUS is
    unchanged — `carry_forward_empty` is still a lost answer, never the
    intentional skip — but which lost seat stops the desk is now an owner
    mandate (2026-09-18, "only technical analysis can stop the desk").
    Macro and news are advisory, so the scan decides, and the decision
    discloses that those two seats contributed nothing."""
    mock_compute_indicators.return_value = MagicMock()
    p = _qualifying_move_pipeline()
    p.macro_store.load_last_state.return_value = None
    p.news_store.load_daily_report.return_value = None

    ctx = RunContext.start("intra_check")
    with patch("src.decision_checkpoint.write_status") as write_status:
        result = p._run_intraday_opportunity_scan(ctx)

    assert result["status"] != "evidence_gate_skip"
    assert ctx.data_status["macro"] == "carry_forward_empty"
    assert ctx.data_status["news"] == "carry_forward_empty"
    # A real earnings provider with nothing cached reports "chose_not_to_refetch" (was "not_run_intraday").
    assert ctx.data_status["earnings"] == "chose_not_to_refetch"
    p.decision_stage.run.assert_called()
    write_status.assert_not_called()
    # The loss is not silent: it is in the decision's own durable freshness
    # record, and `notifier.maybe_alert_data_quality` still pages off the
    # seat states the wrapper attaches to the result.
    assert set(ctx.evidence_freshness["absent_seats"]) == {"macro", "news"}
    assert ctx.evidence_freshness["fresh_seats"] == ["tech"]


@patch("src.pipeline_intraday.compute_indicators")
@patch("src.notifier.send_owner_alert", return_value=True)
def test_failed_morning_carry_forward_skips_the_intraday_pm(
    mock_alert,
    mock_compute_indicators,
):
    """The lookup itself raised. That is `carry_forward_failed`, not the
    intentional skip, and it refuses the same way a morning `failed` does."""
    mock_compute_indicators.return_value = MagicMock()
    p = _qualifying_move_pipeline()
    p.macro_store.load_last_state.side_effect = RuntimeError("disk gone")
    p.news_store.load_daily_report.side_effect = RuntimeError("disk gone")

    ctx = RunContext.start("intra_check")
    result = p._run_intraday_opportunity_scan(ctx)

    assert ctx.data_status["macro"] == "carry_forward_failed"
    assert ctx.data_status["news"] == "carry_forward_failed"
    # Still lost, still recorded, still disclosed — but advisory, so the
    # desk decides (owner mandate 2026-09-18).
    assert result["status"] != "evidence_gate_skip"
    assert set(ctx.evidence_freshness["absent_seats"]) == {"macro", "news"}


@patch("src.pipeline_intraday.compute_indicators")
@patch("src.notifier.send_owner_alert", return_value=True)
def test_a_lost_news_seat_is_advisory_and_the_carried_book_is_disclosed(
    mock_alert,
    mock_compute_indicators,
):
    """The thin-decision shape the mandate change creates, end to end: news
    lost, macro carried from the morning, one chart read done just now. The
    desk decides — and says so."""
    mock_compute_indicators.return_value = MagicMock()
    p = _qualifying_move_pipeline()
    p.news_store.load_daily_report.return_value = None

    ctx = RunContext.start("intra_check")
    result = p._run_intraday_opportunity_scan(ctx)

    assert result["status"] != "evidence_gate_skip"
    assert ctx.data_status["macro"] == "carried_from_morning"
    assert ctx.data_status["news"] == "carry_forward_empty"
    p.decision_stage.run.assert_called()
    freshness = ctx.evidence_freshness
    assert freshness["fresh_seats"] == ["tech"]
    assert "macro" in freshness["carried_seats"]
    assert freshness["absent_seats"] == ["news"]
    assert freshness["seats_read_this_tick"] == 1
    from src.notifier import describe_evidence_freshness

    words = " ".join(describe_evidence_freshness(freshness))
    assert "1 of 5 research seats read just now" in words
    assert "the market-backdrop research" in words


# ---------- item 20 (board): a fully lost intraday tech seat is LOST, not quiet ----------


@patch("src.pipeline_intraday.compute_indicators")
@patch("src.notifier.send_owner_alert", return_value=True)
def test_intraday_total_tech_failure_is_lost_and_skips_the_pm(
    mock_alert,
    mock_compute_indicators,
):
    """Every submitted symbol failed to resolve even after tech_analyst's own
    retry. This used to fall into `intraday_scan_no_opportunity` — the same
    status a genuinely empty candidate set gets — so a real outage was
    invisible next to an ordinary quiet tick. It must classify as the same
    `data_status["tech"]="failed"` LOST category morning uses, route through
    the shared evidence gate, and skip the Portfolio Manager (tech is the
    one blocking seat, owner mandate 2026-09-18)."""
    mock_compute_indicators.return_value = MagicMock()
    p = _intraday_pipeline(universe=["AAPL"])
    p.broker.get_intraday_snapshots.return_value = {
        "AAPL": _snapshot(last=110.0, prev=100.0),
    }
    # Every candidate symbol resolves to None: tech_analyst tried and lost
    # every one of them, not "there was nothing to submit".
    p.tech_analyst.analyze_batch.return_value = (
        {"AAPL": None},
        MagicMock(
            user_message="m", raw_text="{}", tokens_used=1, input_tokens=1, output_tokens=1, cost_usd=0.0, model="t"
        ),
    )

    ctx = RunContext.start("intra_check")
    result = p._run_intraday_opportunity_scan(ctx)

    assert ctx.data_status["tech"] == "failed"
    assert result["status"] == "evidence_gate_skip"
    assert "tech" in result["blocking_lost_seats"]
    p.decision_stage.run.assert_not_called()
    from src import evidence_gate

    assert evidence_gate.counts_as_degraded(ctx.data_status["tech"]) is True
    from src.notifier import maybe_alert_data_quality

    assert maybe_alert_data_quality(result, mode="intra_check") is True


@patch("src.pipeline_intraday.compute_indicators")
@patch("src.notifier.send_owner_alert", return_value=True)
def test_intraday_tech_batch_raising_is_also_lost_not_a_crash(
    mock_alert,
    mock_compute_indicators,
):
    """The morning path wraps its tech_analyst call in try/except; the
    intraday one used to have no guard at all, so a batch-level raise
    (provider outage, unparseable response) crashed the whole tick instead
    of being recorded as a LOST tech seat like every other failure mode
    this scan handles."""
    mock_compute_indicators.return_value = MagicMock()
    p = _intraday_pipeline(universe=["AAPL"])
    p.broker.get_intraday_snapshots.return_value = {
        "AAPL": _snapshot(last=110.0, prev=100.0),
    }
    p.tech_analyst.analyze_batch.side_effect = RuntimeError("provider down")

    ctx = RunContext.start("intra_check")
    result = p._run_intraday_opportunity_scan(ctx)

    assert ctx.data_status["tech"] == "failed"
    assert result["status"] == "evidence_gate_skip"
    assert "tech" in result["blocking_lost_seats"]
    p.decision_stage.run.assert_not_called()


@patch("src.pipeline_intraday.compute_indicators")
def test_intraday_partial_tech_failure_still_trades(mock_compute_indicators):
    """One symbol out of two fails to resolve. This must stay REPORTED
    (`partial`) and keep trading on the resolved subset — item 20 must not
    start blocking ordinary intraday activity on a single bad symbol, only
    on a seat that is genuinely and entirely lost."""
    mock_compute_indicators.return_value = MagicMock()
    p = _intraday_pipeline(universe=["AAPL", "MSFT"], atr={"AAPL": 2.0})
    p.broker.get_intraday_snapshots.return_value = {
        "AAPL": _snapshot(last=110.0, prev=100.0),
        "MSFT": _snapshot(last=330.0, prev=300.0),
    }
    analysis = _ta_result("AAPL", rating="buy")
    p.tech_analyst.analyze_batch.return_value = (
        {"AAPL": analysis, "MSFT": None},
        MagicMock(
            user_message="m", raw_text="{}", tokens_used=1, input_tokens=1, output_tokens=1, cost_usd=0.0, model="t"
        ),
    )
    p.decision_stage.run.side_effect = lambda ctx: setattr(
        ctx,
        "portfolio_decision",
        SimpleNamespace(decisions=[SimpleNamespace(action="BUY", symbol="AAPL")]),
    )
    p.risk_stage.run.return_value = None
    p.execution_stage.run.return_value = [{"id": "o1", "action": "BUY", "symbol": "AAPL"}]

    ctx = RunContext.start("intra_check")
    result = p._run_intraday_opportunity_scan(ctx)

    assert ctx.data_status["tech"] == "partial"
    assert result["status"] != "evidence_gate_skip"
    p.decision_stage.run.assert_called_once_with(ctx)
    assert ctx.analyses == [analysis]


@patch("src.pipeline_intraday.compute_indicators")
def test_intraday_genuinely_empty_candidate_set_still_no_opportunity(
    mock_compute_indicators,
):
    """A tick with no qualifying move never calls tech_analyst at all — that
    stays `intraday_scan_no_opportunity`, distinct from a seat that was
    asked and lost."""
    mock_compute_indicators.return_value = MagicMock()
    p = _intraday_pipeline(universe=["SPY", "AAPL"], atr={})
    p.broker.get_intraday_snapshots.return_value = {
        "SPY": _snapshot(last=500.0, prev=499.0),  # no ATR — no_atr
        "AAPL": _snapshot(last=100.5, prev=100.0),  # no ATR — no_atr
    }

    ctx = RunContext.start("intra_check")
    result = p._run_intraday_opportunity_scan(ctx)

    assert result == {"status": "intraday_scan_no_opportunity", "run_id": ctx.run_id}
    p.tech_analyst.analyze_batch.assert_not_called()
    assert "tech" not in (ctx.data_status or {})


# === Board item 177, the TRIGGER third ================================
#
# `move_threshold_pct` is a flat 3% applied to a 1%/day utility and an
# 8%/day small cap alike, and the number ledger's open question against it
# asks what move marks a development worth re-reading RELATIVE TO THE
# NAME'S OWN ATR. Measured 2026-09-26 against the desk's own
# `intraday_evaluations` table (253 selections, 2026-09-02..25): the flat
# threshold does not discriminate — median move of a selection that led to
# a BUY/SHORT was 3.50% against 3.67% for one that led to nothing, and the
# 5-7% band produced zero orders from 51 selections. The ATR-relative form
# could not be tested at all, because the ledger stored `move_pct=` and
# never the denominator. These tests pin the recording of that denominator.
# NO THRESHOLD IS CHANGED by any of this.


def test_move_in_atr_is_the_move_over_the_names_own_daily_range():
    atr_pct, move_atr = TradingPipeline._intraday_move_in_atr(
        move_pct=6.0,
        atr_14=3.0,
        prev_close=100.0,
    )
    assert atr_pct == pytest.approx(3.0)
    assert move_atr == pytest.approx(2.0)


@pytest.mark.parametrize(
    "atr_14,prev_close",
    [
        (None, 100.0),
        (0.0, 100.0),
        (-1.0, 100.0),
        (3.0, None),
        (3.0, 0.0),
        (3.0, -5.0),
    ],
)
def test_move_in_atr_refuses_to_invent_a_denominator(atr_14, prev_close):
    """An unreadable ATR or prev_close must produce nothing, never a
    fabricated ratio — the measurement exists to answer a live money
    question and a made-up row would poison it."""
    assert TradingPipeline._intraday_move_in_atr(
        move_pct=6.0,
        atr_14=atr_14,
        prev_close=prev_close,
    ) == (None, None)


@patch("src.pipeline_intraday.compute_indicators")
def test_a_movers_ledger_row_records_the_atr_denominator(
    mock_compute_indicators,
):
    """The same 3% move is a third of a daily range on one name and two
    whole ranges on another. The row now carries both."""
    mock_compute_indicators.return_value = SimpleNamespace(atr_14=2.0)
    p = _intraday_pipeline(universe=["AAPL"])
    p.broker.get_intraday_snapshots.return_value = {
        "AAPL": _snapshot(last=110.0, prev=100.0),  # 10% move, ATR 2% of close
    }
    p.tech_analyst.analyze_batch.return_value = (
        {"AAPL": _ta_result("AAPL", rating="buy")},
        MagicMock(
            user_message="m", raw_text="{}", tokens_used=1, input_tokens=1, output_tokens=1, cost_usd=0.0, model="t"
        ),
    )
    p.decision_stage.run.side_effect = lambda ctx: setattr(
        ctx,
        "portfolio_decision",
        SimpleNamespace(decisions=[]),
    )
    p.risk_stage.run.return_value = None
    p.execution_stage.run.return_value = []

    p._run_intraday_opportunity_scan(RunContext.start("intra_check"))

    details = [
        c.kwargs["detail"] for c in p.db.record_intraday_evaluation.call_args_list if c.kwargs["symbol"] == "AAPL"
    ]
    assert details, "the mover was never ledgered at all"
    assert "atr_pct=2.0000" in details[-1]
    assert "move_atr=5.0000" in details[-1]
    # The selection-time field every existing reader parses is untouched.
    assert details[-1].startswith("move_pct=10.0000")


@patch("src.pipeline_intraday.compute_indicators")
def test_the_atr_stamp_reuses_the_selection_row_and_never_adds_one(
    mock_compute_indicators,
):
    """`record_intraday_evaluation` upserts on (symbol, run_id), so the
    stamp updates the row the cooldown already counts. A second ROW would
    be a second cooldown fact for one paid look."""
    mock_compute_indicators.return_value = SimpleNamespace(atr_14=2.0)
    p = _intraday_pipeline(universe=["AAPL"])
    p.broker.get_intraday_snapshots.return_value = {
        "AAPL": _snapshot(last=110.0, prev=100.0),
    }
    p.tech_analyst.analyze_batch.return_value = (
        {"AAPL": _ta_result("AAPL", rating="buy")},
        MagicMock(
            user_message="m", raw_text="{}", tokens_used=1, input_tokens=1, output_tokens=1, cost_usd=0.0, model="t"
        ),
    )
    p.decision_stage.run.side_effect = lambda ctx: setattr(
        ctx,
        "portfolio_decision",
        SimpleNamespace(decisions=[]),
    )
    p.risk_stage.run.return_value = None
    p.execution_stage.run.return_value = []

    ctx = RunContext.start("intra_check")
    p._run_intraday_opportunity_scan(ctx)

    keys = {
        (c.kwargs["symbol"], c.kwargs["run_id"], c.kwargs["status"])
        for c in p.db.record_intraday_evaluation.call_args_list
    }
    assert keys == {("AAPL", ctx.run_id, "selected")}


@patch("src.pipeline_intraday.compute_indicators")
def test_a_held_name_gets_no_trigger_measurement(mock_compute_indicators):
    """Held-book coverage never passed through `move_threshold_pct`, so
    stamping it would contaminate the very measurement meant to answer for
    that trigger — and would give a quiet hold a mover cooldown."""
    mock_compute_indicators.return_value = SimpleNamespace(atr_14=2.0)
    p = _intraday_pipeline(universe=["AAPL", "MSFT"], atr={"AAPL": 2.0})
    p.broker.get_intraday_snapshots.return_value = {
        "AAPL": _snapshot(last=110.0, prev=100.0),  # mover
        "MSFT": _snapshot(last=100.2, prev=100.0),  # quiet hold
    }
    p.tech_analyst.analyze_batch.return_value = (
        {"AAPL": _ta_result("AAPL", rating="buy"), "MSFT": _ta_result("MSFT", rating="buy")},
        MagicMock(
            user_message="m", raw_text="{}", tokens_used=1, input_tokens=1, output_tokens=1, cost_usd=0.0, model="t"
        ),
    )
    p.decision_stage.run.side_effect = lambda ctx: setattr(
        ctx,
        "portfolio_decision",
        SimpleNamespace(decisions=[]),
    )
    p.risk_stage.run.return_value = None
    p.execution_stage.run.return_value = []

    ctx = RunContext.start("intra_check")
    ctx.positions = [_held_position("MSFT")]
    p._run_intraday_opportunity_scan(ctx)

    ledgered = {c.kwargs["symbol"] for c in p.db.record_intraday_evaluation.call_args_list}
    assert ledgered == {"AAPL"}


@patch("src.pipeline_intraday.compute_indicators")
def test_an_unreadable_atr_is_recorded_as_unreadable_not_dropped(
    mock_compute_indicators,
):
    """The true state, reported: a name whose ATR could not be read must
    say so on its row, so the eventual measurement knows its own coverage
    instead of quietly counting a smaller sample."""
    mock_compute_indicators.return_value = SimpleNamespace(atr_14=None)
    p = _intraday_pipeline(universe=["AAPL"])
    p.broker.get_intraday_snapshots.return_value = {
        "AAPL": _snapshot(last=110.0, prev=100.0),
    }
    p.tech_analyst.analyze_batch.return_value = (
        {"AAPL": _ta_result("AAPL", rating="buy")},
        MagicMock(
            user_message="m", raw_text="{}", tokens_used=1, input_tokens=1, output_tokens=1, cost_usd=0.0, model="t"
        ),
    )
    p.decision_stage.run.side_effect = lambda ctx: setattr(
        ctx,
        "portfolio_decision",
        SimpleNamespace(decisions=[]),
    )
    p.risk_stage.run.return_value = None
    p.execution_stage.run.return_value = []

    p._run_intraday_opportunity_scan(RunContext.start("intra_check"))

    detail = [
        c.kwargs["detail"] for c in p.db.record_intraday_evaluation.call_args_list if c.kwargs["symbol"] == "AAPL"
    ][-1]
    assert "atr_pct=unreadable" in detail
    assert "move_atr=unreadable" in detail


@patch("src.pipeline_intraday.compute_indicators")
def test_a_ledger_write_failure_on_the_stamp_never_breaks_the_scan(
    mock_compute_indicators,
):
    """A measurement must not cost a trade. If the stamp write raises, the
    scan completes exactly as it would have without it."""
    mock_compute_indicators.return_value = SimpleNamespace(atr_14=2.0)
    p = _intraday_pipeline(universe=["AAPL"])
    p.broker.get_intraday_snapshots.return_value = {
        "AAPL": _snapshot(last=110.0, prev=100.0),
    }
    calls = {"n": 0}

    def _flaky(**kwargs):
        calls["n"] += 1
        if calls["n"] > 1:  # the selection write succeeds, the stamp fails
            raise RuntimeError("disk full")

    p.db.record_intraday_evaluation.side_effect = _flaky
    p.tech_analyst.analyze_batch.return_value = (
        {"AAPL": _ta_result("AAPL", rating="buy")},
        MagicMock(
            user_message="m", raw_text="{}", tokens_used=1, input_tokens=1, output_tokens=1, cost_usd=0.0, model="t"
        ),
    )
    p.decision_stage.run.side_effect = lambda ctx: setattr(
        ctx,
        "portfolio_decision",
        SimpleNamespace(decisions=[SimpleNamespace(action="BUY", symbol="AAPL")]),
    )
    p.risk_stage.run.return_value = None
    p.execution_stage.run.return_value = [
        {"id": "o1", "action": "BUY", "symbol": "AAPL"},
    ]

    result = p._run_intraday_opportunity_scan(RunContext.start("intra_check"))

    assert result["status"] == "intraday_executed"
    assert calls["n"] == 2
