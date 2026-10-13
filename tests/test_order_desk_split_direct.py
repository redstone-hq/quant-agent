"""order_desk_reads exercised directly with a fake desk and fake client."""

from types import SimpleNamespace

from src.execution.broker_parts import order_desk_reads as reads


def _order(**kw):
    return SimpleNamespace(**kw)


def _desk(orders, prices=None, fail=False):
    def get_orders(filter):
        if fail:
            raise RuntimeError("api down")
        return orders

    client = SimpleNamespace(get_orders=get_orders, get_order_by_id=lambda oid: orders[0])
    return SimpleNamespace(client=client, get_latest_price=lambda sym: (prices or {}).get(sym))


def test_open_buy_notional_mixed_orders():
    orders = [
        _order(side="buy", qty="10", limit_price="5.0", stop_price=None, symbol="A"),
        _order(side="sell", qty="99", limit_price="100", stop_price=None, symbol="B"),
        _order(side="buy", qty="2", limit_price=None, stop_price="7.5", symbol="C"),
        _order(side="buy", qty="4", limit_price="bad", stop_price=None, symbol="D"),
        _order(side="buy", qty="garbage", limit_price="3", stop_price=None, symbol="E"),
    ]
    # D has an unparseable limit and no stop: priced from the live quote (2.0).
    total = reads.open_buy_notional(_desk(orders, {"D": 2.0}))
    assert total == 10 * 5.0 + 2 * 7.5 + 4 * 2.0 + 0.0


def test_open_buy_notional_unknowable_price_is_none():
    orders = [_order(side="buy", qty="1", limit_price=None, stop_price=None, symbol="X")]
    assert reads.open_buy_notional(_desk(orders, {})) is None


def test_open_buy_notional_empty_is_zero_and_failure_is_none():
    assert reads.open_buy_notional(_desk([])) == 0.0
    assert reads.open_buy_notional(_desk([], fail=True)) is None


def test_list_open_entry_orders_checked_filters_stops_and_side():
    orders = [
        _order(id="1", side="buy", order_type="limit"),
        _order(id="2", side="sell", order_type="stop"),
        _order(id="3", side="sell", order_type="limit"),
        _order(id=None, side="buy", order_type="limit"),
    ]
    desk = _desk(orders)
    # The ids reader calls back through the desk, as the verbatim body does.
    desk.list_open_entry_orders_checked = lambda s, side=None: reads.list_open_entry_orders_checked(desk, s, side=side)
    assert reads.list_open_entry_orders_checked(desk, "AAA") == (True, ["1", "3"])
    assert reads.list_open_entry_order_ids(desk, "AAA", side="sell") == ["3"]
    assert reads.list_open_entry_orders_checked(_desk([], fail=True), "AAA") == (False, [])


def test_get_order_fill_info():
    o = _order(status="filled", filled_qty="3", filled_avg_price="2.5")
    assert reads.get_order_fill_info(_desk([o]), "z") == {
        "status": "filled",
        "filled_qty": 3.0,
        "filled_avg_price": 2.5,
        "filled_at": None,  # the broker reported no fill time
    }
