"""Boundary witnesses: the position builder's two held parts build and run with no owner behind them.

`PortfolioConstructor` inherits from nothing; `StopRules` and `OrderBuilders` are
HELD instances wired by src/portfolio_constructor/assembly.py. Each is constructed
here from stubs alone and exercised without a PortfolioConstructor existing.
"""

from __future__ import annotations

import inspect
from unittest.mock import MagicMock

import pytest

from src.portfolio_constructor.assembly import (
    ORDER_DELEGATES,
    STATIC_EXIT_DELEGATES,
    STOP_DELEGATES,
    order_builder_collaborators,
)
from src.portfolio_constructor.orders import _ORDER_BUILDER_COLLABORATORS, OrderBuilders
from src.portfolio_constructor.order_build.long_entry import LongEntryBuilder
from src.portfolio_constructor.stops import StopRules
from tests.boundary_harness import check_boundary

MODULES = ["src.portfolio_constructor.stops", "src.portfolio_constructor.orders", "src.portfolio_constructor.assembly"]


@pytest.mark.parametrize("module", MODULES)
def test_every_part_module_passes_the_boundary_check(module):
    verdict = check_boundary(module)
    assert verdict.passed, verdict.failures


@pytest.mark.parametrize("cls", [StopRules, OrderBuilders])
def test_parts_take_keyword_only_collaborators(cls):
    params = inspect.signature(cls).parameters
    assert params and all(p.kind is inspect.Parameter.KEYWORD_ONLY for p in params.values())


def test_stop_rules_exercised_from_stubs_alone():
    cfg = MagicMock(name="cfg")
    rules = StopRules(read_cfg=lambda: cfg, entry_stop_resolver=MagicMock(name="resolver_factory"))
    target = MagicMock(symbol="XYZ", suggested_stop_price=9.5)
    assert rules._resolve_stop(target, None, 10.0) == 9.5  # typed stop wins
    assert (
        rules._resolve_stop(MagicMock(symbol="XYZ", suggested_stop_price=None), MagicMock(stop_loss=9.0), 10.0) == 9.0
    )
    assert rules._reward_risk_at(10.0, 9.0, 12.0, False) == pytest.approx(2.0)
    assert rules._reward_risk_at(10.0, float("nan"), 12.0, False) is None  # malformed geometry refuses
    assert rules.shipped_stop_rule(None, 10.0, 9.0) == "stop_rule_not_determined"  # no analysis -> explicit unknown, never a blank


def test_stop_rules_reads_the_config_live_and_builds_the_resolver_per_call():
    cfgs = [MagicMock(name="first"), MagicMock(name="second")]
    factory = MagicMock(name="resolver_factory")
    rules = StopRules(read_cfg=lambda: cfgs[-1], entry_stop_resolver=factory)
    assert rules.cfg is cfgs[1]
    cfgs.append(MagicMock(name="third"))
    assert rules.cfg is cfgs[2]  # handed in live, not snapshotted
    rules._resolve_entry_and_stop("a", k=1)
    rules.real_reward_risk_preview()
    assert factory.call_count == 2
    factory.return_value._resolve_entry_and_stop.assert_called_once_with("a", k=1)


def test_order_builders_read_collaborators_per_call():
    host = MagicMock(name="host")
    builders = OrderBuilders(collaborators=lambda: order_builder_collaborators(host))
    first = builders._long_entry_builder()
    host.cfg = MagicMock(name="swapped_cfg")
    second = builders._long_entry_builder()
    assert isinstance(second, LongEntryBuilder)
    assert second.cfg is host.cfg and first.cfg is not second.cfg
    assert set(builders._order_builder_collaborators()) == {p for p, _ in _ORDER_BUILDER_COLLABORATORS}
    from src.models import TargetPosition

    held = builders._hold_decision(
        TargetPosition(symbol="XYZ", target_weight_pct=0.0, conviction="LOW", thesis="keep", invalid_if="n/a")
    )
    assert held.action == "HOLD" and held.symbol == "XYZ"  # exits need no collaborators at all


def test_owner_inherits_nothing_and_delegates_every_part_name():
    from src.portfolio_constructor import PortfolioConstructor

    assert PortfolioConstructor.__bases__ == (object,)
    for name in STOP_DELEGATES + ORDER_DELEGATES + STATIC_EXIT_DELEGATES + ("_entry_stop_resolver",):
        shim = inspect.getattr_static(PortfolioConstructor, name)
        fn = getattr(shim, "__func__", shim)
        assert (fn.__doc__ or "").startswith("Thin shim"), name
    part_names = {n for n, _ in inspect.getmembers(StopRules, inspect.isfunction)} - {"__init__"}
    assert part_names == set(STOP_DELEGATES), part_names ^ set(STOP_DELEGATES)
    part_names = {n for n, _ in inspect.getmembers(OrderBuilders, inspect.isfunction)} - {"__init__"}
    assert part_names == set(ORDER_DELEGATES) | set(STATIC_EXIT_DELEGATES)


def test_owner_instance_patch_is_what_the_parts_see():
    from src.portfolio_constructor import PortfolioConstructor

    owner = PortfolioConstructor()
    owner.cfg = MagicMock(name="swapped")
    assert owner._stop_rules.cfg is owner.cfg
    owner._resolve_stop = MagicMock(return_value=7.0)
    assert owner._entry_stop_resolver()._resolve_stop() == 7.0
