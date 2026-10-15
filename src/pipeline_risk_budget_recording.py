"""Board item 186 — where the concentration recording is taken.

One call site, replacing the realised-sector-weight call in
`src/stage_decision.py` rather than sitting beside it, so the recording adds
no lines to files the rebuild's size ratchet is shrinking. Both recordings
are taken at the same moment and for the same reason: it is the first point
at which the FINISHED order list and the allocator's result both exist.

RECORDING ONLY. Nothing here decides anything and nothing reads these rows
back into a sizing, ordering or refusal decision.
"""

from __future__ import annotations

import logging

from src.recording_accessors import pinned_evidence
from src.pipeline_sector_weights import _record_realised_sector_weights
from src.storage.risk_budget_record import record_realised_risk_budget

logger = logging.getLogger(__name__)


def _record_realised_risk_budget(pipeline, ctx, total_value) -> None:
    """One durable row per run: the realised total at-risk. Reads the allocator's own stashed
    output rather than recomputing — a second computation could disagree
    with the one that rationed the orders. Never raises.
    """
    try:
        db = getattr(pipeline, "db", None)
        if db is None or getattr(db, "conn", None) is None:
            return
        constructor = getattr(pipeline, "portfolio_constructor", None)
        record_realised_risk_budget(
            db,
            allocation=getattr(constructor, "last_risk_allocation", None),
            equity=total_value,
            run_id=pinned_evidence(ctx, "run_id"),
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("realised risk-budget recording failed: %s", exc)


def _record_realised_concentration(
    pipeline,
    ctx,
    portfolio_decision,
    total_value,
) -> None:
    """Both concentration recordings, taken together at the one call site."""
    _record_realised_sector_weights(pipeline, ctx, portfolio_decision, total_value)
    _record_realised_risk_budget(pipeline, ctx, total_value)
