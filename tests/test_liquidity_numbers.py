"""Liquidity numbers: ADV is a calendar-month window; dead floors are gone."""

from datetime import date, timedelta

import pytest

from src.config import SmartMoneyConfig
from src.models import OHLCV
from src.quantities import avg_dollar_volume, dollar_volumes


def _bar(day, close=10.0, volume=1000.0):
    return OHLCV(date=day, open=close, high=close, low=close, close=close, volume=volume)


def test_adv_window_is_the_calendar_month_not_20_bars():
    # One calendar month back from the last bar (2026-02-02) is 2026-01-02.
    # Skip weekends and the 2026-01-19 holiday.
    days = []
    d = date(2025, 12, 1)
    while d <= date(2026, 2, 2):
        if d.weekday() < 5 and d != date(2026, 1, 19):
            days.append(d)
        d += timedelta(days=1)
    bars = [_bar(x) for x in days]
    expected = [b for b in bars if b.date >= date(2026, 1, 2)]
    assert len(expected) == 21  # a month holding a holiday is not 20 bars
    assert len(dollar_volumes(bars)) == len(expected)
    assert avg_dollar_volume(bars) == pytest.approx(10.0 * 1000.0)


def test_adv_keeps_halted_day_as_zero():
    bars = [_bar(date(2026, 3, 2) + timedelta(days=i), volume=0.0 if i == 3 else 1000.0) for i in range(10)]
    assert avg_dollar_volume(bars) == pytest.approx(9 * 10_000.0 / 10)


@pytest.mark.parametrize(
    "key",
    ["min_external_avg_dollar_volume_usd", "min_external_history_days", "min_external_price_usd"],
)
def test_deleted_external_floor_keys_are_gone(key):
    assert key not in SmartMoneyConfig.model_fields
    assert not hasattr(SmartMoneyConfig(), key)
