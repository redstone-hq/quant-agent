"""The target block of a session message speaks only about what the desk holds,
and only about a close that has happened (live defect, 13 Oct 2026).

AAPL had been sold, yet the midday message still said it was "still quoted
$368.74 ... a second day's close", and said "today's close" for a close that
had not happened yet.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from src import notifier, trader_feed
from src.exits.exit_records import ExitRecords
from src.notifier.trade_body import _append_trade_session_body


def _pending(symbol: str, code: str = "REFUSAL_WALL_PENDING_CONFIRMATION", bar_date: str | None = "2026-10-12") -> dict:
    return {"symbol": symbol, "applied": False, "code": code, "prior_price": 368.74, "bar_date": bar_date}


def test_the_live_midday_message_drops_the_sold_stock():
    """The path that reaches the owner: midday/close go through the trader
    feed, which already reads the book. AAPL sold, MSFT held."""
    result = {
        "status": "reviewed",
        "target_revisions": [_pending("AAPL"), _pending("MSFT")],
        "_positions": [{"symbol": "MSFT", "qty": 5, "market_value": 2000.0}],
    }
    msg = trader_feed._format_position_review("midday", result, 10.0)
    assert "MSFT (still quoted" in msg
    assert "AAPL (still quoted" not in msg


def test_a_sold_stock_has_no_target_line():
    result = {"target_revisions": [_pending("AAPL"), _pending("MSFT")]}
    body = "\n".join(notifier.describe_target_revisions(result, held_symbols=["msft"]))
    assert "AAPL" not in body
    assert "MSFT" in body
    assert "1 position(s)" in body  # the count describes what is shown, not what was sold


def test_an_applied_revision_on_a_sold_stock_is_dropped_too():
    applied = {"symbol": "AAPL", "applied": True, "prior_price": 380.0, "new_price": 370.0, "trigger": ""}
    assert notifier.describe_target_revisions({"target_revisions": [applied]}, held_symbols=["MSFT"]) == []


def test_without_a_book_no_stock_is_named():
    """A caller that cannot say what is held gets one line saying so, never a
    target quoted for a stock that may have been sold."""
    body = "\n".join(notifier.describe_target_revisions({"target_revisions": [_pending("AAPL")]}, held_symbols=None))
    assert "AAPL" not in body
    assert "not shown" in body


def test_the_book_is_a_required_input():
    with pytest.raises(TypeError):
        notifier.describe_target_revisions({"target_revisions": [_pending("AAPL")]})  # type: ignore[call-arg]


def test_the_plain_session_message_uses_the_book_it_carries():
    result = {
        "status": "reviewed",
        "orders": [],
        "target_revisions": [_pending("AAPL"), _pending("MSFT")],
        "_positions": [{"symbol": "MSFT"}],
    }
    lines: list[str] = []
    _append_trade_session_body(lines, result)
    body = "\n".join(lines)
    assert "AAPL" not in body
    assert "MSFT" in body


@pytest.mark.parametrize(
    "code",
    ["REFUSAL_WALL_PENDING_CONFIRMATION", "REFUSAL_REACH_PENDING_CONFIRMATION", "REFUSAL_BREAK_PENDING_CONFIRMATION"],
)
def test_the_close_is_named_by_the_date_the_read_came_from(code: str):
    body = "\n".join(
        notifier.describe_target_revisions({"target_revisions": [_pending("MSFT", code)]}, held_symbols=["MSFT"])
    )
    assert "today's" not in body
    assert "Mon 12 Oct" in body


def test_a_row_without_its_date_says_latest_never_today():
    body = "\n".join(
        notifier.describe_target_revisions(
            {"target_revisions": [_pending("MSFT", bar_date=None)]}, held_symbols=["MSFT"]
        )
    )
    assert "today's" not in body
    assert "latest close" in body


def test_the_filed_revision_carries_its_close_date():
    records = ExitRecords(
        trade_executed_or_pending=MagicMock(), record_exit_refusal=MagicMock(), db=MagicMock(), market=MagicMock()
    )
    row = records._file_target_revision(
        run_id="r1",
        symbol="MSFT",
        seat="risk",
        evidence="e",
        code="REFUSAL_WALL_PENDING_CONFIRMATION",
        applied=False,
        bar_date="2026-10-12",
    )
    assert row["bar_date"] == "2026-10-12"
