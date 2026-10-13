"""Per-run context + structured PM facts.

Previously `TradingPipeline` stashed cross-stage data on its own instance
(``self._last_symbols_bars``, ``self._bg_threads``). That conflated per-run
state with the long-lived service container, making runs non-reentrant,
hard to test, and hard to reason about when one stage's output is
another stage's input.

`RunContext` is an explicit container created at the start of each run.
Every stage reads from it and writes to it by field name. Stages become
functions of ``(ctx, deps) -> ctx-with-fields-filled-in`` rather than
methods that rely on implicit attributes of the enclosing instance.

This module ships the dataclass only — it does not (yet) refactor the
pipeline into explicit stages. That's Phase 2 of the architecture work.
For Phase 1 the goal is just to remove implicit state and give each run
its own mutable snapshot.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime

from src.trading_calendar import utc_now
from typing import TYPE_CHECKING, Literal

from src.models import parse_telemetry
from src.soft_exit_restore_buffer import open_restore_run

if TYPE_CHECKING:
    from src.data.event_calendar import EventCalendarCoverage, FOMCCoverage
    from src.data.macro import MacroCoverage
    from src.models import NewsIntelligenceReport, PortfolioDecision, Position
    from src.risk.metrics import PortfolioHeat

logger = logging.getLogger(__name__)

SessionType = Literal["morning", "midday", "close", "evening", "intra_check", "earnings_preprocess"]


@dataclass
class RunContext:
    """Per-run snapshot of everything a session needs.

    Not frozen — stages populate fields as the run progresses. Discipline is
    "each field has one owning stage that writes it; other stages read only."
    """

    run_id: str
    session: SessionType
    started_at: datetime = field(default_factory=lambda: utc_now().replace(tzinfo=None))

    # === Set at the start of each run (broker snapshot) ===
    account: dict = field(default_factory=dict)
    positions: list = field(default_factory=list)  # list[Position]
    cash: float = 0.0
    # What QAMC can deploy into equities WITHOUT borrowing: raw `cash`
    # plus the market value of the cash-equivalent sweep vehicle, which is
    # liquidated before the BUY phase. Both are assets already owned, so
    # this can never exceed equity and never implies margin.
    #
    # Verified Alpaca semantics (2026-08-19): `cash` is credited as soon as
    # a SELL FILLS, so filled sweep proceeds fund an equity BUY the same
    # session. `non_marginable_buying_power` is the settled/crypto figure
    # and lags a same-day equity sale by a business day — it is NOT the
    # right field for equity sizing. `buying_power`/`regt_buying_power` are
    # margin figures (~2x equity here) and must never be used.
    #
    # This is a PLANNING figure for PM / RM / the pre-trade gate.
    # ExecutionStage still re-reads raw broker `cash` after the funding
    # sale and skips any BUY that cash does not actually cover — that
    # deterministic backstop is unchanged and remains authoritative.
    # See TradingPipeline._compute_deployable_cash.
    deployable_cash: float = 0.0
    total_value: float = 0.0
    last_equity: float = 0.0

    # === Populated by the research stage (parallel fan-out) ===
    macro_summary: dict = field(default_factory=dict)
    macro_analysis: dict | None = None  # Macro Analyst's LLM output (model_dump)
    # How many of the configured FRED series actually returned data this run
    # (src.data.macro.MacroCoverage) — Phase 4.2 fix. Kept alongside
    # macro_summary rather than folded into data_status because it carries
    # the per-series detail (which series, why) that a single status word
    # can't; data_status["macro"] is the summary, this is the evidence
    # behind it.
    macro_coverage: "MacroCoverage | None" = None
    # Scheduled macro releases landing inside this run's event horizon, and how
    # much of the configured release set actually returned a schedule
    # (src.data.event_calendar). Fetched once by the research stage and read
    # again by RiskStage, so one session issues one calendar sweep rather than
    # two. The pair is load-bearing TOGETHER: an empty `macro_events` means
    # "nothing scheduled" ONLY when `macro_event_coverage.status == "ok"`;
    # with coverage None or degraded it means NOT FETCHED, and every renderer
    # must say which. Defaults (empty list / None) are the honest "not fetched
    # this run" state for every session that never populates them.
    macro_events: list = field(default_factory=list)  # list[MacroEvent]
    macro_event_coverage: "EventCalendarCoverage | None" = None
    # The forward FOMC meeting schedule and where it came from
    # (src.data.event_calendar.FOMCCalendarProvider). Same pairing rule and
    # same reason as the two fields above: an empty `fomc_meetings` means "no
    # meeting scheduled" ONLY when `fomc_coverage` says a published schedule
    # actually spans the horizon; with coverage None or degraded it means NOT
    # FETCHED. Fetched once by the research stage and read again by RiskStage
    # so one session issues one Fed calendar fetch, not two.
    fomc_meetings: list = field(default_factory=list)  # list[FOMCMeeting]
    fomc_coverage: "FOMCCoverage | None" = None
    news_intel: "NewsIntelligenceReport | None" = None
    analyses: list = field(default_factory=list)  # list[TechAnalysisResult]
    #: {symbol: why} — the technical seat returned a row for this name and
    #: the row could not be read. Board item 220. Distinct from a name the
    #: seat was never asked about: both are "no answer", but only this one
    #: had an answer and lost it, and the owner-facing record must say which.
    tech_unreadable: dict = field(default_factory=dict)
    #: Symbols the technical seat WAS asked about and that produced nothing
    #: usable at all — no row to call unreadable. The third of the three
    #: causes, kept apart from `tech_unreadable` and from "never asked".
    tech_unanswered: set = field(default_factory=set)
    earnings_results: list[dict] = field(default_factory=list)
    smart_money_observations: list = field(default_factory=list)
    smart_money_findings: list = field(default_factory=list)
    smart_money_provider_error: str | None = None
    # Run-scoped BUY eligibility granted only by deterministic SEC Form 4
    # admission. Never written back to config.trading.universe and never
    # authored by an LLM.
    admitted_symbols: set[str] = field(default_factory=set)
    #: {symbol: [blocking seats that did not answer about it]} — the per-name
    #: reading of `evidence_gate.BLOCKING_SEATS`, written by
    #: `_record_name_coverage`. Board item 220.
    name_coverage_blocking_gaps: dict = field(default_factory=dict)
    smart_money_admissions: dict[str, dict] = field(default_factory=dict)
    # Conviction ledger (spec §9.5): {SYMBOL: {seat: {"conviction", "observation"}}}
    # for every raw nomination this run produced, seat names already
    # canonicalized (`src.conviction_ledger.normalize_seat`). Written by
    # MorningResearchStage's nomination responder pass and read by
    # DecisionStage, which is where a decision_id finally exists to record
    # the stances against. Carried on the context rather than re-read from
    # the evidence table because the pass already holds the typed objects —
    # and because a read-back would make a forensic concern depend on a
    # write having succeeded. Empty for every session that runs no
    # nominations (intraday, close, evening), which is the honest state.
    nomination_convictions: dict[str, dict[str, dict]] = field(default_factory=dict)
    symbols_bars: dict = field(default_factory=dict)  # {sym: list[OHLCV]}
    valuations: dict = field(default_factory=dict)  # {sym: {trailing_pe, ...}}
    # Bar-fetch coverage for this run's tech universe: {"universe": N,
    # "bars_fetched": N, "bars_missing": N, "bars_missing_symbols": [...]}.
    # Populated by MorningResearchStage._run_tech (the only place
    # `MarketDataProvider.get_ohlcv` is called per-symbol for the full
    # universe) and read back once the tech future resolves to build the
    # `levels_coverage` evidence row — see the long comment there.
    #
    # Added 2026-09-02 investigating whether 2026-09-01's zero-trade day
    # could recur through a silent bars outage. It could not have BEEN that
    # day (`TechAnalysisResult.computed_levels` did not exist in the code
    # that ran that morning), but the fix shipped that same night made
    # `computed_levels` load-bearing: `derive_structural_target` in
    # src/data/levels.py now hard-refuses any trade when it is empty
    # (REFUSAL_NO_STRUCTURE). A dead bar feed and a genuinely structureless
    # market both produce that same empty list, and without this, nothing
    # recorded which one happened at RUN level. Since 2026-09-12 the
    # per-symbol half is carried on `TechAnalysisResult.levels_coverage`,
    # and an empty list from unusable history is a DATA fault
    # (`FAULT_NO_STRUCTURE`), not a refusal — see src/data/levels.py.
    tech_bars_coverage: dict = field(default_factory=dict)
    data_status: dict[str, str] = field(default_factory=dict)
    # What this session's LLM-response parsing lost or papered over
    # (src.models.parse_telemetry). Same relationship to `data_status` as
    # `macro_coverage` above: data_status carries the one-word verdict per
    # source, these carry the evidence behind it.
    #
    #   dropped_analyses    {(model, symbol): count} — a parsed item that was
    #                       discarded outright at that moment. Recorded even
    #                       when a retry later recovers the symbol and the
    #                       Portfolio Manager does see it, which is the case
    #                       data_status cannot show at all — so an entry here
    #                       is NOT by itself evidence the name is missing from
    #                       the book, and RiskStage checks before saying so.
    #   null_coerced_fields {(model, field): count} — a defaulted field
    #                       arrived as an explicit null and took its default.
    #                       The object survived; a real input did not. On
    #                       `thesis_invalid_if` that input is the soft-exit
    #                       signal, so the coercion is not free.
    #   hygiene_violations  {(model[provider], kind): count} — item 157's
    #                       runtime check: the tech seat's raw answer carried
    #                       fenced markdown around the JSON, or a row carried
    #                       a key the schema doesn't declare, on a route that
    #                       was actually given a strict schema to honour.
    #                       Never costs the row; recorded because no
    #                       deployed process ever holds a real
    #                       GOOGLE_API_KEY for a pytest-based live check to
    #                       confirm schema enforcement instead (see
    #                       docs/WORK.md item 157).
    #
    # WRITTEN BY RiskStage (not by the research stage): the Portfolio
    # Manager parses after research, so a reading taken any earlier would miss
    # every PM-side loss. RiskStage turns a non-empty pair into the
    # `analysis_parse_loss` / `analysis_parse_loss_recovered` /
    # `analysis_field_nulled` / `tech_answer_hygiene` advisories — the first
    # two split by whether the dropped symbol is in the book RiskStage
    # holds, which is why that reconciliation cannot happen anywhere
    # earlier. The counters behind them are zeroed at the top of
    # MorningResearchStage.
    dropped_analyses: dict[tuple[str, str], int] = field(default_factory=dict)
    null_coerced_fields: dict[tuple[str, str], int] = field(default_factory=dict)
    hygiene_violations: dict[tuple[str, str], int] = field(default_factory=dict)

    # === Populated by the decision stage ===
    # Memory layers built for PM that the RiskStage also needs. Before the
    # 2026-08-13 agent audit these were DecisionStage locals, so the AI Risk
    # Manager was told (in its prompt) to enforce PM's holding-discipline
    # rules while receiving no `days_held`. RiskStage rebuilds them when they
    # are absent, which is the RC2 resume lane — there DecisionStage never
    # runs at all.
    #   position_history:   {symbol: {entry_date, days_held, ...}}
    #   recent_performance: {rolling_5d_pct, rolling_20d_pct, trailing_days}
    position_history: dict = field(default_factory=dict)
    recent_performance: dict = field(default_factory=dict)
    # Spec §11.2 — the session's gross-exposure state, resolved from ACCOUNT
    # STATE ONLY (equity, its high-water mark, the configured cap) in the run
    # preamble, before any LLM work. Deliberately not derived from anything
    # the Portfolio Manager produced: a blank PM response must not leave the
    # desk levered during a drawdown.
    #   {gross_usd, gross_x, ceiling_x, base_ceiling_x, drawdown_pct,
    #    distance_to_forced_liquidation_pct, alert_owner, reason}
    leverage: dict = field(default_factory=dict)

    # Spec §11.2 (item 112) — THIS session's fresh per-seat read, the canonical
    # {symbol: {seat: stance}} evidence registry the Portfolio Manager was
    # actually shown, stashed by DecisionStage so the post-decision
    # conviction de-lever can cut the WEAKEST-by-conviction names first
    # instead of a stale persisted stance. Absent on every PM-less lane
    # (early return, resume, paid-suspended) — there the preamble margin
    # floor is the sole enforcer, which is correct.
    evidence_registry: dict[str, dict[str, str]] = field(default_factory=dict)
    # §9.4 freshness, stashed beside the registry it belongs to: the exact
    # {symbol: {source}} set `PortfolioManagerAgent.stale_evidence_sources`
    # produced this session — which today means an over-age EARNINGS stance
    # and nothing else, since that is the only freshness rule §9.4 has. The
    # conviction de-lever passes it as `ignored_sources` so it counts the
    # same stances the constructor is willing to size on.
    evidence_stale_sources: dict[str, frozenset[str]] = field(default_factory=dict)
    # Item 109, read by item 112's cut order: {symbol: {"macro"}} for every
    # held name whose macro stance is the market-wide BROADCAST rather than a
    # read of its own sector. ONE-SIDED, never merged into the set above: a
    # broadcast stance may not CORROBORATE holding a name — macro alone
    # protecting a position is the thing the 2026-09-25 ruling forbids — while
    # its dissent still counts against it.
    evidence_non_corroborating_sources: dict[str, frozenset[str]] = field(
        default_factory=dict,
    )
    # Item 112 — the RAW `AnalystVerdict`s every seat produced this session,
    # BEFORE `candidate_eligibility` and the conviction bar remove names that
    # may not be BOUGHT today. The de-lever's cut order ranks conviction about
    # a HOLDING, and an entry-admission list is the wrong question for that:
    # the conviction bar's STAY side is opposition-only by owner ruling
    # (2026-09-25), so a held name dropped from the entry survivors has
    # explicitly earned its right to stay.
    seat_verdicts: list = field(default_factory=list)
    # Item 112 — the morning preamble scopes itself to the margin FLOOR and
    # leaves the ORDINARY §11.2 ceiling to the post-decision conviction pass.
    # This is the debt that deferral creates. `_discharge_deferred_gross_
    # ceiling`, called from the morning body's `finally`, pays it on every
    # lane the conviction pass never reached (PM-less early returns, resume,
    # an exception exit), so no lane can silently lose the ordinary ceiling.
    gross_ceiling_deferred: bool = False

    portfolio_decision: "PortfolioDecision | None" = None
    # Transport-successful model output can still fail deterministic parsing,
    # schema, or grounding. Preserve the exact subtype for session status and
    # Telegram instead of collapsing every case to "unparseable".
    analysis_failure_status: str | None = None
    analysis_failure_error: str | None = None
    correlation_matrix: dict = field(default_factory=dict)
    daily_pnl: float = 0.0
    # The invested target the pre-trade `deployment_gap` advisory measured
    # against — the fixed fully-invested mandate, not a macro output.
    invested_target_pct: float | None = None
    # Stage 1 (QAMC correlation plumbing): set once by DecisionStage right
    # after a successful PM call. Threaded through to the risk_manager
    # agent_logs row and every trades row this run produces, so a single id
    # links "this PM proposal" -> "RM's review of it" -> "the orders/trades
    # it resulted in" without relying on (run_id, symbol) uniqueness holding
    # up under future control-flow changes (see DecisionStage.run()). None
    # when DecisionStage never reached a successful PM call this run.
    decision_id: str | None = None
    # Conviction ledger (spec §7.2): the ACTUAL model that answered this
    # run's portfolio_manager call (`AgentResult.model` — corrected Stage
    # 0.5 to mean the model that really responded, not merely the one
    # requested). Threaded to every entry `trades` row this run produces so
    # a later outcome can be traced to which model authored the decision —
    # training-data contamination is the dominant failure mode in this
    # literature (docs/RESEARCH_FINDINGS.md) and any evaluation must record
    # which model produced each result. None alongside decision_id=None.
    decision_model: str | None = None
    # Phase 14b (automatic opportunity-cost rotation, `src/rotation.py`).
    # Written ONCE by DecisionStage when — and only when — it appended a
    # zero-size target for a categorically-ineligible holding to the PM's
    # plan: {held_symbol, new_symbol, new_score, reason, held_reasons,
    # protection_basis, headroom_pct}. Read by ExecutionStage to fire the
    # owner alert the moment that sale is broker-accepted and to record the
    # buy leg's outcome. None on every run where no rotation was proposed
    # (flag off, nothing qualified, or a guard refused it).
    rotation: dict | None = None

    # === Populated by execution stage ===
    orders: list[dict] = field(default_factory=list)
    # Per-BUY skip records: {symbol, reason, detail}. Every deterministic
    # skip in the BUY loop lands here AND as an `execution_skip` evidence
    # row — before this, a risk-approved BUY could die on a log-only
    # `continue` and the funnel/journal/evening-reflection all read the
    # session as a deliberate no-trade (2026-08-19: three approved BUYs
    # skipped as unfunded; evening concluded "generate more ideas").
    execution_skips: list[dict] = field(default_factory=list)
    # One paid research-heal retry per seat per session (owner 2026-09-16).
    heal_paid_retries: dict[str, int] = field(default_factory=dict)
    # Current wire text available to the news seat's one paid heal retry.
    # Set only when a fetch this run actually returned wire (the intraday
    # expiry peek). None means the seat has no honest input and must stay
    # lost rather than be re-asked with nothing.
    heal_news_text: str | None = None
    # Board item 78: what the soft-exit heal actually did to each
    # open/increase name that arrived without a falsifier, keyed by symbol,
    # as `{"outcome": <src.seat_heal HEAL_* code>, "detail": <prose>}`.
    # Drained from the PM by `DecisionStage` right after `decide()`, because
    # the candidate-accounting re-ask calls `decide()` again and resets it.
    # The blank-falsifier refusal quotes this rather than asserting a retry
    # that may never have been attempted.
    soft_exit_heals: dict[str, dict] = field(default_factory=dict)
    # How much of this decision's evidence was read on THIS tick, as
    # `evidence_freshness.EvidenceFreshness.to_evidence()`. Written by the
    # evidence gate, the one path every decision passes through. Owner
    # mandate 2026-09-18 made every seat but the technical one advisory, so
    # a decision can now stand on one fresh seat plus a carried-forward
    # book with every seat reporting green; this is what says so. It is a
    # disclosure and carries no threshold.
    evidence_freshness: dict | None = None
    # Fills path: desk-caused stall after Risk (WS handshake). Catch-up
    # inside the already-approved ceiling is a safety net only, not the
    # product. Repeg stays off. Submit deadline is the sum of programmed
    # waits (auth reconnect-max if the socket was not started during Risk,
    # plus the ratified funding timeouts when a funding sale runs).
    desk_latency_stall: bool = False
    catch_up_used: dict[str, bool] = field(default_factory=dict)
    entry_submit_budget_s: float = 0.0
    entry_submit_started_mono: float | None = None
    entry_submit_deadline_mono: float | None = None
    # BUY/SHORT slippage ceilings pinned at ExecutionStage start (post-Risk).
    # A later last-trade must not raise this cap — that would be a chase.
    approved_entry_ceiling: dict[str, float] = field(default_factory=dict)

    # Sector map for THIS run. `None` = never recorded; `{}` = recorded
    # empty. It lived on the long-lived pipeline, so a run exiting before
    # the projected preview left the LAST run's sectors readable. A per-run
    # field cannot leak by any exit path and needs no reset call.
    symbol_sectors: "dict[str, str] | None" = None

    # === Structured facts for PM — Phase 4 #4 ===
    # Populated at the top of the DecisionStage so PM sees numbers, not
    # LLM-summarized-prose, for the quantitative stuff.
    facts: "PMFacts | None" = None

    @classmethod
    def start(cls, session: SessionType) -> "RunContext":
        """Build a fresh context for a new session.

        Run ID prefix matches legacy formatting so log greps like
        'run-abcd1234' and 'midday-abcd1234' keep working.
        """
        # Per-run state is opened HERE, at the single factory every session
        # goes through, never reset by a stage that an early exit can skip.
        parse_telemetry.reset()
        rid_prefix = "run" if session == "morning" else session
        run_id = f"{rid_prefix}-{uuid.uuid4().hex[:8]}"
        open_restore_run(run_id)  # run-scoped; see that module
        return cls(run_id=run_id, session=session)


# `PMFacts` lives in its own module (size ceiling); re-exported for imports.
from src.pipeline_pm_facts import PMFacts  # noqa: E402,F401
