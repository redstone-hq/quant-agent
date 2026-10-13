"""src.cost_circuit.parts.quota_holds -- Quota holds for the cost circuit (reconcile, hold, latched-snapshot refresh, trip).

Bodies moved verbatim from the former src/cost_circuit/breaker_holds.py shim (originally src/cost_circuit.py); held by LLMCostCircuitBreaker.
Every collaborator is an explicit keyword-only constructor argument.
"""

from __future__ import annotations
import sqlite3
from datetime import date, datetime, time as dt_time, timezone
from src.cost_circuit.clock import _sql_stamp
from src.cost_circuit.classification import _trigger_scope


class QuotaHolds:
    def __init__(
        self,
        *,
        config,
        auto_clear_transient_latch_locked,
        scope_key,
        state_row,
        hold_quota_locked=None,
        trip_locked=None,
    ) -> None:
        self.config = config
        self._auto_clear_transient_latch_locked = auto_clear_transient_latch_locked
        self._scope_key = scope_key
        self._state_row = state_row
        # The bodies below are themselves moved; a caller passes one only when it was
        # swapped on the host (see the shim), otherwise this object's own run.
        if hold_quota_locked is not None:
            self._hold_quota_locked = hold_quota_locked
        if trip_locked is not None:
            self._trip_locked = trip_locked

    def _reconcile_quota_holds_locked(
        self,
        conn: sqlite3.Connection,
        *,
        current_day: str,
    ) -> None:
        """Rearm completed ET-day quota windows without weakening hard faults."""

        # Defect B (2026-09-22): a transient provider latch expires on its
        # own. Placed here because every authorization boundary
        # (activate_session / enforce_current_limits / begin_call /
        # before_provider_attempt / status) already calls this reconciler
        # inside its own write transaction, so one insertion covers them all
        # and none of them can observe a stale latch.
        self._auto_clear_transient_latch_locked(conn, current_day=current_day)

        hard_state = self._state_row(conn)
        if int(hard_state.get("suspended") or 0):
            return

        # Item 14 (2026-09-02): the reservation layer, and every cross-day
        # reservation-reconciliation check that used to live here, is gone.
        # There is no in-flight dollar exposure that can outlive a day
        # boundary any more -- only settled cost, which the day/session rows
        # already carry forward correctly across rollover.
        day_row = conn.execute(
            "SELECT unknown_cost_rows, costs_exact FROM llm_budget_days WHERE day=?",
            (current_day,),
        ).fetchone()
        if day_row is None:
            raise RuntimeError(f"cost-circuit day accounting row is missing for {current_day}")
        current_date = date.fromisoformat(current_day)

        cross_day_holds = conn.execute(
            "SELECT * FROM llm_quota_holds WHERE active=1 AND day<>? ORDER BY id",
            (current_day,),
        ).fetchall()

        # The exactness precondition guards ONE operation: releasing a hold
        # carried over from an earlier ET day. Rearming yesterday's stop while
        # today's books are unproven is what it exists to prevent, and it
        # still does.
        #
        # It used to be checked before this query, so it fired even when there
        # was no cross-day hold to rearm -- gating an operation that was not
        # being performed. That made it a booby trap on the ordinary path,
        # because this reconciler runs on EVERY `begin_call`, and `fail_call`
        # stamps the day inexact whenever it charges a conservative reserve
        # for a request whose true cost it never learned. So the FIRST such
        # failure in a day poisoned every paid call after it: the raise is
        # read as the circuit's own infrastructure failing, which writes the
        # emergency latch and stops the desk until an operator clears it.
        #
        # One rate-limited request, and the trading day was over. That is the
        # 2026-08-26/27/28/31 pattern, and it survived four rounds of fixing
        # limits because nobody was looking at the reconciler -- the earlier
        # hard latches masked it, since this function returns early whenever
        # one is set. Removing the last of those masks (see the NOTE by
        # _SESSION_QUOTA_TRIGGERS) is what finally showed it, on the live desk
        # at 10:36 ET on 2026-08-31, as a crash instead of a suspension.
        #
        # Scope restored to what it protects: no cross-day hold, nothing to
        # rearm, nothing to be exact about.
        if cross_day_holds and (int(day_row["unknown_cost_rows"] or 0) or not bool(day_row["costs_exact"])):
            raise RuntimeError(f"cost-circuit cannot rearm {current_day}: current-day accounting is not exact")
        holds = []
        for hold in cross_day_holds:
            try:
                hold_date = date.fromisoformat(str(hold["day"]))
            except ValueError:
                hold_date = None
            if hold_date is None or hold_date > current_date:
                self._trip_locked(
                    conn,
                    code="non_monotonic_quota_hold_day",
                    detail=(
                        f"active {hold['scope']} quota hold {hold['id']} has budget "
                        f"day {hold['day']!s} while the current ET day is {current_day}; "
                        "clock regression or accounting corruption requires operator review"
                    ),
                    run_id=str(hold["run_id"]),
                    mode=str(hold["mode"]),
                    agent_name="budget_rollover",
                    attempts=int(hold["attempts"] or 0),
                    attempts_exact=bool(hold["attempts_exact"]),
                    costs_exact=False,
                    session_cost=float(hold["session_cost_usd"] or 0.0),
                    daily_cost=float(hold["daily_cost_usd"] or 0.0),
                )
                return
            holds.append(hold)
        for hold in holds:
            reason = (
                f"ET budget window advanced from {hold['day']} to {current_day}; "
                "exact ledger and accounting invariants passed"
            )
            updated = conn.execute(
                "UPDATE llm_quota_holds SET active=0, released_at=?, "
                "release_reason=?, recovery_alert_state=? WHERE id=? AND active=1",
                (
                    _sql_stamp(),
                    reason,
                    0 if hold["scope"] in {"day", "mode_day"} else 1,
                    hold["id"],
                ),
            )
            if updated.rowcount != 1:
                continue
            conn.execute(
                "INSERT INTO llm_circuit_events "
                "(event_type, trigger_code, detail, run_id, mode, agent_name, attempts, "
                "session_cost_usd, daily_cost_usd) VALUES "
                "('quota_rearmed', ?, ?, ?, ?, 'budget_rollover', ?, ?, ?)",
                (
                    hold["trigger_code"],
                    reason,
                    hold["run_id"],
                    hold["mode"],
                    hold["attempts"],
                    hold["session_cost_usd"],
                    hold["daily_cost_usd"],
                ),
            )

    def _hold_quota_locked(
        self,
        conn: sqlite3.Connection,
        *,
        scope: str,
        code: str,
        detail: str,
        day: str,
        run_id: str,
        mode: str,
        agent_name: str,
        attempts: int,
        attempts_exact: bool,
        costs_exact: bool,
        session_cost: float,
        daily_cost: float,
    ) -> bool:
        scope_key = self._scope_key(
            scope,
            day=day,
            run_id=run_id,
            mode=mode,
        )
        existing = conn.execute(
            "SELECT id FROM llm_quota_holds WHERE active=1 AND scope=? AND scope_key=? AND day=?",
            (scope, scope_key, day),
        ).fetchone()
        if existing is not None:
            return False
        conn.execute(
            "INSERT INTO llm_quota_holds "
            "(scope, scope_key, day, trigger_code, trigger_detail, run_id, mode, "
            "agent_name, attempts, attempts_exact, costs_exact, session_cost_usd, "
            "daily_cost_usd, session_limit_usd, daily_limit_usd) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                scope,
                scope_key,
                day,
                code,
                detail,
                run_id,
                mode,
                agent_name,
                attempts,
                int(attempts_exact),
                int(costs_exact),
                session_cost,
                daily_cost,
                float(self.config.session_cost_limit_usd),
                float(self.config.daily_cost_limit_usd),
            ),
        )
        conn.execute(
            "INSERT INTO llm_circuit_events "
            "(event_type, trigger_code, detail, run_id, mode, agent_name, attempts, "
            "session_cost_usd, daily_cost_usd) VALUES "
            "('quota_held', ?, ?, ?, ?, ?, ?, ?, ?)",
            (code, detail, run_id, mode, agent_name, attempts, session_cost, daily_cost),
        )
        updated_session = conn.execute(
            "UPDATE llm_budget_sessions SET status='quota_held', updated_at=? WHERE run_id=?",
            (_sql_stamp(), run_id),
        )
        if updated_session.rowcount != 1:
            raise RuntimeError(f"cost-circuit could not mark missing session {run_id} quota-held")
        return True

    def _refresh_latched_snapshot_locked(self, conn: sqlite3.Connection) -> None:
        """Refresh alert totals after concurrent/in-flight accounting settles.

        The first failure owns the trigger identity, but its dollar snapshot
        must not remain frozen while another already-authorized request settles
        its real cost in the same sweep.
        """

        state = self._state_row(conn)
        if not int(state.get("suspended") or 0):
            return
        run_id = str(state.get("run_id") or "")
        session_row = conn.execute(
            "SELECT day, actual_cost_usd, provider_attempts, costs_exact FROM llm_budget_sessions WHERE run_id=?",
            (run_id,),
        ).fetchone()
        if session_row is None:
            raise RuntimeError(f"cost-circuit session row is missing for latched run {run_id}")
        day = str(session_row["day"])
        day_row = conn.execute(
            "SELECT baseline_cost_usd + incremental_cost_usd AS cost, costs_exact FROM llm_budget_days WHERE day=?",
            (day,),
        ).fetchone()
        if day_row is None:
            raise RuntimeError(f"cost-circuit day row is missing for latched run {run_id}")
        session_cost = float(session_row["actual_cost_usd"] or 0.0)
        daily_cost = float(day_row["cost"] if day_row else 0.0)
        costs_exact = (
            bool(state.get("costs_exact", 1))
            and bool(session_row["costs_exact"])
            and bool(day_row["costs_exact"] if day_row else 1)
        )
        if bool(state.get("attempts_exact", 1)):
            attempts = int(session_row["provider_attempts"] or 0)
        else:
            attempts = int(state.get("session_attempts") or 0)
        updated = conn.execute(
            "UPDATE llm_circuit_state SET session_attempts=?, session_cost_usd=?, "
            "daily_cost_usd=?, costs_exact=? "
            "WHERE singleton=1 AND suspended=1",
            (attempts, session_cost, daily_cost, int(costs_exact)),
        )
        if updated.rowcount != 1:
            raise RuntimeError("cost-circuit latched snapshot could not be updated")

    def _trip_locked(
        self,
        conn: sqlite3.Connection,
        *,
        code: str,
        detail: str,
        run_id: str,
        mode: str,
        agent_name: str,
        attempts: int,
        attempts_exact: bool = True,
        costs_exact: bool = True,
        session_cost: float,
        daily_cost: float,
    ) -> bool:
        """Apply the narrowest safe stop. Unknown triggers are hard latches."""

        scope = _trigger_scope(code)
        if scope != "hard":
            session_row = conn.execute("SELECT day FROM llm_budget_sessions WHERE run_id=?", (run_id,)).fetchone()
            if session_row is None:
                raise RuntimeError(f"cost-circuit session row is missing while holding {run_id}")
            return self._hold_quota_locked(
                conn,
                scope=scope,
                code=code,
                detail=detail,
                day=str(session_row["day"]),
                run_id=run_id,
                mode=mode,
                agent_name=agent_name,
                attempts=attempts,
                attempts_exact=attempts_exact,
                costs_exact=costs_exact,
                session_cost=session_cost,
                daily_cost=daily_cost,
            )

        current = self._state_row(conn)
        if int(current.get("suspended") or 0):
            return False
        updated_state = conn.execute(
            "UPDATE llm_circuit_state SET suspended=1, trigger_code=?, "
            "trigger_detail=?, run_id=?, mode=?, agent_name=?, "
            "session_attempts=?, attempts_exact=?, costs_exact=?, "
            "session_cost_usd=?, daily_cost_usd=?, "
            "session_limit_usd=?, daily_limit_usd=?, suspended_at=?, "
            "alert_state=0, updated_at=? WHERE singleton=1",
            (
                code,
                detail,
                run_id,
                mode,
                agent_name,
                attempts,
                int(attempts_exact),
                int(costs_exact),
                session_cost,
                daily_cost,
                float(self.config.session_cost_limit_usd),
                float(self.config.daily_cost_limit_usd),
                _sql_stamp(),
                _sql_stamp(),
            ),
        )
        if updated_state.rowcount != 1:
            raise RuntimeError("cost-circuit singleton could not be latched")
        conn.execute(
            "INSERT INTO llm_circuit_events "
            "(event_type, trigger_code, detail, run_id, mode, agent_name, attempts, "
            "session_cost_usd, daily_cost_usd) VALUES "
            "('suspended', ?, ?, ?, ?, ?, ?, ?, ?)",
            (code, detail, run_id, mode, agent_name, attempts, session_cost, daily_cost),
        )
        updated_session = conn.execute(
            "UPDATE llm_budget_sessions SET status='suspended', updated_at=? WHERE run_id=?", (_sql_stamp(), run_id)
        )
        if updated_session.rowcount != 1:
            raise RuntimeError(f"cost-circuit could not mark missing session {run_id} suspended")
        return True
