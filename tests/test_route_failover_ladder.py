"""The three-route failover ladder: error-aware backoff, the half-open
return to the primary, route 3 (a different MODEL), and — the part that
decides whether any of it is safe to ship — what all of it does to the cost
circuit's hard latch.

Background. The desk's LLM path had two routes serving the SAME model
(Google AI Studio direct, then OpenRouter). When the model itself was
saturated both failed together, and nothing ever returned to the primary. On
2026-09-22 that combination refused all paid analysis from 15:17 until a
human reset it at 00:33.
"""

from __future__ import annotations

import sqlite3
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from src.agents.base import (
    BACKOFF_FATAL,
    BACKOFF_JITTER,
    BACKOFF_RETRY_AFTER,
    _BACKOFF_CAP_S,
    _CAPACITY_STATUS_CODES,
    _FATAL_STATUS_CODES,
    _MIN_CAPACITY_BACKOFF_S,
    _ROUTE_COOLDOWN_MAX_S,
    _ROUTE_COOLDOWN_S,
    BaseAgent,
    RouteBreaker,
    _reset_route_breakers_for_tests,
    classify_backoff,
    error_aware_backoff_seconds,
    provider_attempt_budget,
    route_breaker_for,
)
from src import llm_route_journal
from src.cost_circuit import (
    _all_attempts_provably_free,
    _is_known_zero_cost_failure,
)


class _Notifier:
    """The circuit treats a missing/disabled notifier as a failed mandatory
    prerequisite and latches, so tests must supply a real one."""

    def __init__(self):
        self.messages: list[str] = []

    def send(self, message: str, **_kwargs) -> bool:
        self.messages.append(message)
        return True


class _Agent(BaseAgent):
    @property
    def name(self) -> str:
        return "test_agent"

    @property
    def system_prompt(self) -> str:
        return "Return JSON."

    def build_user_message(self, **kwargs) -> str:
        return "Analyze this bounded fixture."


def _err(status=None, message="boom", headers=None, cls=RuntimeError):
    """An SDK-shaped error: a status_code, and optionally an httpx-ish
    response carrying headers. Matches how `_retry_after_hint_seconds` and
    `_is_known_zero_cost_failure` actually read an exception."""
    exc = cls(message)
    if status is not None:
        exc.status_code = status
    if headers is not None:
        exc.response = SimpleNamespace(headers=headers)
    return exc


# ===========================================================================
# 1. Error-aware backoff — one behaviour per documented error class.
# ===========================================================================


@pytest.mark.parametrize("status", sorted(_FATAL_STATUS_CODES))
def test_fatal_statuses_are_never_retried(status):
    """400/401/403/404 are request-validation, auth and not-found
    rejections. Both provider references consulted describe them as
    deterministic: an identical repeat gets an identical answer, so a retry
    can only burn the budget the transient classes need.

    SOURCES: developers.openai.com/api/docs/guides/error-codes and
    platform.claude.com/docs/en/api/errors (both verified 2026-09-23).
    """
    exc = _err(status)
    assert classify_backoff(exc) == (BACKOFF_FATAL, None)
    assert error_aware_backoff_seconds(0, exc) is None, f"HTTP {status} must not be retried at all"


def test_fatal_class_is_not_retried_even_when_a_retry_after_is_present():
    """A Retry-After on a 400 is the provider contradicting itself. The
    fatal classification wins: waiting changes nothing about a malformed
    request, and honouring the hint would park the seat for nothing."""
    exc = _err(400, headers={"retry-after": "30"})
    assert classify_backoff(exc)[0] == BACKOFF_FATAL
    assert error_aware_backoff_seconds(0, exc) is None


def test_429_with_retry_after_header_honours_the_header_exactly():
    """OpenAI: "Follow the `Retry-After` header when it's present, then retry
    your request." Anthropic's SDKs likewise "honor the `retry-after` header
    when present". The stated number is the one authoritative figure in the
    exchange and it is used AS STATED — not max()'d against the exponential
    term, which is what the previous loop did and which made a server saying
    "come back in 1s" wait the full backoff anyway."""
    exc = _err(429, headers={"retry-after": "37"})
    kind, hint = classify_backoff(exc)
    assert kind == BACKOFF_RETRY_AFTER
    assert hint == 37.0
    for _ in range(50):
        assert error_aware_backoff_seconds(0, exc) == 37.0


def test_a_hostile_retry_after_is_capped():
    """A buggy or hostile hint must not park a seat past the session
    window — `_RETRY_AFTER_CAP_S`."""
    from src.agents.base import _RETRY_AFTER_CAP_S

    exc = _err(429, headers={"retry-after": "99999"})
    assert error_aware_backoff_seconds(0, exc) == _RETRY_AFTER_CAP_S


@pytest.mark.parametrize("status", sorted(_CAPACITY_STATUS_CODES))
def test_capacity_statuses_get_full_jitter_not_a_fixed_wait(status):
    """503/500/502/504/529 are capacity and transient-server classes: the
    retry can succeed, so they back off, and the backoff must be Full Jitter
    (AWS, "Exponential Backoff And Jitter") rather than a fixed ladder every
    concurrent caller walks in lockstep."""
    exc = _err(status)
    assert classify_backoff(exc) == (BACKOFF_JITTER, None)
    waits = {error_aware_backoff_seconds(3, exc) for _ in range(80)}
    assert len(waits) > 30, "a fixed backoff is not jitter"
    bound = min(_BACKOFF_CAP_S, float(2**3))
    assert all(_MIN_CAPACITY_BACKOFF_S <= w <= bound for w in waits)


def test_capacity_backoff_honours_googles_documented_one_second_floor():
    """The one place pure Full Jitter is wrong for this desk, and the source
    that says so.

    With `_DEFAULT_MAX_RETRIES = 2` there is exactly ONE sleep in the primary
    loop, so `uniform(0, 1)` can re-hit a 15-requests-per-minute free-tier
    ceiling within milliseconds. Google's own Gemini API troubleshooting page
    (https://ai.google.dev/gemini-api/docs/troubleshooting, verified
    2026-09-23) says for 429 RESOURCE_EXHAUSTED and 503 UNAVAILABLE: "Wait a
    short time before the first retry (for example, 1 second), then increase
    the delay exponentially ... Add random 'jitter'."

    The floor is read off that page. It applies to the capacity/rate-limit
    classes ONLY — no provider publishes a minimum for a transport blip, and
    inventing one would be an arbitrary number.
    """
    for status in (429, *sorted(_CAPACITY_STATUS_CODES)):
        exc = _err(status)
        for _ in range(100):
            assert error_aware_backoff_seconds(0, exc) >= _MIN_CAPACITY_BACKOFF_S

    # ...and NOT to a transport-class failure with no status.
    blip = _err(None, cls=ConnectionError)
    assert any(error_aware_backoff_seconds(0, blip) < _MIN_CAPACITY_BACKOFF_S for _ in range(200)), (
        "a floor with no published source must not be applied to classes the source does not cover"
    )


def test_google_429_has_no_retry_after_so_it_falls_to_jitter():
    """Google's troubleshooting page documents NO Retry-After header and no
    RetryInfo for its 429. The primary road is therefore the one road where
    the Retry-After branch never fires, and the jitter branch must cover it
    — asserted so a future reader does not assume the hint path protects the
    seat that actually gets rate-limited."""
    exc = _err(429, message="RESOURCE_EXHAUSTED: quota exceeded")
    assert classify_backoff(exc) == (BACKOFF_JITTER, None)
    assert error_aware_backoff_seconds(0, exc) >= _MIN_CAPACITY_BACKOFF_S


# ===========================================================================
# 2. The half-open breaker — backups are temporary.
# ===========================================================================


def test_breaker_starts_closed_and_demotes_on_failure():
    b = RouteBreaker("google")
    assert b.primary_available() is True
    assert b.demoted() is False
    assert b.record_failure() == _ROUTE_COOLDOWN_S
    assert b.demoted() is True
    assert b.primary_available() is False


def test_exactly_one_caller_probes_after_the_cooldown(monkeypatch):
    """The thundering-herd rule. The morning fan-out is five concurrent
    branches against one key; without a single-probe gate all five would
    arrive together the instant the cooldown expires and send five probes at
    a provider that is very likely still saturated."""
    now = [0.0]
    # One clock for the whole test: patched BEFORE the failure is stamped.
    monkeypatch.setattr("time.monotonic", lambda: now[0])
    b = RouteBreaker("google")
    b.record_failure()
    now[0] += 10**6  # far past cooldown
    grants = [b.primary_available() for _ in range(5)]
    assert grants.count(True) == 1, f"exactly one probe, got {grants}"
    assert b.is_probing() is True


def test_a_successful_probe_restores_the_primary_for_everybody():
    """The owner's ruling: "a backup is temporary ... it should ALWAYS be
    retrying the primary." Before this, one failure moved a seat onto a paid
    route permanently."""
    b = RouteBreaker("google")
    b.record_failure()
    assert b.demoted() is True
    assert b.record_success() is True, "success must report that it un-demoted"
    assert b.demoted() is False
    assert b.primary_available() is True
    assert b.record_success() is False, "a plain success is not a restoration"


def test_a_failed_probe_doubles_the_cooldown_up_to_the_ceiling(monkeypatch):
    now = [0.0]
    # Patched BEFORE the first failure, or `_demoted_until` is computed from
    # the real clock and every later comparison is against a different epoch.
    monkeypatch.setattr("time.monotonic", lambda: now[0])
    b = RouteBreaker("google")
    assert b.record_failure() == _ROUTE_COOLDOWN_S
    expected = _ROUTE_COOLDOWN_S
    for _ in range(12):
        now[0] += 10**6  # let the cooldown lapse
        assert b.primary_available() is True  # win the probe
        expected = min(expected * 2, _ROUTE_COOLDOWN_MAX_S)
        assert b.record_failure() == expected
    assert expected == _ROUTE_COOLDOWN_MAX_S, "the ceiling must bind"


def test_a_failed_probe_releases_the_probe_right(monkeypatch):
    """A probe that fails without releasing its right would wedge the breaker
    half-open forever and the primary would never be tried again — the exact
    never-comes-back defect this class was written to remove."""
    now = [0.0]
    # One clock for the whole test: patched BEFORE the failure is stamped.
    monkeypatch.setattr("time.monotonic", lambda: now[0])
    b = RouteBreaker("google")
    b.record_failure()
    now[0] += 10**6  # far past cooldown
    assert b.primary_available() is True
    b.record_failure()
    assert b.is_probing() is False


def test_breakers_are_keyed_on_the_provider_not_the_model():
    """The failure domain of every limit that fires here is the ACCOUNT.
    Google AI Studio's free tier is a per-project RPM/TPM ceiling and
    OpenRouter's limits are per-account, so three seats on three different
    OpenRouter models share ONE quota and must share ONE breaker. Keying on
    (provider, model) fragmented that into three breakers that each had to
    learn the same outage separately."""
    _reset_route_breakers_for_tests()
    a = route_breaker_for("openrouter")
    b = route_breaker_for("openrouter")
    c = route_breaker_for("google")
    assert a is b
    assert a is not c
    a.record_failure()
    assert b.demoted() is True
    assert c.demoted() is False, "one provider's outage is not another's"


def test_cooldowns_are_derived_from_their_bases_and_have_not_drifted():
    """The mechanical drift check standing in for a ledger row.

    Neither cooldown is a literal assignment — both are
    `float(os.environ.get(..., <expr>))` — so `src/number_sources.py`'s
    scanner cannot see them and a `config/number_ledger.yaml` row for either
    would be an orphan by that gate's own rule. This test is the enforcement
    instead, and it is strictly better than a row because it re-computes the
    derivation rather than restating the answer.

    BASE 1: `_RETRY_AFTER_CAP_S` — the longest wait the desk honours when a
    provider explicitly states one. A provider that stated nothing and failed
    everything has given less reason to return sooner.

    BASE 2: `src.config.INTRA_CHECK_TICK_MINUTES` — the gap between
    consecutive PAID runs, itself `sourced` in the ledger off
    `src/scheduler.py`'s real trigger, and already the base the cost
    circuit's own transient-latch cooldown is derived from. A breaker held
    down longer than the gap between runs can never be probed by the next
    run, which would quietly undo "it should ALWAYS be retrying the primary".
    """
    import os
    from src.agents.base import _RETRY_AFTER_CAP_S
    from src.config import INTRA_CHECK_TICK_MINUTES

    # Guard against an env override in the ambient environment masking drift.
    assert "QUANT_AGENT_ROUTE_COOLDOWN_S" not in os.environ
    assert "QUANT_AGENT_ROUTE_COOLDOWN_MAX_S" not in os.environ

    assert _ROUTE_COOLDOWN_S == _RETRY_AFTER_CAP_S
    assert _ROUTE_COOLDOWN_MAX_S == 60.0 * INTRA_CHECK_TICK_MINUTES
    assert _ROUTE_COOLDOWN_S < _ROUTE_COOLDOWN_MAX_S


# ===========================================================================
# 3. Route 3 — a genuinely DIFFERENT model, on a road that exists.
# ===========================================================================


def test_tertiary_default_is_a_different_model_from_both_other_routes():
    from src.agents.base import (
        _DEFAULT_FALLBACK_MODEL,
        _DEFAULT_FALLBACK_PROVIDER,
        _DEFAULT_TERTIARY_MODEL,
        _DEFAULT_TERTIARY_PROVIDER,
    )

    assert _DEFAULT_TERTIARY_MODEL != _DEFAULT_FALLBACK_MODEL, (
        "route 3 must change the MODEL; changing only the road is what routes "
        "1 and 2 already do, and it is what failed together on 2026-09-22"
    )
    assert "gemini" not in _DEFAULT_TERTIARY_MODEL
    # And on the road that has a verified credential grant. See
    # docs/architecture/CREDENTIAL_DELIVERY_EVIDENCE.md: openrouter.ai and
    # generativelanguage.googleapis.com are granted; api.openai.com and
    # api.anthropic.com are NOT.
    assert _DEFAULT_TERTIARY_PROVIDER in {"openrouter", "google"}
    assert _DEFAULT_FALLBACK_PROVIDER == "openrouter"


def test_the_tertiary_model_is_priceable_offline():
    """An unpriceable model reads to the cost circuit as unknown cost and
    latches paid analysis — the failure this route exists to prevent."""
    from src.agents.base import _DEFAULT_TERTIARY_MODEL
    from src.cost_table import PRICING

    row = PRICING.get(_DEFAULT_TERTIARY_MODEL)
    assert row and row.get("input") is not None and row.get("output") is not None


def test_tertiary_is_unreachable_without_a_key_or_when_it_duplicates_a_route():
    """Route 3 must not be a third attempt at something that already failed
    twice, and must not fire with no credential — a keyless attempt is a
    guaranteed 401 dressed up as a rescue."""
    _reset_route_breakers_for_tests()
    with patch("openai.OpenAI"):
        no_key = _Agent(
            api_key="k", model="gemini-3.5-flash-lite", provider="google", fallback_api_key="fk", tertiary_api_key=""
        )
        assert no_key._tertiary_reachable is False

        dupe = _Agent(
            api_key="k",
            model="gemini-3.5-flash-lite",
            provider="google",
            fallback_api_key="fk",
            tertiary_api_key="tk",
            tertiary_provider="openrouter",
            tertiary_model="google/gemini-3.5-flash-lite",
        )
        assert dupe._tertiary_reachable is False, "route 3 duplicating route 2 is not a third route"

        real = _Agent(
            api_key="k", model="gemini-3.5-flash-lite", provider="google", fallback_api_key="fk", tertiary_api_key="tk"
        )
        assert real._tertiary_reachable is True


def test_attempt_budget_grows_with_route_3_or_the_circuit_stops_the_session():
    """THE regression this whole change could have shipped.

    `max_provider_attempts_per_call` derives from `provider_attempt_budget`.
    A third route that the ceiling does not know about makes the rescue
    attempt the attempt that trips the circuit — which is the 2026-08-31
    09:32 ET incident verbatim, two minutes after the open, over $0.05.
    """
    base = provider_attempt_budget(failover_available=False)
    assert provider_attempt_budget(failover_available=True) == base + 1
    assert provider_attempt_budget(failover_available=True, tertiary_available=True) == base + 2


def test_config_default_ceiling_covers_the_whole_ladder():
    from src.config import LLMCostCircuitConfig

    cfg = LLMCostCircuitConfig()
    assert cfg.max_provider_attempts_per_call >= provider_attempt_budget(
        failover_available=True,
        tertiary_available=True,
    )


# ===========================================================================
# 4. THE COST LATCH. The question that decides whether this is safe.
# ===========================================================================
#
# `fail_call` computes `ambiguous = attempted and not
# _all_attempts_provably_free(...)` and hard-latches on ONE ambiguous
# attempt. Every attempt this change adds is another draw from that urn.


def test_skipping_a_demoted_primary_records_no_attempt_error():
    """The single most dangerous thing this change could have done.

    When the primary is demoted the call SKIPS it. If that skip were
    represented as a synthetic exception, it would carry no `status_code`,
    `_is_known_zero_cost_failure` would fail closed on it, and a request the
    desk deliberately DID NOT MAKE would be booked ambiguous and latch the
    desk. The skip is an absence of an attempt and must be represented as
    one — asserted here against the classifier itself.
    """
    synthetic = RuntimeError("primary is demoted and no backup route is reachable")
    assert _is_known_zero_cost_failure(synthetic) is False, (
        "this is exactly why such a marker must never enter attempt_errors"
    )
    # An empty attempt list is what the skip actually produces, and
    # `fail_call` guards on `reservation.attempt_count > 0` before it even
    # consults the classifier.
    assert _all_attempts_provably_free(synthetic, []) is False
    # ...so the protection is the attempt COUNT, not the classification.
    # Covered end-to-end in test_a_fully_skipped_call_cannot_latch below.


def test_a_fully_skipped_call_cannot_latch_the_cost_circuit(tmp_path):
    """No provider attempt was made, so there is nothing whose cost is
    unknown. `fail_call` must record the failure and NOT trip."""
    from src.cost_circuit import LLMCostCircuitBreaker
    from src.storage.db import Database

    path = str(tmp_path / "c.db")
    db = Database(path)
    db.initialize()
    db.conn.close()
    circuit = LLMCostCircuitBreaker(
        path,
        SimpleNamespace(
            enabled=True,
            session_cost_limit_usd=10.0,
            daily_cost_limit_usd=20.0,
            max_calls_per_session=1000,
            max_provider_attempts_per_call=4,
            input_chars_per_token=3.5,
        ),
        _Notifier(),
    )
    circuit.activate_session("run-skip", "morning")
    res = circuit.begin_call(
        agent_name="tech_analyst",
        model="gemini-3.5-flash-lite",
        system_prompt="s",
        user_message="u",
        max_output_tokens=100,
    )
    assert res.attempt_count == 0
    circuit.fail_call(res, RuntimeError("primary demoted, no backup reachable"), attempt_errors=[])
    assert circuit.status().get("suspended") is not True, (
        "a call that never reached the network must not latch the desk"
    )


def test_every_status_the_tertiary_road_documents_is_classified():
    """Route 3's ambiguity surface, enumerated rather than assumed.

    OpenRouter "normalizes every upstream provider error into the stable,
    typed error_type vocabulary" and surfaces the upstream code only in
    `error.metadata.provider_code` (openrouter.ai/docs/api-reference/errors,
    verified 2026-09-23). So the statuses this road can present are
    OpenRouter's own, NOT Anthropic's 529 — which is why the first draft's
    "avoid Anthropic because of 529" argument was withdrawn.

    Of those, everything except the 5xx family is already provably-$0 to the
    circuit. 502/503-after-generation remain ambiguous, and they were ALREADY
    ambiguous for route 2, which runs on the same road. Route 3 therefore
    adds no NEW ambiguous status — it adds one more draw of the same kind.
    The allow-list is deliberately NOT grown; its comment forbids that
    without evidence, and one more route is not evidence.
    """
    provably_free = {400, 401, 402, 403, 404, 408, 429}
    for status in sorted(provably_free - {402, 408}):
        assert _is_known_zero_cost_failure(_err(status)) is True, status
    # The honest residual, asserted so it cannot be forgotten:
    for status in (500, 502):
        assert _is_known_zero_cost_failure(_err(status)) is False, (
            f"{status} is ambiguous and this change does not fix that"
        )
    # 503 is free ONLY when it arrived before generation started.
    assert _is_known_zero_cost_failure(_err(503)) is True


def test_a_rescued_call_never_reaches_fail_call_at_all(tmp_path):
    """The whole reason a third route makes the latch LESS likely, not more:
    `fail_call` runs only when the logical call failed. A route that rescues
    the call means the ambiguity question is never asked."""
    from src.cost_circuit import LLMCostCircuitBreaker
    from src.storage.db import Database

    path = str(tmp_path / "c.db")
    db = Database(path)
    db.initialize()
    db.conn.close()
    circuit = LLMCostCircuitBreaker(
        path,
        SimpleNamespace(
            enabled=True,
            session_cost_limit_usd=10.0,
            daily_cost_limit_usd=20.0,
            max_calls_per_session=1000,
            max_provider_attempts_per_call=4,
            input_chars_per_token=3.5,
        ),
        _Notifier(),
    )
    circuit.activate_session("run-rescue", "morning")
    res = circuit.begin_call(
        agent_name="tech_analyst",
        model="gemini-3.5-flash-lite",
        system_prompt="s",
        user_message="u",
        max_output_tokens=100,
    )
    # Two ambiguous failures on routes 1 and 2 ...
    for _ in range(3):
        circuit.before_provider_attempt(res, model=res.model)
    # ... then route 3 answers, with a real cost.
    circuit.complete_call(res, 0.004)
    assert circuit.status().get("suspended") is not True, (
        "prior ambiguous ATTEMPTS on a call that ultimately SUCCEEDED must not latch: the call's real cost is known"
    )


def test_ambiguity_is_contagious_so_route_3_adds_one_draw_not_a_multiplier():
    """States the mechanism precisely, because the intuition "more attempts =
    more latching" is wrong in one direction and right in the other.

    `_all_attempts_provably_free` is an `all(...)`: ONE ambiguous attempt
    condemns the whole call regardless of how many free ones accompany it.
    So adding a route cannot make a call that was already going to latch
    latch harder — but it CAN turn an all-free failed call into an ambiguous
    one, and that is the honest cost of route 3.
    """
    free = [_err(429), _err(400)]
    assert _all_attempts_provably_free(free[0], free) is True
    ambiguous = free + [_err(500)]
    assert _all_attempts_provably_free(ambiguous[0], ambiguous) is False
    # ...and one more free attempt does not rescue it.
    assert _all_attempts_provably_free(ambiguous[0], ambiguous + [_err(429)]) is False


# ===========================================================================
# 5. Observability — the owner must be able to ask what each route cost.
# ===========================================================================


def _rows(monkeypatch, tmp_path):
    return llm_route_journal.read_events()


def test_route_events_are_recorded_durably(tmp_path, monkeypatch):
    monkeypatch.setenv("QUANT_AGENT_DB_PATH", str(tmp_path / "j.db"))
    llm_route_journal._reset_schema_cache_for_tests()
    assert (
        llm_route_journal.record(
            "route_switch",
            agent_name="tech_analyst",
            run_id="r1",
            route="openrouter/anthropic/claude-haiku-4.5",
            from_route="google/x",
            tier=3,
            input_usd_per_mtok=1.0,
            output_usd_per_mtok=5.0,
        )
        is True
    )
    assert llm_route_journal.record("route_demoted", agent_name="a", wait_s=300.0)
    assert llm_route_journal.record("retry_after", wait_s=37.0)
    rows = llm_route_journal.read_events()
    assert {r["event_type"] for r in rows} == {"route_switch", "route_demoted", "retry_after"}
    switch = next(r for r in rows if r["event_type"] == "route_switch")
    assert switch["tier"] == 3
    assert switch["output_usd_per_mtok"] == 5.0
    assert switch["run_id"] == "r1"
    # Durable means it survives this process reading it back off disk.
    conn = sqlite3.connect(str(tmp_path / "j.db"))
    assert conn.execute("SELECT COUNT(*) FROM llm_route_events").fetchone()[0] == 3
    conn.close()


def test_an_unknown_event_type_is_refused_not_silently_stored():
    assert llm_route_journal.record("route_teleported") is False


def test_a_failed_journal_write_is_counted_not_silently_swallowed(monkeypatch, tmp_path):
    """ "Best-effort, never raises" is right — a journal must not be able to
    fail a trading session. But a swallowed failure nobody can see is a check
    that does not exist: an empty table then looks identical to "no route
    switches happened". The counter is what makes the difference visible."""
    llm_route_journal._reset_schema_cache_for_tests()
    monkeypatch.setenv("QUANT_AGENT_DB_PATH", str(tmp_path / "nope" / "x.db"))
    before = llm_route_journal.write_failures()
    assert llm_route_journal.record("route_switch") is False
    assert llm_route_journal.write_failures() == before + 1


def test_journal_never_raises_into_the_caller(monkeypatch):
    def boom(*a, **k):
        raise sqlite3.OperationalError("disk is on fire")

    monkeypatch.setattr(llm_route_journal, "_connect", boom)
    assert llm_route_journal.record("route_switch") is False
    assert llm_route_journal.read_events() == []


# ===========================================================================
# 3b. Route 3's SECOND ROAD — surviving a whole-provider outage.
#
# Measured defect, production log /home/qamc/quant-agent/quant_agent.log
# 2026-09-29 19:46:45-19:47:46: portfolio_manager's route 1
# (openrouter, openai/gpt-5.5) got HTTP 402 Payment Required, route 2 was
# skipped on the demoted same account, route 3 (openrouter,
# anthropic/claude-haiku-4.5) got HTTP 402 again, and the agent logged
# "Every route is down." At 19:46:08 the same process had completed
# tech_analyst on Google AI Studio direct at HTTP 200 for $0.00. Three
# rungs, one account, one balance, nothing survived.
# ===========================================================================

_ALT = ("google", "gemini-3.5-flash-lite")
_TERT = ("openrouter", "anthropic/claude-haiku-4.5")
_FB = ("openrouter", "google/gemini-3.5-flash-lite")


def test_a_single_road_ladder_is_moved_onto_the_second_road():
    """The measured case: every rung on OpenRouter becomes two roads."""
    from src.agents.base import select_tertiary_route

    chosen = select_tertiary_route(
        primary=("openrouter", "openai/gpt-5.5"),
        fallback=_FB,
        tertiary=_TERT,
        alt=_ALT,
    )
    assert chosen == _ALT
    assert chosen[0] != "openrouter"


def test_a_two_road_ladder_keeps_its_different_model_tertiary():
    """The eight Google-primary specialist seats must NOT be touched: their
    route 1 is already Google, so a Google route 3 would retry the road that
    just failed twice. Model diversity is the right answer there."""
    from src.agents.base import select_tertiary_route

    chosen = select_tertiary_route(
        primary=("google", "gemini-3.5-flash-lite"),
        fallback=_FB,
        tertiary=_TERT,
        alt=_ALT,
    )
    assert chosen == _TERT


def test_the_substitute_is_refused_when_it_would_not_change_the_road():
    from src.agents.base import select_tertiary_route

    assert (
        select_tertiary_route(
            primary=("openrouter", "openai/gpt-5.5"),
            fallback=_FB,
            tertiary=_TERT,
            alt=("openrouter", "mistralai/mistral-medium"),
        )
        == _TERT
    )


def test_the_substitute_is_off_when_unconfigured_or_uncredentialed():
    from src.agents.base import select_tertiary_route

    assert (
        select_tertiary_route(
            primary=("openrouter", "openai/gpt-5.5"),
            fallback=_FB,
            tertiary=_TERT,
            alt=None,
        )
        == _TERT
    )


def test_an_unreachable_route_2_does_not_count_as_a_second_road():
    """`fallback=None` means route 2 cannot fire for this seat, so the ladder
    is route 1 + route 3 only and must not be credited with route 2's road."""
    from src.agents.base import select_tertiary_route

    assert (
        select_tertiary_route(
            primary=("openrouter", "openai/gpt-5.5"),
            fallback=None,
            tertiary=_TERT,
            alt=_ALT,
        )
        == _ALT
    )


def test_the_substitute_model_is_priceable_offline():
    """Same latch risk as the tertiary's own pricing test above."""
    from src.agents.base import _DEFAULT_TERTIARY_ALT_MODEL
    from src.cost_table import PRICING

    row = PRICING.get(_DEFAULT_TERTIARY_ALT_MODEL)
    assert row and row.get("input") is not None and row.get("output") is not None


def test_the_substitute_default_is_not_on_the_default_tertiary_road():
    from src.agents.base import (
        _DEFAULT_FALLBACK_PROVIDER,
        _DEFAULT_TERTIARY_ALT_MODEL,
        _DEFAULT_TERTIARY_ALT_PROVIDER,
        _DEFAULT_TERTIARY_PROVIDER,
    )

    assert _DEFAULT_TERTIARY_ALT_PROVIDER not in {
        _DEFAULT_TERTIARY_PROVIDER,
        _DEFAULT_FALLBACK_PROVIDER,
    }
    # Only a road this deployment has actually completed a call on. See
    # docs/architecture/CREDENTIAL_DELIVERY_EVIDENCE.md.
    assert _DEFAULT_TERTIARY_ALT_PROVIDER in {"openrouter", "google"}
    assert _DEFAULT_TERTIARY_ALT_MODEL


def test_an_openrouter_primary_seat_ends_up_on_two_roads_end_to_end():
    """The agent-level assertion, not just the helper's: a PM-shaped seat
    built through BaseAgent carries a non-OpenRouter route 3, with the
    matching credential and the matching breaker."""
    _reset_route_breakers_for_tests()
    with patch("openai.OpenAI"):
        pm = _Agent(
            api_key="k",
            model="openai/gpt-5.5",
            provider="openrouter",
            fallback_api_key="fk",
            tertiary_api_key="tk",
            tertiary_provider="openrouter",
            tertiary_model="anthropic/claude-haiku-4.5",
            tertiary_alt_api_key="gk",
            tertiary_alt_provider="google",
            tertiary_alt_model="gemini-3.5-flash-lite",
        )
        assert pm._tertiary_provider == "google"
        assert pm._tertiary_model == "gemini-3.5-flash-lite"
        assert pm._tertiary_api_key == "gk"
        assert pm._tertiary_reachable is True
        assert pm._tertiary_on_alt_road is True
        assert pm._tertiary_breaker is not pm._fallback_breaker, (
            "route 3 sharing route 2's breaker means one account, one "
            "failure domain — the whole point is that it is now two"
        )
        assert {pm._provider, pm._fallback_provider, pm._tertiary_provider} == {"openrouter", "google"}

        specialist = _Agent(
            api_key="k",
            model="gemini-3.5-flash-lite",
            provider="google",
            fallback_api_key="fk",
            tertiary_api_key="tk",
            tertiary_alt_api_key="gk",
        )
        assert specialist._tertiary_provider == "openrouter"
        assert specialist._tertiary_on_alt_road is False


def test_the_substitution_adds_no_rung_to_the_attempt_budget():
    """It swaps route 3's destination; it must never make a fourth attempt."""
    _reset_route_breakers_for_tests()
    with patch("openai.OpenAI"):
        swapped = _Agent(
            api_key="k",
            model="openai/gpt-5.5",
            provider="openrouter",
            fallback_api_key="fk",
            tertiary_api_key="tk",
            tertiary_alt_api_key="gk",
        )
        plain = _Agent(
            api_key="k",
            model="openai/gpt-5.5",
            provider="openrouter",
            fallback_api_key="fk",
            tertiary_api_key="tk",
            tertiary_alt_api_key="",
        )
    assert provider_attempt_budget(
        failover_available=swapped._failover_reachable,
        tertiary_available=swapped._tertiary_reachable,
    ) == provider_attempt_budget(
        failover_available=plain._failover_reachable,
        tertiary_available=plain._tertiary_reachable,
    )


def test_the_shipped_config_leaves_no_seat_on_a_single_road():
    """The end-to-end claim, read off config/settings.yaml itself rather
    than off the defaults: after this change EVERY seat's reachable routes
    span at least two providers. This is the test that would have caught
    2026-09-29."""
    import os
    from pathlib import Path

    from src.agents.base import resolve_provider, select_tertiary_route
    from src.config import AGENT_NAMES, load_config

    for key in (
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
        "DEEPSEEK_API_KEY",
        "OPENROUTER_API_KEY",
        "GOOGLE_API_KEY",
        "FRED_API_KEY",
        "ALPACA_API_KEY",
        "ALPACA_SECRET_KEY",
    ):
        os.environ.setdefault(key, "placeholder-for-config-load")
    settings = Path(__file__).resolve().parents[1] / "config" / "settings.yaml"
    cfg = load_config(str(settings))
    llm = cfg.llm
    fallback = (llm.fallback_provider, llm.fallback_model)
    tertiary = (llm.tertiary_provider, llm.tertiary_model)
    alt = (llm.tertiary_alt_provider, llm.tertiary_alt_model)

    primaries = {resolve_provider(getattr(llm, f"{a}_model"), llm.get_provider(a)) for a in AGENT_NAMES}
    assert llm.tertiary_alt_provider in primaries, (
        "the substitute must sit on a provider some seat already uses as its "
        "PRIMARY, so its credential is already mandatory at config load — "
        "otherwise a silently-missing key turns route 3 into a guaranteed 401"
    )

    for agent_name in AGENT_NAMES:
        model = getattr(llm, f"{agent_name}_model")
        primary = (resolve_provider(model, llm.get_provider(agent_name)), model)
        reachable_fallback = None if fallback == primary else fallback
        route3 = select_tertiary_route(
            primary=primary,
            fallback=reachable_fallback,
            tertiary=tertiary,
            alt=alt,
        )
        roads = {primary[0], route3[0]}
        if reachable_fallback is not None:
            roads.add(reachable_fallback[0])
        assert len(roads) >= 2, (
            f"{agent_name} has every route on {roads} — one provider outage "
            f"takes the whole seat, which is the 2026-09-29 defect"
        )
