"""A failed attempt must not leave a phantom charge in the token governor.

Measured 16 Oct live log: the chart seat took 13 "model busy" refusals from
Google. Each attempt was charged input + the full max_tokens output allowance
and only the successful attempt was reconciled, so every refusal left a
~69k-token phantom in the one-minute window and the governor held the seat
21 times (386 s) while real throughput was ~52k/min against a 200k cap.
"""

from __future__ import annotations

import httpx
import pytest

import src.agents.base as base
import src.agents.llm_concurrency as concurrency
from src.agents.base import BaseAgent
from src.token_budget import SizeModel
from src.token_rate import TokenRateGovernor

MAX_TOKENS = 65_536
REAL_IN, REAL_OUT = 30_000, 9_000
FITTED = SizeModel(fixed_tokens=4_766, tokens_per_byte=0.792, samples=40, measured=True, observed_max_bytes=1e9)


class _Seat(BaseAgent):
    @property
    def name(self) -> str:
        return "chart_seat_test"

    @property
    def system_prompt(self) -> str:
        return "You read charts."

    def build_user_message(self, **kwargs) -> str:
        return "x" * 20_000


class _Busy(Exception):
    status_code = 503


@pytest.fixture
def governor(monkeypatch):
    gov = TokenRateGovernor("Google", 10_000_000, max_wait_s=0.0, sleep=lambda s: None)
    monkeypatch.setitem(base._TOKEN_GOVERNORS, "google", gov)
    monkeypatch.setattr(base.time, "sleep", lambda s: None)
    monkeypatch.setattr(concurrency, "size_model_for_agent", lambda agent, default_model=None: FITTED, raising=False)
    return gov


def _seat(failures):
    agent = _Seat(api_key="k", model="gemini-test", provider="google", max_tokens=MAX_TOKENS)
    calls = {"n": 0}

    def fake_call(user_message, *, authorize=None):
        authorize(agent.model)
        calls["n"] += 1
        if calls["n"] <= len(failures):
            raise failures[calls["n"] - 1]
        return ("{}", REAL_IN, REAL_OUT, "stop", None)

    agent._call_openai = fake_call
    return agent, calls


def _fitted_input(agent) -> int:
    return FITTED.predict(len(agent.build_user_message().encode("utf-8")))


def test_two_busy_refusals_then_success_leave_only_real_usage_plus_refused_input(governor):
    agent, calls = _seat([_Busy("model busy"), _Busy("model busy")])
    agent.run()
    assert calls["n"] == 3
    expected = REAL_IN + REAL_OUT + 2 * _fitted_input(agent)
    assert governor.snapshot()["tokens_in_window"] == expected


def test_a_request_that_never_left_the_process_reconciles_to_zero(governor):
    refused = _Busy("connection refused")
    refused.__cause__ = httpx.ConnectError("[Errno 111] Connection refused")
    agent, _ = _seat([refused])
    agent.run()
    assert governor.snapshot()["tokens_in_window"] == REAL_IN + REAL_OUT


def test_the_pre_request_charge_uses_the_seats_fitted_size_model(governor):
    agent, _ = _seat([])
    charged: list[int] = []
    real_charge = governor.charge
    governor.charge = lambda tokens: charged.append(tokens) or real_charge(tokens)
    agent.run()
    assert charged == [_fitted_input(agent) + MAX_TOKENS]


def test_a_seat_with_no_fit_falls_back_to_the_chars_per_token_estimate(governor, monkeypatch):
    unfitted = SizeModel(fixed_tokens=6_000, tokens_per_byte=1.0, samples=0, measured=False)
    monkeypatch.setattr(concurrency, "size_model_for_agent", lambda agent, default_model=None: unfitted)
    agent, _ = _seat([])
    message = agent.build_user_message()
    expected = int((len(agent.system_prompt) + len(message)) / concurrency._GOVERNOR_CHARS_PER_TOKEN)
    assert concurrency._governed_input_estimate(agent, agent.model, message) == expected
