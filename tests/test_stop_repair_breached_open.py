"""A recorded stop the tape already crossed is executed at the open, not abandoned.

k40 (measured 2026-10-12): the open-time repair refused to re-place a stop
the price had gapped through ("would fire immediately") and handed it to a
suspended reviewer, leaving the shares with no stop and no exit. The exit
now goes through the desk's protected sell path, sized off a fresh position
read.
"""

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from src.execution.broker_parts.market_data import LivePrice
from src.execution.broker_parts.trim_book import trim_keeps_shares
from src.execution.stop_repair import repair_stop_coverage
from src.protection.breached_stop_closer import build_breached_stop_closer
from src.protection.fill_reconciler import FillReconciler
from src.trading_calendar import ET

_TUE_0931 = datetime(2026, 10, 13, 9, 31, tzinfo=ET)
_TUE_0800 = datetime(2026, 10, 13, 8, 0, tzinfo=ET)


def _print(price, at=_TUE_0931, *, source="last_trade", today_print=True):
    return LivePrice(price=price, source=source, trade_at=at, is_today_print=today_print, is_today=today_print)


def _pos(qty, symbol="AMD"):
    return SimpleNamespace(symbol=symbol, qty=str(qty))


def _broker(reading, *, held, open_orders=None, readable=True, resting=()):
    """A mock broker returning realistic position / order objects."""
    b = MagicMock()
    b.STOP_LIMIT_BUFFER_PCT = 0.01
    b.get_latest_price_stamped.return_value = reading
    b.list_open_orders_checked.return_value = (readable, list(open_orders or []))
    b.get_positions.return_value = [_pos(held), _pos(7, "MSFT")]
    b.snapshot_protective_stops.return_value = (True, list(resting))
    b.trim_keeps_shares.side_effect = trim_keeps_shares
    b.clear_stops_for_trim.return_value = (True, [], None, dict(resting[0]) if resting else None)
    b.submit_order.return_value = {"id": "mkt-1", "status": "accepted", "symbol": "AMD"}
    return b


def _closer(broker, db):
    """The real protected-sell closer the session caller injects."""
    return build_breached_stop_closer(
        broker=broker, db=db, order_accepted=FillReconciler._order_accepted, alert_exit_declined=MagicMock()
    )


def _run(
    broker, *, is_short=False, stop=90.0, qty=0.3456, owed=(), outcome=None, now=_TUE_0931, resting=None, closer=True
):
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
            resting_stops=resting,
            close_breached=_closer(broker, db) if closer else None,
        )
    return result, rec, db


def _sent(b):
    kw = b.submit_order.call_args.kwargs
    return kw["qty"], kw["side"], kw.get("limit_price")


_GTC = {"id": "gtc-1", "qty": 10.0, "stop_price": 90.0}


def test_long_gap_down_sells_only_the_unprotected_remainder_via_protected_path():
    b = _broker(_print(85.0), held=10.3456, resting=[_GTC])
    outcome = {}
    result, rec, db = _run(b, outcome=outcome, resting=[_GTC])
    assert result is False  # no stop placed: the remainder is being exited
    assert _sent(b) == (0.3456, "sell", None)
    b.clear_stops_for_trim.assert_called_once()  # trim path: the whole-share stop stays
    b.cancel_snapshotted_stops.assert_not_called()
    row = rec.call_args.kwargs
    assert row["code"] == "breached_stop_executed"
    assert row["placed"]["order_id"] == "mkt-1" and row["placed"]["label"] == "PARTIAL_SELL"
    assert row["placed"]["level_source"] == "broker_resting_stop"
    assert row["placed"]["print_price"] == 85.0
    assert db.insert_trade.call_args.kwargs["action"] == "PARTIAL_SELL"
    assert db.insert_trade.call_args.kwargs["broker_order_id"] == "mkt-1"


def test_whole_share_stop_filled_between_reads_sells_the_remainder_only():
    # The sweep saw 10.3456 held with nothing covering it; by the fresh read
    # the whole-share stop had filled and only 0.3456 remains.
    b = _broker(_print(85.0), held=0.3456)
    result, rec, db = _run(b, qty=10.3456)
    assert _sent(b) == (0.3456, "sell", None)
    assert rec.call_args.kwargs["placed"]["label"] == "SELL"
    assert rec.call_args.kwargs["placed"]["level_source"] == "recorded_row"


def test_short_gap_up_covers_the_whole_short():
    b = _broker(_print(115.0), held=-3)
    result, rec, db = _run(b, is_short=True, stop=110.0, qty=3.0)
    assert _sent(b) == (3.0, "buy", None)
    assert db.insert_trade.call_args.kwargs["action"] == "COVER"


def test_short_whose_stop_already_filled_covers_only_what_is_still_short():
    b = _broker(_print(115.0), held=-2)
    result, rec, db = _run(b, is_short=True, stop=110.0, qty=5.0)
    assert _sent(b) == (2.0, "buy", None)  # never 5: that would flip the account long 3


def test_short_partly_held_by_a_resting_stop_is_never_covered_in_part():
    b = _broker(_print(115.0), held=-5)
    result, rec, db = _run(b, is_short=True, stop=110.0, qty=2.0)
    b.submit_order.assert_not_called()
    assert rec.call_args.kwargs["code"] == "breached_stop_partial_cover_refused"


def test_nothing_held_any_more_sends_nothing_and_records_it():
    b = _broker(_print(85.0), held=0)
    result, rec, db = _run(b)
    b.submit_order.assert_not_called()
    assert rec.call_args.kwargs["code"] == "breached_stop_nothing_held"


def test_superseded_stop_with_an_owed_amend_does_not_trigger():
    b = _broker(_print(85.0), held=10.3456)
    result, rec, db = _run(b, owed=[{"symbol": "AMD", "intended_stop": 80.0}])
    b.submit_order.assert_not_called()
    assert rec.call_args.kwargs["code"] == "would_fire_immediately"
    assert "superseded" in rec.call_args.kwargs["reason"]


def test_resting_broker_stop_not_crossed_makes_the_recorded_level_stale():
    resting = [{"id": "gtc-1", "qty": 10.0, "stop_price": 80.0}]
    b = _broker(_print(85.0), held=10.3456, resting=resting)
    result, rec, db = _run(b, resting=resting)
    b.submit_order.assert_not_called()
    assert rec.call_args.kwargs["code"] == "would_fire_immediately"
    assert "stale" in rec.call_args.kwargs["reason"]


def test_derived_level_is_never_executed():
    b = _broker(_print(85.0), held=10.3456)
    with patch("src.execution.stop_repair.derive_protective_level", return_value=(90.0, "derived")):
        result, rec, db = _run(b, stop=None)
    b.submit_order.assert_not_called()
    assert rec.call_args.kwargs["code"] == "would_fire_immediately"


def test_second_run_does_not_double_sell():
    working = SimpleNamespace(id="mkt-1", symbol="AMD", side=SimpleNamespace(value="sell"), order_type="market")
    resting_stop = SimpleNamespace(id="gtc-1", symbol="AMD", side="sell", order_type="stop")
    b = _broker(_print(85.0), held=10.3456, open_orders=[resting_stop, working])
    result, rec, db = _run(b)
    b.submit_order.assert_not_called()
    assert rec.call_args.kwargs["code"] == "breached_stop_exit_already_working"
    assert rec.call_args.kwargs["placed"]["order_id"] == "mkt-1"


def test_unreadable_open_orders_sends_nothing():
    b = _broker(_print(85.0), held=10.3456, readable=False)
    result, rec, db = _run(b)
    b.submit_order.assert_not_called()
    assert rec.call_args.kwargs["code"] == "breached_stop_orders_unreadable"


def test_unreadable_print_still_refuses():
    b = _broker(_print(85.0, source="quote_mid", today_print=False), held=10.3456)
    b.get_intraday_snapshots.return_value = {}
    result, rec, db = _run(b)
    b.submit_order.assert_not_called()
    codes = [c.kwargs["code"] for c in rec.call_args_list]
    assert codes and codes[-1] in ("would_fire_immediately", "no_trade_print_today")


def test_premarket_print_still_refuses_and_says_so():
    b = _broker(_print(85.0, at=_TUE_0800), held=10.3456)
    result, rec, db = _run(b, now=_TUE_0800)
    b.submit_order.assert_not_called()
    assert rec.call_args.kwargs["code"] == "would_fire_immediately"
    assert "regular-session trade print" in rec.call_args.kwargs["reason"]


def test_kill_switch_refusal_is_recorded_not_reported_as_executed():
    b = _broker(_print(85.0), held=0.3456)
    b.submit_order.return_value = {"id": None, "status": "kill_switch_halted", "detail": "kill switch"}
    result, rec, db = _run(b)
    assert result is False
    assert rec.call_args.kwargs["code"] == "breached_stop_exit_refused"
    db.insert_trade.assert_not_called()


def test_no_injected_exit_path_keeps_the_refusal_and_says_why():
    b = _broker(_print(85.0), held=0.3456)
    result, rec, db = _run(b, closer=False)
    b.submit_order.assert_not_called()
    assert rec.call_args.kwargs["code"] == "would_fire_immediately"
    assert "no exit path" in rec.call_args.kwargs["reason"]


def test_execution_layer_does_not_import_the_protection_layer():
    import ast
    from pathlib import Path

    tree = ast.parse((Path(__file__).resolve().parents[1] / "src" / "execution" / "stop_repair.py").read_text())
    names = [n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)]
    assert not [m for m in names if m.startswith("src.protection")]
