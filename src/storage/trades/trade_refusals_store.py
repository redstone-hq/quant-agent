"""Storage for `trade_refusals` (schema: src/storage/schema/trade_refusal_tables.py).

Functions take the `TradeLedger` (`db._trades()`), so the big ledger file does not grow.

`PortfolioConstructor.last_refusals` keeps the human sentence and is drained only for the
symbols that reach `constructor_dropped`; this is the durable half, written at the moment of
refusal, and it stores no English at all. A trial the owner asked to judge later ("see if
that improves the desk purchases") is judged from these columns.

An OBSERVATION is not a refusal and must never be read as one. A sub-floor risk request
(board item 223) is NOT refused and NOT resized, only recorded, as `stage="observed_not_refused"`
with the request in `requested_risk_pct` and the floor in `threshold`. A computed-vs-analyst
target comparison lands the same way, with the signed gap in `observed_gap_pct` and the warn
threshold in `threshold`. Read `refusal` and `stage` together before counting anything here
as a declined trade.
"""

from __future__ import annotations

from src.trading_calendar import sql_utc_stamp

_COLUMNS = (
    "run_id",
    "symbol",
    "direction",
    "refusal",
    "stage",
    "entry_price",
    "stop_price",
    "level_used",
    "reward_risk",
    "threshold",
    "level_was_measured",
    "requested_risk_pct",
    "observed_gap_pct",
)


def insert(
    ledger,
    *,
    symbol: str,
    direction: str | None,
    refusal: str,
    entry_price: float | None = None,
    stop_price: float | None = None,
    level_used: float | None = None,
    reward_risk: float | None = None,
    threshold: float | None = None,
    level_was_measured: bool | None = None,
    stage: str | None = None,
    run_id: str | None = None,
    requested_risk_pct: float | None = None,
    observed_gap_pct: float | None = None,
) -> int | None:
    """Record one NAMED refusal, or one named OBSERVATION, by its numbers."""
    values = (
        run_id,
        str(symbol or "").strip().upper(),
        direction,
        refusal,
        stage,
        entry_price,
        stop_price,
        level_used,
        reward_risk,
        threshold,
        None if level_was_measured is None else int(bool(level_was_measured)),
        requested_risk_pct,
        observed_gap_pct,
    )
    sql = (
        f"INSERT INTO trade_refusals (timestamp, {', '.join(_COLUMNS)}) VALUES (?, {', '.join('?' for _ in _COLUMNS)})"
    )

    def _do():
        cur = ledger.conn.execute(sql, (sql_utc_stamp(), *values))
        ledger.conn.commit()
        return cur.lastrowid

    return ledger._locked_write(_do, label="insert_trade_refusal")


def get_all(ledger, *, refusal: str | None = None, limit: int = 500) -> list[dict]:
    """Read back the durable refusal rows, newest first."""
    sql = "SELECT * FROM trade_refusals"
    args: list = []
    if refusal:
        sql += " WHERE refusal = ?"
        args.append(refusal)
    sql += " ORDER BY id DESC LIMIT ?"
    args.append(int(limit))
    cur = ledger.conn.execute(sql, tuple(args))
    cols = [c[0] for c in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]
