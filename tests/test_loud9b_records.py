"""Swallowed faults in the stop write-back and exit records leave a counted row."""

from src.exits.exit_records import ExitRecords
from src.execution.stop_records import write_back_stop_loss
from src.storage.db import Database


def _kinds(db):
    rows = db.conn.execute("SELECT kind FROM reconciliation_runs").fetchall()
    return [r[0] for r in rows]


class _Boom:
    def __init__(self, conn):
        self.conn = conn

    def update_open_stop_loss(self, symbol, price, **kw):
        raise RuntimeError("boom")


def test_stop_write_back_failure_is_a_counted_row():
    real = Database(":memory:")
    real.initialize()
    assert write_back_stop_loss(_Boom(real.conn), "AAA", 10.0) is False
    assert "guarded:execution.stop_records.write_back_stop_loss" in _kinds(real)
