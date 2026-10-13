#!/usr/bin/env python3
"""Backfill `trades.filled_at` - the broker's own fill time - on fills recorded before it existed.

The lot walk orders fills by `filled_at` alone (src/trade_actions.py). A symbol whose LIVE
position holds a fill without a fill time has an unknown fill order: its open R is unknown
(no R ratchets). This asks the broker (Alpaca PAPER, hard-coded) for each such fill's
`filled_at` by its order id, the same read and full-precision parse the fill reconciler
uses, then re-derives the touched symbols' position ids from the walk - all in one write
through the desk's own write path (`Database.backfill_fill_times`). A failed broker read
is counted apart from "no fill time" and never written; --execute refuses if any failed.

Dry-run by default: opens the database READ-ONLY and prints how many executed fills
lack a fill time, how many of those lack a broker order id (cannot be backfilled), how
many the broker returned a time for, and which symbols open in the ledger would still
have R unknown. Writes only with BOTH --execute and --owner-approved. Review against a
COPY; never --execute on the live database without the owner's approval.
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

_COLUMNS = (
    "id",
    "symbol",
    "action",
    "qty",
    "fill_qty",
    "fill_status",
    "filled_at",
    "broker_order_id",
    "position_id",
    "timestamp",
)


def _parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Backfill broker fill times into trades.filled_at. Dry-run by default.")
    p.add_argument(
        "--db-path",
        type=Path,
        required=True,
        help="SQLite database. Review against a COPY; never --execute on the live database without owner approval.",
    )
    p.add_argument("--execute", action="store_true", help="Write the fill times. Requires --owner-approved.")
    p.add_argument("--owner-approved", action="store_true", help="The owner approved this write; needs --execute.")
    return p.parse_args(argv)


def _read_rows(db_path: Path) -> list[dict]:
    """Every trades row, read-only; `filled_at` reads as NULL on a database that predates it."""
    conn = sqlite3.connect(f"{db_path.as_uri()}?mode=ro", uri=True)
    try:
        conn.row_factory = sqlite3.Row
        have = {r["name"] for r in conn.execute("PRAGMA table_info(trades)")}
        select = ", ".join(c if c in have else f"NULL AS {c}" for c in _COLUMNS)
        return [dict(r) for r in conn.execute(f"SELECT {select} FROM trades ORDER BY symbol, id")]
    finally:
        conn.close()


def _r_unknown_open(rows: list[dict]) -> list[str]:
    """Symbols the ledger holds shares of whose fill order is unknown (R unknown, no R ratchets)."""
    from src.quantities import ORDER_QTY_DECIMALS
    from src.storage.trades.position_chain import _executed_row
    from src.trade_actions import executed_qty, live_lot_rows, share_change

    by_symbol: dict[str, list[dict]] = {}
    for row in rows:
        by_symbol.setdefault(row["symbol"], []).append(row)
    out = []
    for symbol, symbol_rows in sorted(by_symbol.items()):
        moving = [r for r in symbol_rows if share_change(r) is not None and _executed_row(r)]
        net = sum(executed_qty(r) * (1 if share_change(r) == "in" else -1) for r in moving)
        if round(net, ORDER_QTY_DECIMALS) > 0 and live_lot_rows(moving, decimals=ORDER_QTY_DECIMALS) is None:
            out.append(symbol)
    return out


def plan(rows: list[dict], fetch_fill_info) -> dict:
    """What the backfill would write. `fetch_fill_info(order_id)` is the broker's order read."""
    from src.storage.trades.position_chain import _executed_row
    from src.trade_actions import broker_fill_time, fill_time, share_change

    missing = [r for r in rows if share_change(r) is not None and _executed_row(r) and fill_time(r) is None]
    updates: list[tuple[int, str, str]] = []
    broker_none = 0
    read_errors = 0
    for row in missing:
        if not row.get("broker_order_id"):
            continue
        try:
            info = fetch_fill_info(row["broker_order_id"])
        except Exception:  # noqa: BLE001 - a failed read is counted, never written
            info = None
        if not isinstance(info, dict):  # the desk's read returns None when it fails
            read_errors += 1
            continue
        filled_at = broker_fill_time(info.get("filled_at"))
        if filled_at:
            updates.append((row["id"], row["symbol"], filled_at))
        else:
            broker_none += 1
    found = {row_id: filled_at for row_id, _, filled_at in updates}
    after = [dict(r, filled_at=found.get(r["id"], r.get("filled_at"))) for r in rows]
    return {
        "missing": len(missing),
        "no_order_id": sum(1 for r in missing if not r.get("broker_order_id")),
        "broker_none": broker_none,
        "read_errors": read_errors,
        "updates": updates,
        "r_unknown_open": _r_unknown_open(after),
    }


def _report(result: dict, db_path: Path, execute: bool) -> None:
    print(f"Database: {db_path}")
    print("Broker:   Alpaca PAPER (order reads only)")
    print(f"Mode:     {'EXECUTE (owner-approved write)' if execute else 'DRY RUN (read-only, nothing written)'}")
    print(f"Executed fills without a broker fill time: {result['missing']}")
    print(f"  without a broker order id (cannot be backfilled): {result['no_order_id']}")
    print(f"  broker returned a fill time (would be written): {len(result['updates'])}")
    print(f"  broker returned no fill time: {result['broker_none']}")
    print(f"  broker read failed (counted apart, never written): {result['read_errors']}")
    print(f"Symbols open in the ledger that would still have R unknown: {len(result['r_unknown_open'])}")
    if result["r_unknown_open"]:
        print(f"  {', '.join(result['r_unknown_open'])}")


def _apply(db_path: Path, updates: list[tuple[int, str, str]]) -> int:
    from src.storage.db import Database

    db = Database(str(db_path))
    db.initialize()  # adds trades.filled_at to a database that predates it
    try:
        db.backfill_fill_times(updates)  # the desk's own locked write path, one transaction
    finally:
        db.close()
    symbols = sorted({symbol for _, symbol, _ in updates})
    print(f"Wrote {len(updates)} fill time(s); re-derived position ids for {len(symbols)} symbol(s).")
    return 0


def _paper_broker():
    api_key = os.environ.get("ALPACA_API_KEY")
    api_secret = os.environ.get("ALPACA_SECRET_KEY")
    if not api_key or not api_secret:
        print("ERROR: ALPACA_API_KEY / ALPACA_SECRET_KEY not set", file=sys.stderr)
        return None
    from src.execution.broker import AlpacaBroker

    return AlpacaBroker(api_key=api_key, secret_key=api_secret, paper=True)


def main(argv=None, broker=None) -> int:
    args = _parse_args(argv)
    db_path = args.db_path.resolve()
    if not db_path.exists():
        print(f"ERROR: --db-path {db_path} does not exist", file=sys.stderr)
        return 2
    if args.execute != args.owner_approved:
        print("REFUSED: a write needs BOTH --execute and --owner-approved. Nothing written.", file=sys.stderr)
        return 2
    broker = broker or _paper_broker()
    if broker is None:
        return 2
    result = plan(_read_rows(db_path), broker.get_order_fill_info)
    _report(result, db_path, args.execute)
    if args.execute and result["read_errors"]:
        print(
            f"REFUSED: {result['read_errors']} broker read(s) failed. Nothing written; re-run when reads succeed.",
            file=sys.stderr,
        )
        return 1
    return _apply(db_path, result["updates"]) if args.execute else 0


if __name__ == "__main__":
    sys.exit(main())
