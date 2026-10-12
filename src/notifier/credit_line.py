"""The "AI credit" line every Telegram message carries.

Owner, 2026-10-12: "I want to see AI credit with every Telegram alert." The
one balance fetch the $3 alert already makes (owner ruling 2026-10-09) feeds
the line. `deliver_with_outcome` applies it once per message, before any
retry; this module sits below both it and `send_funnel` so neither imports
the other for it.
"""

from __future__ import annotations

import html

from src.notifier.base import _clip_text
from src.notifier.markup import _escape_with_markup


class _CreditFetch:
    """Wraps the one balance fetch so its result can also feed the credit line."""

    def __init__(self) -> None:
        self.called = False
        self.remaining: float | None = None

    def fetch(self) -> dict:
        from src.openrouter_balance import fetch_openrouter_balance

        self.called = True
        result = fetch_openrouter_balance()
        self.remaining = float(result["remaining_usd"])
        return result

    def line(self) -> str:
        if self.remaining is None:
            return "AI credit: could not read"
        return f"AI credit: ${self.remaining:.2f} left"


def _with_credit_line(self, text, line, link_url, link_label, preserve_structural_markup) -> str:
    """Return `text` plus the credit line, trimmed so the line is never cut off.

    The line has no HTML-special characters, so adding it before the escape in
    `send_once` is the same as adding it after. The body is trimmed here, by the
    same rule `_build_payload` uses, so the line and the link always fit.
    """
    if not text:
        return text
    suffix = "\n\n" + line
    url = link_url if link_url is not None else getattr(self, "mission_control_url", None)
    link_len = 0
    if url:
        label = link_label if link_label is not None else getattr(self, "DEFAULT_LINK_LABEL", "")
        link_len = len(f'\n\n<a href="{html.escape(url, quote=True)}">{html.escape(label)}</a>')
    limit = getattr(self, "MAX_MESSAGE_CHARS", 4000) - link_len - len(suffix)
    escape = _escape_with_markup if preserve_structural_markup else html.escape
    body = text
    while len(escape(body)) > limit and len(body) > 1:
        # Escaping only grows text, so shrinking the raw body converges.
        target = int(len(body) * limit / len(escape(body)))
        body = _clip_text(body, min(target, len(body) - 1), marker="\n[...truncated]")
    return body + suffix


#: Module switch, not config: only tests turn it off (see tests/conftest.py).
APPEND_CREDIT_LINE = True


def with_credit(self, text, link_url=None, link_label=None, preserve_structural_markup=False, **_ignored) -> str:
    """Run the $3 check and return `text` with the "AI credit" line.

    Called once per message by `deliver_with_outcome`, the one function both
    ordinary sends and owner alerts pass through, before any retry.
    Owner ruling 2026-10-09: $3 credit alert, checked on every send; it goes
    out through this same funnel (re-entry is skipped inside the check).
    Owner ruling 2026-10-12: the same single fetch also gives every message an
    "AI credit" line.
    """
    from src.llm_balance_runway import check_balance_on_send

    credit = _CreditFetch()
    check_balance_on_send(fetch=credit.fetch, send=lambda message: self.send(message, kind="llm_credit_alert"))
    if APPEND_CREDIT_LINE and credit.called:
        return _with_credit_line(self, text, credit.line(), link_url, link_label, preserve_structural_markup)
    return text
