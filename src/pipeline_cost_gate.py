"""The paid-analysis gate: cost-circuit activation, preflight, status and the suspension payloads.

Moved VERBATIM out of `TradingPipeline` in `src/pipeline.py` (board: the
pipeline split). Function-only module in the `src.pipeline_sizing` shape: each
function takes the pipeline (or any stub carrying the attributes it reads) as
its first argument, so the gate can be built and exercised from stubs without
constructing a `TradingPipeline`. The bodies did not change by one character;
the parameter is still named `self` for that reason. `TradingPipeline` keeps a
one-line shim per name so every `self._x(...)` caller and every
`patch.object(TradingPipeline, "_x")` keeps working.

Attributes read off the first argument: `cost_circuit`,
`_active_cost_run_context`, the ten agent seats named in
`_attach_cost_circuit_to_agents`, and the sibling gate functions through
`self` (which the shims provide).

This module must not import `src.pipeline`.
"""

from __future__ import annotations

import logging

from src.agents.base import BaseAgent
from src.cost_circuit import PaidAnalysisSuspended, UnavailableLLMCostCircuit
from src.cost_circuit.free_route import is_free_route

#: The moved code logged under `src.pipeline` before the move and still does;
#: binding the name rather than `__name__` keeps log records byte-identical.
logger = logging.getLogger("src.pipeline")


def _activate_cost_session(self, run_id: str, mode: str) -> None:
    """Register paid-call context without interfering with safety work."""

    self._active_cost_run_context = (run_id, mode)
    circuit = getattr(self, "cost_circuit", None)
    if circuit is None:
        if BaseAgent._allow_unmetered_for_tests:
            return
        circuit = UnavailableLLMCostCircuit(RuntimeError("mandatory paid-analysis cost circuit is not initialized"))
        self.cost_circuit = circuit
        self._attach_cost_circuit_to_agents()
    try:
        circuit.activate_session(run_id, mode)
    except Exception as exc:
        logger.critical(
            "Cost-circuit activation failed for %s/%s; failing paid analysis "
            "closed without interrupting deterministic safety: %s",
            run_id,
            mode,
            exc,
            exc_info=True,
        )
        marker = getattr(circuit, "mark_unavailable", None)
        if callable(marker):
            marker(exc, run_id=run_id, mode=mode)
        else:
            circuit = UnavailableLLMCostCircuit(exc)
            self.cost_circuit = circuit
            self._attach_cost_circuit_to_agents()
            circuit.activate_session(run_id, mode)


def _primary_model_for(self, agent_name: str) -> str | None:
    """The primary model of the agent a preflight label names, or None.

    Labels are the agent attribute (`tech_analyst`) or a mode-prefixed form of
    it (`intraday_tech_analyst`). Unresolvable labels return None, which the
    circuit treats as paid -- fail closed.
    """

    candidates = [agent_name]
    if "_" in agent_name:
        candidates.append(agent_name.split("_", 1)[1])
    for attr in candidates:
        agent = getattr(self, attr, None)
        if isinstance(agent, BaseAgent):
            model = getattr(agent, "model", None)
            return model if isinstance(model, str) and model else None
    return None


def _require_paid_analysis(self, agent_name: str) -> None:
    circuit = getattr(self, "cost_circuit", None)
    if circuit is None:
        if BaseAgent._allow_unmetered_for_tests:
            return
        raise PaidAnalysisSuspended(
            "mandatory paid-analysis cost circuit is not initialized",
            {"available": False, "suspended": True},
        )
    model = _primary_model_for(self, agent_name)
    try:
        if model is not None and is_free_route(model):
            # A $0 primary route may run under a money-cap-only suspension.
            circuit.require_paid_analysis(agent_name, model=model)
        else:
            circuit.require_paid_analysis(agent_name)
    except PaidAnalysisSuspended:
        raise
    except Exception as exc:
        logger.critical("Cost-circuit preflight failed closed: %s", exc, exc_info=True)
        marker = getattr(circuit, "mark_unavailable", None)
        if callable(marker):
            state = marker(exc)
            raise PaidAnalysisSuspended(
                "mandatory cost-circuit preflight failed",
                state,
            ) from exc
        replacement = UnavailableLLMCostCircuit(exc)
        self.cost_circuit = replacement
        self._attach_cost_circuit_to_agents()
        run_id, mode = getattr(self, "_active_cost_run_context", ("unscoped", "unknown"))
        replacement.activate_session(run_id, mode)
        replacement.require_paid_analysis(agent_name)


def _attach_cost_circuit_to_agents(self) -> None:
    circuit = getattr(self, "cost_circuit", None)
    for name in (
        "tech_analyst",
        "news_analyst",
        "macro_analyst",
        "earnings_analyst",
        "smart_money_analyst",
        "portfolio_manager",
        "risk_manager",
        "position_reviewer",
        "evening_analyst",
        "meta_reflector",
    ):
        agent = getattr(self, name, None)
        setter = getattr(agent, "set_cost_circuit", None)
        if callable(setter):
            setter(circuit)


def _cost_circuit_status(self) -> dict:
    circuit = getattr(self, "cost_circuit", None)
    if circuit is None:
        if BaseAgent._allow_unmetered_for_tests:
            return {"enabled": False, "suspended": False}
        return {
            "available": False,
            "enabled": True,
            "suspended": True,
            "trigger_detail": "mandatory cost circuit is not initialized",
        }
    try:
        return circuit.status()
    except Exception as exc:
        logger.critical("Cost-circuit status failed closed: %s", exc, exc_info=True)
        marker = getattr(circuit, "mark_unavailable", None)
        if callable(marker):
            return marker(exc)
        replacement = UnavailableLLMCostCircuit(exc)
        self.cost_circuit = replacement
        self._attach_cost_circuit_to_agents()
        run_id, mode = getattr(self, "_active_cost_run_context", ("unscoped", "unknown"))
        return replacement.activate_session(run_id, mode)


def _paid_suspended_payload(
    run_id: str,
    *,
    orders: list[dict] | None = None,
    error: BaseException | None = None,
    filings_waiting: list[dict] | None = None,
) -> dict:
    # `filings_waiting` (2026-09-24): when the cost circuit trips after
    # `run_earnings_preprocess` has already computed which filings were
    # queued for the LLM reader, that backlog was silently dropped here
    # -- the suspended payload carried no earnings keys at all, so
    # `_append_earnings_body` rendered "analyzed:0 confirmed:0
    # failed:0" for a run that actually found N new filings. Passing it
    # through lets the owner-facing message say "suspended, N filing(s)
    # waiting" instead of implying nothing happened.
    waiting = list(filings_waiting or [])
    return {
        "status": "paid_analysis_suspended",
        "run_id": run_id,
        "orders": list(orders or []),
        "error": str(error or "mandatory cost circuit is open"),
        "paid_analysis_suspended": True,
        "filings_waiting": waiting,
        "filings_waiting_count": len(waiting),
        "preserved": [
            "broker_resident_protection",
            "order_fill_reconciliation",
            "deterministic_loss_protection",
            "non_llm_safety_jobs",
        ],
    }


def _paid_suspension_after_late_safety(
    self,
    run_id: str,
    *,
    session: str,
    error: BaseException,
    where: str,
    orders: list[dict] | None = None,
    extra: dict | None = None,
) -> dict:
    """The suspension return payload.

    It used to re-run an account-level loss check first. That
    whole mechanism was removed 2026-09-20 on owner instruction
    (docs/INCIDENT_HISTORY.md, retired item 32): per-position stops are
    the desk's loss protection now, and they live at the broker rather
    than depending on this process reaching this line.

    KNOWN RESIDUE, deliberately not chased in that change: `session`
    and `where` are now unused here, and the name still says "after
    late safety" when there is no late safety check left. Eleven call
    sites pass both. Renaming the method and dropping two keyword
    arguments across all eleven is churn with no behavioural effect, so
    it was left for whoever next touches this path — it is recorded
    here rather than silently tolerated.
    """

    existing_orders = list(orders or [])
    payload = self._paid_suspended_payload(
        run_id,
        orders=existing_orders,
        error=error,
    )
    if extra:
        payload.update(extra)
    # 2026-09-30 (item 199): this is the third legit PM-less completion
    # alongside `no_data` and `evidence_gate_skip` above, both of which
    # already call `_dc.write_status` so the evening dead-man probe
    # skips its "research ran, PM never did — killed mid-run?" guess.
    # This path never did, so a same-day cost-circuit suspension the
    # owner was already told about at the time (the morning session's
    # own "SUSPENDED" push) re-arrived ~16h later relabelled as a
    # mystery kill. Morning-only: `read_status`/the sharper probes in
    # `_expected_sessions_missing_today` only ever key on "morning".
    if session == "morning":
        from src import decision_checkpoint as _dc

        _dc.write_status("morning", "paid_analysis_suspended")
    return payload
