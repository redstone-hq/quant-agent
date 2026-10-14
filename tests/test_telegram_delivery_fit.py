"""13 Oct 2026: the morning report never arrived — Telegram refused it (HTTP 400)
three times and nobody was told.

Replayed from the stored rows: the credit-line trim cut the body by character
count inside `<blockquote expandable>`, the final text was under the 4000
budget so the transport's tag-closing branch never ran, `raise_for_status()`
threw Telegram's reason away, and the identical refused body was resent twice.

The texts here are synthetic, of the same paragraph shape as the stored
report; no live message content is committed.
"""

from __future__ import annotations

import html

import pytest
import requests

import src.notifier.owner_alert_delivery as delivery
import src.openrouter_balance as ob
from src.notifier.markup import _escape_with_markup
from src.notifier.transport import TelegramNotifier
from src.trader_feed.common import _DETAILS_SAFETY_RESERVE_CHARS

LIMIT = TelegramNotifier.MAX_MESSAGE_CHARS
LINE = "AI credit: $12.34 left"
TOKEN = "123456:ABCDEFtoken"
URL = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
#: Prose whose escape grows it (3 apostrophes + 1 ampersand per sentence), the
#: way PM/risk prose does; the formatter budgets the RAW length, the wire
#: carries the ESCAPED one.
PROSE = "It's a buyer's market & the desk's view isn't changed. "
REFUSAL = "Bad Request: can't parse entities: Can't find end tag corresponding to start tag \"blockquote\""


def _morning_shaped() -> str:
    """Same shape as the stored 13 Oct report: headline, plain sections, two
    <b> sections, one long prose paragraph, then the DETAILS block — sized to
    the formatter's own raw budget, so only the escape growth is over."""
    head = [
        "⚪ MORNING · 2026-10-13 9:35 AM ET · FAILED",
        "📈 Today's P&L: +$1.00 (+0.1%)\nWeek: +$2.00",
        "⚠️ STOP MIS-SIZED: " + PROSE * 8,
        "📊 Market: neutral",
        "<b>❌ FAILED</b>\n   • BUY AAA — " + PROSE * 7,
        "<b>👀 LOOKED AT, NO TRADE</b>\n" + "\n".join(f"   • SYM{i} — {PROSE}" for i in range(8)),
        "🔄 Rotation check: " + PROSE * 18,
        "<b>DETAILS</b>\n<blockquote expandable></blockquote>",
    ]
    room = LIMIT - _DETAILS_SAFETY_RESERVE_CHARS - len("\n\n".join(head))
    head[-1] = "<b>DETAILS</b>\n<blockquote expandable>" + (PROSE * 100)[:room] + "</blockquote>"
    text = "\n\n".join(head)
    assert len(text) <= LIMIT - _DETAILS_SAFETY_RESERVE_CHARS, "precondition: fits the formatter's raw budget"
    assert len(_escape_with_markup(text)) > LIMIT, "precondition: escaped, it does not fit the wire"
    return text


class _Resp:
    def __init__(self, status: int, body: dict):
        self.status_code = status
        self._body = body

    def json(self) -> dict:
        return self._body

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code} Client Error for url: {URL}", response=self)


@pytest.fixture
def rig(monkeypatch, tmp_path):
    """A real notifier whose `requests.post` is scripted; records and sleeps captured."""
    import src.notifier.base as base
    import src.notifier.transport as transport

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(base, "_DB_PATH", tmp_path / "n.db")
    monkeypatch.setattr("src.notifier.credit_line.APPEND_CREDIT_LINE", True)
    monkeypatch.delenv("TELEGRAM_DISABLED", raising=False)
    monkeypatch.setattr(ob, "fetch_openrouter_balance", lambda: {"remaining_usd": 12.34})
    sleeps: list[float] = []
    monkeypatch.setattr(delivery.time, "sleep", lambda s: sleeps.append(s))
    posts: list[dict] = []
    rows: list[dict] = []
    responses: list = []

    def fake_post(url, json=None, timeout=None):
        posts.append(json)
        resp = responses.pop(0) if responses else _Resp(200, {"ok": True, "result": {"message_id": 1}})
        if isinstance(resp, Exception):
            raise resp
        return resp

    monkeypatch.setattr(transport.requests, "post", fake_post)
    n = TelegramNotifier(token=TOKEN, chat_id="chat-1", mission_control_url="https://mc.test/cockpit/")
    monkeypatch.setattr(n, "_safe_record_send", lambda **k: rows.append(k))
    return n, posts, rows, responses, sleeps


def _balanced(sent: str) -> bool:
    return (
        sent.count("<blockquote expandable>") == sent.count("</blockquote>")
        and sent.count("<b>") == sent.count("</b>")
        and sent.count("<a ") == sent.count("</a>")
        and sent.rfind("<") < sent.rfind(">")
    )


def test_morning_shaped_report_goes_out_balanced_with_credit_line_and_link(rig):
    n, posts, rows, _, _ = rig
    assert n.send(_morning_shaped(), preserve_structural_markup=True, kind="morning") is True
    sent = posts[0]["text"]
    assert len(sent) <= LIMIT
    assert _balanced(sent), "a tag was left open — exactly what Telegram refused on 13 Oct"
    assert sent.startswith("⚪ MORNING")
    assert LINE + '\n\n<a href="https://mc.test/cockpit/">' in sent
    assert "[...truncated]" in sent
    assert [r["status"] for r in rows] == ["sent"]


def test_fitter_drops_whole_blocks_details_first_then_trailing(rig):
    n, _, _, _, _ = rig
    details = "<b>DETAILS</b>\n<blockquote expandable>" + "d " * 1200 + "</blockquote>"
    section = "<b>A</b>\n" + "a " * 900
    tail = "tail " * 100
    text = "\n\n".join(["head", section, details, tail])
    assert len(text) > LIMIT
    sent = n._build_payload(text, link_url="", preserve_structural_markup=True)["text"]
    assert len(sent) <= LIMIT and _balanced(sent)
    assert "<blockquote" not in sent and section in sent and tail.strip() in sent
    assert sent.endswith("[...truncated]")

    section = "<b>A</b>\n" + "a " * 1500
    tail = "b " * 800
    sent = n._build_payload("\n\n".join(["head", section, tail]), link_url="", preserve_structural_markup=True)["text"]
    assert len(sent) <= LIMIT and _balanced(sent)
    assert section in sent and "b b" not in sent


def test_fitter_falls_back_to_whole_lines_then_to_tag_free_text(rig):
    n, _, _, _, _ = rig
    lines = [f"<b>L{i}</b> " + "w" * 90 for i in range(60)]
    sent = n._build_payload("\n".join(lines), link_url="", preserve_structural_markup=True)["text"]
    assert len(sent) <= LIMIT and _balanced(sent)
    kept = sent.split("\n")[:-1]
    assert kept and all(line in lines for line in kept), "a line was cut inside, not dropped whole"

    one_line = "<b>" + "w " * 3000 + "</b>"
    sent = n._build_payload(one_line, link_url="", preserve_structural_markup=True)["text"]
    assert len(sent) <= LIMIT and "<" not in sent and sent.endswith("[...truncated]")


def test_credit_line_is_appended_whole_never_cut():
    from src.notifier.credit_line import split_credit_line

    body = "word & <b> " * 1000
    assert split_credit_line(body + "\n\n" + LINE) == (body, LINE)
    assert split_credit_line(body) == (body, "")


def test_refused_body_is_not_resent_and_the_owner_is_told_why(rig):
    n, posts, rows, responses, sleeps = rig
    responses.append(_Resp(400, {"ok": False, "error_code": 400, "description": REFUSAL}))
    assert n.send("hello", kind="morning") is False
    assert sleeps == []
    assert len(posts) == 2, "one refused attempt plus the one-line notice; the refused body was not resent"
    failed = [r for r in rows if r["status"] == "failed"]
    assert len(failed) == 1 and "400" in failed[0]["detail"] and REFUSAL in failed[0]["detail"]
    assert TOKEN not in failed[0]["detail"]
    undelivered = [r for r in rows if r["status"] == delivery.UNDELIVERED_STATUS]
    assert len(undelivered) == 1 and "after 1 attempt" in undelivered[0]["detail"]
    assert REFUSAL in undelivered[0]["detail"]
    notice = html.unescape(posts[1]["text"])
    assert "\n" not in notice and "<" not in posts[1]["text"]
    assert "morning" in notice and REFUSAL in notice
    assert [r["kind"] for r in rows if r["status"] == "sent"] == [delivery.FAILURE_NOTICE_KIND]


def test_refusal_notice_never_exceeds_the_attempt_budget(rig):
    n, posts, rows, responses, sleeps = rig
    responses.append(_Resp(400, {"ok": False, "error_code": 400, "description": REFUSAL}))
    assert delivery.deliver_with_outcome(n, "hello", max_attempts=1, kind="cost") == (False, False)
    assert len(posts) == 1, "a one-attempt budget spent on the refusal leaves no room for a notice"


def test_rate_limit_waits_telegrams_retry_after_then_lands(rig):
    n, posts, _, responses, sleeps = rig
    limited = {"ok": False, "error_code": 429, "description": "Too Many Requests", "parameters": {"retry_after": 7}}
    responses.append(_Resp(429, limited))
    assert n.send("hello") is True
    assert len(posts) == 2 and sleeps == [7.0]


def test_server_and_network_failures_are_retried_then_reported(rig):
    n, posts, rows, responses, sleeps = rig
    gateway = {"ok": False, "error_code": 502, "description": "Bad Gateway"}
    responses.extend(_Resp(502, gateway) for _ in range(delivery.MAX_ATTEMPTS))
    assert n.send("hello", kind="evening") is False
    # The budget is spent on the message itself: no notice past it.
    assert len(posts) == delivery.MAX_ATTEMPTS and len(sleeps) == delivery.MAX_ATTEMPTS - 1
    undelivered = [r for r in rows if r["status"] == delivery.UNDELIVERED_STATUS]
    assert len(undelivered) == 1 and f"after {delivery.MAX_ATTEMPTS} attempts" in undelivered[0]["detail"]
    assert "Bad Gateway" in undelivered[0]["detail"]

    posts.clear()
    rows.clear()
    responses.extend(requests.ConnectionError(f"DNS hiccup for url: {URL}") for _ in range(delivery.MAX_ATTEMPTS))
    assert n.send("hello") is False
    assert len(posts) == delivery.MAX_ATTEMPTS
    failed = [r for r in rows if r["status"] == "failed"]
    assert failed and all(TOKEN not in r["detail"] and "<redacted>" in r["detail"] for r in failed)
