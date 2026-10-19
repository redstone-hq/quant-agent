"""Stop-out reconciliation — 2026-08-28 ONDS/CCJ accounting gap.

Both positions were closed by their broker-resident protective stop (a GTC
stop-limit order `AlpacaBroker.place_entry_protection` places on a fill but
never writes into `trades`). No SELL/exit row was ever recorded, both BUY
rows sat forever at `realized_pnl IS NULL`, and the `positions` table
(synced straight from broker truth) quietly diverged from what `trades`
claimed. Verified against the live paper account on 2026-08-28:

  ONDS: BUY 17 @ 8.53 (2026-08-27 14:31:55 UTC) → stop-limit order
        00000000-0000-4000-8000-0000000000b1 filled 17 @ 7.93
        (2026-08-28 16:16:07 UTC) → realized -$10.20
  CCJ:  BUY 2 @ 107.465 (2026-08-27 13:36:04 UTC) → stop-limit order
        c785ae7e-359d-49fc-9853-0930e879eae5 filled 2 @ 102.955
        (2026-08-28 14:05:17 UTC) → realized -$9.02

`_reconcile_stop_out_fills` (src/pipeline.py) closes the gap by diffing
what the ledger believes it holds (`Database.get_symbols_with_open_ledger_
qty`) against what the broker actually shows, then asking the broker
directly for filled SELL orders the ledger has never recorded
(`AlpacaBroker.list_filled_sell_orders`) and writing them back via
`Database.insert_stop_out_trade`.
"""

import re
import types
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from src.pipeline import TradingPipeline
from tests.pipeline_factory import build_pipeline
from src.storage.db import Database, _trail_stop_reduced_position


def _mk_pipeline(db: Database, broker, lookback_days: int = 7) -> TradingPipeline:
    pipeline = build_pipeline(db=db, broker=broker)
    pipeline.config = types.SimpleNamespace(
        reconciliation=types.SimpleNamespace(stop_out_lookback_days=lookback_days),
    )
    return pipeline


def _stop_order(order_id, symbol, qty, price, filled_at="2026-08-28T16:16:07.476647+00:00"):
    return {
        "id": order_id,
        "symbol": symbol,
        "qty": qty,
        "price": price,
        "filled_at": filled_at,
        "order_type": "stop_limit",
    }


def _filled_buy(db, symbol, qty, price, order_id, run_id="r1", ts=None):
    """Insert a BUY the way production actually produces one: written
    'submitted' at order time, then flipped to 'filled' with REAL fill_qty
    / fill_price via update_trade_fill — exactly what `_reconcile_fills`
    does once the broker confirms the entry. `_realized_pnl_through_trade`
    (unlike `compute_trade_calibration`) has no requested-qty/price
    fallback, so a BUY row needs its fill_qty/fill_price actually populated
    for any exit to price against it — passing fill_status='filled'
    straight to insert_trade alone leaves those NULL and silently starves
    every downstream realized_pnl computation."""
    db.insert_trade(
        symbol, "BUY", qty, price, "entry", run_id, broker_order_id=order_id, fill_status="submitted", stop_loss=90.0
    )
    db.update_trade_fill(broker_order_id=order_id, fill_status="filled", fill_qty=qty, fill_price=price)
    if ts:
        db.conn.execute(
            "UPDATE trades SET timestamp = ? WHERE broker_order_id = ?",
            (ts, order_id),
        )
        db.conn.commit()


# ---------------------------------------------------------------------------
# Database.insert_stop_out_trade — the write-back primitive.
# ---------------------------------------------------------------------------


def test_insert_stop_out_trade_computes_realized_pnl_for_a_loss(tmp_path):
    """ONDS, exact real numbers: BUY 17 @ 8.53, stopped 17 @ 7.93 →
    realized_pnl must be NEGATIVE and equal to -$10.20 (not a guess, not a
    magnitude-only figure — the sign is the whole point of a P&L column)."""
    db = Database(str(tmp_path / "t.db"))
    db.initialize()
    _filled_buy(db, "ONDS", 17, 8.53, "entry-onds")

    row_id, created = db.insert_stop_out_trade(
        symbol="ONDS",
        qty=17.0,
        price=7.93,
        broker_order_id="00000000-0000-4000-8000-0000000000b1",
        filled_at="2026-08-28 16:16:07",
        run_id="r2",
    )
    assert created is True
    assert row_id > 0

    rows = db.get_trades(symbol="ONDS", executed_only=True)
    stop_out = next(r for r in rows if r["action"] == "STOP_OUT")
    assert stop_out["realized_pnl"] == -10.2
    assert stop_out["realized_pnl"] < 0
    assert stop_out["fill_status"] == "filled"
    assert stop_out["fill_qty"] == 17.0
    assert stop_out["fill_price"] == 7.93
    assert stop_out["broker_order_id"] == "00000000-0000-4000-8000-0000000000b1"
    assert stop_out["timestamp"].startswith("2026-08-28 16:16:07")


def test_insert_stop_out_trade_ccj_realized_pnl_exact(tmp_path):
    """CCJ, exact real numbers: BUY 2 @ 107.465, stopped 2 @ 102.955 →
    realized_pnl == -$9.02 exactly."""
    db = Database(str(tmp_path / "t.db"))
    db.initialize()
    _filled_buy(db, "CCJ", 2, 107.465, "entry-ccj")

    db.insert_stop_out_trade(
        symbol="CCJ",
        qty=2.0,
        price=102.955,
        broker_order_id="c785ae7e-359d-49fc-9853-0930e879eae5",
        filled_at="2026-08-28 14:05:17",
        run_id="r2",
    )

    row = next(r for r in db.get_trades(symbol="CCJ", executed_only=True) if r["action"] == "STOP_OUT")
    assert row["realized_pnl"] == -9.02


def test_insert_stop_out_trade_is_idempotent_across_repeated_calls(tmp_path):
    """The reconciler re-runs every session (morning / intra_check / midday
    / close / evening) — the SAME broker order id must produce exactly ONE
    trades row no matter how many times insert_stop_out_trade is called
    for it."""
    db = Database(str(tmp_path / "t.db"))
    db.initialize()
    _filled_buy(db, "ONDS", 17, 8.53, "entry-onds")

    results = [
        db.insert_stop_out_trade(
            symbol="ONDS",
            qty=17.0,
            price=7.93,
            broker_order_id="00000000-0000-4000-8000-0000000000b1",
            filled_at="2026-08-28 16:16:07",
            run_id=f"pass-{i}",
        )
        for i in range(3)
    ]

    row_ids = [r[0] for r in results]
    created_flags = [r[1] for r in results]
    assert row_ids == [row_ids[0]] * 3, "all three calls must resolve to the SAME row"
    assert created_flags == [True, False, False]

    stop_out_rows = [r for r in db.get_trades(symbol="ONDS", executed_only=True) if r["action"] == "STOP_OUT"]
    assert len(stop_out_rows) == 1


def test_insert_stop_out_trade_requires_broker_order_id(tmp_path):
    """A falsy broker_order_id breaks the idempotency key — refuse loudly
    rather than insert a row that could be double-recorded on replay."""
    import pytest

    db = Database(str(tmp_path / "t.db"))
    db.initialize()
    with pytest.raises(ValueError):
        db.insert_stop_out_trade(
            symbol="ONDS",
            qty=17.0,
            price=7.93,
            broker_order_id="",
            filled_at=None,
        )


def test_insert_stop_out_trade_unmatched_pnl_is_null_not_guessed(tmp_path):
    """The ledger only recorded a 10-share BUY, but the broker's stop
    filled 15 (e.g. a corporate action / untracked prior BUY inflated the
    real position). The exit is still RECORDED — never dropped — but
    realized_pnl must stay NULL rather than pricing 5 phantom shares."""
    db = Database(str(tmp_path / "t.db"))
    db.initialize()
    _filled_buy(db, "XYZ", 10, 50.0, "entry-xyz")

    db.insert_stop_out_trade(
        symbol="XYZ",
        qty=15.0,
        price=45.0,
        broker_order_id="stop-xyz",
        filled_at="2026-08-28 10:00:00",
    )

    row = next(r for r in db.get_trades(symbol="XYZ", executed_only=True) if r["action"] == "STOP_OUT")
    assert row["fill_qty"] == 15.0  # the REAL broker fill, recorded as-is
    assert row["realized_pnl"] is None  # not guessed


# ---------------------------------------------------------------------------
# Database.get_symbols_with_open_ledger_qty / get_known_broker_order_ids
# ---------------------------------------------------------------------------


def test_get_symbols_with_open_ledger_qty_nets_buys_and_exits(tmp_path):
    db = Database(str(tmp_path / "t.db"))
    db.initialize()
    db.insert_trade("AAPL", "BUY", 10, 100.0, "x", "r1", broker_order_id="b1", fill_status="filled", stop_loss=90.0)
    db.insert_trade("AAPL", "SELL", 4, 110.0, "x", "r2", broker_order_id="s1", fill_status="filled")
    # A canceled order contributes nothing (never executed).
    db.insert_trade("AAPL", "SELL", 100, 999.0, "x", "r3", broker_order_id="canceled-1", fill_status="canceled")
    db.insert_trade("MSFT", "BUY", 5, 200.0, "x", "r1", fill_status="filled", stop_loss=90.0)

    net = db.get_symbols_with_open_ledger_qty()
    assert net["AAPL"] == 6.0
    assert net["MSFT"] == 5.0


def test_get_known_broker_order_ids_scoped_to_symbol(tmp_path):
    db = Database(str(tmp_path / "t.db"))
    db.initialize()
    db.insert_trade(
        "ONDS", "BUY", 17, 8.53, "x", "r1", broker_order_id="entry-onds", fill_status="filled", stop_loss=90.0
    )
    db.insert_trade(
        "CCJ", "BUY", 2, 107.465, "x", "r1", broker_order_id="entry-ccj", fill_status="filled", stop_loss=90.0
    )

    assert db.get_known_broker_order_ids("ONDS") == {"entry-onds"}
    assert db.get_known_broker_order_ids("CCJ") == {"entry-ccj"}
    assert db.get_known_broker_order_ids("NFLX") == set()


# ---------------------------------------------------------------------------
# TradingPipeline._reconcile_stop_out_fills — the session-level reconciler.
# ---------------------------------------------------------------------------


def test_reconcile_stop_out_fills_records_ondsccj_with_correct_pnl(tmp_path):
    """End-to-end against the two real 2026-08-28 incidents, using the
    ACTUAL order ids / prices / timestamps verified at the broker."""
    db = Database(str(tmp_path / "t.db"))
    db.initialize()
    _filled_buy(db, "ONDS", 17, 8.53, "entry-onds", ts="2026-08-27 14:31:55")
    _filled_buy(db, "CCJ", 2, 107.465, "entry-ccj", ts="2026-08-27 13:36:04")

    broker = MagicMock()
    broker.get_positions.return_value = []  # both flat at the broker

    def _fills(symbol, after):
        if symbol == "ONDS":
            return [
                _stop_order(
                    "00000000-0000-4000-8000-000000000001",
                    "ONDS",
                    17.0,
                    7.93,
                    "2026-08-28T16:16:07.476647+00:00",
                )
            ]
        if symbol == "CCJ":
            return [
                _stop_order(
                    "00000000-0000-4000-8000-000000000002",
                    "CCJ",
                    2.0,
                    102.955,
                    "2026-08-28T14:05:17.636316+00:00",
                )
            ]
        return []

    broker.list_filled_sell_orders.side_effect = _fills

    pipeline = _mk_pipeline(db, broker)
    results = pipeline._reconcile_stop_out_fills(run_id="r-reconcile")

    by_symbol = {r["symbol"]: r for r in results}
    assert by_symbol["ONDS"]["matched"] is True
    assert by_symbol["ONDS"]["recorded"] == 1
    assert by_symbol["CCJ"]["matched"] is True
    assert by_symbol["CCJ"]["recorded"] == 1

    onds = next(r for r in db.get_trades(symbol="ONDS", executed_only=True) if r["action"] == "STOP_OUT")
    ccj = next(r for r in db.get_trades(symbol="CCJ", executed_only=True) if r["action"] == "STOP_OUT")
    assert onds["realized_pnl"] == -10.2
    assert ccj["realized_pnl"] == -9.02
    assert onds["broker_order_id"] == "00000000-0000-4000-8000-000000000001"
    assert ccj["broker_order_id"] == "00000000-0000-4000-8000-000000000002"
    # Backdated to the ACTUAL fill time, not "now" (detection time).
    assert onds["timestamp"].startswith("2026-08-28 16:16:07")
    assert ccj["timestamp"].startswith("2026-08-28 14:05:17")


def test_reconcile_stop_out_fills_written_exactly_once_across_three_passes(tmp_path):
    """The reconciler runs at every session entry point (morning,
    intra_check every ~30 min, midday, close, evening). Simulating three
    separate passes over the SAME unresolved gap must leave exactly one
    STOP_OUT row — not three."""
    db = Database(str(tmp_path / "t.db"))
    db.initialize()
    _filled_buy(db, "ONDS", 17, 8.53, "entry-onds", ts="2026-08-27 14:31:55")

    broker = MagicMock()
    broker.get_positions.return_value = []
    broker.list_filled_sell_orders.return_value = [
        _stop_order("00000000-0000-4000-8000-0000000000b1", "ONDS", 17.0, 7.93),
    ]

    pipeline = _mk_pipeline(db, broker)
    for i in range(3):
        pipeline._reconcile_stop_out_fills(run_id=f"pass-{i}")

    stop_out_rows = [r for r in db.get_trades(symbol="ONDS", executed_only=True) if r["action"] == "STOP_OUT"]
    assert len(stop_out_rows) == 1
    assert stop_out_rows[0]["realized_pnl"] == -10.2
    # After the first pass the ledger and broker agree (both flat) — later
    # passes must not even re-query the broker for a symbol with no gap.
    assert broker.list_filled_sell_orders.call_count == 1


def test_reconcile_stop_out_fills_flags_unresolved_gap_without_guessing(tmp_path):
    """Ledger believes ONDS still has 17 sh open, broker shows 0, but the
    broker's own order history has NOTHING that explains it (e.g. outside
    the lookback window, or a genuine anomaly). Must NOT invent a trades
    row — only flag, loudly, for manual review."""
    db = Database(str(tmp_path / "t.db"))
    db.initialize()
    db.insert_trade("ONDS", "BUY", 17, 8.53, "entry", "r1", fill_status="filled", stop_loss=90.0)

    broker = MagicMock()
    broker.get_positions.return_value = []
    broker.list_filled_sell_orders.return_value = []  # broker found nothing

    pipeline = _mk_pipeline(db, broker)
    results = pipeline._reconcile_stop_out_fills(run_id="r1")

    assert results == [
        {
            "symbol": "ONDS",
            "ledger_qty": 17.0,
            "broker_qty": 0.0,
            "matched": False,
            "recorded": 0,
        }
    ]
    # Nothing fabricated in the ledger.
    stop_out_rows = [r for r in db.get_trades(symbol="ONDS", executed_only=True) if r["action"] == "STOP_OUT"]
    assert stop_out_rows == []
    # But the anomaly is NOT silently dropped — it's visible in evidence.
    event = db.conn.execute(
        "SELECT evidence_json FROM specialist_evidence WHERE run_id='r1' AND symbol='ONDS' AND kind='pipeline_event'"
    ).fetchone()
    assert event is not None
    assert '"outcome": "stop_out_gap_unexplained"' in event["evidence_json"]


def test_reconcile_stop_out_fills_refuses_a_sale_larger_than_the_gap(tmp_path):
    """The broker fill IS found, but it is larger than the gap it would
    close (15 sold vs 10 missing). Since 2026-10-19 only whole orders that
    close the gap exactly are written back; this one is NOT written, and the
    owner is paged instead of the ledger carrying an unpriceable row."""
    db = Database(str(tmp_path / "t.db"))
    db.initialize()
    _filled_buy(db, "XYZ", 10, 50.0, "entry-xyz", ts="2026-08-27 14:31:55")

    broker = MagicMock()
    broker.get_positions.return_value = []  # ledger sees 10 open, broker sees 0 → gap
    broker.list_filled_sell_orders.return_value = [
        _stop_order("stop-xyz", "XYZ", 15.0, 45.0),  # broker actually sold 15
    ]

    pipeline = _mk_pipeline(db, broker)
    results = pipeline._reconcile_stop_out_fills(run_id="r1")

    assert results == [
        {
            "symbol": "XYZ",
            "ledger_qty": 10.0,
            "broker_qty": 0.0,
            "matched": False,
            "recorded": 0,
        }
    ]
    assert not [r for r in db.get_trades(symbol="XYZ") if r["action"] == "STOP_OUT"]

    event = db.conn.execute(
        "SELECT evidence_json FROM specialist_evidence WHERE run_id='r1' AND symbol='XYZ' AND kind='pipeline_event'"
    ).fetchone()
    assert event is not None
    assert '"outcome": "stop_out_gap_no_exact_fit"' in event["evidence_json"]


def test_reconcile_stop_out_fills_no_gap_is_a_no_op(tmp_path):
    """The common case: the broker still holds what the ledger expects.
    No broker order query, no writes, no flags."""
    db = Database(str(tmp_path / "t.db"))
    db.initialize()
    db.insert_trade("AAPL", "BUY", 10, 180.0, "entry", "r1", fill_status="filled", stop_loss=90.0)

    broker = MagicMock()
    from src.models import Position

    broker.get_positions.return_value = [
        Position(
            symbol="AAPL",
            qty=10,
            avg_entry=180.0,
            current_price=185.0,
            market_value=1850.0,
            unrealized_pnl=50.0,
            sector="Tech",
        ),
    ]

    pipeline = _mk_pipeline(db, broker)
    results = pipeline._reconcile_stop_out_fills(run_id="r1")

    assert results == []
    broker.list_filled_sell_orders.assert_not_called()


def test_reconcile_stop_out_fills_broker_positions_query_failure_is_non_fatal(tmp_path):
    """A broker outage during reconciliation must not raise — leave the
    gap for the next pass."""
    db = Database(str(tmp_path / "t.db"))
    db.initialize()
    db.insert_trade("ONDS", "BUY", 17, 8.53, "entry", "r1", fill_status="filled", stop_loss=90.0)

    broker = MagicMock()
    broker.get_positions.side_effect = RuntimeError("alpaca 503")

    pipeline = _mk_pipeline(db, broker)
    assert pipeline._reconcile_stop_out_fills(run_id="r1") == []


def test_reconcile_stop_out_fills_broker_fill_query_none_leaves_gap_for_next_pass(tmp_path):
    """list_filled_sell_orders returning None means the QUERY FAILED, not
    'no fills' — must retry next time, not flag a false anomaly."""
    db = Database(str(tmp_path / "t.db"))
    db.initialize()
    db.insert_trade("ONDS", "BUY", 17, 8.53, "entry", "r1", fill_status="filled", stop_loss=90.0)

    broker = MagicMock()
    broker.get_positions.return_value = []
    broker.list_filled_sell_orders.return_value = None

    pipeline = _mk_pipeline(db, broker)
    results = pipeline._reconcile_stop_out_fills(run_id="r1")

    assert results == []
    stop_out_rows = [r for r in db.get_trades(symbol="ONDS", executed_only=True) if r["action"] == "STOP_OUT"]
    assert stop_out_rows == []


def test_reconcile_stop_out_fills_noop_without_config(tmp_path):
    """A pipeline with no `.config` (unit-test double, or a settings.yaml
    that somehow predates ReconciliationConfig's default_factory) must
    bail out cleanly rather than raising — mirrors _force_delever's same
    defensive pattern."""
    db = Database(str(tmp_path / "t.db"))
    db.initialize()
    db.insert_trade("ONDS", "BUY", 17, 8.53, "entry", "r1", fill_status="filled", stop_loss=90.0)

    pipeline = build_pipeline(db=db, broker=MagicMock())
    del pipeline.config  # the real constructor always sets it; force the defensive branch

    assert pipeline._reconcile_stop_out_fills(run_id="r1") == []
    pipeline.broker.get_positions.assert_not_called()


# ---------------------------------------------------------------------------
# item 173(a): the recovered exit must be labelled by what the broker fill
# ACTUALLY was, never a blanket STOP_OUT. `list_filled_sell_orders` already
# reports each fill's order_type; the reconciler must thread it through so a
# market/limit sell is not misattributed to a protective stop, and a fill
# whose type does not prove it was a stop is recorded as an honest,
# unattributed exit rather than a stop it cannot substantiate.
# ---------------------------------------------------------------------------

from src.pipeline import _reconciled_exit_action  # noqa: E402


def _sell_order(order_id, symbol, qty, price, order_type, filled_at="2026-09-21T15:00:00+00:00"):
    return {
        "id": order_id,
        "symbol": symbol,
        "qty": qty,
        "price": price,
        "filled_at": filled_at,
        "order_type": order_type,
    }


@pytest.mark.parametrize(
    "order_type,expected",
    [
        ("stop", "STOP_OUT"),
        ("stop_limit", "STOP_OUT"),
        ("trailing_stop", "STOP_OUT"),
        ("OrderType.STOP", "STOP_OUT"),
        ("market", "SELL"),
        ("limit", "SELL"),
        ("OrderType.MARKET", "SELL"),
        (None, "RECONCILED_EXIT"),
        ("", "RECONCILED_EXIT"),
        ("something_new", "RECONCILED_EXIT"),
    ],
)
def test_reconciled_exit_action_maps_order_type_honestly(order_type, expected):
    """The pure mapping — genuine stops to STOP_OUT, plain sells to SELL,
    everything else to the unattributed marker, NEVER a guessed STOP_OUT."""
    assert _reconciled_exit_action(order_type) == expected


def test_reconcile_records_stop_fill_as_stop_out(tmp_path):
    """A recovered fill the broker reports as a stop-limit is a genuine
    protective stop — labelled STOP_OUT, exit_reason_category broker_stop_fill."""
    db = Database(str(tmp_path / "t.db"))
    db.initialize()
    _filled_buy(db, "ONDS", 17, 8.53, "entry-onds", ts="2026-09-20 14:00:00")

    broker = MagicMock()
    broker.get_positions.return_value = []
    broker.list_filled_sell_orders.return_value = [
        _sell_order("stop-onds", "ONDS", 17.0, 7.93, "stop_limit"),
    ]

    pipeline = _mk_pipeline(db, broker)
    results = pipeline._reconcile_stop_out_fills(run_id="r1")
    assert results[0]["recorded"] == 1

    rows = db.get_trades(symbol="ONDS", executed_only=True)
    row = next(r for r in rows if r["broker_order_id"] == "stop-onds")
    assert row["action"] == "STOP_OUT"
    assert row["exit_reason_category"] == "broker_stop_fill"
    assert not any(r["action"] in ("SELL", "RECONCILED_EXIT") for r in rows)


def test_reconcile_records_market_sell_as_sell_not_stop_out(tmp_path):
    """EQNR-style motivating case: the recovered fill was a MARKET sell, not
    a protective stop. It must be recorded as SELL — never STOP_OUT — so
    owner-facing P&L attribution does not invent a protective stop."""
    db = Database(str(tmp_path / "t.db"))
    db.initialize()
    _filled_buy(db, "EQNR", 8.5962, 25.0, "entry-eqnr", ts="2026-09-20 14:00:00")

    broker = MagicMock()
    broker.get_positions.return_value = []
    broker.list_filled_sell_orders.return_value = [
        _sell_order("mkt-eqnr", "EQNR", 8.5962, 24.0, "market"),
    ]

    pipeline = _mk_pipeline(db, broker)
    results = pipeline._reconcile_stop_out_fills(run_id="r1")
    assert results[0]["recorded"] == 1

    rows = db.get_trades(symbol="EQNR", executed_only=True)
    row = next(r for r in rows if r["broker_order_id"] == "mkt-eqnr")
    assert row["action"] == "SELL"
    assert row["action"] != "STOP_OUT"
    # A plain reconciled sell carries no protective-stop category.
    assert row["exit_reason_category"] != "broker_stop_fill"


def test_reconcile_records_missing_order_type_as_unattributed_not_stop_out(tmp_path):
    """A fill whose order_type the broker did not report must NOT be guessed
    a protective stop. It is recorded as an honest, distinct unattributed
    exit — never STOP_OUT — so attribution never claims a stop it can't prove."""
    db = Database(str(tmp_path / "t.db"))
    db.initialize()
    _filled_buy(db, "AMD", 2.0, 549.11, "entry-amd", ts="2026-09-20 14:00:00")

    broker = MagicMock()
    broker.get_positions.return_value = []
    broker.list_filled_sell_orders.return_value = [
        {"id": "ambiguous-amd", "qty": 2.0, "price": 500.0, "filled_at": "2026-09-22 14:00:00"},  # no order_type at all
    ]

    pipeline = _mk_pipeline(db, broker)
    results = pipeline._reconcile_stop_out_fills(run_id="r1")
    assert results[0]["recorded"] == 1

    rows = db.get_trades(symbol="AMD", executed_only=True)
    row = next(r for r in rows if r["broker_order_id"] == "ambiguous-amd")
    assert row["action"] == "RECONCILED_EXIT"
    assert row["action"] != "STOP_OUT"
    assert row["exit_reason_category"] == "reconciled_unattributed_exit"
    # The share-count ledger still sees the exit (row written, book matches).
    assert db.get_symbols_with_open_ledger_qty().get("AMD", 0.0) == 0.0


# ---------------------------------------------------------------------------
# Existing system-initiated exits must be UNCHANGED by this fix — hard
# literals, not just "still passes". SELL / REDUCE / TRAIL_STOP / SWEEP_SELL
# already had a working write-back path (insert_trade at submission +
# update_trade_fill at reconciliation); adding STOP_OUT to the SELL-family
# tuple in compute_trade_calibration and to _EXIT_AUDIT_ACTIONS must not
# change what those four compute.
# ---------------------------------------------------------------------------


def test_existing_sell_realized_pnl_unchanged(tmp_path):
    db = Database(str(tmp_path / "t.db"))
    db.initialize()
    _filled_buy(db, "AAPL", 10, 100.0, "b1")
    db.insert_trade("AAPL", "SELL", 10, 0, "x", "r2", broker_order_id="s1", fill_status="submitted")
    db.update_trade_fill(broker_order_id="s1", fill_status="filled", fill_qty=10.0, fill_price=110.0)

    row = next(r for r in db.get_trades(symbol="AAPL", executed_only=True) if r["action"] == "SELL")
    assert row["realized_pnl"] == 100.0


def test_existing_reduce_realized_pnl_unchanged(tmp_path):
    db = Database(str(tmp_path / "t.db"))
    db.initialize()
    _filled_buy(db, "MSFT", 5, 200.0, "b2")
    db.insert_trade("MSFT", "REDUCE", 5, 0, "x", "r2", broker_order_id="red1", fill_status="submitted")
    db.update_trade_fill(broker_order_id="red1", fill_status="filled", fill_qty=5.0, fill_price=220.0)

    row = next(r for r in db.get_trades(symbol="MSFT", executed_only=True) if r["action"] == "REDUCE")
    assert row["realized_pnl"] == 100.0


def test_existing_filled_trail_stop_realized_pnl_unchanged(tmp_path):
    db = Database(str(tmp_path / "t.db"))
    db.initialize()
    _filled_buy(db, "LLY", 8, 50.0, "b3")
    db.insert_trade("LLY", "TRAIL_STOP", 8, 45.0, "x", "r2", broker_order_id="trail1", fill_status="submitted")
    db.update_trade_fill(broker_order_id="trail1", fill_status="filled", fill_qty=8.0, fill_price=45.0)

    row = next(r for r in db.get_trades(symbol="LLY", executed_only=True) if r["action"] == "TRAIL_STOP")
    assert row["realized_pnl"] == -40.0


def test_existing_sweep_sell_realized_pnl_unchanged(tmp_path):
    db = Database(str(tmp_path / "t.db"))
    db.initialize()
    db.insert_trade("SGOV", "SWEEP_BUY", 100, 100.0, "x", "r1", broker_order_id="sb1", fill_status="submitted")
    db.update_trade_fill(broker_order_id="sb1", fill_status="filled", fill_qty=100.0, fill_price=100.0)
    db.insert_trade("SGOV", "SWEEP_SELL", 100, 0, "x", "r2", broker_order_id="ss1", fill_status="submitted")
    db.update_trade_fill(broker_order_id="ss1", fill_status="filled", fill_qty=100.0, fill_price=100.02)

    row = next(r for r in db.get_trades(symbol="SGOV", executed_only=True) if r["action"] == "SWEEP_SELL")
    assert row["realized_pnl"] == 2.0


def test_compute_trade_calibration_counts_stop_out_as_a_closed_trade(tmp_path):
    """Before this fix, compute_trade_calibration had NO action name that
    represented 'the broker's stop fired' — a STOP_OUT row, even once
    written, would have been silently excluded from win_rate / avg_return
    exactly like the pre-2026-07-16 TRAIL_STOP gap. This pins the fix."""
    db = Database(str(tmp_path / "t.db"))
    db.initialize()

    # Three closed pairs needed to cross compute_trade_calibration's n>=3
    # reporting threshold. Two ordinary SELLs plus one STOP_OUT loss.
    db.insert_trade("AAA", "BUY", 10, 100.0, "x", "r1", broker_order_id="b1", fill_status="filled", stop_loss=90.0)
    db.conn.execute(
        "UPDATE trades SET timestamp = datetime('2026-10-25 14:00:00', '-10 days') WHERE broker_order_id='b1'"
    )
    db.insert_trade("AAA", "SELL", 10, 110.0, "x", "r2", broker_order_id="s1", fill_status="filled")
    db.conn.execute(
        "UPDATE trades SET timestamp = datetime('2026-10-25 14:00:00', '-9 days') WHERE broker_order_id='s1'"
    )

    db.insert_trade("BBB", "BUY", 10, 100.0, "x", "r1", broker_order_id="b2", fill_status="filled", stop_loss=90.0)
    db.conn.execute(
        "UPDATE trades SET timestamp = datetime('2026-10-25 14:00:00', '-8 days') WHERE broker_order_id='b2'"
    )
    db.insert_trade("BBB", "SELL", 10, 110.0, "x", "r2", broker_order_id="s2", fill_status="filled")
    db.conn.execute(
        "UPDATE trades SET timestamp = datetime('2026-10-25 14:00:00', '-7 days') WHERE broker_order_id='s2'"
    )

    db.insert_trade(
        "ONDS", "BUY", 17, 8.53, "x", "r1", broker_order_id="entry-onds", fill_status="filled", stop_loss=90.0
    )
    db.conn.execute("UPDATE trades SET timestamp = '2026-10-27 14:31:55' WHERE broker_order_id='entry-onds'")
    db.insert_stop_out_trade(
        symbol="ONDS",
        qty=17.0,
        price=7.93,
        broker_order_id="00000000-0000-4000-8000-0000000000b1",
        filled_at="2026-10-28 16:16:07",
    )
    db.conn.commit()

    stats = db.compute_trade_calibration(lookback_days=3650)
    assert stats["n"] == 3
    # 2 wins (AAA, BBB) out of 3 closed trades — the STOP_OUT loss counts.
    assert stats["win_rate_pct"] == round(2 / 3 * 100, 1)


# ---------------------------------------------------------------------------
# TRAIL_STOP is protection, not a sale — live corruption found 2026-09-23.
#
# `get_symbols_with_open_ledger_qty` signed every non-BUY/non-HOLD executed
# row -1, so a TRAIL_STOP row — written at PLACEMENT by the stop-placement
# paths, and left at `fill_status IS NULL` on the legacy rows that
# `_executed_trade_predicate` nevertheless treats as executed — subtracted
# the whole protected position from the ledger's own belief.
#
# Measured against the production DB on 2026-09-23 (read-only): three such
# rows existed (COP id 11, EQNR id 12, AMD id 50; all fill_status NULL,
# fill_qty NULL, broker_order_id NULL) and the ledger read AMD 0.0 while
# the broker held 1.7662, COP -5.3194 and EQNR -8.5962 while both were
# flat. AMD is the dangerous one: `_reconcile_stop_out_fills` skips any
# symbol whose ledger qty is <= 0, so an untracked broker sale on that
# symbol was undetectable by construction.
#
# These probe the CLASS — what a TRAIL_STOP in each fill state does to a
# pure share-count ledger, long side and short side, next to the other
# actions the signing rule enumerates.
# ---------------------------------------------------------------------------


def _rest_the_stops(db: Database, *, clear_order_id: bool = False):
    """Force every TRAIL_STOP row into the production legacy shape:
    fill_status NULL and fill_qty NULL. `insert_trade` cannot express this
    together with a broker_order_id, and it is exactly the shape the three
    live rows carry."""
    extra = ", broker_order_id = NULL" if clear_order_id else ""
    db.conn.execute(f"UPDATE trades SET fill_status = NULL, fill_qty = NULL{extra} WHERE action = 'TRAIL_STOP'")
    db.conn.commit()


def _set_fill(db: Database, broker_order_id: str, *, status=None, qty=None):
    """Force one row into a fill state `insert_trade` cannot express (it
    takes no fill_qty) — a legacy NULL status carrying a real executed
    quantity, or a partial fill that was then canceled."""
    db.conn.execute(
        "UPDATE trades SET fill_status = ?, fill_qty = ? WHERE broker_order_id = ?", (status, qty, broker_order_id)
    )
    db.conn.commit()


def test_unfilled_trail_stop_with_null_fill_status_is_not_an_exit(tmp_path):
    """THE LIVE DEFECT (AMD id 50): a resting stop must not zero the book."""
    db = Database(str(tmp_path / "t.db"))
    db.initialize()
    db.insert_trade(
        "AMD", "BUY", 1.7662, 549.11, "entry", "r1", broker_order_id="amd-buy", fill_status="filled", stop_loss=90.0
    )
    db.insert_trade("AMD", "TRAIL_STOP", 1.7662, 549.11, "protect", "r1")
    _rest_the_stops(db, clear_order_id=True)

    # Precondition: the row really is one the executed predicate admits —
    # otherwise this test would pass for the wrong reason.
    admitted = db.conn.execute(
        "SELECT COUNT(*) FROM trades WHERE action = 'TRAIL_STOP' AND " + Database._executed_trade_predicate()
    ).fetchone()[0]
    assert admitted == 1

    assert db.get_symbols_with_open_ledger_qty()["AMD"] == 1.7662


def test_resting_trail_stop_does_not_drive_a_flat_symbol_negative(tmp_path):
    """COP id 11: entry, resting stop, then the real sale that flattened
    the book. The stop must not double-count that exit."""
    db = Database(str(tmp_path / "t.db"))
    db.initialize()
    db.insert_trade(
        "COP", "BUY", 5.3194, 100.0, "entry", "r1", broker_order_id="cop-buy", fill_status="filled", stop_loss=90.0
    )
    db.insert_trade("COP", "TRAIL_STOP", 5.3194, 95.0, "protect", "r1")
    _rest_the_stops(db, clear_order_id=True)
    db.insert_trade("COP", "SELL", 5.3194, 99.0, "exit", "r2", broker_order_id="cop-sell", fill_status="filled")
    _set_fill(db, "cop-sell", status="filled", qty=5.3194)

    assert db.get_symbols_with_open_ledger_qty()["COP"] == 0.0


def test_filled_trail_stop_is_an_exit(tmp_path):
    """The stop actually sold the shares: it MUST still subtract them."""
    db = Database(str(tmp_path / "t.db"))
    db.initialize()
    db.insert_trade(
        "LLY", "BUY", 8, 900.0, "entry", "r1", broker_order_id="lly-buy", fill_status="filled", stop_loss=90.0
    )
    db.insert_trade("LLY", "TRAIL_STOP", 8, 850.0, "protect", "r1", broker_order_id="lly-stop", fill_status="submitted")
    _set_fill(db, "lly-stop", status="filled", qty=8.0)

    assert db.get_symbols_with_open_ledger_qty()["LLY"] == 0.0


#: Every terminal-failure fill_status `_reconcile_fills` can write, and the
#: pre-terminal ones it leaves alone. DERIVED from the writer below rather
#: than typed out here: an earlier hand-written list silently omitted
#: `done_for_day`, `rejected` and the two-L `cancelled`, which is the same
#: two-copies-of-a-contract drift these tests exist to catch.
_TERMINAL_FAIL_STATUSES = ("canceled", "cancelled", "expired", "rejected", "done_for_day")
_PRE_TERMINAL_STATUSES = ("submitted", "pending_submit")


def test_terminal_status_list_still_matches_the_only_writer():
    """The mechanical half of the derivation. `_reconcile_fills` in
    src/protection/fill_reconciler.py is the only thing that writes a terminal fill_status,
    and it stores the broker's string verbatim. If that set ever gains or
    loses a status, the parametrized tests below must follow it, so this
    fails rather than letting them quietly stop covering a real state."""
    source = (Path(__file__).resolve().parents[1] / "src" / "protection" / "fill_reconciler.py").read_text()
    match = re.search(r"terminal_fail\s*=\s*\{([^}]*)\}", source)
    assert match, "could not find terminal_fail in src/protection/fill_reconciler.py"
    written = {s.strip().strip("\"'") for s in match.group(1).split(",") if s.strip()}
    assert written == set(_TERMINAL_FAIL_STATUSES), (
        "src/protection/fill_reconciler.py's terminal_fail set has changed; update "
        "_TERMINAL_FAIL_STATUSES and the tests parametrized over it"
    )


@pytest.mark.parametrize("status", _PRE_TERMINAL_STATUSES + _TERMINAL_FAIL_STATUSES)
def test_non_trading_trail_stop_statuses_are_not_exits(tmp_path, status):
    """A stop that rests, is pulled, lapses, is rejected or is closed out
    at the end of the day WITHOUT trading moves no shares — asserted TWICE
    on purpose.

    The end-to-end number is currently decided by the SQL predicate, which
    admits none of these rows at all (measured: 0 rows) so the Python
    branch never runs. That makes the end-to-end assertion alone vacuous —
    it would still pass if the Python rule were inverted. The SQL and
    Python copies of this contract have drifted before, so the Python rule
    is pinned directly as well."""
    db = Database(str(tmp_path / "t.db"))
    db.initialize()
    db.insert_trade(
        "NVDA", "BUY", 10, 500.0, "entry", "r1", broker_order_id="nv-buy", fill_status="filled", stop_loss=90.0
    )
    db.insert_trade("NVDA", "TRAIL_STOP", 10, 450.0, "protect", "r1", broker_order_id="nv-stop", fill_status=status)

    assert db.get_symbols_with_open_ledger_qty()["NVDA"] == 10.0
    row = db.conn.execute("SELECT fill_qty, fill_status FROM trades WHERE broker_order_id = 'nv-stop'").fetchone()
    assert _trail_stop_reduced_position(row, "TRAIL_STOP") is False


@pytest.mark.parametrize("status", _TERMINAL_FAIL_STATUSES)
def test_every_terminal_status_with_a_partial_fill_still_subtracts(tmp_path, status):
    """The case the wide rule exists for, across EVERY terminal status the
    writer can produce — not just the one that was typed out by hand.

    `_reconcile_fills` stores the broker's terminal status verbatim
    alongside whatever quantity did trade, and logs that combination
    explicitly. `done_for_day` with a partial fill is the ordinary
    real-world instance: a day order that traded part of its size and then
    lapsed at the close. Those shares are gone from the broker's book even
    though there is no priceable round trip, so the share-count ledger has
    to subtract them."""
    db = Database(str(tmp_path / "t.db"))
    db.initialize()
    db.insert_trade(
        "TGT", "BUY", 30, 80.0, "entry", "r1", broker_order_id="tgt-buy", fill_status="filled", stop_loss=90.0
    )
    db.insert_trade("TGT", "TRAIL_STOP", 30, 75.0, "protect", "r1", broker_order_id="tgt-stop", fill_status="submitted")
    _set_fill(db, "tgt-stop", status=status, qty=11.0)

    assert db.get_symbols_with_open_ledger_qty()["TGT"] == 19.0


@pytest.mark.parametrize("action", ["TRAIL_STOP", "trail_stop", "Trail_Stop"])
def test_trail_stop_rule_is_case_insensitive_on_the_action(action):
    """The guard upper-cases before comparing. Every caller happens to pass
    an already-upper-cased action today, so nothing else would notice if
    that normalisation were dropped."""
    filled = {"fill_qty": 4.0, "fill_status": "filled"}
    assert _trail_stop_reduced_position(filled, action) is True


def test_trail_stop_rule_does_not_answer_for_other_actions(tmp_path):
    """The helper is named for TRAIL_STOP and must say so. Without the
    action check it returned True for any row carrying a fill_qty, which
    would quietly hand a true-by-default answer to a future caller."""
    filled_sell = {"fill_qty": 9.0, "fill_status": "filled"}
    assert _trail_stop_reduced_position(filled_sell, "SELL") is False
    assert _trail_stop_reduced_position(filled_sell, "TRAIL_STOP") is True


def test_trail_stop_with_fill_qty_but_null_status_is_an_exit(tmp_path):
    """Legacy shape: no fill_status ever written back, but the broker's
    executed quantity WAS recorded. Those shares are gone."""
    db = Database(str(tmp_path / "t.db"))
    db.initialize()
    db.insert_trade(
        "KO", "BUY", 20, 60.0, "entry", "r1", broker_order_id="ko-buy", fill_status="filled", stop_loss=90.0
    )
    db.insert_trade("KO", "TRAIL_STOP", 20, 55.0, "protect", "r1", broker_order_id="ko-stop")
    _set_fill(db, "ko-stop", status=None, qty=20.0)

    assert db.get_symbols_with_open_ledger_qty()["KO"] == 0.0


def test_partially_filled_trail_stop_subtracts_only_what_traded(tmp_path):
    """Part of the protected size traded. The ledger must lose exactly
    that part — not the whole position, and not nothing.

    Uses the legacy NULL-status shape rather than a `partially_filled`
    status string: `_reconcile_fills` normalises anything filled to
    `'filled'` and stores only its own terminal vocabulary otherwise, so
    `partially_filled` is a state this desk never writes and a test using
    it would exercise the right branch under a name that cannot occur."""
    db = Database(str(tmp_path / "t.db"))
    db.initialize()
    db.insert_trade(
        "PEP", "BUY", 30, 170.0, "entry", "r1", broker_order_id="pep-buy", fill_status="filled", stop_loss=90.0
    )
    db.insert_trade("PEP", "TRAIL_STOP", 30, 160.0, "protect", "r1", broker_order_id="pep-stop")
    _set_fill(db, "pep-stop", status=None, qty=12.0)

    assert db.get_symbols_with_open_ledger_qty()["PEP"] == 18.0


def test_resting_stop_alongside_a_real_partial_sale(tmp_path):
    """Both at once (EQNR id 12): a REDUCE that really traded, and
    protection still resting over the remainder. Only the REDUCE may move
    the number."""
    db = Database(str(tmp_path / "t.db"))
    db.initialize()
    db.insert_trade(
        "EQNR", "BUY", 17.1924, 25.0, "entry", "r1", broker_order_id="eqnr-buy", fill_status="filled", stop_loss=90.0
    )
    db.insert_trade("EQNR", "TRAIL_STOP", 17.1924, 23.0, "protect", "r1")
    _rest_the_stops(db, clear_order_id=True)
    db.insert_trade("EQNR", "REDUCE", 8.5962, 24.0, "trim", "r2", broker_order_id="eqnr-reduce", fill_status="filled")
    _set_fill(db, "eqnr-reduce", status="filled", qty=8.5962)

    assert db.get_symbols_with_open_ledger_qty()["EQNR"] == 8.5962


def test_short_position_resting_stop_does_not_move_the_count(tmp_path):
    """Short side. A SHORT signs negative and its protective stop is a
    BUY-to-cover resting at the broker — still protection, still no
    quantity effect. The caller reads negatives as shorts and skips them,
    so a wrong number here would be silent."""
    db = Database(str(tmp_path / "t.db"))
    db.initialize()
    db.insert_trade(
        "FLNC",
        "SHORT",
        36,
        7.39,
        "short entry",
        "r1",
        broker_order_id="flnc-short",
        fill_status="filled",
        stop_loss=110.0,
    )
    db.insert_trade("FLNC", "TRAIL_STOP", 36, 8.2, "protect", "r1")
    _rest_the_stops(db, clear_order_id=True)

    assert db.get_symbols_with_open_ledger_qty()["FLNC"] == -36.0


def test_other_enumerated_actions_keep_their_existing_signs(tmp_path):
    """Guard the rest of the signing rule against collateral damage:
    BUY/SWEEP_BUY add, SWEEP_SELL/STOP_OUT/REDUCE subtract, and HOLD plus a
    never-sent submit_failed row contribute nothing."""
    db = Database(str(tmp_path / "t.db"))
    db.initialize()
    db.insert_trade("SGOV", "SWEEP_BUY", 65, 100.0, "park", "r1", broker_order_id="sw-b", fill_status="filled")
    db.insert_trade("SGOV", "SWEEP_SELL", 25, 100.1, "release", "r2", broker_order_id="sw-s", fill_status="filled")
    db.insert_trade(
        "OXY", "BUY", 31.3451, 45.0, "entry", "r1", broker_order_id="oxy-buy", fill_status="filled", stop_loss=90.0
    )
    db.insert_trade(
        "OXY", "STOP_OUT", 31.3451, 41.0, "stopped out", "r2", broker_order_id="oxy-stop", fill_status="filled"
    )
    db.insert_trade(
        "XOM", "BUY", 10, 110.0, "entry", "r1", broker_order_id="xom-buy", fill_status="filled", stop_loss=90.0
    )
    db.insert_trade("XOM", "REDUCE", 4, 112.0, "trim", "r2", broker_order_id="xom-red", fill_status="filled")
    db.insert_trade("XOM", "HOLD", 0, 0, "hold", "r2")
    db.insert_trade(
        "XOM",
        "BUY",
        99,
        110.0,
        "never sent",
        "r2",
        broker_order_id="xom-fail",
        fill_status="submit_failed",
        stop_loss=90.0,
    )

    net = db.get_symbols_with_open_ledger_qty()
    assert net["SGOV"] == 40.0
    assert net["OXY"] == 0.0
    assert net["XOM"] == 6.0


def test_reconciler_now_sees_a_stop_out_masked_by_a_resting_stop(tmp_path):
    """End-to-end consequence of the live defect. AMD's resting stop made
    the ledger read 0, and `_reconcile_stop_out_fills` skips any symbol it
    believes is flat — so an untracked broker sale there was undetectable
    by construction. With the fix the gap is seen and written back."""
    db = Database(str(tmp_path / "t.db"))
    db.initialize()
    db.insert_trade(
        "AMD", "BUY", 2.0, 549.11, "entry", "r1", broker_order_id="amd-buy", fill_status="filled", stop_loss=90.0
    )
    db.insert_trade("AMD", "TRAIL_STOP", 2.0, 500.0, "protect", "r1")
    db.conn.execute("UPDATE trades SET timestamp = '2026-09-20 14:00:00' WHERE broker_order_id = 'amd-buy'")
    db.conn.commit()
    _rest_the_stops(db, clear_order_id=True)

    broker = MagicMock()
    broker.get_positions.return_value = []  # broker flat: the stop fired
    broker.list_filled_sell_orders.return_value = [
        {
            "id": "untracked-stop-fill",
            "qty": 2.0,
            "price": 500.0,
            "filled_at": "2026-09-22 14:00:00",
            "order_type": "stop_limit",
        },
    ]

    pipeline = _mk_pipeline(db, broker)
    results = pipeline._reconcile_stop_out_fills(run_id="r9")

    assert [r["symbol"] for r in results] == ["AMD"]
    assert results[0]["ledger_qty"] == 2.0
    assert results[0]["recorded"] == 1
    assert any(r["action"] == "STOP_OUT" for r in db.get_trades(symbol="AMD"))


def test_reconciler_stays_a_no_op_on_a_genuinely_flat_symbol(tmp_path):
    """The mirror case: COP went NEGATIVE, which the caller also skips, so
    the defect was invisible from the reconciler's results either way.
    After the fix the number is a true 0, the pass is still a no-op, and
    no broker query or owner page happens."""
    db = Database(str(tmp_path / "t.db"))
    db.initialize()
    db.insert_trade(
        "COP", "BUY", 5.3194, 100.0, "entry", "r1", broker_order_id="cop-buy", fill_status="filled", stop_loss=90.0
    )
    db.insert_trade("COP", "TRAIL_STOP", 5.3194, 95.0, "protect", "r1")
    _rest_the_stops(db, clear_order_id=True)
    db.insert_trade("COP", "SELL", 5.3194, 99.0, "exit", "r2", broker_order_id="cop-sell", fill_status="filled")
    _set_fill(db, "cop-sell", status="filled", qty=5.3194)

    broker = MagicMock()
    broker.get_positions.return_value = []

    pipeline = _mk_pipeline(db, broker)
    assert pipeline._reconcile_stop_out_fills(run_id="r9") == []
    broker.list_filled_sell_orders.assert_not_called()


def test_calibration_and_ledger_qty_agree_on_trail_stop_fill_state(tmp_path):
    """Cross-check the two accountings that read the same rows. A
    TRAIL_STOP that closes a lot in `compute_trade_calibration` must also
    be one that removes shares here, and one the calibration leaves open
    must still be counted as held. Drift between them is what produced
    this defect in the first place."""
    db = Database(str(tmp_path / "t.db"))
    db.initialize()
    # Three symbols whose protective stop really fired...
    for sym in ("AAA", "CCC", "DDD"):
        db.insert_trade(
            sym, "BUY", 10, 100.0, "entry", "r1", broker_order_id=f"{sym}-buy", fill_status="filled", stop_loss=90.0
        )
        db.insert_trade(
            sym, "TRAIL_STOP", 10, 90.0, "protect", "r1", broker_order_id=f"{sym}-stop", fill_status="submitted"
        )
        db.conn.execute(
            "UPDATE trades SET fill_price = 90.0, fill_qty = 10.0, fill_status = 'filled' WHERE broker_order_id = ?",
            (f"{sym}-stop",),
        )
    # ...and one whose stop is still resting, in the production row shape.
    db.insert_trade(
        "BBB", "BUY", 10, 100.0, "entry", "r1", broker_order_id="bbb-buy", fill_status="filled", stop_loss=90.0
    )
    db.insert_trade("BBB", "TRAIL_STOP", 10, 90.0, "protect", "r1")
    db.conn.execute(
        "UPDATE trades SET fill_status = NULL, fill_qty = NULL WHERE action = 'TRAIL_STOP' AND broker_order_id IS NULL"
    )
    # Clean-record start (owner ruling 2026-10-09): date the rows after it, in order.
    db.conn.execute("UPDATE trades SET timestamp = datetime('2026-10-13 14:00:00', '+' || id || ' seconds')")
    db.conn.commit()

    net = db.get_symbols_with_open_ledger_qty()
    assert net["AAA"] == net["CCC"] == net["DDD"] == 0.0  # the stops sold them
    assert net["BBB"] == 10.0  # this stop is still resting

    stats = db.compute_trade_calibration(lookback_days=3650)
    # Exactly the three stops that fired closed a round trip — BBB is still
    # open to BOTH accountings, which is the agreement being asserted.
    assert stats["n"] == 3


# ---------------------------------------------------------------------------
# Item 101: surfacing the reconciler's dropped return values to the owner.
#
# Before this, `_reconcile_stop_out_fills` wrote a broker-made stop-out back
# to the ledger (a real forced-loss exit) and `_drain_pending_protection_
# restores` re-protected naked positions, but every call site invoked both as
# BARE statements — the return values were dropped and NEITHER event ever
# reached the owner. `_surface_reconcile_outcomes` is the routing point that
# closes that gap by paging through the same `send_owner_alert` path the
# unexplained-gap branch already uses. These tests prove a reconciled stop-out
# now produces owner-facing output where before it produced none.
# ---------------------------------------------------------------------------


def test_surface_reconcile_outcomes_pages_owner_for_a_broker_stop_out(
    tmp_path,
    monkeypatch,
):
    """The exact ONDS incident: a reconciled broker stop-out now sends a
    standalone owner alert carrying WHY (symbol, shares, price, realized
    loss). Before item 101 the call site dropped this return value and the
    owner heard nothing."""
    db = Database(str(tmp_path / "t.db"))
    db.initialize()
    _filled_buy(db, "ONDS", 17, 8.53, "entry-onds", ts="2026-08-27 14:31:55")

    broker = MagicMock()
    broker.get_positions.return_value = []  # flat at the broker

    def _fills(symbol, after):
        if symbol == "ONDS":
            return [
                _stop_order(
                    "00000000-0000-4000-8000-0000000000b1",
                    "ONDS",
                    17.0,
                    7.93,
                    "2026-08-28T16:16:07.476647+00:00",
                )
            ]
        return []

    broker.list_filled_sell_orders.side_effect = _fills

    pipeline = _mk_pipeline(db, broker)
    reco = pipeline._reconcile_stop_out_fills(run_id="r-reconcile")
    assert next(r for r in reco if r["symbol"] == "ONDS")["recorded"] == 1

    import src.notifier as notifier

    sent = []
    monkeypatch.setattr(
        notifier,
        "send_owner_alert",
        lambda text, **kw: sent.append((text, kw)) or True,
    )

    pipeline._surface_reconcile_outcomes(reco, 0, run_id="r-reconcile")

    assert len(sent) == 1, "a reconciled stop-out must page the owner exactly once"
    text, kw = sent[0]
    assert "BROKER STOPPED YOU OUT" in text
    assert "ONDS" in text
    assert "17 share" in text  # the WHY: how many
    assert "7.93" in text  # the WHY: at what price
    assert "$10.20" in text  # the WHY: realized loss magnitude
    assert "−" in text  # ...and it was a LOSS (signed)
    assert kw.get("symbols") == ["ONDS"]


def test_surface_reconcile_outcomes_pages_re_protection_count(
    tmp_path,
    monkeypatch,
):
    """A drained (re-protected) naked position is a live-risk event; its
    count now reaches the owner instead of being silently discarded."""
    db = Database(str(tmp_path / "t.db"))
    db.initialize()
    pipeline = _mk_pipeline(db, MagicMock())

    import src.notifier as notifier

    sent = []
    monkeypatch.setattr(
        notifier,
        "send_owner_alert",
        lambda text, **kw: sent.append((text, kw)) or True,
    )

    pipeline._surface_reconcile_outcomes(
        [],
        drained_count=[
            {"symbol": "ETN", "shares": 40.0, "stop_price": 280.5, "unprotected_since": None},
            {"symbol": "PWR", "shares": 25.0, "stop_price": 312.25, "unprotected_since": None},
        ],
        run_id="r1",
    )

    assert len(sent) == 1
    text, _ = sent[0]
    assert "PROTECTION RESTORED" in text
    assert "2 positions" in text


def test_surface_reconcile_outcomes_silent_when_nothing_happened(
    tmp_path,
    monkeypatch,
):
    """No stop-out recorded and nothing drained → no owner page. An
    unexplained-gap result (matched False / recorded 0) is handled by the
    reconciler's own records-disagree alert, NOT by this surfacing helper,
    so it must not double-page here."""
    db = Database(str(tmp_path / "t.db"))
    db.initialize()
    pipeline = _mk_pipeline(db, MagicMock())

    import src.notifier as notifier

    sent = []
    monkeypatch.setattr(
        notifier,
        "send_owner_alert",
        lambda text, **kw: sent.append((text, kw)) or True,
    )

    pipeline._surface_reconcile_outcomes(
        [{"symbol": "XYZ", "ledger_qty": 5.0, "broker_qty": 0.0, "matched": False, "recorded": 0}],
        0,
        run_id="r1",
    )

    assert sent == []


# ===========================================================================
# Item 173(2) — session ORDERING of the two reconcilers.
#
# At the intra_check and evening sites the fill reconcile now runs BEFORE the
# stop-out reconcile. These two tests pin WHY: a SELL this pipeline submitted
# but has not yet reconciled makes the stop-out check page a FALSE "records
# disagree" CRITICAL, and reconciling that fill first makes the false page
# impossible. They exercise the reconciler primitives directly (fast, no
# session body) — the ordering itself lives in `run_intra_check` /
# `run_evening`.
# ===========================================================================


def test_a_submitted_but_unreconciled_sell_fakes_a_records_disagree_gap(
    tmp_path,
    monkeypatch,
):
    """The bug the reorder fixes. A SELL this pipeline submitted is still
    'submitted' (fill not yet reconciled). `get_symbols_with_open_ledger_qty`
    ignores 'submitted' rows, so the ledger reports the whole position still
    open while the broker has already reduced it — a positive gap. The broker's
    filled-SELL history DOES contain that sale, but its broker_order_id is
    already known (the submitted row carries it), so it is filtered out of
    new_fills and the reconciler pages a FALSE 'records disagree' CRITICAL for
    a sale that is fully explained. Also proves the no-double-record property:
    the known id keeps a duplicate exit out of the ledger."""
    db = Database(str(tmp_path / "t.db"))
    db.initialize()
    _filled_buy(db, "AMD", 10, 500.0, "amd-buy")
    # A SELL this pipeline submitted: broker id known, fill NOT yet reconciled.
    db.insert_trade("AMD", "SELL", 10, 520.0, "exit", "r1", broker_order_id="amd-sell", fill_status="submitted")

    broker = MagicMock()
    broker.get_positions.return_value = []  # broker already flat (the SELL filled)
    broker.list_filled_sell_orders.return_value = [
        {
            "id": "amd-sell",
            "symbol": "AMD",
            "qty": 10.0,
            "price": 520.0,
            "filled_at": "2026-09-25T15:00:00+00:00",
            "order_type": "market",
        }
    ]

    pipeline = _mk_pipeline(db, broker)
    import src.notifier as notifier

    sent: list[str] = []
    monkeypatch.setattr(
        notifier,
        "send_owner_alert",
        lambda text, **kw: sent.append(text) or True,
    )

    results = pipeline._reconcile_stop_out_fills(run_id="r1")

    assert results == [
        {
            "symbol": "AMD",
            "ledger_qty": 10.0,
            "broker_qty": 0.0,
            "matched": False,
            "recorded": 0,
        }
    ]
    assert any("RECORDS DISAGREE" in t for t in sent), "the unreconciled SELL provokes the false records-disagree page"
    # No double-record: the known broker id kept any duplicate exit row out.
    exits = [
        r
        for r in db.get_trades(symbol="AMD", executed_only=True)
        if r["action"] in ("STOP_OUT", "SELL", "RECONCILED_EXIT")
    ]
    assert exits == []


def test_reconciling_the_sell_first_prevents_the_false_records_disagree_page(
    tmp_path,
    monkeypatch,
):
    """The fix (item 173(2)). Reconciling submitted fills BEFORE the stop-out
    check — the intra/evening ordering — flips that SELL to 'filled', so the
    ledger's open-qty view matches the broker, the gap closes, and no false
    page fires. The broker's fill history is never even queried for a symbol
    with no gap."""
    db = Database(str(tmp_path / "t.db"))
    db.initialize()
    _filled_buy(db, "AMD", 10, 500.0, "amd-buy")
    db.insert_trade("AMD", "SELL", 10, 520.0, "exit", "r1", broker_order_id="amd-sell", fill_status="submitted")
    # This is exactly what `_reconcile_fills` does when it runs first.
    db.update_trade_fill(broker_order_id="amd-sell", fill_status="filled", fill_qty=10.0, fill_price=520.0)

    broker = MagicMock()
    broker.get_positions.return_value = []
    broker.list_filled_sell_orders.return_value = [
        {
            "id": "amd-sell",
            "symbol": "AMD",
            "qty": 10.0,
            "price": 520.0,
            "filled_at": "2026-09-25T15:00:00+00:00",
            "order_type": "market",
        }
    ]

    pipeline = _mk_pipeline(db, broker)
    import src.notifier as notifier

    sent: list[str] = []
    monkeypatch.setattr(
        notifier,
        "send_owner_alert",
        lambda text, **kw: sent.append(text) or True,
    )

    results = pipeline._reconcile_stop_out_fills(run_id="r1")

    assert results == []  # no gap, nothing to reconcile
    assert sent == []  # and therefore no false page
    broker.list_filled_sell_orders.assert_not_called()


# ===========================================================================
# Item 173(4) — the LATENT short-cover reconcile case.
#
# Short-cover reconciliation is deliberately NOT built (see the reconciler's
# docstring: a short's protective stop is a BUY-to-cover, staged out). This
# pins the INTENDED behaviour for now: the reconciler skips a covered short
# entirely — it writes no exit and does not even query the broker's fill
# history for it — rather than attempting a write-back it cannot yet get right
# (item 173(c) shows the short-side sign is knowingly wrong). Do NOT "fix" this
# by building short-cover reconciliation here.
# ===========================================================================


def test_reconcile_skips_a_broker_covered_short_no_writeback_no_page(
    tmp_path,
    monkeypatch,
):
    """A SHORT the broker covered unilaterally (buy-to-cover stop fired). The
    ledger still believes the short is open (a negative net qty); the broker is
    flat. The reconciler must skip it: the negative ledger qty trips the
    'ledger already believes it's flat / not a long gap' guard before any broker
    query, so no exit row is written and the owner is not paged. This documents
    the deferred short-cover path — not a bug to close in this change."""
    db = Database(str(tmp_path / "t.db"))
    db.initialize()
    db.insert_trade(
        "FLNC",
        "SHORT",
        36,
        7.39,
        "short entry",
        "r1",
        broker_order_id="flnc-short",
        fill_status="filled",
        stop_loss=110.0,
    )

    # Sanity: the ledger's own count is a negative (short) number.
    assert db.get_symbols_with_open_ledger_qty()["FLNC"] == -36.0

    broker = MagicMock()
    broker.get_positions.return_value = []  # broker covered the short → flat

    pipeline = _mk_pipeline(db, broker)
    import src.notifier as notifier

    sent: list[str] = []
    monkeypatch.setattr(
        notifier,
        "send_owner_alert",
        lambda text, **kw: sent.append(text) or True,
    )

    results = pipeline._reconcile_stop_out_fills(run_id="r1")

    assert results == [], "short-cover reconciliation is deferred — nothing acted on"
    broker.list_filled_sell_orders.assert_not_called()
    assert sent == []
    # No exit row invented for the short.
    exits = [
        r
        for r in db.get_trades(symbol="FLNC", executed_only=True)
        if r["action"] in ("STOP_OUT", "COVER", "RECONCILED_EXIT")
    ]
    assert exits == []


# ---------------------------------------------------------------------------
# Item 173(c): a COVER-family action is a BUY-to-cover. It RETIRES a short
# toward zero and must ADD shares to the ledger's belief, not subtract them.
# Before the fix every non-BUY/SWEEP_BUY executed row was signed -1, so a
# SHORT 36 fully covered read -72 instead of 0 [measured 2026-09-23].
# ---------------------------------------------------------------------------


def test_full_cover_retires_a_short_to_zero(tmp_path):
    """SHORT 36 opened, COVER 36 filled — the ledger must read flat (0),
    not -72 (the pre-fix double-subtract)."""
    db = Database(str(tmp_path / "t.db"))
    db.initialize()
    db.insert_trade(
        "GME", "SHORT", 36, 20.0, "open short", "r1", broker_order_id="gme-short", fill_status="filled", stop_loss=110.0
    )
    # Sanity: the short alone reads negative.
    assert db.get_symbols_with_open_ledger_qty()["GME"] == -36.0
    db.insert_trade("GME", "COVER", 36, 18.0, "cover short", "r2", broker_order_id="gme-cover", fill_status="filled")
    assert db.get_symbols_with_open_ledger_qty()["GME"] == 0.0


def test_partial_cover_reduces_the_short_toward_zero(tmp_path):
    """A COVER of 10 against a SHORT 36 leaves -26, not -46."""
    db = Database(str(tmp_path / "t.db"))
    db.initialize()
    db.insert_trade(
        "GME", "SHORT", 36, 20.0, "open short", "r1", broker_order_id="gme-short", fill_status="filled", stop_loss=110.0
    )
    db.insert_trade("GME", "COVER", 10, 19.0, "trim short", "r2", broker_order_id="gme-cover", fill_status="filled")
    assert db.get_symbols_with_open_ledger_qty()["GME"] == -26.0


def test_partial_cover_pct_label_is_normalised_and_adds(tmp_path):
    """PARTIAL_COVER(50%) must normalise to PARTIAL_COVER and add, exactly
    as _symbols_already_trimmed_today normalises the label."""
    db = Database(str(tmp_path / "t.db"))
    db.initialize()
    db.insert_trade(
        "GME", "SHORT", 36, 20.0, "open short", "r1", broker_order_id="gme-short", fill_status="filled", stop_loss=110.0
    )
    db.insert_trade(
        "GME", "PARTIAL_COVER(50%)", 18, 19.0, "cover half", "r2", broker_order_id="gme-pcover", fill_status="filled"
    )
    assert db.get_symbols_with_open_ledger_qty()["GME"] == -18.0


def test_emergency_cover_retires_a_short(tmp_path):
    """EMERGENCY_COVER is the short-side twin of EMERGENCY_SELL and must add."""
    db = Database(str(tmp_path / "t.db"))
    db.initialize()
    db.insert_trade(
        "GME", "SHORT", 12, 20.0, "open short", "r1", broker_order_id="gme-short", fill_status="filled", stop_loss=110.0
    )
    db.insert_trade(
        "GME", "EMERGENCY_COVER", 12, 25.0, "panic cover", "r2", broker_order_id="gme-ecover", fill_status="filled"
    )
    assert db.get_symbols_with_open_ledger_qty()["GME"] == 0.0


def test_long_exits_still_subtract_after_cover_fix(tmp_path):
    """Regression: SELL / REDUCE / STOP_OUT on a long must still subtract —
    the COVER fix must not turn every buy-ish word into an add."""
    db = Database(str(tmp_path / "t.db"))
    db.initialize()
    db.insert_trade(
        "AAPL", "BUY", 20, 100.0, "entry", "r1", broker_order_id="a-buy", fill_status="filled", stop_loss=90.0
    )
    db.insert_trade("AAPL", "SELL", 4, 110.0, "trim", "r2", broker_order_id="a-sell", fill_status="filled")
    db.insert_trade("AAPL", "REDUCE", 3, 108.0, "trim", "r3", broker_order_id="a-red", fill_status="filled")
    db.insert_trade("AAPL", "STOP_OUT", 2, 95.0, "stopped", "r4", broker_order_id="a-stop", fill_status="filled")
    assert db.get_symbols_with_open_ledger_qty()["AAPL"] == 11.0


def test_filled_buy_to_cover_trail_stop_retires_a_short(tmp_path):
    """Item 173(c), second route: a SHORT protected by a TRAIL_STOP that the
    broker FILLED (a buy-to-cover) must retire the short toward zero. Its side
    is not in the action name, so it is read from the running net — a stop
    resting on a negative position is a buy-to-cover, so it adds. Full cover
    of a 36-share short reads 0, not -72 (the pre-fix double-subtract)."""
    db = Database(str(tmp_path / "t.db"))
    db.initialize()
    db.insert_trade(
        "FLNC",
        "SHORT",
        36,
        7.39,
        "short entry",
        "r1",
        broker_order_id="flnc-short",
        fill_status="filled",
        stop_loss=110.0,
    )
    db.insert_trade(
        "FLNC", "TRAIL_STOP", 36, 8.2, "protect", "r1", broker_order_id="flnc-stop", fill_status="submitted"
    )
    _set_fill(db, "flnc-stop", status="filled", qty=36.0)
    assert db.get_symbols_with_open_ledger_qty()["FLNC"] == 0.0


def test_long_fired_trail_stop_still_subtracts_after_cover_fix(tmp_path):
    """The mirror guard: a fired TRAIL_STOP on a LONG is a protective SELL
    and must still subtract. The running net is positive there, so the
    side-read signs it -1."""
    db = Database(str(tmp_path / "t.db"))
    db.initialize()
    db.insert_trade(
        "LLY", "BUY", 8, 900.0, "entry", "r1", broker_order_id="lly-buy", fill_status="filled", stop_loss=90.0
    )
    db.insert_trade("LLY", "TRAIL_STOP", 8, 850.0, "protect", "r1", broker_order_id="lly-stop", fill_status="submitted")
    _set_fill(db, "lly-stop", status="filled", qty=8.0)
    assert db.get_symbols_with_open_ledger_qty()["LLY"] == 0.0
