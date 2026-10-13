"""docs/WORK.md item 88 — a stop of zero is a REFUSAL, never an absence.

`0.0` was this codebase's sentinel for "no stop", so a garbage or
miscomputed stop of zero removed protection instead of refusing the
trade. These are the guards on the four places a stop VALUE is acted on,
and nothing else: the entry submit, the shared protective-stop placement,
the coverage-repair janitor, and the partial-exit reprotect (whose
`return True` on a zero made the drain DELETE the recovery intent).

The one legitimate absence — `stop_loss_price=None`, the cash-sweep park's
deliberate stopless buy — is pinned here too, because a fix that refused
absence as well would have broken it.
"""

import math
from unittest.mock import MagicMock, patch

import pytest

from src.execution.broker import AlpacaBroker
from src.execution.stop_records import (
    STOP_ABSENT,
    STOP_UNUSABLE,
    STOP_USABLE,
    classify_stop_price,
)
from src.execution.stop_repair import repair_stop_coverage
from tests.pipeline_factory import build_pipeline


@pytest.mark.parametrize("value", [None, "", "   "])
def test_absent_stop_is_named_absent(value):
    assert classify_stop_price(value)[0] == STOP_ABSENT


@pytest.mark.parametrize(
    "value",
    [0, 0.0, -1.0, float("nan"), float("inf"), float("-inf"), "abc"],
)
def test_garbage_stop_is_named_unusable(value):
    """The distinction that IS the fix: supplied-but-garbage is its own
    state, never folded into absence."""
    state, price = classify_stop_price(value)
    assert state == STOP_UNUSABLE
    assert price == 0.0


def test_usable_stop_round_trips():
    assert classify_stop_price("9.66") == (STOP_USABLE, 9.66)


@patch("src.execution.broker.TradingClient")
def test_entry_with_no_stop_requested_still_submits(mock_tc_cls):
    """The cash-sweep park passes `stop_loss_price=None` on purpose. That
    must keep working — absence is legal on that one path."""
    mock_client = MagicMock()
    mock_client.submit_order.return_value = MagicMock(
        id="ord-1",
        status="accepted",
        symbol="BIL",
    )
    mock_tc_cls.return_value = mock_client

    broker = AlpacaBroker(api_key="test", secret_key="test", paper=True)
    result = broker.submit_order(
        symbol="BIL",
        qty=10,
        side="buy",
        limit_price=91.5,
        stop_loss_price=None,
    )
    assert result["status"] == "accepted"
    assert result["pending_stop_price"] is None
    mock_client.submit_order.assert_called_once()


@pytest.mark.parametrize("bad", [0.0, -5.0, float("nan"), float("inf")])
@patch("src.execution.broker.TradingClient")
def test_entry_with_garbage_stop_is_refused_before_the_broker(mock_tc_cls, bad):
    mock_client = MagicMock()
    mock_tc_cls.return_value = mock_client

    broker = AlpacaBroker(api_key="test", secret_key="test", paper=True)
    result = broker.submit_order(
        symbol="X",
        qty=5,
        side="sell_short",
        limit_price=100.0,
        stop_loss_price=bad,
    )
    assert result["status"] == "rejected_bad_stop"
    mock_client.submit_order.assert_not_called()


@pytest.mark.parametrize("bad", [0.0, -1.0, float("nan"), float("-inf")])
@patch("src.execution.broker.TradingClient")
def test_protective_stop_placement_refuses_garbage_without_retrying(
    mock_tc_cls,
    bad,
):
    """The shared placement path every protection lane routes through. A
    garbage trigger is not a transient failure, so it must not burn the
    retry burst, and None (the caller's escalate signal) is the answer —
    never a silently skipped placement reported as done."""
    mock_client = MagicMock()
    mock_tc_cls.return_value = mock_client

    broker = AlpacaBroker(api_key="test", secret_key="test", paper=True)
    assert (
        broker._submit_protective_stop_retrying(
            symbol="X",
            qty=10,
            stop_price=bad,
            side="sell",
        )
        is None
    )
    mock_client.submit_order.assert_not_called()


@pytest.mark.parametrize("bad", [0.0, -1.0, float("nan"), float("inf")])
@patch("src.execution.broker.TradingClient")
def test_stop_limit_order_raises_on_garbage_trigger(mock_tc_cls, bad):
    """The last authority before the broker, for the callers that reach it
    directly. `raise` is the contract those callers already handle."""
    mock_client = MagicMock()
    mock_tc_cls.return_value = mock_client

    broker = AlpacaBroker(api_key="test", secret_key="test", paper=True)
    with pytest.raises(ValueError):
        broker._submit_stop_limit_order(symbol="X", qty=10, stop_price=bad)
    mock_client.submit_order.assert_not_called()


def _repair_broker():
    """A broker stub the repair janitor accepts. `isinstance`, not
    truthiness: a MagicMock's auto-attributes are callable, which is why
    `get_latest_price_stamped` is left off entirely here."""
    broker = MagicMock()
    del broker.get_latest_price_stamped
    broker.get_latest_price.return_value = 100.0
    return broker


@pytest.mark.parametrize("recorded", [0, 0.0, -2.0, float("inf")])
def test_repair_refuses_a_garbage_recorded_stop_and_says_why(recorded):
    """The janitor that exists to CLOSE protection gaps must not place a
    garbage level — and must report why it declined. Before item 88 a
    recorded 0 produced the same "has no recorded stop_loss" line as a row
    that never had one, and the owner alert carried no reason at all."""
    broker = _repair_broker()
    outcome: dict = {}
    assert (
        repair_stop_coverage(
            broker=broker,
            last_buy=lambda sym, action="BUY": {"stop_loss": recorded},
            symbol="X",
            uncovered_qty=5.0,
            is_short=False,
            outcome=outcome,
        )
        is False
    )
    broker._submit_protective_stop_retrying.assert_not_called()
    assert "corrupt" in outcome["repair_refusal"]


def test_repair_distinguishes_a_row_that_never_had_a_stop():
    broker = _repair_broker()
    outcome: dict = {}
    assert (
        repair_stop_coverage(
            broker=broker,
            last_buy=lambda sym, action="BUY": {"stop_loss": None},
            symbol="X",
            uncovered_qty=5.0,
            is_short=False,
            outcome=outcome,
        )
        is False
    )
    assert "was ever recorded" in outcome["repair_refusal"]


def test_reprotect_keeps_the_recovery_intent_when_no_price_is_usable():
    """The fail-open this item was really hiding: `best_stop <= 0` returned
    True, the drain read that as "coverage rebuilt", deleted the persisted
    recovery row, and the residual position was left naked with nothing to
    retry it."""
    from src.pipeline import TradingPipeline

    pipeline = build_pipeline(broker=MagicMock())
    assert (
        pipeline._reprotect_residual_after_partial_sell(
            "X",
            4.0,
            [{"id": "a", "stop_price": 0.0}, {"id": "b"}],
        )
        is False
    )
    pipeline.broker._submit_stop_limit_order.assert_not_called()


def test_reprotect_still_places_the_most_protective_usable_stop():
    """A usable price among the specs must still win, and the long-side
    extreme is the HIGHEST trigger."""
    from src.pipeline import TradingPipeline

    pipeline = build_pipeline(broker=MagicMock(), db=None)
    pipeline.broker._list_open_sell_stop_orders.return_value = []
    pipeline.broker._submit_protective_stop_retrying.return_value = {"id": "s1"}
    assert (
        pipeline._reprotect_residual_after_partial_sell(
            "X",
            4.0,
            [{"id": "a", "stop_price": 0.0}, {"id": "b", "stop_price": 90.0}, {"id": "c", "stop_price": 95.0}],
        )
        is True
    )
    kwargs = pipeline.broker._submit_protective_stop_retrying.call_args.kwargs
    assert kwargs["stop_price"] == 95.0
    assert math.isfinite(kwargs["stop_price"])
