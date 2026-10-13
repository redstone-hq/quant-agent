import json
import logging
import re
import time
from src.trading_calendar import utc_now
import uuid
from pathlib import Path
from typing import get_args

from pydantic import ValidationError

from src.agents.base import BaseAgent, AgentResult
from src.models import (
    NewsIntelligenceReport,
    StateChange,
    StockNewsItem,
    parse_telemetry,
)
from src.data_paths import parse_failure_dir

logger = logging.getLogger(__name__)

PROMPT_PATH = Path(__file__).parent.parent.parent / "config" / "prompts" / "news_analyst.md"

# Where a NewsIntelligenceReport parse/validation failure's raw evidence is
# dumped for offline diagnosis. `agent_logs.full_response` (see
# src/storage/db.py / src/pipeline.py's insert_agent_log call after
# NewsAnalystAgent.analyze) already carries the raw text for a SUCCESSFUL
# insert, but that write happens downstream in the caller, after several
# more lines of pipeline code that could themselves raise before reaching
# it — and it never carries the intermediate parsed-but-invalid dict, which
# is exactly the artifact that distinguishes "malformed JSON" from
# "well-formed JSON, wrong shape" (the ambiguity that blocked diagnosis of
# the 2026-09-02 four-field-missing failure). `specialist_evidence`
# (src/pipeline_stages.py:_persist_evidence) was considered and rejected:
# its own schema comment states it holds "already-VALIDATED structured
# evidence... never raw LLM prose" — reusing it for raw/invalid payloads
# would violate a documented invariant Mission Control relies on. A small
# append-only JSON-per-failure directory, written with the same atomic
# tmp+rename discipline as news_store/macro_store/tech_store/earnings_analyst
# (see earnings_analyst._save_analysis), is the simplest mechanism that is
# both durable and trivially inspectable (`ls`, `cat`, `jq`) without a DB
# migration.
PARSE_FAILURE_DIR = parse_failure_dir()


def _persist_parse_failure(
    *,
    agent_name: str,
    session: str,
    raw_text: str,
    parsed: object,
    error: str,
    affected_symbols: list[str] | None = None,
) -> None:
    """Best-effort dump of a parse/validation failure's raw evidence.

    NEVER raises. This is purely a forensic-display gap fix (mirrors
    `_persist_evidence`'s "NEVER raises" contract in pipeline_stages.py):
    a disk-write failure here (full disk, permissions, whatever) must only
    log a warning and let the ORIGINAL error/failure path continue exactly
    as it would have without this call — never mask or replace it.

    `parsed` is the JSON-decoded object at the point of failure (already
    dict/list — NOT re-serialized from raw_text) when JSON parsing itself
    succeeded but pydantic validation failed; None when JSON parsing never
    succeeded (raw_text alone is then the only evidence). Keeping both
    lets a later reader tell "the JSON was malformed" apart from "the JSON
    was well-formed but the wrong shape" — the exact ambiguity that made
    the 2026-09-02 four-missing-fields failure undiagnosable from the log
    line alone.

    `affected_symbols` (board item 152) names the stocks that lose this seat
    because of this failure — the symbols the seat was shown real headline
    text for. Recorded here so the dump answers "which names are now missing
    their news seat, and why" on its own, rather than leaving a later reader
    to reconstruct it from a rotated log line and a count.
    """
    try:
        PARSE_FAILURE_DIR.mkdir(parents=True, exist_ok=True)
        ts = utc_now().strftime("%Y%m%dT%H%M%SZ")
        fname = f"{agent_name}_{session}_{ts}_{uuid.uuid4().hex[:8]}.json"
        path = PARSE_FAILURE_DIR / fname
        payload = {
            "agent_name": agent_name,
            "session": session,
            "timestamp": ts,
            "error": error,
            "raw_text": raw_text,
            "parsed": parsed,
            "affected_symbols": sorted(affected_symbols or []),
        }
        tmp_path = path.with_suffix(".tmp")
        tmp_path.write_text(json.dumps(payload, indent=2, default=str))
        tmp_path.rename(path)  # atomic — see earnings_analyst._save_analysis
    except Exception as e:  # noqa: BLE001 — capture must never mask the real failure
        logger.warning(
            "Failed to persist news_analyst parse-failure evidence (session=%s): %s",
            session,
            e,
        )


# Tokens too common to anchor an event on — they'd let any hallucinated event
# survive a keyword match. Deliberately conservative: we only want to exclude
# words that appear in virtually any headline.
from src.news_state_change_words import _STATE_CHANGE_STOPWORDS  # noqa: E402,F401 -- lifted verbatim


class NewsAnalystAgent(BaseAgent):
    # See analyze(): parsed JSON is validated as NewsIntelligenceReport(**parsed).
    result_model = NewsIntelligenceReport

    @property
    def name(self) -> str:
        return "news_analyst"

    @property
    def system_prompt(self) -> str:
        if PROMPT_PATH.exists():
            return PROMPT_PATH.read_text()
        return "You are a news intelligence analyst. Respond with JSON."

    # Per-session mode descriptor that shapes the agent's task. Morning
    # does the full 3-layer build; midday focuses on DELTA vs morning
    # (what's new/changed); evening focuses on SUMMARY (what stuck vs
    # faded across the day). All three still emit the same schema so
    # downstream consumers don't care which mode produced the report.
    _SESSION_GUIDANCE = {
        "morning": (
            "MORNING mode — full 3-layer build. Treat today as a fresh book; "
            "produce the complete macro_narrative, state_changes, and stock_news "
            "sections. This report sets the tone for the day's trading."
        ),
        "midday": (
            "MIDDAY mode — DELTA focus. The morning report is shown below as "
            "'This morning's snapshot'. Your job is to surface what CHANGED "
            "since morning: new state changes, resolved state changes, fresh "
            "stock catalysts. Keep sections that haven't changed brief (one "
            "line saying 'unchanged from morning'). Prioritize HIGH-conviction "
            "developments touching held symbols."
        ),
        # audit round 2 #24: the close session (15:30 ET) had no entry and
        # silently fell back to MORNING guidance — mislabeling the run as a
        # "fresh book" full rebuild 30 minutes before the bell.
        "close": (
            "CLOSE mode — DELTA focus, ~30 minutes to the bell. The most "
            "recent prior snapshot (midday or morning) may be shown below as "
            "the baseline. Surface what CHANGED since that snapshot: new or "
            "reversing state changes and fresh stock catalysts that could "
            "trigger an exit before the close or move held positions "
            "overnight. Keep unchanged sections to one line ('unchanged "
            "since midday'). Prioritize HIGH-conviction developments "
            "touching held symbols."
        ),
        "evening": (
            "EVENING mode — SUMMARY focus. Two prior snapshots (morning, midday) "
            "may be shown below. Synthesize: which narratives STUCK (confirmed "
            "by the day's price action) vs FADED (initial interpretation didn't "
            "hold). macro_narrative should reflect where the market ACTUALLY is "
            "at end-of-day, not the morning hypothesis. state_changes should "
            "include events that closed/resolved today. This report becomes "
            "tomorrow's 'previous_narrative' — be the history you want PM to read."
        ),
        # 2026-09-23, the same defect as round 2 #24 above, in the other
        # direction: `intra_check` had no entry, so the intraday re-read
        # fell back to MORNING guidance and was told to "treat today as a
        # fresh book" at, say, 14:00. It is reached only from the seat-heal
        # re-ask, which fires when a market-moving headline supersedes the
        # research the desk is holding. That re-ask is handed the wire text
        # alone — no universe, no prior snapshot — and this text says so
        # rather than letting the model assume it has a baseline it does
        # not have.
        "intra_check": (
            "INTRA-CHECK mode — a mid-session re-read, triggered because a "
            "market-moving headline superseded the research this desk was "
            "holding. The market is OPEN and today's book already exists; "
            "this is NOT a fresh-book rebuild and must not be written as "
            "one. Work from exactly what is supplied below and do not "
            "claim to be diffing against a baseline unless one is actually "
            "shown: report what the wire in front of you says, and say "
            "plainly where it leaves you without enough context to judge."
        ),
    }

    #: Used when a session has no entry above. Deliberately makes no claim
    #: about what part of the day this is or what baseline exists, because
    #: the failure this replaces was a WRONG claim ("treat today as a fresh
    #: book", at 14:00), not a missing one.
    _UNKNOWN_SESSION_GUIDANCE = (
        "This run's session mode is not one this prompt has specific "
        "guidance for. Do not assume it is a start-of-day full rebuild and "
        "do not assume a prior snapshot is available: work from exactly "
        "what is supplied below, and say plainly where you lack the context "
        "to judge."
    )

    def build_user_message(self, **kwargs) -> str:
        news_text: str = kwargs["news_text"]
        universe: list[str] = kwargs.get("universe", [])
        stock_mentions: dict[str, list] = kwargs.get("stock_mentions", {})
        previous_narrative: dict | None = kwargs.get("previous_narrative")
        session: str = kwargs.get("session", "morning")
        prior_session_report: dict | None = kwargs.get("prior_session_report")
        # NewsCoverage (src/data/news.py) — how many of the configured wire
        # feeds actually returned data this run. 2026-08-28: two dead feeds
        # (Reuters 404, AP 403) were dropped with a log warning and the
        # model never saw a hint that anything was missing, so it had no
        # way to distinguish "wires quiet" from "wires unreachable". This
        # section is the fix on the prompt side — see NewsCoverage.describe
        # for the deterministic half and the "Feed coverage" guardrail in
        # config/prompts/news_analyst.md for what the model does with it.
        news_coverage = kwargs.get("news_coverage")

        universe_text = ", ".join(universe) if universe else "N/A"

        # Session-specific guidance
        # NOT `.get(session, morning)`. That default silently mislabelled the
        # close session as a fresh-book rebuild (audit round 2 #24) and then
        # did the identical thing to `intra_check` (2026-09-23) — twice is a
        # mechanism, not two accidents, and the mechanism is that a session
        # with no entry LOOKS like morning instead of looking wrong. An
        # unknown session now gets a neutral entry that claims nothing about
        # what this run is, and says so out loud in the log.
        # `tests/test_seat_heal_wiring.py` pins every SessionType value that
        # can reach this analyst against the table.
        guidance = self._SESSION_GUIDANCE.get(session)
        if guidance is None:
            logger.warning(
                "news_analyst: no session guidance for session=%r; using the "
                "neutral entry rather than defaulting to MORNING",
                session,
            )
            guidance = self._UNKNOWN_SESSION_GUIDANCE
        session_section = f"## Session Mode\n{guidance}\n"

        # Prior snapshot for midday/evening — lets the agent diff/summarize
        # rather than rebuild from scratch.
        if prior_session_report and session != "morning":
            prior_briefing = (prior_session_report.get("pm_briefing") or "")[:500]
            # `or "?"` not `.get(..., "?")`: since board item 152 the stored
            # key can be present and null (the prior session's own sentiment
            # was unreadable and dropped), which would otherwise render the
            # literal "None" into the prompt as if it were a verdict.
            prior_sentiment = prior_session_report.get("market_sentiment") or "?"
            prior_state_changes = prior_session_report.get("state_changes") or []
            sc_lines = [
                f"- [{sc.get('conviction', '?').upper()}] {sc.get('event', '')}: "
                f"{sc.get('previous_state', '')} → {sc.get('new_state', '')}"
                for sc in prior_state_changes[:5]
            ]
            sc_text = "\n".join(sc_lines) or "(none)"
            prior_section = f"""## Prior Session Snapshot (use as baseline for your delta/summary)
Sentiment at prior session: {prior_sentiment}
PM Briefing: {prior_briefing}
State changes captured earlier:
{sc_text}
"""
        else:
            prior_section = ""

        # Previous macro narrative section (evolves slowly across days)
        if previous_narrative:
            narrative_section = f"""## Previous Macro Narrative (update if needed, keep if unchanged)

```json
{json.dumps(previous_narrative, indent=2)}
```
"""
        else:
            narrative_section = (
                "## Previous Macro Narrative\nNo previous narrative. Build one from scratch using today's news.\n"
            )

        # Stock-specific news section.
        # audit round 2 #12: the loop used to discard the symbol key and,
        # because tag_symbol_mentions files a multi-symbol headline under
        # EVERY matching ticker, render the same headline N times with no
        # attribution. Invert to item → [symbols]: each headline renders
        # once, prefixed with the tickers it was tagged for.
        if stock_mentions:
            grouped: dict[tuple, list[str]] = {}
            item_by_key: dict[tuple, object] = {}
            order: list[tuple] = []
            for symbol, items in sorted(stock_mentions.items()):
                for item in items[:5]:  # max 5 per symbol
                    key = (getattr(item, "source", ""), getattr(item, "title", str(item)))
                    if key not in grouped:
                        grouped[key] = []
                        item_by_key[key] = item
                        order.append(key)
                    if symbol not in grouped[key]:
                        grouped[key].append(symbol)
            stock_lines = []
            for key in order:
                source, title = key
                syms = ", ".join(grouped[key])
                # Same reason as the general-news block: if one syndicated
                # story was collapsed into this item, say so explicitly and
                # say what it does not mean. Silence here would let the model
                # read a single widely-carried story as a single small one.
                item = item_by_key[key]
                collapsed = getattr(item, "collapsed_count", 1) or 1
                breadth = ""
                if collapsed > 1:
                    n_src = getattr(item, "source_count", 1) or 1
                    breadth = (
                        f" [carried by {n_src} outlet{'s' if n_src != 1 else ''}"
                        f", {collapsed} articles - syndication breadth, "
                        f"NOT independent corroboration]"
                    )
                stock_lines.append(f"  [{source}] ({syms}) {title}{breadth}")
                summary = getattr(item_by_key[key], "summary", "")
                if summary:
                    stock_lines.append(f"    > {summary[:200]}")
            shown = ", ".join(sorted({str(s).strip().upper() for s in stock_mentions if str(s).strip()}))
            stock_section = (
                "## Stock-Specific News (mentions of universe symbols)\n\n"
                + "\n".join(stock_lines)
                + "\n\n## Shown symbols (must each be a key in stock_news)\n"
                + shown
                + "\nEvery ticker listed here was tagged in the headlines above. "
                "Emit a `stock_news` key for each. If the mention is incidental "
                'or not decision-relevant, emit `"TICKER": []` — an empty list. '
                "Do not invent headlines. Do not omit a key."
            )
        else:
            stock_section = "## Stock-Specific News\nNo universe symbols detected in today's headlines."

        from src.trading_calendar import session_date_key

        today = session_date_key()

        # Coverage section — always present, even when coverage is full,
        # so its absence never has to be interpreted as "coverage was fine"
        # by anyone reading a saved prompt after the fact. `news_coverage`
        # is None only for a caller that hasn't been updated to pass it
        # (defensive; every production call site does).
        if news_coverage is not None:
            coverage_section = f"## News Coverage\n{news_coverage.describe()}\n"
        else:
            coverage_section = (
                "## News Coverage\nNews coverage: UNKNOWN (caller did not report "
                "feed coverage). Treat with the same caution as a reported gap.\n"
            )

        return f"""## Today's Date: {today}

{session_section}
{coverage_section}
{prior_section}
{narrative_section}

## General News (last 24 hours)

{news_text}

{stock_section}

## Trading Universe
{universe_text}

Analyze all the above and produce your intelligence report as JSON."""

    @staticmethod
    def _extract_event_keywords(event: str) -> set[str]:
        """Lowercase 4+ letter tokens that aren't generic structural words.

        Used to check whether a state_change.event is actually supported by
        the input headlines. Four-letter floor keeps out a/an/is/on/etc.
        without excluding meaningful short acronyms (we accept the false-drop
        risk on a 3-letter event for the false-accept-safety).
        """
        tokens = re.findall(r"[A-Za-z]{4,}", event.lower())
        return {t for t in tokens if t not in _STATE_CHANGE_STOPWORDS}

    @classmethod
    def _filter_hallucinated_state_changes(
        cls,
        report: NewsIntelligenceReport,
        news_text: str,
        prior_session_report: dict | None = None,
    ) -> NewsIntelligenceReport:
        """Drop state_changes whose event keywords do not appear in the input
        headlines — a rough but effective guard against LLM-invented narrative
        shifts ("Iran ceasefire" when the input only had Fed news).

        A state_change is kept when either:
          - any extracted event keyword appears in the headlines text, OR
          - any ticker in `affected_symbols` appears in the headlines text, OR
          - the event has no extractable keywords AND no affected_symbols
            (can't verify either way — keep rather than silently drop), OR
          - the event was already present in `prior_session_report`
            state_changes (the reason midday/evening passes prior_session is
            to let the model carry forward / resolve a morning event even
            when fresh headlines don't repeat it verbatim).

        Matching: event keywords are case-insensitive substring (safe — the
        4-char floor + stopwords already exclude generic tokens); ticker
        symbols are whole-token matches (audit round 2 #25 — 1-2 letter
        tickers are substrings of almost anything). Not perfect for
        paraphrasing, but dropping a correctly-interpreted-but-reworded
        change is far less costly than letting a fabricated change reach
        PM sizing logic.
        """
        if not report.state_changes or not news_text:
            return report

        text_lower = news_text.lower()
        # audit round 2 #25: symbol hits must be whole-token matches, not raw
        # substrings — universe tickers like V / MA / GE are substrings of
        # virtually any headline blob ("nvidia" contains "v"), which let a
        # fabricated state_change tagged with a short ticker sail through
        # this filter. Mirrors the word-boundary discipline of
        # src/data/news.py:tag_symbol_mentions.
        text_tokens = set(re.findall(r"[a-z0-9.\-]+", text_lower))
        # Build a supplementary token pool from the prior session's
        # state_changes so events carried forward across sessions survive.
        prior_tokens: set[str] = set()
        prior_symbols: set[str] = set()
        if prior_session_report:
            for psc in prior_session_report.get("state_changes") or []:
                event = psc.get("event") if isinstance(psc, dict) else None
                if event:
                    prior_tokens.update(cls._extract_event_keywords(event))
                syms = psc.get("affected_symbols") if isinstance(psc, dict) else None
                for s in syms or []:
                    if s:
                        prior_symbols.add(s.lower())
        kept: list = []
        dropped: list[str] = []
        for sc in report.state_changes:
            event_kws = cls._extract_event_keywords(sc.event)
            affected = [s for s in (sc.affected_symbols or []) if s]
            symbol_hits = [s for s in affected if s.lower() in text_tokens]
            kw_hits = [k for k in event_kws if k in text_lower]
            prior_kw_hit = bool(event_kws & prior_tokens)
            prior_sym_hit = any(s.lower() in prior_symbols for s in affected)

            if kw_hits or symbol_hits or prior_kw_hit or prior_sym_hit:
                kept.append(sc)
            elif not event_kws and not affected:
                # Nothing verifiable either way — err on keep.
                kept.append(sc)
            else:
                dropped.append(sc.event[:80])

        if dropped:
            logger.warning(
                "news_analyst: dropped %d state_change(s) whose event "
                "keywords and affected_symbols are absent from the input "
                "headlines — likely hallucination: %s",
                len(dropped),
                dropped,
            )
            return report.model_copy(update={"state_changes": kept})
        return report

    def analyze(
        self,
        news_text: str,
        universe: list[str] | None = None,
        stock_mentions: dict | None = None,
        previous_narrative: dict | None = None,
        session: str = "morning",
        prior_session_report: dict | None = None,
        news_coverage=None,
        _retry_used: bool = False,
    ) -> tuple[NewsIntelligenceReport | None, AgentResult]:
        """`_retry_used` is a per-CALL flag, not instance state (item 152,
        2026-09-25): the earlier version set `self._heal_retry_used = True`
        on the agent instance and never cleared it. `self.news_analyst` is a
        single long-lived instance reused across every session in the
        scheduler's `while True` loop (src/pipeline.py), so the FIRST
        validation-failure-then-retry of the whole run permanently disabled
        the one paid heal retry for every later, unrelated failure on any
        later session — silently reducing the seat's own recovery rate over
        time. Threading it as a recursion argument scopes it to this one
        answer, matching every other retry in this file.
        """
        result = self.run(
            news_text=news_text,
            universe=universe or [],
            stock_mentions=stock_mentions or {},
            previous_narrative=previous_narrative,
            session=session,
            prior_session_report=prior_session_report,
            news_coverage=news_coverage,
        )
        parsed = result.parse_json()
        if parsed is None:
            # `parse_json()` already scans every `{`/`[` in the raw text for
            # ANY complete, well-formed JSON fragment (AgentResult.parse_json,
            # src/agents/base.py) before giving up — so None here means the
            # answer contained no parseable JSON substring at all (a genuine
            # non-answer, e.g. "I need more context...", confirmed against
            # the three retained `data/parse_failures/news_analyst_*.json`
            # forensic dumps, all identical). There is nothing left to
            # salvage per-entry in that case; unlike the validation-failure
            # branch below, this path previously never retried at all before
            # giving up, so a transient non-answer (rate limit page, a
            # truncated stream) was never given the SAME one paid heal
            # retry the schema-failure branch already gets. Give it parity.
            if _retry_used:
                logger.error("News analyst returned non-JSON response after one heal retry")
                _persist_parse_failure(
                    agent_name=self.name,
                    session=session,
                    raw_text=result.raw_text,
                    parsed=None,
                    error="non-JSON response (parse_json() returned None)",
                    affected_symbols=self._affected_symbols(stock_mentions, universe),
                )
                return None, result
            logger.warning("News analyst returned non-JSON response; one paid heal retry")
            return self.analyze(
                news_text,
                universe=universe,
                stock_mentions=stock_mentions,
                previous_narrative=previous_narrative,
                session=session,
                prior_session_report=prior_session_report,
                news_coverage=news_coverage,
                _retry_used=True,
            )
        # Per-entry isolation: a single malformed StockNewsItem (e.g. empty
        # headline) or StateChange (e.g. bad conviction enum) must not drop
        # the WHOLE news report — that report carries macro_narrative,
        # pm_briefing, and the rest of state_changes / stock_news that
        # PM relies on to brief the morning. Mirrors EveningAnalyst.
        # _drop_invalid_missed_opportunities (PR #73) and the
        # TechAnalyst.analyze_batch isolate-failures-by-symbol discipline.
        parsed = self._drop_invalid_state_changes(parsed)
        parsed = self._drop_invalid_stock_news(parsed)
        # HEAL FIRST, DROP ONLY AS FALLBACK (docs/OUTCOME.md order). An
        # unreadable top-level field is a reason to ask the seat AGAIN, not
        # a reason to give up on the field: the desk has already paid for
        # the wire text and the prompt, and a second ask may well come back
        # with a legal word. Only when that paid re-ask has been spent and
        # the value is STILL unreadable does the field get dropped to save
        # the rest of the report. Dropping first would have spent the one
        # paid heal retry that shipped the day before (#695) on exactly the
        # five failures this change targets, buying a permanent absence
        # with a re-ask the desk never made.
        parsed, unreadable_fields = self._drop_invalid_market_sentiment(parsed)
        if unreadable_fields and not _retry_used:
            logger.warning(
                "News analyst: unreadable top-level field(s) %s; one paid heal retry before dropping anything",
                ", ".join(f"{k}={v!r}" for k, v in sorted(unreadable_fields.items())),
            )
            return self.analyze(
                news_text,
                universe=universe,
                stock_mentions=stock_mentions,
                previous_narrative=previous_narrative,
                session=session,
                prior_session_report=prior_session_report,
                news_coverage=news_coverage,
                _retry_used=True,
            )
        try:
            report = NewsIntelligenceReport(**parsed)
        except Exception as e:
            if _retry_used:
                # "failed to parse" (not "failed parse") so this final,
                # exhausted-retry failure is classified by log_health's
                # `seat_answer_unreadable` family instead of silently
                # falling into the unrecognised bucket — the same gap #538
                # closed for the technical seat's own final-failure line.
                logger.error("News analysis failed to parse after one heal retry: %s", e)
                _persist_parse_failure(
                    agent_name=self.name,
                    session=session,
                    raw_text=result.raw_text,
                    parsed=parsed,
                    error=str(e),
                    affected_symbols=self._affected_symbols(stock_mentions, universe),
                )
                return None, result
            logger.warning("News analysis failed to parse (%s); one paid heal retry", e)
            return self.analyze(
                news_text,
                universe=universe,
                stock_mentions=stock_mentions,
                previous_narrative=previous_narrative,
                session=session,
                prior_session_report=prior_session_report,
                news_coverage=news_coverage,
                _retry_used=True,
            )
        report.unreadable_fields = unreadable_fields
        report = self._filter_hallucinated_state_changes(
            report,
            news_text,
            prior_session_report=prior_session_report,
        )
        report.dropped_news_symbols = self._find_dropped_news_symbols(
            stock_mentions=stock_mentions,
            report=report,
        )
        # Complete the structured map without inventing headlines: a shown
        # symbol the seat omitted becomes an explicit empty list, so
        # downstream consumers iterating `stock_news` keys see UNCOVERED
        # rather than silence. `dropped_news_symbols` still names the
        # omission so data_status stays `symbol_dropped` and PM/risk do
        # not read the empty list as "no news today".
        if report.dropped_news_symbols:
            filled = dict(report.stock_news)
            for sym in report.dropped_news_symbols:
                filled.setdefault(sym, [])
            report.stock_news = filled
        return report, result

    @staticmethod
    def _affected_symbols(stock_mentions: dict | None, universe: list[str] | None) -> list[str]:
        """Which stocks lose this seat when the whole answer is unreadable.

        Board item 152. `stock_mentions` is the deterministic, pre-LLM
        word-boundary match over real wire text (`NewsDataProvider.
        tag_symbol_mentions`), so its keys are exactly the names the seat was
        shown real headline content for — the ones whose news seat is now
        absent. Falls back to the requested universe when the caller passed no
        mentions map, which is still the honest "asked about" set; returns []
        when neither is known rather than guessing.
        """
        if stock_mentions:
            return sorted({str(s).strip().upper() for s in stock_mentions if str(s).strip()})
        return sorted({str(s).strip().upper() for s in (universe or []) if str(s).strip()})

    @staticmethod
    def _drop_invalid_market_sentiment(parsed: dict) -> tuple[dict, dict[str, str]]:
        """Drop an out-of-vocabulary `market_sentiment` instead of losing the report.

        Board item 152, measured. `market_sentiment` is a three-word Literal
        and it is the field the seat actually gets wrong: 5 of the 7
        reproducible news-seat parse failures in the retained production logs
        are this one field ("mixed" x4 on 2026-08-25/27/27/28,
        "mixed-to-bearish" on 2026-08-21), and each one discarded a whole
        otherwise-valid report. Same shape as
        `_drop_invalid_state_changes` / `_drop_invalid_stock_news` and as the
        technical seat's row-by-row salvage (#538): drop the unreadable unit,
        keep everything else.

        The dropped value is NOT mapped onto a legal one. "mixed" is not
        "neutral", and inventing a verdict the seat never gave is worse than
        having none — the field reads ABSENT and the raw word is returned so
        the report can carry WHY it is absent. Returns (parsed, unreadable).

        This is the FALLBACK, not the first response: `analyze()` spends the
        one paid heal retry on an unreadable value first and only keeps this
        drop when the re-ask comes back unreadable too.
        """
        unreadable: dict[str, str] = {}
        if "market_sentiment" not in parsed:
            return parsed, unreadable
        raw = parsed.get("market_sentiment")
        if raw is None:
            return parsed, unreadable
        # Read the legal words off the model itself so this can never drift
        # from the declaration. The annotation is `Literal[...] | None`, so
        # flatten one level and keep the string members.
        legal = tuple(
            member
            for arg in get_args(
                NewsIntelligenceReport.model_fields["market_sentiment"].annotation,
            )
            for member in get_args(arg)
            if isinstance(member, str)
        )
        if isinstance(raw, str) and raw.strip().lower() in legal:
            parsed["market_sentiment"] = raw.strip().lower()
            return parsed, unreadable
        logger.warning(
            "News analyst: market_sentiment %r is not a word the desk can "
            "read — the seat's sentiment reads ABSENT, not neutral",
            raw,
        )
        parsed = dict(parsed)
        parsed.pop("market_sentiment")
        unreadable["market_sentiment"] = str(raw)
        return parsed, unreadable

    @staticmethod
    def _find_dropped_news_symbols(
        *,
        stock_mentions: dict | None,
        report: NewsIntelligenceReport,
    ) -> list[str]:
        """Which requested symbols the seat's own answer omits — PM TEST GATE
        item 4, second half.

        `stock_mentions` is `NewsDataProvider.tag_symbol_mentions`'s output:
        real headline/summary text the model was actually shown for that
        symbol, matched deterministically (word-boundary regex) BEFORE the
        LLM ever ran. It is the "what was asked for" half of the comparison.
        `report.stock_news` keys are the "what came back" half.

        A symbol present in `stock_mentions` but absent from `stock_news` is
        recorded here. An explicit empty list (`"BAC": []`) is an answer —
        uncovered / incidental, no headlines invented — and is not a drop.
        A missing key is the drop. This is a presence/absence check, not a
        threshold: no count or ratio decides anything, a single missing key
        is enough. The prompt now requires a key for every shown symbol;
        this check still catches a key the model omitted, and `analyze()`
        then fills `[]` so the structured map is complete without inventing
        a headline or a sentiment.
        """
        requested = {str(s).strip().upper() for s in (stock_mentions or {}) if str(s).strip()}
        answered = {str(s).strip().upper() for s in (report.stock_news or {})}
        dropped = sorted(requested - answered)
        for sym in dropped:
            parse_telemetry.record_dropped_item("StockNewsItem", sym)
        if dropped:
            logger.error(
                "News analyst: seat's answer is missing %d requested "
                "symbol(s) that had real headline coverage — treat as LOST, "
                "not as absence of news: %s",
                len(dropped),
                dropped,
            )
        return dropped

    @staticmethod
    def _drop_invalid_state_changes(parsed: dict) -> dict:
        """Pre-validate each StateChange; drop malformed entries with a warning.

        StateChange has Literal validation on `conviction`; a typo in one
        item must not poison the whole report. Mutates parsed in place
        for `state_changes`; non-list shapes are normalized to [].
        """
        raw = parsed.get("state_changes")
        if raw is None:
            return parsed
        if not isinstance(raw, list):
            logger.warning(
                "News analyst: state_changes is %s, not list — replacing with []",
                type(raw).__name__,
            )
            parsed["state_changes"] = []
            return parsed
        valid: list[dict] = []
        for i, item in enumerate(raw):
            if not isinstance(item, dict):
                logger.warning(
                    "News analyst: dropping non-dict state_changes entry at index %d: %r",
                    i,
                    item,
                )
                continue
            try:
                StateChange(**item)
            except ValidationError as e:
                event = item.get("event") or f"<idx {i}>"
                logger.warning(
                    "News analyst: dropping malformed state_change %r: %s",
                    event,
                    e,
                )
                continue
            valid.append(item)
        parsed["state_changes"] = valid
        return parsed

    @staticmethod
    def _drop_invalid_stock_news(parsed: dict) -> dict:
        """Pre-validate each StockNewsItem under each symbol bucket.

        `stock_news` is a dict[str, list[StockNewsItem]]. A single item
        with an empty headline (the most common LLM glitch) currently
        kills the whole NewsIntelligenceReport — including macro_narrative
        and pm_briefing, which PM needs even if a single per-symbol
        bullet is malformed. Drop bad items per-symbol. An explicit empty
        list from the model (`"TICKER": []`) is kept: that is the uncovered
        / incidental marker, not an omission. If the model emitted items
        and every one was malformed, drop the key so `_find_dropped_news_symbols`
        can flag the loss.
        """
        raw = parsed.get("stock_news")
        if raw is None:
            return parsed
        if not isinstance(raw, dict):
            logger.warning(
                "News analyst: stock_news is %s, not dict — replacing with {}",
                type(raw).__name__,
            )
            parsed["stock_news"] = {}
            return parsed
        cleaned: dict[str, list[dict]] = {}
        for sym, items in raw.items():
            if not isinstance(items, list):
                logger.warning(
                    "News analyst: stock_news[%s] is %s, not list — dropping",
                    sym,
                    type(items).__name__,
                )
                continue
            valid: list[dict] = []
            for i, item in enumerate(items):
                if not isinstance(item, dict):
                    logger.warning(
                        "News analyst: dropping non-dict stock_news entry under %s at index %d: %r",
                        sym,
                        i,
                        item,
                    )
                    continue
                try:
                    StockNewsItem(**item)
                except ValidationError as e:
                    headline = (item.get("headline") or f"<idx {i}>")[:80]
                    logger.warning(
                        "News analyst: dropping malformed stock_news entry under %s (%s): %s",
                        sym,
                        headline,
                        e,
                    )
                    continue
                valid.append(item)
            if valid:
                cleaned[sym] = valid
            elif not items:
                # Model emitted an explicit empty list: shown, not
                # decision-relevant, no headlines invented. Keep the key
                # so this is distinct from omitting the symbol entirely.
                cleaned[sym] = []
        parsed["stock_news"] = cleaned
        return parsed
