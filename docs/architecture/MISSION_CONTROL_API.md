# Mission Control API — Accepted Stage 2 Contract

Status: **ACCEPTED / Checkpoint C — 2026-08-09**.

`src/api/` is a separate-process, read-only adapter over existing quant-agent state. It is not a trading engine, memory system or trading dependency.

- Trading code does not depend on `src.api`; API failure has zero trading impact.
- Historical reads use independent SQLite `mode=ro` connections.
- Broker-live reads remain separate from SQLite history reads.
- Routes are GET-only with app-level rejection of non-read methods.
- No broker order placement/cancel/modify/close/stop-management path is exposed.
- Typed responses must not expose credential-bearing configuration.
- Mission Control cannot bypass deterministic risk/execution.

Accepted endpoints: `/health`, `/account`, `/positions`, `/orders`, `/trades`, `/runs`, `/runs/{run_id}`, `/decisions/{decision_id}`, `/agents`, `/agents/{agent_name}`, `/reflections`, `/candidates`. (See the dated sections below for every additive endpoint since, including `/analysts/scorecard`.)

Stage 4 additive endpoints: `/runs/{run_id}/candidates` (distinct symbols
considered in a run — union of symbol-scoped `specialist_evidence` and
`trades`), `/runs/{run_id}/candidates/{symbol}` (per-candidate fidelity:
symbol-specific tech/earnings/news evidence, clearly labeled broader
macro/news context, PM target/proposed order, RM verdict/modification, the
resulting trade(s), typed pipeline lifecycle events, and a computed
disagreement/consensus summary). Distinct
from `/candidates` (an unrelated Stage 2 universe-expansion watchlist
concept over `insights`, unchanged).

Stage 5 additive endpoints: `/journal/dates` (ET trading days with journal
data, newest first), `/journal/{date}` (day-level read-only aggregation:
equity snapshot, evening reflection, that day's runs, trades, and
candidate symbols — 404 when the date has no data), `/search?q=...`
(parameterized `LIKE` search over `trades`/`agent_logs`; the search term
is always a bound parameter, never interpolated into SQL text — the
endpoint cannot accept or generate arbitrary SQL). All three are read-only
aggregations over existing/Stage-4 tables; none introduce new
authoritative state.

When the deterministic gate blocks every candidate, the pipeline writes one additive forensic `agent_logs` row with `agent_name="risk_gate"`. It is not an LLM agent and does not alter risk calculations, eligibility, execution or broker behavior. Run/decision reads use it to reconstruct full hard-risk blocks; old databases simply lack pre-change rows.

Limits:
- no API writes;
- no authoritative UI/journal/search storage;
- `/reflections` does not parse Meta Reflector free text;
- `/candidates` exposes the existing persisted watch/universe-expansion concept, not a new intraday candidate engine.

Accepted full suite at Checkpoint C: **1530 passed, 0 failed**.
Detailed acceptance evidence remains in Git history. The last pre-ultra-lean working-tree snapshot is commit `02e20e6ac1c5c7e65b7f512f76c568328c990e3c`.

Stage 4–5 (specialist evidence, journal, forensic search) are externally
accepted — full suite **1558 passed, 0 failed**. Per
`.claude/rules/frontend-verification.md`, every cockpit UI acceptance pass
must be browser/runtime verified and ships a representative screenshot
set; this tranche's Stage 4-5 evidence was captured accordingly and is
preserved in Git history.

## Stage 6 — liquidity breakdown, position direction, decision funnel

Additive, read-only aggregation over existing data — no new authoritative
state, no broker-write surface, no new external dependency.

- `/account`'s `liquidity` field (`LiquidityBreakdown`) separates raw
  broker cash from the cash-sweep vehicle's parked market value, the
  configured reserve floor, and deployable cash — so the sweep vehicle
  (e.g. SGOV) can never present like an ordinary position or an invented
  risk posture (2026-08-18 soak finding). Computed from `read_account()` +
  `read_positions()` + narrow `cash_sweep` config accessors
  (`src.api.deps.get_cash_sweep_*`); `None` whenever the underlying
  account/config read fails, never fabricated.
- `deployable_cash` is **the engine's figure**, not a dashboard-only one:
  `src.quantities.deployable_cash` (raw cash + the parked sweep vehicle,
  which `CashSweeper.fund_buys` liquidates before the BUY phase) — the
  same function `TradingPipeline._compute_deployable_cash` calls, so what
  the operator reads is what Portfolio Manager sizing, the Risk Manager
  audit and the pre-trade cash gate use. It previously held
  `max(raw_cash - reserve_usd, 0)`, a figure no part of the engine has
  ever used: same word, opposite adjustment, measured $54,000 vs $34,100
  on one book. That conservative figure survives as `cash_above_reserve`
  under its own name. `total_liquidity` is now a **deprecated alias** of
  `deployable_cash` (it was always the same quantity) — assigned from the
  one source, never recomputed.
- `/account` also carries `exposure` (`ExposureBreakdown`):
  `net_exposure_usd` and `net_exposure_pct` from
  `src.quantities.net_exposure_usd/_pct` — the very functions
  `src/risk/rules.py`'s `max_total_position_pct` rule calls. The cockpit
  used to re-derive "% deployed" itself as `(long + hedge) / equity`
  (hedges ADDED, leverage ignored) and draw it against the engine's
  ceiling served from this same endpoint: 40.68% vs 54.24% on one book.
  The read surface serves the measurement now; the UI only renders it.
- The structural guardrail is intact. `src/api/` still may not import
  `src.pipeline`/`src.pipeline_stages`/`src.risk`
  (`tests/test_api_safety.py`). The seam is `src/quantities.py` — a
  dependency-free, stdlib-only module of pure measurement functions that
  lives OUTSIDE `src.risk` and that `src.risk.rules` imports. That is the
  extraction the §11.2 note in `routes_live.py` named as the precondition
  for reporting exposure here at all. The de-levering ladder still is not
  reported: it is session state, not arithmetic.
- `/positions` items gain `is_cash_equivalent` (true only for the
  configured sweep symbol) and `direction`
  (`"long" | "bearish_hedge" | "cash_equivalent"`). `direction` labels an
  inverse ETF already in the trading universe
  (`src.api.deps.INVERSE_ETF_SYMBOLS`, now DERIVED from
  `src.quantities.ETF_LEVERAGE`'s negative entries rather than
  hand-copied) — display labeling only, computing no exposure/sizing/risk
  math on either side of the wire.
- `GET /runs/{run_id}/funnel` (`RunFunnelResponse`): the structural
  decision funnel for one run — how many candidates were considered,
  reached a PM target, reached a PM proposed order, and executed; whether
  a bearish-hedge candidate was considered; the deterministic
  hard-risk-block flag; and the run-scoped PM reasoning / AI Risk verdict
  / macro regime context (quoted verbatim from what those agents actually
  wrote, never a Mission-Control-authored guess about "why"). Answers
  "why did it trade, or why not?" from one call instead of the client
  opening every candidate individually. 404 only when the run itself
  doesn't exist (no `agent_logs`/`specialist_evidence`/`trades` row at
  all) — a run that legitimately considered zero candidates (e.g. a
  hard-risk-block before `risk_manager` ever ran) still returns 200 with
  `decision_state="hard_risk_block"` and an empty `candidates` list.

## Backend recovery — canonical lifecycle evidence (2026-08-25)

The existing `specialist_evidence` stream now also accepts validated
`kind="pipeline_event"` rows. These are additive lifecycle facts, not a new
memory or trading dependency. Symbol events carry `stage`, `outcome`, `reason`
and structured details for opportunity discovery, specialist success/failure,
PM proposal/omission/failure, Risk outcome, deterministic gate, funding, order
submission and protection. One run-scoped event (no symbol) exists as well:
`stage="gross_delever"`, `outcome="still_over_ceiling"`, written when the
gross-exposure de-lever finishes with the book still over its ceiling; its
details carry gross before/after, the ceiling and an `orders` list with each
order's final broker status. Existing evidence kinds remain the canonical agent
payloads; `trades` remains the canonical broker lifecycle row.

**Every gate that changes or discards a decision writes one (board item 164,
2026-09-19).** Each such row carries a `gate` code naming the rule, the
reason, and — where a size changed — the value before and after. The
Risk-stage rows are:

- `risk` / `approved` or `modified` — `modified` ONLY when a field actually
  changed (`changes: {field: [before, after]}`); `reason` is the seat's own
  stated reason for that symbol, or says in words that it gave none. (Until
  2026-09-19 this carried the constant `risk_manager_verdict` and read
  `modified` for any symbol the seat merely named in an edit.)
- `risk` / `rejected` on a book-level veto carries the symbol's own reason
  where the seat named it, with `book_level_reason` beside it.
- `risk` / `dropped` (`gate=rm_modification_schema_invalid` or
  `rm_enlargement_revert_failed`) and `risk` / `modification_not_applied`
  (`rm_modification_unknown_field`, `rm_modification_no_matching_decision`).
  An edit naming a symbol with no decision in the plan is filed run-scoped
  with `symbol_named`, so it never becomes a phantom candidate.
- `deterministic_gate` / `blocked`, `reason=queued_earnings_unconvicted_seat`
  with `before_allocation_pct` / `after_allocation_pct` — the symbol's
  `proposed_order` row is written earlier and keeps the pre-cap size.
- `deterministic_gate` / `modified`, `reason=side_flip_refused` — a flip
  collapsed to its closing leg, with held / requested / emitted weight.
- `portfolio_manager` / `target_dropped` — a target removed after the model
  answered (`pm_target_malformed`, `conflict_unadjudicated`).

An exit the AI Risk seat APPROVES on the exit-review path is recorded in the
exit path's own per-symbol record (`kind="exit_refusal"`,
`code="ai_risk_approved"`, `dropped=false`) — not as a `pipeline_event`,
because the jam detector (`src/refusal_signature.py`) would count a surviving
exit as the session having taken a new idea.

`GET /runs/{run_id}/funnel` and the per-candidate endpoint expose those events,
all matching trades, terminal `fill_status`/fill facts, protection outcome and
`realized_pnl`. Realized P&L is written only for confirmed exit fills with a
complete confirmed average-cost basis; an incomplete legacy basis remains
`null`. Thus submitted/filled/partially-filled/canceled/expired/rejected state,
position-management exits and measured outcomes are queryable without parsing
logs or model prose. API behavior remains read-only and non-authoritative.

## Stage 6 — price bars (chart panel)

`GET /prices/{symbol}?lookback_days=120&timeframe=1d`
(`PriceBarsResponse`): OHLCV bars for one symbol at `5m`, `15m`, `1h`,
or `1d`. Daily bars wrap `AlpacaBroker.get_bars`; intraday bars use the
timestamp-preserving, read-only
`AlpacaBroker.get_intraday_chart_bars` — Alpaca's market-data client
(`StockHistoricalDataClient.get_stock_bars`), a
distinct read-only client from the trading client `broker_reads.py`
already uses for account/positions/orders. Never places, cancels, or
references an order; degrades to `{"bars": [], "error": "..."}` on any
failure rather than raising. `5m` honors `lookback_days` like the other
timeframes (previously ignored it — FIXED 2026-09-11). `lookback_days` has
no real ceiling beyond the `le=36500` input-sanity guard: the prior
500-day client-side cap was never a real Alpaca limit and has been
removed, so pan-to-load-more can page back through Alpaca's full history.
`AlpacaBroker` caches fully-closed prior daily bars in memory (today's bar
is always fetched fresh, never cached). Powers the cockpit's price chart
panel and timestamp-aligned execution markers. During market hours daily
bars can run one session behind — see `/quotes` below for the true current
price this chart is deliberately never mistaken for.

`GET /events/{symbol}?lookback_days=400` (`SymbolEventsResponse`):
dividend ex-dates and earnings-report dates (past and upcoming) for the
chart's fixed-row markers, via `MarketDataProvider.get_price_chart_events`
(yfinance) — separate from the existing single-next-value
`get_upcoming_ex_dividend`/`get_next_earnings_date` helpers stop
adjustment and risk prompts use, whose contracts this does not touch.
Empty `dividends`/`earnings` lists are a normal result, not an error.

`GET /company/{symbol}` (`CompanyIdentityResponse`): the cached company
name for the cockpit chart header. Reads `data/company_profiles.json`
only — the same store the desk already keeps for PM/Telegram identity.
`name` is `null` when that cache has no identity for the ticker; this
path never fetches yfinance and never invents a title. `error` is only
set when the cache itself could not be read.

## Mission Control data-truth / run-history / decision-explainability tranche (2026-08-21)

Operator-authorized, bounded read-side correctness tranche. Additive
only — no new authoritative state, no broker-write surface, no change to
`src/execution/broker.py` / `src/pipeline*.py` / `src/risk/*`.

`GET /quotes?symbols=AAPL,MSFT` (`LiveQuotesResponse`): current-session
quote facts (last trade price, previous session close, today's
still-forming session open/high/low) for up to 25 symbols in one call.
Wraps `AlpacaBroker.get_intraday_snapshots` — the SAME read-only bulk
Alpaca snapshot call (`StockHistoricalDataClient.get_stock_snapshot`)
the already-accepted, already-enabled intraday opportunity scanner uses;
no new broker capability. `as_of` is a Mission-Control fetch-time
timestamp (not a per-trade exchange timestamp), so a consumer can label
a quote "as of HH:MM:SS" rather than presenting it as unqualified live.
Distinct from `/positions`' broker-marked `current_price` (held
positions only) and `/prices`' historical daily bars — exists so the
cockpit chart/candidate context can show a genuinely current price
without ever implying a historical bar is "now" (the 2026-08-21 pricing-
reconciliation finding this tranche addresses).

`GET /journal/dates` now additionally unions in every ET calendar date
that has at least one recorded run (`agent_logs`), not only dates with an
evening reflection (`insights`) or an equity snapshot (`daily_pnl`) — a
day with real runs/candidates but neither of those was previously
invisible in the date list even though `/journal/{date}` already
rendered it correctly once selected directly. See `src/api/db_reads.py`
`get_journal_dates`.

`CandidateFunnelItem`'s `execution_skip_reason`/`execution_skip_detail`
(already returned by `GET /runs/{run_id}/funnel` since Stage 6) are now
also exposed in the frontend TypeScript contract
(`frontend/src/api/client.ts`) and surfaced in the Cockpit, the Candidate
Detail modal's outcome summary, and the Journal's per-candidate ledger
line — closing a frontend-only contract gap where a specific persisted
skip reason existed but the UI fell back to a generic "proposed but not
executed."

`CandidateFunnelItem` also carries `analysis_drop_code` /
`analysis_drop_reason` / `analysis_drop_recovered` (board item 158): why the
desk could not READ this candidate's analysis, one stage earlier than the
execution skip above, quoted from the `analysis_drop` evidence row the risk
stage writes. The code is a stable enum (`malformed_row`, `schema_invalid`,
`unspecified`) safe to switch on; the reason is the human detail. All three
are null when no drop was recorded, and a row written before the code existed
reads as `unspecified` rather than failing. Without them a dropped name
appeared in the funnel with every other field empty and the UI fell back to
"candidate-specific reason was not recorded" when the reason WAS recorded.

Frontend-only, no backend change: the professional Today Sessions strip
can pin the primary view (Candidates / Decision Room / chart) to an
explicit earlier run from the current ET day. Automatic mode truthfully
follows `bestPrimaryRunId` (shown as `AUTO / PRIMARY`, not mislabeled as
literal latest), and the operator can return to it explicitly. Poll
responses are generation-checked, so a new run or stale in-flight response
cannot silently replace a pinned inspection or mix context across runs.

## Research Intelligence Desk (2026-08-25)

`GET /research/daily/{date}` (`ResearchDailyResponse`) is an additive,
day-scoped read projection over the canonical `agent_logs`,
`specialist_evidence`, `trades`, `insights` and `daily_pnl` rows. It creates no
story table and never parses raw agent prose. Every agent call is represented
separately (including retries, failures and fallbacks); the response excludes
`input_message` and `full_response`. Structured specialist evidence is emitted
with its stored scope, symbol, provenance payload and timestamp, including the
Smart Money seat without granting that seat any path around PM, AI Risk or the
deterministic gate.

The response explicitly distinguishes `complete`, `partial`, `empty` and
`error`, and labels timestamp-derived freshness as current, aging, stale,
historical or unknown. A database read failure is a sanitized `error`, never a
misleading quiet day. The endpoint deliberately retains HTTP 200 for that typed
`state="error"` envelope so the reading workspace can render a composed degraded
panel using the same response contract; transport/server faults outside this
known read condition remain ordinary HTTP 500s. Persisted `provider_error` and
`analysis_error` evidence makes the day `partial` and names the affected
seat/provider in `missing_sources`. Per-run decision deltas contain the stored PM targets and
orders, Risk verdicts/modifications, deterministic lifecycle events and
canonical trade/fill rows; Mission Control authors no thesis, confidence or
causal explanation. The route retains all Mission Control isolation guarantees:
independent SQLite `mode=ro`, GET-only, no trading imports and no broker-write
surface.

## `AgentLogItem` surfaces `input_message` / `full_response` (2026-08-27)

Frontend-only contract fix, `GET /runs/{run_id}` unchanged. `agent_logs.input_message`
has always been populated — `pipeline_stages.py` has passed `pm_result.user_message`
(and nine other call sites their own prompt) since those columns existed — and
`get_run_detail`'s `SELECT *` has always returned it; `/runs/{run_id}` was serving
the field end to end before this change. The gap was purely in
`frontend/src/api/client.ts`: the `AgentLogItem` TypeScript interface omitted
`input_message` and `full_response`, so no view in Mission Control could reach
data the backend was already sending. Both fields are now declared on the
interface and rendered by a new `AgentPromptViewer` component in the Run Detail
modal — collapsed by default and height-capped in place, since a stored PM
prompt runs 13KB-190KB and must not reflow the modal on open. This is
unrelated to, and does not change, the `ResearchDailyResponse` route above,
which still deliberately excludes both fields from its own payload.

## Analyst scorecard — the conviction ledger, read (2026-08-31)

`GET /analysts/scorecard?idea_limit=25` (`AnalystScorecardResponse`) is an
additive, read-only projection of the §9.5 conviction ledger. It creates no new
state, adds no broker surface and introduces no new dependency.

The ledger writes into the existing `specialist_evidence` table — `kind`
`seat_stance` for the side each analyst took on an idea, `kind`
`conviction_credit` for the scored outcome once the position closed. Both are
read here through `db_reads.get_conviction_ledger()`, an independent SQLite
`mode=ro` connection like every other read in this package. A database written
before the ledger existed has no rows of these kinds and reads back empty —
which is `state="empty"`, not an error.

**The route scores nothing.** The realized `r_multiple` and the alignment
decision (`side`) are computed once by the ledger layer at close time and
persisted; this route parses the stored JSON and sums it into per-analyst
totals, a running series, a peak, a distance below that peak, monthly buckets
and a per-declared-confidence split. No R is computed here and no stance is
re-classified. Malformed rows are skipped rather than defaulted to zero — a
broken row is absence of evidence, not a break-even call.

**Credit is raw signed R, unweighted (owner decision, 2026-08-31).** An
earlier design multiplied each analyst's credit by its own declared conviction
(high 1.0 / medium 0.6 / low 0.3). That was removed because it is circular: the
ledger exists to find out whether declared confidence predicts anything, and
weighting by it assumes the answer. `ScorecardIdeaAnalyst.weight` is gone from
the response.

A second reason was originally given — that it double-counts, because "a
confident call already earns a larger position through the §9.4 agreement
ceiling". **That was factually wrong and was corrected on 2026-09-02**: §9.4
collapses every seat to a polarity through `stance_is_aligned` and nets them at
unit weight, so a declared conviction has never entered the sizing path at all.
The circularity reason stands alone, and is now joined by a sample reason —
deriving a weight needs `_CONVICTION_OUTCOME_MIN_N` (20) resolved calls per
seat and the book has 7 closed equity round-trips. See
`src/conviction_ledger.py` for the full correction.

What replaced it is `AnalystScorecardItem.by_confidence`
(`ScorecardConfidenceBreakdown`): the same settled calls split by the confidence
the analyst declared on each, high first, only for levels it actually used. The
rows sum back to the analyst's own `resolved_calls`, `calls_right` and
`cumulative_credit`.

**`credit` is derived on read, and this is the migration story.** Credit rows
written before 2026-08-31 stored a weighted `credit`. Nothing rewrites them:
`db_reads.get_conviction_ledger()` recomputes `credit` as `+r_multiple` for a
supporter and `-r_multiple` for an opposer, which is exact because `r_multiple`
was always persisted unweighted. Old and new rows therefore mean the same thing
and no series ever mixes two scales. `Database.get_conviction_credits` derives
it identically on the trading side.

**A short reads exactly like a long (owner decision, 2026-08-31).** `direction`
on an idea is descriptive. A trade that made money is a positive number and a
win whichever way it was taken; the analyst that argued for it is credited and
the one that argued against it is charged. No field is inverted, negated or
specially worded for direction anywhere in this response or the panel that
renders it.

**Deliberate duplication, and why.** `src.conviction_ledger.aggregate_seat_records`
produces the same per-analyst totals, and importing it would be the obvious
move — but that module imports `src.risk.rules`, and `tests/test_api_safety.py`
forbids any `src.risk` import from `src/api/`. Preserving the isolation contract
wins, so the arithmetic is mirrored in `src/api/routes_scorecard.py`.
`tests/test_api_scorecard.py::test_projection_matches_the_ledgers_own_aggregate_when_it_is_available`
runs both implementations over identical input and asserts they agree on every
shared field — including the per-confidence split — so the two cannot silently
diverge.

Three states, all HTTP 200 — the same typed-degraded-envelope posture
`/research/daily/{date}` uses, so the consumer can render an honest "this could
not be read" rather than a blank page: `populated`, `empty`, and `error` (with a
sanitized `read_error`). `idea_limit` is bounded 1..200 and rejected with 422
outside it. Transport/server faults outside this known read condition remain
ordinary 500s via the app's global handler.

`risk_dollars_per_call` (100.0) is a labeled presentation convention, not a real
position size: the ledger's numbers are in R, and this is the single figure the
cockpit multiplies by so the API and the panel cannot disagree about how R is
expressed in money.

Advisory only, per spec §9.5 item 6: no score this endpoint serves feeds sizing,
risk allocation or order construction. Per §9.5 item 8 there is no
minimum-sample gate — raw counts are returned for every analyst, however few
calls it has settled.

Frontend: the cockpit gains an "Analyst Scorecard" top-level view
(`frontend/src/components/scorecard/`), alongside Cockpit / Research Desk /
Journal. When the endpoint returns no scored calls it renders a committed
fixture (`frontend/src/fixtures/analystScorecard.ts`) behind a permanent
"Example data — not real" banner and switches to the live record as soon as
real rows arrive. The fixture lives only in the frontend; the API never
fabricates a row.
