"""Durable records on three stop-side paths that used to decide and forget.

Each test below fails if the line that writes its record is removed:
  1. the trailing stop names WHY it did not trail, on a change of reason;
  2. a refused stop repair writes a row with symbol, qty, reason and what
     the broker was already holding;
  3. a protective stop the kill switch refuses is recorded, and the owner's
     text names the kill switch rather than the broker.

Recording only. The tests also pin that the decisions themselves are
unchanged (same proposal, same False, same halted status).
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

from src.execution.exit_path_records import (
    PROTECTIVE_STOP_BLOCKED_KIND,
    STOP_REPAIR_REFUSAL_KIND,
    TRAIL_STATE_KIND,
)
from src.models import Position
from src.pipeline import TradingPipeline
from src.risk.trailing import (
    TRAIL_CODE_NO_LIVE_STOP,
    TRAIL_CODE_RANGE_BELOW_1R,
    TRAIL_CODE_TRAILED,
    compute_trailing_stop,
    evaluate_trailing_stop,
)
from src.storage.db import Database
from tests.pipeline_factory import build_pipeline


def _db(tmp_path) -> Database:
    db = Database(str(tmp_path / "t.db"))
    db.initialize()
    return db


def _rows(db: Database, kind: str) -> list[dict]:
    with db._lock:
        rows = db.conn.execute(
            "SELECT symbol, run_id, evidence_json FROM specialist_evidence WHERE kind=? ORDER BY id",
            (kind,),
        ).fetchall()
    return [{"symbol": r["symbol"], "run_id": r["run_id"], **json.loads(r["evidence_json"])} for r in rows]


def _position(symbol="AAA", qty=10, avg_entry=100.0, current_price=105.0):
    return Position(
        symbol=symbol,
        qty=qty,
        avg_entry=avg_entry,
        current_price=current_price,
        market_value=qty * current_price,
        unrealized_pnl=qty * (current_price - avg_entry),
        sector="Technology",
    )


# ---------------------------------------------------------------------------
# 1. trailing stop
# ---------------------------------------------------------------------------


def test_evaluate_returns_the_same_proposal_as_compute_plus_a_code():
    kwargs = dict(
        symbol="AAA",
        setup_type="range",
        entry=100.0,
        current_price=105.0,
        current_stop=90.0,
        reference_target=140.0,
        risk_per_share=abs(100.0 - 90.0),
    )
    ev = evaluate_trailing_stop(**kwargs)
    assert ev.proposal is None and compute_trailing_stop(**kwargs) is None
    assert ev.code == TRAIL_CODE_RANGE_BELOW_1R
    kwargs["current_price"] = 112.0  # past +1R: the breakeven ratchet fires
    ev = evaluate_trailing_stop(**kwargs)
    assert ev.code == TRAIL_CODE_TRAILED
    assert ev.proposal == compute_trailing_stop(**kwargs)
    assert ev.proposal.new_stop == 100.0
    kwargs["current_stop"] = None
    assert evaluate_trailing_stop(**kwargs).code == TRAIL_CODE_NO_LIVE_STOP


def _trail_pipeline(db, buy_row, stop):
    db.get_symbol_last_buy = MagicMock(return_value=buy_row)
    p = build_pipeline(db=db, broker=MagicMock(), market=MagicMock())
    p.broker.get_current_stop_price.return_value = stop
    p.market.get_ohlcv.return_value = []
    p._atr_for_symbol = MagicMock(return_value=2.0)
    return p


def test_a_stop_that_does_not_trail_leaves_its_reason_once_per_change(tmp_path):
    db = _db(tmp_path)
    buy = {
        "setup_type": "range",
        "take_profit": 140.0,
        "stop_loss": 90.0,
        "action": "BUY",
        "qty": 10,
        "price": 100.0,
        "timestamp": "2026-09-01 14:00:00",
    }
    p = _trail_pipeline(db, buy, 90.0)
    with patch("src.execution.scale_in.pending_protection_symbols", return_value=set()):
        for run in ("r1", "r2", "r3"):
            assert p._apply_deterministic_trails([_position()], run_id=run) == []
        rows = _rows(db, TRAIL_STATE_KIND)
        # Three evaluations, one reason: ONE row, not three.
        assert [(r["symbol"], r["code"], r["run_id"]) for r in rows] == [
            ("AAA", TRAIL_CODE_RANGE_BELOW_1R, "r1"),
        ]
        assert rows[0]["current_stop"] == 90.0
        # The reason changes (the live stop disappeared): a second row.
        p.broker.get_current_stop_price.return_value = None
        p._apply_deterministic_trails([_position()], run_id="r4")
    rows = _rows(db, TRAIL_STATE_KIND)
    assert [r["code"] for r in rows] == [
        TRAIL_CODE_RANGE_BELOW_1R,
        TRAIL_CODE_NO_LIVE_STOP,
    ]
    assert rows[1]["previous_code"] == TRAIL_CODE_RANGE_BELOW_1R


def test_a_position_with_no_opening_row_is_recorded_not_skipped_silently(tmp_path):
    db = _db(tmp_path)
    p = _trail_pipeline(db, None, 90.0)
    with patch("src.execution.scale_in.pending_protection_symbols", return_value=set()):
        p._apply_deterministic_trails([_position()], run_id="r1")
    assert [r["code"] for r in _rows(db, TRAIL_STATE_KIND)] == ["no_opening_buy_row"]


def test_a_rejected_replace_is_recorded(tmp_path):
    db = _db(tmp_path)
    buy = {
        "setup_type": "range",
        "take_profit": 140.0,
        "stop_loss": 90.0,
        "action": "BUY",
        "qty": 10,
        "price": 100.0,
        "timestamp": "2026-09-01 14:00:00",
    }
    p = _trail_pipeline(db, buy, 90.0)
    with (
        patch("src.execution.scale_in.pending_protection_symbols", return_value=set()),
        patch(
            "src.execution.stop_records.replace_stop_and_record",
            return_value={"id": None, "status": "kill_switch_halted"},
        ),
    ):
        orders = p._apply_deterministic_trails(
            [_position(current_price=112.0)],
            run_id="r1",
        )
    assert orders == []
    rows = _rows(db, TRAIL_STATE_KIND)
    assert [(r["code"], r["detail"], r["proposed_stop"]) for r in rows] == [
        ("replace_not_accepted", "kill_switch_halted", 100.0),
    ]


# ---------------------------------------------------------------------------
# 2. stop repair refusal
# ---------------------------------------------------------------------------


def _repair_broker(result, price=165.0):
    broker = MagicMock()
    broker.get_latest_price_stamped = None
    broker.get_latest_price.return_value = price
    broker._submit_protective_stop_retrying.return_value = result
    return broker


def test_a_partial_repair_writes_a_durable_row_with_what_was_resting(tmp_path):
    """The 2026-09-18 shape: the whole-share leg lands, the sliver does not."""
    from src.execution.stop_repair import repair_stop_coverage

    db = _db(tmp_path)
    resting = [{"id": "gtc-1", "qty": 4.0, "stop_price": 158.0}]
    outcome = {"held_qty": 4.4, "covered_qty": 4.0}
    broker = _repair_broker(
        {
            "id": "gtc-2",
            "covered_qty": 0.0,
            "uncovered_qty": 0.4,
            "gtc_qty": 0.0,
            "day_qty": 0.0,
        }
    )
    placed = repair_stop_coverage(
        broker=broker,
        last_buy=lambda s, action="BUY": {"stop_loss": 158.75},
        symbol="NET",
        uncovered_qty=0.4,
        is_short=False,
        db=db,
        outcome=outcome,
        resting_stops=resting,
        caller="test",
    )
    assert placed is False
    rows = _rows(db, STOP_REPAIR_REFUSAL_KIND)
    assert len(rows) == 1
    row = rows[0]
    assert row["symbol"] == "NET" and row["code"] == "partial_cover"
    assert row["uncovered_qty"] == 0.4
    assert row["held_qty"] == 4.4 and row["covered_qty"] == 4.0
    assert row["resting_stops"] == resting
    assert row["reason"] == outcome["repair_refusal"]
    assert row["placed"]["uncovered_qty"] == 0.4
    assert row["caller"] == "test"


def test_a_guard_refusal_writes_a_row(tmp_path):
    from src.execution.stop_repair import repair_stop_coverage

    db = _db(tmp_path)
    placed = repair_stop_coverage(
        broker=_repair_broker(None, price=150.0),
        last_buy=lambda s, action="BUY": {"stop_loss": 158.75},
        symbol="VST",
        uncovered_qty=31.0,
        is_short=False,
        db=db,
    )
    assert placed is False
    rows = _rows(db, STOP_REPAIR_REFUSAL_KIND)
    assert [(r["symbol"], r["code"]) for r in rows] == [
        ("VST", "would_fire_immediately"),
    ]


def test_the_session_sweep_passes_the_resting_orders_to_the_record(tmp_path):
    db = _db(tmp_path)
    db.get_symbol_last_buy = MagicMock(return_value={"stop_loss": 158.75})
    db.get_pending_protection_restores = MagicMock(return_value=[])
    p = build_pipeline(db=db, broker=_repair_broker(None))
    p.broker.get_positions.return_value = [MagicMock(symbol="VST", qty=31.0)]
    resting = [{"id": "s1", "qty": 10.0, "stop_price": 158.0}]
    p.broker.snapshot_protective_stops.return_value = (True, resting)
    p.cash_sweeper = None
    with patch("src.pipeline_protection._market_is_open_now", return_value=False):
        gaps = p._reconcile_stop_coverage()
    assert gaps and gaps[0]["repaired"] is False
    rows = _rows(db, STOP_REPAIR_REFUSAL_KIND)
    assert len(rows) == 1
    assert rows[0]["resting_stops"] == resting
    assert rows[0]["covered_qty"] == 10.0
    assert rows[0]["caller"] == "session_coverage_reconcile"


# ---------------------------------------------------------------------------
# 3. kill switch blocking a protective stop
# ---------------------------------------------------------------------------


def test_a_kill_switch_block_in_repair_says_kill_switch_not_broker(tmp_path):
    from src.execution.stop_repair import repair_stop_coverage

    db = _db(tmp_path)
    outcome: dict = {}
    placed = repair_stop_coverage(
        broker=_repair_broker({"id": None, "status": "kill_switch_halted"}),
        last_buy=lambda s, action="BUY": {"stop_loss": 158.75},
        symbol="RSG",
        uncovered_qty=2.0,
        is_short=False,
        db=db,
        outcome=outcome,
    )
    assert placed is False
    assert "kill switch" in outcome["repair_refusal"]
    assert "RSG" in outcome["repair_refusal"]
    assert "broker did not accept" not in outcome["repair_refusal"]
    rows = _rows(db, STOP_REPAIR_REFUSAL_KIND)
    assert [(r["symbol"], r["code"]) for r in rows] == [("RSG", "kill_switch")]


@patch("src.execution.broker.TradingClient")
def test_the_broker_records_every_protective_stop_its_kill_switch_refuses(
    mock_tc_cls,
    tmp_path,
):
    from src.execution.broker import AlpacaBroker

    mock_tc_cls.return_value = MagicMock()
    flag = tmp_path / "KILL_SWITCH"
    flag.touch()
    broker = AlpacaBroker(
        api_key="test",
        secret_key="test",
        paper=True,
        kill_switch_path=str(flag),
    )
    db = _db(tmp_path)
    p = build_pipeline(broker=broker, db=db)
    p._wire_protective_stop_block_recorder()
    result = broker._submit_stop_limit_order(
        symbol="AAA",
        qty=5,
        stop_price=90.0,
        side="sell",
    )
    # The decision is unchanged: still halted, still no order id.
    assert result["status"] == "kill_switch_halted" and result["id"] is None
    assert "kill switch" in result["detail"]
    broker.client.submit_order.assert_not_called()
    rows = _rows(db, PROTECTIVE_STOP_BLOCKED_KIND)
    assert len(rows) == 1
    assert rows[0]["symbol"] == "AAA" and rows[0]["qty"] == 5
    assert rows[0]["stop_price"] == 90.0 and rows[0]["side"] == "sell"


def test_the_pipeline_wires_the_recorder_at_construction():
    """The broker has no database; without this call in `__init__` nothing
    is recorded in production even though every unit above passes."""
    import inspect

    assert "self._wire_protective_stop_block_recorder()" in inspect.getsource(
        TradingPipeline.__init__,
    )


def test_a_failing_recorder_never_changes_the_refusal(tmp_path):
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
    broker.protective_stop_block_recorder = MagicMock(side_effect=RuntimeError("x"))
    result = broker._submit_stop_limit_order(
        symbol="AAA",
        qty=5,
        stop_price=90.0,
        side="sell",
    )
    assert result["status"] == "kill_switch_halted"
    broker.protective_stop_block_recorder.assert_called_once()


def test_the_out_of_session_sweep_records_its_refusals_too(tmp_path):
    from src.coverage_watchdog import CoverageGap, replace_missing_stops

    db = _db(tmp_path)
    broker = _repair_broker(None)
    resting = [{"id": "s1", "qty": 4.0, "stop_price": 158.0}]
    broker.snapshot_protective_stops.return_value = (True, resting)
    outcomes = replace_missing_stops(
        broker,
        [CoverageGap("NET", 4.4, 4.0, 0.4, 0.0)],
        last_buy=lambda s, action="BUY": {"stop_loss": 158.75},
        db=db,
    )
    assert [o.placed for o in outcomes] == [False]
    rows = _rows(db, STOP_REPAIR_REFUSAL_KIND)
    assert len(rows) == 1
    assert rows[0]["caller"] == "coverage_sweep"
    assert rows[0]["resting_stops"] == resting
    assert rows[0]["held_qty"] == 4.4 and rows[0]["covered_qty"] == 4.0
