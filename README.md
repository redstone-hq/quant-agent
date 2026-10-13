# quant-agent

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![CI](https://github.com/yebof/quant-agent/actions/workflows/test.yml/badge.svg)](https://github.com/yebof/quant-agent/actions/workflows/test.yml)
[![Last Commit](https://img.shields.io/github/last-commit/yebof/quant-agent?display_timestamp=committer&label=last%20commit)](https://github.com/yebof/quant-agent/commits/main)

LLM multi-agent quantitative trading system for US equities. Nine specialized daily agents — covering technical analysis, macroeconomic regimes, real-time news intelligence, SEC 10-Q/10-K filings, SEC Form 4 insider ("smart money") evidence, portfolio management, risk review, position management, and post-market reflection — coordinate through **schema-enforced reasoning chains**: every chain-of-thought step is a Pydantic `min_length=1` mandatory field, so the LLM cannot skip steps or fake the audit trail. A separate **quarterly Meta Reflector** reviews 90 days of accumulated outcomes (themes caught vs missed, loss patterns by attributable agent, signal activity, agent hit rates) and proposes append-only edits to six of those nine prompts under a 10-invariant safety system. Decisions execute via Alpaca with multi-layer risk controls — deterministic Python filters (a gross-exposure ceiling with a drawdown ladder, sector caps, correlation cluster) gate every order, and an LLM Risk Manager audits the Portfolio Manager's plan with veto power and per-symbol modifications before it reaches the broker. **Margin is enabled** (`risk.allow_margin: true`) with gross exposure bounded by `risk.max_gross_exposure_x`; this is not a cash-only book.

> ⚠️ **Disclaimer**: This software is provided **for educational and research purposes only**. It is NOT investment advice. Trading securities involves substantial risk of loss; you can lose more than your initial deposit. Past performance — including any backtest, simulation, paper-trading result, or live result observed in this repository — does not guarantee future performance. The authors and contributors make no representation that any strategy, signal, or system implemented here will achieve any particular result, and no representation that any code path is correct, fit for purpose, or free from defects.
>
> By using this software, **you accept full responsibility for any trades it places**, including against real-money accounts. Default configuration points at Alpaca paper trading; switching to a live account is your decision, and the consequences are yours alone. The MIT license below disclaims all warranties — read it. Do not run this against capital you cannot afford to lose.

## Highlights

- **Multi-agent coordination, not just orchestration.** Each agent has a single specialty and a strict output contract. The Portfolio Manager synthesizes upstream signals through an **8-layer memory stack** (today's quantitative facts, 7-day portfolio narrative, 14-day active HIGH-conviction state changes, 45-day trade calibration bucketed by size, last-5 Risk-Manager verdicts for self-calibration, own recent decisions for flip-flop detection, and a projected book preview that flags sector concentration before any orders are placed) and emits Pydantic-validated `TargetPosition` intent. A deterministic `PortfolioConstructor` then translates intent + the Tech Analyst's structural stop/target + live broker prices into actual orders. Stops and targets must trace to chart structure the Tech Analyst identified — there is no ATR-multiple or flat-percentage fallback; a candidate missing either is rejected rather than traded.

- **Reflection that closes the loop.** Every evening grades that day's trades along two axes (`correct / premature / wrong` × `thesis_trajectory_at_sell` ∈ `strengthening / intact / weakening / broken`) and grades the previous day's outlook against the actual next-day return — **deterministically** computed by pairing predicted bias with realized P&L, not LLM self-grading. Bias hit rates over rolling 10 sessions feed forward into the next evening's prompt, so the system gets quieter when it has been wrong. Concrete loop documented in the commit history: when evening flagged `memory-pricing` and `rare-earth` as recurring missed themes, the next morning's Portfolio Manager bought MU and MP from those exact themes, and that night's grader marked both entries `correct`.

- **Quarterly self-evolution under 10-invariant safety.** A Meta Reflector runs at quarter-end with a 7-step CoT (`facts → multi-axis self-portrait → gap diagnosis → existing-prompt audit → proposal`) and proposes append-only learnings for six of the nine daily agents' prompt files (tech / news / macro / earnings / portfolio_manager / evening_analyst — the `MetaReflectionAgentName` Literal in `src/models.py` is the authoritative list). The Risk Manager, Position Reviewer and Smart Money Analyst are **schema-protected** — the `MetaReflectionAgentName` Literal in `models.py` doesn't include them, so even with `evolution.enabled=true` the reflector cannot touch the agents that encode hard discipline or that only relay deterministically validated filing facts. Edits ship under per-agent FIFO cap (oldest learning auto-evicts), Jaccard token-similarity dedup against existing entries, prohibited-word regex (`never / always / override / ignore all` directly conflict with hard-invariant wording), atomic file writes, an audit log of accepted and rejected attempts with reasons, and an optional git auto-commit so `git revert <sha>` is your one-shot rollback for an entire quarter of evolution.

- **Production-grade broker discipline.** Full SELL protection lifecycle: cancel existing protective stops, submit limit order, validate broker acceptance via explicit status check, wait for terminal status, then reprotect on the **actual filled quantity** — not the submitted one (a partial-fill failure mode that took several iterations to fully pin down). Orphaned protection-restore intents persist to a SQLite table and drain at every subsequent session entry, so a crash mid-flight cannot leave a position naked overnight. Daily P&L and reflection insights write atomically in a single transaction, so a crash between the two never leaves the next morning's PM reading inconsistent state.

- **Risk/Reward computed in Python, not trusted to the LLM.** `TechAnalysisResult.risk_reward` is a `@computed_field` derived from entry / stop / target geometry, and the number every downstream seat sees is re-measured by the constructor against the stop that will actually ship. **It is a signal, not a gate, and it depends on the setup type** (owner decision 2026-09-11): a `breakout` is never judged on reward:risk at any stage — there is no overhead level to measure against and the position is trailed, not exited at a target; a `range` trade's real ratio feeds the ranking and, below 1.5, caps the trade at starter size rather than refusing it. The only reward:risk refusal left is a range ratio that cannot be measured at all. Neither the Portfolio Manager nor the Risk Manager computes its own figure — both are handed the same Python number.

- **Hard-trigger exit gate.** Every `SELL` and `REDUCE` — not just a repeat trim on a symbol already touched today — is dropped by the executor unless the LLM's `reason` names a recognized trigger (`thesis_invalid_if`, HIGH bearish state-change reversal, adverse/material news, sector shock, bearish earnings / earnings miss / guidance cut, macro regime shift, stop hit). Soft signals like TARGET_BREACH alone, slowing pace, geopolitical noise, or valuation stretch do **not** qualify. The gate originally covered only a symbol's second sell-side action of the day — built to stop the mechanical loop that produced one 73% single-day AMZN cut on a still-strengthening name — but that left a position's *first* sale of the day, almost every sale, executing on soft reasoning unchecked; two of 2026-08-26's evening-graded `premature` exits (EPD, MRVL) were first sales that slipped through it. Widened 2026-08-27 to cover every exit. A blocked exit is logged as `exit_blocked_no_named_trigger` and the position holds, protected by its broker-resident stop. Enforced at both prompt + executor layers.

- **Timezone-resilient by construction.** A single `src/trading_calendar.py` module is the source of truth for ET session windows, fill timestamps, and date keys. The OS-level scheduler (systemd with six discrete timer units — one per session — on Linux, launchd plist on macOS) fires at correct US-market times regardless of the operator's host timezone — the wrapper checks ET wall clock at fire time and skips if outside the window, so the system runs correctly when the operator is in SGT, GMT, or PT. A bash test pins the wrapper's window table against the Python authoritative source.

- **Telegram session-status push (opt-in).** Every session emits a structured status message — orders, sizing, degraded-data flags, daily P&L, tomorrow's bias, or the exact exception trace on failure — to a Telegram chat you control. Set `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID` to enable; missing creds make the notifier a silent no-op and trading is unchanged. Per-mode noise policy hides the 14 silent `intra_check` ticks per day and pre-market `nothing_new` earnings polls while always surfacing emergency liquidations, hard-risk blocks, exceptions, and the substantive morning / midday / close / evening completions. The notifier is wired into `main.py`'s `finally` block so even a `SystemExit` from a wrapper kill still produces a push before the process exits — and HTTP failures to Telegram are swallowed so an outage on their side can never cascade into a trading failure.

- **Tested.** The full pytest suite pins every invariant, including regression tests for every fix in the public commit history — run `pytest tests/ -q` for the current count (a fixed number here has gone stale more than once). Per-entry isolation (one bad LLM sub-item must not drop the whole report) is standard across the agent seats — a discipline that surfaced after a single malformed `MissedOpportunity` entry took down a complete evening report; a new seat inherits the same pattern.

## Architecture

Six sessions per trading day (ET, Mon-Fri), driven by a 30-min OS-level
timer (systemd on Linux / launchd on macOS) that wraps each session
with `scripts/run_if_et_window.sh` for ET-window gating and a cross-mode
session lock. Each session has its own cadence + scope:

```
08:00-09:15  earnings_preprocess  (once/day, pre-market)
             └─ Earnings Analyst runs LLM on newly-filed 10-Q/10-K — the
                ONLY session that calls the earnings LLM. Writes analysis
                to disk + confirms filing. Hot sessions below only READ
                this cache.

09:30-12:00  morning              (once/day, main trading)
             ├─ Cancel stale entry orders, keep protective exits
             ├─ Force-delever — INERT while allow_margin is true (it returns
             │  immediately). It is the safety net for cash-only accounts:
             │  biggest-loser-first sells until cash >= 0. With margin on,
             │  the gross-exposure ceiling and its drawdown ladder are what
             │  bound the book instead.
             │
             ├─── Parallel fan-out ────────────────────────────────┐
             │  Macro     News Intel   Tech         Smart Money    │   Earnings
             │  (6-step)  (3-layer)    (5-step CoT) (SEC Form 4)   │   (read cache)
             │                                                     │
             ├─ Load L1-L8 memory + yesterday's evening insights
             ▼
          Portfolio Manager (7-step CoT; risk-based sizing; sees Portfolio
                             Risk + Correlation Clusters before deciding;
                             regime-adaptive cash floor; drift trim)
             │
             ├─ Symbol Guard (universe + analyst-coverage check)
             ├─ Opportunity-cost rotation (`execution.rotation_enabled`, ON) —
             │    may itself close one held position to fund the best new
             │    candidate, only when capital is constrained AND the held
             │    name has already broken its holding-discipline check
             ├─ Hard Risk Engine (per-BUY, leverage-adjusted)
             │    max_position / max_sector / max_total / max_gross_exposure /
             │    require_stop_loss — blocking
             │    + advisory: macro_exposure_deviation / correlation_cluster
             ├─ Risk Manager LLM (6-step CoT; scale_all_buys; per-symbol edits)
             ▼
          Execute: SELLs first → refresh cash → BUYs
                   (OTO bracket with broker-enforced stop; no hard TP)

09:30-16:00  intra_check          (EVERY 30-MIN TICK, no LLM, ~5 sec)
             └─ Reconcile fills, repair any position found without a live
                protective stop at the broker, run the bounded intraday
                opportunity scan. Carries NO account-level loss breaker: that
                whole mechanism was removed 2026-09-20 on the owner's
                instruction (docs/INCIDENT_HISTORY.md, retired item 32).
                Not subject to the once-per-day guard.

13:00-14:30  midday               (once/day, sell-only)
             ├─ Force-delever (same cash-only safety net; inert under margin)
             ├─ Ex-dividend stop adjustment for held names
             ▼
          Position Reviewer (6-step CoT, session_type="midday" = patient)
             │  Default: HOLD unless named thesis trigger fires
             │  Stops move by structure, not by a profit ladder: the
             │  deterministic trail (src/risk/trailing.py) ratchets on
             │  swing lows / chandelier / +1R breakeven; a reviewer
             │  TRAIL_STOP must clear the ratchet cooldown + ATR noise band
             │
             └─ SELL / REDUCE / TRAIL_STOP only on trigger (thesis_invalid_if
                / HIGH state_change reversal / adverse or material news /
                sector shock / bearish earnings / macro regime shift /
                stop hit). Price alone is never
                a trigger, and neither is a correlation breach — removed
                2026-09-13 because nothing in the desk can verify one.

15:30-16:00  close                (once/day, sell-only, 30-min window)
             └─ Same Position Reviewer, session_type="close" = act-on-trigger.
                17.5h until next intraday control — if a thesis trigger is
                firing, act NOW. But "near close" is never itself a trigger.
                Good stocks are meant to be held through the night.
                Window width ≥ the OS timer's 30-min tick interval so any
                phase of the tick lands inside it (PR #41 lesson — earlier
                25-min window let bad phases miss the close two days running).

20:00-22:00  evening              (once/day, post-market)
             ├─ Reconcile all submitted orders → terminal status
             ▼
          Evening Analyst (7-step CoT)
             │  previous_outlook_assessment — grade yesterday's call
             │  sell_grades / buy_grades — structured per-trade labels
             │  outlook_calibration meta-loop — own bias vs actual across
             │                                  ~10 sessions
             │  thesis_health_review — per-held 8w tech trajectory + news +
             │                         valuation + full 10-Q/10-K reasoning_chain
             │                         (via src/data/earnings_deep_dive) →
             │                         thesis_trajectory: strengthening / intact /
             │                         weakening / broken + loss_root_cause
             │  missed_opportunities — scan universe + Alpaca top_movers for
             │                         names we should have held (learn, don't chase)
             │  tomorrow_bias / conviction / key_risks — PM reads these at open
             │
             └─ Atomic write: daily_pnl + insights in one transaction.
```

See `AGENTS.md` — "Hard boundaries" and "Decisions ratified" — for the
locked-in invariants (SELL `allocation_pct` semantics, ET-everywhere
timezone, paper-only enforcement, etc.). The former `CLAUDE.md` root
contract this section used to point at was retired and no longer exists;
the cash-only default it recorded is also no longer in force (see
"Margin is ON" under Risk Management).

## Agents

| Agent | Role | Key Feature |
|-------|------|-------------|
| **Tech Analyst** | Batch technical analysis | 5-step CoT (trend / momentum / volatility / volume / S&R). **Structural levels computed in Python** (`src/data/levels.py`) from the full ~5-year price history (`trading.lookback_days: 1800`) — local-neighbourhood bad-print filtering, swing-pivot detection, zone clustering, distance/touch-count ranking (recency-weighted until 2026-09-02; a decay fit against our own bars found no age effect, docs/RESEARCH_FINDINGS.md §7) — and rendered as a formatted support/resistance block in the prompt; the LLM sees the last 40 raw bars for immediate context only (up from 20), never as the source of levels. For every actionable rating the model **must** return `support_levels`, `resistance_levels`, `setup_type` (`range`\|`breakout`), `expected_horizon_sessions`, `entry_price`, `stop_loss` and `reference_target` — a Pydantic validator rejects the result if any is missing. **No synthesized fallback**: `PortfolioConstructor` no longer invents a stop (`entry − 2×ATR` / `entry × 0.95`) or a target (`entry × (1 + 2×stop_gap_pct)`) when the analyst omits one — a candidate with no structural stop or target is rejected outright. **Deterministic market context** (`src/data/context.py`, new) — computed in Python from the same bars, not estimated by the LLM — rendered per symbol ahead of the levels block: relative strength vs a same-batch benchmark (SPY, else QQQ, else IWM — no separate fetch), returns over 1w/1m/3m/6m/12m, 52-week range position, ATR as a percentage of price plus its 1-year percentile and an expanding/contracting/stable volatility state, MA20/50/200 slopes (rising vs. falling, not just price-vs-MA), consolidation detection (requires both a narrow range **and** small net drift, so a slow trend isn't mistaken for a base), 20-day average dollar volume, 20-day up/down volume ratio, and unfilled price gaps. `TechAnalystAgent.build_user_message` also accepts an optional `days_to_earnings` map to flag an imminent report per symbol; `MarketDataProvider.get_next_earnings_date()` (`src/data/market.py`) can supply it, and as of 2026-08-31 that method is wired — but to the **Risk Manager's** Event Risk block (`src/data/event_calendar.py`), not to this seat; nothing passes `days_to_earnings` here yet. **Auto-computed `risk_reward`** (Python-calculated, not LLM-trusted) flows into PM sizing and RM veto logic. **Signal-age memory** (`data/tech/last_ratings.json`): prior rating surfaced to LLM as context; `signal_age_days` counted to spot stale setups — PM cuts allocation on 8+ day stale BUYs. **Valuation context** (yfinance trailing PE / forward PE / P/S) surfaced per symbol — LLM flags >40x forward PE or >15x P/S as stretched in `reasoning_chain.support_resistance`. Pre-filter thresholds normalized by ATR. Auto-chunks the batch when a single call would carry too many symbols (currently 25 per chunk, `src/agents/tech_analyst.py`), with a per-chunk retry that re-requests symbols a first response dropped. Cross-field validator: BUY stop must be below entry and target above entry, SELL the reverse. |
| **News Intelligence** | 3-layer news analysis | Layer 1: Persistent macro narrative. Layer 2: State change detection. Layer 3: Per-symbol alerts with conviction. Daily storage in `data/news/` |
| **Macro Analyst** | Regime assessment & sector guidance | 6-step CoT (vol / curve / monetary / inflation+labor+credit / cross-signal / sector). Inputs: VIX, 2Y/10Y yields, **DFF** (daily fed funds), **core & headline CPI**, **UNRATE**, **HY OAS**. Persists yesterday's regime → detects `regime_shift`. Cross-references News narrative via `alignment_with_news`. Emits bull/bear view-change triggers. |
| **Earnings Analyst** | SEC 10-Q/10-K analysis | Revenue, margins, cash flow, strategic direction, competitive positioning, strategic vs operational risks, strategy consistency across filings. `investment_implications` carries a 5-step `reasoning_chain` (fundamental_quality / growth_trajectory / strategic_risks / management_execution / valuation_context) — sentiment call is derivable from the numbers, not a vibe check. |
| **Smart Money Analyst** | SEC Form 4 insider evidence | Enabled by default (`smart_money.enabled: true`), wired into the morning fan-out (`src/agents/smart_money_analyst.py`, provider `src/data/smart_money.py`). **Python owns every fact**: it selects exact non-derivative open-market transaction codes `P` (purchase) and `S` (sale) from first-party, credentialless SEC data, and computes transaction value, transaction-to-acceptance lag, freshness, materiality, owner independence and independent-owner clustering. The LLM receives only the compact surviving evidence and returns a stance (`bullish/bearish/neutral/mixed`) plus an economic role (`actionable/confirmatory/contradictory/historical`); it may synthesize meaning but cannot author source facts or decide admission. A quiet or unchanged day costs **zero model tokens**. A fresh external `P` purchase clearing the higher external materiality threshold may be admitted to that run's research/PM allowlist only after deterministic Alpaca tradability, exchange, minimum-price, minimum-history, 20-session dollar-liquidity and known-sector checks — at most `max_external_candidates` symbols per run, never written into the permanent universe, and still subject to Technical analysis, PM grounding, AI Risk and every deterministic risk rule. Thresholds, lookback and rate limits all live under `smart_money:` in `config/settings.yaml`. 13D/13G and 13F deltas are explicitly out of scope for v1; no paid alternative-data dependency is authorized. |
| **Portfolio Manager** | Central decision maker | Mandatory 7-step reasoning chain + continuity check across **8 memory layers**: L1 today's signals, L2 per-position entry context + Tech rating 7-day trajectory with `Weight:%` and `⚠️DRIFT` flag on concentrated winners, L3a rolling Portfolio Narrative (7 evenings), L3b Macro Regime Trajectory (7 days), L3c Active HIGH-conviction state_changes (14 days), **L4 Trade Calibration** — actual realized win rate + avg return on closed BUYs (45d), bucketed by size, **L5 RM Verdicts** (last 5 sessions — PM shrinks sizing when RM keeps scaling it down), **L6 Own Recent Decisions** (last 3 sessions — spot flip-flops), **L7 Projected Book Preview** — if you rubber-stamp all TA BUYs at their base risk allocation, flags sectors approaching the `risk.max_sector_pct` target. Today's Quantitative Facts also carry **Portfolio Risk** (budget/open risk in $ and % of equity, headroom under the 25% at-risk ceiling, per-position R-multiple and released/unprotected state — `src/risk/metrics.py`) and **Correlation Clusters** (measured transitive groups at |r| ≥ 0.7, computed before PM decides — `src/data/correlation.py::correlation_clusters`), so sizing and diversification are judged against real numbers rather than eyeballed tickers. **Conviction is expressed as risk, not notional** (spec §2.1): the PM emits `risk_allocation_pct` (the share of equity an idea may LOSE if stopped, floored at `risk.min_position_risk_pct` and capped at `risk.max_position_risk_pct`), and `PortfolioConstructor._plan_risk_targets` derives the notional weight from it and the stop (`shares = (equity × risk_pct / 100) ÷ |entry − stop|`) — a wider stop yields a smaller position rather than a rejected trade. `src/risk/budget.py::allocate_risk_budget` then rations that risk across the book as a live gate (spec §2.2): 25% total at-risk ceiling, no correlated cluster may take more than 40% of it, best-ranked candidate first (per `src/verdicts.py::rank_verdicts`, not the size of the request — that was the pre-2026-09-12 rule, item 49) with an alphabetical tie-break for unranked names, and a request rationed below the 0.5% floor is denied rather than shrunk to a token position. `target_weight_pct` is retained only as an Optional legacy field so stored PM decisions still replay through `src/replay.py` and the Mission Control API. **Agreement REFUSES, it no longer sizes** (spec §9.4, `src/risk/rules.py::agreement_refuses_trade`): the NET count of independent seats backing a target (technical / news / earnings / macro / smart_money, aligned minus opposed) is a yes/no gate — a net score at or below zero refuses the target outright and produces no order, while any positive net imposes no size restriction beyond the ratified `max_position_risk_pct` envelope. The graduated ceiling (`risk.agreement_ceiling_pct`, `ceiling(n) = max_position_risk_pct x sqrt(n / 5 seats)`) was **retired 2026-09-14 by owner decision**: the square-root law prices INDEPENDENT estimates, and these five seats read overlapping evidence (same tape, same bars, same filings) with several sharing one underlying model, so the archetype's one precondition is unmet. The key is now rejected on config load so a stale settings file cannot resurrect it. Agreement keeps its real job — it ORDERS which candidates get funded first, via `rank_verdicts` and `allocate_risk_budget`'s `priority`. Reward:risk is a sizing input by setup type, not a universal multiplier (see the R/R highlight above). **Cash floor is soft, not a fixed table**: Macro Analyst emits a free-form `cash_recommendation_pct` each session (no hardcoded risk-off/transitional/risk-on percentages exist in prompt or code); PM reasons `cash_target` from that number and the current regime, not from a fixed schedule. **Drawdown-aware, but nothing is gated on it**: PM is shown the rolling 5d/20d returns and reasons about them in `sizing_logic`. The deterministic halving of every new BUY that used to hang off an `in_drawdown` flag was removed 2026-09-20 on the owner's instruction (retired item 32). **Drift trim**: Weight > 12% + P&L > 10% → must trim or justify; Weight > 18% → hard trim (weight-based, unaffected by risk-based sizing). **Unread-filing REFUSAL** (board item 186, 2026-10-01): a just-filed 10-Q that reached the session with no analysis is MISSING EVIDENCE, not a seat verdict — the `queued` flag is set only for filings the pre-market preprocess failed to analyse — and the deterministic Python belt (`pipeline._refuse_queued_earnings_buys`) refuses the BUY outright rather than buying into the gap. It carries its own reason prefix, deliberately NOT the conviction bar's, which rotation string-matches on held names. It replaced a 5%-of-book weight cap whose 5 had no source; entries only, nothing already held is sold or reclassified. The prompt's separate hand-typed 1% *risk* ceiling was deleted 2026-09-14 (item 62) as a prompt-only number the belt already covers; the prompt now tells the PM only that such a name has no earnings seat and must be sized on the seats that remain. **Holding discipline** (tiered by days_held): <5d → default HOLD unless thesis_invalid_if or macro regime flipped today; 5-15d → standard; >15d profitable + trend intact → let it run. A **Rule Priority** cheat sheet in the prompt resolves conflicts (thesis_invalid > holding > earnings-cap > drift > cash-floor > ...). |
| **Risk Manager** | Trade review with veto power | Mandatory 6-step `reasoning_chain` (rr_audit / signal_fidelity / correlation_check / event_risk / sizing_sanity / overall) — vague approvals rejected. Reward:risk is judged by setup type, not against a universal floor (2026-09-11): a breakout's line reads `R/R n/a — BREAKOUT setup` and carries no reward:risk judgement; a range trade's real ratio is an input, and a thin one is already capped at starter size in Python, so it needs a second, named problem before RM cuts or refuses it. Sees raw Tech ratings + R/R + full macro context, plus the same **Portfolio Risk** block PM sized against (`heat`, `risk_ceiling_pct`) so `sizing_sanity` is judged in at-risk dollars, not notional weight. **`event_risk` is answered from fetched data, not recollection** (2026-08-31): an **Event Risk** block carries the next scheduled earnings date for every symbol under review — swept per symbol via `MarketDataProvider.get_next_earnings_date`, which until then had no callers anywhere — and the fetched FRED calendar of scheduled US macro releases (`src/data/event_calendar.py`). Every absence is NAMED rather than blank, in the `pace_status` style: `unavailable_no_fetched_date` / `unavailable_lookup_failed` / `unavailable_lookup_timeout` / `unavailable_deadline_exceeded` for earnings, a `MacroCoverage`-shaped coverage line for the calendar, and an explicit "not covered" list. **FOMC meeting dates are fetched too** — FRED has no meeting schedule, so they come from the Federal Reserve's own free calendar (`federalreserve.gov/json/calendar.json`, with the rendered `fomccalendars.htm` page as a fallback only when that feed's schedule stops before the horizon, which it does at every year boundary), disk-cached, and served with a provenance line: `measured`, `measured_from_stale_cache` with its age, or a named `unavailable_*`. "No FOMC meeting in this window" is printable only when the published schedule demonstrably spans that window. An unknown earnings date reads as unquantified event risk, never as none. Not the enforcer of any drawdown halving — that rule was retired outright on 2026-09-20 (item 32) — RM instead judges whether the proposed sizes are appropriate given recent returns. Can modify per-symbol fields OR apply portfolio-wide `scale_all_buys` (0.0-1.0). |
| **Position Reviewer** | Profit management & trailing-stop execution | Trailing-stop logic is **real**, not cosmetic — `TRAIL_STOP` action actually cancels the broker's old stop and submits a new one at the specified price via `AlpacaBroker.replace_stop_loss`. Sees VIX + HY OAS + core CPI to gauge whether to tighten stops broadly. Each position's metrics line leads with **R-multiple** — profit in units of the risk taken, measured per lot against the stop each lot was OPENED with (`src/risk/metrics.py::position_risk`), and whether that risk is `risk_released` (stop at/above entry) — read before `thesis_progress_pct`, which does not normalize for how much was risked. **Pace measures against a horizon pinned at entry, never the system's own behaviour**: `expected_horizon_sessions` and `setup_type` are persisted on the `trades` row at BUY time and never recomputed, so a `pace_status` of `measured` / `too_early` (< ⅓ of the pinned horizon elapsed) / `n/a_breakout` (Type B setups — progress and pace disabled by design) / `unavailable_no_pinned_horizon` (legacy positions) always traces to that fixed number — the prior `avg_hold_days` figure, drawn from the desk's own rolling realized-trade calibration, is gone; selling fast could no longer shrink the yardstick and make every survivor look stalled. The prompt renders why pace is absent in each non-`measured` case. **The take-profit is revisable, and progress is measured against the one pinned at entry.** A seat may raise a `target_revision_flag` (`src/models.py::TargetRevisionFlag` — symbol and evidence, deliberately NO price field) when the structure a target was measured against is gone; `src/risk/target_revision.py` then re-runs `derive_structural_target` on today's bars, reusing the entry price, the pinned horizon and the pinned setup type, and the code supplies the number. Only a STRUCTURAL EVENT legitimises it: the target's level closed through on two consecutive trading-day closes (the same confirmation gate and the same `NOISE_BAND_ATR_MULTIPLE` margin `check_structural_protection` uses), or today's ATR putting the stored target outside the derivation's own reach/noise bounds. A view that a name has further to run is refused as `REFUSAL_NO_STRUCTURAL_EVENT`; every flag is filed per symbol with its machine code whichever way it goes, and a refusal leaves the old target untouched. `trades.take_profit` is the live number and `trades.initial_take_profit` the pinned entry one, mirroring `stop_loss`/`initial_stop_loss` — and `thesis_progress_pct` and `pace` divide by the PINNED one. That is not cosmetic: the target is the denominator, so raising it lowers both metrics, both are in `exit_guard._HIGHER_IS_BETTER`, and a revision measured against the live target would land in `MetricDeltas.worsened`, clear `net_improved` and switch OFF the deterioration veto below — good news would unlock a "this position is stalling" SELL. **Nothing exits at a target, revised or not**; the trailing stop remains the only automatic exit (the automatic trim was deleted 2026-09-12). **The reviewer remembers its own prior read**: each review snapshots its per-position metrics (`db.save_position_review_metrics`, `specialist_evidence` kind `review_metrics`); the next review receives the deltas, and `src/risk/exit_guard.py` vetoes a SELL/REDUCE whose stated reason is a deterioration claim ("stalling", "not progressing", etc.) when every metric that moved since the prior review actually improved — logged as `exit_vetoed_contradicts_own_metrics`. Exits on genuinely new information (news, earnings, regime shift, a triggered `thesis_invalid_if`) are never vetoed; a mixed picture is deliberately left to the reviewer's judgment. Output is Pydantic `PositionReview` — action enum enforced (typos like `TRIAL_STOP` rejected); `TRAIL_STOP` requires `new_stop_price > 0`. **Hard-trigger exit gate**: every `SELL`/`REDUCE` must cite a hard trigger in its `reason` (`thesis_invalid_if`, HIGH bearish state-change, adverse/material news, sector shock, bearish earnings / earnings miss / guidance cut, macro regime shift, stop hit — *not* correlation breach, removed 2026-09-13 as unverifiable, see `docs/INCIDENT_HISTORY.md`) — the executor drops every non-matching reason, including a position's first exit of the day, and logs it as `exit_blocked_no_named_trigger`. Soft signals (TARGET_BREACH alone, slowing pace, geopolitical noise, valuation stretch) do NOT qualify — they were already priced into any earlier trim, and re-applying them is the mechanical loop that produced one 73 % single-day cut on a still-strengthening name (AMZN, 2026-05-04). That prior version of the rule only caught a symbol's *second* sell-side action of the day; it was widened 2026-08-27 to cover every exit after two of 2026-08-26's evening-graded `premature` exits (EPD, MRVL) turned out to be first sales that slipped through the narrower gate. The Python executor enforces this independently of the prompt; TRAIL_STOP and HOLD are unaffected. |
| **Evening Analyst** | Daily P&L review & multi-layer learning | Pydantic `EveningReport` with mandatory **7-step** `EveningReasoningChain`. **Single-day outlook retrospection** — grades yesterday's `tomorrow_outlook` against today's actual. **Multi-day calibration meta-loop** — its own tomorrow_bias / tomorrow_conviction hit rate over ~10 sessions (deterministic mirror); can't self-delude. **Structured trade grading** — `sell_grades` / `buy_grades` with `correct/premature/wrong` per trade; `buy_grades` also carries `thesis_trajectory` + `loss_root_cause` so "bought expensive vs fundamentals broke" is distinguishable. **Thesis Health Review (value-investor lens)** — per held position surfaces 8-week tech rating trajectory, 8-week news events, valuation bucket (cheap / fair / stretched), AND the full 5-step fundamentals reasoning_chain from the latest 10-Q/10-K (loaded via `src/data/earnings_deep_dive.py` from `data/earnings/{SYMBOL}/analysis_*.md`, truncated to 500c for primary / 300c for risk+mgmt steps). Outputs `thesis_trajectory` ∈ {strengthening/intact/weakening/broken}. **Missed Opportunities** — universe + Alpaca top-movers scan with quality filters (liquidity, volume confirm, valuation) surfacing names we should have held (learn, don't chase). **Quarterly meta-reflector** — separate agent runs at quarter boundaries with a **7-step facts→portrait→gap→prompt-audit→proposal** CoT. The digest now also carries `agent_prompts_snapshot` (compressed persona + rules + memory + `## Learnings (system-evolved)` section for all 6 editable agents), so step 6 `existing_prompt_audit` grounds proposed edits in what's already in each target prompt rather than rediscovering rules from memory. Auto-evolves prompts under 10 invariants enforced by `src/evolution/prompt_editor.py` (allow-list, append-only, FIFO cap, per-cycle agent cap, Jaccard dedup, prohibited-word regex, length bounds, atomic file write, audit log, optional git commit + revert rollback). Plus 7-day portfolio narrative + 14-day HIGH state changes (same layers PM sees). Outputs feed next morning's PM. |

## Risk Management

### Hard Risk Engine (non-negotiable, per-BUY)
- Single position: `risk.max_position_pct` (gross exposure — SQQQ 3x, SDS 2x counted at full magnitude). **Currently 65**, and it is deliberately not the operative per-trade limit: `risk.max_position_risk_pct` (5) is. This notional cap exists for the one failure mode every other number on this desk ignores — the loss when the stop does **not** fill (overnight gap, halt, fraud, regulatory action). It moved 20 → 100 → 33 → 65 across 2026-09-04..11; the settings.yaml comment above it carries the full derivation and should be read before quoting a figure.
- Total **net** exposure: `risk.max_total_position_pct` (currently 200). This is **not** a ceiling in its own right any more — at 200 it equals `max_gross_exposure_x: 2.0`, and since net can never exceed gross it is non-binding by construction. It remains in `HARD_BLOCK_RULES` as a backstop against a gross ladder that fails open. Before 2026-09-02 it sat at 90 and silently overrode the ladder, holding the real ceiling at 0.90x.
- Daily loss: **there is no account-level daily-loss limit.** A volatility-relative circuit breaker sat here until 2026-09-20, when the owner removed the whole account-level loss-alarm mechanism on the grounds that a normal market fluctuation could halt the desk and that per-position stops are enough (`docs/INCIDENT_HISTORY.md`, retired item 32). Loss protection is the broker-resident stop on each position; the §11.2 de-levering ladder below remains as the only account-level drawdown response.
- Sector concentration (gross, cumulative including pending same-sector orders) — **a dial, not a gate** (spec §10.3), with **separate long and short budgets** (spec §12.2) measured against a **75% target** (spec §12.3). All three owner-ratified 2026-09-01.
  - **Separate sides.** Long sector exposure and short sector exposure are tracked independently, each as an unsigned gross magnitude against the same limit; neither offsets the other. "A long and a short in the same sector is not a hedge — we are trading opportunities." So long the leader and short the laggard in one hot sector is a legal pair, and opening a short buys no room for more longs. (Before §12.2 the sum used SIGNED `market_value`, so a held short made its sector look smaller and the book could over-concentrate unseen.)
  - **75% is the target, not a veto.** Crossing it SHRINKS the next trade instead of refusing it: `PortfolioConstructor` scales by `(90 − side%) / (90 − 75)`, so a sector side at 80% takes the next idea at two-thirds size and one at 85% at a third. Breaching 75% emits an *advisory* `max_sector_pct` violation the Risk Manager sees.
  - **90% is the hard block** (`max_sector_hard_pct`, in `HARD_BLOCK_RULES`) — a dial with no terminal bound bounds nothing. **The cost of 75%, stated plainly: an ordinary 20% sector-wide drawdown then costs 15% of equity, three times the ratified 5% per-trade risk unit.** That is the accepted price of a concentrated trading desk (`docs/OUTCOME.md`), not an oversight. The 90 is not in the ratified §12.3 text and is open for the owner to move.
  - A trade shrunk below `cash_sweep.min_order_usd` ($500) is refused outright rather than placed as a token position: it would pay full commission and consume a slot for an immaterial payoff. Both thresholds are configurable (`risk.max_sector_pct` / `risk.max_sector_hard_pct`); the ceiling defaults to 1.5× the target, capped at 90, when unset.
- Stop loss required
- **Gross-exposure ceiling** (blocking, `max_gross_exposure`, spec §11.2, 2026-09-01): gross exposure — long market value **plus the absolute value of shorts**, leverage-adjusted, with the cash-park vehicle excluded because parked T-bills are cash and not a position — may not exceed `risk.max_gross_exposure_x` (2.0) times equity. Deliberately distinct from `max_total_position_pct` above, which bounds **net** exposure where a hedge cancels a long, and from `max_gross_bearish_pct`, which bounds only the bearish side: neither answers "how much does the book own", which is the question a margin call asks. Enforced at **both** the sizing gate (`PortfolioConstructor` shrinks an entry to fit, and refuses one whose remnant falls under `min_order_usd`) and the execution gate (hard block for anything that reached the engine without that sizing). The ceiling **steps down automatically on peak-to-trough drawdown** — 2.0x / 1.5x below −8% / 1.0x below −15% / 0.5x below −20%, with the owner alerted at the last rung — and it **blocks new exposure first, trimming the held book only if that book alone is still over**, so a drawdown does not trigger the panic-selling the ladder exists to prevent. The ceiling is resolved from **account state only**, in the session preamble before any agent runs, so a blank or truncated Portfolio Manager response cannot leave the desk levered through a drawdown. `src/risk/rules.py::resolve_gross_ceiling` / `apply_gross_ceiling`; **distance to forced liquidation** (~33% at 2.0x) is reported on the session alert. It is deliberately NOT on `/account`: Mission Control may not import the risk stack (`tests/test_api_safety.py`), and a second implementation of the same arithmetic would drift from the gate. `/account` reports only the standing cap (`risk_limits.max_gross_exposure_x`). **Non-finite equity read** (2026-09-02): a NaN/inf current-equity read (Alpaca has been observed to return NaN `portfolio_value` on market-open glitches) is not treated as "unmeasurable drawdown, hold the standing cap" the way a genuinely fresh account with no equity history is — `_resolve_gross_ceiling` forces the ladder to its floor rung and alerts the owner (`rung="bad_read"`) instead, since halting new risk is safer than silently assuming zero drawdown.
- **Margin is ON** (`risk.allow_margin: true` in settings.yaml, flipped 2026-09-02) — the desk is **not** cash-only. What bounds it is the gross-exposure ceiling and its drawdown ladder above, not a cash floor. The `cash_only` filter still exists and still blocks a BUY from driving cash negative when `allow_margin` is `false`, pre-summing same-session SELL proceeds so legitimate rotations pass; when cash goes negative under that setting, PM + midday prompts get a mandatory **DE-LEVER** directive and `_force_delever` sells biggest-loser-first until cash is non-negative. **Both are inert as configured** — `_force_delever` returns immediately when `allow_margin` is true. Set `allow_margin: false` to return to a cash-only book — and note that several other numbers in `config/settings.yaml` (the single-name cap in particular) were re-derived on the assumption that margin is on.
- Inverse ETFs (SH, SDS, PSQ, SQQQ) carry signed multipliers for net exposure and gross magnitude for sizing/sector caps
- **Advisory**: if projected net exposure deviates > 15pp from Macro's `target_invested_pct`, emits a non-blocking `macro_exposure_deviation` violation — RiskManager sees it and can respond with `scale_all_buys`
- **Correlation cluster** (advisory): a proposed BUY plus already-held positions correlated > 0.7 with it (120-day daily returns) must not together exceed 50% of book. Catches AI / mega-cap-growth concentration that sector caps miss when yfinance tags NVDA (Technology) and GOOGL (Communication Services) separately.
- **Correlation coverage gap** (advisory): when yfinance data is too sparse to build the matrix but the book has ≥ 2 positions, RM sees a `correlation_coverage_gap` advisory and can respond with `scale_all_buys < 1.0`. Prevents the cluster check from silently disabling when upstream data degrades.
- **No account-level loss brake.** A `drawdown_buy_cap` hard rule and a 5d/20d rolling-return halving of every new BUY used to sit here, alongside a volatility-relative daily-loss circuit breaker. All of it was removed 2026-09-20 on the owner's direct instruction (`docs/INCIDENT_HISTORY.md`, retired item 32): per-position stops are the desk's loss protection, and the §11.2 peak-to-trough de-levering ladder above is the only account-level drawdown response that remains. The correlation matrix behind the cluster check above is built once per run, on the Decision stage side, and shown to the Portfolio Manager as measured clusters *before* it decides (`src/data/correlation.py::correlation_clusters`) — not just checked against afterward.

### Portfolio Risk (deterministic, and now a live gate)

`src/risk/metrics.py` computes, per position and rolled up for the book: **budget risk** (`qty × max(0, entry − stop)` — what a position costs against its own cost basis if stopped out; drops to zero once a trailing stop reaches entry, i.e. that position's risk is "released" and no longer consumes budget) and **open risk** (`qty × max(0, current_price − stop)` — what the book loses from *today's* price if every stop fired at once). A position with no stop is charged at full notional, not zero — scoring it zero would rank the riskiest book as the safest; the cash-sweep vehicle is excluded rather than counted as unprotected. Rendered to both PM and RM via `format_heat_block()` with headroom under `risk.max_portfolio_risk_pct` (`config/settings.yaml`; 25% of equity at the time of writing).

As of remediation-spec Phase 2b, that ceiling is enforced, not just reported: the PM sizes conviction as `risk_allocation_pct` (the share of equity it may LOSE if stopped, not a share of portfolio weight, bounded by `risk.min_position_risk_pct` / `risk.max_position_risk_pct`; §9.4 agreement can refuse the target outright but no longer narrows it) rather than a notional weight, and `PortfolioConstructor` derives the position size from that and the stop distance (`shares = (equity × risk_pct / 100) ÷ |entry − stop|`, spec §2.1). `src/risk/budget.py::allocate_risk_budget` then rations the total request against the same `max_portfolio_risk_pct` ceiling shown above, with no correlated cluster (by measured return correlation) permitted to consume more than 40% of it (spec §2.2). The gate engages only when the caller supplies the book's current risk and clusters (`pipeline_stages._book_risk_inputs`, fed from `ctx.facts`) — an unmeasurable book falls back to per-position sizing and the single-name risk cap only, rather than either blocking every trade or waving all of them through. There is deliberately no fixed position-count target (spec §2.4); the book's size falls out of the budget as stops move to breakeven and release risk.

### Execution Safety
- Stale orders cancelled before each session
- SELLs execute before BUYs (free cash first)
- SELL uses limit price (0.5% below market for slippage protection); midday emergency sells use a wider 1% buffer to ensure fill during cascades
- BUY limit price auto-raised to market if below (prevents unfilled orders)
- BUY attaches OTO stop-loss via Alpaca (broker-enforced)
- No take-profit of any kind — no bracket leg, and no automatic fixed-gain trim (the 30%/15% auto trim was deleted 2026-09-12). The trailing stop is the only exit rule; the reviewer may tighten it or exit on a named trigger
- Partial sell via `allocation_pct` (1–99 = partial, 100 = full exit; 0 is treated as a no-op)
- **Order-status gating**: every broker submission runs through `_order_accepted()` before the audit log is written — Alpaca error payloads (missing id, status rejected/expired/canceled) are refused so the trades table never records a phantom fill
- **No-price BUY skip**: if neither the broker nor in-memory OHLCV bars can sanity-check the LLM's `entry_price`, the BUY is skipped rather than submitted as a stale limit
- **Earnings-queued BUY cap**: pipeline hard-clamps any BUY on a symbol whose latest 10-Q/10-K is `queued` (fresh filing, LLM analysis still running) so the **resulting** position weight — existing weight plus the add, not the add alone — stays ≤ 5% notional, regardless of PM's conviction. The PM prompt separately asks for a 1% *risk* cap on the same case
- **Hard-trigger exit gate**: every REDUCE or SELL — the first exit of the day on a symbol included, not just a repeat trim — is blocked by the executor unless the LLM cites a hard trigger keyword in its `reason`, and logged as `exit_blocked_no_named_trigger` — see Position Reviewer row above for the full list. Prevents the mechanical loop where a soft flag (TARGET_BREACH, slowing pace, valuation stretch) keeps re-firing across midday + close on the same name
- **A stalled entry is repriced ONCE, then cancelled at the end of its session and escalated — never dropped silently** (2026-09-11, rebuilt 2026-09-12; `execution.repeg_enabled`, still OFF by default): a limit BUY that drifts out of reach used to be cancelled by price protection and that was the end of it — correct, but a silent lost opportunity (6 of 68 historical proposals). With the switch on, the order is let work for `repeg_poll_seconds`, then repriced **once**, straight to the slippage ceiling (`max_entry_slippage_bps` above the reference captured at submission — the price the entry was already approved to pay, and an absolute bound), using Alpaca's real **Replace Order** endpoint (`PATCH /v2/orders/{id}`) so it stays ONE order. One replace, not a ladder: Alpaca's own community practice for a fast market is a single decisive market-crossing replace, and every extra replace is another `pending_replace` window an order can get stuck in (`repeg_max_attempts` is gone; the config loader rejects it). The reprice is **gated on the exchange having acknowledged the order** — Alpaca rejects a replace on an order still `accepted`/`pending_new` (its own lifecycle statuses for "not yet at the venue"), which at the open can take seconds — read from the same order-status wait as the fill wait — bounded REST polling, since `execution.fill_stream_enabled` is off (owner, 2026-09-17: the `trade_updates` websocket never authenticated on this host and the code is now dormant); if it never gets there inside the window no replace is sent and the reason is recorded. An entry still unfilled when the session finishes is **cancelled, not left resting** until 16:00 ET: this desk runs as discrete scheduled sessions (`SESSION_WINDOWS`), each re-analysing from scratch, so an order outliving its session would be acting on analysis already replaced — the cancel is bound to that session boundary, not to an invented timeout — and the owner gets a **standalone Telegram alert** (`send_owner_alert`) naming the symbol, the prices tried, and that it was cancelled. A fill or a partial fill is silent. Nothing is auto-resubmitted — that is a deliberate open owner decision, not an omission (see the comment above `_alert_owner_entry_cancelled` in `src/pipeline_stages.py`).
- **Opportunity-cost rotation acts, it no longer only advises** (`execution.rotation_enabled: true`, enabled by the owner 2026-09-12; `src/rotation.py`). With it off the desk merely computes the comparison each morning and shows it to the PM. With it on, the desk will itself close **one** held position per morning session, and only when every one of these is true, each measured from the desk's own data and never from a model's opinion: the book's existing risk leaves less headroom than the minimum tradeable size (capital genuinely constrained); the held name fails the desk's own entry rules today, so the comparison is categorical rather than a rank wobble; its structural protection has **already** broken under the holding-discipline check (a position whose thesis is intact is never rotated out); it was not bought today and has nothing in flight; and the PM itself asked to BUY the best-ranked new candidate this session — the desk never invents the buy leg. The close is proposed as an ordinary zero-size PM target and goes through the same path as any PM-decided exit — constructor, hard risk rules, Risk Manager review (which can refuse it), the holding-discipline claim check, and the protected-sell discipline. Nothing is bypassed. Every rotation that executes sends a standalone Telegram alert naming what was sold, what it made room for, and the measured reason, plus a second alert if the buy leg then did not happen. **It has never executed against a live broker** — the desk has been paused since ~2026-09-03, so the first real rotation will also be its first end-to-end proof
- **Kill switch** (2026-09-02): `touch`-ing the file at `risk.kill_switch_path` (default `data/KILL_SWITCH`) halts every broker order submission — checked via `path.exists()` and nothing else (no parsing, so it cannot fail open on a malformed file) in `AlpacaBroker.submit_order` / `_submit_stop_limit_order` / `replace_entry_limit`, the three methods every entry, exit, cover and protective-stop order passes through regardless of caller. It is the one deterministic guard that ALSO blocks a risk-reducing order — every other hard-block rule and circuit breaker deliberately exempts SELL/COVER (`RiskRuleEngine.check`'s first statement; `apply_gross_ceiling`'s BUY/SHORT-only filter) so a bad account state can never trap a position, but the kill switch overrides that when ops needs everything stopped. A protective stop already resting at the broker from before the halt is untouched. `run_morning` / `run_position_review` / `run_intra_check` also check it up front so a halted run skips LLM work and sends exactly one `kill_switch_halted` alert via the existing Telegram path, rather than burning tokens to have every order refused at the end.

### LLM Risk Manager
- Mandatory 6-step `reasoning_chain`: rr_audit → signal_fidelity → correlation_check → event_risk → sizing_sanity → overall
- Judges reward:risk by setup type, not against a fixed floor (2026-09-11): no judgement at all on a breakout; on a range trade the real ratio is an input, and a thin one (already starter-capped in Python) is cut or refused only when a second, named problem exists
- Receives raw TechAnalyst signals + computed R/R to verify PM's fidelity (no silent contradictions)
- `event_risk` is grounded in a fetched **Event Risk** block (per-symbol next-earnings proximity + FRED macro release calendar), with every gap explicitly labelled — never answered from the model's memory
- Sees hard-engine advisories inline: `correlation_cluster`, `macro_exposure_deviation`, `data_degraded` — must address each in `reasoning_chain`
- Reviews correlation concentration and imminent event risk (earnings / FOMC proximity), and reads the reward:risk figure as a setup-type-conditional input rather than as a pass/fail test
- Sees the same `PortfolioHeat` object PM sized against (at-risk $/%, headroom, per-position R-multiple) — `sizing_sanity` is judged in at-risk dollars, not notional weight
- Can modify per-symbol fields (aliases: `target`→`take_profit`, `stop`→`stop_loss`) OR set portfolio-level `scale_all_buys` (0.0-1.0) to shrink all BUYs uniformly
- Modifications and scaling re-validated through the risk engine

## Setup

### Prerequisites
- Python 3.11+
- [Alpaca](https://alpaca.markets/) account (paper trading supported)
- [FRED](https://fred.stlouisfed.org/docs/api/api_key.html) API key
- **Two LLM keys, both required by the shipped config** — every `<agent>_provider` is pinned explicitly in `config/settings.yaml` (see the "Accepted model routing policy" comment above the `llm:` block), and the stock checkout is split across two providers, so `AppConfig._check_llm_provider_keys` will refuse to start without both:
  - [Google AI](https://aistudio.google.com/apikey) (`GOOGLE_API_KEY`) — the tech, news, macro, earnings and smart-money analysts, the evening analyst and the meta-reflector all route to `google` directly, and so does the cross-provider failover in the shipped config.
  - [OpenRouter](https://openrouter.ai/keys) (`OPENROUTER_API_KEY`) — the Portfolio Manager, Risk Manager and Position Reviewer seats.
- [Anthropic](https://console.anthropic.com/), [OpenAI](https://platform.openai.com/) and [DeepSeek](https://platform.deepseek.com/) keys are **optional** and unused by the shipped config — needed only if you point a seat, or the failover, at one of those providers. **The failover is configured, not hardcoded**: `llm.fallback_provider` / `llm.fallback_model` decide it process-wide (a 2026-08-31 owner decision), and `src/agents/base.py::_DEFAULT_FALLBACK_MODEL` is the default if they are unset. Note that once `<agent>_provider` is set it always wins, because an OpenRouter `vendor/model` id like `openai/gpt-5.5` cannot be told apart from a native id by prefix alone

### Install

```bash
git clone <repo-url> && cd quant-agent
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
```

**Merge-driver registration is automatic.** `.gitattributes` alone does not enable the `docsmerge` driver; every pytest run registers it for the clone (and its worktrees) via `tests/conftest.py`, and `python scripts/check_merge_drivers.py` fails if any `merge=<driver>` in `.gitattributes` is unregistered (`--install` fixes it).

**The number-ledger ratchet needs no setup and stores nothing.** It was a
hand-edited literal, then the sum of deltas in an append-only history file;
both were storage, and the sum was raised by appending a positive delta to
make a change green (2026-10-05). The ratchet is now DOWN-ONLY and computed:
the count of `status: arbitrary` rows in your `config/number_ledger.yaml`,
compared against the same count on `origin/main` at check time. It may fall
and may stay level; it may not rise, and there is no file to append to. Two
branches that each retire a different number collide nowhere, because neither
writes a count down.

**Register the board-document merge driver (one-time, per clone — not automatic).**
`docs/WORK.md`, `docs/board_notes/` and `docs/INCIDENT_HISTORY.md` are the
three files every parallel branch collides on, and `.gitattributes` names a
merge driver for them (`docsmerge`) — but a git merge driver is only wired up
by a per-clone `git config` entry, never by `.gitattributes` alone. Without
BOTH lines below your clone is not broken, just unaffected: conflicts in these
three files fall back to git's ordinary line-level merge, same as before this
existed. Run BOTH — setting only the `.name` line leaves git aborting the
merge outright with `fatal: custom merge driver docsmerge lacks command
line`, which is a half-registered driver rather than an unaffected clone
[reproduced 2026-09-23]. Run it once per clone:

```bash
git config merge.docsmerge.name   "item-aware doc conflict resolver"
git config merge.docsmerge.driver "scripts/git_merge_driver_docs.sh %O %A %B %P"
```

With it set, a conflicting merge on those three files runs
`scripts/resolve_doc_conflict.py` automatically — it merges by numbered item,
not by line, and **refuses to resolve** rather than guess when two branches
file different items under the same number or a merge would make a live item
vanish.

**Ratchet guards store nothing, so there is no baseline merge driver.** The
silent-swallow, `__new__`-pipeline and import-cycle/layering guards
each compare the working tree with `origin/main` at check time
(`scripts/silent_swallow_guard.py`,
`scripts/pipeline_new_guard.py`, `scripts/import_graph.py --check`,
docs/GUARDS_WITHOUT_STORED_STATE.md). `scripts/install_git_merge_drivers.sh`
registers the one remaining driver (`docsmerge`), once per clone.

**What a refusal leaves behind (changed 2026-09-23).** It leaves the document
with conflict markers around the parts it could not resolve, both sides
preserved, and the plain-English reason in a gitignored file beside it
(`docs/WORK.md.merge-refusal`). Until 2026-09-23 a refusal wrote nothing at
all, which sounds safe and is not: git hands the driver the worktree copy of
the file and reads the result back out of the same path, so writing nothing
left the branch's own stale copy sitting there — valid markdown, no marker,
and the incoming side's newest board entries simply absent. `git status` said
`UU`; nothing in the file did. Three merges hit it in one night and one
nearly committed a silent revert of other people's entries. Resolve the
markers by hand, then delete the reason file.

**Trust its refusal, but check its success.** On 2026-09-18 the resolver was
caught merging a real conflict, correctly handing back one sentence it judged
unsafe to merge automatically — and, in the same run, silently dropping a
whole unrelated item block while still reporting that it had succeeded. It
was only caught because the person running it happened to re-read their own
item back out of the file afterwards. Always do that: after any run that
reports success rather than a refusal, re-read the item you expect to be
there. See `docs/INCIDENT_HISTORY.md`, 2026-09-18, for the full defect.

### Configure

**Credentials: OneCLI is the source of truth, not `.env`** — see `docs/architecture/CREDENTIAL_DELIVERY_EVIDENCE.md` before touching any key.

1. Create `.env` (set `chmod 600` after — these are secrets):
```bash
cat > .env << 'EOF'
GOOGLE_API_KEY=...                     # required — 7 seats + the configured failover route to google
OPENROUTER_API_KEY=sk-or-...           # required — portfolio_manager / risk_manager / position_reviewer seats
ANTHROPIC_API_KEY=sk-ant-...           # optional — unused by the shipped config
OPENAI_API_KEY=sk-...                  # optional — only needed if an agent is switched to call OpenAI directly
DEEPSEEK_API_KEY=sk-...                # optional — only needed if an agent is switched to call DeepSeek directly
FRED_API_KEY=...
ALPACA_API_KEY=...
ALPACA_SECRET_KEY=...

# Optional: Telegram session-status push (see "Optional env vars" below)
# TELEGRAM_BOT_TOKEN=...
# TELEGRAM_CHAT_ID=...
EOF
chmod 600 .env
```

2. Edit `config/settings.yaml` — models per agent, risk parameters, trading universe, schedule. Routing is **per seat and explicit**, not one provider for everything: the tech / news / macro / earnings / smart-money analysts, the evening analyst and the meta-reflector are pinned to `google` and call Gemini directly; the Portfolio Manager and Risk Manager go through OpenRouter (`openai/gpt-5.5` via the cheaper `openai/flex` endpoint — see `portfolio_manager_provider_order` — and `qwen/qwen3-235b-a22b-2507` respectively), and so does the Position Reviewer. The "Accepted model routing policy" comment above the `llm:` block carries the measured per-seat benchmark behind each choice, and why PM and RM are kept independent on purpose; treat that comment as the source of truth for which model is where, since these move. To point a seat elsewhere, set both its `<agent>_model` and its `<agent>_provider` explicitly (`google` / `openrouter` / `anthropic` / `openai` / `deepseek`) — an OpenRouter `vendor/model` id and a native id can look identical, so once `provider` is set it always wins; only an unset `provider` falls back to prefix inference. The cross-provider failover is likewise configured, not hardcoded: `llm.fallback_provider` / `llm.fallback_model` set it process-wide for every seat.

### Optional env vars

- `QUANT_AGENT_MAX_RETRIES` (**default `2`** — `src/agents/base.py::_DEFAULT_MAX_RETRIES`; the docstrings in that module still work their examples through an older N=7 budget, so read the constant, not the prose) — base agent LLM-call retry budget, read from the environment at call time. Backoff is **exponential floor + full positive jitter**: each sleep is in `[2^attempt, 2*2^attempt)`, i.e. `[1,2)`, `[2,4)`, `[4,8)`, `[8,16)`, `[16,32)`, `[32,64)` as N grows. Jitter is the load-bearing piece — without it, every retry attempt fires at deterministic offsets and a 30s outage swallows all of them; with jitter, individual attempts spread over a wider window so at least one tends to land outside any short outage. Drop to 2-3 for fast tests, raise to 10 if your provider chronically misbehaves.

- `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID` — enable Telegram session-status push. If either is missing the notifier no-ops silently; trading runs unchanged. Set both to wire it up:

  1. Create a bot via [@BotFather](https://t.me/BotFather): `/newbot` → name → username → it hands you a token like `12345:ABC...`.
  2. Start a chat with your bot (send any message — `/start` is fine — so it can DM you back).
  3. Get your chat_id: `curl -s "https://api.telegram.org/bot<TOKEN>/getUpdates" | jq '.result[0].message.chat.id'`. For a group, add the bot to the group then send a message; chat_id is negative.
  4. Add to `.env`:
     ```
     TELEGRAM_BOT_TOKEN=12345:ABC...
     TELEGRAM_CHAT_ID=987654321
     ```
  5. Verify delivery without running a trading session: `.venv/bin/python scripts/telegram_test.py` (add `--dry-run` to check the config without sending). It reports each variable as SET / NOT SET — never its value — and says whether the creds came from `.env` or the shell. No broker, LLM or market-data call.
  6. (Optional) Set `TELEGRAM_DISABLED=1` to mute without removing the creds. The notifier accepts exactly `1` / `true` / `yes` (case-insensitive) — `0` and `false` leave pushes ON, so un-mute by removing the line.
  7. (Optional) Set `TELEGRAM_RISK_ONLY=1` to keep the channel open for money-at-risk alarms only — a position with no protective stop, a stop that failed to place/amend/cancel, a rejected or unfilled order that leaves risk on, an executed trade, the broker unreachable while positions are held, any liquidation or halt. Operational noise (paid-analysis cost-circuit suspensions, provider/research failures, data-quality warnings, scan crashes, routine hourly checks, session summaries) is dropped instead, and each drop is still written to `notifier_sends` with status `filtered`, so nothing becomes invisible. Classification is declared per send site (`category=`), never guessed from message text, and a send site with no category FAILS CLOSED — it is treated as money-at-risk and delivered. Independent of `TELEGRAM_DISABLED`; when both are set the hard mute wins and nothing is sent at all.

  **Per-mode noise policy** (so the operator gets signal, not noise):
  - `morning` / `midday` / `close` / `evening`: always notify on completion (status + run_id + orders + degraded-data flag + elapsed).
  - `earnings_preprocess`: notify only when filings were analyzed; silent on `nothing_new` / `market_holiday` / transient SEC `fetch_error`.
  - `intra_check`: silent on the 14 OK ticks per trading day; notifies loudly when the circuit breaker fires (`emergency_sold` / `hard_risk_block`).
  - `meta`: silent on `not_quarter_end`; notifies on actual reflection runs.
  - Any session that raises an exception: always notifies, regardless of mode policy.

  **Asking for the desk status on demand.** `python scripts/desk_status.py` sends one DESK CHECK message built by the *same* trader-feed formatter the scheduled top-of-hour message uses, from live broker equity and positions — read-only: no order, no model call, no database write, no session lock or once-per-day stamp, and `--dry-run` prints the message instead of sending it. The stop-coverage audit and the movers scan only run inside a scheduled session, so on demand they render "not available" with the reason rather than "OK".

  **Re-reading a past evening report.** `run_evening` writes its own output — the exact result dict the evening formatter is handed, plus the book as the `positions` table held it at that moment — to the `evening_reports` table, keyed by trading day with the producing run id alongside (same one-row-per-day convention as `daily_pnl` and `insights`; a same-day re-run replaces the row). `python scripts/desk_status.py --evening` renders the most recent stored night back through the *same* formatter the live evening push uses, and `--evening 2026-09-17` renders a specific one. It is a read-only database read: no broker call, no model call, no write, so reviewing last night's report — or showing someone what the report looks like — costs nothing and runs no part of the pipeline. A night that is not stored, or a row whose payload cannot be read, is reported as unavailable and nothing is sent; a stored row missing pieces names them in words under a NOT AVAILABLE heading. No absent figure is ever rendered as zero, as a default or as a placeholder. Note that `data/evening_replays/<date>.json` is a different thing — it is the LLM's *input* fixture for rehearsal, not the run's output.

  **Proving the channel still works.** `scripts/telegram_test.py` answers this on demand; `scripts/alert_heartbeat.py` answers it on a schedule and records the answer — see [Proving the alert channel is alive](#proving-the-alert-channel-is-alive).

### Production deployment

Either OS-level scheduler can drive the 6 sessions; both wrap `scripts/run_if_et_window.sh` so the actual ET-window / last-run / cross-mode-lock logic is shared.

> **Current operational state.** The paper desk is **running** as of 2026-09-15: the owner unpaused the six session timers on the box. `scripts/systemd/paused_units.yaml` is therefore empty — an empty list is the fully-running state, not a missing declaration. To pause again: `systemctl --user disable` the units and add them to that file in the same change. To unpause: `systemctl --user enable --now` and empty the list in the same change. Leaving a re-enabled unit on the paused list is itself flagged as drift (list and box disagreeing).

**Linux (systemd, recommended for headless server / Tailscale-reachable hosts)**

The repo ships **six discrete unit pairs**, one per session, at `scripts/systemd/quant-agent-<mode>.{service,timer}`. Install them, enable all six, and turn on user-lingering so they fire when the user isn't logged in:

```bash
cp scripts/systemd/quant-agent-*.{service,timer} ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now \
  quant-agent-earnings_preprocess.timer \
  quant-agent-morning.timer \
  quant-agent-intra_check.timer \
  quant-agent-midday.timer \
  quant-agent-close.timer \
  quant-agent-evening.timer
loginctl enable-linger "$USER"
```

> Until 2026-08-31 this section documented a templated `quant-agent@.service` / `quant-agent@.timer` pair and an install command built on it. **No such template ever existed** — not in the repo, not on the production box. The six session units had lived only in one machine's `~/.config/systemd/user` since 2026-08-10 and were never tracked, so the README described a mechanism nobody could install and nobody had noticed was fictional. They are now under version control, and `tests/test_systemd_units.py` fails CI if one goes missing again.

Every session timer is identical — `OnCalendar=*:0/30`, `Persistent=true` — because the schedule does **not** live in systemd. The timer just ticks every 30 minutes and `run_if_et_window.sh <mode>` decides whether this tick falls inside that session's ET window, keeping `src/trading_calendar.py` the single source of truth. `TimeoutStartSec=1260` on each service is the systemd safety net above the wrapper's own `timeout --kill-after=30 1200` (so the wrapper kills Python before systemd kills the wrapper). `Persistent=true` catches up after reboots.

**Mission Control** (`quant-agent-api.service`) is the one unit with no timer: it is a long-running daemon (`Type=simple`, `Restart=always`, bound to `127.0.0.1:8800`) enabled through `[Install] WantedBy=default.target`.

For an exceptional same-day engineering verification, an operator can rerun
the morning session inside its normal ET window with a recorded reason:

```bash
scripts/run_if_et_window.sh morning --operator-rerun "verify deployed fix"
```

This bypasses only the once-per-day marker. The weekday/window check,
cross-mode lock, timeout, paid-analysis circuit, Paper configuration, risk
chain, and broker protections remain in force. Each attempt is appended to
`~/.cache/quant-agent/operator-reruns.log`; this is an audit tool, not a second
schedule.

**macOS (launchd, legacy path — works but Sequoia has two extra hurdles)**

If you deploy via `scripts/install_plists.sh`, Sequoia adds:

1. **Full Disk Access for `/bin/bash`** — System Settings → Privacy & Security → Full Disk Access → `+` → ⌘+Shift+G → `/bin/bash` → enable. Without this, launchd-spawned bash can't read `.env` or the project files in `~/Documents/`. Symptom: `Operation not permitted` (errno 8) flooding `logs/launchd_*.log`.

2. **Plugged-in power on trading nights** — if the laptop hibernates (critical battery), launchd's `StartInterval` jobs don't fire and you'll lose the session. `sudo pmset -c sleep 0` keeps the Mac awake on AC.

If you only use the CLI modes (`python main.py --mode morning` etc.), neither of the above applies.

### Quarterly prompt auto-evolution

**Effective mode is `enabled × dry_run`, and this repo currently ships STAGE-ONLY** — `evolution.enabled: true` **and** `evolution.dry_run: true`. That means the meta-reflector writes its proposals to `proposed_edits.json` for human review and **touches no prompt file**. The three states:

| `enabled` | `dry_run` | Effect |
|---|---|---|
| `false` | — | OFF — observe only, nothing staged |
| `true` | `true` | **STAGE-ONLY (current)** — proposals written for review, no prompt file touched |
| `true` | `false` | LIVE-APPLY — writes prompt files + git commit |

Live auto-apply is deliberately off: every mechanical guardrail below is syntactic and none of them can tell a correct learning from a confidently-wrong one. Review `proposed_edits.json` by hand, then flip `dry_run` to `false` for that run and apply with `--mode meta --force`. With LIVE-APPLY on, the reflector:

- Runs only when you invoke `python main.py --mode meta` (no OS timer schedules this — manual trigger on the last trading day of each quarter)
- Appends proposed Learnings bullets to the 6 editable agent prompts (tech / news / macro / earnings / portfolio_manager / evening_analyst)
- **`risk_manager`, `position_reviewer` and `smart_money_analyst` are schema-protected** — the `MetaReflectionAgentName` literal in `src/models/` doesn't include them, so even in LIVE-APPLY the reflector can't touch them
- 10 invariants enforce safety (full list in `src/evolution/prompt_editor.py` module docstring). User-visible ones: per-agent **FIFO cap** (10 Learnings, oldest auto-evicted), **Jaccard dedup** (0.6 threshold), **prohibited-words regex** (never / always / override / ignore all — these directly conflict with hard-invariant wording in core prompts), **per-cycle agent cap** (max 3 distinct agents edited per quarterly run), **atomic file writes** (tmp + os.replace), **audit log** at `data/evolution/edits.jsonl` with both accepted and rejected attempts + reasons, and `auto_commit: true` so each quarter's edits land as one `chore(prompts):` commit — `git revert <sha>` is your one-shot rollback

Keep `dry_run: true` until you've eyeballed at least one quarterly `reflection.json` under `data/evolution/{period}/` and are comfortable with proposal quality.

## Usage

```bash
source .env

python main.py --mode morning    # Analyze + trade
python main.py --mode midday     # Position review + trailing stops
python main.py --mode evening    # PnL report + insights for tomorrow
python main.py --mode live       # APScheduler in-process (dev/legacy; production
                                 # uses systemd/launchd timers, not this)
python main.py --mode daily      # P&L history CSV -> Telegram document
                                 # (no LLM, no trading; full NAV/SPY/drawdown
                                 # history from Alpaca portfolio_history)
```

**Automated scheduling**: the production path is six discrete 30-min OS-level timers (systemd units `quant-agent-<mode>.timer` on Linux, launchd plist on macOS), one per session, that call `scripts/run_if_et_window.sh <mode>`. The wrapper checks the current **US/Eastern** wall clock against the target window, applies the cross-mode session lock (one heavy LLM session at a time, except `intra_check` which is exempt), and skips if the mode already ran today. Runs the right session at the right ET moment regardless of the host's timezone — handy when traveling. Windows (Mon-Fri ET, authoritative Python table at `src/trading_calendar.py` `SESSION_WINDOWS`, locked to the bash wrapper by `test_trading_calendar.py`):
- `earnings_preprocess` 08:00-09:15 ET — pre-market LLM analysis of fresh 10-Q/10-K filings
- `morning` 09:30-12:00 ET — research + trading
- `intra_check` 09:30-16:00 ET — every 30min tick; stateless circuit-breaker (no LLM)
- `midday` 13:00-14:30 ET — position review + real trailing stops (patient disposition)
- `close` 15:30-16:00 ET — position review (act-on-trigger; window ≥ 30-min OS-timer tick so it never misses)
- `evening` 20:00-22:00 ET — daily P&L + insights for next morning

The **daily P&L CSV export** (`--mode daily`) is scheduled separately — it is a pure data export (no LLM, no orders), so it skips the window/lock wrapper entirely. A fixed-time systemd timer fires it Mon-Fri 09:00 ET via `scripts/run_daily_export.sh` (sources `.env`, 300s timeout). Units are tracked at `scripts/systemd/quant-agent-daily.{service,timer}`; install with `cp scripts/systemd/quant-agent-daily.* ~/.config/systemd/user/ && systemctl --user daemon-reload && systemctl --user enable --now quant-agent-daily.timer`. The CSV replaced the P&L history text table that the evening push used to embed.

The **LLM price-cache refresh** runs on its own timer at **06:30 and 18:30 ET, seven days a week** — `scripts/systemd/quant-agent-pricing-refresh.{service,timer}`, via `scripts/run_pricing_refresh.sh` (sources `.env`, 120s timeout, passes `--force`). Install the same way: `cp scripts/systemd/quant-agent-pricing-refresh.* ~/.config/systemd/user/ && systemctl --user daemon-reload && systemctl --user enable --now quant-agent-pricing-refresh.timer`.

Seven days matters. `data/openrouter_pricing_cache.json` is an input to the mandatory cost circuit, not telemetry: past 24h freshness plus the configured grace window the circuit fails closed and suspends every paid call for the day. Until this timer existed the file was only ever refreshed by a paid session starting, so it aged precisely when no session was running to notice — over the weekend of 2026-08-30 it went stale unwatched, and only a manual refresh before Monday kept the desk from opening and trading nothing. The script checks its own work against the file rather than the return value, and pushes a Telegram alert (same `TelegramNotifier`, no new channel) the moment the cache passes its freshness window — while the whole grace window is still in hand, not after it runs out. Run it by hand with `python scripts/refresh_pricing.py --force`.

The **deploy-drift check** runs Mon-Fri 08:45 ET — `scripts/systemd/quant-agent-drift-check.{service,timer}`, via `scripts/run_drift_check.sh` (sources `.env`, 60s timeout). It compares the deployed HEAD against `origin/main` and alerts when the box is behind. Until 2026-08-31 its unit invoked the venv Python directly with no `.env`: the process had no `TELEGRAM_*`, the notifier disabled itself, and a drift alert would have gone to the journal and nowhere else. Every unit whose script can raise an alert now goes through a wrapper that sources `.env`, and `tests/test_alert_heartbeat.py` fails CI if a new one does not.

The **unit-drift check** runs **08:50 ET, seven days a week** — `scripts/systemd/quant-agent-unit-drift.{service,timer}`, via `scripts/run_unit_drift_check.sh` (sources `.env`, 60s timeout). It answers the half of the question the deploy-drift check cannot. That check compares **commits**, so it is satisfied the moment the checkout sits on `origin/main` — while the units systemd actually loaded are whatever was last `cp`'d into `~/.config/systemd/user`. Installing a unit is a copy; git never sees it. On 2026-08-31 the box ran 11 services and 10 timers while the repo tracked 6 services and 4 timers, and the one tracked copy anyone might have installed pointed at `/home/yebo/quant-agent` — the upstream fork's path, which exists on no QAMC machine.

It compares the deployed checkout's `scripts/systemd/` against the installed units byte for byte (comments included — deploys here are a literal `cp`, so a box copy whose rationale no longer matches the repo is one nobody can reason about) and reports four things:

| Bucket | Meaning |
|---|---|
| `untracked` | Installed on the box, absent from the repo. **The dangerous one** — a unit hand-added at 2am is invisible to every commit-based check, and unrebuildable if the box is lost. |
| `modified` | Installed and tracked, bytes differ. Someone edited the box copy, or a deploy copied only some files. |
| `undeployed` | Tracked but not installed — a new unit that never got its `cp`, or one deleted from the box by hand. |
| `not_enabled` | Installed and correct, but no `.wants` symlink. The unit exists and never fires. |

A `not_enabled` unit named in `scripts/systemd/paused_units.yaml` is reported separately as **deliberately paused** instead — it does not count as drift or trip the exit status. If that same unit turns out to be enabled anyway, the list and the box disagree and it IS reported as drift; a paused-list entry naming a unit not tracked in `scripts/systemd/` is reported as an error in the list itself. An empty list is the fully-running desk. See the operational-state note above and `scripts/systemd/paused_units.yaml`.

Compared against the **deployed checkout**, not `origin/main`, deliberately: that keeps the two checks disjoint so they never alarm twice for one condition. Right after a merge the deploy-drift check says "behind" and this one correctly says the installed units still match the checkout they came from. Exit 1 is a finding (`SuccessExitStatus=0 1`); exit 3 is an operator problem and is left to mark the unit failed. Install with `cp scripts/systemd/quant-agent-unit-drift.* ~/.config/systemd/user/ && systemctl --user daemon-reload && systemctl --user enable --now quant-agent-unit-drift.timer`. Run by hand with `scripts/run_unit_drift_check.sh --no-telegram`.

The **stored take-profit check** runs **09:05 ET, seven days a week** — `scripts/systemd/quant-agent-stored-target-check.{service,timer}`, via `scripts/run_stored_target_check.sh` (sources `.env`, 600s timeout; it is the only one of these checks that goes to the network for bars, which is why its timeout is an order of magnitude larger and why it fires last of the four). For every held position it re-derives the take-profit from that position's **own pinned entry, horizon and setup type** — only levels, ATR and coverage come from today's bars — and compares it to the number stored on the trade row. It writes nothing: the database is opened `mode=ro`, and correcting a live position record is not something a timer does unattended.

It reports two things and treats them differently. A stored target with a **structural level still standing between the entry and that target** is an error, because the target is the nearest wall and never the level past it; a stored target that merely **differs** from today's derivation is not, because the derivation reads today's bars and the desk deliberately does not re-derive on a price move. Exit 1 means the first kind was found — a finding about the book, not a unit failure (`SuccessExitStatus=0 1`). The Telegram message names the positions it can recompute with both numbers, and names separately the ones it **cannot**, saying so in as many words rather than going quiet or substituting a guessed number. Measured against the live book on 2026-09-30: 11 held positions, 3 aiming past a standing wall, 6.3s.

**It is deliberately not a CI check**, and `tests/test_stored_target_guard.py` fails if anyone makes it one. It is legitimately red today on real findings the desk cannot correct, and a permanently-red required check blocks every unrelated merge and is switched off inside a day. Install with `cp scripts/systemd/quant-agent-stored-target-check.* ~/.config/systemd/user/ && systemctl --user daemon-reload && systemctl --user enable --now quant-agent-stored-target-check.timer` — or just deploy, which does all three. Run by hand with `scripts/run_stored_target_check.sh --no-telegram`.

The repository half is `tests/test_systemd_units.py`: CI cannot see the box, so it gates the units instead — no foreign `/home/*` path, `WorkingDirectory` at the deploy root, every `ExecStart` naming a script that exists, every timer paired with its service and installable, every service reachable, no inline credentials, and the six session units plus the API pinned by name. Neither half subsumes the other: CI stops the repo regressing, the timer stops the box diverging.

### The desk status board (`/board`)

One page, on a phone, for the owner — who is trader-minded and is neither a developer nor a finance professional. It is served at `/board` by Mission Control and it is the only status surface he is expected to read.

**It records nothing.** `docs/WORK.md` is the single source of truth for what the desk is doing, and `scripts/status_board.py` reads it — the running order, the decisions waiting on the owner, what is parked, what is already resolved — every time the page is built. `docs/phases.yaml` supplies the mechanically checkable evidence rules, and every one of them is re-evaluated against the current tree on every build. Anything that cannot be established by machine renders as **unknown**, never as a guess and never as a fallback to the recorded claim. The output that matters is the disagreement: something recorded as finished whose evidence no longer holds is rendered loudly as **proof no longer holds**, and the script exits non-zero so the unit shows it as failed too. Every hand-maintained status document in this repo has gone stale expensively; this page exists so that the next one cannot.

**The prose it shows is written, not generated — and it lives in its own file.** A plain-language explanation, a real-world example and a recommendation cannot be derived from code, so somebody writes them, in `docs/board_notes/`, keyed to the item they describe by its **number and section** — never its title, which `docs/WORK.md` can reword at any time. `docs/board_notes/` is deliberately not part of `docs/WORK.md`: the backlog is agent-facing and mechanically capped at 100,000 bytes (see `tests/test_status_board.py::test_work_md_stays_under_a_hundred_thousand_bytes`) precisely because it must stay a short, current statement of what is being worked on, not a growing prose archive; the notes file is owner-facing, uncapped, and carries nothing that the board needs to know an item's number, title, status or ordering — that still comes from `docs/WORK.md` alone. Each entry sits under a heading naming the item ("## item 32", "## gate item 4", "## decision due 2026-09-16" — the same identifiers `QueueItem.ref` and `PendingDecision.ref` already show the owner), each field on its own line:

```markdown
**Plain language —** what this is, in words a trader understands.
**Example —** a concrete case that makes it tangible.
**The decision —** what the owner specifically has to rule on.
**Recommendation —** what we think he should do, stated as a recommendation.
```

Leading whitespace is fine, the `**` is optional, and the separator may be an em dash, a hyphen or a colon. A block runs until the next label, the next heading, or **a blank line** — one paragraph each. Nothing is mandatory and **nothing is invented**: an item nobody has written a plain-English version for — whether nothing has been written to `docs/board_notes/` for it yet, or `docs/WORK.md` names an item the notes file has never heard of — is shown with an explicit "not written for you yet" marker rather than being hidden, dropped, or auto-summarised into friendly-sounding text that means something the backlog never said. Prose is run through the same mechanical jargon detector as the phase summaries, so an explanation containing a file name, a PR number or a code identifier is marked as written for a developer — and still rendered in full, because an unreadable description beats none.

**Two accessibility constraints are requirements, not preferences.** No status, verdict or priority on the page is carried by hue: every one is spelled out in words, supported by left-border weight, position and type size. And the page is mobile-first — narrow layout by default, 44px tap targets, and the one wide element (the plan table) scrolling inside its own container so the page body never scrolls sideways.

**It rebuilds itself.** `quant-agent-status-board.path` watches `docs/WORK.md`, `docs/board_notes/`, `docs/phases.yaml` and `.git/HEAD` (the deploy signal on a detached-HEAD checkout) and triggers `quant-agent-status-board.service`. There is no timer; an hourly one was retired in favour of rebuilding when something that changes what the page *says* actually changes — and an edit to either the backlog or its prose file counts. The ledger is deliberately **not** watched — it is written on every fill, and watching it would be a rebuild storm.

**What is being built is read off GitHub, at request time.** `src/inflight.py` reads the repository's open pull requests over the public REST API — no `gh` login and no token on the trading account are needed for a public repository, though `GH_TOKEN`/`GITHUB_TOKEN` are honoured if present — and renders each as its title plus, in words, whether it is still being written, being tested, waiting to go in, or blocked. A pull request changing touches nothing the path unit watches, so the build-time copy would go stale between rebuilds; instead `/board` re-reads it on every request, using conditional (ETag) requests that GitHub does not count against the rate limit when nothing has changed. The section always carries the time it was read. When GitHub cannot be read it says **could not read what is in flight** — with the last successful read shown, dated, beneath it if the server has one — and never renders as an empty list, because "could not look" and "nothing in flight" are different facts. Finished and parked work is no longer a section the owner scrolls past: it sits behind one closed disclosure carrying only counts.

Install: `cp scripts/systemd/quant-agent-status-board.* ~/.config/systemd/user/ && systemctl --user daemon-reload && systemctl --user enable --now quant-agent-status-board.path`. Generate by hand with `python scripts/status_board.py --out data/board/index.html`, or point `--out` outside the repo for a preview. **`data/` is gitignored on purpose**: a machine-written file that git also tracked is how the deploy-drift problem of 2026-08-28 started, and regenerating the board must never dirty the production checkout.

**Freshness is a fact check, not a clock.** The build stamps the commit it was generated against into a `<meta>` tag; `src/api/server.py` reads it back at serve time, compares it against a freshly-read live commit, and shows a banner when they differ — or says the freshness is *unknown* when either cannot be read. It never says "fine" by default.

### Proving the alert channel is alive

Every alarm here is a Telegram message. Nothing used to check that a Telegram message could still be sent, so **"no alert arrived" meant both "nothing is wrong" and "the alarm is broken"** — a present-but-revoked token, a wrong chat id, a blocked bot, or a new egress rule are all invisible to a credentials check and fatal to a send.

**The sessions are the watchdog.** The desk already runs five to six sessions every weekday and every one of them ends by pushing a Telegram message, so every session now exercises the whole alert path end to end first — one real message with `disable_notification`, deleted immediately afterwards (`TelegramNotifier.probe`, reused not reimplemented) — and writes the verdict to SQLite. The thing that depends on the alarm is the thing that tests it: no new schedule, no new credential, no new dependency, no LLM call. On a weekday that is roughly twenty checks, one every half hour or so while the market is open. `src/alert_watchdog.py` owns the design and the reasoning.

**Where the answer appears when Telegram is the thing that is broken.** The verdict goes to the `alert_channel_checks` table, and Mission Control's `/health` renders it as a four-state `alert_channel`:

| status | meaning | Mission Control |
|---|---|---|
| `ok` | the last check sent a real message and it arrived | green |
| `broken` | the last check **failed** — alarms reach nobody | **red, `/health.status` = degraded** |
| `stale` | the last success is older than 26h — the checks themselves stopped | **red, degraded** |
| `unknown` | no check has ever been recorded (fresh DB, pre-upgrade) | amber, stated in words, **never "ok"** |

`TopStrip` ranks a broken alert channel directly under "database unreachable" and above everything else, because every other fault on that board is reported over the same Telegram path — a dead channel hides all of them.

**Silent on success.** A healthy channel produces zero messages. Only two things are ever said: the channel just failed its check (forced out even from sessions whose noise policy is normally silent — a probe can fail at a stage an ordinary send survives), and the channel just **recovered**, carrying how many checks failed and when it last worked. That recovery line is the one signal that reaches you without your having to go and look.

| Unit | When | What it adds |
|---|---|---|
| `quant-agent-alert-heartbeat.timer` | 06:15 ET, **every day including weekends** | The floor under the staleness threshold, not the primary mechanism. Sessions only run Mon-Fri, so without a weekend check "no successful check in 26h" would fire every Saturday and get switched off. It also covers the one thing sessions structurally cannot report: every session failing to start at all. |

Install: `cp scripts/systemd/quant-agent-alert-heartbeat.* ~/.config/systemd/user/ && systemctl --user daemon-reload && systemctl --user enable --now quant-agent-alert-heartbeat.timer`. Run by hand with `scripts/run_alert_heartbeat.sh`, or `python scripts/alert_heartbeat.py --status` (prints the record, sends nothing).

**Detection latency.** A weekday break is caught within about 30 minutes — the next `intra_check` tick. A break on a Friday evening, after the week's last session, is caught by the Saturday 06:15 ET probe, roughly 9 hours later; the Saturday, Sunday and Monday 06:15 ET probes all land before Monday's first session (08:00 ET earnings pre-process) and well before the open. Every figure is time-to-**recorded-and-rendered**, not time-to-push: on a fully dead channel nothing can push, and Mission Control plus the systemd journal are where the answer appears.

**The bootstrap limit, stated plainly.** Two failure modes, deliberately not conflated. **(A)** The channel is broken while the box is alive — revoked token, wrong chat id, blocked bot, egress rule, a unit that never sourced `.env`. This is the likely failure, it is fully detectable from inside the box, and it is what everything above covers. **(B)** The box itself is dead, off or unreachable. Nothing running on the box can report that, and no local engineering changes it. An out-of-band ping to an external monitoring service would cover (B); that dependency was **refused outright** — this desk does not rely on an outside service to know its own alarm works — so the hook was deleted rather than left switched off, and a test keeps it gone. **If the box dies, you find out when you next look.** That is not fixed here and nothing in this design quietly pretends otherwise.

### Resetting the desk (`scripts/desk_reset.py`)

While the trade logic is still being stabilised the book is wiped and restarted **daily**, so the wipe is a tool with guardrails rather than a sequence someone remembers at 07:00. It is **dry-run by default** — the plain invocation reads the broker and the database, prints every position, every open order and every table with its row count, and changes nothing:

```bash
python scripts/desk_reset.py                 # dry run: prints the plan, touches nothing
python scripts/desk_reset.py --execute       # actually do it
```

`--execute` is the confirmation flag. On a TTY it additionally asks you to type `reset`; scripted use must pass `--yes` explicitly (an `--execute` with no TTY and no `--yes` is refused, so a cron line can never half-confirm).

**It refuses to run against anything it cannot prove is a paper account.** Four independent signals, all of which must agree, because any one of them can be edited or bypassed on its own:

| Check | Catches |
|---|---|
| `alpaca.paper` is exactly `True` | the settings edit `AppConfig._enforce_paper_only` already guards — asserted again here so the tool stays safe if that guard is ever removed |
| `alpaca.base_url` names `paper-api.alpaca.markets` | a config that says paper and points elsewhere |
| the **resolved** SDK base URL is the paper host | a client built `paper=False`, or `url_override`'d past the config entirely |
| `account_number` carries Alpaca's `PA` prefix | the account's own answer. `GET /v2/account` has **no** paper/live field (verified against the reference, not assumed) — paper and live are separate hosts, not an account property — so the account number is the only account-level signal that exists |

Plus a fifth, read-only: a single `GET /v2/account` against the **live** host. Paper keys are rejected there; if these credentials *authenticate*, they are live credentials and the run aborts no matter what the config says. A network failure on that probe is inconclusive, so it warns and defers to the four above. `--no-live-probe` skips it.

Every one of those is a **hard refusal with no override flag**, for the same reason `AppConfig` has none: authorising a liquidation against a live account should be a reviewed code change, not a command-line argument.

**Timing.** It refuses while the market is open (`--allow-market-open`) and while one of the desk's own session windows is live (`--allow-session-window`) — two separate flags, because they are two separate risks. The trade-off is real and there is no free window:

- **Market closed** — the safe-looking option, and the default-legal one. But liquidation is a market order and market orders are not extended-hours eligible, so Alpaca **queues them for release at the next open** ([orders-at-alpaca](https://docs.alpaca.markets/us/docs/orders-at-alpaca)). The book reads "flat" only after that open, right on top of the 09:30 morning session. Keep the morning timer masked until the queued sells have filled.
- **Market open** — fills are immediate and real, but `intra_check` covers the whole 09:30-16:00 session, so the desk can trade against you mid-reset.

There is no window that is both "market open" and "no desk session active". The clean procedure is therefore: stop the desk's timers, run with `--allow-market-open` during regular hours, confirm flat, restart the timers. The tool prints this reasoning on every run rather than leaving it in a document.

It also warns — without touching them — when `data/checkpoints/` holds a post-PM checkpoint younger than `decision_checkpoint.MAX_AGE_MINUTES`. The zero-LLM resume lane will still re-offer that plan, and it was built on the pre-reset book. Only reachable if you reset inside the morning window with the override flags, but silently resuming a plan for positions that no longer exist is not a failure worth discovering live.

**What it does, in order:** prove paper → check timing → **back up** → flatten → clear. The backup is unconditional and has no `--no-backup`: `data/resets/<UTC timestamp>/` gets a consistent SQLite copy (online backup API, WAL-safe), `book_before.json` / `book_after.json`, and a `reset_manifest.json` recording the checks that passed, the plan, and the row counts deleted.

The flatten is `DELETE /v2/positions?cancel_orders=true` (`close_all_positions(cancel_orders=True)`) — cancelling **before** liquidating is required, since a resting protective stop reserves the shares and a naive sell is rejected for insufficient quantity. A second `cancel_orders()` sweeps anything that appeared in the gap.

**If the flatten does not land, the database is not cleared** (exit 5). The local ledger is the only thing linking a still-open position to its history, so wiping it after a failed liquidation would leave the broker holding positions nothing on the box can explain. That covers a broker error, and positions still open after the settle window *with the market open*. Positions still open with the market **closed** are the normal queued case and do not block the clear. The backup and both book snapshots are written either way.

Exit codes: `0` fine, `1` you declined the prompt, `2` bad invocation, `3` **not provably a paper account**, `4` refused on timing, `5` flatten failed so the database was left alone.

**There is no Alpaca API for resetting a paper account to its original funding.** The dashboard no longer resets accounts at all: it creates and deletes them, and a newly created account needs newly generated API keys ([paper-trading](https://docs.alpaca.markets/us/docs/paper-trading)). For this desk that is the expensive path, not the cheap one — the Alpaca credentials are injected by the OneCLI gateway (`docs/architecture/CREDENTIAL_DELIVERY_EVIDENCE.md`), so new keys mean an operator edit inside OneCLI, not a `.env` change `dev` can make. Sell-everything keeps the account number, the keys and the gateway wiring intact, which is why it is the implemented path.

## Trading Universe

101 symbols (source of truth: `config/settings.yaml:trading.universe`):
- **Index ETFs**: SPY, QQQ, IWM, DIA
- **Sector ETFs**: XLF, XLE, XLV, XLI, XLP, XLY, XLU, XLRE, XLB, SMH, DRAM
- **Inverse ETFs**: SH, SDS, PSQ, SQQQ (leverage-corrected in risk engine)
- **Individual stocks**: AAPL, MSFT, GOOGL, AMZN, NVDA, META, AVGO, JPM, CAT, plus ~80 single names across tech, energy / oil, infrastructure, consumer, healthcare, financials, and power-transition themes

**Screened additions (built 2026-09-19, OFF until `universe_screen.enabled` is set).** `src/universe_screen.py` screens the broker's active US-equity list incrementally at the end of each evening session (each symbol at most once a week) and keeps the names that pass in `data/universe/universe_state.json`. Criteria: a year of price history, $5 minimum price (SEC penny-stock line), estimated bid-ask spread inside the desk's entry-slippage belt, shortable and easy to borrow, not a warrant/unit/right/preferred/receipt/fund, no pending takeover, a volatility ceiling derived from the desk's own stop rules, $30M minimum company size (Russell index floor), and a resolved sector. No earnings-date requirement, no maximum price. An admitted name that fails one weekly screen is flagged, two consecutive is removed; delisted/halted is removed at once; a held name is never removed. Each morning session receives every held screened name plus at most `nominations.max_per_seat_per_run` others. Every change is logged (`UNIVERSE_CHANGE`), written to `specialist_evidence` (`agent_name = universe_screen`), and listed in the next morning Telegram message. The hand-typed list above is never screened or pruned. With the switch on, the SEC Form 4 and nomination side doors run the same screen, and a Form 4 purchase must have been disclosed within `risk.max_target_horizon_sessions` sessions.

## Project Structure

```
quant-agent/
├── main.py                        # CLI entry point
├── config/
│   ├── settings.yaml              # Models, risk params, universe, schedule
│   └── prompts/                   # System prompts for each agent
├── src/
│   ├── pipeline.py                # Orchestrator (morning/midday/close/evening/earnings_preprocess/intra_check/meta)
│   ├── pipeline_admission.py      # AdmissionService — who gets into the research universe (screen, nominations, Form-4 currency)
│   ├── pipeline_protection.py     # ProtectionMixin — stop coverage, repair, protected sells, WAL restore, reconcilers
│   ├── pipeline_prompt_facts.py   # PromptFactsMixin — read-only DB/broker reads turned into LLM prompt context
│   ├── pipeline_intraday.py       # IntradayMixin — intra-check session, scan locks, paid-scan slot, intraday opportunity scan
│   ├── pipeline_risk_gate.py      # RiskGate — risk-verdict application: hard-block filter, size/stop/target edits, budget reconcile
│   ├── pipeline_research_continuity.py  # ResearchContinuityMixin — change detectors, carry-forward, Form-4 backlog, seat healing
│   ├── pipeline_stages.py         # shared stage helpers; re-exports the four stage classes below
│   ├── pipeline_rotation_exec.py  # rotation EXECUTION — sell/buy legs, gates, projections (moved verbatim, item 210 step 12)
│   ├── pipeline_entry_orders.py   # entry order placement, re-peg, constructor-drop recording (moved verbatim, item 210 step 12)
│   ├── stage_morning_research.py # MorningResearchStage (moved verbatim, item 210 step 10)
│   ├── stage_decision.py         # DecisionStage (moved verbatim, item 210 step 10)
│   ├── stage_risk.py             # RiskStage + its 5 private helpers (moved verbatim)
│   ├── stage_execution.py        # ExecutionStage (moved verbatim, item 210 step 10)
│   ├── pipeline_context.py        # RunContext dataclass — explicit shared state across stages
│   ├── notifier/                  # Telegram session-status push (opt-in via env vars; per-mode noise policy)
│   ├── portfolio_constructor/     # Deterministic Target → TradeDecision translator (risk-budget sizing)
│   │   ├── __init__.py            # PortfolioConstructor core: targets, risk plan, sector dial; re-exports the whole package
│   │   ├── config.py              # ConstructorConfig, RiskPlan, every refusal/rule constant
│   │   ├── stops.py               # stop + reward-to-risk resolution mixin
│   │   └── orders.py              # buy/sell/short/cover leg builders mixin
│   ├── trading_calendar.py        # ET timezone + SESSION_WINDOWS + session_date_key (single source of truth)
│   ├── scheduler.py               # APScheduler — only used by --mode live (dev/legacy)
│                                  #   Production uses systemd timers (Linux) or launchd (macOS).
│   ├── config/                    # Pydantic config with API key validation (public name: src.config)
│   │   ├── __init__.py            # every *Config class, AppConfig, load_config; re-exports the rest
│   │   └── macro.py               # MacroConfig (FRED fetch resilience), lifted verbatim
│   ├── models/                    # Data models package (base, analysis, decisions, portfolio, risk_verdicts, macro, news, positions, earnings, smart_money, evening, meta)
│   ├── agents/                    # 9 daily LLM agents + 1 quarterly meta_reflector
│   ├── rotation.py                # Opportunity-cost rotation (execution.rotation_enabled)
│   ├── nominations.py             # Per-seat candidate nomination + capping
│   ├── verdicts.py                # Deterministic candidate ranking (rank_verdicts)
│   ├── data/
│   │   ├── market.py              # yfinance OHLCV
│   │   ├── macro.py               # FRED API (VIX, yields, DFF, CPI, UNRATE, HY OAS)
│   │   ├── macro_store.py         # Persists yesterday's regime for shift detection
│   │   ├── news.py                # RSS feeds + symbol mention tagging
│   │   ├── news_store.py          # Dated news storage + narrative persistence
│   │   ├── earnings.py            # SEC EDGAR provider
│   │   ├── technical.py           # TA indicators (MA, RSI, MACD, BB, ATR)
│   │   ├── levels.py              # Deterministic support/resistance from full OHLCV history (pivots, clustering, touch-count/distance ranking)
│   │   ├── context.py             # Deterministic market context: rel. strength, 52w range, ATR regime, MA slopes, consolidation, liquidity, gaps
│   │   ├── correlation.py         # 120d pairwise return correlations + cluster detection
│   │   └── tech_store.py          # Per-symbol rating memory + signal-age computation
│   ├── execution/
│   │   └── broker.py              # Alpaca (OTO brackets, calendar, live prices)
│   ├── risk/
│   │   ├── rules.py               # Hard risk engine (leverage-adjusted; drawdown BUY-cap gate)
│   │   ├── metrics.py             # Budget/open risk, portfolio heat, R-multiple
│   │   ├── budget.py              # allocate_risk_budget: 25% total / 40%-per-cluster at-risk gate
│   │   ├── exit_guard.py          # Metric-contradiction veto + structural-protection fallback band
│   │   └── trailing.py            # Deterministic broker-resident trailing stops
│   └── storage/
│       └── db.py                  # SQLite (trades, positions, logs, PnL, insights)
├── tests/                         # full pytest suite — `pytest tests/ -q` for current count
├── data/
│   ├── quant_agent.db             # SQLite audit trail
│   ├── earnings/                  # Cached SEC filing analyses
│   └── news/                      # Daily reports + persistent macro narrative
└── logs/                          # Timestamped run logs
```

## Tests

**Hermetic end-to-end sessions** (`tests/test_e2e_*.py`, owner ruling 2026-10-04: hand-written, replacing the frozen offline replay rig). Each drives a whole production session — morning, midday, close, evening, rotation, shorts, de-levering, partial fills — through the real pipeline with only the edges stood in (rehearsal broker, frozen clock, scripted model seats, socket wall). Three layers, one harness: SHAPE (`test_e2e_morning_session.py`, stages in order), ORDERS (`test_e2e_morning_protection.py`, `test_e2e_close_existing_book.py`: what reached the broker and nothing else), and RECORD (`test_e2e_position_review_record.py`, `test_e2e_morning_record.py`: the ledger, `session_reports` and `agent_logs` rows a later surface reads, re-read through the read-only replay reader). Extend an existing file's harness (`_run_session`, `_run_close`, `e2e_held_book_support.run_held_book`) rather than writing a second style; a new file must be seen RED by breaking production once before it merges.

```bash
pytest tests/ -v    # full suite (see "Tested" above for why no count is pinned here)
```

## Data Sources

| Source | Data | Provider |
|--------|------|----------|
| Market data | OHLCV, sector performance, valuation (trailing PE / forward PE / P/S) | yfinance |
| Macro | VIX, 2Y/10Y yields, DFF (daily fed funds), headline & core CPI, PCE, UNRATE, HY OAS spread | FRED API |
| News | Real-time headlines (11 standing RSS feeds), plus per-symbol Yahoo Finance RSS fetched each run for held positions + this run's candidates (added 2026-08-30, capped, not a standing feed) | CNBC, MarketWatch, Yahoo Finance, Seeking Alpha, Investing.com, Nasdaq, BBC, NPR, Fed, SEC |
| Earnings | 10-Q/10-K filings + strategic analysis | SEC EDGAR |
| Smart money | Form 4 non-derivative open-market insider purchases/sales (codes `P`/`S`) with accession-level provenance, acceptance lag and owner identity | SEC EDGAR full-text search + Archives (credentialless, first-party) |
| Trading | Orders, positions, account, calendar, live quotes | Alpaca API |

## Storage

**SQLite** (`data/quant_agent.db`, WAL mode + `synchronous=NORMAL`, indexed for prune):
- Trades (with stop/target, reasoning, actual submitted fill price) — `idx_trades_timestamp` keeps the 5-year prune fast
- Position snapshots (synced each midday — rows for closed symbols are purged)
- Agent logs for all 9 daily LLM agents (full input/output, tokens, model — `idx_agent_logs_timestamp` keeps the 2-year prune fast for quarter-over-quarter learning)
- Daily P&L records
- Evening insights (cross-session memory)
- `pending_protection_restores` — orphaned protective-stop recovery queue, drained at every session entry and TTL-pruned after 30 days
- `pending_repegs` — bounded entry re-peg write-ahead queue (an Alpaca replacement mints a NEW order id; this row is what lets a crash mid-replace be recovered), drained at every session entry and TTL-pruned after 30 days

**What the daily reset clears, and what it keeps.** `scripts/desk_reset.py` works from an explicit allowlist (`TABLE_POLICY`) — it only ever `DELETE`s rows, never drops a table and never deletes a file, and a table it has never heard of is **kept** and reported rather than silently emptied.

| | Tables | Why |
|---|---|---|
| **Cleared** | `trades`, `positions`, `daily_pnl`, `insights`, `intraday_evaluations`, `pending_protection_restores`, `pending_repegs` | The contaminated record: the trade ledger, the book mirror, the equity curve, the evening lessons grading those trades, and two write-ahead queues whose order ids die with the flatten |
| **Kept** | `agent_logs`, `alert_channel_checks`, `llm_budget_*`, `llm_circuit_*`, `llm_quota_holds` | Spend accounting and alerting health, not trading decisions. `src/token_budget.py` fits its per-model size estimates from `agent_logs` (`MIN_SAMPLES = 8`), so wiping it would reset the desk's budget fits along with its cost history |
| **Partly cleared** | `specialist_evidence` | The counts are real but the PM/RM rows carry reasons derived from the reward:risk geometry defect. Default `--evidence analysis-only` keeps paid specialist *observation* (`analysis`, `finding`, `admission`, `scan_summary`, `coverage`) and drops every decision-shaped row (`target`, `proposed_order`, `reasoning`, `verdict`, `modification`, `review_metrics`, `seat_stance`, `execution_skip`, `pipeline_event`, …). `--evidence none` empties it; `--evidence all` leaves it alone. Nothing in the trading pipeline reads this table, so the choice is a forensic-display one, not a safety one |

Everything file-based below is **untouched** — the reset does not delete files at all. That is deliberate: `data/pricing_cache.json`, `data/openrouter_pricing_cache.json`, `data/company_profiles.json` and the news/macro/earnings/tech/smart-money stores are expensive to rebuild in time and money, and none of them is a trading decision.

**File-based** (`data/news/`):
- `macro_narrative.json` — persistent grand backdrop, evolves daily
- `YYYY-MM-DD/full_report.json` — daily news intelligence report
- `YYYY-MM-DD/stock_alerts/` — per-symbol news alerts
- `YYYY-MM-DD/raw_headlines.json` — raw headlines for audit

**File-based** (`data/macro/`):
- `last_state.json` — yesterday's regime/confidence/outlook snapshot for shift detection

**File-based** (`data/tech/`):
- `last_ratings.json` — per-symbol prior rating + first_seen_date for signal-age tracking; TechAnalyst reads on next morning, PM uses age to cut allocations on stale setups

**File-based** (`data/earnings/`):
- `{SYMBOL}/analysis_{10-Q}_{date}.md` — cached SEC filing analyses, written by `run_earnings_preprocess` (the only LLM-producing path — see **Hot / Cold Earnings Path** below). Each file carries a human-readable header + one embedded ```` ```json ```` block with the full `EarningsAnalysis` schema, including the 5-step `investment_implications.reasoning_chain`
- `manifest.json` — tracks processed filings; `failed_attempts` counter bounds preprocess retries (abandon + mark `abandoned=True` after 3 failures so a consistently-unparseable 10-Q doesn't burn tokens every run forever)

### Hot / Cold Earnings Path

LLM analysis of 10-Q/10-K filings runs **only** in the pre-market `earnings_preprocess` window (08:00-09:15 ET). Hot sessions (morning / midday / evening) are read-only consumers: `_load_earnings_analyses` surfaces the cached analysis for already-confirmed filings, and any filing that preprocess missed appears as a `queued=True` placeholder — PM then caps the BUY at 5% regardless of conviction. No background threads in hot sessions, no session-time LLM token spend on earnings.

### Evening earnings deep-dive (value-investor lens)

Morning/midday PM sees a one-line earnings sentiment snippet (~140 chars — `"bullish high — revenue +16%"`); that's enough to gate BUY sizing. **Evening** needs more: when judging `thesis_trajectory` on a held position, "bought expensive" vs "fundamentals actually broke" is the core question. So the evening path calls `src/data/earnings_deep_dive.load_earnings_deep_dive(symbol, manifest)` per held name — it parses the JSON block out of `analysis_*.md`, pulls the full 5-step reasoning_chain (fundamental_quality / growth_trajectory / valuation_context / strategic_risks / management_execution), truncates each step (500c primary, 300c risk+mgmt) to bound token cost, and injects the structured dict into `thesis_health_context[symbol]["earnings_deep_dive"]`. The evening prompt renders a `--- Earnings deep-dive (10-Q 2026-01-30, bullish/high) ---` block so the LLM can reason on actual fundamentals, not a compressed tag. Missed-opportunity symbols still use the 140-char snippet — token budget reason; deep-dive only for the ~10-15 names we actually hold.

## Authors

**Yebo Feng** — [@yebof](https://github.com/yebof) · `fengyebo@gmail.com`

Designed and built end-to-end as a personal research project on multi-agent LLM coordination for swing/position trading. The architecture, prompt engineering across all ten agent seats, schema-enforced reasoning chains, broker order-lifecycle state machine, and quarterly self-evolution loop are all original work. Implementation was vibe-coded throughout with Claude Code (Anthropic) and Codex (OpenAI) as pair-programmer agents — the system that runs the agents was itself built by agents. Issues and PRs welcome.

**Jingyang Dai** — [@diana-jydai](https://github.com/diana-jydai) · `diana.jydai@gmail.com`

Drives the creative direction and ongoing feature development of the system — from trading ideas and agent behavioral specs to workflow improvements. Also handles day-to-day maintenance, monitoring, and operational upkeep of the live deployment.

If this repository was useful for your own work — academic, professional, or personal — a star is appreciated and a citation/mention is even better.

## License

MIT — see [LICENSE](LICENSE).
