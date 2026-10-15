"""Every order submission carries a DETERMINISTIC `client_order_id`.

Alpaca uses `client_order_id` as the idempotency key for POST /v2/orders:
a second POST for an id an active order already holds is refused with
HTTP 422 "client_order_id must be unique"
(https://alpaca.markets/learn/how-to-fix-common-trading-api-errors-at-alpaca).
These tests prove (b) a timeout-then-retry leaves ONE order at a stub
broker, not two, and (c) no other request field changed; (a) the key being a
pure function of the intent is tests/test_order_idempotency_key.py. No real account id, no live desk output.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from alpaca.common.exceptions import APIError
from alpaca.trading.requests import (
    LimitOrderRequest,
    MarketOrderRequest,
    StopOrderRequest,
)

from src.execution import broker as broker_mod
from src.execution.broker import AlpacaBroker
from src.execution.order_idempotency import _CLIENT_ORDER_ID_MAX_LEN

DAY = "2026-10-01"


# ----------------------------------------------------------- (b) retry
def _duplicate_error() -> APIError:
    resp = SimpleNamespace(status_code=422)
    http_err = SimpleNamespace(response=resp, request=None)
    body = json.dumps({"code": 40010001, "message": "client_order_id must be unique"})
    return APIError(body, http_err)


# Statuses after which Alpaca no longer holds the order's key. Alpaca's
# troubleshooting guide (cited in the module docstring) says: "Make sure to
# use a unique client_order_id for each ACTIVE order" — uniqueness is among
# ACTIVE orders only. The stub therefore models ACTIVE-ONLY uniqueness: once
# an order is terminal its key is FREE and a resubmission under it is
# ACCEPTED as a brand-new order, never refused. (The first draft of this stub
# modelled global uniqueness — a broker that does not exist — and the tests
# passed against it; see the incident note of 2026-10-01.)
_STUB_TERMINAL = frozenset(
    {
        "filled",
        "canceled",
        "cancelled",
        "expired",
        "rejected",
        "replaced",
        "done_for_day",
        "calculated",
        "suspended",
    }
)


def _status_str(order) -> str:
    return str(getattr(order.status, "value", order.status)).lower()


class _StubBroker:
    """Alpaca stand-in with ACTIVE-ONLY `client_order_id` uniqueness: a POST
    whose key is held by a NON-terminal order is refused with the real 422
    text; a key held only by terminal orders is free again. `timeout_next`
    makes the next POST ACCEPT the order and then raise, as a dropped
    connection would. `orders` holds every order ever accepted (keyed by
    broker id); `by_key` maps a key to the most recent order holding it."""

    def __init__(self):
        self.orders: dict[str, SimpleNamespace] = {}
        self.by_key: dict[str, SimpleNamespace] = {}
        self.timeout_next = False
        self.posts = 0

    def _active_holder(self, cid):
        holder = self.by_key.get(cid)
        if holder is not None and _status_str(holder) not in _STUB_TERMINAL:
            return holder
        return None

    def submit_order(self, req):
        self.posts += 1
        if self._active_holder(req.client_order_id) is not None:
            raise _duplicate_error()
        order = SimpleNamespace(
            id=f"srv-{len(self.orders) + 1}",
            status="accepted",
            symbol=req.symbol,
            client_order_id=req.client_order_id,
        )
        self.orders[order.id] = order
        self.by_key[req.client_order_id] = order
        if self.timeout_next:
            self.timeout_next = False
            raise TimeoutError("read timed out after broker accepted")
        return order

    def get_order_by_client_id(self, cid):
        return self.by_key[cid]


def _broker_with(stub):
    with patch("src.execution.broker.TradingClient", return_value=stub):
        b = AlpacaBroker(api_key="test", secret_key="test", paper=True)
    b.client = stub
    return b


@patch("src.execution.order_idempotency._session_date_key", return_value=DAY)
def test_entry_timeout_then_retry_leaves_one_order(_d):
    stub = _StubBroker()
    b = _broker_with(stub)
    stub.timeout_next = True
    # The broker accepted but the reply timed out: the desk reads the order
    # back by its own key and returns it as placed (no TimeoutError).
    first = b.submit_order(symbol="AAPL", qty=10, side="buy", limit_price=100.0)
    assert first["id"] == "srv-1" and first["status"] == "accepted"
    assert stub.posts == 1 and len(stub.orders) == 1
    result = b.submit_order(symbol="AAPL", qty=10, side="buy", limit_price=100.0)
    assert stub.posts == 2
    assert len(stub.orders) == 1, "retry must NOT create a second position"
    assert result["id"] == "srv-1" and result["status"] == "accepted"
    assert result["qty"] == 10 and result["limit_price"] == 100.0
    # A genuinely new intent the same day is still accepted.
    b.submit_order(symbol="AAPL", qty=10, side="buy", limit_price=101.0)
    assert len(stub.orders) == 2


@patch("src.execution.order_idempotency._session_date_key", return_value=DAY)
def test_entry_duplicate_is_not_reported_as_broker_rejection(_d):
    stub = _StubBroker()
    b = _broker_with(stub)
    b.submit_order(symbol="AAPL", qty=5, side="buy")
    again = b.submit_order(symbol="AAPL", qty=5, side="buy")
    assert again["status"] == "accepted" and again["id"] == "srv-1"
    assert again["status"] != "rejected_by_broker"


@patch("src.execution.order_idempotency._session_date_key", return_value=DAY)
def test_stop_timeout_then_retry_leaves_one_stop(_d):
    stub = _StubBroker()
    b = _broker_with(stub)
    stub.timeout_next = True
    with pytest.raises(TimeoutError):
        b._submit_stop_limit_order("AAPL", 10, 95.0)
    res = b._submit_stop_limit_order("AAPL", 10, 95.0)
    assert len(stub.orders) == 1 and res["id"] == "srv-1"
    # A ratcheted trail (new trigger) is a NEW intent.
    b._submit_stop_limit_order("AAPL", 10, 96.0)
    assert len(stub.orders) == 2


@patch("src.execution.order_idempotency._session_date_key", return_value=DAY)
def test_duplicate_whose_lookup_fails_raises_not_rejects(_d):
    stub = _StubBroker()
    b = _broker_with(stub)
    b.submit_order(symbol="AAPL", qty=5, side="buy")
    stub.get_order_by_client_id = MagicMock(side_effect=ConnectionError("down"))
    with pytest.raises(RuntimeError, match="treat as PLACED"):
        b.submit_order(symbol="AAPL", qty=5, side="buy")


# ------------------------------------------------- (c) fields unchanged
@patch("src.execution.order_idempotency._session_date_key", return_value=DAY)
def test_every_other_request_field_is_unchanged(_d):
    client = MagicMock()
    client.submit_order.return_value = SimpleNamespace(id="o", status="accepted", symbol="BRK.B")
    client.get_all_positions.return_value = [SimpleNamespace(symbol="AAPL", qty="3")]
    with patch("src.execution.broker.TradingClient", return_value=client):
        b = AlpacaBroker(api_key="test", secret_key="test", paper=True)
    b.submit_order(symbol="BRK-B", qty=2, side="buy", limit_price=500.0)
    b.submit_order(symbol="AAPL", qty=3, side="sell")
    b._submit_stop_limit_order("AAPL", 3, 95.0)
    b._submit_stop_limit_order("MSFT", 2.5, 95.0, side="buy")  # fractional -> DAY
    reqs = [c.args[0] for c in client.submit_order.call_args_list]
    assert [type(r) for r in reqs] == [LimitOrderRequest, MarketOrderRequest, StopOrderRequest, StopOrderRequest]
    expected = [
        {"symbol": "BRK.B", "qty": 2.0, "side": "buy", "type": "limit", "time_in_force": "day", "limit_price": 500.0},
        {"symbol": "AAPL", "qty": 3.0, "side": "sell", "type": "market", "time_in_force": "day"},
        {"symbol": "AAPL", "qty": 3.0, "side": "sell", "type": "stop", "time_in_force": "gtc", "stop_price": 95.0},
        {"symbol": "MSFT", "qty": 2.5, "side": "buy", "type": "stop", "time_in_force": "day", "stop_price": 95.0},
    ]
    for r, exp in zip(reqs, expected):
        dumped = r.model_dump(exclude_none=True, mode="json")
        cid = dumped.pop("client_order_id")
        assert dumped == exp, dumped  # byte-for-byte the pre-change request
        assert 0 < len(cid) <= _CLIENT_ORDER_ID_MAX_LEN
    assert reqs[0].client_order_id == f"ENT-BRK.B-buy-{DAY}-2.0-500.0"
    assert reqs[2].client_order_id == f"STP-AAPL-sell-{DAY}-3.0-95.0"


# ---------------------------------------------------------------------------
# Adversary finding (2026-10-01): the duplicate-key read-back must INSPECT the
# order's status. A stop cancelled moments ago still holds its key while it is
# `pending_cancel` (still ACTIVE at Alpaca); once `canceled` the key is free
# and the plain key is simply accepted. Either way one live stop must result,
# and the dying order must never be counted as protection.
# ---------------------------------------------------------------------------
from src.execution.broker import PROTECTIVE_ORDER_ALIVE_STATUSES


class _Status:
    """Stand-in for an alpaca OrderStatus enum member (`.value`)."""

    def __init__(self, value: str):
        self.value = value


def _stub_holding_dead_stop(status: str) -> _StubBroker:
    """A broker whose most recent holder of THIS stop's key has `status`
    (dying `pending_cancel`, dead `canceled`, amending `pending_replace`,
    ...), as it does right after `cancel_protective_stops` /
    `replace_stop_loss` / `replace_order_by_id` touch it. Whether the key is
    still RESERVED follows from the status (active-only uniqueness)."""
    stub = _StubBroker()
    key = broker_mod._client_order_id(
        purpose="STP",
        symbol="AAPL",
        side="sell",
        session_date=DAY,
        qty=10,
        price=95.0,
    )
    dead = SimpleNamespace(
        id="dead-1",
        status=_Status(status),
        symbol="AAPL",
        client_order_id=key,
    )
    stub.orders[dead.id] = dead
    stub.by_key[key] = dead
    return stub


def _live_orders(stub: _StubBroker) -> list:
    return [o for o in stub.orders.values() if _status_str(o) in PROTECTIVE_ORDER_ALIVE_STATUSES]


@pytest.mark.parametrize("dead", ["pending_cancel", "canceled"])
@patch("src.execution.order_idempotency._session_date_key", return_value=DAY)
def test_restore_does_not_count_dead_readback_as_protection(_d, dead):
    stub = _stub_holding_dead_stop(dead)
    b = _broker_with(stub)
    restored, failed = b._restore_stop_orders(
        "AAPL",
        [{"qty": 10, "stop_price": 95.0}],
    )
    live = _live_orders(stub)
    assert live, f"restore read back a {dead} order for its key and placed nothing — the position is unprotected"
    assert live[0].id != "dead-1"
    assert restored == 1 and failed == []


@pytest.mark.parametrize("dead", ["pending_cancel", "canceled"])
@patch("src.execution.order_idempotency._session_date_key", return_value=DAY)
def test_stop_leg_retry_does_not_return_dead_readback(_d, dead):
    stub = _stub_holding_dead_stop(dead)
    b = _broker_with(stub)
    with patch("time.sleep"):
        res = b._submit_stop_leg_retrying(
            symbol="AAPL",
            qty=10,
            stop_price=95.0,
            side="sell",
            leg="GTC",
        )
    live = _live_orders(stub)
    assert live, f"stop leg read back a {dead} order for its key and placed nothing — the position is unprotected"
    assert res is not None
    assert res["id"] == live[0].id and res["id"] != "dead-1"
    assert res["status"] in PROTECTIVE_ORDER_ALIVE_STATUSES


@patch("src.execution.order_idempotency._session_date_key", return_value=DAY)
def test_superseding_key_is_itself_idempotent(_d):
    """The replacement stop's key differs from the dead one's, but a RETRY
    of the replacement reuses it — one dead order, ONE live order, never
    two live ones."""
    stub = _stub_holding_dead_stop("canceled")
    b = _broker_with(stub)
    first = b._submit_stop_limit_order("AAPL", 10, 95.0)
    again = b._submit_stop_limit_order("AAPL", 10, 95.0)
    assert first["id"] == again["id"] != "dead-1"
    assert len(stub.orders) == 2 and len(_live_orders(stub)) == 1


# ---------------------------------------------------------------------------
# Adversary round 3 (2026-10-01), hole 1: with ACTIVE-ONLY uniqueness the
# superseding key does NOT deliver "never two live stops". Sequence:
#   1. stop A is `pending_cancel` and holds the plain key K;
#   2. restore POSTs K -> 422 -> reads back A (dying) -> POSTs K+sA -> the
#      broker ACCEPTS stop B but the connection drops (timeout);
#   3. A's cancel completes (`canceled`), so K is FREE again;
#   4. the retry POSTs K -> accepted outright as stop C. No 422 anywhere.
# B and C both rest. This test asserts what the code DOES, not what the
# first draft claimed; the limitation is recorded in INCIDENT_HISTORY.md.
# ---------------------------------------------------------------------------
@patch("src.execution.order_idempotency._session_date_key", return_value=DAY)
def test_KNOWN_LIMITATION_retry_after_cancel_completes_leaves_two_live_stops(_d):
    stub = _stub_holding_dead_stop("pending_cancel")
    b = _broker_with(stub)
    stub.timeout_next = True
    with pytest.raises(TimeoutError):
        b._submit_stop_limit_order("AAPL", 10, 95.0)  # B accepted, POST timed out
    assert len(_live_orders(stub)) == 1
    stub.orders["dead-1"].status = _Status("canceled")  # A's cancel completes; K is free
    res = b._submit_stop_limit_order("AAPL", 10, 95.0)  # the retry
    live = _live_orders(stub)
    # REALITY, not the guarantee: the retry is accepted as a SECOND live stop.
    assert len(live) == 2, (
        "if this is now 1 the limitation has been closed — update the incident note and flip this assertion"
    )
    assert res["id"] == live[-1].id and res["status"] == "accepted"


# ---------------------------------------------------------------------------
# Adversary round 3 (2026-10-01), hole 2: a read-back whose status means the
# order is being AMENDED in place (`pending_replace` — the desk amends stops
# via replace_order_by_id) or is ALREADY EXECUTING / EXECUTED (`stopped`,
# `calculated`, `filled`) still holds the shares. Superseding it places a
# second sell over shares that are already protected or already sold.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("status", ["pending_replace", "stopped"])
@patch("src.execution.order_idempotency._session_date_key", return_value=DAY)
def test_amending_or_executing_readback_is_not_superseded(_d, status):
    """Both statuses are still ACTIVE at Alpaca, so the plain key IS refused
    and the read-back reaches the status check. (`calculated` / `filled` are
    terminal: the key is free, the broker never 422s, so they are tested on
    the placement response below instead.)"""
    stub = _stub_holding_dead_stop(status)
    b = _broker_with(stub)
    res = b._submit_stop_limit_order("AAPL", 10, 95.0)
    assert res["id"] == "dead-1", f"a {status} stop was superseded by a second stop"
    assert len(stub.orders) == 1, "no second order may be submitted"


@pytest.mark.parametrize("status", ["pending_replace", "stopped", "calculated", "filled"])
@patch("src.execution.order_idempotency._session_date_key", return_value=DAY)
def test_stop_leg_retry_does_not_retry_over_an_executing_stop(_d, status):
    """`_submit_stop_leg_retrying` must not judge an amending / executing /
    executed placement response as a failed attempt: a retry is a fresh
    sell (a stop-MARKET placed through its trigger can come back `filled`
    on the placement response itself)."""
    client = MagicMock()
    client.submit_order.return_value = SimpleNamespace(
        id="o1",
        status=_Status(status),
        symbol="AAPL",
    )
    with patch("src.execution.broker.TradingClient", return_value=client):
        b = AlpacaBroker(api_key="test", secret_key="test", paper=True)
    with patch("time.sleep"):
        res = b._submit_stop_leg_retrying(
            symbol="AAPL",
            qty=10,
            stop_price=95.0,
            side="sell",
            leg="GTC",
        )
    assert res is not None and res["id"] == "o1"
    assert client.submit_order.call_count == 1, f"retry placed a second sell over a {status} stop"
