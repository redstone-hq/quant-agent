"""The session calendar must reproduce today's day exactly, bend on a short
session, say so on a holiday, and refuse loudly when it cannot read."""

from __future__ import annotations

import subprocess
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from src.session_calendar import (
    PHASE_OFFSETS,
    Session,
    SessionCalendarUnavailable,
    covered_phases,
    et_datetime,
    phase_window,
    session_for,
)
from src.trading_calendar import ET, SESSION_WINDOWS

NORMAL_DAY = date(2026, 10, 5)  # an ordinary Monday
EARLY_CLOSE_DAY = date(2026, 11, 27)  # the day after US Thanksgiving
HOLIDAY = date(2026, 11, 26)


class FakeCalendarBroker:
    """Stands in for the broker's calendar reads, with the broker's own
    failure posture: unknown dates come back as None, exactly as a holiday
    does, which is the ambiguity the module has to resolve."""

    def __init__(self, sessions: dict[date, tuple[int, int]]):
        self._sessions = sessions
        self.calls: list[date] = []

    def _edge(self, on_date: date, which: int):
        self.calls.append(on_date)
        window = self._sessions.get(on_date)
        if window is None:
            return None
        return et_datetime(on_date, window[which])

    def get_session_open(self, on_date=None):
        return self._edge(on_date, 0)

    def get_session_close(self, on_date=None):
        return self._edge(on_date, 1)


def _normal_sessions(around: date, days: int = 40) -> dict[date, tuple[int, int]]:
    """Every weekday in a window around `around` is a plain 09:30-16:00 day."""
    out: dict[date, tuple[int, int]] = {}
    for delta in range(-days, days + 1):
        day = around + timedelta(days=delta)
        if day.weekday() < 5:
            out[day] = (570, 960)
    return out


def test_derived_normal_day_table_is_todays_exact_minutes():
    """The offsets now DERIVE the fixed table, so the old equality check would
    be a tautology. Pin the six literal pairs instead: this is the whole claim
    that re-expressing the day is a no-op on a normal 09:30-16:00 session."""
    assert dict(SESSION_WINDOWS) == {
        "earnings_preprocess": (480, 555),
        "morning": (570, 720),
        "intra_check": (570, 960),
        "midday": (780, 870),
        "close": (930, 960),
        "evening": (1200, 1320),
    }
    assert covered_phases() == frozenset(SESSION_WINDOWS)


def test_normal_day_resolves_to_the_same_wall_clock():
    broker = FakeCalendarBroker(_normal_sessions(NORMAL_DAY))
    for phase, (start_min, end_min) in SESSION_WINDOWS.items():
        start, end = phase_window(broker, NORMAL_DAY, phase)
        assert start == et_datetime(NORMAL_DAY, start_min), phase
        assert end == et_datetime(NORMAL_DAY, end_min), phase


def test_early_close_pulls_the_end_of_day_review_before_the_close():
    """The defect in one assertion: on a 13:00 close the end-of-day review
    must finish before the bell, not an hour and a half after it."""
    sessions = _normal_sessions(EARLY_CLOSE_DAY)
    sessions[EARLY_CLOSE_DAY] = (570, 780)  # 09:30 - 13:00 ET
    broker = FakeCalendarBroker(sessions)

    close_at = et_datetime(EARLY_CLOSE_DAY, 780)
    for phase in ("midday", "close", "intra_check"):
        start, end = phase_window(broker, EARLY_CLOSE_DAY, phase)
        assert end <= close_at, f"{phase} runs past the real close"
        assert start < close_at, f"{phase} starts after the market has shut"

    # And specifically: the act-on-trigger close review lands in the last half
    # hour of the SHORT session, 12:30-13:00, not at the hardcoded 15:30.
    assert phase_window(broker, EARLY_CLOSE_DAY, "close") == (
        et_datetime(EARLY_CLOSE_DAY, 750),
        close_at,
    )
    # Under today's fixed table this phase would have been 15:30-16:00, i.e.
    # entirely after the close — the regression this test pins down.
    assert SESSION_WINDOWS["close"][0] > 780


def test_holiday_is_not_a_trading_day():
    sessions = _normal_sessions(HOLIDAY)
    del sessions[HOLIDAY]
    broker = FakeCalendarBroker(sessions)

    session = session_for(broker, HOLIDAY)
    assert session.is_trading_day is False
    assert session.open_at is None and session.close_at is None
    with pytest.raises(SessionCalendarUnavailable):
        phase_window(broker, HOLIDAY, "close")


def test_weekend_is_not_a_trading_day():
    saturday = date(2026, 10, 10)
    assert saturday.weekday() == 5
    broker = FakeCalendarBroker(_normal_sessions(saturday))
    assert session_for(broker, saturday).is_trading_day is False


def test_unreadable_calendar_raises_instead_of_assuming_a_normal_session():
    """A broker whose calendar answers nothing anywhere is an outage, and an
    outage must never be served as a holiday or as 09:30-16:00."""
    broker = FakeCalendarBroker({})
    with pytest.raises(SessionCalendarUnavailable) as exc:
        session_for(broker, NORMAL_DAY)
    assert "unreadable" in str(exc.value)
    with pytest.raises(SessionCalendarUnavailable):
        phase_window(broker, NORMAL_DAY, "close")


def test_half_an_answer_is_refused():
    class OneEdge(FakeCalendarBroker):
        def get_session_close(self, on_date=None):
            return None

    broker = OneEdge(_normal_sessions(NORMAL_DAY))
    with pytest.raises(SessionCalendarUnavailable) as exc:
        session_for(broker, NORMAL_DAY)
    assert "only one edge" in str(exc.value)


def test_missing_broker_raises_rather_than_defaulting():
    with pytest.raises(SessionCalendarUnavailable):
        session_for(None, NORMAL_DAY)


def test_no_date_is_hardcoded_in_the_module():
    """A holiday table written into the source is the same bug in a new
    costume, so the module must contain no literal calendar date."""
    import re
    from pathlib import Path

    import src.session_calendar as module

    source = Path(module.__file__).read_text()
    code = "\n".join(line for line in source.splitlines() if not line.strip().startswith("#"))
    code = re.sub(r'"""(?:.|\n)*?"""', "", code)
    assert not re.search(r"\b(19|20)\d{2}\s*,\s*\d{1,2}\s*,\s*\d{1,2}\b", code)
    assert not re.search(r"\bdate\(\s*\d", code)


def test_phase_offsets_are_all_anchored_to_a_real_edge():
    for phase, offset in PHASE_OFFSETS.items():
        assert offset.start_anchor in ("open", "close"), phase
        assert offset.end_anchor in ("open", "close"), phase
        start, end = phase_window(FakeCalendarBroker(_normal_sessions(NORMAL_DAY)), NORMAL_DAY, phase)
        assert start < end, phase


def test_session_require_edges_refuses_a_closed_day():
    closed = Session(on_date=HOLIDAY, is_trading_day=False, open_at=None, close_at=None)
    with pytest.raises(SessionCalendarUnavailable):
        closed.require_edges()


def test_resolved_edges_are_eastern_aware():
    broker = FakeCalendarBroker(_normal_sessions(NORMAL_DAY))
    opens, closes = session_for(broker, NORMAL_DAY).require_edges()
    for moment in (opens, closes):
        assert isinstance(moment, datetime)
        assert moment.tzinfo is ET


# --- the command the shell wrapper calls once per tick ----------------------

ROOT = Path(__file__).resolve().parent.parent


def _early_close_broker() -> FakeCalendarBroker:
    sessions = _normal_sessions(EARLY_CLOSE_DAY)
    sessions[EARLY_CLOSE_DAY] = (570, 780)
    del sessions[HOLIDAY]
    return FakeCalendarBroker(sessions)


def _cli(capsys, argv, broker, cache_dir=None):
    from src.session_calendar import main

    args = list(argv) + (["--cache-dir", str(cache_dir)] if cache_dir is not None else [])
    code = main(args, broker_factory=lambda: broker)
    captured = capsys.readouterr()
    return code, captured.out.strip(), captured.err.strip()


def test_cli_prints_the_27_nov_windows_from_the_stubbed_calendar(capsys):
    broker = _early_close_broker()
    assert _cli(capsys, ["midday", "2026-11-27"], broker)[:2] == (0, "600 690")  # 10:00-11:30
    assert _cli(capsys, ["close", "2026-11-27"], broker)[:2] == (0, "750 780")  # 12:30-13:00
    assert _cli(capsys, ["intra_check", "2026-11-27"], broker)[:2] == (0, "570 780")
    assert _cli(capsys, ["evening", "2026-11-27"], broker)[:2] == (0, "1020 1140")  # 17:00-19:00


def test_cli_intra_safety_has_intra_checks_window(capsys):
    broker = _early_close_broker()
    assert _cli(capsys, ["intra_safety", "2026-11-27"], broker) == _cli(capsys, ["intra_check", "2026-11-27"], broker)


def test_cli_prints_closed_on_a_holiday(capsys):
    code, out, _ = _cli(capsys, ["midday", "2026-11-26"], _early_close_broker())
    assert (code, out) == (0, "closed")


def test_cli_cache_hit_does_not_call_the_broker(capsys, tmp_path):
    (tmp_path / "session-2026-11-27").write_text("570 780\n")

    def broker_must_not_be_built():
        raise AssertionError("a cache hit must not build or ask the broker")

    from src.session_calendar import main

    assert main(["close", "2026-11-27", "--cache-dir", str(tmp_path)], broker_factory=broker_must_not_be_built) == 0
    assert capsys.readouterr().out.strip() == "750 780"


def test_cli_first_tick_asks_the_broker_once_and_later_ticks_use_the_file(capsys, tmp_path):
    broker = _early_close_broker()
    assert _cli(capsys, ["midday", "2026-11-27"], broker, tmp_path)[:2] == (0, "600 690")
    assert (tmp_path / "session-2026-11-27").read_text().strip() == "570 780"
    asked = len(broker.calls)
    assert asked > 0
    assert _cli(capsys, ["close", "2026-11-27"], broker, tmp_path)[:2] == (0, "750 780")
    assert len(broker.calls) == asked


def test_cli_caches_a_holiday_too(capsys, tmp_path):
    assert _cli(capsys, ["morning", "2026-11-26"], _early_close_broker(), tmp_path)[:2] == (0, "closed")
    assert (tmp_path / "session-2026-11-26").read_text().strip() == "closed"


def test_cli_calendar_unavailable_gives_exit_3_and_the_normal_day_window(capsys, tmp_path):
    code, out, err = _cli(capsys, ["midday", "2026-11-27"], FakeCalendarBroker({}), tmp_path)
    assert code == 3
    assert out == "780 870"  # the derived normal-day window, not a guess typed elsewhere
    assert err, "the reason must be said, not swallowed"
    assert not list(tmp_path.glob("session-*")), "an outage must never be cached as an answer"


def test_cli_broker_that_cannot_be_built_is_also_unavailable(capsys):
    from src.session_calendar import main

    def broken():
        raise RuntimeError("no credentials")

    assert main(["close", "2026-11-27"], broker_factory=broken) == 3
    assert capsys.readouterr().out.strip() == "930 960"


def test_cli_normal_day_flag_prints_the_derived_window_without_a_broker(capsys):
    from src.session_calendar import main

    assert main(["evening", "2026-11-27", "--normal-day"], broker_factory=None) == 0
    assert capsys.readouterr().out.strip() == "1200 1320"


def test_cli_unknown_mode_is_a_usage_error(capsys):
    code, _, err = _cli(capsys, ["brunch", "2026-11-27"], _early_close_broker())
    assert code == 2 and "unknown mode" in err


def test_module_runs_as_a_command_and_reads_the_day_cache(tmp_path):
    (tmp_path / "session-2026-11-27").write_text("570 780\n")
    proc = subprocess.run(
        [sys.executable, "-m", "src.session_calendar", "midday", "2026-11-27", "--cache-dir", str(tmp_path)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert (proc.returncode, proc.stdout.strip()) == (0, "600 690"), proc.stderr
