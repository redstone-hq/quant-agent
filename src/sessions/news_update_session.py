"""News update step (moved verbatim from TradingPipeline)."""

from __future__ import annotations

import logging
from src.agents.base import agent_log_kwargs
from src.agents.base import seat_acceptance_kwargs
from src.cost_circuit import PaidAnalysisSuspended

logger = logging.getLogger(__name__)


class NewsUpdateSession:
    """News update step (moved verbatim from TradingPipeline)."""

    def __init__(
        self,
        *,
        config,
        db,
        news_analyst,
        news_provider,
        news_store,
    ) -> None:
        self._config = config
        self._db = db
        self._news_analyst = news_analyst
        self._news_provider = news_provider
        self._news_store = news_store

    def run(
        self,
        run_id: str,
        session: str = "morning",
        universe: list[str] | None = None,
        held_symbols: list[str] | None = None,
        candidate_symbols: list[str] | None = None,
    ) -> "tuple[NewsIntelligenceReport | None, NewsCoverage | None]":
        """Fetch news, run intelligence analysis, save report. Session-aware.

        - morning: full 3-layer build. prior_session_report=None.
        - midday:  delta mode. prior_session_report=morning's snapshot.
        - evening: summary mode. prior_session_report=midday's or morning's.

        Session-tagged reports persist alongside the latest full_report.json so
        each session's output is individually recoverable for audit / debug.

        `held_symbols` / `candidate_symbols` (2026-08-30 owner decision) are
        the caller's ALREADY-ORDERED lists of symbols to also fetch
        individually via Yahoo Finance's per-symbol RSS — see
        NewsDataProvider.fetch_news. The deterministic selection rule lives
        HERE, not in the provider: held positions first, then the run's
        shortlisted candidates, each list in the caller's own stable order
        (never raw set iteration — see the callers of this method), deduped
        while preserving that order. Every name is fetched (no count cap
        since 2026-10-19); this method only decides ordering.

        Returns `(intel_report, coverage)`. `coverage` (src.data.news.
        NewsCoverage) is the 2026-08-28 fix for a dead feed vanishing
        silently: before this, a feed that 404'd or 403'd was dropped with a
        log warning and the news stage still reported "ok" regardless of
        how many wires actually came back. `coverage` is returned even when
        the analyst call itself fails below, since the fetch already
        happened and the caller (MorningResearchStage) needs it either way
        to set data_status["news"] honestly.
        """
        coverage = None
        try:
            research_universe = universe or self._config.trading.universe
            per_symbol_symbols = list(
                dict.fromkeys(
                    [str(s).strip().upper() for s in (held_symbols or []) if str(s).strip()]
                    + [str(s).strip().upper() for s in (candidate_symbols or []) if str(s).strip()]
                )
            )
            news_items, coverage = self._news_provider.fetch_news(symbols=per_symbol_symbols)
            news_text = self._news_provider.format_for_prompt(
                news_items,
                max_items=self._config.news.max_prompt_items,
            )
            stock_mentions = self._news_provider.tag_symbol_mentions(news_items, research_universe)
            previous_narrative = self._news_store.load_macro_narrative()
            # For midday/evening, load the most recent prior session report as
            # a diff baseline. Prefer midday over morning when both exist
            # (evening sees the most recent snapshot available).
            prior_session_report = None
            if session == "midday":
                prior_session_report = self._news_store.load_daily_report("morning")
            elif session == "evening":
                prior_session_report = self._news_store.load_daily_report(
                    "midday"
                ) or self._news_store.load_daily_report("morning")
            intel_report, result = self._news_analyst.analyze(
                news_text=news_text,
                universe=research_universe,
                stock_mentions=stock_mentions,
                previous_narrative=previous_narrative,
                session=session,
                prior_session_report=prior_session_report,
                news_coverage=coverage,
            )
            if intel_report:
                report_dict = intel_report.model_dump()
                self._news_store.save_daily_report(report_dict, session=session)
                self._news_store.save_macro_narrative(report_dict["macro_narrative"])
                if report_dict.get("stock_news"):
                    self._news_store.save_stock_alerts(report_dict["stock_news"])
                # collapsed_count / source_count are persisted so the dedup
                # stage stays auditable after the fact — you can re-measure
                # the duplication rate from the archive without re-fetching.
                # per_symbol (2026-08-30) is persisted for the same reason:
                # measuring the per-symbol duplicate rate after the fact
                # shouldn't require re-fetching either.
                self._news_store.save_raw_headlines(
                    [
                        {
                            "title": i.title,
                            "source": i.source,
                            "summary": i.summary,
                            "collapsed_count": getattr(i, "collapsed_count", 1),
                            "source_count": getattr(i, "source_count", 1),
                            "per_symbol": getattr(i, "per_symbol", False),
                        }
                        for i in news_items
                    ]
                )
                n_changes = len(intel_report.state_changes)
                n_stocks = len(intel_report.stock_news)
                logger.info(
                    "[%s] News intelligence: sentiment=%s, changes=%d, stocks=%d",
                    session,
                    intel_report.market_sentiment,
                    n_changes,
                    n_stocks,
                )
            self._db.insert_agent_log(
                **seat_acceptance_kwargs("agent_failure" if not intel_report else None),
                agent_name=f"news_analyst_{session}",
                run_id=run_id,
                input_summary=(
                    f"{len(news_items)} news items "
                    f"({coverage.describe() if coverage is not None else 'coverage unknown'})"
                ),
                input_message=result.user_message,
                output_summary=f"sentiment={intel_report.market_sentiment}, changes={len(intel_report.state_changes)}"
                if intel_report
                else "parse_error",
                full_response=result.raw_text,
                model=result.model,
                tokens_used=result.tokens_used,
                input_tokens=result.input_tokens,
                output_tokens=result.output_tokens,
                cost_usd=result.cost_usd,
                **agent_log_kwargs(result),
            )
            return intel_report, coverage
        except PaidAnalysisSuspended:
            raise
        except Exception as e:
            logger.error("[%s] News analyst failed: %s", session, e)
            return None, coverage
