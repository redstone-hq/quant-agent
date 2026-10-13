"""Storage for the per-holding trend-exit tick marker (see src/intraday/trend_exit_tick.py).

The `bar_date` column holds the SESSION key (ET trading date of the tick whose
pass wrote it), not a bar date: the pass runs once per session.

Functions take the `TradeLedger` (`db._trades()`), so the big ledger file does not grow.
"""

from __future__ import annotations

from src.trading_calendar import sql_utc_stamp


def get(ledger, symbol: str) -> dict | None:
    """The last session the trend pass ran in for `symbol` (`bar_date`), and its outcome; None if never."""
    with ledger._lock:
        row = ledger.conn.execute(
            "SELECT symbol, bar_date, outcome, run_id, updated_at FROM trend_exit_tick_checks WHERE symbol = ?",
            (symbol,),
        ).fetchone()
    return dict(row) if row is not None else None


def record(ledger, symbol: str, *, bar_date: str, outcome: str, run_id: str | None = None) -> int:
    """Replace `symbol`'s marker: only the latest session's pass matters."""

    def _do():
        cur = ledger.conn.execute(
            "INSERT OR REPLACE INTO trend_exit_tick_checks (symbol, bar_date, outcome, run_id, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (symbol, bar_date, outcome, run_id, sql_utc_stamp()),
        )
        ledger.conn.commit()
        return cur.rowcount or 0

    return ledger._locked_write(_do, label="record_trend_exit_tick_check")
