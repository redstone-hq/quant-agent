"""Long scale-in path B: cancel-confirm-buy-rearm.

Owner ruling 2026-09-15. These tests pin the objections that killed silent
refuse-adds and that killed the daily-breaker's cancel-restore window:

  * cancel is confirmed via wait_for_order_terminal (trade_updates / REST),
    not assumed from cancel_order_by_id returning;
  * a partial fill of the add rearms at the broker's FULL position qty;
  * rearm failure pages the owner and does not go quiet;
  * short adds are blocked (scale-in is the long path; item 73 closed
    the missing-buy-stop repair, not short adds);
  * execution.repeg_enabled stays false.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from src.config import ExecutionConfig
from src.execution.scale_in import (
    WAL_SCALE_IN_SENTINEL,
    confirm_protective_cancels,
    cover_qty_for_rearm,
    drain_scale_in_row,
    most_protective_long_stop,
    most_protective_short_stop,
    pending_protection_symbols,
    prepare_long_add,
    prepare_short_add,
)
from src.models import PortfolioDecision, Position, ReasoningChain, TradeDecision
from src.pipeline import TradingPipeline
from src.pipeline_context import RunContext
from src.pipeline_stages import ExecutionStage, _alert_owner_protection_failed
from src.storage.db import Database
from tests.pipeline_factory import build_pipeline


def _rc() -> ReasoningChain:
    return ReasoningChain(
        macro_filter="m",
        news_check="n",
        earnings_check="e",
        signal_conflicts="s",
        sizing_logic="z",
        portfolio_balance="b",
        cash_target="c",
    )


def _cop_position(qty=10.0) -> Position:
    return Position(
        symbol="COP",
        qty=qty,
        avg_entry=90.0,
        current_price=100.0,
        market_value=qty * 100.0,
        unrealized_pnl=qty * 10.0,
        sector="Energy",
    )


def _db(tmp_path) -> Database:
    db = Database(str(tmp_path / "scale_in.db"))
    db.initialize()
    return db


def _pipeline(live_price=100.0, cash=50_000.0, positions=None):
    pipeline = MagicMock()
    held = list(positions or [])
    pipeline.broker.get_latest_price.return_value = live_price
    pipeline.broker.get_latest_quote.return_value = {
        "bid_price": round(live_price * 0.999, 4),
        "ask_price": round(live_price * 1.001, 4),
    }
    pipeline.broker.get_fractionability.return_value = {
        "fractionable": False,
        "reason": "test",
    }
    pipeline._format_qty = lambda q: str(q)
    pipeline._order_accepted.return_value = True
    pipeline._refresh_account_state.return_value = (
        {"cash": cash, "portfolio_value": 100_000.0},
        held,
        {},
    )
    return pipeline


def _ctx(decisions, positions=None, cash=50_000.0) -> RunContext:
    ctx = RunContext.start("morning")
    ctx.cash = cash
    ctx.total_value = 100_000.0
    ctx.last_equity = 100_000.0
    ctx.positions = positions or []
    ctx.decision_id = "run-scale-in"
    ctx.portfolio_decision = PortfolioDecision(
        reasoning_chain=_rc(),
        decisions=decisions,
        portfolio_view="t",
    )
    ctx.symbols_bars = {}
    return ctx


def _buy_cop() -> TradeDecision:
    return TradeDecision(
        action="BUY",
        symbol="COP",
        allocation_pct=10,
        entry_price=100.0,
        stop_loss=90.0,
        take_profit=120.0,
        reasoning="add to winner",
    )


def _short_cop_position(qty=-10.0) -> Position:
    """A held SHORT in COP (negative broker-signed qty)."""
    return Position(
        symbol="COP",
        qty=qty,
        avg_entry=100.0,
        current_price=90.0,
        market_value=qty * 90.0,
        unrealized_pnl=abs(qty) * 10.0,
        sector="Energy",
    )


def _short_cop() -> TradeDecision:
    # Short stop is ABOVE entry; add's own stop 110, resting buy-stop set per-test.
    return TradeDecision(
        action="SHORT",
        symbol="COP",
        allocation_pct=10,
        entry_price=100.0,
        stop_loss=110.0,
        take_profit=80.0,
        reasoning="add to short",
    )


def _shortable(pipeline):
    pipeline.broker.get_shortability.return_value = {
        "shortable": True,
        "easy_to_borrow": True,
        "reason": "eligible",
    }
    # The wash-trade guard asks (fail-closed) for foreign working BUYs;
    # (True, []) = listing succeeded, none present.
    pipeline.broker.list_open_entry_orders_checked.return_value = (True, [])
    pipeline.broker.list_open_entry_order_ids.return_value = []
    return pipeline


# --------------------------------------------------------------------------
# primitives
# --------------------------------------------------------------------------


def test_repeg_enabled_stays_false():
    assert ExecutionConfig().repeg_enabled is False


def test_most_protective_long_stop_is_the_highest_trigger():
    assert most_protective_long_stop([88.0, 92.0, 90.0]) == 92.0
    assert most_protective_long_stop([]) == 0.0


def test_confirm_protective_cancels_requires_terminal_cancelled_not_assumption():
    broker = MagicMock()
    broker.wait_for_order_terminal.return_value = "canceled"
    ok, detail = confirm_protective_cancels(
        broker,
        [{"id": "stop-1", "qty": 10, "stop_price": 90.0}],
    )
    assert ok is True
    assert detail == ""
    broker.wait_for_order_terminal.assert_called_once_with("stop-1")


def test_confirm_protective_cancels_refuses_when_status_is_unknown():
    broker = MagicMock()
    broker.wait_for_order_terminal.return_value = None
    ok, detail = confirm_protective_cancels(
        broker,
        [{"id": "stop-1", "qty": 10, "stop_price": 90.0}],
    )
    assert ok is False
    assert "not confirmed cancelled" in detail


def test_confirm_protective_cancels_aborts_when_the_stop_fills():
    broker = MagicMock()
    broker.wait_for_order_terminal.return_value = "filled"
    ok, detail = confirm_protective_cancels(
        broker,
        [{"id": "stop-1", "qty": 10, "stop_price": 90.0}],
    )
    assert ok is False
    assert "FILLED" in detail


def test_cover_qty_uses_broker_full_position_not_the_add_fill():
    broker = MagicMock()
    broker.get_positions.return_value = [_cop_position(qty=13.0)]
    qty = cover_qty_for_rearm(
        broker,
        symbol="COP",
        filled_qty=3.0,
        held_qty_before=10.0,
    )
    assert qty == 13.0


def test_cover_qty_falls_back_to_filled_plus_held_when_broker_unreadable():
    broker = MagicMock()
    broker.get_positions.side_effect = RuntimeError("broker down")
    qty = cover_qty_for_rearm(
        broker,
        symbol="COP",
        filled_qty=3.0,
        held_qty_before=10.0,
    )
    assert qty == 13.0


# --------------------------------------------------------------------------
# prepare: WAL then cancel then confirm
# --------------------------------------------------------------------------


def test_prepare_long_add_is_noop_when_the_name_is_not_held():
    broker = MagicMock()
    prep = prepare_long_add(
        broker=broker,
        db=MagicMock(),
        symbol="COP",
        positions=[],
        intended_stop=90.0,
    )
    assert prep.is_scale_in is False
    broker.snapshot_protective_stops.assert_not_called()


def test_prepare_skips_buy_when_cancel_is_not_confirmed(tmp_path):
    db = _db(tmp_path)
    broker = MagicMock()
    broker.snapshot_protective_stops.return_value = (
        True,
        [{"id": "stop-1", "qty": 10, "stop_price": 88.0}],
    )
    broker.cancel_snapshotted_stops.return_value = MagicMock(cleared=True)
    broker.wait_for_order_terminal.return_value = "pending_cancel"
    broker._restore_stop_orders.return_value = (1, [])

    prep = prepare_long_add(
        broker=broker,
        db=db,
        symbol="COP",
        positions=[_cop_position()],
        intended_stop=90.0,
    )
    assert prep.skip_reason == "scale_in_cancel_unconfirmed"
    assert prep.cancelled is False
    broker.submit_order.assert_not_called()
    broker._restore_stop_orders.assert_called()
    assert db.get_pending_protection_restores() == []
    db.close()


def test_prepare_writes_wal_before_cancel(tmp_path):
    db = _db(tmp_path)
    broker = MagicMock()
    broker.snapshot_protective_stops.return_value = (
        True,
        [{"id": "stop-1", "qty": 10, "stop_price": 88.0}],
    )
    seen_at_cancel = {}

    def _cancel(symbol, specs):
        seen_at_cancel["rows"] = db.get_pending_protection_restores()
        return MagicMock(cleared=True)

    broker.cancel_snapshotted_stops.side_effect = _cancel
    broker.wait_for_order_terminal.return_value = "canceled"

    prep = prepare_long_add(
        broker=broker,
        db=db,
        symbol="COP",
        positions=[_cop_position()],
        intended_stop=90.0,
    )
    assert prep.cancelled is True
    assert prep.skip_reason is None
    assert seen_at_cancel["rows"][0]["sell_order_id"] == WAL_SCALE_IN_SENTINEL
    assert prep.intended_stop == 90.0  # add's stop tighter than 88
    db.close()


# --------------------------------------------------------------------------
# ExecutionStage sequence
# --------------------------------------------------------------------------


def test_execution_stage_cancels_then_submits_buy_add():
    held = [_cop_position()]
    pipeline = _pipeline(positions=held)
    stop = {"id": "stop-cop", "qty": 10, "stop_price": 88.0}
    pipeline.broker.snapshot_protective_stops.return_value = (True, [stop])
    pipeline.broker.cancel_snapshotted_stops.return_value = MagicMock(cleared=True)

    def _wait(order_id, *args, **kwargs):
        return "canceled" if "stop" in str(order_id) else "filled"

    pipeline.broker.wait_for_order_terminal.side_effect = _wait
    pipeline.broker.submit_order.return_value = {
        "id": "buy-cop",
        "status": "accepted",
        "symbol": "COP",
        "pending_stop_price": 90.0,
    }
    pipeline.broker.place_entry_protection.return_value = {"id": "stop-new"}
    pipeline.db.insert_pending_protection_restore.return_value = 7
    pipeline.db.insert_trade.return_value = 1

    ctx = _ctx([_buy_cop()], positions=held)
    orders = ExecutionStage(pipeline=pipeline).run(ctx)

    assert len(orders) == 1
    pipeline.broker.cancel_snapshotted_stops.assert_called()
    pipeline.broker.submit_order.assert_called()
    submit_kwargs = pipeline.broker.submit_order.call_args.kwargs
    assert submit_kwargs["symbol"] == "COP"
    assert submit_kwargs["side"] == "buy"
    protect_kwargs = pipeline.broker.place_entry_protection.call_args.kwargs
    assert protect_kwargs["cover_full_position"] is True
    assert protect_kwargs["held_qty_before"] == 10.0
    assert ctx.execution_skips == []


def test_scale_in_add_carries_the_pinned_setup_type_not_todays_reread():
    """Item 82: `setup_type` is pinned on the trade row at ENTRY and must
    never be reclassified by a later scale-in add.

    COP was originally entered as a "breakout" (Type B — no overhead
    structure, progress/pace disabled, managed by trailing only). Today's
    fresh Technical read for the same symbol has drifted to "range" as the
    chart evolved — a routine, unremarkable occurrence, not itself a bug.
    Before the fix, the execution stage re-derived `setup_type` from
    TODAY's `ctx.analyses` on every BUY, including this scale-in add, and
    wrote "range" onto the new (and now newest) trade row — silently
    stripping the breakout's protection from every consumer that reads
    `get_symbol_last_buy` (position-review pace/progress, the trailing
    stop, target revision). The fix carries the EXISTING pinned value
    forward unchanged on a scale-in add.
    """
    held = [_cop_position()]
    pipeline = _pipeline(positions=held)
    stop = {"id": "stop-cop", "qty": 10, "stop_price": 88.0}
    pipeline.broker.snapshot_protective_stops.return_value = (True, [stop])
    pipeline.broker.cancel_snapshotted_stops.return_value = MagicMock(cleared=True)
    pipeline.broker.wait_for_order_terminal.side_effect = lambda order_id, *a, **k: (
        "canceled" if "stop" in str(order_id) else "filled"
    )
    pipeline.broker.submit_order.return_value = {
        "id": "buy-cop",
        "status": "accepted",
        "symbol": "COP",
        "pending_stop_price": 90.0,
    }
    pipeline.broker.place_entry_protection.return_value = {"id": "stop-new"}
    pipeline.db.insert_pending_protection_restore.return_value = 7
    pipeline.db.insert_trade.return_value = 1
    # The ORIGINAL entry's pinned row — this is what a real
    # `get_symbol_last_buy` would return before today's add is recorded.
    pipeline.db.get_symbol_last_buy.return_value = {
        "setup_type": "breakout",
        "take_profit": 120.0,
    }

    from src.models import TechAnalysisResult, TechReasoningChain

    todays_reread = TechAnalysisResult(
        symbol="COP",
        rating="buy",
        conviction="medium",
        entry_price=100.0,
        stop_loss=95.0,
        reference_target=110.0,
        support_levels=[95.0],
        resistance_levels=[110.0],
        # Deliberately DIFFERENT from the pinned "breakout" — this is
        # today's independent re-classification, not a re-assertion of
        # the entry-day one.
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

    ctx = _ctx([_buy_cop()], positions=held)
    ctx.analyses = [todays_reread]
    orders = ExecutionStage(pipeline=pipeline).run(ctx)

    assert len(orders) == 1
    insert_kwargs = pipeline.db.insert_trade.call_args.kwargs
    assert insert_kwargs["setup_type"] == "breakout", (
        "scale-in add reclassified a pinned setup_type instead of "
        f"carrying it forward: got {insert_kwargs['setup_type']!r}"
    )


def test_new_entry_reads_setup_type_from_the_decision_not_a_second_lookup():
    """Item 82: a genuinely new entry has no prior pinned row, so
    `setup_type` must come from the single value the constructor already
    classified onto `TradeDecision.setup_type` — not from a second,
    independent lookup into `ctx.analyses`. `isinstance` on the resulting
    value keeps this gated on a real classification, never a MagicMock
    truthiness accident (~58 existing tests drive a MagicMock broker)."""
    pipeline = _pipeline(positions=[])
    pipeline.broker.submit_order.return_value = {
        "id": "buy-cop",
        "status": "accepted",
        "symbol": "COP",
        "pending_stop_price": 90.0,
    }
    pipeline.broker.place_entry_protection.return_value = {"id": "stop-new"}
    pipeline.db.insert_trade.return_value = 1

    decision = TradeDecision(
        action="BUY",
        symbol="COP",
        allocation_pct=10,
        entry_price=100.0,
        stop_loss=90.0,
        take_profit=120.0,
        reasoning="new breakout entry",
        setup_type="breakout",
    )
    from src.models import TechAnalysisResult, TechReasoningChain

    # A DIFFERENT analysis for the same symbol, standing in for whatever
    # `ctx.analyses` happens to hold by execution time — the point is that
    # it must not be consulted for setup_type on a new entry either.
    mismatched_analysis = TechAnalysisResult(
        symbol="COP",
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

    ctx = _ctx([decision], positions=[])
    ctx.analyses = [mismatched_analysis]
    ExecutionStage(pipeline=pipeline).run(ctx)

    insert_kwargs = pipeline.db.insert_trade.call_args.kwargs
    assert isinstance(insert_kwargs["setup_type"], str)
    assert insert_kwargs["setup_type"] == "breakout"


def test_execution_stage_rearms_at_the_tighter_of_cancelled_and_add_stop():
    """A looser add stop must not replace a tighter cancelled protective sell."""
    held = [_cop_position()]
    pipeline = _pipeline(positions=held)
    stop = {"id": "stop-cop", "qty": 10, "stop_price": 92.0}
    pipeline.broker.snapshot_protective_stops.return_value = (True, [stop])
    pipeline.broker.cancel_snapshotted_stops.return_value = MagicMock(cleared=True)
    pipeline.broker.wait_for_order_terminal.side_effect = lambda order_id, *a, **k: (
        "canceled" if "stop" in str(order_id) else "filled"
    )
    pipeline.broker.submit_order.return_value = {
        "id": "buy-cop",
        "status": "accepted",
        "symbol": "COP",
        "pending_stop_price": 90.0,
    }
    pipeline.broker.place_entry_protection.return_value = {"id": "stop-new"}
    pipeline.db.insert_pending_protection_restore.return_value = 7
    pipeline.db.insert_trade.return_value = 1

    ctx = _ctx([_buy_cop()], positions=held)
    ExecutionStage(pipeline=pipeline).run(ctx)

    protect_kwargs = pipeline.broker.place_entry_protection.call_args.kwargs
    assert protect_kwargs["stop_price"] == 92.0
    assert protect_kwargs["cover_full_position"] is True


def test_submit_exception_leaves_wal_instead_of_restoring_old_stop_size():
    """Unknown submit must not put the cancelled stop back at the pre-add qty."""
    held = [_cop_position()]
    pipeline = _pipeline(positions=held)
    stop = {"id": "stop-cop", "qty": 10, "stop_price": 88.0}
    pipeline.broker.snapshot_protective_stops.return_value = (True, [stop])
    pipeline.broker.cancel_snapshotted_stops.return_value = MagicMock(cleared=True)
    pipeline.broker.wait_for_order_terminal.return_value = "canceled"
    pipeline.broker.submit_order.side_effect = RuntimeError("broker timeout")
    pipeline.broker._restore_stop_orders.return_value = (1, [])
    pipeline.db.insert_pending_protection_restore.return_value = 7
    pipeline.db.insert_trade.return_value = 1

    ctx = _ctx([_buy_cop()], positions=held)
    orders = ExecutionStage(pipeline=pipeline).run(ctx)

    assert orders == []
    pipeline.broker.submit_order.assert_called()
    pipeline.broker._restore_stop_orders.assert_not_called()
    pipeline.db.delete_pending_protection_restore.assert_not_called()


def test_execution_stage_does_not_submit_when_cancel_unconfirmed():
    held = [_cop_position()]
    pipeline = _pipeline(positions=held)
    pipeline.broker.snapshot_protective_stops.return_value = (
        True,
        [{"id": "stop-cop", "qty": 10, "stop_price": 88.0}],
    )
    pipeline.broker.cancel_snapshotted_stops.return_value = MagicMock(cleared=True)
    pipeline.broker.wait_for_order_terminal.return_value = None
    pipeline.broker._restore_stop_orders.return_value = (1, [])
    pipeline.db.insert_pending_protection_restore.return_value = 3

    ctx = _ctx([_buy_cop()], positions=held)
    orders = ExecutionStage(pipeline=pipeline).run(ctx)

    assert orders == []
    pipeline.broker.submit_order.assert_not_called()
    assert ctx.execution_skips[0]["reason"] == "scale_in_cancel_unconfirmed"


# --------------------------------------------------------------------------
# SHORT scale-in: mirror of the long path (owner-approved)
# --------------------------------------------------------------------------


def test_most_protective_short_stop_is_the_lowest_trigger():
    # Lowest trigger above entry covers the short SOONEST.
    assert most_protective_short_stop([112.0, 108.0, 110.0]) == 108.0
    assert most_protective_short_stop([]) == 0.0


def test_cover_qty_short_fallback_covers_full_enlarged_short_not_just_add():
    """Adversary bug: a short's held_qty_before is NEGATIVE, so the old
    fallback ``filled + max(0.0, held)`` covered only the ADD and left the
    existing short leg naked. The fix covers |filled| + |held|."""
    broker = MagicMock()
    broker.get_positions.side_effect = RuntimeError("broker down")
    qty = cover_qty_for_rearm(
        broker,
        symbol="COP",
        filled_qty=5.0,
        held_qty_before=-10.0,
    )
    assert qty == 15.0  # NOT 5.0 (the add alone), NOT 0.0


def test_prepare_short_add_is_noop_when_the_name_is_not_short():
    broker = MagicMock()
    # Held LONG: not a short scale-in — caller opens a new short unchanged.
    prep = prepare_short_add(
        broker=broker,
        db=MagicMock(),
        symbol="COP",
        positions=[_cop_position(qty=10.0)],
        intended_stop=110.0,
    )
    assert prep.is_scale_in is False
    broker.snapshot_protective_stops.assert_not_called()


def test_prepare_short_add_refuses_on_a_foreign_working_buy(tmp_path):
    """Wash-trade guard: a resting BUY (cover-limit / take-profit) would
    collide with the SELL add, so the add is refused BEFORE any buy-stop is
    cancelled."""
    db = _db(tmp_path)
    broker = MagicMock()
    broker.list_open_entry_orders_checked.return_value = (True, ["foreign-buy-1"])

    prep = prepare_short_add(
        broker=broker,
        db=db,
        symbol="COP",
        positions=[_short_cop_position()],
        intended_stop=110.0,
    )
    assert prep.skip_reason == "short_add_foreign_buy"
    assert prep.cancelled is False
    broker.snapshot_protective_stops.assert_not_called()
    broker.cancel_snapshotted_stops.assert_not_called()
    broker.list_open_entry_orders_checked.assert_called_once_with("COP", side="buy")
    db.close()


def test_prepare_short_add_fails_closed_when_the_order_listing_errors(tmp_path):
    """H4: the wash guard must NOT cancel protection when it cannot VERIFY
    there is no colliding BUY — a listing failure (ok=False) refuses the add
    rather than betting Alpaca bounces a self-cross."""
    db = _db(tmp_path)
    broker = MagicMock()
    # Listing could not be read (API error surfaced as ok=False, not empty).
    broker.list_open_entry_orders_checked.return_value = (False, [])

    prep = prepare_short_add(
        broker=broker,
        db=db,
        symbol="COP",
        positions=[_short_cop_position()],
        intended_stop=110.0,
    )
    assert prep.skip_reason == "short_add_wash_guard_unverified"
    assert prep.cancelled is False
    broker.snapshot_protective_stops.assert_not_called()
    broker.cancel_snapshotted_stops.assert_not_called()
    assert db.get_pending_protection_restores() == []
    db.close()


def test_prepare_short_add_aborts_when_the_buy_stop_fills(tmp_path):
    """A BUY-stop that FIRED during the cancel means the short was COVERED.
    The add aborts, the buy-stop is NOT restored onto a (now flat) name, and
    the WAL row is discharged."""
    db = _db(tmp_path)
    broker = MagicMock()
    broker.list_open_entry_orders_checked.return_value = (True, [])
    broker.snapshot_protective_stops.return_value = (
        True,
        [{"id": "bstop-1", "qty": 10, "stop_price": 108.0}],
    )
    broker.cancel_snapshotted_stops.return_value = MagicMock(cleared=True)
    broker.wait_for_order_terminal.return_value = "filled"
    # Position re-read confirms FLAT — the stop covered the whole short.
    broker.get_positions.return_value = []

    prep = prepare_short_add(
        broker=broker,
        db=db,
        symbol="COP",
        positions=[_short_cop_position()],
        intended_stop=110.0,
    )
    assert prep.skip_reason == "scale_in_stop_filled"
    assert prep.cancelled is False
    broker._restore_stop_orders.assert_not_called()  # do NOT re-arm a flat name
    assert db.get_pending_protection_restores() == []
    db.close()


def test_prepare_short_add_fill_but_not_flat_does_not_leave_a_sibling_naked(tmp_path):
    """H2: with two protective buy-stops, one FIRES during the cancel and the
    other was cancelled — the short is only PARTLY covered. The prep must NOT
    discharge the WAL restoring nothing: it rearms a buy-stop over the
    remaining short (and keeps the WAL if it cannot), so no lot is left naked.
    """
    db = _db(tmp_path)
    broker = MagicMock()
    broker.list_open_entry_orders_checked.return_value = (True, [])
    broker.snapshot_protective_stops.return_value = (
        True,
        [
            {"id": "bstop-lot1", "qty": 10, "stop_price": 108.0},
            {"id": "bstop-lot2", "qty": 5, "stop_price": 112.0},
        ],
    )
    broker.cancel_snapshotted_stops.return_value = MagicMock(cleared=True)
    # First spec FILLED, sibling canceled — abort-on-fill triggers.
    broker.wait_for_order_terminal.side_effect = lambda oid, *a, **k: "filled" if "lot1" in str(oid) else "canceled"
    # Position re-read: still SHORT 5 (lot2 not covered).
    broker.get_positions.return_value = [_short_cop_position(qty=-5.0)]
    broker._submit_protective_stop_retrying.return_value = {
        "id": "bstop-rearm",
        "uncovered_qty": 0.0,
    }
    broker.STOP_LIMIT_BUFFER_PCT = 0.03

    prep = prepare_short_add(
        broker=broker,
        db=db,
        symbol="COP",
        positions=[_short_cop_position(qty=-15.0)],
        intended_stop=110.0,
    )
    assert prep.skip_reason == "scale_in_stop_filled_partial"
    assert prep.cancelled is False
    # The remaining short (5) was rearmed on the BUY side — not left naked.
    kwargs = broker._submit_protective_stop_retrying.call_args.kwargs
    assert kwargs["qty"] == 5.0
    assert kwargs["side"] == "buy"
    db.close()


def test_prepare_short_add_fill_not_flat_and_no_rearm_keeps_the_wal(tmp_path):
    """H2: if the short is not confirmed flat AND the in-session rearm cannot
    land, the WAL row is KEPT (not discharged) so drain finishes the job."""
    db = _db(tmp_path)
    broker = MagicMock()
    broker.list_open_entry_orders_checked.return_value = (True, [])
    broker.snapshot_protective_stops.return_value = (
        True,
        [{"id": "bstop-1", "qty": 15, "stop_price": 108.0}],
    )
    broker.cancel_snapshotted_stops.return_value = MagicMock(cleared=True)
    broker.wait_for_order_terminal.return_value = "filled"
    # Broker position UNREADABLE (cannot confirm flat) and rearm fails.
    broker.get_positions.side_effect = RuntimeError("broker down")
    broker._submit_protective_stop_retrying.return_value = None
    broker.STOP_LIMIT_BUFFER_PCT = 0.03

    prep = prepare_short_add(
        broker=broker,
        db=db,
        symbol="COP",
        positions=[_short_cop_position(qty=-15.0)],
        intended_stop=110.0,
    )
    assert prep.skip_reason == "scale_in_stop_filled_partial"
    # WAL row kept — the remaining short is not stranded without recovery.
    assert db.get_pending_protection_restores() != []
    db.close()


def test_prepare_short_add_snapshots_and_wals_on_the_buy_side(tmp_path):
    db = _db(tmp_path)
    broker = MagicMock()
    broker.list_open_entry_orders_checked.return_value = (True, [])
    broker.snapshot_protective_stops.return_value = (
        True,
        [{"id": "bstop-1", "qty": 10, "stop_price": 108.0}],
    )
    broker.cancel_snapshotted_stops.return_value = MagicMock(cleared=True)
    broker.wait_for_order_terminal.return_value = "canceled"

    prep = prepare_short_add(
        broker=broker,
        db=db,
        symbol="COP",
        positions=[_short_cop_position()],
        intended_stop=110.0,
    )
    assert prep.cancelled is True
    assert prep.side == "buy"
    assert prep.held_qty_before == -10.0
    # Most-protective short = LOWEST of the resting 108 and the add's 110.
    assert prep.intended_stop == 108.0
    broker.snapshot_protective_stops.assert_called_once_with("COP", side="buy")
    rows = db.get_pending_protection_restores()
    assert rows[0]["side"] == "buy"
    assert rows[0]["sell_order_id"] == WAL_SCALE_IN_SENTINEL
    db.close()


def test_execution_stage_cancels_buy_stop_then_submits_sell_add_and_rearms():
    """Full short scale-in through the stage: cancel the buy-stop, confirm,
    submit the SELL add, rearm a buy-stop over the FULL short."""
    held = [_short_cop_position(qty=-10.0)]
    pipeline = _shortable(_pipeline(positions=held))
    # Resting buy-stop at 108; add's own stop 110 → most-protective = 108.
    stop = {"id": "bstop-cop", "qty": 10, "stop_price": 108.0}
    pipeline.broker.snapshot_protective_stops.return_value = (True, [stop])
    pipeline.broker.cancel_snapshotted_stops.return_value = MagicMock(cleared=True)
    pipeline.broker.wait_for_order_terminal.side_effect = lambda order_id, *a, **k: (
        "canceled" if "stop" in str(order_id) else "filled"
    )
    pipeline.broker.submit_order.return_value = {
        "id": "sell-cop",
        "status": "accepted",
        "symbol": "COP",
        "pending_stop_price": 110.0,
    }
    pipeline.broker.place_entry_protection.return_value = {"id": "bstop-new"}
    pipeline.db.insert_pending_protection_restore.return_value = 7
    pipeline.db.insert_trade.return_value = 1

    ctx = _ctx([_short_cop()], positions=held)
    orders = ExecutionStage(pipeline=pipeline).run(ctx)

    assert len(orders) == 1
    pipeline.broker.cancel_snapshotted_stops.assert_called()
    submit_kwargs = pipeline.broker.submit_order.call_args.kwargs
    assert submit_kwargs["symbol"] == "COP"
    assert submit_kwargs["side"] == "sell_short"
    protect_kwargs = pipeline.broker.place_entry_protection.call_args.kwargs
    assert protect_kwargs["cover_full_position"] is True
    assert protect_kwargs["held_qty_before"] == -10.0
    assert protect_kwargs["side"] == "sell_short"
    assert protect_kwargs["stop_price"] == 108.0  # lowest = most protective
    # snapshot for a short reads the BUY-stop side.
    pipeline.broker.snapshot_protective_stops.assert_any_call("COP", side="buy")
    assert ctx.execution_skips == []


def test_short_add_below_floor_is_dropped_before_any_buy_stop_is_cancelled():
    """The owner's minimum-risk floor (0.5% of equity, owner rule
    2026-08-27) MUST run before the protective buy-stop comes off. Was the
    deleted $500 notional floor; the same tiny add is now refused because the
    13-share short risks ~0.1% of the $100k book at its stop."""
    held = [_short_cop_position(qty=-10.0)]
    pipeline = _shortable(_pipeline(positions=held))
    pipeline.broker.snapshot_protective_stops.return_value = (
        True,
        [{"id": "bstop-cop", "qty": 10, "stop_price": 108.0}],
    )

    ctx = _ctx([_short_cop()], positions=held)
    # Force a tiny order: (10 held + 3) shares well under 0.5% risk.
    with patch("src.pipeline_stages._size_shares", return_value=3.0):
        orders = ExecutionStage(pipeline=pipeline).run(ctx)

    assert orders == []
    pipeline.broker.submit_order.assert_not_called()
    # The protection is still standing — the floor gate ran PRE-cancel.
    pipeline.broker.cancel_snapshotted_stops.assert_not_called()
    assert ctx.execution_skips[0]["reason"] == "below_owner_min_risk"


def test_drain_scale_in_rearms_broker_full_short_qty_on_the_buy_side(tmp_path):
    """Buy-side WAL crash recovery: rearm a buy-stop over the full short."""
    db = _db(tmp_path)
    row_id = db.insert_pending_protection_restore(
        symbol="COP",
        sell_order_id=WAL_SCALE_IN_SENTINEL,
        position_qty_before_sell=-10.0,
        specs_json=json.dumps(
            [
                {"id": "bstop-1", "qty": 10, "stop_price": 112.0},
                {"id": None, "qty": 0, "stop_price": 110.0, "role": "intended"},
            ]
        ),
        side="buy",
    )
    pipeline = build_pipeline(db=db, broker=MagicMock())
    pipeline.broker.list_open_entry_order_ids.return_value = []
    pipeline.broker.get_positions.return_value = [_short_cop_position(qty=-13.0)]
    pipeline.broker.snapshot_protective_stops.return_value = (True, [])
    pipeline.broker._submit_protective_stop_retrying.return_value = {
        "id": "bstop-rearm",
        "uncovered_qty": 0.0,
    }
    pipeline.broker.STOP_LIMIT_BUFFER_PCT = 0.03

    drained = pipeline._drain_pending_protection_restores()
    assert drained == 1
    kwargs = pipeline.broker._submit_protective_stop_retrying.call_args.kwargs
    assert kwargs["qty"] == 13.0  # full enlarged short, not the add
    assert kwargs["stop_price"] == 110.0  # LOWEST stored = most protective
    assert kwargs["side"] == "buy"  # buy-stop protects a short
    # Buy needs UP-headroom: limit ABOVE the trigger.
    assert kwargs["limit_price"] == 110.0 * 1.03
    assert db.get_pending_protection_restores() == []
    db.close()
    assert row_id


def test_drain_classifies_short_from_qty_sign_not_the_side_column(tmp_path):
    """H3: a scale-in row must never be read with the generic position/close
    side meaning. Even with a MISLEADING `side` column, drain rearms on the
    buy side because it derives long/short from the SIGN of the stored qty."""
    db = _db(tmp_path)
    db.insert_pending_protection_restore(
        symbol="COP",
        sell_order_id=WAL_SCALE_IN_SENTINEL,
        position_qty_before_sell=-13.0,  # NEGATIVE => short
        specs_json=json.dumps([{"id": "b1", "qty": 13, "stop_price": 110.0}]),
        side="sell",  # deliberately WRONG column
    )
    pipeline = build_pipeline(db=db, broker=MagicMock())
    pipeline.broker.list_open_entry_order_ids.return_value = []
    pipeline.broker.get_positions.return_value = [_short_cop_position(qty=-13.0)]
    pipeline.broker.snapshot_protective_stops.return_value = (True, [])
    pipeline.broker._submit_protective_stop_retrying.return_value = {
        "id": "b-rearm",
        "uncovered_qty": 0.0,
    }
    pipeline.broker.STOP_LIMIT_BUFFER_PCT = 0.03

    drained = pipeline._drain_pending_protection_restores()
    assert drained == 1
    kwargs = pipeline.broker._submit_protective_stop_retrying.call_args.kwargs
    assert kwargs["side"] == "buy"  # short, despite side="sell" column
    assert kwargs["qty"] == 13.0
    pipeline.broker.snapshot_protective_stops.assert_called_with("COP", side="buy")
    db.close()


def test_emergency_cover_after_rearm_fails_covers_the_full_enlarged_short():
    """H1: when the full-position rearm returns None on a short SCALE-IN, the
    D7 emergency market cover must buy the ENLARGED short (broker current qty,
    e.g. 15), NOT just the add's fill (5) — the pre-existing leg's buy-stop was
    already cancelled and would otherwise be left naked."""
    held = [_short_cop_position(qty=-10.0)]
    pipeline = _shortable(_pipeline(positions=held))
    stop = {"id": "bstop-cop", "qty": 10, "stop_price": 108.0}
    pipeline.broker.snapshot_protective_stops.return_value = (True, [stop])
    pipeline.broker.cancel_snapshotted_stops.return_value = MagicMock(cleared=True)
    pipeline.broker.wait_for_order_terminal.side_effect = lambda oid, *a, **k: (
        "canceled" if "stop" in str(oid) else "filled"
    )

    def _submit(*args, **kwargs):
        if kwargs.get("side") == "buy":  # emergency cover leg
            return {"id": "cover-1", "status": "accepted"}
        return {  # the SELL add leg
            "id": "sell-cop",
            "status": "accepted",
            "symbol": "COP",
            "pending_stop_price": 110.0,
        }

    pipeline.broker.submit_order.side_effect = _submit
    # Rearm FAILS — protection could not be placed.
    pipeline.broker.place_entry_protection.return_value = None
    # The add filled 5; the broker now shows the ENLARGED short of 15.
    pipeline.broker.get_order_fill_info.return_value = {"filled_qty": 5.0}
    pipeline.broker.get_positions.return_value = [_short_cop_position(qty=-15.0)]
    pipeline.db.insert_pending_protection_restore.return_value = 7
    pipeline.db.insert_trade.return_value = 1

    ctx = _ctx([_short_cop()], positions=held)
    ExecutionStage(pipeline=pipeline).run(ctx)

    cover_calls = [c for c in pipeline.broker.submit_order.call_args_list if c.kwargs.get("side") == "buy"]
    assert len(cover_calls) == 1, "expected exactly one emergency cover"
    assert cover_calls[0].kwargs["qty"] == 15.0, "emergency cover sized to the add alone leaves the old short leg naked"


# --------------------------------------------------------------------------
# broker: partial fill restore size
# --------------------------------------------------------------------------


@patch("src.execution.broker.TradingClient")
def test_place_entry_protection_scale_in_sizes_stop_to_broker_full_qty(mock_tc_cls):
    from alpaca.trading.requests import StopOrderRequest
    from src.execution.broker import AlpacaBroker

    mock_client = MagicMock()
    mock_client.submit_order.return_value = MagicMock(
        id="s-full",
        status="new",
        symbol="COP",
    )
    mock_tc_cls.return_value = mock_client
    broker = AlpacaBroker(api_key="t", secret_key="t", paper=True)
    broker.wait_for_order_terminal = MagicMock(return_value="filled")
    broker.get_order_fill_info = MagicMock(
        return_value={
            "status": "filled",
            "filled_qty": 3.0,
            "filled_avg_price": 100.0,
        }
    )
    broker.get_positions = MagicMock(return_value=[_cop_position(qty=13.0)])

    out = broker.place_entry_protection(
        symbol="COP",
        order_id="buy-add",
        stop_price=90.0,
        requested_qty=5,
        cover_full_position=True,
        held_qty_before=10.0,
    )
    assert out is not None
    req = mock_client.submit_order.call_args[0][0]
    assert isinstance(req, StopOrderRequest)  # primary protective = stop-MARKET
    assert float(req.qty) == 13.0
    mock_client.submit_order.reset_mock()
    broker.get_order_fill_info = MagicMock(
        return_value={
            "status": "canceled",
            "filled_qty": 0.0,
            "filled_avg_price": None,
        }
    )
    out_zero = broker.place_entry_protection(
        symbol="COP",
        order_id="buy-add-unfilled",
        stop_price=90.0,
        requested_qty=5,
        cover_full_position=True,
        held_qty_before=10.0,
    )
    assert out_zero is None
    mock_client.submit_order.assert_not_called()


# --------------------------------------------------------------------------
# rearm failure alert
# --------------------------------------------------------------------------


def test_rearm_failure_after_scale_in_pages_the_owner():
    pipeline = MagicMock()
    pipeline.broker.get_order_fill_info.return_value = {"filled_qty": 3.0}
    spec = {
        "symbol": "COP",
        "stop_price": 90.0,
        "cover_full_position": True,
        "side": "buy",
    }
    with patch("src.notifier.send_owner_alert") as alert:
        _alert_owner_protection_failed(pipeline, spec, None, "buy-add")
    alert.assert_called_once()
    body = alert.call_args.args[0]
    assert "SCALE-IN" in body
    assert "COP" in body
    assert "NO STOP AT ALL" not in body


def test_ordinary_protection_failure_wording_is_unchanged():
    pipeline = MagicMock()
    pipeline.broker.get_order_fill_info.return_value = {"filled_qty": 7.0}
    spec = {"symbol": "NVDA", "stop_price": 95.0, "side": "buy"}
    with patch("src.notifier.send_owner_alert") as alert:
        _alert_owner_protection_failed(pipeline, spec, None, "e1")
    assert "NO STOP AT ALL" in alert.call_args.args[0]


# --------------------------------------------------------------------------
# drain: full current qty, not cancelled-stop size
# --------------------------------------------------------------------------


def test_drain_scale_in_rearms_broker_full_qty(tmp_path):
    db = _db(tmp_path)
    row_id = db.insert_pending_protection_restore(
        symbol="COP",
        sell_order_id=WAL_SCALE_IN_SENTINEL,
        position_qty_before_sell=10.0,
        specs_json=json.dumps(
            [
                {"id": "stop-1", "qty": 10, "stop_price": 88.0},
                {"id": None, "qty": 0, "stop_price": 90.0, "role": "intended"},
            ]
        ),
        side="sell",
    )
    pipeline = build_pipeline(db=db, broker=MagicMock())
    pipeline.broker.list_open_entry_order_ids.return_value = []
    pipeline.broker.get_positions.return_value = [_cop_position(qty=13.0)]
    pipeline.broker.snapshot_protective_stops.return_value = (True, [])
    pipeline.broker._submit_protective_stop_retrying.return_value = {
        "id": "stop-rearm",
        "uncovered_qty": 0.0,
    }
    pipeline.broker.STOP_LIMIT_BUFFER_PCT = 0.03

    drained = pipeline._drain_pending_protection_restores()
    assert drained == 1
    kwargs = pipeline.broker._submit_protective_stop_retrying.call_args.kwargs
    assert kwargs["qty"] == 13.0
    assert kwargs["stop_price"] == 90.0
    assert kwargs["side"] == "sell"
    assert db.get_pending_protection_restores() == []
    db.close()
    assert row_id  # used; silence unused in some linters


def test_drain_scale_in_row_waits_for_leftover_entry_cancel():
    broker = MagicMock()
    broker.list_open_entry_order_ids.return_value = ["buy-still-working"]
    broker.wait_for_order_terminal.return_value = "new"  # not confirmed
    row = {
        "id": 1,
        "symbol": "COP",
        "specs_json": json.dumps([{"id": "s1", "qty": 10, "stop_price": 90.0}]),
    }
    assert drain_scale_in_row(broker, MagicMock(), row) is False
    broker.cancel_entry_order.assert_called_once_with("buy-still-working")
    broker._submit_protective_stop_retrying.assert_not_called()


# --------------------------------------------------------------------------
# races: trail / watchdog skip in-flight scale-in
# --------------------------------------------------------------------------


def test_deterministic_trail_skips_a_symbol_with_scale_in_wal(tmp_path):
    db = _db(tmp_path)
    db.insert_pending_protection_restore(
        symbol="COP",
        sell_order_id=WAL_SCALE_IN_SENTINEL,
        position_qty_before_sell=10.0,
        specs_json=json.dumps([{"id": "s1", "qty": 10, "stop_price": 90.0}]),
        side="sell",
    )
    pipeline = build_pipeline(db=db, broker=MagicMock(), market=MagicMock())
    orders = pipeline._apply_deterministic_trails(
        [_cop_position()],
        run_id="r1",
    )
    assert orders == []
    pipeline.broker.replace_stop_loss.assert_not_called()
    db.close()


def test_watchdog_skips_scale_in_symbol_while_session_lock_held(
    tmp_path,
    monkeypatch,
):
    from src import coverage_watchdog

    db = _db(tmp_path)
    db.insert_pending_protection_restore(
        symbol="COP",
        sell_order_id=WAL_SCALE_IN_SENTINEL,
        position_qty_before_sell=10.0,
        specs_json=json.dumps([{"id": "s1", "qty": 10, "stop_price": 90.0}]),
        side="sell",
    )
    db.close()
    monkeypatch.setattr(
        "src.execution.scale_in.trading_session_lock_held",
        lambda: True,
    )
    broker = MagicMock()
    broker.get_positions.return_value = [
        SimpleNamespace(symbol="COP", qty=10.0, current_price=100.0),
    ]
    broker.snapshot_protective_stops.return_value = (True, [])
    gaps, err = coverage_watchdog.uncovered_positions(
        broker,
        skip_symbols=coverage_watchdog._scale_in_skip(broker, tmp_path / "scale_in.db"),
    )
    assert err is None
    assert gaps == []
    broker.snapshot_protective_stops.assert_not_called()


def test_pending_protection_symbols_includes_scale_in_wal(tmp_path):
    db = _db(tmp_path)
    db.insert_pending_protection_restore(
        symbol="COP",
        sell_order_id=WAL_SCALE_IN_SENTINEL,
        position_qty_before_sell=10.0,
        specs_json="[]",
        side="sell",
    )
    assert pending_protection_symbols(db) == {"COP"}
    db.close()


# --------------------------------------------------------------------------
# D7 emergency cover: a NON-RAISING broker rejection is still a FAILED cover
# --------------------------------------------------------------------------


def test_emergency_cover_rejected_by_broker_pages_operator_and_writes_no_trade():
    """Board item 183 follow-up. `AlpacaBroker.submit_order` stopped RAISING
    on a terminal broker rejection and now returns
    `{"id": None, "status": "rejected_by_broker"}`. On the D7 path — a SHORT
    filled, its protective stop did NOT place, so the position is a naked
    short with unbounded loss — the old code read only `cover_order["id"]`
    and therefore wrote a `fill_status="submitted"` EMERGENCY_COVER row plus
    a SUCCESS `emergency_cover` event for an order that does not exist, and
    paged nobody. A non-accept must take the SAME path a raised submit took:
    the CRITICAL operator page, the `emergency_cover_failed` event, and NO
    trade row."""
    held = [_short_cop_position(qty=-10.0)]
    pipeline = _shortable(_pipeline(positions=held))
    # The real accept test, not the blanket True the shared fixture installs:
    # the whole point is that an id-less payload must be judged unaccepted.
    pipeline._order_accepted.side_effect = lambda order, symbol, side: TradingPipeline._order_accepted(
        order,
        symbol,
        side,
    )
    stop = {"id": "bstop-cop", "qty": 10, "stop_price": 108.0}
    pipeline.broker.snapshot_protective_stops.return_value = (True, [stop])
    pipeline.broker.cancel_snapshotted_stops.return_value = MagicMock(cleared=True)
    pipeline.broker.wait_for_order_terminal.side_effect = lambda oid, *a, **k: (
        "canceled" if "stop" in str(oid) else "filled"
    )

    def _submit(*args, **kwargs):
        if kwargs.get("side") == "buy":  # the emergency cover leg
            return {
                "id": None,
                "status": "rejected_by_broker",
                "symbol": "COP",
                "detail": "wash trade detected",
            }
        return {  # the SELL entry leg
            "id": "sell-cop",
            "status": "accepted",
            "symbol": "COP",
            "pending_stop_price": 110.0,
        }

    pipeline.broker.submit_order.side_effect = _submit
    pipeline.broker.place_entry_protection.return_value = None  # stop FAILED
    pipeline.broker.get_order_fill_info.return_value = {"filled_qty": 10.0}
    pipeline.broker.get_positions.return_value = [_short_cop_position(qty=-10.0)]
    pipeline.db.insert_pending_protection_restore.return_value = 7
    pipeline.db.insert_trade.return_value = 1

    ctx = _ctx([_short_cop()], positions=held)
    with patch("src.pipeline_stages._record_pipeline_event") as rec:
        ExecutionStage(pipeline=pipeline).run(ctx)

    cover_rows = [c for c in pipeline.db.insert_trade.call_args_list if c.kwargs.get("action") == "EMERGENCY_COVER"]
    assert not cover_rows, (
        "a rejected emergency cover was recorded as a submitted trade — the "
        "desk now believes a naked short is being covered when it is not"
    )
    outcomes = [c.args[4] for c in rec.call_args_list if len(c.args) > 4]
    assert "emergency_cover_failed" in outcomes, (
        "a rejected emergency cover filed no emergency_cover_failed event, "
        "so nobody is paged about an unbounded-loss naked short"
    )
    assert "emergency_cover" not in outcomes, "a rejected emergency cover still reported success"


def test_emergency_cover_source_guards_its_submit_result():
    """MECHANICAL guard, not a behaviour probe. Every other `submit_order`
    caller in this repo tests the RESULT via `_order_accepted`; the D7
    emergency cover was the one that did not, and the cost of that omission
    is a silently-uncovered naked short. This reads the source of the D7
    block and fails if a future edit removes the guard, moves it after the
    `insert_trade`, or drops the `emergency_cover_failed` escalation — none
    of which any single behaviour test would necessarily notice."""
    import ast
    import pathlib

    import src.pipeline_stages as stages_mod
    import src.stage_execution as stage_execution_mod
    import src.stage_execution_parts.protect_entry_stops as protect_mod

    # item 210 step 10 moved ExecutionStage (which owns the D7 block) into
    # src/stage_execution.py, and the 2026-10-09 split moved the D7 block
    # itself into src/stage_execution_parts/protect_entry_stops.py. Scan all
    # three so the guard still has to be found in executable product code.
    blocks = []
    for mod in (stages_mod, stage_execution_mod, protect_mod):
        src = pathlib.Path(mod.__file__).read_text(encoding="utf-8")
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Try):
                continue
            segment = ast.get_source_segment(src, node) or ""
            if '"EMERGENCY_COVER"' in segment and "submit_order" in segment:
                blocks.append((node, segment))
    assert blocks, "the D7 EMERGENCY_COVER submit block has disappeared"
    # Enclosing `try`s match too (the whole protection phase is wrapped);
    # the INNERMOST match is the cover's own guard, so take the shortest.
    node, segment = min(blocks, key=lambda b: len(b[1]))

    submit_lines = [
        n.lineno
        for n in ast.walk(ast.Module(body=node.body, type_ignores=[]))
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "submit_order"
    ]
    guard_lines = [
        n.lineno
        for n in ast.walk(ast.Module(body=node.body, type_ignores=[]))
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "_order_accepted"
    ]
    insert_lines = [
        n.lineno
        for n in ast.walk(ast.Module(body=node.body, type_ignores=[]))
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "insert_trade"
    ]
    assert submit_lines and insert_lines, "D7 block no longer submits/records"
    assert guard_lines, (
        "the emergency cover submits an order and records it without ever "
        "testing the result with `_order_accepted` — a `rejected_by_broker` "
        "return would be written down as a submitted cover"
    )
    assert min(guard_lines) > max(submit_lines), "the `_order_accepted` guard must come AFTER the submit it judges"
    assert max(guard_lines) < min(insert_lines), "the `_order_accepted` guard must come BEFORE the trade row it gates"

    handler_src = "\n".join(ast.get_source_segment(src, h) or "" for h in node.handlers)
    assert "emergency_cover_failed" in handler_src, "the failure branch no longer files emergency_cover_failed"
    assert "logger.critical" in handler_src, "the failure branch no longer pages the operator"


# --- board item 193: the cancel-to-rearm window is measured in-run ---------


def test_unprotected_window_seconds_is_none_without_a_cancel():
    """A naked add cancelled nothing, so there is no window to report."""
    from src.execution.scale_in import unprotected_window_seconds

    assert unprotected_window_seconds(None) is None


def test_unprotected_window_seconds_measures_from_the_cancel_ack():
    import time as _t
    from src.execution.scale_in import unprotected_window_seconds

    seconds = unprotected_window_seconds(_t.monotonic() - 2.0)
    assert seconds is not None and 1.5 <= seconds <= 5.0


def test_prepare_long_add_stamps_the_cancel_acknowledgement():
    """The stamp exists only once the BROKER confirmed the cancel."""
    from src.execution.scale_in import LongAddPrep

    assert LongAddPrep.not_scale_in().cancel_confirmed_at is None


def test_window_event_carries_duration_notional_and_wal_pairing():
    from src.pipeline_stages import _record_scale_in_window_closed
    import time as _t

    recorded = []

    class _DB:
        pass

    class _P:
        db = _DB()

    class _Ctx:
        run_id = "r1"
        decision_id = None

    import src.pipeline_stages as ps

    orig = ps._record_pipeline_event
    ps._record_pipeline_event = lambda pipeline, ctx, symbol, stage, outcome, reason="", **d: recorded.append(
        (symbol, stage, outcome, reason, d)
    )
    try:
        _record_scale_in_window_closed(
            _P(),
            _Ctx(),
            {
                "symbol": "AAPL",
                "cancel_confirmed_at": _t.monotonic() - 1.0,
                "held_qty_before": -12.0,
                "reference_price": 100.0,
                "wal_row_id": 7,
                "stop_price": 90.0,
            },
            covered=True,
        )
        # No cancel happened -> nothing emitted at all.
        _record_scale_in_window_closed(
            _P(),
            _Ctx(),
            {"symbol": "MSFT", "cancel_confirmed_at": None},
            covered=True,
        )
    finally:
        ps._record_pipeline_event = orig

    assert len(recorded) == 1
    symbol, stage, outcome, reason, details = recorded[0]
    assert (symbol, stage) == ("AAPL", "scale_in")
    assert outcome == "unprotected_window_closed"
    assert reason == "rearm_acknowledged"
    assert details["window_seconds"] >= 0.5
    # A short's held qty is negative; the WHOLE position was exposed.
    assert details["held_qty_before"] == 12.0
    assert details["exposed_notional"] == 1200.0
    assert details["wal_row_id"] == 7


def test_window_event_says_so_when_the_rearm_did_not_land():
    from src.pipeline_stages import _record_scale_in_window_closed
    import src.pipeline_stages as ps
    import time as _t

    recorded = []
    orig = ps._record_pipeline_event
    ps._record_pipeline_event = lambda pipeline, ctx, symbol, stage, outcome, reason="", **d: recorded.append(
        (outcome, reason, d)
    )
    try:
        _record_scale_in_window_closed(
            object(),
            object(),
            {
                "symbol": "NOK",
                "cancel_confirmed_at": _t.monotonic(),
                "held_qty_before": 5.0,
                "reference_price": 0.0,
            },
            covered=False,
        )
    finally:
        ps._record_pipeline_event = orig

    outcome, reason, details = recorded[0]
    assert outcome == "unprotected_window_still_open"
    assert reason == "rearm_did_not_land"
    # Price unknowable -> the notional is None, never a fabricated 0.
    assert details["exposed_notional"] is None


# --- The rearm-failure owner alert must stay WIRED --------------------------
# It was defined and never called: a position left naked by a failed rearm
# was logged and nothing else. These fail if the call is removed again.


class _RecordingDB:
    def __init__(self):
        self.evidence = []

    def insert_specialist_evidence(self, **kw):
        self.evidence.append(kw)


class _DrainBroker:
    """Leftover entry already gone; position held; rearm refuses."""

    def __init__(self):
        self.symbol_qty = 5.0

    def get_open_orders(self, *a, **k):
        return []

    def cancel_entry_order(self, order_id):  # pragma: no cover - not reached
        return True

    def get_position(self, symbol):
        return {"qty": self.symbol_qty}

    def snapshot_protective_stops(self, symbol, side="sell"):
        return True, []

    def place_protective_stop(self, *a, **k):
        return None

    def submit_stop_order(self, *a, **k):
        return None


def _patch_alert(monkeypatch):
    from src.execution import scale_in as si

    seen = []
    monkeypatch.setattr(
        si,
        "alert_rearm_failed",
        lambda **kw: seen.append(kw),
        raising=True,
    )
    return seen


def test_drain_rearm_failure_pages_the_owner(monkeypatch, tmp_path):
    from src.execution import scale_in as si

    seen = _patch_alert(monkeypatch)
    monkeypatch.setattr(si, "list_open_entry_ids", lambda broker, symbol: [])
    monkeypatch.setattr(si, "broker_position_qty", lambda broker, symbol: 5.0)
    monkeypatch.setattr(
        si,
        "rearm_full_position_stop",
        lambda *a, **k: None,
        raising=True,
    )
    db = _RecordingDB()
    ok = si.drain_scale_in_row(
        _DrainBroker(),
        db,
        {
            "id": 1,
            "symbol": "ABC",
            "position_qty_before_sell": 5.0,
            "specs_json": '[{"stop_price": 10.0, "qty": 5}]',
        },
    )
    assert ok is False
    assert seen and seen[0]["symbol"] == "ABC", "a failed crash-recovery rearm must page the owner"


def test_failed_add_unrestored_stop_pages_the_owner(monkeypatch):
    from src.execution import scale_in as si

    seen = _patch_alert(monkeypatch)
    monkeypatch.setattr(si, "restore_cancelled_stops", lambda *a, **k: False)
    monkeypatch.setattr(si, "discharge_scale_in_wal", lambda *a, **k: None)
    prep = si.LongAddPrep(
        is_scale_in=True,
        cancelled=True,
        wal_row_id=7,
        specs=[{"stop_price": 10.0, "qty": 3}],
        held_qty_before=3.0,
    )
    si.restore_after_failed_add(object(), _RecordingDB(), prep, "ABC")
    assert seen and seen[0]["symbol"] == "ABC", "an unrestorable protective sell must page the owner"


def test_rearm_alert_records_durably_and_pages_once_per_day(monkeypatch, tmp_path):
    """Muted Telegram must not lose the fault, and one fault must not
    become 44 pages."""
    from src.execution import scale_in as si
    from src import coverage_watchdog as cw

    state = tmp_path / "watchdog.json"
    monkeypatch.setattr(
        si,
        "record_rearm_failure",
        si.record_rearm_failure,
        raising=True,
    )
    real_claim = cw.claim_typed_alert
    monkeypatch.setattr(
        si,
        "claim_typed_alert",
        lambda kind, syms, **k: real_claim(kind, syms, path=state),
    )
    sent = []
    import src.notifier as notifier

    monkeypatch.setattr(
        notifier,
        "send_owner_alert",
        lambda body, **k: sent.append(body),
        raising=False,
    )

    db = _RecordingDB()
    for _ in range(3):
        si.alert_rearm_failed(
            symbol="ABC",
            qty=5.0,
            stop_price=10.0,
            order_id="o1",
            db=db,
        )

    assert len(db.evidence) == 3, "every occurrence is recorded durably"
    assert db.evidence[0]["symbol"] == "ABC"
    assert db.evidence[0]["agent_name"] == si.REARM_FAILURE_AGENT_NAME
    assert len(sent) == 1, "at most one page per symbol per trading day"


# ---------------------------------------------------------------------------
# board item 193 — an in-place QUANTITY amend cannot replace the cancel
# ---------------------------------------------------------------------------


def test_scale_in_never_amends_a_resting_stop_and_survives_42210000(tmp_path):
    """The cancel is forced by the broker's order model, not by our sequencing.

    A resting protective SELL and a working BUY collide on the same symbol, so
    the stop must come off. The obvious alternative — leave it resting and
    amend its QUANTITY in place after the add — is refused by this broker on a
    FRACTIONAL order (code 42210000), and scale-in adds are routinely
    fractional. This pins both halves: the path never reaches for
    `replace_order_by_id`, and a broker that refuses every quantity amend
    changes nothing about the cancel-confirm sequence.
    """
    db = _db(tmp_path)
    broker = MagicMock()
    broker.snapshot_protective_stops.return_value = (
        True,
        [{"id": "stop-1", "qty": 10.5, "stop_price": 88.0}],
    )
    broker.cancel_snapshotted_stops.return_value = MagicMock(cleared=True)
    broker.wait_for_order_terminal.return_value = "canceled"

    def _refuse_quantity_amend(*a, **k):
        raise RuntimeError(
            "{'code':42210000,'message':'qty is not modifiable on a fractional order'}",
        )

    broker.replace_order_by_id.side_effect = _refuse_quantity_amend

    prep = prepare_long_add(
        broker=broker,
        db=db,
        symbol="COP",
        positions=[_cop_position()],
        intended_stop=90.0,
    )
    # The amend was never attempted — so the 42210000 refusal above can never
    # be reached, and the original stop is never left in an unknown state.
    broker.replace_order_by_id.assert_not_called()
    # The cancel-confirm sequence is unchanged and the window is opened on
    # purpose, with the write-ahead row standing behind a crash.
    assert prep.cancelled is True
    assert prep.skip_reason is None
    assert prep.cancel_confirmed_at is not None
    broker.cancel_snapshotted_stops.assert_called_once()
    db.close()
