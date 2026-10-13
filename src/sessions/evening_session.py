"""Evening report session (moved verbatim from TradingPipeline)."""

from __future__ import annotations

import logging
from src.sessions.evening_record import record_evening_pass, run_evening_housekeeping
from src.data.macro import MacroCoverage
from src.cost_circuit import PaidAnalysisSuspended
from src.pipeline_context import RunContext
from src.agents.base import AgentResult, agent_log_kwargs, seat_acceptance_kwargs
from src.trading_calendar import session_date_key
import math

logger = logging.getLogger(__name__)


class EveningSession:
    """Evening report session (moved verbatim from TradingPipeline)."""

    # Declared beside the audit that writes `stop_coverage_gaps`; read by
    # src/sessions/stop_audit.py and enforced by tests/test_protection_undetermined.py.
    AUDITS_BROKER_STOPS = True

    def __init__(
        self,
        *,
        activate_cost_session,
        actualize_trade_row,
        build_active_state_changes,
        build_missed_opportunities_digest,
        build_portfolio_heat,
        build_recent_buys_for_grading,
        build_recent_outlook_calibration,
        build_recent_sells_for_grading,
        build_thesis_health_context,
        build_weekly_narrative,
        drain_pending_protection_restores,
        drain_pending_repegs,
        evening_earnings_proximity,
        evening_stop_proximity,
        expected_sessions_missing_today,
        is_trading_day,
        load_earnings_analyses,
        maybe_run_quarterly_meta,
        news_held_symbols,
        paid_suspended_payload,
        persist_evening_replay_inputs,
        reconcile_fills,
        reconcile_orphan_pending_submits,
        reconcile_stop_coverage,
        reconcile_stop_out_fills,
        require_paid_analysis,
        run_news_update,
        surface_reconcile_outcomes,
        sweeper,
        sync_positions_from_broker,
        total_pnl_since_reset,
        broker,
        config,
        db,
        earnings_provider,
        evening_analyst,
        macro,
        news_store,
    ) -> None:
        self._activate_cost_session = activate_cost_session
        self._actualize_trade_row = actualize_trade_row
        self._build_active_state_changes = build_active_state_changes
        self._build_missed_opportunities_digest = build_missed_opportunities_digest
        self._build_portfolio_heat = build_portfolio_heat
        self._build_recent_buys_for_grading = build_recent_buys_for_grading
        self._build_recent_outlook_calibration = build_recent_outlook_calibration
        self._build_recent_sells_for_grading = build_recent_sells_for_grading
        self._build_thesis_health_context = build_thesis_health_context
        self._build_weekly_narrative = build_weekly_narrative
        self._drain_pending_protection_restores = drain_pending_protection_restores
        self._drain_pending_repegs = drain_pending_repegs
        self._evening_earnings_proximity = evening_earnings_proximity
        self._evening_stop_proximity = evening_stop_proximity
        self._expected_sessions_missing_today = expected_sessions_missing_today
        self._is_trading_day = is_trading_day
        self._load_earnings_analyses = load_earnings_analyses
        self._maybe_run_quarterly_meta = maybe_run_quarterly_meta
        self._news_held_symbols = news_held_symbols
        self._paid_suspended_payload = paid_suspended_payload
        self._persist_evening_replay_inputs = persist_evening_replay_inputs
        self._reconcile_fills = reconcile_fills
        self._reconcile_orphan_pending_submits = reconcile_orphan_pending_submits
        self._reconcile_stop_coverage = reconcile_stop_coverage
        self._reconcile_stop_out_fills = reconcile_stop_out_fills
        self._require_paid_analysis = require_paid_analysis
        self._run_news_update = run_news_update
        self._surface_reconcile_outcomes = surface_reconcile_outcomes
        self._sweeper = sweeper
        self._sync_positions_from_broker = sync_positions_from_broker
        self._total_pnl_since_reset = total_pnl_since_reset
        self._broker = broker
        self._config = config
        self._db = db
        self._earnings_provider = earnings_provider
        self._evening_analyst = evening_analyst
        self._macro = macro
        self._news_store = news_store

    def run(self) -> dict:
        ctx = RunContext.start("evening")
        run_id = ctx.run_id
        logger.info("=== Evening report: %s ===", run_id)

        if not self._is_trading_day():
            logger.info("Evening run skipped: market closed for non-trading day")
            return {"status": "market_holiday", "analysis": None, "run_id": run_id}

        self._activate_cost_session(run_id, "evening")

        # Drain orphaned protection-restore intents — last chance before
        # the trading day ends. If close-session bailed and the SELL has
        # since gone terminal, recover coverage now rather than carrying
        # a naked position overnight. Codex r8 #2.
        drained = self._drain_pending_protection_restores()
        self._drain_pending_repegs()
        self._reconcile_orphan_pending_submits()  # audit F4
        # Broker-truth coverage audit — last check before carrying positions
        # overnight (independent of the WAL).
        coverage_gaps = self._reconcile_stop_coverage()
        # Sweep submitted orders so canceled/expired orders do not get
        # narrated as real trades, and partial terminal fills are reflected
        # in the trade list before the evening prompt is built.
        #
        # Item 173(2): this runs BEFORE the stop-out reconcile below, not
        # after. A SELL this session submitted but hasn't yet reconciled
        # leaves the ledger believing the position is still open
        # (get_symbols_with_open_ledger_qty ignores 'submitted' rows) while
        # the broker has already reduced it — a positive gap the stop-out
        # reconciler can't explain, because the submitted SELL's
        # broker_order_id is already in get_known_broker_order_ids so its
        # fill is filtered out of new_fills, and it pages a false CRITICAL
        # "records disagree with broker". Reconciling fills first flips that
        # SELL to executed, the gap closes, and the stop-out check stays quiet.
        self._reconcile_fills()
        # Broker-truth EXIT audit (2026-08-28 ONDS/CCJ) — last chance before
        # the daily P&L snapshot below is computed, so a same-day stop-out
        # is reflected in tonight's report rather than showing up as an
        # unexplained gap the next time someone looks at realized_pnl.
        reco = None
        try:
            reco = self._reconcile_stop_out_fills(run_id)
        except Exception as exc:  # noqa: BLE001
            record_evening_pass(self, "stop_out_reconcile", exc)
        else:
            record_evening_pass(self, "stop_out_reconcile")
        # Item 101: surface a broker-made stop-out / re-protection to owner.
        self._surface_reconcile_outcomes(reco, drained, run_id=run_id)

        # 1. Record daily PnL — use Alpaca's last_equity (previous trading-day close)
        # as the baseline. This correctly handles weekends/holidays (Alpaca updates
        # last_equity only on trading days) and doesn't depend on whether yesterday's
        # evening run actually persisted a snapshot to our own DB.
        account = self._broker.get_account()
        positions = self._broker.get_positions()
        total_value = account["portfolio_value"]
        last_equity = account.get("last_equity", total_value)
        today_str = session_date_key()  # ET trading-day key — stable across host TZ

        if last_equity > 0:
            daily_pnl = total_value - last_equity
            daily_return_pct = daily_pnl / last_equity * 100
        else:
            daily_pnl = 0.0
            daily_return_pct = 0.0
        ctx.account = account
        ctx.positions = positions
        ctx.total_value = total_value
        ctx.last_equity = last_equity
        ctx.daily_pnl = daily_pnl
        # Sync the full broker book (before the LLM-view split below).
        # Evening's Telegram snapshot reads this table, not the in-memory list.
        self._sync_positions_from_broker(ctx.positions)

        # LLM view: hide the cash-sweep vehicle from evening's position
        # narratives (facts / thesis-health / missed-ops held-set) — parked
        # T-bills have no thesis to review. ctx keeps broker truth.
        sweeper = self._sweeper()
        if sweeper is not None:
            positions, _parked = sweeper.split_positions(positions)

        # Phase 6 (§6.3b): today's P&L expressed against capital actually AT
        # RISK, not just total equity — reuses the same audit §1.3 heat
        # calculation (`_build_portfolio_heat` -> `src.risk.metrics.
        # portfolio_heat`) the risk-manager prompt already trusts, rather
        # than recomputing it. None (not 0.0) on a failed build, so the
        # notifier can say "unknown" instead of a fabricated number.
        try:
            risk_heat = self._build_portfolio_heat(positions, total_value)
            risk_capital_dollars = risk_heat.budget_risk_dollars if risk_heat is not None else None
        except Exception as e:  # noqa: BLE001
            record_evening_pass(self, "risk_heat", e)
            risk_capital_dollars = None
        else:
            record_evening_pass(self, "risk_heat")

        # Phase 4 #5: daily_pnl write is deferred to the atomic
        # save_evening_snapshot() below, along with insights. Doing both in
        # one transaction means a crash between them doesn't leave next
        # morning reading a P&L number with no insights narrative attached.
        # Fallback: if the evening LLM fails (analysis is None), we still
        # save the daily_pnl alone below to preserve the P&L audit trail.

        # This boundary is intentionally after broker protection/fill
        # reconciliation and the deterministic P&L snapshot, but before the
        # first paid news/model request. A latched breaker still persists the
        # P&L audit row and returns a truthful suspended status.
        try:
            self._require_paid_analysis("news_analyst")
        except PaidAnalysisSuspended as exc:
            equity_close = None
            try:
                closes = self._broker.get_recent_daily_closes(lookback_days=10)
                if closes and closes[-1][0] == today_str:
                    equity_close = closes[-1][1]
            except Exception as close_exc:  # noqa: BLE001
                record_evening_pass(self, "suspended_close_fetch", close_exc)
            else:
                record_evening_pass(self, "suspended_close_fetch")
            self._db.insert_daily_pnl(
                date=today_str,
                total_value=total_value,
                daily_pnl=daily_pnl,
                daily_return_pct=daily_return_pct,
                equity_close=equity_close,
            )
            payload = self._paid_suspended_payload(run_id, error=exc)
            payload.update(
                analysis=None,
                total_value=total_value,
                daily_pnl=daily_pnl,
                daily_return_pct=daily_return_pct,
                equity_close=equity_close,
                stop_coverage_gaps=coverage_gaps,
            )
            return payload

        # 2. News + Earnings update — capture end-of-day developments
        try:
            # Same held-symbols-only scope as run_position_review — see the
            # comment there. No separate candidate list exists pre-fetch in
            # this path. `positions` here was already sweeper-split above,
            # so _news_held_symbols' own split is a no-op; called anyway to
            # keep this call site identical to the other two.
            evening_news, evening_news_coverage = self._run_news_update(
                run_id,
                session="evening",
                held_symbols=self._news_held_symbols(positions),
            )
        except PaidAnalysisSuspended as exc:
            self._db.insert_daily_pnl(
                date=today_str,
                total_value=total_value,
                daily_pnl=daily_pnl,
                daily_return_pct=daily_return_pct,
            )
            payload = self._paid_suspended_payload(run_id, error=exc)
            payload.update(
                analysis=None,
                total_value=total_value,
                daily_pnl=daily_pnl,
                daily_return_pct=daily_return_pct,
                stop_coverage_gaps=coverage_gaps,
            )
            return payload
        if evening_news_coverage is not None and evening_news_coverage.status != "ok":
            # Same gap noted in run_position_review: evening has no
            # data_status mechanism of its own to carry this further, so at
            # minimum it does not disappear into a log-only "ok".
            logger.warning("evening: %s", evening_news_coverage.describe())
        if evening_news:
            logger.info("Evening news: %s", evening_news.pm_briefing[:200])
        try:
            _, evening_earnings = self._load_earnings_analyses(run_id, session="evening", ctx=ctx)
        except Exception as e:  # noqa: BLE001 — evening proceeds without earnings
            record_evening_pass(self, "earnings_load", e)
            evening_earnings = []
        else:
            record_evening_pass(self, "earnings_load")

        try:
            self._require_paid_analysis("evening_analyst")
        except PaidAnalysisSuspended as exc:
            self._db.insert_daily_pnl(
                date=today_str,
                total_value=total_value,
                daily_pnl=daily_pnl,
                daily_return_pct=daily_return_pct,
            )
            payload = self._paid_suspended_payload(run_id, error=exc)
            payload.update(
                analysis=None,
                total_value=total_value,
                daily_pnl=daily_pnl,
                daily_return_pct=daily_return_pct,
                stop_coverage_gaps=coverage_gaps,
            )
            return payload

        # 3. LLM evening analysis — daily review and tomorrow outlook
        macro_summary = self._macro.get_macro_summary()
        evening_macro_coverage = self._macro.last_coverage
        if isinstance(evening_macro_coverage, MacroCoverage) and evening_macro_coverage.status != "ok":
            # Same gap noted in run_position_review / the news coverage
            # check above: evening has no data_status mechanism of its own
            # to carry this further, so at minimum it does not disappear
            # into a log-only "ok".
            logger.warning("evening: %s", evening_macro_coverage.describe())
        # Sweep churn (SWEEP_BUY/SWEEP_SELL) is cash parking, not a trading
        # decision — narrating it to the evening analyst would feed the
        # learning loops noise (review finding). Fetch extra rows so the
        # filter doesn't shrink the real-trade view.
        today_trades = [
            self._actualize_trade_row(t)
            for t in self._db.get_trades(limit=30, today_only=True, executed_only=True)
            if (t.get("action") or "") not in ("SWEEP_BUY", "SWEEP_SELL")
        ][:20]
        # Feed yesterday's insights back so evening can grade its own prior outlook
        # against today's reality — enables calibration over time.
        prior_outlook = self._db.get_latest_insights(before_date=today_str)
        # SELL decisions from the last 2 days + each symbol's move since sell.
        # Evening grades each one {correct|premature|wrong} — the feedback loop
        # on selling discipline.
        recent_sells = self._build_recent_sells_for_grading(
            lookback_days=2,
            symbols_bars=ctx.symbols_bars,  # empty for evening (no tech fetch) — OK, we use broker price
        )
        # v2: mirror SELL grading with BUY grading. Entry quality feedback loop.
        recent_buys = self._build_recent_buys_for_grading(
            lookback_days=5,
            symbols_bars=ctx.symbols_bars,
        )
        # v2: meta-calibration — evening sees its own recent tomorrow_bias vs
        # actual outcomes so it can detect "I've been too bullish 7/10 days".
        outlook_calibration = self._build_recent_outlook_calibration(lookback=10)
        # v2: share the PM's 7-day narrative + 14-day active state-change
        # memory so evening doesn't drift from or repeat its own previous
        # language unchecked.
        weekly_narrative = self._build_weekly_narrative()
        active_state_changes = self._build_active_state_changes()

        # Phase-1 evening-upgrade: deterministic "what did we miss" digest.
        # Python pre-computes the signal-state context so the LLM's classification
        # has to cite observable evidence rather than retro-rationalize price.
        held_set = {p.symbol for p in positions}
        try:
            missed_ops_snapshots = self._build_missed_opportunities_digest(
                lookback_days=5,
                move_threshold_pct=8.0,
                top_n=15,
                current_position_symbols=held_set,
            )
        except Exception as e:
            record_evening_pass(self, "missed_ops_digest", e)
            missed_ops_snapshots = []
        else:
            record_evening_pass(self, "missed_ops_digest")

        # Value-lens upgrade (2026-04): per-position 8-week fundamentals
        # evolution — feeds the new thesis_health_review reasoning step.
        try:
            thesis_health_context = self._build_thesis_health_context(positions)
        except Exception as e:
            record_evening_pass(self, "thesis_health_context", e)
            thesis_health_context = {}
        else:
            record_evening_pass(self, "thesis_health_context")

        # Replay/shadow mechanism (2026-04 — P2 follow-up): persist the
        # full evening-analyst input set so a candidate prompt can be
        # re-scored on the same frozen inputs later via
        # `scripts/replay_evening.py`. Doesn't affect the live run;
        # failure here is non-fatal and only logged.
        try:
            self._persist_evening_replay_inputs(
                date_iso=today_str,
                run_id=run_id,
                positions=positions,
                macro_summary=macro_summary,
                total_value=total_value,
                daily_pnl=daily_pnl,
                daily_return_pct=daily_return_pct,
                today_trades=today_trades,
                prior_outlook=prior_outlook,
                recent_sells=recent_sells,
                recent_buys=recent_buys,
                news_intel=evening_news,
                earnings_analyses=evening_earnings,
                weekly_narrative=weekly_narrative,
                active_state_changes=active_state_changes,
                outlook_calibration=outlook_calibration,
                missed_ops_snapshots=missed_ops_snapshots,
                thesis_health_context=thesis_health_context,
            )
        except Exception as e:
            record_evening_pass(self, "replay_input_persist", e)
        else:
            record_evening_pass(self, "replay_input_persist")

        analysis = None
        analysis_error = False
        try:
            analysis, ev_result = self._evening_analyst.analyze(
                positions=positions,
                macro_summary=macro_summary,
                total_value=total_value,
                daily_pnl=daily_pnl,
                daily_return_pct=daily_return_pct,
                today_trades=today_trades,
                prior_outlook=prior_outlook,
                recent_sells=recent_sells,
                recent_buys=recent_buys,
                news_intel=evening_news,
                earnings_analyses=evening_earnings,
                weekly_narrative=weekly_narrative,
                active_state_changes=active_state_changes,
                outlook_calibration=outlook_calibration,
                missed_ops_snapshots=missed_ops_snapshots,
                thesis_health_context=thesis_health_context,
            )
        except PaidAnalysisSuspended as exc:
            self._db.insert_daily_pnl(
                date=today_str,
                total_value=total_value,
                daily_pnl=daily_pnl,
                daily_return_pct=daily_return_pct,
            )
            payload = self._paid_suspended_payload(run_id, error=exc)
            payload.update(
                analysis=None,
                total_value=total_value,
                daily_pnl=daily_pnl,
                daily_return_pct=daily_return_pct,
                stop_coverage_gaps=coverage_gaps,
            )
            return payload
        except Exception as e:
            from src.agents.base import AgentResult, resolve_provider

            analysis_error = True
            logger.error("Evening analyst failed: %s", e, exc_info=True)
            # No call ever completed, so `actual_provider`/model stay unknown
            # (not fabricated) — but WHAT was requested is known regardless
            # of the exception, so record that much for attribution.
            _requested_model = self._config.llm.evening_analyst_model
            _requested_provider = resolve_provider(
                _requested_model,
                self._config.llm.evening_analyst_provider,
            )
            ev_result = AgentResult(
                raw_text=f"[exception] {e}",
                tokens_used=0,
                model=self._config.llm.evening_analyst_model,
                user_message="",
                requested_model=_requested_model,
                requested_provider=_requested_provider,
                provider_requests=0,
            )

        _ev_log_kwargs = agent_log_kwargs(ev_result)
        if analysis_error:
            # agent_log_kwargs() derives "fallback"/"success" from
            # used_fallback, which is False here (no call ever completed) —
            # override so a hard failure isn't misreported as a success.
            _ev_log_kwargs["status"] = "failed"
        elif analysis is None:
            _ev_log_kwargs["status"] = "evening_parse_error"
        self._db.insert_agent_log(
            **seat_acceptance_kwargs(
                _ev_log_kwargs.get("status")
                if _ev_log_kwargs.get("status") in ("failed", "evening_parse_error")
                else None
            ),
            agent_name="evening_analyst",
            run_id=run_id,
            input_summary=f"${total_value:.0f} total, PnL ${daily_pnl:.2f}",
            input_message=ev_result.user_message,
            output_summary=(
                analysis.daily_summary if analysis else ("analysis_error" if analysis_error else "parse_error")
            ),
            full_response=ev_result.raw_text,
            model=ev_result.model,
            tokens_used=ev_result.tokens_used,
            input_tokens=ev_result.input_tokens,
            output_tokens=ev_result.output_tokens,
            cost_usd=ev_result.cost_usd,
            **_ev_log_kwargs,
        )

        # True close-to-close ("4pm-to-4pm") P&L. account.last_equity is the
        # PRIOR day's close (stale at the 20:00 ET evening run), and
        # total_value here is the 8pm after-hours value — neither gives today's
        # official 4pm close. Alpaca portfolio_history (extended_hours=False)
        # does: its latest 1D point is today's regular-session close. We report
        # the clean close-to-close P&L when available and store today's close
        # for the audit trail; on any gap we fall back to the real-time diff.
        equity_close = None
        pnl_4pm = None
        pnl_4pm_pct = None
        try:
            closes = self._broker.get_recent_daily_closes(lookback_days=10)
            if closes and closes[-1][0] == today_str:
                equity_close = closes[-1][1]
                prev_close = closes[-2][1] if len(closes) >= 2 else None
                # Guard > 0: a negative prior close (corrupted data / underwater
                # account) would flip the sign of the return %; leave pnl_4pm
                # None so the headline falls back to the real-time path.
                if prev_close and prev_close > 0:
                    pnl_4pm = equity_close - prev_close
                    pnl_4pm_pct = pnl_4pm / prev_close * 100
            elif closes:
                logger.info(
                    "4pm snapshot: portfolio_history latest date %s != today %s "
                    "(API lag?) — evening uses the real-time P&L fallback",
                    closes[-1][0],
                    today_str,
                )
            # Self-heal: when portfolio_history is a day behind at the
            # 20:00 ET evening run (the "API lag?" branch above), that
            # evening's equity_close landed NULL — but by a LATER evening
            # the API has caught up on those dates, which are still inside
            # this lookback window. Backfill any still-NULL rows now.
            # today_str is excluded because today's row is owned by the
            # branches above + save_evening_snapshot below: when today's
            # bar is present the first branch already uses it as the
            # official close, and when it's absent there is nothing to
            # backfill yet.
            for d, close_val in closes:
                if d == today_str:
                    continue
                # Mirror the `prev_close > 0` guard above: Alpaca
                # portfolio_history can emit 0.0 (pre-funding / account
                # reset) or non-finite points, and a backfilled value is
                # permanent (the fill targets NULL-only rows, so a bad
                # write can never be corrected by a later run) — never
                # freeze a corrupt equity in. NaN must be caught here
                # anyway: sqlite binds it as NULL, which would make
                # backfill report success while storing nothing.
                if not (math.isfinite(close_val) and close_val > 0):
                    logger.warning(
                        "equity_close backfill skipped for %s: suspect equity value %r",
                        d,
                        close_val,
                    )
                    continue
                try:
                    if self._db.backfill_equity_close(d, close_val):
                        logger.info(
                            "equity_close backfilled for %s = %.2f (API lag self-heal)",
                            d,
                            close_val,
                        )
                except Exception as exc:
                    record_evening_pass(self, "equity_close_backfill", exc)
                else:
                    record_evening_pass(self, "equity_close_backfill")
        except Exception as e:
            record_evening_pass(self, "snapshot_fetch_4pm", e)
        else:
            record_evening_pass(self, "snapshot_fetch_4pm")

        # Save daily_pnl + insights atomically (Phase 4 #5). If the LLM
        # failed (analysis is None), still record the P&L number so the
        # audit trail is complete — just with empty insights fields.
        if analysis:
            self._db.save_evening_snapshot(
                date=today_str,
                total_value=total_value,
                daily_pnl=daily_pnl,
                daily_return_pct=daily_return_pct,
                equity_close=equity_close,
                tomorrow_outlook=analysis.tomorrow_outlook,
                lessons=analysis.lessons,
                suggested_actions=analysis.suggested_actions,
                risk_rating=analysis.risk_rating,
                tomorrow_bias=analysis.tomorrow_bias,
                tomorrow_conviction=analysis.tomorrow_conviction,
                tomorrow_key_risks=analysis.tomorrow_key_risks,
                sell_decisions_assessment=analysis.sell_decisions_assessment,
                # v2: persist structured grades so next-day position_reviewer
                # can aggregate counts into its "lean patient" bias.
                sell_grades=analysis.sell_grades,
                buy_grades=analysis.buy_grades,
                # Phase-1 upgrade: per-day missed opportunities feed PM's L3d
                # memory next morning and the quarterly meta-reflector's
                # theme_coverage_report.
                missed_opportunities=analysis.missed_opportunities,
                # Defect (d) fix: these four were produced by the LLM every
                # night and declared on EveningReport, but had no parameter
                # here — dropped before ever reaching disk.
                # thesis_updates/selection_rules/discipline_notes feed
                # tomorrow's portfolio_manager (see build_user_message).
                thesis_updates=analysis.thesis_updates,
                selection_rules=analysis.selection_rules,
                discipline_notes=analysis.discipline_notes,
                previous_outlook_assessment=analysis.previous_outlook_assessment,
            )
        else:
            # LLM failed — keep at least the P&L number for daily audit.
            self._db.insert_daily_pnl(
                date=today_str,
                total_value=total_value,
                daily_pnl=daily_pnl,
                daily_return_pct=daily_return_pct,
                equity_close=equity_close,
            )

        # Conviction ledger (spec §9.5) — score on close. Every position
        # chain that went flat today is credited to the seats that took a
        # side on it: aligned with the direction taken scores +R, opposed
        # scores -R, weighted by the conviction that seat declared. Runs
        # HERE, in evening housekeeping, deliberately: it reads closed
        # `trades` rows and writes forensic evidence rows, touches no broker
        # and no open position, and is idempotent (a position already scored
        # is skipped), so it can never influence or delay an execution path.
        # Advisory only — nothing in the trading chain reads what it writes.
        try:
            ledger = self._db.conviction.resolve_conviction_ledger()
            if ledger.get("scored_positions"):
                logger.info(
                    "Conviction ledger: scored %d newly closed position(s) into "
                    "%d seat credit(s) (%d already scored, %d unscorable without "
                    "an entry stop, %d with no recorded stances)",
                    ledger["scored_positions"],
                    ledger["credits_written"],
                    ledger["skipped_already_scored"],
                    ledger["skipped_no_r"],
                    ledger["skipped_no_stances"],
                )
        except Exception as e:
            record_evening_pass(self, "conviction_ledger", e)
        else:
            record_evening_pass(self, "conviction_ledger")

        run_evening_housekeeping(self)

        logger.info(
            "Evening: value=$%.2f, PnL=$%.2f (%.2f%%), risk=%s",
            total_value,
            daily_pnl,
            daily_return_pct,
            analysis.risk_rating if analysis else "error",
        )
        if analysis:
            logger.info("Summary: %s", analysis.daily_summary)
            logger.info("Tomorrow: %s", analysis.tomorrow_outlook)
        # Evening is the last chance to reconcile today's orders before the
        # next trading day. Sweep everything still marked submitted.
        self._reconcile_fills()
        self._sync_positions_from_broker()

        meta_result = self._maybe_run_quarterly_meta()
        missing_sessions = self._expected_sessions_missing_today()
        # Owner-facing evening report (2026-09-18): today's P&L alone never
        # answered "am I up since the desk restarted". The same
        # `_total_pnl_since_reset` the trader-feed messages already use is
        # read here so the evening message can lead with BOTH figures on the
        # identical basis, rather than computing a second "total" of its own.
        total_pnl, total_return_pct, total_pnl_since = self._total_pnl_since_reset(total_value)
        if missing_sessions:
            logger.warning(
                "Dead-man's check: expected session(s) left no agent_logs today: %s",
                ", ".join(missing_sessions),
            )
        return {
            "status": (
                "analyzed"
                if analysis is not None
                else ("evening_analysis_error" if analysis_error else "evening_parse_error")
            ),
            "total_value": total_value,
            "daily_pnl": daily_pnl,
            "daily_return_pct": daily_return_pct,
            "analysis": analysis.model_dump() if analysis else None,
            "run_id": run_id,
            "auto_meta": meta_result,
            # Observability: surface a silently-missing session so the
            # notifier can raise deterministic escalation (not just LLM).
            "missing_sessions": missing_sessions,
            "stop_coverage_gaps": coverage_gaps,
            # True 4pm-to-4pm headline P&L (None → notifier falls back to the
            # real-time total_value/daily_pnl figures).
            "equity_close": equity_close,
            "pnl_4pm": pnl_4pm,
            "pnl_4pm_pct": pnl_4pm_pct,
            # Phase 6 (§6.3b) — capital actually at risk (sum of
            # (entry-stop) x shares across open positions), for the
            # notifier's "P&L vs risk capital" line. None on a failed heat
            # build; 0.0 for a genuinely flat/fully-released book — the
            # notifier tells those two apart.
            "risk_capital_dollars": risk_capital_dollars,
            # Dated total P&L (see `_total_pnl_since_reset` for why it is
            # dated rather than called "since inception").
            "total_pnl": total_pnl,
            "total_return_pct": total_return_pct,
            "total_pnl_since": total_pnl_since,
            # Two end-of-day facts the desk knew and never told the owner:
            # which holdings sit within one ordinary day's move of their stop,
            # and which report earnings imminently. Both fail soft to [].
            "stop_proximity": self._evening_stop_proximity(positions),
            "earnings_proximity": self._evening_earnings_proximity(positions),
        }
