"""Migration step: `trade_refusals`, one row per NAMED refusal or named OBSERVATION, by its numbers.

Board item 218 (owner ruling 2026-10-01). The parity refusal is an explicitly PROVISIONAL
trial -- "for now ... see if that improves the desk purchases" -- so the thing it refused has
to be recoverable as NUMBERS, not prose: one row per name per run, every quantity in its own
column, so "did refusing these improve the desk's purchases" is a query and not a grep.

Board item 223 (2026-10-01) adds `requested_risk_pct`, the risk the seat ASKED for, so a
sub-floor observation records the request itself and not only the floor it sat under.

`observed_gap_pct` (2026-10-05) carries the signed computed-vs-analyst target gap, one row
per comparison, so the model's chart-reading is read off a distribution instead of argued
about. Nothing accumulates: every share is a read-time query.

Idempotent. Columns added after the table first shipped are ALTERed in for existing files.
"""

from __future__ import annotations

import logging
import sqlite3

logger = logging.getLogger(__name__)

#: Columns that post-date the first release of the table, in (name, ddl) form.
_LATER_COLUMNS: tuple[tuple[str, str], ...] = (
    ("requested_risk_pct", "requested_risk_pct REAL"),
    ("observed_gap_pct", "observed_gap_pct REAL"),
)


def ensure_trade_refusal_table(*, conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS trade_refusals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            run_id TEXT,
            symbol TEXT NOT NULL,
            direction TEXT,
            refusal TEXT NOT NULL,
            stage TEXT,
            entry_price REAL,
            stop_price REAL,
            level_used REAL,
            reward_risk REAL,
            threshold REAL,
            level_was_measured INTEGER,
            requested_risk_pct REAL,
            observed_gap_pct REAL
        );
        CREATE INDEX IF NOT EXISTS idx_trade_refusals_symbol_ts
            ON trade_refusals (symbol, timestamp);
    """)
    existing = {row[1] for row in conn.execute("PRAGMA table_info(trade_refusals)").fetchall()}
    for column, ddl in _LATER_COLUMNS:
        if column in existing:
            continue
        try:
            conn.execute(f"ALTER TABLE trade_refusals ADD COLUMN {ddl}")
            conn.commit()
            logger.info("Schema migration: added trade_refusals.%s", column)
        except Exception as e:  # noqa: BLE001 - an old file stays usable minus one column
            logger.error("Schema migration failed for trade_refusals.%s: %s", column, e)
