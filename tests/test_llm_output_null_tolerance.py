"""An explicit `null` must never cost the desk a whole analysis — and must
never buy one either.

Background (measured against the production agent_logs snapshot covering
2026-08-14..2026-09-01):

    TechAnalysisResult.thesis_invalid_if          42 nulls / 2,021 occurrences
    MissedOpportunity.theme_durability            25 nulls /    50 occurrences
    MissedOpportunity.universe_addition_reason    11 nulls /    50 occurrences

All three declare a default and none accepted `None`, so pydantic rejected the
whole object before any mode="after" validator ran.

Scope, stated honestly: the 42 nulls span 28 symbols; 4 of those were lost
permanently (EQNR, AMT, EQIX, PLD) and the rest were rescued by a paid retry.
But all 42 were rated `neutral` — verified from the raw JSON, independent of
the models — so no tradeable candidate has been shown lost, and 2026-09-01
(`Batch: 58/58 symbols analyzed`) lost nothing at all. The fix is justified by
those 4 analyses and by the exposure, not by a lost trade.

The two halves of this file are equally load-bearing:

  * Fields WITH a default treat null as an absent key — and the coercion is
    counted, because silently blanking `thesis_invalid_if` throws away the
    soft-exit signal.
  * Fields that decide RISK still reject null. `test_load_bearing_*` is the
    test that must fail if that ever stops being true.
"""

import json
from unittest.mock import MagicMock, patch

import pytest
from pydantic import BaseModel, TypeAdapter, ValidationError

import src.models as models
from src.models import (
    LLMOutputModel,
    MissedOpportunity,
    RiskVerdict,
    SOFT_EXIT_UNKNOWN,
    SellGrade,
    TargetPosition,
    TechAnalysisResult,
    TradeDecision,
    parse_telemetry,
)


@pytest.fixture(autouse=True)
def _clean_telemetry():
    parse_telemetry.reset()
    yield
    parse_telemetry.reset()


def _trc() -> dict:
    return dict(trend="t", momentum="m", volatility="v", volume="vol", support_resistance="sr")


def _tech(**over) -> dict:
    base = dict(
        symbol="SPY",
        rating="buy",
        conviction="high",
        entry_price=500.0,
        stop_loss=490.0,
        reference_target=525.0,
        support_levels=[490.0],
        resistance_levels=[525.0],
        setup_type="range",
        expected_horizon_sessions=10,
        reasoning="x",
        reasoning_chain=_trc(),
        thesis_invalid_if="MA50 breaks",
    )
    base.update(over)
    return base


def _risk_rc() -> dict:
    return dict(
        rr_audit="x", signal_fidelity="x", correlation_check="x", event_risk="x", sizing_sanity="x", overall="x"
    )


# ---------------------------------------------------------------------------
# Half 1 — a null on a DEFAULTED field must not cost us the object
# ---------------------------------------------------------------------------


def test_null_thesis_invalid_if_on_neutral_keeps_the_whole_analysis():
    """The 2026-09-01 production payload: null on a NEUTRAL must not bin the read.

    All 42 measured nulls that week were rated neutral. Actionable null is
    a missing falsifier and is refused (see load-bearing cases), not kept
    as a tradeable don't-know.
    """
    payload = _tech(
        rating="neutral",
        thesis_invalid_if=None,
        entry_price=None,
        stop_loss=None,
        reference_target=None,
        setup_type=None,
        expected_horizon_sessions=None,
        support_levels=[],
        resistance_levels=[],
    )
    r = TechAnalysisResult(**payload)
    assert r.thesis_invalid_if == ""
    assert r.rating == "neutral"


@pytest.mark.parametrize(
    "field_name, expected",
    [
        ("conviction", "medium"),
        ("support_levels", []),  # rejected later by the after-validator
        ("computed_levels", []),
    ],
)
def test_null_on_defaulted_tech_field_takes_the_default(field_name, expected):
    payload = _tech(**{field_name: None})
    if field_name == "support_levels":
        # Keep the analysis valid: an actionable rating still needs A level.
        payload["resistance_levels"] = [525.0]
    r = TechAnalysisResult(**payload)
    assert getattr(r, field_name) == expected


@pytest.mark.parametrize(
    "field_name, expected",
    [
        ("theme_durability", "unknown"),
        ("universe_addition_reason", ""),
    ],
)
def test_null_on_defaulted_missed_opportunity_field(field_name, expected):
    """Production emitted a null here on 25 of 50 `theme_durability` slots.

    Before this rule the entry was dropped by the evening pre-filter and the
    quarterly theme aggregation silently lost it.
    """
    mo = MissedOpportunity(
        symbol="XOM",
        move_pct=6.2,
        miss_category="noise_rally",
        lesson="no signal, legitimate hold",
        **{field_name: None},
    )
    assert getattr(mo, field_name) == expected


def test_actionable_missing_soft_exit_is_refused_not_kept_as_unknown():
    """Omit / empty / null / unknown on a buy is missing, not a trade.

    2026-09-16 coerce-to-unknown kept the analysis and fed Risk a blank
    name. Never-blank: the object does not validate. Neutral may omit.
    """
    # 2026-10-12 (XOM incident): kept with the gap RECORDED, never dropped.
    for kept in (
        TechAnalysisResult(**{k: v for k, v in _tech().items() if k != "thesis_invalid_if"}),
        TechAnalysisResult(**_tech(thesis_invalid_if=None)),
        TechAnalysisResult(**_tech(thesis_invalid_if="")),
        TechAnalysisResult(**_tech(thesis_invalid_if=SOFT_EXIT_UNKNOWN)),
    ):
        assert kept.missing_levels == ["thesis_invalid_if"]
    omitted_neutral = TechAnalysisResult(
        **{
            k: v
            for k, v in _tech(
                rating="neutral",
                entry_price=None,
                stop_loss=None,
                reference_target=None,
                setup_type=None,
                expected_horizon_sessions=None,
                support_levels=[],
                resistance_levels=[],
            ).items()
            if k != "thesis_invalid_if"
        }
    )
    assert omitted_neutral.thesis_invalid_if == ""


def test_stated_soft_exit_survives_and_is_not_replaced_with_unknown():
    r = TechAnalysisResult(**_tech(thesis_invalid_if="daily close below 191.5"))
    assert r.thesis_invalid_if == "daily close below 191.5"


def test_unknown_soft_exit_does_not_replace_the_hard_stop_in_the_verdict():
    """Don't-know is not a stated falsifier; to_verdict must not treat it as one.

    Actionable parse refuses unknown. This exercises the restatement path
    on a constructed-without-validation object (legacy / stored).
    """
    r = TechAnalysisResult.model_construct(**_tech(thesis_invalid_if=SOFT_EXIT_UNKNOWN))
    assert r.thesis_invalid_if == SOFT_EXIT_UNKNOWN
    verdict = r.to_verdict()
    assert "hard stop" in verdict.invalidation
    assert SOFT_EXIT_UNKNOWN not in verdict.invalidation


def test_empty_default_soft_exit_on_neutral_is_not_tallied_as_a_drop():
    """Neutral empty is the schema working, not a null wipe."""
    r = TechAnalysisResult(
        **_tech(
            rating="neutral",
            thesis_invalid_if="",
            entry_price=None,
            stop_loss=None,
            reference_target=None,
            setup_type=None,
            expected_horizon_sessions=None,
            support_levels=[],
            resistance_levels=[],
        ),
    )
    assert r.thesis_invalid_if == ""
    assert parse_telemetry.total_null_coercions() == 0


def test_target_null_catalyst_is_unknown_omitted_stays_empty():
    stated = TargetPosition(
        symbol="AAPL",
        target_weight_pct=3.0,
        conviction="medium",
        thesis="hold the add",
        catalyst="8-K tonight",
        thesis_invalid_if="closes below 191.5",
    )
    assert stated.catalyst == "8-K tonight"
    omitted = TargetPosition(
        symbol="AAPL",
        target_weight_pct=3.0,
        conviction="medium",
        thesis="hold the add",
    )
    assert omitted.catalyst == ""
    assert omitted.thesis_invalid_if == ""
    nulled = TargetPosition(
        symbol="AAPL",
        target_weight_pct=3.0,
        conviction="medium",
        thesis="hold the add",
        catalyst=None,
        thesis_invalid_if=None,
    )
    assert nulled.catalyst == SOFT_EXIT_UNKNOWN
    assert nulled.thesis_invalid_if == SOFT_EXIT_UNKNOWN


def test_assignment_does_not_wipe_a_stated_soft_exit():
    t = TargetPosition(
        symbol="AAPL",
        target_weight_pct=3.0,
        conviction="medium",
        thesis="hold the add",
        catalyst="8-K tonight",
        thesis_invalid_if="closes below 191.5",
    )
    t.risk_allocation_pct = 1.0
    assert t.thesis_invalid_if == "closes below 191.5"
    assert t.catalyst == "8-K tonight"


# ---------------------------------------------------------------------------
# 2026-09-03 fix — an empty string is the same "said nothing" signal as null
#
# Production emitted `""` (not `null`) on `theme_durability` for BIAF and
# GPRO — the original `is None`-only check didn't catch it, so it fell
# through to Literal validation and dropped the whole entry. See
# docs/INCIDENT_HISTORY.md 2026-09-03 "evening analyst audit".
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "field_name, expected",
    [
        ("theme_durability", "unknown"),
        ("universe_addition_reason", ""),
    ],
)
def test_empty_string_on_defaulted_missed_opportunity_field(field_name, expected):
    """The exact production payload shape (BIAF/GPRO, 2026-09-03): `""`
    instead of `null` on a droppable field must still validate and land on
    the field's declared default, not raise.
    """
    mo = MissedOpportunity(
        symbol="XOM",
        move_pct=6.2,
        miss_category="noise_rally",
        lesson="no signal, legitimate hold",
        **{field_name: ""},
    )
    assert getattr(mo, field_name) == expected


def test_empty_string_equals_null_for_theme_durability():
    """The fix's whole claim: `""` and `null` must produce the same object."""
    nulled = MissedOpportunity(
        symbol="XOM",
        move_pct=6.2,
        miss_category="noise_rally",
        lesson="no signal, legitimate hold",
        theme_durability=None,
    )
    empty = MissedOpportunity(
        symbol="XOM",
        move_pct=6.2,
        miss_category="noise_rally",
        lesson="no signal, legitimate hold",
        theme_durability="",
    )
    assert nulled.model_dump() == empty.model_dump()


def test_empty_string_coercion_is_recorded_like_a_null():
    """The empty-string path must feed the same operator-visible telemetry
    as the null path — a silent recovery that never shows up in the
    coercion ledger would be just as invisible as the original bug.
    """
    assert parse_telemetry.total_null_coercions() == 0
    MissedOpportunity(
        symbol="BIAF",
        move_pct=9.0,
        miss_category="noise_rally",
        lesson="no signal, legitimate hold",
        theme_durability="",
    )
    snap = parse_telemetry.snapshot()
    assert snap.get(("MissedOpportunity", "theme_durability")) == 1
    assert "MissedOpportunity.theme_durability" in parse_telemetry.describe_null_coercions()


def test_empty_string_matching_the_fields_own_default_is_not_double_processed():
    """Boundary case from the fix's own code comment: on a field whose
    declared default IS `""` (`universe_addition_reason`), an `""` hit
    "matches the field's own default (no behavior change)" — verify that
    claim literally: the field lands on the identical empty string, no
    double-processing bug, and the object still validates (the
    add/watch-requires-a-reason validator does not misfire on the coercion
    path).

    The comment's "no behavior change" is about the resulting VALUE.
    Empty that already equals the declared default is the schema working,
    not a drop — tallying it produced the 2026-09-16 journal flood.
    """
    parse_telemetry.reset()
    mo = MissedOpportunity(
        symbol="XOM",
        move_pct=6.2,
        miss_category="noise_rally",
        lesson="no signal, legitimate hold",
        universe_addition_reason="",
    )
    assert mo.universe_addition_reason == ""
    assert mo.universe_addition_recommendation == "no"
    assert parse_telemetry.snapshot() == {}


# ---------------------------------------------------------------------------
# Half 2 — a null on a LOAD-BEARING field must still reject the object
#
# If any case here starts passing, an analysis with a missing stop, a missing
# entry, a missing side or an unscaled risk verdict has become tradeable.
# ---------------------------------------------------------------------------

LOAD_BEARING_NULLS = [
    # (label, callable that must raise)
    # reference_target, the AI-named level lists (owner rule 2026-10-09) and
    # entry/stop/setup_type/horizon/thesis_invalid_if (2026-10-12, XOM
    # incident) are NOT load-bearing on the tech read: a null is kept and
    # RECORDED in `missing_levels`; sizing uses the live price and the stop
    # is read from the bars. See tests/test_tech_missing_levels_reach_pm.py.
    # Required fields: no default exists, so there is nothing safe to fall back to.
    ("tech.rating", lambda: TechAnalysisResult(**_tech(rating=None))),
    ("tech.symbol", lambda: TechAnalysisResult(**_tech(symbol=None))),
    ("tech.reasoning", lambda: TechAnalysisResult(**_tech(reasoning=None))),
    ("tech.reasoning_chain", lambda: TechAnalysisResult(**_tech(reasoning_chain=None))),
    # Deny-listed: a default exists but it is an affirmative instruction.
    ("target.direction", lambda: TargetPosition(symbol="SPY", thesis="t", risk_allocation_pct=1.0, direction=None)),
    (
        "verdict.scale_all_buys",
        lambda: RiskVerdict(approved=True, reasoning="r", scale_all_buys=None, reasoning_chain=_risk_rc()),
    ),
    ("verdict.approved", lambda: RiskVerdict(approved=None, reasoning="r", reasoning_chain=_risk_rc())),
    (
        "trade.stop_loss",
        lambda: TradeDecision(
            action="BUY",
            symbol="SPY",
            allocation_pct=2.0,
            entry_price=500.0,
            stop_loss=None,
            take_profit=525.0,
            reasoning="r",
        ),
    ),
    (
        "trade.entry_price",
        lambda: TradeDecision(
            action="BUY",
            symbol="SPY",
            allocation_pct=2.0,
            entry_price=None,
            stop_loss=490.0,
            take_profit=525.0,
            reasoning="r",
        ),
    ),
    (
        "sellgrade.sell_price",
        lambda: SellGrade(
            symbol="SPY",
            sell_date="2026-09-01",
            sell_price=None,
            current_price=510.0,
            pct_move_since_sell=1.0,
            grade="correct",
            reason="r",
        ),
    ),
]


@pytest.mark.parametrize(
    "label, build",
    LOAD_BEARING_NULLS,
    ids=[c[0] for c in LOAD_BEARING_NULLS],
)
def test_load_bearing_null_is_never_silently_accepted(label, build):
    with pytest.raises(ValidationError):
        build()


def test_neutral_rating_still_clears_prices_rather_than_inventing_them():
    """Null tolerance must not resurrect stale numbers on a no-trade read."""
    r = TechAnalysisResult(
        symbol="SPY",
        rating="neutral",
        reasoning="x",
        reasoning_chain=_trc(),
        thesis_invalid_if=None,
    )
    assert r.thesis_invalid_if == ""
    assert r.entry_price is None and r.stop_loss is None
    assert r.reference_target is None and r.risk_reward is None


# ---------------------------------------------------------------------------
# The coercion is COUNTED — recovering the object quietly is not enough
# ---------------------------------------------------------------------------


def test_null_coercion_is_recorded_in_parse_telemetry():
    """Actionable null still tallies; the gap is then recorded on the read."""
    assert parse_telemetry.total_null_coercions() == 0
    assert TechAnalysisResult(**_tech(thesis_invalid_if=None)).missing_levels == ["thesis_invalid_if"]
    snap = parse_telemetry.snapshot()
    assert snap.get(("TechAnalysisResult", "thesis_invalid_if")) == 1
    assert "TechAnalysisResult.thesis_invalid_if" in parse_telemetry.describe_null_coercions()


def test_clean_payload_records_nothing():
    TechAnalysisResult(**_tech())
    assert parse_telemetry.total_null_coercions() == 0
    assert parse_telemetry.describe_null_coercions() == ""


def test_nulls_on_optional_fields_are_not_counted_as_coercions():
    """`X | None` nulls are the schema working, not degrading.

    A `neutral` read nulls entry/target/stop BY DESIGN — production emits
    1,111 such nulls per ~2,000 tech items. Counting those would fire the
    `analysis_field_nulled` advisory on every session with a four-figure
    number and make the signal worthless, which is the failure mode this
    whole change exists to avoid.
    """
    TechAnalysisResult(
        symbol="SPY",
        rating="neutral",
        reasoning="x",
        reasoning_chain=_trc(),
        entry_price=None,
        stop_loss=None,
        reference_target=None,
        setup_type=None,
        expected_horizon_sessions=None,
        signal_age_days=None,
        atr_14=None,
    )
    assert parse_telemetry.snapshot() == {}, "an Optional field's null must never reach the coercion ledger"


def test_droppable_sets_never_include_an_optional_field():
    for _name, cls in _model_classes():
        if not issubclass(cls, LLMOutputModel):
            continue
        for field_name in models._null_droppable_fields(cls):
            assert _rejects_none(cls.model_fields[field_name]), (
                f"{cls.__name__}.{field_name} already accepts None — it must not be treated as droppable"
            )


def test_dropped_item_is_counted_separately_from_a_coercion():
    parse_telemetry.record_dropped_item("TechAnalysisResult", "NVDA")
    assert parse_telemetry.total_dropped() == 1
    assert parse_telemetry.total_null_coercions() == 0
    assert "NVDA" in parse_telemetry.describe_dropped()


def test_suspended_blocks_the_tally_but_not_the_coercion():
    with parse_telemetry.suspended():
        t = TargetPosition(
            symbol="AAPL",
            thesis="t",
            risk_allocation_pct=1.0,
            catalyst=None,
            thesis_invalid_if="closes below 191.5",
        )
    assert t.catalyst == SOFT_EXIT_UNKNOWN  # still recovered
    assert parse_telemetry.snapshot() == {}  # but not counted
    # and the suspension is not sticky
    TargetPosition(
        symbol="AAPL",
        thesis="t",
        risk_allocation_pct=1.0,
        catalyst=None,
        thesis_invalid_if="closes below 191.5",
    )
    assert parse_telemetry.total_null_coercions() == 1


def test_evening_prevalidation_does_not_double_count():
    """`_drop_invalid_missed_opportunities` validates, then EveningReport
    validates the same dicts again. One affected entry must count once."""
    from src.agents.evening_analyst import EveningAnalystAgent

    parsed = {
        "missed_opportunities": [
            {
                "symbol": "XOM",
                "move_pct": 6.2,
                "miss_category": "noise_rally",
                "lesson": "no signal",
                "theme_durability": None,
            }
        ],
    }
    EveningAnalystAgent._drop_invalid_missed_opportunities(parsed)
    assert parse_telemetry.snapshot() == {}, "the dry run must not tally"
    MissedOpportunity(**parsed["missed_opportunities"][0])
    assert parse_telemetry.snapshot() == {("MissedOpportunity", "theme_durability"): 1}


def test_pm_prevalidation_does_not_double_count_and_records_its_drops():
    from src.agents.portfolio_manager import PortfolioManagerAgent

    parsed = {
        "targets": [
            {"symbol": "SPY", "thesis": "t", "risk_allocation_pct": 1.0, "thesis_invalid_if": None},
            {"symbol": "BAD", "thesis": "t"},  # sizes to nothing -> dropped
        ]
    }
    PortfolioManagerAgent._drop_invalid_targets(parsed)
    assert len(parsed["targets"]) == 1
    assert parse_telemetry.snapshot() == {}, "the dry run must not tally"
    assert parse_telemetry.dropped_snapshot() == {("TargetPosition", "BAD"): 1}
    TargetPosition(**parsed["targets"][0])
    assert parse_telemetry.snapshot() == {("TargetPosition", "thesis_invalid_if"): 1}


@patch("anthropic.Anthropic")
def test_tech_analyst_records_the_analysis_it_drops(mock_cls):
    """A candidate the analysts researched and PM never saw is now countable.

    Drives the real `analyze_batch` parse loop with a response whose only
    defect is a malformed load-bearing field, so the item is genuinely
    discarded. Before this, the only trace was one ERROR log line — and none
    at all once the bounded retry recovered the symbol, which is what
    production did on 2026-09-01 (`Batch: 58/58 symbols analyzed`).
    """
    from datetime import date

    from src.agents.tech_analyst import TechAnalystAgent
    from src.models import OHLCV, TechnicalIndicators

    bad = json.dumps(
        [
            {
                "symbol": "SPY",
                "rating": "buy",
                "conviction": "high",
                "entry_price": 507.0,
                "reference_target": 530.0,
                "stop_loss": 999.0,  # above entry on a BUY — fatal
                "support_levels": [494.0],
                "resistance_levels": [530.0],
                "setup_type": "range",
                "expected_horizon_sessions": 10,
                "reasoning_chain": {
                    "trend": "t",
                    "momentum": "m",
                    "volatility": "v",
                    "volume": "vol",
                    "support_resistance": "sr",
                },
                "reasoning": "r",
            }
        ]
    )
    mock_client = MagicMock()
    resp = MagicMock()
    resp.content = [MagicMock(text=bad)]
    resp.usage.input_tokens = 10
    resp.usage.output_tokens = 10
    mock_client.messages.create.return_value = resp
    mock_cls.return_value = mock_client

    agent = TechAnalystAgent(api_key="test", model="claude-sonnet-4-6-20250514")
    bars = [OHLCV(date=date(2026, 4, 7), open=503.0, high=510.0, low=500.0, close=507.0, volume=1_000_000)]
    ind = TechnicalIndicators(symbol="SPY", ma_20=505.0, atr_14=8.5)
    results, _ = agent.analyze_batch(
        [{"symbol": "SPY", "bars": bars, "indicators": ind}],
    )

    assert results == {"SPY": None}
    assert parse_telemetry.dropped_snapshot().get(("TechAnalysisResult", "SPY")), (
        "a discarded analysis must be counted, not only logged"
    )


# ---------------------------------------------------------------------------
# The mechanical guard — this is what stops the class of bug reopening
# ---------------------------------------------------------------------------

# Models NOT parsed from LLM output. A null in one of these comes from our own
# code, and a loud failure is the correct response to our own bug.
NON_LLM_MODELS = {
    "OHLCV",
    "TechnicalIndicators",
    "Position",
    "MissedOpportunitySnapshot",
    "AgentLog",
    "LLMOutputModel",
    # Phase 13: derived in Python from a seat's already-validated report
    # (`TechAnalysisResult.to_verdict`), never parsed from an LLM response.
    # A null here is our own bug and should fail loudly.
    "AnalystVerdict",
    "VerdictEvidence",
}


def _rejects_none(field) -> bool:
    try:
        TypeAdapter(field.annotation).validate_python(None)
    except Exception:
        return True
    return False


def _model_classes():
    for name in dir(models):
        obj = getattr(models, name, None)
        if (
            isinstance(obj, type)
            and issubclass(obj, BaseModel)
            and obj is not BaseModel
            and obj.__module__ == "src.models"
        ):
            yield name, obj


def test_every_llm_parsed_model_with_a_defaulted_field_has_null_tolerance():
    """A new model that forgets the mixin fails HERE, not in production.

    The original bug was fixed one `field_validator` at a time. There are 119
    fields with the same shape, so that approach loses by attrition; this test
    is the mechanical replacement. If you add a model that is parsed from an
    LLM response, inherit `LLMOutputModel`. If it genuinely is not, name it in
    `NON_LLM_MODELS` and say why.
    """
    offenders = []
    for name, cls in _model_classes():
        if name in NON_LLM_MODELS or issubclass(cls, LLMOutputModel):
            continue
        exposed = [f for f, fld in cls.model_fields.items() if not fld.is_required() and _rejects_none(fld)]
        if exposed:
            offenders.append((name, exposed))
    assert not offenders, (
        f"these models can lose a whole object to an explicit null on a field that declares a default: {offenders}"
    )


def test_deny_list_names_only_real_fields():
    """A typo in `_NULL_MUST_FAIL` would silently un-protect a risk field."""
    for cls_name, field_name in models._NULL_MUST_FAIL:
        cls = getattr(models, cls_name)
        assert field_name in cls.model_fields, f"{cls_name}.{field_name} does not exist"
        fld = cls.model_fields[field_name]
        assert not fld.is_required(), f"{cls_name}.{field_name} is required — it needs no deny-list entry"
        assert field_name not in models._null_droppable_fields(cls)


def test_required_fields_are_never_treated_as_droppable():
    for _name, cls in _model_classes():
        if not issubclass(cls, LLMOutputModel):
            continue
        droppable = models._null_droppable_fields(cls)
        for field_name in droppable:
            assert not cls.model_fields[field_name].is_required(), (
                f"{cls.__name__}.{field_name} is required but marked droppable"
            )


def test_every_session_starts_with_zeroed_parse_counters():
    """RiskStage also runs on the intraday scan, which never touches the
    research stage. Resetting in a stage would have made the afternoon
    re-report the morning's losses in a long-lived scheduler process."""
    from src.pipeline_context import RunContext

    parse_telemetry.record_dropped_item("TechAnalysisResult", "NVDA")
    parse_telemetry.record_null_coercion("TechAnalysisResult", "thesis_invalid_if")
    for session in ("morning", "midday", "close", "evening", "intra_check"):
        RunContext.start(session)
        assert parse_telemetry.total_dropped() == 0
        assert parse_telemetry.total_null_coercions() == 0
        parse_telemetry.record_dropped_item("TechAnalysisResult", "NVDA")


def test_unknown_soft_exit_isolates_that_name_and_keeps_the_rest_of_the_plan():
    """MRVL retry: Risk vetoed the whole plan over one nulled falsifier.
    Isolate the empty name before Risk. Do not invent a string. Do not
    drop a sibling with a stated falsifier."""
    from types import SimpleNamespace
    from src.models import SOFT_EXIT_MISSING_AFTER_RETRY
    from src.pipeline_context import RunContext
    from src.pipeline_stages import _isolate_empty_soft_exit_entries

    mrvl = TradeDecision(
        action="BUY",
        symbol="MRVL",
        allocation_pct=3.0,
        entry_price=80.0,
        stop_loss=75.0,
        take_profit=90.0,
        reasoning="retry",
        thesis_invalid_if=SOFT_EXIT_UNKNOWN,
    )
    aapl = TradeDecision(
        action="BUY",
        symbol="AAPL",
        allocation_pct=3.0,
        entry_price=190.0,
        stop_loss=185.0,
        take_profit=205.0,
        reasoning="stated",
        thesis_invalid_if="closes below 185",
    )
    hold = TradeDecision(
        action="HOLD",
        symbol="MSFT",
        allocation_pct=0.0,
        entry_price=400.0,
        stop_loss=390.0,
        take_profit=420.0,
        reasoning="keep",
    )
    plan = SimpleNamespace(
        decisions=[mrvl, aapl, hold],
        targets=[
            TargetPosition(
                symbol="MRVL",
                target_weight_pct=3.0,
                conviction="medium",
                thesis="retry",
                thesis_invalid_if=None,
            ),
            TargetPosition(
                symbol="AAPL",
                target_weight_pct=3.0,
                conviction="medium",
                thesis="add",
                thesis_invalid_if="closes below 185",
            ),
        ],
        constructor_dropped=[],
    )
    pipeline = SimpleNamespace(db=MagicMock())
    ctx = RunContext.start("morning")
    isolated = _isolate_empty_soft_exit_entries(pipeline, ctx, plan)
    assert isolated == ["MRVL"]
    assert [d.symbol for d in plan.decisions] == ["AAPL", "MSFT"]
    assert [t.symbol for t in plan.targets] == ["MRVL", "AAPL"]
    assert "MRVL" in plan.constructor_dropped
    assert not any(d.symbol == "MRVL" for d in plan.decisions)
    assert any(SOFT_EXIT_MISSING_AFTER_RETRY in str(c) for c in pipeline.db.insert_specialist_evidence.mock_calls)
