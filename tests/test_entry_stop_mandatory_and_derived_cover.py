"""Two halves of one defect: a desk entry can never be recorded without the
stop it decided to protect it at, and a held position the desk has no row
for still gets protected instead of being flagged for a human.

Owner ruling 2026-10-02: "the desk needs to act ... doubling down or
duplicating is the answer. After it's already exhausted retries and
different asks." A gap left for "manual review" is the same as doing
nothing, because nobody is watching a screen.
"""

from datetime import date, timedelta

import pytest

from src.models.analysis import OHLCV
from src.storage.db import Database
from src.execution.stop_repair import repair_stop_coverage


def _db(tmp_path):
    d = Database(str(tmp_path / "t.db"))
    d.initialize()
    return d


def test_entry_row_without_a_usable_stop_is_refused(tmp_path):
    db = _db(tmp_path)
    with pytest.raises(ValueError) as exc:
        db.insert_trade(
            symbol="TESTA",
            action="BUY",
            qty=10,
            price=100.0,
            reasoning="entry with no stop",
            run_id="r1",
        )
    assert "stop" in str(exc.value).lower()
    # the same refusal for a zero, which the schema default used to supply
    with pytest.raises(ValueError):
        db.insert_trade(
            symbol="TESTA",
            action="SHORT",
            qty=10,
            price=100.0,
            reasoning="short with a zero stop",
            run_id="r1",
            stop_loss=0.0,
        )
    # and a real entry with a real level still records
    row = db.insert_trade(
        symbol="TESTA",
        action="BUY",
        qty=10,
        price=100.0,
        reasoning="entry with a stop",
        run_id="r1",
        stop_loss=92.0,
    )
    assert row


def _bars(n=30, close=100.0):
    out = []
    start = date(2026, 1, 5)
    for i in range(n):
        c = close + (i % 3) - 1
        out.append(OHLCV(date=start + timedelta(days=i), open=c, high=c + 2.0, low=c - 2.0, close=c, volume=1_000_000))
    return out


class _Market:
    def get_ohlcv(self, symbol, lookback_days=120):
        return _bars()


class _Position:
    def __init__(self):
        self.symbol = "TESTB"
        self.qty = 10.0
        self.avg_entry_price = 100.0


class _Broker:
    def __init__(self):
        self.placed = []

    def get_positions(self):
        return [_Position()]

    def get_latest_price(self, symbol):
        return 100.0

    def _submit_protective_stop_retrying(self, **kw):
        self.placed.append(kw)
        return {"id": "oid-1"}


def test_position_with_no_desk_row_gets_a_derived_stop_placed(tmp_path):
    broker = _Broker()
    outcome = {}
    ok = repair_stop_coverage(
        broker=broker,
        last_buy=lambda *a, **k: None,
        symbol="TESTB",
        uncovered_qty=10.0,
        is_short=False,
        db=_db(tmp_path),
        outcome=outcome,
        caller="test",
        market=_Market(),
    )
    assert ok is True, outcome
    assert broker.placed, outcome
    placed = broker.placed[0]["stop_price"]
    assert 0 < placed < 100.0
    assert outcome.get("derived_stop_basis")


def test_recorded_stop_still_wins_over_any_derivation(tmp_path):
    broker = _Broker()
    outcome = {}
    ok = repair_stop_coverage(
        broker=broker,
        last_buy=lambda *a, **k: {"stop_loss": 94.5},
        symbol="TESTB",
        uncovered_qty=10.0,
        is_short=False,
        db=_db(tmp_path),
        outcome=outcome,
        caller="test",
        market=_Market(),
    )
    assert ok is True, outcome
    assert broker.placed[0]["stop_price"] == 94.5
    assert not outcome.get("derived_stop_basis")
