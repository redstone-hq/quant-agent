"""Shared constants, wording helpers, row classification, budgeting and the read-only run lookup.

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
from src.trading_calendar import SESSION_WINDOWS, et_now, in_session_window, to_et

logger = logging.getLogger(__name__)

_DB_PATH = _NOTIFIER_DB_PATH
_SWEEP_SYMBOLS = frozenset({"SGOV", "BIL"})
_BASE_ONLY_STATUSES = frozenset(
    {
        "market_holiday",
        "early_close",
        "broker_error",
        "analysis_error",
        "fetch_error",
        # Guard 1 (2026-09-02): the kill switch's early check returns before
        # any of the rich per-mode data (orders/positions/trades) exists to
        # render, same shape-mismatch reason "broker_error" is here. Routes
        # to the base notifier.py formatter, which renders a plain
        # "status: kill_switch_halted" line and — critically — is NOT
        # silenced for intra_check the way "ok"/"market_holiday" are.
        "kill_switch_halted",
    }
)
# 2026-08-31 visibility fix (src/pipeline.py's `_run_intraday_opportunity_scan`
# / `_intraday_opportunity_scan_body`): these three now attach an explicit
# `result["intraday_scan"]["status"]` dict where a "never engaged a real
# candidate" tick used to leave no `intraday_scan` key at all. They stay off
# the Telegram feed on purpose — same as the old no-key ticks, per the
# "ordinary ~30-minute OK ticks are silent" policy below — because nothing
# about them needs an operator's attention: disabled-by-config and
# lock-contention are routine scheduling noise, and "no opportunity" means
# the scan ran and correctly found nothing. Only a real candidate engaged
# (intraday_no_trades/intraday_executed) or a genuine problem (crashed/
# suspended/analysis_error) is worth a message.
_INTRADAY_SILENT_STATUSES = frozenset(
    {
        "intraday_scan_disabled",
        "intraday_scan_lock_contended",
        "intraday_scan_no_opportunity",
        # item 121: morning released the owner lock on this same 09:30-shared
        # tick — still the open, not a real INTRADAY look, so no INTRADAY
        # OPPORTUNITY message is sold to the owner for it.
        "intraday_scan_open_overlap",
    }
)


def _fmt_elapsed(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.1f}s"
    return f"{int(seconds // 60)}m {int(seconds % 60)}s"


def _clip(value: Any, limit: int = 140) -> str:
    """Collapse whitespace/newlines to one line, then clip via the shared,
    boundary-aware `_clip_text` (src/notifier.py) instead of a raw
    `text[:limit]` slice.

    This used to hard-cut mid-word — the operator's actual complaint was a
    BUY CRM alert whose PM rationale read "...strong heavy accumulation
    volume" and just stopped there, an artifact of `_append_pm` calling
    this with `limit=105` on LLM prose that routinely runs 300-500+ chars.
    Callers below have also had their limits raised substantially (this
    formatter renders several such bullets per message, well inside
    Telegram's real 4096-char budget)."""
    text = " ".join(str(value or "").split())
    return _clip_text(text, limit, marker="…")


def _number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number


def _status_emoji(status: str) -> str:
    if status in {"executed", "intraday_executed"}:
        return "🟢"
    if status in {"reviewed", "intraday_no_trades", "no_trades", "ok"}:
        return "🔵"
    if status in {"rejected", "hard_risk_block", "symbol_block", "buys_unfunded"}:
        return "🟡"
    if "error" in status or status in {
        # "emergency_sold" and the halt status beside it are kept so runs
        # stored before their retirements still render RED rather than
        # falling through to the white "nothing happened" bucket; the owner
        # is red-green colour blind, so a halted historical run reading as
        # ordinary is the wrong kind of wrong. Nothing emits either any more
        # (item 32 — the liquidation half went 2026-09-14, the halt itself
        # 2026-09-20).
        "failed",
        "emergency_sold",
        "daily_loss_halted",  # retired-ok
        "kill_switch_halted",
    }:
        return "🔴"
    return "⚪"


# === Scan-first markup helpers (2026-09-17 redesign) ===
#
# Owner feedback on the pre-redesign messages: "unless I read everything
# word for word, I have no idea what was actually really done and what
# just failed or was killed." Every formatter below now leads with a
# plain-English outcome word and three short, structured sections — DONE /
# BLOCKED-FAILED / LOOKED AT, NO TRADE (HELD / WATCH for a position review)
# — built from the SAME underlying evidence the old wall-of-text used, so
# nothing is newly invented; it is re-shaped. The full per-stock reasoning
# the owner said he is happy with is kept, verbatim, inside a collapsed
# `<b>DETAILS</b>` / `<blockquote expandable>` block (Telegram Bot API HTML
# style, https://core.telegram.org/bots/api#html-style, documents
# `<blockquote>` and `<blockquote expandable>` as supported parse_mode=HTML
# tags) — one tap opens it. `<b>`/`<blockquote expandable>` are literal
# tags in the plain text this module returns; see
# `src.notifier._escape_with_markup` for how they survive `html.escape`
# without any other text being able to inject one.


def _b(text: str) -> str:
    """A literal `<b>...</b>` section header — see the module-level note
    above. Never build a `<b>` tag around text some other way; the escape
    step in src/notifier.py only preserves this exact fixed string."""
    return f"<b>{text}</b>"


# Board item 89 clarity defect — bare tickers after the twelfth name. The
# base formatter's cap (12) sized a separate "who:" block; the trader feed
# names companies inline, so every listed symbol needs one. Cache read only.
_COMPANY_NAME_CAP = 60


def _profiles(*groups: Any) -> dict[str, Any]:
    """Company profiles for every symbol across `groups` — one cache read
    per message, uncapped at the twelfth name (see `_COMPANY_NAME_CAP`)."""
    return _lookup_company_profiles(_all_symbols(*groups), limit=_COMPANY_NAME_CAP)


def _ticker_co(symbol: str, profiles: dict[str, Any] | None) -> str:
    """'SYM (Company Name)', or bare 'SYM' when the company cache doesn't
    know it — never a broken "(?)" placeholder, same posture as
    `src.notifier._append_company_identities`."""
    symbol = str(symbol or "?").upper()
    name = company_name(symbol, profiles) if profiles is not None else None
    return f"{symbol} ({name})" if name else symbol


def _all_symbols(*groups: Any) -> list[str]:
    """First-seen-order, deduped, upper-cased union of every symbol across
    `groups` (each a list of dicts with a 'symbol' key, or a plain list of
    ticker strings) — one CompanyProfileStore batch fetch per message
    instead of one per section."""
    seen: list[str] = []
    for group in groups:
        for item in group or []:
            raw = item.get("symbol") if isinstance(item, dict) else item
            sym = str(raw or "").strip().upper()
            if sym and sym not in seen:
                seen.append(sym)
    return seen


# Plain-English "who actually stopped it" for an execution-skip `reason`
# code (src/pipeline_stages.py's `_record_execution_skip` call sites) — the
# operator asked to be told desk safety check / risk manager / broker /
# not-filled-in-time, not an internal snake_case reason code, and the desk
# guards must say so explicitly — the whole point of this wording is that
# it is NOT the broker. Each value is a complete phrase ready to sit before
# a colon (`_append_blocked` renders "{who}: {reason}"). Risk-manager
# refusals and "not filled in time" are classified separately (they don't
# come through `execution_skips` — see `_blocked_rows`).
_SKIP_WHO_LABELS: dict[str, str] = {
    "fat_finger_guard": "Blocked by desk safety check (not the broker)",
    "unusable_stop": "Blocked by desk safety check — unusable stop (not the broker)",
    "bad_quantity": "Blocked by desk safety check — bad quantity (not the broker)",
    "kill_switch_halted": "Blocked by desk safety check — kill switch (not the broker)",
    "broker_rejected": "Blocked by the broker",
    "insufficient_cash": "Blocked by the desk — insufficient cash",
    "below_min_notional": "Blocked by the desk — order too small",
    "below_owner_min_risk": "Blocked by the desk — under the owner's minimum risk per position",
    "no_price": "Blocked by the desk — no verifiable price",
    "stale_entry": "Blocked by the desk — price moved since the decision",
    "qty_zero": "Blocked by the desk — sizing rounds to zero",
    "latency_window": "Blocked by the desk — too slow (latency)",
    "slippage_gated": "Blocked by the desk — price ran past the slippage limit",
    "borrow_gate": "Blocked by the desk — short not available to borrow",
    "short_add_blocked": "Blocked by the desk — adding to a short isn't supported",
    "rotation_room_not_freed": "Blocked by the desk — no rotation room freed",
}


def _skip_who(reason: str) -> str:
    return _SKIP_WHO_LABELS.get(str(reason or ""), "Blocked by the desk")


# A NORMAL OPERATING STATE IS NOT AN ERROR (owner, 2026-09-23).
#
# Every execution-skip `reason` below is a desk rule declining to place an
# order ON PURPOSE. The risk system refusing a trade it is built to refuse
# is the risk system working, and a session whose only blocks are these did
# not fail — it decided. The measured cases that forced this: "⚡ INTRADAY
# OPPORTUNITY · 9:49 AM ET · FAILED" on 2026-09-21 whose single block was
# `short_add_blocked`, and "🔵 MORNING · 9:36 AM ET · FAILED" on 2026-09-22
# whose only two blocks were the 200-session history pre-check refusing DRAM
# and CBRS while the PM's own verdict was `no_trades`.
#
# THE SET IS AN ALLOWLIST AND IT IS DELIBERATELY SHORT. Anything not named
# here — `broker_rejected`, `no_price`, `submit_failed`, a code added by a
# future change, a code this map has never seen — is classified as a FAULT
# and keeps the loud header. Getting a refusal wrongly called a failure is
# noise; getting a failure wrongly called a refusal is the owner not being
# told his desk broke, so the unknown case must fall the loud way.
#
# WHAT IS NOT ON IT, AND WHY — each of these was proposed for this set and
# thrown out against the production database on 2026-09-23:
#
#   slippage_gated / latency_window  NO LONGER PRODUCED on the quote path
#     (board item 183, 2026-09-30): the far-through-quote skip that emitted
#     them was deleted after all 8 recorded firings measured as venue noise
#     rather than a market that had run, and a displayed quote through the
#     entry ceiling is now recorded as `venue_quote_through_ceiling` with
#     the order still sent. `latency_window` survives on its own, separate
#     submit-window-overrun path. Both codes stay mapped below because
#     stored runs re-render through this table. The original finding, which
#     this deletion acts on: every one of the six `slippage_gated`
#     rows in the database is a venue-data problem wearing a policy code:
#     IEX asks 391, 405, 437, 488, 580 bp above reference against a 40bp
#     ceiling, on an account whose own code comments record that IEX
#     top-of-book is routinely stale. The 2026-09-15 run lost four
#     risk-approved buys this way; calling that session a correct no-trade
#     is exactly the cover story the desk's own doctrine forbids. The two
#     codes are also not independently determined — `src/pipeline_stages.py`
#     picks between them on `ctx.desk_latency_stall`, which is set at six
#     sites and cleared at none, so one stall anywhere in a run relabels
#     every later quote refusal in it.
#   short_add_blocked  Its own detail string reads "adding to a short is
#     not built". An unbuilt feature the desk keeps trying to use is not a
#     decision; it is the desk failing to act on its own signal, five times
#     on FLNC on 2026-09-21 alone.
#   geometry_rr  Dead. `_execution_payoff_skip_reason` returns None
#     unconditionally and its docstring says the token is not emitted. It
#     survives only in stored runs, which `render_stored_session_report`
#     re-renders through this code — so whitelisting it would retroactively
#     relabel the two historical reward:risk refusals as correct.
#   stale_entry  Zero rows in the entire database. Assigning owner-facing
#     meaning to a path that has never once run is a guess.
#   borrow_gate  A failed broker asset lookup is synthesised into
#     `{"shortable": False, "reason": "asset_lookup_failed"}` and filed
#     under this code, so an API failure would read as a borrow decision.
#   insufficient_cash / unusable_stop / bad_quantity / fat_finger_guard
#     "no cash", "no usable stop", "absurd quantity", "absurd price" must all
#     be said loudly: a guard firing means something upstream went wrong.
_DELIBERATE_SKIP_REASONS = frozenset(
    {
        # The $500 minimum trade size — one of the three the owner named.
        "below_min_notional",
        # The owner's minimum risk per position (owner rule 2026-08-27),
        # which replaced the $500 floor above.
        "below_owner_min_risk",
        # Deterministic sizing arithmetic resolving to nothing to place.
        "qty_zero",
        # A full book with nothing outranking a holding. PR #600 established
        # on the owner's own words that this is the desk's normal operating
        # state, not a fault: "Yes portfolio is full. But we're still reviewing
        # things, which is how we built it."
        "rotation_room_not_freed",
    }
)


def _skip_is_fault(reason: Any) -> bool:
    """True when an execution skip means something BROKE rather than a desk
    rule deciding not to trade — see `_DELIBERATE_SKIP_REASONS`."""
    return str(reason or "").strip() not in _DELIBERATE_SKIP_REASONS


# Board item 89 defect 5 — raw internal tokens printed to the owner word
# for word. Every value below is what the token MEANS, in the same words a
# person would use; the key is the internal spelling and never reaches the
# message. An unrecognised token is DESCRIBED ("the desk recorded an
# outcome it has no plain wording for"), never pasted through and never
# guessed at, because guessing what an unknown status means to a person
# reading it as a trading fact is the failure this rule exists to stop.
_ORDER_END_WORDS: dict[str, str] = {
    "canceled": "the order was cancelled before anything filled",
    "cancelled": "the order was cancelled before anything filled",
    "expired": "the order ran out of time before anything filled",
    "rejected": "the broker turned the order down",
    "submit_failed": "the order never reached the broker",
    "replaced": "the order was replaced by another one",
    "done_for_day": "the order was closed out at the end of the day unfilled",
    "suspended": "the order was suspended by the broker before filling",
    "stopped": "the broker stopped the order before it filled",
    "pending_cancel": "the order is being cancelled",
    "pending_replace": "the order is being replaced",
}

#: The same treatment for a fill state shown ALONGSIDE an order line, where
#: the sentence above would read oddly. Kept as a separate map rather than
#: reworded from one, so neither reads as a translation of the other.
_FILL_STATE_WORDS: dict[str, str] = {
    "filled": "filled",
    "submitted": "placed, waiting to fill",
    "pending_submit": "not yet at the broker",
    "partially_filled": "part-filled, still working",
    "canceled": "cancelled unfilled",
    "cancelled": "cancelled unfilled",
    "expired": "expired unfilled",
    "rejected": "turned down by the broker",
    "submit_failed": "never reached the broker",
}


def _machine_detail(text: Any) -> str:
    """Board item 89 defect 5 — the underlying fault text, LABELLED as
    machine output instead of pasted in as though it were a sentence
    written for the reader.

    It is kept rather than dropped: it is often the only record of what
    actually broke, and inventing a friendly paraphrase of an exception
    would be inventing a fact. What changes is that the owner is told what
    he is looking at and that there is nothing in it for him to do.
    """
    return f"Machine fault text, kept for the record — nothing here needs anything from you: {_clip(text, 900)}"


def _order_end_plain(fill_status: Any) -> str:
    token = str(fill_status or "").strip().lower()
    known = _ORDER_END_WORDS.get(token)
    if known:
        return known
    # 2026-09-24: this used to drop the raw broker token entirely, unlike
    # `notifier.humanize_status`'s own unmapped-status fallback, which keeps
    # it ("its own code for it, kept for the record, is ..."). Losing it
    # here meant an unmapped state was unrecoverable from the message.
    return (
        "the order never became a live fill, and the desk recorded an "
        "outcome it has no plain wording for (its own code for it, kept "
        f"for the record, is “{token or 'not recorded'}”)"
    )


def _fill_state_plain(fill_status: Any) -> str:
    token = str(fill_status or "").strip().lower()
    known = _FILL_STATE_WORDS.get(token)
    if known:
        return known
    return (
        f"state not recorded in plain words (its own code for it, kept for the record, is “{token or 'not recorded'}”)"
    )


def _decision_action_for(symbol: str, snap: dict[str, Any]) -> str:
    """The PM/constructor's own action word for `symbol` this run (BUY /
    SELL / SHORT / COVER / HOLD / ...), so a BLOCKED/FAILED line can say
    "SHORT FLNC" rather than a bare "? FLNC"."""
    for row in snap.get("pm_orders") or []:
        if isinstance(row, dict) and str(row.get("symbol", "")).upper() == symbol:
            return str(row.get("action", "?")).upper()
    return "?"


def _symbol_stop(symbol: str, snap: dict[str, Any]) -> float | None:
    """The stop QAMC actually intends for `symbol` — the constructor's own
    `TradeDecision.stop_loss`, overridden by a risk-manager modification of
    the SAME field when one exists (Risk can retune a symbol's stop without
    the constructor knowing — see `RiskModification`). Not the broker's
    fill data: `trades` carries no stop column at all."""
    stop: float | None = None
    for row in snap.get("pm_orders") or []:
        if isinstance(row, dict) and str(row.get("symbol", "")).upper() == symbol:
            stop = _number(row.get("stop_loss"))
            break
    for row in snap.get("risk_mods") or []:
        if (
            isinstance(row, dict)
            and str(row.get("symbol", "")).upper() == symbol
            and str(row.get("field", "")).lower() == "stop_loss"
        ):
            new_value = _number(row.get("new_value"))
            if new_value is not None:
                stop = new_value
    return stop


def _trade_reached_broker(fill_status: Any) -> bool:
    """True once an order is live at the broker — filled, or still working
    ('submitted'/'pending_submit'/'partially_filled'/'accepted'/'new'/
    'held'/'pending_new'). False for every terminal-fail status
    (`canceled`, `expired`, `rejected`, `submit_failed`, ...): a `trades`
    row exists, but nothing is protecting the operator's capital.

    2026-09-24: a normally-resting/working broker state used to be missing
    from this set, so a live, protected order (most commonly
    `partially_filled`, still resting for its remainder) fell into the
    "did not reach the broker" bucket and `_outcome_word` reported it as
    FAILED — a false alarm on an order that was, in fact, working.
    """
    return str(fill_status or "").lower() in {
        "filled",
        "submitted",
        "pending_submit",
        "partially_filled",
        "accepted",
        "new",
        "held",
        "pending_new",
    }


def _classify_trades(snap: dict[str, Any]) -> tuple[list[dict], list[dict]]:
    """Real (non-HOLD, non-sweep) trades split into (reached_broker,
    did_not_reach) — see `_trade_reached_broker`."""
    _, real = _execution_rows(snap)
    reached: list[dict] = []
    stalled: list[dict] = []
    for row in real:
        (reached if _trade_reached_broker(row.get("fill_status")) else stalled).append(row)
    return reached, stalled


def _done_rows(snap: dict[str, Any]) -> list[dict]:
    reached, _ = _classify_trades(snap)
    return reached


def _blocked_rows(result: dict, snap: dict[str, Any]) -> list[dict]:
    """Everything that did NOT make it, one row per symbol, each carrying
    who actually stopped it and why — see NEW LAYOUT item 4. Three sources,
    in priority order (a symbol is never listed twice): the execution
    skips QAMC's own deterministic gates recorded; symbols the risk manager
    refused outright (`RiskVerdict.rejected_symbols`, Phase 10.1 — a
    per-symbol refusal distinct from the whole-plan `approved` bool); and
    a real trade row that reached neither a fill nor a live working order
    (a DAY order that expired unfilled, a submit that ultimately failed).

    Every row also carries `"fault"`: True when it means something BROKE,
    False when it is a desk or risk rule declining on purpose. It is set
    HERE, at construction, because this is the only place the raw reason
    code is still in hand — `_append_blocked` and `_outcome_word` see only
    the plain English and could not tell the two apart. See
    `_DELIBERATE_SKIP_REASONS` for why an unrecognised code is a fault.
    """
    rows: list[dict] = []
    seen: set[str] = set()

    skips = [row for row in (result.get("execution_skips") or []) if isinstance(row, dict)]
    if not skips:
        skips = [row for row in (snap.get("skips") or []) if isinstance(row, dict)]
    for row in skips:
        symbol = str(row.get("symbol", "?")).upper()
        if symbol in seen:
            continue
        rows.append(
            {
                "symbol": symbol,
                "action": _decision_action_for(symbol, snap),
                "who": _skip_who(row.get("reason", "")),
                "reason": row.get("detail") or row.get("reason") or "blocked",
                "fault": _skip_is_fault(row.get("reason")),
            }
        )
        seen.add(symbol)

    risk = snap.get("risk")
    if isinstance(risk, dict):
        for row in risk.get("rejected_symbols") or []:
            if not isinstance(row, dict):
                continue
            symbol = str(row.get("symbol", "?")).upper()
            if symbol in seen:
                continue
            stated = str(row.get("reason") or "").strip()
            rows.append(
                {
                    "symbol": symbol,
                    "action": _decision_action_for(symbol, snap),
                    "who": "Blocked by risk manager",
                    "reason": stated or "refused without a stated reason",
                    # The risk seat refusing a name it is built to refuse is
                    # the risk seat working. But this field is free text with
                    # no code behind it, so the ONLY thing separating a working
                    # risk seat from a malfunctioning one here is whether it
                    # said why. A refusal with nothing written down stays loud.
                    "fault": not stated,
                }
            )
            seen.add(symbol)

    _, stalled = _classify_trades(snap)
    for row in stalled:
        symbol = str(row.get("symbol", "?")).upper()
        if symbol in seen:
            continue
        rows.append(
            {
                "symbol": symbol,
                "action": str(row.get("action", "?")).upper(),
                "who": "Not filled in time",
                # Board item 89 defect 5: this used to print the broker's own
                # status token verbatim ("(status: canceled)"). `_ORDER_END_WORDS`
                # says the same thing in words; an unmapped token is described,
                # never pasted.
                "reason": _order_end_plain(row.get("fill_status")),
                # An order the desk MEANT to place that reached neither a fill
                # nor a live working order is an intention that did not happen.
                # Loud.
                "fault": True,
            }
        )
        seen.add(symbol)

    # Board item 89 defect 6 — the silent drop. A target the constructor
    # ended before an order existed reached no message at all: it produced
    # no trade row, no execution skip and no risk verdict, so every one of
    # the three sources above missed it and the session read "orders: 0"
    # with no explanation. The reason it carries is already a plain-English
    # sentence written at the refusal site (`PortfolioConstructor._note_
    # refusal`), so nothing is invented here; the internal refusal CODE
    # beside it is deliberately NOT rendered.
    for row in snap.get("constructor_blocks") or []:
        if not isinstance(row, dict):
            continue
        symbol = str(row.get("symbol", "?")).upper()
        if symbol in seen:
            continue
        detail = str(row.get("detail") or "").strip()
        # A constructor block is a deliberate refusal ONLY when the
        # constructor recorded one as data — `_record_constructor_drops`
        # files those under `constructor_refused` with a named `refusal`
        # code beside them (measured on the whole production database:
        # insufficient_history 13, gross_exposure_ceiling_refused 5,
        # sector_crowding_leaves_below_min_order 2,
        # delta_below_min_trade_weight 1). Every one of those is a desk
        # rule declining on purpose. The other path — `constructor_dropped`,
        # recovered from a log line, or a row with no reason written down at
        # all — is exactly the case where the report CANNOT tell a rule from
        # a breakage, so it stays loud rather than guess.
        named_refusal = bool(str(row.get("refusal") or "").strip())
        fault = not (named_refusal and detail)
        if not detail:
            # Never a guess and never an internal code: say that the reason
            # was not written down.
            detail = "the desk ended this plan before placing an order and did not record why"
        rows.append(
            {
                "symbol": symbol,
                "action": _decision_action_for(symbol, snap),
                "who": "Stopped by the desk before an order was placed",
                "reason": detail,
                "fault": fault,
            }
        )
        seen.add(symbol)

    return rows


def _looked_at_rows(
    snap: dict[str, Any],
    candidates: list[str] | None,
    acted_symbols: set[str],
) -> list[dict]:
    """Analyzed signals that were neither done nor blocked — the PM/
    constructor's silent "pass". Addresses the 5-analyzed/5-actionable
    defect: the header used to claim more than the message ever said what
    happened to."""
    rows = []
    for row in _signal_rows(snap, candidates):
        symbol = str(row.get("symbol", "")).upper()
        if not symbol or symbol in acted_symbols:
            continue
        rows.append(row)
    return rows


def _traded_word(done_rows: list[dict] | None) -> str:
    """Board item 89 clarity defect — a 'TRADED' header on a run that only
    sold. The word is read off the actions that actually reached the
    broker: all exits read SOLD, all entries read BOUGHT, a mix reads
    TRADED. An unknown action falls back to TRADED rather than guessing
    a direction.

    2026-10-09: the word is read off FILLS, never off submitted orders. A
    row that only reached the broker (`submitted` / `pending_new` /
    `accepted` ...) has sold or bought nothing yet, so it cannot earn SOLD
    or BOUGHT; when no row has filled at all the word is ORDERED."""
    filled = [
        row
        for row in (done_rows or [])
        if isinstance(row, dict) and str(row.get("fill_status") or "").lower() in {"filled", "partially_filled"}
    ]
    if done_rows and not filled:
        return "ORDERED"
    actions = {str(row.get("action", "")).upper() for row in filled}
    exits = {"SELL", "REDUCE", "COVER", "TRIM"}
    entries = {"BUY", "SHORT", "ADD"}
    if actions and actions <= exits:
        return "SOLD"
    if actions and actions <= entries:
        return "BOUGHT"
    return "TRADED"


def _fault_count(blocked_rows: list[dict] | None) -> int:
    """How many blocked rows mean something BROKE. A row with no `fault`
    key is counted as one: an unclassified block is the case the report
    cannot tell apart, and the unknown case falls the loud way."""
    return sum(1 for row in (blocked_rows or []) if not isinstance(row, dict) or row.get("fault", True))


def _outcome_word(
    status: str,
    done_count: int,
    blocked_count: int,
    done_rows: list[dict] | None = None,
    fault_count: int | None = None,
) -> str:
    """ONE plain word for the header line, computed from the SAME rows the
    sections below render, so the header can never claim something the body
    doesn't show. `done_rows` lets the word say which way the trades went.

    THE VOCABULARY, and which state earns which word:

      FAILED     something broke — a red status, or a blocked row
                 classified as a fault (`_blocked_rows`).
      PARTIAL    orders reached the broker AND something also broke.
      BOUGHT /   orders reached the broker; the word says which way
      SOLD /     (`_traded_word`, read off fills). Correct refusals
      TRADED     alongside them do not change it: the session traded.
      ORDERED    orders reached the broker but none has filled yet.
      NO TRADE   nothing reached the broker, and every block was a desk or
                 risk rule declining on purpose. A normal operating state.
      NO CHANGE  nothing reached the broker and nothing was declined.

    WHY "NO TRADE" EXISTS. Until 2026-09-23 any blocked row at all returned
    FAILED, so the gross-exposure ceiling, the $500 minimum trade size and
    the 200-session history pre-check each put the word FAILED on the top
    line of a session that had worked correctly. Owner's instruction, in his
    words: a normal operating state must not be reported as an error. It is
    a separate word from NO CHANGE on purpose — "the desk weighed names and
    declined them" and "the desk had nothing to weigh" are different facts
    and the owner has to be able to tell them apart from the header alone.

    `fault_count=None` keeps the pre-2026-09-23 behaviour (every block
    counts as a fault) for any caller that has not been given rows to
    classify — the loud direction, never the quiet one.
    """
    if _status_emoji(status) == "🔴":
        return "FAILED"
    faults = blocked_count if fault_count is None else fault_count
    if done_count and faults:
        return "PARTIAL"
    if done_count:
        return _traded_word(done_rows)
    if faults:
        return "FAILED"
    if blocked_count:
        return "NO TRADE"
    return "NO CHANGE"


# Reserved characters, not a trading number: `_wrap_details` must guarantee
# its own output fits inside `TelegramNotifier.MAX_MESSAGE_CHARS` so the
# tag-blind emergency clip in `notifier._build_payload` never has to run
# for an ordinary alert (that fallback can only cut on a text/entity
# boundary, not a `<blockquote expandable>` boundary). Covers the Mission
# Control `<a href>` link `send()` appends after this text, plus slack for
# `html.escape` expanding a handful of '&'/'"'/"'" characters in PM/risk
# prose — this module never learns the real escaped length, only text.py's
# `_build_payload` does.
_DETAILS_SAFETY_RESERVE_CHARS = 250


def _wrap_details(
    lines: list[str],
    detail_lines: list[str],
    extra_reserve: int = 0,
    *,
    protected_lines: list[str] | None = None,
) -> int:
    """Append the full, unabridged per-stock reasoning as a collapsed
    `<b>DETAILS</b>` / `<blockquote expandable>` block — NEW LAYOUT item 7.
    Content is unchanged from the pre-redesign message; only its
    presentation (collapsed, tapped open) is new. Sized to fit the
    remaining Telegram budget so a clip, if one is needed, lands inside
    DETAILS and never inside the scan-first sections above it.

    `extra_reserve` is room for a section the caller has NOT appended yet —
    the candidate list, which `_budgeted_sections` deliberately holds back
    so this block gets its share of the budget first. Returns the budget
    this block did not use, which is what the caller then spends on
    rendering that list at a richer tier.

    `protected_lines` — the Risk verdict + execution/gate record
    (`_append_risk` + `_append_gate_and_execution`). Reasoning-visibility
    gap (2026-09-25): `detail_lines` used to be one flat blob clipped from
    the tail by `_clip_text`, in append order PM -> Risk -> Execution ->
    Signals. That protected Risk/Execution from the (huge, per-candidate)
    Signals listing, but NOT from PM: a heavy PM narrative (several
    actionable orders at up to 420 chars of reasoning each, board item 89)
    could alone exceed a tight budget and push the clip boundary back
    into Risk/Execution — the one place besides the DONE line itself that
    names a real decision the desk made, appearing nowhere else in the
    message. This reserves `protected_lines`' own budget FIRST, renders it
    in full whenever it fits, and clips only the PM/Signals prose (in
    `detail_lines`, PM prioritised over Signals as before) with whatever
    is left over.
    """
    protected_text = "\n".join(line for line in (protected_lines or []) if line is not None).strip("\n")
    free_text = "\n".join(line for line in detail_lines if line is not None).strip("\n")
    wrapper_overhead = len("<b>DETAILS</b>\n<blockquote expandable></blockquote>")
    used = len("\n".join(lines))
    budget = (
        TelegramNotifier.MAX_MESSAGE_CHARS
        - used
        - wrapper_overhead
        - _DETAILS_SAFETY_RESERVE_CHARS
        - max(0, extra_reserve)
    )
    budget = max(0, budget)
    marker = "\n[details truncated — see Mission Control]"

    if not protected_text and not free_text:
        return budget

    # Reserve the protected block's own room first — bounded already by the
    # per-field `_clip` calls inside `_append_risk`/`_append_gate_and_
    # execution`, so this is never the unbounded side. Only a pathological
    # budget (extremely tight, or an unexpectedly huge protected block)
    # clips it at all, and even then it's clipped LAST, after free content
    # has already given up everything it can.
    join_cost = 1 if protected_text and free_text else 0
    protected_budget = min(len(protected_text), max(0, budget))
    free_budget = max(0, budget - protected_budget - join_cost)
    if len(free_text) > free_budget:
        free_text = _clip_text(free_text, free_budget, marker=marker)
    if len(protected_text) > protected_budget:
        protected_text = _clip_text(protected_text, protected_budget, marker=marker)

    parts = [part for part in (free_text, protected_text) if part]
    text = "\n".join(parts)
    start = len(lines)
    lines.append(_b("DETAILS"))
    lines.append(f"<blockquote expandable>{text}</blockquote>")
    _seal_section(lines, start)
    return max(0, budget - len(text))


def _budgeted_sections(
    lines: list[str],
    slot: int,
    looked_at_rows: list[dict],
    profiles: dict,
    snap: dict[str, Any] | None,
    detail_lines: list[str],
    protected_lines: list[str] | None = None,
) -> None:
    """Fit BOTH the candidate list and the reasoning block into one
    message, in the owner's order of need — and the reason the candidate
    list is appended late rather than in place.

    THE DEFECT THIS FIXES. `_wrap_details` sizes itself against whatever is
    already in `lines`. The candidate list was built first and, measured on
    run-fccb2026 (2026-09-23) replayed against post-#600 code, ran to 8,376
    characters against a 4,000-character budget — so DETAILS got a negative
    budget and rendered EMPTY. Live proof: that morning's message, and
    2026-09-21's and 2026-09-22's, all end
    `<blockquote expandable></blockquote>`. The desk's reasoning chain was
    the guaranteed daily casualty while a list of names it had already
    declined survived intact. Nothing was wrong with either section; the
    ORDER in which they claimed the budget was wrong.

    THE ORDER, highest priority first:

      1. Everything already in `lines` — heading, P&L, the stop-coverage
         and research banners, DONE, NOT TAKEN / FAILED. Never squeezed.
      2. The candidate list at its tier-3 floor (`_looked_at_block`) —
         every name, no detail. Reserved before DETAILS is sized, so the
         list can never be erased either.
      3. The reasoning block, which takes what is left.
      4. Whatever the reasoning did not need goes back into rendering the
         candidate list at a richer tier.

    Step 4 is what stops this being a straight swap of one casualty for
    another: on a quiet session with little reasoning, the list still
    renders in full.
    """
    floor = _looked_at_block(looked_at_rows, profiles, snap, budget=0)
    # +1 for the blank separator line `_seal_section` would insert.
    reserve = (len("\n".join(floor)) + 1) if floor else 0
    spare = _wrap_details(
        lines,
        detail_lines,
        extra_reserve=reserve,
        protected_lines=protected_lines,
    )
    if not floor:
        return
    block = _looked_at_block(
        looked_at_rows,
        profiles,
        snap,
        budget=max(0, reserve - 1 + spare),
    )
    lines[slot:slot] = ([""] + block) if slot > 0 else block


def _empty_snapshot() -> dict[str, Any]:
    return {
        "macro": None,
        "tech": [],
        "pm_reasoning": None,
        "pm_targets": [],
        "pm_orders": [],
        "risk": None,
        "risk_mods": [],
        "skips": [],
        # Board item 89 defect 6. Targets the CONSTRUCTOR ended before an
        # order was ever built. Deliberately a separate key from `skips`:
        # `skips` is read by `_intraday_tick_actionable` to decide whether a
        # quiet half-hour tick speaks at all, and the owner's ratified
        # silence rule says a tick with nothing to act on sends nothing.
        # These rows only ever ADD lines to a message that is already going
        # out; they never cause one to be sent.
        "constructor_blocks": [],
        # The portfolio manager's per-candidate accounting (board item 133,
        # `src/pm_accounting.py`) — the named ground for every candidate it
        # did not target, keyed by symbol. It was written to
        # `specialist_evidence` from the day item 133 shipped and read by
        # nothing: `_read_run` only ever admitted `deterministic_gate`
        # pipeline events, so the reasons never reached this snapshot and
        # LOOKED AT told the owner the desk "did not record why" about
        # candidates it had recorded a ground for seconds earlier.
        "pm_accounting": {},
        # The opportunity-rotation pre-check for this run (one row, see
        # `_record_rotation_precheck`). A full book is a normal state, and
        # this is where the report gets to say so.
        "rotation": None,
        "trades": [],
        "positions": [],
        "agent_summaries": {},
        "cost": None,
        "calls": 0,
    }


def _read_run(run_id: str | None) -> dict[str, Any]:
    """Read forensic state for one run through a SQLite read-only connection."""
    snapshot = _empty_snapshot()
    if not run_id or run_id == "?" or not Path(_DB_PATH).exists():
        return snapshot

    conn = None
    try:
        uri = f"file:{Path(_DB_PATH).resolve()}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=1.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=1000")

        try:
            rows = conn.execute(
                "SELECT agent_name, kind, symbol, evidence_json FROM specialist_evidence WHERE run_id = ? ORDER BY id",
                (run_id,),
            ).fetchall()
            for row in rows:
                try:
                    data = json.loads(row["evidence_json"] or "{}")
                except (TypeError, ValueError, json.JSONDecodeError):
                    continue
                agent = row["agent_name"]
                kind = row["kind"]
                if agent == "macro_analyst" and kind == "analysis":
                    snapshot["macro"] = data
                elif agent == "tech_analyst" and kind == "analysis":
                    snapshot["tech"].append(data)
                elif agent == "portfolio_manager" and kind == "reasoning":
                    snapshot["pm_reasoning"] = data
                elif agent == "portfolio_manager" and kind == "target":
                    snapshot["pm_targets"].append(data)
                elif agent == "portfolio_manager" and kind == "proposed_order":
                    snapshot["pm_orders"].append(data)
                elif agent == "risk_manager" and kind == "verdict":
                    snapshot["risk"] = data
                elif agent == "risk_manager" and kind == "modification":
                    snapshot["risk_mods"].append(data)
                elif agent == "execution" and kind == "execution_skip":
                    snapshot["skips"].append(data)
                elif agent == "pipeline" and kind == "pipeline_event":
                    # Board item 89 defect 6: a plan the constructor ended
                    # before building an order left a durable per-symbol row
                    # here (`_record_constructor_drops` in
                    # src/pipeline_stages.py) and reached NO message at all —
                    # not the order list, not BLOCKED/FAILED, nothing. The
                    # owner could not know a decision had been made. Read
                    # only the terminal blocked outcomes; `unmeasurable`
                    # (data faults) already pages separately.
                    stage = str(data.get("stage") or "")
                    outcome = str(data.get("outcome") or "")
                    if stage == "deterministic_gate" and outcome == "blocked" and row["symbol"]:
                        snapshot["constructor_blocks"].append({**data, "symbol": row["symbol"]})
                    # The PM's per-candidate accounting. One row per
                    # non-targeted candidate, every one carrying the named
                    # ground the seat gave (or the honest record that it
                    # would not give one). Last row for a symbol wins: the
                    # accounting re-ask re-records the names it healed.
                    elif stage == "portfolio_manager" and row["symbol"]:
                        snapshot["pm_accounting"][str(row["symbol"]).upper()] = dict(data)
                    elif stage == "rotation" and outcome == "precheck":
                        # The event's `reason` slot carries the named
                        # pre-check outcome (`rotation.precheck_outcome`);
                        # `outcome` here is the event kind. Renamed back to
                        # the field `owner_precheck_lines` reads, so the
                        # audit row and the owner's sentence stay one
                        # vocabulary rather than two spellings of it.
                        snapshot["rotation"] = {
                            **data,
                            "outcome": data.get("reason"),
                        }
        except sqlite3.DatabaseError:
            pass

        try:
            rows = conn.execute(
                "SELECT symbol, action, qty, price, reasoning, fill_status, "
                "fill_qty, fill_price FROM trades WHERE run_id = ? ORDER BY id",
                (run_id,),
            ).fetchall()
            snapshot["trades"] = [dict(row) for row in rows]
        except sqlite3.DatabaseError:
            pass

        try:
            rows = conn.execute(
                "SELECT symbol, qty, avg_entry, current_price, market_value, "
                # qty != 0: shorts have a negative qty and must not be
                # invisible in the trader feed.
                "unrealized_pnl FROM positions WHERE qty != 0 "
                "ORDER BY ABS(market_value) DESC",
            ).fetchall()
            snapshot["positions"] = [dict(row) for row in rows]
        except sqlite3.DatabaseError:
            pass

        try:
            try:
                rows = conn.execute(
                    "SELECT agent_name, output_summary, cost_usd, provider_requests "
                    "FROM agent_logs WHERE run_id = ? ORDER BY id",
                    (run_id,),
                ).fetchall()
                snapshot["calls"] = sum(
                    (1 if row["provider_requests"] is None else max(0, int(row["provider_requests"]))) for row in rows
                )
            except sqlite3.DatabaseError:
                rows = conn.execute(
                    "SELECT agent_name, output_summary, cost_usd FROM agent_logs WHERE run_id = ? ORDER BY id",
                    (run_id,),
                ).fetchall()
                snapshot["calls"] = len(rows)
            for row in rows:
                snapshot["agent_summaries"][row["agent_name"]] = row["output_summary"]
            # Board-item defect (2026-09-29 log): this used to sum only the
            # successful `agent_logs.cost_usd` rows, so a run whose ONLY
            # provider activity was a charged-but-failed call (402s the
            # cost-circuit logged as "not every attempt is provably $0",
            # see `_all_attempts_provably_free` in src/cost_circuit.py)
            # summed to $0.00 here and the footer told the owner the run
            # was free while the circuit's own ledger had just marked that
            # exact session `costs_exact=0`. `_canonical_run_cost` is the
            # one place that already gets this right (it backs Mission
            # Control's run list) -- it prefers the circuit's settled
            # total and returns None, not 0, whenever that total is not
            # provably exact, which `describe_ai_cost` renders as "not
            # available" rather than the false "none". Reused here instead
            # of re-implementing it so the two surfaces cannot drift apart.
            from src.api.db_reads import _canonical_run_cost

            snapshot["cost"] = _canonical_run_cost(conn, run_id, rows)
        except sqlite3.DatabaseError:
            pass
    except Exception as exc:  # noqa: BLE001
        logger.warning("trader-feed DB read failed for %s: %s", run_id, exc)
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass
    return snapshot


def _signal_rows(
    snap: dict[str, Any],
    candidates: list[str] | None = None,
) -> list[dict]:
    """The tech rows a signals listing renders as bullets — filtered to
    `candidates` when given, priority-ordered — shared by `_append_signals`
    (the live per-run listing) and the hourly desk-check summary so both
    name exactly the symbols they actually show, never more and never a
    second, divergent ordering.

    Uncapped (2026-09-17): this used to cut off at 4 rows while the header
    line right above it ("N analyzed") kept the true count — a live
    message read "5 analyzed" and then listed only 4, silently dropping
    the 5th (SOXX). The header must never claim more than the bullets
    beneath it actually show.
    """
    tech = [row for row in (snap.get("tech") or []) if isinstance(row, dict)]
    if candidates:
        wanted = {str(symbol).upper() for symbol in candidates}
        tech = [row for row in tech if str(row.get("symbol", "")).upper() in wanted]
    priority = {"strong_buy": 0, "strong_sell": 0, "buy": 1, "sell": 1, "neutral": 2}
    return sorted(
        tech,
        key=lambda row: (
            priority.get(str(row.get("rating", "")).lower(), 3),
            str(row.get("symbol", "")),
        ),
    )


def _execution_rows(snap: dict[str, Any]) -> tuple[list[dict], list[dict]]:
    trades = [row for row in (snap.get("trades") or []) if isinstance(row, dict)]
    sweep = [row for row in trades if str(row.get("action", "")).upper().startswith("SWEEP_")]
    real = [
        row
        for row in trades
        if str(row.get("action", "")).upper() != "HOLD" and not str(row.get("action", "")).upper().startswith("SWEEP_")
    ]
    return sweep, real


def _pm_pass_reason(
    symbol: str,
    snap: dict[str, Any] | None,
) -> tuple[str | None, str]:
    """The desk's own recorded ground for not trading `symbol` this run, in
    plain words, or None when there genuinely is not one.

    TWO sources, in the order of how directly they answer the question:

      1. The per-candidate accounting row (`src/pm_accounting.py`, board
         item 133). This is the seat being MADE to account for every
         candidate it dropped, so on an ordinary session it covers all of
         them. It was persisted from the day item 133 shipped and read by
         nothing — see the `pm_accounting` note in `_empty_snapshot` — and
         that is the whole defect this function fixes: on 2026-09-23 the
         desk logged "every one of the 69 non-targeted candidate(s) carries
         a named ground" seven-tenths of a second before telling the owner,
         sixty-eight times over, that it "did not record why".
      2. Failing that, a HOLD order the PM wrote its own reasoning on.

    The accounting row's CODE is never shown; `pm_accounting.plain_reason`
    turns it into the sentence a person would use.

    Returns `(ground, detail)`. The GROUND is the shared sentence the
    renderer groups on — several names die on the same one — and the DETAIL
    is that one name's own specifics, which must stay off the group header
    or nothing would ever group. A `None` ground is reserved for an honest
    gap: a candidate with no recorded ground at all.
    """
    from src.pm_accounting import plain_reason

    accounted = ((snap or {}).get("pm_accounting") or {}).get(symbol)
    if isinstance(accounted, dict):
        code = str(accounted.get("refusal") or "").strip()
        if code:
            return plain_reason(code), _clip(accounted.get("note"), 300) or ""
    for row in (snap or {}).get("pm_orders") or []:
        if (
            isinstance(row, dict)
            and str(row.get("symbol", "")).upper() == symbol
            and str(row.get("action", "")).upper() == "HOLD"
        ):
            reason = _clip(row.get("reasoning"), 300)
            if reason:
                return reason, ""
    return None, ""


def _looked_at_groups(
    rows: list[dict],
    profiles: dict,
    snap: dict[str, Any] | None,
) -> list[tuple[str, list[tuple[str, str, str]]]]:
    """`[(ground, [(ticker, name_with_rating, detail), ...]), ...]`, groups
    in the order their first name appears — which is the existing priority
    ordering from `_signal_rows`, so the strongest ratings still lead.

    Split out of `_looked_at_block` so all four render tiers below
    group identically: a tier that grouped differently from another could
    show the owner a different count for the same session.
    """
    grouped: dict[str, list[tuple[str, str, str]]] = {}
    for row in rows:
        symbol = str(row.get("symbol", "?")).upper()
        rating = str(row.get("rating", "?")).upper()
        conviction = str(row.get("conviction", "?")).lower()
        reason, detail = _pm_pass_reason(symbol, snap)
        # The honest fallback is conditioned on there being NO recorded
        # ground — never on the renderer being unable to reach one.
        ground = (
            f"the desk did not take these because {reason}"
            if reason
            else "the desk did not record why it passed on these"
        )
        grouped.setdefault(ground, []).append(
            (symbol, f"{_ticker_co(symbol, profiles)} {rating}/{conviction}", detail),
        )
    return list(grouped.items())


def _looked_at_block(
    rows: list[dict],
    profiles: dict,
    snap: dict[str, Any] | None = None,
    budget: int | None = None,
) -> list[str]:
    """NEW LAYOUT item 5 — analyzed signals the PM/constructor passed on,
    rendered at the richest of three tiers that fits `budget`.

    GROUPED BY THE GROUND, not one reason per name (PR #600). A real
    session leaves sixty-odd candidates here and most of them die on the
    SAME ground — on 2026-09-23, thirty-nine of sixty-nine on one.

    WHY THERE ARE TIERS. Measured on run-fccb2026 (2026-09-23, replayed
    against the post-#600 code): this block alone renders 8,376 characters
    for 68 candidates, against a 4,000-character message budget. It was
    built first and took all of it, which is why the DETAILS block carrying
    the desk's actual reasoning rendered EMPTY in every live morning
    message. Clipping was not an option either: the clip would land
    mid-list and silently lose names.

    So the block degrades instead, and NO NAME IS EVER DROPPED at any tier
    — the #600 rule stands that a grouping which hides a name is a worse
    failure than a repetitive one. What is given up, in order, is the
    per-name detail sentence and then the one-name-per-line layout:

      tier 1  ground heading, then each name on its own line with its own
              detail sentence.
      tier 2  ground heading, then each name on its own line.
      tier 3  ground heading, then every name and its rating comma-joined
              on one line. Telegram soft-wraps it.
      tier 4  the same, tickers only.

    The company name goes before the rating because a ticker the owner does
    not recognise tells him nothing (board item 89), so it is the last
    thing given up rather than the first.

    Tier 4 is the FLOOR and is returned whatever `budget` says, because a
    block that renders nothing would put the owner back where #600 found
    him. The count in every heading still accounts for every candidate at
    every tier. `budget=None` means "no ceiling" — tier 1, the old
    behaviour, which is what the position-review and evening formatters
    and the stored-report replay path still get.
    """
    if not rows:
        return []
    groups = _looked_at_groups(rows, profiles, snap)
    header = _b("👀 LOOKED AT, NO TRADE")

    def _tier1() -> list[str]:
        out = [header]
        for ground, entries in groups:
            out.append(f"   ▪ {len(entries)} — {ground}")
            # A per-name detail that is word-for-word the same for every
            # name under the heading is the heading again — the
            # `held_unchanged` accounting note is one fixed sentence, and
            # printing it twelve times under a heading that already says it
            # is the repetition this grouping exists to remove. Dropped
            # only when it is shared by the whole group AND the group has
            # more than one name, so a name whose detail is its own is
            # never silently lost.
            details = {detail for _, _, detail in entries}
            shared = len(entries) > 1 and len(details) == 1
            for _symbol, name, detail in entries:
                out.append(f"      • {name} — {detail}" if detail and not shared else f"      • {name}")
        return out

    def _tier2() -> list[str]:
        out = [header]
        for ground, entries in groups:
            out.append(f"   ▪ {len(entries)} — {ground}")
            for _symbol, name, _detail in entries:
                out.append(f"      • {name}")
        return out

    def _tier3() -> list[str]:
        out = [header]
        for ground, entries in groups:
            out.append(f"   ▪ {len(entries)} — {ground}")
            out.append("      " + ", ".join(name for _s, name, _d in entries))
        return out

    def _tier4() -> list[str]:
        out = [header]
        for ground, entries in groups:
            out.append(f"   ▪ {len(entries)} — {ground}")
            out.append("      " + ", ".join(symbol for symbol, _n, _d in entries))
        return out

    for build in (_tier1, _tier2, _tier3):
        block = build()
        if budget is None or len("\n".join(block)) <= budget:
            return block
    return _tier4()


def _fmt_pnl_line(label: str, pnl: float | None, ret: float | None) -> str:
    """'📈 Today's P&L: +$12.34 (+0.10%)' — a missing figure renders as the
    honest 'not available', never a fabricated 0. If BOTH are unknown the
    parenthetical is dropped too ('not available (not available)' reads as
    a bug, not an honest gap)."""
    if pnl is None and ret is None:
        return f"{label} not available"
    pnl_text = _fmt_signed_money(pnl) if pnl is not None else "not available"
    ret_text = f"{ret:+.2f}%" if ret is not None else "not available"
    return f"{label} {pnl_text} ({ret_text})"


def _pnl_section_lines(result: dict) -> list[str]:
    """The two-line P&L block every trader-feed message shows in the spot
    the old, undated 'Session P&L' line used to sit (owner request,
    2026-09-17): he did not know whether "session" meant this run, this
    calendar day, or something else — it actually meant this run's/tick's
    OWN process, i.e. it was silently absent on every midday/close message
    (`run_position_review` never set `daily_pnl` at all) and, when it did
    show on an intraday tick, was really just today's account change under
    an unexplained label.

    'Today's P&L' is the broker's own day-over-day account change
    (today's total value vs. its `last_equity`, i.e. official prior-close
    equity) — the SAME basis `run_evening`'s already-trusted 'Daily P&L'
    line uses (src/notifier.py), just not yet 4pm-close-verified mid-day.

    'Total P&L' is deliberately labelled with the date it starts from
    rather than called "total" bare: the 2026-09-02 book-wide liquidation
    archived every earlier record (docs/INCIDENT_HISTORY.md), so a total
    spanning that boundary would be meaningless — seeing "total" would
    read as "since the account began", which it is not. See
    `TradingPipeline._total_pnl_since_reset` for exactly which recorded
    value the baseline comes from (never reconstructed).
    """
    today_pnl = _number(result.get("daily_pnl"))
    today_ret = _number(result.get("daily_return_pct"))
    total_pnl = _number(result.get("total_pnl"))
    total_ret = _number(result.get("total_return_pct"))
    since = result.get("total_pnl_since")
    total_label = f"📊 Total P&L since {since}:" if since else "📊 Total P&L:"
    lines = [
        _fmt_pnl_line("📈 Today's P&L:", today_pnl, today_ret),
        _fmt_pnl_line(total_label, total_pnl, total_ret),
    ]
    # Owner, 2026-09-18: P&L leads EVERY message, and a message that cannot
    # know the figure says so rather than dropping the block — an absent
    # block and a broken figure look identical to a reader. `_fmt_pnl_line`
    # already renders each missing figure as "not available"; when NOTHING
    # is known, one short sentence says why, so "not available" is never
    # left looking like a fault.
    if today_pnl is None and today_ret is None and total_pnl is None and total_ret is None:
        lines.append("   " + _pnl_unavailable_sentence(result))
    return lines


# Why a figure is missing, keyed by the code the SESSION sets on its own
# result (`TradingPipeline._attach_pnl` / `run_earnings_preprocess`).
#
# Owner rule: an explanation the code cannot prove is itself a defect. The
# old block asserted "this message was built without an account read"
# whenever the four keys were absent — which was false for every trading
# session (2026-09-23: the same morning message that said it went on to
# print "Book: 12 position(s) · $18,178 invested"). The absence of the keys
# is evidence of nothing but their absence, so the sentence now comes from
# the only place that KNOWS the reason: the run that did or did not read the
# account. An unrecognised or absent code falls back to a sentence that
# claims no cause at all.
_PNL_UNAVAILABLE_REASONS = {
    # The message's own mode does no account read at all (pre-market
    # filing reader).
    "no_account_read": "This message was built without an account read, so there is no figure yet.",
    # The session ended — holiday short-circuit, kill switch, broker
    # snapshot failure — before it reached its account read.
    "ended_before_account_read": "This run ended before the account was read, so there is no figure yet.",
    # The account WAS read, but the broker reported no usable prior-day
    # close to measure today's change against.
    "no_prior_close": "The broker reported no prior-day close, so today's change cannot be measured.",
}
_PNL_UNAVAILABLE_FALLBACK = "No P&L figure was recorded with this message."


def _pnl_unavailable_sentence(result: dict) -> str:
    code = result.get("pnl_unavailable_reason")
    return _PNL_UNAVAILABLE_REASONS.get(str(code or ""), _PNL_UNAVAILABLE_FALLBACK)
