"""Holding-discipline fact-check of the midday exit loop, lifted verbatim out
of `ExitEngineMixin._midday_execute_llm_actions` (src/pipeline_exits.py).

Asks whether the trigger an exit names is actually TRUE, through
`exit_guard.holding_discipline_claim_check` as assembled by
`_holding_discipline_check_for_exit`. The body is unchanged apart from
`continue` -> `return SKIP, hd_position_history`; `self` is bound to the
pipeline instance (`loop.owner`) so every line reads as before. The
position-history map is built at most once per pass, so it comes back to
the caller on every return (a refused symbol included) and is handed to the
next symbol, exactly as the loop-local variable was. Sell-side code:
one rule since 2026-10-13: a sale the same-rules gate
(`src/exits/same_rules_gate.py`) decided keeps a false claim as evidence
only; any other sale is dropped as before. The owner is alerted either way.
"""

import logging

from src.exits_parts.midday_state import SKIP, MiddayLoop
from src.sentinel.guarded_exit import record_exit_guard

#: The moved code logged under `src.pipeline` before the move and still does.
logger = logging.getLogger("src.pipeline")


def midday_holding_discipline(
    loop: MiddayLoop,
    action_item: dict,
    act,
    symbol,
    reason_text,
    hd_position_history: dict | None,
):
    """Fact-check one exit's named trigger; `(SKIP, history)` drops a sale the gate did not decide."""
    self = loop.owner
    positions = loop.positions
    run_id = loop.run_id
    # 2026-09-11 — and now: is the named trigger actually TRUE?
    #
    # The gate immediately above only proves the reason SAYS the
    # words. Until this landed that was the whole of the midday /
    # close check: "regime shift to risk-off; correlation breach
    # across the book" executed a SELL on a structurally protected
    # position on the strength of the phrasing, with no part of the
    # system ever asking whether a regime shift had happened. The
    # deterministic answer to that question already existed —
    # `exit_guard.holding_discipline_claim_check` — but was wired
    # only to the morning Portfolio-Manager path in
    # `pipeline_stages.RiskStage`. Same function here, same
    # semantics, assembled by `_holding_discipline_check_for_exit`.
    #
    # PROVABLY FALSE drops an exit the same-rules gate did not
    # decide; a gate-decided one keeps it as evidence (the one rule).
    # UNVERIFIABLE is recorded and ALLOWED THROUGH, unchanged from
    # the morning path and deliberately: absence of proof is not
    # proof, and refusing an exit on a claim we merely cannot check
    # would trap the desk in a losing position — a far worse
    # failure than the one being fixed. An infrastructure failure
    # inside the check fails OPEN for the same reason, matching
    # `_risk_review_exits`' disclosed posture on this path.
    if act in ("SELL", "REDUCE", "COVER"):
        if hd_position_history is None:
            try:
                hd_position_history = self._build_position_history(positions)
                record_exit_guard(self, "holding_discipline.position_history")
            except Exception as e:  # noqa: BLE001
                record_exit_guard(
                    self,
                    "holding_discipline.position_history",
                    e,
                    logger,
                    symbol=symbol,
                    effect="protection read without entry context",
                )
                hd_position_history = {}
        try:
            hd_check = self._holding_discipline_check_for_exit(
                symbol=symbol,
                action=act,
                reason=reason_text,
                positions=positions,
                run_id=run_id,
                position_history=hd_position_history,
                # The STRUCTURED trigger, so the fact-check reads
                # the claim from the field the seat filled rather
                # than guessing it from the sentence. This is what
                # makes the 2026-09-16 "adverse news" shape
                # adjudicable at all.
                exit_trigger=action_item.get("exit_trigger"),
            )
            record_exit_guard(self, "holding_discipline.check")
        except Exception as e:  # noqa: BLE001
            record_exit_guard(
                self,
                "holding_discipline.check",
                e,
                logger,
                symbol=symbol,
                effect="claim goes unverified rather than blocking",
            )
            hd_check = None
        if hd_check is not None and hd_check.blocks:
            gate_decided = loop.gate_decided(symbol, act)
            logger.warning(
                "Position reviewer: %s %s states a holding-discipline claim PROVEN FALSE (%s). %s",
                act,
                symbol,
                "evidence only; the same-rules gate decided the sale" if gate_decided else "exit blocked",
                hd_check.finding,
            )
            try:
                self.db.record_intraday_evaluation(
                    symbol=symbol,
                    run_id=run_id,
                    status=(
                        "holding_discipline_claim_false_evidence_only"
                        if gate_decided
                        else "exit_blocked_holding_discipline_claim_false"
                    ),
                    detail=(hd_check.finding or "")[:500],
                )
                record_exit_guard(self, "holding_discipline.audit_write_blocked")
            except Exception as e:  # noqa: BLE001
                record_exit_guard(
                    self,
                    "holding_discipline.audit_write_blocked",
                    e,
                    logger,
                    symbol=symbol,
                    effect="audit row not written",
                )
            from src.exits.same_rules_gate import alert_false_claim

            alert_false_claim(
                symbol,
                act,
                hd_check.finding,
                outcome=(
                    "the same-rules gate had already allowed the sale; the false reason is recorded as evidence"
                    if gate_decided
                    else "the sale was blocked; the holding and its stop stay"
                ),
            )
            # ONE RULE with the alignment, keyword and spent-trigger checks
            # (2026-10-13 same-rules ruling): a sale the gate decided keeps
            # the finding as evidence only; any other sale is dropped.
            if not gate_decided:
                from src.risk.exit_refusal import CODE_HOLDING_DISCIPLINE_FALSE

                self._record_exit_refusal(
                    symbol=symbol,
                    run_id=run_id,
                    action=act,
                    code=CODE_HOLDING_DISCIPLINE_FALSE,
                    dropped=True,
                    detail=(hd_check.finding or "")[:400],
                    layer="holding_discipline",
                )
                return SKIP, hd_position_history
        if hd_check is not None and hd_check.verdict == "unverifiable":
            # Audit trail only. NOT a block — see above.
            logger.warning("Holding discipline: %s", hd_check.finding)
            try:
                self.db.record_intraday_evaluation(
                    symbol=symbol,
                    run_id=run_id,
                    status="holding_discipline_claim_unverified",
                    detail=(hd_check.finding or "")[:500],
                )
                record_exit_guard(self, "holding_discipline.audit_write_unverified")
            except Exception as e:  # noqa: BLE001
                record_exit_guard(
                    self,
                    "holding_discipline.audit_write_unverified",
                    e,
                    logger,
                    symbol=symbol,
                    effect="audit row not written",
                )
    return None, hd_position_history
