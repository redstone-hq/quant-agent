"""One durable row per order ATTEMPT (submit accepted, rejected, unknown, cancel).

Why: docs/FUTURE.md's erratic-behaviour breaker needs "orders per minute" and
"trades per hour", and before this nothing counted orders at all. This records
what the execution stage already knows at the moment an outcome is known; it
adds no behaviour and nothing in the desk reads it yet.

The `client_order_id` column is READ from the order payload the broker
adapter returned (never derived here). The desk does send a deterministic key on
entry and stop submissions (src/execution/order_idempotency.py), but the adapter
does not surface it in the dict it returns, so the column is NULL on every row
until it does: nothing is fabricated.
"""

from __future__ import annotations

import logging
import sqlite3

from src.trading_calendar import sql_utc_stamp

logger = logging.getLogger(__name__)


class OrderAttemptLog:
    """Writer and reader for the `order_attempts` table on one sqlite connection."""

    def __init__(self, *, conn: sqlite3.Connection):
        self.conn = conn

    def record(
        self,
        *,
        symbol: str | None,
        side: str | None,
        qty: float | None,
        outcome: str,
        client_order_id: str | None = None,
        broker_order_id: str | None = None,
        run_id: str | None = None,
        reason: str = "",
        limit_price: float | None = None,
    ) -> int:
        """Append one attempt row; returns its rowid."""
        cur = self.conn.execute(
            "INSERT INTO order_attempts (recorded_at, symbol, side, qty, outcome, client_order_id,"
            " broker_order_id, run_id, reason, limit_price)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                sql_utc_stamp(),
                symbol,
                side,
                qty,
                outcome,
                client_order_id,
                broker_order_id,
                run_id,
                reason,
                limit_price,
            ),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def recent(self, *, limit: int) -> list[dict]:
        """Newest-first attempts, as plain dicts."""
        cur = self.conn.execute(
            "SELECT id, recorded_at, symbol, side, qty, outcome, client_order_id,"
            " broker_order_id, run_id, reason, limit_price FROM order_attempts"
            " ORDER BY id DESC LIMIT ?",
            (int(limit),),
        )
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]

    def count_since(self, *, since_utc: str) -> int:
        """Attempts recorded at or after `since_utc` ('YYYY-MM-DD HH:MM:SS', UTC) -- the breaker's rate input."""
        cur = self.conn.execute(
            "SELECT COUNT(*) FROM order_attempts WHERE recorded_at >= ?",
            (since_utc,),
        )
        return int(cur.fetchone()[0])


def record_order_attempt_from_event(
    *, db, symbol, outcome: str, reason: str, run_id: str | None, details: dict
) -> None:
    """Funnel hook: turn an `order` lifecycle event into one attempt row.

    `db` is whatever the pipeline holds; only a real sqlite connection on it
    is written to (test fakes without one are skipped, and that skip is logged).
    """
    conn = getattr(db, "conn", None)
    if not isinstance(conn, sqlite3.Connection):
        logger.debug("order attempt not recorded: db has no sqlite connection")
        return
    qty = details.get("qty")
    OrderAttemptLog(conn=conn).record(
        symbol=symbol,
        side=details.get("side"),
        qty=float(qty) if qty is not None else None,
        outcome=outcome,
        client_order_id=details.get("client_order_id"),
        broker_order_id=details.get("broker_order_id"),
        run_id=run_id,
        reason=reason,
        limit_price=details.get("limit_price"),
    )
