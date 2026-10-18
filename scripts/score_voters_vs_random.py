"""Score the desk's AI voters against random picks from the same day (record only).

Answers one question: do the AI voters pick better than chance? For each
session since the clean-record start, the stocks a voter favoured (and the
stocks the desk actually bought) are compared with 1,000 random draws of the
same number of stocks from the same session's eligible list. Changes no trade;
reads the desk database read-only. See docs/research/voters_vs_random.md.

Run as the desk user so the database and the market-data keys are readable:
    python scripts/score_voters_vs_random.py --db <path> --out result.json
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

EVIDENCE_FROM_UTC = "2026-10-12 13:30"
N_DRAWS = 1000
DEFAULT_WINDOWS = (5, 10, 21)
BULLISH = {"buy", "strong_buy", "bullish"}
COMBINED = "combined_buys"


def load_votes(conn: sqlite3.Connection, since: str = EVIDENCE_FROM_UTC) -> tuple[dict, dict]:
    """Per session date: the eligible list and each voter's favoured stocks.

    Eligible = every stock the technical analyst was shown that session (it
    rates every candidate). Voters: the technical analyst's rating, each
    decision seat's recorded stance, and the combined decision (filled BUYs).
    """
    eligible: dict[str, set[str]] = {}
    picks: dict[str, dict[str, set[str]]] = {}

    def add_pick(day: str, voter: str, sym: str) -> None:
        picks.setdefault(day, {}).setdefault(voter, set()).add(sym)
        eligible.setdefault(day, set()).add(sym)

    rows = conn.execute(
        "SELECT substr(timestamp,1,10), symbol, json_extract(evidence_json,'$.rating') "
        "FROM specialist_evidence WHERE agent_name='tech_analyst' AND kind='analysis' "
        "AND symbol IS NOT NULL AND timestamp >= ? ORDER BY timestamp",
        (since,),
    ).fetchall()
    last_rating: dict[tuple[str, str], str] = {}
    for day, sym, rating in rows:
        eligible.setdefault(day, set()).add(sym)
        last_rating[(day, sym)] = str(rating or "")
    for (day, sym), rating in last_rating.items():
        picks.setdefault(day, {}).setdefault("tech_analyst", set())
        if rating in BULLISH:
            add_pick(day, "tech_analyst", sym)

    for day, sym, seat, stance in conn.execute(
        "SELECT substr(timestamp,1,10), symbol, json_extract(evidence_json,'$.seat'), "
        "json_extract(evidence_json,'$.stance') FROM specialist_evidence "
        "WHERE kind='seat_stance' AND symbol IS NOT NULL AND timestamp >= ?",
        (since,),
    ):
        voter = f"seat_{seat}"
        picks.setdefault(day, {}).setdefault(voter, set())
        eligible.setdefault(day, set()).add(sym)
        if str(stance) in BULLISH:
            add_pick(day, voter, sym)

    for day, sym in conn.execute(
        "SELECT substr(timestamp,1,10), symbol FROM trades WHERE action='BUY' "
        "AND fill_status='filled' AND timestamp >= ?",
        (since,),
    ):
        add_pick(day, COMBINED, sym)
    return eligible, picks


def load_exits(conn: sqlite3.Connection, since: str = EVIDENCE_FROM_UTC) -> dict:
    """First buy day and first exit (date, fill price) per position bought since `since`."""
    out: dict[str, dict] = {}
    for pos, sym, action, ts, price in conn.execute(
        "SELECT position_id, symbol, action, timestamp, fill_price FROM trades "
        "WHERE fill_status='filled' AND position_id IS NOT NULL AND timestamp >= ? "
        "ORDER BY timestamp",
        (since,),
    ):
        rec = out.get(pos)
        if action == "BUY" and rec is None:
            out[pos] = {"symbol": sym, "buy_day": ts[:10], "exit_day": None, "exit_price": None}
        elif rec is not None and action in {"SELL", "STOP_OUT"} and rec["exit_day"] is None:
            rec["exit_day"], rec["exit_price"] = ts[:10], float(price)
    return out


def forward_returns(closes: pd.DataFrame, day: str, window: int) -> pd.Series:
    """Close-to-close return from `day`'s close over the next `window` sessions.

    `closes`: index = session dates (sorted), columns = symbols. A stock with no
    close that day, or a window not yet matured, gets NaN.
    """
    idx = closes.index.searchsorted(pd.Timestamp(day))
    if idx >= len(closes) or closes.index[idx] != pd.Timestamp(day):
        return pd.Series(dtype=float)
    end = idx + window
    if end >= len(closes):
        return pd.Series(np.nan, index=closes.columns)
    return closes.iloc[end] / closes.iloc[idx] - 1.0


def random_draws(rng: np.random.Generator, n_eligible: int, k: int, n_draws: int = N_DRAWS) -> np.ndarray:
    """`n_draws` x `k` indices, each row a draw without replacement (numpy `permuted`)."""
    base = np.tile(np.arange(n_eligible), (n_draws, 1))
    return rng.permuted(base, axis=1)[:, :k]


def percentile(actual: float, null: np.ndarray) -> float:
    """Share of random draws the actual pick beats, ties counted half (0-100)."""
    return float(100.0 * ((null < actual).mean() + 0.5 * (null == actual).mean()))


def score(eligible: dict, picks: dict, returns_by_day: dict, rng: np.random.Generator, n_draws: int = N_DRAWS) -> dict:
    """Per voter: per-session percentile, pooled percentile and permutation p-value.

    `returns_by_day[day]` maps symbol -> forward return. Sessions and voters are
    walked in sorted order so a saved seed reproduces every draw.
    """
    voters = sorted({v for d in picks.values() for v in d})
    result: dict[str, dict] = {}
    for voter in voters:
        sessions, actual_means, null_means, n_picks = [], [], [], 0
        for day in sorted(picks):
            chosen = picks[day].get(voter, set())
            rets = returns_by_day.get(day, {})
            pool = sorted(s for s in eligible.get(day, set()) if np.isfinite(rets.get(s, np.nan)))
            chosen = sorted(s for s in chosen if s in pool)
            if not chosen or len(chosen) >= len(pool):
                continue
            vec = np.array([rets[s] for s in pool])
            actual = float(np.mean([rets[s] for s in chosen]))
            null = vec[random_draws(rng, len(pool), len(chosen), n_draws)].mean(axis=1)
            sessions.append(
                {
                    "day": day,
                    "n_eligible": len(pool),
                    "n_picks": len(chosen),
                    "pick_mean_return": actual,
                    "random_mean_return": float(null.mean()),
                    "percentile": percentile(actual, null),
                }
            )
            actual_means.append(actual)
            null_means.append(null)
            n_picks += len(chosen)
        if not sessions:
            result[voter] = {"n_sessions": 0, "n_picks": 0, "sessions": []}
            continue
        pooled_actual = float(np.mean(actual_means))
        pooled_null = np.vstack(null_means).mean(axis=0)
        exceed = int((pooled_null >= pooled_actual).sum())
        result[voter] = {
            "n_sessions": len(sessions),
            "n_picks": n_picks,
            "pooled_pick_mean_return": pooled_actual,
            "pooled_random_mean_return": float(pooled_null.mean()),
            "pooled_percentile": percentile(pooled_actual, pooled_null),
            "permutation_p_value": (1 + exceed) / (1 + n_draws),
            "sessions": sessions,
        }
    return result


def realised_exits(exits: dict, closes: pd.DataFrame) -> list[dict]:
    """Each bought position: return from buy-day close to the desk's actual exit fill
    (or the latest close if still open), with the buy day's eligible-list median over
    the same span for context."""
    out = []
    last_day = closes.index[-1] if len(closes) else None
    for pos, rec in sorted(exits.items()):
        sym, buy_day = rec["symbol"], pd.Timestamp(rec["buy_day"])
        if sym not in closes.columns or buy_day not in closes.index:
            continue
        start = closes.at[buy_day, sym]
        if rec["exit_price"] is not None:
            ret, status = rec["exit_price"] / start - 1.0, "exited"
        else:
            ret, status = closes.at[last_day, sym] / start - 1.0, "open_marked_to_latest_close"
        out.append(
            {
                "position": pos,
                "symbol": sym,
                "buy_day": rec["buy_day"],
                "exit_day": rec["exit_day"],
                "status": status,
                "return": float(ret),
            }
        )
    return out


def fetch_closes(symbols: list[str], first_day: str) -> pd.DataFrame:
    """Daily closes from the desk's own Alpaca market-data client (read-only)."""
    for p in (Path.cwd(), Path(__file__).resolve().parents[1]):
        if str(p) not in sys.path:
            sys.path.insert(0, str(p))
    from src.api.deps import get_alpaca_credentials, get_alpaca_paper
    from src.execution.broker import AlpacaBroker

    key, secret = get_alpaca_credentials()
    broker = AlpacaBroker(api_key=key, secret_key=secret, paper=get_alpaca_paper())
    lookback = (date.today() - date.fromisoformat(first_day)).days + 7
    frames = {}
    for i in range(0, len(symbols), 100):
        bars = broker.get_bars_batch(symbols[i : i + 100], lookback, adjustment="all")
        for sym, rows in bars.items():
            frames[sym] = pd.Series({pd.Timestamp(b.date): b.close for b in rows}, dtype=float)
    return pd.DataFrame(frames).sort_index()


def summarise(res: dict, windows: list[str], seed: int) -> str:
    lines = [f"AI voters vs 1,000 same-day random picks (seed {seed})"]
    for w in windows:
        lines.append(f"Window {w}:")
        for voter, r in res[w].items():
            if not r["n_sessions"]:
                lines.append(f"  {voter}: no matured sessions yet")
                continue
            lines.append(
                f"  {voter}: percentile {r['pooled_percentile']:.0f}, "
                f"p={r['permutation_p_value']:.3f}, picks {r['pooled_pick_mean_return']:+.2%} "
                f"vs random {r['pooled_random_mean_return']:+.2%} "
                f"({r['n_sessions']} sessions, {r['n_picks']} picks)"
            )
    lines.append("Thin evidence: a few weeks of sessions cannot separate skill from luck; read the p-value.")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--db", default="data/quant_agent.db")
    ap.add_argument("--out", default="voters_vs_random.json")
    ap.add_argument("--windows", default=",".join(map(str, DEFAULT_WINDOWS)))
    ap.add_argument("--seed", type=int, default=None, help="re-run a saved result exactly")
    args = ap.parse_args(argv)

    seed = args.seed if args.seed is not None else int(np.random.SeedSequence().entropy)
    rng = np.random.default_rng(seed)
    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    try:
        eligible, picks = load_votes(conn)
        exits = load_exits(conn)
    finally:
        conn.close()
    days = sorted(eligible)
    if not days:
        print("No sessions recorded since", EVIDENCE_FROM_UTC)
        return 0
    symbols = sorted(set().union(*eligible.values()))
    first = (date.fromisoformat(days[0]) - timedelta(days=7)).isoformat()
    closes = fetch_closes(symbols, first)

    windows = [int(w) for w in args.windows.split(",") if w.strip()]
    res = {}
    for w in windows:
        rets = {d: forward_returns(closes, d, w).dropna().to_dict() for d in days}
        res[str(w)] = score(eligible, picks, rets, rng)
    payload = {
        "seed": seed,
        "n_draws": N_DRAWS,
        "evidence_from_utc": EVIDENCE_FROM_UTC,
        "latest_close": str(closes.index[-1].date()) if len(closes) else None,
        "sessions": days,
        "eligible_counts": {d: len(eligible[d]) for d in days},
        "windows": res,
        "bought_to_actual_exit": realised_exits(exits, closes),
    }
    Path(args.out).write_text(json.dumps(payload, indent=2, default=str))
    print(summarise(res, [str(w) for w in windows], seed))
    print(f"Written: {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
