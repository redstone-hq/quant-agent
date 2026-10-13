"""Return windows anchor on real calendar dates, not fixed bar counts."""

from __future__ import annotations

from datetime import date, timedelta

from src.data.context import compute_market_context
from src.models import OHLCV


def _bar(d: date, close: float) -> OHLCV:
    return OHLCV(date=d, open=close, high=close + 0.5, low=close - 0.5, close=close, volume=1_000_000)


def _weekday_bars(start: date, end: date, close_fn, skip=()) -> list[OHLCV]:
    out, d = [], start
    while d <= end:
        if d.weekday() < 5 and d not in skip:
            out.append(_bar(d, close_fn(d)))
        d += timedelta(days=1)
    return out


def test_one_month_return_anchors_on_the_real_calendar_date() -> None:
    # Latest bar 2024-03-28; one month back is 2024-02-28. Close is 100 on that
    # date and rises 1 per calendar day after it, so the true 1m return is
    # fixed regardless of how many sessions (holidays) fall in between.
    anchor = date(2024, 2, 28)
    holidays = {date(2024, 3, 8), date(2024, 3, 11), date(2024, 3, 12), date(2024, 3, 13)}
    bars = _weekday_bars(
        date(2023, 1, 2),
        date(2024, 3, 28),
        lambda d: 100.0 + max(0, (d - anchor).days) if d >= anchor else 50.0,
        skip=holidays,
    )
    ctx = compute_market_context(bars)
    assert ctx is not None
    last = bars[-1].close  # 100 + 29
    assert ctx.return_1m == round((last - 100.0) / 100.0 * 100.0, 2)  # 29.0
    # The old 21-bar count lands on a different (holiday-shifted) bar.
    assert bars[-22].date != anchor


def test_anchor_falls_back_to_last_bar_on_or_before_the_date() -> None:
    # 2024-02-28 itself has no bar (holiday); the 1m anchor is the prior session.
    bars = _weekday_bars(
        date(2023, 6, 1),
        date(2024, 3, 28),
        lambda d: 200.0 if d <= date(2024, 2, 27) else 300.0,
        skip={date(2024, 2, 28)},
    )
    ctx = compute_market_context(bars)
    assert ctx is not None
    assert ctx.return_1m == 50.0


def test_series_shorter_than_window_has_no_return() -> None:
    bars = _weekday_bars(date(2024, 3, 1), date(2024, 3, 28), lambda d: 100.0 + d.day)
    ctx = compute_market_context(bars)
    assert ctx is not None
    assert ctx.return_1w is not None
    assert ctx.return_3m is None
    assert ctx.return_6m is None
    assert ctx.return_12m is None
