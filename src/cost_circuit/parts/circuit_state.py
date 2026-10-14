"""src.cost_circuit.parts.circuit_state -- Effective-state reads for the cost circuit (settled totals, state row, quota-hold lookup, transient-latch auto-clear).

Bodies moved verbatim from the former src/cost_circuit/breaker_state.py shim (originally src/cost_circuit.py); held by LLMCostCircuitBreaker.
Every collaborator is an explicit keyword-only constructor argument.
"""

from __future__ import annotations
import logging
import sqlite3
from typing import Any, Callable, TypeVar
from src.cost_circuit.classification import _SELF_CLEARING_HARD_TRIGGERS
from src.cost_circuit.refusal import _fmt_settled
from src.cost_circuit.clock import _et_day_and_utc_bounds, _et_day_from_sqlite_utc, _sql_stamp
from src.cost_circuit.free_route import daily_cap_applies

logger = logging.getLogger(__name__)


class CircuitState:
    def __init__(
        self,
        *,
        config,
        context,
        emergency_latch_path,
        totals=None,
        state_row=None,
        active_quota_hold_locked=None,
    ) -> None:
        self.config = config
        self._context = context
        self._emergency_latch_path = emergency_latch_path
        # The bodies below are themselves moved; a caller passes one only when it was
        # swapped on the host (see the shim), otherwise this object's own run.
        if totals is not None:
            self._totals = totals
        if state_row is not None:
            self._state_row = state_row
        if active_quota_hold_locked is not None:
            self._active_quota_hold_locked = active_quota_hold_locked

    @staticmethod
    def _totals(conn: sqlite3.Connection, day: str, run_id: str) -> tuple[float, float]:
        """Real settled (daily, session) spend. No reservation component --
        item 14 (2026-09-02) deleted the reservation layer entirely."""

        day_row = conn.execute(
            "SELECT baseline_cost_usd + incremental_cost_usd AS cost FROM llm_budget_days WHERE day = ?", (day,)
        ).fetchone()
        session_row = conn.execute(
            "SELECT actual_cost_usd FROM llm_budget_sessions WHERE run_id = ?", (run_id,)
        ).fetchone()
        if day_row is None:
            raise RuntimeError(f"cost-circuit day accounting row is missing for {day}")
        daily = float(day_row["cost"] if day_row else 0.0)
        session = float(session_row["actual_cost_usd"] if session_row else 0.0)
        return daily, session

    def _state_row(self, conn: sqlite3.Connection) -> dict[str, Any]:
        row = conn.execute("SELECT * FROM llm_circuit_state WHERE singleton=1").fetchone()
        if row is None:
            raise RuntimeError("cost-circuit singleton state row is missing")
        return dict(row)

    @staticmethod
    def _scope_key(scope: str, *, day: str, run_id: str, mode: str) -> str:
        if scope == "day":
            return day
        if scope == "mode_day":
            return f"{day}:{mode}"
        if scope == "session":
            return run_id
        raise ValueError(f"unsupported quota scope: {scope}")

    def _active_quota_hold_locked(
        self,
        conn: sqlite3.Connection,
        *,
        day: str,
        run_id: str,
        mode: str,
    ) -> dict[str, Any] | None:
        # A session exempted from the DAILY money cap (free_route.daily_cap_applies)
        # does not see the day-wide daily_cost_limit hold; every other hold still binds.
        day_scope = "scope='day'" if daily_cap_applies(mode) else "(scope='day' AND trigger_code<>'daily_cost_limit')"
        row = conn.execute(
            "SELECT * FROM llm_quota_holds WHERE active=1 AND day=? AND ("
            + day_scope
            + " OR (scope='mode_day' AND scope_key=?) OR "
            "(scope='session' AND scope_key=?)) "
            "ORDER BY CASE scope WHEN 'day' THEN 1 WHEN 'mode_day' THEN 2 ELSE 3 END, "
            "id LIMIT 1",
            (day, f"{day}:{mode}", run_id),
        ).fetchone()
        return dict(row) if row is not None else None

    def _effective_state_locked(
        self,
        conn: sqlite3.Connection,
        *,
        day: str,
        run_id: str,
        mode: str,
    ) -> dict[str, Any]:
        state = self._state_row(conn)
        if int(state.get("suspended") or 0):
            state.update(
                suspended=True,
                suspension_class="hard",
                hold_scope="global",
                requires_operator_reset=True,
                auto_rearm=False,
            )
            return state
        hold = self._active_quota_hold_locked(
            conn,
            day=day,
            run_id=run_id,
            mode=mode,
        )
        if hold is None:
            state.update(
                suspended=False,
                suspension_class=None,
                hold_scope=None,
                requires_operator_reset=False,
                auto_rearm=False,
            )
            return state
        state.update(
            suspended=True,
            suspension_class="quota",
            hold_scope=hold["scope"],
            hold_day=hold["day"],
            trigger_code=hold["trigger_code"],
            trigger_detail=hold["trigger_detail"],
            run_id=hold["run_id"],
            mode=hold["mode"],
            agent_name=hold["agent_name"],
            session_attempts=hold["attempts"],
            attempts_exact=hold["attempts_exact"],
            costs_exact=hold["costs_exact"],
            session_cost_usd=hold["session_cost_usd"],
            daily_cost_usd=hold["daily_cost_usd"],
            session_limit_usd=hold["session_limit_usd"],
            daily_limit_usd=hold["daily_limit_usd"],
            suspended_at=hold["created_at"],
            requires_operator_reset=False,
            auto_rearm=hold["scope"] in {"day", "mode_day"},
        )
        return state

    def _auto_clear_transient_latch_locked(
        self,
        conn: sqlite3.Connection,
        *,
        current_day: str,
    ) -> bool:
        """Expire a hard latch raised by a transient provider failure.

        Defect B (2026-09-22). A latch of the `_SELF_CLEARING_HARD_TRIGGERS`
        class -- a provider call that FAILED with an unprovable cost -- used
        to sit until a human noticed. It never once sat because money had
        actually run out: all four such latches on the production DB were
        cleared by an operator with spend well under cap [measured]. On
        2026-09-22 that cost the close run, the evening run and nine hours
        of refused analysis on 29% of the daily budget.

        WHAT THIS DELIBERATELY DOES NOT DO. It never moves a dollar and
        never touches a cap: settled spend on the day and the session rows
        is left exactly as recorded, `self.config`'s limits are only read,
        and `_enforce_settled_limits_locked` -- which runs at every
        authorization boundary, including immediately after this on the same
        connection -- re-latches instantly if real spend is over the line.
        The only thing cleared is the "we could not prove this figure" flag
        on rows this circuit itself booked as failed calls.

        Every one of these must hold, or nothing happens:

        1. The circuit is suspended on a hard trigger in
           `_SELF_CLEARING_HARD_TRIGGERS`. Any other code -- an integrity
           fault, a completed call with unknown cost, anything unrecognized
           -- keeps the durable operator-reset latch, unchanged.
        2. No durable emergency file latch exists. That path means the
           circuit's own infrastructure failed, so nothing it computes may
           be trusted to authorize its own recovery; it stays operator-only.
        3. Every unknown row is a failed-call row, checked on EVERY day the
           latch spans -- the ET day it was stamped on and today. A latch
           can outlive midnight (the 2026-09-22 one did, 15:17 UTC to 00:33
           UTC), and today's row is freshly seeded with zero unknown rows,
           so checking only today would wave through a latch whose actual
           unproven rows sit on yesterday. One row of any other provenance,
           on either day, and it waits for a human. A `failed_call` counter
           ABOVE the total it is a subset of is corruption, and also
           refuses -- deliberately as a refusal to self-clear rather than
           as an accounting invariant, so a bug in this counter falls back
           to the operator instead of escalating to the emergency latch.
        4. `transient_latch_cooldown_minutes` of wall clock has elapsed
           since `suspended_at`, measured by SQLite against the same clock
           that wrote it. A negative elapsed time (clock regression) does
           not qualify.
        5. The day's auto-clear allowance is not spent. A fault recurring
           past that is not transient, and the next occurrence latches
           durably -- which is also what bounds how many unproven-cost calls
           one day can forgive without a human.
        6. Settled day and session spend are both strictly under their caps
           already. This never reopens into a breach.

        Returns True when the latch was cleared.
        """

        state = self._state_row(conn)
        if not int(state.get("suspended") or 0):
            return False
        code = state.get("trigger_code")
        if code not in _SELF_CLEARING_HARD_TRIGGERS:
            return False
        if self._emergency_latch_path is not None and self._emergency_latch_path.exists():
            return False

        suspended_at = state.get("suspended_at")
        if not suspended_at:
            return False

        # Every ET day this latch spans, not just today. `_seed_today`
        # creates today's row once, with zero unknown rows, so a latch that
        # outlived midnight would otherwise be judged against a clean slate
        # while its real unproven rows sat on yesterday -- and the incident
        # this fix exists for is exactly one that crossed midnight.
        spanned_days = {current_day}
        latch_day = _et_day_from_sqlite_utc(str(suspended_at))
        if latch_day is None:
            # An unparseable stamp is not a day this can vouch for.
            return False
        spanned_days.add(latch_day)
        failed_call_rows = 0
        for day in sorted(spanned_days):
            day_row = conn.execute(
                "SELECT unknown_cost_rows, failed_call_unknown_rows FROM llm_budget_days WHERE day=?",
                (day,),
            ).fetchone()
            if day_row is None:
                if day == current_day:
                    return False
                # The stamped day predates this DB's accounting entirely;
                # there is nothing recorded to vouch for either way.
                return False
            unknown = int(day_row["unknown_cost_rows"] or 0)
            failed = int(day_row["failed_call_unknown_rows"] or 0)
            if unknown != failed:
                # Covers both "a row of another provenance" and the
                # corruption case where the subset exceeds its total.
                return False
            failed_call_rows += failed
        elapsed_row = conn.execute(
            "SELECT (julianday(?) - julianday(?)) * 1440.0 AS minutes",
            (_sql_stamp(), suspended_at),
        ).fetchone()
        elapsed = elapsed_row["minutes"] if elapsed_row is not None else None
        cooldown = float(getattr(self.config, "transient_latch_cooldown_minutes", 15.0))
        if elapsed is None or float(elapsed) < cooldown:
            return False

        _, utc_start, utc_end = _et_day_and_utc_bounds()
        already = conn.execute(
            "SELECT COUNT(*) AS n FROM llm_circuit_events WHERE event_type='auto_reset' AND created_at BETWEEN ? AND ?",
            (utc_start, utc_end),
        ).fetchone()
        allowance = int(getattr(self.config, "max_transient_latch_auto_clears_per_day", 14))
        used = int(already["n"] if already else 0)
        if used >= allowance:
            logger.error(
                "cost-circuit refusing to auto-clear %s: %d auto-clear(s) "
                "already used today of an allowance of %d. A fault recurring "
                "this often is not transient; an operator must look.",
                code,
                used,
                allowance,
            )
            return False

        run_id, mode = self._context()
        daily, session_cost = self._totals(conn, current_day, run_id)
        daily_limit = float(self.config.daily_cost_limit_usd)
        session_limit = float(self.config.session_cost_limit_usd)
        if daily >= daily_limit or session_cost >= session_limit:
            return False

        reason = (
            f"transient provider latch {code} auto-expired after "
            f"{float(elapsed):.1f} min (cooldown {cooldown:.0f} min); "
            f"{failed_call_rows} failed-call row(s) of unproven cost forgiven, "
            f"settled spend untouched at {_fmt_settled(daily)} of a "
            f"${daily_limit:.2f} day cap and {_fmt_settled(session_cost)} of "
            f"a ${session_limit:.2f} session cap; "
            f"auto-clear {used + 1} of {allowance} today"
        )
        conn.execute(
            "INSERT INTO llm_circuit_events "
            "(event_type, trigger_code, detail, run_id, mode, agent_name, attempts, "
            "session_cost_usd, daily_cost_usd, suspension_alert_state, recovery_alert_updated_at, created_at) VALUES "
            "('auto_reset', ?, ?, ?, ?, 'transient_latch_expiry', ?, ?, ?, ?, ?, ?)",
            (
                code,
                reason,
                run_id,
                mode,
                int(state.get("session_attempts") or 0),
                session_cost,
                daily,
                # Item 174 pairing: capture whether the owner actually received
                # the "SUSPENDED" note BEFORE the UPDATE below resets
                # alert_state to 0. After that write the answer is gone, and
                # the suspension alert itself becomes undeliverable (its claim
                # requires suspended=1), so this is the last moment it is
                # knowable.
                int(state.get("alert_state") or 0),
                _sql_stamp(),
                _sql_stamp(),
            ),
        )
        updated_state = conn.execute(
            "UPDATE llm_circuit_state SET suspended=0, trigger_code=NULL, "
            "trigger_detail=NULL, run_id=NULL, mode=NULL, agent_name=NULL, "
            "session_attempts=0, session_cost_usd=0, daily_cost_usd=0, "
            "attempts_exact=1, costs_exact=1, "
            "suspended_at=NULL, alert_state=0, reset_at=?, "
            "reset_reason=?, updated_at=? WHERE singleton=1",
            (_sql_stamp(), reason, _sql_stamp()),
        )
        if updated_state.rowcount != 1:
            raise RuntimeError("cost-circuit singleton could not be auto-cleared")
        if failed_call_rows:
            # Exactly the operator-reset treatment, and for the same reason
            # (see `reset`): the recorded AMOUNT is untouched, only the
            # unprovable-figure flag goes, so the reconciler can rearm.
            #
            # `llm_budget_sessions` is deliberately NOT touched, which keeps
            # this strictly weaker than the operator path rather than
            # stronger. An adversary pass on 2026-09-23 found an earlier
            # draft flipping 'call_failed' sessions back to 'active' and
            # clearing their costs_exact: that enforced nothing (only the
            # DAY-level unknown row gates anything) and made
            # `_canonical_run_cost` in src/api/db_reads.py report a
            # by-construction-unknown run cost to the dashboard as an exact
            # dollar figure. The session row stays as the record of what
            # happened.
            #
            # CURRENT DAY ONLY, exactly the rows `reset` writes. A second
            # adversary pass on 2026-09-23 caught an earlier draft clearing
            # every spanned day: nothing reads a PAST day's flags (every
            # consumer keys on the current ET day), so the write bought
            # nothing, and it stamped costs_exact=1 on a historical day
            # whose cost was never proven -- rewriting settled history to
            # look provable is the same defect as the session-row rewrite,
            # relocated. The spanned-day CHECK above stays: reading
            # yesterday to decide is the safety, writing it is not.
            conn.execute(
                "UPDATE llm_budget_days SET unknown_cost_rows=0, "
                "failed_call_unknown_rows=0, costs_exact=1, "
                "updated_at=? WHERE day=?",
                (_sql_stamp(), current_day),
            )
        logger.warning(
            "cost-circuit auto-cleared hard latch %s: %s",
            code,
            reason,
        )
        return True
