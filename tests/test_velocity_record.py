"""The velocity record (owner ruling 2026-10-13, step A): right math, recorded only.

Every number comes from the stock's own bars or the candidate's own preview;
the record lands on the durable `candidate_ranked` event and never moves the
candidate order it is recorded beside.
"""

from __future__ import annotations

import math
import random
from datetime import date, timedelta
from types import SimpleNamespace

import pytest

import src.stage_decision as stage_decision
from src.data.levels import ATR_PER_SIGMA
from src.data.technical import atr_series
from src.models import AnalystVerdict, OHLCV, VerdictEvidence
from src.storage.db import Database
from src.velocity import (
    candidate_velocity,
    median_sessions_to_reach,
    reach_chance,
    round_trip_cost,
    variance_ratio,
    velocity_record,
)
from src.verdicts import rank_verdicts

W = stage_decision.VELOCITY_HOLD_SESSIONS


def _closes(returns, start=100.0):
    closes = [start]
    for r in returns:
        closes.append(closes[-1] * math.exp(r))
    return closes


def _bars(closes):
    day = date(2026, 1, 1)
    return [
        OHLCV(date=day + timedelta(days=i), open=c, high=c * 1.01, low=c * 0.99, close=c, volume=1000)
        for i, c in enumerate(closes)
    ]


def test_hold_horizon_is_the_owner_two_weeks():
    assert W == 10


def test_random_walk_variance_ratio_is_near_one():
    rng = random.Random(7)
    vr = variance_ratio(_closes([rng.gauss(0.0, 0.01) for _ in range(2000)]), W)
    assert vr == pytest.approx(1.0, abs=0.25)


def test_alternating_series_is_whipsaw_below_one():
    closes = [100.0 if i % 2 == 0 else 102.0 for i in range(200)]
    assert variance_ratio(closes, W) < 0.2


def test_trending_series_is_above_one():
    rng = random.Random(11)
    returns, prev = [], 0.0
    for _ in range(2000):
        prev = 0.6 * prev + rng.gauss(0.0, 0.01)
        returns.append(prev)
    assert variance_ratio(_closes(returns), W) > 1.5


def test_variance_ratio_unreadable_is_none():
    assert variance_ratio([100.0 + i for i in range(W)], W) is None  # not more returns than W
    assert variance_ratio([100.0] * 50, W) is None  # never varies
    assert variance_ratio([100.0, -1.0] * 30, W) is None  # not a price


def test_reach_chance_is_erfc_of_distance_over_sigma_root_2w():
    atr = 3.2
    sigma = atr / ATR_PER_SIGMA
    assert reach_chance(0.0, atr, W) == pytest.approx(1.0)
    one = sigma * math.sqrt(2 * W)
    assert reach_chance(one, atr, W) == pytest.approx(math.erfc(1.0)) == pytest.approx(0.1572992, abs=1e-6)
    t = 5.0
    assert reach_chance(t, atr, W) == pytest.approx(math.erfc(t / (sigma * math.sqrt(2 * W))))
    assert reach_chance(None, atr, W) is None and reach_chance(t, None, W) is None


def test_median_sessions_is_where_reach_chance_is_one_half():
    sigma, t = 1.7, 4.0
    median = median_sessions_to_reach(t, sigma)
    assert math.erfc(t / (sigma * math.sqrt(2 * median))) == pytest.approx(0.5)
    assert median_sessions_to_reach(sigma, sigma) == pytest.approx(2.198, abs=1e-3)
    assert median_sessions_to_reach(t, None) is None


def test_cost_is_unknown_without_a_quote_never_zero():
    assert round_trip_cost(None, None) is None
    assert round_trip_cost(10.0, None) is None and round_trip_cost(None, 10.02) is None
    assert round_trip_cost(10.0, 10.02) == pytest.approx(0.02)
    assert (
        velocity_record(_bars([100.0] * 30), entry_price=100.0, target_distance=1.0, horizon_sessions=W)[
            "round_trip_cost"
        ]
        is None
    )


def test_record_sigma_reuses_the_desk_atr_and_conversion():
    rng = random.Random(3)
    bars = _bars(_closes([rng.gauss(0.0, 0.02) for _ in range(120)]))
    rec = velocity_record(list(reversed(bars)), entry_price=50.0, target_distance=-2.0, horizon_sessions=W)
    assert rec["atr"] == pytest.approx(float(atr_series(bars)[-1]))
    assert rec["sigma"] == pytest.approx(rec["atr"] / ATR_PER_SIGMA)
    assert ATR_PER_SIGMA == pytest.approx(1.596, abs=1e-3)
    assert rec["sigma_per_dollar"] == pytest.approx(rec["sigma"] / 50.0)
    assert rec["target_distance"] == 2.0 and rec["horizon_sessions"] == W
    assert rec["variance_ratio"] == pytest.approx(variance_ratio([b.close for b in bars], W))


def test_candidate_velocity_reads_the_preview_target():
    bars = _bars(_closes([0.01, -0.01] * 40))
    analysis = SimpleNamespace(symbol="abc", entry_price=99.0)
    rec = candidate_velocity({"ABC": bars}, analysis, (100.0, 95.0, 106.0), horizon_sessions=W)
    assert rec["target_distance"] == pytest.approx(6.0) and rec["reach_chance"] is not None
    blind = candidate_velocity({"ABC": bars}, analysis, None, horizon_sessions=W)
    assert blind["target_distance"] is None and blind["reach_chance"] is None
    assert blind["sigma_per_dollar"] == pytest.approx(blind["sigma"] / 99.0)
    assert candidate_velocity({}, analysis, None, horizon_sessions=W)["sigma"] is None


def _verdict(symbol: str, magnitude: float) -> AnalystVerdict:
    return AnalystVerdict(
        seat="technical",
        symbol=symbol,
        direction="bullish",
        magnitude=magnitude,
        conviction="medium",
        evidence=[VerdictEvidence(label="stop_loss", value=95.0)],
        invalidation="closes below 95",
    )


def test_recording_velocity_never_changes_the_candidate_order(monkeypatch):
    verdicts = [_verdict("AAA", 0.9), _verdict("MMM", 0.5), _verdict("ZZZ", 0.1)]
    ranked = rank_verdicts(verdicts)
    before = [(c.symbol, c.score, dict(c.components)) for c in ranked]
    # The slowest name ranks first and the fastest last, so any leak of the
    # record into the ordering would reverse it.
    velocity = {
        sym: velocity_record(
            _bars(_closes([step, -step] * 30)), entry_price=100.0, target_distance=2.0, horizon_sessions=W
        )
        for sym, step in (("AAA", 0.001), ("MMM", 0.01), ("ZZZ", 0.05))
    }
    events = []
    monkeypatch.setattr(stage_decision, "_record_pipeline_event", lambda *a, **kw: events.append((a, kw)))
    stage_decision._journal_ranked_candidates(SimpleNamespace(), SimpleNamespace(), ranked, velocity)
    assert [c.symbol for c in ranked] == ["AAA", "MMM", "ZZZ"]
    assert [(c.symbol, c.score, dict(c.components)) for c in ranked] == before
    assert [(c.symbol, c.score, c.components) for c in rank_verdicts(verdicts)] == before
    assert [(a[2], kw["rank"]) for a, kw in events] == [("AAA", 1), ("MMM", 2), ("ZZZ", 3)]
    assert all(kw["velocity"] == velocity[a[2]] for a, kw in events)
    assert all("variance_ratio" not in kw["components"] for _, kw in events)


def test_velocity_lands_on_the_durable_candidate_ranked_event(tmp_path):
    db = Database(str(tmp_path / "t.db"))
    db.initialize()
    ranked = rank_verdicts([_verdict("AAA", 0.9)])
    rec = velocity_record(
        _bars(_closes([0.01, -0.01] * 30)), entry_price=100.0, target_distance=2.0, horizon_sessions=W
    )
    ctx = SimpleNamespace(run_id="run-1", decision_id="dec-1")
    stage_decision._journal_ranked_candidates(SimpleNamespace(db=db), ctx, ranked, {"AAA": rec})
    blob = " ".join(str(tuple(r)) for r in db.conn.execute("SELECT * FROM specialist_evidence").fetchall())
    assert "candidate_ranked" in blob and "variance_ratio" in blob and "median_sessions_to_reach" in blob
