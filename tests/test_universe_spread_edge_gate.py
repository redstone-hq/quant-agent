"""Nightly screen spread gate: a break-even floor on the name's own net edge.

A name fails on spread only when its round-trip spread eats the move expected
over the owner-ruled hold (the bracket of the morning score K), never against
the flat 40 bps entry-slippage ceiling.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from src import universe_screen as us
from tests.test_universe_screen import TH, _bars


def _with_spread(monkeypatch, half_spread_bps: float) -> None:
    # Corwin-Schultz returns the FULL spread as a price fraction.
    monkeypatch.setattr(us, "corwin_schultz_spread", lambda bars: 2 * half_spread_bps / 10_000)


def test_sixty_bp_half_spread_with_a_large_atr_passes(monkeypatch):
    _with_spread(monkeypatch, 60.0)
    failures, measured = us.check_bars(_bars(300, rng=0.03), TH)  # ATR ~6% of price
    assert measured["half_spread_bps"] == 60.0
    assert "spread_exceeds_expected_move" not in failures
    assert "spread_too_wide" not in failures
    assert us.net_edge(measured["atr_pct"] / 100, 60.0, TH.hold_sessions) > 0


def test_round_trip_spread_above_expected_move_fails_with_the_new_reason(monkeypatch):
    _with_spread(monkeypatch, 60.0)
    failures, measured = us.check_bars(_bars(300, rng=0.003), TH)  # ATR ~0.6% of price
    assert us.net_edge(measured["atr_pct"] / 100, 60.0, TH.hold_sessions) <= 0
    assert "spread_exceeds_expected_move" in failures
    assert "spread_too_wide" not in failures


def test_half_spread_as_share_of_atr_is_recorded(monkeypatch):
    _with_spread(monkeypatch, 60.0)
    _, measured = us.check_bars(_bars(300, rng=0.03), TH)
    expected = (measured["half_spread_bps"] / 10_000) / (measured["atr_pct"] / 100)
    assert measured["half_spread_atr_frac"] == pytest.approx(expected, rel=1e-2)


def test_screen_thresholds_no_longer_read_the_entry_slippage_ceiling():
    from src.config import UniverseScreenConfig
    from src.stage_decision import VELOCITY_HOLD_SESSIONS

    assert "max_half_spread_bps" not in us.ScreenThresholds.__dataclass_fields__
    # A config with NO execution section at all still builds the thresholds.
    config = SimpleNamespace(
        universe_screen=UniverseScreenConfig(),
        risk=SimpleNamespace(min_stop_atr_multiple=2.5),
    )
    assert us.ScreenThresholds.from_config(config).hold_sessions == VELOCITY_HOLD_SESSIONS
