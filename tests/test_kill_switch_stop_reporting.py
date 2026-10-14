"""A kill-switch-blocked protective stop must never be reported as placed
or restored, the recovery row for it must stay open until a real stop is
confirmed live, and the owner must be alerted.

Root cause (docs/INCIDENT_HISTORY.md, 2026-09-19 entry, "found on the way,
NOT fixed"): `AlpacaBroker._submit_stop_limit_order` refuses a kill-switch
halted stop by RETURNING a dict (`id=None`, `status="kill_switch_halted"`)
rather than raising. Three callers treated "no exception" as "it worked":

  * `_submit_stop_leg_retrying` (entry protection, scale-in rearm, repair)
    logged "placed" and returned the blocked dict as a placed order;
  * `_restore_stop_orders` (WAL drain restore, replace_stop_loss rollback)
    unconditionally did `restored += 1`;
  * `_submit_stop_legs` (replace_stop_loss's own new-stop submit) appended
    the blocked dict to `placed`, violating its own documented
    "the submit either worked or it raised" contract.

Each of the three below proves one of those is now fixed, plus that an
owner alert fires and is deduped per symbol per day like the desk's other
stop-side pages.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from src.pipeline import TradingPipeline, _WAL_SELL_SENTINEL
from src.storage.db import Database
from tests.pipeline_factory import build_pipeline


def _db(tmp_path) -> Database:
    db = Database(str(tmp_path / "t.db"))
    db.initialize()
    return db


def _halted_broker(tmp_path):
    from src.execution.broker import AlpacaBroker

    with patch("src.execution.broker.TradingClient"):
        flag = tmp_path / "KILL_SWITCH"
        flag.touch()
        broker = AlpacaBroker(
            api_key="test",
            secret_key="test",
            paper=True,
            kill_switch_path=str(flag),
        )
    return broker


# ---------------------------------------------------------------------------
# (a) a kill-switch-blocked stop is never reported as placed
# ---------------------------------------------------------------------------


def test_whole_share_kill_switch_block_is_never_reported_as_placed(tmp_path):
    broker = _halted_broker(tmp_path)
    result = broker._submit_protective_stop_retrying(
        symbol="AAA",
        qty=10,
        stop_price=90.0,
        side="sell",
    )
    assert result is None
    broker.client.submit_order.assert_not_called()


def test_fractional_hybrid_kill_switch_block_is_never_reported_as_placed(tmp_path):
    """Both legs of a fractional position are blocked; the hybrid split
    must not report a partial or full cover from two refusals."""
    broker = _halted_broker(tmp_path)
    result = broker._submit_protective_stop_retrying(
        symbol="AAA",
        qty=10.5,
        stop_price=90.0,
        side="sell",
    )
    assert result is None
    broker.client.submit_order.assert_not_called()


def test_replace_stop_loss_new_leg_raises_instead_of_reporting_placed(tmp_path):
    """`_submit_stop_legs` documents 'the submit either worked or it
    raised' — a kill-switch block must become that raise, not a silently
    accepted leg, or `replace_stop_loss` logs a false 'Trailing stop
    placed'."""
    broker = _halted_broker(tmp_path)
    with pytest.raises(Exception, match="kill switch"):
        broker._submit_stop_legs(symbol="AAA", qty=10, stop_price=90.0, side="sell")


def test_scale_in_rearm_never_treats_a_block_as_covered(tmp_path):
    """`rearm_full_position_stop` routes through the same retrying path;
    a kill-switch block must come back None, not a dict `accepted_stop_order`
    would wrongly call covered."""
    from src.execution.scale_in import rearm_full_position_stop

    broker = _halted_broker(tmp_path)
    result = rearm_full_position_stop(
        broker,
        symbol="AAA",
        qty=10,
        stop_price=90.0,
    )
    assert result is None


# ---------------------------------------------------------------------------
# (b) the recovery row stays open until a real stop exists
# ---------------------------------------------------------------------------


def test_restore_stop_orders_does_not_count_a_block_as_restored(tmp_path):
    broker = _halted_broker(tmp_path)
    specs = [{"qty": 5, "stop_price": 90.0, "limit_price": None}]
    restored, failed = broker._restore_stop_orders(
        "AAA",
        specs,
        check_idempotency=False,
    )
    assert restored == 0
    assert failed == specs
    broker.client.submit_order.assert_not_called()


def test_wal_drain_restore_reports_failure_not_success_on_a_block(tmp_path):
    """`_restore_after_unconfirmed_sell` is the drain handler that decides
    whether `_drain_pending_protection_restores` deletes the recovery row.
    A blocked restore must return `ok=False` so the row is kept."""
    db = _db(tmp_path)
    pipeline = build_pipeline(db=db, broker=MagicMock())
    specs = [{"id": "s1", "qty": 10, "stop_price": 90.0}]
    # The fixed `_restore_stop_orders` behaviour: a kill-switch block is
    # reported as a failed spec, never a restored one.
    pipeline.broker._restore_stop_orders.return_value = (0, specs)
    pipeline._current_position_qty_for_finalize = MagicMock(return_value=10.0)

    ok, retry = pipeline._restore_after_unconfirmed_sell("AAA", 10.0, specs)

    assert ok is False
    assert retry == specs


def test_drain_keeps_the_row_open_when_the_kill_switch_blocks_the_restore(tmp_path):
    """End-to-end: a WAL row survives a drain pass whose only available
    restore is kill-switch-blocked — the row must NOT be discharged as if
    the position were covered again."""
    db = _db(tmp_path)
    specs = [{"id": "s1", "qty": 10, "stop_price": 90.0}]
    row_id = db.insert_pending_protection_restore(
        symbol="AAA",
        sell_order_id=_WAL_SELL_SENTINEL,
        position_qty_before_sell=10.0,
        specs_json=__import__("json").dumps(specs),
        side="sell",
    )
    pipeline = build_pipeline(db=db, broker=MagicMock())
    pipeline.broker._restore_stop_orders.return_value = (0, specs)
    pipeline._current_position_qty_for_finalize = MagicMock(return_value=10.0)
    pipeline._resolve_wal_row_side = MagicMock(return_value={})

    drained = pipeline._drain_pending_protection_restores()

    assert len(drained) == 0
    rows = db.get_pending_protection_restores()
    assert [r["id"] for r in rows] == [row_id]


# ---------------------------------------------------------------------------
# (c) an owner alert fires, deduped per symbol per trading day
# ---------------------------------------------------------------------------


@pytest.fixture
def state_path(tmp_path, monkeypatch):
    from src import alert_claims, coverage_watchdog

    path = tmp_path / "coverage_heartbeat.json"
    monkeypatch.setattr(alert_claims, "STATE_PATH", path)
    monkeypatch.setattr(coverage_watchdog, "STATE_PATH", path)
    return path


def test_a_kill_switch_block_pages_the_owner(tmp_path, state_path):
    broker = _halted_broker(tmp_path)
    db = _db(tmp_path)
    pipeline = build_pipeline(broker=broker, db=db)
    pipeline._wire_protective_stop_block_recorder()

    with patch("src.notifier.send_owner_alert") as send:
        broker._submit_stop_limit_order(
            symbol="AAA",
            qty=5,
            stop_price=90.0,
            side="sell",
        )

    send.assert_called_once()
    text = send.call_args.args[0]
    assert "KILL SWITCH" in text
    assert "AAA" in text
    assert send.call_args.kwargs["symbols"] == ["AAA"]


def test_the_same_symbol_is_not_paged_twice_in_one_day(tmp_path, state_path):
    broker = _halted_broker(tmp_path)
    db = _db(tmp_path)
    pipeline = build_pipeline(broker=broker, db=db)
    pipeline._wire_protective_stop_block_recorder()

    with patch("src.notifier.send_owner_alert") as send:
        broker._submit_stop_limit_order(symbol="AAA", qty=5, stop_price=90.0, side="sell")
        broker._submit_stop_limit_order(symbol="AAA", qty=5, stop_price=90.0, side="sell")

    assert send.call_count == 1


def test_a_different_symbol_is_still_paged_the_same_day(tmp_path, state_path):
    broker = _halted_broker(tmp_path)
    db = _db(tmp_path)
    pipeline = build_pipeline(broker=broker, db=db)
    pipeline._wire_protective_stop_block_recorder()

    with patch("src.notifier.send_owner_alert") as send:
        broker._submit_stop_limit_order(symbol="AAA", qty=5, stop_price=90.0, side="sell")
        broker._submit_stop_limit_order(symbol="BBB", qty=3, stop_price=50.0, side="sell")

    assert send.call_count == 2


def test_a_failing_alert_never_breaks_the_kill_switch_refusal(tmp_path, state_path):
    """The alert is best-effort exactly like the recorder it rides beside —
    a Telegram outage must not turn a refusal into anything else."""
    broker = _halted_broker(tmp_path)
    db = _db(tmp_path)
    pipeline = build_pipeline(broker=broker, db=db)
    pipeline._wire_protective_stop_block_recorder()

    with patch("src.notifier.send_owner_alert", side_effect=RuntimeError("down")):
        result = broker._submit_stop_limit_order(
            symbol="AAA",
            qty=5,
            stop_price=90.0,
            side="sell",
        )

    assert result["status"] == "kill_switch_halted" and result["id"] is None
