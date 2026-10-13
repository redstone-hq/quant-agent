"""The alert channel's own watchdog — the sessions test the alarm they rely on.

THE DEFECT
----------
Every alarm on this desk is a Telegram message: deploy drift, a pricing
cache that did not land, a session that crashed, a stop that went missing.
Nothing checked that a Telegram message could still be sent, so "no alert
arrived" meant two different things at once —

    nothing is wrong        (the desk is healthy)
    the alarm is broken     (the desk may be on fire)

— and nothing on the box could tell them apart.

TWO FAILURE MODES, DELIBERATELY SEPARATED
-----------------------------------------
They need different answers, and conflating them is what produced the
previous design (a weekly "still alive" message whose absence was supposed
to be the signal — i.e. up to seven days of undetected breakage).

  (A) THE CHANNEL IS BROKEN WHILE THE BOX IS ALIVE. A revoked bot token, a
      chat id that is wrong or deleted, a bot the operator blocked, a new
      egress rule that drops api.telegram.org, a unit that never sourced
      `.env`. Every one of these passes a credentials check and fails a
      send. This is the LIKELY failure, and it is fully detectable from
      inside the box. THIS MODULE COVERS (A).

  (B) THE BOX ITSELF IS DEAD, OFF, OR UNREACHABLE. By definition nothing
      running on the box can report this. No amount of local engineering
      solves it and this module does not pretend to. See README's
      "Proving the alert channel is alive" for what is and is not covered.
      An out-of-band ping to an external monitoring service would cover it;
      the owner refused that dependency outright, so the gap is stated
      rather than closed, and no dormant hook is left lying around to be
      switched on by mistake.

THE MECHANISM FOR (A): THE THING THAT DEPENDS ON THE ALARM TESTS IT
-------------------------------------------------------------------
The desk already runs five to six sessions every weekday and every one of
them ends by pushing a Telegram message. So each session now exercises the
whole alert path end to end as part of its own run — `TelegramNotifier.probe`
(one real silent message, deleted immediately afterwards), the same code the
scheduled heartbeat uses. No new schedule, no new credential, no new
dependency, and no LLM call.

The verdict is written to SQLite (`alert_channel_checks`), which is what
makes it durable and readable by Mission Control. A broken channel therefore
shows up in three places that do NOT depend on Telegram working:

  * Mission Control `/health` -> `alert_channel`, rendered red.
  * The session's own status message (forced out even for modes that are
    normally silent) — futile if the channel is fully dead, but a channel
    can fail a probe at a stage an ordinary send survives, and it is how
    the RECOVERY gets reported.
  * The systemd journal / unit state for the heartbeat timer.

WHAT THIS COSTS
---------------
Two extra HTTPS requests to api.telegram.org per session, one row in a
SQLite table, no LLM call, no paid dependency.

NEVER RAISES
------------
Every public function here swallows its own failures. This runs inside
`main.py`'s `finally` block: a raise here would replace the in-flight
session exception and hide the real fault behind a watchdog bug. A watchdog
that can break the thing it watches is worse than no watchdog.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from src.trading_calendar import utc_now
from pathlib import Path
from typing import Any
from src.data_paths import db_path

logger = logging.getLogger(__name__)
from src.sentinel.counted import record_swallowed

#: Same file `src/notifier.py` reads for the cost line and position
#: snapshot, resolved from the project root rather than the working
#: directory — sessions are started both by systemd (which sets
#: WorkingDirectory) and by hand from anywhere.
DB_PATH = db_path()

TABLE = "alert_channel_checks"

#: How old the newest SUCCESSFUL check may be before the channel is
#: reported `stale` — i.e. "we have no current evidence the alarm works".
#:
#: 26 hours, and the number is not arbitrary. Sessions only run Mon-Fri, so
#: a session-only watchdog would go red every weekend and teach the operator
#: to ignore red. `quant-agent-alert-heartbeat.timer` therefore runs the
#: same probe once a day, SEVEN days a week, as the floor: the longest legal
#: gap between two checks is one day plus timer slack. Anything past 26h
#: means both the sessions and the daily backstop have stopped, which is
#: itself worth knowing.
STALE_AFTER_HOURS = 26.0

#: Rows kept in the table. ~20 checks a weekday (5-6 sessions + 14
#: intra_check ticks + the daily backstop) — 2000 rows is roughly three
#: months, enough to answer "has this been flapping?" without unbounded
#: growth in a database the trading path also uses.
ROW_LIMIT = 2000

#: A rehearsal replays a real session offline and must not transmit. Its
#: non-send is NOT an outage and must never be recorded as one: "we did not
#: check" and "we checked and it is broken" are the two things this whole
#: design exists to keep apart.
_NOT_RECORDED_STAGES = frozenset({"rehearsal"})


@dataclass(frozen=True)
class AlertChannelHealth:
    """What the box currently knows about its own ability to raise an alarm.

    `status` is the load-bearing field:

      ok       — the most recent check sent a real message and it arrived.
      broken   — the most recent check FAILED. The desk cannot reach the
                 operator; every alarm it raises from now on goes nowhere.
      stale    — the most recent check succeeded but is older than
                 STALE_AFTER_HOURS. Nothing is known to be wrong and nothing
                 is known to be right; the checks themselves have stopped.
      muted    — the operator deliberately silenced the channel
                 (TELEGRAM_DISABLED). Nothing is BROKEN — and nothing
                 will be sent either, so the desk still cannot raise an
                 alarm. Its own status, and still degraded.
      unknown  — no check has ever been recorded. A fresh database, or a
                 deploy that has not run a session yet. Explicitly NOT "ok".
    """

    status: str
    last_check_at: str | None = None
    last_ok_at: str | None = None
    last_stage: str | None = None
    last_detail: str | None = None
    consecutive_failures: int = 0
    age_hours: float | None = None
    stale_after_hours: float = STALE_AFTER_HOURS
    error: str | None = None

    @property
    def degraded(self) -> bool:
        """True when Mission Control should show red. `unknown` is amber:
        it is a missing measurement, not a detected fault, and flipping the
        whole board red on a fresh database would train the operator to
        ignore the colour that matters. `muted` IS degraded: the operator
        chose the silence, but while it holds no alarm reaches him, and
        calling that healthy would be the same lie in a friendlier
        costume. Separating it from `broken` fixes the other half of the
        lie — nothing is broken, and the repair is different."""
        return self.status in ("broken", "stale", "muted")

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "last_check_at": self.last_check_at,
            "last_ok_at": self.last_ok_at,
            "last_stage": self.last_stage,
            "last_detail": self.last_detail,
            "consecutive_failures": self.consecutive_failures,
            "age_hours": self.age_hours,
            "stale_after_hours": self.stale_after_hours,
            "error": self.error,
        }


# ---------------------------------------------------------------------------
# storage
# ---------------------------------------------------------------------------


def _resolve_db_path(db_path: str | Path | None) -> str | None:
    """Accept only a real path. Returns None when there is nothing usable.

    Deliberately strict about the type: `main.py` hands this
    `config.storage.db_path`, and a test that mocks `load_config` hands a
    MagicMock instead. `str(MagicMock())` is a perfectly valid filename, so
    without this check a mocked test run would silently create a junk
    database in the working directory.
    """
    candidate = db_path if db_path is not None else DB_PATH
    if isinstance(candidate, Path):
        return str(candidate)
    if isinstance(candidate, str) and candidate.strip():
        return candidate
    return None


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Additive, idempotent. Safe to call on every write."""
    conn.executescript(
        f"""
        CREATE TABLE IF NOT EXISTS {TABLE} (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            checked_at TEXT NOT NULL,
            ok INTEGER NOT NULL,
            stage TEXT NOT NULL,
            detail TEXT NOT NULL DEFAULT '',
            residue INTEGER NOT NULL DEFAULT 0,
            source TEXT NOT NULL DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS idx_alert_channel_checks_time
            ON {TABLE}(checked_at);
        """
    )


def _connect_rw(path: str) -> sqlite3.Connection:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=10000")
    return conn


def _connect_ro(path: str) -> sqlite3.Connection:
    """Read-only by OS enforcement, the same structural guarantee
    `src/api/db_reads.py` relies on: a future bug that added a write here
    fails loudly instead of corrupting trading state."""
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=2000")
    return conn


def record_check(
    *,
    ok: bool,
    stage: str,
    detail: str = "",
    residue: bool = False,
    source: str = "",
    db_path: str | Path | None = None,
    now: datetime | None = None,
) -> bool:
    """Persist one verdict. Returns False rather than raising, ever."""
    path = _resolve_db_path(db_path)
    if path is None:
        return False
    moment = (now or utc_now()).astimezone(timezone.utc)
    try:
        conn = _connect_rw(path)
    except Exception as exc:  # noqa: BLE001
        record_swallowed("alert_watchdog.open_db", exc, log=logger)
        return False
    try:
        with conn:
            ensure_schema(conn)
            conn.execute(
                f"INSERT INTO {TABLE} (checked_at, ok, stage, detail, residue, source) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    moment.replace(microsecond=0).isoformat(),
                    1 if ok else 0,
                    str(stage or "unknown"),
                    str(detail or "")[:500],
                    1 if residue else 0,
                    str(source or ""),
                ),
            )
            conn.execute(
                f"DELETE FROM {TABLE} WHERE id NOT IN (SELECT id FROM {TABLE} ORDER BY id DESC LIMIT ?)",
                (ROW_LIMIT,),
            )
        return True
    except Exception as exc:  # noqa: BLE001
        record_swallowed("alert_watchdog.record_check", exc, log=logger)
        return False
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass


def read_health(
    db_path: str | Path | None = None,
    now: datetime | None = None,
) -> AlertChannelHealth:
    """The current verdict. Never raises; degrades to `unknown` with a reason."""
    path = _resolve_db_path(db_path)
    if path is None:
        return AlertChannelHealth("unknown", error="no database path configured")
    moment = (now or utc_now()).astimezone(timezone.utc)
    try:
        conn = _connect_ro(path)
    except Exception as exc:  # noqa: BLE001
        return AlertChannelHealth("unknown", error=f"database unreadable: {exc}")
    try:
        rows = conn.execute(f"SELECT checked_at, ok, stage, detail FROM {TABLE} ORDER BY id DESC LIMIT 200").fetchall()
    except Exception as exc:  # noqa: BLE001
        # Includes "no such table" on a database that predates this feature.
        return AlertChannelHealth("unknown", error=f"no check history: {exc}")
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass

    if not rows:
        return AlertChannelHealth("unknown", error="no check has ever been recorded")

    newest = rows[0]
    last_ok_at = next(
        (str(r["checked_at"]) for r in rows if int(r["ok"] or 0) == 1),
        None,
    )
    consecutive_failures = 0
    for row in rows:
        if int(row["ok"] or 0) == 1:
            break
        consecutive_failures += 1

    age_hours = _age_hours(str(newest["checked_at"]), moment)
    if int(newest["ok"] or 0) != 1:
        # A mute is a failed send by construction; it is not a failure.
        status = "muted" if str(newest["stage"] or "") == "muted" else "broken"
    elif age_hours is not None and age_hours > STALE_AFTER_HOURS:
        status = "stale"
    else:
        status = "ok"

    return AlertChannelHealth(
        status=status,
        last_check_at=str(newest["checked_at"]),
        last_ok_at=last_ok_at,
        last_stage=str(newest["stage"]),
        last_detail=str(newest["detail"] or ""),
        consecutive_failures=consecutive_failures,
        age_hours=age_hours,
    )


from src.alert_watchdog_age import _age_hours  # noqa: E402,F401 -- lifted verbatim


# ---------------------------------------------------------------------------
# the check itself
# ---------------------------------------------------------------------------


def verify_alert_channel(
    notifier: Any | None = None,
    *,
    source: str = "",
    db_path: str | Path | None = None,
    now: datetime | None = None,
):
    """Exercise the alert path end to end and record the verdict.

    Returns the `ProbeResult`, or None when nothing was checked (a
    rehearsal, a mocked notifier, or a probe that blew up). None means
    exactly "we did not check" and is never recorded as an outage.

    Reuses `TelegramNotifier.probe` — the send-and-delete that PR #175
    already built and that the scheduled heartbeat already uses. A second
    implementation of "is the channel alive" would be a self-test that can
    pass while the path it stands in for is broken.
    """
    from src.notifier import ProbeResult, build_default_notifier

    try:
        target = build_default_notifier(existing=notifier)
        result = target.probe()
    except Exception as exc:  # noqa: BLE001
        record_swallowed("alert_watchdog.probe", exc, log=logger)
        return None

    if not isinstance(result, ProbeResult):
        # A mocked/duck-typed notifier in a test. Recording a MagicMock as
        # an outage would manufacture a fake incident in the real table.
        return None
    if result.stage in _NOT_RECORDED_STAGES:
        return result

    record_check(
        ok=result.ok,
        stage=result.stage,
        detail=result.detail,
        residue=result.residue,
        source=source,
        db_path=db_path,
        now=now,
    )
    return result


# ---------------------------------------------------------------------------
# what the operator sees
# ---------------------------------------------------------------------------


def session_note(before: AlertChannelHealth | None, result: Any | None) -> str | None:
    """The line a session appends to its own status message, or None.

    SILENT ON SUCCESS. A routine "the alarm still works" on every session is
    a message the operator learns to swipe away, and a confirmation nobody
    reads is worth exactly what no confirmation is worth. There are only two
    things worth saying:

      * the channel just FAILED its check — futile if the channel is fully
        dead, but a probe can fail at a stage an ordinary send survives, and
        then this is the fastest warning available;
      * the channel has just RECOVERED after failing — this is the one that
        reaches him without him having to go and look, and it carries how
        long the desk was unable to shout.
    """
    from src.notifier import ProbeResult

    if not isinstance(result, ProbeResult) or result.stage in _NOT_RECORDED_STAGES:
        return None

    if not result.ok:
        return (
            "🔴 ALERT CHANNEL FAILED ITS SELF-TEST\n"
            f"Stage: {result.stage}\n"
            f"Detail: {result.detail or 'no detail'}\n"
            "Every alarm on this desk goes out over this channel. Until it "
            "is fixed, silence from QAMC means nothing at all. Mission "
            "Control shows this red under System health. Check "
            "TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID in .env and outbound "
            "access to api.telegram.org."
        )

    failures = before.consecutive_failures if before else 0
    if failures > 0:
        since = (before.last_ok_at if before else None) or "an unknown time"
        return (
            "🟢 Alert channel RECOVERED — it is working again.\n"
            f"It failed {failures} consecutive check(s); the last one that "
            f"worked before now was {since}. Any alarm the desk tried to "
            "raise in that window did not reach you."
        )
    return None


def annotate_session_message(
    message: str | None,
    *,
    mode: str,
    before: AlertChannelHealth | None,
    result: Any | None,
) -> str | None:
    """Fold `session_note` into the session's Telegram message.

    Forces a message out even for modes whose noise policy is normally
    silent (the ~14 intra_check OK ticks a day). A broken alarm that is only
    reported by sessions that happened to be chatty is an alarm that can
    stay quiet all day.
    """
    try:
        note = session_note(before, result)
    except Exception as exc:  # noqa: BLE001
        logger.warning("alert watchdog note failed: %s", exc)
        return message
    if not note:
        return message
    if not message:
        return f"QAMC {mode}\n\n{note}"
    return f"{message}\n\n{note}"
