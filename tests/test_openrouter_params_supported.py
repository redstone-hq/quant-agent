"""`reasoning` is sent to OpenRouter only to a model that publishes it.

With provider.require_parameters=True, one unsupported field makes OpenRouter
refuse every endpoint (404 "No endpoints found that can handle the requested
parameters" - the risk seat outage of 2026-10-15).
"""

from src.agents import base
from src.agents.tech_analyst import TechAnalystAgent
from tests.test_tech_schema import _fake_wire_client, _wrapped_response_for

MODEL = "qwen/qwen3-235b-a22b-2507"


def _body(monkeypatch, payload):
    base._openrouter_param_cache.clear()
    monkeypatch.setattr("src.cost_table.http_get_json", lambda url, headers=None: payload())
    agent = TechAnalystAgent(api_key="test", model=MODEL)
    captured: dict = {}
    client = _fake_wire_client(captured, _wrapped_response_for("SPY"))
    agent._openai_wire_call(client, MODEL, "openrouter", "hello")
    base._openrouter_param_cache.clear()
    return captured["extra_body"]


def _eps(*param_lists):
    return {"data": {"endpoints": [{"supported_parameters": p} for p in param_lists]}}


def test_model_without_reasoning_gets_none_but_keeps_schema_lock(monkeypatch):
    body = _body(monkeypatch, lambda: _eps(["response_format", "max_tokens"]))
    assert "reasoning" not in body
    assert body["response_format"]["type"] == "json_schema"
    assert body["provider"]["require_parameters"] is True


def test_model_listing_reasoning_keeps_it(monkeypatch):
    body = _body(monkeypatch, lambda: _eps(["response_format"], ["reasoning", "response_format"]))
    assert body["reasoning"] == {"effort": "medium"}
    assert body["provider"]["require_parameters"] is True


def test_unknown_support_omits_reasoning(monkeypatch):
    def down():
        raise OSError("down")

    body = _body(monkeypatch, down)
    assert "reasoning" not in body
    assert body["provider"]["require_parameters"] is True
