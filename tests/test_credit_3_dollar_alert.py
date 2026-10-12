"""Owner ruling 2026-10-09: low-credit alert at a fixed $3, checked on every send."""

from unittest.mock import MagicMock

from src.llm_balance_runway import LOW_CREDIT_ALERT_USD, check_balance_on_send


def _fetch(amount):
    return lambda: {"remaining_usd": amount}


def test_threshold_is_three_dollars():
    assert LOW_CREDIT_ALERT_USD == 3.0


def test_three_dollars_fires(tmp_path):
    send = MagicMock(return_value=True)
    assert check_balance_on_send(fetch=_fetch(3.00), path=tmp_path / "s.json", send=send) is True
    assert send.call_count == 1


def test_three_oh_one_does_not_fire(tmp_path):
    send = MagicMock(return_value=True)
    assert check_balance_on_send(fetch=_fetch(3.01), path=tmp_path / "s.json", send=send) is False
    send.assert_not_called()


def test_second_call_same_state_does_not_resend(tmp_path):
    send = MagicMock(return_value=True)
    path = tmp_path / "s.json"
    check_balance_on_send(fetch=_fetch(2.50), path=path, send=send)
    assert check_balance_on_send(fetch=_fetch(2.40), path=path, send=send) is False
    assert send.call_count == 1


def test_fetch_failure_is_logged_and_does_not_raise(tmp_path, caplog):
    def boom():
        raise RuntimeError("down")

    send = MagicMock()
    assert check_balance_on_send(fetch=boom, path=tmp_path / "s.json", send=send) is False
    send.assert_not_called()
    assert any(r.exc_info for r in caplog.records)


def test_fetch_failure_still_sends_the_message(monkeypatch):
    import src.openrouter_balance as ob
    from src.notifier.transport import TelegramNotifier

    def boom():
        raise RuntimeError("down")

    monkeypatch.setattr(ob, "fetch_openrouter_balance", boom)
    seen = []
    monkeypatch.setattr(
        "src.notifier.send_funnel.deliver_with_outcome", lambda *a, **k: seen.append(a[1]) or (True, False)
    )
    n = TelegramNotifier.__new__(TelegramNotifier)
    assert n.send("hello") is True
    assert seen == ["hello"]
