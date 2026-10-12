"""Decision stage (split out of src/pipeline_stages.py, step 10).

Moved verbatim in the staged split (board item 210, step 10). No behaviour change:
the class body below is byte-for-byte the text that used to live in
``src/pipeline_stages.py``, and ``src.pipeline_stages`` re-exports it so every
existing import path and every ``src.pipeline_stages.DecisionStage`` patch target
still resolves to this same object.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from src.pipeline_risk_budget_recording import _record_realised_concentration
from src.rotation_dispositions import apply_rotation_recording_dispositions
from src.soft_exit_never_blank import add_constructor_dropped
from src.risk.min_risk import MinRiskFloor
from src.pipeline_stages import (  # noqa: F401  shared helpers and module-level names
    FAULT_NO_PRICE,
    FAULT_STALE_PRICE,
    ONLY_STALE,
    PortfolioManagerAgent,
    REWARD_RISK_FLOOR,
    RunContext,
    SOFT_EXIT_MISSING_AFTER_RETRY,
    STARTER_POSITION_RISK_PCT,
    _account_for_pm_candidates,
    _alert_unmeasurable_symbols,
    _book_risk_inputs,
    _dropped_since_proposal,
    _entry_deployment_budget,
    _link_nominations_to_decision,
    _live_stops_from_heat,
    _macro_analysis_as_dict,
    _macro_regime,
    _min_position_risk_pct,
    _persist_evidence,
    _record_constructor_drops,
    _record_constructor_side_flips,
    _record_pipeline_event,
    _record_rotation_precheck,
    _record_seat_stances,
    _record_soft_exit_heals,
    _record_soft_exit_refusal_count,
    _record_soft_exit_missing_after_retry,
    _rotation_execution_enabled,
    _rotation_ranked_margin_enabled,
    _session_candidate_ranking,
    _session_gross_ceiling,
    _targets_admitted_to_book,
    agent_log_kwargs,
    logger,
    resolve_live_price,
    seat_acceptance_kwargs,
    uuid,
)
from src.rotation_margins import record_rotation_margins

if TYPE_CHECKING:
    from src.agents.earnings_analyst import EarningsAnalystAgent
    from src.agents.macro_analyst import MacroAnalystAgent
    from src.agents.news_analyst import NewsAnalystAgent
    from src.agents.tech_analyst import TechAnalystAgent
    from src.agents.smart_money_analyst import SmartMoneyAnalystAgent
    from src.data.smart_money import SmartMoneySource
    from src.config import AppConfig
    from src.data.earnings import EarningsDataProvider
    from src.data.event_calendar import (
        FOMCCalendarProvider,
        MacroEventCalendarProvider,
    )
    from src.data.macro import MacroDataProvider
    from src.data.macro_store import MacroStore
    from src.data.market import MarketDataProvider
    from src.data.news import NewsCoverage, NewsDataProvider
    from src.data.news_store import NewsStore
    from src.data.tech_store import TechStore
    from src.models import TradeDecision
    from src.pipeline import TradingPipeline


class DecisionStage:
    """Build PM memory layers → call PM → run Constructor.

    Reads:  ctx.positions, ctx.analyses, ctx.news_intel, ctx.earnings_results,
            ctx.macro_analysis, ctx.total_value, ctx.deployable_cash,
            ctx.last_equity

    `ctx.deployable_cash`, NOT `ctx.cash` — this stage sizes a plan against
    everything the account owns, parked sweep value included. Raw broker cash
    here would hide the parked book from PM and RM and cap the desk at its
    reserve. Since item 190 nothing converts the vehicle automatically before
    the BUY phase. The docstring said `ctx.cash`;
    the code has read `deployable_cash` since the 2026-08-19 tranche.
    Writes: ctx.portfolio_decision (with .targets AND .decisions populated),
            ctx.facts
    """

    def __init__(self, *, pipeline: "TradingPipeline"):
        self._pipeline = pipeline

    def run(self, ctx: RunContext) -> RunContext:
        from src.trading_calendar import session_date_key

        pipeline = self._pipeline
        run_id = ctx.run_id
        positions = ctx.positions
        analyses = ctx.analyses
        news_intel = ctx.news_intel
        earnings_results = ctx.earnings_results
        macro_analysis = ctx.macro_analysis
        total_value = ctx.total_value
        # PM sizes against `ctx.deployable_cash` = raw cash + convertible
        # sweep value (see `_compute_deployable_cash` for the verified
        # Alpaca field semantics: a filled SGOV sale credits `cash`
        # immediately — T+1 gates only withdrawal/transfer). The sweep
        # detail is rendered informationally via `reserve_balance`;
        # execution's raw-cash recheck after the funding sale remains the
        # final authority on what a BUY can actually spend.
        cash = ctx.deployable_cash
        last_equity = ctx.last_equity

        # isinstance guard: stage tests stub `pipeline` with MagicMock, whose
        # auto-attrs would otherwise duck-type as an enabled sweeper.
        from src.execution.cash_sweep import CashSweeper

        sweeper = getattr(pipeline, "_sweeper", None)
        sweeper = sweeper() if callable(sweeper) else None
        reserve_balance = 0.0
        if isinstance(sweeper, CashSweeper):
            positions, parked = sweeper.split_positions(positions)
            if parked is not None:
                reserve_balance = sweeper.parked_value(ctx.positions)

        yesterday_insights = pipeline.db.get_latest_insights(before_date=session_date_key())
        recent_performance = pipeline._compute_recent_performance(last_equity)
        if yesterday_insights:
            logger.info(
                "Loaded yesterday's insights (risk=%s): %s",
                yesterday_insights.get("risk_rating", "?"),
                yesterday_insights.get("tomorrow_outlook", "")[:100],
            )

        position_history = pipeline._build_position_history(positions)
        # Publish both to ctx so RiskStage audits PM against the SAME holding
        # ages and drawdown state PM sized from, instead of a second snapshot
        # taken minutes later (2026-08-13 agent audit).
        ctx.position_history = position_history
        ctx.recent_performance = recent_performance
        weekly_narrative = pipeline._build_weekly_narrative()
        macro_trajectory = pipeline._build_macro_trajectory()
        active_state_changes = pipeline._build_active_state_changes()
        rm_recent_verdicts = pipeline._build_rm_recent_verdicts()
        pm_recent_decisions = pipeline._build_pm_recent_decisions()
        projected_portfolio = pipeline._build_projected_portfolio(
            positions,
            analyses,
            total_value,
            run=ctx,
        )
        calibration_note = pipeline._build_calibration_note()
        macro_tech_alignment = pipeline._build_macro_tech_alignment(macro_analysis, analyses)
        # Phase-1 evening-upgrade feedback: surface recurring missed themes
        # (L3d) and repeat loss patterns (L3f) that evening classified over
        # the last 14 days. Empty strings when no recurring pattern found.
        recent_missed_lessons = pipeline._build_recent_missed_lessons()
        recent_loss_pits = pipeline._build_recent_loss_pits()
        # Names PM keeps proposing and never gets. Every other per-symbol
        # memory above is keyed on a position, so none of them can see a
        # symbol that never became one.
        blocked_proposals = pipeline._build_blocked_proposals(run_id=run_id)
        # Audit §1.2 — build the correlation matrix HERE, before PM decides,
        # rather than in RiskStage after it already has. RiskStage reuses the
        # memoized matrix, so the deterministic cluster check still judges PM
        # against exactly the numbers PM was shown.
        correlation_matrix = pipeline._ensure_correlation_matrix(ctx, positions)
        pm_facts = pipeline._build_pm_facts(
            positions=positions,
            analyses=analyses,
            total_value=total_value,
            cash=cash,
            recent_performance=recent_performance,
            macro_analysis=macro_analysis,
            correlation_matrix=correlation_matrix,
        )
        ctx.facts = pm_facts
        trading_config = getattr(pipeline.config, "trading", None)
        configured_universe = getattr(trading_config, "universe", []) or []

        # Spec §2.2 — the book's EXISTING risk, before anything this session
        # proposes. Computed here (moved up from just before the constructor
        # call below, which still reuses this same pair) so the Phase 14
        # opportunity-rotation pre-check can see the same numbers the
        # constructor will later ration against, rather than a fabricated
        # "book is empty" view. Pure function of `ctx.facts` — safe to
        # compute this early since nothing between here and the constructor
        # call mutates it.
        existing_risk_pct, risk_clusters = _book_risk_inputs(ctx, total_value)

        # 2026-09-04 fix (audit finding): the PM's own eligibility gate
        # (`candidate_eligibility` / `_apply_subfloor_catalyst_rule`) used
        # to read `TechAnalysisResult.risk_reward` — real arithmetic, but
        # over the analyst's own GUESSED target, never checked against
        # structure. `construct_orders` below has computed the REAL
        # derived-target, noise-floor-widened reward:risk since 2026-09-01
        # (§12.1); this gate never got it. Measured on a real day, the two
        # gates passed DISJOINT eligible sets. Computed here, before the PM
        # decides, using the same `PortfolioConstructor` instance
        # `construct_orders` uses below — same config, same derivation, no
        # second copy of the logic. Necessarily a PREVIEW, not the final
        # number: entry is the analyst's snapshot, not the live price, and
        # the stop is the analyst's own, not yet a PM-suggested one — both
        # are only known at construction time. See
        # `PortfolioConstructor.real_reward_risk_preview`.
        _regime_for_preview = _macro_regime(macro_analysis)
        real_reward_risk_by_symbol: dict[str, float | None] = {}
        # Each candidate's entry and the stop the constructor would ship,
        # for the rotation pre-check's minimum-risk funding test.
        candidate_entry_stop: dict[str, tuple[float, float]] = {}
        for _a in analyses:
            _direction = "short" if _a.rating in ("sell", "strong_sell") else "long"
            try:
                _preview = pipeline.portfolio_constructor.entry_stop_preview(
                    _a,
                    _direction,
                    regime=_regime_for_preview,
                )
            except Exception as exc:  # noqa: BLE001 — unreadable stop = funding not measured
                logger.warning("entry/stop preview failed for %s: %s", _a.symbol, exc)
                _preview = None
            if isinstance(_preview, tuple) and len(_preview) == 3:
                candidate_entry_stop[_a.symbol.upper()] = (_preview[0], _preview[1])
            real_reward_risk_by_symbol[_a.symbol.upper()] = pipeline.portfolio_constructor.real_reward_risk_preview(
                _a,
                _direction,
                regime=_regime_for_preview,
            )
        # Item 54 (2026-09-12): the preview above also RECORDS, by code, the
        # names the one shared funnel refused (`last_refusals` — stop wider
        # than the instrument's reach, or too young to measure). A
        # snapshot, not a drain: DecisionStage drains once per session
        # after construction, so the same refusal is filed exactly once.
        constructor_refusals_by_symbol = {
            str(sym).upper(): dict(refusal)
            for sym, refusal in dict(
                getattr(
                    getattr(pipeline, "portfolio_constructor", None),
                    "last_refusals",
                    {},
                )
                or {}
            ).items()
        }

        # Margin capacity for the PM prompt — WORDING ONLY. Reuses the
        # EXACT §11.2 computation the execution submit loop uses to size
        # entries (`_entry_deployment_budget`, which itself resolves the
        # ladder via `_session_gross_ceiling`), so the prompt cannot state a
        # different number than execution sizes against. Book state here
        # (positions/equity/held-gross) has not changed since ctx was built
        # above, so this is the same headroom execution will see for this
        # session's opening entries — never a new formula.
        margin_headroom_usd, margin_ladder_backed, _margin_headroom_note = _entry_deployment_budget(
            pipeline, ctx, positions, total_value, cash
        )
        _margin_ceiling = _session_gross_ceiling(pipeline, ctx)
        margin_ladder_multiple = _margin_ceiling.ceiling_x if _margin_ceiling is not None else None
        margin_ladder_rung = _margin_ceiling.rung if _margin_ceiling is not None else None

        # Kept as a dict so the ONE accounting re-ask below (board item
        # 110) can re-ask the identical question — same inputs, same
        # prompt — with only the bookkeeping challenge added. A re-ask
        # built from different inputs would be a second decision, not a
        # re-ask.
        pm_decide_kwargs = dict(
            analyses=analyses,
            positions=positions,
            macro_analysis=_macro_analysis_as_dict(macro_analysis),
            cash_balance=cash,
            reserve_balance=reserve_balance,
            total_value=total_value,
            news_intel=news_intel,
            earnings_analyses=earnings_results,
            smart_money_findings=ctx.smart_money_findings,
            yesterday_insights=yesterday_insights,
            recent_performance=recent_performance,
            position_history=position_history,
            weekly_narrative=weekly_narrative,
            macro_trajectory=macro_trajectory,
            active_state_changes=active_state_changes,
            rm_recent_verdicts=rm_recent_verdicts,
            pm_recent_decisions=pm_recent_decisions,
            projected_portfolio=projected_portfolio,
            calibration_note=calibration_note,
            macro_tech_alignment=macro_tech_alignment,
            recent_missed_lessons=recent_missed_lessons,
            recent_loss_pits=recent_loss_pits,
            blocked_proposals=blocked_proposals,
            facts=pm_facts,
            allow_margin=bool(getattr(pipeline.config.risk, "allow_margin", False)),
            margin_headroom_usd=margin_headroom_usd,
            margin_ladder_backed=margin_ladder_backed,
            # Board item 95: the PM is shown what the capacity above COSTS.
            # Read off the same loaded config the rest of this call uses, so
            # the prompt renderer never re-loads `AppConfig` (which validates
            # API keys and would fail silently, dropping the price).
            margin_interest_rate_pct=getattr(
                pipeline.config.risk,
                "margin_interest_rate_pct",
                None,
            ),
            # The owner's minimum risk per position (owner rule 2026-08-27),
            # read from the setting, with each candidate's own previewed
            # entry and stop, so the rotation pre-check judges "short of
            # cash" at the candidate's real stop through the one shared
            # `src.risk.min_risk.min_risk_shortfall`. Replaced the deleted
            # flat $500 `cash_sweep.min_order_usd` floor.
            min_risk_floor=MinRiskFloor(
                floor_pct=_min_position_risk_pct(pipeline),
                equity_usd=(
                    float(ctx.total_value)
                    if isinstance(ctx.total_value, (int, float)) and not isinstance(ctx.total_value, bool)
                    else None
                ),
                entry_stop=candidate_entry_stop,
            ),
            margin_ladder_multiple=margin_ladder_multiple,
            margin_ladder_rung=margin_ladder_rung,
            symbol_sectors=dict(ctx.symbol_sectors or {}),
            session_type=ctx.session,
            allowed_buy_symbols={str(symbol).strip().upper() for symbol in configured_universe if str(symbol).strip()}
            | set(ctx.admitted_symbols),
            transient_admitted_symbols=set(ctx.admitted_symbols),
            # The unmeasurable-payoff gate reads the SAME starter size the
            # risk budget will actually grant. `rr_floor` is retired as a
            # size/refuse threshold (owner 2026-09-17) and is still threaded
            # so existing callers/tests do not silently re-default a number
            # that must not decide size. No settings key backs it any more
            # (board item 81) — it is always the historical constant.
            rr_floor=float(REWARD_RISK_FLOOR),
            starter_risk_pct=float(
                getattr(
                    pipeline.config.risk,
                    "min_position_risk_pct",
                    STARTER_POSITION_RISK_PCT,
                )
            ),
            # Phase 14 (opportunity-cost rotation) — same book-risk snapshot
            # and ceiling the constructor rations against below, computed
            # once above so both stages judge the identical numbers.
            existing_risk_pct=existing_risk_pct,
            max_portfolio_risk_pct=float(
                getattr(
                    pipeline.config.risk,
                    "max_portfolio_risk_pct",
                    25.0,
                )
            ),
            # Phase 14b: wording only — see `_apply_rotation_execution`.
            rotation_execute_enabled=_rotation_execution_enabled(pipeline),
            # Board item 39: the ranked-margin tier is executable behind its
            # own second switch, and the PM's prompt has to say so or the
            # model sizes its plan as though no room is being freed.
            rotation_ranked_margin_enabled=_rotation_ranked_margin_enabled(
                pipeline,
            ),
            real_reward_risk_by_symbol=real_reward_risk_by_symbol,
            constructor_refusals_by_symbol=constructor_refusals_by_symbol,
        )
        portfolio_decision, pm_result = pipeline.portfolio_manager.decide(
            **pm_decide_kwargs,
        )
        # Board item 164: read NOW — the candidate-accounting re-ask below
        # calls decide() again, which resets this list.
        _pm_dropped = getattr(pipeline.portfolio_manager, "last_dropped_targets", None)
        pm_dropped_targets = list(_pm_dropped) if isinstance(_pm_dropped, list) else []
        _pm_blocked = getattr(pipeline.portfolio_manager, "last_blocked", None)
        pm_blocked = dict(_pm_blocked) if isinstance(_pm_blocked, dict) else {}
        _pm_ranked = getattr(pipeline.portfolio_manager, "last_candidate_ranking", None)
        pm_ranked = list(_pm_ranked) if isinstance(_pm_ranked, list) else []
        # Board item 78: same "read NOW" reason — the accounting re-ask
        # resets the heal record too. One durable row per name, whether the
        # heal worked or not.
        _record_soft_exit_heals(pipeline, ctx)
        from src.agents.portfolio_manager import PortfolioManagerAgent

        macro_failures = list(getattr(PortfolioManagerAgent, "_macro_parse_failures", None) or [])
        instance_failures = getattr(
            pipeline.portfolio_manager,
            "_macro_parse_failures",
            None,
        )
        if instance_failures and instance_failures is not macro_failures:
            for reason in list(instance_failures):
                if reason not in macro_failures:
                    macro_failures.append(reason)
        for reason in macro_failures:
            _record_pipeline_event(
                pipeline,
                ctx,
                None,
                "macro_parse",
                "failed",
                reason=reason,
            )
        PortfolioManagerAgent._macro_parse_failures = []
        try:
            pipeline.portfolio_manager._macro_parse_failures = []
        except Exception:
            pass

        if portfolio_decision and portfolio_decision.reasoning_chain:
            rc = portfolio_decision.reasoning_chain
            # All TEN fields. This line logged seven, and the ones it
            # omitted were exactly the ones the schema lets default to "" —
            # so the operator-facing log could not distinguish "PM
            # red-teamed its book" from "PM skipped the step" (2026-08-13
            # agent audit). `macro_audit` is the third such field
            # (2026-09-14, item 18e) and is here for the same reason: a
            # field that validates when empty is invisible in a log that
            # does not print it.
            logger.info(
                "PM Reasoning Chain:\n  Macro: %s\n  News: %s\n  Earnings: %s\n  "
                "Conflicts: %s\n  Sizing: %s\n  Balance: %s\n  Cash: %s\n  "
                "Continuity: %s\n  Pre-mortem: %s\n  Macro audit: %s",
                rc.macro_filter[:120],
                rc.news_check[:120],
                rc.earnings_check[:120],
                rc.signal_conflicts[:120],
                rc.sizing_logic[:120],
                rc.portfolio_balance[:120],
                rc.cash_target[:120],
                rc.continuity_check[:120] or "[MISSING]",
                rc.premortem_check[:120] or "[MISSING]",
                rc.macro_audit[:120] or "[MISSING]",
            )

        # Stage 1 (QAMC correlation plumbing): one id per PM call, generated
        # independently of run_id (not reused verbatim) so it stays correct
        # even if a future change ever calls decide() more than once per
        # run. Threaded to the risk_manager agent_logs row (RiskStage) and
        # every trades row this run's decisions produce (ExecutionStage).
        decision_id = f"{run_id}-dec-{uuid.uuid4().hex[:6]}"
        ctx.decision_id = decision_id
        # Conviction ledger (spec §7.2): pm_result.model is the ACTUAL model
        # that answered (see RunContext.decision_model docstring), threaded
        # to ExecutionStage regardless of whether this call ultimately
        # produced a valid decision — a failed/unparseable PM call still
        # carries no trades, so an unused decision_model is harmless.
        ctx.decision_model = pm_result.model
        # Conviction ledger (spec §9.5): the nomination rows this run wrote
        # during MorningResearchStage carry decision_id NULL because the id
        # did not exist yet. Join them now — before the PM-failure early
        # return below, so a run whose PM produced nothing still shows which
        # seats had asked for what. Bookkeeping only; never raises.
        _link_nominations_to_decision(pipeline, ctx)
        # Board item 164: a target the seat proposed and code then removed
        # (malformed, or an unadjudicated seat conflict) is a decision the
        # desk made about that symbol; it is persisted per symbol with the
        # gate and the reason, not only logged. Written whether or not the
        # PM call as a whole produced a usable decision.
        for dropped in pm_dropped_targets:
            _details = {k: v for k, v in dropped.items() if k not in ("symbol", "reason")}
            _record_pipeline_event(
                pipeline,
                ctx,
                dropped.get("symbol"),
                "portfolio_manager",
                "target_dropped",
                dropped.get("reason", ""),
                **_details,
            )

        _journal_refused_candidates(pipeline, ctx, pm_blocked)
        _journal_ranked_candidates(pipeline, ctx, pm_ranked)

        pm_log_kwargs = agent_log_kwargs(pm_result)
        if portfolio_decision is None:
            ctx.analysis_failure_status = pm_result.semantic_status or "pm_agent_failure"
            ctx.analysis_failure_error = pm_result.semantic_error or "no valid PM decision"
        pipeline.db.insert_agent_log(
            **seat_acceptance_kwargs(
                "no_valid_grounded_decision" if not portfolio_decision else None,
                result=pm_result,
            ),
            agent_name="portfolio_manager",
            run_id=run_id,
            input_summary=f"{len(analyses)} analyses, ${total_value:.0f} total",
            input_message=pm_result.user_message,
            output_summary=(
                portfolio_decision.portfolio_view
                if portfolio_decision
                else f"{ctx.analysis_failure_status}: {ctx.analysis_failure_error}"
            ),
            full_response=pm_result.raw_text,
            model=pm_result.model,
            tokens_used=pm_result.tokens_used,
            input_tokens=pm_result.input_tokens,
            output_tokens=pm_result.output_tokens,
            cost_usd=pm_result.cost_usd,
            decision_id=decision_id,
            **pm_log_kwargs,
        )

        if not portfolio_decision:
            _record_pipeline_event(
                pipeline,
                ctx,
                None,
                "portfolio_manager",
                "failed",
                "no_valid_grounded_decision",
            )
            _persist_evidence(
                pipeline.db,
                run_id=run_id,
                agent_name="portfolio_manager",
                kind="agent_failure",
                scope="run",
                decision_id=decision_id,
                evidence_json=('{"failure":"no_valid_grounded_decision","stage":"portfolio_manager","decision":null}'),
            )
            ctx.portfolio_decision = None
            return ctx

        import json as _json

        _persist_evidence(
            pipeline.db,
            run_id=run_id,
            agent_name="portfolio_manager",
            kind="reasoning",
            scope="run",
            decision_id=decision_id,
            evidence_json=_json.dumps(
                {
                    "portfolio_view": portfolio_decision.portfolio_view,
                    "reasoning_chain": portfolio_decision.reasoning_chain.model_dump(),
                }
            ),
        )
        for target in portfolio_decision.targets:
            _persist_evidence(
                pipeline.db,
                run_id=run_id,
                agent_name="portfolio_manager",
                kind="target",
                scope="symbol",
                symbol=target.symbol,
                decision_id=decision_id,
                evidence_json=target.model_dump_json(),
            )
        # Board item 163: cross-check the PM's whole-book sizing NARRATIVE
        # (`reasoning_chain.sizing_logic`) against each symbol's own emitted
        # `risk_allocation_pct`. Detection only -- it records the mismatch to
        # the evidence stream and NEVER changes a target, size, price or exit;
        # `risk_allocation_pct` stays authoritative. Wrapped so a bookkeeping
        # check can never take a live PM session down, matching the posture of
        # `_account_for_pm_candidates` below. The audit helper records one
        # `checked` row per session as well as any mismatch, so an empty
        # stream distinguishes agreement from a path that never ran.
        try:
            from src.sizing_narrative_audit import audit_sizing_narrative

            audit_sizing_narrative(pipeline, ctx, portfolio_decision)
        except Exception:
            logger.exception("sizing_narrative_check audit failed")
        _account_for_pm_candidates(
            pipeline,
            ctx,
            run_id=run_id,
            analyses=analyses,
            positions=positions,
            decision=portfolio_decision,
            pm_decide_kwargs=pm_decide_kwargs,
        )

        price_map = {p.symbol: p.current_price for p in positions}
        # A new name (one not already held) needs a live price to SIZE its
        # BUY: the constructor and ExecutionStage both do
        # `qty = total_value * alloc/100 / price`, so the price is the
        # divisor of the dollar allocation and a wrong price mis-sizes the
        # position PROPORTIONALLY. The bare broker call used here previously
        # (`get_latest_price`) returns whatever `get_latest_price_stamped`
        # finds FIRST — a real trade print if there is one, but otherwise a
        # QUOTE MIDPOINT or a prior-session last trade, unlabelled — so a
        # stale or mid price silently set the share count (docs/WORK.md item
        # 120).
        #
        # Route each new name through the desk's one freshness resolver
        # instead. `resolve_live_price` turns a `get_intraday_snapshots`
        # payload into a today price that is a real print OR today's forming
        # session bar — never a quote mid (the module has no branch that
        # reads a quote) — subject to the date-equality + 09:30-open
        # freshness rule, or an explicit refusal. A fresh price sizes the
        # buy; a name with NO usable today price this session is REFUSED as
        # unmeasurable (`unpriceable_new_syms` below, routed into the
        # constructor's existing data-fault / unmeasurable drop path) rather
        # than sized on a bad price. No fallback price is invented.
        #
        # IEX FREE-FEED CAVEAT: the price comes from the single free feed the
        # account defaults to (`get_intraday_snapshots` pins no `feed`), so
        # for a very thin name a fresh today price can simply be absent. That
        # name is then correctly refused — the safe, intended outcome, not a
        # regression.
        new_syms = list(
            dict.fromkeys(
                t.symbol.strip().upper()
                for t in portfolio_decision.targets
                if t.symbol.strip().upper() not in price_map
            )
        )
        unpriceable_new_syms: dict[str, str] = {}
        if new_syms:
            try:
                snapshots = pipeline.broker.get_intraday_snapshots(new_syms)
            except Exception as e:
                # get_intraday_snapshots is documented never to raise; guard
                # anyway so a broker fault fails CLOSED (every new name
                # refused) rather than reaching a bad-price fallback.
                logger.warning("Constructor snapshot lookup failed: %s", e)
                snapshots = {}
            for sym in new_syms:
                resolved = resolve_live_price(snapshots.get(sym))
                if resolved.is_today_print:
                    price_map[sym] = resolved.price
                else:
                    # Distinguish "only a stale prior-session price" from
                    # "no usable price at all" so the census counts them
                    # apart — the same split the resolver already draws.
                    unpriceable_new_syms[sym] = (
                        FAULT_STALE_PRICE if resolved.unavailable == ONLY_STALE else FAULT_NO_PRICE
                    )
                    logger.warning(
                        "Constructor: no fresh today price for new name %s "
                        "(%s) — refusing as unmeasurable, not sizing the buy "
                        "on a stale or mid price",
                        sym,
                        resolved.describe(),
                    )
        # Spec §2.2 — the book's risk as the constructor must ration it, both
        # already computed above (before `decide()`) so the Phase 14
        # rotation pre-check and the constructor ration against the exact
        # same numbers. Absent facts (a stage built without them) leaves
        # both None and the portfolio ceilings unenforced rather than
        # enforced against a fabricated view of the book.
        # Spec §9.4 — the SAME canonical evidence registry the PM's own
        # prompt was built from (`build_evidence_registry` is a pure
        # function of these exact inputs, so recomputing it here from the
        # identical arguments passed to `decide()` above is guaranteed to
        # agree with what PM was actually shown). Feeds the constructor's
        # agreement refusal — never invented from PM's own provenance,
        # which the PM could under-cite.
        evidence_registry = PortfolioManagerAgent.build_evidence_registry(
            analyses=analyses,
            positions=positions,
            news_intel=news_intel,
            earnings_analyses=earnings_results,
            macro_analysis=_macro_analysis_as_dict(macro_analysis),
            smart_money_findings=ctx.smart_money_findings,
            symbol_sectors=dict(ctx.symbol_sectors or {}),
        )
        # Item 112 — keep THIS session's fresh per-seat read on the context so
        # the post-decision gross de-lever can rank held names by live
        # conviction (`_enforce_gross_ceiling_by_conviction`). The registry is
        # otherwise a DecisionStage local, discarded when the stage returns —
        # exactly the stale-stance trap the prior attempt was rejected for.
        ctx.evidence_registry = evidence_registry
        # Item 112 — and the RAW seat verdicts behind it, BEFORE any admission
        # gate. The cut order must rank seat conviction about a HOLDING, and
        # `last_candidate_ranking` cannot serve: it is the post-
        # `candidate_eligibility`, post-conviction-bar ENTRY survivor list, and
        # the bar's STAY side is opposition-only by owner ruling (2026-09-25) —
        # a held name that fails the entry bar on soft grounds is dropped from
        # it with no cull reason because "it earns its right to STAY".
        try:
            ctx.seat_verdicts = PortfolioManagerAgent._collect_seat_verdicts(
                analyses=analyses,
                news_intel=news_intel,
                macro_analysis=_macro_analysis_as_dict(macro_analysis),
                earnings_analyses=earnings_results,
                smart_money_findings=ctx.smart_money_findings,
                symbol_sectors=dict(ctx.symbol_sectors or {}),
            )
        except Exception as exc:  # noqa: BLE001
            # Best-effort like the collector itself: no verdicts means the cut
            # order falls back to its buckets alone, never to a wrong order.
            logger.warning("seat verdicts unavailable for the cut order: %s", exc)
            ctx.seat_verdicts = []
        # §9.4 freshness — same pure function, same inputs, so the stances
        # the constructor refuses to pay for are exactly the ones the PM's
        # prompt marked stale. An earnings view older than
        # `EARNINGS_STANCE_MAX_AGE_DAYS` stops counting toward the agreement
        # tally on BOTH sides; it stays in the registry above, so grounding
        # still accepts it as coverage and this can only ever shrink a
        # ceiling.
        stale_sources = PortfolioManagerAgent.stale_evidence_sources(
            earnings_analyses=earnings_results,
        )
        # Item 112 — the conviction de-lever reads the registry above; it must
        # apply the SAME freshness exclusions the constructor does. Today
        # `stale_evidence_sources` returns EARNINGS only (§9.4's earnings-age
        # rule is the only freshness rule the desk has), so this excludes an
        # over-age earnings stance and nothing else — it is not a general
        # staleness sweep over macro or smart money.
        ctx.evidence_stale_sources = stale_sources
        # Item 109 — a SEPARATE mapping, never merged into the one above.
        # A macro stance broadcast onto a name whose sector the macro read
        # never mentioned cannot count FOR that name; its dissent still
        # counts against it. One-sided, so this too can only ever shrink a
        # ceiling — merging it into `stale_sources` would drop the dissent
        # as well and RAISE the net on exactly the names a bearish broad
        # read opposed. See `broadcast_macro_sources`.
        non_corroborating_sources = PortfolioManagerAgent.broadcast_macro_sources(
            registry=evidence_registry,
            positions=positions,
            macro_analysis=_macro_analysis_as_dict(macro_analysis),
            symbol_sectors=dict(ctx.symbol_sectors or {}),
        )
        # Items 109 + 112 together — the conviction cut order must honour the
        # SAME one-sided removal. A broadcast macro stance may not be the
        # thing that protects a holding from the de-lever (that would be
        # macro acting alone, which the 2026-09-25 ruling forbids), while its
        # dissent still counts against the name. Stashed separately from the
        # two-sided freshness set for exactly that reason.
        ctx.evidence_non_corroborating_sources = non_corroborating_sources
        # Conviction ledger (spec §9.5): persist every seat's side on every
        # idea — dissent included — from that same registry, BEFORE the
        # constructor runs so a construction failure cannot lose the record
        # of what the desk believed. Writes evidence rows only; the
        # `evidence_registry` handed to `construct_orders` below is the
        # identical object, unread and unmutated by this call.
        # Phase 14b — automatic opportunity-cost rotation. Behind
        # `execution.rotation_enabled` (default OFF: a no-op here). Appends
        # ONE zero-size target for a categorically-ineligible, already-
        # unprotected holding so the constructor, RiskStage and
        # ExecutionStage below treat it exactly like a PM-authored close.
        # Sits BEFORE the constructor on purpose: the freed risk must be
        # visible to `allocate_risk_budget` when it rations the new
        # candidate's BUY, and the close must pass every gate downstream.
        # Unconditional, and BEFORE the acting path: the owner's report has
        # to be able to say the comparison was made even in the (commonest)
        # session where it surfaced nothing and the acting path returns
        # silently. See `_record_rotation_precheck`.
        _record_rotation_precheck(pipeline, ctx)
        record_rotation_margins(pipeline, ctx)
        apply_rotation_recording_dispositions(
            pipeline,
            ctx,
            portfolio_decision,
            positions,
            position_history,
        )
        _record_seat_stances(
            pipeline,
            ctx,
            evidence_registry,
            [t.symbol for t in portfolio_decision.targets],
            non_corroborating_sources=non_corroborating_sources,
        )
        book_targets, refused_soft_exit = _targets_admitted_to_book(
            portfolio_decision.targets,
            positions=positions,
            total_value=total_value,
            existing_risk_pct=existing_risk_pct,
        )
        for symbol in refused_soft_exit:
            _record_soft_exit_missing_after_retry(pipeline, ctx, symbol)
        _record_soft_exit_refusal_count(pipeline, ctx, refused_soft_exit)
        if refused_soft_exit:
            logger.warning(
                "Refusing %d open target(s) %s before the ticket book: %s",
                len(refused_soft_exit),
                SOFT_EXIT_MISSING_AFTER_RETRY,
                refused_soft_exit,
            )
            add_constructor_dropped(portfolio_decision, refused_soft_exit)
        portfolio_decision.decisions = pipeline.portfolio_constructor.construct_orders(
            targets=book_targets,
            positions=positions,
            analyses=analyses,
            total_value=total_value,
            price_map=price_map,
            # New names with no fresh today print this session (item 120):
            # the constructor refuses each as a DATA FAULT rather than
            # sizing it off a stale/mid price or the TA entry fallback.
            unpriceable_symbols=unpriceable_new_syms,
            existing_risk_pct=existing_risk_pct,
            clusters=risk_clusters,
            # Live broker stops from the same heat roll-up as
            # `existing_risk_pct`: sizes a trim of a held, unanalysed name.
            live_stops=_live_stops_from_heat(ctx),
            # The tape the stop has to survive. Widening a stop past the noise
            # band is not a fixed number of ATRs — a risk-off market swings
            # wider for the same ATR reading than a trending one.
            regime=_macro_regime(macro_analysis),
            evidence_registry=evidence_registry,
            stale_sources=stale_sources,
            non_corroborating_sources=non_corroborating_sources,
            # Spec §11.2 — the session's gross-exposure ceiling, already
            # resolved from account state in the run preamble (and re-derived
            # here only on a lane where the preamble did not run). The
            # constructor sizes UNDER it; it never trims the held book.
            gross_ceiling=_session_gross_ceiling(pipeline, ctx),
            # retired board item 49, owner decision 2026-09-12 (`docs/INCIDENT_HISTORY.md`, 2026-09-14): when the risk
            # budget binds, spend it BEST-RANKED FIRST. This is the PM's own
            # `rank_verdicts` ordering, taken from the object the PM's prompt
            # was rendered from this session — never recomputed here, so the
            # order the budget is spent in is provably the order the model
            # was shown. None when no ranking was produced (no Technical
            # reads, or a PM path that never built a prompt): the allocator
            # then keeps its pre-decision ordering rather than being handed
            # an invented one.
            ranking=_session_candidate_ranking(pipeline),
        )
        # Provenance for the AI Risk Manager: which proposed symbols did the
        # deterministic constructor remove? Derived here (targets minus
        # decisions) rather than by changing construct_orders' signature.
        # HOLD decisions still count as "kept" — the symbol survived review.
        portfolio_decision.constructor_dropped = _dropped_since_proposal(portfolio_decision)
        if portfolio_decision.constructor_dropped:
            logger.info(
                "Constructor dropped %s — recorded for the Risk Manager so "
                "PM's narrative mentioning them does not read as incoherence.",
                ", ".join(portfolio_decision.constructor_dropped),
            )
        # Every drop gets a terminal, real-reason evidence row — and a DATA
        # fault (a symbol the constructor could not MEASURE) is filed under
        # its own name and paged, never counted as a trade the desk judged.
        # See `_record_constructor_drops` / `_alert_unmeasurable_symbols`.
        data_faults = _record_constructor_drops(pipeline, ctx, portfolio_decision)
        if data_faults:
            _alert_unmeasurable_symbols(data_faults)
        _record_constructor_side_flips(pipeline, ctx)
        _record_realised_concentration(
            pipeline,
            ctx,
            portfolio_decision,
            total_value,
        )
        logger.info(
            "Constructor: %d targets → %d decisions (%d BUY, %d SELL, %d SHORT, %d COVER, %d HOLD)",
            len(portfolio_decision.targets),
            len(portfolio_decision.decisions),
            sum(1 for d in portfolio_decision.decisions if d.action == "BUY"),
            sum(1 for d in portfolio_decision.decisions if d.action == "SELL"),
            sum(1 for d in portfolio_decision.decisions if d.action == "SHORT"),
            sum(1 for d in portfolio_decision.decisions if d.action == "COVER"),
            sum(1 for d in portfolio_decision.decisions if d.action == "HOLD"),
        )
        # "Proposed" evidence — the constructor's concrete order BEFORE the
        # AI Risk Manager reviews/modifies it (RiskStage persists the
        # post-review verdict/modifications separately). Together these let
        # the UI show a proposed-vs-executed delta per symbol without
        # re-deriving it from raw agent_logs text.
        for decision in portfolio_decision.decisions:
            _persist_evidence(
                pipeline.db,
                run_id=run_id,
                agent_name="portfolio_manager",
                kind="proposed_order",
                scope="symbol",
                symbol=decision.symbol,
                decision_id=decision_id,
                evidence_json=decision.model_dump_json(),
            )
            _record_pipeline_event(
                pipeline,
                ctx,
                decision.symbol,
                "portfolio_manager",
                "proposed",
                "constructor_created_order",
                action=decision.action,
            )
        ctx.portfolio_decision = portfolio_decision
        return ctx


def _journal_refused_candidates(pipeline, ctx, blocked: dict) -> None:
    """One durable `candidate_refused` event per name the PM's gates refused."""
    for symbol, reason in blocked.items():
        _record_pipeline_event(
            pipeline,
            ctx,
            symbol,
            "portfolio_manager",
            "candidate_refused",
            reason,
        )


def _journal_ranked_candidates(pipeline, ctx, ranked: list) -> None:
    """One durable `candidate_ranked` event per ranked survivor, with its score.

    Recording only: stores the composite score, its components and each
    seat's conviction/magnitude so session-to-session score noise can be
    measured. Rank is 1-based position in the PM's own order.
    """
    for rank, cand in enumerate(ranked, start=1):
        seats = {
            str(v.seat): {
                "conviction": getattr(v, "conviction", None),
                "magnitude": getattr(v, "magnitude", None),
            }
            for v in (getattr(cand, "verdicts", None) or [])
        }
        _record_pipeline_event(
            pipeline,
            ctx,
            cand.symbol,
            "portfolio_manager",
            "candidate_ranked",
            "",
            rank=rank,
            ranked_count=len(ranked),
            direction=cand.direction,
            score=cand.score,
            components=dict(cand.components),
            seats=seats,
        )
