"""Fund categories: the one persistent store, and the refuse-only name backstop.

WHY. The Nasdaq listing flags a fund but names no category, so until
2026-10-13 every fund was recorded `fund_category_not_listed` (inconclusive)
and none was ever admitted. Yahoo's Morningstar category ("Large Blend",
"Trading--Leveraged Equity") is the only free source that marks leverage,
and only the nightly pass may read Yahoo (the side door never does). This
module is the seam between them: the nightly pass fills the store from
Yahoo, once per fund, and both admission paths read the store.

Table `fund_category` in the desk database (`storage.db_path`):
    symbol      desk spelling (BRK.B style, as the Nasdaq listing has it)
    category    Yahoo's category text, never blank
    fetched_at  ISO date the category was read
    source      where it came from ("yahoo")

A stored plain-equity category passes `check_fund`; a leveraged or inverse
one stays refused. A fund with no row is inconclusive: the store never
invents a category, and the name backstop below only ever refuses.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from dataclasses import dataclass
from datetime import date
from pathlib import Path

logger = logging.getLogger(__name__)

SOURCE_YAHOO = "yahoo"

#: Refuse-only name backstop. A fund whose NAME carries a leverage or
#: inverse word is refused even before its category is known: "2X", "3X",
#: "-1X", Ultra / UltraPro / UltraShort (ProShares), Bull / Bear (Direxion),
#: Inverse, or "Daily" together with a leverage word (Direxion "Daily ...
#: Bull 3X"). It never admits: a plain name says nothing about leverage.
_LEVERAGE_WORD = re.compile(
    r"(?<![\w.])(?:-?\d+(?:\.\d+)?x|ultra(?:pro|short)?|bull|bear|inverse|leveraged)(?![\w])",
    re.IGNORECASE,
)
_DAILY = re.compile(r"\bdaily\b", re.IGNORECASE)
_DAILY_LEVERAGE = re.compile(r"\b(?:long|short|leveraged)\b", re.IGNORECASE)


def is_leveraged_fund_name(name) -> bool:
    """True when the fund's name marks leverage or an inverse mandate."""
    text = str(name or "")
    if not text:
        return False
    if _LEVERAGE_WORD.search(text):
        return True
    return bool(_DAILY.search(text) and _DAILY_LEVERAGE.search(text))


@dataclass(frozen=True)
class FundCategory:
    symbol: str
    category: str
    fetched_at: date
    source: str


class FundCategoryStore:
    """`fund_category` rows in the desk database. `db_path` None = no store:
    every read is a miss, every write is dropped with a warning."""

    def __init__(self, db_path: str | Path | None):
        self.conn: sqlite3.Connection | None = None
        if not db_path:
            return
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(db_path), timeout=30)
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS fund_category ("
            "symbol TEXT PRIMARY KEY, category TEXT NOT NULL, fetched_at TEXT NOT NULL, source TEXT NOT NULL)"
        )
        self.conn.commit()

    def get(self, symbol: str) -> FundCategory | None:
        if self.conn is None:
            return None
        row = self.conn.execute(
            "SELECT symbol, category, fetched_at, source FROM fund_category WHERE symbol = ?", (symbol,)
        ).fetchone()
        if row is None:
            return None
        return FundCategory(row[0], row[1], date.fromisoformat(row[2]), row[3])

    def put(self, symbol: str, category: str, fetched_at: date, source: str = SOURCE_YAHOO) -> None:
        text = str(category or "").strip()
        if not text:
            raise ValueError(f"fund category for {symbol} is blank")
        if self.conn is None:
            logger.warning("fund category store: no database; %s=%r not kept", symbol, text)
            return
        self.conn.execute(
            "INSERT INTO fund_category (symbol, category, fetched_at, source) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(symbol) DO UPDATE SET category=excluded.category, "
            "fetched_at=excluded.fetched_at, source=excluded.source",
            (symbol, text, fetched_at.isoformat(), source),
        )
        self.conn.commit()

    def close(self) -> None:
        if self.conn is not None:
            self.conn.close()
            self.conn = None
