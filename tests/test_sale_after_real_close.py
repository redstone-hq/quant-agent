"""No sale is submitted after the exchange's real close (defect of 20 Oct 2026).

On an early-close day (Fri 27 Nov 2026, close 13:00 ET) the :45 intraday run can
still be working at 13:05. The one real-close check at the protected sale's
submit reads the close from the broker calendar, so a sale reaching its submit
after it (or on an unreadable calendar) is not sent, its stops go back, and the
owner is told the desk decides it again at the next open.
"""

from __future__ import annotations

from datetime import date, datetime
from unittest.mock import MagicMock

from src.trading_calendar import ET
from tests.test_put_off_sales import _seam, _statuses, _submits
from tests.test_settle_legs import _pipe, _sell

EARLY = date(2026, 11, 27)  # day after Thanksgiving: 13:00 ET close
NORMAL = date(2026, 11, 25)
CLOSED_WHY = "not sold: the market had closed; the desk will decide it again at the next open"


def _at(monkeypatch, pipe, on: date, hh: int, mm: int, ss: int = 0, close_hh: int = 16):
    """Fix the desk clock at `on` hh:mm:ss ET and give the broker a calendar closing at close_hh:00."""
    import src.trading_calendar as tc

    now = datetime(on.year, on.month, on.day, hh, mm, ss, tzinfo=ET)
    monkeypatch.setattr(tc, "et_now", lambda: now)
    monkeypatch.setattr(tc, "utc_now", lambda: now)
    pipe.broker.get_session_open.side_effect = lambda d: datetime(d.year, d.month, d.day, 9, 30, tzinfo=ET)
    pipe.broker.get_session_close.side_effect = lambda d: datetime(d.year, d.month, d.day, close_hh, 0, tzinfo=ET)
    pipe._alert_owner_exit_declined = MagicMock()


def _closed_alerted(pipe) -> bool:
    return any(c.kwargs.get("why") == CLOSED_WHY for c in pipe._alert_owner_exit_declined.call_args_list)


def test_early_close_day_after_1300_sends_nothing_and_puts_the_sale_off(monkeypatch):
    pipe, calls = _pipe()
    _at(monkeypatch, pipe, EARLY, 13, 1, close_hh=13)
    assert _sell(pipe, 100.0) is None
    assert _submits(calls) == []
    assert pipe._last_stop_clear_refusal == "session_closed"
    assert _closed_alerted(pipe)


def test_early_close_day_before_1300_sells_as_before(monkeypatch):
    pipe, calls = _pipe()
    _at(monkeypatch, pipe, EARLY, 12, 59, close_hh=13)
    order, prot = _sell(pipe, 100.0)
    assert _submits(calls) == [("submit", 100.0)]
    assert order["id"] == "ord-1" and prot["submitted_qty"] == 100.0
    assert not _closed_alerted(pipe)


def test_normal_day_just_after_1600_sends_nothing(monkeypatch):
    pipe, calls = _pipe()
    _at(monkeypatch, pipe, NORMAL, 16, 0, 5)
    assert _sell(pipe, 100.0) is None
    assert _submits(calls) == []
    assert pipe._last_stop_clear_refusal == "session_closed"
    assert _closed_alerted(pipe)


def test_unreadable_calendar_fails_closed(monkeypatch):
    pipe, calls = _pipe()
    _at(monkeypatch, pipe, NORMAL, 11, 0)
    pipe.broker.get_session_close.side_effect = RuntimeError("calendar down")
    assert _sell(pipe, 100.0) is None
    assert _submits(calls) == []
    assert pipe._last_stop_clear_refusal == "session_closed"
    assert _closed_alerted(pipe)


def test_put_off_recheck_after_the_early_close_sends_nothing(monkeypatch):
    """The re-check's bound is a fixed 16:00 or the unit's kill; the submit check still refuses at 13:01."""
    pipe, calls = _pipe()
    _statuses(pipe, calls, ["pending_cancel", "canceled"])
    _at(monkeypatch, pipe, EARLY, 13, 1, close_hh=13)
    alert = pipe._alert_owner_exit_declined
    result, seen = _seam(pipe, monkeypatch, seconds=120)
    pipe._alert_owner_exit_declined = alert
    assert seen["sale"] is None
    assert _submits(calls) == []
    (outcome,) = result["put_off_sales"]
    assert outcome["outcome"] == "not_sold" and outcome["why"] == CLOSED_WHY
