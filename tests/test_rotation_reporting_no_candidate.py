"""Room-available pre-check wording: no candidate vs unreadable cash figure."""

from __future__ import annotations

from src.rotation_parts.reporting import ROTATION_ROOM_AVAILABLE
from src.rotation_parts.reporting_lines import owner_precheck_lines


def _text(**over) -> str:
    record = {
        "outcome": ROTATION_ROOM_AVAILABLE,
        "headroom_pct": 5.0,
        "ceiling_pct": 8.0,
        "floor_pct": 1.0,
        "entry_budget_usd": None,
        "min_entry_usd": None,
        "new_symbol": None,
    }
    record.update(over)
    return " ".join(owner_precheck_lines(record))


def test_no_candidate_is_not_a_read_failure():
    text = _text()
    assert "could NOT read" not in text
    assert "no new idea needed room this session, so cash was not checked" in text


def test_candidate_with_unreadable_funding_keeps_old_wording():
    assert "could NOT read" in _text(new_symbol="AAPL")


def test_candidate_with_measured_funding_says_enough_cash():
    text = _text(new_symbol="AAPL", entry_budget_usd=900.0, min_entry_usd=500.0)
    assert "enough cash and borrowing room" in text
    assert "could NOT read" not in text
