"""Tripwire: measured reach of three level-finder numbers on the committed bars.

Records what the LIVE level finder does over every 60-bar window (stride 7)
of the committed daily-bar fixture. If a count moves, the matching
`narrowed:` note in config/number_ledger.yaml must be revisited.
"""

import collections
import gzip
import json
from pathlib import Path

from src.data.levels import find_structural_levels as find
from src.data.technical import compute_indicators
from src.models.analysis import OHLCV

_FIX = Path(__file__).resolve().parents[1] / ("ops/model_policy/fixtures/yf_daily_bars_2026-08-28.json.gz")


def _run():
    raw = json.load(gzip.open(_FIX, "rt"))
    touches = collections.Counter()
    changed = collections.Counter()
    windows = 0
    variants = {
        "reach5": dict(max_reach_atr_multiple=5.0),
        "reach0.5": dict(max_reach_atr_multiple=0.5),
        "hor250": dict(max_horizon_sessions=250),
        "hor20": dict(max_horizon_sessions=20),
    }

    def key(r):
        return [round(lv.price, 4) for lv in r[0] + r[1]]

    for sym, rows in raw.items():
        bars = [OHLCV(**r) for r in rows]
        for end in range(60, len(bars), 7):
            win = bars[end - 60 : end]
            windows += 1
            atr = compute_indicators(sym, win).atr_14
            ref = win[-1].close
            base = find(win, atr=atr, reference_price=ref)
            for lv in base[0] + base[1]:
                touches[lv.touches] += 1
            kb = key(base)
            for name, kw in variants.items():
                if key(find(win, atr=atr, reference_price=ref, **kw)) != kb:
                    changed[name] += 1
    return windows, touches, changed


def test_level_finder_reach_measurements():
    windows, touches, changed = _run()
    assert windows == 1176
    # Re-measured 2026-10-15: swing points are a reversal of the stock's own
    # ATR(14), not a 5-bar window, so the finder sees more turns per window.
    # 2026-10-16 (n06): the six-per-side cut is gone, so nine more 2-touch
    # levels are reported (1429 -> 1438) and hor20 moves 204 -> 205.
    assert sorted(touches.items()) == [
        (2, 1438),
        (3, 1135),
        (4, 829),
        (5, 516),
        (6, 331),
        (7, 164),
        (8, 86),
        (9, 39),
        (10, 16),
        (11, 10),
        (12, 2),
    ]
    assert changed["reach5"] == 13 and changed["hor250"] == 13
    assert changed["hor20"] == 205 and changed["reach0.5"] == 686
