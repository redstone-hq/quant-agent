"""Exit-path refusal policy — one owner, one uncertainty fail direction.

WORK.md item 60 (closed 2026-09-16). The exit path had two layers that
could refuse a SELL/REDUCE/COVER, failing in opposite directions on
uncertainty:

* `_risk_review_exits`: an unavailable, unparseable, or verdict-less Risk
  Manager failed OPEN (owner-ratified 2026-08-27). Failing closed on an
  exit would strand a thesis-invalidated position because a language
  model was unavailable.
* `_reason_cites_hard_trigger`: a case-insensitive substring miss dropped
  the exit — fail CLOSED.

Those are not the same kind of event, and treating them as if they were
is what made the pair look incoherent.

**Owner of refusal: deterministic Python.** A completed content judgment
that the reason did not name a recognised trigger is a refusal by this
owner. So are the fact gates that already run on this loop (noise band,
metric-contradiction veto, proven-false holding-discipline claim). AI
Risk is an ADVISORY challenge seat: since the 2026-09-19 decision
(shipped 2026-10-09) a parseable reject no longer drops an exit; it is
recorded per stock as `CODE_AI_RISK_OBJECTION` (`dropped=False`) and the
sell proceeds. It cannot refuse by being silent, and its approval cannot
override a deterministic drop.

**Fail direction on uncertainty: OPEN, for both layers.** Uncertainty
means the layer could not produce a judgment — the Risk Manager raised,
returned no verdict, or the hard-trigger recogniser could not run
(non-string reason, or the matcher itself raised). That is the 2026-08-27
ratification, now applied to the pair rather than to one layer only. A
present reason whose text does not contain a recognised trigger is not
uncertainty: the matcher finished, the answer is no, the owner refuses.

The silent disagreement this item carried is gone because unnamed-trigger
exits are no longer sent to the Risk Manager. Fail-OPEN on a dead model
therefore only ever applies to an exit the owner already let through the
phrase gate — which is the case the 2026-08-27 ratification described
(a thesis-invalidated position that named the invalidation).

This module does not change sale-block appetite. It does not move
`NOISE_BAND_ATR_MULTIPLE`, `BREAK_CONFIRMATION_ATR_MULTIPLE` or
`absolute_min_stop_atr_multiple` (item 70).
It records every drop, every uncertainty fail-open, and (since board item
164, 2026-09-19) every AI Risk approval, as append-only per-symbol
specialist evidence so a later upsert on the cooldown ledger cannot erase
the reason. The kind keeps its historical name; `dropped` and `code` say
which outcome a row is.
"""

from __future__ import annotations

from src.sentinel.guarded import NO_LEDGER, record_guarded_pass
import json
import logging
from typing import Any, Callable, Literal

logger = logging.getLogger(__name__)

#: Forensic kind written to `specialist_evidence`. Append-only; the
#: trading pipeline never reads these rows.
EXIT_REFUSAL_KIND = "exit_refusal"

#: The layer whose completed "no" is binding. AI Risk may only object
#: (advisory, never a drop); it is not this owner.
REFUSAL_OWNER = "deterministic"

#: Shared uncertainty posture for both layers of the item-60 pair.
UNCERTAINTY_FAIL = "open"

TriggerJudgment = Literal["named", "unnamed", "uncertain"]

# Completed refusals (dropped=True).
CODE_UNRECOGNIZED_TRIGGER = "unrecognized_trigger"
# Historical only: rows written before the 2026-09-19 decision (shipped
# 2026-10-09) when an AI Risk reject still dropped the exit. Nothing writes
# it now; kept so readers can still name those rows.
CODE_AI_RISK_REJECT = "ai_risk_reject"
# Written by the midday TRAIL_STOP branch (`src/pipeline_exits.py`) when a
# proposed stop lands inside `NOISE_BAND_ATR_MULTIPLE` x ATR14 of price and no
# hard trigger is cited; the old stop is kept. Rows written before 2026-10-09
# with this code are the entry-anchored band that then dropped a losing sale.
CODE_NOISE_BAND = "inside_atr_noise_band"
CODE_CONTRADICTS_METRICS = "contradicts_own_metrics"
CODE_HOLDING_DISCIPLINE_FALSE = "holding_discipline_claim_false"

# Uncertainty — recorded, not a drop from that layer.
CODE_HARD_TRIGGER_UNCERTAIN = "hard_trigger_uncertain"
CODE_AI_RISK_UNAVAILABLE = "ai_risk_unavailable"

# A completed APPROVAL by the challenge seat — recorded, not a drop (board
# item 164, 2026-09-19). Until then an approved exit reached `agent_logs`
# only, so this per-symbol record held every outcome except the commonest.
CODE_AI_RISK_APPROVED = "ai_risk_approved"

# A completed OBJECTION by the challenge seat that did NOT stop the sell
# (dropped=False). Decided 2026-09-19, adversary-approved again 2026-10-09:
# the seat may not block an exit. Whole-book or per-name, its reject is
# recorded here per stock with its reason and the exit proceeds to the
# deterministic fact gates. Distinct from `CODE_AI_RISK_REJECT` so a reader
# can tell an objection that did not stop the sell from a refusal that did.
CODE_AI_RISK_OBJECTION = "ai_risk_objection_advisory"

# A completed ADJUSTMENT, not a refusal and not a drop (board item 185,
# 2026-09-30). The midday TRAIL_STOP path used to REFUSE a proposed stop
# sitting absurdly far below price as a model typo -- and it did so on the
# one branch where the live broker stop could not be read, i.e. exactly
# where refusing leaves the position with NO stop at all. That contradicted
# the owner's board-item-80 ruling ("a missing volatility reading is never a
# reason to skip protection"), whose shape is: never answer a stop you
# dislike by placing nothing. The path now CLAMPS such a proposal to the
# widest stop the desk's own rules can legitimately place -- that multiple
# of the name's own live ATR14 -- and places it. Protection is always
# established; this row records that the desk, not the model, chose the
# price.
CODE_TRAIL_CLAMPED_TO_WIDEST = "trail_stop_clamped_to_widest_placeable"

# Two midday TRAIL_STOP refusals that used to reach the log only, so a
# per-symbol reader could not see why a proposed stop never moved
# (2026-10-15). Both keep the existing stop (dropped=True: the proposal is
# dropped, protection is not). The first: the proposed stop sits at or above
# the current price, so it would fill at once -- it is not a stop. The
# second: the proposed stop does not clear the live stop by one venue tick
# (`src.risk.trailing.min_ratchet_floor`), so it is the same price.
CODE_TRAIL_STOP_AT_OR_ABOVE_PRICE = "trail_stop_at_or_above_price"
CODE_TRAIL_BELOW_MIN_RATCHET = "trail_stop_below_min_ratchet_floor"


def classify_trigger_reason(
    reason: object,
    *,
    cites: Callable[[str], bool],
    trigger: object = None,
    trigger_evidence: object = None,
) -> TriggerJudgment:
    """Classify an exit against the named-trigger recogniser.

    `trigger` is the STRUCTURED field (`PositionAction.exit_trigger`) and
    `trigger_evidence` is the field that says what it rests on, when the
    caller has them. A recognised `ExitTrigger` in the field is the typed,
    unambiguous form of the same claim the prose gate hunts for in words —
    but ONLY WITH EVIDENCE BEHIND IT. The field alone does not settle the
    judgment (2026-09-30, adversary pass on this change).

    WHY THE EVIDENCE IS REQUIRED HERE AND NOT LEFT TO THE NEXT GATE. This
    judgment is the only step on the exit path that DROPS an exit for
    paperwork. `exit_trigger.check_exit_trigger` refuses nothing — its own
    docstring says so — and the pipeline files its post-re-ask finding with
    `dropped=False`. So "naming and substantiating are separate gates" is
    true of the code and false of the consequence: the second gate never
    refuses, and an enum value typed into a field with nothing behind it
    would have been the whole of the requirement. Measured on the first cut
    of this change, the reason "Concentration drift; valuation stretched;
    taking profits at target." — the verbatim shape the comment block above
    `pipeline._HARD_TRIGGER_KEYWORDS` records as DELIBERATELY REJECTED
    (2026-05-04 AMZN double-trim), plus a target rationale the owner has
    ruled is never a trigger — classified ``unnamed`` without the field and
    ``named`` with `exit_trigger="adverse_news"` and no evidence at all.

    The evidence test is `exit_trigger._evidence_is_substantiation`, reused
    rather than reinvented: it is not a length test and not a quality
    judgement and carries no threshold — it strips the trigger's own
    phrases from the evidence and asks whether anything is left. Requiring
    the PROSE to agree with the field was the alternative and was rejected:
    it would make the typed field worthless, since the seat would still
    have to recite a sanctioned phrase in the sentence, which is the defect
    this whole change exists to remove.

    A field with no usable evidence does not fail the exit here — it simply
    does not settle the judgment, and the PROSE gate is consulted exactly as
    before. The live META reason still passes, because it names
    `bearish_state_change` in the prose.

    Substantiation downstream is untouched: `check_exit_trigger` still
    re-asks, and `exit_guard.holding_discipline_claim_check` still blocks a
    trigger the desk's records affirmatively contradict.
    `cannot_substantiate` is not a trigger and never settles the judgment.

    * ``named`` — a sanctioned `ExitTrigger` is in the structured field AND
      `trigger_evidence` says something beyond the trigger's own phrases, or
      the matcher ran and found a recognised trigger in the prose.
    * ``unnamed`` — the matcher ran, or the reason is missing/not a
      string, and no recognised trigger was found. A missing reason is a
      completed "no", not uncertainty: nothing was named. The owner
      refuses. Empty string is the same judgment.
    * ``uncertain`` — the matcher itself raised. That is the only case
      this layer cannot produce a judgment, and it fails OPEN, matching
      a dead Risk Manager. Agent application of the 2026-08-27
      posture, not a new owner ratification of a broader fail-open.
    """
    try:
        if trigger is not None:
            from src.risk.exit_trigger import (
                ExitTrigger,
                _evidence_is_substantiation,
                normalize_trigger,
            )

            named = normalize_trigger(trigger)
            if (
                named is not None
                and named is not ExitTrigger.CANNOT_SUBSTANTIATE
                and _evidence_is_substantiation(trigger_evidence, reason, named)
            ):
                return "named"
        if not isinstance(reason, str) or not reason:
            return "unnamed"
        if cites(reason):
            return "named"
        return "unnamed"
    except Exception as exc:  # noqa: BLE001 — matcher failure is uncertainty
        record_guarded_pass(NO_LEDGER, "exit_refusal.matcher", exc)
        return "uncertain"


def record_exit_refusal(
    db: Any,
    *,
    symbol: str,
    run_id: str,
    action: str,
    code: str,
    dropped: bool,
    detail: str,
    layer: str,
    owner: str = REFUSAL_OWNER,
) -> None:
    """Append one per-symbol refusal/uncertainty row. Never raises.

    Written to ``specialist_evidence`` rather than ``intraday_evaluations``
    because the latter upserts on ``(symbol, run_id)`` and a later gate
    on the same symbol would erase this one. A forensic-write failure
    must not change whether the exit is dropped — callers decide that
    before calling, and this function swallows persistence errors.
    """
    symbol_u = (symbol or "").strip().upper()
    if not symbol_u or not run_id:
        return
    payload = {
        "action": str(action or "").upper(),
        "code": str(code),
        "dropped": bool(dropped),
        "detail": str(detail or "")[:500],
        "layer": str(layer),
        "owner": str(owner),
        "uncertainty_fail": UNCERTAINTY_FAIL,
    }
    try:
        db.insert_specialist_evidence(
            run_id=str(run_id),
            agent_name="pipeline",
            kind=EXIT_REFUSAL_KIND,
            scope="symbol",
            symbol=symbol_u,
            evidence_json=json.dumps(payload),
        )
    except Exception as e:  # noqa: BLE001
        logger.warning(
            "exit refusal: durable record failed for %s %s (%s) — the drop/proceed decision is unchanged",
            action,
            symbol_u,
            e,
        )
