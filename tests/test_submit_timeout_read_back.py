"""A submit that raises without a broker refusal is read back by its own key.

(a) found by client_order_id -> returned as placed; an exit then takes the
    accepted path and its stops are NOT put back over shares already sold;
(b) not found -> the submit's own failure propagates (UNKNOWN), never a
    "not placed" result; an exit puts back only what is held and alerts;
(c) a timed-out SHORT entry (a sell order) is matched by the orphan sweep by
    the key stored on its pending row, not missed by a buy-side search;
(d) a reply the broker DID return (4xx/5xx) is never read back: it propagates.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from src.execution.broker_parts.order_desk import OrderDesk, entry_client_order_id
from src.protection.fill_reconciler import FillReconciler
from src.protection.protected_sell import ProtectedSell
from src.storage.db import Database


class _NotFound(Exception):
    status_code = 404


def _client(*, read_back):
    client = MagicMock(name="client")
    client.submit_order.side_effect = TimeoutError("read timed out")
    client.get_order_by_client_id.side_effect = read_back
    return client


def _desk(client) -> OrderDesk:
    return OrderDesk(
        client=client,
        kill_switch_active=lambda: False,
        kill_switch_path="/nonexistent",
        wait_for_order_status=lambda *a, **k: None,
        wait_for_order_status_via_stream=lambda *a, **k: None,
        get_latest_price=lambda symbol: None,
        order_terminal_states={"filled", "canceled"},
        order_replaceable_states={"new"},
        max_replacement_hops=3,
        get_fractionability=lambda symbol: (True, True),
        get_account=MagicMock(),
        max_position_pct=1.0,
    )


def _placed(client_order_id):
    return SimpleNamespace(id="ord-zzz-1", status="accepted", symbol="ZZZ", qty="10", client_order_id=client_order_id)


@pytest.fixture(autouse=True)
def _no_live_quantity_gate():
    with patch("src.execution.broker_parts.order_desk.quantity_refusal_live", return_value=None):
        yield


def test_a_timed_out_submit_found_by_its_key_is_returned_as_placed():
    client = _client(read_back=_placed)
    order = _desk(client).submit_order(symbol="ZZZ", qty=10, side="sell")
    assert order["id"] == "ord-zzz-1"
    assert order["status"] == "accepted"
    client.get_order_by_client_id.assert_called_once_with(entry_client_order_id("ZZZ", "sell", 10, None))


def _seller(desk):
    alert = MagicMock(name="alert_owner_exit_declined")
    seller = ProtectedSell(
        broker=SimpleNamespace(submit_order=desk.submit_order),
        db=None,
        alert_owner_exit_declined=alert,
        order_accepted=lambda order, symbol, side: bool(order and order.get("id")),
        state=MagicMock(name="state"),
    )
    return seller, alert


def _send(seller):
    with (
        patch.object(ProtectedSell, "_size_to_held", lambda self, *a, **k: (10.0, 10.0)),
        patch.object(ProtectedSell, "_put_back") as put_back,
    ):
        result = seller._send_cleared(
            symbol="ZZZ",
            qty=10,
            limit_price=None,
            reference_price=None,
            position_qty_before_sell=10,
            label="test-exit",
            side="sell",
            escalate_to_market_on_reject=False,
            full_exit=True,
            stop_specs=[{"id": "stp-1", "qty": 10, "stop_price": 9.0}],
            wal_row_id=None,
            kept_leg=None,
        )
    return result, put_back


def test_a_exit_whose_submit_timed_out_but_landed_does_not_put_stops_back():
    seller, alert = _seller(_desk(_client(read_back=_placed)))
    result, put_back = _send(seller)
    assert result is not None, "a sale found at the broker must take the accepted path"
    order, prot = result
    assert order["id"] == prot["order_id"] == "ord-zzz-1"
    put_back.assert_not_called()
    alert.assert_not_called()


def test_b_timed_out_submit_not_found_stays_unknown_and_raises():
    client = _client(read_back=_NotFound("order not found"))
    with pytest.raises(TimeoutError) as raised:
        _desk(client).submit_order(symbol="ZZZ", qty=10, side="sell")
    assert any("outcome UNKNOWN" in note for note in getattr(raised.value, "__notes__", []))


def test_b_exit_with_unknown_submit_covers_what_is_held_and_alerts_the_owner():
    seller, alert = _seller(_desk(_client(read_back=_NotFound("order not found"))))
    result, put_back = _send(seller)
    assert result is None
    put_back.assert_called_once()
    alert.assert_called_once()
    assert "unknown" in alert.call_args.kwargs["why"]


def _sweep(client, row):
    db = MagicMock(name="db")
    db.get_orphaned_pending_submits.return_value = [row]
    sweep = FillReconciler(
        broker=SimpleNamespace(list_recent_orders=_desk(client).list_recent_orders),
        db=db,
        config=MagicMock(),
        flag_stop_out_anomaly=MagicMock(),
        format_qty=str,
        parse_broker_fill_timestamp=MagicMock(),
    )
    return sweep, db


def test_c_timed_out_short_entry_is_matched_by_its_stored_key():
    key = entry_client_order_id("YYY", "sell_short", 5, 20.0)
    client = MagicMock(name="client")
    client.get_orders.return_value = []  # a side listing finds nothing to match
    client.get_order_by_client_id.return_value = SimpleNamespace(id="ord-yyy-1", qty="5", status="new")
    sweep, db = _sweep(client, {"id": 7, "symbol": "YYY", "qty": 5, "action": "SHORT", "idempotency_key": key})
    assert sweep._reconcile_orphan_pending_submits() == 1
    client.get_order_by_client_id.assert_called_once_with(key)
    db.confirm_trade_submitted.assert_called_once_with(7, broker_order_id="ord-yyy-1")
    db.mark_trade_submit_failed.assert_not_called()


def test_c_key_the_broker_does_not_hold_is_marked_never_landed():
    client = MagicMock(name="client")
    client.get_order_by_client_id.side_effect = _NotFound("order not found")
    sweep, db = _sweep(client, {"id": 8, "symbol": "YYY", "qty": 5, "action": "SHORT", "idempotency_key": "k"})
    assert sweep._reconcile_orphan_pending_submits() == 1
    db.mark_trade_submit_failed.assert_called_once_with(8)
    db.confirm_trade_submitted.assert_not_called()


class _BrokerReply(OSError):
    """An OSError on purpose: the status code, not the class, marks a reply."""

    def __init__(self, status_code):
        super().__init__(f"status {status_code}")
        self.status_code = status_code


@pytest.mark.parametrize("code", [400, 403, 404, 500])
def test_d_a_reply_the_broker_returned_propagates_without_a_read_back(code):
    client = MagicMock(name="client")
    client.submit_order.side_effect = _BrokerReply(code)
    with pytest.raises(_BrokerReply):
        _desk(client).submit_order(symbol="ZZZ", qty=10, side="sell")
    client.get_order_by_client_id.assert_not_called()


def test_d_an_error_that_is_not_a_lost_reply_propagates_without_a_read_back():
    client = MagicMock(name="client")
    client.submit_order.side_effect = ValueError("bad request object")
    with pytest.raises(ValueError):
        _desk(client).submit_order(symbol="ZZZ", qty=10, side="sell")
    client.get_order_by_client_id.assert_not_called()


def test_c_pending_row_key_reaches_the_sweep_read(tmp_path):
    db = Database(str(tmp_path / "keys.db"))
    db.initialize()
    columns = {row[1] for row in db.conn.execute("PRAGMA table_info(current_account_trades)")}
    assert "idempotency_key" in columns


def test_d_an_http_error_carrying_a_response_propagates_without_a_read_back():
    import requests

    client = MagicMock(name="client")
    client.submit_order.side_effect = requests.exceptions.HTTPError("502", response=SimpleNamespace(status_code=502))
    with pytest.raises(requests.exceptions.HTTPError):
        _desk(client).submit_order(symbol="ZZZ", qty=10, side="sell")
    client.get_order_by_client_id.assert_not_called()


@pytest.mark.parametrize("lost", ["read_timeout", "connection_error", "builtin_timeout"])
def test_d_every_lost_reply_shape_is_read_back(lost):
    import requests

    exc = {
        "read_timeout": requests.exceptions.ReadTimeout("read timed out"),
        "connection_error": requests.exceptions.ConnectionError("connection aborted"),
        "builtin_timeout": TimeoutError("timed out"),
    }[lost]
    client = MagicMock(name="client")
    client.submit_order.side_effect = exc
    client.get_order_by_client_id.side_effect = _placed
    assert _desk(client).submit_order(symbol="ZZZ", qty=10, side="sell")["id"] == "ord-zzz-1"
