"""Stage 3 (shorts) — `pending_protection_restores` gains a `side` column.

Before this fix the write-ahead table used to restore protective stops
after a crash carried no side column: `_derive_close_side_for_drain`
correctly refused to guess when the broker couldn't be read, but every
drain path then fell through to a `side: str = "sell"` default — a
comment at the call sites said this was fine BECAUSE shorts could not be
opened yet. Stage 3 makes that premise false.

This file proves:
  1. A row written today (via `insert_pending_protection_restore`) persists
     its real side and `get_pending_protection_restores` returns it.
  2. The drain path (`_resolve_wal_row_side`, used by both branches of
     `_drain_pending_protection_restores`) PREFERS the persisted side over
     any live-broker derivation — even when the broker would say something
     else, proving it isn't silently re-deriving it.
  3. A legacy row with `side IS NULL` (written before this migration) still
     uses the OLD broker-derived fallback, unchanged — and logs that it did.
"""

import json
import logging
from unittest.mock import MagicMock

from src.pipeline import TradingPipeline, _WAL_SELL_SENTINEL
from src.storage.db import Database
from tests.pipeline_factory import build_pipeline


def _mk_db(tmp_path) -> Database:
    db = Database(str(tmp_path / "t.db"))
    db.initialize()
    return db


def _mk_pipeline(db: Database) -> TradingPipeline:
    pipeline = build_pipeline(
        db=db, broker=MagicMock(), _format_qty=lambda q: str(q), _reprotect_residual_after_partial_sell=MagicMock()
    )
    return pipeline


# ==========================================================================
# 1. DB round-trip: a row written today carries its side.
# ==========================================================================


def test_insert_and_get_pending_protection_restore_round_trips_side(tmp_path):
    db = _mk_db(tmp_path)
    row_id = db.insert_pending_protection_restore(
        symbol="TSLA",
        sell_order_id="cover-order-1",
        position_qty_before_sell=40.0,
        specs_json=json.dumps([{"id": "s1", "qty": 40, "stop_price": 262.5}]),
        side="buy",  # covering a short: the closing order's side is 'buy'
    )
    rows = db.get_pending_protection_restores()
    assert len(rows) == 1
    assert rows[0]["id"] == row_id
    assert rows[0]["side"] == "buy"
    db.close()


def test_insert_without_side_defaults_to_null(tmp_path):
    """A caller that doesn't pass `side` (every pre-Stage-3 call site, and
    every legacy row) gets NULL, not a fabricated default — the drain path
    is what decides what NULL means, not this insert."""
    db = _mk_db(tmp_path)
    db.insert_pending_protection_restore(
        symbol="NVDA",
        sell_order_id="sell-order-1",
        position_qty_before_sell=10.0,
        specs_json=json.dumps([{"id": "s1", "qty": 10, "stop_price": 95.0}]),
    )
    rows = db.get_pending_protection_restores()
    assert rows[0]["side"] is None
    db.close()


# ==========================================================================
# 2. Drain PREFERS the persisted side over live-broker derivation.
# ==========================================================================


def test_drain_sentinel_branch_uses_persisted_side_not_broker_derivation(tmp_path):
    """A row persisted with side='buy' (a short's cover) must drive the
    restore as a BUY-side stop, even when the broker's live position would
    (wrongly, if trusted) derive 'sell' — proving the persisted value wins
    rather than being silently re-derived."""
    db = _mk_db(tmp_path)
    cancelled = [{"id": "stop-old", "qty": 40, "stop_price": 262.5}]
    db.insert_pending_protection_restore(
        symbol="TSLA",
        sell_order_id=_WAL_SELL_SENTINEL,
        position_qty_before_sell=40.0,
        specs_json=json.dumps(cancelled),
        side="buy",
    )

    pipeline = _mk_pipeline(db)
    # If the drain ever fell back to broker derivation, THIS would say
    # 'sell' (a long) — the opposite of the persisted 'buy'. The test
    # proves the persisted value is used INSTEAD of this.
    pipeline._current_position_qty_for_finalize = MagicMock(return_value=40.0)
    pipeline.broker._restore_stop_orders.return_value = (1, [])

    drained = pipeline._drain_pending_protection_restores()

    assert len(drained) == 1
    pipeline.broker._restore_stop_orders.assert_called_once_with(
        "TSLA",
        cancelled,
        check_idempotency=True,
        side="buy",
    )
    db.close()


def test_drain_normal_branch_uses_persisted_side_not_broker_derivation(tmp_path):
    """Same proof as above, on the OTHER drain branch (a real, non-sentinel
    sell_order_id that has reached a terminal broker status) — replayed
    through `_finalize_protection_after_sell`."""
    db = _mk_db(tmp_path)
    cancelled = [{"id": "stop-old", "qty": 40, "stop_price": 262.5, "limit_price": 265.0}]
    db.insert_pending_protection_restore(
        symbol="TSLA",
        sell_order_id="cover-order-resolved",
        position_qty_before_sell=40.0,
        specs_json=json.dumps(cancelled),
        side="buy",
    )

    pipeline = _mk_pipeline(db)
    pipeline._current_position_qty_for_finalize = MagicMock(return_value=40.0)
    pipeline.broker.get_order_fill_info.return_value = {
        "status": "canceled",
        "filled_qty": "0",
        "filled_avg_price": None,
    }
    pipeline.broker._restore_stop_orders.return_value = (1, [])

    drained = pipeline._drain_pending_protection_restores()

    assert len(drained) == 1
    pipeline.broker._restore_stop_orders.assert_called_once_with(
        "TSLA",
        cancelled,
        check_idempotency=True,
        side="buy",
    )
    assert db.get_pending_protection_restores() == []
    db.close()


# ==========================================================================
# 3. A legacy NULL-side row still uses the OLD broker-derived fallback,
#    and logs that it's doing so.
# ==========================================================================


def test_drain_legacy_null_side_row_falls_back_to_broker_derivation(tmp_path, caplog):
    """A row written before this migration has side=NULL. The drain must
    behave EXACTLY as it did pre-migration: derive the side from the live
    broker position (here, a short, so 'buy'), and log that the fallback
    fired."""
    db = _mk_db(tmp_path)
    cancelled = [{"id": "stop-old", "qty": 25, "stop_price": 210.0}]
    # Simulate a pre-migration row: insert without side, exactly as every
    # call site did before this column existed.
    db.insert_pending_protection_restore(
        symbol="MSFT",
        sell_order_id=_WAL_SELL_SENTINEL,
        position_qty_before_sell=25.0,
        specs_json=json.dumps(cancelled),
    )
    assert db.get_pending_protection_restores()[0]["side"] is None

    pipeline = _mk_pipeline(db)
    # Broker reports a SHORT (-25) -> _derive_close_side_for_drain -> 'buy'.
    pipeline._current_position_qty_for_finalize = MagicMock(return_value=-25.0)
    pipeline.broker._restore_stop_orders.return_value = (1, [])

    with caplog.at_level(logging.INFO):
        drained = pipeline._drain_pending_protection_restores()

    assert len(drained) == 1
    pipeline.broker._restore_stop_orders.assert_called_once_with(
        "MSFT",
        cancelled,
        check_idempotency=True,
        side="buy",
    )
    assert any("no persisted side" in rec.message and "MSFT" in rec.message for rec in caplog.records), (
        "legacy NULL-side fallback must be logged, not silent"
    )
    db.close()


def test_drain_legacy_null_side_row_leaves_row_when_broker_unreadable(tmp_path):
    """The other half of 'exactly as it did before': when the broker can't
    be read at all, `_derive_close_side_for_drain` refuses to guess
    (returns None) — `_resolve_wal_row_side` degrades to `{}` (the 'sell'
    default kwargs), and `_restore_after_unconfirmed_sell`'s OWN
    current-position check (unrelated to side) then fails closed and
    leaves the row for next session. Unchanged pre-migration behaviour —
    a legacy row with an unreadable broker is exactly as stuck as it always
    was, not newly stuck and not newly guessed open."""
    db = _mk_db(tmp_path)
    cancelled = [{"id": "stop-old", "qty": 100, "stop_price": 95.0}]
    db.insert_pending_protection_restore(
        symbol="NVDA",
        sell_order_id=_WAL_SELL_SENTINEL,
        position_qty_before_sell=100.0,
        specs_json=json.dumps(cancelled),
    )

    pipeline = _mk_pipeline(db)
    pipeline._current_position_qty_for_finalize = MagicMock(return_value=None)
    pipeline.broker._restore_stop_orders.return_value = (1, [])

    drained = pipeline._drain_pending_protection_restores()

    assert len(drained) == 0
    pipeline.broker._restore_stop_orders.assert_not_called()
    # Row is left in place for the next session's drain pass, not lost.
    assert len(db.get_pending_protection_restores()) == 1
    db.close()
