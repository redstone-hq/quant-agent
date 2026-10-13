"""A captured session's durable decisions must survive exact offline replay."""

import sqlite3

import pytest

from ops.rehearsal.durable_outcomes import (
    DurableOutcomeMismatch,
    compare_durable_outcomes,
)
from src.storage.db import Database
from src.trading_calendar import sql_utc_stamp


CAPTURE = "run-captured"
REPLAY = "rehearsal-morning-20261006"


def _backup(source, target):
    with sqlite3.connect(source) as original, sqlite3.connect(target) as copy:
        original.backup(copy)


def _fixture(tmp_path):
    before, captured, replayed = (tmp_path / name for name in ("before.db", "captured.db", "replayed.db"))
    db = Database(str(before))
    db.initialize()
    db.close()
    with sqlite3.connect(before) as conn:
        conn.execute(
            "INSERT INTO trades (symbol,action,qty,price,reasoning,run_id,stop_loss) "
            "VALUES ('ZZZT','BUY',1,100,'previous entry','run-prior',90)"
        )
        conn.execute(
            "INSERT INTO positions (symbol,qty,avg_entry,current_price,market_value,unrealized_pnl) "
            "VALUES ('ZZZT',1,100,100,100,0)"
        )
    _backup(before, captured)
    _backup(before, replayed)
    _write_run(captured, CAPTURE, "run-captured-dec-a1b2c3", "broker-real-1", "pos-real")
    _write_run(replayed, REPLAY, "rehearsal-dec-f4e5d6", "<QAMC:order_id:0001>", "pos-replay")
    return before, captured, replayed


def _write_run(path, run_id, decision_id, broker_id, position_id):
    with sqlite3.connect(path) as conn:
        for agent, kind, scope, symbol, payload in (
            (
                "portfolio_manager",
                "reasoning",
                "run",
                None,
                '{"portfolio_view":"hold cash","reasoning_chain":{"why":"measured"}}',
            ),
            ("portfolio_manager", "proposed_order", "symbol", "ZZZT", '{"action":"BUY","qty":1,"reason":"measured"}'),
            ("risk_manager", "verdict", "run", None, '{"approved":true,"reasoning":"within risk"}'),
            (
                "pipeline",
                "stop_shift_legs",
                "symbol",
                "ZZZT",
                '{"code":"stop_shift_amended","legs":[{"old_id":"old-stop","new_id":"new-stop","outcome":"amended"}]}',
            ),
            (
                "pipeline",
                "exit_refusal",
                "symbol",
                "ZZZT",
                '{"action":"SELL","code":"ai_risk_approved","dropped":false,'
                '"detail":"named trigger","layer":"ai_risk"}',
            ),
        ):
            conn.execute(
                "INSERT INTO specialist_evidence "
                "(run_id,decision_id,agent_name,kind,scope,symbol,evidence_json) "
                "VALUES (?,?,?,?,?,?,?)",
                (run_id, decision_id if agent != "pipeline" else None, agent, kind, scope, symbol, payload),
            )
        conn.execute(
            "INSERT INTO trade_refusals "
            "(timestamp,run_id,symbol,direction,refusal,stage,reward_risk,threshold) "
            "VALUES ('2026-10-06',?,'ABCD','BUY','losing_geometry','constructor',0.8,1)",
            (run_id,),
        )
        conn.execute(
            "INSERT INTO trades "
            "(symbol,action,qty,price,reasoning,run_id,broker_order_id,fill_status,decision_id,position_id) "
            "VALUES ('ZZZT','BUY',1,101,'measured',?,?,'submitted',?,?)",
            (run_id, broker_id, decision_id, position_id),
        )
        conn.execute(
            "INSERT INTO trades "
            "(symbol,action,qty,price,reasoning,run_id,broker_order_id,fill_status,decision_id,position_id) "
            "VALUES ('ZZZT','SELL',1,102,'named trigger',?,?,'filled',?,?)",
            (run_id, broker_id, decision_id, position_id),
        )
        conn.execute("UPDATE trades SET stop_loss=92 WHERE run_id='run-prior'")
        conn.execute("UPDATE positions SET current_price=101,market_value=101,unrealized_pnl=1 WHERE symbol='ZZZT'")
        conn.execute(
            "INSERT INTO order_attempts "
            "(recorded_at,symbol,side,qty,outcome,broker_order_id,run_id,reason,limit_price) "
            "VALUES (?,'ZZZT','BUY',1,'submitted',?,?,'broker accepted',101)",
            (sql_utc_stamp(), broker_id, run_id),
        )
        conn.execute(
            "INSERT INTO order_attempts (recorded_at,outcome,broker_order_id,reason) "
            "VALUES (?,'cancelled',?,'cancel_order_by_id')",
            (sql_utc_stamp(), broker_id),
        )
        conn.execute(
            "INSERT INTO reconciliation_runs (ran_at,kind,agreed,detail) VALUES (?,'stop_coverage',1,'')",
            (sql_utc_stamp(),),
        )


def _compare(paths):
    before, captured, replayed = paths
    compare_durable_outcomes(before, captured, replayed, captured_run=CAPTURE, replay_run=REPLAY)


def test_exact_durable_outcomes_allow_only_consistent_generated_ids(tmp_path):
    _compare(_fixture(tmp_path))


def test_parallel_evidence_insert_order_does_not_change_verdict(tmp_path):
    paths = _fixture(tmp_path)
    with sqlite3.connect(paths[2]) as conn:
        count = conn.execute("SELECT COUNT(*) FROM specialist_evidence").fetchone()[0]
        conn.execute("UPDATE specialist_evidence SET id=-id")
        conn.execute("UPDATE specialist_evidence SET id=?+id", (count + 1,))
    _compare(paths)


@pytest.mark.parametrize(
    "target,where",
    [
        ("specialist_evidence", "kind='reasoning'"),
        ("trade_refusals", "refusal='losing_geometry'"),
        ("specialist_evidence", "kind='stop_shift_legs'"),
    ],
)
def test_missing_pm_refusal_or_protection_record_is_red(tmp_path, target, where):
    paths = _fixture(tmp_path)
    with sqlite3.connect(paths[2]) as conn:
        conn.execute(f"DELETE FROM {target} WHERE {where}")
    with pytest.raises(DurableOutcomeMismatch):
        _compare(paths)


def test_changed_existing_trade_or_null_reason_is_red(tmp_path):
    paths = _fixture(tmp_path)
    with sqlite3.connect(paths[2]) as conn:
        conn.execute("UPDATE trades SET stop_loss=91 WHERE run_id='run-prior'")
    with pytest.raises(DurableOutcomeMismatch, match="trades changed semantic rows"):
        _compare(paths)

    with sqlite3.connect(paths[2]) as conn:
        conn.execute("UPDATE trades SET stop_loss=92 WHERE run_id='run-prior'")
        conn.execute("UPDATE order_attempts SET reason=NULL WHERE reason='broker accepted'")
    with pytest.raises(DurableOutcomeMismatch, match="order_attempts inserted semantic rows"):
        _compare(paths)


def test_decision_or_broker_link_cannot_split(tmp_path):
    paths = _fixture(tmp_path)
    with sqlite3.connect(paths[2]) as conn:
        conn.execute("UPDATE trades SET decision_id='different-decision' WHERE run_id=?", (REPLAY,))
    with pytest.raises(DurableOutcomeMismatch, match="one-to-one"):
        _compare(paths)
    with sqlite3.connect(paths[2]) as conn:
        conn.execute("UPDATE trades SET decision_id='rehearsal-dec-f4e5d6' WHERE run_id=?", (REPLAY,))
        conn.execute("UPDATE order_attempts SET broker_order_id='different-broker' WHERE run_id=?", (REPLAY,))
    with pytest.raises(DurableOutcomeMismatch, match="one-to-one"):
        _compare(paths)


def test_exit_reason_and_cancel_attempt_are_not_summarized_away(tmp_path):
    paths = _fixture(tmp_path)
    with sqlite3.connect(paths[2]) as conn:
        conn.execute("UPDATE trades SET reasoning='different exit' WHERE run_id=? AND action='SELL'", (REPLAY,))
    with pytest.raises(DurableOutcomeMismatch, match="trades inserted semantic rows"):
        _compare(paths)
    with sqlite3.connect(paths[2]) as conn:
        conn.execute("UPDATE trades SET reasoning='named trigger' WHERE run_id=? AND action='SELL'", (REPLAY,))
        conn.execute("DELETE FROM order_attempts WHERE outcome='cancelled'")
    with pytest.raises(DurableOutcomeMismatch, match="order_attempts"):
        _compare(paths)
