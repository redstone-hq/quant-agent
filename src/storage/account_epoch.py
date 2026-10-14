"""Account epoch: which broker account the desk's money readers belong to.

Owner ruling 9 Oct 2026: history is EXCLUDED by date, never deleted. A paper
account reset leaves the old account's `trades` and `daily_pnl` rows in place,
so every reader that decides orders, stops or gross exposure reads them through
two views that hold only the CURRENT account's rows:

- `current_account_trades`: rows stamped at or after the current epoch.
- `current_account_daily_pnl`: rows dated on or after the first session close
  after the epoch. Rows are keyed by ET date and the upsert never moves their
  timestamp, so a reset at 23:00 ET must not keep that day's old-account row.

`account_epoch` is append-only (triggers refuse DELETE, and any UPDATE except
binding the first row's account id once). Its first row is written once, when
the table is created, with a NULL epoch, which means exactly "no cut: all
history recorded so far belongs to the first account the desk reads". No date
is invented for it. The desk binds that row's account id the first time it
reads the broker account. Every later row is written once, when the desk (or
scripts/desk_reset.py) first sees a different account id and the change passes
`account_change_problems`.

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

import logging
import sqlite3
from collections.abc import Callable
from datetime import datetime, timedelta

from src.trading_calendar import ET, REGULAR_SESSION_CLOSE_MIN, SQL_STAMP_FORMAT, UTC, is_weekday, sql_utc_stamp

logger = logging.getLogger(__name__)

TABLE = "account_epoch"
CURRENT_TRADES_VIEW = "current_account_trades"
CURRENT_DAILY_PNL_VIEW = "current_account_daily_pnl"
GUARD_FUNCTION = "account_epoch_guard"
# Write-ahead queues that hold broker order ids or stop specs for the book they were written against.
OPERATIONAL_TABLES = ("pending_protection_restores", "pending_repegs", "pending_stop_amends")


class AccountEpochMissing(RuntimeError):
    """No account epoch row: the desk cannot tell which rows belong to the current account."""


class AccountEpochRefused(RuntimeError):
    """The broker account changed but the change failed a check; the desk must trade nothing new."""


_TABLE_DDL = f"""CREATE TABLE {TABLE} (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id TEXT UNIQUE,
    epoch_utc TEXT,
    daily_pnl_from TEXT,
    account_created_at TEXT,
    recorded_at TEXT NOT NULL
)"""

_TRIGGERS = (
    f"""CREATE TRIGGER IF NOT EXISTS account_epoch_no_delete BEFORE DELETE ON {TABLE}
BEGIN SELECT RAISE(ABORT, 'account_epoch is append-only'); END""",
    f"""CREATE TRIGGER IF NOT EXISTS account_epoch_written_once BEFORE UPDATE ON {TABLE}
WHEN OLD.account_id IS NOT NULL OR NEW.id IS NOT OLD.id OR NEW.epoch_utc IS NOT OLD.epoch_utc
  OR NEW.daily_pnl_from IS NOT OLD.daily_pnl_from OR NEW.recorded_at IS NOT OLD.recorded_at
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
    """The current account's epoch row. Raises AccountEpochMissing when there is none."""
    cur = conn.execute(f"SELECT * FROM {TABLE} ORDER BY id DESC LIMIT 1")
    row = cur.fetchone()
    if row is None:
        raise AccountEpochMissing(f"{TABLE} has no row: refusing to guess which account the history belongs to")
    return _as_dict(cur, row)


def as_utc_stamp(value) -> str | None:
    """A broker timestamp (datetime or ISO text) in the desk's naive-UTC SQL format; None when absent."""
    if value is None or value == "":
        return None
    when = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return sql_utc_stamp(when)


def first_session_close_after(epoch_utc: str) -> str:
    """ET date key of the first weekday session whose close falls after `epoch_utc` (a sql_utc_stamp)."""
    when = datetime.strptime(epoch_utc, SQL_STAMP_FORMAT).replace(tzinfo=UTC)
    day = when.astimezone(ET).date()
    while True:
        close = datetime(day.year, day.month, day.day, tzinfo=ET) + timedelta(minutes=REGULAR_SESSION_CLOSE_MIN)
        if is_weekday(day) and close > when:
            return day.isoformat()
        day += timedelta(days=1)


def newest_history_stamp(conn: sqlite3.Connection) -> str | None:
    """Newest write stamp in `trades` or `daily_pnl`, across every account (deliberately uncut)."""
    stamps = [
        conn.execute("SELECT MAX(replace(substr(timestamp, 1, 19), 'T', ' ')) FROM trades").fetchone()[0],
        conn.execute("SELECT MAX(replace(substr(timestamp, 1, 19), 'T', ' ')) FROM daily_pnl").fetchone()[0],
    ]
    present = [s for s in stamps if s]
    return max(present) if present else None


def operational_row_counts(conn: sqlite3.Connection) -> dict[str, int]:
    """Rows left in each write-ahead queue that exists in this database."""
    return {
        t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]  # noqa: S608 - fixed table names
        for t in OPERATIONAL_TABLES
        if _table_exists(conn, t)
    }


def account_change_problems(
    conn: sqlite3.Connection,
    *,
    account_id: str | None,
    created_at,
    book_empty: bool,
    open_orders_empty: bool = True,
) -> list[str]:
    """Why switching the desk to `account_id` is unsafe; empty when it is safe. Raises when no epoch row exists."""
    recorded = current_epoch(conn)
    problems: list[str] = []
    if not account_id:
        problems.append("the broker returned no account id")
    elif recorded["account_id"] is None:
        problems.append("the old account's id was never recorded; run one desk session on the old account first")
    elif recorded["account_id"] == account_id:
        problems.append(f"account id {account_id} is the one already recorded; the keys were not swapped")
    created = as_utc_stamp(created_at)
    newest = newest_history_stamp(conn)
    if created is None:
        problems.append("the broker gave no account created_at")
    elif newest is not None and created <= newest:
        problems.append(f"account created_at {created} is not later than the newest history row {newest}")
    if not book_empty:
        problems.append("the broker book is not empty")
    if not open_orders_empty:
        problems.append("the broker has open orders")
    return problems


def record_account_change(
    conn: sqlite3.Connection, *, account_id: str, created_at, now: datetime | None = None
) -> dict:
    """Write the new account's epoch row (caller checked `account_change_problems` and commits)."""
    epoch = sql_utc_stamp(now)
    conn.execute(
        f"INSERT INTO {TABLE} (account_id, epoch_utc, daily_pnl_from, account_created_at, recorded_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (account_id, epoch, first_session_close_after(epoch), as_utc_stamp(created_at), epoch),
    )
    return current_epoch(conn)


def _default_alert(text: str) -> None:
    from src.notifier.owner_alert import send_owner_alert

    send_owner_alert(text, category="account_epoch")


def observe_broker_account(
    conn: sqlite3.Connection,
    *,
    account: dict,
    positions,
    alert: Callable[[str], None] | None = None,
) -> dict:
    """Check the broker account the desk just read against the recorded epoch.

    Same id: nothing to do. First read after this table was created: bind the
    id to the no-cut first row. A different id: accept it (one new epoch row)
    only if `account_change_problems` and the write-ahead queues are clean;
    otherwise alert the owner and raise AccountEpochRefused, so the session
    stops before it decides anything. Resting broker stops are untouched.
    """
    recorded = current_epoch(conn)
    account_id = account.get("account_id")
    created_at = account.get("account_created_at")
    if account_id and recorded["account_id"] is None and recorded["epoch_utc"] is None:
        conn.execute(
            f"UPDATE {TABLE} SET account_id = ?, account_created_at = ? WHERE id = ? AND account_id IS NULL",
            (account_id, as_utc_stamp(created_at), recorded["id"]),
        )
        conn.commit()
        return current_epoch(conn)
    if account_id and recorded["account_id"] == account_id:
        return recorded
    problems = account_change_problems(
        conn, account_id=account_id, created_at=created_at, book_empty=not list(positions or [])
    )
    queued = {t: n for t, n in operational_row_counts(conn).items() if n}
    if queued:
        problems.append(f"write-ahead queues still hold old-book rows {queued}; run scripts/desk_reset.py")
    send = alert or _default_alert
    if problems:
        text = (
            f"Broker account changed from {recorded['account_id']} to {account_id}, and the desk REFUSED it: "
            + "; ".join(problems)
            + ". Trading nothing new until resolved."
        )
        logger.critical(text)
        send(text)
        raise AccountEpochRefused(text)
    new = record_account_change(conn, account_id=account_id, created_at=created_at)
    conn.commit()
    send(f"New broker account {account_id} recorded; history before {new['epoch_utc']} UTC is excluded, not deleted.")
    return new


def current_account_daily_pnl(conn: sqlite3.Connection, limit: int, before_date: str | None = None) -> list[dict]:
    """The current account's daily_pnl rows, newest first (same shape as the uncut analytics read)."""
    where, params = ("WHERE date < ?", (before_date,)) if before_date else ("", ())
    cur = conn.execute(
        f"SELECT * FROM {CURRENT_DAILY_PNL_VIEW} {where} ORDER BY date DESC LIMIT ?",  # noqa: S608 - literal
        (*params, limit),
    )
    return [_as_dict(cur, r) for r in cur.fetchall()]
