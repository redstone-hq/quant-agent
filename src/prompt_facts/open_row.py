"""The row that opened a held position — shared by the prompt-fact builders.

Lives below both `src.pipeline_prompt_facts` and `src.prompt_facts.*` so the
evening thesis review and the morning/midday facts read the same open row
without an import cycle.
"""

from __future__ import annotations

import logging

logger = logging.getLogger("src.pipeline")


def _position_open_row(db, row: dict | None, sym: str, opening: str) -> dict | None:
    """The row that OPENED the held position, so no entry field is read off an add.

    A scale-in writes a second opening row on the same `position_id`, and
    both `get_symbol_last_buy` and today's morning row return the NEWEST one.
    Measured 13 Oct 2026: AAPL opened 12 Oct, added 13 Oct 10:26 ET, and the
    1 pm and 3:31 pm reviewer prompts showed days_held=0, sessions_held=0 and
    the add's target — a day-old trade at -6% presented as a fresh entry.
    Age, initial stop, initial target, setup and thesis all belong to the
    open; average cost stays the broker's blended `avg_entry`.

    A morning row may be an in-memory dict without a `position_id`, so the
    chain is anchored on the newest DB row when it has none. Any miss (legacy
    row, unchainable, lookup error) FAILS CLOSED onto `row` — today's
    behaviour — never a guessed chain boundary. Same lookup as the trail
    (`src/exits_parts/trails.py`) and the alignment exit.
    """
    try:
        anchor = row if (row or {}).get("position_id") else db.get_symbol_last_buy(sym, action=opening)
        open_row = db.get_position_open_row(anchor or row)
    except Exception as e:  # noqa: BLE001 — falls back to the newest opening row
        logger.warning("open-row lookup failed for %s: %s — using newest opening row", sym, e)
        return row
    return open_row if isinstance(open_row, dict) else row
