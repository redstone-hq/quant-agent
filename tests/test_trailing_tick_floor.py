"""The one-venue-tick minimum-ratchet floor that replaced the 2% floor.

Owner ruling 2026-10-02: ratchet stops on smaller moves. Kept in its own
file so `tests/test_trailing_stops.py` does not grow.
"""

import pytest  # noqa: F401

from src.risk.trailing import compute_trailing_stop, venue_tick
from tests.test_trailing_stops import _bars, _rising_with_higher_lows


def _falling_with_lower_highs():
    """The short mirror of `_rising_with_higher_lows`: a clean downtrend
    with two CONFIRMED swing highs."""
    highs = [90, 92, 94, 100, 94, 92, 90, 82, 84, 86, 90, 86, 84, 82, 75]
    return _bars([(h, h - 2) for h in highs])


# ==========================================================================
# The tick floor that replaced MIN_RATCHET_PCT (2026-10-02 owner ruling)
# ==========================================================================


def test_small_improvement_now_ratchets_where_the_2pct_floor_refused_it():
    """A sub-2% tighten is now TAKEN.

    The old floor demanded `new >= old * 1.02`. This candidate clears the
    live stop by far more than one venue tick but by well under 2%, so the
    retired percentage gate would have refused it and the tick gate takes
    it. Guards the owner ruling: ratchet on smaller moves, lock gains in
    sooner.
    """
    stop = 108.0
    proposal = compute_trailing_stop(
        symbol="AAA",
        setup_type="breakout",
        entry=100.0,
        current_price=125.0,
        current_stop=stop,
        reference_target=None,
        bars=_rising_with_higher_lows(),
        atr=2.0,
    )
    assert proposal is not None, "a real improvement must not be refused"
    improvement = proposal.new_stop - stop
    assert improvement >= venue_tick(stop), "must clear one tick"
    assert improvement < stop * 0.02, (
        "this case only proves the point if the old 2% floor would have "
        f"refused it: improvement {improvement} vs 2% floor {stop * 0.02}"
    )


def test_sub_tick_improvement_is_still_refused_it_is_the_same_price():
    """The floor that replaced 2% is one tick, not nothing: a candidate
    inside one tick of the live stop quantizes to the same price and is not
    worth an amend."""
    from src.risk import trailing as _t

    stop = 100.0
    level = stop + venue_tick(stop) / 4.0
    assert _t.min_ratchet_floor(stop) == stop + venue_tick(stop)
    assert level < _t.min_ratchet_floor(stop) - venue_tick(stop) / 2.0


def test_a_trailed_stop_can_never_move_toward_more_risk():
    """THE one-way invariant, swept rather than spot-checked.

    Across a grid of entries, prices, live stops, ATRs, setup types and
    BOTH sides, no proposal may ever sit on the loosening side of the stop
    it replaces: a long's stop may only rise, a short's may only fall.
    Tightening more often (the tick floor) must not create a path that
    loosens even once.
    """
    checked = 0
    for setup in ("breakout", "range"):
        for qty in (1.0, -1.0):
            for entry in (50.0, 100.0, 250.0):
                for drift in (0.80, 0.95, 1.0, 1.05, 1.40):
                    price = entry * (drift if qty > 0 else 2.0 - drift)
                    for stop_frac in (0.70, 0.90, 0.99, 1.01, 1.10, 1.30):
                        stop = entry * stop_frac
                        for atr in (None, 0.5, 2.0, 10.0):
                            bars = _rising_with_higher_lows() if qty > 0 else _falling_with_lower_highs()
                            proposal = compute_trailing_stop(
                                symbol="AAA",
                                setup_type=setup,
                                entry=entry,
                                current_price=price,
                                current_stop=stop,
                                reference_target=None,
                                bars=bars,
                                atr=atr,
                                qty=qty,
                                risk_per_share=(abs(entry - stop) if stop else None),
                            )
                            checked += 1
                            if proposal is None:
                                continue
                            if qty > 0:
                                assert proposal.new_stop > stop, (
                                    f"LOOSENED a long stop: {stop} -> {proposal.new_stop} ({setup}, atr={atr})"
                                )
                            else:
                                assert proposal.new_stop < stop, (
                                    f"LOOSENED a short stop: {stop} -> {proposal.new_stop} ({setup}, atr={atr})"
                                )
    assert checked >= 500, f"sweep covered too little: {checked} cases"
