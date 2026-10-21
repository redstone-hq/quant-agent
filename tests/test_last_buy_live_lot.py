"""`get_symbol_last_buy` / `update_open_stop_loss` anchor only on the LIVE position.

Defect (2026-10-21): both lookups took the newest executed opening row for a
symbol with no check that its position was still open. A held name with no
opening row of its own (lost fill record, broker-adopted share) was anchored
on an old CLOSED holding: wrong days held in prompts, and a live stop written
onto a closed row. Both now pick from `trade_actions.live_lot_rows` over the
reset-scoped `current_account_trades` view.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from src.storage.account_epoch import record_reset
from src.storage.db import Database
from src.util.time import UTC


@pytest.fixture
def db(tmp_path):
    database = Database(str(tmp_path / "lastbuy.db"))
    database.initialize()
    yield database
    database.close()


def _trade(db, symbol, action, qty, ts, price=100.0, fill_status="filled", stop_loss=None):
    db.insert_trade(
        symbol=symbol,
        action=action,
        qty=qty,
        price=price,
        reasoning=action.lower(),
        run_id="r",
        stop_loss=stop_loss,
        fill_status=fill_status,
        filled_at=ts if fill_status == "filled" else None,
    )
    with db._lock:
        row_id = db.conn.execute("SELECT MAX(id) FROM trades WHERE symbol = ?", (symbol,)).fetchone()[0]
        db.conn.execute("UPDATE trades SET timestamp = ? WHERE id = ?", (ts, row_id))
        db.conn.commit()
    return row_id


def _closed_holding(db, symbol="XYZ"):
    buy_id = _trade(db, symbol, "BUY", 5.0, "2026-10-13 14:00:00", stop_loss=90.0)
    _trade(db, symbol, "SELL", 5.0, "2026-10-14 15:00:00", price=110.0)
    return buy_id


def test_only_a_closed_holding_returns_none(db):
    _closed_holding(db)
    assert db.get_symbol_last_buy("XYZ") is None


def test_current_open_buy_after_a_closed_one_is_returned(db):
    _closed_holding(db)
    current = _trade(db, "XYZ", "BUY", 3.0, "2026-10-16 14:30:00", stop_loss=95.0)
    row = db.get_symbol_last_buy("XYZ")
    assert row is not None and row["id"] == current


def test_part_share_remainder_keeps_the_position_open(db):
    buy_id = _trade(db, "XYZ", "BUY", 10.5, "2026-10-13 14:00:00", stop_loss=90.0)
    _trade(db, "XYZ", "SELL", 10.0, "2026-10-14 15:00:00", price=110.0)
    row = db.get_symbol_last_buy("XYZ")
    assert row is not None and row["id"] == buy_id


def test_pre_reset_row_is_hidden_after_a_reset(db):
    _trade(db, "XYZ", "BUY", 5.0, "2026-10-13 14:00:00", stop_loss=90.0)
    assert db.get_symbol_last_buy("XYZ") is not None, "fixture sanity: open before the reset"
    record_reset(db.conn, now=datetime(2026, 10, 15, 12, 0, tzinfo=UTC))
    db.conn.commit()
    assert db.get_symbol_last_buy("XYZ") is None


def test_stop_write_with_only_a_closed_holding_writes_nothing(db):
    buy_id = _closed_holding(db)
    assert db.update_open_stop_loss("XYZ", 101.0, action="BUY") is False
    stop = db.conn.execute("SELECT stop_loss FROM trades WHERE id = ?", (buy_id,)).fetchone()[0]
    assert stop == pytest.approx(90.0), "a live stop must never land on a closed row"


def test_stop_write_lands_on_the_live_position(db):
    _closed_holding(db)
    current = _trade(db, "XYZ", "BUY", 3.0, "2026-10-16 14:30:00", stop_loss=95.0)
    assert db.update_open_stop_loss("XYZ", 97.0, action="BUY") is True
    stop = db.conn.execute("SELECT stop_loss FROM trades WHERE id = ?", (current,)).fetchone()[0]
    assert stop == pytest.approx(97.0)


def test_unknown_fill_order_returns_the_newest_current_account_open(db):
    """No broker fill times on an exit: order unknown, so the pre-fix answer stands."""
    _trade(db, "XYZ", "BUY", 5.0, "2026-10-13 14:00:00", stop_loss=90.0)
    db.insert_trade(
        symbol="XYZ", action="SELL", qty=2.0, price=110.0, reasoning="trim", run_id="r", fill_status="filled"
    )
    newest = _trade(db, "XYZ", "BUY", 3.0, "2026-10-16 14:30:00", stop_loss=95.0)
    with db._lock:
        db.conn.execute("UPDATE trades SET filled_at = NULL WHERE symbol = 'XYZ'")
        db.conn.commit()
    row = db.get_symbol_last_buy("XYZ")
    assert row is not None and row["id"] == newest
