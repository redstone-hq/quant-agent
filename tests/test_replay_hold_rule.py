"""The keep-rule replay script, on a fixture database built with the desk's own
schema (desk archetype, owner ruling 19 Oct 2026)."""

import json
import sqlite3

import pytest

from scripts.replay_hold_rule import main, replay
from src.storage.db import Database


@pytest.fixture
def desk_db(tmp_path):
    path = tmp_path / "desk.db"
    db = Database(str(path))
    db.initialize()
    conn = sqlite3.connect(path)
    for sym, qty in (("CHART", 10), ("NEWS", 10), ("ANSW", 10), ("QUIET", 10), ("SHRT", -5), ("FLAT", 0)):
        conn.execute(
            "INSERT INTO positions (symbol, qty, avg_entry, current_price, market_value, unrealized_pnl)"
            " VALUES (?, ?, 1, 1, 1, 0)",
            (sym, qty),
        )

    def stance(sym, seat, value, ts="2026-10-19 14:00:00"):
        conn.execute(
            "INSERT INTO specialist_evidence (run_id, agent_name, kind, scope, symbol, evidence_json, timestamp)"
            " VALUES ('r1', ?, 'seat_stance', 'symbol', ?, ?, ?)",
            (seat, sym, json.dumps({"seat": seat, "symbol": sym, "stance": value}), ts),
        )

    stance("CHART", "technical", "sell")
    stance("CHART", "news", "bullish")
    stance("NEWS", "technical", "buy")
    stance("NEWS", "news", "bearish")
    stance("NEWS", "news", "bearish", ts="2026-10-01 14:00:00")  # before the clean record: ignored
    stance("ANSW", "technical", "buy")
    stance("ANSW", "earnings", "bearish")
    stance("QUIET", "technical", "buy")
    stance("QUIET", "macro", "bearish")
    stance("SHRT", "technical", "sell")
    stance("SHRT", "smart_money", "bearish")
    stance("SHRT", "technical", "buy", ts="2026-10-11 14:00:00")  # older than the clean record
    answer = {"answers": [{"seat": "earnings", "objection_direction": "bearish", "decision": "overrule"}]}
    conn.execute(
        "INSERT INTO specialist_evidence (run_id, agent_name, kind, scope, symbol, evidence_json)"
        " VALUES ('r1', 'portfolio_manager', 'objection_answer', 'symbol', 'ANSW', ?)",
        (json.dumps(answer),),
    )
    conn.commit()
    conn.close()
    del db
    return str(path)


def test_replay_prints_old_and_new_verdicts(desk_db):
    rows = {sym: (old, new, why) for sym, old, new, why in replay(desk_db)}
    assert set(rows) == {"CHART", "NEWS", "ANSW", "QUIET", "SHRT"}  # flat names are not holdings
    assert rows["CHART"][:2] == ("SELL", "SELL")
    assert rows["NEWS"][:2] == ("SELL", "REVIEW") and "unanswered objection: news" in rows["NEWS"][2]
    assert rows["ANSW"][:2] == ("SELL", "KEEP") and "overruled on record" in rows["ANSW"][2]
    assert rows["QUIET"][:2] == ("SELL", "KEEP")  # macro against + no catalyst: retired floor sold it
    assert rows["SHRT"][:2] == ("KEEP", "KEEP")  # short mirrors long


def test_replay_never_writes(desk_db, capsys):
    from pathlib import Path

    from scripts.replay_hold_rule import open_read_only

    before = Path(desk_db).read_bytes()
    assert main([desk_db]) == 0
    assert "NEWS   old=SELL   new=REVIEW" in capsys.readouterr().out
    assert Path(desk_db).read_bytes() == before
    with pytest.raises(sqlite3.OperationalError):
        open_read_only(desk_db).execute("DELETE FROM positions")
