"""Durable record of every LLM ROUTE decision: switches, half-open probes
back to the primary, and honoured Retry-After hints.

Why a journal at all. The desk's failover was previously invisible: the only
trace a route switch left was a log line, and logs are rotated and are not
queryable. The owner's question -- "how often did each route carry the desk,
and what did it cost" -- could not be answered from anything the desk kept.
Per the standing "keep what costs money" ruling, a route switch is exactly
the class of fact that must persist: it is a decision, made under stress,
that moves the desk from a free route onto a paid one.

Design constraints this module is written against:

* It must NEVER raise into the caller. A journal write failing is not a
  reason to fail a trading session's analysis -- every public function
  swallows and logs. This is the same posture ``_retry_after_hint_seconds``
  takes in ``src/agents/base.py``.
* It must not import ``src.config`` or ``src.storage``. ``src/agents/base.py``
  imports this module, and both of those import chains reach back into the
  agent layer; the cost circuit keeps the same independence for the same
  reason (see its module docstring).
* It writes into the SAME SQLite file the rest of the desk uses
  (``data/quant_agent.db``, ``storage.db_path`` in config/settings.yaml), so
  a route event can be joined against ``agent_logs`` and
  ``llm_circuit_events`` by ``run_id`` without a second database to back up.
  The path is overridable with ``QUANT_AGENT_DB_PATH`` for tests.

Cost is recorded as the PRICE OF THE ROUTE, not the price of the call: at
the moment a switch is decided no usage figures exist yet. ``agent_logs``
already carries the realised per-call cost, and the two join on ``run_id``.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import threading
from pathlib import Path

from src.trading_calendar import sql_utc_stamp

logger = logging.getLogger(__name__)

# Repo root = .../src/llm_route_journal.py -> .../src -> repo root.
_REPO_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_DB_RELATIVE = "data/quant_agent.db"

# The event vocabulary. Kept as an explicit tuple rather than free text so a
# typo in a call site cannot silently create a category nobody ever reports
# on -- the same discipline `llm_circuit_events`' trip codes follow.
#
#   route_switch     -- a call moved off the route it was configured to use.
#   route_demoted    -- the primary was taken out of rotation for a cooldown.
#   probe_primary    -- a half-open probe was sent at the demoted primary.
#   route_restored   -- a probe succeeded; the primary is primary again.
#   probe_failed     -- a probe failed; the cooldown was extended.
#   retry_after      -- a server Retry-After hint was honoured.
#   seat_refused     -- a DECISION seat declined to answer because the only
#                       route left was a model unmeasured at that seat. Not
#                       a provider failure: a deliberate, recorded refusal.
EVENT_TYPES = (
    "route_switch",
    "route_demoted",
    "probe_primary",
    "route_restored",
    "probe_failed",
    "retry_after",
    "seat_refused",
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS llm_route_events (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type    TEXT NOT NULL,
    agent_name    TEXT,
    run_id        TEXT,
    -- The route this event is ABOUT, as provider/model. For a switch this
    -- is the route being moved TO; `from_route` carries the one left behind.
    route         TEXT,
    from_route    TEXT,
    -- Route tier: 1 primary, 2 secondary, 3 tertiary. Lets a report say
    -- "the desk ran on tier 3 for 40 minutes" without parsing model ids.
    tier          INTEGER,
    -- Published list price of `route` at the time of the event, USD per
    -- million tokens. NOT the cost of this call -- see the module docstring.
    input_usd_per_mtok  REAL,
    output_usd_per_mtok REAL,
    -- Seconds: the honoured Retry-After, or the cooldown just applied.
    wait_s        REAL,
    -- Shortened repr of the triggering error, for shape analysis later.
    error_shape   TEXT,
    detail        TEXT,
    timestamp     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_llm_route_events_ts
    ON llm_route_events(timestamp);
CREATE INDEX IF NOT EXISTS idx_llm_route_events_run
    ON llm_route_events(run_id);
"""

_lock = threading.Lock()
_initialised_for: str | None = None

# Count of writes this process could not persist.
#
# "Best-effort, never raises" is the right posture for a journal that must
# not be able to fail a trading session. This in-process counter and the
# ERROR log line are the only trace of a failed write: `write_failures()` is
# read by NOTHING (not the health report, not the dashboard), and it resets
# when the process restarts. A journal that silently stopped recording looks
# like "no route switches happened". The rows that DO land are shown on the
# dashboard's Model Fallbacks panel (`src/api/routes_route_events.py`).
_write_failures = 0


def write_failures() -> int:
    """How many journal writes failed in this process. 0 is healthy."""
    return _write_failures


def journal_db_path() -> str:
    """Resolve the journal's SQLite path.

    ``QUANT_AGENT_DB_PATH`` wins (tests point it at a tmp file); otherwise the
    repo-relative default that mirrors ``storage.db_path``. A relative
    override is resolved against the repo root, not the process CWD, because
    the desk is launched from launchd with an unspecified working directory.
    """
    raw = os.environ.get("QUANT_AGENT_DB_PATH") or _DEFAULT_DB_RELATIVE
    if raw == ":memory:":
        return raw
    p = Path(raw)
    if not p.is_absolute():
        p = _REPO_ROOT / p
    return str(p)


def _connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=10.0)
    # WAL so a journal write can never block, or be blocked by, the cost
    # circuit's own transactions on the same file.
    try:
        conn.execute("PRAGMA journal_mode=WAL")
    except sqlite3.Error:  # a filesystem that refuses WAL must not be fatal
        pass
    return conn


def _ensure_schema(conn: sqlite3.Connection, path: str) -> None:
    global _initialised_for
    if _initialised_for == path:
        return
    conn.executescript(_SCHEMA)
    conn.commit()
    _initialised_for = path


def record(
    event_type: str,
    *,
    agent_name: str | None = None,
    run_id: str | None = None,
    route: str | None = None,
    from_route: str | None = None,
    tier: int | None = None,
    input_usd_per_mtok: float | None = None,
    output_usd_per_mtok: float | None = None,
    wait_s: float | None = None,
    error: BaseException | None = None,
    detail: str | None = None,
) -> bool:
    """Append one route event. Returns True on write, False on any failure.

    Never raises. A journal that can break a trading session is worse than no
    journal, and the caller has nothing useful to do with the exception.
    """
    if event_type not in EVENT_TYPES:
        logger.warning("llm_route_journal: unknown event_type %r — not recorded", event_type)
        return False
    error_shape = None
    if error is not None:
        status = getattr(error, "status_code", None)
        error_shape = f"{type(error).__name__}(status={status!r}): {str(error)[:200]}"
    path = journal_db_path()
    try:
        with _lock:
            conn = _connect(path)
            try:
                _ensure_schema(conn, path)
                conn.execute(
                    "INSERT INTO llm_route_events "
                    "(event_type, agent_name, run_id, route, from_route, tier, "
                    " input_usd_per_mtok, output_usd_per_mtok, wait_s, "
                    " error_shape, detail, timestamp) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        event_type,
                        agent_name,
                        run_id,
                        route,
                        from_route,
                        tier,
                        input_usd_per_mtok,
                        output_usd_per_mtok,
                        wait_s,
                        error_shape,
                        detail,
                        sql_utc_stamp(),
                    ),
                )
                conn.commit()
            finally:
                conn.close()
        return True
    except Exception as exc:  # noqa: BLE001 — see docstring
        global _write_failures
        _write_failures += 1
        # ERROR, not warning: this is the record of which road answered and
        # what it cost. Losing it is not a nuisance, it is losing the only
        # evidence for the question the owner will ask.
        logger.error(
            "llm_route_journal: could not record %s (%d write failure(s) this process): %s",
            event_type,
            _write_failures,
            exc,
        )
        return False


def read_events(limit: int = 200) -> list[dict]:
    """Newest-first route events, for the operator report. Never raises."""
    path = journal_db_path()
    try:
        with _lock:
            conn = _connect(path)
            try:
                _ensure_schema(conn, path)
                conn.row_factory = sqlite3.Row
                rows = conn.execute(
                    "SELECT * FROM llm_route_events ORDER BY id DESC LIMIT ?",
                    (int(limit),),
                ).fetchall()
                return [dict(r) for r in rows]
            finally:
                conn.close()
    except Exception as exc:  # noqa: BLE001
        logger.warning("llm_route_journal: could not read events: %s", exc)
        return []


def _reset_schema_cache_for_tests() -> None:
    """Forget which path the schema was created on.

    Tests repoint ``QUANT_AGENT_DB_PATH`` between cases; without this the
    memoised path would skip schema creation on the new file.
    """
    global _initialised_for, _write_failures
    _initialised_for = None
    _write_failures = 0
