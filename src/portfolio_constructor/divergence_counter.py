"""Records every computed-target vs analyst-target comparison, one durable row
each, and logs it.

Why this exists. Nothing in the repo used to keep the gap, so the model's
chart-reading could only be argued about. There is no warning threshold: the
chosen 25% that once split these lines into WARNING and INFO was deleted
(2026-10-13) because it changed no decision. The gap itself is the evidence.

Each comparison writes ONE row (symbol, signed observed gap) through the
constructor's refusal recorder. Nothing is accumulated here: the distribution
and the sign bias are queries over those rows at READ time. With no recorder wired the
comparison is logged only and nothing is counted. This changes no value,
gates nothing and places no order. A recording is not a settlement.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def log_divergence(
    *,
    symbol: str,
    derivation,
    recorder=None,
) -> None:
    """Record where the model's guess and the computed level disagree.

    Lifted from `PortfolioConstructor._log_target_divergence`. The model's
    target is no longer arithmetic, but it is still the only read available
    on whether the model's chart-reading is worth anything. A large,
    one-directional gap across many symbols is a finding about the seat; a
    large gap on one symbol is a finding about that symbol.
    """
    if derivation.price is None or derivation.model_target is None:
        return
    gap = derivation.divergence_pct
    if gap is None:
        return
    if recorder is not None:
        recorder.record_target_divergence(symbol, gap)
    message = "Constructor: %s target -- computed $%.2f (%s) vs analyst's reference_target $%.2f: %+.1f%%"
    args = (
        symbol,
        derivation.price,
        derivation.basis,
        derivation.model_target,
        gap,
    )
    logger.info(message, *args)
