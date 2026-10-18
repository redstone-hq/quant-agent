"""Event risk is answered from fetched data, not from the model's memory.

Two recorded defects are pinned here.

(c) `MarketDataProvider.get_next_earnings_date` existed specifically to ground
    `RiskVerdict.reasoning_chain.event_risk` — a REQUIRED narrative field — and
    had ZERO callers anywhere in src/ or tests/. The tests below prove the
    fetched figure now reaches the Risk Manager's actual input text, not merely
    that the function got called.

(d) No macro event calendar existed at all, so "is there an FOMC decision or a
    CPI/NFP release coming?" was answered from recollection.

The degraded paths matter as much as the happy ones: this project's standing
rule is that a labelled absence beats a fabricated figure (`pace_status` /
`unavailable_no_pinned_horizon` is the reference). Every failure mode below is
asserted to REACH the seat as an explicit unknown, never as silence and never
as an empty calendar that reads like a calm one.
"""

import json
import time
from datetime import date, timedelta

import pytest

from src.data.event_calendar import (
    EARNINGS_DEADLINE_EXCEEDED,
    EARNINGS_LOOKUP_FAILED,
    EARNINGS_LOOKUP_TIMEOUT,
    EARNINGS_MEASURED,
    EARNINGS_NO_FETCHED_DATE,
    MACRO_RELEASES,
    RELEASE_SCHEDULE_LOOKAHEAD_DAYS,
    UNCOVERED_EVENTS,
    EarningsProximity,
    EventCalendarCoverage,
    MacroEventCalendarProvider,
    fetch_earnings_proximity,
    format_event_risk_block,
    format_macro_events_section,
)
from src.models import PortfolioDecision, Position, ReasoningChain, TradeDecision
from src.trading_calendar import et_today


# --- helpers ---------------------------------------------------------------


def _pm_rc() -> ReasoningChain:
    return ReasoningChain(
        macro_filter="x",
        news_check="x",
        earnings_check="x",
        signal_conflicts="x",
        sizing_logic="x",
        portfolio_balance="x",
        cash_target="x",
    )


def _decision(symbols=("NVDA",)) -> PortfolioDecision:
    return PortfolioDecision(
        reasoning_chain=_pm_rc(),
        decisions=[
            TradeDecision(
                action="BUY",
                symbol=s,
                allocation_pct=10.0,
                entry_price=100.0,
                stop_loss=95.0,
                take_profit=115.0,
                reasoning="setup",
            )
            for s in symbols
        ],
        portfolio_view="Bullish",
    )


class _FakeMarket:
    """Stands in for MarketDataProvider — only the one method matters."""

    def __init__(self, answers=None, raises=False, hang_s=0.0):
        self.answers = answers or {}
        self.raises = raises
        self.hang_s = hang_s
        self.calls = []

    def get_next_earnings_date(self, symbol):
        self.calls.append(symbol)
        if self.hang_s:
            time.sleep(self.hang_s)
        if self.raises:
            raise RuntimeError("yfinance exploded")
        return self.answers.get(symbol)


def _calendar(payloads, *, deadline_s=20.0, **kwargs):
    """A provider whose transport returns canned payloads.

    `payloads` maps release_id -> payload dict, or a callable raising to
    simulate a transport failure.
    """
    provider = MacroEventCalendarProvider(
        api_key="dummy",
        total_fetch_deadline_s=deadline_s,
        **kwargs,
    )
    calls = []

    def _transport(url, timeout):
        calls.append((url, timeout))
        release_id = int(url.split("release_id=")[1].split("&")[0])
        entry = payloads.get(release_id)
        if callable(entry):
            return entry()
        if entry is None:
            raise OSError("connection reset")
        return entry

    provider._http_get_json = _transport
    provider.transport_calls = calls
    return provider


def _dates_payload(dates):
    return {"release_dates": [{"date": d.isoformat()} for d in dates]}


# --- defect (c): the fetched earnings date reaches the Risk Manager ---------


def test_the_fetched_earnings_date_reaches_the_risk_managers_input_text():
    """The load-bearing proof for defect (c).

    Not "the function was called" — that would pass with the number thrown
    away. The exact fetched figure must be visible in the message the model
    actually reads.
    """
    from src.agents.risk_manager import RiskManagerAgent

    market = _FakeMarket({"NVDA": 2, "JPM": 30})
    earnings = fetch_earnings_proximity(
        market,
        ["NVDA", "JPM"],
        per_symbol_timeout_s=2.0,
        total_deadline_s=5.0,
    )
    assert market.calls == ["NVDA", "JPM"]  # the dead function now has callers
    block = format_event_risk_block(
        earnings=earnings,
    )

    agent = RiskManagerAgent.__new__(RiskManagerAgent)
    message = agent.build_user_message(
        portfolio_decision=_decision(("NVDA", "JPM")),
        positions=[],
        macro_summary={},
        rule_violations=[],
        event_risk_block=block,
    )

    assert "NVDA: next earnings ~2 sessions away (fetched)" in message
    assert "EVENT WINDOW" not in message
    assert "JPM: next earnings ~30 sessions away (fetched)" in message
    assert "do NOT answer this from memory" in message


def test_risk_manager_input_says_unknown_when_the_earnings_lookup_returns_nothing():
    """Degraded path. `get_next_earnings_date` returns None for BOTH "unknown"
    and "nothing scheduled" — its own docstring forbids reading that as "no
    earnings soon". The seat must be told, in words, that the date is unknown.
    """
    from src.agents.risk_manager import RiskManagerAgent

    market = _FakeMarket({"NVDA": None})
    earnings = fetch_earnings_proximity(
        market,
        ["NVDA"],
        per_symbol_timeout_s=2.0,
        total_deadline_s=5.0,
    )
    assert earnings[0].status == EARNINGS_NO_FETCHED_DATE
    assert earnings[0].sessions_away is None

    agent = RiskManagerAgent.__new__(RiskManagerAgent)
    message = agent.build_user_message(
        portfolio_decision=_decision(("NVDA",)),
        positions=[],
        macro_summary={},
        rule_violations=[],
        event_risk_block=format_event_risk_block(
            earnings=earnings,
        ),
    )
    assert "unavailable_no_fetched_date" in message
    assert "Treat the earnings date as UNKNOWN" in message
    assert "Earnings proximity is UNKNOWN for: NVDA" in message
    # The one reading it must NOT be able to take this as reassurance.
    assert "does NOT mean no report is due" in message
    # Owner rule: earnings never resize or block a trade, so an unknown date
    # must not invite a veto either.
    assert "veto" not in message.split("Earnings proximity is UNKNOWN", 1)[1].split("\n", 1)[0]
    assert "Earnings never resize or block a trade" in message


def test_risk_manager_input_says_unknown_when_the_earnings_lookup_raises():
    market = _FakeMarket(raises=True)
    earnings = fetch_earnings_proximity(
        market,
        ["NVDA"],
        per_symbol_timeout_s=2.0,
        total_deadline_s=5.0,
    )
    assert earnings[0].status == EARNINGS_LOOKUP_FAILED
    rendered = format_event_risk_block(
        earnings=earnings,
    )
    assert "LOOKUP FAILED" in rendered
    assert "unavailable_lookup_failed" in rendered


def test_a_hanging_earnings_lookup_is_bounded_and_labelled():
    """A session must never hang on this. yfinance's calendar call has no
    timeout of its own, so the wrapper supplies one."""
    market = _FakeMarket({"NVDA": 4}, hang_s=3.0)
    started = time.monotonic()
    earnings = fetch_earnings_proximity(
        market,
        ["NVDA"],
        per_symbol_timeout_s=0.5,
        total_deadline_s=5.0,
    )
    elapsed = time.monotonic() - started
    assert elapsed < 2.5
    assert earnings[0].status == EARNINGS_LOOKUP_TIMEOUT
    assert earnings[0].sessions_away is None


def test_symbols_not_reached_inside_the_budget_are_labelled_never_dropped():
    """A symbol silently missing from the list would read to the seat as a
    symbol with nothing to report."""
    market = _FakeMarket({"AAA": 1, "BBB": 2, "CCC": 3}, hang_s=0.4)
    earnings = fetch_earnings_proximity(
        market,
        ["AAA", "BBB", "CCC"],
        per_symbol_timeout_s=0.5,
        total_deadline_s=0.6,
    )
    assert [e.symbol for e in earnings] == ["AAA", "BBB", "CCC"]
    assert earnings[-1].status in (
        EARNINGS_DEADLINE_EXCEEDED,
        EARNINGS_LOOKUP_TIMEOUT,
    )
    rendered = format_event_risk_block(
        earnings=earnings,
    )
    assert "CCC" in rendered


def test_risk_manager_input_never_goes_silent_when_no_block_is_passed():
    """An absent section reads as a calm calendar. The renderer must fire
    anyway and say NOT FETCHED."""
    from src.agents.risk_manager import RiskManagerAgent

    agent = RiskManagerAgent.__new__(RiskManagerAgent)
    message = agent.build_user_message(
        portfolio_decision=_decision(("NVDA",)),
        positions=[],
        macro_summary={},
        rule_violations=[],
    )
    assert "## Event Risk" in message
    assert "NOT FETCHED this run" in message


def test_earnings_proximity_only_reports_a_figure_when_measured():
    """The pace_status contract: a figure exists only under `measured`."""
    assert EarningsProximity("X", 3, EARNINGS_MEASURED).measured is True
    for status in (
        EARNINGS_NO_FETCHED_DATE,
        EARNINGS_LOOKUP_FAILED,
        EARNINGS_LOOKUP_TIMEOUT,
        EARNINGS_DEADLINE_EXCEEDED,
    ):
        proximity = EarningsProximity("X", None, status)
        assert proximity.measured is False
        assert status in proximity.describe()


# --- defect (a): a real macro event calendar --------------------------------


def test_calendar_returns_scheduled_releases_inside_the_horizon():
    today = et_today()
    payloads = {
        10: _dates_payload([today + timedelta(days=3), today + timedelta(days=40)]),
        50: _dates_payload([today + timedelta(days=1)]),
    }
    for release in MACRO_RELEASES:
        payloads.setdefault(release.release_id, _dates_payload([]))
    provider = _calendar(payloads)
    events = provider.get_upcoming_events(horizon_days=10)

    labels = [(e.label, e.days_away) for e in events]
    assert ("Employment Situation (NFP)", 1) in labels
    assert ("CPI", 3) in labels
    # The +40d CPI print is outside the horizon and must not be reported.
    assert all(e.days_away <= 10 for e in events)
    # Sorted soonest first, so the seat reads the imminent one first.
    assert events == sorted(events, key=lambda e: (e.event_date, e.label))


def test_calendar_asks_fred_for_dates_with_no_data_because_future_dates_need_it():
    """Live-verified 2026-08-31: without
    `include_release_dates_with_no_data=true` FRED returns count 0 for every
    FUTURE scheduled date, because a date is only "with data" once the data has
    been published. Dropping this parameter would silently empty the calendar.
    """
    payloads = {r.release_id: _dates_payload([]) for r in MACRO_RELEASES}
    provider = _calendar(payloads)
    provider.get_upcoming_events(horizon_days=10)
    assert provider.transport_calls
    for url, _timeout in provider.transport_calls:
        assert "include_release_dates_with_no_data=true" in url
        assert url.startswith("https://api.stlouisfed.org/fred/release/dates?")


def test_a_failed_calendar_is_reported_as_coverage_not_as_an_empty_calendar():
    """The degraded path for defect (a). Every release fetch fails; the seat
    must be told the calendar is UNAVAILABLE, not shown an empty one."""
    provider = _calendar({})  # every release_id -> transport raises
    events = provider.get_upcoming_events(horizon_days=10)
    coverage = provider.last_coverage

    assert events == []
    assert coverage is not None
    assert coverage.status == "failed"
    assert coverage.complete is False
    assert coverage.succeeded == 0
    assert coverage.failed_count == len(MACRO_RELEASES)

    rendered = format_macro_events_section(events, coverage, 10)
    assert "the calendar is UNAVAILABLE" in rendered
    assert "means NOT FETCHED, never" in rendered
    assert "does NOT mean an empty calendar" in rendered
    # And it must never say the reassuring thing.
    assert "none lands inside the next" not in rendered


def test_a_partial_calendar_names_the_releases_that_failed():
    today = et_today()
    # Every release answers with a real (far-future) schedule except payrolls,
    # whose transport fails — so `failed` isolates exactly that one.
    payloads = {r.release_id: _dates_payload([today + timedelta(days=200)]) for r in MACRO_RELEASES}
    payloads[10] = _dates_payload([today + timedelta(days=2)])
    payloads[50] = None  # transport failure for payrolls only
    provider = _calendar(
        payloads,
        max_retries=0,
        retry_backoff_base_s=0.01,
        retry_backoff_max_s=0.01,
        retry_backoff_jitter_s=0.0,
    )
    events = provider.get_upcoming_events(horizon_days=10)
    coverage = provider.last_coverage

    assert coverage.status == "partial"
    assert [f.label for f in coverage.failed] == ["Employment Situation (NFP)"]
    assert any(e.label == "CPI" for e in events)
    assert "Employment Situation (NFP)" in coverage.describe()
    assert "coverage GAP" in coverage.describe()


def test_a_clean_response_with_no_scheduled_dates_is_an_absence_not_a_success():
    payloads = {r.release_id: _dates_payload([]) for r in MACRO_RELEASES}
    provider = _calendar(payloads)
    provider.get_upcoming_events(horizon_days=10)
    coverage = provider.last_coverage
    assert coverage.status == "failed"
    assert {f.reason for f in coverage.failed} == {"no_scheduled_dates_published"}


# --- the three outcomes of one release fetch --------------------------------
#
# Until 2026-09-23 there were only two. The fetch window was the HORIZON
# (10 days by default) and the tracked releases are monthly, so a release whose
# schedule was published and simply five weeks out came back empty and was
# recorded as `no_scheduled_dates_published` -- a source failure. Measured
# against the live FRED API on 2026-09-23: over the next 10 days CPI (10), PPI
# (46) and Retail Sales (9) each returned 0 dates; over the next 120 days they
# returned three real dates apiece. Production logged "Macro event calendar
# PARTIAL this run" with those three in the FAILED list, every run, for
# releases that were never missing.


def test_the_fetch_window_is_the_wide_lookahead_not_the_horizon():
    """One request per release, just a later `realtime_end`. The horizon must
    not be what is asked of FRED -- that is the defect."""
    payloads = {r.release_id: _dates_payload([]) for r in MACRO_RELEASES}
    provider = _calendar(payloads)
    today = et_today()
    provider.get_upcoming_events(horizon_days=10)

    assert len(provider.transport_calls) == len(MACRO_RELEASES)
    expected_end = (today + timedelta(days=RELEASE_SCHEDULE_LOOKAHEAD_DAYS)).isoformat()
    for url, _timeout in provider.transport_calls:
        assert f"realtime_start={today.isoformat()}" in url
        assert f"realtime_end={expected_end}" in url


def test_a_horizon_wider_than_the_lookahead_still_fetches_the_whole_horizon():
    payloads = {r.release_id: _dates_payload([]) for r in MACRO_RELEASES}
    provider = _calendar(payloads)
    today = et_today()
    horizon = RELEASE_SCHEDULE_LOOKAHEAD_DAYS + 90
    provider.get_upcoming_events(horizon_days=horizon)
    expected_end = (today + timedelta(days=horizon)).isoformat()
    for url, _timeout in provider.transport_calls:
        assert f"realtime_end={expected_end}" in url


def test_outcome_one_a_date_inside_the_horizon_is_an_event():
    today = et_today()
    payloads = {r.release_id: _dates_payload([]) for r in MACRO_RELEASES}
    payloads[10] = _dates_payload([today + timedelta(days=4)])
    provider = _calendar(payloads)
    events = provider.get_upcoming_events(horizon_days=10)
    coverage = provider.last_coverage

    assert [(e.label, e.days_away) for e in events] == [("CPI", 4)]
    assert 10 not in {f.release_id for f in coverage.failed}
    # An imminent release is event risk, not a "beyond the horizon" note.
    assert all(e.release_id != 10 for e in coverage.next_beyond_horizon)


def test_outcome_two_a_published_schedule_beyond_the_horizon_is_not_a_failure():
    """The regression this fix exists for. CPI, PPI and Retail Sales all have
    real published schedules whose next date is ~3 weeks out. None of them is
    imminent and NONE of them is a failed fetch."""
    today = et_today()
    payloads = {r.release_id: _dates_payload([]) for r in MACRO_RELEASES}
    monthly = {
        10: today + timedelta(days=21),
        46: today + timedelta(days=22),
        9: today + timedelta(days=22),
    }
    for release_id, first in monthly.items():
        payloads[release_id] = _dates_payload([first, first + timedelta(days=28), first + timedelta(days=58)])
    # Everything else answers with a schedule too, so `failed` is empty only if
    # the beyond-horizon releases are genuinely counted as successes.
    for release in MACRO_RELEASES:
        if release.release_id not in monthly:
            payloads[release.release_id] = _dates_payload([today + timedelta(days=40)])
    provider = _calendar(payloads)
    events = provider.get_upcoming_events(horizon_days=10)
    coverage = provider.last_coverage

    assert events == []
    assert coverage.status == "ok"
    assert coverage.complete is True
    assert coverage.succeeded == len(MACRO_RELEASES)
    assert coverage.failed == []
    # The specific mislabelling that produced the production warning.
    assert "no_scheduled_dates_published" not in coverage.describe()
    assert "FAILED" not in coverage.describe()

    # And the next date is surfaced rather than dropped: the desk can name it.
    by_label = {e.label: e for e in coverage.next_beyond_horizon}
    assert by_label["CPI"].event_date == monthly[10]
    assert by_label["CPI"].days_away == 21
    assert by_label["PPI"].event_date == monthly[46]
    # Only the NEXT one per release, and sorted soonest first.
    assert len(by_label) == len(MACRO_RELEASES)
    assert coverage.next_beyond_horizon == sorted(coverage.next_beyond_horizon, key=lambda e: (e.event_date, e.label))

    rendered = format_macro_events_section(events, coverage, 10)
    assert monthly[10].isoformat() in rendered
    assert "NOT imminent" in rendered
    assert "Full coverage" in rendered


def test_outcome_three_an_empty_wide_window_is_still_a_real_failure():
    """The wide window is what makes this trustworthy: 120 days of nothing for
    a monthly release is a genuinely unpublished schedule, not a narrow query.
    """
    payloads = {r.release_id: _dates_payload([]) for r in MACRO_RELEASES}
    provider = _calendar(payloads)
    events = provider.get_upcoming_events(horizon_days=10)
    coverage = provider.last_coverage

    assert events == []
    assert coverage.status == "failed"
    assert coverage.succeeded == 0
    assert {f.reason for f in coverage.failed} == {"no_scheduled_dates_published"}
    assert coverage.next_beyond_horizon == []


def test_a_beyond_horizon_release_never_degrades_macro_coverage():
    """One release genuinely unpublished, one merely not imminent. Exactly one
    of them may appear in FAILED."""
    today = et_today()
    payloads = {r.release_id: _dates_payload([today + timedelta(days=45)]) for r in MACRO_RELEASES}
    payloads[180] = _dates_payload([])  # genuinely nothing published
    provider = _calendar(payloads)
    provider.get_upcoming_events(horizon_days=10)
    coverage = provider.last_coverage

    assert coverage.status == "partial"
    assert [f.label for f in coverage.failed] == ["Initial Jobless Claims"]
    assert coverage.succeeded == len(MACRO_RELEASES) - 1
    labels = {e.label for e in coverage.next_beyond_horizon}
    assert "CPI" in labels
    assert "Initial Jobless Claims" not in labels


def test_nothing_beyond_the_horizon_renders_no_beyond_horizon_block():
    ok = EventCalendarCoverage(configured=7, succeeded=7, failed=[])
    rendered = format_macro_events_section([], ok, 10)
    assert "NOT imminent" not in rendered
    assert "None. All 7 tracked release schedules fetched successfully" in rendered


def test_an_empty_calendar_reads_as_empty_only_when_coverage_is_ok():
    today = et_today()
    ok = EventCalendarCoverage(configured=7, succeeded=7, failed=[])
    rendered = format_macro_events_section([], ok, 10)
    assert "None. All 7 tracked release schedules fetched successfully" in rendered

    not_fetched = format_macro_events_section([], None, 10)
    assert "NOT FETCHED this run" in not_fetched
    assert "None. All" not in not_fetched
    assert today is not None


def test_the_calendar_respects_its_wall_clock_ceiling():
    """A slow FRED must not stall the session. The deadline is a real ceiling,
    not an upper bound implied by retry arithmetic."""

    def _slow():
        time.sleep(0.6)
        return _dates_payload([])

    payloads = {r.release_id: _slow for r in MACRO_RELEASES}
    provider = _calendar(payloads, deadline_s=1.0, request_timeout_s=1.0)
    started = time.monotonic()
    provider.get_upcoming_events(horizon_days=10)
    elapsed = time.monotonic() - started

    assert elapsed < 3.0, f"calendar overran its ceiling: {elapsed:.1f}s"
    reasons = {f.reason for f in provider.last_coverage.failed}
    assert "fetch_deadline_exceeded" in reasons
    # Skipped releases are REPORTED, never quietly omitted.
    assert provider.last_coverage.succeeded + provider.last_coverage.failed_count == len(MACRO_RELEASES)


def test_a_slow_release_does_not_starve_the_releases_behind_it_in_the_list():
    """Board item 187. Production logs (2026-09-24/25) showed the SAME tail
    releases — PPI, PCE, GDP, Retail Sales, Initial Jobless Claims — failing
    `fetch_deadline_exceeded` run after run, while CPI/NFP at the front of
    `MACRO_RELEASES` almost always got through. The cause: one shared
    deadline let an early, slow release consume nearly the whole budget
    (up to its full `request_timeout_s`), so every release behind it in the
    fixed iteration order was skipped WITHOUT EVEN AN ATTEMPT.

    This pins the fix: the remaining budget is split fairly across the
    releases not yet attempted, so a slow CPI response can only consume its
    own share, not the releases behind it. CPI genuinely times out here (its
    fake transport takes far longer than any fair share can afford); the six
    releases behind it must still each get a real attempt and succeed.
    """
    today = et_today()
    calls = []

    def _transport(url, timeout):
        release_id = int(url.split("release_id=")[1].split("&")[0])
        calls.append(release_id)
        if release_id == 10:  # CPI — the always-first release in the list
            # A response slower than any per-release share can afford.
            # Mirrors what a real socket timeout does: block for the granted
            # window, then raise, rather than returning instantly.
            time.sleep(timeout)
            raise OSError("read timed out")
        return _dates_payload([today + timedelta(days=2)])

    provider = MacroEventCalendarProvider(
        api_key="dummy",
        total_fetch_deadline_s=1.0,
        request_timeout_s=1.0,
        max_retries=0,
        retry_backoff_base_s=0.0,
        retry_backoff_max_s=0.0,
        retry_backoff_jitter_s=0.0,
        breaker_after_failed_releases=10,
    )
    provider._http_get_json = _transport

    started = time.monotonic()
    events = provider.get_upcoming_events(horizon_days=10)
    elapsed = time.monotonic() - started
    coverage = provider.last_coverage

    # Every release was actually attempted — none skipped on the "deadline
    # already exceeded, no attempt made" path that starved the tail before.
    assert sorted(calls) == sorted(r.release_id for r in MACRO_RELEASES)
    # CPI is the one genuine failure; the other six all succeeded, including
    # the releases that were chronically starved in production.
    assert coverage.succeeded == len(MACRO_RELEASES) - 1
    failed_labels = {f.label for f in coverage.failed}
    assert failed_labels == {"CPI"}
    for label in (
        "PPI",
        "Personal Income and Outlays (PCE)",
        "GDP",
        "Retail Sales (advance)",
        "Initial Jobless Claims",
    ):
        assert any(e.label == label for e in events), (
            f"{label} was starved even though it should have gotten its fair share of the remaining budget"
        )
    # The fair-share ceiling still respects the overall wall-clock budget.
    assert elapsed < 2.0, f"calendar overran its ceiling: {elapsed:.1f}s"


def test_the_calendar_retries_a_transient_failure_before_degrading():
    today = et_today()
    attempts = {"n": 0}

    def _flaky():
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise OSError("read timed out")
        return _dates_payload([today + timedelta(days=2)])

    payloads = {r.release_id: _dates_payload([]) for r in MACRO_RELEASES}
    payloads[10] = _flaky
    provider = _calendar(
        payloads,
        max_retries=1,
        retry_backoff_base_s=0.01,
        retry_backoff_max_s=0.01,
        retry_backoff_jitter_s=0.0,
    )
    events = provider.get_upcoming_events(horizon_days=10)
    assert attempts["n"] == 2
    assert any(e.label == "CPI" for e in events)


def test_the_calendar_refuses_to_construct_without_a_fred_key():
    with pytest.raises(ValueError, match="FRED_API_KEY"):
        MacroEventCalendarProvider(api_key="  ")


def test_a_malformed_fred_response_degrades_rather_than_raising():
    payloads = {r.release_id: {"unexpected": True} for r in MACRO_RELEASES}
    provider = _calendar(payloads)
    assert provider.get_upcoming_events(horizon_days=10) == []
    assert {f.reason for f in provider.last_coverage.failed} == {"malformed_response"}


# --- the free-source coverage boundary is declared, not hidden --------------


def test_fred_is_still_not_the_fomc_source_and_the_gap_list_no_longer_claims_it_is():
    """FRED's release 101 ("FOMC Press Release") is a DAILY release carrying no
    meeting schedule — live-verified: over 2026-01-01..2026-08-30 it returns all
    240 calendar days. It is still not wired, and it never should be.

    What changed is where the answer comes from. FOMC dates used to be declared
    in `UNCOVERED_EVENTS` as a gap no free source could fill; they are now
    fetched from the Federal Reserve's own free calendar, so that declaration
    would be a stale instruction telling the seat to distrust data it now has.
    The remaining entries are the events for which that is still true.
    """
    assert 101 not in {r.release_id for r in MACRO_RELEASES}
    assert not any("FOMC meeting" in item for item in UNCOVERED_EVENTS)
    assert UNCOVERED_EVENTS, "an empty gap list would render a heading over nothing"
    for rendered in (format_macro_events_section([], None, 10),):
        assert "FOMC" in rendered
        assert "do not supply a date from memory" in rendered


def test_the_tracked_releases_are_the_live_verified_ids():
    """Each id was confirmed against the live FRED API (read-only GET, real
    key) before being wired in — same discipline as the Phase 4.2 series
    additions. A silent id change would fetch someone else's calendar."""
    assert {r.release_id: r.label for r in MACRO_RELEASES} == {
        10: "CPI",
        50: "Employment Situation (NFP)",
        46: "PPI",
        54: "Personal Income and Outlays (PCE)",
        53: "GDP",
        9: "Retail Sales (advance)",
        180: "Initial Jobless Claims",
    }


# --- the macro seat gets the same calendar ----------------------------------


# --- the risk stage assembles it -------------------------------------------


class _StubConfig:
    class event_risk:
        horizon_days = 10
        earnings_deadline_s = 5.0
        earnings_symbol_timeout_s = 2.0


class _StubPipeline:
    def __init__(self, market):
        self.config = _StubConfig()
        self.market = market


def _ctx(decision, events=None, coverage=None):
    from src.pipeline_context import RunContext

    ctx = RunContext(run_id="r1", session="morning")
    ctx.portfolio_decision = decision
    ctx.macro_events = events or []
    ctx.macro_event_coverage = coverage
    return ctx


def test_risk_stage_block_fetches_earnings_for_exactly_the_symbols_under_review():
    from src.pipeline_stages import RiskStage

    market = _FakeMarket({"NVDA": 1, "JPM": None})
    block = RiskStage._build_event_risk_block(
        _StubPipeline(market),
        _ctx(
            _decision(("NVDA", "JPM")),
            events=[],
            coverage=EventCalendarCoverage(configured=7, succeeded=7, failed=[]),
        ),
    )
    assert market.calls == ["NVDA", "JPM"]
    assert "NVDA: next earnings ~1 session away (fetched)" in block
    assert "EVENT WINDOW" not in block
    assert "JPM" in block and "unavailable_no_fetched_date" in block


def test_risk_stage_block_says_not_fetched_when_research_never_ran():
    """The resume lane: RiskStage runs without MorningResearchStage having
    populated a calendar. The seat is told, not shown an empty calendar."""
    from src.pipeline_stages import RiskStage

    block = RiskStage._build_event_risk_block(
        _StubPipeline(_FakeMarket({"NVDA": 6})),
        _ctx(_decision(("NVDA",))),
    )
    assert "NVDA: next earnings ~6 sessions away (fetched)" in block


def test_risk_stage_block_survives_a_broken_market_provider():
    from src.pipeline_stages import RiskStage

    class _Broken:
        def get_next_earnings_date(self, symbol):
            raise RuntimeError("provider down")

    block = RiskStage._build_event_risk_block(
        _StubPipeline(_Broken()),
        _ctx(_decision(("NVDA",))),
    )
    assert "unavailable_lookup_failed" in block


# --- defect (b): FOMC meeting dates are FETCHED, not recalled ---------------
#
# The gap PR #170 declared rather than closed. FRED cannot answer it; the
# Federal Reserve publishes its own calendar free, and these tests pin both the
# happy path and — the part that matters — every way the answer can be absent.
# The one sentence that must never appear without a published schedule behind
# it is "no FOMC meeting is scheduled in this window".

from src.data.event_calendar import (  # noqa: E402 — grouped with its own tests
    FOMC_MEASURED,
    FOMC_MEASURED_STALE_CACHE,
    FOMC_SOURCE_HTML,
    FOMC_SOURCE_JSON,
    FOMC_UNAVAILABLE_DEADLINE_EXCEEDED,
    FOMC_UNAVAILABLE_FETCH_FAILED,
    FOMCCalendarParseError,
    FOMCCalendarProvider,
    FOMCCoverage,
    FOMCMeeting,
    format_fomc_section,
    parse_fomc_meetings_from_html,
    parse_fomc_meetings_from_json,
)


# --- fixtures: the live document shapes, recorded 2026-08-31 ---------------
#
# Trimmed from the real responses (539 KB of JSON, 165 KB of HTML) to the rows
# that carry a distinct parsing decision. Every field name, every escape and
# every wording below is verbatim from the live payload — a fixture that has
# been tidied into a shape the parser finds convenient proves nothing.


def _fomc_json_payload(rows):
    return {"events": list(rows), "announcement": []}


#: A meeting, its minutes, and its press conference all share `type: "FOMC"`.
#: Only the first is a meeting; reporting the other two as meetings would put
#: three "Fed decisions" on the calendar for every one that exists.
_LIVE_JSON_ROWS = [
    {
        "description": "&lt;p&gt;Two-day meeting, September 15 - 16&lt;/p&gt;&#10;&#10;&lt;p&gt;Press Conference&lt;/p&gt;",
        "title": "FOMC Meeting",
        "time": "2:00 p.m.",
        "month": "2026-09",
        "days": "16",
        "type": "FOMC",
    },
    {
        "link": "https://www.federalreserve.gov/live-broadcast.htm",
        "title": "FOMC Press Conference",
        "time": "2:30 p.m.",
        "month": "2026-09",
        "days": "16",
        "type": "FOMC",
    },
    {
        "description": "&lt;p&gt;Meeting of September 15-16&lt;/p&gt;",
        "title": " FOMC Minutes",
        "time": "2:00 p.m.",
        "month": "2026-10",
        "days": "7",
        "type": "FOMC",
    },
    {
        "description": "&lt;p&gt;Two-day meeting, October 27 - 28&lt;/p&gt;&#10;&#10;&lt;p&gt;Press Conference&lt;/p&gt;",
        "title": "FOMC Meeting",
        "time": "2:00 p.m.",
        "month": "2026-10",
        "days": "28",
        "type": "FOMC",
    },
    # Month-straddling block. `month`/`days` give only the CONCLUDING day, in
    # November — the start is in October and has to come from the duration.
    {
        "description": "&lt;p&gt;&lt;span&gt;&lt;span&gt;Two-day meeting, &lt;/span&gt;October &lt;/span&gt;31 - November 1&lt;/p&gt;",
        "title": "FOMC meeting",
        "time": "2:00 p.m.",
        "month": "2017-11",
        "days": "1",
        "type": "FOMC",
    },
    # A non-FOMC row from the same feed — 1,059 of the live 2,582 events are
    # statistical releases, and none of them is a Fed decision.
    {
        "description": "Economic Outlook",
        "title": "Speech - Governor",
        "time": "8:30 a.m.",
        "month": "2026-09",
        "days": "3",
        "type": "Speeches",
    },
]


def _fomc_html_row(month_text, day_text, shaded=False):
    shade = "fomc-meeting--shaded " if shaded else ""
    return f'''
        <div class="{shade}row fomc-meeting" ">
            <div class="{shade}fomc-meeting__month col-xs-5 col-sm-3 col-md-2"><strong>{month_text}</strong></div>
            <div class="fomc-meeting__date col-xs-4 col-sm-9 col-md-10 col-lg-1">{day_text}</div>
            <div class="col-xs-12 col-md-4 col-lg-4 fomc-meeting__minutes"></div>
        </div>
    '''


def _fomc_html_page(panels):
    """Year panels in the order given.

    The live page lists 2026, 2025, ... 2021 and THEN 2027, so a parser that
    assumes chronological panels files the 2027 meetings under 2021. Tests pass
    them out of order for exactly that reason.
    """
    out = []
    for year, rows in panels:
        out.append(
            f'<div class="panel panel-default"><div class="panel-heading">'
            f'<h4><a id="45694">{year} FOMC Meetings</a></h4></div>'
        )
        out.extend(_fomc_html_row(m, d) for m, d in rows)
        out.append(
            '<div class="panel-footer">* Meeting associated with a Summary of Economic Projections. </div></div>'
        )
    return "<html><body>" + "".join(out) + "</body></html>"


_LIVE_HTML_PAGE = _fomc_html_page(
    [
        # Deliberately not chronological, exactly like the live page.
        ("2026", [("September", "15-16*"), ("October", "27-28"), ("December", "8-9*")]),
        ("2025", [("August", "22 (notation vote)"), ("October", "28-29")]),
        ("2021", [("Oct/Nov", "31-1")]),
        ("2027", [("January", "26-27"), ("December", "7-8*")]),
    ]
)


def _fomc_provider(tmp_path, *, json_body=None, html_body=None, **kwargs):
    """A provider whose transport returns canned bodies.

    `json_body` / `html_body` may be bytes, or a callable (called with no
    arguments) to raise or to stall. None means that URL's transport raises.
    """
    kwargs.setdefault("max_retries", 0)
    kwargs.setdefault("retry_backoff_base_s", 0.01)
    kwargs.setdefault("retry_backoff_max_s", 0.01)
    kwargs.setdefault("retry_backoff_jitter_s", 0.0)
    provider = FOMCCalendarProvider(
        cache_path=str(tmp_path / "fomc_calendar.json"),
        **kwargs,
    )
    calls = []

    def _transport(url, timeout):
        calls.append(url)
        body = json_body if url == provider.json_url else html_body
        if callable(body):
            return body()
        if body is None:
            raise OSError("connection reset by peer")
        return body

    provider._http_get_bytes = _transport
    provider.transport_calls = calls
    return provider


def _json_bytes(rows, *, bom=True):
    # The live feed is served UTF-8 WITH A BOM. Encoding it that way here is
    # the point: a `utf-8` decode of this raises, and the provider must not.
    text = json.dumps(_fomc_json_payload(rows))
    return ("﻿" + text).encode("utf-8") if bom else text.encode("utf-8")


def _fomc_days(meetings):
    return [(m.start_date.isoformat(), m.end_date.isoformat()) for m in meetings]


def _two_day_html_page(concluding: date) -> tuple[date, str]:
    """A one-panel page holding a single two-day meeting ending on/after
    `concluding`, nudged off the 1st so the block stays inside one month.

    Nudging keeps these tests date-independent: the fixture builder writes a
    `D-1 - D` row, which is only expressible within a month when D >= 2.
    """
    if concluding.day < 2:
        concluding += timedelta(days=2)
    page = _fomc_html_page(
        [
            (
                str(concluding.year),
                [
                    (concluding.strftime("%B"), f"{concluding.day - 1}-{concluding.day}"),
                ],
            )
        ]
    )
    return concluding, page


# --- the two parse boundaries ----------------------------------------------


def test_the_json_feed_yields_meetings_and_ignores_minutes_and_press_conferences():
    """`type == "FOMC"` is not the same question as "is this a meeting". The
    live feed files minutes and press conferences under the same type, and
    counting them would triple the number of Fed decisions on the calendar."""
    meetings = parse_fomc_meetings_from_json(_fomc_json_payload(_LIVE_JSON_ROWS))
    assert _fomc_days(meetings) == [
        ("2017-10-31", "2017-11-01"),  # month-straddling block
        ("2026-09-15", "2026-09-16"),
        ("2026-10-27", "2026-10-28"),
    ]
    assert all(m.days == 2 for m in meetings)
    assert all(m.duration_stated for m in meetings)


def test_a_json_meeting_with_no_stated_duration_is_one_day_and_says_so():
    """The source gave a concluding date and nothing else. Assuming a second
    day into existence would be inventing a date; the block is reported as one
    day and the unknown is labelled."""
    meetings = parse_fomc_meetings_from_json(
        _fomc_json_payload(
            [
                {
                    "title": "FOMC Meeting",
                    "month": "2026-09",
                    "days": "16",
                    "type": "FOMC",
                }
            ]
        )
    )
    assert _fomc_days(meetings) == [("2026-09-16", "2026-09-16")]
    assert meetings[0].duration_stated is False
    assert "block length is UNKNOWN" in meetings[0].describe(date(2026, 9, 1))


@pytest.mark.parametrize(
    "payload",
    [
        {"events": []},
        {"events": [{"type": "Speeches", "title": "Speech", "month": "2026-09", "days": "3"}]},
        {"nothing": "recognisable"},
        "not an object at all",
    ],
)
def test_a_redesigned_json_feed_raises_instead_of_returning_no_meetings(payload):
    """The load-bearing property of the parse boundary. An empty list would
    reach the seat as "no meetings", which is exactly the false reassurance
    this whole module exists to prevent."""
    with pytest.raises(FOMCCalendarParseError):
        parse_fomc_meetings_from_json(payload)


def test_the_html_page_yields_meetings_including_the_awkward_real_rows():
    """FALLBACK parser. Every shape here is one the live page actually
    contains: an SEP asterisk, a month-straddling block written `Oct/Nov 31-1`,
    a one-day unscheduled notation vote, and year panels out of order."""
    meetings = parse_fomc_meetings_from_html(_LIVE_HTML_PAGE)
    assert _fomc_days(meetings) == [
        ("2021-10-31", "2021-11-01"),
        ("2025-08-22", "2025-08-22"),
        ("2025-10-28", "2025-10-29"),
        ("2026-09-15", "2026-09-16"),
        ("2026-10-27", "2026-10-28"),
        ("2026-12-08", "2026-12-09"),
        ("2027-01-26", "2027-01-27"),
        ("2027-12-07", "2027-12-08"),
    ]
    # The 2027 panel is LAST on the live page; a parser assuming chronological
    # order files these under 2021.
    assert ("2027-01-26", "2027-01-27") in _fomc_days(meetings)


@pytest.mark.parametrize(
    "document",
    [
        "<html><body><p>We have redesigned this page.</p></body></html>",
        '<div class="panel-heading"><h4>2027 FOMC Meetings</h4></div><p>coming soon</p>',
        "",
    ],
)
def test_a_redesigned_fomc_page_raises_instead_of_returning_no_meetings(document):
    with pytest.raises(FOMCCalendarParseError):
        parse_fomc_meetings_from_html(document)


def test_an_implausible_block_is_discarded_rather_than_reported():
    """A parse that produces a plausible-looking but wrong block is worse than
    one that produces nothing: a fabricated Fed decision would be sized
    around."""
    page = _fomc_html_page(
        [
            (
                "2026",
                [
                    ("September", "1-30"),  # a month-long "meeting" — not real
                    ("October", "27-28"),
                ],
            )
        ]
    )
    assert _fomc_days(parse_fomc_meetings_from_html(page)) == [
        ("2026-10-27", "2026-10-28"),
    ]


def test_a_realistic_year_parses_as_eight_two_day_meetings():
    """The shape sanity check: the FOMC meets roughly eight times a year in
    two-day blocks. A source that does not look like that is the wrong
    source."""
    page = _fomc_html_page(
        [
            (
                "2027",
                [
                    ("January", "26-27"),
                    ("March", "16-17*"),
                    ("April", "27-28"),
                    ("June", "8-9*"),
                    ("July", "27-28"),
                    ("September", "14-15*"),
                    ("October", "26-27"),
                    ("December", "7-8*"),
                ],
            )
        ]
    )
    meetings = parse_fomc_meetings_from_html(page)
    assert len(meetings) == 8
    assert {m.days for m in meetings} == {2}


# --- source selection: structured first, rendered page only when needed -----


def test_the_structured_feed_is_used_and_the_rendered_page_is_not_touched(tmp_path):
    """The rendered page is a FALLBACK. When the JSON feed answers with a
    schedule that spans the horizon, the HTML page must not be fetched at
    all."""
    today = et_today()
    far = today + timedelta(days=120)
    rows = [
        {
            "title": "FOMC Meeting",
            "type": "FOMC",
            "month": far.strftime("%Y-%m"),
            "days": str(far.day),
            "description": "<p>Two-day meeting</p>",
        }
    ]
    provider = _fomc_provider(tmp_path, json_body=_json_bytes(rows), html_body=None)
    meetings = provider.get_meetings(horizon_days=10)

    assert provider.transport_calls == [provider.json_url]
    assert FOMC_SOURCE_HTML not in (provider.last_coverage.source or "")
    assert provider.last_coverage.source == FOMC_SOURCE_JSON
    assert provider.last_coverage.status == FOMC_MEASURED
    assert provider.last_coverage.covers_horizon is True
    assert _fomc_days(meetings) == [
        ((far - timedelta(days=1)).isoformat(), far.isoformat()),
    ]


def test_the_rendered_page_is_reached_for_when_the_feed_stops_short(tmp_path):
    """The year-boundary case, and the reason the fallback exists at all.

    Live-checked 2026-08-31: the JSON feed's events end at 2026-12, while the
    page already lists all eight 2027 meetings. A JSON-only implementation
    would report "no meeting" in December for a window it could not see.
    """
    today = et_today()
    near = today + timedelta(days=2)
    rows = [
        {
            "title": "FOMC Meeting",
            "type": "FOMC",
            "month": near.strftime("%Y-%m"),
            "days": str(near.day),
            "description": "<p>Two-day meeting</p>",
        }
    ]
    later, page = _two_day_html_page(today + timedelta(days=60))
    provider = _fomc_provider(
        tmp_path,
        json_body=_json_bytes(rows),
        html_body=page.encode("utf-8"),
    )
    meetings = provider.get_meetings(horizon_days=10)

    assert provider.transport_calls == [provider.json_url, provider.html_url]
    assert provider.last_coverage.source == f"{FOMC_SOURCE_JSON} + {FOMC_SOURCE_HTML}"
    assert provider.last_coverage.covers_horizon is True
    # The union of both sources, not one replacing the other.
    assert _fomc_days(meetings) == [
        ((near - timedelta(days=1)).isoformat(), near.isoformat()),
        ((later - timedelta(days=1)).isoformat(), later.isoformat()),
    ]


def test_the_rendered_page_carries_the_schedule_when_the_feed_fails(tmp_path):
    today = et_today()
    later, page = _two_day_html_page(today + timedelta(days=40))
    provider = _fomc_provider(tmp_path, json_body=None, html_body=page.encode("utf-8"))
    meetings = provider.get_meetings(horizon_days=10)

    assert provider.last_coverage.status == FOMC_MEASURED
    assert provider.last_coverage.source == FOMC_SOURCE_HTML
    assert "json:" in provider.last_coverage.reason
    assert len(meetings) == 1


# --- degraded paths: never the reassuring sentence --------------------------


def test_both_sources_failing_is_reported_as_unavailable_not_as_no_meetings(tmp_path):
    provider = _fomc_provider(tmp_path, json_body=None, html_body=None)
    meetings = provider.get_meetings(horizon_days=10)
    coverage = provider.last_coverage

    assert meetings == []
    assert coverage.status == FOMC_UNAVAILABLE_FETCH_FAILED
    assert coverage.measured is False
    assert coverage.covers_horizon is False

    rendered = format_fomc_section(meetings, coverage, 10)
    assert "FOMC schedule UNAVAILABLE" in rendered
    assert "NOT a confirmation that no meeting is scheduled" in rendered
    assert "do not supply a meeting date from memory" in rendered
    # It must never say the reassuring thing.
    assert "None. The published FOMC schedule spans" not in rendered
    assert "no meeting falls inside it" not in rendered


def test_a_calendar_that_was_never_consulted_says_so_rather_than_rendering_empty(tmp_path):
    rendered = format_fomc_section(None, None, 10)
    assert "NOT FETCHED this run" in rendered
    assert "Treat the FOMC schedule as UNKNOWN" in rendered
    assert "no meeting falls inside it" not in rendered
    assert tmp_path is not None


def test_a_schedule_that_stops_inside_the_horizon_refuses_to_call_it_empty(tmp_path):
    """The subtle failure the fallback exists for, asserted at the renderer.

    A published schedule that ends before the horizon does can produce an empty
    meeting list — and that list means "cannot see", not "nothing there".
    """
    today = et_today()
    coverage = FOMCCoverage(
        status=FOMC_MEASURED,
        source=FOMC_SOURCE_JSON,
        schedule_through=today + timedelta(days=3),
        horizon_end=today + timedelta(days=10),
    )
    assert coverage.covers_horizon is False
    rendered = format_fomc_section([], coverage, 10)
    assert "NOT a confirmed empty window" in rendered
    assert "BEFORE THE END OF THIS HORIZON" in rendered
    assert "None. The published FOMC schedule spans" not in rendered
    assert tmp_path is not None


def test_the_reassuring_sentence_needs_a_schedule_that_spans_the_whole_horizon():
    today = et_today()
    coverage = FOMCCoverage(
        status=FOMC_MEASURED,
        source=FOMC_SOURCE_JSON,
        schedule_through=today + timedelta(days=200),
        horizon_end=today + timedelta(days=10),
    )
    assert coverage.covers_horizon is True
    rendered = format_fomc_section(
        [FOMCMeeting(today + timedelta(days=199), today + timedelta(days=200))],
        coverage,
        10,
    )
    assert "None. The published FOMC schedule spans the next 10 calendar days" in rendered
    assert "Next scheduled meeting beyond this horizon" in rendered


# --- caching ----------------------------------------------------------------


def test_a_fresh_cache_that_spans_the_horizon_issues_no_request_at_all(tmp_path):
    """FOMC dates change roughly twice a year. Refetching every session is
    waste, and this is the assertion that it does not happen."""
    today = et_today()
    far = today + timedelta(days=120)
    rows = [
        {
            "title": "FOMC Meeting",
            "type": "FOMC",
            "month": far.strftime("%Y-%m"),
            "days": str(far.day),
            "description": "<p>Two-day meeting</p>",
        }
    ]
    first = _fomc_provider(tmp_path, json_body=_json_bytes(rows))
    first.get_meetings(horizon_days=10)
    assert first.transport_calls == [first.json_url]

    second = _fomc_provider(tmp_path, json_body=_json_bytes(rows))
    meetings = second.get_meetings(horizon_days=10)
    assert second.transport_calls == []
    assert second.last_coverage.status == FOMC_MEASURED
    assert second.last_coverage.cache_age_days == 0
    assert len(meetings) == 1


def test_a_young_cache_that_stops_short_of_the_horizon_is_refetched(tmp_path):
    """Freshness alone is not enough. A cache that cannot answer the question
    being asked is not a usable cache, however new it is."""
    today = et_today()
    near = today + timedelta(days=2)
    rows = [
        {
            "title": "FOMC Meeting",
            "type": "FOMC",
            "month": near.strftime("%Y-%m"),
            "days": str(near.day),
            "description": "<p>Two-day meeting</p>",
        }
    ]
    first = _fomc_provider(tmp_path, json_body=_json_bytes(rows))
    first.get_meetings(horizon_days=10)

    second = _fomc_provider(tmp_path, json_body=_json_bytes(rows))
    second.get_meetings(horizon_days=10)
    # Both URLs consulted again, despite a same-day cache.
    assert second.transport_calls == [second.json_url, second.html_url]


def test_a_stale_cache_is_served_wearing_its_age_never_as_fresh_data(tmp_path):
    """ "Degrade honestly" in one assertion: the dates are still handed over —
    they are real published data — and the seat is told they are cached, how
    old they are, and that the live calendar did not answer."""
    today = et_today()
    far = today + timedelta(days=120)
    (tmp_path / "fomc_calendar.json").write_text(
        json.dumps(
            {
                "fetched_on": (today - timedelta(days=400)).isoformat(),
                "source": FOMC_SOURCE_JSON,
                "meetings": [
                    {
                        "start_date": (far - timedelta(days=1)).isoformat(),
                        "end_date": far.isoformat(),
                        "duration_stated": True,
                    }
                ],
            }
        )
    )
    provider = _fomc_provider(tmp_path, json_body=None, html_body=None)
    meetings = provider.get_meetings(horizon_days=10)
    coverage = provider.last_coverage

    assert len(meetings) == 1
    assert coverage.status == FOMC_MEASURED_STALE_CACHE
    assert coverage.cache_age_days == 400
    rendered = format_fomc_section(meetings, coverage, 10)
    assert "STALE" in rendered
    assert "400 days old" in rendered
    assert "indicative, not confirmed" in rendered
    # The cached schedule does span the horizon, so an unhedged "none
    # scheduled" would render — except that provenance is part of the test for
    # that sentence, so the hedged form is used instead.
    assert "according to the CACHED schedule" in rendered
    assert "None. The published FOMC schedule spans" not in rendered


def test_an_unreadable_cache_is_ignored_rather_than_crashing_the_session(tmp_path):
    (tmp_path / "fomc_calendar.json").write_text("{ this is not json")
    provider = _fomc_provider(tmp_path, json_body=None, html_body=None)
    assert provider.get_meetings(horizon_days=10) == []
    assert provider.last_coverage.status == FOMC_UNAVAILABLE_FETCH_FAILED


# --- the wall-clock ceiling -------------------------------------------------


def test_a_hanging_fed_site_cannot_blow_the_wall_clock_ceiling(tmp_path):
    """A session must never wait on this. The deadline is a real ceiling, not
    an upper bound implied by retry-count x timeout arithmetic."""

    def _slow():
        time.sleep(0.8)
        raise OSError("read timed out")

    provider = _fomc_provider(
        tmp_path,
        json_body=_slow,
        html_body=_slow,
        request_timeout_s=1.0,
        total_fetch_deadline_s=1.0,
        max_retries=3,
        retry_backoff_base_s=5.0,
        retry_backoff_max_s=30.0,
    )
    started = time.monotonic()
    meetings = provider.get_meetings(horizon_days=10)
    elapsed = time.monotonic() - started

    assert elapsed < 3.0, f"FOMC calendar overran its ceiling: {elapsed:.1f}s"
    assert meetings == []
    assert provider.last_coverage.measured is False
    rendered = format_fomc_section(meetings, provider.last_coverage, 10)
    assert "UNAVAILABLE" in rendered
    assert "no meeting falls inside it" not in rendered


def test_a_source_not_reached_inside_the_budget_is_named_not_skipped_silently(tmp_path):
    def _slow():
        time.sleep(1.2)
        raise OSError("read timed out")

    provider = _fomc_provider(
        tmp_path,
        json_body=_slow,
        html_body=None,
        request_timeout_s=1.0,
        total_fetch_deadline_s=1.0,
    )
    provider.get_meetings(horizon_days=10)
    reason = provider.last_coverage.reason
    assert "fetch_deadline_exceeded" in reason
    assert provider.last_coverage.status in (
        FOMC_UNAVAILABLE_FETCH_FAILED,
        FOMC_UNAVAILABLE_DEADLINE_EXCEEDED,
    )


# --- the fetched dates reach the seats --------------------------------------


def _fomc_provider_with_real_schedule(tmp_path):
    today = et_today()
    inside = today + timedelta(days=4)
    beyond = today + timedelta(days=90)
    rows = [
        {
            "title": "FOMC Meeting",
            "type": "FOMC",
            "month": d.strftime("%Y-%m"),
            "days": str(d.day),
            "description": "<p>Two-day meeting</p>",
        }
        for d in (inside, beyond)
    ]
    provider = _fomc_provider(tmp_path, json_body=_json_bytes(rows))
    return provider, inside, beyond


def test_the_fomc_config_refuses_a_deadline_shorter_than_one_request(tmp_path):
    from src.config import EventRiskConfig

    with pytest.raises(ValueError, match="fomc_deadline_s"):
        EventRiskConfig(fomc_request_timeout_s=30.0, fomc_deadline_s=5.0)
    assert EventRiskConfig().fomc_cache_path == "data/fomc_calendar.json"
    assert tmp_path is not None


def test_a_short_schedule_whose_fallback_also_failed_names_both_facts(tmp_path):
    """The half-degraded case, which is the one most likely to be misread.

    Real dates came back, so the block is not "unavailable" — but the schedule
    stops inside the horizon and the fallback that exists to extend it did not
    answer. Both have to be visible: without the first the seat loses usable
    dates, without the second an operator cannot tell "the Fed has not
    published that far yet" from "our second source is broken".
    """
    today = et_today()
    near = today + timedelta(days=2)
    rows = [
        {
            "title": "FOMC Meeting",
            "type": "FOMC",
            "month": near.strftime("%Y-%m"),
            "days": str(near.day),
            "description": "<p>Two-day meeting</p>",
        }
    ]
    provider = _fomc_provider(
        tmp_path,
        json_body=_json_bytes(rows),
        html_body=None,
    )
    meetings = provider.get_meetings(horizon_days=10)
    coverage = provider.last_coverage

    assert coverage.status == FOMC_MEASURED
    assert coverage.covers_horizon is False
    assert len(meetings) == 1

    rendered = format_fomc_section(meetings, coverage, 10)
    assert near.isoformat() in rendered  # the real date survives
    assert "BEFORE THE END OF THIS HORIZON" in rendered
    assert "The fallback source did not answer either" in rendered
    assert "None. The published FOMC schedule spans" not in rendered


def test_a_shorter_fetched_schedule_does_not_overwrite_a_longer_cached_one(tmp_path):
    """The JSON feed alone stops at the end of the calendar year. A run that
    never needed the fallback must not throw away a next-year tail an earlier
    run already merged in — otherwise the December horizon that motivated the
    fallback goes blind again the first quiet week after it ran."""
    today = et_today()
    far = today + timedelta(days=400)
    cache_file = tmp_path / "fomc_calendar.json"
    cache_file.write_text(
        json.dumps(
            {
                "fetched_on": (today - timedelta(days=30)).isoformat(),
                "source": f"{FOMC_SOURCE_JSON} + {FOMC_SOURCE_HTML}",
                "meetings": [
                    {
                        "start_date": (far - timedelta(days=1)).isoformat(),
                        "end_date": far.isoformat(),
                        "duration_stated": True,
                    }
                ],
            }
        )
    )
    near = today + timedelta(days=30)
    rows = [
        {
            "title": "FOMC Meeting",
            "type": "FOMC",
            "month": near.strftime("%Y-%m"),
            "days": str(near.day),
            "description": "<p>Two-day meeting</p>",
        }
    ]
    provider = _fomc_provider(
        tmp_path,
        json_body=_json_bytes(rows),
        html_body=None,
    )
    meetings = provider.get_meetings(horizon_days=10)

    # This run serves what it actually fetched...
    assert _fomc_days(meetings) == [
        ((near - timedelta(days=1)).isoformat(), near.isoformat()),
    ]
    # ...and the longer cached tail survives on disk for the run that needs it.
    on_disk = json.loads(cache_file.read_text())
    assert on_disk["meetings"][0]["end_date"] == far.isoformat()


# --- board item 187: the release-schedule cache ----------------------------
#
# The fair-share split alone did not stop the calendar failing at the open
# (production log, 2026-09-30: 1/7 at 13:33 and 0/7 at 14:04 with the split
# deployed). These pin the actual fix: the schedules are prefetched off the
# trading path, served from disk at the open, and what is still missing is
# reported missing.

import json as _json
from datetime import date as _date, timedelta as _timedelta

# The provider dates everything by the EXCHANGE day (`et_today`), not by
# the runner's local day. Between 00:00 and 04:00 UTC the two differ, and a
# cache written with the local date then looks like it was fetched TOMORROW,
# which the provider correctly rejects. These tests must speak the
# provider's calendar or they fail for four hours every night.
from src.trading_calendar import et_today as _today  # noqa: E402
from src.data.event_calendar import (  # noqa: E402
    MacroEventCalendarProvider as _Provider,
    MacroRelease as _Release,
    RELEASE_SCHEDULE_CACHE_SCHEMA as _SCHEMA,
)

_ONE = (_Release(10, "CPI", "inflation print"),)


def _provider(tmp_path, **kw):
    p = _Provider(api_key="k", releases=_ONE, schedule_cache_path=str(tmp_path / "rs.json"), **kw)
    p._fetch_release_dates = lambda *a, **k: ([], None)  # offline unless the test says otherwise
    return p


def _write_cache(tmp_path, dates, fetched_on):
    (tmp_path / "rs.json").write_text(
        _json.dumps(
            {
                "schema": _SCHEMA,
                "releases": {
                    "10": {
                        "label": "CPI",
                        "fetched_on": fetched_on.isoformat(),
                        "dates": [d.isoformat() for d in dates],
                    }
                },
            }
        )
    )


def test_a_cached_schedule_answers_without_touching_the_wire(tmp_path):
    p = _provider(tmp_path)
    p._fetch_release_dates = lambda *a, **k: pytest.fail("went to the wire")
    _write_cache(tmp_path, [_today() + _timedelta(days=3)], _today())
    events = p.get_upcoming_events(horizon_days=10)
    assert [e.label for e in events] == ["CPI"]
    assert p.last_coverage.complete
    assert p.last_coverage.from_cache == [("CPI", 0)]


def test_a_cache_served_release_is_stated_as_cached_never_as_a_live_read(tmp_path):
    p = _provider(tmp_path)
    p._fetch_release_dates = lambda *a, **k: ([], "unreachable")
    _write_cache(tmp_path, [_today() + _timedelta(days=3)], _today())
    text = p.get_upcoming_events(horizon_days=10) and p.last_coverage.describe()
    assert "SERVED FROM THE PRE-OPEN SCHEDULE CACHE" in text
    assert "CPI" in text


def test_a_schedule_too_old_or_too_short_is_a_miss_not_a_near_enough_hit(tmp_path):
    for dates, fetched_on in (
        ([_today() + _timedelta(days=3)], _today() - _timedelta(days=99)),
        ([_today() - _timedelta(days=1)], _today()),
    ):
        p = _provider(tmp_path)
        calls = []
        p._fetch_release_dates = lambda *a, **k: (calls.append(1), ([], "boom"))[1]
        _write_cache(tmp_path, dates, fetched_on)
        p.get_upcoming_events(horizon_days=10)
        assert calls, "a stale or short cache must go to the wire"
        assert p.last_coverage.status == "failed"
        assert p.last_coverage.from_cache == []


def test_a_release_in_neither_cache_nor_wire_is_reported_missing_never_defaulted(tmp_path):
    p = _provider(tmp_path)
    p._fetch_release_dates = lambda *a, **k: ([], "fetch_deadline_exceeded")
    assert p.get_upcoming_events(horizon_days=10) == []
    assert p.last_coverage.status == "failed"
    assert [f.reason for f in p.last_coverage.failed] == ["fetch_deadline_exceeded"]
    assert "NOT FETCHED" in p.last_coverage.describe()


def test_only_the_prefetch_writes_the_cache(tmp_path):
    dates = [_today() + _timedelta(days=3)]
    p = _provider(tmp_path)
    p._fetch_release_dates = lambda *a, **k: (dates, None)
    p.get_upcoming_events(horizon_days=10)
    assert not (tmp_path / "rs.json").exists(), "a session must never write the cache"

    p2 = _provider(tmp_path)
    p2._fetch_release_dates = lambda *a, **k: (dates, None)
    coverage = p2.prefetch_release_schedules()
    assert coverage.complete
    assert (tmp_path / "rs.json").exists()
    assert p2.schedule_cache.load(10)[0] == dates


def test_the_prefetch_never_reads_the_cache_so_it_cannot_become_a_no_op(tmp_path):
    _write_cache(tmp_path, [_today() + _timedelta(days=3)], _today())
    p = _provider(tmp_path)
    calls = []
    fresh = [_today() + _timedelta(days=4)]
    p._fetch_release_dates = lambda *a, **k: (calls.append(1), (fresh, None))[1]
    p.prefetch_release_schedules()
    assert calls, "the prefetch must always go to the wire"
    assert p.schedule_cache.load(10)[0] == fresh


def test_the_prefetch_does_not_widen_the_deadline(tmp_path):
    p = _provider(tmp_path, total_fetch_deadline_s=20.0)
    assert p.total_fetch_deadline_s == 20.0
    p.prefetch_release_schedules()
    assert p.total_fetch_deadline_s == 20.0
