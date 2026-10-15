"""Deterministic trailing stops — spec Phase 3.7.

Trailing is arithmetic and belongs in Python. These tests pin the two
management modes and every invariant that keeps a trail from strangling the
trade it is protecting.
"""

from dataclasses import dataclass

import pytest

from src.risk.trailing import (
    CHANDELIER_ATR_MULTIPLE,
    MIN_BARS_FOR_A_READING,
    MIN_RATCHET_TICKS,
    min_ratchet_floor,
    venue_tick,
    RANGE_BREAKEVEN_R_MULTIPLE,
    RANGE_SECOND_RATCHET_LOCK_R,
    RANGE_SECOND_RATCHET_TRIGGER_R,
    compute_trailing_stop,
)


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
        # The swing detector reads the stock's own ATR, whose true range needs
        # a close; a fixture bar closes mid-range unless it says otherwise.
        if self.close is None:
            self.close = (self.high + self.low) / 2


#: Swings are confirmed by a reversal of the stock's own ATR(14) since
#: 2026-10-15, so a swing fixture opens with 14 flat bars for that ATR to
#: warm up on. Flat bars make no turn of their own.
_WARM = 14


def _bars(pattern):
    """`pattern` is a list of (high, low) tuples, oldest first."""
    return [_Bar(high=h, low=lo) for h, lo in pattern]


def _rising_with_higher_lows():
    """A clean uptrend with two CONFIRMED swing lows, at 100 and 110.

    Confirmed means price has since moved back by at least the stock's own
    ATR(14) — `src.data.levels.turning_points`, the one detector the level
    scan reads too (owner ruling 2026-10-15; no bar-count window remains).
    The lows series is deliberately V-shaped around
    each pivot; a monotonic ramp contains no swing lows at all, which is
    exactly what the chandelier fallback is for.
    """
    lows = [110] * _WARM + [110, 108, 106, 100, 106, 108, 110, 118, 116, 114, 110, 114, 116, 118, 125]
    return _bars([(lo + 2, lo) for lo in lows])


# ---------------------------------------------------------------------------
# Type A (range) — item 212: the structural trail runs FROM ENTRY. It used to
# be gated behind the recorded take-profit target, an unsourced number, which
# left a range position holding only its original entry stop through the
# stretch of the move it spends most of its life in. The ratified R-multiple
# ratchets below are unchanged; whichever leg proposes the TIGHTER stop wins.
# ---------------------------------------------------------------------------


def test_range_setup_trails_structurally_below_its_target():
    """Item 212: below the recorded target the structural trail now runs, to
    the confirmed higher low, instead of proposing nothing at all."""
    proposal = compute_trailing_stop(
        symbol="AAA",
        setup_type="range",
        entry=100.0,
        current_price=118.0,
        current_stop=95.0,
        reference_target=130.0,
        bars=_rising_with_higher_lows(),
        entry_date=_ENTRY_DAY,
        atr=2.0,
    )
    assert proposal is not None
    assert proposal.new_stop == 110.0
    assert proposal.source == "structure"


def test_range_trail_never_moves_a_stop_away_from_price():
    """The load-bearing invariant of item 212: ungating can only TIGHTEN. With
    the live stop already above every candidate, nothing is proposed — never a
    loosening one."""
    assert (
        compute_trailing_stop(
            symbol="AAA",
            setup_type="range",
            entry=100.0,
            current_price=118.0,
            current_stop=117.0,
            reference_target=130.0,
            bars=_rising_with_higher_lows(),
            entry_date=_ENTRY_DAY,
            atr=2.0,
            risk_per_share=abs(100.0 - 90.0),
        )
        is None
    )


def test_range_trail_refuses_until_enough_bars_to_read():
    """Missing data must not produce an action. The chandelier reads only
    completed bars dated strictly AFTER the entry day (the current holding's
    opening fill day, passed as `entry_date`): the entry day's own bar holds
    prices from before the fill. With no bar after it there is no information
    since entry, so the chandelier gives no reading and the structure leg
    (which needs its 7-bar pivot span) gives none either.

    The fixture's high (107) reads 107 - 3 x 2 = 101: between the 90 stop and
    the 105 price and outside the 1.25-ATR noise band, so it WOULD move the
    stop if it were read. Price stays below +1R so neither range ratchet
    answers. The last check proves the same prices are read once the bar is
    dated after the entry day."""
    from datetime import date

    from src.models.analysis import OHLCV
    from src.risk.trailing import TRAIL_CODE_TOO_FEW_BARS, evaluate_trailing_stop

    entry_day = date(2026, 10, 6)

    def _bar(d):
        return OHLCV(date=d, open=100.0, high=107.0, low=99.0, close=105.0, volume=1000)

    def _ev(bars):
        return evaluate_trailing_stop(
            symbol="AAA",
            setup_type="range",
            entry=100.0,
            current_price=105.0,
            current_stop=90.0,
            reference_target=130.0,
            bars=bars,
            atr=2.0,
            risk_per_share=abs(100.0 - 90.0),
            entry_date=entry_day,
        )

    for n in (0, 1):
        ev = _ev([_bar(entry_day)] * n)
        assert ev.proposal is None, n
        assert ev.structural_code == TRAIL_CODE_TOO_FEW_BARS, n

    ev = _ev([_bar(entry_day), _bar(date(2026, 10, 7))])
    assert ev.proposal is not None and ev.proposal.source == "chandelier"
    assert ev.proposal.new_stop == 101.0


def test_range_trail_keeps_the_structural_refusal_on_the_record():
    """The ratchet leg answers, and the structural leg's own reason survives
    on the evaluation instead of being overwritten by the ratchet's code."""
    from src.risk.trailing import TRAIL_CODE_TOO_FEW_BARS, evaluate_trailing_stop

    ev = evaluate_trailing_stop(
        symbol="AAA",
        setup_type="range",
        entry=100.0,
        current_price=110.0,
        current_stop=90.0,
        reference_target=130.0,
        bars=[],
        entry_date=_ENTRY_DAY,
        atr=2.0,
        risk_per_share=abs(100.0 - 90.0),
    )
    assert ev.proposal is not None and ev.proposal.new_stop == 100.0
    assert ev.structural_code == TRAIL_CODE_TOO_FEW_BARS


def test_a_ratchet_level_inside_the_noise_band_is_not_placed():
    """Every leg that can place a stop clears the same invariants. Entry 100,
    initial stop 90, price 101: the +1R step is not reached, but at price
    100.5 with a wide ATR the breakeven level would sit inside the daily-noise
    band, and a stop inside the band is how a range trade is stopped out
    inside the very range it was bought to traverse."""
    from src.risk.trailing import TRAIL_CODE_INSIDE_NOISE_BAND, evaluate_trailing_stop

    ev = evaluate_trailing_stop(
        symbol="AAA",
        setup_type="range",
        entry=100.0,
        current_price=110.0,
        current_stop=90.0,
        reference_target=130.0,
        bars=[],
        entry_date=_ENTRY_DAY,
        atr=20.0,
        risk_per_share=abs(100.0 - 90.0),
    )
    assert ev.proposal is None
    assert ev.code == TRAIL_CODE_INSIDE_NOISE_BAND


def test_range_trail_takes_the_tighter_of_ratchet_and_structure():
    """Both legs may propose on the same call; the tighter wins. Entry 100,
    initial stop 90 -> the breakeven ratchet proposes 100 and the higher low
    proposes 110, so 110 is placed."""
    proposal = compute_trailing_stop(
        symbol="AAA",
        setup_type="range",
        entry=100.0,
        current_price=118.0,
        current_stop=95.0,
        reference_target=130.0,
        bars=_rising_with_higher_lows(),
        entry_date=_ENTRY_DAY,
        atr=2.0,
        risk_per_share=abs(100.0 - 90.0),
    )
    assert proposal is not None
    assert proposal.new_stop == 110.0
    assert proposal.source == "structure"


def test_range_setup_trails_once_the_target_is_exceeded():
    proposal = compute_trailing_stop(
        symbol="AAA",
        setup_type="range",
        entry=100.0,
        current_price=125.0,
        current_stop=95.0,
        reference_target=120.0,
        bars=_rising_with_higher_lows(),
        entry_date=_ENTRY_DAY,
        atr=2.0,
    )
    assert proposal is not None
    assert proposal.new_stop == 110.0  # the higher low
    assert proposal.source == "structure"


def test_range_setup_with_no_target_still_trails_structurally():
    """Item 212: the recorded target no longer decides anything here, so a
    range trade without one trails on structure exactly like one with it."""
    proposal = compute_trailing_stop(
        symbol="AAA",
        setup_type="range",
        entry=100.0,
        current_price=125.0,
        current_stop=95.0,
        reference_target=None,
        bars=_rising_with_higher_lows(),
        entry_date=_ENTRY_DAY,
        atr=2.0,
    )
    assert proposal is not None
    assert proposal.new_stop == 110.0
    assert proposal.source == "structure"


# ---------------------------------------------------------------------------
# Type A (range) +1R breakeven ratchet — 2026-09-04 audit fix #3.
#
# Standard practice (Van Tharp's R-multiple framework; Elder's Triple
# Screen): move the stop to breakeven once a trade has banked +1R, instead
# of leaving it fully exposed to the original risk until 100% of a possibly
# much larger target is hit. Additive to the target-exceeded structural
# trail above, which is unchanged.
# ---------------------------------------------------------------------------


def test_range_setup_reaches_breakeven_at_plus_1r():
    """Entry 100, initial stop 90 -> R = 10. +1R = price 110. Below target
    (130), so the OLD code proposed nothing at all here; the new code moves
    the stop to breakeven (100)."""
    proposal = compute_trailing_stop(
        symbol="AAA",
        setup_type="range",
        entry=100.0,
        current_price=110.0,
        current_stop=90.0,
        reference_target=130.0,
        bars=[],
        entry_date=_ENTRY_DAY,
        atr=2.0,
        risk_per_share=abs(100.0 - 90.0),
    )
    assert proposal is not None
    assert proposal.new_stop == 100.0
    assert proposal.source == "breakeven_ratchet"


def test_range_setup_below_1r_still_gets_no_protection():
    """Same trade, price only at 105 (0.5R) — not yet earned breakeven."""
    assert (
        compute_trailing_stop(
            symbol="AAA",
            setup_type="range",
            entry=100.0,
            current_price=105.0,
            current_stop=90.0,
            reference_target=130.0,
            bars=[],
            entry_date=_ENTRY_DAY,
            atr=2.0,
            risk_per_share=abs(100.0 - 90.0),
        )
        is None
    )


def test_range_setup_does_not_re_propose_breakeven_once_already_there():
    """Once the stop is already at or beyond breakeven, no repeat order."""
    assert (
        compute_trailing_stop(
            symbol="AAA",
            setup_type="range",
            entry=100.0,
            current_price=115.0,
            current_stop=100.0,
            reference_target=130.0,
            bars=[],
            entry_date=_ENTRY_DAY,
            atr=2.0,
            risk_per_share=abs(100.0 - 90.0),
        )
        is None
    )


def test_range_setup_retrace_after_1r_no_longer_gives_back_the_full_risk():
    """The audit's concrete failure mode: a range trade reaches +1R, then
    retraces hard. Under the OLD logic (no protection below target) it would
    ride all the way back down to the original stop, giving back the full
    initial risk (100 -> 90) for zero net gain. Under the fix, +1R already
    ratcheted the stop to breakeven, so the SAME retrace only gives back the
    gain, never the original risk."""
    initial_stop = 90.0
    # Step 1: price reaches +1R (110) -> stop ratchets to breakeven.
    first = compute_trailing_stop(
        symbol="AAA",
        setup_type="range",
        entry=100.0,
        current_price=110.0,
        current_stop=initial_stop,
        reference_target=130.0,
        bars=[],
        entry_date=_ENTRY_DAY,
        atr=2.0,
        risk_per_share=(abs(100.0 - initial_stop) if initial_stop else None),
    )
    assert first is not None and first.new_stop == 100.0
    live_stop = first.new_stop

    # Step 2: price retraces hard, back toward the original stop.
    second = compute_trailing_stop(
        symbol="AAA",
        setup_type="range",
        entry=100.0,
        current_price=91.0,
        current_stop=live_stop,
        reference_target=130.0,
        bars=[],
        entry_date=_ENTRY_DAY,
        atr=2.0,
        risk_per_share=(abs(100.0 - initial_stop) if initial_stop else None),
    )
    assert second is None  # no further ratchet proposed on the way down
    # The live stop is what the broker actually holds; it stayed at
    # breakeven rather than reverting to the original risk level.
    assert live_stop == 100.0 > initial_stop


def test_range_setup_short_mirror_reaches_breakeven_at_plus_1r():
    """Short mirror: entry 100, initial stop 110 -> R = 10, +1R at price 90."""
    proposal = compute_trailing_stop(
        symbol="AAA",
        setup_type="range",
        entry=100.0,
        current_price=90.0,
        current_stop=110.0,
        reference_target=70.0,
        bars=[],
        entry_date=_ENTRY_DAY,
        atr=2.0,
        risk_per_share=abs(100.0 - 110.0),
        qty=-10,
    )
    assert proposal is not None
    assert proposal.new_stop == 100.0
    assert proposal.source == "breakeven_ratchet"


def test_range_setup_with_no_initial_stop_gets_no_breakeven_ratchet():
    """Backward compatibility: omitting `initial_stop` (every pre-fix call
    site until updated) means R cannot be measured, so nothing is proposed —
    fails closed, exactly the old behaviour, never a guessed risk."""
    assert (
        compute_trailing_stop(
            symbol="AAA",
            setup_type="range",
            entry=100.0,
            current_price=110.0,
            current_stop=90.0,
            reference_target=130.0,
            bars=[],
            entry_date=_ENTRY_DAY,
            atr=2.0,
        )
        is None
    )


def test_range_setup_with_no_target_still_gets_the_breakeven_ratchet():
    """The breakeven ratchet does not require a target at all — only entry,
    stop and current price — so a range trade with no recorded target still
    gets SOME protection at +1R instead of the old blanket 'no target -> no
    trailing ever' answer."""
    proposal = compute_trailing_stop(
        symbol="AAA",
        setup_type="range",
        entry=100.0,
        current_price=110.0,
        current_stop=90.0,
        reference_target=None,
        bars=[],
        entry_date=_ENTRY_DAY,
        atr=2.0,
        risk_per_share=abs(100.0 - 90.0),
    )
    assert proposal is not None
    assert proposal.new_stop == 100.0
    assert proposal.source == "breakeven_ratchet"


def test_breakeven_ratchet_uses_the_initial_stop_not_the_live_one():
    """R must be measured from the risk actually taken at entry. If the live
    stop had already been ratcheted elsewhere (here: further out, at 80, as
    if a previous move had gone the wrong way), the breakeven trigger still
    uses the original 90 -> R = 10, not the live 20."""
    proposal = compute_trailing_stop(
        symbol="AAA",
        setup_type="range",
        entry=100.0,
        current_price=110.0,
        current_stop=80.0,
        reference_target=130.0,
        bars=[],
        entry_date=_ENTRY_DAY,
        atr=2.0,
        risk_per_share=abs(100.0 - 90.0),
    )
    assert proposal is not None
    assert proposal.new_stop == 100.0


# ---------------------------------------------------------------------------
# Type A (range) +2R -> +1R SECOND ratchet — item 142, owner-ratified
# 2026-09-25. Stacks on top of the breakeven step and stays below the target:
# once a range trade reaches +2R, lock the stop at +1R instead of giving back
# everything between breakeven and target on a reversal.
#
# Worked example throughout: entry 100, initial stop 95, so 1R = $5, +1R = 105,
# +2R = 110, target 130.
# ---------------------------------------------------------------------------


def test_range_second_ratchet_locks_plus_1r_at_plus_2r():
    """At +2R (price 110) the stop moves from breakeven (100) up to +1R (105)."""
    proposal = compute_trailing_stop(
        symbol="AAA",
        setup_type="range",
        entry=100.0,
        current_price=110.0,
        current_stop=100.0,
        reference_target=130.0,
        bars=[],
        entry_date=_ENTRY_DAY,
        atr=2.0,
        risk_per_share=abs(100.0 - 95.0),
    )
    assert proposal is not None
    assert proposal.new_stop == 105.0
    assert proposal.source == "second_ratchet"


def test_range_second_ratchet_fires_directly_from_the_initial_stop():
    """It does not require the breakeven step to have run first: from the entry
    stop (95) straight to +1R (105) once +2R is reached. Never loosens — 105
    beats 95."""
    proposal = compute_trailing_stop(
        symbol="AAA",
        setup_type="range",
        entry=100.0,
        current_price=110.0,
        current_stop=95.0,
        reference_target=130.0,
        bars=[],
        entry_date=_ENTRY_DAY,
        atr=2.0,
        risk_per_share=abs(100.0 - 95.0),
    )
    assert proposal is not None
    assert proposal.new_stop == 105.0
    assert proposal.source == "second_ratchet"


def test_range_reversal_after_2r_stops_with_plus_1r_locked():
    """The gap item 142 closes. Price reaches +2R (110), the stop locks at +1R
    (105); a reversal back to 105 then stops the trade out at +1R of gain
    instead of round-tripping to breakeven."""
    initial_stop = 95.0
    # Step 1: +2R reached -> stop ratchets to +1R.
    first = compute_trailing_stop(
        symbol="AAA",
        setup_type="range",
        entry=100.0,
        current_price=110.0,
        current_stop=100.0,
        reference_target=130.0,
        bars=[],
        entry_date=_ENTRY_DAY,
        atr=2.0,
        risk_per_share=(abs(100.0 - initial_stop) if initial_stop else None),
    )
    assert first is not None and first.new_stop == 105.0
    live_stop = first.new_stop

    # Step 2: price reverses toward the lock. No further ratchet on the way
    # down, and the live stop the broker holds is +1R, not breakeven.
    second = compute_trailing_stop(
        symbol="AAA",
        setup_type="range",
        entry=100.0,
        current_price=106.0,
        current_stop=live_stop,
        reference_target=130.0,
        bars=[],
        entry_date=_ENTRY_DAY,
        atr=2.0,
        risk_per_share=(abs(100.0 - initial_stop) if initial_stop else None),
    )
    assert second is None
    assert live_stop == 105.0 > 100.0  # +1R locked, above breakeven


def test_range_below_2r_stays_at_breakeven_unchanged():
    """Between +1R and +2R (price 108) the stop stays at breakeven (100), the
    same as before item 142 — the second ratchet has not been earned yet."""
    assert (
        compute_trailing_stop(
            symbol="AAA",
            setup_type="range",
            entry=100.0,
            current_price=108.0,
            current_stop=100.0,
            reference_target=130.0,
            bars=[],
            entry_date=_ENTRY_DAY,
            atr=2.0,
            risk_per_share=abs(100.0 - 95.0),
        )
        is None
    )


def test_range_second_ratchet_never_loosens_a_stop():
    """The stop already sits above +1R (at 106). The +2R trigger is met, but
    the lock (105) would LOWER the stop, so nothing is proposed."""
    assert (
        compute_trailing_stop(
            symbol="AAA",
            setup_type="range",
            entry=100.0,
            current_price=110.0,
            current_stop=106.0,
            reference_target=130.0,
            bars=[],
            entry_date=_ENTRY_DAY,
            atr=2.0,
            risk_per_share=abs(100.0 - 95.0),
        )
        is None
    )


def test_breakout_gets_no_second_ratchet():
    """A breakout (Type B) is UNCHANGED: at +2R with no structure or usable
    chandelier it proposes nothing, proving the second ratchet is Type A only.
    The identical range trade would lock +1R."""
    breakout = compute_trailing_stop(
        symbol="AAA",
        setup_type="breakout",
        entry=100.0,
        current_price=110.0,
        current_stop=100.0,
        reference_target=130.0,
        bars=[],
        entry_date=_ENTRY_DAY,
        atr=None,
        risk_per_share=abs(100.0 - 95.0),
    )
    assert breakout is None
    range_trade = compute_trailing_stop(
        symbol="AAA",
        setup_type="range",
        entry=100.0,
        current_price=110.0,
        current_stop=100.0,
        reference_target=130.0,
        bars=[],
        entry_date=_ENTRY_DAY,
        atr=None,
        risk_per_share=abs(100.0 - 95.0),
    )
    assert range_trade is not None and range_trade.new_stop == 105.0


def test_range_second_ratchet_short_mirror():
    """Short mirror: entry 100, initial stop 105 -> 1R = 5, +2R at price 90,
    lock at +1R = 95. Stop moves from breakeven (100) down to 95."""
    proposal = compute_trailing_stop(
        symbol="AAA",
        setup_type="range",
        entry=100.0,
        current_price=90.0,
        current_stop=100.0,
        reference_target=70.0,
        bars=[],
        entry_date=_ENTRY_DAY,
        atr=2.0,
        risk_per_share=abs(100.0 - 105.0),
        qty=-10,
    )
    assert proposal is not None
    assert proposal.new_stop == 95.0
    assert proposal.source == "second_ratchet"


def test_range_second_ratchet_needs_the_initial_stop():
    """Backward compatibility: with no `initial_stop`, R is unmeasurable, so
    neither ratchet fires — the exact pre-fix behaviour."""
    assert (
        compute_trailing_stop(
            symbol="AAA",
            setup_type="range",
            entry=100.0,
            current_price=110.0,
            current_stop=95.0,
            reference_target=130.0,
            bars=[],
            entry_date=_ENTRY_DAY,
            atr=2.0,
        )
        is None
    )


def test_second_ratchet_multiples_are_the_owner_ratified_values():
    """Pin the owner-appetite numbers (Rex 2026-09-25, item 142): trigger +2R,
    lock +1R."""
    assert RANGE_SECOND_RATCHET_TRIGGER_R == 2.0
    assert RANGE_SECOND_RATCHET_LOCK_R == 1.0
    assert RANGE_SECOND_RATCHET_LOCK_R == RANGE_BREAKEVEN_R_MULTIPLE


# ---------------------------------------------------------------------------
# Type B (breakout) — trail from entry
# ---------------------------------------------------------------------------


def test_breakout_trails_from_entry_without_needing_a_target():
    """A breakout's target is a measured-move reference, not a level anyone
    defends, so trailing IS the management."""
    proposal = compute_trailing_stop(
        symbol="AAA",
        setup_type="breakout",
        entry=100.0,
        current_price=125.0,
        current_stop=95.0,
        reference_target=None,
        bars=_rising_with_higher_lows(),
        entry_date=_ENTRY_DAY,
        atr=2.0,
    )
    assert proposal is not None
    assert proposal.new_stop == 110.0
    assert proposal.source == "structure"


def test_breakout_uses_the_highest_usable_swing_low():
    """Each successive higher low, not the first one found."""
    proposal = compute_trailing_stop(
        symbol="AAA",
        setup_type="breakout",
        entry=100.0,
        current_price=125.0,
        current_stop=99.0,
        reference_target=None,
        bars=_rising_with_higher_lows(),
        entry_date=_ENTRY_DAY,
        atr=2.0,
    )
    assert proposal.new_stop == 110.0  # not 100.0


def _falling_with_lower_lows():
    """The shape the old rule got wrong: confirmed swing lows at 110, then
    104, then 101 — a LOWER-low sequence — with price recovered to 118.

    All three lows sit between a stop at 99 and the current price, so the old
    "highest usable low" rule trailed to 110: a level price had since traded
    straight through, twice, and only climbed back above afterwards.
    """
    lows = [118] * _WARM + [
        118,
        116,
        114,
        110,
        114,
        116,
        118,
        112,
        110,
        108,
        104,
        108,
        110,
        112,
        108,
        106,
        104,
        101,
        104,
        106,
        108,
        118,
    ]
    return _bars([(lo + 2, lo) for lo in lows])


def test_a_lower_low_sequence_gets_no_structural_trail():
    """A broken support level is not support. When the stock is making lower
    lows, structure has offered nothing and the chandelier is the honest
    answer — the old rule trailed to a level price had already gone through."""
    proposal = compute_trailing_stop(
        symbol="AAA",
        setup_type="breakout",
        entry=100.0,
        current_price=118.0,
        current_stop=99.0,
        reference_target=None,
        bars=_falling_with_lower_lows(),
        entry_date=_ENTRY_DAY,
        atr=2.0,
    )
    assert proposal is not None
    assert proposal.source == "chandelier"
    assert proposal.new_stop != 110.0


def test_the_trail_follows_the_most_recent_higher_low_not_the_highest():
    """ "Each successive higher low" means the LATEST one, once it is higher
    than the one before it. On a rising sequence that is also the highest, so
    this is only visible where the two rules disagree — see the lower-low
    case above, which is where they do."""
    proposal = compute_trailing_stop(
        symbol="AAA",
        setup_type="breakout",
        entry=100.0,
        current_price=125.0,
        current_stop=99.0,
        reference_target=None,
        bars=_rising_with_higher_lows(),
        entry_date=_ENTRY_DAY,
        atr=2.0,
    )
    assert proposal.new_stop == 110.0
    assert proposal.source == "structure"


def test_a_single_confirmed_low_is_still_accepted():
    """No sequence to judge yet. This is what a freshly broken-out position
    looks like, and refusing it would remove protection, not add it."""
    lows = [110] * _WARM + [110, 108, 106, 100, 106, 108, 110, 112]
    proposal = compute_trailing_stop(
        symbol="AAA",
        setup_type="breakout",
        entry=99.0,
        current_price=112.0,
        current_stop=95.0,
        reference_target=None,
        bars=_bars([(lo + 2, lo) for lo in lows]),
        entry_date=_ENTRY_DAY,
        atr=2.0,
    )
    assert proposal.source == "structure"
    assert proposal.new_stop == 100.0


def test_short_mirror_a_higher_high_sequence_gets_no_structural_trail():
    """The short's mirror: it trails above successive LOWER highs, so a
    sequence of HIGHER highs offers no structure to trail against."""
    highs = [90] * _WARM + [
        90,
        92,
        94,
        98,
        94,
        92,
        90,
        96,
        98,
        100,
        104,
        100,
        98,
        96,
        100,
        102,
        104,
        107,
        104,
        102,
        100,
        90,
    ]
    proposal = compute_trailing_stop(
        symbol="AAA",
        setup_type="breakout",
        entry=100.0,
        current_price=90.0,
        current_stop=109.0,
        reference_target=None,
        qty=-10,
        bars=_bars([(hi, hi - 2) for hi in highs]),
        entry_date=_ENTRY_DAY,
        atr=2.0,
    )
    assert proposal is not None
    assert proposal.source == "chandelier"
    assert proposal.new_stop != 98.0


def test_chandelier_is_the_fallback_when_structure_is_unclear():
    """A vertical move with no confirmed swing low still gets a trail."""
    straight_up = _bars([(100 + i, 99 + i) for i in range(14)])
    proposal = compute_trailing_stop(
        symbol="AAA",
        setup_type="breakout",
        entry=100.0,
        current_price=113.0,
        current_stop=95.0,
        reference_target=None,
        bars=straight_up,
        entry_date=_ENTRY_DAY,
        atr=1.0,
    )
    assert proposal is not None
    assert proposal.source == "chandelier"
    # highest high 113 - 3 x ATR(1.0) = 110
    assert proposal.new_stop == pytest.approx(113 - CHANDELIER_ATR_MULTIPLE * 1.0)


def test_structure_is_preferred_over_chandelier():
    proposal = compute_trailing_stop(
        symbol="AAA",
        setup_type="breakout",
        entry=100.0,
        current_price=125.0,
        current_stop=95.0,
        reference_target=None,
        bars=_rising_with_higher_lows(),
        entry_date=_ENTRY_DAY,
        atr=2.0,
    )
    assert proposal.source == "structure"


# ---------------------------------------------------------------------------
# Invariants
# ---------------------------------------------------------------------------


def test_a_stop_never_ratchets_down():
    """The single most important property. A trail that can lower a stop is
    not protection, it is a slow-motion loss.

    Swept across a wide range of existing stops: every proposal that comes
    back must sit strictly above the stop it replaces, and a stop already
    above everything the chart offers must produce no proposal at all.
    """
    bars = _rising_with_higher_lows()
    produced_at_least_one = False
    for existing in [80.0, 95.0, 100.0, 105.0, 110.0, 115.0, 118.0, 121.0, 124.0]:
        proposal = compute_trailing_stop(
            symbol="AAA",
            setup_type="breakout",
            entry=100.0,
            current_price=125.0,
            current_stop=existing,
            reference_target=None,
            bars=bars,
            entry_date=_ENTRY_DAY,
            atr=2.0,
        )
        if proposal is not None:
            produced_at_least_one = True
            assert proposal.new_stop > existing, f"stop moved DOWN from {existing} to {proposal.new_stop}"
            assert proposal.previous_stop == existing
    assert produced_at_least_one, "the sweep proved nothing if nothing fired"


def test_a_stop_already_above_every_available_level_produces_nothing():
    """Structure exhausted and the chandelier already cleared — hold, don't
    invent a tighter level to justify an order."""
    assert (
        compute_trailing_stop(
            symbol="AAA",
            setup_type="breakout",
            entry=100.0,
            current_price=125.0,
            current_stop=122.0,  # above the 121 chandelier and every swing low
            reference_target=None,
            bars=_rising_with_higher_lows(),
            entry_date=_ENTRY_DAY,
            atr=2.0,
        )
        is None
    )


def test_a_move_smaller_than_the_ratchet_threshold_is_not_worth_an_order():
    """Otherwise every session nudges the stop a few cents.

    The gate is per CANDIDATE, not per evaluation. Until board item 196 this
    test passed with `atr=2.0` because the structural pivot at 110 failed the
    gate and the chandelier was never built — the rejected first candidate
    ended the search. It is now built, and on these bars it clears both the
    gate and the noise band, so it is rightly taken. Nothing in the
    production record changes: the structural leg has never once produced a
    candidate there, so the chandelier was already the candidate in every
    recorded `below_min_ratchet` refusal.
    """
    stop = 110.0 - venue_tick(110.0) / 2.0

    # Structure alone (no ATR, so no chandelier leg): the gate still refuses.
    assert (
        compute_trailing_stop(
            symbol="AAA",
            setup_type="breakout",
            entry=100.0,
            current_price=125.0,
            current_stop=stop,
            reference_target=None,
            bars=_rising_with_higher_lows(),
            entry_date=_ENTRY_DAY,
            atr=None,
        )
        is None
    )

    # With an ATR the second leg exists and is worth an order on its own.
    proposal = compute_trailing_stop(
        symbol="AAA",
        setup_type="breakout",
        entry=100.0,
        current_price=125.0,
        current_stop=stop,
        reference_target=None,
        bars=_rising_with_higher_lows(),
        entry_date=_ENTRY_DAY,
        atr=2.0,
    )
    assert proposal is not None
    assert proposal.source == "chandelier"
    assert proposal.new_stop >= min_ratchet_floor(stop)


def test_a_stop_is_never_placed_inside_the_atr_noise_band():
    """Same floor the discretionary TRAIL_STOP path clamps to, applied at the
    source instead of after the fact."""
    # ATR 13 => noise floor 125 - 1.25*13 = 108.75; the 110 swing low sits
    # inside it, so no order is worth placing.
    assert (
        compute_trailing_stop(
            symbol="AAA",
            setup_type="breakout",
            entry=100.0,
            current_price=125.0,
            current_stop=95.0,
            reference_target=None,
            bars=_rising_with_higher_lows(),
            entry_date=_ENTRY_DAY,
            atr=13.0,
        )
        is None
    )


def test_no_live_stop_yields_no_proposal():
    """An unprotected position is a repair problem, not a trailing problem —
    inventing a trailing stop here would paper over a missing protective
    order."""
    assert (
        compute_trailing_stop(
            symbol="AAA",
            setup_type="breakout",
            entry=100.0,
            current_price=125.0,
            current_stop=None,
            reference_target=None,
            bars=_rising_with_higher_lows(),
            entry_date=_ENTRY_DAY,
            atr=2.0,
        )
        is None
    )
    assert (
        compute_trailing_stop(
            symbol="AAA",
            setup_type="breakout",
            entry=100.0,
            current_price=125.0,
            current_stop=0.0,
            reference_target=None,
            bars=_rising_with_higher_lows(),
            entry_date=_ENTRY_DAY,
            atr=2.0,
        )
        is None
    )


def test_missing_bars_and_atr_yield_no_proposal_not_a_guess():
    assert (
        compute_trailing_stop(
            symbol="AAA",
            setup_type="breakout",
            entry=100.0,
            current_price=125.0,
            current_stop=95.0,
            reference_target=None,
            bars=[],
            entry_date=_ENTRY_DAY,
            atr=None,
        )
        is None
    )


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), 0.0, -5.0])
def test_non_finite_or_impossible_prices_yield_no_proposal(bad):
    assert (
        compute_trailing_stop(
            symbol="AAA",
            setup_type="breakout",
            entry=bad,
            current_price=125.0,
            current_stop=95.0,
            reference_target=None,
            bars=_rising_with_higher_lows(),
            entry_date=_ENTRY_DAY,
            atr=2.0,
        )
        is None
    )
    assert (
        compute_trailing_stop(
            symbol="AAA",
            setup_type="breakout",
            entry=100.0,
            current_price=bad,
            current_stop=95.0,
            reference_target=None,
            bars=_rising_with_higher_lows(),
            entry_date=_ENTRY_DAY,
            atr=2.0,
        )
        is None
    )


def test_a_stop_is_never_placed_at_or_above_current_price():
    for proposal in (
        compute_trailing_stop(
            symbol="AAA",
            setup_type="breakout",
            entry=100.0,
            current_price=111.0,
            current_stop=95.0,
            reference_target=None,
            bars=_rising_with_higher_lows(),
            entry_date=_ENTRY_DAY,
            atr=0.5,
        ),
    ):
        if proposal is not None:
            assert proposal.new_stop < 111.0


def test_reason_names_when_risk_is_released():
    """Spec 2.3 — once the stop reaches entry the position stops consuming the
    book's risk budget, and the audit trail should say so."""
    proposal = compute_trailing_stop(
        symbol="AAA",
        setup_type="breakout",
        entry=100.0,
        current_price=125.0,
        current_stop=95.0,
        reference_target=None,
        bars=_rising_with_higher_lows(),
        entry_date=_ENTRY_DAY,
        atr=2.0,
    )
    assert proposal.new_stop >= 100.0
    assert "stops consuming risk budget" in proposal.reason


def test_an_unconfirmed_recent_low_is_not_used():
    """A low needs bars on BOTH sides to be confirmed. Trailing under today's
    price is how a stop lands inside the noise band."""
    from src.risk.trailing import _swing_lows

    bars = _bars([(104, 102), (103, 101), (102, 100)])  # too short to confirm
    assert _swing_lows(bars) == []
    # And a monotonic ramp has no local minimum anywhere.
    assert _swing_lows(_bars([(100 + i, 99 + i) for i in range(14)])) == []


# ---------------------------------------------------------------------------
# Item 82 — Type A/B routed on the MEASURED breakout verdict, not the label
# alone (#649/#652 carried construction's `structural_ceiling`; this closes
# the same label-only bug in the trailing reader). `is_trend_trade` ORs the
# analyst's label with the measured `structural_ceiling`: a measured breakout
# the analyst mislabelled "range" (label "range" + structural_ceiling False)
# must be trailed as Type B. A correctly-labelled range (ceiling None on a
# legacy row, or True when a ceiling was measured) and a correctly-labelled
# breakout must be BYTE-IDENTICAL to before this change.
#
# All three share ONE fixture: price 118, below target 130, so the choice is
# purely Type A (breakeven ratchet at entry, 100) vs Type B (structural trail
# to the confirmed higher low, 110).
# ---------------------------------------------------------------------------


def _regime_fixture(setup_type, structural_ceiling):
    return compute_trailing_stop(
        symbol="AAA",
        setup_type=setup_type,
        structural_ceiling=structural_ceiling,
        entry=100.0,
        current_price=118.0,
        current_stop=95.0,
        reference_target=130.0,
        bars=_rising_with_higher_lows(),
        entry_date=_ENTRY_DAY,
        atr=2.0,
        risk_per_share=abs(100.0 - 90.0),
    )


def _regime_fixture_no_structure(setup_type, structural_ceiling):
    """The SAME call, on bars whose confirmed pivot is already below the live
    stop, so the structural leg has nothing to offer and the two regimes can
    be told apart again: Type A falls back to its +1R ratchet, Type B has no
    fallback at all."""
    return compute_trailing_stop(
        symbol="AAA",
        setup_type=setup_type,
        structural_ceiling=structural_ceiling,
        entry=100.0,
        current_price=150.0,
        current_stop=111.0,
        reference_target=130.0,
        bars=_rising_with_higher_lows(),
        entry_date=_ENTRY_DAY,
        atr=20.0,
        risk_per_share=abs(100.0 - 80.0),
    )


def test_item82_regimes_are_still_told_apart_without_a_structural_candidate():
    """Item 212 made both regimes trail on structure from entry, so the
    structural answer alone no longer discriminates. The discrimination that
    REMAINS, and that item 82 exists to protect, is the Type A fallback: with
    no usable structural candidate a range keeps its ratified R-ratchets and a
    breakout has nothing to fall back on."""
    for ceiling in (None, True):
        type_a = _regime_fixture_no_structure("range", ceiling)
        assert type_a is not None, ceiling
        assert type_a.source == "second_ratchet", ceiling
    # Type B: the label, and the mislabelled-range case item 82 fixed.
    assert _regime_fixture_no_structure("breakout", None) is None
    assert _regime_fixture_no_structure("range", False) is None


def test_item82_correct_range_is_unchanged_legacy_null_ceiling():
    """A correctly-labelled range on a legacy row (structural_ceiling absent /
    None) keeps Type A: the +1R breakeven ratchet, stop to entry."""
    # Item 212: Type A now trails on structure from entry too, and the higher
    # low (110) is tighter than the +1R breakeven (100), so structure wins.
    proposal = _regime_fixture("range", None)
    assert proposal is not None
    assert proposal.source == "structure"
    assert proposal.new_stop == 110.0
    # Identical to passing no structural_ceiling at all (the pre-item-82 call).
    legacy = compute_trailing_stop(
        symbol="AAA",
        setup_type="range",
        entry=100.0,
        current_price=118.0,
        current_stop=95.0,
        reference_target=130.0,
        bars=_rising_with_higher_lows(),
        entry_date=_ENTRY_DAY,
        atr=2.0,
        risk_per_share=abs(100.0 - 90.0),
    )
    assert legacy is not None
    assert (legacy.source, legacy.new_stop) == (proposal.source, proposal.new_stop)


def test_item82_correct_range_is_unchanged_when_a_ceiling_was_measured():
    """A correctly-labelled range WITH a measured ceiling (structural_ceiling
    True) stays Type A — is_trend_trade is False, exactly as the label says."""
    # Item 212: structure (110) is tighter than the +1R breakeven (100).
    proposal = _regime_fixture("range", True)
    assert proposal is not None
    assert proposal.source == "structure"
    assert proposal.new_stop == 110.0


def test_item82_correct_breakout_is_unchanged():
    """A correctly-labelled breakout keeps Type B: the structural trail to the
    confirmed higher low, with no measured verdict needed."""
    proposal = _regime_fixture("breakout", None)
    assert proposal is not None
    assert proposal.source == "structure"
    assert proposal.new_stop == 110.0


def test_item82_mislabelled_range_breakout_now_gets_type_b():
    """THE FIX: label "range" but the constructor MEASURED no ceiling
    (structural_ceiling False → is_trend_trade True). Previously this got Type
    A (breakeven ratchet at 100); it must now be trailed as Type B (structural
    trail to 110), identical to the correctly-labelled breakout above."""
    proposal = _regime_fixture("range", False)
    assert proposal is not None
    assert proposal.source == "structure"
    assert proposal.new_stop == 110.0
    # And it matches the correctly-labelled breakout exactly.
    breakout = _regime_fixture("breakout", None)
    assert (proposal.source, proposal.new_stop) == (breakout.source, breakout.new_stop)


def _dated_history(lows, first_day):
    from datetime import timedelta as _td

    return [_Bar(high=lo + 2, low=lo, date=first_day + _td(days=i)) for i, lo in enumerate(lows)]


def test_a_swing_confirmed_after_entry_uses_history_from_before_entry():
    """The structure leg reads the FULL history for its ATR(14) warm-up and
    keeps only swings that printed on or after the entry day (2026-10-15).

    Entry is on day 20; only eight bars follow it — far fewer than the 15 the
    detector would need if it were handed the since-entry slice alone. The
    pre-entry swing low at 95 is ignored; the post-entry low at 104 is used."""
    from datetime import timedelta as _td

    from src.risk.trailing import TRAIL_CODE_TRAILED, evaluate_trailing_stop

    first = _date(2026, 1, 1)
    pre = [110] * 14 + [105, 100, 95, 100, 105, 110]  # swing low 95, day 16
    post = [112, 110, 106, 104, 106, 108, 110, 114]  # swing low 104, day 23
    bars = _dated_history(pre + post, first)
    entry_day = first + _td(days=len(pre))
    assert len([b for b in bars if b.date >= entry_day]) < MIN_BARS_FOR_A_READING
    ev = evaluate_trailing_stop(
        symbol="AAA",
        setup_type="breakout",
        entry=110.0,
        current_price=116.0,
        current_stop=100.0,
        reference_target=None,
        bars=bars,
        entry_date=entry_day,
        atr=None,
    )
    assert ev.code == TRAIL_CODE_TRAILED, ev.code
    assert ev.proposal.source == "structure"
    assert ev.proposal.new_stop == 104.0
    # The since-entry slice alone could not confirm it: the old wiring waited.
    since = [b for b in bars if b.date >= entry_day]
    stale = evaluate_trailing_stop(
        symbol="AAA",
        setup_type="breakout",
        entry=110.0,
        current_price=116.0,
        current_stop=100.0,
        reference_target=None,
        bars=since,
        entry_date=entry_day,
        atr=None,
    )
    assert stale.proposal is None
