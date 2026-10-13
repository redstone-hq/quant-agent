"""src.cost_circuit.parts.operator_controls -- OperatorControls for the cost circuit (status, operator reset).

Bodies moved verbatim from the former src/cost_circuit/breaker_operator.py (now held by LLMCostCircuitBreaker) (originally src/cost_circuit.py).
Every collaborator is an explicit keyword-only constructor argument.
"""

from __future__ import annotations
from typing import Any, Callable, TypeVar
from src.cost_circuit.clock import _et_day_and_utc_bounds, _sql_stamp
from src.cost_circuit.schema import ensure_cost_circuit_schema


class OperatorControls:
    def __init__(
        self,
        *,
        enabled,
        context,
        connect,
        infrastructure_lock,
        emergency_latch_path,
        sync_emergency_latch,
        seed_today,
        reconcile_quota_holds_locked,
        effective_state_locked,
        totals,
        notify_if_needed,
        state_row,
        emergency_file_lock,
        notify_auto_resets_if_needed,
        read_unavailable_sentinel,
        write_unavailable_sentinel,
        read_infrastructure_error,
        write_infrastructure_error,
    ) -> None:
        self.enabled = enabled
        self._context = context
        self._connect = connect
        self._infrastructure_lock = infrastructure_lock
        self._emergency_latch_path = emergency_latch_path
        self._sync_emergency_latch = sync_emergency_latch
        self._seed_today = seed_today
        self._reconcile_quota_holds_locked = reconcile_quota_holds_locked
        self._effective_state_locked = effective_state_locked
        self._totals = totals
        self._notify_if_needed = notify_if_needed
        self._state_row = state_row
        self._emergency_file_lock = emergency_file_lock
        self._notify_auto_resets_if_needed = notify_auto_resets_if_needed
        self.read_unavailable_sentinel = read_unavailable_sentinel
        self.write_unavailable_sentinel = write_unavailable_sentinel
        self.read_infrastructure_error = read_infrastructure_error
        self.write_infrastructure_error = write_infrastructure_error

    @property
    def _unavailable_sentinel(self):
        """Read live off the host: the bodies read it after calls that install it."""
        return self.read_unavailable_sentinel()

    @_unavailable_sentinel.setter
    def _unavailable_sentinel(self, value) -> None:
        """Written through to the host under the same lock the body already holds."""
        self.write_unavailable_sentinel(value)

    @property
    def _infrastructure_error(self):
        """Read live off the host: the bodies read it after calls that install it."""
        return self.read_infrastructure_error()

    @_infrastructure_error.setter
    def _infrastructure_error(self, value) -> None:
        """Written through to the host under the same lock the body already holds."""
        self.write_infrastructure_error(value)

    def status(self) -> dict[str, Any]:
        if not self.enabled:
            return {"enabled": False, "suspended": False}
        self._sync_emergency_latch()
        with self._infrastructure_lock:
            sentinel = self._unavailable_sentinel
        if sentinel is not None:
            return sentinel.status()
        day, _, _ = _et_day_and_utc_bounds()
        run_id, mode = self._context()
        with self._connect() as conn:
            ensure_cost_circuit_schema(conn)
            conn.execute("BEGIN IMMEDIATE")
            self._seed_today(conn)
            self._reconcile_quota_holds_locked(conn, current_day=day)
            state = self._effective_state_locked(
                conn,
                day=day,
                run_id=run_id,
                mode=mode,
            )
            daily, session = self._totals(conn, day, run_id)
            session_row = conn.execute(
                "SELECT logical_calls, provider_attempts, retry_attempts FROM llm_budget_sessions WHERE run_id=?",
                (run_id,),
            ).fetchone()
            result = dict(state)
            result.update(
                enabled=True,
                suspended=bool(state.get("suspended")),
                current_day=day,
                current_run_id=run_id,
                current_mode=mode,
                current_session_cost_usd=session,
                current_daily_cost_usd=daily,
                logical_calls=int(session_row["logical_calls"] if session_row else 0),
                provider_attempts=int(session_row["provider_attempts"] if session_row else 0),
                retry_attempts=int(session_row["retry_attempts"] if session_row else 0),
            )
        self._notify_if_needed()
        return result

    def reset(self, reason: str) -> None:
        """Operator-only manual reset. A reason is mandatory and audited.

        Also clears an INEXACT current ET day, which is the other fault only
        an operator can resolve. `fail_call` stamps `costs_exact=0` when it
        charges a conservative reserve for a request whose true cost it could
        not learn, and nothing sets it back within the day -- the flag clears
        only when the next ET day seeds a fresh row. Meanwhile
        `_reconcile_quota_holds_locked` REFUSES to rearm over an inexact day,
        by design.

        Until 2026-08-31 those two facts never met, because an inexact day
        was always accompanied by a hard latch, and the reconciler returns
        early whenever one is set -- the latch was, in effect, masking the
        refusal. Scoping `provider_attempt_limit` to the session removed that
        latch and exposed the real shape of it: an inexact day with no latch
        made every subsequent `activate_session` raise, with no operator
        action able to clear it, until ET rollover. A crash loop is a worse
        failure than the suspension it replaced.

        What this does NOT do is erase settled spend. The day's recorded
        amount is left exactly as it stands -- including the conservative
        reserve charged for the unresolved request, which over-states cost
        rather than under-stating it. Only the "we could not prove this
        figure" flag is cleared, and only by a named operator giving a
        reason. Recomputing what the provider really charged is not
        something this code can honestly do.

        Deciding that a conservative figure is good enough to continue on
        was, until 2026-09-23, described here as "precisely an operator's
        call" full stop. That is now true with one bounded exception, and
        the sentence is amended rather than left to disagree with the code:
        `_auto_clear_transient_latch_locked` makes the same decision without
        a human for rows this circuit itself booked as FAILED calls, and
        only those, under the guards listed on that method -- a cooldown, a
        finite daily allowance, spend under both caps, and every unknown row
        on every day the latch spans being one of that kind. Real unmeasured
        spend from a call that SUCCEEDED, an integrity fault, and the
        durable infrastructure latch are all still this method's alone. The
        argument for the exception is in `docs/INCIDENT_HISTORY.md` under
        2026-09-22: four operator resets of that one class on the live
        circuit, not one of them following a budget breach.
        """

        reason = reason.strip()
        if not reason:
            raise ValueError("a non-empty reset reason is required")
        run_id, mode = self._context()
        # The OS lock serializes reset against infrastructure-failure writers.
        # If a new failure starts after this reset, it acquires the lock next
        # and its marker survives; reset can never delete a concurrent marker.
        with self._emergency_file_lock():
            with self._connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                state = self._state_row(conn)
                emergency_latched = bool(self._emergency_latch_path is not None and self._emergency_latch_path.exists())
                current_day, _, _ = _et_day_and_utc_bounds()
                day_row = conn.execute(
                    "SELECT unknown_cost_rows, costs_exact FROM llm_budget_days WHERE day=?",
                    (current_day,),
                ).fetchone()
                day_inexact = day_row is not None and (
                    int(day_row["unknown_cost_rows"] or 0) or not bool(day_row["costs_exact"])
                )
                if not int(state.get("suspended") or 0) and not emergency_latched and not day_inexact:
                    raise ValueError(
                        "no operator-resettable hard circuit is active and the "
                        "current ET day's accounting is exact; scoped quota "
                        "holds expire only with their budget window"
                    )
                # Same observability as `_auto_clear_transient_latch_locked`,
                # and derived the same way: the elapsed figure comes from the
                # singleton's own `suspended_at` against SQLite's clock, with
                # no threshold of its own invented here. `suspended_at` is
                # NULL whenever this reset is clearing an inexact day or a
                # durable marker with no live suspension, and in that case no
                # duration is appended at all rather than a zero or a guess.
                suspended_at = state.get("suspended_at")
                elapsed_minutes: float | None = None
                if suspended_at:
                    elapsed_row = conn.execute(
                        "SELECT (julianday(?) - julianday(?)) * 1440.0 AS minutes",
                        (_sql_stamp(), suspended_at),
                    ).fetchone()
                    if elapsed_row is not None and elapsed_row["minutes"] is not None:
                        elapsed_minutes = float(elapsed_row["minutes"])
                event_detail = reason
                if elapsed_minutes is not None:
                    event_detail = (
                        f"{reason} [operator reset after a suspension of "
                        f"{elapsed_minutes:.1f} min, latched at "
                        f"{suspended_at} UTC]"
                    )
                conn.execute(
                    "INSERT INTO llm_circuit_events "
                    "(event_type, trigger_code, detail, run_id, mode, agent_name, attempts, "
                    "session_cost_usd, daily_cost_usd, suspension_alert_state) VALUES "
                    "('reset', ?, ?, ?, ?, 'operator', ?, ?, ?, ?)",
                    (
                        state.get("trigger_code"),
                        event_detail,
                        run_id,
                        mode,
                        int(state.get("session_attempts") or 0),
                        float(state.get("session_cost_usd") or 0),
                        float(state.get("daily_cost_usd") or 0),
                        # Item 174 pairing, for the same reason as the auto
                        # path: the UPDATE below zeroes `alert_state`, so this
                        # is the last moment it is knowable whether the owner
                        # ever received the matching "SUSPENDED" note. The
                        # column DEFAULTs to 1 ("he was told"), which for a
                        # manual reset was a claim nothing had checked.
                        int(state.get("alert_state") or 0),
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
                    raise RuntimeError("cost-circuit singleton could not be reset")
                if day_inexact:
                    # The amount is untouched on purpose -- see the docstring.
                    # Only the unprovable-figure flag is cleared, so the
                    # reconciler can rearm and the desk can continue on a
                    # conservative number the operator has accepted.
                    conn.execute(
                        "UPDATE llm_budget_days SET unknown_cost_rows=0, "
                        "failed_call_unknown_rows=0, "
                        "costs_exact=1, updated_at=? WHERE day=?",
                        (_sql_stamp(), current_day),
                    )
                conn.commit()
            # DB opens first while the marker still blocks every process; the
            # unlink is the final operator-authorized transition.
            if self._emergency_latch_path is not None:
                self._emergency_latch_path.unlink(missing_ok=True)
                if self._emergency_latch_path.exists():
                    raise RuntimeError(f"could not clear durable circuit latch {self._emergency_latch_path}")
        with self._infrastructure_lock:
            self._infrastructure_error = None
            self._unavailable_sentinel = None
        # Tell the owner the desk is back, on the same footing as the
        # automatic path (which fires this from `_notify_if_needed`). Outside
        # the file lock and the transaction on purpose: the reset is already
        # durable, and a Telegram outage must not roll it back. The claim /
        # retry state machine in there makes a second call a no-op, and a row
        # whose suspension note never reached the owner resolves as unpaired
        # instead of announcing a recovery from an incident he never heard of.
        self._notify_auto_resets_if_needed()
