"""Owner-facing rotation pre-check lines (lifted verbatim from src/rotation.py)."""

from __future__ import annotations

import re
from collections.abc import Iterable

from src.pm_accounting import plain_reason
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


def _plain_held_reasons(raw: object) -> list[str]:
    """The recorded ranking reasons as the owner reads them: the plain-word
    sentence `pm_accounting` already uses for the passed-names list, never
    the rule code. The record joins reasons with commas and the held-neutral
    text itself contains one, so it is swapped out whole before splitting.
    Display only: the AI is still told the original text."""
    from src.agents.portfolio_manager.candidate_ranking import HELD_NEUTRAL_R2  # lazy: import cycle

    text = str(raw or "").replace(HELD_NEUTRAL_R2, "R2 neutral rating")
    out: list[str] = []
    for part in (p.strip() for p in text.split(",")):
        if not part:
            continue
        match = re.match(r"R(\d+)\b", part)
        out.append(plain_reason(f"ranking_r{match.group(1)}") if match else part)
    return out


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


def _was_sold(symbol: str, sold: Iterable[str] | None) -> bool | None:
    """True/False when the caller passed the session's sells; None when it
    did not, so no line claims an outcome it was not given."""
    if sold is None or not symbol:
        return None
    return symbol.upper() in {str(s).upper() for s in sold}


def _cut_outcome_sentence(symbol: str, sold: Iterable[str] | None, *, execute_enabled: bool) -> str:
    """What actually became of a holding the pre-check put up to be cut."""
    if not execute_enabled:
        return (
            "The desk is not switched on to act on this by itself, so this "
            "is information only — nothing was sold."
        )
    was_sold = _was_sold(symbol, sold)
    if was_sold is None:
        return f"Whether {symbol} was sold is recorded with the session's orders, not here."
    if was_sold:
        return (
            f"{symbol} was sold this session; what that freed stays in the "
            "book as cash."
        )
    return f"The planning AI kept {symbol}; nothing was sold."


def owner_precheck_lines(record: dict | None, *, sold: Iterable[str] | None = None) -> list[str]:
    """The pre-check said to the owner, in plain words, on a phone.

    `sold` is the set of symbols the session actually sold (from its orders);
    `None` means the caller does not know, and the lines then claim neither
    outcome.

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
        # The record carries `new_symbol` (None) when the session had no
        # unheld candidate to size, so no cash figure was ever needed. That
        # is not a read failure. A record with no such key says nothing
        # about candidates and keeps the unreadable wording.
        if "new_symbol" in record and not record.get("new_symbol"):
            return [
                f"🔄 Rotation check: not needed — there is still {headroom} "
                f"of risk headroom under the desk's {ceiling} ceiling, and "
                "no new idea needed room this session, so cash was not "
                "checked."
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
        reasons = _plain_held_reasons(record.get("held_reasons"))
        why = "; ".join(reasons) if reasons else ("it no longer clears the desk's own entry bar")
        subject = held if held else "A holding"
        # 2026-10-20. This is the PROPOSAL the pre-check made. What was
        # actually done with it is the session's own decision, so the
        # outcome sentence reads off the sell orders the caller passes in
        # — a live message once said UBER was "up to be cut" and that the
        # freed cash stayed in the book, on a session where the planning
        # AI kept UBER and sold nothing.
        return [
            full,
            f"   {subject} was put up to be cut on its own merits: it would not "
            f"be bought today — {why}. Nothing outranked it; there was no "
            "candidate on the other side of this at all, so nothing was "
            "ready to replace it.",
            "   " + _cut_outcome_sentence(subject, sold, execute_enabled=bool(record.get("execute_enabled"))),
        ]

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


def pruning_pass_lines(record: dict | None, *, sold: Iterable[str] | None = None) -> list[str]:
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
    reasons = _plain_held_reasons(record.get("held_reasons"))
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
            "the thing that has gone. "
            + _cut_outcome_sentence(cut, sold, execute_enabled=bool(record.get("execute_enabled")))
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
        # "Not cut" is a statement about what happened, not about what
        # was proposed: the proposed name counts as not cut unless the
        # session's orders say it was sold.
        was_sold = _was_sold(cut, sold)
        not_cut = [s for s in below if s != cut or was_sold is False]
        tail = ""
        if cut and was_sold is None:
            tail = f" Whether {cut} was sold is recorded with the session's orders, not here."
        lines.append(
            f"   Below the desk's own entry bar today, and would not be bought now: {', '.join(below)}. "
            f"Not cut this pass: {', '.join(not_cut) or 'none'}.{tail}"
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
