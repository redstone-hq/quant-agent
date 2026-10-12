"""Owner-facing rotation pre-check lines (lifted verbatim from src/rotation.py)."""

from __future__ import annotations

from src.rotation_parts.constraints import funding_view_measured
from src.rotation_parts.reporting import (
    ROTATION_FULL_NOTHING_BETTER,
    ROTATION_FULL_OPPORTUNITY,
    ROTATION_HOLDING_BELOW_BAR,
    ROTATION_ROOM_AVAILABLE,
    ROTATION_TELEMETRY_UNAVAILABLE,
    _pct,
)
from src.rotation_unrecorded import empty_pass_lines


def _full_book_cause(record: dict, *, headroom: str, ceiling: str, floor: str) -> str:
    """Why the book is full, in the owner's words, naming the limit that
    actually stopped it.

    Both limits can bind at once and then both are said, because "we are out
    of risk budget" and "we are out of cash" are different problems with
    different answers and collapsing them would hide whichever the owner
    could do something about.
    """
    binding = [b for b in str(record.get("binding") or "").split(",") if b]
    causes: list[str] = []
    if "risk_budget" in binding:
        causes.append(
            f"only {headroom} of risk headroom left under the desk's "
            f"{ceiling} ceiling, below the {floor} smallest position this "
            "desk will trade"
        )
    if "funding" in binding:
        budget = record.get("entry_budget_usd")
        minimum = record.get("min_entry_usd")
        causes.append(
            f"only ${float(budget):,.0f} of cash and borrowing room left, "
            f"below the ${float(minimum):,.0f} that the owner's 0.5% minimum risk per position needs"
            if isinstance(budget, (int, float)) and isinstance(minimum, (int, float))
            else "no cash or borrowing room left to open a new position"
        )
    if not causes:
        # An older row, written before `binding` existed. Say what that row
        # can support rather than inventing a cause for it.
        causes.append(f"only {headroom} of risk headroom left under the desk's {ceiling} ceiling")
    return " and ".join(causes) + "."


def owner_precheck_lines(record: dict | None) -> list[str]:
    """The pre-check said to the owner, in plain words, on a phone.

    NOT a warning and never rendered as one. A full book is a normal
    operating state of this desk — the owner asked specifically that it read
    that way — so the only thing this block reports is what was compared and
    what the comparison concluded.
    """
    if not isinstance(record, dict) or not record:
        return []
    outcome = str(record.get("outcome") or "")
    headroom = _pct(record.get("headroom_pct"))
    ceiling = _pct(record.get("ceiling_pct"))
    floor = _pct(record.get("floor_pct"))

    if outcome == ROTATION_TELEMETRY_UNAVAILABLE:
        return [
            "🔄 Rotation check: not run this session — the book's own risk "
            "figures could not be read, so nothing was compared. Nothing "
            "was sold or bought because of this."
        ]
    if outcome == ROTATION_ROOM_AVAILABLE:
        # Adversary review 2026-09-23: only claim the cash side when it was
        # actually read. Asserting "enough cash to open a new position" from
        # a figure that came back unreadable is the same silence this change
        # exists to remove, wearing a reassuring sentence.
        if funding_view_measured(
            record.get("entry_budget_usd"),
            record.get("min_entry_usd"),
        ):
            return [
                f"🔄 Rotation check: not needed — there is still {headroom} "
                f"of risk headroom under the desk's {ceiling} ceiling and "
                "enough cash and borrowing room to open a new position, so "
                "nothing had to be sold to make room for a new idea."
            ]
        return [
            f"🔄 Rotation check: not needed on risk — there is still "
            f"{headroom} of risk headroom under the desk's {ceiling} "
            "ceiling. The desk could NOT read how much cash and borrowing "
            "room it had this session, so that side was not checked and "
            "nothing was sold on it."
        ]

    # 2026-09-23. Name the limit that is ACTUALLY binding. Before this the
    # sentence always quoted risk headroom against the risk ceiling, and
    # would have gone on quoting it while the real cause was $92 of cash
    # against a $500 minimum order — a true-sounding sentence about the
    # wrong number.
    #
    # 2026-10-01. And only say it is full when a limit actually binds. The
    # categorical tier can now reach this block with every limit slack, and
    # "the book is FULL" was being asserted there from the outcome name
    # alone — true of the outcome's original case, false of the new one.
    constrained = bool([b for b in str(record.get("binding") or "").split(",") if b])
    if constrained:
        full = (
            "🔄 Rotation check: the book is FULL — "
            + _full_book_cause(
                record,
                headroom=headroom,
                ceiling=ceiling,
                floor=floor,
            )
            + " This is a normal state, not a fault."
        )
    else:
        full = (
            f"🔄 Rotation check: the book has room — {headroom} of risk "
            f"headroom under the desk's {ceiling} ceiling, and no limit is "
            "stopping the desk opening a new position."
        )

    if outcome == ROTATION_HOLDING_BELOW_BAR:
        held = str(record.get("held_symbol") or "").upper()
        reasons = [s for s in str(record.get("held_reasons") or "").split(",") if s]
        why = "; ".join(reasons) if reasons else ("it no longer clears the desk's own entry bar")
        subject = held if held else "A holding"
        lines = [
            full,
            f"   {subject} is up to be cut on its own merits: it would not "
            f"be bought today — {why}. Nothing outranked it; there was no "
            "candidate on the other side of this at all.",
            "   Whatever this frees stays in the book as cash, because "
            "nothing was ready to replace it. Room was never the reason "
            "for the cut.",
        ]
        if not record.get("execute_enabled"):
            lines.append(
                "   The desk is not switched on to act on this by itself, "
                "so this is information only — nothing was sold."
            )
        return lines

    if outcome == ROTATION_FULL_NOTHING_BETTER:
        return [
            full,
            "   Every new candidate was still ranked against what is "
            "already held, and none of them beat a holding by enough to be "
            "worth selling one for. The desk is keeping what it has on "
            "stronger conviction.",
        ]
    if outcome != ROTATION_FULL_OPPORTUNITY:
        return []

    held = str(record.get("held_symbol") or "").upper()
    new = str(record.get("new_symbol") or "").upper()
    tier = str(record.get("tier") or "")
    # 2026-10-01. Never render a placeholder to the owner: if either side of
    # the comparison is absent the sentence naming it cannot be written, so
    # it is not written. Nor claim the holding was "using the room" when no
    # limit was binding — it was not competing for anything.
    if not (held and new):
        return [full]
    weakest = ", the weakest thing currently using the room" if constrained else ""
    lines = [
        full,
        f"   Every new candidate was ranked against what is already held, "
        f"and one comparison came out the other way: {new} outranks {held}"
        f"{weakest}.",
    ]
    if not record.get("execute_enabled"):
        lines.append(
            "   The desk is not switched on to act on this by itself, so this is information only — nothing was sold."
        )
    elif tier == "ranked_margin" and not record.get("ranked_margin_enabled"):
        lines.append(
            "   This is the score-margin kind of comparison, which the desk "
            "is not switched on to act on. It was not weighed and declined "
            "— it was never put to the desk to act on at all."
        )
    return lines


def pruning_pass_lines(record: dict | None) -> list[str]:
    """Board item 219 — the pruning pass said out loud, every session.

    The defect this closes: the pass ran every session and reported
    nowhere, so a session that examined the whole book and kept all of it
    looked exactly like a session in which the pass never ran. Everything
    below is read straight off the durable row `precheck_record` wrote; no
    number here is derived, and none is invented.
    """
    if not isinstance(record, dict) or not record:
        return []
    outcome = str(record.get("outcome") or "")
    if outcome == ROTATION_TELEMETRY_UNAVAILABLE:
        # The pass genuinely did not run. Saying how many holdings it
        # "examined" would be the untrue line this item exists to remove.
        return []
    examined = [s for s in str(record.get("held_examined") or "").split(",") if s]
    count = int(record.get("held_examined_count") or 0)
    below = [s for s in str(record.get("held_below_entry_bar") or "").split(",") if s]
    reasons = [s for s in str(record.get("held_reasons") or "").split(",") if s]
    cut = str(record.get("held_symbol") or "").upper()
    tier = str(record.get("tier") or "")

    if count == 0:
        return empty_pass_lines(record) + _tier_two_line(record)

    noun = "holding" if count == 1 else "holdings"
    lines = [f"\u2702\ufe0f Pruning pass: ran, and examined all {count} {noun} the desk holds ({', '.join(examined)})."]
    # (b) Anything cut, and the CONVICTION reason it was cut on. The
    # categorical tier cuts a name because it no longer clears the desk's
    # own entry bar — never because it is down.
    if cut and tier == "ineligible_hold":
        why = "; ".join(reasons) if reasons else ("it no longer clears the desk's own entry bar")
        lines.append(
            f"   Put up to be cut: {cut} \u2014 {why}. That is a conviction "
            "reason, not a profit-or-loss one: the case for holding it is "
            "the thing that has gone."
        )
    # (c) The names it considered and KEPT. A silent pass is
    # indistinguishable from a pass that never ran.
    kept = [s for s in examined if s != cut and s not in below]
    if kept:
        lines.append(
            f"   Considered and kept: {', '.join(kept)} \u2014 each still "
            "clears the bar it was bought on, so the case for holding it "
            "stands."
        )
    if below:
        lines.append(
            f"   Below the desk's own entry bar today, and would not be bought now: {', '.join(below)}. "
            f"Not cut this pass: {', '.join(s for s in below if s != cut) or 'none'}."
        )
    else:
        lines.append(
            "   None of them has fallen below the desk's own entry bar, so "
            "the pass had nothing it was permitted to cut."
        )
    return lines + _tier_two_line(record)


def _tier_two_line(record: dict) -> list[str]:
    """(d) Never let the owner believe the desk pruned more thoroughly than
    it did. Reads the switch as recorded, so it tells the truth either way.
    """
    if record.get("ranked_margin_enabled"):
        return [
            "   Both kinds of pruning are switched on: the entry-bar kind, "
            "and the score-margin kind that compares a holding against a "
            "better-ranked new idea."
        ]
    return [
        "   Only the entry-bar kind of pruning is switched on. The "
        "score-margin kind \u2014 cutting a holding merely because a new "
        "idea ranks higher by some margin \u2014 is OFF, because the "
        "margin it would need has no source. So the desk pruned less "
        "thoroughly than it could, on purpose."
    ]
