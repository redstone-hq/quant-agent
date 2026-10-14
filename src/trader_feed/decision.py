"""Section builders and the morning/once decision-session message.

Moved verbatim from src/trader_feed.py; see src/trader_feed/__init__.py.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from pathlib import Path
from typing import Any

from src.notifier import (
    _actionable_coverage_gaps,
    _attr_or_key,
    _clip_text,
    _DB_PATH as _NOTIFIER_DB_PATH,
    _fmt_signed_money,
    _lookup_company_profiles,
    _margin_interest_lines,
    _new_block,
    _new_section,
    _seal_section,
    company_name,
    describe_ai_cost,
    describe_data_status,
    describe_skipped_decision,
    fmt_time_12h,
    humanize_status,
    format_session_result as _base_format_session_result,
    TelegramNotifier,
)
from src.status_plain_extra import owner_words, plain_prose, words_for
from src.trading_calendar import SESSION_WINDOWS, et_now, in_session_window, to_et
from src.trader_feed.common import (
    _SWEEP_SYMBOLS,
    _b,
    _blocked_rows,
    _budgeted_sections,
    _clip as _clip_raw,
    _first_sentence as _first_sentence_raw,
    _done_rows,
    _empty_snapshot,
    _execution_rows,
    _fault_count,
    _fill_state_plain,
    _fmt_elapsed,
    _looked_at_rows,
    _number,
    _outcome_word,
    _pnl_section_lines,
    _profiles,
    _read_run,
    _signal_rows,
    _skip_who,
    _status_emoji,
    _symbol_stop,
    _ticker_co,
    logger,
)


def _clip(value: Any, limit: int = 140, *, model_prose: bool = True) -> str:
    """`common._clip`, with any raw field name in the AI prose put into plain words."""
    return plain_prose(_clip_raw(value, limit, model_prose=model_prose))


def _first_sentence(value: Any) -> str:
    """`common._first_sentence`, with any raw field name in the AI prose put into plain words."""
    return plain_prose(_first_sentence_raw(value))


def extract_alert_symbols(run_id: str | None, result: dict | None) -> list[str]:
    """Every symbol worth making tappable in this alert, first-seen order,
    capped (see `TelegramNotifier._MAX_LINKED_SYMBOLS` in src/notifier.py).

    Deliberately narrow: pulled only from the SAME structured fields the
    renderers above already iterate for a `symbol` key (PM proposed orders,
    executed trades, execution skips, stop-coverage gaps, the midday/close
    reviewer's `result["review"]["actions"]` — including a HOLD or a
    decided-but-unexecuted action that never became a broker trade — and the
    top-level `result["orders"]` the base formatter's own
    `_append_company_identities` uses) — never a scan of the free-text PM/
    risk rationale, which routinely contains capitalized words ("ALL",
    "GO", "PASS") that would false-positive as tickers.

    Read-only and fail-soft like the rest of this module: a symbol that
    can't be determined is just not linked — see
    `TelegramNotifier._linkify_symbols` for why a bad/missing symbol must
    never be able to break message delivery.
    """
    symbols: list[str] = []

    def _add(raw: Any) -> None:
        sym = str(raw or "").strip().upper()
        if sym and sym not in symbols:
            symbols.append(sym)

    if isinstance(result, dict):
        for row in result.get("orders") or []:
            if isinstance(row, dict):
                _add(row.get("symbol"))
        for row in result.get("stop_coverage_gaps") or []:
            if isinstance(row, dict):
                _add(row.get("symbol"))
        review = result.get("review")
        if isinstance(review, dict):
            for row in review.get("actions") or []:
                if isinstance(row, dict):
                    _add(row.get("symbol"))

    try:
        snap = _read_run(run_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("extract_alert_symbols: run read failed for %s: %s", run_id, exc)
        snap = _empty_snapshot()

    for key in ("pm_orders", "trades", "skips"):
        for row in snap.get(key) or []:
            if isinstance(row, dict):
                _add(row.get("symbol"))

    return symbols[:10]


def _append_market(lines: list[str], snap: dict[str, Any]) -> None:
    macro = snap.get("macro")
    if not isinstance(macro, dict):
        return
    bits = [
        str(value) for value in (macro.get("regime"), macro.get("equity_outlook"), macro.get("confidence")) if value
    ]
    guidance = macro.get("position_guidance") or {}
    target = guidance.get("target_invested_pct") if isinstance(guidance, dict) else None
    text = " / ".join(bits)
    if isinstance(target, (int, float)):
        text += f" · target {target:g}% invested"
    # Board item 119: the owner's one-line market read must not present a
    # regime call formed on an incomplete FRED set as a complete one. The
    # stamp comes from the deterministic fetch record, not the economist —
    # see `src/data/macro.py::MacroCoverage.verdict_stamp`. "unknown" prints
    # nothing: an unstamped verdict makes no claim either way.
    coverage_state = str(macro.get("coverage_state") or "unknown")
    if coverage_state in ("partial", "failed"):
        note = str(macro.get("coverage_note") or "").strip()
        text += " · ⚠️ PARTIAL READ" + (f" ({note})" if note else "")
    if text:
        lines.append(f"📊 Market: {text}")


def _append_book(lines: list[str], snap: dict[str, Any]) -> None:
    positions = [row for row in (snap.get("positions") or []) if isinstance(row, dict)]
    if not positions:
        return
    risk_rows = [row for row in positions if str(row.get("symbol", "")).upper() not in _SWEEP_SYMBOLS]
    sweep_rows = [row for row in positions if str(row.get("symbol", "")).upper() in _SWEEP_SYMBOLS]
    invested = sum(_number(row.get("market_value")) or 0.0 for row in risk_rows)
    parked = sum(_number(row.get("market_value")) or 0.0 for row in sweep_rows)
    text = f"💼 Book: {len(risk_rows)} position(s) · ${invested:,.0f} invested"
    if parked > 0:
        text += f" · ${parked:,.0f} T-bills"
    lines.append(text)


def _signal_row_line(row: dict, profiles: dict[str, Any] | None = None) -> str:
    """One '   • SYM: RATING/conviction · R/R x.xx — reason' bullet, the
    one place that renders a tech-analysis row this way — shared by
    `_append_signals` (unchanged, default `profiles=None`: this text is
    the "full existing per-stock reasons... unchanged in content" DETAILS
    content) and the hourly desk-check summary, which passes `profiles` to
    put the company name inline since it has no separate DONE/LOOKED-AT
    section to have already introduced it."""
    sym = str(row.get("symbol", "?")).upper()
    label = _ticker_co(sym, profiles) if profiles is not None else sym
    rating = str(row.get("rating", "?")).upper()
    conviction = str(row.get("conviction", "?")).lower()
    rr = row.get("risk_reward")
    # Board item 89 clarity defect — "R/R 2.5" carried no unit. Same
    # figure, said as what it is: the reward measured in multiples of the
    # risk taken to get it.
    rr_text = f" · reward {rr:g}× the risk" if isinstance(rr, (int, float)) else ""
    reason = _first_sentence(row.get("reasoning"))
    text = f"   • {label}: {rating}/{conviction}{rr_text}"
    if reason:
        text += f" — {reason}"
    return text


def _append_intraday_evidence_freshness(
    lines: list[str],
    outer: dict | None,
    nested: dict | None,
) -> None:
    """The freshness disclosure on an intraday tick, in the owner's words.

    The record is attached to the tick result by `run_intra_check`; an older
    stored result, or a tick that never reached the decision, simply has
    none and this renders nothing.
    """
    from src.notifier import describe_evidence_freshness

    for source in (outer, nested):
        if not isinstance(source, dict):
            continue
        block = describe_evidence_freshness(source.get("evidence_freshness"))
        if block:
            lines.extend(block)
            return


def _append_signals(
    lines: list[str],
    snap: dict[str, Any],
    candidates: list[str] | None = None,
) -> None:
    tech = [row for row in (snap.get("tech") or []) if isinstance(row, dict)]
    if candidates:
        wanted = {str(symbol).upper() for symbol in candidates}
        tech = [row for row in tech if str(row.get("symbol", "")).upper() in wanted]
    if not tech:
        if candidates:
            lines.append(f"🔎 Triggered: {', '.join(candidates[:5])}")
        return

    actionable = [row for row in tech if str(row.get("rating", "")).lower() not in ("", "neutral")]
    lines.append(f"🔎 Signals: {len(tech)} analyzed · {len(actionable)} actionable")
    for row in _signal_rows(snap, candidates):
        lines.append(_signal_row_line(row))


def _append_pm(lines: list[str], snap: dict[str, Any]) -> None:
    reasoning = snap.get("pm_reasoning")
    orders = [row for row in (snap.get("pm_orders") or []) if isinstance(row, dict)]
    targets = [row for row in (snap.get("pm_targets") or []) if isinstance(row, dict)]
    pm_summary = (snap.get("agent_summaries") or {}).get("portfolio_manager")
    if not (reasoning or orders or targets or pm_summary):
        return

    actionable = [row for row in orders if str(row.get("action", "")).upper() != "HOLD"]
    holds = [row for row in orders if str(row.get("action", "")).upper() == "HOLD"]
    if orders:
        lines.append(f"🧠 PM/Constructor: {len(actionable)} change(s) · {len(holds)} hold(s)")
        for row in actionable[:4]:
            action = str(row.get("action", "?")).upper()
            symbol = str(row.get("symbol", "?")).upper()
            allocation = row.get("allocation_pct")
            # Board item 89 clarity defect — a percentage with no
            # denominator. `allocation_pct` was labelled "% of the
            # account" for every action, which is wrong for SELL/COVER:
            # `_build_sell`/`_build_cover` set it to the share of the
            # EXISTING POSITION being sold/covered, not a share of the
            # account. For BUY/SHORT/HOLD the constructor's own weight
            # delta is divided by a gross multiplier before this field is
            # set, so calling it a plain account fraction is unverified
            # there too. Label each honestly instead of asserting an
            # account-level figure nobody actually computed.
            if isinstance(allocation, (int, float)) and action in ("SELL", "COVER"):
                alloc_text = f" {allocation:g}% of the position"
            elif isinstance(allocation, (int, float)):
                alloc_text = f" {allocation:g}% (target weight change)"
            else:
                alloc_text = ""
            reason = _first_sentence(row.get("reasoning"))
            text = f"   • {action} {symbol}{alloc_text}"
            if reason:
                text += f" — {reason}"
            lines.append(text)
        for row in holds[:2]:
            symbol = str(row.get("symbol", "?")).upper()
            reason = _first_sentence(row.get("reasoning"))
            text = f"   • PASS {symbol}"
            if reason:
                text += f" — {reason}"
            lines.append(text)
    elif targets:
        lines.append(f"🧠 PM: {len(targets)} target(s), no constructed order evidence")

    # Labelled "PM view (this check)" rather than a bare "View:" — this text
    # is the PM's own prose verbatim (never reworded here) and on an
    # intraday tick it can say something like "no trades today", which read
    # as a bare "View:" is easy to mistake for the whole day's verdict
    # rather than what it actually is: this one check's reasoning.
    portfolio_view = reasoning.get("portfolio_view") if isinstance(reasoning, dict) else None
    if portfolio_view:
        lines.append(f"   PM view (this check): {_first_sentence(portfolio_view)}")
    elif pm_summary and str(pm_summary).lower() != "no trades":
        lines.append(f"   PM view (this check): {_first_sentence(pm_summary)}")

    # Reasoning-visibility gap (2026-09-25): "why THIS size" already reaches
    # the dashboard (`ReasoningChain.sizing_logic`, rendered generically off
    # `pm_reasoning.reasoning_chain` — see frontend `PM_CHAIN_LABELS`) but
    # never Telegram. It's a session-level sentence (one PM call sizes every
    # order this check), not per-symbol, so it's only worth a line when the
    # PM actually changed something. Surfaced verbatim, not recomputed.
    chain = reasoning.get("reasoning_chain") if isinstance(reasoning, dict) else None
    sizing_logic = chain.get("sizing_logic") if isinstance(chain, dict) else None
    if actionable and sizing_logic:
        lines.append(f"   Sizing: {_first_sentence(sizing_logic)}")


# `models.RiskReasonCategory`, each value in the words a person would use
# (board item 89: internal status codes shown as-is). An unmapped value is
# described, with the raw token kept for the record, never guessed at.
#
# The words live in the one owner-wording table (`src/status_plain_extra.py`).
_RISK_CATEGORY_WORDS: dict[str, str] = words_for("risk_category")


def _risk_category_words(category: Any) -> str:
    token = str(category or "").strip().lower()
    if not token:
        return "no category recorded"
    return _RISK_CATEGORY_WORDS.get(token) or (
        f"a category the desk has no plain wording for (kept for the record: {token})"
    )


def _append_risk(lines: list[str], snap: dict[str, Any]) -> None:
    risk = snap.get("risk")
    if not isinstance(risk, dict):
        return
    approved = risk.get("approved")
    label = "APPROVED" if approved is True else "REJECTED" if approved is False else "UNKNOWN"
    category = _risk_category_words(risk.get("reason_category"))
    scale = risk.get("scale_all_buys")
    # Board items 134 + 162 (owner ruling 2026-09-25): scale_all_buys is an
    # ADVISORY exposure concern on entries — it is recorded and surfaced but no
    # longer resizes any buy. Show the seat's concern, not a size cut that no
    # longer happens; a value of 1.0 (no concern) shows nothing.
    scale_text = (
        f" · risk seat flagged a portfolio-wide exposure concern "
        f"({owner_words('risk_field', 'scale_all_buys')} at {scale * 100:.0f}% of plan)"
        f" — advisory only, entries were NOT resized"
        if isinstance(scale, (int, float)) and scale < 1.0
        else ""
    )
    mods = snap.get("risk_mods") or []
    # Phase 10.1: a verdict can now be APPROVED overall and still have refused
    # individual names. Reading only `approved` would show that run as a clean
    # approval and never mention the trade that died.
    rejected = risk.get("rejected_symbols") or []
    rej_syms = (
        sorted({str(r.get("symbol")) for r in rejected if isinstance(r, dict) and r.get("symbol")})
        if isinstance(rejected, list)
        else []
    )
    refused_text = f" · refused {', '.join(rej_syms)}" if rej_syms else ""
    lines.append(f"🛡️ Risk: {label} · {category}{scale_text} · {len(mods)} mod(s){refused_text}")
    reason = _first_sentence(risk.get("reasoning"))
    if reason:
        lines.append(f"   {reason}")


def _append_gate_and_execution(
    lines: list[str],
    result: dict,
    snap: dict[str, Any],
    *,
    explain_no_trade: bool = True,
) -> None:
    status = str(result.get("status", "unknown"))
    skips = [row for row in (result.get("execution_skips") or []) if isinstance(row, dict)]
    if not skips:
        skips = [row for row in (snap.get("skips") or []) if isinstance(row, dict)]

    if status in {"hard_risk_block", "symbol_block"}:
        lines.append(f"⚙️ Deterministic gate: BLOCKED — {_clip(result.get('reason'), 650)}")
    elif skips:
        lines.append(f"⚙️ Execution gate: {len(skips)} skip(s)")
        for row in skips[:3]:
            symbol = str(row.get("symbol", "?")).upper()
            # Board item 89 defect 5: this printed the internal reason code
            # verbatim ("• NVDA: fat_finger_guard"). `_skip_who` is the
            # plain-English map that already exists for exactly these codes
            # and the BLOCKED section already uses it; only this line
            # bypassed it.
            who = _skip_who(row.get("reason", ""))
            detail = _clip(row.get("detail"), 420)
            text = f"   • {symbol}: {who}"
            if detail:
                text += f" — {detail}"
            lines.append(text)

    sweep, real = _execution_rows(snap)
    if sweep:
        releases = sum(1 for row in sweep if str(row.get("action", "")).upper() == "SWEEP_SELL")
        parks = sum(1 for row in sweep if str(row.get("action", "")).upper() == "SWEEP_BUY")
        bits = []
        if releases:
            bits.append(f"{releases} T-bill cash release")
        if parks:
            bits.append(f"{parks} cash-park buy")
        if bits:
            lines.append("💵 Cash management: " + " · ".join(bits))

    if real:
        lines.append(f"⚡ Execution: {len(real)} broker action(s)")
        for row in real[:6]:
            action = str(row.get("action", "?")).upper()
            symbol = str(row.get("symbol", "?")).upper()
            qty = _number(row.get("fill_qty")) or _number(row.get("qty"))
            price = _number(row.get("fill_price")) or _number(row.get("price"))
            # Board item 89 defect 5: the raw fill-status token used to be
            # printed here ("· pending_submit").
            fill_state = _fill_state_plain(row.get("fill_status"))
            qty_text = f" {qty:g}" if qty is not None else ""
            price_text = f" @ ${price:,.2f}" if price is not None and price > 0 else ""
            lines.append(f"   • {action} {symbol}{qty_text}{price_text} · {fill_state}")
    elif explain_no_trade:
        _append_no_trade_reason(lines, result, snap, skips)


def _append_no_trade_reason(
    lines: list[str],
    result: dict,
    snap: dict[str, Any],
    skips: list[dict],
) -> None:
    status = str(result.get("status", "unknown"))
    risk = snap.get("risk")
    pm_orders = [row for row in (snap.get("pm_orders") or []) if isinstance(row, dict)]
    actionable = [row for row in pm_orders if str(row.get("action", "")).upper() != "HOLD"]
    pm_summary = str((snap.get("agent_summaries") or {}).get("portfolio_manager") or "")

    if status == "rejected":
        reason = _clip(result.get("reason"), 650)
        lines.append(f"⏸️ NO TRADE — Risk vetoed the plan{': ' + reason if reason else ''}")
    elif status in {"hard_risk_block", "symbol_block"}:
        lines.append("⏸️ NO TRADE — deterministic eligibility blocked the proposed action(s)")
    elif status == "buys_unfunded" or skips:
        lines.append("⏸️ NO TRADE — decision(s) survived review but execution could not complete")
    elif isinstance(risk, dict) and risk.get("approved") is False:
        lines.append(f"⏸️ NO TRADE — Risk rejected: {_first_sentence(risk.get('reasoning'))}")
    elif pm_orders and not actionable:
        lines.append("⏸️ NO TRADE — PM/constructor produced HOLD only")
    elif snap.get("pm_reasoning") or pm_summary:
        lines.append("⏸️ NO TRADE — PM produced no executable portfolio change")
    else:
        # Was "...; detailed PM evidence unavailable" — internal phrasing.
        # The honest plain-words version, which does NOT overclaim: there is
        # no stored reasoning for this run, which is not the same as saying
        # there was nothing to explain.
        lines.append(
            "⏸️ NO TRADE — nothing was bought or sold, and the desk stored no reasoning to explain for this run"
        )


def _append_footer(lines: list[str], snap: dict[str, Any], elapsed: float, mode: str = "") -> None:
    """Duration and AI spend only — the owner-facing footer. Used to also
    print `run {run_id}` and the raw provider-request count ("LLM
    $0.10/2 provider requests"); both are engineering detail with no
    action attached to it on a phone alert, so 2026-09-17 dropped them.
    The run_id is still available wherever it actually matters (DB lookups,
    `send()`'s `link_url`/`symbols` args) — nothing depends on it being
    IN this printed text; only its presence in the rendered message
    changed.
    """
    bits: list[str] = []
    cost = snap.get("cost")
    # Always shown, never gated on `isinstance(cost, (int, float))` any
    # more (2026-09-29 log defect): that gate silently DROPPED the whole
    # bit whenever `cost` was `None`, and `_read_run` now legitimately
    # returns `None` for a run whose settled cost the cost-circuit itself
    # marked inexact (a charged-but-failed provider call it cannot prove
    # cost $0 -- see `_canonical_run_cost`, src/api/db_reads.py). Dropping
    # the line there would trade a false "none, free models" for silence,
    # which still never tells the owner the true state. `describe_ai_cost`
    # already renders `None` as "not available", so it is always safe to
    # call unconditionally -- same one shared helper as before (owner
    # review, 2026-09-18) that turned "AI cost $0.0000" into the truthful
    # words for a free-tier run.
    bits.append(describe_ai_cost(cost, label="AI cost"))
    bits.append(f"took {_fmt_elapsed(elapsed)}")
    lines.append("\U0001f9fe " + " \u00b7 ".join(bits))
    if mode in ("morning", "once"):
        # Morning only (midday/evening also end here): the one credit line.
        # The one-shot $3 alert is checked on every send, in the send funnel.
        from src.llm_balance_runway import balance_line

        lines.append("\U0001f50b " + balance_line())


def format_coverage_gap_line(row: dict, profiles: dict | None = None) -> str:
    """One owner-facing bullet for a stop-coverage gap.

    Company name, the two quantities as words, the dollars with no stop
    over them, and the reason the automatic repair gave when it refused
    (`repair_refusal` is a plain sentence stamped at the repair site;
    absent when it has nothing to say). Nothing here is estimated.

    Module-level and shared on purpose: `src/pipeline.py` sends the same
    facts as an interrupting alert when a session-hours re-placement fails,
    and two renderings of one condition are how the feed and the alert end
    up describing the same position differently.
    """
    name = _ticker_co(str(row.get("symbol", "?")), profiles)
    held = _number(row.get("held_qty"))
    covered = _number(row.get("covered_qty"))
    bits = [name]
    if held is not None:
        bits.append(f"holding {held:g}")
    if covered is not None:
        bits.append(f"stop covers {covered:g}")
    value = _number(row.get("unprotected_value"))
    if value is not None and value > 0:
        bits.append(f"${value:,.2f} unprotected")
    text = "   \u2022 " + ", ".join(bits)
    # `repair_refusal` is why the automatic repair placed nothing. `note` is
    # the same trailing-sentence slot for a coverage condition that had no
    # repair to refuse — an elected-but-unfilled stop, where the order is
    # present and correctly sized and simply did not execute.
    refusal = str(row.get("repair_refusal") or row.get("note") or "").strip()
    if refusal:
        text += f" \u2014 {_clip(refusal, 300)}"
    return text


def _append_coverage_gaps(lines: list[str], result: dict) -> None:
    """Spec §11.1 guard 3 — two banners, never one merged count.

    A position with NO stop at all and a position whose stop is present but
    mis-sized are different conditions: the first has nothing standing watch,
    the second has an order covering most of it. Reporting them as a single
    "N gaps" line buried the worse condition inside the milder one. Shares
    `notifier._gap_is_uncovered` so the two feeds can never disagree about
    which bucket a gap is in.
    """
    from src.notifier import _gap_is_uncovered

    gaps = result.get("stop_coverage_gaps")
    if not isinstance(gaps, list) or not gaps:
        return
    from src.notifier import _gap_is_expected_fractional

    from src.notifier import _gap_is_unreadable

    # Board item 172. Third bucket, not folded into either of the two that
    # state a measured fact about coverage.
    unreadable = [row for row in gaps if isinstance(row, dict) and _gap_is_unreadable(row)]
    rows = [
        row
        for row in gaps
        if isinstance(row, dict) and not _gap_is_expected_fractional(row) and not _gap_is_unreadable(row)
    ]
    uncovered = [row for row in rows if _gap_is_uncovered(row)]
    partial = [row for row in rows if not _gap_is_uncovered(row)]
    if not uncovered and not partial and not unreadable:
        return
    profiles = _profiles(uncovered, partial, unreadable)

    def _gap_line(row: dict) -> str:
        return format_coverage_gap_line(row, profiles)

    if unreadable:
        lines.append(f"🛑🛑 STOP UNREADABLE: {len(unreadable)} position(s) the broker could not be asked about")
        for row in unreadable[:8]:
            symbol = str(row.get("symbol", "?")).upper()
            held = _number(row.get("held_qty"))
            held_text = f", holding {abs(held):g}" if held is not None else ""
            reason = str(row.get("read_error") or "").strip()
            lines.append(f"   • {_ticker_co(symbol, profiles)}{held_text} — " + (reason or "the stop query failed"))
        lines.append(
            "   Whether these have a stop is UNKNOWN — not confirmed "
            "missing and not confirmed present. Check the position's open "
            "orders at the broker directly."
        )
    if uncovered:
        lines.append(f"🛑🛑🛑 NO STOP AT ALL: {len(uncovered)} position(s) with nothing protecting them")
        lines.extend(_gap_line(row) for row in uncovered[:8])
        lines.append(
            "   The desk's automatic re-protection did not close this. Place "
            "a protective stop by hand or close the position."
        )
    if partial:
        lines.append(f"⚠️ STOP MIS-SIZED: {len(partial)} position(s) only partly protected")
        lines.extend(_gap_line(row) for row in partial[:8])
        lines.append(
            "   A stop is standing watch over part of each position; the "
            "rest has none. The desk re-checks at its next scheduled pass."
        )


_GUARD_EVIDENCE_KIND = "structural_break_trend_context"


def _read_guard_verdicts(run_id: str | None) -> dict[str, dict]:
    """symbol -> the exit guard's recorded verdict for this run (the last row
    for a symbol wins). Read from the durable row the exit stage writes
    instead of paging the owner; `{}` when there is none or the DB is
    unreadable, so a missing row only ever means a missing note."""
    from src.trader_feed import common as _common_mod

    db_path = Path(_common_mod._DB_PATH)
    if not run_id or run_id == "?" or not db_path.exists():
        return {}
    verdicts: dict[str, dict] = {}
    conn = None
    try:
        conn = sqlite3.connect(f"file:{db_path.resolve()}?mode=ro", uri=True, timeout=1.0)
        rows = conn.execute(
            "SELECT symbol, evidence_json FROM specialist_evidence "
            "WHERE run_id = ? AND agent_name = 'risk_manager' AND kind = ? ORDER BY id",
            (run_id, _GUARD_EVIDENCE_KIND),
        ).fetchall()
        for symbol, evidence_json in rows:
            try:
                data = json.loads(evidence_json or "{}")
            except (TypeError, ValueError):
                continue
            if symbol and isinstance(data, dict):
                verdicts[str(symbol).upper()] = data
    except sqlite3.DatabaseError as exc:
        logger.warning("trader-feed guard-verdict read failed for %s: %s", run_id, exc)
    finally:
        if conn is not None:
            conn.close()
    return verdicts


def _with_guard_verdicts(snap: dict[str, Any], run_id: str | None) -> dict[str, Any]:
    """Put the run's recorded guard verdicts on the snapshot so every stock's
    one line can name them. Returns `snap` for chaining."""
    snap["guard_verdicts"] = _read_guard_verdicts(run_id)
    return snap


def _guard_note(guard: dict, action: str) -> str:
    """The guard's recorded verdict in one plain phrase. Where it said keep and
    the PM sold, both are named; nothing here says which should have won."""
    sold = action in ("SELL", "COVER")
    if guard.get("protected") is False:
        return "Guard: protection lifted (break confirmed)"
    if sold:
        return "Guard said keep (break not confirmed yet), PM sold"
    return "Guard kept the hold (break not confirmed yet)"


def _stock_notes(symbol: str, snap: dict[str, Any] | None, action: str = "") -> list[str]:
    """What the other roles recorded about `symbol` this run, each as one short
    phrase for the stock's single line: any risk change, the guard's verdict,
    any pruning. The PM's own decision is the line itself."""
    if not snap:
        return []
    symbol = symbol.upper()
    action = action.upper()
    notes: list[str] = []
    for mod in snap.get("risk_mods") or []:
        if isinstance(mod, dict) and str(mod.get("symbol", "")).upper() == symbol:
            field = str(mod.get("field") or "setting").replace("_", " ")
            old, new = _number(mod.get("original_value")), _number(mod.get("new_value"))
            change = f" {old:g} to {new:g}" if old is not None and new is not None else ""
            notes.append(f"Risk changed {field}{change}")
    guard = (snap.get("guard_verdicts") or {}).get(symbol)
    if isinstance(guard, dict):
        notes.append(_guard_note(guard, action))
    rotation = snap.get("rotation")
    if (
        isinstance(rotation, dict)
        and str(rotation.get("held_symbol") or "").upper() == symbol
        and str(rotation.get("tier") or "") == "ineligible_hold"
    ):
        notes.append("Pruning: put up to be cut")
    return notes


def _notes_text(symbol: str, snap: dict[str, Any] | None, action: str = "") -> str:
    return "".join(f" · {note}" for note in _stock_notes(symbol, snap, action))


def _append_guard_only(lines: list[str], snap: dict[str, Any], acted: set[str], profiles: dict) -> None:
    """Stocks the guard gave a verdict on that have no order line above. The
    verdict used to be a separate phone alert; it is one line here."""
    guard = snap.get("guard_verdicts") or {}
    symbols = [symbol for symbol in guard if symbol not in acted]
    if not symbols:
        return
    lines.append(_b("🛡️ EXIT GUARD"))
    for symbol in symbols:
        lines.append(f"   • {_ticker_co(symbol, profiles)} — no order this session{_notes_text(symbol, snap)}")


def _append_done(lines: list[str], rows: list[dict], snap: dict, profiles: dict) -> None:
    """NEW LAYOUT item 3 — orders that actually reached the broker, one
    line each, the true fill state (never implying a fill that hasn't
    happened — `trades.fill_status` stays 'submitted' until it has).

    Reasoning-visibility gap (2026-09-25): this line used to carry only
    the mechanics (action/qty/price/stop) — the WHY sat exclusively in the
    collapsed DETAILS block below (`_append_pm`), which a phone reader has
    to tap open. `trades.reasoning` is the same PM one-sentence rationale
    already attached to this row by `_read_run`'s `SELECT ... reasoning
    ... FROM trades`; nothing is recomputed here, just surfaced inline so
    the reason for the trade is visible without opening DETAILS."""
    if not rows:
        return
    lines.append(_b("✅ DONE"))
    for row in rows:
        action = str(row.get("action", "?")).upper()
        symbol = str(row.get("symbol", "?")).upper()
        qty = _number(row.get("fill_qty")) or _number(row.get("qty"))
        price = _number(row.get("fill_price")) or _number(row.get("price"))
        fill_status = str(row.get("fill_status") or "").lower()
        state = "filled" if fill_status == "filled" else "placed, waiting to fill"
        qty_text = f"{qty:g}" if qty is not None else "?"
        price_text = f"${price:,.2f}" if price is not None and price > 0 else "?"
        stop = _symbol_stop(symbol, snap)
        # A closing SELL/COVER carries stop_loss=0.0 (no protective stop
        # applies to an exit) — `is not None` let that render "stop $0.00".
        # No legitimate stop is ever exactly 0.0 (the constructor rejects
        # stop_loss <= 0 for BUY/SHORT), so a positive check is safe here.
        stop_text = f" · stop ${stop:,.2f}" if stop else ""
        reason = _first_sentence(row.get("reasoning"))
        reason_text = f" — {reason}" if reason else ""
        lines.append(
            f"   • {action} {_ticker_co(symbol, profiles)} {qty_text} @ {price_text} — {state}{stop_text}{reason_text}"
            f"{_notes_text(symbol, snap, action)}"
        )


def _append_blocked(lines: list[str], rows: list[dict], profiles: dict, snap: dict[str, Any] | None = None) -> None:
    """NEW LAYOUT item 4 — who actually stopped it, in plain words. See
    `_blocked_rows` for the four sources this merges.

    TWO headings, not one. The old single "❌ BLOCKED / FAILED" heading
    filed the risk system working correctly under the word FAILED, which is
    the same defect as the header word and had to be fixed in the same pass
    — a header that says NO TRADE over a section that says FAILED just
    moves the confusion down one line. Rows the desk refused ON PURPOSE go
    under "🚫 NOT TAKEN"; rows where something broke keep "❌ FAILED".
    Every row still appears, with the same wording as before.
    """
    if not rows:
        return
    refused = [row for row in rows if not row.get("fault", True)]
    faults = [row for row in rows if row.get("fault", True)]

    def _bullets(group: list[dict]) -> None:
        for row in group:
            # 600, not 300: a constructor refusal sentence runs ~250
            # characters and a broker reason can run longer; a cut
            # mid-sentence was item 89's "detail truncated" defect.
            reason = _clip(row.get("reason"), 600)
            notes = _notes_text(str(row["symbol"]), snap, str(row["action"]))
            lines.append(f"   • {row['action']} {_ticker_co(row['symbol'], profiles)} — {row['who']}: {reason}{notes}")

    if refused:
        lines.append(_b("🚫 NOT TAKEN"))
        lines.append("   The desk decided against these. Nothing broke.")
        _bullets(refused)
    if faults:
        if refused:
            lines.append("")
        lines.append(_b("❌ FAILED"))
        _bullets(faults)


def _append_rotation(lines: list[str], snap: dict[str, Any] | None) -> None:
    """The opportunity-rotation pre-check, said to the owner.

    Sits directly under LOOKED AT because it answers the same question from
    the other side: LOOKED AT says why each individual candidate was passed
    on, and this says whether the desk weighed the whole field against what
    it already holds and what that comparison concluded.

    Deliberately NOT under a warning heading and deliberately not phrased as
    a fault. A full book is how this desk is meant to run; the owner's
    words, 2026-09-23: "Yes portfolio is full. But we're still reviewing
    things, which is how we built it."

    One vocabulary — the sentences come from `rotation.owner_precheck_lines`,
    reading the same durable row `_record_rotation_precheck` wrote. Renders
    nothing for a run with no pre-check row (every session before this
    shipped, and any stored report replayed from one).
    """
    from src.rotation import owner_precheck_lines, pruning_pass_lines

    record = (snap or {}).get("rotation")
    lines.extend(owner_precheck_lines(record))
    # Board item 219. The pruning pass itself — that it ran, over what, what
    # it cut and why, what it kept, and that the second tier is off.
    lines.extend(pruning_pass_lines(record))


def _append_held(lines: list[str], symbols: list[str], profiles: dict) -> None:
    """NEW LAYOUT item 6 — every held ticker, once, with the correct count
    (the defect this fixes: a live message once said "7 hold(s)" over 6
    positions with only 2 actually listed)."""
    if not symbols:
        return
    lines.append(_b(f"HELD ({len(symbols)})"))
    for symbol in symbols:
        lines.append(f"   • {_ticker_co(symbol, profiles)}")


def _watch_rows(result: dict) -> list[dict]:
    """Positions already flagged by code the desk runs today — the
    STOP MIS-SIZED half of `_append_coverage_gaps` (the milder of its two
    banners; "NO STOP AT ALL" stays the loud top-of-message 🛑🛑🛑 banner it
    already is). No new threshold is invented here — only what
    `_gap_is_uncovered` already classifies.

    Board item 172: a row whose stops could not be READ is excluded. WATCH
    renders "the stop covers only part of the position", which states that a
    stop exists and is undersized — two facts an unreadable row establishes
    neither of. It has its own banner above the numbers instead."""
    from src.notifier import _gap_is_uncovered, _gap_is_unreadable

    gaps = result.get("stop_coverage_gaps")
    if not isinstance(gaps, list):
        return []
    return [row for row in gaps if isinstance(row, dict) and not _gap_is_uncovered(row) and not _gap_is_unreadable(row)]


def _append_watch(lines: list[str], rows: list[dict], profiles: dict) -> None:
    if not rows:
        return
    lines.append(_b("⚠️ WATCH"))
    for row in rows:
        symbol = str(row.get("symbol", "?")).upper()
        held = _number(row.get("held_qty"))
        covered = _number(row.get("covered_qty"))
        sizes = f" (holding {held:g}, stop covers {covered:g})" if held is not None and covered is not None else ""
        lines.append(f"   • {_ticker_co(symbol, profiles)} — the stop covers only part of the position{sizes}")


def _format_decision_session(mode: str, result: dict, elapsed: float) -> str:
    run_id = result.get("run_id")
    snap = _read_run(run_id)
    # A caller may supply `_positions` already — that is how a stored
    # morning report is re-rendered (`render_stored_session_report`).
    # `_read_run`'s positions query is deliberately NOT run-scoped (it
    # reads the book as it stands right now), so replaying an older run
    # through it would print today's holdings under that run's date. A
    # supplied snapshot wins; the live morning path never sets one.
    if isinstance(result.get("_positions"), list):
        snap = dict(snap)
        snap["positions"] = result["_positions"]
    _with_guard_verdicts(snap, run_id)
    status = str(result.get("status", "unknown"))

    done_rows = _done_rows(snap)
    blocked_rows = _blocked_rows(result, snap)
    acted = {row["symbol"] for row in done_rows} | {row["symbol"] for row in blocked_rows}
    looked_at_rows = _looked_at_rows(snap, None, acted)
    profiles = _profiles(done_rows, blocked_rows, looked_at_rows)

    outcome = _outcome_word(
        status,
        len(done_rows),
        len(blocked_rows),
        done_rows,
        fault_count=_fault_count(blocked_rows),
    )
    lines = [f"{_status_emoji(status)} {mode.upper()} · {fmt_time_12h(et_now())} · {outcome}"]

    # P&L FIRST, directly under the heading — owner, 2026-09-18: "all the
    # P&L information has to go at the very top of every telegram alert,
    # right after the first line, which is really the heading." A repeat
    # correction: it kept drifting below whatever banner was added next.
    _new_section(lines, *_pnl_section_lines(result))

    # Margin interest — the price of money the desk borrowed. This is the
    # actual sent morning path (`_format_decision_session`, mode
    # morning/once); `src.notifier`'s own `_margin_interest_lines()` call
    # sits on `format_session_result`'s base formatter, which trader_feed
    # only falls back to for statuses that never reach here (see
    # `_BASE_ONLY_STATUSES` above) — so this line was built and correct but
    # never actually sent (measured: 0 of 78 `notifier_sends` rows contain
    # "margin interest"). Reused verbatim from `src.notifier`, including its
    # own ESTIMATE caveat and the owner's "show it every day, even at zero"
    # policy (2026-09-18) — placed here, right after P&L and before every
    # section subject to the message-length budget, so it survives a clip
    # the way the P&L block above it does.
    _new_section(lines, *_margin_interest_lines())

    _new_block(lines, _append_coverage_gaps, result)

    data_status = result.get("data_status") or {}
    if isinstance(data_status, dict):
        from src import evidence_gate

        degraded = {name: value for name, value in data_status.items() if evidence_gate.counts_as_degraded(value)}
        if degraded:
            # Board item 89 clarity defect — this named internal components
            # ("macro, tech"). Same seats, in words, plus what it means.
            _new_section(
                lines,
                "⚠️ Research was incomplete this session:",
                *(f"   • {line}" for line in describe_data_status(degraded)),
                "   The decisions below were made without that input.",
            )

    _new_block(lines, _append_market, snap)
    _new_block(lines, _append_book, snap)
    _new_block(lines, _append_done, done_rows, snap, profiles)
    _new_block(lines, _append_blocked, blocked_rows, profiles, snap)
    _new_block(lines, _append_guard_only, snap, acted, profiles)
    # The candidate list is rendered LAST and spliced back in HERE — see
    # `_budgeted_sections` for why it may not claim the budget first.
    looked_at_slot = len(lines)
    _new_block(lines, _append_rotation, snap)

    # THE REASONING LEADS, THE ENUMERATION TRAILS. Order matters inside
    # DETAILS for the same reason it matters outside it: if this block has
    # to be clipped, the clip must land in the per-candidate signals
    # listing — which is the same 69 names the LOOKED AT block above
    # already accounts for, read from a different seat — and never in the
    # PM's reasoning chain, the risk verdict or the execution record, which
    # appear nowhere else in the message. Measured on run-fccb2026: the
    # signals listing is 9,379 of the block's 9,799 characters; the PM
    # reasoning is 303, the risk verdict 0 and the execution record 114.
    #
    # Risk/Execution ("protected_lines") are split out and passed to
    # `_wrap_details` separately (2026-09-25 reasoning-visibility fix):
    # they get their own reserved budget so a heavy PM narrative can no
    # longer push the clip boundary back into them — see `_wrap_details`.
    detail_lines: list[str] = []
    _new_block(detail_lines, _append_pm, snap)
    _new_block(detail_lines, _append_signals, snap)
    protected_lines: list[str] = []
    _new_block(protected_lines, _append_risk, snap)
    _new_block(protected_lines, _append_gate_and_execution, result, snap)
    _budgeted_sections(
        lines,
        looked_at_slot,
        looked_at_rows,
        profiles,
        snap,
        detail_lines,
        protected_lines,
    )

    _new_block(lines, _append_footer, snap, elapsed, mode)
    # Last net: lines other modules built (common.py rows, guard notes) can quote a
    # field name too; the owner never sees one.
    return plain_prose("\n".join(lines))


def _held_symbols(snap: dict[str, Any]) -> list[str]:
    """Every symbol actually held, read from broker-truth `positions` —
    NOT the reviewer's self-reported `actions` list, which is LLM output
    and can under- or over-count (the defect this fixes: a live message
    once said "7 hold(s)" over 6 real positions, with only 2 ever named).
    Ordered exactly as `_append_book` already ranks them (by position
    size), excludes cash-sweep vehicles the same way."""
    positions = [row for row in (snap.get("positions") or []) if isinstance(row, dict)]
    return [
        str(row.get("symbol", "")).upper()
        for row in positions
        if str(row.get("symbol", "")).upper() not in _SWEEP_SYMBOLS
    ]
