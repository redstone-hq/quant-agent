"""The nightly scorer must size R from the stop AT ENTRY, not the live trailed stop."""

import pytest

from src.conviction_ledger import SeatStance
from src.storage.db import Database


@pytest.fixture
def db(tmp_path):
    database = Database(str(tmp_path / "initial_stop.db"))
    database.initialize()  # real schema via the schema manager
    yield database
    database.close()


def test_scorer_uses_entry_stop_after_trail_above_entry(db):
    db.insert_trade(
        symbol="AAPL", action="BUY", qty=10, price=100.0, reasoning="entry",
        run_id="run-1", stop_loss=90.0, fill_status="filled", decision_id="dec-1",
    )
    db.update_open_stop_loss("AAPL", 105.0, action="BUY")  # trailed above entry
    db.insert_trade(
        symbol="AAPL", action="SELL", qty=10, price=110.0, reasoning="exit",
        run_id="run-2", fill_status="filled",
    )
    db.record_seat_stances(
        run_id="run-1", decision_id="dec-1",
        stances=[SeatStance(seat="technical", symbol="AAPL", stance="buy")],
    )
    row = db.conn.execute(
        "SELECT stop_loss, initial_stop_loss FROM trades WHERE action = 'BUY'"
    ).fetchone()
    assert (row["stop_loss"], row["initial_stop_loss"]) == (105.0, 90.0)

    result = db.conviction.resolve_conviction_ledger()
    assert result["skipped_no_r"] == 0
    assert result["scored_positions"] == 1
    credits = db.conviction.get_conviction_credits()
    assert credits[0].r_multiple == pytest.approx(1.0)
