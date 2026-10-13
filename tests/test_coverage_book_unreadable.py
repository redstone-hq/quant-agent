"""The stop-coverage sweep must never report all-clear when it could not
read the book.

`TradingPipeline._reconcile_stop_coverage` returns `list[dict]` and every
caller reads `[]` as "no gaps — every position is covered". Its first act
used to be a bare `get_positions()` whose `except` returned `[]`, so a
broker read failure turned the desk's only naked-position audit into a
clean bill of health in the morning, evening, position-review and
intraday sessions at once.

Owner ruling 2026-10-02: retry, ask a different way, then ACT — a
duplicate stop the broker refuses costs less than a naked position left
alone. These tests pin the escalation and pin that a SUCCESSFUL read is
untouched, fractional classification included.
"""

from unittest.mock import MagicMock, patch

from tests.pipeline_factory import build_pipeline


def _pipeline(*, positions_side_effect=None, ledger=None):
    broker = MagicMock()
    if positions_side_effect is not None:
        broker.get_positions.side_effect = positions_side_effect
    broker.snapshot_protective_stops.return_value = (True, [])
    broker.get_latest_price.return_value = 100.0
    broker._submit_protective_stop_retrying.return_value = {"id": "s-1"}
    db = MagicMock()
    db.get_pending_protection_restores.return_value = []
    db.get_symbols_with_open_ledger_qty.return_value = {"AAA": 12.0} if ledger is None else ledger
    db.get_symbol_last_buy.return_value = {"stop_loss": 90.0}
    p = build_pipeline(broker=broker, db=db)
    p.broker, p.db = broker, db
    p.cash_sweeper = None
    return p


def test_unreadable_book_is_not_all_clear_and_repairs_known_holdings():
    p = _pipeline(positions_side_effect=RuntimeError("broker 503"))
    with (
        patch("time.sleep"),
        patch(
            "src.pipeline_protection.ProtectionMixin._alert_owner_unreadable_stop",
        ) as alert,
    ):
        gaps = p._reconcile_stop_coverage()
    # NOT all-clear.
    assert gaps, "a failed positions read reported no gaps — reads as all-clear"
    assert all(g["coverage"] == "unreadable" for g in gaps)
    assert any(g.get("book_unreadable") and g["symbol"] == "(whole book)" for g in gaps)
    # The unknown is never priced: coverage is None, not zero.
    assert all(g["covered_qty"] is None for g in gaps)
    # Every holding the desk knows about went through the repair path.
    held = [g for g in gaps if g["symbol"] == "AAA"]
    assert len(held) == 1 and held[0]["repaired"] is True
    kwargs = p.broker._submit_protective_stop_retrying.call_args.kwargs
    assert kwargs["symbol"] == "AAA" and kwargs["stop_price"] == 90.0
    # The owner is told it is unknown, not clean.
    assert alert.called


def test_retry_succeeds_and_no_duplicate_repair_is_attempted():
    pos = MagicMock(symbol="AAA", qty=12.0)
    p = _pipeline(positions_side_effect=[RuntimeError("timeout"), [pos]])
    with patch("time.sleep"), patch("src.pipeline_protection._market_is_open_now", return_value=True):
        gaps = p._reconcile_stop_coverage()
    assert p.broker.get_positions.call_count == 2
    # One measured gap from the retried read; no book-level unknown row.
    assert all(not g.get("book_unreadable") for g in gaps)
    assert [g["coverage"] for g in gaps] == ["none"]
    # Repaired exactly once — the fallback path must not also have fired.
    assert p.broker._submit_protective_stop_retrying.call_count == 1


def test_successful_read_keeps_fractional_overnight_classification():
    pos = MagicMock(symbol="AAA", qty=12.4, current_price=100.0)
    p = _pipeline(positions_side_effect=[[pos]])
    p.broker.snapshot_protective_stops.return_value = (
        True,
        [{"qty": 12.0, "stop_price": 90.0}],
    )
    with (
        patch("src.pipeline_protection._market_is_open_now", return_value=False),
        patch("src.coverage_watchdog.session_awaiting_print_symbols", return_value=set()),
    ):
        gaps = p._reconcile_stop_coverage()
    assert len(gaps) == 1
    assert gaps[0]["coverage"] == "fractional_overnight"
    assert gaps[0]["repaired"] is False
    assert p.broker._submit_protective_stop_retrying.call_count == 0
