"""Owner ruling 5b (13 Oct): when an order the session message listed as
"placed" fills, the SAME Telegram message is edited so its line reads
"filled" -- no new message, no chosen delay.

The fill path is money-adjacent, so the edit rides on it and may never
change it: a raising edit must leave the fill recorded and the reconciler
returning normally.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from src.notifier.category import CATEGORY_OPERATIONAL
from src.notifier.transport import TelegramNotifier
from src.protection.fill_reconciler import FillReconciler
from src.storage.db import Database
from src.trade_actions import broker_fill_time

TOKEN = "123456:ABCDEFtoken"
MESSAGE_ID = 4242
SELL_ORDER = "ord-zzz-sell-1"
SESSION_TEXT = (
    "✅ MORNING · 9:45 AM · 1 order\n"
    "<b>✅ DONE</b>\n"
    "   • SELL ZZZ 6.95 @ $425.23 — placed, waiting to fill — the thesis broke\n"
    "   • BUY YYY 3 @ $20.00 — filled · stop $18.00"
)


class _Resp:
    def __init__(self, body: dict):
        self.status_code = 200
        self._body = body

    def json(self) -> dict:
        return self._body

    def raise_for_status(self) -> None:
        return None


class _Broker:
    def __init__(self, info: dict[str, dict]):
        self.info = info

    def get_order_fill_info(self, order_id: str) -> dict | None:
        return self.info.get(order_id)


def _iso(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


@pytest.fixture
def rig(monkeypatch, tmp_path):
    """One real DB shared by the ledger and the notifier; `requests.post` faked."""
    import src.notifier.base as base
    import src.notifier.owner_alert_delivery as delivery
    import src.notifier.transport as transport

    path = tmp_path / "desk.db"
    monkeypatch.setattr(base, "_DB_PATH", path)
    monkeypatch.setattr("src.notifier.credit_line.APPEND_CREDIT_LINE", False)
    monkeypatch.delenv("TELEGRAM_DISABLED", raising=False)
    monkeypatch.delenv("TELEGRAM_RISK_ONLY", raising=False)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "chat-1")
    monkeypatch.setattr(delivery.time, "sleep", lambda s: None)
    calls: list[tuple[str, dict]] = []

    def fake_post(url, json=None, timeout=None, **_):
        calls.append((url, json or {}))
        return _Resp({"ok": True, "result": {"message_id": MESSAGE_ID}})

    monkeypatch.setattr(transport.requests, "post", fake_post)

    db = Database(str(path))
    db.initialize()
    sell_time = datetime.now(timezone.utc).replace(microsecond=0)
    # Two buys averaging exactly 431.06, the first 26 days before the sell.
    for n, (qty, price, ago) in enumerate([(3.475, 430.06, 26), (3.475, 432.06, 10)]):
        order = f"ord-zzz-buy-{n}"
        db.insert_trade(
            "ZZZ", "BUY", qty, price, "entry", "run-old", 400.0, 0, broker_order_id=order, fill_status="submitted"
        )
        db.update_trade_fill(
            broker_order_id=order,
            fill_status="filled",
            fill_qty=qty,
            fill_price=price,
            filled_at=broker_fill_time(_iso(sell_time - timedelta(days=ago))),
        )
    db.insert_trade(
        "ZZZ",
        "SELL",
        6.95,
        425.23,
        "the thesis broke",
        "run-now",
        0,
        0,
        broker_order_id=SELL_ORDER,
        fill_status="submitted",
    )
    broker = _Broker(
        {SELL_ORDER: {"status": "filled", "filled_qty": 6.95, "filled_avg_price": 425.23, "filled_at": _iso(sell_time)}}
    )
    reconciler = FillReconciler(
        broker=broker,
        db=db,
        config=None,
        flag_stop_out_anomaly=lambda *a, **k: None,
        format_qty=lambda q: f"{q:g}",
        parse_broker_fill_timestamp=lambda s: s,
    )
    yield {"db": db, "calls": calls, "reconciler": reconciler}
    db.close()


def _send_session(calls: list) -> None:
    notifier = TelegramNotifier(token=TOKEN, chat_id="chat-1")
    assert notifier.send(SESSION_TEXT, preserve_structural_markup=True, category=CATEGORY_OPERATIONAL)
    calls.clear()


def _settle() -> None:
    """Wait for the background edit; a trunk without the module has none to wait for."""
    try:
        from src.notifier import fill_line
    except ImportError:
        return
    for thread in list(fill_line._RECENT):
        thread.join(timeout=10)


def _sell_row(db: Database) -> dict:
    row = db.conn.execute("SELECT * FROM trades WHERE broker_order_id = ?", (SELL_ORDER,)).fetchone()
    return dict(row)


def test_a_fill_edits_the_placed_line_in_the_same_message(rig):
    _send_session(rig["calls"])

    rig["reconciler"]._reconcile_fills(None)
    _settle()

    edits = [(u, p) for u, p in rig["calls"] if u.endswith("/editMessageText")]
    sends = [(u, p) for u, p in rig["calls"] if u.endswith("/sendMessage")]
    assert len(edits) == 1, rig["calls"]
    assert sends == []
    payload = edits[0][1]
    assert payload["message_id"] == MESSAGE_ID
    expected = (425.23 - 431.06) * 6.95
    figure = f"{'-' if expected < 0 else '+'}${abs(expected):,.2f}"
    text = payload["text"]
    zzz_line = next(line for line in text.splitlines() if "ZZZ" in line)
    assert "filled" in zzz_line and "placed" not in zzz_line, zzz_line
    assert figure in zzz_line, (figure, zzz_line)
    assert "26 days" in zzz_line, zzz_line
    assert "$431.06" in zzz_line, zzz_line
    # The rest of the message is untouched.
    assert "BUY YYY 3 @ $20.00 — filled · stop $18.00" in text


def test_a_raising_edit_leaves_the_fill_recorded(rig, monkeypatch):
    from src.notifier import fill_line

    _send_session(rig["calls"])

    def boom(*a, **k):
        raise RuntimeError("edit exploded")

    monkeypatch.setattr(fill_line, "_edit_placed_lines", boom)
    rig["reconciler"]._reconcile_fills(None)  # must not raise
    _settle()

    row = _sell_row(rig["db"])
    assert row["fill_status"] == "filled"
    assert float(row["fill_qty"]) == pytest.approx(6.95)
    assert float(row["fill_price"]) == pytest.approx(425.23)
    assert [u for u, _ in rig["calls"] if u.endswith("/sendMessage")] == []


def test_a_raising_dispatch_leaves_the_fill_recorded(rig, monkeypatch):
    from src.notifier import fill_line

    _send_session(rig["calls"])

    def boom(*a, **k):
        raise RuntimeError("thread start exploded")

    monkeypatch.setattr(fill_line, "_spawn", boom)
    rig["reconciler"]._reconcile_fills(None)  # must not raise

    assert _sell_row(rig["db"])["fill_status"] == "filled"


def test_no_stored_message_means_no_edit(rig):
    rig["reconciler"]._reconcile_fills(None)
    _settle()

    assert rig["calls"] == []
    assert _sell_row(rig["db"])["fill_status"] == "filled"
