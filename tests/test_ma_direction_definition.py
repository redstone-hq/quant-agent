"""The AI-facing MA direction is the exit rule's own definition.

`risk.exit_guard_trend` calls the 200-day MA rising when today's MA200 is
above the prior session's. The market-context block must report that same
change (in the name's own ATR), and the screen must ask for exactly the one
extra bar that definition needs.
"""

from datetime import date, timedelta

import numpy as np
import pytest

from types import SimpleNamespace

from src.config import UniverseScreenConfig
from src.data.context import compute_market_context, format_context_block
from src.data.technical import LONGEST_INDICATOR_WINDOW, atr_series, compute_indicators
from src.models import OHLCV
from src.risk.exit_guard_trend import REGIME_STRONG_WITH_TREND, classify_trend_context
from src.universe_screen import ScreenThresholds


def _bars(closes: list[float]) -> list[OHLCV]:
    out, prev = [], closes[0]
    for i, c in enumerate(closes):
        out.append(
            OHLCV(
                date=date(2023, 1, 2) + timedelta(days=i),
                open=prev,
                high=max(prev, c) + 0.4,
                low=min(prev, c) - 0.4,
                close=c,
                volume=1_000_000,
            )
        )
        prev = c
    return out


_CASES = {
    "rising": [float(x) for x in np.linspace(100.0, 200.0, 300)],
    "falling": [float(x) for x in np.linspace(200.0, 100.0, 300)],
    "flat": [100.0] * 300,
}


def _exit_rule_rising(bars: list[OHLCV]) -> bool:
    ind = compute_indicators("ZZZ", bars)
    regime = classify_trend_context(
        is_short=False,
        current_price=ind.ma_200 + 50,
        ma_50=ind.ma_200 + 10,
        ma_200=ind.ma_200,
        adx=40.0,
        ma_200_prior=ind.ma_200_prior,
    )
    return regime == REGIME_STRONG_WITH_TREND


@pytest.mark.parametrize("name", list(_CASES))
def test_ai_direction_sign_matches_exit_rule(name):
    bars = _bars(_CASES[name])
    ctx = compute_market_context(bars)
    v = ctx.ma200_change_atr
    assert v is not None
    assert (v > 0) == _exit_rule_rising(bars)
    if name == "falling":
        assert v < 0
    if name == "flat":
        assert v == 0
        assert "flat" in format_context_block(ctx)


def test_value_is_previous_session_change_in_atr():
    closes = _CASES["rising"]
    bars = _bars(closes)
    ma = np.convolve(closes, np.ones(200) / 200, mode="valid")
    expected = (ma[-1] - ma[-2]) / float(atr_series(bars)[-1])
    assert compute_market_context(bars).ma200_change_atr == pytest.approx(expected, abs=0.006)


def test_prompt_line_names_the_definition():
    block = format_context_block(compute_market_context(_bars(_CASES["rising"])))
    assert "MA change since prior session (in ATR):" in block
    assert "MA200" in block and "rising" in block


def test_one_prior_bar_is_enough():
    closes = _CASES["rising"]
    assert compute_market_context(_bars(closes[: LONGEST_INDICATOR_WINDOW + 1])).ma200_change_atr is not None
    assert compute_market_context(_bars(closes[:LONGEST_INDICATOR_WINDOW])).ma200_change_atr is None


def test_screen_history_floor_is_longest_window_plus_one():
    config = SimpleNamespace(
        universe_screen=UniverseScreenConfig(),
        risk=SimpleNamespace(min_stop_atr_multiple=2.5),
    )
    s = ScreenThresholds.from_config(config)
    assert s.min_history_bars == LONGEST_INDICATOR_WINDOW + 1


def test_small_rise_is_rising_not_rounded_to_flat():
    # A MA200 move far under 0.005 ATR must still read rising, not be rounded to flat.
    closes = [100.0] * 299 + [100.2]
    bars = _bars(closes)
    ctx = compute_market_context(bars)
    assert 0 < ctx.ma200_change_atr < 0.005
    assert "MA200 +0.00" in format_context_block(ctx) and "(rising)" in format_context_block(ctx)
