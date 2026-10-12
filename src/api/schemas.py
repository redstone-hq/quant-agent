"""Pydantic response models for the Mission Control API.

Every route returns one of these models (never a raw `dict(row)` passthrough)
so the response shape is structurally frozen and, critically, structurally
incapable of carrying a secret: these models simply have no field that could
hold an API key, because none of the read paths that populate them ever touch
`ApiKeysConfig`. Stage 3 (a future consumer) gets a stable, typed contract for
free via FastAPI's generated OpenAPI schema.

Unknown/unavailable values are always `None`, never fabricated — see
CLAUDE.md/AGENTS.md "Health" and "Global criteria" sections.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

# Stage 4: reused directly as typed sub-fields of CandidateDetailResponse
# below. `src/api` importing `src.models` is safe under the Stage 2
# isolation invariant (tests/test_api_safety.py only forbids
# src.pipeline / src.pipeline_stages / src.risk / the write-capable
# Database class) — these are pure Pydantic value objects with no
# execution/trading behavior. Reusing them (instead of hand-duplicating
# near-identical shapes) means CandidateDetailResponse can re-hydrate
# `specialist_evidence.evidence_json` — itself always the
# `model_dump_json()` of one of these same validated objects, never raw
# LLM prose — with real type/field validation instead of an untyped dict.
from src.api.schemas_margin import MarginInterestCumulative, MarginInterestEstimate  # noqa: F401
from src.models import (
    EarningsAnalysis,
    MacroAnalysis,
    NewsIntelligenceReport,
    ReasoningChain,
    RiskModification,
    RiskVerdict,
    TargetPosition,
    TechAnalysisResult,
    TradeDecision,
)


# ---------------------------------------------------------------------------
# /health
# ---------------------------------------------------------------------------


class HealthResponse(BaseModel):
    status: str  # "ok" | "degraded" — process can respond while decisions are broken
    # WHY the status is what it is, assembled from the fields already in
    # this payload — never a new judgement and never a new severity scale.
    # None when nothing is wrong. A bare "degraded" makes the reader guess
    # which sub-field caused it, and a guess is not a status report.
    reason: str | None = None
    db_reachable: bool
    broker_reachable: bool | None = None  # None = not checked (e.g. no keys)
    paper: bool | None = None  # honest echo of config.alpaca.paper; never fabricated
    sessions_logged_today: list[str] = []  # run_id prefixes, e.g. ["run", "midday"]
    last_run_files: dict[str, str | None] = {}  # mode -> ISO mtime or None
    session_lock_active: bool | None = None  # best-effort process hint, not authoritative
    decision_path_status: str = "unknown"
    llm_circuit: dict | None = None
    # Can this desk still reach the operator at all? Written by every
    # session (src/alert_watchdog.py), never inferred here. `status` is one
    # of ok / broken / stale / unknown — "unknown" means no check has been
    # recorded, which is deliberately NOT the same as healthy.
    alert_channel: dict | None = None
    # Is the code running here the code that was merged? Written by
    # scripts/check_deploy_drift.py on its timer, never inferred here.
    # `status` is one of in_sync / behind / unknown. Telegram alerts can be
    # muted and a repeated message changed nothing five times in one day, so
    # the drift state must live on the board the desk already looks at.
    deploy_drift: dict | None = None
    # Paid-model credit left and how long it lasts; plain-English `message`.
    llm_balance: dict | None = None
    timestamp: str


# ---------------------------------------------------------------------------
# /account
# ---------------------------------------------------------------------------


class DailyPnlPoint(BaseModel):
    date: str
    total_value: float | None = None
    daily_pnl: float | None = None
    daily_return_pct: float | None = None
    equity_close: float | None = None


class LiquidityBreakdown(BaseModel):
    """Honest split of `AccountResponse.cash` so raw cash, cash parked in
    the sweep vehicle, and the configured reserve floor are never conflated
    into one ambiguous number (docs/STATE.md 2026-08-18 soak finding: SGOV
    must never present like an ordinary position or invented risk posture).
    Any field is None when the underlying account/positions read failed —
    never fabricated from a partial read.

    `deployable_cash` is THE ENGINE'S deployable figure — the same
    `src.quantities.deployable_cash` the Portfolio Manager sizes against,
    the Risk Manager audits and the pre-trade cash gate enforces. It used
    to be `max(raw_cash - reserve_usd, 0)`, a definition no part of the
    engine has ever used: same word, opposite adjustment, measured 1.58x
    apart on the same book. The reserve-adjusted figure is still reported —
    under its own name, `cash_above_reserve`, because it answers a
    different question.
    """

    sweep_enabled: bool = False
    sweep_symbol: str | None = None
    raw_cash: float | None = None  # broker cash, includes the reserve
    sweep_parked_value: float | None = None  # market value of the held sweep vehicle, 0 if none
    reserve_usd: float | None = None  # cash_reserve.pct% of portfolio_value
    # raw_cash + sweep_parked_value — what the engine can actually deploy
    # without borrowing, because fund_buys sells the sweep vehicle on demand.
    deployable_cash: float | None = None
    # max(raw_cash - reserve_usd, 0) — raw cash spendable WITHOUT selling the
    # sweep vehicle first. Strictly conservative, deliberately a different name.
    cash_above_reserve: float | None = None
    # Deprecated alias of `deployable_cash`, kept so an older cockpit build
    # keeps rendering. Assigned from the same single source — never computed
    # a second time. Remove once no client reads it.
    total_liquidity: float | None = None


class ExposureBreakdown(BaseModel):
    """How invested the book is, measured by the engine's own definition.

    Served here because the alternative — the frontend re-deriving it —
    is what produced the defect this replaces: HeroBand summed long +
    bearish-hedge market value RAW and UNLEVERAGED, then drew the result
    against the risk engine's ceiling fetched from this same endpoint. The
    bar and its fill came from different definitions and read 13.6
    percentage points apart on a book holding one -3x hedge.

    Computed by `src.quantities.net_exposure_usd/_pct`, the same functions
    `src.risk.rules`' `max_total_position_pct` rule calls — a dependency-
    free module outside `src.risk`, so this does not breach the structural
    guardrail in tests/test_api_safety.py. None when the positions or
    account read failed; never a fabricated 0."""

    # sum(market_value * signed ETF multiplier), sweep vehicle excluded.
    # A hedge SUBTRACTS; a 3x fund counts at 3x.
    net_exposure_usd: float | None = None
    # abs(net_exposure_usd) / portfolio_value * 100 — directly comparable to
    # RiskLimits.max_total_position_pct, which is the ceiling it is drawn against.
    net_exposure_pct: float | None = None


class RiskLimits(BaseModel):
    """The deterministic risk gate's own configured percentage limits
    (config/settings.yaml's `risk` section, src.risk.rules.RiskConfig) —
    read-only display context, never a risk decision computed here. Lets a
    UI exposure gauge scale against QAMC's actual hard-block thresholds
    instead of an arbitrary UI-only banding. None when the config read
    fails — never a guessed/default limit standing in for the real one."""

    max_position_pct: float | None = None
    max_total_position_pct: float | None = None
    # Spec §11.2. The STANDING gross-exposure cap as a multiple of equity
    # (long market value + absolute short market value, cash park excluded).
    # Distinct from max_total_position_pct, which bounds NET exposure — a
    # hedge cancels a long there and does not here. The ladder-stepped
    # ladder-stepped ceiling actually in force this session is NOT exposed
    # here: computing it needs `src.risk.rules`, which `src/api/` may never
    # import (tests/test_api_safety.py). It reaches the operator on the
    # session alert instead.
    max_gross_exposure_x: float | None = None


class AccountResponse(BaseModel):
    cash: float | None = None
    portfolio_value: float | None = None
    last_equity: float | None = None
    daily_pnl: float | None = None  # portfolio_value - last_equity (computed here)
    daily_pnl_pct: float | None = None
    #: Total P&L since the earliest row the `daily_pnl` table actually has —
    #: `current portfolio_value - (that row's total_value - that row's own
    #: daily_pnl)`, i.e. the broker equity going into the first tracked day.
    #: Same baseline definition and the same "never reconstructed from an
    #: archive" posture as `TradingPipeline._total_pnl_since_reset`, which
    #: the Telegram feed's own "Total P&L since <date>" line already uses —
    #: this is that same figure, reused for the dashboard rather than
    #: recomputed a second way. `None` when the table can't be read or has
    #: no rows, never a fabricated 0. `history` (above) is NOT the source —
    #: it is capped at 30 recent rows and may be truncated, so it cannot
    #: safely stand in for "since the start of the board".
    total_pnl: float | None = None
    total_pnl_pct: float | None = None
    #: The trading day the `total_pnl` baseline is measured from (that
    #: earliest row's own `date`) — same value trader_feed shows as
    #: "Total P&L since <date>", carried here so the dashboard can label
    #: the figure the same way rather than presenting a bare "total" that
    #: reads as "since the account began".
    total_pnl_since: str | None = None
    paper: bool | None = None
    source: str = "alpaca_live"
    history: list[DailyPnlPoint] = []  # recent daily_pnl table rows, newest first
    liquidity: LiquidityBreakdown | None = None
    exposure: ExposureBreakdown | None = None
    risk_limits: RiskLimits | None = None
    margin_interest: MarginInterestEstimate | None = None
    error: str | None = None  # set (fields above null) when the broker read failed


# ---------------------------------------------------------------------------
# /positions
# ---------------------------------------------------------------------------

PriceKind = Literal[
    "broker_position_mark",
    "current_quote",
    "delayed_quote",
    "historical_daily_close",
    "historical_intraday_close",
    "unknown",
]

FreshnessClassification = Literal[
    "current",
    "delayed",
    "historical",
    "stale",
    "unknown",
]


class PriceObservation(BaseModel):
    """One price plus enough provenance to say what it actually is.

    ``market_as_of`` is the provider's market timestamp when supplied;
    ``retrieved_at`` is when this API observed it. They are deliberately
    separate: retrieval time must never make an old market observation look
    current (docs/WORK.md item 15 — "we cannot tell a stale price from a
    live one").
    """

    value: float | None = None
    price_kind: PriceKind
    provider: str | None = None
    feed: str | None = None
    market_as_of: str | None = None
    retrieved_at: str
    freshness: FreshnessClassification


class PositionItem(BaseModel):
    symbol: str
    qty: float
    avg_entry: float
    current_price: float
    # Canonical provenance for `current_price`. The scalar is retained for
    # compatibility, but it is an Alpaca broker position mark, not a
    # market-data quote, and Alpaca's position response supplies no mark
    # timestamp — so `market_as_of` is always None here and `freshness` is
    # always "unknown" for this specific price_kind, by construction, not
    # by a read failure.
    position_mark: PriceObservation
    market_value: float
    unrealized_pnl: float
    unrealized_intraday_pnl: float | None = None
    sector: str | None = None
    # True only for the configured cash-sweep vehicle (e.g. SGOV) — parked
    # idle cash, never a Portfolio Manager thesis. See LiquidityBreakdown.
    is_cash_equivalent: bool = False
    # "long" (ordinary equity/ETF, qty > 0) | "short" (qty < 0, any symbol —
    # see docs/WORK.md item 176: this value did not exist before that fix,
    # every short position was mislabeled "long") | "bearish_hedge" (qty > 0
    # in an inverse ETF already in the trading universe — see
    # `src.quantities.inverse_etf_symbols()`, derived from the one leverage
    # table; a SHORT position in an inverse ETF is labeled "short", not
    # "bearish_hedge" — see `_position_direction` in
    # `src/api/broker_reads.py` for the precedence and why) |
    # "cash_equivalent" (the sweep vehicle).
    #
    # Display labeling ONLY; computes no exposure/risk math — and unlike
    # before, that is now true on both sides of the wire. The cockpit used
    # to derive its "% deployed" gauge from this label (long + hedge, raw
    # and unleveraged); exposure is served on `AccountResponse.exposure`
    # instead, from the engine's own definition.
    direction: str = "long"


class PositionsResponse(BaseModel):
    positions: list[PositionItem] = []
    source: str = "alpaca_live"
    error: str | None = None


# ---------------------------------------------------------------------------
# /orders
# ---------------------------------------------------------------------------


class OrderItem(BaseModel):
    id: str
    symbol: str
    side: str | None = None
    qty: float | None = None
    order_type: str | None = None
    status: str | None = None
    limit_price: float | None = None
    stop_price: float | None = None
    filled_qty: float | None = None
    filled_avg_price: float | None = None
    submitted_at: str | None = None
    filled_at: str | None = None


class OrdersResponse(BaseModel):
    orders: list[OrderItem] = []
    source: str = "alpaca_live"
    error: str | None = None


# ---------------------------------------------------------------------------
# /company/{symbol} — cached identity for the chart header
# ---------------------------------------------------------------------------


class CompanyIdentityResponse(BaseModel):
    """Cached company name for one ticker. `name` is None when the on-disk
    profile cache has no identity for this symbol — never invented, and
    this path never fetches. `error` is only set when the cache itself
    could not be read."""

    symbol: str
    name: str | None = None
    error: str | None = None


# ---------------------------------------------------------------------------
# /prices/{symbol}
# ---------------------------------------------------------------------------


class PriceBar(BaseModel):
    date: str
    timestamp: str | None = None
    open: float
    high: float
    low: float
    close: float
    volume: int
    # Provenance for `close` (docs/WORK.md item 15): market_as_of is this
    # bar's OWN date/timestamp as the provider reports it, freshness is
    # always "historical" — a bar is a look-back structure by construction,
    # never a claim about "right now". `None` only if this bar predates the
    # provenance rollout (should not happen for a freshly-served response).
    close_price: PriceObservation | None = None


class PriceBarsResponse(BaseModel):
    symbol: str
    timeframe: str = "1d"
    bars: list[PriceBar] = []
    source: str = "alpaca_market_data"
    error: str | None = None


# ---------------------------------------------------------------------------
# /events/{symbol} — dividend/earnings chart markers
# ---------------------------------------------------------------------------


class DividendEvent(BaseModel):
    """One ex-dividend date, past or upcoming. `amount` is None when a
    source reported the date without a per-share figure (never fabricated)."""

    date: str  # YYYY-MM-DD
    amount: float | None = None


class EarningsEvent(BaseModel):
    """One earnings-report date. `upcoming=True` marks a scheduled/estimated
    future report (not yet happened); `upcoming=False` marks one already
    reported — the chart renders these as visually distinct markers."""

    date: str  # YYYY-MM-DD
    upcoming: bool


class SymbolEventsResponse(BaseModel):
    """Dividend/earnings markers for one symbol's price chart. Sourced from
    yfinance (src.data.market.MarketDataProvider) — market-data read only,
    same dependency already used for ex-div stop adjustment and the
    next-earnings-date risk signal. Empty lists (not an error) when the
    source has nothing to report for this symbol.

    `earnings_degraded`, when set, means the past-earnings source itself
    (`ticker.earnings_dates`, needs the optional `lxml` package) could not
    be read — distinct from `earnings` being empty because the symbol
    genuinely has no history. `earnings` may still be non-empty via the
    next-date-only fallback even when this is set, so it can look like a
    normal result while being incomplete. Never collapse this into `error`:
    `error` means the whole request failed; this means one degraded
    sub-source inside an otherwise-successful one (2026-09-12 incident —
    see docs/INCIDENT_HISTORY.md)."""

    symbol: str
    dividends: list[DividendEvent] = []
    earnings: list[EarningsEvent] = []
    earnings_degraded: str | None = None
    error: str | None = None


# ---------------------------------------------------------------------------
# /quotes
# ---------------------------------------------------------------------------


class LiveQuote(BaseModel):
    """One symbol's current-session quote facts — distinct from a
    PositionItem's broker-marked current_price (held positions only) and
    from a PriceBar (historical, one row per completed/forming day).
    Any field is None when Alpaca had nothing to report for this symbol,
    never fabricated."""

    symbol: str
    last_price: float | None = None
    # The freshness-resolved current-session price (docs/WORK.md item 169
    # — `src.data.live_price.resolve_live_price` run over the same
    # snapshot `last_price` above comes from). `last_price` is the raw,
    # never-freshness-checked provider last trade and can be stale for a
    # thin name; `resolved_price` is `None` unless a real print (last
    # trade, minute bar, or today's forming session bar) exists from THIS
    # session. A chart or any other "current price" render should use
    # this field, not `last_price`, to avoid drawing a stale print as
    # today's.
    resolved_price: float | None = None
    # Provenance for `last_price` (docs/WORK.md item 15 — "we cannot tell a
    # stale price from a live one"). `market_as_of` is Alpaca's own
    # `latest_trade.timestamp` — a real per-trade exchange timestamp the
    # SDK actually carries. `freshness` is derived from today's regular-
    # session open (an exchange session boundary Alpaca's own calendar
    # supplies), never an invented elapsed-minutes cutoff: "stale" means
    # the last trade predates today's session open (or today isn't a
    # trading day, or the calendar lookup failed) — this desk's IEX feed
    # can otherwise go quiet for an illiquid name without anything else
    # telling you. `None` only when `last_price` itself is `None`.
    quote: PriceObservation | None = None
    prev_close: float | None = None
    # Whether the `session_*` block below belongs to TODAY (docs/WORK.md
    # item 120). Alpaca returns the PREVIOUS session's daily bar in that
    # slot for a name that has not printed today, so without this the
    # cockpit could draw yesterday's range as this session's. False means
    # the three fields below are `None` and the caller must not infer a
    # range for today; it must NOT be rendered as a range of zero.
    session_bar_is_today: bool = False
    session_open: float | None = None
    session_high: float | None = None
    session_low: float | None = None


class LiveQuotesResponse(BaseModel):
    quotes: list[LiveQuote] = []
    # When Mission Control read Alpaca for this response — a fetch-time
    # timestamp. Each quote's own `market_as_of` (above) is the per-trade
    # exchange timestamp; this `as_of` is deliberately kept separate so
    # retrieval time never gets to make an old market observation look
    # current (same split as `PriceObservation.retrieved_at`).
    as_of: str
    source: str = "alpaca_market_data"
    error: str | None = None


# ---------------------------------------------------------------------------
# /trades
# ---------------------------------------------------------------------------


class TradeItem(BaseModel):
    id: int
    symbol: str
    action: str
    qty: float | None = None
    price: float | None = None
    reasoning: str | None = None
    run_id: str | None = None
    decision_id: str | None = None
    broker_order_id: str | None = None
    fill_status: str | None = None
    fill_qty: float | None = None
    fill_price: float | None = None
    realized_pnl: float | None = None
    fill_reconciled_at: str | None = None
    timestamp: str | None = None
    stop_loss: float | None = None
    take_profit: float | None = None
    # The analyst's stated soft-exit falsifier at entry (models.py's
    # `TradeDecision.thesis_invalid_if`, persisted verbatim on the trades
    # row — see storage/db.py's `_ensure_column("trades", "thesis_invalid_if", ...)`).
    # Was already in the DB row `get_trades()` selects (`SELECT *`) but
    # dropped silently when this model was built with `TradeItem(**row)`,
    # since pydantic ignores unknown keys by default — added here so the
    # cockpit's chart can draw the CONDITIONAL thesis-break reference line.
    thesis_invalid_if: str | None = None
    # Phase 6 (§6.2a/e): links this trade to the position it belongs to, and
    # (for an exit-family row only) the deterministic category its exit was
    # classified into. Both None on rows written before this existed and
    # not yet covered by scripts/backfill_position_ids.py, or on a row this
    # system can't confidently attach to a chain — never fabricated.
    position_id: str | None = None
    exit_reason_category: str | None = None
    # Conviction ledger (spec §7.2, PR #159) — pinned at ENTRY only in
    # `Database.insert_trade`; every interim/exit row and every row written
    # before PR #159 carries None here, never a fabricated 0/default. Kept
    # OUT of every agent prompt deliberately (the outcome-grouped-by-
    # conviction stats these could feed are gated below 20 samples/bucket
    # and reach the human operator only) — this is read-only surfacing for
    # a human reviewing a trade, not a new input to any agent.
    conviction: str | None = None
    requested_risk_pct: float | None = None
    allocated_risk_pct: float | None = None
    decision_model: str | None = None


class TradesResponse(BaseModel):
    trades: list[TradeItem] = []
    count: int = 0


# ---------------------------------------------------------------------------
# /positions/{position_id}/history
# ---------------------------------------------------------------------------


class PositionHistoryResponse(BaseModel):
    position_id: str
    symbol: str
    status: str  # "open" | "closed" — closed once the chain's net qty is flat
    entry: TradeItem
    # Everything between entry and exit — TRAIL_STOP adjustments, partial
    # REDUCE/TAKE_PROFIT trims, anything else that inherited this chain —
    # each carrying its own `reasoning`, i.e. the review decision itself.
    interim: list[TradeItem] = []
    exit: TradeItem | None = None  # None while the position is still open
    # Sum of `realized_pnl` across this chain's exit rows, or None when no
    # exit has realized anything yet (still open, nothing sold).
    realized_pnl_total: float | None = None
    # True when at least one exit-family row in this chain realized a
    # number but at least one other couldn't be priced (unknown cost
    # basis) — realized_pnl_total is a PARTIAL sum in that case, not the
    # whole story, and callers should say so rather than presenting it as
    # complete.
    realized_pnl_partial: bool = False
    hold_days: float | None = None
    trade_count: int = 0


# ---------------------------------------------------------------------------
# /runs, /runs/{run_id}, /decisions/{decision_id}
# ---------------------------------------------------------------------------


class AgentLogItem(BaseModel):
    id: int
    agent_name: str
    run_id: str | None = None
    decision_id: str | None = None
    timestamp: str | None = None
    input_summary: str | None = None
    input_message: str | None = None
    output_summary: str | None = None
    full_response: str | None = None
    model: str | None = None
    requested_provider: str | None = None
    requested_model: str | None = None
    actual_provider: str | None = None
    prompt_version: str | None = None
    tokens_used: int | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_usd: float | None = None
    latency_s: float | None = None
    status: str | None = None
    finish_reason: str | None = None
    truncated: bool | None = None


class RunSummary(BaseModel):
    run_id: str
    session_prefix: str | None = None
    first_timestamp: str | None = None
    last_timestamp: str | None = None
    agent_count: int = 0
    decision_id: str | None = None
    total_cost_usd: float | None = None


class RunsResponse(BaseModel):
    runs: list[RunSummary] = []


class RunDetailResponse(BaseModel):
    run_id: str
    agent_logs: list[AgentLogItem] = []
    trades: list[TradeItem] = []
    decision_id: str | None = None
    total_cost_usd: float | None = None
    # Stage 2 Checkpoint C: True when this run's `agent_logs` includes the
    # `agent_name="risk_gate"` forensic row `TradingPipeline._persist_hard_risk_block`
    # writes when the deterministic hard-risk gate blocked EVERY candidate
    # before risk_manager was ever called (see docs/architecture/
    # MISSION_CONTROL_API.md). Computed from `agent_logs`, never fabricated —
    # a run with no such row (the ordinary case) reports False.
    hard_risk_block_recorded: bool = False
    # Board item 219. The pruning/rotation pass, in the SAME sentences the
    # Telegram session message uses (`src.rotation.owner_precheck_lines` +
    # `pruning_pass_lines`), read off the same durable `rotation`/`precheck`
    # row. Empty only when no such row exists for this run. There is no
    # second reporting path: both surfaces render this one list.
    rotation_lines: list[str] = []


class DecisionDetailResponse(BaseModel):
    decision_id: str
    portfolio_manager: AgentLogItem | None = None
    risk_manager: AgentLogItem | None = None
    # Stage 2 Checkpoint C: populated instead of `risk_manager` when the
    # deterministic hard-risk gate blocked every candidate for this decision
    # before risk_manager was ever called — the forensic `agent_name="risk_gate"`
    # `agent_logs` row (see `RunDetailResponse.hard_risk_block_recorded`).
    # `None` for every ordinary (RM-reached) decision.
    hard_risk_block: AgentLogItem | None = None
    trades: list[TradeItem] = []


# ---------------------------------------------------------------------------
# /candidates
# ---------------------------------------------------------------------------


class CandidateItem(BaseModel):
    symbol: str
    add_count: int = 0
    watch_count: int = 0
    total_flags: int = 0
    dates: list[str] = []  # ISO dates flagged, newest first
    themes: list[str] = []
    latest_reason: str = ""
    latest_miss_category: str = ""


class CandidatesResponse(BaseModel):
    candidates: list[CandidateItem] = []
    lookback_days: int = 30


# ---------------------------------------------------------------------------
# /agents, /agents/{agent_name}
# ---------------------------------------------------------------------------


class AgentRosterItem(BaseModel):
    agent_name: str
    role: str
    configured_model: str | None = None
    configured_provider: str | None = None  # resolved via resolve_provider(); None = prefix-inferred


class AgentsResponse(BaseModel):
    agents: list[AgentRosterItem] = []


class AgentDetailResponse(BaseModel):
    agent_name: str
    role: str
    configured_model: str | None = None
    configured_provider: str | None = None
    recent_calls: list[AgentLogItem] = []


# ---------------------------------------------------------------------------
# /reflections
# ---------------------------------------------------------------------------


class ReflectionItem(BaseModel):
    date: str
    tomorrow_outlook: str | None = None
    lessons: str | None = None
    suggested_actions: str | None = None
    risk_rating: str | None = None
    tomorrow_bias: str | None = None
    tomorrow_conviction: str | None = None
    tomorrow_key_risks: str | None = None
    sell_decisions_assessment: str | None = None
    sell_grades_json: str | None = None
    buy_grades_json: str | None = None
    missed_opportunities_json: str | None = None
    timestamp: str | None = None


class MetaPeriodSummary(BaseModel):
    period: str
    has_digest: bool = False
    has_reflection: bool = False
    has_proposed_edits: bool = False


class ReflectionsResponse(BaseModel):
    insights: list[ReflectionItem] = []
    meta_periods: list[MetaPeriodSummary] = []


# ---------------------------------------------------------------------------
# /runs/{run_id}/candidates, /runs/{run_id}/candidates/{symbol}  (Stage 4)
# ---------------------------------------------------------------------------


class RunCandidatesResponse(BaseModel):
    run_id: str
    candidates: list[str] = []


class PmReasoning(BaseModel):
    """Run-scoped PM reasoning_chain + portfolio_view — the "why", separate
    from the per-symbol `target`/`proposed_order` rows (the "what")."""

    portfolio_view: str | None = None
    reasoning_chain: ReasoningChain | None = None
    timestamp: str | None = None


class NewsBroaderContext(BaseModel):
    """Run/theme-scoped news context — deliberately NOT attributed to the
    candidate symbol (see ConsensusSummary for the symbol-specific slice
    of the same NewsIntelligenceReport, extracted separately below)."""

    market_sentiment: str | None = None
    confidence: str | None = None
    pm_briefing: str | None = None
    era_themes: list[str] = []
    current_regime: str | None = None
    # state_changes whose affected_symbols includes this candidate — a real
    # per-symbol citation the source itself made, not an inference.
    relevant_state_changes: list[dict] = []
    timestamp: str | None = None


class MacroBroaderContext(BaseModel):
    """Run-scoped macro regime context. sector_guidance is the FULL list
    exactly as macro_analyst emitted it — never filtered/attributed to this
    symbol's sector, since specialist_evidence carries no sector mapping
    and inventing one here would violate the Stage 4 boundary against
    manufacturing per-symbol macro conclusions from a run/sector-scoped
    source."""

    regime: str | None = None
    equity_outlook: str | None = None
    confidence: str | None = None
    summary: str | None = None
    sector_guidance: list[dict] = []
    timestamp: str | None = None


class RiskManagerVerdict(BaseModel):
    """Run-scoped RM verdict — approved/rejected + full reasoning, separate
    from any per-symbol `risk_modification` row."""

    verdict: RiskVerdict | None = None
    timestamp: str | None = None


class ConsensusSignal(BaseModel):
    source: str  # "tech_analyst" | "earnings_analyst" | "news_analyst"
    direction: str  # "bullish" | "bearish" | "neutral"
    detail: str = ""


class ConsensusSummary(BaseModel):
    signals: list[ConsensusSignal] = []
    # "aligned" (>=2 signals, exactly one non-neutral direction among them)
    # | "mixed" (non-neutral signals disagree) | "no_directional_signal"
    # (>=2 signals fired but every one was neutral — NOT the same as
    # "aligned"; nobody actually committed to a direction) |
    # "insufficient_data" (0-1 signals available). Never invents a signal
    # source that didn't actually fire this run, and never reports
    # "aligned" for a degenerate all-neutral case.
    agreement: str = "insufficient_data"


class CandidateDetailResponse(BaseModel):
    """Stage 4 per-candidate fidelity: everything Mission Control has about
    one symbol within one run, preserving each source's natural scope
    (symbol-specific vs broader/run-scoped, clearly separated below) —
    follows PM proposal -> AI Risk response -> deterministic gate ->
    executed/rejected result without the client re-parsing raw agent
    output. requested/actual model+provider+cost+latency for any agent
    involved remain available via GET /runs/{run_id} (agent_logs), not
    duplicated here.
    """

    run_id: str
    symbol: str
    decision_id: str | None = None

    # Symbol-specific specialist evidence — None when that specialist
    # never covered this symbol this run (never fabricated).
    tech: TechAnalysisResult | None = None
    earnings: EarningsAnalysis | None = None
    # Only the entries from this run's NewsIntelligenceReport.stock_news
    # that are actually keyed to this symbol.
    news_symbol: list[dict] = []

    # Broader, explicitly-labeled run-scoped context (never symbol-specific).
    macro_context: MacroBroaderContext | None = None
    news_context: NewsBroaderContext | None = None

    # Decision chain: PM intent -> constructed order -> AI Risk modification
    # -> deterministic outcome (trade, if any reached execution).
    pm_reasoning: PmReasoning | None = None
    pm_target: TargetPosition | None = None
    pm_proposed_order: TradeDecision | None = None
    risk_verdict: RiskManagerVerdict | None = None
    risk_modification: RiskModification | None = None
    trade: TradeItem | None = None
    trades: list[TradeItem] = []
    pipeline_events: list["PipelineEvent"] = []

    consensus: ConsensusSummary = ConsensusSummary()


# ---------------------------------------------------------------------------
# /runs/{run_id}/funnel — decision-funnel / "why no trade?" aggregation
# ---------------------------------------------------------------------------


class PipelineEvent(BaseModel):
    stage: str
    outcome: str
    reason: str = ""
    timestamp: str | None = None
    details: dict = {}


class CandidateFunnelItem(BaseModel):
    """One candidate's progress through the decision chain this run —
    the structural facts only (did it reach each stage, what stage it
    stopped at), never a synthesized narrative."""

    symbol: str
    # "bullish" | "bearish" | "neutral" | "unknown" — from tech_analyst's
    # rating when available (see _TECH_DIRECTION), else "unknown". Purely
    # a display label; computes no exposure/risk math.
    direction: str = "unknown"
    is_bearish_hedge: bool = False  # SH/SDS/PSQ/SQQQ — inverse ETF already in the universe
    reached_pm_target: bool = False
    pm_target_weight_pct: float | None = None
    #: Spec §2.1. Conviction is stated as RISK — the share of equity the idea
    #: may lose if its stop is hit — so a risk-sized target carries no notional
    #: weight at all and `pm_target_weight_pct` is None for it. Reporting only
    #: the weight silently dropped the size of every target the PM sized the
    #: new way. The two are not interchangeable and neither is derivable from
    #: the other without the stop, so both are carried.
    pm_risk_allocation_pct: float | None = None
    reached_proposed_order: bool = False
    proposed_action: str | None = None  # BUY | SELL | HOLD | None
    risk_modified: bool = False
    executed: bool = False
    trade_action: str | None = None
    order_status: str | None = None
    fill_qty: float | None = None
    fill_price: float | None = None
    realized_pnl: float | None = None
    protection_outcome: str | None = None
    pipeline_events: list[PipelineEvent] = []
    # Why the execution phase deterministically dropped this candidate's
    # approved order, when it did — quoted from the `execution_skip`
    # evidence row the trading process wrote at the skip site (e.g.
    # "insufficient_cash", "stale_entry"). None when no skip was recorded.
    # Closes the 2026-08-19 gap where `proposed_not_executed` was
    # indistinguishable from a deliberate no-trade.
    execution_skip_reason: str | None = None
    execution_skip_detail: str | None = None
    # Why the desk could not READ this candidate's analysis at all, when that
    # happened — quoted from the `analysis_drop` evidence row the risk stage
    # writes (board item 158). Same shape and same purpose as the execution
    # skip above, one stage earlier: without it a name the technical seat
    # dropped shows up in the funnel with every other field empty and no way
    # to tell a parse loss from a candidate nothing liked.
    # `analysis_drop_code` is the stable enum (`malformed_row`,
    # `schema_invalid`, `unspecified`); `analysis_drop_reason` is the human
    # detail. `analysis_drop_recovered` is True when a retry put the name back
    # into the book anyway — a cost note, not missing coverage. All None when
    # no drop was recorded, and a pre-158 row with no code reads as
    # `unspecified` rather than failing.
    analysis_drop_code: str | None = None
    analysis_drop_reason: str | None = None
    analysis_drop_recovered: bool | None = None


class RunFunnelResponse(BaseModel):
    """Stage 6 — structural decision funnel for one run, built to answer
    "why did it trade, or why not?" without requiring the operator to open
    every candidate individually. Every field is derived from existing
    specialist_evidence/trades rows already written by Stage 4; this
    introduces no new authoritative state. The PM/RM `reasoning`/`verdict`
    text below is quoted verbatim from what those agents actually wrote —
    never a Mission-Control-authored summary of "why," which could
    misrepresent the decision."""

    run_id: str
    session_prefix: str | None = None
    timestamp: str | None = None

    candidates: list[CandidateFunnelItem] = []
    candidates_considered: int = 0
    reached_pm_count: int = 0
    proposed_order_count: int = 0
    executed_count: int = 0
    bearish_hedge_considered: bool = False

    hard_risk_block: bool = False
    pipeline_events: list[PipelineEvent] = []
    pm_reasoning: PmReasoning | None = None
    risk_verdict: RiskManagerVerdict | None = None
    macro_context: MacroBroaderContext | None = None

    # "executed" | "proposed_not_executed" | "hard_risk_block" |
    # "no_proposal" | "no_candidates" — the single structural fact the
    # dashboard's headline state badge renders; never invents a cause
    # beyond what candidates/pm_reasoning/risk_verdict/hard_risk_block
    # above actually show.
    decision_state: str = "no_candidates"


# ---------------------------------------------------------------------------
# /journal/dates, /journal/{date}, /search  (Stage 5)
# ---------------------------------------------------------------------------


class JournalDatesResponse(BaseModel):
    dates: list[str] = []


class JournalDayResponse(BaseModel):
    date: str
    has_data: bool = False
    daily_pnl: DailyPnlPoint | None = None
    reflection: ReflectionItem | None = None
    runs: list[RunSummary] = []
    trades: list[TradeItem] = []
    # Union of candidate symbols considered that day (specialist_evidence +
    # trades) — same natural-scope union as get_run_candidates, just
    # day-wide instead of per-run.
    candidates: list[str] = []


class SearchTradeHit(BaseModel):
    kind: str = "trade"
    id: int
    symbol: str
    action: str
    run_id: str | None = None
    decision_id: str | None = None
    timestamp: str | None = None
    reasoning: str | None = None


class SearchAgentLogHit(BaseModel):
    kind: str = "agent_log"
    id: int
    agent_name: str
    run_id: str | None = None
    decision_id: str | None = None
    timestamp: str | None = None
    model: str | None = None
    output_summary: str | None = None


# ---------------------------------------------------------------------------
# /research/daily/{date} — day-scoped Research Intelligence projection
# ---------------------------------------------------------------------------


class ResearchFreshness(BaseModel):
    latest_recorded_at: str | None = None
    age_minutes: float | None = None
    label: str = "unknown"  # current | aging | stale | historical | unknown


class ResearchAgentCall(BaseModel):
    id: int
    agent_name: str
    run_id: str
    decision_id: str | None = None
    timestamp: str | None = None
    status: str | None = None
    output_summary: str | None = None
    requested_provider: str | None = None
    requested_model: str | None = None
    actual_provider: str | None = None
    model: str | None = None
    prompt_version: str | None = None
    latency_s: float | None = None
    cost_usd: float | None = None
    structured_evidence_count: int = 0


class ResearchEvidenceItem(BaseModel):
    id: int
    run_id: str
    decision_id: str | None = None
    agent_name: str
    kind: str
    scope: str
    symbol: str | None = None
    timestamp: str | None = None
    state: str = "valid"  # valid | invalid
    payload: dict | list | None = None


class ResearchDecisionDelta(BaseModel):
    run_id: str
    decision_id: str | None = None
    state: str = "no_proposal"
    proposed: list[ResearchEvidenceItem] = []
    risk_changes: list[ResearchEvidenceItem] = []
    deterministic_events: list[ResearchEvidenceItem] = []
    trades: list[TradeItem] = []


class ResearchRun(BaseModel):
    summary: RunSummary
    agent_calls: list[ResearchAgentCall] = []
    evidence: list[ResearchEvidenceItem] = []
    decision_delta: ResearchDecisionDelta


class ResearchDailyResponse(BaseModel):
    date: str
    as_of: str
    state: str = "empty"  # complete | partial | empty | error
    freshness: ResearchFreshness = ResearchFreshness()
    read_error: str | None = None
    missing_sources: list[str] = []
    daily_pnl: DailyPnlPoint | None = None
    reflection: ReflectionItem | None = None
    runs: list[ResearchRun] = []


class SearchResponse(BaseModel):
    query: str
    trades: list[SearchTradeHit] = []
    agent_logs: list[SearchAgentLogHit] = []


# ---------------------------------------------------------------------------
# /analysts/scorecard — the conviction ledger, per analyst (spec §9.5)
#
# Every number below is read back from persisted `conviction_credit` evidence
# rows; the API scores nothing. `credit` and `r_multiple` are in R — profit as
# a multiple of the risk the position was opened with — which the panel turns
# into dollars using `risk_dollars_per_call` (a labeled display convention,
# not a real position size).
# ---------------------------------------------------------------------------


class ScorecardPoint(BaseModel):
    """One resolved call in an analyst's running-profit series."""

    resolved_at: str
    #: Running total of this analyst's credit after this call, in R.
    cumulative: float
    #: Highest that running total has reached so far, including the 0.0 it
    #: starts from — so an analyst whose every call lost is below its own
    #: best from the start rather than from its first (negative) point.
    peak: float
    #: `peak - cumulative`, never negative: how far below its own best.
    below_best: float


class ScorecardMonthPoint(BaseModel):
    """One calendar month of an analyst's record (UTC month of `resolved_at`)."""

    month: str  # "YYYY-MM"
    #: Credit scored during this month alone, in R — the waterfall step.
    credit: float
    #: Running total at the end of this month, in R.
    cumulative: float
    resolved_calls: int
    calls_right: int
    #: Hit rate over every call resolved UP TO AND INCLUDING this month.
    #: None only when no call had resolved yet.
    hit_rate_pct: float | None = None


class ScorecardConfidenceBreakdown(BaseModel):
    """One analyst's record over only the calls it made at ONE declared
    confidence.

    This replaced the conviction weight (owner decision, 2026-08-31). Credit
    is raw and unweighted, so whether an analyst's confident calls are worth
    more than its hedged ones is now shown as separate rows a reader can
    compare, instead of being asserted by a multiplier in the scoring.
    """

    #: "high" / "medium" / "low", or whatever else the analyst declared.
    conviction: str
    resolved_calls: int
    calls_right: int
    hit_rate_pct: float | None = None
    #: Mean of the positive credits at this confidence, in R. None when none.
    avg_win: float | None = None
    #: Mean of the negative credits at this confidence, in R — NEGATIVE.
    avg_loss: float | None = None
    #: Total credit from this confidence alone, in R. Sums across the rows to
    #: the analyst's own `cumulative_credit`.
    cumulative_credit: float


class AnalystScorecardItem(BaseModel):
    analyst: str
    resolved_calls: int
    calls_right: int
    hit_rate_pct: float | None = None
    #: Mean of the positive credits, in R. None when there are none.
    avg_win: float | None = None
    #: Mean of the negative credits, in R — returned NEGATIVE, not as a
    #: magnitude, so the sign never has to be reconstructed by a consumer.
    avg_loss: float | None = None
    cumulative_credit: float
    peak: float
    #: How far the analyst currently sits below its own best, in R (>= 0).
    below_best: float
    #: When that best was last set, and how many calls have resolved since —
    #: the "for how long" half of the same fact. None when never off its peak.
    below_best_since: str | None = None
    calls_since_peak: int = 0
    cumulative: list[ScorecardPoint] = []
    monthly: list[ScorecardMonthPoint] = []
    #: The same calls split by the confidence the analyst declared on each,
    #: high first. Only levels it actually used appear — an empty row would
    #: read as a record of zero rather than as no record.
    by_confidence: list[ScorecardConfidenceBreakdown] = []


class ScorecardIdeaAnalyst(BaseModel):
    analyst: str
    side: str  # "supported" | "opposed" — the side taken, not the outcome
    stance: str
    #: What the analyst DECLARED. Reported, never applied — it multiplies
    #: nothing (owner decision, 2026-08-31); there is no `weight` field.
    conviction: str
    #: This analyst's raw signed score for this idea, in R: +R for a
    #: supporter of a trade that made money, -R for a supporter of one that
    #: lost, and the mirror of both for an opposer. Identical convention for
    #: a short and a long — a profitable short is a positive number.
    credit: float
    #: True when this analyst is the one that first tabled the symbol.
    nominated: bool = False
    #: The analyst's own stated reason, verbatim from its stance row. "" when
    #: no stance row survives for it — never a Mission-Control-authored guess.
    reason: str = ""


class ScorecardIdea(BaseModel):
    """One resolved idea, traced back to everyone who took a side on it."""

    symbol: str
    #: "long" | "short". Descriptive only: a short is scored, signed and
    #: worded exactly as a long is, and nothing downstream inverts on it.
    direction: str
    position_id: str | None = None
    decision_id: str | None = None
    resolved_at: str
    #: The trade's own realized R — identical for every analyst on this idea,
    #: and positive when the trade made money whichever way it was taken.
    r_multiple: float
    supported: list[ScorecardIdeaAnalyst] = []
    opposed: list[ScorecardIdeaAnalyst] = []


class AnalystScorecardResponse(BaseModel):
    as_of: str
    #: "populated" — at least one scored call was read.
    #: "empty" — the ledger is readable and holds nothing yet.
    #: "error" — the read failed; `read_error` says so and nothing is inferred.
    state: str = "empty"
    read_error: str | None = None
    #: The notional dollars treated as put at risk on EVERY call, used to
    #: express R in money. A presentation convention stated in one place so
    #: the API and the panel cannot disagree about it — not a real position
    #: size, and nothing compounds it.
    risk_dollars_per_call: float = 100.0
    resolved_calls_total: int = 0
    #: Every month that has at least one resolved call, oldest first.
    months: list[str] = []
    analysts: list[AnalystScorecardItem] = []
    ideas: list[ScorecardIdea] = []


# ---------------------------------------------------------------------------
# Generic error envelope (used by exception handlers, not returned inline)
# ---------------------------------------------------------------------------

# --- holding "why do we hold this" view (2026-09-18) -------------------
# Assembled by `src.api.holding_why.build_holding_why` from what is
# already stored; see that module for every wording rule. The models are
# deliberately shallow and string-first: the consumer renders these
# fields as prose, and nothing here should need a second lookup table on
# the frontend to become readable.


class HoldingInsiderPurchase(BaseModel):
    """The Form 4 facts a person actually wants: who, how much, when, at
    what price. `not_recorded` names any of those the desk did not
    capture, so a gap reads as a gap."""

    plain: str
    actor: str | None = None
    role: str | None = None
    total_usd: float | None = None
    total_usd_plain: str | None = None
    purchase_count: int | None = None
    first_transaction_date: str | None = None
    last_transaction_date: str | None = None
    average_price: float | None = None
    not_recorded: list[str] = []


class HoldingSupportingReason(BaseModel):
    seat: str
    reason: str


class HoldingPurchase(BaseModel):
    """When WE bought it and what WE paid — distinct from the insider's
    purchase above, which is somebody else's trade. Owner-requested
    2026-09-18: the two were confusable on screen because only the
    insider's date and price were ever shown."""

    plain: str
    #: ISO date of the entry trade, for a consumer that wants to format it
    #: itself. `plain` already carries it written out.
    date: str | None = None
    #: The fill price when the fill was reconciled, otherwise the price
    #: recorded on the order. `price_is_fill` says which, because quoting
    #: an order price as "what we paid" would be a small lie.
    price: float | None = None
    price_is_fill: bool = False
    quantity: float | None = None


class HoldingHorizon(BaseModel):
    sessions: int | None = None
    plain: str
    #: Why the number is a plan and not a rule — nothing sells on it.
    note: str


class HoldingTakeProfit(BaseModel):
    price: float | None = None
    plain: str
    #: ALWAYS False today. The automatic take-profit trim was deleted on
    #: 2026-09-12 and no caller passes `take_profit_price` to the broker,
    #: so no ORDER exists at this price. It is not the same as "the number
    #: has no effect": on a range trade `src/risk/trailing.py` reads
    #: whether price has exceeded the target to decide whether the +1R
    #: ratchet floor constrains the structural trail, which moves a live
    #: stop. `note` states that effect in the owner's words; this flag
    #: stays about ORDERS only. Kept as an explicit field rather than a
    #: comment so a future change has to flip it deliberately.
    acted_on: bool = False
    note: str
    #: The target PINNED AT ENTRY (`trades.initial_take_profit`). `price`
    #: above is the live number, which a confirmed structural event can
    #: re-derive (`src.risk.target_revision`); this is the one
    #: `thesis_progress_pct` and `pace` are measured against, and the only
    #: yardstick a revision can ever be graded on. Equal to `price` on a
    #: position that has never been revised, and None on legacy rows.
    entry_price_target: float | None = None
    #: True when the live target is no longer the entry derivation.
    revised: bool = False
    #: The derivation basis behind the LIVE number — "structural_level" or
    #: "measured_move" — so the display can label it honestly rather than
    #: implying a model picked it. Empty when no revision is on record.
    basis: str = ""
    #: The machine code of the most recent adjudicated revision flag for
    #: this symbol, whichever way it went: a TRIGGER_* code when the target
    #: was re-derived, otherwise the REFUSAL_*/FAULT_* code naming why not.
    #: A refusal is a first-class outcome and is never left blank.
    last_revision_code: str = ""
    #: One line of provenance for that outcome.
    last_revision_detail: str = ""


class HoldingWhyReadable(BaseModel):
    why: str | None = None
    primary_driver: str | None = None
    primary_driver_detail: str | None = None
    raised_by: str | None = None
    purchase: HoldingPurchase
    insider: HoldingInsiderPurchase | None = None
    supporting: list[HoldingSupportingReason] = []
    fundamental_reason: str | None = None
    invalidation: str
    stop_price: float | None = None
    horizon: HoldingHorizon
    take_profit: HoldingTakeProfit
    since_entry: list[str] = []


class HoldingWhyResponse(BaseModel):
    """One plain sentence, the decision-relevant detail, and the machine
    evidence behind a toggle."""

    symbol: str
    company_name: str | None = None
    lede: str
    readable: HoldingWhyReadable
    #: What the desk does NOT record for this holding, named in words.
    not_recorded: list[str] = []
    #: Accession numbers, internal flags, broker-eligibility JSON, run
    #: identifiers — everything deliberately kept out of `readable`.
    raw_evidence: dict = {}


class DeferredSuspension(BaseModel):
    """One owner page the cost circuit held back (item 211)."""

    trigger_code: str | None = None
    detail: str | None = None
    run_id: str | None = None
    created_at: str | None = None


class SuppressedRepeatEvent(BaseModel):
    key: str | None = None
    day: str | None = None


class SuppressedRepeat(BaseModel):
    """Repeat alerts of ONE type the watchdog declined to resend today."""

    day: str | None = None
    count: int = 0
    events: list[SuppressedRepeatEvent] = []


class SuppressedAlertsResponse(BaseModel):
    """Item 211 — the suppression record, readable without Telegram.

    `*_available` is False when the underlying record could not be read at
    all, which is a different fact from "nothing was suppressed" and is
    reported as such rather than as an empty list.
    """

    deferred_available: bool = False
    deferred_suspensions: list[DeferredSuspension] = []
    suppression_state_available: bool = False
    suppressed_repeats: dict[str, SuppressedRepeat] = {}


from src.api.schemas_muted import (  # noqa: E402,F401
    MutedBacklogResponse,
    MutedDayCount,
    MutedKindCount,
    MutedLiveRiskMessage,
)


# ---------------------------------------------------------------------------
# /analysts/target-scorecard — how often price reached the entry take-profit
# target. RECORD ONLY (owner ruling 2026-10-09): the target is not a sell rule.
# "Reached" is a LOWER BOUND (snapshot-based best-move figure).
# ---------------------------------------------------------------------------


class TargetScorecardExcluded(BaseModel):
    opened_before_clean_record: int = 0
    no_entry_target: int = 0
    no_best_move_figure: int = 0
    trade_rows_without_position: int = 0


class TargetScorecardResponse(BaseModel):
    read_error: str | None = None
    closed_reached: int = 0
    closed_not_reached: int = 0
    still_open: int = 0
    excluded: TargetScorecardExcluded = TargetScorecardExcluded()
    positions: list[dict] = []
    best_move_is_lower_bound: bool = True
