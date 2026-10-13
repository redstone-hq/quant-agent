"""src.cost_circuit.breaker -- moved verbatim from src/cost_circuit.py; see the package docstring."""

from __future__ import annotations
import logging
import sqlite3
import threading
import uuid
from contextvars import ContextVar
from pathlib import Path
from typing import Any, Callable, TypeVar
from src.cost_circuit.alert_ledger import UnavailableLLMCostCircuit, _durable_alert_surface_ok
from src.cost_circuit.assembly import hold_parts
from src.cost_circuit.parts.admission import Admission
from src.cost_circuit.parts.alert_formats import AlertFormats
from src.cost_circuit.parts.emergency_latch import EmergencyLatch
from src.cost_circuit.parts.circuit_state import CircuitState
from src.cost_circuit.parts.episode_wording import EpisodeWording
from src.cost_circuit.parts.infra_retry import InfraRetry
from src.cost_circuit.parts.operator_controls import OperatorControls
from src.cost_circuit.parts.owner_notify import OwnerNotify
from src.cost_circuit.parts.quota_holds import QuotaHolds
from src.cost_circuit.parts.session_lifecycle import SessionLifecycle
from src.cost_circuit.parts.settlement import Settlement

logger = logging.getLogger(__name__)


class LLMCostCircuitBreaker:
    """Mandatory cost/retry breaker shared by every agent in one pipeline.

    Composition, not inheritance: all eleven cost-circuit parts are HELD.
    `_hold_parts` builds one instance of each once per breaker and every
    same-named method below delegates to that instance. Collaborators that
    can change after construction (`notifier`, `_connect`, the sentinel and
    the infrastructure error) reach the parts live, never as a snapshot.
    """

    def __init__(self, db_path: str, config: Any, notifier: Any | None = None):
        self._memory_keeper: sqlite3.Connection | None = None
        self._emergency_latch_path: Path | None = None
        self._emergency_lock_path: Path | None = None
        if db_path == ":memory:":
            # Separate sqlite ``:memory:`` connections are separate databases.
            # Tests construct the pipeline this way, while the breaker needs a
            # short-lived connection per atomic transaction.  A named shared
            # memory DB plus a keeper preserves those semantics without
            # weakening production's file-backed cross-process behavior.
            self.db_path = f"file:qamc-cost-{uuid.uuid4().hex}?mode=memory&cache=shared"
            self._memory_keeper = sqlite3.connect(self.db_path, uri=True)
        else:
            self.db_path = str(Path(db_path))
            # SQLite is normally the durable source of truth.  This sidecar is
            # the independent fail-closed path for the one case where SQLite
            # itself fails while a paid request is in flight.  A new systemd
            # process must see that failure immediately instead of treating a
            # recovered DB connection as permission to spend again.
            db_file = Path(self.db_path)
            self._emergency_latch_path = db_file.with_name(f"{db_file.name}.llm-circuit-unavailable")
            self._emergency_lock_path = db_file.with_name(f"{db_file.name}.llm-circuit.lock")
        self.config = config
        if notifier is None:
            from src.notifier.owner_alert_funnel import build_default_notifier

            notifier = build_default_notifier()
        self.notifier = notifier
        # ContextVar keeps overlapping APScheduler job threads isolated.  The
        # morning research ThreadPool explicitly copies this context into its
        # workers (pipeline_stages.py); a mutable process-global tuple allowed
        # an intra tick to relabel an in-flight morning request.
        self._session_context: ContextVar[tuple[str, str]] = ContextVar(
            f"qamc_cost_session_{id(self)}", default=("unscoped", "unknown")
        )
        self._infrastructure_lock = threading.Lock()
        self._infrastructure_error: BaseException | None = None
        self._unavailable_sentinel: UnavailableLLMCostCircuit | None = None
        self._hold_parts()
        self._sync_emergency_latch()
        if self._unavailable_sentinel is not None:
            return
        if getattr(self.config, "require_telegram_alerts", True) is True:
            transport_enabled = getattr(self.notifier, "enabled", True) is True
            durable_ok = _durable_alert_surface_ok(self._emergency_latch_path)
            if not transport_enabled and not durable_ok:
                # Genuine case only: nowhere to deliver AND nowhere to record.
                self.mark_unavailable(
                    RuntimeError(
                        "mandatory cost-circuit alerts can be neither delivered "
                        "nor durably recorded: the notification transport is "
                        "disabled and no durable latch record can be written"
                    )
                )
                return
            if not transport_enabled:
                logger.warning(
                    "Cost-circuit notification transport is disabled; mandatory "
                    "alerts will be recorded durably at %s and surfaced through "
                    "the API/dashboard decision-path health instead. Paid "
                    "analysis is NOT suspended for this reason.",
                    self._emergency_latch_path,
                )
        try:
            self._run_with_infra_retry(
                self._initialize,
                agent_name="circuit_startup",
            )
        except Exception as exc:
            self.mark_unavailable(exc, agent_name="circuit_startup")

    @property
    def enabled(self) -> bool:
        return bool(getattr(self.config, "enabled", True))

    @classmethod
    def fail_closed(
        cls,
        db_path: str,
        config: Any,
        error: BaseException,
        *,
        notifier: Any | None = None,
        run_id: str = "unscoped",
        mode: str = "unknown",
        agent_name: str = "circuit_startup",
        attempts: int | None = None,
    ) -> "LLMCostCircuitBreaker":
        """Build a durable sentinel even when normal construction explodes."""

        self = cls.__new__(cls)
        self._memory_keeper = None
        self.db_path = str(db_path)
        if db_path == ":memory:":
            # There is no durable filesystem location for a true in-memory
            # database.  The in-process sentinel below still fails closed;
            # importantly, do not create literal ``:memory:*`` sidecars.
            self._emergency_latch_path = None
            self._emergency_lock_path = None
        else:
            self.db_path = str(Path(db_path))
            db_file = Path(self.db_path)
            self._emergency_latch_path = db_file.with_name(f"{db_file.name}.llm-circuit-unavailable")
            self._emergency_lock_path = db_file.with_name(f"{db_file.name}.llm-circuit.lock")
        self.config = config
        if notifier is None:
            try:
                from src.notifier.owner_alert_funnel import build_default_notifier

                notifier = build_default_notifier()
            except Exception:

                class _LocalOnlyNotifier:
                    enabled = False

                    @staticmethod
                    def send(_message: str) -> bool:
                        return False

                notifier = _LocalOnlyNotifier()
        self.notifier = notifier
        self._session_context = ContextVar(f"qamc_cost_session_{id(self)}", default=(run_id, mode))
        self._infrastructure_lock = threading.Lock()
        self._infrastructure_error = None
        self._unavailable_sentinel = None
        self._hold_parts()
        self.mark_unavailable(
            error,
            run_id=run_id,
            mode=mode,
            agent_name=agent_name,
            attempts=attempts,
        )
        return self

    # --- Held parts (composition): wired once per breaker by assembly.py.
    @property
    def notifier(self) -> Any:
        return self._notifier

    @notifier.setter
    def notifier(self, value: Any) -> None:
        self._notifier = value
        for part in (
            getattr(self, "_emergency_latch", None),
            getattr(self, "_infra_retry", None),
            getattr(self, "_owner_notify", None),
        ):
            if part is not None:
                part.notifier = value

    def _hold_parts(self) -> None:
        hold_parts(self)

    # Every name below forwards to the held part, looked up on the instance at
    # call time so a part swapped on the breaker is what runs (wired at import,
    # see the foot of this module).
    _DELEGATES = {
        "_emergency_latch": (
            "_best_effort_emergency_snapshot",
            "_read_emergency_latch",
            "_sync_emergency_latch",
            "_emergency_file_lock",
            "_write_emergency_latch",
        ),
        "_infra_retry": (
            "_infra_retry_backoff_s",
            "_run_with_infra_retry",
            "mark_unavailable",
            "_raise_if_unavailable",
        ),
        "_session_lifecycle": (
            "_initialize",
            "_validate_accounting_invariants",
            "_seed_today",
            "activate_session",
            "set_session_context",
            "_context",
        ),
        "_owner_notify": (
            "_notify_if_needed",
            "_notify_quota_holds_if_needed",
            "_notify_quota_recoveries_if_needed",
            "_notify_auto_resets_if_needed",
        ),
        "_admission": (
            "_enforce_settled_limits_locked",
            "enforce_current_limits",
            "require_paid_analysis",
            "begin_call",
        ),
        "_settlement": ("before_provider_attempt", "complete_call", "fail_call"),
        "_operator_controls": ("status", "reset"),
        "_episode_wording": (
            "_self_clear_window_minutes",
            "_suspension_still_inside_self_clear_window_locked",
            "_episode_already_paged_locked",
            "_record_suspension_deferral_locked",
            "_episode_facts_locked",
        ),
        "_circuit_state": (
            "_state_row",
            "_active_quota_hold_locked",
            "_effective_state_locked",
            "_auto_clear_transient_latch_locked",
        ),
        "_quota_holds": (
            "_reconcile_quota_holds_locked",
            "_hold_quota_locked",
            "_refresh_latched_snapshot_locked",
            "_trip_locked",
        ),
    }

    # Pure functions of the parts, re-exported under their old names.
    _safe_optional_int = staticmethod(EmergencyLatch._safe_optional_int)
    _safe_optional_float = staticmethod(EmergencyLatch._safe_optional_float)
    format_auto_reset_alert = staticmethod(AlertFormats.format_auto_reset_alert)
    format_quota_alert = staticmethod(AlertFormats.format_quota_alert)
    format_recovery_alert = staticmethod(AlertFormats.format_recovery_alert)
    format_alert = staticmethod(AlertFormats.format_alert)
    _format_episode_line = staticmethod(EpisodeWording._format_episode_line)
    _format_episode_summary = staticmethod(EpisodeWording._format_episode_summary)
    _totals = staticmethod(CircuitState._totals)
    _scope_key = staticmethod(CircuitState._scope_key)

    def _connect(self) -> Any:
        conn = sqlite3.connect(self.db_path, timeout=10.0, uri=self.db_path.startswith("file:qamc-cost-"))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=10000")
        return conn


def _delegate(part: str, name: str):
    def forward(self, *args, **kwargs):
        return getattr(getattr(self, part), name)(*args, **kwargs)

    forward.__name__, forward.__qualname__ = name, f"LLMCostCircuitBreaker.{name}"
    return forward


for _part, _names in LLMCostCircuitBreaker._DELEGATES.items():
    for _name in _names:
        setattr(LLMCostCircuitBreaker, _name, _delegate(_part, _name))
