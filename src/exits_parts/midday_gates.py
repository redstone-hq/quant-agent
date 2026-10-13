"""Pre-gates of the midday exit loop, lifted verbatim out of
`ExitEngineMixin._midday_execute_llm_actions` (src/pipeline_exits.py).

The metric-contradiction read (Phase 3.2) — evidence only since 2026-10-13;
the same-rules gate (`src/exits/same_rules_gate.py`) decides every
judgement sale before this runs, and the AI Risk exit veto is deleted.
The body is unchanged apart from `continue` -> `return SKIP`; `self` is bound
to the pipeline instance (`loop.owner`) so every line reads as before.
Sell-side code: behaviour is identical.
"""

import logging

from src.exits_parts.midday_state import MiddayLoop
from src.sentinel.guarded_exit import record_exit_guard

#: The moved code logged under `src.pipeline` before the move and still does.
logger = logging.getLogger("src.pipeline")


def midday_pre_gates(loop: MiddayLoop, action_item: dict, act, symbol):
    """Record exit evidence before the noise band; never drops an exit."""
    self = loop.owner
    run_id = loop.run_id
    metric_deltas = loop.metric_deltas
    risk_vetoed_symbols = loop.risk_vetoed_symbols
    # Same-day trim discipline: a symbol that already had a sell-side
    # action TODAY (midday REDUCE, force-delever, etc.) is off-limits for
    # additional REDUCE / SELL on a SECOND session unless the LLM
    # explicitly cites a hard trigger in the reason. TRAIL_STOP is
    # exempt — adjusting a stop is not selling shares.
    #
    # 2026-05-04 AMZN: midday REDUCE 20 of 41 @ +12.4% on TARGET_BREACH,
    # then close REDUCE 10 of 21 @ +13.8% on the SAME TARGET_BREACH
    # flag = 73% one-day trim on a strengthening thesis. Mechanical
    # double-application of one signal violates "good stocks are meant
    # to be held".
    # Phase 3.2 — a deterioration verdict may not contradict the
    # reviewer's own recorded numbers. Vetoes ONLY a SELL/REDUCE/
    # COVER whose stated reason claims the position is stalling
    # while every metric that moved since the previous review
    # improved. Exits on new information (news, earnings, regime,
    # invalidation) are untouched, however good the numbers look —
    # see src/risk/exit_guard.py. metric_deltas is already sign-
    # corrected per symbol (see _build_position_facts), so COVER
    # needs no extra handling here.
    if act in ("SELL", "REDUCE", "COVER") and metric_deltas:
        from src.risk.exit_guard import veto_contradicted_exit

        deltas = metric_deltas.get(symbol)
        if deltas is not None:
            veto = veto_contradicted_exit(
                act,
                action_item.get("reason", ""),
                deltas,
            )
            if veto:
                logger.warning("Exit guard: %s", veto)
                try:
                    self.db.record_intraday_evaluation(
                        symbol=symbol,
                        run_id=run_id,
                        status="exit_metrics_improved_evidence_only",
                        detail=veto[:500],
                    )
                    record_exit_guard(self, "exit_guard.metric_audit_write")
                except Exception as e:  # noqa: BLE001
                    record_exit_guard(
                        self,
                        "exit_guard.metric_audit_write",
                        e,
                        logger,
                        symbol=symbol,
                        effect="audit row not written",
                    )
                # EVIDENCE ONLY since the 2026-10-13 same-rules ruling: a
                # sale has already passed `src/exits/same_rules_gate.py`
                # (the holding fails the entry bar), so "my numbers
                # improved" is no longer a second, hold-side standard that
                # can veto it. The row above keeps the measurement.

    # The AI Risk seat's exit veto is DELETED (owner ruling 2026-10-13;
    # advisory since 2026-10-09). `risk_vetoed_symbols` is kept on the loop
    # for callers that still pass it and is no longer read.
    _ = risk_vetoed_symbols
    return None
