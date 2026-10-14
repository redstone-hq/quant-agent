"""Owner ruling 5b (13 Oct): a fill EDITS the message that listed the order as placed.

When an order a session message listed as "placed, waiting to fill" fills, the
same Telegram message is edited so that line reads "filled" -- no new message,
no chosen delay. `send_log.record_placed_lines` stored the message id and the
exact delivered line when the message landed; this module rebuilds the line
from the confirmed fill and makes one editMessageText call.

SAFETY: this rides on the fill path and may never change it. `note_fill` is
called only after the fill write has committed, hands the work to a daemon
thread (so a slow Telegram never delays reconciliation), makes ONE attempt
(no retry loop) and never raises. No stored message id: nothing is sent.

Stop wording: the rebuilt line carries NO stop. The "placed" line's stop comes
from `trader_feed.common._symbol_stop`, which reads the PLANNED stop (the
constructor's `stop_loss`, or a risk modification of it), never the stop the
broker actually holds, and no broker-held stop is known at this point.
"""

from __future__ import annotations

import atexit
import html
import sqlite3
import threading
from collections import deque
from datetime import datetime
from typing import Any

import src.notifier.base as base
from src.notifier.base import logger
from src.notifier.send_log import PLACED_ORDER_MESSAGES_DDL
from src.trading_calendar import sql_utc_stamp

#: Opening actions per side; every other executed action reduces the position.
_OPENING_LONG = {"BUY", "SWEEP_BUY"}
_OPENING = _OPENING_LONG | {"SHORT"}

#: Edits started recently. Bounded: never grows with uptime. Joined at exit so a
#: session process that ends right after a fill does not kill its edit mid-send.
_RECENT: deque[threading.Thread] = deque(maxlen=32)
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
    thread = threading.Thread(target=target, name="fill-line-edit", daemon=True)
    thread.start()
    _RECENT.append(thread)


def _join_pending_edits() -> None:
    """Let in-flight edits finish before the process exits.

    Each edit is already bounded by its own database and Telegram timeouts,
    so this adds no wait of its own; it only stops interpreter exit from
    killing a daemon edit thread between the fill write and the edit.
    """
    for thread in list(_RECENT):
        thread.join()


atexit.register(_join_pending_edits)


def _run(trades_path: str, broker_order_id: str) -> None:
    try:
        _edit_placed_lines(trades_path, broker_order_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("fill edit for %s failed: %s", broker_order_id, exc)


def _notifier():
    from src.notifier.transport import TelegramNotifier

    return TelegramNotifier()


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
                new_text = text.replace(row["line_text"], html.escape(line, quote=False), 1)
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
        pct = f" ({pnl / (entry * qty) * 100:+.2f}%)" if entry > 0 else ""
        parts.append(f"{sign}${abs(pnl):,.2f}{pct}")
        parts.append(f"entry avg ${entry:,.2f}")
    held_for = _held_for(openings, row["filled_at"])
    if held_for:
        parts.append(f"held {held_for}")
    return " · ".join(parts)


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
