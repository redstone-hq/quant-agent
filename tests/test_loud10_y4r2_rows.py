"""Five swallowed DB/broker read failures now log a traceback and leave a counted row."""

from unittest.mock import MagicMock

from src.prompt_facts.decisions import PromptDecisions
from src.sessions.evening_stop_proximity_session import EveningStopProximitySession
from src.sessions.expected_sessions_session import ExpectedSessionsMissingSession
from src.storage.db import Database


def _db(tmp_path):
    db = Database(str(tmp_path / "t.db"))
    db.initialize()
    return db


def _rows(db):
    return [r[0] for r in db.conn.execute("SELECT kind FROM reconciliation_runs WHERE agreed = 0").fetchall()]


def test_prompt_decisions_failures_are_recorded(tmp_path):
    db = _db(tmp_path)
    real = db.get_recent_agent_outputs
    db.get_recent_agent_outputs = MagicMock(side_effect=RuntimeError("boom"))
    pd = PromptDecisions(db=db, parse_logged_agent_response=lambda r: r)
    assert pd._build_pm_recent_decisions() == ""
    assert pd._build_own_recent_decisions() == ""
    db.get_recent_agent_outputs = real
    assert sorted(_rows(db)) == [
        "guarded:prompt_facts.own_recent_decisions",
        "guarded:prompt_facts.pm_recent_decisions",
    ]


def test_missing_session_check_failure_is_recorded(tmp_path, caplog):
    db = _db(tmp_path)
    db.session_prefixes_logged_on = MagicMock(side_effect=RuntimeError("boom"))
    step = ExpectedSessionsMissingSession(db=db)
    assert step.run() == []
    assert _rows(db) == ["guarded:sessions.missing_session_check"]
    assert any(r.exc_info for r in caplog.records)


def test_stop_proximity_failure_is_recorded(tmp_path):
    db = _db(tmp_path)
    step = EveningStopProximitySession(
        atr_for_symbol=MagicMock(side_effect=RuntimeError("boom")),
        sweep_symbol=lambda: "",
        broker=MagicMock(),
        stop_reader=MagicMock(),
        db=db,
    )
    pos = MagicMock()
    assert step.run([pos]) == []
    assert _rows(db) == ["guarded:sessions.evening_stop_proximity"]
