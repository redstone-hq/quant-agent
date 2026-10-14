"""Phase 13 — the shared analyst verdict shape, Technical's mapping onto it,
and the Portfolio Manager's deterministic ranking over it.

Three things are pinned here:

1. `AnalystVerdict` refuses an incomplete verdict — a directional call with
   no invalidation condition or no evidence is not a verdict.
2. `TechAnalysisResult.to_verdict` is a RESTATEMENT of a real technical
   read (including the production-shaped response `tests/test_tech_analyst`
   uses), not a second opinion.
3. `PortfolioManagerAgent.rank_candidates` produces a deterministic order
   among equally-eligible candidates and never orders a name a gate refused.
   The eligibility port is cross-checked against the item-18 audit script on
   the real run-64290730 fixture, so the two cannot drift apart silently.
"""

from __future__ import annotations

import ast
import json
from datetime import date
from pathlib import Path

import pytest
from pydantic import ValidationError

from ops.model_policy import scenarios as S
from ops.model_policy.deterministic_selection import evaluate
from src.agents.portfolio_manager import PortfolioManagerAgent

# The level-less 2026-09-01 day this audit's numbers were measured on.
# `pm_selection` itself moved to run-bba4d4f3 on 2026-09-14; this file
# stays pinned to the day its write-up describes.
_LEVEL_LESS = S.load_frozen_selection(S.LEVEL_LESS_SELECTION_FIXTURE)
from src.models import (
    NO_STATED_STRENGTH,
    RATING_DIRECTION,
    RATING_MAGNITUDE,
    AnalystVerdict,
    EarningsAnalysis,
    NewsIntelligenceReport,
    StockNewsItem,
    TechAnalysisResult,
    TechReasoningChain,
    VerdictEvidence,
)
from src.risk.constants import REWARD_RISK_FLOOR
from src.verdicts import (
    CONVICTION_SCORE,
    RANKING_SIGNALS,
    SEAT_WEIGHT,
    level_touches_of,
    rank_verdicts,
    score_verdict,
    seat_weight,
)

REPO = Path(__file__).parent.parent
SESSION = date(2026, 9, 1)


def _chain() -> TechReasoningChain:
    return TechReasoningChain(
        trend="above MA20/50/200",
        momentum="RSI 58, MACD positive",
        volatility="mid-band",
        volume="+15% confirms",
        support_resistance="support 95, resistance 112",
    )


def _tech(
    symbol: str,
    rating: str = "buy",
    conviction: str = "medium",
    invalid_if: str = "closes below MA50 on volume",
    target: float | None = None,
) -> TechAnalysisResult:
    if rating == "neutral":
        return TechAnalysisResult(
            symbol=symbol,
            rating="neutral",
            conviction=conviction,
            reasoning="no setup",
            reasoning_chain=_chain(),
            thesis_invalid_if=invalid_if,
        )
    long = rating in ("buy", "strong_buy")
    # 100 / 95 / 112 is R/R 2.40 long; 100 / 105 / 88 is 2.40 short.
    return TechAnalysisResult(
        symbol=symbol,
        rating=rating,
        conviction=conviction,
        entry_price=100,
        stop_loss=95 if long else 105,
        reference_target=(target if target is not None else (112 if long else 88)),
        support_levels=[95] if long else [88],
        resistance_levels=[112] if long else [105],
        setup_type="range",
        expected_horizon_sessions=10,
        reasoning="one-line why",
        reasoning_chain=_chain(),
        thesis_invalid_if=invalid_if,
    )


# ==========================================================================
# 1. The shape
# ==========================================================================


def _evidence() -> list[VerdictEvidence]:
    return [VerdictEvidence(label="stop_loss", value=95.0)]


def test_a_complete_directional_verdict_validates():
    v = AnalystVerdict(
        seat="technical",
        symbol="aapl",
        direction="bullish",
        magnitude=0.5,
        conviction="medium",
        evidence=_evidence(),
        invalidation="closes below 95",
    )
    assert v.symbol == "AAPL"
    assert v.signed_magnitude == 0.5


def test_a_bearish_verdict_has_a_negative_signed_magnitude():
    v = AnalystVerdict(
        seat="technical",
        symbol="AAPL",
        direction="bearish",
        magnitude=1.0,
        conviction="high",
        evidence=_evidence(),
        invalidation="closes above 105",
    )
    assert v.signed_magnitude == -1.0


@pytest.mark.parametrize("invalidation", ["", "   ", None])
def test_a_directional_verdict_without_an_invalidation_is_refused(invalidation):
    with pytest.raises(ValidationError, match="invalidation"):
        AnalystVerdict(
            seat="technical",
            symbol="AAPL",
            direction="bullish",
            magnitude=0.5,
            conviction="medium",
            evidence=_evidence(),
            invalidation=invalidation,
        )


def test_a_directional_verdict_without_evidence_is_refused():
    with pytest.raises(ValidationError, match="evidence"):
        AnalystVerdict(
            seat="technical",
            symbol="AAPL",
            direction="bullish",
            magnitude=0.5,
            conviction="medium",
            evidence=[],
            invalidation="closes below 95",
        )


def test_a_neutral_verdict_may_be_blank_but_may_not_lean():
    neutral = AnalystVerdict(
        seat="technical",
        symbol="AAPL",
        direction="neutral",
        magnitude=0.0,
        conviction="low",
    )
    assert neutral.signed_magnitude == 0.0
    with pytest.raises(ValidationError, match="neutral"):
        AnalystVerdict(
            seat="technical",
            symbol="AAPL",
            direction="neutral",
            magnitude=0.3,
            conviction="low",
        )


def test_magnitude_is_bounded_to_the_unit_interval():
    for bad in (-0.1, 1.1):
        with pytest.raises(ValidationError):
            AnalystVerdict(
                seat="technical",
                symbol="AAPL",
                direction="bullish",
                magnitude=bad,
                conviction="medium",
                evidence=_evidence(),
                invalidation="x",
            )


def test_evidence_must_carry_something_checkable():
    with pytest.raises(ValidationError, match="checkable"):
        VerdictEvidence(label="trend")
    VerdictEvidence(label="trend", text="above MA20")
    VerdictEvidence(label="event", as_of=date(2026, 8, 31))
    VerdictEvidence(label="stop", value=95.0)


def test_the_shape_has_exactly_the_four_judgement_fields_plus_identity():
    """The spec names four things. Identity (seat, symbol) is not judgement.
    A fifth judgement field is a design change and should fail here first."""
    assert set(AnalystVerdict.model_fields) == {
        "seat",
        "symbol",
        "direction",
        "magnitude",
        "conviction",
        "evidence",
        "invalidation",
    }


# ==========================================================================
# 2. Technical's mapping
# ==========================================================================


def test_rating_encodings_are_equal_spaced_and_symmetric():
    assert RATING_MAGNITUDE == {
        "strong_buy": 1.0,
        "buy": 0.5,
        "neutral": 0.0,
        "sell": 0.5,
        "strong_sell": 1.0,
    }
    assert RATING_DIRECTION["buy"] == RATING_DIRECTION["strong_buy"] == "bullish"
    assert RATING_DIRECTION["sell"] == RATING_DIRECTION["strong_sell"] == "bearish"
    assert RATING_DIRECTION["neutral"] == "neutral"


def test_tech_read_restates_onto_the_verdict_without_inventing_anything():
    a = _tech("NVDA", "strong_buy", "high", invalid_if="closes below MA50 on volume")
    v = a.to_verdict()
    assert v.seat == "technical"
    assert v.symbol == "NVDA"
    assert (v.direction, v.magnitude, v.conviction) == ("bullish", 1.0, "high")
    assert v.invalidation == "closes below MA50 on volume"
    by_label = {}
    for e in v.evidence:
        by_label.setdefault(e.label, []).append(e)
    assert by_label["entry_price"][0].value == 100.0
    assert by_label["stop_loss"][0].value == 95.0
    assert by_label["reference_target"][0].value == 112.0
    assert by_label["risk_reward"][0].value == a.risk_reward == 2.4
    assert [e.value for e in by_label["support_level"]] == [95.0]
    assert [e.value for e in by_label["resistance_level"]] == [112.0]
    for step in ("trend", "momentum", "volatility", "volume", "support_resistance"):
        assert by_label[step][0].text == getattr(a.reasoning_chain, step)


def test_a_short_read_maps_to_a_bearish_verdict():
    v = _tech("NKE", "sell", "medium").to_verdict()
    assert (v.direction, v.magnitude, v.signed_magnitude) == ("bearish", 0.5, -0.5)


def test_a_neutral_read_maps_to_a_neutral_verdict_with_no_lean():
    v = _tech("QQQ", "neutral", invalid_if="").to_verdict()
    assert (v.direction, v.magnitude, v.invalidation) == ("neutral", 0.0, "")


def test_a_missing_soft_invalidation_falls_back_to_the_analysts_own_stop():
    """Live parse refuses an actionable read with a blank falsifier. A
    historical row (model_construct) still restates the analyst's own
    stop and SAYS so — nothing is invented and the verdict still
    validates."""
    # Recorded as a gap, not rejected, since 2026-10-12 (XOM incident).
    assert _tech("AAPL", "buy", invalid_if="").missing_levels == ["thesis_invalid_if"]
    historical = TechAnalysisResult.model_construct(
        **_tech("AAPL", "buy").model_dump() | {"thesis_invalid_if": ""},
    )
    v = historical.to_verdict()
    assert "below stop 95" in v.invalidation
    assert "hard stop" in v.invalidation
    short = TechAnalysisResult.model_construct(
        **_tech("AAPL", "sell").model_dump() | {"thesis_invalid_if": ""},
    )
    assert "above stop 105" in short.to_verdict().invalidation


def test_the_production_shaped_tech_response_populates_the_verdict():
    """The exact response body `tests/test_tech_analyst.py` feeds the
    agent, parsed the way `_analyze_chunk` parses it."""
    body = json.loads(
        json.dumps(
            [
                {
                    "symbol": "SPY",
                    "rating": "buy",
                    "conviction": "high",
                    "entry_price": 507.0,
                    "reference_target": 530.0,
                    "stop_loss": 494.0,
                    "support_levels": [494.0],
                    "resistance_levels": [530.0],
                    "setup_type": "range",
                    "expected_horizon_sessions": 10,
                    "thesis_invalid_if": "Price closes below MA50 (492) on above-average volume",
                    "reasoning_chain": {
                        "trend": "Above MA20/50/200 stacked bullish.",
                        "momentum": "RSI 58 neutral-bullish, MACD hist positive.",
                        "volatility": "Mid-band, ATR steady.",
                        "volume": "+15% confirms uptrend.",
                        "support_resistance": "Support MA50 498, resistance upper band 520.",
                    },
                    "reasoning": "Clean bullish alignment.",
                }
            ]
        )
    )
    v = TechAnalysisResult(**body[0]).to_verdict()
    assert (v.direction, v.magnitude, v.conviction) == ("bullish", 0.5, "high")
    assert v.invalidation.startswith("Price closes below MA50")
    assert {e.label for e in v.evidence} >= {
        "entry_price",
        "stop_loss",
        "reference_target",
        "risk_reward",
        "support_level",
        "resistance_level",
        "trend",
        "momentum",
    }


def test_every_real_technical_read_on_the_fixture_day_maps_to_a_valid_verdict():
    """59 production reads from run-64290730 — none may fail the shape."""
    verdicts = [a.to_verdict() for a in _LEVEL_LESS.analyses]
    assert len(verdicts) == len(_LEVEL_LESS.analyses)
    for a, v in zip(_LEVEL_LESS.analyses, verdicts):
        assert v.direction == RATING_DIRECTION[a.rating]
        assert v.conviction == a.conviction


# ==========================================================================
# 2b. Earnings' mapping
# ==========================================================================


def _earnings(
    sentiment: str = "bullish",
    conviction: str = "medium",
    bull_case: str = "services mix reaccelerates",
    bear_case: str = "China demand craters",
    data_quality: str = "complete filing, no estimates",
) -> EarningsAnalysis:
    return EarningsAnalysis(
        symbol="AAPL",
        form_type="10-Q",
        filing_date="2026-03-15",
        revenue={"total": "$95.4B"},
        profitability={},
        cash_flow={},
        balance_sheet={},
        guidance="flat",
        data_quality=data_quality,
        investment_implications={
            "sentiment": sentiment,
            "conviction": conviction,
            "key_thesis": "services mix offsets hardware softness",
            "bull_case": bull_case,
            "bear_case": bear_case,
            "reasoning_chain": {
                "fundamental_quality": "gross margin expanding 40bps",
                "growth_trajectory": "services +12% YoY, hardware flat",
                "strategic_risks": "vision pro adoption unproven",
                "management_execution": "buyback pace matches prior guide",
                "valuation_context": "premium holds only if services mix keeps rising",
            },
        },
    )


def test_a_bullish_earnings_read_maps_onto_the_verdict():
    v = _earnings("bullish", "high").to_verdict()
    assert v.seat == "earnings"
    assert v.symbol == "AAPL"
    # magnitude None: earnings states a direction and a conviction and no
    # strength scale of its own — see `NO_STATED_STRENGTH`. It reaches the
    # ranking through its weighted conviction, not through a borrowed rung,
    # and since item 65 (2026-09-26) the absence is None rather than a 0.0
    # the ranking would sum as if it were a reading.
    assert (v.direction, v.magnitude, v.conviction) == ("bullish", None, "high")
    assert v.invalidation == "China demand craters"
    by_label = {e.label: e for e in v.evidence}
    assert by_label["key_thesis"].text == "services mix offsets hardware softness"
    assert by_label["fundamental_quality"].text == "gross margin expanding 40bps"
    assert by_label["growth_trajectory"].text == "services +12% YoY, hardware flat"
    assert by_label["strategic_risks"].text == "vision pro adoption unproven"
    assert by_label["management_execution"].text == "buyback pace matches prior guide"
    assert by_label["valuation_context"].text == "premium holds only if services mix keeps rising"
    assert by_label["data_quality"].text == "complete filing, no estimates"


def test_a_bearish_earnings_read_uses_the_bull_case_as_its_invalidation():
    v = _earnings("bearish", "low").to_verdict()
    assert (v.direction, v.magnitude, v.conviction) == ("bearish", None, "low")
    assert v.invalidation == "services mix reaccelerates"
    assert v.signed_magnitude is None


def test_a_neutral_earnings_read_maps_to_a_neutral_verdict_with_no_lean():
    v = _earnings("neutral", "medium").to_verdict()
    assert (v.direction, v.magnitude, v.invalidation) == ("neutral", None, "")


def test_an_undisclosed_falsifier_is_treated_as_blank_not_as_content():
    """`bull_case`/`bear_case` default to the literal 'not disclosed' — that
    placeholder must not be passed through as if it were a real falsifier.
    With no numeric stop to fall back to (unlike Technical), a directional
    call with nothing disclosed against it has a blank invalidation, and
    `AnalystVerdict` itself refuses to construct with a directional call and
    no invalidation — the seat cannot invent a falsifier from nothing."""
    a = _earnings("bullish", "medium", bear_case="not disclosed")
    with pytest.raises(ValidationError, match="invalidation"):
        a.to_verdict()
    # Case-insensitive / whitespace-padded placeholder is caught too.
    a2 = _earnings("bearish", "medium", bull_case="  Not Disclosed  ")
    with pytest.raises(ValidationError, match="invalidation"):
        a2.to_verdict()


def test_data_quality_evidence_is_omitted_when_it_is_the_bare_default():
    v = _earnings("bullish", "medium", data_quality="not disclosed").to_verdict()
    assert "data_quality" not in {e.label for e in v.evidence}


# ==========================================================================
# 3. The ranking
# ==========================================================================


def test_ranking_reads_two_signals_at_equal_weight():
    assert RANKING_SIGNALS == ("magnitude", "conviction_score")
    assert CONVICTION_SCORE == {"low": 0.0, "medium": 0.5, "high": 1.0}


def test_seat_weight_is_the_ratified_research_informed_prior():
    """§13.3 AMENDED 2026-09-03, this module only. `src/risk/rules.py`'s
    sizing-path SEAT_WEIGHT is untouched and stays pinned at 1 — see
    `tests/test_signed_dissent.py`. This one may hold a per-seat prior
    because it only reorders already-eligible, already-agreeing candidates;
    it never changes how much money a trade risks."""
    assert SEAT_WEIGHT == {
        "technical": 1.2,
        "earnings": 1.2,
        "news": 1.0,
        "smart_money": 0.8,
        "macro": 0.8,
    }
    assert seat_weight("technical") == 1.2
    assert seat_weight("some_future_seat_not_yet_reviewed") == 1.0, (
        "an unreviewed seat must default to unweighted, never zero or missing"
    )


def test_only_one_seat_weight_table_exists_in_the_ranking_module():
    """A SECOND, undocumented seat-keyed numeric dict sneaking in alongside
    the ratified `SEAT_WEIGHT` would be an uncited, chosen weight — the
    exact thing §13.3 still forbids anywhere except the one reviewed table.

    Catches both a `{}` literal AND a `dict(seat=weight, ...)` call — a
    prior version of this guard only walked `ast.Dict` nodes and an
    adversarial review (2026-09-03) found `dict(technical=1.1, ...)` sailed
    straight through it undetected."""
    seats = {"technical", "news", "earnings", "macro", "smart_money"}
    tree = ast.parse((REPO / "src" / "verdicts.py").read_text())
    hits = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Dict):
            keys = {k.value for k in node.keys if isinstance(k, ast.Constant)}
            if keys & seats:
                hits.append(node.lineno)
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "dict":
            kw_keys = {kw.arg for kw in node.keywords if kw.arg is not None}
            if kw_keys & seats:
                hits.append(node.lineno)
    assert len(hits) == 1, f"expected exactly one seat-keyed table (SEAT_WEIGHT), found at lines {hits}"


def test_score_is_magnitude_plus_conviction():
    v = _tech("A", "strong_buy", "low").to_verdict()
    assert score_verdict(v) == 1.0
    v = _tech("A", "buy", "high").to_verdict()
    assert score_verdict(v) == 1.5
    v = _tech("A", "buy", "medium").to_verdict()
    assert score_verdict(v) == 1.0


def test_rank_verdicts_orders_by_score_then_symbol_and_skips_neutral():
    """Scores carry technical's 1.2 seat weight since 2026-09-13: the
    aggregation is a weighted SUM, so a lone seat's weight multiplies its own
    score instead of cancelling out of an average. Ordering is unchanged."""
    verdicts = [
        _tech("CCC", "buy", "medium").to_verdict(),  # 1.0 * 1.2
        _tech("AAA", "buy", "medium").to_verdict(),  # 1.0 * 1.2 — ties on symbol
        _tech("BBB", "strong_sell", "high").to_verdict(),  # 2.0 * 1.2
        _tech("DDD", "buy", "low").to_verdict(),  # 0.5 * 1.2
        _tech("EEE", "neutral", invalid_if="").to_verdict(),  # no lean — not scored
    ]
    ranked = rank_verdicts(verdicts)
    assert [c.symbol for c in ranked] == ["BBB", "AAA", "CCC", "DDD"]
    assert [c.score for c in ranked] == [2.4, 1.2, 1.2, 0.6]
    assert ranked[0].direction == "bearish"
    assert ranked[0].seats == ["technical"]
    # Pure: same input, same order.
    assert [c.symbol for c in rank_verdicts(verdicts)] == ["BBB", "AAA", "CCC", "DDD"]


def test_two_seats_on_one_symbol_sum_at_the_research_informed_weight():
    """Same fixture as the old unit-weight test, recomputed for the
    2026-09-13 change from a weighted AVERAGE to a weighted SUM. Technical
    enters at 1.2x, news at 1.0x, and each seat's contribution is ADDED."""
    tech = _tech("XLE", "buy", "high").to_verdict()  # 0.5 + 1.0, weight 1.2
    other = AnalystVerdict(
        seat="news",
        symbol="XLE",
        direction="bullish",
        magnitude=1.0,
        conviction="low",
        evidence=_evidence(),
        invalidation="x",  # 1.0 + 0.0, weight 1.0
    )
    [c] = rank_verdicts([tech, other])
    # magnitude: 0.5*1.2 + 1.0*1.0 = 1.6
    # conviction: 1.0*1.2 + 0.0*1.0 = 1.2
    # risk_reward_tiebreak: still a weighted MEAN, deliberately — it
    # aggregates several estimates of ONE quantity in a real unit. Only
    # technical carries risk_reward evidence (2.40, from its 100/95/112
    # fixture geometry); "news" contributes 0 weight, not 0 value:
    # (2.40*1.2) / 1.2 = 2.40. Tiebreak only — never folded into `score`.
    # level_touches_tiebreak: 0.0 — the `_tech` fixture sets no
    # `computed_level_touches`, and neither seat here is one that would
    # carry the evidence anyway besides technical (item 141, 2026-09-20).
    assert c.components == {
        "magnitude": 1.6,
        "conviction_score": 1.2,
        "risk_reward_tiebreak": 2.4,
        "level_touches_tiebreak": 0.0,
    }
    assert c.score == 2.8
    assert c.seats == ["news", "technical"]


def test_a_second_agreeing_seat_can_never_lower_a_candidates_score():
    """**The defect that forced the sum, found by adversarial review before
    PR #348 merged, 2026-09-13.** Under the weighted AVERAGE the arithmetic
    below produced 2.0 for the technical read alone and 1.8 once smart_money
    AGREED with it — the strongest thing that seat can say made the candidate
    rank lower. Agreement was dilutive, which contradicts the desk's own
    stated edge (breadth x consistency x asymmetry) and `docs/OUTCOME.md`
    §9.4's "agreement earns size".

    This is pinned as a PROPERTY, not as two magic numbers: for every subset
    of agreeing seats, adding one more must not reduce the score.
    """
    tech = _tech("XLE", "strong_buy", "high").to_verdict()
    agreeing = [
        AnalystVerdict(
            seat=seat,
            symbol="XLE",
            direction="bullish",
            magnitude=NO_STATED_STRENGTH,
            conviction=conviction,
            evidence=_evidence(),
            invalidation="x",
        )
        for seat, conviction in (
            ("smart_money", "high"),  # `actionable`, the strongest it states
            ("macro", "medium"),
            ("news", "low"),  # the weakest a seat can state
            ("earnings", "high"),
        )
    ]
    scores = []
    for i in range(len(agreeing) + 1):
        [c] = rank_verdicts([tech, *agreeing[:i]])
        scores.append(c.score)
    assert scores == sorted(scores), scores
    # And strictly higher wherever the added seat actually said something:
    # only the low-conviction news seat contributes exactly zero.
    assert scores[0] == 2.4  # technical alone, 2.0 * 1.2
    assert scores[1] > scores[0]  # + smart_money `actionable`
    assert scores[4] > scores[1]


def test_a_lone_seats_own_weight_now_scales_its_score():
    """Changed 2026-09-13 with the move from average to sum. Under the
    average a lone seat's weight cancelled out, so a lone macro read and a
    lone technical read of identical strength scored the same — which made
    the researched prior silently inert on every single-seat candidate, the
    majority of them. Under a sum the weight applies, so the seat this desk
    has the best-sourced reason to trust ranks above the one it does not."""
    tech = _tech("XLE", "buy", "high").to_verdict()
    [c] = rank_verdicts([tech])
    assert c.score == round(score_verdict(tech) * SEAT_WEIGHT["technical"], 4) == 1.8
    macro_like = AnalystVerdict(
        seat="macro",
        symbol="XLE",
        direction="bullish",
        magnitude=tech.magnitude,
        conviction=tech.conviction,
        evidence=_evidence(),
        invalidation="x",
    )
    [m] = rank_verdicts([macro_like])
    assert m.score == round(score_verdict(macro_like) * SEAT_WEIGHT["macro"], 4)
    assert c.score > m.score


def test_a_seat_that_looked_and_had_no_lean_is_recorded_not_dropped():
    """2026-09-13. A neutral verdict contributes nothing to the score — there
    is nothing to contribute — but "this seat looked and came back with
    nothing" is a different state from "this seat never looked", and until
    now the two were indistinguishable downstream. It matters because a macro
    read whose sector rows contradict each other now resolves to neutral."""
    tech = _tech("XLE", "buy", "high").to_verdict()
    quiet_macro = AnalystVerdict(
        seat="macro",
        symbol="XLE",
        direction="neutral",
        magnitude=0.0,
        conviction="medium",
        evidence=_evidence(),
        invalidation="",
    )
    [bare] = rank_verdicts([tech])
    [with_quiet] = rank_verdicts([tech, quiet_macro])
    assert bare.neutral_seats == []
    assert with_quiet.neutral_seats == ["macro"]
    assert with_quiet.score == bare.score  # contributes nothing to the score
    assert with_quiet.seats == ["technical"]  # and is not a scoring seat


def test_two_verdicts_from_the_same_seat_do_not_double_that_seats_weight():
    """Found by adversarial review, 2026-09-03: two earnings filings for one
    symbol in the same run (nothing upstream enforces one filing per symbol
    per run) used to push earnings' 1.2 weight from 50% of a two-seat group
    to 66.7%, silently. `rank_verdicts` must count at most ONE verdict per
    (symbol, seat) — last one wins, matching the same convention already
    used for the evidence registry."""
    tech = _tech("XLE", "buy", "high").to_verdict()  # weight 1.2
    first_earnings = AnalystVerdict(
        seat="earnings",
        symbol="XLE",
        direction="bullish",
        magnitude=0.2,
        conviction="low",
        evidence=_evidence(),
        invalidation="x",
    )
    second_earnings = AnalystVerdict(
        seat="earnings",
        symbol="XLE",
        direction="bullish",
        magnitude=0.9,
        conviction="high",
        evidence=_evidence(),
        invalidation="y",
    )
    [with_one] = rank_verdicts([tech, first_earnings])
    [with_duplicate] = rank_verdicts([tech, first_earnings, second_earnings])
    # The duplicate must be treated as a REPLACEMENT (last wins), not an
    # ADDITION — group size, and therefore each seat's share of the total
    # weight, must be identical whether one or two earnings verdicts arrive.
    assert with_one.seats == with_duplicate.seats == ["earnings", "technical"]
    [with_second_only] = rank_verdicts([tech, second_earnings])
    assert with_duplicate.score == with_second_only.score
    assert with_duplicate.score != score_verdict(tech)  # earnings still counts, just once


def test_seats_disagreeing_on_direction_are_not_ranked():
    tech = _tech("XLE", "buy", "high").to_verdict()
    other = AnalystVerdict(
        seat="news",
        symbol="XLE",
        direction="bearish",
        magnitude=1.0,
        conviction="high",
        evidence=_evidence(),
        invalidation="x",
    )
    assert rank_verdicts([tech, other]) == []


def test_tied_score_does_not_break_on_risk_reward():
    """**Inverted 2026-10-10 (owner ruling 9 Oct).** Originally 2026-09-04
    audit fix #2, which broke ties on reward:risk; that ratio's reward side
    is the take-profit target, not a sell rule and unscored, so it no longer
    orders anything. With no level touches either, the tie falls to symbol.
    The original rationale, kept for history: Real production data: ties on the coarse
    composite score are the COMMON case (one real day had 9 of 12 eligible
    names tied, another 23 of 33), and breaking on symbol alone gave every
    early-alphabet ticker a permanent, undisclosed edge on tie days. AAA and
    ZZZ tie exactly on score (1.0 = 0.5 magnitude + 0.5 conviction) here;
    ZZZ's fixture geometry (100/95/130) gives it a materially better R/R
    (7.0) than AAA's default (100/95/112, R/R 2.4). ZZZ must rank FIRST —
    the opposite of what a symbol-only tiebreak would do — because it is
    genuinely the better-supported call, not because of where its ticker
    falls in the alphabet."""
    aaa = _tech("AAA", "buy", "medium").to_verdict()  # R/R 2.4
    zzz = _tech("ZZZ", "buy", "medium", target=130).to_verdict()  # R/R 7.0
    assert score_verdict(aaa) == score_verdict(zzz) == 1.0
    ranked = rank_verdicts([aaa, zzz])
    assert [c.symbol for c in ranked] == ["AAA", "ZZZ"]
    # Still recorded, just not used to order.
    assert ranked[1].components["risk_reward_tiebreak"] > ranked[0].components["risk_reward_tiebreak"]


def test_a_verdict_without_risk_reward_evidence_carries_no_tiebreak_key():
    """**Changed 2026-09-11, docs/WORK.md item 1(d).** An absent reward:risk
    used to be recorded as 0.0, which sorted such a candidate behind every
    peer it tied with. Absent is not zero: the key is simply not there (and
    since 2026-10-10 the ratio orders nothing). It must still never crash
    the ranking."""
    v = AnalystVerdict(
        seat="news",
        symbol="AAA",
        direction="bullish",
        magnitude=0.5,
        conviction="medium",
        evidence=_evidence(),
        invalidation="x",
    )
    [c] = rank_verdicts([v])
    assert "risk_reward_tiebreak" not in c.components


def test_reward_risk_presence_or_size_does_not_order_a_tie_group():
    """**Inverted 2026-10-10 (owner ruling 9 Oct).** Neither having a
    reward:risk nor its size moves a candidate within a tied group: with no
    level touches, the group is ordered by symbol alone."""

    def _v(symbol: str, rr: float | None) -> AnalystVerdict:
        evidence = list(_evidence())
        if rr is not None:
            evidence.append(VerdictEvidence(label="risk_reward", value=rr))
        return AnalystVerdict(
            seat="technical",
            symbol=symbol,
            direction="bullish",
            magnitude=0.5,
            conviction="medium",
            evidence=evidence,
            invalidation="x",
        )

    order = [c.symbol for c in rank_verdicts([_v("WEAK", 0.5), _v("NONE", None), _v("STRONG", 3.0)])]
    assert order == ["NONE", "STRONG", "WEAK"]


def test_a_breakout_candidate_carries_no_reward_risk_key_at_all():
    """Item 1(d): a Type B candidate's reward:risk is dropped from the
    ranking outright, whatever figure its seat happened to attach — there is
    no overhead level for it to mean anything against."""
    v = AnalystVerdict(
        seat="technical",
        symbol="AAA",
        direction="bullish",
        magnitude=0.5,
        conviction="medium",
        evidence=[*_evidence(), VerdictEvidence(label="risk_reward", value=0.4)],
        invalidation="x",
    )
    [c] = rank_verdicts([v], setup_types={"AAA": "breakout"})
    assert "risk_reward_tiebreak" not in c.components
    [c] = rank_verdicts([v], setup_types={"AAA": "range"})
    assert c.components["risk_reward_tiebreak"] == 0.4


def test_the_real_structural_ratio_replaces_the_analysts_guess_in_ranking():
    """The substantive half of item 1(d): where the removed hard floor's
    information went. The verdict carries the analyst's own guessed 0.4; the
    desk's real, structure-derived preview says 2.2, and THAT is what orders
    the candidate."""
    v = AnalystVerdict(
        seat="technical",
        symbol="AAA",
        direction="bullish",
        magnitude=0.5,
        conviction="medium",
        evidence=[*_evidence(), VerdictEvidence(label="risk_reward", value=0.4)],
        invalidation="x",
    )
    [c] = rank_verdicts([v], real_reward_risk={"AAA": 2.2})
    assert c.components["risk_reward_tiebreak"] == 2.2


# --- item 65: a seat with no strength scale is ABSENT, never a constant ------
#
# Until 2026-09-26 the four rungless seats sent the literal 0.0 and
# `rank_verdicts` summed it like any other seat's reading. Verified before
# the change: a sum is additively neutral to a zero, so no candidate ever
# reordered because of it and no money moved. What did happen is that the
# strength term was REPORTED as summed over every leaning seat when only one
# of five could contribute to it, and nothing in the types stopped the next
# consumer from averaging or thresholding four placeholders. These pin all
# three halves: the absence is carried, the arithmetic is unchanged, and the
# placeholder cannot come back.


def _no_strength(
    seat: str, symbol: str = "XLE", conviction: str = "high", direction: str = "bullish"
) -> AnalystVerdict:
    return AnalystVerdict(
        seat=seat,
        symbol=symbol,
        direction=direction,
        magnitude=NO_STATED_STRENGTH,
        conviction=conviction,
        evidence=_evidence(),
        invalidation="x",
    )


def test_a_seat_that_states_no_strength_is_absent_from_the_strength_term():
    """The item's own minimum bar: not a zero contribution — no contribution.
    Four agreeing seats, none with a strength scale, produce a candidate with
    NO `magnitude` component at all, and both seat lists say so."""
    seats = ("news", "macro", "smart_money", "earnings")
    [c] = rank_verdicts([_no_strength(s) for s in seats])
    assert "magnitude" not in c.components
    assert c.strength_seats == []
    assert c.no_strength_seats == sorted(seats)
    # The seats are still fully counted — on conviction, which is the whole
    # of what they state. 1.0*(1.0 + 0.8 + 0.8 + 1.2) = 3.8.
    assert c.components["conviction_score"] == 3.8
    assert c.score == 3.8


def test_a_seat_that_states_a_strength_carries_it_into_the_ranking():
    """The other half of the same bar. Technical DOES state a strength (its
    rating rungs), so its reading reaches `components["magnitude"]` and the
    term names technical as its only coverage even with four other seats on
    the name."""
    tech = _tech("XLE", "strong_buy", "high").to_verdict()
    [c] = rank_verdicts([tech, *(_no_strength(s) for s in ("news", "macro", "smart_money", "earnings"))])
    assert c.strength_seats == ["technical"]
    assert c.no_strength_seats == ["earnings", "macro", "news", "smart_money"]
    # 1.0 (strong_buy) * 1.2 (technical's prior) — and nothing else, because
    # nothing else has a strength to add.
    assert c.components["magnitude"] == 1.2
    assert c.score == round(1.2 + (1.2 + 1.0 + 0.8 + 0.8 + 1.2), 4)


def test_the_score_is_unchanged_by_the_absence_encoding():
    """**The behaviour pin.** The point of item 65 is that the reported
    quantity becomes honest while the ORDER does not move — a placeholder
    that reordered candidates would have been a money defect, and this is
    the test that says it was not and still is not. Scored against the
    arithmetic the old literal-0.0 encoding produced, written out longhand:
    a seat with no strength adds `weight * (0 + conviction)`."""
    tech = _tech("XLE", "buy", "medium").to_verdict()  # 0.5 + 0.5, x1.2
    group = [
        tech,
        _no_strength("news", conviction="low"),  # 0 + 0.0, x1.0
        _no_strength("earnings", conviction="high"),
    ]  # 0 + 1.0, x1.2
    [c] = rank_verdicts(group)
    expected = 1.2 * (0.5 + 0.5) + 1.0 * (0.0 + 0.0) + 1.2 * (0.0 + 1.0)
    assert c.score == round(expected, 4)
    # Same total through the per-verdict helper the rotation margin reuses.
    assert round(sum(seat_weight(v.seat) * score_verdict(v) for v in group), 4) == c.score


def test_a_directional_verdict_cannot_state_a_strength_of_zero():
    """The mechanical guard against the placeholder returning. `0.0` is a
    point ON the 0..1 strength scale, so a seat sending it is claiming to
    have a scale and to have read nothing off it — which is the neutral
    verdict wearing a direction. The honest encoding of no scale is None."""
    with pytest.raises(ValidationError):
        AnalystVerdict(
            seat="news",
            symbol="XLE",
            direction="bullish",
            magnitude=0.0,
            conviction="high",
            evidence=_evidence(),
            invalidation="x",
        )
    # ...and a NEUTRAL verdict may still state 0.0, because a seat that has
    # a scale (technical) genuinely reads zero when it has no lean.
    assert (
        AnalystVerdict(
            seat="technical",
            symbol="XLE",
            direction="neutral",
            magnitude=0.0,
            conviction="low",
        ).signed_magnitude
        == 0.0
    )


def test_the_recorded_decision_is_that_the_absence_is_the_answer():
    """Item 65's recorded decision, pinned rather than trusted from a
    docstring: the shared no-strength marker IS the absence, and the
    earnings seat — the one of the four with the strongest case for a
    gradient, and the one weighted highest — emits it off its own model on a
    directional read. If a seat is ever given a real strength scale this
    fails, which is the intended trip-wire: that is a schema change to
    ratify with its derivation and the seat's prompt, not a quiet edit.
    Each of the other three is pinned the same way in its own file
    (`test_news_verdict`, `test_macro_verdict`, `test_smart_money_verdict`).
    """
    assert NO_STATED_STRENGTH is None
    v = _earnings("bullish", "high").to_verdict()
    assert v.direction == "bullish" and v.magnitude is None
    [c] = rank_verdicts([v])
    assert "magnitude" not in c.components and c.no_strength_seats == ["earnings"]


# --- eligibility + ranking through the PM -----------------------------------

STATE_CHANGES = "- [2026-08-31] Anthropic signs $35 billion cloud deal with Nvidia-backed Lambda → NVDA\n"


def _registry(analyses, extra=None):
    reg = PortfolioManagerAgent.build_evidence_registry(
        analyses=analyses,
        positions=[],
        news_intel=None,
        earnings_analyses=[],
        macro_analysis=None,
        smart_money_findings=[],
        symbol_sectors={},
    )
    for symbol, sources in (extra or {}).items():
        reg.setdefault(symbol, {}).update(sources)
    return reg


def test_pm_ranks_equally_eligible_candidates_deterministically():
    analyses = [
        _tech("CCC", "buy", "medium"),
        _tech("AAA", "strong_buy", "high"),
        _tech("BBB", "sell", "high"),
        _tech("DDD", "buy", "low"),
    ]
    ranked, blocked = PortfolioManagerAgent.rank_candidates(
        analyses=analyses,
        evidence_registry=_registry(analyses),
        allowed_buy_symbols={"AAA", "BBB", "CCC", "DDD"},
        active_state_changes="",
        asof=SESSION,
    )
    assert blocked == {}
    assert [c.symbol for c in ranked] == ["AAA", "BBB", "CCC", "DDD"]
    # Each is a lone technical read, so each carries technical's 1.2 weight
    # (2026-09-13: a weighted sum, so a lone seat's weight no longer cancels).
    assert [c.score for c in ranked] == [2.4, 1.8, 1.2, 0.6]


def test_the_real_ratio_orders_candidates_instead_of_admitting_them():
    """**Rewritten 2026-09-11, docs/WORK.md item 1(d).** This test used to
    assert that eligibility and the constructor's real gate REFUSED the same
    names (the 2026-09-04 disjoint-sets fix). Neither refuses on a reward:risk
    floor any more, so the property worth pinning is the replacement one: the
    same real, structure-derived numbers now decide the ORDER instead.

    Same production geometry as before. NVDA overstates (self-reported R/R
    10.0, real 0.60) and GEV understates (self-reported 0.8, real 1.60).
    Both are admitted; the real numbers put GEV ahead of NVDA, which is the
    exact reversal of what the analysts' own guesses would have produced."""
    from src.portfolio_constructor import PortfolioConstructor

    def _structured(symbol, *, model_target, computed_levels):
        return TechAnalysisResult(
            symbol=symbol,
            rating="buy",
            conviction="medium",
            entry_price=100.0,
            stop_loss=95.0,
            reference_target=model_target,
            support_levels=[95.0],
            resistance_levels=[model_target],
            computed_levels=computed_levels,
            atr_14=(100.0 - 95.0) / 3.5,
            setup_type="range",
            expected_horizon_sessions=60,
            reasoning="test",
            reasoning_chain=_chain(),
            thesis_invalid_if="closes below support",
        )

    overstated = _structured("NVDA", model_target=150.0, computed_levels=[95.0, 103.0])
    understated = _structured("GEV", model_target=104.0, computed_levels=[95.0, 108.0])
    analyses = [overstated, understated]
    allowed = {"NVDA", "GEV"}

    constructor = PortfolioConstructor()
    real_map = {a.symbol: constructor.real_reward_risk_preview(a, "long") for a in analyses}
    # Both are real, measurable numbers now — the preview no longer returns
    # None for a payoff that merely fails a floor.
    assert real_map == {"NVDA": 0.6, "GEV": 1.6}

    # Neither the size of the ratio nor the disagreement between the two
    # readings blocks anything any more.
    verdicts = PortfolioManagerAgent.candidate_eligibility(
        analyses=analyses,
        evidence_registry=_registry(analyses),
        allowed_buy_symbols=allowed,
        active_state_changes="",
        asof=SESSION,
        real_reward_risk_by_symbol=real_map,
    )
    assert {sym for sym, why in verdicts.items() if not why} == {"NVDA", "GEV"}

    # It decides the ORDER instead. The two names tie on the composite score
    # (identical rating and conviction), so the real ratio is what separates
    # them — and it puts the honestly-better payoff first.
    ranked, blocked = PortfolioManagerAgent.rank_candidates(
        analyses=analyses,
        evidence_registry=_registry(analyses),
        allowed_buy_symbols=allowed,
        active_state_changes="",
        asof=SESSION,
        real_reward_risk_by_symbol=real_map,
    )
    assert blocked == {}
    assert [c.symbol for c in ranked] == ["GEV", "NVDA"]
    assert [c.score for c in ranked] == [ranked[0].score] * 2, (
        "the two must genuinely tie on the composite — otherwise this is not testing the reward:risk key at all"
    )
    assert ranked[0].components["risk_reward_tiebreak"] == 1.6
    assert ranked[1].components["risk_reward_tiebreak"] == 0.6


# ==========================================================================
# 2c. All five seats through the real integration path (2026-09-03)
# ==========================================================================


def _news_intel(stock_news: dict) -> "NewsIntelligenceReport":
    return NewsIntelligenceReport(
        macro_narrative={
            "last_updated": "2026-09-03",
            "era_themes": ["rates"],
            "current_regime": "mid-cycle, rates on hold",
        },
        stock_news=stock_news,
        pm_briefing="ok",
        market_sentiment="neutral",
        confidence="medium",
    )


def test_rank_candidates_combines_all_five_seats_at_their_researched_weight():
    """End-to-end through the real PM entry point, not `rank_verdicts`
    directly: one symbol where technical, news, and earnings all cover it
    and agree bullish, must outrank a symbol only technical covers at the
    same tech reading — proving the extra seats actually reach the ranking,
    not just exist as unused methods."""
    solo = _tech("SOLO", "buy", "medium")  # 0.5 + 0.5 = 1.0, alone
    covered = _tech("MULTI", "buy", "medium")  # same tech reading as SOLO
    analyses = [solo, covered]

    news_intel = _news_intel(
        {
            "MULTI": [
                StockNewsItem(
                    headline="guidance raised",
                    sentiment="bullish",
                    conviction="high",
                    impact_summary="raised full-year guide",
                )
            ],
        }
    )
    multi_earnings = _earnings("bullish", "high")
    multi_earnings.symbol = "MULTI"
    earnings_analyses = [
        {
            "symbol": "MULTI",
            # Owner ruling 13 Oct 2026 (same rules for buys and sales): the one
            # shared view builder drops an earnings view older than 90 days
            # before any rule reads it. The fixture's own filing is 170 days
            # before SESSION; this test combines LIVE seats, so it is dated 10.
            "filing_date": date.fromordinal(SESSION.toordinal() - 10).isoformat(),
            "analysis": multi_earnings.model_dump(),
        }
    ]

    ranked, blocked = PortfolioManagerAgent.rank_candidates(
        analyses=analyses,
        evidence_registry=_registry(analyses),
        allowed_buy_symbols={"SOLO", "MULTI"},
        active_state_changes="",
        asof=SESSION,
        news_intel=news_intel,
        earnings_analyses=earnings_analyses,
    )
    assert blocked == {}
    assert [c.symbol for c in ranked] == ["MULTI", "SOLO"]
    assert ranked[0].seats == ["earnings", "news", "technical"]
    # SOLO is untouched by the new seats: still exactly its own score at its
    # own seat weight, with nothing added and nothing averaged away.
    solo_candidate = ranked[1]
    assert solo_candidate.score == round(score_verdict(solo.to_verdict()) * SEAT_WEIGHT["technical"], 4)


def test_rank_candidates_survives_a_malformed_earnings_entry():
    """A single bad earnings dict must drop only earnings' contribution for
    that symbol — never crash the whole ranking call."""
    analyses = [_tech("AAA", "buy", "medium")]
    bad_earnings = [{"symbol": "AAA", "analysis": {"not_a_real_shape": True}}]
    ranked, blocked = PortfolioManagerAgent.rank_candidates(
        analyses=analyses,
        evidence_registry=_registry(analyses),
        allowed_buy_symbols={"AAA"},
        active_state_changes="",
        asof=SESSION,
        earnings_analyses=bad_earnings,
    )
    assert blocked == {}
    assert [c.symbol for c in ranked] == ["AAA"]
    assert ranked[0].seats == ["technical"]  # earnings silently dropped, not crashed


def test_rank_candidates_drops_earnings_with_no_wrapper_symbol_at_all():
    """A blank/missing wrapper symbol means there's no ground truth to check
    the LLM's own claimed symbol against — must drop, not trust it anyway.
    Matches `_earnings_stance_rows`'s identical handling of this case."""
    analyses = [_tech("AAA", "buy", "medium")]
    no_symbol_wrapper = [
        {
            "symbol": "",
            "analysis": _earnings("bullish", "high").model_dump(),
        }
    ]
    ranked, blocked = PortfolioManagerAgent.rank_candidates(
        analyses=analyses,
        evidence_registry=_registry(analyses),
        allowed_buy_symbols={"AAA"},
        active_state_changes="",
        asof=SESSION,
        earnings_analyses=no_symbol_wrapper,
    )
    assert blocked == {}
    assert [c.symbol for c in ranked] == ["AAA"]
    assert ranked[0].seats == ["technical"]


def test_rank_candidates_drops_earnings_on_a_wrapper_vs_analysis_symbol_mismatch():
    """The pipeline wrapper's `symbol` is ground truth; `EarningsAnalysis.
    symbol` is part of the LLM's own JSON and could disagree (a hallucinated
    ticker). A mismatch must drop earnings' contribution for that entry, not
    trust either side silently."""
    analyses = [_tech("AAA", "buy", "medium")]
    mismatched = _earnings("bullish", "high")  # symbol="AAPL" by fixture default
    earnings_analyses = [{"symbol": "AAA", "analysis": mismatched.model_dump()}]
    ranked, blocked = PortfolioManagerAgent.rank_candidates(
        analyses=analyses,
        evidence_registry=_registry(analyses),
        allowed_buy_symbols={"AAA"},
        active_state_changes="",
        asof=SESSION,
        earnings_analyses=earnings_analyses,
    )
    assert blocked == {}
    assert [c.symbol for c in ranked] == ["AAA"]
    assert ranked[0].seats == ["technical"]  # earnings dropped on mismatch


def test_rank_candidates_survives_a_malformed_macro_dict():
    """Same contract for macro: garbage input must not take down the run."""
    analyses = [_tech("AAA", "buy", "medium")]
    ranked, blocked = PortfolioManagerAgent.rank_candidates(
        analyses=analyses,
        evidence_registry=_registry(analyses),
        allowed_buy_symbols={"AAA"},
        active_state_changes="",
        asof=SESSION,
        macro_analysis={"not_a_real_shape": True},
    )
    assert blocked == {}
    assert [c.symbol for c in ranked] == ["AAA"]
    assert ranked[0].seats == ["technical"]


def test_rank_candidates_macro_verdict_applies_uniformly_to_every_symbol():
    """Documented simplification: macro's verdict is the same broad read for
    every candidate, not sector-adjusted. Two unrelated symbols both get the
    same macro contribution."""
    analyses = [_tech("AAA", "buy", "medium"), _tech("BBB", "buy", "medium")]
    macro = {
        "reasoning_chain": {
            "volatility_analysis": "VIX mid-range, flat trend",
            "yield_curve_analysis": "2s10s modestly positive",
            "monetary_policy_analysis": "Fed on hold, no forward guidance shift",
            "inflation_labor_credit": "CPI cooling, unemployment stable, spreads tight",
            "cross_signal_synthesis": "consistent risk-on read across signals",
            "sector_implications": "broad, no strong sector tilt",
        },
        "regime": "risk-on",
        "confidence": "high",
        "equity_outlook": "bullish",
        "position_guidance": {
            "target_invested_pct": 90,
            "cash_recommendation_pct": 10,
            "reasoning": "risk-on regime supports full deployment",
        },
        "summary": "broadly constructive",
        "bear_triggers": ["CPI surprises hot"],
    }
    ranked, blocked = PortfolioManagerAgent.rank_candidates(
        analyses=analyses,
        evidence_registry=_registry(analyses),
        allowed_buy_symbols={"AAA", "BBB"},
        active_state_changes="",
        asof=SESSION,
        macro_analysis=macro,
    )
    assert blocked == {}
    assert {c.symbol for c in ranked} == {"AAA", "BBB"}
    for c in ranked:
        assert c.seats == ["macro", "technical"]


def test_pm_never_orders_a_name_a_gate_refused():
    analyses = [
        _tech("GOOD", "buy", "medium"),
        _tech("NEUT", "neutral", "high", invalid_if=""),  # R2
        _tech("NOTU", "strong_buy", "high"),  # R3 — not BUY-eligible
        # R/R 0.8 — thin but REAL. Item 1(d): no longer an R4 block, it is
        # ranked on that number instead (below NVDA, which shares its score
        # but has the same thin payoff, so `symbol` separates them).
        _tech("SUBF", "strong_buy", "high", target=104),
        _tech("NVDA", "strong_buy", "high", target=104),
        _tech("OPPO", "strong_buy", "high"),  # R5 — net 0
    ]
    registry = _registry(analyses, extra={"OPPO": {"macro": "bearish"}})
    ranked, blocked = PortfolioManagerAgent.rank_candidates(
        analyses=analyses,
        evidence_registry=registry,
        allowed_buy_symbols={"GOOD", "NEUT", "SUBF", "NVDA", "OPPO"},
        active_state_changes=STATE_CHANGES,
        asof=SESSION,
    )
    # Every name a gate REFUSED is still absent from the order. What changed
    # 2026-09-11 (item 1(d)) is which gates refuse: a weak-but-measurable
    # payoff is not one of them any more.
    assert [c.symbol for c in ranked] == ["NVDA", "SUBF", "GOOD"]
    assert set(blocked) == {"NEUT", "NOTU", "OPPO"}
    assert blocked["NEUT"] == ["R2 neutral rating"]
    assert blocked["NOTU"] == ["R3 not BUY-eligible"]
    assert blocked["OPPO"] == ["R5 net evidence +0 if long — no rung"]


def test_an_unmeasurable_payoff_is_not_refused_by_r4():
    """R4 is retired. A candidate whose payoff arithmetic does not resolve
    is still eligible; the missing ratio is ranking information."""
    analyses = [_tech("AAA", "buy", "medium")]
    ranked, blocked = PortfolioManagerAgent.rank_candidates(
        analyses=analyses,
        evidence_registry=_registry(analyses),
        allowed_buy_symbols={"AAA"},
        active_state_changes="",
        asof=SESSION,
        real_reward_risk_by_symbol={"AAA": None},
    )
    assert blocked == {}
    assert [c.symbol for c in ranked] == ["AAA"]


def test_r4_does_not_run_at_all_for_a_breakout_candidate():
    """Item 1(d): a Type B candidate is admitted on its risk side and its
    evidence, with no reward:risk test of any kind — including the
    unmeasurable one, which is still a reward-side refusal."""
    analysis = _tech("AAA", "buy", "medium")
    analysis.setup_type = "breakout"
    ranked, blocked = PortfolioManagerAgent.rank_candidates(
        analyses=[analysis],
        evidence_registry=_registry([analysis]),
        allowed_buy_symbols={"AAA"},
        active_state_changes="",
        asof=SESSION,
        real_reward_risk_by_symbol={"AAA": None},
    )
    assert blocked == {}
    assert [c.symbol for c in ranked] == ["AAA"]
    assert "risk_reward_tiebreak" not in ranked[0].components


def test_the_rr_floor_no_longer_decides_eligibility_at_any_value():
    """**Inverted 2026-09-11, docs/WORK.md item 1(d).** This used to prove
    the threaded floor was the one that decided eligibility, by moving it
    from 1.5 to 2.0 and watching a 1.6 candidate disappear. Nothing about
    the ratio's SIZE decides eligibility any more, at any threaded value.
    Invented reward:risk floors do not refuse or shrink tickets."""
    analyses = [_tech("AAA", "buy", "high", target=108)]  # R/R 1.6
    kwargs = dict(
        analyses=analyses,
        evidence_registry=_registry(analyses),
        allowed_buy_symbols={"AAA"},
        active_state_changes="",
        asof=SESSION,
    )
    for floor in (REWARD_RISK_FLOOR, 2.0, 99.0):
        ranked, blocked = PortfolioManagerAgent.rank_candidates(
            rr_floor=floor,
            **kwargs,
        )
        assert [c.symbol for c in ranked] == ["AAA"], floor
        assert blocked == {}, floor


def test_shorts_are_not_subject_to_the_buy_eligibility_gate():
    analyses = [_tech("NKE", "sell", "medium")]
    ranked, blocked = PortfolioManagerAgent.rank_candidates(
        analyses=analyses,
        evidence_registry=_registry(analyses),
        allowed_buy_symbols=set(),
        active_state_changes="",
        asof=SESSION,
    )
    assert [c.symbol for c in ranked] == ["NKE"] and blocked == {}


def test_production_eligibility_matches_the_item_18_audit_on_the_real_day():
    """The audit script (`ops/model_policy/deterministic_selection.py`) and
    the production port must admit the SAME twelve names on run-64290730.
    If one moves and the other does not, this fails."""
    sel = _LEVEL_LESS.raw
    rows = evaluate(sel, _LEVEL_LESS.analyses, _LEVEL_LESS.positions, _LEVEL_LESS.news)
    audit = sorted(r["symbol"] for r in rows if r["eligible"])
    registry = PortfolioManagerAgent.build_evidence_registry(
        analyses=_LEVEL_LESS.analyses,
        positions=_LEVEL_LESS.positions,
        news_intel=_LEVEL_LESS.news,
        earnings_analyses=sel["earnings_analyses"],
        macro_analysis=sel["macro_analysis"],
        smart_money_findings=[],
        symbol_sectors={},
    )
    stale = PortfolioManagerAgent.stale_evidence_sources(
        earnings_analyses=sel["earnings_analyses"],
        asof=SESSION,
    )
    ranked, blocked = PortfolioManagerAgent.rank_candidates(
        analyses=_LEVEL_LESS.analyses,
        evidence_registry=registry,
        stale_sources=stale,
        allowed_buy_symbols=set(sel["allowed_buy_symbols"]) | set(sel["transient_admitted_symbols"]),
        active_state_changes=sel["memory"]["active_state_changes"],
        asof=SESSION,
    )
    assert sorted(c.symbol for c in ranked) == audit
    # **12 -> 25, 2026-09-11 (docs/WORK.md item 1(d)).** Removing the
    # reward:risk floor from R4 more than doubles the eligible set on this
    # exact real production day: 13 of these 25 names were being refused for
    # a thin-but-real payoff alone. That is the measured size of the change
    # on the day item 1 was originally written about.
    assert len(audit) == 25
    assert not (set(blocked) & set(audit))
    # The pinned order at equal weight over Technical's verdicts alone. Nine
    # of the twelve tie at 1.00 (all `buy`/`sell` at `medium`) and two more
    # tie at 0.5 — see docs/WORK.md item 18 for why the coarse composite
    # score itself is reported rather than "fixed" with a weight nobody has
    # measured. 2026-10-10 (owner ruling 9 Oct): ties no longer break on
    # reward:risk — its reward side is the take-profit target, not a sell
    # rule and unscored. They fall to measured level touches; this fixture
    # carries none, so each tied group is alphabetical (the deterministic
    # stabiliser), with no unverifiable number deciding the order.
    assert [c.symbol for c in ranked] == [
        "SLB",
        "VLO",
        "XLE",
        "AAPL",
        "CMCSA",
        "COP",
        "CVX",
        "DE",
        "DIS",
        "FLNC",
        "JNJ",
        "JPM",
        "KO",
        "MSFT",
        "MU",
        "NKE",
        "NUE",
        "NVDA",
        "PATH",
        "PFE",
        "RSG",
        "TSM",
        "V",
        "CHPX",
        "CRM",
    ]


def test_the_ranking_is_rendered_into_the_pm_prompt(monkeypatch):
    import src.agents.portfolio_manager as pm_module

    monkeypatch.setattr(pm_module, "et_today", lambda: SESSION)
    analyses = [
        _tech("CCC", "buy", "medium"),
        _tech("AAA", "strong_buy", "high"),
        _tech("ZZZ", "neutral", invalid_if=""),
    ]
    # 2026-09-25 conviction bar: a lone-technical name no longer ENTERS, so
    # each rankable name here carries a second, HIGH-conviction bullish seat
    # (news) to clear the bar. That is what makes the rendered scores and seat
    # counts below higher than the pre-mandate single-seat rendering.
    news_intel = _news_intel(
        {
            sym: [
                StockNewsItem(
                    headline="guidance raised",
                    sentiment="bullish",
                    conviction="high",
                    impact_summary="raised full-year guide",
                )
            ]
            for sym in ("AAA", "CCC")
        }
    )
    agent = PortfolioManagerAgent.__new__(PortfolioManagerAgent)
    msg = agent.build_user_message(
        analyses=analyses,
        positions=[],
        macro_analysis=None,
        cash_balance=10_000,
        total_value=100_000,
        news_intel=news_intel,
        earnings_analyses=[],
        smart_money_findings=[],
        allowed_buy_symbols={"AAA", "CCC"},
        active_state_changes="",
        rr_floor=REWARD_RISK_FLOOR,
    )
    section = msg.split("## Candidate Ranking")[1].split("\n## ")[0]
    lines = [ln for ln in section.splitlines() if ln[:2] in ("1.", "2.", "3.")]
    assert lines[0].startswith("1. AAA — bullish | score 3.40")
    assert lines[1].startswith("2. CCC — bullish | score 2.20")
    # The prompt must say the aggregation is a SUM, or the PM reads the number
    # as a 0-2 fraction. Both survivors now carry two seats.
    assert "summed over 2 seat(s)" in lines[0]
    assert "an agreeing seat can never pull a name down" in section
    assert "technical: closes below MA50 on volume" in lines[0]
    assert "- ZZZ: R2 neutral rating" in section
    # Item 65, 2026-09-26. The strength half names the seats it is actually
    # summed over, and news — which has no strength scale — is listed as
    # such instead of silently feeding a 0.0 into a figure the prompt
    # describes as covering every leaning seat.
    assert "strength 1.20 from technical" in lines[0]
    assert "no strength scale: news" in lines[0]
    assert "they are absent from the strength term entirely" in section
    # The ranking sits AFTER the reports it orders.
    assert msg.index("## Technical Analysis Reports") < msg.index("## Candidate Ranking")


def test_the_prompt_reports_no_strength_rather_than_zero_when_no_seat_has_one():
    """Item 65's reporting half, at the surface it actually matters on. A
    name carried entirely by seats with no strength scale used to render as
    "strength 0.00" — identical to what a name whose seats all measured no
    lean would render as, and shown to a model that then chooses between
    them. It now says plainly that no seat stated one."""
    seats = ("news", "macro", "smart_money", "earnings")
    ranked = rank_verdicts([_no_strength(s, symbol="AAA") for s in seats])
    section = PortfolioManagerAgent._render_candidate_ranking(ranked, {})
    line = [ln for ln in section.splitlines() if ln.startswith("1.")][0]
    assert "no strength stated by any seat on this name" in line
    assert "strength 0.00" not in section
    assert "no strength scale: earnings, macro, news, smart_money" in line


def test_the_prompt_says_nothing_is_ranked_when_nothing_is_eligible(monkeypatch):
    import src.agents.portfolio_manager as pm_module

    monkeypatch.setattr(pm_module, "et_today", lambda: SESSION)
    agent = PortfolioManagerAgent.__new__(PortfolioManagerAgent)
    msg = agent.build_user_message(
        analyses=[_tech("AAA", "buy", "high")],
        positions=[],
        macro_analysis=None,
        cash_balance=10_000,
        total_value=100_000,
        news_intel=None,
        earnings_analyses=[],
        smart_money_findings=[],
        allowed_buy_symbols=set(),
        active_state_changes="",
    )
    assert "(no name passes every pre-decision rule today)" in msg
    assert "- AAA: R3 not BUY-eligible" in msg


# ==========================================================================
# Item 141 (2026-09-20) — the LIVE ranking path's residual alphabetical
# fallback, left behind by item 1(d) (2026-09-11): a tied composite tier
# where every candidate is a Type B / breakout setup carries NO risk_reward
# at all (`reward_risk_floor_applies` is False for breakout, on purpose —
# `src/risk/constants.py`), so the whole tier shared one reward:risk sort
# value (that key was removed 2026-10-10) and `rank_verdicts` used to fall straight to
# `symbol`. That is the same bug the 2026-09-04 fix (item 18/64 audit)
# closed for the risk_reward-bearing case, reopened for breakout only.
# ==========================================================================


def _breakout_tech(symbol: str, touches: int, rating: str = "buy", conviction: str = "medium") -> TechAnalysisResult:
    """A Type B / breakout read: no overhead resistance to measure a reward
    against (so `risk_reward` and the `risk_reward_tiebreak` it feeds are
    both absent), but a real, measured support side — `touches` prior
    pivots backing the one support level `find_structural_levels` found,
    the same count `PortfolioConstructor._level_backing_stop` already
    trusts elsewhere."""
    long = rating in ("buy", "strong_buy")
    stop = 95.0 if long else 105.0
    # `reference_target` is required on any actionable rating, breakout
    # included — a measured-move projection, not a defended overhead
    # level. `reward_risk_floor_applies` is what makes this NOT count as a
    # comparable reward for ranking, not the absence of the field.
    target = 120.0 if long else 80.0
    return TechAnalysisResult(
        symbol=symbol,
        rating=rating,
        conviction=conviction,
        entry_price=100,
        stop_loss=stop,
        reference_target=target,
        support_levels=[stop] if long else [],
        resistance_levels=[] if long else [stop],
        computed_levels=[stop],
        computed_level_touches={stop: touches},
        setup_type="breakout",
        expected_horizon_sessions=10,
        reasoning="measured-move breakout, no overhead structure",
        reasoning_chain=_chain(),
        thesis_invalid_if="closes back below breakout level",
    )


def test_level_touches_evidence_is_attached_and_summed():
    v = _breakout_tech("AAA", touches=7).to_verdict()
    assert level_touches_of(v) == 7.0
    by_label = {e.label: e for e in v.evidence}
    assert by_label["stop_side_level_touches"].value == 7.0


def test_level_touches_only_counts_the_risk_side_not_overhead_supply():
    """A long's OVERHEAD resistance touches must never count here — that is
    supply in the way of the trade, not evidence for it. Only the level
    below entry (the stop side) contributes."""
    a = TechAnalysisResult(
        symbol="AAA",
        rating="buy",
        conviction="medium",
        entry_price=100,
        stop_loss=95,
        reference_target=112,
        support_levels=[95],
        resistance_levels=[112],
        computed_levels=[95, 112],
        computed_level_touches={95: 3, 112: 20},
        setup_type="range",
        expected_horizon_sessions=10,
        reasoning="x",
        reasoning_chain=_chain(),
        thesis_invalid_if="x",
    )
    v = a.to_verdict()
    assert level_touches_of(v) == 3.0  # the 20-touch resistance is excluded
    # Mirror check on the short side: only the level ABOVE entry counts.
    b = TechAnalysisResult(
        symbol="BBB",
        rating="sell",
        conviction="medium",
        entry_price=100,
        stop_loss=105,
        reference_target=88,
        support_levels=[88],
        resistance_levels=[105],
        computed_levels=[88, 105],
        computed_level_touches={88: 20, 105: 4},
        setup_type="range",
        expected_horizon_sessions=10,
        reasoning="x",
        reasoning_chain=_chain(),
        thesis_invalid_if="x",
    )
    assert level_touches_of(b.to_verdict()) == 4.0  # the 20-touch support excluded


def test_all_breakout_tied_tier_breaks_on_level_touches_not_alphabet():
    """The exact residual case item 141 names: every candidate ties on the
    composite score (all `buy`/`medium`, same as the item 18 audit's 9-of-12
    day) AND every candidate is a breakout, so none of them carries a
    risk_reward tiebreak at all. Z, the last ticker alphabetically, has the
    most independently-touched support and must still win."""
    verdicts = [
        _breakout_tech("Z", touches=9).to_verdict(),
        _breakout_tech("M", touches=2).to_verdict(),
        _breakout_tech("A", touches=5).to_verdict(),
    ]
    setup_types = {"Z": "breakout", "M": "breakout", "A": "breakout"}
    ranked = rank_verdicts(verdicts, setup_types=setup_types)
    assert [c.score for c in ranked] == [ranked[0].score] * 3  # composite tied
    for c in ranked:
        assert "risk_reward_tiebreak" not in c.components  # breakout: no R/R at all
    assert [c.symbol for c in ranked] == ["Z", "A", "M"]
    # Unweighted — see `level_touches_of` for why `SEAT_WEIGHT` does not
    # apply to a deterministic pivot count.
    assert [c.components["level_touches_tiebreak"] for c in ranked] == [9.0, 5.0, 2.0]
    # Pure and stable: same input, same order, every time.
    assert [c.symbol for c in rank_verdicts(verdicts, setup_types=setup_types)] == [
        "Z",
        "A",
        "M",
    ]


def test_measured_tie_pattern_9_of_12_all_breakout_orders_on_structure():
    """Realistic synthetic data reproducing the measured tie rate the board
    item cites (docs/WORK.md item 141; docs/INCIDENT_HISTORY.md 2026-09-04,
    9 of 12 real names tied one day): 12 candidates, 9 tied on the identical
    `buy`/`medium` composite, all of them breakout so the risk_reward
    tiebreak this desk already shipped for the range case (2026-09-04) does
    not reach them. Reverse-alphabetical touch counts prove the order is
    driven by structure, not by ticker spelling running the other way by
    coincidence."""
    tied_symbols = ["AAPL", "CRM", "CHPX", "JNJ", "JPM", "MSFT", "TSM", "CMCSA", "PFE"]
    verdicts = [
        _breakout_tech(sym, touches=len(tied_symbols) - i).to_verdict()
        for i, sym in enumerate(tied_symbols)  # AAPL gets the most touches
    ]
    # 3 more names at other composite scores, unaffected by this tiebreak.
    verdicts += [
        _tech("SLB", "strong_buy", "high").to_verdict(),
        _tech("VLO", "strong_sell", "high").to_verdict(),
        _tech("XLE", "sell", "low").to_verdict(),
    ]
    setup_types = {sym: "breakout" for sym in tied_symbols}
    setup_types.update({"SLB": "range", "VLO": "range", "XLE": "range"})
    ranked = rank_verdicts(verdicts, setup_types=setup_types)
    tied = [c for c in ranked if c.symbol in tied_symbols]
    assert len(tied) == 9
    # Highest touch count first — AAPL, which is *not* alphabetically first
    # among the tied set by ranking-relevant means (CHPX and CMCSA sort
    # before it), wins because it has the most measured structure, not
    # because of its spelling.
    assert [c.symbol for c in tied] == tied_symbols
    # Unweighted — see `level_touches_of`.
    assert [c.components["level_touches_tiebreak"] for c in tied] == [float(n) for n in range(9, 0, -1)]
    # Confirms this is NOT the alphabetical order the old fallback produced.
    assert [c.symbol for c in tied] != sorted(tied_symbols)
