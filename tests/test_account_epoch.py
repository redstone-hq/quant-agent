"""Account epoch: after a broker account reset the desk keeps history but reads only the new account's rows.

Owner ruling 9 Oct 2026: history is excluded by date, never deleted. These pin
the readers that decide what the desk holds and how far it is in drawdown.
"""

from __future__ import annotations

import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

import src.trading_calendar as trading_calendar
from src.risk.book_exposure import peak_to_trough_pct
from src.storage.db import Database
from src.trading_calendar import UTC, sql_utc_stamp_ago

ROOT = Path(__file__).resolve().parent.parent
OLD, NEW = "old-acct-uuid", "new-acct-uuid"


@pytest.fixture
def db(tmp_path, monkeypatch):
    alerts: list[str] = []
    import src.storage.account_epoch as ae

    monkeypatch.setattr(ae, "_default_alert", alerts.append)
    d = Database(str(tmp_path / "desk.db"))
    d.initialize()
    d.alerts = alerts
    return d


def _old_buy(db, symbol="AAPL", qty=10.0, *, days_ago=2.0, position_id="pos-oldaccount1"):
    db.conn.execute(
        "INSERT INTO trades (symbol, action, qty, price, fill_status, fill_qty, fill_price, position_id, timestamp) "
        "VALUES (?, 'BUY', ?, 100.0, 'filled', ?, 100.0, ?, ?)",
        (symbol, qty, qty, position_id, sql_utc_stamp_ago(days=days_ago)),
    )
    db.conn.commit()


def _pnl(db, day, total, stamp):
    db.conn.execute(
        "INSERT INTO daily_pnl (date, total_value, daily_pnl, daily_return_pct, timestamp) VALUES (?, ?, 0, 0, ?)",
        (day, total, stamp),
    )
    db.conn.commit()


def _bind_old(db):
    db.observe_broker_account({"account_id": OLD, "account_created_at": "2025-01-02 00:00:00"}, [object()])


def _switch(db, *, created_at=None, positions=()):
    created = created_at or sql_utc_stamp_ago(minutes=1)
    return db.observe_broker_account({"account_id": NEW, "account_created_at": created}, list(positions))


def test_first_run_records_a_no_cut_epoch_so_nothing_changes_today(db):
    _old_buy(db)
    row = db.conn.execute("SELECT account_id, epoch_utc, daily_pnl_from FROM account_epoch").fetchall()
    assert [tuple(r) for r in row] == [(None, None, None)]
    assert db.get_symbols_with_open_ledger_qty() == {"AAPL": 10.0}
    _bind_old(db)
    _bind_old(db)  # same id again: a no-op, not a change
    rows = db.conn.execute("SELECT account_id, epoch_utc FROM account_epoch").fetchall()
    assert [tuple(r) for r in rows] == [(OLD, None)]
    assert db.get_symbols_with_open_ledger_qty() == {"AAPL": 10.0}


# (a)
def test_after_an_account_change_the_held_shares_belief_is_empty(db):
    _old_buy(db)
    _bind_old(db)
    _switch(db)
    assert db.conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0] == 1  # kept, not deleted
    assert db.get_symbols_with_open_ledger_qty() == {}


# (b)
def test_a_new_account_buy_of_an_old_name_opens_a_new_position(db):
    _old_buy(db)
    _bind_old(db)
    _switch(db)
    new_id = db.insert_trade("AAPL", "BUY", 5, 101.0, "new account entry", "run-new", stop_loss=95.0)
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
    _bind_old(db)
    # Reset at 23:00 ET on 14 Oct (03:00 UTC on 15 Oct): the same ET date as the old final row.
    monkeypatch.setattr(trading_calendar, "utc_now", lambda: datetime(2026, 10, 15, 3, 0, tzinfo=UTC))
    epoch = _switch(db, created_at="2026-10-15T02:50:00+00:00")
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
@pytest.mark.parametrize(
    "case",
    [
        {"positions": [SimpleNamespace(symbol="MSFT")], "why": "book is not empty"},
        {"created_at": "2020-01-01 00:00:00", "why": "not later than the newest history"},
    ],
)
def test_an_unsafe_account_change_is_refused_loudly(db, case):
    from src.storage.account_epoch import AccountEpochRefused

    _old_buy(db)
    _bind_old(db)
    with pytest.raises(AccountEpochRefused, match=case["why"]):
        _switch(db, created_at=case.get("created_at"), positions=case.get("positions", ()))
    assert len(db.alerts) == 1 and case["why"] in db.alerts[0]
    assert db.conn.execute("SELECT COUNT(*) FROM account_epoch").fetchone()[0] == 1
    assert db.get_symbols_with_open_ledger_qty() == {"AAPL": 10.0}


def test_a_change_with_old_write_ahead_rows_is_refused(db):
    from src.storage.account_epoch import AccountEpochRefused

    _bind_old(db)
    db.conn.execute("INSERT INTO pending_repegs(symbol, old_order_id, new_order_id) VALUES ('X', 'o1', 'o2')")
    db.conn.commit()
    with pytest.raises(AccountEpochRefused, match="write-ahead"):
        _switch(db)


# (f)
def _check_module():
    sys.path.insert(0, str(ROOT / "scripts"))
    try:
        import check_account_epoch_reads
    finally:
        sys.path.pop(0)
    return check_account_epoch_reads


def test_the_check_fails_on_a_new_raw_read_and_passes_once_allowlisted(tmp_path):
    check = _check_module()
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "sizer.py").write_text(
        'def held(conn):\n    """Reads FROM trades in prose: not SQL."""\n'
        '    conn.execute("DELETE FROM trades WHERE 0")\n'
        '    return conn.execute("SELECT symbol, qty FROM trades").fetchall()\n'
    )
    allow = tmp_path / "allow.txt"
    allow.write_text("# nothing yet\n")
    assert check.main(["--root", str(tmp_path), "--allowlist", str(allow)]) == 1
    allow.write_text("src/sizer.py::held  # reason\n")
    assert check.main(["--root", str(tmp_path), "--allowlist", str(allow)]) == 0
    allow.write_text("src/sizer.py::held\nsrc/gone.py::f\n")
    assert check.main(["--root", str(tmp_path), "--allowlist", str(allow)]) == 1  # stale entry


def test_the_repo_passes_its_own_check():
    assert _check_module().main(["--root", str(ROOT)]) == 0
