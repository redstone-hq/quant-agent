"""Make the portfolio manager account for every candidate it was shown.

THE DEFECT THIS CLOSES (board item 133, 2026-09-18)
------------------------------------------------------------------------
The seat returned a list of TARGETS and nothing else. `DecisionStage` then
recorded every analysed candidate absent from that list as

    portfolio_manager | omitted | candidate_not_selected_for_target

The seat was never ASKED why it dropped a name. So there was no
per-candidate reason and there could not be one: the ABSENCE of a reason
was the reason, identically, for every name in every session.

That defeats the jam detector (`src/refusal_signature.py`) by
construction. Its whole trigger is "every candidate died for the SAME
reason while the candidates changed" — a jammed gate looks like that, a
quiet market does not, because a quiet market kills different names for
different reasons. With one unvarying code available to it, the two cases
were indistinguishable, and on 2026-09-18 it fired on two sessions and
three candidates (CMCSA; CRM, GME) reporting the only key it could ever
have reported.

WHAT THIS MODULE DOES, AND WHAT IT DELIBERATELY DOES NOT
------------------------------------------------------------------------
It classifies. It decides nothing. No gate, threshold, size, eligibility
rule or trade-governing number reads anything here, and a name's
accounting outcome never changes whether it can be bought — a target is a
target whatever this module says about the names beside it.

The order is the desk's standing heal order (owner 2026-09-16,
`src/seat_heal`), with no step skipped and no new retry invented:

  1. **Mechanical heal, from what the seat DID say.** Two sources, both
     mechanical and neither inventing a reason:
       * a rejection the seat stated for that symbol (shape-coerced by
         `CandidateRejection`, which already tolerates the aliases and
         casings a model reaches for); and
       * a HELD name the seat left out, which the seat's own prompt
         defines as meaning "hold unchanged" — a stated rule read back,
         not a guess. Recorded as its own outcome so it is never confused
         with a refusal.
  2. **One re-ask**, bounded by the SAME one-paid-retry-per-seat-per-
     session cap the research seats and the exit-trigger re-ask already
     use (`seat_heal.can_paid_retry`). No new cap, no loop.
  3. **A durable per-symbol reason** for whatever the seat still will not
     account for: `unaccounted_after_reask`, naming the seat's refusal to
     account rather than pretending to know why the name was dropped.

WHY THE CODE AND THE PROSE GO IN DIFFERENT FIELDS
------------------------------------------------------------------------
`refusal_signature.signature_key` builds its comparable identity from
`stage`, `outcome`, `reason`, the named `refusal`/`fault` code, and
`detail`. The per-candidate prose must NOT reach that key: prose varies
with wording where the cause is identical, so a genuine jam whose
sentences differed would stop being visible as one. The code carries the
identity, in `refusal`, exactly as the deterministic gate's own refusals
do; the reader-facing sentence rides in `note`, which the key does not
read. The owner still sees it — `plain_reason` below is what renders it.

This can only make that alert QUIETER. Before, every candidate produced
one shared key and a session was always monomorphic; now a session is
monomorphic only when every candidate really did die on the same named
ground. The set of keys can only grow, and a streak can only shorten.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

from src.models import CANDIDATE_REJECTION_CODES

logger = logging.getLogger(__name__)

#: The stage every row below is filed under — the seat that owes the answer.
STAGE = "portfolio_manager"

#: Outcomes. Three, because the three cases are genuinely different and
#: collapsing them is what produced the original defect.
OUTCOME_NOT_SELECTED = "not_selected"  # the seat dropped it and said why
OUTCOME_HELD_UNCHANGED = "held_unchanged"  # held, left out, prompt says hold
OUTCOME_UNACCOUNTED = "unaccounted"  # the seat would not say at all

#: The `reason` half of each key — a rule name, never prose.
REASON_REJECTED = "pm_rejected_candidate"
REASON_HELD = "pm_left_holding_unchanged"
REASON_UNACCOUNTED = "pm_would_not_account_for_candidate"
#: A name the candidate ranking had already refused before the seat decided.
REASON_RANKING_BLOCKED = "candidate_ranking_blocked"

#: The named code for a name the seat still would not account for after the
#: one re-ask. Sits in the same `refusal=` slot as a stated rejection code,
#: so it is comparable with them and distinguishable from them.
CODE_UNACCOUNTED = "unaccounted_after_reask"
CODE_HELD_UNCHANGED = "held_unchanged"
CODE_RANKING_BLOCKED = "ranking_blocked"

#: The seat used to be given no chance to answer this at all, so the re-ask
#: says what is wanted and says plainly that "I am not taking it, and here
#: is the ground" is a complete and correct answer. It asks for BOOKKEEPING,
#: never for a different decision — re-opening the decision would make a
#: paid retry a way to relitigate the book, which it must never be.
REASK_DIRECTIVE = (
    "ACCOUNTING RE-ASK — this is a bookkeeping question, not an invitation "
    "to change your mind. Your previous answer targeted some names and left "
    "these ones out without saying why, and the desk cannot report to its "
    "owner why his candidate was dropped. Re-emit the SAME decision — the "
    "same targets, the same sizes, the same reasoning_chain — and add one "
    "entry to `rejections` for each symbol named here, giving the `code` "
    "you dropped it on and one sentence of `detail`. Deciding not to take a "
    "name is a correct and expected answer; what is not acceptable is "
    "silence about it. Account for: "
)


# ---------------------------------------------------------------------------
# owner-facing wording
# ---------------------------------------------------------------------------
#
# Board item 89 defect 5 — internal reason codes reaching the owner word for
# word, which is exactly what `portfolio_manager|omitted|candidate_not_
# selected_for_target||` was. Every value below is what the code MEANS, in
# the words a person would use, written to sit after "the desk did not take
# it because". The key is the internal spelling and never reaches a message.
# An unrecognised code is DESCRIBED, never pasted through and never guessed
# at — the same rule `_ORDER_END_WORDS` in `src/trader_feed.py` follows,
# because guessing what an unknown token means to someone reading it as a
# trading fact is the failure the rule exists to stop.
_PLAIN_BY_CODE: dict[str, str] = {
    "evidence_insufficient": "there was not enough current evidence behind it to justify risking money on it",
    "evidence_conflicts": "the evidence pointed both ways and the disagreement was not resolved",
    "evidence_stale": "the evidence that existed was too old to act on",
    "thesis_not_compelling": "the evidence was there but the trade it suggested did not earn a place in the book",
    "no_readable_structure": "there was no level on the chart to enter against or to be proved wrong by",
    "reward_not_worth_risk": "the money it stood to make did not justify the money it put at risk",
    "event_risk": "a known event — earnings or similar — was too close to enter in front of",
    "risk_budget_full": "the book had no risk budget left for another name",
    "no_deployment_headroom": "there was no cash or buying power left to put behind it",
    "sector_or_cluster_crowded": "the desk already holds too much that moves with it",
    "better_use_of_the_slot": "another candidate was judged the better use of the same slot",
    "already_sized_correctly": "it is already held at the size the desk thinks it deserves",
    "other": "the desk gave a reason that does not fit any of its standard "
    "categories — the detail below is that reason",
    # Candidate-ranking blocks (src/agents/portfolio_manager/candidate_ranking.py).
    # The code is "ranking_" + the rule's own id; the wording is what the rule means.
    "ranking_r2": "the chart reading was neutral — no buy or sell signal",
    "ranking_r3": "it was not on the list of names the desk is allowed to buy today",
    "ranking_r6": "the desk's own portfolio builder had already refused it before the portfolio manager decided",
    "ranking_r7": "too few of the desk's analysts agreed on it to clear the conviction bar",
    CODE_RANKING_BLOCKED: "the desk's candidate ranking had already blocked it before the "
    "portfolio manager decided — the detail below is that block",
    CODE_HELD_UNCHANGED: "it is already held and the desk decided to leave it exactly as it is",
    CODE_UNACCOUNTED: "the desk's portfolio manager would not say. It was asked once more "
    "and still did not give a ground. This is a fault in the desk, not a "
    "judgement about the stock",
}

# Every stated rejection code must have wording. A code with no sentence
# behind it would reach the owner as a raw token, which is the defect.
_MISSING = [c for c in CANDIDATE_REJECTION_CODES if c not in _PLAIN_BY_CODE]
if _MISSING:  # pragma: no cover - guarded by a test, kept as a live assert
    raise RuntimeError(
        f"candidate rejection codes with no plain-English wording: {_MISSING}",
    )


def plain_reason(code: str) -> str:
    """The reader-facing sentence for one rejection code.

    Never returns the raw token, and never invents a meaning for one it does
    not know.
    """
    known = _PLAIN_BY_CODE.get(str(code or "").strip().lower())
    if known:
        return known
    return "the desk recorded a ground for dropping it that it has no plain wording for"


# ---------------------------------------------------------------------------
# the accounting itself
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AccountedCandidate:
    """One candidate, and how the seat accounted for it."""

    symbol: str
    outcome: str
    reason: str
    code: str
    note: str

    def event_kwargs(self) -> dict:
        """The `_record_pipeline_event` payload for this candidate.

        `refusal` carries the comparable code and `note` the prose, for the
        reason set out in the module docstring: the jam detector's key reads
        `refusal` and does NOT read `note`.
        """
        return {
            "stage": self.stage_name,
            "outcome": self.outcome,
            "reason": self.reason,
            "refusal": self.code,
            "note": self.note,
        }

    @property
    def stage_name(self) -> str:
        return STAGE


@dataclass
class AccountingResult:
    accounted: list[AccountedCandidate] = field(default_factory=list)
    unaccounted: list[str] = field(default_factory=list)

    @property
    def symbols(self) -> list[str]:
        return [a.symbol for a in self.accounted] + list(self.unaccounted)


def _norm(value) -> str:
    return str(value or "").strip().upper()


def account_for_candidates(
    *,
    analyses,
    decision,
    positions=None,
    blocked=None,
) -> AccountingResult:
    """Classify every analysed candidate the decision does not target.

    Pure and side-effect free — it reads, it never writes, never calls a
    model and never touches the broker. `blocked` is the candidate ranking's
    own `{symbol: [reasons]}` refusals: a blocked, non-held name the seat did
    not target is recorded with the block's OWN reason, never one the seat is
    asked to restate (it used to pick an unrelated code for them). `unaccounted` is what step 2 (the
    one re-ask) is for; everything in `accounted` already has a real,
    per-candidate ground behind it.
    """
    targeted = {_norm(getattr(t, "symbol", None)) for t in (getattr(decision, "targets", None) or [])}
    targeted.discard("")
    held = {
        _norm(getattr(p, "symbol", None))
        for p in (positions or [])
        if _norm(getattr(p, "symbol", None)) and getattr(p, "qty", 0) not in (0, 0.0, None)
    }
    stated: dict[str, object] = {}
    for rejection in getattr(decision, "rejections", None) or []:
        sym = _norm(getattr(rejection, "symbol", None))
        if sym:
            stated.setdefault(sym, rejection)

    blocks = {
        _norm(sym): [str(r) for r in (why if isinstance(why, (list, tuple)) else [why]) if str(r).strip()]
        for sym, why in (blocked or {}).items()
    }
    result = AccountingResult()
    seen: set[str] = set()
    for analysis in analyses or []:
        symbol = _norm(getattr(analysis, "symbol", None))
        if not symbol or symbol in targeted or symbol in seen:
            continue
        seen.add(symbol)
        rejection = stated.get(symbol)
        if blocks.get(symbol) and symbol not in held:
            why = blocks[symbol]
            match = re.match(r"R(\d+)\b", why[0])
            code = f"ranking_r{match.group(1)}" if match else CODE_RANKING_BLOCKED
            if code not in _PLAIN_BY_CODE:
                code = CODE_RANKING_BLOCKED
            result.accounted.append(
                AccountedCandidate(
                    symbol=symbol,
                    outcome=OUTCOME_NOT_SELECTED,
                    reason=REASON_RANKING_BLOCKED,
                    code=code,
                    note="; ".join(why)[:400],
                )
            )
            continue
        if rejection is not None:
            result.accounted.append(
                AccountedCandidate(
                    symbol=symbol,
                    outcome=OUTCOME_NOT_SELECTED,
                    reason=REASON_REJECTED,
                    code=str(getattr(rejection, "code", "") or "other"),
                    note=str(getattr(rejection, "detail", "") or "")[:400],
                )
            )
            continue
        if symbol in held:
            # MECHANICAL, from a rule the seat's own prompt states: "Held
            # symbols NOT in your targets list -> held unchanged." Reading
            # that rule back is not inventing a reason, and it is why an
            # ordinary quiet session over a full book does not spend a paid
            # re-ask on names nobody proposed changing.
            result.accounted.append(
                AccountedCandidate(
                    symbol=symbol,
                    outcome=OUTCOME_HELD_UNCHANGED,
                    reason=REASON_HELD,
                    code=CODE_HELD_UNCHANGED,
                    note=(
                        "the seat left a held name out of its targets, which its "
                        "own instructions define as leaving the position alone"
                    ),
                )
            )
            continue
        result.unaccounted.append(symbol)
    return result


def unaccounted_row(symbol: str, *, asked: bool) -> AccountedCandidate:
    """The durable last-resort row for a name the seat would not account for.

    `asked=False` records the same fact when the one re-ask could not be
    spent at all (cap already used, or a spend cap blocked it) — the name is
    still recorded with a real reason, and the reason says which of the two
    happened rather than implying the seat was re-asked when it was not.
    """
    return AccountedCandidate(
        symbol=_norm(symbol),
        outcome=OUTCOME_UNACCOUNTED,
        reason=REASON_UNACCOUNTED,
        code=CODE_UNACCOUNTED,
        note=(
            "the portfolio manager did not name a ground for dropping this "
            "candidate"
            + (
                ", and did not name one when it was asked again"
                if asked
                else "; the one re-ask this seat gets was already spent or blocked "
                "this session, so it was not asked again"
            )
        ),
    )
