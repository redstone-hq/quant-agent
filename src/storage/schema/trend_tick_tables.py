"""Migration step: `trend_exit_tick_checks`, which completed daily bar the trend exit last ran on, per holding.

Owner ruling 2026-10-09: the trend exit runs at every 30-minute check on DAILY closes only. A new
completed bar appears once a day, so the check runs once per holding per new bar; this row is what
lets later ticks the same day skip it, and retry only a sale the guarded sell path refused
(see src/intraday/trend_exit_tick.py). One row per symbol. Idempotent.
"""

from __future__ import annotations

import sqlite3


def ensure_trend_tick_table(*, conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS trend_exit_tick_checks (
            symbol TEXT PRIMARY KEY,
            bar_date TEXT NOT NULL,
            outcome TEXT NOT NULL,
            run_id TEXT,
            updated_at TEXT NOT NULL
        );
    """)
