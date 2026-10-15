"""Budget-day classification for the backtest engine.

Moved out of ``engine`` unchanged so that module can stop growing: whether a
day's risk-budget allocation bound, and whether ticker spelling arbitrated it.
``engine`` re-exports both names.
"""

from __future__ import annotations

from src.risk.budget import BudgetAllocation


def _budget_binds(allocation: BudgetAllocation) -> bool:
    """True when at least one new request was not granted in full.

    That is a day the total ceiling bound. A bind can
    be two equal asks competing (alphabetical among them) or a lone
    candidate cut by held risk — the count is the bind, not a claim
    that ticker spelling decided every one. The report labels the
    tie-break as alphabetical because this engine never passes
    `priority` and every ask is the same size.
    """
    return any(grant.requested_pct > 0.0 and grant.limited_by is not None for grant in allocation.grants.values())


def _tie_break_arbitrated(allocation: BudgetAllocation, new_request_count: int) -> bool:
    """True when the alphabetical tie-break actually DECIDED something.

    That needs two conditions together: at least two new candidates
    competed on the day, and at least one of them was not granted in
    full. With a single candidate there is nobody to order it against —
    a cut there is the ceiling biting, exactly as it
    would in production. `_budget_binds` counts both cases; this counts
    only the ones where ticker spelling chose between names.
    """
    return new_request_count >= 2 and _budget_binds(allocation)
