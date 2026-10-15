"""The "is this stop AT that level" tolerance is the level zone's OWN width.

docs/WORK.md item 46, fixed 2026-09-13. These tests exist to stop one
specific class of regression: a tolerance expressed in one unit drifting
away from the thing it claims to cover, expressed in another.

WHAT WENT WRONG. `risk.level_match_atr_tolerance` was `0.25`, i.e. a stop
counted as sitting AT a computed level if it was within 0.25 ATR of it, and
its config comment justified 0.25 by saying a computed level is a ZONE
(`find_structural_levels` once clustered pivots within a fixed percentage =
1% of price into one level) and that the tolerance therefore had to be "at
least that wide". Those are different units, so the justification was never
a statement about the setting — it was a statement about the instrument:

    0.25 * ATR >= 0.01 * price   <=>   ATR >= 4.0% of price

The claim only held on names whose ATR is at least 4% of price. Every
quieter name got a tolerance NARROWER than the zone, so a stop sitting
inside a level's real zone was not recognised as level-backed, was widened
to the ATR band instead, and the trade's reward:risk was judged against a
stop that had moved off the structure it was placed on (item 1's geometry).

THE FIX, AND WHY IT IS NOT ANOTHER NUMBER. No ATR multiple can be correct
here, because the ratio between an ATR multiple and a percentage of price
is a different number for every name on every day. So the constant is gone
rather than re-tuned: the match tolerance is read from
`src.data.levels.level_zone_halfwidth`, which derives it from the very
fixed percentage that built the zone (deleted 2026-10-15: zones are measured). One number, one unit, one
file. `test_no_atr_multiple_survives_anywhere` is the mechanical guard.

The ATR question was not deleted, it was put back where it belongs: whether
a stop is far enough out to survive the name's noise is still
`min_stop_atr_multiple` / `absolute_min_stop_atr_multiple`, and whether a
level has BROKEN is `BREAK_CONFIRMATION_ATR_MULTIPLE`. Both are genuinely
volatility questions. "Which level is this stop on" is an identity question
about a zone, and is answered in the zone's unit.
"""

from datetime import date, timedelta
from pathlib import Path

import pytest

from src.config import RiskConfig
from src.data.levels import (
    cluster_span,
    MIN_TOUCHES,
    _cluster,
    find_structural_levels,
    level_zone_halfwidth,
)
from src.models import OHLCV
from src.portfolio_constructor import ConstructorConfig, PortfolioConstructor
from src.risk.exit_guard import _structural_level_backing_stop

REPO = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# 1. The relationship itself: the tolerance covers the zone, at EVERY price
#    and for EVERY volatility, because volatility is no longer an input.
# ---------------------------------------------------------------------------


def test_tolerance_covers_every_pivot_the_clusterer_would_have_merged():
    """The bound is provable, not asserted — check it against `_cluster`.

    RESTATED 2026-09-30 (item 55): `_cluster` no longer uses a percentage at
    all. Pivots are one level when their BARS' traded ranges overlap — and
    under COMPLETE linkage, when every pair of them overlaps — and the zone
    is those bars' own combined span. The property under test is unchanged
    and is the one that matters: the reported tolerance still reaches every
    pivot the clusterer merged. It is now checked against the measured span.

    The previous fixture here (bars 99.0-100.5, 100.2-101.2, 100.9-102.0) is
    kept below as the NEGATIVE case: its first and last bars never traded a
    common price, so single linkage welded them and complete linkage must
    not. Its replacement is a genuinely mutually-overlapping run.
    """
    chain = [
        (0, 100.0, "S", 99.0, 100.5),
        (1, 100.4, "S", 100.2, 101.2),
        (2, 101.0, "S", 100.9, 102.0),
    ]
    assert len(_cluster(chain)) == 2, (
        "99.0-100.5 and 100.9-102.0 share no traded price; chaining them "
        "through the middle bar is the single-linkage defect"
    )

    pivots = [
        (0, 100.0, "S", 99.0, 101.5),
        (1, 100.4, "S", 100.2, 101.2),
        (2, 101.0, "S", 100.9, 102.0),
    ]

    clusters = _cluster(pivots)
    assert len(clusters) == 1, "fixture must be ONE zone for the test to mean anything"

    level_price = sum(p[1] for p in clusters[0]) / len(clusters[0])
    zone_low, zone_high = cluster_span(clusters[0])
    tolerance = level_zone_halfwidth(level_price, zone_low=zone_low, zone_high=zone_high)
    for pivot in clusters[0]:
        assert abs(pivot[1] - level_price) <= tolerance


def test_zero_and_nonsense_prices_yield_no_tolerance():
    assert level_zone_halfwidth(0.0) == 0.0
    assert level_zone_halfwidth(-10.0) == 0.0
    assert level_zone_halfwidth(float("nan")) == 0.0
    assert level_zone_halfwidth(float("inf")) == 0.0


# ---------------------------------------------------------------------------
# 2. The defect, reproduced against the arithmetic that caused it.
# ---------------------------------------------------------------------------

OLD_ATR_MULTIPLE = 0.25  # the deleted `risk.level_match_atr_tolerance`
QUOTED_MEDIAN_ATR_PCT = 2.56  # docs/QAMC_REMEDIATION_SPEC.md, measured 2026-08-27


def test_new_tolerance_is_independent_of_volatility():
    """The fix is right for all four of those names, not three of them.

    Nothing in the matcher takes an ATR any more, so there is no volatility
    at which the tolerance can fall short of the zone.
    """
    # Item 215: the stop is on a BAR that drew the level, not merely inside
    # the level's zone. That bar traded 99.4-100.2; the stop sits in it.
    level_price, stop = 100.0, 99.5
    bars = {level_price: [(99.4, 100.2)]}
    for atr in (0.5, 1.5, 2.56, 4.0, 9.0):  # 0.5%..9% of price
        matched = _structural_level_backing_stop(
            entry_price=105.0,
            stop_loss=stop,
            is_short=False,
            computed_levels=[level_price],
            computed_level_bars=bars,
        )
        assert matched == level_price, f"unmatched at atr={atr}"


# ---------------------------------------------------------------------------
# 3. The two implementations agree. `src/risk/exit_guard.py` restates the
#    rule because it is deliberately stdlib-only; a copy is only safe if
#    something checks it.
# ---------------------------------------------------------------------------


def _constructor():
    return PortfolioConstructor(ConstructorConfig())


class _Analysis:
    def __init__(self, levels, touches, bars=None):
        self.computed_levels = levels
        self.computed_level_touches = touches
        # Item 215: (low, high) of the bars that DREW each level. Absent
        # means unknown, which fails closed to "not level-backed".
        self.computed_level_bars = bars or {}
        self.atr_14 = 2.0


# gap -> the answer, written out BY HAND from the bar range stated in the
# test's own docstring. Deliberately NOT recomputed from `bar_low`/`bar_high`
# at assert time: the previous version did exactly that and the comparison
# became true by construction, so the test could no longer fail if the
# matcher's rule changed underneath it.
_MATCH_TRUTH_TABLE = {
    0.0: 100.0,  # stop 100.00 — inside the bar
    0.5: 100.0,  # stop  99.50 — inside the bar
    0.99: 100.0,  # stop  99.01 — inside the bar, a cent above its low
    1.0: 100.0,  # stop  99.00 — exactly ON the bar's low, inclusive
    1.01: None,  # stop  98.99 — a cent BELOW the bar's low
    1.5: None,  # stop  98.50 — outside
    5.0: None,  # stop  95.00 — far outside
}


@pytest.mark.parametrize("gap", sorted(_MATCH_TRUTH_TABLE))
def test_constructor_and_exit_guard_match_identically(gap):
    """Straddles the FORMING BAR's low — inside, on it, and outside.

    Item 215: the boundary both implementations must agree on is no longer
    the zone's edge but the low of a bar that drew the level. The bar here
    traded 99.0-100.5, so gaps up to 1.0 rest on it and larger ones do not.

    The zone here is 1.50 wide against a stop distance of at least 9.50, so
    the item-55 precision bound admits every case and this test is measuring
    only the membership boundary, which is what it is for.
    """
    level_price = 100.0
    stop = level_price - gap
    entry = 110.0
    touches = {level_price: 5}
    bar_low, bar_high = 99.0, 100.5
    bars = {level_price: [(bar_low, bar_high)]}

    from_constructor = _constructor()._level_backing_stop(
        _Analysis([level_price], touches, bars),
        entry,
        stop,
        False,
    )
    from_exit_guard = _structural_level_backing_stop(
        entry_price=entry,
        stop_loss=stop,
        is_short=False,
        computed_levels=[level_price],
        computed_level_bars=bars,
    )
    assert from_constructor == from_exit_guard
    assert from_constructor == _MATCH_TRUTH_TABLE[gap]


def test_boundary_scales_with_price_in_both_implementations():
    """NO single fraction of price can reproduce the matcher's answers.

    This is the property the file was written to defend and it is stated as
    something that can FAIL. The previous version hand-supplied a bar and
    asserted that a point inside that bar matched, which is true by
    construction whatever the rule is; its docstring still claimed to be
    strengthening the price-scaling property. Restored here as a search: if
    ANY constant percentage-of-price tolerance could reproduce every answer
    below, the matcher is secretly a fixed fraction of price again and this
    test fails and names the fraction.

    The three bars below are real-shaped, not constant-shaped: 0.7%, 1.9%
    and 0.4% of their own price. Both implementations are checked, because
    `src/risk/exit_guard.py` keeps a hand-copy of the rule.
    """
    cases = {
        10.0: (9.93, 10.02),  # 0.7% below the level
        100.0: (98.10, 100.40),  # 1.9% below
        1000.0: (996.00, 1002.0),  # 0.4% below
    }
    observed: list[tuple[float, float, bool]] = []
    for level_price, (bar_low, bar_high) in cases.items():
        touches = {level_price: 5}
        bars = {level_price: [(bar_low, bar_high)]}
        entry = level_price * 1.2
        c = _constructor()
        for stop, inside in (
            (bar_low + (bar_high - bar_low) * 0.01, True),
            (bar_low - (bar_high - bar_low) * 0.01, False),
        ):
            from_ctor = c._level_backing_stop(
                _Analysis([level_price], touches, bars),
                entry,
                stop,
                False,
            )
            from_guard = _structural_level_backing_stop(
                entry_price=entry,
                stop_loss=stop,
                is_short=False,
                computed_levels=[level_price],
                computed_level_bars=bars,
            )
            assert from_ctor == from_guard, (level_price, stop)
            assert (from_ctor == level_price) is inside, (level_price, stop)
            observed.append((abs(stop - level_price) / level_price * 100.0, level_price, inside))

    # The search. A constant-fraction rule would accept exactly the cases
    # whose gap is at or under some single percentage; sweep every candidate
    # boundary the observations themselves offer and require all of them to
    # misclassify something.
    candidates = sorted({round(g, 10) for g, _, _ in observed})
    reproducing = [pct for pct in candidates if all((gap <= pct + 1e-12) is inside for gap, _, inside in observed)]
    assert not reproducing, (
        "the matcher is reproducible by a constant %-of-price tolerance "
        f"{reproducing} — the price-scaling property is gone"
    )


def test_stop_to_level_distance_is_bounded_by_the_trade_own_risk():
    """NOTHING previously pinned a maximum stop-to-level distance.

    Found by the adversary pass on PR 880: bar membership alone has no
    outward ceiling, because the zone's edges ARE bar extremes, so the
    furthest passing stop sits a full zone halfwidth from the level — median
    3.33% of price and up to 36.07% on the desk's own 704-level set, against
    a hard 1.00% before. The break check then evaluates the LEVEL, so the
    desk could report structure intact with the stop a fifth of the price
    away. Item 55's bound: the level's measured zone must be strictly
    narrower than the trade's own risk, which makes
    ``abs(stop - level) < abs(entry - stop)`` a guarantee rather than a hope.
    """
    level_price, entry = 100.0, 110.0
    touches = {level_price: 5}
    c = _constructor()

    def match(bar, stop):
        bars = {level_price: [bar]}
        from_ctor = c._level_backing_stop(
            _Analysis([level_price], touches, bars),
            entry,
            stop,
            False,
        )
        from_guard = _structural_level_backing_stop(
            entry_price=entry,
            stop_loss=stop,
            is_short=False,
            computed_levels=[level_price],
            computed_level_bars=bars,
        )
        assert from_ctor == from_guard, (bar, stop)
        return from_ctor

    # A 25-wide turning point. The stop rests inside it, so the item-215
    # membership test alone would call this level-backed with the level 15.0
    # away on a 25.0 risk. Refused.
    assert match((80.0, 105.0), 85.0) is None
    # Same bar, same membership, but the risk is now wider than the zone.
    assert match((80.0, 105.0), 74.0) is None  # outside the bar: still no
    # A precise turning point backing the same stop: admitted.
    assert match((84.5, 86.0), 85.0) == level_price

    # The guarantee itself, swept over widths either side of the boundary.
    for half in (0.5, 2.0, 5.0, 11.9, 12.4, 12.5, 12.6, 20.0):
        stop = entry - 25.0  # 85.0, risk 25.0
        bar = (stop - half, stop + half)
        got = match(bar, stop)
        if got is not None:
            assert abs(stop - got) < abs(entry - stop), half
            assert 2 * half < abs(entry - stop), half
        else:
            assert 2 * half >= abs(entry - stop), half


# ---------------------------------------------------------------------------
# 4. Mechanical guards — the rules in section 1 only hold if nobody quietly
#    reintroduces the deleted setting or a private copy of the constant.
# ---------------------------------------------------------------------------


def test_deleted_setting_is_refused_loudly_not_ignored():
    """`extra="ignore"` would let a stale settings.yaml load in silence."""
    with pytest.raises(ValueError, match="level_match_atr_tolerance"):
        RiskConfig(level_match_atr_tolerance=0.25)


def test_no_atr_multiple_survives_anywhere():
    """No code, config or test may carry the deleted key as anything live.

    Prose mentions are fine and expected (the config tombstone, the incident
    write-up, this file's own docstring) — an assignment or a keyword
    argument is not.
    """
    offenders = []
    roots = [REPO / "src", REPO / "tests", REPO / "scripts", REPO / "config"]
    for root in roots:
        for path in root.rglob("*"):
            if path.suffix not in {".py", ".yaml", ".yml"} or not path.is_file():
                continue
            for lineno, line in enumerate(path.read_text(errors="replace").splitlines(), 1):
                stripped = line.strip()
                if stripped.startswith("#") or "level_match_atr_tolerance" not in line:
                    continue
                if "level_match_atr_tolerance=" in line or (
                    "level_match_atr_tolerance:" in line and not stripped.startswith("#")
                ):
                    # The deletion guard in `RiskConfig` and this test's own
                    # assertion both legitimately name the key.
                    if path.name == "test_level_match_zone.py" or path.relative_to(REPO).parts[:2] == ("src", "config"):
                        continue
                    offenders.append(f"{path.relative_to(REPO)}:{lineno}")
    assert not offenders, (
        "docs/WORK.md item 46 deleted `level_match_atr_tolerance`; a live "
        "reference reappeared at: " + ", ".join(offenders)
    )


def test_min_touches_is_two_and_that_one_is_sourced():
    """The ONE constant in the level definition that has a published answer.

    docs/WORK.md item 55. Two points are the fewest that can define a
    horizontal line, and the published construction of this exact object
    agrees — Tsinaslanidis (PhD thesis, Univ. of Macedonia, 2012, §4.4):
    "Only price areas (bins) with frequencies greater or equal to two are
    considered as HSAR."

    Raising it is ruled out by that work's own measurement, not by taste
    (§4.6.1): more touches did not improve bounce frequency (NASDAQ,
    two-local levels 60.99% over 26,868 hits; three-local 61.04% over
    6,661). So a future session must not "tighten" this to 3 — that would
    discard levels for no measured gain. Lowering it to 1 is excluded by
    geometry: one pivot is a point, not a level.
    """
    assert MIN_TOUCHES == 2


# ---------------------------------------------------------------------------
# 5. End to end against a real level the scanner actually computed, so the
#    relationship is pinned against `find_structural_levels`' own output and
#    not only against a hand-written price.
# ---------------------------------------------------------------------------


def _bars_with_a_double_bottom() -> list[OHLCV]:
    """Enough clean bars for the scan, with two clear pivot lows near 95."""
    bars: list[OHLCV] = []
    closes = (
        [105] * 14  # flat warm-up: swings are an ATR(14) reversal (2026-10-15)
        + [105, 104, 103, 102, 101, 100, 99, 98, 97, 96]
        + [95.0]  # pivot low 1
        + [96, 97, 98, 99, 100, 101, 102, 103, 104, 105]
        + [95.4]  # pivot low 2 — inside 1% of 95.0
        + [96, 97, 98, 99, 100, 101, 102, 103, 104, 105, 106, 107]
    )
    start = date(2026, 1, 5)
    for i, close in enumerate(closes):
        bars.append(
            OHLCV(
                date=start + timedelta(days=i),
                open=close,
                high=close + 0.4,
                low=close - 0.4,
                close=close,
                volume=1_000_000,
            )
        )
    return bars


def test_a_stop_on_a_bar_that_drew_a_real_computed_level_is_recognised():
    """End to end against real bars: `find_structural_levels` records the
    forming bars, and only a stop inside one of them is backed."""
    supports, _ = find_structural_levels(_bars_with_a_double_bottom())
    assert supports, "fixture must produce a support level"
    level = supports[0]
    assert level.touches >= 2
    assert level.pivot_bars, "the level must carry the bars that drew it"

    entry = level.price * 1.1
    touches = {level.price: 5}
    bars = {level.price: [tuple(b) for b in level.pivot_bars]}
    c = _constructor()

    lowest = min(b[0] for b in level.pivot_bars)
    highest = max(b[1] for b in level.pivot_bars)
    on_a_bar = (lowest + highest) / 2.0
    # Still inside the level's own reported zone, but below every bar that
    # made it — the case item 215 exists to refuse.
    off_every_bar = lowest - (highest - lowest)

    assert (
        c._level_backing_stop(
            _Analysis([level.price], touches, bars),
            entry,
            on_a_bar,
            False,
        )
        == level.price
    )
    assert (
        c._level_backing_stop(
            _Analysis([level.price], touches, bars),
            entry,
            off_every_bar,
            False,
        )
        is None
    )


def test_a_stop_inside_the_zone_but_on_no_forming_bar_is_not_backed():
    """THE new rule, item 215, stated on its own.

    A level's zone is the merged span of the bars that formed it (item 55),
    so on a name whose turning points are far apart that span can be a large
    fraction of the price with a wide gap in the middle that nothing ever
    traded. A stop parked in that gap can be taken out with the level never
    broken, so it is not level-backed and gets no exemption. Both
    implementations must say so.
    """
    level_price = 100.0
    touches = {level_price: 5}
    # Two sessions drew this level, ten dollars apart. The zone spans both;
    # the bars do not.
    bars = {level_price: [(94.0, 95.0), (104.0, 105.0)]}
    entry = 120.0
    in_the_gap = 99.0  # inside the zone, on neither bar
    on_the_lower_bar = 94.5  # on a bar that actually traded

    c = _constructor()
    assert (
        c._level_backing_stop(
            _Analysis([level_price], touches, bars),
            entry,
            in_the_gap,
            False,
        )
        is None
    )
    assert (
        c._level_backing_stop(
            _Analysis([level_price], touches, bars),
            entry,
            on_the_lower_bar,
            False,
        )
        == level_price
    )

    for stop, expected in ((in_the_gap, None), (on_the_lower_bar, level_price)):
        assert (
            _structural_level_backing_stop(
                entry_price=entry,
                stop_loss=stop,
                is_short=False,
                computed_levels=[level_price],
                computed_level_bars=bars,
            )
            == expected
        )


def test_no_bar_ranges_recorded_fails_closed_to_not_backed():
    """Item 215 fails closed. An older stored analysis carries no forming
    bars, and an unknown is never an exemption."""
    level_price = 100.0
    assert (
        _constructor()._level_backing_stop(
            _Analysis([level_price], {level_price: 5}),
            110.0,
            99.9,
            False,
        )
        is None
    )
