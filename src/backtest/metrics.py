"""Trade-level metrics, reporting, and the per-trade CSV writer.

Kept separate from `engine.py` on purpose: `compute_metrics` is a pure
function of a `list[Trade]`, so it can be — and is, in
`tests/test_backtest.py` — verified against a hand-built set of trades with
literal expected numbers, independent of the day-by-day simulation that
produced them.

METHODOLOGY NOTE — max drawdown
--------------------------------
Drawdown is computed on the CLOSED-TRADE equity curve: start at
`initial_equity`, walk trades in exit-date order, add each trade's realized
P&L, and track the running peak-to-trough decline. This is NOT a daily
mark-to-market curve — unrealized swings in open positions between entry and
exit are not represented. That is a simplification of a full portfolio
simulator, declared here and repeated in the tool's printed caveats.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, fields
from pathlib import Path

from src.backtest.engine import Trade


@dataclass(frozen=True)
class Metrics:
    trade_count: int
    win_rate_pct: float
    avg_win: float
    avg_loss: float
    avg_win_loss_ratio: float | None
    expectancy_dollars: float
    expectancy_r: float
    avg_hold_days: float
    max_drawdown_pct: float
    max_drawdown_dollars: float
    total_return_pct: float
    final_equity: float


def compute_metrics(trades: list[Trade], initial_equity: float) -> Metrics:
    """Deterministic given `trades` and `initial_equity`: trades are sorted
    by (exit_date, symbol, entry_date) before any running computation, so
    the result never depends on the order the caller happened to list them
    in (and is therefore safe to call twice and diff byte-for-byte)."""
    if not trades:
        return Metrics(
            trade_count=0,
            win_rate_pct=0.0,
            avg_win=0.0,
            avg_loss=0.0,
            avg_win_loss_ratio=None,
            expectancy_dollars=0.0,
            expectancy_r=0.0,
            avg_hold_days=0.0,
            max_drawdown_pct=0.0,
            max_drawdown_dollars=0.0,
            total_return_pct=0.0,
            final_equity=round(initial_equity, 2),
        )

    ordered = sorted(trades, key=lambda t: (t.exit_date, t.symbol, t.entry_date))

    wins = [t for t in ordered if t.pnl > 0]
    losses = [t for t in ordered if t.pnl < 0]

    win_rate = len(wins) / len(ordered) * 100.0
    avg_win = (sum(t.pnl for t in wins) / len(wins)) if wins else 0.0
    avg_loss = (sum(t.pnl for t in losses) / len(losses)) if losses else 0.0
    ratio = (avg_win / abs(avg_loss)) if avg_loss != 0 else None
    expectancy_dollars = sum(t.pnl for t in ordered) / len(ordered)
    expectancy_r = sum(t.r_multiple for t in ordered) / len(ordered)
    avg_hold = sum(t.hold_days for t in ordered) / len(ordered)

    equity = initial_equity
    peak = initial_equity
    max_dd_pct = 0.0
    max_dd_dollars = 0.0
    for t in ordered:
        equity += t.pnl
        if equity > peak:
            peak = equity
        dd_dollars = peak - equity
        dd_pct = (dd_dollars / peak * 100.0) if peak > 0 else 0.0
        max_dd_dollars = max(max_dd_dollars, dd_dollars)
        max_dd_pct = max(max_dd_pct, dd_pct)

    total_return_pct = (equity - initial_equity) / initial_equity * 100.0 if initial_equity else 0.0

    return Metrics(
        trade_count=len(ordered),
        win_rate_pct=round(win_rate, 2),
        avg_win=round(avg_win, 2),
        avg_loss=round(avg_loss, 2),
        avg_win_loss_ratio=round(ratio, 3) if ratio is not None else None,
        expectancy_dollars=round(expectancy_dollars, 2),
        expectancy_r=round(expectancy_r, 4),
        avg_hold_days=round(avg_hold, 2),
        max_drawdown_pct=round(max_dd_pct, 2),
        max_drawdown_dollars=round(max_dd_dollars, 2),
        total_return_pct=round(total_return_pct, 2),
        final_equity=round(equity, 2),
    )


def write_trades_csv(trades: list[Trade], path: str) -> None:
    """One row per closed trade, in the exact field order of `Trade`."""
    cols = [f.name for f in fields(Trade)]
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(cols)
        for t in sorted(trades, key=lambda t: (t.exit_date, t.symbol, t.entry_date)):
            writer.writerow([getattr(t, c) for c in cols])


CAVEAT_TEMPLATE = """\
WHAT THIS MEASURES — read before trusting the numbers above
  - DETERMINISTIC LAYER ONLY. Entries come from the system's own deterministic
    prefilter (TradingPipeline._has_actionable_signal_fn) and structural
    levels (src/data/levels.py) — never from the LLM agents. The agents are
    NOT replayed; their outputs are not reproducible and cannot be honestly
    backtested (docs/QAMC_REMEDIATION_SPEC.md Sec 7.1). These numbers measure
    stop placement, noise-band widening, risk-based sizing, and the
    trailing-stop rules -- nothing else. They do NOT measure how the live
    desk rations a binding risk budget. This engine asks the same risk for
    every candidate and supplies no ranking, so when the budget binds,
    equal-size requests are served by the allocator's alphabetical ticker
    tie-break -- ticker spelling, not a quality ranking. Production spends
    down analyst-verdict order; this engine has no verdicts and does not
    invent a score. Binding-budget days this run: {binding} of {entry}
    entry day(s) (days that had at least one new candidate). That share is
    not a discount you can apply to the numbers above: who got funded
    changes later equity, later size, and later outcomes. These numbers
    therefore cannot evaluate live rationing.
    They are NOT a forecast of live P&L, which also depends on the
    LLM-driven entry/exit judgment this tool does not model.
  - SURVIVORSHIP BIAS. The universe is a fixed, present-day symbol list. Any
    name that was delisted, acquired, or dropped from the universe during
    the test window is simply absent from this run, which biases the result
    upward.
  - SLIPPAGE: {slippage_bps:.1f} bps applied on both entry and exit fills
    ({slippage_source}). No commission or fee model beyond this is applied.
  - DATA COVERAGE: {skipped} symbol-day(s) were skipped for insufficient
    price history (fewer than {min_bars} trading days of history available)
    rather than silently dropped.{missing_line}
  - EXITS: there is no target exit (live never sells at a target); positions
    leave by stop or trailed stop, the hold horizon, or end of data. A bar
    opening through the stop fills at the open.
  - Max drawdown is computed on the closed-trade equity curve (see this
    module's docstring), not a daily mark-to-market curve."""


def format_settleability_verdict(
    *,
    contested_budget_days: int,
    binding_budget_days: int,
    entry_days: int,
    label: str = "this run",
) -> str:
    """The tool's own verdict on whether a parameter conclusion may be
    read off a run, printed BEFORE the numbers rather than left for a
    reader to work out from the caveats.

    A contested day is one where two or more new candidates competed and
    at least one was cut, so the engine's alphabetical ticker tie-break
    chose between names. Any such day makes the run a NON-RESULT: the
    engine compounds equity off realized P&L, so a single funding call
    decided by spelling moves every later size and every later outcome,
    and there is no share small enough to treat as noise.
    """
    share = f"{100.0 * contested_budget_days / entry_days:.1f}%" if entry_days > 0 else "n/a"
    head = (
        f"Budget binding: {binding_budget_days} of {entry_days} entry day(s); "
        f"of those, {contested_budget_days} were CONTESTED "
        f"(2+ candidates competing, at least one cut) = {share} of entry days."
    )
    if contested_budget_days == 0:
        return (
            f"{head}\n"
            f"SETTLEABLE — no funding decision in {label} was arbitrated by "
            f"ticker spelling, so the numbers below are attributable to the "
            f"parameter under test."
        )
    return (
        f"{head}\n"
        f"*** NON-RESULT — do not settle any parameter from {label}. ***\n"
        f"On {contested_budget_days} day(s) this engine's equal-size, "
        f"unranked asks were ordered ALPHABETICALLY by ticker, so spelling "
        f"chose who was funded. Equity compounds off those fills, so every "
        f"later size and outcome inherits the choice. Shrink the universe "
        f"until this count reaches zero, or widen the portfolio risk ceiling "
        f"so the budget stops binding, and re-run before reading a conclusion."
    )


def format_caveats(
    *,
    slippage_bps: float,
    slippage_source: str,
    skipped: int,
    min_bars: int,
    symbols_with_no_data: list[str],
    binding_budget_days: int,
    entry_days: int,
) -> str:
    missing_line = ""
    if symbols_with_no_data:
        missing_line = (
            "\n  - NO DATA AT ALL for: "
            + ", ".join(symbols_with_no_data)
            + " -- excluded from the universe entirely, not counted above."
        )
    return CAVEAT_TEMPLATE.format(
        slippage_bps=slippage_bps,
        slippage_source=slippage_source,
        skipped=skipped,
        min_bars=min_bars,
        missing_line=missing_line,
        binding=binding_budget_days,
        entry=entry_days,
    )


def format_metrics_report(
    label: str,
    metrics: Metrics,
    meta: dict,
    *,
    binding_budget_days: int,
    entry_days: int,
    contested_budget_days: int = 0,
) -> str:
    ratio = f"{metrics.avg_win_loss_ratio:.3f}" if metrics.avg_win_loss_ratio is not None else "n/a"
    lines = [
        f"Backtest: {label}",
        f"  Period: {meta['start']} .. {meta['end']}  |  "
        f"Universe: {meta['n_symbols']} symbol(s)  |  Data source: {meta['data_source']}",
        f"  Trades: {metrics.trade_count}",
        f"  Binding-budget days: {binding_budget_days} of {entry_days} entry day(s)"
        f"  (equal asks served alphabetically — ticker spelling, not a ranking)",
        f"  Contested days: {contested_budget_days}"
        f"  ({'NON-RESULT — spelling decided a funding call' if contested_budget_days else 'none — settleable'})",
        f"  Win rate: {metrics.win_rate_pct:.2f}%",
        f"  Avg win: ${metrics.avg_win:,.2f}   Avg loss: ${metrics.avg_loss:,.2f}   Win/loss ratio: {ratio}",
        f"  Expectancy: ${metrics.expectancy_dollars:,.2f}/trade ({metrics.expectancy_r:+.3f}R)",
        f"  Avg hold: {metrics.avg_hold_days:.1f} session(s)",
        f"  Max drawdown: {metrics.max_drawdown_pct:.2f}% (${metrics.max_drawdown_dollars:,.2f})",
        f"  Total return: {metrics.total_return_pct:+.2f}%   "
        f"Final equity: ${metrics.final_equity:,.2f} "
        f"(from ${meta['initial_equity']:,.2f})",
    ]
    return "\n".join(lines)


_AB_ROWS: tuple[tuple[str, str, str], ...] = (
    ("trade_count", "Trades", "int"),
    ("win_rate_pct", "Win rate %", "pct"),
    ("avg_win", "Avg win $", "usd"),
    ("avg_loss", "Avg loss $", "usd"),
    ("avg_win_loss_ratio", "Win/loss ratio", "ratio"),
    ("expectancy_dollars", "Expectancy $/trade", "usd"),
    ("expectancy_r", "Expectancy R", "r"),
    ("avg_hold_days", "Avg hold (sessions)", "num"),
    ("max_drawdown_pct", "Max drawdown %", "pct"),
    ("total_return_pct", "Total return %", "pct"),
    ("final_equity", "Final equity $", "usd"),
)


def _fmt(value, kind: str, signed: bool = False) -> str:
    if value is None:
        return "n/a"
    sign = "+" if signed else ""
    if kind == "int":
        return f"{value:{sign}d}" if signed else f"{value:d}"
    if kind in ("pct",):
        return f"{value:{sign}.2f}%"
    if kind == "usd":
        return f"${value:{sign},.2f}"
    if kind == "r":
        return f"{value:{sign}.4f}R"
    if kind == "ratio":
        return f"{value:{sign}.3f}"
    return f"{value:{sign}.2f}"


def format_ab_table(
    label_a: str,
    metrics_a: Metrics,
    label_b: str,
    metrics_b: Metrics,
    *,
    binding_budget_days_a: int,
    binding_budget_days_b: int,
    entry_days_a: int,
    entry_days_b: int,
    contested_budget_days_a: int = 0,
    contested_budget_days_b: int = 0,
) -> str:
    """Side-by-side comparison with a delta column (B - A). This is the
    tool's real purpose: "did this parameter change help?" """
    col_a, col_b = label_a[:20], label_b[:20]
    header = f"{'Metric':<22} {col_a:>20} {col_b:>20} {'Delta (B-A)':>16}"
    lines = [header, "-" * len(header)]
    for attr, name, kind in _AB_ROWS:
        a = getattr(metrics_a, attr)
        b = getattr(metrics_b, attr)
        delta = (b - a) if isinstance(a, (int, float)) and isinstance(b, (int, float)) else None
        lines.append(f"{name:<22} {_fmt(a, kind):>20} {_fmt(b, kind):>20} {_fmt(delta, kind, signed=True):>16}")
    # Binding-day count is on the run, not on trade-list Metrics. It still
    # belongs on the comparison: this table is where a reader treats a
    # parameter change as evidence about how the desk picks among trades.
    bind_a = f"{binding_budget_days_a} of {entry_days_a}"
    bind_b = f"{binding_budget_days_b} of {entry_days_b}"
    bind_delta = binding_budget_days_b - binding_budget_days_a
    lines.append(f"{'Binding-budget days':<22} {bind_a:>20} {bind_b:>20} {_fmt(bind_delta, 'int', signed=True):>16}")
    lines.append(
        f"{'Contested days':<22} {contested_budget_days_a:>20} "
        f"{contested_budget_days_b:>20} "
        f"{_fmt(contested_budget_days_b - contested_budget_days_a, 'int', signed=True):>16}"
    )
    lines.append(
        "On binding days this engine serves equal-size requests alphabetically (ticker spelling), not a ranking."
    )
    if contested_budget_days_a or contested_budget_days_b:
        lines.append(
            "*** NON-RESULT — this A/B settles NOTHING. At least one arm had "
            "a day where 2+ candidates competed and alphabetical ticker order "
            "chose who was funded; compounding carries that into every later "
            "size, so the delta column above is not attributable to the "
            "parameter change. Shrink the universe until contested days reach "
            "zero on BOTH arms, then re-run. ***"
        )
    else:
        lines.append("Neither arm had a contested day: the delta column is attributable to the parameter change.")
    return "\n".join(lines)
