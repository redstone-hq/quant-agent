import logging

import numpy as np
import pandas as pd
import ta

from src.models import OHLCV, TechnicalIndicators
from src.sentinel.counted import record_swallowed

logger = logging.getLogger(__name__)

#: Wilder's original lookback, and the one every chart package means by
#: "ATR(14)". Named rather than repeated so the risk path and the analyst's
#: context block cannot end up on different periods.
ATR_PERIOD = 14

#: Wilder's original lookback for the Average Directional Index, exactly as
#: published in J. Welles Wilder, New Concepts in Technical Trading Systems
#: (1978), and the period every chart package means by "ADX(14)". Named
#: rather than repeated (same reason as `ATR_PERIOD`) so the indicator path
#: and the exit guard's trend-context read can never end up on different
#: periods. The strong-trend threshold read against it
#: (`src.risk.exit_guard_trend.ADX_STRONG_TREND_THRESHOLD`, 25) is Wilder's too.
ADX_PERIOD = 14


def atr_series(bars: list[OHLCV], period: int = ATR_PERIOD) -> np.ndarray:
    """Wilder's average true range over `bars` — THE one implementation.

    Every average-true-range number in this system comes from here. It used
    not to: `src/data/context.py` convolved the true ranges with a flat
    14-wide kernel — a simple moving average, not an ATR — so the analyst was
    shown one volatility reading while the risk path sized and widened stops
    off another. Measured across 101 symbols and 973 real sessions the two
    disagreed by a mean absolute 7.05% and disagreed about whether volatility
    was expanding or contracting on 17.2% of symbol-days.

    Wilder's smoothing is recursive: today's reading is
    ``(yesterday * (period - 1) + today's true range) / period``. It therefore
    keeps a geometrically decaying memory of a volatility shock, where a flat
    window drops the shock off a cliff exactly `period` bars later. That
    difference is the whole reason the two implementations diverged, and it is
    what the tests in `tests/test_atr_is_wilder_everywhere.py` pin.

    Returns one value per bar **once warmed up** — length
    ``len(bars) - (period - 1)``, aligned so element *i* belongs to bar
    ``i + period - 1``. The warm-up is trimmed rather than passed through
    because `ta` fills it with 0.0, not NaN, and a zero that reached
    `context.py`'s percentile would read as "quieter than today" and bias
    `volatility_state` toward expanding on short histories.

    Bars are used in the order given — this does not sort. Returns an empty
    array when there are too few bars to say anything, never a guess.
    """
    if len(bars) < period:
        return np.empty(0, dtype=float)
    frame = pd.DataFrame(
        {
            "high": [float(b.high) for b in bars],
            "low": [float(b.low) for b in bars],
            "close": [float(b.close) for b in bars],
        }
    )
    values = (
        ta.volatility.AverageTrueRange(
            frame["high"],
            frame["low"],
            frame["close"],
            window=period,
        )
        .average_true_range()
        .to_numpy(dtype=float)
    )
    return values[period - 1 :]


#: The longest window any indicator in `compute_indicators` needs — the
#: 200-session moving average, the long-term trend reference the analyst is
#: briefed with (`config/settings.yaml` `lookback_days` was raised for it in
#: the 2026-07-16 audit, which treated its absence as the analyst judging
#: trend blind). Named here, not chosen here: it is the same 200 the
#: `rolling(200)` below has always used -- a conventional published trend
#: reference. It is NO LONGER a trade-governing gate: the constructor's
#: young-listing bar-count refusal that also read this constant was dropped
#: on the owner's ruling (docs/WORK.md item 180, 2026-09-25); a young name is
#: now judged on whether a protective stop is readable from whatever bars
#: exist, not on this count. `ma_200` simply stays None under 200 sessions.
LONGEST_INDICATOR_WINDOW = 200


def compute_indicators(symbol: str, bars: list[OHLCV]) -> TechnicalIndicators:
    if not bars:
        return TechnicalIndicators(symbol=symbol)

    df = pd.DataFrame([b.model_dump() for b in bars])
    df = df.set_index("date").sort_index()

    result = TechnicalIndicators(symbol=symbol)

    # Moving averages
    if len(df) >= 20:
        result.ma_20 = float(df["close"].rolling(20).mean().iloc[-1])
    if len(df) >= 50:
        result.ma_50 = float(df["close"].rolling(50).mean().iloc[-1])
    if len(df) >= LONGEST_INDICATOR_WINDOW:
        _ma200 = df["close"].rolling(LONGEST_INDICATOR_WINDOW).mean()
        result.ma_200 = float(_ma200.iloc[-1])
        # The 200-MA one completed session earlier, so the exit guard can read
        # the 200-MA SLOPE (rising vs falling) for its trend-scaled break
        # regime (owner mandate 2026-09-24). Same rolling window as ma_200; None
        # until there is one extra bar to look back on.
        if len(df) >= LONGEST_INDICATOR_WINDOW + 1:
            result.ma_200_prior = float(_ma200.iloc[-2])

    # RSI
    if len(df) >= 15:
        rsi = ta.momentum.RSIIndicator(df["close"], window=14)
        rsi_val = rsi.rsi().iloc[-1]
        if pd.notna(rsi_val):
            result.rsi_14 = round(float(rsi_val), 2)

    # MACD
    if len(df) >= 26:
        macd_ind = ta.trend.MACD(df["close"])
        macd_val = macd_ind.macd().iloc[-1]
        signal_val = macd_ind.macd_signal().iloc[-1]
        hist_val = macd_ind.macd_diff().iloc[-1]
        if pd.notna(macd_val):
            result.macd = round(float(macd_val), 4)
        if pd.notna(signal_val):
            result.macd_signal = round(float(signal_val), 4)
        if pd.notna(hist_val):
            result.macd_hist = round(float(hist_val), 4)

    # ADX (+DI / -DI) — Wilder's directional system, same block pattern as
    # MACD above. ADX warms up over roughly two lookbacks (the recursive
    # smoothing is applied twice: once to the directional movement, once to
    # the DX), so it is only computed once there are enough bars for `ta` to
    # return a finite reading; a NaN warm-up value is left as None rather
    # than shipped as a spurious 0.0.
    if len(df) >= 2 * ADX_PERIOD:
        adx_ind = ta.trend.ADXIndicator(
            df["high"],
            df["low"],
            df["close"],
            window=ADX_PERIOD,
        )
        adx_val = adx_ind.adx().iloc[-1]
        di_plus_val = adx_ind.adx_pos().iloc[-1]
        di_minus_val = adx_ind.adx_neg().iloc[-1]
        if pd.notna(adx_val):
            result.adx_14 = round(float(adx_val), 2)
        if pd.notna(di_plus_val):
            result.di_plus_14 = round(float(di_plus_val), 2)
        if pd.notna(di_minus_val):
            result.di_minus_14 = round(float(di_minus_val), 2)

    # Bollinger Bands
    if len(df) >= 20:
        bb = ta.volatility.BollingerBands(df["close"], window=20)
        bb_h = bb.bollinger_hband().iloc[-1]
        bb_m = bb.bollinger_mavg().iloc[-1]
        bb_l = bb.bollinger_lband().iloc[-1]
        if pd.notna(bb_h):
            result.bb_upper = round(float(bb_h), 2)
        if pd.notna(bb_m):
            result.bb_middle = round(float(bb_m), 2)
        if pd.notna(bb_l):
            result.bb_lower = round(float(bb_l), 2)

    # ATR — the shared Wilder implementation above, on the same date-sorted
    # bars the rest of this function uses.
    if len(df) >= 15:
        sorted_bars = sorted(bars, key=lambda b: b.date)
        series = atr_series(sorted_bars, period=ATR_PERIOD)
        if series.size and np.isfinite(series[-1]):
            result.atr_14 = round(float(series[-1]), 2)

    # Volume change %
    if len(df) >= 6:
        recent_vol = df["volume"].tail(5).mean()
        prev_vol = df["volume"].iloc[-10:-5].mean() if len(df) >= 10 else df["volume"].iloc[:-5].mean()
        if prev_vol > 0:
            result.volume_change_pct = round(float((recent_vol - prev_vol) / prev_vol * 100), 2)

    return result


def atr_for_symbol(market, symbol: str) -> float | None:
    """ATR(14) from ~30 days of daily bars; None when unknowable.

    `market` is any provider with `get_ohlcv(symbol, days)`. Used by the
    TRAIL_STOP noise-band clamp and the position-facts vol-unit metrics.
    Failure is always None (callers degrade to the pre-clamp behavior) --
    never raises. Moved byte-for-byte from `TradingPipeline._atr_for_symbol`
    (2026-10-01); the in-function import is kept so a patch of
    `src.data.technical.compute_indicators` still reaches it.
    """
    try:
        bars = market.get_ohlcv(symbol, 30) or []
        if len(bars) < 15:
            return None
        from src.data.technical import compute_indicators

        atr = compute_indicators(symbol, bars).atr_14
        return float(atr) if atr and atr > 0 else None
    except Exception as e:  # noqa: BLE001
        record_swallowed("data.technical.atr_for_symbol", e, log=logger)
        return None
