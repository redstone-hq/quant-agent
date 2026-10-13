"""Boundary witness: the lifted stop-amend piece builds and runs with no broker or pipeline behind it.

Every collaborator is an explicit keyword-only constructor argument, so the
class is built from stubs alone (clause 5 of tests/boundary_harness.py).
"""

from __future__ import annotations

import inspect
from unittest.mock import MagicMock

from src.execution.broker_parts.stop_amend import (
    StopAmender,
    _AMEND_NOT_ATTEMPTED,
    _is_terminal_broker_rejection,
    _quantize_price,
)
from src.execution.broker_parts.stop_place import (
    StopPlacer,
    _STOP_PLACEMENT_MAX_ATTEMPTS,
    _is_held_for_orders_error,
    _split_protective_qty,
)
from tests.boundary_harness import check_boundary


def _build(cls, **overrides):
    params = inspect.signature(cls).parameters
    kwargs = {name: MagicMock(name=name) for name in params}
    kwargs.update(overrides)
    return cls(**kwargs)


def test_stop_amender_is_constructible_from_stubs():
    _build(StopAmender)
    params = inspect.signature(StopAmender).parameters
    assert all(p.kind is inspect.Parameter.KEYWORD_ONLY for p in params.values())


def test_stop_amend_module_passes_the_boundary_check():
    verdict = check_boundary("src.execution.broker_parts.stop_amend")
    assert verdict.passed, verdict.failures


def test_moved_helpers_still_resolve_on_the_broker_module():
    import src.execution.broker as broker_module

    assert broker_module._AMEND_NOT_ATTEMPTED is _AMEND_NOT_ATTEMPTED
    assert broker_module._is_terminal_broker_rejection is _is_terminal_broker_rejection
    assert broker_module._quantize_price is _quantize_price
    assert broker_module.AlpacaBroker._AMEND_DEAD_STATES is StopAmender._AMEND_DEAD_STATES
    assert broker_module.AlpacaBroker._failed_amend_payload is StopAmender._failed_amend_payload
    assert broker_module.AlpacaBroker._stop_order_amendable_in_place is StopAmender._stop_order_amendable_in_place


def test_amend_refusal_without_a_broker_object():
    """A stand-alone amender: the broker's replace call raising a terminal
    rejection leaves the original stop resting and reports the refusal."""
    client = MagicMock(name="client")
    client.replace_order_by_id.side_effect = RuntimeError("rejected")
    amender = StopAmender(
        client=client,
        list_open_stop_orders_by_side=MagicMock(return_value=[]),
        snapshot_stop_order=MagicMock(return_value=None),
    )
    out = amender._amend_one_stop_price(
        symbol="ZZZ", spec={"id": "s1", "qty": 1, "stop_price": 9.0, "limit_price": None}, new_price=9.5
    )
    assert isinstance(out, dict)
    client.replace_order_by_id.assert_called_once()


def test_stop_placer_is_constructible_from_stubs():
    _build(StopPlacer)
    params = inspect.signature(StopPlacer).parameters
    assert all(p.kind is inspect.Parameter.KEYWORD_ONLY for p in params.values())


def test_stop_place_module_passes_the_boundary_check():
    verdict = check_boundary("src.execution.broker_parts.stop_place")
    assert verdict.passed, verdict.failures


def test_stop_place_helpers_still_resolve_on_the_broker_module():
    import src.execution.broker as broker_module

    assert broker_module._STOP_PLACEMENT_MAX_ATTEMPTS is _STOP_PLACEMENT_MAX_ATTEMPTS
    assert broker_module._is_held_for_orders_error is _is_held_for_orders_error
    assert broker_module._split_protective_qty is _split_protective_qty


def test_broker_shim_passes_its_own_cluster_methods_so_instance_doubles_land():
    """The shim builds the placer per call from the broker's CURRENT bound
    methods, so a test that swaps `_restore_stop_orders` on the instance after
    construction is what the lifted `shift_stops_down` body sees."""
    import src.execution.broker as broker_module

    broker = object.__new__(broker_module.AlpacaBroker)
    broker.client = MagicMock(name="client")
    broker._restore_stop_orders = MagicMock(name="restore", return_value=(0, []))
    placer = broker._stop_placer()
    assert placer._restore_stop_orders is broker._restore_stop_orders
    assert placer.client is broker.client


def test_existing_stop_cover_lookup_without_a_broker_object():
    """A stand-alone placer: with no resting protective stop the cover lookup
    asks the lister once and reports nothing to reuse."""
    lister = MagicMock(name="list_open_protective_stop_orders", return_value=[])
    placer = _build(StopPlacer, list_open_protective_stop_orders=lister, existing_stop_covering_qty=None)
    out = placer._existing_stop_covering_qty("ZZZ", qty=1.0, side="sell", stop_price=9.0)
    assert out is None
    lister.assert_called_once()


# --- third instalment: OrderDesk + AccountReads -------------------------------


def test_order_desk_is_constructible_from_stubs():
    from src.execution.broker_parts.order_desk import OrderDesk

    _build(OrderDesk)
    params = inspect.signature(OrderDesk).parameters
    assert all(p.kind is inspect.Parameter.KEYWORD_ONLY for p in params.values())


def test_order_desk_module_passes_the_boundary_check():
    verdict = check_boundary("src.execution.broker_parts.order_desk")
    assert verdict.passed, verdict.failures


def test_account_reads_is_constructible_from_stubs():
    from src.execution.broker_parts.account_reads import AccountReads

    _build(AccountReads)
    params = inspect.signature(AccountReads).parameters
    assert all(p.kind is inspect.Parameter.KEYWORD_ONLY for p in params.values())


def test_account_reads_module_passes_the_boundary_check():
    verdict = check_boundary("src.execution.broker_parts.account_reads")
    assert verdict.passed, verdict.failures


def test_order_desk_helpers_still_resolve_on_the_broker_module():
    import src.execution.broker as broker_module
    from src.execution.broker_parts import order_desk

    assert broker_module._outlier_refusal_detail is order_desk._outlier_refusal_detail
    assert broker_module._is_terminal_submission_rejection is order_desk._is_terminal_submission_rejection
    assert broker_module._PLAIN_PRICE_LABELS is order_desk._PLAIN_PRICE_LABELS


def test_broker_factories_never_hand_the_desk_its_own_shim():
    """The recursion guard: a broker whose attributes are the shims passes
    none of the desk-owned bodies, and passes a replacement bound on the
    instance; the reads object shares the broker's own cache dicts."""
    from unittest.mock import patch
    from src.execution.broker import AlpacaBroker
    from src.execution.broker_parts.order_desk import OrderDesk

    with patch("src.execution.broker.TradingClient"):
        broker = AlpacaBroker("key", "secret")
    desk = broker._order_desk()
    assert desk.wait_for_order_terminal.__func__ is OrderDesk.wait_for_order_terminal
    assert "resolve_replacement_chain" not in desk.__dict__
    stand_in = MagicMock(name="stand_in")
    broker.resolve_replacement_chain = stand_in
    assert broker._order_desk().resolve_replacement_chain is stand_in
    reads = broker._account_reads()
    assert "_session_edge" not in reads.__dict__
    assert reads._shortable_cache is broker._shortable_cache
    assert reads._fractionable_cache is broker._fractionable_cache
    assert reads.client is broker.client


# --- fourth (final) instalment: trade_stream + market_data -----------------


def test_trade_stream_waits_is_constructible_from_stubs():
    from src.execution.broker_parts.trade_stream import TradeStreamWaits

    _build(TradeStreamWaits)
    params = inspect.signature(TradeStreamWaits).parameters
    assert all(p.kind is inspect.Parameter.KEYWORD_ONLY for p in params.values())


def test_trade_stream_module_passes_the_boundary_check():
    verdict = check_boundary("src.execution.broker_parts.trade_stream")
    # Clause 1 names exactly the two value classes whose __init__ the AST
    # cannot see: a @dataclass (generated) and an Exception (inherited).
    # They moved verbatim; nothing else may fail.
    assert set(verdict.failures) <= {1}, verdict.failures
    assert set(verdict.failures.get(1, [])) == {
        "TradeStreamWarmup: no __init__",
    }, verdict.failures


def test_market_data_is_constructible_from_stubs():
    from src.execution.broker_parts.market_data import MarketData

    _build(MarketData)
    params = inspect.signature(MarketData).parameters
    assert all(p.kind is inspect.Parameter.KEYWORD_ONLY for p in params.values())


def test_market_data_module_passes_the_boundary_check():
    verdict = check_boundary("src.execution.broker_parts.market_data")
    # Clause 1 names exactly the frozen @dataclass `LivePrice`, whose __init__
    # is generated; it moved verbatim. Nothing else may fail.
    assert set(verdict.failures) <= {1}, verdict.failures
    assert verdict.failures.get(1) == ["LivePrice: no __init__"], verdict.failures


def test_moved_stream_and_market_names_still_resolve_on_the_broker_module():
    import src.execution.broker as broker_module
    from src.execution.broker_parts import market_data, trade_stream

    assert broker_module._TradeUpdatesHub is trade_stream._TradeUpdatesHub
    assert broker_module._STREAM_ATTEMPT_BUDGET is trade_stream._STREAM_ATTEMPT_BUDGET
    assert broker_module._install_trading_stream_reconnect_guard is trade_stream._install_trading_stream_reconnect_guard
    assert broker_module.LivePrice is market_data.LivePrice
    assert broker_module._install_http_timeout is market_data._install_http_timeout


def test_patch_on_the_broker_module_reaches_the_moved_body(monkeypatch):
    """`monkeypatch.setattr("src.execution.broker.TradingStream", ...)` must still
    be what the moved stream code sees, and the flags the stream code rebinds
    with `global` must read through from the part."""
    import src.execution.broker as broker_module
    from src.execution.broker_parts import trade_stream

    sentinel = object()
    monkeypatch.setattr("src.execution.broker.TradingStream", sentinel)
    assert trade_stream.TradingStream is sentinel
    monkeypatch.setattr(broker_module, "_stream_auth_deprecation_logged", True)
    from src.execution.broker_parts import trade_stream_auth

    assert trade_stream_auth._stream_auth_deprecation_logged is True
    assert broker_module._stream_auth_deprecation_logged is True
    assert "_stream_auth_deprecation_logged" not in vars(broker_module)


def test_market_data_state_is_the_broker_itself():
    """`_data_client` is created lazily, after construction; the per-call object
    reads and writes it on the broker (`state`), so the lazy client persists."""
    from src.execution.broker_parts.market_data import MarketData

    class Host:
        _data_client = None

    host = Host()
    md = _build(MarketData, state=host)
    assert md._data_client is None
    md._data_client = "client"
    assert host._data_client == "client"


# --- StopShifter: the ex-dividend shift as a part (was ShiftStopsMixin) -------


def _one_amendable_stop(**overrides):
    """Stubs for a single resting stop that the measured-safe amend covers."""
    from src.execution.broker_parts.stop_shifter import StopShifter

    order = MagicMock(name="order", id="o1")
    spec = {"id": "o1", "qty": 3, "stop_price": 10.0, "limit_price": None}
    leg = {"outcome": "amended", "id": "o1", "new_id": "o2", "old_stop": 10.0, "new_stop": 9.5, "qty": 3, "detail": ""}
    stubs = dict(
        client=MagicMock(name="client"),
        list_open_sell_stop_orders=lambda symbol: [order],
        snapshot_stop_order=lambda o: dict(spec),
        stop_order_amendable_in_place=lambda o: True,
        amend_one_stop_price=MagicMock(name="amend", return_value=leg),
        cancel_snapshotted_stops=MagicMock(name="cancel"),
        restore_stop_orders=MagicMock(name="restore"),
    )
    stubs.update(overrides)
    return StopShifter, stubs


def test_stop_shifter_is_constructible_from_stubs():
    from src.execution.broker_parts.stop_shifter import StopShifter

    _build(StopShifter)
    params = inspect.signature(StopShifter).parameters
    assert all(p.kind is inspect.Parameter.KEYWORD_ONLY for p in params.values())


def test_stop_shifter_module_passes_the_boundary_check():
    verdict = check_boundary("src.execution.broker_parts.stop_shifter")
    assert verdict.passed, verdict.failures


def test_stop_shift_shim_module_passes_the_boundary_check():
    verdict = check_boundary("src.execution.broker_parts.stop_shift")
    assert verdict.passed, verdict.failures


def test_shift_stops_down_runs_from_stubs_without_a_placer_or_broker():
    """The boundary: the amend path runs end to end on stubs alone, cancels
    nothing, and reports the confirmed shift. No StopPlacer, no broker."""
    StopShifter, stubs = _one_amendable_stop()
    out = StopShifter(**stubs).shift_stops_down("GE", 0.5)
    assert out["status"] == "accepted" and out["mode"] == "amend"
    assert out["shifted"] == 1 and out["total"] == 1 and out["id"] == "o2"
    stubs["amend_one_stop_price"].assert_called_once_with(
        symbol="GE", spec={"id": "o1", "qty": 3, "stop_price": 10.0, "limit_price": None}, new_price=9.5
    )
    stubs["cancel_snapshotted_stops"].assert_not_called()
    stubs["restore_stop_orders"].assert_not_called()


def test_shift_body_lives_in_the_part_and_the_shim_only_forwards():
    """stop_shift.shift_stops_down must be a one-return forwarder bound onto
    StopPlacer; the body is in StopShifter and no mixin remains. A body
    creeping back into the shim is a regression to the mixin design."""
    import ast
    from src.execution.broker_parts import stop_shift, stop_shifter

    tree = ast.parse(inspect.getsource(stop_shift))
    assert not [n for n in tree.body if isinstance(n, ast.ClassDef)]
    shim = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "shift_stops_down")
    stmts = [s for s in shim.body if not (isinstance(s, ast.Expr) and isinstance(s.value, ast.Constant))]
    assert len(stmts) == 1 and isinstance(stmts[0], ast.Return)
    assert StopPlacer.shift_stops_down is stop_shift.shift_stops_down
    part = ast.parse(inspect.getsource(stop_shifter))
    cls = next(n for n in part.body if isinstance(n, ast.ClassDef) and n.name == "StopShifter")
    body = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == "shift_stops_down")
    assert len(body.body) > 10


def test_shim_hands_the_placer_collaborators_in_live_not_at_construction():
    """The seam witness. A collaborator swapped on the placer AFTER it is built
    (how the broker shim and tests patch) must be the one the moved body calls.
    Snapshotting at construction would call the stale one forever."""
    _, stubs = _one_amendable_stop()
    placer = _build(
        StopPlacer,
        client=stubs["client"],
        list_open_sell_stop_orders=stubs["list_open_sell_stop_orders"],
        snapshot_stop_order=stubs["snapshot_stop_order"],
        stop_order_amendable_in_place=stubs["stop_order_amendable_in_place"],
        amend_one_stop_price=MagicMock(
            name="stale",
            return_value={
                "outcome": "refused",
                "id": "o1",
                "new_id": None,
                "old_stop": 10.0,
                "new_stop": 9.5,
                "qty": 3,
                "detail": "stale",
            },
        ),
        cancel_snapshotted_stops=stubs["cancel_snapshotted_stops"],
    )
    placer.shift_stops_down("GE", 0.5)  # first call builds a shifter
    placer._amend_one_stop_price = stubs["amend_one_stop_price"]  # swap after
    out = placer.shift_stops_down("GE", 0.5)
    assert out["status"] == "accepted", out
    stubs["amend_one_stop_price"].assert_called_once()
    stubs["cancel_snapshotted_stops"].assert_not_called()


def test_shift_helpers_still_resolve_on_the_shim_module():
    from src.execution.broker_parts import stop_shift, stop_shifter

    assert stop_shift._quantize_price is stop_shifter._quantize_price
    assert stop_shift.defer_shift_if_closed is stop_shifter.defer_shift_if_closed
