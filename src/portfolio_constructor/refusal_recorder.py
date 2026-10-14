"""Writes the constructor's durable refusal rows to `trade_refusals`.

Lives OUTSIDE the package `__init__` on purpose: the row writer is a
trade-table write, and the read-only API guard must not see a write-capable
name just because a module imported a sibling from the package. The
constructor never imports this module; the composition root builds one
recorder around its database and hands it in (`recorder=`).
"""

from __future__ import annotations

import logging

from src.portfolio_constructor.config import (
    SUBFLOOR_RISK_OBSERVED,
    _SUBFLOOR_RISK_STAGE,
)

#: One row per computed-vs-analyst target comparison. An OBSERVATION, never a
#: refusal: it carries stage `_SUBFLOOR_RISK_STAGE` and the signed gap in
#: `observed_gap_pct`. `threshold` is NULL: no warn threshold exists (the
#: chosen 25% was deleted 2026-10-13).
TARGET_DIVERGENCE_OBSERVED = "target_divergence_observed"

# The constructor's own logger, so log capture and filters keep matching.
logger = logging.getLogger("src.portfolio_constructor")


class TradeRefusalRecorder:
    """Bodies moved verbatim from `PortfolioConstructor`; neither ever raises."""

    def __init__(self, db) -> None:
        self.db = db

    def record_subfloor_risk_target(
        self,
        symbol: str,
        direction: str | None,
        requested_pct: float,
        min_risk_pct: float,
    ) -> None:
        """Record a positive sub-floor PM risk request. Never raises."""
        db = self.db
        if db is None:
            return
        try:
            db.insert_trade_refusal(
                symbol=symbol,
                direction=direction,
                refusal=SUBFLOOR_RISK_OBSERVED,
                stage=_SUBFLOOR_RISK_STAGE,
                requested_risk_pct=float(requested_pct),
                threshold=float(min_risk_pct),
            )
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "Constructor: sub-floor risk observation write failed for %s: %s",
                symbol,
                e,
            )

    def record_target_divergence(
        self,
        symbol: str,
        gap_pct: float,
    ) -> None:
        """Record ONE computed-vs-analyst target comparison. Never raises."""
        db = self.db
        if db is None:
            return
        try:
            db.insert_trade_refusal(
                symbol=symbol,
                direction=None,
                refusal=TARGET_DIVERGENCE_OBSERVED,
                stage=_SUBFLOOR_RISK_STAGE,
                observed_gap_pct=float(gap_pct),
                threshold=None,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "Constructor: target divergence row write failed for %s: %s",
                symbol,
                e,
            )
