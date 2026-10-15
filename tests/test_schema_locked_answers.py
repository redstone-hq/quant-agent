"""The answer schema holds end to end, so answer text is never patched.

(a) OpenRouter may only route a schema call to an endpoint that honours it.
(b) The news seat's schema is strict.
(c) An answer that is not JSON gets one re-ask, then a per-symbol refusal.
(d) A schema seat is never answered by a backup rung that cannot take it.
"""

from unittest.mock import MagicMock, patch

import pytest

from src.agents.tech_analyst import TechAnalystAgent
from src.models import parse_telemetry
from tests.test_tech_schema import _fake_wire_client, _symbols_data, _wrapped_response_for


@pytest.mark.parametrize("order", [None, ["google-vertex"]])
def test_openrouter_requires_every_parameter_alongside_the_schema(order):
    agent = TechAnalystAgent(api_key="test", model="test-model")
    captured: dict = {}
    client = _fake_wire_client(captured, _wrapped_response_for("SPY"))

    agent._openai_wire_call(client, "test-model", "openrouter", "hello", provider_order=order)

    body = captured["extra_body"]
    assert body["response_format"]["type"] == "json_schema"
    assert body["provider"]["require_parameters"] is True
    if order:
        assert body["provider"]["order"] == order, "the endpoint preference must survive"


def test_the_news_seat_schema_is_strict():
    from src.agents.llm_schema import _response_format_for
    from src.models.news import NewsIntelligenceReport

    assert _response_format_for(NewsIntelligenceReport)["json_schema"]["strict"] is True


@patch("anthropic.Anthropic")
def test_an_answer_that_is_not_json_is_re_asked_once_then_refused_per_symbol(mock_cls):
    # A fenced answer the old parser would have unwrapped into a valid row.
    fenced = "Here you go:\n```json\n" + _wrapped_response_for("SPY") + "\n```"
    resp = MagicMock()
    resp.content = [MagicMock(text=fenced)]
    resp.usage.input_tokens = 1
    resp.usage.output_tokens = 1
    mock_cls.return_value.messages.create.return_value = resp
    parse_telemetry.reset()

    agent = TechAnalystAgent(api_key="test", model="claude-sonnet-4-6-20250514")
    results, _ = agent.analyze_batch(_symbols_data(["SPY"]))

    assert results["SPY"] is None, "the row inside the fence must not be patched out"
    assert mock_cls.return_value.messages.create.call_count == 2, "exactly one re-ask"
    assert parse_telemetry.dropped_snapshot()[("TechAnalysisResult", "SPY")] == 2


class _BadRequest(Exception):
    status_code = 400


def _ladder_agent(backup: str, model: str) -> TechAnalystAgent:
    agent = TechAnalystAgent(
        api_key="k",
        model=model,
        provider="openrouter",
        fallback_api_key="k",
        fallback_provider=backup,
        fallback_model="m",
        tertiary_api_key="k",
        tertiary_provider=backup,
        tertiary_model="other-m",
    )
    agent._call_openai = MagicMock(side_effect=_BadRequest("primary refused"))
    agent._try_failover = MagicMock(return_value=None)
    agent._try_tertiary = MagicMock(return_value=None)
    return agent


def test_a_schema_seat_never_reaches_a_route_that_cannot_take_the_schema():
    from src.agents.llm_schema import route_takes_schema
    from src.models import TechAnalystAnswer

    assert route_takes_schema("anthropic", TechAnalystAnswer, True, "t") is False
    assert route_takes_schema("anthropic", None, True, "t") is True

    agent = _ladder_agent("anthropic", "google/gemini-a")
    with pytest.raises(_BadRequest):
        agent._execute("hello")
    agent._try_failover.assert_not_called()
    agent._try_tertiary.assert_not_called()

    # Control: the same ladder on a schema-taking backup is still tried.
    agent = _ladder_agent("google", "google/gemini-b")
    with pytest.raises(_BadRequest):
        agent._execute("hello")
    agent._try_failover.assert_called_once()
