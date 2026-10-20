"""An unknown is not a safe known (docs/OUTCOME.md): two paths that read a failure as all-clear.

1. A failed entry-order cancel used to come back as "0 entry orders", so the
   protected sale went on while the entry may still rest. Now the cancel
   raises and the sale is PUT OFF with the existing reason `cancel_read_failed`.
2. A stop-coverage audit that raised used to leave `[]`, which every reader
   shows as "every position covered". Now it reports the unreadable-book shape.
"""

from __future__ import annotations

from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import MagicMock

from src.models import Position
from tests.pipeline_factory import build_pipeline

STOP = {"id": "stop-1", "qty": 100.0, "stop_price": 90.0}


def _pos(qty):
    return Position(
        symbol="NVDA",
        qty=qty,
        avg_entry=100.0,
        current_price=100.0,
        market_value=100.0 * qty,
        unrealized_pnl=0.0,
        sector="Tech",
        qty_available=qty,
    )


def _seller():
    pipe = build_pipeline(db=MagicMock(), broker=MagicMock())
    submits: list[float] = []
    pipe.db.insert_pending_protection_restore.return_value = 7
    pipe.broker.snapshot_protective_stops.return_value = (True, [dict(STOP)])
    pipe.broker.cancel_snapshotted_stops.return_value = MagicMock(cleared=True)
    pipe.broker.wait_for_order_terminal.return_value = "canceled"
    pipe.broker.client.get_order_by_id.return_value = SimpleNamespace(filled_qty="0")
    pipe.broker.get_positions.return_value = [_pos(100.0)]

    def _submit(**kw):
        submits.append(kw["qty"])
        return {"id": "ord-1", "status": "accepted"}

    pipe.broker.submit_order.side_effect = _submit
    pipe._order_accepted = MagicMock(return_value=True)
    pipe._alert_owner_exit_declined = MagicMock()
    return pipe, submits


def _sell(pipe):
    return pipe._submit_protected_sell(
        symbol="NVDA",
        qty=100.0,
        limit_price=99.0,
        reference_price=100.0,
        position_qty_before_sell=100.0,
        label="SELL",
    )


# --- 1. the entry-order cancel ----------------------------------------------


def test_a_failed_entry_cancel_puts_the_sale_off_and_sends_nothing():
    pipe, submits = _seller()
    pipe.broker.cancel_open_entry_orders.side_effect = RuntimeError("REST 503")
    assert _sell(pipe) is None
    assert pipe._last_stop_clear_refusal == "cancel_read_failed"
    assert submits == []
    # Put off BEFORE any stop was touched: nothing to put back.
    pipe.broker.cancel_snapshotted_stops.assert_not_called()
    pipe._alert_owner_exit_declined.assert_called_once()
    assert "cancel_read_failed" in pipe._alert_owner_exit_declined.call_args.kwargs["why"]


def test_a_clean_cancel_of_zero_entries_lets_the_sale_go():
    pipe, submits = _seller()
    pipe.broker.cancel_open_entry_orders.return_value = 0
    assert _sell(pipe) is not None
    assert submits == [100.0]
    assert pipe._last_stop_clear_refusal == ""


def test_the_desk_raises_a_failed_cancel_instead_of_reporting_zero():
    from src.execution.broker_parts.order_desk import OrderDesk

    desk = SimpleNamespace(client=MagicMock())
    desk.client.get_orders.side_effect = RuntimeError("REST 503")
    raised = None
    try:
        OrderDesk.cancel_open_entry_orders(desk, symbol="NVDA")
    except RuntimeError as exc:
        raised = exc
    assert raised is not None, "a failed cancel came back as a count instead of raising"


# --- 2. the stop-coverage audit ---------------------------------------------


def _safety():
    pipe = build_pipeline(db=MagicMock(), broker=MagicMock())
    pipe.db.get_pending_protection_restores.return_value = []
    pipe.db.get_symbols_with_open_ledger_qty.return_value = {"AAA": 12.0}
    pipe.cash_sweeper = None
    pipe._intraday_scan_process_lock = lambda: nullcontext(True)
    for name in (
        "_blocking_owner_session",
        "_drain_pending_protection_restores",
        "_drain_pending_repegs",
        "_reconcile_orphan_pending_submits",
        "_reconcile_fills",
        "_reconcile_stop_out_fills",
        "_surface_reconcile_outcomes",
    ):
        setattr(pipe, name, MagicMock(return_value=None))
    return pipe


def test_an_audit_that_raises_reports_the_book_unreadable_not_covered():
    pipe = _safety()
    pipe._reconcile_stop_coverage = MagicMock(side_effect=RuntimeError("audit blew up"))
    gaps, deferred = pipe._run_intra_safety_preamble("run-1")
    assert deferred == ""
    assert gaps, "an audit that raised reported no gaps — reads as all-clear"
    assert all(g["coverage"] == "unreadable" and g["covered_qty"] is None for g in gaps)
    assert any(g.get("book_unreadable") and g["symbol"] == "(whole book)" for g in gaps)
    # Every holding the desk's own ledger knows is reported unverified, not covered.
    assert [g["symbol"] for g in gaps if g["symbol"] != "(whole book)"] == ["AAA"]


def test_a_clean_audit_still_reports_no_gaps():
    pipe = _safety()
    pipe._reconcile_stop_coverage = MagicMock(return_value=[])
    gaps, deferred = pipe._run_intra_safety_preamble("run-1")
    assert (gaps, deferred) == ([], "")
