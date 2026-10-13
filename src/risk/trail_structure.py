"""The structure leg of the deterministic trail: confirmed swing pivots.

Moved verbatim out of `src/risk/trailing.py` (2026-10-04) so the arithmetic
can be built and exercised alone. The doctrine, the numbers and the result
types stay in `src/risk/trailing.py`; read its module docstring first.
"""

from __future__ import annotations

from src.risk.trailing import PIVOT_WINDOW, _finite

__all__ = [
    "_swing_lows",
    "_swing_highs",
    "_swing_lows_dated",
    "_swing_highs_dated",
    "_structural_pivot",
]


def _swing_lows(bars, window: int = PIVOT_WINDOW) -> list[float]:
    """Confirmed swing lows, oldest first.

    A low is confirmed only when `window` bars on BOTH sides are higher, so the
    most recent `window` bars can never produce one. That lag is the point: an
    unconfirmed low is just today's price, and trailing under today's price is
    how a stop ends up inside the noise band.

    **Measured 2026-09-30, live production DB: this function has never once
    produced a stop.** All nine deterministic trails ever placed came from the
    chandelier fallback (eight) or the Type A breakeven ratchet (one); the
    structural leg has contributed zero. The cause is arithmetic, not a bug:
    `window * 2 + 1` = 7 bars are needed before a single low can be confirmed,
    and this desk's positions were 4-9 sessions old when evaluated, with a
    scale-in additionally resetting the caller's bar window to zero until
    `src/pipeline.py::_apply_deterministic_trails` was changed to slice from
    the POSITION OPEN (`Database.get_position_open_timestamp`) instead of the
    latest add.

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
    describes an intent rather than observed behaviour. Shortening
    `PIVOT_WINDOW` would make structure fire, and that is precisely why it
    has not been done: the constant is documented above as unsourceable in
    the literature, and moving it to obtain a result the data would like is
    picking a number.
    """
    lows: list[float] = []
    n = len(bars)
    if n < window * 2 + 1:
        return lows
    values = [_finite(getattr(b, "low", None)) for b in bars]
    for i in range(window, n - window):
        centre = values[i]
        if centre is None:
            continue
        neighbourhood = [v for v in values[i - window : i + window + 1] if v is not None]
        if len(neighbourhood) < window + 1:
            continue
        if centre <= min(neighbourhood):
            lows.append(centre)
    return lows


def _swing_highs(bars, window: int = PIVOT_WINDOW) -> list[float]:
    """Confirmed swing highs, oldest first — the short's mirror of `_swing_lows`.

    A high is confirmed only when `window` bars on BOTH sides are lower, so
    the most recent `window` bars can never produce one, for the same lag
    reason as the long side: an unconfirmed high is just today's price, and
    trailing above today's price is how a short's stop ends up inside the
    noise band.
    """
    highs: list[float] = []
    n = len(bars)
    if n < window * 2 + 1:
        return highs
    values = [_finite(getattr(b, "high", None)) for b in bars]
    for i in range(window, n - window):
        centre = values[i]
        if centre is None:
            continue
        neighbourhood = [v for v in values[i - window : i + window + 1] if v is not None]
        if len(neighbourhood) < window + 1:
            continue
        if centre >= max(neighbourhood):
            highs.append(centre)
    return highs


def _swing_pivots_dated(bars, window: int, *, highs: bool) -> list[tuple[object, float]]:
    """`_swing_lows` / `_swing_highs` with each pivot's BAR DATE kept.

    The same confirmation rule, line for line: a pivot is confirmed only when
    `window` bars on both sides are higher (lower, for highs). The date is the
    session the extreme PRINTED on — `bar.date`, or None when the bar carries
    none — never the later session on which it became confirmed.

    A separate function, not a change to `_swing_lows` / `_swing_highs`: the
    trailing stop calls those and must not change behaviour. Only the trend
    exit (`src.risk.alignment_exit`) needs to know WHEN a swing printed, so it
    can ignore a lower low the position never lived through.
    """
    out: list[tuple[object, float]] = []
    n = len(bars)
    if n < window * 2 + 1:
        return out
    values = [_finite(getattr(b, "high" if highs else "low", None)) for b in bars]
    for i in range(window, n - window):
        centre = values[i]
        if centre is None:
            continue
        neighbourhood = [v for v in values[i - window : i + window + 1] if v is not None]
        if len(neighbourhood) < window + 1:
            continue
        if (centre >= max(neighbourhood)) if highs else (centre <= min(neighbourhood)):
            out.append((getattr(bars[i], "date", None), centre))
    return out


def _swing_lows_dated(bars, window: int = PIVOT_WINDOW) -> list[tuple[object, float]]:
    """Confirmed swing lows as (bar date, low), oldest first. See `_swing_pivots_dated`."""
    return _swing_pivots_dated(bars, window, highs=False)


def _swing_highs_dated(bars, window: int = PIVOT_WINDOW) -> list[tuple[object, float]]:
    """Confirmed swing highs as (bar date, high), oldest first. See `_swing_pivots_dated`."""
    return _swing_pivots_dated(bars, window, highs=True)


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
