"""TradeLedger recovery queues bodies, lifted VERBATIM from ledger.py.

`ledger` is the TradeLedger (`self` before the move).
"""

from __future__ import annotations

import logging

from src.trading_calendar import sql_utc_stamp_ago

logger = logging.getLogger(__name__)


def get_protection_restore_wal_audit(ledger) -> list[dict]:
    """Every protection-restore WAL id ever handed out, oldest first.

    Item 193's completeness check: join these against the
    `scale_in|protective_sell_cancelled` events' own `wal_row_id`, and
    an id present here but absent there was spent by the protected-sell
    exit path or by a rolled-back preparation, not by an unrecorded
    cancel.
    """
    with ledger._lock:
        cur = ledger.conn.execute(
            "SELECT row_id, symbol, sell_order_id, "
            "position_qty_before_sell, side, run_id, created_at "
            "FROM protection_restore_wal_audit ORDER BY row_id"
        )
        return [dict(r) for r in cur.fetchall()]


def insert_pending_protection_restore(
    ledger,
    *,
    symbol: str,
    sell_order_id: str,
    position_qty_before_sell: float,
    specs_json: str,
    run_id: str | None = None,
    side: str | None = None,
) -> int:
    """Persist an orphaned protection-restore intent.

    Written when _finalize_protection_after_sell can't act now —
    either cancel of the lingering SELL raised, or the order didn't
    converge to terminal within the short post-cancel wait. Drained
    at session start: the pending row's sell_order_id is re-queried
    for terminal status, and if now terminal, the persisted specs
    drive a fresh finalize attempt.

    `side` is the PROTECTIVE-STOP / closing-order side, which coincide:
    "sell" (default) for a LONG (its protective stop is a SELL below
    entry, and a long is closed by selling) and "buy" for a SHORT (its
    protective stop is a BUY above entry, and a short is closed by
    covering). This is the REAL side, known at write time by whoever is
    closing the position, and is read back by
    `TradingPipeline._resolve_wal_row_side` under exactly this
    convention. (An earlier version of this docstring stated the
    mapping backwards — long→"buy", short→"sell" — which never matched
    the writers or the reader; corrected here.) NULL only for a row
    written before this column existed; the drain path treats NULL as
    the long-assuming fallback (see
    `TradingPipeline._derive_close_side_for_drain`).

    SCALE-IN rows (sell_order_id == `scale_in.WAL_SCALE_IN_SENTINEL`)
    follow the IDENTICAL convention, but they are dispatched to
    `scale_in.drain_scale_in_row` by their sentinel BEFORE any generic
    side reader runs, and that drain classifies long/short from the SIGN
    of `position_qty_before_sell` rather than this column — so a scale-in
    row is never interpreted with a generic reader's meaning either way.
    """

    def _do():
        cur = ledger.conn.execute(
            "INSERT INTO pending_protection_restores "
            "(symbol, sell_order_id, position_qty_before_sell, specs_json, "
            "run_id, side) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (symbol, sell_order_id, position_qty_before_sell, specs_json, run_id, side),
        )
        row_id = cur.lastrowid or 0
        # Item 193: attribute the id before it can be forgotten. Best
        # effort on purpose - an audit failure must never stop a
        # protective-restore intent from being persisted.
        try:
            ledger.conn.execute(
                "INSERT OR IGNORE INTO protection_restore_wal_audit "
                "(row_id, symbol, sell_order_id, "
                "position_qty_before_sell, side, run_id) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (row_id, symbol, sell_order_id, position_qty_before_sell, side, run_id),
            )
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "protection-restore WAL audit row %s not written: %s",
                row_id,
                exc,
            )
        ledger.conn.commit()
        return row_id

    return ledger._locked_write(_do, label="insert_pending_protection_restore")


def get_pending_protection_restores(ledger) -> list[dict]:
    """All currently-pending protection-restore rows, oldest first."""
    with ledger._lock:
        rows = ledger.conn.execute(
            "SELECT id, symbol, sell_order_id, position_qty_before_sell, "
            "specs_json, created_at, run_id, side FROM pending_protection_restores "
            "ORDER BY created_at ASC"
        ).fetchall()
    return [dict(r) for r in rows]


def delete_pending_protection_restore(ledger, row_id: int) -> int:
    """Remove a row by its primary key (after successful drain)."""

    def _do():
        cur = ledger.conn.execute(
            "DELETE FROM pending_protection_restores WHERE id = ?",
            (row_id,),
        )
        ledger.conn.commit()
        return cur.rowcount or 0

    return ledger._locked_write(_do, label="delete_pending_protection_restore")


def update_pending_protection_restore(
    ledger,
    row_id: int,
    *,
    sell_order_id: str | None = None,
    position_qty_before_sell: float | None = None,
    specs_json: str | None = None,
    side: str | None = None,
) -> int:
    """Partial-update a recovery row (only the provided fields).

    audit F1 write-ahead lifecycle: a row is inserted BEFORE
    cancel_protective_stops with a sentinel sell_order_id; this flips
    it to the real broker order id once the SELL is accepted, and
    finalize-on-bail uses it to UPDATE the existing row (instead of
    INSERTing a duplicate alongside the write-ahead row).

    ``side`` (Stage 3, shorts): re-affirms which side this row
    protects — normally already set at the initial write-ahead
    INSERT, this lets a caller correct/set it on UPDATE too.
    """
    sets: list[str] = []
    params: list = []
    if sell_order_id is not None:
        sets.append("sell_order_id = ?")
        params.append(sell_order_id)
    if position_qty_before_sell is not None:
        sets.append("position_qty_before_sell = ?")
        params.append(position_qty_before_sell)
    if specs_json is not None:
        sets.append("specs_json = ?")
        params.append(specs_json)
    if side is not None:
        sets.append("side = ?")
        params.append(side)
    if not sets:
        return 0
    params.append(row_id)
    with ledger._lock:
        cur = ledger.conn.execute(
            f"UPDATE pending_protection_restores SET {', '.join(sets)} WHERE id = ?",
            tuple(params),
        )
        ledger.conn.commit()
        return cur.rowcount or 0


def update_pending_protection_restore_specs(
    ledger,
    row_id: int,
    specs_json: str,
) -> int:
    """Replace the specs_json of an existing recovery row.

    Used by the drain path's partial-restore handling: when 1 of N
    specs landed on this drain attempt, the next drain should only
    retry the N-1 that failed (re-submitting the already-alive stop
    either creates a duplicate or hits held_for_orders, neither
    productive). Codex r10 #1.
    """
    with ledger._lock:
        cur = ledger.conn.execute(
            "UPDATE pending_protection_restores SET specs_json = ? WHERE id = ?",
            (specs_json, row_id),
        )
        ledger.conn.commit()
        return cur.rowcount or 0


def insert_pending_repeg(
    ledger,
    *,
    trade_row_id: int | None,
    symbol: str,
    old_order_id: str,
    new_order_id: str,
    run_id: str | None = None,
) -> int:
    """Persist the intent to replace `old_order_id`.

    `new_order_id` is the caller's sentinel until the broker answers.
    """

    def _do():
        cur = ledger.conn.execute(
            "INSERT INTO pending_repegs "
            "(trade_row_id, symbol, old_order_id, new_order_id, run_id) "
            "VALUES (?, ?, ?, ?, ?)",
            (trade_row_id, symbol, old_order_id, new_order_id, run_id),
        )
        ledger.conn.commit()
        return cur.lastrowid or 0

    return ledger._locked_write(_do, label="insert_pending_repeg")


def get_pending_repegs(ledger) -> list[dict]:
    """All currently-pending re-peg rows, oldest first."""
    with ledger._lock:
        rows = ledger.conn.execute(
            "SELECT id, trade_row_id, symbol, old_order_id, new_order_id, "
            "created_at, run_id FROM pending_repegs ORDER BY created_at ASC"
        ).fetchall()
    return [dict(r) for r in rows]


def resolve_pending_repeg(ledger, row_id: int, new_order_id: str) -> int:
    """Record the id the broker actually minted for a pending re-peg."""

    def _do():
        cur = ledger.conn.execute(
            "UPDATE pending_repegs SET new_order_id = ? WHERE id = ?",
            (new_order_id, row_id),
        )
        ledger.conn.commit()
        return cur.rowcount or 0

    return ledger._locked_write(_do, label="resolve_pending_repeg")


def delete_pending_repeg(ledger, row_id: int) -> int:
    """Remove a re-peg WAL row once the trades row is authoritative."""

    def _do():
        cur = ledger.conn.execute(
            "DELETE FROM pending_repegs WHERE id = ?",
            (row_id,),
        )
        ledger.conn.commit()
        return cur.rowcount or 0

    return ledger._locked_write(_do, label="delete_pending_repeg")


def prune_pending_repegs(ledger, keep_days: int = 30) -> int:
    """Delete pending_repegs rows older than keep_days.

    Same reasoning as `prune_pending_protection_restores`: the drain
    re-attempts every session, so a row that survives 30 days is one the
    broker can no longer resolve (order id aged out of history). Refuses
    keep_days <= 0 rather than wiping a recovery queue.
    """
    if keep_days <= 0:
        raise ValueError(f"prune_pending_repegs: keep_days must be > 0, got {keep_days}")
    with ledger._lock:
        stale = ledger.conn.execute(
            "SELECT id, symbol, old_order_id, created_at FROM pending_repegs WHERE created_at < ?",
            (sql_utc_stamp_ago(days=keep_days),),
        ).fetchall()
        if not stale:
            return 0
        for row in stale:
            logger.info(
                "Pruning stale pending_repeg row %d: symbol=%s old_order_id=%s created_at=%s (>%dd old)",
                row["id"],
                row["symbol"],
                row["old_order_id"],
                row["created_at"],
                keep_days,
            )
        cursor = ledger.conn.execute(
            "DELETE FROM pending_repegs WHERE created_at < ?",
            (sql_utc_stamp_ago(days=keep_days),),
        )
        ledger.conn.commit()
        return cursor.rowcount or 0


def prune_pending_protection_restores(ledger, keep_days: int = 30) -> int:
    """Delete pending_protection_restores rows older than keep_days.

    Drain re-attempts these rows every session; a row that survives
    ~30 calendar days (~20 trading sessions) means either:
      - broker forgot the sell_order_id (deep history GC),
      - the underlying position is gone via other paths (manual
        close, EMERGENCY_SELL during a separate session), or
      - the row's specs_json is malformed in a way drain can't
        recover from automatically.
    In any of these cases, indefinite retention is just operational
    noise — drain can't help. Logs the symbols pruned at INFO so
    manual review remains possible. Returns count deleted.
    """
    if keep_days <= 0:
        # a zero-day cutoff == now → deletes EVERYTHING.
        # Caller almost certainly passed a typo / config bug. Refuse
        # rather than silently wipe a recovery queue.
        raise ValueError(f"prune_pending_protection_restores: keep_days must be > 0, got {keep_days}")
    with ledger._lock:
        stale = ledger.conn.execute(
            "SELECT id, symbol, sell_order_id, created_at FROM pending_protection_restores WHERE created_at < ?",
            (sql_utc_stamp_ago(days=keep_days),),
        ).fetchall()
        if not stale:
            return 0
        for row in stale:
            logger.info(
                "Pruning stale pending_protection_restore row %d: symbol=%s sell_order_id=%s created_at=%s (>%dd old)",
                row["id"],
                row["symbol"],
                row["sell_order_id"],
                row["created_at"],
                keep_days,
            )
        cursor = ledger.conn.execute(
            "DELETE FROM pending_protection_restores WHERE created_at < ?",
            (sql_utc_stamp_ago(days=keep_days),),
        )
        ledger.conn.commit()
        return cursor.rowcount or 0
