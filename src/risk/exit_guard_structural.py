"""Two structural-protection helpers, lifted verbatim out of `src/risk/exit_guard.py`.

That module re-exports both so every caller and test keeps its name.
"""

from __future__ import annotations

import math


def _consecutive_prior_break_count(
    prior_break_records: list | None,
    prior_session_dates: list | None,
    *,
    clears,
) -> int:
    """Count how many CONSECUTIVE prior trading-day closes confirm a break,
    walking back from the most recent completed session.

    Fixes two cross-day confirmation defects in the raw-streak approach:

      - ADJACENCY (#3). `prior_session_dates` is the exact sequence of
        completed trading sessions strictly before today's close, most-recent
        first, taken from the position's own daily bars (the authoritative
        trading calendar — it already skips weekends and holidays). A prior
        broken close counts toward the streak ONLY if it sits on the session
        immediately preceding the last-counted one. A session with no stored
        read, or a stored read that is not broken, ENDS the streak — so a gap
        (Friday's break then the following Friday's, with the week between
        never confirming) can never chain into a false confirmation.

      - MARGIN CONFLATION (#4). `clears(record)` decides whether that day's
        stored close cleared the margin IN FORCE UNDER TODAY's classification
        — not merely whether it was flagged broken at the time under whatever
        (possibly looser) margin then applied. A close that only cleared a
        looser margin does not count.

    Pure: it reads the records the caller supplies and returns a count. Returns
    0 when the caller supplies no records/sessions (the caller then falls back
    to the legacy boolean shorthand).
    """
    if not prior_break_records or not prior_session_dates:
        return 0
    by_date: dict[str, dict] = {}
    for r in prior_break_records:
        d = str(r.get("bar_date") or "").strip()
        # First-seen wins; the caller supplies at most one row per session, but
        # if duplicates arrive keep the earliest in iteration order.
        if d and d not in by_date:
            by_date[d] = r
    count = 0
    for d in prior_session_dates:
        r = by_date.get(str(d))
        if r is None or not r.get("raw_broken") or not clears(r):
            break
        count += 1
    return count


def _structural_level_backing_stop(
    *,
    entry_price: float,
    stop_loss: float,
    is_short: bool,
    computed_levels: list | None,
    computed_level_zones: dict | None = None,
    computed_level_bars: dict | None = None,
) -> float | None:
    """The verified structural level nearest `stop_loss`, or None.

    Exact same matching rule as
    `PortfolioConstructor._level_backing_stop` (side-correctness relative
    to entry, any real level at any touch count since 2026-10-14, closest
    level whose own ZONE contains the stop) — reimplemented here as a free function, over
    the same plain data, because that method lives on a class this module
    must not import (it would be a risk module depending on the
    constructor, backwards from every other dependency in this codebase)
    and because the bars it reads off `self.cfg` are passed in here
    directly by the caller instead. Any behavioural drift between the two
    would be a bug; there is deliberately only one set of numbers (the
    caller's), never a second one invented here.

    No percentage band is used: the stop must rest on a bar that drew the
    level (its own measured range). The old `level_cluster_tolerance_pct`
    (1%) parameter was deleted 2026-10-15 (docs/NUMBER_TRIAGE.md).
    """
    bars_by_price = computed_level_bars or {}
    best: float | None = None
    best_gap = float("inf")
    for raw in computed_levels or []:
        try:
            price = float(raw)
        except (TypeError, ValueError):
            continue
        if not math.isfinite(price) or price <= 0:
            continue
        if is_short and price < entry_price:
            continue
        if not is_short and price > entry_price:
            continue
        # "AT this level", not "inside its band" — docs/WORK.md item 215.
        # The stop must lie inside the high-low range of at least one BAR
        # that drew the level. The whole merged zone can span a fifth of the price, so a stop at one
        # end of it could be taken out with the level never broken and the
        # desk still reporting the position structurally protected. Missing
        # bar ranges fail closed to not-backed, same as an unmatched stop.
        # Mirrors `src.data.levels.stop_rests_on_level`, restated here in
        # full only because this module imports nothing.
        # OUTWARD BOUND, mirroring `src.data.levels.stop_rests_on_level`: the
        # level's measured zone must be STRICTLY NARROWER than the trade's own
        # risk. Membership in a forming bar alone has no ceiling — the zone
        # edges ARE bar extremes — so without this a stop could be reported
        # level-backed a fifth of the price away from the level the break
        # check then evaluates. Fail closed when the risk is unusable.
        stop_distance = abs(entry_price - stop_loss)
        if not math.isfinite(stop_distance) or stop_distance <= 0:
            continue
        ranges = []
        for rng in bars_by_price.get(price) or ():
            try:
                low, high = float(rng[0]), float(rng[1])
            except (TypeError, ValueError, IndexError):
                continue
            if not (math.isfinite(low) and math.isfinite(high)) or low > high:
                continue
            ranges.append((low, high))
        if not ranges:
            continue
        if (max(h for _, h in ranges) - min(l for l, _ in ranges)) >= stop_distance:
            continue
        rests = False
        for rng in ranges:
            try:
                low, high = float(rng[0]), float(rng[1])
            except (TypeError, ValueError, IndexError):
                continue
            if not (math.isfinite(low) and math.isfinite(high)) or low > high:
                continue
            if low <= stop_loss <= high:
                rests = True
                break
        gap = abs(stop_loss - price)
        if rests and gap < best_gap:
            best, best_gap = price, gap
    return best
