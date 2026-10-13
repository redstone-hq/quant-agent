"""Deterministic-layer backtester — hand-computed proofs.

Every price/P&L assertion below is checked against a HARD LITERAL computed
by hand (or with a plain, independent arithmetic snippet), never by calling
the function under test a second time and comparing it to itself.

Two levels of construction are used:

* `_build_long_win_series()` drives the FULL `run_backtest()` engine —
  signal detection (`TradingPipeline._has_actionable_signal_fn`), structural
  levels (`find_structural_levels`), stop resolution
  (`PortfolioConstructor._resolve_stop` / `._widen_stop_past_noise`), sizing,
  and the day-by-day walk-forward — over a synthetic 212-bar OHLCV series
  engineered so every one of those steps produces a KNOWN, hand-verifiable
  number (see the docstring inside the builder for exactly how).

* The short-trade and stop-hit tests call `_check_exit` / `_close_trade`
  directly with a hand-built `_OpenPosition`. Those two functions ARE what
  the day-by-day loop calls for every exit — this is the same engine code,
  invoked directly instead of through signal generation. The real,
  history-driven run in this repo's current state only ever opens LONGS
  (see `src/backtest/engine.py`'s module docstring: the live system cannot
  short yet, and the deterministic prefilter/levels path this engine reuses
  has no short-side entry rule to borrow), so this is also the only way to
  exercise the short side today without inventing an entry rule nothing
  live corroborates.
"""

from __future__ import annotations

from datetime import date, timedelta
from types import SimpleNamespace

import pytest

from src.backtest.engine import (
    MIN_BARS_FOR_SIGNAL,
    BacktestParams,
    Trade,
    _OpenPosition,
    _budget_binds,
    _check_exit,
    _close_trade,
    _fill_price,
    run_backtest,
)
from src.backtest.metrics import (
    compute_metrics,
    format_ab_table,
    format_caveats,
    format_metrics_report,
)
from src.config import RiskConfig
from src.models import OHLCV

SYMBOL = "ZZZZ"
BASE_DATE = date(2020, 1, 1)


def _risk_config(**overrides) -> SimpleNamespace:
    """`run_backtest` only ever reads `config.risk` (see engine.py) — a
    namespace exposing just that attribute is the real, faithful surface,
    not a stand-in. Defaults are permissive (no single-name/portfolio
    clamp) so a test that isn't specifically about a gate doesn't
    accidentally trip one."""
    fields = dict(
        max_position_pct=100.0,
        max_total_position_pct=100.0,
        require_stop_loss=True,
        max_portfolio_risk_pct=25.0,
        max_position_risk_pct=5.0,
        min_position_risk_pct=0.5,
        max_cluster_risk_share_pct=40.0,
        # Tracks the production default (2.5 since 2026-09-10, 1.5 before
        # that from 2026-09-04); a backtest fixture pinned to a stale floor
        # would silently backtest a rule the desk no longer runs.
        min_stop_atr_multiple=2.5,
    )
    fields.update(overrides)
    # Spec §9.4's graduated agreement ceiling was retired 2026-09-14, so
    # this fixture no longer has to carry a flat schedule to satisfy the
    # "can only narrow, never widen" invariant. The backtest engine sizes
    # off `max_position_risk_pct` directly and never went through the
    # constructor's agreement path in the first place.
    return SimpleNamespace(risk=RiskConfig(**fields))


def _build_long_win_series() -> list[OHLCV]:
    """A 212-bar synthetic series with a known signal, entry, stop, target
    and exit, by construction:

    * Bars 0..208 (209 bars) are padding: a quiet baseline around $100 with
      a tiny strictly-monotonic drift (so no two bars ever TIE for local
      extremity — a perfectly flat/repeating series creates a degenerate,
      massively-clustered pivot right at the baseline price, which drowns
      out the deliberate levels below; see the probe work that found this).
      `MIN_BARS_FOR_SIGNAL` (210) bars of history are required before the
      engine will evaluate a symbol at all, so nothing in this padding is
      ever looked at for a signal — bars 0..208 give exactly 209 bars of
      history on day 208, one short of the threshold.
    * Two deliberate, confirmed swing lows at exactly $95.00 (bars 40 and
      140) and two at exactly $125.00 (bars 70 and 170), each with
      `touches=2` (`MIN_TOUCHES`), so `find_structural_levels` reports
      support=$95.00 and resistance=$125.00 with nothing else nearby to
      compete.

      **Those four bars are TIGHT around their own pivot price, and that is
      load-bearing since 2026-09-30 (docs/WORK.md item 55).** A level's
      extent is no longer a percentage of its price; it is the combined
      traded span of the bars that drew it, and pivots group when those
      spans overlap. The dip bars used to be written as $95.00-$101.50 and
      the spike bars as $98.50-$125.00 — ranges that overlap each other, so
      under the measured definition all four pivots are ONE blob of
      structure spanning $95 to $125, priced at its mean $110: neither a
      support nor a resistance, and no stop can rest on it. That was the
      fixture describing a clean $95 shelf in the comment while planting a
      $95 wick on a bar that also traded at $101.50. Drawn tight
      ($95.00-$97.00 and $123.50-$125.00) the two groups no longer touch,
      and the series means what this docstring always said it meant. Every
      hand-computed number below is UNCHANGED by the redraw.
    * Bar 209 (the 210th bar, first one ever evaluated) closes back at the
      baseline (~$100) — support/resistance classification is relative to
      THIS close — with volume 5x the prior 10-day average on the last 5
      days, which reliably trips the deterministic prefilter's
      volume-change branch (`TradingPipeline._has_actionable_signal_fn`).
    * Bar 210 (signal + 1) opens at exactly $105.00 — the entry fill.
    * Bar 211 opens $106, and its HIGH ($126) breaches the $125.00 target
      while its LOW ($110) stays well clear of the $95.00 stop, so the
      position is NOT exited at the target (live has no target exit); it
      is held and closed at the last close because the data ends.

    By hand: entry $105.00, stop $95.00 (structural; $10 away vs a padding
    ATR far too small for `min_stop_atr_multiple=2.5` to push it any
    further out — see the noise-band arithmetic below), target $125.00.
    At 5% risk on $100,000 equity: shares = floor(100000*0.05/10) = 500.
    pnl = (125.00 - 105.00) * 500 = $10,000.00. r_multiple = 20/10 = 2.0.

    **The resistance is $125.00, not $115.00, and that is load-bearing.**
    At $115.00 this trade's reward:risk is (115-105)/(105-95) = 1.00, and
    since 2026-09-02 `_widen_stop_past_noise` applies
    `min_reward_risk_after_widening` (1.5) to the shipping geometry on
    EVERY path — including a stop already outside the noise band, which is
    this one. The live desk would refuse this entry, so a backtest that
    took it would be modelling a desk that does not exist. At $125.00 the
    ratio is 2.00 and the trade is one the constructor would actually
    place.
    """
    bars: list[OHLCV] = []
    d = BASE_DATE
    n_pad = MIN_BARS_FOR_SIGNAL - 1  # 209
    dip_indices = {40, 140}
    spike_indices = {70, 170}
    for i in range(n_pad):
        drift = 0.0002 * i  # negligible (<= ~$0.04 over the whole window)
        if i in dip_indices:
            o, h, l, c = 96.5 + drift, 97.00 + drift, 95.00, 96.0 + drift
        elif i in spike_indices:
            o, h, l, c = 124.0 + drift, 125.00, 123.50 + drift, 124.5 + drift
        else:
            # A $3 daily range (ATR ~3) — a real instrument's, not a
            # hairline. Since 2026-09-12 the level scan's relevance window
            # is read from ATR (`horizon_reach` at the 60-session cap, ~11.6
            # ATR), so a $0.40-range series would put the $125 shelf 60+ ATR
            # away: correctly irrelevant on any real chart, and the trade
            # this test hand-computes would never exist. At ATR ~3 the
            # $25 distance is ~8 ATR, inside reach, and the stop arithmetic
            # below is unchanged (2.5 x 3 = 7.5 < the $10 structural stop).
            o, h, l, c = 99.9 + drift, 101.5 + drift, 98.5 + drift, 100.0 + drift
        vol = 5_000_000 if i >= n_pad - 5 else 1_000_000
        bars.append(OHLCV(date=d, open=round(o, 4), high=round(h, 4), low=round(l, 4), close=round(c, 4), volume=vol))
        d += timedelta(days=1)

    drift = 0.0002 * n_pad
    bars.append(
        OHLCV(  # bar 209: signal day
            date=d,
            open=round(99.9 + drift, 4),
            high=round(101.5 + drift, 4),
            low=round(98.5 + drift, 4),
            close=round(100.0 + drift, 4),
            volume=5_000_000,
        )
    )
    d += timedelta(days=1)

    bars.append(
        OHLCV(  # bar 210: entry fill at the open
            date=d,
            open=105.00,
            high=106.0,
            low=104.0,
            close=105.5,
            volume=1_000_000,
        )
    )
    d += timedelta(days=1)

    bars.append(
        OHLCV(  # bar 211: target breached, stop untouched
            date=d,
            open=106.0,
            high=126.0,
            low=110.0,
            close=125.5,
            volume=1_000_000,
        )
    )
    return bars


def _run(bars: list[OHLCV], **risk_overrides) -> tuple:
    params = BacktestParams(
        start=bars[0].date,
        end=bars[-1].date,
        max_hold_days=20,
        initial_equity=100_000.0,
        slippage_bps=0.0,
    )
    config = _risk_config(**risk_overrides)
    result = run_backtest(config=config, bars_by_symbol={SYMBOL: bars}, params=params)
    return config, params, result


# ---------------------------------------------------------------------------
# Hand-computed single LONG trade, through the full engine
# ---------------------------------------------------------------------------


def test_hand_computed_long_trade():
    bars = _build_long_win_series()
    _, _, result = _run(bars)

    assert len(result.trades) == 1
    t = result.trades[0]

    assert t.symbol == SYMBOL
    assert t.direction == "long"
    assert t.signal_date == BASE_DATE + timedelta(days=209)
    assert t.entry_date == BASE_DATE + timedelta(days=210)
    assert t.entry_price == 105.0  # next day's OPEN, not the signal day's close
    assert t.stop_price == 95.0  # the structural support, unwidened
    assert t.target_price == 125.0  # the structural resistance
    assert t.exit_date == BASE_DATE + timedelta(days=211)
    # No target exit: bar 211 breached the $125 target but the position
    # stays open (live never sells at a target) and is closed at the last
    # close because the data ends.
    assert t.exit_price == 125.5
    assert t.exit_reason == "end_of_data"
    assert t.shares == 500
    assert t.hold_days == 1
    assert t.pnl == 10250.0
    assert t.r_multiple == 2.05
    assert result.final_equity == 110_250.0

    # Insufficient-history accounting: exactly the 209 days below the
    # MIN_BARS_FOR_SIGNAL threshold were skipped, not silently dropped.
    assert result.skipped_symbol_days == 209
    # One symbol, one fully-granted request: the budget did not bind.
    assert result.entry_days >= 1
    assert result.binding_budget_days == 0


# ---------------------------------------------------------------------------
# Hand-computed single SHORT trade — engine exit/close mechanics directly
# ---------------------------------------------------------------------------


def test_hand_computed_short_trade():
    """entry $50.00, stop $55.00 (above entry, risk $5/share), target
    $40.00. The day's LOW reaches $39.00, breaching the target; P&L is
    signed the short way: (entry - exit) * shares = (50 - 40) * 200 =
    $2,000.00. r_multiple = pnl / (shares * risk_per_share) =
    2000 / (200 * 5) = 2.0."""
    pos = _OpenPosition(
        symbol="SHRT",
        direction="short",
        signal_date=date(2021, 2, 1),
        entry_date=date(2021, 2, 2),
        entry_index=20,
        entry_price=50.0,
        stop_initial=55.0,
        stop=55.0,
        target=40.0,
        setup_type="breakout",
        shares=200,
        risk_pct=5.0,
    )
    bar = OHLCV(date=date(2021, 2, 3), open=49.0, high=48.0, low=39.0, close=40.5, volume=1000)

    # The target is breached but is not an exit: the short stays open.
    assert _check_exit(pos, bar, idx=21, max_hold_days=20) == (None, None)

    # P&L sign/R maths for a short, closed at $40.00 (exit reason is a label here).
    trade = _close_trade(pos, exit_idx=21, exit_date_=bar.date, raw_exit=40.0, exit_reason="horizon", slippage_bps=0.0)
    assert trade.direction == "short"
    assert trade.entry_price == 50.0
    assert trade.stop_price == 55.0
    assert trade.exit_price == 40.0
    assert trade.pnl == 2000.0
    assert trade.r_multiple == 2.0
    assert trade.hold_days == 1


# ---------------------------------------------------------------------------
# No-look-ahead proof
# ---------------------------------------------------------------------------


def test_no_look_ahead_entry_is_next_days_open_not_signal_days_own_move():
    """The signal day's own bar gets an extra, dramatic favourable spike
    (high $112, well above where the position will eventually be bought)
    that fully reverts by that day's close. If the engine captured that
    move — entering at the signal day's close or its high instead of
    waiting for the next session — the entry price would land near $100 or
    $112. It must not: the entry is the FOLLOWING day's open, $105.00,
    unconditionally."""
    bars = _build_long_win_series()
    signal_bar = bars[209]
    bars[209] = OHLCV(
        date=signal_bar.date,
        open=signal_bar.open,
        high=112.0,
        low=signal_bar.low,
        close=signal_bar.close,
        volume=signal_bar.volume,
    )

    _, _, result = _run(bars)
    assert len(result.trades) == 1
    t = result.trades[0]

    assert t.signal_date == bars[209].date
    assert t.entry_date == bars[210].date
    assert t.entry_price == 105.0
    assert t.entry_price == bars[210].open
    # Explicitly NOT the signal day's own price action.
    assert t.entry_price != bars[209].close
    assert t.entry_price != bars[209].high


# ---------------------------------------------------------------------------
# Stop-hit: exit price and slippage-bounded loss
# ---------------------------------------------------------------------------


def test_stop_hit_exit_price_and_slippage_bounded_loss():
    """entry $100, stop $90. The day's LOW pierces the stop at $85 — the
    exit fills AT the stop level ($90), not at the bar's low, then
    slippage (50 bps) is applied against the seller: 90 * (1 - 0.005) =
    $89.55 exactly. The loss is bounded by the stop distance ($10) plus the
    slippage haircut (0.5% of $90 = $0.45) = $10.45 — not by how far the
    bar's low undershot the stop ($15)."""
    pos = _OpenPosition(
        symbol="STOP",
        direction="long",
        signal_date=date(2021, 1, 1),
        entry_date=date(2021, 1, 2),
        entry_index=10,
        entry_price=100.0,
        stop_initial=90.0,
        stop=90.0,
        target=120.0,
        setup_type="range",
        shares=100,
        risk_pct=5.0,
    )
    bar = OHLCV(date=date(2021, 1, 3), open=95.0, high=96.0, low=85.0, close=88.0, volume=1000)

    reason, raw_exit = _check_exit(pos, bar, idx=11, max_hold_days=20)
    assert reason == "stop"
    assert raw_exit == 90.0  # the stop LEVEL, not the bar's low

    trade = _close_trade(
        pos, exit_idx=11, exit_date_=bar.date, raw_exit=raw_exit, exit_reason=reason, slippage_bps=50.0
    )
    assert trade.exit_price == 89.55
    assert trade.pnl == -1045.0

    stop_distance = pos.entry_price - pos.stop_initial  # 10.0
    slippage_dollars = pos.stop_initial * (50.0 / 10_000.0)  # 0.45
    loss = pos.entry_price - trade.exit_price
    assert loss == pytest.approx(stop_distance + slippage_dollars, abs=1e-9)
    # And strictly less than what an unbounded fill at the bar's actual low
    # ($85, a $15 loss) would have been.
    assert loss < pos.entry_price - bar.low


# ---------------------------------------------------------------------------
# Metrics — hand-computed against a literal 5-trade set
# ---------------------------------------------------------------------------


def _literal_trade(pnl: float, r_multiple: float, hold_days: int, exit_day: int) -> Trade:
    return Trade(
        symbol="ABCD",
        direction="long",
        signal_date=date(2022, 1, 1),
        entry_date=date(2022, 1, 2),
        entry_price=100.0,
        stop_price=90.0,
        target_price=110.0,
        exit_date=date(2022, 1, 1) + timedelta(days=exit_day),
        exit_price=100.0 + pnl / 10.0,
        exit_reason="horizon",
        shares=10,
        risk_pct=5.0,
        setup_type="range",
        hold_days=hold_days,
        pnl=pnl,
        r_multiple=r_multiple,
    )


def test_metrics_hand_computed_five_trades():
    """pnls [1000, 1000, -500, 2000, -1000] on $100,000 initial equity, in
    exit-date order. By hand:
      win_rate    = 3/5 * 100          = 60.0
      avg_win     = (1000+1000+2000)/3 = 1333.33 (rounded)
      avg_loss    = (-500-1000)/2      = -750.0
      ratio       = 1333.33.../750     = 1.778 (rounded)
      expectancy  = 2500/5             = 500.0
      expectancy_r (r-multiples 2,2,-1,4,-2) = 5/5 = 1.0
      avg_hold    = (3+5+2+10+4)/5     = 4.8
      equity walk: 100000 -> 101000 -> 102000 -> 101500 -> 103500 -> 102500
      running peaks: 101000, 102000, 102000, 103500, 103500
      drawdowns($): 0, 0, 500, 0, 1000        -> max $ = 1000.0
      drawdowns(%): 0, 0, 0.4902, 0, 0.9662%  -> max % = 0.97 (rounded)
      final equity = 102500.0; total_return = 2.5%
    """
    trades = [
        _literal_trade(pnl=1000, r_multiple=2.0, hold_days=3, exit_day=1),
        _literal_trade(pnl=1000, r_multiple=2.0, hold_days=5, exit_day=2),
        _literal_trade(pnl=-500, r_multiple=-1.0, hold_days=2, exit_day=3),
        _literal_trade(pnl=2000, r_multiple=4.0, hold_days=10, exit_day=4),
        _literal_trade(pnl=-1000, r_multiple=-2.0, hold_days=4, exit_day=5),
    ]

    m = compute_metrics(trades, initial_equity=100_000.0)

    assert m.trade_count == 5
    assert m.win_rate_pct == 60.0
    assert m.avg_win == 1333.33
    assert m.avg_loss == -750.0
    assert m.avg_win_loss_ratio == 1.778
    assert m.expectancy_dollars == 500.0
    assert m.expectancy_r == 1.0
    assert m.avg_hold_days == 4.8
    assert m.max_drawdown_dollars == 1000.0
    assert m.max_drawdown_pct == 0.97
    assert m.total_return_pct == 2.5
    assert m.final_equity == 102_500.0


def test_metrics_order_independence():
    """`compute_metrics` sorts by (exit_date, symbol, entry_date) itself, so
    handing it the same 5 trades in a shuffled order must not change the
    drawdown walk (which depends on chronological order)."""
    trades = [
        _literal_trade(pnl=1000, r_multiple=2.0, hold_days=3, exit_day=1),
        _literal_trade(pnl=1000, r_multiple=2.0, hold_days=5, exit_day=2),
        _literal_trade(pnl=-500, r_multiple=-1.0, hold_days=2, exit_day=3),
        _literal_trade(pnl=2000, r_multiple=4.0, hold_days=10, exit_day=4),
        _literal_trade(pnl=-1000, r_multiple=-2.0, hold_days=4, exit_day=5),
    ]
    shuffled = [trades[3], trades[0], trades[4], trades[1], trades[2]]

    m_ordered = compute_metrics(trades, initial_equity=100_000.0)
    m_shuffled = compute_metrics(shuffled, initial_equity=100_000.0)
    assert m_ordered == m_shuffled


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


def test_determinism_same_inputs_same_output():
    bars = _build_long_win_series()
    _, _, result_1 = _run(bars)
    _, _, result_2 = _run(bars)

    assert result_1.trades == result_2.trades
    assert result_1.final_equity == result_2.final_equity
    assert result_1.skipped_symbol_days == result_2.skipped_symbol_days
    assert result_1.binding_budget_days == result_2.binding_budget_days
    assert result_1.entry_days == result_2.entry_days


# ---------------------------------------------------------------------------
# A/B — two configs differing in one parameter
# ---------------------------------------------------------------------------


def test_ab_two_configs_one_parameter_produce_different_labelled_results():
    """Configs A and B are identical except `max_position_risk_pct`
    (5.0 vs 2.5). Half the requested risk halves the position: 500 shares
    -> 250, $10,250 P&L -> $5,125 — exactly proportional, and each result
    stays correctly attributed to its own config."""
    bars = _build_long_win_series()
    _, _, result_a = _run(bars, max_position_risk_pct=5.0)
    _, _, result_b = _run(bars, max_position_risk_pct=2.5)

    assert result_a.trades[0].shares == 500
    assert result_a.trades[0].pnl == 10250.0
    assert result_b.trades[0].shares == 250
    assert result_b.trades[0].pnl == 5125.0
    assert result_a.trades != result_b.trades

    metrics_a = compute_metrics(result_a.trades, 100_000.0)
    metrics_b = compute_metrics(result_b.trades, 100_000.0)
    assert metrics_a.expectancy_dollars == 10250.0
    assert metrics_b.expectancy_dollars == 5125.0

    table = format_ab_table(
        "A (risk 5.0)",
        metrics_a,
        "B (risk 2.5)",
        metrics_b,
        binding_budget_days_a=result_a.binding_budget_days,
        binding_budget_days_b=result_b.binding_budget_days,
        entry_days_a=result_a.entry_days,
        entry_days_b=result_b.entry_days,
    )
    assert "A (risk 5.0)" in table
    assert "B (risk 2.5)" in table
    assert "$10,250.00" in table
    assert "$5,125.00" in table
    assert "$-5,125.00" in table  # the delta column
    assert "Binding-budget days" in table
    assert "ticker spelling" in table


# ---------------------------------------------------------------------------
# Small pure-function sanity checks
# ---------------------------------------------------------------------------


def test_fill_price_slippage_direction():
    # Buying (long open / short close) costs MORE than the raw price.
    assert _fill_price(100.0, "long", "open", 100.0) == pytest.approx(101.0)
    assert _fill_price(100.0, "short", "close", 100.0) == pytest.approx(101.0)
    # Selling (long close / short open) receives LESS than the raw price.
    assert _fill_price(100.0, "long", "close", 100.0) == pytest.approx(99.0)
    assert _fill_price(100.0, "short", "open", 100.0) == pytest.approx(99.0)
    # Zero slippage is a no-op.
    assert _fill_price(100.0, "long", "open", 0.0) == 100.0


# ---------------------------------------------------------------------------
# Binding-budget day reporting (item 64) — report-only, not a ranking
# ---------------------------------------------------------------------------


def test_budget_binds_when_any_positive_request_is_limited():
    from src.risk.budget import BudgetAllocation, RiskGrant

    full = RiskGrant("AAA", 5.0, 5.0)
    cut = RiskGrant("ZZZ", 5.0, 0.0, limited_by="total_ceiling")
    bound = BudgetAllocation(
        grants={"AAA": full, "ZZZ": cut},
        committed_pct=5.0,
        ceiling_pct=5.0,
        cluster_pct={},
    )
    assert _budget_binds(bound)
    free = BudgetAllocation(
        grants={"AAA": full},
        committed_pct=5.0,
        ceiling_pct=25.0,
        cluster_pct={},
    )
    assert not _budget_binds(free)


def test_binding_day_is_counted_and_served_alphabetically():
    """Two equal-size asks, 5% ceiling, 5% per name: only one fits.
    This engine still has no ranking, so AAA is funded and ZZZ is not
    because A comes before Z. That is the existing behaviour, labeled
    and counted — not a ranking fix.
    """
    bars_a = _build_long_win_series()
    bars_z = _build_long_win_series()
    params = BacktestParams(
        start=bars_a[0].date,
        end=bars_a[-1].date,
        max_hold_days=20,
        initial_equity=100_000.0,
        slippage_bps=0.0,
    )
    config = _risk_config(
        max_portfolio_risk_pct=5.0,
        max_position_risk_pct=5.0,
        max_cluster_risk_share_pct=100.0,
    )
    result = run_backtest(
        config=config,
        bars_by_symbol={"AAA": bars_a, "ZZZ": bars_z},
        params=params,
    )
    traded = {t.symbol for t in result.trades}
    assert "AAA" in traded
    assert "ZZZ" not in traded
    assert result.binding_budget_days >= 1
    assert result.entry_days >= result.binding_budget_days


def test_every_backtest_result_reports_binding_days_and_labels_the_tie_break():
    """Count on every result, including zero. Alphabetical labeled as what
    it is. The old 'these numbers measure the portfolio risk budget' claim
    must not survive — that was the lie item 64 exists to stop.
    """
    caveats = format_caveats(
        slippage_bps=5.0,
        slippage_source="test",
        skipped=0,
        min_bars=210,
        symbols_with_no_data=[],
        binding_budget_days=0,
        entry_days=10,
    )
    assert "0 of 10" in caveats
    assert "alphabetical" in caveats.lower()
    assert "ticker spelling" in caveats.lower()
    assert "cannot evaluate live rationing" in caveats
    assert "the portfolio risk budget, cluster caps" not in caveats

    caveats_bound = format_caveats(
        slippage_bps=5.0,
        slippage_source="test",
        skipped=0,
        min_bars=210,
        symbols_with_no_data=[],
        binding_budget_days=3,
        entry_days=40,
    )
    assert "3 of 40" in caveats_bound

    metrics = compute_metrics([], 100_000.0)
    report = format_metrics_report(
        "A",
        metrics,
        dict(start="2025-01-01", end="2025-12-31", n_symbols=2, data_source="test", initial_equity=100_000.0),
        binding_budget_days=0,
        entry_days=10,
    )
    assert "0 of 10" in report
    assert "ticker spelling" in report
    assert "not a ranking" in report

    table = format_ab_table(
        "A",
        metrics,
        "B",
        metrics,
        binding_budget_days_a=1,
        binding_budget_days_b=3,
        entry_days_a=10,
        entry_days_b=10,
    )
    assert "Binding-budget days" in table
    assert "1 of 10" in table
    assert "3 of 10" in table
    assert "alphabetical" in table.lower()


def test_backtest_still_sends_uniform_unranked_requests():
    """The caveat's premise: every candidate asks the same risk, and this
    engine does not pass a ranking into the allocator. Production still
    spends down verdict order; that path is not this one.
    """
    import inspect

    from src.backtest import engine
    from src.portfolio_constructor import PortfolioConstructor

    src = inspect.getsource(engine.run_backtest)
    assert 'RiskRequest(c["symbol"], config.risk.max_position_risk_pct)' in src
    assert "priority=" not in src
    assert "rank_verdicts" not in src

    production = inspect.getsource(PortfolioConstructor._plan_risk_targets)
    assert "priority=ranking" in production
