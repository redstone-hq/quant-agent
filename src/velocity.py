"""Velocity record: how fast a candidate's own price travels toward its target.

RECORD ONLY (step A of the owner ruling of 2026-10-13: the desk trades for
VELOCITY — hold about a couple of weeks as intent, take only names that move
enough, filter whipsaw, rank by profit per unit time, every number from the
stock's own behaviour). Nothing here trades, ranks, sizes, targets or stops:
the caller stores the record beside the recorded-only reward:risk on each
`candidate_ranked` event and nothing reads it back to decide anything.

Every quantity is read off the stock's own daily bars or the candidate's own
preview geometry; nothing is tuned here.

  * sigma, the daily standard deviation in dollars, is ``ATR / ATR_PER_SIGMA``:
    the desk's one ATR (`src.data.technical.atr_series`, Wilder) and its one
    range-to-sigma conversion (`src.data.levels.ATR_PER_SIGMA` = sqrt(8/pi),
    about 1.596). Neither is re-derived here.
  * ``sigma / P`` is velocity per dollar invested at the entry price P.
  * VR(W) is the Lo-MacKinlay variance ratio of the stock's own daily log
    closes (Lo & MacKinlay 1988, "Stock Market Prices Do Not Follow Random
    Walks", Review of Financial Studies 1(1), eqs. for the overlapping,
    bias-corrected estimator). About 1 for a random walk; below 1 the stock
    mean-reverts (whipsaw); above 1 it trends.
  * The reach chance within W sessions is ``erfc(t / (sigma * sqrt(2W)))``
    for a target t dollars away. That is the desk's existing driftless
    reflection-principle reading, `src.data.levels.touch_probability`, asked
    about the target instead of the stop.
  * The median sessions to reach t is the median of that same first-passage
    (Levy) distribution, the horizon at which the reach chance is one half:
    ``(t / sigma)^2 / PhiInv(3/4)^2``, about ``2.2 * (t / sigma)^2``
    (``erfc(x) = 1/2`` exactly when ``x = PhiInv(3/4) / sqrt(2)``).
  * The round-trip cost is ``ask - bid`` when a live quote exists, else None:
    UNKNOWN, never 0.

The hold horizon W is owner intent, not a measurement, so it is never
defaulted here: the caller passes its own named, ledgered constant.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from statistics import NormalDist

from src.data.levels import ATR_PER_SIGMA, touch_probability
from src.data.technical import ATR_PERIOD, atr_series

__all__ = [
    "candidate_velocity",
    "median_sessions_to_reach",
    "reach_chance",
    "round_trip_cost",
    "variance_ratio",
    "velocity_record",
]


def _finite(value) -> float | None:
    """`value` as a finite float, or None when it is not a real number."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _positive(value) -> float | None:
    number = _finite(value)
    return number if number is not None and number > 0 else None


def variance_ratio(closes: Sequence[float], horizon_sessions: int) -> float | None:
    """Lo-MacKinlay VR(q) of daily log closes, q = `horizon_sessions`.

    Overlapping q-session returns with both of the paper's bias corrections
    (``nq - 1`` for the one-session variance, ``m = q (nq - q + 1)(1 - q/nq)``
    for the q-session one). None when there are not more one-session returns
    than q, when a close is not a positive number, or when the closes never
    vary (no variance to compare against).
    """
    q = int(horizon_sessions)
    prices = [_positive(c) for c in closes]
    if q < 2 or any(p is None for p in prices):
        return None
    logs = [math.log(p) for p in prices]
    nq = len(logs) - 1
    if nq <= q:
        return None
    mu = (logs[-1] - logs[0]) / nq
    one = [logs[k] - logs[k - 1] - mu for k in range(1, nq + 1)]
    var_a = sum(d * d for d in one) / (nq - 1)
    if var_a <= 0:
        return None
    many = [logs[k] - logs[k - q] - q * mu for k in range(q, nq + 1)]
    m = q * (nq - q + 1) * (1 - q / nq)
    return (sum(d * d for d in many) / m) / var_a


def reach_chance(target_distance: float | None, atr: float | None, horizon_sessions: int) -> float | None:
    """Driftless chance the price touches a target `target_distance` away within the horizon.

    ``erfc(t / (sigma sqrt(2W)))`` with ``sigma = ATR / ATR_PER_SIGMA``, which
    is exactly `touch_probability` at a width of ``t / ATR``.
    """
    distance = _finite(target_distance)
    atr_value = _positive(atr)
    if distance is None or distance < 0 or atr_value is None:
        return None
    return touch_probability(distance / atr_value, horizon_sessions)


def median_sessions_to_reach(target_distance: float | None, sigma: float | None) -> float | None:
    """Median sessions for a driftless walk with daily sd `sigma` to first touch `target_distance`.

    The first-passage time to t has ``P(T <= s) = erfc(t / (sigma sqrt(2s)))``
    (Levy distribution), so its median solves ``erfc(x) = 1/2``, i.e.
    ``x = PhiInv(3/4) / sqrt(2)``, giving ``s = (t / (sigma PhiInv(3/4)))^2``
    — about ``2.198 (t / sigma)^2``.
    """
    distance = _finite(target_distance)
    sd = _positive(sigma)
    if distance is None or distance < 0 or sd is None:
        return None
    # 3/4 is the normal quantile at which erfc(x) = 1/2 (the median), not a
    # tuned number: erfc(x) = 2(1 - Phi(x sqrt 2)) = 1/2 <=> Phi(x sqrt 2) = 3/4.
    ratio = distance / (sd * NormalDist().inv_cdf(0.75))
    return ratio * ratio


def round_trip_cost(bid: float | None, ask: float | None) -> float | None:
    """``ask - bid`` per share from a live quote; None (UNKNOWN, never 0) without a readable one."""
    b = _positive(bid)
    a = _positive(ask)
    if b is None or a is None or a < b:
        return None
    return a - b


def velocity_record(
    bars: Sequence | None,
    *,
    entry_price: float | None,
    target_distance: float | None,
    horizon_sessions: int,
    bid: float | None = None,
    ask: float | None = None,
) -> dict:
    """Every velocity measurement for one candidate; None wherever it cannot be read.

    `bars` are the stock's own daily OHLCV bars (any order; sorted by date
    here as `src.data.technical` does). `target_distance` is the distance in
    dollars from the entry to the first structural target.
    """
    usable = sorted(
        (
            b
            for b in (bars or [])
            if getattr(b, "date", None) is not None
            and all(_positive(getattr(b, f, None)) is not None for f in ("high", "low", "close"))
        ),
        key=lambda b: b.date,
    )
    series = atr_series(usable, period=ATR_PERIOD) if usable else []
    atr = _positive(float(series[-1])) if len(series) else None
    sigma = atr / ATR_PER_SIGMA if atr is not None else None
    price = _positive(entry_price)
    distance = _finite(target_distance)
    distance = abs(distance) if distance is not None else None
    return {
        "horizon_sessions": int(horizon_sessions),
        "atr": atr,
        "sigma": sigma,
        "sigma_per_dollar": sigma / price if sigma is not None and price is not None else None,
        "variance_ratio": variance_ratio([b.close for b in usable], horizon_sessions),
        "target_distance": distance,
        "reach_chance": reach_chance(distance, atr, horizon_sessions),
        "median_sessions_to_reach": median_sessions_to_reach(distance, sigma),
        "round_trip_cost": round_trip_cost(bid, ask),
    }


def candidate_velocity(
    symbols_bars: dict | None,
    analysis,
    preview,
    *,
    horizon_sessions: int,
    bid: float | None = None,
    ask: float | None = None,
) -> dict:
    """The velocity record for one analysed candidate at decision time.

    `preview` is `PortfolioConstructor.entry_stop_preview`'s
    ``(entry, stop, target)`` — the same per-candidate preview the
    recorded-only reward:risk is built from — or None when it could not be
    read; the entry then falls back to the analyst's snapshot and the target
    distance is unknown.
    """
    symbol = str(getattr(analysis, "symbol", "") or "")
    bars_by_symbol = symbols_bars or {}
    bars = bars_by_symbol.get(symbol) or bars_by_symbol.get(symbol.upper())
    entry = getattr(analysis, "entry_price", None)
    target = None
    if isinstance(preview, tuple) and len(preview) == 3:
        entry, target = preview[0], preview[2]
    start = _finite(entry)
    end = _finite(target)
    return velocity_record(
        bars,
        entry_price=entry,
        target_distance=end - start if start is not None and end is not None else None,
        horizon_sessions=horizon_sessions,
        bid=bid,
        ask=ask,
    )
