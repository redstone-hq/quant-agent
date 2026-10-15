"""Turning points come from the stock's own movement (owner ruling, Rex 2026-10-15).

A swing low is confirmed only once price has bounced from it by at least the
stock's own ATR(14); a swing high once price has fallen from it by that much.
No bar count. The last, unconfirmed candidate is never used. The level scan
and the trailing stop read the ONE detector, so they name the same swing low.

Synthetic bars only (ticker ZZZ): every bar trades a 2.00 range, so the ATR
is ~2.00 and "one ATR" is a known distance.
"""

from __future__ import annotations

import datetime as _dt

from src.data import levels as _levels
from src.data.technical import ATR_PERIOD
from src.models import OHLCV
from src.risk.trail_structure import _swing_lows, _swing_lows_dated

SYMBOL = "ZZZ"
_DESCENT = [120.0 - i for i in range(21)]  # lows 120 -> 100, index 20 is 100


def _bars(lows: list[float], highs: list[float] | None = None) -> list[OHLCV]:
    start = _dt.date(2026, 1, 5)
    highs = highs or [lo + 2.0 for lo in lows]
    return [
        OHLCV(
            date=start + _dt.timedelta(days=i),
            open=(lo + hi) / 2,
            high=hi,
            low=lo,
            close=(lo + hi) / 2,
            volume=1_000,
        )
        for i, (lo, hi) in enumerate(zip(lows, highs))
    ]


def test_a_dip_smaller_than_one_atr_is_not_a_turning_point():
    # From the 100 low the stock bounces only 1.2 (high 101.2, under 100 + ~2
    # ATR), then keeps falling to 95 and bounces properly from there.
    tail = [99.0, 98.0, 97.0, 96.0, 95.0, 97.0, 99.0]
    lows = _DESCENT + [100.2] + tail
    highs = [lo + 2.0 for lo in _DESCENT] + [101.2] + [lo + 2.0 for lo in tail]
    found = _swing_lows(_bars(lows, highs))
    assert 100.0 not in found
    assert found == [95.0]


def test_a_dip_followed_by_a_bounce_of_one_atr_is_a_turning_point():
    lows = _DESCENT + [101.0, 102.0, 103.0]
    assert _swing_lows(_bars(lows)) == [100.0]


def test_the_unconfirmed_last_candidate_is_never_returned():
    # Price is still falling: 100 is only today's extreme, not a swing low.
    bars = _bars(_DESCENT)
    assert _swing_lows(bars) == []
    assert _swing_lows_dated(bars) == []
    assert all(price != 100.0 for _, price, _ in _levels.turning_points(bars))
    assert all(p[1] != 100.0 for p in _levels._find_pivots(bars))


def test_nothing_is_confirmed_before_the_atr_has_warmed_up():
    bars = _bars([100.0, 98.0, 103.0, 97.0, 104.0, 96.0, 105.0])
    assert len(bars) < ATR_PERIOD
    assert _levels.turning_points(bars) == []


def test_level_stops_and_the_trailing_stop_name_the_same_swing_low():
    # 100, then a higher low at 101 with only three rising bars either side:
    # the old 5-bar level window and 3-bar trailing window disagreed here.
    lows = _DESCENT + [102.0, 104.0, 106.0, 103.0, 101.0, 103.0, 105.0, 107.0, 109.0, 111.0, 113.0]
    bars = _bars(lows)
    level_supports = [p[1] for p in _levels._find_pivots(bars) if p[2] == "S"]
    assert _swing_lows(bars) == [100.0, 101.0]
    assert level_supports == _swing_lows(bars)
