"""The live-fill websocket switch, and the alerts that replace its silence.

BACKGROUND (owner decision 2026-09-17). Alpaca's `trade_updates` websocket
has never once authenticated on this host since it was built on 2026-09-10.
Two independent blockers were confirmed, so nothing in this repo could fix
it: the process holds placeholder Alpaca credentials that only a local
REST-only injecting proxy substitutes, and the installed `alpaca-py` stream
is built on `websockets.legacy` (no proxy support at all); and Alpaca
authenticates the stream with an in-band MESSAGE, not a handshake header,
which a header-injecting gateway cannot supply either way. Every fill wait
therefore burned part of its own bounded window on a doomed handshake and
then degraded to the REST fallback, which is what has actually confirmed
every fill the desk has ever made.

So the socket is OFF by configuration (`execution.fill_stream_enabled`),
and the REST path is now the mechanism rather than the fallback. This file
pins the four things that must stay true:

  1. OFF means no socket is constructed and the account-wide lease is
     never taken — not "opened and then ignored".
  2. OFF does not move a single bounded wait. The flag takes the SAME
     branch as the long-standing `use_stream=False` escape hatch.
  3. ON restores the previous behaviour (tests/test_order_fill_stream.py is
     the full proof; one assertion here guards the switch itself).
  4. The owner is told when fill confirmation actually degrades — and is
     NOT told merely because the socket is off, which is the configuration
     and must never page.
"""

import types
from unittest.mock import MagicMock, patch

from src.storage.db import Database
from src.execution.broker import AlpacaBroker, _ENTRY_FILL_TIMEOUT_S
from tests.pipeline_factory import build_pipeline


def _broker(*, enabled: bool, lease_path=None) -> AlpacaBroker:
    with patch("src.execution.broker.TradingClient"):
        return AlpacaBroker(
            api_key="k",
            secret_key="s",
            paper=True,
            fill_stream_enabled=enabled,
            trade_updates_lease_path=str(lease_path) if lease_path else None,
        )


def _order(status: str):
    return types.SimpleNamespace(status=types.SimpleNamespace(value=status))


# ---------------------------------------------------------------------------
# 1. OFF never opens a socket and never takes the lease
# ---------------------------------------------------------------------------


@patch("src.execution.broker.TradingStream")
def test_off_never_constructs_a_stream_or_takes_the_lease(mock_stream_cls, tmp_path):
    lease = tmp_path / ".trade_updates.lock"
    broker = _broker(enabled=False, lease_path=lease)

    warmup = broker.start_trade_updates()

    mock_stream_cls.assert_not_called()
    assert broker._trade_slot_held is False
    assert broker._trade_lease.held() is False
    assert broker.trade_updates_started() is False
    # The lock FILE is not even created — nothing opened it.
    assert not lease.exists()
    # And the desk is not told it lost time to a broken socket: a socket
    # that was never opened is not a stall (see `_fill_stream_off_warmup`).
    assert warmup.handshake_failed is False
    assert warmup.retried is False


@patch("src.execution.broker.TradingStream")
def test_off_ensure_trade_updates_is_also_inert(mock_stream_cls, tmp_path):
    """`ensure_trade_updates` is the Execution-side warmup entry point;
    `start_trade_updates` is the Risk-side one. Both must be inert, or the
    socket comes back through the other door."""
    broker = _broker(enabled=False, lease_path=tmp_path / "l.lock")
    broker.ensure_trade_updates()
    mock_stream_cls.assert_not_called()
    assert broker._trade_slot_held is False


@patch("src.execution.broker.TradingStream")
def test_off_fill_wait_never_constructs_a_stream(mock_stream_cls, tmp_path):
    broker = _broker(enabled=False, lease_path=tmp_path / "l.lock")
    broker.client.get_order_by_id.return_value = _order("filled")

    assert broker.wait_for_order_terminal("o-1", timeout_seconds=5.0) == "filled"
    mock_stream_cls.assert_not_called()
    assert broker._trade_slot_held is False


@patch("src.execution.broker.TradingStream")
def test_off_direct_stream_wait_refuses_before_touching_the_slot(
    mock_stream_cls,
    tmp_path,
):
    """The guarantee must not depend on every caller routing through the
    public wrapper — `_wait_for_order_status_via_stream` is the only place
    that can construct a stream or grab the slot, so it refuses itself."""
    broker = _broker(enabled=False, lease_path=tmp_path / "l.lock")

    status, connected = broker._wait_for_order_terminal_via_stream("o-1", 5.0)

    assert (status, connected) == (None, False)
    mock_stream_cls.assert_not_called()
    assert broker._trade_slot_held is False


@patch("src.execution.broker.TradingStream")
def test_off_logs_no_auth_failure_lines(mock_stream_cls, tmp_path, caplog):
    """The ~150 `trade_updates` auth-failure lines a day must stop
    ENTIRELY, not move to a different level."""
    caplog.set_level("DEBUG")
    broker = _broker(enabled=False, lease_path=tmp_path / "l.lock")
    broker.client.get_order_by_id.return_value = _order("filled")

    broker.start_trade_updates()
    broker.ensure_trade_updates()
    broker.wait_for_order_terminal("o-1", timeout_seconds=5.0)

    text = caplog.text.lower()
    assert "did not authenticate" not in text
    assert "handshake failed" not in text
    assert "lease held by another process" not in text
    assert "hub unusable" not in text
    # One INFO line for ops, once per process — not once per order.
    assert text.count("disabled by configuration") == 1


# ---------------------------------------------------------------------------
# 2. OFF moves no bounded wait
# ---------------------------------------------------------------------------


def test_off_uses_the_identical_rest_path_as_use_stream_false(tmp_path):
    """Not "an equivalent path" — the same call, with the same numbers.

    If this ever diverges, turning the socket off has silently become a
    behaviour change to the fill window.
    """
    seen = []

    def _spy(self, order_id, timeout_seconds, poll_interval, *, stop_states):
        seen.append((order_id, timeout_seconds, poll_interval, stop_states))
        return "filled"

    with patch.object(
        AlpacaBroker,
        "_wait_for_order_status_via_polling",
        _spy,
    ):
        off = _broker(enabled=False, lease_path=tmp_path / "a.lock")
        off.wait_for_order_terminal("o-1", timeout_seconds=90.0, poll_interval=1.0)

        on = _broker(enabled=True, lease_path=tmp_path / "b.lock")
        on.wait_for_order_terminal(
            "o-1",
            timeout_seconds=90.0,
            poll_interval=1.0,
            use_stream=False,
        )

    assert len(seen) == 2
    assert seen[0] == seen[1]


def test_off_does_not_charge_the_submit_window_for_a_handshake(tmp_path):
    """The entry-submit window is the SUM OF THE WAITS ACTUALLY PROGRAMMED.

    With no socket there is no handshake ahead of submit, so the auth
    budget must not be counted. This tightens no configured timeout — it
    stops claiming a wait that cannot occur.
    """
    from src.pipeline_stages import _known_entry_submit_budget_s
    from src.execution.broker import _ALPACA_STREAM_AUTH_DEADLINE_S

    off = types.SimpleNamespace(
        broker=_broker(
            enabled=False,
            lease_path=tmp_path / "a.lock",
        )
    )
    on = types.SimpleNamespace(
        broker=_broker(
            enabled=True,
            lease_path=tmp_path / "b.lock",
        )
    )

    assert _known_entry_submit_budget_s(off) == 0.0
    assert _known_entry_submit_budget_s(on) == float(
        _ALPACA_STREAM_AUTH_DEADLINE_S,
    )


def test_off_rest_path_still_confirms_a_fill(tmp_path):
    broker = _broker(enabled=False, lease_path=tmp_path / "l.lock")
    broker.client.get_order_by_id.return_value = _order("filled")

    assert broker.wait_for_order_terminal("o-1", timeout_seconds=5.0) == "filled"
    assert broker.wait_for_order_at_exchange("o-1", timeout_seconds=5.0) == "filled"


# ---------------------------------------------------------------------------
# 3. ON restores the old path
# ---------------------------------------------------------------------------


@patch("src.execution.broker.TradingStream")
def test_on_opens_the_socket_again(mock_stream_cls, tmp_path):
    """The reversal the deferred credential decision would need. The whole
    of tests/test_order_fill_stream.py runs with the flag ON and is the real
    proof; this pins the switch itself."""
    broker = _broker(enabled=True, lease_path=tmp_path / "l.lock")
    broker.start_trade_updates()

    assert mock_stream_cls.called
    assert broker._trade_slot_held is True
    broker.stop_trade_updates()


def test_production_default_is_off():
    """`ExecutionConfig` ships the switch OFF, and a broker built without
    the argument fails closed rather than opening the account's one socket
    by omission."""
    from src.config import ExecutionConfig

    assert ExecutionConfig(max_entry_slippage_bps=40).fill_stream_enabled is False
    with patch("src.execution.broker.TradingClient"):
        assert AlpacaBroker(api_key="k", secret_key="s").fill_stream_enabled() is False


# ---------------------------------------------------------------------------
# 4. The alerts — and what must NOT alert
# ---------------------------------------------------------------------------


@patch("src.notifier.send_owner_alert")
@patch("src.execution.broker.TradingStream")
def test_socket_being_off_never_alerts(mock_stream_cls, mock_alert, tmp_path):
    """The load-bearing negative. The socket being off is the intended
    configuration; paging on it would move ~150 daily error lines out of
    the log and into Telegram, which is strictly worse."""
    broker = _broker(enabled=False, lease_path=tmp_path / "l.lock")
    broker.client.get_order_by_id.return_value = _order("filled")

    broker.start_trade_updates()
    broker.ensure_trade_updates()
    broker.wait_for_order_terminal("o-1", timeout_seconds=5.0)
    broker.stop_trade_updates()

    mock_alert.assert_not_called()


@patch("src.notifier.send_owner_alert")
def test_alert_fires_when_an_order_outcome_cannot_be_confirmed(
    mock_alert,
    tmp_path,
):
    """The bounded REST window closed, the cancel-and-recheck closed, and
    the broker still has not said what happened. The desk proceeds on
    "nothing bought" — the owner has to be told that may be wrong."""
    broker = _broker(enabled=False, lease_path=tmp_path / "l.lock")
    # Never reaches a terminal state, however long the REST path polls —
    # patched rather than slept so the test does not burn the real
    # `_ENTRY_FILL_TIMEOUT_S` window.
    broker.wait_for_order_terminal = MagicMock(return_value="new")
    broker.get_order_fill_info = MagicMock(return_value={"filled_qty": 0})

    broker.place_entry_protection(
        symbol="AAPL",
        order_id="o-1",
        side="buy",
        stop_price=1.0,
    )

    assert mock_alert.called
    body = mock_alert.call_args[0][0]
    assert "ORDER OUTCOME NOT CONFIRMED" in body
    assert "AAPL" in body
    # Owner wording rules: plain English, a 12-hour clock with the zone, no
    # run ids, no internal jargon.
    assert " ET" in body and ("AM" in body or "PM" in body)
    for jargon in ("websocket", "trade_updates", "REST", "run_id", "o-1"):
        assert jargon not in body
    # The number comes from the existing bounded-wait constant, not a new one.
    assert str(int(_ENTRY_FILL_TIMEOUT_S)) in body


@patch("src.notifier.send_owner_alert")
def test_alert_fires_when_desk_and_broker_records_disagree(mock_alert, tmp_path):
    """The reconciler's `stop_out_gap_unexplained` outcome: the ledger
    believes it holds shares the broker does not have, and no sale in the
    broker's own history explains it. Until now that reached an ERROR line
    and an evidence row and nothing the owner would ever see."""
    db = Database(str(tmp_path / "t.db"))
    db.initialize()
    db.insert_trade("ONDS", "BUY", 17, 8.53, "entry", "r1", fill_status="filled", stop_loss=90.0)

    broker = MagicMock()
    broker.get_positions.return_value = []
    broker.list_filled_sell_orders.return_value = []

    pipeline = build_pipeline(db=db, broker=broker)
    pipeline.config = types.SimpleNamespace(
        reconciliation=types.SimpleNamespace(stop_out_lookback_days=7),
    )
    pipeline._reconcile_stop_out_fills(run_id="r1")

    assert mock_alert.called
    body = mock_alert.call_args[0][0]
    assert "RECORDS DISAGREE" in body
    assert "ONDS" in body
    assert "17" in body
    assert " ET" in body and ("AM" in body or "PM" in body)
    for jargon in ("websocket", "ledger", "run_id", "reconcil"):
        assert jargon not in body


@patch("src.notifier.send_owner_alert")
def test_no_alert_when_records_agree(mock_alert, tmp_path):
    """The reconciler runs at every session entry point, several times a
    day. It must be silent when there is nothing wrong."""
    db = Database(str(tmp_path / "t.db"))
    db.initialize()
    db.insert_trade("ONDS", "BUY", 17, 8.53, "entry", "r1", fill_status="filled", stop_loss=90.0)

    broker = MagicMock()
    broker.get_positions.return_value = [
        types.SimpleNamespace(symbol="ONDS", qty=17.0),
    ]

    pipeline = build_pipeline(db=db, broker=broker)
    pipeline.config = types.SimpleNamespace(
        reconciliation=types.SimpleNamespace(stop_out_lookback_days=7),
    )
    pipeline._reconcile_stop_out_fills(run_id="r1")

    mock_alert.assert_not_called()


@patch("src.notifier.send_owner_alert")
def test_a_confirmed_fill_never_alerts(mock_alert, tmp_path):
    """A KNOWN outcome is not a degradation, whatever it was."""
    broker = _broker(enabled=False, lease_path=tmp_path / "l.lock")
    broker.wait_for_order_terminal = MagicMock(return_value="filled")
    broker.get_order_fill_info = MagicMock(
        return_value={"filled_qty": 10, "filled_avg_price": 100.0},
    )

    broker.place_entry_protection(
        symbol="AAPL",
        order_id="o-1",
        side="buy",
        stop_price=95.0,
    )

    mock_alert.assert_not_called()
