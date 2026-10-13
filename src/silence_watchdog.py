"""The desk-wide silence watchdog — alerts on the ABSENCE of a session, not
a known failure inside one.

THE GAP THIS CLOSES (docs/WORK.md item 17c)
--------------------------------------------
`src/alert_watchdog.py` proves the Telegram PIPE still works. It says
nothing about whether the desk did any work: it would report the channel
perfectly healthy while the pipeline sat frozen behind a latched circuit,
a database that cannot be opened, or any other total failure — imagined or
not. On 2026-09-02 that happened live: a database fault latched paid
analysis off durably, the alert ABOUT that latch also failed to send, and
nobody was told until a person happened to look.

Every alarm this desk has ever had fires on an EVENT. This is the first one
that fires on the ABSENCE of events, which is exactly the shape that failure
took — a broken alert path cannot defeat an alarm whose trigger is silence
itself.

WHAT COUNTS AS "A SESSION HAPPENED"
------------------------------------
Not `agent_logs`: a crashed or partially-run session can leave rows there
(one LLM call succeeded before the crash), so its presence does not prove
completion.

Instead this reuses `alert_channel_checks` (owned by `src/alert_watchdog.py`)
keyed by `source`. Every one of `main.py`'s scheduled modes writes exactly
one row there from its `finally` block — reached even when the pipeline body
raised, per `main.py`'s own comment: "a watchdog that can replace the
in-flight session exception is worse than no watchdog", so this write is
attempted on every path out of a session, success or failure. It is the
closest thing this codebase has to "a run happened", and it already exists —
reusing it means this module adds no new instrumentation to the trading
path, only a new READER of state that path already produces.

`source == "heartbeat_timer"` (the daily probe in `scripts/alert_heartbeat.py`)
is deliberately excluded: it proves the pipe, not the desk, which is the
exact distinction item 17c exists to preserve. Only the six scheduled
trading modes in `src.trading_calendar.SESSION_WINDOWS` count as evidence of
life here.

DESK-WIDE, NOT PER-MODE
------------------------
The six modes are not independent processes with independent purposes to
monitor separately — they are one desk taking turns through one day. A
single mode legitimately producing nothing that window (e.g. `close` firing
outside a position-holding day) must not read as an outage while `morning`
and `intra_check` are running fine either side of it. So "silence" is
judged across ALL modes together: ANY completed session, in ANY mode, within
a scheduled window counts as evidence the desk is alive for that window.

WHY A PERSISTED "LAST KNOWN ALIVE" MARKER, NOT A FRESH DB SCAN EVERY RUN
--------------------------------------------------------------------------
The failure this watches for includes "the database cannot be opened" — the
same database this module would otherwise have to re-query in full on every
invocation to reconstruct history. Recomputing from scratch would make the
detector blind exactly when it matters most: an unreadable database would
read as "we cannot prove anything happened, so say nothing" instead of
"we cannot prove anything happened, and that is itself the finding."

So the on-box record (`data/alerting/silence_heartbeat.json`, same pattern
and same reasoning as `scripts/alert_heartbeat.py`'s on-box copy) carries
`last_known_session_at` forward across runs. Each run tries to advance it
from the database; if the database cannot be read, the marker simply does
not advance, and the elapsed-window count computed against it keeps
climbing on wall-clock time alone until either a fresh session is recorded
or the alert fires. The alert itself goes out through `send_owner_alert`,
which never touches this database.

ONE ALERT PER SILENCE EPISODE, NOT PER CHECK
----------------------------------------------
The on-box record also carries `alerted_for_baseline`: the `last_known_
session_at` value that was current when the alert last fired. A repeat run
against the same baseline does not re-alert — the operator already knows.
A NEW session recorded (baseline advances) clears it, so a second, later
silence episode alerts again. This mirrors `alert_heartbeat.py`'s
`consecutive_failures` reset-on-success shape.

THE THRESHOLD — OWNER-RATIFIED 2026-09-03
--------------------------------------------
`DEFAULT_SILENT_WINDOW_THRESHOLD = 2` — roughly one hour of total desk-wide
silence. Originally shipped as a placeholder of 6 (one full scheduled
trading day); the owner rejected that as too slow given that every silent
hour is an hour of open positions going unwatched, with real opportunity
cost. Since this check is desk-wide (every one of the six modes has to be
silent at once, not just one), even 2 consecutive elapsed windows is
already a strong signal — a single mode going quiet is normal and common,
but the whole desk going quiet across two independent scheduled slots in a
row is not something a healthy desk does. Ratified 2026-09-03; the `DECIDE
BY` line this shipped with has been removed accordingly. Still fully
parameterised (constructor / CLI argument) so it can be revisited without a
redesign if real operation shows it needs adjusting either way.

A DELIBERATELY PAUSED DESK STILL HAS TO BE REMEMBERED — ADDED 2026-09-13
--------------------------------------------------------------------------
`scripts/run_silence_heartbeat.sh` suppresses the silence check entirely
when no trading-mode timer is running, because an alarm that pages every
30 minutes about a state somebody chose on purpose is an alarm that gets
muted. That suppression is correct and is kept — but on its own it turned
"the desk is paused" into the one desk-wide condition with NO alarm behind
it at all. Every other surface is quiet too: the daily channel probe
sends-and-deletes (the owner sees nothing on a healthy run), the coverage
watchdog only speaks when a held position is short of stops, and the
alert-channel `stale` state is a Mission Control colour nobody is pushed.
A pause plus a flat book was therefore indistinguishable from a working
desk in a quiet market — which is docs/WORK.md item 11's premise exactly,
reintroduced by the guard that was meant to reduce noise.

`check_paused_desk` is the answer: while the desk is paused, say so ONCE
PER ET WEEKDAY, and only after at least one of that day's scheduled
windows has fully elapsed (so the notice lands after the desk visibly
failed to do something, not at 00:30). Both of those are read off existing
facts — `SESSION_WINDOWS` plus `SLACK_MINUTES` for the window, and the
once-every-24-hours-while-it-stays-broken ruling already applied by
`src/coverage_watchdog.py` (docs/WORK.md item 41) for the cadence. No new
threshold is introduced.

KNOWN LIMIT, STATED RATHER THAN HIDDEN: this module has no market-holiday
calendar (`src/trading_calendar.py` says holiday detection needs a live
broker connection, which a pure database reader must not take). So a
paused desk over a US market holiday produces one extra notice for that
day. One message is the whole cost, and a desk still paused on a holiday
is still a desk somebody has to remember to restart.
"""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from src.trading_calendar import utc_now
from pathlib import Path
from typing import Any

from src.trading_calendar import ET, SESSION_WINDOWS
from src.data_paths import alerting_dir, db_path

#: Same database every session and `src/alert_watchdog.py` already write to.
#: Resolved from the project root, not the working directory, for the same
#: reason `alert_watchdog.DB_PATH` is: sessions are started both by systemd
#: (which sets WorkingDirectory) and by hand from anywhere.
DB_PATH = db_path()

#: `data/` is gitignored — this record can never dirty the checkout, exactly
#: like `scripts/alert_heartbeat.py`'s `data/alerting/heartbeat.json`.
STATE_PATH = alerting_dir() / "silence_heartbeat.json"

#: Separate on-box record for the paused-desk notice. Deliberately NOT the
#: same file as the silence marker: the two are written on mutually
#: exclusive runs (the wrapper either finds the desk paused or it does
#: not), and sharing one file would mean a pause episode could silently
#: overwrite the `last_known_session_at` the silence check depends on.
PAUSED_STATE_PATH = alerting_dir() / "paused_desk.json"

TABLE = "alert_channel_checks"

#: Only these count as "a scheduled session ran". `heartbeat_timer` (the
#: daily Telegram-only probe) is deliberately excluded — see module
#: docstring. Order matches `config/settings.yaml`'s `schedule:` block.
KNOWN_MODES: tuple[str, ...] = tuple(SESSION_WINDOWS.keys())

#: OWNER-RATIFIED 2026-09-03 — roughly one hour of total desk-wide silence.
#: See module docstring for the reasoning.
DEFAULT_SILENT_WINDOW_THRESHOLD = 2

#: How long after a window's nominal end a session is still allowed to land
#: before the window counts as fully elapsed. The production timers
#: (`scripts/systemd/quant-agent-*.timer`) tick every 30 minutes and each
#: mode self-gates to its own window inside `run_if_et_window.sh`; a session
#: can therefore legitimately land up to one tick after the window's nominal
#: close. 45 minutes covers one full missed tick plus margin without
#: reaching into the next mode's window on any of the six schedules in
#: `src.trading_calendar.SESSION_WINDOWS`. This is an operational buffer
#: derived directly from the existing timer cadence, not a risk threshold —
#: it does not require owner sign-off.
SLACK_MINUTES = 45

#: How far back a single run will walk looking for the last live window,
#: bounded so a database with no history at all (or one that has been
#: unreadable for a long time) cannot make one invocation scan forever.
#: Comfortably wider than a long weekend plus a holiday.
LOOKBACK_DAYS = 21


def _utc_now() -> datetime:
    """Seam for tests — real code never patches `datetime` itself."""
    return utc_now()


@dataclass(frozen=True)
class ScheduledWindow:
    mode: str
    date: str  # ET calendar date, YYYY-MM-DD
    start_utc: datetime
    end_utc: datetime

    @property
    def slack_end_utc(self) -> datetime:
        return self.end_utc + timedelta(minutes=SLACK_MINUTES)


@dataclass(frozen=True)
class SilenceStatus:
    """What this run found. `should_alert` is the only field callers need
    to act on; the rest is for the message body and for tests."""

    consecutive_silent_windows: int
    threshold: int
    last_known_session_at: str | None
    newest_elapsed_window: ScheduledWindow | None
    db_error: str | None
    already_alerted_for_this_baseline: bool

    @property
    def is_silent(self) -> bool:
        return self.consecutive_silent_windows >= self.threshold

    @property
    def should_alert(self) -> bool:
        return self.is_silent and not self.already_alerted_for_this_baseline


# ---------------------------------------------------------------------------
# scheduled windows
# ---------------------------------------------------------------------------


def _elapsed_windows(now: datetime, *, lookback_days: int = LOOKBACK_DAYS) -> list[ScheduledWindow]:
    """Every (mode, date) window, across all known modes, whose slack-padded
    end has already passed, newest first. Weekends are skipped — the
    production timers never fire a trading mode on a weekend either
    (`run_if_et_window.sh`'s own weekday short-circuit), so a weekend
    produces no expected windows and cannot manufacture a false alarm.
    """
    now_et = now.astimezone(ET)
    windows: list[ScheduledWindow] = []
    for day_offset in range(lookback_days):
        day = (now_et - timedelta(days=day_offset)).date()
        if day.weekday() >= 5:  # Sat=5, Sun=6
            continue
        midnight_et = datetime(day.year, day.month, day.day, tzinfo=ET)
        for mode, (lo, hi) in SESSION_WINDOWS.items():
            start_et = midnight_et + timedelta(minutes=lo)
            end_et = midnight_et + timedelta(minutes=hi)
            window = ScheduledWindow(
                mode=mode,
                date=day.isoformat(),
                start_utc=start_et.astimezone(timezone.utc),
                end_utc=end_et.astimezone(timezone.utc),
            )
            if window.slack_end_utc <= now:
                windows.append(window)
    windows.sort(key=lambda w: w.end_utc, reverse=True)
    return windows


# ---------------------------------------------------------------------------
# database read — advances the on-box marker, never blocks on failure
# ---------------------------------------------------------------------------


def _connect_ro(path: str) -> sqlite3.Connection:
    """Read-only by OS enforcement — mirrors `alert_watchdog._connect_ro`.
    This module must never write to the trading database."""
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=2000")
    return conn


def _latest_session_check(db_path: str | Path | None = None) -> tuple[str | None, str | None]:
    """`(checked_at, error)` of the newest row from a KNOWN mode.

    Never raises. `error` is set (and `checked_at` is None) when the
    database cannot be read at all — that is itself part of what this
    watchdog needs to know, not a reason to skip the check.
    """
    path = str(db_path) if db_path is not None else str(DB_PATH)
    try:
        conn = _connect_ro(path)
    except Exception as exc:  # noqa: BLE001
        return None, f"database unreadable: {exc}"
    try:
        placeholders = ",".join("?" for _ in KNOWN_MODES)
        row = conn.execute(
            f"SELECT checked_at FROM {TABLE} WHERE source IN ({placeholders}) ORDER BY checked_at DESC LIMIT 1",
            KNOWN_MODES,
        ).fetchone()
    except Exception as exc:  # noqa: BLE001
        return None, f"query failed (table missing on a fresh database?): {exc}"
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass
    if row is None:
        return None, None
    return str(row["checked_at"]), None


def _parse_iso(stamp: str) -> datetime | None:
    try:
        when = datetime.fromisoformat(stamp)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return when.astimezone(timezone.utc)


# ---------------------------------------------------------------------------
# on-box persisted state — carries `last_known_session_at` across runs so a
# broken database does not erase the one thing this watchdog must remember
# ---------------------------------------------------------------------------


def load_state(path: Path | None = None) -> dict[str, Any]:
    """Never raises. A corrupt or missing record starts fresh rather than
    stopping the check."""
    try:
        raw = json.loads((path or STATE_PATH).read_text())
    except (OSError, ValueError):
        raw = None
    if not isinstance(raw, dict):
        raw = {}
    raw.setdefault("last_known_session_at", None)
    raw.setdefault("alerted_for_baseline", None)
    raw.setdefault("updated_at", None)
    return raw


def save_state(state: dict[str, Any], path: Path | None = None) -> bool:
    """Atomic write (tmp + os.replace), same shape as
    `scripts/alert_heartbeat.py`'s `save_state`. Returns False rather than
    raising."""
    target = path or STATE_PATH
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(dir=str(target.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as handle:
                json.dump(state, handle, indent=2, sort_keys=True)
                handle.write("\n")
            os.replace(tmp_name, target)
        except BaseException:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise
    except OSError:
        return False
    return True


# ---------------------------------------------------------------------------
# the check itself
# ---------------------------------------------------------------------------


def check_silence(
    *,
    now: datetime | None = None,
    threshold: int = DEFAULT_SILENT_WINDOW_THRESHOLD,
    db_path: str | Path | None = None,
    state_path: Path | None = None,
) -> SilenceStatus:
    """Advance the on-box marker from the database (best-effort), then
    compute how many consecutive scheduled windows have elapsed with no
    evidence of a completed session since that marker.

    Persists the (possibly advanced) marker before returning. Never raises.
    """
    moment = now or _utc_now()
    state = load_state(state_path)

    latest_checked_at, db_error = _latest_session_check(db_path)
    if latest_checked_at is not None:
        candidate = _parse_iso(latest_checked_at)
        existing = _parse_iso(state["last_known_session_at"]) if state["last_known_session_at"] else None
        if candidate is not None and (existing is None or candidate > existing):
            state["last_known_session_at"] = candidate.replace(microsecond=0).isoformat()

    last_known_dt = _parse_iso(state["last_known_session_at"]) if state["last_known_session_at"] else None

    elapsed = _elapsed_windows(moment)
    if last_known_dt is not None:
        # A window counts as covered (not silent) once the last known
        # session's timestamp reaches or passes its START — the session
        # need not have landed inside this exact window to prove the desk
        # was still alive by the time it began. Comparing against the
        # window's END instead would wrongly flag the very window a session
        # landed inside of, whenever it landed before that window's own
        # close (e.g. a session recorded at the midpoint of its window).
        elapsed = [w for w in elapsed if w.start_utc > last_known_dt]
    consecutive_silent = len(elapsed)
    newest_elapsed = elapsed[0] if elapsed else None

    already_alerted = state.get("alerted_for_baseline") is not None and state.get("alerted_for_baseline") == state.get(
        "last_known_session_at"
    )

    status = SilenceStatus(
        consecutive_silent_windows=consecutive_silent,
        threshold=threshold,
        last_known_session_at=state["last_known_session_at"],
        newest_elapsed_window=newest_elapsed,
        db_error=db_error,
        already_alerted_for_this_baseline=already_alerted,
    )

    if status.should_alert:
        state["alerted_for_baseline"] = state["last_known_session_at"]
    state["updated_at"] = moment.replace(microsecond=0).isoformat()
    save_state(state, state_path)
    return status


# ---------------------------------------------------------------------------
# the paused-desk notice — the other half of the wrapper's pause guard
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PausedDeskStatus:
    """What a paused-desk run found. `should_notify` is the only field
    callers act on."""

    et_date: str
    elapsed_windows_today: int
    already_notified_today: bool
    is_weekday: bool

    @property
    def should_notify(self) -> bool:
        return self.is_weekday and self.elapsed_windows_today > 0 and not self.already_notified_today


def _elapsed_windows_on(now: datetime) -> int:
    """How many of TODAY's (ET) scheduled windows have already closed,
    slack included. Zero before the first one — which is why the notice
    cannot fire in the small hours of a day the desk had not yet been due
    to do anything on."""
    now_et = now.astimezone(ET)
    day = now_et.date()
    midnight_et = datetime(day.year, day.month, day.day, tzinfo=ET)
    count = 0
    for _mode, (_lo, hi) in SESSION_WINDOWS.items():
        end_et = midnight_et + timedelta(minutes=hi)
        if end_et.astimezone(timezone.utc) + timedelta(minutes=SLACK_MINUTES) <= now:
            count += 1
    return count


def check_paused_desk(
    *,
    now: datetime | None = None,
    state_path: Path | None = None,
) -> PausedDeskStatus:
    """Decide whether to remind the owner that the desk is deliberately
    paused, and record that decision. Never raises.

    The caller is responsible for having established that the desk IS
    paused — that is a deployment fact (`systemctl --user is-active`), and
    this module stays a pure reader of time and its own on-box record. See
    the module docstring for why once-per-weekday is the cadence and why it
    is not a new number.
    """
    moment = now or _utc_now()
    path = state_path or PAUSED_STATE_PATH
    state = load_state(path)
    et_date = moment.astimezone(ET).date()
    is_weekday = et_date.weekday() < 5
    elapsed_today = _elapsed_windows_on(moment) if is_weekday else 0

    status = PausedDeskStatus(
        et_date=et_date.isoformat(),
        elapsed_windows_today=elapsed_today,
        already_notified_today=state.get("notified_for_date") == et_date.isoformat(),
        is_weekday=is_weekday,
    )

    if status.should_notify:
        state["notified_for_date"] = status.et_date
    state["updated_at"] = moment.replace(microsecond=0).isoformat()
    save_state(state, path)
    return status


def paused_alert_text(status: PausedDeskStatus) -> str:
    """Severity in the leading word, never colour alone — same convention
    as `alert_text` and `src/notifier.py`."""
    return (
        "⏸️ PAUSED: QAMC is not trading — no trading-mode timer is running "
        f"on the box ({status.et_date} ET).\n\n"
        f"{status.elapsed_windows_today} of today's scheduled session "
        "windows have already come and gone with the desk switched off. "
        "This is not a fault: somebody paused it on purpose. It is a "
        "reminder, sent at most once per weekday, because a paused desk "
        "and a working desk in a quiet market produce exactly the same "
        "silence otherwise.\n\n"
        "Resume with the trading-mode timers "
        "(`systemctl --user start quant-agent-<mode>.timer`), or ignore "
        "this if the pause is still deliberate."
    )


def alert_text(status: SilenceStatus) -> str:
    """Severity is carried in the leading word, never colour alone — matches
    `src/notifier.py`'s convention (`FAILED:`, `SUSPENDED:`)."""
    since = status.last_known_session_at or "never (no session has ever been recorded)"
    window_note = ""
    if status.newest_elapsed_window is not None:
        w = status.newest_elapsed_window
        window_note = f"\nMost recent missed window: {w.mode} on {w.date}."
    db_note = f"\nDatabase note: {status.db_error}" if status.db_error else ""
    return (
        "🛑 SILENT: QAMC has recorded no completed session in "
        f"{status.consecutive_silent_windows} consecutive scheduled windows "
        f"(threshold {status.threshold}).\n\n"
        f"Last known completed session: {since}."
        f"{window_note}{db_note}\n\n"
        "This is the desk-wide silence check, not a specific failure alert — "
        "it fires on the ABSENCE of any completed morning/intra_check/"
        "midday/close/evening/earnings_preprocess session, whatever the "
        "cause. Check Mission Control and the systemd journal for the "
        "scheduled units; the desk may be latched, crashing on startup, or "
        "simply not firing at all."
    )
