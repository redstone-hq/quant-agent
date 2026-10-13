"""The fill-time backfill: dry-run by default, writes only with --execute --owner-approved (fake broker)."""

import importlib.util
import sqlite3
from pathlib import Path

import pytest

from src.protection.fill_reconciler import FillReconciler
from src.quantities import held_position_risk
from src.storage.db import Database

_SPEC = importlib.util.spec_from_file_location(
    "backfill_fill_times", Path(__file__).resolve().parent.parent / "scripts" / "backfill_fill_times.py"
)
backfill = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(backfill)

BROKER_FILL_TIMES = {
    "a-buy": "2026-01-05T14:00:00+00:00",
    "a-sell": "2026-01-05T15:00:00+00:00",
    "b-sell": "2026-01-05T15:30:00+00:00",
}


class FakeBroker:
    def __init__(self):
        self.asked = []

    def get_order_fill_info(self, order_id):
        self.asked.append(order_id)
        return {"status": "filled", "filled_at": BROKER_FILL_TIMES.get(order_id)}


@pytest.fixture
def db_path(tmp_path):
    """Legacy fills (no fill time): AAA bought and trimmed by order id; BBB's buy has no order id."""
    path = tmp_path / "copy.db"
    db = Database(str(path))
    db.initialize()
    for symbol, action, qty, oid in (
        ("AAA", "BUY", 100, "a-buy"),
        ("AAA", "SELL", 40, "a-sell"),
        ("BBB", "BUY", 100, None),
        ("BBB", "SELL", 40, "b-sell"),
    ):
        db.insert_trade(
            symbol=symbol,
            action=action,
            qty=qty,
            price=50.0,
            reasoning="legacy",
            run_id="r",
            stop_loss=45.0 if action == "BUY" else None,
            fill_status="filled",
            broker_order_id=oid,
        )
    db.close()
    return path


def _rows(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute("SELECT * FROM trades ORDER BY id")]
    finally:
        conn.close()


def test_dry_run_reports_the_gaps_and_writes_nothing(db_path, capsys):
    before = _rows(db_path)
    assert backfill.main(["--db-path", str(db_path)], broker=FakeBroker()) == 0
    out = capsys.readouterr().out
    assert "Executed fills without a broker fill time: 4" in out
    assert "without a broker order id (cannot be backfilled): 1" in out
    assert "broker returned a fill time (would be written): 3" in out
    assert "Symbols open in the ledger that would still have R unknown: 1\n  BBB\n" in out
    assert _rows(db_path) == before


@pytest.mark.parametrize("flags", [["--execute"], ["--owner-approved"]])
def test_a_write_without_both_flags_is_refused(db_path, flags):
    before = _rows(db_path)
    broker = FakeBroker()
    assert backfill.main(["--db-path", str(db_path), *flags], broker=broker) == 2
    assert _rows(db_path) == before and broker.asked == []


def test_execute_owner_approved_writes_the_brokers_fill_times_and_r_becomes_known(db_path):
    assert backfill.main(["--db-path", str(db_path), "--execute", "--owner-approved"], broker=FakeBroker()) == 0
    rows = _rows(db_path)
    parse = FillReconciler._parse_broker_fill_timestamp
    assert {r["broker_order_id"]: r["filled_at"] for r in rows} == {
        "a-buy": parse(BROKER_FILL_TIMES["a-buy"]),
        "a-sell": parse(BROKER_FILL_TIMES["a-sell"]),
        None: None,
        "b-sell": parse(BROKER_FILL_TIMES["b-sell"]),
    }
    risk, _ = held_position_risk([r for r in rows if r["symbol"] == "AAA"], 60)
    assert risk == pytest.approx(300.0)  # 60 held x $5
    assert held_position_risk([r for r in rows if r["symbol"] == "BBB"], 60) == (None, "fill_time_missing")
