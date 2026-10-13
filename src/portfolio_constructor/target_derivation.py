"""Take-profit derivation and trim sizing inputs, lifted out of `__init__`.

Bodies moved verbatim from `PortfolioConstructor`; the config and the record
side-channels are passed in as explicit arguments.
"""

from __future__ import annotations

import json
import logging

from src.data.levels import (
    COVERAGE_UNKNOWN,
    FAULT_NO_ANALYSIS,
    TargetDerivation,
    derive_structural_target,
)
from src.models import TargetPosition, TechAnalysisResult
from src.portfolio_constructor.config import TRIM_REFUSAL_NO_USABLE_LIVE_STOP
from src.portfolio_constructor.divergence_counter import log_divergence

logger = logging.getLogger(__name__)

#: Stable prefix of the per-symbol "no target" log record (owner rule
#: 2026-10-09). One line per derivation that found no structural level on a
#: measured chart: the trade proceeds on its ATR stop with no take-profit.
NO_TARGET_ROW_TAG = "NO_TARGET_ROW "


def _held_trim_entry_and_stop(
    note_refusal,
    target: TargetPosition,
    market_price: float | None,
    live_stop: float | None,
) -> tuple[float | None, float | None]:
    """(current price, live broker stop) for a trim of an unanalysed
    holding, or (None, None) after filing a named refusal.

    §2.1's own formula, applied to the position as it stands: shares to
    keep = equity x target risk / |price - live stop|. The stop must sit on
    the losing side of the price (below for a long, above for a short) —
    otherwise it bounds no loss and cannot size anything.
    """
    import math as _math

    sym = target.symbol
    is_short = target.direction == "short"
    action = "COVER" if is_short else "SELL"
    price = float(market_price) if market_price else 0.0
    stop = float(live_stop) if live_stop else 0.0
    usable = (
        _math.isfinite(price)
        and _math.isfinite(stop)
        and price > 0
        and stop > 0
        and (stop > price if is_short else stop < price)
    )
    if usable:
        return (price, stop)
    if not stop:
        why = "it has no live stop order at the broker"
    elif not price:
        why = "there is no current price for it"
    else:
        why = (
            f"its live stop (${stop:,.2f}) is not "
            f"{'above' if is_short else 'below'} the current price "
            f"(${price:,.2f}), so it bounds no loss"
        )
    note_refusal(
        sym,
        target.direction,
        TRIM_REFUSAL_NO_USABLE_LIVE_STOP,
        f"the PM asked to trim {sym} to {target.risk_allocation_pct:.2f}% "
        f"risk. {sym} was not analysed this session, so the trim can only "
        f"be sized from the position's own stop, and {why}. The position "
        f"is left unchanged. This is not a market data fault.",
        action=action,
    )
    # drop-reason: filed just above (TRIM_REFUSAL_NO_USABLE_LIVE_STOP).
    return (None, None)


def _derive_target(
    cfg,
    note_data_fault,
    log_target_divergence,
    symbol: str,
    analysis: TechAnalysisResult | None,
    entry_price: float,
    direction: str,
) -> TargetDerivation:
    """Compute the take-profit from structure, or refuse by name.

    This replaces reading `analysis.reference_target` as the trade's
    target. The model's number is still passed in — as `model_target`,
    which the derivation never uses to choose an answer and only carries
    so the disagreement can be logged. See
    `src/data/levels.py::derive_structural_target`.

    Deterministic and cheap, so it is called from both
    `_resolve_entry_and_stop` (which needs it for the reward:risk check
    after widening) and the builders (which need the number itself)
    rather than being threaded through as state. Same inputs, same
    answer, both times.
    """
    if analysis is None:
        # The desk holds no technical analysis for this symbol at all.
        # Every derivation input is absent at once, so this is named
        # for what it is rather than for the first missing field.
        detail = (
            "DATA FAULT: no technical analysis exists for this symbol "
            "this session — nothing to measure a target or a stop from"
        )
        note_data_fault(symbol, direction, FAULT_NO_ANALYSIS, detail)
        return TargetDerivation(price=None, fault=FAULT_NO_ANALYSIS, detail=detail)
    derivation = derive_structural_target(
        entry_price=entry_price,
        direction=direction,
        levels=getattr(analysis, "computed_levels", None) or [],
        atr=getattr(analysis, "atr_14", None),
        horizon_sessions=getattr(analysis, "expected_horizon_sessions", None),
        setup_type=getattr(analysis, "setup_type", None),
        model_target=getattr(analysis, "reference_target", None),
        min_target_atr_multiple=cfg.min_target_atr_multiple,
        breakout_projection_atr_multiple=cfg.breakout_projection_atr_multiple,
        max_reach_atr_multiple=cfg.max_target_reach_atr_multiple,
        max_horizon_sessions=cfg.max_target_horizon_sessions,
        # What the bar history behind `computed_levels` was, so an
        # empty list from a dead feed is a DATA fault and one from a
        # measured, structureless chart is a refusal. `getattr` with
        # the unknown default because older rows and hand-built
        # analyses (backtest shim, tests) predate the field.
        levels_coverage=getattr(analysis, "levels_coverage", None) or COVERAGE_UNKNOWN,
    )
    if derivation.fault:
        # Recorded here, at the single funnel every derivation passes
        # through, so the eligibility preview and order construction
        # cannot disagree about what was unmeasurable.
        note_data_fault(
            symbol,
            direction,
            derivation.fault,
            derivation.detail,
        )
    log_target_divergence(symbol, derivation)
    return derivation


def log_no_target(symbol: str, direction: str, entry_price: float, derivation: TargetDerivation) -> None:
    """Write the per-symbol "no target" record for an order that ships with
    no take-profit. Called by the order builders, once per shipped order —
    not from `_derive_target`, which runs twice per symbol (preview and
    build) and would double-count."""
    if not derivation.no_target:
        return
    logger.warning(
        "%s%s",
        NO_TARGET_ROW_TAG,
        json.dumps(
            {
                "symbol": symbol,
                "direction": direction,
                "entry_price": entry_price,
                "basis": derivation.basis,
                "model_target": derivation.model_target,
                "detail": derivation.detail,
            },
            sort_keys=True,
        ),
    )


def _log_target_divergence(
    cfg,
    refusal_recorder,
    symbol: str,
    derivation: TargetDerivation,
) -> None:
    """Delegate to `divergence_counter.log_divergence`, which logs the
    comparison and, when a recorder is wired, writes one durable row for
    it. Nothing is accumulated in memory."""
    log_divergence(
        symbol=symbol,
        derivation=derivation,
        recorder=refusal_recorder,
    )


def _target_note(derivation: TargetDerivation) -> str:
    """Provenance for the order's reasoning, appended after truncation.

    The AI Risk Manager reads `reasoning`. It must be able to see that
    the take-profit is a computed level rather than the analyst's number,
    and where the two differ — otherwise it re-does the comparison in its
    head, which is the class of error that produced two contradictory
    reward:risk figures in one response on 2026-08-31.
    """
    if derivation.no_target:
        # The AI Risk Manager must see that the target is ABSENT on purpose,
        # not lost: no level was found, so nothing was invented.
        return (
            " [target ABSENT — no structural level on the measured chart; trend trade on its ATR stop, no take-profit]"
        )
    if derivation.price is None:
        return ""
    note = f" [target ${derivation.price:,.2f} — {derivation.basis}]"
    if derivation.model_target is not None and derivation.divergence_pct is not None:
        note = (
            f" [target ${derivation.price:,.2f} computed from "
            f"{derivation.basis.replace('_', ' ')}; analyst's reference "
            f"${derivation.model_target:,.2f}, "
            f"{derivation.divergence_pct:+.1f}%]"
        )
    # The thin-reward fact travels WITH the target or it does not exist
    # (2026-09-30). `derive_structural_target` now targets the nearest
    # wall instead of stepping over it, which is the honest answer, but
    # a $730.41 target on a $728.41 entry reads as an ordinary target to
    # every downstream reader unless the room is stated. This string is
    # the one place the AI Risk Manager sees the target's provenance, so
    # the smallness goes here rather than dying in `detail`, which
    # nothing reads on a successful derivation.
    if derivation.target_inside_noise:
        note = note.rstrip("]") + (
            "; ENTIRE reward is inside one session's typical range — "
            "thin geometry, judge it on conviction and risk, not on "
            "this ratio]"
        )
    return note
