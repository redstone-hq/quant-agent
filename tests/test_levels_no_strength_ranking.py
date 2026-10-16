"""n06 (2026-10-16): the Technical analyst sees every reachable level, unranked.

Until this change `find_structural_levels` scored each level
``touches / (1 + distance% / 10)`` and kept only the top six per side. The 10
and the 6 were never measured. Now every level that clears the minimum-touch
rule inside the reach window is returned, and the prompt states each level's
touches and its distance in ATR(14) as separate facts.
"""

import re
from datetime import date, timedelta

import src.data.levels as levels_module
from src.data.levels import find_structural_levels, format_levels_block
from src.models import OHLCV

# Stock's own ATR handed to the scan: reach window = 3 x sqrt(60) x 1.5 ~ 34.9,
# cluster tolerance 3, so zones 5 apart stay separate levels.
ATR = 3.0


def _bars(prices: list[float], *, spread: float = 0.5) -> list[OHLCV]:
    start = date(2024, 1, 1)
    return [
        OHLCV(
            date=start + timedelta(days=i),
            open=c,
            high=c + spread,
            low=c - spread,
            close=c,
            volume=1_000_000,
        )
        for i, c in enumerate(prices)
    ]


def _oscillation(low: float, high: float, cycles: int, period: int = 12) -> list[float]:
    path: list[float] = []
    half = period // 2
    for _ in range(cycles):
        path += [low + (high - low) * i / half for i in range(half)]
        path += [high - (high - low) * i / half for i in range(half)]
    return path


def _ramp(a: float, b: float, n: int) -> list[float]:
    return [a + (b - a) * i / n for i in range(n)]


def _staircase() -> list[float]:
    """Seven support zones, the furthest touched five times, the nearest twice.

    A 14-bar flat start lets the stock's own ATR(14) exist before the first
    turning point. Each zone is entered from below on a ramp, oscillates, and
    the series ends well above the top zone so every zone is support.
    """
    path = [108.0] * 14
    zones = [109.0, 114.0, 119.0, 124.0, 129.0, 134.0, 139.0]
    for k, z in enumerate(zones):
        cycles = 5 if k == 0 else (2 if k == len(zones) - 1 else 3)
        path += _ramp(path[-1], z, 6)
        path += _oscillation(z, z + 1.5, cycles=cycles)
    path += _ramp(path[-1], 142.0, 6) + [142.0] * 6
    return path


def test_far_five_touch_and_near_two_touch_levels_both_appear():
    bars = _bars(_staircase())
    supports, _ = find_structural_levels(bars, atr=ATR)
    far = [lv for lv in supports if lv.price < 112.0]
    near = [lv for lv in supports if lv.price > 138.0]
    assert far and far[0].touches >= 5, [(lv.price, lv.touches) for lv in supports]
    assert near and near[0].touches >= 2, [(lv.price, lv.touches) for lv in supports]


def test_no_level_inside_the_reach_window_is_dropped():
    bars = _bars(_staircase())
    supports, resistances = find_structural_levels(bars, atr=ATR)
    # Two levels per zone (its lows and its highs), seven zones, all within reach.
    assert len(supports) >= 14, [(lv.price, lv.touches) for lv in supports]
    last = bars[-1].close
    window = levels_module.horizon_reach(ATR, levels_module.MAX_HORIZON_SESSIONS)
    assert all(abs(lv.price - last) <= window for lv in supports + resistances)
    # Price order, nearest first — not a ranking.
    assert [lv.price for lv in supports] == sorted((lv.price for lv in supports), reverse=True)


def test_each_shown_level_carries_its_distance_in_atr():
    bars = _bars(_staircase())
    supports, resistances = find_structural_levels(bars, atr=ATR)
    last = bars[-1].close
    text = format_levels_block(supports, resistances, last, atr=ATR)
    level_lines = [ln for ln in text.splitlines() if ln.strip().startswith("$")]
    assert len(level_lines) == len(supports) + len(resistances)
    for lv, ln in zip(sorted(supports, key=lambda x: -x.price), level_lines[-len(supports) :], strict=True):
        m = re.search(r"([0-9.]+) ATR away", ln)
        assert m, ln
        assert abs(float(m.group(1)) - abs(lv.price - last) / ATR) < 0.06, ln
        assert f"{lv.touches} touches" in ln


def test_strength_score_and_per_side_cut_are_gone():
    assert not hasattr(levels_module, "LEVEL_STRENGTH_DISTANCE_DIVISOR_PCT")
    assert not hasattr(levels_module, "MAX_LEVELS_PER_SIDE")
    assert "strength" not in levels_module.Level.__dataclass_fields__
