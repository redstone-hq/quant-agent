"""Earnings pre-processing session (moved verbatim from TradingPipeline)."""

from __future__ import annotations

import logging
from src.cost_circuit import PaidAnalysisSuspended
from src.pipeline_context import RunContext
from src.agents.base import agent_log_kwargs, seat_acceptance_kwargs

logger = logging.getLogger(__name__)


class EarningsPreprocessSession:
    """Earnings pre-processing session (moved verbatim from TradingPipeline)."""

    # No broker stop audit in this session; see src/sessions/stop_audit.py.
    AUDITS_BROKER_STOPS = False

    def __init__(
        self,
        *,
        activate_cost_session,
        alert_form4_backlog_before_open,
        drain_pending_protection_restores,
        drain_pending_repegs,
        earnings_preprocess_symbols,
        is_trading_day,
        paid_suspended_payload,
        reconcile_orphan_pending_submits,
        record_congressional_refresh,
        record_form4_backlog,
        require_paid_analysis,
        surface_reconcile_outcomes,
        watched_research_symbols,
        smart_money_refresh_sources_word,
        config,
        db,
        earnings_analyst,
        earnings_provider,
        smart_money_provider,
    ) -> None:
        self._activate_cost_session = activate_cost_session
        self._alert_form4_backlog_before_open = alert_form4_backlog_before_open
        self._drain_pending_protection_restores = drain_pending_protection_restores
        self._drain_pending_repegs = drain_pending_repegs
        self._earnings_preprocess_symbols = earnings_preprocess_symbols
        self._is_trading_day = is_trading_day
        self._paid_suspended_payload = paid_suspended_payload
        self._reconcile_orphan_pending_submits = reconcile_orphan_pending_submits
        self._record_congressional_refresh = record_congressional_refresh
        self._record_form4_backlog = record_form4_backlog
        self._require_paid_analysis = require_paid_analysis
        self._surface_reconcile_outcomes = surface_reconcile_outcomes
        self._watched_research_symbols = watched_research_symbols
        self._smart_money_refresh_sources_word = smart_money_refresh_sources_word
        self._config = config
        self._db = db
        self._earnings_analyst = earnings_analyst
        self._earnings_provider = earnings_provider
        self._smart_money_provider = smart_money_provider

    def run(self) -> dict:
        """Pre-market earnings analysis — the ONLY place that calls the LLM
        for 10-Q/10-K filings.

        Scheduled at 08:00-09:15 ET via launchd. Synchronously fetches any
        new filings, runs the earnings analyst on each, saves the analysis,
        and confirms the filing so later sessions see it as cached.

        Hot sessions (morning/midday/evening) use `_load_earnings_analyses`
        which is read-only. That separation guarantees no session burns
        tokens on fresh LLM work — a filing that drops after preprocess
        surfaces as a `queued=True` placeholder and PM sizes down.
        """
        ctx = RunContext.start("earnings_preprocess")
        run_id = ctx.run_id
        logger.info("=== Earnings preprocessing: %s ===", run_id)

        if not self._is_trading_day():
            logger.info("Earnings preprocess skipped: market closed for non-trading day")
            return {"status": "market_holiday", "run_id": run_id}

        self._activate_cost_session(run_id, "earnings_preprocess")

        # Drain orphaned protection-restore intents from any prior session
        # that died mid-finalize. earnings_preprocess (08:00-09:15 ET) is the
        # first session of the trading day, so if an overnight evening run
        # left state in `pending_protection_restores`, this is the earliest
        # opportunity to recover before the 09:30 ET open. Without this call
        # an unprotected position would ride the open-gap with no stop —
        # matches the drain pattern used in run_morning / run_position_review
        # / run_intra_check / run_evening.
        drained = self._drain_pending_protection_restores()
        self._drain_pending_repegs()
        self._reconcile_orphan_pending_submits()  # audit F4
        # Item 101: this pre-market session runs no stop-out reconcile, but a
        # naked position it re-protects is a live-risk event the owner should
        # still hear about — surface the drain count on its own.
        self._surface_reconcile_outcomes(drained_count=drained, run_id=run_id)

        # Refresh the credentialless SEC Form 4 cache before any paid-analysis
        # gate. This deterministic source work remains available while the
        # cost circuit is latched and lets the morning session consume a
        # bounded local cache instead of crawling EDGAR on the trading path.
        smart_money_refresh: dict = {"status": "disabled"}
        if self._config.smart_money.enabled:
            try:
                # Watched names are passed so the Form 4 discovery budget
                # (`max_filings_per_refresh`) is spent on the desk's own
                # names before the rest of the listed market. Nothing is
                # filtered out — external candidate nomination still reads
                # filings on names the desk does not watch.
                watched = self._watched_research_symbols()
                try:
                    smart_money_refresh = self._smart_money_provider.refresh(watched)
                except TypeError:
                    smart_money_refresh = self._smart_money_provider.refresh()
                logger.info(
                    "Smart-money refresh (%s): %s",
                    self._smart_money_refresh_sources_word(self._config.smart_money.congress_enabled),
                    smart_money_refresh,
                )
                # Backlog depth and watched-name coverage, named in their own
                # line: `refresh` runs once a day pre-market, so a residue
                # cannot drain until tomorrow.
                logger.info(
                    "Smart-money Form 4 backlog: pending=%s watched_pending=%s "
                    "cap_reached=%s watched_read_through=%s/%s "
                    "drain_deadline_hit=%s edgar_coverage=%s",
                    smart_money_refresh.get("pending_filings"),
                    smart_money_refresh.get("watched_pending_filings"),
                    smart_money_refresh.get("discovery_cap_reached"),
                    smart_money_refresh.get("watched_names_read_through"),
                    smart_money_refresh.get("watched_names"),
                    smart_money_refresh.get("watched_drain_deadline_hit"),
                    smart_money_refresh.get("edgar_coverage"),
                )
                # ...and RECORDED where the desk records its status. Until
                # 2026-09-19 these counts existed only in a log line and the
                # job's stdout, so no one could ask the database whether the
                # backlog was draining from one morning to the next.
                self._record_form4_backlog(run_id, smart_money_refresh)
                self._record_congressional_refresh(run_id, smart_money_refresh)
                self._alert_form4_backlog_before_open(smart_money_refresh)
            except Exception as exc:
                logger.warning("SEC Form 4 refresh failed softly: %s", exc)
                smart_money_refresh = {
                    "status": "provider_error",
                    "error": type(exc).__name__,
                }

        try:
            reports = self._earnings_provider.check_and_fetch(
                self._earnings_preprocess_symbols(),
            )
        except Exception as e:
            logger.error("Earnings preprocess: fetch failed: %s", e)
            return {
                "status": "fetch_error",
                "run_id": run_id,
                "error": str(e),
                "smart_money_refresh": smart_money_refresh,
            }

        new_reports = [r for r in reports if r.is_new]
        if not new_reports:
            logger.info("Earnings preprocess: no new filings, nothing to analyze.")
            return {
                "status": "nothing_new",
                "run_id": run_id,
                "count": 0,
                "smart_money_refresh": smart_money_refresh,
            }

        logger.info(
            "Earnings preprocess: analyzing %d new filings: %s",
            len(new_reports),
            ", ".join(r.symbol for r in new_reports),
        )
        # Owner-facing record of WHICH filings this pass handled (2026-09-18:
        # the message used to say "analyzed: 1 confirmed: 1 failed: 0" and
        # the owner asked "which one? what's the symbol? what's the
        # company?"). Report-only — nothing reads this back into a decision.
        filings_waiting = [
            {"symbol": r.symbol, "form_type": r.form_type, "filing_date": r.filing_date, "outcome": "waiting"}
            for r in new_reports
        ]
        try:
            self._require_paid_analysis("earnings_analyst")
            results = self._earnings_analyst.analyze_reports(new_reports)
        except PaidAnalysisSuspended as exc:
            # No filing failure is recorded: the filing remains new and will
            # be eligible after an operator resets the circuit. Attach the
            # already-computed `filings_waiting` backlog so the notifier
            # renders "suspended, N filing(s) waiting" instead of the bare
            # counts, which read as "nothing happened" for a real backlog.
            payload = self._paid_suspended_payload(
                run_id,
                error=exc,
                filings_waiting=filings_waiting,
            )
            payload["smart_money_refresh"] = smart_money_refresh
            return payload
        except Exception as e:
            logger.error("Earnings preprocess: LLM analysis failed: %s", e, exc_info=True)
            # Record failures so the retry bounds kick in for each filing.
            for r in new_reports:
                try:
                    self._earnings_provider.record_failure(r)
                except Exception as re:
                    logger.error("record_failure failed for %s: %s", r.symbol, re)
            return {
                "status": "analysis_error",
                "run_id": run_id,
                "error": str(e),
                "filings": filings_waiting,
            }

        # Match results to reports by (symbol, form_type, filing_date), not
        # just symbol. Same-symbol multiple-form-day is rare but real
        # (10-Q + 10-K can land the same fiscal-year-end day). Symbol-only
        # matching meant a successful 10-K silently flagged a failed 10-Q
        # as confirmed and never consumed its retry budget — the failed
        # filing would then be re-queued every preprocess run forever.
        def _filing_key(symbol: str, form_type: str | None, filing_date: str | None):
            return (symbol, form_type, filing_date)

        successful_keys = {
            _filing_key(res["symbol"], res.get("form_type"), res.get("filing_date"))
            for res in results
            if res.get("is_new")
        }
        failed_reports = [
            r for r in new_reports if _filing_key(r.symbol, r.form_type, r.filing_date) not in successful_keys
        ]
        for report in failed_reports:
            try:
                self._earnings_provider.record_failure(report)
            except Exception as re:
                logger.error("record_failure failed for %s: %s", report.symbol, re)

        # Log each LLM call (parity with the inline bg-thread path).
        analyzed_count = 0
        for res in results:
            agent_result = res.get("agent_result")
            if agent_result is None:
                continue
            sym = res.get("symbol", "?")
            analysis = res.get("analysis") or {}
            sentiment = (analysis.get("investment_implications") or {}).get("sentiment", "?")
            try:
                self._db.insert_agent_log(
                    **seat_acceptance_kwargs("agent_failure" if not analysis else None),
                    agent_name="earnings_analyst_preprocess",
                    run_id=run_id,
                    input_summary=f"{sym} {res.get('form_type', '?')} filed {res.get('filing_date', '?')}",
                    input_message=agent_result.user_message,
                    output_summary=(f"sentiment={sentiment}" if res.get("analysis") else "parse_error"),
                    full_response=agent_result.raw_text,
                    model=agent_result.model,
                    tokens_used=agent_result.tokens_used,
                    input_tokens=agent_result.input_tokens,
                    output_tokens=agent_result.output_tokens,
                    cost_usd=agent_result.cost_usd,
                    **agent_log_kwargs(agent_result),
                )
            except Exception as e:
                logger.error("Earnings preprocess: log insert failed for %s: %s", sym, e)
            analyzed_count += 1

        # Confirm filings. Do this AFTER logging so a crash between the two
        # leaves the filing still "new" for the next preprocess run.
        # Match by (symbol, form_type, filing_date) to avoid confirming a
        # failed 10-Q on the back of a successful same-day 10-K.
        confirmed = 0
        for r in new_reports:
            if _filing_key(r.symbol, r.form_type, r.filing_date) in successful_keys:
                try:
                    self._earnings_provider.confirm_filing(r)
                    confirmed += 1
                except Exception as e:
                    logger.warning("confirm_filing failed for %s: %s", r.symbol, e)

        logger.info(
            "Earnings preprocess complete: %d analyzed, %d confirmed, %d failed",
            analyzed_count,
            confirmed,
            len(failed_reports),
        )
        # Per-filing outcome for the owner message: the reader's own
        # sentiment / conviction / key_thesis where it produced one, and
        # "failed" where it did not. Same (symbol, form, date) key as the
        # confirmation logic above, so a same-day 10-Q and 10-K stay apart.
        verdict_by_key: dict = {}
        for res in results:
            analysis = res.get("analysis") or {}
            impl = analysis.get("investment_implications") or {}
            if not isinstance(impl, dict):
                impl = {}
            verdict_by_key[
                _filing_key(
                    res.get("symbol"),
                    res.get("form_type"),
                    res.get("filing_date"),
                )
            ] = {
                "sentiment": impl.get("sentiment"),
                "conviction": impl.get("conviction"),
                "key_thesis": impl.get("key_thesis"),
            }
        filings: list[dict] = []
        for r in new_reports:
            key = _filing_key(r.symbol, r.form_type, r.filing_date)
            row = {
                "symbol": r.symbol,
                "form_type": r.form_type,
                "filing_date": r.filing_date,
                "outcome": "analyzed" if key in successful_keys else "failed",
                **r.evidence_fields(),
            }
            row.update(verdict_by_key.get(key) or {})
            filings.append(row)
        return {
            "status": "preprocessed",
            "run_id": run_id,
            "analyzed": analyzed_count,
            "confirmed": confirmed,
            "failed": len(failed_reports),
            "filings": filings,
            "smart_money_refresh": smart_money_refresh,
        }
