"""Trade Review: display-only aggregation of the clean-record closed trades.

DISPLAY AND ANALYSIS ONLY. Nothing here feeds sizing, stops, entries or exits,
and `src/agents` / `src/prompt_facts` may not import it (.importlinter).

Nothing is re-derived that already exists:
  - one closed round trip per position  -> `quantities.summarize_closed_position`
  - R (profit in units of initial risk) -> per lot: `quantities.summarize_closed_position`
    (closed) and `open_position_r` (open)
(They live in src.quantities, the stdlib-only seam src/api may import.)
  - clean-record start                  -> `calibration._opened_before_clean_record`

A trade whose R is undefined (no stop, wrong-side stop) is counted as "R unknown"
and left out of every R statistic. It is never treated as zero. There is no
minimum-trade threshold anywhere: every figure carries its N and the caveat.
"""

from __future__ import annotations

import math
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from src.trading_calendar import utc_now

from src.quantities import (
    held_position_risk,
    live_position_lots,
    recorded_initial_stop,
    split_position_chain,
    summarize_closed_position,
)
from src.storage.analytics.calibration import _opened_before_clean_record
from src.trade_actions import fill_sort_key
from src.util.time import ET

CAVEAT = "small samples and lopsided results make this unreliable"


# Display-only (dashboard grouping); never read by trading code.
# (label, upper bound inclusive): a trade falls in the first bucket whose bound is >= its R.
R_BUCKETS: tuple[tuple[str, float], ...] = (
    ("<= -2R", -2.0),
    ("-2R to -1R", -1.0),
    ("-1R to 0R", 0.0),
    ("0R to 1R", 1.0),
    ("1R to 2R", 2.0),
    ("2R to 3R", 3.0),
    ("> 3R", math.inf),
)

# Display-only (dashboard grouping); never read by trading code. (label, upper bound in seconds, exclusive).
HOLD_BUCKETS: tuple[tuple[str, float], ...] = (
    ("under 1 hour", 3600.0),
    ("1 hour to 1 day", 86400.0),
    ("1 to 3 days", 259200.0),
    ("3 days or more", math.inf),
)

# Display-only (dashboard row count); never read by trading code.
LAST_N = 20
_OPEN_ACTIONS = ("BUY", "SHORT")


def _parse_utc(text: str | None) -> datetime | None:
    try:
        dt = datetime.fromisoformat(str(text).replace(" ", "T").replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


def _position_pnl(position, rows: list[dict]) -> float:
    """Realised $ for one closed position: the recorded exit P&L, else price difference x qty.

    Exit rows are the same split `summarize_closed_position` prices from
    (`split_position_chain`): a TRAIL_STOP stop-move that never filled is not an exit.
    """
    _, exits, _ = split_position_chain(rows)
    recorded = [r.get("realized_pnl") for r in exits if r.get("realized_pnl") is not None]
    if exits and len(recorded) == len(exits):
        return float(sum(float(v) for v in recorded))
    return (position.exit_price - position.entry_price) * position.qty


def _trade_record(position, rows: list[dict]) -> dict:
    r = position.r_multiple
    opened, closed = _parse_utc(position.opened_at), _parse_utc(position.closed_at)
    hold = (closed - opened).total_seconds() if opened and closed else None
    return {
        "position_id": position.position_id,
        "symbol": position.symbol,
        "side": position.direction,
        "pnl": round(_position_pnl(position, rows), 2),
        "r": r,
        "opened_at": position.opened_at,
        "closed_at": position.closed_at,
        "hold_seconds": hold,
    }


def closed_trades(rows: list[dict]) -> tuple[list[dict], int]:
    """(clean-record closed trades oldest-closed first, count excluded as pre-clean-record)."""
    chains: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        pid = row.get("position_id")
        if pid:
            chains[str(pid)].append(row)
    trades: list[dict] = []
    excluded = 0
    for chain in chains.values():
        chain.sort(key=fill_sort_key)
        position = summarize_closed_position(chain)
        if position is None:
            continue
        if _opened_before_clean_record(position.opened_at):
            excluded += 1
            continue
        trades.append(_trade_record(position, chain))
    trades.sort(key=lambda t: t["closed_at"] or "")
    return trades, excluded


# ---------------------------------------------------------------- statistics


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _round(v: float | None, places: int = 2) -> float | None:
    return None if v is None else round(v, places)


def profit_factor(pnls: list[float]) -> float | str:
    """Gross wins / gross losses. "n/a" with no trades, "no losses" with none lost — never infinity."""
    if not pnls:
        return "n/a"
    gross_loss = -sum(p for p in pnls if p < 0)
    if gross_loss <= 0:
        return "no losses"
    return round(sum(p for p in pnls if p > 0) / gross_loss, 2)


def _extremes(winners: list[dict], losers: list[dict]) -> dict:
    def pick(group: list[dict], fn, key: str):
        vals = [t[key] for t in group if t[key] is not None]
        return _round(fn(vals)) if vals else None

    return {
        "largest_winner_r": pick(winners, max, "r"),
        "largest_winner_usd": pick(winners, max, "pnl"),
        "average_winner_r": _round(_mean([t["r"] for t in winners if t["r"] is not None])),
        "average_winner_usd": _round(_mean([t["pnl"] for t in winners])),
        "largest_loser_r": pick(losers, min, "r"),
        "largest_loser_usd": pick(losers, min, "pnl"),
        "average_loser_r": _round(_mean([t["r"] for t in losers if t["r"] is not None])),
        "average_loser_usd": _round(_mean([t["pnl"] for t in losers])),
    }


def window_stats(trades: list[dict]) -> dict:
    """N, win/loss extremes, profit factor, expectancy in R, average hold for one set of trades."""
    n = len(trades)
    winners = [t for t in trades if t["pnl"] > 0]
    losers = [t for t in trades if t["pnl"] < 0]
    r_known = [t["r"] for t in trades if t["r"] is not None]
    holds = [t["hold_seconds"] for t in trades if t["hold_seconds"] is not None]
    return {
        "n": n,
        "wins": len(winners),
        "losses": len(losers),
        "win_rate_pct": round(len(winners) / n * 100, 1) if n else None,
        "total_pnl_usd": round(sum(t["pnl"] for t in trades), 2),
        "profit_factor": profit_factor([t["pnl"] for t in trades]),
        "expectancy_r": _round(_mean(r_known)),
        "r_unknown": n - len(r_known),
        "avg_hold_seconds": _round(_mean(holds), 0),
        **_extremes(winners, losers),
    }


def r_histogram(trades: list[dict]) -> list[dict]:
    counts = [0] * len(R_BUCKETS)
    for t in trades:
        if t["r"] is None:
            continue
        idx = next(i for i, (_, hi) in enumerate(R_BUCKETS) if t["r"] <= hi)
        counts[idx] += 1
    return [{"label": label, "count": c} for (label, _), c in zip(R_BUCKETS, counts, strict=True)]


def _slice_stats(trades: list[dict]) -> dict:
    r_known = [t["r"] for t in trades if t["r"] is not None]
    n = len(trades)
    wins = sum(1 for t in trades if t["pnl"] > 0)
    return {
        "n": n,
        "win_rate_pct": round(wins / n * 100, 1) if n else None,
        "expectancy_r": _round(_mean(r_known)),
        "total_pnl_usd": round(sum(t["pnl"] for t in trades), 2),
        "r_unknown": n - len(r_known),
    }


def _entry_bucket(opened_at: str) -> str:
    dt = _parse_utc(opened_at)
    if dt is None:
        return "unknown"
    et = dt.astimezone(ET)
    return f"{et.hour:02d}:{30 if et.minute >= 30 else 0:02d}"


def _hold_bucket(seconds: float | None) -> str:
    if seconds is None:
        return "unknown"
    return next(label for label, hi in HOLD_BUCKETS if seconds < hi)


def _group(trades: list[dict], key_fn, order: list[str] | None = None) -> list[dict]:
    groups: dict[str, list[dict]] = defaultdict(list)
    for t in trades:
        groups[key_fn(t)].append(t)
    keys = [k for k in (order or []) if k in groups] + sorted(k for k in groups if k not in (order or []))
    return [{"label": k, **_slice_stats(groups[k])} for k in keys]


def slices(trades: list[dict]) -> dict:
    return {
        "by_side": _group(trades, lambda t: t["side"], ["long", "short"]),
        "by_entry_time_et": _group(trades, lambda t: _entry_bucket(t["opened_at"])),
        "by_hold": _group(trades, lambda t: _hold_bucket(t["hold_seconds"]), [b for b, _ in HOLD_BUCKETS]),
    }


# ------------------------------------------------------ drawdown and quality


def losing_streak(trades: list[dict]) -> int:
    longest = run = 0
    for t in trades:
        run = run + 1 if t["pnl"] < 0 else 0
        longest = max(longest, run)
    return longest


def cumulative_r(trades: list[dict]) -> list[dict]:
    total = 0.0
    out = []
    for t in trades:
        if t["r"] is None:
            continue
        total += t["r"]
        out.append({"closed_at": t["closed_at"], "symbol": t["symbol"], "cumulative_r": round(total, 2)})
    return out


def max_drawdown_r(series: list[dict]) -> float:
    """Largest peak-to-trough fall in cumulative R (peak starts at 0). Reported as a positive number."""
    peak = worst = 0.0
    for point in series:
        peak = max(peak, point["cumulative_r"])
        worst = max(worst, peak - point["cumulative_r"])
    return round(worst, 2)


def _betacf(a: float, b: float, x: float) -> float:
    tiny = 1e-30
    qab, qap, qam = a + b, a + 1.0, a - 1.0
    c, d = 1.0, 1.0 - qab * x / qap
    d = 1.0 / (d if abs(d) > tiny else tiny)
    h = d
    for m in range(1, 300):
        m2 = 2 * m
        for num in (m * (b - m) * x / ((qam + m2) * (a + m2)), -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))):
            d = 1.0 + num * d
            d = 1.0 / (d if abs(d) > tiny else tiny)
            c = 1.0 + num / c
            c = c if abs(c) > tiny else tiny
            h *= d * c
        if abs(d * c - 1.0) < 1e-12:
            break
    return h


def _betainc(a: float, b: float, x: float) -> float:
    """Regularised incomplete beta I_x(a, b) (no scipy: it is not a dependency)."""
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    ln_front = math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b) + a * math.log(x) + b * math.log1p(-x)
    front = math.exp(ln_front)
    if x < (a + 1.0) / (a + b + 2.0):
        return front * _betacf(a, b, x) / a
    return 1.0 - front * _betacf(b, a, 1.0 - x) / b


def t_test_p_one_tailed(t: float, df: int) -> float:
    """P(T >= t) for Student's t with `df` degrees of freedom."""
    tail = 0.5 * _betainc(df / 2.0, 0.5, df / (df + t * t))
    return tail if t >= 0 else 1.0 - tail


def quality(trades: list[dict]) -> dict:
    """Mean R / stdev R and the one-tailed p-value that true mean R > 0. Always with N and the caveat."""
    r_vals = [t["r"] for t in trades if t["r"] is not None]
    n = len(r_vals)
    out: dict = {"n": n, "mean_r": None, "stdev_r": None, "score": None, "p_value": None, "caveat": CAVEAT}
    if n == 0:
        return out
    mean = sum(r_vals) / n
    out["mean_r"] = round(mean, 2)
    if n < 2:
        return out
    stdev = math.sqrt(sum((v - mean) ** 2 for v in r_vals) / (n - 1))
    out["stdev_r"] = round(stdev, 2)
    if stdev <= 0:
        return out
    out["score"] = round(mean / stdev, 2)
    out["p_value"] = round(t_test_p_one_tailed(mean / (stdev / math.sqrt(n)), n - 1), 4)
    return out


# ------------------------------------------------------------------- builder


def _et_date(ts: str | None):
    dt = _parse_utc(ts)
    return dt.astimezone(ET).date() if dt else None


def build_trade_review(rows: list[dict], now: datetime | None = None) -> dict:
    """The whole Trade Review payload from raw `trades` rows. Pure; no I/O."""
    now = now or utc_now()
    today = now.astimezone(ET).date()
    week_start = today - timedelta(days=today.weekday())
    trades, excluded = closed_trades(rows)
    todays = [t for t in trades if _et_date(t["closed_at"]) == today]
    this_week = [t for t in trades if (d := _et_date(t["closed_at"])) is not None and d >= week_start]
    today_stats = window_stats(todays)
    series = cumulative_r(trades)
    return {
        "as_of": now.isoformat(),
        "caveat": CAVEAT,
        "excluded_pre_clean_record_n": excluded,
        "n_closed": len(trades),
        "today": {
            "pnl_usd": today_stats["total_pnl_usd"],
            "n": today_stats["n"],
            "wins": today_stats["wins"],
            "losses": today_stats["losses"],
            "win_rate_pct": today_stats["win_rate_pct"],
        },
        "last_trades": trades[-LAST_N:][::-1],
        "r_histogram": r_histogram(trades),
        "windows": {"today": today_stats, "week": window_stats(this_week), "all": window_stats(trades)},
        "slices": slices(trades),
        "drawdown_r": max_drawdown_r(series),
        "longest_losing_streak": losing_streak(trades),
        "cumulative_r": series,
        "quality": quality(trades),
    }


def close_review_summary(rows: list[dict], now: datetime | None = None) -> dict:
    """Today's closed-trade figures for the Telegram close review. Pure; display only.

    Reuses `closed_trades`, `window_stats` and `quality`. Adds only the R total and the best
    and worst trade by R with their symbols, which the window stats do not carry. Any R that
    is unreadable makes the R total and average None (unknown), never zero.
    """
    now = now or utc_now()
    today = now.astimezone(ET).date()
    trades, _excluded = closed_trades(rows)
    todays = [t for t in trades if _et_date(t["closed_at"]) == today]
    stats = window_stats(todays)
    known = [t for t in todays if t["r"] is not None]
    all_known = bool(todays) and len(known) == len(todays)
    total_r = _round(sum(t["r"] for t in known)) if all_known else None
    best = max(known, key=lambda t: t["r"], default=None)
    worst = min(known, key=lambda t: t["r"], default=None)
    q = quality(trades)
    return {
        "n": stats["n"],
        "wins": stats["wins"],
        "losses": stats["losses"],
        "win_rate_pct": stats["win_rate_pct"],
        "total_r": total_r,
        "average_r": stats["expectancy_r"] if all_known else None,
        "best": None if best is None else {"symbol": best["symbol"], "r": _round(best["r"])},
        "worst": None if worst is None else {"symbol": worst["symbol"], "r": _round(worst["r"])},
        "profit_factor": stats["profit_factor"],
        "quality_n": q["n"],
        "p_value": q["p_value"],
    }


# ----------------------------------------------------- open-position R (display)


def open_position_r(positions: list[dict], chain_rows: list[dict]) -> dict[str, dict]:
    """symbol -> {"initial_stop", "unrealised_r"} for held positions, from their position chains.

    `chain_rows` are the EXECUTED rows of each held symbol (`db_reads.get_open_position_rows`, the same
    `quantities.select_position_chain_rows` reader every other R path uses). The live lots are the fill
    walk's open position (`quantities.live_position_lots`) - never a stored `position_id` picked by
    timestamp, which showed a CLOSED position's stop when a fresh buy was written before the old
    position's stop-out fill was known. `initial_stop` is the first live lot's entry stop; `unrealised_r`
    = P&L on the shares held / `quantities.held_position_risk` - the one open-position R formula - and is
    None when unknown (fill order unknown, a lot without a stop, or lots that do not reconcile). Never zero.
    """
    by_symbol: dict[str, list[dict]] = defaultdict(list)
    for row in chain_rows:
        by_symbol[str(row.get("symbol") or "").upper()].append(row)
    out: dict[str, dict] = {}
    for pos in positions:
        sym = str(pos.get("symbol") or "").upper()
        rows = by_symbol.get(sym, [])
        live = live_position_lots(rows) if rows else None
        lots = [r for r in live or [] if str(r.get("action") or "").upper() in _OPEN_ACTIONS]
        stop = next((s for row in lots if (s := recorded_initial_stop(row)) > 0), None)
        qty = float(pos.get("qty") or 0)
        risk = held_position_risk(rows, qty)[0] if rows else None
        r = None
        if risk and pos.get("avg_entry") and pos.get("current_price") and qty:
            r = round((pos["current_price"] - pos["avg_entry"]) * qty / risk, 2)
        out[sym] = {"initial_stop": stop, "unrealised_r": r}
    return out


def closed_r_by_exit_row(rows: list[dict]) -> dict[int, float]:
    """row id of each closed position's final exit row -> its R (omitted when R is unknown).

    For the "R" column in the trades table. Takes whole position chains; not clean-record
    filtered, because it labels rows rather than counting them.
    """
    chains: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        if row.get("position_id"):
            chains[str(row["position_id"])].append(row)
    out: dict[int, float] = {}
    for chain in chains.values():
        chain.sort(key=fill_sort_key)
        position = summarize_closed_position(chain)
        r = _trade_record(position, chain)["r"] if position is not None else None
        if r is not None and chain[-1].get("id") is not None:
            out[chain[-1]["id"]] = r
    return out
