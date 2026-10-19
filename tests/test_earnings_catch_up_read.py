"""Catch-up read flag: trading-calendar previous session, seat evidence, owner line."""

from datetime import date, datetime, timezone
from unittest.mock import MagicMock, patch

from src.agents.base import AgentResult
from src.agents.earnings_analyst import EarningsAnalystAgent
from src.data.earnings import EarningsReport, is_catch_up_read
from src.notifier.snapshots import _append_earnings_body


class _Broker:
    """Calendar broker: weekdays trade except the listed holidays."""

    def __init__(self, holidays=()):
        self.holidays = {date.fromisoformat(h) for h in holidays}

    def _trades(self, d):
        return d.weekday() < 5 and d not in self.holidays

    def get_session_open(self, d):
        return datetime(d.year, d.month, d.day, 13, 30, tzinfo=timezone.utc) if self._trades(d) else None

    def get_session_close(self, d):
        return datetime(d.year, d.month, d.day, 20, 0, tzinfo=timezone.utc) if self._trades(d) else None


def test_filing_from_last_session_before_holiday_is_not_catch_up():
    broker = _Broker(holidays=["2026-11-26"])  # Thanksgiving, Thu
    # Wed 25 Nov is the last session before the holiday; read Fri 27 Nov it is not catch-up.
    assert not is_catch_up_read("2026-11-25", date(2026, 11, 27), broker)
    assert is_catch_up_read("2026-11-24", date(2026, 11, 27), broker)
    assert not is_catch_up_read("2026-11-27", date(2026, 11, 30), broker)


def test_unknown_calendar_reads_as_catch_up():
    assert is_catch_up_read("2026-11-25", date(2026, 11, 27), None)


def _report(tmp_path, is_new, catch_up):
    return EarningsReport(
        symbol="ZZZA",
        form_type="10-Q",
        filing_date="2026-09-08",
        filing_path=str(tmp_path / "f.html"),
        analysis_path=str(tmp_path / "ZZZA" / "analysis_10-Q_2026-09-08.md"),
        text_excerpt="Revenue was $10.0 billion.",
        is_new=is_new,
        catch_up_read=catch_up,
    )


def test_both_seat_paths_carry_filing_date_and_catch_up_flag(tmp_path):
    with patch("anthropic.Anthropic"):
        agent = EarningsAnalystAgent(api_key="k", model="m")
    agent._analyze_new = MagicMock(return_value=({"x": 1}, AgentResult(raw_text="", tokens_used=1, model="m")))
    agent._save_analysis = MagicMock()
    got = agent._analyze_one(_report(tmp_path, True, True))[0]
    assert got["catch_up_read"] is True and got["first_read_today"] is True
    assert got["filing_date"] == "2026-09-08"

    cached = _report(tmp_path, False, True)
    (tmp_path / "ZZZA").mkdir()
    (tmp_path / "ZZZA" / "analysis_10-Q_2026-09-08.md").write_text("x")
    agent._load_analysis = MagicMock(return_value={"x": 1})
    got = agent._analyze_one(cached)[0]
    assert got["catch_up_read"] is True and got["first_read_today"] is False
    assert got["filing_date"] == "2026-09-08"


def test_owner_line_marks_catch_up_read():
    lines: list[str] = []
    _append_earnings_body(
        lines,
        {
            "filings": [
                {
                    "symbol": "ZZZA",
                    "form_type": "10-Q",
                    "filing_date": "2026-09-08",
                    "outcome": "analyzed",
                    "catch_up_read": True,
                },
                {"symbol": "ZZZB", "form_type": "10-K", "filing_date": "2026-10-16", "outcome": "analyzed"},
            ]
        },
    )
    assert "ZZZA 10-Q filed 2026-09-08: read — catch-up read (filed Sep 8)" in lines
    assert "ZZZB 10-K filed 2026-10-16: read" in lines
