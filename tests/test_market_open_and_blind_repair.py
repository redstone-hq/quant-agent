"""One market-open answer; a stop repair never refuses for want of a price."""

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from src import coverage_watchdog
from src.execution.stop_repair import repair_stop_coverage
from src.trading_calendar import ET

_FRI_1005 = datetime(2026, 9, 11, 10, 5, tzinfo=ET).astimezone(timezone.utc)
_SAT_1005 = datetime(2026, 9, 12, 10, 5, tzinfo=ET).astimezone(timezone.utc)


def _dead_calendar():
    b = MagicMock()
    b.is_trading_day.side_effect = RuntimeError("calendar down")
    b.get_session_close.side_effect = RuntimeError("calendar down")
    return b


def test_both_callers_agree_and_fail_open_when_the_session_read_fails():
    from src.pipeline_protection import _market_is_open_now

    gate, _ = coverage_watchdog.session_is_open(_dead_calendar(), _FRI_1005)
    with patch("src.pipeline_protection.et_now", return_value=_FRI_1005.astimezone(ET)):
        pipe = _market_is_open_now(_dead_calendar())
    assert gate is True and pipe is True


def test_a_positive_shut_from_the_fallback_clock_is_still_honoured():
    from src.pipeline_protection import _market_is_open_now

    gate, _ = coverage_watchdog.session_is_open(_dead_calendar(), _SAT_1005)
    with patch("src.pipeline_protection.et_now", return_value=_SAT_1005.astimezone(ET)):
        pipe = _market_is_open_now(_dead_calendar())
    assert gate is False and pipe is False


def test_default_is_open_when_calendar_and_clock_both_fail():
    with patch("src.market_session.in_regular_session", side_effect=RuntimeError("x")):
        open_now, _ = coverage_watchdog.session_is_open(_dead_calendar(), _FRI_1005)
    assert open_now is True


def _broker(**kw):
    b = MagicMock()
    b._submit_protective_stop_retrying.return_value = {
        "id": "o1",
        "covered_qty": 5.0,
        "uncovered_qty": 0.0,
    }
    for k, v in kw.items():
        setattr(b, k, v)
    return b


def _run(broker, outcome=None):
    db = MagicMock()
    return repair_stop_coverage(
        broker=broker,
        last_buy=lambda s, action="BUY": {"stop_loss": 90.0},
        symbol="TEST",
        uncovered_qty=5.0,
        is_short=False,
        db=db,
        outcome=outcome,
    )


def test_price_read_fails_but_snapshot_succeeds_so_a_stop_is_placed():
    b = _broker()
    b.get_latest_price.side_effect = RuntimeError("feed down")
    b.get_latest_price_stamped.side_effect = RuntimeError("feed down")
    b.get_intraday_snapshots.return_value = {"TEST": {"snap": 1}}
    from types import SimpleNamespace

    with patch(
        "src.data.live_price.resolve_live_price", return_value=SimpleNamespace(price=100.0, source="minute_bar")
    ):
        assert _run(b) is True
    assert b._submit_protective_stop_retrying.called


def test_every_price_source_fails_still_places_the_recorded_stop_and_records_it():
    b = _broker()
    b.get_latest_price.side_effect = RuntimeError("feed down")
    b.get_latest_price_stamped.side_effect = RuntimeError("feed down")
    b.get_intraday_snapshots.side_effect = RuntimeError("feed down")
    outcome = {}
    with patch("src.execution.exit_path_records.record_stop_repair_refusal") as rec:
        assert _run(b, outcome) is True
    assert b._submit_protective_stop_retrying.call_args.kwargs["stop_price"] == 90.0
    assert outcome["repair_blind_placement"] == "price_unreadable"
    assert rec.call_args.kwargs["code"] == "price_unreadable_placed_blind"
