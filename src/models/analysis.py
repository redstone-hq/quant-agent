from datetime import date
from typing import Annotated, Literal
from pydantic import BaseModel, Field, ValidationInfo, computed_field, field_validator, model_validator
from src.models.tech_reread import TechRereadFields
from src.models.base import (
    ACTIONABLE_TECH_RATINGS,
    LLMOutputModel,
    SOFT_EXIT_UNKNOWN,
    _normalize_enum_case_fields,
    _normalize_symbol,
    missing_stated_falsifier,
    parse_telemetry,
    reward_to_risk,
    stated_soft_exit,
)


class Nomination(LLMOutputModel):
    """A research seat's request that Technical examine a candidate.

    Phase 9 (`docs/QAMC_REMEDIATION_SPEC.md` §9.1/§9.2): before this,
    Technical was the ONLY seat that could originate a trade idea — every
    other seat could only rate a symbol Technical had already picked. A
    nomination inverts that: any seat can ask the desk to look at a
    symbol, and an on-demand Technical call decides whether there is an
    actual tradeable setup.

    This is deliberately NOT a trade recommendation. It carries the
    minimum a responder pass needs to act on it: which symbol, how
    strongly the nominating seat feels, and the concrete observation
    behind the ask — a nomination with no stated reason is not a
    nomination, hence `observation` is required non-empty.

    `seat` is stamped by the pipeline when a report's nominations are
    collected (`src/pipeline_stages.py::_collect_seat_nominations`), not
    emitted by the LLM — a seat's own prompt never has to know its own
    internal name, only that it may nominate. It defaults to "" so a
    directly-constructed Nomination (e.g. in a test) doesn't require it.
    """

    symbol: str
    seat: str = ""
    conviction: Literal["low", "medium", "high"]
    observation: str
    thesis_invalid_if: str = ""

    @field_validator("symbol")
    @classmethod
    def normalize_symbol(cls, value: str) -> str:
        return _normalize_symbol(value)

    @field_validator("thesis_invalid_if")
    @classmethod
    def strip_falsifier(cls, value: str) -> str:
        # Whitespace only. Item 99: a nomination whose seat gave no
        # falsifier stays EMPTY here and is recorded as missing by the
        # caller (`_collect_seat_nominations`'s event carries
        # `falsifier_missing`). Nothing downstream may substitute a
        # template — an invented condition reads as protection the desk
        # does not have. `src/risk/exit_guard.py::check_thesis_invalid_if`
        # consumes this string in exactly the shape it consumes the
        # technical seat's, and returns UNPARSEABLE (visibly, with a
        # reason) for a condition it cannot evaluate.
        return (value or "").strip()

    @field_validator("observation")
    @classmethod
    def require_observation(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("nomination observation cannot be empty")
        return text

    @model_validator(mode="before")
    @classmethod
    def _normalize_enum_case(cls, values):
        return _normalize_enum_case_fields(values, lower_fields=("conviction",))


def _sanitize_nominations_field(values):
    """Drop malformed nomination entries rather than fail the whole report.

    Mirrors `MacroAnalysis._sanitize_sector_guidance`: one bad nomination
    (empty observation, bad conviction, empty symbol) must not cost the
    seat its entire structured output for the run — the rest of the
    analysis is real and valuable even when the model's nomination
    attempt was malformed. Applied as a `mode="before"` validator on each
    nominating seat's report model.
    """
    if not isinstance(values, dict):
        return values
    raw = values.get("nominations")
    if not isinstance(raw, list):
        return values
    cleaned = []
    for item in raw:
        # Already a validated Nomination — the direct-construction path
        # (`MacroAnalysis(..., nominations=[Nomination(...)])`, used by
        # tests and any programmatic caller) hands this validator real
        # model instances, not dicts. Pass those straight through; only
        # dict items (the LLM-JSON path) need re-validation.
        if isinstance(item, Nomination):
            cleaned.append(item)
            continue
        if not isinstance(item, dict):
            continue
        try:
            Nomination.model_validate(item)
        except Exception:
            continue
        cleaned.append(item)
    values["nominations"] = cleaned
    return values


class OHLCV(BaseModel):
    date: date
    open: float
    high: float
    low: float
    close: float
    volume: int


class TechnicalIndicators(BaseModel):
    symbol: str
    ma_20: float | None = None
    ma_50: float | None = None
    ma_200: float | None = None
    #: The 200-session SMA one completed session earlier, so the exit guard can
    #: read the 200-MA SLOPE (rising vs falling) — not just the level — when it
    #: classifies a structural break's trend regime (owner mandate 2026-09-24,
    #: trend-scaled exit). None until there is one extra bar beyond the 200-MA
    #: warm-up.
    ma_200_prior: float | None = None
    rsi_14: float | None = None
    macd: float | None = None
    macd_signal: float | None = None
    macd_hist: float | None = None
    bb_upper: float | None = None
    bb_middle: float | None = None
    bb_lower: float | None = None
    atr_14: float | None = None
    #: Wilder's Average Directional Index and its two directional components,
    #: all on the same 14-session lookback (`src.data.technical.ADX_PERIOD`).
    #: ADX measures trend STRENGTH only (never direction); +DI/-DI carry the
    #: direction. Used by `src.risk.exit_guard.check_structural_protection` to
    #: select a support/resistance break's trend-scaled confirmation regime
    #: (owner mandate 2026-09-24): a break against the trend or in a weak tape
    #: exits fast, while a break WITH a strong trend (a likely shakeout) is held
    #: longer. None until there are enough bars to warm the recursive smoothing.
    adx_14: float | None = None
    di_plus_14: float | None = None
    di_minus_14: float | None = None
    volume_change_pct: float | None = None

    @field_validator("symbol")
    @classmethod
    def normalize_symbol(cls, value: str) -> str:
        return _normalize_symbol(value)


class VerdictEvidence(BaseModel):
    """One checkable fact backing an `AnalystVerdict`.

    Phase 13 (`docs/QAMC_REMEDIATION_SPEC.md` §13.2, item 3): evidence is
    "specific, checkable facts backing the call (a number, a dated event, a
    level) — not prose alone". So an item is a labelled NUMBER, a labelled
    DATED event, or a labelled observation, and an item with none of those
    is refused — a bare label is a heading, not evidence.

    Not an `LLMOutputModel`: in this first increment every verdict is
    DERIVED in Python from a seat's already-validated report (see
    `TechAnalysisResult.to_verdict`), never parsed from an LLM response. A
    null here would be our own bug and should fail loudly.
    """

    label: str = Field(min_length=1)  # e.g. "stop_loss", "trend", "risk_reward"
    value: float | None = None  # a price, a level, a ratio
    as_of: date | None = None  # a dated event
    text: str = ""  # the observation, when it is not a number

    @model_validator(mode="after")
    def _require_something_checkable(self):
        if self.value is None and self.as_of is None and not self.text.strip():
            raise ValueError(
                f"evidence {self.label!r} carries no value, no date and no text — a label alone is not a checkable fact"
            )
        return self


class AnalystVerdict(BaseModel):
    """The one shape every specialist seat hands the Portfolio Manager.

    Phase 13 (`docs/QAMC_REMEDIATION_SPEC.md` §13.2): the PM was comparing
    several different essays and picking one. This is the checkable,
    comparable judgement instead — the same four things from every seat:

    1. **direction** — bullish / bearish / neutral, PLUS `magnitude`, how
       far in that direction the seat leans on a 0..1 scale — or `None`,
       meaning this seat HAS no strength scale and states none. The label
       vocabulary is the one `stance_is_aligned` and the evidence registry
       already speak (`StockNewsItem.sentiment`, `SmartMoneyFinding.stance`,
       `MacroAnalysis.equity_outlook`), so a verdict can be netted against
       the §9.4 score without a translation table.

       **`None` is not zero — item 65, 2026-09-26.** Until this date a seat
       with no strength scale sent the literal `0.0`, which is a STATED
       strength of nothing and is indistinguishable, at every consumer, from
       a seat that has a scale and read zero off it. The ranking then summed
       four such terms into a number it labelled "strength, summed over N
       seats" and showed the Portfolio Manager. A sum is additively neutral
       to a zero, so the ORDER was never wrong — but the reported quantity
       was, and nothing structural stopped a future consumer from averaging,
       min-ing or thresholding those zeros, at which point the placeholder
       would have moved money. `None` makes the absence a type rather than a
       value: it cannot be summed by accident, and every consumer has to say
       what it does with a seat that has no scale.
    2. **conviction** — how sure the seat is, on the desk's existing
       `high` / `medium` / `low` scale, SEPARATE from what it thinks.
    3. **evidence** — a list of `VerdictEvidence`, at least one for any
       directional call. A neutral read may carry none.
    4. **invalidation** — the stated condition under which the call is
       wrong. Required non-empty for any directional call. A neutral verdict
       is the ABSENCE of a call, so there is nothing to falsify and the field
       may be blank; a neutral verdict with a non-zero magnitude is refused
       as self-contradictory.

    `seat` and `symbol` are identity, not judgement. `seat` uses the
    evidence-registry key for the seat ("technical", "news", ...) so a
    verdict and a registry stance about the same name agree on who said it.

    `signed_magnitude` is the number a ranking or a netting rule reads:
    +magnitude for bullish, -magnitude for bearish, 0 for neutral — and
    `None` when the seat stated no strength at all, for the same reason
    `magnitude` is `None`: an absent number must not arrive as a zero.

    Not an `LLMOutputModel` — see `VerdictEvidence` for why.
    """

    seat: str = Field(min_length=1)
    symbol: str
    direction: Literal["bullish", "bearish", "neutral"]
    #: `None` = this seat states no strength (see the class docstring and
    #: `NO_STATED_STRENGTH`). A float is a strength the seat actually stated.
    magnitude: Annotated[float, Field(ge=0.0, le=1.0)] | None = None
    conviction: Literal["high", "medium", "low"]
    evidence: list[VerdictEvidence] = Field(default_factory=list)
    invalidation: str = ""

    @field_validator("symbol")
    @classmethod
    def normalize_symbol(cls, value: str) -> str:
        return _normalize_symbol(value)

    @field_validator("invalidation", mode="before")
    @classmethod
    def _strip_invalidation(cls, value):
        return "" if value is None else str(value).strip()

    @model_validator(mode="after")
    def _a_call_must_be_falsifiable_and_backed(self):
        if self.direction == "neutral":
            if self.magnitude not in (None, 0.0):
                raise ValueError(
                    f"{self.symbol}: a neutral verdict cannot carry magnitude {self.magnitude} — neutral means no lean"
                )
            return self
        # Item 65, 2026-09-26. A seat that HAS a strength scale and puts a
        # directional call on it cannot place that call at zero distance:
        # "bullish, strength nil" is the neutral verdict above wearing a
        # direction. The honest encoding of no distance claimed is `None`
        # (no scale), which this deliberately still allows — see the class
        # docstring. This closes the hole the old placeholder left open: a
        # 0.0 arriving on a directional verdict is now always a bug and is
        # refused at construction instead of being summed as information.
        if self.magnitude == 0.0:
            raise ValueError(
                f"{self.symbol}: a {self.direction} verdict from {self.seat} "
                "states a strength of 0.0 — a directional call at zero "
                "distance is a neutral one. Use magnitude=None if this seat "
                "has no strength scale (see NO_STATED_STRENGTH)"
            )
        if not self.invalidation:
            raise ValueError(
                f"{self.symbol}: a {self.direction} verdict from {self.seat} "
                "must state its invalidation condition — a call nobody can "
                "prove wrong is not a call"
            )
        if not self.evidence:
            raise ValueError(
                f"{self.symbol}: a {self.direction} verdict from {self.seat} "
                "must cite at least one piece of checkable evidence"
            )
        return self

    @computed_field
    @property
    def signed_magnitude(self) -> float | None:
        # `None` in, `None` out — a seat with no strength scale has no
        # signed strength either, and handing back 0.0 here would put the
        # placeholder straight back for any netting rule that reads this.
        if self.magnitude is None:
            return None
        if self.direction == "bullish":
            return self.magnitude
        if self.direction == "bearish":
            return -self.magnitude
        return 0.0


#: The Technical rating scale in its own order, most bearish to most bullish.
#: The rungs are an ORDINAL fact the seat states; no spacing between them is
#: chosen or measured anywhere.
RATING_SCALE: tuple[str, ...] = ("strong_sell", "sell", "neutral", "buy", "strong_buy")


def rating_rung_strength(rating: str) -> float:
    """How far a rating sits from the neutral rung, as a fraction of the way
    to the scale's end -- read off the rung's position, not a chosen number.

    It labels the rung on `AnalystVerdict.magnitude` and is used ONLY to
    order candidates that tie on conviction (`src/verdicts.py`); it is never
    added to a score (n06, 2026-10-16).
    """
    centre = len(RATING_SCALE) // 2
    return abs(RATING_SCALE.index(rating) - centre) / centre


#: `AnalystVerdict.magnitude` for a DIRECTIONAL verdict from a seat that
#: states no independent strength of its own. It is `None` — the ABSENCE of
#: a number, not a number — so there is nothing here for an arithmetic to
#: consume by accident.
#:
#: WHY THIS IS FLAT, 2026-09-13 (see `docs/INCIDENT_HISTORY.md`, retired
#: item 31). `score_verdict`
#: (`src/verdicts.py`) was `magnitude + conviction_score(conviction)` — TWO
#: signals, weight 1 each. Three seats used to derive `magnitude` from the
#: very field they also hand to `conviction`:
#:
#:   * news       — magnitude was a table on `conviction` itself;
#:   * macro      — magnitude was a table on `confidence`, which IS the
#:                  conviction it reports;
#:   * smart_money— magnitude AND conviction were both tables on the single
#:                  `economic_role` label.
#:
#: For those seats the composite was not two signals averaged, it was one
#: signal counted twice, at a spacing (0.33/0.67/1.0 for news, 0.25/0.5/0.75
#: for macro, 1.0/0.6/0.3 for smart_money) that no source and no measurement
#: stood behind — three different unsourced encodings of the same 3-rung
#: scale. A "actionable" smart-money label alone scored 2.0, the maximum the
#: composite can produce, equal to a strong_buy at high conviction from the
#: one seat that was measured (item 18) as actually concluding.
#:
#: The rule now: magnitude carries information ONLY where a seat states a
#: strength independent of its confidence. Technical does (its rating rungs).
#: Nothing else on this desk does, so nothing else claims one. This REMOVES
#: invented numbers rather than replacing them with better-argued ones —
#: `qamc-no-arbitrary-numbers-principle`. The dropped inputs are not lost to
#: the reader: macro's `regime_shift`/`shift_reason` still reach the verdict
#: as evidence and invalidation, and smart_money's `economic_role` still sets
#: conviction via `_SMART_MONEY_ROLE_CONVICTION`, whose ordering is a
#: restatement of the pre-existing `_ROLE_TIERS`, not a new judgment.
#:
#: AND WHY IT IS NOT 0.5 — corrected 2026-09-13, same day, on
#: adversarial review before merge. The first version of this deletion set the
#: four rungless seats to 0.5, "Technical's `buy` rung, reused rather than
#: respelled". Borrowing is not deriving: 0.5 is a number read off ANOTHER
#: seat's scale, and these four seats do not have that scale — that is the
#: whole reason they are here. A seat that states no strength states no
#: strength, and the honest encoding of "no distance claimed" is no distance.
#:
#: AND WHY IT IS NO LONGER `0.0` EITHER — item 65, RESOLVED 2026-09-26.
#: 2026-09-13 got the judgement right and the encoding wrong. `0.0` is a
#: point ON the 0..1 strength scale, so "this seat has no scale" was being
#: spelled with a value drawn from the scale it is denying having. Verified
#: against the code before changing anything: `rank_verdicts` SUMS the term
#: (`magnitude = sum(v.magnitude * w ...)`), a zero is additively neutral,
#: and no consumer averaged or thresholded it — so the placeholder never
#: reordered a candidate and no money moved because of it. What it did do
#: was make the ranking REPORT a "strength summed over N seats" that only
#: one seat could ever contribute to, and leave the next consumer free to
#: treat four placeholders as four measured zeros. `None` is the absence
#: itself: it cannot be summed by accident, `rank_verdicts` now names the
#: seats the strength term actually covers, and a directional verdict that
#: arrives with a literal `0.0` is REFUSED by `AnalystVerdict`'s validator.
#:
#: This is only coherent because `rank_verdicts` no longer AVERAGES seats (see
#: `src/verdicts.py`): such a seat still contributes its own weighted
#: conviction to the total, so a directional read with nothing behind it is
#: not silently equal to no coverage at all — it is equal to exactly what it
#: is worth, its conviction. Under the old weighted average an absent
#: strength would have DRAGGED an agreeing candidate down; under a sum over
#: only the seats that state one, it cannot.
#:
#: THE STANDING DECISION (item 65, recorded 2026-09-26, retired with it):
#: direction plus confidence is all these four seats can say, and that is the
#: answer, not a gap awaiting a number. Each was re-checked against its own
#: model on the day: `EarningsAnalysis.investment_implications.sentiment`,
#: `StockNewsItem.sentiment`, `MacroAnalysis.equity_outlook` and
#: `SmartMoneyFinding.stance` are every one of them a SINGLE directional rung
#: (bullish/bearish/neutral) with no graded vocabulary anywhere beside them —
#: unlike Technical, whose `strong_buy`/`buy` rungs are a strength the seat
#: already publishes and `rating_rung_strength` merely transcribes. Asking any of
#: the four to emit a 0..1 number would be asking it to invent boundaries no
#: source, no measurement and no instrument supplies
#: (`qamc-no-arbitrary-numbers-principle`), and it would be uncheckable by
#: construction. REVISIT ONLY IF one of two things happens: a seat's own
#: output model gains a graded rung it genuinely observes (the way Technical
#: has one), or the conviction ledger clears `_CONVICTION_OUTCOME_MIN_N`
#: resolved calls for that seat and supplies a measured one. Either is a
#: schema change to RATIFY with the derivation attached, plus the seat's
#: prompt in the same pass — never a constant restored here.
NO_STATED_STRENGTH: None = None

#: Text recorded (evidence, prompts) when an actionable read has no analyst
#: target / names no support or resistance (owner rule 2026-10-09: neither
#: blocks a trade). Prompts print these, never the word "None".
NO_TARGET_TEXT = "no target"
NO_LEVEL_NAMED_TEXT = "no level named"

RATING_DIRECTION: dict[str, str] = {
    "strong_buy": "bullish",
    "buy": "bullish",
    "neutral": "neutral",
    "sell": "bearish",
    "strong_sell": "bearish",
}


class TechReasoningChain(LLMOutputModel):
    """5-step chain of thought for a single symbol: trend, momentum,
    volatility, volume, and support/resistance."""

    # Every field has `min_length=1` so the LLM cannot skip a step by
    # sending an empty string. This matches the discipline already in place
    # on the other CoT chains (Evening / Position / Meta) and closes the
    # audit gap that contradicted the README's "schema-enforced CoT, LLM
    # cannot skip steps" claim. This class's docstring above IS the
    # model-facing schema description (item 157 adversary review,
    # 2026-09-23) — keep it short and free of internal references; put
    # engineering notes here in comments instead.
    #
    # Adversary review, 2026-09-23, 2nd pass: an earlier draft of this
    # docstring said "one sentence per framework step" — an instruction
    # this class never actually enforced (only non-empty, via
    # `min_length=1`) and that the main prompt never asks for either; real
    # answers routinely use more than one sentence, and `support_resistance`
    # below explicitly wants both a level AND its ATR distance, which a
    # one-sentence rule would fight. Removed rather than left as an
    # unenforced, prompt-contradicting instruction sent to the model on
    # every call.
    trend: str = Field(min_length=1)  # MA alignment, price vs MA20/50/200
    momentum: str = Field(min_length=1)  # RSI level, MACD cross direction
    volatility: str = Field(min_length=1)  # BB position, ATR expansion/contraction
    volume: str = Field(min_length=1)  # volume confirming or diverging vs trend
    support_resistance: str = Field(min_length=1)  # key levels from indicators + recent pivots


class TechAnalystAnswerItem(LLMOutputModel):
    """One symbol's technical read: rating, structural levels, and the
    reasoning behind them."""

    # This is the part of a technical-seat row the MODEL actually fills in.
    # `TechAnalysisResult` below mixes these LLM-emitted fields with eight
    # fields the desk fills in itself after the call (`atr_14`,
    # `computed_levels`, `computed_level_touches`, `levels_coverage`,
    # `signal_bar_low`, `signal_bar_high`, `bars_available`,
    # `signal_age_days`). A strict model-facing schema cannot require the
    # model to emit fields it never sees, so this class is exactly what is
    # sent to the provider as the response schema (via `TechAnalystAnswer`
    # below). Nothing actually constructs a `TechAnalystAnswerItem` in the
    # parsing path today — `TechAnalystAgent._analyze_chunk` still builds
    # `TechAnalysisResult(**item)` directly, same as before this split — so
    # this class's only live job is shaping the wire schema;
    # `TechAnalysisResult` adds the eight desk-filled fields on top for
    # internal use once the row is enriched. (Board item 157, from #538's
    # write-up.)
    #
    # Splitting the python-set fields out of the model-facing schema also
    # removes `computed_level_touches` — the one free-form
    # `dict[float, int]` map that used to force `strict=false` on the whole
    # thing (see `_has_free_form_map` in src/agents/base.py). Whether that
    # actually yields `strict=true` in practice is verified, not assumed —
    # see `_response_format_for(TechAnalystAnswer)` and the live-call check
    # in tests/test_tech_schema_live.py.
    #
    # This class's docstring above IS what reaches the model on every call
    # (item 157 adversary review, 2026-09-23: pydantic emits the class
    # docstring verbatim as the schema's "description"). Keep it short and
    # free of item numbers, file paths and internal decision history — put
    # that here in a comment instead. `tests/test_tech_schema.py` fails if
    # any of those markers reappear in the schema actually sent.

    symbol: str
    rating: Literal["strong_buy", "buy", "neutral", "sell", "strong_sell"]
    conviction: Literal["high", "medium", "low"] = "medium"
    entry_price: float | None = None
    reference_target: float | None = None  # renamed from exit_price — it's a soft reference, not a hard TP
    stop_loss: float | None = None
    # --- Structural levels (2026-08-27) -------------------------------------
    # The prior design let the analyst omit levels and had
    # `PortfolioConstructor` invent replacements: `entry - 2*ATR` for the stop
    # and `entry * (1 + 2*stop_gap)` for the target. Every downstream metric
    # — thesis_progress, pace, R/R, TARGET_BREACH — was then measured against
    # a number nobody derived from the chart. These fields make the levels
    # first-class so the invented fallbacks can be deleted.
    #
    # Prices only; no indicator values. Empty lists are legal for `neutral`
    # (no trade is being proposed) but not for an actionable rating.
    support_levels: list[float] = Field(default_factory=list)
    resistance_levels: list[float] = Field(default_factory=list)
    # How the position must be MANAGED, decided at entry from the chart:
    #   "range"    — clear structure on both sides. Fixed target is meaningful;
    #                thesis_progress and pace are valid measurements.
    #   "breakout" — no overhead structure (highs, clean break). The target is
    #                a MEASURED MOVE reference, not a level anyone is defending;
    #                the position is managed by trailing and progress/pace must
    #                be disabled downstream (see QAMC_REMEDIATION_SPEC Phase 3).
    setup_type: Literal["range", "breakout"] | None = None
    # The analyst's own estimate of how many trading sessions this thesis needs
    # to resolve. Pinned at entry and never recomputed. This replaces the
    # self-referential `avg_hold_days` calibration that made `pace` a feedback
    # loop: selling quickly shrank the average, which made every position look
    # stalled, which drove more selling.
    expected_horizon_sessions: int | None = None
    reasoning_chain: TechReasoningChain
    reasoning: str  # 1-sentence summary; reasoning_chain carries the full analysis
    # Soft exit signal separate from the hard stop_loss. Example:
    # "MACD histogram turns negative for 2 consecutive closes" — lets PM / midday
    # exit BEFORE the broker stop fires, saving the 3-5% typically given up
    # between thesis-break and stop-trigger.
    # MEASURED 2026-09-01: models emit `"thesis_invalid_if": null` on about 2% of
    # candidates (42 explicit nulls in 2,056 field occurrences across two weeks
    # of production responses, most recently the morning of 2026-09-01). Every
    # OTHER field they null here is typed `| None` and tolerates it; this one
    # was a bare `str`, so pydantic rejected the null and the WHOLE candidate
    # was dropped with "Failed to parse tech analysis item". A silently
    # discarded analysis is an idea the desk never gets to consider, which is
    # the under-deployment problem arriving by a side door. Found by the
    # rehearsal rig, confirmed against the production database.
    #
    # Typed `| None` (item 157 adversary review, 2026-09-20), NOT bare `str`,
    # even though the `mode="before"` validator below already tolerates a
    # runtime `None` regardless of the annotation. The annotation is what
    # `model_json_schema()` — and therefore the schema actually SENT to a
    # constrained-decoding provider — is built from. A bare `str` schema has
    # no `null` branch, so a provider that genuinely enforces the schema
    # could no longer let the model say "I don't know" here at all: it would
    # have to invent a plausible-looking falsifier just to satisfy the type,
    # which is worse than the null this field exists to record honestly.
    # `str | None` keeps the wire schema truthful to what the model may
    # legitimately mean, while the validator still normalizes that null down
    # to `""` (neutral) or `SOFT_EXIT_UNKNOWN` (actionable) exactly as today.
    thesis_invalid_if: str | None = ""

    @field_validator("thesis_invalid_if", mode="before")
    @classmethod
    def _null_thesis_invalid_if_is_blank(cls, v, info: ValidationInfo):
        """An absent soft-exit signal is blank, never a reason to bin the read.

        A stated non-empty string is never replaced. Explicit null on an
        actionable rating records `unknown` (not a falsifier). Neutral
        stays empty, matching the prompt.

        Item 157 (2026-09-20): this field's annotation moved from bare `str`
        to `str | None` so the model-facing wire SCHEMA can express the null
        this validator has always tolerated at the Python layer (see the
        field's own comment). That moved it out of `LLMOutputModel`'s generic
        `_null_droppable_fields` set — which only catches fields whose
        annotation still REJECTS None — so the generic mechanism's
        `parse_telemetry.record_null_coercion` call no longer fires for it.
        Recorded here instead, so "the model sent an explicit null here" is
        still counted exactly as before, whatever the eventual value becomes.
        """
        if isinstance(v, str) and v.strip():
            return v
        if v is None:
            parse_telemetry.record_null_coercion(cls.__name__, "thesis_invalid_if")
        rating = str((info.data or {}).get("rating") or "").strip().lower()
        if v is None and rating not in ("", "neutral"):
            return SOFT_EXIT_UNKNOWN
        return "" if v is None or v == "" else v


class TechAnalystAnswer(LLMOutputModel):
    """Return your analysis as an object with one key, "results", whose
    value is the list of per-symbol results."""

    # OpenAI/OpenRouter/Google-compat strict `json_schema` response formats
    # require an OBJECT at the schema root (`_strictify_schema` in
    # src/agents/base.py only ever strictifies `type: object` nodes); the
    # seat's answer was a bare list, so no schema could ever be attached to
    # it. This wrapper is the model-facing top level; `results` is unwrapped
    # back to the underlying list by `AgentResult.parse_json_rows(...,
    # list_field="results")` before the existing per-row salvage runs, so a
    # provider that ignores or partially honours the schema (any legacy
    # stored answer, the non-schema-enforcing failover path, a model that
    # just answers with a bare array anyway) is still parsed exactly as
    # before. (Board item 157.)
    #
    # This class's docstring above IS the schema description sent to the
    # model on every call — see the comment on `TechAnalystAnswerItem`
    # above for why it must stay free of internal references.
    results: list[TechAnalystAnswerItem] = Field(default_factory=list)


class TechAnalysisResult(TechRereadFields, TechAnalystAnswerItem):
    # PYTHON-SET, not LLM-emitted (same pattern as `atr_14` below): every
    # level `src/data/levels.py::find_structural_levels` found over the full
    # fetched history, supports and resistances unioned into one list of bare
    # prices. The two fields above are the LLM's SELECTION from the levels
    # block in its prompt; this is the block itself, preserved so the
    # constructor can derive the target arithmetically instead of reading the
    # model's `reference_target` (2026-09-01 — see the target-derivation
    # section of src/data/levels.py for why that division was invalid).
    #
    # Unioned on purpose. `find_structural_levels` calls a level support or
    # resistance relative to the LAST CLOSE; the trade is entered at a live
    # price that can sit on the other side of it, so the partition is redone
    # against the actual entry at derivation time.
    computed_levels: list[float] = Field(default_factory=list)
    # PYTHON-SET, keyed by the same prices as `computed_levels` above: how
    # many pivots `find_structural_levels` clustered into each one (Phase
    # 12.1, 2026-09-03). `computed_levels` collapses every qualifying level
    # to a bare price, which is enough to derive a target but not enough to
    # ask "how much do we trust this specific level" — and §12.1 made that
    # question load-bearing by honouring a level-backed stop however tight.
    # It is DATA, not a gate: since 2026-10-14 no touch count decides whether
    # a real level backs a stop (a 148-stock study found a stop just beyond a
    # real level is hit less often than one with no level at the same
    # distance, and extra touches add nothing). It is recorded on the exit
    # guard's break rows and summed as verdict evidence; target derivation is
    # deliberately untouched by this field.
    computed_level_touches: dict[float, int] = Field(default_factory=dict)
    # PYTHON-SET, keyed by the same prices as `computed_levels`: each level's
    # MEASURED zone as ``[low, high]`` — the combined traded range of the
    # pivot bars that formed it (item 55, 2026-09-30). This is what replaced
    # the flat 1% cluster tolerance; see `src/data/levels.py::_cluster`.
    # A price missing from this map is not an error: the matching sites fall
    # back to the percentage bound, which is the fail-closed direction.
    computed_level_zones: dict[float, list[float]] = Field(default_factory=dict)
    #: (low, high) of the bars that DREW each computed level, keyed by the
    #: same price. `_level_backing_stop` needs it to answer "is the stop AT
    #: this level" without a tolerance — docs/WORK.md item 215. Python-set
    #: from `find_structural_levels`, never emitted by a model; missing means
    #: unknown, which fails closed to "not level-backed".
    computed_level_bars: dict[float, list[tuple[float, float]]] = Field(default_factory=dict)
    # PYTHON-SET (2026-09-12): what the bar history behind `computed_levels`
    # was — one of the COVERAGE_* states in `src/data/levels.py`. An empty
    # `computed_levels` with coverage "measured" is a chart with no
    # structure (a trade refusal); with any other coverage it is a DATA
    # fault (unusable history) that must be recorded and alerted as such,
    # never counted as a trade the desk judged. Defaults to "unknown",
    # which the derivation classifies with the faults, fail-closed.
    levels_coverage: str = "unknown"
    # PYTHON-SET (owner rule 2026-10-09). True when the analyst's
    # `reference_target` sits on the WRONG side of its own entry (at/below
    # entry for a buy, at/above for a sell). Until 2026-10-09 that rejected
    # the whole analysis and dropped the signal; the guess is only evidence
    # (the order's take-profit is derived from structure), so the signal is
    # kept and this recorded flag travels with it for every AI reader.
    reference_target_wrong_side: bool = False
    # VALIDATOR-SET (2026-10-12, XOM incident): the actionable-rating fields
    # the analyst left out — any of `entry_price`, `stop_loss`, `setup_type`,
    # `expected_horizon_sessions`, `thesis_invalid_if`. RECORDED, never a
    # reason to drop the idea: sizing uses the live price and the stop is
    # read from the bars when these are absent. Rendered to the PM as one line.
    missing_levels: list[str] = Field(default_factory=list)
    # PYTHON-SET (2026-09-12, docs/WORK.md item 54), same pattern as the
    # two above, from the same bars: the last completed bar's low and high
    # — the SIGNAL bar the analyst judged — and how many completed sessions
    # the desk actually had for this instrument. The constructor reads the
    # bar edge as the instrument's own fallback stop when nothing computed
    # backs the typed one (the wider of it and the ATR noise band —
    # Kullamägi's "low of the day"), and the bar count to refuse a listing
    # too young to measure (`src/data/technical.py::
    # LONGEST_INDICATOR_WINDOW`). None = not recorded (older persisted row,
    # hand-built object): the band alone decides, and no youth is assumed.
    signal_bar_low: float | None = None
    signal_bar_high: float | None = None
    bars_available: int | None = None
    # Days since this rating was first issued (unchanged). Python-computed from
    # TechStore after TechAnalystAgent returns; None on first run or when the
    # symbol wasn't in yesterday's cache. Fresh=1 means "new today", 7+=stale.
    signal_age_days: int | None = None
    # ATR(14) carried through from the input indicators (Python-set after
    # TechAnalystAgent returns — the LLM doesn't emit this; it's read from
    # the indicators object the prompt was built from). Used downstream by
    # `PortfolioConstructor._resolve_stop` as a volatility-aware fallback
    # when neither the target's `suggested_stop_price` nor the LLM's
    # `stop_loss` is available — `entry - 2*ATR` thrashes less on
    # high-volatility names than a hardcoded 5% stop.
    atr_14: float | None = None

    @computed_field
    @property
    def risk_reward(self) -> float | None:
        """Reward/risk ratio from entry, stop, and reference_target.

        Computed in Python (not trusted to the LLM). For BUY we expect (target > entry > stop);
        for SELL the inequalities flip. Returns None when any price is missing, the rating
        is neutral, or the geometry is malformed (so PM / RM won't render a fake ratio).

        **This is the ANALYST's geometry, not the order's**, and the two
        are routinely different: `entry_price` is the analyst's snapshot
        price and `reference_target` is the model's guess, while the order
        ships at the live price against a target the constructor derives
        from the bars. `TradeDecision.reward_risk` is the number the desk
        gates on. Both now divide through the same `reward_to_risk`, so
        any gap between them is a genuine difference of INPUTS — which is
        what the constructor's reconciliation note explains — and never a
        difference of arithmetic.
        """
        if self.rating in ("buy", "strong_buy"):
            is_short = False
        elif self.rating in ("sell", "strong_sell"):
            is_short = True
        else:
            return None
        ratio = reward_to_risk(
            self.entry_price,
            self.stop_loss,
            self.reference_target,
            is_short=is_short,
        )
        return None if ratio is None else round(ratio, 2)

    @property
    def target_text(self) -> str:
        """The analyst's target as prompt text: the number, or "no target"."""
        return NO_TARGET_TEXT if self.reference_target is None else str(self.reference_target)

    @property
    def levels_text(self) -> str:
        """The analyst-named levels as text, or "no level named"."""
        if not self.support_levels and not self.resistance_levels:
            return NO_LEVEL_NAMED_TEXT
        return f"support {self.support_levels} / resistance {self.resistance_levels}"

    def to_verdict(self) -> "AnalystVerdict":
        """This read, restated in the shared Phase 13 verdict shape.

        A RESTATEMENT, not a second opinion: every field is read off values
        this result already carries and the analyst already validated. The
        technical seat was measured (item 18, 2026-09-03) as the one seat
        that already CONCLUDES — rating, conviction, R/R, levels, `Invalid
        if`, one-line why — so this is a mapping, not new prompting.

        direction / magnitude — `RATING_DIRECTION` / `rating_rung_strength`.
        conviction            — verbatim.
        evidence              — the numbers the desk can check against a
                                chart (entry, stop, target, R/R, the levels
                                the analyst selected) plus the five
                                reasoning-chain observations, labelled.
        invalidation          — `thesis_invalid_if` when the analyst stated
                                one. When it did not (measured ~2% of
                                actionable reads, see the field's own
                                comment), the hard stop IS the analyst's own
                                stated falsifier, so it is used, and the text
                                says so — nothing is invented.
        """
        direction = RATING_DIRECTION[self.rating]
        evidence: list[VerdictEvidence] = []
        for label, value in (
            ("entry_price", self.entry_price),
            ("stop_loss", self.stop_loss),
            ("reference_target", self.reference_target),
            ("risk_reward", self.risk_reward),
        ):
            if value is not None:
                evidence.append(VerdictEvidence(label=label, value=float(value)))
        for level in self.support_levels:
            evidence.append(VerdictEvidence(label="support_level", value=float(level)))
        for level in self.resistance_levels:
            evidence.append(VerdictEvidence(label="resistance_level", value=float(level)))
        # `computed_level_touches` (Python-set, `find_structural_levels`) is
        # already how many prior pivots back EACH computed level. It is
        # evidence only; no touch count decides whether a stop is level-backed
        # (that bar was deleted 2026-10-14).
        #
        # RISK-SIDE ONLY, not every computed level. `computed_levels` is
        # `find_structural_levels`' supports and resistances UNIONED
        # (`TechAnalysisResult.computed_levels`'s own field comment), so a
        # blind sum over `computed_level_touches` counts overhead
        # resistance the same as underlying support — for a long, a
        # heavily-touched ceiling is supply IN THE WAY of the trade, not
        # evidence for it, and summing it in would score exactly the wrong
        # direction. This filters to the levels on the RISK side of the
        # analyst's own entry (below entry for a long, above it for a
        # short) — the same side `_level_backing_stop` looks at, and the
        # ONLY side `reward_risk_floor_applies` says a breakout is approved
        # on at all (`src/risk/constants.py`): "a real level-backed or
        # ATR-derived stop, plus the multi-agent conviction and evidence
        # checks". A breakout's overhead is deliberately not measured
        # anywhere in this desk's approval logic; this does not reach in
        # and measure it either.
        #
        # Summed across qualifying levels (not averaged: more
        # independently-touched structure under the stop is more evidence
        # behind it, not a dilution of any one level — same "evidence adds"
        # posture `score_verdict` already takes), attached as its own
        # evidence item so `src/verdicts.py::rank_verdicts` can read it as
        # a real ranking signal WITHOUT recomputing anything (WORK.md item
        # 141: this is what closes the residual alphabetical fallback left
        # when a tied composite tier has no risk_reward at all, e.g. an
        # all-breakout tier). No entry price, or no computed levels at
        # all, means zero — a real absence, not an invented value.
        if self.reference_target is None and direction != "neutral":
            evidence.append(VerdictEvidence(label="reference_target", text=NO_TARGET_TEXT))
        if not self.support_levels and not self.resistance_levels and direction != "neutral":
            evidence.append(VerdictEvidence(label="analyst_levels", text=NO_LEVEL_NAMED_TEXT))
        if self.reference_target_wrong_side:
            evidence.append(
                VerdictEvidence(
                    label="reference_target_wrong_side",
                    text=(
                        f"the analyst's reference target {self.reference_target} is on "
                        f"the wrong side of its own entry {self.entry_price}; it is "
                        f"evidence only and was not used as the take-profit"
                    ),
                )
            )
        stop_side_level_touches = 0.0
        if self.entry_price is not None:
            is_short = self.rating in ("sell", "strong_sell")
            for price, touches in self.computed_level_touches.items():
                on_risk_side = price >= self.entry_price if is_short else price <= self.entry_price
                if on_risk_side:
                    stop_side_level_touches += touches
        evidence.append(
            VerdictEvidence(
                label="stop_side_level_touches",
                value=float(stop_side_level_touches),
            )
        )
        chain = self.reasoning_chain
        for label in ("trend", "momentum", "volatility", "volume", "support_resistance"):
            text = getattr(chain, label, "") or ""
            if text.strip():
                evidence.append(VerdictEvidence(label=label, text=text.strip()))

        invalidation = stated_soft_exit(self.thesis_invalid_if)
        if not invalidation and direction != "neutral" and self.stop_loss is not None:
            side = "below" if direction == "bullish" else "above"
            invalidation = (
                f"close {side} stop {self.stop_loss} (hard stop; the analyst stated no separate soft invalidation)"
            )
        return AnalystVerdict(
            seat="technical",
            symbol=self.symbol,
            direction=direction,
            magnitude=rating_rung_strength(self.rating),
            conviction=self.conviction,
            evidence=evidence,
            invalidation=invalidation,
        )

    @field_validator("symbol")
    @classmethod
    def normalize_symbol(cls, value: str) -> str:
        return _normalize_symbol(value)

    @model_validator(mode="before")
    @classmethod
    def _normalize_enum_case(cls, values):
        return _normalize_enum_case_fields(
            values,
            lower_fields=("rating", "conviction"),
        )

    @model_validator(mode="after")
    def _validate_rating_price_consistency(self):
        """Check the rating's price geometry; RECORD absent fields.

        - Actionable rating with a missing entry, stop, setup_type, horizon or
          falsifier: kept, the gap recorded in `missing_levels` (2026-10-12,
          XOM dropped at 9:35 ET for a missing entry). Sizing uses the live
          price; the stop is read from the bars.
        - A stop on the WRONG side of a present entry is a contradiction, not
          an absence: still rejected.
        - Neutral: prices cleared to avoid stale hints.
        """
        if self.rating == "neutral":
            # Coerce to None — PM's template would otherwise print stale numbers.
            self.__dict__["entry_price"] = None
            self.__dict__["reference_target"] = None
            self.__dict__["stop_loss"] = None
            self.__dict__["setup_type"] = None
            self.__dict__["expected_horizon_sessions"] = None
            self.__dict__["missing_levels"] = []
            return self

        missing: list[str] = []
        if self.entry_price is None or self.entry_price <= 0:
            self.__dict__["entry_price"] = None
            missing.append("entry_price")
        if self.stop_loss is None or self.stop_loss <= 0:
            self.__dict__["stop_loss"] = None
            missing.append("stop_loss")
        if self.entry_price is not None and self.stop_loss is not None:
            if self.rating in ("buy", "strong_buy"):
                if self.stop_loss >= self.entry_price:
                    raise ValueError(
                        f"{self.symbol}: BUY stop_loss {self.stop_loss} must be below entry {self.entry_price}"
                    )
            elif self.stop_loss <= self.entry_price:  # sell / strong_sell — stop (buy-back) above entry
                raise ValueError(
                    f"{self.symbol}: SELL stop_loss {self.stop_loss} must be above entry {self.entry_price}"
                )

        # A missing or non-positive analyst target is RECORDED as "no
        # target", not rejected (owner rule 2026-10-09): the take-profit is
        # derived from measured structure (`derive_structural_target`) and
        # the stop never reads this number.
        if self.reference_target is not None and self.reference_target <= 0:
            self.__dict__["reference_target"] = None
        # A wrong-side guess is RECORDED, not rejected (owner rule
        # 2026-10-09). Only checked when a target and an entry both exist.
        wrong_side = False
        if self.reference_target is not None and self.entry_price is not None:
            if self.rating in ("buy", "strong_buy"):
                wrong_side = self.reference_target <= self.entry_price
            else:
                wrong_side = self.reference_target >= self.entry_price
        self.__dict__["reference_target_wrong_side"] = bool(wrong_side)
        # No AI-named support/resistance is RECORDED as "no level named"
        # (owner rule 2026-10-09); stops and take-profit come from
        # `computed_levels`. setup_type, horizon and the "I'll sell if"
        # falsifier are RECORDED when absent too (2026-10-12): they are
        # nullable on the wire, and dropping the whole idea for them lost
        # XOM. Nothing here invents a value for any of them.
        if self.setup_type is None:
            missing.append("setup_type")
        if self.expected_horizon_sessions is None or self.expected_horizon_sessions <= 0:
            self.__dict__["expected_horizon_sessions"] = None
            missing.append("expected_horizon_sessions")
        if self.rating in ACTIONABLE_TECH_RATINGS and missing_stated_falsifier(self.thesis_invalid_if):
            missing.append("thesis_invalid_if")
        self.__dict__["missing_levels"] = missing
        return self

    @property
    def missing_levels_text(self) -> str | None:
        """One plain PM-prompt line naming the absent fields, or None."""
        if not self.missing_levels:
            return None
        line = f"levels missing: {', '.join(self.missing_levels)}"
        if "entry_price" in self.missing_levels or "stop_loss" in self.missing_levels:
            line += " — the desk will size from the live price and derive the stop from the bars"
        return line
