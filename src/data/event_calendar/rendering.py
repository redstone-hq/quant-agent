"""Prompt-section rendering of the event calendar for the seats."""

from datetime import timedelta

from src.data.event_calendar.earnings import EarningsProximity
from src.data.event_calendar.fomc import FOMC_MEASURED, FOMCCoverage, FOMCMeeting
from src.data.event_calendar.macro import UNCOVERED_EVENTS
from src.data.event_calendar.macro_types import EventCalendarCoverage, MacroEvent
from src.trading_calendar import et_today

# --- rendering -------------------------------------------------------------


def format_fomc_section(
    meetings: list[FOMCMeeting] | None,
    coverage: FOMCCoverage | None,
    horizon_days: int,
    *,
    heading: str = "### FOMC meetings — FETCHED from the Federal Reserve",
) -> str:
    """The FOMC half of the event calendar, rendered once for every seat.

    Like `format_macro_events_section`, there is no branch here that renders as
    silence, and exactly one branch is allowed to say the reassuring thing —
    "no meeting inside this window" — which requires a published schedule that
    demonstrably spans the whole window (`FOMCCoverage.covers_horizon`). Every
    other path says UNKNOWN and says why.
    """
    horizon_days = max(0, int(horizon_days))
    today = et_today()
    horizon_end = today + timedelta(days=horizon_days)
    lines = [heading, ""]

    if coverage is None:
        lines.append(
            "- NOT FETCHED this run — the FOMC meeting calendar was not "
            "consulted. Treat the FOMC schedule as UNKNOWN and say so; do not "
            "supply a meeting date from memory."
        )
        return "\n".join(lines) + "\n"

    if not coverage.measured:
        lines.append(f"- {coverage.describe()}")
        return "\n".join(lines) + "\n"

    forward = [m for m in (meetings or []) if m.end_date >= today]
    inside = [m for m in forward if m.intersects(today, horizon_end)]

    if inside:
        lines.extend(f"- {m.describe(today, horizon_end)}" for m in inside)
    elif coverage.covers_horizon and coverage.status == FOMC_MEASURED:
        # The ONLY place this block is permitted to be reassuring, and the
        # provenance test is part of the condition: a schedule that spans the
        # horizon but came off a stale cache gets the hedged wording below
        # instead, because "nothing is coming" is a stronger claim than a
        # cached copy of a published schedule can carry on its own.
        lines.append(
            f"- None. The published FOMC schedule spans the next "
            f"{horizon_days} calendar days and no meeting falls inside it."
        )
    elif coverage.covers_horizon:
        lines.append(
            f"- None inside the next {horizon_days} calendar days according to "
            f"the CACHED schedule — read the coverage line below before "
            f"treating that as settled."
        )
    else:
        lines.append(
            "- None inside this horizon — but read the coverage line below: "
            "the published schedule does not reach the end of the horizon, so "
            "this is NOT a confirmed empty window."
        )

    upcoming = [m for m in forward if m not in inside]
    if upcoming:
        lines.append(f"- Next scheduled meeting beyond this horizon: {upcoming[0].describe(today)}")
    lines.append(f"- {coverage.describe()}")
    return "\n".join(lines) + "\n"


def format_macro_events_section(
    events: list[MacroEvent] | None,
    coverage: EventCalendarCoverage | None,
    horizon_days: int,
    *,
    heading: str = "## Scheduled Macro Releases — FETCHED (do NOT answer from memory)",
    fomc_meetings: list[FOMCMeeting] | None = None,
    fomc_coverage: FOMCCoverage | None = None,
) -> str:
    """The macro half of the event calendar, rendered once for every seat.

    There is no branch here that renders as silence. A seat shown an empty
    section reads it as "nothing to worry about", and an empty calendar and an
    unfetched calendar look identical unless something says which is which —
    that confusion is the fabrication this module exists to prevent.
    """
    lines = [heading, ""]
    if coverage is None:
        lines.append(
            "- NOT FETCHED this run — the macro event calendar was not "
            "consulted. Treat every scheduled release date as UNKNOWN and say "
            "so; do not substitute a date you recall."
        )
    else:
        if events:
            lines.extend(f"- {e.describe()}" for e in events)
        elif coverage.status == "ok":
            lines.append(
                f"- None. All {coverage.configured} tracked release schedules "
                f"fetched successfully and none lands inside the next "
                f"{horizon_days} calendar days."
            )
        else:
            lines.append(
                "- None returned — but read the coverage line below: this "
                "calendar is impaired, so an empty list here does NOT mean an "
                "empty calendar."
            )
        # Releases that ARE covered but whose next date sits past the horizon.
        # Rendered separately and explicitly NOT as event risk, so the seat can
        # name the date without treating it as imminent.
        if coverage.next_beyond_horizon:
            lines.append(
                f"- Beyond the next {horizon_days} calendar days — scheduled, "
                f"fetched, NOT imminent (do not treat these as event risk for "
                f"this session):"
            )
            lines.extend(
                f"  - {e.event_date.isoformat()} (in {e.days_away} calendar days): {e.label}"
                for e in coverage.next_beyond_horizon
            )
        lines.append(f"- {coverage.describe()}")

    lines.append("")
    # The FOMC schedule rides inside this section rather than beside it, so
    # both seats read one identically-worded calendar — the same reason
    # `format_event_risk_block` delegates here instead of re-rendering.
    # No horizon is quoted when nothing was fetched, for the same reason the
    # macro heading above drops it: "the next 0 calendar days" would be a
    # number describing a window that was never queried.
    fomc_heading = (
        "### FOMC meetings — NOT FETCHED"
        if fomc_coverage is None
        else (
            f"### FOMC meetings, next {horizon_days} calendar days — FETCHED "
            f"from the Federal Reserve (do NOT answer from memory)"
        )
    )
    lines.append(
        format_fomc_section(
            fomc_meetings,
            fomc_coverage,
            horizon_days,
            heading=fomc_heading,
        ).rstrip("\n")
    )

    if UNCOVERED_EVENTS:
        lines.append("")
        lines.append("Not covered by this calendar:")
        lines.extend(f"- {item}" for item in UNCOVERED_EVENTS)
    return "\n".join(lines) + "\n"


def format_event_risk_block(
    *,
    earnings: list[EarningsProximity] | None,
    events: list[MacroEvent] | None,
    coverage: EventCalendarCoverage | None,
    horizon_days: int,
    fomc_meetings: list[FOMCMeeting] | None = None,
    fomc_coverage: FOMCCoverage | None = None,
) -> str:
    """The Event Risk section the Risk Manager reads instead of its memory.

    Earnings proximity per symbol under review, then the shared macro-release
    section. Same absence discipline throughout.
    """
    lines = [
        "## Event Risk — FETCHED DATA (do NOT answer this from memory)",
        "",
        "Everything below was fetched this run. Where a line says UNAVAILABLE, "
        "UNKNOWN or NOT COVERED, that is the answer — say so plainly in "
        "`reasoning_chain.event_risk` rather than substituting a date you "
        "recall. A remembered earnings or release date is a fabricated figure.",
        "",
        "### Next scheduled earnings, per symbol under review",
    ]
    if earnings:
        lines.extend(f"- {e.describe()}" for e in earnings)
        unknown = [e.symbol for e in earnings if not e.measured]
        if unknown:
            lines.append(
                f"  ⚠️ Earnings proximity is UNKNOWN for: {', '.join(unknown)}. "
                f"Say so and name it. Earnings never resize or block a trade, "
                f"known date or not."
            )
    else:
        lines.append(
            "- NOT FETCHED this run — no earnings proximity is available for "
            "any symbol under review. This is an absence of data, NOT a "
            "confirmation that no name reports soon."
        )

    lines.append("")
    # No horizon is quoted when nothing was fetched — "the next 0 calendar
    # days" would be a number describing a window that was never queried.
    heading = (
        "### Scheduled US macro releases"
        if coverage is None
        else f"### Scheduled US macro releases, next {horizon_days} calendar days"
    )
    return (
        "\n".join(lines)
        + "\n"
        + format_macro_events_section(
            events,
            coverage,
            horizon_days,
            heading=heading,
            fomc_meetings=fomc_meetings,
            fomc_coverage=fomc_coverage,
        )
    )
