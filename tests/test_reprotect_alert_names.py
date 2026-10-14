"""The "stops put back" alert names each stock, with the stop it got back.

When the desk puts protective stops back on positions an earlier run left
unprotected, the owner's alert used to say only HOW MANY. These tests drain two
pending restores (ETN and PWR) through the real drain with a fake broker and
hand its answer to the real surfacing step, exactly as every session does.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

import src.notifier as notifier
from src.storage.db import Database
from tests.pipeline_factory import build_pipeline


@pytest.fixture
def world(tmp_path, monkeypatch):
    db = Database(str(tmp_path / "t.db"))
    db.initialize()
    for symbol, qty, stop in (("ETN", 40.0, 280.5), ("PWR", 25.0, 312.25)):
        db.insert_pending_protection_restore(
            symbol=symbol,
            sell_order_id=f"sell-{symbol}",
            position_qty_before_sell=qty,
            specs_json=json.dumps([{"id": f"old-{symbol}", "qty": qty, "stop_price": stop, "limit_price": stop - 3}]),
        )
    # Pretend both stops went missing three hours ago.
    db._trades().conn.execute("UPDATE pending_protection_restores SET created_at = datetime('now', '-3 hours')")
    db._trades().conn.commit()

    broker = MagicMock()
    broker.get_order_fill_info.return_value = {"status": "canceled", "filled_qty": "0", "filled_avg_price": None}
    broker._restore_stop_orders.return_value = (1, [])
    pipeline = build_pipeline(db=db, broker=broker)

    sent = []
    monkeypatch.setattr(notifier, "send_owner_alert", lambda text, **kw: sent.append((text, kw)) or True)
    return pipeline, db, sent


def test_alert_names_each_restored_stock_and_its_stop(world):
    pipeline, db, sent = world

    drained = pipeline._drain_pending_protection_restores()
    pipeline._surface_reconcile_outcomes([], drained, run_id="r1")

    assert len(sent) == 1
    text, _ = sent[0]
    assert "ETN" in text and "PWR" in text
    assert "$280.50" in text and "$312.25" in text
    assert "3 hours" in text
    assert db.get_pending_protection_restores() == []


def test_drain_hands_back_one_record_per_restored_stock(world):
    pipeline, _db, _sent = world

    drained = pipeline._drain_pending_protection_restores()

    assert [r["symbol"] for r in drained] == ["ETN", "PWR"]
    assert [r["shares"] for r in drained] == [40.0, 25.0]
    assert [r["stop_price"] for r in drained] == [280.5, 312.25]
    assert all(r["unprotected_since"] for r in drained)
