"""LLM spend circuit settings: per-session and per-day cost ceilings and the paid-run count they derive from.

Moved verbatim from src/config/__init__.py (pure move; bodies AST-identical).
"""

from pydantic import BaseModel, Field, model_validator
from src.agents.base import (
    provider_attempt_budget,
)
from src import infra_retry_policy as _retry_policy
from src.trading_calendar import SESSION_WINDOWS


#: The intraday control's tick spacing, in minutes. Duplicated from
#: `src/scheduler.py::_build_intra_check_trigger`, which is the authority;
#: pinned here so the two are at least visible in one grep, and asserted
#: equal by `tests/test_cost_circuit.py`.
INTRA_CHECK_TICK_MINUTES = 30


def _paid_run_count() -> int:
    """How many scheduled runs in a trading day can make a paid call.

    Every canonical session window, with `intra_check` counted once per
    30-minute tick because it fires that often.

    `src/trading_calendar.py` used to label that window "no LLM" and an
    earlier draft of this number excluded it on that basis. The label is
    false: on the production DB intra_check is the desk's LARGEST paid
    cost centre -- $0.5672 of the day's $0.7883 on 2026-09-22 (72%) and
    $2.3839 of $2.6478 on 2026-09-21 (90%), 13-14 paid sessions a day
    [measured 2026-09-23, llm_budget_sessions]. The 2026-09-22 latch this
    whole change exists for was tripped BY an intra_check run. Excluding
    it would derive a paid-call number by leaving out most of the paid
    calls. Currently 19: 14 ticks plus earnings_preprocess, morning,
    midday, close and evening.
    """
    lo_min, hi_min = SESSION_WINDOWS["intra_check"]
    intra_ticks = len(range(lo_min, hi_min + 1, INTRA_CHECK_TICK_MINUTES))
    return intra_ticks + len([m for m in SESSION_WINDOWS if m != "intra_check"])


class LLMCostCircuitConfig(BaseModel):
    """Fail-closed limits for every paid model request.

    These are deliberately configuration values (visible and testable), but
    disabling the breaker is not supported by production settings.  The
    optional ``enabled`` field exists for isolated unit fixtures and defaults
    on so older settings files acquire protection automatically.
    """

    enabled: bool = True
    require_telegram_alerts: bool = True
    session_cost_limit_usd: float = Field(default=0.90, gt=0, allow_inf_nan=False)
    # The close session ("close") is exempt from THIS cap only (owner ruling
    # 2026-10-14; src/cost_circuit/free_route.py DAILY_CAP_EXEMPT_MODES). Its
    # session cap and the call-count backstop above still apply to it.
    daily_cost_limit_usd: float = Field(default=1.50, gt=0, allow_inf_nan=False)
    # Last OpenRouter top-up (amount, ET date). Only used when the provider
    # will not report a balance; see src/llm_balance_runway.py.
    openrouter_topup_usd: float | None = None
    openrouter_topup_date: str | None = None
    # Item 14 (OWNER-APPROVED 2026-09-02, docs/WORK.md): the per-call cost
    # RESERVATION layer -- and every exposure ceiling / per-mode allowance /
    # afternoon reserve / free-failure-session backstop that existed only
    # to manage a reservation's over-holding -- is deleted. Real calls
    # settled at a median 0.38x of the pinned worst-case reservation rate,
    # so that machinery held ~2.6x what was ever spent and stopped the desk
    # on money that was never spent. What replaces it, exactly:
    #   (a) a spend cap on the OpenRouter API key itself -- outside this
    #       codebase; see docs/WORK.md item 14(a). NOT implemented here.
    #   (b) `session_cost_limit_usd` / `daily_cost_limit_usd` above, checked
    #       against REAL SETTLED cost only (no projection) by
    #       `LLMCostCircuitBreaker._enforce_settled_limits_locked`.
    #   (c) `max_calls_per_session` below -- a count-based runaway-loop
    #       backstop, independent of price.
    #
    # Set 2026-09-03 from real production data: the worst COMPLETE session
    # ever recorded made 14 calls, almost all of it tech_analyst chunking
    # the symbol universe, not the portfolio_manager (always exactly 1 call
    # per session). 40 is ~3x that measured ceiling -- a first number, not
    # a final one; see the DECIDE BY line in docs/WORK.md.
    max_calls_per_session: int = Field(default=40, ge=1)
    # Ceiling on provider attempts within ONE logical agent call, counting
    # the initial request. NOT an independent number: it must cover what
    # `BaseAgent.run()`'s retry loop can actually spend, or the circuit trips
    # on the loop's own designed behaviour instead of on anything unsafe.
    # Derived by default from `provider_attempt_budget()`, which owns that
    # arithmetic; `AppConfig._check_provider_attempt_budget` rejects any
    # explicit value below it at load time. See the 2026-08-31 incident
    # recorded on `provider_attempt_budget`.
    #
    # Setting it HIGHER than the derived floor is allowed and does not grant
    # extra attempts — the retry loop, not this ceiling, decides how many
    # requests are made. This only decides when the circuit intervenes.
    max_provider_attempts_per_call: int = Field(
        default_factory=lambda: provider_attempt_budget(
            failover_available=True,
            tertiary_available=True,
        ),
        ge=1,
    )
    # === Transient-latch self-clear (Defect B, 2026-09-22) ===
    # A hard latch raised by a FAILED provider call whose cost could not be
    # proven used to wait for a human. On 2026-09-22 that cost the desk the
    # close and evening runs and nine hours of refused analysis on $0.7883 of
    # a $2.75 day.
    #
    # THE COOLDOWN'S ADMISSIBLE INTERVAL, and why the midpoint:
    #   Upper bound 30 min -- the gap between consecutive paid runs, which
    #     is the intra_check tick, the most frequent paid run there is
    #     [measured: 13-14 paid intra_check sessions a day]. At or above it
    #     a second paid run is lost, which is the damage being fixed.
    #   Lower bound 0 -- there is no run-duration floor. The run that trips
    #     the latch STOPS at the trip (`_trip_locked` suspends its session
    #     and every later `begin_call` raises); the 2026-09-22 tripper ran
    #     1.47 min end to end [measured]. Too short is not unsafe, it just
    #     wastes the day's allowance re-failing against a provider that is
    #     still down.
    # 15.0 is the midpoint of (0, 30): the value furthest from both failure
    # modes, and the one most tolerant of run-start jitter and clock skew in
    # either direction. Two earlier derivations were wrong and are recorded
    # so the number is not re-derived from them: "half the intra tick"
    # (right value, but justified by an aesthetic half) and "bracketed by
    # the longest run at 9.8 min and the 90-min earnings-to-morning gap"
    # (wrong on both ends -- the tripping run does not continue, and 90 min
    # only looks like the smallest gap if intra_check is wrongly excluded).
    transient_latch_cooldown_minutes: float = Field(default=15.0, gt=0, allow_inf_nan=False)
    # One forgiveness per scheduled PAID run in a trading day. A fault
    # recurring past that has outlasted every paid run of the day and is not
    # a transient blip, so the next occurrence latches durably and waits for
    # a human -- which is also what bounds how many unproven-cost calls a
    # single day can forgive without one.
    max_transient_latch_auto_clears_per_day: int = Field(
        default_factory=lambda: _paid_run_count(),
        ge=1,
    )
    # === OpenRouter pricing staleness grace window (SPOF fix, 2026-08-28) ===
    # Before this fix, `cost_table.refresh_openrouter_pricing()` accepted a
    # cached rate ONLY while under 24h old. Past that boundary it had to
    # reach openrouter.ai/api/v1/models or return False, and both
    # `TradingPipeline.__init__` and `activate_paid_call_session()` respond
    # to False with `breaker.mark_unavailable(...)` -- the durable,
    # cross-process emergency latch that `LLMCostCircuitBreaker.reset()`
    # (operator-only, reason mandatory) is the sole way to clear. Because the
    # cache file is only rewritten when a fetch actually happens, and a fetch
    # only happens once the cache is ALREADY stale, this meant one
    # openrouter.ai outage overlapping the first session after the 24h mark
    # -- verified reproducible 2026-08-28 via
    # test_mandatory_openrouter_refresh_rejects_stale_cache_when_network_is_down
    # -- could stop every future session, including the next day's, until a
    # human ran `reset()` by hand. The desk runs unattended specifically
    # because the owner cannot be relied on to intervene quickly, so a
    # guardrail whose failure mode is "wait for a human" defeats the reason
    # it exists.
    #
    # A price that turned stale five minutes ago is a different fact from a
    # price nobody has ever fetched: OpenRouter's routed rates change on the
    # order of once a quarter, not hour to hour. So: within this many hours
    # PAST the 24h freshness boundary, a cache that can't be refreshed live
    # is used rather than latched -- widened per
    # `openrouter_pricing_stale_multiplier_max` below and logged loudly on
    # every call -- and only a cache older than 24h + this grace, or no
    # cache at all, or a cache missing a rate for a model actually
    # configured, still fails closed exactly as before. 0 restores the
    # pre-fix behaviour (fail the instant the cache turns stale) for anyone
    # who wants it back. Independent of item 14: this bounds the pricing
    # CATALOG's own staleness, not a call's dollar reservation (deleted).
    openrouter_pricing_grace_period_hours: float = Field(
        default=24.0,
        ge=0.0,
        le=168.0,
        allow_inf_nan=False,
    )
    # Multiplier applied to a stale-but-in-grace rate at the FAR edge of the
    # grace window above (`cost_table.openrouter_pricing_reservation_
    # multiplier` scales linearly up to this value as the cache ages toward
    # the end of grace) -- still used for the estimated-cost fallback in
    # `estimate_cost()` when a provider does not report its own cost, even
    # though item 14 removed the per-call reservation this was originally
    # sized for.
    openrouter_pricing_stale_multiplier_max: float = Field(
        default=1.50,
        ge=1.0,
        le=5.0,
        allow_inf_nan=False,
    )
    # === Infrastructure-fault retry (docs/WORK.md item 17a, 2026-09-03) ===
    # Before this, ANY exception while reading/seeding the ledger --
    # "I cannot read the budget", e.g. a transient SQLite lock or disk I/O
    # error -- was treated exactly like "I am over budget" (a real, measured
    # breach): both latched paid analysis via the durable file marker on the
    # very first occurrence, requiring an operator to clear it by hand. A
    # real breach still latches immediately and correctly (`_trip_locked`
    # writes the in-DB `llm_circuit_state` row, unaffected by this block).
    # This block only bounds retries for the DB-open/read path itself
    # (`LLMCostCircuitBreaker._run_with_infra_retry`, used by construction,
    # `activate_session`, and `enforce_current_limits`) before IT escalates
    # to the same durable latch.
    #
    # Shape and defaults mirror `MacroConfig` above (`max_retries`,
    # `retry_backoff_base_s`, `retry_backoff_max_s`, `retry_backoff_jitter_s`)
    # -- the same "bounded exponential-backoff retry before a harder failure
    # mode" pattern this codebase already uses for FRED's transient network
    # faults (`MacroDataProvider._next_backoff`), reused rather than a fresh
    # number invented for this circuit.
    infra_fault_max_retries: int = Field(default=_retry_policy.MAX_RETRIES, ge=0, le=5)
    """Bounded retries for a transient cost-circuit infrastructure fault
    BEFORE it escalates to the durable emergency latch. Mirrors
    `MacroConfig.max_retries`."""

    infra_fault_retry_backoff_base_s: float = Field(default=_retry_policy.BACKOFF_BASE_S, gt=0, le=30.0)
    """First retry's backoff, in seconds; doubles each subsequent retry,
    capped at `infra_fault_retry_backoff_max_s`. Mirrors
    `MacroConfig.retry_backoff_base_s`."""

    infra_fault_retry_backoff_max_s: float = Field(default=_retry_policy.BACKOFF_MAX_S, gt=0, le=60.0)
    """Ceiling on the exponential backoff. Mirrors
    `MacroConfig.retry_backoff_max_s`."""

    infra_fault_retry_backoff_jitter_s: float = Field(default=1.0, ge=0, le=10.0)
    """Uniform jitter, 0..this many seconds, added to every backoff sleep.
    Mirrors `MacroConfig.retry_backoff_jitter_s`."""

    @model_validator(mode="before")
    @classmethod
    def _reject_deleted_reservation_keys(cls, data):
        # Item 14 (OWNER-APPROVED 2026-09-02, docs/WORK.md): the per-call
        # cost reservation layer -- and every key below that existed only
        # to manage its over-holding -- is deleted. BaseModel's default
        # `extra="ignore"` would let a settings.yaml still carrying one of
        # these load silently, quietly dropping whatever an operator set.
        # Fail loudly instead of drifting doc-versus-behaviour again.
        removed_keys = {
            "max_paid_sessions_per_mode_per_day",  # pre-2026-08-29 name
            "session_reserved_exposure_limit_usd",
            "daily_reserved_exposure_limit_usd",
            "max_free_failure_sessions_per_mode",
            "backstop_cooloff_minutes",
            "max_mode_daily_exposure_pct",
            "afternoon_reserve_pct",
            "afternoon_reserve_release_et_hour",
            "max_retry_attempts_per_session",
            "reservation_ttl_minutes",
            "reservation_multiplier",
            "reservation_min_history_samples",
            "reservation_conservative_percentile",
            "reservation_output_margin",
        }
        if isinstance(data, dict):
            present = sorted(removed_keys & set(data))
            if present:
                raise ValueError(
                    "llm_cost_circuit no longer supports: "
                    + ", ".join(present)
                    + " -- item 14 (2026-09-02, docs/WORK.md) deleted the "
                    "per-call cost reservation layer these configured. Remove "
                    "them from the settings file; see max_calls_per_session "
                    "for the replacement runaway-loop backstop."
                )
        return data

    @model_validator(mode="after")
    def _daily_not_below_session(self):
        if self.enabled is not True:
            raise ValueError("llm_cost_circuit.enabled must remain true; paid-analysis protection is mandatory")
        if self.require_telegram_alerts is not True:
            raise ValueError(
                "llm_cost_circuit.require_telegram_alerts must remain true; "
                "a durably recorded, operator-visible shutdown notification is "
                "mandatory (a muted transport alone no longer suspends the desk)"
            )
        if self.daily_cost_limit_usd < self.session_cost_limit_usd:
            raise ValueError("daily_cost_limit_usd must be >= session_cost_limit_usd")
        return self
