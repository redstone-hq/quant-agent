"""Which calendar days FRED can actually publish a new print on.

WHY THIS EXISTS
---------------
`src/data/macro.py` derives, per series, the date by which a newer print is
due: `expected_next_by = last_observation + cadence + publication_lag`. That
arithmetic was plain calendar arithmetic, so the due date could land on a day
the publisher does not work, and the series was then judged against a date on
which nothing could ever have been published.

Measured false positive, from the production log
(`/home/qamc/quant-agent/quant_agent.log`, 2026-09-21 13:32:19 UTC):

    DFF: OVERDUE — latest reading is 2026-09-17; on this series' own cadence
    (1d between readings) and its own publication lag (1d) a newer print was
    due by 2026-09-19

2026-09-19 was a **Saturday**. The Federal Reserve Board's H.15 release, which
is where DFF comes from, publishes on business days only, so no print could
have landed on 2026-09-19 and none was missing. The desk nevertheless ran the
whole 2026-09-21 session on a macro seat that had downgraded itself — sixteen
separate prompt renderings that day carry "low confidence because DFF and
dollar-index inputs are overdue" (same log, 2026-09-21 13:34 UTC onward).

So the due date must be rolled forward off non-publication days before it is
compared with today. This module answers only that one question.

WHICH DAYS, AND ON WHAT AUTHORITY
---------------------------------
FRED is published by the Federal Reserve Bank of St. Louis, and the statistical
releases it redistributes come from federal agencies (Federal Reserve Board,
BLS, BEA, Census, Department of Labor). Those publishers observe:

* Saturdays and Sundays — no releases.
* The eleven federal holidays enumerated in 5 U.S.C. § 6103(a), with the
  weekend-observance rule of 5 U.S.C. § 6103(b) and Executive Order 11582:
  a holiday falling on a Saturday is observed on the preceding Friday, one
  falling on a Sunday is observed on the following Monday.

The holiday set is rule-based law, not a tuned parameter and not a table that
rots: pandas' `USFederalHolidayCalendar` computes it from the statute's own
description of each day, so this module needs no network, no data file and no
yearly maintenance.

WHAT THIS IS NOT
----------------
This is NOT the market calendar. `src/trading_calendar.py` deliberately keeps
exchange-holiday detection on the broker (`AlpacaBroker.is_trading_day()`)
because it needs a live connection, and the exchange calendar is not the same
as the federal one — Good Friday closes the NYSE and is not a federal holiday.
What matters for "has the statistical agency published yet?" is the federal
calendar, which is why this lives here rather than there.

This is also NOT the per-release schedule. FRED exposes real forward release
dates at `/fred/release/dates` (already used by `src/data/event_calendar.py`
for seven releases), which is strictly better information. It is not used here
because it costs an extra API call per series; rolling off non-publication days
is the part of that calendar that can be known for free and offline, and it is
the part the measured defect needed.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta

from pandas.tseries.holiday import USFederalHolidayCalendar

from src.trading_calendar import ET

__all__ = [
    "PUBLICATION_BOUNDARIES_ET",
    "crossed_a_publication_boundary",
    "federal_holidays",
    "is_publication_day",
    "next_publication_boundary",
    "roll_to_publication_day",
]


#: The times of day, US/Eastern, at which the releases behind the desk's
#: fifteen FRED series actually land. A cached copy of a series is only the
#: latest print that exists while NO boundary below has passed since it was
#: fetched — which is the whole cache-validity test, and is why it is a clock
#: question and not an age-in-hours question.
#:
#: Both are published schedules, not estimates:
#:
#: * 08:30 — the Bureau of Labor Statistics releases its principal federal
#:   economic indicators at 8:30 a.m. ET. That covers CPIAUCSL, CPILFESL and
#:   UNRATE here, and the Department of Labor's initial claims (ICSA) shares
#:   the slot. Sources: https://www.bls.gov/schedule/news_release/cpi.htm and
#:   https://www.bls.gov/schedule/news_release/empsit.htm (both read
#:   2026-09-23; both list 08:30 AM).
#: * 16:15 — the Federal Reserve Board's H.15 Selected Interest Rates is
#:   "posted daily Monday through Friday at 4:15pm", which is where DFF,
#:   DGS3MO, DGS2, DGS10, DFII10 and T10YIE come from. Source:
#:   https://www.federalreserve.gov/releases/h15/ (read 2026-09-23).
#:
#: This list is deliberately the release clock and NOT a safety margin. If a
#: release moves, the fix is to correct the entry against its publisher's
#: schedule, not to pad it.
PUBLICATION_BOUNDARIES_ET: tuple[time, ...] = (
    time(hour=8, minute=30),
    time(hour=16, minute=15),
)


def federal_holidays(year: int) -> frozenset[date]:
    """The observed US federal holidays in `year`, from pandas' federal calendar.

    Uses `pandas.tseries.holiday.USFederalHolidayCalendar`, which files a
    weekend-observed holiday under the date it is observed (1 Jan on a
    Saturday is Friday 31 Dec of the prior year, and is returned for that year).
    """
    return frozenset(d.date() for d in USFederalHolidayCalendar().holidays(f"{year}-01-01", f"{year}-12-31"))


def is_publication_day(day: date) -> bool:
    """True when a federal statistical agency could publish on `day`.

    Weekdays that are not observed federal holidays. This is a necessary
    condition, not a sufficient one — a series with a monthly schedule
    publishes on one of these days, not on all of them.
    """
    if day.weekday() >= 5:
        return False
    return day not in federal_holidays(day.year)


def roll_to_publication_day(day: date) -> date:
    """The first day on or after `day` on which a print could be published.

    A due date that lands on a weekend or a federal holiday is not a date by
    which anything was owed: nothing publishes then. Rolling FORWARD is the
    only safe direction — it can only delay the moment a series is called
    overdue, never bring it forward, so this can turn a false alarm into
    silence but can never turn a real late release into a false all-clear
    beyond the length of the closure itself.

    Bounded by construction: the longest run of consecutive non-publication
    days the federal calendar can produce is a holiday-adjacent weekend, so
    this loop terminates within a handful of iterations. The cap below is a
    guard against a caller passing a corrupt date, not a tuning knob.
    """
    candidate = day
    for _ in range(len(federal_holidays(day.year)) + 7):
        if is_publication_day(candidate):
            return candidate
        candidate += timedelta(days=1)
    return candidate


def _boundary_instants(day: date) -> list[datetime]:
    """Every publication instant on `day`, empty on a non-publication day."""
    if not is_publication_day(day):
        return []
    return [datetime.combine(day, clock, tzinfo=ET) for clock in PUBLICATION_BOUNDARIES_ET]


def next_publication_boundary(after: datetime) -> datetime:
    """The first publication instant strictly after `after` (ET-aware).

    Used to say, in the operator's terms, how long a freshly written cache
    entry will remain the latest print that exists.
    """
    moment = after.astimezone(ET)
    day = moment.date()
    for _ in range(len(federal_holidays(day.year)) + 7):
        for instant in _boundary_instants(day):
            if instant > moment:
                return instant
        day += timedelta(days=1)
    return moment


def crossed_a_publication_boundary(fetched_at: datetime, now: datetime) -> bool:
    """True when a release could have landed between `fetched_at` and `now`.

    This is the cache-validity question, asked the only way it can be asked
    honestly. An age in hours cannot answer it — four hours spanning 08:30
    invalidates a cached CPI while fourteen hours spanning nothing at all
    invalidate nothing.

    Both arguments are converted to US/Eastern first, so a host in another
    zone gets the same answer as a host in New York.
    """
    start = fetched_at.astimezone(ET)
    end = now.astimezone(ET)
    if end <= start:
        return False
    day = start.date()
    while day <= end.date():
        for instant in _boundary_instants(day):
            if start < instant <= end:
                return True
        day += timedelta(days=1)
    return False
