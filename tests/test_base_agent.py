import pytest
import json
from unittest.mock import patch, MagicMock
from types import SimpleNamespace

from src.agents.base import BaseAgent


class ConcreteAgent(BaseAgent):
    """Test subclass of BaseAgent."""

    @property
    def name(self) -> str:
        return "test_agent"

    @property
    def system_prompt(self) -> str:
        return "You are a test agent."

    def build_user_message(self, **kwargs) -> str:
        return f"Analyze: {kwargs.get('data', 'nothing')}"


@pytest.fixture
def mock_anthropic():
    with patch("anthropic.Anthropic") as mock_cls:
        mock_client = MagicMock()
        mock_response = MagicMock()
        mock_response.content = [MagicMock(text='{"result": "bullish"}')]
        mock_response.usage.input_tokens = 100
        mock_response.usage.output_tokens = 50
        mock_client.messages.create.return_value = mock_response
        mock_cls.return_value = mock_client
        yield mock_client


def test_agent_call(mock_anthropic):
    agent = ConcreteAgent(api_key="test-key", model="claude-sonnet-4-6-20250514", max_tokens=1024)
    result = agent.run(data="SPY price data")
    assert result.raw_text == '{"result": "bullish"}'
    assert result.tokens_used == 150


def test_agent_call_with_json_parse(mock_anthropic):
    agent = ConcreteAgent(api_key="test-key", model="claude-sonnet-4-6-20250514", max_tokens=1024)
    result = agent.run(data="SPY price data")
    parsed = result.parse_json()
    assert parsed["result"] == "bullish"


def test_agent_call_bad_json(mock_anthropic):
    mock_anthropic.messages.create.return_value.content = [MagicMock(text="not json")]
    agent = ConcreteAgent(api_key="test-key", model="claude-sonnet-4-6-20250514", max_tokens=1024)
    result = agent.run(data="test")
    assert result.parse_json() is None


def test_agent_records_model(mock_anthropic):
    agent = ConcreteAgent(api_key="test-key", model="claude-sonnet-4-6-20250514", max_tokens=1024)
    result = agent.run(data="test")
    assert result.model == "claude-sonnet-4-6-20250514"


def test_agent_retries_transient_failure_and_succeeds(mock_anthropic, monkeypatch):
    """The bounded budget rides through one transient provider hiccup."""
    # No real sleeping during the test.
    monkeypatch.setattr("time.sleep", lambda s: None)

    calls = {"n": 0}
    good_response = MagicMock()
    good_response.content = [MagicMock(text='{"result": "ok"}')]
    good_response.usage.input_tokens = 10
    good_response.usage.output_tokens = 5

    def side_effect(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise ConnectionError("DNS temporarily unavailable")
        return good_response

    mock_anthropic.messages.create.side_effect = side_effect

    agent = ConcreteAgent(api_key="test-key", model="claude-sonnet-4-6-20250514", max_tokens=1024)
    result = agent.run(data="test")
    assert calls["n"] == 2, f"expected 2 attempts, got {calls['n']}"
    assert result.raw_text == '{"result": "ok"}'


def test_agent_retry_budget_is_two_attempts_by_default(mock_anthropic, monkeypatch):
    """A sustained outage cannot amplify one logical call beyond two requests."""
    monkeypatch.setattr("time.sleep", lambda s: None)

    calls = {"n": 0}

    def always_fail(*args, **kwargs):
        calls["n"] += 1
        raise ConnectionError("network is down")

    mock_anthropic.messages.create.side_effect = always_fail

    agent = ConcreteAgent(api_key="test-key", model="claude-sonnet-4-6-20250514", max_tokens=1024)
    with pytest.raises(ConnectionError):
        agent.run(data="test")
    assert calls["n"] == 2, f"expected 2 attempts before giving up, got {calls['n']}"


def test_agent_retry_budget_respects_env_override(mock_anthropic, monkeypatch):
    """QUANT_AGENT_MAX_RETRIES lets tests/deployments tighten or
    loosen the retry budget without touching code."""
    monkeypatch.setattr("time.sleep", lambda s: None)
    monkeypatch.setenv("QUANT_AGENT_MAX_RETRIES", "2")

    calls = {"n": 0}

    def always_fail(*args, **kwargs):
        calls["n"] += 1
        raise ConnectionError("x")

    mock_anthropic.messages.create.side_effect = always_fail

    agent = ConcreteAgent(api_key="test-key", model="claude-sonnet-4-6-20250514", max_tokens=1024)
    with pytest.raises(ConnectionError):
        agent.run(data="test")
    assert calls["n"] == 2


def test_retry_backoff_is_aws_full_jitter():
    """`_retry_backoff_seconds` must implement AWS Full Jitter exactly:
    `random_between(0, min(cap, base * 2**attempt))`.

    SOURCE: https://aws.amazon.com/blogs/architecture/exponential-backoff-and-jitter/

    REPLACES the old "deterministic floor plus jitter on top" assertion
    ([base, 2*base)). That floor re-synchronises every client that failed at
    the same instant onto the same next instant, which is the behaviour the
    AWS article measures as completing the same work in MORE time with MORE
    competing calls. The lower bound the desk still needs for a rate limit
    is asserted separately, and read off Google's own published guidance —
    see test_capacity_backoff_honours_googles_documented_one_second_floor.
    """
    from src.agents.base import _retry_backoff_seconds, _BACKOFF_CAP_S

    for attempt in range(9):
        bound = min(_BACKOFF_CAP_S, float(2**attempt))
        for _ in range(200):
            wait = _retry_backoff_seconds(attempt)
            assert 0.0 <= wait < bound or bound == 0.0, f"attempt={attempt}: expected wait in [0, {bound}), got {wait}"
    # The cap must actually bind, or "full jitter" is unbounded in attempt.
    assert all(_retry_backoff_seconds(20) <= _BACKOFF_CAP_S for _ in range(200))


def test_retry_backoff_decorrelates_across_calls():
    """Two consecutive calls at the same attempt index must produce
    different waits with high probability. This is the property that
    decorrelates retry timing from outage timing — without it, every
    session running the SAME attempt at the SAME wall-clock would
    produce the same retry pattern. With ~50% jitter spread, repeated
    calls should produce distinct values."""
    from src.agents.base import _retry_backoff_seconds

    waits = {_retry_backoff_seconds(3) for _ in range(50)}
    # 50 samples in a continuous range should yield many distinct
    # values; require at least 30 to allow for some collisions but
    # rule out a constant function.
    assert len(waits) >= 30, (
        f"jitter must produce variability across calls; got {len(waits)} unique values from 50 samples"
    )


def test_agent_retry_budget_default_constant_is_two():
    """Pin the incident-response ceiling: initial request plus one retry."""
    from src.agents.base import _DEFAULT_MAX_RETRIES

    assert _DEFAULT_MAX_RETRIES == 2


def test_anthropic_client_gets_explicit_http_timeout():
    """LLM clients must pin an explicit per-request HTTP timeout —
    SDK default is 600s, which leaves the morning window exposed to a
    single stalled SSE stream. Regression-guards the _LLM_HTTP_TIMEOUT
    invariant so a future refactor can't silently drop the kwarg."""
    from src.agents.base import _LLM_HTTP_TIMEOUT

    with patch("anthropic.Anthropic") as mock_cls:
        ConcreteAgent(api_key="k", model="claude-sonnet-4-6-20250514", max_tokens=1024)
        # max_retries=0: the SDK's 2 internal retries would silently triple
        # every _execute() attempt — the agent loop is the single retry owner.
        mock_cls.assert_called_once_with(api_key="k", timeout=_LLM_HTTP_TIMEOUT, max_retries=0)


def test_openai_client_gets_explicit_http_timeout():
    """Same invariant for the OpenAI path. Model prefix 'gpt-' routes
    to OpenAI (see _OPENAI_PREFIXES), and tech_analyst runs on gpt-5.4
    — that's the agent that 2026-04-23 morning failed on, so this
    branch is the load-bearing one."""
    from src.agents.base import _LLM_HTTP_TIMEOUT

    with patch("openai.OpenAI") as mock_cls:
        ConcreteAgent(api_key="k", model="gpt-5.4", max_tokens=1024)
        # base_url defaults to None (api.openai.com) unless OPENAI_BASE_URL is set
        # for relay routing — see test_openai_base_url_routes_through_relay.
        mock_cls.assert_called_once_with(api_key="k", base_url=None, timeout=_LLM_HTTP_TIMEOUT, max_retries=0)


def test_parse_json_full_text_still_wins_when_clean():
    """Clean full-text JSON parses directly — no candidate search triggered."""
    from src.agents.base import AgentResult

    result = AgentResult(
        raw_text='{"decisions": [], "portfolio_view": "flat"}',
        tokens_used=0,
        model="test",
    )
    parsed = result.parse_json()
    assert parsed == {"decisions": [], "portfolio_view": "flat"}


# === Missing-opening-quote repair (root cause of the 2026-09-02 news_analyst
# "4 required top-level fields missing" structural failure — see
# docs/INCIDENT_HISTORY.md). The real production payload had every field
# present and correct EXCEPT one interior key that lost its opening quote
# (`  pm_briefing": "..."` instead of `  "pm_briefing": "..."`), which broke
# whole-object json.loads() and made the fragment scanner below return an
# inner sub-object lacking all 4 required fields. These tests reproduce the
# same STRUCTURE (not the verbatim production text) to keep the fix's
# behavior pinned. ===


def test_parse_json_repair_does_not_alter_already_valid_json():
    """The repair pass must never fire (or never change the result) when
    the JSON already parses cleanly — it only runs as a fallback after the
    direct parse fails."""
    from src.agents.base import AgentResult

    raw = '{"pm_briefing": "fine", "market_sentiment": "neutral"}'
    result = AgentResult(raw_text=raw, tokens_used=0, model="test")
    assert result.parse_json() == {"pm_briefing": "fine", "market_sentiment": "neutral"}


# === Cost tracking edge cases (R7 self-audit) ===


def test_run_records_cost_for_known_model(monkeypatch):
    """Happy path: tokens land, model is in PRICING, AgentResult carries cost.

    Uses a test-fixture PRICING (input=$10/M, output=$50/M) instead of
    the live PRICING dict. Reading PRICING for BOTH `expected` and
    `actual` is tautological — a regression that wiped PRICING to
    {"input": 0, "output": 0} would pass (both sides agree on $0).
    Pinned fixture rates force a real math check.
    """
    # monkeypatch auto-reverts the PRICING entry at test exit.
    from src import cost_table

    monkeypatch.setitem(
        cost_table.PRICING,
        "claude-opus-4-7",
        {"input": 10.0, "output": 50.0},
    )

    with patch("anthropic.Anthropic") as mock_cls:
        mock_client = MagicMock()
        mock_response = MagicMock()
        mock_response.content = [MagicMock(text='{"x": 1}')]
        mock_response.usage.input_tokens = 100
        mock_response.usage.output_tokens = 50
        mock_response.usage.cache_creation_input_tokens = 0
        mock_response.usage.cache_read_input_tokens = 0
        mock_client.messages.create.return_value = mock_response
        mock_cls.return_value = mock_client

        agent = ConcreteAgent(api_key="t", model="claude-opus-4-7", max_tokens=128)
        result = agent.run(data="x")
        # 100 × $10/M + 50 × $50/M = $0.001 + $0.0025 = $0.0035
        expected = (100 * 10.0 + 50 * 50.0) / 1_000_000
        assert result.cost_usd is not None
        assert abs(result.cost_usd - expected) < 1e-9
        assert result.input_tokens == 100
        assert result.output_tokens == 50


def test_run_flags_cost_none_for_unknown_model():
    """Model not in PRICING (e.g. typo or new model) → cost_usd=None
    so the operator sees '$?.??' in the push instead of a fake $0.
    Token counts must still land correctly."""
    with patch("anthropic.Anthropic") as mock_cls:
        mock_client = MagicMock()
        mock_response = MagicMock()
        mock_response.content = [MagicMock(text='{"x": 1}')]
        mock_response.usage.input_tokens = 100
        mock_response.usage.output_tokens = 50
        mock_response.usage.cache_creation_input_tokens = 0
        mock_response.usage.cache_read_input_tokens = 0
        mock_client.messages.create.return_value = mock_response
        mock_cls.return_value = mock_client

        agent = ConcreteAgent(api_key="t", model="claude-opus-99-future", max_tokens=128)
        result = agent.run(data="x")
        assert result.cost_usd is None
        assert result.input_tokens == 100
        assert result.output_tokens == 50


def test_run_flags_cost_none_when_usage_is_zero_zero():
    """Rare SDK error path returns 0 input + 0 output tokens. Pre-fix
    code would silently log cost=$0 (the math is correct but the
    SEMANTICS are wrong — we don't actually know what we spent).
    Pin: that case yields cost_usd=None so the operator notices."""
    with patch("anthropic.Anthropic") as mock_cls:
        mock_client = MagicMock()
        mock_response = MagicMock()
        mock_response.content = [MagicMock(text='{"x": 1}')]
        mock_response.usage.input_tokens = 0
        mock_response.usage.output_tokens = 0
        mock_response.usage.cache_creation_input_tokens = 0
        mock_response.usage.cache_read_input_tokens = 0
        mock_client.messages.create.return_value = mock_response
        mock_cls.return_value = mock_client

        agent = ConcreteAgent(api_key="t", model="claude-opus-4-7", max_tokens=128)
        result = agent.run(data="x")
        # cost=None NOT $0.00 — flag missing-usage case as unknown
        # so it doesn't sum into a confident daily total.
        assert result.cost_usd is None
        assert result.tokens_used == 0


def test_run_with_anthropic_caching_sums_input_correctly():
    """When prompt caching is enabled in the future, input is split
    into input_tokens (uncached) + cache_creation_input_tokens +
    cache_read_input_tokens. Token COUNT must include all three."""
    with patch("anthropic.Anthropic") as mock_cls:
        mock_client = MagicMock()
        mock_response = MagicMock()
        mock_response.content = [MagicMock(text='{"x": 1}')]
        # Simulate caching: 1000 fresh + 500 cache-written + 3000 cache-hit
        mock_response.usage.input_tokens = 1000
        mock_response.usage.cache_creation_input_tokens = 500
        mock_response.usage.cache_read_input_tokens = 3000
        mock_response.usage.output_tokens = 200
        mock_client.messages.create.return_value = mock_response
        mock_cls.return_value = mock_client

        agent = ConcreteAgent(api_key="t", model="claude-opus-4-7", max_tokens=128)
        result = agent.run(data="x")
        # Token COUNT must include all three input fields.
        assert result.input_tokens == 1000 + 500 + 3000
        assert result.output_tokens == 200


# === retry classification / truncation / prompt-cache (audit re-scan) ===


class _FakeStatusError(Exception):
    """Mimics an SDK APIStatusError carrying an HTTP status_code."""

    def __init__(self, status_code, msg="boom"):
        super().__init__(msg)
        self.status_code = status_code


def test_agent_fast_fails_non_retryable_4xx(mock_anthropic, monkeypatch):
    """A 401/400-class error is not transient — retrying burns the budget
    and masks 'your key is dead'. It must fail on the FIRST attempt."""
    monkeypatch.setattr("time.sleep", lambda s: None)
    calls = {"n": 0}

    def auth_fail(*a, **k):
        calls["n"] += 1
        raise _FakeStatusError(401, "invalid x-api-key")

    mock_anthropic.messages.create.side_effect = auth_fail
    agent = ConcreteAgent(api_key="bad", model="claude-sonnet-4-6-20250514", max_tokens=64)
    with pytest.raises(_FakeStatusError):
        agent.run(data="test")
    assert calls["n"] == 1, f"non-retryable error must not retry; got {calls['n']} attempts"


@pytest.mark.parametrize("status", [429, 503])
def test_agent_retries_429_and_5xx(mock_anthropic, monkeypatch, status):
    """Each transient class gets the one bounded retry still allowed."""
    monkeypatch.setattr("time.sleep", lambda s: None)
    calls = {"n": 0}
    good = MagicMock()
    good.content = [MagicMock(text='{"result": "ok"}')]
    good.usage.input_tokens = 1
    good.usage.output_tokens = 1
    good.stop_reason = "end_turn"

    def flaky(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            raise _FakeStatusError(status, "transient")
        return good

    mock_anthropic.messages.create.side_effect = flaky
    agent = ConcreteAgent(api_key="k", model="claude-sonnet-4-6-20250514", max_tokens=64)
    result = agent.run(data="test")
    assert calls["n"] == 2
    assert result.raw_text == '{"result": "ok"}'


def test_agent_flags_truncated_response(mock_anthropic):
    """stop_reason='max_tokens' marks the result truncated so a cut-off
    decision isn't mistaken for a deliberate 'no signal'."""
    mock_anthropic.messages.create.return_value.stop_reason = "max_tokens"
    agent = ConcreteAgent(api_key="k", model="claude-sonnet-4-6-20250514", max_tokens=8)
    result = agent.run(data="test")
    assert result.truncated is True
    assert result.finish_reason == "max_tokens"


def test_agent_normal_response_not_flagged_truncated(mock_anthropic):
    mock_anthropic.messages.create.return_value.stop_reason = "end_turn"
    agent = ConcreteAgent(api_key="k", model="claude-sonnet-4-6-20250514", max_tokens=1024)
    result = agent.run(data="test")
    assert result.truncated is False
    assert result.finish_reason == "end_turn"


def test_anthropic_system_prompt_uses_uncached_pricing_shape(mock_anthropic):
    """Caching stays off until cache write/read rates are modeled by the breaker."""
    mock_anthropic.messages.create.return_value.stop_reason = "end_turn"
    agent = ConcreteAgent(api_key="k", model="claude-sonnet-4-6-20250514", max_tokens=64)
    agent.run(data="test")
    _, kwargs = mock_anthropic.messages.create.call_args
    assert kwargs["system"] == "You are a test agent."


# === cross-provider failover (OpenAI primary -> Anthropic fallback) ===


def _good_anthropic_response(text='{"result": "ok"}'):
    r = MagicMock()
    r.content = [MagicMock(text=text)]
    r.usage.input_tokens = 10
    r.usage.output_tokens = 5
    r.stop_reason = "end_turn"
    return r


def test_failover_openai_exhausted_then_anthropic_succeeds(monkeypatch):
    """OpenAI primary fails all retries → ONE Anthropic call with the configured
    fallback model succeeds → result carries the fallback model (so cost is
    priced right). fallback_provider/fallback_model pinned explicitly here so
    the test keeps proving the Anthropic-SDK failover path specifically,
    independent of whatever the process-wide default fallback is."""
    monkeypatch.setattr("time.sleep", lambda s: None)
    monkeypatch.setenv("QUANT_AGENT_MAX_RETRIES", "2")
    oai = MagicMock()
    oai.chat.completions.create.side_effect = ConnectionError("openai down")
    anth = MagicMock()
    anth.messages.create.return_value = _good_anthropic_response()
    with patch("openai.OpenAI", return_value=oai), patch("anthropic.Anthropic", return_value=anth):
        agent = ConcreteAgent(
            api_key="k",
            model="gpt-5.5",
            max_tokens=64,
            fallback_api_key="fk",
            fallback_provider="anthropic",
            fallback_model="claude-opus-4-7",
        )
        result = agent.run(data="x")
    assert result.raw_text == '{"result": "ok"}'
    assert result.model == "claude-opus-4-7"  # actual model used → correct pricing
    anth.messages.create.assert_called_once()  # single-shot failover (no retry)


def test_no_failover_when_fallback_key_empty(monkeypatch):
    """No fallback key → the original OpenAI error propagates; no Anthropic call."""
    monkeypatch.setattr("time.sleep", lambda s: None)
    monkeypatch.setenv("QUANT_AGENT_MAX_RETRIES", "2")
    oai = MagicMock()
    oai.chat.completions.create.side_effect = ConnectionError("down")
    with patch("openai.OpenAI", return_value=oai), patch("anthropic.Anthropic") as A:
        agent = ConcreteAgent(api_key="k", model="gpt-5.5", max_tokens=64)  # no fallback_api_key
        with pytest.raises(ConnectionError):
            agent.run(data="x")
        A.assert_not_called()


def test_no_failover_when_fallback_pair_identical_to_primary(monkeypatch):
    """Never fail over onto the exact (provider, model) that just failed —
    the generalized rule that replaces the old "primary is Claude" special
    case. Exercised here with a Claude primary whose fallback is EXPLICITLY
    pinned to the identical (anthropic, claude-opus-4-7) pair; construction
    with a fallback key must NOT raise even though it ends up unreachable."""
    monkeypatch.setattr("time.sleep", lambda s: None)
    monkeypatch.setenv("QUANT_AGENT_MAX_RETRIES", "2")
    anth = MagicMock()
    anth.messages.create.side_effect = ConnectionError("anthropic down")
    with patch("anthropic.Anthropic", return_value=anth):
        agent = ConcreteAgent(
            api_key="k",
            model="claude-opus-4-7",
            max_tokens=64,
            fallback_api_key="fk",
            fallback_provider="anthropic",
            fallback_model="claude-opus-4-7",
        )
        assert agent._failover_reachable is False
        with pytest.raises(ConnectionError):
            agent.run(data="x")
    assert anth.messages.create.call_count == 2  # 2 primary retries, no extra failover call


def test_failover_fires_for_a_claude_primary_when_fallback_pair_differs(monkeypatch):
    """The generalization this rule enables: a Claude primary is no longer
    categorically excluded from failover — only an IDENTICAL fallback pair
    is. Here the (default) fallback is openrouter + gemini, which differs
    from the anthropic + claude-opus-4-7 primary, so failover fires."""
    monkeypatch.setattr("time.sleep", lambda s: None)
    monkeypatch.setenv("QUANT_AGENT_MAX_RETRIES", "2")
    anth = MagicMock()
    anth.messages.create.side_effect = ConnectionError("anthropic down")
    ork = MagicMock()
    ork.chat.completions.create.return_value = _openai_stream_mock().chat.completions.create.return_value
    with patch("anthropic.Anthropic", return_value=anth), patch("openai.OpenAI", return_value=ork):
        agent = ConcreteAgent(api_key="k", model="claude-opus-4-7", max_tokens=64, fallback_api_key="fk")
        assert agent._failover_reachable is True
        result = agent.run(data="x")
    assert result.model == "google/gemini-3.5-flash-lite"
    assert result.actual_provider == "openrouter"
    assert anth.messages.create.call_count == 2  # both primary retries burned
    ork.chat.completions.create.assert_called_once()  # single-shot failover


def test_failover_both_fail_reraises_original_openai_error(monkeypatch):
    """OpenAI AND Anthropic both fail → the ORIGINAL OpenAI error surfaces (so
    the operator sees the root cause), not the secondary Anthropic error."""
    monkeypatch.setattr("time.sleep", lambda s: None)
    monkeypatch.setenv("QUANT_AGENT_MAX_RETRIES", "2")

    class OpenAIDown(ConnectionError):
        pass

    class AnthropicDown(Exception):
        pass

    oai = MagicMock()
    oai.chat.completions.create.side_effect = OpenAIDown("openai")
    anth = MagicMock()
    anth.messages.create.side_effect = AnthropicDown("anthropic")
    with patch("openai.OpenAI", return_value=oai), patch("anthropic.Anthropic", return_value=anth):
        agent = ConcreteAgent(
            api_key="k",
            model="gpt-5.5",
            max_tokens=64,
            fallback_api_key="fk",
            fallback_provider="anthropic",
            fallback_model="claude-opus-4-7",
        )
        with pytest.raises(OpenAIDown):
            agent.run(data="x")
    anth.messages.create.assert_called_once()


# === DeepSeek provider (OpenAI-compatible API, distinct routing) ===


def _deepseek_oai_mock(content='{"result": "ok"}', finish_reason="stop", reasoning=None):
    """Build a mock OpenAI client whose chat.completions.create returns a
    DeepSeek-shaped (OpenAI-shaped) response."""
    oai = MagicMock()
    resp = MagicMock()
    msg = MagicMock()
    msg.content = content
    if reasoning is not None:
        msg.reasoning_content = reasoning
    choice = MagicMock()
    choice.message = msg
    choice.finish_reason = finish_reason
    resp.choices = [choice]
    resp.usage.prompt_tokens = 100
    resp.usage.completion_tokens = 50
    oai.chat.completions.create.return_value = resp
    return oai


def test_deepseek_routes_to_openai_sdk_with_base_url():
    """A deepseek-* model uses the OpenAI SDK pointed at the DeepSeek base_url,
    NOT Anthropic; _use_deepseek True and _use_openai False."""
    from src.agents.base import _DEEPSEEK_BASE_URL

    with patch("openai.OpenAI") as oai_cls, patch("anthropic.Anthropic") as anth_cls:
        oai_cls.return_value = _deepseek_oai_mock()
        agent = ConcreteAgent(api_key="dk", model="deepseek-v4-flash", max_tokens=4096)
        assert agent._use_deepseek is True
        assert agent._use_openai is False
        anth_cls.assert_not_called()
        _, kwargs = oai_cls.call_args
        assert kwargs.get("base_url") == _DEEPSEEK_BASE_URL == "https://api.deepseek.com"
        assert kwargs.get("api_key") == "dk"


def test_deepseek_sends_max_tokens_not_max_completion_tokens():
    """THE load-bearing fact: DeepSeek honors `max_tokens`, ignores
    `max_completion_tokens`. The OpenAI path must keep sending
    `max_completion_tokens`; the DeepSeek path must send `max_tokens`."""
    with patch("openai.OpenAI") as oai_cls:
        client = _deepseek_oai_mock()
        oai_cls.return_value = client
        agent = ConcreteAgent(api_key="dk", model="deepseek-v4-flash", max_tokens=4096)
        agent.run(data="x")
        _, call_kwargs = client.chat.completions.create.call_args
        assert "max_tokens" in call_kwargs
        assert "max_completion_tokens" not in call_kwargs

    # mirror: the OpenAI path still uses max_completion_tokens (and streams —
    # see the relay-hardening section at the end of this file)
    with patch("openai.OpenAI") as oai_cls:
        client = _openai_stream_mock()
        oai_cls.return_value = client
        agent = ConcreteAgent(api_key="k", model="gpt-5.5", max_tokens=4096)
        agent.run(data="x")
        _, call_kwargs = client.chat.completions.create.call_args
        assert "max_completion_tokens" in call_kwargs
        assert "max_tokens" not in call_kwargs


def test_deepseek_clamps_max_tokens_to_model_ceiling():
    """max_tokens is clamped to the per-model ceiling (DeepSeek rejects, does
    not clamp, an over-ceiling value). v4-flash (384000) passes 128000 through;
    an unknown deepseek-* id clamps to the conservative 8192 default."""
    from src.agents.base import _DEEPSEEK_DEFAULT_CEILING

    # under ceiling → unchanged
    with patch("openai.OpenAI") as oai_cls:
        client = _deepseek_oai_mock()
        oai_cls.return_value = client
        ConcreteAgent(api_key="dk", model="deepseek-v4-flash", max_tokens=128000).run(data="x")
        _, kw = client.chat.completions.create.call_args
        assert kw["max_tokens"] == 128000
    # unknown deepseek id → clamped to conservative default
    with patch("openai.OpenAI") as oai_cls:
        client = _deepseek_oai_mock()
        oai_cls.return_value = client
        ConcreteAgent(api_key="dk", model="deepseek-zzz-unknown", max_tokens=128000).run(data="x")
        _, kw = client.chat.completions.create.call_args
        assert kw["max_tokens"] == _DEEPSEEK_DEFAULT_CEILING == 8192


def test_deepseek_parses_content_and_records_model():
    with patch("openai.OpenAI") as oai_cls:
        oai_cls.return_value = _deepseek_oai_mock(content='{"result": "ok"}')
        agent = ConcreteAgent(api_key="dk", model="deepseek-v4-flash", max_tokens=4096)
        result = agent.run(data="x")
        assert result.parse_json() == {"result": "ok"}
        assert result.model == "deepseek-v4-flash"
        assert result.input_tokens == 100 and result.output_tokens == 50


def test_deepseek_empty_content_with_reasoning_returns_empty():
    """A reasoner truncated mid-thought returns empty content + reasoning_content.
    We parse only content → empty string (downstream None distinguishable via the
    truncated flag), and must not crash on the non-standard field."""
    with patch("openai.OpenAI") as oai_cls:
        oai_cls.return_value = _deepseek_oai_mock(content="", finish_reason="length", reasoning="thinking...")
        agent = ConcreteAgent(api_key="dk", model="deepseek-reasoner", max_tokens=4096)
        result = agent.run(data="x")
        assert result.raw_text == ""
        assert result.truncated is True  # finish_reason=length


def test_is_deepseek_model_prefix_routing():
    from src.agents.base import _is_deepseek_model, _is_openai_model

    assert _is_deepseek_model("deepseek-v4-flash") is True
    assert _is_deepseek_model("deepseek-chat") is True
    assert _is_openai_model("deepseek-v4-flash") is False  # must NOT be OpenAI
    assert _is_deepseek_model("gpt-5.5") is False
    assert _is_deepseek_model("claude-opus-4-7") is False


def test_insufficient_system_resource_flags_truncated():
    """DeepSeek's resource-interruption finish_reason (200 with a cut-off body)
    is flagged truncated, like a token-limit cutoff."""
    with patch("openai.OpenAI") as oai_cls:
        oai_cls.return_value = _deepseek_oai_mock(content='{"x":1}', finish_reason="insufficient_system_resource")
        agent = ConcreteAgent(api_key="dk", model="deepseek-v4-flash", max_tokens=4096)
        assert agent.run(data="x").truncated is True


def test_is_retryable_402_insufficient_balance_fast_fails():
    """DeepSeek 402 'Insufficient Balance' must NOT retry (dead-money, like a
    dead key) so the failover takes over; 429/503 stay retryable."""
    from src.agents.base import _is_retryable

    class Err(Exception):
        def __init__(self, status):
            self.status_code = status

    assert _is_retryable(Err(402)) is False
    assert _is_retryable(Err(429)) is True
    assert _is_retryable(Err(503)) is True
    assert _is_retryable(Err(400)) is False
    assert _is_retryable(Err(401)) is False


def test_deepseek_primary_fails_over_to_anthropic(monkeypatch):
    """DeepSeek primary exhausted → ONE Anthropic failover call; result carries
    the fallback model so cost is priced right."""
    monkeypatch.setattr("time.sleep", lambda s: None)
    monkeypatch.setenv("QUANT_AGENT_MAX_RETRIES", "2")
    ds = MagicMock()
    ds.chat.completions.create.side_effect = ConnectionError("deepseek down")
    anth = MagicMock()
    anth.messages.create.return_value = _good_anthropic_response()
    with patch("openai.OpenAI", return_value=ds), patch("anthropic.Anthropic", return_value=anth):
        agent = ConcreteAgent(
            api_key="dk",
            model="deepseek-v4-flash",
            max_tokens=64,
            fallback_api_key="fk",
            fallback_provider="anthropic",
            fallback_model="claude-opus-4-7",
        )
        result = agent.run(data="x")
    assert result.raw_text == '{"result": "ok"}'
    assert result.model == "claude-opus-4-7"
    anth.messages.create.assert_called_once()


def test_deepseek_primary_no_failover_without_key(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda s: None)
    monkeypatch.setenv("QUANT_AGENT_MAX_RETRIES", "2")
    ds = MagicMock()
    ds.chat.completions.create.side_effect = ConnectionError("down")
    with patch("openai.OpenAI", return_value=ds), patch("anthropic.Anthropic") as A:
        agent = ConcreteAgent(api_key="dk", model="deepseek-v4-flash", max_tokens=64)  # no fallback key
        with pytest.raises(ConnectionError):
            agent.run(data="x")
        A.assert_not_called()


# === OPENAI_BASE_URL relay routing (中转站) ===


def test_openai_base_url_routes_through_relay(monkeypatch):
    """When OPENAI_BASE_URL is set, the OpenAI client is built pointing at the
    relay (same chat/completions wire format, different host)."""
    monkeypatch.setenv("OPENAI_BASE_URL", "http://relay.test/v1")
    with patch("openai.OpenAI") as oai_cls:
        oai_cls.return_value = MagicMock()
        ConcreteAgent(api_key="relay-key", model="gpt-5.5", max_tokens=64)
        _, kwargs = oai_cls.call_args
        assert kwargs.get("base_url") == "http://relay.test/v1"
        assert kwargs.get("api_key") == "relay-key"


def test_openai_base_url_defaults_none_when_unset(monkeypatch):
    """Unset/empty OPENAI_BASE_URL → base_url=None → SDK default (api.openai.com)."""
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    with patch("openai.OpenAI") as oai_cls:
        oai_cls.return_value = MagicMock()
        ConcreteAgent(api_key="k", model="gpt-5.5", max_tokens=64)
        assert oai_cls.call_args.kwargs.get("base_url") is None
    monkeypatch.setenv("OPENAI_BASE_URL", "   ")  # whitespace → treated as unset
    with patch("openai.OpenAI") as oai_cls:
        oai_cls.return_value = MagicMock()
        ConcreteAgent(api_key="k", model="gpt-5.5", max_tokens=64)
        assert oai_cls.call_args.kwargs.get("base_url") is None


def test_deepseek_base_url_unaffected_by_openai_base_url(monkeypatch):
    """A deepseek-* model keeps its own base_url even if OPENAI_BASE_URL is set."""
    from src.agents.base import _DEEPSEEK_BASE_URL

    monkeypatch.setenv("OPENAI_BASE_URL", "http://relay/v1")
    with patch("openai.OpenAI") as oai_cls:
        oai_cls.return_value = MagicMock()
        ConcreteAgent(api_key="dk", model="deepseek-v4-flash", max_tokens=64)
        assert oai_cls.call_args.kwargs.get("base_url") == _DEEPSEEK_BASE_URL


# === OPENAI_CA_BUNDLE — trust a private relay CA (Caddy-internal etc.) ===


def test_openai_ca_bundle_trusts_relay_ca(monkeypatch, tmp_path):
    """With OPENAI_CA_BUNDLE set, the OpenAI client is built with an httpx
    client that verifies against THAT CA (full verification, just a private
    trust anchor) and is passed as http_client."""
    ca = tmp_path / "relay_ca.pem"
    ca.write_text("-----BEGIN CERTIFICATE-----\nfake\n-----END CERTIFICATE-----\n")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://relay.test/v1")
    monkeypatch.setenv("OPENAI_CA_BUNDLE", str(ca))
    with patch("openai.OpenAI") as oai_cls, patch("httpx.Client") as hc:
        oai_cls.return_value = MagicMock()
        hc.return_value = MagicMock()
        ConcreteAgent(api_key="k", model="gpt-5.5", max_tokens=64)
        # httpx client verifies against the pinned CA (NOT verify=False)
        assert hc.call_args.kwargs.get("verify") == str(ca)
        # and it's handed to the OpenAI SDK as the transport
        assert "http_client" in oai_cls.call_args.kwargs
        assert oai_cls.call_args.kwargs.get("base_url") == "https://relay.test/v1"


def test_openai_no_ca_bundle_uses_default_trust(monkeypatch):
    """Unset OPENAI_CA_BUNDLE → no custom http_client → SDK default (public CAs)."""
    monkeypatch.delenv("OPENAI_CA_BUNDLE", raising=False)
    monkeypatch.setenv("OPENAI_BASE_URL", "https://relay.test/v1")
    with patch("openai.OpenAI") as oai_cls:
        oai_cls.return_value = MagicMock()
        ConcreteAgent(api_key="k", model="gpt-5.5", max_tokens=64)
        assert "http_client" not in oai_cls.call_args.kwargs
        assert oai_cls.call_args.kwargs.get("timeout") is not None


# === streamed OpenAI path + relay hardening (CF-524 / 429 storms / degenerate 200s) ===


def _stream_chunk(piece=None, finish_reason=None, usage=None, error=None, chunk_id=None):
    """One streamed chunk. A usage-only chunk (include_usage's final extra
    chunk) carries an empty choices list."""
    chunk = MagicMock()
    chunk.usage = usage  # explicit None — MagicMock auto-attrs would count as usage
    # Same hazard for the mid-stream error field: an auto-attr MagicMock is
    # truthy and would read as "the provider errored" on every healthy chunk.
    # A real SDK chunk has no `error` attribute at all on the happy path.
    chunk.error = error
    chunk.model_extra = {}
    # Same hazard again for `id`: an auto-attr MagicMock is truthy but not a
    # str, so _call_openai's isinstance guard would ignore it anyway — but
    # setting it explicitly (default None) keeps every existing test's
    # generation_id capture honest rather than relying on that guard alone.
    chunk.id = chunk_id
    if piece is None and finish_reason is None and usage is not None:
        chunk.choices = []
        return chunk
    delta = MagicMock()
    delta.content = piece
    choice = MagicMock()
    choice.delta = delta
    choice.finish_reason = finish_reason
    chunk.choices = [choice]
    return chunk


def _stream_usage(prompt=100, completion=50):
    u = MagicMock()
    u.prompt_tokens = prompt
    u.completion_tokens = completion
    return u


def _openai_stream_mock(pieces=('{"result"', ': "ok"}'), finish_reason="stop", usage="default", gen_id=None):
    """Mock OpenAI client whose chat.completions.create returns a streamed
    (iterable-of-chunks) response — the shape _call_openai consumes.
    finish_reason=None simulates a connection cut mid-generation (no final
    chunk ever carries a finish_reason). usage=None simulates a relay that
    ignores stream_options include_usage. gen_id, when set, is stamped on
    every chunk — a real stream repeats the same generation id throughout."""
    oai = MagicMock()
    chunks = [_stream_chunk(piece=p, chunk_id=gen_id) for p in pieces]
    if finish_reason is not None:
        chunks.append(_stream_chunk(finish_reason=finish_reason, chunk_id=gen_id))
    if usage == "default":
        usage = _stream_usage()
    if usage is not None:
        chunks.append(_stream_chunk(usage=usage, chunk_id=gen_id))
    oai.chat.completions.create.return_value = chunks
    return oai


def test_openai_path_streams_and_assembles_content():
    """_call_openai streams (stream=True) on purpose: the relay sits behind
    Cloudflare's ~120s proxy read timeout (HTTP 524), so a non-streamed 120s+
    generation can never succeed through it (2026-06-08/09 outage). Content is
    assembled from the deltas; usage arrives on the include_usage final chunk."""
    with patch("openai.OpenAI") as oai_cls:
        client = _openai_stream_mock()
        oai_cls.return_value = client
        agent = ConcreteAgent(api_key="k", model="gpt-5.5", max_tokens=4096)
        result = agent.run(data="x")
        kw = client.chat.completions.create.call_args.kwargs
        assert kw.get("stream") is True
        assert kw.get("stream_options") == {"include_usage": True}
    assert result.raw_text == '{"result": "ok"}'
    assert result.parse_json() == {"result": "ok"}
    assert result.input_tokens == 100 and result.output_tokens == 50
    assert result.truncated is False


def test_openai_stream_without_usage_reports_unknown_cost():
    """A usage-less stream must not fabricate billing telemetry."""
    with patch("openai.OpenAI") as oai_cls:
        oai_cls.return_value = _openai_stream_mock(usage=None)
        agent = ConcreteAgent(api_key="k", model="gpt-5.5", max_tokens=4096)
        result = agent.run(data="x")
    assert result.raw_text == '{"result": "ok"}'
    assert result.input_tokens == 0 and result.output_tokens == 0
    assert result.cost_usd is None


def test_openai_stream_interrupted_discards_partial_and_retries(monkeypatch):
    """finish_reason never arrives = connection cut mid-generation. Partial
    text must be DISCARDED (a half-emitted PM decision parses like 'no
    trades') and the error must be retryable."""
    from src.agents.base import LLMStreamInterruptedError, _is_retryable

    monkeypatch.setattr("time.sleep", lambda s: None)
    monkeypatch.setenv("QUANT_AGENT_MAX_RETRIES", "2")
    with patch("openai.OpenAI") as oai_cls:
        client = _openai_stream_mock(pieces=('{"half":',), finish_reason=None, usage=None)
        oai_cls.return_value = client
        agent = ConcreteAgent(api_key="k", model="gpt-5.5", max_tokens=64)
        with pytest.raises(LLMStreamInterruptedError):
            agent.run(data="x")
        assert client.chat.completions.create.call_count == 2  # retried, then exhausted
    assert _is_retryable(LLMStreamInterruptedError("x")) is True


def test_openai_empty_content_raises_retryable_not_silent_success(monkeypatch):
    """A degenerate 200 (finish_reason=stop, empty body) must enter the
    retry/failover machinery — previously it returned '' as a SUCCESS,
    masquerading as a deliberate no-signal and burning the agent's one shot
    for the session."""
    from src.agents.base import LLMEmptyResponseError, _is_retryable

    monkeypatch.setattr("time.sleep", lambda s: None)
    monkeypatch.setenv("QUANT_AGENT_MAX_RETRIES", "2")
    with patch("openai.OpenAI") as oai_cls:
        oai_cls.return_value = _openai_stream_mock(pieces=(), finish_reason="stop")
        agent = ConcreteAgent(api_key="k", model="gpt-5.5", max_tokens=64)
        with pytest.raises(LLMEmptyResponseError):
            agent.run(data="x")
    assert _is_retryable(LLMEmptyResponseError("x")) is True


def test_openai_empty_content_on_length_is_truncation_not_error():
    """Empty body + truncation-family finish_reason = the whole budget burned
    before any visible text. That's a legit truncation (truncated=True), NOT
    a degenerate response — must not raise / retry / fail over."""
    with patch("openai.OpenAI") as oai_cls:
        oai_cls.return_value = _openai_stream_mock(pieces=(), finish_reason="length")
        agent = ConcreteAgent(api_key="k", model="gpt-5.5", max_tokens=64)
        result = agent.run(data="x")
    assert result.raw_text == ""
    assert result.truncated is True


def test_openai_empty_content_fails_over_to_anthropic(monkeypatch):
    """The point of raising on empty: the Anthropic failover can rescue the
    session instead of the agent silently contributing a blank."""
    monkeypatch.setattr("time.sleep", lambda s: None)
    monkeypatch.setenv("QUANT_AGENT_MAX_RETRIES", "2")
    anth = MagicMock()
    anth.messages.create.return_value = _good_anthropic_response()
    with (
        patch("openai.OpenAI", return_value=_openai_stream_mock(pieces=(), finish_reason="stop")),
        patch("anthropic.Anthropic", return_value=anth),
    ):
        agent = ConcreteAgent(
            api_key="k",
            model="gpt-5.5",
            max_tokens=64,
            fallback_api_key="fk",
            fallback_provider="anthropic",
            fallback_model="claude-opus-4-7",
        )
        result = agent.run(data="x")
    assert result.raw_text == '{"result": "ok"}'
    assert result.model == "claude-opus-4-7"


def test_anthropic_empty_content_raises_unless_truncation(monkeypatch):
    """Same guard on the Anthropic path: end_turn + empty content raises
    (degenerate 200); max_tokens + empty content is a legit whole-budget
    truncation and returns '' flagged truncated."""
    from src.agents.base import LLMEmptyResponseError

    monkeypatch.setattr("time.sleep", lambda s: None)
    monkeypatch.setenv("QUANT_AGENT_MAX_RETRIES", "1")
    with patch("anthropic.Anthropic") as anth_cls:
        r = _good_anthropic_response()
        r.content = []
        anth_cls.return_value.messages.create.return_value = r
        agent = ConcreteAgent(api_key="k", model="claude-opus-4-7", max_tokens=64)
        with pytest.raises(LLMEmptyResponseError):
            agent.run(data="x")
    # The call above exhausted the primary, which now DEMOTES the provider
    # process-wide for a cooldown (see RouteBreaker) — that is the point of
    # the half-open breaker and it is shared by every seat on that provider.
    # This test's second half is a separate scenario, not a second call in
    # the same outage, so the breaker is cleared between them.
    from src.agents.base import _reset_route_breakers_for_tests

    _reset_route_breakers_for_tests()
    with patch("anthropic.Anthropic") as anth_cls:
        r = _good_anthropic_response()
        r.content = []
        r.stop_reason = "max_tokens"
        anth_cls.return_value.messages.create.return_value = r
        agent = ConcreteAgent(api_key="k", model="claude-opus-4-7", max_tokens=64)
        result = agent.run(data="x")
        assert result.raw_text == "" and result.truncated is True


def test_deepseek_empty_content_nontruncation_raises(monkeypatch):
    """DeepSeek mirror of the degenerate-200 guard (the truncation-family
    empty case is covered by test_deepseek_empty_content_with_reasoning...)."""
    from src.agents.base import LLMEmptyResponseError

    monkeypatch.setattr("time.sleep", lambda s: None)
    monkeypatch.setenv("QUANT_AGENT_MAX_RETRIES", "1")
    with patch("openai.OpenAI") as oai_cls:
        oai_cls.return_value = _deepseek_oai_mock(content="", finish_reason="stop")
        agent = ConcreteAgent(api_key="dk", model="deepseek-v4-flash", max_tokens=64)
        with pytest.raises(LLMEmptyResponseError):
            agent.run(data="x")


def test_retry_deadline_abandons_primary_for_failover(monkeypatch):
    """The attempt budget alone doesn't bound wall-clock: under the relay's
    CF-524 mode each attempt burned 120-380s, so exhausting 7 attempts
    collided with the wrapper's 1200s kill and the failover below the loop
    never fired (2026-06-08/09 mornings). Past the deadline the primary is
    abandoned with attempts still left, and failover fires."""
    monkeypatch.setattr("time.sleep", lambda s: None)
    # monotonic: loop_start=0; attempt 1 fails at 200s (<480 → retry);
    # attempt 2 fails at 600s (>=480 → abandon primary, fail over).
    ticks = [0.0, 200.0, 600.0]
    monkeypatch.setattr("src.agents.base.time.monotonic", lambda: ticks.pop(0) if ticks else 600.0)
    # Pin the budget: the assertion below counts attempts, so an ambient
    # QUANT_AGENT_MAX_RETRIES override must not change the arithmetic.
    monkeypatch.setenv("QUANT_AGENT_MAX_RETRIES", "7")
    oai = MagicMock()
    oai.chat.completions.create.side_effect = ConnectionError("relay 524 storm")
    anth = MagicMock()
    anth.messages.create.return_value = _good_anthropic_response()
    with patch("openai.OpenAI", return_value=oai), patch("anthropic.Anthropic", return_value=anth):
        agent = ConcreteAgent(
            api_key="k",
            model="gpt-5.5",
            max_tokens=64,
            fallback_api_key="fk",
            fallback_provider="anthropic",
            fallback_model="claude-opus-4-7",
        )
        result = agent.run(data="x")
    assert oai.chat.completions.create.call_count == 2  # NOT the full 7-attempt budget
    assert result.model == "claude-opus-4-7"


def test_retry_deadline_env_override(monkeypatch):
    from src.agents.base import _retry_deadline_s, _DEFAULT_RETRY_DEADLINE_S

    monkeypatch.delenv("QUANT_AGENT_RETRY_DEADLINE_S", raising=False)
    assert _retry_deadline_s() == _DEFAULT_RETRY_DEADLINE_S
    monkeypatch.setenv("QUANT_AGENT_RETRY_DEADLINE_S", "60")
    assert _retry_deadline_s() == 60.0
    monkeypatch.setenv("QUANT_AGENT_RETRY_DEADLINE_S", "garbage")
    assert _retry_deadline_s() == _DEFAULT_RETRY_DEADLINE_S
    monkeypatch.setenv("QUANT_AGENT_RETRY_DEADLINE_S", "0")
    assert _retry_deadline_s() == 1.0  # floor — 0/negative would break the loop


def test_retry_after_hint_extraction():
    from src.agents.base import _retry_after_hint_seconds

    class HeaderErr(Exception):
        pass

    e = HeaderErr("429")
    resp = MagicMock()
    resp.headers = {"retry-after": "37"}
    e.response = resp
    assert _retry_after_hint_seconds(e) == 37.0

    class BodyErr(Exception):
        pass

    b = BodyErr("429")
    b.body = {"retry_after": 15}
    assert _retry_after_hint_seconds(b) == 15.0

    # relay 524 bodies embed the hint in the message text
    m = Exception('Concurrency limit exceeded, "retry_after": 120')
    assert _retry_after_hint_seconds(m) == 120.0

    # bool must not pass the numeric check; plain errors carry no hint
    t = BodyErr("429")
    t.body = {"retry_after": True}
    assert _retry_after_hint_seconds(t) is None
    assert _retry_after_hint_seconds(Exception("plain connection error")) is None


def test_retry_sleeps_at_least_the_server_hint(monkeypatch):
    """Backoff honors a server retry-after hint — sleeping 2s against a
    'come back in 90s' 429 just burns attempts against a closed door. A
    hostile/buggy hint is capped so it can't park the agent past the
    session window."""
    sleeps = []
    monkeypatch.setattr("time.sleep", lambda s: sleeps.append(s))
    monkeypatch.setenv("QUANT_AGENT_MAX_RETRIES", "2")

    oai = MagicMock()
    oai.chat.completions.create.side_effect = ConnectionError('Concurrency limit exceeded {"retry_after": 90}')
    with patch("openai.OpenAI", return_value=oai):
        agent = ConcreteAgent(api_key="k", model="gpt-5.5", max_tokens=64)
        with pytest.raises(ConnectionError):
            agent.run(data="x")
    assert sleeps == [90.0]

    # Same reason as in the empty-content test: the call above exhausted the
    # primary and demoted the provider process-wide. The second scenario is a
    # fresh outage, not a continuation.
    from src.agents.base import _reset_route_breakers_for_tests

    _reset_route_breakers_for_tests()
    sleeps.clear()
    oai2 = MagicMock()
    oai2.chat.completions.create.side_effect = ConnectionError('slow down {"retry_after": 6000}')
    with patch("openai.OpenAI", return_value=oai2):
        agent = ConcreteAgent(api_key="k", model="gpt-5.5", max_tokens=64)
        with pytest.raises(ConnectionError):
            agent.run(data="x")
    assert sleeps == [120.0]  # _RETRY_AFTER_CAP_S


def test_deepseek_client_disables_sdk_internal_retries():
    """max_retries=0 on EVERY SDK client: both SDKs default to 2 internal
    retries on 429/5xx, silently tripling each _execute() attempt and
    invalidating the retry-budget/deadline math. The agent loop is the
    single retry owner. (OpenAI/Anthropic primary constructors are asserted
    in the http-timeout tests above; this covers the DeepSeek constructor.)"""
    with patch("openai.OpenAI") as oai_cls:
        oai_cls.return_value = _deepseek_oai_mock()
        ConcreteAgent(api_key="dk", model="deepseek-v4-flash", max_tokens=64)
        assert oai_cls.call_args.kwargs.get("max_retries") == 0


def test_failover_client_disables_sdk_internal_retries(monkeypatch):
    """The failover Anthropic client is single-shot BY DESIGN — SDK-internal
    retries would quietly turn it into 3 shots."""
    monkeypatch.setattr("time.sleep", lambda s: None)
    monkeypatch.setenv("QUANT_AGENT_MAX_RETRIES", "1")
    oai = MagicMock()
    oai.chat.completions.create.side_effect = ConnectionError("down")
    with patch("openai.OpenAI", return_value=oai), patch("anthropic.Anthropic") as anth_cls:
        anth_cls.return_value.messages.create.return_value = _good_anthropic_response()
        agent = ConcreteAgent(
            api_key="k",
            model="gpt-5.5",
            max_tokens=64,
            fallback_api_key="fk",
            fallback_provider="anthropic",
            fallback_model="claude-opus-4-7",
        )
        agent.run(data="x")
        assert anth_cls.call_args.kwargs.get("max_retries") == 0


def test_llm_semaphore_released_after_success_and_failure(monkeypatch):
    """The per-provider in-flight caps must never leak a slot — a leaked slot
    would permanently shrink OpenAI concurrency for the whole process."""
    from src.agents import base as base_mod

    monkeypatch.setattr("time.sleep", lambda s: None)
    monkeypatch.setenv("QUANT_AGENT_MAX_RETRIES", "1")
    start = base_mod._OPENAI_LLM_SEMAPHORE._value
    with patch("openai.OpenAI") as oai_cls:
        oai_cls.return_value = _openai_stream_mock()
        ConcreteAgent(api_key="k", model="gpt-5.5", max_tokens=64).run(data="x")
    assert base_mod._OPENAI_LLM_SEMAPHORE._value == start
    with patch("openai.OpenAI") as oai_cls:
        oai_cls.return_value = _openai_stream_mock(pieces=('{"half":',), finish_reason=None, usage=None)
        with pytest.raises(Exception):
            ConcreteAgent(api_key="k", model="gpt-5.5", max_tokens=64).run(data="x")
    assert base_mod._OPENAI_LLM_SEMAPHORE._value == start


# === Stage 1 (QAMC provider/model/correlation plumbing) ===
# OpenRouter provider seam + AgentResult attribution fields.


def test_openrouter_routes_to_openai_sdk_with_base_url():
    """provider='openrouter' uses the OpenAI SDK pointed at OpenRouter's
    base_url, NOT Anthropic — same shape as the DeepSeek routing test above."""
    from src.agents.base import _OPENROUTER_BASE_URL

    with patch("openai.OpenAI") as oai_cls, patch("anthropic.Anthropic") as anth_cls:
        oai_cls.return_value = _openai_stream_mock()
        agent = ConcreteAgent(
            api_key="ork", model="anthropic/claude-3.5-sonnet", max_tokens=4096, provider="openrouter"
        )
        assert agent._use_openrouter is True
        assert agent._use_openai is False
        assert agent._use_deepseek is False
        anth_cls.assert_not_called()
        _, kwargs = oai_cls.call_args
        assert kwargs.get("base_url") == _OPENROUTER_BASE_URL == "https://openrouter.ai/api/v1"
        assert kwargs.get("api_key") == "ork"


# === Google AI Studio direct (2026-08-31 owner decision) ===


def test_google_routes_to_openai_sdk_with_base_url():
    """provider='google' — a bare 'gemini-*' id, no 'google/' vendor prefix —
    uses the OpenAI SDK pointed at Google's OpenAI-compatible compat
    endpoint, NOT Anthropic. Same shape as the OpenRouter/DeepSeek routing
    tests above: Google is reached through the shared OpenAI-wire path
    (_openai_wire_call), not a native Gemini client."""
    from src.agents.base import _GOOGLE_BASE_URL

    with patch("openai.OpenAI") as oai_cls, patch("anthropic.Anthropic") as anth_cls:
        oai_cls.return_value = _openai_stream_mock()
        agent = ConcreteAgent(api_key="gk", model="gemini-3.5-flash-lite", max_tokens=4096, provider="google")
        assert agent._use_google is True
        assert agent._use_openrouter is False
        assert agent._use_openai is False
        assert agent._use_deepseek is False
        anth_cls.assert_not_called()
        _, kwargs = oai_cls.call_args
        assert kwargs.get("base_url") == _GOOGLE_BASE_URL
        assert kwargs.get("base_url") == "https://generativelanguage.googleapis.com/v1beta/openai/"
        # Authorization: Bearer {value} — the OpenAI SDK sends this natively
        # from `api_key=`, no custom header work needed.
        assert kwargs.get("api_key") == "gk"


def test_bare_gemini_id_infers_google_provider_without_explicit_override():
    """A config that names a bare 'gemini-*' model with no explicit
    `provider` must route to Google, not silently fall through to the
    Anthropic default — the whole point of adding the prefix inference."""
    from src.agents.base import resolve_provider, _provider_for

    assert _provider_for("gemini-3.5-flash-lite") == "google"
    assert resolve_provider("gemini-3.5-flash-lite") == "google"
    assert resolve_provider("gemini-3.5-flash-lite", None) == "google"
    # An OpenRouter vendor/model id is unaffected — still explicit-only.
    assert resolve_provider("google/gemini-3.5-flash-lite") == "anthropic"
    assert resolve_provider("google/gemini-3.5-flash-lite", "openrouter") == "openrouter"


def test_google_client_disables_sdk_internal_retries():
    with patch("openai.OpenAI") as oai_cls:
        oai_cls.return_value = _openai_stream_mock()
        ConcreteAgent(api_key="gk", model="gemini-3.5-flash-lite", max_tokens=64, provider="google")
        assert oai_cls.call_args.kwargs.get("max_retries") == 0


def test_google_uses_dedicated_semaphore_not_openai_relay_or_openrouter():
    """Google AI Studio is its own account/rate-limit domain — a call there
    must not contend for (or be starved by) the OpenAI relay's or
    OpenRouter's concurrency slots."""
    from src.agents import base as base_mod

    openai_start = base_mod._OPENAI_LLM_SEMAPHORE._value
    openrouter_start = base_mod._OPENROUTER_LLM_SEMAPHORE._value
    with patch("openai.OpenAI") as oai_cls:
        oai_cls.return_value = _openai_stream_mock()
        ConcreteAgent(api_key="gk", model="gemini-3.5-flash-lite", max_tokens=64, provider="google").run(data="x")
    assert base_mod._OPENAI_LLM_SEMAPHORE._value == openai_start
    assert base_mod._OPENROUTER_LLM_SEMAPHORE._value == openrouter_start


def test_google_primary_run_succeeds_end_to_end():
    """Full run() through the streamed OpenAI-wire path for a Google primary:
    content assembled, usage extracted, and the uniform-testing settings
    (owner requirement, 2026-09-14) ARE sent — Google's own OpenAI-compat
    endpoint documents `reasoning_effort` as a top-level field (see
    test_google_sends_default_medium_reasoning_effort below) — and
    attribution reports 'google' with no failover."""
    with patch("openai.OpenAI") as oai_cls:
        oai_cls.return_value = _openai_stream_mock()
        agent = ConcreteAgent(api_key="gk", model="gemini-3.5-flash-lite", max_tokens=64, provider="google")
        result = agent.run(data="x")
    assert result.raw_text == '{"result": "ok"}'
    assert result.requested_provider == "google"
    assert result.actual_provider == "google"
    assert result.used_fallback is False
    _, kwargs = oai_cls.return_value.chat.completions.create.call_args
    assert kwargs["extra_body"]["reasoning_effort"] == "medium"


def test_explicit_provider_field_overrides_prefix_inference():
    """An OpenRouter 'vendor/model' id like 'anthropic/claude-3.5-sonnet'
    would NOT match any native prefix and would default to the Anthropic
    branch by elimination — the explicit provider field must win instead,
    which is the entire reason OpenRouter needs an explicit field at all."""
    with patch("openai.OpenAI") as oai_cls, patch("anthropic.Anthropic") as anth_cls:
        oai_cls.return_value = _openai_stream_mock()
        agent = ConcreteAgent(
            api_key="ork", model="anthropic/claude-3.5-sonnet", max_tokens=4096, provider="openrouter"
        )
        assert agent._provider == "openrouter"
        anth_cls.assert_not_called()
        oai_cls.assert_called_once()


def test_provider_unset_preserves_existing_prefix_routing_behavior():
    """Regression pin: provider=None (the default, and every pre-Stage-1
    config) must route BYTE-IDENTICAL to before Stage 1 existed — prefix
    inference alone, for all three pre-existing providers."""
    with patch("openai.OpenAI"), patch("anthropic.Anthropic"):
        assert ConcreteAgent(api_key="k", model="gpt-5.5", max_tokens=64)._provider == "openai"
        assert ConcreteAgent(api_key="k", model="deepseek-v4-flash", max_tokens=64)._provider == "deepseek"
        assert ConcreteAgent(api_key="k", model="claude-opus-4-7", max_tokens=64)._provider == "anthropic"
        # Explicitly passing provider=None must be identical to omitting it.
        assert ConcreteAgent(api_key="k", model="gpt-5.5", max_tokens=64, provider=None)._provider == "openai"


def test_invalid_explicit_provider_falls_back_to_prefix_inference():
    """resolve_provider() only honors a KNOWN provider name — a typo must
    not silently misroute (and must not crash construction either)."""
    from src.agents.base import resolve_provider

    assert resolve_provider("gpt-5.5", "not-a-real-provider") == "openai"
    assert resolve_provider("gpt-5.5", "") == "openai"
    assert resolve_provider("gpt-5.5", None) == "openai"
    assert resolve_provider("anthropic/claude-3.5-sonnet", "openrouter") == "openrouter"
    assert resolve_provider("anthropic/claude-3.5-sonnet", "OpenRouter") == "openrouter"  # case-insensitive


def test_openrouter_primary_fails_over_to_anthropic(monkeypatch):
    """OpenRouter primary exhausted → ONE Anthropic failover call succeeds;
    result carries the fallback model, and requested_provider stays
    'openrouter' (never silently reported as the fallback's provider)."""
    monkeypatch.setattr("time.sleep", lambda s: None)
    monkeypatch.setenv("QUANT_AGENT_MAX_RETRIES", "2")
    ork = MagicMock()
    ork.chat.completions.create.side_effect = ConnectionError("openrouter down")
    anth = MagicMock()
    anth.messages.create.return_value = _good_anthropic_response()
    with patch("openai.OpenAI", return_value=ork), patch("anthropic.Anthropic", return_value=anth):
        agent = ConcreteAgent(
            api_key="ork",
            model="anthropic/claude-3.5-sonnet",
            max_tokens=64,
            fallback_api_key="fk",
            provider="openrouter",
            fallback_provider="anthropic",
            fallback_model="claude-opus-4-7",
        )
        result = agent.run(data="x")
    assert result.raw_text == '{"result": "ok"}'
    assert result.model == "claude-opus-4-7"
    assert result.requested_model == "anthropic/claude-3.5-sonnet"
    assert result.requested_provider == "openrouter"
    assert result.actual_provider == "anthropic"
    assert result.used_fallback is True
    anth.messages.create.assert_called_once()


def test_openrouter_primary_no_failover_without_key(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda s: None)
    monkeypatch.setenv("QUANT_AGENT_MAX_RETRIES", "2")
    ork = MagicMock()
    ork.chat.completions.create.side_effect = ConnectionError("down")
    with patch("openai.OpenAI", return_value=ork), patch("anthropic.Anthropic") as A:
        agent = ConcreteAgent(
            api_key="ork", model="anthropic/claude-3.5-sonnet", max_tokens=64, provider="openrouter"
        )  # no fallback key
        with pytest.raises(ConnectionError):
            agent.run(data="x")
        A.assert_not_called()


def test_openrouter_client_disables_sdk_internal_retries():
    with patch("openai.OpenAI") as oai_cls:
        oai_cls.return_value = _openai_stream_mock()
        ConcreteAgent(api_key="ork", model="anthropic/claude-3.5-sonnet", max_tokens=64, provider="openrouter")
        assert oai_cls.call_args.kwargs.get("max_retries") == 0


def test_openrouter_uses_dedicated_semaphore_not_openai_relay():
    """OpenRouter is a distinct account/rate-limit domain from the OpenAI
    relay — it must not contend for the relay's concurrency slots."""
    from src.agents import base as base_mod

    openai_start = base_mod._OPENAI_LLM_SEMAPHORE._value
    with patch("openai.OpenAI") as oai_cls:
        oai_cls.return_value = _openai_stream_mock()
        ConcreteAgent(api_key="ork", model="anthropic/claude-3.5-sonnet", max_tokens=64, provider="openrouter").run(
            data="x"
        )
    # The OpenAI relay's semaphore must be untouched by an OpenRouter call.
    assert base_mod._OPENAI_LLM_SEMAPHORE._value == openai_start


# === OpenRouter post-hoc generation-cost recovery ===============================
# When a streamed OpenRouter call carries content + finish_reason but NO usage
# chunk, _call_openai used to settle it as 0/0-unknown, which the cost circuit
# then charges at the FULL pre-call reservation ($0.60-$1.20 against a $2.75
# daily cap). OpenRouter retains the true billed cost behind
# GET /generation?id=<chunk.id> — not immediately queryable (measured 404 then
# resolves minutes later), so recovery is a small bounded best-effort retry
# that must never raise and must never make a call worse than 0/0-unknown.


def _generation_response(total_cost=1.1e-06, tokens_prompt=8, tokens_completion=1):
    """A requests.Response-shaped mock for GET /generation?id=..., matching
    the live shape verified 2026-08-31 (see src/agents/base.py)."""
    resp = MagicMock()
    resp.status_code = 200
    resp.raise_for_status.return_value = None
    resp.json.return_value = {
        "data": {
            "total_cost": total_cost,
            "tokens_prompt": tokens_prompt,
            "tokens_completion": tokens_completion,
            "native_tokens_prompt": tokens_prompt,
            "native_tokens_completion": tokens_completion,
            "model": "anthropic/claude-3.5-sonnet",
            "streamed": True,
        }
    }
    return resp


def _404_response():
    resp = MagicMock()
    resp.status_code = 404
    return resp


def _requests_get_stub(generation_response=None, generation_exception=None):
    """requests.get replacement that answers ONLY the OpenRouter /generation
    URL; any other URL (notably cost_table's unrelated on-demand pricing-
    catalog fetch to .../v1/models, which run() can trigger while computing
    the ratio-check log against the estimate) gets the same 'network
    disabled' behavior conftest's autouse fixture normally provides. That
    keeps assertions about the generation lookup from being confused by an
    unrelated call sharing the same requests.get patch target."""
    from src.agents.base import _OPENROUTER_GENERATION_URL
    import requests as _requests

    def _get(url, *args, **kwargs):
        if url == _OPENROUTER_GENERATION_URL:
            if generation_exception is not None:
                raise generation_exception
            return generation_response
        raise _requests.ConnectionError("outbound HTTP disabled in this test (only /generation is stubbed)")

    return MagicMock(side_effect=_get)


def _generation_calls(mock_get):
    """The subset of a stubbed requests.get's calls that hit /generation —
    filters out any unrelated pricing-catalog call sharing the same mock."""
    from src.agents.base import _OPENROUTER_GENERATION_URL

    return [c for c in mock_get.call_args_list if c.args[:1] == (_OPENROUTER_GENERATION_URL,)]


def test_generation_id_captured_and_used_for_recovery_lookup(monkeypatch):
    """chunk.id (the last non-empty one seen) is what gets looked up, sent as
    the `id` query param with this client's own API key as the bearer."""
    monkeypatch.setattr("time.sleep", lambda s: None)
    mock_get = _requests_get_stub(generation_response=_generation_response())
    with patch("requests.get", mock_get), patch("openai.OpenAI") as oai_cls:
        client = _openai_stream_mock(usage=None, gen_id="gen-abc123")
        # patch("openai.OpenAI") replaces the constructor itself, so
        # self.client here is this bare MagicMock — it won't reflect the
        # constructor's api_key kwarg the way a real OpenAI() client does
        # (verified separately: real OpenAI(api_key=...).api_key round-
        # trips). Set it explicitly to exercise "read it off self.client".
        client.api_key = "ork"
        oai_cls.return_value = client
        agent = ConcreteAgent(api_key="ork", model="anthropic/claude-3.5-sonnet", max_tokens=64, provider="openrouter")
        agent.run(data="x")
    calls = _generation_calls(mock_get)
    assert len(calls) == 1
    assert calls[0].kwargs["params"] == {"id": "gen-abc123"}
    assert calls[0].kwargs["headers"] == {"Authorization": "Bearer ork"}


def test_recovered_openrouter_cost_reaches_settled_cost(monkeypatch):
    """A successful post-hoc lookup must settle the ACTUAL cost/tokens, not
    the 0/0-unknown fallback that would otherwise hold the full reservation."""
    monkeypatch.setattr("time.sleep", lambda s: None)
    mock_get = _requests_get_stub(
        generation_response=_generation_response(
            total_cost=1.1e-06,
            tokens_prompt=8,
            tokens_completion=1,
        )
    )
    with patch("requests.get", mock_get), patch("openai.OpenAI") as oai_cls:
        oai_cls.return_value = _openai_stream_mock(usage=None, gen_id="gen-xyz")
        agent = ConcreteAgent(api_key="ork", model="anthropic/claude-3.5-sonnet", max_tokens=64, provider="openrouter")
        result = agent.run(data="x")
    assert result.input_tokens == 8
    assert result.output_tokens == 1
    assert result.cost_usd == pytest.approx(1.1e-06)


def test_recovery_only_attempted_when_usage_absent():
    """A healthy stream WITH a usage chunk must never touch the network —
    recovery exists only for the no-usage case."""
    mock_get = _requests_get_stub(generation_response=_generation_response())
    with patch("requests.get", mock_get), patch("openai.OpenAI") as oai_cls:
        oai_cls.return_value = _openai_stream_mock(gen_id="gen-should-be-unused")
        agent = ConcreteAgent(api_key="ork", model="anthropic/claude-3.5-sonnet", max_tokens=64, provider="openrouter")
        result = agent.run(data="x")
    assert _generation_calls(mock_get) == []
    assert result.input_tokens == 100 and result.output_tokens == 50


def test_recovery_404_falls_back_to_unknown_cost_without_raising(monkeypatch):
    """The measured common case (querying right after the stream closes) —
    must degrade to exactly today's unknown-cost behavior, never raise, and
    stop after the bounded retry budget."""
    monkeypatch.setattr("time.sleep", lambda s: None)
    mock_get = _requests_get_stub(generation_response=_404_response())
    with patch("requests.get", mock_get), patch("openai.OpenAI") as oai_cls:
        oai_cls.return_value = _openai_stream_mock(usage=None, gen_id="gen-404")
        agent = ConcreteAgent(api_key="ork", model="anthropic/claude-3.5-sonnet", max_tokens=64, provider="openrouter")
        result = agent.run(data="x")
    assert result.input_tokens == 0 and result.output_tokens == 0
    assert result.cost_usd is None
    from src.agents.base import _OPENROUTER_GENERATION_LOOKUP_ATTEMPTS

    assert len(_generation_calls(mock_get)) == _OPENROUTER_GENERATION_LOOKUP_ATTEMPTS


def test_recovery_timeout_falls_back_to_unknown_cost_without_raising(monkeypatch):
    """A network timeout must degrade the same way as a 404 — never raise,
    never block the session beyond the bounded retry budget."""
    import requests

    monkeypatch.setattr("time.sleep", lambda s: None)
    mock_get = _requests_get_stub(generation_exception=requests.Timeout("generation lookup timed out"))
    with patch("requests.get", mock_get), patch("openai.OpenAI") as oai_cls:
        oai_cls.return_value = _openai_stream_mock(usage=None, gen_id="gen-timeout")
        agent = ConcreteAgent(api_key="ork", model="anthropic/claude-3.5-sonnet", max_tokens=64, provider="openrouter")
        result = agent.run(data="x")
    assert result.input_tokens == 0 and result.output_tokens == 0
    assert result.cost_usd is None
    from src.agents.base import _OPENROUTER_GENERATION_LOOKUP_ATTEMPTS

    assert len(_generation_calls(mock_get)) == _OPENROUTER_GENERATION_LOOKUP_ATTEMPTS


def test_non_openrouter_provider_skips_generation_lookup_entirely():
    """Only OpenRouter has this endpoint — a plain OpenAI call with a
    (hypothetically) captured id must still skip the lookup cleanly rather
    than calling a URL that cannot work for this provider."""
    mock_get = _requests_get_stub(generation_response=_generation_response())
    with patch("requests.get", mock_get), patch("openai.OpenAI") as oai_cls:
        oai_cls.return_value = _openai_stream_mock(usage=None, gen_id="gen-openai-plain")
        agent = ConcreteAgent(api_key="k", model="gpt-5.5", max_tokens=64)
        result = agent.run(data="x")
    assert _generation_calls(mock_get) == []
    assert result.input_tokens == 0 and result.output_tokens == 0
    assert result.cost_usd is None


def test_recovery_skipped_when_no_generation_id_was_ever_seen():
    """OpenRouter, no usage chunk, but the stream never carried an id either
    (older relay / stripped field) — nothing to look up, must skip rather
    than querying with id=None."""
    mock_get = _requests_get_stub(generation_response=_generation_response())
    with patch("requests.get", mock_get), patch("openai.OpenAI") as oai_cls:
        oai_cls.return_value = _openai_stream_mock(usage=None, gen_id=None)
        agent = ConcreteAgent(api_key="ork", model="anthropic/claude-3.5-sonnet", max_tokens=64, provider="openrouter")
        result = agent.run(data="x")
    assert _generation_calls(mock_get) == []
    assert result.cost_usd is None


def test_agent_result_new_fields_populated_on_primary_success(mock_anthropic):
    """requested_* mirrors actual_* on a clean primary success; used_fallback
    False; prompt_version deterministic; latency_s measured."""
    mock_anthropic.messages.create.return_value.stop_reason = "end_turn"
    agent = ConcreteAgent(api_key="k", model="claude-opus-4-7", max_tokens=64)
    result = agent.run(data="test")
    assert result.requested_model == "claude-opus-4-7"
    assert result.requested_provider == "anthropic"
    assert result.actual_provider == "anthropic"
    assert result.used_fallback is False
    assert result.latency_s >= 0.0
    import hashlib

    expected = hashlib.sha256(agent.system_prompt.encode("utf-8")).hexdigest()[:12]
    assert result.prompt_version == expected


def test_prompt_version_changes_with_prompt_content(mock_anthropic):
    """Two agents with different system_prompt text produce different
    prompt_version hashes; identical text produces identical hashes."""
    mock_anthropic.messages.create.return_value.stop_reason = "end_turn"

    class OtherPromptAgent(ConcreteAgent):
        @property
        def system_prompt(self) -> str:
            return "A completely different prompt."

    a = ConcreteAgent(api_key="k", model="claude-opus-4-7", max_tokens=64)
    b = ConcreteAgent(api_key="k", model="claude-opus-4-7", max_tokens=64)
    c = OtherPromptAgent(api_key="k", model="claude-opus-4-7", max_tokens=64)
    r_a = a.run(data="test")
    r_b = b.run(data="test")
    r_c = c.run(data="test")
    assert r_a.prompt_version == r_b.prompt_version
    assert r_a.prompt_version != r_c.prompt_version


def test_agent_log_kwargs_helper_maps_fallback_status():
    from src.agents.base import agent_log_kwargs, AgentResult

    success = AgentResult(raw_text="", tokens_used=0, model="m", used_fallback=False)
    fallback = AgentResult(raw_text="", tokens_used=0, model="m", used_fallback=True)
    assert agent_log_kwargs(success)["status"] == "success"
    assert agent_log_kwargs(fallback)["status"] == "fallback"


# ---------------------------------- every attempt's failure reaches the circuit


def test_the_failovers_own_error_reaches_the_cost_circuit(monkeypatch):
    """`run()` re-raises the PRIMARY error, so the failover's error used to be
    thrown away — and with it the proof that the failover billed nothing.

    On 2026-08-31 that meant a call refused 429/429 upstream and then rejected
    401 by a failover with no credential was charged $0.62 at the failover
    model's price, for work that cost exactly $0. The unexplained spend then
    latched the desk. Both errors must now reach the circuit.
    """
    monkeypatch.setattr("time.sleep", lambda s: None)
    monkeypatch.setenv("QUANT_AGENT_MAX_RETRIES", "2")

    class _Status(Exception):
        def __init__(self, status):
            super().__init__(f"status {status}")
            self.status_code = status

    seen = {}

    class _Circuit:
        db_path = ":memory:"

        def begin_call(self, **kw):
            return SimpleNamespace(
                reservation_id="r",
                run_id="run",
                mode="morning",
                agent_name="test_agent",
                model=kw.get("model"),
                input_tokens_estimate=10,
                max_output_tokens=10,
            )

        def before_provider_attempt(self, reservation, *, model):
            return 1

        def fail_call(self, reservation, error, attempt_errors=None):
            seen["error"] = error
            seen["attempts"] = list(attempt_errors or [])

        def complete_call(self, *a, **k):
            return None

    oai = MagicMock()
    oai.chat.completions.create.side_effect = _Status(429)
    anth = MagicMock()
    anth.messages.create.side_effect = _Status(401)

    with patch("openai.OpenAI", return_value=oai), patch("anthropic.Anthropic", return_value=anth):
        agent = ConcreteAgent(
            api_key="k",
            model="google/gemini-3.5-flash-lite",
            max_tokens=64,
            fallback_api_key="fk",
            provider="openrouter",
            fallback_provider="anthropic",
            fallback_model="claude-opus-4-7",
        )
        agent.set_cost_circuit(_Circuit())
        with pytest.raises(_Status):
            agent.run(data="x")

    statuses = [getattr(e, "status_code", None) for e in seen["attempts"]]
    assert 401 in statuses, "the failover's 401 must reach the circuit"
    from src.agents.base import capacity_max_attempts

    assert statuses.count(429) == capacity_max_attempts(), (
        "and EVERY primary refusal — the count is the deadline-derived capacity cap, not a hand-pinned 2"
    )


# ===========================================================================
# Mid-stream provider errors must keep their status code
#
# OpenRouter cannot change the HTTP status once the first byte is written, so
# a rate limit that lands mid-stream arrives as an SSE chunk carrying a
# top-level `error` object (code/message/metadata.error_type) plus
# finish_reason "error", on an HTTP 200.
#
# Before this, such a chunk fell through to the empty-content guard and raised
# LLMEmptyResponseError, which carries NO status_code. The cost circuit's
# zero-cost classifier keys on status_code, found none, failed closed, and
# charged the full pre-call reservation for a call the provider refused and
# never billed. On 2026-08-31 that consumed $1.92 of a $2.75 daily cap and
# shut the desk down. Adding 429 to the allow-list did not help, because the
# status never reached the classifier.
# ===========================================================================


def _mid_stream_error(code=429, error_type="rate_limit_exceeded", message="rate-limited upstream"):
    oai = MagicMock()
    oai.chat.completions.create.return_value = [
        _stream_chunk(piece='{"partial"'),
        _stream_chunk(
            error={"code": code, "message": message, "metadata": {"error_type": error_type}}, finish_reason="error"
        ),
    ]
    return oai


def test_mid_stream_error_surfaces_the_providers_status_code():
    from src.agents.base import LLMStreamErrorChunk

    with patch("openai.OpenAI") as oai_cls:
        oai_cls.return_value = _mid_stream_error()
        agent = ConcreteAgent(api_key="k", model="gpt-5.5", max_tokens=4096)
        with pytest.raises(LLMStreamErrorChunk) as exc:
            agent._call_openai("x")
    assert exc.value.status_code == 429
    assert exc.value.error_type == "rate_limit_exceeded"


def test_mid_stream_rate_limit_is_classified_provably_free():
    """The whole point: a 429 the provider refused before billing must reach
    the cost circuit AS a 429, so it settles at $0 instead of the full
    reservation."""
    from src.agents.base import LLMStreamErrorChunk
    from src.cost_circuit import _is_known_zero_cost_failure

    assert (
        _is_known_zero_cost_failure(
            LLMStreamErrorChunk("rate limited", status_code=429, error_type="rate_limit_exceeded")
        )
        is True
    )


def test_mid_stream_error_without_a_code_stays_ambiguous():
    """Fail-closed is preserved. This change does not widen what counts as
    free — it only stops discarding a status the provider did send."""
    from src.agents.base import LLMStreamErrorChunk
    from src.cost_circuit import _is_known_zero_cost_failure

    assert _is_known_zero_cost_failure(LLMStreamErrorChunk("something odd", status_code=None)) is False


def test_mid_stream_429_retries_but_400_fast_fails():
    """LLMStreamErrorChunk is deliberately NOT in _RETRYABLE_EXC_NAMES, which
    is checked before status_code — so it is judged on the provider's own
    code rather than blanket-retried."""
    from src.agents.base import LLMStreamErrorChunk, _is_retryable

    assert _is_retryable(LLMStreamErrorChunk("rl", status_code=429)) is True
    assert _is_retryable(LLMStreamErrorChunk("bad", status_code=400)) is False
    assert _is_retryable(LLMStreamErrorChunk("upstream", status_code=502)) is True


def test_healthy_stream_is_unaffected_by_the_error_check():
    """A normal chunk has no error field; the check must not fire on it."""
    with patch("openai.OpenAI") as oai_cls:
        oai_cls.return_value = _openai_stream_mock()
        agent = ConcreteAgent(api_key="k", model="gpt-5.5", max_tokens=4096)
        result = agent.run(data="x")
    assert result.raw_text == '{"result": "ok"}'


# === Uniform reasoning + structured output (owner requirement, 2026-09-14) ===
# Every OpenRouter-routed seat must send the SAME explicit reasoning effort,
# and (when it has a known result schema) the SAME response_format, so every
# candidate model is measured/traded under identical settings instead of its
# own undeclared default. See src/agents/base.py's _openai_wire_call and
# _response_format_for.

from pydantic import BaseModel as _PydanticBaseModel


class _FakeResult(_PydanticBaseModel):
    symbol: str
    note: str | None = None


class ConcreteAgentWithSchema(ConcreteAgent):
    result_model = _FakeResult


def test_openrouter_sends_default_medium_reasoning_effort(monkeypatch):
    monkeypatch.setattr("src.agents.base._openrouter_supported_parameters", lambda m: frozenset({"reasoning"}))
    with patch("openai.OpenAI") as oai_cls:
        oai_cls.return_value = _openai_stream_mock()
        ConcreteAgent(api_key="ork", model="anthropic/claude-3.5-sonnet", max_tokens=64, provider="openrouter").run(
            data="x"
        )
    _, kwargs = oai_cls.return_value.chat.completions.create.call_args
    assert kwargs["extra_body"]["reasoning"] == {"effort": "medium"}


def test_openrouter_sends_configured_reasoning_effort(monkeypatch):
    monkeypatch.setattr("src.agents.base._openrouter_supported_parameters", lambda m: frozenset({"reasoning"}))
    with patch("openai.OpenAI") as oai_cls:
        oai_cls.return_value = _openai_stream_mock()
        ConcreteAgent(
            api_key="ork",
            model="anthropic/claude-3.5-sonnet",
            max_tokens=64,
            provider="openrouter",
            reasoning_effort="high",
        ).run(data="x")
    _, kwargs = oai_cls.return_value.chat.completions.create.call_args
    assert kwargs["extra_body"]["reasoning"] == {"effort": "high"}


def test_openrouter_omits_response_format_when_agent_has_no_result_model():
    """ConcreteAgent declares no result_model (BaseAgent's default None) —
    the request must not carry response_format at all."""
    with patch("openai.OpenAI") as oai_cls:
        oai_cls.return_value = _openai_stream_mock()
        ConcreteAgent(api_key="ork", model="anthropic/claude-3.5-sonnet", max_tokens=64, provider="openrouter").run(
            data="x"
        )
    _, kwargs = oai_cls.return_value.chat.completions.create.call_args
    assert "response_format" not in kwargs["extra_body"]


def test_openrouter_sends_strict_json_schema_when_result_model_set():
    with patch("openai.OpenAI") as oai_cls:
        oai_cls.return_value = _openai_stream_mock()
        ConcreteAgentWithSchema(
            api_key="ork",
            model="anthropic/claude-3.5-sonnet",
            max_tokens=64,
            provider="openrouter",
        ).run(data="x")
    _, kwargs = oai_cls.return_value.chat.completions.create.call_args
    fmt = kwargs["extra_body"]["response_format"]
    assert fmt["type"] == "json_schema"
    assert fmt["json_schema"]["name"] == "_FakeResult"
    assert fmt["json_schema"]["strict"] is True
    schema = fmt["json_schema"]["schema"]
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == {"symbol", "note"}


def test_openrouter_structured_output_false_omits_response_format():
    with patch("openai.OpenAI") as oai_cls:
        oai_cls.return_value = _openai_stream_mock()
        ConcreteAgentWithSchema(
            api_key="ork",
            model="anthropic/claude-3.5-sonnet",
            max_tokens=64,
            provider="openrouter",
            structured_output=False,
        ).run(data="x")
    _, kwargs = oai_cls.return_value.chat.completions.create.call_args
    assert "response_format" not in kwargs["extra_body"]


def test_google_sends_default_medium_reasoning_effort():
    """Google AI Studio direct (Google's own OpenAI-compatibility endpoint,
    NOT OpenRouter) must get the SAME llm.reasoning_effort setting as the
    OpenRouter path — the owner requirement is identical settings on the
    route the live desk actually uses. Google's endpoint documents
    `reasoning_effort` as a flat top-level field (see
    https://ai.google.dev/gemini-api/docs/openai, fetched 2026-09-14),
    unlike OpenRouter's nested `reasoning: {effort: ...}`."""
    with patch("openai.OpenAI") as oai_cls:
        oai_cls.return_value = _openai_stream_mock()
        ConcreteAgent(api_key="gk", model="gemini-3.5-flash-lite", max_tokens=64, provider="google").run(data="x")
    _, kwargs = oai_cls.return_value.chat.completions.create.call_args
    assert kwargs["extra_body"]["reasoning_effort"] == "medium"
    assert "reasoning" not in kwargs["extra_body"]  # that key is OpenRouter's shape


def test_google_sends_configured_reasoning_effort():
    with patch("openai.OpenAI") as oai_cls:
        oai_cls.return_value = _openai_stream_mock()
        ConcreteAgent(
            api_key="gk", model="gemini-3.5-flash-lite", max_tokens=64, provider="google", reasoning_effort="high"
        ).run(data="x")
    _, kwargs = oai_cls.return_value.chat.completions.create.call_args
    assert kwargs["extra_body"]["reasoning_effort"] == "high"


def test_google_undocumented_reasoning_effort_left_unset_and_logged(caplog):
    """No documented Google equivalent must never be invented — leave
    thinking unset for that call and log a clear warning instead."""
    with patch("openai.OpenAI") as oai_cls:
        oai_cls.return_value = _openai_stream_mock()
        with caplog.at_level("WARNING"):
            ConcreteAgent(
                api_key="gk",
                model="gemini-3.5-flash-lite",
                max_tokens=64,
                provider="google",
                reasoning_effort="ultrathink",
            ).run(data="x")
    _, kwargs = oai_cls.return_value.chat.completions.create.call_args
    assert "reasoning_effort" not in kwargs.get("extra_body", {})
    assert any("no documented Google" in r.message for r in caplog.records)


def test_google_sends_strict_json_schema_when_result_model_set():
    """Same _response_format_for helper, same schema, as the OpenRouter
    path — Google's compat endpoint is OpenAI-wire-compatible for
    response_format too."""
    with patch("openai.OpenAI") as oai_cls:
        oai_cls.return_value = _openai_stream_mock()
        ConcreteAgentWithSchema(api_key="gk", model="gemini-3.5-flash-lite", max_tokens=64, provider="google").run(
            data="x"
        )
    _, kwargs = oai_cls.return_value.chat.completions.create.call_args
    fmt = kwargs["extra_body"]["response_format"]
    assert fmt["type"] == "json_schema"
    assert fmt["json_schema"]["name"] == "_FakeResult"


def test_google_structured_output_false_omits_response_format():
    with patch("openai.OpenAI") as oai_cls:
        oai_cls.return_value = _openai_stream_mock()
        ConcreteAgentWithSchema(
            api_key="gk",
            model="gemini-3.5-flash-lite",
            max_tokens=64,
            provider="google",
            structured_output=False,
        ).run(data="x")
    _, kwargs = oai_cls.return_value.chat.completions.create.call_args
    assert "response_format" not in kwargs["extra_body"]


def test_strict_schema_fallback_logs_once_and_still_sends_response_format(caplog):
    """A result model whose schema cannot be made strict-compatible must
    fall back to strict:false rather than silently dropping response_format
    entirely — and the fallback is logged only ONCE per model class."""
    from src.agents import base as base_mod

    base_mod._RESPONSE_FORMAT_CACHE.pop("_FakeResult", None)
    base_mod._STRICT_SCHEMA_FALLBACK_LOGGED.discard("_FakeResult")
    with patch.object(base_mod, "_strictify_schema", side_effect=RuntimeError("boom")):
        with caplog.at_level("WARNING"):
            fmt1 = base_mod._response_format_for(_FakeResult)
    assert fmt1["json_schema"]["strict"] is False
    assert fmt1["json_schema"]["schema"] == _FakeResult.model_json_schema()
    assert sum(1 for r in caplog.records if "_FakeResult" in r.message) == 1

    # Cached on the first call — a second lookup neither recomputes nor
    # logs again.
    caplog.clear()
    fmt2 = base_mod._response_format_for(_FakeResult)
    assert fmt2 == fmt1
    assert not caplog.records


# --- 402 shrink-retry (insufficient credit names a servable allowance) -------
#
# Message text is the MEASURED production wording, 2026-09-30.


class _Credit402(Exception):
    status_code = 402


_REAL_402_MESSAGE = (
    "Error code: 402 - {'error': {'message': 'This request requires more "
    "credits, or fewer max_tokens. You requested up to 16000 tokens, but "
    "can only afford 775. To increase, visit "
    "https://openrouter.ai/settings/credits and upgrade your balance', "
    "'code': 402}}"
)


def test_affordable_max_tokens_reads_the_providers_own_figure():
    from src.agents.base import _affordable_max_tokens

    assert _affordable_max_tokens(_Credit402(_REAL_402_MESSAGE)) == 775


def test_affordable_max_tokens_never_guesses_when_no_figure_is_named():
    from src.agents.base import _affordable_max_tokens

    exc = _Credit402("Error code: 402 - insufficient credit")
    assert _affordable_max_tokens(exc) is None


def test_affordable_max_tokens_ignores_non_credit_errors():
    from src.agents.base import _affordable_max_tokens

    class _RateLimited(Exception):
        status_code = 429

    assert _affordable_max_tokens(_RateLimited("you can only afford 775")) is None


def test_402_stays_non_retryable_so_the_shrink_retry_is_the_only_re_ask():
    from src.agents.base import _is_retryable

    assert _is_retryable(_Credit402(_REAL_402_MESSAGE)) is False


def test_truncated_finish_reason_is_still_unusable():
    """A shrunken allowance may cut the answer off; that answer must still be
    discarded, never salvaged into a decision."""
    from src.agents.base import _TRUNCATION_FINISH_REASONS

    assert "max_tokens" in _TRUNCATION_FINISH_REASONS
    assert "length" in _TRUNCATION_FINISH_REASONS
