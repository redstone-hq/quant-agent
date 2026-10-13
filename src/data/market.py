import logging
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout
from datetime import timedelta

import pandas as pd
import yfinance as yf

from src.models import OHLCV
from src.trading_calendar import last_completed_bar_date
from src.util.time import et_today
from src.sentinel.counted import record_swallowed

logger = logging.getLogger(__name__)

_VALUATION_TIMEOUT_S = 10  # per-symbol ceiling on yfinance .info hang
_DOWNLOAD_TIMEOUT_S = 30  # per-call ceiling on yf.download() hang — same risk as .info,
# without this a network stall hangs the whole session window
# until the launchd outer kill (~20min) fires.

# Keyed by the canonical sector name used everywhere else (yfinance + MacroSectorGuidance enum).
SECTOR_ETFS = {
    "Technology": "XLK",
    "Healthcare": "XLV",
    "Financial Services": "XLF",
    "Consumer Cyclical": "XLY",
    "Communication Services": "XLC",
    "Industrials": "XLI",
    "Consumer Defensive": "XLP",
    "Energy": "XLE",
    "Utilities": "XLU",
    "Real Estate": "XLRE",
    "Basic Materials": "XLB",
}


def _completed_only(bars: list, cutoff, symbol: str, source: str) -> list:
    """Drop any bar dated after `cutoff` (a still-forming session bar).

    Logged, not silent: a dropped bar means a source handed back an
    in-progress day, which must never be read as a finished close.
    """
    if not bars:
        return bars
    kept = [b for b in bars if getattr(b, "date", None) is None or b.date <= cutoff]
    if len(kept) < len(bars):
        logger.info(
            "get_ohlcv %s: dropped %d in-progress bar(s) dated after %s from %s "
            "(completed bars only; live price comes from the broker snapshot)",
            symbol,
            len(bars) - len(kept),
            cutoff,
            source,
        )
    return kept


class NoBarsSourceError(RuntimeError):
    """`get_ohlcv` was called on a provider with no broker bars source wired.

    Raised, never swallowed: a provider built without `bars_source` (e.g. a
    script that forgot to wire the broker) would otherwise report every
    symbol as "no data", and a backtest would run on nothing and look fine.
    """


class MarketDataProvider:
    def __init__(self, bars_source=None, fallback_bars=None, bars_batch_source=None):
        """
        bars_source: callable `(symbol, lookback_days) -> list[OHLCV]` that
        answers `get_ohlcv`. The live desk wires it to the broker's daily-bar
        read (`AlpacaBroker.get_bars`), so history comes from the same source
        the desk trades on (owner, 2026-10-09: stock data from Alpaca, not
        Yahoo). Yahoo is NOT a fallback here: an Alpaca failure is reported
        for that one symbol and the read returns [], which every caller
        already treats as "skip this name" (never "halt the desk").

        `fallback_bars` is the pre-2026-10-09 name of the same hook, kept so
        the pipeline's existing wiring (`set_fallback_bars(broker.get_bars)`)
        still lands on the primary source.

        `bars_batch_source`: callable `(symbols, lookback_days, adjustment=)
        -> {symbol: list[OHLCV]}` answering `get_ohlcv_batch`. When omitted
        it is taken from the SAME object that answers the single read
        (`broker.get_bars_batch` beside `broker.get_bars`), so one wiring
        line gives both reads one price source (2026-10-12: the batch read
        Yahoo while the single read Alpaca, and open-position targets were
        revised from the other chart).
        """
        self._bars_source = None
        self._bars_batch_source = None
        self.set_bars_source(
            bars_source if bars_source is not None else fallback_bars,
            batch=bars_batch_source,
        )

    def set_bars_source(self, fn, batch=None) -> None:
        self._bars_source = fn
        if batch is None and fn is not None:
            batch = getattr(getattr(fn, "__self__", None), "get_bars_batch", None)
        self._bars_batch_source = batch

    # Pre-2026-10-09 name of `set_bars_source`; the pipeline still calls it.
    set_fallback_bars = set_bars_source

    def get_ohlcv(self, symbol: str, lookback_days: int = 120) -> list[OHLCV]:
        """COMPLETED daily bars only, oldest first, from the broker's data feed.

        Contract (2026-09-14, docs/INCIDENT_HISTORY.md): the series ends at
        the latest session whose daily bar is finished —
        `trading_calendar.last_completed_bar_date()`. During regular hours
        that is the PREVIOUS session; after the 16:00 ET close it is today.

        This series is for smoothed indicators and structure (ATR, MAs,
        pivots, levels). It is never "the current price" during market
        hours — a caller comparing price against a level, stop, target or
        breakout while the session is open must use a live price
        (`AlpacaBroker.get_intraday_snapshots` / `get_latest_price`) and
        label it as in-progress. A still-forming bar for today is dropped
        here (Alpaca does return one mid-session), so it can never be
        silently mixed into the series.

        Failure shape: the source raising is logged and COUNTED for that
        symbol (`record_swallowed`) and returns [] — the same shape a Yahoo
        outage produced before, which callers handle by skipping that one
        name. NO source wired is a wiring defect, not a feed failure, and
        raises `NoBarsSourceError` so it cannot pass for "no data".
        """
        if self._bars_source is None:
            raise NoBarsSourceError(
                f"get_ohlcv({symbol!r}): no bars source wired — pass bars_source=broker.get_bars "
                "(src/execution/broker_parts/market_data.py) or call set_bars_source()"
            )
        cutoff = last_completed_bar_date()
        try:
            bars = list(self._bars_source(symbol, lookback_days) or [])
        except Exception as e:  # noqa: BLE001 — counted, per-symbol; a feed error must not halt the desk
            record_swallowed("data.market.bars_source", e, log=logger, symbol=symbol)
            return []
        if not bars:
            logger.warning("get_ohlcv %s: broker daily bars returned nothing", symbol)
            return []
        return _completed_only(bars, cutoff, symbol, "broker")

    def get_ohlcv_batch(self, symbols: list[str], lookback_days: int) -> dict[str, list[OHLCV]]:
        """COMPLETED daily bars for many symbols from the broker's data feed.

        Same contract and SAME price source as `get_ohlcv`: the broker's
        multi-symbol daily reader (`AlpacaBroker.get_bars_batch`), asked for
        the same adjustment the single-symbol read uses ("all": splits and
        dividends), because the callers — open-position target revision and
        universe admission — compare their answer against the chart the
        alignment exit reads. Until 2026-10-12 this read Yahoo instead, and
        targets were revised from a different chart than the one they were
        judged on.

        A symbol missing from the reply maps to []; a failed request RAISES,
        so the caller can tell "this symbol has no history" from "the feed
        is down". No batch source wired is a wiring defect
        (`NoBarsSourceError`), never "no data".
        """
        wanted = [str(s).strip().upper() for s in symbols if str(s).strip()]
        if not wanted:
            return {}
        if self._bars_batch_source is None:
            raise NoBarsSourceError(
                "get_ohlcv_batch: no batch bars source wired — set_bars_source(broker.get_bars) "
                "takes broker.get_bars_batch from the same object, or pass batch=..."
            )
        cutoff = last_completed_bar_date()
        # "all" == the single-symbol read's `Adjustment.ALL`; the screen's own
        # call keeps its default ("split"). tests/test_ohlcv_batch_alpaca.py
        # holds the two requests to the same adjustment.
        got = self._bars_batch_source(wanted, lookback_days, adjustment="all") or {}
        out: dict[str, list[OHLCV]] = {}
        for symbol in wanted:
            bars = list(got.get(symbol) or [])
            out[symbol] = _completed_only(bars, cutoff, symbol, "broker batch")
        return out

    def get_company_profile(self, symbol: str) -> dict | None:
        """{"market_cap_usd", "sector_raw", "quote_type", "category"} from yfinance, or None
        when it could not be read. Bounded by the same per-symbol timeout as
        valuations."""

        def _fetch():
            return yf.Ticker(symbol).info or {}

        try:
            with ThreadPoolExecutor(max_workers=1) as ex:
                info = ex.submit(_fetch).result(timeout=_VALUATION_TIMEOUT_S)
        except Exception as exc:  # noqa: BLE001 — timeout or fetch error
            record_swallowed("data.market.company_profile", exc, log=logger, symbol=symbol)
            return None
        if not isinstance(info, dict) or not info:
            return None
        cap = info.get("marketCap")
        try:
            cap = float(cap) if cap is not None else None
        except (TypeError, ValueError):
            cap = None
        return {
            "market_cap_usd": cap,
            "sector_raw": info.get("sector"),
            "quote_type": info.get("quoteType"),
            "category": info.get("category"),
        }

    def get_upcoming_ex_dividend(self, symbol: str) -> dict:
        """Return {date, amount} for a symbol's upcoming ex-dividend, or {}.

        Used by midday ex-div adjustment to lower stops by dividend amount
        before the ex-div gap triggers them for a non-thesis reason.
        Bounded by a 10s timeout per symbol — same pattern as valuations.
        """
        from datetime import date as _date

        def _fetch():
            try:
                return yf.Ticker(symbol).info or {}
            except Exception as e:
                record_swallowed("data.market.ex_dividend", e, log=logger, symbol=symbol)
                return {}

        try:
            with ThreadPoolExecutor(max_workers=1) as ex:
                info = ex.submit(_fetch).result(timeout=_VALUATION_TIMEOUT_S)
        except FuturesTimeout:
            logger.warning("ex-div fetch timed out for %s", symbol)
            return {}

        ex_ts = info.get("exDividendDate")
        amount = info.get("lastDividendValue")
        if amount is None:
            annual = info.get("trailingAnnualDividendRate")
            # Most US large-caps pay quarterly; fall back to annual/4 if we
            # don't have a concrete last-event value.
            if annual:
                try:
                    amount = float(annual) / 4
                except (TypeError, ValueError):
                    amount = None
        if ex_ts is None or amount is None:
            return {}
        try:
            # audit round 2: exDividendDate is UTC-midnight epoch; a host-local
            # parse shifts the date on any TZ east of UTC (SG host: +1 day off).
            from datetime import datetime as _dtt, timezone as _tz

            ex_date = _dtt.fromtimestamp(float(ex_ts), tz=_tz.utc).date()
        except (TypeError, ValueError, OSError, OverflowError):
            # OverflowError is NOT a ValueError subclass: an absurd epoch
            # (e.g. milliseconds mistaken for seconds) raises "timestamp out
            # of range for platform time_t" and escaped this guard, breaking
            # the method's own "returns {} on anything unusable" contract.
            # The one current caller wraps this call, so the observed effect
            # was a warning + skipped symbol rather than a failed session —
            # but the contract is what the next caller will rely on.
            return {}
        try:
            amount = round(float(amount), 4)
        except (TypeError, ValueError):
            return {}
        if amount <= 0:
            return {}
        return {"date": ex_date, "amount": amount}

    def get_next_earnings_date(self, symbol: str) -> int | None:
        """Sessions until the next scheduled earnings report, or None.

        Nothing in the system knew this. `src/data/earnings.py` discovers
        filings that have ALREADY been submitted to EDGAR — it is retrospective.
        For a desk holding positions for days to weeks, an unnoticed earnings
        date is a binary event the thesis never chose to take, and the only
        place "days to earnings" appeared previously was as illustrative text
        inside the Risk Manager prompt, which means any figure the model quoted
        was invented.

        Returns a trading-session estimate (calendar days x 5/7), not a precise
        count, because the exact market calendar is not needed to answer "is a
        report imminent". None whenever the date is unknown or in the past —
        callers must treat None as "unknown", never as "no earnings soon".
        """
        try:
            ticker = yf.Ticker(symbol)
            candidates: list = []

            calendar = getattr(ticker, "calendar", None)
            if isinstance(calendar, dict):
                value = calendar.get("Earnings Date")
                if isinstance(value, list):
                    candidates.extend(value)
                elif value is not None:
                    candidates.append(value)

            if not candidates:
                frame = ticker.earnings_dates
                if frame is not None and not frame.empty:
                    candidates.extend(list(frame.index))

            today = et_today()
            upcoming: list[int] = []
            for candidate in candidates:
                as_date = getattr(candidate, "date", None)
                resolved = as_date() if callable(as_date) else candidate
                if not hasattr(resolved, "year"):
                    continue
                delta = (resolved - today).days
                if delta >= 0:
                    upcoming.append(delta)

            if not upcoming:
                return None
            # Calendar days -> trading sessions, floored at same-day.
            return max(0, int(min(upcoming) * 5 / 7))
        except Exception as exc:  # noqa: BLE001
            record_swallowed("data.market.next_earnings_date", exc, log=logger, symbol=symbol)
            return None

    def get_price_chart_events(self, symbol: str, lookback_days: int = 400) -> dict:
        """Dividend ex-dates and earnings-report dates (past + upcoming),
        for Mission Control's price-chart markers only — not read by any
        trading/risk code path. Deliberately separate from
        `get_upcoming_ex_dividend`/`get_next_earnings_date` above (which
        answer a narrower "is one imminent" question for stop adjustment /
        risk prompts and must keep their existing single-value contracts
        unchanged): a chart wants the fuller list, past events included, not
        just the next one.

        Returns {"dividends": [...], "earnings": [...], "earnings_degraded":
        str | None}. `earnings` may legitimately be empty (an ETF with no
        earnings, a newly-listed name) — that is a normal, silent empty
        result, never an error. `earnings_degraded` is the DISTINCT signal
        for "the past-earnings source itself could not be read" (e.g. the
        optional `lxml` dependency `ticker.earnings_dates` needs is missing
        from this environment): when set, the `earnings` list may be
        incomplete even though it looks like a normal result, because it can
        still have been filled in by the single-next-date `ticker.calendar`
        fallback below. Modelled on `SeriesFreshness`/`FeedFailure`
        (src/data/macro.py, src/data/news.py): a missing-data source must
        surface as a distinguishable degraded signal, not silently collapse
        into the same empty list a symbol with genuinely no history returns.
        (2026-09-12 incident: exactly this collapse hid every PAST earnings
        marker, for every symbol, because `lxml` was never installed in
        production — see docs/INCIDENT_HISTORY.md.)
        """
        today = et_today()
        cutoff = today - timedelta(days=lookback_days)

        def _fetch():
            ticker = yf.Ticker(symbol)
            dividends = []
            try:
                series = ticker.dividends  # pandas Series, tz-aware DatetimeIndex
                if series is not None and not series.empty:
                    for ts, amount in series.items():
                        d = ts.date() if hasattr(ts, "date") else ts
                        if not hasattr(d, "year") or d < cutoff:
                            continue
                        try:
                            amt = round(float(amount), 4)
                        except (TypeError, ValueError):
                            amt = None
                        dividends.append({"date": d.isoformat(), "amount": amt})
            except Exception as e:
                logger.debug("dividend history unavailable for %s: %s", symbol, e)

            # `ticker.earnings_dates` gives BOTH past-reported and upcoming
            # estimated dates, but requires the optional `lxml` package. It
            # is now declared in pyproject.toml (2026-09-12) and should be
            # installed everywhere, but a fetch failure here — an ImportError
            # if some environment still lacks it, or anything else yfinance
            # can raise — must NOT collapse into the same empty list a
            # symbol with genuinely no earnings history produces: that
            # collapse is exactly what hid every past-earnings marker in
            # production before this dependency was declared (see
            # docs/INCIDENT_HISTORY.md, 2026-09-12). So the failure is
            # recorded in `earnings_degraded` and logged at WARNING (not
            # DEBUG) — loud enough to be noticed instead of silently
            # indistinguishable from "no data".
            earnings_dates: set = set()
            earnings_degraded: str | None = None
            try:
                frame = ticker.earnings_dates
                if frame is not None and not frame.empty:
                    for ts in frame.index:
                        d = ts.date() if hasattr(ts, "date") else ts
                        if not hasattr(d, "year") or d < cutoff:
                            continue
                        earnings_dates.add(d)
            except Exception as e:
                earnings_degraded = f"{type(e).__name__}: {e}"
                logger.warning(
                    "past-earnings source unavailable for %s (%s) — "
                    "falling back to next-date-only; results may be "
                    "incomplete, not genuinely empty",
                    symbol,
                    earnings_degraded,
                )

            # `ticker.calendar` needs no optional dependency and is already
            # the primary source `get_next_earnings_date` above uses — but
            # it only ever reports the single NEXT scheduled date, never
            # past ones. Folded in here (deduped against earnings_dates
            # above) so "upcoming" markers still render even when lxml is
            # absent and earnings_dates came back empty.
            try:
                calendar = getattr(ticker, "calendar", None)
                if isinstance(calendar, dict):
                    value = calendar.get("Earnings Date")
                    candidates = value if isinstance(value, list) else ([value] if value is not None else [])
                    for candidate in candidates:
                        d = candidate.date() if hasattr(candidate, "date") and callable(candidate.date) else candidate
                        if hasattr(d, "year") and d >= cutoff:
                            earnings_dates.add(d)
            except Exception as e:
                logger.debug("earnings calendar unavailable for %s: %s", symbol, e)

            earnings = [{"date": d.isoformat(), "upcoming": d > today} for d in sorted(earnings_dates)]

            return {
                "dividends": dividends,
                "earnings": earnings,
                "earnings_degraded": earnings_degraded,
            }

        try:
            with ThreadPoolExecutor(max_workers=1) as ex:
                return ex.submit(_fetch).result(timeout=_VALUATION_TIMEOUT_S)
        except FuturesTimeout:
            logger.warning("price-chart events fetch timed out for %s", symbol)
            return {
                "dividends": [],
                "earnings": [],
                "earnings_degraded": f"TimeoutError: fetch exceeded {_VALUATION_TIMEOUT_S}s",
            }
        except Exception as e:
            logger.warning("price-chart events fetch failed for %s: %s", symbol, e)
            return {
                "dividends": [],
                "earnings": [],
                "earnings_degraded": f"{type(e).__name__}: {e}",
            }

    def get_valuation_metrics(self, symbol: str) -> dict:
        """Fetch trailing PE, forward PE, and price-to-sales from yfinance.

        Returns a dict with keys trailing_pe, forward_pe, ps_ratio. Any field
        unavailable (ETFs, newly-listed names, or transient yfinance gaps)
        comes back as None. Bounded by a 10s timeout per symbol so a stalled
        network request can't eat the morning's launchd budget.
        """

        def _fetch():
            try:
                info = yf.Ticker(symbol).info or {}
            except Exception as e:
                record_swallowed("data.market.valuation", e, log=logger, symbol=symbol)
                return {}
            return info

        try:
            with ThreadPoolExecutor(max_workers=1) as ex:
                info = ex.submit(_fetch).result(timeout=_VALUATION_TIMEOUT_S)
        except FuturesTimeout:
            logger.warning("valuation fetch timed out for %s (>%.0fs)", symbol, _VALUATION_TIMEOUT_S)
            info = {}

        def _num(v):
            if v is None:
                return None
            try:
                return round(float(v), 2)
            except (TypeError, ValueError):
                return None

        return {
            "trailing_pe": _num(info.get("trailingPE")),
            "forward_pe": _num(info.get("forwardPE")),
            "ps_ratio": _num(info.get("priceToSalesTrailing12Months")),
        }

    def get_sector_performance(self, period: str = "5d") -> dict[str, float]:
        etf_symbols = list(SECTOR_ETFS.values())

        def _download():
            return yf.download(etf_symbols, period=period, progress=False)

        try:
            with ThreadPoolExecutor(max_workers=1) as ex:
                df = ex.submit(_download).result(timeout=_DOWNLOAD_TIMEOUT_S)
        except FuturesTimeout:
            logger.warning("yfinance sector_performance timed out after %ds", _DOWNLOAD_TIMEOUT_S)
            return {}
        except Exception as e:
            record_swallowed("data.market.sector_performance", e, log=logger)
            return {}
        if df is None or df.empty:
            return {}
        result = {}
        for sector, etf in SECTOR_ETFS.items():
            try:
                if isinstance(df.columns, pd.MultiIndex):
                    # Real yfinance: (field, ticker) — df["Close"][etf]
                    # Some mocks use: (ticker, field) — df[etf]["Close"]
                    level0_vals = df.columns.get_level_values(0).unique().tolist()
                    if "Close" in level0_vals:
                        close = df["Close"][etf]
                    else:
                        close = df[etf]["Close"]
                else:
                    close = df["Close"]
                # Drop NaN before slicing — a delisted / paused ETF in a
                # batch download can return a column with leading/trailing
                # NaN. iloc[0] or iloc[-1] would then yield NaN and silently
                # report a NaN sector return.
                close = close.dropna()
                if len(close) >= 2:
                    pct = ((close.iloc[-1] - close.iloc[0]) / close.iloc[0]) * 100
                    if pd.notna(pct):
                        result[sector] = round(float(pct), 2)
            except (KeyError, IndexError):
                continue
        return result
