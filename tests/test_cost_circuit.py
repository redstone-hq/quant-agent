"""Mandatory paid-analysis circuit: accounting, persistence and cutoffs."""

from __future__ import annotations

import logging
import json
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from src.agents.base import (
    BaseAgent,
    LLMStreamErrorChunk,
    LLMStreamInterruptedError,
)
import src.cost_circuit
from src import trading_calendar
from src.cost_circuit import (
    LLMCostCircuitBreaker,
    OptionalPaidAnalysisRetrySkipped,
    PaidAnalysisSuspended,
    _ET,
    _et_day_and_utc_bounds,
    _is_known_zero_cost_failure,
    _trigger_scope,
    activate_paid_call_session,
)
from src.pipeline import TradingPipeline
from src.storage.db import Database
from tests.pipeline_factory import build_pipeline


class _Notifier:
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


def _config(**overrides):
    values = {
        "enabled": True,
        "session_cost_limit_usd": 10.0,
        "daily_cost_limit_usd": 20.0,
        # Item 14 (2026-09-02): the runaway-loop backstop is now a plain
        # call count (docs/WORK.md item 14c), not a reservation-era
        # free-failure-session count. High by default so tests not
        # exercising the cap itself never hit it; tests for the cap
        # override it explicitly.
        "max_calls_per_session": 1000,
        "max_provider_attempts_per_call": 2,
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


def test_pipeline_attaches_breaker_to_every_paid_agent():
    circuit = object()
    pipeline = build_pipeline(cost_circuit=circuit)
    names = (
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
    agents = {}
    for name in names:
        agent = MagicMock()
        agents[name] = agent
        setattr(pipeline, name, agent)

    pipeline._attach_cost_circuit_to_agents()

    for agent in agents.values():
        agent.set_cost_circuit.assert_called_once_with(circuit)


def test_optional_retry_budget_exhaustion_skips_without_opening_circuit(tmp_path):
    """Item 14 (2026-09-02): the optional-retry skip is now keyed off the
    plain per-session call-count backstop (14c), not a reservation-era
    retry-attempts counter. A session that has already used its whole
    call budget on real work still lets an OPTIONAL retry bow out quietly
    (OptionalPaidAnalysisRetrySkipped) instead of tripping the circuit --
    the caller may safely keep its already-completed primary analysis."""
    path = _db_path(tmp_path)
    circuit = LLMCostCircuitBreaker(path, _config(max_calls_per_session=2), _Notifier())
    circuit.activate_session("run-optional-retry", "morning")
    for _ in range(2):
        reservation = circuit.begin_call(
            agent_name="tech_analyst",
            model="google/gemini-3.5-flash-lite",
            system_prompt="s",
            user_message="u",
            max_output_tokens=100,
        )
        circuit.before_provider_attempt(reservation, model=reservation.model)
        circuit.complete_call(reservation, 0.01)

    with pytest.raises(OptionalPaidAnalysisRetrySkipped):
        circuit.begin_call(
            agent_name="tech_analyst",
            model="google/gemini-3.5-flash-lite",
            system_prompt="s",
            user_message="u",
            max_output_tokens=100,
            retry_kind="missing_symbol_recovery",
            optional_retry=True,
        )

    state = circuit.status()
    assert state["suspended"] is False
    with sqlite3.connect(path) as conn:
        row = conn.execute(
            "SELECT logical_calls, status FROM llm_budget_sessions WHERE run_id=?",
            ("run-optional-retry",),
        ).fetchone()
    # The skipped optional retry never incremented logical_calls or
    # touched session status -- exactly like the reservation-era version,
    # just measured by a raw count instead of a reservation row.
    assert row == (2, "active")


def test_completed_session_spend_holds_only_that_session_and_cannot_be_reset(tmp_path):
    path = _db_path(tmp_path)
    notifier = _Notifier()
    cfg = _config(session_cost_limit_usd=0.50, daily_cost_limit_usd=2.0)
    circuit = LLMCostCircuitBreaker(path, cfg, notifier)
    circuit.activate_session("run-cost", "morning")
    reservation = circuit.begin_call(
        agent_name="tech_analyst",
        model="google/gemini-3.5-flash-lite",
        system_prompt="s",
        user_message="u",
        max_output_tokens=100,
    )
    circuit.before_provider_attempt(reservation, model=reservation.model)
    circuit.complete_call(reservation, 0.60, actual_model=reservation.model)

    assert circuit.status()["trigger_code"] == "session_cost_limit"
    assert circuit.status()["current_session_cost_usd"] == pytest.approx(0.60)
    assert len(notifier.messages) == 1

    # A new process/object sees the same run-scoped hold and does not duplicate
    # an alert already successfully delivered.
    second_notifier = _Notifier()
    second = LLMCostCircuitBreaker(path, cfg, second_notifier)
    second.activate_session("run-cost", "morning")
    assert second.status()["suspended"] is True
    assert second_notifier.messages == []

    with pytest.raises(ValueError):
        second.reset("  ")
    with pytest.raises(ValueError, match="no operator-resettable hard circuit"):
        second.reset("operator reviewed quota hold")

    # The affected run remains stopped, while a genuinely independent session
    # can spend under the still-available daily budget.
    second.activate_session("run-cost-next", "midday")
    reservation = second.begin_call(
        agent_name="tech_analyst",
        model="google/gemini-3.5-flash-lite",
        system_prompt="s",
        user_message="u",
        max_output_tokens=100,
    )
    assert reservation.reservation_id != "disabled"
    second.activate_session("run-cost", "morning")
    with pytest.raises(PaidAnalysisSuspended):
        second.require_paid_analysis("same_run_again")


def test_existing_daily_logs_seed_budget_and_trip_on_activation(tmp_path):
    path = _db_path(tmp_path)
    with sqlite3.connect(path) as conn:
        conn.executemany(
            "INSERT INTO agent_logs (agent_name, run_id, model, tokens_used, cost_usd) VALUES (?, ?, ?, ?, ?)",
            [
                ("tech_analyst", "run-old-1", "m", 1, 0.80),
                ("portfolio_manager", "run-old-2", "m", 1, 0.75),
            ],
        )
    notifier = _Notifier()
    circuit = LLMCostCircuitBreaker(
        path,
        _config(session_cost_limit_usd=1.0, daily_cost_limit_usd=1.50),
        notifier,
    )
    circuit.activate_session("run-new", "morning")

    state = circuit.status()
    assert state["trigger_code"] == "daily_cost_limit"
    assert state["current_daily_cost_usd"] == pytest.approx(1.55)
    assert "$1.55 on ET day" in notifier.messages[0]
    assert "auto" in notifier.messages[0]


def test_base_agent_third_provider_attempt_is_blocked_before_network(tmp_path, monkeypatch):
    path = _db_path(tmp_path)
    notifier = _Notifier()
    circuit = LLMCostCircuitBreaker(
        path,
        _config(max_provider_attempts_per_call=2),
        notifier,
    )
    circuit.activate_session("run-retries", "morning")
    monkeypatch.setenv("QUANT_AGENT_MAX_RETRIES", "3")
    monkeypatch.setattr("time.sleep", lambda _seconds: None)

    client = MagicMock()
    client.messages.create.side_effect = ConnectionError("provider down")
    with patch("anthropic.Anthropic", return_value=client):
        agent = _Agent(api_key="x", model="claude-sonnet-4-6", max_tokens=64)
        agent.set_cost_circuit(circuit)
        with pytest.raises(PaidAnalysisSuspended):
            agent.run()

    # The point of the test, unchanged: the attempt past the ceiling never
    # reaches the network.
    assert client.messages.create.call_count == 2
    assert circuit.status()["provider_attempts"] == 2

    # Defect 5 (2026-08-31): the attempt limit now HOLDS the run rather than
    # latching the desk, so it is the first of two alerts, not the only one.
    assert "QUOTA HOLD" in notifier.messages[0]
    assert "provider attempt 3 exceeds per-call safe limit 2" in notifier.messages[0]
    assert "scope: run run-retries only" in notifier.messages[0]

    # And then the call it blocked fails with no usage telemetry, which is a
    # SEPARATE, still-hard trigger. Asserted rather than glossed: this is the
    # remaining path by which a per-call attempt stop can still cost a
    # trading day, and it is deliberately left alone. `fail_call` cannot tell
    # "the circuit refused to send attempt 3" from "attempts 1 and 2 burned
    # tokens we never got told about", and on a system that trades money,
    # under-counting real spend is the worse error. It no longer fires on the
    # 2026-08-31 failure mode (see tests/test_provider_attempt_budget.py --
    # with the ceiling correct, the failover simply succeeds), so what
    # reaches here is a genuine attempt runaway, which is a thing an operator
    # SHOULD be made to look at.
    state = circuit.status()
    assert state["trigger_code"] == "failed_call_unknown_cost"
    # Item 208: the quota hold pages at once (it needs an operator); the
    # transient latch waits out its own self-clear window first.
    assert len(notifier.messages) == 1
    _age_latch_past_self_clear_window(circuit)
    assert len(notifier.messages) == 2
    assert "no provable-zero-cost telemetry" in notifier.messages[1]


def test_failed_call_with_ambiguous_cost_latches_without_inventing_a_charge(tmp_path, monkeypatch):
    """Item 14 (2026-09-02): an ambiguous failure (not provably $0) still
    fails closed -- but there is no reservation left to convert into a
    dollar charge, so it marks the day/session inexact and latches on
    THAT, rather than booking a guessed amount. Renamed from
    `test_failed_call_without_usage_consumes_reserve_and_latches`, which
    pinned the old "charge the conservative reserve" behavior this
    replaces."""
    path = _db_path(tmp_path)
    notifier = _Notifier()
    circuit = LLMCostCircuitBreaker(path, _config(), notifier)
    circuit.activate_session("run-failed", "morning")
    monkeypatch.setenv("QUANT_AGENT_MAX_RETRIES", "1")

    client = MagicMock()
    client.messages.create.side_effect = ConnectionError("stream cut")
    with patch("anthropic.Anthropic", return_value=client):
        agent = _Agent(api_key="x", model="claude-sonnet-4-6", max_tokens=64)
        agent.set_cost_circuit(circuit)
        with pytest.raises(ConnectionError):
            agent.run()

    state = circuit.status()
    assert state["trigger_code"] == "failed_call_unknown_cost"
    # No reservation exists to charge any more -- an ambiguous failure
    # costs the ledger nothing invented, only certainty.
    assert state["current_session_cost_usd"] == 0
    assert state["current_daily_cost_usd"] == 0
    # Item 208: recorded immediately, sent only once it outlives the
    # circuit's own self-clear window.
    assert notifier.messages == []
    _age_latch_past_self_clear_window(circuit)
    assert "no provable-zero-cost telemetry" in notifier.messages[0]


# ============================================================================
# Defect 2 (2026-08-28): a provably-$0 provider rejection must not be
# charged, and must not trip the circuit. `_is_known_zero_cost_failure`
# is deliberately narrow -- every one of these tests confirms one specific
# admitted class; the very last one confirms the fail-closed default is
# untouched (an unrecognized/ambiguous failure is charged and trips
# exactly as before this fix).
# ============================================================================


class _StatusCodeError(Exception):
    """Stand-in for `anthropic.APIStatusError` / `openai.APIStatusError` --
    both SDKs set `.status_code` on every subclass (RateLimitError,
    BadRequestError, AuthenticationError, PermissionDeniedError,
    NotFoundError, InternalServerError, ...)."""

    def __init__(self, status_code: int, message: str = "provider error"):
        super().__init__(message)
        self.status_code = status_code


def _wrapped(outer_message: str, *, outer_name: str, cause: BaseException) -> Exception:
    """Build `OuterName("outer_message")` with `__cause__ == cause`, the way
    the anthropic/openai SDKs wrap the concrete httpx/socket/ssl failure
    (`raise APIConnectionError(...) from exc`) instead of replacing it."""
    outer_cls = type(outer_name, (Exception,), {})
    err = outer_cls(outer_message)
    err.__cause__ = cause
    return err


def _authorize_and_fail(
    circuit,
    error: BaseException,
    *,
    run_id: str,
    agent_name: str = "tech_analyst",
    model: str = "google/gemini-3.5-flash-lite",
):
    """Reserve + authorize one provider attempt (so `attempt_count > 0`,
    matching every real call this classification applies to -- provider
    authorization always happens before the network call that can fail),
    then fail it with `error`. Returns the reservation."""
    circuit.activate_session(run_id, "morning")
    reservation = circuit.begin_call(
        agent_name=agent_name,
        model=model,
        system_prompt="s",
        user_message="u",
        max_output_tokens=100,
    )
    circuit.before_provider_attempt(reservation, model=reservation.model)
    circuit.fail_call(reservation, error)
    return reservation


def _assert_charged_nothing_and_did_not_trip(circuit, notifier, path, reservation):
    state = circuit.status()
    assert state["suspended"] is False
    assert not state.get("trigger_code")
    assert state["current_session_cost_usd"] == 0
    assert state["current_daily_cost_usd"] == 0
    assert notifier.messages == []
    with sqlite3.connect(path) as conn:
        row = conn.execute(
            "SELECT status, actual_cost_usd, costs_exact FROM llm_budget_sessions WHERE run_id=?",
            (reservation.run_id,),
        ).fetchone()
    assert row == ("active", 0.0, 1)


def _age_latch_past_self_clear_window(circuit):
    """Item 208: make a self-clearing latch DURABLE for test purposes.

    The owner is no longer paged the instant a transient provider fault
    latches -- only once the latch has outlived the circuit's own
    `transient_latch_cooldown_minutes` self-clear window without expiring.
    Tests that assert the owner-facing text therefore have to get the latch
    to that point, and this backdates `suspended_at` rather than sleeping.
    """
    with circuit._connect() as conn:
        conn.execute("UPDATE llm_circuit_state SET suspended_at=datetime('now', '-1 day') WHERE singleton=1")
        conn.commit()
    circuit._notify_if_needed()


def _assert_charged_and_tripped(circuit, notifier, reservation, expected_code="failed_call_unknown_cost"):
    state = circuit.status()
    assert state["suspended"] is True
    assert state["trigger_code"] == expected_code
    # Item 208: a transient latch inside its own self-clear window is
    # RECORDED, not sent -- announcing both edges of a fault that clears
    # itself in minutes is what produced 44 of the 107 messages the owner
    # received over 26-29 Sep. The suspension itself is in force either way.
    assert notifier.messages == []
    with circuit._connect() as conn:
        deferred = conn.execute(
            "SELECT COUNT(*) FROM llm_circuit_events WHERE event_type='suspend_alert_deferred'"
        ).fetchone()[0]
    assert deferred == 1, "a held alert must still be written down"
    _age_latch_past_self_clear_window(circuit)
    # Item 14 (2026-09-02): no reservation exists to convert into a dollar
    # charge any more -- an ambiguous failure marks the ledger inexact
    # instead of booking a guessed amount.
    assert state["current_session_cost_usd"] == 0
    assert state["current_daily_cost_usd"] == 0
    # The owner-facing sentence must match the recorded cause: an
    # out-of-credit suspension says so, everything else keeps the
    # unproven-cost wording (item 226).
    expected_phrase = (
        "out of credit" if expected_code == "provider_out_of_credit" else "no provable-zero-cost telemetry"
    )
    assert expected_phrase in notifier.messages[0]


@pytest.mark.parametrize("status_code", [429, 400, 401, 403, 404])
def test_known_zero_cost_status_codes_charge_nothing_and_do_not_trip(tmp_path, status_code):
    path = _db_path(tmp_path)
    notifier = _Notifier()
    circuit = LLMCostCircuitBreaker(path, _config(), notifier)
    error = _StatusCodeError(status_code, f"provider rejected ({status_code})")

    reservation = _authorize_and_fail(circuit, error, run_id=f"run-{status_code}")

    assert _is_known_zero_cost_failure(error) is True
    _assert_charged_nothing_and_did_not_trip(circuit, notifier, path, reservation)


def test_known_zero_cost_pre_send_connection_refused_charges_nothing(tmp_path):
    path = _db_path(tmp_path)
    notifier = _Notifier()
    circuit = LLMCostCircuitBreaker(path, _config(), notifier)
    # A bare, unwrapped connection-refused failure -- e.g. a local outbound
    # proxy/firewall reachability problem before the request ever left the
    # box. No status code was ever received because nothing was ever sent.
    error = ConnectionRefusedError("Connection refused")

    reservation = _authorize_and_fail(circuit, error, run_id="run-conn-refused")

    assert _is_known_zero_cost_failure(error) is True
    _assert_charged_nothing_and_did_not_trip(circuit, notifier, path, reservation)


def test_known_zero_cost_pre_send_dns_failure_wrapped_by_sdk_charges_nothing(tmp_path):
    path = _db_path(tmp_path)
    notifier = _Notifier()
    circuit = LLMCostCircuitBreaker(path, _config(), notifier)
    # DNS resolution failed before a connection attempt was even possible.
    # Both provider SDKs wrap the concrete httpx/socket cause into their own
    # `APIConnectionError` rather than raising it directly -- the top-level
    # wrapper's own name is ambiguous; the *cause* is what proves this was
    # pre-send.
    import socket

    error = _wrapped(
        "Connection error.",
        outer_name="APIConnectionError",
        cause=socket.gaierror("Name or service not known"),
    )

    reservation = _authorize_and_fail(circuit, error, run_id="run-dns")

    assert _is_known_zero_cost_failure(error) is True
    _assert_charged_nothing_and_did_not_trip(circuit, notifier, path, reservation)


def test_known_zero_cost_pre_send_tls_handshake_failure_charges_nothing(tmp_path):
    path = _db_path(tmp_path)
    notifier = _Notifier()
    circuit = LLMCostCircuitBreaker(path, _config(), notifier)
    ssl_error_cls = type("SSLError", (Exception,), {})
    error = _wrapped(
        "Connection error.",
        outer_name="APIConnectionError",
        cause=ssl_error_cls("[SSL] handshake failure"),
    )

    reservation = _authorize_and_fail(circuit, error, run_id="run-tls")

    assert _is_known_zero_cost_failure(error) is True
    _assert_charged_nothing_and_did_not_trip(circuit, notifier, path, reservation)


# 503 was in this list until 2026-09-22 and is now handled separately: a
# PRE-GENERATION 503 is provably free (see the 503 tests below), a mid-stream
# one is still ambiguous. 500/502 remain unconditionally ambiguous -- neither
# says anything about whether generation started.
@pytest.mark.parametrize("status_code", [500, 502, 504])
def test_5xx_keeps_conservative_charge_and_trip(tmp_path, status_code):
    path = _db_path(tmp_path)
    notifier = _Notifier()
    circuit = LLMCostCircuitBreaker(path, _config(), notifier)
    error = _StatusCodeError(status_code, f"provider error ({status_code})")

    reservation = _authorize_and_fail(circuit, error, run_id=f"run-{status_code}")

    assert _is_known_zero_cost_failure(error) is False
    _assert_charged_and_tripped(circuit, notifier, reservation)


def test_timeout_after_send_keeps_conservative_charge_and_trips(tmp_path):
    path = _db_path(tmp_path)
    notifier = _Notifier()
    circuit = LLMCostCircuitBreaker(path, _config(), notifier)
    # The request was already written to the socket; the provider stopped
    # responding while QAMC was waiting for output. Unlike ConnectTimeout
    # (never even connected), a ReadTimeout cannot prove nothing was billed.
    error = _wrapped(
        "Request timed out.",
        outer_name="APITimeoutError",
        cause=_wrapped("timed out", outer_name="ReadTimeout", cause=Exception("stub")),
    )

    reservation = _authorize_and_fail(circuit, error, run_id="run-read-timeout")

    assert _is_known_zero_cost_failure(error) is False
    _assert_charged_and_tripped(circuit, notifier, reservation)


def test_truncated_stream_keeps_conservative_charge_and_trips(tmp_path):
    path = _db_path(tmp_path)
    notifier = _Notifier()
    circuit = LLMCostCircuitBreaker(path, _config(), notifier)
    error = LLMStreamInterruptedError("stream cut mid-response, no usage telemetry")

    reservation = _authorize_and_fail(circuit, error, run_id="run-truncated")

    assert _is_known_zero_cost_failure(error) is False
    _assert_charged_and_tripped(circuit, notifier, reservation)


def test_unrecognized_failure_defaults_to_ambiguous_not_zero_cost(tmp_path):
    """Fail closed: an exception this classification has never seen (no
    status_code, no recognized pre-send transport name anywhere in its
    cause chain) must be treated exactly as conservatively as before --
    never assumed to be the cheaper, zero-cost outcome."""
    path = _db_path(tmp_path)
    notifier = _Notifier()
    circuit = LLMCostCircuitBreaker(path, _config(), notifier)
    error = RuntimeError("something the classifier has never heard of")

    reservation = _authorize_and_fail(circuit, error, run_id="run-unknown")

    assert _is_known_zero_cost_failure(error) is False
    _assert_charged_and_tripped(circuit, notifier, reservation)


# ============================================================================
# Defect 2, success half (2026-09-02): the same phantom charge on the branch
# where the retry SUCCEEDS. `fail_call` learned the zero-cost allow-list on
# 2026-08-31; `complete_call` did not, so a first attempt refused with a 429
# still had its conservative reserve added on top of the real cost of the
# response the retry produced. Measured twice in the live ledger at 9.6x and
# 5.4x the true cost of those calls.
# ============================================================================


def _retry_then_succeed(
    circuit,
    *,
    run_id,
    first_error,
    actual_cost,
    failed_attempt_errors,
    agent_name="tech_analyst",
    model="google/gemini-3.5-flash-lite",
):
    """One logical call: attempt 1 authorized and failed with `first_error`,
    attempt 2 authorized and settled at `actual_cost`. Mirrors what
    `BaseAgent._execute` does -- the reservation is never failed, because the
    call as a whole succeeded -- and returns the reservation."""
    circuit.activate_session(run_id, "morning")
    reservation = circuit.begin_call(
        agent_name=agent_name,
        model=model,
        system_prompt="s",
        user_message="u",
        max_output_tokens=100,
    )
    circuit.before_provider_attempt(reservation, model=reservation.model)
    # attempt 1 fails; the agent keeps the exception and retries rather than
    # calling fail_call, exactly as the retry loop does.
    circuit.before_provider_attempt(reservation, model=reservation.model)
    circuit.complete_call(
        reservation,
        actual_cost,
        actual_model=reservation.model,
        failed_attempt_errors=failed_attempt_errors,
    )
    return reservation


def _settled(path, reservation):
    """This logical call's settled cost. Item 14 (2026-09-02): there is no
    per-call reservation row any more, so read the session's running total
    instead -- exactly one call was made per run_id in every caller below,
    so the session total IS this call's settled cost."""
    with sqlite3.connect(path) as conn:
        return conn.execute(
            "SELECT actual_cost_usd FROM llm_budget_sessions WHERE run_id=?",
            (reservation.run_id,),
        ).fetchone()[0]


@pytest.mark.parametrize("status_code", [429, 400, 401, 403, 404])
def test_success_after_provably_free_attempt_is_charged_only_the_real_cost(
    tmp_path,
    status_code,
):
    path = _db_path(tmp_path)
    notifier = _Notifier()
    circuit = LLMCostCircuitBreaker(path, _config(), notifier)
    refusal = _StatusCodeError(status_code, f"provider rejected ({status_code})")

    reservation = _retry_then_succeed(
        circuit,
        run_id=f"run-retry-{status_code}",
        first_error=refusal,
        actual_cost=0.0014,
        failed_attempt_errors=[refusal],
    )

    assert _settled(path, reservation) == pytest.approx(0.0014)
    state = circuit.status()
    assert state["current_session_cost_usd"] == pytest.approx(0.0014)
    assert state["suspended"] is False
    # The day is exact: nothing about this call is estimated any more.
    with sqlite3.connect(path) as conn:
        assert (
            conn.execute(
                "SELECT costs_exact FROM llm_budget_sessions WHERE run_id=?",
                (f"run-retry-{status_code}",),
            ).fetchone()[0]
            == 1
        )


def test_success_after_ambiguous_attempt_keeps_spend_exact_and_does_not_latch(tmp_path):
    """A 500 might have billed for a stream that started and died.

    Item 14 deleted the reservation there was ever anything to add, so the
    ledger only ever holds the winner's real cost. That winner with a
    real cost_usd keeps the day exact — marking inexact was a second lie
    about spend. unknown_cost_rows stays 0 and legacy_unknown_cost does
    not latch. A fully-failed ambiguous call (`fail_call`) and a completed
    call with no telemetry still latch. Caps are not raised; settled
    spend is not erased.
    """
    path = _db_path(tmp_path)
    circuit = LLMCostCircuitBreaker(path, _config(), _Notifier())
    ambiguous = _StatusCodeError(500, "upstream exploded mid-stream")

    reservation = _retry_then_succeed(
        circuit,
        run_id="run-retry-500",
        first_error=ambiguous,
        actual_cost=0.0014,
        failed_attempt_errors=[ambiguous],
    )

    # Only the real, settled cost is ever booked -- never a guess.
    assert _settled(path, reservation) == pytest.approx(0.0014)
    with sqlite3.connect(path) as conn:
        day_row = conn.execute(
            "SELECT unknown_cost_rows, costs_exact FROM llm_budget_days",
        ).fetchone()
        assert (
            conn.execute(
                "SELECT costs_exact FROM llm_budget_sessions WHERE run_id=?",
                ("run-retry-500",),
            ).fetchone()[0]
            == 1
        )
        # Incrementing unknown_cost_rows here was the 2026-09-16 midday
        # wipe (event id=27) at ~$0.65 of $2.75. The winner's cost is
        # known, so the day stays exact.
        assert day_row[0] == 0
        assert day_row[1] == 1
    state = circuit.status()
    assert state["suspended"] is False
    assert state.get("trigger_code") != "legacy_unknown_cost"
    # Caps still bind on the booked total — the next call is authorized.
    circuit.begin_call(
        agent_name="tech_analyst",
        model="google/gemini-3.5-flash-lite",
        system_prompt="s",
        user_message="u",
        max_output_tokens=100,
    )


def test_one_ambiguous_attempt_among_free_ones_stays_exact(tmp_path):
    """A mixed retry still books only the winner's real cost.

    Ambiguity on a prior attempt is not a reason to mark the day inexact
    once the provider returned a number for the seat that succeeded.
    """
    path = _db_path(tmp_path)
    circuit = LLMCostCircuitBreaker(path, _config(), _Notifier())
    free = _StatusCodeError(429, "rate limited")
    ambiguous = _StatusCodeError(503, "upstream unavailable")

    reservation = _retry_then_succeed(
        circuit,
        run_id="run-retry-mixed",
        first_error=free,
        actual_cost=0.0014,
        failed_attempt_errors=[free, ambiguous],
    )

    assert _settled(path, reservation) == pytest.approx(0.0014)
    with sqlite3.connect(path) as conn:
        assert (
            conn.execute(
                "SELECT costs_exact FROM llm_budget_sessions WHERE run_id=?",
                ("run-retry-mixed",),
            ).fetchone()[0]
            == 1
        )
        assert (
            conn.execute(
                "SELECT unknown_cost_rows FROM llm_budget_days",
            ).fetchone()[0]
            == 0
        )
    assert circuit.status()["suspended"] is False


def test_caller_that_names_no_attempts_is_treated_as_exact(tmp_path):
    """Renamed from `..._keeps_the_old_conservative_charge`: the parameter
    is still optional, but with no reservation to inflate, a caller that
    cannot enumerate its attempts no longer gets a worse-case guess -- it
    gets exactly what the provider actually reported, same as a clean
    first-attempt success. There is nothing left to be conservative WITH."""
    path = _db_path(tmp_path)
    circuit = LLMCostCircuitBreaker(path, _config(), _Notifier())

    reservation = _retry_then_succeed(
        circuit,
        run_id="run-retry-silent",
        first_error=_StatusCodeError(429, "rate limited"),
        actual_cost=0.0014,
        failed_attempt_errors=None,
    )

    assert _settled(path, reservation) == pytest.approx(0.0014)


def test_clean_first_attempt_success_is_unaffected_by_the_new_parameter(tmp_path):
    """No failed attempts, no reserve to forgive: an empty list must settle
    at exactly the provider's figure, same as before."""
    path = _db_path(tmp_path)
    circuit = LLMCostCircuitBreaker(path, _config(), _Notifier())
    circuit.activate_session("run-clean", "morning")
    reservation = circuit.begin_call(
        agent_name="tech_analyst",
        model="google/gemini-3.5-flash-lite",
        system_prompt="s",
        user_message="u",
        max_output_tokens=100,
    )
    circuit.before_provider_attempt(reservation, model=reservation.model)
    circuit.complete_call(
        reservation,
        0.0014,
        actual_model=reservation.model,
        failed_attempt_errors=[],
    )

    assert _settled(path, reservation) == pytest.approx(0.0014)


def test_base_agent_reports_its_failed_attempts_to_the_circuit(tmp_path, monkeypatch):
    """End-to-end: the 2026-08-28 14:31 shape. A 429 on the first provider
    attempt, a clean success on the retry, and the ledger must show the real
    cost -- not the real cost plus a reserve for a request the provider
    refused before generating a token."""
    path = _db_path(tmp_path)
    circuit = LLMCostCircuitBreaker(path, _config(), _Notifier())
    circuit.activate_session("run-agent-retry", "morning")
    monkeypatch.setenv("QUANT_AGENT_MAX_RETRIES", "2")
    monkeypatch.setenv("QUANT_AGENT_RETRY_BASE_S", "0")

    usage = SimpleNamespace(input_tokens=100, output_tokens=10)
    ok = SimpleNamespace(
        content=[SimpleNamespace(text='{"ok": true}')],
        usage=usage,
        stop_reason="end_turn",
    )
    client = MagicMock()
    client.messages.create.side_effect = [_StatusCodeError(429, "slow down"), ok]
    with patch("anthropic.Anthropic", return_value=client):
        agent = _Agent(api_key="x", model="claude-sonnet-4-6", max_tokens=64)
        agent.set_cost_circuit(circuit)
        result = agent.run()

    # Pin the PATH before pinning the money. Without these the test can pass
    # for the wrong reason: a single-attempt success, or a failover onto a
    # dearer model whose own attempt reserve swallows the failed one, both
    # settle at the right number while exercising none of the fix.
    assert result.provider_requests == 2
    assert result.used_fallback is False
    assert result.model == "claude-sonnet-4-6"

    with sqlite3.connect(path) as conn:
        settled, exact, attempts = conn.execute(
            "SELECT actual_cost_usd, costs_exact, provider_attempts FROM llm_budget_sessions WHERE run_id=?",
            ("run-agent-retry",),
        ).fetchone()
    assert attempts == 2, "both attempts must have been authorized"
    # Item 14 (2026-09-02): no reservation exists to carry two attempts'
    # worth of reserve any more -- only the actual settled cost of the
    # response that came back is ever booked.
    assert exact == 1
    assert settled == pytest.approx(result.cost_usd, rel=1e-9)
    assert settled == pytest.approx(0.00045, rel=1e-9)


# ============================================================================
# Item 14 (OWNER-APPROVED 2026-09-02, docs/WORK.md): the runaway-loop
# backstop is now a plain per-session call COUNT (`max_calls_per_session`),
# independent of price -- replacing the reservation-era free-failure-session
# counting query, its cooling-off window, and the dollar-based per-mode/
# afternoon-reserve machinery that all existed to manage the deleted
# reservation's over-holding. A loop is defined by call count, and counting
# cannot be wrong about a rate the way a dollar estimate can.
# ============================================================================


def _settle_cheap_session(circuit, path, run_id, mode, cost):
    circuit.activate_session(run_id, mode)
    reservation = circuit.begin_call(
        agent_name="tech_analyst",
        model="google/gemini-3.5-flash-lite",
        system_prompt="s",
        user_message="u",
        max_output_tokens=100,
    )
    circuit.before_provider_attempt(reservation, model=reservation.model)
    circuit.complete_call(reservation, cost, actual_model=reservation.model)


def test_session_count_cap_triggers_on_the_next_call(tmp_path):
    """(c): a session that has already made N calls is stopped on call
    N+1 -- a runaway loop is caught by COUNT, with no dollar amount
    involved at all (every call here settles at exactly $0)."""
    path = _db_path(tmp_path)
    notifier = _Notifier()
    circuit = LLMCostCircuitBreaker(
        path,
        _config(max_calls_per_session=3),
        notifier,
    )
    circuit.activate_session("run-loop", "intra_check")
    for _ in range(3):
        reservation = circuit.begin_call(
            agent_name="tech_analyst",
            model="google/gemini-3.5-flash-lite",
            system_prompt="s",
            user_message="u",
            max_output_tokens=100,
        )
        circuit.before_provider_attempt(reservation, model=reservation.model)
        circuit.complete_call(reservation, 0.0, actual_model=reservation.model)

    with pytest.raises(PaidAnalysisSuspended) as excinfo:
        circuit.begin_call(
            agent_name="tech_analyst",
            model="google/gemini-3.5-flash-lite",
            system_prompt="s",
            user_message="u",
            max_output_tokens=100,
        )
    state = excinfo.value.state
    assert state["trigger_code"] == "session_call_count_limit"
    assert "3 call" in state["trigger_detail"]
    # Session-scoped and self-healing: a genuinely different session is not
    # affected by another session's runaway loop.
    circuit.activate_session("run-independent", "intra_check")
    reservation = circuit.begin_call(
        agent_name="tech_analyst",
        model="google/gemini-3.5-flash-lite",
        system_prompt="s",
        user_message="u",
        max_output_tokens=100,
    )
    assert reservation.reservation_id != "disabled"


def test_session_count_backstop_still_trips_a_genuine_runaway_loop(tmp_path):
    """A loop of sessions that spend essentially nothing (e.g. every
    attempt is a Defect-2 known-zero-cost rejection) would never trip a
    dollar-based check -- the call-count cap is what stops it, one call
    per session in this shape."""
    path = _db_path(tmp_path)
    notifier = _Notifier()
    circuit = LLMCostCircuitBreaker(
        path,
        _config(max_calls_per_session=1),
        notifier,
    )
    _settle_cheap_session(circuit, path, "intra_check-0", "intra_check", 0.0)

    circuit.activate_session("intra_check-0", "intra_check")
    with pytest.raises(PaidAnalysisSuspended) as excinfo:
        circuit.begin_call(
            agent_name="tech_analyst",
            model="google/gemini-3.5-flash-lite",
            system_prompt="s",
            user_message="u",
            max_output_tokens=100,
        )
    assert excinfo.value.state["trigger_code"] == "session_call_count_limit"


def _loose_config(**overrides):
    """A config with cost ceilings wide enough that only the specific
    check each test is exercising can fire."""
    values = dict(session_cost_limit_usd=10.0, daily_cost_limit_usd=10.0)
    values.update(overrides)
    return _config(**values)


def test_call_count_cap_is_atomic_across_process_objects(tmp_path):
    """Renamed/rewritten from `test_daily_reservation_is_atomic_across_
    process_objects`: that test raced two `begin_call`s against a shared
    DOLLAR reservation ceiling to prove the ``BEGIN IMMEDIATE`` write lock
    made admission atomic across process objects (no double-admit). Item
    14 (2026-09-02) deleted that dollar reservation, so there is nothing
    left to race a projected cost against -- but the SAME atomicity
    property still matters for the call-count cap (14c) that replaced it:
    two processes racing the last slot on one session must still produce
    exactly one winner, never two, and never zero."""
    path = _db_path(tmp_path)
    cfg = _config(max_calls_per_session=1)
    first = LLMCostCircuitBreaker(path, cfg, _Notifier())
    second = LLMCostCircuitBreaker(path, cfg, _Notifier())
    barrier = threading.Barrier(2)
    outcomes: list[str] = []

    def reserve(circuit):
        # ContextVars intentionally do not inherit into raw threads. Each
        # independent worker/process activates its own paid session context
        # for the SAME run_id -- exactly two systemd processes racing the
        # same session's call budget.
        circuit.activate_session("run-shared", "morning")
        barrier.wait()
        try:
            circuit.begin_call(
                agent_name="a",
                model="google/gemini-3.5-flash-lite",
                system_prompt="s",
                user_message="u",
                max_output_tokens=1_000,
            )
            outcomes.append("admitted")
        except PaidAnalysisSuspended:
            outcomes.append("blocked")

    threads = [
        threading.Thread(target=reserve, args=(first,)),
        threading.Thread(target=reserve, args=(second,)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sorted(outcomes) == ["admitted", "blocked"]
    with sqlite3.connect(path) as conn:
        logical_calls = conn.execute(
            "SELECT logical_calls FROM llm_budget_sessions WHERE run_id=?",
            ("run-shared",),
        ).fetchone()[0]
    assert logical_calls == 1


def test_provider_boundary_revalidates_ledger_after_reservation(tmp_path):
    """The name predates item 14, but the property it pins does not: a
    call authorized by `begin_call` can wait behind the provider semaphore
    while a DIFFERENT session settles real cost and the ledger becomes
    internally inconsistent. `before_provider_attempt` re-validates the
    whole day's accounting invariants immediately before network I/O
    rather than trusting whatever `begin_call` saw earlier -- unrelated to
    the (deleted) reservation table, this is about not sending a request
    on top of a damaged ledger."""
    path = _db_path(tmp_path)
    circuit = LLMCostCircuitBreaker(
        path,
        _config(session_cost_limit_usd=2.0, daily_cost_limit_usd=1.0),
        _Notifier(),
    )
    circuit.activate_session("run-waiting", "morning")
    waiting = circuit.begin_call(
        agent_name="waiting",
        model="google/gemini-3.5-flash-lite",
        system_prompt="s",
        user_message="u",
        max_output_tokens=100,
    )

    circuit.activate_session("run-settled", "midday")
    settled = circuit.begin_call(
        agent_name="settled",
        model="google/gemini-3.5-flash-lite",
        system_prompt="s",
        user_message="u",
        max_output_tokens=100,
    )
    circuit.before_provider_attempt(settled, model=settled.model)
    circuit.complete_call(settled, 0.10, actual_model=settled.model)

    # Simulate damage after the waiting call was authorized but before it
    # reaches the provider semaphore. The day and session ledgers now
    # disagree.
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE llm_budget_days SET incremental_cost_usd=0")

    with pytest.raises(
        RuntimeError,
        match="day/session settled-cost ledgers disagree",
    ):
        circuit.before_provider_attempt(waiting, model=waiting.model)
    # The waiting call never reached the network -- its in-process attempt
    # counter is untouched.
    assert waiting.attempt_count == 0


def test_durable_emergency_latch_survives_new_process_and_preserves_trigger(tmp_path):
    path = _db_path(tmp_path)
    cfg = _config()
    first_notifier = _Notifier()
    first = LLMCostCircuitBreaker(path, cfg, first_notifier)
    first.activate_session("run-original", "morning")
    first.mark_unavailable(
        RuntimeError("simulated accounting I/O failure"),
        run_id="run-original",
        mode="morning",
        agent_name="portfolio_manager",
        attempts=1,
    )

    marker = Path(f"{path}.llm-circuit-unavailable")
    assert marker.exists()
    original_payload = json.loads(marker.read_text())
    assert original_payload["run_id"] == "run-original"

    second = LLMCostCircuitBreaker(path, cfg, _Notifier())
    later_state = second.activate_session("run-later", "midday")
    assert later_state["run_id"] == "run-original"
    assert later_state["current_run_id"] == "run-later"
    with pytest.raises(PaidAnalysisSuspended):
        second.begin_call(
            agent_name="position_reviewer",
            model="google/gemini-3.5-flash-lite",
            system_prompt="s",
            user_message="u",
            max_output_tokens=100,
        )
    second.mark_unavailable(
        RuntimeError("later worker error"),
        run_id="run-later",
        mode="midday",
    )
    assert json.loads(marker.read_text())["run_id"] == "run-original"

    second.reset("accounting infrastructure repaired")
    assert not marker.exists()


def test_external_emergency_latch_blocks_inflight_response_at_completion(tmp_path):
    path = _db_path(tmp_path)
    cfg = _config()
    worker = LLMCostCircuitBreaker(path, cfg, _Notifier())
    worker.activate_session("run-inflight", "morning")
    reservation = worker.begin_call(
        agent_name="portfolio_manager",
        model="google/gemini-3.5-flash-lite",
        system_prompt="s",
        user_message="u",
        max_output_tokens=100,
    )
    worker.before_provider_attempt(reservation, model=reservation.model)

    other = LLMCostCircuitBreaker(path, cfg, _Notifier())
    other.mark_unavailable(
        RuntimeError("other process lost accounting"),
        run_id="run-other",
        mode="midday",
        attempts=0,
    )
    with pytest.raises(PaidAnalysisSuspended):
        worker.complete_call(reservation, 0.001, actual_model=reservation.model)

    # The in-flight response's real cost was never accounted -- another
    # process's accounting failure blocks it before any ledger write, so
    # the session shows no settled spend at all rather than a phantom
    # reservation charge.
    with sqlite3.connect(path) as conn:
        row = conn.execute(
            "SELECT status, actual_cost_usd FROM llm_budget_sessions WHERE run_id=?",
            (reservation.run_id,),
        ).fetchone()
    assert row == ("active", 0.0)


def test_emergency_alert_prefers_exact_persisted_attempt_count(tmp_path):
    path = _db_path(tmp_path)
    notifier = _Notifier()
    circuit = LLMCostCircuitBreaker(path, _config(), notifier)
    circuit.activate_session("run-snapshot", "morning")
    reservation = circuit.begin_call(
        agent_name="tech",
        model="google/gemini-3.5-flash-lite",
        system_prompt="s",
        user_message="u",
        max_output_tokens=100,
    )
    circuit.before_provider_attempt(reservation, model=reservation.model)
    circuit.complete_call(reservation, 0.001, actual_model=reservation.model)

    circuit.mark_unavailable(
        RuntimeError("post-call prerequisite failed"),
        run_id="run-snapshot",
        mode="morning",
        attempts=0,
    )
    assert "attempts: 1 provider attempt" in notifier.messages[-1]
    assert "at least 0" not in notifier.messages[-1]


def test_malformed_sidecar_metadata_still_constructs_clear_fail_closed_sentinel(tmp_path):
    path = _db_path(tmp_path)
    marker = Path(f"{path}.llm-circuit-unavailable")
    marker.write_text(
        json.dumps(
            {
                "recorded_at": "not-a-date",
                "error": "simulated marker",
                "run_id": "run-marker",
                "mode": "morning",
                "attempts": {"bad": "shape"},
                "attempts_exact": "yes",
                "session_cost_usd": "not-money",
                "daily_cost_usd": -1,
                "costs_exact": "yes",
            }
        )
    )
    notifier = _Notifier()
    circuit = LLMCostCircuitBreaker(path, _config(), notifier)

    state = circuit.activate_session("run-later", "midday")
    assert state["suspended"] is True
    assert state["run_id"] == "run-marker"
    assert state["session_attempts"] is None
    assert state["costs_available"] is False
    assert "cost: unavailable" in notifier.messages[0]


def test_missing_usage_latches_real_circuit_and_no_result_flows(tmp_path, monkeypatch):
    path = _db_path(tmp_path)
    circuit = LLMCostCircuitBreaker(path, _config(), _Notifier())
    circuit.activate_session("run-no-usage", "morning")
    monkeypatch.setenv("QUANT_AGENT_MAX_RETRIES", "1")

    client = MagicMock()
    chunk = SimpleNamespace(
        choices=[
            SimpleNamespace(
                delta=SimpleNamespace(content='{"ok": true}'),
                finish_reason="stop",
            )
        ],
        usage=None,
    )
    client.chat.completions.create.return_value = iter([chunk])
    with patch("openai.OpenAI", return_value=client):
        agent = _Agent(api_key="x", model="gpt-5.5", max_tokens=64)
        agent.set_cost_circuit(circuit)
        # The response is transport-valid, but accounting latches. A later
        # paid boundary is blocked; the current call is conservatively charged.
        result = agent.run()

    assert result.cost_usd is None
    state = circuit.status()
    assert state["trigger_code"] == "unknown_actual_cost"
    # Item 14 (2026-09-02): no reservation exists to charge for a request
    # with no usable telemetry any more -- the ledger stays exactly $0
    # while the trip itself still fails closed on the missing usage data.
    assert state["current_session_cost_usd"] == 0
    with pytest.raises(PaidAnalysisSuspended):
        circuit.require_paid_analysis("next_agent")


def test_corrupt_state_row_blocks_before_network_and_writes_durable_latch(
    tmp_path,
    monkeypatch,
):
    path = _db_path(tmp_path)
    circuit = LLMCostCircuitBreaker(path, _config(), _Notifier())
    circuit.activate_session("run-corrupt-state", "morning")
    with sqlite3.connect(path) as conn:
        conn.execute("DELETE FROM llm_circuit_state WHERE singleton=1")

    monkeypatch.setenv("QUANT_AGENT_MAX_RETRIES", "1")
    client = MagicMock()
    with patch("anthropic.Anthropic", return_value=client):
        agent = _Agent(api_key="x", model="claude-sonnet-4-6", max_tokens=64)
        agent.set_cost_circuit(circuit)
        with pytest.raises(PaidAnalysisSuspended):
            agent.run()

    client.messages.create.assert_not_called()
    assert Path(f"{path}.llm-circuit-unavailable").exists()


@pytest.mark.parametrize("missing", ["day", "session"])
def test_accounting_row_deleted_after_authorization_blocks_response(
    tmp_path,
    monkeypatch,
    missing,
):
    path = _db_path(tmp_path)
    circuit = LLMCostCircuitBreaker(path, _config(), _Notifier())
    run_id = f"run-missing-{missing}"
    circuit.activate_session(run_id, "morning")
    monkeypatch.setenv("QUANT_AGENT_MAX_RETRIES", "1")

    response = SimpleNamespace(
        content=[SimpleNamespace(text='{"ok": true}')],
        usage=SimpleNamespace(
            input_tokens=10,
            output_tokens=5,
            cache_creation_input_tokens=0,
            cache_read_input_tokens=0,
        ),
        stop_reason="end_turn",
    )
    client = MagicMock()

    def answer_then_corrupt(**_kwargs):
        with sqlite3.connect(path) as conn:
            if missing == "day":
                conn.execute("DELETE FROM llm_budget_days")
            else:
                conn.execute("DELETE FROM llm_budget_sessions WHERE run_id=?", (run_id,))
        return response

    client.messages.create.side_effect = answer_then_corrupt
    with patch("anthropic.Anthropic", return_value=client):
        agent = _Agent(api_key="x", model="claude-sonnet-4-6", max_tokens=64)
        agent.set_cost_circuit(circuit)
        with pytest.raises(PaidAnalysisSuspended):
            agent.run()

    assert client.messages.create.call_count == 1
    assert Path(f"{path}.llm-circuit-unavailable").exists()


@pytest.mark.parametrize(
    "table",
    [
        "llm_budget_days",
        "llm_budget_sessions",
        "llm_circuit_state",
        "llm_circuit_events",
    ],
)
def test_partial_breaker_schema_never_recreates_missing_accounting_as_empty(
    tmp_path,
    table,
):
    path = _db_path(tmp_path)
    with sqlite3.connect(path) as conn:
        conn.execute(f"DROP TABLE {table}")

    circuit = LLMCostCircuitBreaker(path, _config(), _Notifier())
    state = circuit.status()
    assert state["suspended"] is True
    assert state["available"] is False
    assert "schema is partial" in state["trigger_detail"]
    assert Path(f"{path}.llm-circuit-unavailable").exists()


@pytest.mark.parametrize(
    ("code", "scope"),
    [
        # Item 14 (2026-09-02): every projection-based trigger this list
        # used to carry ("projected_*_cost_limit", "provider_projected_*",
        # "outstanding_projected_*", "mode_daily_spend_limit",
        # "session_retry_attempt_limit") is gone along with the reservation
        # layer that computed them. "daily_cost_limit"/"session_cost_limit"
        # now fire on REAL SETTLED spend only; "session_call_count_limit"
        # is the new call-count runaway-loop backstop (item 14c).
        ("daily_cost_limit", "day"),
        ("session_cost_limit", "session"),
        ("session_call_count_limit", "session"),
        # Defect 5 (2026-08-31, unaffected by item 14): "provider_attempt_
        # limit" stays session-scoped. It bounds attempts within ONE call,
        # independent of the item-14c call-count backstop -- see the NOTE
        # by _SESSION_QUOTA_TRIGGERS in src/cost_circuit.py.
        ("provider_attempt_limit", "session"),
        ("legacy_unknown_cost", "hard"),
        ("unknown_model_price", "hard"),
        ("unknown_actual_cost", "hard"),
        ("failed_call_unknown_cost", "hard"),
        # Unrecognized/removed codes fail closed to "hard" by design --
        # includes every reservation-era code deleted by item 14.
        ("expired_attempted_reservation", "hard"),
        ("cross_day_started_reservation", "hard"),
        ("mode_daily_spend_limit", "hard"),
        ("projected_daily_cost_limit", "hard"),
        ("projected_session_cost_limit", "hard"),
    ],
)
def test_trigger_scope_classification_is_explicit(code, scope):
    assert _trigger_scope(code) == scope


@pytest.mark.parametrize("code", [None, "", "future_daily_limit", {"bad": "shape"}])
def test_unknown_trigger_scope_fails_closed_hard(code):
    assert _trigger_scope(code) == "hard"


def test_day_quota_hold_recovers_once_on_next_et_day(tmp_path, monkeypatch):
    clock = {"value": ("2099-01-01", "2099-01-01 05:00:00", "2099-01-02 04:59:59")}
    monkeypatch.setattr(
        "src.cost_circuit._et_day_and_utc_bounds",
        lambda now=None: clock["value"],
    )
    path = _db_path(tmp_path)
    notifier = _Notifier()
    circuit = LLMCostCircuitBreaker(
        path,
        _config(session_cost_limit_usd=2.0, daily_cost_limit_usd=1.0),
        notifier,
    )
    circuit.activate_session("run-old-day", "morning")
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE llm_budget_sessions SET actual_cost_usd=1.0 WHERE run_id='run-old-day'")
        conn.execute("UPDATE llm_budget_days SET incremental_cost_usd=1.0 WHERE day='2099-01-01'")
    circuit.enforce_current_limits("test_limit")
    assert circuit.status()["suspended"] is True
    assert circuit.status()["hold_scope"] == "day"
    assert len(notifier.messages) == 1

    clock["value"] = (
        "2099-01-02",
        "2099-01-02 05:00:00",
        "2099-01-03 04:59:59",
    )
    state = circuit.activate_session("run-new-day", "morning")
    assert state["suspended"] is False
    assert state["current_daily_cost_usd"] == 0
    assert len(notifier.messages) == 2
    assert notifier.messages[-1].startswith("🟢 QAMC PAID ANALYSIS REARMED")
    with sqlite3.connect(path) as conn:
        old_day = conn.execute(
            "SELECT baseline_cost_usd + incremental_cost_usd FROM llm_budget_days WHERE day='2099-01-01'"
        ).fetchone()[0]
        new_day = conn.execute(
            "SELECT baseline_cost_usd + incremental_cost_usd FROM llm_budget_days WHERE day='2099-01-02'"
        ).fetchone()[0]
        recoveries = conn.execute(
            "SELECT COUNT(*) FROM llm_circuit_events WHERE event_type='quota_rearmed'"
        ).fetchone()[0]
    assert old_day == pytest.approx(1.0)
    assert new_day == pytest.approx(0.0)
    assert recoveries == 1
    circuit.status()
    assert len(notifier.messages) == 2


@pytest.mark.parametrize("pending_state", [0, -1])
def test_rollover_delivers_pending_quota_trip_before_recovery(
    tmp_path,
    monkeypatch,
    pending_state,
):
    clock = {"value": ("2099-01-01", "2099-01-01 05:00:00", "2099-01-02 04:59:59")}
    monkeypatch.setattr(
        "src.cost_circuit._et_day_and_utc_bounds",
        lambda now=None: clock["value"],
    )
    path = _db_path(tmp_path)
    notifier = _Notifier()
    circuit = LLMCostCircuitBreaker(
        path,
        _config(session_cost_limit_usd=2.0, daily_cost_limit_usd=1.0),
        notifier,
    )
    circuit.activate_session("run-pending-alert", "morning")
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE llm_budget_sessions SET actual_cost_usd=1.0 WHERE run_id='run-pending-alert'")
        conn.execute("UPDATE llm_budget_days SET incremental_cost_usd=1.0 WHERE day='2099-01-01'")
    circuit.enforce_current_limits("test_limit")
    assert len(notifier.messages) == 1
    notifier.messages.clear()
    with sqlite3.connect(path) as conn:
        conn.execute(
            "UPDATE llm_quota_holds SET alert_state=?, alert_updated_at=?",
            (pending_state, "2000-01-01 00:00:00"),
        )

    clock["value"] = (
        "2099-01-02",
        "2099-01-02 05:00:00",
        "2099-01-03 04:59:59",
    )
    state = circuit.activate_session("run-after-pending-alert", "morning")

    assert state["suspended"] is False
    assert len(notifier.messages) == 2
    assert notifier.messages[0].startswith("🟠 QAMC PAID ANALYSIS QUOTA HOLD")
    assert notifier.messages[1].startswith("🟢 QAMC PAID ANALYSIS REARMED")
    circuit.status()
    assert len(notifier.messages) == 2


def test_rollover_waits_for_fresh_trip_alert_lease_before_recovery(
    tmp_path,
    monkeypatch,
):
    clock = {"value": ("2099-01-01", "2099-01-01 05:00:00", "2099-01-02 04:59:59")}
    monkeypatch.setattr(
        "src.cost_circuit._et_day_and_utc_bounds",
        lambda now=None: clock["value"],
    )
    path = _db_path(tmp_path)
    notifier = _Notifier()
    circuit = LLMCostCircuitBreaker(
        path,
        _config(session_cost_limit_usd=2.0, daily_cost_limit_usd=1.0),
        notifier,
    )
    circuit.activate_session("run-fresh-lease", "morning")
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE llm_budget_sessions SET actual_cost_usd=1.0 WHERE run_id='run-fresh-lease'")
        conn.execute("UPDATE llm_budget_days SET incremental_cost_usd=1.0 WHERE day='2099-01-01'")
    circuit.enforce_current_limits("test_limit")
    notifier.messages.clear()
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE llm_quota_holds SET alert_state=-1, alert_updated_at=datetime('now')")

    clock["value"] = (
        "2099-01-02",
        "2099-01-02 05:00:00",
        "2099-01-03 04:59:59",
    )
    state = circuit.activate_session("run-after-fresh-lease", "morning")
    assert state["suspended"] is False
    assert notifier.messages == []

    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE llm_quota_holds SET alert_updated_at='2000-01-01 00:00:00'")
    circuit.status()
    assert len(notifier.messages) == 2
    assert notifier.messages[0].startswith("🟠 QAMC PAID ANALYSIS QUOTA HOLD")
    assert notifier.messages[1].startswith("🟢 QAMC PAID ANALYSIS REARMED")


def test_clock_regression_with_future_quota_hold_fails_closed(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "src.cost_circuit._et_day_and_utc_bounds",
        lambda now=None: (
            "2099-01-01",
            "2099-01-01 05:00:00",
            "2099-01-02 04:59:59",
        ),
    )
    path = _db_path(tmp_path)
    circuit = LLMCostCircuitBreaker(
        path,
        _config(session_cost_limit_usd=2.0, daily_cost_limit_usd=1.0),
        _Notifier(),
    )
    circuit.activate_session("run-future-hold", "morning")
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE llm_budget_sessions SET actual_cost_usd=1.0 WHERE run_id='run-future-hold'")
        conn.execute("UPDATE llm_budget_days SET incremental_cost_usd=1.0 WHERE day='2099-01-01'")
    circuit.enforce_current_limits("test_limit")
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE llm_quota_holds SET day='2099-01-02', scope_key='2099-01-02'")

    state = circuit.activate_session("run-clock-regressed", "morning")

    assert state["suspended"] is True
    assert state["suspension_class"] == "hard"
    assert state["trigger_code"] == "non_monotonic_quota_hold_day"
    assert state["requires_operator_reset"] is True
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT active FROM llm_quota_holds").fetchone()[0] == 1


def test_hard_latch_survives_et_rollover_until_audited_reset(tmp_path, monkeypatch):
    """The hard-latch scenario used to be `begin_call` on an unpriced
    model (`unknown_model_price`) -- gone along with the reservation layer
    that priced calls at all. `unknown_actual_cost` (no usable cost/token
    telemetry at completion) is a different, still-real hard trigger that
    exercises the identical durability property: unlike a quota hold, it
    does NOT clear on ET rollover and needs an audited `reset()`."""
    clock = {"value": ("2099-01-01", "2099-01-01 05:00:00", "2099-01-02 04:59:59")}
    monkeypatch.setattr(
        "src.cost_circuit._et_day_and_utc_bounds",
        lambda now=None: clock["value"],
    )
    notifier = _Notifier()
    circuit = LLMCostCircuitBreaker(_db_path(tmp_path), _config(), notifier)
    circuit.activate_session("run-hard", "morning")
    reservation = circuit.begin_call(
        agent_name="portfolio_manager",
        model="openai/gpt-5.5",
        system_prompt="s",
        user_message="u",
        max_output_tokens=100,
    )
    circuit.before_provider_attempt(reservation, model=reservation.model)
    circuit.complete_call(reservation, None)  # no usable telemetry -> hard latch
    assert circuit.status()["suspension_class"] == "hard"
    assert circuit.status()["requires_operator_reset"] is True

    clock["value"] = (
        "2099-01-02",
        "2099-01-02 05:00:00",
        "2099-01-03 04:59:59",
    )
    state = circuit.activate_session("run-next-day", "morning")
    assert state["suspended"] is True
    assert state["trigger_code"] == "unknown_actual_cost"
    assert len(notifier.messages) == 1
    circuit.reset("operator verified spend and the missing telemetry cause")
    assert circuit.status()["suspended"] is False


def test_old_daily_latch_migrates_without_early_clear_or_duplicate_alert(
    tmp_path,
    monkeypatch,
):
    clock = {"value": ("2099-01-01", "2099-01-01 05:00:00", "2099-01-02 04:59:59")}
    monkeypatch.setattr(
        "src.cost_circuit._et_day_and_utc_bounds",
        lambda now=None: clock["value"],
    )
    path = _db_path(tmp_path)
    with sqlite3.connect(path) as conn:
        conn.execute("DROP TABLE llm_quota_holds")
        conn.execute(
            "INSERT INTO llm_budget_days(day, baseline_cost_usd, seeded_at, updated_at) "
            "VALUES ('2099-01-01', 4.211481, ?, ?) "
            "ON CONFLICT(day) DO UPDATE SET baseline_cost_usd=excluded.baseline_cost_usd",
            (trading_calendar.sql_utc_stamp(), trading_calendar.sql_utc_stamp()),
        )
        conn.execute(
            "INSERT OR REPLACE INTO llm_budget_sessions(run_id, day, mode, started_at, updated_at) "
            "VALUES ('deployment-legacy', '2099-01-01', 'operator', ?, ?)",
            (trading_calendar.sql_utc_stamp(), trading_calendar.sql_utc_stamp()),
        )
        conn.execute(
            "UPDATE llm_circuit_state SET suspended=1, "
            "trigger_code='daily_cost_limit', "
            "trigger_detail='daily LLM spend $4.2115 reached safe limit $1.50', "
            "run_id='deployment-legacy', mode='operator', agent_name='session_start', "
            "daily_cost_usd=4.211481, session_limit_usd=.90, daily_limit_usd=1.50, "
            "suspended_at='2099-01-01 18:11:04', alert_state=1"
        )
    notifier = _Notifier()
    circuit = LLMCostCircuitBreaker(path, _config(daily_cost_limit_usd=1.50), notifier)
    circuit.activate_session("same-day-check", "morning")
    state = circuit.status()
    assert state["suspended"] is True
    assert state["suspension_class"] == "quota"
    assert state["daily_cost_usd"] == pytest.approx(4.211481)
    assert notifier.messages == []

    clock["value"] = (
        "2099-01-02",
        "2099-01-02 05:00:00",
        "2099-01-03 04:59:59",
    )
    circuit.activate_session("next-day-check", "morning")
    assert circuit.status()["suspended"] is False
    assert len(notifier.messages) == 1
    assert "REARMED" in notifier.messages[0]


def test_concurrent_rollover_has_one_recovery_event_and_alert(tmp_path, monkeypatch):
    clock = {"value": ("2099-01-01", "2099-01-01 05:00:00", "2099-01-02 04:59:59")}
    monkeypatch.setattr(
        "src.cost_circuit._et_day_and_utc_bounds",
        lambda now=None: clock["value"],
    )
    path = _db_path(tmp_path)
    first_notifier = _Notifier()
    cfg = _config(session_cost_limit_usd=2.0, daily_cost_limit_usd=1.0)
    first = LLMCostCircuitBreaker(path, cfg, first_notifier)
    first.activate_session("run-old", "morning")
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE llm_budget_sessions SET actual_cost_usd=1.0 WHERE run_id='run-old'")
        conn.execute("UPDATE llm_budget_days SET incremental_cost_usd=1.0 WHERE day='2099-01-01'")
    first.enforce_current_limits("test_limit")
    second_notifier = _Notifier()
    second = LLMCostCircuitBreaker(path, cfg, second_notifier)

    clock["value"] = (
        "2099-01-02",
        "2099-01-02 05:00:00",
        "2099-01-03 04:59:59",
    )
    barrier = threading.Barrier(2)
    states: list[bool] = []

    def activate(circuit, run_id):
        barrier.wait()
        states.append(bool(circuit.activate_session(run_id, "morning")["suspended"]))

    threads = [
        threading.Thread(target=activate, args=(first, "run-new-a")),
        threading.Thread(target=activate, args=(second, "run-new-b")),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert states == [False, False]
    recovery_messages = [
        message for message in first_notifier.messages + second_notifier.messages if "PAID ANALYSIS REARMED" in message
    ]
    assert len(recovery_messages) == 1
    with sqlite3.connect(path) as conn:
        assert (
            conn.execute("SELECT COUNT(*) FROM llm_circuit_events WHERE event_type='quota_rearmed'").fetchone()[0] == 1
        )


def test_legacy_quota_without_day_provenance_remains_hard(tmp_path):
    path = _db_path(tmp_path)
    with sqlite3.connect(path) as conn:
        conn.execute("DROP TABLE llm_quota_holds")
        conn.execute(
            "UPDATE llm_circuit_state SET suspended=1, "
            "trigger_code='daily_cost_limit', trigger_detail='legacy ambiguous hold', "
            "run_id='missing-run', mode='operator', agent_name='migration', "
            "daily_cost_usd=2, daily_limit_usd=1.5, alert_state=1"
        )
    circuit = LLMCostCircuitBreaker(path, _config(), _Notifier())
    state = circuit.status()
    assert state["suspended"] is True
    assert state["available"] is False
    assert state["trigger_code"] == "circuit_infrastructure_unavailable"
    assert Path(f"{path}.llm-circuit-unavailable").exists()


# === Pricing-staleness SPOF fix (2026-08-28) ===
#
# Before this fix, `cost_table.refresh_openrouter_pricing()` accepted a
# cached OpenRouter rate ONLY under 24h old. Past that boundary, both
# `TradingPipeline.__init__` (src/pipeline.py) and `activate_paid_call_
# session` above respond to a False return with `breaker.mark_unavailable`
# -- the durable, cross-process latch that only `LLMCostCircuitBreaker.
# reset()` (operator-only, reason mandatory) can clear. Because the cache
# is rewritten only when a fetch happens, and a fetch only happens once the
# cache is already stale, one openrouter.ai outage overlapping the first
# session past the 24h mark could stop the desk until a human intervened --
# see `test_mandatory_openrouter_refresh_rejects_stale_cache_when_network_
# is_down` in tests/test_cost_table.py, which reproduces exactly that.
#
# These tests exercise the fix at the level a real deployment actually
# hits it: `activate_paid_call_session`, and `begin_call`'s persisted
# reservation amount.


def test_activate_paid_call_session_proceeds_on_stale_within_grace_pricing(tmp_path, monkeypatch):
    """The actual SPOF: before this fix, a stale-past-24h OpenRouter cache
    with the catalog unreachable made activate_paid_call_session latch via
    mark_unavailable on every call. Within the configured grace window it
    must instead proceed -- no suspension, and a real call can still be
    reserved -- exactly the "scoped/self-healing, not a human-reset latch"
    behaviour the fix is required to produce."""
    import os
    import time
    from src import cost_table

    cache = tmp_path / "openrouter_pricing_cache.json"
    rates = {model: dict(value) for model, value in cost_table._PRICING_OPENROUTER.items()}
    cache.write_text(json.dumps(rates))
    stale_age_s = cost_table._CACHE_MAX_AGE_SECONDS + 6 * 3600  # 6h into a 24h grace window
    old = time.time() - stale_age_s
    os.utime(cache, (old, old))
    monkeypatch.setattr(cost_table, "_OPENROUTER_CACHE_PATH", cache)
    monkeypatch.setattr(cost_table, "_fetch_openrouter_pricing", lambda: None)

    app_config = SimpleNamespace(
        llm_cost_circuit=_loose_config(
            openrouter_pricing_grace_period_hours=24.0,
            openrouter_pricing_stale_multiplier_max=1.5,
        ),
    )
    circuit = activate_paid_call_session(
        app_config,
        run_id="run-stale-grace",
        mode="morning",
        notifier=_Notifier(),
        db_path=_db_path(tmp_path),
    )

    state = circuit.status()
    assert state["suspended"] is False
    assert not Path(f"{circuit.db_path}.llm-circuit-unavailable").exists()

    reservation = circuit.begin_call(
        agent_name="portfolio_manager",
        model="openai/gpt-5.5",
        system_prompt="p",
        user_message="m",
        max_output_tokens=1000,
    )
    assert reservation.reservation_id != "disabled"


def test_activate_paid_call_session_still_latches_beyond_grace_pricing(tmp_path, monkeypatch):
    """The grace window is a bound, not a blank check: a cache older than
    24h + the configured grace, with the catalog unreachable, is genuinely
    unknown pricing and must still latch exactly as it did before this fix
    -- the global operator-reset latch is the CORRECT response to truly
    unbounded cost, and this proves the fix didn't accidentally remove it."""
    import os
    import time
    from src import cost_table

    cache = tmp_path / "openrouter_pricing_cache.json"
    rates = {model: dict(value) for model, value in cost_table._PRICING_OPENROUTER.items()}
    cache.write_text(json.dumps(rates))
    beyond_grace_s = cost_table._CACHE_MAX_AGE_SECONDS + 24 * 3600 + 3600  # 1h past the grace edge
    old = time.time() - beyond_grace_s
    os.utime(cache, (old, old))
    monkeypatch.setattr(cost_table, "_OPENROUTER_CACHE_PATH", cache)
    monkeypatch.setattr(cost_table, "_fetch_openrouter_pricing", lambda: None)

    app_config = SimpleNamespace(
        llm_cost_circuit=_loose_config(
            openrouter_pricing_grace_period_hours=24.0,
            openrouter_pricing_stale_multiplier_max=1.5,
        ),
    )
    circuit = activate_paid_call_session(
        app_config,
        run_id="run-beyond-grace",
        mode="morning",
        notifier=_Notifier(),
        db_path=_db_path(tmp_path),
    )

    state = circuit.status()
    assert state["suspended"] is True
    assert state["available"] is False
    assert Path(f"{circuit.db_path}.llm-circuit-unavailable").exists()


# ============================================================================
# Item 14 (OWNER-APPROVED 2026-09-02, docs/WORK.md): the three behaviors the
# owner's replacement design specifies, pinned directly.
# ============================================================================


def test_settled_cost_cap_triggers_only_after_a_call_actually_pushes_it_over(tmp_path):
    """(b): the desk stops when REAL SETTLED cost -- the actual amount a
    completed call's provider response reported -- pushes today's spend
    over the daily cap. No projection is involved: the call that trips it
    is authorized normally (its own dollar amount is unknown until it
    settles) and only the ACTUAL reported cost, applied by `complete_call`,
    crosses the line."""
    path = _db_path(tmp_path)
    notifier = _Notifier()
    circuit = LLMCostCircuitBreaker(
        path,
        _config(session_cost_limit_usd=5.0, daily_cost_limit_usd=1.00),
        notifier,
    )
    circuit.activate_session("run-daily-cap", "morning")
    reservation = circuit.begin_call(
        agent_name="portfolio_manager",
        model="openai/gpt-5.5",
        system_prompt="s",
        user_message="u",
        max_output_tokens=16_000,
    )
    circuit.before_provider_attempt(reservation, model=reservation.model)
    # The call itself was never blocked going in -- only once its ACTUAL
    # returned cost lands does the cap trip.
    assert circuit.status()["suspended"] is False
    circuit.complete_call(reservation, 1.20, actual_model=reservation.model)

    state = circuit.status()
    assert state["suspended"] is True
    assert state["trigger_code"] == "daily_cost_limit"
    assert state["current_daily_cost_usd"] == pytest.approx(1.20)

    # And it stops the DESK, not just this one caller: the very next call,
    # from any session, is refused before it can even be authorized.
    circuit.activate_session("run-next", "midday")
    with pytest.raises(PaidAnalysisSuspended):
        circuit.begin_call(
            agent_name="tech_analyst",
            model="google/gemini-3.5-flash-lite",
            system_prompt="s",
            user_message="u",
            max_output_tokens=100,
        )


def test_call_count_cap_triggers_correctly(tmp_path):
    """(c): a session that exceeds `max_calls_per_session` calls is
    stopped -- a runaway-loop guard by COUNT, independent of price. Every
    call here settles at exactly $0, so a dollar-based check could never
    catch this; only counting can."""
    path = _db_path(tmp_path)
    notifier = _Notifier()
    circuit = LLMCostCircuitBreaker(
        path,
        _config(max_calls_per_session=5),
        notifier,
    )
    circuit.activate_session("run-loop", "intra_check")
    for _ in range(5):
        reservation = circuit.begin_call(
            agent_name="tech_analyst",
            model="google/gemini-3.5-flash-lite",
            system_prompt="s",
            user_message="u",
            max_output_tokens=100,
        )
        circuit.before_provider_attempt(reservation, model=reservation.model)
        circuit.complete_call(reservation, 0.0, actual_model=reservation.model)

    with pytest.raises(PaidAnalysisSuspended) as excinfo:
        circuit.begin_call(
            agent_name="tech_analyst",
            model="google/gemini-3.5-flash-lite",
            system_prompt="s",
            user_message="u",
            max_output_tokens=100,
        )
    assert excinfo.value.state["trigger_code"] == "session_call_count_limit"
    assert circuit.status()["current_daily_cost_usd"] == 0
    with sqlite3.connect(path) as conn:
        logical_calls = conn.execute(
            "SELECT logical_calls FROM llm_budget_sessions WHERE run_id=?",
            ("run-loop",),
        ).fetchone()[0]
    assert logical_calls == 5


def test_a_call_is_no_longer_preemptively_blocked_by_an_unspent_reservation(tmp_path):
    """The actual defect item 14 replaces, pinned directly. Adapted from
    the deleted `test_projected_session_spend_blocks_before_provider_
    request`, which pinned the OLD, now-removed behavior: `begin_call`
    computed a conservative worst-case dollar RESERVATION from the prompt
    size and `max_output_tokens` alone, and refused the call outright if
    that estimate alone would have projected session/daily spend over a
    ("reserved-exposure") ceiling -- even though no money had been spent
    yet. Measured: real calls settled at a median 0.38x of that estimate,
    so the desk was stopped by money that was never spent (docs/WORK.md
    item 14).

    That old assertion is now WRONG on its own terms. Pinned here instead:
    a call with a large prompt and a large `max_output_tokens` ceiling --
    priced by the OLD formula alone at far more than the session limit --
    is authorized without hesitation, because nothing is estimated or
    reserved before the call. Only what the call actually, eventually
    settles at can ever stop the desk."""
    path = _db_path(tmp_path)
    notifier = _Notifier()
    # A limit an old worst-case reservation for this prompt/ceiling would
    # have blown through instantly: ~$1.86 for a ~259KB prompt at
    # openai/gpt-5.5 rates under the old byte=token/full-ceiling formula
    # (see the 2026-08-28 09:32 ET incident in docs/WORK.md item 14),
    # against a cap two orders of magnitude smaller.
    circuit = LLMCostCircuitBreaker(
        path,
        _config(session_cost_limit_usd=0.01, daily_cost_limit_usd=1.0),
        notifier,
    )
    circuit.activate_session("run-not-preemptively-blocked", "morning")

    # No PaidAnalysisSuspended here: item (b)'s settled-cost check has
    # nothing to compare against yet (this session has spent exactly $0
    # so far), and there is no reservation left to project a worst case
    # from at all.
    reservation = circuit.begin_call(
        agent_name="portfolio_manager",
        model="openai/gpt-5.5",
        system_prompt="S" * 48_000,
        user_message="U" * 210_744,
        max_output_tokens=16_000,
    )
    assert reservation.reservation_id != "disabled"
    assert circuit.status()["suspended"] is False
    assert notifier.messages == []

    # The real cap still bites once real money is actually reported.
    circuit.before_provider_attempt(reservation, model=reservation.model)
    circuit.complete_call(reservation, 0.02, actual_model=reservation.model)
    assert circuit.status()["trigger_code"] == "session_cost_limit"


# === docs/WORK.md item 17(a): infra fault vs. real breach must not share an
# outcome. "I cannot read the budget" (a transient I/O/DB-open failure)
# retries with backoff before latching; "I am over budget" (a real, measured
# breach) still latches immediately, exactly as before. ===


class _FailingNotifier:
    """A notifier whose `send` always fails -- simulates a dead Telegram
    channel for item 17(b)'s durable-alert-retry tests below."""

    def __init__(self):
        self.attempts = 0

    def send(self, message: str, **_kwargs) -> bool:
        self.attempts += 1
        return False


def test_transient_infra_fault_retries_with_backoff_and_does_not_latch(
    tmp_path,
    monkeypatch,
):
    path = _db_path(tmp_path)
    cfg = _config(
        infra_fault_max_retries=2,
        infra_fault_retry_backoff_base_s=0.01,
        infra_fault_retry_backoff_max_s=0.01,
        infra_fault_retry_backoff_jitter_s=0.0,
    )
    notifier = _Notifier()
    circuit = LLMCostCircuitBreaker(path, cfg, notifier)

    real_connect = circuit._connect
    calls = {"n": 0}

    def flaky_connect():
        calls["n"] += 1
        if calls["n"] == 1:
            raise sqlite3.OperationalError("disk I/O error")
        return real_connect()

    monkeypatch.setattr(circuit, "_connect", flaky_connect)
    sleeps: list[float] = []
    monkeypatch.setattr("src.cost_circuit.time.sleep", lambda s: sleeps.append(s))

    state = circuit.activate_session("run-transient", "morning")

    assert state["suspended"] is False
    assert state.get("available", True) is not False
    assert notifier.messages == []  # never escalated to the durable latch/alert
    assert not Path(f"{path}.llm-circuit-unavailable").exists()
    assert len(sleeps) == 1  # exactly one backoff sleep before the retry succeeded


def test_persistent_infra_fault_exhausts_retries_then_latches_durably(
    tmp_path,
    monkeypatch,
):
    path = _db_path(tmp_path)
    cfg = _config(
        infra_fault_max_retries=2,
        infra_fault_retry_backoff_base_s=0.01,
        infra_fault_retry_backoff_max_s=0.01,
        infra_fault_retry_backoff_jitter_s=0.0,
    )
    notifier = _Notifier()
    circuit = LLMCostCircuitBreaker(path, cfg, notifier)

    def always_broken():
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(circuit, "_connect", always_broken)
    sleeps: list[float] = []
    monkeypatch.setattr("src.cost_circuit.time.sleep", lambda s: sleeps.append(s))

    state = circuit.activate_session("run-broken", "morning")

    assert state["suspended"] is True
    assert state["available"] is False
    # infra_fault_max_retries=2 -> 3 total attempts, 2 backoff sleeps between them.
    assert len(sleeps) == 2

    marker = Path(f"{path}.llm-circuit-unavailable")
    assert marker.exists()
    payload = json.loads(marker.read_text())
    assert "OperationalError" in payload["error"]
    assert notifier.messages  # the operator WAS told, unlike a real breach's silence path

    with pytest.raises(PaidAnalysisSuspended):
        circuit.require_paid_analysis("portfolio_manager")


def test_real_spend_breach_still_latches_immediately_no_retry_no_file_latch(
    tmp_path,
    monkeypatch,
):
    path = _db_path(tmp_path)
    cfg = _config(
        session_cost_limit_usd=0.01,
        daily_cost_limit_usd=1.0,
        infra_fault_max_retries=5,
    )
    notifier = _Notifier()
    circuit = LLMCostCircuitBreaker(path, cfg, notifier)
    circuit.activate_session("run-breach", "morning")

    sleeps: list[float] = []
    monkeypatch.setattr("src.cost_circuit.time.sleep", lambda s: sleeps.append(s))

    reservation = circuit.begin_call(
        agent_name="portfolio_manager",
        model="google/gemini-3.5-flash-lite",
        system_prompt="s",
        user_message="u",
        max_output_tokens=100,
    )
    circuit.before_provider_attempt(reservation, model=reservation.model)
    circuit.complete_call(reservation, 0.02, actual_model=reservation.model)

    assert circuit.status()["trigger_code"] == "session_cost_limit"
    assert sleeps == []  # a real, measured breach is never retried
    # A real breach latches via the in-DB `llm_circuit_state` row, not the
    # cross-process file marker item 17(a) adds retry/backoff in front of.
    assert not Path(f"{path}.llm-circuit-unavailable").exists()
    with pytest.raises(PaidAnalysisSuspended):
        circuit.require_paid_analysis("portfolio_manager")


# === docs/WORK.md item 17(b): a failed latch alert must be persisted and
# retried, not silently dropped forever. ===


def test_failed_latch_alert_is_persisted_and_retried_on_a_later_run(tmp_path):
    path = _db_path(tmp_path)
    cfg = _config()
    marker = Path(f"{path}.llm-circuit-unavailable")

    failing_notifier = _FailingNotifier()
    first = LLMCostCircuitBreaker(path, cfg, failing_notifier)
    first.mark_unavailable(
        RuntimeError("simulated accounting I/O failure"),
        run_id="run-a",
        mode="morning",
        agent_name="portfolio_manager",
    )
    assert failing_notifier.attempts == 1
    payload = json.loads(marker.read_text())
    assert payload["alert_delivered"] is False
    assert payload["alert_attempts"] == 1

    # A later process (a fresh breaker instance -- the same boundary as a
    # systemd restart) re-reads the durable marker and retries the send.
    ok_notifier = _Notifier()
    second = LLMCostCircuitBreaker(path, cfg, ok_notifier)
    second.activate_session("run-b", "midday")

    assert ok_notifier.messages  # delivered this time
    payload = json.loads(marker.read_text())
    assert payload["alert_delivered"] is True
    assert payload["alert_attempts"] == 2
    assert second.status()["alert_delivered"] is True


def test_alert_delivery_failures_accumulate_durably_instead_of_vanishing(tmp_path):
    path = _db_path(tmp_path)
    cfg = _config()
    marker = Path(f"{path}.llm-circuit-unavailable")

    first = LLMCostCircuitBreaker(path, cfg, _FailingNotifier())
    first.mark_unavailable(
        RuntimeError("simulated accounting I/O failure"),
        run_id="run-a",
        mode="morning",
    )
    assert json.loads(marker.read_text())["alert_attempts"] == 1

    for expected_attempts in (2, 3, 4):
        later = LLMCostCircuitBreaker(path, cfg, _FailingNotifier())
        later.activate_session(f"run-{expected_attempts}", "midday")
        payload = json.loads(marker.read_text())
        assert payload["alert_attempts"] == expected_attempts
        assert payload["alert_delivered"] is False
        # Visible on the status surface (Mission Control / session
        # summaries read this), not only in the raw sidecar file.
        assert later.status()["alert_attempts"] == expected_attempts
        assert later.status()["alert_delivered"] is False


# =====================================================================
# 2026-09-22 cost-circuit outage: 503 classification (Defect A) and the
# transient-latch self-clear (Defect B). docs/WORK.md item 174.
#
# What happened: Google AI Studio returned HTTP 503 "This model is
# currently experiencing high demand" 17 times on 2026-09-22 [measured,
# production log]. 503 was not on the zero-cost allow-list, so the
# tech_analyst call that failed on both primary and failover was booked
# ambiguous and hard-latched the desk from 15:17 UTC to 00:33 UTC the
# next day -- through the close run and the evening run -- on settled
# spend of $0.7883 of a $2.75 day [measured, `scripts/cost_circuit.py
# status`]. No budget was breached.
# =====================================================================


def _cooldown_config(**overrides):
    """Production's own defaults for the self-clear knobs, not stand-ins.

    An earlier version pinned the allowance to 14, a number a later commit
    superseded -- so every self-clear test was running against a value the
    desk no longer uses. Read the real defaults instead.
    """
    from src.config import LLMCostCircuitConfig

    defaults = LLMCostCircuitConfig()
    values = {
        "transient_latch_cooldown_minutes": defaults.transient_latch_cooldown_minutes,
        "max_transient_latch_auto_clears_per_day": (defaults.max_transient_latch_auto_clears_per_day),
    }
    values.update(overrides)
    return _config(**values)


def _age_latch(path: str, minutes: float) -> None:
    """Backdate `suspended_at` so the cooldown has elapsed.

    The elapsed time is computed by SQLite against `julianday('now')`, the
    same clock that wrote the stamp, so moving the stamp is the honest way
    to simulate waiting.
    """
    # Against the circuit's own clock, not the OS clock. When a test has
    # frozen the desk's day the two are the same instant; when it has not,
    # this is `datetime.now(timezone.utc)` and the stamp is what SQLite
    # would have written anyway.
    stamp = (trading_calendar.utc_now() - timedelta(minutes=minutes)).strftime("%Y-%m-%d %H:%M:%S")
    with sqlite3.connect(path) as conn:
        conn.execute(
            "UPDATE llm_circuit_state SET suspended_at=? WHERE singleton=1",
            (stamp,),
        )


def _freeze_et_day(monkeypatch) -> None:
    """Pin `_et_day_and_utc_bounds` to one instant for the rest of the test.

    Several self-clear tests derive "today" from the real wall clock more
    than once in the same test -- once while building the latch, again
    later via `status()` (which reseeds "today" and recomputes the
    auto-reset window). Within a few minutes of the real ET midnight those
    two real-clock reads can land on different ET days, which is exactly
    the ~04:00 UTC flake this exists to remove. Freezing to noon ET on
    today's real date -- safely mid-day, never a boundary -- makes every
    later call return the identical day/bounds, mirroring the fixed-clock
    pattern `test_day_quota_hold_recovers_once_on_next_et_day` already
    uses. `_age_latch` still backdates `suspended_at` against SQLite's own
    real clock on purpose: the cooldown check measures elapsed wall time
    against that same clock, so only the ET-day bucketing needs pinning.
    """
    from datetime import time as _time

    today = datetime.now(timezone.utc).astimezone(_ET).date()
    pinned = datetime.combine(today, _time(12, 0), tzinfo=_ET).astimezone(timezone.utc)
    monkeypatch.setattr(trading_calendar, "utc_now", lambda: pinned)


def _utc_stamp_on_et_day(et_day: str) -> str:
    """A SQLite-shaped UTC timestamp landing at noon ET on `et_day`.

    Noon, not midnight, so the conversion cannot land on the adjacent ET
    day whatever the offset.
    """
    from datetime import time as _time

    noon_et = datetime.combine(
        datetime.fromisoformat(et_day).date(),
        _time(12, 0),
        tzinfo=_ET,
    )
    return noon_et.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _day_row(path: str) -> tuple:
    with sqlite3.connect(path) as conn:
        return conn.execute(
            "SELECT baseline_cost_usd + incremental_cost_usd, unknown_cost_rows, "
            "failed_call_unknown_rows, costs_exact FROM llm_budget_days"
        ).fetchone()


# --------------------------- Defect A ---------------------------------


def test_pre_generation_503_charges_nothing_and_does_not_trip(tmp_path):
    """The exact production shape: an SDK status error carrying 503.

    Google AI Studio refused for capacity before generating anything, so
    nothing was metered -- the same billing position as the 429 that is
    already allow-listed.
    """
    path = _db_path(tmp_path)
    notifier = _Notifier()
    circuit = LLMCostCircuitBreaker(path, _config(), notifier)
    error = _StatusCodeError(
        503,
        "Error code: 503 - [{'error': {'code': 503, 'message': 'This model is "
        "currently experiencing high demand. Spikes in demand are usually "
        "temporary. Please try again later.', 'status': 'UNAVAILABLE'}}]",
    )

    reservation = _authorize_and_fail(circuit, error, run_id="run-503-presend")

    assert _is_known_zero_cost_failure(error) is True
    _assert_charged_nothing_and_did_not_trip(circuit, notifier, path, reservation)
    # Nothing was marked unprovable either -- this is the flag that used to
    # keep re-latching the desk after every operator reset.
    assert _day_row(path)[1:] == (0, 0, 1)


def test_mid_stream_503_is_still_ambiguous_and_still_trips(tmp_path):
    """A 503 reported from INSIDE a started stream may already have billed.

    OpenRouter cannot change an HTTP status once the first byte is out, so
    it reports an upstream failure as an in-band SSE error chunk carrying
    the status the response would have had. Tokens may already have been
    generated. 503 is allow-listed subject to this veto, not blanket.
    """
    path = _db_path(tmp_path)
    notifier = _Notifier()
    circuit = LLMCostCircuitBreaker(path, _config(), notifier)
    error = LLMStreamErrorChunk(
        "provider error mid-stream (code=503, type=None): high demand",
        status_code=503,
    )

    reservation = _authorize_and_fail(circuit, error, run_id="run-503-midstream")

    assert _is_known_zero_cost_failure(error) is False
    _assert_charged_and_tripped(circuit, notifier, reservation)


def test_mid_stream_503_wrapped_by_another_exception_is_still_ambiguous(tmp_path):
    """The veto follows the cause chain, so wrapping cannot launder it."""
    path = _db_path(tmp_path)
    notifier = _Notifier()
    circuit = LLMCostCircuitBreaker(path, _config(), notifier)
    inner = LLMStreamErrorChunk("mid-stream 503", status_code=503)
    error = _StatusCodeError(503, "retry wrapper")
    error.__cause__ = inner

    reservation = _authorize_and_fail(circuit, error, run_id="run-503-wrapped")

    assert _is_known_zero_cost_failure(error) is False
    _assert_charged_and_tripped(circuit, notifier, reservation)


def test_mid_stream_429_stays_free_unchanged_by_the_503_fix(tmp_path):
    """Deliberately NOT changed: the ratified 2026-08-31 behaviour.

    `LLMStreamErrorChunk` exists precisely so a mid-stream 429 is judged as
    the 429 it is. The mid-stream veto added for 503 must not spill onto
    the codes that were already allow-listed.
    """
    path = _db_path(tmp_path)
    notifier = _Notifier()
    circuit = LLMCostCircuitBreaker(path, _config(), notifier)
    error = LLMStreamErrorChunk("mid-stream 429", status_code=429)

    reservation = _authorize_and_fail(circuit, error, run_id="run-429-midstream")

    assert _is_known_zero_cost_failure(error) is True
    _assert_charged_nothing_and_did_not_trip(circuit, notifier, path, reservation)


@pytest.mark.parametrize("status_code", [402, 500, 502, 504, 529])
def test_status_codes_outside_the_allow_list_stay_ambiguous(tmp_path, status_code):
    """402 in particular: "insufficient balance" is handled as a failover
    signal in base.py, has never been on the enumerated zero-cost list, and
    adding it here on our own initiative is the allow-list inversion this
    module's comments forbid."""
    path = _db_path(tmp_path)
    notifier = _Notifier()
    circuit = LLMCostCircuitBreaker(path, _config(), notifier)
    error = _StatusCodeError(status_code, f"provider error ({status_code})")

    reservation = _authorize_and_fail(circuit, error, run_id=f"run-amb-{status_code}")

    assert _is_known_zero_cost_failure(error) is False
    # Item 226: a 402 is still ambiguous cost and still trips -- the
    # accounting did not move -- but the desk now NAMES the cause instead
    # of reporting an unbounded-cost mystery it can actually explain.
    _assert_charged_and_tripped(
        circuit,
        notifier,
        reservation,
        expected_code=("provider_out_of_credit" if status_code == 402 else "failed_call_unknown_cost"),
    )


def test_missing_and_non_integer_status_codes_stay_ambiguous(tmp_path):
    """No status, a None status, a string status and a bool status must all
    fail closed. `True == 1` in Python, so a bool must never be read as a
    status code."""

    class _NoStatus(Exception):
        pass

    none_status = _StatusCodeError(500, "x")
    none_status.status_code = None
    str_status = _StatusCodeError(500, "x")
    str_status.status_code = "503"
    bool_status = _StatusCodeError(500, "x")
    bool_status.status_code = True

    for error in (_NoStatus("no attribute"), none_status, str_status, bool_status):
        assert _is_known_zero_cost_failure(error) is False

    path = _db_path(tmp_path)
    notifier = _Notifier()
    circuit = LLMCostCircuitBreaker(path, _config(), notifier)
    reservation = _authorize_and_fail(
        circuit,
        _NoStatus("no attribute"),
        run_id="run-no-status",
    )
    _assert_charged_and_tripped(circuit, notifier, reservation)


def test_one_mid_stream_503_among_pre_send_503s_charges_the_whole_call(tmp_path):
    """Ambiguity stays contagious across a call's attempts."""
    path = _db_path(tmp_path)
    notifier = _Notifier()
    circuit = LLMCostCircuitBreaker(path, _config(), notifier)
    free = _StatusCodeError(503, "pre-send 503")
    ambiguous = LLMStreamErrorChunk("mid-stream 503", status_code=503)

    circuit.activate_session("run-503-mixed", "morning")
    reservation = circuit.begin_call(
        agent_name="tech_analyst",
        model="google/gemini-3.5-flash-lite",
        system_prompt="s",
        user_message="u",
        max_output_tokens=100,
    )
    circuit.before_provider_attempt(reservation, model=reservation.model)
    circuit.fail_call(reservation, free, attempt_errors=[free, ambiguous])

    assert circuit.status()["suspended"] is True
    assert circuit.status()["trigger_code"] == "failed_call_unknown_cost"


def test_all_pre_send_503_attempts_are_free(tmp_path):
    """The production case: every attempt on the logical call 503'd."""
    path = _db_path(tmp_path)
    notifier = _Notifier()
    circuit = LLMCostCircuitBreaker(path, _config(), notifier)
    errors = [_StatusCodeError(503, "high demand") for _ in range(3)]

    circuit.activate_session("run-503-all", "intra_check")
    reservation = circuit.begin_call(
        agent_name="tech_analyst",
        model="google/gemini-3.5-flash-lite",
        system_prompt="s",
        user_message="u",
        max_output_tokens=100,
    )
    circuit.before_provider_attempt(reservation, model=reservation.model)
    circuit.fail_call(reservation, errors[-1], attempt_errors=errors)

    assert circuit.status()["suspended"] is False
    assert _day_row(path)[1:] == (0, 0, 1)


# --------------------------- Defect B ---------------------------------


def _latch_on_failed_call(
    path,
    notifier=None,
    config=None,
    run_id="run-latched",
    durable=True,
):
    """Drive the circuit into the real `failed_call_unknown_cost` latch by
    the only route that produces it: an ambiguous failed provider call.

    `durable=True` also carries the latch past the circuit's own self-clear
    window and runs the notify pass, which is item 208's precondition for
    the owner hearing about it at all: a transient latch that expires inside
    that window is one recorded episode and no message. Every item-174 test
    below is about what the owner READS, so all of them need a latch that
    has earned a page."""
    circuit = LLMCostCircuitBreaker(
        path,
        config or _cooldown_config(),
        notifier or _Notifier(),
    )
    _authorize_and_fail(
        circuit,
        _StatusCodeError(500, "ambiguous"),
        run_id=run_id,
    )
    assert circuit.status()["trigger_code"] == "failed_call_unknown_cost"
    if durable:
        _age_latch_past_self_clear_window(circuit)
    return circuit


def test_transient_latch_self_clears_once_the_cooldown_has_elapsed(
    tmp_path,
    monkeypatch,
):
    _freeze_et_day(monkeypatch)
    path = _db_path(tmp_path)
    circuit = _latch_on_failed_call(path)

    _age_latch(path, 16)
    state = circuit.status()

    assert state["suspended"] is False
    assert not state.get("trigger_code")
    assert "auto-expired" in (state.get("reset_reason") or "")
    # The audit trail names the trigger it forgave.
    with sqlite3.connect(path) as conn:
        row = conn.execute(
            "SELECT trigger_code, agent_name FROM llm_circuit_events WHERE event_type='auto_reset'"
        ).fetchone()
    assert row == ("failed_call_unknown_cost", "transient_latch_expiry")
    # And the desk can actually spend again -- the point of the whole fix.
    circuit.activate_session("run-after-clear", "close")
    circuit.begin_call(
        agent_name="tech_analyst",
        model="google/gemini-3.5-flash-lite",
        system_prompt="s",
        user_message="u",
        max_output_tokens=100,
    )


def test_auto_clear_alerts_the_owner_on_the_same_surface_as_the_suspension(
    tmp_path,
    monkeypatch,
):
    """Item 174: the suspension reaches Telegram, but the auto-expiry used to
    write only an `auto_reset` DB event and a log line -- so the owner saw
    'desk suspended' and never 'desk back live'. The auto-clear now sends the
    same-surface owner alert, stating the desk resumed and why (auto-expiry),
    while keeping the DB event and the log.
    """
    _freeze_et_day(monkeypatch)
    path = _db_path(tmp_path)
    notifier = _Notifier()
    circuit = _latch_on_failed_call(path, notifier=notifier)

    # The suspension WAS announced on Telegram; the resume was not, yet.
    assert any("SUSPENDED" in m for m in notifier.messages)
    assert [m for m in notifier.messages if "RESUMED" in m] == []

    _age_latch(path, 16)
    state = circuit.status()
    assert state["suspended"] is False

    resumes = [m for m in notifier.messages if "RESUMED" in m]
    assert len(resumes) == 1
    # It says WHAT resumed the desk (the forgiven trigger) and WHY (auto-expiry).
    assert "failed_call_unknown_cost" in resumes[0]
    assert "auto-expired" in resumes[0]

    # The DB event the auto-clear always wrote is preserved, not replaced.
    with sqlite3.connect(path) as conn:
        auto_resets = conn.execute("SELECT COUNT(*) FROM llm_circuit_events WHERE event_type='auto_reset'").fetchone()[
            0
        ]
        alerted = conn.execute(
            "SELECT recovery_alert_state FROM llm_circuit_events WHERE event_type='auto_reset'"
        ).fetchone()[0]
    assert auto_resets == 1
    assert alerted == 1  # marked sent

    # A later boundary does NOT re-announce the same resume.
    circuit.status()
    assert len([m for m in notifier.messages if "RESUMED" in m]) == 1


def test_auto_clear_resume_alert_retries_after_a_telegram_outage(
    tmp_path,
    monkeypatch,
):
    """A failed send leaves the resume alert PENDING (state 0), not lost, so
    the next boundary tries again -- the durable-retry posture the quota
    recovery alert already has, applied to item 174."""
    _freeze_et_day(monkeypatch)
    path = _db_path(tmp_path)
    circuit = _latch_on_failed_call(path, notifier=_Notifier())
    _age_latch(path, 16)

    # Swap in a notifier whose send() fails, THEN let the latch auto-clear.
    class _Down:
        def __init__(self):
            self.calls = 0

        def send(self, _message, **_kwargs):
            self.calls += 1
            return False

    down = _Down()
    circuit.notifier = down
    state = circuit.status()
    assert state["suspended"] is False
    assert down.calls >= 1
    with sqlite3.connect(path) as conn:
        pending = conn.execute(
            "SELECT recovery_alert_state FROM llm_circuit_events WHERE event_type='auto_reset'"
        ).fetchone()[0]
    assert pending == 0  # not sent -> still pending for a later retry

    # Telegram comes back; the pending resume is delivered on the next boundary.
    good = _Notifier()
    circuit.notifier = good
    circuit.status()
    assert len([m for m in good.messages if "RESUMED" in m]) == 1


def test_transient_latch_does_not_clear_before_the_cooldown(tmp_path):
    path = _db_path(tmp_path)
    circuit = _latch_on_failed_call(path)

    _age_latch(path, 14)

    assert circuit.status()["suspended"] is True
    assert circuit.status()["trigger_code"] == "failed_call_unknown_cost"


def test_transient_latch_does_not_clear_on_a_backwards_clock(tmp_path):
    """A `suspended_at` in the future yields negative elapsed time, which
    must not be read as "the cooldown passed"."""
    path = _db_path(tmp_path)
    circuit = _latch_on_failed_call(path)

    _age_latch(path, -600)

    assert circuit.status()["suspended"] is True


def test_self_clear_erases_no_settled_spend_and_raises_no_cap(
    tmp_path,
    monkeypatch,
):
    _freeze_et_day(monkeypatch)
    path = _db_path(tmp_path)
    notifier = _Notifier()
    config = _cooldown_config(daily_cost_limit_usd=20.0, session_cost_limit_usd=10.0)
    circuit = LLMCostCircuitBreaker(path, config, notifier)
    circuit.activate_session("run-spend-then-fail", "morning")
    reservation = circuit.begin_call(
        agent_name="tech_analyst",
        model="google/gemini-3.5-flash-lite",
        system_prompt="s",
        user_message="u",
        max_output_tokens=100,
    )
    circuit.before_provider_attempt(reservation, model=reservation.model)
    circuit.complete_call(reservation, 0.75)
    spend_before = circuit.status()["current_daily_cost_usd"]
    reservation2 = circuit.begin_call(
        agent_name="tech_analyst",
        model="google/gemini-3.5-flash-lite",
        system_prompt="s",
        user_message="u",
        max_output_tokens=100,
    )
    circuit.before_provider_attempt(reservation2, model=reservation2.model)
    circuit.fail_call(reservation2, _StatusCodeError(500, "ambiguous"))
    assert circuit.status()["suspended"] is True

    _age_latch(path, 16)
    state = circuit.status()

    assert state["suspended"] is False
    assert state["current_daily_cost_usd"] == pytest.approx(spend_before)
    assert state["current_daily_cost_usd"] == pytest.approx(0.75)
    assert float(state["daily_limit_usd"]) == 20.0
    assert float(state["session_limit_usd"]) == 10.0
    assert _day_row(path)[0] == pytest.approx(0.75)


def test_self_clear_never_reopens_into_a_breached_budget(tmp_path):
    """Spend at the cap plus an ambiguous failure: the cooldown expiring
    must not hand the desk a window to spend through an unchanged cap."""
    path = _db_path(tmp_path)
    config = _cooldown_config(daily_cost_limit_usd=0.50, session_cost_limit_usd=0.50)
    circuit = LLMCostCircuitBreaker(path, config, _Notifier())
    circuit.activate_session("run-at-cap", "morning")
    reservation = circuit.begin_call(
        agent_name="tech_analyst",
        model="google/gemini-3.5-flash-lite",
        system_prompt="s",
        user_message="u",
        max_output_tokens=100,
    )
    circuit.before_provider_attempt(reservation, model=reservation.model)
    circuit.complete_call(reservation, 0.60)
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE llm_budget_days SET unknown_cost_rows=1, failed_call_unknown_rows=1, costs_exact=0")
        conn.execute(
            "UPDATE llm_circuit_state SET suspended=1, "
            "trigger_code='failed_call_unknown_cost', run_id='run-at-cap', "
            "mode='morning', agent_name='tech_analyst', "
            "trigger_detail='x', suspended_at=datetime('now','-60 minutes')"
        )

    assert circuit.status()["suspended"] is True
    with pytest.raises(PaidAnalysisSuspended):
        circuit.require_paid_analysis("after-cooldown")
    # Stronger than "still suspended": the settled-limit check would
    # re-latch a wrongly-cleared circuit anyway, so assert the clear never
    # HAPPENED. The original trigger survives and no auto_reset was written.
    assert circuit.status()["trigger_code"] == "failed_call_unknown_cost"
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM llm_circuit_events WHERE event_type='auto_reset'").fetchone()[0] == 0


def test_the_emergency_infrastructure_latch_is_exempt_from_the_self_clear(tmp_path):
    """The circuit's own storage failing is not a provider blip.

    Exercised against the locked helper directly: the sidecar normally also
    installs an in-process sentinel that short-circuits every public entry
    point, so the only way to show THIS guard carries its own weight is to
    present the sidecar without it -- which is exactly the live race it
    exists for, another process writing the marker while this one is
    mid-transaction.
    """
    path = _db_path(tmp_path)
    circuit = _latch_on_failed_call(path)
    _age_latch(path, 600)
    circuit._emergency_latch_path.write_text(
        json.dumps({"recorded_at": "2026-09-22T15:17:09Z", "detail": "sqlite gone"}),
        encoding="utf-8",
    )

    day, _, _ = _et_day_and_utc_bounds()
    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("BEGIN IMMEDIATE")
        cleared = circuit._auto_clear_transient_latch_locked(conn, current_day=day)
        conn.commit()

    assert cleared is False
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT suspended FROM llm_circuit_state").fetchone()[0] == 1


def test_a_genuine_spend_breach_is_not_touched_by_the_self_clear(tmp_path):
    """A real settled-cost breach stops the desk on its own budget window,
    not on a hard latch, and the transient self-clear must not release it."""
    path = _db_path(tmp_path)
    config = _cooldown_config(daily_cost_limit_usd=0.50, session_cost_limit_usd=0.50)
    circuit = LLMCostCircuitBreaker(path, config, _Notifier())
    circuit.activate_session("run-breach", "morning")
    reservation = circuit.begin_call(
        agent_name="tech_analyst",
        model="google/gemini-3.5-flash-lite",
        system_prompt="s",
        user_message="u",
        max_output_tokens=100,
    )
    circuit.before_provider_attempt(reservation, model=reservation.model)
    circuit.complete_call(reservation, 0.60)
    circuit.enforce_current_limits("preflight")
    state = circuit.status()
    assert state["suspended"] is True
    assert state["trigger_code"] in {"daily_cost_limit", "session_cost_limit"}

    _age_latch(path, 600)

    assert circuit.status()["suspended"] is True
    with pytest.raises(PaidAnalysisSuspended):
        circuit.require_paid_analysis("after-cooldown")


def test_unknown_actual_cost_from_a_completed_call_still_needs_a_human(tmp_path):
    """A call that SUCCEEDED with no usage telemetry generated real tokens.
    That unknown is real spend, not a failed attempt, so it keeps the
    durable operator-reset latch."""
    path = _db_path(tmp_path)
    circuit = LLMCostCircuitBreaker(path, _cooldown_config(), _Notifier())
    circuit.activate_session("run-no-telemetry", "morning")
    reservation = circuit.begin_call(
        agent_name="tech_analyst",
        model="google/gemini-3.5-flash-lite",
        system_prompt="s",
        user_message="u",
        max_output_tokens=100,
    )
    circuit.before_provider_attempt(reservation, model=reservation.model)
    circuit.complete_call(reservation, None)
    assert circuit.status()["trigger_code"] == "unknown_actual_cost"

    _age_latch(path, 600)

    assert circuit.status()["suspended"] is True
    assert circuit.status()["trigger_code"] == "unknown_actual_cost"


def test_unknown_actual_cost_is_excluded_by_its_code_not_by_row_provenance(tmp_path):
    """Kills the mutation that adds `unknown_actual_cost` to the
    self-clearing set.

    The test above passes even with that mutation, because a completed
    call with no telemetry books an unknown row that is NOT a failed-call
    row, so the provenance guard blocks it for a different reason. Here
    the day's rows ARE all failed-call rows and only the trigger code
    differs, so the trigger-class line is the only thing under test.
    """
    path = _db_path(tmp_path)
    circuit = _latch_on_failed_call(path)
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE llm_circuit_state SET trigger_code='unknown_actual_cost'")

    _age_latch(path, 600)

    assert circuit.status()["suspended"] is True
    assert circuit.status()["trigger_code"] == "unknown_actual_cost"
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM llm_circuit_events WHERE event_type='auto_reset'").fetchone()[0] == 0


def test_integrity_trigger_still_needs_a_human(tmp_path):
    """`non_monotonic_quota_hold_day` means clock regression or accounting
    corruption. Never transient, never self-clearing."""
    path = _db_path(tmp_path)
    circuit = LLMCostCircuitBreaker(path, _cooldown_config(), _Notifier())
    circuit.activate_session("run-integrity", "morning")
    with sqlite3.connect(path) as conn:
        conn.execute(
            "UPDATE llm_circuit_state SET suspended=1, "
            "trigger_code='non_monotonic_quota_hold_day', "
            "run_id='run-integrity', mode='morning', "
            "agent_name='budget_rollover', "
            "trigger_detail='clock regression', "
            "suspended_at=datetime('now','-600 minutes')"
        )

    assert circuit.status()["suspended"] is True
    assert circuit.status()["trigger_code"] == "non_monotonic_quota_hold_day"


def test_an_unknown_row_of_another_provenance_blocks_the_self_clear(tmp_path):
    """One unknown row this circuit did not book as a failed call, and the
    whole day waits for a human -- it cannot tell which row the latch is
    really about."""
    path = _db_path(tmp_path)
    circuit = _latch_on_failed_call(path)
    with sqlite3.connect(path) as conn:
        # A pre-deployment agent_logs row with a NULL cost, seeded by
        # `_seed_today`: counted in unknown_cost_rows, not a failed call.
        conn.execute("UPDATE llm_budget_days SET unknown_cost_rows=unknown_cost_rows+1")

    _age_latch(path, 600)

    assert circuit.status()["suspended"] is True
    assert circuit.status()["trigger_code"] == "failed_call_unknown_cost"


def test_legacy_unknown_cost_from_failed_call_rows_self_clears(
    tmp_path,
    monkeypatch,
):
    """The 2026-09-16 recurrence: the SAME defect surfaces under the
    downstream code once the day is inexact. It must expire the same way."""
    _freeze_et_day(monkeypatch)
    path = _db_path(tmp_path)
    circuit = _latch_on_failed_call(path)
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE llm_circuit_state SET trigger_code='legacy_unknown_cost'")

    _age_latch(path, 16)

    assert circuit.status()["suspended"] is False


def test_the_daily_auto_clear_allowance_is_finite(tmp_path, monkeypatch):
    """A fault that keeps recurring is not transient. Past the allowance the
    next occurrence latches durably and waits for a human."""
    _freeze_et_day(monkeypatch)
    path = _db_path(tmp_path)
    config = _cooldown_config(max_transient_latch_auto_clears_per_day=2)
    notifier = _Notifier()
    circuit = LLMCostCircuitBreaker(path, config, notifier)
    for n in range(3):
        _authorize_and_fail(
            circuit,
            _StatusCodeError(500, "ambiguous"),
            run_id=f"run-flap-{n}",
        )
        assert circuit.status()["trigger_code"] == "failed_call_unknown_cost"
        _age_latch(path, 16)
        cleared = circuit.status()["suspended"] is False
        assert cleared is (n < 2), f"occurrence {n} cleared={cleared}"
    with sqlite3.connect(path) as conn:
        n_auto = conn.execute("SELECT COUNT(*) FROM llm_circuit_events WHERE event_type='auto_reset'").fetchone()[0]
    assert n_auto == 2
    # The operator route still works on the one that stuck.
    circuit.reset("operator reviewed the repeating provider fault")
    assert circuit.status()["suspended"] is False


def test_the_durable_emergency_latch_is_never_self_cleared(tmp_path):
    """The circuit's own infrastructure failed, so nothing it computes may
    authorize its own recovery."""
    path = _db_path(tmp_path)
    circuit = _latch_on_failed_call(path)
    circuit.mark_unavailable(
        RuntimeError("sqlite unavailable"),
        run_id="run-latched",
        mode="morning",
        agent_name="tech_analyst",
    )

    _age_latch(path, 600)

    assert circuit.status()["suspended"] is True
    with pytest.raises(PaidAnalysisSuspended):
        circuit.require_paid_analysis("after-cooldown")


def test_the_2026_09_22_outage_no_longer_stops_the_desk(tmp_path):
    """End to end, both defects at once, against the real shape of the day.

    Primary and failover both 503 pre-generation on the final attempt with
    spend far under cap. Before this change that hard-latched the desk for
    nine hours. Now nothing latches at all.
    """
    path = _db_path(tmp_path)
    notifier = _Notifier()
    circuit = LLMCostCircuitBreaker(
        path,
        _cooldown_config(max_provider_attempts_per_call=8),
        notifier,
    )
    circuit.activate_session("intra_check-005f582a", "intra_check")
    reservation = circuit.begin_call(
        agent_name="tech_analyst",
        model="google/gemini-3.5-flash-lite",
        system_prompt="s",
        user_message="u",
        max_output_tokens=100,
    )
    attempts = []
    for _ in range(7):  # 7 provider attempts, as the live latch recorded
        circuit.before_provider_attempt(reservation, model=reservation.model)
        attempts.append(_StatusCodeError(503, "high demand"))
    circuit.fail_call(reservation, attempts[-1], attempt_errors=attempts)

    state = circuit.status()
    assert state["suspended"] is False
    assert notifier.messages == []
    # The close run that was missed on 2026-09-22 now proceeds.
    circuit.activate_session("close-next", "close")
    circuit.begin_call(
        agent_name="position_reviewer",
        model="google/gemini-3.5-flash-lite",
        system_prompt="s",
        user_message="u",
        max_output_tokens=100,
    )


# --- adversary round 1 (2026-09-23): holes found in the first draft -------


def test_a_latch_that_outlived_midnight_checks_the_day_it_was_stamped_on(tmp_path):
    """The hole the adversary found, and the exact shape of the incident.

    The 2026-09-22 latch ran 15:17 UTC to 00:33 UTC the next day. Today's
    accounting row is seeded fresh with zero unknown rows, so a self-clear
    that looked only at today would wave the latch through while its real
    unproven rows sat on yesterday, unexamined.
    """
    path = _db_path(tmp_path)
    circuit = _latch_on_failed_call(path)
    today, _, _ = _et_day_and_utc_bounds()
    yesterday = (datetime.fromisoformat(today) - timedelta(days=1)).date().isoformat()
    with sqlite3.connect(path) as conn:
        # Move the whole incident onto yesterday, then seed a clean today --
        # what the next morning's run actually finds.
        conn.execute("UPDATE llm_budget_days SET day=?", (yesterday,))
        conn.execute("UPDATE llm_budget_sessions SET day=?", (yesterday,))
        conn.execute(
            "INSERT INTO llm_budget_days(day, unknown_cost_rows, "
            "failed_call_unknown_rows, costs_exact, seeded_at, updated_at) VALUES (?, 0, 0, 1, ?, ?)",
            (today, trading_calendar.sql_utc_stamp(), trading_calendar.sql_utc_stamp()),
        )
        # Yesterday also carries one row of ANOTHER provenance, so the
        # forgiveness is not owed.
        conn.execute(
            "UPDATE llm_budget_days SET unknown_cost_rows=unknown_cost_rows+1 WHERE day=?",
            (yesterday,),
        )
        conn.execute(
            "UPDATE llm_circuit_state SET suspended_at=?",
            (_utc_stamp_on_et_day(yesterday),),
        )

    day = today
    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("BEGIN IMMEDIATE")
        cleared = circuit._auto_clear_transient_latch_locked(conn, current_day=day)
        conn.commit()

    assert cleared is False
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT suspended FROM llm_circuit_state").fetchone()[0] == 1


def test_a_clean_overnight_latch_clears_and_forgives_both_days(tmp_path):
    """Same span, nothing of another provenance: it expires, and yesterday's
    unproven flags are cleared too -- not left to gate tomorrow."""
    path = _db_path(tmp_path)
    circuit = _latch_on_failed_call(path)
    today, _, _ = _et_day_and_utc_bounds()
    yesterday = (datetime.fromisoformat(today) - timedelta(days=1)).date().isoformat()
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE llm_budget_days SET day=?", (yesterday,))
        conn.execute("UPDATE llm_budget_sessions SET day=?", (yesterday,))
        conn.execute(
            "INSERT INTO llm_budget_days(day, unknown_cost_rows, "
            "failed_call_unknown_rows, costs_exact, seeded_at, updated_at) VALUES (?, 0, 0, 1, ?, ?)",
            (today, trading_calendar.sql_utc_stamp(), trading_calendar.sql_utc_stamp()),
        )
        conn.execute(
            "UPDATE llm_circuit_state SET suspended_at=?",
            (_utc_stamp_on_et_day(yesterday),),
        )

    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("BEGIN IMMEDIATE")
        cleared = circuit._auto_clear_transient_latch_locked(
            conn,
            current_day=today,
        )
        conn.commit()

    assert cleared is True
    with sqlite3.connect(path) as conn:
        rows = conn.execute(
            "SELECT day, unknown_cost_rows, failed_call_unknown_rows, costs_exact FROM llm_budget_days ORDER BY day"
        ).fetchall()
    # Today is forgiven. Yesterday is READ to decide and deliberately left
    # alone: nothing consumes a past day's flags, and stamping costs_exact=1
    # on a historical day would rewrite settled history to look provable.
    assert rows == [(yesterday, 1, 1, 0), (today, 0, 0, 1)]


def test_a_corrupt_failed_call_counter_refuses_instead_of_escalating(tmp_path):
    """A subset counter above its own total is corruption.

    It must block the self-clear and hand the day to an operator -- NOT
    raise, which `_validate_accounting_invariants` would have escalated into
    the durable emergency latch, the strictest stop in the system. A fix for
    spurious hard latches must not add one.
    """
    path = _db_path(tmp_path)
    circuit = _latch_on_failed_call(path)
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE llm_budget_days SET failed_call_unknown_rows=99")

    _age_latch(path, 600)

    state = circuit.status()  # must not raise
    assert state["suspended"] is True
    assert state["trigger_code"] == "failed_call_unknown_cost"


def test_the_self_clear_leaves_the_session_row_as_the_record(
    tmp_path,
    monkeypatch,
):
    """Strictly weaker than the operator path, on purpose.

    An earlier draft flipped `call_failed` sessions back to `active` and
    marked them exact. That enforced nothing, and made the dashboard's
    per-run cost report a by-construction-unknown figure as exact.
    """
    _freeze_et_day(monkeypatch)
    path = _db_path(tmp_path)
    circuit = _latch_on_failed_call(path)

    _age_latch(path, 16)

    assert circuit.status()["suspended"] is False
    with sqlite3.connect(path) as conn:
        row = conn.execute("SELECT status, costs_exact FROM llm_budget_sessions WHERE run_id='run-latched'").fetchone()
    # `_trip_locked` already moved it to 'suspended'; either way the point
    # is that the self-clear does not rewrite it, exactly as `reset` does not.
    assert row == ("suspended", 0)


def test_the_allowance_is_one_per_scheduled_paid_run_including_intra_ticks(tmp_path):
    """One forgiveness per scheduled run that can make a paid call.

    intra_check counts, once per 30-minute tick. Its window comment said
    "no LLM" and was wrong: measured on the production DB it is 13-14 paid
    sessions a day and 72-90% of the daily spend, and the 2026-09-22 latch
    was tripped by one."""
    from src.config import LLMCostCircuitConfig
    from src.trading_calendar import SESSION_WINDOWS

    # Pinned literally. Recomputing `_paid_run_count`'s own body and
    # comparing it to `_paid_run_count` asserts nothing.
    assert LLMCostCircuitConfig().max_transient_latch_auto_clears_per_day == 19
    # And intra_check is IN it, which is the whole correction: it is the
    # desk's largest paid cost centre, not the "no LLM" tick its window
    # comment claimed for two derivations running.
    assert "intra_check" in SESSION_WINDOWS
    assert len(SESSION_WINDOWS) == 6  # 5 named + intra_check


def test_the_cooldown_is_the_midpoint_of_one_paid_run_gap(tmp_path):
    """The only hard bound is the gap between consecutive paid runs (the
    30-minute intra tick): at or above it a second paid run is lost. Below
    it nothing is unsafe, only wasteful. The midpoint is the value furthest
    from both, and most tolerant of jitter."""
    from src.config import LLMCostCircuitConfig

    from src.config import INTRA_CHECK_TICK_MINUTES

    cooldown = LLMCostCircuitConfig().transient_latch_cooldown_minutes
    # Pinned to the midpoint of (0, one paid-run gap), not merely inside it.
    assert cooldown == INTRA_CHECK_TICK_MINUTES / 2
    assert cooldown == 15.0


# --- adversary round 2 (2026-09-23) ---------------------------------------


def test_the_self_clear_leaves_another_days_failed_session_alone(
    tmp_path,
    monkeypatch,
):
    """Guards the deleted `llm_budget_sessions` write for real.

    The previous test for this could not: `_trip_locked` has already moved
    the LATCHED run's session to 'suspended', so the deleted statement
    (`WHERE day=? AND status='call_failed'`) could never have matched it,
    and restoring the statement passed anyway. The row the statement WOULD
    have matched is a different same-day session left at 'call_failed' --
    which is exactly where the dashboard-lie risk lived, because
    `_canonical_run_cost` reports a definite dollar figure for a run once
    `costs_exact` flips to 1.
    """
    _freeze_et_day(monkeypatch)
    path = _db_path(tmp_path)
    circuit = _latch_on_failed_call(path)
    day, _, _ = _et_day_and_utc_bounds()
    with sqlite3.connect(path) as conn:
        conn.execute(
            "INSERT INTO llm_budget_sessions"
            "(run_id, day, mode, actual_cost_usd, costs_exact, status, started_at, updated_at) "
            "VALUES ('run-other-failed', ?, 'midday', 0.0, 0, 'call_failed', ?, ?)",
            (day, trading_calendar.sql_utc_stamp(), trading_calendar.sql_utc_stamp()),
        )

    _age_latch(path, 16)

    assert circuit.status()["suspended"] is False
    with sqlite3.connect(path) as conn:
        row = conn.execute(
            "SELECT status, costs_exact FROM llm_budget_sessions WHERE run_id='run-other-failed'"
        ).fetchone()
    assert row == ("call_failed", 0)


def test_the_tick_spacing_this_module_pins_matches_the_scheduler(tmp_path):
    """`INTRA_CHECK_TICK_MINUTES` is duplicated from the scheduler, which is
    the authority. A silent divergence would move the cooldown's only hard
    bound without anything noticing."""
    from src.config import INTRA_CHECK_TICK_MINUTES
    from src.scheduler import TradingScheduler
    from src.trading_calendar import SESSION_WINDOWS

    trigger = TradingScheduler._build_intra_check_trigger()
    lo_min, hi_min = SESSION_WINDOWS["intra_check"]
    expected = len(range(lo_min, hi_min + 1, INTRA_CHECK_TICK_MINUTES))
    assert len(trigger.triggers) == expected == 14


def test_schema_migration_adds_recovery_alert_timestamp_on_an_old_db(tmp_path):
    """An older DB missing recovery_alert_updated_at must migrate, not crash.

    SQLite forbids a non-constant default on ALTER TABLE ADD COLUMN, so an
    ADD COLUMN ... DEFAULT (datetime('now')) raised OperationalError on every
    pre-existing DB, aborted ensure_cost_circuit_schema, and failed paid
    analysis closed (which then fired a stray owner alert that broke unrelated
    tests). This reproduces the migration path and asserts it succeeds. See
    src/cost_circuit.py ensure_cost_circuit_schema for the constant-default fix.
    """
    from src.cost_circuit import ensure_cost_circuit_schema

    conn = sqlite3.connect(str(tmp_path / "old.db"))
    conn.row_factory = sqlite3.Row
    ensure_cost_circuit_schema(conn)
    conn.commit()
    # Simulate a DB created before the column existed.
    conn.execute("ALTER TABLE llm_circuit_events DROP COLUMN recovery_alert_updated_at")
    conn.commit()
    cols_before = {r[1] for r in conn.execute("PRAGMA table_info(llm_circuit_events)")}
    assert "recovery_alert_updated_at" not in cols_before

    # The migration must re-add the column without raising.
    ensure_cost_circuit_schema(conn)
    conn.commit()

    cols_after = {r[1] for r in conn.execute("PRAGMA table_info(llm_circuit_events)")}
    assert "recovery_alert_updated_at" in cols_after
    conn.close()


def test_resume_is_not_announced_for_a_suspension_the_owner_never_received(
    tmp_path,
    monkeypatch,
):
    """Item 174 pairing: the resume note must be PAIRED to a suspension note.

    If the "PAID ANALYSIS SUSPENDED" alert never reached Telegram (outage at
    latch time), the auto-clear wipes `suspended` and `alert_state` on its way
    out, so that suspension alert can never be delivered -- it is gone. Firing
    "RESUMED" anyway hands the owner a recovery for an incident he was never
    told about, which reads as the desk having been down without telling him.
    The honest state is that, from his side, nothing happened: neither note.
    """
    _freeze_et_day(monkeypatch)
    path = _db_path(tmp_path)

    class _Down:
        messages: list[str] = []

        def send(self, _message, **_kwargs):
            return False

    circuit = _latch_on_failed_call(path, notifier=_Down())
    with sqlite3.connect(path) as conn:
        assert (
            conn.execute("SELECT alert_state FROM llm_circuit_state WHERE singleton=1").fetchone()[0] == 0
        )  # suspension never delivered

    _age_latch(path, 16)
    good = _Notifier()
    circuit.notifier = good
    assert circuit.status()["suspended"] is False

    assert [m for m in good.messages if "RESUMED" in m] == []
    # And it is not left pending forever: the row is resolved as unpaired.
    with sqlite3.connect(path) as conn:
        assert (
            conn.execute(
                "SELECT recovery_alert_state FROM llm_circuit_events WHERE event_type='auto_reset'"
            ).fetchone()[0]
            == 2
        )


def test_resume_alert_states_what_resumed_when_and_why(tmp_path, monkeypatch):
    """Item 174 content: never a bare status word, and every number in the
    message read from the `auto_reset` event row, not recomputed at send time.
    """
    _freeze_et_day(monkeypatch)
    path = _db_path(tmp_path)
    notifier = _Notifier()
    circuit = _latch_on_failed_call(path, notifier=notifier)
    _age_latch(path, 16)
    circuit.status()

    message = next(m for m in notifier.messages if "RESUMED" in m)
    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        event = dict(conn.execute("SELECT * FROM llm_circuit_events WHERE event_type='auto_reset'").fetchone())

    # WHAT was suspended, and that paid analysis (not the whole desk) resumed.
    assert "PAID ANALYSIS RESUMED" in message
    assert event["trigger_code"] in message
    # WHEN -- the event row's own stamp, not the clock at send time.
    assert f"resumed at: {event['created_at']} UTC" in message
    # WHY it cleared.
    assert "auto-expired" in message
    assert event["detail"] in message
    # Numbers are the event's, not invented: settled spend at resume.
    # `format_settled_money` (src/notifier.py), not a raw 4-decimal string:
    # the owner-facing dollar shape is always exactly 2 decimal places, or
    # else the notifier's own malformed-number guard would redact this line
    # before it ever reached the owner (2026-09-29 log: it did, repeatedly).
    from src.notifier import format_settled_money

    assert format_settled_money(event["session_cost_usd"]) in message
    assert format_settled_money(event["daily_cost_usd"]) in message
    # And it says the limits still bite, so "resumed" is not read as "all clear".
    assert "remain enforced" in message


def test_rehearsal_sends_no_resume_alert_to_the_owner(tmp_path, monkeypatch, caplog):
    """QAMC_REHEARSAL=1 must suppress every outbound notification, including
    this one -- a rehearsal alert is indistinguishable from a real one in the
    owner's chat, which is worse than sending nothing."""
    from src.notifier import TelegramNotifier

    _freeze_et_day(monkeypatch)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "sentinel-token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "sentinel-chat")
    monkeypatch.delenv("TELEGRAM_DISABLED", raising=False)
    monkeypatch.setattr("src.notifier._REHEARSAL_MODE", True)

    def _explode(*_args, **_kwargs):  # pragma: no cover - must never run
        raise AssertionError("a rehearsal transmitted to the network")

    monkeypatch.setattr("src.notifier.requests.post", _explode)

    telegram = TelegramNotifier()
    # Guard against a vacuous pass: `send` returns early when the notifier is
    # disabled, which would suppress the alert for a reason that is NOT the
    # rehearsal flag and prove nothing.
    assert telegram.enabled is True

    path = _db_path(tmp_path)
    with caplog.at_level(logging.INFO, logger="src.notifier"):
        circuit = _latch_on_failed_call(path, notifier=telegram)
        _age_latch(path, 16)
        assert circuit.status()["suspended"] is False
    suppressed = [r.getMessage() for r in caplog.records if "REHEARSAL: suppressed operator alert" in r.getMessage()]
    # The suspension alert proves the chokepoint is live in this test (a
    # disabled notifier would have returned before reaching it), and
    # `_explode` proves nothing went on the wire.
    assert any("SUSPENDED" in m for m in suppressed)
    # Under rehearsal the suspension is itself undelivered, so item 174's
    # pairing rule independently withholds the resume: the owner gets neither
    # note, and the event is resolved as unpaired rather than left pending.
    assert not any("RESUMED" in m for m in suppressed)
    with sqlite3.connect(path) as conn:
        assert (
            conn.execute(
                "SELECT recovery_alert_state FROM llm_circuit_events WHERE event_type='auto_reset'"
            ).fetchone()[0]
            == 2
        )

    # And the resume text itself is suppressed by the same guard when it does
    # reach the notifier (the paired case), rather than only by the pairing rule.
    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        event = dict(conn.execute("SELECT * FROM llm_circuit_events WHERE event_type='auto_reset'").fetchone())
    caplog.clear()
    with caplog.at_level(logging.INFO, logger="src.notifier"):
        assert not telegram.send(LLMCostCircuitBreaker.format_auto_reset_alert(event))
    assert any("REHEARSAL: suppressed operator alert" in r.getMessage() for r in caplog.records)
    # The audit trail is still written -- suppression is delivery-only.
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM llm_circuit_events WHERE event_type='auto_reset'").fetchone()[0] == 1


def test_schema_migration_adds_suspension_alert_state_on_an_old_db(tmp_path):
    """An older DB missing the pairing column migrates rather than crashing,
    and its pre-existing rows default to 'the owner was told' -- the direction
    that can only over-send, never silently swallow a resume."""
    path = _db_path(tmp_path)
    LLMCostCircuitBreaker(path, _config(), _Notifier())
    with sqlite3.connect(path) as conn:
        conn.execute("ALTER TABLE llm_circuit_events DROP COLUMN suspension_alert_state")
        conn.commit()
        cols_before = {r[1] for r in conn.execute("PRAGMA table_info(llm_circuit_events)")}
    assert "suspension_alert_state" not in cols_before

    LLMCostCircuitBreaker(path, _config(), _Notifier())
    with sqlite3.connect(path) as conn:
        cols_after = {r[1] for r in conn.execute("PRAGMA table_info(llm_circuit_events)")}
    assert "suspension_alert_state" in cols_after


# === docs/WORK.md item 147: a NULL cost that is provably $0, not unknown ===


def _seed_agent_log(path, **columns):
    """Insert one same-ET-day agent_logs row with an explicit timestamp."""

    utc_start = _et_day_and_utc_bounds()[1]
    columns.setdefault("timestamp", utc_start)
    names = ", ".join(columns)
    marks = ", ".join("?" for _ in columns)
    with sqlite3.connect(path) as conn:
        conn.execute(
            f"INSERT INTO agent_logs ({names}) VALUES ({marks})",
            tuple(columns.values()),
        )


def test_seeded_day_stays_exact_for_a_cache_hit_that_called_no_provider(tmp_path):
    """Item 147. A success with provider_requests=0 cost exactly $0.

    This is the shape of all 7 NULL-cost rows in the production DB
    [measured read-only 2026-09-26]: smart_money_analyst synthesis-cache
    hits. No provider request was issued, so the row is not unknown spend
    and must not seed the day inexact or arm legacy_unknown_cost.
    """

    path = _db_path(tmp_path)
    _seed_agent_log(
        path,
        agent_name="smart_money_analyst",
        run_id="run-cached",
        model="google/gemini-2.5-flash-lite",
        tokens_used=0,
        input_tokens=0,
        output_tokens=0,
        cost_usd=None,
        provider_requests=0,
        status="success",
        input_message="[cached evidence hash]",
    )
    circuit = LLMCostCircuitBreaker(path, _config(), _Notifier())
    circuit.activate_session("run-cached", "morning")

    with sqlite3.connect(path) as conn:
        unknown, exact = conn.execute("SELECT unknown_cost_rows, costs_exact FROM llm_budget_days").fetchone()
    assert unknown == 0
    assert exact == 1
    assert circuit.status()["suspended"] is False


def test_seeded_day_goes_inexact_for_a_success_that_did_call_a_provider(tmp_path):
    """Item 147. The unknown case is untouched: a real request, no price."""

    path = _db_path(tmp_path)
    _seed_agent_log(
        path,
        agent_name="tech_analyst",
        run_id="run-unpriced",
        model="some/unpriced-model",
        tokens_used=900,
        input_tokens=600,
        output_tokens=300,
        cost_usd=None,
        provider_requests=1,
        status="success",
    )
    circuit = LLMCostCircuitBreaker(path, _config(), _Notifier())
    circuit.activate_session("run-unpriced", "morning")

    with sqlite3.connect(path) as conn:
        unknown, exact = conn.execute("SELECT unknown_cost_rows, costs_exact FROM llm_budget_days").fetchone()
    assert unknown == 1
    assert exact == 0
    assert circuit.status()["trigger_code"] == "legacy_unknown_cost"


def test_seeded_day_goes_inexact_for_a_zero_request_row_that_did_not_succeed(tmp_path):
    """Item 147. status='success' is load-bearing, not decoration.

    `src/pipeline.py`'s evening exception path also synthesises a result
    with provider_requests=0 and no cost, after a call that may well have
    reached the provider. That row stays unknown.
    """

    path = _db_path(tmp_path)
    _seed_agent_log(
        path,
        agent_name="evening_analyst",
        run_id="run-exception",
        model="m",
        tokens_used=0,
        cost_usd=None,
        provider_requests=0,
        status="error",
    )
    circuit = LLMCostCircuitBreaker(path, _config(), _Notifier())
    circuit.activate_session("run-exception", "morning")

    with sqlite3.connect(path) as conn:
        unknown, exact = conn.execute("SELECT unknown_cost_rows, costs_exact FROM llm_budget_days").fetchone()
    assert unknown == 1
    assert exact == 0


def test_seeded_day_goes_inexact_for_a_legacy_row_with_no_request_count(tmp_path):
    """Item 147. 194 production rows predate provider_requests [measured].

    NULL is not a proof of zero, and SQL's `= 0` never matches it, so such
    a row keeps counting as unknown -- the safe direction.
    """

    path = _db_path(tmp_path)
    _seed_agent_log(
        path,
        agent_name="news_analyst",
        run_id="run-legacy",
        model="m",
        tokens_used=0,
        cost_usd=None,
        provider_requests=None,
        status="success",
    )
    circuit = LLMCostCircuitBreaker(path, _config(), _Notifier())
    circuit.activate_session("run-legacy", "morning")

    with sqlite3.connect(path) as conn:
        unknown, exact = conn.execute("SELECT unknown_cost_rows, costs_exact FROM llm_budget_days").fetchone()
    assert unknown == 1
    assert exact == 0


def test_seeded_day_books_a_priced_row_from_its_own_reported_cost(tmp_path):
    """Item 147. A row WITH usage is still charged what usage says."""

    path = _db_path(tmp_path)
    _seed_agent_log(
        path,
        agent_name="tech_analyst",
        run_id="run-priced",
        model="m",
        tokens_used=500,
        input_tokens=400,
        output_tokens=100,
        cost_usd=0.0042,
        provider_requests=1,
        status="success",
    )
    _seed_agent_log(
        path,
        agent_name="smart_money_analyst",
        run_id="run-priced",
        model="m",
        tokens_used=0,
        cost_usd=None,
        provider_requests=0,
        status="success",
    )
    circuit = LLMCostCircuitBreaker(path, _config(), _Notifier())
    circuit.activate_session("run-priced", "morning")

    with sqlite3.connect(path) as conn:
        baseline, unknown, exact = conn.execute(
            "SELECT baseline_cost_usd, unknown_cost_rows, costs_exact FROM llm_budget_days"
        ).fetchone()
        session_exact = conn.execute(
            "SELECT costs_exact FROM llm_budget_sessions WHERE run_id='run-priced'"
        ).fetchone()[0]
    # The cache hit adds nothing and hides nothing: the day is the priced
    # row's own reported figure, and it is exact.
    assert baseline == pytest.approx(0.0042)
    assert unknown == 0
    assert exact == 1
    assert session_exact == 1


def test_smart_money_cache_hit_reports_zero_cost_not_unknown():
    """Item 147 root cause: the cache path books $0.00, never NULL."""

    from src.agents.smart_money_analyst import SmartMoneyAnalystAgent

    analyst = SmartMoneyAnalystAgent.__new__(SmartMoneyAnalystAgent)
    observations = [SimpleNamespace(symbol="AAA")]
    finding = SimpleNamespace(symbol="AAA")
    cached = {"findings": [{"symbol": "AAA"}]}

    with (
        patch.object(SmartMoneyAnalystAgent, "_evidence_hash", return_value="h"),
        patch.object(SmartMoneyAnalystAgent, "_presented_symbols", return_value={"AAA"}),
        patch.object(SmartMoneyAnalystAgent, "_synthesis_cache_key", return_value="k"),
        patch.object(SmartMoneyAnalystAgent, "_load_cache", return_value={"k": cached}),
        patch.object(SmartMoneyAnalystAgent, "_parse_findings", return_value=([finding], 0)),
        patch.object(SmartMoneyAnalystAgent, "run") as ran,
    ):
        _findings, result, error = analyst.analyze(observations)

    ran.assert_not_called()
    assert error is None
    assert result.provider_requests == 0
    # Not None: a call that never happened is priced, not unknown.
    assert result.cost_usd == 0.0


# ============================================================================
# Item 211 (2026-09-30): a self-clearing fault is ONE episode, not two pages
# ============================================================================


def test_a_latch_that_self_clears_inside_its_window_pages_nobody(
    tmp_path,
    monkeypatch,
):
    """The weekend defect, end to end.

    22 SUSPENDED and 22 RESUMED messages over 26-29 Sep were one provider
    fault flapping [measured, production `notifier_sends`]. A latch that
    expires inside the circuit's own self-clear window must now produce no
    owner message at either edge -- and must still be fully recorded.
    """
    _freeze_et_day(monkeypatch)
    path = _db_path(tmp_path)
    notifier = _Notifier()
    circuit = _latch_on_failed_call(path, notifier=notifier, durable=False)

    assert notifier.messages == []

    _age_latch(path, 16)
    assert circuit.status()["suspended"] is False

    # Neither edge reached him.
    assert notifier.messages == []

    # But the desk can still say exactly what happened.
    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        kinds = [row["event_type"] for row in conn.execute("SELECT event_type FROM llm_circuit_events ORDER BY id")]
    assert "suspend_alert_deferred" in kinds
    assert "auto_reset" in kinds


def test_the_deferral_is_written_once_per_latch_not_once_per_check(
    tmp_path,
    monkeypatch,
):
    """Moving the noise into the database would not be a fix."""
    _freeze_et_day(monkeypatch)
    path = _db_path(tmp_path)
    circuit = _latch_on_failed_call(path, durable=False)

    for _ in range(5):
        circuit.status()

    with sqlite3.connect(path) as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM llm_circuit_events WHERE event_type='suspend_alert_deferred'"
        ).fetchone()[0]
    assert count == 1


def test_the_durable_page_and_the_resume_both_carry_the_episode(
    tmp_path,
    monkeypatch,
):
    """Duration and flap count, on both edges of a DURABLE episode."""
    _freeze_et_day(monkeypatch)
    path = _db_path(tmp_path)
    notifier = _Notifier()
    circuit = _latch_on_failed_call(path, notifier=notifier)

    suspended = next(m for m in notifier.messages if "SUSPENDED" in m)
    assert "episode:" in suspended
    assert "self-clear window" in suspended

    _age_latch(path, 16)
    circuit.status()
    resumed = next(m for m in notifier.messages if "RESUMED" in m)
    assert "episode:" in resumed
    assert "self-cleared" in resumed


def test_a_trigger_needing_an_operator_is_never_held(tmp_path, monkeypatch):
    """Deferring is only ever right where the circuit itself might clear the
    latch. A code outside `_SELF_CLEARING_HARD_TRIGGERS` has no window to
    expire inside, so holding its alert would only delay a page that has to
    happen -- and an unreadable or missing stamp errs the same way."""
    from src.cost_circuit import _SELF_CLEARING_HARD_TRIGGERS

    _freeze_et_day(monkeypatch)
    path = _db_path(tmp_path)
    circuit = LLMCostCircuitBreaker(path, _cooldown_config(), _Notifier())
    fresh = "2999-01-01 00:00:00"

    with circuit._connect() as conn:
        for code in ("operator_manual", "daily_cost_limit", "", None):
            assert code not in _SELF_CLEARING_HARD_TRIGGERS
            assert (
                circuit._suspension_still_inside_self_clear_window_locked(
                    conn,
                    {"trigger_code": code, "suspended_at": fresh},
                )
                is False
            )
        # A self-clearing code with no usable stamp also refuses to hold.
        assert (
            circuit._suspension_still_inside_self_clear_window_locked(
                conn,
                {"trigger_code": "failed_call_unknown_cost", "suspended_at": None},
            )
            is False
        )
        # An unparseable stamp is not something to hold an alert on either.
        assert (
            circuit._suspension_still_inside_self_clear_window_locked(
                conn,
                {"trigger_code": "failed_call_unknown_cost", "suspended_at": "not-a-timestamp"},
            )
            is False
        )
        # ...but the same code, stamped a moment ago, IS held.
        now_stamp = trading_calendar.sql_utc_stamp()  # the circuit's own clock, which the test may have frozen
        assert (
            circuit._suspension_still_inside_self_clear_window_locked(
                conn,
                {"trigger_code": "failed_call_unknown_cost", "suspended_at": now_stamp},
            )
            is True
        )
        # And once it has outlived the window, it is not.
        assert (
            circuit._suspension_still_inside_self_clear_window_locked(
                conn,
                {"trigger_code": "failed_call_unknown_cost", "suspended_at": "2000-01-01 00:00:00"},
            )
            is False
        )


class _MutedNotifier(_Notifier):
    """A notification transport the operator deliberately switched off."""

    enabled = False

    def send(self, message: str, **_kwargs) -> bool:
        self.messages.append(message)
        return False


def test_muted_transport_does_not_suspend_paid_analysis(tmp_path):
    """Muting alerts must never halt the desk while the durable record works.

    Regression: TELEGRAM_DISABLED took the whole desk offline because the
    circuit's precondition was "Telegram is enabled" rather than "a mandatory
    alert is durably recorded and surfaced".
    """

    db_path = tmp_path / "qamc.db"
    circuit = LLMCostCircuitBreaker(str(db_path), _config(), notifier=_MutedNotifier())

    # No suspension, and the paid boundary is usable.
    assert circuit.status().get("suspended") is not True
    circuit.require_paid_analysis("analysis")
    # The durable, dashboard-visible record surface is writable.
    assert circuit._emergency_latch_path is not None
    assert circuit._emergency_latch_path.exists() is False


def test_no_durable_record_surface_still_latches(tmp_path):
    """The genuine case keeps failing closed: nowhere to deliver OR record."""

    circuit = LLMCostCircuitBreaker(":memory:", _config(), notifier=_MutedNotifier())

    with pytest.raises(PaidAnalysisSuspended):
        circuit.require_paid_analysis("analysis")


def test_operator_reset_records_duration_and_tells_the_owner(tmp_path):
    """A manual reset must be as observable as the automatic one: the event
    row carries how long the suspension lasted and the suspension's own alert
    state, and the owner gets the resume note he gets for an auto-clear."""

    path = _db_path(tmp_path)
    notifier = _Notifier()
    circuit = _latch_on_failed_call(path, notifier=notifier)
    # Short of the auto-clear cooldown, so this is the operator path and the
    # elapsed figure is a real backdated interval rather than ~0.
    _age_latch(path, 7)
    assert any("SUSPENDED" in m for m in notifier.messages)

    circuit.reset("operator verified the provider fault and cleared it")

    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        row = dict(conn.execute("SELECT * FROM llm_circuit_events WHERE event_type='reset'").fetchone())
    assert "operator reset after a suspension of" in row["detail"]
    assert "7." in row["detail"] or "6." in row["detail"]
    # Captured before the UPDATE zeroed it, so the pairing rule is answered
    # from what was true, not from the column default.
    assert row["suspension_alert_state"] == 1
    assert row["recovery_alert_state"] == 1

    resumed = [m for m in notifier.messages if "RESUMED" in m]
    assert len(resumed) == 1
    assert "after an operator reset" in resumed[0]
    assert "no operator reset was needed" not in resumed[0]
    assert "operator reset after a suspension of" in resumed[0]

    # Idempotent: a second boundary must not re-announce the same reset.
    circuit._notify_auto_resets_if_needed()
    assert len([m for m in notifier.messages if "RESUMED" in m]) == 1


def test_pre_existing_operator_resets_are_not_re_announced(tmp_path):
    """Resets already on the books cleared before this alert existed; the
    one-time backfill marks them handled so no stale 'RESUMED' burst fires."""

    path = _db_path(tmp_path)
    with sqlite3.connect(path) as conn:
        # Simulate a database written before this change: the row is pending
        # (0) and the schema counter has not been stamped.
        conn.execute(
            "INSERT INTO llm_circuit_events "
            "(event_type, detail, run_id, mode, agent_name, attempts, "
            "session_cost_usd, daily_cost_usd, recovery_alert_state, "
            "recovery_alert_updated_at, created_at) VALUES "
            "('reset', 'old operator reset', 'r', 'operator', 'operator', "
            "0, 0, 0, 0, ?, ?)",
            (trading_calendar.sql_utc_stamp(), trading_calendar.sql_utc_stamp()),
        )
        conn.execute("PRAGMA user_version = 0")

    notifier = _Notifier()
    circuit = LLMCostCircuitBreaker(path, _cooldown_config(), notifier)
    circuit._notify_auto_resets_if_needed()
    assert not [m for m in notifier.messages if "RESUMED" in m]
    with sqlite3.connect(path) as conn:
        assert (
            conn.execute("SELECT recovery_alert_state FROM llm_circuit_events WHERE event_type='reset'").fetchone()[0]
            == 1
        )
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 1


def test_a_relatch_of_the_same_fault_in_one_episode_pages_the_owner_once(
    tmp_path,
    monkeypatch,
):
    """Item 211 defect 1. The first coalescing attempt keyed suppression to
    the self-clear WINDOW, which is a duration and the wrong quantity: on
    production data all 22 suspension episodes of 26-30 Sep ran 24 to 253
    minutes, so not one was inside its window and all 44 messages still
    paged [measured, production `llm_circuit_events`].

    Suppression now spans the EPISODE — one trigger code inside one ET
    budget day, the boundary `_episode_facts_locked` already reports on.
    The owner is told once when the fault starts; a re-latch of the same
    unresolved fault is not a second incident. No duration is invented.

    Fails on the old behaviour: the second durable latch sent a second
    "SUSPENDED" note.
    """
    _freeze_et_day(monkeypatch)
    path = _db_path(tmp_path)
    notifier = _Notifier()

    # First latch of the day: durable, so it earns its page.
    circuit = _latch_on_failed_call(path, notifier=notifier, durable=True)
    assert len([m for m in notifier.messages if "SUSPENDED" in m]) == 1
    _age_latch(path, 16)
    assert circuit.status()["suspended"] is False
    assert len([m for m in notifier.messages if "RESUMED" in m]) == 1

    # Same fault latches again, same budget day, and outlasts its window
    # exactly as every real episode did.
    circuit = _latch_on_failed_call(path, notifier=notifier, durable=True)
    assert len([m for m in notifier.messages if "SUSPENDED" in m]) == 1, (
        "a re-latch of one unresolved fault must not page the owner twice"
    )

    # Nothing vanished: the suppression is on the record.
    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        held = [
            row["detail"]
            for row in conn.execute("SELECT detail FROM llm_circuit_events WHERE event_type='suspend_alert_deferred'")
        ]
    assert any("already paged him in this ET budget day" in d for d in held)
