"""The up/down volume read starts at the same calendar date one month back.

Hermetic series for ticker ZZZ; no network, no fixtures.
"""

from datetime import date

import pandas as pd

from src.data.context import compute_market_context
from src.models.analysis import OHLCV


def _bars(end: date, holidays: set[date]) -> list[OHLCV]:
    days = [d.date() for d in pd.bdate_range(end=end, periods=320) if d.date() not in holidays]
    out = []
    for i, d in enumerate(days):
        up = i % 3 != 0
        vol = 1_000_000 + 10_000 * (i % 7)
        out.append(OHLCV(date=d, open=100.0, high=102.0, low=98.0, close=101.0 if up else 99.0, volume=vol))
    return out


def _expected_ratio(bars: list[OHLCV], start: date) -> float:
    df = pd.DataFrame(
        {"d": [b.date for b in bars], "o": [b.open for b in bars], "c": [b.close for b in bars], "v": [b.volume for b in bars]}
    )
    w = df[df.d >= start]
    return round(w.v[w.c > w.o].sum() / w.v[w.c < w.o].sum(), 2)


def _old_rule_ratio(bars: list[OHLCV]) -> float:
    w = bars[-21:]
    up = sum(b.volume for b in w if b.close > b.open)
    dn = sum(b.volume for b in w if b.close < b.open)
    return round(up / dn, 2)


def test_window_starts_on_same_calendar_date_one_month_back():
    # Last bar 2025-03-31; one month earlier is Fri 2025-02-28, itself a bar.
    bars = _bars(date(2025, 3, 31), set())
    start = (pd.Timestamp(bars[-1].date) - pd.DateOffset(months=1)).date()
    assert start == date(2025, 2, 28)
    got = compute_market_context(bars).up_down_volume_ratio
    assert got == _expected_ratio(bars, start)
    assert got != _old_rule_ratio(bars)  # 22 bars in the month, not 21


def test_window_starts_at_last_bar_before_a_holiday_boundary():
    # Last bar Mon 2025-05-19; one month earlier is Sat 04-19, after Good
    # Friday 04-18 (closed): the window must start at Thu 04-17.
    bars = _bars(date(2025, 5, 19), {date(2025, 4, 18)})
    anchor = date(2025, 4, 17)
    assert any(b.date == anchor for b in bars)
    got = compute_market_context(bars).up_down_volume_ratio
    assert got == _expected_ratio(bars, anchor)
    assert got != _old_rule_ratio(bars)
