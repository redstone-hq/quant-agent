"""Explicit date tables for the 2026 calendar.

Expected values come from the published NYSE holiday calendar and the Federal
Reserve H.15 release calendar (which follows the federal holidays, 5 U.S.C. 6103).
src/trading_calendar.py has no holiday table (weekday heuristic only, by design),
so the NYSE-holiday and early-close cases assert what the market does and are
xfail where the code does not know it.
"""

from datetime import date, datetime

import pytest

from src.data.fred_publication_days import federal_holidays, is_publication_day
from src.trading_calendar import ET, UTC, in_regular_session, is_weekday, to_et

# NYSE full-day closures 2026 (nyse.com/markets/hours-calendars).
NYSE_HOLIDAYS_2026 = {
    "New Year's Day": date(2026, 1, 1),
    "MLK Day": date(2026, 1, 19),
    "Washington's Birthday": date(2026, 2, 16),
    "Good Friday": date(2026, 4, 3),
    "Memorial Day": date(2026, 5, 25),
    "Juneteenth": date(2026, 6, 19),
    "Independence Day (observed, Sat Jul 4)": date(2026, 7, 3),
    "Labor Day": date(2026, 9, 7),
    "Thanksgiving": date(2026, 11, 26),
    "Christmas": date(2026, 12, 25),
}
GOOD_FRIDAY = date(2026, 4, 3)
FEDERAL_AND_NYSE = {k: v for k, v in NYSE_HOLIDAYS_2026.items() if v != GOOD_FRIDAY}


@pytest.mark.parametrize("name,day", FEDERAL_AND_NYSE.items())
def test_nyse_holiday_that_is_federal_is_not_a_publication_day(name, day):
    assert day in federal_holidays(2026)
    assert not is_publication_day(day)


def test_good_friday_is_nyse_closed_but_h15_publishes():
    assert is_publication_day(GOOD_FRIDAY)


@pytest.mark.xfail(reason="src/trading_calendar.py has no NYSE holiday table (weekday heuristic only)", strict=True)
@pytest.mark.parametrize("name,day", NYSE_HOLIDAYS_2026.items())
def test_regular_session_is_closed_on_nyse_holiday(name, day):
    assert not in_regular_session(datetime(day.year, day.month, day.day, 11, 0, tzinfo=ET))


def test_federal_holidays_2026_exact_set():
    assert federal_holidays(2026) == frozenset(
        {
            date(2026, 1, 1), date(2026, 1, 19), date(2026, 2, 16), date(2026, 5, 25),
            date(2026, 6, 19), date(2026, 7, 3), date(2026, 9, 7), date(2026, 10, 12),
            date(2026, 11, 11), date(2026, 11, 26), date(2026, 12, 25),
        }
    )  # fmt: skip


def test_saturday_holiday_observed_friday_sunday_observed_monday():
    # 2026-07-04 is a Saturday -> Friday Jul 3.
    assert date(2026, 7, 4).weekday() == 5
    assert date(2026, 7, 3) in federal_holidays(2026)
    assert date(2026, 7, 4) not in federal_holidays(2026)
    # 2027-07-04 is a Sunday -> Monday Jul 5.
    assert date(2027, 7, 4).weekday() == 6
    assert date(2027, 7, 5) in federal_holidays(2027)
    # 2027-12-25 is a Saturday -> Friday Dec 24.
    assert date(2027, 12, 24) in federal_holidays(2027)


def test_dst_spring_forward_2026_mar_8():
    assert datetime(2026, 3, 7, 12, 0, tzinfo=ET).utcoffset().total_seconds() == -5 * 3600
    assert datetime(2026, 3, 9, 12, 0, tzinfo=ET).utcoffset().total_seconds() == -4 * 3600
    # 09:30 ET open is 14:30 UTC in winter, 13:30 UTC in summer.
    assert datetime(2026, 3, 6, 14, 30, tzinfo=UTC).astimezone(ET).hour == 9
    assert datetime(2026, 3, 9, 13, 30, tzinfo=UTC).astimezone(ET).hour == 9


def test_dst_fall_back_2026_nov_1():
    assert datetime(2026, 10, 31, 12, 0, tzinfo=ET).utcoffset().total_seconds() == -4 * 3600
    assert datetime(2026, 11, 2, 12, 0, tzinfo=ET).utcoffset().total_seconds() == -5 * 3600
    assert datetime(2026, 11, 2, 14, 30, tzinfo=UTC).astimezone(ET).hour == 9
    assert to_et(datetime(2026, 11, 2, 14, 30)).hour == 9  # naive treated as UTC


@pytest.mark.parametrize("utc_hour,in_session", [(13, False), (15, True), (20, True), (21, False)])
def test_regular_session_follows_et_after_fall_back(utc_hour, in_session):
    # Mon 2026-11-02 (EST): open 14:30 UTC, close 21:00 UTC; probes at :45.
    assert in_regular_session(datetime(2026, 11, 2, utc_hour, 45, tzinfo=UTC)) is in_session


@pytest.mark.xfail(reason="src/trading_calendar.py has no NYSE early-close table (fixed 16:00 close)", strict=True)
def test_day_after_thanksgiving_closes_at_13_00():
    assert not in_regular_session(datetime(2026, 11, 27, 14, 0, tzinfo=ET))


def test_day_after_thanksgiving_is_open_morning_and_a_publication_day():
    assert is_weekday(date(2026, 11, 27))
    assert is_publication_day(date(2026, 11, 27))
    assert in_regular_session(datetime(2026, 11, 27, 11, 0, tzinfo=ET))


def test_columbus_day_2026_bond_closed_stock_open():
    columbus = date(2026, 10, 12)
    assert columbus.weekday() == 0
    assert columbus in federal_holidays(2026)
    assert not is_publication_day(columbus)  # H.15 not published
    assert is_weekday(columbus)  # NYSE trades
    assert in_regular_session(datetime(2026, 10, 12, 11, 0, tzinfo=ET))
    assert is_publication_day(date(2026, 10, 13))
