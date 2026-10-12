"""Risk-adjacent sections: cash sweep, cash reserve and scheduled-event risk. RiskConfig itself stays in the package root (a single 516-line class; new files are capped at the 400-line floor).

Moved verbatim from src/config/__init__.py (pure move; bodies AST-identical).
"""

from pydantic import BaseModel, Field, field_validator, model_validator


class CashSweepConfig(BaseModel):
    """Idle-cash sweep into a T-bill ETF (default SGOV).

    The sweep vehicle is treated as CASH-EQUIVALENT everywhere: excluded
    from every LLM-facing position view, excluded from risk-engine exposure
    math (its market value counts toward cash in the cash_only filter),
    exempt from stop-coverage audits (it deliberately carries no stop), and
    force_delever liquidates it FIRST. Deterministic and zero-LLM — the
    LLM never decides to park or unpark; the pipeline bookends do.
    """

    enabled: bool = False
    """Master switch. False = the sweeper is inert everywhere (no view
    filtering, no funding sells, no parking buys)."""

    symbol: str = "SGOV"
    """The parking vehicle. Must be a cash-like T-bill ETF (SGOV/BIL);
    anything with real market beta breaks the cash-equivalence assumption
    that justifies every exemption listed above."""

    @field_validator("symbol")
    @classmethod
    def _symbol_nonempty(cls, v: str) -> str:
        v = (v or "").strip().upper()
        if not v:
            raise ValueError("cash_sweep.symbol must be a non-empty ticker")
        return v


class CashReserveConfig(BaseModel):
    """The raw-cash reserve band the /account liquidity view reports.

    RELOCATED 2026-10-01 (board item 190) out of `CashSweepConfig`, value
    unchanged. It was never part of the retired cash sweep's own machinery:
    `src.api.routes_live._compute_liquidity` reads it on every /account
    request to report `reserve_usd` and `cash_above_reserve`, and that
    reader outlives the sweep. Kept here so retiring the rest of the sweep
    cannot delete a live display band by association.
    """

    pct: float = Field(default=1.0, ge=0, le=20)
    """% of equity reported as held back as raw cash for fees, slippage and
    partial fills. Deliberately 1.0 — an earlier pass in the 2026-08-19
    tranche raised it to 5.0 as a workaround for BUYs being skipped for lack
    of cash, which treated a symptom and was put back. Still `arbitrary` in
    config/number_ledger.yaml; relocation changed its home, not its value or
    its honesty label."""


class EventRiskConfig(BaseModel):
    """Scheduled-event lookups that ground the Risk Manager's mandatory
    `event_risk` check (`src/data/event_calendar.py`).

    Added because that check was previously answered from the model's own
    memory: `MarketDataProvider.get_next_earnings_date` had zero callers, and
    no module fetched a macro release calendar at all. The numbers here are
    ceilings, not tuning knobs — a session must never be delayed, and must
    certainly never hang, because a nice-to-have calendar was slow. The FRED
    retry/backoff policy itself is NOT duplicated here: the calendar hits the
    same host as `src/data/macro.py` with the same failure mode, so
    `src/pipeline.py` threads the existing `macro.*` retry settings into it and
    only the deadline below is calendar-specific.
    """

    horizon_days: int = Field(default=10, ge=1, le=60)
    """How far ahead the macro release calendar looks, in calendar days. 10
    covers "the next few sessions" the `event_risk` field asks about with
    enough margin to see a release the desk should already be positioning
    around, without burying the seat in rows it will skim past."""

    calendar_deadline_s: float = Field(default=20.0, ge=1.0, le=120.0)
    """Hard wall-clock ceiling for one `get_upcoming_events()` call. Much
    tighter than `macro.total_fetch_deadline_s` (90s) on purpose: the macro
    summary is load-bearing for the regime call, this calendar is an
    advisory layered on top of a session that must not wait for it. Enforced
    the same way — every request timeout and every backoff sleep is clipped to
    the remaining budget, and releases not yet started are skipped and reported
    as `fetch_deadline_exceeded` rather than silently omitted."""

    earnings_deadline_s: float = Field(default=20.0, ge=1.0, le=120.0)
    """Hard wall-clock ceiling for the whole per-symbol earnings-date sweep.
    Symbols not reached inside it come back labelled
    `unavailable_deadline_exceeded`, never dropped."""

    earnings_symbol_timeout_s: float = Field(default=8.0, ge=0.5, le=60.0)
    """Per-symbol ceiling on the earnings-date lookup. `yfinance`'s calendar
    call has no timeout of its own — the same hang risk `get_ohlcv` /
    `get_valuation_metrics` are already `ThreadPoolExecutor`-bounded against."""

    fomc_request_timeout_s: float = Field(default=10.0, ge=1.0, le=60.0)
    """Per-request timeout for the Federal Reserve's own FOMC calendar. Its own
    setting rather than a reuse of `macro.request_timeout_s` because this is a
    different host with a different failure mode — federalreserve.gov, not
    FRED. The backoff CURVE is still taken from `macro.*`: that is a generic
    retry policy, not a fact about either host."""

    fomc_max_retries: int = Field(default=2, ge=0, le=5)
    """Retries per Fed calendar URL before that source is given up on."""

    fomc_deadline_s: float = Field(default=15.0, ge=1.0, le=120.0)
    """Hard wall-clock ceiling for one `FOMCCalendarProvider.get_meetings()`
    call, covering BOTH the JSON feed and the fallback page. Same enforcement
    as the macro calendar: every request timeout and every backoff sleep is
    clipped to what remains, and a source not reached inside the budget is
    reported as a named absence rather than silently skipped."""

    fomc_cache_ttl_days: float = Field(default=7.0, ge=0.0, le=90.0)
    """How long a cached FOMC schedule is trusted without a refetch. FOMC dates
    are published a year ahead and change perhaps twice a year, so a weekly
    refresh is generous. Freshness alone is never sufficient: a cache is used
    without fetching only if it ALSO spans `horizon_days`, and an expired cache
    is still served — clearly labelled `measured_from_stale_cache`, with its
    age — when the live sources are unreachable."""

    fomc_cache_path: str = Field(default="data/fomc_calendar.json")
    """Where that cache lives. Relative by design, like the other on-disk
    caches (`data/company_profiles.json`, `data/news`, ...), so the rehearsal
    rig's chdir-based filesystem wall redirects it into the sandbox."""

    @model_validator(mode="after")
    def _deadlines_are_well_formed(self):
        if self.earnings_deadline_s < self.earnings_symbol_timeout_s:
            raise ValueError(
                "event_risk.earnings_deadline_s must be >= "
                "earnings_symbol_timeout_s — a sweep budget shorter than one "
                "symbol's own timeout would abandon every symbol before it "
                f"could answer; got {self.earnings_deadline_s} < "
                f"{self.earnings_symbol_timeout_s}"
            )
        if self.fomc_deadline_s < self.fomc_request_timeout_s:
            raise ValueError(
                "event_risk.fomc_deadline_s must be >= fomc_request_timeout_s "
                "— a deadline shorter than one request's own timeout would "
                "abort every fetch immediately without ever really trying; got "
                f"{self.fomc_deadline_s} < {self.fomc_request_timeout_s}"
            )
        return self
