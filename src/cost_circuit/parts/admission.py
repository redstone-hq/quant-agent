"""src.cost_circuit.parts.admission -- Admission for the cost circuit (settled-limit enforcement, preflight, require_paid_analysis, begin_call).

Bodies moved verbatim from the former src/cost_circuit/breaker_admission.py (now held by LLMCostCircuitBreaker) (originally src/cost_circuit.py).
Every collaborator is an explicit keyword-only constructor argument.
"""

from __future__ import annotations
import sqlite3
import uuid
from typing import Any, Callable, TypeVar
from src.cost_circuit.refusal import (
    CallReservation,
    OptionalPaidAnalysisRetrySkipped,
    PaidAnalysisSuspended,
    _fmt_settled,
)
from src.cost_circuit.clock import _et_day_and_utc_bounds
from src.cost_circuit.free_route import admit_free_route_locked


class Admission:
    def __init__(
        self,
        *,
        config,
        enabled,
        connect,
        context,
        infrastructure_lock,
        effective_state_locked,
        notify_if_needed,
        raise_if_unavailable,
        reconcile_quota_holds_locked,
        run_with_infra_retry,
        seed_today,
        sync_emergency_latch,
        totals,
        trip_locked,
        status,
        read_unavailable_sentinel,
        enforce_settled_limits_locked=None,
        enforce_current_limits=None,
    ) -> None:
        self.config = config
        self.enabled = enabled
        self._connect = connect
        self._context = context
        self._infrastructure_lock = infrastructure_lock
        self._effective_state_locked = effective_state_locked
        self._notify_if_needed = notify_if_needed
        self._raise_if_unavailable = raise_if_unavailable
        self._reconcile_quota_holds_locked = reconcile_quota_holds_locked
        self._run_with_infra_retry = run_with_infra_retry
        self._seed_today = seed_today
        self._sync_emergency_latch = sync_emergency_latch
        self._totals = totals
        self._trip_locked = trip_locked
        self.status = status
        self.read_unavailable_sentinel = read_unavailable_sentinel
        # The bodies below are themselves moved; a caller passes one only when it was
        # swapped on the host (see the shim), otherwise this object's own run.
        if enforce_settled_limits_locked is not None:
            self._enforce_settled_limits_locked = enforce_settled_limits_locked
        if enforce_current_limits is not None:
            self.enforce_current_limits = enforce_current_limits

    @property
    def _unavailable_sentinel(self):
        """Read live off the host: the bodies read it after calls that install it."""
        return self.read_unavailable_sentinel()

    def _enforce_settled_limits_locked(
        self,
        conn: sqlite3.Connection,
        *,
        day: str,
        run_id: str,
        mode: str,
        agent_name: str,
        attempts: int,
        attempts_exact: bool,
        daily: float,
        session: float,
    ) -> None:
        """Latch already-consumed/unknown spend inside the caller's transaction.

        This check belongs at every authorization boundary, not just pipeline
        preflight. In particular, an operator reset does not erase settled
        spend, so a direct or already-running caller must not get a brief
        opportunity to spend above the unchanged cap.
        """

        day_row = conn.execute(
            "SELECT unknown_cost_rows, costs_exact FROM llm_budget_days WHERE day=?",
            (day,),
        ).fetchone()
        session_row = conn.execute(
            "SELECT costs_exact FROM llm_budget_sessions WHERE run_id=?",
            (run_id,),
        ).fetchone()
        unknown_cost_rows = int(day_row["unknown_cost_rows"] if day_row else 0)
        daily_exact = bool(day_row["costs_exact"] if day_row else True)
        session_exact = bool(session_row["costs_exact"] if session_row else True)

        if unknown_cost_rows > 0:
            self._trip_locked(
                conn,
                code="legacy_unknown_cost",
                detail=(
                    f"{unknown_cost_rows} same-day row(s) (pre-deployment agent "
                    "logs, or a fully-failed call with unknown cost — see "
                    "fail_call / complete_call with no telemetry) have unknown "
                    "cost; daily spend cannot be bounded safely"
                ),
                run_id=run_id,
                mode=mode,
                agent_name=agent_name,
                attempts=attempts,
                attempts_exact=attempts_exact,
                costs_exact=False,
                session_cost=session,
                daily_cost=daily,
            )
        elif daily >= float(self.config.daily_cost_limit_usd):
            self._trip_locked(
                conn,
                code="daily_cost_limit",
                detail=(
                    f"daily LLM spend {_fmt_settled(daily)} reached safe limit "
                    f"${float(self.config.daily_cost_limit_usd):.2f}"
                ),
                run_id=run_id,
                mode=mode,
                agent_name=agent_name,
                attempts=attempts,
                attempts_exact=attempts_exact,
                session_cost=session,
                daily_cost=daily,
                costs_exact=daily_exact,
            )
        elif session >= float(self.config.session_cost_limit_usd):
            self._trip_locked(
                conn,
                code="session_cost_limit",
                detail=(
                    f"session LLM spend {_fmt_settled(session)} reached safe limit "
                    f"${float(self.config.session_cost_limit_usd):.2f}"
                ),
                run_id=run_id,
                mode=mode,
                agent_name=agent_name,
                attempts=attempts,
                attempts_exact=attempts_exact,
                session_cost=session,
                daily_cost=daily,
                costs_exact=session_exact,
            )

    def enforce_current_limits(self, agent_name: str = "preflight") -> dict[str, Any]:
        """Latch on already-consumed daily/session budgets; never raises."""

        if not self.enabled:
            return {"suspended": False, "enabled": False}
        self._sync_emergency_latch()
        with self._infrastructure_lock:
            sentinel = self._unavailable_sentinel
        if sentinel is not None:
            return sentinel.enforce_current_limits(agent_name)
        run_id, mode = self._context()
        day, _, _ = _et_day_and_utc_bounds()

        def _enforce() -> None:
            with self._connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                self._seed_today(conn)
                self._reconcile_quota_holds_locked(conn, current_day=day)
                daily, session = self._totals(conn, day, run_id)
                row = conn.execute(
                    "SELECT provider_attempts, status FROM llm_budget_sessions WHERE run_id=?",
                    (run_id,),
                ).fetchone()
                attempts = int(row["provider_attempts"] if row else 0)
                attempts_exact = not (row and row["status"] == "legacy")
                self._enforce_settled_limits_locked(
                    conn,
                    day=day,
                    run_id=run_id,
                    mode=mode,
                    agent_name=agent_name,
                    attempts=attempts,
                    attempts_exact=attempts_exact,
                    daily=daily,
                    session=session,
                )
                conn.commit()

        try:
            self._run_with_infra_retry(
                _enforce,
                agent_name=agent_name,
                run_id=run_id,
                mode=mode,
            )
        except Exception:
            # Latched durably already by `_run_with_infra_retry`. This
            # method's contract is "never raises" -- defer to the sentinel
            # it just installed rather than propagate.
            with self._infrastructure_lock:
                sentinel = self._unavailable_sentinel
            if sentinel is not None:
                return sentinel.enforce_current_limits(agent_name)
            raise
        self._notify_if_needed()
        return self.status()

    def require_paid_analysis(self, agent_name: str = "analysis", model: str | None = None) -> None:
        """Raise if analysis is suspended; safe to call after broker work.

        `model` is the agent's primary route. When it is priced $0 and the only
        active stops are money caps, the call is admitted (see free_route).
        """

        if not self.enabled:
            return
        self.enforce_current_limits(agent_name=agent_name)
        state = self.status()
        if state.get("suspended"):
            if model is not None and self._free_route_admitted(agent_name, model):
                return
            self._notify_if_needed()
            raise PaidAnalysisSuspended(str(state.get("trigger_detail") or "circuit open"), state)

    def _free_route_admitted(self, agent_name: str, model: str) -> bool:
        """Same rule as begin_call, for the pipeline preflight. Fails closed."""

        self._raise_if_unavailable(agent_name)
        run_id, mode = self._context()
        day, _, _ = _et_day_and_utc_bounds()
        with self._connect() as conn:
            return admit_free_route_locked(
                conn,
                day=day,
                run_id=run_id,
                mode=mode,
                agent_name=agent_name,
                model=model,
                boundary="require_paid_analysis",
            )

    def begin_call(
        self,
        *,
        agent_name: str,
        model: str,
        system_prompt: str,
        user_message: str,
        max_output_tokens: int,
        retry_kind: str | None = None,
        optional_retry: bool = False,
    ) -> CallReservation:
        """Authorize one logical call.

        Item 14 (2026-09-02): no reservation is computed or held. `system_
        prompt`/`user_message`/`max_output_tokens` are accepted purely for
        call-site compatibility -- nothing here prices them. Two checks
        gate the call: (b) real settled cost already recorded today/this
        session against the configured caps (`_enforce_settled_limits_
        locked`, using ONLY what `complete_call`/`fail_call` have already
        recorded -- no projection), and (c) this session's call count
        against `max_calls_per_session`, the runaway-loop backstop.
        """

        if not self.enabled:
            run_id, mode = self._context()
            return CallReservation("disabled", run_id, mode, agent_name, model)
        self._raise_if_unavailable(agent_name)
        run_id, mode = self._context()
        day, _, _ = _et_day_and_utc_bounds()
        reservation_id = uuid.uuid4().hex

        with self._connect() as conn:
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
                "SELECT provider_attempts, logical_calls, retry_attempts FROM llm_budget_sessions WHERE run_id=?",
                (run_id,),
            ).fetchone()
            attempts = int(session_row["provider_attempts"] if session_row else 0)
            logical_calls = int(session_row["logical_calls"] if session_row else 0)

            if int(state.get("suspended") or 0) and not admit_free_route_locked(
                conn,
                day=day,
                run_id=run_id,
                mode=mode,
                agent_name=agent_name,
                model=model,
                boundary="begin_call",
            ):
                conn.commit()
                self._notify_if_needed()
                raise PaidAnalysisSuspended(str(state.get("trigger_detail") or "circuit open"), state)

            if session_row is None:
                raise RuntimeError(f"cost-circuit session accounting row is missing for run {run_id}")

            # (b): Reset clears the latch, not historical spend.  Recheck the
            # REAL settled-cost ceilings in this same write transaction so
            # neither a direct caller nor an already-running job can spend
            # through the gap between reset and a later pipeline preflight.
            self._enforce_settled_limits_locked(
                conn,
                day=day,
                run_id=run_id,
                mode=mode,
                agent_name=agent_name,
                attempts=attempts,
                attempts_exact=True,
                daily=daily,
                session=session,
            )
            state = self._effective_state_locked(
                conn,
                day=day,
                run_id=run_id,
                mode=mode,
            )
            if int(state.get("suspended") or 0) and not admit_free_route_locked(
                conn,
                day=day,
                run_id=run_id,
                mode=mode,
                agent_name=agent_name,
                model=model,
                boundary="begin_call",
                log=False,
            ):
                conn.commit()
                self._notify_if_needed()
                raise PaidAnalysisSuspended(str(state.get("trigger_detail") or "circuit open"), state)

            # (c): runaway-loop backstop by call COUNT, independent of price.
            max_calls = int(self.config.max_calls_per_session)
            if logical_calls + 1 > max_calls:
                detail = (
                    f"session has already made {logical_calls} call(s); the next "
                    f"{agent_name} call would be call {logical_calls + 1}, above "
                    f"the runaway-loop backstop of {max_calls} calls/session"
                )
                if optional_retry:
                    conn.commit()
                    raise OptionalPaidAnalysisRetrySkipped(
                        detail,
                        {
                            "run_id": run_id,
                            "mode": mode,
                            "agent_name": agent_name,
                            "retry_kind": retry_kind,
                            "logical_calls": logical_calls,
                            "max_calls_per_session": max_calls,
                            "trigger_code": "optional_retry_budget_exhausted",
                        },
                    )
                self._trip_locked(
                    conn,
                    code="session_call_count_limit",
                    detail=detail,
                    run_id=run_id,
                    mode=mode,
                    agent_name=agent_name,
                    attempts=attempts,
                    session_cost=session,
                    daily_cost=daily,
                    costs_exact=True,
                )
                state = self._effective_state_locked(
                    conn,
                    day=day,
                    run_id=run_id,
                    mode=mode,
                )
                conn.commit()
                self._notify_if_needed()
                raise PaidAnalysisSuspended(str(state.get("trigger_detail") or detail), state)

            updated_session = conn.execute(
                "UPDATE llm_budget_sessions SET logical_calls=logical_calls+1, "
                "retry_attempts=retry_attempts+?, updated_at=datetime('now') "
                "WHERE run_id=?",
                (1 if retry_kind else 0, run_id),
            )
            if updated_session.rowcount != 1:
                raise RuntimeError(f"cost-circuit session row disappeared while authorizing call for {run_id}")
            conn.commit()
        return CallReservation(reservation_id, run_id, mode, agent_name, model)
