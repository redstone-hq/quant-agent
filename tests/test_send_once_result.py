"""One Telegram send attempt returns why it failed, not just False.

`send_once` used to return a bare bool, so Telegram's reason for a refusal
was lost on the way out and the retry loop had to fetch it from a per-thread
slot on the notifier. The attempt now returns its own result: ok, Telegram's
description, the status code and the message id.
"""

from __future__ import annotations

import threading

import pytest
import requests

import src.notifier.owner_alert_delivery as delivery
from src.notifier.transport import TelegramNotifier

TOKEN = "123456:ABCDEFtoken"
URL = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
PARSE = "Bad Request: can't parse entities"
CHAT = "Bad Request: chat not found"


class _Resp:
    def __init__(self, status: int, body: dict):
        self.status_code = status
        self._body = body

    def json(self) -> dict:
        return self._body

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code} Client Error for url: {URL}", response=self)


def _refusal(description: str) -> _Resp:
    return _Resp(400, {"ok": False, "error_code": 400, "description": description})


@pytest.fixture
def rig(monkeypatch, tmp_path):
    """A real notifier whose `requests.post` is scripted by a caller-set function."""
    import src.notifier.base as base
    import src.notifier.transport as transport

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(base, "_DB_PATH", tmp_path / "n.db")
    monkeypatch.setattr("src.notifier.credit_line.APPEND_CREDIT_LINE", False)
    monkeypatch.delenv("TELEGRAM_DISABLED", raising=False)
    monkeypatch.setattr(delivery.time, "sleep", lambda s: None)
    rows: list[dict] = []
    script = {"answer": lambda payload: _Resp(200, {"ok": True, "result": {"message_id": 1}})}

    def fake_post(url, json=None, timeout=None):
        return script["answer"](json)

    monkeypatch.setattr(transport.requests, "post", fake_post)
    n = TelegramNotifier(token=TOKEN, chat_id="chat-1")
    monkeypatch.setattr(n, "_safe_record_send", lambda **k: rows.append(k))
    return n, script, rows


def test_a_refused_attempt_returns_telegrams_reason(rig):
    n, script, _rows = rig
    script["answer"] = lambda payload: _refusal(PARSE)
    result = n.send_once("hello")
    assert result.ok is False
    assert result.description == PARSE
    assert result.status_code == 400
    assert result.message_id is None
    assert not result


def test_a_landed_attempt_returns_the_message_id(rig):
    n, script, _rows = rig
    script["answer"] = lambda payload: _Resp(200, {"ok": True, "result": {"message_id": 42}})
    result = n.send_once("hello")
    assert result.ok is True
    assert result.message_id == 42
    assert result.status_code == 200
    assert result.description is None
    assert result


def test_two_threads_failing_for_different_reasons_each_keep_their_own(rig):
    n, script, rows = rig
    both_in_flight = threading.Barrier(2, timeout=10)

    def answer(payload):
        text = payload["text"]
        if "ALPHA" in text or "BRAVO" in text:
            both_in_flight.wait()
            return _refusal(PARSE if "ALPHA" in text else CHAT)
        return _Resp(200, {"ok": True, "result": {"message_id": 7}})

    script["answer"] = answer
    results: dict[str, object] = {}
    errors: list[BaseException] = []

    def attempt(name: str) -> None:
        try:
            results[name] = n.send_once(f"{name} message")
        except BaseException as exc:  # noqa: BLE001 - surfaced by the assert below
            errors.append(exc)

    threads = [threading.Thread(target=attempt, args=(name,)) for name in ("ALPHA", "BRAVO")]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)
    assert not errors
    assert results["ALPHA"].description == PARSE
    assert results["BRAVO"].description == CHAT


def test_the_undelivered_row_names_the_reason_of_its_own_message(rig):
    n, script, rows = rig

    def answer(payload):
        text = payload["text"]
        if "ALPHA" in text:
            return _refusal(PARSE)
        return _refusal(CHAT) if "BRAVO" in text else _Resp(200, {})

    script["answer"] = answer
    results = {}

    def deliver(name: str) -> None:
        results[name] = delivery.deliver_with_outcome(n, f"{name} message", kind="owner_alert")

    threads = [threading.Thread(target=deliver, args=(name,)) for name in ("ALPHA", "BRAVO")]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)
    assert results == {"ALPHA": (False, False), "BRAVO": (False, False)}
    undelivered = {
        ("ALPHA" if "ALPHA" in r["text"] else "BRAVO"): r["detail"]
        for r in rows
        if r["status"] == delivery.UNDELIVERED_STATUS
    }
    assert PARSE in undelivered["ALPHA"] and CHAT not in undelivered["ALPHA"]
    assert CHAT in undelivered["BRAVO"] and PARSE not in undelivered["BRAVO"]


def test_a_refusal_is_still_never_resent(rig):
    n, script, rows = rig
    posts = []

    def answer(payload):
        posts.append(payload["text"])
        return _refusal(PARSE) if "ALPHA" in payload["text"] else _Resp(200, {})

    script["answer"] = answer
    assert delivery.deliver_with_outcome(n, "ALPHA message", kind="owner_alert") == (False, False)
    assert [p for p in posts if "ALPHA" in p] == ["ALPHA message"]
