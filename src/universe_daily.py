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
  Yahoo gap    A second pass (owner ruling 2026-10-10: wrongly dropped
               names go back in). ONLY a name that passed every other check
               and is (a) a fund with no stored category, or one stored
               longer ago than `fund_category_refresh_days`, or (b) a stock
               whose Nasdaq sector is Unknown gets one yfinance `.info` read
               (`category` for a fund, `sector` for a stock), one after
               another with yfinance's own timeout. A fund's category is
               KEPT in the desk database (`src/data/fund_category.py`,
               table `fund_category`): read once, then served to this pass
               and to the weekly scan and side doors
               (`src/pipeline_admission.py`), which read Yahoo themselves
               only for a fund with none kept (`fund_category_reader`,
               2026-10-14). A stock's sector fills the ISO-week cache (the SEC
               filings rule). A rate limit (HTTP 429 or yfinance's own
               error) is a slow-down, not a stop (owner ruling 2026-10-13):
               the pass honours Retry-After when Yahoo sends one, otherwise
               backs off exponentially (`yahoo_backoff_s` doubling up to
               `yahoo_backoff_max_s`), then carries on in the same run. It
               stops only at `YahooFill.until` — the start of the desk's day,
               08:00 ET = the `earnings_preprocess` phase start
               (`src/session_calendar.py`), ~5 h after the 03:00 wake-up;
               every name not yet read is recorded unreached (never
               admitted) and is read the next night. It runs after every
               other read, so it never eats their time budget. A fund
               category goes through `check_fund` (leveraged, inverse, bond,
               T-bill and money-market words refuse) and the refuse-only
               name backstop (`is_leveraged_fund_name`) runs before it; a
               stock sector is used as is (Yahoo's names ARE the desk's). An
               error or blank answer records the name inconclusive
               `profile_unavailable` ("could not read"): never admitted,
               never dropped; a stale stored category keeps its record when
               the refresh cannot be read. The 9 Oct record had 2,780 such
               funds and 37 such stocks; the full per-name pass removed in
               #1661 read all ~14,000 names.
  takeover     SEC issuer filing history, cached per ISO week. Calls counted.

Records: `<universe_screen.data_dir>/daily/<YYYY-MM-DD>.jsonl` (one line per
listed name: passed, dropped with its failing check(s), inconclusive on a
data outage, or "unreached: time limit") and `<YYYY-MM-DD>.summary.json`
(names listed, passed, dropped per reason, calls made, seconds taken).

Run: `python -m src.universe_daily` — screens the LAST COMPLETED session
(owner ruling 2026-10-13: the timer fires 03:00 ET, so that is the previous
trading day's close); a no-op when that day already has a record, so any
wake-up tick may call it.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from src.trading_calendar import et_today, utc_now
from src.data.fund_category import FundCategoryStore
from src.universe_screen import (
    INCONCLUSIVE,
    ScreenSources,
    ScreenThresholds,
    SecondFilterInputs,
    _asset_symbol,
    _field,
    check_asset,
    check_bars,
    check_fund,
    iso_week,
    screen_symbol,
    second_filter_score,
)

logger = logging.getLogger(__name__)

UNREACHED = "unreached_time_limit"
UNREACHED_TEXT = "unreached: time limit"
YAHOO_LIMITED = "unreached_yahoo_rate_limit"
YAHOO_LIMITED_TEXT = "unreached: Yahoo rate limit, no night left to back off; read next night"
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
    yahoo: dict[str, int] = field(
        default_factory=lambda: {"names": 0, "calls": 0, "failures": 0, "cache_hits": 0, "backoffs": 0}
    )
    seconds: float = 0.0
    deadline_hit: bool = False
    listing_unavailable: bool = False

    def record(self, symbol: str, status: str, failures=(), measured=None, **extra) -> None:
        row = {"symbol": symbol, "status": status, "failures": list(failures), "measured": dict(measured or {})}
        if status == "unreached":
            row["reason"] = UNREACHED_TEXT
        row.update(extra)
        self.records[symbol] = row

    def add_second_filter(self, inputs: SecondFilterInputs) -> None:
        """Record-only: score K and its rank among passers, on each passer's own record.

        Touches no status and no failure list, so the names that pass are unchanged."""
        import pandas as pd

        scored = {
            symbol: second_filter_score(row["measured"], inputs)
            for symbol, row in self.records.items()
            if row["status"] == "passed"
        }
        have = pd.Series({s: v["k"] for s, v in scored.items() if v["k"] is not None}, dtype=float)
        ranks = have.rank(ascending=False, method="min")
        for symbol, score in scored.items():
            score["rank"] = int(ranks[symbol]) if symbol in ranks.index else None
            self.records[symbol]["second_filter"] = score

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


@dataclass
class YahooFill:
    """How the Yahoo pass fills and keeps what it reads.

    `store` keeps fund categories across runs (None: nothing is kept);
    `until` is the monotonic instant the pass must stop at (None: the run's
    own deadline); `refresh_days` re-reads a stored category older than
    this (None: never); `backoff_s` doubles per consecutive rate limit up to
    `backoff_max_s`, unless Yahoo sends Retry-After. A zero backoff stops
    at the first rate limit."""

    store: FundCategoryStore | None = None
    until: float | None = None
    refresh_days: int | None = None
    backoff_s: float = 0.0
    backoff_max_s: float = 0.0
    sleep: Callable[[float], None] = time.sleep


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
    yahoo_fill: YahooFill | None = None,
    second_filter: SecondFilterInputs | None = None,
) -> DailyRun:
    """Screen every listed name once; record each. Never mutates admission state.

    `get_yahoo(symbol) -> yfinance .info dict` fills the Yahoo gap (module
    docstring); None leaves those names as the listing alone records them.
    `yahoo_fill` says where a fund category is kept and how long the pass
    may run (default: nothing kept, the run's own deadline). `second_filter`,
    when given, adds the record-only morning-scan score K to each passer's record."""
    started = clock()
    fill = yahoo_fill or YahooFill()
    if fill.until is None:
        fill.until = deadline
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
        fill=fill,
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
    if second_filter is not None:
        screen.run.add_second_filter(second_filter)
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
    fill: YahooFill = field(default_factory=YahooFill)
    # The time limit is checked when a name STARTS: a name begun in time
    # finishes its reads; one begun late uses only what is cached.
    late: bool = False
    # Names that passed every other check and wait for a Yahoo read:
    # (symbol, "fund" | "fund_refresh" | "stock", measured, extra record
    # fields). A "fund_refresh" already has a (stale) stored category and a
    # passed record; the read only replaces both when it succeeds.
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
                result = screen_symbol(
                    symbol, sources, self.th, asset=by_symbol[symbol], bars=bars[symbol], today=self.run.today
                )
                failures, measured = result.failures, result.measured
                kind = self._fund_kind(failures, seen.get("profile"))
                if self.get_yahoo is not None and failures == ["unresolved_sector"]:
                    # Run the remaining (takeover) check with a stand-in
                    # sector, so Yahoo is read only for a name that passed it.
                    held = {**seen["profile"], "sector": _SECTOR_PENDING}
                    sources.get_profile = lambda s, _p=held: _p
                    rerun = screen_symbol(
                        symbol, sources, self.th, asset=by_symbol[symbol], bars=bars[symbol], today=self.run.today
                    )
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

    def _fund_kind(self, failures: list, profile) -> str | None:
        """Which Yahoo read a fund is due: "fund" when its category is its last
        failing check, "fund_refresh" when it passed on a stored category older
        than the refresh horizon, else None."""
        if self.get_yahoo is None:
            return None
        if failures == ["fund_category_not_listed"]:
            return "fund"
        if failures or self.fill.refresh_days is None or not isinstance(profile, dict):
            return None
        fetched = profile.get("fund_category_fetched_on")
        if not fetched:
            return None
        stale = date.fromisoformat(str(fetched)) + timedelta(days=int(self.fill.refresh_days)) <= self.run.today
        return "fund_refresh" if stale else None

    def _not_reached(self, symbol: str, measured: dict, keep: bool) -> None:
        """Record a name the pass could not get to (a refresh keeps its record)."""
        if keep:
            return
        if self.yahoo_blocked:
            self.run.record(symbol, "unreached", [YAHOO_LIMITED], measured, reason=YAHOO_LIMITED_TEXT)
        else:
            self.run.record(symbol, "unreached", [UNREACHED], measured)

    def yahoo_phase(self) -> None:
        """Fill a fund's category / a stock's sector from Yahoo; fail closed."""
        from src.models import _ALLOWED_SECTORS

        self.run.yahoo["names"] = len(self.yahoo_due)
        for symbol, kind, measured, read_on in sorted(self.yahoo_due, key=lambda d: self._age(d[0], "yahoo")):
            keep = kind == "fund_refresh"  # the stored category's record stands unless the read succeeds
            late = self.clock() >= self.fill.until
            if late:
                self.run.deadline_hit = True
            # A fund category lives in the store (served by the profile reader);
            # only a stock's sector goes through the ISO-week cache.
            entry = self.cache.get("yahoo", symbol) if kind == "stock" else None
            if entry is not None and (entry.get("week") == iso_week(self.run.today) or late):
                self.run.yahoo["cache_hits"] += 1
                value, read_on_day = entry["value"], entry.get("on")
            elif late or self.yahoo_blocked:
                self._not_reached(symbol, measured, keep)
                continue
            else:
                value = self._yahoo_read(symbol, kind, measured, read_on, keep=keep)
                read_on_day = self.run.today.isoformat()
                if self.yahoo_blocked:
                    self._not_reached(symbol, measured, keep)
                    continue
                if value is _READ_FAILED or (keep and not value):
                    continue
            extra = {**read_on, "yahoo_read_on": read_on_day if value else None}
            key = "fund" if keep else kind
            text = str((value or {}).get(key) or "").strip() if isinstance(value, dict) else ""
            if key == "fund" and text:
                failures, got = check_fund(text)
                self.run.record(symbol, _status(failures), failures, {**measured, **got}, **extra)
            elif kind == "stock" and text in _ALLOWED_SECTORS:
                self.run.record(symbol, "passed", [], {**measured, "sector": text}, **extra)
            else:
                # Error, blank or an unknown sector name: could not read.
                self.run.record(symbol, "inconclusive", ["profile_unavailable"], measured, **extra)

    def _yahoo_read(self, symbol: str, kind: str, measured: dict, read_on: dict, *, keep: bool = False):
        """One `.info` read -> {kind: text}, kept when not blank; None when blank.
        A raised read is recorded for the name (unless `keep`), with Yahoo's own
        error text, and returns `_READ_FAILED`. A rate limit backs off and
        retries the same name; when no night is left to wait, `yahoo_blocked`
        is set and nothing more is read this run."""
        attempt = 0
        while not self.yahoo_blocked:
            self.run.yahoo["calls"] += 1
            try:
                info = self.get_yahoo(symbol) or {}
                break
            except Exception as exc:  # noqa: BLE001 -- recorded per name as could-not-read
                self.run.yahoo["failures"] += 1
                if not _is_rate_limit(exc):
                    logger.warning("universe daily: Yahoo read failed for %s: %s", symbol, exc)
                    if not keep:
                        self.run.record(
                            symbol,
                            "inconclusive",
                            ["profile_unavailable"],
                            measured,
                            reason=f"Yahoo read failed: {type(exc).__name__}: {exc}",
                            **{**read_on, "yahoo_read_on": None},
                        )
                    return _READ_FAILED
                self._back_off(symbol, exc, attempt)
                attempt += 1
        else:
            return None
        key = "category" if kind in ("fund", "fund_refresh") else "sector"
        text = str(info.get(key) or "").strip() if isinstance(info, dict) else ""
        if not text:
            self.run.yahoo["failures"] += 1
            return None
        if key == "category":
            if self.fill.store is not None:
                self.fill.store.put(symbol, text, self.run.today)
            return {"fund": text}
        value = {kind: text}
        self.cache.put("yahoo", symbol, value, self.run.today)
        return value

    def _back_off(self, symbol: str, exc: BaseException, attempt: int) -> None:
        """Honour Retry-After, else wait `backoff_s` doubled per attempt up to
        `backoff_max_s`; when the wait would outlast the night, close Yahoo."""
        wait = _retry_after(exc)
        if wait is None:
            wait = min(float(self.fill.backoff_max_s), float(self.fill.backoff_s) * (2**attempt))
        if wait <= 0 or self.clock() + wait >= self.fill.until:
            logger.warning("universe daily: Yahoo rate limit on %s; no night left to back off, Yahoo closed", symbol)
            self.yahoo_blocked = True
            return
        logger.warning("universe daily: Yahoo rate limit on %s; backing off %.0f s", symbol, wait)
        self.run.yahoo["backoffs"] += 1
        self.fill.sleep(wait)


def _is_rate_limit(exc: BaseException) -> bool:
    text = str(exc).lower()
    return type(exc).__name__ == "YFRateLimitError" or "429" in text or "too many requests" in text


def _retry_after(exc: BaseException) -> float | None:
    """Seconds Yahoo asked us to wait (RFC 9110 Retry-After, delay-seconds or
    HTTP-date) when the raised error carries its response; else None."""
    from email.utils import parsedate_to_datetime

    headers = getattr(getattr(exc, "response", None), "headers", None)
    raw = headers.get("Retry-After") if hasattr(headers, "get") else None
    if raw is None:
        return None
    text = str(raw).strip()
    if text.isdigit():
        return float(text)
    try:
        when = parsedate_to_datetime(text)
    except (TypeError, ValueError):
        return None
    return max(0.0, (when - utc_now()).total_seconds())


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
    summary["written_at"] = utc_now().isoformat(timespec="seconds")
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


def _screen_day(broker, today: date, now: datetime) -> date | None:
    """The last completed session on or before `today`: the day whose close
    has passed. At the 03:00 ET wake-up that is the previous trading day;
    after the close it is today. None when the calendar names none in the
    past fortnight (a calendar outage: nothing to screen)."""
    day = today
    for _ in range(14):
        if broker.is_trading_day(day):
            close = broker.get_session_close(day)
            if close is not None and now >= close:
                return day
        day -= timedelta(days=1)
    return None


def _due(broker, data_dir, today: date, now: datetime) -> date | None:
    """The session to screen now, or None when it is already recorded."""
    day = _screen_day(broker, today, now)
    if day is None:
        logger.info("universe daily: no completed session found before %s", today)
        return None
    if already_recorded(data_dir, day):
        logger.info("universe daily: %s already recorded", day)
        return None
    return day


def _yahoo_until(broker, today: date, now: datetime, fallback: float) -> float:
    """Monotonic instant the Yahoo pass must stop at: the start of the desk's
    day, `earnings_preprocess` on `today` (08:00 ET on a full day). When
    `today` has no session (holiday, calendar outage) the run's own deadline."""
    from src.session_calendar import SessionCalendarUnavailable, phase_window

    try:
        start, _ = phase_window(broker, today, "earnings_preprocess")
    except SessionCalendarUnavailable as exc:
        logger.warning("universe daily: no session edges for %s (%s); Yahoo pass keeps the run deadline", today, exc)
        return fallback
    return time.monotonic() + max(0.0, (start - now).total_seconds())


def _profile_reader(listing, canonicalize, store: FundCategoryStore | None = None):
    """Profile in the shape `check_profile` reads, from one Nasdaq listing
    (None when the download failed: every name is then unavailable). A
    fund's category comes from `store` (the desk database); with none on
    record it is `fund_category_not_listed` (inconclusive, never admitted)."""

    def _profile(symbol: str):
        if listing is None:
            return None
        rec = listing.get(symbol)
        if rec is None:
            return None
        if rec.is_fund:
            kept = store.get(symbol) if store is not None else None
            if kept is None:
                # Nasdaq has no fund category; never let a placeholder reach check_fund.
                return {"quote_type": "ETF", "fund_category_not_listed": True, "name": rec.name}
            return {
                "quote_type": "ETF",
                "category": kept.category,
                "name": rec.name,
                "fund_category_fetched_on": kept.fetched_at.isoformat(),
                "fund_category_source": kept.source,
            }
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


def fund_category_reader(store: FundCategoryStore | None):
    """`ScreenSources.get_fund_category` for the weekly scan and the side
    doors: one Yahoo `.info` read of a fund's category, kept in `store` so it
    is read once. A raised read propagates (the screen leaves the fund
    undecided); a blank answer is None."""
    read_info = _yahoo_reader()

    def _category(symbol: str) -> str | None:
        text = str((read_info(symbol) or {}).get("category") or "").strip()
        if text and store is not None:
            store.put(symbol, text, et_today())
        return text or None

    return _category


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


def _second_filter_inputs(config, broker) -> SecondFilterInputs:
    """The score's numbers. G is read as `src/delever/ladder.py` reads it: the
    current-account equity curve (same 252-row window), today's broker equity, the
    drawdown, then `resolve_gross_ceiling`. Read-only; any failure records G as
    None with the reason (a durable fault row), never a guessed ceiling."""
    import sqlite3

    from src.api.broker_reads_record import record_read_fault
    from src.risk.book_exposure import peak_to_trough_pct
    from src.risk.gross_ladder import resolve_gross_ceiling
    from src.stage_decision import VELOCITY_HOLD_SESSIONS
    from src.storage.account_epoch import current_account_daily_pnl

    gross_x, why = None, None
    try:
        equity = float(broker.get_account()["portfolio_value"])
        conn = sqlite3.connect(Path(config.storage.db_path).resolve().as_uri() + "?mode=ro", uri=True)
        try:
            rows = current_account_daily_pnl(conn, limit=252)
        finally:
            conn.close()
        drawdown = peak_to_trough_pct([r.get("total_value") for r in rows], equity)
        if drawdown is None:
            why = "gross_ceiling_drawdown_unmeasurable"
        else:
            gross_x = resolve_gross_ceiling(drawdown, base_x=float(config.risk.max_gross_exposure_x)).ceiling_x
    except Exception as exc:  # noqa: BLE001 -- recorded as a fault row, G stays None
        record_read_fault("universe_daily.gross_ceiling", exc)
        why = f"gross_ceiling_unreadable: {type(exc).__name__}"
    return SecondFilterInputs(
        hold_sessions=VELOCITY_HOLD_SESSIONS,
        stop_atr_multiple=float(config.risk.min_stop_atr_multiple),
        max_portfolio_risk_pct=float(config.risk.max_portfolio_risk_pct),
        gross_ceiling_x=gross_x,
        gross_ceiling_reason=why,
    )


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
    now = utc_now()
    wake_day = et_today()
    broker = _get_broker()
    today = _due(broker, cfg.data_dir, wake_day, now)
    if today is None:
        return 0
    deadline = time.monotonic() + float(cfg.screen_deadline_s)
    try:
        listing = load_listing(fetch=fetch_json)
    except ListingUnavailable as exc:
        logger.warning("universe daily: Nasdaq listing unavailable: %s", exc)
        listing = None
    bars = _CountedBatches(broker, HISTORY_FETCH_DAYS)
    cache = ReadCache(cfg.data_dir)
    store = FundCategoryStore(config.storage.db_path)
    get_filings, get_role = _filings_reader(config, deadline)
    fill = YahooFill(
        store=store,
        until=_yahoo_until(broker, wake_day, now, deadline),
        refresh_days=int(cfg.fund_category_refresh_days),
        backoff_s=float(cfg.yahoo_backoff_s),
        backoff_max_s=float(cfg.yahoo_backoff_max_s),
    )
    run = run_daily_record(
        broker.list_assets(),
        get_bars_batch=bars,
        get_profile=_profile_reader(listing, _canonicalize_sector, store),
        get_filings=get_filings,
        get_role=get_role,
        th=ScreenThresholds.from_config(config),
        today=today,
        deadline=deadline,
        batch_size=int(cfg.bars_batch_size),
        cache=cache,
        get_yahoo=_yahoo_reader(),
        yahoo_fill=fill,
        second_filter=_second_filter_inputs(config, broker),
    )
    run.calls["alpaca_assets"] = 1
    run.calls["nasdaq_listing"] = 2
    run.listing_unavailable = listing is None
    run.calls["bar_batches"] = bars.calls
    cache.save()
    store.close()
    write_record(cfg.data_dir, run)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
