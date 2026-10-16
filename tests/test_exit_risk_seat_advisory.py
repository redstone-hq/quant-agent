"""The AI risk seat is ADVISORY on exits (decided 2026-09-19).

Adversary-approved again 2026-10-09. A whole-book or per-name reject no
longer removes any exit: the sell proceeds (the returned veto set is empty,
and the caller drops only what that set names) and the objection is written
per stock, with the seat's reason, as `CODE_AI_RISK_OBJECTION`
(`dropped=False`). What did NOT move: the code-owned named-trigger drop that
runs before the seat, and fail-open when the seat is unavailable.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from src.models import (
    PositionAction,
    PositionReasoningChain,
    PositionReview,
    RiskReasoningChain,
    RiskVerdict,
)
from src.risk.exit_refusal import (
    CODE_AI_RISK_APPROVED,
    CODE_AI_RISK_OBJECTION,
    CODE_AI_RISK_REJECT,
    CODE_AI_RISK_UNAVAILABLE,
    CODE_UNRECOGNIZED_TRIGGER,
)
from tests.test_exit_refusal_coherence import _payloads, _position, _risk_pipeline


def _review(*actions):
    return PositionReview(
        reasoning_chain=PositionReasoningChain(
            macro_continuity_check="stable",
            thesis_progress_check="broken",
            thesis_integrity_check="invalidation hit",
            winners_discipline_check="n/a",
            session_disposition_check="midday",
            execution_rationale="exit",
        ),
        actions=[PositionAction(action=a, symbol=s, reason=r) for a, s, r in actions],
        overall_assessment="exits",
        risk_level="moderate",
    )


def _verdict(approved, reasoning="because", rejected=None):
    return RiskVerdict(
        approved=approved,
        reasoning_chain=RiskReasoningChain(
            rr_audit="n/a",
            signal_fidelity="ok",
            correlation_check="ok",
            event_risk="none",
            sizing_sanity="ok",
            overall="ok",
        ),
        reasoning=reasoning,
        rejected_symbols=rejected or [],
    )


_NAMED = "thesis_invalid triggered"


def _by_code(pipeline, code):
    return {p["symbol"]: p for p in _payloads(pipeline) if p["code"] == code}


def test_whole_book_reject_lets_every_exit_proceed_with_one_objection_each():
    pipeline = _risk_pipeline(_verdict(False, "drawdown state - hold everything"))
    vetoed, verdict = pipeline._risk_review_exits(
        _review(("SELL", "AAA", _NAMED), ("SELL", "BBB", _NAMED)),
        [_position("AAA"), _position("BBB")],
        run_id="r1",
        total_value=100_000.0,
    )
    assert vetoed == set()
    assert verdict is not None and verdict.approved is False
    objections = [p for p in _payloads(pipeline) if p["code"] == CODE_AI_RISK_OBJECTION]
    assert sorted(p["symbol"] for p in objections) == ["AAA", "BBB"]
    for p in objections:
        assert p["dropped"] is False
        assert p["layer"] == "ai_risk"
        assert p["detail"] == "drawdown state - hold everything"
    # No row claims the seat dropped a sale, and no objection is an approval.
    assert not _by_code(pipeline, CODE_AI_RISK_REJECT)
    assert not _by_code(pipeline, CODE_AI_RISK_APPROVED)


def test_per_name_reject_lets_that_exit_proceed_with_its_own_objection():
    pipeline = _risk_pipeline(
        _verdict(
            True,
            "BBB may exit",
            rejected=[{"symbol": "AAA", "reason": "invalidation not confirmed", "category": "other"}],
        )
    )
    vetoed, _ = pipeline._risk_review_exits(
        _review(("SELL", "AAA", _NAMED), ("SELL", "BBB", _NAMED)),
        [_position("AAA"), _position("BBB")],
        run_id="r1",
        total_value=100_000.0,
    )
    assert vetoed == set()
    objections = _by_code(pipeline, CODE_AI_RISK_OBJECTION)
    assert set(objections) == {"AAA"}
    assert objections["AAA"]["detail"] == "invalidation not confirmed"
    assert objections["AAA"]["dropped"] is False
    assert objections["AAA"]["action"] == "SELL"
    assert set(_by_code(pipeline, CODE_AI_RISK_APPROVED)) == {"BBB"}
    assert not _by_code(pipeline, CODE_AI_RISK_REJECT)


def test_named_trigger_drop_still_drops_before_the_seat():
    pipeline = _risk_pipeline(_verdict(False, "would object"))
    vetoed, verdict = pipeline._risk_review_exits(
        _review(("SELL", "AAA", "momentum cooling, prudent to harvest")),
        [_position("AAA")],
        run_id="r1",
        total_value=100_000.0,
    )
    pipeline.risk_manager.review.assert_not_called()
    assert (vetoed, verdict) == (set(), None)
    dropped = _by_code(pipeline, CODE_UNRECOGNIZED_TRIGGER)
    assert dropped["AAA"]["dropped"] is True
    assert not _by_code(pipeline, CODE_AI_RISK_OBJECTION)


def test_seat_unavailable_still_fails_open():
    pipeline = _risk_pipeline(raises=True)
    vetoed, verdict = pipeline._risk_review_exits(
        _review(("SELL", "AAA", _NAMED)),
        [_position("AAA")],
        run_id="r1",
        total_value=100_000.0,
    )
    assert (vetoed, verdict) == (set(), None)
    unavailable = _by_code(pipeline, CODE_AI_RISK_UNAVAILABLE)
    assert unavailable["AAA"]["dropped"] is False
    assert not _by_code(pipeline, CODE_AI_RISK_OBJECTION)


def test_seat_returning_no_verdict_still_fails_open():
    pipeline = _risk_pipeline(verdict=None)
    vetoed, verdict = pipeline._risk_review_exits(
        _review(("SELL", "AAA", _NAMED)),
        [_position("AAA")],
        run_id="r1",
        total_value=100_000.0,
    )
    assert (vetoed, verdict) == (set(), None)
    assert _by_code(pipeline, CODE_AI_RISK_UNAVAILABLE)["AAA"]["dropped"] is False


def test_objection_write_failure_does_not_block_the_sell():
    pipeline = _risk_pipeline(_verdict(False, "hold"))
    pipeline.db.insert_specialist_evidence = MagicMock(side_effect=RuntimeError("db down"))
    vetoed, _ = pipeline._risk_review_exits(
        _review(("SELL", "AAA", _NAMED)),
        [_position("AAA")],
        run_id="r1",
        total_value=100_000.0,
    )
    assert vetoed == set()
