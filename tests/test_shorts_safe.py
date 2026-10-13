"""Stage 2 of short selling — a short position must be SAFE.

Stage 1 (`tests/test_shorts_countable.py`) made a held short visible to every
counting/risk surface without making it tradeable. Stage 2 does not change
that boundary — `PortfolioConstructor` still refuses to open or cover a
short, and no order path in this repo submits a SELL_SHORT or BUY-to-cover.
What Stage 2 fixes is the PROTECTIVE machinery a short would rely on if one
ever existed: the stop it is placed with, the stop discovery that decides
whether it is "protected", and the deterministic trail that tightens it —
all of which were written and tested only against a long's SELL-stop-below
geometry.

**The load-bearing half of this file is still the no-op proof**, same as
Stage 1: the live book is long-only, so every surface below is exercised
three ways where it makes sense —

  * ``*_long_*`` — the exact pre-change behaviour, pinned with a literal.
  * ``*_short_*`` — the short case, asserting the correct MIRRORED handling
    (a BUY stop above instead of a SELL stop below; ratchet down instead of
    up; "protected" measured against the other side of entry).
  * The final section re-proves the hard boundary: shorts still cannot be
    opened or covered by anything in this repo.

Surfaces covered: ``src/risk/trailing.py`` (``compute_trailing_stop``),
``src/execution/broker.py`` (``place_entry_protection``,
``_submit_stop_limit_order``, ``get_current_stop_price``,
``replace_stop_loss``), and ``src/pipeline.py``'s
``_reconcile_stop_coverage``.
"""

from dataclasses import dataclass
from unittest.mock import MagicMock, patch

import pytest
from alpaca.trading.enums import OrderSide

from src.execution.broker import AlpacaBroker
from src.risk.trailing import (
    CHANDELIER_ATR_MULTIPLE,
    MIN_RATCHET_TICKS,
    min_ratchet_floor,
    compute_trailing_stop,
)
from tests.pipeline_factory import build_pipeline


# ==========================================================================
# Shared fixtures
# ==========================================================================


from datetime import date as _date

#: Every fixture bar is a real completed daily bar dated after the entry day:
#: the chandelier reads only bars dated strictly after `entry_date`.
_ENTRY_DAY = _date(2026, 1, 2)
_BAR_DAY = _date(2026, 1, 5)


@dataclass
class _Bar:
    high: float
    low: float
    date: _date = _BAR_DAY


def _bars(pattern):
    """`pattern` is a list of (high, low) tuples, oldest first."""
    return [_Bar(high=h, low=lo) for h, lo in pattern]


def _mirror_bars(bars, axis):
    """Reflect a (high, low) bar series around `axis`. Turns a long's
    ascending-higher-lows chart into a short's descending-lower-highs
    chart: each bar's high/low swap roles AND reflect, so a confirmed swing
    LOW in the source becomes a confirmed swing HIGH at the mirrored price,
    at the same distance from the axis. Used to derive every short test
    below algebraically from the long fixtures in
    `tests/test_trailing_stops.py`, instead of hand-typing numbers that
    could silently drift out of sync with them.
    """
    return [_Bar(high=2 * axis - b.low, low=2 * axis - b.high) for b in bars]


def _mirror(price: float, axis: float) -> float:
    return 2 * axis - price


def _rising_with_higher_lows():
    """Same fixture as tests/test_trailing_stops.py: a clean uptrend with two
    CONFIRMED swing lows, at 100 and 110."""
    lows = [110, 108, 106, 100, 106, 108, 110, 118, 116, 114, 110, 114, 116, 118, 125]
    return _bars([(lo + 2, lo) for lo in lows])


# Mirror axis for the trailing-stop scenarios below. Arbitrary — chosen only
# so mirrored prices come out as round numbers.
_AXIS = 150.0


def _broker(mock_tc_cls):
    mock_client = MagicMock()
    mock_tc_cls.return_value = mock_client
    return AlpacaBroker(api_key="t", secret_key="t", paper=True), mock_client


def _mock_position(symbol, qty):
    p = MagicMock()
    p.symbol = symbol
    p.qty = qty
    return p


def _mock_stop_order(order_id, stop_price, side, qty=10, order_type="stop", status="accepted"):
    o = MagicMock()
    o.id = order_id
    o.order_type = order_type
    o.side = side
    o.qty = qty
    o.stop_price = stop_price
    o.limit_price = stop_price * (0.97 if side == "sell" else 1.03)
    o.status = status
    return o


# ==========================================================================
# 1. risk/trailing.compute_trailing_stop — mirrored ratchet direction
# ==========================================================================


def test_trailing_long_breakout_structure_pick_unchanged():
    """No-op proof, literal-for-literal (same fixture as
    tests/test_trailing_stops.py): a breakout trails to the higher swing low."""
    proposal = compute_trailing_stop(
        symbol="AAA",
        setup_type="breakout",
        entry=100.0,
        current_price=125.0,
        current_stop=95.0,
        reference_target=None,
        bars=_rising_with_higher_lows(),
        entry_date=_ENTRY_DAY,
        atr=2.0,
    )
    assert proposal.new_stop == 110.0
    assert proposal.source == "structure"
    # Explicit positive qty must change nothing.
    proposal2 = compute_trailing_stop(
        symbol="AAA",
        setup_type="breakout",
        entry=100.0,
        current_price=125.0,
        current_stop=95.0,
        reference_target=None,
        bars=_rising_with_higher_lows(),
        entry_date=_ENTRY_DAY,
        atr=2.0,
        qty=250.0,
    )
    assert proposal2.new_stop == 110.0


def test_trailing_short_breakout_structure_pick_is_the_mirror():
    """Short mirror of the long structure-pick case above, generated by
    reflecting entry/price/stop/bars through the same axis. Pre-fix this
    would have tried to trail a short's stop UP using long arithmetic —
    exactly backwards for a position that profits as price falls."""
    mbars = _mirror_bars(_rising_with_higher_lows(), _AXIS)
    proposal = compute_trailing_stop(
        symbol="SSS",
        setup_type="breakout",
        entry=_mirror(100.0, _AXIS),
        current_price=_mirror(125.0, _AXIS),
        current_stop=_mirror(95.0, _AXIS),
        reference_target=None,
        bars=mbars,
        entry_date=_ENTRY_DAY,
        atr=2.0,
        qty=-1.0,
    )
    assert proposal is not None
    assert proposal.new_stop == _mirror(110.0, _AXIS) == 190.0
    assert proposal.source == "structure"
    # The new stop must be BELOW the old one (tighter for a short) and
    # ABOVE current price — the mirror of the long invariant.
    assert proposal.new_stop < _mirror(95.0, _AXIS)
    assert proposal.new_stop > _mirror(125.0, _AXIS)


def test_trailing_short_chandelier_fallback_is_the_mirror():
    """Mirror of test_chandelier_is_the_fallback_when_structure_is_unclear:
    a vertical move with no confirmed swing high still gets a trail, off the
    LOWEST low instead of the highest high."""
    straight_down = _bars([(101 - i, 100 - i) for i in range(14)])
    proposal = compute_trailing_stop(
        symbol="SSS",
        setup_type="breakout",
        entry=100.0,
        current_price=87.0,
        current_stop=105.0,
        reference_target=None,
        bars=straight_down,
        entry_date=_ENTRY_DAY,
        atr=1.0,
        qty=-1.0,
    )
    assert proposal is not None
    assert proposal.source == "chandelier"
    # lowest low 87 + 3 x ATR(1.0) = 90
    assert proposal.new_stop == pytest.approx(87 + CHANDELIER_ATR_MULTIPLE * 1.0)


def test_trailing_short_ratchets_down_only():
    """Mirror of test_a_stop_never_ratchets_down — the single most important
    property for a short. Every proposal must sit strictly BELOW the stop it
    replaces; nothing may ever move a short's stop up."""
    mbars = _mirror_bars(_rising_with_higher_lows(), _AXIS)
    produced_at_least_one = False
    for existing in [220.0, 205.0, 200.0, 195.0, 190.0, 185.0, 182.0, 179.0, 176.0]:
        proposal = compute_trailing_stop(
            symbol="SSS",
            setup_type="breakout",
            entry=_mirror(100.0, _AXIS),
            current_price=_mirror(125.0, _AXIS),
            current_stop=existing,
            reference_target=None,
            bars=mbars,
            entry_date=_ENTRY_DAY,
            atr=2.0,
            qty=-1.0,
        )
        if proposal is not None:
            produced_at_least_one = True
            assert proposal.new_stop < existing, f"short stop moved UP from {existing} to {proposal.new_stop}"
            assert proposal.previous_stop == existing
    assert produced_at_least_one, "the sweep proved nothing if nothing fired"


def test_trailing_short_noise_band():
    """Mirror of test_a_stop_is_never_placed_inside_the_atr_noise_band: a
    candidate stop within 1.25xATR of current price is inside one ordinary
    day's range and must not be placed, on either side."""
    mbars = _mirror_bars(_rising_with_higher_lows(), _AXIS)
    # ATR 13 => noise ceiling 175 + 1.25*13 = 191.25; the mirrored 190 swing
    # high sits inside it, so no order is worth placing.
    assert (
        compute_trailing_stop(
            symbol="SSS",
            setup_type="breakout",
            entry=_mirror(100.0, _AXIS),
            current_price=_mirror(125.0, _AXIS),
            current_stop=_mirror(95.0, _AXIS),
            reference_target=None,
            bars=mbars,
            entry_date=_ENTRY_DAY,
            atr=13.0,
            qty=-1.0,
        )
        is None
    )


def test_trailing_short_range_setup_trails_from_entry():
    """Mirror of item 212: the target gate is gone, so a range short trails on
    structure whether or not price has fallen past the defended target."""
    mbars = _mirror_bars(_rising_with_higher_lows(), _AXIS)
    # current_price mirrors 118.0 — short of the mirrored target, and still
    # trailed, which is exactly what item 212 changed.
    early = compute_trailing_stop(
        symbol="SSS",
        setup_type="range",
        entry=_mirror(100.0, _AXIS),
        current_price=_mirror(118.0, _AXIS),
        current_stop=_mirror(95.0, _AXIS),
        reference_target=_mirror(130.0, _AXIS),
        bars=mbars,
        entry_date=_ENTRY_DAY,
        atr=2.0,
        qty=-1.0,
    )
    assert early is not None
    assert early.new_stop == _mirror(110.0, _AXIS)
    # current_price mirrors 125.0 (past the mirrored target 120) — trails.
    proposal = compute_trailing_stop(
        symbol="SSS",
        setup_type="range",
        entry=_mirror(100.0, _AXIS),
        current_price=_mirror(125.0, _AXIS),
        current_stop=_mirror(95.0, _AXIS),
        reference_target=_mirror(120.0, _AXIS),
        bars=mbars,
        entry_date=_ENTRY_DAY,
        atr=2.0,
        qty=-1.0,
    )
    assert proposal is not None
    assert proposal.new_stop == _mirror(110.0, _AXIS)


def test_trailing_short_locked_message_at_or_below_entry():
    """Mirror of test_reason_names_when_risk_is_released: a short's risk is
    released once the trailed stop reaches entry FROM ABOVE."""
    mbars = _mirror_bars(_rising_with_higher_lows(), _AXIS)
    proposal = compute_trailing_stop(
        symbol="SSS",
        setup_type="breakout",
        entry=_mirror(100.0, _AXIS),
        current_price=_mirror(125.0, _AXIS),
        current_stop=_mirror(95.0, _AXIS),
        reference_target=None,
        bars=mbars,
        entry_date=_ENTRY_DAY,
        atr=2.0,
        qty=-1.0,
    )
    assert proposal.new_stop <= _mirror(100.0, _AXIS)
    assert "stops consuming risk budget" in proposal.reason
    assert "at or below entry" in proposal.reason


def test_trailing_short_with_no_live_stop_yields_no_proposal():
    """Mirror of test_no_live_stop_yields_no_proposal: an unprotected short
    is a repair problem, not a trailing problem, same as a long."""
    assert (
        compute_trailing_stop(
            symbol="SSS",
            setup_type="breakout",
            entry=100.0,
            current_price=80.0,
            current_stop=None,
            reference_target=None,
            bars=_bars([(101, 99)] * 10),
            entry_date=_ENTRY_DAY,
            atr=2.0,
            qty=-1.0,
        )
        is None
    )


# ==========================================================================
# 2. execution/broker — the protective stop's SIDE and LIMIT direction
# ==========================================================================
# The single most dangerous line in this task: a short's protective order is
# a BUY stop, and its limit must sit ABOVE the trigger (a BUY needs headroom
# to fill on the way up). Getting this backwards submits an order that looks
# accepted but can never fill, so the position runs unprotected. The primary
# protective stop is now a stop-MARKET (owner ratified 2026-09-25), so the
# side-of-trigger limit only matters on the stop-limit FALLBACK — these tests
# drive that fallback so the dangerous line still has coverage.


class _FakeComboReject(Exception):
    """Stand-in for alpaca's APIError: the classifier reads only
    `status_code` and `str(exc)`."""

    def __init__(self, message="order type not supported", status_code=422):
        super().__init__(message)
        self.status_code = status_code


@patch("src.execution.broker.TradingClient")
def test_place_entry_protection_long_side_unchanged(mock_tc_cls):
    """A BUY entry is protected by a SELL stop-MARKET (no limit). On the
    unsupported-combo fallback the stop-LIMIT sits 3% BELOW the trigger."""
    from alpaca.trading.requests import StopLimitOrderRequest, StopOrderRequest

    broker, client = _broker(mock_tc_cls)
    broker.wait_for_order_terminal = MagicMock(return_value="filled")
    broker.get_order_fill_info = MagicMock(return_value={"filled_qty": 10.0})
    client.submit_order.side_effect = [
        _FakeComboReject(),  # primary stop-MARKET refused
        MagicMock(id="s1", status="new"),  # fallback stop-LIMIT accepted
    ]

    out = broker.place_entry_protection("AAA", "e1", stop_price=100.0, requested_qty=10)

    assert out is not None
    reqs = [c.args[0] for c in client.submit_order.call_args_list]
    assert isinstance(reqs[0], StopOrderRequest)  # primary: stop-MARKET
    assert reqs[0].side == OrderSide.SELL
    assert float(reqs[0].stop_price) == 100.0
    assert getattr(reqs[0], "limit_price", None) is None
    assert isinstance(reqs[1], StopLimitOrderRequest)  # fallback: stop-LIMIT
    assert reqs[1].side == OrderSide.SELL
    assert float(reqs[1].limit_price) == 97.0  # 3% BELOW the trigger


@patch("src.execution.broker.TradingClient")
def test_place_entry_protection_short_side_submits_buy_stop_limit_above_trigger(mock_tc_cls):
    """THE pin. A short entry (side='sell') is protected by a BUY stop. The
    primary is a stop-MARKET; on the fallback the stop-LIMIT must sit 3%
    ABOVE the trigger — backwards, and the fallback fires into an
    unmarketable limit that can never fill and the short runs unprotected."""
    from alpaca.trading.requests import StopLimitOrderRequest, StopOrderRequest

    broker, client = _broker(mock_tc_cls)
    broker.wait_for_order_terminal = MagicMock(return_value="filled")
    broker.get_order_fill_info = MagicMock(return_value={"filled_qty": 10.0})
    client.submit_order.side_effect = [
        _FakeComboReject(),  # primary stop-MARKET refused
        MagicMock(id="s1", status="new"),  # fallback stop-LIMIT accepted
    ]

    out = broker.place_entry_protection(
        "SSS",
        "e1",
        stop_price=100.0,
        requested_qty=10,
        side="sell",
    )

    assert out is not None
    reqs = [c.args[0] for c in client.submit_order.call_args_list]
    assert isinstance(reqs[0], StopOrderRequest)  # primary: stop-MARKET
    assert reqs[0].side == OrderSide.BUY
    assert float(reqs[0].stop_price) == 100.0
    assert getattr(reqs[0], "limit_price", None) is None
    assert isinstance(reqs[1], StopLimitOrderRequest)  # fallback: stop-LIMIT
    assert reqs[1].side == OrderSide.BUY
    assert float(reqs[1].limit_price) == 103.0  # 3% ABOVE the trigger


@patch("src.execution.broker.TradingClient")
def test_submit_stop_limit_order_sell_default_fallback_unchanged(mock_tc_cls):
    """The raw primitive: primary is a SELL stop-MARKET; the stop-limit
    fallback (no explicit limit_price, default side) sits 3% below."""
    from alpaca.trading.requests import StopLimitOrderRequest, StopOrderRequest

    broker, client = _broker(mock_tc_cls)
    client.submit_order.side_effect = [
        _FakeComboReject(),
        MagicMock(id="o1", status="new"),
    ]

    broker._submit_stop_limit_order(symbol="AAA", qty=10, stop_price=200.0)

    reqs = [c.args[0] for c in client.submit_order.call_args_list]
    assert isinstance(reqs[0], StopOrderRequest)
    assert reqs[0].side == OrderSide.SELL
    assert getattr(reqs[0], "limit_price", None) is None
    assert isinstance(reqs[1], StopLimitOrderRequest)
    assert reqs[1].side == OrderSide.SELL
    assert float(reqs[1].limit_price) == 194.0


@patch("src.execution.broker.TradingClient")
def test_submit_stop_limit_order_buy_side_fallback_is_the_mirror(mock_tc_cls):
    from alpaca.trading.requests import StopLimitOrderRequest, StopOrderRequest

    broker, client = _broker(mock_tc_cls)
    client.submit_order.side_effect = [
        _FakeComboReject(),
        MagicMock(id="o1", status="new"),
    ]

    broker._submit_stop_limit_order(symbol="SSS", qty=10, stop_price=200.0, side="buy")

    reqs = [c.args[0] for c in client.submit_order.call_args_list]
    assert isinstance(reqs[0], StopOrderRequest)
    assert reqs[0].side == OrderSide.BUY
    assert isinstance(reqs[1], StopLimitOrderRequest)
    assert reqs[1].side == OrderSide.BUY
    assert float(reqs[1].limit_price) == 206.0


# ==========================================================================
# 3. get_current_stop_price — a short's BUY stop must be visible
# ==========================================================================


@patch("src.execution.broker.TradingClient")
def test_get_current_stop_price_long_unchanged(mock_tc_cls):
    """No-op proof, literal-for-literal (same shape as
    test_get_current_stop_price_reports_the_highest_of_many)."""
    broker, client = _broker(mock_tc_cls)
    client.get_orders.return_value = [
        _mock_stop_order("s1", 340.0, "sell"),
        _mock_stop_order("s2", 350.0, "sell"),
        _mock_stop_order("s3", 330.0, "sell"),
    ]
    assert broker.get_current_stop_price("GE") == 350.0


@patch("src.execution.broker.TradingClient")
def test_get_current_stop_price_short_reports_lowest_buy_stop(mock_tc_cls):
    """A short's protective stops are BUY stops; the level that fires FIRST
    as price rises is the LOWEST one — the mirror of 'highest wins' for a
    long. Pre-fix this method only ever looked for side='sell' and would
    have reported a perfectly protected short as having NO stop at all."""
    broker, client = _broker(mock_tc_cls)
    client.get_orders.return_value = [
        _mock_stop_order("b1", 340.0, "buy"),
        _mock_stop_order("b2", 350.0, "buy"),
        _mock_stop_order("b3", 330.0, "buy"),
    ]
    assert broker.get_current_stop_price("GE") == 330.0


@patch("src.execution.broker.TradingClient")
def test_get_current_stop_price_ambiguous_both_sides_fails_closed(mock_tc_cls):
    """Stops on both sides (stale orders): refuse to guess; unreadable."""
    broker, client = _broker(mock_tc_cls)
    client.get_orders.return_value = [
        _mock_stop_order("s1", 340.0, "sell"),
        _mock_stop_order("b1", 360.0, "buy"),
    ]
    from src.execution.stop_read import StopReadUnavailable

    with pytest.raises(StopReadUnavailable):
        broker.get_current_stop_price("GE")


@patch("src.execution.broker.TradingClient")
def test_get_current_stop_price_no_stops_is_none(mock_tc_cls):
    broker, client = _broker(mock_tc_cls)
    client.get_orders.return_value = []
    assert broker.get_current_stop_price("GE") is None


# ==========================================================================
# 4. replace_stop_loss — the deterministic and discretionary trail's target
# ==========================================================================


@patch("src.execution.broker.TradingClient")
def test_replace_stop_loss_long_unchanged(mock_tc_cls):
    """No-op proof, literal-for-literal (same shape as
    test_replace_stop_loss_cancels_old_and_submits_new)."""
    broker, client = _broker(mock_tc_cls)
    client.get_orders.return_value = [_mock_stop_order("old", 185.0, "sell")]
    client.submit_order.return_value = MagicMock(id="new-stop", status="accepted")
    client.get_all_positions.return_value = [
        MagicMock(
            symbol="NVDA",
            qty="10",
            avg_entry_price="180.0",
            current_price="200.0",
            market_value="2000.0",
            unrealized_pl="200.0",
        ),
    ]

    result = broker.replace_stop_loss("NVDA", 192.0)

    assert result is not None and result["id"] == "new-stop"
    client.cancel_order_by_id.assert_called_once_with("old")
    req = client.submit_order.call_args[0][0]
    assert req.side == OrderSide.SELL
    assert float(req.qty) == 10.0
    assert float(req.stop_price) == 192.0


@patch("src.execution.broker.TradingClient")
def test_replace_stop_loss_short_ratchets_down_and_submits_buy_stop(mock_tc_cls):
    """The mirror of the long case above: an existing BUY stop is found,
    cancelled, and replaced with a LOWER BUY stop sized to the (unsigned)
    short qty."""
    broker, client = _broker(mock_tc_cls)
    client.get_orders.return_value = [_mock_stop_order("old", 215.0, "buy")]
    client.submit_order.return_value = MagicMock(id="new-stop", status="accepted")
    client.get_all_positions.return_value = [
        MagicMock(
            symbol="TSLA",
            qty="-10",
            avg_entry_price="220.0",
            current_price="200.0",
            market_value="-2000.0",
            unrealized_pl="200.0",
        ),
    ]

    result = broker.replace_stop_loss("TSLA", 208.0)  # tighter (lower) than 215

    assert result is not None and result["id"] == "new-stop"
    client.cancel_order_by_id.assert_called_once_with("old")
    req = client.submit_order.call_args[0][0]
    assert req.side == OrderSide.BUY
    assert float(req.qty) == 10.0  # unsigned, not -10
    assert float(req.stop_price) == 208.0


@patch("src.execution.broker.TradingClient")
def test_replace_stop_loss_short_rejects_a_higher_new_stop(mock_tc_cls):
    """Mirror of test_replace_stop_loss_rejects_lower_new_stop: a short's
    trailing stop must ratchet DOWN only. A 'new' stop at or above the
    existing one would weaken protection — reject before any cancel.

    A live short position is mocked too (matching qty), even though the
    ratchet check fires before this code ever looks at it, so the rejection
    is pinned to the ratchet logic specifically rather than incidentally
    riding on "no position found". (Against the pre-Stage-2 code this still
    returns None either way — via the OLD blanket `qty <= 0` refusal, a
    different real bug this task also fixes — so this particular test does
    not by itself discriminate old vs. new; see
    test_replace_stop_loss_short_ratchets_down_and_submits_buy_stop for the
    one that does.)"""
    broker, client = _broker(mock_tc_cls)
    client.get_orders.return_value = [_mock_stop_order("old", 210.0, "buy")]
    client.get_all_positions.return_value = [
        MagicMock(
            symbol="TSLA",
            qty="-10",
            avg_entry_price="220.0",
            current_price="212.0",
            market_value="-2120.0",
            unrealized_pl="80.0",
        ),
    ]

    result = broker.replace_stop_loss("TSLA", 215.0)  # ABOVE existing — reject

    assert result is None
    client.cancel_order_by_id.assert_not_called()
    client.submit_order.assert_not_called()


@patch("src.execution.broker.TradingClient")
def test_replace_stop_loss_ambiguous_both_sides_refuses(mock_tc_cls):
    """Stops on both sides for one symbol is a stale-order state (can't be
    both long and short at once) — refuse rather than guess.

    A live LONG position is mocked to match the sell-stop side, so this
    scenario is forced all the way to the point where the ambiguity check
    matters: against the pre-Stage-2 code (which never looks at the buy
    side at all), the same setup does NOT refuse — it silently cancels the
    sell-stop and submits a new one, leaving the buy-stop it never noticed
    untouched. That silent divergence is exactly what this check exists to
    catch."""
    broker, client = _broker(mock_tc_cls)
    client.get_orders.return_value = [
        _mock_stop_order("s1", 190.0, "sell"),
        _mock_stop_order("b1", 210.0, "buy"),
    ]
    client.get_all_positions.return_value = [
        MagicMock(
            symbol="AAA",
            qty="10",
            avg_entry_price="180.0",
            current_price="200.0",
            market_value="2000.0",
            unrealized_pl="200.0",
        ),
    ]

    assert broker.replace_stop_loss("AAA", 195.0) is None
    client.cancel_order_by_id.assert_not_called()


@patch("src.execution.broker.TradingClient")
def test_replace_stop_loss_no_position_returns_none_unchanged(mock_tc_cls):
    broker, client = _broker(mock_tc_cls)
    client.get_orders.return_value = []
    client.get_all_positions.return_value = []
    assert broker.replace_stop_loss("AAA", 100.0) is None
    client.submit_order.assert_not_called()


# ==========================================================================
# 5. pipeline._reconcile_stop_coverage — a protected short is not NAKED
# ==========================================================================


def _pipeline_for_reconcile(positions, snapshot_side_effect):
    from src.pipeline import TradingPipeline

    pipe = build_pipeline(broker=MagicMock(), db=MagicMock())
    pipe.db.get_pending_protection_restores.return_value = []
    pipe.broker.get_positions.return_value = positions
    pipe.broker.snapshot_protective_stops.side_effect = snapshot_side_effect
    return pipe


def test_reconcile_stop_coverage_long_fully_covered_unchanged():
    """No-op proof, literal-for-literal: a fully-covered long produces no gap."""

    def _snap(sym, side="sell"):
        assert side == "sell"
        return (True, [{"id": "s1", "qty": 10.0}])

    pipe = _pipeline_for_reconcile([_mock_position("AAA", 10.0)], _snap)
    assert pipe._reconcile_stop_coverage() == []


def test_reconcile_stop_coverage_short_with_live_stop_is_protected_not_naked():
    """The headline Stage 2 property for this reconciler. A short with a
    BUY stop covering its full size must NOT be reported as a gap — pre-fix
    this position was skipped outright (qty<=0), so it was never even
    checked; a naive same-side check would have found zero SELL-stops and
    called a perfectly protected short NAKED."""

    def _snap(sym, side="sell"):
        assert side == "buy", "a short must be checked on the BUY side"
        return (True, [{"id": "b1", "qty": 40.0}])

    pipe = _pipeline_for_reconcile([_mock_position("TSLA", -40.0)], _snap)
    gaps = pipe._reconcile_stop_coverage()
    assert gaps == []
    pipe.broker.snapshot_protective_stops.assert_called_once_with("TSLA", side="buy")


def test_reconcile_stop_coverage_naked_short_is_repaired_from_short_row():
    """A short with NO live stop is a real gap and must be auto-repaired
    from the SHORT row the same way a long is repaired from its BUY row.
    Stage 3 writes that row; the old 'no order path can open a short' line
    is false."""

    def _snap(sym, side="sell"):
        assert side == "buy"
        return (True, [])

    pipe = _pipeline_for_reconcile([_mock_position("TSLA", -40.0)], _snap)
    pipe.db.get_symbol_last_buy.return_value = {"stop_loss": 220.0, "action": "SHORT"}
    pipe.broker.get_latest_price.return_value = 200.0
    pipe.broker.STOP_LIMIT_BUFFER_PCT = 0.03
    pipe.broker._submit_protective_stop_retrying.return_value = {"id": "buy-stop-1"}
    gaps = pipe._reconcile_stop_coverage()
    assert len(gaps) == 1
    assert gaps[0]["symbol"] == "TSLA"
    assert gaps[0]["repaired"] is True
    kwargs = pipe.broker._submit_protective_stop_retrying.call_args.kwargs
    assert kwargs["side"] == "buy"
    assert kwargs["stop_price"] == 220.0
    assert kwargs["qty"] == 40.0
    pipe.db.get_symbol_last_buy.assert_called()
    assert pipe.db.get_symbol_last_buy.call_args.kwargs.get("action") == "SHORT"


# ==========================================================================
# 6. The hard boundary, re-proved: shorts still cannot be opened or covered
# ==========================================================================


def test_shorts_can_now_be_opened_and_covered_by_the_constructor():
    """NEW boundary (Stage 3). This test used to pin the Stage-1 guard —
    the constructor produced zero orders against any short, opened or
    held. That guard is gone: opening a fresh short now produces a SHORT
    decision carrying the mirrored stop-above-entry / target-below-entry
    geometry this file exists to test (the same geometry
    `place_entry_protection` above protects with a BUY stop), and closing
    a held short now produces a COVER. Borrow eligibility, the exposure
    caps, and the mandatory protective-stop escalation are execution-layer
    concerns proved end to end in tests/test_shorts_stage3.py — this only
    re-proves the constructor's own half of the boundary.
    """
    from src.models import (
        Position,
        TargetPosition,
        TechAnalysisResult,
        TechReasoningChain,
    )
    from src.portfolio_constructor import PortfolioConstructor

    constructor = PortfolioConstructor()
    rc = TechReasoningChain(trend="x", momentum="x", volatility="x", volume="x", support_resistance="x")
    analysis = TechAnalysisResult(
        symbol="TSLA",
        rating="sell",
        entry_price=250.0,
        stop_loss=262.5,
        reference_target=220.0,
        reasoning="test",
        support_levels=[220.0],
        resistance_levels=[262.5],
        # Python-set in production (TechAnalystAgent), and required since
        # 2026-09-01: the take-profit is derived from the computed levels,
        # not read off the analyst's `reference_target`.
        computed_levels=[220.0, 262.5],
        atr_14=12.5 / 3.5,
        setup_type="range",
        expected_horizon_sessions=60,
        reasoning_chain=rc,
        thesis_invalid_if="closes below support",
    )

    open_decisions = constructor.construct_orders(
        targets=[
            TargetPosition(
                symbol="TSLA", direction="short", target_weight_pct=15.0, conviction="high", thesis="overvalued"
            )
        ],
        positions=[],
        analyses=[analysis],
        total_value=100_000,
        price_map={"TSLA": 250.0},
    )
    assert len(open_decisions) == 1
    opened = open_decisions[0]
    assert opened.action == "SHORT"
    assert opened.stop_loss > opened.entry_price, "a short's stop must sit above entry"
    assert opened.take_profit < opened.entry_price, "a short's target must sit below entry"

    cover_decisions = constructor.construct_orders(
        targets=[TargetPosition(symbol="TSLA", target_weight_pct=0.0, conviction="high", thesis="close it")],
        positions=[
            Position(
                symbol="TSLA",
                qty=-40,
                avg_entry=250,
                current_price=250,
                market_value=-10_000,
                unrealized_pnl=0,
                sector="Consumer Cyclical",
            )
        ],
        analyses=[],
        total_value=100_000,
        price_map={"TSLA": 250.0},
    )
    assert len(cover_decisions) == 1
    assert cover_decisions[0].action == "COVER"


def test_full_sell_qty_refuses_a_short():
    """`_full_sell_qty` gates EVERY sell-side order builder (EMERGENCY_SELL,
    force-delever, take-profit, reviewer SELL/REDUCE). A negative qty must
    keep refusing to produce a sell quantity — this is a second, independent
    reason a short can't be liquidated by any existing order path, and
    Stage 2 must not accidentally open it while fixing the stop side."""
    from src.pipeline import TradingPipeline

    assert TradingPipeline._full_sell_qty(-40.0) is None
    # Long-only unchanged.
    assert TradingPipeline._full_sell_qty(40.0) == 40.0


# --------------------------------------------------------------------------
# fail closed on a side we do not recognise
# --------------------------------------------------------------------------


def test_an_unrecognised_entry_side_is_refused_not_guessed():
    """The fail-OPEN case caught in review.

    `protective_side = "sell" if side == "buy" else "buy"` reads harmlessly,
    but everything that is not exactly "buy" falls into the short branch. A
    typo, a None, or some future side string would put a BUY stop ABOVE a
    LONG — not weak protection, but a standing order to buy more of a
    position that is already losing.

    Refusing is safe: the caller treats a raise like any other protection
    failure and logs the position as naked. Naked-but-known is survivable.
    Naked-and-believed-covered is what costs money.
    """
    from src.execution.broker import AlpacaBroker

    broker = AlpacaBroker.__new__(AlpacaBroker)
    for bad in ("byu", "long", "BUY_TO_COVER", "", "   ", None):
        result = AlpacaBroker.place_entry_protection(
            broker,
            "AAPL",
            "order-1",
            100.0,
            side=bad,
        )
        # No stop, and — critically — no broker call at all. `broker` here is
        # an uninitialised instance with no `.client`, so any attempt to reach
        # the broker would blow up with AttributeError. Returning cleanly
        # proves the guard fires before anything is submitted.
        assert result is None, f"{bad!r} should place no stop"


@pytest.mark.parametrize("side", ["buy", "BUY", " Buy ", "sell", "sell_short", "SELL_SHORT"])
def test_recognised_entry_sides_are_accepted_case_and_space_insensitively(side):
    """The guard must not become brittle: real callers pass mixed case, and
    Alpaca's own enum stringifies upper-case."""
    from src.execution.broker import _ENTRY_SIDES

    assert (side or "").strip().lower() in _ENTRY_SIDES
