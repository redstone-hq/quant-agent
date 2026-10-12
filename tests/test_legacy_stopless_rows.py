"""Legacy stopless entry rows: downstream readers still handle them honestly.

The ledger now REFUSES to write a stopless BUY/SHORT, so these two tests
(moved unchanged in intent from tests/test_conviction_ledger.py and
tests/test_stop_writeback.py, which sit at their size baselines) seed the
legacy row the way history holds it: written with a stop, then the stop
erased.
"""

import pytest

from src.execution.stop_records import recorded_initial_stop, write_back_stop_loss
from src.conviction_ledger import SeatStance
from src.storage.db import Database


@pytest.fixture
def db(tmp_path):
    database = Database(str(tmp_path / "legacy.db"))
    database.initialize()
    yield database
    database.close()


def test_resolve_never_scores_a_position_with_no_entry_stop(db):
    """No stop at entry means no honest R denominator. Counted, never guessed."""
    # A stopless entry can no longer be WRITTEN (the ledger refuses it), so
    # this seeds the legacy row the way history holds it: written, then its
    # stop erased. The resolver must still count it, never guess a stop.
    with pytest.raises(ValueError, match="refusing to record"):
        db.insert_trade(
            symbol="IBM",
            action="BUY",
            qty=10,
            price=100.0,
            reasoning="entry",
            run_id="run-1",
            stop_loss=0,
            fill_status="filled",
            decision_id="dec-1",
        )
    db.insert_trade(
        symbol="IBM",
        action="BUY",
        qty=10,
        price=100.0,
        reasoning="entry",
        run_id="run-1",
        stop_loss=90.0,
        fill_status="filled",
        decision_id="dec-1",
    )
    db.conn.execute(
        "UPDATE trades SET stop_loss = 0, initial_stop_loss = NULL WHERE symbol = 'IBM'",
    )
    db.conn.commit()
    db.insert_trade(
        symbol="IBM",
        action="SELL",
        qty=10,
        price=120.0,
        reasoning="target",
        run_id="run-2",
        fill_status="filled",
    )
    db.record_seat_stances(
        run_id="run-1",
        decision_id="dec-1",
        stances=[
            SeatStance(seat="technical", symbol="IBM", stance="buy"),
        ],
    )
    result = db.conviction.resolve_conviction_ledger()
    assert result["skipped_no_r"] == 1
    assert db.conviction.get_conviction_credits() == []


def test_zero_entry_stop_write_back_does_not_mint_an_entry_bet(db):
    # The ledger now refuses to WRITE a stopless entry; this seeds the legacy
    # row as history holds it (written, then its stop erased).
    with pytest.raises(ValueError, match="refusing to record"):
        db.insert_trade(
            symbol="NAKED",
            action="BUY",
            qty=5,
            price=100.0,
            reasoning="entry",
            run_id="r1",
            stop_loss=0,
            fill_status="filled",
        )
    db.insert_trade(
        symbol="NAKED",
        action="BUY",
        qty=5,
        price=100.0,
        reasoning="entry",
        run_id="r1",
        stop_loss=90.0,
        fill_status="filled",
    )
    db.conn.execute(
        "UPDATE trades SET stop_loss = 0, initial_stop_loss = NULL WHERE symbol = 'NAKED'",
    )
    db.conn.commit()
    assert recorded_initial_stop(db.get_symbol_last_buy("NAKED")) == 0.0
    assert write_back_stop_loss(db, "NAKED", 97.0) is True
    row = db.get_symbol_last_buy("NAKED")
    assert row["stop_loss"] == pytest.approx(97.0)
    assert recorded_initial_stop(row) == 0.0
