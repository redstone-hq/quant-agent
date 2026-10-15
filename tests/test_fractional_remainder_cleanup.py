"""Owner ruling 2026-10-15, whole shares only: held sub-share remainders are sold.

A remainder can only carry a DAY stop, so it has no stop overnight. The
coverage sweep sells exactly the remainder of a long, once, while the session
is open and fractional sizing is off, through the protected sell as a trim,
so the whole-share GTC stop is kept and the DAY sliver stop goes with the
shares. Offline: MagicMock pipeline for the sweep, the HoldingBroker fake for
what the broker's book looks like after the sale.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from src import pipeline_protection
from src.protection.trim_amend import finalize_trim_amend
from tests.fakes.holding_broker import OLD, HoldingBroker
from tests.test_partial_sell_keeps_stop import _cancels, _desk, _finalizer, _sell, _sells, _trim

SYMBOL = "XOM"


def _pipeline(held: float, covered: float, *, fractional_enabled=False, open_orders=()):
    pipeline = MagicMock()
    pipeline.config.execution.fractional_enabled = fractional_enabled
    pipeline._unsettled_exit_orders = {}
    pipeline.broker.list_open_orders_checked.return_value = (True, list(open_orders))
    pipeline.broker.get_positions.return_value = [SimpleNamespace(symbol=SYMBOL, qty=held, current_price=110.0)]
    pipeline.db.get_pending_protection_restores.return_value = []
    pipeline._sweeper.return_value = None
    pipeline._repair_stop_coverage.return_value = True
    pipeline._elected_unfilled_stop_row.return_value = None
    pipeline.broker.snapshot_protective_stops.return_value = (True, [{"qty": covered}] if covered else [])
    pipeline._submit_protected_sell.return_value = ({"id": "s1"}, {"symbol": SYMBOL})
    return pipeline


def _sweep(pipeline, *, market_open=True):
    with (
        patch("src.pipeline_protection._market_is_open_now", return_value=market_open),
        patch("src.market_session.market_open_verdict", return_value=(market_open, "")),
        patch("src.trading_calendar.in_regular_session", return_value=market_open),
        patch("src.pipeline_protection.drain_owed_stop_levels"),
        patch("src.notifier.send_owner_alert"),
    ):
        return pipeline_protection.ProtectionMixin._reconcile_stop_coverage(pipeline)


def test_flag_off_market_open_sells_exactly_the_remainder_with_the_reason():
    pipeline = _pipeline(36.688, 36.688)

    _sweep(pipeline)

    pipeline._submit_protected_sell.assert_called_once()
    kw = pipeline._submit_protected_sell.call_args.kwargs
    assert kw["qty"] == 0.688  # exactly the remainder: no float dust left behind
    assert kw["position_qty_before_sell"] == 36.688
    assert kw["label"] == "REDUCE" and kw["side"] == "sell"
    assert pipeline._record_exit_refusal.call_args.kwargs["code"] == "fractional_remainder_cleanup"
    assert pipeline.db.insert_trade.call_args.kwargs["reasoning"] == "fractional_remainder_cleanup"
    pipeline._finalize_pending_protections.assert_called_once()


def test_second_pass_with_the_cleanup_order_open_sends_nothing():
    working = SimpleNamespace(symbol=SYMBOL, side="sell", order_type="market", qty="0.688")
    pipeline = _pipeline(36.688, 36.0, open_orders=[working])

    _sweep(pipeline)

    pipeline._submit_protected_sell.assert_not_called()


def test_after_the_fill_the_whole_share_position_is_left_alone():
    pipeline = _pipeline(36.0, 36.0)

    gaps = _sweep(pipeline)

    pipeline._submit_protected_sell.assert_not_called()
    assert [g for g in gaps if g.get("symbol") == SYMBOL] == []


@pytest.mark.parametrize(
    "kwargs, market_open",
    [({}, False), ({"fractional_enabled": True}, True)],
    ids=["market_closed", "flag_on"],
)
def test_no_sale_when_closed_or_fractional_sizing_is_on(kwargs, market_open):
    pipeline = _pipeline(36.688, 36.688, **kwargs)

    _sweep(pipeline, market_open=market_open)

    pipeline._submit_protected_sell.assert_not_called()


def test_a_short_is_never_touched():
    pipeline = _pipeline(-36.688, 36.688)
    pipeline.broker.snapshot_protective_stops.return_value = (True, [{"qty": 36.688}])

    _sweep(pipeline)

    pipeline._submit_protected_sell.assert_not_called()


@pytest.fixture
def alerts(monkeypatch):
    sent: list[str] = []
    monkeypatch.setattr("src.notifier.send_owner_alert", lambda text, **kw: sent.append(text))
    return sent


@pytest.mark.parametrize("day_leg", [True, False], ids=["day_leg_live", "day_leg_lapsed"])
def test_after_the_sale_the_stop_covers_exactly_the_whole_shares(alerts, day_leg):
    """Step 2, proved against the fake broker: the existing trim path keeps the
    36-share GTC stop, cancels only the 0.688 DAY sliver, and leaves no
    fractional leg."""
    fake = HoldingBroker(36.688)
    fake.rest(36)
    if day_leg:
        fake.rest(0.688)
    b = _desk(fake)
    ps, _ = _sell(fake, b)

    sale = _trim(ps, 0.688, 36.688)

    assert sale is not None
    _, prot = sale
    assert _sells(fake) == [0.688]
    assert _cancels(fake) == (["o2"] if day_leg else [])
    assert finalize_trim_amend(_finalizer(b), prot) is True
    assert fake.held == pytest.approx(36.0)
    assert fake.book() == [(36.0, OLD)]
    assert alerts == []
