"""Moving averages keep full precision so a sub-cent 200-MA slope is not lost.

Rounding ma_200 and ma_200_prior to the cent made a 200-day average rising by
under half a cent a day read as flat, so the exit guard dropped to the default
regime. The 200-MA rises exactly when today's close is above the close 200
sessions ago, so the full-precision comparison needs no threshold.
"""

from datetime import date, timedelta

from src.data.technical import compute_indicators
from src.models import OHLCV
from src.risk.exit_guard_trend import (
    REGIME_DEFAULT,
    REGIME_STRONG_WITH_TREND,
    classify_trend_context,
)


def _bars(closes):
    return [
        OHLCV(
            date=date(2023, 1, 2) + timedelta(days=i),
            open=c,
            high=c + 0.1,
            low=c - 0.1,
            close=c,
            volume=1_000_000,
        )
        for i, c in enumerate(closes)
    ]


def _sub_cent_rise():
    # 200 flat closes then one close 0.50 higher: MA200 moves by 0.0025.
    return compute_indicators("ZZZ", _bars([100.0] * 200 + [100.5]))


def test_sub_half_cent_ma200_rise_is_visible():
    ind = _sub_cent_rise()
    assert ind.ma_200_prior is not None
    assert 0 < ind.ma_200 - ind.ma_200_prior < 0.005
    assert ind.ma_200 > ind.ma_200_prior


def test_exit_guard_uses_trend_regime_on_sub_cent_rise():
    ind = _sub_cent_rise()
    regime = classify_trend_context(
        is_short=False,
        current_price=105.0,
        ma_50=ind.ma_50,
        ma_200=ind.ma_200,
        ma_200_prior=ind.ma_200_prior,
        adx=40.0,
    )
    assert regime == REGIME_STRONG_WITH_TREND
    assert regime != REGIME_DEFAULT


def test_moving_averages_are_not_cent_rounded():
    ind = _sub_cent_rise()
    assert ind.ma_200 != round(ind.ma_200, 2)
