"""Synthetic ZZZ checks for the voters-vs-random scorer."""

import importlib.util
import sqlite3
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

_SPEC = importlib.util.spec_from_file_location(
    "score_voters_vs_random", Path(__file__).resolve().parents[1] / "scripts" / "score_voters_vs_random.py"
)
svr = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(svr)

SYMS = [f"ZZZ{i:02d}" for i in range(20)]
DAYS = [f"2026-01-{d:02d}" for d in range(5, 25)]


def _synthetic(seed: int = 7):
    data_rng = np.random.default_rng(seed)
    returns = {d: dict(zip(SYMS, data_rng.normal(0, 0.02, len(SYMS)))) for d in DAYS}
    shown = {d: {} for d in DAYS}
    return shown, returns, data_rng


def _all_listed(shown, voter, syms=SYMS):
    for d in DAYS:
        shown[d][voter] = set(syms)


def test_voter_that_picks_the_best_stock_scores_near_100th_percentile():
    shown, returns, _ = _synthetic()
    _all_listed(shown, "oracle")
    picks = {d: {"oracle": {max(r, key=r.get): (1, None)}} for d, r in returns.items()}
    out = svr.score(shown, picks, returns, np.random.default_rng(1))["oracle"]
    assert out["n_sessions"] == len(DAYS)
    assert out["pooled_percentile"] > 99.0
    assert out["permutation_p_value"] <= 2 / 1001


def test_random_voter_scores_near_50th_percentile():
    shown, returns, data_rng = _synthetic()
    _all_listed(shown, "coin")
    pct = []
    for trial in range(30):
        picks = {d: {"coin": {s: (1, None) for s in data_rng.choice(SYMS, 3, replace=False)}} for d in DAYS}
        pct.append(svr.score(shown, picks, returns, np.random.default_rng(trial))["coin"]["pooled_percentile"])
    assert 35.0 < float(np.mean(pct)) < 65.0


def test_saved_seed_reproduces_identical_draws_and_scores():
    seed = int(np.random.SeedSequence().entropy)
    a = svr.random_draws(np.random.default_rng(seed), 20, 3)
    b = svr.random_draws(np.random.default_rng(seed), 20, 3)
    assert a.shape == (svr.N_DRAWS, 3)
    assert np.array_equal(a, b)
    shown, returns, _ = _synthetic()
    _all_listed(shown, "v")
    picks = {d: {"v": {SYMS[0]: (1, None), SYMS[1]: (-1, None)}} for d in DAYS}
    assert svr.score(shown, picks, returns, np.random.default_rng(seed)) == svr.score(
        shown, picks, returns, np.random.default_rng(seed)
    )


def test_forward_return_is_nan_until_the_window_matures():
    idx = pd.to_datetime(["2026-01-05", "2026-01-06", "2026-01-07"])
    closes = pd.DataFrame({"ZZZ00": [100.0, 110.0, 121.0]}, index=idx)
    assert svr.forward_returns(closes, "2026-01-05", 2)["ZZZ00"] == pytest.approx(0.21)
    assert np.isnan(svr.forward_returns(closes, "2026-01-06", 5)["ZZZ00"])


def test_short_pick_on_a_falling_stock_scores_positive():
    shown, returns, _ = _synthetic()
    _all_listed(shown, "bear")
    for d in DAYS:
        returns[d]["ZZZ00"] = -0.30  # the stock the voter shorts falls hard
    picks = {d: {"bear": {"ZZZ00": (-1, None)}} for d in DAYS}
    out = svr.score(shown, picks, returns, np.random.default_rng(3))["bear"]
    assert out["pooled_pick_mean_return"] == pytest.approx(0.30)
    assert out["excess_over_random"] > 0.2
    assert out["pooled_percentile"] > 99.0


def test_voter_is_graded_only_on_its_own_list():
    shown, returns, _ = _synthetic()
    # The voter saw only the 5 worst stocks each day and picks one of them.
    # Against the union list it would beat chance; against its own list it must not.
    for d in DAYS:
        worst = sorted(returns[d], key=returns[d].get)[:5]
        shown[d]["narrow"] = set(worst)
        shown[d]["wide"] = set(SYMS)
    picks = {d: {"narrow": {sorted(shown[d]["narrow"])[0]: (1, None)}} for d in DAYS}
    out = svr.score(shown, picks, returns, np.random.default_rng(5))["narrow"]
    assert all(s["n_eligible"] == 5 for s in out["sessions"])
    assert out["pooled_random_mean_return"] < -0.01  # random baseline drawn from the 5 worst only


def test_voter_without_a_recorded_list_is_not_graded_and_says_so():
    shown, returns, _ = _synthetic()
    picks = {d: {"ghost": {"ZZZ00": (1, None)}} for d in DAYS}
    out = svr.score(shown, picks, returns, np.random.default_rng(1))["ghost"]
    assert out["n_sessions"] == 0
    text = svr.summarise({"5": {"ghost": out}}, ["5"], 1, {"ghost": "NOT RECORDED: list unknown"})
    assert "NOT RECORDED" in text


def test_interval_and_conviction_split_present():
    shown, returns, _ = _synthetic()
    _all_listed(shown, "v")
    _all_listed(shown, "w")
    picks = {d: {"v": {"ZZZ00": (1, "high"), "ZZZ01": (-1, "low")}, "w": {"ZZZ00": (1, None)}} for d in DAYS}
    out = svr.score(shown, picks, returns, np.random.default_rng(2))
    lo, hi = out["v"]["chance_band_95"]
    assert lo < 0 < hi
    assert set(out["v"]["by_conviction"]) == {"high", "low"}
    assert out["v"]["by_conviction"]["high"]["chance_band_95"]
    assert "no conviction" in out["w"]["by_conviction"]


def test_load_votes_reads_both_directions_and_each_seats_own_list():
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE specialist_evidence (timestamp TEXT, agent_name TEXT, kind TEXT, symbol TEXT, evidence_json TEXT)"
    )
    conn.execute("CREATE TABLE trades (timestamp TEXT, symbol TEXT, action TEXT, fill_status TEXT)")
    ts = "2026-10-13 14:00:00"
    ev = [
        ("tech_analyst", "analysis", "ZZZ01", '{"rating":"sell","conviction":"high"}'),
        ("tech_analyst", "analysis", "ZZZ02", '{"rating":"buy","conviction":"low"}'),
        ("tech_analyst", "analysis", "ZZZ03", '{"rating":"neutral","conviction":"low"}'),
        ("x", "seat_stance", "ZZZ01", '{"seat":"news","stance":"bearish","conviction":"medium"}'),
        ("x", "seat_stance", "ZZZ02", '{"seat":"news","stance":"underweight","conviction":"high"}'),
    ]
    conn.executemany("INSERT INTO specialist_evidence VALUES (?,?,?,?,?)", [(ts, a, k, s, j) for a, k, s, j in ev])
    conn.execute("INSERT INTO trades VALUES (?,?,?,?)", (ts, "ZZZ02", "BUY", "filled"))
    shown, picks, source = svr.load_votes(conn, since="2026-10-12")
    day = "2026-10-13"
    assert shown[day]["tech_analyst"] == {"ZZZ01", "ZZZ02", "ZZZ03"}
    assert shown[day][svr.COMBINED] == {"ZZZ01", "ZZZ02", "ZZZ03"}
    assert shown[day]["seat_news"] == {"ZZZ01", "ZZZ02"}
    assert picks[day]["tech_analyst"] == {"ZZZ01": (-1, "high"), "ZZZ02": (1, "low")}
    assert picks[day]["seat_news"] == {"ZZZ01": (-1, "medium"), "ZZZ02": (-1, "high")}
    assert not source[svr.COMBINED].startswith("NOT RECORDED")
