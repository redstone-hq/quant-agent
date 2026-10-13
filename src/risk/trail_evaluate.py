"""The deterministic trail's evaluator: both legs, every invariant, one verdict.

Moved verbatim out of `src/risk/trailing.py` (2026-10-04) so the arithmetic
can be built and exercised alone. The doctrine, the numbers and the result
types stay in `src/risk/trailing.py`; read its module docstring first.
`compute_trailing_stop` and `evaluate_trailing_stop` remain reachable as
`src.risk.trailing.compute_trailing_stop` / `evaluate_trailing_stop`.
"""

from __future__ import annotations

from dataclasses import replace as _replace

from src.risk.trail_range_ratchet import _range_breakeven_ratchet, _range_second_ratchet
from src.risk.trail_structure import _structural_pivot, _swing_highs, _swing_lows
from src.risk.trail_tick import (
    MIN_RATCHET_TICKS,
    min_ratchet_floor,
    venue_tick,
)
from src.risk.trailing import (
    CHANDELIER_ATR_MULTIPLE,
    MIN_BARS_FOR_A_READING,
    NOISE_BAND_ATR_MULTIPLE,
    TRAIL_CODE_BAD_PRICE_INPUT,
    TRAIL_CODE_BELOW_MIN_RATCHET,
    TRAIL_CODE_INSIDE_NOISE_BAND,
    TRAIL_CODE_NO_CANDIDATE,
    TRAIL_CODE_NO_LIVE_STOP,
    TRAIL_CODE_ROUNDED_OFF_SIDE,
    TRAIL_CODE_TOO_FEW_BARS,
    TRAIL_CODE_TRAILED,
    TrailEvaluation,
    TrailProposal,
    _finite,
)

__all__ = ["compute_trailing_stop", "evaluate_trailing_stop"]


def compute_trailing_stop(
    *,
    symbol: str,
    setup_type: str | None,
    entry: float,
    current_price: float,
    current_stop: float | None,
    reference_target: float | None,
    bars=None,
    atr: float | None = None,
    min_ratchet_ticks: int = MIN_RATCHET_TICKS,
    qty: float = 1.0,
    structural_ceiling: bool | None = None,
    risk_per_share: float | None = None,
) -> TrailProposal | None:
    """Propose a tightened stop, or None when no move is warranted.

    `bars` are the daily bars SINCE ENTRY (the caller slices them); only those
    matter, because a swing low/high from before the position existed is not
    a level this trade ever defended.

    `qty` supplies only the SIDE.
    A negative qty is a short: everything below mirrors across the entry
    price. A long's stop lives BELOW price and ratchets UP toward it as the
    trade works; a short's stop lives ABOVE price and ratchets DOWN toward it.
    Defaults to +1.0 so every existing (long-only) call site is unchanged.

    `risk_per_share` is 1R per held share from the per-lot function
    (`quantities.held_position_risk` / held qty) — used only by the Type A
    R ratchets. None (R unknown) means those ratchets never fire.

    `structural_ceiling` is the constructor's MEASURED breakout verdict,
    pinned at entry (item 82, #649/#652). Passing it routes a measured
    breakout the analyst mislabelled "range" to Type B trailing instead of
    Type A — see `src.risk.constants.is_trend_trade`. `None` (the default,
    and every legacy row) falls back to the analyst's label alone, which is
    the exact behaviour this argument replaces.
    """
    return evaluate_trailing_stop(
        symbol=symbol,
        setup_type=setup_type,
        entry=entry,
        current_price=current_price,
        current_stop=current_stop,
        reference_target=reference_target,
        bars=bars,
        atr=atr,
        min_ratchet_ticks=min_ratchet_ticks,
        qty=qty,
        structural_ceiling=structural_ceiling,
        risk_per_share=risk_per_share,
    ).proposal


def evaluate_trailing_stop(
    *,
    symbol: str,
    setup_type: str | None,
    entry: float,
    current_price: float,
    current_stop: float | None,
    reference_target: float | None,
    bars=None,
    atr: float | None = None,
    min_ratchet_ticks: int = MIN_RATCHET_TICKS,
    qty: float | None = None,  # None reads as a long — see `is_short` below
    structural_ceiling: bool | None = None,
    risk_per_share: float | None = None,
) -> TrailEvaluation:
    """`compute_trailing_stop`, plus the code naming why it ended where it
    did. Same arguments, same arithmetic, same proposal — this IS the body;
    the other is its one-field view. See `compute_trailing_stop` for the
    argument contract."""

    ent = _finite(entry)
    cur = _finite(current_price)
    stop = _finite(current_stop) if current_stop is not None else None
    atr_f = _finite(atr) if atr is not None else None

    if ent is None or cur is None or ent <= 0 or cur <= 0:
        return TrailEvaluation(None, TRAIL_CODE_BAD_PRICE_INPUT)
    if stop is None or stop <= 0:
        # No live stop means the position is unprotected, which is a repair
        # problem, not a trailing problem. Inventing a trailing stop here
        # would paper over a missing protective order.
        return TrailEvaluation(None, TRAIL_CODE_NO_LIVE_STOP)

    is_short = (_finite(qty) or 1.0) < 0

    # Type A vs Type B is the SAME breakout verdict every other money-path
    # reader now uses: the analyst's label OR the constructor's MEASURED
    # `structural_ceiling` — either sufficient (item 82, #649/#652). A
    # measured breakout the analyst mislabelled "range" (setup_type="range"
    # with structural_ceiling=False → is_trend_trade True) is trailed as
    # Type B, not left in Type A. `structural_ceiling=None` (legacy row, or
    # a caller that cannot measure it) falls back to the label alone —
    # identical to the pre-item-82 `!= "breakout"` compare this replaces.
    from src.risk.constants import is_trend_trade

    # --- Type A: the R-ratchets, then the SAME structural trail as Type B --
    # Item 212: the structural/chandelier trail used to be GATED behind the
    # recorded take-profit target, so between entry and that target a range
    # position had only its original entry stop and nothing followed price
    # up. The target is an unsourced number, it never reaches the broker as
    # an order, and gating this trail was its only live behaviour — so the
    # gate is removed rather than re-derived or replaced (the owner's
    # ratified answer to "when do we sell" is the alignment exit, which is
    # already live). No multiple is widened and no new constant appears.
    #
    # The two ratified R-multiple ratchets are UNCHANGED and still run
    # first; the structural trail is now simply also allowed to run, and
    # whichever of the two proposes the TIGHTER stop wins. Both legs only
    # ever ratchet toward less risk, so nothing here can move a stop away
    # from price.
    range_fallback: TrailEvaluation | None = None
    if not is_trend_trade(setup_type, structural_ceiling=structural_ceiling):
        # Try the higher-protection +2R -> lock-+1R step first (item 142); if
        # price has not reached +2R it returns no proposal and the +1R ->
        # breakeven step (fix #3) decides. Both fail closed without an
        # initial stop, and neither ever loosens a stop.
        second = _range_second_ratchet(
            symbol=symbol,
            ent=ent,
            cur=cur,
            stop=stop,
            risk_per_share=risk_per_share,
            is_short=is_short,
            setup_type=setup_type,
        )
        range_fallback = (
            second
            if second.proposal is not None
            else (
                _range_breakeven_ratchet(
                    symbol=symbol,
                    ent=ent,
                    cur=cur,
                    stop=stop,
                    risk_per_share=risk_per_share,
                    is_short=is_short,
                    setup_type=setup_type,
                )
            )
        )

    def _clears_invariants(level: float) -> str | None:
        """The minimum-ratchet and noise-band tests, as one function so that
        EVERY leg able to place a stop is held to them. The R-ratchets skip
        them when they answer alone, which is ratified and unchanged — but a
        ratchet level is only allowed to REACH the broker on a Type A name
        once this says yes, because the alternative is placing a stop inside
        the very daily-noise band the structural leg was just refused for.
        Returns the refusal code, or None when the level is placeable."""
        floor = min_ratchet_floor(stop, is_short=is_short, min_ratchet_ticks=min_ratchet_ticks)
        # Half-a-tick tolerance is the float<->Decimal round-trip this repo
        # already allows in `_prices_match`, not a threshold: a level that
        # quantizes ONTO the floor is a real one-tick improvement.
        _slack = venue_tick(stop) / 2.0
        if is_short:
            if level > floor + _slack:
                return TRAIL_CODE_BELOW_MIN_RATCHET
        elif level < floor - _slack:
            return TRAIL_CODE_BELOW_MIN_RATCHET
        if atr_f is not None and atr_f > 0:
            if is_short:
                if level < cur + NOISE_BAND_ATR_MULTIPLE * atr_f:
                    return TRAIL_CODE_INSIDE_NOISE_BAND
            elif level > cur - NOISE_BAND_ATR_MULTIPLE * atr_f:
                return TRAIL_CODE_INSIDE_NOISE_BAND
        return None

    def _or_range(ev: "TrailEvaluation") -> "TrailEvaluation":
        """The structural leg found nothing usable: fall back to whatever the
        ratified R-ratchets proposed, which is exactly what this function
        returned for a Type A position before item 212. The structural leg's
        OWN code travels with the answer in `structural_code`, so
        `inside_noise_band`, `rounded_candidate_not_between_stop_and_price`
        and `no_structure_and_no_usable_chandelier` remain recordable for a
        range name instead of being overwritten by the ratchet's code."""
        if range_fallback is None:
            return ev
        if range_fallback.proposal is None:
            return _replace(range_fallback, structural_code=ev.code)
        # The ratchet leg is about to place a stop, so it is held to the same
        # invariants as the structural leg. Without this, a structural
        # candidate refused as `inside_noise_band` would hand the decision
        # straight to a ratchet level that was never band-checked — placing a
        # stop inside the band the structural leg had just been refused for,
        # which is exactly the "trailed into its own range" failure the old
        # target gate was really protecting against.
        _refusal = _clears_invariants(range_fallback.proposal.new_stop)
        if _refusal is not None:
            return TrailEvaluation(None, _refusal, structural_code=ev.code)
        return _replace(range_fallback, structural_code=ev.code)

    # --- Candidate SET: structure first, chandelier second -----------------
    # BOTH legs are now always built. Before this change the chandelier was
    # computed only `if candidate is None`, so the module committed to the
    # structural pivot BEFORE testing it — and a pivot that the invariants
    # below then rejected (most sharply the noise band) silently suppressed a
    # chandelier level that would have passed every one of them. Committing
    # to the first candidate before testing it is a defect in how the
    # candidate is FOUND, not a reason to drop a leg.
    #
    # Nothing about the preference order or the arithmetic changes: structure
    # is still tried first, the chandelier is still second, no new multiple or
    # threshold is introduced, and each candidate is carried through exactly
    # the SAME invariants as before. The first candidate that survives all of
    # them is used. There is deliberately NO synthesised third candidate at
    # the noise band's own edge: a level read off today's price is a pure
    # price-follower, which is a different exit rule from the ratified one and
    # needs an argued decision, not a quiet patch here.
    _usable_bars = [b for b in (bars or []) if _finite(getattr(b, "low" if is_short else "high", None)) is not None]
    if len(_usable_bars) < MIN_BARS_FOR_A_READING:
        return _or_range(TrailEvaluation(None, TRAIL_CODE_TOO_FEW_BARS))

    candidates: list[tuple[float, str]] = []
    if is_short:
        pivot = _structural_pivot(_swing_highs(bars or []), is_short=True)
        # The pivot is only usable if it is BELOW the current stop and ABOVE
        # current price: above the stop is not a ratchet, below the price is
        # not a stop.
        if pivot is not None and cur < pivot < stop:
            candidates.append((pivot, "structure"))
    else:
        pivot = _structural_pivot(_swing_lows(bars or []), is_short=False)
        # Mirror: only a pivot ABOVE the current stop and BELOW current price
        # is usable — below the stop is not a ratchet, above the price is not
        # a stop.
        if pivot is not None and stop < pivot < cur:
            candidates.append((pivot, "structure"))

    if atr_f is not None and atr_f > 0:
        # Missing data must not produce an action. With no usable bar the
        # extreme used to fall back to CURRENT PRICE, which makes the
        # chandelier `price - 3 x ATR`: a pure price-follower read off today's
        # print, exactly the synthesised candidate the comment above refuses.
        # It fires on an entry-day position and on EVERY bar-fetch failure
        # (the caller leaves `bars` empty on an exception), so it would
        # tighten a stop off nothing. No bar -> no chandelier, and the refusal
        # is recorded as `no_bars_since_entry`.
        if is_short:
            lows = [_finite(getattr(b, "low", None)) for b in (bars or [])]
            lows = [l for l in lows if l is not None]
            if lows:
                chandelier = min(lows) + CHANDELIER_ATR_MULTIPLE * atr_f
                if cur < chandelier < stop:
                    candidates.append((chandelier, "chandelier"))
        else:
            highs = [_finite(getattr(b, "high", None)) for b in (bars or [])]
            highs = [h for h in highs if h is not None]
            if highs:
                chandelier = max(highs) - CHANDELIER_ATR_MULTIPLE * atr_f
                if stop < chandelier < cur:
                    candidates.append((chandelier, "chandelier"))

    if not candidates:
        return _or_range(TrailEvaluation(None, TRAIL_CODE_NO_CANDIDATE))

    # --- Invariants --------------------------------------------------------
    # Ratchet toward less risk only, and only when the move is worth an order,
    # and never inside one ordinary day's range of current price. Applied per
    # candidate. The refusal reported when every candidate fails is the FIRST
    # candidate's, which keeps the recorded reason identical to what this
    # module produced before whenever only one leg offered anything at all —
    # which is every refusal in the production record to date.
    candidate: float | None = None
    source = ""
    first_refusal: str | None = None
    for _cand, _source in candidates:
        _refusal = _clears_invariants(_cand)
        if _refusal is None:
            candidate, source = _cand, _source
            break
        if first_refusal is None:
            first_refusal = _refusal

    if candidate is None:
        return _or_range(TrailEvaluation(None, first_refusal or TRAIL_CODE_NO_CANDIDATE))

    candidate = round(candidate, 2)
    if is_short:
        if candidate >= stop or candidate <= cur:
            return _or_range(TrailEvaluation(None, TRAIL_CODE_ROUNDED_OFF_SIDE))
    else:
        if candidate <= stop or candidate >= cur:
            return _or_range(TrailEvaluation(None, TRAIL_CODE_ROUNDED_OFF_SIDE))

    # Item 212: a Type A position can now have BOTH a ratchet proposal and a
    # structural one. Take the tighter — never the looser, and never a step
    # away from price.
    if range_fallback is not None and range_fallback.proposal is not None:
        _r = range_fallback.proposal.new_stop
        if (_r < candidate) if is_short else (_r > candidate):
            # The two R-ratchets deliberately skip the minimum-ratchet and
            # noise-band invariants (their docstrings say so, and that is
            # ratified behaviour this item does not touch). But PREFERRING a
            # ratchet level over a structural candidate that DID clear those
            # invariants would be new behaviour: it could place a stop inside
            # the very noise band the structural leg was just refused for.
            # So the override is allowed only when the ratchet level clears
            # the same two invariants. When it does not, the structural
            # candidate stands — which is never worse than the pre-item-212
            # answer, since before this item the structural leg could not run
            # here at all. The ratchet's own unconditional path is untouched:
            # with no structural candidate it still answers alone, exactly as
            # it did before.
            if _clears_invariants(_r) is None:
                return _replace(range_fallback, structural_code=TRAIL_CODE_TRAILED)

    locked = ""
    if is_short:
        if candidate <= ent:
            locked = " — at or below entry, so this position stops consuming risk budget"
    else:
        if candidate >= ent:
            locked = " — at or above entry, so this position stops consuming risk budget"
    return TrailEvaluation(
        TrailProposal(
            symbol=symbol.upper(),
            new_stop=candidate,
            previous_stop=stop,
            source=source,
            reason=(
                f"deterministic trail ({source}): {setup_type or 'unknown'} setup, "
                f"stop ${stop:.2f} -> ${candidate:.2f} with price ${cur:.2f}"
                f"{locked}"
            ),
        ),
        TRAIL_CODE_TRAILED,
    )
