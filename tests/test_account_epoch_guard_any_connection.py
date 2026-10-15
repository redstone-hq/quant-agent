"""The account-epoch readers work on any plain sqlite3 connection (nightly scan regression, 15 Oct 2026)."""

from __future__ import annotations

import sqlite3

import pytest

from src.storage.account_epoch import AccountEpochMissing, current_account_daily_pnl
from src.storage.db import Database


def _build(tmp_path):
    path = str(tmp_path / "desk.db")
    d = Database(path)
    d.initialize()
    d.conn.execute(
        "INSERT INTO daily_pnl (date, total_value, daily_pnl, daily_return_pct, timestamp) "
        "VALUES ('2099-01-02', 1000.0, 5.0, 0, '2099-01-03 00:00:00')"
    )
    d.conn.commit()
    d.conn.close()
    return path


def test_plain_connection_reads_daily_pnl(tmp_path):
    path = _build(tmp_path)
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    rows = current_account_daily_pnl(conn, 5)
    assert [r["date"] for r in rows] == ["2099-01-02"]


def test_empty_epoch_still_raises(tmp_path):
    path = _build(tmp_path)
    conn = sqlite3.connect(path)
    conn.execute("DROP TRIGGER account_epoch_no_delete")
    conn.execute("DELETE FROM account_epoch")
    conn.commit()
    with pytest.raises((AccountEpochMissing, sqlite3.Error)):
        current_account_daily_pnl(conn, 5)
