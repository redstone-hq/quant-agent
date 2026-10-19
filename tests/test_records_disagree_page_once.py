"""The 'records disagree, no sale explains it' page fires once per unchanged state.

The reconciler runs every ~30 min; before this the older page re-sent on every
pass while the newer owner pages (#1884) were already once-per-state.
"""

import types
from unittest.mock import MagicMock, patch

from src.storage.db import Database
from tests.pipeline_factory import build_pipeline


def _pipeline(db, broker):
    p = build_pipeline(db=db, broker=broker)
    p.config = types.SimpleNamespace(reconciliation=types.SimpleNamespace(stop_out_lookback_days=7))
    return p


def _setup(tmp_path, qty):
    db = Database(str(tmp_path / "t.db"))
    db.initialize()
    db.insert_trade("ZZZA", "BUY", qty, 8.5, "entry", "r1", fill_status="filled", stop_loss=7.0)
    broker = MagicMock()
    broker.get_positions.return_value = []
    broker.list_filled_sell_orders.return_value = []
    return db, broker


def _pass(db, broker, run_id):
    sent = MagicMock(return_value=True)
    with (
        patch("src.notifier.alerts.send_owner_alert", sent),
        patch("src.notifier.send_owner_alert", sent),
    ):
        _pipeline(db, broker)._reconcile_stop_out_fills(run_id=run_id)
    return sent


def _events(db):
    return db.conn.execute(
        "SELECT 1 FROM specialist_evidence WHERE symbol='ZZZA' AND kind='pipeline_event' "
        "AND evidence_json LIKE '%stop_out_gap_unexplained%'"
    ).fetchall()


def test_identical_passes_page_once_but_flag_evidence_each_time(tmp_path):
    db, broker = _setup(tmp_path, 17)
    first = _pass(db, broker, "r1")
    second = _pass(db, broker, "r2")
    assert first.call_count == 1
    assert second.call_count == 0, "same disagreement state must not page again"
    assert len(_events(db)) == 2


def test_changed_quantity_pages_again(tmp_path):
    db, broker = _setup(tmp_path, 17)
    assert _pass(db, broker, "r1").call_count == 1
    db.insert_trade("ZZZA", "BUY", 3, 8.5, "entry", "r2", fill_status="filled", stop_loss=7.0)
    assert _pass(db, broker, "r2").call_count == 1
