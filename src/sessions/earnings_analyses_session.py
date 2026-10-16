"""Earnings-analyses load step (moved verbatim from TradingPipeline)."""

from __future__ import annotations

import logging
from src.pipeline_context import RunContext

logger = logging.getLogger(__name__)


class EarningsAnalysesLoadSession:
    """Earnings-analyses load step (moved verbatim from TradingPipeline)."""

    def __init__(
        self,
        *,
        config,
        earnings_analyst,
        earnings_provider,
    ) -> None:
        self._config = config
        self._earnings_analyst = earnings_analyst
        self._earnings_provider = earnings_provider

    def run(
        self,
        run_id: str,
        session: str = "morning",
        ctx: RunContext | None = None,
        universe: list[str] | None = None,
    ) -> tuple[list, list]:
        """Hot-path consumer: read cached earnings analyses, never call the LLM.

        The LLM-producing path is `run_earnings_preprocess()`, which runs
        pre-market (08:00-09:15 ET) and synchronously analyzes + confirms
        every new 10-Q/10-K. By the time morning/midday/evening fire, the
        authoritative result is already on disk.

        This method returns:
          - cached analyses for any filing already confirmed by preprocess
          - placeholder `queued=True` entries for filings that preprocess
            missed (e.g. preprocess didn't run, or the filing dropped after
            preprocess but before a later session). PM sees these and sizes
            down accordingly — better than blocking the session on an LLM.

        No background threads, no session-time token spend. The
        `run_id` + `session` + `ctx` signature is preserved for
        compatibility with MorningResearchStage's callable injection.
        """
        try:
            reports = self._earnings_provider.check_and_fetch(
                universe or self._config.trading.universe,
            )
            if not reports:
                return [], []

            new_reports = [r for r in reports if r.is_new]
            cached_reports = [r for r in reports if not r.is_new]

            cached_results = self._earnings_analyst.analyze_reports(cached_reports)

            for r in new_reports:
                cached_results.append(
                    {
                        "symbol": r.symbol,
                        "analysis": None,
                        "is_new": True,
                        "queued": True,
                        "form_type": r.form_type,
                        "filing_date": r.filing_date,
                        "current_until": r.current_until,
                    }
                )

            if new_reports:
                symbols = ", ".join(r.symbol for r in new_reports)
                logger.warning(
                    "[%s] %d filings missed pre-market preprocessing (%s); "
                    "surfacing as placeholder only — PM will size down.",
                    session,
                    len(new_reports),
                    symbols,
                )

            logger.info(
                "[%s] Earnings: %d cached analyses, %d unanalyzed placeholders",
                session,
                len(cached_results) - len(new_reports),
                len(new_reports),
            )
            return reports, cached_results
        except Exception as e:
            # audit round 2: swallowing here made data_status["earnings"]
            # "failed" unreachable — a full SEC-EDGAR outage was
            # indistinguishable from "no filings today", so RM's
            # data_degraded advisory never counted earnings. Morning routes
            # through MorningResearchStage, whose except sets the status;
            # midday/evening call sites wrap this locally to keep their
            # continue-without-earnings behavior.
            logger.error("[%s] Earnings load failed: %s", session, e)
            raise
