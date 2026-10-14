"""Weekly market scan with funds, no per-run cap (owner board item, 2026-10-14).

The weekly pass (`AdmissionService._run_universe_screen`) reads a fund's
category from Yahoo when none is kept, so a plain fund is decided (admitted)
and a leveraged or inverse fund is refused; an operating company is never
refused for a leverage word in its name; and every admitted name reaches the
session, not three of them.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from src import universe_screen as us
from src.admission_build import build_admission_service
from src.config import UniverseScreenConfig
from src.data.fund_category import FundCategoryStore
from src.data.nasdaq_listing import Listing, ListingRecord
from src.models import OHLCV

#: symbol -> (name, is_fund, Nasdaq sector, Yahoo category)
NAMES = {
    "SPY": ("SPDR S&P 500 ETF Trust", True, None, "Large Blend"),
    "QLD": ("ProShares Ultra QQQ", True, None, "Trading--Leveraged Equity"),
    "SPUU": ("Direxion Daily S&P 500 Bull 2X Shares", True, None, "Trading--Leveraged Equity"),
    "SH": ("ProShares Short S&P500", True, None, "Trading--Inverse Equity"),
    "UCTT": ("Ultra Clean Holdings, Inc. Common Stock", False, "Industrials", None),
}


def _bars(n=300, price=50.0):
    days, day = [], date(2026, 9, 18)
    while len(days) < n:
        if day.weekday() < 5:
            days.append(day)
        day -= timedelta(days=1)
    days.reverse()
    return [
        OHLCV(date=d, open=price, high=price * 1.0005, low=price * 0.9995, close=price, volume=1_000_000) for d in days
    ]


def _asset(symbol):
    name, is_fund, _, _ = NAMES[symbol]
    return {
        "symbol": symbol,
        "name": name,
        "status": "active",
        "tradable": True,
        "class": "us_equity",
        "exchange": "ARCA" if is_fund else "NASDAQ",
    }


@pytest.fixture(autouse=True)
def _listing_and_yahoo(monkeypatch):
    listing = Listing(
        {
            s: ListingRecord(s, name, None if fund else Decimal("5e9"), fund, sector)
            for s, (name, fund, sector, _) in NAMES.items()
        }
    )
    monkeypatch.setattr("src.data.nasdaq_listing.load_listing", lambda *, fetch: listing)
    monkeypatch.setattr("src.universe_daily._yahoo_reader", lambda: lambda s: {"category": NAMES[s][3]})


def _service(tmp_path, symbols):
    broker, market, provider = MagicMock(), MagicMock(), MagicMock()
    broker.list_assets.return_value = [_asset(s) for s in symbols]
    broker.get_asset_record.side_effect = _asset
    broker.get_positions.return_value = []
    market.get_ohlcv_batch.side_effect = lambda chunk, days: {s: _bars() for s in chunk}
    provider.listed_map.return_value = {str(i): {s: "NYSE"} for i, s in enumerate(symbols, start=1)}
    provider._get.return_value.json.return_value = {}  # no filings at all
    config = SimpleNamespace(
        trading=SimpleNamespace(universe=["AAPL"], lookback_days=120),
        universe_screen=UniverseScreenConfig(enabled=True, data_dir=str(tmp_path)),
        storage=SimpleNamespace(db_path=str(tmp_path / "desk.db")),
        execution=SimpleNamespace(max_entry_slippage_bps=40.0),
        risk=SimpleNamespace(min_stop_atr_multiple=2.5, max_target_horizon_sessions=60),
        nominations=SimpleNamespace(max_per_seat_per_run=3),
    )
    return build_admission_service(
        config=config,
        broker=broker,
        market=market,
        db=MagicMock(),
        sec_form4_provider=provider,
        constructor_cfg_fn=lambda: None,
    )


def _weekly_pass(tmp_path, symbols):
    _service(tmp_path, symbols)._run_universe_screen("evening-x")
    return us.UniverseStore(tmp_path).load()


def test_weekly_scan_admits_a_plain_fund(tmp_path):
    state = _weekly_pass(tmp_path, ["SPY"])
    assert "SPY" in state["admitted"], state["screened"]
    assert state["admitted"]["SPY"]["measured"]["sector"] == "Large Blend"
    assert FundCategoryStore(tmp_path / "desk.db").get("SPY").category == "Large Blend"  # read once, kept


def test_weekly_scan_refuses_leveraged_and_inverse_funds(tmp_path):
    state = _weekly_pass(tmp_path, ["QLD", "SPUU", "SH"])
    assert state["admitted"] == {}
    for symbol in ("QLD", "SPUU", "SH"):
        assert state["screened"].get(symbol, {}).get("failures") == ["leveraged_or_inverse_fund"], symbol


def test_weekly_scan_does_not_refuse_a_company_for_ultra_in_its_name(tmp_path):
    state = _weekly_pass(tmp_path, ["UCTT"])
    assert "UCTT" in state["admitted"], state["screened"]
