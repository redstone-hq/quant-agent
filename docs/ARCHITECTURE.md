# Target architecture and the route to it

Status: design document. No production code changes, no behaviour change.
Measured against `origin/main` at commit `ceef89b0`, 2026-10-01.

Every structural number below was produced by a grep or an AST pass run against
that commit; the method is named next to the number. Numbers without a stated
method do not appear in this document.

---

## 0. What was measured, and where the brief was wrong

The brief that commissioned this document made six structural claims. Four hold,
two do not. Correcting them matters, because the conversion order in section 4
depends on which pieces are actually entangled.

### 0.1 Confirmed

**One class, six mixins.** `src/pipeline.py` declares
`class TradingPipeline(ProtectionMixin, PromptFactsMixin, DeleverMixin,
ExitEngineMixin, ResearchContinuityMixin, IntradayMixin)`. `RiskGateMixin`
and `AdmissionMixin` were deleted on 2026-10-05: each is now a built
collaborator reached through a descriptor on `TradingPipeline`.

**The broker's in-place stop amend is a boundary (2026-10-02, first broker instalment).** `src/execution/broker_parts/stop_amend.py` holds `StopAmender` (the amend-one-stop, classify-after-dead-replacement and amend-resting-stops bodies, lifted verbatim with the `_quantize_price` / `_is_terminal_broker_rejection` helpers and the `_AMEND_NOT_ATTEMPTED` sentinel); `AlpacaBroker` keeps same-named thin shims and re-exports the helpers so every patch target still resolves. Witness: `tests/test_broker_parts_boundary.py`. `broker.py` is 6,673 lines after it; later instalments follow the same package.

**The broker's stop submission, restore and replacement is a boundary (2026-10-02, second broker instalment).** `src/execution/broker_parts/stop_place.py` holds `StopPlacer` (the retrying stop-submit helpers, `_submit_stop_limit_order` / `_submit_stop_legs` / `_restore_stop_orders`, `shift_stops_down` and `replace_stop_loss`, lifted verbatim with the module helpers and constants those bodies read: `_is_held_for_orders_error`, `_is_unsupported_stop_market_rejection`, `_split_protective_qty`, `_derive_stop_tif`, `_alpaca_symbol`, `_internal_symbol`, `real_broker_order_id`, `_FRACTIONAL_QTY_EPSILON`, `PROTECTIVE_ORDER_ACTIVE_STATUSES`). `AlpacaBroker` keeps same-named thin shims built per call, passes its own bound cluster methods in so instance-level test doubles still land, and re-exports every moved helper; the number ledger rows for the moved constants point at the new module, and `src/execution/broker_parts` is in both the number-scan scope and the silent-swallow guard. `place_entry_protection` stays on the broker for now: its body imports `src.execution.scale_in`, which reaches back to `AlpacaBroker` for the stop-limit buffer default, so lifting it closes a new import cycle until that default lives below both. `broker.py` is 5,485 lines after it.

**The broker's order desk and its account/calendar reads are boundaries (2026-10-02, third broker instalment).** `src/execution/broker_parts/order_desk.py` holds `OrderDesk` (`submit_order`, `replace_entry_limit`, `cancel_entry_order`, the replacement-chain follow, `wait_for_order_terminal` / `wait_for_order_at_exchange` and the polling waits, the open/filled/recent order reads and `close_position`, lifted verbatim with the module helpers those bodies read: `_outlier_refusal_detail`, `_is_terminal_submission_rejection`, `_PLAIN_PRICE_LABELS`). `src/execution/broker_parts/account_reads.py` holds `AccountReads` (account, activities, asset, shortability/fractionability, portfolio history, trading-calendar and resting-stop-price reads; the four per-process caches are passed in and mutated in place, so they stay the broker's own dicts). The stream-backed waits (`_wait_for_order_status`, `_wait_for_order_status_via_stream` and its `_locked` half) stay on the broker because they read and write the live trade-updates hub, lease slot and warm-up record; the desk reaches them as collaborators. Both factories reuse the `_stop_placer` recursion guard (`_is_broker_class_shim`) so a desk is never handed the broker's shim for a body it already owns. The number ledger rows for the moved defaults point at the new modules; both files are in the silent-swallow guard's scope. `broker.py` is 3,762 lines after it.

**The deployable-cash helpers are a boundary (2026-10-04, first `src/pipeline.py` core instalment).** `src/cash_park.py` holds `CashPark`: `_compute_deployable_cash` and `_news_held_symbols`, both bodies lifted AST-identical from `TradingPipeline` (2 of 2 compared against the trunk). The one collaborator is keyword-only: `sweeper`, the host's `_sweeper` callable, handed in and NOT lifted, so a test that binds `_sweeper` on the pipeline is what the bodies see and no lifted body is passed back in. `TradingPipeline` keeps same-named thin shims built per call through `_cash_park`; `_sweeper` itself stays on the host as the one-line read of `sweeper_or_none`. The retired-vehicle pair (`_retired_cash_park_symbol`, `_release_retired_cash_park`, 38 lines) was lifted and PUT BACK: each does a function-local `from src.execution.cash_sweep import CashSweeper`, and `tests/test_import_layering.py` refuses any new module reaching the broker seam, the same ruling the missed-ops builders below are waiting on (widen the importer set by one relocated module, or hand `CashSweeper` in as a collaborator, which changes the body). Measured before the cut: `src/pipeline.py` is 1,894 lines of which `__init__` is 520 and no other method exceeds 55; the constructor is left to shrink as parts move out (decided, not re-opened). Still on `TradingPipeline` after this instalment and why: the P&L trio (`_total_pnl_since_reset`, `_record_account_snapshot`, `_attach_pnl`, 116 lines) shares the `_last_account_snapshot` attribute that `src/pipeline_intraday.py` and the session wrappers write on the host directly, so lifting it needs that state handed in rather than held; the earnings cluster (`_earnings_preprocess_symbols`, `_load_earnings_analyses`, `_run_earnings_preprocess_body`, `_evening_earnings_proximity`, 115 lines) and the broker-sync pair (`_sync_positions_from_broker`, `_record_short_overnight_gaps`, 56 lines) are the next cuts. Witness: `tests/test_cash_park_boundary.py`.

**The trade-review prompt facts are five boundaries (2026-10-04, second prompt-facts instalment).** `src/prompt_facts/review/` holds `ReviewGrading` (graded sells, graded buys, trade-grade summary), `ReviewExits` (post-exit reality, missed lessons, loss pits), `ReviewCalibration` (outlook calibration, calibration note, recent performance), `ReviewBlocked` (blocked proposals) and `ReviewReplay` (evening replay inputs): all eleven bodies lifted AST-identical from `PromptFactsReviewMixin`, each file under the 400-line floor for a new file. Collaborators (`db`, `broker`, `market`, the host's `_sweeper` callable read per use, `_EXIT_AUDIT_ACTIONS`, the operator conviction logger) are keyword-only constructor arguments; the one cross-family read, `_build_trade_grade_summary` -> `_build_post_exit_reality`, is handed into the grading part as the host's shim (or whatever a test swapped in), so no recursion guard is needed. `src/pipeline_prompt_facts_review.py` keeps same-named thin shims built per call through five `_review_*` builders; the 14 ledger ids moved with their bodies (`src/ledger_move.py`) and their line citations were re-pointed. Witnesses: `tests/test_prompt_facts_parts_boundary.py`. **The seat HOLDS the review part (2026-10-04, third instalment):** `PromptFactsReviewMixin` is retired; `src/prompt_facts/review/held.py` holds `PromptFactsReview(host=...)`, the five `_review_*` builders and ten shims moved AST-identical onto it, every collaborator a live per-call read off the host, and `hold_prompt_facts_review` (the class decorator on `PromptFactsMixin`) installs same-named delegates onto one part per pipeline instance (built on first use). `_build_post_exit_reality` is a collaborator of the grading part, so the part reads the host's live and the host's delegate goes straight to the exits part. Witnesses: the seat tests in `tests/test_prompt_facts_parts_boundary.py` and the MRO test in `tests/test_boundary_harness.py`. **Fourth instalment (2026-10-05): the part knows no host.** `PromptFactsReview` takes its six collaborators as keyword arguments (`db`, `broker`, `market`, `sweeper`, `exit_audit_actions`, `log_conviction_outcome_for_operator`; `COLLABORATORS` in `held.py`) and builds with none of them; `review_of` in `src/pipeline_prompt_facts_review.py` is the ONE place the seat's attribute names meet the part's keywords (`HOST_COLLABORATORS`) and rebuilds the part per delegate call from the seat's current values, so nothing is cached on the seat and the old live-read descriptor and `HOLDER_ATTR` cache are gone. A collaborator swapped on the SEAT between calls still reaches the next call (same behaviour as before); a collaborator swapped on an already-built PART does not, and no test relies on that any more. **Removed 2026-10-16:** the post-exit reality block (`_build_post_exit_reality`, its `post_exit_reality` summary key, the reviewer prompt section and the `build_post_exit_reality` collaborator) is deleted: it fed the desk's own past exit outcomes back as an instruction (REBUILD_SPECIFICATION rule 3.14 forbids fitting to the desk's past record), its 2/12/120/5%/50% were made-up, and it mis-scored short covers. Mentions of it above are history.

**The remaining prompt facts are eight boundaries (2026-10-04, third prompt-facts instalment).** `src/prompt_facts/` holds `PromptHistory` (position history, weekly narrative, macro trajectory, active state changes), `PromptDecisions` (recent risk verdicts, recent PM decisions, review metric deltas, own recent decisions), `PromptProjected` (the projected-portfolio preview; the host is handed in as the sector-cache owner, so `_last_symbol_sectors` is read and written through live), `PromptWatchlist`, `PromptExposure` (correlation matrix, live stop map), `PromptHeat` (portfolio heat; handed the host's stop-map shim), `PromptPMFacts` (PM facts block, conviction-outcome operator log; handed the host's heat and history shims) and `PromptPositionFacts`. `PromptFactsMixin` is shims only, built per call. Witnessed in `tests/test_prompt_facts_parts_boundary.py`.

**The intra-check session and the intraday scan are five boundaries (2026-10-04, item 210 step 8 parts conversion).** `src/intraday/` holds `IntradaySafety` (the free safety pass and its reconcile-and-drain preamble), `IntradaySession` (the intra-check, its body and its report), `IntradayGating` (cooldown memory, the owner-session lock, the paid-scan slot, the single-scan process lock, snapshot-health tracking) and `IntradayCandidates` (the process-locked scan wrapper, held-technical refresh, mover candidates, the ATR move context and the two skip records); `IntradayScanBody` holds `_intraday_opportunity_scan_body` and stays in `src/pipeline_intraday.py` because that one function is 399 lines and cannot be carved to fit the 400-line floor for a new file without changing it (a split of the function is a behaviour-preserving refactor, not a move, and is left for its own change). All 21 bodies lifted AST-identical from `IntradayMixin` (21 of 21 compared against the trunk by `ast.dump`). Collaborators are keyword-only constructor arguments read off the host per shim call (`db`, `broker`, `config`, `market`, the stores, the three stages, the host's `_sweeper` and every host method a body calls); the five host attributes a body both reads and ASSIGNS (`_intra_preamble_deferred`, `_last_account_snapshot`, `_last_evidence_freshness`, `_paid_scan_waited`, `_paid_scan_waited_for`) go through `_HostState`, a get/set view over the live host, never a copy. A body a part reads through `self.` that lives on the SAME part (`_run_intra_safety_preamble`, `_persist_intra_check_report`, `_run_intra_check_body`, `_blocking_owner_session`, `_intra_window_remaining_s`) is handed in through `IntradayMixin._intraday_live`: it honours an instance- or class-level swap on the host (tests do both) and runs the part's own body while the host still carries the module's `@_shim`-marked shim, so nothing recurses. `IntradayMixin` is shims only; `_another_session_recently_active`'s shim keeps its explicit signature so the ledgered default stays at its trunk site (the body's identical default is ledgered under the new module). `compute_indicators` is still patched on `src.pipeline_intraday` by 34 tests and still reaches the scan body, which did not move. `tests/test_invariants.py` reads the parts' source instead of the shims'; `tests/test_rehearsal_report_verdict.py` scans the part modules before the shim module. Witness: `tests/test_intraday_parts_boundary.py`.

**The intra check trails stops at every 30-minute tick (owner ruling 2026-10-08).** Before this, the deterministic trail ran only at the midday and close reviews. `src/intraday/tick_trail.py::trail_on_tick` runs the SAME `_apply_deterministic_trails` (no new maths, no new number) before the paid scan, so it only ever tightens, mirrors a short, and replaces through `replace_stop_and_record` (the stop-quantity-invariant amend path, PR 1556). It refuses to trail while the tick's safety preamble was deferred, re-takes the broker-write lock and re-checks the owner session, and skips any name whose price or ATR does not read (logged, with a traceback when a read raised) rather than trailing it without its noise band. The tick's report carries `tick_trail` (status, order count, skipped names). Wiring exception: `IntradaySession` reads these five host collaborators through `_HostState.get` because `src/pipeline_intraday.py` cannot grow under the file-size guard; an explicit `trail_collaborators=` argument exists for when that file shrinks. Witness: `tests/test_trail_every_intra_tick.py`.

**The de-levering ladder is five boundaries (2026-10-04).** `src/delever/` holds `DeleverLadder` (live de-lever price, drawdown-resolved ceiling, margin-floor breach), `DeleverConviction` (weakest-conviction cut order), `DeleverEnforce` (ceiling enforcement, shortfall record), `DeleverForced` (forced de-lever against a margin deficit) and `DeleverTrims` (trim submission, by-conviction variant, deferred discharge): ten bodies plus the two risk-number helpers (`src/delever/risk_number.py`) lifted AST-identical from `DeleverMixin`, each file at or under the 400-line ceiling for a new file. Collaborators (`db`, `broker`, `config`, `_sweeper`, `_sweep_symbol`, `_open_exit_relief`, the protection-path sell helpers) are keyword-only constructor arguments read off the host per call; every cross-body call (`_force_delever` -> `_live_delever_price`, `_discharge_deferred_gross_ceiling` -> `_enforce_gross_ceiling`, ...) is handed in as the host's shim, and no part calls a body it owns, so no recursion guard is needed. `src/pipeline_delever.py` keeps same-named thin shims built per call through five `_delever_*` builders; the two owner alerts (`_alert_owner_force_delever_incomplete`, `_alert_owner_delever_incomplete`) still carry their bodies there and are handed into the parts. The one ledger id moved with its body (`src/ledger_move.py`). Witnesses: `tests/test_delever_parts_boundary.py`.

**The missed-opportunities / thesis-health signal helpers are a boundary (2026-10-02, item 210 step 10b, first prompt-facts instalment).** `src/prompt_facts/missed_ops_signals.py` holds `MissedOpsSignals`: the eight per-symbol signal helpers (held set, tech, news, theme tags, earnings, macro sector map, thesis tech trajectory, thesis news events) that `_build_missed_opportunities_digest` and `_build_thesis_health_context` are built from, lifted verbatim with `db`, `news_store`, `earnings_provider`, `macro_store` and `parse_logged_agent_response` as keyword-only constructor arguments. `PromptFactsMixin` keeps same-named thin shims built per call; no lifted body is passed back in because none of the eight calls another. Still on the mixin, and why: the two builders themselves each do a function-local `from src.execution.broker import _get_sector`, so lifting them verbatim would add a new module to the frozen broker-seam importer allowlist in `tests/import_layers.json`; that needs a ruling (widen the frozen list by one relocated importer, or hand the sector lookup in as a collaborator, which changes the body) before they move. Witness: `tests/test_prompt_facts_parts_boundary.py`.

**The cost circuit's state reads, quota holds and admission are boundaries (2026-10-02, second cost-circuit instalment).** `src/cost_circuit/parts/circuit_state.py` holds `CircuitState` (settled totals, state row, scope key, active-hold lookup, effective state and the transient-latch auto-clear), `src/cost_circuit/parts/quota_holds.py` holds `QuotaHolds` (reconcile, hold, latched-snapshot refresh, trip) and `src/cost_circuit/parts/admission.py` holds `Admission` (settled-limit enforcement, `enforce_current_limits`, `require_paid_analysis`, `begin_call`), all lifted verbatim. The three mixins keep same-named thin shims built per call and reuse `parts/shim_guard.py` so a part is never handed the mixin's own shim for a body it already owns. `Admission` reads the unavailable sentinel through a getter (a property on the part), not a construction-time copy: `enforce_current_limits` reads it after `_sync_emergency_latch` / `_run_with_infra_retry` can install it on the breaker, so a snapshot would have raised where the breaker returned the sentinel's answer. No lock moved: `enforce_current_limits` takes the infrastructure lock inside its body, exactly where it did on the mixin. Witness: `tests/test_cost_circuit_parts_boundary.py`.

**The whole cost circuit is parts (2026-10-02, third cost-circuit instalment).** The last five mixins are lifted verbatim: `parts/settlement.py` (`Settlement`: `before_provider_attempt`, `complete_call`, `fail_call`), `parts/emergency_latch.py` (`EmergencyLatch`: durable latch read/write/sync, best-effort snapshot), `parts/infra_retry.py` (`InfraRetry`: retry/backoff, `mark_unavailable`, `_raise_if_unavailable`), `parts/operator_controls.py` (`OperatorControls`: `status`, `reset`) and `parts/session_lifecycle.py` (`SessionLifecycle`: initialize, seed/validate the day, `activate_session`, context). The bodies that ASSIGN `_infrastructure_error` / `_unavailable_sentinel` (latch sync, `mark_unavailable`, `reset`) do so through a read/write property pair on the part backed by live getter/setter collaborators that write straight through to the breaker; the parts that only read get the getter. No lock moved: every `with self._infrastructure_lock:` and the `_emergency_file_lock()` in `reset` stay inside the moved bodies, exactly where they were, and the setter runs under that same held lock. `src/cost_circuit/breaker_*.py` are now all thin per-call shims; `LLMCostCircuitBreaker` is the composition point and nothing else.

**Four cost-circuit parts are HELD, not inherited (2026-10-02, fourth cost-circuit instalment).** `LLMCostCircuitBreaker` now builds one `AlertFormats`, `EpisodeWording`, `CircuitState` and `QuotaHolds` each in `_hold_parts` (called from both `__init__` and `fail_closed`, before any latch sync or `mark_unavailable` can run through them) and every same-named breaker method delegates to that instance; `QuotaHolds` is wired to the held `CircuitState`'s own bound methods, not back through the breaker. The four shim modules `breaker_formats.py`, `breaker_wording.py`, `breaker_state.py`, `breaker_holds.py` are deleted and the four mixins are out of the MRO. Built once is safe for these four because their only non-part collaborators are `config` and the latch path (never reassigned after construction) and the session `_context` (a bound method that reads the ContextVar live). `parts/alert_formats.py` names `EpisodeWording` directly, so the old bind-the-breaker-class-into-the-module cycle is gone. Consequence for tests: a swap of `_trip_locked`, `_state_row` etc. on the breaker instance no longer reaches a call made inside a part; swap it on `breaker._quota_holds` / `breaker._circuit_state` (no existing test did either). Still inheriting per-call shims: latch, retry, session, notify, admission, settlement, operator (seven). Witness: `test_breaker_holds_the_four_parts_instead_of_inheriting_them` in `tests/test_cost_circuit_parts_boundary.py`.

**All eleven cost-circuit parts are HELD; no mixin remains (2026-10-02, fifth cost-circuit instalment).** `LLMCostCircuitBreaker` now inherits from nothing: `_hold_parts` (a two-line call into `src/cost_circuit/assembly.py`, which holds the whole wiring as `hold_parts(breaker)` so the holder stays a thin 265-line delegator; the same-named delegates are generated at import from the class's `_DELEGATES` table) also builds one `EmergencyLatch`, `InfraRetry`, `SessionLifecycle`, `OwnerNotify`, `Admission`, `Settlement` and `OperatorControls`, and is called after every slot they read exists (`_session_context`, `_infrastructure_lock`, the sentinel and error slots), in both `__init__` and `fail_closed`. The seven shim modules `breaker_latch.py`, `breaker_retry.py`, `breaker_session.py`, `breaker_notify.py`, `breaker_admission.py`, `breaker_settlement.py`, `breaker_operator.py` are deleted. Three collaborators were NOT safe to snapshot at build time and are handed in live: `notifier` (tests reassign `circuit.notifier` after construction; it is now a write-through property on the breaker that updates the three parts holding it), `_connect` (tests swap it on the instance; every part gets a late-bound lambda), and the unavailable sentinel for `OwnerNotify` (it took a construction-time copy, which per-call building had hidden; it now takes `read_unavailable_sentinel` and reads it through a property like every other part). `enabled` is still a construction-time bool because nothing reassigns `config` or `config.enabled` on a built breaker. A part is never handed the breaker's delegate for a body it already owns (that would recurse), so `shim_guard.py` is no longer used here (the PM seat still uses it). Consequence for tests: a swap of `_seed_today`, `mark_unavailable`, `_notify_if_needed` etc. on the breaker instance no longer reaches a call made inside a part; swap it on the held part (no existing test did). Witness: `test_breaker_holds_every_part_instead_of_inheriting_any` and `test_held_parts_see_collaborators_that_change_after_construction` in `tests/test_cost_circuit_parts_boundary.py`.

**A $0 route survives a money-cap-only suspension (2026-10-12).** `src/cost_circuit/free_route.py` admits a call at `require_paid_analysis`, `begin_call` and each `before_provider_attempt` only when its model has an all-zero `PRICING` row and every active hold is `daily_cost_limit`/`session_cost_limit` with no hard latch; any other trigger refuses everything, and a paid fallback attempt raises `PaidAnalysisSuspended` (so a free seat whose free route fails is lost, never silently paid). Why: owner ruling 2026-10-01, every holding re-justified multiple times a day; the free tech seat feeds the below-bar sale. Residual risk: the $0 row is asserted, not measured, and the call-count backstop is per session, not per day. Witness: `tests/test_cost_circuit_free_route.py`.

**The close session runs past a tripped DAILY cap (2026-10-14).** `daily_cap_applies` in `src/cost_circuit/free_route.py` exempts the `close` session (held-stock end-of-day review) from `daily_cost_limit` only: it neither trips nor sees the day-wide hold, while its own `session_cost_limit`, the call-count backstop and every hard latch still bind, and all other sessions are still refused. Why: owner ruling 2026-10-14; on 12 Oct the cap tripped at 13:19 ET and close made 0 paid calls, versus $0.0072/$0.0068/$0.0034 on 9/13/14 Oct [measured]. Witness: `tests/test_close_review_past_daily_cap.py`.

**The portfolio-manager seat's decision grounding is a boundary (2026-10-02, first PM-seat instalment).** `src/agents/portfolio_manager/decision_grounding.py` holds `DecisionGrounding` (reply validation, unadjudicated-conflict and sub-floor-catalyst drops, invalid-target/rejection drops, canonical targets, repair-equality check), all eleven bodies lifted verbatim from `DecisionGroundingMixin` with only `cls` -> `self`. The mixin in `grounding.py` keeps same-named thin shims built per call, hands the part the host's alias table and `build_evidence_registry`, and reuses `src/cost_circuit/parts/shim_guard.py` (plus a raw-descriptor check, because a classmethod read off a class binds fresh each time) so the part is never handed the mixin's own shim. `_DECISION_FIELDS` stays on the mixin: the agent reads it, no lifted body does. The module-level status keys and regexes moved with the bodies and are re-exported from `grounding.py`; the package's patch mirror now also covers the new module. Witness: `tests/test_portfolio_manager_parts_boundary.py`. Second instalment (2026-10-02): the other three mixins follow the same pattern -- `evidence_prompting.py` (`PromptEvidence`, ten bodies, prompt text byte-identical), `rotation_rendering.py` (`RotationSection`, three bodies) and `candidate_ranking.py` (`CandidateRanking`, five bodies). The ranking bodies read AND assign `PortfolioManagerAgent._macro_parse_failures` and call `_macro_sectors`; the part sees them through `_HostState`, a property-backed view over the getter/setter/callable the shim passes per call (never a copy), so the only extra rename there is `PortfolioManagerAgent` -> `self._host`. Former staticmethods gain `self` as their first parameter. All four PM-seat mixins now pass `check_boundary`; nothing in the seat is welded to the agent class any more. Third instalment (2026-10-04): the seat HOLDS all four parts and inherits none of them. `PortfolioManagerAgent(LiveLimitPrompt, BaseAgent)` is the whole MRO; `hold_prompt_evidence`, `hold_candidate_ranking`, `hold_rotation_section` and `hold_decision_grounding` (in `prompt_evidence.py`, `ranking.py`, `rotation_section.py`, `grounding.py`, which are no longer mixins) each build ONE part at import and install same-named classmethod delegates generated from a `DELEGATED` table (`src/agents/portfolio_manager/held_part.py`), so the 123 class-level test call sites still resolve unchanged. Nothing is snapshotted: `_macro_parse_failures` is read and assigned on the agent class per call through `_HostState`, `build_evidence_registry` and `_macro_sectors` are called through the class, `_CONFLICT_SOURCE_ALIASES` is a `LiveMapping` over the class attribute, and the bodies a part reads through `self.` that tests swap on the agent class (`_collect_seat_verdicts`, `candidate_eligibility`, `rotation_precheck`, `_rotation_constraint_line`, `_target_intent`, `_canonical_targets`, `_conflict_is_named`) are `live_body` collaborators that re-read the class per call and run the part's own body while the class still carries the delegate (the recursion guard that replaces `shim_guard` here; the PM seat no longer imports `shim_guard.py`). `_CONFLICT_SOURCE_ALIASES` and `_DECISION_FIELDS` moved AST-identical from the mixin body to `grounding.py` module constants that the holder installs on the agent class. Consequence for tests: an instance-level swap (`agent._target_intent = ...`) never reached a body before either (the shims read the class), and still does not. Witnesses: `test_agent_inherits_no_mixin_and_holds_every_part`, `test_part_is_built_held_and_run_on_a_bare_class_with_no_agent` in `tests/test_portfolio_manager_parts_boundary.py`.

**The technical seat's re-read cache is a boundary (2026-10-04).** `src/agents/tech_reread.py` holds `TechReread`, the one body lifted from `TechRereadMixin` with two seams: `ask` (a getter for `_analyze_batch_uncached`, read per call so an instance-bound spy is honoured) and `state` (the owner, which carries `last_carried`/`last_unanswered`/`last_unreadable` for the pipeline). `TechAnalystAgent` inherits only `BaseAgent`; `hold_tech_reread` is a class decorator installing the single thin `analyze_batch` shim, built per call so an agent made with `__new__` still answers. `_record_answer_hygiene` and its two constants moved verbatim to `src/agents/tech_answer_hygiene.py`. Witness: `tests/test_tech_reread_parts_boundary.py` (never imports the agent).

**The research change detectors are a boundary (2026-10-02, item 210 step 9, first research-continuity instalment).** `src/research_continuity/change_detectors.py` holds `ResearchChangeDetectors`: the macro regime / FRED-print change detectors (`_macro_regime_or_print_changed`, `_macro_history_regime_changed`, `_live_macro_series_prints`, `_macro_series_prints_changed`) and the news-wire peek (`_watched_research_symbols`, `_peek_news_headlines`, `_peeked_news_wire_text`, `_news_has_newer_material_wire`), lifted verbatim with `macro_store`, `macro`, `news_provider`, `news_store` and `config` as keyword-only constructor arguments. Chosen first because it is the shallowest piece left in the pipeline: read-only, no journal write, no broker import, five collaborators and one shared slot. That slot -- the wire items the expiry peek fetched, which the heal path re-asks with -- is a `last_news_peek_items` property backed by an optional getter/setter pair, so the pipeline keeps it on `_last_news_peek_items` where `_cover_healed_news_wire` finds it and a standalone part keeps it in memory; the only edit to a moved body is that rename. `ResearchContinuityMixin` keeps same-named thin shims built per call by a module-level `_change_detectors(host)` (a function, not a method, so the tests that bind one shim onto a bare object with `__get__` still reach the moved code); a host's instance-level replacement for a lifted body is passed in, the mixin's own shim never is (`shim_guard`). Witness: `tests/test_research_continuity_parts_boundary.py`. Still on the mixin: the carry-forward readers, the Form-4 backlog alert and the seat-heal path (~930 lines), which write the journal through `_persist_evidence` and read `self.db`; they are the second instalment.

**The carry-forward readers, the insider memory, the Form 4 backlog record and the seat heal are boundaries (2026-10-02, item 210 step 9, second research-continuity instalment).** Five parts, bodies lifted verbatim from `ResearchContinuityMixin`: `src/research_continuity/form4_backlog.py` (`Form4BacklogRecorder`: the Form 4 backlog and congressional refresh records and the before-the-open alert), `carry_forward.py` (`CarryForwardReaders`: macro, news and earnings carry-forward plus `_latest_news_read_today`, and the `CarryForward` dataclass, which the mixin and `src.pipeline` re-export), `insider_memory.py` (`InsiderMemory`: the Form 4 freshness probe, known accessions, remembered findings, session date and the insider carry-forward), `heal_records.py` (`HealRecords`: the heal log row, the paid-call records, the macro-store and news-wire keeps) and `seat_heal_path.py` (`SeatHealer`: the one-paid-retry decision and the heal dispatcher). The dependency that made these harder than the detectors is passed IN: every journal write goes through a `journal` collaborator with `EventJournal.persist_evidence` (the mixin hands in a `_HostJournal` that resolves `_persist_evidence` over the host's current `db` per write), storage is a `db` argument, the paid-analysis gate and the analyst seats are callables (`require_paid_analysis`, `agent_for`), and the four record writers reach `SeatHealer` as callables; no part holds a host. The only edits to moved bodies are those substitutions (`_persist_evidence(self.db, ...)` -> `self.journal.persist_evidence(...)`, `getattr(self, name)` -> the constructor argument) and dropping the `RunContext` type hints. The mixin is now shims only (287 lines) built per call by five module-level builders with the same `_lifted_collab` rule as the first instalment; `tests/test_boundary_harness.py` accepts a shim-only mixin as failing clause 1 alone. Two guards were repointed, not widened: the HEALABLE_CATEGORIES single-reader allowlist in `tests/test_seat_heal_wiring.py` names `seat_heal_path.py` instead of the mixin, and nine `config/number_ledger.yaml` notes that cited line ranges inside the mixin for numbers that live in `pipeline_prompt_facts_review.py`, `pipeline_intraday.py`, `pipeline_exits.py` and `prompt_facts/missed_ops_signals.py` (stale since the pipeline.py split; exposed when the file shrank) now cite the function each number belongs to. Witness: `tests/test_research_continuity_parts2_boundary.py`. Nothing of the research-continuity cluster is welded to the pipeline class any more.

**Five exit-engine pieces now ARE boundaries (2026-10-02).** `src/exits/`
holds `TargetRevision`, `StructuralProtection`, `ExitSubstantiation`,
`HoldingDiscipline` and `AlignmentExit`, each a standalone class taking every
collaborator as a keyword-only constructor argument (the `src/sessions/`
pattern); `ExitEngineMixin` keeps a thin same-named shim per method. All five
pass `check_boundary`; `tests/test_exits_boundary.py` is the witness.

**One more followed (2026-10-04):** `ExitRecords` (today's trims, filed target
revisions, the trail cooldown, exit-review approvals, the event-risk block),
same shape, same shims, same witness file. The trails and the AI risk review
were first refused by the import-layering and cycle guards; on 2026-10-08 (PR 1588)
they were lifted as module functions taking the pipeline as their first argument,
into `src/exits_parts/trails.py` and `src/exits_parts/risk_review.py`, each behind a
one-line shim on `ExitEngineMixin`. The risk review is advisory only since PR 1591:
its objections to a sell are recorded, never used to block it. Still on the mixin, on purpose:
`_alignment_exit_cached` and `_voice_structural_protection_break` hold per-run
memos on the host (a part rebuilt per call would lose them), and
`_midday_execute_llm_actions` stays the executor. Three phases of its per-symbol
loop were lifted verbatim on 2026-10-09 into `src/exits_parts/midday_gates.py`
(metric-contradiction and AI Risk vetoes), `midday_holding_discipline.py` (the
named-trigger fact-check) and `midday_spent_trigger.py` (the same-trigger dedup).
Each returns `SKIP` where the loop used to `continue`; the loop invariants
travel in `MiddayLoop` (`midday_state.py`). The noise band, the trigger
classification and the order submission stay in the method: the factor
literals the number ledger cites by method, and a test that patches
`_reason_cites_hard_trigger` on `src.pipeline_exits`, are pinned there.

**The portfolio constructor's order builders ARE a boundary (2026-10-02, first constructor instalment).** `src/portfolio_constructor/order_build/` holds one standalone piece per leg, each under the 400-line new-file floor: `long_entry.py` (`LongEntryBuilder._build_buy`), `short_entry.py` (`ShortEntryBuilder._build_short`) and `exits.py` (`ExitOrderBuilders._build_sell`, `_build_cover`, `_hold_decision` — pure functions of their arguments, no collaborators), all lifted verbatim. Each entry builder's collaborator (`cfg`, `_derive_target`, `_resolve_entry_and_stop`, `_apply_sector_dial`, `_note_refusal`, `shipped_stop_rule`, `shipped_stop_level_basis`, `_target_note`) is a keyword-only constructor argument; the held `OrderBuilders` part (`src/portfolio_constructor/orders.py`; since 2026-10-04 HELD by `PortfolioConstructor`, which inherits from nothing, with the shims installed by `src/portfolio_constructor/assembly.py`) builds the entry builder per call from live collaborators, and no collaborator is itself a lifted method so the shim cannot recurse. Witness: `tests/test_portfolio_constructor_boundary.py`; the drop-path guard skips thin shims so it scans the moved bodies, not the shims. The stop methods were lifted separately into `entry_stop/resolver.py` (`EntryStopResolver`); still inline on `_StopMixin`: `_stop_atr_multiple`, `_level_backing_stop`, `_derive_structural_stop_no_atr`, `_reward_risk_at`, `shipped_stop_rule`, `shipped_stop_level_basis`, `_resolve_stop`, plus the risk-plan / sector-dial / weights methods on `PortfolioConstructor` itself.

**The number-ledger's definition-site scanner is a boundary (2026-10-04, first number-sources instalment).** `src/number_site_scan.py` holds the pure scanner layer lifted verbatim from `src/number_sources.py`: `NumberSite`, the three classifier constants the scanner reads (`CONFIG_CLASS_SUFFIX`, `NEUTRAL_VALUES`, `FACTOR_BAND`), the literal/constant readers (`_numeric`, `_module_constants`, `_imported_constants`, `_leaves`, `_field_default`) and the rule-(e) shape scanner with its helpers (`_scan_extended_shapes`, `_qualified_scopes`, `_factor_operands`, `_own_nodes`). Every input arrives as an argument (a parsed tree, module name, bound names), so the seam is exercised from a tree built in a test with no ledger, scope list or settings file behind it; the constants moved WITH the code because the scanner reads them and they are immutable, and `number_sources` re-exports every moved name in its one marked block so all imports and the `collect_sites` / `_scan_module` callers are unchanged. `_scan_module`, `collect_sites`, the scope list, the ledger loader and `audit` stay in `number_sources.py`, which is 1,145 lines after it (was 1,456); the new module is 345 lines. Witness: `tests/test_number_site_scan_boundary.py`.

**The deterministic trail's arithmetic now IS three boundaries (2026-10-04).** `src/risk/trailing.py` (928 -> 369 lines) keeps the contract: the proposal/evaluation types, the `TRAIL_CODE_*` names and every ratified number (each pinned there by the number ledger and `tests/test_pivot_window_independence.py`). The bodies moved verbatim, AST-identical, into `src/risk/trail_structure.py` (swing pivots), `src/risk/trail_range_ratchet.py` (the Type A R-ratchets) and `src/risk/trail_evaluate.py` (`evaluate_trailing_stop` / `compute_trailing_stop`), each importable and exercisable alone from a stub bar; `tests/test_trailing_parts_boundary.py` is the witness. The facade resolves the moved names through ONE lazy module `__getattr__`, so `from src.risk.trailing import X` and `patch("src.risk.trailing.X")` are unchanged for every caller. No logic moved by a line; this split exists so the stop-ratchet work can land under the file-size guard without raising it.

**Five protection pieces now ARE boundaries (2026-10-02).** `src/protection/`
holds `OwnerAlerts`, `SellFinalization`, `FillReconciler`, `RepegDrain` and
`CoverageElection`, each a standalone class taking every collaborator as a
keyword-only constructor argument (the `src/sessions/` pattern);
`ProtectionMixin` keeps a thin same-named shim per method. All five pass
`check_boundary`; `tests/test_protection_boundary.py` is the witness. What
followed on 2026-10-04: `ProtectedSell` and `ReprotectRecords` joined `src/protection/`
(`tests/test_protection_parts_boundary.py` is the witness). `CoverageRepair`,
`ExitRelief`, `RestoreDrain` and `ExDividends` import the broker seam
(`src.execution`, a frozen importer list the layering guard enforces) so they are
standalone classes built the same way but kept in `src/pipeline_protection.py`.
`ReprotectResidual` moved to `src/protection_parts/reprotect_residual.py` (PR 1593).
`_reconcile_stop_coverage` (600 lines) is still a mixin body: PR 1223 uncrams one
of its lines, and the statement-cram ratchet keys by class.method, so it keeps its
original identity until that lands. Host attributes a body assigns or reads with a
default (`_last_stop_clear_refusal`, `_unsettled_exit_orders`, `db`) go through a live
get/set view (`_HostState`), never a copy.

**No mixin can be constructed alone.** `grep -n 'def __init__'` across
`src/pipeline_protection.py`, `src/pipeline_exits.py`, `src/pipeline_intraday.py`
and `src/pipeline_risk_gate.py` returns nothing. None of them defines a
constructor; each is a bag of methods that only becomes live once mixed into
`TradingPipeline`, whose `__init__` sets the attributes they read.

**Every test needs the whole desk.** `grep -rl TradingPipeline tests/` matches
115 of 330 test files. Only 4 test files mention `Mixin` at all.

**A two-way import exists between `pipeline_stages` and `pipeline_sizing`** — and
seven more besides. An AST pass over every `ImportFrom` node under `src/`
(module-level and function-level) found 8 mutually-importing module pairs:

| A | B |
|---|---|
| `src.config` | `src.live_capital_preflight` |
| `src.coverage_watchdog` | `src.execution.scale_in` |
| `src.execution.broker` | `src.execution.scale_in` |
| `src.execution.broker` | `src.notifier` |
| `src.models` | `src.seat_heal` |
| `src.notifier` | `src.trader_feed` |
| `src.pipeline` | `src.pipeline_stages` |
| `src.pipeline_sizing` | `src.pipeline_stages` |

**25 tracked Python files exceed 2,561 lines.** `git ls-files '*.py' | xargs wc -l`
returns exactly 25 such files. The largest is `src/execution/broker.py` at 7,046
lines, then `src/storage/db.py` (6,270), `src/models.py` (5,265),
`src/pipeline.py` (5,004), `src/portfolio_constructor.py` (4,649). Ten of the 25
are test files.

**Hub symbols with no owner.** From the same AST pass, counting distinct importing
modules per imported symbol:

| Symbol | Distinct importers |
|---|---|
| `src.util.time.et_today` | 15 |
| `src.models.TradeDecision` | 14 |
| `src.trading_calendar.et_today` | 13 |
| `src.models.NewsIntelligenceReport` | 12 |
| `src.agents.base.BaseAgent` | 11 |
| `src.models.TechAnalysisResult` | 11 |
| `src.cost_circuit.PaidAnalysisSuspended` | 9 |
| `src.config.AppConfig` | 9 |
| `src.data.market.MarketDataProvider` | 9 |
| `src.pipeline_stages._record_pipeline_event` | 8 |
| `src.pipeline_stages._persist_evidence` | 7 |
| `src.pipeline.TradingPipeline` | 7 |
| `src.execution.broker._get_sector` | 6 |

Two of these deserve naming now. `et_today` exists **twice**, in
`src.util.time` (15 importers) and in `src.trading_calendar` (13 importers): the
single most-imported name in the codebase is ambiguous at the import site.
`_record_pipeline_event` and `_persist_evidence` are private-by-name helpers in
`pipeline_stages` imported by 8 and 7 other modules respectively — a de facto
public journal API wearing an underscore.

### 0.2 Wrong, as measured

**The `self.` counts in the brief do not reproduce.** The brief says
`pipeline_protection.py` makes 71 `self.` references and `pipeline_exits.py` 60.
Measured two ways:

| File | Raw `self.` occurrences (`grep -c`) | Distinct attribute names (AST) |
|---|---|---|
| `src/pipeline_protection.py` | 161 | 39 |
| `src/pipeline_exits.py` | 124 | 35 |

Neither figure is 71 or 60. The raw counts are roughly twice what the brief
states; the distinct-name counts are roughly half. The brief's numbers are not
reproducible from this commit by either method and should not be quoted again.

**"Every piece reaches into every other's state" is overstated.** An AST pass
that classifies each `self.<attr>` as either (a) a method defined on another
mixin or on `TradingPipeline`, or (b) a data attribute, gives:

| Mixin file | Distinct `self.` names | Methods owned by another mixin | Data/collaborator attributes |
|---|---|---|---|
| `pipeline_risk_gate.py` | 6 | 1 | 3 (`db`, `risk_engine`, one constant) |
| `pipeline_admission.py` | 10 | 1 | 4 (`broker`, `config`, `db`, `market`) |
| `pipeline_delever.py` | 20 | 8 | 3 (`broker`, `config`, `db`) |
| `pipeline_prompt_facts.py` | 26 | 3 | 10 |
| `pipeline_research_continuity.py` | 27 | 1 | 6 |
| `pipeline_exits.py` | 35 | 13 | 9 |
| `pipeline_protection.py` | 39 | 4 | 6 |
| `pipeline_intraday.py` | 61 | 28 | 15 |
| `pipeline.py` (the residue) | 133 | 47 | 40 |

Four of the eight mixins make **one to four** calls into another mixin's methods.
Their coupling is overwhelmingly to four injectable collaborators already
constructed in `TradingPipeline.__init__` — `broker`, `db`, `market`, `config`.
Only `pipeline_exits` (13) and `pipeline_intraday` (28) are genuinely entangled
with their siblings. This is the single most important correction in this
document: the problem is **not uniform**, so the conversion is not uniform
either. Roughly half the surface is cheap and the other half is not, and the
plan in section 4 is ordered on exactly that measurement.

**"`src/execution/broker.py` was never in any plan"** — not verified here.
`docs/PIPELINE_SPLIT_PLAN.md` mentions the word "broker" 11 times; a grep for the
path `execution/broker` in that file returns nothing. That is weak evidence, not
proof that it was never planned, and nothing in this document rests on it.

### 0.3 A finding the brief did not contain

**The domain-rules layer already imports upward, at runtime.** Two deferred
imports inside function bodies:

- `src/risk/rules.py:2298` — `from src.execution.broker import _get_sector, _sector_resolution_status_for`
- `src/risk/exit_guard.py:1695` — `from src.agents.portfolio_manager import PortfolioManagerAgent`

Function-level imports do not show up in a module-header scan, which is why this
kind of violation survives. The risk rules — the layer that is supposed to know
nothing of plumbing — reach into the broker adapter and into an LLM-backed agent.
Any dependency check that only reads the top of the file will declare this clean.
The check specified in section 7 walks the AST for exactly this reason.

**The split so far is partly notional.** `src/stage_decision.py`,
`src/stage_execution.py` and `src/stage_risk.py` have 1, 2 and 3 distinct `self.`
attributes, and in each case the attribute is `self._pipeline`. These files hold
a class that stores the pipeline and calls back into it. `src/stage_risk.py:4`
says the class body "is byte-for-byte the text that used to live in" the
pipeline. They moved text, not dependencies. `src/pipeline_stages.py:99-104`
builds a re-export mirror (`**{n: _pipeline_sizing for n in vars(...)}`) so that
old import paths keep resolving. That mirror is the mechanism by which the
two-way import in the table above exists. These are honest transitional devices,
but they are the reason the owner is right that the split so far is cosmetic:
a file boundary that preserves every original import path has not moved a
dependency.

---

## 1. The layers

Derived from what this system actually does — read evidence, form a judgement per
name, size and gate it against risk doctrine, place and protect orders at a
venue, and record and narrate all of it — not from a generic template. Seven
layers, numbered by depth. **A module may import only from a strictly lower
number.** Equal-number imports are allowed only within the same package and only
where stated.

```
  L6  Surfaces        api routes, status board, dashboard, notifier rendering
       |
  L5  Composition     TradingPipeline, scheduler, api/deps  <- the ONLY layer
       |                                                       that names an L3
  L4  Services        protection, exits, admission, delever, prompt facts,
       |              research continuity, intraday, rotation execution
       |
  L3  Adapters        broker (Alpaca), storage/db, telegram, market/news/
       |              earnings/macro providers, agents (Anthropic seats)
       |
  L2  Ports           the interfaces an adapter must satisfy
       |
  L1  Doctrine        risk rules, sizing arithmetic, exit triggers, stop geometry
       |
  L0  Kernel          time and calendar, units, value types, constants
```

The two rules the owner asked for, stated precisely:

- **Domain rules know nothing of plumbing.** L1 may import L0 and nothing else.
  No `src.storage`, no `src.notifier`, no `src.execution`, no `src.agents`, at
  module level or inside a function body.
- **Plumbing knows nothing of the broker or the LLM providers.** L4 Services may
  import L0, L1 and L2, and may **not** import any L3 module. A service that
  needs to sell receives an object satisfying the order port; it never imports
  `src.execution.broker`. A service that needs a seat's judgement receives an
  object satisfying the seat port; it never imports `src.agents.*`.

L5 Composition is the single place where a concrete adapter is named and handed
to a service. That is what makes the rule enforceable: there is exactly one file
to read to know what the desk is actually wired to.

---

## 2. What belongs in each layer, with real examples

### L0 Kernel
**Belongs.** Exchange-time arithmetic, the trading calendar, pure value types,
named constants.
*Here today:* `src/util/time.py`, `src/trading_calendar.py`, `src/models/`
(47 importing modules, measured), `src/risk/constants.py` (14).

**Must never be here.** Anything that reads a file, a socket, a database or an
environment variable. Any type whose construction requires a live account.
*Violation to fix:* `src.models` and `src.seat_heal` import each other (AST pass).
A value-type module must not depend on a healing routine; the shared type belongs
in `models`, the behaviour in L4.
*Violation to fix:* `et_today` is defined in two L0 modules with 15 and 13
importers. One must become a re-export of the other, with the duplicate removed.

### L1 Doctrine
**Belongs.** Decisions expressible as a function of numbers and value types:
gross-exposure limits, gap-adjusted risk per share, stop geometry, the
alignment-exit test, share rounding and fractional eligibility.
*Here today:* `src/risk/rules.py`, `src/risk/constants.py`,
`src/risk/exit_trigger.py`, `src/risk/trailing.py`, `src/pipeline_sizing.py`
(426 lines, **0** `self.` references measured — already pure, already a correct
L1 module, and the best evidence that this layering is reachable).

**Must never be here.** A broker call, a database handle, an LLM call, a
notifier, wall-clock `now()` taken implicitly rather than passed in.
*Violations measured:* `src/risk/rules.py:2298` imports `_get_sector` from the
broker adapter; `src/risk/exit_guard.py:1695` imports `PortfolioManagerAgent`.
Both are L1→L3 and both are illegal under this rule.

### L2 Ports
**Belongs.** Narrow interfaces, defined in terms of L0 types: an order port
(place, amend, cancel, read positions), a ledger port (append an event, persist
evidence, read recent decisions), a quote port, an evidence-provider port, a seat
port, an announcement port. Protocols or ABCs, no logic.
*Here today:* nothing. This layer must be created, one port at a time, as each
service in section 4 is converted. It is not a big-bang deliverable.

**Must never be here.** Any implementation. Any import of `src.execution`,
`src.storage`, `src.agents`, `src.data`. A port that mentions Alpaca, SQLite,
Telegram or Anthropic by name is not a port.

### L3 Adapters
**Belongs.** One module per outside system, each implementing one port: the
Alpaca venue client, the SQLite ledger, the Telegram sender, each market/news/
earnings/macro provider, each analyst seat.
*Here today:* `src/execution/broker.py` (7,046 lines), `src/storage/db.py`
(6,270), `src/notifier/` (3,984), `src/data/*` (24 files), `src/agents/*`
(14 files).

**Must never be here.** Doctrine. A sizing rule or a gating threshold living
inside the broker adapter is in the wrong layer; so is reference data that no
venue is needed to compute — `src.execution.broker._get_sector` has 6 distinct
importers (measured), none of which wants a broker session.
**Adapters must not import each other.** Measured violations:
`src.execution.broker` ↔ `src.notifier` and `src.execution.broker` ↔
`src.execution.scale_in`.

### L4 Services
**Belongs.** Stateful collaborators that combine doctrine, evidence and seats to
produce a decision or an order intent: protection, exits, admission, delever,
prompt facts, research continuity, intraday, rotation execution. Each has an
explicit constructor. Each is one noun with one job.
*Here today:* the eight mixins, which are this layer wearing the wrong shape.

**Must never be here.** An import of any L3 module — that is the rule's whole
content. Also never: a reference to `TradingPipeline`, by import, by type hint,
or by `self._pipeline`. `src/stage_decision.py`, `src/stage_execution.py` and
`src/stage_risk.py` each hold `self._pipeline` (measured) and therefore sit in
L4's position without meeting L4's rule.

### L5 Composition
**Belongs.** Construction and sequencing only: build each adapter, hand it to the
services that declared that port, run the stages in order, handle the session
lifecycle. The only layer permitted to name a concrete adapter.
*Here today:* `src/pipeline.py` (5,004 lines; 133 distinct `self.` names, 40 of
them collaborator attributes, measured), `src/scheduler.py`, `src/api/deps.py`.

**Must never be here.** Any rule anyone could want to test on its own. The
success measure for this layer is that it shrinks until it is almost entirely
wiring.

### L6 Surfaces
**Belongs.** `src/api/*`, `scripts/status_board.py`, the dashboard, the rendering
half of `src/notifier/`. Read state, render it, accept a command and pass it
down.

**Must never be here.** A decision. A surface that decides is a service that got
lost.

---

## 3. The definition of a boundary

> **A boundary exists where a piece can be constructed and exercised on its own,
> without building a `TradingPipeline`.**

Stated as a test that can fail, applied to every module proposed below. A module
`M` passes only if all five hold:

1. **It has a constructor.** `M`'s class defines `__init__`, and every
   collaborator it uses is a parameter of that constructor. Measured today: none
   of the four mixins checked defines `__init__`.
2. **It has no `self` attribute that is not set in that constructor.** Checked by
   AST: the set of `self.<attr>` reads is a subset of the attributes assigned in
   `__init__` plus the methods defined on `M` itself. Today
   `pipeline_intraday.py` reads 28 methods it does not define (measured); that is
   the failure this clause catches.
3. **It does not import `src.pipeline`**, at module level or inside any function
   body, and does not accept a `TradingPipeline` as a parameter under any name.
4. **It imports no module from a layer at or above its own**, by the AST walk in
   section 7 — which inspects function-bodied imports, because the two live L1
   violations in section 0.3 are both function-bodied.
5. **At least one test constructs `M` with explicit stand-ins for its declared
   ports and exercises its public entry point, and that test file does not import
   `TradingPipeline`.** This is the clause that makes the other four worth
   having, and the only one that cannot be satisfied by rearranging text.

Clause 5 is the falsifier. A module that passes 1–4 but has no such test has not
been proven separable; it has only been asserted to be. 115 of 330 test files
import `TradingPipeline` today (measured) — that ratio is the honest scoreboard
for this whole programme, and it should be reported after every step.

---

## 4. The conversion route

Fifteen steps. Ordered by measured entanglement — fewest cross-mixin method calls
and fewest collaborator attributes first — so that the cheap steps build the
ports the expensive steps will need. Each step is independently shippable,
independently revertible, and leaves the desk working if the next step never
happens.

Every step's equivalence proof has the same shape unless stated otherwise:
the extracted methods move **byte-for-byte**; the old call site becomes a
delegation to the new object constructed in `TradingPipeline.__init__`; the
existing test suite for that area runs unchanged and green; and one new test
constructs the new object alone under clause 5 of section 3. A step that cannot
move its code byte-for-byte is not this step — it is a rewrite, and it needs its
own review.

**MONEY** marks a step that touches order placement, stop losses or position
sizing. Those steps need a second reviewer, an explicit before/after diff of
every order field, and a dry-run against the rehearsal account before merge. They
are deliberately placed late, behind the ports the cheap steps create.

### Phase A — make the rule checkable (no code moves)

**Step 1. Layer manifest and dependency check.**
*Moves:* nothing. Adds a manifest mapping each `src/` module to a layer number,
and a script that AST-walks every `ImportFrom` including function-bodied ones and
reports upward edges. Ships in report-only mode: it prints the current violations
and exits zero.
*Constructor:* n/a.
*Proof:* the script's first run output is committed as the baseline. It must
reproduce the 8 two-way pairs and the 2 L1 violations in section 0 exactly. If it
does not, the manifest is wrong and the step is not done.

**Step 2. Boundary-test harness.**
*Moves:* nothing. Adds the clause 1–5 checker from section 3 as a test helper, and
the running count of test files importing `TradingPipeline` (115 today) as a
reported metric with a ratchet that may only go down.
*Proof:* the harness passes `src/pipeline_sizing.py` (0 `self.` references,
measured) and fails every mixin. Since 2026-10-04 it also passes
`src/pipeline_cost_gate.py` and `src/pipeline_halt_gates.py` (the run gates lifted out of
`TradingPipeline` as duck-typed functions; `tests/test_pipeline_run_gates_boundary.py` drives
them from `SimpleNamespace` stubs). Also since 2026-10-04 it passes
`src/pipeline_seat_evidence.py`: the seat-evidence block (10 names, 374 lines, moved
verbatim out of `src/pipeline_stages.py`: the nomination-to-decision join, seat-stance
rows, the raw seat-nomination gather, the dual-shape macro read and its parse-failure
stash, the risk seat's per-symbol event and edit snapshot, the advisory-only
`scale_all_buys` record, the SEC sale-census probe) — duck-typed functions over plain
arguments, re-exported through the one lazy table in `pipeline_stages` so every old
import path and patch target still resolves to the same object;
`tests/test_boundary_pipeline_seat_evidence.py` drives them from stubs. The new file
is exactly 400 lines because `scripts/file_size_guard.py` (replaced 2026-10-08 by fixed ruff
limits in `pyproject.toml`) refused any NEW file over 400 lines — that guard is why this block was taken alone and not combined with another.
It is deliberately NOT in `SCOPED_PATHS` (no ledgered number site in the block). What
remains in `pipeline_stages.py` after this and the candidate-records split is the
levels-coverage / protection-alert block (`_check_levels_coverage`,
`_alert_owner_protection_failed`, `_alert_holding_discipline_block`, ~255 lines) and
the sizing-price / book-risk helpers (`_book_risk_inputs`, `_today_sizing_price`,
`_session_gross_ceiling`, ~180 lines), plus imports, the re-export table and mirror.
`_today_sizing_price` returns None only for a MEASURED absence; a read that fails raises
`SizingPriceUnavailable` (`src/refusal_errors.py`), and every caller goes through
`src/sizing_refusal.py`, which refuses the entry as `sizing_price_unreadable`.
Also since 2026-10-04 it passes `src/pipeline_candidate_records.py` (6 names, 331 lines:
the execution-skip row, the typed pipeline-event row, the PM candidate accounting with
its one paid re-ask, and the never-fatal heal record) and `src/pipeline_soft_exit_records.py`
(7 names, 298 lines: the missing-falsifier test and admitted-to-book filter, the item-78
mechanical-restore and per-name heal records, the missing-after-retry row, the refusal
count and the BUY/SHORT isolate), both moved verbatim out of `src/pipeline_stages.py` on
the same duck-typed-arguments pattern and re-exported through the same one lazy table.
The candidate-records module sits BELOW the stage module: it imports its helpers from
their own homes (`_persist_evidence` now lives in `src/pipeline_stage_helpers.py`) and
`pipeline_stages.py` imports it at the top, so the import-cycle guard stays clean;
`tests/test_boundary_pipeline_candidate_records.py` and
`tests/test_boundary_pipeline_soft_exit_records.py` drive them from stubs. One body was
deliberately NOT moved: `_record_scale_in_window_closed` imports the broker seam
(`src.execution.scale_in`) in its body, which the import-layering guard refuses in a part,
so it stays in `pipeline_stages.py`. The earlier attempt at this split (PR 1229) was
closed on a conflict with main's item-78 falsifier work; this version lifts from the
post-item-78 bodies.
A harness that passes something it should fail
is not yet a harness.

### Phase B — fix the layer violations that are already there (small, high value)

**Step 3. De-duplicate `et_today`.**
*Moves:* one of the two definitions becomes a re-export; the 28 importing modules
(15 + 13, measured) are left untouched in this step.
*Proof:* the dependency check from step 1 shows one fewer duplicated kernel
symbol; no behaviour test changes.

**Step 4. Pull `_get_sector` out of the broker adapter.** **MONEY-adjacent** —
sector resolution feeds the exposure ladder.
*Moves:* `_get_sector` and `_sector_resolution_status_for` from
`src/execution/broker.py` to an L0 reference-data module. The broker re-exports
them for one release.
*Constructor:* n/a — these are pure lookups.
*Proof:* the illegal L1→L3 import at `src/risk/rules.py:2298` disappears from the
step-1 report. All 6 measured importers resolve to the new module.

**Step 5. Break `src/risk/exit_guard.py` → `src/agents/portfolio_manager`.**
*Moves:* the agent is no longer imported at `exit_guard.py:1695`; whatever it is
used for becomes a parameter the caller supplies.
*Proof:* the second L1 violation disappears from the step-1 report; exit-guard
tests run with a stand-in in place of the agent.

**Step 6. Promote the journal helpers to a port.**
*Moves:* `_record_pipeline_event` (8 importers) and `_persist_evidence` (7)
become the two methods of an L2 `EventJournal` port, with the current
`pipeline_stages` functions as its L3 implementation.
*Constructor:* every later service takes `journal: EventJournal` instead of
reaching for `self.db` and the module-level helper.
*Proof:* recorded events for one full session are byte-identical before and after.

### Phase C — the cheap services (measured 1 cross-mixin call each)

**Step 7. `RiskGateMixin` → `RiskGate`.** **MONEY** — this is the gate that sizes
down and refuses.
*Measured:* 954 lines, 6 distinct `self.` names, 1 foreign method call.
*Constructor (as built, 2026-10-01):* `RiskGate(*, risk_engine, db, sweeper)`.
The journal port could NOT replace `self.db` here: the gate's one `db` use is
`insert_agent_log` (an `agent_logs` row, read back by `scripts/replay_decision.py`),
which is not on `EventJournal`; routing it through the journal is a body change
and is left for its own reviewed step. The one foreign call (`_sweeper`) is a
constructor parameter. *Converted to a built collaborator, 2026-10-05:* the
delegating `RiskGateMixin` (`src/pipeline_risk_gate_mixin.py`) is DELETED.
`build_risk_gate(config, risk_engine, db, sweeper)` in `src/risk_gate_build.py`
builds the gate from values; `TradingPipeline.risk_gate` is a `RiskGateSlot`
descriptor there that reuses the stored gate only while its collaborators are
the pipeline's current ones (tests assign `db`/`risk_engine`/`_sweeper` after
construction). Callers use `pipeline.risk_gate.<name>`;
`tests/test_boundary_risk_gate.py` proves in a subprocess that building the
gate never imports `src.pipeline`.
*Proof:* standard, plus every refusal and every resize produced over a replayed
session must match the pre-change output exactly, field by field. Second reviewer
required.

**Step 8. `AdmissionMixin` → `AdmissionService`. DONE 2026-10-05.**
*Measured before cutting:* the shell was 99 lines forwarding 11 names, and the
seam had 54 external call sites -- the fewest of the remaining mixins (risk
gate 165, delever 222, exits 300), which is why it was cut next.
*Builder:* `build_admission_service(config, broker, market, db,
sec_form4_provider, portfolio_constructor | constructor_cfg_fn)` in
`src/admission_build.py` -- collaborators BY VALUE, no pipeline.
*Shell:* `src/pipeline_admission_shell.py` DELETED; no forwarding method
survives on `TradingPipeline`, which exposes only the `admission` descriptor.
`_constructor_cfg_or_none` went with it (the shell was its only caller).
*Proof:* `tests/test_admission_service.py` builds and drives the service in a
SUBPROCESS and asserts `src.pipeline` never enters `sys.modules`.

**Step 9. `ResearchContinuityMixin` → `ResearchContinuity`.**
*Measured:* 1,377 lines, 27 distinct `self.` names, 1 foreign call, collaborators
`config`, `db`, `macro_store`, `news_store`.
*Constructor:* `ResearchContinuity(config, journal, macro_store, news_store)`.
*Proof:* standard.

### Phase D — the large but shallow services

**Step 10. `PromptFactsMixin` → `PromptFacts`.** Large. DONE 2026-10-04 as eight parts under `src/prompt_facts/` (see the boundary notes above).
*Measured:* 3,614 lines, 26 distinct `self.` names, 3 foreign calls, **10**
collaborator attributes (`broker`, `config`, `db`, `earnings_provider`,
`macro_store`, `market`, `news_store`, `tech_store`, and two caches).
*Constructor:* ten parameters is too many for one object. This step is therefore
two: first move it behind a constructor taking all ten unchanged, then split it
along the fact families it already serves. Do not attempt both at once.
*Proof:* the rendered prompt facts for a replayed session are byte-identical.
That is a strong, cheap proof and it is the reason this step is safe despite its
size.

**Step 11. `DeleverMixin` → `DeleverService`.** **MONEY** — places sell orders.
*Measured:* 1,282 lines, 20 distinct `self.` names, 8 foreign calls (including
`_submit_protected_sell`, `_full_sell_qty`, `_sweep_symbol`), 3 collaborators.
*Constructor:* `DeleverService(config, orders: OrderPort, journal, protection:
ProtectionService, sweeper)`. The 8 foreign calls split: the order-shaped ones
become the protection collaborator, the rest become injected helpers.
*Proof:* standard, plus a replayed delever session must emit the identical order
intents — symbol, side, quantity, type, limit, time-in-force — with placement
suppressed. Second reviewer and a rehearsal-account dry run required.

### Phase E — the money core

**Step 12. `ProtectionMixin` → `ProtectionService`.** **MONEY**, largest money
step.
*Measured:* 4,438 lines (the 7th-largest tracked Python file), 39 distinct
`self.` names, 4 foreign calls, collaborators `broker`, `db`, `market`.
*Constructor:* `ProtectionService(orders: OrderPort, positions: PositionsPort,
journal, quotes: QuotePort, clock)`. Only 4 foreign calls (`_format_qty`,
`_record_exit_refusal`, `_retired_cash_park_symbol`, `_sweeper`) — shallow for its
size, which is why it precedes exits.
*Proof:* standard, plus a replay in which every stop amendment, cancellation and
resubmission is captured and compared field-by-field, and an explicit check that
no stop moves in a direction it could not move before. Second reviewer and a
rehearsal-account dry run required. Do not combine with any other step.

**Step 13. `ExitEngineMixin` → `ExitEngine`.** **MONEY**, deeply entangled.
*Measured:* 3,754 lines, 35 distinct `self.` names, **13** foreign method calls,
9 collaborators including `portfolio_constructor`, `position_reviewer` and
`risk_manager`.
*Constructor:* `ExitEngine(protection: ProtectionService, orders, positions,
journal, quotes, reviewer: SeatPort, risk: SeatPort, constructor_cfg, clock)`.
Step 12 must land first, because 5 of the 13 foreign calls resolve to protection.
*Proof:* standard, plus every exit decision and every resulting order intent over
a replayed session compared field-by-field, and the alignment-exit verdict
compared per name. Second reviewer and a rehearsal-account dry run required.

**Step 14. `IntradayMixin` → `IntradaySession`.** Largest step overall.
*Measured:* 1,310 lines but **61** distinct `self.` names and **28** foreign
method calls — the most entangled piece in the codebase by both measures, three
times the next worst. Its 15 collaborators include `decision_stage`,
`execution_stage` and `risk_stage`.
*Constructor:* cannot be written today. After steps 7–13, 24 of its 28 foreign
calls resolve to objects that by then have constructors, and the remainder are
session-lifecycle concerns that belong in L5. This step is **mostly deletion**:
it becomes a thin L5 sequencer over services that already exist, which is why it
must be last and why attempting it early would be the single most expensive
mistake available.
*Proof:* a full replayed intraday session produces an identical event journal.

### Phase F — the leftovers

**Step 15. Retire the `self._pipeline` wrappers and add the size backstop.**
*Moves:* `src/stage_decision.py`, `src/stage_execution.py`, `src/stage_risk.py`
hold only `self._pipeline` (measured: 1, 2 and 3 distinct attributes). Once their
bodies' dependencies are injected by steps 7–14, the wrapper is deleted, not
converted. Also retires the `pipeline_stages` re-export mirror at
`src/pipeline_stages.py:99-104`, which closes the measured
`pipeline_sizing ↔ pipeline_stages` cycle.
*Proof:* the step-1 report shows zero two-way pairs involving pipeline modules;
no import path outside `src/` changes.

---

## 5. The honest cost

**Fifteen steps.** Three are large: **step 14 (intraday)**, **step 12
(protection)** and **step 13 (exits)** — by the measured entanglement figures,
28, 4-but-4,438-lines, and 13 foreign calls respectively. Step 10 (prompt facts,
3,614 lines) is large in volume but shallow in coupling, and is explicitly split
into two sub-steps rather than pretended to be one.

Five steps are **MONEY**: 7, 11, 12, 13, and 4 by adjacency. Step 14 inherits
money exposure from everything beneath it. That is a third of the programme under
stronger review, and it is not compressible: the money code is where the
entanglement is, because that is where the desk's actual behaviour lives.

What this plan does **not** promise:

- It does not shrink the codebase. Extracting a service with an explicit
  constructor and a port adds lines before it removes them. Total line count will
  rise through phases C and D and only fall at step 14 and step 15.
- It does not fix the four largest files. `broker.py` (7,046), `db.py` (6,270),
  `models.py` (5,265) and `portfolio_constructor.py` (4,649) are untouched by all
  fifteen steps. Section 6 says why.
- It does not reach a clean dependency graph at any intermediate step. Phases
  A–E leave known violations standing on purpose; the step-1 report is a
  decreasing count, not a passing check, until step 15.
- Steps 12, 13 and 14 cannot be parallelised — 13 depends on 12, and 14 on both.
  That serial chain is the real schedule, and it sits entirely inside the money
  code.
- The 115-of-330 test files that import `TradingPipeline` do not all disappear.
  Many test the sequencing, which is legitimately L5 work.

---

## 6. What will not be converted, and why

**`src/execution/broker.py` (7,046 lines).** It is one adapter for one venue, and
venue code is cohesive by nature: session, auth, retry, rate limiting, order
field translation. Splitting it produces several modules that all need the same
session object, which is the mixin failure again in a new costume. Two carve-outs
only, both already named: `_get_sector` (step 4, 6 importers, needs no session)
and the `broker ↔ notifier` and `broker ↔ scale_in` cycles (measured), which are
layer violations rather than size problems. The remaining bulk stays.

**`src/storage/db.py` (6,270 lines).** One adapter behind one ledger port.
Splitting the SQL risks silent schema drift between halves, and the benefit is
cosmetic: nothing becomes independently testable that a ledger port does not
already make testable. The port is the boundary; the file size is not.

**`src/models.py` (5,265 lines, 47 importing modules).** Value types belong
together — that is what makes them importable from everywhere without creating a
cycle. The one real defect is the measured `models ↔ seat_heal` cycle, fixed by
moving behaviour out of `models`, not by splitting `models`.

**`src/portfolio_constructor.py` (4,649 lines) and `src/cost_circuit.py`
(4,300).** Each is already a single collaborator with a constructor, used by the
pipeline rather than mixed into it. They are large, not glued. They fail no
clause of section 3. Size alone is not a reason to touch working money code.

**The stage wrappers are deleted, not converted** (step 15). Converting a
delegation shim produces a better delegation shim.

**The LLM seats' prompt text.** Prompt text is behaviour. Moving it during a
structural change makes the equivalence proof — identical output on a replayed
session — impossible to interpret. It is out of scope for all fifteen steps.

**The test files over 2,561 lines** (10 of the 25 measured). They get rewritten
as a consequence of steps 7–14, not as a target.

---

## 7. How this prevents regrowth

The mechanism is the dependency rule and the boundary test. File-size limits are
a backstop and nothing more.

**The dependency check (step 1) is the primary guard.** It AST-walks every
`ImportFrom` in `src/`, including imports inside function bodies, maps both ends
to a layer in the manifest, and fails on any upward edge. Function-bodied imports
are the whole point: both live L1 violations in section 0.3 are deferred imports
inside functions, invisible to any header scan, and the second-oldest trick for
re-creating a cycle after someone breaks it. The check runs in CI on every pull
request. A new module with no manifest entry fails; that is deliberate, because
the alternative is a layer assignment nobody ever made.

**The boundary test (step 2) is the guard that cannot be gamed.** Clauses 1–4 are
structural and a determined author can satisfy them while changing nothing real.
Clause 5 — a test that constructs the module with stand-ins and never imports
`TradingPipeline` — cannot be satisfied by rearranging text. The count of test
files importing `TradingPipeline` (115 of 330 today, measured) is reported on
every run and ratchets downward only.

**Why this catches what the last attempt did not.** The split so far produced
`stage_decision.py`, `stage_execution.py` and `stage_risk.py`, which hold nothing
but `self._pipeline` (measured), and a re-export mirror at
`src/pipeline_stages.py:99-104` that preserves every original import path. Both
pass any file-size rule. Both fail clause 3 of the boundary test on the first
run. That is the difference between the two mechanisms, and it is why the
file-size ceiling is listed last.

**The backstop.** A file-size ceiling in CI, set above the current largest
exempted file so it never blocks the work in section 6, and lowered only when a
step actually reduces the maximum. A ceiling that fires constantly gets
suppressed; a ceiling that fires once a year gets read. It exists to catch a new
5,000-line file, not to force the old ones apart.

**The reporting discipline.** After every step, three numbers go in the pull
request: upward dependency edges remaining, two-way import pairs remaining
(8 today, measured), and test files importing `TradingPipeline` (115 of 330
today, measured). If a step does not move at least one of them, it was not a
conversion step.

---

## 8. The Sentinel seams

`docs/FUTURE.md` specifies a separate watchdog ("Sentinel") on another
provider's host, built only after the desk is operational. It also distinguishes
the full owner Mission Control (on this VPS, Tailscale-only) from a limited
wife-and-friends guest dashboard (on its own password-protected VPS). The guest
VPS and Sentinel VPS are separate. The two QAMC seams go in during the rebuild
so those later builds are connections, not surgery.

**Inbound — a flag, never a call.** Already built: the broker layer refuses
every order while the file at `RiskConfig.kill_switch_path` exists
(`src/execution/broker.py::_kill_switch_active`, existence check only, no
content read). Not yet built: a second, exits-only flag — today the one flag
halts entries AND exits alike, so "freeze new trades but let protection act"
has no inbound expression.

**Outward — the signed snapshot** (`src/sentinel_seam/`, L4, imports nothing
from `src`). `build_snapshot` is a pure function of PASSED-IN state: schema
version, heartbeat, desk code version (git short SHA, else package version,
else the literal "unknown"), trading state, expected positions, expected
protections, risk state, last reconciliation, recent trades, cost spent.
`scrub_snapshot` runs before signing and removes account identifiers, keys
and tokens, filesystem paths, hostnames, e-mail and IP addresses, by key name
and by value shape; the committed test feeds it one of each. `sign_snapshot`
seals the scrubbed body with HMAC-SHA256 under a key from
`QAMC_SNAPSHOT_SIGNING_KEY`; with no key the block reads
`{"scheme": "unsigned", "value": null}` — explicit, never a fake seal.
`SnapshotPublisher` takes every collaborator keyword-only and drops the JSON
atomically to a local path. The future Sentinel receives the signed operational
snapshot; the future guest dashboard receives only a scrubbed, limited view.
Neither remote host reads QAMC's database or calls back into QAMC. Mission
Control remains the local/Tailscale read-side and does not depend on this remote
delivery path.

**Deliberately NOT built yet:** the push to the drop point (no network call),
any schedule or daemon, the Sentinel reader, the guest dashboard, the
exits-only flag, and the composition-root call that gathers live state and
calls `publish()` — wiring that touches the session scheduler, so the seam
ships unwired.

## 9. Sentinel seams (recorded, read by nothing yet)

Two durable records exist so the future off-box watchdog (docs/FUTURE.md,
"Sentinel" and the erratic-behaviour breaker) has something to read. Neither
adds behaviour; both record what already happens. Nothing in the desk reads
either of them yet.

- **Order attempts** (`src/sentinel/order_attempts.py`, table `order_attempts`):
  one row per attempt the execution stage already reports through its `order`
  lifecycle event -- time, side, symbol, quantity, outcome (submitted / rejected /
  submit_unknown), broker order id, run id, and the deterministic client order
  id read from the broker payload. That id is NULL until the adapter surfaces
  it in the dict it returns (it does not today). Cancels issued inside
  `src/execution/` never reach this funnel, so a wrapper on the trading client
  (`src/sentinel/cancel_attempts.py`, installed by the pipeline) writes one row
  per broker cancel instead -- `cancelled`, or `cancel_failed` with the error text.
- **Last reconciliation** (`src/sentinel/reconciliation.py`, table
  `reconciliation_runs`): one row per reconciler run, written at the return
  site of the stop-coverage, recorded-stop-level, orphan-submit and stop-out
  reconcilers with the result they already return. A reader gets three distinct
  answers -- `agreed`, `disagreed`, `not_run` -- and the third is never
  collapsed into either of the others.

Both tables are created by one appended, idempotent migration step
(`src/storage/schema/sentinel_tables.py`).

## R-multiple of a closed position (scale-in rule)

A position's risk is the sum over its entry lots of (lot filled qty x |lot fill price - that lot's own
initial stop|). R = realised P&L / that total risk. If any lot lacks a readable initial stop, or its stop
is on the wrong side of entry, R is None ("R unknown"); another lot's stop is never substituted. A
single-lot position gives the same R as before. One path: `summarize_closed_position` sets
`ClosedPosition.r_multiple`; the conviction ledger scorer and trade review both read it.

Open positions: unrealised R = unrealised P&L on the shares still held / the same per-lot risk sum scaled
by held qty / total entry qty. A partial sell therefore leaves R at the same price unchanged. Closed
positions use the full-lot sum, because realised P&L covers every share sold.

## Morning research order and screened names (owner ruling 2026-10-14)

The morning stage builds one ordered list that the research seats, the Tech batch (split in input order)
and the news candidate list follow: held names, then this run's SEC Form 4 admissions, then the configured
universe, then screened names best score first. Screened names are last night's nightly-filter passers
(the `src/universe_daily.py` record, read by `universe_screen.rank_passers`), ordered by the score K that
record carries. Only the top `universe_screen.morning_top_n` = floor(`morning_budget_usd` /
`morning_cost_per_name_usd`) are sent. Every other passer gets a dropped-name pipeline event through
`record_dropped_name`: `not_sent_weaker_rank` with its rank, or `not_sent_unscored` with the record's own
reason when K is None. A held passer is coverage and is never capped. No complete record for the last
session is a morning fault and no screened name. `nominations.max_per_seat_per_run` still caps each
nominating seat and no longer caps screened names. All of it sits behind `universe_screen.enabled`.

Screened names reach a thesis seat (2026-10-19). The conviction bar needs a name-level seat (news,
earnings or smart money) with a directional thesis, so a screened name the chart seat likes is refused
unless one of them reads it.
Macro, technical and earnings run in parallel; the news and smart-money seats start once the technical
seat has answered, each still as one call. It reads every name on the shortlist the chart seat read, in this order:
held names, then names the chart seat rated buy or sell, then the rest. There is no name or headline
count: each name keeps its headlines since the previous session's close (exchange calendar), only the
request throttle `news.per_symbol_requests_per_second` remains, and a name with no headline of its own
carries the reason (fetch failed / none in window / merged into a wire copy) in the news coverage line
the seat reads. Witness: `tests/test_news_reads_shortlist.py`. The pre-market earnings pass checks the configured
universe, Form 4 admission-eligible names, then the screened names this morning will admit (held
passers plus the top `morning_top_n` others), so a new filing for one is analysed before the open; only
new filings are analysed. Witness: `tests/test_screened_names_reach_thesis_seats.py`.

The smart-money seat (2026-10-19) reads the same shortlist in the same order. Admission (external
candidates), insider-cluster detection and the census records read EVERY fetched insider and congress row
in its lookback window; there is no row cap. The seat gets one summary line per shortlisted name, built
with a pandas groupby (`src/data/smart_money_summary.py`): net insider dollars, buy and sell counts,
opportunistic and routine counts, the purchase cluster, the latest insider buy and congress buy and sell
counts. A shortlisted name with no row is listed with its reason ("no insider or congress filings in
window"), also written to the run's `scan_summary` evidence row. The former 40-row cut
(`max_observations`) and its 5 reserved cluster slots were deleted; 105 names measure about 9k tokens.

**Admission and the keep rule follow the desk archetype (owner ruling 2026-10-19, docs/OUTCOME.md).**
- Admission (R7, `src/risk/conviction_bar.py`): the chart seat confirms the direction AND at least one
  name-level seat (news, earnings, smart money) took a supported side in it. Shorts mirror longs.
- Macro is never a supporter and never opposition; it tilts size only. The 9 Oct sector-macro vote is gone.
- An opposed name-level seat no longer blocks: `seat_objections` turns it into an objection (seat,
  direction, reason) shown to the planning AI. A buy or short over an objection is dropped with gate
  `objection_unanswered` unless the target carries an `ObjectionAnswer` with decision "overrule"
  (`_drop_unanswered_objections`, decision grounding). Until the AI is told about the field, every
  opposed name stays refused.
- R5 (net source score) is retired from eligibility. The constructor's §9.4 floor is now
  `entry_floor_score`: chart for + name-level seats for - chart against (macro and opposed name-level
  seats are not subtracted). Its refusal never sells a holding.
- Keep rule (`src/exits/same_rules_gate.py`): a holding is kept while the chart is not against it AND
  every opposing name-level seat has a written overrule on record (`objection_answers_from_db`). A
  catalyst is needed at entry only. An unanswered objection is the position reviewer's call (answer
  or sell), never a mechanical sale. Broker stops and the confirmed-structure-break exit never pass
  through this gate.
- `scripts/replay_hold_rule.py DB` replays both rules over the current book read-only.
- Witnesses: `tests/test_archetype_admission.py`, `tests/test_archetype_keep_rule.py`,
  `tests/test_replay_hold_rule.py`.

Where N comes from. B = `morning_budget_usd` = the owner's $7 a month (2026-10-14) / 21 sessions = $0.33,
NEW money added to `llm_cost_circuit.daily_cost_limit_usd` (2.75 -> 3.08).
Per-name cost c = $0.0033 is the slope of settled cost against names analysed over the 26 paid morning
portfolio-manager calls 2026-08-25..2026-10-14 (production `agent_logs`), measured 2026-10-14 by the
coordinating session. c is the PM call's marginal cost only; the Tech seat's per-name cost is not in it.
N = floor(0.33 / 0.0033) = 100.
