"""The "AI credit" line every Telegram message carries.

Owner, 2026-10-12: "I want to see AI credit with every Telegram alert." The
one balance fetch the $3 alert already makes (owner ruling 2026-10-09) feeds
the line. `deliver_with_outcome` applies it once per message, before any
retry; this module sits below both it and `send_funnel` so neither imports
the other for it.

This module only APPENDS the line. Fitting the message to Telegram's limit is
the transport's job (`TelegramNotifier._build_payload`), which splits the line
back off with `split_credit_line` and keeps it as a protected block. 13 Oct
2026: a trim here cut the body by character count inside an open
`<blockquote expandable>`, the result was under budget so the transport never
re-fitted it, and Telegram refused the morning report three times.
"""

from __future__ import annotations

#: The line is `CREDIT_PREFIX` + amount, always the last paragraph of the text.
CREDIT_PREFIX = "AI credit: "


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
            return CREDIT_PREFIX + "could not read"
        return f"{CREDIT_PREFIX}${self.remaining:.2f} left"


def split_credit_line(text: str) -> tuple[str, str]:
    """Return (body, credit line) for a text `with_credit` built, else (text, "").

    The line is the last paragraph and starts with `CREDIT_PREFIX`; it has no
    HTML-special characters, so escaping it is the same before or after.
    """
    body, sep, last = text.rpartition("\n\n")
    if sep and last.startswith(CREDIT_PREFIX) and "\n" not in last:
        return body, last
    return text, ""


#: Module switch, not config: only tests turn it off (see tests/conftest.py).
APPEND_CREDIT_LINE = True


def with_credit(self, text, **_ignored) -> str:
    """Run the $3 check and return `text` with the "AI credit" line appended.

    Called once per message by `deliver_with_outcome`, the one function both
    ordinary sends and owner alerts pass through, before any retry.
    Owner ruling 2026-10-09: $3 credit alert, checked on every send; it goes
    out through this same funnel (re-entry is skipped inside the check).
    Owner ruling 2026-10-12: the same single fetch also gives every message an
    "AI credit" line. Nothing is cut here: the transport fits the whole.
    """
    from src.llm_balance_runway import check_balance_on_send

    credit = _CreditFetch()
    check_balance_on_send(fetch=credit.fetch, send=lambda message: self.send(message, kind="llm_credit_alert"))
    if APPEND_CREDIT_LINE and credit.called and text:
        return f"{text}\n\n{credit.line()}"
    return text
