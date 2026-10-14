"""Owner ruling 5b (13 Oct): a fill EDITS the message that listed the order as placed.

When an order a session message listed as "placed, waiting to fill" fills, the
same Telegram message is edited so that line reads "filled" -- no new message,
no chosen delay. `send_log.record_placed_lines` stored the message id and the
exact delivered line when the message landed; this module rebuilds the line
from the confirmed fill and makes one editMessageText call.

SAFETY: this rides on the fill path and may never change it. `note_fill` is
called only after the fill write has committed, hands the work to a worker
thread (so a slow Telegram never delays reconciliation; the standard library
joins it at interpreter exit), makes ONE attempt (no retry loop) and never
raises. No stored message id: nothing is sent.

Stop wording: the "placed" line's stop comes from
`trader_feed.common._symbol_stop`, which reads the PLANNED stop (the
constructor's `stop_loss`, or a risk modification of it), never the stop the
broker holds. The fill path does not read the broker's stop, and an entry's
protective stop is attached after the fill, so none is known here. The
rebuilt line therefore carries that planned stop forward from the stored
"placed" line and labels it "planned stop" -- never a bare "stop".
"""

from __future__ import annotations

import html
import re
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from types import SimpleNamespace
from typing import Any

import src.notifier.base as base
from src.notifier.base import logger
from src.notifier.send_log import PLACED_ORDER_MESSAGES_DDL, _plain
from src.risk.metrics import unrealized_pnl_pct
from src.trading_calendar import sql_utc_stamp

#: Opening actions per side; every other executed action reduces the position.
_OPENING_LONG = {"BUY", "SWEEP_BUY"}
_OPENING = _OPENING_LONG | {"SHORT"}

#: Runs the edits off the fill path. The standard library joins its worker
#: threads at interpreter exit, so a session process that ends right after a
#: fill does not kill its edit mid-send. Threads start lazily, on first submit.
_EXECUTOR = ThreadPoolExecutor(thread_name_prefix="fill-line-edit")
#: Two fills in one message must not both edit from the same stale text.
_EDIT_LOCK = threading.Lock()


def note_fill(db: Any, broker_order_id: str) -> None:
    """Start the "filled" edit for `broker_order_id` in the background. Never raises."""
    try:
        trades_path = str(getattr(db, "db_path", None) or base._DB_PATH)
        _spawn(lambda: _run(trades_path, broker_order_id))
    except Exception as exc:  # noqa: BLE001
        logger.warning("fill edit for %s not started: %s", broker_order_id, exc)


def _spawn(target) -> None:
    _EXECUTOR.submit(target)


def _run(trades_path: str, broker_order_id: str) -> None:
    try:
        _edit_placed_lines(trades_path, broker_order_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("fill edit for %s failed: %s", broker_order_id, exc)


def _notifier():
    from src.notifier.owner_alert_funnel import build_default_notifier

    return build_default_notifier()


def _edit_placed_lines(trades_path: str, broker_order_id: str) -> int:
    """Edit every stored message listing `broker_order_id` as placed; returns edits landed."""
    with _EDIT_LOCK:
        conn = sqlite3.connect(str(base._DB_PATH), timeout=5.0)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute(PLACED_ORDER_MESSAGES_DDL)
            rows = conn.execute(
                "SELECT id, message_id, line_text, message_text FROM placed_order_messages "
                "WHERE broker_order_id = ? AND edited_at IS NULL ORDER BY id",
                (broker_order_id,),
            ).fetchall()
            if not rows:
                logger.info("fill edit: no stored message lists order %s as placed; nothing edited", broker_order_id)
                return 0
            line = filled_line(trades_path, broker_order_id)
            if line is None:
                logger.warning("fill edit: no confirmed fill found for order %s", broker_order_id)
                return 0
            notifier = _notifier()
            landed = 0
            for row in rows:
                current = conn.execute(
                    "SELECT message_text FROM placed_order_messages WHERE message_id = ? ORDER BY id DESC LIMIT 1",
                    (row["message_id"],),
                ).fetchone()
                text = current["message_text"] if current is not None else row["message_text"]
                if row["line_text"] not in text:
                    logger.warning("fill edit: stored line for order %s no longer in its message", broker_order_id)
                    continue
                row_line = _with_planned_stop(line, row["line_text"])
                new_text = text.replace(row["line_text"], html.escape(row_line, quote=False), 1)
                if not notifier.edit_message_text(row["message_id"], new_text):
                    continue
                conn.execute(
                    "UPDATE placed_order_messages SET message_text = ? WHERE message_id = ?",
                    (new_text, row["message_id"]),
                )
                conn.execute(
                    "UPDATE placed_order_messages SET edited_at = ? WHERE id = ?", (sql_utc_stamp(), row["id"])
                )
                conn.commit()
                landed += 1
            return landed
        finally:
            conn.close()


def filled_line(trades_path: str, broker_order_id: str) -> str | None:
    """The owner's "filled" line for a confirmed fill, from the ledger as it now stands.

    P&L is the row's `realized_pnl` -- the ledger's own average-cost fill walk,
    written when the fill was confirmed -- and the entry average is recovered
    from it, so nothing is re-derived a second way. Holding time runs from the
    position's first opening fill (same `position_id`) to this fill.
    """
    conn = sqlite3.connect(trades_path, timeout=5.0)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT symbol, action, fill_qty, fill_price, filled_at, realized_pnl, position_id "
            "FROM trades WHERE broker_order_id = ? ORDER BY id DESC LIMIT 1",
            (broker_order_id,),
        ).fetchone()
        if row is None or not row["fill_qty"] or row["fill_price"] is None:
            return None
        position = []
        if row["position_id"]:
            position = conn.execute(
                "SELECT action, fill_qty, filled_at FROM trades "
                "WHERE symbol = ? AND position_id = ? AND COALESCE(fill_qty, 0) > 0 ORDER BY id",
                (row["symbol"], row["position_id"]),
            ).fetchall()
    finally:
        conn.close()
    action = str(row["action"] or "").upper()
    qty = float(row["fill_qty"])
    price = float(row["fill_price"])
    parts = [f"   • {action} {str(row['symbol']).upper()} {qty:g} sh @ ${price:,.2f} — filled"]
    openings = [p for p in position if str(p["action"] or "").upper() in _OPENING]
    if action in _OPENING:
        if len(openings) > 1:
            held = abs(sum(_signed(p) for p in position))
            parts.append(f"now holds {held:g} sh")
        return " · ".join(parts)
    pnl = row["realized_pnl"]
    if pnl is not None:
        pnl = float(pnl)
        short = action.startswith("COVER")
        entry = price + pnl / qty if short else price - pnl / qty
        sign = "-" if pnl < 0 else "+"
        # The ONE definition of a P&L percent (|avg_entry x qty| denominator).
        pct_value = unrealized_pnl_pct(SimpleNamespace(avg_entry=entry, qty=qty, unrealized_pnl=pnl))
        pct = f" ({pct_value:+.2f}%)" if pct_value is not None else ""
        parts.append(f"{sign}${abs(pnl):,.2f}{pct}")
        parts.append(f"entry avg ${entry:,.2f}")
    held_for = _held_for(openings, row["filled_at"])
    if held_for:
        parts.append(f"held {held_for}")
    return " · ".join(parts)


_PLACED_STOP = re.compile(r"· stop (\$[\d,]+\.\d+)")


def _with_planned_stop(line: str, placed_line: str) -> str:
    """`line` plus the stop the "placed" line printed, labelled as the planned stop."""
    match = _PLACED_STOP.search(_plain(placed_line))
    return f"{line} · planned stop {match.group(1)}" if match else line


def _signed(row: Any) -> float:
    action = str(row["action"] or "").upper()
    qty = float(row["fill_qty"] or 0)
    if action in _OPENING_LONG or action.startswith("COVER"):
        return qty
    return -qty


def _held_for(openings: list, filled_at: str | None) -> str | None:
    """'26 days' / '5 hours' / '12 minutes' from the first opening fill; None when unknown."""
    times = [p["filled_at"] for p in openings if p["filled_at"]]
    if not times or not filled_at:
        return None
    try:
        seconds = (datetime.fromisoformat(str(filled_at)) - datetime.fromisoformat(str(min(times)))).total_seconds()
    except ValueError:
        return None
    if seconds < 0:
        return None
    for size, unit in ((86400, "day"), (3600, "hour"), (60, "minute")):
        if seconds >= size:
            count = int(seconds // size)
            return f"{count} {unit}{'s' if count != 1 else ''}"
    return "under a minute"
