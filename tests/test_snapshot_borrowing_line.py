"""Evening snapshot summary must not say "100% deployed" when borrowing."""

import sqlite3

from src.notifier import snapshots


def _snapshot(tmp_path, monkeypatch, market_value, total_value):
    db = tmp_path / "t.db"
    conn = sqlite3.connect(str(db))
    conn.execute(
        "CREATE TABLE positions (symbol TEXT, qty REAL, avg_entry REAL, current_price REAL, "
        "market_value REAL, unrealized_pnl REAL)"
    )
    conn.execute("INSERT INTO positions VALUES ('AAA', 10, 100, 100, ?, 0)", (market_value,))
    conn.commit()
    conn.close()
    monkeypatch.setattr(snapshots, "_DB_PATH", db)
    lines: list[str] = []
    snapshots._append_position_snapshot(lines, total_value)
    return next(ln for ln in lines if "Positions:" in ln)


def test_borrowing_case(tmp_path, monkeypatch):
    line = _snapshot(tmp_path, monkeypatch, 1120.0, 1000.0)
    assert "112% of the account invested (using $120 of borrowing), 0% cash" in line
    assert "100% deployed" not in line


def test_non_borrowing_case_unchanged(tmp_path, monkeypatch):
    line = _snapshot(tmp_path, monkeypatch, 700.0, 1000.0)
    assert "(70% deployed / 30% cash)" in line
    assert "borrowing" not in line
