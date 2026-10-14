"""Session calendar — the real open/close of one trading date, and the desk's
phases resolved against those two edges.

Why this exists
---------------
A fixed minute of the Eastern clock is only correct on a normal 09:30-16:00
session. On an early close (13:00) the end-of-day review would fire after the
market had shut; on a holiday every weekday phase would fire anyway. The phase
offsets (`PHASE_OFFSETS`, defined in `src/trading_calendar.py`, re-exported
here) state every phase as minutes from that day's real open or close, and this
module resolves them for one date from the broker's exchange calendar.

What trunk does with it
-----------------------
`scripts/run_if_et_window.sh` runs `python -m src.session_calendar <mode>
<ET date>` once per tick and fires the phase only inside the window it prints:
`LO HI` (ET minutes of day) or `closed` on a holiday. Examples for Fri 27 Nov
2026 (09:30-13:00): midday 600-690 (10:00-11:30), close 750-780 (12:30-13:00).
Only the first tick of a day asks the broker; the answer is kept in
`<last-run dir>/session-<date>` and every later tick, of every mode, reads that
file. An outage is never cached.

When the calendar cannot be read the command exits 3 and prints the NORMAL-DAY
window for the mode (derived from the same offsets at 09:30/16:00) and the wrapper
logs that it used it. That is today's behaviour, wrong only on a half day with the
broker down; the desk trades paper money and does not add a refuse-to-act failure
mode for it. `SessionCalendarUnavailable` is still raised by `session_for` /
`phase_window` for any in-process caller that wants to decide for itself.

In-process readers of the fixed `SESSION_WINDOWS` table are NOT switched by this
module; the list and the reasoning are in the `src/trading_calendar.py` docstring.

Two properties matter more than anything else here:

1.  It FAILS LOUDLY when the exchange calendar cannot be read. The broker's
    `is_trading_day` now raises on failure, but the two edge reads this module
    needs still log and return None. At that edge surface, "today is a holiday"
    and "Alpaca did not answer" are therefore the same answer. Quietly treating
    an outage as a holiday, or worse as a normal 09:30-16:00 session,
    reintroduces exactly the bug this is here to remove, invisibly.
    `SessionCalendarUnavailable` is raised instead; a caller may catch it and
    decide, but no caller is handed a guess dressed as a fact.

2.  No holiday or early-close date is written down. A hardcoded 2026 holiday
    table is the same time bomb in a new costume: correct until the year
    turns, then silently wrong. Every date-specific fact comes from the
    exchange calendar the broker publishes.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Callable
from enum import IntEnum
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from src.trading_calendar import (
    ET,
    PHASE_OFFSETS,
    REGULAR_SESSION_CLOSE_MIN,
    REGULAR_SESSION_OPEN_MIN,
    SESSION_WINDOWS,
    window_minutes,
)


class SessionCalendarUnavailable(RuntimeError):
    """The exchange calendar could not be read, so the session is UNKNOWN.

    Deliberately distinct from "this date is not a trading day". A caller that
    cannot tell those two apart will eventually treat an outage as a holiday
    (and skip real work) or as a normal session (and trade into a shut
    market). Both have happened; neither is allowed to happen quietly.
    """


@dataclass(frozen=True)
class Session:
    """One date's real session: both edges, or a flag saying there is none."""

    on_date: date
    is_trading_day: bool
    open_at: datetime | None
    close_at: datetime | None

    def require_edges(self) -> tuple[datetime, datetime]:
        """Both edges, or a loud refusal. Never a guessed 09:30-16:00."""
        if not self.is_trading_day or self.open_at is None or self.close_at is None:
            raise SessionCalendarUnavailable(f"{self.on_date} is not a trading day, so it has no session edges")
        return self.open_at, self.close_at


def _edges(broker: Any, on_date: date) -> tuple[datetime | None, datetime | None]:
    """Both edges for one date, straight from the broker's cached calendar.

    An exception out of the broker is NOT swallowed here. The broker's own
    wrappers already catch their Alpaca errors and return None, so anything
    escaping is a programming fault in the reader and must be seen.
    """
    return (broker.get_session_open(on_date), broker.get_session_close(on_date))


def _calendar_is_answering(broker: Any, around: date) -> bool:
    """Is the exchange calendar readable at all right now?

    Asked only when the target date reads blank, to tell a genuine holiday
    from an outage — the broker cannot tell them apart for us, since both come
    back as None. Walks backwards day by day and stops at the first date that
    produces a real session; those reads are cached per date on the broker, so
    the walk costs nothing once the day is warm.
    """
    # The look-back is bounded by calendar structure, not a count: from the
    # day before `around` back to the first day of the previous calendar month.
    # An exchange always trades on some date in a whole month, so a span that
    # long returning nothing means the calendar is not answering.
    floor = (around.replace(day=1) - timedelta(days=1)).replace(day=1)
    probe = around - timedelta(days=1)
    while probe >= floor:
        opens, closes = _edges(broker, probe)
        if opens is not None and closes is not None:
            return True
        probe -= timedelta(days=1)
    return False


def session_for(broker: Any, on_date: date) -> Session:
    """The real session for `on_date`, or a loud failure.

    Returns a `Session` with `is_trading_day=False` and no edges when the
    exchange genuinely does not trade that date (weekend, holiday). Raises
    `SessionCalendarUnavailable` when the calendar cannot be read, which is a
    different thing and must never be mistaken for the first.
    """
    if broker is None:
        raise SessionCalendarUnavailable(
            f"no broker supplied, so the exchange calendar for {on_date} "
            "cannot be read; refusing to assume a normal session"
        )
    opens, closes = _edges(broker, on_date)
    if opens is not None and closes is not None:
        return Session(on_date=on_date, is_trading_day=True, open_at=opens, close_at=closes)
    if opens is not None or closes is not None:
        # Half an answer is not an answer. One edge present and the other
        # missing means the calendar entry is malformed or the second lookup
        # failed; filling the gap in with a standard 09:30 or 16:00 is the
        # precise mistake this module exists to stop.
        raise SessionCalendarUnavailable(
            f"the exchange calendar gave only one edge for {on_date} "
            f"(open={opens!r}, close={closes!r}); refusing to invent the other"
        )
    if _calendar_is_answering(broker, on_date):
        return Session(on_date=on_date, is_trading_day=False, open_at=None, close_at=None)
    raise SessionCalendarUnavailable(
        f"the exchange calendar returned nothing for {on_date} and nothing for "
        "any day back to the start of the previous month, so it is unreadable "
        "rather than closed; refusing to assume a normal session"
    )


def phase_window(broker: Any, on_date: date, phase: str) -> tuple[datetime, datetime]:
    """Resolve one phase to real wall-clock instants on `on_date`.

    Raises `SessionCalendarUnavailable` when the date does not trade or the
    calendar is unreadable, so a caller cannot accidentally receive a window
    on a day the market never opened.
    """
    offset = PHASE_OFFSETS.get(phase)
    if offset is None:
        raise KeyError(f"unknown session phase {phase!r}")
    opens, closes = session_for(broker, on_date).require_edges()
    anchors = {"open": opens, "close": closes}
    return (
        anchors[offset.start_anchor] + timedelta(minutes=offset.start_delta_minutes),
        anchors[offset.end_anchor] + timedelta(minutes=offset.end_delta_minutes),
    )


def covered_phases() -> frozenset[str]:
    """The phases this module can resolve. Exactly the Python minute table's
    keys today; the shell wrapper's extra eighth mode is a later piece."""
    return frozenset(PHASE_OFFSETS) & frozenset(SESSION_WINDOWS)


def et_datetime(on_date: date, minute_of_day: int) -> datetime:
    """An ET-aware instant `minute_of_day` minutes into `on_date`. Test and
    caller convenience so neither hand-rolls the timezone attachment."""
    return datetime(on_date.year, on_date.month, on_date.day, tzinfo=ET) + timedelta(minutes=minute_of_day)


# --- the command the shell wrapper runs once per tick ------------------------


class ExitStatus(IntEnum):
    """What the command's exit status says (the wrapper reads these)."""

    ANSWERED = 0
    USAGE_ERROR = 2
    #: The exchange calendar could not be read. The command still prints the
    #: normal-day window on stdout so the caller needs no numbers of its own;
    #: the reason goes to stderr.
    CALENDAR_UNAVAILABLE = 3


#: `intra_safety` is the free half of `intra_check`'s preamble on its own unit:
#: the same work over the same hours BY CONSTRUCTION, so it takes intra_check's
#: offsets. It is deliberately not a `PHASE_OFFSETS` / `SESSION_WINDOWS` key,
#: because that table feeds the expected PAID-session count and this mode can
#: never spend.
_MODE_ALIASES = {"intra_safety": "intra_check"}

_CACHE_PREFIX = "session-"
_CLOSED = "closed"


def _cache_file(cache_dir: str, on_date: date) -> Path:
    return Path(cache_dir) / f"{_CACHE_PREFIX}{on_date.isoformat()}"


def _read_cache(path: Path) -> tuple[int, int] | str | None:
    """The day's edges as (open_min, close_min), "closed", or None when there
    is no usable file. A corrupt file is a miss, never an answer."""
    try:
        text = path.read_text().strip()
    except OSError:
        return None
    if text == _CLOSED:
        return _CLOSED
    parts = text.split()
    if len(parts) == 2 and all(p.isdigit() for p in parts):
        return int(parts[0]), int(parts[1])
    return None


def _write_cache(path: Path, edges: tuple[int, int] | None) -> None:
    """Keep the day's answer for the later ticks, and drop earlier days' files.

    Written then renamed, so a tick that races another never reads half a file.
    A failure to write costs one more broker read on the next tick, nothing
    else, so it is not allowed to fail the command.
    """
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        tmp.write_text(_CLOSED + "\n" if edges is None else f"{edges[0]} {edges[1]}\n")
        os.replace(tmp, path)
        for old in path.parent.glob(f"{_CACHE_PREFIX}????-??-??"):
            if old.name < path.name:
                old.unlink(missing_ok=True)
    except OSError:
        pass


def _minute_of_day(moment: datetime) -> int:
    local = moment.astimezone(ET)
    return local.hour * 60 + local.minute


def _build_broker() -> Any:
    """The read-only broker handle the API's broker reads already build, from the
    narrow credential accessors. `src.execution` is the broker seam and the set of
    modules importing it must not widen, so this module reaches it through the
    one listed importer rather than adding a second."""
    from src.api.broker_reads import _get_broker

    return _get_broker()


def _one_line(exc: BaseException) -> str:
    return " ".join(f"{type(exc).__name__}: {exc}".split())


def main(argv: list[str] | None = None, *, broker_factory: Callable[[], Any] | None = None) -> int:
    """Print `LO HI` (ET minutes of day) for the mode on that date, or `closed`.

    Exit 0 on an answer, 2 on a usage error, `ExitStatus.CALENDAR_UNAVAILABLE` when
    the calendar cannot be read (stdout is then the normal-day window).
    """
    parser = argparse.ArgumentParser(prog="python -m src.session_calendar", description=(main.__doc__ or "").strip())
    parser.add_argument("mode", help="a desk phase, e.g. midday")
    parser.add_argument("date", help="the ET session date, YYYY-MM-DD")
    parser.add_argument("--cache-dir", help="directory keeping one answer file per date; absent means no cache")
    parser.add_argument("--normal-day", action="store_true", help="print the normal-day window; no broker, no cache")
    args = parser.parse_args(argv)

    offset = PHASE_OFFSETS.get(_MODE_ALIASES.get(args.mode, args.mode))
    if offset is None:
        print(f"unknown mode: {args.mode}", file=sys.stderr)
        return 2
    try:
        on_date = date.fromisoformat(args.date)
    except ValueError:
        print(f"bad date: {args.date!r}", file=sys.stderr)
        return 2

    normal_lo, normal_hi = window_minutes(offset, REGULAR_SESSION_OPEN_MIN, REGULAR_SESSION_CLOSE_MIN)
    if args.normal_day:
        print(f"{normal_lo} {normal_hi}")
        return 0

    cache_path = _cache_file(args.cache_dir, on_date) if args.cache_dir else None
    cached = _read_cache(cache_path) if cache_path else None
    if cached is not None:
        edges = None if cached == _CLOSED else cached
    else:
        try:
            session = session_for((broker_factory or _build_broker)(), on_date)
            edges = (
                (_minute_of_day(session.open_at), _minute_of_day(session.close_at))
                if session.is_trading_day and session.open_at and session.close_at
                else None
            )
        except Exception as exc:  # any failure to read the calendar is "unavailable", said out loud on stderr
            print(_one_line(exc), file=sys.stderr)
            print(f"{normal_lo} {normal_hi}")
            return ExitStatus.CALENDAR_UNAVAILABLE
        if cache_path:
            _write_cache(cache_path, edges)

    if edges is None:
        print(_CLOSED)
        return 0
    lo, hi = window_minutes(offset, *edges)
    print(f"{lo} {hi}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
