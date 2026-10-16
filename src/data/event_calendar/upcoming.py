"""One shared fetch of the forward macro-release and FOMC calendars.

The morning research stage and the intraday scan both need this block; it used
to live inline in the morning only, so every intraday risk prompt said the
calendar was NOT FETCHED. Both now call `fetch_upcoming`.

Each calendar degrades to ([], None) on any error, reported through `on_fault`
(the morning passes `record_morning_fault`); a failure never propagates.
"""

from __future__ import annotations

from collections.abc import Callable


def fetch_upcoming(
    event_calendar,
    fomc_calendar,
    horizon_days: int,
    on_fault: Callable[[str, BaseException], None],
) -> tuple[list, object | None, list, object | None]:
    """Return (macro_events, event_coverage, fomc_meetings, fomc_coverage)."""
    macro_events: list = []
    event_coverage = None
    if event_calendar is not None:
        try:
            macro_events = event_calendar.get_upcoming_events(horizon_days=horizon_days)
            event_coverage = event_calendar.last_coverage
        except Exception as e:  # noqa: BLE001 — degrades to a named absence, fault recorded
            on_fault("event_calendar", e)
            macro_events, event_coverage = [], None
    fomc_meetings: list = []
    fomc_coverage = None
    if fomc_calendar is not None:
        try:
            fomc_meetings = fomc_calendar.get_meetings(horizon_days=horizon_days)
            fomc_coverage = fomc_calendar.last_coverage
        except Exception as e:  # noqa: BLE001
            on_fault("fomc_calendar", e)
            fomc_meetings, fomc_coverage = [], None
    return macro_events, event_coverage, fomc_meetings, fomc_coverage
