"""The conviction bar (R7) — the desk archetype, owner 2026-10-19.

`docs/OUTCOME.md`, "The desk archetype": specialists are WEIGHED, the head
trader (the planning AI) decides and answers the strongest dissent in
writing, macro is the backdrop that tilts size. This module is the yes/no
part of that picture, read off the seats' verdicts for one name:

  * ADMISSION (`own_bar_block_reason`): the chart confirms the trade's
    direction AND at least one NAME-LEVEL seat (news, earnings, smart money)
    took a supported side the same way. Macro never counts as a supporter.
  * OBJECTIONS (`seat_objections`): a name-level seat pointing the OTHER way
    no longer blocks. It is an objection, machine-readable per symbol
    (seat, direction, reason), carried to the planning AI. A buy over it
    proceeds only when the AI's output carries an answer with decision
    "overrule" for that symbol and seat (`objection_is_overruled`); otherwise
    the name is refused as `OBJECTION_UNANSWERED`.
  * KEEP (`own_bar_hold_reason`, `unanswered_objections`): a holding is kept
    while the chart is not against it AND every opposing name-level seat has
    an overrule on record. A catalyst is an ENTRY requirement only: a quiet
    news seat never sells. An unanswered objection on a holding goes to the
    position reviewer to answer or sell; it is never a mechanical sale.

Retired 2026-10-19 (were owner rulings of 25 Sep and 9 Oct): "no seat may be
opposed" as an outright block, macro's sector-stance majority vote, and the
"macro is the only supporter" check (macro no longer supports at all).

WHY THIS INTRODUCES NO ARBITRARY NUMBER. Counts, booleans and non-empty
checks only: "at least one" is existence, "every objection answered" is all,
the chart veto is a boolean. Shorts mirror longs exactly.
"""

from typing import get_args

from src.models import AnalystVerdict
from src.models.positions import ObjectionSeat


#: Reasons these functions emit all start with this tag, the SAME string
#: `src.rotation.CONVICTION_BAR_REASON_PREFIX` matches on, so the STAY cull
#: (rotation's `ineligible_hold` tier) recognises exactly these reasons and
#: `holdings_below_entry_bar` counts them. A test pins the two equal.
OWN_BAR_REASON_PREFIX = "R7 conviction bar"

#: The seats that speak about the NAME itself (macro speaks about the market
#: or a sector; technical is the chart). Only these can support an entry, and
#: only these can object.
NAME_LEVEL_SEATS = get_args(ObjectionSeat)

#: Refusal code for a buy over an objection the planning AI did not overrule
#: in writing, and the basis for a holding with an objection not answered.
OBJECTION_UNANSWERED = "objection_unanswered"

#: Desk archetype step A (owner, 19 Oct 2026): the specialist-evidence kind
#: that holds a deciding AI's written answers to seat objections, one row
#: per symbol per decision. Read (step B) by the keep rule: an objection
#: with a written overrule on record does not send a holding to review.
OBJECTION_ANSWER_KIND = "objection_answer"


def _sides(direction: str) -> tuple[str, str]:
    """(aligned, opposed) verdict directions for a trade direction. A long is
    supported by a bullish verdict, a short by a bearish one."""
    if direction in ("bearish", "short"):
        return "bearish", "bullish"
    return "bullish", "bearish"


def _has_supported_directional_thesis(v: "AnalystVerdict", aligned: str) -> bool:
    """A NON-technical seat that took a SUPPORTED DIRECTIONAL side.

    What this checks, and all it checks: the seat took a DIRECTIONAL side
    (not a neutral shrug), which `AnalystVerdict`'s own validator then FORCES
    to carry a non-empty invalidation condition AND at least one checkable
    evidence item. It is NOT a test for a genuinely specific or fresh thesis:
    News and Smart-money always synthesise their invalidation, so it cannot
    tell a templated falsifier from an analyst-authored one. The caller
    restricts it to `NAME_LEVEL_SEATS`; technical is the chart veto.
    """
    return (
        v.seat != "technical" and v.direction == aligned and bool((v.invalidation or "").strip()) and bool(v.evidence)
    )


def _is_broadcast_macro_verdict(v: "AnalystVerdict") -> bool:
    """True for a MACRO verdict whose direction is the MARKET-WIDE
    `equity_outlook` broadcast, not a sector-specific stance (no
    `sector_stance:<sector>` evidence label, see `MacroAnalysis.to_verdict`).

    Since 2026-10-19 neither kind of macro verdict supports, objects or
    blocks (macro tilts size only); this stays as the one reader of the
    distinction for the prompt and the records."""
    if v.seat != "macro":
        return False
    return not any(str(getattr(ev, "label", "") or "").startswith("sector_stance:") for ev in (v.evidence or []))


def _chart_against_reason(seat_verdicts, opposed: str) -> str | None:
    if any(v.seat == "technical" and v.direction == opposed for v in seat_verdicts):
        return f"{OWN_BAR_REASON_PREFIX} — technical opposed; chart hostile to the trade"
    return None


def own_bar_block_reason(
    seat_verdicts: list["AnalystVerdict"],
    *,
    direction: str,
) -> str | None:
    """ADMISSION, as one yes/no: `None` when the name clears it for
    `direction`, else the one-line reason it does not.

    Clears iff BOTH:

      1. The chart CONFIRMS: a technical read is present and points the
         trade's way. Absent, neutral or opposed each refuse an ENTRY
         (the chart is the heaviest-weighted view); a holding is judged without timing
         (`own_bar_hold_reason`).
      2. At least one NAME-LEVEL seat (news, earnings, smart money) took a
         supported side the same way (`_has_supported_directional_thesis`).
         Macro never counts: it tilts size only.

    An opposing name-level seat does NOT refuse here; it is an objection
    (`seat_objections`) the planning AI must overrule in writing.
    """
    aligned, opposed = _sides(direction)
    tech = [v for v in seat_verdicts if v.seat == "technical"]
    if not tech:
        return (
            f"{OWN_BAR_REASON_PREFIX} — no technical read this review; "
            "the chart is the heaviest-weighted view for this hold and nothing is bought without it"
        )
    against = _chart_against_reason(tech, opposed)
    if against:
        return against
    if not any(v.direction == aligned for v in tech):
        return (
            f"{OWN_BAR_REASON_PREFIX} — chart neutral/broken: the heaviest-weighted view "
            "does not back the trade's direction, and nothing is bought without it"
        )
    if not any(v.seat in NAME_LEVEL_SEATS and _has_supported_directional_thesis(v, aligned) for v in seat_verdicts):
        return (
            f"{OWN_BAR_REASON_PREFIX} — no name-level seat (news, earnings, "
            "smart money) took a supported side in the trade's direction"
        )
    return None


def _objection_reason(v) -> str:
    texts = [str(getattr(e, "text", "") or getattr(e, "label", "") or "").strip() for e in (v.evidence or [])]
    reason = "; ".join(t for t in texts if t) or str(getattr(v, "invalidation", "") or "").strip()
    return reason[:300] or "no reason stated"


def seat_objections(seat_verdicts, *, direction: str) -> list[dict]:
    """Every NAME-LEVEL seat pointing against `direction`, one machine-
    readable objection each: `{"seat", "direction", "reason"}`, sorted by
    seat. Reads only `.seat`, `.direction` and (for the reason) `.evidence`
    / `.invalidation`. Macro and technical never object."""
    _, opposed = _sides(direction)
    seen: dict[str, dict] = {}
    for v in seat_verdicts or ():
        if v.seat in NAME_LEVEL_SEATS and v.direction == opposed and v.seat not in seen:
            seen[v.seat] = {"seat": v.seat, "direction": opposed, "reason": _objection_reason(v)}
    return [seen[s] for s in sorted(seen)]


def objection_is_overruled(objection: dict, answer) -> bool:
    """True when `answer` (an `ObjectionAnswer` or its dict) overrules THIS
    objection: same seat, same objection direction, decision "overrule"."""
    if answer is None:
        return False
    if isinstance(answer, dict):
        seat, side, decision = answer.get("seat"), answer.get("objection_direction"), answer.get("decision")
    else:
        seat = getattr(answer, "seat", None)
        side = getattr(answer, "objection_direction", None)
        decision = getattr(answer, "decision", None)
    return (
        str(seat or "").strip().lower() == objection["seat"]
        and str(side or "").strip().lower() == objection["direction"]
        and str(decision or "").strip().lower() == "overrule"
    )


def unanswered_objections(seat_verdicts, *, direction: str, answer_for) -> list[dict]:
    """The objections (`seat_objections`) with no overrule on record.
    `answer_for(seat)` returns the answer on record for that seat, or None."""
    return [
        o
        for o in seat_objections(seat_verdicts, direction=direction)
        if not objection_is_overruled(o, answer_for(o["seat"]))
    ]


def own_bar_opposition_reason(
    seat_verdicts: list["AnalystVerdict"],
    *,
    direction: str,
) -> str | None:
    """The STAY cull test: `None` unless the CHART is against the held
    `direction`. Since 2026-10-19 that is the only definite reason a holding
    fails the bar; an opposing name-level seat is an objection for the
    position reviewer (`unanswered_objections`), never a cull, and macro is
    never opposition. SALES are decided by `src/exits/same_rules_gate.py`."""
    _, opposed = _sides(direction)
    return _chart_against_reason(seat_verdicts, opposed)


def own_bar_hold_reason(
    seat_verdicts: list["AnalystVerdict"],
    *,
    direction: str,
) -> str | None:
    """The KEEP rule's definite part for a name the desk HOLDS: fails only
    when the chart is against it. No chart confirmation and no catalyst is
    needed to stay (a catalyst is entry-only; a quiet news seat never sells).
    The other half of the keep rule — every objection answered — is
    `unanswered_objections`, and an unanswered one goes to the reviewer."""
    return own_bar_opposition_reason(seat_verdicts, direction=direction)


def objection_answer_for(db, symbol: str, seat: str, *, run_id: str | None = None) -> dict | None:
    """The newest written answer on file for `symbol` to `seat`'s objection.

    Reads the newest `OBJECTION_ANSWER_KIND` row for the symbol; with
    `run_id`, only a row from that run counts. Returns the answer dict
    (seat, objection_direction, answer_text, decision) plus the row's
    run_id, session, decision_id and timestamp, or None."""
    import json as _json

    sym = str(symbol).strip().upper()
    row = (db.get_latest_symbol_evidence(OBJECTION_ANSWER_KIND, [sym]) or {}).get(sym)
    if not row or (run_id is not None and row.get("run_id") != run_id):
        return None
    try:
        payload = _json.loads(row["evidence_json"])
    except (TypeError, ValueError):
        return None
    want = str(seat).strip().lower()
    for answer in payload.get("answers") or []:
        if str(answer.get("seat", "")).strip().lower() == want:
            return {
                **answer,
                "run_id": row.get("run_id"),
                "session": payload.get("session"),
                "decision_id": payload.get("decision_id"),
                "timestamp": row.get("timestamp"),
            }
    return None
