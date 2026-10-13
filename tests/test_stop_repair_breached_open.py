"""A recorded stop the tape already crossed is executed at the open, not abandoned.

k40 (measured 2026-10-12): the open-time repair refused to re-place a stop
the price had gapped through ("would fire immediately") and handed it to a
suspended reviewer, leaving the shares with no stop and no exit.
"""

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from src.execution.broker_parts.market_data import LivePrice
from src.execution.stop_repair import repair_stop_coverage
from src.trading_calendar import ET

_TUE_0931 = datetime(2026, 10, 13, 9, 31, tzinfo=ET)
_TUE_0800 = datetime(2026, 10, 13, 8, 0, tzinfo=ET)


def _print(price, at=_TUE_0931, *, source="last_trade", today_print=True):
    return LivePrice(price=price, source=source, trade_at=at, is_today_print=today_print, is_today=today_print)


def _broker(reading, open_orders=None, readable=True):
    b = MagicMock()
    b.STOP_LIMIT_BUFFER_PCT = 0.01
    b.get_latest_price_stamped.return_value = reading
    b.list_open_orders_checked.return_value = (readable, list(open_orders or []))
    b.submit_order.return_value = {"id": "mkt-1", "status": "accepted"}
    return b


def _run(broker, *, is_short=False, stop=90.0, qty=0.3456, owed=(), outcome=None, now=_TUE_0931):
    db = MagicMock()
    with (
        patch("src.storage.trades.pending_stop_amends_store.get_all", return_value=list(owed)),
        patch("src.trading_calendar.et_now", return_value=now),
        patch("src.execution.exit_path_records.record_stop_repair_refusal") as rec,
    ):
        result = repair_stop_coverage(
            broker=broker,
            last_buy=lambda s, action="BUY": {"stop_loss": stop},
            symbol="AMD",
            uncovered_qty=qty,
            is_short=is_short,
            db=db,
            outcome=outcome,
        )
    return result, rec


def test_long_gap_down_through_the_stop_sells_the_protected_qty():
    b = _broker(_print(85.0))
    outcome = {}
    result, rec = _run(b, outcome=outcome)
    assert result is False  # no stop placed: the position is being exited
    b.submit_order.assert_called_once()
    kw = b.submit_order.call_args.kwargs
    assert (kw["symbol"], kw["qty"], kw["side"]) == ("AMD", 0.3456, "sell")
    assert kw.get("limit_price") is None
    b._submit_protective_stop_retrying.assert_not_called()
    row = rec.call_args.kwargs
    assert row["code"] == "breached_stop_executed"
    assert row["placed"]["order_id"] == "mkt-1"
    assert row["placed"]["stop_price"] == 90.0 and row["placed"]["print_price"] == 85.0
    assert outcome["repair_refusal_code"] == "breached_stop_executed"


def test_short_gap_up_through_the_stop_covers():
    b = _broker(_print(115.0))
    result, rec = _run(b, is_short=True, stop=110.0, qty=3.0)
    kw = b.submit_order.call_args.kwargs
    assert (kw["qty"], kw["side"]) == (3.0, "buy")
    assert rec.call_args.kwargs["code"] == "breached_stop_executed"


def test_superseded_stop_with_an_owed_amend_does_not_trigger():
    b = _broker(_print(85.0))
    result, rec = _run(b, owed=[{"symbol": "AMD", "intended_stop": 80.0}])
    b.submit_order.assert_not_called()
    assert rec.call_args.kwargs["code"] == "would_fire_immediately"
    assert "superseded" in rec.call_args.kwargs["reason"]


def test_derived_level_is_never_executed():
    b = _broker(_print(85.0))
    with patch("src.execution.stop_repair.derive_protective_level", return_value=(90.0, "derived")):
        result, rec = _run(b, stop=None)
    b.submit_order.assert_not_called()
    assert rec.call_args.kwargs["code"] == "would_fire_immediately"


def test_second_run_does_not_double_sell():
    working = SimpleNamespace(id="mkt-1", symbol="AMD", side=SimpleNamespace(value="sell"), order_type="market")
    resting_stop = SimpleNamespace(id="gtc-1", symbol="AMD", side="sell", order_type="stop")
    b = _broker(_print(85.0), open_orders=[resting_stop, working])
    result, rec = _run(b)
    b.submit_order.assert_not_called()
    assert rec.call_args.kwargs["code"] == "breached_stop_exit_already_working"
    assert rec.call_args.kwargs["placed"]["order_id"] == "mkt-1"


def test_unreadable_open_orders_sends_nothing():
    b = _broker(_print(85.0), readable=False)
    result, rec = _run(b)
    b.submit_order.assert_not_called()
    assert rec.call_args.kwargs["code"] == "breached_stop_orders_unreadable"


def test_unreadable_print_still_refuses():
    # A quote (no trade print) cannot confirm the breach.
    b = _broker(_print(85.0, source="quote_mid", today_print=False))
    b.get_intraday_snapshots.return_value = {}
    result, rec = _run(b)
    b.submit_order.assert_not_called()
    codes = [c.kwargs["code"] for c in rec.call_args_list]
    assert codes and codes[-1] in ("would_fire_immediately", "no_trade_print_today")


def test_premarket_print_still_refuses_and_says_so():
    b = _broker(_print(85.0, at=_TUE_0800))
    result, rec = _run(b, now=_TUE_0800)
    b.submit_order.assert_not_called()
    assert rec.call_args.kwargs["code"] == "would_fire_immediately"
    assert "regular-session trade print" in rec.call_args.kwargs["reason"]


def test_kill_switch_refusal_is_recorded_not_reported_as_executed():
    b = _broker(_print(85.0))
    b.submit_order.return_value = {"id": None, "status": "kill_switch_halted", "detail": "kill switch"}
    result, rec = _run(b)
    assert result is False
    assert rec.call_args.kwargs["code"] == "breached_stop_exit_refused"
