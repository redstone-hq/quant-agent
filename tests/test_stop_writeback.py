"""Write-back of trades.stop_loss when a stop moves, and broker reconcile.

Item 71: the opening row's stop_loss was written once at entry and never
again. After replace/trail/repair it must match the new level; reconcile
must surface a deliberate mismatch. No invented prices.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from src.execution.stop_records import (
    accepted_stop_order,
    reconcile_recorded_stop_levels,
    recorded_initial_stop,
    replace_stop_and_record,
    write_back_live_protective_stops,
    write_back_stop_loss,
    StopLevelMismatch,
)
from src.execution.stop_repair import repair_stop_coverage
from src.storage.db import Database
from tests.pipeline_factory import build_pipeline


@pytest.fixture
def db(tmp_path):
    database = Database(str(tmp_path / "stops.db"))
    database.initialize()
    yield database
    database.close()


def _open_long(db, symbol="AAPL", stop=140.0, *, fill_status="filled"):
    db.insert_trade(
        symbol=symbol,
        action="BUY",
        qty=10,
        price=150.0,
        reasoning="entry",
        run_id="r1",
        stop_loss=stop,
        fill_status=fill_status,
    )
    return db.get_symbol_last_buy(symbol)


def _open_short(db, symbol="TSLA", stop=220.0, *, fill_status="filled"):
    db.insert_trade(
        symbol=symbol,
        action="SHORT",
        qty=4,
        price=200.0,
        reasoning="entry",
        run_id="r1",
        stop_loss=stop,
        fill_status=fill_status,
    )
    return db.get_symbol_last_buy(symbol, action="SHORT")


def test_insert_trade_pins_initial_stop_loss_to_the_entry_stop(db):
    row = _open_long(db, stop=137.53)
    assert row["stop_loss"] == pytest.approx(137.53)
    assert row["initial_stop_loss"] == pytest.approx(137.53)


def test_write_back_updates_stop_loss_and_freezes_the_entry_stop(db):
    _open_long(db, stop=140.0)
    assert write_back_stop_loss(db, "AAPL", 148.25) is True
    row = db.get_symbol_last_buy("AAPL")
    assert row["stop_loss"] == pytest.approx(148.25)
    assert row["initial_stop_loss"] == pytest.approx(140.0)
    assert recorded_initial_stop(row) == pytest.approx(140.0)


def test_write_back_on_a_short_updates_the_short_row_not_a_buy(db):
    _open_long(db, symbol="TSLA", stop=180.0)
    _open_short(db, symbol="TSLA", stop=220.0)
    assert write_back_stop_loss(db, "TSLA", 210.0) is True
    short = db.get_symbol_last_buy("TSLA", action="SHORT")
    buy = db.get_symbol_last_buy("TSLA")
    assert short["stop_loss"] == pytest.approx(210.0)
    assert short["initial_stop_loss"] == pytest.approx(220.0)
    assert buy["stop_loss"] == pytest.approx(180.0)


def test_replace_stop_and_record_writes_back_the_new_level(db):
    _open_long(db, stop=140.0)
    broker = MagicMock()
    broker.replace_stop_loss.return_value = {"id": "stop-2"}
    order = replace_stop_and_record(broker, db, "AAPL", 145.0)
    assert order["id"] == "stop-2"
    broker.replace_stop_loss.assert_called_once_with("AAPL", 145.0)
    row = db.get_symbol_last_buy("AAPL")
    assert row["stop_loss"] == pytest.approx(145.0)
    assert row["initial_stop_loss"] == pytest.approx(140.0)


def test_failed_replace_does_not_write_back(db):
    _open_long(db, stop=140.0)
    broker = MagicMock()
    broker.replace_stop_loss.return_value = None
    assert replace_stop_and_record(broker, db, "AAPL", 145.0) is None
    row = db.get_symbol_last_buy("AAPL")
    assert row["stop_loss"] == pytest.approx(140.0)


def test_deterministic_trail_write_back_matches_the_new_level(db):
    """After a trail, the opening row's stop_loss is the new level."""
    from src.pipeline import TradingPipeline
    from src.models import Position
    from src.risk.trailing import TrailProposal

    _open_long(db, symbol="AAA", stop=95.0)
    db.conn.execute(
        "UPDATE trades SET timestamp = '2026-08-01 14:00:00', setup_type = 'breakout' WHERE symbol = 'AAA'",
    )
    db.conn.commit()
    pipeline = build_pipeline(db=db, broker=MagicMock(), market=MagicMock())
    pipeline.broker.get_current_stop_price.return_value = 95.0
    pipeline.broker.replace_stop_loss.return_value = {"id": "o1"}
    pipeline._atr_for_symbol = MagicMock(return_value=2.0)
    pipeline.market.get_ohlcv.return_value = []

    pos = Position(
        symbol="AAA",
        qty=10,
        avg_entry=100.0,
        current_price=125.0,
        market_value=1250.0,
        unrealized_pnl=250.0,
        sector="Technology",
    )
    proposal = TrailProposal(
        symbol="AAA",
        new_stop=110.0,
        previous_stop=95.0,
        source="structure",
        reason="test trail",
    )
    # The caller reads `evaluate_trailing_stop` (proposal + why-code) since
    # the trail-state record landed; `compute_trailing_stop` is its view.
    from src.risk.trailing import TRAIL_CODE_TRAILED, TrailEvaluation

    with patch(
        "src.risk.trailing.evaluate_trailing_stop",
        return_value=TrailEvaluation(proposal, TRAIL_CODE_TRAILED),
    ):
        orders = pipeline._apply_deterministic_trails([pos], run_id="r1")
    assert len(orders) == 1
    row = db.get_symbol_last_buy("AAA")
    assert row["stop_loss"] == pytest.approx(110.0)
    assert row["initial_stop_loss"] == pytest.approx(95.0)


def test_repair_write_back_long_matches_the_placed_level(db):
    _open_long(db, symbol="VST", stop=158.75)
    broker = MagicMock()
    broker.get_latest_price.return_value = 165.0
    broker.STOP_LIMIT_BUFFER_PCT = 0.03
    broker._submit_protective_stop_retrying.return_value = {"id": "stop-1"}
    placed = repair_stop_coverage(
        broker=broker,
        last_buy=lambda s, action="BUY": db.get_symbol_last_buy(
            s,
            include_in_flight=True,
            action=action,
        ),
        symbol="VST",
        uncovered_qty=31.0,
        is_short=False,
        db=db,
    )
    assert placed is True
    kwargs = broker._submit_protective_stop_retrying.call_args.kwargs
    assert kwargs["stop_price"] == pytest.approx(158.75)
    assert kwargs["side"] == "sell"
    row = db.get_symbol_last_buy("VST")
    assert row["stop_loss"] == pytest.approx(158.75)


def test_repair_write_back_short_matches_the_placed_level(db):
    _open_short(db, symbol="TSLA", stop=220.0)
    broker = MagicMock()
    broker.get_latest_price.return_value = 200.0
    broker.STOP_LIMIT_BUFFER_PCT = 0.03
    broker._submit_protective_stop_retrying.return_value = {"id": "buy-stop"}
    placed = repair_stop_coverage(
        broker=broker,
        last_buy=lambda s, action="BUY": db.get_symbol_last_buy(
            s,
            include_in_flight=True,
            action=action,
        ),
        symbol="TSLA",
        uncovered_qty=4.0,
        is_short=True,
        db=db,
    )
    assert placed is True
    kwargs = broker._submit_protective_stop_retrying.call_args.kwargs
    assert kwargs["side"] == "buy"
    assert kwargs["stop_price"] == pytest.approx(220.0)
    row = db.get_symbol_last_buy("TSLA", action="SHORT")
    assert row["stop_loss"] == pytest.approx(220.0)


def test_repair_after_a_trail_restores_the_trailed_level_not_the_entry(db):
    _open_long(db, symbol="VST", stop=158.75)
    write_back_stop_loss(db, "VST", 162.40)
    broker = MagicMock()
    broker.get_latest_price.return_value = 170.0
    broker.STOP_LIMIT_BUFFER_PCT = 0.03
    broker._submit_protective_stop_retrying.return_value = {"id": "stop-2"}
    placed = repair_stop_coverage(
        broker=broker,
        last_buy=lambda s, action="BUY": db.get_symbol_last_buy(
            s,
            include_in_flight=True,
            action=action,
        ),
        symbol="VST",
        uncovered_qty=10.0,
        is_short=False,
        db=db,
    )
    assert placed is True
    assert broker._submit_protective_stop_retrying.call_args.kwargs["stop_price"] == (pytest.approx(162.40))
    row = db.get_symbol_last_buy("VST")
    assert row["stop_loss"] == pytest.approx(162.40)
    assert row["initial_stop_loss"] == pytest.approx(158.75)


def test_reconcile_surfaces_a_deliberate_long_mismatch(db):
    _open_long(db, symbol="V", stop=374.27)
    broker = MagicMock()
    broker.get_current_stop_price.return_value = 362.58
    position = SimpleNamespace(symbol="V", qty=1.0)
    mismatches = reconcile_recorded_stop_levels(
        db=None,
        broker=broker,
        last_buy=lambda s, action="BUY": db.get_symbol_last_buy(
            s,
            include_in_flight=True,
            action=action,
        ),
        positions=[position],
    )
    assert len(mismatches) == 1
    assert mismatches[0].symbol == "V"
    assert mismatches[0].recorded == pytest.approx(374.27)
    assert mismatches[0].live == pytest.approx(362.58)
    assert mismatches[0].is_short is False
    # Report-only: the archive is untouched.
    row = db.get_symbol_last_buy("V")
    assert row["stop_loss"] == pytest.approx(374.27)


def test_reconcile_surfaces_a_deliberate_short_mismatch(db):
    _open_short(db, symbol="TSLA", stop=220.0)
    broker = MagicMock()
    broker.get_current_stop_price.return_value = 215.0
    position = SimpleNamespace(symbol="TSLA", qty=-4.0)
    mismatches = reconcile_recorded_stop_levels(
        db=None,
        broker=broker,
        last_buy=lambda s, action="BUY": db.get_symbol_last_buy(
            s,
            include_in_flight=True,
            action=action,
        ),
        positions=[position],
    )
    assert len(mismatches) == 1
    assert mismatches[0].symbol == "TSLA"
    assert mismatches[0].is_short is True
    assert mismatches[0].recorded == pytest.approx(220.0)
    assert mismatches[0].live == pytest.approx(215.0)


def test_reconcile_is_quiet_when_archive_matches_broker(db):
    _open_long(db, symbol="AAPL", stop=148.25)
    broker = MagicMock()
    broker.get_current_stop_price.return_value = 148.25
    mismatches = reconcile_recorded_stop_levels(
        db=None,
        broker=broker,
        last_buy=lambda s, action="BUY": db.get_symbol_last_buy(
            s,
            include_in_flight=True,
            action=action,
        ),
        positions=[SimpleNamespace(symbol="AAPL", qty=10.0)],
    )
    assert mismatches == []


def test_reconcile_skips_a_missing_live_stop_that_coverage_owns(db):
    _open_long(db, symbol="AAPL", stop=140.0)
    broker = MagicMock()
    broker.get_current_stop_price.return_value = None
    mismatches = reconcile_recorded_stop_levels(
        db=None,
        broker=broker,
        last_buy=lambda s, action="BUY": db.get_symbol_last_buy(
            s,
            include_in_flight=True,
            action=action,
        ),
        positions=[SimpleNamespace(symbol="AAPL", qty=10.0)],
    )
    assert mismatches == []


def test_scale_in_rearm_writes_back_the_rearmed_level(db):
    from src.execution.scale_in import rearm_full_position_stop

    _open_long(db, symbol="ORCL", stop=137.53)
    broker = MagicMock()
    broker.STOP_LIMIT_BUFFER_PCT = 0.03
    broker._submit_protective_stop_retrying.return_value = {"id": "rearm", "uncovered_qty": 0}
    placed = rearm_full_position_stop(
        broker,
        symbol="ORCL",
        qty=12.0,
        stop_price=142.00,
        db=db,
    )
    assert placed is not None
    row = db.get_symbol_last_buy("ORCL")
    assert row["stop_loss"] == pytest.approx(142.00)
    assert row["initial_stop_loss"] == pytest.approx(137.53)


@patch("src.notifier.send_owner_alert", return_value=True)
def test_report_mismatch_pages_the_owner_and_does_not_write(_alert, db):
    from src.execution.stop_records import report_stop_level_mismatches

    _open_long(db, symbol="DIS", stop=105.80)
    report_stop_level_mismatches(
        [
            StopLevelMismatch(
                symbol="DIS",
                recorded=105.80,
                live=101.44,
                is_short=False,
                reason="recorded BUY stop_loss $105.8000 != broker stop $101.4400",
            ),
        ]
    )
    _alert.assert_called_once()
    assert db.get_symbol_last_buy("DIS")["stop_loss"] == pytest.approx(105.80)


@patch("src.notifier.send_owner_alert", return_value=True)
def test_unfixed_mismatch_keeps_paging(_alert, db):
    """Mute-without-fix was the 2026-09-16 COP/EQNR defect. Remaining
    mismatches page every time until the record is written back."""
    from src.execution.stop_records import report_stop_level_mismatches

    mismatch = StopLevelMismatch(
        symbol="COP",
        recorded=125.21,
        live=131.76,
        is_short=False,
        reason="recorded BUY stop_loss $125.2100 != broker stop $131.7600",
    )
    report_stop_level_mismatches([mismatch])
    report_stop_level_mismatches([mismatch])
    assert _alert.call_count == 2


def test_live_protective_stop_write_back_fixes_cop_without_inventing(db):
    """Reconcile found COP's archive behind the desk's own live stop.
    Write that live price back. Do not invent a third number."""
    _open_long(db, symbol="COP", stop=125.21)
    remaining = write_back_live_protective_stops(
        db,
        [
            StopLevelMismatch(
                symbol="COP",
                recorded=125.21,
                live=131.76,
                is_short=False,
                reason="recorded BUY stop_loss $125.2100 != broker stop $131.7600",
            ),
        ],
    )
    assert remaining == []
    assert db.get_symbol_last_buy("COP")["stop_loss"] == pytest.approx(131.76)
    assert db.get_symbol_last_buy("COP")["initial_stop_loss"] == pytest.approx(125.21)


def test_write_back_does_not_invent_a_level_when_live_is_missing(db):
    _open_long(db, symbol="EQNR", stop=70.10)
    remaining = write_back_live_protective_stops(
        db,
        [
            StopLevelMismatch(
                symbol="EQNR",
                recorded=70.10,
                live=None,
                is_short=False,
                reason="no live stop",
            ),
        ],
    )
    assert remaining and remaining[0].symbol == "EQNR"
    assert db.get_symbol_last_buy("EQNR")["stop_loss"] == pytest.approx(70.10)


def test_accepted_stop_order_rejects_kill_switch_and_missing_id():
    assert accepted_stop_order({"id": "stop-1"}) is True
    assert accepted_stop_order({"id": None, "status": "kill_switch_halted"}) is False
    assert accepted_stop_order({"id": "None", "status": "kill_switch_halted"}) is False
    assert accepted_stop_order(None) is False
    assert accepted_stop_order(MagicMock()) is False


def test_kill_switch_replace_does_not_write_back(db):
    _open_long(db, stop=140.0)
    broker = MagicMock()
    broker.replace_stop_loss.return_value = {
        "id": None,
        "status": "kill_switch_halted",
    }
    order = replace_stop_and_record(broker, db, "AAPL", 145.0)
    assert order["status"] == "kill_switch_halted"
    assert db.get_symbol_last_buy("AAPL")["stop_loss"] == pytest.approx(140.0)


def test_kill_switch_repair_does_not_write_back(db):
    _open_long(db, symbol="VST", stop=158.75)
    broker = MagicMock()
    broker.get_latest_price.return_value = 165.0
    broker.STOP_LIMIT_BUFFER_PCT = 0.03
    broker._submit_protective_stop_retrying.return_value = {
        "id": None,
        "status": "kill_switch_halted",
    }
    placed = repair_stop_coverage(
        broker=broker,
        last_buy=lambda s, action="BUY": db.get_symbol_last_buy(
            s,
            include_in_flight=True,
            action=action,
        ),
        symbol="VST",
        uncovered_qty=31.0,
        is_short=False,
        db=db,
    )
    assert placed is False
    assert db.get_symbol_last_buy("VST")["stop_loss"] == pytest.approx(158.75)


def test_kill_switch_rearm_does_not_write_back(db):
    from src.execution.scale_in import rearm_full_position_stop

    _open_long(db, symbol="ORCL", stop=137.53)
    broker = MagicMock()
    broker.STOP_LIMIT_BUFFER_PCT = 0.03
    broker._submit_protective_stop_retrying.return_value = {
        "id": None,
        "status": "kill_switch_halted",
    }
    result = rearm_full_position_stop(
        broker,
        symbol="ORCL",
        qty=12.0,
        stop_price=142.00,
        db=db,
    )
    assert result["status"] == "kill_switch_halted"
    assert db.get_symbol_last_buy("ORCL")["stop_loss"] == pytest.approx(137.53)


def test_write_back_action_short_when_a_newer_buy_exists(db):
    _open_short(db, symbol="TSLA", stop=220.0)
    _open_long(db, symbol="TSLA", stop=180.0)
    assert write_back_stop_loss(db, "TSLA", 210.0, is_short=True) is True
    short = db.get_symbol_last_buy("TSLA", action="SHORT")
    buy = db.get_symbol_last_buy("TSLA")
    assert short["stop_loss"] == pytest.approx(210.0)
    assert buy["stop_loss"] == pytest.approx(180.0)


def test_reconcile_treats_a_sub_dollar_tick_as_a_match(db):
    db.insert_trade(
        symbol="PENNY",
        action="BUY",
        qty=100,
        price=0.80,
        reasoning="entry",
        run_id="r1",
        stop_loss=0.50,
        fill_status="filled",
    )
    broker = MagicMock()
    broker.get_current_stop_price.return_value = 0.50005
    mismatches = reconcile_recorded_stop_levels(
        db=None,
        broker=broker,
        last_buy=lambda s, action="BUY": db.get_symbol_last_buy(
            s,
            include_in_flight=True,
            action=action,
        ),
        positions=[SimpleNamespace(symbol="PENNY", qty=100.0)],
    )
    assert mismatches == []


def test_reconcile_surfaces_a_sub_dollar_mismatch_beyond_a_tick(db):
    db.insert_trade(
        symbol="PENNY",
        action="BUY",
        qty=100,
        price=0.80,
        reasoning="entry",
        run_id="r1",
        stop_loss=0.50,
        fill_status="filled",
    )
    broker = MagicMock()
    broker.get_current_stop_price.return_value = 0.501
    mismatches = reconcile_recorded_stop_levels(
        db=None,
        broker=broker,
        last_buy=lambda s, action="BUY": db.get_symbol_last_buy(
            s,
            include_in_flight=True,
            action=action,
        ),
        positions=[SimpleNamespace(symbol="PENNY", qty=100.0)],
    )
    assert len(mismatches) == 1
    assert mismatches[0].symbol == "PENNY"


def test_replace_survives_a_mock_get_positions_and_writes_back(db):
    """MagicMock get_positions is not a list; direction stays unknown."""
    _open_long(db, stop=140.0)
    broker = MagicMock()
    broker.replace_stop_loss.return_value = {"id": "stop-2"}
    # default MagicMock get_positions() is iterable-but-not-a-list
    order = replace_stop_and_record(broker, db, "AAPL", 145.0)
    assert order["id"] == "stop-2"
    assert db.get_symbol_last_buy("AAPL")["stop_loss"] == pytest.approx(145.0)


def test_reprotect_idempotent_skip_still_writes_back(db):
    from src.pipeline import TradingPipeline

    _open_long(db, symbol="NVDA", stop=85.0)
    pipeline = build_pipeline(db=db, broker=MagicMock(), _format_qty=lambda q: str(q))
    existing = MagicMock()
    existing.stop_price = "90.00"
    # Item 199: the skip is now decided by IDENTITY and live status, not by
    # price alone. This stands for a stop a PREVIOUS attempt placed, so it
    # carries an id this run did NOT cancel and an active status. An open
    # stop whose id IS in `cancelled` is the 2026-09-30 naked-position
    # incident and must no longer satisfy the check.
    existing.id = "s-previous-attempt"
    existing.status = "new"
    pipeline.broker._list_open_sell_stop_orders.return_value = [existing]
    cancelled = [{"id": "s1", "qty": 10, "stop_price": 90.0, "limit_price": 88.0}]
    ok = pipeline._reprotect_residual_after_partial_sell("NVDA", 10.0, cancelled)
    assert ok is True
    pipeline.broker._submit_stop_limit_order.assert_not_called()
    row = db.get_symbol_last_buy("NVDA")
    assert row["stop_loss"] == pytest.approx(90.0)
    assert row["initial_stop_loss"] == pytest.approx(85.0)


def test_reprotect_kill_switch_does_not_write_back(db):
    from src.pipeline import TradingPipeline

    _open_long(db, symbol="NVDA", stop=85.0)
    pipeline = build_pipeline(db=db, broker=MagicMock(), _format_qty=lambda q: str(q))
    pipeline.broker._list_open_sell_stop_orders.return_value = []
    pipeline.broker._submit_protective_stop_retrying.return_value = {
        "id": None,
        "status": "kill_switch_halted",
    }
    cancelled = [{"id": "s1", "qty": 10, "stop_price": 90.0, "limit_price": 88.0}]
    ok = pipeline._reprotect_residual_after_partial_sell("NVDA", 10.0, cancelled)
    assert ok is False
    assert db.get_symbol_last_buy("NVDA")["stop_loss"] == pytest.approx(85.0)


def test_repair_partial_with_an_accepted_id_still_writes_back(db):
    _open_long(db, symbol="VST", stop=158.75)
    write_back_stop_loss(db, "VST", 162.40)
    broker = MagicMock()
    broker.get_latest_price.return_value = 170.0
    broker.STOP_LIMIT_BUFFER_PCT = 0.03
    broker._submit_protective_stop_retrying.return_value = {
        "id": "stop-partial",
        "uncovered_qty": 0.3456,
    }
    placed = repair_stop_coverage(
        broker=broker,
        last_buy=lambda s, action="BUY": db.get_symbol_last_buy(
            s,
            include_in_flight=True,
            action=action,
        ),
        symbol="VST",
        uncovered_qty=10.0,
        is_short=False,
        db=db,
    )
    assert placed is False
    row = db.get_symbol_last_buy("VST")
    assert row["stop_loss"] == pytest.approx(162.40)
    assert row["initial_stop_loss"] == pytest.approx(158.75)


def test_write_back_updates_every_open_row_of_the_same_position(db):
    _open_long(db, symbol="ORCL", stop=95.0)
    db.insert_trade(
        symbol="ORCL",
        action="BUY",
        qty=2,
        price=110.0,
        reasoning="scale-in",
        run_id="r1",
        stop_loss=98.0,
        fill_status="filled",
    )
    assert write_back_stop_loss(db, "ORCL", 101.5, is_short=False) is True
    rows = db.conn.execute(
        "SELECT stop_loss, initial_stop_loss, qty FROM trades WHERE symbol = 'ORCL' AND action = 'BUY' ORDER BY id",
    ).fetchall()
    assert len(rows) == 2
    assert rows[0]["stop_loss"] == pytest.approx(101.5)
    assert rows[0]["initial_stop_loss"] == pytest.approx(95.0)
    assert rows[1]["stop_loss"] == pytest.approx(101.5)
    assert rows[1]["initial_stop_loss"] == pytest.approx(98.0)


def test_reconcile_reports_a_full_tick_difference(db):
    _open_long(db, symbol="AAPL", stop=148.25)
    broker = MagicMock()
    broker.get_current_stop_price.return_value = 148.26
    mismatches = reconcile_recorded_stop_levels(
        db=None,
        broker=broker,
        last_buy=lambda s, action="BUY": db.get_symbol_last_buy(
            s,
            include_in_flight=True,
            action=action,
        ),
        positions=[SimpleNamespace(symbol="AAPL", qty=10.0)],
    )
    assert len(mismatches) == 1


def test_position_history_reads_the_frozen_entry_stop_not_the_live_one(db):
    from src.models import Position
    from src.pipeline import TradingPipeline

    _open_long(db, symbol="V", stop=374.27)
    write_back_stop_loss(db, "V", 362.58)
    pipeline = build_pipeline(db=db, tech_store=MagicMock())
    pipeline.tech_store.get_history.return_value = []
    pos = Position(
        symbol="V",
        qty=1,
        avg_entry=380.0,
        current_price=370.0,
        market_value=370.0,
        unrealized_pnl=-10.0,
        sector="Cyclical",
    )
    hist = pipeline._build_position_history([pos])
    assert hist["V"]["stop_loss"] == pytest.approx(374.27)
