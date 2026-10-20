"""A lost fill reply proves nothing (docs/OUTCOME.md): the desk asks the broker.

Entry protection: when the fill read raises, the broker POSITION decides the
stop; when that read also fails the symbol goes to the write-ahead restore.
Sell finalization: when the restore AND the write-ahead row both fail, the
desk re-protects immediately, or says `naked` so coverage is unconfirmed.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from src.execution.broker_parts import entry_protection
from src.protection.sell_finalization import SellFinalization

SYM = "COP"


def _broker(*, fill_info, positions):
    broker = MagicMock(name="broker")
    broker._TERMINAL_ORDER_STATES = {"filled", "canceled", "cancelled", "expired", "rejected"}
    broker.wait_for_order_terminal.return_value = "filled"
    broker.get_order_fill_info.side_effect = fill_info
    broker.get_positions.side_effect = positions
    broker._submit_protective_stop_retrying.return_value = {"id": "stop-1"}
    return broker


def _protect(broker, **kw):
    return entry_protection.place_entry_protection(
        broker,
        SYM,
        "buy-1",
        90.0,
        requested_qty=5,
        held_qty_before=10.0,
        _ENTRY_FILL_TIMEOUT_S=0.0,
        **kw,
    )


def test_fill_read_raises_and_position_shows_new_shares_so_a_stop_covers_them():
    broker = _broker(
        fill_info=RuntimeError("fill reply lost"),
        positions=lambda: [SimpleNamespace(symbol=SYM, qty="13")],
    )
    out = _protect(broker)
    assert out == {"id": "stop-1"}
    kwargs = broker._submit_protective_stop_retrying.call_args.kwargs
    assert kwargs["symbol"] == SYM and kwargs["side"] == "sell"
    assert float(kwargs["qty"]) == 3.0  # 13 held now - 10 held before = the entry's fill
    assert float(kwargs["stop_price"]) == 90.0


def test_fill_and_position_both_unreadable_hand_the_symbol_to_the_wal_restore():
    broker = _broker(fill_info=RuntimeError("fill reply lost"), positions=RuntimeError("positions down"))
    persist = MagicMock(name="persist_restore")
    out = _protect(broker, persist_restore=persist)
    assert out is None
    broker._submit_protective_stop_retrying.assert_not_called()
    args, kwargs = persist.call_args
    assert args[0] == entry_protection._WAL_SELL_SENTINEL and args[1] == SYM
    assert args[2] == 15.0  # held before + requested: the most the broker can hold
    assert args[3] == [{"qty": 5.0, "stop_price": 90.0, "side": "sell"}]
    assert kwargs == {"side": "sell"}


def test_fill_read_ok_with_zero_filled_places_no_stop_and_asks_no_wal():
    broker = _broker(
        fill_info=lambda _order_id: {"status": "canceled", "filled_qty": 0.0},
        positions=lambda: [SimpleNamespace(symbol=SYM, qty="13")],
    )
    persist = MagicMock(name="persist_restore")
    assert _protect(broker, persist_restore=persist) is None
    broker._submit_protective_stop_retrying.assert_not_called()
    persist.assert_not_called()


def _finalizer(broker, db):
    fin = SellFinalization(
        broker=broker,
        db=db,
        terminal_order_statuses=frozenset({"filled", "canceled"}),
        finalize_protection_after_sell=MagicMock(),
        register_exit_settlement=MagicMock(),
        finalize_protection_after_sell_core=MagicMock(),
        cancel_stray_stops_on_flat=MagicMock(),
        current_position_qty_for_finalize=MagicMock(return_value=10.0),
        persist_orphaned_protection_restore=MagicMock(),
        reprotect_residual_after_partial_sell=MagicMock(),
        derive_close_side_for_drain=MagicMock(),
    )
    # The two bodies under test are injected collaborators on the lifted
    # class; bind the real ones so the core reaches the real persist.
    for name in ("_persist_orphaned_protection_restore", "_finalize_protection_after_sell_core"):
        setattr(fin, name, getattr(SellFinalization, name).__get__(fin))
    return fin


SPECS = [{"id": "s1", "qty": 6.0, "stop_price": 88.0}, {"id": "s2", "qty": 4.0, "stop_price": 91.5}]


def test_restore_and_wal_failure_reprotects_at_the_most_protective_cancelled_stop():
    broker = MagicMock(name="broker")
    broker.get_order_fill_info.return_value = {"status": "canceled", "filled_qty": 0.0}
    broker._restore_stop_orders.side_effect = RuntimeError("restore down")
    broker._reprotect_or_naked.return_value = {"id": "stop-2", "status": "accepted"}
    db = MagicMock(name="db")
    db.insert_pending_protection_restore.side_effect = RuntimeError("db locked")
    fin = _finalizer(broker, db)

    ok, retry = fin._finalize_protection_after_sell_core("sell-1", SYM, 10.0, SPECS)

    assert ok is False and retry == SPECS
    broker._reprotect_or_naked.assert_called_once_with(SYM, 10.0, "sell", 91.5, SPECS)


def test_restore_and_wal_failure_with_failed_reprotect_reports_coverage_unconfirmed():
    broker = MagicMock(name="broker")
    broker.get_order_fill_info.return_value = {"status": "canceled", "filled_qty": 0.0}
    broker._restore_stop_orders.side_effect = RuntimeError("restore down")
    broker._reprotect_or_naked.return_value = {"symbol": SYM, "status": "naked", "amend_status": "naked", "legs": []}
    db = MagicMock(name="db")
    db.insert_pending_protection_restore.side_effect = RuntimeError("db locked")
    fin = _finalizer(broker, db)

    result = fin._persist_orphaned_protection_restore("sell-1", SYM, 10.0, SPECS)
    assert result["status"] == "naked"

    ok, _retry = fin._finalize_protection_after_sell_core("sell-1", SYM, 10.0, SPECS)
    assert ok is False
    assert broker._reprotect_or_naked.call_count == 2
