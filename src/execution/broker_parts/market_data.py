"""Market-data reads, lifted verbatim from src/execution/broker.py.

Fourth (final) broker instalment. `LivePrice`, `_BROKER_HTTP_TIMEOUT` and
`_install_http_timeout` moved here as-is and are re-exported by
`src.execution.broker`. `MarketData` holds the former `AlpacaBroker` read methods.
"""

from __future__ import annotations

from dataclasses import dataclass
from src.execution.broker_parts.stop_place import _alpaca_symbol
from src.execution.broker_parts.stop_place import _internal_symbol
import logging

from src.execution.broker_parts.trade_stream import _OnState
from src.sentinel.counted import record_swallowed
from src.trading_calendar import utc_now
from src.execution.price_read import read_price_with_retry
from src.execution.broker_parts.http_timeout import (  # noqa: F401 (re-export)
    _BROKER_HTTP_TIMEOUT,
    _install_http_timeout,
)
from src.execution.broker_parts.intraday_snapshots import snapshots_from_client


# Alpaca serves the consolidated SIP feed WITHOUT a paid subscription as
# long as the request's `end` is at least 15 minutes in the past (Alpaca
# market-data FAQ, "Do I need a subscription to get SIP data?"; measured
# 2026-10-10 on this free-plan account: feed=sip daily bars return 200).
# One extra minute of margin keeps clock skew from tipping a request over.
_SIP_DELAY_MINUTES = 16


def _sip_safe_end(now=None):
    """Latest `end` a free-plan SIP history request may carry: now - 16 min (UTC)."""
    from datetime import timedelta as _td

    return (now or utc_now()) - _td(minutes=_SIP_DELAY_MINUTES)


# Same log channel as before the move: operators and tests filter on the
# broker's logger name, and the move must not change what they see.
logger = logging.getLogger("src.execution.broker")


@dataclass(frozen=True)
class LivePrice:
    """A price WITH the provenance needed to decide whether to trust it.

    `get_latest_price` answers "what is it worth" but throws away how it
    knew: a real trade print, or a quote the tape has not confirmed. It also
    never looked at WHEN the trade happened, so a thinly-traded name that
    last printed yesterday, or any read outside market hours, came back
    looking exactly like a live price. Callers that place or move real orders
    need the difference; the reporting callers do not, which is why
    `get_latest_price` keeps its old shape and this is additive.

    `source` is one of `last_trade`, `quote_mid`, `quote_ask`, `quote_bid`.

    Two freshness answers, because two kinds of caller need different
    strictness and collapsing them into one flag would either block ordinary
    trading or wave through yesterday's price:
      - `is_today` — the provider stamped this value with the current ET
        date, whether it is a trade or a quote. A live quote mid-session is
        a legitimate fill reference; yesterday's anything is not.
      - `is_today_print` — additionally a REAL trade print. Only this proves
        the tape actually traded there, which is what deciding where a stop
        belongs requires.
    An unstamped or naive timestamp fails visible rather than passing as
    live — the same rule `src.trading_calendar.live_price_is_today` already
    applies to research snapshots.
    """

    price: float
    source: str
    trade_at: object | None
    is_today: bool
    is_today_print: bool


class MarketData:
    """Market-data reads (movers, bars, quotes, snapshots), lifted verbatim from
    `AlpacaBroker`. Built per call by the broker's thin shims, so `_data_client`
    -- created lazily, after construction -- is read and written on the broker
    (`state`) itself."""

    _data_client = _OnState()
    _screener_client = _OnState()

    def __init__(
        self,
        *,
        state,
        api_key,
        secret_key,
        closed_bars_cache,
        closed_bars_cache_lock,
        get_latest_price_stamped=None,
        _extract_symbol_payload=None,
    ):
        self._state = state
        self.api_key = api_key
        self.secret_key = secret_key
        self._closed_bars_cache = closed_bars_cache
        self._closed_bars_cache_lock = closed_bars_cache_lock
        if get_latest_price_stamped is not None:
            self.get_latest_price_stamped = get_latest_price_stamped
        if _extract_symbol_payload is not None:
            self._extract_symbol_payload = _extract_symbol_payload

    def get_top_movers(self, n: int = 15) -> list[dict]:
        """Return today's top-`n` gainers from Alpaca's screener.

        Output shape: ``[{"symbol": str, "percent_change": float, "price": float}, ...]``,
        sorted by `percent_change` descending as Alpaca returns them.
        Returns `[]` on any failure (SDK error, auth issue, empty response) —
        the missed-opportunity digest falls back to universe-only when the
        top-movers signal is unavailable, so a degraded screener must never
        crash an evening run. Caller treats [] as "no top-mover augmentation".
        """
        if n <= 0:
            return []
        try:
            # Lazy import + lazy-construct so the extra SDK client is only
            # instantiated the first time evening actually runs a digest.
            from alpaca.data.historical.screener import ScreenerClient
            from alpaca.data.requests import MarketMoversRequest
        except ImportError as exc:
            logger.warning("get_top_movers: screener SDK unavailable: %s", exc)
            return []

        if not hasattr(self, "_screener_client") or self._screener_client is None:
            try:
                self._screener_client = ScreenerClient(
                    api_key=self.api_key,
                    secret_key=self.secret_key,
                )
                _install_http_timeout(self._screener_client)
            except Exception as exc:
                record_swallowed("broker.screener_client_init", exc, log=logger)
                self._screener_client = None
                return []

        try:
            movers = self._screener_client.get_market_movers(MarketMoversRequest(top=n))
        except Exception as exc:
            record_swallowed("broker.get_top_movers", exc, log=logger)
            return []

        gainers = getattr(movers, "gainers", None) or []
        out: list[dict] = []
        # Suffix filter — Alpaca's screener returns warrants (.WS, .WSA,
        # .WSB), units (.U, .UN), and rights (.RT) alongside common stock.
        # None of these are tradable as equities in our system, and
        # yfinance 404s on them later — flooding logs with errors. Drop
        # them at the boundary instead. Class shares (e.g. BRK.B) keep
        # the dot but are legitimate; the universe uses the dash form
        # (BRK-B), so any .A/.B from the screener would also be skipped
        # if we filtered too aggressively. So we only filter the
        # non-equity-instrument suffixes explicitly.
        _NON_EQUITY_SUFFIXES = (".WS", ".WSA", ".WSB", ".U", ".UN", ".RT")
        for m in gainers:
            sym = getattr(m, "symbol", None)
            if not sym:
                continue
            alpaca_sym_upper = str(sym).upper()
            if alpaca_sym_upper.endswith(_NON_EQUITY_SUFFIXES):
                continue
            sym_upper = _internal_symbol(alpaca_sym_upper)
            try:
                out.append(
                    {
                        "symbol": sym_upper,
                        "percent_change": float(getattr(m, "percent_change", 0) or 0),
                        "price": float(getattr(m, "price", 0) or 0),
                    }
                )
            except (TypeError, ValueError):
                continue
            if len(out) >= n:
                break
        return out

    def get_bars(self, symbol: str, lookback_days: int = 120) -> list:
        """Fetch daily OHLCV bars from Alpaca as a list[OHLCV].

        The desk's PRIMARY daily history since 2026-10-09:
        `MarketDataProvider.get_ohlcv` calls this and nothing else. Same
        shape as `get_ohlcv`; may include today's still-forming bar, which
        the provider drops. Returns [] on any error (counted).
        """
        from datetime import timedelta as _td
        from src.models import OHLCV
        from src.util.time import et_today

        try:
            if self._data_client is None:
                from alpaca.data.historical.stock import StockHistoricalDataClient

                self._data_client = StockHistoricalDataClient(self.api_key, self.secret_key)
                _install_http_timeout(self._data_client)

            from alpaca.data.enums import Adjustment, DataFeed
            from alpaca.data.requests import StockBarsRequest
            from alpaca.data.timeframe import TimeFrame

            end = et_today()
            start = end - _td(days=lookback_days)
            alpaca_symbol = _alpaca_symbol(symbol)

            def _fetch(range_start, range_end) -> list[OHLCV]:
                req = StockBarsRequest(
                    symbol_or_symbols=alpaca_symbol,
                    timeframe=TimeFrame.Day,
                    start=range_start,
                    # `range_end` is always today; a date would let the
                    # request reach "now", which SIP refuses on this plan.
                    end=_sip_safe_end(),
                    # Split AND dividend adjusted: this read replaced Yahoo's
                    # default (yfinance auto_adjust=True) as the desk's daily
                    # history on 2026-10-09, so indicators see the same
                    # adjusted series they always did. Feed is SIP (the whole
                    # market): IEX alone is ~3% of volume and printed flat
                    # high==low days on thin names. SIP history is free when
                    # `end` is 15+ minutes old (see `_sip_safe_end`).
                    adjustment=Adjustment.ALL,
                    feed=DataFeed.SIP,
                )
                raw = self._data_client.get_stock_bars(req)
                # SDK returns a BarSet-like object with .data = {symbol: [Bar, ...]}
                bars_list = None
                if hasattr(raw, "data") and isinstance(raw.data, dict):
                    bars_list = raw.data.get(alpaca_symbol)
                elif isinstance(raw, dict):
                    bars_list = raw.get(alpaca_symbol)
                if not bars_list:
                    return []
                parsed: list[OHLCV] = []
                for b in bars_list:
                    ts = getattr(b, "timestamp", None)
                    d = ts.date() if ts is not None else None
                    if d is None:
                        continue
                    try:
                        parsed.append(
                            OHLCV(
                                date=d,
                                open=float(getattr(b, "open", 0) or 0),
                                high=float(getattr(b, "high", 0) or 0),
                                low=float(getattr(b, "low", 0) or 0),
                                close=float(getattr(b, "close", 0) or 0),
                                volume=int(getattr(b, "volume", 0) or 0),
                            )
                        )
                    except (TypeError, ValueError):
                        continue
                return parsed

            # Caching: only the portion of the range up to (and including)
            # yesterday can possibly be closed/complete daily bars — Alpaca
            # doesn't publish a daily bar for a session that hasn't closed
            # yet, but we still never trust "today" to a cache: today's
            # entry is always fetched fresh, never cached. Keyed so a new
            # calendar day naturally invalidates the historical portion.
            hist_end = end - _td(days=1)
            cache_key = ("daily", alpaca_symbol, start, hist_end)
            with self._closed_bars_cache_lock:
                cached_hist = self._closed_bars_cache.get(cache_key)

            if cached_hist is None:
                # Cache miss: one fetch over the whole range, exactly as
                # before caching existed. Split the result so only the
                # closed (pre-today) portion is stored.
                all_bars = _fetch(start, end)
                cached_hist = [b for b in all_bars if b.date <= hist_end]
                with self._closed_bars_cache_lock:
                    self._closed_bars_cache[cache_key] = cached_hist
                return all_bars

            # Cache hit: reuse the closed history, only refetch today.
            today_bars = _fetch(end, end)
            out = cached_hist + [b for b in today_bars if b.date not in {c.date for c in cached_hist}]
            out.sort(key=lambda b: b.date)
            return out
        except Exception as e:
            record_swallowed("broker.get_bars", e, log=logger, symbol=symbol)
            return []

    def get_intraday_chart_bars(self, symbol: str, timeframe: str, lookback_days: int) -> list[dict]:
        """Fetch read-only intraday OHLCV bars for Mission Control.

        This deliberately does not participate in trading decisions or
        execution. It uses the same Alpaca historical-data client as
        ``get_bars`` but preserves each bar's timestamp so Lightweight
        Charts can render 5m/15m/1h candles and align execution markers.
        Returns [] on any failure, matching the broker's other market-data
        degradation contracts.
        """
        from datetime import datetime as _dt, timedelta as _td, timezone as _tz
        from src.util.time import ET

        try:
            if self._data_client is None:
                from alpaca.data.historical.stock import StockHistoricalDataClient

                self._data_client = StockHistoricalDataClient(self.api_key, self.secret_key)
                _install_http_timeout(self._data_client)

            from alpaca.data.enums import DataFeed
            from alpaca.data.requests import StockBarsRequest
            from alpaca.data.timeframe import TimeFrame, TimeFrameUnit

            timeframe_value = {
                "5m": TimeFrame(5, TimeFrameUnit.Minute),
                "15m": TimeFrame(15, TimeFrameUnit.Minute),
                "1h": TimeFrame.Hour,
            }.get(timeframe)
            if timeframe_value is None:
                return []

            now = utc_now()
            start = now - _td(days=lookback_days)
            alpaca_symbol = _alpaca_symbol(symbol)

            def _fetch(range_start, range_end) -> list[dict]:
                req = StockBarsRequest(
                    symbol_or_symbols=alpaca_symbol,
                    timeframe=timeframe_value,
                    start=range_start,
                    end=range_end,
                    # This account's market-data plan has real-time IEX only;
                    # SIP is free only for data 15+ minutes old (daily
                    # history uses it, see `get_bars`). Leaving feed unset resolves to SIP server-side for
                    # sub-daily bars and comes back with zero bars for every
                    # symbol/range — silently, since Alpaca doesn't error, it
                    # just returns nothing. This chart read wants the latest
                    # minutes, so it stays on IEX.
                    feed=DataFeed.IEX,
                )
                raw = self._data_client.get_stock_bars(req)
                if hasattr(raw, "data") and isinstance(raw.data, dict):
                    bars_list = raw.data.get(alpaca_symbol)
                elif isinstance(raw, dict):
                    bars_list = raw.get(alpaca_symbol)
                else:
                    bars_list = None
                if not bars_list:
                    return []

                parsed: list[dict] = []
                for bar in bars_list:
                    ts = getattr(bar, "timestamp", None)
                    if ts is None:
                        continue
                    try:
                        parsed.append(
                            {
                                "date": ts.astimezone(ET).date().isoformat(),
                                "timestamp": ts.isoformat(),
                                "open": float(getattr(bar, "open", 0) or 0),
                                "high": float(getattr(bar, "high", 0) or 0),
                                "low": float(getattr(bar, "low", 0) or 0),
                                "close": float(getattr(bar, "close", 0) or 0),
                                "volume": int(getattr(bar, "volume", 0) or 0),
                            }
                        )
                    except (TypeError, ValueError):
                        continue
                return parsed

            # Caching: only prior, fully-closed trading days are cacheable.
            # Today (including its still-forming candle) is always fetched
            # fresh, never cached. The historical portion is cached keyed
            # by the (rounded-to-day) start and today's date, so a new
            # calendar day naturally invalidates it. The start is rounded
            # down to ET midnight of its day (a superset of the exact
            # `start` instant) purely so repeated calls with the same
            # lookback_days share one cache key — outside trading hours
            # Alpaca simply returns nothing extra, so this never fabricates
            # data, only makes the cache key stable.
            today_et = now.astimezone(ET).date()
            today_midnight_et = _dt.combine(today_et, _dt.min.time(), tzinfo=ET)
            start_day_et = start.astimezone(ET).date()
            cache_key = ("intraday", alpaca_symbol, timeframe, start_day_et, today_et)

            with self._closed_bars_cache_lock:
                cached_hist = self._closed_bars_cache.get(cache_key)

            if cached_hist is None:
                # Cache miss: one fetch over the whole range, exactly as
                # before caching existed. Split the result so only the
                # portion from before today is stored.
                all_bars = _fetch(start, now)
                cached_hist = [b for b in all_bars if b["date"] < today_et.isoformat()]
                with self._closed_bars_cache_lock:
                    self._closed_bars_cache[cache_key] = cached_hist
                return all_bars

            # Cache hit: reuse the closed history, only refetch today.
            today_bars = _fetch(today_midnight_et, now)
            out = cached_hist + today_bars
            out.sort(key=lambda b: b["timestamp"])
            return out
        except Exception as exc:
            record_swallowed("broker.intraday_chart_bars", exc, log=logger, symbol=symbol, timeframe=str(timeframe))
            return []

    def get_latest_price_stamped(self, symbol: str) -> "LivePrice | None":
        """Latest price for `symbol` with its source and freshness attached.

        Order of preference is unchanged from `get_latest_price`: a real
        trade print first, then the quote midpoint, then a single side. What
        is new is that the answer says which one it was and whether the trade
        print is from today's ET date, so a caller about to place or move an
        order can refuse a stale number instead of silently acting on it.
        `None` is MEASURED absence; a read that FAILS is retried by
        `src.execution.price_read` and then raises `PriceReadFailed`.
        """
        return read_price_with_retry(self._read_latest_price_stamped_once, symbol, log=logger)

    def _read_latest_price_stamped_once(self, symbol: str) -> "LivePrice | None":
        if self._data_client is None:
            from alpaca.data.historical.stock import StockHistoricalDataClient

            self._data_client = StockHistoricalDataClient(self.api_key, self.secret_key)
            _install_http_timeout(self._data_client)

        from alpaca.data.requests import StockLatestQuoteRequest, StockLatestTradeRequest

        alpaca_symbol = _alpaca_symbol(symbol)

        trade_data = self._data_client.get_stock_latest_trade(StockLatestTradeRequest(symbol_or_symbols=alpaca_symbol))
        trade = self._extract_symbol_payload(trade_data, alpaca_symbol)
        trade_price = float(getattr(trade, "price", 0) or 0)
        if trade_price > 0:
            trade_at = getattr(trade, "timestamp", None)
            from src.trading_calendar import live_price_is_today

            fresh = bool(live_price_is_today(trade_at))
            return LivePrice(
                price=trade_price,
                source="last_trade",
                trade_at=trade_at,
                is_today=fresh,
                is_today_print=fresh,
            )

        quote_data = self._data_client.get_stock_latest_quote(StockLatestQuoteRequest(symbol_or_symbols=alpaca_symbol))
        quote = self._extract_symbol_payload(quote_data, alpaca_symbol)
        ask_price = float(getattr(quote, "ask_price", 0) or 0)
        bid_price = float(getattr(quote, "bid_price", 0) or 0)
        quote_at = getattr(quote, "timestamp", None)
        from src.trading_calendar import live_price_is_today

        quote_today = bool(live_price_is_today(quote_at))
        if ask_price > 0 and bid_price > 0:
            return LivePrice(
                price=(ask_price + bid_price) / 2,
                source="quote_mid",
                trade_at=quote_at,
                is_today=quote_today,
                is_today_print=False,
            )
        if ask_price > 0:
            return LivePrice(
                price=ask_price,
                source="quote_ask",
                trade_at=quote_at,
                is_today=quote_today,
                is_today_print=False,
            )
        if bid_price > 0:
            return LivePrice(
                price=bid_price,
                source="quote_bid",
                trade_at=quote_at,
                is_today=quote_today,
                is_today_print=False,
            )
        return None

    def get_latest_price(self, symbol: str) -> float | None:
        """Latest price as a bare number; a FAILED read raises `PriceReadFailed`.

        Reporting and grading callers ("how far has this moved since we sold
        it") do not care where the number came from; each wraps this call and
        degrades to a last close. Anything that places or moves an order
        should call `get_latest_price_stamped` and check `is_today_print`.
        """
        stamped = self.get_latest_price_stamped(symbol)
        return stamped.price if stamped is not None else None

    def get_latest_quote(self, symbol: str) -> dict[str, float | None]:
        """Return the current bid/ask without inventing a side of the book.

        Execution uses the ask to construct a bounded marketable BUY limit
        and the bid to construct the mirrored SHORT floor. Missing or failed
        quote data returns explicit ``None`` fields so the caller can retain
        its existing last-trade behavior without guessing.
        """
        out = {"bid_price": None, "ask_price": None}
        try:
            if self._data_client is None:
                from alpaca.data.historical.stock import StockHistoricalDataClient

                self._data_client = StockHistoricalDataClient(self.api_key, self.secret_key)
                _install_http_timeout(self._data_client)

            from alpaca.data.requests import StockLatestQuoteRequest

            alpaca_symbol = _alpaca_symbol(symbol)
            quote_data = self._data_client.get_stock_latest_quote(
                StockLatestQuoteRequest(symbol_or_symbols=alpaca_symbol)
            )
            quote = self._extract_symbol_payload(quote_data, alpaca_symbol)
            for field in out:
                try:
                    value = float(getattr(quote, field, 0) or 0)
                except (TypeError, ValueError):
                    value = 0.0
                out[field] = value if value > 0 else None
        except Exception as exc:
            logger.warning("Failed to fetch latest quote for %s: %s", symbol, exc)
        return out

    def read_latest_trade_prints(self, symbols: list[str]) -> dict[str, LivePrice]:
        """ONE batched latest-trades read for many names; only TODAY prints answer.

        Used by the entry stage's today-print wait
        (`src.price_feed_preflight.wait_for_today_prints`): every waiting name
        goes into one request, never one request per name. Same
        retry-then-typed-failure contract as `get_latest_price_stamped`: a read
        that FAILS after the ledgered retry raises `PriceReadFailed`; an empty
        dict is measured absence."""

        def once(wanted: list[str]) -> dict[str, LivePrice]:
            from alpaca.data.requests import StockLatestTradeRequest
            from src.trading_calendar import live_price_is_today

            if self._data_client is None:
                from alpaca.data.historical.stock import StockHistoricalDataClient

                self._data_client = StockHistoricalDataClient(self.api_key, self.secret_key)
                _install_http_timeout(self._data_client)
            by_alpaca = {_alpaca_symbol(s): s for s in wanted}
            trade_data = self._data_client.get_stock_latest_trade(
                StockLatestTradeRequest(symbol_or_symbols=list(by_alpaca))
            )
            prints: dict[str, LivePrice] = {}
            for alpaca_symbol, symbol in by_alpaca.items():
                trade = self._extract_symbol_payload(trade_data, alpaca_symbol)
                trade_price = float(getattr(trade, "price", 0) or 0)
                trade_at = getattr(trade, "timestamp", None)
                if trade_price > 0 and live_price_is_today(trade_at):
                    prints[symbol] = LivePrice(
                        price=trade_price,
                        source="last_trade",
                        trade_at=trade_at,
                        is_today=True,
                        is_today_print=True,
                    )
            return prints

        return read_price_with_retry(once, list(symbols), log=logger)

    def get_bars_batch(
        self, symbols: list[str], lookback_days: int, on_page=None, adjustment: str = "split"
    ) -> dict[str, list]:
        """Daily bars for many symbols, one Alpaca request per page.

        `adjustment`: "split" (default — the universe screen's read, which
        ranks on recent price and volume where dividends do not change the
        answer) or "all" (splits AND dividends — what
        `MarketDataProvider.get_ohlcv_batch` passes, so the batched history
        behind target revision and admission is the SAME series as the
        single-symbol `get_bars` below, `Adjustment.ALL`).

        Follows `next_page_token` here (not inside the SDK) so every page is
        visible: `on_page()` is called once per page requested. The client is
        built HERE, where the replay patches it. Any failure (HTTP 429 or
        otherwise) RAISES and returns nothing for the whole call, so the
        caller records every name in the chunk as unavailable; a half-read
        chunk is never returned and nothing is retried here (the SDK's own
        bounded retry applies). The SDK exposes no rate-limit headers, so the
        caller paces by issuing chunks one after another.
        """
        from datetime import datetime as _dt, timedelta as _td
        from src.models import OHLCV
        from src.util.time import et_today

        if not symbols:
            return {}
        if self._data_client is None:
            from alpaca.data.historical.stock import StockHistoricalDataClient

            self._data_client = StockHistoricalDataClient(self.api_key, self.secret_key)
            _install_http_timeout(self._data_client)
        from alpaca.data.enums import Adjustment, DataFeed
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame

        end = et_today()
        wanted = {_alpaca_symbol(s): s for s in symbols}
        req = StockBarsRequest(
            symbol_or_symbols=list(wanted),
            timeframe=TimeFrame.Day,
            start=end - _td(days=int(lookback_days)),
            end=_sip_safe_end(),
            # Default split-adjusted only (the screen's read; see docstring).
            # The provider's batch passes "all" to match `get_bars`. Feed is
            # SIP: the screen ranks on volume, and IEX is ~3% of it. SIP
            # history is free when `end` is 15+ minutes old (`_sip_safe_end`).
            adjustment=Adjustment(adjustment),
            feed=DataFeed.SIP,
        )
        fields = req.to_request_fields()
        out: dict[str, list] = {s: [] for s in symbols}
        token = None
        seen: set = set()
        while True:
            if on_page is not None:
                on_page()
            params = dict(fields)
            params["limit"] = 10_000
            if token:
                params["page_token"] = token
            page = self._data_client.get(path="/stocks/bars", data=params) or {}
            for sym, rows in (page.get("bars") or {}).items():
                internal = wanted.get(sym)
                if internal is None:
                    continue
                for row in rows or []:
                    try:
                        stamp = _dt.fromisoformat(str(row["t"]).replace("Z", "+00:00"))
                        out[internal].append(
                            OHLCV(
                                date=stamp.date(),
                                open=float(row.get("o") or 0),
                                high=float(row.get("h") or 0),
                                low=float(row.get("l") or 0),
                                close=float(row.get("c") or 0),
                                volume=int(row.get("v") or 0),
                            )
                        )
                    except (KeyError, TypeError, ValueError):
                        continue
            token = page.get("next_page_token")
            if not token or token in seen:
                break
            seen.add(token)
        for rows in out.values():
            rows.sort(key=lambda b: b.date)
        return out

    def get_intraday_snapshots(self, symbols: list[str]) -> dict[str, dict]:
        """Bulk current-session move data; see `snapshots_from_client` for the
        payload. The client is built HERE, where the replay patches it."""
        if not symbols:
            return {}
        if self._data_client is None:
            try:
                from alpaca.data.historical.stock import StockHistoricalDataClient

                self._data_client = StockHistoricalDataClient(self.api_key, self.secret_key)
                _install_http_timeout(self._data_client)
            except Exception as exc:
                record_swallowed("broker.intraday_snapshots_client_init", exc, log=logger)
                return {}
        from alpaca.data.requests import StockSnapshotRequest

        return snapshots_from_client(self._data_client, symbols, StockSnapshotRequest)

    @staticmethod
    def _extract_symbol_payload(payload, symbol: str):
        if isinstance(payload, dict):
            return payload.get(symbol)
        try:
            return payload[symbol]
        except Exception:
            return getattr(payload, symbol, None)
