"""The scope of the number-source guard: modules on the path to a broker order.

Relocated verbatim from ``src/number_sources.py`` so that file shrinks instead
of growing with each module split that adds an entry here. Data only: a tuple
of repository-relative paths, constructible with nothing else imported.
"""

from __future__ import annotations

from src.number_universe import py_universe

__all__ = ["SCOPED_PATHS", "py_universe"]

#: The modules on the path from a verdict to a broker order. See the SCOPE
#: rule in the module docstring; this list is the rule applied, and
#: `scripts/unscoped_number_guard.py` is what stops it from silently lagging.
#: A directory entry covers every `.py` under it.
SCOPED_PATHS: tuple[str, ...] = (
    # 2026-10-12: shared quantities the read side and trading path both import; previously unscoped.
    "src/quantities.py",
    # 2026-10-05: the position-history reader behind the prompt facts; its one limit is a dead default.
    "src/data/tech_store.py",
    "src/risk",
    "src/portfolio_constructor",
    "src/rotation.py",
    "src/rotation_parts/types.py",
    "src/rotation_parts/constraints.py",
    "src/rotation_parts/wording.py",
    "src/rotation_parts/reporting.py",
    "src/rotation_parts/reporting_lines.py",
    "src/infra_retry_policy.py",
    # 2026-10-09: the fixed $3 low-AI-credit alert (owner ruling) lives here.
    "src/llm_balance_runway.py",
    "src/nominations.py",
    "src/evidence_gate.py",
    "src/verdicts.py",
    "src/data/correlation.py",
    # Rating magnitudes feed verdict ranking, insider weights order the
    # smart-money evidence, and market-context windows shape evidence shown to
    # the Technical seat. Their numeric definitions therefore sit on the same
    # verdict-to-order path as the consumers already in scope.
    "src/models/analysis.py",
    "src/data/insider_signal.py",
    "src/data/context.py",
    # 2026-10-05: the session-window table and the regular-session bounds gate
    # WHEN an order may be placed and when a bar is treated as complete; a
    # stop cannot cover a closed market, so these minutes are on the path
    # from a verdict to an order exactly as a price threshold is.
    "src/trading_calendar.py",
    # 2026-10-01: the sector cluster moved out of src/execution/broker.py
    # verbatim (sector resolution feeds the exposure ladder); same code, same scope.
    "src/sector_reference.py",
    # 2026-09-19, board item 124: the research-defined insider purchase
    # cluster now lifts the smart-money seat's conviction, so its definition
    # is on the path from a verdict to an order.
    "src/data/smart_money_cluster.py",
    # The indicator and level units. Every ATR multiple and every stop the
    # ledger tracks is a multiple of `technical.ATR_PERIOD`, and levels are
    # where stops are placed; watching the multiplier and not the unit was
    # the gap the scope rule above was written to close.
    "src/data/technical.py",
    "src/data/levels.py",
    # Decides whether an APPROVED trade is actually sent
    # (`MAX_ENTRY_SLIPPAGE_BPS`), and carries the sizing fallback.
    "src/pipeline_stages.py",
    # 2026-10-01, board item 210 step 10: the four stage classes moved out of
    # `src/pipeline_stages.py` verbatim. Same code, same scope -- these paths
    # keep their numbers inside the ledger instead of dropping out silently.
    "src/stage_morning_research.py",
    "src/stage_decision.py",
    "src/stage_risk.py",
    "src/stage_execution.py",
    # 2026-10-08: the entry-viability preflight moved out of
    # `src/stage_execution.py` verbatim. Same code, same scope.
    "src/stage_entry_preflight.py",
    # 2026-10-08: the no-print wait budgets against the session deadline
    # less a measured reserve; that reserve is a trade-governing number.
    "src/price_feed_preflight.py",
    "src/pipeline_sizing.py",
    "src/pipeline_earnings_quality.py",
    # 2026-10-01, board item 210 step 12: the rotation-EXECUTION block and
    # the entry order-placement/re-peg block moved out of
    # `src/pipeline_stages.py` verbatim. Same code, same scope.
    "src/pipeline_rotation_exec.py",
    # 2026-10-08: lifted verbatim out of pipeline_rotation_exec; same code, same scope.
    "src/rotation_projection.py",
    "src/rotation_buy_leg_post.py",
    "src/pipeline_entry_orders.py",
    "src/execution/cash_sweep.py",
    "src/execution/stop_records.py",
    # 2026-09-19, board item 130: `broker.py` IS the broker order -- the
    # scope rule's own words ("every module on the path from a seat's
    # verdict to a broker order") named this file and it was not here.
    # `stop_repair.py` and `coverage_watchdog.py` are the repair/alarm path
    # for a protective stop that failed to place. `stop_repair.py` still
    # defines no module-level numeric constant (see the docstring note this
    # entry used to require); scoping it adds nothing today but stops a
    # future one arriving unseen.
    "src/execution/broker.py",
    "src/execution/broker_parts",
    "src/execution/stop_repair.py",
    "src/execution/order_gates.py",
    "src/execution/order_idempotency.py",
    "src/coverage_watchdog.py",
    "src/alert_claims.py",  # the alert-claim half, lifted 2026-10-05
    "src/coverage_watchdog_parts",  # verbatim lifts out of coverage_watchdog.py
    # The pipeline's own decision/execution glue. The de-lever and midday
    # order-price buffers are inline multipliers and rule (e) has seen them
    # since 2026-09-19; rule (c) (function-parameter defaults) was added the
    # same day for `_clamp_queued_earnings_buys`' `max_pct=5.0`, which no
    # longer exists — that gate refuses the BUY instead of sizing it (board
    # item 186, 2026-10-01) — and the rule stays because the shape recurs.
    "src/pipeline.py",
    "src/pipeline_parts",
    "src/pipeline_delever.py",
    "src/delever/forced.py",
    "src/delever/ladder.py",
    "src/delever/conviction.py",
    "src/delever/enforce.py",
    "src/delever/trims.py",
    "src/delever/risk_number.py",
    # The held-position exit engine and the exit-trigger vocabulary -- moved
    # here out of `src/pipeline.py` by step 4 of docs/PIPELINE_SPLIT_PLAN.md.
    # Every trail multiple and every exit threshold it carries stays scoped.
    "src/pipeline_exits.py",
    "src/exits/exit_records.py",
    "src/exits_parts",  # the trail cooldown lifted verbatim 2026-10-04
    # The intra-check session and the intraday opportunity scan -- moved here
    # out of `src/pipeline.py` by step 8 of docs/PIPELINE_SPLIT_PLAN.md.
    # 2026-10-04: the intraday bodies are parts under src/intraday/; the directory entry covers them all.
    "src/pipeline_intraday.py",
    "src/intraday",
    # 2026-10-01, board item 210 step 6: the universe-admission cluster --
    # the external-nomination gates, the screen and its admission -- moved
    # here out of `src/pipeline.py`. Its dollar-volume and price floors stay
    # scoped.
    "src/pipeline_admission.py",
    "src/pipeline_prompt_facts.py",
    "src/pipeline_prompt_facts_pure.py",
    "src/pipeline_prompt_facts_review.py",
    "src/prompt_facts/missed_ops_signals.py",
    "src/prompt_facts/review/grading.py",
    "src/prompt_facts/review/exits.py",
    "src/prompt_facts/review/calibration.py",
    "src/prompt_facts/review/blocked.py",
    "src/prompt_facts/review/replay.py",
    "src/prompt_facts/decisions.py",
    "src/prompt_facts/projected.py",
    "src/prompt_facts/watchlist.py",
    "src/prompt_facts/heat.py",
    "src/prompt_facts/pm_facts.py",
    # Step 5 of docs/PIPELINE_SPLIT_PLAN.md (board item 210): risk-verdict
    # application moved here out of `src/pipeline.py`.
    "src/pipeline_risk_gate.py",
    # 2026-10-01, board item 210 step 2: the protection cluster -- stop
    # coverage, repair, protected sells, write-ahead restore, the fill and
    # stop-out reconcilers -- moved here out of `src/pipeline.py`. Scoped at
    # its new address so its numbers stay under the guard.
    "src/pipeline_protection.py",
    "src/protection/protected_sell.py",
    "src/protection/reprotect_records.py",
    # Every seat's prompt-construction and LLM-call code -- the path from
    # evidence to a seat's verdict the scope rule names. Most of what lives
    # here is LLM plumbing (timeouts, retries, token budgets) that is
    # `not-trade-governing` once seen; the truncation caps and rank tables
    # that shape what evidence a verdict is built from are not.
    "src/agents",
    # 2026-09-19: the universe admission screen. Every threshold that decides
    # whether a symbol may be traded at all lives here or in
    # `UniverseScreenConfig`.
    "src/universe_screen.py",
    "src/cost_table.py",
    # 2026-10-05: the free-space floor a session must clear before it may start.
    "src/desk_disk_floor.py",
    # 2026-10-05: the trading-day lookup lifted out of the coverage watchdog (its scoped
    # home) so the read-only dashboard can share it; scoped so its number stays ledgered.
    "src/trading_day.py",
    # 2026-10-05: the event-risk DATA module -- the FOMC schedule fetch/parse
    # and the earnings-proximity window every seat's prompt is built from.
    # The event-risk GATES already sit in scope via `EventRiskConfig`; this
    # brings the fetch windows, parse sanity bounds and the 3-session
    # earnings window under the same ledger instead of beside it.
    "src/data/event_calendar",
    # 2026-10-05: three modules whose numbers were never classified; each is
    # scoped so every site must be ledgered, including the ones it hides.
    "src/data/news_dedup.py",
    "src/token_budget.py",
    "src/backtest/engine.py",
    # 2026-10-05: four offline research scripts (the level sweep, its volatility-clustered control, the
    # minimum-stop sweep and the noise-band holding scan); each site is ledgered so none hides unseen.
    "ops/research/item55_level_sweep.py",
    "ops/research/item55_volclustered_control.py",
    "ops/research/min_stop_atr_sweep.py",
    "ops/research/noise_band_holding_scaling.py",
    # 2026-10-05 numbers sweep 2: the offline model benchmark's fixtures and
    # sizing mirror, and the CI shard weights. Scoped so each number is
    # ledgered with the proof that it reaches no order.
    "ops/model_policy/scenarios.py",
    "ops/model_policy/scenarios_midday_exit.py",
    "ops/model_policy/deterministic_selection.py",
    "scripts/ci_shard.py",
    # 2026-10-08: the restored trend-alignment research harness (board item 75); offline, reaches no order.
    "scripts/trend_alignment/analysis.py",
    "scripts/trend_alignment/data.py",
    # 2026-10-05 numbers sweep 3: the news-verdict model and the company-profile
    # and market-data fetch modules. Scoped so each number is ledgered with the evidence
    # of whether it reaches a trade decision.
    "src/models/news.py",
    "src/data/company.py",
    "src/data/market.py",
)
