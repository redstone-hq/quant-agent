"""The trades.action vocabulary and TRAIL_STOP rules are defined once, in src/trade_actions.py."""

from src import quantities, trade_actions
from src.api import db_reads
from src.storage.analytics import calibration
from src.storage.trades import ledger, position_chain


def test_storage_and_quantities_use_the_one_definition():
    assert position_chain._POSITION_EXIT_ACTIONS is trade_actions.POSITION_EXIT_ACTIONS
    assert position_chain._POSITION_EXIT_PREFIXES is trade_actions.POSITION_EXIT_PREFIXES
    assert ledger._trail_stop_reduced_position is trade_actions.trail_stop_reduced_position
    assert position_chain._POSITION_OPEN_ACTIONS is trade_actions.POSITION_OPEN_ACTIONS
    assert calibration._POSITION_OPEN_ACTIONS is trade_actions.POSITION_OPEN_ACTIONS
    assert calibration._is_filled_trail_stop is trade_actions.is_filled_trail_stop
    assert quantities.POSITION_OPEN_ACTIONS is trade_actions.POSITION_OPEN_ACTIONS
    assert db_reads.POSITION_OPEN_ACTIONS is trade_actions.POSITION_OPEN_ACTIONS
    assert db_reads.share_change is trade_actions.share_change
    for alias in ("_OPEN_ACTIONS", "_share_change", "_executed_qty"):
        assert not hasattr(quantities, alias)
    for alias in ("_POSITION_OPEN_ACTIONS", "_POSITION_EXIT_ACTIONS", "_POSITION_EXIT_PREFIXES"):
        assert not hasattr(db_reads, alias)
    assert not hasattr(quantities, "_SHARE_EXIT_ACTIONS") and not hasattr(quantities, "_SHARE_EXIT_PREFIXES")


def test_partial_fill_then_cancel_reduces_shares_but_is_not_a_realized_exit():
    row = {"action": "TRAIL_STOP", "fill_status": "canceled", "fill_qty": 50, "qty": 200}
    assert trade_actions.trail_stop_reduced_position(row, "TRAIL_STOP") is True
    assert trade_actions.is_filled_trail_stop(row, "TRAIL_STOP") is False
    assert trade_actions.share_change(row) == "out"
    assert trade_actions.executed_qty(row) == 50


def test_unfilled_stop_move_moves_no_shares():
    for status in (None, "submitted", "canceled", "expired"):
        row = {"action": "TRAIL_STOP", "fill_status": status, "fill_qty": None, "qty": 200}
        assert trade_actions.trail_stop_reduced_position(row, "TRAIL_STOP") is False
        assert trade_actions.share_change(row) is None


def test_reconciled_exit_retires_shares_on_either_side():
    """Item 173(a) pin: a broker exit of unproven cause still closes the lot."""
    row = {"action": "RECONCILED_EXIT", "fill_status": "filled", "fill_qty": 10, "qty": 10}
    assert trade_actions.is_position_exit_action("RECONCILED_EXIT")
    assert trade_actions.exit_action_side("RECONCILED_EXIT") is None
    assert trade_actions.share_change(row) == "out"


def _r(i, action, qty, ts=None, fill_qty=None, status="filled"):
    row = {"id": i, "action": action, "qty": qty, "fill_qty": qty if fill_qty is None else fill_qty}
    row["fill_status"] = status
    if ts is not None:
        row["timestamp"] = ts
    return row


def test_live_lots_start_after_the_last_flat_point_in_fill_order():
    rows = [
        _r(4, "BUY", 50, "2026-10-01 10:03:00"),
        _r(2, "TRAIL_STOP", 100, "2026-10-01 10:01:00", fill_qty=40, status="canceled"),
        _r(1, "BUY", 100, "2026-10-01 10:00:00"),
        _r(3, "SELL", 60, "2026-10-01 10:02:00"),
        _r(5, "TRAIL_STOP", 50, "2026-10-01 10:04:00", fill_qty=0, status="submitted"),
    ]
    assert [r["id"] for r in trade_actions.live_lot_rows(rows, decimals=9)] == [4]


def test_live_lots_fail_closed_when_only_some_fills_carry_a_time():
    rows = [_r(1, "BUY", 100, "2026-10-01 10:00:00"), _r(2, "SELL", 60)]
    assert trade_actions.live_lot_rows(rows, decimals=9) is None
    assert quantities.held_position_risk(rows, 40) == (None, trade_actions.FILL_ORDER_UNKNOWN)


def test_live_lots_ignore_an_exit_with_nothing_open_and_a_wrong_side_exit():
    rows = [_r(1, "SELL", 10), _r(2, "SHORT", 100), _r(3, "SELL", 30), _r(4, "COVER", 40)]
    assert [r["id"] for r in trade_actions.live_lot_rows(rows, decimals=9)] == [2, 4]
