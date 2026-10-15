from datetime import datetime
from typing import Literal
from pydantic import BaseModel, Field, field_validator, model_validator
from src.models.base import LLMOutputModel, _normalize_enum_case_fields, _normalize_symbol


class EveningReasoningChain(LLMOutputModel):
    """Seven-step chain evening analyst must fill before emitting the report.

    Depth parallel to PM's 7-step and position_reviewer's 6-step chains.
    Empty strings fail validation — the agent cannot skip a step. Gives
    evening the same thought-depth structure as other LLM agents so its
    decisions are auditable, not just narrative.

    Design note (2026-04 upgrade): the previous 6-step chain was
    structurally anchored on DAILY cycles (yesterday's outlook, today's
    tape, tomorrow's preparation). For a medium-long-term investor, the
    most important question — "how is each held thesis playing out over
    the past 6-8 weeks?" — wasn't being asked anywhere. `thesis_health_
    review` is that missing step, and it sits between the retrospective
    (what happened) and the decision-quality review (how did we react).
    """

    performance_attribution: str = Field(min_length=1)
    """What drove today's P&L? Which positions contributed + / −, which macro /
    news factors explain the moves. Concrete, not vague."""

    outlook_retrospection: str = Field(min_length=1)
    """Honest grade of yesterday's tomorrow_outlook vs today's actual. If
    yesterday said bullish and today ripped down, say so. Calibration > saving
    face. Cross-reference specific predictions to specific outcomes."""

    thesis_health_review: str = Field(min_length=1)
    """For each held position: given 6-8 weeks of fundamentals evolution
    (earnings trajectory, macro sector stance, news flow, tech rating
    history), is the ORIGINAL entry thesis strengthening, still intact,
    weakening, or broken? This is the step that makes the agent a value
    investor not a swing trader. For holdings where the thesis is
    broken — flag them for SELL consideration tomorrow even if price
    hasn't yet moved. For holdings where the thesis is strengthening
    but price hasn't caught up — flag them as add-more candidates.
    Price noise is not thesis noise; conflating them is the main way
    medium-long-term strategies go wrong."""

    decision_quality_review: str = Field(min_length=1)
    """BUY / SELL / HOLD decisions today + the last few days. Pattern check:
    are you selling winners too early? Buying near tops? Hedging at the wrong
    time? Name the pattern if one exists."""

    calibration_meta: str = Field(min_length=1)
    """Zoom out on your recent bias / conviction track record (surfaced in the
    prompt). Are you systematically too bullish? Does HIGH conviction actually
    outperform LOW? This is the meta-loop — learning from your own accuracy
    not just yesterday's single call."""

    market_regime_read: str = Field(min_length=1)
    """Where is the market now, where's it going, what's the key evidence from
    today's tape + news. This is the foundation the tomorrow_bias rests on."""

    tomorrow_preparation: str = Field(min_length=1)
    """Key events tomorrow (earnings, econ data, Fed), levels to watch, how
    today's action shapes tomorrow's posture. What PM needs to know at 09:30."""


# Thesis-trajectory classifier for trade grading — the 2nd dimension that
# separates "swing trader" feedback from "value investor" feedback. A buy
# can be down 10% with the thesis still intact (noise); a buy can be up 10%
# with the thesis broken (momentum, not value). Grade must weigh BOTH
# price AND thesis; this enum carries the latter.
ThesisTrajectory = Literal[
    "strengthening",  # new data since entry reinforces the thesis
    "intact",  # no new negative information, reasons still valid
    "weakening",  # some contrary data but thesis isn't yet broken
    "broken",  # thesis invalidated by hard data (earnings miss,
    # guidance cut, regulatory action, etc.)
]


class SellGrade(LLMOutputModel):
    """Structured grade of a single recent SELL — what evening judged right or
    wrong. PM / position reviewer can read aggregate counts to feed back into
    their SELL discretion.

    Grading is dual-axis: `grade` aggregates `price_outcome` (what the tape
    did since we sold) and `thesis_trajectory_at_sell` (whether we sold
    with thesis-justification or on nerves / noise). A defensible SELL
    is one where we exited a weakening/broken thesis, even if price
    subsequently bounced — we kept discipline. A `wrong` SELL is one
    where we exited an intact/strengthening thesis AND price ran.
    """

    symbol: str
    sell_date: str  # "YYYY-MM-DD"
    sell_price: float
    current_price: float
    pct_move_since_sell: float
    grade: Literal["correct", "premature", "wrong"]
    reason: str = Field(min_length=1)
    # 2nd dimension added 2026-04 (value-lens upgrade). Optional so
    # pre-upgrade rows still parse, but the evening prompt now requires
    # LLM to fill it for every new grade it emits.
    thesis_trajectory_at_sell: ThesisTrajectory | None = None

    @field_validator("symbol")
    @classmethod
    def _sym(cls, v: str) -> str:
        return _normalize_symbol(v)

    @model_validator(mode="before")
    @classmethod
    def _normalize_enum_case(cls, values):
        return _normalize_enum_case_fields(
            values,
            lower_fields=("grade", "thesis_trajectory_at_sell"),
        )


# Root-cause taxonomy for losing BUYs. Used by evening_analyst when a
# buy_grade is "wrong" so the quarterly meta-reflector can aggregate
# patterns ("3 of our last 10 wrongs were greed_top_chasing → tech_analyst
# prompt needs an ATR-upper-band guard"). Ordering below mirrors priority
# for tie-breaking when multiple apply: self-inflicted root causes first,
# systemic / unavoidable ones last (don't let the LLM default to the easy
# "tail_event" out).
BuyLossRootCause = Literal[
    "greed_top_chasing",  # entered near top, momentum chased, no margin of safety
    "macro_warning_ignored",  # macro/news signals warned, we ignored (must cite evidence)
    "herd_buying",  # bought because news was loud, no independent thesis
    "averaged_down",  # added to loser past stop discipline
    "thesis_broken_held",  # thesis invalidated by data but we didn't sell
    "concentration_blow",  # single sector/theme overweight turned
    "timing_mistake",  # thesis correct, timing off — least-blameworthy class
    "systemic_drawdown",  # broad market fell; we fell with it (not alpha destruction)
    "tail_event",  # real black-swan; rare; LLM should resist defaulting here
]


class BuyGrade(LLMOutputModel):
    """Structured grade of a recent BUY — did the entry play out?
    Mirrors SellGrade so the feedback loop is symmetric.

    Like SellGrade, grading is dual-axis. `grade` aggregates price
    action AND `thesis_trajectory` (how the underlying fundamentals /
    theme have evolved since entry). A buy can be down 8% with thesis
    strengthening — that's NOT wrong, that's value entry being tested
    by noise. A buy can be up 10% with thesis broken — that's NOT
    correct, that's momentum masking a real failure."""

    symbol: str
    buy_date: str
    buy_price: float
    current_price: float
    pct_move_since_buy: float
    grade: Literal["correct", "premature", "wrong"]
    reason: str = Field(min_length=1)
    # 2nd grading dimension. Optional for back-compat; prompt requires it
    # on all new grades.
    thesis_trajectory: ThesisTrajectory | None = None
    # Loss-autopsy fields: required only when grade == "wrong". Evening analyst
    # must classify WHY a losing BUY lost so quarterly meta-reflection can
    # aggregate patterns and propose targeted prompt edits. Optional on
    # correct/premature so existing fixtures stay valid.
    loss_root_cause: BuyLossRootCause | None = None
    # SPY return over the same window as pct_move_since_buy. Python-injected
    # by the pipeline before passing to the LLM. Positive number when we
    # under-performed the market (alpha destruction); ~0 or negative when
    # the whole market fell (systemic). Lets the LLM distinguish greed_top_chasing
    # from systemic_drawdown without pattern-matching prose.
    market_relative_move_pct: float | None = None
    # Required when loss_root_cause == "macro_warning_ignored": the specific
    # warning that was visible at entry and dismissed. Format expected:
    # "<agent> <date> <conviction>: <headline>" — evidence, not vibes.
    missed_warning_ref: str | None = None

    @field_validator("symbol")
    @classmethod
    def _sym(cls, v: str) -> str:
        return _normalize_symbol(v)

    @model_validator(mode="before")
    @classmethod
    def _normalize_enum_case(cls, values):
        return _normalize_enum_case_fields(
            values,
            lower_fields=("grade", "thesis_trajectory", "loss_root_cause"),
        )

    @model_validator(mode="after")
    def _loss_fields_required(self) -> "BuyGrade":
        if self.grade == "wrong" and self.loss_root_cause is None:
            raise ValueError(
                "BuyGrade with grade='wrong' requires loss_root_cause so the "
                "quarterly meta-reflector can aggregate patterns"
            )
        # A 'wrong' grade also needs thesis_trajectory so position_reviewer
        # can distinguish "bought expensive" (intact thesis, price-only
        # mistake — re-entry candidate when price comes back) from
        # "fundamentals broke" (broken thesis — stay out). Without both
        # fields together, the loss-autopsy loop loses half its information
        # and the next-day prompt can't apply the value-investor lens.
        # Optional on correct/premature for back-compat.
        if self.grade == "wrong" and self.thesis_trajectory is None:
            raise ValueError(
                "BuyGrade with grade='wrong' requires thesis_trajectory so "
                "position_reviewer can distinguish a value re-entry candidate "
                "(intact thesis) from a stay-out signal (broken thesis)"
            )
        if self.loss_root_cause == "macro_warning_ignored" and not (self.missed_warning_ref or "").strip():
            raise ValueError(
                "loss_root_cause='macro_warning_ignored' requires missed_warning_ref "
                "citing the specific signal that was ignored (agent + date + headline)"
            )
        return self


class MissedOpportunitySnapshot(BaseModel):
    """Python-computed facts for one notable mover — INPUT to the evening LLM,
    not its output. The LLM reads a list of these and writes one
    MissedOpportunity per interesting row.

    Carries enough signal-state context (prior TA rating, recent news
    headline, earnings signal, macro sector stance) that the LLM's miss
    classification has to be grounded in observable prior evidence rather
    than price retro-rationalization.

    For symbols sourced from Alpaca's top-mover screener (not in our
    trading universe), the quality fields (avg_dollar_volume_20d_m,
    volume_confirmation_ratio, single_day_concentration_pct) are the
    main filter for "worth considering adding to universe" vs "low-
    volume squeeze we should ignore". A medium-long-term investor
    doesn't chase thin moves.
    """

    symbol: str
    move_pct: float
    window_days: int
    held_during_window: bool
    had_ta_signal: bool
    had_news_signal: bool
    had_earnings_signal: bool
    source: Literal["universe", "top_mover", "both"]
    # Optional evidence the LLM should cite in its `lesson`.
    last_ta_rating: str | None = None  # e.g. "hold" / "buy"
    last_ta_date: str | None = None  # ISO YYYY-MM-DD
    last_news_headline: str | None = None  # trimmed ≤ 140 chars upstream
    # Theme fingerprint the LLM can adopt in MissedOpportunity.theme_if_any.
    # Populated from recent news state_changes / earnings IIC tags.
    theme_tags: list[str] = []
    # Latest earnings-analyst take if this symbol reported in last ~90d.
    # Trimmed to ≤ 140 chars upstream. Lets the LLM flag
    # "fundamentals_mispricing" only when there's real fundamental backing.
    recent_earnings_signal: str | None = None
    # Macro's sector_guidance direction for this symbol's sector, recent call.
    # "unknown" = macro never covered the sector (itself a signal — blindspot).
    macro_sector_tailwind: Literal["bullish", "neutral", "bearish", "unknown"] = "unknown"

    # Quality metrics — primary lens for whether a top-mover deserves
    # watchlist consideration. Filled by Python from bar data; None when
    # insufficient bars to compute reliably.
    avg_dollar_volume_20d_m: float | None = None
    """20-day average daily dollar volume in MILLIONS of USD. Low numbers
    (< ~5M) indicate thin liquidity — easy to squeeze, dangerous for a
    medium-long-term position. Used to pre-filter very illiquid movers
    upstream; the LLM also sees it to reason about "real institutional
    interest vs low-volume drift"."""
    volume_confirmation_ratio: float | None = None
    """Today's dollar volume / 20-day avg. > ~1.5 indicates buyers
    showed up in size (real interest). < 1.0 = move happened on
    normal-or-less flow; unlikely to sustain."""
    single_day_concentration_pct: float | None = None
    """Percent of the window's total return that came from the BIGGEST
    single day. 0-100. > 70 = gap-up day (event / squeeze); < 50 =
    distributed move (trend). For a medium-long-term investor, a
    distributed trend is far more interesting than a single gap."""

    # Valuation context (2026-04 upgrade — value-lens). Yahoo data via
    # MarketDataProvider.get_valuation_metrics. None when ETF / not
    # available. The LLM should not chase stretched-PE symbols even if
    # they pass the quality bars.
    trailing_pe: float | None = None
    forward_pe: float | None = None
    ps_ratio: float | None = None
    valuation_signal: Literal["cheap", "fair", "stretched", "no_data"] = "no_data"
    """Rough forward-PE-based classifier filled by Python upstream.
    < 12 → cheap, 12-25 → fair, >= 25 → stretched, None → no_data.
    Thresholds are deliberately crude — the LLM reads raw PE numbers
    too and makes sector-adjusted judgments. `valuation_signal` is
    just a fast first cut that prevents obvious hype chasing."""

    # Bidirectional opportunity framing. Default False; set True by
    # digest when move_pct < -8% AND there is intact fundamental/theme
    # signal — classic "price panicked, thesis didn't" value dip.
    value_entry_candidate: bool = False

    @field_validator("symbol")
    @classmethod
    def _sym(cls, v: str) -> str:
        return _normalize_symbol(v)


class MissedOpportunity(LLMOutputModel):
    """Evening-analyst OUTPUT for one snapshot: classified miss + lesson +
    (for non-universe symbols) watchlist-addition recommendation.

    `miss_category` frames the miss through the three lenses the user cares
    about: catching trends, not missing themes, spotting fundamental
    mispricing. `noise_rally` and `risk_disciplined` are escape hatches so
    the LLM isn't forced to label every price move as a miss — but the
    prompt has to push back when they're overused.

    For symbols sourced from the top-mover screener (not in the trading
    universe), `universe_addition_recommendation` is the high-bar answer
    to "should we add this to the 77-symbol universe we carefully curated?"
    Default is "no" — the universe is deliberately small; thin or
    one-day-gap moves should not expand it. "add" only when volume,
    sustain, theme, and fundamentals all point in the right direction.
    """

    symbol: str
    move_pct: float
    miss_category: Literal[
        "trend_timing_miss",  # trend visible, entry late or absent
        "theme_blindspot",  # entire theme/sector uncovered by our agents
        "fundamentals_mispricing",  # hard earnings numbers, price not yet reacting
        "value_entry_missed",  # stock DOWN >=8% with thesis intact, we
        # didn't add — classic value dip missed
        "noise_rally",  # no signal, legitimate HOLD — not a real miss
        "risk_disciplined",  # RM / hard-rule blocked, accepted — not a real miss
    ]
    # Free-form theme label the LLM picks (e.g. "AI-capex", "nuclear/power",
    # "rare-earth", "reshoring"). Required for trend / theme / mispricing /
    # value categories so the quarterly digest can aggregate. None when
    # miss_category is noise_rally / risk_disciplined.
    theme_if_any: str | None = None
    # Theme duration classifier — "looks excellent" is not enough for the
    # user's 77-symbol universe; we want to distinguish a 2-month hype
    # cycle from a decade-long secular trend. Required when theme_if_any
    # is set; optional otherwise.
    theme_durability: Literal[
        "multi_year_secular",  # decade+ structural trend (AI capex, energy
        # transition, aging demographics)
        "1_3_year_cycle",  # cyclical opportunity (rate cuts, capex
        # cycle, inventory correction)
        "months_fad",  # short-lived hype (meme, single-event pop,
        # narrative rotation)
        "unknown",  # not enough information to classify
    ] = "unknown"
    lesson: str = Field(min_length=1, max_length=400)
    # Watchlist-addition recommendation (only meaningful for top-mover sources;
    # default "no" for universe symbols since they're already tracked).
    # High bar: "add" requires documented, multi-factor justification —
    # volume confirmation + multi-day sustain + theme/fundamental anchor +
    # reasonable valuation.
    universe_addition_recommendation: Literal["add", "watch", "no"] = "no"
    universe_addition_reason: str = Field(default="", max_length=400)
    """1-2 sentences citing the QUALITY metrics (volume, sustain, theme,
    fundamentals, valuation) that justify a non-'no' recommendation.
    Required when recommendation is "add" or "watch"; must stay empty
    when "no" so the reason field doesn't drift into wishful thinking."""

    @field_validator("symbol")
    @classmethod
    def _sym(cls, v: str) -> str:
        return _normalize_symbol(v)

    # A real miss with no clear theme is a valid finding: the model must not be
    # pushed to invent a theme. Consumers bucket an empty theme as "no theme".

    # audit round 2 #31: the former `_theme_durability_required_when_themed`
    # validator (raise when theme_if_any set and theme_durability is None)
    # was provably unreachable dead code: theme_durability is a non-Optional
    # Literal with default "unknown", so an omitted field silently becomes
    # "unknown" and an explicit null used to fail FIELD-level Literal
    # validation before any mode="after" model validator could run. Deleted
    # rather than "wired" — the docstring above explicitly permits "unknown"
    # as an allowed (if rare) value, so raising on it would contradict the
    # schema contract and get whole entries dropped by the evening pre-filter.
    #
    # UPDATED 2026-09-02: that last sentence turned out to describe what was
    # ALREADY happening. The field-level rejection this note treats as
    # incidental was dropping 25 of every 50 entries in production, for
    # exactly the reason the note gives as the argument against raising.
    # `LLMOutputModel._explicit_null_means_absent` now reads the null as an
    # absent key, so a nulled durability becomes "unknown" and the entry
    # survives. The validator stays deleted; the test that pinned the old
    # mechanism was inverted rather than removed
    # (tests/test_agents_audit_round2.py::
    #  test_idx31_explicit_null_durability_is_now_unknown_not_a_dropped_entry).

    @model_validator(mode="after")
    def _addition_recommendation_consistency(self) -> "MissedOpportunity":
        # "add" / "watch" require a concrete reason; "no" forbids one so
        # the field doesn't become a dumping ground for weak opinions.
        if self.universe_addition_recommendation in ("add", "watch"):
            if not (self.universe_addition_reason or "").strip():
                raise ValueError(
                    f"universe_addition_recommendation="
                    f"'{self.universe_addition_recommendation}' requires "
                    f"universe_addition_reason citing volume, sustain, or "
                    f"theme quality — bar is high, evidence must be concrete"
                )
        return self


class EveningReport(LLMOutputModel):
    # Three Literal enums (risk_rating + tomorrow_bias + tomorrow_conviction)
    # are case-folded BEFORE Pydantic validates — LLMs occasionally drift to
    # uppercase variants like "MODERATE" which would otherwise reject the
    # whole evening output. See _normalize_enum_case_fields docstring.
    @model_validator(mode="before")
    @classmethod
    def _normalize_enum_case(cls, values):
        return _normalize_enum_case_fields(
            values,
            lower_fields=(
                "risk_rating",
                "tomorrow_bias",
                "tomorrow_conviction",
            ),
        )

    reasoning_chain: EveningReasoningChain
    daily_summary: str = Field(min_length=1)
    lessons: str = Field(min_length=1)
    tomorrow_outlook: str = Field(min_length=1)  # prose narrative for PM context
    risk_rating: Literal["low", "moderate", "elevated", "high"]
    suggested_actions: list[str] = []
    # Outlook-vs-reality retrospection — was yesterday's tomorrow_outlook right?
    previous_outlook_assessment: str = ""
    # Structured version of tomorrow_outlook so PM can act on it deterministically
    # instead of re-parsing prose. PM tilts base sizing ±20% on the bias/conviction
    # pair at morning open.
    tomorrow_bias: Literal["bullish", "neutral", "bearish"] = "neutral"
    tomorrow_conviction: Literal["high", "medium", "low"] = "medium"
    tomorrow_key_risks: list[str] = []
    # SELL discipline feedback loop — prose summary retained for narrative
    # continuity + backward compat.
    sell_decisions_assessment: str = ""
    # Structured per-trade grades. PM / position reviewer can compute aggregate
    # stats ("last 14d: correct 5 / premature 3 / wrong 1") from these without
    # parsing prose. Empty list = no grades this session (no recent trades or
    # LLM skipped). Both lists are filled by the LLM from the `recent_*`
    # tables surfaced in the prompt.
    sell_grades: list[SellGrade] = []
    buy_grades: list[BuyGrade] = []
    # What we missed today — up to ~15 entries, one per notable mover not
    # owned during the window. Empty when no universe/top-mover symbols
    # crossed the move_threshold_pct. Feeds next-day PM's L3d memory and
    # the quarterly meta-reflector's theme_coverage_report.
    missed_opportunities: list[MissedOpportunity] = []

    # Medium-term thesis catalysts (2026-04 value-lens upgrade) —
    # complements `tomorrow_key_risks` with a this-week / next-week view
    # on events that would confirm or break held theses. Examples:
    # "NVDA reports Q1 earnings Thu after close", "FOMC minutes next Wed
    # — rate-sensitive sleeves at risk", "MU guidance cut window if
    # memory ASP data disappoints". 0-6 entries, each specific.
    this_week_thesis_catalysts: list[str] = []

    # Structured lesson categories (2026-04 value-lens upgrade). The
    # prose `lessons` field is retained for back-compat / continuity,
    # but downstream agents prefer these three lists when they exist:
    # - thesis_updates: specific held-position thesis changes ("NVDA
    #   thesis strengthening — data-center capex Q1 guide +18%").
    # - selection_rules: new stock-selection insights ("on theme plays,
    #   require ≥2 confirming fundamental prints before sizing >5%").
    # - discipline_notes: behavioral / process reminders ("stop cutting
    #   GOOGL on single-day -2% wobbles; 5 of 7 recent sells premature").
    # All optional; LLM may fill one, two, or all three depending on
    # the day.
    thesis_updates: list[str] = []
    selection_rules: list[str] = []
    discipline_notes: list[str] = []


class AgentLog(BaseModel):
    agent_name: str
    run_id: str
    timestamp: datetime
    input_summary: str
    output_summary: str
    full_response: str
    model: str
    tokens_used: int
