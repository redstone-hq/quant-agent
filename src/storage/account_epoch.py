"""Account epoch: when the paper account was last reset, so the desk's money readers start clean.

Owner ruling 9 Oct 2026: history is EXCLUDED by date, never deleted. A paper
account reset leaves the old account's `trades` and `daily_pnl` rows in place,
so every reader that decides orders, stops or gross exposure reads them through
two views that hold only the rows since the latest reset:

- `current_account_trades`: rows stamped at or after the latest reset time.
- `current_account_daily_pnl`: rows dated on or after the first session close
  after the reset. Rows are keyed by ET date and the upsert never moves their
  timestamp, so a reset at 23:00 ET must not keep that day's old-account row.

`account_epoch` is append-only (triggers refuse DELETE and UPDATE). Its first
row is written once, when the table is created, with a NULL epoch, which means
exactly "no cut: all history recorded so far counts". No date is invented for
it. Every later row is one reset, written once by scripts/desk_reset.py with
the time it ran. The desk does not detect the broker account: the owner resets
the paper account as a planned act and runs that script.

A missing epoch raises. Both views call `account_epoch_guard`, which raises
when the table is empty, and a connection that never registered the guard
cannot read the views at all ("no such function"). It never defaults to the
beginning of time: that would bring back the old equity peak and push the
gross-exposure ladder to its floor.

Holidays: the cut date uses the weekday calendar, not the exchange's. That is
safe for the cut: the old account's last row is dated on or before the epoch's
ET date, so any weekday whose close falls after the epoch is later than it.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta

from src.trading_calendar import ET, REGULAR_SESSION_CLOSE_MIN, SQL_STAMP_FORMAT, UTC, is_weekday, sql_utc_stamp

TABLE = "account_epoch"
CURRENT_TRADES_VIEW = "current_account_trades"
CURRENT_DAILY_PNL_VIEW = "current_account_daily_pnl"
GUARD_FUNCTION = "account_epoch_guard"


class AccountEpochMissing(RuntimeError):
    """No account epoch row: the desk cannot tell which rows belong to the current account."""


_TABLE_DDL = f"""CREATE TABLE {TABLE} (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    epoch_utc TEXT,
    daily_pnl_from TEXT,
    recorded_at TEXT NOT NULL
)"""

_TRIGGERS = (
    f"""CREATE TRIGGER IF NOT EXISTS account_epoch_no_delete BEFORE DELETE ON {TABLE}
BEGIN SELECT RAISE(ABORT, 'account_epoch is append-only'); END""",
    f"""CREATE TRIGGER IF NOT EXISTS account_epoch_written_once BEFORE UPDATE ON {TABLE}
BEGIN SELECT RAISE(ABORT, 'account_epoch rows are written once'); END""",
)


def _latest(column: str) -> str:
    return f"(SELECT {column} FROM {TABLE} ORDER BY id DESC LIMIT 1)"


# `replace(substr(...))` reads an ISO stamp with a "T" the same as SQLite's own
# "YYYY-MM-DD HH:MM:SS", so a T-stamped row cannot sort after the epoch it precedes.
_VIEWS = {
    CURRENT_TRADES_VIEW: f"""CREATE VIEW {CURRENT_TRADES_VIEW} AS
SELECT t.* FROM trades AS t
WHERE {GUARD_FUNCTION}((SELECT COUNT(*) FROM {TABLE}))
  AND ({_latest("epoch_utc")} IS NULL
       OR replace(substr(t.timestamp, 1, 19), 'T', ' ') >= {_latest("epoch_utc")})""",
    CURRENT_DAILY_PNL_VIEW: f"""CREATE VIEW {CURRENT_DAILY_PNL_VIEW} AS
SELECT d.* FROM daily_pnl AS d
WHERE {GUARD_FUNCTION}((SELECT COUNT(*) FROM {TABLE}))
  AND ({_latest("daily_pnl_from")} IS NULL OR d.date >= {_latest("daily_pnl_from")})""",
}


def _guard(row_count) -> int:
    if not row_count:
        raise AccountEpochMissing(f"{TABLE} has no row: refusing to guess which account the history belongs to")
    return 1


def install_guard(conn: sqlite3.Connection) -> None:
    """Register the guard the views call. Every connection that reads the views needs it."""
    conn.create_function(GUARD_FUNCTION, 1, _guard, deterministic=True)


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


def ensure_account_epoch(conn: sqlite3.Connection) -> None:
    """Create the table (seeding the no-cut first row only at creation), its triggers and the views."""
    install_guard(conn)
    if not conn.in_transaction:
        conn.execute("BEGIN IMMEDIATE")
    try:
        if not _table_exists(conn, TABLE):
            conn.execute(_TABLE_DDL)
            conn.execute(f"INSERT INTO {TABLE} (recorded_at) VALUES (?)", (sql_utc_stamp(),))
        for ddl in _TRIGGERS:
            conn.execute(ddl)
        for name, ddl in _VIEWS.items():
            row = conn.execute("SELECT sql FROM sqlite_master WHERE type='view' AND name=?", (name,)).fetchone()
            if row is not None and row[0] == ddl:
                continue
            if row is not None:
                conn.execute(f"DROP VIEW {name}")
            conn.execute(ddl)
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def _as_dict(cursor: sqlite3.Cursor, row) -> dict:
    return {d[0]: row[i] for i, d in enumerate(cursor.description)}


def current_epoch(conn: sqlite3.Connection) -> dict:
    """The latest epoch row. Raises AccountEpochMissing when there is none."""
    cur = conn.execute(f"SELECT * FROM {TABLE} ORDER BY id DESC LIMIT 1")
    row = cur.fetchone()
    if row is None:
        raise AccountEpochMissing(f"{TABLE} has no row: refusing to guess which account the history belongs to")
    return _as_dict(cur, row)


def first_session_close_after(epoch_utc: str) -> str:
    """ET date key of the first weekday session whose close falls after `epoch_utc` (a sql_utc_stamp)."""
    when = datetime.strptime(epoch_utc, SQL_STAMP_FORMAT).replace(tzinfo=UTC)
    day = when.astimezone(ET).date()
    while True:
        close = datetime(day.year, day.month, day.day, tzinfo=ET) + timedelta(minutes=REGULAR_SESSION_CLOSE_MIN)
        if is_weekday(day) and close > when:
            return day.isoformat()
        day += timedelta(days=1)


def record_reset(conn: sqlite3.Connection, *, now: datetime | None = None) -> dict:
    """Write one epoch row for a reset at `now` (default: the desk clock). The caller commits."""
    epoch = sql_utc_stamp(now)
    conn.execute(
        f"INSERT INTO {TABLE} (epoch_utc, daily_pnl_from, recorded_at) VALUES (?, ?, ?)",
        (epoch, first_session_close_after(epoch), epoch),
    )
    return current_epoch(conn)


def current_account_daily_pnl(conn: sqlite3.Connection, limit: int, before_date: str | None = None) -> list[dict]:
    """The daily_pnl rows since the latest reset, newest first (same shape as the uncut analytics read)."""
    where, params = ("WHERE date < ?", (before_date,)) if before_date else ("", ())
    cur = conn.execute(
        f"SELECT * FROM {CURRENT_DAILY_PNL_VIEW} {where} ORDER BY date DESC LIMIT ?",  # noqa: S608 - literal
        (*params, limit),
    )
    return [_as_dict(cur, r) for r in cur.fetchall()]
