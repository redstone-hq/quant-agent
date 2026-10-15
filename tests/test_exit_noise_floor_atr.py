"""Exit-metric noise band is the name's own 14-day ATR, not a made-up constant.

Owner ruling 2026-10-15: a move smaller than one ATR is noise. Thesis
progress (and pace, which is built from it) is measured against the AI's own
target, so it is shown but never scored.
"""

from __future__ import annotations

import src.risk.exit_guard_deltas as deltas_mod
from src.risk.exit_guard_deltas import compute_deltas, veto_contradicted_exit


def _pair(dist_before, dist_after, price_before, price_after, *, atr_pct, r=None, progress=None):
    prior = {"distance_to_stop_pct": dist_before, "current_price": price_before, "stop_loss": 90.0, "qty": 10}
    current = {"distance_to_stop_pct": dist_after, "current_price": price_after, "stop_loss": 90.0, "qty": 10}
    if atr_pct is not None:
        current["atr_pct"] = atr_pct
    if r is not None:
        prior["r_multiple"], current["r_multiple"] = r
    if progress is not None:
        prior["thesis_progress_pct"], current["thesis_progress_pct"] = progress
        prior["pace"], current["pace"] = progress
    return compute_deltas("abc", prior, current)


def test_no_noise_floor_constant_remains():
    assert not hasattr(deltas_mod, "_NOISE_FLOOR")


def test_distance_move_under_one_atr_is_flat():
    # Price 100 -> 101 with stop fixed at 90: distance 10.0% -> 10.89%, ATR 2% of price.
    d = _pair(10.0, 10.89, 100.0, 101.0, atr_pct=2.0)
    assert d.improved == [] and d.worsened == []
    assert "(flat, within one ATR)" in d.render()


def test_distance_move_over_one_atr_counts():
    # Price 100 -> 104: distance 10.0% -> 13.46%, ATR 2% of price.
    d = _pair(10.0, 13.46, 100.0, 104.0, atr_pct=2.0)
    assert d.improved == ["distance_to_stop_pct"]
    up = _pair(13.46, 10.0, 104.0, 100.0, atr_pct=2.0)
    assert up.worsened == ["distance_to_stop_pct"]


def test_r_multiple_floor_is_one_atr_in_r():
    # ATR = 2% of 101 = $2.02; price moved $1 -> under one ATR -> flat.
    small = _pair(10.0, 10.89, 100.0, 101.0, atr_pct=2.0, r=(0.5, 0.6))
    assert "r_multiple" not in small.improved
    # Price moved $4 -> over one ATR -> counts.
    big = _pair(10.0, 13.46, 100.0, 104.0, atr_pct=2.0, r=(0.5, 0.9))
    assert "r_multiple" in big.improved


def test_no_atr_means_unscored_never_a_fallback():
    d = _pair(10.0, 30.0, 100.0, 130.0, atr_pct=None, r=(0.0, 3.0))
    assert d.improved == [] and d.worsened == []
    assert "(unscored, no ATR)" in d.render()


def test_thesis_progress_and_pace_never_scored():
    d = _pair(10.0, 10.0, 100.0, 100.0, atr_pct=2.0, progress=(10.0, 90.0))
    assert "thesis_progress_pct" not in d.improved and "pace" not in d.improved
    d2 = _pair(10.0, 10.0, 100.0, 100.0, atr_pct=2.0, progress=(90.0, -50.0))
    assert "thesis_progress_pct" not in d2.worsened and "pace" not in d2.worsened
    assert "thesis_progress_pct 10.00 → 90.00 (unscored" in d.render()
    # A huge thesis-progress jump alone can no longer veto an exit.
    assert veto_contradicted_exit("SELL", "position has stalled", d) is None
