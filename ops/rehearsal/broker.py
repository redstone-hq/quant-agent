"""The one thing in a rehearsal that must not be real: the broker.

WHAT IS STUBBED, EXACTLY
------------------------
Not `AlpacaBroker`. The rehearsal uses the real `AlpacaBroker` class, with all
1,988 lines of its logic intact — tick-size quantization, the 20%-deviation
fat-finger guard, symbol normalization, the OrderStatus enum-value unwrap that
a 2026-05-27 audit found could mask a real rejection, the highest-stop
selection in `get_current_stop_price`, the post-fill (not OTO) protective-stop
placement that the 2026-07-16 audit rewrote. All of it runs.

What is replaced is one layer lower: the two alpaca-py SDK client objects the
broker holds, `self.client` (TradingClient) and `self._data_client`
(StockHistoricalDataClient). Those are the only two objects in `AlpacaBroker`
that put a request on the network. Swapping them is the narrowest possible cut
that still guarantees no order can reach the account, and it means the code
under test is the code that ships.

WHERE THE ANSWERS COME FROM
---------------------------
Reads are served from the rehearsal's own database copy — real production
data, not invented numbers:

  * positions      <- the `positions` table (broker-synced by every session)
  * equity history <- the `daily_pnl` table
  * prices         <- `positions.current_price` for held names

Anything with no evidence behind it returns the same "I don't know" a degraded
Alpaca returns: `None` price, `[]` bars, `{}` quote. The pipeline then takes
its real degradation path and records a real skip (`no_price`), which shows up
in the report. Nothing is fabricated to keep a rehearsal green.

WHAT THIS DOES NOT REPRESENT — READ BEFORE TRUSTING A RESULT
------------------------------------------------------------
The stub answers "would this session have produced these orders?". It cannot
answer "would those orders have worked?". Specifically it does NOT model:

  * **Fills.** With the default `fill_model="immediate"` a submitted order is
    reported filled, in full, at the price we asked for. Real fills partial,
    slip, queue behind other flow, or never happen. The 2026-08-19 incident —
    a funding sell that filled 36 seconds after the session gave up waiting —
    is invisible to this harness by construction. `fill_model="unfilled"`
    rehearses the opposite extreme.
  * **Broker-side rejection.** Buying power, PDT, fractionability, halted or
    non-shortable symbols, wash-trade blocks, minimum notional. Alpaca
    enforces these server-side; a rehearsal never asks it.
  * **Latency and ordering.** Every call returns instantly. Races between
    submission, cancellation and fill cannot occur here.
  * **Live prices.** Prices are the last close-of-session values production
    stored, not this morning's tape. A stop or limit computed against them is
    arithmetically real but temporally stale.
  * **Trading-day truth.** The exchange calendar is not consulted (that is a
    network call). The rehearsal asserts the date it was asked to rehearse is
    a trading day unless it falls on a weekend. Rehearsing a market holiday
    will therefore proceed where production would have stopped.

Every one of these appears in the rehearsal report under "what this rehearsal
could not tell you", so the limitation travels with the result.
"""

from __future__ import annotations

import contextlib
import logging
import sqlite3
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, time as dt_time, timezone
from types import SimpleNamespace

from ops.rehearsal.stand_in import AmendEndpoint, LoudStandIn, status_matches

logger = logging.getLogger(__name__)

FILL_IMMEDIATE = "immediate"
FILL_UNFILLED = "unfilled"


class BrokerReachAttempted(RuntimeError):
    """The session called a broker capability the rehearsal cannot answer."""


# --------------------------------------------------------------- snapshot


@dataclass
class BrokerSnapshot:
    """Account truth for a rehearsal, read out of the database copy."""

    as_of: date
    cash: float = 0.0
    portfolio_value: float = 0.0
    last_equity: float = 0.0
    positions: list[dict] = field(default_factory=list)
    prices: dict[str, float] = field(default_factory=dict)
    equity_curve: list[tuple[str, float]] = field(default_factory=list)
    #: symbol -> stop price of the protective order production is holding for
    #: it right now. Reconstructed from the trade ledger, see
    #: `_standing_stops`.
    standing_stops: dict[str, float] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    @classmethod
    def from_database(cls, db_path: str, as_of: date) -> "BrokerSnapshot":
        """Reconstruct the account from what production last recorded.

        `cash` is not stored anywhere, so it is derived as
        ``total_value - sum(position market values)`` from the most recent
        `daily_pnl` row at or before `as_of`. That is arithmetic on real
        recorded numbers, not an estimate, but it is only as fresh as the last
        evening snapshot — which the report says out loud.
        """
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        notes: list[str] = []
        try:
            rows = conn.execute(
                "SELECT symbol, qty, avg_entry, current_price, market_value, "
                "unrealized_pnl, sector, updated_at FROM positions "
                "WHERE qty > 0 ORDER BY symbol"
            ).fetchall()
            positions = [dict(r) for r in rows]
            standing_stops = _standing_stops(
                conn, [str(p["symbol"]) for p in positions], as_of,
            )
            if standing_stops:
                notes.append(
                    f"{len(standing_stops)} protective stop(s) reconstructed "
                    "from the trade ledger, so the session starts with the "
                    "coverage production actually holds"
                )
            missing = [
                str(p["symbol"]) for p in positions
                if str(p["symbol"]) not in standing_stops
            ]
            if missing:
                notes.append(
                    "no recorded stop for " + ", ".join(sorted(missing))
                    + " — the session will see these as uncovered, which is "
                    "correct only if production has no stop for them either"
                )

            pnl_row = conn.execute(
                "SELECT date, total_value, equity_close FROM daily_pnl "
                "WHERE date <= ? ORDER BY date DESC LIMIT 1",
                (as_of.isoformat(),),
            ).fetchone()
            curve = [
                (str(r["date"]), float(r["equity_close"] or r["total_value"] or 0.0))
                for r in conn.execute(
                    "SELECT date, total_value, equity_close FROM daily_pnl "
                    "WHERE date <= ? ORDER BY date",
                    (as_of.isoformat(),),
                ).fetchall()
            ]
        finally:
            conn.close()

        invested = sum(float(p.get("market_value") or 0.0) for p in positions)
        if pnl_row is not None:
            total_value = float(pnl_row["total_value"] or 0.0)
            last_equity = float(pnl_row["equity_close"] or total_value)
            notes.append(
                f"account equity taken from the daily_pnl row for "
                f"{pnl_row['date']} (${total_value:,.2f})"
            )
        else:
            total_value = invested
            last_equity = invested
            notes.append(
                "no daily_pnl row at or before the rehearsed date — account "
                "equity falls back to the sum of recorded position values"
            )
        cash = max(0.0, total_value - invested)
        notes.append(
            f"{len(positions)} position(s) restored from the positions table; "
            f"cash derived as equity minus invested value (${cash:,.2f})"
        )
        return cls(
            as_of=as_of,
            cash=cash,
            portfolio_value=total_value,
            last_equity=last_equity,
            positions=positions,
            prices={
                str(p["symbol"]): float(p["current_price"] or 0.0)
                for p in positions
                if p.get("current_price")
            },
            equity_curve=curve,
            standing_stops=standing_stops,
            notes=notes,
        )


def _standing_stops(conn, symbols: list[str], as_of) -> dict[str, float]:
    """The protective stop production is holding for each symbol.

    Read from the trade ledger rather than left empty. An empty order book
    makes every held position look unprotected, so the session's stop-coverage
    repair fires on all of them at open — which is both noise and, worse, a
    mask: a genuine coverage gap becomes invisible among the false ones.

    The most recent row carrying a stop wins, so a stop that has ratcheted up
    via TRAIL_STOP is reflected rather than the original entry stop. Sells are
    included for that reason; a symbol sold to flat is not in `symbols`.
    """
    stops: dict[str, float] = {}
    for symbol in symbols:
        row = conn.execute(
            "SELECT stop_loss FROM trades WHERE symbol=? AND stop_loss IS NOT NULL "
            "AND stop_loss > 0 AND date(timestamp) <= ? "
            "ORDER BY timestamp DESC LIMIT 1",
            (symbol, as_of.isoformat()),
        ).fetchone()
        if row and row["stop_loss"]:
            stops[symbol] = float(row["stop_loss"])
    return stops


# --------------------------------------------------------- SDK stand-ins


class _Status(str):
    """A string that also answers `.value`, like alpaca-py's OrderStatus."""

    @property
    def value(self) -> str:
        return str(self)


@dataclass
class RecordedOrder:
    """An order the session tried to place. It went nowhere."""

    order_id: str
    symbol: str
    side: str
    qty: float
    order_type: str
    limit_price: float | None
    stop_price: float | None
    time_in_force: str
    submitted_at: datetime
    status: str

    def as_plain(self) -> dict:
        return {
            "id": self.order_id,
            "symbol": self.symbol,
            "side": self.side,
            "qty": self.qty,
            "type": self.order_type,
            "limit_price": self.limit_price,
            "stop_price": self.stop_price,
            "status": self.status,
        }


class RehearsalTradingClient(LoudStandIn, AmendEndpoint):
    """Stands in for `alpaca.trading.TradingClient`. Submits nothing."""

    def __init__(self, snapshot: BrokerSnapshot, *, now: datetime,
                 fill_model: str = FILL_IMMEDIATE):
        self._snapshot = snapshot
        self._now = now
        self._fill_model = fill_model
        self.submitted: list[RecordedOrder] = []
        self.cancelled: list[str] = []
        self._orders: dict[str, RecordedOrder] = {}
        self.unsupported_calls: list[str] = []
        self._seed_standing_stops(now)

    def _seed_standing_stops(self, now: datetime) -> None:
        """Open the order book with the protective stops production holds.

        Without these the session sees every position as uncovered and repairs
        all of them at open — noise that also hides a real coverage gap.
        These are marked `pre_existing` so the report can tell what the
        session placed from what it inherited.

        Each is a stop-MARKET with no limit price, split and timed exactly as
        the live broker places one: a GTC leg over the whole shares and a DAY
        leg over any sub-share remainder (owner ruling 2026-09-25; the
        stop-limit fallback was deleted 2026-10-13).
        """
        from src.execution.order_gates import _derive_stop_tif, _split_protective_qty

        held = {str(p["symbol"]): float(p.get("qty") or 0.0)
                for p in self._snapshot.positions}
        for symbol, stop in sorted(self._snapshot.standing_stops.items()):
            qty = held.get(symbol, 0.0)
            if qty <= 0:
                continue
            whole, frac = _split_protective_qty(qty)
            legs = [(f"pre-existing-stop-{symbol}-day", frac)] if frac > 0 else []
            if whole >= 1:
                legs.append((f"pre-existing-stop-{symbol}", whole))
            for order_id, leg_qty in legs:
                self._orders[order_id] = RecordedOrder(
                    order_id=order_id,
                    symbol=symbol,
                    side="sell",
                    qty=leg_qty,
                    order_type="stop",
                    limit_price=None,
                    stop_price=stop,
                    time_in_force=_derive_stop_tif(leg_qty).value,
                    submitted_at=now,
                    status="pre_existing",
                )

    # -- account / positions ------------------------------------------------

    def get_account(self):
        snap = self._snapshot
        return SimpleNamespace(
            cash=snap.cash,
            portfolio_value=snap.portfolio_value,
            last_equity=snap.last_equity,
            # The settled, non-margin figure. Equal to cash here because the
            # rehearsal has no unsettled same-day sale proceeds to model.
            non_marginable_buying_power=snap.cash,
        )

    def get_all_positions(self):
        out = []
        for p in self._snapshot.positions:
            qty = float(p.get("qty") or 0.0)
            price = float(p.get("current_price") or 0.0)
            out.append(SimpleNamespace(
                symbol=str(p["symbol"]).replace("-", "."),
                qty=qty,
                avg_entry_price=float(p.get("avg_entry") or 0.0),
                current_price=price,
                market_value=float(p.get("market_value") or qty * price),
                unrealized_pl=float(p.get("unrealized_pnl") or 0.0),
                unrealized_intraday_pl=0.0,
            ))
        return out

    def get_portfolio_history(self, *args, **kwargs):
        curve = self._snapshot.equity_curve
        timestamps = [
            int(datetime.combine(
                date.fromisoformat(d), dt_time(21, 0), tzinfo=timezone.utc,
            ).timestamp())
            for d, _ in curve
        ]
        return SimpleNamespace(
            timestamp=timestamps,
            equity=[value for _, value in curve],
            profit_loss=[0.0] * len(curve),
        )

    # -- calendar -----------------------------------------------------------

    def get_calendar(self, request):
        """Assert the rehearsed date, unless it is a weekend.

        The real answer is a network lookup. Rather than fabricate a holiday
        calendar, the rehearsal treats the date it was explicitly asked to
        rehearse as a session date. The report states this.
        """
        start = getattr(request, "start", None) or self._now.date()
        if isinstance(start, datetime):
            start = start.date()
        if start.weekday() >= 5:
            return []
        close = datetime.combine(start, dt_time(16, 0))
        return [SimpleNamespace(date=start, open=datetime.combine(start, dt_time(9, 30)),
                                close=close)]

    # -- orders -------------------------------------------------------------

    def get_orders(self, filter=None):  # noqa: A002 — alpaca-py's parameter name
        """The seeded standing stops plus whatever this rehearsal placed;
        `status=OPEN` hides filled, cancelled and replaced orders exactly as
        the broker does, so an amended stop reads back as ONE open order."""
        wanted_symbols = {
            str(s).replace("-", ".") for s in (getattr(filter, "symbols", None) or [])
        }
        side = getattr(getattr(filter, "side", None), "value", getattr(filter, "side", None))
        status = getattr(filter, "status", None)
        out = []
        for order in self._orders.values():
            if wanted_symbols and order.symbol not in wanted_symbols:
                continue
            if side and str(side).lower() != order.side:
                continue
            if not status_matches(status, order.status):
                continue
            out.append(SimpleNamespace(
                id=order.order_id,
                symbol=order.symbol,
                side=_Status(order.side),
                qty=order.qty,
                status=_Status(order.status),
                order_type=_Status(order.order_type),
                stop_price=order.stop_price,
                limit_price=order.limit_price,
                filled_qty=(order.qty if order.status == "filled" else 0.0),
                filled_avg_price=(order.limit_price or self._price(order.symbol)
                                  if order.status == "filled" else 0.0),
                legs=None,
            ))
        return out

    def get_order_by_id(self, order_id):
        order = self._orders.get(str(order_id))
        if order is None:
            raise BrokerReachAttempted(
                f"rehearsal has no record of order {order_id}"
            )
        filled = order.status == "filled"
        return SimpleNamespace(
            id=order.order_id,
            symbol=order.symbol,
            side=_Status(order.side),
            qty=order.qty,
            status=_Status(order.status),
            filled_qty=(order.qty if filled else 0.0),
            filled_avg_price=(
                order.limit_price or self._price(order.symbol) if filled else 0.0
            ),
        )

    def submit_order(self, request):
        symbol = str(getattr(request, "symbol", ""))
        side = str(getattr(getattr(request, "side", None), "value",
                           getattr(request, "side", ""))).lower()
        qty = float(getattr(request, "qty", 0) or 0)
        limit_price = getattr(request, "limit_price", None)
        stop_price = getattr(request, "stop_price", None)
        order_type = "limit" if limit_price is not None else "market"
        if stop_price is not None:
            order_type = "stop_limit" if limit_price is not None else "stop"
        status = "filled" if self._fill_model == FILL_IMMEDIATE else "accepted"
        # Protective stops rest in the book; they do not fill on submission.
        if "stop" in order_type:
            status = "new"
        order = RecordedOrder(
            order_id=f"rehearsal-{uuid.uuid4().hex[:12]}",
            symbol=symbol,
            side=side,
            qty=qty,
            order_type=order_type,
            limit_price=(float(limit_price) if limit_price is not None else None),
            stop_price=(float(stop_price) if stop_price is not None else None),
            time_in_force=str(getattr(getattr(request, "time_in_force", None), "value",
                                      getattr(request, "time_in_force", ""))),
            submitted_at=self._now,
            status=status,
        )
        self.submitted.append(order)
        self._orders[order.order_id] = order
        logger.info(
            "Rehearsal broker: RECORDED (not submitted) %s %s x%s @ %s -> %s",
            side, symbol, qty, limit_price or "market", status,
        )
        return SimpleNamespace(
            id=order.order_id,
            symbol=symbol,
            status=_Status(status),
            qty=qty,
            filled_qty=(qty if status == "filled" else 0.0),
            filled_avg_price=(order.limit_price or self._price(symbol)
                              if status == "filled" else 0.0),
        )

    def cancel_order_by_id(self, order_id):
        self.cancelled.append(str(order_id))
        order = self._orders.get(str(order_id))
        if order is not None:
            order.status = "canceled"
        return None

    def cancel_orders(self):
        for order in self._orders.values():
            order.status = "canceled"
        return []

    def close_position(self, symbol, *args, **kwargs):
        # A liquidation the stand-in cannot model is a gap, never a soft no.
        self.unsupported_calls.append(f"close_position({symbol})")
        raise BrokerReachAttempted(
            f"close_position({symbol}) is a live liquidation the rehearsal "
            f"cannot model; the run is void")

    def get_asset(self, symbol):
        """No asset directory offline: the desk fails closed on this, and the
        run is void because the real broker would have answered."""
        self.unsupported_calls.append(f"get_asset({symbol})")
        raise BrokerReachAttempted(
            "asset directory is unavailable offline; eligibility fails closed"
        )

    def _price(self, symbol: str) -> float:
        return float(self._snapshot.prices.get(symbol.replace(".", "-"), 0.0) or 0.0)


class RehearsalDataClient(LoudStandIn):
    """Stands in for `StockHistoricalDataClient`. Serves recorded prices only."""

    def __init__(self, snapshot: BrokerSnapshot):
        self._snapshot = snapshot
        self.missing_price_symbols: list[str] = []

    def _lookup(self, symbol: str) -> float | None:
        price = self._snapshot.prices.get(symbol.replace(".", "-"))
        if price is None or price <= 0:
            if symbol not in self.missing_price_symbols:
                self.missing_price_symbols.append(symbol)
            return None
        return float(price)

    @staticmethod
    def _symbols(request) -> list[str]:
        raw = getattr(request, "symbol_or_symbols", [])
        return [raw] if isinstance(raw, str) else list(raw or [])

    def get_stock_latest_trade(self, request):
        out = {}
        for symbol in self._symbols(request):
            price = self._lookup(symbol)
            out[symbol] = SimpleNamespace(price=price or 0.0)
        return out

    def get_stock_latest_quote(self, request):
        out = {}
        for symbol in self._symbols(request):
            price = self._lookup(symbol)
            # No recorded bid/ask exists. Reporting the last recorded trade on
            # both sides is a zero-spread assumption, and the report says so
            # rather than the harness inventing a spread.
            out[symbol] = SimpleNamespace(
                bid_price=price or 0.0, ask_price=price or 0.0,
            )
        return out

    def get_stock_snapshot(self, request):
        out = {}
        for symbol in self._symbols(request):
            price = self._lookup(symbol)
            out[symbol] = SimpleNamespace(
                latest_trade=SimpleNamespace(price=price or 0.0),
                previous_daily_bar=None,
                daily_bar=None,
            )
        return out

    def get_stock_bars(self, request):
        """No recorded bar history exists offline. Return nothing, honestly.

        The pipeline treats an empty bar set as a degraded data source and
        records it in `ctx.data_status`, which the report surfaces. Inventing
        a synthetic price series would put fabricated numbers through the
        real technical analysis and risk sizing — the opposite of what this
        harness is for.
        """
        for symbol in self._symbols(request):
            if symbol not in self.missing_price_symbols:
                self.missing_price_symbols.append(symbol)
        return SimpleNamespace(data={})


# ------------------------------------------------------------- assembly


def install_rehearsal_broker(
    broker, snapshot: BrokerSnapshot, *, now: datetime,
    fill_model: str = FILL_IMMEDIATE,
):
    """Swap a live `AlpacaBroker`'s two SDK clients for rehearsal stand-ins.

    The broker object, and every line of its logic, is the production one.
    Returns the trading stub so the runner can read back what was recorded.
    """
    from ops.rehearsal.isolation import SENTINEL_KEY

    trading = RehearsalTradingClient(snapshot, now=now, fill_model=fill_model)
    data = RehearsalDataClient(snapshot)
    data.unsupported_calls = trading.unsupported_calls  # one journal, one verdict
    broker.client = trading
    broker._data_client = data
    broker.api_key = SENTINEL_KEY
    broker.secret_key = SENTINEL_KEY
    # A trading-day answer cached from a previous life would silently decide
    # whether the session runs at all.
    broker._trading_day_cache = {}
    return trading


def blocked_market_data(record: list[str]):
    """A `MarketDataProvider` replacement that fetches nothing.

    yfinance is a live HTTP fetch with no disk cache, so a rehearsal cannot
    reproduce it offline, deterministically or for free. Rather than let the
    socket wall raise a confusing exception from inside a thread pool, the
    provider is replaced with one that returns the same empty result a
    yfinance outage produces — the pipeline's real degradation path, which it
    already has code for — and records what was asked for.
    """
    from src.data.market import MarketDataProvider

    class RehearsalMarketData(MarketDataProvider):
        def get_ohlcv(self, symbol: str, lookback_days: int = 120):
            entry = f"daily bars for {symbol}"
            if entry not in record:
                record.append(entry)
            # The Alpaca fallback the pipeline wires in is itself stubbed and
            # also has no bars, so this is honestly empty either way.
            return []

        def get_valuation_metrics(self, symbol: str):
            entry = f"valuation metrics for {symbol}"
            if entry not in record:
                record.append(entry)
            return {}

        def get_upcoming_ex_dividend(self, symbol: str):
            return {}

    return RehearsalMarketData()


@contextlib.contextmanager
def recorded_sector_lookup(record: list[str], recording: dict | None):
    """Serve `broker._get_sector` from the recording, never from yfinance.

    Board item 202, criterion 2. `_get_sector` builds its OWN live market-data
    access — `yf.Ticker(symbol).info` — so swapping `pipeline.market` for the
    recording never touched it. Behind the rehearsal's network wall every one
    of those calls still went out, was blocked, and was RETRIED by yfinance's
    crumb/cookie machinery one symbol at a time: ~188s of a rehearsal spent
    failing to reach Yahoo [measured 2026-10-01], plus one journalled outbound
    attempt that (correctly) voids the run.

    Patching the `yf` name inside `src.execution.broker` is deliberate: it is
    the single place the live client is built, so the ETF table, the cache,
    the timeout and the honest "Unknown" degradation above it stay REAL, and
    no importer of `_get_sector` (`src.pipeline` binds it at import time) can
    route around the patch.

    Nothing is invented. A symbol the recording carries is answered from the
    recording; a symbol it does not is recorded in `record` as a missing
    recorded input — which `assert_hermetic` turns into a loud stop — and the
    lookup takes the same path a yfinance outage takes, which the sector gate
    already surfaces to the owner as "Unknown".
    """
    from src.execution import broker as _broker_module

    sectors = {
        str(k).upper(): v
        for k, v in dict((recording or {}).get("sectors") or {}).items()
        if v
    }

    class _OfflineTicker:
        def __init__(self, symbol: str):
            self._symbol = str(symbol or "").upper()

        @property
        def info(self) -> dict:
            recorded = sectors.get(self._symbol)
            if recorded:
                return {"sector": recorded}
            note = (
                f"sector for {self._symbol} (not in the market recording; "
                "capture one that includes sectors)"
            )
            if note not in record:
                record.append(note)
            return {}

    original = _broker_module.yf
    _broker_module.yf = SimpleNamespace(Ticker=_OfflineTicker)
    try:
        yield (
            "sector lookups are served from the recording "
            f"({len(sectors)} symbols) and can no longer reach yfinance"
        )
    finally:
        _broker_module.yf = original
