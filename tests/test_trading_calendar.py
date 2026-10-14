"""Tests for src.trading_calendar — the single source of truth for trading-day
semantics. Also asserts the bash wrapper holds no copy of the window minutes.
"""

import re
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from src.trading_calendar import (
    ET,
    SESSION_WINDOWS,
    UTC,
    et_now,
    et_today,
    format_window,
    in_session_window,
    is_weekday,
    quarter_label,
    quarter_of,
    session_date_key,
    to_et,
)


def test_et_now_is_timezone_aware_and_in_ny():
    now = et_now()
    assert now.tzinfo is not None
    assert now.tzinfo.key == "America/New_York"


def test_et_today_matches_current_et_date():
    # Cheap smoke — same call twice won't straddle midnight in any plausible test env
    assert et_today() == et_now().date()


def test_to_et_treats_naive_as_utc():
    naive = datetime(2026, 4, 17, 21, 0, 0)  # 21:00 UTC == 17:00 EDT
    result = to_et(naive)
    assert result.tzinfo.key == "America/New_York"
    assert result.hour == 17
    assert result.date() == date(2026, 4, 17)


def test_to_et_preserves_aware_datetimes():
    utc_aware = datetime(2026, 4, 17, 21, 0, 0, tzinfo=UTC)
    result = to_et(utc_aware)
    assert result.hour == 17


def test_session_date_key_is_et_iso_string():
    # 2026-04-18 01:30 UTC is still 2026-04-17 in ET → key must be "2026-04-17"
    boundary = datetime(2026, 4, 18, 1, 30, 0, tzinfo=UTC)
    assert session_date_key(boundary) == "2026-04-17"


def test_session_date_key_accepts_none_for_now():
    key = session_date_key()
    assert re.match(r"^\d{4}-\d{2}-\d{2}$", key)


def test_is_weekday_weekday_vs_weekend():
    assert is_weekday(date(2026, 4, 17)) is True  # Friday
    assert is_weekday(date(2026, 4, 18)) is False  # Saturday
    assert is_weekday(date(2026, 4, 19)) is False  # Sunday
    assert is_weekday(date(2026, 4, 20)) is True  # Monday


@pytest.mark.parametrize(
    "mode, lo_min, hi_min",
    [
        ("earnings_preprocess", 480, 555),
        ("morning", 570, 720),
        ("intra_check", 570, 960),
        ("midday", 780, 870),
        ("close", 930, 960),
        ("evening", 1200, 1320),
    ],
)
def test_session_windows_cover_documented_ranges(mode, lo_min, hi_min):
    assert SESSION_WINDOWS[mode] == (lo_min, hi_min)


def test_every_session_window_is_at_least_launchd_tick_wide():
    """Regression: 2026-04-23/24 close session (then 25min wide) silently
    missed two days because launchd StartInterval=1800s (30min) ticks
    could land on both sides of the narrow window depending on phase
    (e.g. 15:28 + 15:58 → never inside 15:30-15:55).

    Rule: every window MUST be ≥ 30 minutes so at least one tick always
    lands inside, regardless of when launchd loaded the plist.
    """
    LAUNCHD_TICK_MIN = 30
    for mode, (lo, hi) in SESSION_WINDOWS.items():
        width = hi - lo
        assert width >= LAUNCHD_TICK_MIN, (
            f"{mode} window is {width}min wide (<{LAUNCHD_TICK_MIN}min); "
            f"launchd 30-min ticks can miss it entirely on unlucky phase"
        )


def _et_dt(y, m, d, hh, mm) -> datetime:
    return datetime(y, m, d, hh, mm, tzinfo=ET)


def test_in_session_window_morning_bounds_inclusive():
    # 2026-04-17 is a Friday
    assert in_session_window("morning", _et_dt(2026, 4, 17, 9, 30)) is True
    assert in_session_window("morning", _et_dt(2026, 4, 17, 12, 0)) is True
    # Just outside
    assert in_session_window("morning", _et_dt(2026, 4, 17, 9, 29)) is False
    assert in_session_window("morning", _et_dt(2026, 4, 17, 12, 1)) is False


def test_in_session_window_blocks_weekend():
    # Saturday 2026-04-18 10:00 — inside morning minutes but not a weekday
    assert in_session_window("morning", _et_dt(2026, 4, 18, 10, 0)) is False


def test_in_session_window_unknown_mode_errors():
    with pytest.raises(ValueError):
        in_session_window("unknown", _et_dt(2026, 4, 17, 10, 0))


def test_in_session_window_takes_tz_aware_input():
    # Caller may pass UTC — must convert internally
    utc_1330 = datetime(2026, 4, 17, 13, 30, tzinfo=UTC)  # 09:30 EDT
    assert in_session_window("morning", utc_1330) is True


def test_format_window_human_readable():
    assert format_window("morning") == "09:30-12:00 ET"
    assert format_window("evening") == "20:00-22:00 ET"


def test_bash_wrapper_holds_no_minute_literals():
    """The wrapper used to carry its own LO/HI table, pinned to SESSION_WINDOWS
    by a test that made two copies agree. The rule now removes the second copy:
    the wrapper asks `python -m src.session_calendar` for the day's window, so
    drift cannot happen, and this test fails if anyone types a minute back in.
    """
    wrapper = Path(__file__).resolve().parent.parent / "scripts" / "run_if_et_window.sh"
    text = wrapper.read_text()

    literal = re.compile(r"\b(?:LO|HI)=[\"']?\d")
    assert not [ln for ln in text.splitlines() if literal.search(ln)], "the wrapper holds a LO=/HI= minute literal"
    assert not re.search(r"^\s*\w+\)\s+LO=", text, re.MULTILINE), "the wrapper has a per-mode LO/HI case table"
    assert "src.session_calendar" in text, "the wrapper must ask the session calendar for the window"


def test_phase_offsets_live_here_and_derive_the_normal_day_table():
    """One copy of the schedule: the offsets are defined in this module and the
    fixed normal-day table is derived from them at the regular 09:30/16:00 edges."""
    from src import session_calendar
    from src import trading_calendar as tc

    assert session_calendar.PHASE_OFFSETS is tc.PHASE_OFFSETS
    assert set(tc.PHASE_OFFSETS) == set(tc.SESSION_WINDOWS)
    for name, offset in tc.PHASE_OFFSETS.items():
        derived = tc.window_minutes(offset, tc.REGULAR_SESSION_OPEN_MIN, tc.REGULAR_SESSION_CLOSE_MIN)
        assert tc.SESSION_WINDOWS[name] == derived, name


def test_quarter_of_maps_months_correctly():
    assert quarter_of(date(2026, 1, 15)) == 1
    assert quarter_of(date(2026, 3, 31)) == 1
    assert quarter_of(date(2026, 4, 1)) == 2
    assert quarter_of(date(2026, 6, 30)) == 2
    assert quarter_of(date(2026, 7, 1)) == 3
    assert quarter_of(date(2026, 9, 30)) == 3
    assert quarter_of(date(2026, 10, 1)) == 4
    assert quarter_of(date(2026, 12, 31)) == 4


def test_quarter_label_format():
    assert quarter_label(date(2026, 3, 15)) == "2026-Q1"
    assert quarter_label(date(2026, 12, 31)) == "2026-Q4"


def test_util_time_shim_still_re_exports():
    """Existing `from src.util.time import ...` sites must keep working."""
    from src.util.time import ET as shim_ET
    from src.util.time import UTC as shim_UTC
    from src.util.time import et_now as shim_et_now
    from src.util.time import et_today as shim_et_today
    from src.util.time import to_et as shim_to_et

    assert shim_ET is ET
    assert shim_UTC is UTC
    assert shim_et_now() is not None
    assert shim_et_today() is not None
    # naive UTC input round-trips through both paths
    dt = datetime(2026, 4, 17, 21, 0)
    assert shim_to_et(dt) == to_et(dt)
