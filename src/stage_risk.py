"""Risk stage and its private helpers (split out of src/pipeline_stages.py, step 10).

Moved verbatim in the staged split (board item 210, step 10). No behaviour change:
the class body below is byte-for-byte the text that used to live in
``src/pipeline_stages.py``, and ``src.pipeline_stages`` re-exports it so every
existing import path and every ``src.pipeline_stages.RiskStage`` patch target
still resolves to this same object.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from src.stage_risk_helpers import (  # noqa: F401  re-exported: callers import these from here
    ANALYSIS_DROP_KIND,
    UNIDENTIFIED_DROP_KEY,
    _apply_sector_unresolved_alert,
    _parse_loss_advisories,
    _persist_dropped_reasons,
    _reconcile_parse_loss,
    _record_queued_earnings_refusals,
)
from src.pipeline_stages import (  # noqa: F401  shared helpers and module-level names
    Any,
    DROP_CODE_UNSPECIFIED,
    EventCalendarCoverage,
    FOMCCoverage,
    RunContext,
    SOFT_EXIT_MISSING_AFTER_RETRY,
    _ANALYSIS_DROP_KIND,
    _dropped_since_proposal,
    _isolate_empty_soft_exit_entries,
    _macro_regime,
    _persist_evidence,
    _record_pipeline_event,
    _record_scale_advisory,
    _risk_edit_snapshot,
    _risk_event_for,
    _session_gross_ceiling,
    _start_trade_updates_early,
    _stop_trade_updates,
    agent_log_kwargs,
    evidence_gate,
    fetch_earnings_proximity,
    format_event_risk_block,
    logger,
    parse_telemetry,
    seat_acceptance_kwargs,
)

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


class RiskStage:
    """Hard filter → earnings cap → correlation → RM review → mods → re-filter.

    Reads:  ctx.portfolio_decision, ctx.positions, ctx.total_value,
            ctx.last_equity, ctx.earnings_results, ctx.macro_analysis,
            ctx.analyses, ctx.symbols_bars, ctx.data_status, ctx.news_intel,
            ctx.macro_summary, ctx.macro_events, ctx.macro_event_coverage,
            ctx.fomc_meetings, ctx.fomc_coverage

    Writes: ctx.portfolio_decision.decisions (filtered/capped/scaled),
            ctx.correlation_matrix, ctx.daily_pnl, ctx.invested_target_pct

    Returns an early-exit dict (symbol_block / hard_risk_block / rejected)
    or None when the pipeline should proceed to execution.
    """

    def __init__(self, *, pipeline: "TradingPipeline"):
        self._pipeline = pipeline

    @staticmethod
    def _build_event_risk_block(pipeline, ctx: RunContext) -> str:
        """The fetched Event Risk section handed to the Risk Manager.

        `RiskVerdict.reasoning_chain.event_risk` is a REQUIRED narrative field
        asking whether earnings or a macro release land in the next few
        sessions. Nothing fetched either fact before this: the earnings-date
        lookup (`MarketDataProvider.get_next_earnings_date`) had no callers
        anywhere, and no macro calendar existed — so a mandatory risk check was
        being answered from the model's recollection.

        Earnings are looked up for exactly the symbols RM is judging (this
        run's decisions), bounded per-symbol AND in aggregate so a stalled
        yfinance call can never delay the session. The macro calendar is REUSED
        from the research stage (`ctx.macro_events`) so one session issues one
        FRED sweep, and the FOMC schedule the same way (`ctx.fomc_meetings`),
        so one session issues one Fed calendar fetch; on a resume lane where
        research never ran, ctx carries its "not fetched" defaults and the
        block says exactly that.

        Never raises and never returns an empty string: on any failure the seat
        is shown the NOT FETCHED form. A missing section reads as a calm
        calendar, which is precisely the failure being fixed.
        """
        symbols = [d.symbol for d in (ctx.portfolio_decision.decisions if ctx.portfolio_decision else [])]
        # Every lookup below is a getattr with a default: this helper is called
        # on partially-constructed pipelines (the resume lane, and several test
        # doubles built with __new__), and an event-risk block is never worth
        # aborting a risk review over. A missing dependency degrades to the
        # labelled NOT FETCHED form, which is still an honest answer.
        event_cfg = getattr(getattr(pipeline, "config", None), "event_risk", None)
        horizon_days = getattr(event_cfg, "horizon_days", 10)
        earnings = None
        try:
            if symbols and getattr(pipeline, "market", None) is not None:
                earnings = fetch_earnings_proximity(
                    pipeline.market,
                    symbols,
                    per_symbol_timeout_s=getattr(
                        event_cfg,
                        "earnings_symbol_timeout_s",
                        8.0,
                    ),
                    total_deadline_s=getattr(event_cfg, "earnings_deadline_s", 20.0),
                )
        except Exception as e:  # noqa: BLE001
            logger.warning("Earnings proximity sweep failed: %s", e)
            earnings = None

        coverage = ctx.macro_event_coverage
        if not isinstance(coverage, EventCalendarCoverage):
            coverage = None
        events = list(ctx.macro_events or []) if coverage is not None else None
        # Same pairing rule for the FOMC half: the schedule is only shown
        # alongside the coverage object that says how far it reaches, because
        # an empty schedule with no coverage line reads as "no Fed decision
        # coming" — the exact fabrication this block exists to prevent.
        fomc_coverage = getattr(ctx, "fomc_coverage", None)
        if not isinstance(fomc_coverage, FOMCCoverage):
            fomc_coverage = None
        fomc_meetings = list(getattr(ctx, "fomc_meetings", None) or []) if fomc_coverage is not None else None
        try:
            return format_event_risk_block(
                earnings=earnings,
                events=events,
                coverage=coverage,
                horizon_days=horizon_days,
                fomc_meetings=fomc_meetings,
                fomc_coverage=fomc_coverage,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("Event-risk block render failed: %s", e)
            return format_event_risk_block(
                earnings=None,
                events=None,
                coverage=None,
                horizon_days=0,
            )

    def run(self, ctx: RunContext) -> dict | None:
        try:
            out = self._run_review(ctx)
        except Exception:
            _stop_trade_updates(self._pipeline)
            raise
        if out is not None:
            _stop_trade_updates(self._pipeline)
        return out

    def _run_review(self, ctx: RunContext) -> dict | None:
        pipeline = self._pipeline
        run_id = ctx.run_id
        portfolio_decision = ctx.portfolio_decision
        positions = ctx.positions
        total_value = ctx.total_value
        last_equity = ctx.last_equity
        earnings_results = ctx.earnings_results
        macro_analysis = ctx.macro_analysis
        analyses = ctx.analyses
        news_intel = ctx.news_intel
        data_status = ctx.data_status

        isolated = _isolate_empty_soft_exit_entries(pipeline, ctx, portfolio_decision)
        if isolated and not getattr(portfolio_decision, "decisions", None):
            logger.warning(
                "RiskStage: every BUY/SHORT was refused (%s); skipping Risk rather than vetoing an empty plan",
                SOFT_EXIT_MISSING_AFTER_RETRY,
            )
            return None

        # Cash-sweep view — same contract as DecisionStage: the RiskManager
        # must never see parked T-bills as an 84%-of-book "position" (review
        # finding: PM and RM otherwise get contradictory views of the same
        # dollars in the same run). IMPORTANT: only the LLM-facing uses (RM
        # prompt, correlation pool, has_book_to_check) take the scrubbed
        # list — the hard filter keeps RAW positions because it still needs
        # to find the vehicle in the list to exclude it from net-exposure /
        # cluster math (it no longer credits any cash from it — see the
        # 2026-08-19 SGOV/deployable-liquidity forensic note below).
        from src.execution.cash_sweep import CashSweeper

        # Direct read: `_sweeper` is a TradingPipeline method, so a pipeline
        # without it is a wiring fault and raises here.
        sweeper = pipeline._sweeper()
        rm_positions = positions
        if isinstance(sweeper, CashSweeper):
            rm_positions, _parked = sweeper.split_positions(positions)

        # Symbol guard
        before_symbol_guard = list(portfolio_decision.decisions)
        guard_kwargs = {"admitted_symbols": ctx.admitted_symbols} if ctx.admitted_symbols else {}
        portfolio_decision.decisions, symbol_blocked_reasons = pipeline.admission._filter_supported_symbols(
            portfolio_decision.decisions,
            analyses,
            positions,
            **guard_kwargs,
        )
        if symbol_blocked_reasons:
            reasons = "; ".join(dict.fromkeys(symbol_blocked_reasons))
            logger.warning("SYMBOL GUARD BLOCK: %s", reasons)
            allowed_ids = {id(d) for d in portfolio_decision.decisions}
            for decision in before_symbol_guard:
                if id(decision) not in allowed_ids:
                    _record_pipeline_event(
                        pipeline,
                        ctx,
                        decision.symbol,
                        "deterministic_gate",
                        "blocked",
                        "symbol_guard",
                        detail=reasons,
                    )
            if not portfolio_decision.decisions:
                return {"status": "symbol_block", "orders": [], "reason": reasons}
            logger.info(
                "Allowing %d supported orders through after symbol guard filter",
                len(portfolio_decision.decisions),
            )

        # Board item 186 (2026-10-01): no book is needed any more. The gate
        # no longer measures a resulting weight against a 5%-of-book cap — an
        # unread filing is an unconvicted earnings seat, so the BUY is refused
        # outright and there is nothing to size.
        before_earnings_cap = list(portfolio_decision.decisions)
        portfolio_decision.decisions = pipeline.risk_gate._refuse_queued_earnings_buys(
            portfolio_decision.decisions,
            earnings_results,
        )
        # Board item 164: the gate used to reach the log only, while this
        # symbol's `proposed_order` row (written by DecisionStage, before
        # this gate) kept the pre-gate size. Recording only.
        _record_queued_earnings_refusals(
            pipeline,
            ctx,
            before_earnings_cap,
            portfolio_decision.decisions,
        )

        daily_pnl = total_value - last_equity
        ctx.daily_pnl = daily_pnl
        # Owner mandate 2026-09-17: fully invested, always. The advisory's
        # target is the fixed mandate; macro no longer sets or lowers it.
        from src.risk.rules import DESK_INVESTED_TARGET_PCT

        invested_target_pct = DESK_INVESTED_TARGET_PCT
        ctx.invested_target_pct = invested_target_pct

        # Holding ages + system-drawdown state (2026-08-13 agent audit).
        # Normally DecisionStage already published both. On the RC2 resume lane
        # it never ran, so rebuild rather than let the gate silently lose the
        # evidence. Both builders are local DB reads with no LLM call and no
        # broker call; a failure degrades to "not provided" in the prompt,
        # never to a wrong value that reads as "no drawdown".
        #
        # Resolved HERE rather than just before the RM call (where it lived
        # until the audit §1.1 fix) because the drawdown-halve is now a
        # deterministic gate that runs BEFORE the hard risk filter, and the
        # filter is upstream of RM.
        rm_position_history = ctx.position_history
        rm_recent_performance = ctx.recent_performance
        if not rm_position_history:
            try:
                rm_position_history = pipeline._build_position_history(rm_positions)
                ctx.position_history = rm_position_history
            except Exception as e:  # noqa: BLE001
                logger.warning(
                    "RiskStage: position history rebuild failed — RM will see holding ages as unknown: %s",
                    e,
                )
                rm_position_history = {}
        if not rm_recent_performance:
            try:
                rm_recent_performance = pipeline._compute_recent_performance(last_equity)
                ctx.recent_performance = rm_recent_performance
            except Exception as e:  # noqa: BLE001
                logger.warning(
                    "RiskStage: recent-performance rebuild failed — the "
                    "seat sees no rolling-return context this run: %s",
                    e,
                )
                rm_recent_performance = {}

        # Spec §11.2 — the session's gross-exposure ceiling, resolved from
        # ACCOUNT STATE and never from PM output. The run preamble already
        # acted on it before any agent was called; reading it again here is
        # what makes the execution gate below measure new orders against the
        # same rung the constructor sized them under.
        session_gross_ceiling = _session_gross_ceiling(pipeline, ctx)

        # The rolling-return drawdown halve used to run here, scaling every
        # BUY and SHORT by 0.5 whenever `in_drawdown` was true. Removed  # retired-ok
        # 2026-09-20 on the owner's instruction together with the daily-loss
        # halt (retired item 32, docs/INCIDENT_HISTORY.md). The §11.2 gross
        # ceiling resolved above is untouched and still sizes and blocks.

        # Memoized by DecisionStage so PM and this gate score the same numbers.
        # On the RC2 resume lane DecisionStage never ran and this is the first
        # build; the helper handles both cases.
        correlation_matrix = pipeline._ensure_correlation_matrix(ctx, rm_positions)

        before_hard_gate = list(portfolio_decision.decisions)
        portfolio_decision.decisions, rule_violations, blocked_reasons = pipeline.risk_gate._filter_hard_risk_decisions(
            portfolio_decision.decisions,
            positions,
            total_value,
            invested_target_pct=invested_target_pct,
            correlation_matrix=correlation_matrix,
            cash=ctx.deployable_cash,
            gross_ceiling=session_gross_ceiling,
        )
        _apply_sector_unresolved_alert(data_status, rule_violations)
        if blocked_reasons:
            reasons = "; ".join(dict.fromkeys(blocked_reasons))
            logger.warning("HARD RISK BLOCK (BUY blocked): %s", reasons)
            allowed_ids = {id(d) for d in portfolio_decision.decisions}
            for decision in before_hard_gate:
                if id(decision) not in allowed_ids:
                    _record_pipeline_event(
                        pipeline,
                        ctx,
                        decision.symbol,
                        "deterministic_gate",
                        "blocked",
                        "hard_risk",
                        detail=reasons,
                    )
            if not portfolio_decision.decisions:
                pipeline.risk_gate._persist_hard_risk_block(ctx, reasons, stage="pre_rm")
                return {"status": "hard_risk_block", "orders": [], "reason": reasons}
            logger.info(
                "Allowing %d non-blocked orders through after hard risk filter",
                len(portfolio_decision.decisions),
            )

        # Same-session reuse (`carried_from_morning`) and an intentional
        # skip (`not_run_intraday`) are usable, not integrity failures —
        # see evidence_gate.INTEGRITY_CLEAN_STATUSES. Interpolate ONLY the
        # degraded seats: dumping the full dict re-smuggled reuse words
        # into RM's prompt on a mixed tick (measured 2026-09-16).
        degraded = {k: v for k, v in data_status.items() if evidence_gate.counts_as_degraded(v)}
        if len(degraded) >= 2:
            from src.risk.rules import RiskViolation as _RV

            rule_violations.append(
                _RV(
                    rule="data_degraded",
                    message=(
                        f"Upstream data sources degraded: {', '.join(sorted(degraded))} "
                        f"(status: {degraded}). Decisions may be built on incomplete input — "
                        f"RM should consider scale_all_buys < 1.0."
                    ),
                    value=float(len(degraded)),
                    limit=1.0,
                )
            )
            logger.warning("Morning data degradation: %s", data_status)

        # Parse-level losses anywhere in this session (2026-09-02). Before
        # this, an analysis discarded over one malformed field was a single
        # ERROR log line nobody counted, and an explicit null silently
        # replaced by a default left no trace at all. Both are inputs the
        # analysts produced and the desk then failed to use — invisible
        # under-deployment, and the expensive kind, because a candidate the
        # Portfolio Manager never sees cannot be traded and cannot be
        # measured as a miss either.
        #
        # ADVISORY, never blocking — the same non-blocking seam
        # `data_degraded`, `correlation_coverage_gap` and
        # `pm_audit_step_missing` above already use. It reaches the Risk
        # Manager's prompt through `rule_violations` and the operator through
        # the session log; no order is blocked by it, because a parse loss is
        # evidence about COVERAGE, not about the soundness of the orders that
        # did survive.
        #
        # Read LIVE rather than from a research-stage snapshot: the Portfolio
        # Manager parses in DecisionStage, AFTER research, and
        # `TargetPosition.thesis_invalid_if` is one of the two fields this
        # whole change is about. A snapshot taken at the end of research would
        # miss every PM-side loss.
        ctx.dropped_analyses = parse_telemetry.dropped_snapshot()
        ctx.null_coerced_fields = parse_telemetry.snapshot()
        dropped = ctx.dropped_analyses
        # WHY each row was dropped, keyed the same as `dropped` (board item
        # 158). Read live beside the counts so the reason and the count come
        # from the same telemetry snapshot.
        dropped_reasons = parse_telemetry.dropped_reasons_snapshot()
        # The stable code beside the prose, from the same telemetry pass.
        dropped_reason_codes = parse_telemetry.dropped_reason_codes_snapshot()
        if dropped:
            # RECONCILED against the book before anything is said about it.
            # A drop whose retry succeeded leaves its counter standing for
            # ever (by design — see `_reconcile_parse_loss`), and until
            # 2026-09-23 that made the advisory assert "absent from the book
            # below" about symbols sitting in the book. Measured on
            # 2026-09-21: META, held with stops and re-sized by the
            # constructor in the same run, was reported to the Risk Manager as
            # discarded and absent, and the seat flagged the environment
            # degraded on it.
            #
            # The book is what the risk seat is actually SHOWN: the positions
            # in the prompt plus the orders proposed to it. Deliberately NOT
            # `portfolio_decision.targets` — a target the constructor dropped
            # never reaches the seat, so counting it as present would be the
            # same false reassurance in the other direction — and deliberately
            # not `analyses`, which proves only that a row parsed.
            book_symbols = {str(p.symbol).upper() for p in (rm_positions or []) if getattr(p, "symbol", None)} | {
                str(d.symbol).upper() for d in (portfolio_decision.decisions or []) if getattr(d, "symbol", None)
            }
            recovered_names, lost_names = _reconcile_parse_loss(
                dropped,
                book_symbols,
            )
            # Board item 158: file each dropped stock's REASON to
            # `specialist_evidence`, tied to symbol + run, so a later reader
            # can tell why a name was absent without the rotated log. Purely
            # observational — a write failure never touches the risk decision.
            _persist_dropped_reasons(
                getattr(pipeline, "db", None),
                run_id,
                dropped,
                dropped_reasons,
                book_symbols,
                dropped_reason_codes,
            )
            rule_violations.extend(_parse_loss_advisories(dropped, book_symbols, dropped_reasons))
            if lost_names:
                logger.error(
                    "Analysis parse loss reached the risk stage: %d item(s) — %s",
                    sum(lost_names.values()),
                    ", ".join(lost_names),
                )
            if recovered_names:
                logger.warning(
                    "Analysis parse loss RECOVERED by retry before the risk "
                    "stage: %d item(s) — %s; present in the book, reported as "
                    "a cost note rather than as missing coverage",
                    sum(recovered_names.values()),
                    ", ".join(recovered_names),
                )

        nulled = ctx.null_coerced_fields
        if nulled:
            from src.risk.rules import RiskViolation as _RV

            detail = ", ".join(
                f"{model}.{field}x{n}" for (model, field), n in sorted(nulled.items(), key=lambda kv: -kv[1])
            )
            n_nulled = sum(nulled.values())
            rule_violations.append(
                _RV(
                    rule="analysis_field_nulled",
                    message=(
                        f"{n_nulled} field(s) arrived as an explicit null and took "
                        f"their schema default: {detail}. The analyses were KEPT "
                        f"(the alternative — dropping them — is worse), but a "
                        f"nulled `thesis_invalid_if` means that idea has no "
                        f"soft-exit trigger and will be managed on the hard stop "
                        f"alone."
                    ),
                    value=float(n_nulled),
                    limit=0.0,
                )
            )

        ctx.hygiene_violations = parse_telemetry.hygiene_snapshot()
        hygiene = ctx.hygiene_violations
        if hygiene:
            # Item 157's runtime check (2026-09-23), routed the same way
            # `analysis_parse_loss`/`analysis_field_nulled` already are —
            # adversary review found the earlier research-stage-only log
            # line never reached anywhere a human actually looks (the
            # owner sees Telegram and the dashboard, not logs); this
            # reaches the Risk Manager's own advisory the same way those
            # two do. Informational only — a hygiene violation never costs
            # the row and this rule is not one the risk seat can veto on
            # (docs/WORK.md: hard limits are code-enforced; over guidelines
            # the risk seat may only resize), it only makes the finding
            # visible to whatever reads the RM's review.
            from src.risk.rules import RiskViolation as _RV

            n_hygiene = sum(hygiene.values())
            detail = ", ".join(
                f"{model}.{kind}x{n}" if n > 1 else f"{model}.{kind}"
                for (model, kind), n in sorted(hygiene.items(), key=lambda kv: -kv[1])
            )
            rule_violations.append(
                _RV(
                    rule="tech_answer_hygiene",
                    message=(
                        f"{n_hygiene} tech-seat answer(s) this session carried "
                        f"fenced markdown or an undeclared key despite a strict "
                        f"response schema: {detail}. Model name is tagged with "
                        f"the actual provider that answered — only openrouter/"
                        f"google were ever sent a schema, so a count against "
                        f"any other provider reflects no schema being sent, not "
                        f"one failing to suppress. Never blocks anything; the "
                        f"row was still parsed and used. See docs/WORK.md item 157."
                    ),
                    value=float(n_hygiene),
                    limit=0.0,
                )
            )

        has_book_to_check = len(rm_positions) >= 2 or any(
            d.action in ("BUY", "SHORT") for d in portfolio_decision.decisions
        )
        if (not correlation_matrix) and has_book_to_check:
            from src.risk.rules import RiskViolation as _RV

            rule_violations.append(
                _RV(
                    rule="correlation_coverage_gap",
                    message=(
                        "Correlation matrix is empty (insufficient bar data this run). "
                        "The cluster-concentration advisory is DISABLED. Consider "
                        "scale_all_buys < 1.0 until coverage returns, especially for "
                        "thematic names (AI, semis, energy)."
                    ),
                    value=0.0,
                    limit=2.0,
                )
            )
            logger.warning(
                "Correlation matrix empty — cluster risk check disabled for this run (positions=%d, buy_candidates=%d)",
                len(positions),
                sum(1 for d in portfolio_decision.decisions if d.action == "BUY"),
            )

        # 2026-08-13 agent audit — "premortem/observability". `premortem_check`
        # and `continuity_check` are MANDATORY in portfolio_manager.md but
        # default to "" in ReasoningChain, so PM skipping the two disconfirming
        # steps produced a clean parse, a clean log line and a clean verdict.
        # The step could vanish and nothing in the system would say so.
        #
        # The schema stays permissive on purpose (pre-2026-06 logs carry
        # neither field and replay must keep parsing them — see
        # src/models.py::ReasoningChain), so the observability lands here as an
        # ADVISORY, the same non-blocking seam `data_degraded` and
        # `correlation_coverage_gap` already use. No order is blocked by it;
        # RM's prompt requires every advisory to be answered in the matching
        # reasoning_chain field, which is what makes the omission visible.
        rc_now = portfolio_decision.reasoning_chain
        if rc_now is not None:
            missing_audit_steps = [
                name
                for name, value in (
                    ("premortem_check", rc_now.premortem_check),
                    ("continuity_check", rc_now.continuity_check),
                )
                if not (value or "").strip()
            ]
            if missing_audit_steps:
                from src.risk.rules import RiskViolation as _RV

                rule_violations.append(
                    _RV(
                        rule="pm_audit_step_missing",
                        message=(
                            f"PM returned no {' and no '.join(missing_audit_steps)} — "
                            f"mandatory in its prompt, optional in the schema, so this "
                            f"raised no parse error. The disconfirming/red-team step of "
                            f"today's plan was NOT performed. Weigh the plan as unaudited "
                            f"in that respect and address it in "
                            f"`reasoning_chain.overall`."
                        ),
                        value=float(len(missing_audit_steps)),
                        limit=0.0,
                    )
                )
                logger.warning(
                    "PM reasoning chain missing mandatory audit step(s): %s "
                    "(run_id=%s) — surfaced to RM as a pm_audit_step_missing advisory",
                    ", ".join(missing_audit_steps),
                    run_id,
                )

        # 2026-08-19 SGOV/deployable-liquidity forensic: RM used to be told
        # `cash + parked SGOV value`, the same overstated figure PM saw —
        # RM's cash_only / sizing_sanity audit was therefore auditing PM
        # against a number neither of them could actually spend same-day.
        # RM now gets `ctx.deployable_cash` (settled, non-margin) plus the
        # parked reserve separately/informationally via `reserve_balance`.
        rm_cash = ctx.deployable_cash
        rm_reserve_balance = 0.0
        if isinstance(sweeper, CashSweeper):
            rm_reserve_balance = sweeper.parked_value(ctx.positions)

        rm_event_risk_block = self._build_event_risk_block(pipeline, ctx)

        # Recompute against the list the Risk Manager is about to be shown.
        # Several gates between construction and here can remove SOME orders
        # and pass the rest through; a name dropped by one of those was
        # missing from the order list AND from the drop list. See
        # `_dropped_since_proposal`.
        portfolio_decision.constructor_dropped = _dropped_since_proposal(portfolio_decision)

        # Handshake overlaps the review so auth is not serial after Risk.
        _start_trade_updates_early(pipeline, ctx)

        verdict, rm_result = pipeline.risk_manager.review(
            portfolio_decision=portfolio_decision,
            positions=rm_positions,
            macro_summary=ctx.macro_summary,
            rule_violations=rule_violations,
            tech_analyses=analyses,
            news_intel=news_intel,
            earnings_analyses=earnings_results,
            # audit round 2: the veto layer's rr_audit / sizing_sanity steps
            # ran blind — no equity, no cash, no weights.
            total_value=total_value,
            cash=rm_cash,
            reserve_balance=rm_reserve_balance,
            position_history=rm_position_history,
            recent_performance=rm_recent_performance,
            # Audit §1.3 — same heat object PM sized against, so RM audits the
            # book's real risk instead of re-deriving it from notional weights.
            heat=getattr(ctx.facts, "heat", None) if ctx.facts else None,
            # None, not a hand-typed 25.0: absent facts, the agent resolves
            # the ceiling from `risk.max_portfolio_risk_pct` in the live
            # settings rather than from a literal copied into this call site.
            risk_ceiling_pct=(getattr(ctx.facts, "risk_ceiling_pct", None) if ctx.facts else None),
            # The fetched answer to `reasoning_chain.event_risk` — see
            # RiskStage._build_event_risk_block.
            event_risk_block=rm_event_risk_block,
        )

        rm_log_kwargs = agent_log_kwargs(rm_result)
        if verdict is None:
            rm_log_kwargs["status"] = "agent_failure"
        pipeline.db.insert_agent_log(
            **seat_acceptance_kwargs(
                "risk_manager_unparseable_output" if verdict is None else None,
                result=rm_result,
            ),
            agent_name="risk_manager",
            run_id=run_id,
            # "violations" was wrong AND owner-facing: this string is what
            # `CandidateDetailModal` shows on the dashboard, and by this point
            # `_filter_hard_risk_decisions` has already dropped every hard
            # breach, so the count can only ever be advisories (item 162).
            input_summary=(f"{len(portfolio_decision.decisions)} trades, {len(rule_violations)} engine advisories"),
            input_message=rm_result.user_message,
            output_summary=f"Approved: {verdict.approved if verdict else 'error'}",
            full_response=rm_result.raw_text,
            model=rm_result.model,
            tokens_used=rm_result.tokens_used,
            input_tokens=rm_result.input_tokens,
            output_tokens=rm_result.output_tokens,
            cost_usd=rm_result.cost_usd,
            decision_id=ctx.decision_id,
            **rm_log_kwargs,
        )

        if verdict:
            _persist_evidence(
                pipeline.db,
                run_id=run_id,
                agent_name="risk_manager",
                kind="verdict",
                scope="run",
                decision_id=ctx.decision_id,
                evidence_json=verdict.model_dump_json(),
            )
            for mod in verdict.modifications:
                _persist_evidence(
                    pipeline.db,
                    run_id=run_id,
                    agent_name="risk_manager",
                    kind="modification",
                    scope="symbol",
                    symbol=mod.symbol,
                    decision_id=ctx.decision_id,
                    evidence_json=mod.model_dump_json(),
                )
            # Phase 10.1 — the per-symbol audit trail. Written for EVERY
            # refusal the verdict carries, including one naming a symbol not
            # in the plan, so "why was this name refused" stays answerable per
            # name and not only through the run-scoped verdict blob.
            for rejection in verdict.rejected_symbols:
                _persist_evidence(
                    pipeline.db,
                    run_id=run_id,
                    agent_name="risk_manager",
                    kind="rejection",
                    scope="symbol",
                    symbol=rejection.symbol,
                    decision_id=ctx.decision_id,
                    evidence_json=rejection.model_dump_json(),
                )

        if verdict is None:
            logger.error(
                "Risk manager AGENT FAILURE: output remained unparseable after "
                "bounded repair; no trading verdict exists",
            )
            for decision in portfolio_decision.decisions:
                _record_pipeline_event(
                    pipeline,
                    ctx,
                    decision.symbol,
                    "risk",
                    "failed",
                    "risk_manager_unparseable_output",
                )
            _persist_evidence(
                pipeline.db,
                run_id=run_id,
                agent_name="risk_manager",
                kind="agent_failure",
                scope="run",
                decision_id=ctx.decision_id,
                evidence_json=('{"failure":"unparseable_output","stage":"risk_manager","verdict":null}'),
            )
            return {
                "status": "agent_failure",
                "orders": [],
                "reason": "risk_manager_unparseable_output",
            }

        # WHOLE-PLAN veto REMOVED. Owner ruling 2026-09-24 (final): the risk
        # seat may NEVER cancel or reject the whole batch of new trades. Its
        # only levers are (a) SHRINK — `scale_all_buys` (all the way to 0.0 to
        # stop new buying) and per-trade `modifications`; and (b) DROP specific
        # named NEW entries via `rejected_symbols`. It must never block a
        # protective exit or touch existing holdings.
        #
        # `approved=False` is therefore a NO-OP for batch rejection. It is
        # recorded in the durable trail so we can see the seat was uneasy, but
        # it never stops the plan: the run always proceeds to apply the drops +
        # modifications + scale below, and the survivors flow through the
        # deterministic hard gate (gross/exposure), which runs before AND after
        # scaling and is the only thing that can block on a hard limit.
        #
        # DELIBERATE: the old "veto the whole plan on an incoherent
        # reasoning_chain / >5 mods" capability is gone WITH the batch veto —
        # that is the ruling, not an oversight. The seat records unease and
        # proceeds; a coherence concern is expressed by dropping/shrinking the
        # affected names, never by stopping the batch.
        if not verdict.approved:
            logger.info(
                "Risk manager set approved=False; per owner ruling 2026-09-24 "
                "this no longer rejects the batch — recording and proceeding to "
                "apply rejected_symbols + modifications + scale_all_buys. "
                "Reasoning: %s",
                verdict.reasoning,
            )
            _record_pipeline_event(
                pipeline,
                ctx,
                None,
                "risk",
                "batch_veto_ignored",
                verdict.reasoning,
                gate="risk_manager_batch_veto_disabled",
                reason_category=getattr(verdict, "reason_category", None),
            )

        # PER-SYMBOL refusal (spec Phase 10.1). One failing leg dies alone —
        # this is the seat's ONLY way to remove a trade, and it can only remove
        # a NEW entry. Before Phase 10.1, `approved` was the only refusal the
        # schema had, so a single sub-floor R/R took the whole plan with it —
        # run-64290730 (2026-09-01) refused the morning citing XLE alone and
        # killed CHPX, a passing trade in a different sector, with it.
        rejections = verdict.rejections_by_symbol()
        refused_decisions: list = []
        protected_exit_symbols: list[str] = []
        if rejections:
            surviving: list = []
            for decision in portfolio_decision.decisions:
                reason = rejections.get(decision.symbol.strip().upper())
                if reason is None:
                    surviving.append(decision)
                    continue
                # PROTECTIVE-EXIT GUARD (owner ruling 2026-09-24): the seat may
                # only drop a NEW entry (BUY / SHORT). A SELL or COVER is a
                # protective exit and a HOLD touches an existing holding —
                # none of these may EVER be dropped or blocked by the seat,
                # under any path. Naming one in `rejected_symbols` is recorded
                # and IGNORED; the decision stays in the plan.
                if decision.action not in ("BUY", "SHORT"):
                    surviving.append(decision)
                    protected_exit_symbols.append(decision.symbol)
                    logger.warning(
                        "Risk manager named %s (%s) in rejected_symbols, but a "
                        "protective exit / holding is never droppable by the "
                        "seat — keeping it. Reason given: %s",
                        decision.symbol,
                        decision.action,
                        reason,
                    )
                    _record_pipeline_event(
                        pipeline,
                        ctx,
                        decision.symbol,
                        "risk",
                        "exit_refusal_ignored",
                        reason,
                        gate="risk_manager_exit_protected",
                        action=decision.action,
                    )
                    continue
                refused_decisions.append(decision)
                logger.info(
                    "Risk manager REFUSED %s (the rest of the plan is unaffected): %s",
                    decision.symbol,
                    reason,
                )
                _record_pipeline_event(
                    pipeline,
                    ctx,
                    decision.symbol,
                    "risk",
                    "rejected",
                    reason,
                )
            matched = {d.symbol.strip().upper() for d in refused_decisions} | {
                s.strip().upper() for s in protected_exit_symbols
            }
            unmatched = sorted(set(rejections) - matched)
            if unmatched:
                logger.warning(
                    "Risk manager refused %s, which is not in the proposed plan — no-op (evidence still recorded)",
                    ", ".join(unmatched),
                )
            portfolio_decision.decisions = surviving

            # `refused_decisions` guards the case where the refusals matched
            # nothing: an empty plan plus a stray symbol name is not a
            # refusal of anything and must not become one.
            if refused_decisions and not surviving:
                # Every leg refused INDIVIDUALLY. This is the SUM of per-symbol
                # drops, not a whole-batch veto: it is reachable only when every
                # decision was a droppable NEW entry (BUY/SHORT) and each was
                # named on its own merits. A protective exit is guarded into
                # `surviving` above, so it can never be here — this path cannot
                # kill a SELL/COVER. There is simply nothing left to place, and
                # each symbol carries its OWN reason.
                reasons = "; ".join(
                    f"{sym}: {rejections[sym]}" for sym in sorted({d.symbol.strip().upper() for d in refused_decisions})
                )
                logger.info(
                    "Every proposed trade was refused on its own merits: %s",
                    reasons,
                )
                return {"status": "rejected", "orders": [], "reason": reasons}

        # SAME RULES AS BUYING — owner ruling 2026-10-13 ("selling is not set
        # up the same as buying and it should be. Same rules should apply").
        # Every PM SELL/REDUCE/COVER — morning and intraday, and the half-hour
        # check's rotation close, which reaches this list as a zero-size
        # target — is decided by ONE gate (`src/exits/same_rules_gate.py`): a
        # holding is sold by judgement only when it FAILS the entry bar a buy
        # of it would have to pass now, judged on this run's seat views. If
        # that bar cannot be evaluated the sale is dropped and the stop
        # stays; a name bought today is never sold by judgement. This
        # REPLACES the "provably false claim" check and the "my numbers
        # improved" veto as deciders (ETN/FLNC/AMD 12-13 Oct were sold while
        # protected, on reasons nothing measured against the bar).
        # Structural protection is still read and logged as evidence.
        if portfolio_decision.decisions:
            from src.exits.same_rules_gate import is_judgement_sale, judge_holding, views_from_pipeline

            hd_surviving: list = []
            hd_blocked: list[tuple[str, str]] = []
            for decision in portfolio_decision.decisions:
                if not is_judgement_sale(decision.action):
                    hd_surviving.append(decision)
                    continue
                symbol_u = decision.symbol.strip().upper()
                hist = rm_position_history.get(decision.symbol) or {}
                pos = next(
                    (p for p in rm_positions if p.symbol.upper() == symbol_u),
                    None,
                )
                is_short = bool(pos and pos.qty < 0) or decision.action == "COVER"
                try:
                    protection = pipeline._structural_protection_for_holding(
                        symbol=symbol_u,
                        thesis_invalid_if=hist.get("thesis_invalid_if"),
                        entry_price=hist.get("entry_price"),
                        stop_loss=hist.get("stop_loss"),
                        is_short=is_short,
                        run_id=run_id,
                    )
                    logger.info(
                        "Structural protection for %s (evidence only): protected=%s basis=%s — %s",
                        symbol_u,
                        protection.protected,
                        protection.basis,
                        protection.detail,
                    )
                except Exception as e:  # noqa: BLE001
                    logger.warning("Structural protection read failed for %s (evidence only): %s", symbol_u, e)
                gate_verdict = judge_holding(views_from_pipeline(pipeline, ctx, symbol_u, is_short=is_short))
                _record_pipeline_event(
                    pipeline,
                    ctx,
                    decision.symbol,
                    "risk",
                    f"same_rules_gate_{gate_verdict.basis}",
                    f"{decision.action} {symbol_u}: {gate_verdict.detail}",
                )
                if gate_verdict.sell_allowed:
                    hd_surviving.append(decision)
                    continue
                logger.warning(
                    "Same-rules gate REFUSED %s %s — %s (proposal: %s)",
                    decision.action,
                    symbol_u,
                    gate_verdict.detail,
                    (decision.reasoning or "")[:200],
                )
                _record_pipeline_event(pipeline, ctx, decision.symbol, "risk", "rejected", gate_verdict.detail)
                hd_blocked.append((symbol_u, gate_verdict.detail))

            if hd_blocked:
                portfolio_decision.decisions = hd_surviving
                if not hd_surviving:
                    reasons = "; ".join(f"{sym}: {why}" for sym, why in hd_blocked)
                    logger.info("Every remaining trade was refused by the same-rules gate: %s", reasons)
                    return {
                        "status": "rejected",
                        "orders": [],
                        "reason": reasons,
                    }

        # Board item 164: what each surviving leg looked like BEFORE the
        # seat's edits and `scale_all_buys`, so the per-symbol `risk` event
        # below can say what actually changed rather than stamping one
        # constant on every symbol. Recording only.
        pre_rm_fields = _risk_edit_snapshot(portfolio_decision.decisions)

        if verdict.modifications:
            # Board items 135 + 155. An `allocation_pct` edit that ENLARGES an
            # entry is refused by `_apply_risk_modifications` guard 1b, which
            # now covers SHORT as well as BUY. It used to test
            # `decision.action == "BUY"` only, and the SHORT half was patched
            # in HERE, one layer out, by a second sweep over the same
            # decisions — solely because `src/pipeline.py` was locked by
            # another workstream on 2026-09-18. That sweep is DELETED and
            # must not come back: one rule, one enforcement point, inside the
            # guard that already owns it. `tests/test_pipeline_stages.py`
            # fails the build if a duplicate reappears in this file.
            unapplied_mods: list[dict] = []
            portfolio_decision.decisions, rejected_mods = pipeline.risk_gate._apply_risk_modifications(
                portfolio_decision.decisions,
                verdict.modifications,
                symbols_bars=getattr(ctx, "symbols_bars", None),
                unapplied=unapplied_mods,
            )
            rejected_mods = list(rejected_mods)
            # A modification this method refused (exit silently zeroed, or a
            # stop/target edit that would have shipped a reward:risk / noise-
            # band floor breach) must be a visible, distinguishable event —
            # not an edit that just vanishes. See `_apply_risk_modifications`
            # docstring, guards 1 and 2 (2026-09-03 audit).
            # Board item 164: the dropped / not-applied outcomes
            # `_apply_risk_modifications` used to log and nothing else. Each
            # entry already carries its own outcome, gate and reason.
            #
            # An edit naming a symbol with no decision in this stage's plan
            # is filed RUN-scoped with the symbol in the payload: a
            # symbol-scoped row would make `src/refusal_signature.py` count
            # a name the run never considered as a candidate, or overwrite
            # the real refusal of a name the seat already refused above.
            in_plan = {sym for sym, _action in pre_rm_fields}
            for rejected in list(rejected_mods) + unapplied_mods:
                _details = {k: v for k, v in rejected.items() if k not in ("symbol", "reason", "outcome")}
                _sym = rejected["symbol"]
                if str(_sym or "").strip().upper() not in in_plan:
                    _details["symbol_named"] = _sym
                    _sym = None
                _record_pipeline_event(
                    pipeline,
                    ctx,
                    _sym,
                    "risk",
                    rejected.get("outcome", "modification_rejected"),
                    rejected["reason"],
                    **_details,
                )

        portfolio_decision.decisions, scale, scale_advised = _record_scale_advisory(
            portfolio_decision.decisions,
            verdict,
        )
        # Board items 134 + 162 (owner ruling 2026-09-25, reaffirming
        # 2026-09-19). `scale_all_buys` is ADVISORY on entries: a model-picked,
        # unverifiable portfolio-wide multiplier may not size real trades. The
        # seat's exposure concern and its stated reason are RECORDED here per
        # flagged entry — the same owner-facing trace board item 136 built for
        # the old scale-driven drop — but no allocation_pct is changed and no
        # trade is dropped. The hard aggregate limits below remain the real
        # constraint. `scaled_out` (a drop) can no longer occur from scaling.
        _scale_reason = (getattr(verdict, "reasoning", None) or "").strip()
        for _sym, _alloc in scale_advised:
            _record_pipeline_event(
                pipeline,
                ctx,
                _sym,
                "risk",
                "scale_advisory",
                f"risk seat set scale_all_buys={scale:.2f}, a portfolio-wide "
                f"exposure concern — ADVISORY ONLY on entries (owner ruling "
                f"2026-09-25): {_sym}'s entry allocation_pct {_alloc:.2f}% is "
                f"UNCHANGED and the trade is NOT dropped. The hard aggregate "
                f"limits (gross ceiling, per-trade risk %, correlation / "
                f"at-risk budget, per-name cap) remain the constraint. RM "
                f"reason: "
                f"{_scale_reason[:300] if _scale_reason else 'none stated'} "
                f"(category {getattr(verdict, 'reason_category', None)!r})",
                field="allocation_pct",
            )

        if verdict.modifications or scale < 1.0 or refused_decisions:
            portfolio_decision.decisions, post_mod_violations, blocked_reasons = (
                pipeline.risk_gate._filter_hard_risk_decisions(
                    portfolio_decision.decisions,
                    positions,
                    total_value,
                    invested_target_pct=invested_target_pct,
                    correlation_matrix=correlation_matrix,
                    cash=ctx.deployable_cash,
                    gross_ceiling=session_gross_ceiling,
                )
            )
            _apply_sector_unresolved_alert(data_status, post_mod_violations)
            if blocked_reasons:
                reasons = "; ".join(dict.fromkeys(blocked_reasons))
                logger.warning("HARD RISK BLOCK AFTER MODIFICATIONS: %s", reasons)
                if not portfolio_decision.decisions:
                    pipeline.risk_gate._persist_hard_risk_block(ctx, reasons, stage="post_rm_modifications")
                    return {"status": "hard_risk_block", "orders": [], "reason": reasons}

        for decision in portfolio_decision.decisions:
            outcome, reason, details = _risk_event_for(
                decision,
                pre_rm_fields,
                verdict,
                scale,
                field_aliases=getattr(pipeline, "_FIELD_ALIASES", None),
            )
            _record_pipeline_event(
                pipeline,
                ctx,
                decision.symbol,
                "risk",
                outcome,
                reason,
                **details,
            )
            _record_pipeline_event(
                pipeline,
                ctx,
                decision.symbol,
                "deterministic_gate",
                "allowed",
                "post_risk_checks_passed",
            )
        return None
