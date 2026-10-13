"""Morning session (moved verbatim from TradingPipeline)."""

from __future__ import annotations

import logging
from src.cost_circuit import PaidAnalysisSuspended
from src.pipeline_context import RunContext
from src.sessions.termination import SessionTerminated

logger = logging.getLogger(__name__)


class MorningSession:
    """Morning session (moved verbatim from TradingPipeline)."""

    # Declared beside the audit that writes `stop_coverage_gaps`; read by
    # src/sessions/stop_audit.py and enforced by tests/test_protection_undetermined.py.
    AUDITS_BROKER_STOPS = True

    def __init__(
        self,
        *,
        activate_cost_session,
        compute_deployable_cash,
        cost_circuit_status,
        decision_stage,
        discharge_deferred_gross_ceiling,
        drain_pending_protection_restores,
        drain_pending_repegs,
        enforce_gross_ceiling,
        enforce_gross_ceiling_by_conviction,
        evidence_gate_skip,
        execution_stage,
        force_delever,
        heal_lost_research_seats,
        install_sigterm_unwind,
        is_trading_day,
        kill_switch_halt_result,
        paid_suspension_after_late_safety,
        reconcile_fills,
        reconcile_orphan_pending_submits,
        reconcile_stop_coverage,
        reconcile_stop_out_fills,
        repair_stops_on_kill,
        record_account_snapshot,
        release_retired_cash_park,
        require_paid_analysis,
        restore_sigterm,
        risk_stage,
        surface_reconcile_outcomes,
        sync_positions_from_broker,
        broker,
        config,
        market,
        morning_research_stage,
    ) -> None:
        self._activate_cost_session = activate_cost_session
        self._compute_deployable_cash = compute_deployable_cash
        self._cost_circuit_status = cost_circuit_status
        self._decision_stage = decision_stage
        self._discharge_deferred_gross_ceiling = discharge_deferred_gross_ceiling
        self._drain_pending_protection_restores = drain_pending_protection_restores
        self._drain_pending_repegs = drain_pending_repegs
        self._enforce_gross_ceiling = enforce_gross_ceiling
        self._enforce_gross_ceiling_by_conviction = enforce_gross_ceiling_by_conviction
        self._evidence_gate_skip = evidence_gate_skip
        self._execution_stage = execution_stage
        self._force_delever = force_delever
        self._heal_lost_research_seats = heal_lost_research_seats
        self._install_sigterm_unwind = install_sigterm_unwind
        self._is_trading_day = is_trading_day
        self._kill_switch_halt_result = kill_switch_halt_result
        self._paid_suspension_after_late_safety = paid_suspension_after_late_safety
        self._reconcile_fills = reconcile_fills
        self._reconcile_orphan_pending_submits = reconcile_orphan_pending_submits
        self._reconcile_stop_coverage = reconcile_stop_coverage
        self._reconcile_stop_out_fills = reconcile_stop_out_fills
        self._repair_stops_on_kill = repair_stops_on_kill
        self._record_account_snapshot = record_account_snapshot
        self._release_retired_cash_park = release_retired_cash_park
        self._require_paid_analysis = require_paid_analysis
        self._restore_sigterm = restore_sigterm
        self._risk_stage = risk_stage
        self._surface_reconcile_outcomes = surface_reconcile_outcomes
        self._sync_positions_from_broker = sync_positions_from_broker
        self._broker = broker
        self._config = config
        self._market = market
        self._morning_research_stage = morning_research_stage

    def run(self) -> dict:
        ctx = RunContext.start("morning")
        run_id = ctx.run_id
        logger.info("=== Morning run started: %s ===", run_id)

        if not self._is_trading_day():
            logger.info("Morning run skipped: market closed for non-trading day")
            return {"status": "market_holiday", "orders": [], "run_id": run_id}

        halt = self._kill_switch_halt_result(run_id)
        if halt is not None:
            return halt

        self._activate_cost_session(run_id, "morning")

        # The wrapper kills a slow morning with SIGTERM 30s before SIGKILL,
        # and that is a DOCUMENTED, OBSERVED death mode for this very
        # function. Without this, SIGTERM ends the process where it stands
        # and the `finally` below — which pays the deferred §11.2 gross
        # ceiling — never runs. Converting it to an unwind spends part of
        # that grace window on the session's outstanding safety debts.
        _prior_sigterm = self._install_sigterm_unwind("morning")

        try:
            # 0a. FIRST BROKER ACTION OF THE DAY: broker-truth coverage audit
            # (independent of the WAL). Catches any long that went naked
            # WITHOUT leaving a recovery row — and, since spec §11.1's hybrid
            # fractional stops, RE-PLACES the sub-share DAY stops that the
            # broker expired at yesterday's close.
            #
            # This used to run at 0b, after three drain passes that each make
            # their own broker round-trips. Every second it spent waiting was
            # a second the fractional remainder of every held position sat
            # unprotected into an open market, and the open is exactly when
            # that matters most. The owner accepted a bounded OVERNIGHT
            # exposure; he did not accept it bleeding into the session, so
            # the unprotected window at the open is now as short as this
            # system can make it.
            #
            # Symbols the drain owns are skipped by the reconciler either way
            # (it reads `get_pending_protection_restores` itself), so moving
            # ahead of the drain changes nothing for them — the drain still
            # restores their coverage microseconds later, exactly as before.
            coverage_gaps = self._reconcile_stop_coverage()
            # 0a'. Sweep retired (owner mandate 2026-09-17): sell any T-bill
            # vehicle still held into cash before any seat reads the book.
            self._release_retired_cash_park(run_id)
            # 0b. Drain orphaned protection-restore intents from prior
            # sessions where finalize had to bail (lingering SELL didn't
            # converge, or broker API hiccup). Each drained row brings a
            # symbol's stop coverage back in line with broker reality.
            drained = self._drain_pending_protection_restores()
            self._drain_pending_repegs()
            # audit F4: resolve BUY write-ahead orphans from a prior
            # crashed session before this run touches positions/cash.
            self._reconcile_orphan_pending_submits()
            # 0c. Broker-truth EXIT audit (2026-08-28 ONDS/CCJ): a protective
            # stop firing overnight is exactly the case morning must catch
            # first — the position has been closed for hours by the time
            # this runs, and every other session entry point runs this same
            # check again in case morning's own attempt failed.
            #
            # Item 173(2): unlike intra/evening, this site is NOT reordered to
            # run `_reconcile_fills` first. Morning's `_reconcile_fills` lives
            # in the method-end `finally:` block, reconciling THIS session's
            # own just-submitted orders after execution — there are no stale
            # 'submitted' SELLs from earlier today for it to resolve here, so
            # the false-gap page the reorder prevents cannot arise at morning,
            # and moving it ahead would strand this session's fills.
            reco = None
            try:
                reco = self._reconcile_stop_out_fills(run_id)
            except Exception as exc:  # noqa: BLE001
                logger.warning("morning stop-out reconcile failed (non-fatal): %s", exc)
            # Item 101: surface a broker-made stop-out / re-protection to the
            # owner — the write-backs above are otherwise silent.
            self._surface_reconcile_outcomes(reco, drained, run_id=run_id)

            # 0. Cancel stale entry orders from previous sessions, but preserve live protective exits.
            self._broker.cancel_open_entry_orders()

            # 1. Get account state (snapshot into ctx). Explicit guard mirrors
            # `run_intra_check` — a broker-API failure at snapshot time should
            # bail cleanly with a clear status, not propagate an exception
            # that leaves `ctx` half-populated and every downstream stage
            # guessing at state.
            try:
                account = self._broker.get_account()
                positions = self._broker.get_positions()
            except Exception as e:
                logger.error("Morning: broker snapshot failed: %s", e)
                return {
                    "status": "broker_error",
                    "orders": [],
                    "run_id": run_id,
                    "error": str(e),
                }
            cash = account["cash"]
            total_value = account["portfolio_value"]
            last_equity = account.get("last_equity", total_value)
            ctx.account = account
            ctx.positions = positions
            ctx.cash = cash
            ctx.deployable_cash = self._compute_deployable_cash(cash, positions)
            ctx.total_value = total_value
            ctx.last_equity = last_equity
            # The owner's P&L block is built from THIS read, whichever of
            # the body's return paths the run leaves by (see `_attach_pnl`).
            self._record_account_snapshot(total_value, last_equity)
            logger.info(
                "Account: $%.2f total, $%.2f cash (deployable $%.2f), %d positions (last close $%.2f)",
                total_value,
                cash,
                ctx.deployable_cash,
                len(positions),
                last_equity,
            )

            # 1a. Cash-only safety net — force-sell if margin was entered before
            # this session. Refreshes ctx.cash / positions on completion, so
            # every stage below runs on clean truth.
            forced_orders = self._force_delever(ctx)

            # 1b. Spec §11.2 — the gross-exposure MARGIN FLOOR. Deliberately
            # here, before ANY agent runs: it is computed from account state
            # alone, so a Portfolio Manager that returns nothing (a measured
            # failure mode — one candidate model truncated mid-JSON on 1 run
            # in 10) still cannot leave the desk in a liquidation-proximity
            # breach. Item 112: the morning lane scopes this to `floor_only`
            # — a genuine margin breach is de-levered NOW on the live price;
            # an ordinary §11.2 ceiling breach is de-levered after the PM has
            # run, by `_enforce_gross_ceiling_by_conviction`, so the WEAKEST-
            # by-conviction names are cut first using this session's fresh
            # per-seat read. Always populates ctx.leverage for the alert and
            # the dashboard, including distance-to-forced-liquidation.
            forced_orders = list(forced_orders) + self._enforce_gross_ceiling(
                ctx,
                floor_only=True,
            )
            positions = ctx.positions
            cash = ctx.cash
            total_value = ctx.total_value
            last_equity = ctx.last_equity
            # Local `positions` table is a derived snapshot (journal /
            # notifier / rehearsal). Morning used to never write it, so a
            # midday/close that last ran when only one name was held left
            # the table lying after later fills. Refresh from the broker
            # book we just read, before the long research window.
            self._sync_positions_from_broker(positions)

            # All broker-resident and deterministic safety work above runs
            # even while the paid-analysis circuit is latched. Only now, at
            # the boundary before research/resume-RM, may it stop the run.
            try:
                self._require_paid_analysis("morning_research")
            except PaidAnalysisSuspended as exc:
                return self._paid_suspension_after_late_safety(
                    run_id,
                    session="morning",
                    error=exc,
                    where="paid-pre-research",
                    orders=forced_orders,
                )

            # RC2 resume lane: a prior morning tick may have been killed by
            # the wrapper timeout AFTER the PM produced a plan but BEFORE the
            # RiskStage reviewed it (the observed death mode: 61/61 BUY-
            # proposal days destroyed at the PM→RM boundary during the
            # 6/30-7/15 relay outage). If today's unconsumed checkpoint
            # exists and is fresh, skip research+PM entirely — the full
            # preamble above (drains, coverage audit, force_delever, circuit
            # breaker, FRESH account snapshot) has already run, and the
            # RiskStage + execution guards below all operate on live state.
            # RM always re-runs; there is no resume-past-RM.
            from src import decision_checkpoint as _dc

            resumed = _dc.load("morning")
            if resumed is not None:
                logger.warning(
                    "RESUME LANE: unconsumed decision checkpoint from %s "
                    "(age %.0f min, %d decisions) — skipping research+PM, "
                    "re-entering at RiskStage on fresh account state",
                    resumed["run_id"],
                    resumed["age_minutes"],
                    len(resumed["portfolio_decision"].decisions),
                )
                ctx.macro_summary = resumed["macro_summary"]
                ctx.macro_analysis = resumed["macro_analysis"]
                ctx.news_intel = resumed["news_intel"]
                ctx.analyses = resumed["analyses"]
                ctx.earnings_results = resumed["earnings_results"]
                ctx.data_status = resumed["data_status"]
                ctx.admitted_symbols = set(resumed["admitted_symbols"])
                ctx.portfolio_decision = resumed["portfolio_decision"]
                portfolio_decision = ctx.portfolio_decision
                # Rehydrate bars for the plan's BUY symbols (zero-LLM, fresh
                # data). The checkpoint deliberately omits symbols_bars
                # (huge); without this the entry ATR stop floor silently
                # no-ops and the correlation advisory false-fires on resume.
                bars: dict = {}
                for d in portfolio_decision.decisions:
                    if d.action != "BUY":
                        continue
                    try:
                        bars[d.symbol] = (
                            self._market.get_ohlcv(
                                d.symbol,
                                self._config.trading.lookback_days,
                            )
                            or []
                        )
                    except Exception as e:  # noqa: BLE001
                        logger.warning("resume: bar rehydrate failed for %s: %s", d.symbol, e)
                ctx.symbols_bars = bars
            else:
                # Phase 4 #1: research stage runs the parallel fan-out (macro /
                # news / tech / earnings). Populates ctx fields.
                try:
                    self._morning_research_stage.run(ctx)
                except PaidAnalysisSuspended as exc:
                    return self._paid_suspension_after_late_safety(
                        run_id,
                        session="morning",
                        error=exc,
                        where="paid-research-suspended",
                        orders=forced_orders,
                    )
                circuit_state = self._cost_circuit_status()
                if circuit_state.get("suspended"):
                    return self._paid_suspension_after_late_safety(
                        run_id,
                        session="morning",
                        where="post-research-circuit-open",
                        orders=forced_orders,
                        error=PaidAnalysisSuspended(str(circuit_state.get("trigger_detail") or "cost circuit opened")),
                    )
                analyses = ctx.analyses

                if not analyses:
                    logger.warning("No analyses produced, skipping trading")
                    # Legit PM-less completion — record it so the evening
                    # dead-man probe doesn't read "research rows, no PM row"
                    # as a killed morning.
                    _dc.write_status("morning", "no_data")
                    return {"status": "no_data", "orders": [], "run_id": run_id}

                # docs/WORK.md item 20 — the owner's own design. Deliberately
                # sequenced HERE: after every safety path above (the two
                # late-breach emergency-liquidation checks and the paid-
                # suspension bails still run, because a refusal to DECIDE must
                # never become a refusal to PROTECT), and before the Portfolio
                # Manager call, which is the expensive one this exists to not
                # spend on absent evidence.
                self._heal_lost_research_seats(ctx)
                gate_skip = self._evidence_gate_skip(ctx, run_id)
                if gate_skip is not None:
                    return gate_skip

                # Phase 4 #1: decision stage — memory layers + PM + Constructor.
                try:
                    self._decision_stage(ctx)
                except PaidAnalysisSuspended as exc:
                    return self._paid_suspension_after_late_safety(
                        run_id,
                        session="morning",
                        error=exc,
                        where="paid-decision-suspended",
                        orders=forced_orders,
                    )
                portfolio_decision = ctx.portfolio_decision

                # Persist the plan the moment it exists — a kill anywhere
                # between here and execution leaves a resumable checkpoint
                # instead of a wasted research+PM spend.
                _dc.write(ctx)

            if not portfolio_decision:
                failure_status = ctx.analysis_failure_status or "pm_agent_failure"
                failure_error = ctx.analysis_failure_error or "no valid PM decision"
                logger.error(
                    "Portfolio manager produced no valid decision (%s): %s",
                    failure_status,
                    failure_error,
                )
                return {
                    # Terminal for this slot. main.py must not repeat the full
                    # paid stack on deterministic parse/schema/grounding faults.
                    "status": failure_status,
                    "orders": [],
                    "run_id": run_id,
                    "error": failure_error,
                    "data_status": dict(ctx.data_status),
                    # Spec §11.2 — gross exposure, its ladder-resolved ceiling and
                    # the distance to forced liquidation, for the operator alert.
                    "leverage": dict(ctx.leverage),
                    "stop_coverage_gaps": coverage_gaps,
                }
            if not portfolio_decision.decisions:
                logger.info("Portfolio manager + Constructor: no trades suggested")
                return {
                    "status": "no_trades",
                    "orders": [],
                    "run_id": run_id,
                    "data_status": dict(ctx.data_status),
                    # Spec §11.2 — gross exposure, its ladder-resolved ceiling and
                    # the distance to forced liquidation, for the operator alert.
                    "leverage": dict(ctx.leverage),
                    "stop_coverage_gaps": coverage_gaps,
                }

            # Phase 4 #1: risk stage — hard filter + earnings cap + RM review + mods.
            try:
                early_exit = self._risk_stage(ctx)
            except PaidAnalysisSuspended as exc:
                return self._paid_suspension_after_late_safety(
                    run_id,
                    session="morning",
                    error=exc,
                    where="paid-risk-suspended",
                    orders=forced_orders,
                )
            # The plan has now been risk-reviewed — whatever the outcome, it
            # must never be re-offered by the resume lane (an RM-rejected
            # plan retried next tick would be a veto bypass), and marking
            # BEFORE execution makes the execution at-most-once (a kill
            # mid-execution is owned by the BUY write-ahead orphan sweep,
            # not by re-running the plan).
            _dc.mark_consumed("morning")
            if early_exit is not None:
                early_exit["run_id"] = run_id
                early_exit["data_status"] = dict(ctx.data_status)
                stop_updates = getattr(
                    getattr(self, "broker", None),
                    "stop_trade_updates",
                    None,
                )
                if callable(stop_updates):
                    try:
                        stop_updates()
                    except Exception:
                        pass
                return early_exit

            # Item 112 — the ordinary §11.2 gross-ceiling de-lever, run HERE
            # (morning only) so it cuts the WEAKEST-by-conviction names first
            # using THIS session's fresh per-seat read, not the stale-stance
            # biggest-loser cut the preamble would have used. After the risk
            # stage (so an RM-driven early exit is honoured first) and before
            # execution (so its SELLs land with the session's other orders).
            # A no-op on any book already under its ceiling — the ordinary
            # case — and re-measures gross first, so if the preamble margin
            # floor already fired it only trims a residual breach.
            conviction_delever = self._enforce_gross_ceiling_by_conviction(ctx)

            # Phase 4 #1: execution stage — HOLDs logged, SELLs then BUYs submitted.
            orders = self._execution_stage(ctx)
            if conviction_delever:
                orders = list(conviction_delever) + list(orders)

            # Truthful terminal status. 2026-08-19: three risk-approved BUYs
            # were skipped as unfunded (the funding sell filled 36s after the
            # session gave up), yet the run reported status='executed' with
            # orders=[] — the day read as done and nothing retried while the
            # freed cash sat idle until midday re-parked it. When the session
            # had approved BUYs, submitted NOTHING, and at least one skip was
            # the transient funding race, report `buys_unfunded` truthfully.
            # It is terminal for this slot: automatically re-running the full
            # paid research -> PM -> RM stack amplified cost for an execution-
            # timing issue. A future execution-only checkpoint can retry this
            # without buying another decision chain.
            approved_buys = [d for d in (portfolio_decision.decisions or []) if d.action == "BUY"]
            unfunded = [s for s in ctx.execution_skips if s.get("reason") == "insufficient_cash"]
            # Sweep bookkeeping orders are not "the session traded" — only
            # real BUY/SELL submissions count against the retry decision.
            real_orders = [
                o for o in orders if not (isinstance(o, dict) and str(o.get("action", "")).startswith("SWEEP_"))
            ]
            if approved_buys and unfunded and not real_orders:
                logger.warning(
                    "=== Morning run: %d approved BUY(s), 0 submitted, "
                    "%d unfunded skip(s) — reporting terminal "
                    "buys_unfunded ===",
                    len(approved_buys),
                    len(unfunded),
                )
                return {
                    "status": "buys_unfunded",
                    "orders": orders,
                    "run_id": run_id,
                    "data_status": dict(ctx.data_status),
                    # Spec §11.2 — gross exposure, its ladder-resolved ceiling and
                    # the distance to forced liquidation, for the operator alert.
                    "leverage": dict(ctx.leverage),
                    "stop_coverage_gaps": coverage_gaps,
                    "execution_skips": list(ctx.execution_skips),
                }
            if not real_orders:
                logger.info(
                    "=== Morning run complete: no equity order submitted (not marking executed) ===",
                )
                return {
                    "status": "no_orders",
                    "orders": orders,
                    "run_id": run_id,
                    "data_status": dict(ctx.data_status),
                    # Spec §11.2 — gross exposure, its ladder-resolved ceiling and
                    # the distance to forced liquidation, for the operator alert.
                    "leverage": dict(ctx.leverage),
                    "stop_coverage_gaps": coverage_gaps,
                    "execution_skips": list(ctx.execution_skips),
                }
            logger.info("=== Morning run complete: %d orders executed ===", len(orders))
            return {
                "status": "executed",
                "orders": orders,
                "run_id": run_id,
                "data_status": dict(ctx.data_status),
                # Spec §11.2 — gross exposure, its ladder-resolved ceiling and
                # the distance to forced liquidation, for the operator alert.
                "leverage": dict(ctx.leverage),
                "stop_coverage_gaps": coverage_gaps,
                "execution_skips": list(ctx.execution_skips),
            }
        except SessionTerminated:
            # A kill between the buys and their stops leaves a filled buy
            # naked; repairing coverage comes before every settle step below.
            self._repair_stops_on_kill("morning")
            raise
        finally:
            # Item 112 — pay the deferred ordinary §11.2 ceiling. The preamble
            # scoped itself to the margin floor so the cut could be ordered by
            # THIS session's fresh conviction read; if the run never reached
            # that pass (any PM-less early return, the resume lane, or an
            # exception), the ordinary ceiling is enforced here instead, with
            # the unchanged biggest-loser ordering. One place, so a lane added
            # later cannot silently lose the ceiling. No-op once discharged.
            self._discharge_deferred_gross_ceiling(ctx)
            # Phase 3: ask broker which of today's submitted orders actually filled.
            # Unfilled ones get flagged so PM memory / calibration skip them.
            self._reconcile_fills(ctx)
            # Fills (or stop-outs since snapshot) change the book. Re-read
            # the broker; do not reuse the pre-execution list.
            self._sync_positions_from_broker()
            self._restore_sigterm(_prior_sigterm)
