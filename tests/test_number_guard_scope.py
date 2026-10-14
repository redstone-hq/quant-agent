"""The garbled-number guard belongs where MODEL prose enters an owner
message, not at the send chokepoint.

Measured on trunk 64a64d7b: the guard ran on every outgoing message, so
desk-formatted text such as "stop covers 3, $164.61 unprotected" reached the
owner as "stop covers [number garbled — removed] $164.61 unprotected" — the
guard's token pattern let a number end in a comma ("3,"), and the guard then
judged that token a malformed thousands separator.

These tests pin both halves of the fix:
  * text the desk formatted itself arrives at Telegram intact;
  * a garbled figure inside a model-written field is still removed.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from src.notifier import _MALFORMED_NUMBER_MARKER, TelegramNotifier, _redact_malformed_numbers


def _send_and_capture(monkeypatch, text: str) -> str:
    """Send `text` through the real `TelegramNotifier.send` with a fake HTTP
    transport and return exactly what would have reached Telegram."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "BOT_TOK")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "CHAT_ID")
    monkeypatch.delenv("TELEGRAM_DISABLED", raising=False)
    notifier = TelegramNotifier()
    with patch("src.notifier.requests.post") as mock_post:
        response = MagicMock()
        response.raise_for_status.return_value = None
        mock_post.return_value = response
        ok = notifier.send(text)
    assert ok is True
    return mock_post.call_args.kwargs["json"]["text"]


@pytest.mark.parametrize(
    "desk_text",
    [
        "stop covers 3, $164.61 unprotected",
        "held 3,\n",
        "shares held: 3,",
    ],
)
def test_desk_formatted_text_reaches_telegram_intact(monkeypatch, desk_text):
    sent = _send_and_capture(monkeypatch, desk_text)
    assert "number garbled" not in sent
    assert "3," in sent


def test_send_chokepoint_no_longer_rewrites_numbers(monkeypatch):
    """Even a shape the guard WOULD flag passes the chokepoint untouched:
    the chokepoint cannot tell desk text from model text, so it must not
    judge numbers at all."""
    sent = _send_and_capture(monkeypatch, "desk ledger row 1,23,4")
    assert "1,23,4" in sent
    assert "number garbled" not in sent


@pytest.mark.parametrize("text", ["stop covers 3, $164.61 unprotected", "held 3,\n", "3,"])
def test_a_number_cannot_end_in_a_comma(text):
    assert _redact_malformed_numbers(text) == text


def test_garbled_number_in_model_field_is_still_removed():
    from src.trader_feed.common import _clip

    clipped = _clip("PM view: add on weakness, roughly 1,23,4 shares over the week", 300)
    assert "1,23,4" not in clipped
    assert _MALFORMED_NUMBER_MARKER in clipped
    assert "add on weakness" in clipped


def test_clean_model_field_passes_unchanged():
    from src.trader_feed.common import _clip

    text = "PM view: closed CRM for $1,021.36, up 14.6%; stop covers 3, $164.61 left"
    assert _clip(text, 300) == text
