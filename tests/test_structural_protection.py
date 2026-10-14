"""`check_structural_protection` / `structural_protection_broken` — spec item
25's data-driven replacement for the flat "days_held < 5" holding-discipline
window (owner decisions, 2026-09-03/04; see `src/risk/exit_guard.py`'s
"Structural (data-driven) holding protection" module note for the full
rationale, corrected same day against real technical-analysis practice).

A position is protected from a plain no-real-trigger SELL/REDUCE/COVER
UNLESS the level backing its thesis has actually been broken by price —
never by elapsed time. Priority order tested below:

  1. `thesis_invalid_if`, checked for real via `check_thesis_invalid_if`.
  2. Absent/unparseable (1): the verified structural level backing the
     stop — level IDENTIFICATION uses the same touch-count/zone rule as
     `PortfolioConstructor._level_backing_stop` (any touch count, and
     the stop falling inside the level's own `CLUSTER_TOLERANCE_PCT`
     zone), no new constant introduced.
  3. Neither resolves (or no data to read either): NOT protected (owner
     mandate 2026-10-09: cut losers fast; the protective stop bounds every
     loss). The ATR noise-band fallback that kept such holdings protected
     was removed.

`current_price` for (1) and (2) MUST be a CLOSING price, never a live
intraday quote — a wick that pierces a level and closes back inside is
noise, not a break (real technical-analysis practice, and the reason this
module's confirmation gate exists at all). Deciding WHETHER a close counts
as "beyond" the level is `BREAK_CONFIRMATION_ATR_MULTIPLE` (the margin
already ratified for "is an adverse move real") — an ATR question. That is
deliberately NOT the level-zone tolerance used to identify which level a
stop sits on, which is a percentage-of-price identity question
(docs/WORK.md item 46).

A break under (1) or (2) must additionally hold on the close of TWO
CONSECUTIVE TRADING DAYS before it lifts protection (owner refinement,
corrected 2026-09-04) — a "spring" (a level breaking then reclaiming the
very next day, often itself a bullish signal) is a real, well-documented
pattern and must not read as an invalidated thesis off one day's close.
(3) is NOT gated by this — it lifts immediately, as do the two independent
regime-flip / bearish-state-change triggers tested in
`test_holding_discipline_guard.py`.

Every number below is hand-computed against the real formulas in
`src/risk/exit_guard.py`, never guessed.
"""

from src.data.levels import CLUSTER_TOLERANCE_PCT
from src.risk.exit_guard import (
    BREAK_CONFIRMATION_ATR_MULTIPLE,
    check_structural_protection,
    level_zone_span_phrase,
    structural_protection_broken,
)

# Level IDENTIFICATION only, and NOT a tunable: this is
# `src.data.levels.CLUSTER_TOLERANCE_PCT`, the same constant
# `find_structural_levels` uses to cluster pivots into a zone, imported here
# rather than restated so the test cannot pass against a stale copy. It
# replaced a 0.25-ATR multiple on 2026-09-13 (docs/WORK.md item 46) — see
# `tests/test_level_match_zone.py` for why an ATR multiple could never stay
# consistent with a percentage-of-price zone.
ZONE_PCT = CLUSTER_TOLERANCE_PCT


# ---------------------------------------------------------------------------
# 1. thesis_invalid_if drives the decision when stated and parseable.
#    Long, entry 100, atr 2, ma_20 98 -> "closes below MA20" triggers at any
#    close below 98.
# ---------------------------------------------------------------------------


def test_thesis_invalid_if_triggered_two_consecutive_closes_lifts_protection():
    """The key behaviour change: a broken thesis CONFIRMED on two
    consecutive trading-day closes lifts protection regardless of how young
    the position is (days_held is not even a parameter any more)."""
    result = check_structural_protection(
        thesis_invalid_if="closes below MA20",
        current_price=95.0,  # today's CLOSE: 95 < 98 -> TRIGGERED
        entry_price=100.0,
        stop_loss=90.0,
        atr=2.0,
        level_cluster_tolerance_pct=ZONE_PCT,
        ma_20=98.0,
        break_seen_prior_close=True,  # yesterday's close was ALSO below MA20
    )
    assert result.raw_broken is True
    assert result.protected is False
    assert result.basis == "thesis_invalid_if_triggered"
    assert (
        structural_protection_broken(
            thesis_invalid_if="closes below MA20",
            current_price=95.0,
            entry_price=100.0,
            stop_loss=90.0,
            atr=2.0,
            level_cluster_tolerance_pct=ZONE_PCT,
            ma_20=98.0,
            break_seen_prior_close=True,
        )
        is True
    )


def test_thesis_invalid_if_triggered_single_close_stays_protected():
    """A break seen for the FIRST time on today's close does not lift
    protection yet — it must still be broken on TOMORROW's close too."""
    result = check_structural_protection(
        thesis_invalid_if="closes below MA20",
        current_price=95.0,
        entry_price=100.0,
        stop_loss=90.0,
        atr=2.0,
        level_cluster_tolerance_pct=ZONE_PCT,
        ma_20=98.0,
        break_seen_prior_close=False,  # no prior confirming close on record
    )
    assert result.raw_broken is True
    assert result.protected is True
    assert result.basis == "thesis_invalid_if_pending_confirmation"


def test_spring_reversal_the_next_day_does_not_lift_protection():
    """THE most important case: day 1 closes below the level, day 2
    reclaims (a textbook 'spring' false-breakdown) -> protection must NEVER
    have lifted, and day 3 breaking again starts the confirmation over from
    scratch rather than instantly firing off the stale day-1 break."""
    day1 = check_structural_protection(
        thesis_invalid_if="closes below MA20",
        current_price=95.0,  # day 1 close: below MA20 -> broken
        entry_price=100.0,
        stop_loss=90.0,
        atr=2.0,
        level_cluster_tolerance_pct=ZONE_PCT,
        ma_20=98.0,
        break_seen_prior_close=False,
    )
    assert day1.raw_broken is True
    assert day1.protected is True  # single close never lifts protection

    day2 = check_structural_protection(
        thesis_invalid_if="closes below MA20",
        current_price=99.0,  # day 2 close: reclaimed above MA20 (98.0)
        entry_price=100.0,
        stop_loss=90.0,
        atr=2.0,
        level_cluster_tolerance_pct=ZONE_PCT,
        ma_20=98.0,
        break_seen_prior_close=day1.raw_broken,
    )
    assert day2.raw_broken is False
    assert day2.protected is True
    assert day2.basis == "thesis_invalid_if_intact"

    day3 = check_structural_protection(
        thesis_invalid_if="closes below MA20",
        current_price=95.0,  # breaks again on day 3
        entry_price=100.0,
        stop_loss=90.0,
        atr=2.0,
        level_cluster_tolerance_pct=ZONE_PCT,
        ma_20=98.0,
        break_seen_prior_close=day2.raw_broken,  # False -> resets
    )
    assert day3.protected is True
    assert day3.basis == "thesis_invalid_if_pending_confirmation"


def test_thesis_invalid_if_not_triggered_stays_protected_indefinitely():
    """The other half of the behaviour change: an intact thesis stays
    protected no matter how long the position has been held — there is no
    days_held parameter to time it out with any more."""
    result = check_structural_protection(
        thesis_invalid_if="closes below MA20",
        current_price=105.0,  # close above MA20 -> NOT_TRIGGERED
        entry_price=100.0,
        stop_loss=90.0,
        atr=2.0,
        level_cluster_tolerance_pct=ZONE_PCT,
        ma_20=98.0,
        break_seen_prior_close=True,  # even a stale confirmed break can't matter here
    )
    assert result.protected is True
    assert result.basis == "thesis_invalid_if_intact"
    assert result.raw_broken is False


# ---------------------------------------------------------------------------
# 2. Structural level backing the stop, when thesis_invalid_if is absent or
#    unparseable — level IDENTIFICATION as `_level_backing_stop`, break
#    MARGIN as `BREAK_CONFIRMATION_ATR_MULTIPLE` (1.0), not the level-zone tolerance.
# ---------------------------------------------------------------------------


def test_structural_level_broken_two_consecutive_closes_lifts_protection():
    # Long: entry 100, stop 90, atr 2. Level IDENTIFICATION tolerance =
    # 0.25 * 2 = 0.5 -> a verified level at 90.3 (gap 0.3 <= 0.5) with 6
    # touches (>= 5) backs the stop. Break MARGIN = BREAK_CONFIRMATION_ATR_MULTIPLE
    # (1.0) * atr (2) = 2.0 -> broken when close <= level - margin = 88.3.
    assert BREAK_CONFIRMATION_ATR_MULTIPLE == 1.0
    result = check_structural_protection(
        thesis_invalid_if=None,
        current_price=88.0,  # <= 88.3 -> decisively broken
        entry_price=100.0,
        stop_loss=90.0,
        atr=2.0,
        is_short=False,
        computed_levels=[90.3],
        computed_level_touches={90.3: 6},
        # Item 215: backing is "the stop rests on a bar that DREW the level",
        # so the fixture states the bar. 90.3 was turned at by a session that
        # traded 90.0-90.6, and the 90.00 stop sits inside that session's own
        # range — the level cannot be taken out without the stop being hit.
        computed_level_bars={90.3: [(90.0, 90.6)]},
        level_cluster_tolerance_pct=ZONE_PCT,
        break_seen_prior_close=True,
    )
    assert result.raw_broken is True
    assert result.protected is False
    assert result.basis == "structural_level_broken"


def test_structural_level_broken_single_close_stays_protected():
    result = check_structural_protection(
        thesis_invalid_if=None,
        current_price=88.0,
        entry_price=100.0,
        stop_loss=90.0,
        atr=2.0,
        is_short=False,
        computed_levels=[90.3],
        computed_level_touches={90.3: 6},
        # Item 215: backing is "the stop rests on a bar that DREW the level",
        # so the fixture states the bar. 90.3 was turned at by a session that
        # traded 90.0-90.6, and the 90.00 stop sits inside that session's own
        # range — the level cannot be taken out without the stop being hit.
        computed_level_bars={90.3: [(90.0, 90.6)]},
        level_cluster_tolerance_pct=ZONE_PCT,
        break_seen_prior_close=False,
    )
    assert result.raw_broken is True
    assert result.protected is True
    assert result.basis == "structural_level_pending_confirmation"


def test_small_close_below_level_within_break_margin_is_not_a_break():
    """A close that dips modestly below the level but stays inside the
    BREAK_CONFIRMATION_ATR_MULTIPLE margin is exactly the same shape as an
    intrabar wick that closes back inside it — real trading practice does
    not call this a break, and neither does this function. This also
    covers the "intraday wick closing back inside on the same day never
    counts" case: the function only ever sees a close, and a close this
    close to the level is indistinguishable from — and treated the same
    as — a wick that recovered."""
    result = check_structural_protection(
        thesis_invalid_if=None,
        current_price=90.0,  # AT the level, well inside the 2.0 margin -> not broken
        entry_price=100.0,
        stop_loss=90.0,
        atr=2.0,
        is_short=False,
        computed_levels=[90.3],
        computed_level_touches={90.3: 6},
        # Item 215: backing is "the stop rests on a bar that DREW the level",
        # so the fixture states the bar. 90.3 was turned at by a session that
        # traded 90.0-90.6, and the 90.00 stop sits inside that session's own
        # range — the level cannot be taken out without the stop being hit.
        computed_level_bars={90.3: [(90.0, 90.6)]},
        level_cluster_tolerance_pct=ZONE_PCT,
        break_seen_prior_close=True,  # even a stale prior break can't matter — not broken now
    )
    assert result.raw_broken is False
    assert result.protected is True
    assert result.basis == "structural_level_intact"


def test_structural_level_intact_stays_protected_at_30_days_equivalent():
    """The key behaviour change, on the level path this time: intact
    structure protects a position with no time limit at all."""
    result = check_structural_protection(
        thesis_invalid_if=None,
        current_price=95.0,  # well above the level -> intact
        entry_price=100.0,
        stop_loss=90.0,
        atr=2.0,
        is_short=False,
        computed_levels=[90.3],
        computed_level_touches={90.3: 6},
        # Item 215: backing is "the stop rests on a bar that DREW the level",
        # so the fixture states the bar. 90.3 was turned at by a session that
        # traded 90.0-90.6, and the 90.00 stop sits inside that session's own
        # range — the level cannot be taken out without the stop being hit.
        computed_level_bars={90.3: [(90.0, 90.6)]},
        level_cluster_tolerance_pct=ZONE_PCT,
        break_seen_prior_close=False,
    )
    assert result.protected is True
    assert result.basis == "structural_level_intact"


def test_structural_level_broken_short_side_mirrors_long():
    # Short: entry 100, stop 110, atr 2. Identification tolerance 0.5 ->
    # verified resistance at 109.8 (gap 0.2 <= 0.5, 6 touches). Break
    # margin 2.0 -> broken when close >= level + margin = 111.8.
    result = check_structural_protection(
        thesis_invalid_if=None,
        current_price=112.0,  # >= 111.8 -> broken
        entry_price=100.0,
        stop_loss=110.0,
        atr=2.0,
        is_short=True,
        computed_levels=[109.8],
        computed_level_touches={109.8: 6},
        computed_level_bars={109.8: [(109.4, 110.0)]},
        level_cluster_tolerance_pct=ZONE_PCT,
        break_seen_prior_close=True,
    )
    assert result.protected is False
    assert result.basis == "structural_level_broken"


def test_unparseable_thesis_falls_back_to_structural_level():
    """An UNPARSEABLE thesis_invalid_if must not be treated as protected or
    broken by default — it falls through to the level check exactly as if
    no thesis had been stated at all."""
    result = check_structural_protection(
        thesis_invalid_if="RSI drops below 30 and MACD crosses down",  # compound -> UNPARSEABLE
        current_price=88.0,
        entry_price=100.0,
        stop_loss=90.0,
        atr=2.0,
        computed_levels=[90.3],
        computed_level_touches={90.3: 6},
        # Item 215: backing is "the stop rests on a bar that DREW the level",
        # so the fixture states the bar. 90.3 was turned at by a session that
        # traded 90.0-90.6, and the 90.00 stop sits inside that session's own
        # range — the level cannot be taken out without the stop being hit.
        computed_level_bars={90.3: [(90.0, 90.6)]},
        level_cluster_tolerance_pct=ZONE_PCT,
        break_seen_prior_close=True,
    )
    assert result.basis == "structural_level_broken"
    assert result.protected is False


# ---------------------------------------------------------------------------
# 3. No thesis_invalid_if and no qualifying level -> NOT protected, immediately
#    (owner mandate 2026-10-09). No chart level never refuses a cut.
# ---------------------------------------------------------------------------


def _no_basis(**over):
    kwargs = dict(
        thesis_invalid_if=None,
        current_price=99.0,
        entry_price=100.0,
        stop_loss=90.0,
        atr=2.0,
        computed_levels=[],
        computed_level_touches={},
        level_cluster_tolerance_pct=ZONE_PCT,
    )
    kwargs.update(over)
    return check_structural_protection(**kwargs)


def test_no_basis_small_loss_is_not_protected():
    """A loss of half an ATR used to sit inside the fallback band and stay
    protected; with no chart level backing the thesis it now never is."""
    result = _no_basis()
    assert result.protected is False
    assert result.basis == "no_chart_level"
    assert result.raw_broken is False
    assert result.confirmed_chart_break is False


def test_no_basis_large_loss_is_not_protected():
    result = _no_basis(current_price=94.0, break_seen_prior_close=False)
    assert result.protected is False
    assert result.basis == "no_chart_level"


def test_no_basis_in_profit_stays_protected():
    """A WINNER with no chart level stays protected, so a rotation cannot sell
    it on that ground (owner mandate: take profit early only sideways)."""
    result = _no_basis(current_price=105.0)
    assert result.protected is True
    assert result.basis == "no_adverse_move_from_entry"
    assert result.confirmed_chart_break is False


def test_no_basis_flat_stays_protected_and_short_mirrors():
    assert _no_basis(current_price=100.0).protected is True
    short_winning = _no_basis(current_price=95.0, stop_loss=110.0, is_short=True)
    assert short_winning.protected is True
    assert short_winning.basis == "no_adverse_move_from_entry"
    short_losing = _no_basis(current_price=101.0, stop_loss=110.0, is_short=True)
    assert short_losing.protected is False
    assert short_losing.basis == "no_chart_level"


def test_no_basis_missing_data_is_never_protected_by_default():
    """Missing data never manufactures protection — and the case is visible
    via `basis`/`detail`, not a silent no-op."""
    result = _no_basis(current_price=None, entry_price=None, stop_loss=None, atr=None)
    assert result.protected is False
    assert result.basis == "no_chart_level"
    assert "no verified structural level" in result.detail


def test_low_touch_level_still_backs_the_stop():
    """2026-10-14: the touch bar is deleted. A real level with 2 prior
    touches backs the stop exactly as a 5-touch one does (same rule as
    `_level_backing_stop`), so the position is protected by that level."""
    result = _no_basis(
        computed_levels=[90.3],
        computed_level_touches={90.3: 2},
        computed_level_bars={90.3: [(90.0, 90.6)]},
    )
    assert result.basis == "structural_level_intact"
    assert result.protected is True


def test_confirmed_chart_break_is_recorded_only_on_a_confirmed_break():
    assert (
        check_structural_protection(
            thesis_invalid_if=None,
            current_price=99.0,
            entry_price=100.0,
            stop_loss=95.0,
            atr=2.0,
            computed_levels=[95.0],
            computed_level_touches={95.0: 5},
            computed_level_zones={95.0: [94.5, 95.5]},
            computed_level_bars={95.0: [(94.5, 95.5)]},
            level_cluster_tolerance_pct=1.0,
        ).confirmed_chart_break
        is False
    )


# --- item 215: every "still backed by a level" claim carries the span ----


def _intact_check(**over):
    kwargs = dict(
        thesis_invalid_if=None,
        current_price=99.0,
        entry_price=100.0,
        stop_loss=95.0,
        atr=2.0,
        computed_levels=[95.0],
        computed_level_touches={95.0: 5},
        computed_level_zones={95.0: [94.5, 95.5]},
        computed_level_bars={95.0: [(94.5, 95.5)]},
        level_cluster_tolerance_pct=1.0,
    )
    kwargs.update(over)
    return check_structural_protection(**kwargs)


def test_intact_level_claim_carries_measured_zone_span():
    """docs/WORK.md item 215 (a). A position reported as still backed by a
    level must say how wide that level's MEASURED zone is, so the owner can
    see how precise the claim is."""
    check = _intact_check()
    assert check.protected is True
    assert check.basis == "structural_level_intact"
    assert "measured zone 94.5-95.5" in check.detail
    assert "span 1" in check.detail
    assert "% of the level price" in check.detail


def test_intact_level_claim_says_span_unknown_rather_than_substituting():
    """No zone and no bar ranges means the span is UNKNOWN; the claim says so
    rather than computing a percentage-of-price stand-in. The level is still
    only honoured where the code's own bound honours it, so the bars are kept
    for the match and only the reporting zone map is emptied."""
    check = _intact_check(computed_level_zones={})
    assert "measured zone 94.5-95.5" in check.detail

    no_span = level_zone_span_phrase(95.0, {}, {})
    assert no_span == "measured zone span NOT RECORDED for this level"
