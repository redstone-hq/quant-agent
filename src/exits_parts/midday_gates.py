"""Pre-gates of the midday exit loop, lifted verbatim out of
`ExitEngineMixin._midday_execute_llm_actions` (src/pipeline_exits.py).

The metric-contradiction veto (Phase 3.2) and the AI Risk veto (Phase 3.4).
One rule since 2026-10-13: a sale the same-rules gate
(`src/exits/same_rules_gate.py`) decided keeps both as evidence only; any
other sale is vetoed as before.
The body is unchanged apart from `continue` -> `return SKIP`; `self` is bound
to the pipeline instance (`loop.owner`) so every line reads as before.
Sell-side code: behaviour is identical.
"""

import logging

from src.exits_parts.midday_state import SKIP, MiddayLoop
from src.sentinel.guarded_exit import record_exit_guard

#: The moved code logged under `src.pipeline` before the move and still does.
logger = logging.getLogger("src.pipeline")


def midday_pre_gates(loop: MiddayLoop, action_item: dict, act, symbol):
    """Veto one symbol's exit before the noise band (`SKIP` drops it) unless the gate decided it."""
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
                        status=(
                            "exit_metrics_improved_evidence_only"
                            if loop.gate_decided(symbol, act)
                            else "exit_vetoed_contradicts_own_metrics"
                        ),
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
                # ONE RULE with the alignment, keyword and spent-trigger
                # checks (2026-10-13 same-rules ruling): a sale the gate
                # decided keeps "my numbers improved" as evidence only; any
                # other sale is vetoed exactly as before.
                if not loop.gate_decided(symbol, act):
                    from src.risk.exit_refusal import CODE_CONTRADICTS_METRICS

                    self._record_exit_refusal(
                        symbol=symbol,
                        run_id=run_id,
                        action=act,
                        code=CODE_CONTRADICTS_METRICS,
                        dropped=True,
                        detail=veto[:400],
                        layer="metric_contradiction",
                    )
                    return SKIP

    # Phase 3.4 — an AI Risk veto, under the same one rule: it never
    # undoes a sale the same-rules gate decided (no seat holds or sells
    # alone); any other sale it vetoed is skipped as before. Its per-stock
    # objections are also recorded by `_risk_review_exits` as advisory rows.
    if act in ("SELL", "REDUCE", "COVER") and symbol in (risk_vetoed_symbols or set()):
        if not loop.gate_decided(symbol, act):
            logger.warning("Position reviewer: skipping %s %s — vetoed by AI Risk", act, symbol)
            return SKIP
        logger.warning(
            "Position reviewer: AI Risk objected to %s %s — evidence only; the same-rules gate decided it",
            act,
            symbol,
        )
    return None
