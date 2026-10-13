"""src.intraday.session -- the FREE intra-check session: the check itself, its body and its report.

Bodies moved verbatim from src/pipeline_intraday.py (`IntradayMixin`), which keeps
same-named thin shims built per call. Every collaborator is an explicit keyword-only
constructor argument, so this builds and runs with no pipeline behind it. Host
attributes a body both reads and ASSIGNS go through `state` (a get/set view the shim
hands in), never a construction-time copy.
"""

import logging

from src.cost_circuit import PaidAnalysisSuspended
from src.evidence_gate import EvidenceGateEvaluationError
from src.intraday_scan_outcome import failed_scan_result
from src.pipeline_context import RunContext
from src.sessions.termination import SessionTerminated
from src.intraday.session_gate import session_has_ended
from src.intraday.tick_trail import trail_on_tick
from src.intraday.trend_exit_tick import wire_trend_exit
from src.sentinel.guarded_site import record_site as _site
from src.trading_calendar import session_date_key

#: The moved code logged under `src.pipeline` before the move and still does;
#: binding the name rather than `__name__` keeps log records byte-identical.
logger = logging.getLogger("src.pipeline")


class IntradaySession:
    """The FREE intra-check session: the check itself, its body and its report. Standalone, built from
    explicit collaborators.
    """

    # Declared beside the audit that writes `stop_coverage_gaps`; read by
    # src/sessions/stop_audit.py and enforced by tests/test_protection_undetermined.py.
    AUDITS_BROKER_STOPS = True

    def __init__(
        self,
        *,
        db=None,
        broker=None,
        is_trading_day=None,
        kill_switch_halt_result=None,
        run_intra_safety_preamble=None,
        attach_evidence_freshness=None,
        attach_pnl=None,
        activate_cost_session=None,
        compute_deployable_cash=None,
        record_account_snapshot=None,
        run_intraday_opportunity_scan=None,
        sync_positions_from_broker=None,
        total_pnl_since_reset=None,
        persist_intra_check_report=None,
        run_intra_check_body=None,
        state=None,
        now=None,
        trail_collaborators=None,
        install_sigterm_unwind=None,
        repair_stops_on_kill=None,
        restore_sigterm=None,
    ) -> None:
        self.db = db
        self.broker = broker
        self._is_trading_day = is_trading_day
        self._kill_switch_halt_result = kill_switch_halt_result
        self._run_intra_safety_preamble = run_intra_safety_preamble
        self._attach_evidence_freshness = attach_evidence_freshness
        self._attach_pnl = attach_pnl
        self._activate_cost_session = activate_cost_session
        self._compute_deployable_cash = compute_deployable_cash
        self._record_account_snapshot = record_account_snapshot
        self._run_intraday_opportunity_scan = run_intraday_opportunity_scan
        self._sync_positions_from_broker = sync_positions_from_broker
        self._total_pnl_since_reset = total_pnl_since_reset
        if persist_intra_check_report is not None:
            self._persist_intra_check_report = persist_intra_check_report  # else: this part's own body
        if run_intra_check_body is not None:
            self._run_intra_check_body = run_intra_check_body  # else: this part's own body
        self._state, self._now = state, now
        self._trail_collaborators = trail_collaborators
        self._install_sigterm_unwind = install_sigterm_unwind
        self._repair_stops_on_kill = repair_stops_on_kill
        self._restore_sigterm = restore_sigterm

    @property
    def _intra_preamble_deferred(self):
        return self._state.get("_intra_preamble_deferred")

    @_intra_preamble_deferred.setter
    def _intra_preamble_deferred(self, value) -> None:
        self._state.set("_intra_preamble_deferred", value)

    @property
    def _last_account_snapshot(self):
        return self._state.get("_last_account_snapshot")

    @_last_account_snapshot.setter
    def _last_account_snapshot(self, value) -> None:
        self._state.set("_last_account_snapshot", value)

    @property
    def _last_evidence_freshness(self):
        return self._state.get("_last_evidence_freshness")

    @_last_evidence_freshness.setter
    def _last_evidence_freshness(self, value) -> None:
        self._state.set("_last_evidence_freshness", value)

    def run_intra_check(self) -> dict:
        """Intra-session circuit-breaker check, plus the durable record of
        its own output.

        Same gap as `run_morning`/`run_position_review` (2026-09-18 sweep):
        `stop_coverage_gaps` is computed from live broker state every tick
        and handed to the notifier with no other durable home. This wrapper
        persists every return path, keyed by run_id (not date — this fires
        roughly every 30 minutes, so a date-keyed row would keep only the
        last tick; see `Database.save_intra_check_report`). Fail-soft.
        """
        # The intraday scan buys through the same execution step as the
        # morning, so a wrapper kill between its buys and their stops leaves
        # a buy naked. Same unwind as the morning: add owed stops first.
        prior = self._install_sigterm_unwind("intra_check") if self._install_sigterm_unwind is not None else None
        try:
            return self._run_intra_check_recorded()
        except SessionTerminated:
            if self._repair_stops_on_kill is not None:
                self._repair_stops_on_kill("intra_check")
            raise
        finally:
            if self._restore_sigterm is not None:
                self._restore_sigterm(prior)

    def _run_intra_check_recorded(self) -> dict:
        """The tick's body plus the durable record of its output."""
        self._last_evidence_freshness = None
        self._last_account_snapshot = None
        self._intra_preamble_deferred = ""
        result = self._run_intra_check_body()
        if isinstance(result, dict) and self._intra_preamble_deferred:
            result["preamble_deferred"] = self._intra_preamble_deferred
        self._attach_pnl(result)
        self._attach_evidence_freshness(result)
        self._persist_intra_check_report(result)
        return result

    def _persist_intra_check_report(self, result: dict) -> None:
        if not isinstance(result, dict):
            return
        run_id = result.get("run_id")
        if not run_id:
            return
        try:
            self.db.save_intra_check_report(
                run_id=run_id,
                date=session_date_key(),
                payload=result,
            )
        except Exception as exc:  # noqa: BLE001 — never break the push
            _site(self, "report_persist", exc)
        else:
            _site(self, "report_persist")

    def _run_intra_check_body(self) -> dict:
        """Lightweight intra-session maintenance tick (no LLM calls).

        Scheduled between morning and midday (typically 12:00 ET). It
        reconciles fills, repairs stop coverage on anything found
        unprotected, reports the session snapshot, and runs the bounded
        intraday opportunity scan.

        **It no longer carries an account-level loss breaker.** That whole
        mechanism — a daily P&L vs loss-limit test that halted the desk —
        was removed 2026-09-20 on the owner's instruction (retired item 32,
        docs/INCIDENT_HISTORY.md). Loss protection is the per-position stop
        living at the broker, which does not depend on this tick running.
        Runs in ~5 seconds.
        """
        ctx = RunContext.start("intra_check")
        run_id = ctx.run_id
        logger.info("=== Intra-session risk check: %s ===", run_id)

        if not self._is_trading_day():
            logger.info("Intra check skipped: market closed for non-trading day")
            return {"status": "market_holiday", "run_id": run_id}
        if session_has_ended(self.broker, self._now() if self._now else None):
            logger.info("Intra check skipped: exchange session already closed")
            return {"status": "session_closed", "run_id": run_id}

        halt = self._kill_switch_halt_result(run_id)
        if halt is not None:
            return halt

        self._activate_cost_session(run_id, "intra_check")

        # Board item 127 (2026-09-19). Every write below reaches the broker,
        # and `intra_check` is exempt from the wrapper's session lock (item
        # 128), so this whole preamble used to run with no lock at all. It
        # now runs only while this process holds the same advisory flock the
        # paid scan below takes (`_intraday_scan_process_lock`) — which the
        # standalone coverage sweep's repair pass also takes — and only while
        # no morning/midday/close session owns the desk
        # (`_blocking_owner_session`, the check the paid scan already uses).
        # A live session runs this same preamble itself near the start of its
        # own run, and it may be in the middle of cancelling stops to sell; a
        # stop added here in that window is the worst pairing item 127 names.
        # Deferring skips only this tick's preamble; the next tick re-reads
        # the broker (no loss check any more, retired item 32). Board item
        # 127 is open on that exposure.
        # Board item 177: the free safety work below also has its own entry
        # point (`run_intra_safety`) and systemd unit; this tick still calls it.
        coverage_gaps, preamble_deferred = self._run_intra_safety_preamble(run_id)
        self._intra_preamble_deferred = preamble_deferred

        try:
            account = self.broker.get_account()
            positions = self.broker.get_positions()
        except Exception as e:
            _site(self, "broker_query", e, log=logger)
            return {"status": "broker_error", "run_id": run_id, "error": str(e), "stop_coverage_gaps": coverage_gaps}

        total_value = account["portfolio_value"]
        last_equity = account.get("last_equity", total_value)
        daily_pnl = total_value - last_equity
        self._record_account_snapshot(total_value, last_equity)
        ctx.account = account
        ctx.positions = positions
        ctx.cash = account["cash"]
        ctx.deployable_cash = self._compute_deployable_cash(ctx.cash, ctx.positions)
        ctx.total_value = total_value
        ctx.last_equity = last_equity
        ctx.daily_pnl = daily_pnl
        self._sync_positions_from_broker(positions)
        daily_return_pct = (daily_pnl / last_equity * 100) if last_equity > 0 else 0
        total_pnl, total_return_pct, total_pnl_since = self._total_pnl_since_reset(total_value)
        logger.info(
            "Intra snapshot: equity=$%.2f, last_close=$%.2f, pnl=$%.2f (%.2f%%), positions=%d",
            total_value,
            last_equity,
            daily_pnl,
            daily_return_pct,
            len(positions),
        )

        result = {
            "status": "ok",
            "daily_pnl": daily_pnl,
            "daily_return_pct": daily_return_pct,
            "total_pnl": total_pnl,
            "total_return_pct": total_return_pct,
            "total_pnl_since": total_pnl_since,
            "positions": len(positions),
            "run_id": run_id,
            "stop_coverage_gaps": coverage_gaps,
        }
        # Owner ruling 2026-10-08: trail stops at every 30-minute check, not
        # only at midday/close; before the scan, so protection moves first.
        # Free and deterministic; see src/intraday/tick_trail.py.
        result["tick_trail"] = trail_on_tick(
            positions=positions,
            run_id=run_id,
            preamble_deferred=preamble_deferred,
            total_value=total_value,
            **self._tick_trail_collaborators(),
        )
        # 2026-08-19 intraday opportunity-discovery fix: bounded new-
        # opportunity scan.
        try:
            scan_result = self._run_intraday_opportunity_scan(ctx)
        except EvidenceGateEvaluationError:
            # A broken refusal gate is not an ordinary opportunity-scan miss:
            # no verdict exists, so let the session's established failed-run
            # notification/nonzero boundary report it. Other scan crashes keep
            # their historical non-fatal, explicitly recorded result below.
            raise
        except PaidAnalysisSuspended as exc:
            scan_result = {
                "status": "paid_analysis_suspended",
                "run_id": run_id,
                "error": str(exc),
                "suspended": "intraday opportunity discovery only",
                "preserved": "fill reconciliation and stop-coverage repair",
            }
        except Exception as e:  # noqa: BLE001 — never let the scan
            # turn a routine intra_check tick into a failed run.
            # Operator-honesty fix: a crash used to set scan_result to
            # None, which is exactly what a healthy "ran, nothing to
            # do" tick also produces — no `intraday_scan` key, session
            # status stays "ok". The Telegram feed and the rehearsal
            # rig were both blind to the difference. Attaching a
            # dict (mirroring the `paid_analysis_suspended` shape
            # above) makes the crash visible through the same nested
            # path, while the tick itself still completes normally.
            _site(self, "intraday_scan", e, log=logger)
            scan_result = failed_scan_result(e, run_id)
        if scan_result is not None:
            result["intraday_scan"] = scan_result
            if scan_result.get("status") == "intraday_executed":
                # Scan went through ExecutionStage; refresh the
                # local table from broker truth (the start-of-tick
                # snapshot above is now stale).
                self._sync_positions_from_broker()
        return result

    def _tick_trail_collaborators(self) -> dict:
        """The host's trail, ATR read, broker-write lock, owner check and sweep split.

        Handed in explicitly when given; otherwise read live off the host through
        the `state` view. A host that lacks one gets None for it, and the tick
        trail then reports itself unavailable instead of guessing.
        """
        if self._trail_collaborators is not None:
            return dict(self._trail_collaborators)

        def _host(name):
            try:
                return self._state.get(name) if self._state is not None else None
            except AttributeError:
                return None

        sweeper_of = _host("_sweeper")

        def _split(positions):
            sweeper = sweeper_of() if callable(sweeper_of) else None
            return sweeper.split_positions(positions) if sweeper is not None else (positions, None)

        return {
            "apply_deterministic_trails": _host("_apply_deterministic_trails"),
            "atr_for_symbol": _host("_atr_for_symbol"),
            "process_lock": _host("_intraday_scan_process_lock"),
            "blocking_owner_session": _host("_blocking_owner_session"),
            "split_positions": _split,
            # Owner ruling 2026-10-09: trend exit at the tick, daily closes only.
            "trend_exit": wire_trend_exit(_host),
        }
