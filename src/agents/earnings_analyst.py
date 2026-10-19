"""Earnings Analyst Agent — reads SEC filings and writes structured analyses.

For new filings: reads raw text, produces analysis, saves to markdown file.
For existing filings: returns previously saved analysis.
"""

import json
import logging
import os
import re
from pathlib import Path

from pydantic import ValidationError

from src.agents.base import BaseAgent, AgentResult
from src.cost_circuit import PaidAnalysisSuspended
from src.data.earnings import EarningsReport
from src.models import EarningsAnalysis

logger = logging.getLogger(__name__)

PROMPT_PATH = Path(__file__).parent.parent.parent / "config" / "prompts" / "earnings_analyst.md"

# 2026-08-13 agent audit — "earnings reasoning quality". `build_user_message`
# passes filing text plus symbol / form_type / filing_date. No share price, no
# market cap, no multiple. The prompt now says so explicitly and asks for
# `[UNSOURCED:no_market_data]` when nothing grounded can be said — but a prompt
# rule with no detector is a rule nobody can tell was broken, and the worked
# example that shipped for months invented "~28x forward earnings" while PM
# sized off that field.
#
# These patterns match claims that REQUIRE a share price to compute, so a
# filing alone cannot support them. Deliberately NOT included: leverage and
# coverage ratios ("net debt 2.1x EBITDA", "3.4x interest coverage"), which
# 10-Q/10-K text does disclose and which the agent may legitimately cite.
_PRICE_DERIVED_CLAIM_PATTERNS = (
    (r"\bP\s*/\s*E\b", "P/E"),
    (r"\bPE\s+(?:ratio|multiple)\b", "PE ratio"),
    (r"\bPEG\b", "PEG"),
    (r"\bP\s*/\s*S\b", "P/S"),
    (r"\bP\s*/\s*B\b", "P/B"),
    (r"\bEV\s*/\s*(?:EBITDA|EBIT|sales|revenue)\b", "EV/x"),
    (r"\bprice[-\s]to[-\s](?:earnings|sales|book)\b", "price-to-x"),
    (r"\bmarket\s+cap(?:italization)?\b", "market cap"),
    (r"\bshare\s+price\b", "share price"),
    (r"\bstock\s+price\b", "stock price"),
    (r"\btrading\s+at\b", "trading at"),
    (
        r"\d+(?:\.\d+)?\s*x\s+(?:forward\s+|trailing\s+|fwd\s+|ntm\s+)?"
        r"(?:earnings|sales|revenue|book|free\s+cash\s+flow|fcf)\b",
        "Nx earnings/sales",
    ),
)
_PRICE_DERIVED_CLAIM_RE = tuple(
    (re.compile(pattern, re.IGNORECASE), label) for pattern, label in _PRICE_DERIVED_CLAIM_PATTERNS
)

# The detector below used to be advisory-only: it logged a fabricated
# valuation claim but never stopped it reaching the PM, and because the
# analysis is cached to disk for the life of the filing, one bad LLM call
# meant the SAME invented number was re-served to the PM every run until a
# human noticed the log line. This is what actually gets written in its
# place once a claim is caught — the rest of the analysis is untouched.
_UNSOURCED_VALUATION_DISCLOSURE = (
    "[Valuation claim removed: this filing does not disclose a share price, "
    "market cap, or multiple, so no price-derived valuation judgement can be "
    "grounded. Treat this name's valuation as UNKNOWN, not neutral.]"
)


class EarningsAnalystAgent(BaseAgent):
    # See _validate_analysis(): parsed JSON is validated as EarningsAnalysis(**parsed).
    result_model = EarningsAnalysis

    @property
    def name(self) -> str:
        return "earnings_analyst"

    @property
    def system_prompt(self) -> str:
        if PROMPT_PATH.exists():
            return PROMPT_PATH.read_text()
        return "You are an earnings analyst. Respond with JSON."

    def build_user_message(self, **kwargs) -> str:
        symbol: str = kwargs["symbol"]
        form_type: str = kwargs["form_type"]
        filing_date: str = kwargs["filing_date"]
        filing_text: str = kwargs["filing_text"]
        prior_analysis: str = kwargs.get("prior_analysis", "")

        prior_section = ""
        if prior_analysis:
            prior_section = f"""## Prior Analysis (for context)
{prior_analysis}

---

"""

        return f"""{prior_section}## Filing: {symbol} {form_type} (filed {filing_date})

{filing_text}

Analyze this filing and respond with JSON. Cite specific numbers from the text above."""

    def analyze_reports(self, reports: list[EarningsReport]) -> list[dict]:
        """Analyze all reports. Only schema-validated analyses are returned.

        Returns list of {symbol, analysis_dict, agent_result_or_none}.
        """
        results = []

        for report in reports:
            try:
                results.extend(self._analyze_one(report))
            except PaidAnalysisSuspended:
                raise
            except Exception as e:  # noqa: BLE001 — audit round 2: one bad
                # filing (corrupt text, LLM error escaping _analyze_new, disk
                # failure in _save_analysis) must not abort the WHOLE batch —
                # the remaining symbols' filings would silently go unanalyzed
                # while record_failure never ticked for them.
                logger.error("earnings: analysis failed for %s %s — isolating: %s", report.symbol, report.form_type, e)
                try:
                    self.earnings_provider_record_failure(report)
                except Exception:  # noqa: BLE001
                    pass
        return results

    def earnings_provider_record_failure(self, report) -> None:
        """Overridable seam: batch-isolation failure ticks the same 3-strike
        counter as an in-analysis failure. No-op default when the provider
        isn't wired (tests)."""
        provider = getattr(self, "earnings_provider", None)
        if provider is not None:
            provider.record_failure(report)

    def _analyze_one(self, report: EarningsReport) -> list[dict]:
        results = []
        if True:
            if report.is_new and report.text_excerpt:
                # New filing — run LLM analysis
                analysis, agent_result = self._analyze_new(report)
                if analysis:
                    # Save analysis to disk
                    self._save_analysis(report.analysis_path, report, analysis)
                    results.append(
                        {
                            "symbol": report.symbol,
                            "analysis": analysis,
                            "agent_result": agent_result,
                            "is_new": True,
                            "form_type": report.form_type,
                            "filing_date": report.filing_date,
                            "current_until": report.current_until,
                            **report.evidence_fields(),
                            # Pointer for the PM's short verdict (item 18): the
                            # full 8-field extraction is still on disk at this
                            # path, retrievable for audit — see
                            # PortfolioManagerAgent's earnings-section rendering
                            # and `src/data/earnings_deep_dive.py`, which already
                            # reads analyses back out by this same path.
                            "analysis_path": report.analysis_path,
                            # Real, parsed SEC XBRL values for the fields that
                            # map one-to-one onto this analysis (see
                            # EarningsDataProvider._xbrl_comparable_values) —
                            # ground truth `_classify_earnings_status`
                            # (src/pipeline_stages.py) cross-checks the LLM's
                            # own reported figures against, to catch a
                            # confident but fabricated number that isn't empty
                            # and isn't self-flagged. {} (not missing) when no
                            # XBRL data was available for this filer/period.
                            "xbrl_facts": report.xbrl_facts,
                        }
                    )
            elif report.analysis_path and Path(report.analysis_path).exists():
                # Existing analysis — read from disk
                analysis = self._load_analysis(report)
                if analysis:
                    results.append(
                        {
                            "symbol": report.symbol,
                            "analysis": analysis,
                            "agent_result": None,
                            "is_new": False,
                            "form_type": report.form_type,
                            "filing_date": report.filing_date,
                            "current_until": report.current_until,
                            **report.evidence_fields(),
                            "analysis_path": report.analysis_path,
                            # No fresh XBRL fetch on the cached-read path (see
                            # EarningsReport.xbrl_facts docstring) — {} reads as
                            # "nothing to cross-check," same as fails-open.
                            "xbrl_facts": report.xbrl_facts,
                        }
                    )

        return results

    def _analyze_new(self, report: EarningsReport) -> tuple[dict | None, AgentResult]:
        """Run LLM analysis on a new filing."""
        # Check for prior analysis to provide context
        prior = ""
        symbol_dir = Path(report.analysis_path).parent

        # Sort by FILING DATE, not filename (audit round 2): names are
        # analysis_{form}_{date}.md, so a lexicographic sort ranks every
        # 10-Q above every 10-K ("Q" > "K") regardless of date — the "most
        # recent prior" could be a year-old 10-Q while last month's 10-K
        # sat ignored. Mirrors data/earnings._get_existing_analysis.
        def _filing_date_key(path: Path) -> str:
            parts = path.stem.split("_")
            return parts[-1] if parts else ""

        prior_analyses = sorted(symbol_dir.glob("analysis_*.md"), key=_filing_date_key, reverse=True)
        if prior_analyses:
            # Read the most recent prior analysis (skip current)
            for p in prior_analyses:
                if str(p) != report.analysis_path:
                    try:
                        prior = p.read_text()[:5000]
                    except OSError:
                        continue
                    break

        result = self.run(
            symbol=report.symbol,
            form_type=report.form_type,
            filing_date=report.filing_date,
            filing_text=report.text_excerpt,
            prior_analysis=prior,
        )
        parsed = result.parse_json()
        if parsed is None:
            logger.error("Earnings analyst returned non-JSON for %s", report.symbol)
            return None, result
        validated = self._validate_analysis(report, parsed, source="llm")
        if validated is None:
            return None, result
        return validated.model_dump(), result

    def _save_analysis(self, path: str, report: EarningsReport, analysis: dict):
        """Save analysis as markdown + JSON. Atomic write: tmp + rename so
        a SIGKILL mid-write can never leave a half-written file on disk.

        Without atomicity (the pre-2026-05-13 behavior), a kill between
        `p.write_text` and process exit produces a truncated markdown,
        which then fails JSON-block re-parse on next session →
        record_failure() ticks → 3 consecutive ticks abandon the filing
        permanently. The LLM succeeded but the disk write didn't is the
        worst kind of silent regression — wasted tokens AND lost thesis
        context. Same atomic-write discipline as news_store /
        macro_store / tech_store (those were already protected; earnings
        was the only outlier).
        """
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)

        # Write markdown with embedded JSON
        header = f"# {report.symbol} {report.form_type} Analysis ({report.filing_date})\n\n"
        header += f"Filing source: `{report.filing_path}`\n\n"
        header += f"## Investment Implications\n\n"
        impl = analysis.get("investment_implications", {})
        header += f"- Sentiment: {impl.get('sentiment', 'N/A')}\n"
        header += f"- Conviction: {impl.get('conviction', 'N/A')}\n"
        header += f"- Thesis: {impl.get('key_thesis', 'N/A')}\n\n"
        header += f"## Full Analysis\n\n```json\n{json.dumps(analysis, indent=2)}\n```\n"

        tmp = p.with_suffix(p.suffix + ".tmp")
        try:
            tmp.write_text(header)
            os.replace(tmp, p)
        except Exception:
            # Clean up tmp on failure so the next run doesn't see a
            # stale partial. Re-raise so the caller (manifest update)
            # doesn't proceed as if the analysis was saved.
            tmp.unlink(missing_ok=True)
            raise
        logger.info("Saved analysis for %s %s → %s", report.symbol, report.form_type, path)

    def _load_analysis(self, report: EarningsReport) -> dict | None:
        """Load previously saved analysis from markdown file."""
        text = Path(report.analysis_path).read_text()
        # Extract JSON from ```json ... ``` block
        match = re.search(r"```json\s*\n(.*?)\n```", text, re.DOTALL)
        if match:
            try:
                parsed = json.loads(match.group(1))
                validated = self._validate_analysis(report, parsed, source="cache")
                return validated.model_dump() if validated else None
            except json.JSONDecodeError:
                logger.warning("Failed to parse saved analysis: %s", report.analysis_path)
        return None

    def _validate_analysis(self, report: EarningsReport, parsed: dict | list, source: str) -> EarningsAnalysis | None:
        if not isinstance(parsed, dict):
            logger.warning("Invalid %s earnings analysis for %s: expected JSON object", source, report.symbol)
            return None

        try:
            analysis = EarningsAnalysis(**parsed)
        except ValidationError as exc:
            logger.warning("Invalid %s earnings analysis for %s: %s", source, report.symbol, exc)
            return None

        if analysis.symbol != report.symbol.upper():
            logger.warning(
                "Invalid %s earnings analysis for %s: symbol mismatch (%s)",
                source,
                report.symbol,
                analysis.symbol,
            )
            return None
        if analysis.form_type != report.form_type:
            logger.warning(
                "Invalid %s earnings analysis for %s: form mismatch (%s)",
                source,
                report.symbol,
                analysis.form_type,
            )
            return None
        if analysis.filing_date != report.filing_date:
            logger.warning(
                "Invalid %s earnings analysis for %s: filing_date mismatch (%s)",
                source,
                report.symbol,
                analysis.filing_date,
            )
            return None

        matched = self._flag_unsourced_valuation_claims(report, analysis, source)
        if matched:
            # Redact in place — the rest of the analysis (sentiment,
            # conviction, thesis, revenue/profitability figures) is sound and
            # stays untouched; only the ungrounded sentence is replaced, not
            # the whole analysis. This is what closes the loop the old
            # log-only version left open (see docstring below).
            analysis = analysis.model_copy(deep=True)
            analysis.investment_implications.reasoning_chain.valuation_context = _UNSOURCED_VALUATION_DISCLOSURE
            if source == "cache":
                # The bad claim was written to disk before this fix existed
                # (or before the prompt was corrected) and would otherwise be
                # re-served, unfixed, on every future run for the life of the
                # filing. Self-heal the cache file now so this fires once per
                # filing, not once per run forever.
                try:
                    self._save_analysis(report.analysis_path, report, analysis.model_dump())
                    logger.warning(
                        "earnings: redacted and re-saved cached analysis for %s %s "
                        "— cache no longer carries the fabricated valuation claim",
                        report.symbol,
                        report.form_type,
                    )
                except Exception:  # noqa: BLE001 — redaction in memory must
                    # still take effect even if the disk re-write fails; the
                    # next run will just try to self-heal again.
                    logger.warning(
                        "earnings: could not re-save redacted cache for %s %s "
                        "— serving the redacted version this run only",
                        report.symbol,
                        report.form_type,
                    )
        return analysis

    @staticmethod
    def _flag_unsourced_valuation_claims(report: EarningsReport, analysis: EarningsAnalysis, source: str) -> list[str]:
        """Detect a price-derived valuation claim in `valuation_context`.

        The claim is a sentence inside an otherwise sound filing read, and
        this is a text heuristic; a false positive must not cost the whole
        analysis, which is the only fundamentals input PM and
        position_reviewer get for that name. So detection here never rejects
        the analysis — the caller (`_validate_analysis`) redacts just the
        offending field instead of discarding everything.

        Runs for `source="cache"` too: analyses are written to disk once and
        re-served for the life of the filing, so an invented multiple written
        before the prompt was corrected would otherwise keep arriving at PM
        indefinitely — the caller re-saves the redacted version to close that
        loop. Returns the matched labels so tests and callers can assert on
        them.
        """
        text = analysis.investment_implications.reasoning_chain.valuation_context or ""
        if text.strip() == _UNSOURCED_VALUATION_DISCLOSURE:
            # The disclosure this detector writes CONTAINS "share price" and
            # "market cap", which are two of the patterns above, so without
            # this guard every later run matched the field on the desk's own
            # boilerplate, redacted it to the identical text and rewrote the
            # identical cache file — 106 detect/re-save pairs for two symbols
            # over five days, none of them a real fabricated claim.
            #
            # Deliberately EXACT equality, not startswith/substring: a model
            # that prepends this marker to an invented multiple must still be
            # caught. And deliberately NOT a reword of the disclosure to dodge
            # the patterns — that fix would silently come undone the day
            # someone edits either the disclosure or the pattern list.
            return []
        matched = [label for rx, label in _PRICE_DERIVED_CLAIM_RE if rx.search(text)]
        if matched:
            logger.warning(
                "earnings: %s analysis for %s %s asserts price-derived valuation "
                "(%s) in valuation_context, but the agent was given filing text "
                "only — no price, market cap or multiple. The figure is not "
                "grounded in its input; PM sizes off this field. Text: %r",
                source,
                report.symbol,
                report.form_type,
                ", ".join(matched),
                text[:220],
            )
        return matched
