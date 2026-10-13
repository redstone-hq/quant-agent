"""Every computed-vs-analyst target comparison leaves ONE durable row.

The divergence counter used to keep tallies in a module-level dictionary: state
that outlived the run, was shared by parallel runs and was backed by no record.
These pin that the dictionary is gone, that each comparison is a row with the
symbol and the signed gap (no chosen threshold: the 25% warn level was deleted
2026-10-13), and that with no recorder the comparison is logged and nothing is
accumulated.
"""

import logging
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.portfolio_constructor import (
    ConstructorConfig,
    PortfolioConstructor,
    divergence_counter,
)
from src.portfolio_constructor.refusal_recorder import (
    TARGET_DIVERGENCE_OBSERVED,
    TradeRefusalRecorder,
)
from src.storage.db import Database
from src.storage.schema.trade_refusal_tables import ensure_trade_refusal_table
from src.storage.trades import trade_refusals_store


@pytest.fixture()
def db():
    with tempfile.TemporaryDirectory() as d:
        _db = Database(str(Path(d) / "t.db"))
        _db.initialize()
        yield _db


def _derivation(gap):
    return SimpleNamespace(
        price=100.0,
        model_target=100.0 * (1 + gap / 100.0),
        basis="atr",
        divergence_pct=gap,
    )


def test_no_module_level_tally_survives():
    for name in ("observe", "snapshot", "reset", "summary_line", "_counts", "_lock"):
        assert not hasattr(divergence_counter, name), name


def test_each_comparison_is_one_row_with_the_gap_and_no_threshold(db):
    pc = PortfolioConstructor(ConstructorConfig(), recorder=TradeRefusalRecorder(db))
    pc._log_target_divergence("aaa", _derivation(-12.5))
    pc._log_target_divergence("bbb", _derivation(40.0))
    rows = db.get_trade_refusals(refusal=TARGET_DIVERGENCE_OBSERVED)
    assert len(rows) == 2
    by_symbol = {r["symbol"]: r for r in rows}
    assert by_symbol["AAA"]["observed_gap_pct"] == -12.5
    assert by_symbol["BBB"]["observed_gap_pct"] == 40.0
    assert all(r["threshold"] is None for r in rows)
    assert all(r["stage"] == "observed_not_refused" for r in rows)


def test_no_recorder_logs_only_and_counts_nothing(caplog):
    pc = PortfolioConstructor(ConstructorConfig())
    with caplog.at_level(logging.INFO):
        pc._log_target_divergence("aaa", _derivation(40.0))
    assert "+40.0%" in caplog.text
    assert not [n for n in vars(divergence_counter) if "count" in n.lower() and n != "divergence_counter"]


def test_no_chosen_threshold_and_no_warning_level(caplog):
    """The deleted 25% warn level must not come back: no config field, and a
    large gap logs at INFO like any other."""
    assert not hasattr(ConstructorConfig(), "target_divergence_warn_pct")
    pc = PortfolioConstructor(ConstructorConfig())
    with caplog.at_level(logging.INFO):
        pc._log_target_divergence("aaa", _derivation(400.0))
    assert [r.levelno for r in caplog.records if "AAA" in r.getMessage().upper()] == [logging.INFO]


def test_an_unmeasurable_comparison_writes_nothing(db):
    pc = PortfolioConstructor(ConstructorConfig(), recorder=TradeRefusalRecorder(db))
    pc._log_target_divergence(
        "aaa",
        SimpleNamespace(
            price=None,
            model_target=None,
            basis="x",
            divergence_pct=None,
        ),
    )
    assert db.get_trade_refusals(refusal=TARGET_DIVERGENCE_OBSERVED) == []


class _StubLedger:
    """A ledger-shaped stand-in: the store needs `conn`, `_lock` and `_locked_write` only."""

    def __init__(self):
        import sqlite3
        import threading

        self.conn = sqlite3.connect(":memory:")
        self._lock = threading.Lock()
        ensure_trade_refusal_table(conn=self.conn)

    def _locked_write(self, fn, *, label):
        with self._lock:
            return fn()


def test_store_and_schema_stand_alone_without_the_ledger_file():
    ledger = _StubLedger()
    ensure_trade_refusal_table(conn=ledger.conn)  # idempotent on a second call
    rid = trade_refusals_store.insert(
        ledger,
        symbol="aaa",
        direction=None,
        refusal="x",
        stage="observed_not_refused",
        observed_gap_pct=-7.5,
        threshold=25.0,
    )
    assert rid == 1
    rows = trade_refusals_store.get_all(ledger, refusal="x")
    assert rows[0]["symbol"] == "AAA" and rows[0]["observed_gap_pct"] == -7.5


def test_an_old_file_gains_the_gap_column_on_migration():
    import sqlite3

    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE trade_refusals (id INTEGER PRIMARY KEY, timestamp TEXT, "
        "symbol TEXT NOT NULL, refusal TEXT NOT NULL)"
    )
    ensure_trade_refusal_table(conn=conn)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(trade_refusals)")}
    assert {"observed_gap_pct", "requested_risk_pct"} <= cols
