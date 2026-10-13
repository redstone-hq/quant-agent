"""S&P float-adjusted liquidity ratio is the universe's FIRST filter.

Owner ruling 2026-10-13: a stock passes when its traded value over the 365
calendar days before evaluation (average close x total volume, S&P's
definition) is at least 0.75 x its market cap. Every drop carries a reason.
"""

from __future__ import annotations

import inspect
from datetime import date, timedelta

import pytest

from src import universe_daily as ud
from src import universe_screen as us
from src.models import OHLCV

TODAY = date(2026, 10, 9)

TH = us.ScreenThresholds(
    min_price_usd=5.0,
    min_market_cap_usd=30_000_000,
    max_half_spread_bps=40.0,
    max_atr_fraction=1.0 / 2.5,
    min_history_bars=210,
)

ASSET = {
    "symbol": "ACME",
    "name": "Acme Corp. Common Stock",
    "status": "active",
    "tradable": True,
    "class": "us_equity",
    "exchange": "NYSE",
}


def _bars(calendar_days=400, *, price=50.0, volume=1_000_000):
    """One calm bar per weekday over `calendar_days`, ending TODAY."""
    out = []
    day = TODAY - timedelta(days=calendar_days)
    while day <= TODAY:
        if day.weekday() < 5:
            out.append(OHLCV(date=day, open=price, high=price * 1.0005, low=price * 0.9995, close=price, volume=volume))
        day += timedelta(days=1)
    return out


def _traded_365(bars):
    window = [b for b in bars if b.date > TODAY - timedelta(days=365)]
    return sum(b.close for b in window) / len(window) * sum(b.volume for b in window)


def _sources(bars, profile):
    return us.ScreenSources(
        get_asset=lambda s: ASSET,
        get_bars=lambda s: bars,
        get_profile=lambda s: profile,
        get_filings=lambda s: [],
    )


def _profile(cap):
    return {"market_cap_usd": cap, "sector": "Industrials", "quote_type": "EQUITY"}


def test_constants_are_the_sp_published_values():
    assert us.SP_MIN_LIQUIDITY_RATIO == 0.75
    assert us.SP_LIQUIDITY_LOOKBACK_DAYS == 365


def test_passes_at_exactly_the_ratio_and_drops_just_below_with_reason():
    bars = _bars()
    traded = _traded_365(bars)
    at = us.screen_symbol("ACME", _sources(bars, _profile(traded / 0.75)), TH)
    assert at.passed, at.failures
    assert at.measured["liquidity_ratio"] == pytest.approx(0.75, abs=1e-4)

    below = us.screen_symbol("ACME", _sources(bars, _profile(traded / 0.74)), TH)
    assert below.failures == ["liquidity_below_sp_ratio"]
    assert not below.inconclusive
    assert below.measured["liquidity_ratio"] == pytest.approx(0.74, abs=1e-4)
    assert "liquidity_below_sp_ratio" in us.PLAIN_REASON


def test_it_is_the_first_filter_ahead_of_the_bar_checks():
    # A penny stock that also trades too little records the liquidity reason first.
    bars = _bars(price=1.0)
    result = us.screen_symbol("ACME", _sources(bars, _profile(_traded_365(bars) * 10)), TH)
    assert result.failures == ["liquidity_below_sp_ratio"]


@pytest.mark.parametrize("cap", [None, 0, -5.0, float("nan")])
def test_missing_market_value_is_dropped_with_reason(cap):
    result = us.screen_symbol("ACME", _sources(_bars(), _profile(cap)), TH)
    assert result.failures == ["market_cap_unknown"]
    # Recorded by the liquidity check (first filter), not the later size check.
    assert "liquidity_cap_basis" in result.measured
    assert "last_price" not in result.measured
    assert not result.inconclusive  # dropped, visibly, not silently passed
    assert "market_cap_unknown" in us.PLAIN_REASON


def test_short_history_is_dropped_with_reason():
    bars = _bars(calendar_days=300)
    result = us.screen_symbol("ACME", _sources(bars, _profile(1.0)), TH)
    assert result.failures == ["liquidity_history_short"]
    assert not result.inconclusive
    assert "liquidity_history_short" in us.PLAIN_REASON


def test_daily_record_drops_below_ratio_with_reason(tmp_path):
    bars = _bars()
    traded = _traded_365(bars)
    profiles = {"LOW": _profile(traded / 0.5), "HIGH": _profile(traded / 2.0)}
    assets = [{**ASSET, "symbol": s} for s in profiles]
    run = ud.run_daily_record(
        assets,
        get_bars_batch=lambda chunk: {s: bars for s in chunk},
        get_profile=lambda s: profiles[s],
        get_filings=lambda s: [],
        th=TH,
        today=TODAY,
        deadline=float("inf"),
        batch_size=50,
        cache=ud.ReadCache(tmp_path),
    )
    assert run.records["LOW"]["status"] == "dropped"
    assert run.records["LOW"]["failures"] == ["liquidity_below_sp_ratio"]
    assert run.records["HIGH"]["status"] == "passed", run.records["HIGH"]


def test_no_dollar_volume_floor_remains_in_the_screen():
    for module in (us, ud):
        text = inspect.getsource(module).lower()
        assert "min_avg_dollar_volume" not in text
        assert "dollar_volume_below_minimum" not in text
    fields = set(us.ScreenThresholds.__dataclass_fields__)
    assert not any("dollar" in f or "volume" in f for f in fields)
    # The only liquidity verdict is the S&P ratio.
    assert "liquidity_below_sp_ratio" in inspect.getsource(us.check_liquidity_ratio)
