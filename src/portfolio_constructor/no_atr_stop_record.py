"""Counted rows for every no-ATR structural stop placement (board item 90).

`ConstructorConfig.structural_stop_buffer_pct` (0.5%) is filed `arbitrary`:
owner appetite, not doctrine. Its only consumer is
`StopRules._derive_structural_stop_no_atr`, the branch the desk takes when a
name arrives with no ATR(14) reading and the protective stop has to be read
off price STRUCTURE instead (owner ruling 2026-09-25, board item 80).

Nothing has ever counted that branch, so two questions about it cannot be
answered from the record at all:

  * How OFTEN does it run? The ledger note says the branch is NEVER
    EXERCISED on the committed daily-bar fixture (1197 windows, 7 symbols,
    an ATR in all of them) -- which is a statement about a fixture, not
    about the live desk.
  * When it runs, is the level's MEASURED cluster half-width present? That
    half-width is the only candidate denominator that could replace the flat
    percentage with something read off the name itself. If it is routinely
    absent, the flat percentage stays and the ledger says so.

NOTHING CHANGES BEHAVIOUR HERE. `noted` is handed the placement the caller
already decided and hands it straight back; `nothing_found` returns None,
which is exactly what the caller returns without it. No stop moves, no
level is re-chosen, no buffer is re-expressed. The 2026-10-04 ruling that
money numbers be expressed per stock is NOT applied to this buffer here:
re-expressing it tightens the stop on a quiet name, protection changes are
one-way, and doing that on a path whose frequency nobody has measured is
the trap this module exists to close.

THE ROUTE, NAMED HONESTLY. There is no stored field for this. Each row is
one `logger.info` line on the portfolio-constructor package logger
(`src.portfolio_constructor.config.logger` -- the same handle
`stops.py` itself logs through, and the same handle
`absolute_floor_record.py` writes its `ABS_FLOOR_ROW` rows through),
carrying a stable `NO_ATR_STOP_ROW ` prefix and a JSON payload. `rows_from`
parses those lines back and `summarise` computes every total from them at
read time. There is deliberately NO module-level tally and no reset: a
running count is stored bookkeeping, it cannot be re-sliced or audited, and
it would let one session inherit another's numbers.

Three states stay distinguishable, which is the whole point:

  * no row at all                 -- the branch was NEVER REACHED
  * a row with outcome `level`    -- tier 1 placed the stop off a verified
                                     computed structural level
  * a row with outcome `prior_bar` -- tier 2 placed it off the signal bar
  * a row with outcome `none`     -- the branch ran and found no usable
                                     structure on the protective side
"""

from __future__ import annotations

import json
import math

from src.portfolio_constructor.config import logger

__all__ = [
    "OUTCOME_LEVEL",
    "OUTCOME_NONE",
    "OUTCOME_PRIOR_BAR",
    "ROW_TAG",
    "emit_row",
    "recorder",
    "rows_from",
    "summarise",
    "zone_halfwidth",
]

#: Stable prefix every counted row carries, so the rows can be found in a
#: log without guessing at wording.
ROW_TAG = "NO_ATR_STOP_ROW "

#: Tier 1 -- the stop was read off a verified computed structural level.
OUTCOME_LEVEL = "level"
#: Tier 2 -- the stop was read off the signal (last completed) bar's edge.
OUTCOME_PRIOR_BAR = "prior_bar"
#: Neither tier yielded a usable level on the protective side of entry.
OUTCOME_NONE = "none"


def _finite(value) -> float | None:
    """`value` as a finite float, or None. None is never a stand-in zero."""
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def zone_halfwidth(analysis, level) -> tuple[float | None, bool]:
    """Half of the MEASURED span of the cluster that formed `level`.

    Returns `(halfwidth, measured)`. `measured` is False whenever the
    analysis carries no usable span for that level -- which is itself the
    finding the ledger row waits on, because a half-width that is routinely
    absent cannot replace the flat percentage. Read off
    `TechAnalysisResult.computed_level_zones`, the level's own
    `[low, high]` span; no percentage fallback is applied here, because a
    percentage of the price is the very thing being questioned.
    """
    if level is None:
        return None, False
    zones = getattr(analysis, "computed_level_zones", None)
    if not isinstance(zones, dict):
        return None, False
    span = zones.get(level)
    if not isinstance(span, (list, tuple)) or len(span) != 2:
        return None, False
    low = _finite(span[0])
    high = _finite(span[1])
    if low is None or high is None or high < low:
        return None, False
    return (high - low) / 2.0, True


def signal_bar_range(analysis) -> float | None:
    """The signal bar's high-minus-low, or None when it is not readable.

    The second candidate per-name denominator, observed and never applied.
    None -- never zero -- for a missing, non-finite or non-positive span, so
    a reader can tell "no denominator" from "a denominator of nothing".
    """
    high = _finite(getattr(analysis, "signal_bar_high", None))
    low = _finite(getattr(analysis, "signal_bar_low", None))
    if high is None or low is None:
        return None
    span = high - low
    return span if span > 0 else None


def build_row(
    *,
    outcome: str,
    analysis,
    is_short: bool,
    entry_price,
    buffer_pct,
    level=None,
    stop_price=None,
    touches=None,
    candidate_levels: int = 0,
) -> dict:
    """The payload for ONE placement decision. Pure; holds no state."""
    entry = _finite(entry_price)
    lvl = _finite(level)
    stop = _finite(stop_price)
    halfwidth, measured = zone_halfwidth(analysis, level)
    stop_distance = None
    if entry is not None and stop is not None:
        stop_distance = abs(entry - stop)
    buffer_distance = None
    if lvl is not None and stop is not None:
        buffer_distance = abs(lvl - stop)
    bar_range = signal_bar_range(analysis)
    buffer_in_bar_ranges = None
    if buffer_distance is not None and bar_range:
        buffer_in_bar_ranges = buffer_distance / bar_range
    buffer_in_halfwidths = None
    if buffer_distance is not None and halfwidth:
        buffer_in_halfwidths = buffer_distance / halfwidth
    return {
        "outcome": outcome,
        "symbol": getattr(analysis, "symbol", None),
        "direction": "short" if is_short else "long",
        "entry_price": entry,
        "buffer_pct": _finite(buffer_pct),
        "level": lvl,
        "stop_price": stop,
        "stop_distance": stop_distance,
        "buffer_distance": buffer_distance,
        "level_zone_halfwidth": halfwidth,
        "level_zone_halfwidth_measured": bool(measured),
        "buffer_in_level_halfwidths": buffer_in_halfwidths,
        "signal_bar_range": bar_range,
        "buffer_in_bar_ranges": buffer_in_bar_ranges,
        "level_touches": touches,
        "candidate_levels": int(candidate_levels),
    }


def emit_row(row: dict) -> str:
    """Write one counted row to the package logger and return it as text."""
    text = json.dumps(row, sort_keys=True, default=str)
    logger.info("%s%s", ROW_TAG, text)
    return text


def rows_from(lines) -> list[dict]:
    """Every counted row in `lines`, in order. Unparseable rows are skipped."""
    out: list[dict] = []
    for line in lines:
        _head, sep, tail = str(line).partition(ROW_TAG)
        if not sep:
            continue
        try:
            out.append(json.loads(tail.strip()))
        except ValueError:
            continue
    return out


def summarise(rows) -> dict[str, object]:
    """Count the rows and read their spread back; nothing is stored.

    `halfwidth_readings` over `placements` is the live answer to the
    ledger's open question -- how often a MEASURED per-name denominator is
    even available at placement. None, never a stand-in number, while
    nothing has been seen.
    """
    rows = list(rows)
    by_outcome: dict[str, int] = {}
    for row in rows:
        key = str(row.get("outcome"))
        by_outcome[key] = by_outcome.get(key, 0) + 1
    placements = by_outcome.get(OUTCOME_LEVEL, 0) + by_outcome.get(OUTCOME_PRIOR_BAR, 0)
    halfwidths = [
        float(row["level_zone_halfwidth"])
        for row in rows
        if row.get("level_zone_halfwidth_measured") and isinstance(row.get("level_zone_halfwidth"), (int, float))
    ]
    ratios = [
        float(row["buffer_in_level_halfwidths"])
        for row in rows
        if isinstance(row.get("buffer_in_level_halfwidths"), (int, float))
    ]
    return {
        "branch_entries": len(rows),
        "placements": placements,
        "by_outcome": by_outcome,
        "halfwidth_readings": len(halfwidths),
        "narrowest_halfwidth_seen": min(halfwidths) if halfwidths else None,
        "widest_halfwidth_seen": max(halfwidths) if halfwidths else None,
        "mean_halfwidth_seen": (sum(halfwidths) / len(halfwidths)) if halfwidths else None,
        "buffer_in_halfwidths_readings": len(ratios),
        "mean_buffer_in_halfwidths": (sum(ratios) / len(ratios)) if ratios else None,
    }


def recorder(analysis, is_short, entry_price, buffer_pct):
    """A one-call row writer bound to ONE no-ATR derivation.

    Positional deliberately: the caller is the single site inside
    `StopRules._derive_structural_stop_no_atr`, which the file-size ratchet
    holds at its current width, so the wiring there is as narrow as it can
    be and the naming lives here.

    The returned callable takes the caller's own `(level, stop, rule)` --
    already decided, recomputed nowhere here -- and RETURNS IT UNCHANGED, or
    returns None when it is handed None. So a recording failure can never
    cost a name its stop, and the genuine skip case still skips.
    """

    def _write(outcome, placement=None, touches=None, candidate_levels=0):
        level, stop_price = (None, None) if placement is None else placement[:2]
        emit_row(
            build_row(
                outcome=outcome,
                analysis=analysis,
                is_short=is_short,
                entry_price=entry_price,
                buffer_pct=buffer_pct,
                level=level,
                stop_price=stop_price,
                touches=touches,
                candidate_levels=candidate_levels,
            )
        )
        return placement

    return _write
