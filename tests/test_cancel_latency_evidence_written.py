"""A settled cancel leg writes its `cancel_latency` evidence row.

The broker's lent ledger handle is a `_LazyLedger` (contract: `.conn` only),
not a `Database`; the write must go through that connection.
"""

from types import SimpleNamespace

from src.execution.broker_parts.cancel_confirm import LegResult, _record_latency
from src.sentinel.guarded_reach import RECON_DB_ATTR, _LazyLedger
from src.storage.db import Database


def _db(tmp_path):
    db = Database(str(tmp_path / "ledger.db"))
    db.initialize()
    return db


def _leg():
    return LegResult("ord-1", "canceled", 0.0, 120.0, side="sell", kind="stop", latency_ms=250.0)


def test_cancel_latency_row_written_through_lent_handle(tmp_path):
    db = _db(tmp_path)
    broker = SimpleNamespace()
    setattr(broker, RECON_DB_ATTR, _LazyLedger(lambda: db.conn))

    _record_latency(broker, _leg(), 15.0)

    rows = db.conn.execute(
        "SELECT agent_name, scope, evidence_json FROM specialist_evidence WHERE kind='cancel_latency'"
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["agent_name"] == "settle_legs"
    assert '"order_id": "ord-1"' in rows[0]["evidence_json"]
    assert '"within_bound": true' in rows[0]["evidence_json"]
    faults = db.conn.execute("SELECT COUNT(*) FROM reconciliation_runs WHERE agreed = 0").fetchone()[0]
    assert faults == 0


def test_unreachable_ledger_does_not_raise(tmp_path):
    broker = SimpleNamespace()
    setattr(broker, RECON_DB_ATTR, _LazyLedger(lambda: None))
    _record_latency(broker, _leg(), 15.0)
