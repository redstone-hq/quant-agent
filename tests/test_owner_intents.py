"""Stop cancels resting unfilled entries when it is recorded; any fault alerts the owner.

`freeze_cancel.cancel_resting_entries` never raises when the broker refuses a
cancel -- it returns the fault -- so the refusing broker here refuses WITHOUT
raising, through the real sweep.
"""

import json
import os
import sqlite3
from types import SimpleNamespace as NS

import pytest

from src import owner_intents
from src.execution import freeze_cancel
from src.owner_intents import STOP, process_pending, raise_intent
from src.owner_switch import app
from src.storage.schema.owner_intent_tables import apply


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

    def sweep_frozen_resting_orders(self, db_path):
        return freeze_cancel.sweep_if_frozen(self, db_path)


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


def _stopped_db(tmp_path):
    db = str(tmp_path / "d.db")
    conn = sqlite3.connect(db)
    apply(conn)
    raise_intent(conn, STOP)
    process_pending(conn)
    conn.close()
    return db


def test_a_refused_cancel_that_does_not_raise_alerts_the_owner(tmp_path, alerts):
    sent, rows = alerts
    broker = _Broker(cancel_ok=False)
    assert owner_intents.honour_stop(_stopped_db(tmp_path), lambda: broker) is True
    assert broker.cancelled == ["e1"]
    assert rows == [owner_intents.STOP_CANCEL_FAILED]
    assert len(sent) == 1
    assert "Stop is on, but 1 unfilled order could not be cancelled" in sent[0]
    assert "Cancel them at the broker by hand" in sent[0]


def test_an_unreachable_broker_alerts_the_owner(tmp_path, alerts):
    sent, rows = alerts

    def down():
        raise ConnectionError("broker down")

    assert owner_intents.honour_stop(_stopped_db(tmp_path), down) is True
    assert rows == [owner_intents.STOP_CANCEL_FAILED]
    assert "could not reach the broker" in sent[0]


def test_a_clean_cancel_alerts_nobody(tmp_path, alerts):
    sent, rows = alerts
    broker = _Broker()
    assert owner_intents.honour_stop(_stopped_db(tmp_path), lambda: broker) is True
    assert broker.cancelled == ["e1"]
    assert sent == [] and rows == []


def _post(settings, word):
    headers = {
        "Origin": settings.origin,
        "Tailscale-User-Login": settings.owner_login,
        "X-Owner-Token": settings.token,
        "Content-Type": "application/json",
    }
    return app.handle_post(settings, "/action", headers, json.dumps({"action": word}).encode())


@pytest.mark.parametrize("word,runs", [("stop", True), ("freeze", False), ("start", False)])
def test_the_switch_runs_the_stop_path_only_when_it_records_a_stop(tmp_path, monkeypatch, word, runs):
    started = []
    monkeypatch.setattr(app, "start_stop_path", lambda: started.append(True))
    settings = app.Settings(
        owner_login="owner@example", token="t" * 40, origin="https://switch.example", db_path=str(tmp_path / "d.db")
    )
    assert _post(settings, word).status == 200
    assert bool(started) is runs


@pytest.mark.parametrize("rc,alerted", [(os.EX_TEMPFAIL, False), (0, False), (os.EX_UNAVAILABLE, True), (1, True)])
def test_the_switch_alerts_on_a_cancel_fault_or_unexpected_exit_but_not_when_start_won(
    monkeypatch, alerts, rc, alerted
):
    sent, _ = alerts
    monkeypatch.setattr(app.subprocess, "run", lambda *a, **k: NS(returncode=rc))
    app._run_stop_path()
    assert bool(sent) is alerted


def test_the_switch_runs_the_stop_only_mode_and_agrees_with_main_on_exit_codes():
    import main

    cmd = app._stop_path_command()
    assert cmd[1].endswith("main.py") and cmd[2:] == ["--mode", "honour_stop"]
    assert app.STOP_SWEPT_EXIT == main.OWNER_STOP_EXIT
    assert main.OWNER_STOP_CANCEL_FAULT_EXIT not in (app.STOP_SWEPT_EXIT, app.STOP_NOT_IN_FORCE_EXIT)


@pytest.mark.parametrize(
    "outcome,code",
    [
        (owner_intents.SWEPT, os.EX_TEMPFAIL),
        (owner_intents.NOT_STOPPED, 0),
        (owner_intents.CANCEL_FAULTED, os.EX_UNAVAILABLE),
        (None, os.EX_UNAVAILABLE),
    ],
)
def test_honour_stop_mode_is_never_a_session_and_sends_nothing(monkeypatch, outcome, code):
    import main

    calls = []

    def boom(*a, **k):
        raise AssertionError("honour_stop mode built a notifier, pricing or a session")

    for name in ("TelegramNotifier", "TradingPipeline", "TradingScheduler", "refresh_pricing", "load_config"):
        monkeypatch.setattr(main, name, boom)
    monkeypatch.setattr(main, "_owner_stop_outcome", lambda cfg, *, alert: calls.append(alert) or outcome)
    monkeypatch.setattr(main.sys, "argv", ["main.py", "--mode", "honour_stop"])
    with pytest.raises(SystemExit) as exit_:
        main.main()
    assert exit_.value.code == code
    assert calls == [False]  # the mode itself never alerts; the switch does, on the exit code
