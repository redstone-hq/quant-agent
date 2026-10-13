"""src.cost_circuit.schema -- moved verbatim from src/cost_circuit.py; see the package docstring."""

from __future__ import annotations
import sqlite3
from src.cost_circuit.classification import _trigger_scope
from src.cost_circuit.clock import _pinned_if_clock_replaced, _sql_stamp


def ensure_cost_circuit_schema(conn: sqlite3.Connection) -> None:
    """Create the additive breaker schema on an existing SQLite connection."""
    conn = _pinned_if_clock_replaced(conn)

    expected_breaker_tables = {
        "llm_budget_days",
        "llm_budget_sessions",
        "llm_circuit_state",
        "llm_circuit_events",
    }
    existing_breaker_tables = {
        str(row[0])
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        if str(row[0]) in expected_breaker_tables
    }
    quota_table_exists = (
        conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='llm_quota_holds'").fetchone() is not None
    )
    if quota_table_exists and existing_breaker_tables != expected_breaker_tables:
        raise RuntimeError(
            "cost-circuit schema is partial: quota holds exist without the complete base accounting schema"
        )
    if existing_breaker_tables and existing_breaker_tables != expected_breaker_tables:
        missing = sorted(expected_breaker_tables - existing_breaker_tables)
        raise RuntimeError("cost-circuit schema is partial; missing table(s): " + ", ".join(missing))
    state_table_existed = "llm_circuit_state" in existing_breaker_tables
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS llm_budget_days (
            day TEXT PRIMARY KEY,
            baseline_cost_usd REAL NOT NULL DEFAULT 0,
            incremental_cost_usd REAL NOT NULL DEFAULT 0,
            unknown_cost_rows INTEGER NOT NULL DEFAULT 0,
            -- Subset of unknown_cost_rows contributed by a FAILED call
            -- (`fail_call`), as opposed to a completed call with no usage
            -- telemetry or a pre-deployment log row. Only this subset is
            -- eligible for the transient self-clear; see
            -- `_auto_clear_transient_latch_locked`.
            failed_call_unknown_rows INTEGER NOT NULL DEFAULT 0,
            costs_exact INTEGER NOT NULL DEFAULT 1,
            seeded_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS llm_budget_sessions (
            run_id TEXT PRIMARY KEY,
            day TEXT NOT NULL,
            mode TEXT NOT NULL,
            actual_cost_usd REAL NOT NULL DEFAULT 0,
            logical_calls INTEGER NOT NULL DEFAULT 0,
            provider_attempts INTEGER NOT NULL DEFAULT 0,
            retry_attempts INTEGER NOT NULL DEFAULT 0,
            costs_exact INTEGER NOT NULL DEFAULT 1,
            status TEXT NOT NULL DEFAULT 'active',
            started_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_llm_budget_sessions_day_mode
            ON llm_budget_sessions(day, mode);

        CREATE TABLE IF NOT EXISTS llm_circuit_state (
            singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
            suspended INTEGER NOT NULL DEFAULT 0,
            trigger_code TEXT,
            trigger_detail TEXT,
            run_id TEXT,
            mode TEXT,
            agent_name TEXT,
            session_attempts INTEGER NOT NULL DEFAULT 0,
            attempts_exact INTEGER NOT NULL DEFAULT 1,
            costs_exact INTEGER NOT NULL DEFAULT 1,
            session_cost_usd REAL NOT NULL DEFAULT 0,
            daily_cost_usd REAL NOT NULL DEFAULT 0,
            session_limit_usd REAL,
            daily_limit_usd REAL,
            suspended_at TEXT,
            alert_state INTEGER NOT NULL DEFAULT 0,
            reset_at TEXT,
            reset_reason TEXT,
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS llm_circuit_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_type TEXT NOT NULL,
            trigger_code TEXT,
            detail TEXT,
            run_id TEXT,
            mode TEXT,
            agent_name TEXT,
            attempts INTEGER,
            session_cost_usd REAL,
            daily_cost_usd REAL,
            -- Item 174: an `auto_reset` event (a transient latch that expired on
            -- its own) must reach the owner on the same Telegram surface the
            -- suspension did. These two columns track that recovery alert with
            -- the same 0=pending / -1=claimed-retryable / 1=sent state machine
            -- `llm_quota_holds.recovery_alert_state` uses. They stay 0 (NULL on
            -- a legacy row -> treated as 0) for every non-`auto_reset` event.
            recovery_alert_state INTEGER NOT NULL DEFAULT 0,
            recovery_alert_updated_at TEXT NOT NULL DEFAULT (datetime('now')),
            -- Item 174 pairing: the suspension's own `alert_state` as it stood
            -- the instant the auto-clear wiped it. 1 = the owner actually got
            -- the "SUSPENDED" note, -1 = a send was in flight and its outcome
            -- is unknown, 0 = it never reached him. A resume note is only
            -- honest as the answer to a suspension note he received, so a 0
            -- here resolves the recovery as unpaired (recovery_alert_state=2)
            -- instead of sending. Defaults to 1 so non-`auto_reset` rows and
            -- legacy rows keep the pre-pairing behaviour.
            suspension_alert_state INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS llm_quota_holds (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            scope TEXT NOT NULL CHECK (scope IN ('day', 'mode_day', 'session')),
            scope_key TEXT NOT NULL,
            day TEXT NOT NULL,
            trigger_code TEXT NOT NULL,
            trigger_detail TEXT NOT NULL,
            run_id TEXT NOT NULL,
            mode TEXT NOT NULL,
            agent_name TEXT NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0,
            attempts_exact INTEGER NOT NULL DEFAULT 1,
            costs_exact INTEGER NOT NULL DEFAULT 1,
            session_cost_usd REAL NOT NULL DEFAULT 0,
            daily_cost_usd REAL NOT NULL DEFAULT 0,
            session_limit_usd REAL,
            daily_limit_usd REAL,
            active INTEGER NOT NULL DEFAULT 1,
            alert_state INTEGER NOT NULL DEFAULT 0,
            alert_updated_at TEXT NOT NULL DEFAULT (datetime('now')),
            recovery_alert_state INTEGER NOT NULL DEFAULT 0,
            recovery_alert_updated_at TEXT NOT NULL DEFAULT (datetime('now')),
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            released_at TEXT,
            release_reason TEXT
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_llm_quota_holds_active_scope
            ON llm_quota_holds(scope, scope_key, day) WHERE active=1;
        CREATE INDEX IF NOT EXISTS idx_llm_quota_holds_active_day
            ON llm_quota_holds(day, active);
        """
    )
    if not existing_breaker_tables:
        conn.execute("INSERT INTO llm_circuit_state(singleton) VALUES (1)")
    elif not state_table_existed:
        raise RuntimeError(
            "cost-circuit schema is partial: state table is missing while other budget tables already exist"
        )
    elif conn.execute("SELECT COUNT(*) FROM llm_circuit_state WHERE singleton=1").fetchone()[0] != 1:
        # Recreating an absent state row as open would erase a durable latch.
        # Treat loss/corruption of the singleton as infrastructure failure.
        raise RuntimeError("cost-circuit singleton state row is missing or corrupt")
    # Additive migration for databases initialized by an earlier breaker
    # build during rollout.
    state_columns = {row[1] for row in conn.execute("PRAGMA table_info(llm_circuit_state)")}
    if "attempts_exact" not in state_columns:
        conn.execute("ALTER TABLE llm_circuit_state ADD COLUMN attempts_exact INTEGER NOT NULL DEFAULT 1")
    if "costs_exact" not in state_columns:
        conn.execute("ALTER TABLE llm_circuit_state ADD COLUMN costs_exact INTEGER NOT NULL DEFAULT 1")
    day_columns = {row[1] for row in conn.execute("PRAGMA table_info(llm_budget_days)")}
    if "unknown_cost_rows" not in day_columns:
        conn.execute("ALTER TABLE llm_budget_days ADD COLUMN unknown_cost_rows INTEGER NOT NULL DEFAULT 0")
    if "costs_exact" not in day_columns:
        conn.execute("ALTER TABLE llm_budget_days ADD COLUMN costs_exact INTEGER NOT NULL DEFAULT 1")
    if "failed_call_unknown_rows" not in day_columns:
        # Defaults to 0, so any unknown row already on the books when this
        # column appears is attributed to the NON-self-clearing class and
        # still needs an operator. Migrating conservatively is the point.
        conn.execute("ALTER TABLE llm_budget_days ADD COLUMN failed_call_unknown_rows INTEGER NOT NULL DEFAULT 0")
    session_columns = {row[1] for row in conn.execute("PRAGMA table_info(llm_budget_sessions)")}
    if "costs_exact" not in session_columns:
        conn.execute("ALTER TABLE llm_budget_sessions ADD COLUMN costs_exact INTEGER NOT NULL DEFAULT 1")
    # Item 174: recovery-alert tracking for `auto_reset` events on databases
    # created before this column existed. Defaults to 0 (pending), so any
    # auto_reset row already on the books when this column appears is treated
    # as not-yet-alerted and the owner is told on the next boundary -- the
    # conservative direction (a possibly-duplicate "back live" note, never a
    # silently-missed one).
    event_columns = {row[1] for row in conn.execute("PRAGMA table_info(llm_circuit_events)")}
    if "recovery_alert_state" not in event_columns:
        conn.execute("ALTER TABLE llm_circuit_events ADD COLUMN recovery_alert_state INTEGER NOT NULL DEFAULT 0")
        # Any auto_reset row already on the books cleared before this alert
        # existed; its latch is long gone. Mark those handled (state=1) so the
        # first boundary after deploy does NOT fire a burst of stale "desk
        # resumed" notes for latches that expired days ago -- a stale recovery
        # alert is itself a defect. Only auto-clears from here forward alert.
        conn.execute("UPDATE llm_circuit_events SET recovery_alert_state=1 WHERE event_type='auto_reset'")
    if "recovery_alert_updated_at" not in event_columns:
        # SQLite forbids a non-constant default (e.g. datetime('now')) on
        # ALTER TABLE ADD COLUMN -- it raises "Cannot add a column with
        # non-constant default", which would abort the whole schema init and
        # fail paid analysis closed. The CREATE TABLE path keeps the
        # datetime('now') default; on the migration path a constant '' default
        # is safe because this column is only ever read while a row is in the
        # in-flight retry state (recovery_alert_state=-1), and the code always
        # binds recovery_alert_updated_at from the desk clock (_sql_stamp) at
        # the 0 -> -1 transition before any comparison against it can run.
        conn.execute("ALTER TABLE llm_circuit_events ADD COLUMN recovery_alert_updated_at TEXT NOT NULL DEFAULT ''")
    if "suspension_alert_state" not in event_columns:
        # Item 174 pairing. Default 1 ("the owner was told about the
        # suspension") is the only safe backfill: for rows written before this
        # column existed the true value is unrecoverable, and the two wrong
        # directions are not symmetric -- defaulting to 0 would silently
        # swallow a legitimate resume note, while defaulting to 1 at worst
        # sends one. Every row already on the books is additionally
        # recovery_alert_state=1 (handled) from the migration above, so this
        # backfill changes nothing retroactively; it only governs rows written
        # by a build that still lacks the column.
        conn.execute("ALTER TABLE llm_circuit_events ADD COLUMN suspension_alert_state INTEGER NOT NULL DEFAULT 1")

    # Operator resets only became owner-notifiable here; every 'reset' row
    # already on the books cleared before that, so firing "RESUMED" for them
    # now would announce recoveries from incidents that ended weeks ago --
    # the same stale-alert defect the auto_reset backfill above guards
    # against, and it is marked handled the same way. SQLite's own
    # `user_version` counter is the one-time hook (nothing else in this
    # schema reads or writes it, so it is 0 on every existing database);
    # the backfill therefore runs exactly once per database.
    if int(conn.execute("PRAGMA user_version").fetchone()[0] or 0) < 1:
        conn.execute("UPDATE llm_circuit_events SET recovery_alert_state=1 WHERE event_type='reset'")
        conn.execute("PRAGMA user_version = 1")

    # Migrate a latch created by the original one-state implementation.  Known
    # quota triggers retain their full audit snapshot but cease to masquerade
    # as hard infrastructure incidents.  Unknown codes deliberately remain
    # hard/operator-reset-only.
    state = conn.execute("SELECT * FROM llm_circuit_state WHERE singleton=1").fetchone()
    if state is not None and int(state["suspended"] or 0):
        code = str(state["trigger_code"] or "")
        scope = _trigger_scope(code)
        if scope != "hard":
            run_id = str(state["run_id"] or "unscoped")
            mode = str(state["mode"] or "unknown")
            session_day_row = conn.execute("SELECT day FROM llm_budget_sessions WHERE run_id=?", (run_id,)).fetchone()
            if session_day_row is None:
                # Scope/date provenance is part of the safety decision.  A
                # partially migrated incident must remain hard instead of
                # guessing that an old quota belongs to today's budget.
                raise RuntimeError(f"legacy quota latch {code!r} has no session/day provenance for run {run_id}")
            hold_day = str(session_day_row["day"])
            scope_key = hold_day if scope == "day" else f"{hold_day}:{mode}" if scope == "mode_day" else run_id
            conn.execute(
                "INSERT OR IGNORE INTO llm_quota_holds "
                "(scope, scope_key, day, trigger_code, trigger_detail, run_id, mode, "
                "agent_name, attempts, attempts_exact, costs_exact, session_cost_usd, "
                "daily_cost_usd, session_limit_usd, daily_limit_usd, alert_state) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    scope,
                    scope_key,
                    hold_day,
                    code,
                    str(state["trigger_detail"] or code),
                    run_id,
                    mode,
                    str(state["agent_name"] or "unknown"),
                    int(state["session_attempts"] or 0),
                    int(state["attempts_exact"] or 0),
                    int(state["costs_exact"] or 0),
                    float(state["session_cost_usd"] or 0),
                    float(state["daily_cost_usd"] or 0),
                    state["session_limit_usd"],
                    state["daily_limit_usd"],
                    int(state["alert_state"] or 0),
                ),
            )
            conn.execute(
                "UPDATE llm_circuit_state SET suspended=0, trigger_code=NULL, "
                "trigger_detail=NULL, run_id=NULL, mode=NULL, agent_name=NULL, "
                "session_attempts=0, session_cost_usd=0, daily_cost_usd=0, "
                "attempts_exact=1, costs_exact=1, suspended_at=NULL, alert_state=0, "
                "updated_at=? WHERE singleton=1",
                (_sql_stamp(),),
            )
    # Schema creation and legacy-latch migration are one self-contained
    # initialization unit.  Callers intentionally start their operational
    # BEGIN IMMEDIATE transaction only after this durable migration commits.
    conn.commit()
