"""Which research seats' readings are unknown this run, per holding.

Lifted out of `src/pipeline_rotation_exec.py` so the same-rules gate
(`src/exits/same_rules_gate.py`) and the rotation pre-check read the ONE
rule without importing each other (the two formed an import cycle).
Imports nothing from `src`.
"""

from __future__ import annotations

#: A research seat that returned NO usable reading this run (provider
#: outage, unparseable answer, a carry-forward that could not be read). Every
#: holding's evidence from such a seat is unknown.
_SEAT_NO_READING_STATUSES = frozenset(
    {
        "failed",
        "provider_error",
        "parse_error",
        "carry_forward_failed",
    }
)

#: A seat whose answer WAS read but is incomplete (some sources or symbols
#: missing, an answer cut off mid-generation, external coverage blind). News
#: is `partial` on most runs (one source of eleven down, measured 12 Oct), so
#: this counts against a holding ONLY when that holding has no evidence from
#: the seat this run; otherwise the holding's own reading is complete.
_SEAT_INCOMPLETE_READING_STATUSES = frozenset(
    {
        "partial",
        "truncated",
        "degraded",
        "symbol_dropped",
        "field_unreadable",
        "market_wide_blind",
    }
)


def failed_evidence_seats(ctx, symbol: str | None = None) -> tuple[str, ...]:
    """The non-technical seats whose reading for `symbol` is unknown this run.

    A seat with no reading at all counts for every holding. A seat whose
    answer was read but is incomplete counts only when `symbol` has no
    evidence from it in this run's evidence registry. Technical is per name:
    a holding whose own technical read failed is never judged at all.
    """
    status = getattr(ctx, "data_status", None) or {}
    if not isinstance(status, dict):
        return ()
    registry = getattr(ctx, "evidence_registry", None) or {}
    own = registry.get(str(symbol or "").upper(), {}) if isinstance(registry, dict) else {}
    failed = []
    for seat, value in status.items():
        seat, value = str(seat), str(value)
        if seat == "tech":
            continue
        if value in _SEAT_NO_READING_STATUSES:
            failed.append(seat)
        elif value in _SEAT_INCOMPLETE_READING_STATUSES and own.get(seat) is None:
            failed.append(seat)
    return tuple(sorted(failed))
