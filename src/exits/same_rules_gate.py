"""ONE gate for every JUDGEMENT sale — owner ruling 2026-10-13.

Owner, verbatim: "selling is not set up the same as buying and it should be.
Same rules should apply."

A holding may be sold, reduced or covered BY JUDGEMENT (the portfolio manager,
the midday/close position reviewer, the half-hour "earn its place" check) only
when it FAILS the entry bar a buy of the same stock would have to pass at that
minute. Seats may still PROPOSE a sale; only this gate decides it.

The entry bar is the desk's existing one, CALLED here, never restated:

  * R2-R6 — `candidate_eligibility` (rating, BUY-eligible universe, net
    independent evidence, constructor refusal), read from the PM's own
    `last_blocked` record of this run.
  * R7 — the role-based conviction bar `own_bar_block_reason` (technical
    confirms timing, NO name-level seat opposed — the 25 Sep rule that a
    bearish news seat refuses a buy — and a supported non-technical thesis).
    For a HELD name this is the FULL bar, not the opposition-only STAY subset
    (`own_bar_opposition_reason`) the 25 Sep ruling used; the 13 Oct ruling
    supersedes it.

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

from src.risk.conviction_bar import (
    OWN_BAR_REASON_PREFIX,
    own_bar_block_reason,
    own_bar_opposition_reason,
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

_NET_EVIDENCE_PREFIX = "R5 "
_TECH_CLAUSE_PREFIX = f"{OWN_BAR_REASON_PREFIX} — technical"


@dataclass(frozen=True)
class HoldingViews:
    """Everything a buy of `symbol` would be judged on this minute."""

    symbol: str
    is_short: bool
    #: This run's seat `AnalystVerdict`s (any symbol; filtered here).
    seat_verdicts: tuple = ()
    #: R2-R6 reasons from this run's `candidate_eligibility`. `None` = the
    #: entry rules were not evaluated this run (cannot judge).
    eligibility_reasons: tuple[str, ...] | None = None
    #: Non-technical seats whose reading for this name is unknown this run.
    failed_seats: tuple[str, ...] = ()
    #: True/False from today's trade record; `None` = unreadable.
    bought_today: bool | None = False


@dataclass(frozen=True)
class SameRulesVerdict:
    symbol: str
    sell_allowed: bool
    basis: str
    reasons: tuple[str, ...]
    detail: str


def is_judgement_sale(action: str | None) -> bool:
    return str(action or "").strip().upper() in JUDGEMENT_SALE_ACTIONS


def _definite_failures(views: HoldingViews, verdicts: list, has_tech: bool, failed: tuple[str, ...]) -> list[str]:
    """Entry-bar failures that no missing view could undo."""
    direction = "bearish" if views.is_short else "bullish"
    definite: list[str] = []
    if has_tech and views.eligibility_reasons is not None:
        for reason in views.eligibility_reasons:
            text = str(reason)
            if text.startswith(OWN_BAR_REASON_PREFIX):
                continue  # R7 is recomputed in full below
            if text.startswith(_NET_EVIDENCE_PREFIX) and failed:
                continue  # unknown is not fail: a failed seat feeds R5
            definite.append(text)
    opposition = own_bar_opposition_reason(verdicts, direction=direction)
    # A macro vote is weighed against supporters, so a missing supporter
    # could outweigh it; every other opposition is a positive reading.
    if opposition and not (failed and "macro opposed" in opposition):
        definite.append(opposition)
    if has_tech and opposition is None:
        full = own_bar_block_reason(verdicts, direction=direction)
        if full and (not failed or full.startswith(_TECH_CLAUSE_PREFIX)):
            definite.append(full)
    return definite


def _missing_views(views: HoldingViews, has_tech: bool, failed: tuple[str, ...]) -> list[str]:
    missing: list[str] = []
    if not has_tech:
        missing.append("no technical read this run")
    elif views.eligibility_reasons is None:
        missing.append("entry rules R2-R6 not evaluated this run")
    if failed:
        missing.append("seat(s) without a reading: " + ",".join(failed))
    return missing


def judge_holding(views: HoldingViews) -> SameRulesVerdict:
    """Would a buy of this holding be REFUSED right now? Only then may it be
    sold by judgement."""
    sym = str(views.symbol or "").strip().upper()
    if views.bought_today is None:
        return SameRulesVerdict(
            sym, False, BASIS_EVIDENCE_MISSING, (), "today's trade record is unreadable — no judgement sale"
        )
    if views.bought_today:
        return SameRulesVerdict(sym, False, BASIS_BOUGHT_TODAY, (), "bought today — never sold by judgement")
    verdicts = [v for v in (views.seat_verdicts or ()) if str(getattr(v, "symbol", "")).strip().upper() == sym]
    has_tech = any(v.seat == "technical" for v in verdicts)
    failed = tuple(sorted({str(s) for s in (views.failed_seats or ())}))
    definite = _definite_failures(views, verdicts, has_tech, failed)
    if definite:
        return SameRulesVerdict(
            sym, True, BASIS_FAILS_BAR, tuple(definite), "fails the entry bar a buy must pass: " + "; ".join(definite)
        )
    missing = _missing_views(views, has_tech, failed)
    if missing:
        return SameRulesVerdict(
            sym,
            False,
            BASIS_EVIDENCE_MISSING,
            tuple(missing),
            "entry bar cannot be evaluated (" + "; ".join(missing) + ") — not sold by judgement; the stop stays",
        )
    return SameRulesVerdict(
        sym, False, BASIS_PASSES_BAR, (), "still passes the entry bar a buy must pass — not sold by judgement"
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
    except Exception as exc:  # noqa: BLE001
        logger.warning("same-rules gate: today's trades for %s unreadable (%s)", symbol, exc)
        return None
    return any(str(r.get("action") or "").upper() in _OPENING_ACTIONS for r in (rows or ()))


def views_from_pipeline(pipeline, ctx, symbol: str, *, is_short: bool) -> HoldingViews:
    """The views a buy of `symbol` would be judged on in THIS pipeline run."""
    from src.pipeline_rotation_exec import failed_evidence_seats

    sym = str(symbol or "").strip().upper()
    last_blocked = getattr(getattr(pipeline, "portfolio_manager", None), "last_blocked", None)
    eligibility = (
        parse_blocked_reasons({str(k).upper(): v for k, v in last_blocked.items()}.get(sym))
        if isinstance(last_blocked, dict)
        else None
    )
    return HoldingViews(
        symbol=sym,
        is_short=is_short,
        seat_verdicts=tuple(getattr(ctx, "seat_verdicts", None) or ()),
        eligibility_reasons=eligibility,
        failed_seats=failed_evidence_seats(ctx, sym),
        bought_today=bought_today_from_db(pipeline.db, sym),
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
        verdict = judge_holding(views)
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
