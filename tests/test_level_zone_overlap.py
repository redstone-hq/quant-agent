"""Item 55: a level is a band the BARS drew, not a percentage of price.

The old rule chained a pivot into a level when its PRICE sat within a flat
1% of the group's anchor. These pin the replacement: two pivots are the same
level when the price ranges their bars actually traded overlap, the level's
zone is those bars' own combined span, and a level with no recorded zone
falls back to the old percentage rather than losing its zone entirely.
"""

import math

import pytest

from src.data.levels import (
    _cluster,
    cluster_span,
    level_zone_halfwidth,
)


def pivot(index, price, kind, low, high):
    return (index, price, kind, low, high)


def test_overlapping_bars_are_one_level_even_far_apart_in_price():
    """Two turning points 6% apart in price, but their bars overlap."""
    a = pivot(0, 100.0, "S", 99.0, 106.5)
    b = pivot(10, 106.0, "R", 105.0, 108.0)
    groups = _cluster([a, b])
    assert len(groups) == 1, "overlapping traded ranges are one level"
    assert cluster_span(groups[0]) == (99.0, 108.0)


def test_non_overlapping_bars_are_separate_levels_even_when_close():
    """Two turning points 0.4% apart — inside the old 1% — but no overlap."""
    a = pivot(0, 100.0, "S", 99.8, 100.1)
    b = pivot(10, 100.4, "R", 100.3, 100.6)
    groups = _cluster([a, b])
    assert len(groups) == 2, "bars that never traded the same prices are two levels"


def _pairwise_overlap(a, b):
    return a[3] <= b[4] and b[3] <= a[4]


def _assert_complete_linkage(groups):
    """THE property: every pair inside a level shares traded prices."""
    for g in groups:
        measurable = [p for p in g if math.isfinite(p[3]) and math.isfinite(p[4])]
        for i, a in enumerate(measurable):
            for b in measurable[i + 1 :]:
                assert _pairwise_overlap(a, b), f"{a} and {b} are in one level but never traded the same price"


def test_one_tall_bar_cannot_weld_two_shelves_together():
    """The single-linkage failure, pinned. A bar spanning 95-104 overlaps BOTH
    a 100-101 shelf and a 102-103 shelf, which do not overlap each other.
    Complete linkage must not put all three in one level."""
    a = pivot(0, 100.0, "S", 100.0, 101.0)
    b = pivot(10, 102.0, "R", 102.0, 103.0)
    c = pivot(20, 104.0, "R", 95.0, 104.0)
    groups = _cluster([a, b, c])
    assert len(groups) == 2, "the tall bar joins one shelf, it does not merge them"
    _assert_complete_linkage(groups)


def test_complete_linkage_holds_whatever_order_the_pivots_arrive_in():
    """Pinned as a property over permutations, not as one expected partition:
    the previous test passed only because of the sweep's sort order."""
    import itertools

    pivots = [
        pivot(0, 100.0, "S", 100.0, 101.0),
        pivot(10, 102.0, "R", 102.0, 103.0),
        pivot(20, 104.0, "R", 95.0, 104.0),
        pivot(30, 110.0, "R", 109.5, 110.5),
        pivot(40, 110.2, "R", 110.0, 111.0),
    ]
    for perm in itertools.permutations(pivots):
        groups = _cluster(list(perm))
        _assert_complete_linkage(groups)
        assert sum(len(g) for g in groups) == len(pivots), "no pivot is lost"
        # Deterministic: input order must not change the partition.
        assert sorted(sorted(p[0] for p in g) for g in groups) == sorted(
            sorted(p[0] for p in g) for g in _cluster(list(pivots))
        )


def test_zone_halfwidth_is_read_off_the_span_not_a_percentage():
    hw = level_zone_halfwidth(100.0, zone_low=96.0, zone_high=103.0)
    assert hw == pytest.approx(4.0), "the furthest edge of the level's own band"


def test_no_zone_uses_one_atr_or_nothing():
    """No measured zone: one 14-day ATR if known, else no zone (fail closed)."""
    assert level_zone_halfwidth(200.0, atr=3.5) == pytest.approx(3.5)
    assert level_zone_halfwidth(200.0) == 0.0
    assert level_zone_halfwidth(200.0, zone_low=None, zone_high=None) == 0.0
    # Unusable bounds are the same case, not a narrower zone.
    assert level_zone_halfwidth(200.0, zone_low=float("nan"), zone_high=210.0) == 0.0
    assert level_zone_halfwidth(200.0, zone_low=210.0, zone_high=190.0) == 0.0
    # A zero-range bar cannot bound a match either.
    assert level_zone_halfwidth(200.0, zone_low=200.0, zone_high=200.0) == 0.0


def test_unevaluable_range_joins_within_one_atr_else_is_dropped():
    """A pivot whose bar range is not a number joins within one 14-day ATR
    of every member; with no ATR it is dropped, never placed by a percentage."""
    nan = float("nan")
    a = pivot(0, 100.0, "S", 99.9, 100.1)
    b = pivot(9, 100.5, "R", nan, nan)  # within one ATR (2.0) of 100.0
    c = pivot(18, 140.0, "R", nan, nan)  # far outside it
    groups = _cluster([a, b, c], 2.0)
    assert len(groups) == 2
    assert b in groups[0]
    assert c not in groups[0]
    assert _cluster([a, b, c]) == [[a]]


def test_cluster_span_is_none_when_nothing_is_measurable():
    nan = float("nan")
    assert cluster_span([pivot(0, 100.0, "S", nan, nan)]) == (None, None)


def test_levels_built_from_bars_carry_their_measured_zone():
    from src.data.levels import find_structural_levels
    from src.models import OHLCV
    from datetime import date, timedelta

    start = date(2025, 1, 2)
    bars = []
    for i in range(160):
        base = 100.0 + (3.0 if i % 20 < 10 else -3.0)
        bars.append(
            OHLCV(
                date=start + timedelta(days=i),
                open=base,
                high=base + 1.5,
                low=base - 1.5,
                close=base,
                volume=1_000_000,
            )
        )
    sup, res = find_structural_levels(bars)
    found = sup + res
    assert found, "the fixture must produce levels or it pins nothing"
    for lv in found:
        assert lv.zone_low is not None and lv.zone_high is not None
        assert lv.zone_low <= lv.price <= lv.zone_high
        assert lv.zone_halfwidth > 0
