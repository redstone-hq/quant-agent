"""Daily record-only universe screen — STEP A of the owner's 2026-10-09 ruling.

Owner ruling 2026-10-09: a free, AI-free filter over all US stocks and plain
equity ETFs, run fresh every trading day, dropping only clearly dead names,
with every dropped name recorded with its reason. The "is anything
happening" cut-off is not designed yet, so this module only RECORDS: it runs
the existing eligibility checks of `src/universe_screen.py` (asset, history,
price, spread, volatility, S&P liquidity ratio, size, sector, plain equity fund,
takeover; the session screen runs the liquidity ratio first, here the free
bar checks run in the bars pass and the ratio opens the per-name pass) over the
whole Alpaca active US-equity asset list (read through the read-only broker
the API uses) and writes one durable record per
name. It sends nothing to any AI, admits nothing, removes nothing and never
touches `universe_state.json`; `universe_screen.enabled` still governs the
session path alone. No new threshold: every limit is `ScreenThresholds`.

Data and call budget:
  asset list   one Alpaca GET (`broker.list_assets`).
  daily bars   `broker.get_bars_batch` - Alpaca multi-symbol daily bars,
               `universe_screen.bars_batch_size` symbols per request, chunks
               read one after another (the SDK exposes no rate-limit
               headers), pages followed by next-page token and each page
               counted. The client is built inside the broker method, where
               the rehearsal replay patches it. A failed chunk (HTTP 429 or
               other) is recorded `market_data_unavailable` per name; there
               is no retry loop. No Yahoo bar download is made.
  size/sector  The Nasdaq all-listings documents (stocks and ETFs), two GETs
               per run through `src.data.nasdaq_fetch`; no per-name call.
               Nasdaq lists no fund category, and gives some stocks a blank
               or "Miscellaneous" sector (BRK-B, FERG). If the download fails
               every name is inconclusive `profile_unavailable` and the day
               is NOT marked recorded, so the next wake-up retries.
  Yahoo gap    A second, bounded pass (owner ruling 2026-10-10: wrongly
               dropped names go back in). ONLY a name that passed every
               other check and is (a) a fund with no category or (b) a stock
               whose Nasdaq sector is Unknown gets one yfinance `.info` read
               (`category` for a fund, `sector` for a stock), one after
               another with yfinance's own timeout. The FIRST rate limit
               (HTTP 429) stops Yahoo for the rest of the run: every name not
               yet read is recorded unreached (never admitted), and the
               ISO-week cache (the SEC filings rule) fills across runs. It
               runs after every other read, so it never eats their time
               budget. A
               fund category goes through `check_fund` (leveraged, inverse,
               bond, T-bill and money-market words refuse); a stock sector is
               used as is (Yahoo's names ARE the desk's). An error, rate
               limit or blank answer records the name inconclusive
               `profile_unavailable` ("could not read"): never admitted,
               never dropped. The 9 Oct record had 2,780 such funds and 37
               such stocks; the full per-name pass removed in #1661 read all
               ~14,000 names.
  takeover     SEC issuer filing history, cached per ISO week. Calls counted.

Records: `<universe_screen.data_dir>/daily/<YYYY-MM-DD>.jsonl` (one line per
listed name: passed, dropped with its failing check(s), inconclusive on a
data outage, or "unreached: time limit") and `<YYYY-MM-DD>.summary.json`
(names listed, passed, dropped per reason, calls made, seconds taken).

Run: `python -m src.universe_daily` — a no-op unless today is a trading day
on the broker's calendar, the session has closed, and today has no record
yet, so any wake-up tick may call it.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable

from src.universe_screen import (
    INCONCLUSIVE,
    ScreenSources,
    ScreenThresholds,
    _asset_symbol,
    _field,
    check_asset,
    check_bars,
    check_fund,
    iso_week,
    screen_symbol,
)

logger = logging.getLogger(__name__)

UNREACHED = "unreached_time_limit"
UNREACHED_TEXT = "unreached: time limit"
YAHOO_LIMITED = "unreached_yahoo_rate_limit"
YAHOO_LIMITED_TEXT = "unreached: Yahoo rate limit earlier this run"
_READ_FAILED = object()  # a raised Yahoo read, already recorded for the name

#: Stand-in sector while a stock with no Nasdaq sector runs its remaining
#: (takeover) check; always replaced before the record is written.
_SECTOR_PENDING = "pending: Yahoo sector"


# --------------------------------------------------------------------------
# Cache (SEC filings change slowly)
# --------------------------------------------------------------------------


class ReadCache:
    """{kind: {symbol: {"week", "on", "value"}}} at `<dir>/daily_cache.json`."""

    def __init__(self, data_dir: str | Path):
        self.path = Path(data_dir) / "daily_cache.json"
        try:
            raw = json.loads(self.path.read_text()) if self.path.exists() else {}
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("universe daily: cache unreadable at %s: %s", self.path, exc)
            raw = {}
        self.data = {k: dict(raw.get(k) or {}) for k in ("filings", "yahoo")}

    def get(self, kind: str, symbol: str) -> dict | None:
        return self.data[kind].get(symbol)

    def put(self, kind: str, symbol: str, value, today: date) -> None:
        self.data[kind][symbol] = {"week": iso_week(today), "on": today.isoformat(), "value": value}

    def save(self) -> None:
        _atomic_write(self.path, json.dumps(self.data, sort_keys=True))


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# --------------------------------------------------------------------------
# One daily run
# --------------------------------------------------------------------------


class _Unreached(BaseException):
    """A network read was due after the time limit. BaseException on purpose:
    `screen_symbol` turns any Exception from a source into a data-outage
    code, and running out of time is not an outage."""


@dataclass
class DailyRun:
    today: date
    records: dict[str, dict] = field(default_factory=dict)
    calls: dict[str, int] = field(
        default_factory=lambda: {"alpaca_assets": 0, "bar_batches": 0, "nasdaq_listing": 0, "sec_filings": 0}
    )
    #: Second-pass Yahoo reads: names due one, calls made (retries count),
    #: failures (error, rate limit or blank) and cache hits.
    yahoo: dict[str, int] = field(default_factory=lambda: {"names": 0, "calls": 0, "failures": 0, "cache_hits": 0})
    seconds: float = 0.0
    deadline_hit: bool = False
    listing_unavailable: bool = False

    def record(self, symbol: str, status: str, failures=(), measured=None, **extra) -> None:
        row = {"symbol": symbol, "status": status, "failures": list(failures), "measured": dict(measured or {})}
        if status == "unreached":
            row["reason"] = UNREACHED_TEXT
        row.update(extra)
        self.records[symbol] = row

    def summary(self) -> dict:
        statuses: dict[str, int] = {}
        dropped_any: dict[str, int] = {}
        dropped_first: dict[str, int] = {}
        for row in self.records.values():
            statuses[row["status"]] = statuses.get(row["status"], 0) + 1
            if row["status"] == "dropped":
                for code in row["failures"]:
                    dropped_any[code] = dropped_any.get(code, 0) + 1
                first = row["failures"][0]
                dropped_first[first] = dropped_first.get(first, 0) + 1
        return {
            "date": self.today.isoformat(),
            "names_listed": len(self.records),
            "passed": statuses.get("passed", 0),
            "dropped": statuses.get("dropped", 0),
            "inconclusive": statuses.get("inconclusive", 0),
            "unreached": statuses.get("unreached", 0),
            "dropped_by_first_reason": dict(sorted(dropped_first.items())),
            "dropped_by_any_reason": dict(sorted(dropped_any.items())),
            "calls": dict(self.calls),
            "yahoo_second_pass": dict(self.yahoo),
            "seconds": round(self.seconds, 1),
            "deadline_hit": self.deadline_hit,
        }


def _status(failures: list[str]) -> str:
    if not failures:
        return "passed"
    if any(code in INCONCLUSIVE for code in failures):
        return "inconclusive"
    return "dropped"


def run_daily_record(
    assets: list,
    *,
    get_bars_batch: Callable[[list[str]], dict[str, list]],
    get_profile: Callable[[str], Any],
    get_filings: Callable[[str], Any],
    th: ScreenThresholds,
    today: date,
    deadline: float,
    batch_size: int,
    cache: ReadCache,
    clock: Callable[[], float] = time.monotonic,
    get_yahoo: Callable[[str], dict] | None = None,
    get_role: Callable[[Any], str] | None = None,
) -> DailyRun:
    """Screen every listed name once; record each. Never mutates admission state.

    `get_yahoo(symbol) -> yfinance .info dict` fills the Yahoo gap (module
    docstring); None leaves those names as the listing alone records them."""
    started = clock()
    screen = _DailyScreen(
        run=DailyRun(today=today),
        th=th,
        cache=cache,
        deadline=deadline,
        clock=clock,
        get_profile=get_profile,
        get_filings=get_filings,
        get_yahoo=get_yahoo,
        get_role=get_role,
    )
    by_symbol: dict[str, Any] = {}
    for asset in assets:
        symbol = _asset_symbol(asset)
        if symbol:
            by_symbol[symbol] = asset
    needs_bars = screen.asset_phase(by_symbol)
    bars = screen.bars_phase(needs_bars, get_bars_batch, max(1, int(batch_size)))
    screen.detail_phase(by_symbol, bars)
    screen.yahoo_phase()
    screen.run.seconds = clock() - started
    return screen.run


@dataclass
class _DailyScreen:
    run: DailyRun
    th: ScreenThresholds
    cache: ReadCache
    deadline: float
    clock: Callable[[], float]
    get_profile: Callable[[str], Any]
    get_filings: Callable[[str], Any]
    get_yahoo: Callable[[str], dict] | None = None
    get_role: Callable[[Any], str] | None = None
    # The time limit is checked when a name STARTS: a name begun in time
    # finishes its reads; one begun late uses only what is cached.
    late: bool = False
    # Names that passed every other check and wait for a Yahoo read:
    # (symbol, "fund" | "stock", measured, extra record fields).
    yahoo_due: list = field(default_factory=list)
    yahoo_blocked: bool = False

    def asset_phase(self, by_symbol: dict[str, Any]) -> list[str]:
        """Asset checks: free, the list is already in hand."""
        needs_bars = []
        for symbol in sorted(by_symbol):
            failures = check_asset(symbol, by_symbol[symbol])
            if failures:
                self.run.record(symbol, _status(failures), failures)
            else:
                needs_bars.append(symbol)
        return needs_bars

    def bars_phase(self, symbols: list[str], get_bars_batch, step: int) -> dict[str, list]:
        """Bars, many symbols per request; then the bar checks (no network)."""
        passed: dict[str, list] = {}
        for start in range(0, len(symbols), step):
            chunk = symbols[start : start + step]
            if self.clock() >= self.deadline:
                self.run.deadline_hit = True
                for symbol in chunk:
                    self.run.record(symbol, "unreached", [UNREACHED])
                continue
            try:
                got = get_bars_batch(chunk) or {}
            except Exception as exc:  # noqa: BLE001 -- recorded per name as an outage
                logger.warning("universe daily: bar batch failed (%d symbols): %s", len(chunk), exc)
                for symbol in chunk:
                    self.run.record(symbol, "inconclusive", ["market_data_unavailable"])
                continue
            for symbol in chunk:
                failures, measured = check_bars(got.get(symbol) or [], self.th)
                if failures:
                    self.run.record(symbol, _status(failures), failures, measured)
                else:
                    passed[symbol] = got[symbol]
        return passed

    def _age(self, symbol: str, kind: str = "filings") -> tuple:
        entry = self.cache.get(kind, symbol)
        if entry is None:
            return (2, "", symbol)
        return (0 if entry.get("week") == iso_week(self.run.today) else 1, entry.get("on") or "", symbol)

    def _cached_read(self, kind: str, symbol: str, fetch, counter: str, keep_none: bool):
        entry = self.cache.get(kind, symbol)
        if entry is not None and (entry.get("week") == iso_week(self.run.today) or self.late):
            return entry["value"], entry.get("on")
        if self.late:
            raise _Unreached()
        self.run.calls[counter] += 1
        value = fetch(symbol)
        if value is not None or keep_none:
            self.cache.put(kind, symbol, value, self.run.today)
        return value, self.run.today.isoformat()

    def detail_phase(self, by_symbol: dict[str, Any], bars: dict[str, list]) -> None:
        """Filings: cached this week first (no network), then the
        oldest reads, then names never read."""
        for symbol in sorted(bars, key=self._age):
            self.late = self.clock() >= self.deadline
            if self.late:
                self.run.deadline_hit = True
            read_on: dict[str, str | None] = {}

            def _filings(s, _r=read_on):
                value, _r["filings_read_on"] = self._cached_read("filings", s, self.get_filings, "sec_filings", True)
                return value

            seen: dict[str, Any] = {}

            def _profile(s, _seen=seen):
                _seen["profile"] = self.get_profile(s)
                return _seen["profile"]

            sources = ScreenSources(
                get_asset=lambda s: None,
                get_bars=lambda s: [],
                get_profile=_profile,
                get_filings=_filings,
                get_role=self.get_role,
            )
            try:
                result = screen_symbol(symbol, sources, self.th, asset=by_symbol[symbol], bars=bars[symbol])
                failures, measured = result.failures, result.measured
                kind = None
                if self.get_yahoo is not None and failures == ["fund_category_not_listed"]:
                    kind = "fund"  # a fund's category is its last check
                elif self.get_yahoo is not None and failures == ["unresolved_sector"]:
                    # Run the remaining (takeover) check with a stand-in
                    # sector, so Yahoo is read only for a name that passed it.
                    held = {**seen["profile"], "sector": _SECTOR_PENDING}
                    sources.get_profile = lambda s, _p=held: _p
                    rerun = screen_symbol(symbol, sources, self.th, asset=by_symbol[symbol], bars=bars[symbol])
                    measured = {**rerun.measured, "sector": "Unknown"}
                    if rerun.failures:
                        failures = ["unresolved_sector", *rerun.failures]
                    else:
                        kind = "stock"
            except _Unreached:
                self.run.record(symbol, "unreached", [UNREACHED])
                continue
            self.run.record(symbol, _status(failures), failures, measured, **read_on)
            if kind is not None:
                self.yahoo_due.append((symbol, kind, measured, read_on))

    # ----------------------------------------------------------------------
    # Yahoo gap: second pass, bounded to names that passed every other check
    # ----------------------------------------------------------------------

    def yahoo_phase(self) -> None:
        """Fill a fund's category / a stock's sector from Yahoo; fail closed."""
        from src.models import _ALLOWED_SECTORS

        self.run.yahoo["names"] = len(self.yahoo_due)
        for symbol, kind, measured, read_on in sorted(self.yahoo_due, key=lambda d: self._age(d[0], "yahoo")):
            late = self.clock() >= self.deadline
            if late:
                self.run.deadline_hit = True
            entry = self.cache.get("yahoo", symbol)
            if entry is not None and (entry.get("week") == iso_week(self.run.today) or late):
                self.run.yahoo["cache_hits"] += 1
                value, read_on_day = entry["value"], entry.get("on")
            elif late:
                self.run.record(symbol, "unreached", [UNREACHED])
                continue
            else:
                value, read_on_day = self._yahoo_read(symbol, kind, measured, read_on), self.run.today.isoformat()
                if self.yahoo_blocked:
                    self.run.record(symbol, "unreached", [YAHOO_LIMITED], measured, reason=YAHOO_LIMITED_TEXT)
                    continue
                if value is _READ_FAILED:
                    continue
            extra = {**read_on, "yahoo_read_on": read_on_day if value else None}
            text = str((value or {}).get(kind) or "").strip() if isinstance(value, dict) else ""
            if kind == "fund" and text:
                failures, got = check_fund(text)
                self.run.record(symbol, _status(failures), failures, {**measured, **got}, **extra)
            elif kind == "stock" and text in _ALLOWED_SECTORS:
                self.run.record(symbol, "passed", [], {**measured, "sector": text}, **extra)
            else:
                # Error, rate limit, blank or an unknown sector name: could not read.
                self.run.record(symbol, "inconclusive", ["profile_unavailable"], measured, **extra)

    def _yahoo_read(self, symbol: str, kind: str, measured: dict, read_on: dict):
        """One `.info` read -> {kind: text}, cached when not blank; None when blank.
        A raised read is recorded for the name, with Yahoo's own error text, and
        returns `_READ_FAILED`. The first rate limit sets `yahoo_blocked`: no
        further read this run."""
        if self.yahoo_blocked:
            return None
        self.run.yahoo["calls"] += 1
        try:
            info = self.get_yahoo(symbol) or {}
        except Exception as exc:  # noqa: BLE001 -- recorded per name as could-not-read
            self.run.yahoo["failures"] += 1
            if _is_rate_limit(exc):
                logger.warning("universe daily: Yahoo rate limit on %s; no more Yahoo reads this run", symbol)
                self.yahoo_blocked = True
            else:
                logger.warning("universe daily: Yahoo read failed for %s: %s", symbol, exc)
            self.run.record(
                symbol,
                "inconclusive",
                ["profile_unavailable"],
                measured,
                reason=f"Yahoo read failed: {type(exc).__name__}: {exc}",
                **{**read_on, "yahoo_read_on": None},
            )
            return _READ_FAILED
        key = "category" if kind == "fund" else "sector"
        text = str(info.get(key) or "").strip() if isinstance(info, dict) else ""
        if not text:
            self.run.yahoo["failures"] += 1
            return None
        value = {kind: text}
        self.cache.put("yahoo", symbol, value, self.run.today)
        return value


def _is_rate_limit(exc: BaseException) -> bool:
    text = str(exc).lower()
    return type(exc).__name__ == "YFRateLimitError" or "429" in text or "too many requests" in text


# --------------------------------------------------------------------------
# Durable record
# --------------------------------------------------------------------------


def record_dir(data_dir: str | Path) -> Path:
    return Path(data_dir) / "daily"


def already_recorded(data_dir: str | Path, today: date) -> bool:
    return (record_dir(data_dir) / f"{today.isoformat()}.summary.json").exists()


def write_record(data_dir: str | Path, run: DailyRun) -> dict:
    """Per-name lines first, totals last: the summary file marks a complete day."""
    folder = record_dir(data_dir)
    stamp = run.today.isoformat()
    lines = "".join(json.dumps(run.records[s], sort_keys=True, default=str) + "\n" for s in sorted(run.records))
    _atomic_write(folder / f"{stamp}.jsonl", lines)
    summary = run.summary()
    if run.listing_unavailable:
        logger.warning("universe daily: %s listing unavailable; day not marked recorded, next run retries", stamp)
        return summary
    summary["written_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    _atomic_write(folder / f"{stamp}.summary.json", json.dumps(summary, indent=1, sort_keys=True))
    logger.info(
        "UNIVERSE_DAILY %s %s",
        stamp,
        json.dumps(
            {
                k: summary[k]
                for k in ("names_listed", "passed", "dropped", "inconclusive", "unreached", "calls", "seconds")
            },
            sort_keys=True,
        ),
    )
    return summary


# --------------------------------------------------------------------------
# Entry point (timer wake-up)
# --------------------------------------------------------------------------


def _due_today(broker, data_dir, today: date) -> bool:
    """A trading day on the broker's calendar, after its close, not yet recorded."""
    if already_recorded(data_dir, today):
        logger.info("universe daily: %s already recorded", today)
        return False
    if not broker.is_trading_day(today):
        logger.info("universe daily: %s is not a trading day", today)
        return False
    close = broker.get_session_close(today)
    if close is None or datetime.now(timezone.utc) < close:
        logger.info("universe daily: session not closed yet (close=%s)", close)
        return False
    return True


def _profile_reader(listing, canonicalize):
    """Profile in the shape `check_profile` reads, from one Nasdaq listing
    (None when the download failed: every name is then unavailable)."""

    def _profile(symbol: str):
        if listing is None:
            return None
        rec = listing.get(symbol)
        if rec is None:
            return None
        if rec.is_fund:
            # Nasdaq has no fund category; never let a placeholder reach check_fund.
            return {"quote_type": "ETF", "fund_category_not_listed": True}
        return {
            "market_cap_usd": float(rec.market_cap) if rec.market_cap is not None else None,
            "sector": canonicalize(rec.sector_recorded),
            "quote_type": "EQUITY",
            "category": None,
        }

    return _profile


def _yahoo_reader():
    """yfinance `Ticker(sym).info` for the second pass, on yfinance's own timeout."""
    import yfinance as yf

    def _info(symbol: str) -> dict:
        # Yahoo spells share classes with a dash (BRK-B), never a dot.
        return yf.Ticker(symbol.replace(".", "-")).info or {}

    return _info


def _filings_reader(config, deadline: float):
    """(get_filings, get_role) for the screen's takeover check.

    Built from `smart_money` config, passing every constructor field that
    config names, as the pipeline does. Every SEC read goes through that
    client (its rate limiter and its configured User-Agent): the issuer's
    submissions JSON for the filing rows, then, only for the one open
    takeover filing, its header and (self-filed proxies) its document.
    Each read raises on failure; the screen records takeover_lookup_failed.
    """
    import inspect

    from src.data import sec_filings
    from src.data.smart_money import SECForm4Provider

    sm = config.smart_money
    params = inspect.signature(SECForm4Provider).parameters
    sec = SECForm4Provider(**{name: getattr(sm, name) for name in params if hasattr(sm, name)})
    try:
        listed = sec.listed_map(deadline)
    except Exception as exc:  # noqa: BLE001 -- recorded per name as takeover_lookup_failed
        logger.warning("universe daily: SEC ticker map unavailable: %s", exc)
        listed = None

    if listed is None:

        def _unavailable(symbol: str):
            raise RuntimeError("SEC ticker map unavailable")

        return _unavailable, None
    return sec_filings.screen_readers(sec, deadline, listed)


class _CountedBatches:
    """`broker.get_bars_batch` (Alpaca multi-symbol daily bars), counting pages."""

    def __init__(self, broker, lookback_days: int):
        self.broker = broker
        self.lookback_days = int(lookback_days)
        self.calls = 0

    def __call__(self, symbols: list[str]) -> dict[str, list]:
        return self.broker.get_bars_batch(symbols, self.lookback_days, on_page=self._page)

    def _page(self) -> None:
        self.calls += 1


def main(argv: list[str] | None = None) -> int:
    import argparse

    from src.api.broker_reads import _get_broker
    from src.config import load_config
    from src.data.nasdaq_fetch import fetch_json
    from src.data.nasdaq_listing import ListingUnavailable, load_listing
    from src.sector_reference import _canonicalize_sector
    from src.universe_screen import HISTORY_FETCH_DAYS
    from src.util.time import et_today

    parser = argparse.ArgumentParser(description="Daily record-only universe screen")
    parser.add_argument("--config", default="config/settings.yaml")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    config = load_config(Path(parser.parse_args(argv).config))
    cfg = config.universe_screen
    today = et_today()
    broker = _get_broker()
    if not _due_today(broker, cfg.data_dir, today):
        return 0
    deadline = time.monotonic() + float(cfg.screen_deadline_s)
    try:
        listing = load_listing(fetch=fetch_json)
    except ListingUnavailable as exc:
        logger.warning("universe daily: Nasdaq listing unavailable: %s", exc)
        listing = None
    bars = _CountedBatches(broker, HISTORY_FETCH_DAYS)
    cache = ReadCache(cfg.data_dir)
    get_filings, get_role = _filings_reader(config, deadline)
    run = run_daily_record(
        broker.list_assets(),
        get_bars_batch=bars,
        get_profile=_profile_reader(listing, _canonicalize_sector),
        get_filings=get_filings,
        get_role=get_role,
        th=ScreenThresholds.from_config(config),
        today=today,
        deadline=deadline,
        batch_size=int(cfg.bars_batch_size),
        cache=cache,
        get_yahoo=_yahoo_reader(),
    )
    run.calls["alpaca_assets"] = 1
    run.calls["nasdaq_listing"] = 2
    run.listing_unavailable = listing is None
    run.calls["bar_batches"] = bars.calls
    cache.save()
    write_record(cfg.data_dir, run)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
