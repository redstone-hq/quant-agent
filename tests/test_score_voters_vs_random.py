"""Synthetic ZZZ checks for the voters-vs-random scorer."""

import importlib.util
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
    eligible = {d: set(SYMS) for d in DAYS}
    return eligible, returns, data_rng


def test_voter_that_picks_the_best_stock_scores_near_100th_percentile():
    eligible, returns, _ = _synthetic()
    picks = {d: {"oracle": {max(r, key=r.get)}} for d, r in returns.items()}
    out = svr.score(eligible, picks, returns, np.random.default_rng(1))["oracle"]
    assert out["n_sessions"] == len(DAYS)
    assert out["pooled_percentile"] > 99.0
    assert all(s["percentile"] > 90.0 for s in out["sessions"])
    assert out["permutation_p_value"] <= 2 / 1001


def test_random_voter_scores_near_50th_percentile():
    eligible, returns, data_rng = _synthetic()
    pct = []
    for trial in range(30):
        picks = {d: {"coin": set(data_rng.choice(SYMS, 3, replace=False))} for d in DAYS}
        pct.append(svr.score(eligible, picks, returns, np.random.default_rng(trial))["coin"]["pooled_percentile"])
    assert 35.0 < float(np.mean(pct)) < 65.0


def test_saved_seed_reproduces_identical_draws_and_scores():
    seed = int(np.random.SeedSequence().entropy)
    a = svr.random_draws(np.random.default_rng(seed), 20, 3)
    b = svr.random_draws(np.random.default_rng(seed), 20, 3)
    assert a.shape == (svr.N_DRAWS, 3)
    assert np.array_equal(a, b)
    assert all(len(set(row)) == 3 for row in a)  # without replacement
    eligible, returns, _ = _synthetic()
    picks = {d: {"v": {SYMS[0], SYMS[1]}} for d in DAYS}
    assert svr.score(eligible, picks, returns, np.random.default_rng(seed)) == svr.score(
        eligible, picks, returns, np.random.default_rng(seed)
    )


def test_forward_return_is_nan_until_the_window_matures():
    idx = pd.to_datetime(["2026-01-05", "2026-01-06", "2026-01-07"])
    closes = pd.DataFrame({"ZZZ00": [100.0, 110.0, 121.0]}, index=idx)
    assert svr.forward_returns(closes, "2026-01-05", 2)["ZZZ00"] == pytest.approx(0.21)
    assert np.isnan(svr.forward_returns(closes, "2026-01-06", 5)["ZZZ00"])
