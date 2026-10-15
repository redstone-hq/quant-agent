"""Structural level detection — deterministic support/resistance from bars.

These tests pin the behaviour the Tech Analyst now depends on. Levels feed the
stop, the target, and therefore thesis_progress and pace; a silent regression
here would put stops at prices nobody derived from the chart, which is the
failure this module was written to end.
"""

from datetime import date, timedelta

import pytest

from src.data.levels import (
    Level,
    find_structural_levels,
    format_levels_block,
)
from src.models import OHLCV


def _bars(prices: list[float], *, spread: float = 0.5) -> list[OHLCV]:
    """Build a bar series from a close path, with a small symmetric range."""
    start = date(2024, 1, 1)
    out = []
    for i, close in enumerate(prices):
        out.append(
            OHLCV(
                date=start + timedelta(days=i),
                open=close,
                high=close + spread,
                low=close - spread,
                close=close,
                volume=1_000_000,
            )
        )
    return out


def _oscillation(low: float, high: float, cycles: int, period: int = 12) -> list[float]:
    """A price path that repeatedly turns at `low` and `high`."""
    path: list[float] = []
    half = period // 2
    for _ in range(cycles):
        path += [low + (high - low) * i / half for i in range(half)]
        path += [high - (high - low) * i / half for i in range(half)]
    return path


class TestLevelDetection:
    def test_repeated_turning_points_become_levels(self):
        bars = _bars(_oscillation(100.0, 120.0, cycles=6))
        supports, resistances = find_structural_levels(bars)
        found = [lv.price for lv in supports + resistances]
        assert any(98 <= p <= 102 for p in found), f"missing ~100 level in {found}"
        assert any(118 <= p <= 122 for p in found), f"missing ~120 level in {found}"

    def test_levels_are_classified_by_position_relative_to_last_close(self):
        # Ends near the top of the range, so ~100 must read as support.
        bars = _bars(_oscillation(100.0, 120.0, cycles=6) + [119.0, 119.5])
        supports, resistances = find_structural_levels(bars)
        last = bars[-1].close
        assert all(lv.price < last for lv in supports)
        assert all(lv.price > last for lv in resistances)

    def test_a_level_touched_once_is_not_structure(self):
        # A single spike should not be reported as a level.
        path = [100.0] * 40 + [140.0] + [100.0] * 40
        _, resistances = find_structural_levels(_bars(path), atr=25.0)
        assert not any(135 <= lv.price <= 145 for lv in resistances)

    def test_insufficient_history_returns_nothing(self):
        supports, resistances = find_structural_levels(_bars([100.0] * 4))
        assert supports == [] and resistances == []

    def test_empty_input_is_safe(self):
        assert find_structural_levels([]) == ([], [])


class TestBadData:
    def test_single_bad_print_does_not_create_a_level(self):
        """A 10x spike in one bar is a vendor error, not resistance."""
        path = _oscillation(100.0, 110.0, cycles=6)
        bars = _bars(path)
        # Corrupt one bar the way a bad feed does.
        i = len(bars) // 2
        bars[i] = OHLCV(
            date=bars[i].date,
            open=bars[i].open,
            high=1000.0,
            low=bars[i].low,
            close=bars[i].close,
            volume=bars[i].volume,
        )
        supports, resistances = find_structural_levels(bars, atr=1_000.0)
        assert not any(lv.price > 500 for lv in supports + resistances)

    def test_impossible_bars_are_discarded(self):
        bars = _bars(_oscillation(100.0, 110.0, cycles=6))
        bars[10] = OHLCV(date=bars[10].date, open=0.0, high=0.0, low=0.0, close=0.0, volume=0)
        # Must not raise, and must still find the real structure.
        supports, resistances = find_structural_levels(bars)
        assert supports or resistances

    def test_a_stock_that_multiplied_keeps_its_recent_history(self):
        """Regression: a global-median outlier filter deleted the real range.

        OKLO ran from ~$10 to ~$114. Comparing every bar to the five-year
        median rejected its entire recent range as 'outliers' and reported no
        levels at all. Outlier detection must be local to each bar's
        neighbourhood, so a trend survives and only print errors are removed.
        """
        ramp = [10.0 + i * 0.5 for i in range(200)]  # 10 -> 110
        settle = _oscillation(104.0, 110.0, cycles=6)  # consolidates high
        supports, resistances = find_structural_levels(_bars(ramp + settle))
        assert supports or resistances, "trending stock produced no levels"
        assert all(lv.price > 50 for lv in supports + resistances), (
            "levels from the pre-ramp era should be out of range, not the recent ones"
        )


class TestRelevanceFiltering:
    def test_distant_history_is_excluded(self):
        """A long-dead price zone is trivia, not actionable structure."""
        old = _oscillation(10.0, 11.0, cycles=8)  # ancient $10 era
        ramp = [10.0 + i * 0.4 for i in range(100)]
        now = _oscillation(48.0, 52.0, cycles=8)  # current range
        supports, resistances = find_structural_levels(_bars(old + ramp + now))
        assert not any(lv.price < 20 for lv in supports + resistances)

    def test_touch_count_outranks_recency(self):
        """Strength rewards touch count now, not recency — reversed 2026-09-02.

        This test asserted the opposite until 2026-09-02: a recently-touched
        level beat a long-abandoned one on fewer touches, because `strength`
        decayed each touch by a 252-session half-life nobody had measured.
        `src/data/level_quality.py` then fit decay directly against our own
        bars and found no age effect at all (docs/RESEARCH_FINDINGS.md §7),
        so the assumption this test pinned was wrong, not just outdated. It
        now asserts what the measurement supports: more touches outranks
        fewer, however old they are.

        Ancient and recent price zones are kept well over 1% apart so
        `_cluster`'s tolerance cannot chain them into one confounded level —
        the original version of this test put them close enough together
        that the "recent" cluster absorbed several ancient pivots too, which
        is why it kept passing under both the old formula and the new one
        and never actually caught anything.
        """
        ancient = _oscillation(70.0, 72.0, cycles=15)  # 15 touches, old
        buffer = [72.0 + i * (18.0 / 120) for i in range(120)]  # monotonic: no pivots
        recent = (
            _oscillation(94.0, 96.0, cycles=3)  # 3 touches, fresh
            + [96.5, 97.0, 97.5, 98.0, 98.5, 99.0]  # clears edge-of-series
        )
        supports, _ = find_structural_levels(_bars(ancient + buffer + recent), atr=25.0)
        assert supports, "expected support levels"
        newest = min(supports, key=lambda lv: lv.last_touch_sessions_ago)
        strongest = max(supports, key=lambda lv: lv.strength)
        assert strongest.touches > newest.touches, (
            "the strongest level here must be the one with more touches, not the one most recently touched"
        )

    def test_strength_does_not_depend_on_when_the_touches_happened(self):
        """Same touches, same distance, different age -> identical strength.

        The direct regression test for the recency term's removal. Two
        series share one touch pattern (4 touches at each of two prices) and
        the same final distance-from-price, differing only in how long ago
        the pattern happened (~200 sessions vs ~7-12 sessions). If any age
        term — even a weak one — crept back in, these would diverge; under
        the formula this pins, they must match exactly.
        """
        osc = _oscillation(88.0, 90.0, cycles=4)
        old_tail = [90.0 + i * (20.0 / 199) for i in range(200)]
        recent_tail = [90.0 + i * (20.0 / 6) for i in range(7)]

        old_supports, _ = find_structural_levels(_bars(osc + old_tail), atr=25.0)
        recent_supports, _ = find_structural_levels(_bars(osc + recent_tail), atr=25.0)
        assert old_supports and recent_supports
        assert [lv.strength for lv in old_supports] == [lv.strength for lv in recent_supports]
        # The match above is only meaningful if touches agreed (so strength
        # had no OTHER reason to match) and recency genuinely differed (so
        # there was something for a lingering age term to react to).
        assert [lv.touches for lv in old_supports] == [lv.touches for lv in recent_supports]
        assert [lv.last_touch_sessions_ago for lv in old_supports] != [
            lv.last_touch_sessions_ago for lv in recent_supports
        ]

    def test_closer_levels_still_outrank_equally_touched_distant_ones(self):
        """Distance still discounts strength — untouched by the 2026-09-02 change.

        Nothing about the distance term was measured or changed; this pins it
        directly so a future edit to the touch-count formula cannot silently
        take the distance discount out with it. Two zones share the same
        touch count (4 each) so only distance from the last close can be
        doing the separating.
        """
        # 14 flat bars first: turning points need the stock's own ATR(14).
        far = [58.0] * 14 + _oscillation(58.0, 60.0, cycles=4)
        ramp = [60.0 + i * (33.0 / 120) for i in range(120)]
        near = _oscillation(93.0, 95.0, cycles=4) + [95.5, 96.0, 96.5, 97.0, 97.5, 98.0]
        supports, _ = find_structural_levels(_bars(far + ramp + near), atr=25.0)
        near_levels = [lv for lv in supports if lv.price > 90.0]
        far_levels = [lv for lv in supports if lv.price < 65.0]
        assert near_levels and far_levels
        assert all(lv.touches == 4 for lv in near_levels + far_levels), (
            "this only isolates distance if the touch counts already match"
        )
        assert min(lv.strength for lv in near_levels) > max(lv.strength for lv in far_levels)

    def test_levels_are_returned_strongest_first(self):
        bars = _bars(_oscillation(100.0, 120.0, cycles=8))
        supports, resistances = find_structural_levels(bars)
        for side in (supports, resistances):
            strengths = [lv.strength for lv in side]
            assert strengths == sorted(strengths, reverse=True)

    def test_result_count_is_bounded(self):
        bars = _bars(_oscillation(100.0, 120.0, cycles=20))
        supports, resistances = find_structural_levels(bars, max_per_side=3)
        assert len(supports) <= 3 and len(resistances) <= 3


class TestPromptBlock:
    def test_block_marks_absent_structure_explicitly(self):
        text = format_levels_block([], [], 42.0)
        assert "NONE IDENTIFIED" in text
        # The analyst must be told not to fabricate what wasn't found.
        assert "invent" in text.lower()

    def test_block_orders_like_a_chart_axis(self):
        supports = [Level(90.0, "support", 3, 5, 2.0)]
        resistances = [Level(110.0, "resistance", 2, 9, 1.0)]
        text = format_levels_block(supports, resistances, 100.0)
        assert text.index("110") < text.index("last close") < text.index("90")

    def test_block_reports_distance_and_recency(self):
        text = format_levels_block([Level(95.0, "support", 4, 7, 3.0)], [], 100.0)
        assert "-5.0%" in text
        assert "4 touches" in text
        assert "7d ago" in text


@pytest.mark.parametrize("cycles", [3, 6, 12])
def test_detection_is_deterministic(cycles):
    """Same bars in, same levels out — every time.

    This is the property that makes the desk back-testable and that an LLM
    reading raw bars could never guarantee.
    """
    bars = _bars(_oscillation(100.0, 115.0, cycles=cycles))
    first = find_structural_levels(bars)
    for _ in range(3):
        assert find_structural_levels(bars) == first


def test_cluster_tolerance_fallback_is_unreachable_on_cleaned_bars():
    """Route (2) of `CLUSTER_TOLERANCE_PCT_FALLBACK` is dead on real bars.

    `_cluster` consults the percentage fallback only for a pivot whose bar
    range is not finite. `_clean_bars` removes every bar that could produce
    one, so the branch cannot be reached from `find_structural_levels`.
    Measured 2026-10-04 and recorded against the ledger row of the same name.
    """
    import datetime as _dt
    import math as _math

    from src.data import levels as _levels
    from src.models.analysis import OHLCV as _OHLCV

    def _bar(i, high, low):
        return _OHLCV(
            date=_dt.date(2025, 1, 1) + _dt.timedelta(days=i),
            open=100.0,
            high=high,
            low=low,
            close=100.0,
            volume=1000,
        )

    corrupt = {
        "nan_high": (float("nan"), 99.0),
        "inf_high": (float("inf"), 99.0),
        "inverted": (90.0, 110.0),
        "zero_low": (101.0, 0.0),
        "neg_inf_low": (101.0, float("-inf")),
    }
    for name, (high, low) in corrupt.items():
        # An oscillating tape: turning points need a move of one ATR (2026-10-15).
        bars = [_bar(i, c + 1.0, c - 1.0) for i, c in enumerate(_oscillation(90.0, 110.0, cycles=22))]
        bars[130] = _bar(130, high, low)
        pivots = _levels._find_pivots(_levels._clean_bars(bars))
        assert pivots, name
        unmeasurable = [p for p in pivots if not (_math.isfinite(p[3]) and _math.isfinite(p[4]) and p[3] <= p[4])]
        assert unmeasurable == [], name
