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

from src.status_plain_extra import owner_words
from src.storage.analytics.trade_review import clean_record_since_label, close_review_summary
from src.trader_feed.common import _DB_PATH

logger = logging.getLogger(__name__)

NO_TRADES_LINE = "No closed trades today."
UNKNOWN = "unknown"
MAX_NAMES_SHOWN = 4


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


def _signed_usd(value) -> str:
    """Dollars with a sign ("+$12.34"); the owner does not use R multiples."""
    if value is None:
        return UNKNOWN
    return f"{'-' if value < 0 else '+'}${abs(value):,.2f}"


def _trade_usd(entry) -> str:
    if not entry:
        return UNKNOWN
    return f"{entry['symbol']} {_signed_usd(entry['usd'])}"


def _words(key: str) -> str:
    """The plain label for one `close_review_summary` key (the one table: `src/status_plain_extra.py`)."""
    return owner_words("trade_review", key)


def _profit_factor_text(value) -> str:
    """Dollars won per dollar lost; the stored tokens ("no losses", "n/a") come out as words."""
    if value is None:
        return UNKNOWN
    if isinstance(value, str):
        return owner_words("profit_factor_value", value)
    return f"${value:.2f}"


def _count_line(summary: dict, since: str) -> str:
    n, wins, losses = summary["n"], summary["wins"], summary["losses"]
    return (
        f"{_words('n').format(since=since)}: {n} {'trade' if n == 1 else 'trades'}, "
        f"{wins} {'win' if wins == 1 else _words('wins')}, {losses} {'loss' if losses == 1 else _words('losses')}"
    )


def _not_counted_line(summary: dict, since: str) -> str | None:
    """Trades closed today that opened before the clean record, named so the owner sees what is left out."""
    names = summary["not_counted"]
    if not names:
        return None
    shown = ", ".join(names[:MAX_NAMES_SHOWN])
    more = f" +{len(names) - MAX_NAMES_SHOWN} more" if len(names) > MAX_NAMES_SHOWN else ""
    return f"{_words('not_counted').format(since=since)}: {shown}{more}"


def trade_block_lines(summary: dict | None) -> list[str]:
    """At most six lines. None (unreadable history) renders as one unknown line."""
    if summary is None:
        return [f"Trade review: {UNKNOWN} (trade history could not be read)."]
    since = clean_record_since_label()
    not_counted = _not_counted_line(summary, since)
    if not summary["n"]:
        return [NO_TRADES_LINE] if not_counted is None else [_count_line(summary, since), not_counted]
    p = summary["p_value"]
    lines = [
        _count_line(summary, since),
        f"{_words('win_rate_pct')} {_num(summary['win_rate_pct'], '{:.1f}%')} · {_words('total_usd')} "
        f"{_signed_usd(summary['total_usd'])} · {_words('average_usd')} {_signed_usd(summary['average_usd'])}",
        f"{_words('best')} {_trade_usd(summary['best'])} · {_words('worst')} {_trade_usd(summary['worst'])}",
        f"{_words('profit_factor')}: {_profit_factor_text(summary['profit_factor'])}",
        f"Luck check: {summary['quality_n']} {_words('quality_n')} · {_words('p_value')}: "
        f"{UNKNOWN if p is None else _num(p * 100, '{:.2f}%')}",
    ]
    if not_counted is not None:
        lines.append(not_counted)
    return lines


def append_trade_block(lines: list[str]) -> None:
    """Append the block; any failure degrades to the unknown line, never to a missing message."""
    try:
        rows = _read_trade_rows()
        summary = None if rows is None else close_review_summary(rows)
        lines.extend(trade_block_lines(summary))
    except Exception as exc:  # noqa: BLE001
        logger.warning("close-review trade block failed: %s", exc)
        lines.extend(trade_block_lines(None))
