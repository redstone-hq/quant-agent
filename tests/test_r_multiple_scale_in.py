"""Scale-in R through the real `trades` table: risk is the sum over every entry
lot of qty x (entry - that lot's OWN stop at entry), shorts mirrored; any lot
with no stop at entry makes R unknown (None), never the first lot's stop.

Rows go into a table built by `Database.initialize()` and are read back by the
same `db_reads` call the trades table uses, so the read path is exercised too.
"""

import pytest

import src.api.db_reads as db_reads
from src.storage.analytics.trade_review import closed_r_by_exit_row
from src.storage.db import Database

_COLS = (
    "symbol, action, qty, price, fill_qty, fill_price, fill_status, stop_loss, "
    "initial_stop_loss, timestamp, position_id"
)


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = tmp_path / "scale_in.db"
    database = Database(str(path))
    database.initialize()
    monkeypatch.setattr(db_reads, "get_db_path", lambda: str(path))
    yield database
    database.conn.close()


def _insert(db, pid, lots):
    """lots: (action, qty, price, stop_at_entry or None), oldest first."""
    for i, (action, qty, price, stop) in enumerate(lots):
        db.conn.execute(
            f"INSERT INTO trades ({_COLS}) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            ("XYZ", action, qty, price, qty, price, "filled", stop, stop, f"2026-10-13 14:0{i}:00", pid),
        )
    db.conn.commit()


def _r(pid):
    rows = db_reads.get_position_chain_rows([pid])
    assert rows, "chain not read back from the real table"
    return closed_r_by_exit_row(rows).get(rows[-1]["id"])


def test_two_long_buys_with_different_stops_sum_their_risk(db):
    # risk = 100 x (50-49) + 100 x (52-50) = 300; P&L = 100x5 + 100x3 = 800.
    # The old first-stop grading gave 2.0 (VWAP 51 vs stop 49 on 200 shares).
    _insert(db, "L", [("BUY", 100, 50.0, 49.0), ("BUY", 100, 52.0, 50.0), ("SELL", 200, 55.0, None)])
    assert _r("L") == round(800 / 300, 2)


def test_two_short_lots_with_different_stops_sum_their_risk(db):
    # risk = 10 x (102-100) + 10 x (100-96) = 60; P&L = 10x6 + 10x2 = 80.
    # The old first-stop grading gave 1.0 (VWAP 98 vs stop 102 on 20 shares).
    _insert(db, "S", [("SHORT", 10, 100.0, 102.0), ("SHORT", 10, 96.0, 100.0), ("COVER", 20, 94.0, None)])
    assert _r("S") == round(80 / 60, 2)


def test_one_buy_without_a_stop_makes_r_unknown(db):
    _insert(db, "N", [("BUY", 100, 50.0, 49.0), ("BUY", 100, 52.0, None), ("SELL", 200, 55.0, None)])
    assert _r("N") is None


def test_single_buy_is_unchanged(db):
    _insert(db, "ONE", [("BUY", 100, 50.0, 49.0), ("SELL", 100, 52.0, None)])
    assert _r("ONE") == 2.0
