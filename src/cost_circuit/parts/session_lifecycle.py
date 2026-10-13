"""src.cost_circuit.parts.session_lifecycle -- SessionLifecycle for the cost circuit (initialize, seed/validate the day, activate_session, context).

Bodies moved verbatim from the former src/cost_circuit/breaker_session.py (now held by LLMCostCircuitBreaker) (originally src/cost_circuit.py).
Every collaborator is an explicit keyword-only constructor argument.
"""

from __future__ import annotations
import sqlite3
from typing import Any, Callable, TypeVar
from src.cost_circuit.classification import _unknown_cost_row_expr
from src.cost_circuit.clock import _et_day_and_utc_bounds, _legacy_mode, _sql_stamp
from src.cost_circuit.schema import ensure_cost_circuit_schema


class SessionLifecycle:
    def __init__(
        self,
        *,
        enabled,
        connect,
        session_context,
        infrastructure_lock,
        sync_emergency_latch,
        reconcile_quota_holds_locked,
        run_with_infra_retry,
        notify_if_needed,
        enforce_current_limits,
        status,
        read_unavailable_sentinel,
        seed_today=None,
        validate_accounting_invariants=None,
    ) -> None:
        self.enabled = enabled
        self._connect = connect
        self._session_context = session_context
        self._infrastructure_lock = infrastructure_lock
        self._sync_emergency_latch = sync_emergency_latch
        self._reconcile_quota_holds_locked = reconcile_quota_holds_locked
        self._run_with_infra_retry = run_with_infra_retry
        self._notify_if_needed = notify_if_needed
        self.enforce_current_limits = enforce_current_limits
        self.status = status
        self.read_unavailable_sentinel = read_unavailable_sentinel
        # The bodies below are themselves moved; a caller passes one only when it was
        # swapped on the host (see the shim), otherwise this object's own run.
        if seed_today is not None:
            self._seed_today = seed_today
        if validate_accounting_invariants is not None:
            self._validate_accounting_invariants = validate_accounting_invariants

    @property
    def _unavailable_sentinel(self):
        """Read live off the host: the bodies read it after calls that install it."""
        return self.read_unavailable_sentinel()

    def _initialize(self) -> None:
        with self._connect() as conn:
            ensure_cost_circuit_schema(conn)
            conn.execute("BEGIN IMMEDIATE")
            self._seed_today(conn)
            conn.commit()

    def _validate_accounting_invariants(self, conn: sqlite3.Connection, day: str) -> None:
        """Reject missing/cross-linked rows instead of interpreting them as $0."""

        day_row = conn.execute("SELECT incremental_cost_usd FROM llm_budget_days WHERE day=?", (day,)).fetchone()
        if day_row is None:
            raise RuntimeError(f"cost-circuit day accounting row is missing for {day}")

        session_total = conn.execute(
            "SELECT COALESCE(SUM(actual_cost_usd), 0) AS cost "
            "FROM llm_budget_sessions WHERE day=? AND status<>'legacy'",
            (day,),
        ).fetchone()
        if abs(float(day_row["incremental_cost_usd"] or 0.0) - float(session_total["cost"] or 0.0)) > 1e-8:
            raise RuntimeError("cost-circuit day/session settled-cost ledgers disagree for " + day)

        try:
            missing_log_session = conn.execute(
                "SELECT a.run_id FROM agent_logs a "
                "LEFT JOIN llm_budget_sessions s ON s.run_id=a.run_id "
                "WHERE a.timestamp BETWEEN ? AND ? AND s.run_id IS NULL LIMIT 1",
                _et_day_and_utc_bounds()[1:],
            ).fetchone()
        except sqlite3.OperationalError as exc:
            if "no such table: agent_logs" not in str(exc).lower():
                raise
            missing_log_session = None
        if missing_log_session is not None:
            raise RuntimeError(f"same-day paid agent log has no cost-circuit session: {missing_log_session['run_id']}")

    def _seed_today(self, conn: sqlite3.Connection) -> None:
        """Seed pre-deployment spend and attempts exactly once per ET day."""

        day, utc_start, utc_end = _et_day_and_utc_bounds()
        exists = conn.execute("SELECT 1 FROM llm_budget_days WHERE day = ?", (day,)).fetchone()
        if exists:
            self._validate_accounting_invariants(conn, day)
            return

        # agent_logs predates the breaker.  Seed its actual reported spend so
        # deploying mid-day cannot reset the budget to zero.
        try:
            unknown_expr = _unknown_cost_row_expr(conn)
            row = conn.execute(
                "SELECT COALESCE(SUM(cost_usd), 0) AS cost, "
                f"COALESCE(SUM({unknown_expr}), 0) "
                "AS unknown_cost_rows "
                "FROM agent_logs WHERE timestamp BETWEEN ? AND ?",
                (utc_start, utc_end),
            ).fetchone()
            baseline = float(row["cost"] or 0.0)
            unknown_cost_rows = int(row["unknown_cost_rows"] or 0)
            legacy = conn.execute(
                "SELECT run_id, COALESCE(SUM(cost_usd), 0) AS cost, "
                "COUNT(*) AS calls, "
                f"COALESCE(SUM({unknown_expr}), 0) "
                "AS unknown_cost_rows FROM agent_logs "
                "WHERE timestamp BETWEEN ? AND ? GROUP BY run_id",
                (utc_start, utc_end),
            ).fetchall()
        except sqlite3.OperationalError as exc:
            # A brand-new standalone breaker DB may legitimately predate the
            # application's agent_logs table. Locks, I/O failures, malformed
            # schema, and every other OperationalError are accounting failures
            # and must fail closed rather than seed a fictitious $0 day.
            if "no such table: agent_logs" not in str(exc).lower():
                raise
            baseline, unknown_cost_rows, legacy = 0.0, 0, []

        conn.execute(
            "INSERT OR IGNORE INTO llm_budget_days"
            "(day, baseline_cost_usd, unknown_cost_rows, costs_exact, seeded_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (day, baseline, unknown_cost_rows, int(unknown_cost_rows == 0), _sql_stamp(), _sql_stamp()),
        )
        for row in legacy:
            run_id = str(row["run_id"])
            calls = int(row["calls"] or 0)
            conn.execute(
                "INSERT OR IGNORE INTO llm_budget_sessions "
                "(run_id, day, mode, actual_cost_usd, logical_calls, "
                "provider_attempts, costs_exact, status, started_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 'legacy', ?, ?)",
                (
                    run_id,
                    day,
                    _legacy_mode(run_id),
                    float(row["cost"] or 0),
                    calls,
                    calls,
                    int(int(row["unknown_cost_rows"] or 0) == 0),
                    _sql_stamp(),
                    _sql_stamp(),
                ),
            )
        self._validate_accounting_invariants(conn, day)

    def activate_session(self, run_id: str, mode: str) -> dict[str, Any]:
        """Set call context and register a run without blocking safety work."""

        self._session_context.set((run_id, mode))
        self._sync_emergency_latch()
        with self._infrastructure_lock:
            sentinel = self._unavailable_sentinel
        if sentinel is not None:
            return sentinel.activate_session(run_id, mode)
        if not self.enabled:
            return {"suspended": False, "enabled": False}
        day, _, _ = _et_day_and_utc_bounds()

        def _activate() -> None:
            with self._connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                self._seed_today(conn)
                conn.execute(
                    "INSERT OR IGNORE INTO llm_budget_sessions(run_id, day, mode, started_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (run_id, day, mode, _sql_stamp(), _sql_stamp()),
                )
                self._reconcile_quota_holds_locked(conn, current_day=day)
                conn.commit()

        try:
            self._run_with_infra_retry(
                _activate,
                agent_name="circuit_activation",
                run_id=run_id,
                mode=mode,
            )
        except Exception:
            # `_run_with_infra_retry` already latched durably after
            # exhausting retries. Fall through to the sentinel path below
            # (deterministic/broker safety work must still proceed) instead
            # of raising out of session activation.
            with self._infrastructure_lock:
                sentinel = self._unavailable_sentinel
            if sentinel is not None:
                return sentinel.activate_session(run_id, mode)
            raise
        self._notify_if_needed()
        # Existing overspend must latch immediately, but never raises here:
        # callers still need to run deterministic/broker safety functions.
        self.enforce_current_limits(agent_name="session_start")
        return self.status()

    def set_session_context(self, run_id: str, mode: str) -> None:
        """Bind this call's run/mode WITHOUT the validating seed-and-check path.

        `activate_session` re-seeds and validates the current day's ledger
        before returning, which is exactly right for every normal caller:
        a broken ledger latches immediately, before any paid call can be
        authorized against it.  `scripts/cost_circuit.py reset` is the one
        legitimate exception.  On 2026-08-28 an operator ran that script to
        clear a hard latch and `main()`'s unconditional `activate_session()`
        call re-validated the day's ledger and raised before `reset` was
        ever dispatched -- the tool meant to clear the emergency was itself
        blocked by it, and the reset had to be done by hand from a Python
        shell instead. `reset()` never depends on the seeded/validated
        state (it reads the `llm_circuit_state` singleton row and the
        emergency-latch file directly, not `_seed_today`), so all this needs
        to do is set the run/mode context `reset()`'s audit trail reads --
        deliberately nothing else.
        """
        self._session_context.set((run_id, mode))

    def _context(self) -> tuple[str, str]:
        return self._session_context.get()
