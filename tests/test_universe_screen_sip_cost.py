"""Nightly screen trading cost from real consolidated (SIP) quotes.

The cost each passer's score K subtracts is the median quoted half-spread of
its quotes over the cost window, never a bar estimate. Defect 2026-10-19: the
Corwin & Schultz bar estimate read 1,501 of 3,513 passers at zero cost, 45
tied at the top score, and the morning top 60 were cut alphabetically.
ZZZ tickers, fake quote pairs, no network.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone

import pytest

from src import universe_screen as us
from tests.test_universe_screen import TH

INPUTS = us.SecondFilterInputs(
    hold_sessions=TH.hold_sessions, stop_atr_multiple=2.5, max_portfolio_risk_pct=1.0, gross_ceiling_x=2.0
)


def _quotes(mid: float, half_bps: float, n: int = 50) -> list[tuple[float, float]]:
    h = mid * half_bps / 10_000
    return [(mid - h, mid + h)] * n


def _passer(symbol: str, quotes: list | None, atr_pct: float = 2.0) -> dict:
    """One nightly-record passer row, its cost read through `check_cost`."""
    measured = {"atr_pct": atr_pct}
    failures, cost = us.check_cost(quotes, measured, TH)
    assert failures == []
    measured.update(cost)
    return {
        "symbol": symbol,
        "status": "passed",
        "failures": [],
        "measured": measured,
        "second_filter": us.second_filter_score(measured, INPUTS),
    }


def _rank(tmp_path, rows: list[dict]):
    path = tmp_path / "record.jsonl"
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))
    return us.rank_passers(path)


def test_liquid_name_with_tight_quotes_gets_a_small_nonzero_cost():
    # AAPL-like: a one-cent spread on a $250 stock is 0.2 bps half-spread.
    _, cost = us.check_cost(_quotes(250.0, 0.2) + [(250.0, 250.0)], {"atr_pct": 2.0}, TH)
    assert 0 < cost["half_spread_bps"] < 1
    assert cost["half_spread_bps"] == pytest.approx(0.2, rel=1e-3)
    assert "cost_unmeasured" not in cost


def test_median_ignores_locked_crossed_and_one_sided_quotes():
    noisy = _quotes(100.0, 1.0, 9) + [(100.0, 100.0), (100.1, 100.0), (0.0, 100.0), (100.0, 0.0)]
    half, why = us.quoted_half_spread_bps(noisy)
    assert why is None and half == pytest.approx(1.0, rel=1e-3)


@pytest.mark.parametrize(
    "quotes, why",
    [
        ([], "no_quotes"),
        ([(100.0, 100.0), (100.2, 100.1)], "locked_or_crossed_only"),
        ([(0.0, 100.0)], "no_two_sided_quote"),
        (None, "not_read"),
    ],
)
def test_name_with_no_usable_quote_carries_its_reason_and_never_reads_zero(quotes, why):
    failures, cost = us.check_cost(quotes, {"atr_pct": 2.0}, TH)
    assert failures == []  # never silently dropped
    assert cost == {"cost_unmeasured": f"cost_unmeasured:{why}"}
    assert "half_spread_bps" not in cost  # never zero
    score = us.second_filter_score({"atr_pct": 2.0, **cost}, INPUTS)
    assert score["k"] is None and score["reason"] == f"cost_unmeasured:{why}"


def test_unmeasured_name_does_not_outrank_measured_names(tmp_path):
    rows = [
        _passer("ZZZA", []),  # alphabetically first, no quotes
        _passer("ZZZB", _quotes(50.0, 30.0)),  # wide but measured
        _passer("ZZZC", _quotes(50.0, 1.0)),
    ]
    ranked, details = _rank(tmp_path, rows)
    assert ranked == ["ZZZC", "ZZZB"]
    assert details["ZZZA"]["k"] is None
    assert details["ZZZA"]["score_reason"] == "cost_unmeasured:no_quotes"


def test_two_names_with_different_spreads_do_not_tie(tmp_path):
    # 0.04 bps apart: inside the old 0.1 bps rounding, so it must still order.
    rows = [_passer("ZZZD", _quotes(80.0, 1.04)), _passer("ZZZE", _quotes(80.0, 1.0))]
    ks = [r["second_filter"]["k"] for r in rows]
    assert ks[0] != ks[1]
    ranked, _ = _rank(tmp_path, rows)
    assert ranked == ["ZZZE", "ZZZD"]  # tighter first, not alphabetical


def test_cost_window_ends_at_the_calendar_close_not_a_fixed_clock():
    # An early-close day (13:00 ET = 17:00 UTC): the window must end there.
    close = datetime(2026, 11, 27, 17, 0, tzinfo=timezone.utc)
    asked = {}

    class _Broker:
        def get_session_close(self, day):
            asked["day"] = day
            return close

        def get_quotes_batch(self, chunk, start, end, on_page=None):
            asked["window"] = (start, end)
            return {s: _quotes(20.0, 2.0) for s in chunk}

    read = us.quotes_reader(_Broker(), date(2026, 11, 27), 3)
    got = read(["ZZZF"])
    assert asked["day"] == date(2026, 11, 27)
    assert asked["window"] == (close - timedelta(minutes=3), close)
    assert us.quoted_half_spread_bps(got["ZZZF"])[0] == pytest.approx(2.0, rel=1e-3)


def test_no_calendar_close_reads_no_quotes():
    class _Broker:
        def get_session_close(self, day):
            return None

    assert us.quotes_reader(_Broker(), date(2026, 10, 19), 3) is None


def test_the_bar_estimator_is_gone():
    assert not hasattr(us, "corwin_schultz_spread")
    _, measured = us.check_bars([], TH)
    assert "half_spread_bps" not in measured
