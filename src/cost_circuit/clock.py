"""src.cost_circuit.clock -- moved verbatim from src/cost_circuit.py; see the package docstring."""

from __future__ import annotations
import sqlite3
from datetime import date, datetime, time as dt_time, timedelta, timezone
from typing import Any, Callable, TypeVar
from zoneinfo import ZoneInfo

from src import trading_calendar as _desk_clock
from src.trading_calendar import sql_utc_stamp


_ET = ZoneInfo("America/New_York")


def _pinned_if_clock_replaced(conn: Any) -> Any:
    """Wrap a RAW connection when a caller has supplied a clock.

    Schema DDL is reached with connections this module did not open (the
    shared bootstrap passes its own), and `created_at TEXT NOT NULL DEFAULT
    (datetime('now'))` bakes a clock into the table at CREATE time. Left
    unwrapped, every event row is dated by the OS while the rest of the
    circuit is dated by the caller's clock, and a dedup that compares the
    two never matches. Production never takes this branch.
    """
    if not clock_replaced() or not isinstance(conn, sqlite3.Connection):
        return conn
    return _ClockPinnedConnection(conn, _now_utc().strftime("%Y-%m-%d %H:%M:%S"))


def _now_utc() -> datetime:
    """The one clock this module reads.

    PRODUCTION BEHAVIOUR IS UNCHANGED BY THIS INDIRECTION. Left alone, this
    reads the desk's one clock (`src.trading_calendar.utc_now`, looked up at
    call time so freezing the desk clock freezes this module) and `_connect` hands back the bare
    sqlite3 connection, so every SQL `'now'` still reads SQLite's own clock
    exactly as it always has -- the same OS clock, the same instant.

    WHY IT EXISTS. The circuit dates two different ways: the ET day comes
    from Python here, and `suspended_at` / `created_at` come from SQLite's
    `'now'`. In production those are the same clock and cannot disagree. In
    a test they can, because a test freezes the Python side and cannot
    freeze SQLite's -- and a few minutes after ET midnight the two land on
    different ET days, which is what reds this module's self-clear tests
    for the first quarter-hour of every ET day. Replacing this function is
    the single, caller-side way to pin BOTH at once: `_connect` notices it
    has been replaced and pins SQLite's `'now'` to the same instant.
    """
    return _desk_clock.utc_now()


#: Identity of the unreplaced clock. `_connect` compares against this rather
#: than against a flag, so there is no way to be in "pinned" mode without a
#: caller having actually supplied a clock.
_REAL_NOW_UTC = _now_utc
#: Identity of the desk's unreplaced clock, for the same reason.
_REAL_DESK_UTC_NOW = _desk_clock.utc_now


def clock_replaced() -> bool:
    """True when a test has frozen EITHER this module's clock or the desk clock it reads."""
    return _now_utc is not _REAL_NOW_UTC or _desk_clock.utc_now is not _REAL_DESK_UTC_NOW


class _ClockPinnedConnection:
    """A sqlite3 connection whose `'now'` is the caller's clock, not the OS.

    Only ever constructed when `_now_utc` has been replaced. It rewrites the
    literal `'now'` in SQL this module issues, so the ET day derived in
    Python and every timestamp written or compared in SQLite come from one
    instant. Everything else is the real connection, untouched.
    """

    def __init__(self, conn: sqlite3.Connection, stamp: str) -> None:
        self.__dict__["_conn"] = conn
        self.__dict__["_stamp"] = stamp

    def _pin(self, sql: str) -> str:
        # Both of SQLite's clocks, not just one. `'now'` is read in queries
        # and updates; `CURRENT_TIMESTAMP` is the column default that stamps
        # `created_at` on every event row. Pinning only the first leaves the
        # event log dated by the OS while everything else is dated by the
        # caller's clock -- the same two-clock split this whole mechanism
        # exists to remove.
        literal = "'" + self._stamp + "'"
        return sql.replace("'now'", literal).replace("CURRENT_TIMESTAMP", literal)

    def execute(self, sql, *args, **kwargs):
        return self._conn.execute(self._pin(sql), *args, **kwargs)

    def executemany(self, sql, *args, **kwargs):
        return self._conn.executemany(self._pin(sql), *args, **kwargs)

    def executescript(self, sql):
        return self._conn.executescript(self._pin(sql))

    def __enter__(self):
        self._conn.__enter__()
        return self

    def __exit__(self, *exc):
        return self._conn.__exit__(*exc)

    def __getattr__(self, name):
        return getattr(self._conn, name)

    def __setattr__(self, name, value):
        setattr(self._conn, name, value)


def _sql_stamp(*, minutes_ago: float = 0.0) -> str:
    """This circuit's clock, ``minutes_ago`` back, as the text SQLite stores.

    The parts bind this where their SQL used to read ``datetime('now')``, so a
    test that freezes ``_now_utc`` freezes every stamp the circuit writes or
    compares -- no SQL rewriting needed for those sites.
    """
    return sql_utc_stamp(_now_utc() - timedelta(minutes=minutes_ago))


def _et_day_and_utc_bounds(now: datetime | None = None) -> tuple[str, str, str]:
    now = now or _now_utc()
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
    """The ET day a `datetime('now')` timestamp falls on, or None.

    SQLite writes UTC without an offset. A latch stamped at 23:00 ET is
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
