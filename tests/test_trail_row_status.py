"""A deterministic trailing-stop raise is recorded and reported as a stop
raise, never as an order that failed to fill.

2026-10-19, XOM: the deterministic trail moved the stop $169.14 -> $173.60 by
amending both legs in place, but wrote its TRAIL_STOP trade row with no
order id and no fill status. The owner feed then printed it under "Not filled
in time ... not recorded", and the executed-trade filter counted it as a fill.
"""

from unittest.mock import MagicMock

from src.trader_feed.common import _classify_trades, _traded_word
from src.trader_feed.decision import _append_done
from tests.test_phase3_exit_rework import _pipeline, _position


class _Bar:
    def __init__(self, hi, lo, d):
        self.high, self.low, self.date = hi, lo, d
        self.close = (hi + lo) / 2


def _trailing_pipeline(order: dict):
    pipeline = _pipeline()
    pipeline.db.get_symbol_last_buy.return_value = {
        "setup_type": "breakout",
        "take_profit": None,
        "timestamp": "2026-08-01 14:00:00",
    }
    pipeline.broker.get_current_stop_price.return_value = 95.0
    pipeline._atr_for_symbol = MagicMock(return_value=2.0)
    pipeline.db.get_position_chain_rows.return_value = [
        {"id": 1, "symbol": "ZZZA", "action": "BUY", "qty": 10, "filled_at": "2026-08-07 14:00:00.000000"}
    ]
    lows = [110, 108, 106, 100, 106, 108, 110, 118, 116, 114, 110, 114, 116, 118, 125]
    pipeline.market = MagicMock()
    warm = [_Bar(112, 110, "2026-08-01") for _ in range(14)]
    pipeline.market.get_ohlcv.return_value = warm + [_Bar(lo + 2, lo, "2026-08-10") for lo in lows]
    pipeline.broker.replace_stop_loss.return_value = order
    return pipeline


def _trail_row_kwargs(pipeline) -> dict:
    calls = [c for c in pipeline.db.insert_trade.call_args_list if c.kwargs.get("action") == "TRAIL_STOP"]
    assert len(calls) == 1
    return calls[0].kwargs


def test_deterministic_trail_row_carries_submitted_and_the_order_id():
    pipeline = _trailing_pipeline({"id": "zzz-stop-1"})
    orders = pipeline._apply_deterministic_trails(
        [_position("ZZZA", qty=10, avg_entry=100.0, current_price=125.0)],
        run_id="r1",
    )
    assert len(orders) == 1
    row = _trail_row_kwargs(pipeline)
    assert row.get("fill_status") == "submitted"
    assert row.get("broker_order_id") == "zzz-stop-1"


def test_multi_leg_amend_records_the_first_amended_legs_id_like_midday():
    # The amend returns the first amended leg's new id as `id` (both legs in
    # `legs`); midday stores `order.get("id")`, and so does this writer.
    order = {
        "id": "zzz-leg-a-new",
        "amend_status": "accepted",
        "legs": [
            {"id": "a", "qty": 5, "old_stop": 95.0, "new_stop": 110.0, "outcome": "amended", "new_id": "zzz-leg-a-new"},
            {"id": "b", "qty": 5, "old_stop": 95.0, "new_stop": 110.0, "outcome": "amended", "new_id": "zzz-leg-b-new"},
        ],
    }
    pipeline = _trailing_pipeline(order)
    pipeline._apply_deterministic_trails(
        [_position("ZZZA", qty=10, avg_entry=100.0, current_price=125.0)],
        run_id="r1",
    )
    row = _trail_row_kwargs(pipeline)
    assert row.get("fill_status") == "submitted"
    assert row.get("broker_order_id") == "zzz-leg-a-new"


def _trail_row(fill_status):
    return {
        "symbol": "ZZZX",
        "action": "TRAIL_STOP",
        "qty": 0.688,
        "price": 173.60,
        "reasoning": "trail tightened",
        "fill_status": fill_status,
        "fill_qty": None,
        "fill_price": None,
    }


def test_feed_renders_a_stop_raise_never_a_missed_fill():
    for status in ("submitted", None):  # None: rows written before this fix
        snap = {"trades": [_trail_row(status)]}
        reached, stalled = _classify_trades(snap)
        assert stalled == []
        assert len(reached) == 1
        lines: list[str] = []
        _append_done(lines, reached, snap, {})
        text = "\n".join(lines)
        assert "stop raised to $173.60" in text
        assert "nothing sold" in text
        assert "waiting to fill" not in text
        assert _traded_word(reached) == "STOP RAISED"


def test_a_filled_trail_stop_is_still_a_fill():
    snap = {"trades": [_trail_row("filled")]}
    reached, _ = _classify_trades(snap)
    lines: list[str] = []
    _append_done(lines, reached, snap, {})
    assert "— filled" in "\n".join(lines)
    assert "stop raised" not in "\n".join(lines)
