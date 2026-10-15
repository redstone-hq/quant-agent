"""One swing detector, no bar-count window (owner ruling, Rex 2026-10-15).

This file used to pin that `src/data/levels.py` (5 bars either side) and
`src/risk/trailing.py` (3 bars either side) deliberately disagreed. Both
windows are deleted: every swing point now comes from
`src.data.levels.turning_points`, a reversal of the stock's own ATR(14).
"""

from __future__ import annotations

import src.data.levels as levels_mod
import src.risk.alignment_exit as alignment_mod
import src.risk.trail_structure as structure_mod
import src.risk.trailing as trailing_mod
from src.data.technical import ATR_PERIOD


def test_no_pivot_window_constant_remains():
    for mod in (levels_mod, trailing_mod, structure_mod, alignment_mod):
        assert not hasattr(mod, "PIVOT_WINDOW"), mod.__name__


def test_the_trail_reads_the_level_scans_detector():
    assert structure_mod.turning_points is levels_mod.turning_points


def test_the_warm_up_is_read_from_the_atr_not_chosen():
    assert levels_mod.MIN_SCAN_BARS == ATR_PERIOD + 1
    assert trailing_mod.MIN_BARS_FOR_A_READING == levels_mod.MIN_SCAN_BARS
