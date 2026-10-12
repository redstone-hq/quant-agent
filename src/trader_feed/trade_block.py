"""Trade-review block for the CLOSE REVIEW Telegram message (display only).

Reads the `trades` table through a read-only connection and hands the rows to
`src.storage.analytics.trade_review`, which owns every figure. Nothing here
computes a statistic. No agent or prompt reads this text back: the stored close
report keeps structured fields only, and `src.agents` / `src.prompt_facts` may
not import the trade-review module (import contract).
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

from src.storage.analytics.trade_review import close_review_summary
from src.trader_feed.common import _DB_PATH

logger = logging.getLogger(__name__)

NO_TRADES_LINE = "No closed trades today."
UNKNOWN = "unknown"


def _read_trade_rows() -> list[dict] | None:
    """Every position-chain `trades` row, or None when the history cannot be read."""
    path = Path(_DB_PATH)
    if not path.exists():
        return None
    conn = None
    try:
        conn = sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True, timeout=1.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=1000")
        rows = conn.execute(
            "SELECT * FROM trades WHERE position_id IS NOT NULL AND position_id != '' ORDER BY timestamp, id"
        ).fetchall()
        return [dict(r) for r in rows]
    except sqlite3.Error as exc:
        logger.warning("close-review trade block read failed: %s", exc)
        return None
    finally:
        if conn is not None:
            conn.close()


def _num(value, template: str = "{:.2f}") -> str:
    return UNKNOWN if value is None else template.format(value)


def _signed_r(value) -> str:
    return UNKNOWN if value is None else f"{value:+.2f}R"


def _trade_r(entry) -> str:
    if not entry:
        return UNKNOWN
    return f"{entry['symbol']} {_signed_r(entry['r'])}"


def trade_block_lines(summary: dict | None) -> list[str]:
    """At most five lines. None (unreadable history) renders as one unknown line."""
    if summary is None:
        return [f"Trade review: {UNKNOWN} (trade history could not be read)."]
    if not summary["n"]:
        return [NO_TRADES_LINE]
    pf = summary["profit_factor"]
    return [
        f"Closed today: {summary['n']} trades, {summary['wins']} wins, {summary['losses']} losses",
        f"Win rate {_num(summary['win_rate_pct'], '{:.1f}%')} · total {_signed_r(summary['total_r'])}"
        f" · average {_signed_r(summary['average_r'])}",
        f"Best {_trade_r(summary['best'])} · worst {_trade_r(summary['worst'])}",
        f"Profit factor {UNKNOWN if pf is None else pf}",
        f"Luck check: {summary['quality_n']} trades with known R, p-value {_num(summary['p_value'], '{:.4f}')}",
    ]


def append_trade_block(lines: list[str]) -> None:
    """Append the block; any failure degrades to the unknown line, never to a missing message."""
    try:
        rows = _read_trade_rows()
        summary = None if rows is None else close_review_summary(rows)
        lines.extend(trade_block_lines(summary))
    except Exception as exc:  # noqa: BLE001
        logger.warning("close-review trade block failed: %s", exc)
        lines.extend(trade_block_lines(None))
