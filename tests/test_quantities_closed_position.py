"""Closed-position pairing: an un-triggered stop order is not a sale.

A TRAIL_STOP row is written for the FULL position at every stop move, with no
fill. Counting it as an exit reported META closed while 3.48 shares were still
held, and priced ETN and AMD exits at stop prices that never traded (prod,
2026-10-13).
"""

import pytest

from src.quantities import split_position_chain, summarize_closed_position

_ID = [0]


def _row(action, qty, price, ts, *, stop=None, fill_qty="same", fill_status="filled", pnl=None):
    _ID[0] += 1
    return {
        "id": _ID[0],
        "position_id": "P1",
        "symbol": "XYZ",
        "action": action,
        "qty": qty,
        "price": price,
        "fill_qty": qty if fill_qty == "same" else fill_qty,
        "fill_price": price if fill_status else None,
        "fill_status": fill_status,
        "stop_loss": stop,
        "decision_id": None,
        "timestamp": ts,
        "realized_pnl": pnl,
    }


def _stop_move(qty, price, ts):
    """A trail move as the stop writer records it: full position, no status, no fill."""
    return _row("TRAIL_STOP", qty, price, ts, fill_qty=None, fill_status=None)


def test_meta_shaped_chain_with_resting_stop_is_still_open():
    rows = [
        _row("BUY", 3, 700.0, "2026-10-13 14:00:00", stop=680.0),
        _row("BUY", 2.48, 710.0, "2026-10-13 15:00:00", stop=690.0),
        _row("PARTIAL_SELL(36%)", 2, 720.0, "2026-10-13 16:00:00", pnl=36.0),
        _stop_move(3.48, 705.0, "2026-10-13 16:05:00"),
    ]
    assert summarize_closed_position(rows) is None


def test_etn_shaped_chain_prices_exit_from_real_sells_only():
    rows = [
        _row("BUY", 10, 100.0, "2026-10-13 14:00:00", stop=95.0),
        _row("BUY", 5, 102.0, "2026-10-13 14:30:00", stop=97.0),
        _row("PARTIAL_SELL(33%)", 5, 99.0, "2026-10-13 15:00:00", pnl=-7.33),
        _stop_move(10, 96.0, "2026-10-13 15:05:00"),
        _stop_move(10, 97.5, "2026-10-13 15:30:00"),
        _row("SELL", 10, 98.0, "2026-10-13 16:00:00", pnl=-24.67),
    ]
    _, exits, _ = split_position_chain(rows)
    assert [r["action"] for r in exits] == ["PARTIAL_SELL(33%)", "SELL"]
    position = summarize_closed_position(rows)
    assert position is not None
    assert position.qty == 15
    assert position.exit_price == pytest.approx((5 * 99.0 + 10 * 98.0) / 15)
    assert position.closed_at == "2026-10-13 16:00:00"


def test_filled_trail_stop_still_counts_as_exit():
    rows = [
        _row("BUY", 10, 100.0, "2026-10-13 14:00:00", stop=95.0),
        _stop_move(10, 96.0, "2026-10-13 15:00:00"),
        _row("TRAIL_STOP", 10, 97.0, "2026-10-13 16:00:00"),
    ]
    position = summarize_closed_position(rows)
    assert position is not None
    assert position.exit_price == pytest.approx(97.0)
    assert position.closed_at == "2026-10-13 16:00:00"


def test_partly_filled_then_canceled_trail_stop_counts_its_filled_shares():
    rows = [
        _row("BUY", 10, 100.0, "2026-10-13 14:00:00", stop=95.0),
        _row("TRAIL_STOP", 10, 97.0, "2026-10-13 15:00:00", fill_qty=4, fill_status="canceled"),
        _row("SELL", 6, 98.0, "2026-10-13 16:00:00"),
    ]
    position = summarize_closed_position(rows)
    assert position is not None
    assert position.exit_price == pytest.approx((4 * 97.0 + 6 * 98.0) / 10)
