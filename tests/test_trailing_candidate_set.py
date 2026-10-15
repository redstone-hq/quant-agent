"""The trail evaluates a candidate SET, not the first candidate it finds.

Board items 195 and 196. `evaluate_trailing_stop` used to build the
chandelier only when the structural pivot produced nothing, so it committed
to one candidate BEFORE testing it against the invariants. A structural pivot
that the noise band then rejected silently suppressed a chandelier level that
would have passed — the structural leg could block protection it could not
itself provide.

These tests pin the widened search and, just as deliberately, pin the case it
does NOT rescue: when the chandelier is itself inside the noise band there is
no lower already-derived level, and nothing is synthesised at the band's own
edge, because a level read off today's price is a price-follower rather than
a structural stop. No multiple or threshold is introduced or moved here.
"""

from dataclasses import dataclass

from src.risk.trailing import (
    CHANDELIER_ATR_MULTIPLE,
    MIN_BARS_FOR_A_READING,
    NOISE_BAND_ATR_MULTIPLE,
    TRAIL_CODE_INSIDE_NOISE_BAND,
    TRAIL_CODE_NO_CANDIDATE,
    TRAIL_CODE_TRAILED,
    evaluate_trailing_stop,
)

ATR = 4.0
PRICE = 120.0
STOP = 100.0
#: Everything below is stated in terms of the module's own constants so that
#: moving one of them breaks the test rather than silently invalidating it.
BAND_FLOOR = PRICE - NOISE_BAND_ATR_MULTIPLE * ATR


from datetime import date as _date

#: Every fixture bar is a real completed daily bar dated after the entry day:
#: the chandelier reads only bars dated strictly after `entry_date`.
_ENTRY_DAY = _date(2026, 1, 2)
_BAR_DAY = _date(2026, 1, 5)


@dataclass
class _Bar:
    high: float
    low: float
    date: _date = _BAR_DAY
    close: float | None = None

    def __post_init__(self):
        # The swing detector's ATR needs a close; mid-range unless stated.
        if self.close is None:
            self.close = (self.high + self.low) / 2


def _bars(lows, *, peak):
    """Bars with the given lows; the LAST bar carries the run high.

    Highs are otherwise pinned just above their own low so that `peak` is
    unambiguously `max(high)` and therefore fixes the chandelier level.
    """
    bars = [_Bar(high=lo + 0.2, low=lo) for lo in lows]
    assert peak > max(b.high for b in bars), (
        "the constructed peak must be the run high for the chandelier to be the level this test intends"
    )
    bars[-1] = _Bar(high=peak, low=bars[-1].low)
    return bars


def _peak_for(chandelier: float) -> float:
    return chandelier + CHANDELIER_ATR_MULTIPLE * ATR


def _evaluate(bars):
    return evaluate_trailing_stop(
        symbol="AAA",
        setup_type="breakout",
        entry=100.0,
        current_price=PRICE,
        current_stop=STOP,
        reference_target=None,
        bars=bars,
        entry_date=_ENTRY_DAY,
        atr=ATR,
    )


# A V whose bounce clears the bars' own ATR(14), after 14 flat bars for that
# ATR to warm up on (swings are confirmed by an ATR reversal since 2026-10-15).
def _pivot_at(level: float):
    return [level + 1.5] * 14 + [level + 1.5, level + 1.0, level + 0.5, level, level + 0.5, level + 1.0, level + 1.5]


def test_a_pivot_inside_the_noise_band_no_longer_suppresses_the_chandelier():
    """Item 196: the rejected first candidate used to end the search."""
    pivot = 118.0
    assert pivot > BAND_FLOOR, "the pivot must be inside the band to bind"
    chandelier = 112.0
    assert chandelier < BAND_FLOOR, "the chandelier must clear the band"

    result = _evaluate(_bars(_pivot_at(pivot), peak=_peak_for(chandelier)))

    assert result.code == TRAIL_CODE_TRAILED
    assert result.proposal is not None
    assert result.proposal.source == "chandelier"
    assert result.proposal.new_stop == chandelier


def test_structure_is_still_preferred_when_it_passes_the_invariants():
    """Item 195: the leg is not deleted, and it still outranks the chandelier.

    It has never fired in production because a confirmed pivot needs seven
    bars and the desk's windows were shorter; where one does exist and is
    usable, it is taken over a chandelier level that is further from price.
    """
    pivot = 112.0
    assert pivot < BAND_FLOOR
    result = _evaluate(_bars(_pivot_at(pivot), peak=_peak_for(108.0)))

    assert result.code == TRAIL_CODE_TRAILED
    assert result.proposal is not None
    assert result.proposal.source == "structure"
    assert result.proposal.new_stop == pivot


def test_a_chandelier_inside_the_band_still_refuses_and_invents_nothing():
    """Item 196's unfixed half, pinned so a later patch cannot slip it in.

    Monotonically rising lows confirm no pivot, so the chandelier is the only
    candidate. It sits inside the band, and the module places nothing — it
    does NOT fall back to the band edge, which would be a stop read off
    today's price.
    """
    rising = [104.0, 105.0, 106.0, 107.0, 108.0, 109.0, 110.0]
    result = _evaluate(_bars(rising, peak=_peak_for(118.0)))

    assert result.proposal is None
    assert result.code == TRAIL_CODE_INSIDE_NOISE_BAND


def test_too_few_bars_reports_the_missing_data_not_an_empty_search():
    """`no_structure_and_no_usable_chandelier` reads as "we looked and found
    nothing", which is wrong when there was not enough to look at. The caller
    filters bars to since-entry and leaves them empty on any fetch failure,
    so this is the common case, not the exotic one."""
    from src.risk.trailing import MIN_BARS_FOR_A_READING, TRAIL_CODE_TOO_FEW_BARS

    for n in (0, 1, MIN_BARS_FOR_A_READING - 1):
        result = evaluate_trailing_stop(
            symbol="AAA",
            setup_type="breakout",
            entry=100.0,
            current_price=PRICE,
            current_stop=STOP,
            reference_target=None,
            bars=_bars([101.0] * n, peak=102.0) if n else [],
            entry_date=_ENTRY_DAY,
            atr=None,
        )
        assert result.proposal is None, n
        assert result.code == TRAIL_CODE_TOO_FEW_BARS, n


def test_bars_present_but_no_usable_leg_still_reports_no_candidate():
    """Bars DID arrive and neither leg could use them: the empty-set reason
    code is unchanged by the widened search."""
    result = evaluate_trailing_stop(
        symbol="AAA",
        setup_type="breakout",
        entry=100.0,
        current_price=PRICE,
        current_stop=STOP,
        reference_target=None,
        bars=_bars([130.0] * MIN_BARS_FOR_A_READING, peak=131.0),
        entry_date=_ENTRY_DAY,
        atr=None,
    )
    assert result.proposal is None
    assert result.code == TRAIL_CODE_NO_CANDIDATE
