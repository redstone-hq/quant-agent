"""The conviction ledger store is STANDALONE.

Constructed from plain collaborators — an in-memory sqlite connection this
test opened and a lock this test made — with `src.storage.db` imported
nowhere in this file. If the store ever reaches back into `Database`, this
file stops importing.
"""

import json
import sqlite3
import sys
import threading

from src.storage.conviction_ledger_store import (
    CONVICTION_CREDIT_KIND,
    ConvictionLedgerStore,
    build_conviction_ledger_store,
)
from src.storage.seat_stances import SEAT_STANCE_KIND, read_seat_stances

SCHEMA = """
CREATE TABLE specialist_evidence (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    decision_id TEXT,
    agent_name TEXT NOT NULL,
    kind TEXT NOT NULL,
    scope TEXT NOT NULL,
    symbol TEXT,
    evidence_json TEXT NOT NULL,
    timestamp TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    position_id TEXT, symbol TEXT, action TEXT, qty REAL, price REAL,
    fill_qty REAL, fill_price REAL, fill_status TEXT, stop_loss REAL,
    decision_id TEXT, run_id TEXT, timestamp TEXT
);
"""


def _conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def _seed_round_trip(conn, *, position_id="p1", exit_price=110.0):
    conn.executescript("")
    rows = [
        (position_id, "AAPL", "BUY", 10, 100.0, 10, 100.0, "filled", 90.0, "dec-1", "run-1", "2026-09-01T14:00:00"),
        (
            position_id,
            "AAPL",
            "SELL",
            10,
            exit_price,
            10,
            exit_price,
            "filled",
            0.0,
            "dec-1",
            "run-1",
            "2026-09-05T14:00:00",
        ),
    ]
    conn.executemany(
        "INSERT INTO trades (position_id, symbol, action, qty, price, fill_qty,"
        " fill_price, fill_status, stop_loss, decision_id, run_id, timestamp)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        rows,
    )
    conn.execute(
        "INSERT INTO specialist_evidence (run_id, decision_id, agent_name, kind,"
        " scope, symbol, evidence_json) VALUES (?,?,?,?,?,?,?)",
        (
            "run-1",
            "dec-1",
            "macro",
            SEAT_STANCE_KIND,
            "symbol",
            "AAPL",
            json.dumps(
                {
                    "seat": "macro",
                    "symbol": "AAPL",
                    "stance": "buy",
                    "conviction": "high",
                    "nominated": True,
                    "observation": "o",
                }
            ),
        ),
    )
    conn.commit()


def test_the_builder_hands_back_the_collaborators_given():
    conn, lock = _conn(), threading.Lock()
    store = build_conviction_ledger_store(conn=conn, lock=lock)
    assert isinstance(store, ConvictionLedgerStore)
    assert store.conn is conn
    assert store._lock is lock


def test_the_store_does_not_import_the_database_module():
    """The boundary is real: neither module pulls `src.storage.db` in."""
    for name in ("src.storage.conviction_ledger_store", "src.storage.seat_stances", "src.storage.locked_write"):
        src = open(sys.modules[name].__file__).read()
        for line in src.splitlines():
            stripped = line.strip()
            if stripped.startswith(("import ", "from ")):
                assert "storage.db" not in stripped, (name, line)
                assert "Database" not in stripped, (name, line)


def test_seat_stances_read_back_from_a_plain_connection():
    conn, lock = _conn(), threading.Lock()
    _seed_round_trip(conn)
    stances = read_seat_stances(conn, lock, decision_id="dec-1", symbol="AAPL")
    assert [s.seat for s in stances] == ["macro"]
    assert read_seat_stances(conn, lock, decision_id="") == []


def test_a_closed_position_scores_one_credit_row_per_seat():
    conn = _conn()
    _seed_round_trip(conn)
    store = build_conviction_ledger_store(conn=conn, lock=threading.Lock())
    counters = store.resolve_conviction_ledger()
    assert counters["closed_positions"] == 1
    assert counters["scored_positions"] == 1
    assert counters["credits_written"] == 1
    credits = store.get_conviction_credits()
    assert len(credits) == 1
    assert credits[0].seat == "macro"
    assert credits[0].symbol == "AAPL"
    # +10 on a 10-wide stop is +1R, credited positively to a supporter.
    assert credits[0].r_multiple == 1.0
    assert credits[0].credit == 1.0
    stored = conn.execute("SELECT kind FROM specialist_evidence WHERE kind = ?", (CONVICTION_CREDIT_KIND,)).fetchall()
    assert len(stored) == 1


def test_scoring_is_idempotent_by_position_id():
    conn = _conn()
    _seed_round_trip(conn)
    store = build_conviction_ledger_store(conn=conn, lock=threading.Lock())
    store.resolve_conviction_ledger()
    again = store.resolve_conviction_ledger()
    assert again["skipped_already_scored"] == 1
    assert again["credits_written"] == 0
    assert len(store.get_conviction_credits()) == 1
    assert store._scored_position_ids() == {"p1"}


def test_a_chain_with_no_seat_stances_is_counted_never_fabricated():
    conn = _conn()
    _seed_round_trip(conn)
    conn.execute("DELETE FROM specialist_evidence WHERE kind = ?", (SEAT_STANCE_KIND,))
    conn.commit()
    store = build_conviction_ledger_store(conn=conn, lock=threading.Lock())
    counters = store.resolve_conviction_ledger()
    assert counters["skipped_no_stances"] == 1
    assert counters["credits_written"] == 0
    assert store.get_conviction_credits() == []


def test_a_chain_with_no_entry_stop_has_no_honest_r_and_is_skipped():
    conn = _conn()
    _seed_round_trip(conn)
    conn.execute("UPDATE trades SET stop_loss = 0 WHERE action = 'BUY'")
    conn.commit()
    store = build_conviction_ledger_store(conn=conn, lock=threading.Lock())
    counters = store.resolve_conviction_ledger()
    assert counters["skipped_no_r"] == 1
    assert counters["credits_written"] == 0
