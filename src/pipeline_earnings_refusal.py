"""Refuse queued BUYs on names whose filed report went unanalysed, lifted out
of ``pipeline_risk_gate.py`` (at its size ratchet) so that module's catch-alls
could be made LOUD. A pure rule over the decisions and the earnings results it
is handed; ``RiskGate`` re-exports it as a staticmethod under its old name.
"""

from __future__ import annotations

import logging

from src.models import TradeDecision

logger = logging.getLogger("src.pipeline_risk_gate")


def refuse_queued_earnings_buys(
    decisions: list[TradeDecision],
    earnings_results: list[dict],
) -> list[TradeDecision]:
    """REFUSE every BUY on a symbol whose just-filed report reached this
    session unread. Board item 186, 2026-10-01.

    MISSING EVIDENCE, NOT LOW CONVICTION. `queued=True` is set in one
    place only (the session-time earnings fetch below): a filing the
    pre-market preprocess failed to pick up and analyse. It records an
    operations failure of this desk's own pipeline, not a seat verdict
    and not a market event, and this gate is argued on exactly those
    terms — the desk meant to read the report before deciding, it did
    not, and it declines to buy into the gap. It is NOT the conviction
    bar and does not touch it: see `risk.rules.unread_filing_block_reason`
    for why routing it through R7 would be wrong and would also change
    behaviour on names the desk already holds.

    WHAT THIS REPLACED, AND WHY THE NUMBER IS GONE. Until now this was a
    clamp: the resulting position weight on such a name was held to 5% of
    the book. That 5 had no source. It was researched to a definite
    negative (the closest published quantity, the ~5.07% average
    one-day absolute earnings-announcement move, measures the size of a
    MOVE and not a share of a BOOK, and the desk's own per-trade risk
    envelope runs forward to a weight near 100%, so it cannot be the
    cap's parent). Under the owner's 2026-09-30 ruling a global constant
    governing risk is a defect to be removed, not an appetite to be
    answered, so the condition is REFORMULATED instead of re-derived and
    no percentage survives. REFUSING rather than sizing down is
    REASONING, not a quoted rule: the entry bar already refuses a name
    whose technical read is merely ABSENT, so requiring the filing to
    have been read before buying is consistent with how this desk
    already treats evidence it does not have, and the desk archetype's rule
    that a fresh catalyst is required to ENTER is what makes an entry
    the right thing to withhold.

    BUY-ONLY, and silent about everything else. A SELL is untouched, a
    name whose filing has been read is untouched, and nothing already
    held is sold or reclassified — refusing to BUY is not a decision to
    SELL, the same contract `agreement_refuses_trade` carries.

    The refusal is recorded durably per symbol by
    `pipeline_stages._record_queued_earnings_refusals`, which reads the
    before/after lists, so a refused BUY can be judged later from the
    record rather than from argument.
    """
    queued_symbols = {
        (ea.get("symbol") or "").strip().upper()
        for ea in earnings_results
        if ea.get("queued") and not ea.get("analysis")
    }
    queued_symbols.discard("")
    if not queued_symbols:
        return decisions

    from src.risk.rules import unread_filing_block_reason

    kept: list[TradeDecision] = []
    for d in decisions:
        if d.action != "BUY" or d.symbol.upper() not in queued_symbols:
            kept.append(d)
            continue
        logger.warning(
            "Unread-filing refusal: dropping %s BUY %.2f%% — %s",
            d.symbol,
            d.allocation_pct,
            unread_filing_block_reason(d.symbol),
        )
    return kept
