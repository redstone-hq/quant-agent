"""The CLOSE REVIEW Telegram message carries a short trade-review block (display only)."""

from datetime import UTC, datetime
from functools import partial

from src import trader_feed
from src.storage.analytics import trade_review
from src.storage.db import Database
from src.trader_feed import trade_block

NOW = datetime(2026, 10, 14, 18, 0, tzinfo=UTC)


def _trip(db, pid, sym, qty, entry, exit_, stop, pnl, t_open, t_close):
    for action, price, ts, stop_loss, realized in (
        ("BUY", entry, t_open, stop, None),
        ("SELL", exit_, t_close, None, pnl),
    ):
        db.conn.execute(
            "INSERT INTO trades (symbol, action, qty, price, fill_qty, fill_price, fill_status, stop_loss, "
            "initial_stop_loss, timestamp, position_id, realized_pnl) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (sym, action, qty, price, qty, price, "filled", stop_loss, stop_loss, ts, pid, realized),
        )


def _close_message(tmp_path, monkeypatch, trips):
    path = tmp_path / "close.db"
    db = Database(str(path))
    db.initialize()
    for trip in trips:
        _trip(db, *trip)
    db.conn.commit()
    monkeypatch.setattr(trader_feed, "_DB_PATH", path)
    monkeypatch.setattr(trade_block, "close_review_summary", partial(trade_review.close_review_summary, now=NOW))
    result = {"status": "reviewed", "run_id": "close-x", "positions": 0, "orders": []}
    return trader_feed.format_session_result("close", result, 1.0)


def test_winners_and_losers_day(tmp_path, monkeypatch):
    msg = _close_message(
        tmp_path,
        monkeypatch,
        [
            ("A", "AAA", 100, 50, 52, 49, 200.0, "2026-10-14 14:00:00", "2026-10-14 16:00:00"),  # +2R
            ("B", "BBB", 100, 20, 19.5, 19, -50.0, "2026-10-14 14:00:00", "2026-10-14 15:00:00"),  # -0.5R
        ],
    )
    assert "CLOSE REVIEW" in msg
    assert "Closed today: 2 trades, 1 wins, 1 losses" in msg
    assert "Win rate 50.0% · total +1.50R · average +0.75R" in msg
    assert "Best AAA +2.00R · worst BBB -0.50R" in msg
    assert "Profit factor 4.0" in msg
    assert "Luck check: 2 trades with known R, p-value" in msg
    assert "unknown" not in msg.split("Closed today")[1].split("Profit factor")[0]


def test_no_trades_day(tmp_path, monkeypatch):
    msg = _close_message(tmp_path, monkeypatch, [])
    assert "No closed trades today." in msg
    assert "Closed today" not in msg


def test_unknown_r_trade_shows_unknown_not_zero(tmp_path, monkeypatch):
    msg = _close_message(
        tmp_path,
        monkeypatch,
        [
            ("A", "AAA", 100, 50, 52, 49, 200.0, "2026-10-14 14:00:00", "2026-10-14 16:00:00"),
            ("D", "DDD", 10, 10, 11, None, 10.0, "2026-10-14 14:00:00", "2026-10-14 17:00:00"),
        ],
    )
    assert "Closed today: 2 trades, 2 wins, 0 losses" in msg
    assert "total unknown · average unknown" in msg
    assert "Best AAA +2.00R" in msg
    assert "p-value unknown" in msg


def test_unreadable_history_is_unknown_not_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(trader_feed, "_DB_PATH", tmp_path / "missing.db")
    lines: list[str] = []
    trade_block.append_trade_block(lines)
    assert lines == ["Trade review: unknown (trade history could not be read)."]
