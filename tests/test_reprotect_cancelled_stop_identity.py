"""INCIDENT 2026-09-30 (AAPL) — the reprotect idempotency check must not be
satisfied by the stop THIS run just cancelled.

Production log, 14:47:23: the WAL intent was written, 2 protective stops
were cancelled, the SELL went out, and 486ms later the idempotency check
listed those same just-cancelled orders as still open (Alpaca's cancel is
asynchronous and its OPEN filter includes `pending_cancel`), matched
`best_stop`, logged "idempotent re-run", returned True — so the drain
caller deleted the recovery row and ~$2,500 sat with no stop and no record
that one was owed.

The distinction is by broker order IDENTITY, not by timing and not by a
tolerance. These tests pin both directions: the just-cancelled order must
NOT satisfy the check (a stop is placed), and a genuinely live stop from a
PREVIOUS attempt must still satisfy it (the duplicate-stop protection the
2026-05-27 audit note describes is not weakened).
"""

import functools
from unittest.mock import MagicMock

from src.execution.broker import AlpacaBroker
from src.pipeline import TradingPipeline
from tests.pipeline_factory import build_pipeline


def _order(order_id, stop_price, status="new", qty=2.43):
    # `qty` MATTERS: the idempotency check requires an open stop to cover
    # the WHOLE residual before it counts as this position's protection (a
    # leftover sliver stop is not coverage). The residual in these tests is
    # 2.43 shares, so a stop that is genuinely protection carries that qty.
    o = MagicMock()
    o.id = order_id
    o.stop_price = stop_price
    o.status = status
    o.qty = qty
    return o


def _pipeline(existing):
    p = build_pipeline(broker=MagicMock())
    p.broker._list_open_sell_stop_orders.return_value = existing
    p.broker._list_open_protective_stop_orders.return_value = existing
    p.broker._submit_stop_limit_order.return_value = {
        "id": "new-stop-1",
        "status": "accepted",
    }
    # The reprotect path submits through the desk's ONE protective-stop
    # submit, not the raw order call, so the test drives the real thing
    # (leg split, DAY-sliver-first ordering, retry burst) over a mocked
    # `_submit_stop_limit_order`. Binding the real functions rather than
    # stubbing them is what makes these tests able to see defect 4.
    # Both bodies now live on the standalone StopPlacer and the broker keeps
    # a same-named shim that builds one. Bind the real factory too, so the
    # shims below reach the real bodies over this mock's collaborators
    # instead of a child mock. Same functions, new home -- nothing stubbed.
    p.broker._stop_placer = functools.partial(
        AlpacaBroker._stop_placer,
        p.broker,
    )
    p.broker._submit_stop_leg_retrying = functools.partial(
        AlpacaBroker._submit_stop_leg_retrying,
        p.broker,
    )
    p.broker._submit_protective_stop_retrying = functools.partial(
        AlpacaBroker._submit_protective_stop_retrying,
        p.broker,
    )
    p.db = None
    return p


def test_just_cancelled_stop_does_not_satisfy_idempotency():
    """THE INCIDENT. The open stop the broker still lists is the very order
    whose id is in `cancelled_specs` — a stop MUST be submitted."""
    specs = [{"id": "ord-A", "qty": 2.43, "stop_price": 323.74}]
    p = _pipeline([_order("ord-A", 323.74, status="pending_cancel")])

    assert p._reprotect_residual_after_partial_sell("AAPL", 2.43, specs) is True
    assert p.broker._submit_stop_limit_order.called, (
        "reprotect skipped on the stop it had just cancelled — this is the 2026-09-30 naked-position incident"
    )


def test_live_stop_from_a_previous_attempt_still_skips():
    """The duplicate-stop protection the check exists for is UNCHANGED: a
    live stop at the same price that this run did NOT cancel still wins."""
    specs = [{"id": "ord-A", "qty": 2.43, "stop_price": 323.74}]
    p = _pipeline([_order("ord-PREVIOUS", 323.74, status="new")])

    assert p._reprotect_residual_after_partial_sell("AAPL", 2.43, specs) is True
    assert not p.broker._submit_stop_limit_order.called


def test_pending_cancel_order_is_never_protection():
    """Belt and braces: a foreign order that is dying is not coverage."""
    specs = [{"id": "ord-A", "qty": 2.43, "stop_price": 323.74}]
    p = _pipeline([_order("ord-OTHER", 323.74, status="pending_cancel")])

    assert p._reprotect_residual_after_partial_sell("AAPL", 2.43, specs) is True
    assert p.broker._submit_stop_limit_order.called


def test_specs_without_ids_fail_toward_submitting():
    """Ambiguity places a stop: identity could not be established."""
    specs = [{"qty": 2.43, "stop_price": 323.74}]
    p = _pipeline([_order("ord-UNKNOWN", 323.74, status="new")])

    assert p._reprotect_residual_after_partial_sell("AAPL", 2.43, specs) is True
    assert p.broker._submit_stop_limit_order.called


def test_submit_failure_pages_the_owner():
    """The silence was its own defect: a refusal that leaves the residual
    unprotected goes down the existing NO-STOP escalation."""
    specs = [{"id": "ord-A", "qty": 2.43, "stop_price": 323.74}]
    p = _pipeline([])
    p.broker._submit_stop_limit_order.side_effect = RuntimeError("boom")
    p._alert_owner_no_stop = MagicMock()

    assert p._reprotect_residual_after_partial_sell("AAPL", 2.43, specs) is False
    assert p._alert_owner_no_stop.called
    (naked,), _ = p._alert_owner_no_stop.call_args
    assert naked[0]["symbol"] == "AAPL"
    assert "re-protect" in naked[0]["repair_refusal"]


def test_unusable_spec_price_pages_the_owner():
    specs = [{"id": "ord-A", "qty": 2.43, "stop_price": 0}]
    p = _pipeline([])
    p._alert_owner_no_stop = MagicMock()

    assert p._reprotect_residual_after_partial_sell("AAPL", 2.43, specs) is False
    assert p._alert_owner_no_stop.called


# ---------------------------------------------------------------------------
# The four defects the adversary pass found on this branch, 2026-09-30.
# ---------------------------------------------------------------------------


def test_pending_new_stop_is_neither_banked_nor_duplicated():
    """ADVERSARY ROUND 2, DEFECT 2 — this REPLACES an earlier test that
    asserted `pending_new` counts as protection.

    That assertion was wrong in a way that costs money: a `pending_new`
    order has been received and not routed and can still go to `rejected`,
    so banking it wrote a stop price into the desk's own record that the
    broker may refuse seconds later AND drained the recovery intent, with
    only the coverage sweep left to notice — and that sweep defers repair
    while a trading session holds the lock, which is exactly when this
    runs. Placing a second stop over it is equally unsafe. The third
    outcome: return False, write nothing back, keep the intent alive.
    """
    specs = [{"id": "ord-A", "qty": 2.43, "stop_price": 323.74}]
    p = _pipeline([_order("ord-PREVIOUS", 323.74, status="pending_new")])
    written = []
    import src.execution.stop_records as sr

    real = sr.write_back_stop_loss
    sr.write_back_stop_loss = lambda *a, **k: written.append(a)
    try:
        result = p._reprotect_residual_after_partial_sell("AAPL", 2.43, specs)
    finally:
        sr.write_back_stop_loss = real

    assert result is False, "an in-flight stop was banked as protection and the recovery intent drained"
    assert not p.broker._submit_stop_limit_order.called, (
        "a second stop was submitted over an in-flight stop — the duplicate"
    )
    assert not written, "a stop the broker has not yet routed was recorded"


def test_identity_decides_even_when_the_price_differs():
    """ADVERSARY ROUND 2, DEFECT 1/3. A live stop from a PREVIOUS attempt
    resting a cent away from the price this run wanted must still block the
    submit, and the price RECORDED is the one actually resting. The old
    half-penny window made that stop invisible and put a second live stop on
    the same shares; there is no tolerance constant left in this path."""
    specs = [{"id": "ord-A", "qty": 2.43, "stop_price": 323.74}]
    p = _pipeline([_order("ord-PREVIOUS", 323.73, status="new")])
    written = []
    import src.execution.stop_records as sr

    real = sr.write_back_stop_loss
    sr.write_back_stop_loss = lambda db, sym, price, **k: written.append(price)
    try:
        result = p._reprotect_residual_after_partial_sell("AAPL", 2.43, specs)
    finally:
        sr.write_back_stop_loss = real

    assert result is True
    assert not p.broker._submit_stop_limit_order.called, (
        "a duplicate stop was placed over a live stop one cent away — the incident's own price-first filter"
    )
    assert written == [323.73], "the desk recorded the stop it WANTED, not the one resting at the broker"


def test_unprovable_identity_is_recorded_durably_before_any_price_filter():
    """ADVERSARY ROUND 2, DEFECT 4. The completeness flag is loop-invariant.
    Evaluated inside the loop after the price/quantity filters it was never
    consulted when nothing matched, so the submit happened in silence. Here
    the only open stop fails the quantity filter, so the old ordering could
    not have reached the flag at all."""
    specs = [{"id": None, "qty": 2.43, "stop_price": 323.74}]
    p = _pipeline([_order("ord-PREVIOUS", 999.00, status="new", qty=0.1)])
    recorded = []
    p._record_exit_refusal = lambda **kw: recorded.append(kw)

    assert p._reprotect_residual_after_partial_sell("AAPL", 2.43, specs) is True
    assert p.broker._submit_stop_limit_order.called
    assert recorded, (
        "the desk submitted over an unidentifiable broker stop and left no durable per-symbol record of why"
    )
    assert recorded[0]["symbol"] == "AAPL"
    assert recorded[0]["code"] == "reprotect_broker_state_unprovable"
    assert "order id" in recorded[0]["detail"]


def test_no_price_tolerance_literal_survives_in_the_reprotect_path():
    """DOCTRINE. The 0.005 window was a chosen constant justified by an
    asserted float<->Decimal round-trip that was never measured, and as a
    bare literal in a comparison it was invisible to the number scanner by
    construction. The need for it is removed, not re-justified."""
    import inspect
    from src.pipeline import TradingPipeline as _TP

    body = inspect.getsource(_TP._reprotect_residual_after_partial_sell)
    code = "\n".join(line for line in body.splitlines() if not line.lstrip().startswith("#"))
    assert "0.005" not in code


def test_pending_cancel_is_still_not_protection_in_the_wider_set():
    """DEFECT 1, the other direction: widening the set must not admit a
    DYING order. `pending_cancel` is excluded from both status sets."""
    from src.execution.broker import (
        PROTECTIVE_ORDER_ACTIVE_STATUSES,
        PROTECTIVE_ORDER_ALIVE_STATUSES,
    )

    assert "pending_cancel" not in PROTECTIVE_ORDER_ALIVE_STATUSES
    assert "pending_cancel" not in PROTECTIVE_ORDER_ACTIVE_STATUSES
    # One definition, extended by name — not a second copy of the literals.
    assert PROTECTIVE_ORDER_ACTIVE_STATUSES < PROTECTIVE_ORDER_ALIVE_STATUSES
    assert "pending_new" in PROTECTIVE_ORDER_ALIVE_STATUSES


def test_stringified_none_id_is_not_a_real_id():
    """DEFECT 2. `_snapshot_stop_order` stamps `str(order.id)`, so a
    missing id arrives as the TRUTHY string "None"; the old truthiness
    filter counted it as present, `ids_complete` stayed True, and it then
    matched no open order — so every replay submitted."""
    from src.execution.broker import real_broker_order_id

    assert real_broker_order_id(None) == ""
    assert real_broker_order_id("None") == ""
    assert real_broker_order_id("") == ""
    assert real_broker_order_id("ord-A") == "ord-A"

    specs = [{"id": str(None), "qty": 2.43, "stop_price": 323.74}]
    p = _pipeline([_order("ord-PREVIOUS", 323.74, status="new")])
    # The id is not real, so no open stop may satisfy the check: the run
    # cannot prove the open stop is not the one it just cancelled.
    assert p._reprotect_residual_after_partial_sell("AAPL", 2.43, specs) is True
    assert p.broker._submit_stop_limit_order.called


def test_partial_cover_reports_the_quantity_actually_covered():
    """DEFECT 3. The alert hardcoded `covered_qty` to 0, so a landed
    whole-share leg was reported to the owner as ZERO coverage — an untrue
    statement about how exposed the position is."""
    specs = [{"id": "ord-A", "qty": 2.43, "stop_price": 323.74}]
    p = _pipeline([])
    p._alert_owner_no_stop = MagicMock()

    calls = []

    def _submit(*, symbol, qty, stop_price, limit_price=None, side="sell"):
        calls.append(qty)
        if qty < 1:  # the DAY sub-share sliver is refused
            raise RuntimeError("sliver refused")
        return {"id": "gtc-1", "status": "accepted"}

    p.broker._submit_stop_limit_order = _submit

    assert p._reprotect_residual_after_partial_sell("AAPL", 2.43, specs) is False
    assert p._alert_owner_no_stop.called
    (naked,), _ = p._alert_owner_no_stop.call_args
    assert float(naked[0]["covered_qty"]) == 2.0, "the whole-share leg landed; reporting 0 covered is a lie"
    assert float(naked[0]["held_qty"]) == 2.43


def test_fractional_sliver_is_submitted_before_the_whole_share_gtc():
    """DEFECT 4. Measured 2026-09-16: placing the whole-share GTC leg
    first made the GTC hold reserve the position and Alpaca refused the
    sub-share DAY sliver with held_for_orders. The shared submit places
    the remainder FIRST; the old hand-rolled loop here did the opposite."""
    specs = [{"id": "ord-A", "qty": 2.43, "stop_price": 323.74}]
    p = _pipeline([])
    order = []

    def _submit(*, symbol, qty, stop_price, limit_price=None, side="sell"):
        order.append(qty)
        return {"id": f"leg-{len(order)}", "status": "accepted"}

    p.broker._submit_stop_limit_order = _submit

    assert p._reprotect_residual_after_partial_sell("AAPL", 2.43, specs) is True
    assert len(order) == 2
    assert order[0] < 1 < order[1], f"sliver must be placed before the whole-share GTC leg, got {order}"


def test_a_refusal_is_not_retried_by_the_desk_and_is_reported_naked():
    """2026-10-14: the desk's retry burst is deleted (alpaca-py retries
    429/504 itself). One refusal is reported as a failed re-protect at once,
    for the owner alert and the coverage sweep to take over."""
    specs = [{"id": "ord-A", "qty": 3.0, "stop_price": 323.74}]
    p = _pipeline([])
    p._alert_owner_no_stop = MagicMock()
    attempts = []

    def _submit(*, symbol, qty, stop_price, limit_price=None, side="sell"):
        attempts.append(qty)
        if len(attempts) == 1:
            raise RuntimeError("transient broker blip")
        return {"id": "gtc-1", "status": "accepted"}

    p.broker._submit_stop_limit_order = _submit

    assert p._reprotect_residual_after_partial_sell("AAPL", 3.0, specs) is False
    assert len(attempts) == 1
