"""The trades.action vocabulary and TRAIL_STOP rules are defined once, in src/trade_actions.py."""

from src import quantities, trade_actions
from src.storage.analytics import calibration
from src.storage.trades import position_chain


def test_storage_and_quantities_use_the_one_definition():
    assert position_chain._POSITION_EXIT_ACTIONS is trade_actions.POSITION_EXIT_ACTIONS
    assert position_chain._POSITION_EXIT_PREFIXES is trade_actions.POSITION_EXIT_PREFIXES
    assert position_chain._trail_stop_reduced_position is trade_actions.trail_stop_reduced_position
    assert position_chain._POSITION_OPEN_ACTIONS is trade_actions.POSITION_OPEN_ACTIONS
    assert calibration._POSITION_OPEN_ACTIONS is trade_actions.POSITION_OPEN_ACTIONS
    assert calibration._is_filled_trail_stop is trade_actions.is_filled_trail_stop
    assert quantities._OPEN_ACTIONS is trade_actions.POSITION_OPEN_ACTIONS
    assert not hasattr(quantities, "_SHARE_EXIT_ACTIONS") and not hasattr(quantities, "_SHARE_EXIT_PREFIXES")


def test_partial_fill_then_cancel_reduces_shares_but_is_not_a_realized_exit():
    row = {"action": "TRAIL_STOP", "fill_status": "canceled", "fill_qty": 50, "qty": 200}
    assert trade_actions.trail_stop_reduced_position(row, "TRAIL_STOP") is True
    assert trade_actions.is_filled_trail_stop(row, "TRAIL_STOP") is False
    assert quantities._share_change(row) == "out"
    assert quantities._executed_qty(row) == 50


def test_unfilled_stop_move_moves_no_shares():
    for status in (None, "submitted", "canceled", "expired"):
        row = {"action": "TRAIL_STOP", "fill_status": status, "fill_qty": None, "qty": 200}
        assert trade_actions.trail_stop_reduced_position(row, "TRAIL_STOP") is False
        assert quantities._share_change(row) is None
