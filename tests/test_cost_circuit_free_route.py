"""A $0 route keeps running while ONLY a money cap is tripped (owner ruling 2026-10-01).

Every other trigger still refuses everything, and a paid fallback is never
admitted while the cap is tripped.
"""

from __future__ import annotations

import logging
import sqlite3
from types import SimpleNamespace

import pytest

from src.agents.base import BaseAgent
from src.cost_circuit import LLMCostCircuitBreaker, PaidAnalysisSuspended
from src.cost_circuit.free_route import is_free_route
from src.storage.db import Database
from tests.pipeline_factory import build_pipeline

FREE = "gemini-3.5-flash-lite"  # pinned $0 Google AI Studio free tier
PAID = "google/gemini-3.5-flash-lite"  # same model through OpenRouter, paid
PM = "openai/gpt-5.5"


class _Notifier:
    def __init__(self):
        self.messages: list[str] = []

    def send(self, message: str, **_kwargs) -> bool:
        self.messages.append(message)
        return True


def _config(**overrides):
    values = {
        "enabled": True,
        "session_cost_limit_usd": 10.0,
        "daily_cost_limit_usd": 1.0,
        "max_calls_per_session": 1000,
        "max_provider_attempts_per_call": 3,
        "input_chars_per_token": 3.5,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _circuit(tmp_path, **overrides):
    path = tmp_path / "circuit.db"
    db = Database(str(path))
    db.initialize()
    db.conn.close()
    circuit = LLMCostCircuitBreaker(str(path), _config(**overrides), _Notifier())
    circuit.activate_session("run-free", "intraday")
    return circuit, str(path)


def _begin(circuit, model, agent="tech_analyst"):
    return circuit.begin_call(
        agent_name=agent,
        model=model,
        system_prompt="s",
        user_message="u",
        max_output_tokens=100,
    )


def _trip_daily_cap(circuit):
    reservation = _begin(circuit, "deepseek-v4-flash", agent="portfolio_manager")
    circuit.before_provider_attempt(reservation, model=reservation.model)
    circuit.complete_call(reservation, 1.10, actual_model=reservation.model)
    state = circuit.status()
    assert state["suspended"] and state["trigger_code"] == "daily_cost_limit"


def test_price_table_marks_only_the_pinned_free_tier_as_free():
    assert is_free_route(FREE)
    assert not is_free_route(PAID)
    assert not is_free_route(PM)
    assert not is_free_route("never-heard-of-it")
    assert not is_free_route(None)


def test_free_route_admitted_under_daily_cap_and_logged(tmp_path, caplog):
    circuit, _ = _circuit(tmp_path)
    _trip_daily_cap(circuit)
    with caplog.at_level(logging.WARNING, logger="src.cost_circuit.free_route"):
        reservation = _begin(circuit, FREE)
        assert circuit.before_provider_attempt(reservation, model=FREE) == 1
        circuit.complete_call(reservation, 0.0, actual_model=FREE)
    admitted = [r for r in caplog.records if "admitted $0 route" in r.getMessage()]
    assert len(admitted) == 1
    assert "tech_analyst" in admitted[0].getMessage()
    assert "daily_cost_limit" in admitted[0].getMessage()


def test_free_route_admitted_under_session_cap(tmp_path):
    circuit, _ = _circuit(tmp_path, session_cost_limit_usd=0.5, daily_cost_limit_usd=10.0)
    reservation = _begin(circuit, "deepseek-v4-flash", agent="portfolio_manager")
    circuit.before_provider_attempt(reservation, model=reservation.model)
    circuit.complete_call(reservation, 0.60, actual_model=reservation.model)
    assert circuit.status()["trigger_code"] == "session_cost_limit"
    _begin(circuit, FREE)


@pytest.mark.parametrize("model", [PAID, PM, "deepseek-v4-flash"])
def test_paid_agent_still_refused_under_daily_cap(tmp_path, model):
    circuit, _ = _circuit(tmp_path)
    _trip_daily_cap(circuit)
    with pytest.raises(PaidAnalysisSuspended):
        _begin(circuit, model, agent="portfolio_manager")


def test_paid_fallback_refused_while_cap_tripped(tmp_path):
    circuit, _ = _circuit(tmp_path)
    _trip_daily_cap(circuit)
    reservation = _begin(circuit, FREE)
    circuit.before_provider_attempt(reservation, model=FREE)  # free primary: admitted
    with pytest.raises(PaidAnalysisSuspended):
        circuit.before_provider_attempt(reservation, model=PAID)  # 503 -> paid fallback: refused


def test_call_count_limit_still_refuses_free_route(tmp_path):
    circuit, _ = _circuit(tmp_path, max_calls_per_session=3)
    _trip_daily_cap(circuit)  # call 1
    _begin(circuit, FREE)  # call 2
    _begin(circuit, FREE)  # call 3
    with pytest.raises(PaidAnalysisSuspended):
        _begin(circuit, FREE)  # call 4: over the backstop
    with pytest.raises(PaidAnalysisSuspended):
        _begin(circuit, FREE)


def test_call_count_hold_alone_refuses_free_route(tmp_path):
    circuit, _ = _circuit(tmp_path, max_calls_per_session=1, daily_cost_limit_usd=10.0)
    _begin(circuit, FREE)
    with pytest.raises(PaidAnalysisSuspended):
        _begin(circuit, FREE)
    assert circuit.status()["trigger_code"] == "session_call_count_limit"
    with pytest.raises(PaidAnalysisSuspended):
        circuit.require_paid_analysis("tech_analyst", model=FREE)


def test_emergency_latch_refuses_free_route(tmp_path):
    circuit, _ = _circuit(tmp_path)
    _trip_daily_cap(circuit)
    circuit.mark_unavailable(
        RuntimeError("simulated ledger failure"),
        run_id="run-free",
        mode="intraday",
        agent_name="tech_analyst",
        attempts=1,
    )
    with pytest.raises(PaidAnalysisSuspended):
        _begin(circuit, FREE)
    with pytest.raises(PaidAnalysisSuspended):
        circuit.require_paid_analysis("tech_analyst", model=FREE)


def test_hard_latch_refuses_free_route_even_with_money_hold(tmp_path):
    circuit, path = _circuit(tmp_path)
    _trip_daily_cap(circuit)
    conn = sqlite3.connect(path)
    conn.execute(
        "UPDATE llm_circuit_state SET suspended=1, trigger_code='legacy_unknown_cost', "
        "trigger_detail='unknown cost', run_id='run-free', mode='intraday' WHERE singleton=1"
    )
    conn.commit()
    conn.close()
    with pytest.raises(PaidAnalysisSuspended):
        _begin(circuit, FREE)
    with pytest.raises(PaidAnalysisSuspended):
        circuit.require_paid_analysis("tech_analyst", model=FREE)


def test_require_paid_analysis_mirrors_begin_call(tmp_path):
    circuit, _ = _circuit(tmp_path)
    _trip_daily_cap(circuit)
    circuit.require_paid_analysis("tech_analyst", model=FREE)
    with pytest.raises(PaidAnalysisSuspended):
        circuit.require_paid_analysis("portfolio_manager", model=PM)
    with pytest.raises(PaidAnalysisSuspended):
        circuit.require_paid_analysis("portfolio_manager")


class _Seat(BaseAgent):
    name = "seat"
    system_prompt = "s"

    def build_user_message(self, **kwargs) -> str:
        return "u"


def _agent(model):
    agent = object.__new__(_Seat)
    agent.model = model
    return agent


def test_pipeline_gate_resolves_agent_route(tmp_path):
    circuit, _ = _circuit(tmp_path)
    _trip_daily_cap(circuit)
    pipeline = build_pipeline(cost_circuit=circuit)
    pipeline.tech_analyst = _agent(FREE)
    pipeline.portfolio_manager = _agent(PM)
    pipeline._require_paid_analysis("intraday_tech_analyst")  # free: admitted
    pipeline._require_paid_analysis("tech_analyst")
    with pytest.raises(PaidAnalysisSuspended):
        pipeline._require_paid_analysis("portfolio_manager")
    with pytest.raises(PaidAnalysisSuspended):
        pipeline._require_paid_analysis("evening_news")  # unresolvable label: fail closed
    pipeline.tech_analyst = _agent(PAID)
    with pytest.raises(PaidAnalysisSuspended):
        pipeline._require_paid_analysis("intraday_tech_analyst")
