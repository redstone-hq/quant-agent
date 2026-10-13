"""Migration step: the two Sentinel-seam tables (see src/sentinel/). Append-only, idempotent."""

from __future__ import annotations

import logging
import sqlite3

logger = logging.getLogger(__name__)


def ensure_sentinel_tables(*, conn: sqlite3.Connection) -> None:
    """Create `order_attempts` and `reconciliation_runs` when absent. Never alters existing tables."""
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS order_attempts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            recorded_at TEXT NOT NULL,
            symbol TEXT,
            side TEXT,
            qty REAL,
            outcome TEXT NOT NULL,            -- submitted | rejected | submit_unknown | cancelled ...
            client_order_id TEXT,             -- read from the broker payload; NULL until the adapter surfaces it
            broker_order_id TEXT,
            run_id TEXT,
            reason TEXT,
            limit_price REAL
        );
        CREATE INDEX IF NOT EXISTS idx_order_attempts_recorded_at ON order_attempts(recorded_at);
        CREATE TABLE IF NOT EXISTS reconciliation_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ran_at TEXT NOT NULL,
            kind TEXT NOT NULL,               -- stop_coverage | recorded_stop_levels | orphan_submits | stop_out_fills
            agreed INTEGER NOT NULL,          -- 1 broker and ledger agreed, 0 they did not
            detail TEXT,
            run_id TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_reconciliation_runs_kind ON reconciliation_runs(kind);
    """)
    conn.commit()
