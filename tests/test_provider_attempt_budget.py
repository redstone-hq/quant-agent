"""The circuit's per-call attempt ceiling must cover the retry loop's worst case.

THE INCIDENT THIS FILE EXISTS FOR (2026-08-31)
----------------------------------------------
`BaseAgent.run()` takes up to `_max_retries()` attempts at an agent's primary
model and then, if the primary is not itself Anthropic and a fallback key is
configured, ONE cross-provider failover. Worst case: three provider attempts.

`llm_cost_circuit.max_provider_attempts_per_call` was pinned by hand at two.

So every failover attempt was attempt three against a ceiling of two, and the
circuit stopped it. Not sometimes — always, and only in the case failover
exists to handle: a retryable primary failure (429/5xx/timeout) burns both
permitted attempts before failover is even reached. Cross-provider failover
could never once complete. On Monday 2026-08-31 an upstream rate-limit on the
primary triggered it at 09:32 ET, two minutes after the open, and because
`provider_attempt_limit` was also the one trip code wired to the durable
operator-reset latch, paid analysis stayed off for the rest of the day over
$0.05 of a $2.75 budget.

The two numbers lived in different worlds — an env-overridable module
constant in `src/agents/base.py`, a YAML setting read by `src/cost_circuit.py`
— which is how they drifted apart for six days across five separate circuit
trips without anyone noticing. These tests bind them together so they cannot
drift again silently.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.trading_calendar import sql_utc_stamp
import yaml

from src.agents.base import (
    _max_retries,
    capacity_max_attempts,
    provider_attempt_budget,
)
from src.cost_circuit import (
    LLMCostCircuitBreaker,
    PaidAnalysisSuspended,
    _trigger_scope,
)
from src.storage.db import Database

SETTINGS = Path(__file__).resolve().parents[1] / "config" / "settings.yaml"


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
        "daily_cost_limit_usd": 20.0,
        # Item 14 (2026-09-02): the runaway-loop backstop is now a plain
        # call count (docs/WORK.md item 14c), not a reservation-era
        # free-failure-session count. High by default so tests not
        # exercising the cap itself never hit it.
        "max_calls_per_session": 1000,
        "max_provider_attempts_per_call": provider_attempt_budget(failover_available=True),
        "input_chars_per_token": 3.5,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _db_path(tmp_path):
    path = tmp_path / "circuit.db"
    db = Database(str(path))
    db.initialize()
    db.conn.close()
    return str(path)


# --------------------------------------------------------------- arithmetic


def test_budget_covers_primary_attempts_plus_one_failover():
    """The property is "the ceiling covers the loop's worst case", not any
    particular count. The worst case is `capacity_max_attempts()` — a
    capacity refusal (429/5xx) is bounded by the wall-clock retry deadline,
    not by `_max_retries()`, which still bounds every other failure."""
    assert provider_attempt_budget(failover_available=True) == capacity_max_attempts() + 1
    assert capacity_max_attempts() >= _max_retries()


def test_budget_drops_the_failover_attempt_when_no_failover_is_possible():
    """An Anthropic-primary agent never fails over (Claude to Claude is
    pointless), so it cannot spend the extra attempt."""
    assert provider_attempt_budget(failover_available=False) == capacity_max_attempts()


@pytest.mark.parametrize("retries", [1, 2, 3, 7])
def test_budget_tracks_the_env_override_the_retry_loop_actually_reads(monkeypatch, retries):
    """`.env` on the box carries a commented-out QUANT_AGENT_MAX_RETRIES=7.
    Uncommenting it must move the circuit's ceiling too, not silently
    recreate the 2026-08-31 mismatch three attempts wider. The ceiling
    tracks whichever of the two primary bounds is the wider one."""
    monkeypatch.setenv("QUANT_AGENT_MAX_RETRIES", str(retries))
    assert provider_attempt_budget(failover_available=True) == max(retries, capacity_max_attempts()) + 1
    assert provider_attempt_budget(failover_available=True) >= retries + 1


def test_a_short_deadline_shrinks_the_capacity_attempt_cap(monkeypatch):
    """`capacity_max_attempts()` is DERIVED from the published deadline and
    the published backoff schedule, so shrinking the deadline must shrink
    it — proof nothing new was hand-picked here."""
    monkeypatch.setenv("QUANT_AGENT_MAX_RETRIES", "1")
    monkeypatch.setenv("QUANT_AGENT_RETRY_DEADLINE_S", "10000")
    wide = capacity_max_attempts()
    monkeypatch.setenv("QUANT_AGENT_RETRY_DEADLINE_S", "3")
    assert capacity_max_attempts() < wide


def test_waiting_longer_only_applies_to_a_provably_unbilled_refusal():
    """The cost question. The wider cap is granted ONLY where the provider
    refused because it was busy (429/5xx) — attempts that generate no tokens
    and so cannot multiply spend. A transport blip or a stream cut, which may
    well have been charged, keeps the original `_max_retries()` bound."""
    from src.agents.base import is_capacity_refusal

    assert is_capacity_refusal(_Rate429("busy"))
    assert is_capacity_refusal(_StatusErr(503, "high demand"))
    assert not is_capacity_refusal(ConnectionError("stream cut"))
    assert not is_capacity_refusal(_StatusErr(400, "bad request"))


# ------------------------------------------------------------ configuration


def _load_settings_with(ceiling):
    from src.config import AppConfig, _walk_and_substitute

    raw = yaml.safe_load(SETTINGS.read_text())
    if ceiling is None:
        raw["llm_cost_circuit"].pop("max_provider_attempts_per_call", None)
    else:
        raw["llm_cost_circuit"]["max_provider_attempts_per_call"] = ceiling
    return AppConfig(**_walk_and_substitute(raw))


@pytest.fixture
def _keys(monkeypatch):
    for var in (
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
        "DEEPSEEK_API_KEY",
        "OPENROUTER_API_KEY",
        "GOOGLE_API_KEY",
        "ALPACA_API_KEY",
        "ALPACA_SECRET_KEY",
        "FRED_API_KEY",
        "TELEGRAM_BOT_TOKEN",
        "TELEGRAM_CHAT_ID",
    ):
        monkeypatch.setenv(var, "test-key")


def test_shipped_settings_ceiling_covers_the_retry_loop(_keys):
    """The regression guard proper: whatever config/settings.yaml says today,
    it must not be below what one call can spend."""
    config = _load_settings_with(None)
    assert config.llm_cost_circuit.max_provider_attempts_per_call >= provider_attempt_budget(failover_available=True)


def test_settings_does_not_re_pin_the_ceiling(_keys):
    """Pinning it in YAML is what broke; the value belongs to the code that
    spends the attempts."""
    raw = yaml.safe_load(SETTINGS.read_text())
    assert "max_provider_attempts_per_call" not in raw["llm_cost_circuit"]


def test_config_refuses_a_ceiling_below_the_retry_loops_worst_case(_keys):
    """The exact broken configuration must now fail at load rather than at
    09:32 on a Monday."""
    with pytest.raises(ValueError) as excinfo:
        _load_settings_with(provider_attempt_budget(failover_available=True) - 1)
    message = str(excinfo.value)
    assert "max_provider_attempts_per_call" in message
    assert "cross-provider failover" in message


def test_config_allows_a_ceiling_above_the_worst_case(_keys):
    """Raising it is a legitimate operator choice and grants no extra
    attempts — the retry loop, not this ceiling, decides how many requests
    happen."""
    above = provider_attempt_budget(failover_available=True) + 3
    config = _load_settings_with(above)
    assert config.llm_cost_circuit.max_provider_attempts_per_call == above


def test_config_failover_available_agrees_with_every_real_agents_own_gate(_keys):
    """The two computations of "is failover reachable" — AppConfig's
    (`_fallback_reachable_for_any_agent`/`_fallback_key_for_provider`, which
    `_check_provider_attempt_budget` derives its required ceiling from) and
    `BaseAgent`'s own runtime gate (`_failover_reachable`, computed
    independently per agent instance from the SAME settings) — must never
    disagree. Two independent computations of the identical fact drifting
    apart, unnoticed, is exactly the shape of the 2026-08-31 incident (see
    `provider_attempt_budget`'s docstring): this constructs a REAL BaseAgent
    for every one of the ten production seats from the shipped
    config/settings.yaml (no mocking — SDK client construction makes no
    network call) and cross-checks each one's own verdict against config's.
    """
    from tests.test_base_agent import ConcreteAgent

    config = _load_settings_with(None)
    config_says_reachable = config._fallback_reachable_for_any_agent()

    key_for = {
        "openai": config.api_keys.openai,
        "deepseek": config.api_keys.deepseek,
        "openrouter": config.api_keys.openrouter,
        "google": config.api_keys.google,
    }
    agent_names = (
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
    )
    any_agent_reachable = False
    for name in agent_names:
        model = getattr(config.llm, f"{name}_model")
        provider = config.llm.get_provider(name)
        from src.agents.base import resolve_provider

        resolved = resolve_provider(model, provider)
        agent = ConcreteAgent(
            api_key=key_for.get(resolved, config.api_keys.anthropic) or "placeholder",
            model=model,
            max_tokens=64,
            provider=provider,
            fallback_api_key=key_for.get(config.llm.fallback_provider, config.api_keys.anthropic),
            fallback_provider=config.llm.fallback_provider,
            fallback_model=config.llm.fallback_model,
        )
        any_agent_reachable = any_agent_reachable or agent._failover_reachable

    assert any_agent_reachable == config_says_reachable, (
        "AppConfig and BaseAgent disagree on whether failover is reachable "
        "for the shipped settings.yaml — exactly the drift the 2026-08-31 "
        "incident was caused by"
    )


# ----------------------------------------------------------------- the trip


def _reserve(circuit, *, agent="tech_analyst", model="google/gemini-3.5-flash-lite"):
    return circuit.begin_call(
        agent_name=agent,
        model=model,
        system_prompt="system",
        user_message="input",
        max_output_tokens=1_000,
    )


def test_failover_attempt_is_permitted_under_the_derived_ceiling(tmp_path):
    """The 2026-08-31 sequence, end to end at the circuit boundary: two
    primary attempts are rate-limited, and the third — the failover — must be
    ALLOWED THROUGH rather than trip the circuit."""
    circuit = LLMCostCircuitBreaker(_db_path(tmp_path), _config(), _Notifier())
    circuit.activate_session("run-failover", "morning")
    reservation = _reserve(circuit)

    circuit.before_provider_attempt(reservation, model="google/gemini-3.5-flash-lite")
    circuit.before_provider_attempt(reservation, model="google/gemini-3.5-flash-lite")
    # The failover: a different provider and a dearer model, priced as such.
    circuit.before_provider_attempt(reservation, model="claude-opus-4-7")

    assert circuit.status()["suspended"] in (0, False)


def test_ceiling_still_stops_a_genuine_attempt_runaway(tmp_path):
    """Raising the ceiling to cover failover must not remove the stop. One
    attempt past the budget is still stopped — it is only no longer the
    loop's own designed behaviour that trips it."""
    circuit = LLMCostCircuitBreaker(_db_path(tmp_path), _config(), _Notifier())
    circuit.activate_session("run-runaway", "morning")
    reservation = _reserve(circuit)

    budget = provider_attempt_budget(failover_available=True)
    for _ in range(budget):
        circuit.before_provider_attempt(reservation, model="claude-opus-4-7")

    with pytest.raises(PaidAnalysisSuspended):
        circuit.before_provider_attempt(reservation, model="claude-opus-4-7")


def test_attempt_limit_holds_the_session_instead_of_latching_the_desk(tmp_path):
    """Defect 5: `provider_attempt_limit` was the only trip code of its family
    left on the durable operator-reset latch, so a transient upstream fault
    cost a whole trading day and needed a human to clear. It is now scoped to
    the session that caused it -- later sessions stay eligible. (Its wider
    sibling `session_retry_attempt_limit`, part of the reservation-era
    retry-budget machinery, was deleted outright by item 14, 2026-09-02 --
    see `test_session_count_cap_triggers_on_the_next_call` in
    tests/test_cost_circuit.py for its call-count-based replacement.)"""
    assert _trigger_scope("provider_attempt_limit") == "session"

    notifier = _Notifier()
    circuit = LLMCostCircuitBreaker(_db_path(tmp_path), _config(), notifier)
    circuit.activate_session("run-held", "morning")
    reservation = _reserve(circuit)
    for _ in range(provider_attempt_budget(failover_available=True)):
        circuit.before_provider_attempt(reservation, model="claude-opus-4-7")
    with pytest.raises(PaidAnalysisSuspended):
        circuit.before_provider_attempt(reservation, model="claude-opus-4-7")

    # `status()` reports the EFFECTIVE state for the calling session, which a
    # session hold does suspend — that is the point of the hold. What must not
    # be set is the durable singleton latch: the row that outlives the session,
    # darkens every later one, and requires a human to clear.
    with sqlite3.connect(circuit.db_path, uri=True) as conn:
        latched = conn.execute("SELECT suspended FROM llm_circuit_state WHERE singleton=1").fetchone()[0]
    assert not latched

    alert = "\n".join(notifier.messages)
    assert "QUOTA HOLD" in alert
    assert "no operator reset is required" in alert
    assert "scope: run run-held only" in alert
    assert "later independent sessions remain eligible" in alert


def test_a_later_session_still_runs_after_an_attempt_limit_hold(tmp_path):
    """The operative consequence: the next scheduled session is not collateral
    damage. This is the difference between a degraded morning and a dark
    trading day."""
    path = _db_path(tmp_path)
    circuit = LLMCostCircuitBreaker(path, _config(), _Notifier())
    circuit.activate_session("run-held", "morning")
    reservation = _reserve(circuit)
    for _ in range(provider_attempt_budget(failover_available=True)):
        circuit.before_provider_attempt(reservation, model="claude-opus-4-7")
    with pytest.raises(PaidAnalysisSuspended):
        circuit.before_provider_attempt(reservation, model="claude-opus-4-7")

    later = LLMCostCircuitBreaker(path, _config(), _Notifier())
    later.activate_session("run-next", "intra_check")
    assert _reserve(later) is not None


# ------------------------------------- failover WITH the circuit attached

# WHY THIS SECTION IS THE POINT OF THE FILE
#
# `tests/test_base_agent.py` already covers cross-provider failover, and every
# one of those tests passed throughout the six days this defect was live. They
# could not have caught it: `tests/conftest.py` sets
# `BaseAgent._allow_unmetered_for_tests = True` for the whole suite, so those
# agents run with NO cost circuit attached at all. And `test_cost_circuit.py`
# exercises the circuit with no failover.
#
# So the suite proved failover works without the circuit, and the circuit
# works without failover, and nothing anywhere ran the two together — which is
# precisely where they contradicted each other. These tests attach a real
# breaker to a real agent and make the primary fail for real reasons.


class _Rate429(Exception):
    """Shaped like a provider rate-limit: `_is_retryable` reads `status_code`."""

    status_code = 429


class _StatusErr(Exception):
    """Any other provider error carrying an HTTP status."""

    def __init__(self, status_code: int, message: str = "provider error"):
        super().__init__(message)
        self.status_code = status_code


class _CircuitAgent:
    """Built in the test rather than imported so the agent under test is
    unambiguously running the production `run()` with a live breaker."""


def _agent_with_circuit(circuit, **kwargs):
    from tests.test_base_agent import ConcreteAgent

    # fallback_provider/fallback_model pinned to Anthropic by default so this
    # helper's existing callers keep exercising the Anthropic-SDK failover
    # path with a separately-mocked client, independent of whatever the
    # process-wide DEFAULT fallback (openrouter + gemini-3.5) is — see
    # test_google_primary_fails_over_to_openrouter_with_a_live_circuit below
    # for a test of that default explicitly.
    kwargs.setdefault("fallback_provider", "anthropic")
    kwargs.setdefault("fallback_model", "claude-opus-4-7")
    agent = ConcreteAgent(
        api_key="k",
        model="google/gemini-3.5-flash-lite",
        max_tokens=64,
        fallback_api_key="fk",
        provider="openrouter",
        **kwargs,
    )
    agent.set_cost_circuit(circuit)
    return agent


def test_rate_limited_primary_fails_over_with_a_live_circuit(tmp_path, monkeypatch):
    """The whole incident, reproduced against the real breaker.

    Two rate-limited primary attempts, then the failover — which under the old
    ceiling of 2 was attempt 3 and tripped the circuit instead of rescuing the
    session. It must now succeed, and the run must come back priced at the
    model that actually answered.
    """
    from unittest.mock import MagicMock, patch

    from tests.test_base_agent import _good_anthropic_response

    monkeypatch.setattr("time.sleep", lambda s: None)
    monkeypatch.setenv("QUANT_AGENT_MAX_RETRIES", "2")
    monkeypatch.setattr("src.agents.base.BaseAgent._allow_unmetered_for_tests", False)

    circuit = LLMCostCircuitBreaker(_db_path(tmp_path), _config(), _Notifier())
    circuit.activate_session("run-live-failover", "morning")

    openrouter = MagicMock()
    openrouter.chat.completions.create.side_effect = _Rate429("rate-limited upstream")
    anthropic_client = MagicMock()
    anthropic_client.messages.create.return_value = _good_anthropic_response()

    with patch("openai.OpenAI", return_value=openrouter), patch("anthropic.Anthropic", return_value=anthropic_client):
        agent = _agent_with_circuit(circuit)
        result = agent.run(data="x")

    assert result.raw_text == '{"result": "ok"}'
    assert result.model == "claude-opus-4-7"
    # Primary exhausted — by the deadline-derived capacity cap, not by 2.
    assert openrouter.chat.completions.create.call_count == capacity_max_attempts()
    anthropic_client.messages.create.assert_called_once()  # single-shot failover

    with sqlite3.connect(circuit.db_path, uri=True) as conn:
        latched = conn.execute("SELECT suspended FROM llm_circuit_state WHERE singleton=1").fetchone()[0]
    assert not latched, "a transient rate-limit must not latch the desk"


def test_a_failover_that_also_fails_does_not_latch_the_desk(tmp_path, monkeypatch):
    """The worse case: both providers are down. The session cannot produce
    analysis and must not pretend otherwise — but tomorrow's sessions, and the
    next half-hour's, are not the outage's to take."""
    from unittest.mock import MagicMock, patch

    monkeypatch.setattr("time.sleep", lambda s: None)
    monkeypatch.setenv("QUANT_AGENT_MAX_RETRIES", "2")
    monkeypatch.setattr("src.agents.base.BaseAgent._allow_unmetered_for_tests", False)

    circuit = LLMCostCircuitBreaker(_db_path(tmp_path), _config(), _Notifier())
    circuit.activate_session("run-both-down", "morning")

    openrouter = MagicMock()
    openrouter.chat.completions.create.side_effect = _Rate429("primary down")
    anthropic_client = MagicMock()
    anthropic_client.messages.create.side_effect = _Rate429("fallback down too")

    with patch("openai.OpenAI", return_value=openrouter), patch("anthropic.Anthropic", return_value=anthropic_client):
        agent = _agent_with_circuit(circuit)
        with pytest.raises(Exception):
            agent.run(data="x")

    with sqlite3.connect(circuit.db_path, uri=True) as conn:
        latched = conn.execute("SELECT suspended FROM llm_circuit_state WHERE singleton=1").fetchone()[0]
    assert not latched


def test_google_primary_fails_over_to_openrouter_with_a_live_circuit(tmp_path, monkeypatch):
    """The REAL production shape (2026-08-31 owner decision): eight seats run
    Google AI Studio direct as PRIMARY, OpenRouter serving the SAME model as
    BACKUP — same road difference this whole file exists to test, now with
    the process-wide DEFAULT fallback (no explicit override) rather than the
    Anthropic path pinned by `_agent_with_circuit` above.

    Also proves governor attribution end to end: the two rate-limited
    primary attempts must be paced against the GOOGLE token governor and the
    single failover attempt against the OPENROUTER governor — charging a
    failover to the wrong governor would let a failover storm slip past the
    ceiling meant to bound it (see `_governor_domain_for` in
    src/agents/base.py). Exercised WITH a live cost circuit attached, not the
    stub/no-circuit shape most of tests/test_base_agent.py's failover
    coverage uses — the exact seam left untested that let the 2026-08-31
    incident through.
    """
    from unittest.mock import MagicMock, patch

    from src.agents import base as base_mod
    from tests.test_base_agent import ConcreteAgent, _openai_stream_mock

    monkeypatch.setattr("time.sleep", lambda s: None)
    monkeypatch.setenv("QUANT_AGENT_MAX_RETRIES", "2")
    monkeypatch.setattr("src.agents.base.BaseAgent._allow_unmetered_for_tests", False)

    circuit = LLMCostCircuitBreaker(_db_path(tmp_path), _config(), _Notifier())
    circuit.activate_session("run-google-failover", "morning")

    # Primary and fallback both speak the OpenAI-wire shape, so both attempts
    # go through the SAME mocked client (patch("openai.OpenAI") always
    # returns this one object regardless of which base_url the real code
    # would have used) — a side_effect LIST lets the first two calls fail and
    # the third (the failover) succeed.
    success_chunks = _openai_stream_mock().chat.completions.create.return_value
    client = MagicMock()
    client.chat.completions.create.side_effect = [
        _Rate429("google free-tier rate-limited") for _ in range(capacity_max_attempts())
    ] + [success_chunks]

    google_gov = base_mod._TOKEN_GOVERNORS["google"]
    openrouter_gov = base_mod._TOKEN_GOVERNORS["openrouter"]
    google_before = google_gov.snapshot()["tokens_in_window"]
    openrouter_before = openrouter_gov.snapshot()["tokens_in_window"]

    with patch("openai.OpenAI", return_value=client):
        agent = ConcreteAgent(
            api_key="k",
            model="gemini-3.5-flash-lite",
            max_tokens=64,
            provider="google",
            fallback_api_key="fk",
        )
        assert agent._fallback_provider == "openrouter"
        assert agent._fallback_model == "google/gemini-3.5-flash-lite"
        agent.set_cost_circuit(circuit)
        result = agent.run(data="x")

    assert result.raw_text == '{"result": "ok"}'
    assert result.model == "google/gemini-3.5-flash-lite"
    assert result.actual_provider == "openrouter"
    assert result.used_fallback is True
    assert client.chat.completions.create.call_count == capacity_max_attempts() + 1  # primary attempts + 1 failover

    assert google_gov.snapshot()["tokens_in_window"] > google_before, (
        "the primary attempts must be charged to the GOOGLE governor"
    )
    assert openrouter_gov.snapshot()["tokens_in_window"] > openrouter_before, (
        "the failover attempt must be charged to the OPENROUTER governor"
    )

    with sqlite3.connect(circuit.db_path, uri=True) as conn:
        latched = conn.execute("SELECT suspended FROM llm_circuit_state WHERE singleton=1").fetchone()[0]
    assert not latched, "a transient rate-limit rescued by failover must not latch the desk"


# ------------------------------------ an inexact day must stay recoverable

# Found on 2026-08-31 by deploying the scope change above to production and
# watching what happened next. `fail_call` stamps the ET day inexact when it
# charges a conservative reserve for a request whose true cost it never
# learned, and nothing clears that within the day. The reconciler refuses to
# rearm over an inexact day, BUT returns early whenever a hard latch is set —
# so for as long as `provider_attempt_limit` latched, the latch masked the
# refusal. Scoping it to the session removed the mask, and the live desk went
# from "suspended" to "every activate_session raises RuntimeError, with no
# operator action able to clear it until ET rollover".
#
# A crash loop is a worse failure than the suspension it replaced. These pin
# the recovery path.


def _make_day_inexact(circuit, day):
    with sqlite3.connect(circuit.db_path, uri=True) as conn:
        conn.execute("UPDATE llm_budget_days SET costs_exact=0 WHERE day=?", (day,))
        conn.commit()


def _current_day():
    from src.cost_circuit import _et_day_and_utc_bounds

    return _et_day_and_utc_bounds()[0]


def test_an_inexact_day_with_no_latch_is_operator_resettable(tmp_path):
    """The exact live state after the deploy: no hard latch, inexact day."""
    circuit = LLMCostCircuitBreaker(_db_path(tmp_path), _config(), _Notifier())
    circuit.activate_session("run-inexact", "morning")
    _make_day_inexact(circuit, _current_day())

    circuit.reset("operator reviewed the conservative figure and accepted it")

    with sqlite3.connect(circuit.db_path, uri=True) as conn:
        row = conn.execute(
            "SELECT costs_exact, unknown_cost_rows FROM llm_budget_days WHERE day=?",
            (_current_day(),),
        ).fetchone()
    assert row[0] == 1 and row[1] == 0


def test_the_reset_does_not_erase_the_days_recorded_spend(tmp_path):
    """`scripts/cost_circuit.py` promises reset "never erases settled spend".
    Clearing the unprovable-figure flag must not become a way to zero the
    ledger — the conservative amount OVER-states cost, which is the safe
    direction, and it stays."""
    circuit = LLMCostCircuitBreaker(_db_path(tmp_path), _config(), _Notifier())
    circuit.activate_session("run-keep", "morning")
    day = _current_day()
    with sqlite3.connect(circuit.db_path, uri=True) as conn:
        conn.execute("UPDATE llm_budget_days SET incremental_cost_usd=0.0524, costs_exact=0 WHERE day=?", (day,))
        conn.commit()

    circuit.reset("accepted the conservative figure")

    with sqlite3.connect(circuit.db_path, uri=True) as conn:
        spend = conn.execute("SELECT incremental_cost_usd FROM llm_budget_days WHERE day=?", (day,)).fetchone()[0]
    assert spend == pytest.approx(0.0524)


def test_an_inexact_day_alone_does_not_stop_the_next_session(tmp_path):
    """Defect 6 (2026-08-31, found on the live desk): the exactness
    precondition guards ONE operation — releasing a hold carried over from an
    earlier ET day. It used to be checked before the query that finds those
    holds, so it fired when there were none, gating an operation that was not
    being performed.

    Because the reconciler runs on every `begin_call`, and `fail_call` stamps
    the day inexact whenever it charges a conservative reserve, the FIRST
    failed request in a day poisoned every paid call after it — the raise
    reads as the circuit's own infrastructure failing, which writes the
    emergency latch and stops the desk until an operator clears it.

    One rate-limited request, and the trading day was over."""
    path = _db_path(tmp_path)
    circuit = LLMCostCircuitBreaker(path, _config(), _Notifier())
    circuit.activate_session("run-first", "morning")
    _make_day_inexact(circuit, _current_day())

    later = LLMCostCircuitBreaker(path, _config(), _Notifier())
    later.activate_session("run-after", "intra_check")
    assert _reserve(later, agent="news_analyst") is not None


def test_an_inexact_day_still_refuses_to_rearm_a_cross_day_hold(tmp_path):
    """The safety property itself is unchanged: releasing YESTERDAY's stop
    while today's books are unproven is exactly what the precondition exists
    to prevent, and it still does."""
    path = _db_path(tmp_path)
    circuit = LLMCostCircuitBreaker(path, _config(), _Notifier())
    circuit.activate_session("run-first", "morning")
    _make_day_inexact(circuit, _current_day())
    with sqlite3.connect(circuit.db_path, uri=True) as conn:
        conn.execute(
            "INSERT INTO llm_quota_holds "
            "(scope, scope_key, day, trigger_code, trigger_detail, run_id, mode, "
            " agent_name, attempts, attempts_exact, costs_exact, session_cost_usd, "
            " daily_cost_usd, session_limit_usd, daily_limit_usd, active, "
            " alert_updated_at, recovery_alert_updated_at, created_at) "
            "VALUES ('mode_day','1999-01-01:morning','1999-01-01','daily_cost_limit',"
            "'yesterday','run-old','morning','tech_analyst',0,1,1,0,0,0.9,2.75,1,?,?,?)",
            (sql_utc_stamp(), sql_utc_stamp(), sql_utc_stamp()),
        )
        conn.commit()

    blocked = LLMCostCircuitBreaker(path, _config(), _Notifier())
    with pytest.raises(RuntimeError, match="accounting is not exact"):
        blocked.activate_session("run-blocked", "intra_check")


def test_a_later_session_activates_once_the_day_is_reconciled(tmp_path):
    """And the operator reset still clears the day, so a cross-day rearm can
    proceed after review."""
    path = _db_path(tmp_path)
    circuit = LLMCostCircuitBreaker(path, _config(), _Notifier())
    circuit.activate_session("run-first", "morning")
    _make_day_inexact(circuit, _current_day())

    circuit.reset("operator accepted the conservative figure")

    recovered = LLMCostCircuitBreaker(path, _config(), _Notifier())
    recovered.activate_session("run-after", "intra_check")
    assert _reserve(recovered, agent="news_analyst") is not None


def test_reset_still_refuses_when_there_is_nothing_to_reset(tmp_path):
    """The guard must not become a no-op: a healthy circuit on an exact day
    has nothing an operator reset should touch."""
    circuit = LLMCostCircuitBreaker(_db_path(tmp_path), _config(), _Notifier())
    circuit.activate_session("run-healthy", "morning")
    with pytest.raises(ValueError, match="accounting is exact"):
        circuit.reset("nothing is wrong")


# ------------------------------------- charging for calls that cost nothing

# Defect 7 (2026-08-31). A logical call can attempt several DIFFERENT
# providers, and `run()` re-raises the PRIMARY error while discarding whatever
# the failover hit. `fail_call` judged "did this cost anything" from that one
# exception, so a failover rejected 401 — which by definition billed nothing —
# was invisible, and the whole call was charged its conservative reserve at
# the FAILOVER model's dearer price.
#
# Live on the desk that afternoon: two upstream refusals plus a missing
# credential, real cost $0, charged $0.62, and the unexplained spend then
# latched everything. The rate limit did not stop trading. The phantom bill
# for it did.


class _Status(Exception):
    def __init__(self, status):
        super().__init__(f"status {status}")
        self.status_code = status


def test_a_call_whose_every_attempt_was_refused_is_not_charged(tmp_path):
    """429 upstream, 429 again, 401 on the failover. Nothing generated,
    nothing billed, nothing to charge."""
    from src.cost_circuit import _all_attempts_provably_free

    primary = _Status(429)
    assert _all_attempts_provably_free(primary, [primary, _Status(429), _Status(401)])


def test_one_ambiguous_attempt_makes_the_whole_call_chargeable(tmp_path):
    """Ambiguity is contagious on purpose: the reservation covers the whole
    call and there is no per-attempt figure to fall back on. A cut stream may
    have been billed for tokens already generated."""
    from src.agents.base import LLMStreamInterruptedError
    from src.cost_circuit import _all_attempts_provably_free

    primary = _Status(429)
    assert not _all_attempts_provably_free(primary, [primary, LLMStreamInterruptedError("cut mid-generation")])


def test_a_caller_that_cannot_enumerate_attempts_keeps_the_old_behaviour(tmp_path):
    """This may only ever recognise MORE genuinely-free failures, never
    fewer."""
    from src.cost_circuit import _all_attempts_provably_free

    assert _all_attempts_provably_free(_Status(429), None)
    assert not _all_attempts_provably_free(_Status(500), None)


def test_the_refused_call_neither_charges_nor_latches_the_desk(tmp_path):
    """End to end at the circuit: the exact 2026-08-31 shape must leave the
    ledger and the desk untouched."""
    circuit = LLMCostCircuitBreaker(_db_path(tmp_path), _config(), _Notifier())
    circuit.activate_session("run-refused", "morning")
    reservation = _reserve(circuit, agent="news_analyst")
    circuit.before_provider_attempt(reservation, model="google/gemini-3.5-flash-lite")
    circuit.before_provider_attempt(reservation, model="google/gemini-3.5-flash-lite")
    circuit.before_provider_attempt(reservation, model="claude-opus-4-7")

    primary = _Status(429)
    circuit.fail_call(
        reservation,
        primary,
        attempt_errors=[primary, _Status(429), _Status(401)],
    )

    with sqlite3.connect(circuit.db_path, uri=True) as conn:
        (latched,) = conn.execute("SELECT suspended FROM llm_circuit_state WHERE singleton=1").fetchone()
        spend, exact = conn.execute(
            "SELECT incremental_cost_usd, costs_exact FROM llm_budget_days WHERE day=?",
            (_current_day(),),
        ).fetchone()
    assert not latched, "a call that billed nothing must not latch the desk"
    assert spend == pytest.approx(0.0), "and must not appear on the ledger"
    assert exact, "nor make the day's accounting inexact"


def test_an_ambiguous_failure_still_latches_without_inventing_a_charge(tmp_path):
    """The guard is unchanged where it matters: a stream cut after generation
    may really have been billed, so it still stops the desk for an operator
    to look at. Item 14 (2026-09-02) changed HOW: there is no reservation
    left to convert into a dollar charge, so it marks the day inexact
    (`unknown_cost_rows`) instead of booking a guessed amount -- renamed
    from `..._still_charges_and_still_latches`, which pinned the deleted
    reservation charge."""
    from src.agents.base import LLMStreamInterruptedError

    circuit = LLMCostCircuitBreaker(_db_path(tmp_path), _config(), _Notifier())
    circuit.activate_session("run-ambiguous", "morning")
    reservation = _reserve(circuit, agent="news_analyst")
    circuit.before_provider_attempt(reservation, model="google/gemini-3.5-flash-lite")

    cut = LLMStreamInterruptedError("cut mid-generation")
    circuit.fail_call(reservation, cut, attempt_errors=[cut])

    with sqlite3.connect(circuit.db_path, uri=True) as conn:
        spend, unknown_rows = conn.execute(
            "SELECT incremental_cost_usd, unknown_cost_rows FROM llm_budget_days WHERE day=?",
            (_current_day(),),
        ).fetchone()
    assert spend == 0, "no reservation exists to convert into a dollar charge"
    assert unknown_rows > 0, "an attempt that may have been billed still fails closed"
    assert circuit.status()["suspended"] is True
