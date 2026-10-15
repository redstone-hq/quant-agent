"""The structure leg of the deterministic trail: confirmed swing pivots.

Moved verbatim out of `src/risk/trailing.py` (2026-10-04) so the arithmetic
can be built and exercised alone. The doctrine, the numbers and the result
types stay in `src/risk/trailing.py`; read its module docstring first.
"""

from __future__ import annotations

from src.data.levels import turning_points

__all__ = [
    "_swing_lows",
    "_swing_highs",
    "_swing_lows_dated",
    "_swing_highs_dated",
    "_structural_pivot",
]


def _swing_lows(bars) -> list[float]:
    """Confirmed swing lows, oldest first.

    Read from `src.data.levels.turning_points`, the one detector (owner
    ruling, Rex 2026-10-15): a low is confirmed only once price has since
    risen from it by at least the stock's own ATR(14), read at that low's
    bar. The latest, still-unconfirmed candidate is never returned. That lag
    is the point: an unconfirmed low is just today's price, and trailing
    under today's price is how a stop ends up inside the noise band.

    **Measured 2026-09-30, live production DB: this function has never once
    produced a stop.** All nine deterministic trails ever placed came from the
    chandelier fallback (eight) or the Type A breakeven ratchet (one); the
    structural leg has contributed zero. The cause is arithmetic, not a bug:
    the then 3-bar window needed 7 bars before a single low can be confirmed,
    and this desk's positions were 4-9 sessions old when evaluated, with a
    scale-in additionally resetting the caller's bar window to zero until
    the caller was changed to slice from the position open instead of the
    latest add. Since 13 Oct the caller (`src/exits_parts/trails.py`) takes
    the entry day from the broker fill time of the current holding's first
    opening fill, and both legs read from that one day (unknown: no reading).

    **That change is not risk-free, and it was described as such in error.**
    A longer window can only raise `highest`, so it can only raise
    `chandelier = highest - CHANDELIER_ATR_MULTIPLE * ATR`; a candidate that
    rises through the noise floor makes `evaluate_trailing_stop` return
    `TRAIL_CODE_INSIDE_NOISE_BAND` outright, with no fallback to a lower
    level the shorter window would have accepted. Price 100, ATR 4, live stop
    90: a 106 high proposes 94 and the stop tightens; a 108 high proposes 96,
    above the 95 floor, and the stop stays at 90. The wider window can LOSE a
    tighten. The justification is that the old window disagreed by
    construction with the blended `avg_entry` price used in the same call —
    not that the change cannot cost protection.

    **Since then the candidate search was widened, which narrows that
    exposure without moving any number.** `evaluate_trailing_stop` used to
    build the chandelier only when structure produced nothing, so it fixed on
    one candidate BEFORE testing it; it now builds both legs and carries each
    through the same invariants, taking the first that survives. A structural
    pivot inside the noise band therefore no longer suppresses a chandelier
    level that clears it. This does NOT rescue the case above, where the
    chandelier is itself the offending candidate — there is no lower
    already-derived level to fall back to, and synthesising one at the band's
    own edge would trail under today's price, which is the very thing
    `_swing_lows` refuses on principle.

    **The exposure was then MEASURED, not assumed.** Re-running all 21
    recorded refusals through both windows (live DB, 2026-09-30): only 5 have
    a window start that moves at all — every one of them MRVL, the only
    position whose adds fall on different sessions; META's two adds are the
    same session, and the other four names never scaled in. In all 5 the
    verdict is unchanged and NOT ONE lands inside the noise band that did
    not before. Newly-refused-as-inside-noise-band: ZERO.

    The 7-bar floor is what binds in 20 of the 21, but not in all of them,
    and the PR first claimed otherwise. The widest new window (MRVL,
    2026-09-29) holds 8 bars and CLEARS the floor — it still produces no
    pivot, because the eight lows rise almost monotonically and neither of
    the two eligible centre bars is a strict local minimum. Confirmed pivots
    found under the new window: ZERO, same as the old. "Flips none" survives;
    "the 7-bar floor is the only reason" does not.

    So on today's holding periods the chandelier IS the trail, and the
    "trail under each successive higher low" rule in the module docstring
    describes an intent rather than observed behaviour. The measurements
    above were taken under the deleted 3-bar window; since 2026-10-15 the
    confirmation is the ATR reversal below, which needs the ATR's own
    warm-up inside the bars handed in, so they do not carry over.
    """
    return [price for _, price in _swing_pivots_dated(bars, highs=False)]


def _swing_highs(bars) -> list[float]:
    """Confirmed swing highs, oldest first — the short's mirror of `_swing_lows`.

    A high is confirmed only once price has since fallen from it by at least
    the stock's own ATR(14), read at that high's bar; the unconfirmed latest
    candidate is never returned, for the same reason as the long side.
    """
    return [price for _, price in _swing_pivots_dated(bars, highs=True)]


def _swing_pivots_dated(bars, *, highs: bool) -> list[tuple[object, float]]:
    """`_swing_lows` / `_swing_highs` with each pivot's BAR DATE kept.

    The same detector (`turning_points`). The date is the session the extreme
    PRINTED on — `bar.date`, or None when the bar carries none — never the
    later session on which it became confirmed. Only the trend exit
    (`src.risk.alignment_exit`) needs to know WHEN a swing printed, so it can
    ignore a lower low the position never lived through.
    """
    bars = list(bars or [])
    want = "H" if highs else "L"
    return [(getattr(bars[i], "date", None), price) for i, price, kind in turning_points(bars) if kind == want]


def _swing_lows_dated(bars) -> list[tuple[object, float]]:
    """Confirmed swing lows as (bar date, low), oldest first. See `_swing_pivots_dated`."""
    return _swing_pivots_dated(bars, highs=False)


def _swing_highs_dated(bars) -> list[tuple[object, float]]:
    """Confirmed swing highs as (bar date, high), oldest first. See `_swing_pivots_dated`."""
    return _swing_pivots_dated(bars, highs=True)


def _structural_pivot(pivots: list[float], *, is_short: bool) -> float | None:
    """The one pivot this module is entitled to trail against, or None.

    The rule this module states is "trail under each successive HIGHER low"
    (mirror: above each successive LOWER high). What the code did for a long
    was take the highest confirmed low sitting between the stop and price —
    which is a different rule, and on a stock making LOWER lows it is the
    wrong one. Example, all real shapes this desk holds: entry 100, stop 90,
    confirmed lows 95 then 92 then 91, price back at 98. The old code trailed
    to 95 — a level price had since traded straight through down to 91 and
    only recovered above afterwards. A support level that has been broken is
    not support; the sequence is making lower lows, so structure has not
    offered a trail at all and the chandelier fallback below is the honest
    answer.

    So: the pivot is the MOST RECENT confirmed one, and it counts only when
    it is genuinely higher than the pivot before it (a real higher low).
    Where only ONE pivot is confirmed there is no sequence to judge and it is
    accepted on its own — that is the pre-existing behaviour, it is what a
    freshly-broken-out position looks like, and tightening it would remove
    protection rather than add it.

    Returns None when structure gives no answer. The caller falls through to
    the chandelier, which is exactly what "where structure is unclear" in the
    module docstring means.
    """
    if not pivots:
        return None
    latest = pivots[-1]
    if len(pivots) == 1:
        return latest
    previous = pivots[-2]
    if is_short:
        # A short trails above successive LOWER highs.
        return latest if latest < previous else None
    return latest if latest > previous else None
