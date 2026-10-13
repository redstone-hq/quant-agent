"""src.cost_circuit.parts.emergency_latch -- EmergencyLatch for the cost circuit (durable emergency-latch read/write/sync, best-effort snapshot).

Bodies moved verbatim from the former src/cost_circuit/breaker_latch.py (now held by LLMCostCircuitBreaker) (originally src/cost_circuit.py).
Every collaborator is an explicit keyword-only constructor argument.
"""

from __future__ import annotations
from src.sentinel.guarded import NO_LEDGER, record_guarded_pass
import logging
import json
import os
import uuid
from typing import Any, Callable, TypeVar
from src.cost_circuit.clock import _et_day_and_utc_bounds
from src.trading_calendar import utc_now
from src.cost_circuit.alert_ledger import UnavailableLLMCostCircuit, _file_lock

logger = logging.getLogger(__name__)


class EmergencyLatch:
    def __init__(
        self,
        *,
        connect,
        notifier,
        infrastructure_lock,
        emergency_latch_path,
        emergency_lock_path,
        read_unavailable_sentinel,
        write_unavailable_sentinel,
        read_infrastructure_error,
        write_infrastructure_error,
        read_emergency_latch=None,
        emergency_file_lock=None,
    ) -> None:
        self._connect = connect
        self.notifier = notifier
        self._infrastructure_lock = infrastructure_lock
        self._emergency_latch_path = emergency_latch_path
        self._emergency_lock_path = emergency_lock_path
        self.read_unavailable_sentinel = read_unavailable_sentinel
        self.write_unavailable_sentinel = write_unavailable_sentinel
        self.read_infrastructure_error = read_infrastructure_error
        self.write_infrastructure_error = write_infrastructure_error
        # The bodies below are themselves moved; a caller passes one only when it was
        # swapped on the host (see the shim), otherwise this object's own run.
        if read_emergency_latch is not None:
            self._read_emergency_latch = read_emergency_latch
        if emergency_file_lock is not None:
            self._emergency_file_lock = emergency_file_lock

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

    def _best_effort_emergency_snapshot(self, run_id: str) -> dict[str, Any]:
        """Read honest known spend for an emergency alert without mutating DB."""

        day, _, _ = _et_day_and_utc_bounds()
        try:
            with self._connect() as conn:
                day_row = conn.execute(
                    "SELECT baseline_cost_usd + incremental_cost_usd AS cost, "
                    "costs_exact FROM llm_budget_days WHERE day=?",
                    (day,),
                ).fetchone()
                if day_row is None:
                    return {}
                session_row = conn.execute(
                    "SELECT actual_cost_usd, provider_attempts, costs_exact FROM llm_budget_sessions WHERE run_id=?",
                    (run_id,),
                ).fetchone()
        except Exception as exc:
            record_guarded_pass(NO_LEDGER, "emergency_latch.budget_session_read", exc)
            return {}

        snapshot: dict[str, Any] = {
            "daily_cost_usd": float(day_row["cost"] or 0.0),
            "daily_costs_exact": bool(day_row["costs_exact"]),
        }
        if session_row is not None:
            snapshot.update(
                session_cost_usd=float(session_row["actual_cost_usd"] or 0.0),
                attempts=int(session_row["provider_attempts"] or 0),
                attempts_exact=True,
                session_costs_exact=bool(session_row["costs_exact"]),
            )
        return snapshot

    def _read_emergency_latch(self) -> tuple[BaseException, dict[str, Any]] | None:
        path = self._emergency_latch_path
        if path is None or not path.exists():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            detail = str(payload.get("error") or "persistent accounting failure")
            recorded = str(payload.get("recorded_at") or "unknown time")
            return (
                RuntimeError(f"durable cost-circuit emergency latch from {recorded}: {detail}"),
                payload,
            )
        except Exception as exc:
            # A corrupt/unreadable marker is still a marker.  Never interpret
            # an observability problem as permission to make a paid request.
            return (
                RuntimeError(
                    "durable cost-circuit emergency latch exists but is unreadable: "
                    f"{type(exc).__name__}: {str(exc)[:240]}"
                ),
                {},
            )

    def _sync_emergency_latch(self) -> None:
        latched = self._read_emergency_latch()
        if latched is None:
            return
        error, payload = latched
        with self._infrastructure_lock:
            if self._unavailable_sentinel is None:
                self._infrastructure_error = error
                self._unavailable_sentinel = UnavailableLLMCostCircuit(
                    error,
                    notifier=self.notifier,
                    run_id=str(payload.get("run_id") or "unscoped"),
                    mode=str(payload.get("mode") or "unknown"),
                    agent_name=str(payload.get("agent_name") or "circuit_infrastructure"),
                    attempts=self._safe_optional_int(payload.get("attempts")),
                    attempts_exact=payload.get("attempts_exact") is True,
                    session_cost_usd=self._safe_optional_float(payload.get("session_cost_usd")),
                    daily_cost_usd=self._safe_optional_float(payload.get("daily_cost_usd")),
                    costs_exact=payload.get("costs_exact") is True,
                    emergency_latch_path=self._emergency_latch_path,
                    emergency_lock_path=self._emergency_lock_path,
                )

    @staticmethod
    def _safe_optional_int(value: Any) -> int | None:
        if value is None or isinstance(value, bool):
            return None
        try:
            parsed = int(value)
        except (TypeError, ValueError, OverflowError):
            return None
        return parsed if parsed >= 0 else None

    @staticmethod
    def _safe_optional_float(value: Any) -> float | None:
        if value is None or isinstance(value, bool):
            return None
        try:
            parsed = float(value)
        except (TypeError, ValueError, OverflowError):
            return None
        return parsed if parsed >= 0 and parsed < float("inf") else None

    def _emergency_file_lock(self):
        """Serialize infrastructure-latch writers with operator reset."""

        return _file_lock(self._emergency_lock_path)

    def _write_emergency_latch(
        self,
        error: BaseException,
        *,
        run_id: str,
        mode: str,
        agent_name: str,
        attempts: int | None,
        attempts_exact: bool,
        session_cost_usd: float | None,
        daily_cost_usd: float | None,
        costs_exact: bool,
    ) -> None:
        """Atomically persist an accounting-infrastructure shutdown marker."""

        path = self._emergency_latch_path
        if path is None:
            return
        with self._emergency_file_lock():
            # Preserve the original shutdown trigger/affected run. Later
            # failures in other workers must not rewrite incident identity.
            if path.exists():
                return
            payload = (
                json.dumps(
                    {
                        "recorded_at": utc_now().isoformat(),
                        "error": f"{type(error).__name__}: {str(error)[:500]}",
                        "run_id": run_id,
                        "mode": mode,
                        "agent_name": agent_name,
                        "attempts": attempts,
                        "attempts_exact": attempts_exact,
                        "session_cost_usd": session_cost_usd,
                        "daily_cost_usd": daily_cost_usd,
                        "costs_exact": costs_exact,
                        # item 17(b): durable alert-delivery bookkeeping, folded
                        # in place by `_record_alert_attempt` as
                        # `UnavailableLLMCostCircuit._alert()` runs. Starts
                        # undelivered/zero -- nothing has attempted to notify
                        # the operator about THIS incident yet.
                        "alert_delivered": False,
                        "alert_suppressed": False,
                        "alert_attempts": 0,
                        "last_alert_attempt_at": None,
                    },
                    sort_keys=True,
                )
                + "\n"
            )
            tmp = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
            try:
                fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                try:
                    with os.fdopen(fd, "w", encoding="utf-8") as handle:
                        handle.write(payload)
                        handle.flush()
                        os.fsync(handle.fileno())
                    os.replace(tmp, path)
                    # Persist the directory entry when supported.
                    try:
                        directory_fd = os.open(path.parent, os.O_RDONLY)
                        try:
                            os.fsync(directory_fd)
                        finally:
                            os.close(directory_fd)
                    except OSError:
                        logger.warning(
                            "Could not fsync cost-circuit latch directory %s",
                            path.parent,
                        )
                except Exception:
                    try:
                        os.close(fd)
                    except OSError:
                        pass
                    raise
            finally:
                try:
                    tmp.unlink(missing_ok=True)
                except OSError:
                    logger.warning("Could not remove temporary cost-circuit latch %s", tmp)
