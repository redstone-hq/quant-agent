"""Owner, 2026-10-12: "I should see current credit every alert."

The one balance fetch the $3 alert already makes also feeds a credit line on
every outgoing text message, added once in the public send.
"""

import pytest

import src.notifier.owner_alert_delivery as delivery
import src.openrouter_balance as ob
from src.notifier.transport import TelegramNotifier

LINE = "AI credit: $12.34 left"
MISSING = "AI credit: could not read"


@pytest.fixture
def rig(monkeypatch, tmp_path):
    """A real notifier with the wire stubbed; the alert state file is in tmp."""
    monkeypatch.chdir(tmp_path)  # the alert state path is relative
    monkeypatch.setattr("src.notifier.credit_line.APPEND_CREDIT_LINE", True)
    monkeypatch.delenv("TELEGRAM_DISABLED", raising=False)
    monkeypatch.setattr(delivery.time, "sleep", lambda s: None)
    wire = []
    fetches = []

    def fetch():
        fetches.append(1)
        return {"remaining_usd": 12.34}

    monkeypatch.setattr(ob, "fetch_openrouter_balance", fetch)
    n = TelegramNotifier(token="t", chat_id="c", mission_control_url="")
    monkeypatch.setattr(n, "_post_payload", lambda payload: wire.append(payload["text"]))
    monkeypatch.setattr(n, "_safe_record_send", lambda **k: None)
    return n, wire, fetches


def test_normal_alert_carries_the_fetched_value(rig):
    n, wire, _ = rig
    assert n.send("Bought AAPL") is True
    assert wire == ["Bought AAPL\n\n" + LINE]


def test_fetch_called_once_per_message_including_retry(rig, monkeypatch):
    n, wire, fetches = rig
    calls = []

    def flaky(payload):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("telegram down")
        wire.append(payload["text"])

    monkeypatch.setattr(n, "_post_payload", flaky)
    assert n.send("Sold MSFT") is True
    assert len(calls) == 2
    assert len(fetches) == 1
    assert wire == ["Sold MSFT\n\n" + LINE]


def test_long_message_keeps_line_intact_within_4000(rig):
    n, wire, _ = rig
    assert n.send("word & <b> " * 1000) is True
    assert len(wire[0]) <= 4000
    assert wire[0].endswith(LINE)
    assert "[...truncated]" in wire[0]


def test_long_message_with_link_keeps_line_and_link(rig):
    n, wire, _ = rig
    assert n.send("word " * 2000, link_url="https://x.test/mc", link_label="MC") is True
    assert len(wire[0]) <= 4000
    assert LINE + '\n\n<a href="https://x.test/mc">MC</a>' in wire[0]


def test_failed_fetch_says_could_not_read(rig, monkeypatch):
    n, wire, _ = rig

    def boom():
        raise RuntimeError("down")

    monkeypatch.setattr(ob, "fetch_openrouter_balance", boom)
    assert n.send("hello") is True
    assert wire == ["hello\n\n" + MISSING]
    assert "$" not in wire[0]


def test_probe_message_carries_no_line(rig, monkeypatch):
    import src.notifier.transport as transport

    n, _, fetches = rig
    bodies = []

    class _Resp:
        status_code = 200

        def json(self):
            return {"ok": True, "result": {"message_id": 1}}

    def fake_post(url, json=None, timeout=None):
        bodies.append((json or {}).get("text"))
        return _Resp()

    monkeypatch.setattr(transport.requests, "post", fake_post)
    n.probe()
    texts = [b for b in bodies if b]
    assert texts, "probe sent nothing, so this test proves nothing"
    assert all("AI credit" not in b for b in texts)
    assert fetches == []


def test_sent_record_keeps_the_body_as_sent_including_the_line(rig, monkeypatch):
    n, wire, _ = rig
    recorded = []
    monkeypatch.setattr(n, "_safe_record_send", lambda **k: recorded.append(k))
    assert n.send("Bought AAPL") is True
    sent = [r for r in recorded if r.get("status") == "sent"]
    assert [r["text"] for r in sent] == wire == ["Bought AAPL\n\n" + LINE]


def test_owner_alert_carries_the_line_exactly_once(rig):
    # 12 Oct 14:03 UTC: a cost-circuit owner alert went out without the line,
    # because owner alerts reach `deliver_with_outcome` without passing `send`.
    from src.notifier.owner_alert_funnel import send_owner_alert_with_outcome

    n, wire, fetches = rig
    delivered, suppressed = send_owner_alert_with_outcome(
        "QUOTA HOLD", notifier=n, kind="cost_circuit", pnl_header=False
    )
    assert delivered is True and suppressed is False
    assert len(wire) == 1 and wire[0].endswith("\n\n" + LINE)
    assert wire[0].count("AI credit:") == 1
    assert len(fetches) == 1


def test_direct_delivery_with_single_attempt_carries_the_line(rig):
    n, wire, fetches = rig
    delivered, _ = delivery.deliver_with_outcome(n, "cost hold", max_attempts=1, kind="cost_circuit")
    assert delivered is True
    assert wire == ["cost hold\n\n" + LINE]
    assert len(fetches) == 1
