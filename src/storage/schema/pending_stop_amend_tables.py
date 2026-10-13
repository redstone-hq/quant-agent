"""Migration step: `pending_stop_amends`, a protective-stop level the desk intended while the market was shut.

Alpaca refuses `replace_order_by_id` on an `accepted` order (HTTP 422, measured on the paper broker
2026-10-02), the status every resting stop carries after the 16:00 ET close. The row is written
instead of the amend and discharged by the next open's coverage preamble. Idempotent.
"""

from __future__ import annotations

import sqlite3


def ensure_pending_stop_amend_table(*, conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS pending_stop_amends (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT NOT NULL,
            intended_stop REAL NOT NULL,
            is_short INTEGER NOT NULL DEFAULT 0,
            reason TEXT,
            run_id TEXT,
            created_at TEXT NOT NULL
        );
    """)
