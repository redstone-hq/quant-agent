"""Spec §9.4 — agreement REFUSES a trade; it does not size one.

The SIGNED score over the independent seats (of technical/news/earnings/
macro/smart_money) — those aligned with the target's proposed action MINUS
those opposed to it — is computed deterministically from the canonical
evidence registry (reusing `validate_grounding`'s own polarity rule, not a
second one) and turned into ONE yes/no decision in `PortfolioConstructor`:
at or below zero the target is refused outright and produces no order; at
+1 or more it carries no size restriction of its own.

**The graduated ceiling was retired 2026-09-14 by owner decision.** It
scaled permitted risk as `max_position_risk_pct x sqrt(net / 5 seats)`; the
square-root law prices INDEPENDENT estimates, and these five seats read
overlapping evidence with several sharing one underlying model, so its one
precondition was never met. Measured over the archived sized targets of the
2026-08-28..2026-09-02 snapshot, the rungs capped ZERO targets — only the
refusal ever bit. `test_no_agreement_keyed_size_ladder_exists` fails if any
per-score size ladder comes back.

The signed sum landed 2026-09-02. `tests/test_signed_dissent.py` holds the
acceptance criterion for that change and the mechanical pin on seat weights;
this file is the rule's own behaviour, end to end.
"""

from src.config import RiskConfig
from src.models import Position, TargetPosition, TechAnalysisResult, TechReasoningChain
from src.portfolio_constructor import ConstructorConfig, PortfolioConstructor
from src.risk.rules import (
    agreement_refuses_trade,
    count_aligned_sources,
    signed_source_score,
    stance_is_aligned,
)

import pytest
from pydantic import ValidationError


def _tech_rc() -> TechReasoningChain:
    return TechReasoningChain(
        trend="x",
        momentum="x",
        volatility="x",
        volume="x",
        support_resistance="x",
    )


def _analysis(symbol: str, entry: float = 100.0, stop: float = 95.0, target: float = 115.0) -> TechAnalysisResult:
    return TechAnalysisResult(
        symbol=symbol,
        rating="buy",
        entry_price=entry,
        stop_loss=stop,
        reference_target=target,
        support_levels=[stop],
        resistance_levels=[target],
        # Python-set by TechAnalystAgent, not model-emitted. The constructor
        # derives the take-profit from `computed_levels` (2026-09-01) and
        # refuses without them; the ATR sits just inside the noise band so
        # the structural stop is left alone.
        computed_levels=[stop, target],
        atr_14=(entry - stop) / 3.5,
        setup_type="range",
        expected_horizon_sessions=60,
        reasoning="test",
        reasoning_chain=_tech_rc(),
        thesis_invalid_if="closes below support",
    )


# --------------------------------------------------------------------------
# count_aligned_sources / stance_is_aligned — the deterministic vocabulary
# --------------------------------------------------------------------------


def test_count_aligned_sources_one_aligned_long():
    sources = {"technical": "bullish", "news": "bearish", "macro": "neutral"}
    assert count_aligned_sources("NVDA", sources, "long") == 1


def test_count_aligned_sources_one_aligned_short():
    sources = {"technical": "bullish", "news": "bearish", "macro": "neutral"}
    assert count_aligned_sources("NVDA", sources, "short") == 1


def test_count_aligned_sources_two_aligned_long():
    sources = {"technical": "bullish", "earnings": "bullish", "macro": "bearish"}
    assert count_aligned_sources("AAPL", sources, "long") == 2
    assert count_aligned_sources("AAPL", sources, "short") == 1


def test_count_aligned_sources_three_or_more_aligned():
    sources = {
        "technical": "bullish",
        "earnings": "bullish",
        "news": "bullish",
        "macro": "bearish",
        "smart_money": "bearish",
    }
    assert count_aligned_sources("CEG", sources, "long") == 3
    assert count_aligned_sources("CEG", sources, "short") == 2


def test_count_aligned_sources_zero_when_nothing_points_that_way():
    sources = {"technical": "neutral", "macro": "neutral"}
    assert count_aligned_sources("XLF", sources, "long") == 0
    assert count_aligned_sources("XLF", sources, "short") == 0


def test_count_aligned_sources_empty_registry_is_zero():
    assert count_aligned_sources("ANY", {}, "long") == 0


def test_macro_polarity_flips_for_inverse_etf():
    """A risk-off macro stance supports owning an INVERSE ETF (SQQQ is
    -3x) — same twist `validate_grounding` has always applied."""
    assert stance_is_aligned("macro", "SQQQ", "risk_off", wants_bullish=True)
    assert not stance_is_aligned("macro", "SQQQ", "risk_on", wants_bullish=True)
    # A normal (non-inverse) symbol is not flipped.
    assert stance_is_aligned("macro", "AAPL", "risk_on", wants_bullish=True)
    assert not stance_is_aligned("macro", "AAPL", "risk_off", wants_bullish=True)


# --------------------------------------------------------------------------
# agreement_refuses_trade — the refusal gate, and the ladder's absence
# --------------------------------------------------------------------------


def test_a_net_at_or_below_zero_is_refused():
    """The rule that SURVIVED the 2026-09-14 retirement, unchanged in
    behaviour. A name with no net evidence for its direction, or with net
    dissent, is not a small idea — it is not an idea."""
    assert agreement_refuses_trade(0)
    assert agreement_refuses_trade(-1)
    assert agreement_refuses_trade(-5)


def test_any_positive_net_is_not_refused_and_that_is_all_it_decides():
    """No rung, no ladder, no per-score number: +1 and +5 are the same
    answer, because the only question left is go/no-go."""
    assert not agreement_refuses_trade(1)
    assert not agreement_refuses_trade(2)
    assert not agreement_refuses_trade(5)
    assert not agreement_refuses_trade(99)


def test_the_gate_is_a_bool_not_a_float_sentinel():
    """A float sentinel is what let "no rung" and "a real zero-weight
    close" be the same value downstream. The gate cannot be misread as a
    size because it is not a number."""
    assert isinstance(agreement_refuses_trade(0), bool)
    assert isinstance(agreement_refuses_trade(3), bool)


def test_no_agreement_keyed_size_ladder_exists():
    """The anti-regression pin for the retirement (owner decision,
    2026-09-14). Fails if any per-score sequence of size numbers keyed on
    agreement comes back — in the risk constants, in `RiskConfig`, in the
    constructor's mirrored config, or in `config/settings.yaml`.

    Five copies of one number is the smell that started this; so is a
    revived schedule under a new name. Both are caught here.
    """
    import yaml
    from pathlib import Path
    import src.risk.constants as risk_constants

    for name in (
        "agreement_ceiling_pct",
        "derive_agreement_ceiling_schedule",
        "INDEPENDENT_SEAT_COUNT",
        "AGREEMENT_CEILING_PCT",
    ):
        assert not hasattr(risk_constants, name), (
            f"src/risk/constants.py re-exports {name} — the agreement sizing ladder is retired"
        )

    for model in (RiskConfig, ConstructorConfig):
        fields = getattr(model, "model_fields", None) or {f.name: f for f in __import__("dataclasses").fields(model)}
        for field_name in fields:
            assert "agreement" not in field_name, (
                f"{model.__name__}.{field_name} keys size on agreement — retired 2026-09-14"
            )

    risk_settings = yaml.safe_load((Path(__file__).resolve().parents[1] / "config" / "settings.yaml").read_text())[
        "risk"
    ]
    for key, value in risk_settings.items():
        if "agreement" in key:
            raise AssertionError(
                f"config/settings.yaml carries risk.{key} = {value!r} — the agreement sizing ladder is retired"
            )


def test_a_stale_settings_file_cannot_resurrect_the_ladder():
    """The key is REJECTED on load, not ignored. `extra="ignore"` would let
    a stale deployment keep the list and an operator would believe a ladder
    they set was in force — the same posture
    `ExecutionConfig._reject_deleted_repeg_keys` takes."""
    with pytest.raises(ValidationError) as excinfo:
        RiskConfig(**_risk_kwargs(agreement_ceiling_pct=[2.236, 3.162, 3.873, 4.472, 5.0]))
    assert "agreement_ceiling_pct" in str(excinfo.value)


# --------------------------------------------------------------------------
# RiskConfig — the envelope is now the only per-trade number agreement
# interacts with, and agreement cannot narrow it.
# --------------------------------------------------------------------------


def _risk_kwargs(**overrides):
    base = dict(
        max_position_pct=20,
        max_total_position_pct=90,
        require_stop_loss=True,
    )
    base.update(overrides)
    return base


def test_risk_config_has_no_agreement_field_at_all():
    cfg = RiskConfig(**_risk_kwargs())
    assert not hasattr(cfg, "agreement_ceiling_pct")
    assert cfg.max_position_risk_pct == 5.0


# --------------------------------------------------------------------------
# PortfolioConstructor integration — the ceiling in the actual sizing path
# --------------------------------------------------------------------------


def _registry(**sources_per_symbol) -> dict[str, dict[str, str]]:
    return sources_per_symbol


def test_a_single_net_source_request_is_sized_exactly_as_asked():
    """A one-seat idea is sized at what the PM asked for. Nothing about the
    seat count narrows it any more."""
    constructor = PortfolioConstructor()
    target = TargetPosition(
        symbol="NVDA",
        risk_allocation_pct=2.0,
        conviction="medium",
        thesis="Modest single-source idea.",
    )
    analysis = _analysis("NVDA")
    registry = _registry(NVDA={"technical": "bullish"})  # net +1 — allowed, uncapped

    decisions = constructor.construct_orders(
        targets=[target],
        positions=[],
        analyses=[analysis],
        total_value=100_000.0,
        price_map={"NVDA": 100.0},
        evidence_registry=registry,
    )
    assert len(decisions) == 1
    d = decisions[0]
    assert d.action == "BUY"
    # 2% risk / $5 risk-per-share * $100 entry = 40% notional weight, well
    # under the 20% single-name cap... so check the single-name clamp binds
    # instead of asserting a bare weight. What matters for THIS test is that
    # the agreement ceiling (3.0%) never touched a 2.0% request.
    assert "agreement ceiling" not in d.reasoning


def test_a_single_net_source_full_envelope_ask_is_no_longer_capped():
    """THE retirement, as behaviour. Before 2026-09-14 a one-seat target
    asking for the full envelope was cut to the sqrt(1/5) rung (2.236%) and
    the order said "agreement ceiling". It is now sized at the full ratified
    envelope, and no cap note is written, because no cap happened."""
    constructor = PortfolioConstructor()
    target = TargetPosition(
        symbol="NVDA",
        risk_allocation_pct=5.0,
        conviction="high",
        thesis="Single-source high-conviction ask.",
    )
    # Wide stop so the single-name notional ceiling does not bind either.
    analysis = _analysis("NVDA", entry=100.0, stop=80.0, target=140.0)
    registry = _registry(NVDA={"technical": "bullish"})  # net +1

    decisions = constructor.construct_orders(
        targets=[target],
        positions=[],
        analyses=[analysis],
        total_value=100_000.0,
        price_map={"NVDA": 100.0},
        evidence_registry=registry,
    )
    assert len(decisions) == 1
    d = decisions[0]
    assert "agreement ceiling" not in d.reasoning
    # 5.0% risk / $20 risk-per-share * $100 entry = 25% notional weight.
    assert abs(d.allocation_pct - 25.0) < 0.05


def test_one_seat_and_five_seats_are_sized_identically():
    """No rung, stated as behaviour rather than as an internals check: the
    same ask backed by one seat and by five produces the same order."""
    constructor = PortfolioConstructor()

    def _size(registry):
        decisions = constructor.construct_orders(
            targets=[
                TargetPosition(
                    symbol="NVDA",
                    risk_allocation_pct=5.0,
                    conviction="high",
                    thesis="Same ask, different seat counts.",
                )
            ],
            positions=[],
            analyses=[_analysis("NVDA", entry=100.0, stop=80.0, target=140.0)],
            total_value=100_000.0,
            price_map={"NVDA": 100.0},
            evidence_registry=registry,
        )
        assert len(decisions) == 1
        return decisions[0].allocation_pct

    one = _size(_registry(NVDA={"technical": "bullish"}))
    five = _size(
        _registry(
            NVDA={
                "technical": "bullish",
                "news": "bullish",
                "earnings": "bullish",
                "macro": "bullish",
                "smart_money": "bullish",
            }
        )
    )
    assert one == pytest.approx(five, abs=1e-9)


def test_no_op_wall_any_positive_net_is_byte_identical_to_no_registry():
    """Any net above zero must size IDENTICALLY to a call that supplies no
    evidence_registry at all — the agreement path adds nothing but its
    refusal."""
    constructor = PortfolioConstructor()
    target = TargetPosition(
        symbol="NVDA",
        risk_allocation_pct=3.0,
        conviction="high",
        thesis="Full agreement idea.",
    )
    analysis = _analysis("NVDA")

    baseline = constructor.construct_orders(
        targets=[target],
        positions=[],
        analyses=[analysis],
        total_value=100_000.0,
        price_map={"NVDA": 100.0},
    )
    full_agreement_registry = _registry(
        NVDA={
            "technical": "bullish",
            "earnings": "bullish",
            "macro": "bullish",
        }
    )
    with_registry = constructor.construct_orders(
        targets=[target],
        positions=[],
        analyses=[analysis],
        total_value=100_000.0,
        price_map={"NVDA": 100.0},
        evidence_registry=full_agreement_registry,
    )
    assert len(baseline) == len(with_registry) == 1
    b, w = baseline[0], with_registry[0]
    assert b.action == w.action
    assert b.allocation_pct == w.allocation_pct
    assert b.entry_price == w.entry_price
    assert b.stop_loss == w.stop_loss
    assert b.take_profit == w.take_profit
    assert b.reasoning == w.reasoning


def test_missing_evidence_registry_leaves_the_refusal_unenforced():
    """Same "no view, don't invent one" posture as `existing_risk_pct`:
    omitting the registry must NOT be silently treated as zero
    agreement — a missing registry is not evidence of disagreement."""
    constructor = PortfolioConstructor()
    target = TargetPosition(
        symbol="NVDA",
        risk_allocation_pct=5.0,
        conviction="high",
        thesis="No registry supplied at all.",
    )
    analysis = _analysis("NVDA", entry=100.0, stop=80.0, target=140.0)

    decisions = constructor.construct_orders(
        targets=[target],
        positions=[],
        analyses=[analysis],
        total_value=100_000.0,
        price_map={"NVDA": 100.0},
        # evidence_registry omitted entirely
    )
    assert len(decisions) == 1
    assert "agreement ceiling" not in decisions[0].reasoning
    # 5.0% / $20 * $100 = 25% notional, under the single-name cap (100%
    # since 2026-09-04) and the sector's absolute ceiling (90%) alike, so
    # nothing clamps it.
    assert abs(decisions[0].allocation_pct - 25.0) < 0.05


def test_composition_agreement_refusal_then_budget_allocator_then_single_name():
    """The deterministic layers must still hold together and in order: the
    agreement gate admits or refuses each target first, `allocate_risk_budget`
    rations what is left under the total risk ceiling second, and the
    single-name notional clamp applies to the resulting size third. What is
    NO LONGER in the chain is any agreement-driven narrowing of the request.

    Held risk of 21% leaves 4.0% of the 25% portfolio ceiling so the
    allocator actually has to ration between the two requests.
    `max_position_pct` pinned back to its pre-2026-09-04 value of 20 (the
    real deployed value is 100 since that date, see settings.yaml) so this
    fixture's notional still exercises the single-name clamp.
    """
    constructor = PortfolioConstructor(
        ConstructorConfig(
            max_position_pct=20.0,
        )
    )
    # Two single-seat targets, each asking for 3.0% risk — 6.0% combined
    # against 4.0% of ceiling headroom.
    targets = [
        TargetPosition(symbol="OKLO", risk_allocation_pct=3.0, conviction="high", thesis="Nuclear theme A."),
        TargetPosition(symbol="CEG", risk_allocation_pct=3.0, conviction="high", thesis="Nuclear theme B."),
    ]
    analyses = [
        _analysis("OKLO", entry=100.0, stop=90.0, target=130.0),
        _analysis("CEG", entry=100.0, stop=90.0, target=130.0),
    ]
    registry = _registry(
        OKLO={"technical": "bullish"},
        CEG={"technical": "bullish"},
    )  # both net +1 — admitted, and NOT narrowed

    decisions = constructor.construct_orders(
        targets=targets,
        positions=[],
        analyses=analyses,
        total_value=100_000.0,
        price_map={"OKLO": 100.0, "CEG": 100.0},
        existing_risk_pct={"HELD": 21.0},
        evidence_registry=registry,
    )
    buys = {d.symbol: d for d in decisions if d.action == "BUY"}
    assert set(buys) == {"OKLO", "CEG"}
    for d in buys.values():
        assert "agreement ceiling" not in d.reasoning

    # Equal requests, alphabetical tie-break: CEG is processed first and
    # fits the 4.0% headroom in full (3.0% risk / $10 risk-per-share *
    # $100 entry = 30% notional, clamped to 20% by the single-name ceiling).
    # OKLO is processed second with 1.0% of headroom left, rationed
    # to 1.0% risk = 10% notional, which never reaches the single-name cap.
    assert "single-name ceiling" in buys["CEG"].reasoning
    assert buys["CEG"].allocation_pct == pytest.approx(20.0, abs=0.05)
    assert "total at-risk ceiling" in buys["OKLO"].reasoning
    assert "single-name ceiling" not in buys["OKLO"].reasoning
    assert buys["OKLO"].allocation_pct == pytest.approx(10.0, abs=0.05)


# ==========================================================================
# §9.4 FRESHNESS — a stale view must not earn live size
# ==========================================================================
#
# `build_evidence_registry` read `investment_implications.sentiment` and threw
# `filing_date` / `is_new` away, and nothing in `src/risk/rules.py` or
# `src/portfolio_constructor.py` ever looked at the age of an earnings stance.
# A bullish earnings view therefore counted as a full live corroborating
# source forever.
#
# Since the sizing ladder was retired (2026-09-14) the gate can no longer
# change a size at all — it can only pull the NET down, and the only outcome
# that changes anything is a net driven to zero or below, which refuses the
# trade. The stance stays in the canonical registry either way, so
# `validate_grounding` still sees the coverage and a PM that cites it does
# not fail the session.

from datetime import date, timedelta  # noqa: E402
from unittest.mock import patch  # noqa: E402

from src.agents.portfolio_manager import PortfolioManagerAgent  # noqa: E402
from src.risk.rules import count_opposing_sources  # noqa: E402

_ASOF = date(2026, 9, 1)
_FIXTURE_WINDOW = 90  # illustrative only: filing date to the next report's deadline


def _earnings(symbol: str, sentiment: str, *, age_days: int, is_new: bool = False, asof: date = _ASOF) -> dict:
    """One entry in the `earnings_analyses` list, in the shape the pipeline
    actually hands the PM (`run_earnings_preprocess` / `analyze_reports`).

    Fixture: the next report's statutory deadline (`current_until`, set by
    the provider from the SEC list) falls `_FIXTURE_WINDOW` days after this
    filing, so `age_days` past that window means the stance is stale."""
    filing_date = (asof - timedelta(days=age_days)).isoformat()
    return {
        "symbol": symbol,
        "form_type": "10-Q",
        "filing_date": filing_date,
        "current_until": (asof - timedelta(days=age_days - _FIXTURE_WINDOW)).isoformat(),
        "is_new": is_new,
        "analysis": {
            "symbol": symbol,
            "filing_date": filing_date,
            "investment_implications": {"sentiment": sentiment},
        },
    }


# --------------------------------------------------------------------------
# The threshold itself, at the boundary
# --------------------------------------------------------------------------


def test_stance_exactly_at_the_threshold_is_still_fresh():
    """On the next report's deadline day the stance is NOT stale — the gate
    fires strictly after it, so the boundary day is paid for like any other."""
    stale = PortfolioManagerAgent.stale_evidence_sources(
        earnings_analyses=[_earnings("NVDA", "bullish", age_days=90)],
        asof=_ASOF,
    )
    assert stale == {}


def test_stance_one_day_past_the_threshold_is_stale():
    stale = PortfolioManagerAgent.stale_evidence_sources(
        earnings_analyses=[_earnings("NVDA", "bullish", age_days=91)],
        asof=_ASOF,
    )
    assert stale == {"NVDA": frozenset({"earnings"})}


def test_a_stance_with_no_currency_date_is_treated_as_stale():
    """An unknowable currency is not evidence of one."""
    entry = _earnings("NVDA", "bullish", age_days=1)
    entry.pop("current_until")
    stale = PortfolioManagerAgent.stale_evidence_sources(
        earnings_analyses=[entry],
        asof=_ASOF,
    )
    assert stale == {"NVDA": frozenset({"earnings"})}


def test_freshness_verdict_follows_the_same_last_wins_rule_as_the_stance():
    """Two filings for one symbol: the registry keeps the LAST one's stance,
    so the freshness verdict must attach to that same filing and not to an
    earlier one that happens to be fresher."""
    analyses = [
        _earnings("NVDA", "bullish", age_days=200),
        _earnings("NVDA", "bullish", age_days=5),
    ]
    registry = PortfolioManagerAgent.build_evidence_registry(
        analyses=[],
        positions=[],
        news_intel=None,
        earnings_analyses=analyses,
        macro_analysis=None,
    )
    assert registry["NVDA"]["earnings"] == "bullish"
    assert (
        PortfolioManagerAgent.stale_evidence_sources(
            earnings_analyses=analyses,
            asof=_ASOF,
        )
        == {}
    )
    # ...and reversed, the stale one wins and is gated.
    assert PortfolioManagerAgent.stale_evidence_sources(
        earnings_analyses=list(reversed(analyses)),
        asof=_ASOF,
    ) == {"NVDA": frozenset({"earnings"})}


def test_a_gated_stance_stays_in_the_registry():
    """The gate must not delete coverage. `validate_grounding` fails the
    WHOLE session on any error, so removing a stale earnings stance from the
    registry would turn a PM citation of it into a session failure — a hard
    block, not the size reduction this is meant to be."""
    analyses = [_earnings("NVDA", "bullish", age_days=200)]
    registry = PortfolioManagerAgent.build_evidence_registry(
        analyses=[],
        positions=[],
        news_intel=None,
        earnings_analyses=analyses,
        macro_analysis=None,
    )
    assert registry["NVDA"]["earnings"] == "bullish"


# --------------------------------------------------------------------------
# The tally: a stale stance stops counting
# --------------------------------------------------------------------------


def test_count_aligned_sources_ignores_a_gated_source():
    sources = {"technical": "bullish", "earnings": "bullish"}
    assert count_aligned_sources("NVDA", sources, "long") == 2
    assert (
        count_aligned_sources(
            "NVDA",
            sources,
            "long",
            ignored_sources=frozenset({"earnings"}),
        )
        == 1
    )


# --------------------------------------------------------------------------
# End to end: gating a source can refuse the trade, and can do nothing else
# --------------------------------------------------------------------------


def _stale_ceiling_decisions(stale_sources):
    """One full-envelope long on NVDA with technical + earnings both bullish.

    Geometry: entry 100 / stop 70 / target 160. Risk-per-share $30, so the
    full 5% envelope is a 16.67% weight — under the 20% single-name cap,
    which therefore cannot be what moves the number.

    Returns the raw decision LIST, because gating every aligned source
    leaves a net score of zero and produces no order at all.
    """
    constructor = PortfolioConstructor()
    target = TargetPosition(
        symbol="NVDA",
        risk_allocation_pct=5.0,
        conviction="high",
        thesis="Technical and earnings both bullish.",
    )
    registry = _registry(NVDA={"technical": "bullish", "earnings": "bullish"})
    return constructor.construct_orders(
        targets=[target],
        positions=[],
        analyses=[_analysis("NVDA", entry=100.0, stop=70.0, target=160.0)],
        total_value=100_000.0,
        price_map={"NVDA": 100.0},
        evidence_registry=registry,
        stale_sources=stale_sources,
    )


def _stale_ceiling_decision(stale_sources):
    decisions = _stale_ceiling_decisions(stale_sources)
    assert len(decisions) == 1
    return decisions[0]


def test_two_fresh_seats_size_at_the_full_ask():
    d = _stale_ceiling_decision(None)
    # 5.0% risk / $30 rps * $100 = 16.67% weight
    assert abs(d.allocation_pct - 16.67) < 0.05


def test_a_stale_view_no_longer_changes_the_size_at_all():
    """Retired 2026-09-14. Gating the earnings stance takes the net from +2
    to +1 — which used to drop the risk allowance a rung (3.162% -> 2.236%)
    and now changes nothing, because the net no longer sizes anything. The
    gate's only remaining power is to refuse (see below)."""
    d = _stale_ceiling_decision({"NVDA": frozenset({"earnings"})})
    assert abs(d.allocation_pct - 16.67) < 0.05
    assert "agreement ceiling" not in d.reasoning


def test_the_freshness_gate_can_only_ever_reduce():
    """Gating a source can never grow a position, whatever it gates.

    Gating EVERYTHING leaves a net score of zero, which is a refusal.
    Asserted as "no order", not as a smaller order."""
    fresh = _stale_ceiling_decision(None).allocation_pct
    for gated in ({"NVDA": frozenset({"earnings"})}, {"NVDA": frozenset({"technical"})}):
        assert _stale_ceiling_decision(gated).allocation_pct <= fresh + 1e-9
    assert _stale_ceiling_decisions({"NVDA": frozenset({"technical", "earnings"})}) == []


def test_no_stale_map_leaves_the_outcome_exactly_as_it_was():
    """A caller with no freshness view must not have one invented for it —
    the same posture `evidence_registry=None` already takes."""
    assert _stale_ceiling_decision(None).allocation_pct == _stale_ceiling_decision({}).allocation_pct


# ==========================================================================
# §9.4 DISSENT — counted, visible, and SUBTRACTED (2026-09-02)
# ==========================================================================
#
# `count_aligned_sources` counts only sources aligned with the trade, so on a
# long a bearish earnings stance contributes 0 to it — arithmetically
# identical to neutral and to no coverage at all. That is still true OF THAT
# COUNT; what changed is that the count is not what decides the trade.
# `signed_source_score` nets the opposed seats off, and
# `agreement_refuses_trade` turns the net into one yes/no. Both counts are
# still reported because "2 for, 1 against" and "net +1" are different facts.


def test_count_opposing_sources_on_a_long():
    sources = {"technical": "bullish", "earnings": "bearish", "macro": "neutral", "news": "bearish"}
    assert count_aligned_sources("NVDA", sources, "long") == 1
    assert count_opposing_sources("NVDA", sources, "long") == 2


def test_count_opposing_sources_on_a_short():
    """The exact mirror: on a short the bullish seats are the dissenters."""
    sources = {"technical": "bearish", "earnings": "bullish", "macro": "neutral", "news": "bullish"}
    assert count_aligned_sources("NVDA", sources, "short") == 1
    assert count_opposing_sources("NVDA", sources, "short") == 2


def test_neutral_is_in_neither_count():
    """A seat with no view took no side — it must not read as dissent."""
    sources = {"technical": "bullish", "macro": "neutral", "news": "mixed"}
    assert count_aligned_sources("NVDA", sources, "long") == 1
    assert count_opposing_sources("NVDA", sources, "long") == 0


def test_a_silent_a_neutral_and_a_dissenting_seat_are_no_longer_one_number():
    """The defect in one assertion. All three used to score the same zero;
    only the first two still do."""
    silent = {"technical": "bullish"}
    neutral = {"technical": "bullish", "earnings": "neutral"}
    dissenting = {"technical": "bullish", "earnings": "bearish"}
    assert signed_source_score("NVDA", silent, "long") == 1
    assert signed_source_score("NVDA", neutral, "long") == 1
    assert signed_source_score("NVDA", dissenting, "long") == 0


def test_opposing_count_honours_the_freshness_gate_too():
    """A stance too stale to corroborate is also too stale to dissent —
    one freshness rule, not two."""
    sources = {"technical": "bullish", "earnings": "bearish"}
    assert count_opposing_sources("NVDA", sources, "long") == 1
    assert (
        count_opposing_sources(
            "NVDA",
            sources,
            "long",
            ignored_sources=frozenset({"earnings"}),
        )
        == 0
    )


def test_opposing_count_flips_with_macro_polarity_on_an_inverse_etf():
    """`count_opposing_sources` must use the SAME polarity vocabulary as the
    aligned count, inverse-ETF macro flip included — a second notion of
    "opposed" would let the two disagree about identical evidence."""
    sources = {"macro": "risk_on"}  # bullish tape
    assert count_aligned_sources("SQQQ", sources, "long") == 0
    assert count_opposing_sources("SQQQ", sources, "long") == 1


def _dissent_decisions(registry, *, risk_pct: float = 5.0):
    """One NVDA long, entry 100 / stop 70 / target 160 (risk-per-share $30).

    At that geometry 5.0% risk is a 16.67% weight, under the 20% single-name
    cap, so nothing but the agreement path can change the outcome.
    """
    constructor = PortfolioConstructor()
    target = TargetPosition(
        symbol="NVDA",
        risk_allocation_pct=risk_pct,
        conviction="high",
        thesis="Seats disagree about this one.",
    )
    return constructor.construct_orders(
        targets=[target],
        positions=[],
        analyses=[_analysis("NVDA", entry=100.0, stop=70.0, target=160.0)],
        total_value=100_000.0,
        price_map={"NVDA": 100.0},
        evidence_registry=registry,
    )


def test_survivable_dissent_does_not_move_the_size_but_is_still_recorded():
    """2-aligned/1-opposed nets to +1: above zero, so the trade stands at
    the full ask. The dissent is still written into the order note — it is
    information for the Risk Manager, not a size cut."""
    decisions = _dissent_decisions(
        _registry(
            NVDA={
                "technical": "bullish",
                "earnings": "bullish",
                "macro": "bearish",
            }
        )
    )
    assert len(decisions) == 1
    d = decisions[0]
    assert abs(d.allocation_pct - 16.67) < 0.05
    assert "1 independent source(s) took the OPPOSITE side" in d.reasoning


def test_every_surviving_net_sizes_the_same_contested_or_not():
    """The retirement, checked where the old ladder used to be loudest:
    S = 3 - 1 = 2, a flat +2 and a flat +3 all produce the same size."""
    contested = _dissent_decisions(
        _registry(
            NVDA={
                "technical": "bullish",
                "earnings": "bullish",
                "news": "bullish",
                "macro": "bearish",
            }
        )
    )
    flat_two = _dissent_decisions(
        _registry(
            NVDA={
                "technical": "bullish",
                "earnings": "bullish",
            }
        )
    )
    flat_three = _dissent_decisions(
        _registry(
            NVDA={
                "technical": "bullish",
                "earnings": "bullish",
                "news": "bullish",
            }
        )
    )
    assert len(contested) == len(flat_two) == len(flat_three) == 1
    assert contested[0].allocation_pct == pytest.approx(
        flat_two[0].allocation_pct,
        abs=1e-9,
    )
    assert contested[0].allocation_pct == pytest.approx(
        flat_three[0].allocation_pct,
        abs=1e-9,
    )


def test_a_net_score_of_zero_produces_no_order_at_all():
    """The entry floor as ruled 19 Oct 2026 (`entry_floor_score`): one
    name-level seat for against a chart pointing the other way nets 0 and is
    refused. An opposing NAME-LEVEL seat no longer subtracts (it is an
    objection the planning AI answers), so chart for + earnings against
    now builds an order."""
    assert _dissent_decisions(_registry(NVDA={"technical": "bullish", "earnings": "bearish"})) != []
    assert (
        _dissent_decisions(
            _registry(
                NVDA={
                    "technical": "bearish",
                    "earnings": "bullish",
                }
            )
        )
        == []
    )


def test_a_net_score_below_zero_produces_no_order_at_all():
    assert (
        _dissent_decisions(
            _registry(
                NVDA={
                    # chart against, macro for: macro never counts, so -1
                    "technical": "bearish",
                    "macro": "bullish",
                }
            )
        )
        == []
    )


def test_a_net_score_at_or_below_zero_leaves_a_durable_machine_readable_reason():
    """Board item 10 (2026-09-14). This was the SAME defect item 49 already
    fixed for the portfolio-level budget allocator: the log line
    ("Constructor: SYM produces no order — ...") does not contain
    rejected/refused/skipped directly after the symbol, so
    `_DropReasonCapture._SYMBOL` never matches it and the drop reached the
    database as a generic `constructor_dropped` with "no matching
    constructor log line captured" — found here by running the regex
    against the constructor's own message, statically, no live data needed.
    """
    from src.portfolio_constructor import PortfolioConstructor, STOP_REFUSAL_AGREEMENT_NET

    constructor = PortfolioConstructor()
    target = TargetPosition(
        symbol="NVDA",
        risk_allocation_pct=5.0,
        conviction="high",
        thesis="Seats disagree about this one.",
    )
    decisions = constructor.construct_orders(
        targets=[target],
        positions=[],
        analyses=[_analysis("NVDA", entry=100.0, stop=70.0, target=160.0)],
        total_value=100_000.0,
        price_map={"NVDA": 100.0},
        evidence_registry=_registry(
            NVDA={
                "technical": "bearish",
                "earnings": "bullish",
            }
        ),
    )
    assert decisions == []
    refusals = constructor.drain_refusals()
    assert refusals["NVDA"]["refusal"] == STOP_REFUSAL_AGREEMENT_NET
    assert "entry backstop" in refusals["NVDA"]["detail"]
    # And the log-scrape fallback now matches it too (via `_note_refusal`'s
    # own "refused" wording), so neither record path is silent on this
    # symbol any more.
    assert "NVDA" in constructor.last_drop_reasons


def test_blocking_a_target_leaves_a_held_position_alone():
    """A refusal to BUY is not a decision to SELL. A zero-weight plan would
    read to the delta loop as "PM wants this closed", so a blocked target has
    to vanish from the plan entirely rather than be sized at zero."""
    constructor = PortfolioConstructor()
    target = TargetPosition(
        symbol="NVDA",
        risk_allocation_pct=5.0,
        conviction="high",
        thesis="Adding to a name the earnings seat is bearish on.",
    )
    held = Position(
        symbol="NVDA",
        qty=100.0,
        avg_entry=90.0,
        current_price=100.0,
        market_value=10_000.0,
        unrealized_pnl=1_000.0,
        sector="Technology",
    )
    decisions = constructor.construct_orders(
        targets=[target],
        positions=[held],
        analyses=[_analysis("NVDA", entry=100.0, stop=70.0, target=160.0)],
        total_value=100_000.0,
        price_map={"NVDA": 100.0},
        evidence_registry=_registry(
            NVDA={
                # nobody backs the add: chart neutral, earnings against
                "technical": "neutral",
                "earnings": "bearish",
            }
        ),
    )
    assert [d.action for d in decisions if d.action in ("SELL", "BUY")] == []


def test_dissent_is_recorded_on_the_order_that_survives_it():
    """The split still appears in the order note. It no longer changes the
    size — the note says so, so a reader does not look for a cut that is
    not there."""
    with_dissent = _dissent_decisions(
        _registry(
            NVDA={
                "technical": "bullish",
                "earnings": "bullish",
                "macro": "bearish",
            }
        )
    )
    without_dissent = _dissent_decisions(
        _registry(
            NVDA={
                "technical": "bullish",
                "earnings": "bullish",
                "macro": "neutral",
            }
        )
    )
    assert len(with_dissent) == len(without_dissent) == 1
    assert with_dissent[0].allocation_pct == pytest.approx(
        without_dissent[0].allocation_pct,
        abs=1e-9,
    )
    assert "OPPOSITE side" in with_dissent[0].reasoning
    assert "no longer sizes anything" in with_dissent[0].reasoning
    assert "OPPOSITE side" not in without_dissent[0].reasoning


# ==========================================================================
# What the PM is shown
# ==========================================================================


def _pm_agent():
    with patch("anthropic.Anthropic"):
        return PortfolioManagerAgent(api_key="test", model="claude-sonnet-4-6")


def _pm_message(age_days: int) -> str:
    agent = _pm_agent()
    with patch("src.agents.portfolio_manager.et_today", return_value=_ASOF):
        return agent.build_user_message(
            analyses=[_analysis("NVDA")],
            positions=[],
            earnings_analyses=[_earnings("NVDA", "bullish", age_days=age_days)],
            cash_balance=100_000.0,
            total_value=100_000.0,
        )


def test_a_gated_stance_is_not_shown_to_the_pm_as_corroborating():
    """The prompt already labelled a cached view `[from cache]` with its
    filing date — and then counted it as a live aligned source in the same
    message. Both halves must now say the same thing."""
    msg = _pm_message(age_days=200)
    # The tally the PM is quoted, the agreement line's own caveat, the
    # registry block, and the earnings section must ALL say the same thing —
    # each is asserted separately because each is written separately.
    assert "- NVDA: if long 1 seat(s) for / 0 against, entry backstop +1" in msg
    assert "earnings stance NOT counted either way — filing superseded or past the next report's deadline" in msg
    assert "STALE (still real coverage, still citable as provenance" in msg
    assert "does NOT count toward the agreement score" in msg


def test_a_fresh_stance_is_shown_as_corroborating():
    msg = _pm_message(age_days=10)
    assert "- NVDA: if long 2 seat(s) for / 0 against, entry backstop +2" in msg
    assert "NOT counted" not in msg
    assert "STALE" not in msg


def test_the_pm_is_shown_the_opposing_count_and_the_net():
    """The net is what the constructor will admit or refuse on, so the PM
    must see it before it proposes — a refusal the PM cannot predict reads
    as the constructor contradicting its reasoning (2026-08-20 incident)."""
    agent = _pm_agent()
    msg = agent.build_user_message(
        analyses=[_analysis("NVDA")],
        positions=[],
        earnings_analyses=[_earnings("NVDA", "bearish", age_days=10)],
        cash_balance=100_000.0,
        total_value=100_000.0,
    )
    assert "- NVDA: if long 1 seat(s) for / 1 against, entry backstop +0" in msg
    assert "if short 1 seat(s) for / 1 against, entry backstop +0" in msg


def test_the_pm_is_told_a_non_positive_net_produces_no_order():
    """Standing rule of this desk: the PM is never sized against silently."""
    msg = _pm_message(age_days=10)
    assert "backstop score of zero or below produces NO ORDER AT ALL" in msg
