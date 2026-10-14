"""Leverage line, company identities, target revisions.

Moved verbatim from the former src/notifier.py; the package re-exports it.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from src.notifier.base import (
    _clip_text,
    logger,
)
from src.notifier.markup import (
    _dedupe_symbols,
)


def _append_leverage_line(lines: list[str], result: dict) -> None:
    """Spec §11.2 — how much the book owns, its ceiling, and how far it could
    fall before the broker sells without asking.

    Nothing watched the distance to forced liquidation before this. At the
    ratified 2.0x it reads about 33% — a bad quarter, not an impossibility —
    which is precisely why it belongs on the alert the operator actually
    reads rather than in a log.

    Rendered whenever the run measured it. Silent when the block is absent
    (an older result dict, or a session that never reached the preamble) —
    an omitted line is honest; an invented "1.0x" would not be.
    """
    leverage = result.get("leverage")
    if not isinstance(leverage, dict) or not leverage:
        return
    gross_x = leverage.get("gross_x")
    ceiling_x = leverage.get("ceiling_x")
    if not isinstance(gross_x, (int, float)) or not isinstance(ceiling_x, (int, float)):
        return
    # Colour-blind-safe: the state is carried by the WORD, never by hue alone.
    de_levered = isinstance(leverage.get("base_ceiling_x"), (int, float)) and ceiling_x < leverage["base_ceiling_x"]
    parts = [f"exposure: {gross_x:.2f}x of {ceiling_x:.2f}x allowed"]
    distance = leverage.get("distance_to_forced_liquidation_pct")
    if isinstance(distance, (int, float)):
        parts.append(f"{distance:.0f}% fall to a margin call")
    drawdown = leverage.get("drawdown_pct")
    if isinstance(drawdown, (int, float)) and drawdown < 0:
        parts.append(f"{abs(drawdown):.1f}% below the equity high")
    prefix = "⚠️ DE-LEVERED" if de_levered else "leverage"
    lines.append(f"{prefix} — {'  ·  '.join(parts)}")
    if leverage.get("alert_owner"):
        # The rung is READ from the resolved ceiling, never restated as a
        # literal: a hardcoded "0.5x" here would go stale the day the ratified
        # ladder changes, and an alert that misreports the cap is worse than
        # no alert. And the wording is exact — at the lowest rung new
        # positions are refused ONCE THE BOOK REACHES the cap, not
        # unconditionally; a book already below it may still trade.
        #
        # 2026-09-18: `alert_owner` is not only the deepest rung. It is
        # also set when the drawdown could not be MEASURED at all (an
        # unreadable equity read, rung "bad_read", which has set it since
        # 2026-09-02; and an absent equity curve, rung "unknown"). Printing
        # a measured-drawdown message for those said something false
        # about the book — the owner would read a measured -20% where
        # nothing had been measured. The message is chosen from the RUNG,
        # so a state that was never measured never reports a number.
        rung = leverage.get("rung")
        if rung == "unknown":
            lines.append(
                f"🛑 DRAWDOWN UNMEASURABLE: there is no equity history to "
                f"measure a high-water mark against, so the de-levering "
                f"ladder cannot fire at all. This is NOT a book at record "
                f"highs. Nothing is being trimmed and gross exposure is "
                f"held to the standing {ceiling_x:.2f}x cap until a real "
                f"equity curve exists."
            )
        elif rung == "bad_read":
            lines.append(
                f"🛑 DRAWDOWN UNMEASURABLE: the account equity reading came "
                f"back unusable, so the book's drawdown cannot be verified. "
                f"Gross exposure is held to the ladder's floor rung "
                f"({ceiling_x:.2f}x equity) until a valid reading arrives."
            )
        else:
            # 2026-09-30 (board item 182): the owner-alert level moved from
            # -20% to the sourced -10% depreciation-notification threshold,
            # and this sentence was left behind saying "-20%" and "lowest
            # rung". Both were then FALSE for any book between -10% and
            # -20%, where the ladder is on its 1.5x or 1.0x rung and may
            # not have cut at all. Nothing here is a literal any more: the
            # drawdown, the alert level and the rung are all READ from the
            # resolved ceiling, so moving either table cannot desynchronise
            # the words from the book again.
            measured = leverage.get("drawdown_pct")
            alert_at = leverage.get("alert_pct")
            head = "🛑 DRAWDOWN"
            if isinstance(measured, (int, float)):
                head += f" {abs(float(measured)):.1f}%"
            if isinstance(alert_at, (int, float)):
                head += f", past the {abs(float(alert_at)):.0f}% level at which you are told"
            if rung == "none":
                lines.append(
                    f"{head}: the de-levering ladder has NOT cut anything yet "
                    f"— gross exposure is still at the standing "
                    f"{ceiling_x:.2f}x cap."
                )
            elif rung in (None, ""):
                # No rung reported. Say only what is known — the cap — and
                # claim nothing about whether the ladder cut, because with
                # no rung neither claim can be checked.
                lines.append(
                    f"{head}: gross exposure is capped at {ceiling_x:.2f}x "
                    f"equity and new positions are refused once the book "
                    f"reaches it."
                )
            else:
                lines.append(
                    f"{head}: the de-levering ladder has the book on its "
                    f"{rung} rung. Gross exposure is capped at "
                    f"{ceiling_x:.2f}x equity and new positions are refused "
                    f"once the book reaches it."
                )
    if leverage.get("delever_incomplete"):
        # §11.2 reporting gap: a de-lever was attempted but the account is
        # still over its limit afterward. Plain words for a non-developer
        # owner — what was tried, that it fell short, and the real number,
        # never an internal name or an invented figure.
        lines.append(
            f"⚠️ Tried to bring the account's exposure back under its limit "
            f"by selling down positions, but it is still over: the account "
            f"currently has {gross_x:.2f}x of equity invested against a "
            f"{ceiling_x:.2f}x limit."
        )


_MAX_LOOKED_UP_COMPANIES = 12


def _lookup_company_profiles(symbols: list, limit: int | None = None) -> dict[str, Any]:
    """symbol -> CompanyProfile for every symbol the cache already knows.

    The ONE place that calls `CompanyProfileStore` for a trader-facing
    alert — `_append_company_identities` below (the base formatter's own
    "who:" block) and `company_name()` (src/trader_feed.py's inline
    "TICKER (Company)" annotations) both build on this instead of each
    keeping its own dedupe/cap/fetch logic; do not add a second lookup,
    call this with a symbol list instead.

    `allow_fetch=False` is not an optimisation, it is the contract: an
    operator alert must never sit waiting on a network call. By the time an
    alert goes out the PM path has already warmed the cache for exactly
    these symbols, so this is a dictionary lookup. Symbols the cache does
    not know come back absent rather than blocking or inventing a name.
    """
    seen = _dedupe_symbols(symbols)
    if not seen:
        return {}
    # `limit` — board item 89 clarity defect "bare ticker symbols with no
    # company name after the twelfth name in a list": the cap below sized
    # the base formatter's "who:" block, but src/trader_feed.py annotates
    # names INLINE, where a bare ticker after the twelfth line is exactly
    # the defect. The lookup is a cache read (allow_fetch=False), so a
    # larger cap costs nothing on the wire; the trader feed passes its own.
    cap = _MAX_LOOKED_UP_COMPANIES if limit is None else max(1, int(limit))
    try:
        from src.data.company import CompanyProfileStore

        return CompanyProfileStore().get_many(seen[:cap], allow_fetch=False)
    except Exception as e:  # noqa: BLE001 — never lose an alert over prose
        logger.warning("notifier: company profiles unavailable: %s", e)
        return {}


def company_name(symbol: str, profiles: dict[str, Any] | None = None) -> str | None:
    """The one company name for `symbol`, or None if the cache doesn't have
    it. Pass a pre-fetched `profiles` dict (from `_lookup_company_profiles`,
    fetched once for every symbol a message is about to render) when
    annotating several symbols in one message, so each render is one cache
    read, not N — see src/trader_feed.py's inline "TICKER (Company)" use.
    """
    sym = str(symbol or "").strip().upper()
    if not sym:
        return None
    if profiles is None:
        profiles = _lookup_company_profiles([sym])
    profile = profiles.get(sym)
    return getattr(profile, "name", None) if profile is not None else None


def _append_company_identities(lines: list[str], symbols: list) -> None:
    """One line per relevant symbol: who the company is.

    The operator reads `BUY CCJ qty=40 @$58.10` and has to already know that
    CCJ is Cameco. Name and industry only — deliberately NOT the business
    summary that goes to the PM. A Telegram message is 4096 characters and
    competes for a phone screen; ten paragraphs of company description would
    push the order list itself out of view.

    `symbols` is a plain, already-resolved ticker list — deduplication and
    upper-casing happen in `_lookup_company_profiles`, so every caller (the
    base formatter's own order list, and src/trader_feed.py's richer
    per-mode formatters, via `extract_alert_symbols`) can hand this a raw,
    unfiltered sequence. Deliberately the ONLY place that turns a symbol
    list into "who:"-style identity text — do not duplicate this lookup
    elsewhere; give it a symbol list instead. (src/trader_feed.py no longer
    calls this — see its own inline "TICKER (Company)" annotations, which
    share the same `_lookup_company_profiles` lookup via `company_name()`.)
    """
    seen = _dedupe_symbols(symbols)
    if not seen:
        return
    profiles = _lookup_company_profiles(seen)
    if not profiles:
        return
    identities = []
    for symbol in seen[:_MAX_LOOKED_UP_COMPANIES]:
        profile = profiles.get(symbol)
        if profile is None:
            continue
        bits = [
            b
            for b in (
                getattr(profile, "name", None),
                getattr(profile, "industry", None),
            )
            if b
        ]
        if not bits:
            continue
        identities.append(f"  {symbol} — {' · '.join(bits)}")
    if identities:
        lines.append("who:")
        lines.extend(identities)


def _held_revision_rows(result: dict | None, held_symbols: Iterable[str]) -> list[dict]:
    """The session's revision rows for positions the desk holds as the message is built."""
    held = {str(s).strip().upper() for s in held_symbols if s}
    rows = [r for r in ((result or {}).get("target_revisions") or []) if isinstance(r, dict)]
    return [r for r in rows if str(r.get("symbol") or "").strip().upper() in held]


def _close_label(bar_date: str | None) -> str:
    """The daily close a revision read came from, as 'Mon 13 Oct', taken from
    the row's own `bar_date`. Without one the close is 'the latest' — never
    'today's', which midday does not exist yet."""
    from datetime import date

    try:
        day = date.fromisoformat(str(bar_date)[:10])
    except (TypeError, ValueError):
        return "latest"
    return f"{day.strftime('%a')} {day.day} {day.strftime('%b')}"


def describe_target_revisions(result: dict | None, *, held_symbols: Iterable[str] | None) -> list[str]:
    """The session's take-profit revisions, in the owner's words.

    Only positions the desk holds as the message is built are voiced: every
    caller passes the book it read (`held_symbols`). A position sold earlier
    in the session no longer has a target the owner is being quoted. A
    caller with no book passes None and gets one line saying the block is
    not shown, never lines about stocks it cannot say are held.

    WHY THIS EXISTS (item 194). The desk's standing rule is that every buy,
    sell or hold states its real reason, and a bare number is a defect. A
    target revision changes the number the desk quotes the owner for a
    position he already holds, and until now it reached the dashboard
    (`src/api/holding_why.py`) and Telegram nowhere at all — so for the
    owner it did not happen.

    Each line says WHAT CHANGED and WHY, never just the new number: the
    structural event that legitimised asking (a ceiling broken, a ceiling
    grown, today's volatility putting the old number out of reach), the
    direction of travel, and the chart basis the new number was measured
    from. Refusals and faults are summarised as a count rather than listed:
    a refusal is the normal outcome on most held names every session, and
    the owner's attention belongs on the ones that moved.

    Nothing here changes what is sent to the broker. The revised target
    places no order. Two mechanisms close a position on a thesis rather
    than on a price: the alignment exit, and — since the owner's ruling of
    2026-10-01 — the rotation's categorical tier, which sells a holding
    that no longer clears the desk's own entry bar. Neither is reachable
    from here.
    """
    if held_symbols is None:
        every = [r.get("symbol") for r in ((result or {}).get("target_revisions") or []) if isinstance(r, dict)]
        if not describe_target_revisions(result, held_symbols=every):
            return []
        return ["🎯 Target changes: not shown — this message has no read of the book, so cannot say what is held"]
    rows = _held_revision_rows(result, held_symbols)
    if not rows:
        return []
    applied = [r for r in rows if r.get("applied")]
    pending = [
        r for r in rows if not r.get("applied") and str(r.get("code") or "").upper().endswith("_PENDING_CONFIRMATION")
    ]
    if not applied and not pending:
        return []
    lines: list[str] = []
    if not applied:
        lines.append("🎯 Target: no revision applied this session")
    if applied:
        lines.append(f"🎯 Target revised: {len(applied)} position(s)")
    for row in applied:
        symbol = str(row.get("symbol") or "?").upper()
        prior = row.get("prior_price")
        new = row.get("new_price")
        move = ""
        try:
            if prior and new:
                direction = "up" if float(new) > float(prior) else "down"
                move = f"${float(prior):,.2f} → ${float(new):,.2f} ({direction})"
        except (TypeError, ValueError):
            move = ""
        why = _target_revision_reason(str(row.get("trigger") or ""))
        basis = str(row.get("basis") or "").strip()
        text = f"   • {symbol}"
        if move:
            text += f" {move}"
        if why:
            text += f" — {why}"
        if basis:
            text += f"; measured from the chart as {basis}"
        lines.append(_clip_text(text, 420))
    if pending:
        # A PENDING CONFIRMATION IS A HOLD ON A STALE NUMBER, caused by
        # this desk's own brake, so it is named rather than counted. The
        # owner is still being quoted a target the desk has itself stopped
        # believing, and a bare count cannot tell him WHICH position that
        # is. These are listed even on a session where nothing was
        # applied, which is now the common case.
        lines.append(
            f"   ⏸️ {len(pending)} position(s) on a target the desk has "
            f"stopped believing, waiting one more close to confirm:"
        )
        for row in pending:
            symbol = str(row.get("symbol") or "?").upper()
            prior = row.get("prior_price")
            quoted = ""
            try:
                if prior:
                    quoted = f" (still quoted ${float(prior):,.2f})"
            except (TypeError, ValueError):
                quoted = ""
            why = _pending_confirmation_reason(str(row.get("code") or ""), _close_label(row.get("bar_date")))
            lines.append(_clip_text(f"      • {symbol}{quoted} — {why}", 420))
    held = len(rows) - len(applied) - len(pending)
    if held:
        lines.append(f"   ({held} other position(s) measured, target unchanged)")
    lines.append(
        "   The target is a quoted number, not a sell order — the desk still exits only when the trend itself is over."
    )
    return lines


def _pending_confirmation_reason(code: str, close: str = "latest") -> str:
    """Plain words for why a revision the desk wanted to make is being held
    one more session. Unknown codes return the code rather than silence.

    `close` names the close the reading came from (a date such as 'Mon 13
    Oct'), because at midday today's close has not happened yet and 'today's
    close' would describe something that does not exist."""
    c = (code or "").strip().upper()
    if c == "REFUSAL_WALL_PENDING_CONFIRMATION":
        return (
            f"a new ceiling appeared between the buy price and the target on "
            f"the {close} close; the desk waits for a second day's close to "
            f"agree before moving the number"
        )
    if c == "REFUSAL_REACH_PENDING_CONFIRMATION":
        return (
            f"the daily range as of the {close} close puts the target out of "
            f"reach for the holding period; the desk waits for a second day's "
            f"close to agree before moving the number"
        )
    if c == "REFUSAL_BREAK_PENDING_CONFIRMATION":
        return (
            f"the ceiling this target was measured against was closed through "
            f"on the {close} close; the desk waits for a second day's close "
            f"to agree before moving the number"
        )
    return c


def _target_revision_reason(trigger: str) -> str:
    """Plain words for why a target was re-measured. Unknown codes return
    the code itself rather than silence — an unexplained revision must read
    as unexplained, not as if there were no reason."""
    code = (trigger or "").strip().upper()
    if not code:
        return ""
    if code == "TARGET_LEVEL_BROKEN_CONFIRMED":
        return (
            "the ceiling this target was measured against has been closed "
            "through on two consecutive days, so it is not a ceiling any more"
        )
    if code == "STRUCTURAL_WALL_STANDING_IN_FRONT_OF_TARGET":
        return (
            "a new ceiling has built up between the buy price and the old "
            "target, confirmed on two consecutive days — the old number had "
            "a wall in front of it"
        )
    if code == "TARGET_BEYOND_TODAYS_REACH":
        return (
            "this stock's daily range has shrunk enough that the old target "
            "is no longer reachable inside the holding period, on two "
            "consecutive days' readings"
        )
    if code == "TARGET_DERIVATION_BUG_CORRECTED":
        return "the desk corrected a fault in how the original number was worked out"
    return f"trigger {code}"
