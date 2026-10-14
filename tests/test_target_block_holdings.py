"""The target block of a session message speaks only about what the desk holds,
and only about a close that has happened (live defect, 13 Oct 2026).

AAPL had been sold, yet the midday message still said it was "still quoted
$368.74 ... a second day's close", and said "today's close" for a close that
had not happened yet.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from src import notifier
from src.notifier.trade_body import _append_trade_session_body

_ET = ZoneInfo("America/New_York")


def _pending(symbol: str, code: str = "REFUSAL_WALL_PENDING_CONFIRMATION", prior: float = 368.74) -> dict:
    return {"symbol": symbol, "applied": False, "code": code, "prior_price": prior}


def _at(monkeypatch: pytest.MonkeyPatch, year: int, month: int, day: int, hour: int) -> None:
    moment = datetime(year, month, day, hour, 0, tzinfo=_ET)
    monkeypatch.setattr("src.trading_calendar.et_now", lambda: moment)


def test_a_sold_stock_has_no_target_line():
    result = {
        "target_revisions": [_pending("AAPL"), _pending("MSFT", prior=410.0)],
        "_positions": [{"symbol": "MSFT"}],
    }
    body = "\n".join(notifier.describe_target_revisions(result))
    assert "AAPL" not in body
    assert "MSFT" in body
    assert "1 position(s)" in body  # the count describes what is shown, not what was sold


def test_a_caller_that_knows_the_book_can_pass_it_in():
    result = {"target_revisions": [_pending("AAPL"), _pending("MSFT", prior=410.0)]}
    body = "\n".join(notifier.describe_target_revisions(result, held_symbols=["msft"]))
    assert "AAPL" not in body
    assert "MSFT" in body


def test_when_every_revised_stock_was_sold_the_block_is_empty():
    result = {"target_revisions": [_pending("AAPL")]}
    assert notifier.describe_target_revisions(result, held_symbols=[]) == []


def test_an_applied_revision_on_a_sold_stock_is_dropped_too():
    applied = {"symbol": "AAPL", "applied": True, "prior_price": 380.0, "new_price": 370.0, "trigger": ""}
    result = {"target_revisions": [applied], "_positions": [{"symbol": "MSFT"}]}
    assert notifier.describe_target_revisions(result) == []


def test_without_any_book_nothing_is_dropped():
    # No holdings known: say what the session measured rather than invent a book.
    body = "\n".join(notifier.describe_target_revisions({"target_revisions": [_pending("AAPL")]}))
    assert "AAPL" in body


def test_the_trade_session_message_drops_the_sold_stock():
    result = {
        "status": "reviewed",
        "orders": [],
        "target_revisions": [_pending("AAPL"), _pending("MSFT", prior=410.0)],
        "_positions": [{"symbol": "MSFT"}],
    }
    lines: list[str] = []
    _append_trade_session_body(lines, result)
    body = "\n".join(lines)
    assert "AAPL" not in body
    assert "MSFT" in body


@pytest.mark.parametrize(
    "code",
    [
        "REFUSAL_WALL_PENDING_CONFIRMATION",
        "REFUSAL_REACH_PENDING_CONFIRMATION",
        "REFUSAL_BREAK_PENDING_CONFIRMATION",
    ],
)
def test_midday_never_says_todays_close(monkeypatch: pytest.MonkeyPatch, code: str):
    _at(monkeypatch, 2026, 10, 13, 12)  # Tuesday midday: today's close has not happened
    body = "\n".join(notifier.describe_target_revisions({"target_revisions": [_pending("MSFT", code)]}))
    assert "today's close" not in body
    assert "Mon 12 Oct" in body  # the close it actually refers to


def test_after_the_bell_the_close_is_named_by_its_date_too(monkeypatch: pytest.MonkeyPatch):
    _at(monkeypatch, 2026, 10, 13, 17)
    body = "\n".join(notifier.describe_target_revisions({"target_revisions": [_pending("MSFT")]}))
    assert "today's close" not in body
    assert "Tue 13 Oct" in body


def test_monday_midday_names_friday_not_a_weekend_day(monkeypatch: pytest.MonkeyPatch):
    _at(monkeypatch, 2026, 10, 12, 12)
    body = "\n".join(notifier.describe_target_revisions({"target_revisions": [_pending("MSFT")]}))
    assert "Fri 9 Oct" in body
