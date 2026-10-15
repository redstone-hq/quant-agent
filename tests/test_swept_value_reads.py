"""The backtest must prove it REACHED a value before a sweep over it counts.

Four times this project read a byte-identical A/B as "the parameter is
inert" when the harness had never reached the parameter at all. These
tests pin the three ways that happened and the counter that now makes it
visible:

* ``find_structural_levels`` freezes ``min_touches`` as DEFAULT ARGUMENTS, evaluated once at definition
  time — reassigning the module constant was swept past silently.
* ``compute_trailing_stop`` lives in ``src/risk/trail_evaluate.py`` and
  binds its chandelier / noise-band multiples with ``from
  src.risk.trailing import ...`` at import time — a sweep that patched
  ``src.risk.trailing`` changed nothing the trailing maths saw.
* A run that reports identical numbers and no read count cannot be told
  apart from a run that never read the value.

Nothing here asserts a threshold, a limit or a trading rule: only whether
a value is READ.
"""

from __future__ import annotations

import pytest

import src.data.levels as levels_module
import src.risk.trail_evaluate as trail_evaluate
import src.risk.trailing as trailing_module
from src.backtest import swept_values
from src.backtest.structural_stops import _resolve_structural_stop_and_target
from tests.test_backtest import _build_long_win_series, _run


def _capture(monkeypatch):
    import src.backtest.structural_stops as structural_stops

    seen: list[dict] = []
    real = structural_stops.find_structural_levels

    def capture(bars, **kwargs):
        seen.append(dict(kwargs))
        return real(bars, **kwargs)

    monkeypatch.setattr(structural_stops, "find_structural_levels", capture)
    return seen


def test_a_run_reads_every_level_tunable_at_its_use_site():
    """Before this wiring the engine passed none of them, so a sweep over
    them could only ever return byte-identical results. The swing-point bar
    window is gone (2026-10-15: swings are an ATR(14) reversal), so it is
    neither a tunable nor read."""
    _, params, _ = _run(_build_long_win_series())
    counts = params.meter.counts()
    assert "levels.pivot_window" not in counts
    assert counts["levels.min_touches"] >= 1


def test_counts_belong_to_their_run_and_do_not_leak():
    _, first, _ = _run(_build_long_win_series())
    _, second, _ = _run(_build_long_win_series())
    assert first.meter is not second.meter
    assert first.meter.counts() == second.meter.counts()


def test_no_wrapper_outlives_the_run():
    _run(_build_long_win_series())
    assert not isinstance(trail_evaluate.CHANDELIER_ATR_MULTIPLE, swept_values.CountedFloat)
    assert not isinstance(trail_evaluate.NOISE_BAND_ATR_MULTIPLE, swept_values.CountedFloat)


def test_reassigning_the_defining_module_constant_now_reaches_the_finder(monkeypatch):
    """`find_structural_levels` froze its defaults at definition time, so a
    sweep that reassigned the module constant never arrived. The engine now
    reads it late and passes it explicitly — captured here as it arrives."""
    seen = _capture(monkeypatch)
    monkeypatch.setattr(levels_module, "MIN_TOUCHES", levels_module.MIN_TOUCHES + 1)
    meter = swept_values.SweepMeter()
    _resolve_structural_stop_and_target(_build_long_win_series(), "long", 105.0, meter=meter)

    assert len(seen) == 1
    assert "pivot_window" not in seen[0]
    assert seen[0]["min_touches"] == levels_module.MIN_TOUCHES
    assert "tolerance_pct" not in seen[0]
    assert meter.counts()["levels.min_touches"] == 1


def test_a_meter_override_wins_over_the_module_constant(monkeypatch):
    seen = _capture(monkeypatch)
    meter = swept_values.SweepMeter()
    meter.set_override("levels.min_touches", 9)
    _resolve_structural_stop_and_target(_build_long_win_series(), "long", 105.0, meter=meter)
    assert seen[0]["min_touches"] == 9


def test_the_trailing_multiples_are_counted_while_a_run_is_in_progress():
    """`src/risk/trail_evaluate.py` holds the binding the maths uses; the
    defining module `src/risk/trailing.py` does not."""
    meter = swept_values.SweepMeter()
    with meter.counting():
        assert isinstance(trail_evaluate.CHANDELIER_ATR_MULTIPLE, swept_values.CountedFloat)
        assert isinstance(trail_evaluate.NOISE_BAND_ATR_MULTIPLE, swept_values.CountedFloat)
        # Numerically identical to the constant as written down.
        assert float(trail_evaluate.CHANDELIER_ATR_MULTIPLE) == pytest.approx(
            float(trailing_module.CHANDELIER_ATR_MULTIPLE)
        )
    assert not isinstance(trail_evaluate.CHANDELIER_ATR_MULTIPLE, swept_values.CountedFloat)


def test_an_override_reaches_the_module_that_actually_reads_it():
    meter = swept_values.SweepMeter()
    meter.set_override("trailing.chandelier_atr_multiple", 7.5)
    with meter.counting():
        assert float(trail_evaluate.CHANDELIER_ATR_MULTIPLE) == pytest.approx(7.5)
    assert float(trail_evaluate.CHANDELIER_ATR_MULTIPLE) == pytest.approx(
        float(trailing_module.CHANDELIER_ATR_MULTIPLE)
    )


def test_a_counted_float_is_numerically_transparent():
    counted = swept_values.CountedFloat(2.5, swept_values.SweepMeter(), "levels.min_touches")
    assert counted == 2.5
    assert counted * 4 == 10.0
    assert 4 * counted == 10.0
    assert counted + 1 == 3.5
    assert 1 - counted == -1.5
    assert -counted == -2.5
    assert isinstance(counted * 4, float)
    assert not isinstance(counted * 4, swept_values.CountedFloat)


def test_a_use_of_a_counted_float_is_counted():
    meter = swept_values.SweepMeter()
    counted = swept_values.CountedFloat(2.0, meter, "trailing.chandelier_atr_multiple")
    _ = counted * 3
    _ = 3 * counted
    assert meter.counts()["trailing.chandelier_atr_multiple"] == 2


def test_an_unread_swept_value_is_reported_as_a_non_result():
    """The whole point: a zero-read sweep must be loud, not silently
    identical to the baseline."""
    meter = swept_values.SweepMeter()
    meter.set_override("levels.min_touches", 9)
    report = meter.format_read_counts("unit test")
    assert "NEVER READ" in report
    assert "non-result" in report
    assert "levels.min_touches" in report


def test_a_value_with_no_reading_site_is_named_rather_than_ignored():
    meter = swept_values.SweepMeter()
    meter.set_override("risk.min_stop_atr_multiple", 3.0)
    assert meter.unknown_overrides() == ["risk.min_stop_atr_multiple"]
    assert "no reading site" in meter.format_read_counts()


def test_the_diagnostic_never_raises_on_a_nonsense_override():
    """A diagnostic that halts a backtest is worse than the problem."""
    meter = swept_values.SweepMeter()
    meter.set_override("not.a.value", "banana")
    meter.read("not.a.value", 1.0)
    assert meter.format_read_counts()
