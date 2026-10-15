"""Record-only morning-scan score K (src/universe_screen.py `second_filter_score`,
written per passer by src/universe_daily.py). It orders, sizes and stops nothing."""

from __future__ import annotations

import pytest

from src import universe_daily as ud
from src.universe_screen import SecondFilterInputs, second_filter_score
from tests.test_universe_daily import PROFILE, Clock, _asset
from tests.test_universe_screen import TH, TODAY, _good_bars

RT_PER_A = 0.1  # equal round-trip-spread to ATR ratio for every name in the tie tests


def _inputs(gross_x, reason=None):
    return SecondFilterInputs(
        hold_sessions=10,
        stop_atr_multiple=2.5,
        max_portfolio_risk_pct=25.0,
        gross_ceiling_x=gross_x,
        gross_ceiling_reason=reason,
    )


def _measured(a):
    """Measured fields for ATR/price `a` and round trip = RT_PER_A x a (half spread in bps)."""
    return {"atr_pct": a * 100, "half_spread_bps": RT_PER_A * a / 2 * 10_000}


def _k(a, gross_x):
    return second_filter_score(_measured(a), _inputs(gross_x))["k"]


def test_at_gross_2_twelve_percent_and_six_percent_tie_and_three_scores_below_five():
    assert _k(0.12, 2.0) == pytest.approx(_k(0.06, 2.0))
    assert _k(0.03, 2.0) < _k(0.05, 2.0)


def test_at_gross_1_the_crossover_moves_to_ten_percent():
    assert _k(0.20, 1.0) == pytest.approx(_k(0.10, 1.0))
    assert _k(0.05, 1.0) < _k(0.10, 1.0)
    # at G 2.0 the plateau already starts at 5%, so 5% and 10% tie there
    assert _k(0.05, 2.0) == pytest.approx(_k(0.10, 2.0))


def test_missing_atr_or_spread_gives_no_k_and_a_reason():
    assert second_filter_score({"half_spread_bps": 5.0}, _inputs(2.0)) == {
        "k": None,
        "reason": "atr_unmeasured",
        "gross_ceiling_x": 2.0,
    }
    got = second_filter_score({"atr_pct": 3.0}, _inputs(2.0))
    assert got["k"] is None and got["reason"] == "spread_unmeasured"


def test_unread_gross_ceiling_records_the_two_parts_and_no_k():
    got = second_filter_score(_measured(0.04), _inputs(None, "gross_ceiling_unreadable: OSError"))
    assert got["k"] is None and got["reason"] == "gross_ceiling_unreadable: OSError"
    assert got["risk_cap_x"] == pytest.approx(0.25 / (2.5 * 0.04))
    assert got["net_edge"] > 0


def test_rank_is_among_passers_only_and_changes_no_status():
    run = ud.DailyRun(today=TODAY)
    for symbol, a in (("LOW", 0.02), ("MID", 0.05), ("HIGH", 0.10)):
        run.record(symbol, "passed", [], _measured(a))
    run.record("NOATR", "passed", [], {"half_spread_bps": 5.0})
    run.record("GONE", "dropped", ["spread_too_wide"], _measured(0.05))
    before = {s: (r["status"], r["failures"]) for s, r in run.records.items()}
    run.add_second_filter(_inputs(2.0))
    after = {s: (r["status"], r["failures"]) for s, r in run.records.items()}
    assert before == after
    sf = {s: r.get("second_filter") for s, r in run.records.items()}
    assert sf["HIGH"]["rank"] == 1 and sf["MID"]["rank"] == 1 and sf["LOW"]["rank"] == 3
    assert sf["NOATR"]["k"] is None and sf["NOATR"]["rank"] is None and sf["NOATR"]["reason"]
    assert sf["GONE"] is None


def _screen(assets, tmp_path, second_filter):
    return ud.run_daily_record(
        assets,
        get_bars_batch=lambda chunk: {s: _good_bars() for s in chunk},
        get_profile=lambda s: PROFILE,
        get_filings=lambda s: [],
        get_role=lambda ref: "target",
        th=TH,
        today=TODAY,
        deadline=1e9,
        batch_size=2,
        cache=ud.ReadCache(tmp_path),
        clock=Clock(),
        second_filter=second_filter,
    )


def test_every_passer_gets_a_k_or_a_reason_and_the_passing_set_is_unchanged(tmp_path):
    assets = [_asset("AAA"), _asset("BBB"), _asset("DEAD", status="inactive")]
    plain = _screen(assets, tmp_path, None)
    scored = _screen(assets, tmp_path, _inputs(2.0))
    summary = lambda run: {s: (r["status"], r["failures"]) for s, r in run.records.items()}  # noqa: E731
    assert summary(plain) == summary(scored)
    passers = [s for s, r in scored.records.items() if r["status"] == "passed"]
    assert passers == ["AAA", "BBB"]
    for symbol in passers:
        sf = scored.records[symbol]["second_filter"]
        assert sf["k"] is not None or sf["reason"]
    assert all("second_filter" not in r for r in plain.records.values())
    assert "second_filter" not in scored.records["DEAD"]
