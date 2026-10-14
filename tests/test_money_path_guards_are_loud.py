"""A money-path catch-all must be LOUD: full traceback plus one counted row.

The defect this file exists to stop: an argument passed twice raised a
TypeError, a broad `except Exception` swallowed it as a one-line log, and the
per-name record was simply never written. Nothing complained, and the path
read as merely quiet. These tests pin the three states apart — never ran,
ran clean, ran and swallowed — on the protection-restore drain, which is the
path that rebuilds a stop after a sell and so decides whether shares sit
unprotected overnight.
"""

import inspect
import json
import logging

from src.pipeline_protection import RestoreDrain, _WAL_SELL_SENTINEL
from src.sentinel.reconciliation import (
    AGREED,
    DISAGREED,
    NOT_RUN,
    ReconciliationLog,
    record_guarded_outcome,
)
from src.storage.db import Database


def _db(tmp_path) -> Database:
    db = Database(str(tmp_path / "guards.db"))
    db.initialize()
    return db


def _status(db, where: str) -> str:
    return ReconciliationLog(conn=db.conn).status(kind=f"guarded:{where}")


def _detail(db, where: str) -> str:
    row = ReconciliationLog(conn=db.conn).latest(kind=f"guarded:{where}")
    return "" if row is None else row["detail"]


def test_a_swallowed_programming_error_logs_a_traceback_and_counts_a_row(tmp_path, caplog):
    db = _db(tmp_path)
    assert _status(db, "demo") == NOT_RUN
    with caplog.at_level(logging.ERROR):
        try:
            dict(a=1, **{"a": 2})  # a real duplicate-argument TypeError
        except TypeError as exc:
            record_guarded_outcome(db=db, where="demo", exc=exc)
    record = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert record, "a swallowed programming error must log at ERROR"
    assert record[0].exc_info is not None, "the full traceback must be attached"
    assert _status(db, "demo") == DISAGREED
    assert "TypeError" in _detail(db, "demo")


def test_ran_clean_is_a_different_answer_from_never_ran(tmp_path):
    db = _db(tmp_path)
    assert _status(db, "quiet") == NOT_RUN
    record_guarded_outcome(db=db, where="quiet")
    assert _status(db, "quiet") == AGREED, (
        "a counter that only fires on failure cannot tell a clean pass from a site that was never reached"
    )


def _drain(db, broker):
    """Build the drain with whatever collaborators its constructor takes."""
    offered = {
        "db": db,
        "broker": broker,
        "market": None,
        "restore_after_unconfirmed_sell": lambda *a, **k: (False, None),
        "finalize_protection_after_sell": lambda *a, **k: (False, None),
    }
    names = set(inspect.signature(RestoreDrain.__init__).parameters)
    return RestoreDrain(**{k: v for k, v in offered.items() if k in names})


class _BrokerRaisingTypeError:
    def get_order_fill_info(self, order_id):
        raise TypeError("get_order_fill_info() got multiple values for 'order_id'")


def test_the_drain_makes_a_broker_query_programming_error_loud(tmp_path, caplog):
    db = _db(tmp_path)
    db.insert_pending_protection_restore(
        symbol="AAA",
        sell_order_id="order-1",
        position_qty_before_sell=10.0,
        specs_json=json.dumps([{"stop_price": 1.0}]),
        side="sell",
    )
    drain = _drain(db, _BrokerRaisingTypeError())
    with caplog.at_level(logging.ERROR):
        assert len(drain._drain_pending_protection_restores()) == 0
    assert _status(db, "drain.broker_fill_query") == DISAGREED
    assert "TypeError" in _detail(db, "drain.broker_fill_query")
    assert any(r.exc_info for r in caplog.records if r.levelno >= logging.ERROR)
    assert _status(db, "drain.completed") == AGREED, (
        "the drain must leave proof it RAN, or a clean run and a run that never happened look identical"
    )


def test_an_unparseable_row_is_counted_and_the_sentinel_row_does_not_crash(tmp_path):
    db = _db(tmp_path)
    db.insert_pending_protection_restore(
        symbol="BBB",
        sell_order_id=_WAL_SELL_SENTINEL,
        position_qty_before_sell=5.0,
        specs_json="{not json",
        side="sell",
    )
    drain = _drain(db, _BrokerRaisingTypeError())
    assert len(drain._drain_pending_protection_restores()) == 0
    assert _status(db, "drain.wal_specs_parse") == DISAGREED
