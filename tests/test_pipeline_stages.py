"""Pipeline stages — Phase 4 #1 infrastructure.

These tests cover the stage pattern itself: stages take explicit
dependencies at construction, accept a RunContext, populate ctx fields,
and return the context. Exhaustive integration testing of the
MorningResearchStage's parallel fan-out is covered indirectly by the
existing pipeline integration tests in test_pipeline.py.
"""

from tests.fakes.held_book import hold
import json
from unittest.mock import MagicMock, patch

from src.data.news import FeedFailure, NewsCoverage
from src.pipeline_context import RunContext
from src.pipeline_stages import (
    DecisionStage,
    ExecutionStage,
    MorningResearchStage,
    RiskStage,
)
from tests.pipeline_factory import build_pipeline


def _mock_stop_seam(broker, *, specs=(), snapshot_ok=True, cancel_ok=True):
    """Wire a MagicMock broker's split stop-cancel seam (audit F1 #1):
    snapshot_protective_stops (read) + cancel_snapshotted_stops (mutate),
    plus the composed cancel_protective_stops for any direct caller."""
    specs = list(specs)
    broker.snapshot_protective_stops.return_value = (snapshot_ok, specs)
    broker.cancel_snapshotted_stops.return_value = MagicMock(cleared=cancel_ok, coverage_shrank=False)
    cleared = snapshot_ok and cancel_ok
    broker.cancel_protective_stops.return_value = (
        cleared,
        specs if cleared else [],
    )


def _mock_stage_seam(pipeline, *, specs=(), ok=True, wal_row_id=None):
    """Full-MagicMock-pipeline tests: ExecutionStage obtains stops via
    pipeline._cancel_stops_with_write_ahead (audit F1 #1); stub its
    3-tuple directly (the broker seam is never reached on a mock)."""
    pipeline._cancel_stops_with_write_ahead.return_value = (
        ok,
        list(specs),
        wal_row_id,
    )
    # Callers unpack finalize's (ok, retry_specs) contract; default to
    # "coverage confirmed" so the full-MagicMock pipeline yields a tuple.
    pipeline._finalize_protection_after_sell.return_value = (True, [])
    # Bind the REAL protected-sell helpers onto the mock so the extracted
    # cancel→submit→accept→restore + wait→finalize discipline actually runs
    # against the mocked broker/seams (real integration, not a no-op mock).
    import types as _types
    from src.pipeline import TradingPipeline as _TP

    pipeline._submit_protected_sell = _types.MethodType(
        _TP._submit_protected_sell,
        pipeline,
    )
    pipeline._finalize_pending_protections = _types.MethodType(
        _TP._finalize_pending_protections,
        pipeline,
    )


def _carried_forward_macro_dict(target_invested_pct=55.0):
    """Old MacroStore trim: dict sector_guidance, no reasoning_chain.

    That shape must not be smuggled into PM. Tests that mean today's
    persisted live snapshot use `_valid_carried_macro_dict`.
    """
    return {
        "date": "2026-08-26",
        "regime": "risk-on",
        "confidence": "high",
        "equity_outlook": "bullish",
        "summary": "carried forward from yesterday",
        "position_guidance": {
            "target_invested_pct": target_invested_pct,
            "cash_recommendation_pct": 100.0 - target_invested_pct,
            "reasoning": "x",
        },
        "sector_guidance": {"Technology": "bullish"},
    }


def _valid_carried_macro_dict(target_invested_pct=55.0):
    """Same-day MacroStore snapshot that still validates as MacroAnalysis."""
    from src.models import MacroAnalysis, MacroPositionGuidance, MacroReasoningChain

    return {
        "date": "2026-08-26",
        **MacroAnalysis(
            regime="risk-on",
            confidence="high",
            equity_outlook="bullish",
            summary="carried forward from yesterday",
            position_guidance=MacroPositionGuidance(
                target_invested_pct=target_invested_pct,
                cash_recommendation_pct=100.0 - target_invested_pct,
                reasoning="x",
            ),
            sector_guidance=[],
            reasoning_chain=MacroReasoningChain(
                volatility_analysis="x",
                yield_curve_analysis="x",
                monetary_policy_analysis="x",
                inflation_labor_credit="x",
                cross_signal_synthesis="x",
                sector_implications="x",
            ),
        ).model_dump(),
        "sector_guidance": {"Technology": "bullish"},
    }


def test_persist_evidence_never_raises_on_db_failure():
    """Stage 4: a specialist-evidence write failure must never propagate
    into the research/decision/risk flow it's forensically recording — it's
    additive and non-authoritative (docs/architecture/MISSION_CONTROL_API.md,
    .claude/rules/trading-core.md)."""
    from src.pipeline_stages import _persist_evidence

    db = MagicMock()
    db.insert_specialist_evidence.side_effect = RuntimeError("disk full")

    _persist_evidence(
        db,
        run_id="run-1",
        agent_name="macro_analyst",
        kind="analysis",
        scope="run",
        evidence_json="{}",
    )
    # No exception raised — that's the assertion. The call was still
    # attempted (not silently skipped).
    db.insert_specialist_evidence.assert_called_once()


def test_stage_classes_take_pipeline_reference():
    """DecisionStage / RiskStage / ExecutionStage wire a pipeline for helpers."""
    fake_pipeline = MagicMock()
    for cls in (DecisionStage, RiskStage, ExecutionStage):
        stage = cls(pipeline=fake_pipeline)
        assert stage._pipeline is fake_pipeline


def _buy(symbol, alloc):
    from src.models import TradeDecision

    return TradeDecision(
        action="BUY",
        symbol=symbol,
        allocation_pct=alloc,
        entry_price=100.0,
        stop_loss=95.0,
        take_profit=110.0,
        reasoning="x",
        thesis_invalid_if="closes below support",
    )


def _pm_rc():
    """Minimal valid PM reasoning_chain — every required step a non-empty
    string per PR #89 min_length=1 enforcement."""
    from src.models import ReasoningChain

    return ReasoningChain(
        macro_filter="x",
        news_check="x",
        earnings_check="x",
        signal_conflicts="x",
        sizing_logic="x",
        portfolio_balance="x",
        cash_target="x",
    )


def _risk_rc():
    """Minimal valid RM reasoning_chain — every required step a non-empty
    string per PR #89 min_length=1 enforcement."""
    from src.models import RiskReasoningChain

    return RiskReasoningChain(
        rr_audit="x",
        signal_fidelity="x",
        correlation_check="x",
        event_risk="x",
        sizing_sanity="x",
        overall="x",
    )


def _tech_rc():
    """Minimal valid Tech reasoning_chain."""
    from src.models import TechReasoningChain

    return TechReasoningChain(
        trend="x",
        momentum="x",
        volatility="x",
        volume="x",
        support_resistance="x",
    )


def _hold(symbol):
    from src.models import TradeDecision

    return TradeDecision(
        action="HOLD",
        symbol=symbol,
        allocation_pct=0.0,
        entry_price=100.0,
        stop_loss=95.0,
        take_profit=110.0,
        reasoning="hold",
    )


def _sell(symbol):
    from src.models import TradeDecision

    return TradeDecision(
        action="SELL",
        symbol=symbol,
        allocation_pct=100.0,
        entry_price=100.0,
        stop_loss=95.0,
        take_profit=110.0,
        reasoning="exit",
    )


def test_scale_advisory_zero_does_not_change_or_drop_any_entry():
    """Board items 134 + 162 (owner ruling 2026-09-25). scale_all_buys is
    ADVISORY on entries: even 0.0 — the old full-entry-side veto — now changes
    NO allocation_pct and drops NO trade. Every entry survives at its original
    size; the concern is recorded, not applied."""
    from src.models import RiskVerdict
    from src.pipeline_stages import _record_scale_advisory

    verdict = RiskVerdict(
        approved=True,
        scale_all_buys=0.0,
        reasoning_chain=_risk_rc(),
        reasoning="risk-off — exposure concern",
    )
    decisions = [_buy("SPY", 10), _buy("QQQ", 8), _hold("MSFT"), _sell("NVDA")]

    out, scale, advised = _record_scale_advisory(decisions, verdict)

    assert scale == 0.0, "0.0 must propagate, not collapse to 1.0"
    # Nothing dropped, nothing resized.
    assert [(d.symbol, d.action, d.allocation_pct) for d in out] == [
        ("SPY", "BUY", 10.0),
        ("QQQ", "BUY", 8.0),
        ("MSFT", "HOLD", 0.0),
        ("NVDA", "SELL", 100.0),
    ]
    # Both entries flagged for the advisory record; exits not flagged.
    assert sorted(advised) == [("QQQ", 8.0), ("SPY", 10.0)]


def test_scale_advisory_partial_records_entries_but_does_not_resize():
    """0 < scale < 1 flags every BUY/SHORT for the advisory record but leaves
    allocations untouched; HOLD/SELL are neither resized nor flagged."""
    from src.models import RiskVerdict
    from src.pipeline_stages import _record_scale_advisory

    verdict = RiskVerdict(
        approved=True,
        scale_all_buys=0.5,
        reasoning_chain=_risk_rc(),
        reasoning="trim",
    )
    decisions = [_buy("SPY", 10), _short("XLU", 8), _hold("MSFT")]

    out, scale, advised = _record_scale_advisory(decisions, verdict)

    assert scale == 0.5
    by_sym = {d.symbol: d for d in out}
    assert by_sym["SPY"].allocation_pct == 10.0
    assert by_sym["XLU"].allocation_pct == 8.0
    assert by_sym["MSFT"].action == "HOLD"
    assert sorted(advised) == [("SPY", 10.0), ("XLU", 8.0)]


def test_scale_advisory_one_is_no_op_and_flags_nothing():
    """scale=1.0 (default) leaves decisions untouched and flags nothing."""
    from src.models import RiskVerdict
    from src.pipeline_stages import _record_scale_advisory

    verdict = RiskVerdict(approved=True, scale_all_buys=1.0, reasoning_chain=_risk_rc(), reasoning="ok")
    decisions = [_buy("SPY", 10), _buy("QQQ", 8)]

    out, scale, advised = _record_scale_advisory(decisions, verdict)

    assert scale == 1.0
    assert advised == []
    assert [(d.symbol, d.allocation_pct) for d in out] == [
        ("SPY", 10.0),
        ("QQQ", 8.0),
    ]


def test_scale_advisory_handles_missing_attribute_as_one():
    """If a verdict somehow lacks scale_all_buys (legacy or partial parse),
    treat as 1.0 (no concern) — not as None propagating to a TypeError."""
    from src.pipeline_stages import _record_scale_advisory

    class LegacyVerdict:
        approved = True
        # no scale_all_buys attribute
        modifications = []

    decisions = [_buy("SPY", 10)]
    out, scale, advised = _record_scale_advisory(decisions, LegacyVerdict())

    assert scale == 1.0
    assert advised == []
    assert len(out) == 1 and out[0].allocation_pct == 10.0


def test_execution_stage_skips_buy_when_entry_price_more_than_5pct_off_market():
    """When LLM's entry_price deviates >5% from live market, the BUY must be
    skipped — not fallback-to-market. A stale entry implies the stop_loss
    (computed against that entry) is also stale, so the whole R/R math is
    unsafe. Better to wait for the next session's fresh signal."""
    from src.models import PortfolioDecision, TradeDecision
    from src.pipeline_context import RunContext

    pipeline = MagicMock()
    pipeline.broker.list_open_orders_checked.return_value = (True, [])  # listing read, nothing open
    pipeline.broker.get_latest_price.return_value = 100.0  # live market
    pipeline.broker.get_latest_quote.return_value = {"bid_price": 100.0, "ask_price": 100.0}
    pipeline._format_qty = lambda q: str(q)
    pipeline._order_accepted.return_value = True
    # Pre-BUY daily-loss re-check (fix for P1 #1) refreshes account state
    # and consults risk_engine — wire benign defaults so this orthogonal
    # entry-price-stale test isn't entangled with the loss-breach path.
    pipeline._refresh_account_state.return_value = (
        {"cash": 50_000.0, "portfolio_value": 100_000.0},
        [],
        {},
    )

    ctx = RunContext.start("morning")
    ctx.cash = 50_000.0
    ctx.total_value = 100_000.0
    ctx.last_equity = 100_000.0
    ctx.positions = []
    ctx.portfolio_decision = PortfolioDecision(
        reasoning_chain=_pm_rc(),
        decisions=[
            # LLM says entry $80, market is $100 → 20% off → must skip.
            TradeDecision(
                action="BUY",
                symbol="SPY",
                allocation_pct=10,
                entry_price=80.0,
                stop_loss=72.0,
                take_profit=130.0,
                reasoning="stale entry scenario",
            ),
        ],
        portfolio_view="test",
    )
    ctx.symbols_bars = {}

    stage = ExecutionStage(pipeline=pipeline)
    orders = stage.run(ctx)

    assert orders == [], "BUY should have been skipped entirely"
    pipeline.broker.submit_order.assert_not_called()


def test_execution_stage_allows_buy_when_entry_price_within_5pct():
    """A 2% deviation is well within the 5% threshold — BUY proceeds,
    sizing uses the live market price (limit < market → raised to market)."""
    from src.models import PortfolioDecision, TradeDecision
    from src.pipeline_context import RunContext

    pipeline = MagicMock()
    pipeline.broker.list_open_orders_checked.return_value = (True, [])  # listing read, nothing open
    pipeline.broker.get_latest_price.return_value = 100.0
    pipeline.broker.get_latest_quote.return_value = {"bid_price": 100.0, "ask_price": 100.0}
    pipeline.broker.submit_order.return_value = {
        "id": "order-1",
        "status": "accepted",
        "symbol": "SPY",
    }
    pipeline._format_qty = lambda q: str(q)
    pipeline._order_accepted.return_value = True
    pipeline._refresh_account_state.return_value = (
        {"cash": 50_000.0, "portfolio_value": 100_000.0},
        [],
        {},
    )

    ctx = RunContext.start("morning")
    ctx.cash = 50_000.0
    ctx.total_value = 100_000.0
    ctx.last_equity = 100_000.0
    ctx.positions = []
    ctx.portfolio_decision = PortfolioDecision(
        reasoning_chain=_pm_rc(),
        decisions=[
            # LLM says $98, market $100 → 2% off → proceed. TP=140 keeps
            # the raised-to-market R/R at (140-100)/(100-72)=1.43 — above the
            # audit-round-2 executed-geometry floor of 1.2 (a TP of 130 would
            # now be correctly SKIPPED at 1.07; that case has its own test).
            TradeDecision(
                action="BUY",
                symbol="SPY",
                allocation_pct=10,
                entry_price=98.0,
                stop_loss=72.0,
                take_profit=140.0,
                reasoning="fresh setup",
            ),
        ],
        portfolio_view="test",
    )
    ctx.symbols_bars = {}

    stage = ExecutionStage(pipeline=pipeline)
    stage.run(ctx)

    pipeline.broker.submit_order.assert_called_once()


def test_execution_stage_logs_when_finalize_cannot_confirm_coverage(caplog):
    """Morning SELL path: when finalize can't rebuild coverage (returns
    ok=False, having persisted the recovery intent), the shared helper logs a
    warning so the operator sees protection wasn't confirmed in-session (drain
    rebuilds it next session). With the real protected-sell helpers bound, the
    SELL submits + finalizes against the mocked broker end-to-end."""
    import logging
    from src.models import PortfolioDecision, Position, TradeDecision
    from src.pipeline_context import RunContext

    specs = [{"id": "stop-1", "qty": 10, "stop_price": 280.0, "limit_price": 275.0}]
    pipeline = MagicMock()
    pipeline.broker.list_open_orders_checked.return_value = (True, [])  # listing read, nothing open
    pipeline.broker.get_latest_price.return_value = 320.0
    pipeline.broker.get_latest_quote.return_value = {"bid_price": 320.0, "ask_price": 320.0}
    _mock_stop_seam(pipeline.broker, specs=specs)
    _mock_stage_seam(pipeline, specs=specs)
    # finalize couldn't rebuild coverage → (False, specs).
    pipeline._finalize_protection_after_sell.return_value = (False, list(specs))
    pipeline.broker.submit_order.return_value = {
        "id": "sell-9",
        "status": "accepted",
        "symbol": "JPM",
    }
    pipeline.broker.wait_for_order_terminal.return_value = "filled"
    # Board item 178: ExecutionStage now re-reads the account before the
    # SELL loop too, so this fixture must reflect a fresh snapshot that
    # STILL holds JPM (this test isn't exercising the staleness fix itself
    # — that's pinned separately — it needs the SELL to actually reach the
    # broker so the finalize-failure path under test can run).
    pipeline._refresh_account_state.return_value = (
        {"cash": 60_000.0, "portfolio_value": 100_000.0},
        [
            Position(
                symbol="JPM",
                qty=10.0,
                avg_entry=300.0,
                current_price=320.0,
                market_value=3_200.0,
                unrealized_pnl=200.0,
                sector="Financial",
            ),
        ],
        {},
    )
    pipeline._order_accepted.return_value = True
    pipeline._format_qty = lambda q: str(q)
    pipeline._full_sell_qty = lambda q: q
    pipeline.db = MagicMock()

    ctx = RunContext.start("morning")
    ctx.cash = 30_000.0
    ctx.total_value = 100_000.0
    ctx.last_equity = 100_000.0
    ctx.positions = [
        Position(
            symbol="JPM",
            qty=10.0,
            avg_entry=300.0,
            current_price=320.0,
            market_value=3_200.0,
            unrealized_pnl=200.0,
            sector="Financial",
        ),
    ]
    ctx.portfolio_decision = PortfolioDecision(
        reasoning_chain=_pm_rc(),
        decisions=[
            TradeDecision(
                action="SELL",
                symbol="JPM",
                allocation_pct=100,
                entry_price=300.0,
                stop_loss=280.0,
                take_profit=350.0,
                reasoning="exit",
            ),
        ],
        portfolio_view="test",
    )
    ctx.symbols_bars = {}

    stage = ExecutionStage(pipeline=pipeline)
    # The warning is emitted by _finalize_pending_protections, which lives on
    # TradingPipeline (src.pipeline) now that the loop is extracted.
    with caplog.at_level(logging.WARNING, logger="src.pipeline"):
        hold(
            pipeline.broker, {p.symbol: p.qty for p in ctx.positions}
        )  # what the broker holds when the exit is re-sized
        stage.run(ctx)

    # The SELL submitted through the real protected-sell helper...
    pipeline.broker.submit_order.assert_called_once()
    assert pipeline.broker.submit_order.call_args.kwargs["side"] == "sell"
    # ...and the finalize-failure warning surfaced.
    assert any(
        "did not confirm stop coverage" in r.getMessage() and "ExecutionStage" in r.getMessage() for r in caplog.records
    ), f"expected a finalize-failure warning; got {[r.getMessage() for r in caplog.records]}"


def test_execution_stage_sells_the_fresh_reduced_qty_not_the_run_open_snapshot():
    """Board item 178. `ctx.positions` is the run-OPEN broker snapshot,
    taken before Research/Decision/Risk ran; if the position was reduced
    mid-run (e.g. a stop filled) that snapshot is stale by the time this
    stage submits an ordinary SELL. Pin that the fix re-reads the account
    before sizing: the SELL must use the FRESH (lower) qty and the FRESH
    price for its limit, not the stale run-open ones."""
    from src.models import PortfolioDecision, Position
    from src.pipeline_context import RunContext

    pipeline = MagicMock()
    pipeline.broker.list_open_orders_checked.return_value = (True, [])  # listing read, nothing open
    _mock_stop_seam(pipeline.broker)
    _mock_stage_seam(pipeline)
    pipeline.broker.submit_order.return_value = {
        "id": "sell-1",
        "status": "accepted",
        "symbol": "JPM",
    }
    pipeline.broker.wait_for_order_terminal.return_value = "filled"
    pipeline._order_accepted.return_value = True
    pipeline._format_qty = lambda q: str(q)
    pipeline._full_sell_qty = lambda q: q
    pipeline.db = MagicMock()

    # Fresh broker read (mid-run): only 40 shares remain (down from the
    # run-open 100) and the price has moved to $340.
    fresh_position = Position(
        symbol="JPM",
        qty=40.0,
        avg_entry=250.0,
        current_price=340.0,
        market_value=13_600.0,
        unrealized_pnl=3_600.0,
        sector="Financial",
    )
    pipeline._refresh_account_state.return_value = (
        {"cash": 20_000.0, "portfolio_value": 60_000.0},
        [fresh_position],
        {"JPM": 340.0},
    )

    ctx = RunContext.start("morning")
    ctx.cash = 10_000.0
    ctx.total_value = 50_000.0
    ctx.last_equity = 50_000.0
    # Stale run-open snapshot: 100 shares @ $300.
    ctx.positions = [
        Position(
            symbol="JPM",
            qty=100.0,
            avg_entry=250.0,
            current_price=300.0,
            market_value=30_000.0,
            unrealized_pnl=5_000.0,
            sector="Financial",
        ),
    ]
    ctx.portfolio_decision = PortfolioDecision(
        reasoning_chain=_pm_rc(),
        decisions=[_sell("JPM")],
        portfolio_view="test",
    )
    ctx.symbols_bars = {}

    stage = ExecutionStage(pipeline=pipeline)
    hold(
        pipeline.broker, {fresh_position.symbol: fresh_position.qty}
    )  # what the broker holds when the exit is re-sized
    stage.run(ctx)

    pipeline._refresh_account_state.assert_called()
    pipeline.broker.submit_order.assert_called_once()
    call_kwargs = pipeline.broker.submit_order.call_args.kwargs
    assert call_kwargs["qty"] == 40.0, (
        f"SELL sized off the stale run-open qty (100) instead of the fresh read (40): got {call_kwargs['qty']}"
    )
    # 2026-10-09: an ordinary SELL is a plain market order; the fresh price
    # still travels as its reference.
    assert call_kwargs["limit_price"] is None
    assert call_kwargs["reference_price"] == 340.0, (
        f"SELL referenced the stale run-open price ($300) instead of "
        f"the fresh read ($340): got {call_kwargs['reference_price']}"
    )


def test_execution_stage_covers_the_fresh_reduced_qty_not_the_run_open_snapshot():
    """Same defect, COVER side: a short reduced mid-run (partial buy-to-cover
    already filled elsewhere, or a stop) must be covered off the fresh qty
    and price, not the run-open snapshot."""
    from src.models import PortfolioDecision, Position, TradeDecision
    from src.pipeline_context import RunContext

    pipeline = MagicMock()
    pipeline.broker.list_open_orders_checked.return_value = (True, [])  # listing read, nothing open
    _mock_stop_seam(pipeline.broker)
    _mock_stage_seam(pipeline)
    pipeline.broker.submit_order.return_value = {
        "id": "cover-1",
        "status": "accepted",
        "symbol": "TSLA",
    }
    pipeline.broker.wait_for_order_terminal.return_value = "filled"
    pipeline._order_accepted.return_value = True
    pipeline._format_qty = lambda q: str(q)
    pipeline._full_sell_qty = lambda q: q
    pipeline.db = MagicMock()

    # Fresh broker read (mid-run): the short shrank from -50 to -20 shares,
    # and the price moved to $210.
    fresh_position = Position(
        symbol="TSLA",
        qty=-20.0,
        avg_entry=200.0,
        current_price=210.0,
        market_value=-4_200.0,
        unrealized_pnl=-200.0,
        sector="Consumer Cyclical",
    )
    pipeline._refresh_account_state.return_value = (
        {"cash": 20_000.0, "portfolio_value": 60_000.0},
        [fresh_position],
        {"TSLA": 210.0},
    )

    ctx = RunContext.start("morning")
    ctx.cash = 10_000.0
    ctx.total_value = 50_000.0
    ctx.last_equity = 50_000.0
    # Stale run-open snapshot: -50 shares @ $200.
    ctx.positions = [
        Position(
            symbol="TSLA",
            qty=-50.0,
            avg_entry=200.0,
            current_price=200.0,
            market_value=-10_000.0,
            unrealized_pnl=0.0,
            sector="Consumer Cyclical",
        ),
    ]
    ctx.portfolio_decision = PortfolioDecision(
        reasoning_chain=_pm_rc(),
        decisions=[
            TradeDecision(
                action="COVER",
                symbol="TSLA",
                allocation_pct=100.0,
                entry_price=200.0,
                stop_loss=220.0,
                take_profit=180.0,
                reasoning="cover",
            ),
        ],
        portfolio_view="test",
    )
    ctx.symbols_bars = {}

    stage = ExecutionStage(pipeline=pipeline)
    hold(
        pipeline.broker, {fresh_position.symbol: fresh_position.qty}
    )  # what the broker holds when the exit is re-sized
    stage.run(ctx)

    pipeline._refresh_account_state.assert_called()
    pipeline.broker.submit_order.assert_called_once()
    call_kwargs = pipeline.broker.submit_order.call_args.kwargs
    assert call_kwargs["qty"] == 20.0, (
        f"COVER sized off the stale run-open qty (50) instead of the fresh read (20): got {call_kwargs['qty']}"
    )
    assert call_kwargs["limit_price"] is None
    assert call_kwargs["reference_price"] == 210.0, (
        f"COVER referenced the stale run-open price ($200) instead of "
        f"the fresh read ($210): got {call_kwargs['reference_price']}"
    )


def test_execution_stage_submits_buys_after_the_account_state_refresh():
    """Renamed 2026-09-20 (retired item 32). This was
    `..._allows_buys_when_daily_loss_not_breached_after_refresh` and its
    docstring described a pre-BUY daily-loss re-check that no longer exists,
    so it read as guarding a condition it could not reach. The refresh it
    exercises is still real and still runs; what it pins is that a BUY
    reaches the broker after it."""
    from src.models import PortfolioDecision, TradeDecision
    from src.pipeline_context import RunContext

    pipeline = MagicMock()
    pipeline.broker.list_open_orders_checked.return_value = (True, [])  # listing read, nothing open
    pipeline.broker.get_latest_price.return_value = 100.0
    pipeline.broker.get_latest_quote.return_value = {"bid_price": 100.0, "ask_price": 100.0}
    _mock_stop_seam(pipeline.broker)
    pipeline.broker.submit_order.return_value = {
        "id": "buy-1",
        "status": "accepted",
        "symbol": "SPY",
    }
    pipeline._refresh_account_state.return_value = (
        {"cash": 50_000.0, "portfolio_value": 100_500.0},
        [],
        {},
    )
    pipeline._order_accepted.return_value = True
    pipeline._format_qty = lambda q: str(q)

    ctx = RunContext.start("morning")
    ctx.cash = 50_000.0
    ctx.total_value = 100_000.0
    ctx.last_equity = 100_000.0
    ctx.positions = []
    ctx.portfolio_decision = PortfolioDecision(
        reasoning_chain=_pm_rc(),
        decisions=[
            TradeDecision(
                action="BUY",
                symbol="SPY",
                allocation_pct=10,
                entry_price=99.0,
                stop_loss=92.0,
                take_profit=110.0,
                reasoning="normal dip buy",
            ),
        ],
        portfolio_view="test",
    )
    ctx.symbols_bars = {}

    stage = ExecutionStage(pipeline=pipeline)
    stage.run(ctx)

    pipeline.broker.submit_order.assert_called_once()
    assert pipeline.broker.submit_order.call_args.kwargs["side"] == "buy"


def test_execution_stage_skips_buy_when_entry_price_above_market_by_more_than_5pct():
    """Symmetric case: LLM proposed entry ABOVE market by >5% — still stale,
    still skip. The direction of the deviation doesn't change the conclusion
    (LLM's thesis was priced at something that isn't the current tape)."""
    from src.models import PortfolioDecision, TradeDecision
    from src.pipeline_context import RunContext

    pipeline = MagicMock()
    pipeline.broker.list_open_orders_checked.return_value = (True, [])  # listing read, nothing open
    pipeline.broker.get_latest_price.return_value = 100.0
    pipeline.broker.get_latest_quote.return_value = {"bid_price": 100.0, "ask_price": 100.0}
    pipeline._format_qty = lambda q: str(q)
    pipeline._order_accepted.return_value = True
    pipeline._refresh_account_state.return_value = (
        {"cash": 50_000.0, "portfolio_value": 100_000.0},
        [],
        {},
    )

    ctx = RunContext.start("morning")
    ctx.cash = 50_000.0
    ctx.total_value = 100_000.0
    ctx.last_equity = 100_000.0
    ctx.positions = []
    ctx.portfolio_decision = PortfolioDecision(
        reasoning_chain=_pm_rc(),
        decisions=[
            # LLM says $115, market $100 → 15% above → skip.
            TradeDecision(
                action="BUY",
                symbol="SPY",
                allocation_pct=10,
                entry_price=115.0,
                stop_loss=105.0,
                take_profit=135.0,
                reasoning="above-market proposal",
            ),
        ],
        portfolio_view="test",
    )
    ctx.symbols_bars = {}

    stage = ExecutionStage(pipeline=pipeline)
    orders = stage.run(ctx)

    assert orders == []
    pipeline.broker.submit_order.assert_not_called()


def test_execution_stage_delegation_runs_pipeline_path():
    """Pipeline's `_execution_stage` thunks into `execution_stage.run(ctx)`."""
    from src.pipeline import TradingPipeline

    pipeline = build_pipeline(execution_stage=MagicMock())
    pipeline.execution_stage.run.return_value = ["order-1"]
    ctx = RunContext.start("morning")

    out = TradingPipeline._execution_stage(pipeline, ctx)

    assert out == ["order-1"]
    pipeline.execution_stage.run.assert_called_once_with(ctx)


def test_risk_stage_delegation_returns_early_exit_dict():
    from src.pipeline import TradingPipeline

    pipeline = build_pipeline(risk_stage=MagicMock())
    pipeline.risk_stage.run.return_value = {"status": "rejected", "orders": []}
    ctx = RunContext.start("morning")

    out = TradingPipeline._risk_stage(pipeline, ctx)

    assert out["status"] == "rejected"


# ---------------------------------------------------------------------------
# Stage 2 Checkpoint C — deterministic hard-risk block forensic persistence.
#
# Before this, RiskStage.run() returned early with an in-memory
# {"status": "hard_risk_block", "reason": ...} dict whenever the
# deterministic hard-risk gate blocked EVERY candidate before
# risk_manager ever ran — no row of any kind recorded which rule fired.
# `TradingPipeline._persist_hard_risk_block` now writes a forensic
# `agent_logs` row (agent_name="risk_gate") at both early-return sites.
# ---------------------------------------------------------------------------


def test_persist_hard_risk_block_writes_risk_gate_agent_log():
    """Unit test of the persistence helper itself: distinct sentinel
    agent_name (never confused with a real risk_manager LLM call), known-
    zero (not unknown) cost/tokens, decision_id threaded through, reasons
    text preserved verbatim."""
    from src.pipeline import TradingPipeline
    from src.pipeline_context import RunContext as _RunContext

    pipeline = build_pipeline(db=MagicMock())
    ctx = _RunContext.start("morning")
    ctx.decision_id = f"{ctx.run_id}-dec-000099"

    pipeline.risk_gate._persist_hard_risk_block(
        ctx,
        "AAPL position would be 25.0% and exceed max 20%",
        stage="pre_rm",
    )

    pipeline.db.insert_agent_log.assert_called_once()
    kwargs = pipeline.db.insert_agent_log.call_args.kwargs
    assert kwargs["agent_name"] == "risk_gate"
    assert kwargs["agent_name"] != "risk_manager"
    assert kwargs["run_id"] == ctx.run_id
    assert kwargs["decision_id"] == ctx.decision_id
    assert kwargs["status"] == "hard_risk_block"
    assert kwargs["model"] == "deterministic"
    assert kwargs["cost_usd"] == 0.0  # known-zero, not None/"unknown"
    assert kwargs["tokens_used"] == 0
    assert "exceed max 20%" in kwargs["full_response"]


def test_persist_hard_risk_block_never_raises_on_db_failure():
    """A persistence failure must never propagate into the risk decision
    path — it's forensic-only, additive, and non-critical."""
    from src.pipeline import TradingPipeline
    from src.pipeline_context import RunContext as _RunContext

    pipeline = build_pipeline(db=MagicMock())
    pipeline.db.insert_agent_log.side_effect = RuntimeError("disk full")
    ctx = _RunContext.start("morning")

    pipeline.risk_gate._persist_hard_risk_block(ctx, "reason", stage="pre_rm")
    # No exception raised — that's the assertion.


def _risk_stage_pipeline(decisions):
    """A pipeline stub wired just enough for RiskStage.run() to reach the
    hard-risk-block early-return path deterministically, mirroring
    tests/test_bugfixes.py's direct-_filter_hard_risk_decisions style but
    through the full RiskStage.run() so the persistence call site itself
    is exercised, not just the helper."""
    from src.pipeline import TradingPipeline

    pipeline = build_pipeline(
        db=MagicMock(),
        _sweeper=MagicMock(return_value=None),
        _filter_supported_symbols=MagicMock(return_value=(decisions, [])),
        _refuse_queued_earnings_buys=MagicMock(return_value=decisions),
    )
    return pipeline


def test_risk_stage_persists_hard_risk_block_when_pre_rm_gate_blocks_everything():
    """First early-return site (before risk_manager.review is ever called)."""
    from src.models import PortfolioDecision

    decisions = [_buy("AAPL", 25)]
    pipeline = _risk_stage_pipeline(decisions)
    pipeline.risk_gate._filter_hard_risk_decisions = MagicMock(
        return_value=([], [], ["AAPL position would be 25.0% and exceed max 20%"]),
    )

    ctx = RunContext.start("morning")
    ctx.decision_id = f"{ctx.run_id}-dec-000001"
    ctx.total_value = 100_000.0
    ctx.last_equity = 100_000.0
    ctx.cash = 50_000.0
    ctx.portfolio_decision = PortfolioDecision(
        reasoning_chain=_pm_rc(),
        decisions=decisions,
        portfolio_view="test",
    )

    stage = RiskStage(pipeline=pipeline)
    result = stage.run(ctx)

    assert result == {
        "status": "hard_risk_block",
        "orders": [],
        "reason": "AAPL position would be 25.0% and exceed max 20%",
    }
    pipeline.db.insert_agent_log.assert_called_once()
    kwargs = pipeline.db.insert_agent_log.call_args.kwargs
    assert kwargs["agent_name"] == "risk_gate"
    assert kwargs["run_id"] == ctx.run_id
    assert kwargs["decision_id"] == ctx.decision_id
    assert "exceed max 20%" in kwargs["full_response"]


def test_risk_stage_records_visible_event_when_rm_zeroes_a_sell():
    """End-to-end through RiskStage.run() with the REAL (unmocked)
    `_apply_risk_modifications`: an RM modification that zeroes a SELL's
    allocation_pct must not silently cancel the exit, and the refusal must
    be persisted as a visible pipeline event rather than just vanishing.
    Matches the two live 2026-08-24 incidents this fix closes."""
    from src.models import PortfolioDecision, RiskVerdict

    sell = _sell("XLE")
    pipeline = _risk_stage_pipeline([sell])

    verdict = RiskVerdict(
        approved=True,
        reasoning_chain=_risk_rc(),
        reasoning="ok",
        modifications=[
            {
                "symbol": "XLE",
                "field": "allocation_pct",
                "original_value": 100,
                "new_value": 0,
                "reason": "RM believes this exit is unnecessary",
            }
        ],
    )
    rm_result = MagicMock()
    rm_result.used_fallback = False
    pipeline.risk_manager = MagicMock()
    pipeline.risk_manager.review.return_value = (verdict, rm_result)

    pipeline.risk_gate._filter_hard_risk_decisions = MagicMock(
        side_effect=[
            ([sell], [], []),
            ([sell], [], []),
        ],
    )

    ctx = RunContext.start("morning")
    ctx.decision_id = f"{ctx.run_id}-dec-000010"
    ctx.total_value = 100_000.0
    ctx.last_equity = 100_000.0
    ctx.cash = 50_000.0
    ctx.portfolio_decision = PortfolioDecision(
        reasoning_chain=_pm_rc(),
        decisions=[sell],
        portfolio_view="test",
    )

    # Owner ruling 13 Oct 2026 (same rules as buying): a PM sale is decided by
    # the same-rules gate, and unknown never sells, so with no technical read
    # this sale would now be refused before the RM step. This run's technical
    # read opposes the held long, so the gate allows it and the RM-zeroing
    # behaviour below is still what this test checks.
    from types import SimpleNamespace

    ctx.seat_verdicts = [
        SimpleNamespace(seat="technical", symbol="XLE", direction="bearish", invalidation="x", evidence=[object()])
    ]
    ctx.held_bar_record = {"XLE": ()}

    stage = RiskStage(pipeline=pipeline)
    result = stage.run(ctx)

    # The exit still ships — RM's edit was reverted, not the trade.
    assert result is None
    assert ctx.portfolio_decision.decisions[0].allocation_pct == 100.0

    evidence_calls = pipeline.db.insert_specialist_evidence.call_args_list
    rejection_calls = [
        c
        for c in evidence_calls
        if c.kwargs.get("kind") == "pipeline_event" and "modification_rejected" in c.kwargs.get("evidence_json", "")
    ]
    assert len(rejection_calls) == 1
    assert rejection_calls[0].kwargs["symbol"] == "XLE"


def test_risk_stage_persists_hard_risk_block_when_post_rm_modifications_block_everything():
    """Second early-return site: RM approves with modifications, the
    re-filter after applying them blocks everything. risk_manager.review
    IS reached and logged normally here — the forensic risk_gate row is
    additive on top of (not instead of) the real risk_manager agent_logs
    row RiskStage.run() already writes for a reached RM call."""
    from src.models import PortfolioDecision, RiskVerdict

    first_pass_decisions = [_buy("AAPL", 10)]
    pipeline = _risk_stage_pipeline(first_pass_decisions)
    pipeline.risk_gate._apply_risk_modifications = MagicMock(return_value=(first_pass_decisions, []))

    verdict = RiskVerdict(
        approved=True,
        reasoning_chain=_risk_rc(),
        reasoning="trim AAPL",
        modifications=[
            {
                "symbol": "AAPL",
                "field": "allocation_pct",
                "original_value": 10,
                "new_value": 5,
                "reason": "trim sizing",
            }
        ],
    )
    rm_result = MagicMock()
    rm_result.used_fallback = False
    pipeline.risk_manager = MagicMock()
    pipeline.risk_manager.review.return_value = (verdict, rm_result)

    # First _filter_hard_risk_decisions call (pre-RM) lets the BUY through;
    # second call (post-modifications re-filter) blocks everything.
    pipeline.risk_gate._filter_hard_risk_decisions = MagicMock(
        side_effect=[
            (first_pass_decisions, [], []),
            ([], [], ["AAPL position would be 25.0% and exceed max 20%"]),
        ],
    )

    ctx = RunContext.start("morning")
    ctx.decision_id = f"{ctx.run_id}-dec-000002"
    ctx.total_value = 100_000.0
    ctx.last_equity = 100_000.0
    ctx.cash = 50_000.0
    ctx.portfolio_decision = PortfolioDecision(
        reasoning_chain=_pm_rc(),
        decisions=first_pass_decisions,
        portfolio_view="test",
    )

    stage = RiskStage(pipeline=pipeline)
    result = stage.run(ctx)

    assert result == {
        "status": "hard_risk_block",
        "orders": [],
        "reason": "AAPL position would be 25.0% and exceed max 20%",
    }
    # One real risk_manager agent_logs write (RM was reached) + one
    # forensic risk_gate write (post-modifications block).
    assert pipeline.db.insert_agent_log.call_count == 2
    agent_names = [c.kwargs["agent_name"] for c in pipeline.db.insert_agent_log.call_args_list]
    assert agent_names == ["risk_manager", "risk_gate"]
    gate_kwargs = pipeline.db.insert_agent_log.call_args_list[1].kwargs
    assert gate_kwargs["decision_id"] == ctx.decision_id
    assert "exceed max 20%" in gate_kwargs["full_response"]


def test_risk_stage_invested_target_is_the_mandate_not_a_carried_macro_number():
    """Owner mandate 2026-09-17: the invested target is fixed at 100%. A
    carried-forward macro snapshot that still carries an old
    `target_invested_pct` (62.5 here) must neither crash RiskStage (the
    original AttributeError regression this test guarded) nor lower the
    target the pre-trade gate measures against."""
    from src.models import PortfolioDecision

    decisions = [_buy("AAPL", 25)]
    pipeline = _risk_stage_pipeline(decisions)
    # Block everything on the pre-RM hard gate so run() returns right after
    # macro_target_pct is computed, without needing to mock the RM call.
    pipeline.risk_gate._filter_hard_risk_decisions = MagicMock(
        return_value=([], [], ["AAPL position would be 25.0% and exceed max 20%"]),
    )

    ctx = RunContext.start("intra_check")
    ctx.decision_id = f"{ctx.run_id}-dec-000003"
    ctx.total_value = 100_000.0
    ctx.last_equity = 100_000.0
    ctx.cash = 50_000.0
    ctx.macro_analysis = _carried_forward_macro_dict(target_invested_pct=62.5)
    ctx.portfolio_decision = PortfolioDecision(
        reasoning_chain=_pm_rc(),
        decisions=decisions,
        portfolio_view="test",
    )

    stage = RiskStage(pipeline=pipeline)
    # The bug: macro_analysis.position_guidance raises AttributeError on a
    # dict. No exception is the primary assertion here.
    result = stage.run(ctx)

    assert result == {
        "status": "hard_risk_block",
        "orders": [],
        "reason": "AAPL position would be 25.0% and exceed max 20%",
    }
    assert ctx.invested_target_pct == 100.0
    # The hard-risk filter itself must have been invoked WITH the mandate —
    # never with macro's old 62.5, and never with None (which would silently
    # disable the deployment-gap advisory for the rest of the run).
    fh_kwargs = pipeline.risk_gate._filter_hard_risk_decisions.call_args.kwargs
    assert fh_kwargs["invested_target_pct"] == 100.0
    assert "macro_target_invested_pct" not in fh_kwargs


def test_risk_stage_does_not_flag_same_session_reuse_as_data_degraded():
    """2026-09-16 intra_check-f0f27e08: morning news/macro ran, intra
    reused them by design, earnings was an intentional skip, tech re-ran
    clean. RiskStage's 2+ advisory used to treat all three reuse words
    as degraded and dump the status dict into RM, which then vetoed the
    whole plan as a data-integrity failure. Reuse is usable; the
    advisory must not fire, and must not mention those words if it
    fires for some other reason."""
    from src.models import PortfolioDecision, RiskVerdict

    decisions = [_buy("MRVL", 5)]
    pipeline = _risk_stage_pipeline(decisions)
    pipeline.risk_gate._filter_hard_risk_decisions = MagicMock(
        side_effect=lambda d, *a, **kw: (list(d), [], []),
    )
    pipeline.risk_gate._apply_risk_modifications = MagicMock(return_value=(decisions, []))
    pipeline._ensure_correlation_matrix = MagicMock(return_value={})
    verdict = RiskVerdict(
        approved=True,
        reasoning_chain=_risk_rc(),
        reasoning="clean reuse",
    )
    rm_result = MagicMock()
    rm_result.used_fallback = False
    pipeline.risk_manager = MagicMock()
    pipeline.risk_manager.review.return_value = (verdict, rm_result)

    ctx = RunContext.start("intra_check")
    ctx.decision_id = f"{ctx.run_id}-dec-000016"
    ctx.total_value = 100_000.0
    ctx.last_equity = 100_000.0
    ctx.cash = 50_000.0
    ctx.data_status = {
        "tech": "ok",
        "macro": "carried_from_morning",
        "news": "carried_from_morning",
        "earnings": "not_run_intraday",
    }
    ctx.portfolio_decision = PortfolioDecision(
        reasoning_chain=_pm_rc(),
        decisions=decisions,
        portfolio_view="test",
    )

    result = RiskStage(pipeline=pipeline).run(ctx)

    assert result is None
    pipeline.risk_manager.review.assert_called_once()
    violations = pipeline.risk_manager.review.call_args.kwargs["rule_violations"]
    degraded = [v for v in violations if v.rule == "data_degraded"]
    assert degraded == [], (
        f"same-session reused morning intel must not be a Risk data-integrity advisory; got {degraded!r}"
    )


def test_risk_stage_data_degraded_message_omits_reuse_statuses():
    """If two REAL failures still trip the advisory, the message must
    name those seats only — not smuggle carried_from_morning /
    not_run_intraday back into RM via the full-dict dump."""
    from src.models import PortfolioDecision, RiskVerdict

    decisions = [_buy("MRVL", 5)]
    pipeline = _risk_stage_pipeline(decisions)
    pipeline.risk_gate._filter_hard_risk_decisions = MagicMock(
        side_effect=lambda d, *a, **kw: (list(d), [], []),
    )
    pipeline.risk_gate._apply_risk_modifications = MagicMock(return_value=(decisions, []))
    pipeline._ensure_correlation_matrix = MagicMock(return_value={})
    verdict = RiskVerdict(
        approved=True,
        reasoning_chain=_risk_rc(),
        reasoning="two real failures",
    )
    rm_result = MagicMock()
    rm_result.used_fallback = False
    pipeline.risk_manager = MagicMock()
    pipeline.risk_manager.review.return_value = (verdict, rm_result)

    ctx = RunContext.start("intra_check")
    ctx.decision_id = f"{ctx.run_id}-dec-000017"
    ctx.total_value = 100_000.0
    ctx.last_equity = 100_000.0
    ctx.cash = 50_000.0
    ctx.data_status = {
        "tech": "partial",
        "macro": "failed",
        "news": "carried_from_morning",
        "earnings": "not_run_intraday",
    }
    ctx.portfolio_decision = PortfolioDecision(
        reasoning_chain=_pm_rc(),
        decisions=decisions,
        portfolio_view="test",
    )

    RiskStage(pipeline=pipeline).run(ctx)

    violations = pipeline.risk_manager.review.call_args.kwargs["rule_violations"]
    degraded = [v for v in violations if v.rule == "data_degraded"]
    assert len(degraded) == 1
    message = degraded[0].message
    assert "macro" in message and "tech" in message
    assert "carried_from_morning" not in message
    assert "not_run_intraday" not in message
    assert "news" not in message
    assert "earnings" not in message


def test_risk_stage_invested_target_holds_when_guidance_missing():
    """A carried-forward snapshot may legitimately lack position_guidance
    (e.g. very old / partially-written state). Must never raise, and the
    fully-invested mandate does not depend on macro at all."""
    from src.models import PortfolioDecision

    decisions = [_buy("AAPL", 5)]
    pipeline = _risk_stage_pipeline(decisions)
    pipeline.risk_gate._filter_hard_risk_decisions = MagicMock(
        return_value=([], [], ["blocked"]),
    )

    ctx = RunContext.start("intra_check")
    ctx.decision_id = f"{ctx.run_id}-dec-000004"
    ctx.total_value = 100_000.0
    ctx.last_equity = 100_000.0
    ctx.cash = 50_000.0
    ctx.macro_analysis = {"regime": "risk-on"}  # no position_guidance key
    ctx.portfolio_decision = PortfolioDecision(
        reasoning_chain=_pm_rc(),
        decisions=decisions,
        portfolio_view="test",
    )

    RiskStage(pipeline=pipeline).run(ctx)

    assert ctx.invested_target_pct == 100.0


# ---------------------------------------------------------------------------
# Parse loss reconciled against the book (2026-09-21 META incident)
# ---------------------------------------------------------------------------


def _parse_loss_violations(drops, decisions=None, positions=None):
    """Run RiskStage to the RM call with `drops` recorded in the GLOBAL
    parse telemetry, and return the parse-loss advisories it built.

    Deliberately end-to-end through `RiskStage.run`: the whole point of the
    fix is that the reconciliation happens where the book is held, so a test
    that called the helper directly would not prove the risk stage passes it
    the right book.
    """
    from src.models import PortfolioDecision, RiskVerdict, parse_telemetry

    decisions = decisions if decisions is not None else [_buy("MRVL", 5)]
    pipeline = _risk_stage_pipeline(decisions)
    pipeline.admission = MagicMock(
        _filter_supported_symbols=MagicMock(
            side_effect=lambda d, *a, **kw: (list(d), []),
        )
    )
    pipeline.risk_gate._refuse_queued_earnings_buys = MagicMock(
        side_effect=lambda d, *a, **kw: list(d),
    )
    pipeline.risk_gate._filter_hard_risk_decisions = MagicMock(
        side_effect=lambda d, *a, **kw: (list(d), [], []),
    )
    pipeline.risk_gate._apply_risk_modifications = MagicMock(
        side_effect=lambda d, *a, **kw: (list(d), []),
    )
    pipeline._ensure_correlation_matrix = MagicMock(return_value={})
    verdict = RiskVerdict(
        approved=True,
        reasoning_chain=_risk_rc(),
        reasoning="ok",
    )
    rm_result = MagicMock()
    rm_result.used_fallback = False
    pipeline.risk_manager = MagicMock()
    pipeline.risk_manager.review.return_value = (verdict, rm_result)

    ctx = RunContext.start("intra_check")
    ctx.decision_id = f"{ctx.run_id}-dec-000099"
    ctx.total_value = 100_000.0
    ctx.last_equity = 100_000.0
    ctx.cash = 50_000.0
    ctx.positions = list(positions or [])
    ctx.data_status = {"tech": "ok"}
    ctx.portfolio_decision = PortfolioDecision(
        reasoning_chain=_pm_rc(),
        decisions=decisions,
        portfolio_view="test",
    )

    parse_telemetry.reset()
    try:
        for model, key in drops:
            parse_telemetry.record_dropped_item(model, key)
        RiskStage(pipeline=pipeline).run(ctx)
    finally:
        parse_telemetry.reset()

    pipeline.risk_manager.review.assert_called_once()
    violations = pipeline.risk_manager.review.call_args.kwargs["rule_violations"]
    return {v.rule: v for v in violations if v.rule.startswith("analysis_parse_loss")}


def _held(symbol):
    from src.models import Position

    return Position(
        symbol=symbol,
        qty=10,
        avg_entry=100.0,
        current_price=101.0,
        market_value=1010.0,
        unrealized_pnl=10.0,
        sector="Technology",
    )


def test_a_recovered_parse_drop_is_a_cost_note_not_a_missing_coverage_claim():
    """2026-09-21 intra_check: META was held with stops at 14:15:45, its row
    dropped at 14:16:35, META was re-analysed and re-sized by the constructor
    at 14:17:31, and the risk stage still told the Risk Manager it was
    "discarded at parse ... and absent from the book below". It was in the
    book. The RM called the environment degraded on that sentence."""
    found = _parse_loss_violations(
        [("TechAnalysisResult", "META")],
        decisions=[_buy("META", 5)],
        positions=[_held("META")],
    )
    assert "analysis_parse_loss" not in found, (
        f"a symbol sitting in the book is not a parse LOSS; got {found.get('analysis_parse_loss')!r}"
    )
    note = found.get("analysis_parse_loss_recovered")
    assert note is not None, "the paid round-trip must still be reported"
    assert "META" in note.message
    assert "absent" not in note.message.lower(), note.message
    assert "RECOVERED" in note.message


def test_a_genuinely_lost_symbol_keeps_the_violation_and_its_wording():
    """The falsifiable clause survives for the case where it is true."""
    found = _parse_loss_violations(
        [("TechAnalysisResult", "NVDA")],
        decisions=[_buy("MRVL", 5)],
        positions=[],
    )
    assert "analysis_parse_loss_recovered" not in found
    lost = found.get("analysis_parse_loss")
    assert lost is not None
    assert "NVDA" in lost.message
    assert "are absent from the book below" in lost.message
    assert lost.value == 1.0


def test_an_unidentified_drop_is_always_treated_as_lost():
    """`"?"` is a row whose own symbol could not be read — it can never be
    matched against the book, and one entry may aggregate several malformed
    rows, so it cannot be PROVEN recovered and must not be."""
    found = _parse_loss_violations(
        [("TechAnalysisResult", "?")],
        decisions=[_buy("MRVL", 5)],
        positions=[_held("MRVL")],
    )
    assert "analysis_parse_loss_recovered" not in found, "an unidentified row must never be reported as recovered"
    lost = found.get("analysis_parse_loss")
    assert lost is not None
    assert "TechAnalysisResult:?" in lost.message
    assert "could not be read" in lost.message


def test_both_categories_render_together_when_both_occur():
    """A run with one recovered and one lost drop reports both, separately."""
    found = _parse_loss_violations(
        [("TechAnalysisResult", "META"), ("TechAnalysisResult", "NVDA")],
        decisions=[_buy("META", 5)],
        positions=[],
    )
    lost = found.get("analysis_parse_loss")
    note = found.get("analysis_parse_loss_recovered")
    assert lost is not None and note is not None
    assert "NVDA" in lost.message and "META" not in lost.message
    assert "META" in note.message and "NVDA" not in note.message
    assert "absent" not in note.message.lower()


def test_reconciliation_never_mutates_the_global_parse_counter():
    """Five research seats share one global `parse_telemetry` and the tech
    seat runs twice per morning run, so the risk stage may only READ it.
    Nothing here un-records a drop."""
    from src.models import parse_telemetry
    from src.pipeline_stages import _parse_loss_advisories, _reconcile_parse_loss

    parse_telemetry.reset()
    try:
        parse_telemetry.record_dropped_item("TechAnalysisResult", "META")
        before = parse_telemetry.dropped_snapshot()
        _reconcile_parse_loss(parse_telemetry.dropped_snapshot(), {"META"})
        _parse_loss_advisories(parse_telemetry.dropped_snapshot(), {"META"})
        assert parse_telemetry.dropped_snapshot() == before
        assert parse_telemetry.total_dropped() == 1
    finally:
        parse_telemetry.reset()


def test_risk_parse_failure_is_agent_failure_not_rejection():
    """No validated RiskVerdict means the agent failed; it did not veto."""
    from src.agents.base import AgentResult
    from src.models import PortfolioDecision

    decisions = [_buy("AAPL", 10)]
    pipeline = _risk_stage_pipeline(decisions)
    pipeline.risk_gate._filter_hard_risk_decisions = MagicMock(
        return_value=(decisions, [], []),
    )
    pipeline.risk_manager = MagicMock()
    pipeline.risk_manager.review.return_value = (
        None,
        AgentResult(
            raw_text="not valid json",
            tokens_used=10,
            model="test-model",
            user_message="risk input",
        ),
    )

    ctx = RunContext.start("morning")
    ctx.decision_id = f"{ctx.run_id}-dec-failed"
    ctx.total_value = 100_000.0
    ctx.last_equity = 100_000.0
    ctx.cash = 50_000.0
    ctx.portfolio_decision = PortfolioDecision(
        reasoning_chain=_pm_rc(),
        decisions=decisions,
        portfolio_view="test",
    )

    result = RiskStage(pipeline=pipeline).run(ctx)

    assert result == {
        "status": "agent_failure",
        "orders": [],
        "reason": "risk_manager_unparseable_output",
    }
    risk_log = pipeline.db.insert_agent_log.call_args.kwargs
    assert risk_log["status"] == "agent_failure"
    evidence = pipeline.db.insert_specialist_evidence.call_args.kwargs
    assert evidence["kind"] == "agent_failure"


def test_decision_stage_delegation_returns_none():
    """Method contract preserved: _decision_stage mutates ctx, returns None."""
    from src.pipeline import TradingPipeline

    pipeline = build_pipeline(decision_stage=MagicMock())
    ctx = RunContext.start("morning")

    out = TradingPipeline._decision_stage(pipeline, ctx)

    assert out is None
    pipeline.decision_stage.run.assert_called_once_with(ctx)


def test_decision_stage_passes_valid_carried_macro_dict_after_coerce():
    """A same-day snapshot is a plain dict, not a MacroAnalysis model.
    Calling .model_dump() on that dict used to AttributeError and kill
    the scan. Coerce dict sector_guidance, re-validate, pass the dump —
    do not smuggle an unvalidated trim, do not crash.
    """
    from src.models import MacroAnalysis
    from src.pipeline import TradingPipeline

    p = build_pipeline(db=MagicMock(), portfolio_manager=MagicMock())
    p.db.get_latest_insights.return_value = None
    p._sweeper = MagicMock(return_value=None)
    p._compute_recent_performance = MagicMock(return_value={})
    p._build_position_history = MagicMock(return_value={})
    p._build_weekly_narrative = MagicMock(return_value="")
    p._build_macro_trajectory = MagicMock(return_value="")
    p._build_active_state_changes = MagicMock(return_value="")
    p._build_pm_recent_decisions = MagicMock(return_value="")
    p._build_projected_portfolio = MagicMock(return_value="")
    p._build_calibration_note = MagicMock(return_value="")
    p._build_macro_tech_alignment = MagicMock(return_value="")
    p._build_recent_missed_lessons = MagicMock(return_value="")
    p._build_recent_loss_pits = MagicMock(return_value="")
    p._build_pm_facts = MagicMock(return_value=MagicMock())
    p._ensure_correlation_matrix = MagicMock(return_value={})
    p.config = MagicMock()
    p.config.risk.allow_margin = False
    p.config.trading.universe = []
    p._last_symbol_sectors = {}
    p.portfolio_manager.decide.return_value = (
        None,
        MagicMock(
            user_message="m",
            raw_text="{}",
            tokens_used=1,
            input_tokens=1,
            output_tokens=1,
            cost_usd=0.0,
            model="test-model",
        ),
    )

    macro_dict = _valid_carried_macro_dict()
    ctx = RunContext.start("intra_check")
    ctx.positions = []
    ctx.analyses = []
    ctx.macro_analysis = macro_dict
    ctx.total_value = 100_000.0
    ctx.last_equity = 100_000.0
    ctx.cash = 50_000.0
    ctx.deployable_cash = 50_000.0
    ctx.admitted_symbols = set()

    DecisionStage(pipeline=p).run(ctx)

    kwargs = p.portfolio_manager.decide.call_args.kwargs
    passed = kwargs["macro_analysis"]
    assert passed is not None
    assert passed["regime"] == "risk-on"
    MacroAnalysis.model_validate(passed)


def test_decision_stage_does_not_pass_chainless_trim_into_pm():
    """Silent broken macro into PM is the Phase 13 afternoon failure."""
    from src.pipeline import TradingPipeline
    from src.agents.portfolio_manager import PortfolioManagerAgent

    PortfolioManagerAgent._macro_parse_failures = []
    p = build_pipeline(db=MagicMock(), portfolio_manager=MagicMock())
    p.db.get_latest_insights.return_value = None
    p._sweeper = MagicMock(return_value=None)
    p._compute_recent_performance = MagicMock(return_value={})
    p._build_position_history = MagicMock(return_value={})
    p._build_weekly_narrative = MagicMock(return_value="")
    p._build_macro_trajectory = MagicMock(return_value="")
    p._build_active_state_changes = MagicMock(return_value="")
    p._build_pm_recent_decisions = MagicMock(return_value="")
    p._build_projected_portfolio = MagicMock(return_value="")
    p._build_calibration_note = MagicMock(return_value="")
    p._build_macro_tech_alignment = MagicMock(return_value="")
    p._build_recent_missed_lessons = MagicMock(return_value="")
    p._build_recent_loss_pits = MagicMock(return_value="")
    p._build_pm_facts = MagicMock(return_value=MagicMock())
    p._ensure_correlation_matrix = MagicMock(return_value={})
    p.config = MagicMock()
    p.config.risk.allow_margin = False
    p.config.trading.universe = []
    p._last_symbol_sectors = {}
    p.portfolio_manager.decide.return_value = (
        None,
        MagicMock(
            user_message="m",
            raw_text="{}",
            tokens_used=1,
            input_tokens=1,
            output_tokens=1,
            cost_usd=0.0,
            model="test-model",
        ),
    )

    ctx = RunContext.start("intra_check")
    ctx.positions = []
    ctx.analyses = []
    ctx.macro_analysis = _carried_forward_macro_dict()
    ctx.total_value = 100_000.0
    ctx.last_equity = 100_000.0
    ctx.cash = 50_000.0
    ctx.deployable_cash = 50_000.0
    ctx.admitted_symbols = set()

    DecisionStage(pipeline=p).run(ctx)

    kwargs = p.portfolio_manager.decide.call_args.kwargs
    assert kwargs["macro_analysis"] is None
    PortfolioManagerAgent._macro_parse_failures = []


def test_decision_stage_still_model_dumps_a_fresh_macro_model():
    """Companion regression: when macro DID run this tick, ctx.macro_analysis
    is a real MacroAnalysis (Pydantic) model. The fix must not break that
    path — .model_dump() should still be called so the PM sees a plain
    dict either way."""
    from src.pipeline import TradingPipeline
    from src.models import (
        MacroAnalysis,
        MacroPositionGuidance,
        MacroReasoningChain,
    )

    p = build_pipeline(db=MagicMock(), portfolio_manager=MagicMock())
    p.db.get_latest_insights.return_value = None
    p._sweeper = MagicMock(return_value=None)
    p._compute_recent_performance = MagicMock(return_value={})
    p._build_position_history = MagicMock(return_value={})
    p._build_weekly_narrative = MagicMock(return_value="")
    p._build_macro_trajectory = MagicMock(return_value="")
    p._build_active_state_changes = MagicMock(return_value="")
    p._build_pm_recent_decisions = MagicMock(return_value="")
    p._build_projected_portfolio = MagicMock(return_value="")
    p._build_calibration_note = MagicMock(return_value="")
    p._build_macro_tech_alignment = MagicMock(return_value="")
    p._build_recent_missed_lessons = MagicMock(return_value="")
    p._build_recent_loss_pits = MagicMock(return_value="")
    p._build_pm_facts = MagicMock(return_value=MagicMock())
    p._ensure_correlation_matrix = MagicMock(return_value={})
    p.config = MagicMock()
    p.config.risk.allow_margin = False
    p.config.trading.universe = []
    p._last_symbol_sectors = {}
    p.portfolio_manager.decide.return_value = (
        None,
        MagicMock(
            user_message="m",
            raw_text="{}",
            tokens_used=1,
            input_tokens=1,
            output_tokens=1,
            cost_usd=0.0,
            model="test-model",
        ),
    )

    macro_model = MacroAnalysis(
        regime="risk-on",
        confidence="high",
        equity_outlook="bullish",
        summary="fresh macro run",
        position_guidance=MacroPositionGuidance(
            target_invested_pct=55.0,
            cash_recommendation_pct=45.0,
            reasoning="x",
        ),
        sector_guidance=[],
        reasoning_chain=MacroReasoningChain(
            volatility_analysis="x",
            yield_curve_analysis="x",
            monetary_policy_analysis="x",
            inflation_labor_credit="x",
            cross_signal_synthesis="x",
            sector_implications="x",
        ),
    )
    ctx = RunContext.start("morning")
    ctx.positions = []
    ctx.analyses = []
    ctx.macro_analysis = macro_model
    ctx.total_value = 100_000.0
    ctx.last_equity = 100_000.0
    ctx.cash = 50_000.0
    ctx.deployable_cash = 50_000.0
    ctx.admitted_symbols = set()

    DecisionStage(pipeline=p).run(ctx)

    kwargs = p.portfolio_manager.decide.call_args.kwargs
    assert kwargs["macro_analysis"] == macro_model.model_dump()


def test_decision_stage_threads_the_configured_starter_size():
    """No reward:risk floor is threaded any more (the constant was deleted
    2026-10-15). `starter_risk_pct` is
    still read off the deterministic risk layer's own config, so a
    settings.yaml override moves it (2026-09-02)."""
    from src.pipeline import TradingPipeline

    p = build_pipeline(db=MagicMock(), portfolio_manager=MagicMock())
    p.db.get_latest_insights.return_value = None
    p._sweeper = MagicMock(return_value=None)
    p._compute_recent_performance = MagicMock(return_value={})
    p._build_position_history = MagicMock(return_value={})
    p._build_weekly_narrative = MagicMock(return_value="")
    p._build_macro_trajectory = MagicMock(return_value="")
    p._build_active_state_changes = MagicMock(return_value="")
    p._build_pm_recent_decisions = MagicMock(return_value="")
    p._build_projected_portfolio = MagicMock(return_value="")
    p._build_calibration_note = MagicMock(return_value="")
    p._build_macro_tech_alignment = MagicMock(return_value="")
    p._build_recent_missed_lessons = MagicMock(return_value="")
    p._build_recent_loss_pits = MagicMock(return_value="")
    p._build_pm_facts = MagicMock(return_value=MagicMock())
    p._ensure_correlation_matrix = MagicMock(return_value={})
    p.config = MagicMock()
    p.config.risk.allow_margin = False
    # Deliberately NOT the default, so a hardcoded number cannot pass.
    p.config.risk.min_position_risk_pct = 0.3
    p.config.trading.universe = []
    p._last_symbol_sectors = {}
    p.portfolio_manager.decide.return_value = (
        None,
        MagicMock(
            user_message="m",
            raw_text="{}",
            tokens_used=1,
            input_tokens=1,
            output_tokens=1,
            cost_usd=0.0,
            model="test-model",
        ),
    )

    ctx = RunContext.start("morning")
    ctx.positions = []
    ctx.analyses = []
    ctx.macro_analysis = None
    ctx.total_value = 100_000.0
    ctx.last_equity = 100_000.0
    ctx.cash = 50_000.0
    ctx.deployable_cash = 50_000.0
    ctx.admitted_symbols = set()

    DecisionStage(pipeline=p).run(ctx)

    kwargs = p.portfolio_manager.decide.call_args.kwargs
    assert "rr_floor" not in kwargs
    assert kwargs["starter_risk_pct"] == 0.3


def test_morning_research_stage_constructs_with_all_deps():
    """Stage wiring — all required dependencies exposed as constructor kwargs."""
    stage = MorningResearchStage(
        config=MagicMock(),
        db=MagicMock(),
        market=MagicMock(),
        macro=MagicMock(),
        news_provider=MagicMock(),
        news_store=MagicMock(),
        macro_store=MagicMock(),
        tech_store=MagicMock(),
        earnings_provider=MagicMock(),
        macro_analyst=MagicMock(),
        news_analyst=MagicMock(),
        tech_analyst=MagicMock(),
        earnings_analyst=MagicMock(),
        has_actionable_signal_fn=lambda *args, **kw: True,
        run_news_update_fn=lambda *a, **kw: (None, None),
        load_earnings_analyses_fn=lambda *a, **kw: ([], []),
    )
    assert stage is not None
    # Dependencies retained as attributes for future tests to swap in
    assert callable(stage._has_actionable_signal)


def test_morning_research_stage_populates_ctx_on_success():
    """Stage.run(ctx) fills in macro_analysis / news_intel / analyses / earnings_results."""
    from src.models import MacroAnalysis, MacroReasoningChain, MacroPositionGuidance
    from src.agents.base import AgentResult

    ma = MacroAnalysis(
        reasoning_chain=MacroReasoningChain(
            volatility_analysis="a",
            yield_curve_analysis="b",
            monetary_policy_analysis="c",
            inflation_labor_credit="d",
            cross_signal_synthesis="e",
            sector_implications="f",
        ),
        regime="risk-on",
        confidence="high",
        equity_outlook="bullish",
        position_guidance=MacroPositionGuidance(
            target_invested_pct=70,
            cash_recommendation_pct=30,
            reasoning="y",
        ),
        summary="z",
    )
    agent_result = AgentResult(raw_text="{}", tokens_used=100, model="test", user_message="x")

    mock_config = MagicMock()
    mock_config.trading.universe = ["NVDA"]
    mock_config.trading.lookback_days = 30
    mock_config.llm.macro_analyst_model = "claude-opus-4-6"
    mock_config.llm.tech_analyst_model = "claude-opus-4-6"

    macro_agent = MagicMock()
    macro_agent.analyze.return_value = (ma, agent_result)

    market = MagicMock()
    market.get_ohlcv.return_value = []  # Skip NVDA → empty symbols_data → tech returns empty

    macro_store = MagicMock()
    macro_store.load_last_state.return_value = None
    news_store = MagicMock()
    news_store.load_macro_narrative.return_value = None

    stage = MorningResearchStage(
        config=mock_config,
        db=MagicMock(),
        market=market,
        macro=MagicMock(),
        news_provider=MagicMock(),
        news_store=news_store,
        macro_store=macro_store,
        tech_store=MagicMock(),
        earnings_provider=MagicMock(),
        macro_analyst=macro_agent,
        news_analyst=MagicMock(),
        tech_analyst=MagicMock(),
        earnings_analyst=MagicMock(),
        has_actionable_signal_fn=lambda *args, **kw: False,
        run_news_update_fn=lambda run_id, session: (None, None),
        load_earnings_analyses_fn=lambda run_id, session, ctx=None: ([], []),
    )

    ctx = RunContext.start("morning")
    ctx.positions = []
    result_ctx = stage.run(ctx)

    assert result_ctx.macro_analysis is not None
    assert result_ctx.macro_analysis.regime == "risk-on"
    assert result_ctx.data_status["macro"] == "ok"
    # News / earnings returned None / empty — should be handled gracefully
    assert result_ctx.news_intel is None
    assert result_ctx.analyses == []
    assert result_ctx.earnings_results == []


def _macro_stage_with_coverage(macro_analysis, coverage):
    """Shared builder for the confidence-override tests below: a
    MorningResearchStage whose macro leg returns `macro_analysis` and whose
    `macro.last_coverage` (what `src/pipeline_stages.py` actually reads,
    see ~line 1608) is `coverage` — a real `MacroCoverage`, not a MagicMock,
    so the coverage-based ok/partial/failed branches run for real instead of
    falling into the coverage-less test-double fallback."""
    from src.agents.base import AgentResult

    agent_result = AgentResult(raw_text="{}", tokens_used=100, model="test", user_message="x")
    macro_agent = MagicMock()
    macro_agent.analyze.return_value = (macro_analysis, agent_result)

    macro = MagicMock()
    macro.last_coverage = coverage

    market = MagicMock()
    market.get_ohlcv.return_value = []

    macro_store = MagicMock()
    macro_store.load_last_state.return_value = None
    news_store = MagicMock()
    news_store.load_macro_narrative.return_value = None

    mock_config = MagicMock()
    mock_config.trading.universe = ["NVDA"]
    mock_config.trading.lookback_days = 30
    mock_config.llm.macro_analyst_model = "claude-opus-4-6"
    mock_config.llm.tech_analyst_model = "claude-opus-4-6"

    return MorningResearchStage(
        config=mock_config,
        db=MagicMock(),
        market=market,
        macro=macro,
        news_provider=MagicMock(),
        news_store=news_store,
        macro_store=macro_store,
        tech_store=MagicMock(),
        earnings_provider=MagicMock(),
        macro_analyst=macro_agent,
        news_analyst=MagicMock(),
        tech_analyst=MagicMock(),
        earnings_analyst=MagicMock(),
        has_actionable_signal_fn=lambda *args, **kw: False,
        run_news_update_fn=lambda run_id, session: (None, None),
        load_earnings_analyses_fn=lambda run_id, session, ctx=None: ([], []),
    )


def _minimal_macro_analysis(confidence="high"):
    from src.models import MacroAnalysis, MacroReasoningChain, MacroPositionGuidance

    return MacroAnalysis(
        reasoning_chain=MacroReasoningChain(
            volatility_analysis="a",
            yield_curve_analysis="b",
            monetary_policy_analysis="c",
            inflation_labor_credit="d",
            cross_signal_synthesis="e",
            sector_implications="f",
        ),
        regime="risk-on",
        confidence=confidence,
        equity_outlook="bullish",
        position_guidance=MacroPositionGuidance(
            target_invested_pct=70,
            cash_recommendation_pct=30,
            reasoning="y",
        ),
        summary="z",
    )


def test_morning_research_stage_macro_low_self_reported_confidence_marks_status_low_confidence():
    """The confidence half of the honesty fix: full FRED coverage AND a
    clean parse (the one case coverage alone reads as 'ok'), but the
    model's own `confidence` field says 'low' — a coherent set of
    indicators the model itself isn't confident reading. Coverage can't
    catch this; it only counts whether FRED series came back, not whether
    the model made sense of them. Before this, that combination was
    indistinguishable from a clean, trustworthy 'ok' run anywhere
    data_status['macro'] is read. Must read as its own distinct status."""
    from src.data.macro import MacroCoverage

    ma = _minimal_macro_analysis(confidence="low")
    coverage = MacroCoverage(configured=9, succeeded=9, failed=[])
    stage = _macro_stage_with_coverage(ma, coverage)

    ctx = RunContext.start("morning")
    ctx.positions = []
    result_ctx = stage.run(ctx)

    assert result_ctx.data_status["macro"] == "low_confidence"
    assert result_ctx.data_status["macro"] != "ok"


def test_morning_research_stage_macro_high_confidence_full_coverage_stays_ok():
    """Control case: high self-reported confidence on full coverage must
    NOT be touched by the new override — only 'low' downgrades an
    otherwise-'ok' status."""
    from src.data.macro import MacroCoverage

    ma = _minimal_macro_analysis(confidence="high")
    coverage = MacroCoverage(configured=9, succeeded=9, failed=[])
    stage = _macro_stage_with_coverage(ma, coverage)

    ctx = RunContext.start("morning")
    ctx.positions = []
    result_ctx = stage.run(ctx)

    assert result_ctx.data_status["macro"] == "ok"


def test_morning_research_stage_macro_overdue_print_is_not_ok():
    """Full coverage, clean parse, confident model — and one FRED series
    sitting on a reading that should already have been superseded. That is
    the real staleness that survived the removal of the calendar-day gate
    (src/data/macro.py::SeriesFreshness): a publication or fetch failure,
    not normal release timing. Reporting it as "ok" would be the same
    "no data, but everything's fine!" gap every other seat's audit closed,
    so it gets its own status value on the existing `macro` key."""
    from datetime import date

    from src.data.macro import MacroCoverage, SeriesFreshness

    ma = _minimal_macro_analysis(confidence="high")
    coverage = MacroCoverage(
        configured=9,
        succeeded=9,
        failed=[],
        overdue=[
            SeriesFreshness(
                series_id="CPIAUCSL",
                status="overdue",
                latest_observation=date(2026, 5, 1),
                expected_next_by=date(2026, 7, 15),
                detail="a newer print was due by 2026-07-15",
            )
        ],
    )
    stage = _macro_stage_with_coverage(ma, coverage)

    ctx = RunContext.start("morning")
    ctx.positions = []
    result_ctx = stage.run(ctx)

    assert result_ctx.data_status["macro"] == "release_overdue"


def test_morning_research_stage_macro_coverage_failure_beats_overdue_print():
    """Precedence: a total FRED outage is still 'failed'. An overdue print
    is a narrower fact and must not mask the wider one."""
    from datetime import date

    from src.data.macro import MacroCoverage, SeriesFailure, SeriesFreshness

    ma = _minimal_macro_analysis(confidence="high")
    coverage = MacroCoverage(
        configured=9,
        succeeded=0,
        failed=[SeriesFailure(series_id=f"S{i}", reason="timed out") for i in range(9)],
        overdue=[
            SeriesFreshness(
                series_id="CPIAUCSL",
                status="overdue",
                latest_observation=date(2026, 5, 1),
            )
        ],
    )
    stage = _macro_stage_with_coverage(ma, coverage)

    ctx = RunContext.start("morning")
    ctx.positions = []
    result_ctx = stage.run(ctx)

    assert result_ctx.data_status["macro"] == "failed"


def test_morning_research_stage_macro_low_confidence_does_not_override_coverage_failure():
    """Coverage-driven failure must still win over a confidence check — a
    total FRED outage is 'failed' regardless of what the model's own
    confidence field says on all-None input."""
    from src.data.macro import MacroCoverage, SeriesFailure

    ma = _minimal_macro_analysis(confidence="low")
    coverage = MacroCoverage(
        configured=9,
        succeeded=0,
        failed=[SeriesFailure(series_id=f"S{i}", reason="timed out") for i in range(9)],
    )
    stage = _macro_stage_with_coverage(ma, coverage)

    ctx = RunContext.start("morning")
    ctx.positions = []
    result_ctx = stage.run(ctx)

    assert result_ctx.data_status["macro"] == "failed"


def test_morning_research_stage_records_admission_reason_without_collision():
    """A qualified external SEC candidate must not abort morning research.

    Admission details deliberately contain their own ``reason`` field.  The
    lifecycle event also has a canonical reason, so the two values must be
    stored under distinct keys instead of being passed twice to the recorder.
    """
    from types import SimpleNamespace

    from src.agents.base import AgentResult

    config = SimpleNamespace(
        trading=SimpleNamespace(universe=["SPY"], lookback_days=30),
        smart_money=SimpleNamespace(enabled=True),
    )
    market = MagicMock()
    market.get_ohlcv.return_value = []
    macro = MagicMock()
    macro.get_macro_summary.return_value = {}
    macro_analyst = MagicMock()
    macro_analyst.analyze.return_value = (
        None,
        AgentResult(raw_text="{}", tokens_used=0, model="test", user_message="x"),
    )
    macro_store = MagicMock()
    macro_store.load_last_state.return_value = None
    news_store = MagicMock()
    news_store.load_macro_narrative.return_value = None
    smart_money_provider = MagicMock()
    smart_money_provider.fetch.return_value = ([SimpleNamespace(symbol="RSG")], None)
    admission = {
        "temporary": True,
        "reason": "material_sec_form4_purchase",
        "transaction_value_usd": 87_980_000.0,
    }
    db = MagicMock()

    stage = MorningResearchStage(
        config=config,
        db=db,
        market=market,
        macro=macro,
        news_provider=MagicMock(),
        news_store=news_store,
        macro_store=macro_store,
        tech_store=MagicMock(),
        earnings_provider=MagicMock(),
        macro_analyst=macro_analyst,
        news_analyst=MagicMock(),
        tech_analyst=MagicMock(),
        earnings_analyst=MagicMock(),
        smart_money_provider=smart_money_provider,
        smart_money_analyst=None,
        admit_smart_money_candidates_fn=lambda _observations: (
            {"RSG"},
            {"RSG": admission},
        ),
        has_actionable_signal_fn=lambda *args, **kwargs: False,
        run_news_update_fn=lambda *args, **kwargs: (None, None),
        load_earnings_analyses_fn=lambda *args, **kwargs: ([], []),
    )
    ctx = RunContext.start("morning")
    ctx.positions = []

    result_ctx = stage.run(ctx)

    assert result_ctx.admitted_symbols == {"RSG"}
    assert [call.args[0] for call in market.get_ohlcv.call_args_list[:2]] == [
        "RSG",
        "SPY",
    ]
    event_payloads = [
        json.loads(call.kwargs["evidence_json"])
        for call in db.insert_specialist_evidence.call_args_list
        if call.kwargs["agent_name"] == "pipeline"
        and call.kwargs["kind"] == "pipeline_event"
        and call.kwargs["symbol"] == "RSG"
    ]
    admission_event = next(payload for payload in event_payloads if payload["outcome"] == "admitted")
    assert admission_event["reason"] == "smart_money_form4_admission"
    assert admission_event["admission_reason"] == "material_sec_form4_purchase"
    assert admission_event["transaction_value_usd"] == 87_980_000.0


def test_morning_research_stage_smart_money_truncated_marks_status_truncated():
    """2026-09-02: smart_money_analyst_max_tokens was measured against real
    production truncations (finish_reason=length). A truncated response can
    still parse to syntactically valid-but-incomplete JSON, which must not
    be silently recorded as "ok" (clean run) or "empty" (no signal) —
    those are both currently-reachable branches in this stage's status
    logic. It must land as its own "truncated" status so the notifier's
    degraded banner picks it up.
    """
    from types import SimpleNamespace

    from src.agents.base import AgentResult

    config = SimpleNamespace(
        trading=SimpleNamespace(universe=["SPY"], lookback_days=30),
        smart_money=SimpleNamespace(enabled=True),
    )
    market = MagicMock()
    market.get_ohlcv.return_value = []
    macro = MagicMock()
    macro.get_macro_summary.return_value = {}
    macro_analyst = MagicMock()
    macro_analyst.analyze.return_value = (
        None,
        AgentResult(raw_text="{}", tokens_used=0, model="test", user_message="x"),
    )
    macro_store = MagicMock()
    macro_store.load_last_state.return_value = None
    news_store = MagicMock()
    news_store.load_macro_narrative.return_value = None

    smart_money_provider = MagicMock()
    smart_money_provider.fetch.return_value = ([SimpleNamespace(symbol="RSG")], None)

    truncated_result = AgentResult(
        raw_text='{"findings":[]}',
        tokens_used=3000,
        model="test",
        user_message="x",
        output_tokens=3000,
        finish_reason="length",
        truncated=True,
    )
    smart_money_analyst = MagicMock()
    # Findings list is empty (as a truncated call would plausibly look
    # like "no signal") and there's no analysis_error — the parse
    # succeeded on the incomplete-but-valid JSON.
    smart_money_analyst.analyze_with_gaps.return_value = ([], truncated_result, None, [])

    db = MagicMock()

    stage = MorningResearchStage(
        config=config,
        db=db,
        market=market,
        macro=macro,
        news_provider=MagicMock(),
        news_store=news_store,
        macro_store=macro_store,
        tech_store=MagicMock(),
        earnings_provider=MagicMock(),
        macro_analyst=macro_analyst,
        news_analyst=MagicMock(),
        tech_analyst=MagicMock(),
        earnings_analyst=MagicMock(),
        smart_money_provider=smart_money_provider,
        smart_money_analyst=smart_money_analyst,
        admit_smart_money_candidates_fn=lambda _observations: (set(), {}),
        has_actionable_signal_fn=lambda *args, **kwargs: False,
        run_news_update_fn=lambda *args, **kwargs: (None, None),
        load_earnings_analyses_fn=lambda *args, **kwargs: ([], []),
    )
    ctx = RunContext.start("morning")
    ctx.positions = []

    result_ctx = stage.run(ctx)

    assert result_ctx.data_status["smart_money"] == "truncated"


def _smart_money_morning_stage(coverage, findings):
    """A morning stage whose only interesting seat is smart money."""
    from types import SimpleNamespace

    from src.agents.base import AgentResult

    config = SimpleNamespace(
        trading=SimpleNamespace(universe=["SPY"], lookback_days=30),
        smart_money=SimpleNamespace(enabled=True),
    )
    market = MagicMock()
    market.get_ohlcv.return_value = []
    macro = MagicMock()
    macro.get_macro_summary.return_value = {}
    macro_analyst = MagicMock()
    macro_analyst.analyze.return_value = (
        None,
        AgentResult(raw_text="{}", tokens_used=0, model="test", user_message="x"),
    )
    macro_store = MagicMock()
    macro_store.load_last_state.return_value = None
    news_store = MagicMock()
    news_store.load_macro_narrative.return_value = None
    smart_money_provider = MagicMock()
    smart_money_provider.fetch.return_value = ([SimpleNamespace(symbol="RSG")], None)
    smart_money_provider.form4_coverage.return_value = coverage
    smart_money_analyst = MagicMock()
    smart_money_analyst.analyze_with_gaps.return_value = (
        findings,
        AgentResult(raw_text='{"findings":[]}', tokens_used=1, model="test", user_message="x"),
        None,
        [],
    )
    stage = MorningResearchStage(
        config=config,
        db=MagicMock(),
        market=market,
        macro=macro,
        news_provider=MagicMock(),
        news_store=news_store,
        macro_store=macro_store,
        tech_store=MagicMock(),
        earnings_provider=MagicMock(),
        macro_analyst=macro_analyst,
        news_analyst=MagicMock(),
        tech_analyst=MagicMock(),
        earnings_analyst=MagicMock(),
        smart_money_provider=smart_money_provider,
        smart_money_analyst=smart_money_analyst,
        admit_smart_money_candidates_fn=lambda _observations: (set(), {}),
        has_actionable_signal_fn=lambda *args, **kwargs: False,
        run_news_update_fn=lambda *args, **kwargs: (None, None),
        load_earnings_analyses_fn=lambda *args, **kwargs: ([], []),
    )
    ctx = RunContext.start("morning")
    ctx.positions = []
    return stage.run(ctx)


def test_morning_smart_money_is_partial_while_watched_names_are_unread():
    """2026-09-19. The seat's answer covers only names whose Form 4s have
    all been read. With unread filings left on a watched name, a clean run
    must not be recorded as `ok` — nor as `empty`, which would claim "no
    material insider activity" on names nobody finished reading."""
    verified = {
        "known": True,
        "verified": True,
        "reasons": [],
        "edgar_total": 900,
        "enumerated": 900,
        "ratio": 1.0,
        "days_queried": 15,
        "days_in_window": 15,
        "days_with_total": 15,
    }
    unread = {
        "known": True,
        "as_of": "2026-09-21",
        "watched": 82,
        "read_through": 60,
        "unread": ["WMT"],
        "edgar": dict(verified),
    }
    assert _smart_money_morning_stage(unread, []).data_status["smart_money"] == "partial"
    assert (
        _smart_money_morning_stage(
            {"known": False, "as_of": "", "watched": 0, "read_through": 0, "unread": [], "edgar": dict(verified)},
            [],
        ).data_status["smart_money"]
        == "partial"
    )

    complete = {
        "known": True,
        "as_of": "2026-09-21",
        "watched": 82,
        "read_through": 82,
        "unread": [],
        "edgar": dict(verified),
    }
    assert _smart_money_morning_stage(complete, []).data_status["smart_money"] == "ok"


def test_morning_smart_money_market_wide_blind_is_its_own_word():
    """2026-09-23. Between 2026-09-18 and 2026-09-23 the market-wide Form 4
    pass read ZERO filings on every run and the seat reported `partial` —
    which is also what it reports on any ordinary residue, so five sessions
    of total external-insider blindness were indistinguishable from a normal
    morning. The state gets its own word, so the standing DATA QUALITY ALERT
    can say what actually happened.
    """
    verified = {
        "known": True,
        "verified": True,
        "reasons": [],
        "edgar_total": 900,
        "enumerated": 900,
        "ratio": 1.0,
        "days_queried": 15,
        "days_in_window": 15,
        "days_with_total": 15,
    }
    complete = {
        "known": True,
        "as_of": "2026-09-21",
        "watched": 82,
        "read_through": 82,
        "unread": [],
        "edgar": dict(verified),
    }

    blind = {**complete, "market_wide_blind": True, "market_wide_read": 0, "market_wide_pending": 21217}
    assert _smart_money_morning_stage(blind, []).data_status["smart_money"] == "market_wide_blind"

    # It wins over `partial` — a blind pass with unread watched names is
    # still blind, and `partial` is the quieter of the two.
    blind_and_unread = {**blind, "read_through": 60, "unread": ["WMT"]}
    assert (
        _smart_money_morning_stage(
            blind_and_unread,
            [],
        ).data_status["smart_money"]
        == "market_wide_blind"
    )

    # And a pass that read normally is untouched.
    assert (
        _smart_money_morning_stage(
            {**complete, "market_wide_blind": False, "market_wide_read": 1000, "market_wide_pending": 21217},
            [],
        ).data_status["smart_money"]
        == "ok"
    )


def test_morning_smart_money_is_partial_when_edgar_coverage_is_unverified():
    """Board item 126. Every watched name read through, no provider error,
    no unread filings — and EDGAR's own count of what was filed could not be
    read. That is the desk being unable to tell a quiet insider day from a
    broken fetch, and it must not be reported as a clean run. It is
    `partial`, an EXISTING status word `src/evidence_gate.py` already
    classifies; a new word would be read as unknown and treated as a loss.
    """
    complete_names = {"known": True, "as_of": "2026-09-21", "watched": 82, "read_through": 82, "unread": []}
    unverified = {
        "known": True,
        "verified": False,
        "reasons": ["edgar_total_unreadable"],
        "edgar_total": 0,
        "enumerated": 0,
        "ratio": None,
        "days_queried": 15,
        "days_in_window": 15,
        "days_with_total": 3,
    }
    assert (
        _smart_money_morning_stage(
            {**complete_names, "edgar": unverified},
            [],
        ).data_status["smart_money"]
        == "partial"
    )

    # A coverage record from before this shipped carries no `edgar` key at
    # all. Never-recorded is not evidence of a clean fetch.
    assert (
        _smart_money_morning_stage(
            dict(complete_names),
            [],
        ).data_status["smart_money"]
        == "partial"
    )


def test_morning_smart_money_quiet_day_with_verified_coverage_stays_clean():
    """The other half of board item 126, and the one it warns about loudest:
    EDGAR said nothing was filed, the scan confirmed it read that count, and
    the analyst found nothing. A genuinely quiet insider day must still read
    clean — `src/evidence_gate.py` treats degraded seats as a loss and on
    2026-09-16 that made Risk veto a whole intraday plan."""
    quiet = {
        "known": True,
        "verified": True,
        "reasons": [],
        "edgar_total": 0,
        "enumerated": 0,
        "ratio": None,
        "days_queried": 15,
        "days_in_window": 15,
        "days_with_total": 15,
    }
    status = _smart_money_morning_stage(
        {"known": True, "as_of": "2026-09-21", "watched": 82, "read_through": 82, "unread": [], "edgar": quiet},
        [],
    ).data_status["smart_money"]
    assert status == "ok", status

    # ...and a budget-bounded scan is likewise not a failure: the cap and
    # the deadline are the desk's own choices, reported through the backlog
    # counts, and must not flip the seat every morning.
    bounded = {
        "known": True,
        "verified": True,
        "reasons": ["days_not_queried", "scan_cap_reached"],
        "edgar_total": 900,
        "enumerated": 900,
        "ratio": 1.0,
        "days_queried": 3,
        "days_in_window": 15,
        "days_with_total": 3,
    }
    assert (
        _smart_money_morning_stage(
            {"known": True, "as_of": "2026-09-21", "watched": 82, "read_through": 82, "unread": [], "edgar": bounded},
            [],
        ).data_status["smart_money"]
        == "ok"
    )


@patch("src.stage_morning_research.compute_indicators")
def test_morning_research_stage_tech_partial_batch_marks_status_partial(mock_compute_indicators):
    """2026-08-19 Tech batch-response symbol-loss fix, pipeline-level: when
    tech_analyst.analyze_batch comes back with some symbols unresolved
    (None), MorningResearchStage must (a) still populate ctx.analyses with
    the symbols that DID resolve — never crash on the None entries when
    updating tech_store/computing ages — and (b) set
    data_status['tech'] = 'partial' rather than a plain 'ok' that hides
    the loss. This is the exact production incident (1/10 symbols parsed)
    reaching the pipeline layer, not just the agent layer."""
    mock_compute_indicators.return_value = MagicMock()

    mock_config = MagicMock()
    mock_config.trading.universe = ["AAPL", "MSFT"]
    mock_config.trading.lookback_days = 30

    market = MagicMock()
    market.get_ohlcv.return_value = [MagicMock()]

    tech_analyst = MagicMock()
    from src.models import TechAnalysisResult, TechReasoningChain

    resolved = TechAnalysisResult(
        symbol="AAPL",
        rating="buy",
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
    # AAPL resolved, MSFT explicitly failed even after tech_analyst's own
    # retry — the sentinel this whole fix introduces.
    tech_analyst.analyze_batch.return_value = (
        {"AAPL": resolved, "MSFT": None},
        MagicMock(
            user_message="m", raw_text="{}", tokens_used=1, input_tokens=1, output_tokens=1, cost_usd=0.0, model="t"
        ),
    )

    macro_store = MagicMock()
    macro_store.load_last_state.return_value = None
    news_store = MagicMock()
    news_store.load_macro_narrative.return_value = None
    macro_agent = MagicMock()
    macro_agent.analyze.return_value = (
        None,
        MagicMock(
            user_message="m",
            raw_text="{}",
            tokens_used=1,
            model="t",
            input_tokens=1,
            output_tokens=1,
            cost_usd=0.0,
        ),
    )
    tech_store = MagicMock()
    tech_store.load.return_value = {}
    tech_store.compute_ages.return_value = {}

    stage = MorningResearchStage(
        config=mock_config,
        db=MagicMock(),
        market=market,
        macro=MagicMock(),
        news_provider=MagicMock(),
        news_store=news_store,
        macro_store=macro_store,
        tech_store=tech_store,
        earnings_provider=MagicMock(),
        macro_analyst=macro_agent,
        news_analyst=MagicMock(),
        tech_analyst=tech_analyst,
        earnings_analyst=MagicMock(),
        has_actionable_signal_fn=lambda *args, **kw: True,
        run_news_update_fn=lambda run_id, session: (None, None),
        load_earnings_analyses_fn=lambda run_id, session, ctx=None: ([], []),
    )

    ctx = RunContext.start("morning")
    ctx.positions = []
    result_ctx = stage.run(ctx)

    # AAPL (resolved) is a real analysis; MSFT (None) never becomes a fake
    # TechAnalysisResult and never crashes tech_store.update/compute_ages.
    assert [a.symbol for a in result_ctx.analyses] == ["AAPL"]
    assert result_ctx.data_status["tech"] == "partial"
    tech_store.update.assert_called_once_with([resolved])


def _tech_stage_for_conviction_test(analyses_map):
    """Shared MorningResearchStage wiring for the low-conviction tests below
    — every symbol resolves (no None entries), only `conviction` varies."""
    mock_config = MagicMock()
    mock_config.trading.universe = ["AAPL", "MSFT"]
    mock_config.trading.lookback_days = 30

    market = MagicMock()
    market.get_ohlcv.return_value = [MagicMock()]

    tech_analyst = MagicMock()
    tech_analyst.analyze_batch.return_value = (
        analyses_map,
        MagicMock(
            user_message="m", raw_text="{}", tokens_used=1, input_tokens=1, output_tokens=1, cost_usd=0.0, model="t"
        ),
    )

    macro_store = MagicMock()
    macro_store.load_last_state.return_value = None
    news_store = MagicMock()
    news_store.load_macro_narrative.return_value = None
    macro_agent = MagicMock()
    macro_agent.analyze.return_value = (
        None,
        MagicMock(
            user_message="m",
            raw_text="{}",
            tokens_used=1,
            model="t",
            input_tokens=1,
            output_tokens=1,
            cost_usd=0.0,
        ),
    )
    tech_store = MagicMock()
    tech_store.load.return_value = {}
    tech_store.compute_ages.return_value = {}

    return MorningResearchStage(
        config=mock_config,
        db=MagicMock(),
        market=market,
        macro=MagicMock(),
        news_provider=MagicMock(),
        news_store=news_store,
        macro_store=macro_store,
        tech_store=tech_store,
        earnings_provider=MagicMock(),
        macro_analyst=macro_agent,
        news_analyst=MagicMock(),
        tech_analyst=tech_analyst,
        earnings_analyst=MagicMock(),
        has_actionable_signal_fn=lambda *args, **kw: True,
        run_news_update_fn=lambda run_id, session: (None, None),
        load_earnings_analyses_fn=lambda run_id, session, ctx=None: ([], []),
    )


@patch("src.stage_morning_research.compute_indicators")
def test_morning_research_stage_tech_full_batch_low_conviction_marks_low_confidence(
    mock_compute_indicators,
):
    """2026-09-04 data-honesty fix: a batch where every symbol resolves
    (data_status would otherwise be a plain 'ok') must not hide a read the
    model itself flagged as low-conviction. One low-conviction read among
    otherwise-resolved symbols downgrades data_status['tech'] to the new
    'low_confidence' value — real, visible, but distinct from 'partial'/
    'failed' since the content is present, just self-reportedly shaky."""
    mock_compute_indicators.return_value = MagicMock()

    from src.models import TechAnalysisResult, TechReasoningChain

    def _mk(symbol, conviction):
        return TechAnalysisResult(
            symbol=symbol,
            rating="buy",
            conviction=conviction,
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

    analyses_map = {"AAPL": _mk("AAPL", "medium"), "MSFT": _mk("MSFT", "low")}
    stage = _tech_stage_for_conviction_test(analyses_map)

    ctx = RunContext.start("morning")
    ctx.positions = []
    result_ctx = stage.run(ctx)

    # Both symbols resolved — this is NOT a partial/failed batch.
    assert {a.symbol for a in result_ctx.analyses} == {"AAPL", "MSFT"}
    assert result_ctx.data_status["tech"] == "low_confidence"


@patch("src.stage_morning_research.compute_indicators")
def test_morning_research_stage_tech_full_batch_high_conviction_stays_ok(
    mock_compute_indicators,
):
    """Control case: a fully-resolved batch with no low-conviction reads is
    unaffected by the new check and still reports plain 'ok'."""
    mock_compute_indicators.return_value = MagicMock()

    from src.models import TechAnalysisResult, TechReasoningChain

    def _mk(symbol, conviction):
        return TechAnalysisResult(
            symbol=symbol,
            rating="buy",
            conviction=conviction,
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

    analyses_map = {"AAPL": _mk("AAPL", "high"), "MSFT": _mk("MSFT", "medium")}
    stage = _tech_stage_for_conviction_test(analyses_map)

    ctx = RunContext.start("morning")
    ctx.positions = []
    result_ctx = stage.run(ctx)

    assert {a.symbol for a in result_ctx.analyses} == {"AAPL", "MSFT"}
    assert result_ctx.data_status["tech"] == "ok"


@patch("src.stage_morning_research.compute_indicators")
def test_a_neutral_only_low_confidence_batch_does_not_log_research_degraded(
    mock_compute_indicators,
    caplog,
):
    """2026-09-19 log-health false-alarm fix.

    A `neutral` rating carries no view at all, so the model's own
    conviction on it is structurally always 'low' — that is not a real
    research degradation, it is the shape of a no-view read. Before this
    fix, `data_status['tech'] == 'low_confidence'` (set whenever ANY
    resolved read is low-conviction, regardless of rating) made the
    "Morning research degraded: tech" ERROR line fire every single morning,
    and the log-health report then told the owner a research desk "could
    not be reached" on a morning where nothing was actually wrong.

    `data_status['tech']` itself is UNCHANGED (still 'low_confidence',
    asserted below) — RiskStage's `data_degraded` advisory and the Risk
    Manager's prompt see exactly what they saw before. Only the ERROR
    summary line is corrected."""
    import logging

    mock_compute_indicators.return_value = MagicMock()

    from src.models import TechAnalysisResult, TechReasoningChain

    def _mk(symbol, rating, conviction):
        kwargs = dict(
            symbol=symbol,
            rating=rating,
            conviction=conviction,
            reasoning_chain=TechReasoningChain(
                trend="x",
                momentum="x",
                volatility="x",
                volume="x",
                support_resistance="x",
            ),
            reasoning="test",
        )
        if rating != "neutral":
            kwargs.update(
                entry_price=100.0,
                stop_loss=95.0,
                reference_target=110.0,
                support_levels=[95.0],
                resistance_levels=[110.0],
                setup_type="range",
                expected_horizon_sessions=10,
                thesis_invalid_if="closes below support",
            )
        return TechAnalysisResult(**kwargs)

    analyses_map = {
        "AAPL": _mk("AAPL", "buy", "medium"),
        "MSFT": _mk("MSFT", "neutral", "low"),
    }
    stage = _tech_stage_for_conviction_test(analyses_map)

    ctx = RunContext.start("morning")
    ctx.positions = []
    with caplog.at_level(logging.WARNING, logger="src.pipeline_stages"):
        result_ctx = stage.run(ctx)

    # data_status is untouched — this is a reporting fix, not a gate change.
    assert result_ctx.data_status["tech"] == "low_confidence"
    degraded_lines = [r.getMessage() for r in caplog.records if "Morning research degraded" in r.getMessage()]
    assert degraded_lines, "test setup sanity: other mocked seats are expected to degrade"
    assert not any("tech" in line.split("|", 1)[0] for line in degraded_lines), (
        f"tech must not be named as degraded on a neutral-only morning: {degraded_lines}"
    )


@patch("src.stage_morning_research.compute_indicators")
def test_an_actionable_low_confidence_batch_still_logs_research_degraded(
    mock_compute_indicators,
    caplog,
):
    """Contrast case for the fix above: a real BUY/SELL read the model
    itself flagged as low-conviction is genuine degradation and must still
    reach the operator log — only the neutral-driven false alarm is
    suppressed."""
    import logging

    mock_compute_indicators.return_value = MagicMock()

    from src.models import TechAnalysisResult, TechReasoningChain

    def _mk(symbol, conviction):
        return TechAnalysisResult(
            symbol=symbol,
            rating="buy",
            conviction=conviction,
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

    analyses_map = {"AAPL": _mk("AAPL", "medium"), "MSFT": _mk("MSFT", "low")}
    stage = _tech_stage_for_conviction_test(analyses_map)

    ctx = RunContext.start("morning")
    ctx.positions = []
    with caplog.at_level(logging.WARNING, logger="src.pipeline_stages"):
        result_ctx = stage.run(ctx)

    assert result_ctx.data_status["tech"] == "low_confidence"
    degraded_lines = [r.getMessage() for r in caplog.records if "Morning research degraded" in r.getMessage()]
    assert any("tech" in line.split("|", 1)[0] for line in degraded_lines), (
        f"a real low-conviction BUY/SELL read must still be reported: {degraded_lines}"
    )


def _minimal_news_report(confidence="medium"):
    from src.models import MacroNarrative, NewsIntelligenceReport

    return NewsIntelligenceReport(
        macro_narrative=MacroNarrative(
            last_updated="2026-08-28",
            era_themes=["AI capex"],
            current_regime="risk-on",
        ),
        pm_briefing="Quiet tape.",
        market_sentiment="neutral",
        confidence=confidence,
    )


def _news_coverage_stage(run_news_update_fn):
    """Minimal MorningResearchStage wiring shared by the coverage tests
    below — only the news branch varies between them."""
    mock_config = MagicMock()
    mock_config.trading.universe = ["AAPL"]
    mock_config.trading.lookback_days = 30

    market = MagicMock()
    market.get_ohlcv.return_value = []  # skip tech entirely, not under test here

    macro_store = MagicMock()
    macro_store.load_last_state.return_value = None
    news_store = MagicMock()
    news_store.load_macro_narrative.return_value = None
    macro_agent = MagicMock()
    macro_agent.analyze.return_value = (
        None,
        MagicMock(
            user_message="m",
            raw_text="{}",
            tokens_used=1,
            model="t",
            input_tokens=1,
            output_tokens=1,
            cost_usd=0.0,
        ),
    )

    return MorningResearchStage(
        config=mock_config,
        db=MagicMock(),
        market=market,
        macro=MagicMock(),
        news_provider=MagicMock(),
        news_store=news_store,
        macro_store=macro_store,
        tech_store=MagicMock(),
        earnings_provider=MagicMock(),
        macro_analyst=macro_agent,
        news_analyst=MagicMock(),
        tech_analyst=MagicMock(),
        earnings_analyst=MagicMock(),
        has_actionable_signal_fn=lambda *args, **kw: False,
        run_news_update_fn=run_news_update_fn,
        load_earnings_analyses_fn=lambda run_id, session, ctx=None: ([], []),
    )


def test_morning_research_stage_news_partial_coverage_marks_status_partial():
    """2026-08-28 coverage fix, pipeline level: two feeds down (Reuters 404,
    AP 403) out of nine configured, but the seven survivors were enough for
    the analyst to produce a valid report. Before the fix, data_status['news']
    was 'ok' purely because the LLM call parsed — this asserts it is now
    'partial', which is the whole point of tracking coverage at all."""
    report = _minimal_news_report()
    coverage = NewsCoverage(
        configured=9,
        succeeded=7,
        failed=[
            FeedFailure(name="Reuters Business", reason="HTTP Error 404: Not Found"),
            FeedFailure(name="AP Business", reason="HTTP Error 403: Forbidden"),
        ],
    )
    stage = _news_coverage_stage(lambda run_id, session: (report, coverage))

    ctx = RunContext.start("morning")
    ctx.positions = []
    result_ctx = stage.run(ctx)

    assert result_ctx.news_intel is report
    assert result_ctx.data_status["news"] == "partial"
    assert result_ctx.data_status["news"] != "ok"


def test_morning_research_stage_news_total_feed_failure_marks_status_failed_even_when_report_parses():
    """The exact scenario the fix targets: ALL configured feeds fail (a
    total wire outage), yet the analyst still returns a technically-valid
    report (e.g. 'no fresh headlines, neutral sentiment'). Coverage failure
    must dominate — this must read as 'failed', never 'ok', regardless of
    whether the LLM call itself succeeded on empty input. This is the
    'coverage is NOT reported complete' assertion at the pipeline layer."""
    report = _minimal_news_report()
    coverage = NewsCoverage(
        configured=9,
        succeeded=0,
        failed=[FeedFailure(name=f"Feed {i}", reason="timed out") for i in range(9)],
    )
    stage = _news_coverage_stage(lambda run_id, session: (report, coverage))

    ctx = RunContext.start("morning")
    ctx.positions = []
    result_ctx = stage.run(ctx)

    assert result_ctx.data_status["news"] == "failed"
    assert result_ctx.data_status["news"] != "ok"


def test_morning_research_stage_news_full_coverage_marks_status_ok():
    """Control case: 9/9 feeds returned data and the report parsed — this
    is the one scenario that legitimately reads as 'ok'."""
    report = _minimal_news_report()
    coverage = NewsCoverage(configured=9, succeeded=9, failed=[])
    stage = _news_coverage_stage(lambda run_id, session: (report, coverage))

    ctx = RunContext.start("morning")
    ctx.positions = []
    result_ctx = stage.run(ctx)

    assert result_ctx.data_status["news"] == "ok"


# === Earnings content-honesty (2026-09-04) ===
#
# Real incident this closes: `data_status["earnings"]` used to be "ok"
# whenever `earnings_future.result()` returned without raising, with zero
# check of what the returned `earnings_results` actually contained — the
# exact shape of a real production incident where 12/67 filings once came
# back schema-valid with ZERO extracted figures. See
# `_classify_earnings_status` in src/pipeline_stages.py.


def _valid_earnings_analysis(symbol="AAPL", **field_overrides) -> dict:
    """A schema-valid `EarningsAnalysis.model_dump()` with real figures in
    every checked field — the clean control case. Tests override just the
    fields they care about via dotted-path shortcuts below."""
    from src.models import (
        EarningsAnalysis,
        EarningsBalanceSheet,
        EarningsCashFlow,
        EarningsInvestmentImplications,
        EarningsProfitability,
        EarningsReasoningChain,
        EarningsRevenue,
    )

    analysis = EarningsAnalysis(
        symbol=symbol,
        form_type="10-Q",
        filing_date="2026-09-01",
        revenue=EarningsRevenue(total="$50B", yoy_growth="12%"),
        profitability=EarningsProfitability(
            gross_margin="45%",
            operating_margin="20%",
            net_income="$5B",
            eps="$2.50",
        ),
        cash_flow=EarningsCashFlow(
            operating_cf="$8B",
            free_cf="$6B",
            capex="$2B",
        ),
        balance_sheet=EarningsBalanceSheet(
            cash_and_equivalents="$10B",
            total_debt="$0",
            assessment="strong balance sheet",
        ),
        guidance="Raised FY guidance.",
        investment_implications=EarningsInvestmentImplications(
            sentiment="bullish",
            conviction="medium",
            reasoning_chain=EarningsReasoningChain(
                fundamental_quality="x",
                growth_trajectory="x",
                strategic_risks="x",
                management_execution="x",
                valuation_context="x",
            ),
            key_thesis="Strong quarter.",
        ),
        data_quality="All figures sourced directly from the filing.",
    )
    dumped = analysis.model_dump()
    for dotted_path, value in field_overrides.items():
        parts = dotted_path.split(".")
        target = dumped
        for part in parts[:-1]:
            target = target[part]
        target[parts[-1]] = value
    return dumped


def _empty_earnings_analysis(symbol="AAPL", **field_overrides) -> dict:
    """A schema-valid `EarningsAnalysis.model_dump()` that structurally has
    NO real figures anywhere — every checked field left at its
    'not disclosed' default. Reproduces the real 12/67-zero-figures
    incident: the LLM call succeeds, the schema validates, but nothing
    usable came back."""
    return _valid_earnings_analysis(
        symbol=symbol,
        **{
            "revenue.total": "not disclosed",
            "revenue.yoy_growth": "not disclosed",
            "profitability.gross_margin": "not disclosed",
            "profitability.operating_margin": "not disclosed",
            "profitability.net_income": "not disclosed",
            "profitability.eps": "not disclosed",
            "cash_flow.operating_cf": "not disclosed",
            "cash_flow.free_cf": "not disclosed",
            "cash_flow.capex": "not disclosed",
            "balance_sheet.cash_and_equivalents": "not disclosed",
            "balance_sheet.total_debt": "not disclosed",
            **field_overrides,
        },
    )


def _earnings_stage_for(load_earnings_analyses_fn):
    """Minimal MorningResearchStage wiring shared by the earnings
    content-honesty tests below — only the earnings branch varies."""
    mock_config = MagicMock()
    mock_config.trading.universe = ["AAPL"]
    mock_config.trading.lookback_days = 30

    market = MagicMock()
    market.get_ohlcv.return_value = []  # skip tech entirely, not under test here

    macro_store = MagicMock()
    macro_store.load_last_state.return_value = None
    news_store = MagicMock()
    news_store.load_macro_narrative.return_value = None
    macro_agent = MagicMock()
    macro_agent.analyze.return_value = (
        None,
        MagicMock(
            user_message="m",
            raw_text="{}",
            tokens_used=1,
            model="t",
            input_tokens=1,
            output_tokens=1,
            cost_usd=0.0,
        ),
    )

    return MorningResearchStage(
        config=mock_config,
        db=MagicMock(),
        market=market,
        macro=MagicMock(),
        news_provider=MagicMock(),
        news_store=news_store,
        macro_store=macro_store,
        tech_store=MagicMock(),
        earnings_provider=MagicMock(),
        macro_analyst=macro_agent,
        news_analyst=MagicMock(),
        tech_analyst=MagicMock(),
        earnings_analyst=MagicMock(),
        has_actionable_signal_fn=lambda *args, **kw: False,
        run_news_update_fn=lambda run_id, session: (None, None),
        load_earnings_analyses_fn=load_earnings_analyses_fn,
    )


def test_earnings_status_ok_when_no_new_filing_today():
    """The ordinary, most-common day: no filings at all. This is legitimately
    benign, not a failure — must stay 'ok', never read as 'empty' or worse."""
    stage = _earnings_stage_for(lambda run_id, session, ctx=None: ([], []))

    ctx = RunContext.start("morning")
    ctx.positions = []
    result_ctx = stage.run(ctx)

    assert result_ctx.data_status["earnings"] == "ok"


def test_earnings_status_ok_when_filing_has_real_figures():
    """Control case: a filing was analyzed and the structured fields carry
    real content — clean 'ok', matching the pre-fix behavior for the
    genuinely-good case."""
    results = [{"symbol": "AAPL", "analysis": _valid_earnings_analysis(), "is_new": True}]
    stage = _earnings_stage_for(lambda run_id, session, ctx=None: ([], results))

    ctx = RunContext.start("morning")
    ctx.positions = []
    result_ctx = stage.run(ctx)

    assert result_ctx.data_status["earnings"] == "ok"


def test_earnings_status_content_missing_when_structured_fields_all_empty():
    """The real incident, reproduced: the future resolves without raising and
    the schema validates, but every checked figure is the 'not disclosed'
    sentinel — no real content was actually extracted. Before this fix,
    data_status['earnings'] would have read 'ok'. Must alert (excluded from
    the notifier's ('ok', 'empty') non-alerting set), so it must NOT be
    literally 'empty'."""
    results = [{"symbol": "AAPL", "analysis": _empty_earnings_analysis(), "is_new": True}]
    stage = _earnings_stage_for(lambda run_id, session, ctx=None: ([], results))

    ctx = RunContext.start("morning")
    ctx.positions = []
    result_ctx = stage.run(ctx)

    assert result_ctx.data_status["earnings"] == "content_missing"
    assert result_ctx.data_status["earnings"] not in ("ok", "empty")


def test_earnings_status_content_missing_when_self_reported_data_quality_flags_problem():
    """Second real bug: the structured fields technically parse (real
    figures present) but the LLM's own `data_quality` self-report says it
    could not actually get the data. The owner's own framing: a seat must
    never claim 'ok' while its own free-text field says something is wrong.
    This must downgrade status even though structured content looks fine."""
    results = [
        {
            "symbol": "AAPL",
            "analysis": _valid_earnings_analysis(
                data_quality="Unable to extract segment detail; filing incomplete in the excerpt provided.",
            ),
            "is_new": True,
        }
    ]
    stage = _earnings_stage_for(lambda run_id, session, ctx=None: ([], results))

    ctx = RunContext.start("morning")
    ctx.positions = []
    result_ctx = stage.run(ctx)

    assert result_ctx.data_status["earnings"] == "content_missing"


def test_earnings_status_partial_when_some_filings_good_some_not():
    """Mixed batch: one filing genuinely has figures, another came back
    content-empty. Mirrors tech's 'partial' semantics for a mixed batch —
    not blanket 'ok', not blanket failure either."""
    results = [
        {"symbol": "AAPL", "analysis": _valid_earnings_analysis(symbol="AAPL"), "is_new": True},
        {"symbol": "MSFT", "analysis": _empty_earnings_analysis(symbol="MSFT"), "is_new": True},
    ]
    stage = _earnings_stage_for(lambda run_id, session, ctx=None: ([], results))

    ctx = RunContext.start("morning")
    ctx.positions = []
    result_ctx = stage.run(ctx)

    assert result_ctx.data_status["earnings"] == "partial"


def test_earnings_status_ok_when_only_queued_placeholders():
    """A filing exists but preprocess hasn't analyzed it yet (`queued=True`,
    `analysis=None`) — already surfaced honestly via the `queued` flag and
    sized around downstream. This pass does not repurpose that state; a
    queued-only run stays 'ok' rather than inventing a new status for it."""
    results = [
        {
            "symbol": "AAPL",
            "analysis": None,
            "is_new": True,
            "queued": True,
            "form_type": "10-Q",
            "filing_date": "2026-09-01",
        }
    ]
    stage = _earnings_stage_for(lambda run_id, session, ctx=None: ([], results))

    ctx = RunContext.start("morning")
    ctx.positions = []
    result_ctx = stage.run(ctx)

    assert result_ctx.data_status["earnings"] == "ok"


def test_classify_earnings_status_real_zero_is_not_treated_as_missing():
    """A legitimately zero figure (e.g. a company that carries no debt,
    reported as an explicit '$0') must NOT be conflated with the 'not
    disclosed' absence sentinel — the exact 'zero is overloaded' mistake
    this codebase already paid for once (docs/WORK.md item 13, PR #255)."""
    from src.pipeline_stages import _classify_earnings_status

    analysis = _valid_earnings_analysis(**{"balance_sheet.total_debt": "$0"})
    results = [{"symbol": "AAPL", "analysis": analysis, "is_new": True}]

    assert _classify_earnings_status(results) == "ok"


def test_earnings_data_quality_flags_problem_matches_real_failure_phrases():
    from src.pipeline_stages import _earnings_data_quality_flags_problem

    assert (
        _earnings_data_quality_flags_problem(
            "Unable to find revenue breakdown in the excerpt.",
        )
        is True
    )
    assert (
        _earnings_data_quality_flags_problem(
            "All figures sourced directly from the filing.",
        )
        is False
    )
    assert _earnings_data_quality_flags_problem("not disclosed") is False
    assert _earnings_data_quality_flags_problem("") is False


# --- XBRL cross-check: a confident but WRONG (not merely empty) figure ---
#
# The gap this closes: `_earnings_analysis_has_real_figures` and
# `_earnings_data_quality_flags_problem` above only catch an EMPTY or
# self-flagged analysis. Neither would ever see a problem in a filing where
# every field is populated, plausible-looking, and simply doesn't match
# what the filer actually filed — the AI isn't reporting any doubt. These
# tests exercise the real SEC-XBRL ground-truth comparison that catches
# exactly that case. See `_earnings_xbrl_mismatch_fields` in
# src/pipeline_stages.py for the field scope and tolerance reasoning.


def test_parse_reported_figure_handles_real_world_formats():
    from src.pipeline_stages import _parse_reported_figure

    assert _parse_reported_figure("$50B") == 50_000_000_000.0
    assert _parse_reported_figure("$5.2 billion") == 5_200_000_000.0
    assert _parse_reported_figure("12%") == 12.0
    assert _parse_reported_figure("$0") == 0.0
    assert _parse_reported_figure("($500M)") == -500_000_000.0
    assert _parse_reported_figure("not disclosed") is None
    assert _parse_reported_figure("") is None
    # A phrasing the parser doesn't recognize is "nothing to compare," not
    # a mismatch — see the function's own docstring.
    assert _parse_reported_figure("roughly fifty billion") is None


def test_classify_earnings_status_ok_when_figures_match_real_xbrl_data():
    """Genuine match: the analyst's reported figures agree (within
    legitimate rounding) with the real SEC XBRL values fetched for this
    filing. Must read as ordinary 'ok', not flagged."""
    from src.pipeline_stages import _classify_earnings_status

    analysis = _valid_earnings_analysis()  # revenue $50B, net_income $5B, eps $2.50, cash $10B
    results = [
        {
            "symbol": "AAPL",
            "analysis": analysis,
            "is_new": True,
            "xbrl_facts": {
                "revenue": 49_700_000_000.0,  # "$50B" rounds this, well within 5%
                "net_income": 5_000_000_000.0,
                "eps": 2.50,
                "cash": 10_000_000_000.0,
            },
        }
    ]

    assert _classify_earnings_status(results) == "ok"


def test_classify_earnings_status_figures_contradicted_on_material_xbrl_mismatch():
    """The real gap this task closes: a confident, non-empty, structurally
    valid figure that is simply WRONG next to the filer's own real SEC
    data. Must downgrade — and land on the distinct 'figures_contradicted'
    status, not 'content_missing' (this filing has plenty of content, it's
    just false) and not 'ok'."""
    from src.pipeline_stages import _classify_earnings_status

    analysis = _valid_earnings_analysis(**{"revenue.total": "$50B"})
    results = [
        {
            "symbol": "AAPL",
            "analysis": analysis,
            "is_new": True,
            # Real filed revenue is $20B — "$50B" is not a rounding of this,
            # it's a different number entirely.
            "xbrl_facts": {"revenue": 20_000_000_000.0},
        }
    ]

    status = _classify_earnings_status(results)
    assert status == "figures_contradicted"
    assert status not in ("ok", "empty", "content_missing", "partial")


def test_classify_earnings_status_figures_contradicted_dominates_a_mixed_batch():
    """A mismatch on even one filing must not be diluted into the routine
    'partial' bucket by other filings in the same run being clean — a
    provably wrong figure is a stronger, more specific signal than an
    ordinary mixed day and must not blend into it."""
    from src.pipeline_stages import _classify_earnings_status

    good = _valid_earnings_analysis(symbol="AAPL")
    bad = _valid_earnings_analysis(symbol="MSFT", **{"profitability.net_income": "$50B"})
    results = [
        {
            "symbol": "AAPL",
            "analysis": good,
            "is_new": True,
            "xbrl_facts": {"revenue": 49_700_000_000.0, "net_income": 5_000_000_000.0},
        },
        {"symbol": "MSFT", "analysis": bad, "is_new": True, "xbrl_facts": {"net_income": 5_000_000_000.0}},
    ]

    assert _classify_earnings_status(results) == "figures_contradicted"


def test_classify_earnings_status_tolerates_legitimate_rounding():
    """Prove the threshold isn't accidentally too tight: a model paraphrasing
    an exact filed figure to a round, readable number (a couple percent off,
    the kind of rounding every earnings summary uses) must NOT trigger the
    downgrade — only a materially wrong figure should."""
    from src.pipeline_stages import _classify_earnings_status

    # "$50B" reported against an exact $49.3B filed value — ~1.4% off,
    # comfortably inside the tolerance real rounding-for-readability produces.
    analysis = _valid_earnings_analysis(**{"revenue.total": "$50B"})
    results = [
        {
            "symbol": "AAPL",
            "analysis": analysis,
            "is_new": True,
            "xbrl_facts": {"revenue": 49_300_000_000.0},
        }
    ]

    assert _classify_earnings_status(results) == "ok"


def test_classify_earnings_status_no_xbrl_data_is_not_a_mismatch():
    """`_get_xbrl_financial_facts`/`_fetch_xbrl_raw` fail open when SEC has
    no XBRL data for a filer/period — `xbrl_facts` then arrives as `{}`.
    That must never itself be read as a mismatch; there is simply nothing
    to cross-check, and the analysis is judged purely on the existing
    content-honesty checks, same as before this cross-check existed."""
    from src.pipeline_stages import _classify_earnings_status

    analysis = _valid_earnings_analysis()
    results = [{"symbol": "AAPL", "analysis": analysis, "is_new": True, "xbrl_facts": {}}]

    assert _classify_earnings_status(results) == "ok"

    # Also true when the key is simply absent (older-shaped result dicts,
    # or the cached-read path — see EarningsReport.xbrl_facts).
    results_no_key = [{"symbol": "AAPL", "analysis": analysis, "is_new": True}]
    assert _classify_earnings_status(results_no_key) == "ok"


def test_earnings_xbrl_mismatch_fields_skips_unparseable_and_absent_values():
    from src.pipeline_stages import _earnings_xbrl_mismatch_fields

    analysis = _valid_earnings_analysis(
        **{
            "revenue.total": "not disclosed",
            "profitability.net_income": "roughly a lot",
        }
    )
    # Real data exists for both, but neither analyst field parsed into a
    # comparable number — must not be flagged as a mismatch.
    xbrl_values = {"revenue": 50_000_000_000.0, "net_income": 5_000_000_000.0}

    assert _earnings_xbrl_mismatch_fields(analysis, xbrl_values) == []


def test_morning_research_stage_news_low_self_reported_confidence_marks_status_low_confidence():
    """The confidence half of the honesty fix: full coverage AND a clean
    parse (the one case the coverage fix alone reads as 'ok'), but the
    model's own `confidence` field says 'low' — a thin or contradictory
    read on headlines it did actually receive. Coverage can't catch this;
    it only counts whether feeds returned data, not whether the model made
    sense of what they returned. Before this, that combination was
    indistinguishable from a clean, trustworthy 'ok' run anywhere
    data_status['news'] is read (Telegram data-quality alert, status
    board). This must read as its own distinct status, not 'ok'."""
    report = _minimal_news_report(confidence="low")
    coverage = NewsCoverage(configured=9, succeeded=9, failed=[])
    stage = _news_coverage_stage(lambda run_id, session: (report, coverage))

    ctx = RunContext.start("morning")
    ctx.positions = []
    result_ctx = stage.run(ctx)

    assert result_ctx.data_status["news"] == "low_confidence"
    assert result_ctx.data_status["news"] != "ok"


def test_morning_research_stage_news_dropped_symbol_marks_status_symbol_dropped():
    """PM TEST GATE item 4, second half (2026-09-14). Full coverage and a
    clean parse, but `NewsAnalystAgent` recorded that the seat's own answer
    is missing a symbol it was shown real headline coverage for
    (`dropped_news_symbols`, set by `_find_dropped_news_symbols`). This is a
    confirmed structural loss, not a self-report, and must page as its own
    status — not silently absorbed into 'ok' the way a plain missing
    stock_news entry would be."""
    report = _minimal_news_report(confidence="high")
    report.dropped_news_symbols = ["AMD"]
    coverage = NewsCoverage(configured=9, succeeded=9, failed=[])
    stage = _news_coverage_stage(lambda run_id, session: (report, coverage))

    ctx = RunContext.start("morning")
    ctx.positions = []
    result_ctx = stage.run(ctx)

    assert result_ctx.data_status["news"] == "symbol_dropped"
    assert result_ctx.data_status["news"] != "ok"


def test_morning_research_stage_news_dropped_symbol_takes_priority_over_low_confidence():
    """Both signals can be true at once — a structural loss is the worse,
    more actionable fact and must win the single data_status['news'] slot."""
    report = _minimal_news_report(confidence="low")
    report.dropped_news_symbols = ["AMD"]
    coverage = NewsCoverage(configured=9, succeeded=9, failed=[])
    stage = _news_coverage_stage(lambda run_id, session: (report, coverage))

    ctx = RunContext.start("morning")
    ctx.positions = []
    result_ctx = stage.run(ctx)

    assert result_ctx.data_status["news"] == "symbol_dropped"


def test_morning_research_stage_news_no_dropped_symbols_stays_ok():
    """Control case: an empty `dropped_news_symbols` (the default) must not
    itself trip the new status."""
    report = _minimal_news_report(confidence="high")
    assert report.dropped_news_symbols == []
    coverage = NewsCoverage(configured=9, succeeded=9, failed=[])
    stage = _news_coverage_stage(lambda run_id, session: (report, coverage))

    ctx = RunContext.start("morning")
    ctx.positions = []
    result_ctx = stage.run(ctx)

    assert result_ctx.data_status["news"] == "ok"


def test_morning_research_stage_news_high_confidence_full_coverage_stays_ok():
    """Control case for the confidence check: high self-reported confidence
    on full coverage must NOT be touched by the new override — only 'low'
    downgrades an otherwise-'ok' status."""
    report = _minimal_news_report(confidence="high")
    coverage = NewsCoverage(configured=9, succeeded=9, failed=[])
    stage = _news_coverage_stage(lambda run_id, session: (report, coverage))

    ctx = RunContext.start("morning")
    ctx.positions = []
    result_ctx = stage.run(ctx)

    assert result_ctx.data_status["news"] == "ok"


@patch("src.stage_morning_research.compute_indicators")
def test_morning_research_stage_persists_specialist_evidence(mock_compute_indicators, tmp_path):
    """Stage 4: MorningResearchStage persists already-validated macro/news/
    tech evidence into `specialist_evidence` with natural scope (run for
    macro/news, symbol for tech) and no decision_id (research-phase,
    generated later in DecisionStage) — read back via a real DB file."""
    import sqlite3

    from src.agents.base import AgentResult
    from src.models import (
        MacroAnalysis,
        MacroNarrative,
        MacroPositionGuidance,
        MacroReasoningChain,
        NewsIntelligenceReport,
        TechAnalysisResult,
    )
    from src.storage.db import Database

    mock_compute_indicators.return_value = MagicMock()

    ma = MacroAnalysis(
        reasoning_chain=MacroReasoningChain(
            volatility_analysis="a",
            yield_curve_analysis="b",
            monetary_policy_analysis="c",
            inflation_labor_credit="d",
            cross_signal_synthesis="e",
            sector_implications="f",
        ),
        regime="risk-on",
        confidence="high",
        equity_outlook="bullish",
        position_guidance=MacroPositionGuidance(
            target_invested_pct=70,
            cash_recommendation_pct=30,
            reasoning="y",
        ),
        summary="z",
    )
    news_intel = NewsIntelligenceReport(
        macro_narrative=MacroNarrative(
            last_updated="2026-04-17",
            era_themes=["AI capex"],
            current_regime="risk-on expansion",
        ),
        pm_briefing="Quiet tape.",
        market_sentiment="bullish",
        confidence="medium",
    )
    agent_result = AgentResult(raw_text="{}", tokens_used=100, model="test", user_message="x")

    mock_config = MagicMock()
    mock_config.trading.universe = ["NVDA"]
    mock_config.trading.lookback_days = 30
    mock_config.llm.macro_analyst_model = "claude-opus-4-6"
    mock_config.llm.tech_analyst_model = "claude-opus-4-6"

    market = MagicMock()
    market.get_ohlcv.return_value = [MagicMock(date="2026-04-17", open=99, high=101, low=98, close=100, volume=1000)]
    market.get_valuation_metrics.return_value = {}

    macro_provider = MagicMock()
    macro_provider.get_macro_summary.return_value = {
        "vix": {"current": 18.0},
        "credit_spread": {"current_bps": 300},
        "inflation": {"core_cpi_yoy": 3.0},
        "unemployment": {"current": 4.2},
    }

    macro_store = MagicMock()
    macro_store.load_last_state.return_value = {}
    news_store = MagicMock()
    news_store.load_macro_narrative.return_value = None

    macro_agent = MagicMock()
    macro_agent.analyze.return_value = (ma, agent_result)

    tech_agent = MagicMock()
    tech_agent.analyze_batch.return_value = (
        {
            "NVDA": TechAnalysisResult(
                symbol="NVDA",
                rating="buy",
                conviction="high",
                entry_price=100.0,
                reference_target=110.0,
                stop_loss=95.0,
                support_levels=[95.0],
                resistance_levels=[110.0],
                setup_type="range",
                expected_horizon_sessions=10,
                reasoning="fresh setup",
                reasoning_chain=_tech_rc(),
                thesis_invalid_if="closes below support",
            )
        },
        agent_result,
    )
    tech_store = MagicMock()
    tech_store.load.return_value = {}
    tech_store.compute_ages.return_value = {}

    db = Database(str(tmp_path / "test.db"))
    db.initialize()
    try:
        stage = MorningResearchStage(
            config=mock_config,
            db=db,
            market=market,
            macro=macro_provider,
            news_provider=MagicMock(),
            news_store=news_store,
            macro_store=macro_store,
            tech_store=tech_store,
            earnings_provider=MagicMock(),
            macro_analyst=macro_agent,
            news_analyst=MagicMock(),
            tech_analyst=tech_agent,
            earnings_analyst=MagicMock(),
            has_actionable_signal_fn=lambda *args, **kw: True,
            run_news_update_fn=lambda run_id, session: (news_intel, None),
            load_earnings_analyses_fn=lambda run_id, session, ctx=None: ([], []),
        )
        ctx = RunContext.start("morning")
        ctx.positions = []
        stage.run(ctx)

        conn = sqlite3.connect(str(tmp_path / "test.db"))
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT agent_name, kind, scope, symbol, decision_id, evidence_json FROM specialist_evidence ORDER BY id"
        ).fetchall()
        by_key = {(r["agent_name"], r["kind"], r["symbol"]): r for r in rows}
        conn.close()

        macro_row = by_key[("macro_analyst", "analysis", None)]
        assert macro_row["scope"] == "run" and macro_row["decision_id"] is None

        news_row = by_key[("news_analyst", "analysis", None)]
        assert news_row["scope"] == "run" and news_row["decision_id"] is None
        assert json.loads(news_row["evidence_json"])["pm_briefing"] == "Quiet tape."

        tech_row = by_key[("tech_analyst", "analysis", "NVDA")]
        assert tech_row["scope"] == "symbol" and tech_row["decision_id"] is None
        assert json.loads(tech_row["evidence_json"])["rating"] == "buy"
    finally:
        db.close()


@patch("src.stage_morning_research.compute_indicators")
def test_morning_research_stage_tech_uses_prior_macro_snapshot(mock_compute_indicators):
    from src.agents.base import AgentResult
    from src.models import (
        MacroAnalysis,
        MacroPositionGuidance,
        MacroReasoningChain,
        TechAnalysisResult,
    )

    mock_compute_indicators.return_value = MagicMock()

    ma = MacroAnalysis(
        reasoning_chain=MacroReasoningChain(
            volatility_analysis="a",
            yield_curve_analysis="b",
            monetary_policy_analysis="c",
            inflation_labor_credit="d",
            cross_signal_synthesis="e",
            sector_implications="f",
        ),
        regime="risk-on",
        confidence="high",
        equity_outlook="bullish",
        position_guidance=MacroPositionGuidance(
            target_invested_pct=70,
            cash_recommendation_pct=30,
            reasoning="y",
        ),
        summary="z",
    )
    agent_result = AgentResult(raw_text="{}", tokens_used=100, model="test", user_message="x")

    mock_config = MagicMock()
    mock_config.trading.universe = ["NVDA"]
    mock_config.trading.lookback_days = 30
    mock_config.llm.macro_analyst_model = "claude-opus-4-6"
    mock_config.llm.tech_analyst_model = "claude-opus-4-6"

    market = MagicMock()
    market.get_ohlcv.return_value = [MagicMock(date="2026-04-17", open=99, high=101, low=98, close=100, volume=1000)]
    market.get_valuation_metrics.return_value = {}

    macro_provider = MagicMock()
    macro_provider.get_macro_summary.return_value = {
        "vix": {"current": 18.0},
        "credit_spread": {"current_bps": 300},
        "inflation": {"core_cpi_yoy": 3.0},
        "unemployment": {"current": 4.2},
    }

    macro_store = MagicMock()
    macro_store.load_last_state.return_value = {
        "regime": "risk-off",
        "equity_outlook": "bearish",
    }
    news_store = MagicMock()
    news_store.load_macro_narrative.return_value = "prior narrative"

    macro_agent = MagicMock()
    macro_agent.analyze.return_value = (ma, agent_result)

    tech_agent = MagicMock()
    tech_agent.analyze_batch.return_value = (
        {
            "NVDA": TechAnalysisResult(
                symbol="NVDA",
                rating="buy",
                conviction="high",
                entry_price=100.0,
                reference_target=110.0,
                stop_loss=95.0,
                support_levels=[95.0],
                resistance_levels=[110.0],
                setup_type="range",
                expected_horizon_sessions=10,
                reasoning="fresh setup",
                reasoning_chain=_tech_rc(),
                thesis_invalid_if="closes below support",
            )
        },
        agent_result,
    )

    tech_store = MagicMock()
    tech_store.load.return_value = {}
    tech_store.compute_ages.return_value = {}

    stage = MorningResearchStage(
        config=mock_config,
        db=MagicMock(),
        market=market,
        macro=macro_provider,
        news_provider=MagicMock(),
        news_store=news_store,
        macro_store=macro_store,
        tech_store=tech_store,
        earnings_provider=MagicMock(),
        macro_analyst=macro_agent,
        news_analyst=MagicMock(),
        tech_analyst=tech_agent,
        earnings_analyst=MagicMock(),
        has_actionable_signal_fn=lambda *args, **kw: True,
        run_news_update_fn=lambda run_id, session: (None, None),
        load_earnings_analyses_fn=lambda run_id, session, ctx=None: ([], []),
    )

    ctx = RunContext.start("morning")
    ctx.positions = []
    stage.run(ctx)

    assert macro_store.load_last_state.call_count == 1
    tech_kwargs = tech_agent.analyze_batch.call_args.kwargs
    assert tech_kwargs["prior_macro_regime"] == "risk-off"
    assert tech_kwargs["prior_macro_outlook"] == "bearish"


# --------------------------------------------------------------------------
# `_build_active_state_changes` — Phase 13 catalyst-gate fix: rendering the
# per-symbol `(direction)` suffix `PortfolioManagerAgent.
# _state_change_symbols_by_date` parses back out.
# --------------------------------------------------------------------------


def test_build_active_state_changes_renders_direction_per_symbol():
    from src.pipeline_prompt_facts import PromptHistory

    fake_pipeline = MagicMock()
    fake_pipeline.news_store.recent_state_changes.return_value = [
        {
            "first_seen_date": "2026-08-31",
            "event": "Oil majors expand footprint, bearish for airlines",
            "affected_symbols": ["XOM", "CVX", "COST"],
            "symbol_direction": {"XOM": "bullish", "CVX": "bullish", "COST": "bearish"},
        }
    ]
    rendered = PromptHistory._build_active_state_changes(fake_pipeline)
    assert rendered == (
        "- [2026-08-31] Oil majors expand footprint, bearish for airlines → XOM(bullish), CVX(bullish), COST(bearish)"
    )


def test_build_active_state_changes_renders_unknown_for_a_symbol_with_no_direction():
    """A symbol named in `affected_symbols` but absent from
    `symbol_direction` (older persisted report predating this field, or a
    genuine analyst omission) renders as `(unknown)` — never silently
    treated as agreeing with any trade direction."""
    from src.pipeline_prompt_facts import PromptHistory

    fake_pipeline = MagicMock()
    fake_pipeline.news_store.recent_state_changes.return_value = [
        {
            "first_seen_date": "2026-08-30",
            "event": "Some older event",
            "affected_symbols": ["FOO"],
            # no "symbol_direction" key at all — pre-Phase-13 persisted shape
        }
    ]
    rendered = PromptHistory._build_active_state_changes(fake_pipeline)
    assert rendered == "- [2026-08-30] Some older event → FOO(unknown)"


# === Levels coverage — closing the "silent data failure looks like a quiet
# day" hole (2026-09-02). See `_check_levels_coverage`'s own docstring in
# src/pipeline_stages.py for why 2026-09-01 is NOT the day this reproduces
# (computed_levels did not exist in the code that ran that morning) and what
# actually caused that day's zero trades (the R/R-geometry defect, unrelated
# to structure). These tests cover the new mechanism on its own terms: does
# it record coverage every run, and does it alert on the cases it claims to
# catch and stay quiet on the cases it doesn't. ===


def _levels_analysis(symbol, levels):
    """A minimal resolved TechAnalysisResult carrying a given computed_levels
    — the only field these tests vary."""
    from src.models import TechAnalysisResult

    return TechAnalysisResult(
        symbol=symbol,
        rating="neutral",
        conviction="low",
        entry_price=100.0,
        stop_loss=95.0,
        reference_target=110.0,
        support_levels=[],
        resistance_levels=[],
        setup_type="range",
        expected_horizon_sessions=10,
        reasoning_chain=_tech_rc(),
        reasoning="test",
        computed_levels=levels,
        thesis_invalid_if="closes below support",
    )


def _coverage_ctx(*, universe, bars_missing, run_id="run-test00000000"):
    ctx = RunContext.start("morning")
    ctx.run_id = run_id
    ctx.tech_bars_coverage = {
        "universe": universe,
        "bars_fetched": universe - bars_missing,
        "bars_missing": bars_missing,
        "bars_missing_symbols": [],
    }
    return ctx


def test_check_levels_coverage_persists_evidence_on_a_normal_run():
    """The `levels_coverage` evidence row is written even when nothing is
    wrong — observability cannot be conditional on severity, or the one
    normal day nobody looks at twice is exactly the day with no record."""
    from src.pipeline_stages import _check_levels_coverage

    ctx = _coverage_ctx(universe=12, bars_missing=0)
    analyses = [_levels_analysis(f"S{i}", [100.0]) for i in range(11)] + [
        _levels_analysis("EMPTY1", []),
    ]
    db = MagicMock()
    with patch("src.notifier.send_owner_alert") as alert:
        _check_levels_coverage(db, ctx, analyses)

    assert not alert.called
    db.insert_specialist_evidence.assert_called_once()
    kwargs = db.insert_specialist_evidence.call_args.kwargs
    assert kwargs["run_id"] == ctx.run_id
    assert kwargs["agent_name"] == "tech_analyst"
    assert kwargs["kind"] == "levels_coverage"
    assert kwargs["scope"] == "run"
    assert kwargs["symbol"] is None
    payload = json.loads(kwargs["evidence_json"])
    assert payload["universe"] == 12
    assert payload["bars_missing"] == 0
    assert payload["resolved"] == 12
    assert payload["levels_present"] == 11
    assert payload["levels_empty"] == 1
    assert payload["levels_empty_symbols"] == ["EMPTY1"]


def test_check_levels_coverage_matches_the_only_real_baseline_measured():
    """Regression pin for the one clean normal-run measurement that exists:
    2026-09-02's morning run had 64 resolved analyses, 63 with a computed
    level and 1 (MRVL) without — 1.6% empty. The threshold derivation in
    LEVELS_DEGRADED_RUN_EMPTY_SHARE's comment is only honest if that shape
    genuinely does not alert; pin it so a future change to the arithmetic
    can't silently start paging the owner every morning."""
    from src.pipeline_stages import _check_levels_coverage

    ctx = _coverage_ctx(universe=64, bars_missing=0)
    analyses = [_levels_analysis(f"S{i}", [100.0]) for i in range(63)] + [
        _levels_analysis("MRVL", []),
    ]
    db = MagicMock()
    with patch("src.notifier.send_owner_alert") as alert:
        _check_levels_coverage(db, ctx, analyses)

    assert not alert.called


def test_check_levels_coverage_alerts_red_on_total_levels_blackout():
    """Every resolved symbol comes back with zero structural levels — the
    unambiguous case the task calls out by name. A live feed does not put
    every chart in one batch into the identical empty state; this is a
    provider failure, not 12 coincidentally structureless charts."""
    from src.pipeline_stages import _check_levels_coverage

    ctx = _coverage_ctx(universe=12, bars_missing=0)
    analyses = [_levels_analysis(f"S{i}", []) for i in range(12)]
    db = MagicMock()
    with patch("src.notifier.send_owner_alert") as alert:
        _check_levels_coverage(db, ctx, analyses)

    alert.assert_called_once()
    body = alert.call_args.args[0]
    assert body.startswith("🔴 TECH DATA BLIND SPOT")
    assert "structural level" in body
    assert "12" in body


def test_check_levels_coverage_alerts_red_on_total_bars_blackout():
    """No symbol in the universe got bars back at all — tech_analyst never
    even ran (symbols_data was empty), so `analyses` is []. This is the
    sharper, earlier form of blindness: the pre-filter and tech_analyst
    both look identical to 'nothing worth trading' from downstream."""
    from src.pipeline_stages import _check_levels_coverage

    ctx = _coverage_ctx(universe=15, bars_missing=15)
    db = MagicMock()
    with patch("src.notifier.send_owner_alert") as alert:
        _check_levels_coverage(db, ctx, [])

    alert.assert_called_once()
    body = alert.call_args.args[0]
    assert body.startswith("🔴 TECH DATA BLIND SPOT")
    assert "data feed" in body
    assert "15" in body


def test_check_levels_coverage_alerts_orange_at_the_degraded_boundary():
    """Exactly half of resolved symbols come back empty: above the coarse
    50% tripwire but not total. Must alert, and must NOT use the RED/total
    header — the message should read as serious-but-partial, matching the
    existing NO STOP AT ALL / STOP PARTIALLY COVERS severity split (marks are
    counted, not coloured — item 21b)."""
    from src.pipeline_stages import _check_levels_coverage

    ctx = _coverage_ctx(universe=10, bars_missing=0)
    analyses = [_levels_analysis(f"E{i}", []) for i in range(5)] + [
        _levels_analysis(f"F{i}", [100.0]) for i in range(5)
    ]
    db = MagicMock()
    with patch("src.notifier.send_owner_alert") as alert:
        _check_levels_coverage(db, ctx, analyses)

    alert.assert_called_once()
    body = alert.call_args.args[0]
    assert body.startswith("🟠 TECH DATA DEGRADED")
    assert "5/10" in body


def test_check_levels_coverage_no_alert_just_under_the_degraded_boundary():
    """49% empty must NOT alert — pins the boundary from the other side so
    `>=` vs `>` cannot silently flip without a test noticing."""
    from src.pipeline_stages import _check_levels_coverage

    ctx = _coverage_ctx(universe=100, bars_missing=0)
    analyses = [_levels_analysis(f"E{i}", []) for i in range(49)] + [
        _levels_analysis(f"F{i}", [100.0]) for i in range(51)
    ]
    db = MagicMock()
    with patch("src.notifier.send_owner_alert") as alert:
        _check_levels_coverage(db, ctx, analyses)

    assert not alert.called


def test_check_levels_coverage_small_universe_guarded_from_false_alarm():
    """3 resolved symbols, all empty, is 100% — but 3 is noise, not a
    universe. LEVELS_COVERAGE_MIN_SAMPLE must suppress it; without the
    guard this is exactly the small-N case that would page the owner off
    a 2-symbol rehearsal or test run."""
    from src.pipeline_stages import _check_levels_coverage

    ctx = _coverage_ctx(universe=3, bars_missing=0)
    analyses = [_levels_analysis(f"S{i}", []) for i in range(3)]
    db = MagicMock()
    with patch("src.notifier.send_owner_alert") as alert:
        _check_levels_coverage(db, ctx, analyses)

    assert not alert.called
    # Still recorded — the guard suppresses the ALERT, not the observability.
    db.insert_specialist_evidence.assert_called_once()


def test_check_levels_coverage_never_raises_when_bars_coverage_absent():
    """If `_run_tech` crashed before ever setting ctx.tech_bars_coverage
    (still its `{}` dataclass default), the bars axis must be skipped
    quietly rather than raising — and the levels axis, which does not
    depend on it, must still be evaluated correctly."""
    from src.pipeline_stages import _check_levels_coverage

    ctx = RunContext.start("morning")
    assert ctx.tech_bars_coverage == {}
    analyses = [_levels_analysis(f"S{i}", []) for i in range(12)]
    db = MagicMock()
    with patch("src.notifier.send_owner_alert") as alert:
        _check_levels_coverage(db, ctx, analyses)  # must not raise

    alert.assert_called_once()
    assert "structural level" in alert.call_args.args[0]
    assert "bar fetch" not in alert.call_args.args[0]  # bars axis had nothing to say


def test_check_levels_coverage_never_raises_on_malformed_coverage_dict():
    """A defensive check, not a real production shape: if
    ctx.tech_bars_coverage were ever malformed, this must degrade to
    'nothing to report' rather than crashing MorningResearchStage.run —
    matching _persist_evidence's own never-raises contract."""
    from src.pipeline_stages import _check_levels_coverage

    ctx = _coverage_ctx(universe=12, bars_missing=0)
    ctx.tech_bars_coverage = {"universe": "not-a-number"}
    db = MagicMock()
    with patch("src.notifier.send_owner_alert") as alert:
        _check_levels_coverage(db, ctx, [_levels_analysis("A", [100.0])])  # must not raise

    assert not alert.called


@patch("src.stage_morning_research.compute_indicators")
def test_morning_research_stage_alerts_owner_on_full_universe_levels_blackout(
    mock_compute_indicators,
):
    """End-to-end through MorningResearchStage.run(): every symbol gets
    bars, tech_analyst resolves all of them, and every one comes back with
    an empty computed_levels — the shape a dead structural-levels
    computation (or a feed silently serving unusable history to every
    request) would produce. Confirms the wiring from `_run_tech` (setting
    ctx.tech_bars_coverage) through to `_check_levels_coverage` actually
    fires the owner alert in the real stage, not just in the unit test
    calling it directly."""
    mock_compute_indicators.return_value = MagicMock()

    universe = [f"SYM{i}" for i in range(12)]
    mock_config = MagicMock()
    mock_config.trading.universe = universe
    mock_config.trading.lookback_days = 30

    market = MagicMock()
    market.get_ohlcv.return_value = [MagicMock()]

    from src.models import TechAnalysisResult

    analyses_map = {
        sym: TechAnalysisResult(
            symbol=sym,
            rating="neutral",
            conviction="low",
            entry_price=100.0,
            stop_loss=95.0,
            reference_target=110.0,
            support_levels=[],
            resistance_levels=[],
            setup_type="range",
            expected_horizon_sessions=10,
            reasoning_chain=_tech_rc(),
            reasoning="test",
            computed_levels=[],  # every symbol blind,
            thesis_invalid_if="closes below support",
        )
        for sym in universe
    }
    tech_agent = MagicMock()
    tech_agent.analyze_batch.return_value = (
        analyses_map,
        MagicMock(
            user_message="m", raw_text="{}", tokens_used=1, input_tokens=1, output_tokens=1, cost_usd=0.0, model="t"
        ),
    )

    macro_store = MagicMock()
    macro_store.load_last_state.return_value = None
    news_store = MagicMock()
    news_store.load_macro_narrative.return_value = None
    macro_agent = MagicMock()
    macro_agent.analyze.return_value = (
        None,
        MagicMock(
            user_message="m",
            raw_text="{}",
            tokens_used=1,
            model="t",
            input_tokens=1,
            output_tokens=1,
            cost_usd=0.0,
        ),
    )
    tech_store = MagicMock()
    tech_store.load.return_value = {}
    tech_store.compute_ages.return_value = {}

    stage = MorningResearchStage(
        config=mock_config,
        db=MagicMock(),
        market=market,
        macro=MagicMock(),
        news_provider=MagicMock(),
        news_store=news_store,
        macro_store=macro_store,
        tech_store=tech_store,
        earnings_provider=MagicMock(),
        macro_analyst=macro_agent,
        news_analyst=MagicMock(),
        tech_analyst=tech_agent,
        earnings_analyst=MagicMock(),
        has_actionable_signal_fn=lambda *args, **kw: True,
        run_news_update_fn=lambda run_id, session: (None, None),
        load_earnings_analyses_fn=lambda run_id, session, ctx=None: ([], []),
    )

    ctx = RunContext.start("morning")
    ctx.positions = []
    with patch("src.notifier.send_owner_alert") as alert:
        result_ctx = stage.run(ctx)

    assert len(result_ctx.analyses) == 12
    assert result_ctx.tech_bars_coverage["bars_missing"] == 0
    alert.assert_called_once()
    assert alert.call_args.args[0].startswith("🔴 TECH DATA BLIND SPOT")


@patch("src.stage_morning_research.compute_indicators")
def test_morning_research_stage_no_alert_when_bars_fetch_partly_fails_normally(
    mock_compute_indicators,
):
    """A couple of symbols missing bars out of a large universe is normal
    (a delisting, a temporary halt) and must not page the owner — only a
    majority-or-more failure should. Also confirms ctx.tech_bars_coverage
    counts the drop instead of only logging it (2026-09-02 fix)."""
    mock_compute_indicators.return_value = MagicMock()

    universe = [f"SYM{i}" for i in range(12)]
    missing = {"SYM0", "SYM1"}
    mock_config = MagicMock()
    mock_config.trading.universe = universe
    mock_config.trading.lookback_days = 30

    def _get_ohlcv(symbol, _lookback):
        return [] if symbol in missing else [MagicMock()]

    market = MagicMock()
    market.get_ohlcv.side_effect = _get_ohlcv

    from src.models import TechAnalysisResult

    resolved_symbols = [s for s in universe if s not in missing]
    analyses_map = {
        sym: TechAnalysisResult(
            symbol=sym,
            rating="neutral",
            conviction="low",
            entry_price=100.0,
            stop_loss=95.0,
            reference_target=110.0,
            support_levels=[],
            resistance_levels=[],
            setup_type="range",
            expected_horizon_sessions=10,
            reasoning_chain=_tech_rc(),
            reasoning="test",
            computed_levels=[100.0],  # every RESOLVED symbol has a level,
            thesis_invalid_if="closes below support",
        )
        for sym in resolved_symbols
    }
    tech_agent = MagicMock()
    tech_agent.analyze_batch.return_value = (
        analyses_map,
        MagicMock(
            user_message="m", raw_text="{}", tokens_used=1, input_tokens=1, output_tokens=1, cost_usd=0.0, model="t"
        ),
    )

    macro_store = MagicMock()
    macro_store.load_last_state.return_value = None
    news_store = MagicMock()
    news_store.load_macro_narrative.return_value = None
    macro_agent = MagicMock()
    macro_agent.analyze.return_value = (
        None,
        MagicMock(
            user_message="m",
            raw_text="{}",
            tokens_used=1,
            model="t",
            input_tokens=1,
            output_tokens=1,
            cost_usd=0.0,
        ),
    )
    tech_store = MagicMock()
    tech_store.load.return_value = {}
    tech_store.compute_ages.return_value = {}

    stage = MorningResearchStage(
        config=mock_config,
        db=MagicMock(),
        market=market,
        macro=MagicMock(),
        news_provider=MagicMock(),
        news_store=news_store,
        macro_store=macro_store,
        tech_store=tech_store,
        earnings_provider=MagicMock(),
        macro_analyst=macro_agent,
        news_analyst=MagicMock(),
        tech_analyst=tech_agent,
        earnings_analyst=MagicMock(),
        has_actionable_signal_fn=lambda *args, **kw: True,
        run_news_update_fn=lambda run_id, session: (None, None),
        load_earnings_analyses_fn=lambda run_id, session, ctx=None: ([], []),
    )

    ctx = RunContext.start("morning")
    ctx.positions = []
    with patch("src.notifier.send_owner_alert") as alert:
        result_ctx = stage.run(ctx)

    assert result_ctx.tech_bars_coverage["bars_missing"] == 2
    assert result_ctx.tech_bars_coverage["universe"] == 12
    assert set(result_ctx.tech_bars_coverage["bars_missing_symbols"]) == missing
    assert not alert.called


@patch("src.stage_morning_research.compute_indicators")
def test_morning_research_stage_records_bars_coverage_even_when_tech_analyst_crashes(
    mock_compute_indicators,
    tmp_path,
):
    """Bar fetch is the FIRST thing `_run_tech` does, before tech_analyst is
    ever called — so if analyze_batch itself raises (LLM/provider crash,
    not a bars problem), the coverage evidence row must still land with the
    correct bars numbers. This is the scenario the placement of
    `_check_levels_coverage` OUTSIDE the tech try/except in
    MorningResearchStage.run exists for: a tech-stage failure severe enough
    to crash the batch is exactly a case worth recording, not one to skip
    because the surrounding try/except already caught something. Uses a
    real on-disk DB (not a Mock) so the row is read back rather than
    asserted against a mock's call args."""
    import sqlite3

    from src.storage.db import Database

    mock_compute_indicators.return_value = MagicMock()

    universe = [f"SYM{i}" for i in range(12)]
    mock_config = MagicMock()
    mock_config.trading.universe = universe
    mock_config.trading.lookback_days = 30

    market = MagicMock()
    market.get_ohlcv.return_value = [MagicMock()]  # bars fine for everyone

    tech_agent = MagicMock()
    tech_agent.analyze_batch.side_effect = RuntimeError("provider outage")

    macro_store = MagicMock()
    macro_store.load_last_state.return_value = None
    news_store = MagicMock()
    news_store.load_macro_narrative.return_value = None
    macro_agent = MagicMock()
    macro_agent.analyze.return_value = (
        None,
        MagicMock(
            user_message="m",
            raw_text="{}",
            tokens_used=1,
            model="t",
            input_tokens=1,
            output_tokens=1,
            cost_usd=0.0,
        ),
    )
    tech_store = MagicMock()
    tech_store.load.return_value = {}

    db = Database(str(tmp_path / "test.db"))
    db.initialize()
    try:
        stage = MorningResearchStage(
            config=mock_config,
            db=db,
            market=market,
            macro=MagicMock(),
            news_provider=MagicMock(),
            news_store=news_store,
            macro_store=macro_store,
            tech_store=tech_store,
            earnings_provider=MagicMock(),
            macro_analyst=macro_agent,
            news_analyst=MagicMock(),
            tech_analyst=tech_agent,
            earnings_analyst=MagicMock(),
            has_actionable_signal_fn=lambda *args, **kw: True,
            run_news_update_fn=lambda run_id, session: (None, None),
            load_earnings_analyses_fn=lambda run_id, session, ctx=None: ([], []),
        )
        ctx = RunContext.start("morning")
        ctx.positions = []
        with patch("src.notifier.send_owner_alert") as alert:
            result_ctx = stage.run(ctx)

        assert result_ctx.data_status["tech"] == "failed"
        assert result_ctx.analyses == []
        # Bars were fine — the crash was in analyze_batch, after bars-fetch
        # already completed and recorded.
        assert result_ctx.tech_bars_coverage["bars_missing"] == 0
        assert result_ctx.tech_bars_coverage["universe"] == 12
        # 12 resolved (0) is below LEVELS_COVERAGE_MIN_SAMPLE-vs-0 either
        # way, so no owner alert fires from an empty analyses list — but
        # the coverage row must still exist.
        assert not alert.called

        conn = sqlite3.connect(str(tmp_path / "test.db"))
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT evidence_json FROM specialist_evidence WHERE agent_name='tech_analyst' AND kind='levels_coverage'"
        ).fetchone()
        conn.close()
        assert row is not None, "levels_coverage row must be written even when analyze_batch crashes"
        payload = json.loads(row["evidence_json"])
        assert payload["universe"] == 12
        assert payload["bars_missing"] == 0
        assert payload["resolved"] == 0
    finally:
        db.close()


# --------------------------------------------------------------------------
# retired board item 49 — the ranking handed to the constructor.
# --------------------------------------------------------------------------


def test_session_candidate_ranking_reads_the_pms_own_order():
    from types import SimpleNamespace

    from src.pipeline_stages import _session_candidate_ranking

    pipeline = SimpleNamespace(
        portfolio_manager=SimpleNamespace(
            last_candidate_ranking=[
                SimpleNamespace(symbol="nvda"),
                SimpleNamespace(symbol=" amd "),
            ],
        )
    )
    assert _session_candidate_ranking(pipeline) == ["NVDA", "AMD"]


def test_session_candidate_ranking_is_none_not_empty_when_there_is_no_ranking():
    """An EMPTY ranking and an ABSENT one mean the same thing to the
    allocator — fall back to its pre-decision ordering. Returning `[]` would
    be indistinguishable from "every candidate ranked last"."""
    from types import SimpleNamespace

    from src.pipeline_stages import _session_candidate_ranking

    assert _session_candidate_ranking(SimpleNamespace()) is None
    assert (
        _session_candidate_ranking(SimpleNamespace(portfolio_manager=SimpleNamespace(last_candidate_ranking=[])))
        is None
    )
    assert (
        _session_candidate_ranking(
            SimpleNamespace(
                portfolio_manager=SimpleNamespace(
                    last_candidate_ranking=[SimpleNamespace(symbol="  ")],
                )
            )
        )
        is None
    )


# ---------------------------------------------------------------------------
# Board items 135 (short side of the #519 guard) and 136 (a scale-driven
# drop must leave a trace). 2026-09-18.
# ---------------------------------------------------------------------------


def _short(symbol, alloc):
    from src.models import TradeDecision

    return TradeDecision(
        action="SHORT",
        symbol=symbol,
        allocation_pct=alloc,
        entry_price=100.0,
        stop_loss=105.0,
        take_profit=90.0,
        reasoning="x",
        thesis_invalid_if="reclaims resistance",
    )


def _run_mods(decisions, modifications):
    """Exactly the sequence `RiskStage.run` performs. Board item 155: that
    is now ONE call — the outer `_revert_entry_size_increases` sweep that
    used to follow it is deleted, its SHORT coverage folded into guard 1b."""
    from src.pipeline import TradingPipeline

    pipeline = build_pipeline()
    updated, rejected = pipeline.risk_gate._apply_risk_modifications(decisions, modifications)
    return updated, list(rejected)


def test_item135_short_allocation_may_not_be_enlarged_by_a_risk_mod():
    """Board item 135. `_apply_risk_modifications` guard 1b tests
    `decision.action == "BUY"`, so an RM edit that ENLARGED a SHORT sailed
    through — although the constructor sizes a short add with the mirror of
    the same cumulative clamp, so an upward edit grows the short by more than
    the number reads. Must be reverted to the constructor's size."""
    from src.models import RiskModification

    mods = [
        RiskModification(
            symbol="XLU",
            field="allocation_pct",
            original_value=12.0,
            new_value=40.0,
            reason="wants a bigger short",
        )
    ]
    updated, rejected = _run_mods([_short("XLU", 12.0)], mods)

    assert len(updated) == 1
    assert updated[0].allocation_pct == 12.0, "the SHORT must ship at the constructor's size, not the enlarged one"
    assert any(
        r["symbol"] == "XLU" and r["field"] == "allocation_pct" and "INCREASE" in r["reason"] for r in rejected
    ), f"the refusal must be recorded as a visible event; got {rejected}"


def test_item135_short_allocation_may_still_be_reduced():
    """Control. Cutting a short is the seat's actual remit and must apply."""
    from src.models import RiskModification

    mods = [
        RiskModification(
            symbol="XLU",
            field="allocation_pct",
            original_value=12.0,
            new_value=5.0,
            reason="too big",
        )
    ]
    updated, rejected = _run_mods([_short("XLU", 12.0)], mods)

    assert updated[0].allocation_pct == 5.0
    assert rejected == []


def test_item135_buy_enlargement_is_refused_exactly_once():
    """Control, and the BUY half of item 155. The BUY case is handled inside
    `_apply_risk_modifications` guard 1b and must be refused there exactly
    once — not double-reported by a second enforcement point."""
    from src.models import RiskModification

    mods = [
        RiskModification(
            symbol="SPY",
            field="allocation_pct",
            original_value=10.0,
            new_value=25.0,
            reason="bigger",
        )
    ]
    updated, rejected = _run_mods([_buy("SPY", 10.0)], mods)

    assert updated[0].allocation_pct == 10.0
    assert len(rejected) == 1, f"exactly one refusal, not two; got {rejected}"


def test_item155_entry_enlargement_has_exactly_one_enforcement_point():
    """Board item 155. The rule "the risk seat may only REDUCE an entry's
    `allocation_pct`" lives in `_apply_risk_modifications` guard 1b and
    NOWHERE ELSE. It was duplicated one layer out for months
    (`_revert_entry_size_increases` in `src/pipeline_stages.py`), placed there
    only because `src/pipeline.py` was locked by another workstream; this
    codebase's own comments call exactly that duplication the drift behind
    the #519 defect.

    This test fails if a second enforcement point reappears, on either of the
    two ways it can come back:

      (a) a resurrected sweep function in `src/pipeline_stages.py`, or
      (b) guard 1b silently narrowing back to BUY-only, which is what makes a
          second point look necessary again.
    """
    import inspect
    import src.pipeline_stages as ps
    from src.pipeline_risk_gate import RiskGate  # the body lives here (step 7)

    # (a) No second enforcement point in the stage layer.
    stage_src = inspect.getsource(ps)
    assert "_revert_entry_size_increases" not in stage_src, (
        "board item 155: the entry-size rule was consolidated onto "
        "`_apply_risk_modifications` guard 1b. A second enforcement point in "
        "src/pipeline_stages.py has reappeared — extend guard 1b instead."
    )
    for name, obj in vars(ps).items():
        if callable(obj) and "revert_entry_size" in name:
            raise AssertionError(f"board item 155: `{name}` duplicates guard 1b's rule")

    # (b) Guard 1b itself still covers BOTH sides, so no second point is
    # needed. Read only the guard-1b block, not the whole method: guards 2
    # and 3 carry their own BUY/SHORT tests.
    guard_src = inspect.getsource(RiskGate._apply_risk_modifications)
    block = guard_src.split("# Guard 1b")[-1].split("# Guard 2")[0]
    assert 'decision.action in ("BUY", "SHORT")' in block, (
        "board item 155: guard 1b no longer tests both sides — if it has "
        "narrowed back to BUY-only, that is the gap the deleted outer sweep "
        f"existed to cover. Guard 1b now reads:\n{block}"
    )

    # And it behaves, on both sides, from one place.
    from src.models import RiskModification

    for leg, side in ((_buy("SPY", 10.0), "BUY"), (_short("XLU", 10.0), "SHORT")):
        updated, rejected = _run_mods(
            [leg],
            [
                RiskModification(
                    symbol=leg.symbol,
                    field="allocation_pct",
                    original_value=10.0,
                    new_value=30.0,
                    reason="bigger",
                )
            ],
        )
        assert updated[0].allocation_pct == 10.0, side
        assert len(rejected) == 1, f"{side}: one refusal only; got {rejected}"


def test_item134_scale_advisory_records_every_entry_without_dropping_it():
    """Board items 134 + 162 (owner ruling 2026-09-25). scale_all_buys is now
    ADVISORY on entries: it drops NOTHING (contrast the old item-136 drop it
    replaces) and resizes NOTHING, but it still flags every BUY/SHORT so the
    caller can file the per-symbol advisory record that carries the concern +
    reason to the desk. Exits are never flagged."""
    from src.models import RiskVerdict
    from src.pipeline_stages import _record_scale_advisory

    verdict = RiskVerdict(
        approved=True,
        scale_all_buys=0.0,
        reasoning_chain=_risk_rc(),
        reasoning="risk-off",
    )
    decisions = [_buy("SPY", 10), _short("XLU", 8), _hold("MSFT"), _sell("NVDA")]

    out, scale, advised = _record_scale_advisory(decisions, verdict)

    assert scale == 0.0
    # Nothing dropped — every decision survives, including both entries.
    assert [d.action for d in out] == ["BUY", "SHORT", "HOLD", "SELL"]
    assert [d.allocation_pct for d in out] == [10.0, 8.0, 0.0, 100.0]
    assert sorted(advised) == [("SPY", 10.0), ("XLU", 8.0)], (
        f"every flagged entry must be recorded with its size; got {advised}"
    )


def test_item134_nothing_flagged_when_scale_is_one():
    """Control. scale == 1.0 (no concern) flags nothing and resizes nothing."""
    from src.models import RiskVerdict
    from src.pipeline_stages import _record_scale_advisory

    verdict = RiskVerdict(
        approved=True,
        scale_all_buys=1.0,
        reasoning_chain=_risk_rc(),
        reasoning="ok",
    )
    out, scale, advised = _record_scale_advisory([_buy("SPY", 10)], verdict)

    assert out[0].allocation_pct == 10.0
    assert advised == []


def test_item134_riskstage_records_scale_concern_and_keeps_sizes(monkeypatch):
    """Board items 134 + 162, end-to-end through `RiskStage.run`. A verdict with
    scale_all_buys < 1.0 must (a) leave every entry's allocation_pct unchanged
    and drop nothing, and (b) surface the seat's exposure concern + its reason
    as a durable `scale_advisory` pipeline event per entry."""
    import src.pipeline_stages as ps
    from src.pipeline_stages import RiskStage
    from src.pipeline import TradingPipeline
    from src.models import (
        PortfolioDecision,
        ReasoningChain,
        RiskVerdict,
        RiskReasoningChain,
    )

    events: list = []
    monkeypatch.setattr(
        ps,
        "_record_pipeline_event",
        lambda pipeline, ctx, sym, kind, outcome, reason, **kw: events.append(
            {"symbol": sym, "kind": kind, "outcome": outcome, "reason": reason, "kw": kw}
        ),
    )

    p = build_pipeline(market=MagicMock(), risk_manager=MagicMock(), db=MagicMock())
    p.market.get_ohlcv.return_value = []
    p.admission = MagicMock(_filter_supported_symbols=MagicMock(side_effect=lambda d, a, pos: (d, [])))
    p.risk_gate._refuse_queued_earnings_buys = MagicMock(side_effect=lambda d, e, **kw: d)
    # Hard filter is a pass-through HERE so we can isolate the scale behaviour;
    # a separate test proves the real hard filter still binds.
    p.risk_gate._filter_hard_risk_decisions = MagicMock(side_effect=lambda d, *a, **k: (d, [], []))
    p.risk_manager.review.return_value = (
        RiskVerdict(
            approved=True,
            modifications=[],
            scale_all_buys=0.5,
            reasoning="Aggregate exposure looks stretched for the regime.",
            reason_category="oversized",
            reasoning_chain=RiskReasoningChain(
                rr_audit="x",
                signal_fidelity="x",
                correlation_check="x",
                event_risk="x",
                sizing_sanity="x",
                overall="x",
            ),
        ),
        MagicMock(user_message="m", raw_text="{}", tokens_used=1, input_tokens=1, output_tokens=1, cost_usd=0.0),
    )
    p.config = MagicMock()
    p.config.llm.risk_manager_model = "test-model"
    p.config.trading.lookback_days = 120

    ctx = RunContext.start("morning")
    ctx.positions = []
    ctx.total_value = 100_000.0
    ctx.last_equity = 100_000.0
    ctx.cash = 50_000.0
    ctx.deployable_cash = 50_000.0
    ctx.portfolio_decision = PortfolioDecision(
        reasoning_chain=ReasoningChain(
            macro_filter="x",
            news_check="x",
            earnings_check="x",
            signal_conflicts="x",
            sizing_logic="x",
            portfolio_balance="x",
            cash_target="x",
        ),
        decisions=[_buy("SPY", 10.0), _short("XLU", 8.0)],
        portfolio_view="v",
    )
    ctx.symbols_bars = {}
    ctx.data_status = {}

    RiskStage(pipeline=p).run(ctx)

    # (a) sizes untouched, nothing dropped.
    final = {(d.symbol, d.action): d.allocation_pct for d in ctx.portfolio_decision.decisions}
    assert final == {("SPY", "BUY"): 10.0, ("XLU", "SHORT"): 8.0}

    # (b) the concern + reason are surfaced as scale_advisory events.
    advisories = [e for e in events if e["outcome"] == "scale_advisory"]
    assert {e["symbol"] for e in advisories} == {"SPY", "XLU"}
    for e in advisories:
        assert "scale_all_buys=0.50" in e["reason"]
        assert "ADVISORY ONLY" in e["reason"]
        assert "Aggregate exposure looks stretched" in e["reason"]
    # And no scale-driven DROP event exists any more.
    assert not [e for e in events if e["outcome"] == "scaled_out"]


def test_item134_hard_limits_still_bind_when_scale_does_not_shrink():
    """Board items 134 + 162. Because scale_all_buys no longer shrinks entries,
    the hard aggregate limit is the ONLY thing standing between an over-cap plan
    and execution — and it must still bind. Prove the advisory pass leaves the
    over-cap plan intact, then the real hard filter blocks it."""
    from src.pipeline import TradingPipeline
    from src.pipeline_stages import _record_scale_advisory
    from src.risk.rules import RiskRuleEngine
    from src.config import RiskConfig
    from src.models import RiskVerdict, TradeDecision

    verdict = RiskVerdict(
        approved=True,
        scale_all_buys=0.5,
        reasoning_chain=_risk_rc(),
        reasoning="too aggressive",
        reason_category="oversized",
    )
    # Two same-direction longs summing to 60% net, over a 50% hard total cap.
    decisions = [
        TradeDecision(
            action="BUY",
            symbol="SPY",
            allocation_pct=30,
            entry_price=500,
            stop_loss=480,
            take_profit=530,
            reasoning="core",
        ),
        TradeDecision(
            action="BUY",
            symbol="QQQ",
            allocation_pct=30,
            entry_price=400,
            stop_loss=380,
            take_profit=430,
            reasoning="also core",
        ),
    ]

    # Advisory pass changes NOTHING — the plan is still over the cap.
    out, scale, advised = _record_scale_advisory(decisions, verdict)
    assert [(d.symbol, d.allocation_pct) for d in out] == [("SPY", 30.0), ("QQQ", 30.0)]
    assert sorted(advised) == [("QQQ", 30.0), ("SPY", 30.0)]

    pipeline = build_pipeline(
        risk_engine=RiskRuleEngine(
            RiskConfig(
                max_position_pct=40,
                max_total_position_pct=50,
                require_stop_loss=True,
            )
        )
    )
    with (
        patch("src.pipeline_admission._get_sector", return_value="Broad"),
        patch("src.execution.broker._get_sector", return_value="Broad"),
    ):
        allowed, _violations, blocked = pipeline.risk_gate._filter_hard_risk_decisions(
            out,
            positions=[],
            total_value=100000,
        )

    # The hard cap still binds: the second long is blocked.
    assert [d.symbol for d in allowed] == ["SPY"]
    assert any("Net exposure" in r for r in blocked)


def test_morning_research_stage_news_unreadable_field_marks_status_field_unreadable():
    """Board item 152, salvage half. The seat answered and the answer is
    usable, but `analyze()` had to drop a top-level field it sent in a word
    the desk cannot read (measured in production: `market_sentiment` =
    "mixed"). That report is neither clean nor lost, and reading it as "ok"
    would hide a field the next seat is about to make a decision without."""
    report = _minimal_news_report(confidence="high")
    report.market_sentiment = None
    report.unreadable_fields = {"market_sentiment": "mixed"}
    coverage = NewsCoverage(configured=9, succeeded=9, failed=[])
    stage = _news_coverage_stage(lambda run_id, session: (report, coverage))

    ctx = RunContext.start("morning")
    ctx.positions = []
    result_ctx = stage.run(ctx)

    assert result_ctx.data_status["news"] == "field_unreadable"
    assert result_ctx.data_status["news"] != "ok"


def test_morning_research_stage_news_dropped_symbol_outranks_unreadable_field():
    """A per-STOCK loss is the worse fact and keeps the single slot."""
    report = _minimal_news_report(confidence="high")
    report.market_sentiment = None
    report.unreadable_fields = {"market_sentiment": "mixed"}
    report.dropped_news_symbols = ["AMD"]
    coverage = NewsCoverage(configured=9, succeeded=9, failed=[])
    stage = _news_coverage_stage(lambda run_id, session: (report, coverage))

    ctx = RunContext.start("morning")
    ctx.positions = []

    assert stage.run(ctx).data_status["news"] == "symbol_dropped"


def test_morning_research_stage_news_parse_error_files_a_record_per_affected_stock():
    """Board item 152, unsalvageable half. When the news answer cannot be
    read at all, the loss must be durable and machine-readable PER STOCK —
    not a log line that dies at the next rotation plus a count. One
    `analysis_drop` row per name the seat was asked about, each carrying the
    reason, through the same writer the technical seat's row drops use."""
    from src.pipeline_stages import ANALYSIS_DROP_KIND

    coverage = NewsCoverage(configured=9, succeeded=9, failed=[])
    stage = _news_coverage_stage(lambda run_id, session: (None, coverage))

    ctx = RunContext.start("morning")
    ctx.positions = []
    ctx.admitted_symbols = {"AAPL", "MSFT"}
    result_ctx = stage.run(ctx)

    assert result_ctx.data_status["news"] == "parse_error"
    drop_calls = [
        c for c in stage.db.insert_specialist_evidence.call_args_list if c.kwargs.get("kind") == ANALYSIS_DROP_KIND
    ]
    assert {c.kwargs["symbol"] for c in drop_calls} == {"AAPL", "MSFT"}
    for call in drop_calls:
        payload = json.loads(call.kwargs["evidence_json"])
        assert payload["model"] == "NewsIntelligenceReport"
        assert payload["outcome"] == "dropped"
        assert "unreadable" in payload["reason"]
        # The record must say the seat is ABSENT, never that it is neutral.
        assert "not neutral" in payload["reason"]


def test_morning_research_stage_news_clean_answer_files_no_drop_records():
    """Control: a readable answer must file nothing."""
    from src.pipeline_stages import ANALYSIS_DROP_KIND

    coverage = NewsCoverage(configured=9, succeeded=9, failed=[])
    stage = _news_coverage_stage(
        lambda run_id, session: (_minimal_news_report(confidence="high"), coverage),
    )

    ctx = RunContext.start("morning")
    ctx.positions = []
    ctx.admitted_symbols = {"AAPL"}
    stage.run(ctx)

    assert not [
        c for c in stage.db.insert_specialist_evidence.call_args_list if c.kwargs.get("kind") == ANALYSIS_DROP_KIND
    ]


def test_new_buy_records_entry_atr_from_the_analysis():
    """A newly opened position must land with the ATR(14) it was sized against.

    Regression guard for a silent `getattr` default: the pinning read
    `getattr(decision, "atr_14", None)` but `TradeDecision` has no such
    field (ATR lives on the analysis / TechnicalIndicators), so the column
    was NULL on every row the desk ever wrote. The assertion below is on
    the value actually handed to `insert_trade`, not on the column existing.
    """
    from types import SimpleNamespace

    from src.models import PortfolioDecision, TradeDecision
    from src.pipeline_context import RunContext

    # The typo can only be caught if the wrong object genuinely lacks the
    # attribute — pin that, or a future field addition would hide it again.
    assert "atr_14" not in TradeDecision.model_fields

    pipeline = MagicMock()
    pipeline.broker.list_open_orders_checked.return_value = (True, [])  # listing read, nothing open
    pipeline.broker.get_latest_price.return_value = 100.0
    pipeline.broker.get_latest_quote.return_value = {"bid_price": 100.0, "ask_price": 100.0}
    pipeline.broker.submit_order.return_value = {
        "id": "order-1",
        "status": "accepted",
        "symbol": "SPY",
    }
    pipeline._format_qty = lambda q: str(q)
    pipeline._order_accepted.return_value = True
    pipeline._refresh_account_state.return_value = (
        {"cash": 50_000.0, "portfolio_value": 100_000.0},
        [],
        {},
    )

    ctx = RunContext.start("morning")
    ctx.cash = 50_000.0
    ctx.total_value = 100_000.0
    ctx.last_equity = 100_000.0
    ctx.positions = []
    ctx.analyses = [SimpleNamespace(symbol="SPY", atr_14=3.25)]
    ctx.portfolio_decision = PortfolioDecision(
        reasoning_chain=_pm_rc(),
        decisions=[
            TradeDecision(
                action="BUY",
                symbol="SPY",
                allocation_pct=10,
                entry_price=98.0,
                stop_loss=72.0,
                take_profit=140.0,
                reasoning="fresh setup",
                stop_rule="stop_honoured_at_computed_level",
            ),
        ],
        portfolio_view="test",
    )
    ctx.symbols_bars = {}

    ExecutionStage(pipeline=pipeline).run(ctx)

    opens = [c for c in pipeline.db.insert_trade.call_args_list if c.kwargs.get("action") == "BUY" or "BUY" in c.args]
    assert opens, "no opening row was inserted"
    kwargs = opens[0].kwargs
    assert kwargs.get("entry_atr") == 3.25
    # The stop rule travels on the decision and must survive the same hop.
    assert kwargs.get("stop_basis") == "stop_honoured_at_computed_level"
