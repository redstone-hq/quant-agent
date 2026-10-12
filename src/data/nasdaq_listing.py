"""Nasdaq all-listings loader: two requests per run replace per-stock profile lookups.

REPLAY SEAM (board item 202): this module imports no HTTP client. The caller
INJECTS ``fetch`` -- there is deliberately no default -- so a replay serves the
two documents from a recording and a live run passes a real fetcher. The live
fetcher is built where the daily screen is wired (a later change) and must go
through the rehearsal seam before it lands.

``fetch(url, headers)`` returns the parsed JSON body (the fetcher owns its timeout) and raises on an
HTTP error or timeout. Any failure, malformed body or empty row list raises
``ListingUnavailable``; an empty ``Listing`` is never returned.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

from src.universe_screen import _ADR_NAME, _DEPOSITARY_WORD, _NON_COMMON_NAME

STOCKS_URL = "https://api.nasdaq.com/api/screener/stocks?tableonly=true&download=true"
FUNDS_URL = "https://api.nasdaq.com/api/screener/etf?tableonly=true&download=true"
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
    "Accept": "application/json",
}

FUND_CATEGORY_UNKNOWN = "UNKNOWN"
_PREFERRED_WORDS = {"preferred", "pfd"}


class ListingUnavailable(RuntimeError):
    """The Nasdaq listing could not be fetched or parsed; the screen must record inconclusive."""


@dataclass(frozen=True)
class ListingRecord:
    symbol: str
    name: str
    market_cap: Decimal | None
    is_fund: bool
    sector_recorded: str | None
    is_preferred: bool = False
    fund_category: str | None = None


@dataclass(frozen=True)
class Listing:
    records: dict[str, ListingRecord] = field(default_factory=dict)

    def get(self, symbol: str) -> ListingRecord | None:
        return self.records.get(symbol)

    def __len__(self) -> int:
        return len(self.records)


def normalise_symbol(raw: str) -> str:
    """Nasdaq 'BRK/B' -> the desk's class-share form 'BRK-B' (as universe_screen._asset_symbol)."""
    s = raw.strip().upper()
    return re.sub(r"^([A-Z]+)[/.]([A-Z])$", r"\1-\2", s)


def _market_cap(raw) -> Decimal | None:
    text = str(raw or "").replace(",", "").replace("$", "").strip()
    if not text:
        return None
    try:
        value = Decimal(text)
    except InvalidOperation:
        return None
    return value if value.is_finite() and value > 0 else None


def _is_preferred(symbol: str, name: str) -> bool:
    if "^" in symbol:
        return True
    if any(m.group(1).lower() in _PREFERRED_WORDS for m in _NON_COMMON_NAME.finditer(name)):
        return True
    # A depositary name that is not an ADR/ADS wrapper is a preferred wrapper.
    return bool(_DEPOSITARY_WORD.search(name)) and not _ADR_NAME.search(name)


def _rows(body, *path: str) -> list[dict]:
    node = body
    for key in path:
        node = node.get(key) if isinstance(node, dict) else None
    if not isinstance(node, list) or not node:
        raise ListingUnavailable(f"no rows at {'.'.join(path)}")
    return [r for r in node if isinstance(r, dict)]


def _fetch(fetch: Callable, url: str):
    try:
        return fetch(url, HEADERS)
    except Exception as exc:  # any transport failure is the same outcome
        raise ListingUnavailable(f"fetch failed for {url}: {exc!r}") from exc


def load_listing(*, fetch: Callable[[str, dict], object]) -> Listing:
    stock_rows = _rows(_fetch(fetch, STOCKS_URL), "data", "rows")
    fund_rows = _rows(_fetch(fetch, FUNDS_URL), "data", "data", "rows")
    records: dict[str, ListingRecord] = {}
    for row in stock_rows:
        raw = str(row.get("symbol") or "").strip()
        if not raw:
            continue
        sym = normalise_symbol(raw)
        name = str(row.get("name") or "").strip()
        records[sym] = ListingRecord(
            symbol=sym,
            name=name,
            market_cap=_market_cap(row.get("marketCap")),
            is_fund=False,
            sector_recorded=str(row.get("sector") or "").strip() or None,
            is_preferred=_is_preferred(raw, name),
        )
    for row in fund_rows:
        raw = str(row.get("symbol") or "").strip()
        if not raw:
            continue
        sym = normalise_symbol(raw)
        records[sym] = ListingRecord(
            symbol=sym,
            name=str(row.get("companyName") or "").strip(),
            market_cap=None,
            is_fund=True,
            sector_recorded=None,
            fund_category=FUND_CATEGORY_UNKNOWN,
        )
    if not records:
        raise ListingUnavailable("listing parsed to zero symbols")
    return Listing(records)
