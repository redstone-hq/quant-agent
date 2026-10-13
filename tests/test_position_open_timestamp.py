"""`Database.get_position_open_timestamp` — the trail's bar window starts at
the POSITION OPEN, not at the most recent add.

Measured 2026-09-30 against the live production DB: the deterministic trail
sliced its bars from `get_symbol_last_buy`, i.e. the LATEST opening row, while
taking the entry PRICE from `position.avg_entry`, which is blended across every
add. MRVL's position `pos-08b43df53103` opened 2026-09-17 and took a third add
on 2026-09-23 at 17:19; the 19:31 trail evaluation that day therefore saw ZERO
bars "since entry" for a position four sessions old, and
`src/risk/trailing.py::_swing_lows` — which needs `2 * PIVOT_WINDOW + 1` = 7
bars before it can confirm one pivot — had nothing to read.

These tests pin the lookup itself and the one property it really has: the
open is never AFTER the last buy, so the window only ever LENGTHENS. That is
NOT the same as "cannot remove protection", which an earlier version of this
docstring claimed. A longer window raises the chandelier's high-water anchor,
and `evaluate_trailing_stop` refuses outright once the candidate rises through
its noise floor, so a wider window can cost a tighten. The change is justified
by consistency with the blended `avg_entry` price, and its cost was measured
at zero across all 21 recorded refusals (see `src/risk/trailing.py::_swing_lows`).
"""

import pytest

from src.storage.db import Database
from tests.pipeline_factory import build_pipeline


@pytest.fixture
def db(tmp_path):
    database = Database(str(tmp_path / "posopen.db"))
    database.initialize()
    yield database
    database.close()


def _buy(db, symbol, ts, qty=1.0, price=100.0, **kw):
    db.insert_trade(
        symbol=symbol,
        action="BUY",
        qty=qty,
        price=price,
        reasoning="entry",
        run_id="r",
        stop_loss=price * 0.9,
        fill_status="filled",
        **kw,
    )
    with db._lock:
        db.conn.execute(
            "UPDATE trades SET timestamp = ? WHERE id = (SELECT MAX(id) FROM trades WHERE symbol = ?)",
            (ts, symbol),
        )
        db.conn.commit()


def test_scale_in_does_not_move_the_position_open(db):
    """Three adds on one position: the open is the FIRST, not the last."""
    _buy(db, "MRVL", "2026-09-17 14:26:00", price=242.07)
    _buy(db, "MRVL", "2026-09-21 16:19:00", price=255.64)
    _buy(db, "MRVL", "2026-09-23 17:19:00", price=258.60)

    last = db.get_symbol_last_buy("MRVL")
    assert last["timestamp"].startswith("2026-09-23"), "fixture sanity"

    opened = db.get_position_open_timestamp(last)
    assert opened is not None
    assert opened.startswith("2026-09-17"), (
        "the position opened on the 17th; the add on the 23rd mints no new "
        "position and must not restart the trail's bar window"
    )


def test_window_only_ever_lengthens(db):
    """The one property this change really has: the open is never AFTER the
    last buy, so the bar window only ever lengthens.

    Do not read this as "cannot withdraw protection". A longer window raises
    the chandelier's high-water anchor, which raises the proposed stop, which
    can push it through the noise floor — and `evaluate_trailing_stop` then
    refuses outright rather than proposing a lower level. The monotonicity
    pinned here is real; the safety conclusion drawn from it was not.
    """
    for ts in ("2026-09-17 14:26:00", "2026-09-21 16:19:00", "2026-09-23 17:19:00"):
        _buy(db, "ETN", ts)
    last = db.get_symbol_last_buy("ETN")
    assert db.get_position_open_timestamp(last) <= last["timestamp"]


def test_single_buy_position_is_its_own_open(db):
    """No scale-in: open and last buy are the same row, so nothing changes."""
    _buy(db, "NET", "2026-09-17 19:04:00", price=335.93)
    last = db.get_symbol_last_buy("NET")
    assert db.get_position_open_timestamp(last) == last["timestamp"]


def test_a_new_position_after_flat_does_not_reach_back(db):
    """A fully-closed-then-reopened name must NOT inherit the old chain's date.

    `_assign_position_ids` closes a chain when net qty returns to ~0 and mints
    a fresh id for the next entry. The lookup is scoped by `position_id`, so
    the second position's window starts at the second position's open — bars
    from a trade that was already exited are not levels this trade defended.
    """
    _buy(db, "RKLB", "2026-08-01 14:00:00", qty=5.0, price=50.0)
    db.insert_trade(
        symbol="RKLB",
        action="SELL",
        qty=5.0,
        price=55.0,
        reasoning="exit",
        run_id="r",
        fill_status="filled",
    )
    _buy(db, "RKLB", "2026-09-21 15:00:00", qty=3.0, price=69.0)

    last = db.get_symbol_last_buy("RKLB")
    opened = db.get_position_open_timestamp(last)
    assert opened is not None
    assert opened.startswith("2026-09-21"), "the August position was closed; its bars belong to a different trade"


def test_missing_or_unidentified_row_returns_none_not_a_guess(db):
    """None means 'unknown'. The caller keeps its own fallback; nothing here
    invents a date."""
    assert db.get_position_open_timestamp(None) is None
    assert db.get_position_open_timestamp({}) is None
    assert (
        db.get_position_open_timestamp(
            {"symbol": "AAPL", "action": "BUY", "position_id": None},
        )
        is None
    )
    # A non-opening action is a caller bug, not a position.
    assert (
        db.get_position_open_timestamp(
            {"symbol": "AAPL", "action": "SELL", "position_id": "pos-1"},
        )
        is None
    )


# ---------------------------------------------------------------------------
# `get_position_open_row` — the date was never the only thing an add corrupts.
# `take_profit` (the trail's reference target) and `initial_stop_loss` (the
# denominator of R) are pinned at entry too, and the trailing pass read them
# off the newest add.
# ---------------------------------------------------------------------------


def test_open_row_carries_the_first_entrys_target_and_stop(db):
    """An add with a different target/stop must not become the trade's."""
    db.insert_trade(
        symbol="MRVL",
        action="BUY",
        qty=10,
        price=242.07,
        reasoning="open",
        run_id="r",
        stop_loss=218.0,
        take_profit=290.0,
        setup_type="breakout",
        fill_status="filled",
    )
    db.insert_trade(
        symbol="MRVL",
        action="BUY",
        qty=5,
        price=258.60,
        reasoning="add",
        run_id="r",
        stop_loss=244.0,
        take_profit=310.0,
        setup_type="range",
        fill_status="filled",
    )

    last = db.get_symbol_last_buy("MRVL")
    assert last["take_profit"] == pytest.approx(310.0), "fixture sanity"

    opened = db.get_position_open_row(last)
    assert opened is not None
    assert opened["price"] == pytest.approx(242.07)
    assert opened["take_profit"] == pytest.approx(290.0)
    assert opened["initial_stop_loss"] == pytest.approx(218.0)
    assert opened["setup_type"] == "breakout"


def test_open_row_is_none_without_a_position_id(db):
    """Fail closed: an unchainable row yields None so the caller keeps the
    row it already had — exactly today's behaviour, never a guess."""
    assert db.get_position_open_row(None) is None
    assert db.get_position_open_row({"symbol": "X", "action": "BUY"}) is None
    assert db.get_position_open_row({"symbol": "X", "action": "SELL", "position_id": "pos-1"}) is None


def test_trailing_pass_reads_the_open_not_the_add(db):
    """End to end: with an add on the books, the trail is handed the
    ORIGINAL target and the ORIGINAL stop, not the add's."""
    from types import SimpleNamespace
    from unittest.mock import MagicMock, patch

    from src.pipeline import TradingPipeline

    db.insert_trade(
        symbol="MRVL",
        action="BUY",
        qty=10,
        price=242.07,
        reasoning="open",
        run_id="r",
        stop_loss=218.0,
        take_profit=290.0,
        setup_type="breakout",
        fill_status="filled",
    )
    db.insert_trade(
        symbol="MRVL",
        action="BUY",
        qty=5,
        price=258.60,
        reasoning="add",
        run_id="r",
        stop_loss=244.0,
        take_profit=310.0,
        setup_type="range",
        fill_status="filled",
    )

    p = build_pipeline(db=db, broker=MagicMock(), market=MagicMock())
    p.broker.get_current_stop_price.return_value = 244.0
    p.market.get_ohlcv.return_value = []
    p._atr_for_symbol = MagicMock(return_value=5.0)
    position = SimpleNamespace(symbol="MRVL", avg_entry=247.5, current_price=265.0, qty=15.0)

    with (
        patch("src.execution.scale_in.pending_protection_symbols", return_value=set()),
        patch("src.risk.trailing.evaluate_trailing_stop") as ev,
    ):
        ev.return_value = SimpleNamespace(
            proposal=None,
            code="noop",
            structural_code=None,
        )
        p._apply_deterministic_trails([position], run_id="r1")

    kwargs = ev.call_args.kwargs
    assert kwargs["reference_target"] == pytest.approx(290.0), (
        "the add's target would sit above the original and re-open room the trade had already closed"
    )
    # 1R per share is PER LOT: each lot against its OWN entry stop, over held qty.
    assert kwargs["risk_per_share"] == pytest.approx((10 * (242.07 - 218.0) + 5 * (258.60 - 244.0)) / 15)
    assert kwargs["setup_type"] == "breakout"
