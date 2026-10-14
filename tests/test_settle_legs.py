"""Refused sales (ETN, NET 13 Oct): wait for the broker to confirm the cancel, then sell.

The desk sent the SELL 51-100 ms after asking the broker to cancel the
protecting stop; the shares were still held and the broker refused it. Every
test here fails on the trunk this branched from: there the sale went at once,
sized down to whatever was held, and nothing was owed.
"""

from __future__ import annotations

import inspect
import json
from types import SimpleNamespace
from unittest.mock import MagicMock

from src.models import Position
from tests.pipeline_factory import build_pipeline

STOP = {"id": "stop-1", "qty": 100.0, "stop_price": 90.0}


def _pos(qty, free=None):
    return Position(
        symbol="NVDA",
        qty=qty,
        avg_entry=100.0,
        current_price=100.0,
        market_value=100.0 * qty,
        unrealized_pnl=0.0,
        sector="Tech",
        qty_available=qty if free is None else free,
    )


def _pipe(*, leg_status="canceled", leg_filled=0.0, held=100.0, free=None, spec=STOP):
    pipe = build_pipeline(db=MagicMock(), broker=MagicMock())
    calls: list[tuple] = []
    pipe.db.insert_pending_protection_restore.return_value = 7
    pipe.broker.cancel_open_entry_orders.return_value = 0
    pipe.broker.snapshot_protective_stops.return_value = (True, [dict(spec)])
    pipe.broker.cancel_snapshotted_stops.return_value = MagicMock(cleared=True)

    def _wait(order_id, **_kw):
        calls.append(("wait", order_id))
        return leg_status

    def _submit(**kw):
        calls.append(("submit", kw["qty"]))
        return {"id": "ord-1", "status": "accepted"}

    pipe.broker.wait_for_order_terminal.side_effect = _wait
    pipe.broker.client.get_order_by_id.return_value = SimpleNamespace(filled_qty=str(leg_filled))
    pipe.broker.get_positions.return_value = [_pos(held, free)]
    pipe.broker.submit_order.side_effect = _submit
    pipe._order_accepted = MagicMock(return_value=True)
    return pipe, calls


def _sell(pipe, qty, held=100.0, label="SELL"):
    return pipe._submit_protected_sell(
        symbol="NVDA",
        qty=qty,
        limit_price=99.0,
        reference_price=100.0,
        position_qty_before_sell=held,
        label=label,
    )


def _owed_writes(pipe):
    return [
        c.kwargs["sell_order_id"]
        for c in pipe.db.update_pending_protection_restore.call_args_list
        if str(c.kwargs.get("sell_order_id", "")).startswith("__OWED_SALE__")
    ]


# --- the step: one deadline shared by every leg ---------------------------


def test_two_legs_share_one_deadline(monkeypatch):
    """First leg terminal at 14 s: the second gets at most the 1 s left, not 15 s more."""
    from src.execution.broker_parts import cancel_confirm
    from src.execution.broker_parts.order_desk import ORDER_TERMINAL_CEILING_S

    clock = {"t": 1000.0}
    monkeypatch.setattr(cancel_confirm.time, "monotonic", lambda: clock["t"])
    given: list[float] = []

    def _wait(order_id, timeout_seconds):
        given.append(timeout_seconds)
        clock["t"] += ORDER_TERMINAL_CEILING_S - 1 if order_id == "a" else 0
        return "canceled"

    broker = SimpleNamespace(
        wait_for_order_terminal=_wait,
        client=SimpleNamespace(get_order_by_id=lambda _id: SimpleNamespace(filled_qty="0")),
    )
    out = cancel_confirm.settle_legs(broker, [{"id": "a"}, {"id": "b"}])
    assert out.settled
    assert given[0] == ORDER_TERMINAL_CEILING_S
    assert given[1] <= 1


# --- the sale waits, then sells; or sends nothing and owes it ---------------


def test_sale_goes_only_after_the_cancel_is_confirmed():
    pipe, calls = _pipe(leg_status="canceled")
    assert _sell(pipe, 100.0) is not None
    assert calls.index(("wait", "stop-1")) < calls.index(("submit", 100.0))


def test_unconfirmed_cancel_sends_nothing_and_owes_the_sale():
    pipe, calls = _pipe(leg_status="pending_cancel")
    assert _sell(pipe, 100.0) is None
    assert not [c for c in calls if c[0] == "submit"]
    assert pipe._last_stop_clear_refusal == "cancel_unconfirmed"
    (owed,) = _owed_writes(pipe)
    sale = json.loads(owed[len("__OWED_SALE__") :])
    assert (sale["label"], sale["qty"], sale["full_exit"]) == ("SELL", 100.0, True)
    # A pending cancel refuses a new stop too: nothing is put back over it.
    pipe.broker._restore_stop_orders.assert_not_called()


def test_a_failed_status_read_is_its_own_reason_and_owes_the_sale():
    pipe, calls = _pipe(leg_status=None)
    assert _sell(pipe, 100.0) is None
    assert pipe._last_stop_clear_refusal == "cancel_read_failed"
    assert _owed_writes(pipe)


# --- exactly the intended size, or nothing ---------------------------------


def test_trim_with_shares_reserved_sends_nothing():
    pipe, calls = _pipe(free=60.0)
    assert _sell(pipe, 80.0) is None
    assert pipe._last_stop_clear_refusal == "shares_reserved"
    assert not [c for c in calls if c[0] == "submit"]


def test_full_exit_with_shares_reserved_sends_nothing():
    pipe, calls = _pipe(free=60.0)
    assert _sell(pipe, 100.0) is None
    assert pipe._last_stop_clear_refusal == "shares_reserved"


def test_trim_after_a_stop_filled_in_part_has_its_own_reason():
    pipe, calls = _pipe(leg_status="canceled", leg_filled=10.0, held=90.0)
    assert _sell(pipe, 50.0) is None
    assert pipe._last_stop_clear_refusal == "stop_filled_in_part"


def test_fractional_cross_check_is_exact_on_the_broker_grid():
    spec = {"id": "stop-1", "qty": 10.123456789, "stop_price": 90.0}
    pipe, calls = _pipe(leg_filled=0.000000001, held=10.123456788, spec=spec)
    assert _sell(pipe, 10.123456789, held=10.123456789) is not None
    assert ("submit", 10.123456788) in calls
    pipe, calls = _pipe(leg_filled=0.000000001, held=10.12345679, spec=spec)
    assert _sell(pipe, 10.123456789, held=10.123456789) is None
    assert pipe._last_stop_clear_refusal == "position_disagrees"


# --- the drain sends what is owed, after re-checking the position ----------


def _owed_row(qty=100.0, full_exit=True):
    sale = {"label": "SELL", "qty": qty, "full_exit": full_exit, "limit_price": 99.0, "reference_price": 100.0}
    return {
        "id": 7,
        "symbol": "NVDA",
        "side": "sell",
        "run_id": "r1",
        "position_qty_before_sell": 100.0,
        "specs_json": json.dumps([STOP]),
        "sell_order_id": "__OWED_SALE__" + json.dumps(sale | {"reason": "cancel_unconfirmed"}),
    }


def test_drain_sends_the_owed_sale_once_the_leg_is_dead():
    from src.protection.protected_sell import drain_owed_sales

    pipe, calls = _pipe(leg_status="canceled")
    pipe.broker.snapshot_protective_stops.return_value = (True, [])
    pipe.db.get_pending_protection_restores.return_value = [_owed_row()]
    assert drain_owed_sales(pipe) == 1
    assert ("submit", 100.0) in calls
    pipe.db.update_pending_protection_restore.assert_called_with(7, sell_order_id="ord-1")


def test_drain_keeps_the_sale_owed_while_the_leg_is_live():
    from src.protection.protected_sell import drain_owed_sales

    pipe, calls = _pipe(leg_status="pending_cancel")
    pipe.db.get_pending_protection_restores.return_value = [_owed_row()]
    assert drain_owed_sales(pipe) == 0
    assert not [c for c in calls if c[0] == "submit"]
    pipe.db.update_pending_protection_restore.assert_not_called()


def test_drain_refuses_a_trim_the_position_no_longer_holds():
    from src.protection.protected_sell import drain_owed_sales
    from src.protection.sell_finalization import _WAL_SELL_SENTINEL

    pipe, calls = _pipe(leg_status="canceled", held=50.0)
    pipe.db.get_pending_protection_restores.return_value = [_owed_row(qty=80.0, full_exit=False)]
    assert drain_owed_sales(pipe) == 0
    assert not [c for c in calls if c[0] == "submit"]
    # Handed back to the restore drain, which puts the stops back at held.
    pipe.db.update_pending_protection_restore.assert_called_with(7, sell_order_id=_WAL_SELL_SENTINEL)


def test_intra_safety_drains_owed_sales_before_restores():
    from src.intraday import safety

    src = inspect.getsource(safety)
    assert src.index("drain_owed_sales(self)") < src.index("self._drain_pending_protection_restores()")


# --- one wait ceiling, no site literals ------------------------------------


def test_every_wait_site_uses_the_one_ceiling():
    from src.execution.broker_parts import entry_protection, order_desk
    from src.protection import sell_finalization

    for mod in (sell_finalization, entry_protection):
        assert "timeout_seconds=5.0" not in inspect.getsource(mod)
        assert "timeout_seconds=10.0" not in inspect.getsource(mod)
    default = inspect.signature(order_desk.OrderDesk.wait_for_order_terminal).parameters["timeout_seconds"].default
    assert default == order_desk.ORDER_TERMINAL_CEILING_S
