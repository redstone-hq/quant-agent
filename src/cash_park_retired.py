"""The retired cash-park vehicle: exempting it from the stop audit.

Former `TradingPipeline._retired_cash_park_symbol` (body moved here unchanged).
Its sibling `_release_retired_cash_park` was deleted 2026-10-14. The cash sweeper is HANDED IN as a getter
rather than imported: `src.execution` is the broker seam and the set of
modules reaching it must not widen (`scripts/import_graph.LAYER_RULES`).

The getter is called on EVERY use, never captured once. Tests build the
owner via ``__new__`` and assign ``cash_sweeper`` afterwards, and the owner
may replace it; a snapshot taken at construction would silently keep the
original forever. The getter also owns the type gate (a MagicMock or None
sweeper must read as "no sweeper"), because the type to gate against lives
behind the broker seam this module may not import.
"""

from __future__ import annotations

import logging
from typing import Callable
from src.sentinel.counted import record_swallowed_here

logger = logging.getLogger(__name__)

# Returns the owner's real CashSweeper, or None when absent / not a real one.
SweeperGetter = Callable[[], object | None]


def retired_cash_park_symbol(get_sweeper: SweeperGetter) -> str | None:
    """The configured sweep vehicle when the sweep is DISABLED, else None.

    Owner mandate 2026-09-17 turned the sweep off. A vehicle bought
    before that is a deliberately stopless holding (nothing sells it
    automatically since 2026-10-14), so the stop-coverage audit
    must keep exempting it rather than raising a naked-position banner
    (its opening row is SWEEP_BUY, so the repair could not rebuild a
    stop anyway).
    """
    sweeper = get_sweeper()
    if sweeper is None:
        return None
    try:
        if sweeper.enabled():
            return None
        sym = sweeper.symbol
    except Exception:  # noqa: BLE001
        record_swallowed_here("cash_park_retired.retired_cash_park_symbol", log=logger)
        return None
    return sym if isinstance(sym, str) and sym.strip() else None
