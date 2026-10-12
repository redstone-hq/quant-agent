"""Money paths read pipeline parts directly, so a missing part raises at the read.

Each site below used to read its part with ``getattr(obj, "_x", None)`` and
silently do nothing when the part was gone (no ceiling, no exit relief, no
cash-park split). Every one of these parts is set unconditionally by
``TradingPipeline.__init__`` or is a method on the class, so a missing one is
a wiring fault and must surface as AttributeError at the exact line.

Deliberately NOT changed: the shared ``_session_gross_ceiling`` helper. The
midday review session accepts a pipeline without a resolver, alerts at build,
and must still run its exits; a raise there would break that exit path.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from src.config.risk_adjuncts import CashSweepConfig
from src.pipeline_delever import DeleverMixin
from src.pipeline_protection import _build_exit_relief
from tests.pipeline_factory import build_pipeline


@pytest.fixture
def pipe():
    return build_pipeline()


@pytest.fixture
def no_resolver(monkeypatch):
    monkeypatch.delattr(DeleverMixin, "_resolve_gross_ceiling")


@pytest.mark.parametrize("builder", ["_delever_enforce", "_delever_trims"])
def test_delever_builders_raise_without_gross_ceiling_resolver(pipe, no_resolver, builder):
    with pytest.raises(AttributeError, match="_resolve_gross_ceiling"):
        getattr(pipe, builder)()


def test_exit_relief_never_raises_without_register(pipe):
    """Exit relief is protective and creates its register lazily on the host,
    so a missing register must stay a no-op, never an error."""
    del pipe._unsettled_exit_orders
    relief = _build_exit_relief(pipe)
    assert relief._open_exit_relief([]) == ([], False)
    relief._register_exit_settlement({"order_id": "o-1", "symbol": "aapl", "submitted_qty": 3})
    assert pipe._unsettled_exit_orders == {"o-1": {"symbol": "AAPL", "submitted_qty": 3.0}}


def test_exit_relief_unchanged_when_register_present(pipe):
    relief = _build_exit_relief(pipe)
    assert relief._open_exit_relief([]) == ([], False)
    relief._register_exit_settlement({"order_id": "o-1", "symbol": "aapl", "submitted_qty": 3})
    assert pipe._unsettled_exit_orders == {"o-1": {"symbol": "AAPL", "submitted_qty": 3.0}}


@pytest.mark.parametrize("reader", ["_sweeper", "_typed_cash_sweeper"])
def test_cash_park_lookup_raises_without_sweeper(pipe, reader):
    del pipe.cash_sweeper
    with pytest.raises(AttributeError, match="cash_sweeper"):
        getattr(pipe, reader)()


def test_sweeping_on_with_empty_park_symbol_is_rejected_at_load():
    """A config error, refused once where settings are validated, so it cannot start."""
    with pytest.raises(ValidationError, match="cash_sweep.symbol must be a non-empty ticker"):
        CashSweepConfig(enabled=True, symbol="  ")


def test_sweep_symbol_logs_nothing_per_call(pipe, caplog):
    pipe._sweeper = lambda: SimpleNamespace(symbol="SGOV")
    with caplog.at_level(logging.DEBUG, logger="src.pipeline"):
        assert pipe._sweep_symbol() == "SGOV"
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


def test_stop_coverage_runs_on_a_real_pipeline_without_raising(pipe):
    """The stop-coverage check reads the sweeper directly; a constructor-built pipeline has it."""
    pipe.broker.get_positions.return_value = []
    assert isinstance(pipe._reconcile_stop_coverage(), list)
