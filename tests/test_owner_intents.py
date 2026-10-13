"""Stop cancels resting unfilled entries when it is recorded; any fault alerts the owner.

`freeze_cancel.cancel_resting_entries` never raises when the broker refuses a
cancel -- it returns the fault -- so the refusing broker here refuses WITHOUT
raising.
"""

import json
from types import SimpleNamespace as NS

import pytest

from src import owner_intents
from src.owner_switch import app


class _Broker:
    def __init__(self, cancel_ok=True):
        self.cancel_ok = cancel_ok
        self.cancelled = []

    def get_positions(self):
        return []

    def list_open_orders_checked(self):
        order = NS(id="e1", symbol="AAPL", side=NS(value="buy"), qty="10", filled_qty="0", order_type=NS(value="limit"))
        return True, [order]

    def cancel_entry_order(self, order_id):
        self.cancelled.append(order_id)
        return self.cancel_ok


@pytest.fixture
def alerts(monkeypatch):
    sent, rows = [], []
    monkeypatch.setattr(
        "src.notifier.owner_alert_funnel.send_owner_alert_with_outcome",
        lambda text, **kw: sent.append(text) or (True, False),
    )
    monkeypatch.setattr("src.sentinel.counted.record_swallowed", lambda where, exc, **kw: rows.append(where))
    monkeypatch.setattr("src.execution.freeze_cancel._record", lambda *a, **k: None)
    return sent, rows


def test_a_refused_cancel_that_does_not_raise_alerts_the_owner(alerts):
    sent, rows = alerts
    broker = _Broker(cancel_ok=False)
    result = owner_intents.cancel_resting_at_stop(lambda: broker)
    assert broker.cancelled == ["e1"] and result.faults
    assert rows == [owner_intents.STOP_CANCEL_FAILED]
    assert len(sent) == 1
    assert "Stop is on, but 1 unfilled order could not be cancelled" in sent[0]
    assert "Cancel them at the broker by hand" in sent[0]


def test_an_unreachable_broker_alerts_the_owner(alerts):
    sent, rows = alerts

    def down():
        raise ConnectionError("broker down")

    assert owner_intents.cancel_resting_at_stop(down) is None
    assert rows == [owner_intents.STOP_CANCEL_FAILED]
    assert "could not reach the broker" in sent[0]


def test_a_clean_cancel_alerts_nobody(alerts):
    sent, rows = alerts
    broker = _Broker()
    result = owner_intents.cancel_resting_at_stop(lambda: broker)
    assert broker.cancelled == ["e1"] and result.ok
    assert sent == [] and rows == []


def _post(settings, word):
    headers = {
        "Origin": settings.origin,
        "Tailscale-User-Login": settings.owner_login,
        "X-Owner-Token": settings.token,
        "Content-Type": "application/json",
    }
    return app.handle_post(settings, "/action", headers, json.dumps({"action": word}).encode())


@pytest.mark.parametrize("word,cancels", [("stop", True), ("freeze", False), ("start", False)])
def test_the_switch_cancels_when_it_records_a_stop(tmp_path, monkeypatch, alerts, word, cancels):
    sent, _ = alerts
    broker = _Broker(cancel_ok=False)
    monkeypatch.setattr(app, "stop_broker", lambda: broker)
    settings = app.Settings(
        owner_login="owner@example", token="t" * 40, origin="https://switch.example", db_path=str(tmp_path / "d.db")
    )
    resp = _post(settings, word)
    assert resp.status == 200
    assert (broker.cancelled == ["e1"]) is cancels
    assert bool(sent) is cancels
