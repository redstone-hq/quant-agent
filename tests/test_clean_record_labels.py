"""The CLOSE REVIEW scorecard counts only clean-record trades, says so, names the rest, and uses dollars.

Owner ruling 13 Oct: label the clean record, name any trade closed today that is not counted,
and show results in dollars (the owner does not use R multiples).
"""

import re
from datetime import UTC, datetime
from functools import partial

from src import trader_feed
from src.storage.analytics import trade_review
from src.storage.analytics.calibration import CLEAN_RECORD_START_UTC
from src.storage.db import Database
from src.trader_feed import trade_block

NOW = datetime(2026, 10, 14, 18, 0, tzinfo=UTC)
SINCE = f"{int(CLEAN_RECORD_START_UTC[8:10])} Oct"  # "12 Oct", read from the one constant
R_MULTIPLE = re.compile(r"\d\s?R\b")


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


def _message(tmp_path, monkeypatch, trips):
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


CLEAN = ("A", "YYY", 10, 50.0, 51.234, 49.0, 12.34, "2026-10-13 14:00:00", "2026-10-14 16:00:00")
OLD = ("B", "ZZZ", 10, 20.0, 10.1, 15.0, -99.0, "2026-09-17 14:00:00", "2026-10-14 15:00:00")


def test_scorecard_counts_the_clean_record_names_the_rest_and_uses_dollars(tmp_path, monkeypatch):
    msg = _message(tmp_path, monkeypatch, [CLEAN, OLD])
    assert f"Closed today (clean record, opened since {SINCE}): 1 trade" in msg
    assert f"Not counted (opened before {SINCE}): ZZZ" in msg
    assert "$12.34" in msg
    assert "total +$12.34" in msg
    assert "99.00" not in msg  # the excluded trade's dollars are in no total, best or worst
    assert not R_MULTIPLE.search(msg)


def test_a_missing_dollar_figure_is_unknown_never_computed(tmp_path, monkeypatch):
    unrecorded = ("C", "YYY", 10, 50.0, 51.234, 49.0, None, "2026-10-13 14:00:00", "2026-10-14 16:00:00")
    msg = _message(tmp_path, monkeypatch, [unrecorded])
    assert "total unknown · average unknown" in msg
    assert "12.34" not in msg  # price difference x quantity is not a recorded result
    assert not R_MULTIPLE.search(msg)


def test_only_old_trades_closed_today_still_get_named(tmp_path, monkeypatch):
    msg = _message(tmp_path, monkeypatch, [OLD])
    assert "No closed trades today." not in msg
    assert f"Closed today (clean record, opened since {SINCE}): 0 trades" in msg
    assert f"Not counted (opened before {SINCE}): ZZZ" in msg


def test_nothing_closed_today_stays_one_line(tmp_path, monkeypatch):
    msg = _message(tmp_path, monkeypatch, [])
    assert "No closed trades today." in msg
    assert "Not counted" not in msg
