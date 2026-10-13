"""src.cost_circuit.alert_ledger -- moved verbatim from src/cost_circuit.py; see the package docstring."""

from __future__ import annotations
import logging
import json
import os
import fcntl
import threading
import time
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import date, datetime, time as dt_time, timezone
from pathlib import Path
from typing import Any, Callable, TypeVar
from src.cost_circuit.refusal import CallReservation, PaidAnalysisSuspended, _fmt_settled
from src.trading_calendar import utc_now
from src.cost_circuit.alert_outcome import _send_alert_outcome

logger = logging.getLogger(__name__)


@contextmanager
def _file_lock(lock_path: Path | None):
    """Serialize concurrent writers to one filesystem sidecar.

    Shared by `LLMCostCircuitBreaker`'s own emergency-latch writer/reset and
    `UnavailableLLMCostCircuit`'s alert-outcome bookkeeping below -- the same
    lock file, so a latch write/reset can never race an alert-outcome update.
    """

    if lock_path is None:
        yield
        return
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def _read_alert_outcome(latch_path: Path | None) -> tuple[bool, int, bool]:
    """Best-effort read of (alert_delivered, alert_attempts, alert_suppressed).

    Never raises: an unreadable/missing/corrupt marker reads as "not yet
    delivered, zero attempts" rather than blocking anything.
    """

    if latch_path is None or not latch_path.exists():
        return False, 0, False
    try:
        payload = json.loads(latch_path.read_text(encoding="utf-8"))
    except Exception:
        return False, 0, False
    if not isinstance(payload, dict):
        return False, 0, False
    delivered = payload.get("alert_delivered") is True
    suppressed = payload.get("alert_suppressed") is True
    try:
        attempts = int(payload.get("alert_attempts") or 0)
    except (TypeError, ValueError):
        attempts = 0
    return delivered, max(0, attempts), suppressed


def _record_alert_attempt(
    latch_path: Path | None,
    lock_path: Path | None,
    *,
    delivered: bool,
    suppressed: bool = False,
    now: datetime | None = None,
) -> int:
    """Durably fold one alert-send outcome into the emergency latch file.

    docs/WORK.md item 17(b): a Telegram send failure for the "paid analysis
    suspended" alert used to be tracked only in the in-process sentinel
    (`UnavailableLLMCostCircuit._alert_delivered`) -- if the process exited
    (or was never touched again) before a retry succeeded, the failure was
    both terminal and invisible: no record survived to let a later run
    retry it or to let anything else notice. This writes the outcome into
    the SAME durable JSON marker `mark_unavailable` already writes for the
    latch itself (the one filesystem fact guaranteed to exist and to be
    independent of whatever database fault caused the latch in the first
    place), under the shared `_file_lock`, so:
      * a later process re-reading this file (`_read_alert_outcome`) knows
        immediately whether the operator was ever actually told, and keeps
        retrying until `alert_delivered` is true;
      * `alert_attempts` is a durable, cross-restart count exposed through
        `UnavailableLLMCostCircuit._state()` / `status()` -- a visible
        surface (Mission Control / session summaries already read
        `status()`) that keeps climbing for as long as delivery keeps
        failing, rather than the failure being silently dropped after one
        in-memory attempt.

    Never raises and never invents a marker: if the latch file does not
    exist yet (e.g. this is running against a ":memory:" breaker with no
    filesystem sidecar), this is a no-op -- there is nothing durable to
    update, matching how `mark_unavailable` itself degrades for that case.
    Returns the resulting attempt count on success, or -1 if the update
    could not be made durable (caller logs that distinctly).
    """

    if latch_path is None:
        return -1
    with _file_lock(lock_path):
        try:
            if not latch_path.exists():
                return -1
            payload = json.loads(latch_path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                return -1
            attempts = int(payload.get("alert_attempts") or 0) + 1
            payload["alert_attempts"] = attempts
            # Once delivered, stays delivered -- a later failed retry of a
            # SECOND, unrelated alert attempt (there should not be one, but
            # never regress a true fact back to false).
            payload["alert_delivered"] = bool(payload.get("alert_delivered")) or bool(delivered)
            # Third state, never folded into delivered: a dropped message was not told.
            payload["alert_suppressed"] = bool(payload.get("alert_suppressed")) or bool(suppressed)
            payload["last_alert_attempt_at"] = (now or utc_now()).isoformat()
            tmp = latch_path.with_name(f".{latch_path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    handle.write(json.dumps(payload, sort_keys=True) + "\n")
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(tmp, latch_path)
            finally:
                try:
                    tmp.unlink(missing_ok=True)
                except OSError:
                    pass
            return attempts
        except Exception:
            logger.exception(
                "Could not durably record cost-circuit alert-delivery outcome at %s",
                latch_path,
            )
            return -1


def _durable_alert_surface_ok(latch_path: Path | None) -> bool:
    """True when a mandatory alert can be DURABLY RECORDED and SURFACED.

    The mandatory-alert requirement is that an operator can always find out
    that paid analysis was suspended -- not that one particular transport is
    switched on.  The durable JSON latch sidecar written by
    `mark_unavailable` (and folded with delivery attempts by
    `_record_alert_attempt`) is that record, and it is already surfaced to
    the operator by the API/dashboard: `src/api/db_reads.py`
    `get_llm_circuit_health()` reads this very file and `routes_live.py`
    reports it as `decision_path_status=degraded_cost_circuit_unavailable`.

    So the precondition is "this sidecar can be written", never "Telegram is
    enabled".  Muting a notification channel must not be able to suspend the
    desk; having nowhere at all to record a mandatory alert still must.
    """

    if latch_path is None:
        return False
    try:
        parent = latch_path.parent
        return parent.is_dir() and os.access(parent, os.W_OK)
    except OSError:
        return False


class UnavailableLLMCostCircuit:
    """Fail-closed sentinel when persistent breaker infrastructure is broken.

    It deliberately never raises during session activation, so broker and
    deterministic safety work can proceed.  Every paid boundary raises.
    """

    def __init__(
        self,
        error: BaseException,
        notifier: Any | None = None,
        *,
        run_id: str = "unscoped",
        mode: str = "unknown",
        agent_name: str = "circuit_infrastructure",
        attempts: int | None = None,
        attempts_exact: bool = False,
        session_cost_usd: float | None = None,
        daily_cost_usd: float | None = None,
        costs_exact: bool = False,
        emergency_latch_path: Path | None = None,
        emergency_lock_path: Path | None = None,
    ):
        self.error = error
        if notifier is None:
            from src.notifier.owner_alert_funnel import build_default_notifier

            notifier = build_default_notifier()
        self.notifier = notifier
        self.agent_name = agent_name
        self.trigger_run_id = run_id
        self.trigger_mode = mode
        self.attempts = attempts
        self.attempts_exact = attempts_exact
        self.session_cost_usd = session_cost_usd
        self.daily_cost_usd = daily_cost_usd
        self.costs_exact = costs_exact
        # item 17(b): the filesystem marker `mark_unavailable` already wrote
        # for THIS incident -- the one durable fact this sentinel can lean
        # on to remember whether the operator has actually been told, across
        # both same-process retries and a fresh process after a restart.
        # None (e.g. a ":memory:" breaker with no filesystem sidecar, or one
        # of the defensive fallback sentinels pipeline.py builds without a
        # breaker instance) degrades to the old in-memory-only behaviour.
        self._emergency_latch_path = emergency_latch_path
        self._emergency_lock_path = emergency_lock_path
        self._context_value: ContextVar[tuple[str, str]] = ContextVar(
            f"qamc_unavailable_cost_session_{id(self)}",
            default=(run_id, mode),
        )
        self._alert_lock = threading.Lock()
        durably_delivered, durable_attempts, durably_suppressed = _read_alert_outcome(self._emergency_latch_path)
        self._alert_suppressed = durably_suppressed
        self._alert_delivered = durably_delivered
        self._alert_attempts = durable_attempts
        self._last_alert_attempt = 0.0

    def _trigger(self) -> str:
        return f"mandatory paid-analysis circuit is unavailable: {type(self.error).__name__}: {str(self.error)[:300]}"

    def _state(self) -> dict[str, Any]:
        current_run_id, current_mode = self._context_value.get()
        return {
            "enabled": True,
            "available": False,
            "suspended": True,
            "trigger_code": "circuit_infrastructure_unavailable",
            "trigger_detail": self._trigger(),
            "run_id": self.trigger_run_id,
            "mode": self.trigger_mode,
            "current_run_id": current_run_id,
            "current_mode": current_mode,
            "agent_name": self.agent_name,
            "session_attempts": self.attempts,
            "attempts_exact": self.attempts_exact,
            "costs_exact": self.costs_exact,
            "costs_available": (self.session_cost_usd is not None or self.daily_cost_usd is not None),
            "session_cost_usd": self.session_cost_usd or 0.0,
            "daily_cost_usd": self.daily_cost_usd or 0.0,
            # item 17(b): visible, durable proof of whether the operator has
            # actually been told about this latch -- read by anything that
            # consumes `status()` (Mission Control, session summaries), not
            # just this process's own logs.
            "alert_delivered": self._alert_delivered,
            # Dropped on purpose (risk-only filter / hard mute): settled, NOT delivered.
            "alert_suppressed": self._alert_suppressed,
            "alert_attempts": self._alert_attempts,
        }

    def _alert(self) -> None:
        now = time.monotonic()
        with self._alert_lock:
            if self._alert_delivered or self._alert_suppressed:
                return
            # Telegram/network outages are retryable, but do not hammer the
            # endpoint on every agent boundary in a parallel fan-out.
            if self._last_alert_attempt and now - self._last_alert_attempt < 120:
                return
            self._last_alert_attempt = now
        state = self._state()
        if self.attempts is None:
            attempts_line = "attempts: unavailable because a mandatory circuit prerequisite failed"
        elif self.attempts_exact:
            attempts_line = f"attempts: {self.attempts} provider attempt{'s' if self.attempts != 1 else ''}"
        else:
            attempts_line = (
                f"attempts: at least {self.attempts} locally observed provider "
                f"attempt{'s' if self.attempts != 1 else ''}"
            )
        if self.session_cost_usd is None and self.daily_cost_usd is None:
            cost_line = "cost: unavailable; the failed mandatory circuit prerequisite prevented a trustworthy snapshot"
        else:
            session_text = _fmt_settled(self.session_cost_usd)
            daily_text = _fmt_settled(self.daily_cost_usd)
            qualifier = "" if self.costs_exact else " (known/conservative snapshot)"
            cost_line = f"cost: {session_text} this run · {daily_text} today{qualifier}"
        message = (
            "🔴 QAMC PAID ANALYSIS SUSPENDED\n"
            f"trigger: {state['trigger_detail']}\n"
            f"affected run: {state['run_id']} ({state['mode']} / {self.agent_name})\n"
            f"{attempts_line}\n"
            f"{cost_line}\n"
            "suspended: all paid LLM analysis, repairs, retries, and provider failover\n"
            "preserved: broker-resident stops, order/fill reconciliation, deterministic "
            "loss protection, close/P&L jobs, and the read-only API\n"
            "operator reset is required after restoring the failed prerequisite; "
            "restart any long-lived worker that observed this emergency latch."
        )
        logger.critical("\n%s", message)
        delivered, suppressed = _send_alert_outcome(
            self.notifier,
            message,
            "cost-circuit unavailable Telegram alert failed",
        )
        sent = delivered or suppressed  # a drop is settled, but tracked apart
        # item 17(b): fold this outcome into the SAME durable marker the
        # latch itself lives in, so it survives this process exiting before
        # a retry succeeds -- see `_record_alert_attempt`'s docstring.
        durable_attempts = _record_alert_attempt(
            self._emergency_latch_path,
            self._emergency_lock_path,
            delivered=delivered,
            suppressed=suppressed,
        )
        with self._alert_lock:
            if delivered:
                self._alert_delivered = True
            if suppressed:
                self._alert_suppressed = True
            if durable_attempts >= 0:
                self._alert_attempts = durable_attempts
            else:
                self._alert_attempts += 1
        if sent:
            return
        if durable_attempts < 0:
            logger.critical(
                "cost-circuit unavailable alert was not delivered to Telegram, "
                "AND the durable delivery-outcome record could not be updated "
                "-- this failure is tracked only in this process's memory "
                "(attempt %d) until a future call succeeds in writing it",
                self._alert_attempts,
            )
        else:
            logger.critical(
                "cost-circuit unavailable alert was not delivered to Telegram "
                "(durable attempt %d recorded at %s); will keep retrying every "
                "~120s in this process, and again from scratch on any future "
                "process/session that touches this circuit, until it succeeds "
                "or an operator intervenes",
                durable_attempts,
                self._emergency_latch_path,
            )

    def activate_session(self, run_id: str, mode: str) -> dict[str, Any]:
        self._context_value.set((run_id, mode))
        self._alert()
        return self._state()

    def status(self) -> dict[str, Any]:
        return self._state()

    def enforce_current_limits(self, agent_name: str = "preflight") -> dict[str, Any]:
        self._alert()
        return self._state()

    def require_paid_analysis(self, agent_name: str = "analysis", model: str | None = None) -> None:
        # Unavailable ledger refuses everything, $0 routes included.
        self._alert()
        state = self._state()
        raise PaidAnalysisSuspended(self._trigger(), state)

    def begin_call(self, **_kwargs) -> CallReservation:
        self.require_paid_analysis(str(_kwargs.get("agent_name") or "analysis"))
        raise AssertionError("unreachable")

    def before_provider_attempt(self, *_args, **_kwargs) -> int:
        self.require_paid_analysis("provider_request")
        raise AssertionError("unreachable")

    def complete_call(self, *_args, **_kwargs) -> None:
        return None

    def fail_call(self, *_args, **_kwargs) -> None:
        return None
