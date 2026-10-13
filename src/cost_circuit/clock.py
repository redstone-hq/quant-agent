"""src.cost_circuit.clock -- ET-day arithmetic over the desk's one clock.

The cost circuit has no clock of its own. Every instant it reads comes from
``src.trading_calendar.utc_now`` (looked up at call time, so freezing the desk
clock freezes this package), and every stamp it stores or compares is bound
from ``_sql_stamp`` -- the one clock in SQLite's stored text. No table has a
time DEFAULT and no SQL reads ``'now'``, so there is no second clock left for
a test to pin.
"""

from __future__ import annotations
from datetime import datetime, time as dt_time, timezone
from zoneinfo import ZoneInfo

from src import trading_calendar as _desk_clock
from src.trading_calendar import sql_utc_stamp_ago


_ET = ZoneInfo("America/New_York")


def _sql_stamp(*, minutes_ago: float = 0.0) -> str:
    """The desk clock, ``minutes_ago`` back, as the text SQLite stores.

    The parts bind this where their SQL used to read ``datetime('now')`` or
    rely on a column default, so a frozen desk clock freezes every stamp the
    circuit writes or compares.
    """
    return sql_utc_stamp_ago(minutes=minutes_ago)


def _et_day_and_utc_bounds(now: datetime | None = None) -> tuple[str, str, str]:
    now = now or _desk_clock.utc_now()
    local = now.astimezone(_ET)
    day = local.date()
    start = datetime.combine(day, dt_time.min, tzinfo=_ET).astimezone(timezone.utc)
    end = datetime.combine(day, dt_time.max, tzinfo=_ET).astimezone(timezone.utc)
    # SQLite timestamps in this project use UTC without an offset.
    return (
        day.isoformat(),
        start.strftime("%Y-%m-%d %H:%M:%S"),
        end.strftime("%Y-%m-%d %H:%M:%S"),
    )


def _et_day_from_sqlite_utc(stamp: str) -> str | None:
    """The ET day a stored UTC timestamp falls on, or None.

    SQLite stamps are UTC without an offset. A latch stamped at 23:00 ET is
    already the next UTC day, and the accounting is keyed on ET days, so
    the conversion has to be explicit rather than a string prefix.
    """
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            parsed = datetime.strptime(stamp.strip()[:19], fmt)
        except ValueError:
            continue
        return parsed.replace(tzinfo=timezone.utc).astimezone(_ET).date().isoformat()
    return None


def _legacy_mode(run_id: str) -> str:
    if run_id.startswith("intra_check-"):
        return "intra_check"
    if run_id.startswith("midday-"):
        return "midday"
    if run_id.startswith("close-"):
        return "close"
    if run_id.startswith("evening-"):
        return "evening"
    if run_id.startswith("earnings-"):
        return "earnings_preprocess"
    if run_id.startswith("meta-"):
        return "meta"
    return "morning"
