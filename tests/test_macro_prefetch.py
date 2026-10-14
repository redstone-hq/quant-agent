"""Board item 119 — the FRED fetch moves off the trading path, and the
release-due-date is computed against the publication calendar.

Two defects are covered here.

DEFECT 1 — the fetch starves whichever session runs it.
Measured in `/home/qamc/quant-agent/quant_agent.log`: the morning macro stage
starts 09:30:49 ET, 49 seconds after the bell, and on 2026-09-22 it consumed
its whole 90-second ceiling and skipped 8 of 15 series without an attempt. Of
the seven runs that exhausted that ceiling, three were midday, three were
evening and one was the morning — so this is not an open-only problem.

DEFECT 2 — the due date was plain calendar arithmetic.
`expected_next_by = last_obs + cadence + lag` could land on a Saturday, and the
series was then judged against a date on which nothing could be published.
Production instance, 2026-09-21 13:32:19 UTC: DFF flagged OVERDUE against a due
date of 2026-09-19, a Saturday. `test_a_due_date_landing_on_a_weekend_is_not_
overdue_on_monday` reproduces that exact case and FAILS without the fix.
"""

from datetime import date, datetime, timedelta
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from src.data.fred_publication_days import (
    crossed_a_publication_boundary,
    federal_holidays,
    is_publication_day,
    roll_to_publication_day,
)
from src.data.macro import CONFIGURED_SERIES, FRESHNESS_CURRENT, MacroDataProvider
from src.data.macro_series_cache import MacroSeriesCache
from src.trading_calendar import ET


# --------------------------------------------------------------------------
# DEFECT 2 — the publication calendar
# --------------------------------------------------------------------------


def _provider_with(monkeypatch, tmp_path, observations, info, today):
    """A provider whose FRED returns `observations`/`info` and whose clock is
    pinned to `today`."""
    provider = MacroDataProvider(
        api_key="test-key",
        series_cache=MacroSeriesCache(str(tmp_path / "cache")),
    )
    provider.fred = MagicMock()
    provider.fred.get_series.return_value = observations
    provider.fred.get_series_info.return_value = info
    monkeypatch.setattr("src.data.macro.et_today", lambda: today)
    return provider


def test_a_due_date_landing_on_a_weekend_is_not_overdue_on_monday(monkeypatch, tmp_path):
    """The production false positive, reproduced exactly.

    DFF's latest reading was 2026-09-17, its derived cadence 1 day and its
    derived publication lag 1 day, so the bare calendar sum is 2026-09-19 — a
    SATURDAY. FRED's H.15 publishes on business days, so nothing was missing,
    yet on Monday 2026-09-21 the desk logged DFF OVERDUE and the macro seat ran
    the whole session at low confidence citing it.

    Without the roll-forward this asserts `current` and gets `overdue`.
    """
    index = pd.DatetimeIndex([date(2026, 9, 15), date(2026, 9, 16), date(2026, 9, 17)])
    observations = pd.Series([3.88, 3.88, 3.88], index=index, dtype=float)
    info = pd.Series({"observation_end": "2026-09-17", "last_updated": "2026-09-18"})

    provider = _provider_with(
        monkeypatch,
        tmp_path,
        observations,
        info,
        today=date(2026, 9, 21),
    )
    freshness = provider._safe_get_series("DFF") is not None and provider._run_freshness["DFF"]

    assert freshness.expected_next_by == date(2026, 9, 21), (
        "the Saturday due date must roll to the next publication day (Monday)"
    )
    assert freshness.status == FRESHNESS_CURRENT, (
        "a print due on a Saturday cannot be overdue on the following Monday — nothing publishes at a weekend"
    )


def test_a_genuinely_late_print_is_still_reported_overdue(monkeypatch, tmp_path):
    """The roll must not become a blanket amnesty.

    Same series, same weekend-landing due date, but now it is the following
    WEDNESDAY. The Monday publication day has come and gone with no newer
    print, so this is a real failure and must still be flagged.
    """
    index = pd.DatetimeIndex([date(2026, 9, 15), date(2026, 9, 16), date(2026, 9, 17)])
    observations = pd.Series([3.88, 3.88, 3.88], index=index, dtype=float)
    info = pd.Series({"observation_end": "2026-09-17", "last_updated": "2026-09-18"})

    provider = _provider_with(
        monkeypatch,
        tmp_path,
        observations,
        info,
        today=date(2026, 9, 23),
    )
    provider._safe_get_series("DFF")
    assert provider._run_freshness["DFF"].status == "overdue"


def test_a_due_date_on_a_federal_holiday_rolls_past_it():
    """Independence Day 2026 falls on a Saturday, so it is observed on Friday
    2026-07-03 (5 U.S.C. 6103(b)). A print due that Friday is due the following
    Monday in practice."""
    assert date(2026, 7, 3) in federal_holidays(2026)
    assert roll_to_publication_day(date(2026, 7, 3)) == date(2026, 7, 6)


def test_good_friday_is_not_treated_as_a_closure():
    """The NYSE shuts on Good Friday; the federal government does not, and BLS
    publishes that morning. Using the exchange calendar here would invent a
    false negative, so this asserts the federal calendar is the one in use."""
    good_friday_2026 = date(2026, 4, 3)
    assert good_friday_2026 not in federal_holidays(2026)
    assert is_publication_day(good_friday_2026)


def test_the_roll_only_ever_moves_a_due_date_later():
    """One-directional by construction: the roll can silence a false alarm but
    can never bring a real one forward."""
    day = date(2026, 1, 1)
    for _ in range(400):
        assert roll_to_publication_day(day) >= day
        day += timedelta(days=1)


# --------------------------------------------------------------------------
# The cache-validity test — the release clock, not an age
# --------------------------------------------------------------------------


def _entry(fetched_at, expected_next_by=None):
    return {
        "fetched_at": fetched_at.isoformat(),
        "expected_next_by": (expected_next_by.isoformat() if expected_next_by is not None else None),
    }


def test_the_0845_cache_serves_the_morning_and_midday_reads():
    """No publication boundary sits between 08:45 and either read."""
    fetched = datetime(2026, 9, 22, 8, 45, tzinfo=ET)
    for read_at in (
        datetime(2026, 9, 22, 9, 30, 49, tzinfo=ET),  # measured morning read
        datetime(2026, 9, 22, 13, 0, tzinfo=ET),  # midday session
    ):
        assert MacroSeriesCache.is_usable(_entry(fetched), read_at)


def test_the_0845_cache_is_refused_after_the_1615_h15_post():
    """The evening session at 20:00 ET must NOT be served the morning copy.

    This is the daily regression an adversarial review caught: DFF, DGS10 and
    the other H.15 series reprint at 16:15 ET, and today's evening session
    reads that print. A date-granular validity test would have served the
    08:45 snapshot instead.
    """
    fetched = datetime(2026, 9, 22, 8, 45, tzinfo=ET)
    evening = datetime(2026, 9, 22, 20, 0, tzinfo=ET)
    assert not MacroSeriesCache.is_usable(_entry(fetched), evening)


def test_a_cache_written_before_0830_is_refused_on_a_cpi_morning():
    """The other half of the same finding.

    On a CPI morning the series' own `expected_next_by` equals today, so the
    obvious "today <= expected_next_by" test APPROVES the cache on the one day
    a month the print moves the book. The boundary test refuses it.
    """
    fetched = datetime(2026, 9, 22, 7, 0, tzinfo=ET)
    morning = datetime(2026, 9, 22, 9, 30, tzinfo=ET)
    entry = _entry(fetched, expected_next_by=date(2026, 9, 22))
    assert date(2026, 9, 22) <= date(2026, 9, 22), "the naive test would pass"
    assert not MacroSeriesCache.is_usable(entry, morning)


def test_the_1830_cache_serves_the_evening_read():
    fetched = datetime(2026, 9, 22, 18, 30, tzinfo=ET)
    evening = datetime(2026, 9, 22, 20, 0, tzinfo=ET)
    assert MacroSeriesCache.is_usable(_entry(fetched), evening)


def test_an_entry_whose_own_due_date_has_passed_is_never_served():
    """Second, weaker condition: if the desk already knows a newer print is
    owed, it asks for it rather than serving what it holds."""
    fetched = datetime(2026, 9, 22, 8, 45, tzinfo=ET)
    read_at = datetime(2026, 9, 22, 9, 30, tzinfo=ET)
    entry = _entry(fetched, expected_next_by=date(2026, 9, 21))
    assert not MacroSeriesCache.is_usable(entry, read_at)


def test_an_entry_with_no_readable_timestamp_is_a_miss():
    read_at = datetime(2026, 9, 22, 9, 30, tzinfo=ET)
    assert not MacroSeriesCache.is_usable({"fetched_at": "not-a-date"}, read_at)
    assert not MacroSeriesCache.is_usable({}, read_at)
    # A naive timestamp cannot be placed on the ET clock, so it is refused
    # rather than guessed at.
    assert not MacroSeriesCache.is_usable(
        {"fetched_at": datetime(2026, 9, 22, 8, 45).isoformat()},
        read_at,
    )


def test_a_weekend_gap_crosses_no_boundary():
    """Friday evening to Monday 08:00: nothing publishes in between, so a
    Friday 18:30 entry is still the latest print that exists."""
    friday = datetime(2026, 9, 18, 18, 30, tzinfo=ET)
    monday = datetime(2026, 9, 21, 8, 0, tzinfo=ET)
    assert not crossed_a_publication_boundary(friday, monday)
    # ...and crossing Monday's 08:30 does invalidate it.
    assert crossed_a_publication_boundary(friday, datetime(2026, 9, 21, 9, 0, tzinfo=ET))


# --------------------------------------------------------------------------
# DEFECT 1 — the fetch leaves the trading path
# --------------------------------------------------------------------------


def _stub_fred(provider):
    index = pd.DatetimeIndex([date(2026, 9, 21), date(2026, 9, 22)])
    provider.fred = MagicMock()
    provider.fred.get_series.return_value = pd.Series(
        [1.0, 1.0],
        index=index,
        dtype=float,
    )
    provider.fred.get_series_info.return_value = pd.Series(
        {"observation_end": "2026-09-22", "last_updated": "2026-09-22"}
    )
    return provider


def test_the_configured_series_tuple_matches_what_a_full_fetch_asks_for(tmp_path):
    """Mechanical, not prose: `prefetch_deadline_s` sizes itself off
    CONFIGURED_SERIES, so a sixteenth series added to a fetcher without being
    listed there would silently under-budget the prefetch. This fails instead."""
    provider = _stub_fred(
        MacroDataProvider(
            api_key="test-key",
            series_cache=MacroSeriesCache(str(tmp_path / "cache")),
        )
    )
    provider.get_macro_summary()
    asked = {call.args[0] for call in provider.fred.get_series.call_args_list}
    assert asked == set(CONFIGURED_SERIES)


def test_a_session_never_writes_the_cache(tmp_path):
    """Only the prefetch writes. A session that half-failed must not be able to
    turn its own partial fetch into the next session's 'cached' answer."""
    cache_dir = tmp_path / "cache"
    provider = _stub_fred(
        MacroDataProvider(
            api_key="test-key",
            series_cache=MacroSeriesCache(str(cache_dir)),
        )
    )
    provider.get_macro_summary()
    assert not cache_dir.exists() or not list(cache_dir.glob("*.json"))


def test_the_prefetch_fills_the_cache_and_the_next_session_makes_no_http_call(tmp_path):
    """The whole point, end to end.

    After a prefetch, a session's `get_macro_summary()` must reach full
    coverage having issued ZERO requests — which is what removes the measured
    'deadline exceeded, 8 of 15 skipped without an attempt' outcome, because
    there is no longer a budget to exhaust.
    """
    cache = MacroSeriesCache(str(tmp_path / "cache"))
    fetched_at = datetime(2026, 9, 22, 8, 45, tzinfo=ET)

    prefetcher = _stub_fred(MacroDataProvider(api_key="test-key", series_cache=cache))
    with (
        patch("src.data.macro.et_now", return_value=fetched_at),
        patch("src.data.macro.et_today", return_value=fetched_at.date()),
    ):
        prefetcher.prefetch_series_cache()
    assert prefetcher.last_coverage.complete

    session = _stub_fred(MacroDataProvider(api_key="test-key", series_cache=cache))
    read_at = datetime(2026, 9, 22, 9, 30, 49, tzinfo=ET)
    with (
        patch("src.data.macro.et_now", return_value=read_at),
        patch("src.data.macro.et_today", return_value=read_at.date()),
    ):
        session.get_macro_summary()

    assert session.fred.get_series.call_count == 0, "the session went to the wire despite a valid pre-open cache"
    assert session.fred.get_series_info.call_count == 0
    assert session.last_coverage.complete
    assert len(session._run_cache_served) == len(CONFIGURED_SERIES)


def test_the_cache_does_not_launder_an_overdue_series(tmp_path):
    """A cache-served series re-derives freshness from the metadata it was
    stored with, so `overdue` still reaches the operator. The cache changes
    where the bytes came from, never what the desk claims about them."""
    cache = MacroSeriesCache(str(tmp_path / "cache"))
    index = pd.DatetimeIndex([date(2026, 9, 14), date(2026, 9, 15)])
    provider = MacroDataProvider(api_key="test-key", series_cache=cache)
    provider.fred = MagicMock()
    provider.fred.get_series.return_value = pd.Series([1.0, 1.0], index=index, dtype=float)
    provider.fred.get_series_info.return_value = pd.Series(
        {"observation_end": "2026-09-15", "last_updated": "2026-09-15"}
    )
    fetched_at = datetime(2026, 9, 22, 8, 45, tzinfo=ET)
    with (
        patch("src.data.macro.et_now", return_value=fetched_at),
        patch("src.data.macro.et_today", return_value=fetched_at.date()),
    ):
        provider._prefetch_mode = True
        provider._safe_get_series("DGS10")
        provider._prefetch_mode = False
    # Overdue at prefetch time, so no due date is stored and it is re-asked.
    assert provider._run_freshness["DGS10"].status == "overdue"
    entry = cache.load("DGS10", {})
    assert entry is not None and entry["expected_next_by"] is None


def test_the_prefetch_ceiling_is_computed_not_stored():
    """It must track the settings in force. Doubling the per-request timeout
    doubles the wire half of the bound; it is not a constant anyone typed."""
    base = MacroDataProvider(api_key="test-key", request_timeout_s=15.0)
    wider = MacroDataProvider(api_key="test-key", request_timeout_s=30.0)
    assert wider.budget.prefetch_deadline_s > base.budget.prefetch_deadline_s
    # And it must clear the 08:45 -> 09:30:49 ET gap it is scheduled into.
    assert base.budget.prefetch_deadline_s < (9 * 3600 + 30 * 60 + 49) - (8 * 3600 + 45 * 60)


def test_the_prefetch_uses_its_own_ceiling_not_the_trading_one(tmp_path):
    """90 seconds exists to stop a FRED outage stalling a live session. Nothing
    waits on the prefetch, so borrowing that ceiling here would be the
    starvation again, just an hour earlier."""
    provider = MacroDataProvider(
        api_key="test-key",
        series_cache=MacroSeriesCache(str(tmp_path / "c")),
    )
    assert provider.budget.prefetch_deadline_s > provider.total_fetch_deadline_s


@pytest.mark.parametrize("year", [2024, 2025, 2026, 2030])
def test_eleven_federal_holidays_every_year(year):
    """5 U.S.C. 6103(a) enumerates eleven. Observance can collapse two onto the
    same weekday only when a fixed-date holiday is adjacent to another, which
    does not occur in the range checked here. 2027 is excluded on purpose: 1 Jan
    2028 is a Saturday, so its observed Friday 31 Dec 2027 is filed under 2027
    (12 dates), which is the correct rule."""
    assert len(federal_holidays(year)) == 11
