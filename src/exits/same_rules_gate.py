"""ONE gate for every JUDGEMENT sale — owner ruling 2026-10-13.

Owner, verbatim: "selling is not set up the same as buying and it should be.
Same rules should apply."

Owner ruling Option A, 14 Oct 2026: a stock the desk already HOLDS is kept
while it passes every buy rule EXCEPT timing. It is sold by judgement only when
its chart turns BROKEN or turns AGAINST the holding (or another buy rule
fails). A NEUTRAL chart read or a NEUTRAL rating does NOT sell it.

A holding may be sold, reduced or covered BY JUDGEMENT (the portfolio manager,
the midday/close position reviewer, the half-hour "earn its place" check) only
when it FAILS that bar. Seats may still PROPOSE a sale; only this gate decides
it.

The bar is the desk's existing entry bar minus timing, CALLED here, never
restated:

  * R2-R6 — `candidate_eligibility` (rating, BUY-eligible universe,
    constructor refusal; R5 net evidence retired 2026-10-19), read from THIS run's
    `ctx.held_bar_record` — built from the decision stage's DETERMINISTIC
    prompt assembly (`build_user_message`: candidate ranking, the conviction
    bar, the rotation pre-check), in the HOLDING's direction, on every run
    whether or not the paid manager answers (owner 12 Oct: the "earn its
    place" sale needs no paid call). No record (the assembly did not run, or
    failed) means no judgement sale. R2 fails only on a rating AGAINST the holding
    (sell for a long, buy for a short); a neutral rating holds, and the
    manager then reads R3-R6 in the holding's direction (`HELD_NEUTRAL_R2`).
  * R7 — the KEEP rule (desk archetype, owner 2026-10-19,
    `src/risk/conviction_bar.py`): a holding is kept while the chart is not
    against it (`own_bar_hold_reason`) AND every opposing name-level seat
    (news, earnings, smart money) has a written "overrule" on record
    (`unanswered_objections`, read through `objection_answer_for`). A
    catalyst is entry-only: a quiet news seat never sells. An UNANSWERED
    objection is not a mechanical sale: it goes to the position reviewer
    (`judge_holding(reviewer=True)`), who answers it or sells; every other
    judgement seller is refused (`BASIS_OBJECTION_UNANSWERED`). Macro never
    supports, objects or sells.
  * A CONFIRMED chart-structure break is the protective sale below, never
    gated.

Every refusal code is classified ONCE (`COUNTS_CODES`, `NEVER_CODES`,
`UNKNOWN_CODES`). Only a definite failure about the stock itself sells.
Account-level refusals (budget, gross ceiling, sizing, minimum risk, order
shape, book risk unknown) and target / profit-taking reasons NEVER sell; a
missing measurement, or a code nobody classified, is UNKNOWN and never sells.

Two readings of "unknown", each used where it means something:
  * a seat with no reading for the name this run
    (`failed_evidence_seats`, the ONE rule the half-hour check has always
    used) raises no objection and supports nothing; since 2026-10-19 no
    part of the keep rule needs a supporter, so a missing seat can never
    turn a keep into a sale.
A failure a missing reading could undo is not definite.

A PROTECTIVE sale is not judgement: a holding whose structural-protection
read is a CONFIRMED chart break (`PROTECTIVE_BREAK_BASES`) sells ungated, as
on trunk. Every other `protected=False` read goes to the gate: a loser with
no chart level under its stop (`no_chart_level` — owner 14 Oct 2026: its
sale must pass the buy-rule check; if it still passes, it is held and its
stop caps the loss), a failed bar fetch (`chart_unreadable`, unknown, not
broken), and any basis added later.

Three rules sit around the bar:

  1. SAME VIEWS. The holding is judged on the seat views the run already
     holds — the same ones a buy at that minute would be judged on, with the
     same freshness rules (stale earnings already excluded upstream). Nothing
     here asks a model again: re-asking on unchanged inputs only re-rolls a
     random answer, and a sale on a re-roll is a sale on sampling noise.
  2. MISSING EVIDENCE NEVER TRIGGERS A TRADE. A failure counts only when no
     missing view could undo it (a seat actively opposed is a positive
     reading a missing seat cannot manufacture or erase). If the bar cannot
     be evaluated, the holding is NOT sold by judgement: it keeps its stop,
     and the reason is recorded per symbol. A seat outage or a budget stop
     therefore cannot sell the book.
  3. NEVER SELL BY JUDGEMENT A NAME BOUGHT TODAY (rotation's existing
     `held_symbol_bought_today` refusal, applied to every judgement sale).
     An unreadable trade record refuses too (fail closed: no sale).

Mechanical exits never come here: broker stop, below-bar sale, trend /
alignment exit, drawdown ladder, forced de-lever, breached-stop closer and
take-profit each run on their own paths, unchanged.
"""

from __future__ import annotations

import ast
import logging
from dataclasses import dataclass

from src.exits.evidence_seats import failed_evidence_seats
from src.risk.conviction_bar import (
    NAME_LEVEL_SEATS,
    OBJECTION_UNANSWERED,
    OWN_BAR_REASON_PREFIX,
    own_bar_hold_reason,
    unanswered_objections,
)

logger = logging.getLogger("src.pipeline")

#: The actions a seat can take by judgement that reduce or close a holding.
JUDGEMENT_SALE_ACTIONS = ("SELL", "REDUCE", "COVER")

#: Trade actions that OPEN a position (long or short) — "bought today".
_OPENING_ACTIONS = ("BUY", "SHORT", "SELL_SHORT")

BASIS_FAILS_BAR = "fails_bar"
BASIS_PASSES_BAR = "passes_bar"
BASIS_EVIDENCE_MISSING = "evidence_missing"
BASIS_BOUGHT_TODAY = "bought_today"
BASIS_PROTECTIVE = "protective_structure_broken"
#: A holding the keep rule cannot keep because an opposing name-level seat
#: has no written overrule on record: the position reviewer's call.
BASIS_OBJECTION_UNANSWERED = OBJECTION_UNANSWERED

#: The ONLY `protected=False` bases that are a PROTECTIVE sale and sell
#: UNGATED: a structure break confirmed on its required closes is a chart
#: fact ("cut losers at pre-defined structure", docs/OUTCOME.md). These are
#: `src/risk/exit_guard.py`'s confirmed-break bases. An ALLOWLIST, so every
#: other unprotected basis is judged on the buy rules: `no_chart_level` (a
#: loser with no chart level under the stop — owner 14 Oct 2026),
#: `chart_unreadable` (a failed bar fetch; unknown never sells), and any
#: basis added later.
PROTECTIVE_BREAK_BASES = frozenset({"structural_level_broken", "thesis_invalid_if_triggered"})


def is_protective(unprotected_basis: str | None) -> bool:
    """True only for the basis of a CONFIRMED chart break (sold ungated)."""
    return unprotected_basis in PROTECTIVE_BREAK_BASES


# Refusal classification: every constructor refusal code exactly once
# (enumerated by `tests/test_sell_same_rules.py`).
#: A definite failure about the stock itself.
COUNTS_CODES = frozenset(
    {
        "no_valid_stop_against_entry",  # a stop MEASURED on the wrong side of entry
    }
)
#: Not about the stock: the account, the order's shape, or profit-taking.
NEVER_CODES = frozenset(
    {
        # The entry floor (desk archetype, 2026-10-19): a catalyst is an
        # ENTRY requirement; a holding is never sold for lacking one.
        "agreement_net_at_or_below_zero",
        "risk_budget_exhausted",
        "gross_exposure_ceiling_refused",
        "position_sized_to_zero",
        "below_owner_min_risk",
        "dollar_target_no_stop",
        "book_risk_unknown",
        "held_partial_trim_refused",
        "short_already_at_target_weight",
        "target_weight_zero_nothing_held",
        "trim_without_analysis_has_no_usable_live_stop",
        "delta_below_min_trade_weight",
        "target_not_above_entry",
        "target_not_below_entry",
        "no_structural_target",
        "no_structural_levels",
        "no_level_in_direction",
        "no_expected_horizon",
        "projection_implausible",
        # Retired, never raised; kept only to decode history.
        "stop_wider_than_instrument_reach",
        "structural_stop_past_sanity_bound",
        "sector_crowding_leaves_below_min_order",
        "reward_risk_below_floor_at_widened_stop",
        "reward_risk_below_floor_at_honoured_stop",
        "reward_risk_below_floor_at_kept_stop",
        "reward_risk_not_measurable",
    }
)
#: A measurement is missing or an input is wrong: never sells.
UNKNOWN_CODES = frozenset(
    {
        "stop_on_wrong_side_of_entry",
        "stop_emptied_without_recorded_reason",
        "stop_price_not_finite",
        "entry_price_not_finite",
        "no_stop_and_no_volatility_reading",
        "no_structural_stop_and_no_volatility_reading",
        "no_entry_price",
        "no_volatility_reading",
        "entry_price_missing",
        "volatility_reading_missing",
        "price_history_unusable",
        "sizing_price_missing",
        "sizing_price_stale",
        "analysis_missing",
    }
)

CLASS_COUNTS = "counts"
CLASS_NEVER = "never"
CLASS_UNKNOWN = "unknown"


def classify_refusal(code: str | None) -> str:
    """COUNTS / NEVER / UNKNOWN for one refusal code; unclassified = UNKNOWN."""
    code = str(code or "").strip()
    if code in COUNTS_CODES:
        return CLASS_COUNTS
    if code in NEVER_CODES:
        return CLASS_NEVER
    return CLASS_UNKNOWN


@dataclass(frozen=True)
class HoldingViews:
    """Everything a buy of `symbol` would be judged on this minute."""

    symbol: str
    is_short: bool
    #: This run's seat `AnalystVerdict`s (any symbol; filtered here).
    seat_verdicts: tuple = ()
    #: This run's R2-R6 reasons for the holding, in the HOLDING's direction.
    #: `None` = the entry rules were not evaluated this run (cannot judge).
    eligibility_reasons: tuple[str, ...] | None = None
    #: Extra seats whose reading for this name is unknown this run, on top of
    #: every non-technical seat with no verdict for it.
    unknown_seats: tuple[str, ...] = ()
    #: True/False from today's trade record; `None` = unreadable.
    bought_today: bool | None = False
    #: Symbols whose earnings stance is stale (no, or an over-age, filing date).
    stale_earnings: frozenset = frozenset()
    #: Basis of a `protected=False` structural read (None when protected or
    #: unread); only a confirmed break (`is_protective(basis)`) sells ungated.
    protection_basis: str | None = None
    #: The written objection answers on record for this name (dicts with
    #: seat, objection_direction, decision), newest per seat first
    #: (`objection_answers_from_db`). Empty = none on record.
    objection_answers: tuple = ()


@dataclass(frozen=True)
class SameRulesVerdict:
    symbol: str
    sell_allowed: bool
    basis: str
    reasons: tuple[str, ...]
    detail: str


def is_judgement_sale(action: str | None) -> bool:
    return str(action or "").strip().upper() in JUDGEMENT_SALE_ACTIONS


def fresh_verdicts(verdicts, symbol: str, stale_earnings=frozenset()) -> list:
    """This stock's verdicts, a stale earnings stance dropped before any rule
    reads it (the same `stale_evidence_sources` rule the tally uses)."""
    sym = str(symbol or "").strip().upper()
    stale = {str(s).strip().upper() for s in (stale_earnings or ())}
    out = []
    for v in verdicts or ():
        if str(getattr(v, "symbol", "")).strip().upper() != sym:
            continue
        if getattr(v, "seat", None) == "earnings" and sym in stale:
            continue
        out.append(v)
    return out


def _classify_reason(text: str) -> str | None:
    """One recorded reason as CLASS_*. An R7 reason the assembly recorded for
    a holding is the CHART against it (the held side of
    `_apply_conviction_bar` records nothing else): a positive reading a
    missing seat cannot erase."""
    if text.startswith(OWN_BAR_REASON_PREFIX):
        if "opposed" not in text:
            return CLASS_UNKNOWN  # fail closed: only the chart against is recorded here
        return CLASS_COUNTS
    if text.startswith("R2 neutral rating"):
        return CLASS_NEVER  # Option A: a neutral rating never sells a holding
    if text.startswith("R2"):
        return CLASS_COUNTS
    if text.startswith("R3"):
        return CLASS_NEVER  # the desk's list, not the stock
    if text.startswith("R5"):
        return CLASS_NEVER  # retired 2026-10-19; a catalyst is entry-only
    if text.startswith("R6"):
        code = text.split("[", 1)[1].split("]", 1)[0] if "[" in text and "]" in text else ""
        return classify_refusal(code)
    return CLASS_UNKNOWN  # fail closed


def _judge_bar(views: HoldingViews, verdicts: list):
    """`(definite failures, readings that could not be judged)`."""
    direction = "bearish" if views.is_short else "bullish"
    definite: list[str] = []
    unknown: list[str] = []
    # A record entry exists only for a name the assembly evaluated on this
    # run's technical read, so its presence is the "entry rules were read".
    for reason in views.eligibility_reasons or ():
        text = str(reason)
        klass = _classify_reason(text)
        if klass == CLASS_COUNTS:
            definite.append(text)
        elif klass == CLASS_UNKNOWN:
            unknown.append(text)
    # The keep rule's definite part: the chart against the holding.
    against = own_bar_hold_reason(verdicts, direction=direction)
    if against and against not in definite + unknown:
        definite.append(against)
    return definite, unknown


def _answer_lookup(views: HoldingViews):
    by_seat = {}
    for answer in views.objection_answers or ():
        seat = str((answer or {}).get("seat", "")).strip().lower()
        if seat and seat not in by_seat:
            by_seat[seat] = answer
    return by_seat.get


def judge_holding(views: HoldingViews, *, reviewer: bool = False) -> SameRulesVerdict:
    """Does this holding fail the KEEP rule right now? Only then may it be
    sold by judgement. A confirmed chart break passes ungated.

    `reviewer=True` only for the position reviewer: a holding whose only
    failure is an objection with no written overrule on record is ITS call
    (answer or sell); for every other seller it is refused, so an unanswered
    objection is never a mechanical sale."""
    sym = str(views.symbol or "").strip().upper()
    if is_protective(views.protection_basis):
        return SameRulesVerdict(
            sym,
            True,
            BASIS_PROTECTIVE,
            (str(views.protection_basis),),
            f"structure broken ({views.protection_basis}) — a protective sale, never gated",
        )
    if views.bought_today is None:
        return SameRulesVerdict(
            sym, False, BASIS_EVIDENCE_MISSING, (), "today's trade record is unreadable — no judgement sale"
        )
    if views.bought_today:
        return SameRulesVerdict(sym, False, BASIS_BOUGHT_TODAY, (), "bought today — never sold by judgement")
    verdicts = fresh_verdicts(views.seat_verdicts, sym, views.stale_earnings)
    has_tech = any(v.seat == "technical" for v in verdicts)
    definite, unknown = _judge_bar(views, verdicts)
    if definite:
        return SameRulesVerdict(
            sym, True, BASIS_FAILS_BAR, tuple(definite), "fails a buy rule other than timing: " + "; ".join(definite)
        )
    direction = "bearish" if views.is_short else "bullish"
    open_ = unanswered_objections(verdicts, direction=direction, answer_for=_answer_lookup(views))
    if open_:
        reasons = tuple(f"{OBJECTION_UNANSWERED}: {o['seat']} {o['direction']} — {o['reason']}" for o in open_)
        who = ", ".join(o["seat"] for o in open_)
        if reviewer:
            detail = f"{who} objected with no written overrule on record — the reviewer may answer it or sell"
        else:
            detail = f"{who} objected with no written overrule on record — the position reviewer's call"
        return SameRulesVerdict(sym, reviewer, BASIS_OBJECTION_UNANSWERED, reasons, detail)
    missing = list(unknown)
    if not has_tech:
        missing.append("no technical read this run")
    elif views.eligibility_reasons is None:
        missing.append("entry rules R2-R6 not evaluated this run in the holding's direction")
    if missing:
        return SameRulesVerdict(
            sym,
            False,
            BASIS_EVIDENCE_MISSING,
            tuple(missing),
            "entry bar cannot be evaluated (" + "; ".join(missing) + ") — not sold by judgement; the stop stays",
        )
    return SameRulesVerdict(
        sym, False, BASIS_PASSES_BAR, (), "still passes every buy rule except timing — not sold by judgement"
    )


def parse_blocked_reasons(raw) -> tuple[str, ...]:
    """`PortfolioManagerAgent.last_blocked` stores `str(list)`; read it back."""
    if raw is None:
        return ()
    if isinstance(raw, (list, tuple)):
        return tuple(str(r) for r in raw)
    text = str(raw).strip()
    if not text:
        return ()
    try:
        value = ast.literal_eval(text)
    except (ValueError, SyntaxError):
        return (text,)
    if isinstance(value, (list, tuple)):
        return tuple(str(r) for r in value)
    return (str(value),)


def bought_today_from_db(db, symbol: str) -> bool | None:
    """True when today's trade record shows an opening trade; None if unreadable."""
    try:
        rows = db.get_trades(symbol=symbol, limit=50, today_only=True)
    except Exception as exc:  # noqa: BLE001 — unreadable = refuse the sale, recorded durably
        from types import SimpleNamespace

        from src.sentinel.guarded_site import record_site

        record_site(SimpleNamespace(db=db), "same_rules_gate.bought_today", exc, context={"symbol": symbol}, log=logger)
        return None
    return any(str(r.get("action") or "").upper() in _OPENING_ACTIONS for r in (rows or ()))


def set_aside_carried_refusals(constructor) -> dict:
    """Empty the constructor's refusal record before this run's preview and
    return what an earlier run left in it (not yet drained by entry orders)."""
    live = getattr(constructor, "last_refusals", None)
    carried = dict(live) if isinstance(live, dict) else {}
    if carried:
        live.clear()
    return carried


def restore_carried_refusals(constructor, carried: dict) -> None:
    """Put the carried refusals back after the snapshot, this run's winning,
    so the durable filing still happens exactly once."""
    live = getattr(constructor, "last_refusals", None)
    if carried and isinstance(live, dict):
        for symbol, refusal in carried.items():
            live.setdefault(symbol, refusal)


def build_held_bar_record(positions, analyses, blocked) -> dict[str, tuple[str, ...]]:
    """THIS run's entry-rule reasons for every holding the assembly evaluated
    (a technical read this run), in the HOLDING's direction (long stays long,
    short stays short).

    R2 fails when the rating points AGAINST the holding. When it points the
    holding's way, the assembly's own `candidate_eligibility` reasons for the
    name were computed in that same direction, so they are read, never
    restated. A NEUTRAL rating holds (owner Option A, 14 Oct 2026): R3-R6 are
    read from the held-direction pass (`HELD_NEUTRAL_R2`); without that pass
    R2-R6 are unread (no record, so no sale on them). A held name the
    assembly recorded in `blocked` with no rating at hand is read as recorded."""
    from src.agents.portfolio_manager.candidate_ranking import HELD_NEUTRAL_R2

    rating_by = {str(getattr(a, "symbol", "")).strip().upper(): getattr(a, "rating", None) for a in analyses or ()}
    blocked_by = {str(k).strip().upper(): v for k, v in dict(blocked or {}).items()}
    record: dict[str, tuple[str, ...]] = {}
    for p in positions or ():
        sym = str(getattr(p, "symbol", "") or "").strip().upper()
        qty = getattr(p, "qty", 0) or 0
        if not sym or not qty or (sym not in rating_by and sym not in blocked_by):
            continue
        rating = str(rating_by.get(sym) or "")
        held = "short" if qty < 0 else "long"
        rated = "short" if rating in ("sell", "strong_sell") else "long"
        reasons = parse_blocked_reasons(blocked_by.get(sym))
        if reasons[:1] == (HELD_NEUTRAL_R2,):
            record[sym] = reasons[1:]
        elif rating == "neutral":
            continue
        elif rating and rated != held:
            record[sym] = (f"R2 rating {rating} does not point the holding's way ({held})",)
        else:
            record[sym] = reasons
    return record


def held_bar_record_from_assembly(pm, positions, analyses) -> dict[str, tuple[str, ...]]:
    """The held-direction record from the manager agent's DETERMINISTIC prompt
    assembly (`build_user_message`), which runs on every run — inside the paid
    `decide()` call, or alone for the half-hour "earn its place" check when
    the paid manager does not answer (owner 12 Oct 2026). Nothing here reads
    the manager's ANSWER: `blocked` is computed before any model is asked.

    `blocked` is read whole (`last_blocked`); the rotation pre-check carries
    the held part of the same set (`ineligible_candidates`) and is read too,
    so a name the pre-check acts on always has its reasons here. The caller
    reads this only right after the assembly ran this run (both values are
    reset at its start), so nothing from an earlier run is read."""
    blocked: dict = {}
    opportunity = getattr(getattr(pm, "last_rotation_precheck", None), "opportunity", None)
    if getattr(opportunity, "tier", None) == "ineligible_hold":
        for sym, reasons in getattr(opportunity, "ineligible_candidates", None) or ():
            blocked[str(sym).strip().upper()] = tuple(reasons)
    live = getattr(pm, "last_blocked", None)
    if isinstance(live, dict):
        blocked.update({str(k).strip().upper(): v for k, v in live.items()})
    return build_held_bar_record(positions, analyses, blocked)


def objection_answers_from_db(db, symbol: str) -> tuple:
    """Every name-level seat's newest written objection answer on record for
    `symbol` (desk archetype step A's reader, `objection_answer_for`). An
    unreadable record reads as no answer: the objection then stays open,
    which only ever routes the holding to the reviewer, never sells it."""
    try:
        from src.stage_decision import objection_answer_for

        found = (objection_answer_for(db, symbol, seat) for seat in NAME_LEVEL_SEATS)
        return tuple(a for a in found if a)
    except Exception as exc:  # noqa: BLE001 — a record read must never break a run
        logger.warning("same-rules gate: objection answers for %s unreadable (%s)", symbol, exc)
        return ()


def views_from_pipeline(
    pipeline, ctx, symbol: str, *, is_short: bool, protection_basis: str | None = None
) -> HoldingViews:
    """The views a buy of `symbol` would be judged on in THIS pipeline run.

    Reads only `ctx.held_bar_record` (built by the decision stage right after
    this run's deterministic assembly), never the manager agent's live
    `last_blocked` (which survives a run with no assembly and is reset by the
    accounting re-ask)."""
    sym = str(symbol or "").strip().upper()
    record = getattr(ctx, "held_bar_record", None)
    eligibility = record.get(sym) if isinstance(record, dict) else None
    stale = getattr(ctx, "evidence_stale_sources", None)
    stale_earnings = frozenset(
        str(s).strip().upper()
        for s, seats in (dict(stale) if isinstance(stale, dict) else {}).items()
        if "earnings" in set(seats or ())
    )
    return HoldingViews(
        symbol=sym,
        is_short=is_short,
        seat_verdicts=tuple(getattr(ctx, "seat_verdicts", None) or ()),
        eligibility_reasons=tuple(eligibility) if eligibility is not None else None,
        # The seats with no reading for this name this run — the one rule the
        # half-hour check has always used; a seat outage is unknown, never fail.
        unknown_seats=failed_evidence_seats(ctx, sym),
        bought_today=bought_today_from_db(pipeline.db, sym),
        stale_earnings=stale_earnings,
        protection_basis=protection_basis,
        objection_answers=objection_answers_from_db(pipeline.db, sym),
    )


def gate_review_actions(review, views_by_symbol: dict[str, HoldingViews]):
    """Route the position reviewer's proposed sales through the gate.

    Returns `(review, verdicts)`: every SELL/REDUCE/COVER the gate refuses is
    turned into a HOLD whose reason records the refusal; every verdict is
    returned so the caller can record it per symbol.
    """
    actions = list(getattr(review, "actions", None) or [])
    out, verdicts = [], []
    for item in actions:
        if not is_judgement_sale(getattr(item, "action", None)):
            out.append(item)
            continue
        sym = str(item.symbol or "").strip().upper()
        views = views_by_symbol.get(sym) or HoldingViews(symbol=sym, is_short=False, bought_today=None)
        verdict = judge_holding(views, reviewer=True)
        verdicts.append(verdict)
        if verdict.sell_allowed:
            out.append(item)
            continue
        out.append(
            item.model_copy(
                update={
                    "action": "HOLD",
                    "reason": f"same-rules gate refused proposed {item.action}: {verdict.detail}. "
                    f"Proposal was: {item.reason}",
                }
            )
        )
    return review.model_copy(update={"actions": out}), verdicts


def alert_false_claim(symbol, action, finding, *, outcome: str) -> None:
    """Tell the owner a sale gave a reason proven false, on the morning and
    the midday/close paths alike. `outcome` says what actually happened to the
    sale, so the alert never claims a block that did not occur. Best effort:
    a failed send is logged, never raised."""
    body = (
        f"\u26a0\ufe0f A SALE GAVE A REASON THAT IS NOT TRUE \u2014 {action} {symbol}\n"
        f"The check found: {str(finding or '').strip()[:400]}\n"
        f"What happened: {outcome}"
    )
    try:
        from src import notifier

        notifier.send_owner_alert(body, symbols=[str(symbol)])
    except Exception as exc:  # noqa: BLE001
        logger.warning("same-rules gate: false-claim alert for %s not sent (%s)", symbol, exc)
