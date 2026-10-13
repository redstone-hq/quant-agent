"""Lots are ordered by the broker's own fill time (`trades.filled_at`) - the one clock.

`trades.timestamp` is three clocks: the broker's fill time on a reconciled exit,
submission time on our own orders, placement time on a stop move. Ordering lots by it
(or by when the desk confirmed a fill) put a resting add that filled AFTER a stop-out in
front of it: two positions merged, 1R came out wrong and the ratchets fired at the wrong
price. Stored position ids are re-derived from the same walk on every fill confirmation.
Each test here fails on the pre-fix head (05c91585).
"""

import json
from datetime import date, datetime, timezone
from types import SimpleNamespace

import pytest

from src.execution.order_gates import ORDER_QTY_DECIMALS
from src.quantities import held_position_risk
from src.storage.db import Database
from src.trade_actions import FILL_ORDER_UNKNOWN, broker_fill_time, live_lot_rows
from tests.test_open_r_per_lot import _pos, _r_ratchet_fired, _trail

DAY = "2026-10-12"


@pytest.fixture
def db(tmp_path):
    database = Database(str(tmp_path / "fill_time.db"))
    database.initialize()
    yield database
    database.close()


def _row(i, action, qty, price, stop=None, *, written, filled_at, confirmed=None):
    """An executed row as the trades table holds it, with its three clocks."""
    return {
        "id": i,
        "symbol": "LATE",
        "action": action,
        "qty": qty,
        "price": price,
        "fill_price": price,
        "fill_qty": qty,
        "fill_status": "filled",
        "stop_loss": stop,
        "initial_stop_loss": stop,
        "position_id": "pos-stamped",
        "timestamp": written,
        "fill_reconciled_at": confirmed,
        "filled_at": filled_at,
    }


def test_an_add_that_fills_after_the_stop_out_is_its_own_lot_at_its_own_1r():
    """Open 100 @ $50 stop $45; a resting add 100 @ $60 stop $55 is SUBMITTED at 13:00; the
    stop takes the first 100 out at 14:00 (confirmed late, 16:00); the add FILLS at 15:00.
    Write time and confirmation time both put the add before the stop-out (one merged
    position); the broker's fill times say the book was flat before the add filled."""
    rows = [
        _row(
            1,
            "BUY",
            100,
            50.0,
            45.0,
            written=f"{DAY} 10:00:00",
            filled_at=f"{DAY} 10:00:00",
            confirmed=f"{DAY} 10:01:00",
        ),
        _row(
            2,
            "BUY",
            100,
            60.0,
            55.0,
            written=f"{DAY} 13:00:00",
            filled_at=f"{DAY} 15:00:00",
            confirmed=f"{DAY} 15:01:00",
        ),
        _row(3, "SELL", 100, 45.0, written=f"{DAY} 14:00:00", filled_at=f"{DAY} 14:00:00", confirmed=f"{DAY} 16:00:00"),
    ]
    assert [r["id"] for r in live_lot_rows(rows, decimals=ORDER_QTY_DECIMALS)] == [2]
    risk, _ = held_position_risk(rows, 100)
    assert risk == pytest.approx(500.0)  # 100 x ($60 - $55): 1R is $5/share


def test_through_the_ledger_the_late_add_gets_its_own_position_and_its_own_1r(db):
    from src.exits_parts.trails import _risk_per_share

    def write(action, qty, price, stop=None, *, oid=None, status="submitted", filled_at=None):
        db.insert_trade(
            symbol="LADD",
            action=action,
            qty=qty,
            price=price,
            reasoning="seq",
            run_id="r",
            stop_loss=stop,
            setup_type="range",
            fill_status=status,
            broker_order_id=oid,
            filled_at=filled_at,
        )

    write("BUY", 100, 50.0, 45.0, status="filled", filled_at=f"{DAY} 10:00:00")
    write("BUY", 100, 60.0, 55.0, oid="add")  # the resting add, submitted while 100 are held
    write("SELL", 100, 45.0, oid="stop")  # the protective stop's exit
    db.update_trade_fill("stop", "filled", 100, 45.0, filled_at=f"{DAY} 14:00:00")
    db.update_trade_fill("add", "filled", 100, 60.0, filled_at=f"{DAY} 15:00:00")

    opened, add, stop = [
        r[0] for r in db.conn.execute("SELECT position_id FROM trades WHERE symbol = 'LADD' ORDER BY id")
    ]
    assert opened == stop != add
    opening_row = db.get_symbol_last_buy("LADD", action="BUY")
    assert _risk_per_share(SimpleNamespace(db=db), opening_row, 100, "LADD") == pytest.approx(5.0)


def _stale_fresh_buy(db, symbol):
    """S6 setup: open 100 @ $110 stop $100 (fills 10:00); the broker's stop fills at 11:00
    unseen; a fresh 50 @ $115 stop $105 fills at 12:00 and is written while the ledger
    still holds 100. Returns the id both buys were stamped with."""
    for qty, price, stop, at in ((100, 110.0, 100.0, "10:00:00"), (50, 115.0, 105.0, "12:00:00")):
        db.insert_trade(
            symbol=symbol,
            action="BUY",
            qty=qty,
            price=price,
            reasoning="s6",
            run_id="r",
            stop_loss=stop,
            setup_type="range",
            fill_status="filled",
            filled_at=f"{DAY} {at}",
        )
    first, fresh = [
        r[0] for r in db.conn.execute("SELECT position_id FROM trades WHERE symbol = ? ORDER BY id", (symbol,))
    ]
    assert first == fresh  # stamped with the old id: the stop-out is not known yet
    return first


def _reconcile_stop_out(db, symbol):
    db.insert_stop_out_trade(
        symbol=symbol, qty=100, price=100.0, broker_order_id=f"{symbol}-stop", filled_at=f"{DAY} 11:00:00"
    )
    return [dict(r) for r in db.conn.execute("SELECT * FROM trades WHERE symbol = ? ORDER BY id", (symbol,))]


def test_s6_stop_out_reconciled_after_a_fresh_buy_splits_the_positions(db, tmp_path, monkeypatch):
    from src.api import db_reads
    from src.storage.analytics.trade_review import closed_r_by_exit_row, open_position_r

    _stale_fresh_buy(db, "SSIX")
    opened, fresh, stop_out = _reconcile_stop_out(db, "SSIX")
    assert opened["position_id"] == stop_out["position_id"] != fresh["position_id"]  # the fresh lot: a new id
    assert closed_r_by_exit_row([opened, fresh, stop_out])[stop_out["id"]] == pytest.approx(-1.0)
    monkeypatch.setattr(db_reads, "get_db_path", lambda: str(tmp_path / "fill_time.db"))
    held = [{"symbol": "SSIX", "qty": 50, "avg_entry": 115.0, "current_price": 116.0}]
    dash = open_position_r(held, db_reads.get_open_position_rows(["SSIX"]))
    assert dash["SSIX"]["initial_stop"] == pytest.approx(105.0)


def test_a_fill_without_a_fill_time_makes_r_unknown_once_shares_have_left():
    opened = _row(1, "BUY", 100, 50.0, 45.0, written=f"{DAY} 10:00:00", filled_at=None)
    added = _row(2, "BUY", 100, 60.0, 52.0, written=f"{DAY} 11:00:00", filled_at=None)
    trimmed = _row(3, "SELL", 40, 55.0, written=f"{DAY} 12:00:00", filled_at=f"{DAY} 12:00:00")
    assert FILL_ORDER_UNKNOWN == "fill_time_missing"
    assert held_position_risk([opened, added, trimmed], 160) == (None, FILL_ORDER_UNKNOWN)
    # With no exit on record every order gives the same lots, so R stays known.
    risk, reason = held_position_risk([opened, added], 200)
    assert risk == pytest.approx(1300.0) and reason != FILL_ORDER_UNKNOWN


def test_unknown_fill_order_keeps_the_r_ratchets_off(db):
    for symbol, opened_at, trimmed_at in (("KNWN", f"{DAY} 10:00:00", f"{DAY} 11:00:00"), ("UNKN", None, None)):
        db.insert_trade(
            symbol=symbol,
            action="BUY",
            qty=100,
            price=50.0,
            reasoning="open",
            run_id="r",
            stop_loss=45.0,
            setup_type="range",
            fill_status="filled",
            filled_at=opened_at,
        )
        db.insert_trade(
            symbol=symbol,
            action="SELL",
            qty=50,
            price=55.0,
            reasoning="trim",
            run_id="r",
            fill_status="filled",
            filled_at=trimmed_at,
        )
    # +2R on the 50 still held: the R ratchet fires only where the fill order is known.
    assert _r_ratchet_fired(_trail(db, _pos("KNWN", 50, 50.0, 60.0), live_stop=45.0))
    assert not _r_ratchet_fired(_trail(db, _pos("UNKN", 50, 50.0, 60.0), live_stop=45.0))


def test_the_backtest_lot_carries_its_simulated_fill_time(monkeypatch):
    from src.backtest import engine

    seen = []

    def _risk(rows, held):
        seen.extend(rows)
        return 500.0, "ok"

    monkeypatch.setattr("src.quantities.held_position_risk", _risk)
    pos = SimpleNamespace(
        direction="long", shares=100, entry_price=50.0, stop_initial=45.0, entry_date=date(2026, 1, 5)
    )
    assert engine._risk_per_share(pos, 100) == pytest.approx(5.0)
    assert seen[0]["filled_at"] == "2026-01-05"


def test_the_broker_read_returns_the_brokers_own_fill_time(monkeypatch):
    from src.execution.broker_parts import order_desk_reads as reads

    monkeypatch.setattr(reads, "ok", lambda *a, **k: None)
    filled = datetime(2026, 1, 5, 15, tzinfo=timezone.utc)
    order = SimpleNamespace(status="filled", filled_qty="10", filled_avg_price="20", filled_at=filled)
    desk = SimpleNamespace(client=SimpleNamespace(get_order_by_id=lambda _oid: order))
    assert reads.get_order_fill_info(desk, "o1")["filled_at"] == "2026-01-05T15:00:00+00:00"
    order.filled_at = None
    assert reads.get_order_fill_info(desk, "o1")["filled_at"] is None  # none reported: never "now"


def test_the_reconciler_writes_the_brokers_fill_time_never_now(db):
    from src.protection.fill_reconciler import FillReconciler

    db.insert_trade(
        symbol="REC",
        action="BUY",
        qty=10,
        price=20.0,
        reasoning="r",
        run_id="r",
        stop_loss=18.0,
        setup_type="range",
        fill_status="submitted",
        broker_order_id="rec-1",
    )
    info = {
        "status": "filled",
        "filled_qty": 10,
        "filled_avg_price": 20.0,
        "filled_at": "2026-01-05T15:00:00.123456+00:00",
    }
    FillReconciler(
        broker=SimpleNamespace(get_order_fill_info=lambda _oid: info),
        db=db,
        config=SimpleNamespace(),
        flag_stop_out_anomaly=None,
        format_qty=str,
        parse_broker_fill_timestamp=FillReconciler._parse_broker_fill_timestamp,
    )._reconcile_fills()
    (filled_at,) = db.conn.execute("SELECT filled_at FROM trades WHERE broker_order_id = 'rec-1'").fetchone()
    assert filled_at == "2026-01-05 15:00:00.123456"  # the broker's microseconds, never truncated
    assert filled_at.startswith("2026-01-05 15:00")


def test_stored_ids_are_the_walk_every_reader_agrees_and_a_regroup_voids_credit(db):
    from src.storage.conviction_ledger_store import CONVICTION_CREDIT_KIND
    from src.storage.trades.position_chain import _event_order, _executed_row
    from src.trade_actions import walk_positions

    merged = _stale_fresh_buy(db, "WALK")
    db.conn.execute(
        "INSERT INTO specialist_evidence (run_id, decision_id, agent_name, kind, scope, symbol, evidence_json)"
        " VALUES (?,?,?,?,?,?,?)",
        ("r", "d", "macro", CONVICTION_CREDIT_KIND, "symbol", "WALK", json.dumps({"position_id": merged})),
    )
    db.conn.commit()
    rows = _reconcile_stop_out(db, "WALK")

    groups, _, last_open = walk_positions(_event_order(rows), decimals=ORDER_QTY_DECIMALS, executed=_executed_row)
    stored = [{r["position_id"] for r in group} for group in groups]
    assert len(groups) == 2 and all(len(ids) == 1 for ids in stored) and stored[0] != stored[1]
    # The open-R lot walk's live lots ARE the stored open position's lots.
    (open_id,) = stored[-1]
    executed = [r for r in rows if _executed_row(r)]
    assert last_open
    assert [r["id"] for r in live_lot_rows(executed, decimals=ORDER_QTY_DECIMALS)] == [
        r["id"] for r in executed if r["position_id"] == open_id
    ]
    # Credit scored while the fresh lot sat inside the old position is void; the scorer re-scores it.
    count = db.conn.execute("SELECT COUNT(*) FROM specialist_evidence WHERE kind = ?", (CONVICTION_CREDIT_KIND,))
    assert count.fetchone()[0] == 0


def test_an_add_and_a_stop_out_in_the_same_second_go_in_microsecond_order():
    """The stop took the old 100 out at 14:00:00.2; the resting add, written first (lower row
    id), filled at 14:00:00.7. Cut to the second the two tie and the row id puts the add
    first - one merged position; at full precision the book was flat before the add filled."""
    stop_out_at = broker_fill_time(f"{DAY}T14:00:00.200000+00:00")
    add_at = broker_fill_time(f"{DAY}T14:00:00.700000+00:00")
    assert (stop_out_at, add_at) == (f"{DAY} 14:00:00.200000", f"{DAY} 14:00:00.700000")
    rows = [
        _row(1, "BUY", 100, 50.0, 45.0, written=f"{DAY} 10:00:00", filled_at=f"{DAY} 10:00:00.000000"),
        _row(2, "BUY", 100, 60.0, 55.0, written=f"{DAY} 13:00:00", filled_at=add_at),
        _row(3, "SELL", 100, 45.0, written=f"{DAY} 14:00:00", filled_at=stop_out_at),
    ]
    assert [r["id"] for r in live_lot_rows(rows, decimals=ORDER_QTY_DECIMALS)] == [2]
    assert held_position_risk(rows, 100) == (pytest.approx(500.0), "ok")  # the add's own 1R: 100 x $5


def test_an_old_untimed_position_no_longer_makes_the_live_positions_r_unknown():
    """An old position bought and sold before fill times were recorded - its buy has no broker
    order id, so it can never be backfilled - then a new position with every fill timed. Only
    the live position's rows matter, so R is known; a fill with no time INSIDE it is not."""
    old_buy = _row(1, "BUY", 100, 40.0, 35.0, written="2025-12-01 10:00:00", filled_at=None)
    old_buy["broker_order_id"] = None
    old_sell = _row(2, "SELL", 100, 44.0, written="2025-12-01 15:00:00", filled_at=None)
    new_buy = _row(3, "BUY", 50, 60.0, 55.0, written=f"{DAY} 10:00:00", filled_at=f"{DAY} 10:00:00.250000")
    rows = [old_buy, old_sell, new_buy]
    assert [r["id"] for r in live_lot_rows(rows, decimals=ORDER_QTY_DECIMALS, held=50)] == [3]
    assert held_position_risk(rows, 50) == (pytest.approx(250.0), "ok")  # 50 x $5
    add = _row(4, "BUY", 50, 62.0, 57.0, written=f"{DAY} 11:00:00", filled_at=None)
    trim = _row(5, "SELL", 20, 63.0, written=f"{DAY} 12:00:00", filled_at=f"{DAY} 12:00:00.000000")
    assert held_position_risk([*rows, add, trim], 80) == (None, FILL_ORDER_UNKNOWN)
