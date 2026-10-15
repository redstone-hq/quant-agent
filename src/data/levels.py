"""Deterministic support/resistance detection from completed daily bars.

Why this exists
---------------
Until 2026-08-27 the Tech Analyst was shown the last **20** daily bars and asked
to report "the two or three key levels". Twenty bars is one month: a
consolidation from six weeks ago, a swing high from last spring, a level price
has respected four times since — all invisible. The only things the analyst
could honestly cite were the moving averages and the 20-day high/low, so the
structural levels the whole exit system depends on were effectively absent, and
`PortfolioConstructor` invented stops and targets to fill the gap.

The history was never the problem: the pipeline already fetched hundreds of
bars to compute MA200, then showed the analyst twenty of them.

Finding where price repeatedly stopped is arithmetic, not judgment, so it
belongs here rather than in a language model. Computing five years of levels
costs single-digit milliseconds and zero tokens, and — unlike asking a model to
eyeball a wall of numbers — the same chart yields the same levels every time,
which is what makes the behaviour testable and back-testable.

The model still does the part it is good at: deciding which of these levels
matters right now, and what kind of setup the chart is showing.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, asdict

import numpy as np

from src.models import OHLCV
from src.data.technical import ATR_PERIOD, atr_series
from src.risk.constants import is_trend_trade

# A turning point is found from the stock's OWN movement, never a bar count
# (owner ruling, Rex 2026-10-15: "a turning point must come from the stock's
# own movement, not a made-up bar count"). The two bar-count windows that used
# to stand here and in `src/risk/trailing.py` (5 and 3 bars either side, both
# ledgered `arbitrary`) are deleted. The rule is the standard zigzag reversal,
# as VectorBT PRO's `pivot_info_1d_nb` implements it, with the reversal
# threshold set to the stock's own Wilder ATR(14) (`atr_series`, ledgered as
# Wilder 1978) — no multiplier, no new constant. See `turning_points` below,
# which is the ONE detector the level scan, the trailing stop and the trend
# exit all read.

# The fewest CLEAN bars that can yield one confirmed turning point — READ from
# the ATR warm-up, never chosen here: the first reversal threshold exists on
# bar ``ATR_PERIOD`` (`atr_series` returns nothing below that), and a candidate
# there is confirmed at the earliest by the next bar. Both
# `find_structural_levels` and `structure_coverage` read it from here so they
# can never disagree about whether the scan could have run.
MIN_SCAN_BARS = ATR_PERIOD + 1

# Two pivots within this percentage of each other are the same level. Price
# does not respect a number to the cent — it respects a zone.
#
# **Still a convention with no derivation. It is now a PLACED one.**
# docs/WORK.md item 55, 2026-09-13.
#
# That a level is a band and not a price is sourced, not assumed. Bulkowski,
# quoted in Tsinaslanidis (PhD thesis, University of Macedonia, 2012, §4.2):
# "Support and resistance are not individual price points, but rather thick
# bands of molasses that slow or even stop price movement." The same section
# derives the requirement from Murphy and Bulkowski together: "it can be
# inferred that a support or a resistant level is an area of prices, rather
# than a specific individual price level, in where local peaks and bottoms
# reside." So the SHAPE — a percentage-width band around a cluster of pivots
# — is the published archetype and is adopted here deliberately.
#
# The WIDTH is not derived by that literature. Its own construction (§4.4)
# leaves it as a user input: "The third variable 'x' is the desired
# percentage distance of each bin." What the literature does supply is a
# measured insensitivity range, and this desk sits at the edge of it. Their
# illustrated default is 3%, and footnote 32 records that "desired distances
# of 2%, 4% and 5% are also implemented", with the body stating "Any further
# parameterization does not affect the empirical findings" — measured over
# 733 NASDAQ/NYSE names, 1990-2010. `_cluster` below chains a pivot in when
# it sits within this percentage of the cluster's ANCHOR, so a cluster spans
# at most 1.0% — HALF the narrowest bin that literature has tested. The
# match zone built from it (`level_zone_halfwidth`, +/-1%) spans 2.0%, which
# is exactly that narrowest tested bin.
#
# So: 1.0 is not shown to be right, and it is not shown to be wrong either.
# It stays, labelled, rather than being moved to 3.0 — moving it would be
# adopting a foreign default, which is the same unsourced act in the other
# direction. What would settle it is named in docs/WORK.md item 55.
# SUPERSEDED 2026-09-30 (docs/WORK.md item 55). The clustering rule above is
# no longer a percentage at all: two pivots are the same level when the actual
# PRICE RANGES of the bars that made them OVERLAP, and the level's zone is
# those bars' own combined span (`_cluster`, `Level.zone_low/zone_high`). That
# is read off the instrument, so there is no width to pick and nothing left to
# sweep. The everything above about 1.0 vs 2%-5% bins is history, kept because
# it records why a number was there.
#
# This constant survives for ONE purpose: FAIL-CLOSED fallback. A caller that
# holds only a bare level PRICE with no zone attached (an older stored
# analysis, a fixture, any path that predates the zone field) still needs some
# bound, and silently dropping the level would remove a stop's structural
# backing. Such a caller gets exactly today's behaviour. It must never be used
# when a real zone is available.
CLUSTER_TOLERANCE_PCT_FALLBACK = 1.0
CLUSTER_TOLERANCE_PCT = CLUSTER_TOLERANCE_PCT_FALLBACK


def stop_rests_on_level(
    stop_price: float,
    pivot_bars: "tuple | list | None",
    *,
    stop_distance: float | None = None,
) -> bool:
    """Is `stop_price` AT this level, rather than merely inside its band?

    docs/WORK.md item 215. `level_zone_halfwidth` bounds the level's WHOLE
    zone, so "inside the zone" and "at the level" were reported as the same
    statement. They are not: a merged cluster's zone can span a fifth of the
    price, and a stop at one end of it can be taken out with the level itself
    never broken.

    The test here introduces NO new number. A level is drawn by the bars that
    turned at it; the smallest thing the instrument itself says is "structure
    traded here" is one of those bars' own high-low range. So the stop rests
    on the level when it lies inside the range of at least one bar that formed
    the level — a price the market actually defended — and not when it merely
    lies somewhere in the merged span between two distant pivots. The bound is
    read off the same bars `find_structural_levels` clustered, so it can never
    drift from the object it is matching.

    **THE OUTWARD BOUND (adversary pass, 2026-09-30).** Bar membership alone
    is NOT enough, and on its own it is a one-way loosening. The furthest a
    stop can sit from `Level.price` and still pass the membership test is the
    zone halfwidth exactly, because the zone edges ARE bar extremes and an
    extreme always lies inside some bar. Measured over the 704 levels this
    desk's own 400-bar set produces under the clustering in `_cluster`: median
    halfwidth 3.33% of price, p90 9.41%, max 36.07%; restricted to levels with
    at least 5 touches, median 4.31% and 38% of them above 5%. Against a hard
    1.00% on the prior rule. With the break check evaluating the matched LEVEL
    price rather than the stop, that permits "structure intact" reported with
    the stop a fifth of the price away. So membership needs a ceiling.

    `stop_distance` is that ceiling and it introduces NO number. It is
    ``abs(entry_price - stop_loss)`` — the trade's own risk, already decided
    before this question is asked. The rule: **the level must be more precise
    than the thing it is backing.** The level's measured zone (``min(low)`` to
    ``max(high)`` over `pivot_bars`, the same span `cluster_span` reports) must
    be STRICTLY NARROWER than the stop distance. Because the stop-to-level gap
    can never exceed that span, the bound guarantees
    ``abs(stop - level) < abs(entry - stop)``: the level a stop claims to rest
    on is never further from the stop than the stop is from the entry. The
    bound is a property of the trade, not a constant, so there is nothing to
    sweep and nothing to ratify.

    What it admits and refuses, measured on the same 704 levels, using the
    desk's own two existing stop floors as the stop distance (no new number is
    introduced by the measurement either): at a 1.0-ATR stop it admits 33/704
    levels (5%), and 1/154 of the >=5-touch levels; at a 2.5-ATR stop it admits
    465/704 (66%), and 63/154 (41%) of the >=5-touch levels. The levels it
    refuses at 2.5 ATR have median halfwidth 5.64% of price and reach 36.07%;
    the widest level it admits has halfwidth 13.41%, which is still inside the
    trade's own risk by construction. The exemption therefore becomes rare on
    tight stops, which is the honest consequence of refusing to pick a width:
    a level too vague to be more precise than the stop cannot earn a stop the
    right to be tighter than the noise floor.

    Fail closed, in both directions: no bar ranges, a non-finite stop, or a
    `stop_distance` that is missing, non-finite or non-positive all mean NOT
    backed, which routes the stop to the ordinary ATR floor exactly as an
    unmatched stop does today.
    """
    if not math.isfinite(stop_price):
        return False
    if stop_distance is None or not math.isfinite(stop_distance) or stop_distance <= 0:
        return False
    zone_low, zone_high = _pivot_bar_span(pivot_bars)
    if zone_low is None or zone_high is None:
        return False
    if (zone_high - zone_low) >= stop_distance:
        return False
    for rng in pivot_bars or ():
        try:
            low, high = float(rng[0]), float(rng[1])
        except (TypeError, ValueError, IndexError):
            continue
        if not (math.isfinite(low) and math.isfinite(high)) or low > high:
            continue
        if low <= stop_price <= high:
            return True
    return False


def _pivot_bar_span(
    pivot_bars: "tuple | list | None",
) -> "tuple[float | None, float | None]":
    """``(min low, max high)`` over a level's forming bars, or ``(None, None)``.

    The same span `cluster_span` computes, recovered from the ``(low, high)``
    pairs that travel with the level on `TechAnalysisResult.computed_level_bars`
    rather than from the cluster object, which the risk seats never see.
    """
    lows: list[float] = []
    highs: list[float] = []
    for rng in pivot_bars or ():
        try:
            low, high = float(rng[0]), float(rng[1])
        except (TypeError, ValueError, IndexError):
            continue
        if not (math.isfinite(low) and math.isfinite(high)) or low > high:
            continue
        lows.append(low)
        highs.append(high)
    if not lows or not highs:
        return None, None
    return min(lows), max(highs)


def level_zone_halfwidth(
    level_price: float,
    tolerance_pct: float = CLUSTER_TOLERANCE_PCT,
    *,
    zone_low: float | None = None,
    zone_high: float | None = None,
) -> float:
    """How far from a reported `Level.price` its own zone can still reach.

    **2026-09-30, item 55.** When `zone_low`/`zone_high` are supplied — the
    level's MEASURED span, the combined high-low range of the bars whose
    pivots formed it — the answer is read straight off them and no
    percentage is involved. `tolerance_pct` is then ignored entirely.

    When they are absent or unusable the percentage fallback below applies,
    unchanged. That is deliberate and it is the fail-closed direction: a
    caller holding a bare price still gets a bound wide enough to match
    within, instead of a zero-width zone that would silently strip a real
    level out of stop placement.

    THE ONE definition of "a level is a zone, not a number", so no caller
    ever has to restate it in different units. docs/WORK.md item 46.

    Derivation, read straight off `_cluster` and `find_structural_levels`
    above — not chosen here:

      * On the fallback path `_cluster` admits a pivot only when it sits
        within `tolerance_pct` of EVERY member, and in particular of the
        group's anchor — its LOWEST member, since the pivots are swept in
        ascending price. So every member of such a cluster lies in
        ``[anchor, anchor * (1 + tolerance_pct/100)]`` and the cluster's
        full span is at most ``anchor * tolerance_pct/100``.
      * `Level.price` is the MEAN of the cluster, so it lies inside that span
        and ``anchor <= price``.
      * Therefore the distance from `Level.price` to the furthest real pivot
        in its own zone is at most ``anchor * tolerance_pct/100``, which is at
        most ``price * tolerance_pct/100``.

    That last line is the whole function: a bound that is guaranteed to cover
    the zone, in the same unit the zone was defined in. Anything asking "is
    this price AT that level" must use this and nothing else — a tolerance
    expressed in some other unit (ATRs, say) tracks a quantity that has no
    fixed relationship to `tolerance_pct` and silently stops covering the
    zone on any name whose volatility moves. That is exactly the defect item
    46 recorded.
    """
    if not math.isfinite(level_price) or level_price <= 0:
        return 0.0
    lo = _finite_positive(zone_low)
    hi = _finite_positive(zone_high)
    if lo is not None and hi is not None and hi >= lo:
        # The furthest the level's own measured band reaches from its price.
        reach = max(level_price - lo, hi - level_price)
        if math.isfinite(reach) and reach > 0:
            return float(reach)
        # A degenerate band (one bar, zero range) cannot bound a match.
        # Fall through to the percentage rather than return 0.0, which would
        # delete the level from every "is price AT this level" test.
    return level_price * tolerance_pct / 100.0


# A level touched once is a coincidence, not structure.
#
# **This one IS sourced, and 2 is the answer the source gives.** docs/WORK.md
# item 55, 2026-09-13. It is not a tuned parameter: two points are the fewest
# that can define a horizontal line at all, and the published construction of
# this exact object uses the same figure — Tsinaslanidis (PhD thesis,
# University of Macedonia, 2012, §4.4): "Only price areas (bins) with
# frequencies greater or equal to two are considered as HSAR."
#
# Raising it is ruled out by measurement, not by preference. The same work
# tested whether more touches make a level better and found they do not
# (§4.6.1, 733 NASDAQ/NYSE names): "results indicate that these 'strengths'
# play no major role in predicting trend interruptions." Concretely, on
# NASDAQ two-local levels were hit 26,868 times and bounced 60.99% of the
# time, three-local levels 6,661 times and bounced 61.04%. Anything above 2
# would discard levels for no measured gain, so 2 is a floor read from the
# geometry and confirmed against published measurement — do not "tighten" it.
MIN_TOUCHES = 2

#: Board item 148 (2026-09-25): this was an inline `/ 10.0` in the strength
#: formula below, invisible to `src/number_sources.py`'s scanner (outside
#: rule (e)'s [0.5, 2.0) factor band, per that module's own docstring). Named
#: here so the ledger scanner sees it; value and behaviour are unchanged.
#: Nothing measured 10.0 against any alternative — it sets how fast strength
#: falls off with distance (at `distance_pct` == this value, distance has
#: halved the raw touch count), and no source for that particular fall-off
#: rate has been found.
LEVEL_STRENGTH_DISTANCE_DIVISOR_PCT = 10.0

# No MAX_DISTANCE_PCT here. Until 2026-09-12 a level only counted if it sat
# within a flat 40% of the current price — a number with no derivation that
# did not scale: 40% on a utility that moves 1.2% a day is thirty-plus days
# of typical travel, 40% on a name that moves 8% a day is five. The window
# is now READ from the instrument: a level is relevant if the stock can
# plausibly reach it, using the SAME reachability estimate
# `derive_structural_target` already uses to decide whether a target is
# reachable (`horizon_reach`, ATR x sqrt(sessions) x the reach multiple),
# taken at the longest horizon the desk permits any trade to state
# (`MAX_HORIZON_SESSIONS`). See `find_structural_levels`.

# No RECENCY_HALFLIFE_SESSIONS here. Until 2026-09-02 this was 252.0 (~1
# trading year), decaying each touch's contribution to `strength` by
# `0.5 ** (age_sessions / 252)`. It was picked for being round, never
# measured, and is gone rather than slowed down — see the strength
# computation in `find_structural_levels` for the measurement that replaced
# it and docs/RESEARCH_FINDINGS.md §7 for the full caveats.

# How many levels to report per side. Enough to describe the structure, few
# enough to stay readable in a prompt.
MAX_LEVELS_PER_SIDE = 6

# Bad-print detection. A bar is rejected only when its high exceeds, or its low
# falls below, the median of its immediate neighbours by this factor. 5x is
# deliberately permissive: real markets gap, halt and limit-move, and throwing
# away genuine volatility is worse than tolerating a rare bad print.
_CLEAN_WINDOW = 10
_CLEAN_FACTOR = 5.0

# ---------------------------------------------------------------------------
# Reachability — the ONE estimate of how far an instrument travels
# ---------------------------------------------------------------------------
# These two constants belong to the target derivation further down (see the
# "Deriving the TARGET from structure" section for the full reasoning and
# the asymmetry note). They are defined up here because the level scan now
# reuses them: the same question — "can price plausibly get there?" — decides
# both whether a level is a reachable TARGET and whether a level is RELEVANT
# at all. One estimate, two consumers, so they cannot drift apart.

#: How far price can plausibly get within the horizon, in sqrt(session)-scaled
#: ATRs. Deliberately looser than the measured-move projection — see the note
#: on asymmetry in the target section.
MAX_REACH_ATR_MULTIPLE = 1.5
#
# KEPT ON PURPOSE, 2026-10-01 (docs/WORK.md item 218). The question was
# whether this multiple can be replaced by READING the instrument -- each
# name's own realised favourable excursion over a hold of the stated
# length -- so no multiple need be chosen at all. It was measured, on the
# 400-bar daily set for 101 symbols the desk already holds: over a
# 15-session hold the MEDIAN per-name realised excursion is 1.93 ATR and
# the per-name MAXIMUM is 8.78 ATR, against this cap's 1.5*sqrt(15) =
# 5.81 ATR [measured 2026-10-01, rolling windows, ATR(14)].
#
# Two findings, both against replacing it:
#   1. The cap is NOT the binding constraint it was believed to be. At a
#      typical hold it sits at ~5.8 ATR while the instrument's own typical
#      advance is ~1.9 ATR, and recorded target distance is a median 3.25
#      ATR. It binds only in the tail, not on the ordinary trade. Said
#      plainly, because an earlier diagnosis in this repo says otherwise:
#      this cap is NOT what holds the desk's targets close.
#   2. A measured replacement still needs a QUANTILE -- median (1.93) and
#      maximum (8.78) differ by 4.5x and sit either side of today's value.
#      Picking between them is exactly the appetite choice the doctrine
#      bars, and the only choice-free statistic (the sample maximum) is an
#      outlier support bound, not a reach estimate.
# So the purpose cannot be served by reading the instrument without
# inventing a number, and the cap stays. Recorded rather than silently
# left alone.

#: Ceiling on `expected_horizon_sessions` before it enters the sqrt() travel
#: estimate. An analyst claiming a 250-session horizon would otherwise
#: licence a target ~16 ATRs out; this also absorbs a nonsense value. It is
#: also, by construction, the LONGEST horizon over which any trade on this
#: desk may ask how far the instrument travels — which is why the level scan
#: reads it as the relevance horizon.
MAX_HORIZON_SESSIONS = 60


def travel_over(atr: float, horizon_sessions: int) -> float:
    """Typical excursion over `horizon_sessions`: ``ATR * sqrt(sessions)``.

    Square-root scaling, not linear: daily ranges accumulate as a random
    walk, and ``ATR * N`` overstates an N-session excursion by roughly
    sqrt(N). This is the desk's single definition of "how far does this
    instrument travel in that many sessions".
    """
    return float(atr) * math.sqrt(max(1, int(horizon_sessions)))


#: Expected daily RANGE of a driftless random walk, in units of its daily
#: standard deviation: ``E[high - low] = sqrt(8/pi) * sigma ~= 1.5958 * sigma``
#: (Feller's asymptotic range distribution, the result Parkinson 1980 builds
#: the extreme-value variance estimator on — "Parkinson (1980) applies the
#: asymptotic distribution of range from the minimum value to the maximum
#: value over a specified time interval from Feller (1951) to get the
#: estimators of variance and volatility",
#: https://portfoliooptimizer.io/blog/range-based-volatility-estimators-overview-and-examples-of-usage/).
#:
#: This desk measures ATR, which is a RANGE, and the reflection principle
#: below is stated in SIGMA. This constant is the unit conversion between
#: them and nothing else. It is not a tuning knob and must never be
#: configured: it is a property of the Gaussian, not of any instrument.
#: Substitution stated plainly, as elsewhere in this module: ATR(14) is a
#: TRUE range and includes the overnight gap, so it runs slightly wider than
#: the intraday high-low range this identity describes. The effect is
#: conservative here — a larger ATR reports a LOWER touch probability for
#: the same stop, so the reading never flatters a stop's reachability.
ATR_PER_SIGMA = math.sqrt(8.0 / math.pi)


def touch_probability(
    width_atrs: float | None,
    horizon_sessions: int | None,
) -> float | None:
    """Probability a stop `width_atrs` ATRs away is TOUCHED within the horizon.

    This is a READING, not a fitted constant, and it is the quantity the
    desk actually wants whenever it asks "is this stop too wide". Two
    published results and no chosen number:

    1. The reflection principle for the running maximum of Brownian motion:
       ``P(sup_{s<=t} X_s >= a) = 2 P(X_t >= a) = 2 (1 - Phi(a / (sigma
       sqrt(t))))``. Stated at
       https://almostsuremath.com/2023/04/18/the-maximum-of-brownian-motion-and-the-reflection-principle/
       as "the reflected process is also a standard Brownian motion" giving
       ``P(X_t^* >= a) = 2 P(X_t >= a)``, with the running maximum
       distributed as ``|X_t|``.
    2. `ATR_PER_SIGMA` above, to state (1) in the range units this desk
       measures.

    So ``a / (sigma sqrt(H)) = width_atrs * ATR_PER_SIGMA / sqrt(H)`` and the
    answer falls out. Every input is read off the instrument in front of us
    (its own ATR) or off the trade (its own horizon); nothing is tuned.

    **What this does and does not license.** It converts a width into a
    probability. It does NOT say which probability is too low — that
    threshold is docs/WORK.md item 56 and is still open. It is exposed so
    that every refusal, and every stop that passes, records the reading that
    would settle it, rather than an ATR multiple whose meaning changes with
    the horizon.

    Driftless by construction: a drift term would need an expected return
    this desk does not forecast, and assuming one in the trade's favour
    would make every stop look less reachable than it is.

    Returns None when the width or the horizon cannot be read.
    """
    try:
        width = float(width_atrs)
        horizon = int(horizon_sessions)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(width) or width < 0 or horizon < 1:
        return None
    z = width * ATR_PER_SIGMA / math.sqrt(horizon)
    # 2 * (1 - Phi(z)) == erfc(z / sqrt(2)), evaluated directly so no normal
    # CDF approximation enters.
    return math.erfc(z / math.sqrt(2.0))


def horizon_reach(
    atr: float | None,
    horizon_sessions: int | None,
    *,
    max_reach_atr_multiple: float = MAX_REACH_ATR_MULTIPLE,
    max_horizon_sessions: int = MAX_HORIZON_SESSIONS,
) -> float | None:
    """The furthest price can plausibly get within the horizon, in dollars.

    ``ATR * sqrt(min(horizon, cap)) * max_reach_atr_multiple`` — the exact
    quantity `derive_structural_target` reports as `horizon_reach` and uses
    to decide whether a structural level is a reachable target. Exposed so
    `find_structural_levels` can ask the SAME question of every candidate
    level ("could any trade on this desk plausibly get here?") instead of
    applying a flat percentage that meant something different on every
    instrument.

    Returns None when there is no usable ATR or horizon: reachability
    cannot be measured, and nothing here guesses it.
    """
    volatility = _finite_positive(atr)
    if volatility is None:
        return None
    try:
        horizon = int(horizon_sessions) if horizon_sessions is not None else 0
    except (TypeError, ValueError):
        horizon = 0
    if horizon <= 0:
        return None
    horizon = min(horizon, max(1, int(max_horizon_sessions)))
    return travel_over(volatility, horizon) * float(max_reach_atr_multiple)


def _finite_positive(value) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or number <= 0:
        return None
    return number


@dataclass(frozen=True)
class Level:
    """One structural price level."""

    price: float
    kind: str  # "support" | "resistance"
    touches: int  # pivots clustered into this level
    last_touch_sessions_ago: int  # informational only — not a strength input, see below
    strength: float  # touch count, distance-discounted; higher = more significant
    # The level's MEASURED zone (item 55, 2026-09-30): the combined traded
    # range of the pivot bars that formed it. `None` on a level built by an
    # older caller or a fixture — then `level_zone_halfwidth` falls back to
    # the percentage, which is the fail-closed direction.
    zone_low: float | None = None
    zone_high: float | None = None

    @property
    def zone_halfwidth(self) -> float:
        """This level's own match tolerance, measured where possible."""
        return level_zone_halfwidth(self.price, zone_low=self.zone_low, zone_high=self.zone_high)

    # (low, high) of every BAR that drew this level — one entry per pivot in
    # the cluster, read straight off the instrument. This is what makes
    # "the stop is AT this level" answerable without inventing a tolerance:
    # see `stop_rests_on_level`. Empty means unknown, which fails closed.
    pivot_bars: tuple[tuple[float, float], ...] = ()

    def as_dict(self) -> dict:
        return asdict(self)


def _clean_bars(bars: list[OHLCV]) -> list[OHLCV]:
    """Drop bars that cannot be true, and single-bar print errors.

    Vendor feeds produce bad prints: a high 10x the surrounding range, a zero
    low, an inverted bar. A single bad print creates a phantom pivot, and a
    phantom pivot becomes a phantom level that a stop then gets placed against.

    The comparison is deliberately **local**. An earlier version compared every
    bar against the median of the whole series, which discarded most of the
    real history of any stock that trended hard — OKLO ran from $10 to $114, so
    a global median rejected its entire recent range as "outliers". A stock
    that multiplied is not corrupt data; a bar five times its immediate
    neighbours is. Only egregious deviations are removed, so genuine gaps and
    limit moves survive.
    """
    usable = [b for b in bars if b.high > 0 and b.low > 0 and b.close > 0 and b.high >= b.low]
    n = len(usable)
    if n < _CLEAN_WINDOW * 2 + 1:
        return usable

    highs = np.array([b.high for b in usable], dtype=float)
    lows = np.array([b.low for b in usable], dtype=float)

    keep: list[OHLCV] = []
    for i, bar in enumerate(usable):
        lo = max(0, i - _CLEAN_WINDOW)
        hi = min(n, i + _CLEAN_WINDOW + 1)
        # Neighbourhood excluding the bar under test — a bad print must not be
        # allowed to widen the band that is supposed to catch it.
        neighbour_highs = np.concatenate([highs[lo:i], highs[i + 1 : hi]])
        neighbour_lows = np.concatenate([lows[lo:i], lows[i + 1 : hi]])
        if neighbour_highs.size == 0 or neighbour_lows.size == 0:
            keep.append(bar)
            continue
        local_high = float(np.median(neighbour_highs))
        local_low = float(np.median(neighbour_lows))
        if local_high <= 0 or local_low <= 0:
            keep.append(bar)
            continue
        if bar.high > local_high * _CLEAN_FACTOR:
            continue
        if bar.low < local_low / _CLEAN_FACTOR:
            continue
        keep.append(bar)
    return keep


def turning_points(bars) -> list[tuple[int, float, str]]:
    """Confirmed swing highs and lows, oldest first, as ``(index, price, kind)``.

    ``kind`` is ``"H"`` for a swing high and ``"L"`` for a swing low. THE one
    detector: the level scan, the trailing stop and the trend exit all read it.

    The standard zigzag reversal rule, line for line as VectorBT PRO's
    ``pivot_info_1d_nb`` implements it, with an absolute threshold instead of
    a percentage: a candidate low is CONFIRMED once a later high is at least
    ``candidate_low + threshold``; a candidate high once a later low is at most
    ``candidate_high - threshold``; until then the candidate moves to each
    more extreme value; highs and lows alternate. The threshold is read at the
    candidate's own bar, and it is the stock's own Wilder ATR(14) on that bar
    — the move the stock itself typically makes in a day. No multiplier.

    The final candidate is never returned: it is unconfirmed, i.e. just the
    latest extreme, and trailing or exiting on it is trailing on today's price.

    Bars before the ATR has warmed up carry no threshold and cannot be a
    candidate. A bar with a missing or non-finite high/low/close is skipped
    (the true range needs the close). One departure from VectorBT: the first
    bar after the warm-up is never itself returned as a pivot — it is where
    the data starts, not where the stock turned.
    """
    bars = list(bars or [])
    n = len(bars)
    if n < MIN_SCAN_BARS:
        return []

    def _val(b, name):
        try:
            v = float(getattr(b, name, None))
        except (TypeError, ValueError):
            return math.nan
        return v if math.isfinite(v) else math.nan

    high = np.array([_val(b, "high") for b in bars], dtype=float)
    low = np.array([_val(b, "low") for b in bars], dtype=float)
    close = np.array([_val(b, "close") for b in bars], dtype=float)
    ok = ~(np.isnan(high) | np.isnan(low) | np.isnan(close))
    th = np.full(n, np.nan)
    idx = np.flatnonzero(ok)
    if idx.size >= ATR_PERIOD:
        atr = atr_series([bars[i] for i in idx])
        th[idx[ATR_PERIOD - 1 :]] = atr

    out: list[tuple[int, float, str]] = []
    first = -1
    last_kind = ""  # "L" or "H" — the current, unconfirmed candidate
    last_i = -1
    last_v = math.nan
    for i in range(n):
        if np.isnan(high[i]) or np.isnan(low[i]):
            continue
        if first == -1:
            if np.isnan(th[i]):
                continue
            first = i
        if last_i == -1:
            t = th[first]
            up = high[i] >= low[first] + t
            down = low[i] <= high[first] - t
            # The first move of one ATR only sets the direction. VectorBT
            # also books the starting bar as a confirmed pivot here; that bar
            # is the edge of the data, not a turn the stock made (a straight
            # climb would "confirm" a swing low at its first bar), so it is
            # not returned.
            if up and not down:
                last_kind, last_i, last_v = "H", i, float(high[i])
            elif down and not up:
                last_kind, last_i, last_v = "L", i, float(low[i])
            continue
        t = th[last_i]
        if last_kind == "L":
            if not np.isnan(t) and high[i] >= last_v + t:
                out.append((last_i, last_v, "L"))
                last_kind, last_i, last_v = "H", i, float(high[i])
            elif low[i] < last_v:
                last_i, last_v = i, float(low[i])
        else:
            if not np.isnan(t) and low[i] <= last_v - t:
                out.append((last_i, last_v, "H"))
                last_kind, last_i, last_v = "L", i, float(low[i])
            elif high[i] > last_v:
                last_i, last_v = i, float(high[i])
    return out


def _find_pivots(bars: list[OHLCV]) -> list[tuple[int, float, str, float, float]]:
    """Locate swing highs and lows with `turning_points`.

    Returns ``(index, price, "R"|"S", bar_low, bar_high)``. The last two are
    the pivot BAR's own traded range, which is what `_cluster` groups on
    (item 55, 2026-09-30) — the pivot price alone cannot say how wide the
    turning point actually was.
    """
    return [
        (i, price, "R" if kind == "H" else "S", float(bars[i].low), float(bars[i].high))
        for i, price, kind in turning_points(bars)
    ]


def _cluster(
    pivots: list[tuple[int, float, str, float, float]],
    tolerance_pct: float | None = None,
) -> list[list[tuple[int, float, str, float, float]]]:
    """Group pivots into levels by OVERLAP of the bars that made them.

    **The rule (item 55, 2026-09-30), and there is no number in it.** Two
    pivots belong to the same level when the price RANGES their bars actually
    traded overlap.

    **THE ACCEPTANCE TEST IS ALL-MEMBERS; THE PARTITION IS GREEDY FIRST-FIT,
    AND THE DIFFERENCE IS STATED HERE RATHER THAN GLOSSED.** A pivot joins a
    level only if its bar range overlaps the range of EVERY member already in
    it — never merely the nearest one. Equivalently, the members' ranges must
    share at least one common price: the running intersection
    ``[max(low), min(high)]`` stays non-empty. So every level names a price
    band that every one of its bars actually traded, and that invariant holds
    unconditionally.

    What this is NOT is agglomerative complete-linkage clustering, and an
    earlier version of this docstring said it was. The loop below sweeps the
    pivots in ascending price and puts each into the FIRST existing level that
    accepts it, so a pivot whose bar overlaps two levels joins the
    lower-priced one and the partition depends on the sweep order. True
    complete linkage would merge the globally closest pair at every step and
    is order-independent. The all-members acceptance test is what buys the
    anti-chaining property described below; the first-fit assignment is a
    deliberate, cheaper choice and the resulting partition is not claimed to
    be optimal. An untrue description of an algorithm is the same class of
    defect as an untrue alert, so it is written down rather than implied.

    Single linkage (chaining a pivot in when it reaches any one member) was
    tried first and is WRONG here for the reason this desk already wrote
    down in `src/data/news_dedup.py`: "single-linkage chaining is the classic
    way two distinct events get welded together through an intermediate
    article that resembles both." One tall bar spanning two unrelated shelves
    welds them into a level whose band covers neither. The assignment loop
    below is deliberately the same shape as `cluster_news`: try each existing
    level in order, require the candidate to pass against every member, and
    open a new level only when none accepts it.

    The level's width is then the members' own combined span — ``min(low)``
    to ``max(high)`` — measured, not assigned. That span can exceed the
    common intersection, and it is reported as it falls; nothing is capped,
    because a cap would be exactly the invented number this rule deletes.

    What this replaces, and why. Until today a pivot joined a group when its
    PRICE sat within `CLUSTER_TOLERANCE_PCT` (1%) of the group's anchor. That
    1% was never shown to be right (its own ledger entry said so), it did not
    scale with how violently a name moves, and it made a quiet utility's
    level as wide as a volatile name's. The bars themselves already answer
    the question the percentage was guessing at: if the market traded through
    both turning points at the same prices, it was defending the same band.
    A wide, volatile turning point produces a wide zone and a tight one
    produces a tight zone, with nothing to sweep and nothing to ratify.

    `tolerance_pct` is accepted and IGNORED so existing callers that still
    pass it keep working; it is no longer part of the definition.
    """
    if not pivots:
        return []
    ordered = sorted(pivots, key=lambda p: (p[1], p[0]))
    clusters: list[list[tuple[int, float, str, float, float]]] = []
    # Running common intersection of each cluster's bar ranges, parallel to
    # `clusters`. Non-empty by construction, which IS the complete-linkage
    # invariant.
    shared: list[tuple[float, float]] = []

    for pivot in ordered:
        p_low, p_high = pivot[3], pivot[4]
        measurable = math.isfinite(p_low) and math.isfinite(p_high) and p_low <= p_high
        placed = False
        for idx, members in enumerate(clusters):
            lo, hi = shared[idx]
            if measurable:
                # Overlaps EVERY member iff it overlaps their common band.
                if p_low > hi or p_high < lo:
                    continue
                shared[idx] = (max(lo, p_low), min(hi, p_high))
            else:
                # Unusable range: the overlap test cannot be evaluated. Fail
                # closed to the OLD percentage rule against this cluster's
                # anchor rather than split a level (which would drop it below
                # MIN_TOUCHES and delete it outright). Same complete-linkage
                # spirit: it must sit within the fallback band of every
                # member, and the cluster's shared band is unchanged because
                # this pivot contributes no measured range.
                anchor = members[0][1]
                if anchor <= 0:
                    continue
                tol = CLUSTER_TOLERANCE_PCT_FALLBACK
                if any(m[1] <= 0 or abs(pivot[1] - m[1]) / m[1] * 100.0 > tol for m in members):
                    continue
            members.append(pivot)
            placed = True
            break
        if not placed:
            clusters.append([pivot])
            shared.append((p_low, p_high) if measurable else (pivot[1], pivot[1]))
    return clusters


def cluster_span(cluster: list[tuple[int, float, str, float, float]]) -> tuple[float | None, float | None]:
    """``(low, high)`` of the bars forming a cluster — the level's own zone."""
    lows = [c[3] for c in cluster if len(c) > 4 and math.isfinite(c[3]) and c[3] > 0]
    highs = [c[4] for c in cluster if len(c) > 4 and math.isfinite(c[4]) and c[4] > 0]
    if not lows or not highs:
        return None, None
    return min(lows), max(highs)


def find_structural_levels(
    bars: list[OHLCV],
    *,
    tolerance_pct: float = CLUSTER_TOLERANCE_PCT,
    min_touches: int = MIN_TOUCHES,
    atr: float | None = None,
    max_reach_atr_multiple: float = MAX_REACH_ATR_MULTIPLE,
    max_horizon_sessions: int = MAX_HORIZON_SESSIONS,
    max_per_side: int = MAX_LEVELS_PER_SIDE,
    reference_price: float | None = None,
) -> tuple[list[Level], list[Level]]:
    """Return ``(support_levels, resistance_levels)``, most significant first.

    **Which side of price (2026-09-14).** `reference_price`, when a finite
    positive live price is supplied, decides support vs resistance. During
    market hours `bars` end at the PREVIOUS session's close (completed bars
    only — `MarketDataProvider.get_ohlcv`), so classifying against that close
    labelled a level "support" after the stock had already traded through
    it (ORCL 2026-09-10: traded 158.38 at the open, desk still called 159.79
    support). The relevance window and strength stay measured from the last
    completed close — they are properties of the completed history.

    Support is below the last close, resistance above it — classified by where
    the level sits *now*, not by whether the pivots forming it were highs or
    lows. A ceiling that price has broken through becomes a floor, and treating
    it as resistance because it was once a swing high would be wrong.

    **Relevance window (2026-09-12).** A repeated turning point only counts
    as actionable structure if the stock can plausibly reach it. "Plausibly
    reach" is not a percentage: it is `horizon_reach` — ``ATR x sqrt(H) x
    MAX_REACH_ATR_MULTIPLE`` — the same estimate `derive_structural_target`
    uses to decide whether a level is a reachable target, evaluated at
    ``H = max_horizon_sessions``, the longest horizon the desk lets any
    trade state. So the window is exactly "the furthest any permitted trade
    could travel": every level the target derivation could ever accept is
    inside it, and a level outside it could be neither a target for any
    stated horizon nor a stop (stops sit ~1.3-1.8 ATR away). It widens on
    a volatile name and narrows on a quiet one because ATR does, with no
    number chosen here. Until 2026-09-12 this was a flat 40% of price,
    which on a 1.2%-ATR utility looked thirty-plus days of travel away and
    on an 8%-ATR name looked five days away — and, once "no floor, no
    trade" became a rule, would have refused good trades on volatile names
    for a level that existed but sat just past an arbitrary line.

    `atr` may be supplied by a caller that already has the instrument's
    ATR(14) (`src/data/technical.py::atr_series` — the one implementation);
    otherwise it is computed here from the same cleaned bars. No ATR means
    reachability cannot be measured, and the scan returns nothing rather
    than fall back to a distance it cannot justify.

    Returns two empty lists when there is not enough clean history to say
    anything. Callers must treat that as "no structure identified" and decline
    the trade, never as "no structure exists".
    """
    clean = _clean_bars(bars)
    if len(clean) < MIN_SCAN_BARS:
        return [], []

    last_close = clean[-1].close
    if last_close <= 0:
        return [], []
    side_price = _finite_positive(reference_price) or last_close

    volatility = _finite_positive(atr)
    if volatility is None:
        series = atr_series(clean)
        volatility = _finite_positive(series[-1]) if series.size else None
    window = horizon_reach(
        volatility,
        max_horizon_sessions,
        max_reach_atr_multiple=max_reach_atr_multiple,
        max_horizon_sessions=max_horizon_sessions,
    )
    if window is None:
        return [], []

    last_index = len(clean) - 1
    pivots = _find_pivots(clean)

    supports: list[Level] = []
    resistances: list[Level] = []

    for cluster in _cluster(pivots, tolerance_pct):
        if len(cluster) < min_touches:
            continue

        price = float(np.mean([p[1] for p in cluster]))
        if price <= 0:
            continue

        distance_pct = abs(price - last_close) / last_close * 100.0
        if abs(price - last_close) > window:
            continue

        newest_index = max(p[0] for p in cluster)
        pivot_bars = tuple((float(clean[p[0]].low), float(clean[p[0]].high)) for p in cluster if 0 <= p[0] < len(clean))
        sessions_ago = last_index - newest_index

        # Strength is touch count, discounted by distance — no age term.
        # A recency half-life stood here until 2026-09-02, weighting each
        # touch down by `0.5 ** (age_sessions / 252)` so old touches barely
        # counted. It is gone because it was checked, not because it was
        # suspected: `src/data/level_quality.py` fit a decay curve directly
        # against our own bars (7,218 daily touch episodes, 101 symbols, 5
        # years) and found no age effect at all — likelihood ratio 0.00
        # against a constant-probability null, on both an age-of-level and a
        # time-since-last-touch definition (docs/RESEARCH_FINDINGS.md §7). A
        # level defended three years ago predicts a bounce exactly as well as
        # one defended last week, in the data we actually have.
        #
        # The same measurement DOES support touches: pooled bounce
        # probability rises from 0.516 (first touch) to 0.644 (5+ prior
        # touches) on real price series, against a flat ~0.48-0.51 on
        # shuffled controls (slope +0.0249 real vs +0.0049 shuffled) — a
        # real, reproduced effect. Treat it as promising, not settled: two
        # settings were changed after an initial run found nothing, and the
        # shuffle control rules out the return distribution as an
        # explanation but does not isolate levels from ordinary volatility
        # clustering (§7 states both caveats plainly). Touch count is the
        # honest way to use a promising-not-settled finding without
        # overclaiming it.
        #
        # The measured probability CURVE itself is deliberately not imported
        # as scoring weights, for two reasons visible in the numbers above:
        # it is non-monotonic at low touch counts (0.507 at one prior touch,
        # BELOW 0.516 at zero), and it pools every level past 5 touches into
        # one number for posterior-sample-size reasons, which would score a
        # 5-touch level and a 20-touch level identically here. Checked
        # directly on the desk's 101-symbol universe (2026-09-02): swapping
        # in that curve moves MORE of the top-6 selection than dropping decay
        # did (33.9% of side-slots vs 27.1%, both measured the same way), so
        # it is not a free upgrade sitting next to the simpler option — it is
        # a different and less defensible ranking. Distance is untouched:
        # nothing here measured it, so nothing here changes it.
        strength = float(len(cluster)) / (1.0 + distance_pct / LEVEL_STRENGTH_DISTANCE_DIVISOR_PCT)

        zlow, zhigh = cluster_span(cluster)
        level = Level(
            price=round(price, 2),
            kind="support" if price < side_price else "resistance",
            touches=len(cluster),
            last_touch_sessions_ago=int(sessions_ago),
            strength=round(strength, 4),
            zone_low=round(zlow, 4) if zlow is not None else None,
            zone_high=round(zhigh, 4) if zhigh is not None else None,
            pivot_bars=pivot_bars,
        )
        (supports if level.kind == "support" else resistances).append(level)

    supports.sort(key=lambda lv: -lv.strength)
    resistances.sort(key=lambda lv: -lv.strength)
    return supports[:max_per_side], resistances[:max_per_side]


def format_levels_block(
    supports: list[Level],
    resistances: list[Level],
    last_close: float,
    live_price: float | None = None,
) -> str:
    """Render levels for the Tech Analyst prompt.

    Resistance descends toward the price and support descends away from it, so
    the block reads top-to-bottom like a chart's vertical axis.

    `live_price` (in-progress session, 2026-09-14): when given, gaps and the
    price marker are measured from it and the marker says so explicitly,
    with the last completed close shown beside it — never one silently
    standing in for the other.
    """
    if not supports and not resistances:
        return (
            "Structural levels: NONE IDENTIFIED — insufficient clean price "
            "history. Do not invent levels; rate this symbol neutral."
        )

    live = _finite_positive(live_price)
    anchor = live or last_close

    def line(lv: Level) -> str:
        gap = (lv.price - anchor) / anchor * 100.0
        return f"    ${lv.price:,.2f} ({gap:+.1f}%) · {lv.touches} touches · last {lv.last_touch_sessions_ago}d ago"

    out = ["Structural levels (computed from the full price history):", "  Resistance (nearest last):"]
    if resistances:
        out.extend(line(lv) for lv in sorted(resistances, key=lambda x: -x.price))
    else:
        out.append("    none within range")
    if live:
        out.append(
            f"  >>> LIVE ${live:,.2f} (session IN PROGRESS, not a close; last completed close ${last_close:,.2f}) <<<"
        )
    else:
        out.append(f"  >>> last close ${last_close:,.2f} <<<")
    out.append("  Support (nearest first):")
    if supports:
        out.extend(line(lv) for lv in sorted(supports, key=lambda x: -x.price))
    else:
        out.append("    none within range")
    return "\n".join(out)


# ===========================================================================
# Deriving the TARGET from structure
# ===========================================================================
#
# Why this exists (2026-09-01)
# ----------------------------
# Reward:risk is `(target - entry) / (entry - stop)`. Since 2026-08-27 the
# STOP has been derived in code: structure places it, and
# `min_stop_atr_multiple` pushes it out when structure put it inside ordinary
# volatility. The TARGET was still `TechAnalysisResult.reference_target` — a
# number a language model wrote down. So the gate divided a measured quantity
# by an opinion. Those two are not commensurable, and the failure is
# systematic rather than random: a correctly-sized wide stop plus a modestly
# guessed target fails a 1.5 floor as a matter of arithmetic, whatever the
# trade is actually worth.
#
# Evidence, morning run of 2026-09-01 (`run-64290730`): 38 actionable
# signals, 30 of them (79%) under the floor before any judgement was applied.
# SLB `strong_buy`/`high` scored 1.28 on a 7.7% stop; AGX `sell`/`high`
# scored 0.84. Zero trades were placed.
#
# The floor is not the defect — its numerator is. So the numerator is
# computed here, from the same bars the stop already comes from, and the
# model's guess is demoted from arithmetic input to evidence.
#
# The rule
# --------
# Find the nearest structural level in the trade's direction. FULL STOP —
# the distance to it is not a filter (2026-09-30). It used to be: the level
# had to clear `min_target_atr_multiple` ATRs or it left the candidate set,
# and the target was then taken from the next level out. That contradicted
# the very next clause of this rule, and META's 2026-09-21 add is the
# measured case. `min_target_atr_multiple` now LABELS the answer
# (`TargetDerivation.target_inside_noise`: the whole reward sits inside one
# ordinary session's range) and never chooses it. Then:
#
#   level exists, within reach  ->  the level IS the target. Price has to get
#                                   through the first ceiling before any
#                                   further one, and reaching past it to a
#                                   further level to make the ratio work is
#                                   exactly the invention being removed.
#
#   level exists, beyond reach  ->  nothing structural stands between entry
#                                   and as far as this symbol travels in the
#                                   intended hold, so structure does not
#                                   bound this trade. Target = measured move.
#
#   no level in the direction   ->  nothing overhead is expected to stop this
#                                   trade. Target = measured move.
#                                   (2026-09-11, funnel item 6: this used to
#                                   REFUSE unless `setup_type` separately said
#                                   "breakout". The other reading of an empty
#                                   direction — a chart nothing can be read
#                                   from — is already caught one branch
#                                   earlier as REFUSAL_NO_STRUCTURE, so the
#                                   label was adding nothing but refusals.
#                                   `risk.constants.is_trend_trade` now owns
#                                   this condition, shared with the
#                                   reward:risk exemption.)
#
# "Reach" and the measured move are the same estimate of travel:
# `ATR * sqrt(sessions) * multiple`. Square-root scaling, not linear: daily
# ranges accumulate as a random walk, and `ATR * N` overstates an N-session
# excursion by roughly sqrt(N). `expected_horizon_sessions` is the analyst's
# own estimate of time-to-resolution, already pinned at entry for exactly
# this kind of use.
#
# Note the deliberate asymmetry between the reach multiple and the projection
# multiple. Reach answers "could price plausibly get there at all?", so it is
# loose — a trade that works is by definition an above-typical move. The
# projection answers "how far do I claim it goes when no level says
# otherwise?", so it is the typical excursion and nothing more. The two are
# separately configurable because they are answering different questions.
#
# Everything fails closed. No levels, no level in the direction, no ATR, no
# horizon -> no target and a NAMED reason. There is deliberately no default
# and no fallback: a manufactured target is the defect being removed, and a
# manufactured target with better provenance is still manufactured.
#
# Interaction with `min_stop_atr_multiple` is arithmetic and worth stating
# outright, because it is the binding constraint on the measured-move branch.
# A stop at `k` ATRs and a target at `p * ATR * sqrt(H)` clear a floor `f`
# only when `sqrt(H) >= f * k / p`.
#
# CORRECTED 2026-09-30, board item 90 — the paragraph that stood here was
# wrong on both of its inputs and is not softened. It read "at today's
# settings (p = 1.0, f = 1.5, and k = 1.5 scaled by setup ...) that is
# H >= ~6 sessions". Neither input was still true. `k` has been 2.5 since
# 2026-09-10 (PR #269 moved 3.0 -> 1.5 -> 2.5 in one squashed merge, so
# the 1.5 this line was written against never deployed), and the
# reward:risk floor `f` does not exist at
# all: `min_reward_risk_after_widening` was deleted as dead code on
# 2026-09-24 (board item 81) after it was found never to have refused or
# shrunk a single trade. With no floor there is no `f`, so nothing here
# binds a minimum horizon any more and the session counts quoted below are
# history, not live thresholds.
#
# What IS live, and what replaces the deleted floor as the thing worth
# knowing: on the measured-move branch the target is `1.0 * ATR * sqrt(H)`
# while the unbacked stop is `k * ATR` with no horizon term, so the
# reward:risk ratio of such a trade RISES with the stated hold — about
# 0.98 at H = 6 and 1.79 at H = 20 against the bare 2.5 base, and scaled
# by `_stop_atr_multiple`'s setup and regime factors either side of that.
#
# TWO CAVEATS ON THAT COMPARISON, because an earlier draft of this comment
# overstated it and was corrected the same day. FIRST, the two rules do
# not govern one population: this measured-move target fires when no level
# sits ABOVE entry, while the stop floor fires when no 5-touch level sits
# BELOW it. Those are independent conditions on opposite sides of price,
# so the ratio above describes their INTERSECTION, not either rule's own
# population. SECOND, a stop floor rebuilt as `1.0 * ATR * sqrt(H)` would
# not pin the ratio at exactly 1.0 as that draft claimed — the scalers
# still multiply in, giving roughly 1.17 for range/risk-on and 0.83 for
# breakout/risk-off. What is true, and is the reason to be careful rather
# than to refuse, is that any such floor makes the ratio horizon-INVARIANT
# where today it rises with the hold. The reformulation actually being
# pursued is structural, not another ATR multiple: board item 199.
#
# One further correction in the same place. The line below credited the
# stop floor to "real Maximum Adverse Excursion data". That describes the
# 1.5 that PR #269 passed through, not the live 2.5: the MAE fit was
# dropped both because its window's seat outputs were found to misreport
# confidence and data quality and because fitting a threshold to this
# desk's own past outcomes is barred (docs/OUTCOME.md, the 2026-09-12
# correction). Nor is the live 2.5 "sourced" — see
# `config/number_ledger.yaml` under
# `src.config.RiskConfig.min_stop_atr_multiple`: there is no citation for
# a fixed entry stop at that multiple, and both ends of the quoted band
# are unsupported.
#
# THIS IS THE ARITHMETIC THAT WAS CLOSING THE FUNNEL. Until 2026-09-10 the
# base `k` was 3.0 (range 3.45, breakout 2.55), which put the same thresholds
# at H >= ~21 / ~27 / ~15 sessions. This desk has never stated a 27-session
# horizon, so the range branch — the majority setup — could not clear the
# floor for ANY real signal, and measured against the record it did not: 0 of
# 222. The stop floor was moved off 3.0 in response (see
# `risk.min_stop_atr_multiple` in config/settings.yaml, and the correction
# above about what it was and was not re-derived FROM); these session
# counts fall out of that change, they were not tuned to a target, and per
# the correction above they no longer threshold anything.
#
# The structural-level branch is looser, because the level does not have to
# be a full projection away: it needs `W >= f*k*ATR` to clear the floor and
# `W <= 1.5*ATR*sqrt(H)` to be reachable, so H >= (f*k/1.5)^2 — about 2
# sessions for a range setup (it was ~12).

#: A target closer than this many ATRs is not a destination. Price is
#: already there and the "reward" is one ordinary session's noise.
#:
#: **What it does NOT do, since 2026-09-30: choose the level.** It used to
#: filter the candidate set, so a level inside this distance was dropped and
#: the target promoted to the next level out — past the very structure the
#: instrument had been rejected from. It now only LABELS the outcome
#: (`TargetDerivation.target_inside_noise`), which is the question it was
#: always asking: is the reward worth anything? Where that reward is
#: measured to is a separate fact about the chart, and a wall does not stop
#: being a wall by standing close.
MIN_TARGET_ATR_MULTIPLE = 1.0

#: Measured move, in sqrt(session)-scaled ATRs, claimed when no level stands
#: in the way. 1.0 = the typical excursion over the stated horizon, and
#: nothing more.
BREAKOUT_PROJECTION_ATR_MULTIPLE = 1.0

#: `MAX_REACH_ATR_MULTIPLE` and `MAX_HORIZON_SESSIONS` — the reach multiple
#: and the horizon ceiling this section reasons about — are defined near the
#: top of the module (the "Reachability" block) since 2026-09-12, because the
#: level scan reuses them. Their meaning is unchanged.

#: Refusal codes. Each names a DIFFERENT thing being wrong, because "no
#: trade" without a reason is what let the original defect survive unnoticed.
#:
#: A REFUSAL is a judgement about the trade: real inputs were measured and
#: the trade's geometry does not work. It belongs in the desk's "why didn't
#: we trade" statistics.
REFUSAL_NO_HORIZON = "no_expected_horizon"
#: Kept for historical logs and the census. Never produced since 2026-10-09:
#: a measured chart with no structural level now TRADES as a no-target trend
#: trade (`BASIS_NO_TARGET` below). The data-fault twin, an UNUSABLE history,
#: is still `FAULT_NO_STRUCTURE` and still does not trade.
REFUSAL_NO_STRUCTURE = "no_structural_levels"
REFUSAL_NO_LEVEL_IN_DIRECTION = "no_level_in_direction"
REFUSAL_PROJECTION_IMPLAUSIBLE = "projection_implausible"

#: DATA FAULT codes (2026-09-12). These are NOT trade judgements. A listed
#: instrument always has a price, always has volatility and always has
#: bars; when this desk holds none of them, the desk's own data acquisition
#: or computation failed. Until 2026-09-12 these were REFUSAL_* codes and
#: went into the record as "the trade was rejected", so a dead feed and a
#: trade that failed its rules were the same class of outcome and nobody
#: could count either. They are now carried on `TargetDerivation.fault`
#: (never `.refusal`), recorded as `data_fault` rather than
#: `constructor_dropped`, and paged to the owner through `send_owner_alert`
#: (`src/pipeline_stages.py::_alert_unmeasurable_symbols`).
#:
#: The safety posture is unchanged: a symbol the desk cannot measure is
#: NOT traded. Only its classification, record and alerting changed.
FAULT_NO_ENTRY = "entry_price_missing"
FAULT_NO_VOLATILITY = "volatility_reading_missing"
FAULT_NO_STRUCTURE = "price_history_unusable"
#: SIZING faults (docs/WORK.md item 120). The share count divides the dollar
#: allocation by a live price, so the price that sizes a new-name buy must be
#: a real TODAY PRINT — never a prior-session last trade and never a quote
#: MID. When the desk cannot obtain one, the name is refused as unmeasurable
#: rather than sized on a bad price. Two distinct codes because "the feed
#: returned nothing" and "the feed returned only a stale print" are different
#: conditions the census counts separately — the same split
#: `src/data/live_price.py` already draws between NO_PRICE_AT_ALL and
#: ONLY_STALE. FAULT_NO_ENTRY is NOT reused for these: its string
#: ("entry_price_missing") would misdescribe a name that has an analyst entry
#: but no live print to size against.
FAULT_NO_PRICE = "sizing_price_missing"
FAULT_STALE_PRICE = "sizing_price_stale"
#: Raised by the constructor, not here: the desk holds NO technical analysis
#: for a symbol it was asked to size. Every input below is absent at once,
#: so naming the first one ("no ATR") would misdescribe it.
FAULT_NO_ANALYSIS = "analysis_missing"

#: Kept for historical logs and the census: the two codes that used to name
#: the entry and volatility faults as refusals. Never produced any more.
REFUSAL_NO_ENTRY = "no_entry_price"
REFUSAL_NO_VOLATILITY = "no_volatility_reading"

#: Structural-level COVERAGE — what the bar history behind an empty
#: `computed_levels` list actually was. Same idea as
#: `src/data/macro.py::SeriesFreshness`: a separate axis from the reading
#: itself, so an absent reading is never passed off as a real one and a real
#: one is never mistaken for an outage. `find_structural_levels` returns the
#: same `[]` for "the feed gave us four bars" and for "three hundred clean
#: bars with no repeated turning point within reach", and the derivation
#: cannot tell which without this. Recorded by the tech analyst, which has
#: the bars, onto `TechAnalysisResult.levels_coverage`.
#: Owner rule 2026-10-09: a stock whose chart was read and holds no level
#: still trades, on the 2.5 ATR stop, with NO take-profit — a missing number
#: is never invented. The derivation returns `price=None`, this basis,
#: `no_target=True` and `level_used=None`, so every consumer of
#: `structural_ceiling=(level_used is not None)` manages it as a trend trade
#: (trailed, no reward:risk check). Distinct from `FAULT_NO_STRUCTURE`, which
#: is a data fault (the bars were unusable) and still does not trade.
BASIS_NO_TARGET = "no_target"

COVERAGE_MEASURED = "measured"  # scan ran; an empty result is about the chart
COVERAGE_NO_BARS = "no_bars"  # the feed returned nothing
COVERAGE_UNUSABLE_BARS = "unusable_bars"  # bars arrived; fewer clean ones than MIN_SCAN_BARS
COVERAGE_UNKNOWN = "unknown"  # not recorded (older row, hand-built object)

#: There is deliberately NO "insufficient_history" coverage state. A short
#: listing history is no longer a trade refusal on a bar count (the
#: constructor's young-listing count gate was dropped, docs/WORK.md item 180,
#: owner ruling 2026-09-25); a young name is judged on whether a protective
#: stop is readable, and a name too young to read ANY stop from is refused by
#: the constructor's stop-readability rule
#: (`STOP_REFUSAL_NO_STRUCTURAL_STOP_NO_VOLATILITY`), not here. When the scan
#: has fewer clean bars than `MIN_SCAN_BARS` it simply finds no levels;
#: `unusable_bars` already says the true thing — the bars the desk holds
#: cannot run the scan.
#:
#: The coverage states under which an empty level list is a DATA fault. The
#: honest reading of `unknown` is "cannot claim the chart was measured", so
#: it is classified with the faults: fail-closed for the trade either way,
#: and the alert names the coverage so an `unknown` that recurs is visible.
_FAULT_COVERAGE = frozenset(
    {
        COVERAGE_NO_BARS,
        COVERAGE_UNUSABLE_BARS,
        COVERAGE_UNKNOWN,
    }
)


def structure_coverage(
    bars: Sequence[OHLCV] | None,
) -> str:
    """Which COVERAGE_* state this bar history is in, read from the bars.

    The minimum is `find_structural_levels`'s own precondition
    (`MIN_SCAN_BARS` clean bars — enough for one pivot AND one ATR
    reading, the two things the scan needs), not a threshold chosen here.
    Below it the scan cannot run, whether too few bars arrived or cleaning
    removed them — either way the desk holds no usable chart; at or above
    it the scan runs and whatever it finds, including nothing, is a
    measurement of the chart.
    """
    if not bars:
        return COVERAGE_NO_BARS
    if len(_clean_bars(list(bars))) < MIN_SCAN_BARS:
        return COVERAGE_UNUSABLE_BARS
    return COVERAGE_MEASURED


@dataclass(frozen=True)
class TargetDerivation:
    """Outcome of deriving a target. ``price is None`` means NO TRADE.

    Exactly one of two things explains a ``None`` price, and they are
    carried on DIFFERENT fields so they can never be confused in the record:

    * `refusal` — a trade judgement (a REFUSAL_* code): real inputs were
      measured and the trade's geometry does not work.
    * `fault` — a data fault (a FAULT_* code): the desk could not obtain
      the input a real market always has. The symbol is UNMEASURABLE, not
      judged.

    `detail` is the one line a human (or a downstream model reading the
    order's reasoning) needs in either case.
    """

    price: float | None
    basis: str = ""  # "structural_level" | "measured_move" | "" (no trade)
    refusal: str = ""  # one of the REFUSAL_* codes; "" otherwise
    detail: str = ""
    level_used: float | None = None
    horizon_reach: float | None = None  # ATR * sqrt(H) * max_reach_atr_multiple
    model_target: float | None = None  # the LLM's guess, kept as evidence
    divergence_pct: float | None = None  # computed vs. the model's guess
    fault: str = ""  # one of the FAULT_* codes; "" otherwise
    #: The target sits on a real structural level, but CLOSER to entry than
    #: ``atr * min_target_atr_multiple`` — the whole reward is inside one
    #: ordinary session's movement. A recorded FACT about the geometry, never
    #: a refusal: the desk's ratified position is that reward:risk ranks a
    #: trade and does not gate it (`src/risk/constants.py::
    #: reward_risk_floor_applies`, owner 2026-09-17). Before 2026-09-30 this
    #: condition was invisible, because such a level was dropped from the
    #: candidate set and the target was promoted past it instead.
    target_inside_noise: bool = False
    #: The chart was measured and holds no structural level: the trade goes
    #: ahead with NO take-profit (`price is None`) — owner rule 2026-10-09.
    #: Never set together with `refusal` or `fault`.
    no_target: bool = False

    @property
    def refused(self) -> bool:
        """No trade — for EITHER reason. Callers that need to tell the two
        apart read `unmeasurable` / `fault` and `refusal`. A `no_target`
        derivation is NOT refused: it trades without a take-profit."""
        return self.price is None and not self.no_target

    @property
    def unmeasurable(self) -> bool:
        return bool(self.fault)

    def as_dict(self) -> dict:
        return asdict(self)


def _refused(code: str, detail: str, model_target: float | None) -> TargetDerivation:
    return TargetDerivation(
        price=None,
        refusal=code,
        detail=detail,
        model_target=model_target,
    )


def _faulted(code: str, detail: str, model_target: float | None) -> TargetDerivation:
    return TargetDerivation(
        price=None,
        fault=code,
        detail=detail,
        model_target=model_target,
    )


def structural_floor(
    levels: Sequence[float],
    entry_price: float | None,
    direction: str,
) -> float | None:
    """The nearest computed level on the STOP side of this entry, or None.

    A stop-placement helper, not a gate. For one day (2026-09-12, docs/
    WORK.md item 54) `None` here refused the trade — "no floor, no trade".
    That rule was replaced the same day on sourced research: no published
    method refuses a trade for lack of support below (Chandelier, Parabolic
    SAR, the Darvas box and Kullamägi's entry-bar low all place a stop with
    no level at all), and the population it refused — names at or near
    their highs — is the one George & Hwang (2004) found forecasts returns.
    The live constructor now reads a fallback stop from the instrument and
    gates on the stop's WIDTH instead. `src/backtest/engine.py` still calls
    this for its stop candidate.

    `levels` is `TechAnalysisResult.computed_levels` — the union of every
    level `find_structural_levels` found within the instrument's own
    reach — partitioned here against THIS entry rather than the last close,
    for the same reason `derive_structural_target` re-partitions. Returns
    the nearest such level so a log can name it.
    """
    entry = _finite_positive(entry_price)
    if entry is None:
        return None
    usable = [p for p in (_finite_positive(lv) for lv in levels or ()) if p is not None]
    if str(direction or "").strip().lower() == "short":
        side = [p for p in usable if p > entry]
        return min(side) if side else None
    side = [p for p in usable if p < entry]
    return max(side) if side else None


def derive_structural_target(
    *,
    entry_price: float | None,
    direction: str,
    levels: Sequence[float],
    atr: float | None,
    horizon_sessions: int | None,
    setup_type: str | None,
    model_target: float | None = None,
    min_target_atr_multiple: float = MIN_TARGET_ATR_MULTIPLE,
    breakout_projection_atr_multiple: float = BREAKOUT_PROJECTION_ATR_MULTIPLE,
    max_reach_atr_multiple: float = MAX_REACH_ATR_MULTIPLE,
    max_horizon_sessions: int = MAX_HORIZON_SESSIONS,
    levels_coverage: str = COVERAGE_UNKNOWN,
) -> TargetDerivation:
    """Compute where the instrument actually travels, or decline by name.

    Two classes of "no target", on two fields (see `TargetDerivation`):
    a data FAULT when an input a real market always has is missing (entry
    price, ATR, usable bars), a REFUSAL when the inputs are real and the
    trade's geometry does not work. `levels_coverage` is what separates an
    empty `levels` list into those two — see the COVERAGE_* constants.

    Works both directions. For a long the target is ABOVE entry and drawn
    from levels above it; for a short it is BELOW entry and drawn from levels
    below it. Nothing about the rule is long-only — the comparison operators
    and the sign of the projection are the whole of the difference.

    `levels` is the UNION of the computed supports and resistances, as bare
    prices. That is deliberate: `find_structural_levels` classifies a level
    as support or resistance relative to the LAST CLOSE, while a trade is
    entered at a live price that may sit on the other side of it. What
    matters here is only whether a level is above or below THIS ENTRY, so the
    partition is redone against `entry_price` rather than inherited.

    `model_target` is never used to choose the answer. It is carried through
    so callers can log where the model's guess and the computed level
    disagree, which is the cheapest available read on whether the model's
    targets were ever worth anything.
    """
    guess = _finite_positive(model_target)

    entry = _finite_positive(entry_price)
    if entry is None:
        # A listed instrument always has a price. Not having one is a quote
        # or analysis-acquisition failure on this desk's side.
        return _faulted(
            FAULT_NO_ENTRY,
            "DATA FAULT: no usable entry price was obtained (no live quote "
            "and no analyst entry) — the symbol cannot be measured",
            guess,
        )

    volatility = _finite_positive(atr)
    if volatility is None:
        # ATR is computed by this desk from its own bars
        # (`src/data/technical.py`), never read from a model. A real market
        # always has volatility; a missing reading means the bar history
        # was too short for the calculation or the indicator never ran.
        return _faulted(
            FAULT_NO_VOLATILITY,
            "DATA FAULT: no ATR reading was computed from the bar history, "
            "so neither the noise floor nor the reachable distance can be "
            "measured — the symbol cannot be measured",
            guess,
        )

    try:
        horizon = int(horizon_sessions) if horizon_sessions is not None else 0
    except (TypeError, ValueError):
        horizon = 0
    if horizon <= 0:
        return _refused(
            REFUSAL_NO_HORIZON,
            "no expected_horizon_sessions, so there is no period over which to ask how far this symbol travels",
            guess,
        )
    horizon = min(horizon, max(1, int(max_horizon_sessions)))

    is_short = str(direction or "").strip().lower() == "short"
    travel = travel_over(volatility, horizon)
    # The same `horizon_reach` the level scan uses for relevance; the scan's
    # window (taken at `max_horizon_sessions`) is therefore always at least
    # this wide, so no level this derivation could accept was ever dropped
    # before it got here.
    reach = horizon_reach(
        volatility,
        horizon,
        max_reach_atr_multiple=max_reach_atr_multiple,
        max_horizon_sessions=max_horizon_sessions,
    )
    noise = volatility * min_target_atr_multiple
    setup = str(setup_type or "").strip().lower()

    def _divergence(price: float) -> float | None:
        if guess is None:
            return None
        return round((price - guess) / guess * 100.0, 2)

    usable = [p for p in (_finite_positive(lv) for lv in levels or ()) if p is not None]
    if not usable:
        # No structure at all is NOT the same as "no ceiling overhead", and
        # it is not one thing either. `find_structural_levels` returns the
        # same `[]` when the history was too short or too dirty for the
        # pivot scan to run at all (a DATA fault: the desk did not obtain a
        # usable chart) and when the scan ran over enough clean bars and
        # found no level with the minimum touches within reach (a
        # measurement of the chart: a relentless trend, or every repeated
        # turn further away than the instrument can travel). Only the coverage
        # recorded beside the levels tells them apart. The data fault does
        # not trade. The measured absence DOES (owner rule 2026-10-09): the
        # stop is the 2.5 ATR unbacked stop, there is no take-profit, and
        # `level_used=None` makes it a trend trade downstream.
        coverage = str(levels_coverage or COVERAGE_UNKNOWN).strip().lower()
        if coverage in _FAULT_COVERAGE:
            return _faulted(
                FAULT_NO_STRUCTURE,
                f"DATA FAULT: no structural levels could be computed because "
                f"the price history was unusable (coverage={coverage}) — the "
                f"symbol cannot be measured",
                guess,
            )
        return TargetDerivation(
            price=None,
            basis=BASIS_NO_TARGET,
            detail=(
                "NO TARGET: the price history was measured and holds no "
                "structural level (no repeated turning point within reach of "
                "the current price) — the trade has no take-profit and is "
                "managed as a trend trade on its ATR stop"
            ),
            level_used=None,
            horizon_reach=round(reach, 4),
            model_target=guess,
            no_target=True,
        )

    # THE WALL: the nearest structural level standing in this trade's way,
    # partitioned against the ENTRY and with NO noise filter applied.
    #
    # **The noise floor used to select the level, and that is the defect
    # this block fixes (META, 2026-09-21).** The filter was
    # ``p > entry + noise``, so a level inside one ATR of entry did not
    # merely fail to be a destination — it vanished from the candidate set,
    # and `min(...)` then promoted the target to the NEXT level up, on the
    # far side of the wall price had just been rejected from.
    #
    # Measured on the desk's own record: the intraday add at $728.41
    # carried ATR $21.22, so the floor sat at $749.63 and swallowed the
    # computed resistances at $730.41 (2 touches) and $739.84 (3 touches,
    # the 2026-01-29 rejection). The STORED target was $785.20 — above BOTH
    # rejections and above the then 52-week high. (Reproducing the scan
    # from completed bars puts that cluster at $784.58; the order was
    # built intraday against a live reference price, which is the whole of
    # the difference. $785.20 is what the trade record holds and is the
    # number the desk quoted.) Price stalled at $779.82
    # and reversed. The level detector was never at fault: it found that
    # structure over its 1800-day history and reported it. The selection
    # threw it away.
    #
    # A level is not made irrelevant by being close. A wall two dollars
    # overhead is the most relevant fact on the chart — it is simply a wall
    # with very little room under it, which is a statement about the
    # REWARD, not about where the level is. So the noise floor keeps its
    # real job (`target_inside_noise` below: saying the reward is inside one
    # ordinary session's movement) and loses the job it was never meant to
    # do (choosing which level the desk trades toward).
    #
    # This is the rule this module already applied one step further out:
    # `tests/test_target_derivation.py::
    # test_a_nearer_shelf_still_fails_the_floor_and_that_is_the_answer`
    # pins that a nearer shelf giving a WORSE payoff is the answer and the
    # floor does not move to accommodate it. The same is true when the
    # shelf is nearer still.
    if is_short:
        in_the_way = [p for p in usable if p < entry]
        wall = max(in_the_way) if in_the_way else None
    else:
        in_the_way = [p for p in usable if p > entry]
        wall = min(in_the_way) if in_the_way else None

    # Kept under the old name for the branches below, whose meaning is
    # unchanged: "the level this derivation measured itself against".
    nearest = wall

    if wall is not None and abs(wall - entry) <= reach:
        price = round(wall, 2)
        crowded = abs(wall - entry) < noise
        room = (
            f"; the reward is ${abs(wall - entry):,.2f}, INSIDE the "
            f"${noise:,.2f} one-session noise floor — a wall with little "
            f"room under it, recorded as such rather than stepped over"
            if crowded
            else ""
        )
        return TargetDerivation(
            price=price,
            basis="structural_level",
            detail=(
                f"nearest structural level {'below' if is_short else 'above'} "
                f"entry ${entry:,.2f} is ${price:,.2f} "
                f"({(price - entry) / entry * 100:+.1f}%), reachable inside "
                f"{horizon} sessions (ATR ${volatility:,.2f} x sqrt({horizon})"
                f" x {max_reach_atr_multiple:g} = ${reach:,.2f}){room}"
            ),
            level_used=price,
            horizon_reach=round(reach, 4),
            model_target=guess,
            divergence_pct=_divergence(price),
            target_inside_noise=crowded,
        )

    # Past this point no level stands in the way within the horizon.
    #
    # **2026-09-11, docs/WORK.md funnel item 6.** This used to refuse when
    # `nearest is None` unless the analyst had separately typed
    # `setup_type="breakout"`, on the reasoning that no-level-in-direction is
    # ambiguous between a genuine breakout and a chart nothing can be read
    # from. That second possibility is ALREADY excluded above: a chart
    # nothing can be read from yields no `usable` levels at all and is
    # refused as REFUSAL_NO_STRUCTURE. So by the time we get here, the desk's
    # own level computation succeeded AND found nothing in the trade's
    # direction — a measured absence of a ceiling, which is exactly the
    # condition the ATR projection below is for. Requiring a label on top of
    # it refused real trades (funnel item 6) for a wording, while the correct
    # projection sat in this same function unreachable.
    #
    # `is_trend_trade` is the SINGLE definition of that condition, shared
    # with the reward:risk exemption in `PortfolioConstructor.
    # _widen_stop_past_noise` — the two agree by construction rather than by
    # coincidence. Passing the measured fact means this is now always True
    # here and REFUSAL_NO_LEVEL_IN_DIRECTION is unreachable; the constant is
    # kept because it appears in historical logs and in the census.
    if nearest is None and not is_trend_trade(setup_type, structural_ceiling=False):
        return _refused(
            REFUSAL_NO_LEVEL_IN_DIRECTION,
            f"no structural level {'below' if is_short else 'above'} entry "
            f"${entry:,.2f} beyond the ${noise:,.2f} noise floor, and "
            f"setup_type={setup or 'unset'!r} does not claim a breakout",
            guess,
        )

    projection = travel * breakout_projection_atr_multiple
    if projection <= noise:
        return _refused(
            REFUSAL_PROJECTION_IMPLAUSIBLE,
            f"a {horizon}-session measured move of ${projection:,.2f} does not clear its own ${noise:,.2f} noise floor",
            guess,
        )
    raw = entry - projection if is_short else entry + projection
    # A projection may never cross a wall either. This branch is reached
    # only when `wall` is out of REACH, and the projection multiple is
    # below the reach multiple at this module's own defaults, so the clamp
    # is inert on the default path. It is not decoration: both multiples
    # are caller-supplied parameters, so a caller passing a projection
    # multiple at or above the reach multiple would otherwise reintroduce
    # exactly the defect fixed above — an ATR target on the far side of a
    # level the instrument has been rejected from.
    if wall is not None:
        raw = max(raw, wall) if is_short else min(raw, wall)
    if raw <= 0:
        # Only reachable on an extreme ATR-to-price ratio, but a short whose
        # projection runs through zero is arithmetic, not a trade.
        return _refused(
            REFUSAL_PROJECTION_IMPLAUSIBLE,
            f"a {horizon}-session measured move of ${projection:,.2f} puts "
            f"the short's target at or below zero from entry ${entry:,.2f}",
            guess,
        )

    price = round(raw, 2)
    if nearest is None:
        why = (
            f"no structural level {'below' if is_short else 'above'} entry on "
            f"a chart that yielded {len(usable)} level(s) elsewhere, so "
            f"nothing overhead is expected to stop this trade "
            f"(setup_type={setup or 'unset'!r})"
        )
    else:
        why = (
            f"nearest structural level ${nearest:,.2f} is "
            f"${abs(nearest - entry):,.2f} away, past the ${reach:,.2f} "
            f"reachable in {horizon} sessions, so nothing stands in the way"
        )
    return TargetDerivation(
        price=price,
        basis="measured_move",
        detail=(
            f"{why}; measured move = ATR ${volatility:,.2f} x sqrt({horizon})"
            f" x {breakout_projection_atr_multiple:g} = ${projection:,.2f} -> "
            f"${price:,.2f}"
        ),
        level_used=round(nearest, 2) if nearest is not None else None,
        horizon_reach=round(reach, 4),
        model_target=guess,
        divergence_pct=_divergence(price),
    )


# ---------------------------------------------------------------------------
# Item 55 recording — WHAT the stop was actually based on, pinned at entry.
# ---------------------------------------------------------------------------
#
# FALSIFICATION ONLY. This record exists so the question "what IS a structural
# level — how many bars make a swing point, and how wide is a level's zone?"
# can one day be answered from the desk's OWN record of what its levels did,
# instead of from argument. It may be read to show that the CURRENT definition
# is WRONG: that stops sitting on supposedly-real levels were broken as often
# as stops that sat on nothing, that a zone of a given width held no better
# than a wider or narrower one, that a pivot confirmed by N bars predicted
# nothing.
#
# IT MAY NEVER BE SWEPT FOR A BETTER NUMBER. Choosing a swing-point rule or
# `CLUSTER_TOLERANCE_PCT` by trying candidate values against the outcomes
# stored here is FITTING A NUMBER TO THIS DESK'S OWN HISTORY, which this desk
# bars outright ("no fitting, only reading"). A replacement for either number
# comes from published evidence or from a measurement made on data that is not
# this book's trading record. A pass that lowers a window because the record
# "says so" is the defect, not the finding.
#
# NO INVENTED THRESHOLDS. Nothing here classifies an outcome as "respected",
# "pierced" or "broken", because every one of those words needs a cutoff that
# nobody can source today. Only RAW DISTANCES in price units are stored; the
# classification is derived later, by a reader who states and defends its own
# cutoff, from numbers that were never rounded to it. Anything genuinely
# unknown at write time is stored as JSON null and never substituted.

#: Schema version for `describe_stop_level_basis`. Bumped only when a FIELD is
#: added or its meaning changes, so a later reader can tell which rows carry
#: which facts rather than guessing from absence.
STOP_LEVEL_BASIS_VERSION = 2


def describe_stop_level_basis(
    *,
    level_price: float | None,
    stop_loss: float,
    entry_price: float,
    computed_levels: Sequence[float] | None = None,
    computed_level_touches: dict | None = None,
    is_short: bool = False,
    tolerance_pct: float = CLUSTER_TOLERANCE_PCT,
) -> dict:
    """The identity of the level standing behind a stop, as plain facts.

    RECORDING ONLY — reads the inputs it is handed and computes nothing the
    desk acts on. Nothing in this function decides whether a stop is
    level-backed, how wide a zone is, or where a level sits; it is handed the
    answer the live code already reached and writes down what that answer was
    made of. Read the module note above for the falsification-only limit.

    `level_price` is whatever `PortfolioConstructor._level_backing_stop`
    returned — the computed level the shipping stop sits at, or None when no
    computed level stood behind it. None is a FACT worth recording, not a
    missing value: it is the control group without which "levels hold" cannot
    be falsified, so the record is written either way.

    Fields, all in price units unless stated:
      `level_backed`        — whether a computed level stood behind the stop.
      `level_price`         — that level's price, or null.
      `level_kind`          — "support" for a long, "resistance" for a short,
                              which is the side `_level_backing_stop` searches;
                              null when there is no level.
      `level_touches`       — how many pivots clustered into it, i.e. how many
                              separate times price turned there. Null when the
                              analysis carried no touch count for that price.
      `pivot_rule`/`pivot_atr_period` — how a swing point was confirmed at
                              entry: a reversal of at least the stock's own
                              ATR over this many sessions (`turning_points`).
                              Version 1 rows carried a bar-count window instead.
      `zone_low`/`zone_high`/`zone_width` — the level's own zone, the "how wide
                              is a level's zone" half, as the live
                              `level_zone_halfwidth` drew it at entry.
      `stop_to_level`       — signed distance from the stop to the level price,
                              positive when the stop sits BEYOND the level (further
                              from entry, the protective side) and negative when it
                              sits short of it. Sign is taken per side.
      `stop_inside_zone`    — whether the stop fell inside the level's zone.
      `entry_to_level`      — distance from entry to the level, same sign rule.
      `tolerance_pct`       — the clustering tolerance in force at entry, so a
                              later reader knows which definition produced the
                              level rather than assuming today's.
      `schema_version`      — `STOP_LEVEL_BASIS_VERSION`.
    """

    def _f(value) -> float | None:
        try:
            out = float(value)
        except (TypeError, ValueError):
            return None
        return out if math.isfinite(out) else None

    stop = _f(stop_loss)
    entry = _f(entry_price)
    price = _f(level_price)
    record: dict = {
        "schema_version": STOP_LEVEL_BASIS_VERSION,
        "level_backed": price is not None,
        "level_price": price,
        "level_kind": None if price is None else ("resistance" if is_short else "support"),
        "level_touches": None,
        "pivot_rule": "zigzag_reversal_atr",
        "pivot_atr_period": int(ATR_PERIOD),
        "zone_low": None,
        "zone_high": None,
        "zone_width": None,
        "zone_tolerance_pct": _f(tolerance_pct),
        "stop_loss": stop,
        "entry_price": entry,
        "stop_to_level": None,
        "entry_to_level": None,
        "stop_inside_zone": None,
        "computed_level_count": (None if computed_levels is None else len(list(computed_levels))),
    }
    if price is None:
        return record

    touches = None
    if computed_level_touches:
        # The touch map is keyed by the same float the level list carries, so
        # an exact hit is the normal case; a tolerant match covers a key that
        # survived a round-trip through JSON at a different precision. No
        # match stores null rather than a guessed count.
        for key, count in computed_level_touches.items():
            key_f = _f(key)
            if key_f is not None and math.isclose(key_f, price, rel_tol=1e-9, abs_tol=1e-9):
                try:
                    touches = int(count)
                except (TypeError, ValueError):
                    touches = None
                break
    record["level_touches"] = touches

    half = level_zone_halfwidth(price, tolerance_pct)
    record["zone_low"] = price - half
    record["zone_high"] = price + half
    record["zone_width"] = half * 2.0
    if stop is not None:
        # "Beyond" is below the level for a long and above it for a short.
        record["stop_to_level"] = (price - stop) if not is_short else (stop - price)
        record["stop_inside_zone"] = bool(record["zone_low"] <= stop <= record["zone_high"])
    if entry is not None:
        record["entry_to_level"] = (entry - price) if not is_short else (price - entry)
    return record
