"""The fake broker refuses what the real Alpaca paper broker refuses.

1. A sell/stop for more shares than are free (not held for other open
   orders) -> 403 / 40310000 "insufficient qty available" (held_for_orders),
   with the real body shape.
2. An order while an opposite-side order is open on the same symbol -> 403
   potential wash trade detected (the block documented in
   src/execution/scale_in.py).
3. Amending the quantity of a fractional order -> 422 / 42210000
   "qty must be an integer".
4. Cancelling an order that already filled -> 422 / 42210000
   'order is already in "filled" state'.
5. Amending an order the broker holds in the "accepted" state (after hours)
   -> 422 / 42210000 "cannot replace order in accepted status".

Items 1, 3, 4, 5 are copied from logged broker replies on the practice
account (practice-account order test and exit test logs, 9 and 13 Oct 2026;
ids, symbols and prices are invented). The cancel refusal is driven through a real desk call
path (AlpacaBroker.cancel_entry_order), which must report it and not assume
the cancel worked; the amend refusals are driven through the desk's client
seam (the desk's own amend wrappers either avoid them or, for
replace_order_qty, do not yet survive them: see the board note).
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest
from alpaca.trading.requests import ReplaceOrderRequest

from src.execution.broker import AlpacaBroker
from src.execution.broker_parts.stop_rejections import _is_held_for_orders_error
from tests.fakes.holding_broker import NEW, OLD, SYM, ApiErr, HoldingBroker

# The broker's reply to a SELL of 4 shares while a stop held all 10
# (practice account): fixed wording and fields as logged; ids and symbol invented.
REAL_403 = {
    "available": "0",
    "code": 40310000,
    "existing_qty": "10",
    "held_for_orders": "10",
    "message": "insufficient qty available for order (requested: 4, available: 0)",
    "related_orders": ["00000000-0000-4000-8000-000000000000"],
    "symbol": "ZZZ",
}


def _desk(fake):
    with patch("src.execution.broker.TradingClient", return_value=MagicMock()):
        b = AlpacaBroker(api_key="t", secret_key="t", paper=True)
    b.client = fake
    b._list_open_stop_orders_by_side = fake.by_side
    b._list_open_protective_stop_orders = lambda symbol, side="sell", errors=None: fake.by_side(symbol)[
        0 if side == "sell" else 1
    ]
    b.get_positions = fake.positions
    b._submit_stop_limit_order = fake.submit_stop
    b.wait_for_order_terminal = fake.wait_terminal
    return b


def test_stop_larger_than_position_refused():
    b = HoldingBroker(10)
    with pytest.raises(ApiErr) as e:
        b.submit_stop(SYM, 11, 95.0)
    assert e.value.status_code == 403 and "insufficient qty available" in str(e.value)


def test_second_stop_exceeding_free_shares_refused():
    b = HoldingBroker(10)
    b.rest(8)
    with pytest.raises(ApiErr) as e:
        b.submit_stop(SYM, 3, 95.0)
    assert e.value.status_code == 403 and "held_for_orders" in str(e.value)
    b.submit_stop(SYM, 2, 95.0)  # exactly the free shares is accepted


def test_sell_into_held_shares_refused():
    b = HoldingBroker(10)
    b.rest(10)
    with pytest.raises(ApiErr) as e:
        b.sell(SYM, 4)
    assert e.value.status_code == 403 and "insufficient qty available" in str(e.value)


def test_opposite_side_order_is_wash_trade_refused():
    b = HoldingBroker(10)
    b.rest(5)  # resting SELL stop
    with pytest.raises(ApiErr) as e:
        b.submit_stop(SYM, 1, 105.0, side="buy")
    assert e.value.status_code == 403 and "potential wash trade detected" in str(e.value)
    with pytest.raises(ApiErr) as e:
        b.sell(SYM, 1, side="buy")
    assert "potential wash trade detected" in str(e.value)


def test_wash_clears_once_opposite_order_cancelled():
    b = HoldingBroker(10)
    oid = b.rest(5)
    b.cancel_order_by_id(oid)  # confirmed cancel
    b.submit_stop(SYM, 1, 105.0, side="buy")


# ----------------------------------------------------------- the real wire shape


def test_sell_into_held_shares_has_the_real_403_code_message_and_body():
    fake = HoldingBroker(10)
    fake.rest(10)
    with pytest.raises(ApiErr) as e:
        fake.sell(SYM, 4)
    err = e.value
    assert err.status_code == 403 and err.code == 40310000
    assert err.message == REAL_403["message"]
    body = json.loads(str(err))
    assert set(body) == set(REAL_403)
    for key in ("available", "code", "existing_qty", "held_for_orders", "message"):
        assert body[key] == REAL_403[key], key
    assert body["related_orders"] == ["o1"] and body["symbol"] == SYM
    # the desk's own classifier reads it as the held-for-orders refusal
    assert _is_held_for_orders_error(err)


def test_qty_growth_into_held_shares_through_the_desk_client_gets_the_real_403():
    fake = HoldingBroker(10)
    first = fake.rest(6)
    fake.rest(4)
    with pytest.raises(ApiErr) as e:
        _desk(fake).client.replace_order_by_id(first, ReplaceOrderRequest(qty=8))
    assert e.value.status_code == 403 and e.value.code == 40310000
    assert e.value.message == "insufficient qty available for order (requested: 8, available: 6)"
    assert fake.book() == [(4.0, OLD), (6.0, OLD)]  # nothing moved


# ------------------------------------------------- fractional qty amend (422)


def test_fractional_qty_amend_through_the_desk_client_gets_the_real_422():
    fake = HoldingBroker(2.5)
    oid = fake.rest(2.5)
    with pytest.raises(ApiErr) as e:
        _desk(fake).client.replace_order_by_id(oid, ReplaceOrderRequest(qty=2))
    assert e.value.status_code == 422 and e.value.code == 42210000
    assert e.value.message == "qty must be an integer"
    assert fake.orders[oid].status == "new" and fake.orders[oid].qty == 2.5


# ------------------------------------------------ cancel of a filled order (422)


def test_cancelling_a_filled_order_is_refused_like_the_broker():
    fake = HoldingBroker(10)
    sold = fake.sell(SYM, 4)["id"]
    with pytest.raises(ApiErr) as e:
        fake.cancel_order_by_id(sold)
    assert e.value.status_code == 422 and e.value.code == 42210000
    assert e.value.message == 'order is already in "filled" state'
    assert fake.orders[sold].status == "filled"


def test_desk_cancel_of_a_filled_order_reports_failure_not_success():
    fake = HoldingBroker(10)
    sold = fake.sell(SYM, 4)["id"]
    assert _desk(fake).cancel_entry_order(sold) is False
    assert fake.orders[sold].status == "filled"  # not flipped to canceled


# --------------------------------------------- amend after hours, accepted (422)


def test_price_amend_after_hours_through_the_desk_client_gets_the_real_422():
    closed = HoldingBroker(10, market_open=False)
    oid = closed.rest(10)
    assert closed.orders[oid].status == "accepted" and closed.get_clock().is_open is False
    with pytest.raises(ApiErr) as e:
        _desk(closed).client.replace_order_by_id(oid, ReplaceOrderRequest(stop_price=NEW))
    assert e.value.status_code == 422 and e.value.code == 42210000
    assert e.value.message == "cannot replace order in accepted status"
    assert closed.orders[oid].stop_price == OLD and closed.orders[oid].status == "accepted"
    # control: the same amend in regular hours (status "new") is accepted
    opened = HoldingBroker(10)
    oid = opened.rest(10)
    assert _desk(opened).client.replace_order_by_id(oid, ReplaceOrderRequest(stop_price=NEW)).stop_price == NEW


def test_desk_does_not_ratchet_a_stop_after_hours_so_never_meets_that_422(monkeypatch):
    """The desk itself declines to amend while the market is closed (its own
    guard), so the fake's after-hours 422 is the net under that guard."""
    monkeypatch.setattr("src.notifier.send_owner_alert", lambda text, **kw: None)
    fake = HoldingBroker(10, market_open=False)
    fake.rest(10)
    _desk(fake).replace_stop_loss(SYM, NEW)
    assert [c for c in fake.calls if c[0] == "replace"] == []
