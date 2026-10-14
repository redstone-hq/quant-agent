"""Baseline for the account's "Total P&L since ..." figure (dashboard and Telegram).

Owner ruling 2026-10-09: trading before the clean record is not evidence. The
total therefore starts at the equity going INTO the first daily_pnl row dated
on/after the clean-record date (`total_value - daily_pnl`), not at the table's
oldest row. Older rows stay in the database, only uncounted.

Pure functions over plain row dicts so `src/api` (which may not import the
trading stack, tests/test_api_safety.py) and the pipeline share ONE rule; each
side only supplies its own two row reads (`CLEAN_BASELINE_FIRST_SQL`,
`CLEAN_BASELINE_PREV_SQL`).
"""

from __future__ import annotations

import math
from datetime import date

from src.storage.analytics.calibration import CLEAN_RECORD_START_UTC

CLEAN_RECORD_DATE = CLEAN_RECORD_START_UTC[:10]

#: First row on/after the clean-record date, and the row just before it.
#: Bind parameter: CLEAN_RECORD_DATE.
CLEAN_BASELINE_FIRST_SQL = "SELECT * FROM daily_pnl WHERE date >= ? ORDER BY date ASC LIMIT 1"
CLEAN_BASELINE_PREV_SQL = "SELECT * FROM daily_pnl WHERE date < ? ORDER BY date DESC LIMIT 1"

#: The broker read falls back to last_equity = portfolio_value when it returns
#: nothing, which records daily_pnl = 0 and a baseline equal to that day's
#: close. A real baseline must equal the previous row's recorded close.
_CLOSE_MATCH_TOLERANCE = 0.05


def _label(day: str, *, close: bool) -> str | None:
    try:
        d = date.fromisoformat(str(day)[:10])
    except ValueError:
        return None
    text = f"{d:%a} {d.day} {d:%b}"
    return f"{text} close" if close else text


def clean_record_baseline(first: dict | None, prev: dict | None) -> tuple[float, str] | None:
    """`(baseline, since_label)` or None when the baseline cannot be trusted.

    `first` is the first daily_pnl row on/after the clean-record date, `prev`
    the row just before it (None when the table has none). None means the
    total must show as unknown, never a guess.
    """
    if not first:
        return None
    try:
        baseline = float(first["total_value"]) - float(first["daily_pnl"])
    except (TypeError, ValueError, KeyError):
        return None
    if not (baseline > 0) or not math.isfinite(baseline):
        return None
    if not prev:
        label = _label(first.get("date"), close=False)
        return (baseline, label) if label else None
    close = prev.get("equity_close")
    if close is None:
        close = prev.get("total_value")
    try:
        close = float(close)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(close) or abs(baseline - close) > _CLOSE_MATCH_TOLERANCE:
        return None
    label = _label(prev.get("date"), close=True)
    return (baseline, label) if label else None
