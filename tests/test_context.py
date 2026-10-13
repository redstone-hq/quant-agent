"""Market context — the deterministic facts a technical analyst reasons from.

These pin behaviour the Tech Analyst now depends on. Relative strength decides
whether a name is genuinely leading; consolidation decides `setup_type`;
liquidity bounds position size in Phase 2. A silent regression here degrades
every decision downstream without failing anything loudly.
"""

from datetime import date, timedelta

from src.data.context import (
    MarketContext,
    compute_market_context,
    format_context_block,
)
from src.models import OHLCV


def _bars(closes: list[float], *, volume: int = 1_000_000, spread: float = 0.4) -> list[OHLCV]:
    start = date(2023, 1, 2)
    out = []
    prev = closes[0]
    for i, close in enumerate(closes):
        out.append(
            OHLCV(
                date=start + timedelta(days=i),
                open=prev,
                high=max(prev, close) + spread,
                low=min(prev, close) - spread,
                close=close,
                volume=volume,
            )
        )
        prev = close
    return out


def _flat(n: int, price: float = 100.0) -> list[float]:
    return [price] * n


def _ramp(n: int, start: float, end: float) -> list[float]:
    step = (end - start) / max(n - 1, 1)
    return [start + step * i for i in range(n)]


class TestReturns:
    def test_returns_measured_over_session_windows(self):
        ctx = compute_market_context(_bars(_ramp(400, 100.0, 200.0)))
        assert ctx is not None
        assert ctx.return_1m is not None and ctx.return_1m > 0
        assert ctx.return_12m is not None
        # A steady ramp must show larger gains over longer windows.
        assert ctx.return_12m > ctx.return_3m > ctx.return_1m

    def test_short_history_leaves_long_windows_unset(self):
        ctx = compute_market_context(_bars(_flat(30)))
        assert ctx is not None
        assert ctx.return_1w is not None
        assert ctx.return_12m is None

    def test_too_few_bars_returns_none(self):
        assert compute_market_context(_bars([100.0])) is None
        assert compute_market_context([]) is None


class TestRelativeStrength:
    def test_underperformer_is_negative_even_while_rising(self):
        """The gap this closes: a stock can rise and still be weak."""
        stock = _bars(_ramp(120, 100.0, 105.0))  # +5%
        index = _bars(_ramp(120, 100.0, 115.0))  # +15%
        ctx = compute_market_context(stock, benchmark_bars=index, benchmark_symbol="SPY")
        assert ctx is not None
        assert ctx.return_1m is not None and ctx.return_1m > 0, "stock did rise"
        assert ctx.rel_strength_1m is not None and ctx.rel_strength_1m < 0, (
            "rising less than the benchmark must read as relative weakness"
        )

    def test_outperformer_is_positive(self):
        stock = _bars(_ramp(120, 100.0, 130.0))
        index = _bars(_ramp(120, 100.0, 105.0))
        ctx = compute_market_context(stock, benchmark_bars=index, benchmark_symbol="SPY")
        assert ctx is not None and ctx.rel_strength_1m is not None
        assert ctx.rel_strength_1m > 0

    def test_absent_benchmark_leaves_relative_strength_unset(self):
        ctx = compute_market_context(_bars(_ramp(120, 100.0, 130.0)))
        assert ctx is not None
        assert ctx.rel_strength_1m is None
        assert ctx.benchmark_symbol is None


class TestRangePosition:
    def test_at_the_high_reads_as_full_range(self):
        ctx = compute_market_context(_bars(_ramp(300, 50.0, 150.0)))
        assert ctx is not None and ctx.range_position_pct is not None
        assert ctx.range_position_pct > 90
        assert ctx.pct_from_52w_high is not None and ctx.pct_from_52w_high > -5

    def test_at_the_low_reads_as_empty_range(self):
        ctx = compute_market_context(_bars(_ramp(300, 150.0, 50.0)))
        assert ctx is not None and ctx.range_position_pct is not None
        assert ctx.range_position_pct < 10


class TestVolatility:
    def test_atr_expressed_as_percent_of_price(self):
        ctx = compute_market_context(_bars(_flat(100, price=100.0), spread=1.0))
        assert ctx is not None and ctx.atr_pct is not None
        assert 0 < ctx.atr_pct < 20

    def test_volatility_state_is_classified(self):
        ctx = compute_market_context(_bars(_ramp(300, 100.0, 120.0)))
        assert ctx is not None
        assert ctx.volatility_state in {"expanding", "contracting", "stable", None}


class TestMovingAverageSlopes:
    def test_rising_market_has_positive_slopes(self):
        ctx = compute_market_context(_bars(_ramp(400, 100.0, 200.0)))
        assert ctx is not None
        assert ctx.ma20_slope_pct is not None and ctx.ma20_slope_pct > 0
        assert ctx.ma50_slope_pct is not None and ctx.ma50_slope_pct > 0

    def test_falling_market_has_negative_slopes(self):
        ctx = compute_market_context(_bars(_ramp(300, 200.0, 100.0)))
        assert ctx is not None
        assert ctx.ma20_slope_pct is not None and ctx.ma20_slope_pct < 0

    def test_slope_unset_without_enough_history(self):
        ctx = compute_market_context(_bars(_flat(40)))
        assert ctx is not None
        assert ctx.ma200_slope_pct is None


class TestConsolidation:
    def test_tight_range_is_detected(self):
        ctx = compute_market_context(_bars(_ramp(200, 80.0, 100.0) + _flat(40, 100.0)))
        assert ctx is not None
        assert ctx.is_consolidating
        assert ctx.consolidation_range_pct is not None
        assert ctx.sessions_in_range is not None and ctx.sessions_in_range >= 15

    def test_trending_price_is_not_consolidating(self):
        ctx = compute_market_context(_bars(_ramp(200, 100.0, 300.0)))
        assert ctx is not None
        assert not ctx.is_consolidating

    def test_range_extends_backwards_through_the_whole_base(self):
        """A three-month base must not be reported as a fifteen-day one."""
        ctx = compute_market_context(_bars(_ramp(100, 50.0, 100.0) + _flat(60, 100.0)))
        assert ctx is not None and ctx.is_consolidating
        assert ctx.sessions_in_range is not None and ctx.sessions_in_range > 30


class TestLiquidityAndVolume:
    def test_dollar_volume_is_price_times_size(self):
        ctx = compute_market_context(_bars(_flat(60, price=50.0), volume=2_000_000))
        assert ctx is not None and ctx.avg_dollar_volume_20d is not None
        assert abs(ctx.avg_dollar_volume_20d - 100_000_000) < 1_000_000

    def test_up_down_volume_ratio_reflects_direction(self):
        rising = compute_market_context(_bars(_ramp(60, 100.0, 140.0)))
        falling = compute_market_context(_bars(_ramp(60, 140.0, 100.0)))
        assert rising is not None and falling is not None
        assert rising.up_down_volume_ratio is not None
        assert falling.up_down_volume_ratio is not None
        assert rising.up_down_volume_ratio > falling.up_down_volume_ratio


class TestGaps:
    def test_unfilled_gap_up_is_reported(self):
        bars = _bars(_flat(40, 100.0))
        # Jump to a level price never revisits.
        bars += _bars(_flat(20, 130.0))[-20:]
        ctx = compute_market_context(bars)
        assert ctx is not None
        assert any(g.direction == "up" for g in ctx.unfilled_gaps)

    def test_filled_gap_is_not_reported(self):
        bars = _bars(_flat(30, 100.0) + _flat(10, 130.0) + _flat(30, 100.0))
        ctx = compute_market_context(bars)
        assert ctx is not None
        assert ctx.unfilled_gaps == []

    def test_gap_list_is_bounded(self):
        ctx = compute_market_context(_bars(_ramp(300, 10.0, 400.0)))
        assert ctx is not None
        assert len(ctx.unfilled_gaps) <= 3


class TestPromptBlock:
    def test_missing_context_is_stated_plainly(self):
        assert "unavailable" in format_context_block(None)

    def test_relative_strength_is_explained_not_just_printed(self):
        stock = _bars(_ramp(120, 100.0, 105.0))
        index = _bars(_ramp(120, 100.0, 120.0))
        ctx = compute_market_context(stock, benchmark_bars=index, benchmark_symbol="SPY")
        text = format_context_block(ctx)
        assert "SPY" in text
        assert "outperforming" in text

    def test_imminent_earnings_is_flagged(self):
        ctx = compute_market_context(_bars(_flat(60)))
        text = format_context_block(ctx, days_to_earnings=3)
        assert "3 sessions" in text
        assert "binary event" in text

    def test_no_earnings_date_means_no_earnings_line(self):
        ctx = compute_market_context(_bars(_flat(60)))
        assert "earnings" not in format_context_block(ctx).lower()


def test_computation_is_deterministic():
    """Same bars in, same context out — the property that enables backtesting."""
    bars = _bars(_ramp(250, 100.0, 180.0))
    first = compute_market_context(bars)
    for _ in range(3):
        assert compute_market_context(bars) == first


def test_context_is_a_frozen_value_object():
    ctx = compute_market_context(_bars(_flat(60)))
    assert isinstance(ctx, MarketContext)
    try:
        ctx.last_close = 1.0  # type: ignore[misc]
    except Exception:
        return
    raise AssertionError("MarketContext must be immutable")


# ---------------------------------------------------------------------------
# docs/WORK.md item 58 (closed 2026-09-13): the shape constants are read off
# the instrument, not picked. These pin the READING, not any number — a
# regression here would silently reintroduce a flat percentage.
# ---------------------------------------------------------------------------


def _steady(n: int, *, price: float, half_range: float, start_day: int = 0) -> list[OHLCV]:
    """`n` bars at `price` whose every true range is exactly 2*half_range."""
    base = date(2023, 1, 2) + timedelta(days=start_day)
    return [
        OHLCV(
            date=base + timedelta(days=i),
            open=price,
            high=price + half_range,
            low=price - half_range,
            close=price,
            volume=1_000_000,
        )
        for i in range(n)
    ]


class TestInstrumentReadShapeConstants:
    """No flat percentage may decide what the analyst is told."""

    def test_module_states_no_percentage_thresholds(self):
        """The named convention constants are gone, not renamed."""
        import src.data.context as context_mod

        for gone in (
            "_MIN_GAP_PCT",
            "_CONSOLIDATION_MAX_RANGE_PCT",
            "_CONSOLIDATION_MAX_DRIFT_RATIO",
        ):
            assert not hasattr(context_mod, gone), f"{gone} is back — item 58 replaced it with an instrument read"

    def test_consolidation_window_is_the_atr_period_not_a_picked_number(self):
        from src.data.context import _CONSOLIDATION_WINDOW
        from src.data.technical import ATR_PERIOD

        assert _CONSOLIDATION_WINDOW == ATR_PERIOD

    def test_same_percentage_gap_is_an_event_on_a_quiet_name(self):
        quiet = _steady(40, price=100.0, half_range=0.2)
        # Gap up to a level price never revisits. Width 2.8 vs an ATR of 0.4.
        quiet += _steady(6, price=104.0, half_range=0.2, start_day=40)
        ctx = compute_market_context(quiet)
        assert ctx is not None
        ups = [g for g in ctx.unfilled_gaps if g.direction == "up"]
        assert ups, "a gap many times the name's ordinary day must be reported"
        assert ups[0].size_atr is not None and ups[0].size_atr > 1.0

    def test_the_same_percentage_gap_is_noise_on_a_volatile_name(self):
        """Identical ~3.6% gap, ATR ten times larger — not worth reporting."""
        volatile = _steady(40, price=100.0, half_range=3.0)
        volatile += _steady(6, price=104.0, half_range=0.4, start_day=40)
        ctx = compute_market_context(volatile)
        assert ctx is not None
        assert [g for g in ctx.unfilled_gaps if g.direction == "up"] == [], (
            "a gap smaller than the name's own ordinary day is not an event"
        )

    def test_gap_size_is_reported_in_the_names_own_atr(self):
        quiet = _steady(40, price=100.0, half_range=0.2)
        quiet += _steady(6, price=104.0, half_range=0.2, start_day=40)
        ctx = compute_market_context(quiet)
        assert ctx is not None
        text = format_context_block(ctx)
        assert "× its ATR then" in text

    def test_contraction_against_the_names_own_prior_stretch(self):
        """Wide, then tight and sideways — a base by contraction."""
        bars = _steady(20, price=100.0, half_range=6.0)
        bars += _steady(14, price=100.0, half_range=1.0, start_day=20)
        ctx = compute_market_context(bars)
        assert ctx is not None and ctx.is_consolidating

    def test_expansion_is_not_a_base_however_tight_it_looks(self):
        """Tight, then wider — the envelope grew, so it is not coiling."""
        bars = _steady(20, price=100.0, half_range=1.0)
        bars += _steady(14, price=100.0, half_range=6.0, start_day=20)
        ctx = compute_market_context(bars)
        assert ctx is not None and not ctx.is_consolidating

    def test_a_base_wider_than_the_old_eight_percent_still_qualifies(self):
        """The old flat 8% refused this; the instrument says it is a base.

        A high-beta name whose envelope has halved is consolidating even
        though 20% of price is nowhere near 8%.
        """
        bars = _steady(20, price=100.0, half_range=20.0)
        bars += _steady(14, price=100.0, half_range=10.0, start_day=20)
        ctx = compute_market_context(bars)
        assert ctx is not None and ctx.is_consolidating
        assert ctx.consolidation_range_pct is not None
        assert ctx.consolidation_range_pct > 8.0
        assert ctx.consolidation_range_atr is not None
        assert "× its own ATR" in format_context_block(ctx)

    def test_a_steady_trend_is_still_not_a_base(self):
        """Drift must not exceed oscillation — the sideways half is intact."""
        ctx = compute_market_context(_bars(_ramp(200, 100.0, 300.0)))
        assert ctx is not None and not ctx.is_consolidating
