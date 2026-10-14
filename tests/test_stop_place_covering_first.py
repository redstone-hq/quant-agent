"""Stop placement asks "is a covering stop already resting?" on the FIRST refusal.

The only recorded stop-placement failures (2026-09-16, BRK-B DAY sliver;
tests/fixtures/log_health_production_excerpt.txt) were the broker refusing
with 403 / 40310000 held_for_orders because a covering stop was ALREADY
resting. Retrying cannot clear that refusal; the desk's own three-attempt,
two-sleep loop only delayed the covering-stop lookup that answers it. Any
other failure is not retried by the desk either: alpaca-py's REST client
already retries 429/504 itself, and a final failure returns None, which is
the signal every caller escalates on (owner alert, re-protect pass).

Offline: no network, no real broker. Symbols are placeholders.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from src.execution.broker_parts.stop_place import StopPlacer
from tests.fakes.holding_broker import FORBIDDEN_QTY, refusal

SYM = "ZZZ"
STOP = 90.0
QTY = 7.0


def _held_for_orders():
    # Fixed broker wording and fields copied from the 2026-09-16 logged body.
    return refusal(
        403,
        FORBIDDEN_QTY,
        "insufficient qty available for order (requested: 7, available: 0)",
        available="0",
        existing_qty="7",
        held_for_orders="7",
        symbol=SYM,
    )


def _placer(submit, covering):
    return StopPlacer(
        client=MagicMock(),
        list_open_stop_orders_by_side=MagicMock(),
        list_open_protective_stop_orders=MagicMock(),
        list_open_sell_stop_orders=MagicMock(),
        snapshot_stop_order=MagicMock(),
        amend_one_stop_price=MagicMock(),
        amend_resting_stop_price=MagicMock(),
        stop_order_amendable_in_place=MagicMock(),
        cancel_snapshotted_stops=MagicMock(),
        get_positions=MagicMock(),
        kill_switch_active=MagicMock(return_value=False),
        kill_switch_path=MagicMock(),
        protective_stop_block_recorder=MagicMock(),
        submit_stop_limit_order=submit,
        existing_stop_covering_qty=covering,
    )


def test_held_for_orders_goes_straight_to_the_covering_stop_lookup():
    submit = MagicMock(side_effect=_held_for_orders())
    resting = {"id": "YYY-1", "qty": QTY, "stop_price": STOP, "already_live": True}
    covering = MagicMock(return_value=resting)
    with patch("time.sleep") as sleep:
        got = _placer(submit, covering)._submit_protective_stop_retrying(
            symbol=SYM, qty=QTY, stop_price=STOP, side="sell"
        )
    assert submit.call_count == 1, "a held_for_orders refusal must not be resubmitted"
    assert sleep.call_count == 0, "no desk-level backoff before the lookup"
    covering.assert_called_once_with(SYM, qty=QTY, side="sell", stop_price=STOP)
    assert got == resting


def test_held_for_orders_with_nothing_resting_fails_at_once():
    submit = MagicMock(side_effect=_held_for_orders())
    covering = MagicMock(return_value=None)
    with patch("time.sleep") as sleep:
        got = _placer(submit, covering)._submit_protective_stop_retrying(
            symbol=SYM, qty=QTY, stop_price=STOP, side="sell"
        )
    assert (submit.call_count, sleep.call_count, covering.call_count) == (1, 0, 1)
    assert got is None, "None is the caller's escalate signal"


def test_any_other_error_is_not_retried_by_the_desk_and_reaches_the_failure_path():
    submit = MagicMock(side_effect=refusal(500, 50010000, "internal server error"))
    covering = MagicMock()
    with patch("time.sleep") as sleep:
        got = _placer(submit, covering)._submit_protective_stop_retrying(
            symbol=SYM, qty=QTY, stop_price=STOP, side="sell"
        )
    assert submit.call_count == 1, "the vendor client owns retry; the desk adds none"
    assert sleep.call_count == 0
    covering.assert_not_called()
    assert got is None, "None is the caller's escalate signal"
