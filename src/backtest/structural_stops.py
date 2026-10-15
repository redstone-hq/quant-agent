"""Structural stop and target resolution for the backtest engine.

Moved out of ``engine`` unchanged so that module can stop growing: the setup
label for a signal and the stop/target taken from structural levels.
``engine`` re-exports both names.
"""

from __future__ import annotations

from src.backtest.swept_values import SweepMeter
from src.data import levels as levels_module
from src.data.context import compute_market_context
from src.data.levels import find_structural_levels, structural_floor
from src.models import OHLCV


def _setup_type_for(bars_through_signal: list[OHLCV]) -> str:
    """Deterministic substitute for the analyst's chart read (see module
    docstring). `is_consolidating` is the measurable half of "range or
    breakout" that `src/data/context.py` already computes."""
    ctx = compute_market_context(bars_through_signal)
    if ctx is not None and ctx.is_consolidating:
        return "range"
    return "breakout"


def _resolve_structural_stop_and_target(
    bars_through_signal: list[OHLCV],
    direction: str,
    entry_price: float,
    meter: SweepMeter | None = None,
) -> tuple[
    float | None,
    float | None,
    list[float],
    dict[float, int],
    dict[float, list[tuple[float, float]]],
]:
    """Nearest structural level on the protective side of `entry_price`
    becomes the stop candidate; the nearest level on the other side of it
    becomes the reference target. A `None` stop is NOT a refusal: it means
    no level defends this entry, and the caller hands `None` to the real
    `_widen_stop_past_noise`, which reads the stop from the instrument —
    exactly what `_resolve_stop` does live (item 54).

    ONE REFERENCE PRICE. `entry_price` is the NEXT day's open, the price
    this engine actually fills at, and it is the only price the levels are
    partitioned against. It was previously the signal day's close for the
    level partition and the next open everywhere else (sizing, the noise
    band, the reward:risk check), so a gap through a level put the stop on
    the wrong side of what was paid. This is NOT look-ahead: the engine
    already resolved the whole stop against this same next-day open before
    sizing, and the level choice is part of that same at-the-open decision.
    The LEVELS themselves still come only from bars through the signal day.

    The third element is every computed level, supports and resistances
    unioned — the same shape `TechAnalysisResult.computed_levels` carries in
    live. Spec §12.1 keys the stop rule off it, so without it this engine
    would silently measure the OLD behaviour and the parity claim in the
    module docstring would stop being true.

    The fourth element is the touch count behind each of those prices — the
    same shape `TechAnalysisResult.computed_level_touches` carries in live
    (Phase 12.1, 2026-09-03). It is carried as data only: since 2026-10-14
    no touch count gates whether a real level backs a stop, live or here.

    The fifth element is the high-low range of every bar that DREW each of
    those levels — the same shape `TechAnalysisResult.computed_level_bars`
    carries in live (docs/WORK.md items 55/215). The live stop rule asks
    whether the stop rests on one of those bars, and fails closed when the
    ranges are absent; an engine that did not carry them would refuse every
    level-backed stop while live honoured it, which is the opposite of the
    parity this function exists to keep."""
    # The level tunables are passed EXPLICITLY, read late from the
    # module that defines them, and counted. `find_structural_levels`
    # freezes them as default arguments at definition time, so a sweep
    # that reassigns `src.data.levels.MIN_TOUCHES` was swept past in silence and returned byte-identical
    # results — the exact trap `swept_values` exists to make visible.
    # Same values, same behaviour: only the binding time changes.
    meter = meter if meter is not None else SweepMeter()
    supports, resistances = find_structural_levels(
        bars_through_signal,
        min_touches=meter.read("levels.min_touches", levels_module.MIN_TOUCHES),
    )
    all_level_objs = (*supports, *resistances)
    all_levels = sorted(lv.price for lv in all_level_objs)
    touches = {lv.price: lv.touches for lv in all_level_objs}
    level_bars = {lv.price: list(lv.pivot_bars) for lv in all_level_objs}
    # The stop candidate is the nearest level on the stop side of the
    # ENTRY. A `None` here is passed on, not refused: the live constructor
    # reads a fallback stop from the instrument (the wider of the ATR noise
    # band and the signal bar's far edge) and gates on width, and this
    # engine now does the same through the same function.
    stop = structural_floor(all_levels, entry_price, direction)
    if direction == "long":
        target = min(
            (lv.price for lv in resistances if lv.price > entry_price),
            default=None,
        )
    else:
        target = max(
            (lv.price for lv in supports if lv.price < entry_price),
            default=None,
        )
    return stop, target, all_levels, touches, level_bars
