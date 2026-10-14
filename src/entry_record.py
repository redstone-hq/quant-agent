"""Record the pending entry row for a BUY / SHORT before the order is submitted.

Lifted verbatim out of ``ExecutionStage._run_session`` (src/stage_execution.py)
so the row the orphan sweep reconciles against can be written and exercised
with a stub database -- nothing here needs the pipeline. Every collaborator is
an explicit keyword-only argument.

The three conviction-ledger fields that a settlement recording pins at entry
(``stop_basis``, ``entry_atr``, ``requested_risk_pct``) are read through
``pinned_evidence`` so a renamed or missing field FAILS here instead of
quietly storing NULL for the life of the feature (see src/recording_accessors.py).
"""

from __future__ import annotations

from typing import Any

from src.entry_evidence import resolve_entry_pins
from src.recording_accessors import pinned_evidence

#: `stop_basis` when no rule is on the decision (a lane that never ran the
#: constructor stop rules). Explicit, so a blank can never mean "nobody asked".
STOP_BASIS_UNKNOWN = "stop_rule_not_determined"

__all__ = ["insert_pending_entry"]


def insert_pending_entry(
    *,
    db: Any,
    decision: Any,
    add_prep: Any,
    is_short: bool,
    qty: Any,
    executed_price: Any,
    run_id: Any,
    stop_price: Any,
    decision_id: Any,
    entry_analysis: Any,
    decision_model: Any,
) -> tuple[Any, str]:
    """Insert the ``pending_submit`` trade row; return ``(row_id, entry_side)``."""
    # Item 82: `setup_type` was being classified a SECOND time
    # here, independently of `PortfolioConstructor._build_buy`/
    # `_build_short` (see `TradeDecision.setup_type` in
    # models.py, which exists specifically so execution does not
    # have to re-derive this fact). On a scale-in ADD to an
    # already-held name this second lookup re-read TODAY's
    # technical read and wrote it onto the new row —
    # `get_symbol_last_buy` returns the newest row, so this
    # silently RECLASSIFIED a position whose setup_type was
    # already pinned on its original entry, which is worse than
    # a mere disagreement: pace/progress (disabled for a
    # breakout) could flip back on, or off, on a held position
    # with no new entry decision behind the change. A genuinely
    # new entry has no prior pinned row and reads the single
    # value the constructor already classified, carried on the
    # decision.
    _prior_row, pinned_setup_type, pinned_structural_ceiling = resolve_entry_pins(
        db,
        decision,
        is_short=is_short,
        is_scale_in=add_prep is not None and add_prep.is_scale_in,
    )
    entry_side = "sell_short" if is_short else "buy"
    pending_row_id = db.insert_trade(
        symbol=decision.symbol,
        action=decision.action,
        qty=qty,
        price=executed_price,
        reasoning=decision.reasoning,
        run_id=run_id,
        stop_loss=stop_price,
        take_profit=decision.take_profit,
        broker_order_id=None,
        fill_status="pending_submit",
        decision_id=decision_id,
        expected_horizon_sessions=getattr(
            entry_analysis,
            "expected_horizon_sessions",
            None,
        ),
        setup_type=pinned_setup_type,
        # Item 82: the MEASURED half of the same verdict, pinned at
        # entry so a row read back (pace/progress) reaches the SAME
        # breakout verdict construction reached, not a label-only
        # approximation. See TradeDecision.structural_ceiling.
        structural_ceiling=pinned_structural_ceiling,
        # Stop-floor evidence, pinned at ENTRY because neither
        # fact can be recovered afterwards: the ATR the stop was
        # measured in has moved by the time the trade resolves,
        # and the constructor's stop rule is not stored anywhere
        # else. Together with the adverse excursion accumulated
        # while the position is open and the realised outcome
        # already on the row, these let a future pass ask whether
        # the ratified minimum stop width was ever VIOLATED in
        # practice. They may NOT be swept for a better
        # multiplier — doctrine bars fitting a number to this
        # desk's history. See the `entry_atr` migration note in
        # src/storage/db.py.
        # Read off the ENTRY ANALYSIS, not off `decision`:
        # `TradeDecision` has no `atr_14` field at all (ATR(14)
        # lives on `TechnicalIndicators`/the analysis object), so
        # the original `getattr(decision, "atr_14", None)` was a
        # silent typo that resolved to its default on every
        # single trade and left the column empty for the whole
        # life of the feature. Same accessor the fat-finger
        # refusal below already uses. None on the resume/sweep
        # lanes that carry no analysis — the row then records no
        # ATR rather than a reconstructed one.
        entry_atr=pinned_evidence(entry_analysis, "atr_14"),
        stop_basis=pinned_evidence(decision, "stop_rule") or STOP_BASIS_UNKNOWN,
        # Item 55 RECORDING, no behaviour: what that stop was
        # BASED on — which level, how many turns made it, how
        # wide its zone was, how far the stop sat from it. Set
        # by the constructor; nothing downstream reads it back.
        stop_level_basis=getattr(decision, "stop_level_basis", None),
        # Conviction ledger (spec §7.2) — pinned at entry from
        # the constructor's TradeDecision (see portfolio_
        # constructor._build_buy/_build_short) and from this
        # run's PM model. None/None/None for a legacy notional
        # target that carried no risk-based plan.
        conviction=getattr(decision, "conviction", None),
        requested_risk_pct=pinned_evidence(decision, "requested_risk_pct"),
        allocated_risk_pct=getattr(decision, "allocated_risk_pct", None),
        decision_model=decision_model,
        # Same entry-only pinning as the conviction ledger above
        # — see TradeDecision.thesis_invalid_if in models.py.
        thesis_invalid_if=getattr(decision, "thesis_invalid_if", None),
    )
    return pending_row_id, entry_side
