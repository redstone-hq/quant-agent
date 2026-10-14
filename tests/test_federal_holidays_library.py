"""The federal holiday set comes from pandas, not hand-written date arithmetic.

Measured defect this pins: when 1 January falls on a Saturday the observed
holiday is Friday 31 December of the PRIOR year. The hand-written list filed
it under the next year, so 31 December 2027 (also 2032, 2038) was treated as a
normal publication day.
"""

from __future__ import annotations

from datetime import date

import pytest
from pandas.tseries.holiday import USFederalHolidayCalendar

from src.data.fred_publication_days import federal_holidays, is_publication_day


@pytest.mark.parametrize("day", [date(2027, 12, 31), date(2032, 12, 31), date(2038, 12, 31)])
def test_prior_year_observed_new_year_is_not_a_publication_day(day):
    assert day.weekday() == 4  # a Friday, so only the holiday rule can exclude it
    assert is_publication_day(day) is False


@pytest.mark.parametrize("year", range(2021, 2041))
def test_matches_pandas_federal_calendar_exactly(year):
    expected = frozenset(d.date() for d in USFederalHolidayCalendar().holidays(f"{year}-01-01", f"{year}-12-31"))
    assert federal_holidays(year) == expected
