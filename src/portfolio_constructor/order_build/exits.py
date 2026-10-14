"""Exit order builders — sell, cover and hold legs of the Portfolio Constructor.

Pure functions of their arguments (no collaborators): the host's thin shims
delegate straight to the staticmethods here.
Standalone piece: it takes no collaborators, so it builds and runs without a `PortfolioConstructor` or a
`TradingPipeline` behind it (clauses 1-5 of tests/boundary_harness.py).
Method bodies moved VERBATIM from src/portfolio_constructor/orders.py; the
same-named thin shims there build this object per call."""

from __future__ import annotations
from src.models import Position, TargetPosition, TradeDecision
from src.portfolio_constructor.config import logger
from src.portfolio_constructor.config import _named_reduction_trigger


class ExitOrderBuilders:
    """`_hold_decision`, `_build_sell`, `_build_cover` (lifted verbatim)."""

    def __init__(self):
        """Collaborator-free: the exit legs are pure functions of their arguments."""

    @staticmethod
    def _hold_decision(target: TargetPosition) -> TradeDecision:
        """Record PM's explicit 'keep' intent as a HOLD for audit trail."""
        return TradeDecision(
            action="HOLD",
            symbol=target.symbol,
            allocation_pct=0.0,
            entry_price=0.0,
            stop_loss=0.0,
            take_profit=0.0,
            reasoning=f"Hold at current weight. Thesis: {target.thesis[:200]}",
        )

    @staticmethod
    def _build_sell(
        target: TargetPosition,
        position: Position | None,
        current_pct: float,
        target_pct: float,
        current_risk_pct: float | None = None,
    ) -> TradeDecision | None:
        if position is None or position.qty <= 0:
            return None
        # Defensive: position.market_value can be NaN during broker price
        # glitches (qty > 0 but current_price NaN → market_value NaN).
        # Without this guard `current_pct` (computed upstream as
        # market_value / total_value * 100) is NaN, the partial-fraction
        # math `(NaN - target_pct) / NaN` is NaN, alloc becomes NaN, and
        # the BUY downstream sends a NaN qty to the broker. Pipeline.py:446
        # has the symmetric guard on the SELL pre-sum path; this is the
        # same fix in the constructor path. R4 audit finding.
        import math as _math

        if not _math.isfinite(current_pct) or current_pct <= 0:
            logger.warning(
                "Constructor: SELL %s skipped — current_pct=%s (market_value=%s likely NaN/zero from broker glitch)",
                target.symbol,
                current_pct,
                position.market_value,
            )
            return None
        named = _named_reduction_trigger(
            target,
            current_pct,
            target_pct,
            long_side=True,
            current_risk_pct=current_risk_pct,
        )
        if named is None:
            return None
        reasoning, falsifier = named
        if target_pct == 0:
            # Full close
            alloc = 100.0
        else:
            # Partial: sell enough to land on target_pct
            # fraction to sell = (current - target) / current
            fraction = (current_pct - target_pct) / current_pct
            alloc = max(1.0, min(99.0, round(fraction * 100, 1)))
        # SELLs don't need live entry/stop/target — execution uses market price
        return TradeDecision(
            action="SELL",
            symbol=target.symbol,
            allocation_pct=alloc,
            entry_price=0.0,
            stop_loss=0.0,
            take_profit=0.0,
            reasoning=reasoning,
            # Real, untruncated field alongside the embedded-in-reasoning
            # text above — see TradeDecision.thesis_invalid_if.
            # Blank-falsifier funding-trims leave this None — never invent.
            thesis_invalid_if=falsifier or None,
        )

    @staticmethod
    def _build_cover(
        target: TargetPosition,
        position: Position | None,
        current_pct: float,
        target_pct: float,
        current_risk_pct: float | None = None,
    ) -> TradeDecision | None:
        """D1/D3 (Stage 3): the SELL-side twin, for reducing/closing a short.

        `current_pct` and `target_pct` are both SIGNED and <= 0 here (the
        `construct_orders` dispatch only reaches this builder when the
        position is currently short and the signed target does not cross
        zero to the long side). The fraction-to-cover formula is
        algebraically identical to `_build_sell`'s — it falls out of the
        same `(current - target) / current` shape on negative numbers.
        """
        if position is None or position.qty >= 0:
            return None
        import math as _math

        if not _math.isfinite(current_pct) or current_pct >= 0:
            logger.warning(
                "Constructor: COVER %s skipped — current_pct=%s (market_value=%s likely NaN/zero from broker glitch)",
                target.symbol,
                current_pct,
                position.market_value,
            )
            return None
        named = _named_reduction_trigger(
            target,
            current_pct,
            target_pct,
            long_side=False,
            current_risk_pct=current_risk_pct,
        )
        if named is None:
            return None
        reasoning, falsifier = named
        if target_pct == 0:
            # Full cover
            alloc = 100.0
        else:
            # Partial: buy back enough to land on target_pct.
            fraction = (current_pct - target_pct) / current_pct
            alloc = max(1.0, min(99.0, round(fraction * 100, 1)))
        # COVERs don't need live entry/stop/target — execution uses market price
        return TradeDecision(
            action="COVER",
            symbol=target.symbol,
            allocation_pct=alloc,
            entry_price=0.0,
            stop_loss=0.0,
            take_profit=0.0,
            reasoning=reasoning,
            # Real, untruncated field alongside the embedded-in-reasoning
            # text above — see TradeDecision.thesis_invalid_if.
            # Blank-falsifier funding-trims leave this None — never invent.
            thesis_invalid_if=falsifier or None,
        )
