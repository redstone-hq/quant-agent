"""The entry room subtracts orders still working at the BROKER.

`_entry_deployment_budget` is the one room figure both the intraday room
check and the execution sizing loop read. Before this, it subtracted only
HELD gross: an entry order resting unfilled at the broker (an end-of-run
cancel that failed, or any other survivor) counted as free room, so the
next run could size the same dollars again and breach the gross ceiling.
Every test here fails on the code before the fix.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from src.pipeline_sizing import _entry_deployment_budget
from src.risk.rules import GrossCeiling

EQUITY = 10_000.0
CEILING_X = 2.0
FULL_ROOM = CEILING_X * EQUITY  # empty book, no open orders


def _pipeline(open_orders=(), *, listed=True, last_price=None, broker_held=()):
    pipeline = SimpleNamespace()
    pipeline.broker = MagicMock(name="broker")
    pipeline.broker.list_open_orders_checked.return_value = (listed, list(open_orders))
    pipeline.broker.get_latest_price.return_value = last_price
    pipeline.broker.get_positions.return_value = list(broker_held)
    pipeline._resolve_gross_ceiling = lambda ctx: GrossCeiling(
        ceiling_x=CEILING_X, base_x=CEILING_X, drawdown_pct=None, alert_owner=False, rung="none", reason="test"
    )
    pipeline._sweep_symbol = lambda: "SGOV"
    pipeline.config = SimpleNamespace(risk=SimpleNamespace(allow_margin=True))
    return pipeline


def _order(oid="o1", symbol="NVDA", side="buy", qty=10, filled_qty=0, limit_price=50.0, status="new"):
    return SimpleNamespace(
        id=oid,
        symbol=symbol,
        side=side,
        qty=str(qty),
        filled_qty=str(filled_qty),
        limit_price=None if limit_price is None else str(limit_price),
        status=status,
        order_type="limit" if limit_price is not None else "market",
    )


def _held(symbol, qty, price):
    return SimpleNamespace(symbol=symbol, qty=qty, market_value=qty * price)


def _room(pipeline, positions=()):
    room, _ladder, note = _entry_deployment_budget(pipeline, None, list(positions), EQUITY, 0.0)
    return room, note


def test_an_open_buy_order_shrinks_room_by_its_remaining_notional(caplog):
    pipeline = _pipeline([_order(oid="abc-123", qty=10, limit_price=50.0)])
    with caplog.at_level(logging.INFO, logger="src.pipeline_stages"):
        room, note = _room(pipeline)
    assert room == pytest.approx(FULL_ROOM - 10 * 50.0)
    assert "open broker entry orders" in note
    assert any("abc-123" in r.getMessage() and "NVDA" in r.getMessage() for r in caplog.records)


def test_a_partially_filled_order_counts_only_its_unfilled_part():
    pipeline = _pipeline([_order(qty=10, filled_qty=4, limit_price=50.0)])
    room, _ = _room(pipeline)
    assert room == pytest.approx(FULL_ROOM - 6 * 50.0)


@pytest.mark.parametrize("failure", ["listing_failed", "listing_raised"])
def test_unreadable_open_orders_give_zero_room(failure, caplog):
    pipeline = _pipeline(listed=False)
    if failure == "listing_raised":
        pipeline.broker.list_open_orders_checked.side_effect = RuntimeError("broker down")
    with caplog.at_level(logging.WARNING, logger="src.pipeline_stages"):
        room, note = _room(pipeline)
    assert room == 0.0
    assert "open broker orders unreadable" in note
    assert any("open broker orders unreadable" in r.getMessage() for r in caplog.records)


def test_no_open_orders_leaves_room_unchanged():
    pipeline = _pipeline([])
    room, _ = _room(pipeline)
    assert room == pytest.approx(FULL_ROOM)
    pipeline.broker.list_open_orders_checked.assert_called_once_with()


def test_a_market_order_is_priced_at_the_last_price_and_unpriceable_fails_closed():
    priced = _pipeline([_order(qty=10, limit_price=None)], last_price=40.0)
    assert _room(priced)[0] == pytest.approx(FULL_ROOM - 10 * 40.0)
    unpriced = _pipeline([_order(qty=10, limit_price=None)], last_price=None)
    assert _room(unpriced)[0] == 0.0


def test_a_short_entry_counts_but_exits_and_dormant_bracket_legs_do_not():
    orders = [
        _order(oid="short", symbol="TSLA", side="sell_short", qty=5, limit_price=100.0),
        _order(oid="exit", symbol="AAPL", side="sell", qty=20, limit_price=10.0),
        _order(oid="leg", symbol="NVDA", side="sell", qty=10, limit_price=45.0, status="held"),
    ]
    pipeline = _pipeline(orders, broker_held=[_held("AAPL", 20, 10.0)])
    room, _ = _room(pipeline, positions=[_held("AAPL", 20, 10.0)])
    assert room == pytest.approx(FULL_ROOM - 20 * 10.0 - 5 * 100.0)


def test_a_rotation_projected_book_does_not_turn_the_outgoing_stop_into_a_short_entry():
    """Rotation passes the PROJECTED post-sale book, with the outgoing name
    already removed. Its protective sell-stop is still resting at the
    broker; classified against the projection it would look like a fresh
    short and erase the room the sale frees. Orders are classified against
    the broker's ACTUAL held book instead."""
    stop = _order(oid="stop-x", symbol="XOUT", side="sell", qty=30, limit_price=None, status="new")
    pipeline = _pipeline([stop], last_price=100.0, broker_held=[_held("XOUT", 30, 100.0)])
    room, _ = _room(pipeline, positions=[])  # projected book: XOUT already sold
    assert room == pytest.approx(FULL_ROOM)


def test_an_unreadable_broker_held_book_gives_zero_room_when_orders_are_open():
    pipeline = _pipeline([_order(qty=10, limit_price=50.0)])
    pipeline.broker.get_positions.side_effect = RuntimeError("broker down")
    room, note = _room(pipeline)
    assert room == 0.0
    assert "held book unreadable" in note
