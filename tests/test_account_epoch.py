"""Account epoch: after a paper account reset the desk keeps history but reads only the rows since the reset.

Owner ruling 9 Oct 2026: history is excluded by date, never deleted. These pin
the readers that decide what the desk holds and how far it is in drawdown.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime

import pytest

import src.trading_calendar as trading_calendar
from src.clean_record_baseline import clean_record_baseline
from src.risk.book_exposure import peak_to_trough_pct
from src.storage.account_epoch import first_session_close_after, record_reset
from src.storage.analytics.calibration import CLEAN_RECORD_DATE, pnl_start
from src.storage.db import Database
from src.trading_calendar import UTC, sql_utc_stamp_ago


@pytest.fixture
def db(tmp_path):
    d = Database(str(tmp_path / "desk.db"))
    d.initialize()
    return d


def _old_buy(db, symbol="AAPL", qty=10.0, *, days_ago=2.0, position_id="pos-oldaccount1"):
    db.conn.execute(
        "INSERT INTO trades (symbol, action, qty, price, fill_status, fill_qty, fill_price, position_id, timestamp) "
        "VALUES (?, 'BUY', ?, 100.0, 'filled', ?, 100.0, ?, ?)",
        (symbol, qty, qty, position_id, sql_utc_stamp_ago(days=days_ago)),
    )
    db.conn.commit()


def _pnl(db, day, total, stamp, change=0.0):
    db.conn.execute(
        "INSERT INTO daily_pnl (date, total_value, daily_pnl, daily_return_pct, timestamp) VALUES (?, ?, ?, 0, ?)",
        (day, total, change, stamp),
    )
    db.conn.commit()


def _reset(db, now=None):
    epoch = record_reset(db.conn, now=now)
    db.conn.commit()
    return epoch


def test_first_run_records_a_no_cut_epoch_so_nothing_changes_today(db):
    _old_buy(db)
    rows = db.conn.execute("SELECT epoch_utc, daily_pnl_from FROM account_epoch").fetchall()
    assert [tuple(r) for r in rows] == [(None, None)]
    assert db.get_symbols_with_open_ledger_qty() == {"AAPL": 10.0}


# (a)
def test_after_a_reset_the_held_shares_belief_is_empty(db):
    _old_buy(db)
    _reset(db)
    assert db.conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0] == 1  # kept, not deleted
    assert db.get_symbols_with_open_ledger_qty() == {}


# (b)
def test_a_new_buy_of_an_old_name_opens_a_new_position(db):
    _old_buy(db)
    _reset(db)
    new_id = db.insert_trade("AAPL", "BUY", 5, 101.0, "post-reset entry", "run-new", stop_loss=95.0)
    rows = dict(db.conn.execute("SELECT id, position_id FROM trades").fetchall())
    old_id = next(i for i in rows if i != new_id)
    assert rows[old_id] == "pos-oldaccount1"
    assert rows[new_id] and rows[new_id] != "pos-oldaccount1"
    assert db.get_symbols_with_open_ledger_qty() == {"AAPL": 5.0}


# (c)
def test_the_ladder_drawdown_ignores_the_old_peak_even_for_a_late_evening_reset(db, monkeypatch):
    # Old account: peak 100,500; its final row (14 Oct) is written at 21:00 ET = 01:00 UTC on 15 Oct.
    _pnl(db, "2026-10-12", 100000.0, "2026-10-13 00:30:00")
    _pnl(db, "2026-10-13", 100500.0, "2026-10-14 00:30:00")
    _pnl(db, "2026-10-14", 99000.0, "2026-10-15 01:00:00")
    # Reset at 23:00 ET on 14 Oct (03:00 UTC on 15 Oct): the same ET date as the old final row.
    monkeypatch.setattr(trading_calendar, "utc_now", lambda: datetime(2026, 10, 15, 3, 0, tzinfo=UTC))
    epoch = _reset(db)
    assert epoch["epoch_utc"] == "2026-10-15 03:00:00"
    assert epoch["daily_pnl_from"] == "2026-10-15"
    _pnl(db, "2026-10-15", 10050.0, "2026-10-16 00:30:00")
    rows = db.get_daily_pnl(limit=252)  # the ladder's exact read (src/delever/ladder.py)
    assert [r["date"] for r in rows] == ["2026-10-15"]
    assert peak_to_trough_pct([r["total_value"] for r in rows], 10050.0) == 0.0


# (d)
def test_a_reader_with_no_epoch_row_raises(db):
    _old_buy(db)
    _pnl(db, "2026-10-13", 100500.0, "2026-10-14 00:30:00")
    with pytest.raises(sqlite3.DatabaseError):
        db.conn.execute("DELETE FROM account_epoch")  # append-only: the trigger refuses
    db.conn.execute("DROP TRIGGER account_epoch_no_delete")
    db.conn.execute("DELETE FROM account_epoch")
    db.conn.commit()
    with pytest.raises(sqlite3.OperationalError):
        db.get_symbols_with_open_ledger_qty()
    with pytest.raises(sqlite3.OperationalError):
        db.get_daily_pnl(limit=252)
    # A connection that never registered the guard cannot read the views at all.
    raw = sqlite3.connect(db.db_path)
    with pytest.raises(sqlite3.OperationalError, match="no such function"):
        raw.execute("SELECT * FROM current_account_trades").fetchall()


# (e)
def test_after_a_recorded_reset_the_pnl_start_is_the_reset(db, monkeypatch):
    _pnl(db, "2026-10-13", 100500.0, "2026-10-14 00:30:00", change=500.0)
    _pnl(db, "2026-10-14", 99000.0, "2026-10-15 00:30:00", change=-1500.0)
    assert pnl_start(db.conn) == (CLEAN_RECORD_DATE, False)  # no reset yet: unchanged
    first, prev = db.get_clean_record_baseline_rows()
    assert first["date"] == "2026-10-13" and prev is None  # nothing before the clean record in this table

    _reset(db, now=datetime(2026, 10, 1, 12, 0, tzinfo=UTC))  # earlier than the clean record: no move
    assert pnl_start(db.conn) == (CLEAN_RECORD_DATE, False)

    monkeypatch.setattr(trading_calendar, "utc_now", lambda: datetime(2026, 10, 15, 3, 0, tzinfo=UTC))
    epoch = _reset(db)
    assert pnl_start(db.conn) == (first_session_close_after(epoch["epoch_utc"]), True) == ("2026-10-15", True)
    _pnl(db, "2026-10-15", 10050.0, "2026-10-16 00:30:00", change=50.0)
    first, prev = db.get_clean_record_baseline_rows()
    assert first["date"] == "2026-10-15"
    assert prev is None  # the old account's close is not this account's start
    assert clean_record_baseline(first, prev) == (10000.0, "Thu 15 Oct")
