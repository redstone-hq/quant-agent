"""The screen's trading cost is the EDGE estimate (`bidask.edge`), never a zero stand-in.

The hand-written Corwin & Schultz estimator it replaced floored a fast mover's
average at zero (16 Oct 2026: 1,606 of 3,660 passers at zero, 42 tied at the
top score K), so the morning shortlist was cut alphabetically. Synthetic
bars trade only at bid or ask around a random-walk mid (`_quoted_bars`).
"""

from __future__ import annotations

import json

from src import universe_screen as us
from tests.test_universe_screen import TH, _quoted_bars

INPUTS = us.SecondFilterInputs(hold_sessions=10, stop_atr_multiple=2.5, max_portfolio_risk_pct=1.0, gross_ceiling_x=2.0)


def _screened(spread: float, vol: float, seed: int) -> dict:
    failures, measured = us.check_bars(_quoted_bars(400, spread=spread, vol=vol, seed=seed), TH)
    assert failures == []
    return measured


def _passer(symbol: str, measured: dict) -> dict:
    return {
        "symbol": symbol,
        "status": "passed",
        "failures": [],
        "measured": measured,
        "second_filter": us.second_filter_score(measured, INPUTS),
    }


def test_a_fast_mover_gets_a_non_zero_cost():
    # 0.6% quoted spread under 5% daily volatility: the old estimator read 0.
    measured = _screened(0.006, 0.05, seed=0)
    assert measured["half_spread_bps"] > 0
    assert "cost_unestimable" not in measured


def test_an_unestimable_name_carries_its_reason_and_is_not_ranked_above_measured_names(tmp_path):
    unknown = _screened(0.0, 0.03, seed=0)  # zero spread: EDGE's signed estimate is negative
    assert unknown["cost_unestimable"] == "cost_unestimable:negative_estimate"
    assert "half_spread_bps" not in unknown
    known = _screened(0.01, 0.05, seed=0)
    record = tmp_path / "2026-10-19.jsonl"
    record.write_text("\n".join(json.dumps(_passer(s, m)) for s, m in [("ZZZA", unknown), ("ZZZB", known)]) + "\n")
    ranked, details = us.rank_passers(record)
    assert ranked == ["ZZZB"]
    assert details["ZZZA"]["k"] is None
    assert details["ZZZA"]["score_reason"] == "cost_unestimable:negative_estimate"


def test_two_names_with_different_spreads_do_not_tie():
    tight = _screened(0.006, 0.05, seed=0)
    wide = _screened(0.01, 0.05, seed=0)  # the same mid path, a wider quote
    assert tight["half_spread_bps"] < wide["half_spread_bps"]
    k_tight = us.second_filter_score(tight, INPUTS)["k"]
    k_wide = us.second_filter_score(wide, INPUTS)["k"]
    assert k_tight is not None and k_wide is not None and k_tight != k_wide
