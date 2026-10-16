"""n06: the AI seat sees exact facts, not made-up buckets, counts or caps."""

from datetime import date, timedelta
from types import SimpleNamespace

from src.data import context, insider_signal
from src.data.context import compute_market_context
from src.models.analysis import OHLCV
from src.models.smart_money import SmartMoneyObservation


def _bar(d: date, level: float, half: float = 0.5) -> OHLCV:
    return OHLCV(date=d, open=level, high=level + half, low=level - half, close=level, volume=1_000_000)


def _daily(start: date, n: int, skip_weekday: int | None = None) -> list[date]:
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5 and d.weekday() != skip_weekday:
            out.append(d)
        d += timedelta(days=1)
    return out


def test_constants_are_gone():
    assert not hasattr(context, "_W_12M")
    assert not hasattr(context, "_MAX_GAPS_REPORTED")
    assert not hasattr(insider_signal, "_BAND_LOW")
    assert not hasattr(insider_signal, "_BAND_HIGH")
    assert "holdings_fraction_band" not in SmartMoneyObservation.model_fields


def test_seat_sees_exact_ratio_and_no_bucket():
    sell = SimpleNamespace(stream="insider", direction="sell", shares=12_345.0, post_transaction_shares=87_655.0)
    fraction, no_prior = insider_signal.holdings_fraction(sell)
    assert fraction == 12_345.0 / 100_000.0
    assert no_prior is False
    buy = SimpleNamespace(stream="insider", direction="buy", shares=500.0, post_transaction_shares=500.0)
    assert insider_signal.holdings_fraction(buy) == (None, True)
    assert not any(name.startswith("BAND_") for name in dir(insider_signal))


def test_52_week_range_is_the_last_calendar_year_across_holiday_gaps():
    # One weekday missing every week: far fewer than 252 bars per year, so a
    # 252-bar window would reach back more than a year.
    dates = _daily(date(2024, 1, 1), 320, skip_weekday=2)
    levels = [100.0] * len(dates)
    levels[len(dates) - 240] = 150.0  # older than one year before the last bar
    levels[len(dates) - 50] = 120.0  # inside the year
    levels[len(dates) - 40] = 80.0  # inside the year
    bars = [_bar(d, lv) for d, lv in zip(dates, levels)]
    cutoff = dates[-1].replace(year=dates[-1].year - 1)
    inside = [b for b in bars if b.date >= cutoff]
    assert 0 < len(bars) - len(inside) and len(inside) < 252
    ctx = compute_market_context(bars)
    assert ctx.high_52w == max(b.high for b in inside) == 120.5
    assert ctx.low_52w == min(b.low for b in inside) == 79.5


def test_every_unfilled_gap_is_reported():
    dates = _daily(date(2025, 1, 1), 200)
    levels, level = [], 100.0
    for i in range(len(dates)):
        if i in (40, 70, 100, 130, 160):
            level += 10.0  # permanent jump up: the gap is never refilled
        levels.append(level)
    bars = [_bar(d, lv) for d, lv in zip(dates, levels)]
    ctx = compute_market_context(bars)
    assert len(ctx.unfilled_gaps) == 5
