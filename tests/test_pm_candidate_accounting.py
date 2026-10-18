"""The portfolio manager must account for every candidate it was shown.

Board item 133, 2026-09-18. The defect, reproduced by
`test_defect_every_omitted_candidate_carried_one_identical_reason` below:
the seat returned TARGETS and nothing else, and `DecisionStage` recorded
every analysed candidate missing from that list as

    portfolio_manager | omitted | candidate_not_selected_for_target

The seat was never asked WHY, so there was no per-candidate reason and
there could not be one — which made the jam detector
(`src/refusal_signature.py`) unable to tell a jammed gate from a quiet
market, because the only key it could ever report was that one.

Nothing in this file asserts anything about what the desk may buy. The
accounting is bookkeeping: it runs after `decide()` has already settled
every candidate's fate.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from src.models import (
    CANDIDATE_REJECTION_CODES,
    CandidateRejection,
    PortfolioDecision,
    ReasoningChain,
    TechAnalysisResult,
    TechReasoningChain,
)
from src.pipeline_stages import DecisionStage, RunContext
from src.pm_accounting import (
    CODE_HELD_UNCHANGED,
    CODE_UNACCOUNTED,
    OUTCOME_HELD_UNCHANGED,
    OUTCOME_NOT_SELECTED,
    OUTCOME_UNACCOUNTED,
    account_for_candidates,
    plain_reason,
)
from src.refusal_signature import signature_key
from tests.pipeline_factory import build_pipeline


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


def _chain() -> ReasoningChain:
    return ReasoningChain(
        macro_filter="m",
        news_check="n",
        earnings_check="e",
        signal_conflicts="c",
        sizing_logic="s",
        portfolio_balance="b",
        cash_target="ct",
        continuity_check="cc",
        premortem_check="pm",
        macro_audit="ma",
    )


def _analysis(symbol: str) -> TechAnalysisResult:
    return TechAnalysisResult(
        symbol=symbol,
        rating="buy",
        conviction="medium",
        entry_price=100.0,
        stop_loss=95.0,
        reference_target=115.0,
        support_levels=[95.0],
        resistance_levels=[115.0],
        setup_type="range",
        expected_horizon_sessions=10,
        reasoning_chain=TechReasoningChain(
            trend="x",
            momentum="x",
            volatility="x",
            volume="x",
            support_resistance="x",
        ),
        reasoning="test",
        thesis_invalid_if="closes below support",
    )


def _decision(rejections=None) -> PortfolioDecision:
    return PortfolioDecision(
        reasoning_chain=_chain(),
        targets=[],
        rejections=rejections or [],
        portfolio_view="nothing earned a slot today",
    )


def _pm_result(**over):
    base = dict(
        user_message="m",
        raw_text="{}",
        tokens_used=1,
        input_tokens=1,
        output_tokens=1,
        cost_usd=0.0,
        model="test-model",
        semantic_status=None,
        semantic_error=None,
    )
    base.update(over)
    return MagicMock(**base)


def _pipeline(decide_returns):
    """A DecisionStage-shaped pipeline whose PM returns `decide_returns`.

    `decide_returns` is a list — one entry per expected `decide()` call, so
    a test can give a different answer to the re-ask than to the first ask.
    """
    from src.pipeline import TradingPipeline

    p = build_pipeline(db=MagicMock())
    p.db.get_latest_insights.return_value = None
    p._sweeper = MagicMock(return_value=None)
    for name in ("_compute_recent_performance", "_build_position_history"):
        setattr(p, name, MagicMock(return_value={}))
    for name in (
        "_build_weekly_narrative",
        "_build_macro_trajectory",
        "_build_active_state_changes",
        "_build_pm_recent_decisions",
        "_build_projected_portfolio",
        "_build_calibration_note",
        "_build_macro_tech_alignment",
        "_build_recent_missed_lessons",
        "_build_recent_loss_pits",
    ):
        setattr(p, name, MagicMock(return_value=""))
    p._build_pm_facts = MagicMock(return_value=MagicMock())
    p._ensure_correlation_matrix = MagicMock(return_value={})
    p._require_paid_analysis = MagicMock(return_value=None)
    p._record_heal = MagicMock(return_value=None)
    p.config = MagicMock()
    p.config.risk.allow_margin = False
    p.config.trading.universe = []
    p._last_symbol_sectors = {}
    p.portfolio_constructor = MagicMock()
    p.portfolio_constructor.real_reward_risk_preview.return_value = {}
    p.portfolio_constructor.last_refusals = {}
    p.broker = MagicMock()
    p.portfolio_manager = MagicMock()
    p.portfolio_manager.decide.side_effect = decide_returns
    return p


def _run_stage(pipeline, symbols, positions=None):
    """Run DecisionStage far enough to do the accounting; collect the rows."""
    ctx = RunContext.start("intra_check")
    ctx.positions = list(positions or [])
    ctx.analyses = [_analysis(s) for s in symbols]
    ctx.macro_analysis = None
    ctx.total_value = 100_000.0
    ctx.last_equity = 100_000.0
    ctx.cash = 50_000.0
    ctx.deployable_cash = 50_000.0
    ctx.admitted_symbols = set()

    captured: list[tuple[str, dict]] = []

    def _cap(db, **kw):
        if kw.get("kind") == "pipeline_event" and kw.get("symbol"):
            captured.append((kw["symbol"], json.loads(kw["evidence_json"])))

    with patch("src.pipeline_stages._persist_evidence", _cap):
        try:
            DecisionStage(pipeline=pipeline).run(ctx)
        except Exception:
            # The stage continues into construction/execution, which this
            # harness does not stand up. The accounting has already run.
            pass
    return ctx, captured


# ---------------------------------------------------------------------------
# the defect, and the fix
# ---------------------------------------------------------------------------


def test_defect_every_omitted_candidate_carried_one_identical_reason():
    """REGRESSION LOCK on the shape of the original defect.

    Two sessions, different candidates, no stated grounds: the keys must no
    longer be the string the 2026-09-18 alert reported, and the name must
    now carry a reason that says the seat would not account for it — which
    is a fact about the desk, not a silent non-answer.
    """
    rows = []
    for symbols in (["CMCSA"], ["CRM", "GME"]):
        pipeline = _pipeline(
            [
                (_decision(), _pm_result()),  # first ask: no rejections
                (_decision(), _pm_result()),  # re-ask: still none
            ]
        )
        _, captured = _run_stage(pipeline, symbols)
        rows.extend(captured)

    assert {sym for sym, _ in rows} == {"CMCSA", "CRM", "GME"}
    for symbol, payload in rows:
        key = signature_key(payload, symbol)
        assert "candidate_not_selected_for_target" not in key
        assert payload["outcome"] == OUTCOME_UNACCOUNTED
        assert payload["refusal"] == CODE_UNACCOUNTED
        assert payload["note"], "a durable row must carry reader-facing text"


def test_stated_grounds_become_distinct_per_candidate_reasons():
    """The fix's whole point: two candidates, two grounds, two keys."""
    decision = _decision(
        [
            CandidateRejection.model_validate(
                {"symbol": "CRM", "code": "risk_budget_full", "detail": "no risk budget left for another name"},
            ),
            CandidateRejection.model_validate(
                {"symbol": "GME", "code": "no_readable_structure", "detail": "nearest support is 14% away"},
            ),
        ]
    )
    pipeline = _pipeline([(decision, _pm_result())])
    _, captured = _run_stage(pipeline, ["CRM", "GME"])

    assert pipeline.portfolio_manager.decide.call_count == 1, (
        "a fully accounted decision must not spend the paid re-ask"
    )
    by_symbol = {sym: payload for sym, payload in captured}
    assert by_symbol["CRM"]["refusal"] == "risk_budget_full"
    assert by_symbol["GME"]["refusal"] == "no_readable_structure"
    keys = {signature_key(p, s) for s, p in captured}
    assert len(keys) == 2, "distinct grounds must produce distinct keys"


def test_the_one_reask_is_made_and_its_grounds_are_recorded():
    """Step 2 of the heal order: ask once, then record what came back."""
    answered = _decision(
        [
            CandidateRejection.model_validate(
                {"symbol": "CMCSA", "code": "evidence_insufficient", "detail": "only a neutral technical read exists"},
            ),
        ]
    )
    pipeline = _pipeline(
        [
            (_decision(), _pm_result()),
            (answered, _pm_result()),
        ]
    )
    _, captured = _run_stage(pipeline, ["CMCSA"])

    assert pipeline.portfolio_manager.decide.call_count == 2
    challenge = pipeline.portfolio_manager.decide.call_args.kwargs["accounting_challenge"]
    assert "CMCSA" in challenge
    assert "not an invitation" in challenge, "the re-ask must ask for bookkeeping, never for a new decision"
    payload = dict(captured)["CMCSA"]
    assert payload["outcome"] == OUTCOME_NOT_SELECTED
    assert payload["refusal"] == "evidence_insufficient"


def test_the_reask_cannot_change_the_decision():
    """A paid retry must never become a way to re-decide the book."""
    from src.models import TargetPosition

    reasked = PortfolioDecision(
        reasoning_chain=_chain(),
        targets=[
            TargetPosition(
                symbol="CMCSA",
                risk_allocation_pct=3.0,
                conviction="high",
                thesis="second thoughts",
                thesis_invalid_if="breaks support",
            )
        ],
        rejections=[
            CandidateRejection.model_validate(
                {"symbol": "CMCSA", "code": "event_risk", "detail": "earnings"},
            )
        ],
        portfolio_view="changed my mind",
    )
    first = _decision()
    pipeline = _pipeline([(first, _pm_result()), (reasked, _pm_result())])
    ctx, captured = _run_stage(pipeline, ["CMCSA"])

    assert first.targets == [], "the re-ask's targets must be discarded"
    assert ctx.portfolio_decision is first
    assert dict(captured)["CMCSA"]["refusal"] == "event_risk"


def test_a_held_name_is_accounted_for_mechanically_without_paying():
    """Step 1: the seat's own prompt defines silence on a HELD name as
    'hold unchanged'. Reading that rule back is not inventing a reason, and
    it must not spend the one paid re-ask."""
    position = MagicMock(symbol="AAPL", qty=10)
    pipeline = _pipeline([(_decision(), _pm_result())])
    _, captured = _run_stage(pipeline, ["AAPL"], positions=[position])

    assert pipeline.portfolio_manager.decide.call_count == 1
    payload = dict(captured)["AAPL"]
    assert payload["outcome"] == OUTCOME_HELD_UNCHANGED
    assert payload["refusal"] == CODE_HELD_UNCHANGED


def test_the_reask_is_bounded_by_the_existing_per_seat_cap():
    """No new retry budget is invented: once this seat's one paid retry is
    spent, the name is recorded rather than asked about again."""
    pipeline = _pipeline([(_decision(), _pm_result())])
    ctx = RunContext.start("intra_check")
    ctx.positions = []
    ctx.analyses = [_analysis("CMCSA")]
    ctx.macro_analysis = None
    ctx.total_value = 100_000.0
    ctx.last_equity = 100_000.0
    ctx.cash = 50_000.0
    ctx.deployable_cash = 50_000.0
    ctx.admitted_symbols = set()
    ctx.heal_paid_retries = {
        "portfolio_manager_candidate_accounting": 1,
    }

    captured: list[tuple[str, dict]] = []

    def _cap(db, **kw):
        if kw.get("kind") == "pipeline_event" and kw.get("symbol"):
            captured.append((kw["symbol"], json.loads(kw["evidence_json"])))

    with patch("src.pipeline_stages._persist_evidence", _cap):
        try:
            DecisionStage(pipeline=pipeline).run(ctx)
        except Exception:
            pass

    assert pipeline.portfolio_manager.decide.call_count == 1, "the cap must stop a second paid call"
    payload = dict(captured)["CMCSA"]
    assert payload["refusal"] == CODE_UNACCOUNTED
    assert "already spent or blocked" in payload["note"]


# ---------------------------------------------------------------------------
# the jam detector must get QUIETER, never noisier
# ---------------------------------------------------------------------------


def test_more_distinct_grounds_can_only_shorten_a_streak():
    """The detector fires on ONE unvarying key across sessions. Before, one
    key was all that existed. Distinct grounds can only split that set, so
    the alert can only become rarer — never spurious."""
    from src.refusal_signature import SessionShape

    old = SessionShape(
        run_id="r",
        trading_day="2026-09-17",
        last_seen=None,
        placed_entry=False,
        keys_by_symbol={
            "CRM": "portfolio_manager|omitted|candidate_not_selected_for_target||",
            "GME": "portfolio_manager|omitted|candidate_not_selected_for_target||",
        },
        outcomes_by_symbol={"CRM": "omitted", "GME": "omitted"},
    )
    assert old.is_monomorphic

    decision = _decision(
        [
            CandidateRejection.model_validate(
                {"symbol": "CRM", "code": "risk_budget_full", "detail": "d"},
            ),
            CandidateRejection.model_validate(
                {"symbol": "GME", "code": "event_risk", "detail": "d"},
            ),
        ]
    )
    result = account_for_candidates(
        analyses=[_analysis("CRM"), _analysis("GME")],
        decision=decision,
        positions=[],
    )
    new = SessionShape(
        run_id="r",
        trading_day="2026-09-17",
        last_seen=None,
        placed_entry=False,
        keys_by_symbol={a.symbol: signature_key(a.event_kwargs(), a.symbol) for a in result.accounted},
        outcomes_by_symbol={a.symbol: a.outcome for a in result.accounted},
    )
    assert not new.is_monomorphic


def test_the_candidate_prose_stays_out_of_the_comparable_key():
    """Two names refused on the SAME ground in different words are one
    reason, and must compare as one — otherwise a real jam goes unseen."""
    decision = _decision(
        [
            CandidateRejection.model_validate(
                {"symbol": "CRM", "code": "risk_budget_full", "detail": "the book is already at its risk ceiling"},
            ),
            CandidateRejection.model_validate(
                {"symbol": "GME", "code": "risk_budget_full", "detail": "no budget left, every slot is taken"},
            ),
        ]
    )
    result = account_for_candidates(
        analyses=[_analysis("CRM"), _analysis("GME")],
        decision=decision,
        positions=[],
    )
    keys = {signature_key(a.event_kwargs(), a.symbol) for a in result.accounted}
    assert len(keys) == 1


# ---------------------------------------------------------------------------
# owner-facing wording (board item 89's standard)
# ---------------------------------------------------------------------------


def test_every_rejection_code_has_plain_english():
    for code in CANDIDATE_REJECTION_CODES:
        sentence = plain_reason(code)
        assert sentence and code not in sentence, f"{code} renders as its own internal token"


def test_an_unknown_code_is_described_never_pasted_through():
    assert plain_reason("some_new_gate_token") == (
        "the desk recorded a ground for dropping it that it has no plain wording for"
    )


def test_the_jam_alert_no_longer_shows_a_bare_internal_key():
    """The 2026-09-18 message read, in full:
    `The one reason: portfolio_manager|omitted|candidate_not_selected_for_target||`
    """
    from src.refusal_signature import _human_reason, SessionShape

    session = SessionShape(
        run_id="r",
        trading_day="2026-09-17",
        last_seen=None,
        placed_entry=False,
        keys_by_symbol={
            "CRM": "portfolio_manager|not_selected|pm_rejected_candidate|risk_budget_full|",
        },
        outcomes_by_symbol={"CRM": "not_selected"},
    )
    rendered = _human_reason(session)
    assert rendered.startswith("the book had no risk budget left")
    # The key is kept for whoever has to grep for it — but labelled, and
    # never standing alone as though it were an explanation.
    assert "kept for the record" in rendered


# ---------------------------------------------------------------------------
# the model fails OPEN on shape
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,code",
    [
        ("cmcsa ", "other"),
        ({"symbol": "crm", "reason_code": "Risk Budget Full", "reason": "x"}, "risk_budget_full"),
        ({"symbol": "gme", "code": "invented_token"}, "other"),
    ],
)
def test_a_rejection_is_never_lost_to_a_formatting_slip(raw, code):
    parsed = CandidateRejection.model_validate(raw)
    assert parsed.code == code
    assert parsed.detail, "a rejection must always carry reader-facing text"


def test_an_unrecognised_code_keeps_its_spelling_in_the_detail():
    parsed = CandidateRejection.model_validate(
        {"symbol": "gme", "code": "invented_token", "detail": "because"},
    )
    assert "invented_token" in parsed.detail


def test_a_malformed_rejections_list_never_kills_the_decision():
    """Bookkeeping must not be able to destroy a session's whole plan."""
    from src.agents.portfolio_manager import PortfolioManagerAgent

    parsed = {
        "reasoning_chain": _chain().model_dump(),
        "targets": [],
        "rejections": [{"symbol": "", "code": "event_risk"}, 17],
        "portfolio_view": "v",
    }
    cleaned = PortfolioManagerAgent._drop_invalid_rejections(dict(parsed))
    decision = PortfolioDecision(**cleaned)
    assert decision.portfolio_view == "v"
