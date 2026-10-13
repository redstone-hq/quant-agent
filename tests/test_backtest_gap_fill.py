"""Backtest exit fills on gaps: a day that opens through the stop fills at the
OPEN (not the stop); an intraday touch still fills at the stop. Targets that
gap favourably fill at the open. Hermetic: no network, no files."""

from __future__ import annotations

from datetime import date

from src.backtest.engine import _check_exit
from src.backtest.records import _OpenPosition
from src.models import OHLCV


def _pos(direction: str, stop: float, target: float | None) -> _OpenPosition:
    return _OpenPosition(
        symbol="GAP",
        direction=direction,
        signal_date=date(2024, 1, 2),
        entry_date=date(2024, 1, 3),
        entry_index=10,
        entry_price=100.0,
        stop_initial=stop,
        stop=stop,
        target=target,
        setup_type="breakout",
        shares=100,
        risk_pct=1.0,
    )


def _bar(o: float, h: float, low: float, c: float) -> OHLCV:
    return OHLCV(date=date(2024, 1, 4), open=o, high=h, low=low, close=c, volume=1000)


def test_long_gap_below_stop_fills_at_open():
    reason, px = _check_exit(_pos("long", 95.0, 110.0), _bar(90.25, 91.0, 89.0, 90.0), 11, 20)
    assert (reason, px) == ("stop", 90.25)  # open is 5% below the stop


def test_long_intraday_touch_fills_at_stop():
    reason, px = _check_exit(_pos("long", 95.0, 110.0), _bar(99.0, 100.0, 94.0, 96.0), 11, 20)
    assert (reason, px) == ("stop", 95.0)


def test_short_gap_above_stop_fills_at_open():
    reason, px = _check_exit(_pos("short", 105.0, 90.0), _bar(110.25, 111.0, 109.0, 110.0), 11, 20)
    assert (reason, px) == ("stop", 110.25)  # open is 5% above the stop


def test_short_intraday_touch_fills_at_stop():
    reason, px = _check_exit(_pos("short", 105.0, 90.0), _bar(101.0, 106.0, 100.0, 104.0), 11, 20)
    assert (reason, px) == ("stop", 105.0)


def test_long_target_gap_up_fills_at_open():
    reason, px = _check_exit(_pos("long", 95.0, 110.0), _bar(115.0, 116.0, 114.0, 115.0), 11, 20)
    assert (reason, px) == ("target", 115.0)


def test_long_target_intraday_touch_fills_at_target():
    reason, px = _check_exit(_pos("long", 95.0, 110.0), _bar(105.0, 111.0, 104.0, 109.0), 11, 20)
    assert (reason, px) == ("target", 110.0)


def test_short_target_gap_down_fills_at_open():
    reason, px = _check_exit(_pos("short", 105.0, 90.0), _bar(85.0, 86.0, 84.0, 85.0), 11, 20)
    assert (reason, px) == ("target", 85.0)


def test_short_target_intraday_touch_fills_at_target():
    reason, px = _check_exit(_pos("short", 105.0, 90.0), _bar(95.0, 96.0, 89.0, 91.0), 11, 20)
    assert (reason, px) == ("target", 90.0)
