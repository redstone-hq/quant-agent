"""Every Telegram send goes through the retry funnel, because `send` IS it.

The owner-alert discipline (retry a failure, then write a counted
`owner_alert_undelivered` row) used to live only in `send_owner_alert`, so any
code that built its own `TelegramNotifier` and called `send` escaped it
entirely -- a dropped session summary was indistinguishable from a delivered
one. That was opt-in discipline, and opt-in discipline is forgotten.

So the single attempt is `send_once` and the public `send` is the funnel.
There is no longer a way to reach the wire without it: a caller cannot bypass
a method by calling the same method. Both are bound onto `TelegramNotifier`
in `transport.py`; they live here because that file is at its size ceiling.
"""

from __future__ import annotations

import html
import os
from typing import Any

from src.notifier.base import (
    _DB_PATH,
    _REHEARSAL_MODE,
    _clip_text,
    _redact_malformed_numbers,
    logger,
)
from src.notifier.sections import (
    _redact_raw_exception_text,
)
from src.notifier.category import (
    SUPPRESSED,
    filtered_by_category,
    resolve_risk_only,
)
from src.notifier.markup import (
    _close_open_markup,
    _dedupe_symbols,
    _escape_with_markup,
    _linkify_symbols,
)
from src.notifier.owner_alert_delivery import deliver_with_outcome


def send(
    self,
    text: str,
    link_url: str | None = None,
    link_label: str | None = None,
    symbols: list[str] | None = None,
    preserve_structural_markup: bool = False,
    kind: str = "generic",
    run_id: str | None = None,
    category: str | None = None,
) -> bool:
    """Send with the full delivery discipline: retry, then record a failure.

    Returns exactly what one attempt used to: True delivered, `SUPPRESSED` for
    a deliberate mute/category drop (settled, never retried), False otherwise
    -- and a False here means every attempt failed AND a durable undelivered
    row was written. Never raises.
    """
    delivered, suppressed = deliver_with_outcome(
        self,
        text,
        link_url=link_url,
        link_label=link_label,
        symbols=symbols,
        preserve_structural_markup=preserve_structural_markup,
        kind=kind,
        run_id=run_id,
        category=category,
    )
    if suppressed:
        return SUPPRESSED
    return delivered


#: Read by `deliver_with_outcome` to tell its own wrapper from a plain
#: duck-typed `send`, and compared with `is True` because a MagicMock
#: answers every `getattr` with a truthy mock.
send._is_delivery_funnel = True


def send_once(
    self,
    text: str,
    link_url: str | None = None,
    link_label: str | None = None,
    symbols: list[str] | None = None,
    preserve_structural_markup: bool = False,
    kind: str = "generic",
    run_id: str | None = None,
    category: str | None = None,
) -> bool:
    """ONE delivery attempt. Returns True on success.

    Not the public entry point: `TelegramNotifier.send` is, and it runs
    this through the retry-and-record funnel. Called directly only by
    `deliver_with_outcome`, which owns the attempt loop.

    `kind` and `run_id` (2026-09-18) label the durable record this
    call leaves behind (see `_record_send`) — `kind` is a short,
    caller-chosen tag ("morning", "owner_alert", ...; default
    "generic" for the many existing callers that have no reason to
    care), `run_id` ties it back to `agent_logs`/`session_reports`
    when a run produced this message. Neither changes delivery.

    - No-op when not enabled (returns False).
    - Escapes `text` and sends with `parse_mode="HTML"` — a stray
      underscore/asterisk/`<`/`&` in a ticker or a rationale must not
      corrupt the message or get the whole send rejected by Telegram.
    - Appends a tap-through `<a href>` link when one is available:
      `link_url` if given, else `self.mission_control_url` (from
      config). Neither set → no link, ever — never a broken one.
    - `symbols`, when given, wraps each matching ticker mentioned in
      `text` with its own tap-through link (see `_linkify_symbols`) —
      best-effort: if adding links would push the message over budget,
      it degrades to plain text rather than risk truncating markup.
    - Auto-truncates messages over MAX_MESSAGE_CHARS on a sentence/word
      boundary (see `_clip_text`) rather than mid-word.
    - Any HTTP / network / Telegram-side error is logged and
      swallowed: trading must never fail because a notifier is
      unreachable.
    - `preserve_structural_markup=False` (default): `text` is fully
      escaped — the historical, safe contract every existing caller
      relies on (cost-circuit alerts, `send_owner_alert`, scripts/*,
      none of which ever intend a literal '<' as a tag). Pass True
      ONLY for text built by src/trader_feed.py's formatters, which
      embed a fixed, small set of literal structural tags (`<b>`,
      `<blockquote expandable>`) on purpose — see
      `_escape_with_markup`'s docstring. This is opt-in, per call, not
      a global default, so a coincidental literal "<b>" in some other
      alert's free text is still rendered as visible text, not markup.
    """
    if not self.enabled:
        # Item 211 defect 3. The global mute used to drop the message
        # here, BEFORE anything was written down, so nothing that would
        # have fired since the desk was muted exists anywhere — neither
        # the owner nor the desk can see what he has been missing. This
        # un-mutes nothing and sends nothing; it only means a muted
        # message leaves a durable trace (type, symbols, timestamp) in
        # `notifier_sends`, the same table a delivered one lands in.
        if text and getattr(self, "muted", False):
            self._safe_record_send(
                kind=kind,
                status="muted",
                run_id=run_id,
                text=text,
                detail=(
                    "suppressed by TELEGRAM_DISABLED; symbols: " + ", ".join(_dedupe_symbols(symbols or []))
                    if symbols
                    else "suppressed by TELEGRAM_DISABLED; not sent"
                ),
            )
            # Deliberate and settled, not a failure: see `SuppressedSend`.
            return SUPPRESSED
        return False
    if not text:
        return False
    if filtered_by_category(
        self,
        kind=kind,
        category=category,
        text=text,
        run_id=run_id,
        symbols=symbols,
    ):
        return SUPPRESSED
    # Single chokepoint for every owner-facing message this notifier
    # sends, regardless of which of the ~20 upstream formatters (PM
    # rationale, risk-manager reasoning, evening outlook, key thesis,
    # ...) produced the free-text prose it came from — see
    # `_redact_malformed_numbers`'s docstring for why here rather than
    # at each of those call sites, and for the deliberate distinction
    # from the REJECTED prompt-text scanner (board item 99).
    text = _redact_malformed_numbers(text)
    # Board item 89 defect 5: a genuine raw exception/internal-error
    # string (e.g. a `reason=f"... raised: {exc}"` built upstream) must
    # never reach the owner verbatim, even though internal STATUS CODES
    # are already plain English. Same chokepoint as the guard above —
    # see `_redact_raw_exception_text`'s docstring.
    text = _redact_raw_exception_text(text)
    if _REHEARSAL_MODE:
        # A rehearsal replays a real session, so it raises real alerts —
        # "PAID ANALYSIS SUSPENDED", "STOP COVERAGE REPAIRED", trade
        # notifications. Delivered unmarked to the operator's normal chat
        # they are indistinguishable from production, which is worse than
        # useless: it teaches him to distrust the channel that exists to
        # tell him something is wrong.
        #
        # Refusing outright is the wrong answer too — what a rehearsal
        # WOULD have sent is evidence, and the harness captures it for the
        # report. So this suppresses delivery and says so, rather than
        # silently dropping.
        logger.info(
            "REHEARSAL: suppressed operator alert (%d chars): %s",
            len(text),
            text.splitlines()[0][:120] if text else "",
        )
        self._safe_record_send(kind=kind, status="suppressed", text=text, run_id=run_id)
        # Settled, not failed: SUPPRESSED stops the funnel retrying a drop
        # the desk made on purpose and recording it as an undelivered alert.
        return SUPPRESSED

    payload = self._build_payload(
        text,
        link_url,
        link_label,
        symbols,
        preserve_structural_markup=preserve_structural_markup,
    )
    # 2026-09-24: `notifier_sends` used to record the ORIGINAL `text`
    # here on both branches below, but `_build_payload` may have
    # truncated it (`MAX_MESSAGE_CHARS`, see `_clip_text` above) before
    # it went on the wire. The stored record then differed from what
    # Telegram actually delivered, with no marker that a truncation
    # happened at all. `payload["text"]` is the exact string sent, so
    # recording it (rather than `text`) makes the record match delivery.
    delivered_text = payload["text"]

    try:
        self._post_payload(payload)
        self._safe_record_send(
            kind=kind,
            status="sent",
            text=delivered_text,
            run_id=run_id,
        )
        return True
    except Exception as exc:
        # Catch broadly on purpose — TelegramNotifier is a
        # best-effort side channel. A 429 rate-limit, a 5xx, a
        # connection reset, a DNS failure, a bad token — none of
        # those should bubble up and crash the trading session.
        logger.warning("Telegram notify failed: %s", self._redact(exc))
        self._safe_record_send(
            kind=kind,
            status="failed",
            text=delivered_text,
            detail=self._redact(exc),
            run_id=run_id,
        )
        return False
