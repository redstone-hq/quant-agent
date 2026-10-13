"""One-paragraph company profiles — who a ticker actually is.

Every agent in this system reasons about `CCJ` and `PATH` as price series with
a sector tag attached. So does every message the operator receives. Nobody —
model or human — is told that CCJ is Cameco, a Canadian uranium miner founded
in 1988, or that PATH is UiPath, an enterprise automation software company
that IPO'd in 2021 and has never been consistently profitable.

That is a real gap, not a cosmetic one. "Utilities" covers both a regulated
water utility and a merchant power trader with commodity exposure; "Energy"
covers an integrated major and a pre-revenue nuclear startup. A sector label
alone lets an analyst reach for the wrong prior with complete confidence.

Cached on disk because these facts change on the order of years, and paying a
network round trip per symbol per session for a business description that has
not moved since 1988 is waste. Every field degrades to None independently:
a missing profile must never block a trade, and a partial one is still
better than a ticker with no identity at all.
"""

from __future__ import annotations

import json
import logging
import time
from src.trading_calendar import utc_now
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeout
from dataclasses import asdict, dataclass
from pathlib import Path
from src.sentinel.counted import record_swallowed

logger = logging.getLogger(__name__)

#: Same ceiling the valuation and ex-div fetches use — a stalled network call
#: must not eat the morning session's budget.
_FETCH_TIMEOUT_S = 10

#: Profiles are re-fetched this rarely because business descriptions are
#: near-static. A stale market cap is far cheaper than a blocked session.
_CACHE_TTL_DAYS = 30

_SUMMARY_MAX_CHARS = 320


@dataclass(frozen=True)
class CompanyProfile:
    """What a trader would want to know before reading a chart."""

    symbol: str
    name: str | None = None
    sector: str | None = None
    industry: str | None = None
    country: str | None = None
    founded: int | None = None
    employees: int | None = None
    market_cap: float | None = None
    summary: str | None = None
    is_etf: bool = False

    def as_dict(self) -> dict:
        return asdict(self)

    def render(self) -> str:
        """One compact line plus a business description. Facts only."""
        if not any((self.name, self.summary, self.industry)):
            return f"- {self.symbol}: no company profile available"
        bits: list[str] = []
        if self.industry:
            bits.append(self.industry)
        if self.country and self.country != "United States":
            bits.append(self.country)
        if self.founded:
            bits.append(f"founded {self.founded}")
        if self.employees:
            bits.append(f"{self.employees:,} employees")
        if self.market_cap:
            bits.append(_format_cap(self.market_cap))
        head = f"- **{self.symbol} — {self.name or self.symbol}**"
        if self.is_etf:
            head += " (ETF)"
        meta = f" · {' · '.join(bits)}" if bits else ""
        body = f"\n  {self.summary}" if self.summary else ""
        return f"{head}{meta}{body}"


def _format_cap(value: float) -> str:
    for threshold, suffix in ((1e12, "T"), (1e9, "B"), (1e6, "M")):
        if value >= threshold:
            return f"${value / threshold:.1f}{suffix} cap"
    return f"${value:,.0f} cap"


def _truncate(text: str | None) -> str | None:
    """First sentences of the business description, up to the cap.

    Cuts on a sentence boundary rather than mid-word: a description severed
    at "provides cloud infrastructure and" reads as though the fetch broke.
    """
    if not text:
        return None
    text = " ".join(str(text).split())
    if len(text) <= _SUMMARY_MAX_CHARS:
        return text
    cut = text[:_SUMMARY_MAX_CHARS]
    for boundary in (". ", "; "):
        idx = cut.rfind(boundary)
        if idx > _SUMMARY_MAX_CHARS * 0.5:
            return cut[: idx + 1]
    return cut.rsplit(" ", 1)[0] + "…"


class CompanyProfileStore:
    """Disk-cached profile lookup. Never raises; never blocks a trade."""

    def __init__(self, cache_path: str = "data/company_profiles.json"):
        self.cache_path = Path(cache_path)
        self._cache: dict = self._load()

    def _load(self) -> dict:
        try:
            if self.cache_path.exists():
                return json.loads(self.cache_path.read_text()) or {}
        except Exception as e:  # noqa: BLE001
            logger.warning("company profile cache unreadable (%s) — starting empty", e)
        return {}

    def _save(self) -> None:
        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            self.cache_path.write_text(json.dumps(self._cache, indent=1, sort_keys=True))
        except Exception as e:  # noqa: BLE001
            logger.warning("company profile cache unwritable: %s", e)

    def _fresh(self, entry: dict) -> bool:
        try:
            age_days = (utc_now().timestamp() - float(entry.get("_fetched_at", 0))) / 86400
        except (TypeError, ValueError):
            return False
        return age_days < _CACHE_TTL_DAYS

    def get(self, symbol: str, *, allow_fetch: bool = True) -> CompanyProfile:
        symbol = str(symbol).strip().upper()
        entry = self._cache.get(symbol)
        if entry and self._fresh(entry):
            return CompanyProfile(**{k: v for k, v in entry.items() if not k.startswith("_")})
        if not allow_fetch:
            return CompanyProfile(symbol=symbol)
        profile = self._fetch(symbol)
        payload = profile.as_dict()
        payload["_fetched_at"] = utc_now().timestamp()
        self._cache[symbol] = payload
        self._save()
        return profile

    def peek_cached(self, symbol: str) -> CompanyProfile:
        """Return the on-disk identity even if the cache is stale. Never fetches.

        Mission Control uses this so a name that aged past the fetch TTL is
        still the company's name rather than a blank ticker. A missing or
        unreadable entry degrades to `name=None` — never invented.
        """
        symbol = str(symbol).strip().upper()
        entry = self._cache.get(symbol)
        if not isinstance(entry, dict):
            return CompanyProfile(symbol=symbol)
        try:
            return CompanyProfile(**{k: v for k, v in entry.items() if not str(k).startswith("_")})
        except TypeError:
            return CompanyProfile(symbol=symbol)

    def get_many(self, symbols, *, allow_fetch: bool = True) -> dict[str, CompanyProfile]:
        return {
            s: self.get(s, allow_fetch=allow_fetch)
            for s in dict.fromkeys(str(x).strip().upper() for x in symbols if str(x).strip())
        }

    @staticmethod
    def _fetch(symbol: str) -> CompanyProfile:
        def _work() -> dict:
            try:
                import yfinance as yf

                return yf.Ticker(symbol).info or {}
            except Exception as e:  # noqa: BLE001
                record_swallowed("data.company._work", e, log=logger)
                return {}

        info: dict = {}
        try:
            with ThreadPoolExecutor(max_workers=1) as ex:
                info = ex.submit(_work).result(timeout=_FETCH_TIMEOUT_S)
        except FuturesTimeout:
            logger.warning("company profile fetch timed out for %s", symbol)
            info = {}
        except Exception as e:  # noqa: BLE001
            logger.warning("company profile fetch errored for %s: %s", symbol, e)
            info = {}

        def _int(value):
            try:
                out = int(value)
            except (TypeError, ValueError):
                return None
            return out if out > 0 else None

        def _float(value):
            try:
                out = float(value)
            except (TypeError, ValueError):
                return None
            return out if out > 0 else None

        quote_type = str(info.get("quoteType") or "").upper()
        is_etf = quote_type == "ETF"
        summary = info.get("longBusinessSummary") or info.get("description")
        return CompanyProfile(
            symbol=symbol,
            name=info.get("longName") or info.get("shortName"),
            sector=info.get("sector"),
            industry=info.get("industry") or ("Exchange-traded fund" if is_etf else None),
            country=info.get("country"),
            founded=_int(info.get("yearFounded") or info.get("foundedYear")),
            employees=_int(info.get("fullTimeEmployees")),
            market_cap=_float(info.get("marketCap") or info.get("totalAssets")),
            summary=_truncate(summary),
            is_etf=is_etf,
        )


def format_profiles_block(profiles, title: str = "Who these companies are") -> str:
    """Render profiles for an agent prompt or an operator message."""
    entries = [p for p in profiles if p is not None]
    if not entries:
        return ""
    lines = [f"## {title}"]
    lines.extend(p.render() for p in sorted(entries, key=lambda x: x.symbol))
    return "\n".join(lines) + "\n"
