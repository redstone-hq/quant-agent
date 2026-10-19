"""Desk archetype step A (owner, 19 Oct 2026): written answers to seat
objections are carried in both deciding AIs' output formats and saved per
symbol as specialist evidence. Plumbing only: no trading behaviour changes
and the field is not yet in the schema the AIs are handed."""

import json

import pytest
from pydantic import ValidationError

from src.models.decisions import ReasoningChain
from src.models.portfolio import PortfolioDecision, TargetPosition
from src.models.positions import ObjectionAnswer, PositionAction, PositionReview
from src.stage_decision import (
    OBJECTION_ANSWER_KIND,
    objection_answer_for,
    record_objection_answers,
)
from src.storage.db import Database

_RC = {
    "macro_filter": "x",
    "news_check": "x",
    "earnings_check": "x",
    "signal_conflicts": "x",
    "sizing_logic": "x",
    "portfolio_balance": "x",
    "cash_target": "x",
}
_REVIEW_RC = {
    "macro_continuity_check": "stable",
    "thesis_progress_check": "ok",
    "thesis_integrity_check": "no triggers",
    "winners_discipline_check": "no flags",
    "session_disposition_check": "patient",
    "execution_rationale": "n/a",
}
_ANSWER = {
    "seat": "Tech_Analyst",
    "objection_direction": "Bearish",
    "answer_text": "Breakdown was on thin volume; the daily close held support.",
    "decision": "OVERRULE",
}


@pytest.fixture
def db(tmp_path):
    database = Database(str(tmp_path / "objection.db"))
    database.initialize()
    return database


def test_planning_output_carries_objection_answers():
    raw = {
        "reasoning_chain": _RC,
        "portfolio_view": "v",
        "targets": [{"symbol": "zzza", "thesis": "t", "risk_allocation_pct": 0.5, "objection_answers": [_ANSWER]}],
    }
    decision = PortfolioDecision.model_validate_json(json.dumps(raw))
    (answer,) = decision.targets[0].objection_answers
    assert answer == ObjectionAnswer(
        seat="tech_analyst",
        objection_direction="bearish",
        answer_text=_ANSWER["answer_text"],
        decision="overrule",
    )


def test_reviewer_output_carries_objection_answers():
    raw = {
        "reasoning_chain": _REVIEW_RC,
        "overall_assessment": "fine",
        "risk_level": "low",
        "actions": [{"action": "HOLD", "symbol": "ZZZB", "reason": "r", "objection_answers": [_ANSWER]}],
    }
    review = PositionReview.model_validate_json(json.dumps(raw))
    assert review.actions[0].objection_answers[0].decision == "overrule"


def test_old_outputs_without_the_field_parse_unchanged():
    decision = PortfolioDecision.model_validate(
        {
            "reasoning_chain": _RC,
            "portfolio_view": "v",
            "targets": [{"symbol": "ZZZA", "thesis": "t", "risk_allocation_pct": 0.5}],
        }
    )
    assert decision.targets[0].objection_answers == []
    action = PositionAction(action="HOLD", symbol="ZZZB", reason="r")
    assert action.objection_answers == []


def test_decision_must_be_overrule_or_accept_and_text_not_blank():
    with pytest.raises(ValidationError):
        ObjectionAnswer(**{**_ANSWER, "decision": "ignore"})
    with pytest.raises(ValidationError):
        ObjectionAnswer(**{**_ANSWER, "answer_text": "   "})


def test_field_is_not_yet_in_the_schema_the_ais_are_handed():
    # Step D changes what the AIs are told; step A must not.
    assert "objection_answers" not in json.dumps(PortfolioDecision.model_json_schema())
    assert "objection_answers" not in json.dumps(PositionReview.model_json_schema())


def test_round_trip_save_and_read(db):
    target = TargetPosition(symbol="ZZZA", thesis="t", risk_allocation_pct=0.5, objection_answers=[_ANSWER])
    assert record_objection_answers(
        db,
        run_id="run-1",
        agent_name="portfolio_manager",
        symbol="zzza",
        answers=target.objection_answers,
        session="morning",
        decision_id="run-1-dec-abc",
    )
    got = objection_answer_for(db, "ZZZA", "TECH_ANALYST")
    assert got["decision"] == "overrule"
    assert got["answer_text"] == _ANSWER["answer_text"]
    assert got["objection_direction"] == "bearish"
    assert (got["run_id"], got["session"], got["decision_id"]) == ("run-1", "morning", "run-1-dec-abc")
    row = db.conn.execute("SELECT kind, scope, symbol, decision_id, agent_name FROM specialist_evidence").fetchone()
    assert tuple(row) == (OBJECTION_ANSWER_KIND, "symbol", "ZZZA", "run-1-dec-abc", "portfolio_manager")
    assert OBJECTION_ANSWER_KIND == "objection_answer"


def test_reader_misses_other_seat_other_run_and_no_answers(db):
    assert not record_objection_answers(
        db, run_id="run-1", agent_name="position_reviewer", symbol="ZZZC", answers=[], session="midday"
    )
    assert objection_answer_for(db, "ZZZC", "tech_analyst") is None
    record_objection_answers(
        db,
        run_id="run-2",
        agent_name="position_reviewer",
        symbol="ZZZC",
        answers=[ObjectionAnswer(**_ANSWER)],
        session="midday",
    )
    assert objection_answer_for(db, "ZZZC", "news_analyst") is None
    assert objection_answer_for(db, "ZZZC", "tech_analyst", run_id="run-1") is None
    assert objection_answer_for(db, "ZZZC", "tech_analyst", run_id="run-2")["session"] == "midday"
