"""The resting-entry sweep owed for a Stop is remembered per acted Stop, not per process."""

import sqlite3

from src.owner_intents import RESUME, STOP, honour_stop, process_pending, raise_intent, stop_sweep_owed
from src.storage.schema.owner_intent_tables import apply


def _db(tmp_path, *actions):
    db = tmp_path / "intents.db"
    conn = sqlite3.connect(db)
    apply(conn)
    for action in actions:
        raise_intent(conn, action)
        process_pending(conn)
    conn.close()
    return str(db)


class _Broker:
    def __init__(self, fail=False):
        self.fail = fail

    def sweep_frozen_resting_orders(self, _db):
        if self.fail:
            raise ConnectionError("broker down")
        return {"cancelled": 1}


def test_no_stop_owes_no_sweep(tmp_path):
    assert stop_sweep_owed(_db(tmp_path)) is False


def test_a_successful_sweep_settles_that_stop(tmp_path):
    db = _db(tmp_path, STOP)
    assert stop_sweep_owed(db) is True
    assert honour_stop(db, _Broker) is True
    assert stop_sweep_owed(db) is False


def test_a_failed_sweep_stays_owed(tmp_path):
    db = _db(tmp_path, STOP)
    assert honour_stop(db, lambda: _Broker(fail=True)) is True
    assert stop_sweep_owed(db) is True


def test_a_new_stop_after_start_owes_a_fresh_sweep(tmp_path):
    db = _db(tmp_path, STOP)
    honour_stop(db, _Broker)
    conn = sqlite3.connect(db)
    for action in (RESUME, STOP):
        raise_intent(conn, action)
        process_pending(conn)
    conn.close()
    assert stop_sweep_owed(db) is True
