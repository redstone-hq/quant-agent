"""src.cost_circuit.parts.settlement -- Settlement for the cost circuit (before_provider_attempt, complete_call, fail_call).

Bodies moved verbatim from the former src/cost_circuit/breaker_settlement.py (now held by LLMCostCircuitBreaker) (originally src/cost_circuit.py).
Every collaborator is an explicit keyword-only constructor argument.
"""

from __future__ import annotations
import logging
from src.cost_circuit.classification import _all_attempts_provably_free
from src.cost_circuit.refusal import (
    CallReservation,
    OUT_OF_CREDIT_DETAIL,
    out_of_credit_detail,
    OUT_OF_CREDIT_TRIGGER_CODE,
    PaidAnalysisSuspended,
    any_payment_refusal,
)
from src.cost_circuit.clock import _et_day_and_utc_bounds
from src.cost_circuit.free_route import admit_free_route_locked

logger = logging.getLogger(__name__)


class Settlement:
    def __init__(
        self,
        *,
        config,
        enabled,
        connect,
        infrastructure_lock,
        raise_if_unavailable,
        seed_today,
        reconcile_quota_holds_locked,
        effective_state_locked,
        notify_if_needed,
        totals,
        enforce_settled_limits_locked,
        trip_locked,
        refresh_latched_snapshot_locked,
        sync_emergency_latch,
        read_unavailable_sentinel,
    ) -> None:
        self.config = config
        self.enabled = enabled
        self._connect = connect
        self._infrastructure_lock = infrastructure_lock
        self._raise_if_unavailable = raise_if_unavailable
        self._seed_today = seed_today
        self._reconcile_quota_holds_locked = reconcile_quota_holds_locked
        self._effective_state_locked = effective_state_locked
        self._notify_if_needed = notify_if_needed
        self._totals = totals
        self._enforce_settled_limits_locked = enforce_settled_limits_locked
        self._trip_locked = trip_locked
        self._refresh_latched_snapshot_locked = refresh_latched_snapshot_locked
        self._sync_emergency_latch = sync_emergency_latch
        self.read_unavailable_sentinel = read_unavailable_sentinel

    @property
    def _unavailable_sentinel(self):
        """Read live off the host: the bodies read it after calls that install it."""
        return self.read_unavailable_sentinel()

    def before_provider_attempt(self, reservation: CallReservation, *, model: str) -> int:
        """Authorize one actual provider request; return this call's attempt number.

        Item 14 (2026-09-02): no reservation to look up or extend. Attempt
        counting WITHIN this one logical call is tracked on `reservation`
        itself (in-process; see `CallReservation`'s docstring) purely to
        bound `max_provider_attempts_per_call` -- a retry/failover-storm
        guard independent of the item-14c per-session call-count backstop.
        """

        if not self.enabled or reservation.reservation_id == "disabled":
            return 1
        self._raise_if_unavailable(reservation.agent_name)
        day, _, _ = _et_day_and_utc_bounds()
        next_attempt = reservation.attempt_count + 1
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            # A call can wait behind the provider semaphore while a
            # different process settles spend or accounting is damaged.
            # The provider authorization boundary must therefore re-seed/
            # validate the complete current-day ledger in the same write
            # transaction -- trusting only state read at begin_call would
            # leave a fail-open window immediately before network I/O.
            self._seed_today(conn)
            self._reconcile_quota_holds_locked(conn, current_day=day)
            state = self._effective_state_locked(
                conn,
                day=day,
                run_id=reservation.run_id,
                mode=reservation.mode,
            )
            if int(state.get("suspended") or 0) and not admit_free_route_locked(
                conn,
                day=day,
                run_id=reservation.run_id,
                mode=reservation.mode,
                agent_name=reservation.agent_name,
                model=model,
                boundary="before_provider_attempt",
                log=False,
            ):
                conn.commit()
                self._notify_if_needed()
                raise PaidAnalysisSuspended(str(state.get("trigger_detail") or "circuit open"), state)

            session = conn.execute("SELECT * FROM llm_budget_sessions WHERE run_id=?", (reservation.run_id,)).fetchone()
            if session is None:
                raise RuntimeError(f"cost-circuit session accounting row is missing for run {reservation.run_id}")
            session_attempts = int(session["provider_attempts"] or 0)
            daily, session_cost = self._totals(conn, day, reservation.run_id)

            # (b): real settled caps, rechecked immediately before network
            # I/O -- an earlier call in this same session/day can have
            # settled its real cost while this one waited.
            self._enforce_settled_limits_locked(
                conn,
                day=day,
                run_id=reservation.run_id,
                mode=reservation.mode,
                agent_name=reservation.agent_name,
                attempts=session_attempts,
                attempts_exact=True,
                daily=daily,
                session=session_cost,
            )
            state = self._effective_state_locked(
                conn,
                day=day,
                run_id=reservation.run_id,
                mode=reservation.mode,
            )
            if int(state.get("suspended") or 0) and not admit_free_route_locked(
                conn,
                day=day,
                run_id=reservation.run_id,
                mode=reservation.mode,
                agent_name=reservation.agent_name,
                model=model,
                boundary="before_provider_attempt",
                log=False,
            ):
                conn.commit()
                self._notify_if_needed()
                raise PaidAnalysisSuspended(str(state.get("trigger_detail") or "circuit open"), state)

            max_per_call = int(self.config.max_provider_attempts_per_call)
            if next_attempt > max_per_call:
                self._trip_locked(
                    conn,
                    code="provider_attempt_limit",
                    detail=(
                        f"{reservation.agent_name} provider attempt {next_attempt} "
                        f"exceeds per-call safe limit {max_per_call}"
                    ),
                    run_id=reservation.run_id,
                    mode=reservation.mode,
                    agent_name=reservation.agent_name,
                    attempts=session_attempts,
                    session_cost=session_cost,
                    daily_cost=daily,
                    costs_exact=True,
                )
                state = self._effective_state_locked(
                    conn,
                    day=day,
                    run_id=reservation.run_id,
                    mode=reservation.mode,
                )
                conn.commit()
                self._notify_if_needed()
                raise PaidAnalysisSuspended(str(state.get("trigger_detail") or "circuit open"), state)

            updated_session = conn.execute(
                "UPDATE llm_budget_sessions SET provider_attempts=provider_attempts+1, "
                "updated_at=datetime('now') WHERE run_id=?",
                (reservation.run_id,),
            )
            if updated_session.rowcount != 1:
                raise RuntimeError(f"cost-circuit session {reservation.run_id} disappeared at authorization")
            conn.commit()
        reservation.attempt_count = next_attempt
        return next_attempt

    def complete_call(
        self,
        reservation: CallReservation,
        actual_cost_usd: float | None,
        *,
        actual_model: str | None = None,
        failed_attempt_errors: list[BaseException] | None = None,
    ) -> None:
        """Account the ACTUAL provider-reported cost of a completed call.

        Item 14 (2026-09-02): no reservation to release and no estimate to
        reconcile -- only the real number the provider returned is ever
        added to the ledger. `actual_cost_usd is None` means no usable
        cost/token telemetry came back at all; that is recorded as unknown
        spend and latches the circuit outright (continuing would make the
        daily/session totals fiction), same as before.

        `failed_attempt_errors` -- attempts on THIS logical call that did
        NOT produce the response being settled here (a retried primary, a
        failed primary whose failover then succeeded) -- no longer adds a
        dollar charge: there is no reservation left to estimate one from.
        A winner that reports a real `cost_usd` keeps the day/session
        exact: the booked increment is that number, not a guess and not an
        inexact flag. Marking exactness from one ambiguous prior attempt
        was a second lie about spend (2026-09-16). `unknown_cost_rows` and
        the `legacy_unknown_cost` latch stay reserved for a row whose OWN
        cost is missing (`actual_cost_usd is None`, or `fail_call` on a
        fully-failed ambiguous attempt). Caps are not raised; settled
        spend is not erased; operator reset remains the product for a
        real unknown row.
        """

        if not self.enabled or reservation.reservation_id == "disabled":
            return
        # Another process can persist the emergency sidecar while this
        # request is in flight.  Observe it before releasing an unaccounted
        # response into the decision pipeline.
        self._raise_if_unavailable(reservation.agent_name)
        with self._infrastructure_lock:
            sentinel = self._unavailable_sentinel
        if sentinel is not None:
            sentinel.require_paid_analysis(reservation.agent_name)
        day, _, _ = _et_day_and_utc_bounds()
        unknown = actual_cost_usd is None
        accounted = 0.0 if unknown else float(actual_cost_usd)
        # A completed call with provider-reported cost is an exact row even
        # when an earlier attempt on the SAME logical call was ambiguous.
        # Incrementing `unknown_cost_rows` here was the 2026-09-16 false
        # latch: a successful position_reviewer ($0.003861 real cost) tripped
        # `legacy_unknown_cost` and wiped every paid scan through close while
        # known spend was ~$0.65 of $2.75. Marking `costs_exact=0` on that
        # same winner was a second spend lie: the booked increment is the
        # provider's number. `unknown_cost_rows` is reserved for rows whose
        # OWN cost is missing (no telemetry, or fail_call on a fully-failed
        # ambiguous attempt) — not for a winner with a number.
        if failed_attempt_errors and not unknown:
            logger.info(
                "cost-circuit: %s completed at $%.6f after %d prior attempt(s); booking the winner as exact",
                reservation.agent_name,
                accounted,
                len(failed_attempt_errors),
            )
        exact = not unknown
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            updated_session = conn.execute(
                "UPDATE llm_budget_sessions SET actual_cost_usd=actual_cost_usd+?, "
                "costs_exact=CASE WHEN ? THEN costs_exact ELSE 0 END, "
                "updated_at=datetime('now') WHERE run_id=?",
                (accounted, int(exact), reservation.run_id),
            )
            if updated_session.rowcount != 1:
                raise RuntimeError(f"cost-circuit session {reservation.run_id} is missing at completion")
            updated_day = conn.execute(
                "UPDATE llm_budget_days SET incremental_cost_usd=incremental_cost_usd+?, "
                "unknown_cost_rows=unknown_cost_rows+?, "
                "costs_exact=CASE WHEN ? THEN costs_exact ELSE 0 END, "
                "updated_at=datetime('now') WHERE day=?",
                (
                    accounted,
                    int(unknown),
                    int(exact),
                    day,
                ),
            )
            if updated_day.rowcount != 1:
                raise RuntimeError(f"cost-circuit day {day} is missing at completion")
            daily, session_cost = self._totals(conn, day, reservation.run_id)
            session_row = conn.execute(
                "SELECT provider_attempts FROM llm_budget_sessions WHERE run_id=?",
                (reservation.run_id,),
            ).fetchone()
            attempts = int(session_row["provider_attempts"] or 0)
            if unknown:
                self._trip_locked(
                    conn,
                    code="unknown_actual_cost",
                    detail=(
                        f"{reservation.agent_name} returned no usable token/cost telemetry; "
                        "continuing cannot be budgeted safely"
                    ),
                    run_id=reservation.run_id,
                    mode=reservation.mode,
                    agent_name=reservation.agent_name,
                    attempts=attempts,
                    session_cost=session_cost,
                    daily_cost=daily,
                    costs_exact=False,
                )
            else:
                # (b): stop the instant REAL SETTLED spend -- this call's
                # actual reported cost included -- reaches either cap.
                # An inexact day from an ambiguous prior attempt does NOT
                # hard-latch here: the winner's cost is booked; remaining
                # caps still bind on the known minimum.
                self._enforce_settled_limits_locked(
                    conn,
                    day=day,
                    run_id=reservation.run_id,
                    mode=reservation.mode,
                    agent_name=reservation.agent_name,
                    attempts=attempts,
                    attempts_exact=True,
                    daily=daily,
                    session=session_cost,
                )
            self._refresh_latched_snapshot_locked(conn)
            conn.commit()
        self._notify_if_needed()

    def fail_call(
        self,
        reservation: CallReservation,
        error: BaseException,
        attempt_errors: list[BaseException] | None = None,
    ) -> None:
        """Account a failed provider request -- no reservation to convert
        into spend any more (item 14, 2026-09-02).

        A failure PROVEN to have cost $0 (`_is_known_zero_cost_failure`: an
        HTTP 429/400/401/403/404 rejection, or a pre-send transport failure
        -- DNS, connection refused, TLS handshake) is recorded as exactly
        that, $0, and changes nothing else. A provably-$0 rejection is not
        evidence the SESSION went wrong; it is evidence this one attempt
        cost nothing and the caller is free to retry.

        An AMBIGUOUS failure -- a cut stream, an unclassified error, a 5xx
        after generation may have started -- has a real, unknowable cost.
        This module no longer estimates one: it marks the day/session
        inexact (`unknown_cost_rows`) so `_enforce_settled_limits_locked`
        fails closed on it at the very next authorization boundary. Same
        fail-closed posture as before, without inventing a dollar figure
        for a request that may or may not have billed anything.

        Nothing is accounted at all if no provider attempt was ever made
        (`reservation.attempt_count == 0`, e.g. `before_provider_attempt`
        itself raised) -- there is nothing ambiguous about a request that
        never reached the network.

        `attempt_errors` is every provider attempt's failure on this
        logical call, not just the one the caller re-raised -- see
        `_all_attempts_provably_free`'s docstring for why that matters.
        """

        if not self.enabled or reservation.reservation_id == "disabled":
            return
        self._sync_emergency_latch()
        with self._infrastructure_lock:
            if self._unavailable_sentinel is not None:
                return
        attempted = reservation.attempt_count > 0
        ambiguous = attempted and not _all_attempts_provably_free(error, attempt_errors)
        day, _, _ = _et_day_and_utc_bounds()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if ambiguous:
                updated_session = conn.execute(
                    "UPDATE llm_budget_sessions SET status='call_failed', "
                    "costs_exact=0, updated_at=datetime('now') WHERE run_id=?",
                    (reservation.run_id,),
                )
            else:
                updated_session = conn.execute(
                    "UPDATE llm_budget_sessions SET updated_at=datetime('now') WHERE run_id=?",
                    (reservation.run_id,),
                )
            if updated_session.rowcount != 1:
                raise RuntimeError(f"cost-circuit session {reservation.run_id} is missing at failure")
            if ambiguous:
                updated_day = conn.execute(
                    "UPDATE llm_budget_days SET "
                    "unknown_cost_rows=unknown_cost_rows+1, "
                    # Provenance for the self-clear: this unknown row came
                    # from a call that FAILED, so its unproven cost is
                    # bounded by one attempt rather than being real
                    # unmeasured spend. Kept as a separate counter so
                    # `_auto_clear_transient_latch_locked` can refuse to
                    # forgive a day that also carries any other kind.
                    "failed_call_unknown_rows=failed_call_unknown_rows+1, "
                    "costs_exact=0, updated_at=datetime('now') WHERE day=?",
                    (day,),
                )
                if updated_day.rowcount != 1:
                    raise RuntimeError(f"cost-circuit day {day} is missing at failure")
            daily, session_cost = self._totals(conn, day, reservation.run_id)
            session = conn.execute(
                "SELECT provider_attempts FROM llm_budget_sessions WHERE run_id=?",
                (reservation.run_id,),
            ).fetchone()
            attempts = int(session["provider_attempts"] if session else 0)
            conn.execute(
                "INSERT INTO llm_circuit_events "
                "(event_type, detail, run_id, mode, agent_name, attempts, "
                "session_cost_usd, daily_cost_usd) VALUES "
                "('call_failed', ?, ?, ?, ?, ?, ?, ?)",
                (
                    type(error).__name__,
                    reservation.run_id,
                    reservation.mode,
                    reservation.agent_name,
                    attempts,
                    session_cost,
                    daily,
                ),
            )
            if ambiguous:
                # Name the cause when the desk KNOWS it. A payment refusal
                # is not an unbounded-cost mystery -- the provider refused
                # before generating anything -- and reporting it as one sent
                # the owner looking for a broken desk on 2026-10-01. The call
                # is still booked as unproven cost (the circuit is not
                # weakened); only the explanation changes.
                out_of_credit = any_payment_refusal(error, attempt_errors)
                if out_of_credit:
                    trip_code = OUT_OF_CREDIT_TRIGGER_CODE
                    trip_detail = f"paid analysis is off for {reservation.agent_name} because {out_of_credit_detail()}"
                else:
                    trip_code = "failed_call_unknown_cost"
                    trip_detail = (
                        f"{reservation.agent_name} failed after {attempts} provider "
                        "attempt(s) with no provable-zero-cost telemetry; the real "
                        "cost is unknown and cannot be bounded safely"
                    )
                self._trip_locked(
                    conn,
                    code=trip_code,
                    detail=trip_detail,
                    run_id=reservation.run_id,
                    mode=reservation.mode,
                    agent_name=reservation.agent_name,
                    attempts=attempts,
                    session_cost=session_cost,
                    daily_cost=daily,
                    costs_exact=False,
                )
            self._refresh_latched_snapshot_locked(conn)
            conn.commit()
        self._notify_if_needed()
