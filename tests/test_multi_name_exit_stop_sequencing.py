"""A multi-name exit loop must re-protect each name before touching the next.

PR #542 fixed this for the two de-lever loops (`_enforce_gross_ceiling`,
`_force_delever`; see tests/test_gross_exposure_ladder.py). The same
all-at-once shape — cancel every name's protective stops and submit every
exit, then wait and rebuild coverage for the whole batch afterwards — was
also in:

* the midday/close position reviewer's executor
  (`TradingPipeline._midday_execute_llm_actions`), and
* the decision path's SELL and COVER loops (`ExecutionStage.run`,
  src/pipeline_stages.py).

These drive the REAL loops, the REAL `_submit_protected_sell` (snapshot ->
write-ahead -> cancel -> submit), the REAL `_cancel_stops_with_write_ahead`
and the REAL `_finalize_pending_protections`. Only the broker-edge seams are
recorded: the moment a name's stops are cancelled ("naked"), the moment an
exit is submitted ("submit"), and the moment finalize rebuilds that name's
coverage on its actual fill ("covered"). The defect is purely the ORDER of
those events.
"""

from tests.fakes.held_book import hold
from unittest.mock import MagicMock

from src.models import (
    Position,
    PortfolioDecision,
    PositionAction,
    ReasoningChain,
    TradeDecision,
)
from src.pipeline import TradingPipeline
from src.pipeline_context import RunContext
from src.pipeline_stages import ExecutionStage
from tests.pipeline_factory import build_pipeline
from tests.fakes.confirmed_cancel import confirm_cancels


def _pos(symbol: str, qty: float, entry: float, price: float) -> Position:
    return Position(
        symbol=symbol,
        qty=qty,
        avg_entry=entry,
        current_price=price,
        market_value=qty * price,
        unrealized_pnl=qty * (price - entry),
        sector="Technology",
    )


def _wire_broker_seams(broker, events: list[tuple[str, str]]) -> None:
    broker.snapshot_protective_stops.side_effect = lambda symbol, **_kw: (
        True,
        [{"id": f"stop-{symbol}", "stop_price": 80.0}],
    )

    def _cancel(symbol, _specs):
        events.append(("naked", symbol))
        return MagicMock(cleared=True)

    def _submit(*, symbol, side, **_kw):
        events.append(("submit", symbol))
        return {"id": f"ord-{symbol}", "symbol": symbol, "status": "accepted", "side": side}

    broker.cancel_snapshotted_stops.side_effect = _cancel
    broker.submit_order.side_effect = _submit
    broker.wait_for_order_terminal.return_value = "filled"
    confirm_cancels(broker)  # the broker confirms each stop cancel before the sale


def _recording_finalize(events: list[tuple[str, str]]) -> MagicMock:
    def _finalize(order_id, symbol, *_a, **_kw):
        events.append(("covered", symbol))
        return True, []

    return MagicMock(side_effect=_finalize)


def _assert_no_symbol_left_naked_while_another_is_touched(events):
    naked: set[str] = set()
    touched: list[str] = []
    for kind, symbol in events:
        if kind in ("naked", "submit"):
            others = naked - {symbol}
            assert not others, (
                f"{sorted(others)} still had NO protective stop when the "
                f"loop moved on to {symbol} — full timeline: {events}"
            )
            if kind == "naked":
                naked.add(symbol)
                touched.append(symbol)
        elif kind == "covered":
            naked.discard(symbol)
    assert not naked, f"{sorted(naked)} never had coverage rebuilt: {events}"
    return touched


# ---------------------------------------------------------------------------
# Midday / close position reviewer
# ---------------------------------------------------------------------------


def _midday_pipeline(events):
    pipeline = build_pipeline(broker=MagicMock())
    _wire_broker_seams(pipeline.broker, events)
    pipeline.db = MagicMock()
    pipeline.db.has_pending_action_for_symbol.return_value = False
    pipeline._order_accepted = MagicMock(return_value=True)
    pipeline._write_ahead_protection_restore = MagicMock(return_value=1)
    pipeline._format_qty = lambda q: str(q)
    pipeline._finalize_protection_after_sell = _recording_finalize(events)
    return pipeline


_TRIGGER = "thesis_invalid_if condition satisfied — thesis broken on filing."


def test_midday_reviewer_restores_each_stop_before_touching_the_next_name():
    """Two SELLs and a COVER from one review: each name's stop must be
    rebuilt on its actual fill before the next name's stops are cancelled.
    Before the fix, finalize ran once after the loop, so the first name rode
    naked through every later cancel and submit, and every later name sat
    uncovered through the earlier names' fill waits."""
    events: list[tuple[str, str]] = []
    pipeline = _midday_pipeline(events)
    positions = [
        _pos("NVDA", qty=10, entry=100.0, price=90.0),
        _pos("AMD", qty=20, entry=100.0, price=90.0),
        _pos("TSLA", qty=-15, entry=250.0, price=260.0),
    ]
    review = MagicMock(
        actions=[
            PositionAction(action="SELL", symbol="NVDA", reason=_TRIGGER),
            PositionAction(action="SELL", symbol="AMD", reason=_TRIGGER),
            PositionAction(action="COVER", symbol="TSLA", reason=_TRIGGER),
        ]
    )

    hold(pipeline.broker, {p.symbol: p.qty for p in positions})  # what the broker holds when the exit is re-sized
    orders = pipeline._midday_execute_llm_actions(positions, review, run_id="r1")

    touched = _assert_no_symbol_left_naked_while_another_is_touched(events)
    assert len(orders) == 3 and sorted(touched) == ["AMD", "NVDA", "TSLA"], events


def test_midday_reviewer_still_rebuilds_coverage_when_the_trade_row_fails():
    """The per-name finalize must still run when the ledger write after an
    accepted order raises — the stops were cancelled and the order is live."""
    events: list[tuple[str, str]] = []
    pipeline = _midday_pipeline(events)
    pipeline.db.insert_trade.side_effect = RuntimeError("db locked")
    positions = [
        _pos("NVDA", qty=10, entry=100.0, price=90.0),
        _pos("AMD", qty=20, entry=100.0, price=90.0),
    ]
    review = MagicMock(
        actions=[
            PositionAction(action="SELL", symbol="NVDA", reason=_TRIGGER),
            PositionAction(action="SELL", symbol="AMD", reason=_TRIGGER),
        ]
    )

    hold(pipeline.broker, {p.symbol: p.qty for p in positions})  # what the broker holds when the exit is re-sized
    pipeline._midday_execute_llm_actions(positions, review, run_id="r1")

    touched = _assert_no_symbol_left_naked_while_another_is_touched(events)
    assert sorted(touched) == ["AMD", "NVDA"], events


# ---------------------------------------------------------------------------
# ExecutionStage — the decision path's SELL and COVER loops
# ---------------------------------------------------------------------------


def _pm_rc() -> ReasoningChain:
    return ReasoningChain(
        macro_filter="x",
        news_check="x",
        earnings_check="x",
        signal_conflicts="x",
        sizing_logic="x",
        portfolio_balance="x",
        cash_target="x",
    )


def _exit_decision(action: str, symbol: str, pct: float) -> TradeDecision:
    return TradeDecision(
        action=action,
        symbol=symbol,
        allocation_pct=pct,
        entry_price=0.0,
        stop_loss=0.0,
        take_profit=0.0,
        reasoning="exit",
    )


def _execution_stage_pipeline(events, positions):
    """MagicMock pipeline (the convention tests/test_shorts_stage3.py uses
    for ExecutionStage) with the real SELL discipline bound onto it."""
    pipeline = MagicMock()
    pipeline._format_qty = lambda q: str(q)
    pipeline._order_accepted.return_value = True
    pipeline._full_sell_qty = TradingPipeline._full_sell_qty
    pipeline._refresh_account_state.return_value = (
        {"cash": 50_000.0, "portfolio_value": 100_000.0},
        positions,
        {},
    )
    _wire_broker_seams(pipeline.broker, events)
    pipeline._write_ahead_protection_restore.return_value = 1
    pipeline._finalize_protection_after_sell = _recording_finalize(events)
    for name in (
        "_submit_protected_sell",
        "_cancel_stops_with_write_ahead",
        "_finalize_pending_protections",
    ):
        setattr(pipeline, name, getattr(TradingPipeline, name).__get__(pipeline))
    return pipeline


def _execution_ctx(decisions, positions) -> RunContext:
    ctx = RunContext.start("morning")
    ctx.cash = 50_000.0
    ctx.total_value = 100_000.0
    ctx.last_equity = 100_000.0
    ctx.positions = positions
    ctx.portfolio_decision = PortfolioDecision(
        reasoning_chain=_pm_rc(),
        decisions=decisions,
        portfolio_view="test",
    )
    ctx.symbols_bars = {}
    return ctx


def test_execution_stage_sell_loop_restores_each_stop_before_the_next_name():
    events: list[tuple[str, str]] = []
    positions = [
        _pos("NVDA", qty=10, entry=100.0, price=90.0),
        _pos("AMD", qty=20, entry=100.0, price=90.0),
        _pos("MSFT", qty=30, entry=100.0, price=90.0),
    ]
    pipeline = _execution_stage_pipeline(events, positions)
    ctx = _execution_ctx(
        [
            _exit_decision("SELL", "NVDA", 100.0),
            _exit_decision("SELL", "AMD", 100.0),
            _exit_decision("SELL", "MSFT", 100.0),
        ],
        positions,
    )

    hold(pipeline.broker, {p.symbol: p.qty for p in positions})  # what the broker holds when the exit is re-sized
    orders = ExecutionStage(pipeline=pipeline).run(ctx)

    touched = _assert_no_symbol_left_naked_while_another_is_touched(events)
    assert len(orders) == 3 and touched == ["NVDA", "AMD", "MSFT"], events
    # Each SELL's terminal status is still recorded for the rotation gate.
    waited = [c.args[0] for c in pipeline.broker.wait_for_order_terminal.call_args_list]
    waited = [oid for oid in waited if oid not in pipeline.broker.cancelled_stop_ids]  # SELL waits only
    assert waited == ["ord-NVDA", "ord-AMD", "ord-MSFT"]


def test_execution_stage_cover_loop_restores_each_stop_before_the_next_name():
    events: list[tuple[str, str]] = []
    positions = [
        _pos("TSLA", qty=-40, entry=250.0, price=260.0),
        _pos("RIVN", qty=-30, entry=20.0, price=21.0),
    ]
    pipeline = _execution_stage_pipeline(events, positions)
    ctx = _execution_ctx(
        [
            _exit_decision("COVER", "TSLA", 100.0),
            _exit_decision("COVER", "RIVN", 100.0),
        ],
        positions,
    )

    hold(pipeline.broker, {p.symbol: p.qty for p in positions})  # what the broker holds when the exit is re-sized
    orders = ExecutionStage(pipeline=pipeline).run(ctx)

    touched = _assert_no_symbol_left_naked_while_another_is_touched(events)
    assert len(orders) == 2 and touched == ["TSLA", "RIVN"], events
