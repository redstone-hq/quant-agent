"""Covering a short must record realised P&L (live COVER rows 104 and 106 held None).

`_realized_pnl_through_trade` walks SIGNED inventory with the average entry
price of the open side. The trades table has no side column, so direction comes
from the action name, and for either-side exits (STOP_OUT) from the sign of the
position being closed. Every number below is worked by hand in the comment.
"""

from __future__ import annotations

import itertools

import pytest

from src.storage.db import Database

_ids = itertools.count(1)


@pytest.fixture
def db(tmp_path):
    database = Database(str(tmp_path / "ledger.db"))
    database.initialize()
    yield database
    database.close()


def _fill(db, symbol, action, qty, price):
    """Insert one row and confirm its fill at `price`; return the stored row."""
    order_id = f"ord-{next(_ids)}"
    # An entry must carry the stop it was sized against (the ledger refuses one without).
    stop = {"BUY": price * 0.9, "SHORT": price * 1.1}.get(action)
    extra = {} if stop is None else {"stop_loss": stop}
    db.insert_trade(
        symbol, action, qty, price, "test", "run", broker_order_id=order_id, fill_status="submitted", **extra
    )
    db.update_trade_fill(order_id, "filled", fill_qty=qty, fill_price=price)
    return db.conn.execute("SELECT * FROM trades WHERE broker_order_id = ?", (order_id,)).fetchone()


def test_cover_of_a_short_realises_pnl(db):
    # short 10 @ 50, cover 10 @ 45: (50 - 45) * 10 = +50
    assert _fill(db, "UPS", "SHORT", 10, 50.0)["realized_pnl"] is None
    assert _fill(db, "UPS", "COVER", 10, 45.0)["realized_pnl"] == 50.0


def test_partial_covers_each_realise_against_the_same_average(db):
    # short 10 @ 50. cover 4 @ 46: (50 - 46) * 4 = +16. cover 6 @ 55: (50 - 55) * 6 = -30
    _fill(db, "FLNC", "SHORT", 10, 50.0)
    assert _fill(db, "FLNC", "PARTIAL_COVER", 4, 46.0)["realized_pnl"] == 16.0
    assert _fill(db, "FLNC", "COVER", 6, 55.0)["realized_pnl"] == -30.0


def test_short_added_to_uses_the_weighted_average_entry(db):
    # short 5 @ 50 and 5 @ 60: average (250 + 300) / 10 = 55. cover 10 @ 50: (55 - 50) * 10 = +50
    _fill(db, "AAA", "SHORT", 5, 50.0)
    _fill(db, "AAA", "SHORT", 5, 60.0)
    assert _fill(db, "AAA", "COVER", 10, 50.0)["realized_pnl"] == 50.0


def test_short_stopped_out_takes_its_direction_from_the_open_short(db):
    # short 10 @ 50, STOP_OUT 10 @ 53 (a buy-to-cover): (50 - 53) * 10 = -30
    _fill(db, "BBB", "SHORT", 10, 50.0)
    assert _fill(db, "BBB", "STOP_OUT", 10, 53.0)["realized_pnl"] == -30.0


def test_long_path_is_unchanged(db):
    # buy 10 @ 101, sell 4 @ 111: (111 - 101) * 4 = +40. stop out the other 6 @ 99: (99 - 101) * 6 = -12
    _fill(db, "CCC", "BUY", 10, 101.0)
    assert _fill(db, "CCC", "SELL", 4, 111.0)["realized_pnl"] == 40.0
    assert _fill(db, "CCC", "STOP_OUT", 6, 99.0)["realized_pnl"] == -12.0


def test_cover_larger_than_the_open_short_stays_unknown(db):
    # short 5, cover 8: three shares have no recorded entry, so the P&L is unknown, not guessed
    _fill(db, "DDD", "SHORT", 5, 50.0)
    assert _fill(db, "DDD", "COVER", 8, 45.0)["realized_pnl"] is None


def test_sell_larger_than_the_open_long_stays_unknown(db):
    _fill(db, "EEE", "BUY", 5, 100.0)
    assert _fill(db, "EEE", "SELL", 8, 110.0)["realized_pnl"] is None


def test_cover_with_nothing_short_stays_unknown(db):
    assert _fill(db, "FFF", "COVER", 3, 45.0)["realized_pnl"] is None


def test_a_sell_cannot_close_a_short(db):
    # direction comes from the action: a SELL is a long exit, so it cannot realise a short's P&L
    _fill(db, "GGG", "SHORT", 5, 50.0)
    assert _fill(db, "GGG", "SELL", 5, 45.0)["realized_pnl"] is None
