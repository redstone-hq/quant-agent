"""R for a closed position is realised P&L / sum over entry lots of
(lot qty x |lot price - that lot's own initial stop|); any bad lot -> None."""

import pytest

from src.quantities import r_multiple, summarize_closed_position


def _row(action, qty, price, **kw):
    row = {
        "action": action,
        "qty": qty,
        "price": price,
        "symbol": "AAPL",
        "fill_qty": None,
        "fill_price": None,
        "fill_status": "filled",
        "stop_loss": 0,
        "decision_id": None,
        "position_id": "pos-1",
        "timestamp": "2026-01-01 10:00:00",
    }
    row.update(kw)
    return row


def test_single_lot_long_matches_r_multiple():
    c = summarize_closed_position([_row("BUY", 10, 100.0, stop_loss=95.0), _row("SELL", 10, 112.0)])
    assert c.r_multiple == r_multiple(112.0, 100.0, 95.0, 10)
    assert c.entry_risk_dollars == pytest.approx(50.0)


def test_single_lot_short_matches_r_multiple():
    c = summarize_closed_position([_row("SHORT", 10, 100.0, stop_loss=105.0), _row("COVER", 10, 90.0)])
    assert c.r_multiple == r_multiple(90.0, 100.0, 105.0, -10)


def test_two_long_lots_use_each_lots_own_stop():
    c = summarize_closed_position(
        [_row("BUY", 10, 100.0, stop_loss=90.0), _row("BUY", 10, 110.0, stop_loss=105.0), _row("SELL", 20, 130.0)]
    )
    assert c.entry_risk_dollars == pytest.approx(10 * 10 + 10 * 5)
    assert c.r_multiple == round(20 * 25 / 150, 2)  # first-lot stop would give a different R


def test_two_short_lots_use_each_lots_own_stop():
    c = summarize_closed_position(
        [_row("SHORT", 10, 100.0, stop_loss=110.0), _row("SHORT", 10, 90.0, stop_loss=95.0), _row("COVER", 20, 80.0)]
    )
    assert c.entry_risk_dollars == pytest.approx(10 * 10 + 10 * 5)
    assert c.r_multiple == round(20 * 15 / 150, 2)


def test_lot_without_stop_gives_none():
    c = summarize_closed_position(
        [_row("BUY", 10, 100.0, stop_loss=90.0), _row("BUY", 10, 110.0, stop_loss=0), _row("SELL", 20, 130.0)]
    )
    assert c is not None and c.r_multiple is None and c.entry_risk_dollars is None


def test_wrong_side_stop_gives_none():
    c = summarize_closed_position(
        [_row("BUY", 10, 100.0, stop_loss=90.0), _row("BUY", 10, 110.0, stop_loss=115.0), _row("SELL", 20, 130.0)]
    )
    assert c.r_multiple is None
    s = summarize_closed_position([_row("SHORT", 10, 100.0, stop_loss=95.0), _row("COVER", 10, 90.0)])
    assert s.r_multiple is None


def _open_r(rows, qty, avg_entry, price):
    from src.storage.analytics.trade_review import open_position_r

    pos = [{"symbol": "AAPL", "qty": qty, "avg_entry": avg_entry, "current_price": price}]
    return open_position_r(pos, rows)["AAPL"]["unrealised_r"]


def test_open_single_lot_unchanged():
    rows = [_row("BUY", 10, 100.0, stop_loss=95.0)]
    assert _open_r(rows, 10, 100.0, 107.0) == r_multiple(107.0, 100.0, 95.0, 10)


def test_open_two_lot_long_uses_each_lots_stop():
    rows = [
        _row("BUY", 10, 100.0, stop_loss=90.0),
        _row("BUY", 10, 110.0, stop_loss=105.0, timestamp="2026-01-02 10:00:00"),
    ]
    assert _open_r(rows, 20, 105.0, 120.0) == round(15 * 20 / 150, 2)


def test_open_lot_without_stop_is_none():
    rows = [
        _row("BUY", 10, 100.0, stop_loss=90.0),
        _row("BUY", 10, 110.0, stop_loss=0, timestamp="2026-01-02 10:00:00"),
    ]
    assert _open_r(rows, 20, 105.0, 120.0) is None


def test_open_two_lot_r_unchanged_by_partial_sell():
    rows = [
        _row("BUY", 10, 100.0, stop_loss=90.0),
        _row("BUY", 10, 110.0, stop_loss=105.0, timestamp="2026-01-02 10:00:00"),
    ]
    before = _open_r(rows, 20, 105.0, 120.0)
    # The partial sell is in the chain, so the lots reconcile to the 5 still held.
    after = _open_r(rows + [_row("SELL", 15, 118.0, timestamp="2026-01-03 10:00:00")], 5, 105.0, 120.0)
    assert before is not None and after == before
