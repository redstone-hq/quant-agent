"""The close-session review of held stocks runs past a tripped DAILY cap (owner ruling 2026-10-14).

On 2026-10-12 the daily cap tripped at 13:19 ET and the close session then made
0 paid calls; on 9, 13 and 14 Oct the close session cost $0.0072, $0.0068 and
$0.0034. Only the DAILY cap is lifted for the close session. Every other stop
(its own session cost cap, call-count backstop) still binds, and every other
session is still refused.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.cost_circuit import LLMCostCircuitBreaker, PaidAnalysisSuspended
from src.storage.db import Database

PAID_MODEL = "deepseek-v4-flash"  # priced, so only the exemption (not the free route) can admit it


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
    circuit.activate_session("midday-test", "midday")
    return circuit


def _begin(circuit, agent="position_reviewer"):
    return circuit.begin_call(
        agent_name=agent,
        model=PAID_MODEL,
        system_prompt="s",
        user_message="u",
        max_output_tokens=100,
    )


def _spend(circuit, cost, agent="position_reviewer"):
    reservation = _begin(circuit, agent)
    circuit.before_provider_attempt(reservation, model=PAID_MODEL)
    circuit.complete_call(reservation, cost, actual_model=PAID_MODEL)


def _trip_daily_cap_in_midday(circuit):
    _spend(circuit, 1.10, agent="portfolio_manager")
    state = circuit.status()
    assert state["suspended"] and state["trigger_code"] == "daily_cost_limit"


def test_close_session_admitted_with_daily_cap_tripped(tmp_path):
    circuit = _circuit(tmp_path)
    _trip_daily_cap_in_midday(circuit)
    circuit.activate_session("close-test", "close")

    circuit.require_paid_analysis("position_reviewer", model=PAID_MODEL)
    _spend(circuit, 0.0072)  # a paid call, begin to settle, all admitted

    state = circuit.status()
    assert not state["suspended"]
    assert state["current_daily_cost_usd"] > 1.0  # the day really is past its cap


def test_non_close_session_still_refused_with_daily_cap_tripped(tmp_path):
    circuit = _circuit(tmp_path)
    _trip_daily_cap_in_midday(circuit)
    circuit.activate_session("evening-test", "evening")

    with pytest.raises(PaidAnalysisSuspended):
        _begin(circuit)
    with pytest.raises(PaidAnalysisSuspended):
        circuit.require_paid_analysis("position_reviewer", model=PAID_MODEL)
    assert circuit.status()["trigger_code"] == "daily_cost_limit"


def test_close_session_own_session_cap_still_refuses(tmp_path):
    circuit = _circuit(tmp_path, session_cost_limit_usd=0.50)
    _trip_daily_cap_in_midday(circuit)
    circuit.activate_session("close-test", "close")

    _spend(circuit, 0.60)  # first call admitted past the daily cap, then spends over the session cap
    with pytest.raises(PaidAnalysisSuspended):
        _begin(circuit)
    assert circuit.status()["trigger_code"] == "session_cost_limit"


def test_close_session_call_count_backstop_still_refuses(tmp_path):
    circuit = _circuit(tmp_path, max_calls_per_session=2)
    _trip_daily_cap_in_midday(circuit)
    circuit.activate_session("close-test", "close")

    _spend(circuit, 0.01)
    _spend(circuit, 0.01)
    with pytest.raises(PaidAnalysisSuspended):
        _begin(circuit)
    assert circuit.status()["trigger_code"] == "session_call_count_limit"
