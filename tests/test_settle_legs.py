"""Refused sales (ETN, NET 13 Oct): wait for the broker to confirm the cancel, then sell.

The desk sent the SELL 51-100 ms after asking the broker to cancel the
protecting stop; the shares were still held and the broker refused it. Every
test here fails on the trunk this branched from: there the sale went at once,
sized down to whatever was held, and nothing was owed.
"""

from __future__ import annotations

import inspect
from datetime import UTC
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


def test_unconfirmed_cancel_sends_nothing_and_puts_the_sale_off():
    from src.protection import put_off_sales

    pipe, calls = _pipe(leg_status="pending_cancel")
    pipe._alert_owner_exit_declined = MagicMock()
    sales = []
    put_off_sales.run_owning_put_offs(pipe, lambda: sales.append(_sell(pipe, 100.0)), "no_window_session")
    assert sales == [None]
    assert not [c for c in calls if c[0] == "submit"]
    assert pipe._last_stop_clear_refusal == "cancel_unconfirmed"
    # The write-ahead row is left as written: the restore drain owns the put-back.
    pipe.db.update_pending_protection_restore.assert_not_called()
    # A pending cancel refuses a new stop too: nothing is put back over it.
    pipe.broker._restore_stop_orders.assert_not_called()


def test_a_failed_status_read_is_its_own_reason_and_puts_the_sale_off():
    pipe, calls = _pipe(leg_status=None)
    pipe._alert_owner_exit_declined = MagicMock()
    assert _sell(pipe, 100.0) is None
    assert pipe._last_stop_clear_refusal == "cancel_read_failed"
    assert not [c for c in calls if c[0] == "submit"]


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


# --- one wait ceiling, no site literals ------------------------------------


def test_every_wait_site_uses_the_one_ceiling():
    from src.execution.broker_parts import entry_protection, order_desk
    from src.protection import sell_finalization

    for mod in (sell_finalization, entry_protection):
        assert "timeout_seconds=5.0" not in inspect.getsource(mod)
        assert "timeout_seconds=10.0" not in inspect.getsource(mod)
    default = inspect.signature(order_desk.OrderDesk.wait_for_order_terminal).parameters["timeout_seconds"].default
    assert default == order_desk.ORDER_TERMINAL_CEILING_S


# --- put-back: up to held, never over a dying stop --------------------------


def test_put_back_sizes_cover_up_to_held():
    from src.execution.broker_parts.stop_invariant import cover_to_held

    placer = SimpleNamespace(
        get_positions=lambda: [SimpleNamespace(symbol="NVDA", qty=103.0)],
        _restore_stop_orders=MagicMock(return_value=(1, [])),
    )
    assert cover_to_held(placer, "NVDA", [dict(STOP)])
    (args, kw) = placer._restore_stop_orders.call_args
    assert [s["qty"] for s in args[1]] == [103.0] and kw["check_idempotency"] is True


def test_put_back_failure_line_is_read_by_the_health_report(caplog):
    import re

    import src.log_health as lh
    from src.execution.broker_parts.stop_invariant import cover_to_held

    placer = SimpleNamespace(
        get_positions=lambda: [SimpleNamespace(symbol="NVDA", qty=100.0)],
        _restore_stop_orders=MagicMock(return_value=(0, [dict(STOP)])),
    )
    assert not cover_to_held(placer, "NVDA", [dict(STOP)])
    (line,) = [r.getMessage() for r in caplog.records if "cover_to_held" in r.getMessage()]
    fams = [f for v in vars(lh).values() if isinstance(v, (list, tuple)) for f in v]
    (fam,) = [f for f in fams if getattr(f, "key", None) == "stop_missing_or_failed"]
    assert any(re.search(getattr(p, "pattern", p), line) for p in fam.patterns)
    assert line.split("FAILED for ")[1].startswith("NVDA ")


def test_a_dying_stop_is_never_counted_as_already_alive():
    from src.execution.broker_parts.stop_place import StopPlacer

    placer = SimpleNamespace(
        _list_open_protective_stop_orders=lambda symbol, side="sell": [
            SimpleNamespace(id="old-1", status="pending_cancel", qty=100.0, stop_price=90.0)
        ],
        _snapshot_stop_order=lambda o: {"id": o.id, "qty": o.qty, "stop_price": o.stop_price},
        _submit_stop_limit_order=MagicMock(return_value={"id": "new-1", "status": "accepted"}),
        wait_for_order_terminal=lambda oid, **_kw: "canceled",
        client=SimpleNamespace(get_order_by_id=lambda oid: SimpleNamespace(filled_qty="0")),
    )
    spec = {"id": "old-1", "qty": 100.0, "stop_price": 90.0}
    StopPlacer._restore_stop_orders(placer, "NVDA", [spec], check_idempotency=True)
    placer._submit_stop_limit_order.assert_called_once()


# --- entry / leftover orders are legs too ----------------------------------


def test_an_entry_that_fills_during_its_cancel_is_sold_too():
    from src.execution.broker_parts.cancel_confirm import CancelledEntries

    pipe, calls = _pipe(held=103.0)
    entry = CancelledEntries([{"id": "ent-1", "side": "buy", "kind": "order"}])
    pipe.broker.cancel_open_entry_orders.return_value = entry
    pipe.broker.client.get_order_by_id.side_effect = lambda oid: SimpleNamespace(
        filled_qty="3" if oid == "ent-1" else "0"
    )
    assert _sell(pipe, 100.0) is not None
    assert ("wait", "ent-1") in calls and ("submit", 103.0) in calls


def test_each_leg_writes_its_broker_clock_cancel_latency():
    from datetime import datetime, timedelta

    from src.execution.broker_parts.cancel_confirm import settle_legs

    sent = datetime(2026, 10, 14, 14, 0, 0, tzinfo=UTC)
    db = MagicMock()
    order = SimpleNamespace(filled_qty="0", canceled_at=sent + timedelta(seconds=2.5))
    broker = SimpleNamespace(
        _recon_db=db,
        wait_for_order_terminal=lambda oid, **_kw: "canceled",
        client=SimpleNamespace(get_order_by_id=lambda oid: order),
    )
    settle_legs(broker, [{"id": "s1", "cancel_sent_at": sent.isoformat()}])
    (call,) = [c for c in db.insert_specialist_evidence.call_args_list if c.kwargs["kind"] == "cancel_latency"]
    row = json.loads(call.kwargs["evidence_json"])
    assert row["latency_ms"] == 2500 and row["within_bound"] is True


# --- the drain re-prices, and an unknown free count keeps the sale owed ------


def test_a_broker_that_never_confirms_puts_the_sale_off():
    from tests.fakes.confirmed_cancel import confirm_cancels

    pipe, calls = _pipe()
    pipe.broker.wait_for_order_terminal.side_effect = None
    confirm_cancels(pipe.broker, confirms=False)
    assert _sell(pipe, 100.0) is None
    assert pipe._last_stop_clear_refusal == "cancel_unconfirmed"
    assert not [c for c in calls if c[0] == "submit"]
