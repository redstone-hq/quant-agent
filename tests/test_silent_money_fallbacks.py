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


def test_open_exit_relief_raises_without_register(pipe):
    del pipe._unsettled_exit_orders
    with pytest.raises(AttributeError, match="_unsettled_exit_orders"):
        _build_exit_relief(pipe)._open_exit_relief([])


def test_register_exit_settlement_raises_without_register(pipe):
    del pipe._unsettled_exit_orders
    with pytest.raises(AttributeError, match="_unsettled_exit_orders"):
        _build_exit_relief(pipe)._register_exit_settlement({"order_id": "o-1", "symbol": "AAPL"})


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


def test_empty_cash_park_symbol_logs_critical_and_does_not_stop(pipe, caplog):
    pipe._sweeper = lambda: SimpleNamespace(symbol="  ")
    with caplog.at_level(logging.CRITICAL, logger="src.pipeline"):
        assert pipe._sweep_symbol() is None
    assert any(r.levelno == logging.CRITICAL and "Cash park not connected" in r.getMessage() for r in caplog.records)


def test_set_cash_park_symbol_logs_nothing(pipe, caplog):
    pipe._sweeper = lambda: SimpleNamespace(symbol="SGOV")
    with caplog.at_level(logging.CRITICAL, logger="src.pipeline"):
        assert pipe._sweep_symbol() == "SGOV"
    assert not [r for r in caplog.records if r.levelno == logging.CRITICAL]


def test_sweeping_off_logs_nothing(pipe, caplog):
    pipe._sweeper = lambda: None
    with caplog.at_level(logging.CRITICAL, logger="src.pipeline"):
        assert pipe._sweep_symbol() is None
    assert not [r for r in caplog.records if r.levelno == logging.CRITICAL]
