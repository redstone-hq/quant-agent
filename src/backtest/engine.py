"""Deterministic-layer backtest engine.

`src/replay.py` replays past LLM decisions; it is not a strategy backtest
(docs/QAMC_REMEDIATION_SPEC.md §7.1). Without one, every change to stop
placement, sizing, or the risk budget is a guess evaluated against one noisy
live day, and a bad change is indistinguishable from a bad week.

SCOPE — read this before reading a number out of this module
-------------------------------------------------------------
This engine does NOT replay the LLM agents. Their outputs are not
reproducible (same prompt, different day, different answer), so there is no
honest way to "backtest" a Portfolio Manager or Tech Analyst call. What CAN
be measured, and what nearly every recent engineering change has actually
touched, is the DETERMINISTIC layer underneath them:

  * entry timing        — `RiskGate._has_actionable_signal_fn`
  * structural stops     — `src/data/levels.py::find_structural_levels`
  * stop discipline      — `PortfolioConstructor._resolve_stop` /
                            `._widen_stop_past_noise` (src/portfolio_constructor.py)
  * position sizing       — the §2.1 risk formula
                            (shares = equity x risk_pct/100 / |entry - stop|)
  * the portfolio risk budget and cluster caps
                          — `src/risk/budget.py::allocate_risk_budget`
                            (the function runs; on a binding day this
                            engine's equal-size unranked asks are served
                            alphabetically, not production's ranking —
                            see OTHER DECLARED SIMPLIFICATIONS)
  * trailing stops        — `src/risk/trailing.py::compute_trailing_stop`

Every one of those is reused from the live modules, not reimplemented, so a
change in this tool's output is attributable to the deterministic change
under test and nothing else. The one piece with no live counterpart to call
is "which structural level would the Tech Analyst have picked as the stop":
in production an LLM chooses among the levels `find_structural_levels`
reports. This engine substitutes a fixed, deterministic rule — the nearest
support below the NEXT DAY'S OPEN — the price this engine actually fills
at — for a long (nearest resistance above it for a short) becomes the
analyst's `stop_loss`, and the nearest level on the other side of that same
open becomes `reference_target`. ONE reference price for the whole entry
decision: the level partition, the ATR noise band, the reward:risk check
and the sizing all read the same number. No look-ahead is added — the stop
was already resolved against that open before sizing; only the level
partition used to disagree, against the signal-day close. When no level
defends the entry the stop is no longer refused: `None` goes to the real
`_widen_stop_past_noise`, which reads the stop from the instrument (the
wider of the ATR noise band and the signal bar's far edge, `signal_bar_low`
/ `signal_bar_high` set here from the SIGNAL day's bar), exactly as live.
Whatever comes out is fed through the REAL `_resolve_stop` /
`_widen_stop_past_noise`, exactly as the live constructor would if the
analyst had reported those numbers. `setup_type`
("range" vs "breakout") is likewise substituted deterministically from
`MarketContext.is_consolidating` (src/data/context.py) rather than an LLM's
chart read. Both substitutions are declared here, not hidden in a helper.

NO-LOOK-AHEAD
-------------
A signal computed from bars through day D is only ever acted on at day D+1's
OPEN — never at day D's own close, and never using anything dated after D+1's
open. Stops and targets are resolved from information available at the close
of day D. Indicators, structural levels and trailing-stop updates for day D
use bars [.., D] inclusive, never a bar past D.

DIRECTION
---------
The engine is direction-aware end to end (long and short fills, stops,
exits, signed P&L) so a later change that lands short trading does not need
this rewritten. The historical run in this repo's current state only ever
emits LONG signals, because the live system cannot open a short yet and the
deterministic prefilter/structural-levels path this engine reuses has no
short-side entry rule to borrow (mirroring one here, with nothing live to
validate it against, is not "reuse" — it's a new invention this backtester
declined to make). The short side of the engine is exercised directly by
`tests/test_backtest.py` with synthetic trades, not by the real-data run.

Also note: `PortfolioConstructor._widen_stop_past_noise` is long-only math
(it computes `entry_price - multiple * atr`, a floor BELOW entry). It is
reused as-is for longs. For shorts, this engine applies the raw structural
stop without a noise-band widening step — production has no short-side
widening rule yet to reuse, and this tool does not invent one.

OTHER DECLARED SIMPLIFICATIONS
-------------------------------
* One open position per symbol at a time (no pyramiding).
* Correlation clusters are recomputed once per simulated day, from bars
  through that day only (no look-ahead), using the same
  `build_correlation_matrix` / `correlation_clusters` the live risk budget
  uses.
* Equity used for sizing and for the risk budget compounds with REALIZED
  trade P&L only; unrealized marks on open positions are not folded in
  (matches the trade-level max-drawdown methodology in `metrics.py`).
* macro regime is not modelled — `_widen_stop_past_noise` is called with
  `regime=None`, i.e. no regime scale is applied.
* There is NO take-profit exit: live never sells at a target, so a position
  leaves only by stop (trailed), the hold horizon, or end of data. A bar
  that opens through the stop fills at the open, not the stop.
* A position still open when the data window ends is force-closed at the
  last available close (`exit_reason="end_of_data"`), not silently dropped.
* Every new candidate requests the same `max_position_risk_pct` and this
  engine supplies no ranking (`priority` is omitted). When the budget
  binds, equal-size requests are served by the allocator's alphabetical
  ticker tie-break — ticker spelling, not a quality ranking. Production
  spends down `rank_verdicts` order; this engine has no verdicts and does
  not invent a score. Every run reports how many entry days bound; those
  days cannot evaluate live rationing. The share is not a discount you
  can apply to the other numbers: who got funded changes later equity,
  later size, and later outcomes.

  Every run ALSO reports `contested_budget_days`: the strictly narrower
  count of days on which two or more new candidates competed AND at
  least one was cut. Those are the days the alphabetical tie-break
  actually arbitrated between names. A day with a single candidate
  trimmed by already-held risk binds the budget but decides nothing by
  spelling, and production would have trimmed it the same way, so it is
  not contested. A run with ZERO contested days has no alphabetical
  arbitration anywhere in it and is therefore settleable; one with any
  contested day is reported as a NON-RESULT by the tool itself rather
  than left for a reader to disqualify. Zero is the line because this
  engine compounds equity off realized P&L: one funding call decided by
  spelling moves every later size and every later outcome, so there is
  no share of contested days that can be treated as noise around an
  otherwise sound number.

NOT SUBSTITUTED — why the asks stay equal
------------------------------------------
Production's `requested_pct` is `combined_override.value`
(src/portfolio_constructor/__init__.py), a conviction multiplier built
from the analyst and PM verdicts. There is no deterministic quantity in
this engine's reach that reproduces it. Making the asks differ per
candidate here would mean inventing a quality score, which is the one
thing this tool refuses to do, so the asks stay equal and the
consequence is DECLARED and COUNTED instead of disguised.
"""

from __future__ import annotations

import functools
from dataclasses import dataclass, field
from datetime import date
from types import SimpleNamespace

from src.backtest.swept_values import SweepMeter
from src.backtest.budget_days import _budget_binds, _tie_break_arbitrated  # noqa: F401
from src.backtest.exit_rules import (  # noqa: F401
    _check_exit,
    _existing_risk_pct,
    _size_position,
)
from src.config import AppConfig
from src.data.correlation import build_correlation_matrix, correlation_clusters
from src.data.technical import compute_indicators
from src.backtest.structural_stops import (  # noqa: F401
    _resolve_structural_stop_and_target,
    _setup_type_for,
)
from src.backtest.records import Trade, _OpenPosition, _close_trade, _fill_price  # noqa: F401
from src.models import OHLCV
from src.pipeline_risk_gate import RiskGate
from src.portfolio_constructor import ConstructorConfig, PortfolioConstructor
from src.risk.budget import RiskRequest, allocate_risk_budget
from src.risk.trailing import compute_trailing_stop

#: Trading days of history a symbol needs before this engine will evaluate it
#: for a signal. 210 = 200 (MA200) + 10 (the slope lookback `compute_market_context`
#: needs to say whether that average is rising or falling) — below this, both
#: `find_structural_levels` and `compute_market_context` are working with a
#: materially incomplete picture, and the live system would be too.
MIN_BARS_FOR_SIGNAL = 210

DEFAULT_MAX_HOLD_DAYS = 20
DEFAULT_INITIAL_EQUITY = 100_000.0
DEFAULT_SLIPPAGE_BPS = 5.0


@dataclass(frozen=True)
class BacktestParams:
    """Engine-only knobs. None of these has a live-system counterpart to
    reuse: the live horizon comes from the Tech Analyst's own
    `expected_horizon_sessions` estimate (an LLM output this engine cannot
    reproduce), and there is no dedicated backtest slippage field in
    `Settings` — see `scripts/backtest.py` for how the default is chosen."""

    start: date
    end: date
    max_hold_days: int = DEFAULT_MAX_HOLD_DAYS
    initial_equity: float = DEFAULT_INITIAL_EQUITY
    slippage_bps: float = DEFAULT_SLIPPAGE_BPS
    min_bars_for_signal: int = MIN_BARS_FOR_SIGNAL
    #: This run's swept-value overrides and read counts. Built with the
    #: params, so it is born and discarded with the run it describes.
    meter: SweepMeter = field(default_factory=SweepMeter, compare=False, repr=False)


@dataclass(frozen=True)
class BacktestRunResult:
    trades: list[Trade]
    skipped_symbol_days: int
    symbols_used: list[str]
    symbols_with_no_data: list[str]
    params: BacktestParams
    initial_equity: float
    final_equity: float
    #: Days `allocate_risk_budget` ran (the engine had at least one new
    #: candidate). Denominator for `binding_budget_days`.
    entry_days: int
    #: Days at least one new candidate was not granted its full request.
    #: This engine asks the same risk for every name and supplies no
    #: ranking, so among equal-size requests the allocator's alphabetical
    #: ticker tie-break is the order they are served. That is not a
    #: ranking, and it is not production's `rank_verdicts` spend-down.
    binding_budget_days: int
    #: Days on which TWO OR MORE new candidates competed and at least one
    #: was cut — the days the alphabetical tie-break actually arbitrated
    #: between names. Strictly a subset of `binding_budget_days`. Any
    #: value above zero makes the run a NON-RESULT for settling a
    #: parameter: see the module docstring for why zero is the line.
    contested_budget_days: int

    @property
    def is_settleable(self) -> bool:
        """Whether a parameter conclusion may be read off this run at
        all. False whenever ticker spelling arbitrated any funding
        decision, because compounding carries that choice into every
        later size and outcome."""
        return self.contested_budget_days == 0


def _resolve_stop_for_signal(
    constructor: PortfolioConstructor,
    *,
    symbol: str,
    direction: str,
    structural_stop: float | None,
    target: float | None,
    atr_14: float | None,
    setup_type: str,
    ref_entry: float,
    signal_bar_low: float | None = None,
    signal_bar_high: float | None = None,
    computed_levels: list[float] | None = None,
    computed_level_touches: dict[float, int] | None = None,
    computed_level_bars: dict[float, list[tuple[float, float]]] | None = None,
) -> float | None:
    """Reuses `PortfolioConstructor._resolve_stop` (direction-agnostic — it
    only reads whichever of `target.suggested_stop_price` /
    `analysis.stop_loss` is supplied) and, for longs only,
    `_widen_stop_past_noise` (long-only math — see module docstring).

    `computed_levels` carries the levels §12.1's stop rule verifies against,
    the same field `TechAnalystAgent` sets in Python on the live path. This
    engine's stop candidate IS one of them, so without this the backtest
    would exercise the pre-§12.1 rule while claiming to run the real one.
    `computed_level_touches` is the touch count behind each of those prices,
    carried as data only: since 2026-10-14 no touch count gates whether a
    real level backs a stop, live or here (a 148-stock study found a stop
    just beyond a real level is hit less often than one with no level at
    the same distance, and extra touches add nothing). `computed_level_bars` is the pivot-bar
    ranges behind those same prices — `_level_backing_stop` fails closed
    without them, so omitting it would make this engine refuse every
    level-backed stop that live honours.

    `setup_type` (already on the shim, and already required by the stop
    scaling in `_stop_atr_multiple`) is what carries docs/WORK.md item
    1(d) into the backtest for free, 2026-09-11: `_widen_stop_past_noise`
    reads it off this same shim, so a backtested breakout skips the
    reward:risk floor and a backtested range trade is no longer refused by
    it — the same rule live trading runs, from the same function, with no
    second copy. **One real caveat, and it is a parity gap that predates
    this change:** live, `setup_type` is the Technical Analyst's own chart
    read; here it is `_setup_type_for`'s deterministic
    `is_consolidating` proxy (see that function and the module docstring).
    The engine has no ranking stage at all — no PM, no verdicts — so the
    other half of item 1(d), feeding the real ratio into the weighted
    ranking, has no analogue to apply here."""
    analysis = SimpleNamespace(
        stop_loss=structural_stop,
        atr_14=atr_14,
        setup_type=setup_type,
        reference_target=target,
        computed_levels=list(computed_levels or []),
        computed_level_touches=dict(computed_level_touches or {}),
        computed_level_bars=dict(computed_level_bars or {}),
        signal_bar_low=signal_bar_low,
        signal_bar_high=signal_bar_high,
    )
    # `symbol` is read by `_resolve_stop`'s no-typed-stop log line, which
    # only runs now that a stopless signal is passed through instead of
    # being dropped (GAP 2).
    target_shim = SimpleNamespace(suggested_stop_price=None, symbol=symbol)
    stop = constructor._resolve_stop(target_shim, analysis, ref_entry)
    if direction == "long":
        stop = constructor._widen_stop_past_noise(
            symbol,
            analysis,
            ref_entry,
            stop,
            regime=None,
        )
        if stop is None or stop <= 0 or stop >= ref_entry:
            return None
    else:
        if stop is None or stop <= 0 or stop <= ref_entry:
            return None
    return stop


def _with_run_meter(simulate):
    """Install this run's read counters for the duration of the run and
    remove them afterwards, so no count or wrapper outlives the run."""

    @functools.wraps(simulate)
    def wrapper(*, config, bars_by_symbol, params):
        with params.meter.counting():
            return simulate(config=config, bars_by_symbol=bars_by_symbol, params=params)

    return wrapper


@_with_run_meter
def run_backtest(
    *,
    config: AppConfig,
    bars_by_symbol: dict[str, list[OHLCV]],
    params: BacktestParams,
) -> BacktestRunResult:
    """Run the day-by-day simulation. `bars_by_symbol` must already be
    fetched (see `src/backtest/data.py`) — this function makes no network
    calls, which is what makes it unit-testable offline."""
    constructor = PortfolioConstructor(
        ConstructorConfig(
            # Mirrors src/pipeline.py's ConstructorConfig wiring exactly, so a
            # change to `config.risk.*` is the same experiment here as live.
            risk_budget_pct=config.risk.max_position_risk_pct,
            min_risk_pct=config.risk.min_position_risk_pct,
            max_portfolio_risk_pct=config.risk.max_portfolio_risk_pct,
            max_cluster_risk_share_pct=config.risk.max_cluster_risk_share_pct,
            max_position_pct=config.risk.max_position_pct,
            min_stop_atr_multiple=config.risk.min_stop_atr_multiple,
            # Spec §12.1 — a stop at a COMPUTED level is honoured whatever the
            # band says, down to a deterministic 1x ATR floor. Wired here so a
            # change to `config.risk.*` is the same experiment in the backtest
            # as it is live.
            # No `level_match_atr_tolerance` to wire: deleted 2026-09-13
            # (docs/WORK.md item 46). The match tolerance is the level zone's
            # own width, read from `src.data.levels.CLUSTER_TOLERANCE_PCT`, so
            # live and backtest get it from the same place by construction.
            absolute_min_stop_atr_multiple=config.risk.absolute_min_stop_atr_multiple,
            # Target-derivation tunables (2026-09-01). Wired for parity with
            # live, though this engine does not reach `_derive_target`: it
            # computes its own nearest-level target in
            # `_resolve_structural_stop_and_target` and hands it to
            # `_widen_stop_past_noise` directly, which is the same rule by a
            # shorter path and was never exposed to the guessed-target defect.
            min_target_atr_multiple=config.risk.min_target_atr_multiple,
            breakout_projection_atr_multiple=config.risk.breakout_projection_atr_multiple,
            max_target_reach_atr_multiple=config.risk.max_target_reach_atr_multiple,
            max_target_horizon_sessions=config.risk.max_target_horizon_sessions,
        )
    )

    symbols_with_data = sorted(sym for sym, bars in bars_by_symbol.items() if bars)
    symbols_with_no_data = sorted(sym for sym, bars in bars_by_symbol.items() if not bars)

    bars_sorted: dict[str, list[OHLCV]] = {
        sym: sorted(bars_by_symbol[sym], key=lambda b: b.date) for sym in symbols_with_data
    }
    index_of_date: dict[str, dict[date, int]] = {
        sym: {b.date: i for i, b in enumerate(bars_sorted[sym])} for sym in symbols_with_data
    }

    calendar = sorted({b.date for bars in bars_sorted.values() for b in bars if params.start <= b.date <= params.end})

    trades: list[Trade] = []
    open_positions: dict[str, _OpenPosition] = {}
    realized_pnl = 0.0
    skipped_symbol_days = 0
    entry_days = 0
    binding_budget_days = 0
    contested_budget_days = 0

    for i, day in enumerate(calendar):
        # ---- 1. Exits, then trailing-stop updates, for open positions ----
        for symbol in list(open_positions):
            pos = open_positions[symbol]
            idx_map = index_of_date[symbol]
            if day not in idx_map:
                continue
            idx = idx_map[day]
            bar = bars_sorted[symbol][idx]

            exit_reason, raw_exit = _check_exit(pos, bar, idx, params.max_hold_days)

            if exit_reason is not None:
                trade = _close_trade(pos, idx, day, raw_exit, exit_reason, params.slippage_bps)
                trades.append(trade)
                realized_pnl += trade.pnl
                del open_positions[symbol]
                continue

            # Still open: propose a trail using bars SINCE ENTRY, through today.
            bars_since_entry = bars_sorted[symbol][pos.entry_index : idx + 1]
            atr_today = compute_indicators(symbol, bars_sorted[symbol][: idx + 1]).atr_14
            qty_sign = pos.shares if pos.direction == "long" else -pos.shares
            proposal = compute_trailing_stop(
                symbol=symbol,
                setup_type=pos.setup_type,
                entry=pos.entry_price,
                current_price=bar.close,
                current_stop=pos.stop,
                reference_target=pos.target,
                bars=bars_since_entry,
                atr=atr_today,
                qty=qty_sign,
                # 1R per share from the SAME per-lot function the live trail
                # uses, over this position's one lot and its ENTRY stop (frozen
                # at fill — never the live `pos.stop` a prior trail moved).
                # Powers the +1R breakeven and +2R -> +1R ratchets (item 142).
                risk_per_share=_risk_per_share(pos, qty_sign),
                # The simulated fill day (next open after the signal): the
                # chandelier reads only completed bars dated after it, the
                # structure leg bars on or after it — the live rule.
                entry_date=pos.entry_date,
            )
            # CAVEAT (item 142): passing the frozen entry stop here means BOTH
            # R-ratchets now fire in backtest whereas NONE fired before this
            # change — so pre-this-change backtest numbers are NOT comparable to
            # post-change ones. And this backtest is optimistically one-sided:
            # a stop freshly locked to +1R this bar cannot stop out on the SAME
            # bar it is set, so the second ratchet will look LESS scratchy here
            # than it will live. Do NOT trust backtest scratch-rates to tune the
            # R multiples (`RANGE_SECOND_RATCHET_TRIGGER_R` / `_LOCK_R`).
            if proposal is not None:
                pos.stop = proposal.new_stop

        # ---- 2. New entries, filled at the NEXT day's open ----
        if i + 1 >= len(calendar):
            continue  # no next-day open available; the window has ended
        next_day = calendar[i + 1]
        equity = params.initial_equity + realized_pnl

        candidates: list[dict] = []
        for symbol in symbols_with_data:
            if symbol in open_positions:
                continue
            idx_map = index_of_date[symbol]
            if day not in idx_map or next_day not in idx_map:
                continue
            idx = idx_map[day]
            bars_through_today = bars_sorted[symbol][: idx + 1]
            if len(bars_through_today) < params.min_bars_for_signal:
                skipped_symbol_days += 1
                continue

            indicators = compute_indicators(symbol, bars_through_today)
            if not RiskGate._has_actionable_signal_fn(
                indicators,
                symbol,
                bars_through_today,
                [],
            ):
                continue

            direction = "long"  # see module docstring: real-data run is long-only
            setup_type = _setup_type_for(bars_through_today)
            next_idx = idx_map[next_day]
            ref_entry = bars_sorted[symbol][next_idx].open
            if not ref_entry or ref_entry <= 0:
                continue
            # Levels from bars through the signal day only; partitioned
            # against the price actually paid at the next open, which is
            # the same reference every other part of this decision already
            # used. See `_resolve_structural_stop_and_target`.
            (
                structural_stop,
                target,
                computed_levels,
                computed_level_touches,
                computed_level_bars,
            ) = _resolve_structural_stop_and_target(
                bars_through_today,
                direction,
                ref_entry,
                meter=params.meter,
            )
            signal_bar = bars_through_today[-1]
            stop = _resolve_stop_for_signal(
                constructor,
                symbol=symbol,
                direction=direction,
                structural_stop=structural_stop,
                target=target,
                atr_14=indicators.atr_14,
                setup_type=setup_type,
                ref_entry=ref_entry,
                signal_bar_low=signal_bar.low,
                signal_bar_high=signal_bar.high,
                computed_levels=computed_levels,
                computed_level_touches=computed_level_touches,
                computed_level_bars=computed_level_bars,
            )
            if stop is None:
                continue
            candidates.append(
                dict(
                    symbol=symbol,
                    direction=direction,
                    ref_entry=ref_entry,
                    stop=stop,
                    target=target,
                    setup_type=setup_type,
                    next_idx=next_idx,
                    signal_date=day,
                )
            )

        if not candidates:
            continue

        hist_for_matrix = {
            sym: bars_sorted[sym][: index_of_date[sym][day] + 1]
            for sym in symbols_with_data
            if day in index_of_date[sym]
        }
        matrix = build_correlation_matrix(hist_for_matrix)
        cluster_universe = list(open_positions) + [c["symbol"] for c in candidates]
        clusters = correlation_clusters(cluster_universe, matrix)

        existing_pct = {sym: _existing_risk_pct(pos, equity) for sym, pos in open_positions.items()}
        requests = [RiskRequest(c["symbol"], config.risk.max_position_risk_pct) for c in candidates]
        allocation = allocate_risk_budget(
            requests,
            existing_pct=existing_pct,
            clusters=clusters,
            ceiling_pct=config.risk.max_portfolio_risk_pct,
            cluster_share_pct=config.risk.max_cluster_risk_share_pct,
            floor_pct=config.risk.min_position_risk_pct,
        )
        entry_days += 1
        if _budget_binds(allocation):
            binding_budget_days += 1
        if _tie_break_arbitrated(allocation, len(requests)):
            contested_budget_days += 1

        for c in candidates:
            granted = allocation.granted(c["symbol"])
            if granted <= 0:
                continue
            fill_entry = _fill_price(c["ref_entry"], c["direction"], "open", params.slippage_bps)
            shares, eff_risk_pct = _size_position(
                equity=equity,
                granted_risk_pct=granted,
                fill_entry=fill_entry,
                stop=c["stop"],
                symbol=c["symbol"],
                max_position_pct=config.risk.max_position_pct,
            )
            if shares <= 0:
                continue
            open_positions[c["symbol"]] = _OpenPosition(
                symbol=c["symbol"],
                direction=c["direction"],
                signal_date=c["signal_date"],
                entry_date=next_day,
                entry_index=c["next_idx"],
                entry_price=fill_entry,
                stop_initial=c["stop"],
                stop=c["stop"],
                target=c["target"],
                setup_type=c["setup_type"],
                shares=shares,
                risk_pct=eff_risk_pct,
            )

    # ---- Force-close anything still open when the data window ends ----
    for symbol, pos in list(open_positions.items()):
        idx_map = index_of_date[symbol]
        last_idx = None
        last_day = None
        for day in reversed(calendar):
            if day in idx_map:
                last_idx, last_day = idx_map[day], day
                break
        if last_idx is None or last_idx <= pos.entry_index:
            continue  # no bar since entry to mark it against
        bar = bars_sorted[symbol][last_idx]
        trade = _close_trade(pos, last_idx, last_day, bar.close, "end_of_data", params.slippage_bps)
        trades.append(trade)
        realized_pnl += trade.pnl

    return BacktestRunResult(
        trades=trades,
        skipped_symbol_days=skipped_symbol_days,
        symbols_used=symbols_with_data,
        symbols_with_no_data=symbols_with_no_data,
        params=params,
        initial_equity=params.initial_equity,
        final_equity=round(params.initial_equity + realized_pnl, 2),
        entry_days=entry_days,
        binding_budget_days=binding_budget_days,
        contested_budget_days=contested_budget_days,
    )


def _risk_per_share(pos, held_qty: float) -> float | None:
    """1R per share via `quantities.held_position_risk` over the one backtest lot."""
    from src.quantities import held_position_risk

    lot = {
        "action": "BUY" if pos.direction == "long" else "SHORT",
        "qty": pos.shares,
        "price": pos.entry_price,
        "stop_loss": pos.stop_initial,
        # The simulated fill time, in the field live fills carry the broker's.
        "filled_at": str(entry) if (entry := getattr(pos, "entry_date", None)) is not None else None,
    }
    risk, _ = held_position_risk([lot], held_qty)
    return risk / abs(held_qty) if risk and held_qty else None
