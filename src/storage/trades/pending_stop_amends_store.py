"""Storage for stop levels the desk owes the next open (see src/execution/pending_stop_amends.py).

Functions take the `TradeLedger` (`db._trades()`), so the big ledger file does not grow.
"""

from __future__ import annotations

from src.trading_calendar import sql_utc_stamp


def record(
    ledger,
    symbol: str,
    intended_stop: float,
    *,
    is_short: bool = False,
    reason: str = "market_closed",
    run_id: str | None = None,
) -> int:
    """Remember a stop level the desk could not amend to while shut.

    One row per symbol: a later intent REPLACES an earlier one, because only the
    latest level the desk decided on is worth applying at the open. The direction
    test lives in the drain, so the writer never silently drops an intent.
    """

    def _do():
        ledger.conn.execute("DELETE FROM pending_stop_amends WHERE symbol = ?", (symbol,))
        cur = ledger.conn.execute(
            "INSERT INTO pending_stop_amends (symbol, intended_stop, is_short, reason, run_id, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (symbol, float(intended_stop), 1 if is_short else 0, reason, run_id, sql_utc_stamp()),
        )
        ledger.conn.commit()
        return cur.lastrowid or 0

    return ledger._locked_write(_do, label="record_pending_stop_amend")


def get_all(ledger) -> list[dict]:
    """Every undischarged out-of-hours stop intent, oldest first."""
    with ledger._lock:
        rows = ledger.conn.execute(
            "SELECT id, symbol, intended_stop, is_short, reason, run_id, created_at "
            "FROM pending_stop_amends ORDER BY created_at ASC"
        ).fetchall()
    return [dict(r) for r in rows]


def delete(ledger, row_id: int) -> int:
    """Discharge one row by primary key (after it is applied, or voided)."""

    def _do():
        cur = ledger.conn.execute("DELETE FROM pending_stop_amends WHERE id = ?", (row_id,))
        ledger.conn.commit()
        return cur.rowcount or 0

    return ledger._locked_write(_do, label="delete_pending_stop_amend")
