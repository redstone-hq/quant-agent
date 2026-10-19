# QAMC Remediation Specification

**Author:** Claude (Opus 5), with Rex Redstone
**Date:** 2026-08-27
**Status:** Owner-ratified 2026-08-27; historical plan. Since 2026-10-19 the seat doctrine in `docs/OUTCOME.md`, "The desk archetype: specialists, weighed, then one head trader" (owner, 2026-10-19) governs, and where this document counts seats, sums equal votes, reads a net sign as the rule, or has specialists debate, the archetype wins.

---

## 0. The Mandate (corrected)

QAMC exists to **make money**. It is not a research experiment into whether cheap models add trading value. `docs/OUTCOME.md` currently states the latter; that framing was authored by coding agents and is wrong.

**The edge, stated so it is testable:**

> **Breadth × consistency × asymmetry.** Underwrite ~100 liquid names daily across technicals, fundamentals, news, macro and insider flow — coverage no individual sustains. Act identically every time, long or short. Risk a bounded fraction per idea, cut losers at pre-defined structure, and let winners run. Be right ~51% of the time and make roughly twice as much when right as is lost when wrong.

**Operating parameters:**

| Parameter | Value |
|---|---|
| Horizon | Swing — days to weeks |
| Direction | Long **and** short (real shorting, Alpaca margin) |
| Autonomy | Fully hands-off. The system never asks for trade approval. |
| Capital deployment | **Fully deployed.** No idle T-bill parking unless nothing is worth owning. |
| Max risk per trade | **5% of equity** (ceiling, not target) |
| Min risk per trade | **0.5%** — below this, don't trade |
| Max total at risk | **25% of equity**, correlation-aware |
| Environment | Alpaca **Paper** until explicitly authorized otherwise |

**Governing principle: agents decide, deterministic code bounds.**
The desk chooses what to buy, how much conviction it carries, where the stop belongs, when to exit. Deterministic Python enforces only the outer envelope — the limits that must survive a model having a bad day.

---

## Phase 0 — Gates (blocking; nothing else merges until done)

Nothing below should be merged until there is a working test gate. 94 PRs have already merged ungated; adding trading-logic changes to that pile would be reckless.

**0.1 — Enable CI.**
`.github/workflows/test.yml` exists but has **never executed**. Root cause is almost certainly that **GitHub disables Actions by default on forked repositories**.
- Enable Actions: repo Settings → Actions → General → Allow all actions. *(Owner action.)*
- Update the workflow to reflect current test/build requirements.
- Prove it runs green on `main`.
- **Deliberately push a failing test and confirm the merge is blocked.** This is the only step that constitutes proof. Do not skip it.
- Add branch protection on `main` requiring the check to pass. A gate that reports but does not block is not a gate.

*Correction (2026-09-19): this section describes the 2026-08-27 state. CI was enabled shortly afterward (fixed in #96) and has run on every PR since; it is not currently disabled or unexecuted.*

**0.2 — Ratification rule.**
Root cause of QAMC's drift: coding agents write the documents that define "accepted truth," and `STATE.md` declares independent review *"optional evidence, not permission and not a blocking gate."* Scope decisions made for engineering convenience (e.g. dropping short selling) laundered into requirements that every later agent treated as owner intent.

- Amend `AGENTS.md`: agents may **propose** changes to `STATE.md` / `WORK.md` / `OUTCOME.md`; only the owner may **accept** them. Proposals live in the PR description until ratified.
- Any existing constraint in those documents is **unverified** until the owner confirms it.

**0.3 — Commit the production config drift.**
Live `config/settings.yaml` has uncommitted edits. `STATE.md` claims exactly one intended delta. Reconcile and commit, so what runs is what is in git.

---

## Phase 1 — Tech Analyst returns real structure — IMPLEMENTED (branch `feat/tech-analyst-structural-levels`)

Everything downstream depends on this. Stops, targets, thesis progress and pace are all currently derived from *invented* numbers because the analyst supplies no levels.

**Status note (2026-08-27):** the substance of 1.1–1.4 is implemented, with two departures from this text worth recording:

- The spec names a new `suggested_stop_price` field on the Tech Analyst's output. The implementation reuses the existing `stop_loss` field on `TechAnalysisResult` (`src/models.py`) instead of adding a differently-named one; `suggested_stop_price` remains a distinct field elsewhere, on the Portfolio Manager's `TargetPosition`, which `PortfolioConstructor._resolve_stop` still checks first.
- `reference_target` was made **mandatory** for every actionable rating (not "only where structure supports one" as originally written) — the validator in `src/models.py` requires it, `setup_type`, `expected_horizon_sessions`, and at least one support/resistance level together, or the result is rejected. For a `"breakout"` setup, `reference_target` is documented (`config/prompts/tech_analyst.md`) as a measured-move projection rather than a defended level.
- Two things were added beyond this section's original text: a standalone deterministic levels module (`src/data/levels.py`, new) that finds support/resistance from OHLCV bars via bad-print filtering, swing-pivot detection, zone clustering, distance/recency-weighted ranking; and raising `trading.lookback_days` from 320 to 1800 (~5 years, `config/settings.yaml`) so those levels are computed over real multi-year history rather than the ~1-year window the 320-day figure implied.

**1.1 — Require structural levels. DONE.**
The Tech Analyst must return, per candidate:
- `support_levels[]`, `resistance_levels[]` — actual prior consolidation / swing structure. **Done** — computed by `src/data/levels.py` over the full fetched history and required by the validator for actionable ratings.
- `suggested_stop_price` — derived from structure, never from a volatility multiple. **Done, via the existing `stop_loss` field** rather than a new field of this name (see status note above).
- `setup_type` — see 1.2. **Done** — `Literal["range", "breakout"]` on `TechAnalysisResult`, required for actionable ratings.
- `reference_target` — only where structure supports one. **Done, but made unconditionally required** for actionable ratings rather than conditional (see status note above).
- `expected_horizon_sessions` — the analyst's own estimate of time-to-resolution. **Done**, required for actionable ratings.

**1.2 — Classify the setup.** Two types, managed differently. **`setup_type` field is implemented and required; the differentiated exit-management behavior in the table below (fixed target vs. trailing-only, progress/pace enable/disable) is NOT implemented — that is Phase 3 work and remains pending.**

| Type | Definition | Stop | Exit management |
|---|---|---|---|
| **A — Range / level** | Clear overhead resistance exists | Below structural support | Fixed target; progress & pace metrics valid |
| **B — Breakout / trend** | No overhead resistance (all-time highs, clean break) | Below the breakout level or prior consolidation high | **Trailing only. No fixed target. Progress and pace metrics DISABLED.** |

**1.3 — No levels, no trade. DONE; first half SUPERSEDED 2026-10-09.** The model validator no longer requires an analyst-named support/resistance level or a `reference_target` (owner rule 2026-10-09: a missing chart level or target never blocks a trade; both are recorded as "no level named" / "no target", and the stop and take-profit come only from measured levels). Originally: `PortfolioConstructor._resolve_stop` rejects (returns `None`) a BUY with no structural stop. The `2 × ATR` and `5%` fallbacks are deleted from `src/portfolio_constructor.py` (and the `default_stop_atr_multiple` / `fallback_stop_pct` config fields removed with them). `atr_14` is retained on `TechAnalysisResult` for a future noise-band use (Phase 3); it is no longer read as a stop source anywhere in `portfolio_constructor.py`.

**1.4 — Delete the invented target. DONE.** `entry × (1 + 2 × stop_gap_pct)` is removed from `portfolio_constructor.py`; a BUY with no `reference_target` from the analyst is now rejected rather than assigned a synthesized one, so a Type B (breakout) position without a real reference is a declined trade today rather than "no target, handled as a first-class state" — the latter is Phase 3 exit-rework territory (disabling progress/pace for breakouts) and remains pending.

**Phase 1b — market context. DONE, not in the original spec text.** Structural levels alone left the Tech Analyst without most of what a technical analyst actually reasons about — it had moving averages, RSI, MACD, Bollinger bands, ATR and a single volume-change percentage, and nothing else. `src/data/context.py` (new) computes, deterministically from bars already fetched, and renders via `format_context_block()` ahead of the levels block in the prompt:

- Relative strength vs a benchmark drawn from the same batch (SPY, else QQQ, else IWM — no extra fetch), over 1m and 3m.
- Returns over 1w / 1m / 3m / 6m / 12m.
- Position within the trailing 52-week range.
- ATR as a percentage of price, its percentile against its own trailing year, and an `expanding` / `contracting` / `stable` volatility state (thresholds: 70th / 30th percentile of the trailing 252 sessions).
  - **Corrected 2026-09-01.** This block originally computed its own "ATR" by convolving the true ranges with a flat 14-wide kernel — a simple moving average of true range, not an ATR. The risk path (`atr_14`, used for stop widening and sizing) has always used Wilder's smoothing via `ta.volatility.AverageTrueRange`, so the analyst was shown one volatility reading while the deterministic layer acted on another. There is now one implementation, `src/data/technical.py::atr_series` (Wilder's, warm-up trimmed), and `context.py` imports it. Measured over the configured 101-symbol universe across 973 real sessions (2022-10-13 to 2026-08-31, 93,712 symbol-days): the two disagreed by a mean absolute **7.05%** (median 5.50%, p90 15.0%, worst +95.9% on GME 2024-07-09), and `volatility_state` changes on **17.20%** of symbol-days — near-symmetrically (49.3% of the changes toward more volatile, 50.7% toward less). The net effect is that `stable` shrinks 2.76 points and both tails grow; it is not a systematic re-rating in one direction. Pinned by `tests/test_atr_is_wilder_everywhere.py`.
- MA20 / MA50 / MA200 slopes over a 10-session lookback — trend direction of the averages, not merely price's position against them.
- Consolidation detection: a candidate window must be **both** narrow (≤8% high/low spread over 15 sessions) **and** low-drift (net move ≤50% of the range) to be flagged — a narrow window alone does not distinguish a base from a slow steady trend.
- 20-day average dollar volume and 20-day up/down volume ratio (accumulation vs. distribution).
- Unfilled price gaps (≥2%, up to 3 most recent).

`src/agents/tech_analyst.py` wires this in per symbol and accepts an optional `days_to_earnings` kwarg to render an earnings-proximity warning line. **Not wired end to end**: `src/data/market.py` adds `MarketDataProvider.get_next_earnings_date()`, which estimates trading sessions to the next scheduled earnings report (approximate — calendar days × 5/7, not a precise trading-calendar count) — the first place in the system that has ever known a *future* earnings date, as distinct from `src/data/earnings.py`, which is retrospective (it finds filings already on EDGAR). As of 2026-08-31 that method **is called** — by `src/data/event_calendar.py::fetch_earnings_proximity`, which sweeps it for every symbol the Risk Manager is judging and renders a session count or a NAMED absence into RM's Event Risk block. **Still true:** nothing passes `days_to_earnings` to the Tech Analyst — that kwarg is accepted and handled but always empty in practice today. `tests/test_context.py` (new, 27 tests) covers the module.

**Status of everything below (2026-08-27, corrected 2026-08-28).** This line
was previously a blanket "none of this work has been implemented", which
stopped being true the moment Phase 2a landed and is exactly the kind of
stale absolute that misleads the next session. The status below is itself
dated and will go stale the same way — it is not a live pointer. For what is
actually deployed right now: `sudo -n -u qamc git -C /home/qamc/quant-agent
log --oneline -1`, cross-referenced against `docs/STATE.md` → "Production
position" and `docs/WORK.md`'s ordered backlog. As of the dates given below:

- **Phase 2a** — merged and deployed (PR #106). The four audit-finding fold-ins: deterministic
  drawdown gate, correlation to the PM, portfolio heat, R-multiple.
- **Phase 2b** (§2.1 risk-based sizing, §2.2 correlation-aware budgeting,
  §2.4 retire the position-count concept) — merged and deployed 2026-08-27
  evening (commit `75c0233`, branch `feat/pm-flex-routing`, merged as
  PR #113). `max_portfolio_risk_pct` is
  now an enforced gate, not a reported figure. Two follow-on fixes landed the
  same day, same branch, also merged and deployed: the constructor now clamps to
  the 20% single-name ceiling instead of proposing orders the engine
  hard-blocks (`b712f4c`), and entry stops sitting inside ordinary volatility
  are widened to a regime/setup-scaled ATR floor before that clamp is even
  computed (`3dff940`) — see the Phase 2/Phase 3 sections below.
- **Phase 3 (all of 3.1–3.4, 3.6, 3.7)** — merged and deployed 2026-08-27
  ~09:20 ET to the live
  paper account at `058273f1`. §3.5 resolved as an owner decision rather than
  implemented (see the correction note under §3.5); §3.8 unchanged by design.
- **Phase 4.1** — merged and deployed 2026-08-27 evening (commit `fb88e08`,
  branch `feat/pm-flex-routing`, merged as PR #113): the intraday PM is no
  longer blindfolded to macro/news.
  **Phase 4.2** (feed repair) — not started as of 2026-08-27.
- **Phase 5** (short selling) — now a three-stage plan, not the single "roughly
  a day" task this document originally scoped it as. Stage 1 (make shorts
  countable) merged into `main` as PR #116; stages 2 and 3 not started. See
  `docs/WORK.md`'s Phase 5 backlog entry for current status — deploy status
  changes daily and is not tracked here.
- **Phases 6–7** — not started as of 2026-08-27.

The owner reordered Phase 3 ahead of the rest of Phase 2 on 2026-08-27; the
evidence and the decision are recorded in `docs/WORK.md`. The execution-order
diagram at the foot of this document reflects the ORIGINAL dependency analysis
and has not been redrawn.

---

## Phase 2 — Risk-based sizing and correlation-aware budgeting

**Status note (2026-08-27, commit `c89e957`, branch `feat/risk-metrics-and-pm-correlation`, merged and deployed):** landed as "Phase 2a" is the deterministic risk arithmetic this phase depends on — it closes four `AGENT_ROLE_AUDIT.md` audit findings (§1.1–§1.4), not this section's own numbered items:

- The drawdown-halve (audit §1.1) moved from a PM-prompt instruction into `src/risk/rules.py::apply_drawdown_scale`, with `drawdown_buy_cap` as a hard-block backstop. The PM prompt no longer pre-applies it.
- The correlation matrix (audit §1.2) is now built before PM decides (`TradingPipeline._ensure_correlation_matrix`) and PM's Quantitative Facts render the measured clusters (`src/data/correlation.py::correlation_clusters`).
- Portfolio heat (audit §1.3) — budget risk and open risk — is computed in `src/risk/metrics.py` and rendered to PM and RM with headroom under a new `risk.max_portfolio_risk_pct` config field (default 25). **That ceiling is reporting-only** — nothing gates on it yet.
- R-multiple (audit §1.4) is computed against the entry stop and rendered to the Position Reviewer.
- `src/risk/metrics.py`'s budget-risk arithmetic already implements §2.3's release condition below (a stop at or above entry zeroes that position's budget contribution) and the module docstring cites §2.3 directly — but this is exposed only as a reported figure. No gate consumes it, so the book does not yet actually expand/contract on it.

**Phase 2b — COMPLETE (2026-08-27).**

- **§2.1 landed.** `TargetPosition.risk_allocation_pct` (0.5–5.0, owner-ratified envelope) replaces `target_weight_pct` as the live sizing field, and `PortfolioConstructor._plan_risk_targets` derives the weight as `risk_pct x entry / (entry - stop)` — the §2.1 formula with the equity term cancelled, so it needs the stop distance and nothing else. `target_weight_pct` is retained as Optional purely so historical `agent_logs` / `specialist_evidence` rows still parse through `src/replay.py` and the Mission Control API; a target carrying neither field is rejected at the schema. `config/prompts/portfolio_manager.md` now emits risk, and its conviction bands have been through two revisions since: originally 2.0–4.0 / 1.0–2.5 / 0.5–1.0, then recalibrated (2026-08-27, `3dff940` + doc-sync, merged and deployed as PR #113) to 1.5–3.0 / 1.0–2.0 / 0.5–1.0 once the stop-width fix below changed what the 20% single-name ceiling actually leaves — see that entry for why. The formula now reads `risk = min(raw, {{risk.max_position_risk_pct}})`, pinned by `tests/test_prompts_anchors.py`; the `queued_cap` term it carried until 2026-09-14 was deleted with item 62 (a hand-typed ceiling with no settings key, no code behind it and no derivation — a just-filed name already reaches the formula with one fewer agreeing seat).
- **§2.2 landed** as `src/risk/budget.py::allocate_risk_budget`, a pure function the constructor calls before sizing anything. Total ceiling 25%, per-cluster cap 40% of it (10% of equity), clusters from measured return correlation rather than a hand-maintained sector table. Rationing is largest-request-first with an alphabetical tie-break, so the outcome never depends on the order the PM listed its targets; a request rationed below the 0.5% floor is denied outright rather than shrunk to a token position. Every cut carries a note into the order's reasoning, because on 2026-08-20 an unexplained deterministic cut read to the AI Risk Manager as "plan inconsistency" and drew a full-plan veto.
- **§2.4 verified already satisfied, and now genuinely so.** A grep of `config/prompts/` and `src/` confirms no fixed position-count target exists anywhere: `PMFacts.position_count` is a reported figure only. That was true before this tranche too — but §2.4 asks for the count to be *determined* by the budget, and until §2.2 was a gate rather than a report, nothing determined it at all. It now falls out of the risk budget: the book expands as stops reach entry and release budget (§2.3, already computed in `src/risk/metrics.py`) and contracts as they do not.

**Two fixes on top of §2.1, found the same day, both merged and deployed 2026-08-27 evening as PR #113:**

- **The 20% single-name ceiling was reachable but unenforced by the constructor (`b712f4c`).** `risk.max_position_pct` sits in the risk engine's `HARD_BLOCK_RULES` — it drops an oversized order outright, it does not trim it. Nothing connected §2.1's sizing to that ceiling, so at this book's real stop distances (median 4.3%) any conviction above roughly 1% risk computed a notional weight the engine would refuse, and the failure mode was a silently empty book, not an error. One combination (4% risk, 3% stop) computed a 133% allocation and raised an uncaught `ValidationError` inside `construct_orders`, taking down order construction for the whole session. The constructor now clamps to the same ceiling the engine enforces (wired from `risk.max_position_pct`, same setting) and states in the order's reasoning that the position therefore risks LESS than the PM allocated — silently under-delivering risk is exactly the gap this closes, and an unexplained deterministic cut is what drew the 2026-08-20 veto.
- **The stops feeding that formula were themselves too tight (`3dff940`).** See the root-cause note under §3.6 — the same 1.7-ATR median stop distance that fires exits inside ordinary volatility also, mechanically, forces the 20% clamp above to bind at nearly every conviction level. `PortfolioConstructor` now widens an entry stop placed inside `risk.min_stop_atr_multiple` ATRs (base 3.0, scaled 0.85x/1.15x by breakout/range setup type and 0.95x/1.10x/1.20x by risk-on/transitional/risk-off macro regime) and rejects the trade if the resulting reward:risk drops below `risk.min_reward_risk_after_widening` (1.5). Measured effect: MSFT 2.4% → 7.0%, VLO 4.5% → 9.2%, OKLO 7.7% → 24.7%, and 0.5/1.0/1.5% conviction now produces 7.1/14.2/20.0% positions instead of clamping all three to 20% — conviction changes size again, the entire premise of §2.1. 2487 tests pass. **This paragraph does not track live deploy status** — see `docs/STATE.md` → "Production position" for how to check what production is actually running.

**The one thing §2.2 does NOT do:** the gate is enforced only when the caller supplies `existing_risk_pct` and `clusters`. `pipeline_stages._book_risk_inputs` supplies both from `ctx.facts` — the same numbers the PM was shown before it decided — and returns `(None, None)` when the facts are unavailable, which leaves the portfolio ceilings unenforced. That is deliberate: enforcing a 25% ceiling against a book that cannot be measured would either block every trade or wave everything through. Per-position sizing and the 5% single-name cap still apply in that state.

**2.1 — Conviction is expressed as risk, not notional.**
The PM currently emits "BUY OKLO 3%" — percent of portfolio, which is risk-blind. A 3% position with a 10% stop risks 0.3% of equity; the same position with a 2% stop risks 0.06%.

Replace with a risk allocation in `[0.5%, 5.0%]` of equity. Share count is then derived:

```
shares = (equity × risk_pct) ÷ |entry_price − stop_price|
```

A wider stop yields a smaller position rather than a rejected trade. This eliminates the entire "stops too tight" failure class — risk is never controlled by squeezing the stop.

**2.2 — Correlation-aware portfolio budget.**
Total at-risk ceiling is **25% of equity**, but correlated names consume **one bet's** budget, not several.
- Cluster by sector *and* thematic exposure. `OKLO / CEG / VST / CCJ` is one nuclear bet. `NVDA / AMD / AVGO / MU / TSM / SMH / SOXX` is one semiconductor bet.
- Budget consumed by a cluster = the **sum** of its members' risk, and a single cluster may not exceed a defined share of the total (suggest 40%).
- Rewards genuine diversification; refuses to pay for fake diversification.

**2.3 — Risk is released, not static.**
Once a position's trailing stop sits **at or above entry**, its risk contribution drops to zero (or negative) and it stops consuming budget. The book therefore expands when trades are working and contracts when they are not — automatically, with nobody choosing a position count.

**2.4 — Retire the fixed position-count concept.** There is no target number of positions. The correlation-aware budget determines it dynamically.

---

## Phase 3 — Exit rework — **COMPLETE, DEPLOYED 2026-08-27**

**Status.** §3.1 and §3.2 landed in PR #107; §3.3 and §3.4 in PR #108; §3.6 and
§3.7 in PR #109. All are deployed to the live paper account at `058273f1`.
§3.5 was **resolved as an owner decision rather than implemented**: its
"weakest model in the stack" premise is contradicted by
`ops/model_policy/results/merged.json`, where `google/gemini-2.5-flash-lite`
scores `quality_min 1.0` at `midday_exit` tied with four other models
including `openai/gpt-5.5` (which costs ~84x more per review), and scores 1.0
on every scenario it was measured on. The `midday_exit` scenario ties five of
twelve candidates at the ceiling and therefore does not discriminate; the
agreed path is to build a scenario that does. §3.8 is unchanged — the reviewer
retains full authority to exit on new information.


This is where the money has been going.

**Superseded by the Status block above.** This note originally tracked 3.1–3.3
as the only landed items with 3.4–3.7 "NOT started"; all of Phase 3 except the
owner-resolved §3.5 has since shipped and deployed (`058273f1`). Left here only
so the per-item detail below still reads as a record of what was fixed and why,
not as a live status line.

**3.1 — Kill the pace feedback loop. (Highest priority defect in the system.) DONE.**
`pipeline.py:6318` computed `pace = progress ÷ (days_held ÷ avg_hold_days)` where `avg_hold_days` was drawn from the system's **own rolling 30-day realized-trade calibration** (currently ≈2.0 days). Selling quickly shrank that figure, which made every position look stalled, which drove more selling. It was a self-tightening noose.

- Replace `avg_hold_days` with the **`expected_horizon_sessions` pinned at entry** from the Tech Analyst (Phase 1.1). **Done** — `expected_horizon_sessions` and `setup_type` are persisted on the `trades` row at BUY time (new columns via `_ensure_column`, `src/storage/db.py`; `insert_trade` takes both as new kwargs); `ExecutionStage` (`src/pipeline_stages.py`) looks them up from `ctx.analyses` to supply them.
- Never derive a trade's expected horizon from the system's own past behaviour. **Done** — the calibration query is deleted from `run_position_review`, and `avg_hold_days` is removed from `_build_position_facts`'s signature entirely (`src/pipeline.py`), so it cannot be reconnected by accident.
- Do not evaluate pace until at least **one third** of the pinned horizon has elapsed. Before that the metric is mathematically meaningless. **Done** — `pace_status="too_early"` below that threshold; no figure is produced.
- Disable progress and pace entirely for Type B (trend) positions. **Done** — `pace_status="n/a_breakout"`.

A fourth state exists beyond this section's original three: `pace_status="unavailable_no_pinned_horizon"`, for positions opened before this landed and carrying no pinned horizon — they get no figure rather than an inferred one. `pace_status="measured"` is the normal case. `config/prompts/position_reviewer.md` renders why pace is absent in each of the three non-measured cases, because a missing number that reads as "nothing to see" is how a day-2 position gets called stalled.

**3.2 — Give the reviewer memory. DONE.**
`_build_own_recent_decisions` (`pipeline.py:6369-6409`) discards HOLDs and never passes prior metric values. The reviewer sold EPD for "not progressing" when progress had risen 16% → 20% and distance-to-stop had *improved*.
- Persist each review's metrics per position. **Done** — `db.save_position_review_metrics` snapshots per-position metrics to `specialist_evidence` (`agent_name="position_reviewer"`, `kind="review_metrics"`).
- Pass the previous snapshot into the next review. **Done** — `db.get_prior_position_review_metrics`, plus new pipeline methods `_build_review_metric_deltas` and `_persist_review_metrics` (`src/pipeline.py`).
- Deterministically reject a deterioration verdict when the deltas are positive. A model may not claim a position is stalling while its own numbers improve. **Done** — new `src/risk/exit_guard.py` (`MetricDeltas`, `compute_deltas`, `is_deterioration_claim`, `veto_contradicted_exit`). `_midday_execute_llm_actions` now takes `metric_deltas` and drops a SELL/REDUCE whose stated reason is a deterioration claim when every metric that moved since the prior review improved, recording `exit_vetoed_contradicts_own_metrics`. Exits on new information (news, earnings, regime shift, correlation breach, a triggered `thesis_invalid_if`) are never vetoed. A mixed picture is deliberately not vetoed either — that stays the reviewer's judgment call.

**3.3 — Close the first-sale-of-the-day loophole. DONE.**
The hard-trigger gate previously applied only to symbols already trimmed that day, so a position's *first* sale executed on soft reasoning unchecked — which is almost every sale. **Done** — commit `2f177e33` (branch `feat/exit-gate-and-risk-routing`): `src/pipeline.py` now runs `_reason_cites_hard_trigger` against every `SELL`/`REDUCE`, not just a symbol's second sell-side action of the day; a non-matching reason is dropped and logged as `exit_blocked_no_named_trigger`, and the position holds, protected by its broker-resident stop. The `_HARD_TRIGGER_KEYWORDS` vocabulary was widened first — macro regime shift ("regime shift"/"regime flip"/"risk-off"), "sector shock", "adverse news", "material news", "earnings miss", "guidance cut" — all sanctioned by §3.8 and previously unrepresented, so gating every exit against the narrower list would have blocked legitimate ones. Concentration and drift were considered and deliberately NOT added: "Concentration drift; valuation stretched" is the verbatim shape of the reason behind the 2026-05-04 AMZN double-trim, and drift trims belong to the Portfolio Manager's rule-priority rows 4-5, not this seat. `config/prompts/position_reviewer.md` states the new scope and full trigger list. 2324 tests pass (2323 before).

**3.4 — Route exits through AI Risk. DONE (PR #108).**
`run_position_review` (`pipeline.py:6412`) called only `position_reviewer`, then executed — violating the `AGENTS.md` contract: `Specialists → Portfolio Manager → AI Risk → deterministic Python → broker`. `_risk_review_exits` now puts every SELL/REDUCE in front of the AI Risk Manager and drops what it rejects. The failure posture is deliberately asymmetric with entries: `RiskStage` fails CLOSED on an unparseable Risk Manager response, but the exit path fails OPEN — failing closed on an exit would leave a thesis-invalidated position unable to close because a language model is unavailable. The deterministic gates (named-trigger requirement, metric-contradiction veto, ATR noise band) are the real protection; every fail-open path logs at ERROR. See `docs/STATE.md` for the full rationale.

**3.5 — Upgrade the reviewer's model.**
`position_reviewer` runs on `google/gemini-2.5-flash-lite` — the weakest model in the stack — while the PM runs GPT-5.5. The consequential, loss-generating decision is on the cheap seat with no second opinion. Move it to a strong model; the cost is negligible against the losses.

> **Correction (2026-08-27, documentation-sync pass).** The "weakest model in the stack" claim above is not supported by the committed benchmark data and must not be read as fact. `ops/model_policy/results/merged.json`, at `position_reviewer`'s own measured scenario (`midday_exit`), scores `google/gemini-2.5-flash-lite` at `quality_min: 1.0` / `quality_mean: 1.0` — tied at the ceiling with four other candidates: `openai/gpt-5.5` (the PM's own model), `deepseek/deepseek-v4-pro-0813`, `qwen/qwen3.7-flash` and `qwen/qwen3-235b-a22b-2507`. It is not a low scorer at this scenario by any measured axis, and it scored 1.0/1.0 on every scenario it was measured on across the whole sweep (`tech_batch`, `macro_stress`, `news_intel`, `pm_constrained`, `risk_rr_breach`, `midday_exit`, `tech_batch_full`). This is the same error `DECISION_CHAIN_AUDIT.md` (F5) caught and corrected for `risk_manager`: reading the model's price, or its place in the routing policy's cost narrative, as a quality finding, when quality was never measured that way. `midday_exit` ties five of the twelve measured candidates at 1.0 and therefore does not discriminate between them on quality — a model could still be moved off this seat for other reasons (independence from the shared specialist model, the discretion this seat carries, or a scenario suite that plainly doesn't discriminate at the ceiling), but not for the reason stated above. §3.5 stays open and undecided; only its stated premise is corrected here.

**3.6 — ATR noise band on exits. DONE (PR #109).**
An adverse move inside ~1 × ATR of entry is noise, not thesis failure. OKLO was sold at 0.67 × ATR. An adverse move inside 1.0× ATR of entry can no longer trigger a price-derived exit; external-information triggers (news, earnings, regime shift, thesis invalidation) bypass the band. **Correlation breach was removed from this bypass list 2026-09-13** — a correlation is computed from the price series, so it is price-derived and never qualified as external information; see `docs/INCIDENT_HISTORY.md`.

> **Root cause found later, merged and deployed 2026-08-27 evening (`3dff940`, PR #113).** §3.6 gates the *exit* side of the noise band — it stops a stop-hit-adjacent price move from firing a discretionary SELL. It does not touch where the *stop itself* sits. Measured against the book: entry stops were sitting a median 4.3% below entry against a median ATR of 2.56% of price — about 1.7 ATRs, barely past §3.6's own 1.25 ATR "ordinary day's range" threshold, on positions meant to be held for ten sessions. That is the same failure §3.6 was built to catch, one layer earlier: a stop that tight gets tripped by ordinary volatility before §3.6's exit-side gate is ever relevant, because the *broker-resident hard stop* (§3.7) fires first and unconditionally — it has no noise band. It is also, independently, why Phase 2b's single-name clamp (below) was binding on nearly every conviction level: `notional = risk_pct x entry/(entry - stop)` makes a tight stop demand an oversized position for any real risk. `src/portfolio_constructor.py` now pushes an entry stop out to `risk.min_stop_atr_multiple` (base 3.0 ATRs, scaled by setup type and macro regime) when structure placed it inside that floor — never tightening a wide stop — and rejects the trade outright if the resulting reward:risk falls under `risk.min_reward_risk_after_widening` (1.5). Structure still decides the level. **This paragraph does not track live deploy status** — see `docs/STATE.md` → "Production position" for how to check what production is actually running.

**3.7 — Trailing stops, broker-resident. DONE (PR #109, `src/risk/trailing.py`).**
Trailing is arithmetic and belongs in deterministic code, not in an LLM's discretion:
- **Type A:** trail only after the target is exceeded.
- **Type B:** trail from entry — under each successive higher low (structural), with a chandelier stop as fallback where structure is unclear.
- Ratchet upward only, never down. Keep the existing ≥2% ratchet threshold and cooldown.

**3.8 — Exits the reviewer may still make on judgment.** The reviewer retains full authority to exit on **new information**: adverse news, earnings miss, macro regime shift, sector shock, thesis invalidation. Price movement alone is not new information.

> **Amended 2026-09-13.** This clause originally also listed *correlation breach*. It was removed from the accepted trigger vocabulary because nothing in the desk computes a correlation-breach event and no published operational definition (stated window, stated threshold) was found to build one from — so the phrase was an exit that could never fail a check. See `docs/INCIDENT_HISTORY.md`.

---

## Phase 4 — Evidence symmetry and feed repair

**4.1 — Unblindfold the intraday buy path. DONE (2026-08-27, commit `fb88e08`, branch `feat/pm-flex-routing`, merged and deployed as PR #113).**
`portfolio_manager.py` previously hard-returned a technical-only evidence registry whenever `session_type == "intra_check"`, even though the morning's macro was already loaded in memory. The 13:04 reviewer saw macro the 11:02 buyer was denied — which is precisely how OKLO was bought and killed within two hours on evidence that existed at purchase time. Measured over the 10 days to 2026-08-27, this session cost $0.222/run — essentially the same as a full `morning` run ($0.221), ~99% of it the PM call — while deciding on a fraction of the evidence: 33% of all LLM spend on blindfolded scanning.
- **Done, with the remedy narrowed from the original design.** Earnings stay excluded — an intraday filing genuinely has not been read this tick — but today's macro and news are now carried forward from the store (`TradingPipeline._carry_forward_macro` / `_carry_forward_news`) rather than passed by reference from memory. `_carry_forward_macro` refuses anything not dated today (`load_last_state` is not date-scoped, so that check is this method's job); `_carry_forward_news` re-validates the stored dump against the `NewsIntelligenceReport` schema. Both degrade to `None` on any failure — a carry-forward problem costs the tick its context, never the deterministic loss protection that already ran.
- `data_status` now reads `carried_from_morning` for macro/news instead of `not_run_intraday`, so RiskStage's existing 2+-degraded-sources advisory still fires honestly: a 09:30 regime call IS weaker evidence at 14:00, it is simply not NO evidence. `session_type` was removed from `build_evidence_registry` and `validate_grounding` entirely — rather than left inert — so no future reader assumes an intraday tick is still filtered somewhere.
- **Nothing is re-fetched.** The research stack still does not rerun this tick, which is the saving this scan exists to preserve; only what was already on disk is now shown to the PM.
- Fixed a latent crash this carry-forward surfaced: `sector_guidance` has two shapes — the live macro agent emits `[{sector, stance, reason}, ...]`, `MacroStore` persists the normalized `{sector: direction}` form — and the registry iterated the dict shape as a list, so `row.get` raised `AttributeError`. Carrying stored macro forward is what first put that shape in front of the registry; without the fix, every intraday tick would have degraded to no scan at all. Both shapes now resolve; the vocabulary asymmetry they expose (`overweight` vs `bullish` for one macro view) is pinned as a known property, not fixed here.

**4.2 — Repair the data feeds. DONE (2026-08-30, commit `92689e6`, branch `fix/fred-feed-repair`, merged and deployed as PR #162 — news half done earlier via PR #148/#157).**
Production logs show Reuters Business 404, AP Business 403, repeated FRED timeouts, 28 incomplete tech batches, and 11 `Portfolio decision failed deterministic grounding` errors (the PM inventing holdings).
- Replace or re-point the dead news feeds. **Done** — see `docs/AGENT_ROLE_AUDIT.md` §2.2 and `docs/phases.yaml`'s `phase_4` entry.
- Add retry/backoff for FRED. **Done, and rebuilt from what the spec's own text assumed already existed.** The single-retry/flat-2s-backoff policy live at the time this line was written was not enough — it let a three-minute outage on 2026-08-26 fail all nine series in one run. It is now config-driven retries, exponential backoff with jitter, per-request timeouts, and a real wall-clock ceiling (`total_fetch_deadline_s`, 90s default) the old policy never had.
- **Add a feed-health gate:** if macro or news coverage is unavailable, the desk must know it is operating degraded, and that fact must appear in the Telegram alert and the dashboard. **Done for both.** News coverage was wired into `data_status["news"]` earlier; macro coverage now feeds `data_status["macro"]` through the same ok/partial/failed path, and the macro analyst is shown its own coverage directly.
- **Not part of this item's original text, but a real gap surfaced by this work:** there was no macro event calendar (Fed decision / inflation release dates) — the desk answered "is one coming up" from the model's own memory. **Built 2026-08-31** in `src/data/event_calendar.py`, from FRED's free release-dates API: CPI, Employment Situation (NFP), PPI, PCE, GDP, retail sales and jobless claims, threaded into the macro analyst and the Risk Manager with a `MacroCoverage`-shaped coverage line and the same wall-clock-ceiling discipline as the FRED series feed. FOMC meeting dates followed on the same day: FRED cannot supply them (release 101 is a daily release carrying no meeting schedule), so they come from the Federal Reserve's own free calendar — `federalreserve.gov/json/calendar.json` as the structured primary, the rendered `fomccalendars.htm` page as a fallback used only when that feed fails or its schedule stops before the horizon. Disk-cached, with a stale cache served labelled rather than silently, and with "no meeting in this window" printable only when the published schedule spans the window.

---

## Phase 5 — Short selling

Owner-authorized. Currently **absent** from `src/execution/broker.py` — not disabled, not implemented. `allow_margin: false` is unrelated (it governs buying beyond cash).

Now a three-stage plan — see `docs/WORK.md` Phase 5 backlog entry for current
status (merge/deploy status is not tracked here — it changes daily).

- **No owner action outstanding.** The account was verified on 2026-08-28 as
  margin-enabled: `shorting_enabled: True`, `multiplier: 4`, equity
  $9,871.87. This corrects the line previously here asking the owner to
  switch the account to margin.
- Implement short entry/exit in the broker layer.
- Add borrow-availability and hard-to-borrow checks before submitting.
- **New risk math:** a short's loss is unbounded. Deterministic risk must treat shorts differently from longs — a stop is mandatory, gap risk is materially worse, and the position must be force-covered on breach.
- Teach the PM that the inverse ETFs already in the universe (`SH`, `SDS`, `PSQ`, `SQQQ`) are bearish instruments. Nothing in the codebase currently conveys this, so even the previously "sanctioned" bearish mechanism was never wired up.
- Short candidates flow through the same Specialists → PM → AI Risk → deterministic chain.

---

## Phase 6 — Cost circuit and transparency

**6.1 — Dollar-based budget with an afternoon reserve.**
`max_paid_sessions_per_mode_per_day: 2` tripped at 12:00 ET with **$0.59 of the $1.50 daily budget unused**, leaving four scheduled afternoon slots structurally dead.
- Cap on **dollars**, not session count.
- Reserve capacity for the afternoon so a quiet morning cannot consume the day.
- Suppress repeat suspension alerts — one notice, then silence until state changes.

*Note: prompt caching is deliberately disabled for a legitimate documented reason (it would make the circuit's pre-call reservations incomparable to actual spend) and would save essentially nothing here — one PM call per session, sessions 30 minutes apart, caches live ~5–10 minutes. Leave it off.*

**6.2 — Make the dashboard answer "why."**
The reasoning is already persisted; it is simply never shown.
- Add a `position_id` to `trades`, assigned when a BUY opens a flat position and inherited by every subsequent SELL / REDUCE / TRAIL_STOP against it until flat. Currently nothing links a buy to the sell that closed it.
- Add `GET /positions/{position_id}/history` returning the ordered chain: entry thesis, stop, each interim review decision, exit reason, realized P&L, hold duration.
- Render `reasoning` in the trades table (it is fetched and discarded today).
- Fix `PositionsPanel` so clicking a position opens its **entry** thesis, not today's re-evaluation.
- Add an `exit_reason_category` so exits are classifiable rather than free text.

**6.3 — Report the real number.** Telegram showed −$1.22 at 15:00 ET on 2026-08-26; the day actually closed **−$46.58**. The alert stream must carry a true end-of-day figure, and P&L should be expressed against risk capital as well as total equity.

---

## Phase 7 — Measurement

**Status note (2026-08-30):** §7.1 (backtester) and §7.3 (edge numbers) are DONE. §7.2 has two distinct halves: (a) the **logging** half (pin each trade's allocated risk, conviction, model at entry; label exit decision_id status) is DONE as of PR #159, and (b) the **analysis** half (grade whether stated conviction predicts outcomes) is NOT DONE and is data-constrained, not code-constrained — only 8 closed round trips exist in all live history, far too few for statistical claims.

**7.1 — Build a backtester.** `src/replay.py` replays past decisions; it is not a strategy backtest. Without one, every change is a guess evaluated against one noisy live day, and a bad change is indistinguishable from a bad week. 419 commits in 17 days were merged into that blindness. **DONE** — `src/backtest/engine.py` simulates the deterministic layer against real history, entering on next day's open, producing deterministic reproducible results.

**7.2 — Calibrate conviction.** Log each trade's allocated risk against its realized outcome. If the desk's conviction predicts results, conviction-weighted sizing amplifies the edge. If it does not, flat sizing is superior — and that must be discovered from data, not assumed. **Logging half DONE (PR #159)** — each trade carries conviction, requested_risk_pct, allocated_risk_pct, decision_model at entry; exits label whether they link to an originating decision. **Analysis half NOT DONE** — only 8 closed round trips exist in production history, insufficient for statistical work.

**7.3 — Track the edge directly.** Surface win rate, average win ÷ average loss, expectancy per trade, and average hold duration. The edge claim in §0 is a hypothesis until these numbers confirm it. **DONE** — `compute_trade_calibration` surfaces win rate, avg_win_loss_ratio, expectancy_pct, avg_hold_days for both long and short sides separately.

---

## Phase 8 — Documentation correction

- Rewrite `docs/OUTCOME.md` to the mandate in §0. The current "research experiment" framing is the root cause of the 99%-cash, 3%-starter-position behaviour — the system was built correctly for the wrong brief.
- Amend `docs/STATE.md`: remove the claim that the full chain applies to exits (it does not, until Phase 3.4), remove health claims contradicted by the failing feeds, and correct the "exactly one config delta" statement.
- Amend `AGENTS.md` with the Phase 0.2 ratification rule.
- Repo hygiene, low priority: delete the 83 merged-but-undeleted branches; triage the 26 orphans, rescuing the two abandoned VPS-security branches.

---

## Phase 9 — The research desk actually deliberates

**Status note (2026-08-31):** §9.1/§9.2 (any seat may nominate, Technical becomes a responder) are DONE and deployed (PR #153). §9.3 (disagreement adjudication) and §9.4 (sizing by agreement — a signed score since 2026-09-02) are NOW DONE and deployed (PR #160, merged during this audit window). §9.5 (conviction ledger per candidate) is **owner-ratified scheduled work, PARTIALLY BUILT** — its recording and scoring layer landed on `feat/conviction-ledger-recording`; its operator-facing view has not been built. See §9.5 below for exactly which parts exist.

*An earlier version of this note recorded §9.5 as "deliberately not built" with no attribution and no stated reason — an engineering scope decision written as though it were settled truth, the exact pattern AGENTS.md's Governance ratification section exists to prevent. The owner reopened it on 2026-08-31 and ratified the full specification below. Corrected here rather than carried forward.*

**Owner-directed, 2026-08-27.** Rex, verbatim: *"We have agents doing research
and analysis. If something has high conviction or strong candidacy it should
be debated amongst all the agents. We're trying to create a trading desk with
synergy, not a technical analysis trade bot."*

> Refined 2026-10-19 by the desk archetype (OUTCOME): specialists do not debate freely among themselves; disagreement goes to the head trader, who answers the strongest dissent in writing.

### What the system does today

Evidence from the 2026-08-27 morning run. Ten Portfolio Manager targets,
every one of them carrying `technical=buy/supports` as its **only** supporting
source. News contributed to exactly one target, as neutral context. Smart
Money contributed to none. Four of the ten were bought while
`macro=underweight/conflicts` — the conflict was recorded and then ignored.

The cause is structural, not a tuning problem. `validate_grounding`
(`src/agents/portfolio_manager.py`) rejects any increase where
`symbol not in {analysis.symbol for analysis in analyses}` — **a current-run
Technical analysis is a hard gate on candidacy.** The funnel is
`universe → prefilter → Technical → PM`, so News, Earnings, Macro and Smart
Money are all *filters applied to a list Technical already chose*. A name with
a blowout earnings beat and cluster insider buying cannot be bought unless
Technical happened to analyse and rate it. The other seats cannot bring
anything to the table; they can only nod at what is already on it.

That is a technical-analysis bot with commentary attached, which is precisely
what the owner does not want.

### 9.1 — Any seat may NOMINATE a candidate

Each research seat gains a bounded nomination output: symbol, conviction, and
the specific observation behind it. News nominates on a genuine catalyst;
Earnings on a filing that materially changes the picture; Smart Money on
clustered insider buying (this lane half-exists already and is capped at three
per run — generalise that pattern rather than inventing a new one); Macro
nominates sector leaders when a regime turns.

Cap nominations per seat per run. The cap is what keeps this bounded and
affordable, and the existing SEC Form 4 admission lane is the working
precedent for how to do it safely.

### 9.2 — Technical becomes a RESPONDER, not the gatekeeper

Invert the funnel. A nomination triggers an on-demand Technical analysis of
that symbol. Technical stays **mandatory** — Phase 1's contract is
non-negotiable, no trade without structural levels, a stop and a target — but
it stops being the thing that *originates* every idea. It answers "here is a
name the desk is interested in; is there a tradeable setup, and where are the
levels?"

This is the whole architectural change. Everything else follows from it.

Cost is bounded and modest: seats already do the analysis that produces a
nomination, so nomination itself is free — it is structured output from work
already paid for. Only the responding Technical call is new spend, and it is
capped by 9.1.

### 9.3 — Disagreement must be adjudicated, not merely logged

Today a conflicting macro stance is recorded in `provenance` as
`conflicts` and the trade proceeds unchanged. That is a record of a
disagreement, not a resolution of one.

Where a material conflict exists between seats, the PM must address that
specific conflict in `signal_conflicts` and name why it is overridden. A
target carrying a `conflicts` provenance entry with no corresponding
adjudication should fail grounding, the same way an unsourced claim does.

### 9.4 — Conviction is agreement, not a technical rating

> SUPERSEDED 2026-10-19 by the desk archetype (OUTCOME): views are weighed by each seat's measured record (equal to start, James-Stein shrinkage to the group mean), not counted; no net sign refuses a trade; the head trader decides and risk alone may veto; risk sizes from volatility and limits. The equal-vote tally below describes the code as it stood and is kept as history while the code is replaced.

The mandate is **breadth × consistency × asymmetry**. Sizing should follow the
number of *independent* sources that agree, not the strength of one seat's
rating. A name confirmed by Technical, Earnings and Smart Money is a different
bet from a name only Technical likes, and today they size identically at 3-5%.

This lands naturally on top of Phase 2b, which replaces notional conviction
sizing with risk allocation: multi-source agreement earns a larger share of
the risk budget.

**Freshness (built 2026-09-02).** "Independent sources agree" was implemented
as a headcount with no notion of *when* a source formed its view.
`build_evidence_registry` read only `investment_implications.sentiment` from an
earnings analysis and discarded `is_new` and `filing_date`; neither
`src/risk/rules.py` nor `src/portfolio_constructor.py` looked at age anywhere
in the sizing path. A cached bullish earnings stance therefore counted as a
full live corroborating source indefinitely, moving a name from one aligned
source to two and buying it a 3.0% → 4.0% risk allowance — a 33% larger
allowance on evidence that had confirmed nothing about today. This is reachable
rather than theoretical: when a symbol has no filing inside the provider's
45-day SEC scan window, `EarningsProvider._check_symbol` falls back to
`_get_existing_analysis`, which — until the cause fix below — had **no age
bound at all** and re-served whatever was on disk (the store prunes at 1000
days).

**Superseded 2026-10-16:** the 90-day age below was a made-up number and is
deleted. A stance now counts only while its filing is the company's latest
10-Q/10-K in the SEC submissions list fetched that session, and only until the
next report's latest legal filing date (period of report + one quarter, + 90
days + 15-day Rule 12b-25 extension for a 10-K, else + 45 + 5): see
`earnings_stance_is_stale` and `periodic_report_current_until`. History follows.

`PortfolioManagerAgent.stale_evidence_sources` now gates an earnings stance
out of the tally past `EARNINGS_STANCE_MAX_AGE_DAYS` (90). **90 is not a new
number**: `config/prompts/earnings_analyst.md` already caps the seat's own
conviction at `low` past 90 days and calls a filing past 180 days one that
"should not have reached you", and `_missed_ops_earnings_signal` already
refuses anything older than 90 days as recent earnings evidence. The seat and
the reflector had a threshold; only the sizing path did not read it.

Two properties are deliberate and tested:

- **Removal from the tally only.** The stance stays in the canonical registry,
  so `validate_grounding` still recognises the coverage and a PM that cites it
  does not fail the session. `validate_grounding` fails the WHOLE session on a
  non-empty error list, so deleting the stance would have converted a stale
  view from a sizing question into a hard block. This is a size reduction, and
  it can only ever lower a ceiling.
- **The PM is told.** The prompt already labelled a cached view `[from cache]`
  with its filing date and then counted it as live in the agreement block of
  the same message. The agreement line, the registry block and the earnings
  section now all say the same thing.

**Measured impact, `run-64290730` (2026-09-01), 65 symbols carrying an earnings
stance: zero.** No filing in that run is older than 48 days (median 28), so the
gate removes 0 sources and drops 0 ceiling rungs. Across all 28 runs in the
snapshot (2026-08-17 → 2026-09-01, 1643 analyses) nothing exceeds 48 days
either. The gate is a guard against the unbounded `_get_existing_analysis`
fallback, not a correction to today's book. For scale, the same measurement at
a 45-day threshold — the provider's own scan window — gates 3 stances and drops
2 symbols a rung (GE and NFLX, 4.0% → 3.0%). **Choosing a threshold below 90
would be inventing a number; 90 is the one the desk had already written down.**

**The cause fixed too (built 2026-09-02).** The freshness gate above stops a
stale earnings stance from EARNING SIZE; it deliberately does not remove the
stance from the evidence registry, because pulling something the PM has
already been shown out from under it fails `validate_grounding` for the WHOLE
session, not just that one symbol. That left the cause untouched:
`_get_existing_analysis` (`src/data/earnings.py`) still hands an over-age
analysis to a session as if it were current — it just no longer bought that
analysis extra size once there. `_get_existing_analysis` now refuses to
re-serve anything past `EARNINGS_STANCE_MAX_AGE_DAYS` (the same constant, not
a second number): past the bound it returns `None`, and the symbol looks
exactly like one with no earnings coverage yet at all — a state every
consumer already handles (a CIK miss, an unlisted name, a first SEC-covered
run). `stale_evidence_sources` is untouched and still recomputes staleness
generically for anything that does reach it, so a report arriving through a
different path is still caught there.

Measured against the local rehearsal data snapshot
(`/tmp/qamc-rehearsal-hqiel8o1/data/earnings`, 71 symbol directories,
2026-09-02): 63 symbols currently resolve to a servable analysis via this
fallback (8 have raw filing HTML but no completed analysis, and already
return `None` today, unaffected by this change). Age distribution across
those 63: median 29 days, max 49 days, min 6 days — **zero currently exceed
the 90-day bound**, so this fix changes no symbol's behavior today, the same
"guardrail, not a correction" shape the tally gate measured. Confirmed
independently, not just repeated: three symbols are already past the
provider's 45-day scan window and therefore already served purely by this
fallback — NKE (10-K, filed 2026-07-15, 49d), GE (10-Q, filed 2026-07-16,
48d), NFLX (10-Q, filed 2026-07-17, 47d) — one day older each than the
values recorded above, consistent with one day of elapsed time since that
measurement. What changes going forward: the day any symbol's most recent
on-disk analysis turns 91 days old with no newer filing, it now silently
drops out of that session's evidence entirely instead of being served as
current indefinitely (the store still only prunes the file itself at 1000
days).

**Dissent is counted, not priced (built 2026-09-02).** `count_aligned_sources`
counts only sources aligned with the trade direction, so on a long a bearish
earnings stance contributes 0 — arithmetically identical to neutral and to no
coverage at all. Nothing subtracts, and until now nothing recorded that it had
happened. `count_opposing_sources` is the exact mirror of the aligned count
through the same `stance_is_aligned` vocabulary; it is surfaced in the PM
prompt, logged per sized target, and written into the order note that reaches
the AI Risk Manager and the persisted `proposed_order` evidence. **It changes
no ceiling.** Making dissent subtract is a risk-rule change and needs an owner
decision; this exists so the frequency and the cost of overriding a dissenting
seat are measurable before that decision is taken. On `run-64290730`, of the 42
symbols carrying both a technical and an earnings stance, 4 were internally
split (CAT, GEV, PFE, SLB) — sized identically to a name with one aligned
source and no dissent.

**Dissent was counted, not priced (built 2026-09-02, superseded the same
day).** `count_aligned_sources` counted only sources aligned with the trade
direction, so on a long a bearish earnings stance contributed 0 —
arithmetically identical to neutral and to no coverage at all. Nothing
subtracted. `count_opposing_sources` made that visible: the exact mirror of the
aligned count through the same `stance_is_aligned` vocabulary, surfaced in the
PM prompt, logged per sized target, and written into the order note that
reaches the AI Risk Manager and the persisted `proposed_order` evidence. It
changed no ceiling. On `run-64290730` (2026-09-01), of the 42 symbols carrying
both a technical and an earnings stance, 4 were internally split (CAT, GEV,
PFE, SLB) — sized identically to a name with one aligned source and no dissent.

**Dissent is priced: the ceiling reads a SIGNED SUM (ratified 2026-09-02).**

```
s_i in {-1, 0, +1}   per seat: +1 aligned with the proposed direction,
                     -1 opposed, 0 neutral OR silent
S   = sum(s_i)       equal weight, unit magnitude
```

`signed_source_score` computes `S` as `aligned - opposed` (the difference of
the two counts, not a second traversal — one definition of "aligned", the
`stance_is_aligned` one), and `agreement_refuses_trade` turns `S` into one
yes/no refusal (until 2026-09-14 it indexed a graduated ceiling schedule —
retired, see below). **Doctrine:** counting only agreers has no support in
any published composite methodology. MSCI-style index construction and Grinold
& Kahn's `alpha = volatility x IC x score` both admit a disagreeing input as a
NEGATIVE number in a signed sum. Equal unit weighting is not a placeholder —
it is what index construction literally does, and what the Grinold & Kahn form
reduces to when per-source skill is equal.

Three properties, all falling out of the arithmetic rather than bolted on:

- **`S >= 1` imposes no size restriction of its own** (since 2026-09-14). The
  ratified per-trade envelope and the portfolio budget allocator are the only
  bounds. A dissenter no longer costs a rung, because there are no rungs.
- **A dissenter can still refuse the trade**, by pulling `S` to zero or below.
  That is the only thing dissent now does to size, and it is binary.
- **`S <= 0` produces no order at all.** There is deliberately NO standalone
  veto rule: the sign of the net IS the rule. A separate veto would charge the
  same dissenter twice. A blocked
  target leaves any existing position untouched — refusing to open is not a
  decision to sell, and a zero-weight plan would read to the delta loop as an
  instruction to liquidate.

**Measured impact** over the 28 sized PM targets carrying registry coverage in
the 12 most recent runs of the local snapshot (2026-08-28 → 2026-09-02):
6 targets change ceiling rung and 3 (10.7%) are newly blocked by `S <= 0`
(ONDS and NVDA on 2026-08-28 intraday, both `technical: buy` against
`macro: bearish`; UNH short on 2026-09-02, `technical: sell` against
`macro: bullish`). The ceiling BOUND on 0 of 28 targets before and 3 of 28
after — every rung change other than the blocks is a ceiling that still sits
above what the PM asked for, so the size does not actually move. On the most
recent full run (`run-bba4d4f3`, 2026-09-02) 1 of 9 targets is blocked. This is
nothing like the 2026-09-01 zero-trade day: that was a rule NO trade could
satisfy; this one leaves 25 of 28 untouched.

**Open, not built: conviction-weighted agreement.** Every seat emits a
conviction and the score ignores it entirely — a high-conviction bearish read
and a weak one are both `-1`. `SEAT_WEIGHT` (`src/risk/rules.py`) pins that at
unit magnitude and `tests/test_signed_dissent.py` fails if anyone introduces a
per-seat weight. The reason is the desk's own standing rule, recorded in
`src/conviction_ledger.py`: a confidence weight may only be DERIVED from
measured history, never chosen up front, and `_CONVICTION_OUTCOME_MIN_N`
(`src/storage/db.py`) sets the minimum at 20 resolved calls. The book has 7
closed equity round-trips and every one carries conviction NULL, so there is
nothing to derive from. That is precisely why the dissent change could ship
before this question is settled: with weights pinned at 1, the dissent rule has
no constant to inherit.

**The graduated ceiling is RETIRED — owner decision, 2026-09-14.**
`risk.agreement_ceiling_pct` and `derive_agreement_ceiling_schedule` are
DELETED; the settings key is rejected on load so a stale deployment cannot
resurrect it. What replaced it is nothing: a net score of 1 or more imposes
no size restriction beyond `max_position_risk_pct`.

Why. Earlier the same day the schedule had been made a DERIVED
`ceiling(n) = max_position_risk_pct x sqrt(n / 5 seats)` (retired items
30/57), on the published law that independent estimates combine as 1/N in
variance. That law's one precondition is INDEPENDENCE, and this desk's five
seats are not independent — they read overlapping evidence (the same tape,
the same bars, the same filings) and several are the same underlying model
behind different prompts. No honest correlation haircut is available to
tighten it with; inventing one would be the arbitrary number the derivation
was meant to remove. Secondarily, a graduated ceiling cannot distinguish
"the seats disagreed" from "the seats had nothing to look at" — a thinly
covered name and a contested one land on the same low rung. That is the same
defect already fixed in the rotation rule (retired board item 66).

Measured bite, before deletion: over the archived sized targets on the
2026-08-28..2026-09-02 snapshot, the graduated rungs capped **zero** targets.
Every rung sat above what the PM ever asked for (largest request 2.80% risk
against a rung-1 ceiling of 2.236% that only a net of exactly +1 would have
reached). Only the refusal ever bit — 1 target, UNH on 2026-09-02 at net 0.
The ladder was a rule that had never once changed a size.

**Agreement keeps its real job.** It ORDERS which candidates get funded
first, through `src/verdicts.py::rank_verdicts` and `allocate_risk_budget`'s
`priority` (retired item 49). **The weighting question above is unaffected:**
all five seats still count 1, and a per-seat sizing weight is still refused
for want of measured history.

**Correction, 2026-09-02.** The owner removed conviction weighting from the
ledger's credit on 2026-08-31 for two stated reasons (see §9.5 item 3a).
Reason 2 — "a confident call already earns a larger position through the §9.4
agreement ceiling" — **is factually wrong and has been corrected in place** in
`src/conviction_ledger.py` and `docs/architecture/MISSION_CONTROL_API.md`. §9.4
has never read a conviction: it collapses each seat to a polarity and nets at
unit weight, so weighting the ledger's credit would have charged confidence
once, not twice. Reason 1 (circularity) is sound and stands alone. **The
decision itself does not change** — no conviction weighting, now for one valid
reason plus insufficient sample.

### 9.5 — A conviction ledger per candidate

**Status (owner-ratified 2026-08-31): specified, scheduled work. PARTIALLY
BUILT — the recording and scoring data layer landed on
`feat/conviction-ledger-recording` (PR #196), and the read side (one read-only
API route plus the Mission Control panel) landed on
`feat/analyst-scorecard-panel`.** Items 2, 4, 5 and 7 remain unbuilt. Two
owner amendments are recorded inline on 2026-08-31: **item 3a** (credit is raw
signed R — the conviction weight in item 3's original wording was removed, and
a per-declared-confidence breakdown replaced it) and **item 3b** (shorts are
chained, scored, signed and worded identically to longs). The
per-item implementation status is item 11 at the end of this section, and item
10's "missing" list has been corrected to match. Everything between here and
item 10 is the specification as ratified, unchanged.

`docs/phases.yaml` had recorded this as "deliberately NOT built," with no
attribution and no stated reason — the exact pattern `AGENTS.md`'s
"Governance ratification" section exists to prevent: an engineering scope cut
recorded as though it were settled, with nothing to show it was ever a
decision rather than an omission. The owner reopened it on 2026-08-31. What
follows is the specification; nothing below is built yet.

Note the name collision before anything else: `src/storage/db.py` already
has a `_CONVICTION_OUTCOME_MIN_N` constant and a `trades.conviction` /
`requested_risk_pct` / `allocated_risk_pct` / `decision_model` column set,
both labelled "conviction ledger" in their own comments — but that is
**§7.2's** ledger, a single PM-assigned conviction value per *trade*, graded
against that trade's own outcome. **This section is a different thing**: a
per-*seat*, per-*idea* record, tracking every seat that took a position on a
symbol, not just the one conviction value the PM finally acted on. The two
should not be conflated; §9.5 does not replace or modify §7.2's ledger, and
item 8 below states exactly where the two touch.

**1. What is recorded.**

Per idea (symbol), per session, one row per seat that took a position on it:
which seat, its stance (long/short/pass) and declared conviction
(low/medium/high — `Nomination.conviction`'s existing scale), its stated
reason, whether it originated the idea or is responding to a seat that
already tabled it, and the session date.

The original wording for this section said the ledger tracks "what changed
since the previous session." That line is easy to read past, but it is the
point: this is **not a per-run snapshot**. A seat's stance on a symbol it
already has a position on can strengthen, soften, or reverse from one session
to the next, and that trajectory — not any single session's snapshot — is
what makes a seat's judgement legible. The ledger is longitudinal per idea by
design, not a table refreshed and discarded every morning.

**2. Two skills, scored separately.**

*Origination* — the seat that first tabled the symbol — and *judgement* —
any seat (including the originator, on a later session) taking a position on
an idea someone else already put on the table — are scored independently. A
seat can be a sharp originator and a weak judge of others' ideas, or the
reverse; collapsing the two into one number hides which skill is which.

**3. Scoring. (Amended by the owner, 2026-08-31 — see item 3a.)**

On position close, the realized outcome in R
(`src/risk/metrics.py::r_multiple`) is credited backwards to every seat that
took a stance on that idea: seats whose stance aligned with the direction
actually taken score **+R**, seats opposed score **−R**. A seat that argued
against a trade that went on to lose therefore gains from having been right
to dissent — the ledger scores the call, not the trade.

*The original wording of this item ended: "Each seat's score is weighted by
its own declared conviction on that call, so a loud wrong call costs more than
a quiet one, and a loud right call earns more than a quiet one." The owner
reversed that on 2026-08-31. Struck here rather than deleted, because item 3a
is only legible against what it replaced.*

**3a. No conviction weighting. Credit is raw signed R. (Owner decision,
2026-08-31 — reverses item 3's original last sentence.)**

Credit is `+R` for a supporter and `−R` for an opposer, and nothing multiplies
it. The declared conviction is still recorded on every credit row; it simply
scales nothing.

*Rationale (owner), both halves of which are recorded in
`src/conviction_ledger.py` so nobody reinstates the weight from this document
alone:*

1. **It is circular.** The ledger exists to discover whether an analyst's
   declared confidence predicts anything. Multiplying its credit by its own
   confidence assumes that answer and bakes it into the measurement. This desk
   has already observed high-conviction trades underperforming low-conviction
   ones at small sample (see `_CONVICTION_OUTCOME_MIN_N` in
   `src/storage/db.py` and §7.2); under weighting, that finding would have
   been hidden inside the score.
2. **It double-counts.** A confident call already earns a larger position
   through the §9.4 agreement ceiling (retired 2026-09-14), and a larger position already produces
   a proportionally larger R. Weighting the credit again charges confidence a
   second time for the same fact.

*What replaces it.* Each analyst's record is broken down **by the confidence
it declared**: resolved calls, calls right, average win, average loss and
cumulative total, separately for each level that analyst used
(`SeatRecord.by_confidence`, `AnalystScorecardItem.by_confidence`, and the
"When it said it was sure, and when it hedged" section of the panel). "Does
this analyst's high confidence earn more?" becomes something a reader can see
rather than something the code asserts.

*Reinstating a weight later.* A confidence weight could legitimately be
introduced, but **only one derived from an analyst's own measured history —
never one chosen up front**. Deriving it requires this breakdown to exist and
to have accumulated a real sample first, which is precisely why the breakdown
is the replacement rather than a smaller multiplier.

*Already-written rows.* Credit rows persisted before 2026-08-31 stored a
weighted `credit`. Nothing migrates them. Both read paths
(`Database.get_conviction_credits` and `db_reads.get_conviction_ledger`)
recompute `credit` from the stored `r_multiple` and `side`, which is exact
because `r_multiple` was always persisted unweighted — so old and new rows
mean the same thing and no series ever mixes two scales. The stored `credit`
and the historical `weight` key are ignored on read; `weight` is no longer
written, and is gone from `SeatCredit` and from the API response.

**3b. Shorts are scored identically to longs. (Owner decision, 2026-08-31.)**

A trade that made money is a **win** and a **positive** number; one that lost
money is a **loss** and a **negative** number — whichever direction it was
taken in. An analyst that argued for a profitable short is credited; one that
argued against it is charged. Exactly as for a long.

**Nothing is inverted, negated or specially-cased for direction anywhere a
human reads it.** Direction affects only how profit is computed from prices —
`src/risk/metrics.py::r_multiple` takes the side from a signed `qty`, so a
short arrives at the ledger already carrying the right sign — and never the
sign convention, the wording, the colour, or which side of zero a figure lands
on. The panel describes both directions in one sentence whose only difference
is the verb ("the desk bet it would rise" / "…fall").

*What actually had to change.* `_assign_position_ids` (§6.2a,
`src/storage/db.py`) opened a chain only on a `BUY`, so no `SHORT` ever
received a `position_id` and no short round trip existed for this ledger to
score. A `SHORT` now mints a chain and the `COVER` family
(`COVER`/`PARTIAL_COVER*`/`EMERGENCY_COVER`) retires it, mirroring
`BUY`/`SELL`; stops, trails, take-profits and de-levers retire either side. A
long-side exit against an open short chain (or the reverse) is left
unattached rather than allowed to close the wrong position, and an entry on
the opposite side of an already-open chain is passed through untouched — both
are malformed histories this desk cannot produce, and neither is guessed at.

**This changed no trading behaviour.** `position_id` is a forensic column: no
module outside `src/storage/db.py` and the read-only `src/api/` package names
it, and nothing in the decision chain reads it, the ledger, or any credit row.
`tests/test_conviction_ledger.py::test_short_chaining_touches_no_trading_decision`
runs `DecisionStage` against a database that does and does not already hold a
scored short round trip and requires byte-identical constructed orders.

**4. Unconverted nominations are not scored, but tracked.**

An idea that never became a trade has no outcome and gets no verdict — there
is nothing in R to credit. It is not silently dropped, though: conversion
rate (nominations that became a trade ÷ nominations made) is tracked and
shown per seat, alongside the scored record. A seat that nominates constantly
but rarely survives to a trade is a different seat from one that nominates
rarely but almost always converts, and the ledger should show that
difference even though neither number is a win/loss score.

**5. Shadowed objections. (Owner decision, 2026-08-31.)**

When a seat's objection blocks a trade outright, there is no real market
outcome to grade the objection against. The trade is *shadowed* instead:
tracked as if it had been taken, at the price and structural levels the
desk would have used, so the blocking seat's objection is scored against
what actually happened to the position it prevented.

*Rationale (owner):* without this, the cheapest way for any seat to look
good on this ledger is to block everything. A seat that only ever votes no
would otherwise be unscoreable — never wrong, because never in a trade — and
that is a worse incentive than the one the ledger is trying to fix.

**6. Advisory only. (Owner decision, 2026-08-31.)**

No score produced by this ledger feeds back into position sizing, in this
build, under any circumstance.

*Rationale (owner):* a leaderboard that reallocates risk starts grading
itself on trades it caused. A seat upweighted because it scored well starts
influencing the very trades that determine whether it keeps scoring well —
the measurement poisons itself the moment it has power over the thing it is
measuring. This ledger stays read-only until that circularity has a real
answer, which is not attempted here.

**7. Decay. (Owner decision, 2026-08-31.)**

Scores are computed over a rolling window, not a lifetime record, so a seat
is judged on the regime it is trading now rather than carrying a call from
eight months ago at equal weight to one from last week.

*Tension to flag explicitly, per the owner:* decay too aggressively and no
seat ever accumulates enough resolved calls inside the window to be
statistically readable — the same small-n problem §7.2 already hit at 8
closed round trips (see the `_CONVICTION_OUTCOME_MIN_N` comment in
`src/storage/db.py`). The window length is not fixed by this spec; whoever
implements this has to pick a value that survives that tension, not assume
one exists by default.

**8. No arbitrary sample gate. (Owner decision, 2026-08-31 — reverses earlier
design.)**

An earlier version of this plan gated any displayed score behind 20 resolved
calls per seat — the threshold `src/storage/db.py::_CONVICTION_OUTCOME_MIN_N`
already uses to gate §7.2's `by_conviction` / `by_allocated_risk` outcome
buckets. The owner rejected the gate as arbitrary: a reader can tell the
difference between "3 of 3" and "25 of 30" without being told which one is
allowed to be shown. **Show the raw counts (n resolved, n scored) alongside
every figure; never hide a score behind a threshold.**

This is a deliberate, narrow reversal, and it does not touch
`_CONVICTION_OUTCOME_MIN_N` itself. That constant keeps its existing job
exactly as-is: gating whether §7.2's `compute_trade_calibration` states a
win rate / average return for a *conviction bucket across the whole book* (a
claim strong enough to justify changing how much risk the desk allocates, if
it were ever wired to sizing — which per item 6 above, this ledger's
per-seat score will not be). The two ledgers are answering different
questions at different resolutions, and only one of them is gated. Anyone
extending §7.2's aggregate claims should keep using the floor there; this
section's per-seat, raw-count display is the only thing exempted.

**9. Presentation — a recommendation, not a decision.**

Five encodings were prototyped for showing one seat's record at a glance and
rejected. Recording why, so nobody re-discovers the same dead ends:

- **(a) A diverging bar-per-call strip** (one bar per resolved call, signed
  by outcome) — shows the sequence of calls but hides the total at a glance.
- **(b) A stacked distribution / dot plot** — shows the spread of outcomes
  but hides which order they happened in.
- **(c) A hard-edged confidence-interval bar** — Correll & Gleicher (IEEE
  TVCG 2014,
  https://graphics.cs.wisc.edu/Papers/2014/CG14/Preprint.pdf) show that a
  hard interval edge causes readers to systematically over-trust values
  just inside the bar ("within-the-bar" overconfidence) — the wrong failure
  mode for a metric meant to be read skeptically.
- **(d) A variable-width "tug of war" bar** (width/area encoding conviction
  or magnitude) — width and area are weak perceptual channels next to
  position and length (Cleveland & McGill's ranking of perceptual
  accuracy), which is why differences in block size did not actually read
  to a viewer.
- **(e) Any single mark trying to combine frequency and magnitude into one
  number** — collapsing "how often" and "by how much" loses exactly the
  distinction item 2 above depends on (origination vs. judgement are
  different skills; a single score cannot show both were measured
  separately).

**Recommendation:** a **bullet graph** (Stephen Few — designed for exactly
this: a compact, dashboard-row-sized metric) carrying magnitude as
**length from a baseline** (cumulative R, or average R per call), paired
with a **separate small glyph** for hit rate (calls scored positive ÷ calls
scored) rather than folding the two together.

*Justification:* this is a convergent convention, not a personal
preference. Trading journals separate win% from average-win-vs-average-loss.
Sports tipster tables separate strike rate from ROI. Forecasting scorers
(Brier score) separate calibration from sharpness. Three independent
domains that all face this exact "frequency vs. magnitude" problem
independently refuse to collapse it into one number — that convergence is
the strongest evidence available that two channels, not one, is correct.

*Binding accessibility constraint:* meaning must never rest on hue alone
anywhere in this presentation — encode sign and category with position,
length, shape, or an explicit +/− glyph, and use color only as
reinforcement on top of one of those, never as the sole carrier.

This section is a recommendation for whoever builds the Mission Control view,
not a locked decision — unlike items 5–8, it carries no owner ratification,
because no design has been shown to the owner yet.

**10. What already exists versus what must be built.**

Verified by reading the current code, not assumed:

*Already exists:*

- **Nomination capture.** Every seat's raw nomination (symbol, conviction,
  observation) is already captured, per seat, before capping/dedup
  (`src/nominations.py::_collect_seat_nominations` is called from
  `src/pipeline_stages.py::_run_nomination_responder_pass`), and is already
  **persisted** — one `specialist_evidence` row per nomination, kind
  `pipeline_event`, `outcome="nominated"`, carrying `seat`, `conviction` and
  `observation` in its JSON payload, scoped to that symbol
  (`src/pipeline_stages.py::_record_pipeline_event` /
  `Database.insert_specialist_evidence`). It is even already surfaced
  read-only through Mission Control's research/evidence API
  (`src/api/routes_evidence.py`, `src/api/routes_research.py`) as a per-run
  event timeline. **This corrects the premise that nominator identity is
  computed and then discarded — it reaches `src/storage/db.py` today.** What
  it is NOT, today, is a ledger: it is one forensic log line per event, per
  run, with no aggregation, no cross-session view, and (see below) no link
  to what the trade did.
- **Per-seat, per-symbol stance for a session.** Each seat's own structured
  analysis for a symbol is independently persisted
  (`specialist_evidence`, kind `analysis`) and is the exact registry
  `src/risk/rules.py::signed_source_score` reads to compute how many seats
  net out in favour. This is effectively the raw material for "who confirmed, who
  dissented" — but nothing marks an analysis row as a response *to a
  specific nomination*, so that relationship has to be reconstructed, not
  read.
- **Seat alignment counting.** `count_aligned_sources`,
  `count_opposing_sources`, `signed_source_score` and
  `agreement_refuses_trade` (`src/risk/rules.py`) already turn per-seat
  stances into a deterministic signed score and a sizing ceiling (§9.4,
  shipped in PR #160; signed 2026-09-02).
- **R computation.** `src/risk/metrics.py::r_multiple` already turns
  current price, entry and initial stop into a signed R-multiple for either
  side (long or short), which is exactly the scoring unit item 3 needs.
- **Per-trade risk fields.** The `trades` table already carries `conviction`,
  `requested_risk_pct`, `allocated_risk_pct` and `decision_model` pinned at
  entry, and exits are already labelled `linked` /
  `no_originating_decision` against their originating decision
  (`src/storage/db.py`, §7.2). That is the single-conviction-per-trade
  ledger, distinct from this one (see the note above item 1), but its
  `decision_id` linking pattern is the precedent to reuse.

*Missing — has to be built:*

*(Corrected against the tree after `feat/conviction-ledger-recording`. The
first two bullets below were written before that branch and are now done;
they are kept, struck through in prose, because the diagnosis they record is
what the fix was built from.)*

- ~~**Persistence of nominator/dissenter identity as a structured, joinable
  record**~~ — **DONE.** The diagnosis stands and was verified against the
  code: nomination `pipeline_event` rows were written with
  `decision_id=None` because `RunContext.decision_id`
  (`src/pipeline_context.py`) is not assigned until `DecisionStage`
  (`src/pipeline_stages.py`), which runs *after*
  `_run_nomination_responder_pass` has already written those rows.
  `Database.link_nominations_to_decision` now back-fills the id onto them
  once `DecisionStage` mints it, and `Database.record_seat_stances` writes
  one `seat_stance` row per (idea, seat) — dissent as well as support.
- ~~**The join from ledger to trade outcome.**~~ — **DONE.**
  `Database.resolve_conviction_ledger` walks each closed position chain to
  its realized R (`r_multiple`) and writes a scored verdict back per seat.
- **The longitudinal per-idea view.** Still missing. Everything persisted is
  scoped to one run/decision. Nothing aggregates a symbol's
  nomination/stance history *across* sessions, which is what item 1's "what
  changed since the previous session" requires.
- **Origination vs. judgement scoring** (item 2), **the conversion-rate
  metric** (item 4), **the shadowed-objection tracking** (item 5), **the
  decay window** (item 7), and **the presentation layer** (item 9). Still
  missing. One qualification on item 2: whether a seat originated an idea or
  responded to one is now recorded on every stance and credit row
  (`nominated`), so the split is derivable — but nothing computes the two
  scores separately, and `aggregate_seat_records` returns one record per
  seat.

**11. Implementation status, per item (as of `feat/conviction-ledger-recording`).**

The scope of that branch was deliberately the data layer only: no UI, no API
route, no frontend, and no change to sizing or to what gets traded.

| Item | Status | Where |
|---|---|---|
| 1 — what is recorded | **Partial** — seat, stance, declared conviction, originated-vs-responding, stated reason and session date are recorded per idea per decision. The **longitudinal** half (cross-session trajectory) is not built. | `Database.record_seat_stances`, `seat_stance` evidence rows |
| 2 — origination vs judgement scored separately | **Not built.** The `nominated` flag is persisted on every stance and credit row so the two can be separated later; nothing scores them apart today. | — |
| 3 — scoring on close | **Built.** +R aligned / −R opposed via the existing `r_multiple` against the stop the position was OPENED with. Idempotent; a chain with no entry stop is counted and skipped, never scored against a fabricated denominator. | `Database.resolve_conviction_ledger`, `src/conviction_ledger.py::score_position` |
| 3a — no conviction weighting | **Built (2026-08-31).** Credit is raw signed R; `conviction_weight()` and `CONVICTION_WEIGHT` are gone and `SeatCredit.weight` with them. Pre-2026-08-31 weighted rows are read back unweighted, recomputed from the stored `r_multiple` and `side` rather than migrated. | `src/conviction_ledger.py`, `Database.get_conviction_credits`, `src/api/db_reads.py::get_conviction_ledger` |
| 3a — per-confidence breakdown | **Built (2026-08-31).** Each analyst's resolved calls, calls right, average win, average loss and cumulative total, split by the confidence it declared. Sums back to its own totals; only levels it actually used appear. | `SeatRecord.by_confidence`, `AnalystScorecardItem.by_confidence`, `AnalystDetail.tsx` |
| 3b — shorts scored like longs | **Built (2026-08-31).** A SHORT mints a position chain and the COVER family retires it, mirroring BUY/SELL. A profitable short is a positive number and a win for its backers; nothing is inverted for direction anywhere a human reads it. | `_assign_position_ids` / `_exit_action_side` (`src/storage/db.py`) |
| 4 — unconverted nominations tracked, conversion rate | **Not built.** The join now makes conversion rate computable (a nomination row either does or does not have a `trades` row on its `decision_id` + symbol); no function computes it. | — |
| 5 — shadowed objections | **Not built.** | — |
| 6 — advisory only | **Built and enforced.** Nothing in the trading chain reads any ledger row; every write is best-effort. `tests/test_conviction_ledger.py::test_ledger_recording_does_not_change_a_single_trading_decision` runs `DecisionStage` with recording live and disabled and requires the constructed orders to serialize byte-identically. | `src/pipeline_stages.py::_link_nominations_to_decision` / `_record_seat_stances` |
| 7 — decay / rolling window | **Not built.** The record is lifetime. | — |
| 8 — no arbitrary sample gate | **Built.** `aggregate_seat_records` returns raw `resolved_calls` and `calls_right` with no threshold anywhere, and so does every per-confidence row. | `src/conviction_ledger.py` |
| 9 — presentation | **Built**, as a Mission Control view rather than the bullet graph item 9 recommended — see item 12. Raw counts always accompany any percentage, and no meaning rests on hue alone. | `GET /analysts/scorecard`, `frontend/src/components/scorecard/` |
| 10 — the join | **Built.** See the corrected list above. | `Database.link_nominations_to_decision` |

Additionally built, as the read side items 2/4/9 will need: a per-analyst
aggregate — `src/conviction_ledger.py::aggregate_seat_records` returns, per
seat, resolved calls, calls right, average win, average loss, the cumulative
profit series over time, and current drawdown from that seat's own peak. Pure
logic, no I/O, same posture as `src/nominations.py`.

**12. The read side (`feat/analyst-scorecard-panel`).**

One read-only endpoint and one Mission Control view. No write path, no change
to sizing, no change to what gets traded — item 6 holds unchanged.

- **`GET /analysts/scorecard`** (`src/api/routes_scorecard.py`,
  `AnalystScorecardResponse` in `src/api/schemas.py`). Reads the persisted
  `conviction_credit` and `seat_stance` evidence rows through
  `db_reads.get_conviction_ledger()` — an independent SQLite `mode=ro`
  connection, like every other Mission Control read. It **scores nothing**:
  no `r_multiple` is computed and no stance is re-classified as supporting or
  opposing. It sums into per-analyst totals, a running series, a peak, a
  distance below that peak, monthly buckets and a per-declared-confidence
  split. `credit` is the one value it derives — `±r_multiple` by `side` —
  which is what lets a pre-2026-08-31 weighted row read back on the same
  scale as a new one without a migration (item 3a).
- **It does not import `src.conviction_ledger`**, even though
  `aggregate_seat_records` produces the same per-analyst totals, because that
  module imports `src.risk.rules` and `tests/test_api_safety.py` forbids any
  `src.risk` import from `src/api/`. The aggregation is therefore mirrored in
  the route, and `tests/test_api_scorecard.py::test_projection_matches_the_ledgers_own_aggregate_when_it_is_available`
  compares the two implementations against identical input so they cannot
  drift apart. This duplication is a deliberate cost of the isolation
  contract, not an oversight.
- **The panel** (`frontend/src/components/scorecard/`, reachable from the
  cockpit's top nav as "Analyst Scorecard"). Four sections: two slope panels
  comparing every analyst at the earliest and latest month on accuracy and on
  money; a ranked table; one analyst opened (running-profit chart, a strip
  showing how far below its own best it has fallen and for how long, its
  record split by how confidently it spoke, and a month-by-month waterfall);
  and one closed idea traced back to everyone who took a side on it. Drawn with `lightweight-charts`, `recharts` used
  directly, plain SVG with `d3-scale`/`d3-shape`, and `@xyflow/react` — no new
  dependency, and deliberately not Tremor.
- **Item 9's bullet-graph recommendation was not followed**, and the
  recommendation itself said it was not a ratified decision. What it was
  protecting — never collapsing "how often right" and "by how much" into one
  mark — is preserved: the two are separate panels in section 1 and separate
  columns in the table. The binding accessibility constraint is met by
  position against a drawn zero line, explicit + / − signs, ▲ / ▼ glyphs and
  solid-versus-outlined shapes; colour only ever repeats one of those.
- **Plain language is enforced by test.** The panel defines every term on the
  page before using it, states the $100-at-risk convention, that nothing
  compounds and nothing expires, that how confidently an analyst spoke does
  not change what a call is worth, that a bet on a share falling counts
  exactly the same way as any other trade, that no score changes how much
  money any trade gets, and that no minimum call count hides anything.
  `frontend/src/components/scorecard/AnalystScorecard.test.tsx` asserts that
  "R-multiple", "seat", "payoff ratio", "expectancy", "drawdown" and
  "conviction-weighted" appear nowhere in what renders.
- **Example data, clearly labelled.** Until a position closes and is scored
  the ledger is empty, so the panel renders a committed fixture
  (`frontend/src/fixtures/analystScorecard.ts`) behind a permanent
  "Example data — not real" banner, and switches to the live record the moment
  the endpoint returns scored calls. The fixture is never merged into a live
  response, and the API never fabricates a row.

**That gap is now closed (2026-08-31).** Short round trips were previously
unscored because they carried no `position_id` to chain on —
`_assign_position_ids` (§6.2a) opened a chain only on a `BUY`. A `SHORT` now
mints a chain and the `COVER` family retires it, and the ledger's arithmetic
scored them unchanged the moment they had chains, exactly as this note
predicted. See item 3b above for the sign convention, which is the part that
mattered: a short is a win or a loss on the same side of zero as a long.

### Sequencing

Phase 9 depends on Phase 2b, because "agreement earns size" is meaningless
until size is expressed as risk. Build 2b first.

---

## Phase 10 — One voice must never veto the book (owner-ratified 2026-09-01)

**Status: ratified by Rex on 2026-09-01, not yet implemented.** Four decisions,
all made by the owner in conversation, all pointing at the same defect: the
pipeline repeatedly turns a judgement that should ADJUST a trade into a binary
that BLOCKS it.

**The evidence that produced them.** Morning run `run-64290730`, 2026-09-01.
59 symbols analysed, 38 actionable, **zero trades**. Reconstructed from the
run funnel:

- **30 of 38 actionable signals (79%) scored below the 1.5 R/R floor** and were
  untradeable before any human judgement applied — including the two
  highest-conviction calls of the day, SLB `strong_buy/high` at 1.28 and AGX
  `sell/high` at 0.84.
- Of the 8 that cleared the floor, **5 were shorts** (NKE 2.28, GEV 2.12, UNH
  1.90, NEE 1.84, FLNC 1.84). The PM proposed **none** of them and put up two
  longs instead. Macro had called the day `risk-on / bullish / high`. The market
  closed the session down. 15 bearish candidates reached
  `technical_analysis_validated` and every one died at
  `candidate_not_selected_for_target`.
- Risk rejected the whole plan naming **XLE only** (constructed R/R 1.18).
  **CHPX went down with it at R/R 3.03**, comfortably inside the floor, on an
  unrelated technical thesis in a different sector.

### 10.1 The risk verdict becomes per-trade, judged against the live portfolio

`RiskVerdict.approved` is a single `bool` for the entire plan.
`RiskModification` can retune a symbol's fields but cannot reject one symbol.
So one failing leg kills every other leg in the same run.

Change the verdict to carry a per-symbol outcome. A failing trade dies alone.

**Owner's framing, which is sharper than the original and governs the design:**
*the batch is arbitrary — it is whatever happened to be proposed in one run.
Judging a trade against its accidental co-passengers makes no sense. Judge it
against what the account actually holds.* Portfolio-level risk (correlation,
concentration, total exposure) is a property of the **live book**, not of the
run.

**Not acceptable as a fix:** detecting the rejection and re-running the plan
without the failing leg. That asks the wrong question twice instead of the
right question once, and burns a second paid session to hide a schema defect.

**Note for whoever implements this.** `docs/architecture/DECISION_CHAIN_AUDIT.md`
records F5's veto hierarchy as *intentionally retained* after external review on
2026-08-14. This is not a careless gate — it is a reviewed decision now being
revised on evidence that did not exist then. Read that audit before changing the
hierarchy, and record the supersession there.

**IMPLEMENTED 2026-09-01** (branch `fix/risk-verdict-per-trade`). `RiskVerdict`
gains `rejected_symbols` — a list of `{symbol, reason}`. The verdict now has
four levers, narrowest first:

| lever | scope | fires when |
|---|---|---|
| `modifications` | one symbol's fields | the trade is sound, sized or stopped wrong |
| `rejected_symbols` | one symbol, refused | *that name* fails |
| `scale_all_buys` | ADVISORY only (owner ruling 2026-09-25) | the entry side is too big for the regime — RECORDED with a reason, but resizes/drops nothing; the hard aggregate limits are the cap |
| `approved: false` | the whole plan | the BOOK is what fails |

Book-level risk still refuses broadly and is evaluated FIRST — correlation
clusters, total exposure and drawdown state are properties of the account, not
of one name. **No threshold moved**; this is the granularity of refusal only.

**Fail-closed asymmetry.** A malformed `modification` is dropped. A malformed
`rejection` is NOT — dropping it would let a symbol the risk manager explicitly
refused go on to trade. Anything naming no symbol fails the whole verdict
closed, and a repair reprompt that adds, drops or rewrites a rejection is
treated as an unauthorized re-decision.

The supersession note required above now lives under F5 in
`docs/architecture/DECISION_CHAIN_AUDIT.md`.

### 10.2 Macro sets exposure. Macro does not select trades.

`config/prompts/portfolio_manager.md` orders the reasoning chain
`macro_filter · news_check · earnings_check · signal_conflicts`. Macro is first
and is named *filter*. For an LLM reading top-to-bottom that is an instruction
to eliminate before considering, and on 2026-09-01 it did exactly that.

- Technical, news, earnings and smart-money **select** what to trade.
- Macro **sizes**: it sets total and net exposure, nothing else. (Aligned 2026-10-19 with the desk archetype: macro is the backdrop that tilts size and direction and rarely blocks a name.)

A bullish macro call must not be able to suppress a qualified short. Under this
split there is no conflict to resolve: a bullish macro keeps the book net long
while an individual short lives inside that exposure. Macro was never asked
about the single name.

**Owner's correction, accepted:** *if the weighting is real, position in the
prompt is irrelevant.* Ordering only matters today because there is no
weighting — the PM is a language model reading prose. Compute the weighting
deterministically in Python and hand the PM a ranked, scored list; then no
analyst can dominate by position. This is the same principle as the
reward:risk fix below and as PR #202: **the number comes from code, the agent
brings judgement.**

Macro remains authoritative for its own question — index and sector direction.
It has little to say about a single name falling for months on company-specific
news, and must not be allowed to answer that question by default.

### 10.3 Concentration scales size. It does not veto.

Sector crowding currently blocks. It should reduce position size. A
high-conviction opportunity in an already-heavy sector should still be taken,
smaller. Same defect as 10.2: a dial wired as a gate.

### 10.4 The reward:risk gate is replaced, not relaxed

**Do NOT simply lower the 1.5 floor.** The floor is not the defect; its inputs
are.

> **RESOLVED, 2026-09-11 — docs/WORK.md item 1 part (d), owner decision.**
> The gate was replaced rather than relaxed, exactly as this section
> required, and by SETUP TYPE because the two archetypes this desk already
> distinguishes (`src/risk/trailing.py`) need different answers:
>
> * **Type B / trend (breakout).** No reward:risk comparison applies at any
>   stage — PM eligibility, the PM sub-floor gate, the constructor, the
>   execution belt, or the Risk Manager's prompt. There is no overhead level
>   to measure a reward against, and the position is exited by a trailing
>   stop with no fixed target, so every ratio computed for one divided by an
>   invented number. Approval rests on the risk side and on the multi-agent
>   conviction/evidence checks.
> * **Type A / range.** The real ratio — this trade's own support against its
>   own resistance — is still computed and now feeds the ratified weighted
>   ranking (§13.3, `src/verdicts.py::rank_verdicts`) as an ordering signal
>   rather than a cutoff. A sub-floor range target is still capped at the
>   smallest starter size; it is no longer refused.
>
> No new fixed number was introduced. `src/risk/constants.py::is_trend_trade`
> is the single definition of "no ceiling", shared with
> `derive_structural_target`'s measured-move projection (funnel item 6) so
> the two cannot drift apart.

The stop is already derived in code from measured volatility
(`min_stop_atr_multiple: 3.0`). The **target is the model's guess**.

> **CORRECTION, 2026-09-04.** "Derived from measured volatility" was true of
> the ATR unit and false of the multiple. The 3.0 was never derived from
> anything; it has since been re-derived to **1.5** from a Maximum Adverse
> Excursion study on this desk's real signals, and the setup scalers were
> found to be inverted and corrected (breakout x1.00, range x0.90). The
> paragraph's actual argument — that the floor is not the defect, its inputs
> are — held up: the target input was fixed by §10.4, and the stop input was
> the other half. See `docs/INCIDENT_HISTORY.md`, 2026-09-04. R/R is then
`(target − entry) / (entry − stop)` — arithmetic performed on an opinion. A
correctly-sized wide stop plus a modestly-guessed target rejects automatically,
which is precisely how a `strong_buy/high` breakout (SLB, 7.7% stop) became
untradeable.

**Derive the target in code**, from the structural levels the system already
computes from price history (see Phase 1, `feat/tech-analyst-structural-levels`)
and the distance the symbol actually travels over the intended holding period.
Then the ratio measures something real and the floor means what its name says.

**Rejected alternative, and why.** An expected-value gate (win-rate × payoff)
is the theoretically better answer and was proposed first. The owner rejected
it for now: it requires a body of closed trades this account does not yet have,
and it replaces one unmeasured number with another. Revisit under Phase 7 once
there is real outcome history.

**IMPLEMENTED 2026-09-01** (branch `fix/target-from-structure`), as
`src/data/levels.py::derive_structural_target`. Find the nearest computed
structural level in the trade's direction, beyond a `min_target_atr_multiple`
noise floor, then:

| situation | target |
|---|---|
| level exists, within reach | **the level** — price must clear the first ceiling before any further one |
| level exists, beyond reach | **measured move** — nothing structural bounds the trade |
| no level, `setup_type == "breakout"` | **measured move** — absence of overhead structure is what "breakout" asserts |
| no level, any other setup | **REFUSE** — chart and analyst's read disagree |
| no levels / no ATR / no horizon | **REFUSE**, by name |

Reach and the measured move share one estimate of travel:
`ATR x sqrt(sessions) x multiple`, from the analyst's own
`expected_horizon_sessions`. Square-root, not linear — daily ranges accumulate
as a random walk, so `ATR x N` overstates an N-session excursion by ~`sqrt(N)`.
Fully symmetric for shorts; a short projection running through zero is refused.
Six named refusal codes — "no trade" without a reason is what let the original
defect survive. The model's `reference_target` is retained as EVIDENCE, never
as arithmetic, and the computed-vs-guessed gap is logged.

**The interaction with `min_stop_atr_multiple` is arithmetic and binding.** A
stop `k` ATRs out and a target `p x ATR x sqrt(H)` clear a floor `f` only when
`sqrt(H) >= f*k/p`. At the settings shipped on that branch (k=3.45 for a range
setup, p=1.0, f=1.5) a measured-move trade needs a stated horizon of **~27
sessions** and a structural-level trade **~12**. **This is precisely why 12.1
is required and not optional**: honouring a level-backed stop collapses `k`
from 3.45 to the measured median 1.7, and the required horizon falls with it.
**Superseded in part, 2026-09-04:** `k` is no longer 3.45 for a range setup
but 1.35 (base 1.5 x range 0.90), so the required horizons above fall to ~5
and ~2 sessions. The reasoning stands; the numbers it produced were driven by
a `min_stop_atr_multiple` that has since been re-derived. §12.1 remains
correct and useful, but it is no longer load-bearing for whether a range
trade can clear the floor at all.

10.4 and 12.1 are one fix in two halves — shipping 10.4 alone leaves most
trades refused on horizon geometry.

**Unverified, and must not be quoted as if measured:** how many of the 38
signals from 2026-09-01 would now pass. The run's per-symbol levels, ATRs and
horizons are not available offline. The SLB reproduction in
`tests/test_target_derivation.py::TestSLB` uses **synthetic** level data.

**Pre-existing defect fixed alongside.** `_widen_stop_past_noise` moved a stop
to `entry +/- multiple x ATR`, which is unconditionally on the correct side of
entry — so a short handed a stop BELOW its entry came out with a valid-looking
stop above it, silently repairing the nonsense the caller's side-check exists
to catch. It now refuses. The existing test passed only because its fixture
carried no ATR, which is not a state production reaches.

#### 10.4a The floor's catalyst exception was assertable. It is now checked.

**IMPLEMENTED 2026-09-02** (branch `wt/catalyst-loophole`), in
`PortfolioManagerAgent._apply_subfloor_catalyst_rule`.

The floor has always carried an escape hatch: a below-floor pick is allowed
if it names a catalyst. Nothing checked the name. Benchmarked 2026-09-01 on
the real opportunity set of the zero-trade day (`run-64290730`), both
candidate models picked NVDA at R/R 1.03 in 9 of 9 runs and passed over GEV,
which cleared the floor — **without disobeying anything**. Each sub-floor pick
named a catalyst, cut size under 1%, and stated in plain text that the ratio
was below floor. The rule-compliance grader passed them; the risk manager
agreed.

**The defect is in the rule, not in the model.** For any heavily-covered name
the news feed always carries a concrete, dated, real catalyst, so an
*assertable* exception is a null constraint on exactly the names it most needs
to bind — and the desk's own `active_state_changes` block was supplying the
catalyst that justified the exception. Note this is **not** a name-recognition
story: blinding the ticker was actually run (2026-09-02,
`feat/blind-the-ticker`) and changed nothing, NVDA being picked 5/5 in both
blinded arms. See `docs/architecture/MODEL_ROUTING_POLICY.md`.

Two deterministic changes, both applied **after the PM submits** — a tenth
firmly worded sentence in a 52KB prompt whose ninth was already obeyed is not
a design:

1. **The catalyst must resolve to a stored row.** It is cited by the ISO date
   of an `Active News State Changes` row, and that row must name the symbol.
   Those rows are already persisted (`news_store.recent_state_changes` over
   the dated daily reports) and already rendered into the PM's prompt with
   their date and affected symbols — no new data source. A citation that
   resolves to no such row means the exception does not apply and **the target
   is dropped**.
2. **A resolving sub-floor pick is capped in code** at
   `STARTER_POSITION_RISK_PCT` (0.5%), whatever size the model asked for.
   Not a new number: it is `RiskConfig.min_position_risk_pct`, the floor
   `allocate_risk_budget` already denies requests beneath. The capability is
   preserved at the smallest size the desk can express.

**Refusal DROPS the target; it never sizes it at zero.** A zero weight on a
held symbol is read downstream as *close the position*, so expressing this
refusal as a zero would silently liquidate a name we merely declined to add
to. It would not error — it would sell.

**Fail-closed throughout.** `risk_reward` of `None` (neutral rating, malformed
geometry) and NaN both land on the sub-floor branch — NaN is reachable, since
`reference_target` accepts it and every comparison against NaN is False. An
empty or unparseable state-change block, a row naming no symbols, a row older
than `ACTIVE_STATE_CHANGE_WINDOW_DAYS`, a future-dated row, or an unreadable
clock all make the exception *unavailable* rather than unbounded.

**Scope is deliberately asymmetric**: only opening and increasing targets are
gated. Exits and reductions are exempt — this desk must never find it harder
to cut risk than to add it.

**Measured blast radius, and read this before claiming the fix is decisive.**
Against the pre-reset production database (reset `20260902T181859Z`), 76 PM
targets were stored, 8 of which claimed a catalyst; all 8 were sub-floor.
Replaying the resolver over each session's real state-change block:

| verdict | count | symbols |
|---|---|---|
| still passes (citation resolves as written) | 2 | NVDA (08-28), ZS (09-01) |
| refused | 6 | CRM ×2 (08-28), NVDA (08-31, 09-01, 09-02), AUGO (09-02) |

**That 2-of-8 overstates the gate's bite and must not be quoted alone.** Those
six were written before the rule existed, so most fail only on *formatting* —
they assert prose with no ISO date. Asking the stronger question, *could a
correctly formatted citation have been found?*, **7 of the 8 were backable by
a real row naming the symbol**; only AUGO, a small cap with no state-change
coverage at all, is refused structurally. Tightening the recency bound does
not recover this: sweeping the maximum row age from 14 days down to 1 removes
only ZS (6 of 8 still backable), and only a same-session-only rule cuts it to
2 — a threshold with no doctrinal support, so it was not adopted.

**So the honest reading is that the CAP, not the resolution requirement, is
what binds on the mega-caps.** Resolution removes the unfalsifiable assertion
and makes every claimed exception auditable against a row a human can read;
it does not, on this evidence, remove many trades. The size cap applies to all
7 regardless.

**FOLLOW-UP 2026-09-03 — resolution was still existence-only.** "Resolves to
a row naming the symbol" said nothing about whether that row's news was GOOD
or BAD for the trade — a stock sinking on genuinely bad news could cite the
very row reporting that bad news to justify a BUY. See §13.1 and
`docs/INCIDENT_HISTORY.md` for the direction check added on top of this
resolution requirement; the existence check documented above is unchanged,
this only narrows what a resolving citation is allowed to be.

### Ordering

10.1 and 10.3 are contained schema/sizing changes. 10.2 requires the
deterministic analyst-weighting layer and is the largest. 10.4 depends on
Phase 1's structural levels being trustworthy. **Nothing here relaxes a risk
limit** — every change converts a blunt refusal into a proportionate response,
and the portfolio-level ceilings are untouched.

---

## Phase 11 — Fractional sizing and bounded margin (owner-ratified 2026-09-01)

**Status: ratified by Rex on 2026-09-01. 11.1 NOT implemented. 11.2's gross
cap, de-levering ladder and liquidation guard IMPLEMENTED 2026-09-01 — see
the note at the end of 11.2. `allow_margin` went `true` on 2026-09-02 — see
the note at line ~1646.**
Two decisions taken together because they both change how a position is
sized, and shipping one without the other changes the risk profile in a way
neither decision intended. The required ordering is therefore satisfied: the
ceiling exists before borrowing is switched on, and before fractional sizing
removes the whole-share friction that has been accidentally holding
deployment down.

### 11.1 Fractional shares are ON — the 2026-08-27 decision is reversed

**What the earlier decision said.** Fractional stays off because Alpaca
supports fractional for simple orders but not combined with bracket/OCO, and
QAMC attaches the protective stop as an OTO bracket at entry (invariant 3:
every position carries a broker-resident stop from the moment it opens).
Going fractional means placing the stop as a separate order after the fill —
a window where a position exists unprotected.

**Why it was wrong, in the owner's words: "if the gap is brief upon entry,
then it's irrelevant to eliminate that option."** He is right, and the
original reasoning conflated two different risks. A second or two of exposure
on a liquid name is negligible. The real risk is the stop order **never
landing at all** — an API error, a rate limit, the process dying between the
two steps — and that is not a brief gap, it is an indefinite one.

**That failure is already covered.** The intra-session stop-coverage
reconcile runs every 30 minutes and reports e.g. "all 5 long / 0 short
position(s) adequately stop-covered". So the true worst case is bounded at
one reconcile interval, and only when placement failed outright. The
machinery for attaching a stop to an already-landed fill also exists —
`place_entry_protection(superseded_filled_qty=...)`, built for the re-peg
case where a fill lands under a superseded order id.

**Verified live 2026-09-01, not inherited from the note:** the paper account
returns `fractionable=true` for MSFT, SPY and BRK.B.

**Required before this ships — all three, none optional:**
1. Stop placement retries immediately and hard on failure.
2. A stop that still fails alerts the OWNER, not a log line.
3. The 30-minute sweep gains an explicit check for positions with NO stop at
   all, distinct from the existing "stop is present but mis-sized" path.

**What it buys.** Exact sizing. Whole-share rounding is a silent, constant
tax: V wanted 6% of the book and got 3.84%.

**`BRK-B` — CLAIM WEAKENED 2026-09-01, do not treat as established.** I
verified only that a DIRECT call to `/v2/assets/BRK-B` returns `asset not
found`, and `/v2/assets/BRK.B` resolves. I then wrote "it fails on every run"
into this spec and into memory. **That does not follow.** A later session
pointed out the code has carried a symbol translator since 2026-08-25 that
rewrites this ticker at the broker boundary, with tests pinning it. So either
something bypasses that translator on the path that produced the logged error,
or the every-run claim was never true. **Unproven in both directions — trace it
before acting.** Recording the overclaim rather than quietly correcting it,
because it is exactly the failure mode this project keeps hitting: a single
verified fact generalised into a standing one.

**IMPLEMENTED 2026-09-01** (branch `worktree-agent-ab445cf7c4b9aa358`).
Fractional sizing exists for the first time — it was implemented nowhere, on
any branch, before this. **No risk threshold or limit moved.**

**The switch: `execution.fractional_enabled`, default `true`**, with
`execution.fractional_share_decimals` (default 4) setting the resolution. The
share count is FLOORED to that many places, never rounded up: rounding up
would spend more risk budget than the sizing math allowed, and a budget that
can be exceeded is not a budget.

**Eligibility fails closed, twice over.** The flag says what the desk wants;
only `AlpacaBroker.get_fractionability` — the same asset-directory lookup and
the same per-run cache `get_shortability` already uses — says what the broker
will accept. `fractionable` absent, unreadable, or the lookup raising all
mean **whole shares**. A SHORT is always whole-share and the broker is not
even asked: a borrowed share cannot be fractional.

**The premise that made the 2026-08-27 objection moot, and it is worth
stating because it changes the argument.** The protective stop has NOT been
an OTO bracket leg since the 2026-07-16 audit — an OTO child inherits the
parent's DAY tif and Alpaca expired it at 16:00 ET, leaving positions naked
overnight. It has been a separate GTC order placed after the fill ever since.
**So the fill→stop window fractional was said to introduce already existed on
every entry this desk has ever placed.** The reversal did not accept a new
risk; it noticed an old one and put guards on it.

**Guard 1 — retries, immediately and hard.**
`_submit_protective_stop_retrying` in `src/execution/broker.py`: **three
attempts over ~2 seconds** (0.5s then 1.5s), inside the same call, at the
point of failure. Three because every failure worth retrying is transient (a
429, a 5xx, a dropped connection) and clears in under a second, while a
failure that survives three attempts is a rejection that retrying will never
fix. ~2 seconds because the owner's own standard is that the gap is *brief* —
a retry loop long enough to matter becomes the exposure it was added to
close, and escalating to a human beats a fourth doomed attempt.

**Guard 2 — the owner is alerted, not the log.** `notifier.send_owner_alert`
raises it out-of-band the moment it happens, mirroring `src/cost_circuit.py`'s
established escalation shape (log CRITICAL first so a Telegram outage cannot
hide it, then send, never raise). The end-of-session message was the wrong
vehicle: an `intra_check` tick sends nothing unless it liquidates, and a
session that crashes after the failure sends nothing at all. It fires on a
stop that never landed and on a stop that covers fewer shares than are held —
and deliberately **not** on an entry that filled zero, which produces the same
`None` and is a non-event. Waking a human for a BUY that did not fill is how
a guard gets switched off.

**Guard 3 — NO STOP AT ALL is now a separate condition from MIS-SIZED.**
`_reconcile_stop_coverage` stamps `coverage: none | partial`, logs the first
at CRITICAL in those words, and both Telegram formatters render two banners
instead of one merged count. Only `none` escalates to the owner; a mis-sized
stop rides the session banner, because alerting on both is how a channel gets
tuned out. Two defects were found and fixed alongside it: the 30-minute
sweep **discarded** the reconciler's return value, so the tightest-cadence
caller was the one whose findings never reached the operator at all; and an
`intra_check` tick is silent by policy, so a gap found at 12:30 went to a log
file and nowhere else. The tick now breaks its own silence when, and only
when, the sweep found a gap.

**OPEN QUESTION, NOT SETTLED — and the reason the flag exists.** Whether
Alpaca accepts a **fractional-qty GTC stop-limit** is unverified. The
alpaca-py request docstrings say fractional quantities are for market orders
only; that was not confirmed against the live paper account, and no order was
submitted to find out. If it is false, every fractional entry would leave a
sub-share remainder the broker refuses to stop. **Contained, not ignored:**
when the exact quantity is refused through all retries, the stop is re-placed
for `floor(qty)` — covering 12 of 12.3456 shares beats covering none — and
the remainder is reported to the owner with `uncovered_qty`. **Settle this
empirically on the paper account before trusting fractional in production.**
If it resolves badly, set `execution.fractional_enabled: false`; that is what
the switch is for.

### 11.2 Margin is ON, capped at 2.0x gross exposure

### 11.2 Margin is ON, capped at 2.0x gross exposure — IMPLEMENTED (the cap, the ladder and the liquidation guard)

**Owner ratified 1.5x, then raised it to 2.0x on 2026-09-01**, against my
recommendation to defer. His reasoning, recorded because it is the reason the
higher number is acceptable: it is a PAPER account, leverage amplifies gains as
well as losses, and there are lessons to be learned that cannot be learned at
1x. **The 2.0x figure is therefore a deliberate learning setting on paper, NOT
a number to carry into live capital unexamined** — see
[[qamc-live-capital-checklist]] and re-derive it before real money. That
re-derivation is condition `margin_2x_rederived_for_live` in the live-capital
pre-flight gate (`src/live_capital_preflight.py`), which blocks live activation
until it is attested.
My argument was that the desk is 78% cash and refusing to deploy, so leverage
raises the stakes on the few trades it does take rather than producing more
of them. He considered it and decided; recorded here so the disagreement is
visible rather than silently dropped, and so that if 11.2 is later reversed,
the reason it was tried is legible.

**The account already permits this.** Alpaca reports `multiplier: 4`,
`buying_power` ~$28.2k against ~$9.88k equity. Margin is not being switched
on — it is being BOUNDED for the first time.

**This is the part that matters: there is currently NO gross-exposure cap in
the codebase.** `max_portfolio_risk_pct: 25` bounds AT-RISK capital (the sum
of stop distances), not gross exposure. Nothing today stops the book reaching
4x; it sits at 21.7% invested purely by the PM's own choice. **So 11.2 adds a
ceiling where none existed. Implementing it is a tightening, not a
loosening** — and shipping fractional sizing (11.1) without it would remove
the whole-share friction that has been accidentally holding deployment down.

**The rule:**
- Gross exposure (long market value + absolute short market value) may not
  exceed **2.0x equity**. Enforced deterministically in Python at the sizing
  and execution gates, not by asking an agent to respect it.
- **The cash-park does not count as exposure.** SGOV is parked cash, not a
  position; counting it would consume the entire allowance doing nothing.
- Never draw on the 4x intraday allowance — it forces a flat close.
- **2.0x applies overnight too.** No separate intraday ceiling; see the
  de-levering ladder below for where the cushion actually comes from.

**Two exposures the current risk envelope does not model, and must before
this is trusted:**
1. **Overnight gap.** Stops protect intraday. They do not protect against an
   open 15% lower, and levered that gap comes out of a thinner cushion. A
   separate, tighter ceiling on overnight gross exposure is required.

   **2.0x is the standing cap, day AND night. There is no lower overnight
   ceiling, deliberately.** An intraday-only allowance would force a trim into
   every close — selling on a clock rather than on merit, dumping whatever is
   easiest to sell rather than whatever deserves to go. That is a worse risk
   than the one it removes, and it is doubly pointless here because QAMC holds
   for days: it would almost never USE an intraday-only allowance, so the
   overnight number was always the real number. Owner's decision, 2026-09-01:
   *"I want to go 2x, so you just have to adjust at the 2x to give me enough
   cushion so we don't have forced selling. And 2x is the actual number then."*

   **The arithmetic.** At 2.0x — equity ~$9,825, gross ~$19,650, borrowed
   ~$9,825, ~25% maintenance — positions must fall **~33%** before forced
   liquidation. (At 1.5x it was ~55%.) A 33% drawdown is a bad quarter, not an
   impossibility, so the cushion cannot be left to chance.

   **The cushion comes from de-levering on drawdown, not from a lower cap.**
   The ceiling scales DOWN automatically as losses accumulate, so the 33%
   threshold is never approached, let alone tested:

   | peak-to-trough drawdown | gross exposure ceiling |
   | --- | --- |
   | 0% to -8% | 2.0x |
   | -8% to -15% | 1.5x |
   | -15% to -20% | 1.0x |
   | worse than -20% | 0.5x, and alert the owner |

   Enforced deterministically in Python, never as an instruction to an agent.
   De-levering REDUCES rather than liquidates — it blocks new exposure first
   and trims only if still over ceiling after that, so a drawdown does not
   trigger the panic-selling it exists to prevent. A drawdown-scaling mechanism
   already exists (`src/risk/rules.py::apply_drawdown_scale`); wire the ceiling
   to it rather than building a second one.

   **Distance-to-forced-liquidation is monitored and reported** in the morning
   alert and on the dashboard, as a percentage the book could fall before the
   broker sells. At 2.0x undrawn that reads ~33%.

   **Margin interest is charged ONLY on the end-of-day (overnight) debit
   balance** — Alpaca's live rate is 6.25% non-elite, 4.75% elite, computed as
   `(overnight debit balance x rate) / 360`. Intraday leverage is FREE. That
   is a live design lever, not a footnote: a desk that runs leveraged intraday
   and trims into the close pays nothing and carries no overnight gap risk.
   At a sustained 2.0x the cost is ~$1.71/day, ~$614/yr, **6.25% of equity that
   the book must out-earn before leverage contributes a cent.** Holding 2.0x
   overnight rather than trimming into the close is what makes that cost real
   — it is the accepted price of not force-selling on a clock, and the owner
   accepted it explicitly on the basis that the desk should be clearing well
   above 7% for the project to be worth doing at all.

   **Paper does NOT simulate short borrow fees** — Alpaca's own paper-trading
   comparison lists them as "Coming Soon". Whether paper simulates MARGIN
   INTEREST is not documented either way; secondary sources say it does not,
   and Alpaca's docs neither confirm nor deny. **Settle this empirically on the
   first night a debit balance is carried** — the charge either appears in the
   account's INT activities or it does not. Until then, report it as a clearly
   labelled ESTIMATE so paper trading does not teach the lesson with its
   largest recurring cost silently removed.

   **Margin interest tracker IMPLEMENTED 2026-09-01**, in
   `src/margin_interest.py` — this piece only; the gross-exposure cap
   and de-levering ladder above remain unbuilt. `overnight_debit_balance()`
   takes ONLY an end-of-day cash figure as its argument (no intraday
   variant exists), so intraday leverage with a flat close is zero interest
   by construction, not by convention — the design lever the owner's
   reasoning depends on is pinned in the function signature, not left to a
   caller's discipline. Formula `(debit x rate) / 360` at `risk.
   margin_interest_rate_pct` (config default 6.25, so a rate change needs
   no deploy); reproduces the spec's own $9,839-equity example exactly
   ($1.71/day, $614.94/yr). Every rendering carries `ESTIMATE_LABEL`
   verbatim — the dataclass field itself, not just the formatted string, so
   a consumer cannot drop the framing by reformatting. Surfaces on the
   morning Telegram alert only (silent, zero broker calls, whenever
   `allow_margin` is `False` — today's actual state) and on `GET /account`'s
   new `margin_interest` field (`src/api/schemas.py`,
   `src/api/broker_reads.py::read_margin_interest`) alongside the existing
   `risk_limits`/`liquidity` figures. The open empirical question is read
   via a new broker method, `AlpacaBroker.get_margin_interest_activities()`
   (Alpaca's `/v2/account/activities/INT`, via the SDK's untyped REST
   passthrough — alpaca-py 0.44.0 has no typed wrapper for this endpoint),
   compared against the estimate by `compare_estimate_to_broker_activity()`.
   It does not pre-judge the answer: no `INT` rows reports "not confirmed",
   not "confirmed absent". **Still unobserved** — `allow_margin` has not
   been flipped on, so no night has yet carried a real debit balance to
   check this against.

   **Weekend/holiday carry, 2026-09-23.** Owner-confirmed from Alpaca's
   own docs: interest is charged for EVERY calendar day a debit balance is
   carried, trading day or not — a Friday's overnight is 3 days
   (Fri+Sat+Sun), a Friday before a Monday holiday is 4. The Telegram line
   now reads the exchange calendar (`AlpacaBroker.is_trading_day`, via
   `days_charged_until_next_trading_day`) and, when the carry spans more
   than one day, appends "carried over the weekend that's 3 days ≈ $X".
   `daily_usd`/`annual_usd` are unchanged; `period_usd` is the multi-day
   total. A calendar read failure degrades to 1 day (the old flat figure),
   never to an error. `GET /account`'s field still reports the flat
   per-day figure only.

   **Verified 2026-09-01, one defect found and fixed.** Both wrappers
   (`read_margin_interest` and the Telegram `_margin_interest_lines`)
   originally fast-exited to "nothing to report" whenever `allow_margin`
   was `False`, without ever looking at `cash` — reasoning that cash-only
   keeps cash non-negative. It does not, in general: `cash_only` hard-
   blocks a plain BUY, but D10 deliberately exempts COVER from it, and the
   PM's own DE-LEVER MANDATE already treats "cash negative, `allow_margin`
   False" as a real state a session can reach. That gate could have
   silently under-reported exactly the debit balance this tracker exists
   to catch. Fixed to key off `cash` alone, matching
   `overnight_debit_balance()`'s own contract; regression tests added
   (`tests/test_margin_interest.py`, §6-7). Formula and $9,839-example
   reproduction independently re-derived by hand, not just re-read from
   the module's own tests — both match. Two spec items above remain
   short of "done": the estimate is a live read-time snapshot, not
   accrued daily into storage and reported cumulatively; and it reaches
   the Telegram alert and `GET /account` only — no dashboard UI panel
   renders `margin_interest` yet (the frontend's `AccountResponse` type in
   `frontend/src/api/client.ts` doesn't declare the field). See
   `docs/WORK.md`'s margin-interest status note for the fuller account.

   **Cumulative view shipped 2026-09-24**, closing both gaps above. Owner
   ask: replace the per-day/per-year figures and the ESTIMATE-caveat
   paragraph with this week's running total, the current month, each of up
   to five more recent months that had any interest (zero months skipped),
   and an all-time total — on both the cockpit panel and the Telegram line.
   `AlpacaBroker.get_margin_interest_activities()` was re-checked with a
   real $6,114.51 overnight debit (up from the first $915.83 measurement)
   and again returned zero `INT` rows — a second, larger, independent
   confirmation that paper trading does not post real margin-interest
   charges. `compute_cumulative_margin_interest()`
   (`src/margin_interest.py`) therefore still prefers a broker-confirmed
   `INT` charge per bucket (Alpaca's own permanent ledger, which would
   cover the account's FULL history with no local storage needed) but
   currently always falls back to summing a NEW daily-accrual persistence
   table, `margin_interest_daily` (`src/storage/db.py`, written each
   morning by `src/notifier.py::_persist_margin_interest_daily`).
   **Known limit, stated rather than papered over:** `daily_pnl` never
   stored a historical cash/debit figure, so no accurate ESTIMATE-path
   "all-time" total is possible for any day before this table started
   being written (2026-09-24) — `all_time_since` on every cumulative
   result names exactly which date its own total is counted from, so
   "all-time" can never be misread as "since the desk went on margin" when
   the two differ. The cockpit's per-day/per-year figures and the
   paragraph-length `ESTIMATE_LABEL` caveat are gone from both renderings;
   a single small "est." tag next to a genuinely nonzero, unconfirmed
   figure is the entire marker now (a certain zero never carries it, same
   rule `format_daily_line` already applied to the per-day figure).
2. **Forced liquidation.** Below maintenance margin the broker sells, at the
   worst moment, without asking. Nothing currently watches the distance to
   that threshold or alerts on it.

**CORRECTION APPLIED 2026-09-01 during the Phase 12 integration.** The golden
PM prompt on `feat/golden-pm-prompt` was written as though 11.2 had shipped: it
told the Portfolio Manager that gross exposure up to **2.0x** was permitted and
that "the engine de-levers automatically as drawdown deepens". Neither is true.
`allow_margin` is `false`, `src/pipeline.py` force-delevers on any cash deficit,
and `apply_drawdown_scale` is a flat 0.5x halving on a binary `in_drawdown`
flag — not the graduated 2.0/1.5/1.0/0.5 ladder the prompt described as already
enforced. A prompt that promises capability the deterministic engine does not
have is the same class of defect as Phase 10's: the agent proposes a book the
engine then refuses.

The prompt's exposure table and guardrail line were therefore corrected to the
**1.0x cash-only reality**, keeping the golden rewrite's structure. **This is a
correction to a description, not a reversal of the owner's 2.0x decision** —
11.2 remains ratified and unbuilt. When it ships, the prompt's exposure table,
its guardrail line and rule-priority rows 6 and 9 are what change back.

**Sequencing.** 11.2's gross cap and both gap/liquidation guards land BEFORE
or WITH 11.1. Fractional sizing plus no gross cap is the one ordering that is
worse than either change alone.

**IMPLEMENTED 2026-09-01** (the gross cap, the ladder and the liquidation
guard — *not* 11.1, and *not* the margin-interest tracker, which is separate
work). The ceiling was built BEFORE borrowing was enabled, which was the
whole sequencing requirement. **`allow_margin` went `true` on 2026-09-02**
(owner ratified, PM prompt moved to the 2.0x table in the same commit).

**The ceiling.** `risk.max_gross_exposure_x: 2.0`. Gross = long market value
+ absolute short market value, leverage-adjusted, with the cash park
(`cash_sweep.symbol`) excluded — one measurement, `src/risk/rules.py::
gross_exposure`, and every consumer calls it. Enforced at BOTH gates, in
Python, never as an instruction to an agent:

| gate | where | what it does |
|---|---|---|
| sizing | `PortfolioConstructor.construct_orders` | shrinks entries to fit; refuses one whose remnant is under `min_order_usd` |
| pre-trade | `RiskRuleEngine.check` → `max_gross_exposure` (in `HARD_BLOCK_RULES`) | hard-blocks anything that reached the engine without that sizing |
| submit | `ExecutionStage` → `_entry_deployment_budget` | the pool every entry actually draws from, sized at the rung in force (added 2026-09-02 — see below) |

Distinct from `max_total_position_pct`, which bounds NET exposure — a hedge
cancels a long there and does not here — and from `max_gross_bearish_pct`,
which bounds only the bearish side. **No previous ceiling answered "how much
does the book own", which is the question a margin call asks.**

**The ladder** is `resolve_gross_ceiling(drawdown_pct, base_x)` — a PURE
FUNCTION of peak-to-trough drawdown and the configured cap, returning the
ratified 2.0/1.5/1.0/0.5 rungs. It can only ever TIGHTEN the configured cap,
so lowering the setting lowers every rung. **Ties resolve to the tighter
rung** (exactly -8.00% is 1.5x, not 2.0x); an UNKNOWN drawdown resolves to
the standing cap and trims nothing, because a fresh account with no equity
history has not fallen.

**Block first, trim second — enforced structurally, not by convention.**
`apply_gross_ceiling` counts planned exits, then rations new entries against
the remaining headroom, and only then tests whether the HELD book ALONE is
still over. Proposed exposure is not an input to that last test, so the
engine cannot sell what the desk owns to make room for what it does not.

**Nothing in the ladder depends on the Portfolio Manager.** The ceiling is
resolved from account state in the run preamble
(`TradingPipeline._enforce_gross_ceiling`), before any agent is called, and
the de-lever is engine-authored. A blank or mid-JSON-truncated PM response —
measured at 1 run in 10 on one candidate model — is a no-trade session, and
must never also be a no-de-lever session.

**SUPERSEDED 2026-09-20.** `apply_drawdown_scale` and the whole rolling
5d/20d `in_drawdown` brake were deleted on the owner's instruction, together
with the daily-loss breaker (`docs/INCIDENT_HISTORY.md`, retired board item
32). The §11.2 ladder described in this section is untouched and remains
live; the paragraph below describes a wiring that no longer exists, and is
kept because this file records what was specified at the time.

**Wired to `apply_drawdown_scale`, not duplicated.** That function keeps its
ratified flat 0.5x halving of new BUYs on the rolling 5d/20d `in_drawdown`
flag, unchanged in threshold or magnitude, and now takes the resolved ceiling
so its note NAMES the rung in force. The two drawdown measures are
deliberately distinct and documented as such: "has our recent edge degraded,
so halve new BUYs" is not the same question as "how far are we off the
high-water mark, so how much may the book own".

**Distance-to-forced-liquidation** is computed by
`distance_to_forced_liquidation_pct` from the maintenance requirement
(`risk.maintenance_margin_pct: 25`) and reproduces this section's two
published figures rather than restating them: 33.3% at 2.0x, 55.6% at 1.5x.
It is on the session alert. It is deliberately NOT on `/account`: `src/api/`
is forbidden by a ratified structural guardrail
(`tests/test_api_safety.py::test_api_source_files_never_import_pipeline_or_risk`)
from importing `src.risk.*` at all, and re-implementing the arithmetic in the
API layer was rejected — a second definition of "how much does the book own"
is the sprawl §12.2 cleaned up. `/account` reports the standing cap only
(`RiskLimits.max_gross_exposure_x`). Putting the live ladder state on the
dashboard needs the pure measurement functions moved out of `src.risk` first;
that is not done and is the one piece of §11.2 left open.

**Each trimmed name is re-protected before the next is touched (2026-09-19,
board item 111).** Selling means cancelling that name's protective stop first
(a resting stop holds the whole position). `_enforce_gross_ceiling` and
`_force_delever` now finalize each name's stop coverage on its actual fill —
restore, re-protect the residual, or nothing on a full exit — before the loop
cancels the next name's stops. Before this, the whole batch was finalized once
after the loop, leaving every earlier name uncovered while later names were
sold. What is trimmed, and how much, is unchanged.

**A de-lever that leaves the book over its ceiling is recorded (2026-09-19,
board item 112).** Besides the `delever_incomplete` flag the session message
reads, one run-scoped `specialist_evidence` `pipeline_event` row
(`stage='gross_delever'`, `outcome='still_over_ceiling'`) keeps gross before
and after, the ceiling, and each order's final broker status. Observability
only; see `docs/INCIDENT_HISTORY.md`, 2026-09-19.

**The owner's gate is met.** `tests/test_gross_exposure_ladder.py` (54 tests
at the time of writing; 92 as of 2026-09-02, the 21 added being the submit
gate below)
asserts the ceiling CHANGES on both sides of all four thresholds, that new
exposure is blocked before anything is trimmed, that the ladder is applied
exactly once — per call AND across the sizing-gate/execution-gate chain, with
trimming structurally restricted to a single caller — and that a blank-PM
session in drawdown still de-levers. Every property was re-verified by
mutating the implementation and confirming the tests fail: a constant ladder
fails 23 of them, a trim-to-make-room ordering fails 4, a compounding
multiplier fails 1, and a PM-coupled de-lever fails 5 (including the two
named blank-PM tests). The session alert that renders the rung is pinned too
— it is the only place a human sees the ladder, now that the dashboard
cannot.

**Mutation figures, re-measured 2026-09-01** (the numbers in the paragraph
above were the first pass and understate three of the four; these are the
ones actually reproduced, each mutation applied to `src/risk/rules.py` and
reverted):

| mutation | tests failed |
|---|---|
| the ladder never steps (rung loop disabled) | 24 of 54 |
| trim-to-make-room (entries never rationed, trims judged on the *projected* book) | 6 |
| the rung applied as a compounding multiplier on order size | 2 |
| the de-lever suppressed when the PM proposed nothing | 5 |

One negative result is worth recording, because it looks like a test hole and
is not: changing *only* `over` in step 3 from `held_gross_after_exits` to
`projected_gross` fails nothing. It cannot — step 2 has already capped
`granted` at the headroom, so `projected_gross` can never exceed the ceiling
unless the held book alone already does, in which case the two expressions
are equal. Block-before-trim is enforced by the *sequence*, not by that
subtraction, and a real trim-to-make-room defect (the row above) is caught 6
ways.

**Not done here, deliberately:** 11.1 (fractional shares), the margin-
interest tracker, and flipping `allow_margin`.

#### 2026-09-02 — the ceiling was INERT for longs, and a third gate now exists

`allow_margin: true` shipped on the morning of 2026-09-02 and changed nothing
a long could do. The two gates in the table above were correct and both ran;
a THIRD gate nobody had listed sat behind them, in the BUY submit loop, and
it was the one that actually decided the size:

    available_cash = cash                    # broker's RAW cash figure
    ...
    if not is_short and estimated_cost > available_cash:   # gated on nothing

Raw cash is at most `equity - gross`, so this held gross under 1.0x
STRUCTURALLY: however high `max_gross_exposure_x` was set, a long could never
cost more than settled money. Shorts were exempt (D11) and unaffected.

**The clamp was NOT removed — deleting it was considered and rejected.** With
margin on the broker ACCEPTS a buy that exceeds cash, so this loop is the last
quantitative bound before an order leaves. What it clamps against changed:
`_entry_deployment_budget` returns `ceiling_x x equity - held_gross`, so the
ladder is the one number that governs how much the desk deploys. The pool
decrements per entry, and a SHORT decrements it too (a short OWNS gross) while
never being sized or refused by it, which keeps D11 exactly as it shipped.
`max_position_pct` is re-applied to the size execution chose so one order
cannot drain the session's pool — same idiom as the §10.3 notional floor
beside it.

Fail-closed in all three degraded directions, because nothing runs after this
gate: an unresolvable ceiling falls back to the pre-margin RAW-CASH clamp
(never to the standing cap — the constructor may do that because another gate
still follows it); an unusable equity read yields a ZERO budget, since the
floor rung the bad-read guard forces would otherwise be multiplied by a NaN
equity and every `>` against the result would be False, granting infinite
room on exactly the broken-snapshot morning that guard exists for; and an
unreadable cash-park symbol counts parked cash as gross, understating the
headroom.

**With `allow_margin` false the tighter of ladder-headroom and settled cash
still governs**, so turning the ceiling into the primary limit did not quietly
hand a cash-only account the right to borrow.

**The magnitude, stated plainly.** On the live book of 2026-09-02 (equity
$9,822.37, gross $3,819.72 = 0.39x, raw cash $580.09, $5,422.56 parked, rung
`none` = 2.0x) the maximum new entry notional in one session moves from
$6,002.65 (cash + convertible park) to $15,825.02 (ladder headroom) — **2.64x
more deployable per session, and the largest book the desk can hold doubles
from 1.0x to 2.0x of equity.** No order in the recorded 2026-09-02 runs was
trimmed or refused by the old clamp, so the change deploys nothing extra
retrospectively; it raises a ceiling that had not yet been reached.

**OPEN, and it is the owner's call: 2.0x is still not reachable.**
`max_total_position_pct: 90` is a hard block on NET exposure, and for a
long-only book net IS gross. Measured against the live settings, the highest
long-only gross that still admits a BUY is **0.90x**; with shorts run up to
their own `max_gross_bearish_pct: 20` cap the ceiling is about **1.3x**. So
the standing 2.0x rung and the -8% 1.5x rung can never bind — only the -15%
(1.0x) and -20% (0.5x) rungs can. The PM prompt's `risk-on` target of
1.60-2.00x gross is therefore still telling the model something the engine
will refuse. Moving `max_total_position_pct` is a risk-limit change and is
not made here.

**Neither part ships without a rehearsal-rig run** — see the session-start
rule in `docs/WORK.md`. This changes sizing and execution, which is exactly
the class the rig exists for.

---

## Phase 12 — Owner decisions ratified 2026-09-01 (end of session)

Four decisions, taken by Rex on 2026-09-01 after the desk placed zero trades.
**All ratified, none implemented.** Together they are what unblocks trading.

### 12.1 A stop that sits at a verified structural level is honoured, however tight

`min_stop_atr_multiple` (3.0) currently OVERWRITES the level-derived stop
whenever the level sits closer, after which the stop is not at anything real —
and `min_reward_risk_after_widening` (1.5) is judged against that fabricated
number.

**New rule: if the stop sits at a VERIFIED structural level, use it regardless
of ATR distance. Apply the ATR floor ONLY when no level backs the stop.**

Why this is safe: `config/prompts/tech_analyst.md` already carries a hard floor
— "never place the stop inside 1*ATR of entry ... a guaranteed whipsaw, not
protection". A genuine noise-band stop cannot reach the constructor, so the
case the 3.0 floor was built for is already handled upstream.

Why it is necessary: measured 2026-08-27 and quoted in that same prompt, this
book's stops sat at a median **1.7x ATR** — above the 1x guard, i.e. legitimate
structural stops being inflated ~76%. Over a ~15-session hold a stock travels
~3.9 ATR, so against a 3.0 ATR stop the best achievable ratio is ~1.29 against
a 1.5 floor: **no trade can pass.** Proof: SLB on 2026-09-01, `strong_buy`/
`high`, R/R 1.28 against a geometric maximum of 1.29. At 1.7x ATR the same hold
yields ~2.3 and clears comfortably.

**Explicitly rejected: tuning 3.0 -> 2.0.** It keeps a floor that cannot
distinguish a level-backed tight stop from an arbitrary one.

> **2026-09-04 — this rejection was right about the mechanism and wrong to
> stop there.** §12.1's exemption was necessary and was built; it did not
> address the fact that the 3.0 itself had no derivation. The floor has now
> been re-derived to 1.5 from real MAE data — which is a different act from
> "tuning it to 2.0", because the replacement is measured against real
> winners rather than chosen. Both fixes were needed. Note the practical
> effect on this section: at a 1.35-ATR band, the honoured/band gap §12.1
> arbitrates is a [1.00, 1.35] ATR window rather than [1.00, 3.45], so §12.1
> now governs far less of the space than it did.

**A level must be VERIFIED to earn this** — it comes from `src/data/levels.py`,
not from the model asserting one. A stop the analyst simply placed close, with
no level under it, still gets the floor.

**IMPLEMENTED 2026-09-01** (branch `feat/level-backed-stops`), in
`PortfolioConstructor._widen_stop_past_noise`. The stop rule now has five
outcomes instead of one, each logged by name:

| the stop | what happens | reward:risk is measured against |
|---|---|---|
| already ≥ `min_stop_atr_multiple` ATRs out | **left exactly where structure put it** | the kept stop |
| at a computed level, ≥ 1x ATR out | **honoured exactly as placed** | the honoured stop |
| at a computed level, < 1x ATR out | widened to **1x ATR** — never to the band | the 1x ATR stop |
| not at a computed level | widened to `min_stop_atr_multiple` ATRs, as before | the band edge |
| no ATR reading at all | **stop READ from price structure and the position HELD** (board item 80, owner 2026-09-25) — nearest verified computed level on the protective side, else the signal/prior bar, placed one appetite buffer past it; refused only when no level is readable or it fails the desk's stop-distance sanity bound | the structural stop |

**Item 80 rework (owner ruling 2026-09-25).** The last row above changed twice. It originally *honoured* a model-typed stop with no ATR (an unverifiable number setting position size — the item-80 defect). A first pass then made it *refuse* the name. The owner overruled that: "there are always levels, even from a few days ago, and there are other ways of setting a stop." So with no ATR the stop is now derived from price structure that survives on the analysis object (`computed_levels` with enough touches, else `signal_bar_low`/`signal_bar_high`) and the position is HELD; the name is skipped only when no structural level is readable at all, or the only readable one sits past the desk's existing stop-distance sanity bound (skip on risk, not on the missing reading). Published basis: swing-low / prior-bar low / Donchian channel-low stops. The buffer past the level is owner-appetite (`ConstructorConfig.structural_stop_buffer_pct`, ledgered).

**Neither threshold moved AT THE TIME THIS SPEC SECTION WAS WRITTEN.**
`min_stop_atr_multiple` has since moved twice — 3.0 (as written here) -> 1.5
(2026-09-04, own MAE data, later found to overlap a window of misreported
seat confidence) -> 2.5 (2026-09-10, published swing-trading doctrine
instead). `min_reward_risk_after_widening` is still 1.5, unmoved. This spec
is a historical record and is not corrected number-by-number below; check
`docs/STATE.md` for the current value. This changes WHICH stop the
arithmetic is performed on, nothing else — a level-backed trade whose real
geometry still fails 1.5 is still refused, by a distinctly named code.

**No second data path was built.** §10.4 had already established
`TechAnalysisResult.computed_levels` — every level `find_structural_levels`
found, supports and resistances unioned, set in Python by `TechAnalystAgent`
after the model's response is parsed. That field is what "verified" means
here, and it is reused rather than duplicated. Its side is re-partitioned
against the trade's ENTRY, not against the last close, for exactly the reason
`derive_structural_target` already does so: a ceiling price has broken through
is a floor, and the entry can sit on the other side of a level from the close.

**The model cannot write the field, and that is now pinned by tests.**
`computed_levels` appears nowhere in `config/prompts/tech_analyst.md`, and the
parse path overwrites it unconditionally with what the bars actually produced —
so a model that emits one anyway has it discarded, including down to an empty
list when the history is too short to yield levels. Without that, a model could
assert a level beside its own stop and buy itself an exemption from the noise
floor, and the verification would be worthless.

**Matching tolerance: the level's own zone width. Not configurable.**
~~`risk.level_match_atr_tolerance`, default 0.25 ATR~~ — **CORRECTED
2026-09-13, docs/INCIDENT_HISTORY.md.** The original text here argued the
tolerance should be ATR-relative *and* "at least as wide" as the 1% zone
`find_structural_levels` clusters pivots into. Those are different units, so
the second claim was a hidden condition on the instrument rather than a
property of the setting: `0.25 x ATR >= 0.01 x price` needs `ATR >= 4% of
price`, and at this desk's quoted 2.56% median ATR the tolerance was 0.64%
against a 1% zone — 1.56x too narrow, so a stop inside a level's real zone
was not recognised as level-backed. The setting is deleted rather than
re-tuned (no ATR multiple can stay consistent with a percentage of price),
and the match now reads the bound off the zone's own definition,
`src.data.levels.CLUSTER_TOLERANCE_PCT`. A settings file still carrying the
old key is refused at load. The ATR argument was not wrong, only misfiled:
whether a stop survives the name's noise remains `min_stop_atr_multiple` /
`absolute_min_stop_atr_multiple`, and whether a level has broken remains the
ATR noise band.

**No strength or touches threshold was added on top.**
`find_structural_levels` already filters: at least 2 touches (`MIN_TOUCHES` —
"a level touched once is a coincidence, not structure"), within 40% of the last
close, recency-weighted and distance-penalised, and capped at the 6 strongest
per side. Stacking a second threshold here would be an unratified risk decision.

### 12.1a The 1x ATR floor — an addition beyond the ratified wording

**Flagged for reversal if the owner disagrees.** §12.1 above argues the
exemption is safe because `config/prompts/tech_analyst.md` already forbids a
stop inside 1*ATR of entry. That argument rests on a **prompt** — an
instruction to a language model — while Invariant 2 of this spec requires
deterministic Python protections to be the final authority and to fail closed.
A prompt is not a deterministic guarantee, and the whole point of §12.1 is that
a tight stop now ships unaltered.

The case that forced this: a genuine support level sitting 0.2 ATR under entry
is real structure *and* a guaranteed whipsaw. Both are true at once, and
honouring it would hand the broker a stop that fires on the first ordinary
tick.

So the implementation adds `risk.absolute_min_stop_atr_multiple` (default 1.0):
a level-backed stop is honoured however tight **down to 1x ATR**, and inside
that it is widened to exactly 1x ATR — **not** to the 3.0x band, which is the
behaviour §12.1 removes. Setting it to 0 restores the ratified wording
literally, and a test pins that.

**The prompt was corrected, not left to contradict the code.**
`config/prompts/tech_analyst.md` previously told the analyst that a 3.0x ATR
floor would widen its stop and could reject the trade on reward:risk. That is
no longer true and was actively misleading — it taught the model to pad stops
to survive a floor that no longer applies to a level-backed stop. It now
describes the three-outcome rule above, tells the analyst to anchor the stop to
the computed "Structural levels" block it is already shown, and states plainly
that naming a price in `support_levels` does not make it a verified level.

**Unverified, and must not be quoted as if measured:** how many of the 38
signals from 2026-09-01 would now pass. That run's per-symbol levels, ATRs and
horizons are not available offline. The SLB reproduction in
`tests/test_risk_based_sizing.py::TestSLBStopIsHonoured` uses **synthetic**
level data, exactly as §10.4's does, and pins the arithmetic to the same
back-solved ATR so the two reproductions agree: R/R 1.28 against the band stop
(refused, as it was on the day), 2.59 against the honoured stop at the measured
1.7x ATR median.

### 12.1b The reward:risk floor was not a floor (fixed 2026-09-02)

**Rows 1 and 5 of §12.1's table are a correction.** As shipped on 2026-09-01
the reward:risk check lived INSIDE the two widening branches, behind two
unnamed early returns — "already outside the noise band" and "no ATR
reading". A stop that was wide enough to begin with was returned before the
check was reached, so **the 1.5 floor was never applied to it**. The
config key `min_reward_risk_after_widening` recorded that as if it were the
design; it was not, and it fails OPEN and silently.

Measured on the pre-reset production database
(`data/resets/20260902T181859Z/quant_agent.db`), 2026-08-18 to 2026-09-02:

* **14 of the 49 constructed entry orders — 29% — shipped with a
  reward:risk below 1.5.** Lowest: XLB 0.43 (2026-09-02), NET 0.50
  (2026-08-27), ZS 0.56, XLE 0.57.
* The constructor's own refusal message appears **zero times** in the
  entire production log history before 2026-09-02.
* **Two of the 14 reached the broker.** XLE on 2026-08-21 FILLED — 9 shares
  at $64.26, reward:risk 0.81. VLO the same run at 1.19 was submitted and
  cancelled at the broker, not by us.
* Everything else was stopped by the **Risk Manager's prose** or by the
  1.2 execution-time belt in `src/pipeline_stages.py`. A deterministic
  floor was being enforced by a language model, which Invariant 2 forbids.

The floor now runs once, on the stop that will actually ship, whichever
rule placed it, and the refusal names that rule
(`reward_risk_below_floor_at_kept_stop` for the two paths above). **The
threshold did not move and no path became more permissive.** Rerun over the
same 49 orders it refuses exactly those 14 at construction and admits
nothing new. Of the 14, **7 had nothing downstream cite reward:risk at
all** — XLE 0.57 and XLF 1.31 (08-18), XLE 0.65 (08-20), VLO 1.19 and COP
1.40 (08-21), NET 0.50 and ZS 0.56 (08-27) — so for those the fix is not
"dies earlier", it is "dies at all". The one that FILLED, XLE 0.81 on
08-21, was not among them but was not saved either: the RM cited "R/R =
1.22", which is the ANALYST's geometry, and merely halved the allocation.
The rest were caught late by the RM's prose or the execution belt, and now
die deterministically at the stage that owns the rule.

**A missing or unmeasurable ratio is now a refusal, not a pass.** `nan <
1.5` is False, so an unguarded non-finite price cleared the floor rather
than failing it. Non-finite entry and stop prices are refused at the top of
`_widen_stop_past_noise` — a NaN stop previously passed both that function
and the caller's `stop_loss >= entry_price` validity check and reached
`TradeDecision` intact.

### 12.1c One definition of reward:risk, and one stop geometry

The division itself now lives in `models.reward_to_risk` and nowhere else.
It had been re-derived by hand in four places, which disagreed.

The rejection that exposed it, verbatim from the Risk Manager on
2026-09-01: *"PM's reasoning assumes R/R 1.67 but the executed order has R/R
1.18."* **Both numbers were correct and no stop had moved** — $61.54 on the
tech analyst's row and $61.54 on the proposed order. The ENTRY moved,
$63.96 (analysis snapshot) to $64.51 (live). On this trade the analyst's
guess and the derived target happened to coincide at $68.00, so the entry
drift was the WHOLE of the gap. The same seat caught the same thing on
2026-08-31: *"entry price degradation from TechAnalyst's $62.29 to
$63.76."* **The ATR stop floor was not involved in either case** — a
correction to the earlier reading of this defect.

So the two remaining differences are handled where they actually are:

* **Arithmetic** — one function, used by `TechAnalysisResult.risk_reward`,
  `TradeDecision.reward_risk`, the constructor's floor and the 1.2
  execution-time re-check. Fail-closed on non-finite input.
* **Inputs** — the analyst's R/R and the order's R/R answer different
  questions and will legitimately differ. The Risk Manager prompt now says
  which of them the floor is judged on, instead of leaving the seat to
  infer a defect from an unexplained gap.

**The execution stage could still undo §12.1, and no longer can.**
`src/pipeline_stages.py` carries a second, execution-time 1x ATR stop floor
(BUY-only) applied with no knowledge of levels, against an ATR recomputed
from `ctx.symbols_bars` rather than the `analysis.atr_14` the constructor
used. Two readings of one quantity, the larger silently winning, moving a
level-honoured stop off its level and shrinking the R/R the 1.2 re-check
then judges. **This is a mechanism, not an observed event** — no production
instance of it firing on a level-honoured stop was found, and §12.1 has
only been live since 2026-09-02, so there was barely a window for one. It
now reads `TradeDecision.stop_rule` — set by the constructor, the stage
that can see `computed_levels` — and skips a stop honoured at a computed
level. No second level-detection path was built.

### 12.2 Sector exposure is measured with separate long and short budgets

Today the engine sums sector exposure using SIGNED `market_value`, so a held
short makes its sector look SMALLER and the book can over-concentrate unseen.
The code comment above it says "gross ... unsigned"; code and comment disagree.

**New rule: track long sector exposure and short sector exposure
independently, each against its own limit.** Neither offsets the other.

Owner's reasoning, which governs: *"A long and a short in the same sector is
not a hedge... We are trading opportunities."* Gross summing was rejected
because it would block a legitimate pair trade — long the leader, short the
laggard in the same hot sector.

**IMPLEMENTED 2026-09-01** (branch `worktree-agent-aa9554aa2b76e1b3f`).
Exposure is keyed by `(sector, side)` and summed as an UNSIGNED magnitude.
`side` is the POSITION SIDE (long vs short), not the bullish/bearish thesis —
an inverse-ETF LONG is long-side exposure in its sector, and the separate
`max_gross_bearish_pct` cap answers the directional question. Both sides are
measured against the SAME `max_sector_pct`; no second config key was added.

**Reconnaissance found THREE implementations of "how much is this sector
holding", and a fourth.** All four had the same signed bug and all four now
call one definition — `src/risk/rules.py::sector_side_gross` /
`sector_side_weights`:

| # | site | what it drives | what changed |
|---|---|---|---|
| a | `RiskRuleEngine.check` | the enforcement gate | signed sum → `(sector, side)` unsigned; the lying "gross ... unsigned" comment is now true |
| b | `PMFacts.sector_weights` | what the PM reads | **deliberate reversal** — `sector_weights_long` / `sector_weights_short`, rendered as two lists and never netted |
| c | `_build_projected_portfolio` | the pre-decision preview | signed sum → side-split; its hardcoded overweight threshold of `35` now reads `risk.max_sector_pct` |
| d | `PortfolioConstructor._current_sector_weights` | order sizing | a FOURTH site, found during the build; its §10.3 docstring had explicitly deferred this fix ("not this change's business"). Left alone it would have sized orders against a different book than the gate measures |

Both in-batch accumulators are keyed `(sector, side)` for the same reason — a
pending SHORT must not consume the next BUY's sector budget:
`pending_sector_investment` in the pipeline's risk filter, and
`PortfolioConstructor._accrue_sector` in the sizing loop.

**(b) is a reversal of a previously deliberate decision.**
`tests/test_shorts_countable.py` pinned NETTING as intended (long 15% + short
-5% in Technology → one 10% line). It now pins the opposite, and its docstring
records why. The reason is not taste: the gate enforces per side, so a netted
PM table showed the Portfolio Manager a *smaller* number than the engine
refuses on — the same PM-sees-one-thing / engine-enforces-another defect class
as Phase 10.

**The threshold in (c) was wired to config rather than replaced with a fourth
number.** It warns at `max_sector_pct` itself, not at some band below it: at or
under the target, crowding costs a trade nothing (`sector_size_scale` returns
1.0), so there is nothing actionable to say; above it every further trade in
that sector is shrunk, which is exactly what the PM needs to know before it
writes decisions.

**Explicitly NOT changed here, and it was a real exposure.** A held position
whose sector resolves to `"Unknown"` contributed to no sector bucket, and an
incoming `"Unknown"` symbol was cap-exempt entirely. **80 of the 101 universe
symbols depend on a live yfinance `.info` lookup with no static fallback**
(the other 21 are ETFs covered by `_ETF_SECTORS` / `_INDEX_ETFS` in
`src/execution/broker.py`). `_get_sector` deliberately does not cache
`"Unknown"`, so an outage does not permanently exempt a symbol — but for the
duration of the outage the sector cap was OFF for any of those 80. Reported,
not fixed, under this task's scope.

**IMPLEMENTED 2026-09-01** (branch `worktree-agent-af17eb92755512448`,
same night — this was the gate the 75%/90%/margin-2.0x combination above was
shipping behind). `RiskRuleEngine.check` rule 5 now calls
`sector_side_gross(positions, include_unknown=True)` and no longer skips the
block when `_get_sector` returns `"Unknown"`; `accumulate_pending_sector`
pools it the same way for the in-batch accumulator. "Unknown" is treated as
its own `(sector, side)` bucket, checked against the same soft target / hard
ceiling as any real sector — conservative, not exempt — so a held or
incoming unresolved-sector position no longer disappears from exposure or
skips the cap. Deliberately narrow: `PortfolioConstructor`'s sizing pass
(`_apply_sector_dial`, site (d) above) still does not pre-shrink for
`"Unknown"` — that is unrelated, out of scope here, and safe left alone
because the gate's hard wall is what actually enforces the ceiling
regardless of whether sizing pre-shrank the order. A resolution failure now
also raises a `sector_unresolved_lookup_failed` / `sector_unresolved_no_sector`
advisory (distinguishing a transient lookup miss from a symbol that
genuinely has no sector) that reaches the Risk Manager via `rule_violations`
and surfaces as `data_status["sector"]` — the same "degraded" line the news
and macro feeds already use in the session output and the owner's Telegram
alert. No offline sector table was built for the 80 uncovered names — see
`docs/INCIDENT_HISTORY.md` (2026-09-01, "a network blip could silently
switch off the sector concentration cap") for why that was judged out of
scope. Pinned by `tests/test_sector_cap_unresolved.py`.

**Independently verified 2026-09-01** against the real production config
(`max_sector_pct` 75 / `max_sector_hard_pct` 90, not a test sandbox number),
in `tests/test_sector_cap_unresolved_independent_verification.py` — a
separately-authored suite that does not import the fixtures above, to avoid
rubber-stamping a bug shared between the fix and its own tests. Confirms:
a pooled-Unknown book past the 90% ceiling is REFUSED, not merely logged; a
small isolated unresolved order well under the ceiling is warned but not
refused (same treatment a real sector gets at that size); the long/short
split (§12.2) applies to the Unknown pool exactly as it does to a real
sector, so an unresolved SHORT is judged against its own empty budget, not
an unrelated 85%-full unresolved LONG pool; and a broad-market index ETF
(SPY) resolves deterministically to `"Broad"` via the pre-existing
`_INDEX_ETFS` table and never enters the unresolved-sector path at all —
cash-park buys (SGOV/BIL) go further and never reach `RiskRuleEngine.check`
in the first place (`CashSweeper.split_positions` removes the parked
vehicle from `positions` before the sector block runs; `park_excess` calls
only `check_daily_loss`), so neither is at risk of the new hard block
freezing them.

**Pinned by tests**, in `tests/test_sector_dial.py` unless noted: a held short
no longer shrinks its sector's measured long exposure; each side is measured
independently; a pending short does not consume the long budget; the pair
trade is legal at both the constructor and the gate; an inverse-ETF long is
long-side; and one test holds a single book against the gate, the constructor,
`PMFacts` and the projection at once — three implementations drifting apart is
how this defect survived.

### 12.3 The sector limit rises from 40% to 75%

Owner chose 75% over the recommended 60%. The 40% figure is a
retirement-portfolio number and does not survive `docs/OUTCOME.md`'s
trading-desk framing: diversification is not a goal here.

**A sector limit's only remaining job is bounding correlated blow-up risk** —
several positions dying in one shock. 75% keeps that bound while permitting
genuine concentration in a hot sector.

**State the consequence honestly rather than burying it:** at 75% in one
sector, an ordinary 20% sector drawdown costs 15% of equity — three times the
ratified 5% per-trade risk unit, and it will trip the Phase 11.2 de-levering
ladder. (This line compared against the daily-loss breaker until 2026-09-20,
when that breaker was removed outright — retired board item 32.) That is
the accepted cost of a concentrated trading desk, not an oversight. Phase
10.3's scaling still applies underneath: crossing the target shrinks each
further position rather than refusing it.

**IMPLEMENTED 2026-09-01** (branch `worktree-agent-aa9554aa2b76e1b3f`).
`risk.max_sector_pct: 40 → 75` in `config/settings.yaml`, and moved everywhere
it was written down: three sites in `config/prompts/portfolio_manager.md` (the
caps-summary line, the momentum-sleeve clause, the example JSON), one line of
static prose in `config/prompts/risk_manager.md`, the text anchor in
`tests/test_prompts_anchors.py`, `docs/AGENT_ROLE_AUDIT.md`, and the sector
paragraph in `README.md`.

**Confirmed false positives, deliberately left alone:** the "40%-per-cluster
risk budget" in the PM prompt (that is `src/risk/budget.py`'s correlated
-cluster cap, a different mechanism), the "1.5 needs 40%, 2.0 needs 33%"
hit-rate arithmetic in both the PM and RM prompts, and `_PM_PROFILE_SYMBOL_CAP
= 40` in `src/pipeline.py`. Most `max_sector_pct=40` occurrences in tests are
self-contained fixture values passed straight into `RiskConfig(...)`; they do
not read production config and were left as they are.

**The prompt anchor was re-pointed, not retargeted.** The old anchor was the
bare string `"40%"`, which still appears in the PM prompt twice for those
unrelated mechanisms — so a naive `40% → 75%` swap would have left a green test
guarding nothing. The anchor is now `"75%"` plus a second anchor on
`"sector notional PER SIDE"`, so §12.2's split is guarded too.

**THE DIAL'S UPPER BOUND — NOT IN THIS RATIFIED TEXT, CHOSEN AT BUILD TIME AND
OPEN FOR THE OWNER TO MOVE.** §10.3 derived the absolute ceiling as 1.5x the
target (40 → 60). At a target of 75 that gives **112.5%, which is not a ceiling
at all**, and a dial with no terminal bound bounds nothing. Shipped:
**scaling begins at 75, hard refusal at 90** (`risk.max_sector_hard_pct: 90`,
with the derived default now capped at 90 by
`RiskConfig.SECTOR_HARD_CEILING_MAX` and never allowed below the target). 90
keeps a real ceiling while leaving 15 points of scaling range. The 40 → 60
relationship is unchanged below the cap. **The owner has not ratified 90.**

**The cost is stated where the decision is made, not only here.** The 75%/20%/
15%-of-equity/five-times-the-breaker arithmetic is written in plain language
into `config/prompts/portfolio_manager.md` (under "What good judgement looks
like here"), `config/settings.yaml` next to the setting, `src/risk/rules.py`
above the dial, and `README.md`. A consequence recorded only in a spec nobody
reads at decision time is not stated; a test asserts it is present in the PM
prompt.

**Pinned by tests** in `tests/test_sector_dial.py`: the shipped `risk:` block
really is 75/90 (parsed from `config/settings.yaml`, not a fixture); the
derived ceiling is capped at 90 rather than 1.5x; a position crossing 75 is
SCALED and one past 90 is REFUSED, at both the constructor and the gate.

**Pre-existing breakage repaired alongside, cause recorded because it is a
merge lesson.** 11 tests in `tests/test_sector_dial.py` were already failing on
`integration/ship-2026-09-01` before any §12.2/§12.3 work. Cause: that file's
analyst fixtures carry no `atr_14` / `computed_levels`, which was fine when
§10.3 was written because the constructor then used the model's
`reference_target`. §10.4 — merged the same day, from a different branch — made
the constructor DERIVE the target from structure and REFUSE without an ATR, so
every order those tests build was refused at `no_volatility_reading` before the
sector dial was ever reached. The dial was not broken; it was unreachable.
Neither branch's suite caught it because each passed in isolation. Fixtures
corrected to the post-§10.4 shape (matching
`tests/test_portfolio_constructor.py::_analysis`); no assertion was weakened.

### 12.4 Everything ships together, tonight

Owner instruction: implement and deploy all outstanding work in one pass rather
than staging it. **This is a deliberate acceptance of change risk**, taken
because the desk currently cannot trade at all and a partial fix leaves it that
way. The mitigation is not staging — it is the rehearsal rig, which must be run
against the fully merged result before deploy. See the session-start rule in
`docs/WORK.md`.

---

## Phase 13 — Every analyst reports the same way, weighted by measured skill, not equally (owner-ratified 2026-09-03)

**Owner's framing, verbatim in spirit: this is a trading desk, and analyst
agents are not interchangeable and must not be weighted equally.** He reached
this independently twice — first for combining signals in the PM's scoring
(see the composite-score work), now extended one layer earlier, to how each
specialist agent reports in the first place.

### 13.1 The problem this closes

Two structural defects were found reading the Portfolio Manager's actual
decision code line by line (not by testing prompt wording — by reading the
rule code itself, for the first time):

- **No tiebreaker among equally-eligible candidates.** Several stocks can all
  pass every rule; nothing ranks or compares them, so the effective choice
  falls to whatever the model defaults toward, not a real decision.
- **A "catalyst exception" checks that a citation exists, never that it
  supports the trade.** A stock can cite genuinely bad news and still pass —
  the code proves the news item exists and names the symbol, nothing more.
  **FIXED 2026-09-03 for this specific gate** (`docs/INCIDENT_HISTORY.md`):
  `StateChange` now carries a `symbol_direction` field, populated by the same
  news_analyst call that already produces `StockNewsItem.sentiment`, and
  `PortfolioManagerAgent._catalyst_cites_state_change` requires the cited
  row's direction for that symbol to support the trade (bullish for a long,
  bearish for a short) — missing or neutral no longer qualifies. This closes
  the specific hole named here; it is a narrower fix than §13.2's full
  standard-verdict-shape vision (which still applies to every seat, not just
  the state-change table this one gate reads).

Both are instances of the same root cause: analysts hand the PM free-form
prose instead of a checkable, comparable judgment, so nothing downstream can
verify substance, only the shape of a reference.

### 13.2 The fix: a standard verdict shape, from every seat, every time

Every specialist agent (technical, news, macro, earnings, smart money,
evening) outputs the same four things, in the same structured shape, on
every call:

1. **Direction** — bullish / bearish / neutral, with a magnitude, not just a
   label.
2. **Conviction** — how sure the agent is, separate from what it thinks.
3. **Evidence** — specific, checkable facts backing the call (a number, a
   dated event, a level) — not prose alone.
4. **Invalidation** — a stated condition that, if true, means the call was
   wrong. Every call must be falsifiable in a way code can eventually check.

This is what makes the PM's comparison across candidates a real comparison
instead of reading several different essays and picking one.

### 13.3 Weighting: a research-informed prior, refined by measured skill

Per the composite-score work above, corroboration only counts to the extent
signals are independent, and combining skill requires knowing each source's
actual reliability — never assumed. Each analyst's contribution is weighted
by ITS OWN reliability **for the specific type of judgment it is making**,
not a single trust level applied to everything it says.

**AMENDED 2026-09-03 (owner decision, ranking module only —
`src/verdicts.py`).** The original rule here was "start equal-weight, adjust
only on this desk's own out-of-sample proof." The owner's correction: "equal
until we have our own data" is functionally identical to "always equal" for
however long that data takes to accumulate, which contradicts the desk's own
repeated instruction that analysts are never interchangeable. The fix is not
to guess a weight — it is to use a REAL, externally published prior (a claim
about how the general TYPE of analysis performs across many markets and
decades, backed by real citations) rather than an unfounded blanket "start
flat" default, refined later by this desk's own measured record once it
exists. Five parallel literature reviews (2026-09-03, WebSearch-verified,
real papers not impression) found:

- **Technical (momentum) — strong, well-replicated.** Jegadeesh & Titman
  (1993), replicated internationally by Rouwenhorst (1998); decays over time
  as more capital trades on it (McLean & Pontiff 2016) and carries real
  crash risk (Daniel & Moskowitz 2016) — a real edge, not a permanent one.
- **Earnings (post-earnings drift) — strong, well-replicated.** Ball & Brown
  (1968), Bernard & Thomas (1989/1990); also documented as decaying in
  magnitude decade over decade, still present.
- **News/sentiment — real but narrow.** Tetlock (2007) is the foundational
  result; short-horizon (days), concentrated in small/illiquid names, and
  the literature specifically flags LLM-generated sentiment as looking
  strong in-sample and failing out-of-sample (Benhenda 2026) — directly
  relevant to an LLM news seat. Left at the unweighted baseline.
- **Smart money/insider — real edge, mostly gone by the time it's public.**
  Cohen, Malloy & Pomorski (2012) and a 2025 Finance Research Letters lag
  study both locate the edge in the pre-disclosure window; this desk only
  ever sees the disclosed, lagged version.
- **Macro timing — the specific capability asked of this seat is one of the
  most repeatedly debunked findings in applied macroeconomics.** IMF
  WP/18/39 studied 153 recession episodes across 63 countries and found
  professional forecasters missed the onset in the vast majority of them.
  "Regime matters for returns" is well supported; "this seat's specific
  turning-point calls are reliable" has no such support.

Applied (until deleted 2026-10-15; weights now come only from each seat's measured record, see OUTCOME desk archetype) as modest, bounded multipliers in `src/verdicts.py::SEAT_WEIGHT`:
technical 1.2, earnings 1.2, news 1.0, smart_money 0.8, macro 0.8. No source
handed over an exact cross-category ratio — only a real, sourced ranking —
so these are deliberately modest, not aggressive.

**2026-09-13 — what this weight multiplies was itself unsound for four of
the five seats, and has been corrected.** `score_verdict` is
`magnitude + conviction`. News, macro and smart_money were each DERIVING
`magnitude` from the same field they reported as `conviction` (news from its
own conviction, macro from its `confidence`, smart_money from the single
`economic_role` label that also set its conviction), at three different
unsourced spacings. Those seats therefore entered the composite with one
signal counted twice before this prior was applied at all. The three tables
are deleted; every seat but Technical now carries `NO_STATED_STRENGTH`
(`None` since 2026-09-26, item 65 — the literal `0.0` it used to be was a
point on a strength scale those seats do not have) for a directional call, so this prior weights exactly the seat's stated
conviction. Technical keeps a gradient because its rating rungs are a strength
it actually states, and earnings was already flat for exactly this reason.
Deleting invented weights, not replacing them. Detail:
`docs/INCIDENT_HISTORY.md`, 2026-09-13 (retired item 31).

**2026-09-13, same day, ON REVIEW BEFORE MERGE — two corrections to the
above, one of them load-bearing.** First, the flat magnitude was originally
0.5 — Technical's `buy` rung, borrowed by four seats that have no rungs. A
number read off another seat's scale is not read off this seat's instrument,
so it is now 0.0. Second and far more serious: **`rank_verdicts` aggregated
seats by weighted AVERAGE, which made a second, fully AGREEING seat LOWER a
candidate's score** (technical `strong_buy` at high conviction alone scored
2.0; adding smart_money's strongest possible agreeing verdict scored 1.8).
Agreement was dilutive, contradicting §0's edge and §9.4's "agreement earns
size". The aggregation is now a weighted SUM, which introduces no number,
deletes a divisor, and makes "an agreeing seat can only add" a property of the
arithmetic rather than a property to test for. The reward:risk tiebreak stays
a weighted mean: evidence adds, measurements average. Consequences (score no
longer capped at 2.0; coverage now moves the score, which `src/rotation.py`
compares on) are recorded as `docs/WORK.md` item 63 (item 62 closed 2026-09-14 — see
`docs/INCIDENT_HISTORY.md`, three prompt-only order-size ceilings). Detail:
`docs/INCIDENT_HISTORY.md`, 2026-09-13, the entry following retired item 31.

**What did NOT change: the sizing path.** `src/risk/rules.py::SEAT_WEIGHT`
— the §9.4 signed sum that actually prices position size — is untouched and
still requires the desk's OWN measured record (20+ resolved calls per seat,
`_CONVICTION_OUTCOME_MIN_N`) before any weight applies there, per the
2026-08-31 owner decision recorded in `src/conviction_ledger.py` and
mechanically enforced by `tests/test_signed_dissent.py`. That rule is about
a materially different claim — how good THIS desk's specific analyst prompts
are with real capital on the line — and a general literature prior cannot
stand in for it. This amendment applies only to `src/verdicts.py`'s
tiebreak-ordering among already-eligible, already-agreeing candidates, which
changes WHICH stock is picked among several, never how much is risked on it.
The two SEAT_WEIGHT constants are deliberately different types (a dict here,
an int there) so they can never be confused for each other.

### 13.4 What this does NOT authorize

- Does not remove or replace the deterministic reward:risk, stop-geometry, or
  risk-budget code — those stay Python, verified independently, exactly as
  today. This changes what analysts HAND UP, not what the deterministic layer
  does with real numbers it already computes itself.
- Does not invent a new risk threshold. If implementing this surfaces a place
  that needs a new numeric bar, that is a separate decision, flagged
  separately — same rule as everywhere else in this document.
- Does not require rewriting every agent at once. Land the verdict shape and
  wire it into the PM's comparison first; extend seat-by-seat.

See [[qamc-weighted-scoring-architecture]] (memory) for the full reasoning
chain and prior art this extends.

---

## Invariants (must hold at all times)

1. Alpaca **Paper** only. Live capital requires separate explicit authorization,
   and is mechanically gated by `src/live_capital_preflight.py` (board item 150) —
   the gate, not any prose checklist, is the source of truth for the conditions.
2. Deterministic Python risk and broker protections are **final authority and fail closed**.
3. Every position carries a **broker-resident stop** from the moment it is opened.
4. No single trade risks more than **5% of equity**; total at-risk never exceeds **25%**, correlation-adjusted.
5. `Specialists → Portfolio Manager → AI Risk → deterministic Python → broker` — for **exits as well as entries**.
6. No trade without analyst-supplied structural levels.
7. The system never blocks awaiting human approval.
8. Agents may propose changes to governance documents; only the owner accepts them.

---

## Execution order

```
Phase 0  ──▶  Phase 1  ──▶  Phase 2  ──▶  Phase 3
(gates)      (levels)     (sizing)      (exits)
                              │
                              ├──▶ Phase 4 (evidence + feeds)
                              ├──▶ Phase 5 (shorting)
                              ├──▶ Phase 6 (cost + transparency)
                              └──▶ Phase 7 (measurement)

Phase 8 (docs) — alongside, as each phase lands.
```

Phases 1 → 3 are the sequence that stops the bleeding. Phase 0 must precede all of it: none of this should be merged into a repository with no working test gate.
