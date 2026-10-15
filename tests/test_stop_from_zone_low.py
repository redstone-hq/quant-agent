"""No-ATR structural stop sits at the level's OWN zone extreme (2026-10-15).

The flat 0.5% buffer past the level was a chosen number. With no ATR the
stop is now the lowest low of the bars that formed the support (short: the
highest high of the resistance zone) -- a price the stock itself printed.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.portfolio_constructor.config import ConstructorConfig
from src.portfolio_constructor.stops import StopRules


def _rules() -> StopRules:
    cfg = ConstructorConfig()
    return StopRules(read_cfg=lambda: cfg, entry_stop_resolver=lambda: None)


def _analysis(**kw):
    base = dict(
        symbol="ZZZ",
        computed_levels=[],
        computed_level_touches={},
        computed_level_zones={},
        computed_level_bars={},
        signal_bar_low=None,
        signal_bar_high=None,
    )
    base.update(kw)
    return SimpleNamespace(**base)


def test_long_stop_sits_at_support_zone_low():
    a = _analysis(computed_levels=[95.0], computed_level_zones={95.0: [94.40, 95.60]})
    out = _rules()._derive_structural_stop_no_atr(a, 100.0, False)
    assert out is not None
    assert out[0] == 95.0
    assert out[1] == pytest.approx(94.40)


def test_short_stop_sits_at_resistance_zone_high():
    a = _analysis(computed_levels=[105.0], computed_level_zones={105.0: [104.40, 105.60]})
    out = _rules()._derive_structural_stop_no_atr(a, 100.0, True)
    assert out is not None
    assert out[0] == 105.0
    assert out[1] == pytest.approx(105.60)


def test_zone_read_from_pivot_bars_when_zone_map_absent():
    # Entry-path analyses carry the pivot bars but not the zone map.
    a = _analysis(
        computed_levels=[95.0],
        computed_level_bars={95.0: [(94.70, 95.80), (94.40, 95.30)]},
    )
    out = _rules()._derive_structural_stop_no_atr(a, 100.0, False)
    assert out is not None
    assert out[1] == pytest.approx(94.40)


def test_level_without_zone_uses_signal_bar_low_never_a_percentage():
    a = _analysis(computed_levels=[95.0], signal_bar_low=96.50)
    out = _rules()._derive_structural_stop_no_atr(a, 100.0, False)
    assert out is not None
    assert out[0] == 96.50
    assert out[1] == pytest.approx(96.50)


def test_no_zone_and_no_bars_refuses():
    a = _analysis(computed_levels=[95.0])
    assert _rules()._derive_structural_stop_no_atr(a, 100.0, False) is None
    assert _rules()._derive_structural_stop_no_atr(_analysis(), 100.0, True) is None


def test_buffer_setting_is_gone():
    assert not hasattr(ConstructorConfig(), "structural_stop_buffer_pct")
