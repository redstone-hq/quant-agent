"""Spent-trigger gate of the midday exit loop, lifted verbatim out of
`ExitEngineMixin._midday_execute_llm_actions` (src/pipeline_exits.py).

Board item 74: the second-sell-side-action warning and the per-event dedup
that refuses a cut whose trigger and cited record were already acted on
today. The body is unchanged apart from `continue` -> `return SKIP` and
returning the check result `spent` (nothing later in the loop reads it);
`self` is bound to the pipeline instance (`loop.owner`) so every
line reads as before. `acted_today` is the method's own list, so cuts
submitted earlier in the same pass are seen. Sell-side code: behaviour is
identical.
"""

import logging

from src.exits_parts.midday_state import SKIP, MiddayLoop
from src.sentinel.guarded_exit import record_exit_guard

#: The moved code logged under `src.pipeline` before the move and still does.
logger = logging.getLogger("src.pipeline")


def midday_spent_trigger(loop: MiddayLoop, action_item: dict, act, symbol):
    """Refuse a cut on a spent trigger; returns `SKIP` or the check result."""
    from src.risk.spent_trigger import SPENT_LAYER, spent_trigger_check

    self = loop.owner
    run_id = loop.run_id
    already_trimmed = loop.already_trimmed
    acted_today = loop.acted_today
    # The same-day-trim gate that used to sit here is GONE, not
    # relaxed: it read `symbol in already_trimmed and not
    # _reason_cites_hard_trigger(...)`, and a completed unnamed-
    # trigger judgment above now `continue`s on every untriggered
    # SELL/REDUCE before control ever reaches it. (A recogniser that
    # cannot run fails OPEN instead — that is uncertainty, not a
    # completed "no".) Leaving the old gate in place would have been
    # dead code wearing the costume of a safety check, which is worse
    # than no check at all.
    #
    # That residual gap — hard triggers exempt, so a symbol trimmed
    # at midday on "bearish earnings" could be trimmed again at close
    # on the SAME "bearish earnings" — is CLOSED below by the
    # per-event dedup it called for (board item 74, 2026-09-26). The
    # warning here stays: a second sell-side action is still worth
    # seeing in the log even when it is legitimate.
    if act in ("SELL", "REDUCE", "COVER") and symbol in already_trimmed:
        logger.warning(
            "Position reviewer: %s %s is a SECOND sell-side action today. Reason: %r",
            act,
            symbol,
            (action_item.get("reason") or "")[:160],
        )
    # Board item 74 — the RESIDUAL GAP above, now closed. The line is
    # the RECORD the seat cites, never a cooldown or a score: same
    # trigger + same cited record = spent, refuse; a different record
    # = new information, execute and say so.
    spent = spent_trigger_check(
        action=act,
        symbol=symbol,
        trigger=action_item.get("exit_trigger"),
        evidence=action_item.get("trigger_evidence"),
        acted_today=acted_today,
    )
    if spent.verdict == "uncertain":
        logger.error(
            "Spent-trigger check: today's acted-trigger record is unreadable — failing OPEN on %s %s. %s",
            act,
            symbol,
            spent.detail,
        )
    elif spent.blocks:
        logger.warning(
            "Position reviewer: REFUSING %s %s — the trigger is SPENT. %s",
            act,
            symbol,
            spent.detail,
        )
        try:
            self.db.record_intraday_evaluation(
                symbol=symbol,
                run_id=run_id,
                status="exit_blocked_trigger_already_spent",
                detail=spent.detail[:500],
            )
            record_exit_guard(self, "spent_trigger.audit_write")
        except Exception as e:  # noqa: BLE001
            record_exit_guard(
                self,
                "spent_trigger.audit_write",
                e,
                logger,
                symbol=symbol,
                effect="audit row not written",
            )
        self._record_exit_refusal(
            symbol=symbol,
            run_id=run_id,
            action=act,
            code=spent.code,
            dropped=not loop.gate_decided(symbol, act),
            detail=spent.detail[:400],
            layer=SPENT_LAYER,
        )
        if not loop.gate_decided(symbol, act):
            return SKIP  # a gate-decided sale keeps this as evidence only
    elif spent.verdict in ("new_evidence", "unidentifiable"):
        # Allowed, NOT silent. `new_evidence` is the "genuinely worse
        # reading" this item preserves; `unidentifiable` is a second
        # cut naming no record, which this layer cannot prove is the
        # same one and which the upstream substantiation layer
        # already lets through — the two must not disagree about the
        # identical input. Both are recorded for the evening grade.
        logger.warning(
            "Position reviewer: %s %s is a second cut on the same trigger — allowed (%s). %s",
            act,
            symbol,
            spent.verdict,
            spent.detail,
        )
        self._record_exit_refusal(
            symbol=symbol,
            run_id=run_id,
            action=act,
            code=spent.code,
            dropped=False,
            detail=spent.detail[:400],
            layer=SPENT_LAYER,
        )
    return spent
