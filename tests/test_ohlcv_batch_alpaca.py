"""`MarketDataProvider.get_ohlcv_batch` reads Alpaca, not Yahoo.

Measured 2026-10-12: the batch read (open-position target revision, universe
admission) downloaded daily bars from Yahoo while the single-symbol
`get_ohlcv` read Alpaca — two price sources for one decision; ETN and META
targets were revised from Yahoo bars. The batch must go through the broker's
existing multi-symbol reader (`get_bars_batch`), with the SAME price
adjustment the single-symbol read asks for, and keep its return shape.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from src.data.market import MarketDataProvider, NoBarsSourceError
from src.models import OHLCV

CUTOFF = date(2026, 10, 9)  # a Friday; "today" below is the next Monday


def _bar(d: date, px: float = 10.0) -> OHLCV:
    return OHLCV(date=d, open=px, high=px + 1, low=px - 1, close=px, volume=100)


class _FakeBroker:
    """Shape of `AlpacaBroker`: single and batch daily readers on one object."""

    def __init__(self, bars_by_symbol):
        self.bars_by_symbol = bars_by_symbol
        self.batch_calls: list[tuple] = []

    # Both real readers hand bars back oldest first.
    def get_bars(self, symbol, lookback_days=120):
        return sorted(self.bars_by_symbol.get(symbol, []), key=lambda b: b.date)

    def get_bars_batch(self, symbols, lookback_days, on_page=None, **kwargs):
        self.batch_calls.append((list(symbols), lookback_days, kwargs))
        return {s: sorted(self.bars_by_symbol.get(s, []), key=lambda b: b.date) for s in symbols}


@pytest.fixture
def frozen_calendar():
    with (
        patch("src.data.market.last_completed_bar_date", return_value=CUTOFF),
        patch("src.data.market.et_today", return_value=CUTOFF + timedelta(days=3)),
    ):
        yield


def _yahoo_forbidden():
    return patch(
        "src.data.market.yf.download",
        side_effect=AssertionError("get_ohlcv_batch reached yfinance"),
    )


def test_batch_never_calls_yfinance(frozen_calendar):
    broker = _FakeBroker({"ETN": [_bar(CUTOFF)], "META": [_bar(CUTOFF)]})
    provider = MarketDataProvider()
    provider.set_fallback_bars(broker.get_bars)  # the pipeline's existing wiring, unchanged
    with _yahoo_forbidden():
        got = provider.get_ohlcv_batch(["ETN", "META"], 120)
    assert set(got) == {"ETN", "META"}
    assert broker.batch_calls and broker.batch_calls[0][0] == ["ETN", "META"]
    assert broker.batch_calls[0][1] == 120


def test_batch_return_shape_matches_single_read(frozen_calendar):
    """dict keyed by every requested symbol (upper-cased), [] for a miss,
    completed bars only, oldest first — what target revision and the
    universe screen already consume."""
    forming = CUTOFF + timedelta(days=3)  # a still-forming session bar
    broker = _FakeBroker(
        {
            "AAPL": [_bar(forming), _bar(CUTOFF), _bar(CUTOFF - timedelta(days=1))],
        }
    )
    provider = MarketDataProvider(bars_source=broker.get_bars)
    with _yahoo_forbidden():
        got = provider.get_ohlcv_batch([" aapl ", "GONE", ""], 30)
    assert set(got) == {"AAPL", "GONE"}
    assert got["GONE"] == []
    assert [b.date for b in got["AAPL"]] == [CUTOFF - timedelta(days=1), CUTOFF]
    assert all(isinstance(b, OHLCV) for b in got["AAPL"])
    # Same completed-only series the single-symbol read hands back.
    assert [b.date for b in provider.get_ohlcv("AAPL", 30)] == [b.date for b in got["AAPL"]]


def test_batch_without_a_bars_source_is_a_wiring_defect(frozen_calendar):
    with _yahoo_forbidden(), pytest.raises(NoBarsSourceError):
        MarketDataProvider().get_ohlcv_batch(["AAPL"], 30)


def test_batch_feed_failure_raises_like_before(frozen_calendar):
    """A failed request RAISES (callers tell 'feed down' from 'no history')."""

    def _boom(*_a, **_k):
        raise RuntimeError("429")

    broker = _FakeBroker({})
    broker.get_bars_batch = _boom
    provider = MarketDataProvider(bars_source=broker.get_bars)
    with _yahoo_forbidden(), pytest.raises(RuntimeError):
        provider.get_ohlcv_batch(["AAPL"], 30)


# ---------------------------------------------------------------------------
# The broker requests themselves: batch and single ask Alpaca for the SAME
# adjustment; the universe screen's own batch stays as it was.
# ---------------------------------------------------------------------------


def _real_broker():
    from src.execution.broker import AlpacaBroker

    with patch("src.execution.broker.TradingClient") as tc:
        tc.return_value = MagicMock()
        b = AlpacaBroker(api_key="test", secret_key="test", paper=True)
    raw_bar = SimpleNamespace(
        timestamp=datetime(2026, 10, 8, tzinfo=timezone.utc), open=9.0, high=11.0, low=8.0, close=10.0, volume=1
    )
    client = MagicMock()
    client.get_stock_bars.return_value = SimpleNamespace(data={"NVDA": [raw_bar]})
    client.get.return_value = {
        "bars": {"NVDA": [{"t": "2026-10-08T04:00:00Z", "o": 9, "h": 11, "l": 8, "c": 10, "v": 1}]},
        "next_page_token": None,
    }
    b._data_client = client
    return b, client


def test_batch_and_single_readers_request_the_same_adjustment(frozen_calendar):
    from alpaca.data.enums import Adjustment, DataFeed

    broker, client = _real_broker()
    provider = MarketDataProvider()
    provider.set_fallback_bars(broker.get_bars)
    with _yahoo_forbidden():
        single = provider.get_ohlcv("NVDA", 5)
        batch = provider.get_ohlcv_batch(["NVDA"], 5)
    assert [b.date for b in single] == [b.date for b in batch["NVDA"]] == [date(2026, 10, 8)]
    single_req = client.get_stock_bars.call_args.args[0]
    batch_fields = client.get.call_args.kwargs["data"]
    assert single_req.adjustment == Adjustment.ALL
    assert batch_fields["adjustment"] == single_req.adjustment
    assert batch_fields["feed"] == DataFeed.SIP


def test_universe_screen_batch_keeps_split_only_adjustment():
    from alpaca.data.enums import Adjustment

    broker, client = _real_broker()
    broker.get_bars_batch(["NVDA"], 5)  # the screen's call, no adjustment named
    assert client.get.call_args.kwargs["data"]["adjustment"] == Adjustment.SPLIT
