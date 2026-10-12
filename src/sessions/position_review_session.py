"""Midday/close position review session (moved verbatim from TradingPipeline)."""

from __future__ import annotations

import logging
from src.data.macro import MacroCoverage
from src.cost_circuit import PaidAnalysisSuspended
from src.pipeline_context import RunContext
from src.agents.base import agent_log_kwargs, seat_acceptance_kwargs
from src.trading_calendar import et_now, session_date_key

logger = logging.getLogger(__name__)


class PositionReviewSession:
    """Midday/close position review session (moved verbatim from TradingPipeline)."""

    def __init__(
        self,
        *,
        activate_cost_session,
        adjudicate_target_revision_flags,
        apply_deterministic_trails,
        build_active_state_changes,
        build_calibration_note,
        build_macro_trajectory,
        build_own_recent_decisions,
        build_position_facts,
        build_review_metric_deltas,
        build_trade_grade_summary,
        build_weekly_narrative,
        compute_deployable_cash,
        compute_recent_performance,
        cost_circuit_status,
        drain_pending_protection_restores,
        drain_pending_repegs,
        enforce_gross_ceiling,
        force_delever,
        handle_ex_dividends,
        is_trading_day,
        kill_switch_halt_result,
        load_earnings_analyses,
        midday_execute_llm_actions,
        news_held_symbols,
        paid_suspension_after_late_safety,
        persist_review_metrics,
        reconcile_fills,
        reconcile_orphan_pending_submits,
        reconcile_stop_coverage,
        reconcile_stop_out_fills,
        record_account_snapshot,
        release_retired_cash_park,
        require_paid_analysis,
        risk_review_exits,
        run_news_update,
        substantiate_exit_triggers,
        surface_reconcile_outcomes,
        sweeper,
        symbols_already_trimmed_today,
        sync_positions_from_broker,
        total_pnl_since_reset,
        trade_executed_or_pending,
        broker,
        config,
        db,
        macro,
        macro_store,
        position_reviewer,
        pipeline,
    ) -> None:
        # The §11.2 helpers (`_entry_deployment_budget`, `_session_gross_ceiling`)
        # read the pipeline's ladder resolver, cash-park symbol, broker and
        # config. Missing wiring is a BUILD-time error, never a mid-run one:
        # a raise inside `run` would skip the trails and risk exits that
        # follow the margin-capacity block. Checked directly on the object,
        # not via `_collab`, whose stand-in would hide the absence.
        if not callable(getattr(pipeline, "_resolve_gross_ceiling", None)):
            raise TypeError(
                "PositionReviewSession: `pipeline` has no callable "
                "`_resolve_gross_ceiling` — the reviewer's margin headroom "
                f"cannot be ladder-backed (got {type(pipeline).__name__})"
            )
        self._pipeline = pipeline
        self._activate_cost_session = activate_cost_session
        self._adjudicate_target_revision_flags = adjudicate_target_revision_flags
        self._apply_deterministic_trails = apply_deterministic_trails
        self._build_active_state_changes = build_active_state_changes
        self._build_calibration_note = build_calibration_note
        self._build_macro_trajectory = build_macro_trajectory
        self._build_own_recent_decisions = build_own_recent_decisions
        self._build_position_facts = build_position_facts
        self._build_review_metric_deltas = build_review_metric_deltas
        self._build_trade_grade_summary = build_trade_grade_summary
        self._build_weekly_narrative = build_weekly_narrative
        self._compute_deployable_cash = compute_deployable_cash
        self._compute_recent_performance = compute_recent_performance
        self._cost_circuit_status = cost_circuit_status
        self._drain_pending_protection_restores = drain_pending_protection_restores
        self._drain_pending_repegs = drain_pending_repegs
        self._enforce_gross_ceiling = enforce_gross_ceiling
        self._force_delever = force_delever
        self._handle_ex_dividends = handle_ex_dividends
        self._is_trading_day = is_trading_day
        self._kill_switch_halt_result = kill_switch_halt_result
        self._load_earnings_analyses = load_earnings_analyses
        self._midday_execute_llm_actions = midday_execute_llm_actions
        self._news_held_symbols = news_held_symbols
        self._paid_suspension_after_late_safety = paid_suspension_after_late_safety
        self._persist_review_metrics = persist_review_metrics
        self._reconcile_fills = reconcile_fills
        self._reconcile_orphan_pending_submits = reconcile_orphan_pending_submits
        self._reconcile_stop_coverage = reconcile_stop_coverage
        self._reconcile_stop_out_fills = reconcile_stop_out_fills
        self._record_account_snapshot = record_account_snapshot
        self._release_retired_cash_park = release_retired_cash_park
        self._require_paid_analysis = require_paid_analysis
        self._risk_review_exits = risk_review_exits
        self._run_news_update = run_news_update
        self._substantiate_exit_triggers = substantiate_exit_triggers
        self._surface_reconcile_outcomes = surface_reconcile_outcomes
        self._sweeper = sweeper
        self._symbols_already_trimmed_today = symbols_already_trimmed_today
        self._sync_positions_from_broker = sync_positions_from_broker
        self._total_pnl_since_reset = total_pnl_since_reset
        self._trade_executed_or_pending = trade_executed_or_pending
        self._broker = broker
        self._config = config
        self._db = db
        self._macro = macro
        self._macro_store = macro_store
        self._position_reviewer = position_reviewer

    def run(self, session_type: str) -> dict:
        """Unified entry for both midday (13:00 ET) and close (15:30 ET).

        Same memory layers, same schema, same agent. Session bias is injected
        via prompt language driven by `session_type`. Everything else — force
        de-lever / ex-div / news / earnings / LLM review /
        emergency liquidate / execution / reconcile — is identical.
        """
        ctx = RunContext.start(session_type)
        run_id = ctx.run_id
        logger.info("=== %s check: %s ===", session_type.capitalize(), run_id)

        if not self._is_trading_day():
            logger.info("%s run skipped: market closed for non-trading day", session_type)
            return {"status": "market_holiday", "positions": 0, "orders": [], "run_id": run_id}

        halt = self._kill_switch_halt_result(run_id, positions=0)
        if halt is not None:
            return halt

        self._activate_cost_session(run_id, session_type)

        # Early-close check. On half-day sessions (day after Thanksgiving 13:00
        # close; July 3 half-day) the launchd-gated midday (13:00-14:30 ET) and
        # close (15:30-15:55 ET) windows fire against a market that's already
        # shut. Every submit would land as rejected; the LLM would still burn
        # tokens reviewing. Skip cleanly when today's session_close has already
        # passed. `isinstance(datetime)` instead of `is not None` because we
        # can only compare to a real datetime — a None or unexpected type
        # (misconfigured mock, broker returning a placeholder) defaults to
        # "proceed and let downstream checks handle it" rather than crashing.
        from datetime import datetime as _dt

        session_close = None
        if hasattr(self._broker, "get_session_close"):
            try:
                session_close = self._broker.get_session_close()
            except Exception as exc:
                logger.warning(
                    "early_close check: get_session_close failed (%s); proceeding with %s run",
                    exc,
                    session_type,
                )
                session_close = None
        if isinstance(session_close, _dt) and et_now() >= session_close:
            logger.info(
                "%s run skipped: regular session already closed today at %s ET (early-close day)",
                session_type,
                session_close.strftime("%H:%M"),
            )
            return {
                "status": "early_close",
                "positions": 0,
                "orders": [],
                "run_id": run_id,
                "session_close_et": session_close.isoformat(),
            }

        # Drain orphaned protection-restore intents from prior sessions.
        # If morning bailed on a finalize and the SELL has since become
        # terminal, recover stop coverage NOW rather than waiting for
        # next-morning's drain — codex r8 #2.
        drained = self._drain_pending_protection_restores()
        self._drain_pending_repegs()
        self._reconcile_orphan_pending_submits()  # audit F4
        # Broker-truth coverage audit (independent of the WAL).
        coverage_gaps = self._reconcile_stop_coverage()
        # Sweep retired (owner mandate 2026-09-17): release any held vehicle.
        self._release_retired_cash_park(run_id)
        # Broker-truth EXIT audit (2026-08-28 ONDS/CCJ) — midday/close run
        # every trading day, so this is the most frequent chance to catch a
        # stop that fired since the last pass and write it back before the
        # reviewer builds its "what happened today" picture.
        #
        # Item 173(2): unlike intra/evening, this site is NOT reordered to run
        # `_reconcile_fills` first. This session's only `_reconcile_fills` is
        # conditional and runs later — after `_force_delever` /
        # `_enforce_gross_ceiling` — SOLELY to flip THIS session's own
        # FORCE_DELEVER rows so the reviewer can see them; it is not the
        # unscoped stale-'submitted' sweep intra/evening run. Moving it ahead
        # of the ceiling logic would reconcile rows that don't exist yet, so
        # the reorder does not apply here.
        reco = None
        try:
            reco = self._reconcile_stop_out_fills(run_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "%s stop-out reconcile failed (non-fatal): %s",
                session_type,
                exc,
            )
        # Item 101: surface a broker-made stop-out / re-protection to owner.
        self._surface_reconcile_outcomes(reco, drained, run_id=run_id)

        # 1. Sync positions (snapshot into ctx)
        account = self._broker.get_account()
        positions = self._broker.get_positions()
        cash = account["cash"]
        total_value = account["portfolio_value"]
        last_equity = account.get("last_equity", total_value)
        # Carries the P&L block out of the paid-suspension return paths too,
        # which read the account and then reported "not available".
        self._record_account_snapshot(total_value, last_equity)
        ctx.account = account
        ctx.positions = positions
        ctx.cash = cash
        ctx.deployable_cash = self._compute_deployable_cash(cash, positions)
        ctx.total_value = total_value
        ctx.last_equity = last_equity

        # Replace the positions snapshot (drops rows for symbols no longer held).
        self._sync_positions_from_broker(positions)

        # 1a. Cash-only safety net — force-sell if the account drifted into
        # margin. Refreshes ctx fields on completion.
        forced_orders = self._force_delever(ctx)

        # 1b. Spec §11.2 — the gross-exposure ceiling and its de-levering
        # ladder. Runs on midday and close too, not just the morning: the
        # ceiling steps down on measured drawdown, and waiting for tomorrow's
        # session to act on it is the coupling the ladder exists to avoid.
        # Computed from account state alone — no agent output is an input.
        forced_orders = list(forced_orders) + self._enforce_gross_ceiling(ctx)
        if forced_orders:
            # Reconcile immediately so the FORCE_DELEVER rows flip from
            # fill_status='submitted' to 'filled' before the reviewer's
            # morning_trades query (executed_only=True) is built. Otherwise
            # the reviewer can't see the same-session forced sells in
            # system_action_lines and would reason about a shrunken book
            # without the explanation.
            self._reconcile_fills(ctx)
        positions = ctx.positions
        cash = ctx.cash
        total_value = ctx.total_value
        last_equity = ctx.last_equity
        self._sync_positions_from_broker(positions)

        # Today's P&L for the Telegram feed (item: "Session P&L" rename) —
        # same basis as `run_intra_check`/`run_evening`: the broker's own
        # last_equity (prior trading-day close), not a run-scoped figure.
        daily_pnl = (total_value - last_equity) if last_equity else 0.0
        daily_return_pct = (daily_pnl / last_equity * 100) if last_equity else 0.0
        total_pnl, total_return_pct, total_pnl_since = self._total_pnl_since_reset(total_value)

        # 1b. (DELETED 2026-09-12, owner decision.) A midday "auto take-profit"
        # used to sit here: sell 15% of any position once its unrealised
        # gain reached 30%. Both numbers were tuned off ONE trade (a GOOGL
        # trim at +27% on 2026-04-30) — hindsight-tuning on n=1 — and, more
        # fundamentally, it was a preset profit target: a fixed fraction at
        # a fixed gain decided in advance with no reference to what the
        # instrument is doing. The owner removed that class of logic when he
        # removed reward:risk as a universal gate: the reward side of a
        # trade cannot be predetermined because the holding period is
        # unknown, and profit-taking belongs to the trailing stop
        # (`src/risk/trailing.py`). The rule predated QAMC and was never
        # ratified against that doctrine. The ONLY exit rule is the
        # trailing stop; `tests/test_pipeline.py::
        # test_no_fixed_gain_automatic_profit_trim_exists` fails if a
        # fixed-gain trim is reintroduced.

        # 1c. Ex-dividend stop adjustment (both sessions — a dividend tomorrow
        # is still a dividend tomorrow no matter which session looks at it).
        exdiv_orders = self._handle_ex_dividends(positions, run_id)

        # Ex-dividend actions and all protection reconciliation above are
        # deterministic. A latched paid-analysis breaker stops only at this
        # boundary, before news/reviewer model requests.
        orders = list(forced_orders) + list(exdiv_orders)
        try:
            self._require_paid_analysis(f"{session_type}_analysis")
        except PaidAnalysisSuspended as exc:
            self._reconcile_fills()
            return self._paid_suspension_after_late_safety(
                run_id,
                session=session_type,
                error=exc,
                where=f"{session_type}-paid-preflight",
                orders=orders,
                extra={
                    "session": session_type,
                    "positions": len(positions),
                    "stop_coverage_gaps": coverage_gaps,
                    # Spec §11.2 — gross exposure and its ceiling.
                    "leverage": dict(ctx.leverage),
                },
            )

        # 2. News + Earnings update — capture developments since morning.
        try:
            # held_symbols: current book, cash-sweep vehicle excluded (see
            # _news_held_symbols), in broker snapshot order (stable within
            # this run — see _run_news_update's ordering contract). No
            # separate "candidate_symbols" concept exists at this point in
            # the midday/close path (unlike MorningResearchStage, which has
            # ctx.admitted_symbols computed before news fetches) — a
            # deliberate scope limit, not an oversight; see the PR
            # description.
            session_news, session_news_coverage = self._run_news_update(
                run_id,
                session=session_type,
                held_symbols=self._news_held_symbols(positions),
            )
        except PaidAnalysisSuspended as exc:
            self._reconcile_fills()
            return self._paid_suspension_after_late_safety(
                run_id,
                session=session_type,
                error=exc,
                where=f"{session_type}-paid-news",
                orders=orders,
                extra={
                    "session": session_type,
                    "positions": len(positions),
                    "stop_coverage_gaps": coverage_gaps,
                    # Spec §11.2 — gross exposure and its ceiling.
                    "leverage": dict(ctx.leverage),
                },
            )
        if session_news_coverage is not None and session_news_coverage.status != "ok":
            # midday/close have no data_status mechanism of their own (that
            # is a morning-only construct today — see MorningResearchStage),
            # so a degraded wire here would otherwise be silent even after
            # the 2026-08-28 coverage fix. At minimum this keeps it out of
            # the log-only failure mode the fix exists to close.
            logger.warning("%s: %s", session_type, session_news_coverage.describe())
        if session_news:
            logger.info("%s news: %s", session_type.capitalize(), session_news.pm_briefing[:200])
        try:
            _, session_earnings = self._load_earnings_analyses(
                run_id,
                session=session_type,
                ctx=ctx,
            )
        except PaidAnalysisSuspended as exc:
            self._reconcile_fills()
            return self._paid_suspension_after_late_safety(
                run_id,
                session=session_type,
                error=exc,
                where=f"{session_type}-paid-earnings",
                orders=orders,
                extra={
                    "session": session_type,
                    "positions": len(positions),
                    "stop_coverage_gaps": coverage_gaps,
                    # Spec §11.2 — gross exposure and its ceiling.
                    "leverage": dict(ctx.leverage),
                },
            )
        except Exception as e:  # noqa: BLE001 — reviewer proceeds without earnings
            logger.error("%s: earnings load failed (continuing without): %s", session_type, e)
            session_earnings = []

        circuit_state = self._cost_circuit_status()
        if circuit_state.get("suspended"):
            self._reconcile_fills()
            return self._paid_suspension_after_late_safety(
                run_id,
                session=session_type,
                orders=orders,
                where=f"{session_type}-post-news-circuit-open",
                error=PaidAnalysisSuspended(str(circuit_state.get("trigger_detail") or "cost circuit opened")),
                extra={
                    "session": session_type,
                    "positions": len(positions),
                    "stop_coverage_gaps": coverage_gaps,
                    # Spec §11.2 — gross exposure and its ceiling.
                    "leverage": dict(ctx.leverage),
                },
            )

        # 3. LLM position review — memory-heavy, 6-step CoT.
        macro_summary = self._macro.get_macro_summary()
        macro_coverage = self._macro.last_coverage
        if isinstance(macro_coverage, MacroCoverage) and macro_coverage.status != "ok":
            # Same gap noted for news coverage just above: midday/close have
            # no data_status mechanism of their own (that is a morning-only
            # construct today — see MorningResearchStage), so a degraded
            # FRED fetch here would otherwise be silent even after the
            # Phase 4.2 macro-coverage fix. At minimum this keeps it out of
            # the log-only failure mode the fix exists to close.
            logger.warning("%s: %s", session_type, macro_coverage.describe())
        review = None
        # Adjudicated take-profit revision flags, filed per symbol whichever
        # way each one goes (revision, named refusal, named data fault).
        target_revisions: list[dict] = []
        # Pre-LLM orders (take-profit + ex-div) feed into the same bucket.

        # LLM view: the cash-sweep vehicle is cash-equivalent, not a
        # position — the reviewer must never see it, hold-grade it, or sell
        # it. Raw `positions` stays in scope for the paths that need broker
        # truth (emergency liquidate below sells EVERYTHING, parked cash
        # included).
        #
        # 2026-08-19 SGOV/deployable-liquidity forensic: crediting the
        # parked vehicle's market value straight into "cash" (2026-07-16
        # audit's fix) told the reviewer money was instantly available when
        # it was not — Alpaca settlement (T+1) means a same-day SGOV
        # liquidation is not reliably spendable by the time execution
        # rechecks. `review_cash` is now `ctx.deployable_cash` (Alpaca's
        # settled non-margin buying power); `reserve_balance` carries the
        # parked value separately, informationally, so the reviewer still
        # knows the reserve exists without treating it as instant cash.
        review_positions = positions
        review_cash = ctx.deployable_cash
        reserve_balance = 0.0
        sweeper = self._sweeper()
        if sweeper is not None:
            review_positions, parked = sweeper.split_positions(positions)
            if parked is not None:
                reserve_balance = sweeper.parked_value(positions)

        if review_positions:
            # Sweep any straggler fills before building the reviewer prompt.
            # run_morning's final reconcile is run_id-scoped, so a BUY whose
            # fill landed AFTER morning's wait window stays at fill_status=
            # 'submitted' in DB even though broker shows the position. The
            # reviewer's executed_only=True query would skip it, losing
            # entry/stop/thesis context for that holding. An unscoped
            # reconcile here is cheap (1 broker call per pending row) and
            # closes that gap. Codex r11 P2.
            self._reconcile_fills()
            morning_trades = self._db.get_trades(
                limit=50,
                today_only=True,
                executed_only=True,
            )

            # Board item 89 defect 3 (and item 104's eighth trade-affecting
            # prompt defect, which is the same root cause one layer down).
            #
            # `morning_trades` is deliberately `today_only=True` — the
            # reviewer's "what already happened this session" block depends
            # on that and must keep it. But it is ALSO the only source the
            # reviewer had for a position's ENTRY THESIS, so every position
            # opened on an earlier day rendered "Entry thesis: (unavailable
            # — position opened before today)". The reason was never
            # missing: it is on the entry row in `trades`, which the lookup
            # simply never searched. The reviewer then wrote "thesis
            # unavailable" into its hold reasons, and those reasons go
            # straight into the owner's message.
            #
            # `get_symbol_last_buy` is the existing unrestricted lookup —
            # the same "most recent executed opening row for this symbol,
            # no date bound" query the evening thesis-health context and
            # the cockpit's "why do we hold this" endpoint already use. No
            # second lookup is written here, and nothing about the review
            # DECISION changes: this only stops a fact the desk already
            # holds from being reported as absent.
            entry_context: dict[str, dict] = {}
            for _p in review_positions:
                _sym = getattr(_p, "symbol", None)
                if not _sym:
                    continue
                # A short's opening row is a SHORT, not a BUY, and mixing
                # the two would hand a short a long's thesis and stop —
                # the precise confusion `get_symbol_last_buy` refuses by
                # taking the opening action explicitly.
                _action = "SHORT" if getattr(_p, "qty", 0) < 0 else "BUY"
                try:
                    _row = self._db.get_symbol_last_buy(_sym, action=_action)
                except Exception as e:  # noqa: BLE001
                    logger.warning(
                        "%s: entry-context lookup failed for %s: %s",
                        session_type,
                        _sym,
                        e,
                    )
                    continue
                if _row:
                    entry_context[_sym] = _row

            # Reuse morning's macro_analysis from macro_store so the
            # reviewer sees the same regime the PM committed to today.
            macro_analysis_dict = None
            try:
                macro_analysis_dict = self._macro_store.load_last_state()
            except Exception as e:
                logger.warning("%s: macro_store load failed: %s", session_type, e)

            # Pre-compute deterministic per-position metrics.
            #
            # Phase 3.1: this used to fetch `avg_hold_days` from the rolling
            # 45-day realized-trade calibration and hand it to the facts
            # builder as the denominator of `pace`. That is the feedback loop —
            # the system's own selling behaviour set the bar every surviving
            # position was measured against. The horizon is now pinned at entry
            # on the trade row and the calibration query is gone from this
            # path entirely, so there is nothing to accidentally reconnect.
            position_facts = self._build_position_facts(
                review_positions,
                morning_trades,
                total_value,
            )

            # Phase 3.2 / audit §1.5 — the reviewer's memory of its OWN prior
            # numbers. `_build_own_recent_decisions` below replays past ACTIONS
            # and drops HOLDs, so without this the seat rebuilds its view from
            # scratch every session and can report a position deteriorating
            # while everything it measured six hours ago improved. That is
            # exactly how EPD and MRVL were sold on intact theses.
            metric_deltas = self._build_review_metric_deltas(
                position_facts,
                run_id=run_id,
            )

            # Memory layers — share the same helpers PM uses.
            weekly_narrative = self._build_weekly_narrative()
            macro_trajectory = self._build_macro_trajectory()
            active_state_changes = self._build_active_state_changes()
            calibration_note = self._build_calibration_note()
            own_recent_decisions = self._build_own_recent_decisions()
            # v2: evening's per-trade grades feed back into position_reviewer.
            # 14-day rolling counts of correct/premature/wrong SELLs (and BUYs)
            # let the reviewer lean patient when past SELLs trended premature.
            trade_grade_summary = self._build_trade_grade_summary(lookback_days=14)
            # Same-day trim discipline — feeds the prompt + the executor.
            # See _symbols_already_trimmed_today for the AMZN-2026-05-04 origin.
            #
            # 2026-09-17 XOM incident: _symbols_already_trimmed_today reads
            # today's trade rows with no notion of what is still held — a
            # symbol that was fully SOLD (not merely trimmed) this morning
            # comes back exactly like one that still has shares open. The
            # prompt section this feeds tells the LLM to render a HOLD
            # decision for every name in the set ("HOLD them at this
            # session unless..."), so a fully-closed name that never
            # appears in `review_positions` (broker truth, fetched above)
            # still got a fabricated action out of the model — 7 actions
            # returned against 6 real broker positions. Intersect with the
            # symbols actually being reviewed right here, at the one place
            # both sets are in scope, so a sold-out name can never reach
            # the reviewer's prompt or its action list again.
            already_trimmed_today = self._symbols_already_trimmed_today() & {p.symbol for p in review_positions}
            # Board item 74 — the seat must SEE which triggers it has already
            # spent today, or the executor's refusal is an invisible filter.
            # Same text the enforcement reads, so prompt and gate cannot rot
            # apart. A failed read renders nothing rather than claiming
            # nothing is spent.
            try:
                from src.risk.spent_trigger import (
                    format_spent_triggers_block,
                    keep_executed_acted_triggers,
                    parse_acted_triggers,
                )

                _acted_rows = self._db.get_acted_exit_triggers_today()
                # Same fill verification the executor applies, so the seat is
                # never told a trigger is spent by a cut that sold nothing.
                _executed_ids = {
                    str(r.get("broker_order_id"))
                    for r in (self._db.get_trades(today_only=True, limit=200) or [])
                    if r.get("broker_order_id") and self._trade_executed_or_pending(r)
                }
                _acted = keep_executed_acted_triggers(
                    None if _acted_rows is None else parse_acted_triggers(_acted_rows),
                    executed_order_ids=_executed_ids,
                )
                spent_triggers_block = (
                    ""
                    if _acted is None
                    else format_spent_triggers_block(
                        _acted,
                        {p.symbol for p in review_positions},
                    )
                )
            except Exception as _e:  # noqa: BLE001
                logger.warning(
                    "spent trigger: prompt block unavailable (%s) — the executor still enforces it",
                    _e,
                )
                spent_triggers_block = ""

            yesterday_insights = self._db.get_latest_insights(before_date=session_date_key())
            recent_performance = self._compute_recent_performance(last_equity)

            # Margin capacity for the reviewer prompt — WORDING ONLY, mirrors
            # the same fix threaded into the PM prompt (`DecisionStage.run`
            # in `src/pipeline_stages.py`). Reuses the EXACT §11.2
            # computation execution's submit loop sizes entries against
            # (`_entry_deployment_budget`, which itself resolves the ladder
            # via `_session_gross_ceiling`) — never a second formula. Book
            # state here (positions/equity/held-gross) has not changed since
            # ctx was built above, so this is the same headroom execution
            # will see for this session's entries.
            from src.pipeline_stages import (
                _entry_deployment_budget,
                _session_gross_ceiling,
            )

            margin_headroom_usd, margin_ladder_backed, _margin_headroom_note = _entry_deployment_budget(
                self._pipeline,
                ctx,
                review_positions,
                total_value,
                review_cash,
            )
            _margin_ceiling = _session_gross_ceiling(self._pipeline, ctx)
            margin_ladder_multiple = _margin_ceiling.ceiling_x if _margin_ceiling is not None else None
            margin_ladder_rung = _margin_ceiling.rung if _margin_ceiling is not None else None

            review_kwargs = dict(
                positions=review_positions,
                macro_summary=macro_summary,
                cash_balance=review_cash,
                reserve_balance=reserve_balance,
                total_value=total_value,
                session_type=session_type,
                position_facts=position_facts,
                metric_deltas=metric_deltas,
                morning_trades=morning_trades,
                # Board item 89 defect 3 — see the build above.
                entry_context=entry_context,
                news_intel=session_news,
                earnings_analyses=session_earnings,
                macro_analysis=macro_analysis_dict,
                weekly_narrative=weekly_narrative,
                macro_trajectory=macro_trajectory,
                active_state_changes=active_state_changes,
                calibration_note=calibration_note,
                own_recent_decisions=own_recent_decisions,
                trade_grade_summary=trade_grade_summary,
                yesterday_insights=yesterday_insights,
                recent_performance=recent_performance,
                already_trimmed_today=already_trimmed_today,
                spent_triggers_block=spent_triggers_block,
                allow_margin=bool(getattr(self._config.risk, "allow_margin", False)),
                margin_headroom_usd=margin_headroom_usd,
                margin_ladder_backed=margin_ladder_backed,
                margin_ladder_multiple=margin_ladder_multiple,
                margin_ladder_rung=margin_ladder_rung,
            )
            try:
                review, md_result = self._position_reviewer.review(**review_kwargs)
            except PaidAnalysisSuspended as exc:
                self._reconcile_fills()
                return self._paid_suspension_after_late_safety(
                    run_id,
                    session=session_type,
                    error=exc,
                    where=f"{session_type}-paid-reviewer",
                    orders=orders,
                    extra={
                        "session": session_type,
                        "positions": len(positions),
                        "stop_coverage_gaps": coverage_gaps,
                        # Spec §11.2 — gross exposure and its ceiling.
                        "leverage": dict(ctx.leverage),
                    },
                )
            review_log_kwargs = agent_log_kwargs(md_result)
            if review is None:
                review_log_kwargs["status"] = "position_review_parse_error"
            self._db.insert_agent_log(
                **seat_acceptance_kwargs(
                    "position_review_parse_error" if review is None else None,
                    result=md_result,
                ),
                agent_name="position_reviewer",
                run_id=run_id,
                input_summary=(f"{session_type} | {len(review_positions)} positions, ${total_value:.0f} total"),
                input_message=md_result.user_message,
                output_summary=review.overall_assessment if review else "parse_error",
                full_response=md_result.raw_text,
                model=md_result.model,
                tokens_used=md_result.tokens_used,
                input_tokens=md_result.input_tokens,
                output_tokens=md_result.output_tokens,
                cost_usd=md_result.cost_usd,
                **review_log_kwargs,
            )

            # Substantiation pass on the exit side (2026-09-18). Heals the
            # structured trigger from the prose, RE-ASKS once for anything
            # still unsubstantiated, and records a durable reason for what
            # survives both. See `_substantiate_exit_triggers`.
            review = self._substantiate_exit_triggers(
                review,
                ctx=ctx,
                run_id=run_id,
                review_kwargs=review_kwargs,
            )

            # Refresh the broker book before dispatching the LLM's
            # per-position action list: the locals here date from BEFORE the
            # review (minutes of tape ago). Falls back to the pre-review
            # snapshot if the refresh fails.
            try:
                fresh_positions = self._broker.get_positions()
                if fresh_positions:
                    positions = fresh_positions
            except Exception as e:  # noqa: BLE001
                logger.warning("post-review position refresh failed (using pre-review snapshot): %s", e)
            # Phase 3.7 — deterministic trailing FIRST, before the LLM's
            # discretionary TRAIL_STOP is considered. Arithmetic does not
            # need a language model's permission, and a winner's stop
            # should not depend on one remembering to propose a move.
            orders.extend(self._apply_deterministic_trails(review_positions, run_id=run_id))

            # Phase 3.4 — AGENTS.md puts AI Risk in the chain for exits
            # as well as entries. Until this landed the entire sell side
            # skipped the veto layer the buy side has always had.
            risk_vetoed, _exit_verdict = self._risk_review_exits(
                review,
                review_positions,
                run_id=run_id,
                total_value=total_value,
                macro_summary=macro_summary,
                position_facts=position_facts,
                # This loop fetched all of these before the position
                # reviewer ran; until 2026-09-13 none of them reached the
                # AI Risk seat, which was then asked to audit the exits
                # against news and drawdown state it had never been shown.
                news_intel=session_news,
                earnings_analyses=session_earnings,
                cash=review_cash,
                reserve_balance=reserve_balance,
                recent_performance=recent_performance,
            )
            orders.extend(
                self._midday_execute_llm_actions(
                    review_positions,
                    review,
                    run_id,
                    already_trimmed_today=already_trimmed_today,
                    metric_deltas=metric_deltas,
                    risk_vetoed_symbols=risk_vetoed,
                    position_facts=position_facts,
                )
            )

            # Take-profit revision flags, adjudicated LAST — after every
            # exit decision this session makes. A re-derived target
            # therefore cannot reach this session's exits even in
            # principle; and because progress/pace are measured against
            # the entry target, it cannot reach a later session's
            # exit-guard veto either. Places no orders: nothing here
            # exits anything, and the trailing stop remains the only
            # automatic exit (PR #321).
            #
            # NOT the same as "it can never contribute to a sale" (item
            # 194, corrected 2026-10-01). The LIVE target still feeds
            # `distance_to_target_pct` in the position facts the reviewing
            # model reads, so a revised target can still influence a sale
            # through that model's prose on a LATER session. What is true
            # is narrower and worth stating precisely: no DETERMINISTIC
            # gate reads it — not the exit guard, not progress or pace,
            # and since item 194 not the trailing-stop regime either.
            try:
                # Runs over the WHOLE open book, not only the symbols a
                # seat flagged (item 194); `seat` here is only the label
                # worn by the outcomes that a seat did raise.
                target_revisions = self._adjudicate_target_revision_flags(
                    review,
                    review_positions,
                    run_id=run_id,
                    seat="position_reviewer",
                )
            except Exception as exc:  # noqa: BLE001
                logger.error(
                    "target revision sweep failed (non-fatal, no target was changed): %s",
                    exc,
                )
                target_revisions = []

            # Snapshot AFTER the review so the next session compares against
            # what this one actually saw. Written even when the review failed:
            # the metrics are deterministic and their continuity is the point.
            self._persist_review_metrics(position_facts, run_id=run_id)

        logger.info(
            "%s: %d positions, risk=%s, %d orders",
            session_type.capitalize(),
            len(positions),
            review.risk_level if review else "no_positions",
            len(orders),
        )
        # Reconcile everything still marked submitted (today's new orders +
        # any lingering from morning that didn't reach terminal in time).
        self._reconcile_fills()

        # Session execution (reviewer exits, sweep) may have changed the
        # book since the start-of-session snapshot.
        self._sync_positions_from_broker()

        return {
            "status": ("reviewed" if not review_positions or review is not None else "position_review_parse_error"),
            "session": session_type,
            "positions": len(positions),
            "review": review.model_dump() if review else None,
            "orders": orders,
            "run_id": run_id,
            "stop_coverage_gaps": coverage_gaps,
            # Every take-profit revision flag this session adjudicated, with
            # its basis code — read by the cockpit, which draws the target.
            "target_revisions": target_revisions,
            # Spec §11.2 — gross exposure, its ladder-resolved ceiling and the
            # distance to forced liquidation, for the operator alert.
            "leverage": dict(ctx.leverage),
            # Telegram P&L line ("Session P&L" rename): today's account
            # change (broker last_equity basis, same as run_intra_check/
            # run_evening) plus total since the last recorded baseline.
            "daily_pnl": daily_pnl,
            "daily_return_pct": daily_return_pct,
            "total_pnl": total_pnl,
            "total_return_pct": total_return_pct,
            "total_pnl_since": total_pnl_since,
        }
