from typing import Annotated, Literal
from pydantic import field_validator, model_validator
from pydantic.json_schema import SkipJsonSchema
from src.quantities import collapse_stances
from src.models.base import LLMOutputModel, _normalize_enum_case_fields
from src.models.analysis import (
    AnalystVerdict,
    NO_STATED_STRENGTH,
    Nomination,
    VerdictEvidence,
    _sanitize_nominations_field,
)
from src.models.macro import MacroNarrative, _AsWireRows, _rows_to_map


class SymbolDirectionRow(LLMOutputModel):
    """Wire shape of one `symbol_direction` entry: the AI fills a list of
    these (a strict schema cannot express a ticker-keyed map)."""

    symbol: str
    direction: Literal["bullish", "bearish", "neutral"]


class StockNewsRow(LLMOutputModel):
    """Wire shape of one `stock_news` entry: one row per stock. An empty
    `items` list is the explicit "shown, nothing to report" marker."""

    symbol: str
    items: list["StockNewsItem"]


class StateChange(LLMOutputModel):
    event: str
    previous_state: str
    new_state: str
    market_impact: str
    affected_symbols: list[str] = []
    conviction: Literal["high", "medium", "low"]
    # Phase 13 catalyst-gate fix (2026-09-03): per-symbol direction for
    # THIS state change, keyed by symbols named in `affected_symbols`.
    # NOT a single scalar — `market_impact` is free text that routinely
    # names OPPOSITE directions for different symbols in the same row
    # (see the ceasefire/oil example in config/prompts/news_analyst.md:
    # "Bullish for consumer discretionary and airlines, bearish for
    # energy" over one row naming both XLY and XLE names). A scalar
    # direction would misrepresent exactly the rows most likely to
    # matter for this.
    #
    # Populated by the SAME news_analyst LLM call that already produces
    # `StockNewsItem.sentiment` for individual stock items — no new LLM
    # call, no new analyst seat. A symbol absent from this dict has no
    # recorded direction; `PortfolioManagerAgent._catalyst_cites_state_
    # change` (src/agents/portfolio_manager.py) treats that the same as
    # an explicit "neutral": it does not qualify for the sub-floor
    # catalyst exception. Fail closed, matching the rest of that gate.
    symbol_direction: Annotated[
        dict[str, Literal["bullish", "bearish", "neutral"]], _AsWireRows(SymbolDirectionRow)
    ] = {}

    @model_validator(mode="before")
    @classmethod
    def _normalize_enum_case(cls, values):
        values = _normalize_enum_case_fields(values, lower_fields=("conviction",))
        if isinstance(values, dict) and "symbol_direction" in values:
            values = {**values, "symbol_direction": _rows_to_map(values["symbol_direction"], "symbol", "direction")}
        if isinstance(values, dict):
            raw = values.get("symbol_direction")
            if isinstance(raw, dict):
                cleaned: dict[str, str] = {}
                for sym, direction in raw.items():
                    if not isinstance(sym, str) or not isinstance(direction, str):
                        continue
                    d = direction.strip().lower()
                    # Unrecognized directions are DROPPED, not raised —
                    # one malformed entry must narrow what can be cited,
                    # never crash the whole news report (same posture as
                    # `_normalize_enum_case_fields` above). A dropped
                    # entry is indistinguishable from "no direction
                    # recorded" downstream, which is exactly the fail-
                    # closed behavior wanted.
                    if d not in ("bullish", "bearish", "neutral"):
                        continue
                    s = sym.strip().upper()
                    if s:
                        cleaned[s] = d
                values = {**values, "symbol_direction": cleaned}
        return values


class StockNewsItem(LLMOutputModel):
    headline: str
    sentiment: Literal["bullish", "bearish", "neutral"]
    conviction: Literal["high", "medium", "low"]
    impact_summary: str

    @field_validator("headline")
    @classmethod
    def require_headline(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("headline cannot be empty")
        return v

    @model_validator(mode="before")
    @classmethod
    def _normalize_enum_case(cls, values):
        return _normalize_enum_case_fields(
            values,
            lower_fields=("sentiment", "conviction"),
        )


#: `news_verdict_for_symbol`'s conviction -> `AnalystVerdict.magnitude` map.
#: Judgment call (flagged for review, Phase 13 §13.3 posture — start equal,
#: adjust only on out-of-sample proof): a `StockNewsItem` carries no numeric
#: field at all, only a three-rung conviction, so unlike
#: `rating_rung_strength` (which has a true neutral rung at 0.0 to anchor
#: against) there is nothing to equally space AROUND. Spacing the three
#: rungs equally across (0, 1] — never landing on 0.0, which the
#: `AnalystVerdict` validator reserves for a real neutral read — keeps "low
#: conviction" a genuine (if weak) lean instead of a silent no-lean that
#: would rank identically to a symbol nobody covered.
#: DELETED 2026-09-13 (retired item 31): `NEWS_CONVICTION_MAGNITUDE`,
#: {low 0.33, medium 0.67, high 1.0}. It was a table on `conviction` — the
#: same field this verdict already reports as `conviction` — so
#: `score_verdict`'s two-signal composite counted news's conviction twice, at
#: a spacing nothing stood behind. Magnitude is now `NO_STATED_STRENGTH`
#: (0.0 — this seat has no strength scale of its own and does not borrow
#: Technical's); the conviction it was derived from is unchanged and still
#: carried, and is what reaches the ranking.

#: Same ordinal `_CONVICTION_RANK` idea as `src/nominations.py` and
#: `CONVICTION_SCORE` in `src/verdicts.py` (low < medium < high), kept as a
#: private copy here rather than imported: both of those modules import
#: `src.models`, so importing either back would be circular. This is a
#: single `max()` key, not a quantity computed and compared across files, so
#: it does not trip the one-definition guard's arithmetic-shape matching —
#: see `tests/test_one_definition_guard.py`.
_NEWS_CONVICTION_RANK: dict[str, int] = {"low": 0, "medium": 1, "high": 2}

#: Evidence cap for `news_verdict_for_symbol` — see its docstring.
_MAX_NEWS_EVIDENCE_ITEMS = 5


def news_verdict_for_symbol(symbol: str, items: list["StockNewsItem"]) -> "AnalystVerdict":
    """Collapse every `StockNewsItem` the News seat filed for one symbol
    into the one `AnalystVerdict` the Portfolio Manager compares seats by.

    Precondition: `items` is the list filed under one `stock_news` key.
    An empty list is the uncovered marker (`analyze()` fills `[]` for a
    shown symbol the seat omitted, and the model may emit `[]` for an
    incidental mention). Callers that build ranking verdicts skip empty
    lists so UNKNOWN is not collapsed into a fake neutral lean; this
    function itself fails soft (neutral, no lean) rather than raising.

    **direction** — the collapsed sentiment across every item, via
    `src.quantities.collapse_stances` — the SAME reduction
    `PortfolioManagerAgent.build_evidence_registry` already applies to
    `(i.sentiment for i in items)` (see `PortfolioManagerAgent.
    _collapse_stances`, now a thin wrapper over the same function). Reusing
    it rather than inventing a second disagreement rule is deliberate: a
    verdict and a registry stance about the same symbol must never resolve
    a three-way sentiment split differently. `collapse_stances` returns
    "mixed" for any unresolved disagreement (including a directional
    sentiment sitting alongside a "neutral" one) — that is treated as
    neutral here, same as an empty `items` list: an unresolved split is the
    absence of a call, not a third direction `AnalystVerdict` has no room
    for.

    **conviction** — JUDGMENT CALL, flagged for review. `collapse_stances`
    decides the winning DIRECTION but says nothing about conviction, so:
    among the items whose own `sentiment` agrees with the final collapsed
    direction, take the HIGHEST conviction. Rationale: an item that
    disagreed with the eventual call was outvoted and its confidence in the
    losing side is not evidence for how strongly to hold the winning one;
    among the items that agree, the most confident one is the strongest
    stated support the desk actually has for this call. A neutral verdict
    (collapse resolved to neutral, or resolved to "mixed", or `items` was
    empty) has no winning side to draw a conviction from, so it defaults to
    "low" — the weakest assertion the scale offers, since there is
    nothing here to be confident ABOUT.

    **magnitude** — `NO_STATED_STRENGTH` (None), directional or not. A news
    item states a sentiment and a conviction, and nothing else about how far
    it leans: a magnitude derived from that conviction would be the same
    signal counted twice in `score_verdict`, and a magnitude borrowed off
    Technical's rungs would be a scale this seat does not have. The seat
    still reaches the ranking through its weighted conviction. See
    `NO_STATED_STRENGTH` for the full reasoning and what was deleted.

    **evidence** — one `VerdictEvidence(label="headline", text=...)` per
    item, `headline` and `impact_summary` joined so the check is visible
    without opening the source article, capped at `_MAX_NEWS_EVIDENCE_ITEMS`
    (JUDGMENT CALL, flagged for review) so a symbol with a long news day
    does not bloat the verdict — earlier items are kept (arrival order,
    unchanged from `NewsIntelligenceReport.stock_news`), on the assumption
    that News files its most decision-relevant item first. Included even
    for a neutral verdict (optional there, but still useful context).

    **invalidation** — JUDGMENT CALL, flagged for review. News items carry
    no stated falsifier the way `TechAnalysisResult.thesis_invalid_if` does,
    but `AnalystVerdict` refuses a directional (non-neutral) verdict with a
    blank one, so something honest has to be constructed.

    The obvious candidate — quote whichever item disagreed with the final
    direction as "the stated case against the call" — turns out to be
    unreachable given `StockNewsItem.sentiment`'s domain (bullish/bearish/
    neutral only): `collapse_stances` only resolves to a directional
    (non-"mixed") result when EVERY surviving sentiment is that exact same
    value (its `len(cleaned) == 1` branch — the positive/negative-SET
    branches below it can never fire for a 3-valued domain where each
    polarity set has exactly one reachable member). So whenever this
    function's `direction` is bullish or bearish, by construction every
    item already agrees with it and there is no opposing item to quote.
    (Proven in `tests/test_news_verdict.py::
    test_a_directional_call_never_has_a_disagreeing_item_to_quote`.)

    So the only honest falsifier available is structural: a later headline
    reporting the opposite sentiment on this symbol. That is a generic,
    templated sentence, not a fabricated specific fact, and it is the same
    sentence for every directional call — flagged so review can judge
    whether that bar is met, or whether "" (like the neutral case) would be
    more honest than a templated non-fact.

    A neutral verdict states no invalidation ("") — a neutral read is the
    absence of a call, so `AnalystVerdict` does not require one and none is
    invented.

    seat — "news", the same key `build_evidence_registry` puts news stances
    under (`put(symbol, "news", ...)`).
    """
    direction = collapse_stances(item.sentiment for item in items) or "neutral"
    if direction not in ("bullish", "bearish", "neutral"):
        # "mixed" (or anything else collapse_stances might someday return
        # that isn't one of the three verdict directions) is an unresolved
        # split — treated as no lean, never guessed at.
        direction = "neutral"

    if direction == "neutral":
        conviction = "low"
        # NOT 0.0 (item 65, 2026-09-26). News has no strength scale at all,
        # so it states none on a neutral read either — spelling this one
        # `0.0` while the directional branch below says `None` would claim
        # the seat has a scale it happens to read zero on.
        magnitude = NO_STATED_STRENGTH
        invalidation = ""
    else:
        agreeing = [item.conviction for item in items if item.sentiment == direction]
        conviction = max(agreeing, key=lambda c: _NEWS_CONVICTION_RANK.get(c, -1)) if agreeing else "low"
        magnitude = NO_STATED_STRENGTH
        # No opposing item to quote — see the docstring's invalidation
        # section for why that is provably always true here, not merely
        # true of the fixtures this happens to have been tested against.
        opposite = "bearish" if direction == "bullish" else "bullish"
        invalidation = f"a subsequent headline reporting {opposite} sentiment on {symbol}"

    evidence = [
        VerdictEvidence(
            label="headline",
            text=f"{item.headline} — {item.impact_summary}".strip(" —"),
        )
        for item in items[:_MAX_NEWS_EVIDENCE_ITEMS]
    ]

    return AnalystVerdict(
        seat="news",
        symbol=symbol,
        direction=direction,
        magnitude=magnitude,
        conviction=conviction,
        evidence=evidence,
        invalidation=invalidation,
    )


class NewsIntelligenceReport(LLMOutputModel):
    macro_narrative: MacroNarrative
    state_changes: list[StateChange] = []
    stock_news: Annotated[dict[str, list[StockNewsItem]], _AsWireRows(StockNewsRow)] = {}
    pm_briefing: str
    # OPTIONAL SINCE 2026-09-26 (board item 152) — and `None` here means
    # ABSENT, never "neutral". Measured against the retained production logs:
    # 5 of the 7 reproducible news-seat parse failures were this one field
    # carrying a word outside the three legal ones ("mixed" x4,
    # "mixed-to-bearish" x1), and each of those threw away an otherwise
    # well-formed report — macro_narrative, pm_briefing, every state change
    # and every per-symbol headline — over a single enum. `analyze()` now
    # drops the unreadable value (NewsAnalystAgent._drop_invalid_market_
    # sentiment) and keeps the rest, exactly as it already does per
    # state-change and per stock-news item. Coercing "mixed" to "neutral"
    # was deliberately NOT done: "mixed" is not neutral, and a fabricated
    # verdict that reads like a real one is the failure this item exists to
    # stop. Every renderer must therefore print an explicit UNREADABLE
    # marker on None, never a blank and never a default word.
    market_sentiment: Literal["bullish", "bearish", "neutral"] | None = None
    confidence: Literal["high", "medium", "low"]
    # Phase 9 (§9.1): a genuine catalyst News wants Technical to look at,
    # even when the symbol never tripped the tech prefilter. Default []
    # so an old persisted/replayed report parses unchanged.
    nominations: list[Nomination] = []
    # PM TEST GATE item 4, second half (2026-09-14). Computed by
    # `NewsAnalystAgent.analyze()` AFTER parsing, exactly like
    # `TechAnalysisResult.computed_levels` — never asked of the model,
    # never invented from it either. Holds every symbol `stock_mentions`
    # (the deterministic, pre-LLM word-boundary match over real wire text —
    # see `NewsDataProvider.tag_symbol_mentions`) proves had real headline
    # content shown to the model, but which had no key at all in the
    # model's `stock_news`. `analyze()` then fills those keys with `[]`
    # so the structured map is complete without inventing a headline;
    # this list still names the omission so data_status stays
    # `symbol_dropped` and downstream seats do not read the empty list
    # as "no news". Default `[]` so an old persisted/replayed report —
    # and every caller that hasn't been updated — parses unchanged.
    # `SkipJsonSchema` is what makes "never asked of the model" true on the
    # wire as well as in this comment: the field used to be rendered into
    # this report's `response_format` schema like any other, so the seat was
    # shown an output slot inviting it to nominate its own coverage gaps,
    # and `analyze()` overwrote whatever it said one line after parsing.
    dropped_news_symbols: Annotated[list[str], SkipJsonSchema()] = []
    # Board item 152. Field name -> the raw, unreadable value the seat sent,
    # for every top-level field `analyze()` had to drop to salvage the rest
    # of the report. Never asked of the model (`SkipJsonSchema`), filled by
    # `NewsAnalystAgent._drop_invalid_market_sentiment`. Travels with the
    # report into `specialist_evidence` (kind `analysis`, the stage persists
    # `model_dump_json()`), so a later reader can tell WHY a field is absent
    # from a stored answer without the log line that is gone after rotation.
    unreadable_fields: Annotated[dict[str, str], SkipJsonSchema()] = {}

    @classmethod
    def __get_pydantic_json_schema__(cls, core_schema, handler):
        """Keep the WIRE ask exactly as strong as it was before item 152.

        Making `market_sentiment` optional in Python is what lets a single
        unreadable word be dropped instead of discarding a paid report — but
        it also, by default, turns the sent schema's plain enum into
        `anyOf[enum, null]` and drops the field out of `required`. That
        would be a real loosening of what the desk ASKS FOR, on top of the
        tolerance it adds to what it ACCEPTS, and only the second one is
        intended. So the generated schema is put back: bare enum, still
        required. Python stays tolerant; the model is still told the field
        is mandatory and still told the only three legal words.

        (`stock_news` is asked for as a list of per-stock rows, so the
        schema is strict-able; `_rows_to_map` turns the rows back into the
        map. Python stays tolerant of this field; the wire does not.)
        """
        schema = handler(core_schema)
        props = schema.get("properties")
        if isinstance(props, dict) and "market_sentiment" in props:
            props["market_sentiment"] = {
                "type": "string",
                "enum": ["bullish", "bearish", "neutral"],
                "title": "Market Sentiment",
            }
            required = schema.setdefault("required", [])
            if "market_sentiment" not in required:
                required.append("market_sentiment")
        return schema

    def format_market_sentiment(self) -> str:
        """The sentiment word, or an explicit ABSENT marker — never a blank.

        Board item 152. `market_sentiment` can be `None` because the seat sent
        a word outside the three legal ones and `analyze()` dropped it to save
        the rest of the report. Every prompt and display must then say so in
        words: a bare empty string, or the word "neutral", would read to the
        next seat as a real verdict the seat never gave. Single helper so all
        four renderers (PM, risk, position reviewer, evening) say the same
        thing and none can drift back to interpolating the raw field.
        """
        if self.market_sentiment:
            return self.market_sentiment
        raw = self.unreadable_fields.get("market_sentiment")
        if raw:
            return (
                f"ABSENT — the seat answered {raw!r}, which is not one of "
                "bullish/bearish/neutral, so it was dropped. Treat the news "
                "seat as having given NO sentiment; it is NOT neutral"
            )
        return "ABSENT — the seat gave no sentiment. Treat it as having given NO sentiment; it is NOT neutral"

    def format_dropped_symbols_block(self) -> str:
        """Prompt text naming symbols shown real headlines but omitted from
        the seat's structured answer. Empty string when nothing was lost.

        Shared by PM, Risk, and the position reviewer so incomplete news is
        stated as UNKNOWN in every downstream seat, never left as silence.
        """
        if not self.dropped_news_symbols:
            return ""
        header = (
            "\n\n### News Answer Lost — {n} symbol(s) — NOT an absence of news\n"
            "The news seat had real headline coverage for these symbols "
            "but its structured answer for them did not survive parsing "
            "(a dropped response, not a judgment that there was nothing "
            "to report). Treat coverage for these names as UNKNOWN, "
            'never as clean and never as "no news":\n'
        ).format(n=len(self.dropped_news_symbols))
        return header + "\n".join(f"- {sym}" for sym in self.dropped_news_symbols)

    @model_validator(mode="before")
    @classmethod
    def _normalize_enum_case(cls, values):
        values = _normalize_enum_case_fields(
            values,
            lower_fields=("market_sentiment", "confidence"),
        )
        if isinstance(values, dict) and "stock_news" in values:
            values = {**values, "stock_news": _rows_to_map(values["stock_news"], "symbol", "items")}
        return values

    @model_validator(mode="before")
    @classmethod
    def _sanitize_nominations(cls, values):
        return _sanitize_nominations_field(values)
