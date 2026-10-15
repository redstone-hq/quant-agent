# QAMC Current State

Updated: 2026-09-17

This file records what is accepted and true **now**. Git history preserves implementation detail; do not turn this file into a changelog.

## Accepted product / architecture

- **QAMC / Mission Control** is the whole product/system. **Dashboard** is the browser/iPad read-side UI.
- QAMC is an autonomous AI-assisted Alpaca trading system whose **currently authorized execution environment is Alpaca Paper**. Live-broker order submission is not authorized.
- Paper vs live is an execution-environment boundary, not a separate trading architecture.
- **Owner operating hold, 2026-10-06:** the Paper desk is deliberately stopped and must not be restarted without explicit owner authorization. The owner rejected a second capture/replay system as a prerequisite to using the Paper desk as the beta test. Existing rehearsal code is inactive; its presence is not proof of a live session, nor a reason to delay the structural and defect audit. Verify current services and timers on the box rather than treating this dated statement as a runtime check.
- **Owner 2026-09-17:** missing required data anywhere end-to-end is a defect in the step that should have produced it. Never invent the missing value. Never make skip/drop/ignore-and-continue the permanent product. Isolate-empty-name (item 78) is temporary quarantine, not the product.
- Decision chain remains: **Specialists → Portfolio Manager → AI Risk Manager → deterministic Python risk/execution → broker**.
- **FIXED 2026-08-27 (PR #108) — the exit path now follows this chain.**
  `run_position_review` (`src/pipeline.py`, midday and close) previously called
  `position_reviewer` and executed directly: no Portfolio Manager, no AI Risk
  Manager, so sells were taken by a single model call with no second opinion
  while buys passed three layers. `_risk_review_exits` now puts every
  SELL/REDUCE in front of the AI Risk Manager and drops what it rejects.
  **The failure posture is deliberately asymmetric with entries and this is
  not an oversight:** `RiskStage` fails CLOSED with zero orders when the Risk
  Manager is unparseable, but the exit path fails OPEN. Failing closed on an
  entry means not buying, which costs nothing; failing closed on an exit means
  a thesis-invalidated position cannot be closed because a language model is
  unavailable, with the loss then bounded only by the broker stop.
  **Reconciled 2026-09-16 (item 60).** Deterministic Python owns refusal on
  this pair. A completed "no named trigger" is that owner's drop, not
  uncertainty, and those exits are not sent to the Risk Manager — so a dead
  model cannot fail-OPEN a sale the phrase gate already refused. Uncertainty
  (Risk Manager unavailable/unparseable/verdict-less, or the hard-trigger
  recogniser itself unable to run) fails OPEN on both layers: that is the
  2026-08-27 ratification, now applied to the pair rather than to one layer
  only. AI Risk remains a challenge seat: a parseable reject still drops; an
  approval cannot override a deterministic drop. Every drop, and every
  uncertainty fail-open, writes an append-only per-symbol reason. The fact
  gates (metric-contradiction veto, ATR noise band, proven-false holding-
  discipline) still run before the broker; their 1.0 multiples are item 70,
  not this. Every fail-open path logs at ERROR.
- Deterministic Python and broker protections remain final safety authority; uncertainty fails closed.
- **Long scale-in path B (owner 2026-09-15):** a BUY add on a name that already
  has a resting protective sell is allowed only by cancelling that sell,
  confirming the cancel at the broker (not assuming it), submitting the add,
  then placing ONE protective sell covering the broker's full position at the
  existing protection level (the tighter of the cancelled stop and the add's
  own stop). Crash recovery is a write-ahead row that rearms at current
  broker quantity; trail, in-session coverage repair and the coverage watchdog
  skip a name mid-sequence. If rearm fails, the owner is paged — the position
  is not left naked quietly. Short adds stay blocked: scale-in is the long
  path. Missing short stops are repaired separately (item 73, closed).
  No take-profit / bracket; post-fill GTC protective stops stay
  the protect model. `execution.repeg_enabled` is unchanged (false).
- **Stop-level archive (2026-09-16, item 71 closed):** `trades.stop_loss` on
  the opening BUY/SHORT row is written back whenever an in-code path changes
  the live stop (replace/trail, coverage repair, scale-in rearm, ex-div shift,
  residual reprotect), and only when the broker accepted an order id — a
  kill-switch dict is not archived. The entry bet is frozen as
  `initial_stop_loss` so R-multiple is not rewritten by a trail. Repair
  restores that live recorded level, not the frozen entry stop; a stop that
  would fire immediately is still refused (restoring the entry stop would be
  a widen). A session and watchdog reconcile writes the desk's live protective
  order back onto the opening row when that order exists (COP/EQNR:
  archive lagged our own stop — not an invented level). Remaining
  mismatches still page; do not mute an alert without fixing the
  record. Do not treat a "traded through its stop" reading from the
  archive as real while they disagree. Items 35 and 69 stay archive illusions,
  not re-filed as fixed. The halt still asks the broker whether a stop
  *exists*.
- Mission Control/API/journal/search/UI remain private, read-only and non-critical to trading.
- OneCLI remains the accepted credential-delivery layer. No public listener is authorized.
- Private operator access uses Tailscale. Canonical VPS FQDN: `ovh-vps.wallaby-bowfin.ts.net`.
- Smart Money v1 uses first-party SEC Form 4. The operator has authorized a
  fail-closed, run-scoped external-symbol admission lane for fresh material
  open-market purchases; it changes no permanent universe membership and
  preserves the complete accepted decision/risk/execution chain.
- **`QAMC_REMEDIATION_SPEC.md` Phase 1 (Tech Analyst structural levels) is
  implemented and merged to `main`** (PR #102). `TechAnalysisResult`
  (`src/models.py`) now requires `support_levels`, `resistance_levels`, `setup_type`
  (`"range"` / `"breakout"`), `expected_horizon_sessions` and `reference_target` for
  every actionable rating, on top of the existing `entry_price` / `stop_loss`; a
  candidate missing any of them fails validation. A new deterministic module,
  `src/data/levels.py`, computes support/resistance from the full OHLCV history
  (swing-pivot detection, zone clustering, distance/recency-weighted ranking) and
  the Tech Analyst prompt now includes a formatted levels block computed over
  `trading.lookback_days: 1800` (~5 years, up from 320) rather than the 20-40 bar
  window it previously reasoned over. `PortfolioConstructor` no longer synthesizes
  a stop (`entry − 2×ATR`, then `entry × 0.95`) or a target
  (`entry × (1 + 2×stop_gap_pct)`) when the analyst omits one — those fallbacks and
  their config fields (`default_stop_atr_multiple`, `fallback_stop_pct`) are deleted.
  **Behavioral consequence: the desk now declines a trade outright — no BUY or SELL
  is constructed — whenever the Tech Analyst does not supply a structural stop and
  target for a symbol**, rather than trading it against an invented number.
  **Superseded in part on 2026-09-01 (spec Phase 10.4, branch
  `fix/target-from-structure`): the order's take-profit is no longer read from
  the analyst's `reference_target` at all.** It is derived in Python from the
  computed levels — the nearest structural level in the trade's direction, or
  a measured move of `ATR × sqrt(expected_horizon_sessions)` when nothing
  stands in the way — because dividing a volatility-derived stop by a guessed
  target put 30 of 38 signals (79%) under the 1.5 reward:risk floor on the
  2026-09-01 morning run and placed no trades. The floor was not changed. The
  analyst's `reference_target` is still required by the validator and is kept
  as evidence, logged against the computed number; it no longer enters the
  arithmetic. Derivation refuses by name (missing levels / ATR / horizon)
  rather than falling back to a default, so the "declines a trade outright"
  posture above now extends to a symbol whose chart cannot support a target.
  **Pending on branch `wt/levels-wire`, not yet merged (2026-09-02): the
  distance/recency-weighted ranking above loses the recency half of that —
  `strength` is now touch count discounted by distance only.** The
  252-session half-life was never measured; `src/data/level_quality.py`'s
  decay fit against the desk's own daily bars found no age effect at all
  (likelihood ratio 0.00, `docs/RESEARCH_FINDINGS.md` §7), so a level
  defended three years ago now counts the same as one defended last week.
  Checked on the full 101-symbol universe before this was decided: dropping
  recency changes the top-6 levels shown to the Tech Analyst on at least one
  side for 66 of 99 symbols (27.1% of the 1,020 side-slots compared) — not a
  no-op. `level_quality.py` itself stays unwired; nothing under `src/`
  imports it, and `TestNotWiredIntoTrading` still passes.

  On top of this, a new deterministic module `src/data/context.py` computes market context
  per symbol from the same bars — relative strength vs a same-batch benchmark (SPY,
  else QQQ, else IWM), returns across 1w/1m/3m/6m/12m, 52-week range position, ATR
  as a percentage of price with its 1-year percentile and a volatility state
  (Wilder's ATR from `src/data/technical.py::atr_series`, the same one the risk
  path sizes and widens stops with — until 2026-09-01 this block used a simple
  moving average of true range instead, see `docs/QAMC_REMEDIATION_SPEC.md`),
  moving-average slopes, consolidation detection (requires both a narrow range and
  small net drift), average dollar volume, up/down volume ratio, and unfilled gaps
  — rendered into the Tech Analyst prompt via `format_context_block()`
  (`src/agents/tech_analyst.py`). `TechAnalystAgent.build_user_message` also accepts
  an optional `days_to_earnings` kwarg, and `MarketDataProvider.get_next_earnings_date()`
  (`src/data/market.py`, new) can supply an approximate trading-session count to a
  symbol's next scheduled earnings. **That method now has real callers** (2026-08-31):
  `src/data/event_calendar.py::fetch_earnings_proximity` sweeps it for every symbol
  the Risk Manager is judging and renders the result — a session count, or one of
  four NAMED absences — into RM's Event Risk block, so the mandatory
  `reasoning_chain.event_risk` field is grounded in fetched data rather than the
  model's recollection. **Still unwired for the Tech Analyst specifically**: nothing
  passes `days_to_earnings` into `TechAnalystAgent.build_user_message` today.
- **Phase 2a of the remediation spec is merged and deployed** — `c89e957`
  on branch `feat/risk-metrics-and-pm-correlation`. It folds in four
  `AGENT_ROLE_AUDIT.md` audit findings that preceded Phase 2's own sizing work:
  the drawdown-halve was made deterministic (since RETIRED IN FULL on
  2026-09-20 at the owner's instruction, with the rest of the account-level
  loss alarms — docs/INCIDENT_HISTORY.md, board item 32), the
  correlation matrix is built before the Portfolio Manager decides and shown to
  it as measured clusters (removed 15 Oct 2026, see OUTCOME.md),
  portfolio heat / budget risk / open risk exist (`src/risk/metrics.py`) and
  render to PM and RM, and R-multiple reaches the Position Reviewer. A new
  `risk.max_portfolio_risk_pct` config field (25%) was reporting-only at this
  point — no gate consumed it yet. Phase 2's own items (§2.1 risk-based
  sizing, §2.2 correlation-aware budget ceiling, §2.4 retiring the fixed
  position count) landed afterward as Phase 2b — see below.
- **Phase 3.1 and 3.2 of the remediation spec are merged and deployed**
  — `aea82ee` on branch `feat/exit-rework-pace-and-memory`. §3.1: the
  `pace` metric no longer feeds back on the desk's own realized-trade
  calibration — `expected_horizon_sessions` and `setup_type` are pinned to the
  `trades` row at BUY time and never recomputed, the calibration query is
  deleted from the review path, and `pace_status` (`measured` / `too_early` /
  `n/a_breakout` / `unavailable_no_pinned_horizon`) replaces a fabricated
  number with a labeled absence where pace cannot be measured. §3.2 (closes
  `AGENT_ROLE_AUDIT.md` §1.5): the Position Reviewer now has memory of its own
  prior review — each review snapshots its per-position metrics
  (`db.save_position_review_metrics`), the next review receives the deltas,
  and new `src/risk/exit_guard.py` deterministically vetoes a SELL/REDUCE
  whose stated reason is a deterioration claim when every metric that moved
  since the prior review improved. Exits on new information (news, earnings,
  regime shift, a triggered `thesis_invalid_if`) are never
  vetoed. ("Correlation breach" was on that list until 2026-09-13, when it was
  removed from the accepted exit vocabulary outright — nothing in the desk
  could ever verify it. See `docs/INCIDENT_HISTORY.md`.) §3.3 is merged separately — see below. §3.4–§3.7 (routing exits
  through AI Risk, upgrading the reviewer's model off `gemini-2.5-flash-lite`,
  ATR noise band, broker-resident trailing stops) are also merged and
  deployed — see "Phase 3 ... COMPLETE" below (§3.5 was resolved as an owner
  decision rather than implemented). Phases 4–8 (evidence/feed repair, short
  selling, cost/transparency, measurement, further documentation correction)
  remain pending in whole or part — see `docs/QAMC_REMEDIATION_SPEC.md` and
  `docs/WORK.md`'s ordered backlog for current status.
- **Phase 3.3 of the remediation spec is merged and deployed** —
  `2f177e33` on branch `feat/exit-gate-and-risk-routing`. The hard-trigger
  phrase gate on exits previously applied only to a symbol already trimmed
  that day, so a position's first sale of the day executed on soft reasoning
  unchecked — almost every sale. Every SELL and REDUCE now requires the
  reason to name a recognised trigger (`src/pipeline.py`,
  `_reason_cites_hard_trigger` against `_HARD_TRIGGER_KEYWORDS`, widened to
  cover macro regime shift, sector shock, adverse/material news, earnings
  miss and guidance cut); a non-matching reason is dropped and logged as
  `exit_blocked_no_named_trigger`, and the position is held, protected by its
  broker-resident stop. **Since 2026-09-16 (item 60)** that drop is the
  deterministic owner's completed content judgment, not an uncertainty fail:
  unnamed-trigger exits are not sent to the Risk Manager, and the reason is
  also written append-only per symbol so a later cooldown-ledger upsert
  cannot erase it. A recogniser that cannot run (non-string reason, or the
  matcher itself raises) fails OPEN, matching a dead Risk Manager.
  **Extended 2026-09-11 — the named trigger is now FACT-CHECKED at midday and
  close, not just named.** `_reason_cites_hard_trigger` is a substring match
  and nothing more; the deterministic verifier built for exactly this problem
  (`src/risk/exit_guard.holding_discipline_claim_check`, live on the morning
  Portfolio-Manager path in `RiskStage` since 2026-09-03/04) was imported from
  that one call site and nowhere else, so on the intraday surfaces a claimed
  regime flip or bearish state change executed on the strength of the words
  alone. `_midday_execute_llm_actions` now runs the same check, on the same
  inputs, for every SELL/REDUCE/COVER — `run_position_review` backs both
  midday and close, so one gate covers both. **The PROVABLY-FALSE /
  UNVERIFIABLE split is unchanged and load-bearing:** only a claim real
  same-day data CONTRADICTS drops the exit
  (`exit_blocked_holding_discipline_claim_false`); a claim that merely cannot
  be checked is recorded (`holding_discipline_claim_unverified`) and ALLOWED
  through, because blocking an exit on absence of proof would strand the desk
  in a losing position. No macro analyst runs intraday, so the regime input is
  this morning's stored read via the already date-scoped
  `_carry_forward_macro` (status `carried_from_morning`); when nothing is
  stored for today the status is passed as None and the claim is unverifiable,
  never defaulted into a value that could call it false.
  **Extended 2026-09-18 — the trigger is a FIELD, not a phrase in a
  sentence.** Fact-checking a claim read out of prose only works when the
  prose happens to phrase the claim in a way a regex recognises. On
  2026-09-16 the two real exits (COP SELL, EQNR REDUCE) carried the reason
  `"adverse news"` — two words, entire. They cleared the substring gate
  because the phrase is on the list, and `holding_discipline_claim_check`
  returned "ok" because those two words make neither of the claims it knew
  how to check, so nothing was checked. Meanwhile an exit that honestly
  described a stall is what the metric veto audits and one naming no listed
  phrase is dropped outright: the gate was **selecting for bad paperwork**.
  `PositionAction` now carries `exit_trigger` (an `ExitTrigger` enum) and
  `trigger_evidence` (the recorded thing the trigger rests on), and
  `ExitTrigger.CANNOT_SUBSTANTIATE` is a first-class value so the seat is
  never pushed into naming a trigger it cannot support. Both fields are
  OPTIONAL: an action carrying only prose is mechanically healed from the
  SAME phrase vocabulary as before, so nothing that used to execute stops
  executing. Anything still unsubstantiated is RE-ASKED once — bounded by
  the same one-paid-retry-per-seat-per-session cap the research seats use —
  and what survives the re-ask gets an append-only per-symbol
  `exit_refusal` row (`unsubstantiated_after_reask`) plus a heal-FAILED
  owner alert. **It is not dropped:** stranding the desk in a losing
  position is worse than an uncheckable claim passing. What changed is that
  the claim is now in a field, so it can be routed to the record it is
  about — an `adverse_news` trigger is checked against the same-day state-
  change rows, and the ratified three-valued outcome applies to it
  (contradicted = blocked + alerted, unfindable = logged only). See
  `src/risk/exit_trigger.py`.
- **A wider stop is no longer an "improvement" (2026-09-18).**
  `distance_to_stop_pct = (current - stop) / current * 100` sits in the exit
  guard's `_HIGHER_IS_BETTER`, and it is a function of the desk's own
  protection as much as of the market — so LOWERING the stop makes a
  position read as improved. Real recorded snapshots, 2026-08-31 close to
  2026-09-01 midday: V distance-to-stop 1.42 -> 3.41 "improved" while
  `r_multiple` fell -0.11 -> -0.82; CMCSA 3.53 -> 5.58 while r fell -0.04 ->
  -0.33; DIS 1.85 -> 5.07 while thesis progress fell -3.06 -> -12.50. All
  three were deteriorating. The review snapshot now carries `stop_loss` and
  `current_price` (provenance, never scored), and `MetricDeltas` re-runs the
  metric's own formula with the stop pinned at its prior value: a rise that
  survives only because the stop moved is listed in `stop_driven` and
  excluded from `improved`. The metric is NOT deleted — a price moving away
  from a FIXED stop is a real improvement and is the case it exists for (the
  2026-08-26 EPD premature exit turns on it). `worsened` is deliberately
  left undecomposed: discounting a stop-driven FALL could turn
  `net_improved` True and make the veto block an exit on paperwork, which is
  the wrong failure direction here. Snapshot pairs written before
  2026-09-18 carry no provenance; their distance-to-stop rise is
  unattributable and is not counted, which self-heals after one session.
- **Phase 3 of the remediation spec is COMPLETE and DEPLOYED** at `058273f1`
  (rollback `9f77b03e`), live on the paper account since ~09:20 ET
  2026-08-27. §3.1 the `pace` feedback loop is cut — the horizon is pinned to
  the trade row at entry from the analyst's thesis and `avg_hold_days` is gone
  from the review path, so the system's own selling behaviour no longer sets
  the bar every surviving position is measured against. §3.2 the reviewer
  snapshots its metrics and a deterioration verdict that contradicts its own
  improving numbers is vetoed (`src/risk/exit_guard.py`). §3.3/§3.4 as above.
  §3.6 an adverse move inside 1.0x ATR of entry cannot trigger a price-derived
  exit; external-information triggers bypass it. §3.7 trailing stops are
  computed deterministically in `src/risk/trailing.py` before the LLM is asked
  — range setups do not trail until the target is exceeded, breakouts trail
  under confirmed higher lows with a 3x ATR chandelier fallback, ratchet
  upward only. §3.5 was resolved as an owner decision rather than
  implemented: the "weakest model" premise is contradicted by committed
  benchmark data (see `docs/WORK.md`). §3.8 unchanged.
- **Phase 2b (risk-based sizing) merged and deployed** — `75c0233` on branch
  `feat/pm-flex-routing`, merged as PR #113. The
  branch replaces `target_weight_pct` with `risk_allocation_pct` as the live
  sizing field and turns `max_portfolio_risk_pct` from a reported figure into
  an enforced gate. **Owner-ratified risk envelope, per `config/settings.yaml`
  `risk:`:** 5% of equity per trade ceiling (`max_position_risk_pct`), 0.5%
  floor below which a request is denied rather than shrunk
  (`min_position_risk_pct`), 25% of equity total at-risk ceiling across the
  book (`max_portfolio_risk_pct`, previously reporting-only), (the per-cluster
  share of it was removed by the owner on 15 Oct 2026) —
  `src/risk/budget.py::allocate_risk_budget`, best-ranked candidate first
  (owner decision 2026-09-12, item 49; largest-request-first was the rule
  before that) with an alphabetical tie-break for unranked names. The gate
  engages only when the caller supplies the
  book's current risk and clusters (`pipeline_stages._book_risk_inputs`); an
  unmeasurable book falls back to per-position sizing and the 5% single-name
  cap only. `target_weight_pct` stays Optional so stored decisions still
  replay through `src/replay.py` and the Mission Control API. **Caution for
  the next session:** `/home/ubuntu/projects/quant-agent-worktrees/phase2b`
  (branch `feat/risk-based-sizing`) still holds an independent, uncommitted,
  in-progress attempt at this same phase (dirty `src/models.py`, untracked
  `src/data/company.py`) that predates and duplicates `75c0233`. It was not
  touched by this documentation pass; reconcile or discard it before doing
  further Phase 2b work so two implementations don't collide.
  **Two follow-on fixes landed the same day, same branch, also merged and
  deployed:** `b712f4c` clamps the constructor to the 20% single-name ceiling
  the risk engine actually enforces (`max_position_pct` is a HARD BLOCK, not
  a trim — without this, the sizing above computed orders the engine would
  silently drop). `3dff940` widens an entry stop to a minimum ATR distance
  (`risk.min_stop_atr_multiple`, base 2.5 since 2026-09-10 — was 1.5
  (2026-09-04, from this desk's own Maximum Adverse Excursion data, later
  found to overlap the window some seats misreported confidence/data
  quality in) before that 3.0 (never derived). 2.5 is sourced from published
  swing-trading doctrine instead, independent of this desk's own trade
  history; scaled by setup type and macro
  regime) when no real level backs the stop. As first shipped it then
  rejected the trade if the widened stop dropped reward:risk below
  `risk.min_reward_risk_after_widening` (1.5); **that rejection no longer
  exists** (owner decision 2026-09-11, docs/WORK.md item 1(d); leftover
  invented size-cap and execution belt removed 2026-09-17). Today the
  constructor runs no reward:risk comparison at all on a `breakout`, and on
  a `range` trade the ratio — both sides measured since 2026-09-01, the
  target computed from structure rather than read from the analyst (see the
  Phase 1 entry above and spec Phase 10.4) — is a ranking signal only. A
  computed ratio does not refuse a ticket and does not shrink it. An
  unmeasurable ratio is recorded as unknown ranking information; it does
  not refuse, size-cap, or open a catalyst-exception door. Measured, the book's stops
  were sitting a median 1.7 ATRs from entry, which both fired exits inside
  noise and forced the 20% clamp above to bind at nearly every conviction
  level. `config/prompts/portfolio_manager.md`'s conviction bands and
  `config/prompts/tech_analyst.md`'s stop guidance were recalibrated to match.
  2487 tests pass. **This section does not track a live deploy pointer** —
  see "Production position" below for how to check what production is
  actually running.
- **The Portfolio Manager now routes through OpenRouter's `openai/flex`
  endpoint** (`16f6535`, same branch) — the identical `openai/gpt-5.5-20260423`
  weights at half price ($2.50/$15 vs $5/$30 per M tokens), an endpoint choice
  rather than a model choice. `llm.portfolio_manager_provider_order:
  ["openai/flex"]`; fallbacks stay enabled since the fallback endpoint serves
  the same weights. OpenRouter calls now request `usage: {include: true}` and
  the daily cost circuit spends against the provider-reported figure, not the
  pinned per-model estimate, because one model id now prices two ways.
- **`intra_check` is no longer blindfolded** (`fb88e08`, same branch). The
  intraday PM previously received a technical-only evidence registry even
  though the morning's macro and news were already on disk; both are now
  carried forward, date-scoped and re-validated, and labelled
  `carried_from_morning` in `data_status` rather than `not_run_intraday`.
  Same-session reuse is usable evidence, not a data-integrity failure: Risk
  does not veto a plan solely because those seats were not paid for again
  this tick (PR #430). **Kind+event reuse (2026-09-16):** pay again only
  when that kind expired or was never good. News expires on a newer
  material wire or when the session ends — no second fitted clock. Chart
  always re-reads live price at submit; a daily bar close is not a fill
  reference. **Intraday Tech covers held names, not just ≥threshold
  movers:** a quiet hold the PM can still increase gets current-run
  Technical on the same paid batch so grounding can see it. Missing Tech
  is a producing-step defect, not a drop-the-name product. Macro regime is
  reusable across days until a real
  regime/print change; a failed parse is not a regime. Print change is a
  change in statistical FRED prints (CPI, unemployment, claims) or their
  observation dates, not a daily quote reprint (VIX, treasuries) and not
  only the regime label string. An undated snapshot is not same-session —
  that claim needs a trustworthy date; a GOOD undated regime can still be
  remembered across days until a real change. News expires when a live
  RSS peek shows a name-relevant headline the remembered report/raw titles
  do not already cover. Form 4 expires when EDGAR lists an accession for a
  watched name the cache has not already processed. Failed peeks/fetches
  and a last_state that never recorded prints are not a change. The print
  peek restores the morning `last_coverage` side-channel.
  Earnings write-ups
  and Form 4 filings are remembered until the next report/8-K or a new
  filing. Blank/LOST is never research. Missing seats self-heal
  (mechanical first, then at most one paid retry inside cost caps); heal
  failure and a spend-cap block page the owner. Empty or failed morning
  carry-forward still refuses before the Portfolio Manager. Desk-caused
  stall after Risk is a defect, not "a little late." **Fills are confirmed
  by the bounded REST path; the live `trade_updates` websocket is OFF
  (owner, 2026-09-17, `execution.fill_stream_enabled`) — it never
  authenticated on this host, and two confirmed blockers mean no code
  change could make it. No socket, no lease, no reconnect loop, no
  handshake before submit; REST timeouts and poll intervals unchanged. The
  code is dormant, not deleted. Fill confirmation actually degrading now
  pages the owner; the socket being off never does.** The rest of this
  paragraph is what flipping the flag back on restores: handshake starts
  during Risk so auth is not serial after approval; Alpaca allows one
  `trade_updates` socket per account: one desk process owns it via a
  cross-process lease; other processes attach to that process's hub or
  REST-poll with a wait no longer than the REST path — they never open a
  second socket. Lengthening auth backoff is not the ownership fix.
  Submit uses the
  already-coded fund/auth budgets; overrun is `latency_window` even if
  the tape is still inside the pinned ceiling. One in-ceiling catch-up
  is a safety net only. `execution.repeg_enabled` stays false.
- **`AgentLogItem` now surfaces `input_message` / `full_response`**
  (`6b7af86`, same branch), a frontend-only fix — the backend has populated
  and served both fields all along; only the TypeScript interface omitted
  them. See `docs/architecture/MISSION_CONTROL_API.md`.
- **Sector-stance vocabulary is unified** (`cdb387b`, same branch).
  `SECTOR_STANCE_TO_DIRECTION` / `SECTOR_DIRECTIONS` / `normalize_sector_stance`
  now live once in `src/models.py`; `macro_store` imports them instead of
  keeping its own copy. This also fixed a live crash where carrying stored
  macro forward into the intraday PM (`fb88e08`, above) raised `TypeError:
  string indices must be integers` because `build_user_message` indexed the
  persisted `{sector: direction}` dict shape as if it were the live agent's
  list-of-mappings shape.

## Stabilization account model

The prior three-account daily workflow created excessive friction. During stabilization the accepted operating model is now:

- **`ubuntu` — engineering/operator account.** Codex development, Git/GitHub, development tooling, private DEV preview/browser work, testing, Docker/sudo tasks, and deployment orchestration happen here.
- **`qamc` — production runtime account only.** It owns the production checkout, runtime `.env`/OneCLI wiring, user services/timers, and QAMC execution.
- **`dev` — parked.** It remains present but is removed from the normal workflow. Do not expand its permissions or require the operator to use it during stabilization.

The retained isolation boundary is `ubuntu` engineering/operator vs `qamc` runtime. Codex must not run as `qamc`.

## Production position — how to check it, and history

**This file does not record a live production pointer — check it directly:**

`sudo -n -u qamc git -C /home/qamc/quant-agent log --oneline -1`

Compare that SHA against `git log origin/main` and `docs/WORK.md`'s ordered
backlog to see what production has and what is still pending. Every commit
SHA and rollback SHA recorded anywhere in this file (including below) is a
historical point-in-time record, not a live pointer — it was true when it
was written and goes stale at the next deploy. This section alone recorded
five different "current" production SHAs in two days; that was the bug, not
something to keep repeating.

**Historical: the 2026-08-27 evening deploy.** As of that evening,
production was deployed at `46b2029` (merge of PR #113,
`feat/pm-flex-routing`), superseding `32c174b` (PR #114, the deploy-drift
alarm), which PR #113 carries inside its own merge history. It descended
from `e6ada88` (PR #112) and `058273f1` (Phase 3, PR #109/#110), and
included Phase 2b risk-based sizing (`75c0233`), the stop-width fix
(`3dff940`) and the OpenRouter flex-routing change (`16f6535`). As of that
same evening, PR #115 (earnings extraction fix) and PR #116 (shorts Stage 1,
countable) were open and undeployed. **None of this paragraph describes
current state** — use the command above, and see `docs/WORK.md`'s ordered
backlog for what is currently pending.

The `a25a723f70a4e0f1548b3389c93c96d9b5ced6d7` / PR #93 SHA recorded below is
similarly historical — it was production on 2026-08-26 and has since been
superseded multiple times over. It is kept only because the forensic
narrative under it (the recovery/retry-limit defect, the Smart Money/earnings
data-shaping fixes) is not duplicated elsewhere. Its recorded rollback SHA,
`7fe6e4babbf3cf0209d8f93536f8150de70fea37`, is likewise a historical artifact
of that one incident — never a usable rollback target — and must not be used
against current production without first checking what production actually
is and what gap it would reopen.

Production verification on 2026-08-26 established (historical, PR #93 state):

- the prior ET-day quota hold rearmed automatically after the date advanced;
  no spend was erased and no manual reset was used;
- the first natural morning run exposed a mismatch between Tech's per-batch
  recovery and the session-wide retry limit. It admitted RSG, AMR and PAM but
  stopped safely before PM, Risk or broker submission;
- PR #92 now analyzes every primary Tech batch before one bounded consolidated
  recovery, retains successful results, prioritizes run-scoped admissions,
  narrows the deterministic prefilter and compacts Smart Money input. The
  audited operator rerun completed all 54 selected Technical analyses, compared
  with 93 loosely selected symbols before the fix;
- that rerun found seven directional PM candidates but exposed two independent
  data-shaping defects before Risk: a valid fenced Smart Money result was
  reduced to its inner list, and absent queued earnings analysis became the
  false stance `none`;
- PR #93 fixes both contracts at their source. The exact stored Smart Money
  response replays as the full eight-finding object, missing earnings is omitted
  from PM's authoritative registry, and unsupported evidence is still rejected;
- the complete hermetic suite passed **2,181 tests**;
  - *Correction (2026-08-27):* this figure was accurate locally but had never been
    reproduced in CI. GitHub Actions had never executed a single run on this fork,
    so all 94 merges to that date were ungated. The first CI run failed: `fastapi`
    was declared only in the optional `[api]` extra while the workflow installed
    `.[dev]`, so all 8 API test modules failed at collection and pytest exited
    before running anything. Every run would have failed identically. Fixed in #96;
    CI now reproduces 2,181 passing tests;
- the operator-rerun switch may bypass only a same-day morning marker and
  requires a reason. It does not bypass the ET window, weekday check, session
  lock, paid-session/cost circuit, Paper configuration, PM, AI Risk,
  deterministic risk or broker protections. Its use is audit-logged;
- `/health` reports DB and broker reachable, `paper=true`, no active session
  lock and no global circuit suspension. Its degraded label reflects the
  historical session-scoped retry hold from the earlier failed run, not a
  block on later independent sessions;
- exact settled ET-day paid-analysis cost is **$0.5279159 / $1.50**. Both
  permitted paid morning sessions were used, so no third run was attempted;
- Alpaca still reports EPD 12 shares and SGOV 89 shares. EPD remains fully
  covered by a broker-resident 12-share stop-limit order at stop $38.00 and
  limit $36.86. No order was forced, submitted, cancelled or modified;
- Mission Control remains private/read-only and the intended production-only
  `intraday_scan.enabled: true` setting was preserved.

The Smart Money seat is enabled on first-party SEC Form 4. Its deterministic
pre-market refresh is credentialless, bounded, cached and available while the
paid circuit is suspended. External symbols can be admitted for one run only
after fresh material open-market purchase evidence plus broker common-stock,
supported-exchange, $5 price, 20-session history, $10M average dollar-volume
and known-sector checks. The run cap is three. Admission bypasses only permanent
universe membership and the Technical prefilter; current Technical analysis,
PM grounding, AI Risk, deterministic risk/funding, broker protection and
Alpaca Paper remain mandatory. The configured 101-stock universe is unchanged.

**Insider routine/opportunistic filter — PR #133 opened against `main`, not yet
merged or deployed** — `f3aeba4` + `866e423` plus a 2026-08-28 finishing
pass on branch `feat/insider-signal-filter`. `src/data/insider_signal.py`
classifies every parsed Form 4 P/S row as `routine`, `opportunistic` or
`indeterminate` (Cohen/Malloy/Pomorski calendar test, a recurring-cadence
fallback, and proportional-size rules on sells; a 10b5-1 flag alone never
marks a large sale routine). A routine purchase can no longer make a symbol
`admission_eligible`, narrowing the lane described above; no other admission
check changed. Every threshold (sell-materiality fraction, calendar years,
cadence window, history retention) is a `SmartMoneyConfig` field in
`src/config/smart_money.py` (prefixed `insider_`), not a hardcoded constant. Re-measured
on the live cache 2026-08-28: 57.3% of open-market P/S rows routine (2,742
rows) — consistent with the original 56.2%-of-2,188 figure a day earlier —
but zero of those matched the calendar test in either measurement, since the
required multi-year history index still does not exist in production.
End-to-end on `SECForm4Provider.fetch()` with the real cache: admission-
eligible symbols are unchanged today (43 before and after — the one buy-side
row the classifier demotes was already below the materiality floor), but
ranking is materially affected — 94 symbols in the fetch() output have their
entire visible dollar volume down-weighted to $0, including one ($IHT) whose
raw $2.04B total was entirely two routine sales. See `docs/WORK.md`
"Landed" for the full measurement, the config fields, and the caveat.
Recorded here as branch/PR work, not as deployed behavior; production
admission logic is unchanged until this merges and deploys.

The production checkout retains exactly one intended tracked local configuration delta:

- `config/settings.yaml`: `intraday_scan.enabled: true`

GitHub `main` may advance beyond the current production SHA. Production changes only through the governed engineering workflow below.

**Deploy-drift alarm (2026-08-27, PR #114).** This "may advance beyond" gap is
now monitored instead of silently trusted: `scripts/check_deploy_drift.py`
compares the deployed HEAD on the qamc box against `origin/main` and alerts
over Telegram when they diverge, run by `quant-agent-drift-check.timer`
Mon-Fri 08:45 ET. It exists because PR #111 sat merged-but-undeployed for
eight hours with nothing catching it (`docs/WORK.md` records that incident).
Merged and deployed as part of `32c174b`; verified firing.

**Second correction (2026-09-30): an alert that repeats and changes nothing
is not a control.** The production checkout ran six commits behind
`origin/main` and the drift alert fired five times that day without anything
happening; desk alerts are also muted, so the message reached nobody in any
case. Two changes: `scripts/check_deploy_drift.py` now writes a durable
snapshot to `data/alerting/deploy_drift.json` on every run (in sync or not,
so "checked and clean" is distinguishable from "never checked"), and `/health`
reads it and reports `deploy_drift`, turning the board degraded while the box
is behind. A snapshot older than 26 hours is reported `stale`, which is also
degraded — the checker having stopped is itself a fault. The Telegram push is
now deduplicated per day AND per `origin/main` tip using the same
`load_state`/`save_state` helpers the three stop-coverage alerts already share,
so a new merge still speaks but the same drift does not repeat.

**Stale CI results (2026-09-30).** A push to an already-open PR branch was
measured to create no `pull_request` workflow run at all, leaving the previous
commit's failure standing and auto-merge unable to fire; roughly six finished
changes sat stuck that way for hours. Cause not established — `test.yml`
cancels superseded PR runs, and GitHub also drops run creation under load —
so `scripts/check_stale_ci.py` tests the observable state instead: every open
non-draft PR whose head commit has no completed `tests` run is reported, a
run is dispatched for it, and the job exits non-zero so the scheduled
`stale-ci` workflow goes red in the Actions tab. A cancelled run counts as
stale; it is not an answer.

**main's own post-merge run is now watched too (2026-10-01).** Branch
protection reads a pull request's `pytest` check and does NOT require the
branch to be current with main first (`strict: false`, verified by API), and
nothing read the run main produces after a merge. On 2026-10-01 main sat red
on thirteen cost-circuit tests while every open PR showed green: each PR's
own run had legitimately passed earlier, against a different wall clock. The
tests were not the defect; nothing watching main was. `scripts/check_main_red.py`
now asks whether main's newest COMPLETED push run of `tests` succeeded, and
the `main-red` job in the scheduled `stale-ci` workflow runs it every half
hour. A non-success answer prints the failing commit, its subject, the run
URL and how long the branch has been red, writes the same record to the
workflow run summary, and exits non-zero so the job goes red in the Actions
tab. It never re-dispatches: a red main is an answer, not a flake. A
cancelled newest run counts as red, a `pull_request` run is never a verdict
on main, and a GitHub read that fails exits 2 rather than claiming health.

**Those thirteen tests were a two-clock bug, not a regression.** The circuit
dated the ET day from Python and `suspended_at`/`created_at` from SQLite's
`'now'`. In production that is one OS clock and they cannot disagree; a test
could freeze only the Python half, so for the first quarter-hour of each ET
day the two landed on different days and the self-clear refused — correctly,
given what it was shown. `src.cost_circuit._now_utc()` is now the module's
single clock: replace it and `_connect` pins SQLite's `'now'` (and the
`created_at` column default) to the same instant. Production is unchanged —
with the real clock in place the bare connection is returned and every SQL
`'now'` still reads SQLite's own clock. `tests/desk_clock.py::freeze_desk_day`
is the one way to freeze the desk's day, after three clock-class bugs in one
night.

**Correction (2026-08-31): the drift alarm could not actually send.** The unit
installed on 2026-08-27 declared no `EnvironmentFile` and invoked the venv
Python directly on the script, unlike the session units which go through an
env-sourcing wrapper. Verified on the box: the qamc systemd user environment
contains zero `TELEGRAM_*` variables, `.env` does carry them, and
`TelegramNotifier` disables itself when they are absent — so a drift alert
would have been printed to the journal and delivered to nobody. Every run
since installation had reported "in sync", so the alerting path had never
been exercised; "verified firing" above meant the timer fired, not that the
alarm could speak. Fixed by routing the unit through
`scripts/run_drift_check.sh`, matching `run_daily_export.sh` /
`run_pricing_refresh.sh`. `tests/test_alert_heartbeat.py` now fails CI for
any unit in `scripts/systemd/` whose script can raise a Telegram alert but
whose `ExecStart` does not go through an `.env`-sourcing wrapper.

**The schedule was not under version control (2026-08-31).** The deploy-drift
alarm above compares *commits*, which left a second gap nothing was watching:
installing a systemd unit is a `cp` into `~/.config/systemd/user`, and git
never sees it. Measured on the box that day — 11 services and 10 timers
installed, 6 services and 4 timers tracked. The seven units that run the
trading day and serve Mission Control (`api`, `morning`, `midday`,
`intra_check`, `close`, `evening`, `earnings_preprocess`) existed nowhere but
on that one machine, and had since 2026-08-10. The tracked copies were not
trustworthy either: `quant-agent-daily.service` declared
`WorkingDirectory=/home/yebo/quant-agent` and ran the export from the same
path — the upstream fork's, present on no QAMC machine — so installing the
repository's own copy would have broken the daily P&L export on contact.

All 21 installed units are now captured verbatim in `scripts/systemd/`,
byte-for-byte as the box runs them, and the `/home/yebo` path is corrected to
`/home/qamc`. Capture was faithful, not improved: the repo describes the
running system even where the running system is imperfect.

Two checks keep it that way, and neither subsumes the other — CI cannot see
the box, and the box cannot see a pull request:

- **`scripts/check_unit_drift.py`, 08:50 ET seven days a week**
  (`quant-agent-unit-drift.timer`). Compares the deployed checkout's
  `scripts/systemd/` against the installed units byte for byte and alerts on
  four buckets: `untracked` (installed but not in the repo — the hand-added
  unit no commit comparison can ever see), `modified`, `undeployed`, and
  `not_enabled` (installed, correct, and never fires because nothing linked
  it into `.wants`). Compared against the *deployed checkout* rather than
  `origin/main` on purpose, so it stays disjoint from the deploy-drift alarm
  and the two never report one condition twice. Exit 1 is a finding; exit 3
  is an operator problem and marks the unit failed.   **Update, 2026-09-14 / 2026-09-15:**
  a `not_enabled` unit named in `scripts/systemd/paused_units.yaml` is
  reported in a separate "deliberately paused" section instead of alarming,
  and does not count toward the exit status; a paused-list unit that comes
  back enabled, or a paused-list entry naming a unit not tracked in
  `scripts/systemd/`, is still reported as drift. An empty list is valid and
  means the desk is fully running (owner unpaused the paper desk
  2026-09-15).
- **`tests/test_systemd_units.py`** gates the repository: no foreign
  `/home/*` path (the `/home/yebo` defect, pinned by name), `WorkingDirectory`
  at the deploy root, every `ExecStart` naming a script that exists, every
  timer paired and installable, every service reachable by a timer or
  `[Install]`, no inline credentials, and the six session units plus the API
  pinned by name.

Not yet deployed at the time of writing: the box still carries the pre-#175
`quant-agent-drift-check.service` and none of the alert-heartbeat, alert-digest
or unit-drift units. That is ordinary pending-deploy state, and the checks
above report it correctly.

**Alerting heartbeat (2026-08-31).** Every alarm on this desk is a Telegram
message, and nothing checked that a Telegram message can still be sent — so
"no alert arrived" meant both "nothing is wrong" and "the alarm is broken",
with nothing able to separate them. `scripts/alert_heartbeat.py` closes that:

- **Every session proves the channel — the primary mechanism.** All five to
  six weekday sessions (plus the ~14 `intra_check` ticks) exercise the whole
  Telegram path end to end before pushing their own message: one real send
  with `disable_notification`, deleted immediately after
  (`TelegramNotifier.probe`, reused). A present-but-revoked token, a wrong
  chat id, a blocked bot and a dropped egress route all pass a variable
  check and fail this. `src/alert_watchdog.py` owns the design.
- **The verdict is durable and rendered.** SQLite `alert_channel_checks`
  (capped at 2000 rows), read by `/health` as a four-state `alert_channel`:
  `ok` / `broken` / `stale` (last success older than
  `STALE_AFTER_HOURS = 26`) / `unknown`. `broken` and `stale` flip
  `/health.status` to `degraded` and render red in Mission Control;
  `unknown` is amber and stated in words, never "ok".
- **Silent on success.** A healthy channel sends nothing. A failed check
  forces a message out even from sessions that are normally silent; a
  recovery reports how many checks failed and when the channel last worked.
- **Daily probe, 06:15 ET, seven days a week**
  (`quant-agent-alert-heartbeat.timer`) — the floor, not the primary
  mechanism. Sessions run Mon-Fri only, so without a weekend check the 26h
  staleness threshold would fire every Saturday and have to be switched off.
  It also covers the one case sessions structurally cannot report: every
  session failing to start at all. Writes the same durable table plus
  `data/alerting/heartbeat.json` (gitignored, so it can never become deploy
  drift); a failure exits non-zero so the unit shows failed.
- **Detection latency.** Weekday: ~30 minutes (the next `intra_check`
  tick). Friday evening, after the week's last session: ~9 hours (the
  Saturday 06:15 ET probe). The Saturday, Sunday and Monday probes all land
  before Monday's first session at 08:00 ET, so the channel is proved
  before the week's first session needs it.
- **The bootstrap limit, stated plainly.** (A) The channel breaking while
  the box runs is the likely failure, is fully detectable locally, and is
  what the above covers. (B) The box itself being dead cannot be reported by
  anything running on the box. An external monitoring service would cover
  (B); that dependency was **refused outright**, so the old
  `ALERT_HEARTBEAT_HEALTHCHECK_URL` hook was deleted rather than left
  dormant, and a test keeps it gone. If the box dies, the owner finds out
  when he next looks — not fixed, not pretended otherwise.
- **The weekly Sunday digest is retired.** Its absence was supposed to be
  the signal, which allowed up to seven days of undetected breakage. The
  `--digest` flag, `run_digest`, `digest_text` and both
  `quant-agent-alert-digest.{service,timer}` units are deleted, and tests
  fail CI if a unit ships with `digest` in its name or `--digest` in its
  `ExecStart`.

## Mandatory paid-analysis cost circuit

Paid model requests share one persistent SQLite authority across systemd
processes. It fails closed before provider I/O on missing/corrupt accounting,
unknown or stale pricing/cost, unresolved attempted requests, excessive
provider attempts, session call count, or session/ET-day spend. Current
limits are **$0.90 per session** and **$2.75 per ET day**, checked against
real settled spend only, plus `max_calls_per_session` (40, a runaway-loop
backstop) and a provider-attempt cap per logical call computed in code
(`provider_attempt_budget()`, `src/agents/llm_attempts.py`) rather than pinned here.
**2026-09-02, the cost-circuit rewrite:** the per-mode session cap, the
separate per-session retry/repair-attempt limit, and the cost-reservation
layer they existed to manage were all deliberately deleted — see
`docs/INCIDENT_HISTORY.md`, 2026-09-03, "item 14: the budget guard was
stopping the desk on money it never spent".

Expected budget exhaustion creates the narrowest applicable quota hold: the
current run (`run_id`) for session cost, call-count or provider-attempt
exposure, or the current ET day for aggregate spend. Session holds do not
block later independent runs. Day holds rearm only after the ET date
advances, the new ledger seeds exactly, accounting invariants pass and no
prior-day attempted reservation remains unresolved. (A `mode_day` hold scope
still exists in the schema/rearm logic purely to recognize pre-2026-09-02
historical rows; nothing in the current code can create a new one — see
the same 2026-09-03 entry in `docs/INCIDENT_HISTORY.md`.) Trip and
successful rearm each send
one deduplicated Telegram alert. Missing/corrupt accounting, a row whose own cost is unknown (no telemetry,
or a fully-failed ambiguous attempt), unknown pricing, unresolved attempted
requests, provider-attempt exhaustion and any unrecognized trigger remain a
hard global latch requiring an auditable operator reset. A completed call
that reports a real cost after an earlier ambiguous attempt on the same
logical call keeps spend exact (`costs_exact=1`) and does **not** increment
`unknown_cost_rows` and does **not** hard-latch: the 2026-09-16 midday wipe
(event id=27) was that false latch while known spend was ~$0.65 of $2.75.
Caps still bind on the booked total. Reset never erases settled spend and
cannot bypass a quota hold. Caps are not raised to hide a latch. Operator
reset remains the product for a real unknown row.

Broker-resident stops, deterministic loss protection, order/fill reconciliation,
close/P&L jobs and the read-only API remain active under every hold/latch.

## Paper-beta engineering authority — accepted rule

While execution remains **Alpaca Paper**, already-authorized engineering work may run end-to-end without a human code-review, merge or deployment gate:

**`ubuntu` engineering → tests/inspection → dedicated PR → merge → governed `ubuntu` → `qamc` deploy → production verification → rollback if needed.**

Important consequences:

- Codex may complete implementation, self-review, merge and Paper-production deployment autonomously.
- Independent review is optional evidence, not permission and not a blocking gate.
- **CI is now a blocking gate.** As of 2026-08-27 `main` is protected: the `pytest`
  check is required, merges go through GitHub's native merge queue (ruleset on
  `main`, repo `redstone-hq/quant-agent`), and `enforce_admins` is true, so a failing
  suite blocks merges for everyone including administrators. Verified by merging a
  deliberately failing test (refused: `Required status check "pytest" is failing`).
- Changes to `OUTCOME.md`, `STATE.md` and `WORK.md` require **owner ratification**
  — agents propose, the owner accepts by merging. See `AGENTS.md`.
- Version control, the dedicated PR and known-good production state provide traceability and rollback.
- A failed production verification stops further mutation and triggers preservation/restoration of the last known-good state.
- This fast lane does **not** authorize live capital, paid dependencies, secrets/credential redesign, destructive infrastructure replacement or material architecture outside current authority.

## Parallel engineering policy — accepted rule

Parallelism and subagents are available and should be used when they reduce wall-clock time without creating coordination waste.

- Parallelize genuinely independent investigation, implementation surfaces, tests, browser/visual verification and evidence gathering.
- The lead agent owns integration and resolves conflicting findings.
- Do not spawn multiple workers to rediscover the same facts or overlap edits without a reason.
- Use strong reasoning models for architecture, trading logic, safety-sensitive changes, complex debugging, hard review and ambiguous product/UX judgment.
- Use cheaper/faster workers for bounded tests, searches, inventories, logs and mechanical evidence collection.
- Escalate a cheap worker when the task becomes reasoning-heavy.

This is an efficiency policy, not an agent-count target.

## Mission Control accepted state

Accepted behavior includes:

- professional cockpit composition using Tremor/TanStack for ordinary UI primitives;
- TradingView Lightweight Charts for price candles/volume and BUY/SELL markers;
- Dockview for the desktop workspace;
- QAMC-specific decision-chain topology as the justified custom visualization;
- current-session quote context exposed separately from historical bars and broker position marks through GET `/quotes`;
- historical daily candles are not presented as a live/current quote;
- automatic mode follows the best primary run and is labeled `AUTO / PRIMARY`;
- selected-session execution rows can drive chart context;
- accepted chart timeframes are `5m Today`, `15m`, `1h`, and `1D` using read-only market data;
- intraday OHLCV timestamps are preserved so execution markers can align to relevant candles;
- stale/degraded read-side data must be identified rather than silently represented as current;
- historical chart pan-to-load-more has no artificial day cap — Alpaca's own history depth is the only limit, not a client-side ceiling;
- chart zoom/pan state is user-controlled (persists across the live-quote poll; explicit "Reset zoom" control) rather than being silently reset by background polling;
- all chart time display (axis, crosshair, subtitle) is DST-aware US Eastern time, not unlabeled UTC;
- dividend/earnings markers on the price chart are sourced from `GET /events/{symbol}` (`MarketDataProvider.get_price_chart_events`, yfinance), separate from the single-next-value ex-div/earnings helpers stop adjustment and risk prompts use;
- a position's entry marker is placed at the real entry-fill bar, not an approximation;
- the Positions/Orders panel row uses a real resize sash (draggable, minimum-height floor) rather than a CSS-only hack that could crush the panel; dragging the chart bigger past that floor grows the workspace and that extra height persists across reload;
- the above-chart identity row is company name (from the cached profile; omitted when unknown) + ticker + Lifecycle — no holding grid and no decision strip under the candles; the default market-context symbol is not shown as a back-chip beside a real name; the decision one-liner lives inside Lifecycle; unrealized P&L stays on the chart ENTRY line and on Positions, not in that identity row or the quote subtitle; the price panel does not repeat the ticker as a second title above the candles;
- the cockpit default is chart-first: everything inside the Cockpit workspace is a resizable Dockview panel (Holdings, Account/NLV/liquidity, Sessions/decision, Chart, Positions, Orders). App chrome is the identity/status bar plus the Cockpit/Research Desk/Scorecard/Journal tabs. The chart pane is the largest region;
- Holdings cards keep their size, wrap at four per row on a normal desktop (fewer when the panel is narrower), and never use a horizontal slider; on iPad/phone the same cards remain a header strip;
- workspace vertical sashes work both ways: the sash above the chart borrows from the row above (Holdings/Account/Sessions by default), the sash below borrows from Positions/Orders then grows the page past that floor; extra height persists across reload;
- Cockpit tables clip and truncate with free column resize rather than forcing a page or section horizontal slider;
- Positions/Orders table columns support resize and drag-to-reorder, opt-in per table via `DataTable` props, persisted to `localStorage`; columns can shrink to ~1ch so Stop and Target stay on the blotter;
- a read-only Desk diary at `/diary` (and `/diary/`) served from gitignored `data/diary/`, same posture as `/board`; Mission Control does not generate the entries.
- a read-only `/holdings/{symbol}/why` endpoint (2026-09-18) that answers "why do we hold this" in plain English, assembled from the stored entry row plus that run's specialist evidence: one lede sentence with the numbers in it, a readable section (named primary driver, which seat raised it, insider who/how-much/when/at-what-price, the recorded invalidation condition, the pinned horizon, the reference target and the fact that nothing acts on it, and what has happened since entry), and a `raw_evidence` bucket for the accession numbers / internal flags / broker-eligibility JSON. It also reports OUR OWN purchase — the entry date, the fill price (or the order price, labelled as such when no fill was recorded) and the quantity — added 2026-09-18 because the owner asked for the purchase date and price by name and only the insider's were on file. **Consumed by the cockpit's "Why" tab** (2026-09-18), a Dockview panel that starts beside Candidates: clicking a symbol anywhere in the cockpit — holdings, positions, trades, orders — charts it as before and also fills this tab, fetched in `App.tsx` next to the chart symbol so a background tab is already populated when the owner switches to it. `raw_evidence` sits behind a collapsed "show the technical detail" toggle and is rendered as labelled rows, never as a payload dump. `LifecycleTimeline`'s generic `detailsText` renderer is untouched and still serves the run-detail modal.

The chart live-price/current-price truth issue is **already resolved**. Commit `796558f184f8dd800c7e1cbb57f11173ad3d6f6b` (`fix(qamc): show session fills and live chart price`, 2026-08-21) introduced the genuinely live `/quotes` path and separated live/current price from historical bars. Current `PriceChartPanel` also hides the historical series' default last-value line and renders explicit `LIVE` and `PREV CLOSE` lines. This is accepted behavior and is not an outstanding task.

## Trading-utility recovery — deployed

Production forensics for sessions 2026-08-18 through 2026-08-24 disproved the
prior claim that all mechanical blockers between opportunity discovery and
execution were corrected. Verified current-code gaps included batch snapshot
failure from Alpaca-incompatible class-share symbols, unmarketable BUY limits
coupled to a 15-second cancel guard, Risk parse failures labeled as rejection,
zero-order runs labeled executed, and SGOV funding sized before entry viability
with whole-order drops on partial funding.

Priority 1 is merged through PR #74 and deployed.

The final backend tranche adds fail-closed, machine-checkable PM specialist
provenance and holding validation; records PM model/parse/grounding failures as
agent failures; and extends the existing `specialist_evidence`/`trades` state so
the complete candidate/order/protection/outcome chain and deterministically
derivable realized P&L are queryable through the existing API. Production-scale
measurement supports changing only the PM seat to `openai/gpt-5.5`; Risk
routing and deterministic Python/broker authority remain unchanged. This final
tranche is merged through PR #75 and deployed.

The final Mission Control utility tranche is merged through PRs #76 and #77
and deployed. It removes ECharts/handmade allocation visuals, moves ordinary tables to TanStack,
keeps price history on Lightweight Charts, makes SGOV unambiguously cash
parking outside directional-risk/P&L emphasis, consumes PR #75 lifecycle facts,
and turns the desktop Candidates/Chart/Decision Room working surface into a
persisted Dockview workspace while retaining simple iPad/mobile tabs. The
Mission Control finish line and backend recovery promotion are complete.

The Research Intelligence Desk and Smart Money seat are merged through PRs #79,
#85 and #87 and deployed. The read-only daily projection, edited daily story,
evidence-backed change/tension/why-now treatment, restrained semantic language,
technical setup context, run-local PM/Risk/gate/execution deltas, persistent
desktop Dockview workspace, purpose-built iPad reading flow and truthful
sparse/degraded/after-bell states are production-verified against stored QAMC
data. The first-party SEC source is commissioned and preserves transaction
versus disclosure time, suppresses stale/lone noise, exposes deterministic
run-scoped admissions and cannot bypass the accepted decision chain. Paid Smart
Money synthesis remains suspended by the intentional cost circuit; that does
not block the completed read-side experience or deterministic source facts.

After promotion, acceptance still requires natural Alpaca Paper sessions
demonstrating the real chain:

**opportunity discovered → evaluated → defensible bullish/bearish/neutral decision → executed when eligible → managed/exited → measured**.

No trade may be forced or manufactured to create evidence.

## Intraday opportunity discovery

Production intraday discovery remains enabled by the one governed local config override.

Accepted live configuration:

- move threshold: **3.0%** absolute move from previous close;
- per-symbol cooldown: **3.0 hours**;
- maximum candidates per tick: **5**;
- approved bearish-expression ETFs: `SH`, `SDS`, `PSQ`, `SQQQ`.

Bearish expression currently runs through approved inverse ETFs. **Direct stock
shorting and margin are authorized as of 2026-08-27** (owner ratification; see
`docs/OUTCOME.md`) and are pending implementation — the broker layer contains no
short-selling capability at all today. The prior exclusion was an engineering scope
decision recorded here as an accepted constraint; it was never an owner requirement.
Options and theta strategies remain outside the accepted architecture.

*Correction (2026-08-30):* the "pending implementation" / "no short-selling
capability at all today" claim above is now false. Real short selling was built in
three stages — Stage 1 (a held short counts correctly in risk metrics), Stage 2
(protective-stop and trailing-stop geometry are short-aware), Stage 3 (the actual
open/cover order path, gated on the broker confirming the name is both
`shortable` and `easy_to_borrow`, `AlpacaBroker.get_shortability` in
`src/execution/broker.py`) — and all three stages are merged (PR #150) **and
confirmed deployed**, production's HEAD matching `origin/main`. `TradeDecision`
and the position-reviewer action enum now carry `SHORT`/`COVER` literals
(`src/models.py`). See `docs/phases.yaml`'s `phase_5` entry for the full
mechanical evidence. The four inverse ETFs remain in the trading universe as a
separate, still-live bearish-expression path alongside real shorting — retiring
them is noted as a residual, not yet decided.

Note also that nothing in the codebase currently tells the Portfolio Manager that
`SH`, `SDS`, `PSQ` and `SQQQ` are bearish instruments, so even the sanctioned
bearish expression is not actually wired up.

*Correction (2026-08-30):* also false as of today. `config/prompts/portfolio_manager.md`
now carries a dedicated "Inverse ETFs are bearish, not a hedge-flavoured long"
section stating that a long position in `SH`/`SDS`/`PSQ`/`SQQQ` is bearish
exposure and draws from the same book-wide `max_gross_bearish_pct` budget as an
outright short; `config/prompts/risk_manager.md`'s "Short discipline" section
states the identical rule for audit purposes. Both prompts also carve out the
inverse case correctly: shorting one of these funds is a bullish bet on the
index, not additional bearish exposure, and is excluded from that budget.

## Model / provider policy

- OpenRouter remains the model-provider path.
- Current engineering routing uses `openai/gpt-5.5` for Portfolio Manager,
  `qwen/qwen3-235b-a22b-2507` for Risk Manager, and
  `google/gemini-2.5-flash-lite` for the remaining seats, according to the
  measured per-seat policy. Production remains on its separately promoted config.
- **Merged and deployed** (`16f6535`, branch `feat/pm-flex-routing`, PR #113):
  the Portfolio Manager's `openai/gpt-5.5` calls now
  prefer OpenRouter's `openai/flex` endpoint over `openai`/`azure` — the same
  model weights at half the per-token price. This is an endpoint preference
  (`llm.<agent>_provider_order`), not a routing change, and it is rejected at
  config load on any seat not on OpenRouter. See `docs/architecture/MODEL_ROUTING_POLICY.md`.
  See "Production position" above for how to check current deploy status.

## Market-data feed finding — resolved, not an active defect

The previously flagged concern that `src/execution/broker.py::get_latest_price` omits an explicit Alpaca feed is **not an established defect**. Alpaca's current latest-trade/latest-quote behavior defaults to the best feed available to the subscription; for this account that is IEX. Independent probes confirmed current IEX latest trade/quote data succeeds while explicitly requesting SIP for real-time data is rejected as unsubscribed, which is expected. That rejection is real-time only: Alpaca serves SIP free when a request's `end` is at least 15 minutes old (Alpaca market-data FAQ), so since 2026-10-10 both daily-history reads (`get_bars`, `get_bars_batch`) ask for SIP with `end` clamped to now-16min. Measured 2026-10-10 on this account: feed=sip daily bars return 200; IEX carries ~3% of volume (AAPL median daily $ volume IEX $376M vs SIP $12.6B) and printed flat high==low days on thin names (ACNB 86 of 257) where SIP shows 0-1. Latest trade/quote, snapshots and intraday chart bars stay on IEX.

**DEFECT, observed 2026-08-26 — FIXED 2026-08-30 (news half earlier).** Production
logs at the time showed the Reuters Business feed returning HTTP 404 and AP Business
returning HTTP 403 (16 occurrences each), repeated FRED macro API timeouts (13), 28
incomplete Tech batches, and 11 `Portfolio decision failed deterministic grounding`
errors. Reuters/AP were dead feeds removed on purpose (both required a paid
subscription to restore, not a code fix) with free substitutes added in their place;
FRED's retry policy has since been rebuilt (config-driven retries, exponential
backoff with jitter, a real wall-clock ceiling) and its coverage now surfaces to the
operator the same way news coverage already did. Repair is `docs/QAMC_REMEDIATION_SPEC.md`
Phase 4.2 — see `docs/phases.yaml`'s `phase_4` entry for current mechanical evidence.
Genuinely still open: full wire-service breadth needs a paid subscription and an
owner decision. The macro event calendar was built on 2026-08-31
(`src/data/event_calendar.py`): FRED's free release-dates API supplies the forward
schedule for CPI, Employment Situation (NFP), PPI, PCE, GDP, retail sales and
jobless claims, threaded into both the macro analyst and the Risk Manager with a
`MacroCoverage`-shaped coverage line. Since 2026-09-23 the FRED schedules are
FETCHED over `RELEASE_SCHEDULE_LOOKAHEAD_DAYS` (120 days, four monthly cadences)
and FILTERED to the caller's `horizon_days`, which is unchanged: the fetch window
used to be the horizon itself, so the monthly releases returned nothing for most
of the month and the calendar recorded them as the source failure
`no_scheduled_dates_published` — measured live on 2026-09-23, CPI, PPI and retail
sales each returned 0 dates over 10 days and 3 over 120. A release whose schedule
is published but whose next date sits past the horizon is now a SUCCESS, its next
date carried on `EventCalendarCoverage.next_beyond_horizon` and rendered to the
seats as scheduled-but-not-imminent; only an empty 120-day window is a failure.
**FOMC meeting dates are covered as of
2026-08-31** (same module, same block): FRED cannot supply them — release 101 reports
as a daily release, so its date list is every calendar day — so the schedule comes
from the Federal Reserve's own free calendar instead. `https://www.federalreserve.gov/
json/calendar.json` is the primary source (structured JSON; the rows typed `FOMC` and
titled `FOMC Meeting` carry the concluding date and the block length), and the
rendered `fomccalendars.htm` page is a fallback used only when that feed fails or its
schedule stops before the end of the horizon — which it does at every year boundary,
since the feed carries the current year and the page already carries the next. No key,
no cost. The schedule is cached at `data/fomc_calendar.json` and a stale cache is
served labelled `measured_from_stale_cache` with its age, never silently. An empty
meeting list is only allowed to read as "no meeting scheduled" when the coverage line
says the published schedule spans the whole horizon; every other case reads UNKNOWN.

`get_latest_price` returning `None` on a genuine market-data exception is an intentional fail-closed/degradation contract and is covered by existing tests. Do not change this trading-critical method merely because `feed` is omitted. Reopen it only on concrete production evidence of incorrect behavior.

## Not authorized

- Live-broker order submission or live-capital activation.
- Options/theta strategies. (Direct stock shorting and margin were authorized
  2026-08-27, see "Intraday opportunity discovery" above — pending
  implementation, not yet in production; see `docs/WORK.md` Phase 5.)
- New timers, daemons, databases, proxies, credential systems or durable infrastructure without explicit architectural approval.
- Deterministic risk/execution semantic redesign outside accepted work.
- Paper-specific trading shortcuts that would create a second trading architecture.
- Broker-write Mission Control controls.
- Telegram command/control.
- Public exposure of QAMC or OneCLI.
- Turning `qamc` into a development/operator account.
- Expanding `dev` permissions or reintroducing it into the normal workflow during stabilization without explicit authorization.
- Forcing/manufacturing trades for validation.
- Live-capital promotion without separate explicit authorization.

Live-capital activation is mechanically gated, not merely prohibited in prose:
`AlpacaConfig._enforce_paper_only` (`src/config/__init__.py`) will not build a non-paper
config unless `config.LIVE_TRADING_AUTHORIZED` has been flipped in a reviewed
change AND the live-capital pre-flight gate
(`src/live_capital_preflight.py`, board item 150) reports every condition
satisfied. The gate — not `docs/FUTURE.md` — is the source of truth for the
pre-flight checklist.

## Handoff

Current bounded activities:

1. **Natural Alpaca Paper acceptance:** the Aug 26 rerun proved source,
   admission, full Technical coverage and PM candidate generation. The first
   post-PR #93 eligible session must still prove PM → AI Risk → deterministic
   gate → execution when eligible, followed by management/exit and measurement.
2. **No artificial activity:** do not exceed paid-session limits, force a trade
   or weaken evidence/risk thresholds. A no-trade result must be specific and
   defensible rather than caused by a pipeline defect.
3. **Evidence-driven follow-up only:** do not reopen resolved dashboard or
   trading-critical feed defects from historical notes alone; require current
   evidence.

See `docs/WORK.md` for the active contract and exact Research Intelligence acceptance criteria.

## Note, 2026-09-02 — the reward:risk floor was not a floor

Appended, not a rewrite of the Phase 2b paragraph above (that paragraph
has since had its pass, 2026-09-12 — the rejection described below was
itself removed on 2026-09-11, see docs/WORK.md item 1(d); what follows is
the history of the floor as it stood on 2026-09-02). That paragraph then
said the constructor "rejects the trade if the widened stop drops
reward:risk below `risk.min_reward_risk_after_widening` (1.5)". True at the
time — and until 2026-09-02 that was ALL it did. The check sat inside the widening branches, behind
early returns for "stop already outside the noise band" and "no ATR
reading", so an entry whose stop was wide enough to begin with was never
judged against the floor at all.

Measured on the pre-reset production database
(`data/resets/20260902T181859Z/quant_agent.db`), 2026-08-18 to 2026-09-02:
14 of the 49 constructed entry orders shipped with a reward:risk under 1.5,
as low as 0.43; the constructor's refusal message appears zero times in the
whole production log history before 2026-09-02; two of the 14 reached the
broker and XLE on 2026-08-21 FILLED 9 shares at $64.26 on a ratio of 0.81.
The rest were caught by the Risk Manager's prose or by the 1.2
execution-time belt — a deterministic floor enforced by a language model.

Fixed on `wt/rr-geometry`: the floor is applied once, to the stop that will
actually ship, whichever rule placed it. The threshold did not move and no
path became more permissive. Details and the full measurement are in spec
§12.1b / §12.1c. **Not deployed.**
