"""Trend-scaled break confirmation, lifted verbatim from exit_guard.py."""

from __future__ import annotations

from typing import TYPE_CHECKING
from src.risk.exit_guard_deltas import _finite

if TYPE_CHECKING:
    from src.risk.exit_guard import StructuralProtectionCheck


# ---------------------------------------------------------------------------
# TREND-SCALED break confirmation
# (owner mandate 2026-09-24; citation-backed spec — named TA authorities)
# ---------------------------------------------------------------------------
#
# A support/resistance break is confirmed by a decisive CLOSE beyond the level
# by the break margin (`BREAK_CONFIRMATION_ATR_MULTIPLE`, 1.0 ATR — the SAME margin for
# every regime, never a second ATR multiple), held over TWO consecutive closes
# with a reclaim in between resetting (the ratified spring floor). The owner
# mandate (2026-09-24) makes the desk MORE PATIENT only where a break is most
# likely a shakeout; it never lifts protection faster than that floor. Two
# regimes, both grounded, monotonic (nothing exits faster than the floor):
#
#   STRONG-WITH-TREND — a break WITH a strong uptrend (ADX>=25 AND price above a
#     RISING 200-day MA AND 50MA>200MA; short mirror): the shakeout-most-likely
#     case. Require the two-close floor AND that the PRIOR structural swing-low
#     has ALSO closed broken before lifting protection (Sperandeo 1-2-3
#     failed-retest; Bulkowski retest rate). Most patient.
#   DEFAULT — everything else (against the trend, weak/tangled tape, or no trend
#     data): the EXISTING ratified floor — two consecutive confirmed closes
#     beyond 1.0 ATR, reclaim resets (Edwards & Magee two-consecutive-day close
#     filter; Wyckoff spring).
#
# There is deliberately NO single-close path: lifting protection on one close
# would reverse the ratified two-close spring rule and add whipsaw. DIRECTION is
# Weinstein/Dow: price vs a RISING/FALLING 200-day MA (the SLOPE, so a real top
# is not read as "still uptrend") plus the 50/200 stack. STRENGTH is Wilder's
# ADX — only the single >=25 "strong" threshold is used, to ENTER the patient
# regime. The 25 threshold and the 14-session ADX period GATE behaviour, so they
# are trade-governing and sourced to Wilder (1978).
#
# Board item 70, 2026-09-26: this margin used to BE the noise-band constant.
# It is now its own name, `BREAK_CONFIRMATION_ATR_MULTIPLE`, at the same 1.0
# value and with no behaviour change — see that constant for why the published
# literature gives this quantity no ATR basis at all.

#: Consecutive confirmed daily closes beyond the level required to treat ANY
#: break as real. SOURCED: the two-consecutive-day close filter in Edwards &
#: Magee, Technical Analysis of Stock Trends — a level breaks on a decisive
#: close, confirmed on the next day's close, and a reclaim in between resets
#: (Wyckoff spring). Applies in BOTH regimes; the strong-with-trend regime adds
#: a STRUCTURAL condition (the prior swing-low must also break) on top, never a
#: larger close count.
TREND_CONFIRMING_CLOSES = 2

#: Wilder's ADX reading at or above which a trend is treated as STRONG (New
#: Concepts in Technical Trading Systems, 1978; the standard convention that
#: ADX>25 is a trend strong enough to trade with). GATES behaviour: at/above
#: this, a with-trend break enters the patient strong regime (two closes AND the
#: prior swing-low must also break). Below it, the default two-close floor.
ADX_STRONG_TREND_THRESHOLD = 25.0


#: The confirmation REGIME labels `classify_trend_context` returns. Each maps
#: to a sourced confirmation rule in `_break_confirmation_settings`.
REGIME_STRONG_WITH_TREND = "with_trend_strong"
REGIME_DEFAULT = "default"


def classify_trend_context(
    *,
    is_short: bool,
    current_price: float | None,
    ma_50: float | None,
    ma_200: float | None,
    ma_200_prior: float | None = None,
    adx: float | None = None,
) -> str:
    """Classify an adverse structural break into a confirmation REGIME, per the
    owner mandate 2026-09-24 (trend-scaled exit) and its citation-backed spec.
    Exit speed scales with how strongly the break aligns with the prevailing
    trend. Pure and side-aware.

    The "adverse break" is the one that would lift protection: for a LONG,
    price falling through support; for a SHORT, price rising through resistance.

    DIRECTION (Weinstein stage analysis; Dow theory). For a LONG, "with a strong
    trend" (an uptrend an adverse dip is likely a shakeout WITHIN) means price
    ABOVE a RISING 200-day MA *and* the 50MA above the 200MA. Anything else —
    price below a falling 200MA, 50MA<200MA, flat/tangled MAs, or no MA data — is
    the DEFAULT regime. For a SHORT it mirrors: strong-with-trend is price below
    a FALLING 200MA and 50MA<200MA (a strong downtrend). Using the 200MA SLOPE
    (not the level alone) is what stops a real top being read as "still an
    uptrend": once the 200MA rolls over, the position is no longer with-trend.

    STRENGTH (Wilder 1978, ADX): only the single >=`ADX_STRONG_TREND_THRESHOLD`
    (25) "strong" test is used, to ENTER the patient regime.

    Returns one of:
      - REGIME_STRONG_WITH_TREND — break with a STRONG uptrend (ADX>=25 and the
                                   structure above). The shakeout-most-likely
                                   case: the two-close floor AND the prior
                                   structural swing-low must also break.
      - REGIME_DEFAULT           — everything else (against/weak/tangled, or no
                                   trend data): the ratified two-consecutive-
                                   close floor, reclaim resets. Nothing exits
                                   faster than this.
    """
    price = _finite(current_price)
    m50 = _finite(ma_50)
    m200 = _finite(ma_200)
    a = _finite(adx)
    if price is None or m50 is None or m200 is None or a is None:
        # No direction/strength to read -> the ratified two-close floor.
        return REGIME_DEFAULT

    m200_prev = _finite(ma_200_prior)
    rising_200 = m200_prev is not None and m200 > m200_prev
    falling_200 = m200_prev is not None and m200 < m200_prev

    if is_short:
        # A short is "with a strong trend" in a strong DOWNtrend: price below a
        # falling 200MA and 50<200.
        with_trend = (price < m200) and (m50 < m200) and falling_200
    else:
        # A long is "with a strong trend" in an uptrend: price above a rising
        # 200MA and 50>200.
        with_trend = (price > m200) and (m50 > m200) and rising_200

    if with_trend and a >= ADX_STRONG_TREND_THRESHOLD:
        return REGIME_STRONG_WITH_TREND
    return REGIME_DEFAULT


def _break_confirmation_settings(trend_context: str) -> tuple[int, bool]:
    """Map a regime label to `(confirming_closes_needed, requires_prior_low)`.

    The break MARGIN is ALWAYS `BREAK_CONFIRMATION_ATR_MULTIPLE` (1.0 ATR) —
    there is no second, wider ATR multiple. Both regimes use the same two-close floor;
    the strong-with-trend regime adds the prior-swing-low structural condition,
    never a larger close count. There is no single-close path.

      - REGIME_STRONG_WITH_TREND -> 2 consecutive closes AND the prior
        structural swing-low must also close broken (Sperandeo 1-2-3
        failed-retest; Bulkowski retest rate).
      - REGIME_DEFAULT -> `TREND_CONFIRMING_CLOSES` (2) consecutive closes
        (Edwards & Magee two-consecutive-day close filter; reclaim = spring).
    """
    if trend_context == REGIME_STRONG_WITH_TREND:
        return TREND_CONFIRMING_CLOSES, True
    return TREND_CONFIRMING_CLOSES, False


def _prior_structural_level_broken(
    computed_levels: list | None,
    broken_level: float,
    cur: float,
    margin: float,
    *,
    is_short: bool,
) -> bool:
    """Regime 3's structural patience test: has the PRIOR swing point beyond the
    broken level ALSO closed broken?

    For a LONG the broken level is a swing higher-low; the prior swing-low is
    the NEXT structural level below it (from the desk's existing item-55
    `find_structural_levels` output, passed in as `computed_levels`). The strong
    uptrend's structure is only confirmed broken once price has ALSO closed
    below THAT level by the same margin. For a SHORT it mirrors upward.

    Returns True (the gate is satisfied / vacuous) when there is no prior
    structural level beyond the broken one — regime 3 then falls back to the
    plain two-consecutive-close confirmation rather than holding forever.

    NOTE (honest limitation): there is NO volume in the exit path, so this is
    not Wyckoff's volume-confirmed spring; the reclaim-resets-protection test
    elsewhere plus this prior-low break are the structural substitutes, weaker
    than a volume read.
    """
    lvls = [v for v in (_finite(x) for x in (computed_levels or [])) if v is not None]
    if is_short:
        prior = min((x for x in lvls if x > broken_level), default=None)
        if prior is None:
            return True
        return cur >= prior + margin
    prior = max((x for x in lvls if x < broken_level), default=None)
    if prior is None:
        return True
    return cur <= prior - margin


def _trend_clause(is_short: bool, trend_context: str) -> str:
    """The plain-language trend-context phrase for the owner message, correct
    for the position's side. Empty in the default regime."""
    if trend_context == REGIME_STRONG_WITH_TREND:
        return "while it's still in a strong downtrend" if is_short else "while it's still in a strong uptrend"
    return ""  # default regime — omit the trend clause


def _compose_owner_break_reason(
    *,
    is_short: bool,
    trigger_desc: str,
    trend_context: str,
    confirmed: bool,
    awaiting_prior_low: bool = False,
) -> str:
    """Assemble the plain-language, owner-facing reason clause for a decisive
    break outcome. Pure and side-aware; states the trigger, the trend context
    and the confirmation state in words. `render_owner_break_message` wraps a
    symbol and lead verb around this for the Telegram/board surfaces.

    Truthful about what the GATE did (it removes the hold-protection veto), not
    what the desk will do: a default confirmed break reads as a "real
    breakdown"; a break held with a strong trend reads as a "possible shakeout"
    the desk is waiting on.
    """
    trend = _trend_clause(is_short, trend_context)
    trend_suffix = f" {trend}" if trend else ""
    if confirmed:
        if trend_context == REGIME_STRONG_WITH_TREND:
            return (
                f"{trigger_desc}{trend_suffix}, and the prior swing-low has now "
                f"broken too, so even the strong trend's structure has failed — "
                f"a real breakdown, confirmed over two closes."
            )
        return f"{trigger_desc}{trend_suffix}, confirmed over two closes — a real breakdown, not noise."
    # Pending confirmation — the desk is HOLDING through the break and waiting.
    if trend_context == REGIME_STRONG_WITH_TREND and awaiting_prior_low:
        return (
            f"{trigger_desc}{trend_suffix}, so this looks like a possible "
            f"shakeout; holding until the prior swing-low also breaks."
        )
    if trend_context == REGIME_STRONG_WITH_TREND:
        return (
            f"{trigger_desc}{trend_suffix}, so this looks like a possible "
            f"shakeout; holding one more session for a confirming close."
        )
    return (
        f"{trigger_desc}{trend_suffix}, but the break isn't confirmed yet; "
        f"holding one more session to rule out a one-day false breakdown."
    )


def owner_break_clause(reason: str) -> str:
    """The reason clause joined to its subject: "it <reason>", except when the
    reason already opens with its own subject ("its stated exit condition was
    met ...", "it ...") — prefixing those gave the owner "it its stated exit
    condition". Pure."""
    text = (reason or "").strip()
    if not text:
        return ""
    if text.split(None, 1)[0].lower().rstrip(",") in ("it", "its"):
        return text
    return f"it {text}"


def render_owner_break_message(
    symbol: str,
    check: "StructuralProtectionCheck",
) -> str | None:
    """The full owner-facing sentence for a decisive structural-protection
    outcome, or None when there is nothing decisive to voice.

    Reused by the pipeline to push the SAME sentence to both owner surfaces —
    the Telegram alert and the dashboard/board journal. Pure: no I/O.

    Lead verb states only what the gate DID, never desk intent: a confirmed
    break REMOVES the hold-protection veto (protected=False lifts the veto only;
    the actual sale still runs the other exit gates, and for a rotation is
    contingent on a replacement buy). A pending break KEEPS the veto. It never
    says or implies the position was sold.
    """
    reason = (check.owner_reason or "").strip()
    if not reason:
        return None
    sym = (symbol or "").strip().upper() or "this position"
    clause = owner_break_clause(reason)
    if not check.protected:
        # Confirmed break — the hold-protection veto is removed (not a sale).
        return f"{sym}: hold-protection lifted — {clause}"
    # Break held pending confirmation — the veto stays on.
    return f"{sym}: hold-protection kept — {clause}"
