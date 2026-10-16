"""Deterministic market context for the Tech Analyst.

Why this exists
---------------
The Tech Analyst was given moving averages, RSI, MACD, Bollinger bands, ATR and
a single volume-change percentage, and was expected to behave like a technical
analyst. Most of what a technical analyst actually reasons about was missing:

* whether the name is outperforming its index and its sector — arguably the
  single most-used screen in the profession, and completely absent;
* where price sits in its own multi-year range;
* whether volatility is expanding or contracting relative to its own history;
* whether the moving averages are rising or falling, not merely where they are;
* whether price is coiling in a tight range — which is what actually decides
  whether a setup is a range trade or a breakout;
* how liquid the name is, which bounds how much can be traded;
* momentum across several horizons rather than one recent snapshot.

Every one of those is arithmetic over bars the pipeline already fetches. Like
`src/data/levels.py`, it is computed here rather than inferred by a language
model: it costs microseconds and no tokens, it is identical on every run, and
it can be unit-tested and back-tested. The model's job is to weigh these facts,
not to estimate them from a wall of OHLC rows.

Everything here is a pure function of bars. No network, no configuration, no
side effects.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from src.data.technical import ATR_PERIOD, atr_series
from src.models import OHLCV
from src.quantities import avg_dollar_volume

# Return horizons. DEFINITION, not tunable numbers: the 1w/1m/3m/6m/12m return
# is by definition the change over 7 calendar days / 1 / 3 / 6 / 12 calendar
# months back from the latest bar's date, resolved against the series' own date
# index by `_anchor_index` (last bar on or before that date), so holidays are
# handled by the data itself. These are calendar periods, not lookback windows.
_HORIZON_1W = pd.DateOffset(days=7)
_HORIZON_1M = pd.DateOffset(months=1)
_HORIZON_3M = pd.DateOffset(months=3)
_HORIZON_6M = pd.DateOffset(months=6)
_HORIZON_12M = pd.DateOffset(months=12)

# Session-count window for the up/down volume read (_W_1M); see number_ledger.yaml.
# The 52-week range and ATR percentile use a calendar year of bars instead.
_W_1M = 21

# Sessions used to measure whether a moving average is rising or falling.
_SLOPE_LOOKBACK = 10

# The shortest stretch currently admitted as a base. It reuses the ATR period
# this file already reads (Wilder, via `src.data.technical.ATR_PERIOD`), but
# equal values do not derive base duration from volatility-estimator duration.
# Item 90 therefore keeps this as an interim arbitrary number and routes the
# detector to read the base start from present price structure. The detector
# extends the base backwards for as long as price stays inside the envelope.
#
# Replaced `_CONSOLIDATION_WINDOW = 15` / `_CONSOLIDATION_MAX_RANGE_PCT = 8.0`
# (docs/WORK.md item 58, closed 2026-09-13). The 8% was inherited convention:
# a flat percentage means a different thing on a utility than on a high-beta
# name, which is the whole complaint. There is now no percentage anywhere in
# this test.
_CONSOLIDATION_WINDOW = ATR_PERIOD


@dataclass(frozen=True)
class Gap:
    """An unfilled price gap. Gaps act as magnets and as soft levels."""

    date: str
    from_price: float
    to_price: float
    direction: str  # "up" | "down"
    sessions_ago: int
    #: Gap width in units of the name's own ATR at the session BEFORE the gap
    #: — i.e. how many ordinary days' trading the gap is worth for this
    #: instrument. None when there was not enough history for an ATR reading.
    size_atr: float | None = None

    @property
    def size_pct(self) -> float:
        if self.from_price <= 0:
            return 0.0
        return abs(self.to_price - self.from_price) / self.from_price * 100.0


@dataclass(frozen=True)
class MarketContext:
    """Everything about a symbol that is arithmetic rather than judgment."""

    last_close: float

    # Momentum across horizons, in percent.
    return_1w: float | None = None
    return_1m: float | None = None
    return_3m: float | None = None
    return_6m: float | None = None
    return_12m: float | None = None

    # Performance minus the benchmark's over the same window, in percentage
    # points. Positive means outperforming. A stock rising less than its index
    # is weak however green the candle looks.
    rel_strength_1m: float | None = None
    rel_strength_3m: float | None = None
    benchmark_symbol: str | None = None

    # Position within the trailing 52-week range.
    high_52w: float | None = None
    low_52w: float | None = None
    pct_from_52w_high: float | None = None
    pct_from_52w_low: float | None = None
    range_position_pct: float | None = None  # 0 = at the low, 100 = at the high

    # Volatility, in units a human reasons about.
    atr_pct: float | None = None  # ATR(14) as a percentage of price
    atr_percentile_1y: float | None = None  # today's ATR vs its own last year
    volatility_state: str | None = None  # "expanding" | "contracting" | "stable"

    # Trend direction of the averages, not merely price's position against them.
    ma20_slope_pct: float | None = None
    ma50_slope_pct: float | None = None
    ma200_slope_pct: float | None = None

    # Consolidation — the measurable half of "range or breakout".
    is_consolidating: bool = False
    consolidation_high: float | None = None
    consolidation_low: float | None = None
    consolidation_range_pct: float | None = None
    #: The same envelope expressed in the name's own ATR units — how many
    #: ordinary days' range the whole base spans. Instrument units, so it
    #: compares across names in a way a percentage does not.
    consolidation_range_atr: float | None = None
    sessions_in_range: int | None = None

    # Liquidity, which bounds position size.
    avg_dollar_volume_20d: float | None = None

    # Accumulation: volume on up days versus down days. Above 1 means buyers
    # are showing up with more size than sellers.
    up_down_volume_ratio: float | None = None

    unfilled_gaps: list[Gap] = field(default_factory=list)


def _anchor_index(bars: list[OHLCV], horizon: pd.DateOffset) -> int | None:
    """Index of the last bar on or before (latest bar date - `horizon`).

    None when the series starts after that calendar date (too short).
    """
    if not bars:
        return None
    target = (pd.Timestamp(bars[-1].date) - horizon).date()
    for i in range(len(bars) - 1, -1, -1):
        if bars[i].date <= target:
            return i
    return None


def _pct_change(bars: list[OHLCV], horizon: pd.DateOffset) -> float | None:
    idx = _anchor_index(bars, horizon)
    if idx is None or idx >= len(bars) - 1:
        return None
    past = float(bars[idx].close)
    if past <= 0:
        return None
    return round((float(bars[-1].close) - past) / past * 100.0, 2)


def _ma_slope(closes: np.ndarray, period: int, lookback: int) -> float | None:
    """Percent change in the moving average over `lookback` sessions.

    'Price above MA50' says little on its own; 'price above a *rising* MA50'
    is the actual signal, and a falling MA50 beneath price is a warning.
    """
    if len(closes) < period + lookback:
        return None
    kernel = np.ones(period) / period
    ma = np.convolve(closes, kernel, mode="valid")
    if len(ma) <= lookback:
        return None
    past = float(ma[-1 - lookback])
    if past <= 0:
        return None
    return round((float(ma[-1]) - past) / past * 100.0, 2)


def _find_unfilled_gaps(bars: list[OHLCV]) -> list[Gap]:
    """Gaps that price has not since traded back through.

    Walks backwards from the most recent bar so the nearest gaps come first,
    and reports every unfilled gap the walk finds — there is no cap.

    **What makes a gap a gap.** Bulkowski's definition is structural and
    carries no size floor at all: "Gaps occur when today's high is below
    yesterday's low (bearish gap), or today's low is above yesterday's high
    (bullish gap)" (https://thepatternsite.com/gaps.html, fetched
    2026-09-13). The non-overlap test below IS that definition.

    **What makes one worth reporting.** This used to be `_MIN_GAP_PCT = 2.0`,
    inherited convention, justified in prose as "smaller ones are noise that
    ordinary intraday movement fills within hours" (docs/WORK.md item 58).
    A flat percentage says two different things on two different names: 2%
    on a sleepy utility is an event, 2% on a high-beta name is a Tuesday.
    The prose reason is now the test, measured on the instrument: a gap is
    reported when it is wider than one ordinary day's trading range for
    *this* name, which is what ATR measures — so ordinary intraday movement
    demonstrably cannot close it in a session. The published prescription is
    to normalise gap size by ATR rather than by a threshold: "Take the
    distance of the gap and divide it by the ATR of the stock. This will
    give you a sense of the significance of the gap over the ATR lookback
    period."
    (https://www.tradingsetupsreview.com/complete-breakaway-gap-trading-guide/,
    fetched 2026-09-13). The resulting multiple is carried on `Gap.size_atr`
    and rendered, so the analyst sees the significance rather than a raw
    percentage. There is no percentage in this filter any more.

    The ATR used is the one in force at the session BEFORE the gap, so the
    gap bar's own outsized true range cannot raise the bar it has to clear.

    Reported to the analyst as context, and nothing more (docs/WORK.md item
    54, 2026-09-12): for one day a level on the far side of one of these
    gaps was ruled "not a floor". Bulkowski's measurement is the other way
    round — a rising window holds as support only ~20% of the time
    (https://thepatternsite.com/GaugingGaps.html) — so the gap is the weak
    floor and the level beneath it is MORE reachable, not void. No rule
    reads this list any more.
    """
    out: list[Gap] = []
    n = len(bars)
    atr = atr_series(bars)
    # `atr_series` trims the warm-up: element j belongs to bar j + ATR_PERIOD - 1.
    offset = ATR_PERIOD - 1

    def _ordinary_day_before(bar_index: int) -> float | None:
        j = (bar_index - 1) - offset
        if j < 0 or j >= atr.size:
            return None
        value = float(atr[j])
        return value if value > 0 else None

    for i in range(n - 1, 0, -1):
        prev, cur = bars[i - 1], bars[i]
        if prev.high <= 0:
            continue
        if cur.low > prev.high:  # gap up
            width = cur.low - prev.high
            from_price, to_price, direction = prev.high, cur.low, "up"
            # Filled if anything after it traded back down into the gap.
            filled = any(b.low <= prev.high for b in bars[i + 1 :])
        elif cur.high < prev.low:  # gap down
            width = prev.low - cur.high
            from_price, to_price, direction = prev.low, cur.high, "down"
            filled = any(b.high >= prev.low for b in bars[i + 1 :])
        else:
            continue
        if filled:
            continue
        ordinary_day = _ordinary_day_before(i)
        if ordinary_day is not None:
            if width < ordinary_day:
                continue
            size_atr = round(width / ordinary_day, 2)
        else:
            # No ATR reading yet (start of history). Report the gap rather
            # than invent a threshold to judge it by.
            size_atr = None
        out.append(Gap(str(cur.date), from_price, to_price, direction, n - 1 - i, size_atr))
    return out


def compute_market_context(
    bars: list[OHLCV],
    *,
    benchmark_bars: list[OHLCV] | None = None,
    benchmark_symbol: str | None = None,
) -> MarketContext | None:
    """Compute the deterministic context for one symbol.

    `benchmark_bars` should be the index or sector ETF the symbol is judged
    against. Relative strength is simply the symbol's return minus the
    benchmark's over the same window, which is how the comparison is normally
    made and requires no extra data beyond bars already fetched.

    Returns None when there are not enough bars to say anything useful.
    """
    if len(bars) < 2:
        return None

    closes = np.array([b.close for b in bars], dtype=float)
    highs = np.array([b.high for b in bars], dtype=float)
    lows = np.array([b.low for b in bars], dtype=float)
    volumes = np.array([b.volume for b in bars], dtype=float)
    last_close = float(closes[-1])
    if last_close <= 0:
        return None

    returns = {h: _pct_change(bars, h) for h in (_HORIZON_1W, _HORIZON_1M, _HORIZON_3M, _HORIZON_6M, _HORIZON_12M)}

    rel_1m = rel_3m = None
    if benchmark_bars and len(benchmark_bars) >= 2:
        for window, key in ((_HORIZON_1M, "1m"), (_HORIZON_3M, "3m")):
            mine, theirs = returns[window], _pct_change(benchmark_bars, window)
            if mine is not None and theirs is not None:
                value = round(mine - theirs, 2)
                if key == "1m":
                    rel_1m = value
                else:
                    rel_3m = value

    # 52-week range.
    # Bars on or after one calendar year before the last bar, selected by date.
    year_start = (pd.Timestamp(bars[-1].date) - _HORIZON_12M).date()
    year_idx = next(i for i, b in enumerate(bars) if b.date >= year_start)
    high_52w = float(highs[year_idx:].max())
    low_52w = float(lows[year_idx:].min())
    span = high_52w - low_52w
    range_position = round((last_close - low_52w) / span * 100.0, 1) if span > 0 else None

    # Volatility in comparable units. `atr_series` is Wilder's, shared with
    # the risk path — see src/data/technical.py. This block used to convolve
    # the true ranges with a flat kernel, which is a simple moving average
    # and not an ATR at all; the analyst was reading one volatility number
    # while position sizing and stop widening used another.
    atr_pct = atr_percentile = None
    volatility_state = None
    atr = atr_series(bars)
    if atr.size:
        atr_now = float(atr[-1])
        atr_pct = round(atr_now / last_close * 100.0, 2)
        # `atr_series` trims the warm-up: element j belongs to bar j + ATR_PERIOD - 1,
        # so pick the elements by their own bar's date, not by a shared count.
        atr_dates = [b.date for b in bars[ATR_PERIOD - 1 :][: atr.size]]
        recent = atr[[d >= year_start for d in atr_dates]]
        if recent.size > 1:
            atr_percentile = round(float((recent <= atr_now).sum()) / float(recent.size) * 100.0, 1)
            if atr_percentile >= 70:
                volatility_state = "expanding"
            elif atr_percentile <= 30:
                volatility_state = "contracting"
            else:
                volatility_state = "stable"

    # Consolidation — measurable, so it should not be a model's impression,
    # and measured against the instrument rather than against a percentage
    # (docs/WORK.md item 58, closed 2026-09-13).
    #
    # Two tests, neither of which contains a number:
    #
    # 1. CONTRACTION. The trailing window's high-low envelope is no wider
    #    than the envelope of the window of equal length immediately before
    #    it. This is Crabel's narrow-range shape, moved from a single bar to
    #    a window: "An NR4 bar is a bar whose high-to-low range is the
    #    narrowest of the most recent four bars; an NR7 is the narrowest of
    #    the most recent seven"
    #    (https://www.luxalgo.com/library/concept/nr4-nr7-narrow-range-bars/,
    #    fetched 2026-09-13), and StockCharts on the same pattern: "Crabel
    #    used the absolute range, as opposed to the percentage range"
    #    (chartschool.stockcharts.com, .../narrow-range-day-nr7, fetched
    #    2026-09-13). The comparison is against the name's own immediately
    #    preceding stretch, so it means the same thing on a utility and on a
    #    high-beta name. It is also Minervini's VCP shape — successive
    #    contractions each smaller than the last — reduced to the one
    #    comparison this detector can make on a fixed window.
    #
    # 2. SIDEWAYS. A narrow range is not sufficient: a slow steady trend
    #    also spans little over a short window. The window's range is spent
    #    either on drift (net move start to end) or on oscillation (the
    #    rest). A base oscillates more than it drifts. That is an identity,
    #    not a tuned cut: `drift <= span - drift` is exactly the old
    #    `_CONSOLIDATION_MAX_DRIFT_RATIO = 0.5`, restated as the break-even
    #    point between drift-dominated and oscillation-dominated rather than
    #    as a chosen ratio. Behaviour is unchanged; the arbitrariness was in
    #    the framing.
    #
    # Ruled out by name: O'Neil's flat base ("roughly five weeks or more of
    # sideways trade with a correction of no more than about 15 percent") —
    # the same source states "Both numbers are conventions from studies of
    # past leaders, not laws"
    # (https://www.luxalgo.com/library/concept/flat-base/, fetched
    # 2026-09-13), so adopting them swaps one convention for another with a
    # citation attached.
    is_consolidating = False
    cons_high = cons_low = cons_range_pct = None
    cons_range_atr = None
    sessions_in_range = None
    if len(bars) >= 2 * _CONSOLIDATION_WINDOW:
        window = bars[-_CONSOLIDATION_WINDOW:]
        prior = bars[-2 * _CONSOLIDATION_WINDOW : -_CONSOLIDATION_WINDOW]
        cons_high = float(max(b.high for b in window))
        cons_low = float(min(b.low for b in window))
        mid = (cons_high + cons_low) / 2.0
        if mid > 0:
            cons_range_pct = round((cons_high - cons_low) / mid * 100.0, 2)
            span = cons_high - cons_low
            prior_span = float(max(b.high for b in prior)) - float(min(b.low for b in prior))
            drift = abs(window[-1].close - window[0].close)
            oscillation = span - drift
            if atr.size and float(atr[-1]) > 0:
                cons_range_atr = round(span / float(atr[-1]), 2)
            is_consolidating = span <= prior_span and drift <= oscillation
            if is_consolidating:
                # Extend backwards while price stays inside the same envelope,
                # so a three-month base is not reported as a fifteen-day one.
                count = _CONSOLIDATION_WINDOW
                for bar in reversed(bars[:-_CONSOLIDATION_WINDOW]):
                    if cons_low <= bar.low and bar.high <= cons_high:
                        count += 1
                    else:
                        break
                sessions_in_range = count

    # 20-day average dollar volume — the SAME definition the two admission
    # gates use (`src.quantities.avg_dollar_volume`), not a third one. This
    # site previously averaged `_W_1M` = 21 bars: the generic
    # one-month-of-sessions constant leaking into a measure whose name, the
    # config key it is compared against and every log line all say 20. With
    # one halted session in the window the three implementations read
    # $11.400M / $12.000M / $11.429M on identical bars.
    _adv = avg_dollar_volume(bars)
    avg_dollar_volume_20d_usd = None if _adv is None else round(_adv, 2)

    # Accumulation vs distribution.
    up_down_ratio = None
    if len(bars) > _W_1M:
        window = bars[-_W_1M:]
        up_vol = sum(b.volume for b in window if b.close > b.open)
        down_vol = sum(b.volume for b in window if b.close < b.open)
        if down_vol > 0:
            up_down_ratio = round(up_vol / down_vol, 2)
        elif up_vol > 0:
            # No down days at all in the window. Report a capped value rather
            # than infinity so the number stays renderable and serialisable.
            up_down_ratio = 99.0

    return MarketContext(
        last_close=round(last_close, 2),
        return_1w=returns[_HORIZON_1W],
        return_1m=returns[_HORIZON_1M],
        return_3m=returns[_HORIZON_3M],
        return_6m=returns[_HORIZON_6M],
        return_12m=returns[_HORIZON_12M],
        rel_strength_1m=rel_1m,
        rel_strength_3m=rel_3m,
        benchmark_symbol=benchmark_symbol if (rel_1m is not None or rel_3m is not None) else None,
        high_52w=round(high_52w, 2),
        low_52w=round(low_52w, 2),
        pct_from_52w_high=round((last_close - high_52w) / high_52w * 100.0, 2) if high_52w > 0 else None,
        pct_from_52w_low=round((last_close - low_52w) / low_52w * 100.0, 2) if low_52w > 0 else None,
        range_position_pct=range_position,
        atr_pct=atr_pct,
        atr_percentile_1y=atr_percentile,
        volatility_state=volatility_state,
        ma20_slope_pct=_ma_slope(closes, 20, _SLOPE_LOOKBACK),
        ma50_slope_pct=_ma_slope(closes, 50, _SLOPE_LOOKBACK),
        ma200_slope_pct=_ma_slope(closes, 200, _SLOPE_LOOKBACK),
        is_consolidating=is_consolidating,
        consolidation_high=round(cons_high, 2) if cons_high else None,
        consolidation_low=round(cons_low, 2) if cons_low else None,
        consolidation_range_pct=cons_range_pct,
        consolidation_range_atr=cons_range_atr,
        sessions_in_range=sessions_in_range,
        avg_dollar_volume_20d=avg_dollar_volume_20d_usd,
        up_down_volume_ratio=up_down_ratio,
        unfilled_gaps=_find_unfilled_gaps(bars),
    )


def format_context_block(ctx: MarketContext | None, days_to_earnings: int | None = None) -> str:
    """Render the context for the Tech Analyst prompt."""
    if ctx is None:
        return "Market context: unavailable (insufficient price history)."

    def pct(value: float | None, suffix: str = "%") -> str:
        return "n/a" if value is None else f"{value:+.2f}{suffix}"

    lines = ["Market context (computed, not estimated):"]

    lines.append(
        f"  Returns: 1w {pct(ctx.return_1w)} · 1m {pct(ctx.return_1m)} · "
        f"3m {pct(ctx.return_3m)} · 6m {pct(ctx.return_6m)} · 12m {pct(ctx.return_12m)}"
    )

    if ctx.rel_strength_1m is not None or ctx.rel_strength_3m is not None:
        bench = ctx.benchmark_symbol or "benchmark"
        lines.append(
            f"  Relative strength vs {bench}: 1m {pct(ctx.rel_strength_1m, ' pts')} · "
            f"3m {pct(ctx.rel_strength_3m, ' pts')} "
            f"(positive = outperforming; a rise smaller than the benchmark's is weakness)"
        )

    if ctx.high_52w and ctx.low_52w:
        lines.append(
            f"  52w range: ${ctx.low_52w:,.2f} – ${ctx.high_52w:,.2f} · "
            f"now {ctx.range_position_pct:.0f}% of the way up · "
            f"{pct(ctx.pct_from_52w_high)} from the high"
            if ctx.range_position_pct is not None
            else f"  52w range: ${ctx.low_52w:,.2f} – ${ctx.high_52w:,.2f}"
        )

    if ctx.atr_pct is not None:
        state = f" · {ctx.volatility_state}" if ctx.volatility_state else ""
        pctile = (
            f" (ATR at the {ctx.atr_percentile_1y:.0f}th percentile of its past year)"
            if ctx.atr_percentile_1y is not None
            else ""
        )
        lines.append(f"  Volatility: ATR {ctx.atr_pct:.2f}% of price{state}{pctile}")

    slopes = [
        f"MA{p} {pct(v)}"
        for p, v in (("20", ctx.ma20_slope_pct), ("50", ctx.ma50_slope_pct), ("200", ctx.ma200_slope_pct))
        if v is not None
    ]
    if slopes:
        lines.append(f"  MA direction over {_SLOPE_LOOKBACK} sessions: " + " · ".join(slopes))

    if ctx.is_consolidating and ctx.consolidation_high and ctx.consolidation_low:
        # Width is stated in the name's OWN ATR as well as in percent: a 6%
        # base on a quiet name and a 6% base on a high-beta name are not the
        # same object, and the ATR multiple is the half that says which.
        in_atr = f", {ctx.consolidation_range_atr:.1f}× its own ATR" if ctx.consolidation_range_atr is not None else ""
        lines.append(
            f"  CONSOLIDATING: ${ctx.consolidation_low:,.2f}–${ctx.consolidation_high:,.2f} "
            f"({ctx.consolidation_range_pct:.1f}% wide{in_atr}) for "
            f"{ctx.sessions_in_range} sessions — range is no wider than the "
            f"preceding stretch of equal length, and sideways rather than drifting"
        )
    else:
        lines.append("  Not consolidating: no tight range in the recent window")

    if ctx.avg_dollar_volume_20d:
        lines.append(f"  Liquidity: ${ctx.avg_dollar_volume_20d / 1e6:,.1f}M average daily dollar volume")

    if ctx.up_down_volume_ratio is not None:
        verdict = (
            "accumulation"
            if ctx.up_down_volume_ratio > 1.2
            else ("distribution" if ctx.up_down_volume_ratio < 0.83 else "balanced")
        )
        lines.append(f"  Up/down volume (20d): {ctx.up_down_volume_ratio:.2f} — {verdict}")

    for gap in ctx.unfilled_gaps:
        # The ATR multiple, not the percentage, is what says whether this gap
        # is an event for THIS name — see `_find_unfilled_gaps`.
        in_atr = f", {gap.size_atr:.1f}× its ATR then" if gap.size_atr is not None else ""
        lines.append(
            f"  Unfilled gap {gap.direction}: ${gap.from_price:,.2f} → ${gap.to_price:,.2f} "
            f"({gap.size_pct:.1f}%{in_atr}) {gap.sessions_ago}d ago"
        )

    if days_to_earnings is not None:
        warn = "  ⚠️ " if days_to_earnings <= 10 else "  "
        lines.append(
            f"{warn}Next earnings in {days_to_earnings} sessions. A swing position held "
            f"through a report is exposed to a binary event the thesis did not choose."
        )

    return "\n".join(lines)
