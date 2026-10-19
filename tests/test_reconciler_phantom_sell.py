"""Missed-sell write-back must only close the CURRENT position, and only exactly.

Measured 2026-10-19 14:08 UTC: an owner hand-sale of 0.688 XOM left the
ledger at 36.688 vs the broker's 36. The stop-out reconciler then wrote back
EVERY unknown broker sell in its 60-day window, including a 24 Aug 5-share
desk sale from before the current position existed, leaving the ledger at 31
vs 36 with a phantom realized loss, and its P&L check read the newest row
instead of the one it had just written. Tickers here are fictional (ZZZ*).
"""

import types
from unittest.mock import MagicMock, patch

from src.storage.db import Database
from tests.pipeline_factory import build_pipeline


def _pipeline(db, broker):
    pipeline = build_pipeline(db=db, broker=broker)
    pipeline.config = types.SimpleNamespace(reconciliation=types.SimpleNamespace(stop_out_lookback_days=60))
    return pipeline


def _buy(db, symbol, qty, price, order_id, ts):
    db.insert_trade(
        symbol, "BUY", qty, price, "entry", "r1", broker_order_id=order_id, fill_status="submitted", stop_loss=90.0
    )
    db.update_trade_fill(broker_order_id=order_id, fill_status="filled", fill_qty=qty, fill_price=price)
    db.conn.execute("UPDATE trades SET timestamp = ? WHERE broker_order_id = ?", (ts, order_id))
    db.conn.commit()


def _sell(order_id, qty, price, filled_at, order_type="market"):
    return {"id": order_id, "qty": qty, "price": price, "filled_at": filled_at, "order_type": order_type}


def _broker(held, fills):
    broker = MagicMock()
    broker.get_positions.return_value = [types.SimpleNamespace(symbol=s, qty=q) for s, q in held.items()]
    broker.list_filled_sell_orders.return_value = fills
    return broker


def _outcomes(db, symbol):
    rows = db.conn.execute(
        "SELECT evidence_json FROM specialist_evidence WHERE symbol = ? AND kind = 'pipeline_event'", (symbol,)
    ).fetchall()
    return " ".join(r["evidence_json"] for r in rows)


def _db(tmp_path):
    db = Database(str(tmp_path / "t.db"))
    db.initialize()
    return db


def test_sale_from_before_the_position_opened_is_never_written_back(tmp_path):
    """The 19 Oct shape: only the owner's 0.688 sale is written; the 24 Aug pre-position sale is not."""
    db = _db(tmp_path)
    _buy(db, "ZZZX", 36.688, 170.0, "buy-zzzx", "2026-09-02 14:00:00")
    broker = _broker(
        {"ZZZX": 36.0},
        [
            _sell("old-desk-sale", 5.0, 163.78, "2026-08-24T19:31:30+00:00"),
            _sell("owner-sale", 0.688, 175.0, "2026-10-19T13:30:00+00:00"),
        ],
    )
    with patch("src.notifier.send_owner_alert") as page:
        _pipeline(db, broker)._reconcile_stop_out_fills(run_id="r1")

    written = {r["broker_order_id"] for r in db.get_trades(symbol="ZZZX") if r["action"] != "BUY"}
    assert written == {"owner-sale"}
    assert abs(db.get_symbols_with_open_ledger_qty()["ZZZX"] - 36.0) < 1e-6
    page.assert_not_called()


def test_gap_no_whole_orders_fit_writes_nothing_and_pages(tmp_path):
    """Gap 4; post-open sales of 3 and 2 cannot make 4 exactly — nothing written, owner paged."""
    db = _db(tmp_path)
    _buy(db, "ZZZY", 10.0, 50.0, "buy-zzzy", "2026-09-02 14:00:00")
    broker = _broker(
        {"ZZZY": 6.0},
        [_sell("s3", 3.0, 51.0, "2026-10-01T15:00:00+00:00"), _sell("s2", 2.0, 52.0, "2026-10-02T15:00:00+00:00")],
    )
    with patch("src.notifier.send_owner_alert") as page:
        results = _pipeline(db, broker)._reconcile_stop_out_fills(run_id="r1")

    assert [r["action"] for r in db.get_trades(symbol="ZZZY")] == ["BUY"]
    assert results[0]["recorded"] == 0 and results[0]["matched"] is False
    page.assert_called_once()
    text = page.call_args.args[0]
    assert "ZZZY" in text and "gap of 4" in text and "3" in text and "2" in text
    assert "stop_out_gap_no_exact_fit" in _outcomes(db, "ZZZY")


def test_ledger_short_of_broker_pages_the_owner(tmp_path):
    """The broker holds 12, the ledger 10 — no longer passed silently."""
    db = _db(tmp_path)
    _buy(db, "ZZZW", 10.0, 50.0, "buy-zzzw", "2026-09-02 14:00:00")
    broker = _broker({"ZZZW": 12.0}, [])
    with patch("src.notifier.send_owner_alert") as page:
        _pipeline(db, broker)._reconcile_stop_out_fills(run_id="r1")

    page.assert_called_once()
    assert "ZZZW" in page.call_args.args[0]
    assert "stop_out_ledger_short" in _outcomes(db, "ZZZW")
    broker.list_filled_sell_orders.assert_not_called()


def test_pnl_check_reads_the_row_it_just_wrote(tmp_path):
    """The written fill is OLDER than the newest ledger row; its P&L must still be read, not mislabelled unmatched."""
    db = _db(tmp_path)
    _buy(db, "ZZZV", 10.0, 50.0, "buy-zzzv-1", "2026-09-02 14:00:00")
    _buy(db, "ZZZV", 5.0, 50.0, "buy-zzzv-2", "2026-10-18 14:00:00")
    broker = _broker({"ZZZV": 13.0}, [_sell("s-mid", 2.0, 55.0, "2026-10-10T15:00:00+00:00")])
    with patch("src.notifier.send_owner_alert"):
        _pipeline(db, broker)._reconcile_stop_out_fills(run_id="r1")

    row = next(r for r in db.get_trades(symbol="ZZZV", limit=-1) if r["broker_order_id"] == "s-mid")
    assert row["realized_pnl"] is not None
    assert "stop_out_pnl_unmatched" not in _outcomes(db, "ZZZV")


def test_unchanged_disagreement_pages_once_and_a_changed_gap_pages_again(tmp_path):
    """Every pass records evidence; only the first pass in an unchanged state pages, a changed gap re-pages."""
    db = _db(tmp_path)
    _buy(db, "ZZZU", 10.0, 50.0, "buy-zzzu", "2026-09-02 14:00:00")
    broker = _broker({"ZZZU": 12.0}, [])
    pipeline = _pipeline(db, broker)
    with patch("src.notifier.send_owner_alert", return_value=True) as page:
        pipeline._reconcile_stop_out_fills(run_id="r1")
        pipeline._reconcile_stop_out_fills(run_id="r2")
        assert page.call_count == 1
        assert _outcomes(db, "ZZZU").count("stop_out_ledger_short") == 2

        broker.get_positions.return_value = [types.SimpleNamespace(symbol="ZZZU", qty=13.0)]
        pipeline._reconcile_stop_out_fills(run_id="r3")
        assert page.call_count == 2
        assert "13" in page.call_args.args[0]
