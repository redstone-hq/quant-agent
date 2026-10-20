"""Universe expansion and pruning (src/universe_screen.py).

One test (or small group) per criterion, the prune state machine, the
weekly incremental runner, the per-run cap, the side-door routing (Form 4
and nominations run the same screen when it is on, and their old gates when
it is off), the restored Form 4 age gate, and the morning message.
"""

from __future__ import annotations

import json
import math
import time
from datetime import date, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from src import universe_screen as us
from src.config import UniverseScreenConfig
from src.models import OHLCV
from tests.pipeline_factory import build_pipeline

TODAY = date(2026, 9, 18)  # a Friday


def _bars(n=300, *, price=50.0, rng=0.01, end=TODAY, close_drift=0.0):
    """n weekday bars ending at `end`, high/low = close*(1 +/- rng)."""
    out = []
    day = end
    while len(out) < n:
        if day.weekday() < 5:
            out.append(day)
        day -= timedelta(days=1)
    out.reverse()
    bars = []
    for i, d in enumerate(out):
        close = price * (1 + close_drift * i)
        bars.append(
            OHLCV(date=d, open=close, high=close * (1 + rng), low=close * (1 - rng), close=close, volume=1_000_000)
        )
    return bars


TH = us.ScreenThresholds(
    min_price_usd=5.0,
    min_market_cap_usd=30_000_000,
    hold_sessions=10,
    max_atr_fraction=1.0 / 2.5,
    min_history_bars=201,
)

GOOD_ASSET = {
    "symbol": "ACME",
    "name": "Acme Corp. Common Stock",
    "status": "active",
    "tradable": True,
    "class": "us_equity",
    "exchange": "NYSE",
    "shortable": True,
    "borrow_status": "easy_to_borrow",
    "easy_to_borrow": True,
}


def _good_bars():
    # A calm stock: zero intraday range, tiny drift -> spread estimate 0.
    return _bars(300, price=50.0, rng=0.0005)


#: A JSON-safe filing locator, as `src.data.sec_filings.filing_rows` emits.
LOCATOR = {"cik": "111111", "accession": "0001193125-26-000001", "primary_doc": "d1.htm", "form": "DEFM14A"}


def _sources(asset=GOOD_ASSET, bars=None, profile=None, filings=(), role=None):
    return us.ScreenSources(
        get_asset=lambda s: asset,
        get_bars=lambda s: _good_bars() if bars is None else bars,
        get_profile=lambda s: profile if profile is not None else {"market_cap_usd": 5e9, "sector": "Industrials"},
        get_filings=lambda s: list(filings) if filings is not None else None,
        get_role=(lambda ref: role) if role is not None else None,
    )


def test_a_clean_stock_passes_every_check():
    result = us.screen_symbol("ACME", _sources(), TH)
    assert result.passed, result.failures
    assert result.measured["last_price"] == pytest.approx(50.0)


# ---------------------------------------------------------------- asset ----


@pytest.mark.parametrize(
    "asset,code",
    [
        (None, "asset_not_found"),
        ({**GOOD_ASSET, "status": "inactive"}, "asset_inactive"),
        ({**GOOD_ASSET, "tradable": False}, "asset_not_tradable"),
    ],
)
def test_delisted_or_halted_is_a_permanent_failure(asset, code):
    result = us.screen_symbol("ACME", _sources(asset=asset), TH)
    assert result.failures == [code]
    assert result.permanent


def test_otc_is_refused():
    result = us.screen_symbol("ACME", _sources(asset={**GOOD_ASSET, "exchange": "OTC"}), TH)
    assert "unsupported_exchange" in result.failures


@pytest.mark.parametrize(
    "symbol,name",
    [
        ("ACMEW", "Acme Acquisition Corp. Warrant"),
        ("ACMEU", "Acme Acquisition Corp. Units"),
        ("ACMER", "Acme Acquisition Corp. Rights"),
        ("ACME.WS", "Acme Corp"),
        ("ACME.U", "Acme Corp"),
        ("ACME.RT", "Acme Corp"),
    ],
)
def test_warrants_units_and_rights_are_refused(symbol, name):
    asset = {**GOOD_ASSET, "symbol": symbol, "name": name}
    assert us.check_asset(symbol, asset) == ["not_common_stock"]


def test_a_company_called_united_is_not_mistaken_for_a_unit():
    asset = {**GOOD_ASSET, "symbol": "FUNC", "name": "First United Corporation Common Stock"}
    assert us.check_asset("FUNC", asset) == []


# --------------------------------------------------------------- borrow ----
# Borrow is not an admission criterion (owner ruling 2026-10-09); see
# tests/test_universe_screen_etf.py.


# -------------------------------------------------------------- history ----


def test_less_than_a_calendar_year_of_bars_is_refused():
    # 240 weekday bars span under a calendar year.
    failures, _ = us.check_bars(_bars(240, rng=0.0005), TH)
    assert "insufficient_history" in failures


def test_a_year_of_bars_with_gaps_below_the_indicator_window_is_refused():
    bars = _bars(300, rng=0.0005)[::2]  # a full year, but only ~150 bars
    failures, _ = us.check_bars(bars, TH)
    assert "insufficient_history" in failures


def test_no_bars_at_all():
    assert us.check_bars([], TH)[0] == ["no_price_history"]


# ---------------------------------------------------------------- price ----


def test_penny_stock_is_refused():
    failures, _ = us.check_bars(_bars(300, price=4.99, rng=0.0005), TH)
    assert failures == ["price_below_minimum"]


def test_no_maximum_price():
    failures, _ = us.check_bars(_bars(300, price=4_000.0, rng=0.0005), TH)
    assert failures == []


# --------------------------------------------------------------- spread ----


def _quoted_bars(n, *, spread, vol, seed, price=50.0, trades=40):
    """n daily bars whose every trade prints at bid or ask around a random-walk
    mid: the data-generating process of Ardia, Guidotti & Kroencke (2024)."""
    import random

    rng, mid, out = random.Random(seed), price, []
    for i in range(n):
        prints = []
        for _ in range(trades):
            mid *= math.exp(rng.gauss(0, vol / math.sqrt(trades)))
            prints.append(mid * (1 + spread / 2) if rng.random() < 0.5 else mid * (1 - spread / 2))
        out.append(
            OHLCV(
                date=TODAY - timedelta(days=n - i),
                open=prints[0],
                high=max(prints),
                low=min(prints),
                close=prints[-1],
                volume=1_000_000,
            )
        )
    return out


def test_edge_recovers_a_known_spread_on_a_volatile_name():
    spread, reason = us.edge_spread(_quoted_bars(260, spread=0.01, vol=0.03, seed=0))
    assert reason is None
    assert spread == pytest.approx(0.01, rel=0.2)


def test_edge_with_too_few_bars_is_unestimable_not_zero():
    assert us.edge_spread(_quoted_bars(2, spread=0.01, vol=0.03, seed=0)) == (None, "cost_unestimable:too_few_bars")


def test_spread_is_judged_against_the_names_own_expected_move():
    # 1% quoted range -> ~50 bps half-spread: over the retired flat 40 bps
    # line, but its round trip is below the move expected over the hold,
    # so the break-even floor passes it.
    failures, measured = us.check_bars(_quoted_bars(300, spread=0.01, vol=0.03, seed=0), TH)
    assert measured["half_spread_bps"] > 40.0
    assert "spread_exceeds_expected_move" not in failures
    assert "spread_too_wide" not in failures


# ----------------------------------------------------------- volatility ----


def test_volatility_ceiling_refuses_a_name_whose_widest_stop_cannot_be_placed():
    # 25% daily range -> ATR/price ~0.5, past the 1/3.00 ceiling: the
    # desk's widest legitimate stop (3.00 x ATR under price) would sit
    # below zero. The spread check would also fire; the volatility one
    # must be among the failures.
    failures, measured = us.check_bars(_bars(300, rng=0.25), TH)
    assert "volatility_above_ceiling" in failures
    assert measured["atr_pct"] > 100.0 / 3.0


def test_volatility_ceiling_is_the_widest_reachable_stop_not_the_base_one():
    """Board item 185. The ceiling used to be `0.5 / min_stop_atr_multiple`
    = 20%. The ceiling is now 1 / the widest multiple, so the screen and the
    stop rules cannot disagree by construction. Since 2026-10-09 the widest
    multiple IS the owner-ruled 2.5 ATR base: no setup or regime scaler."""
    from src.portfolio_constructor import widest_reachable_stop_atr_multiple

    config = SimpleNamespace(
        universe_screen=UniverseScreenConfig(),
        execution=SimpleNamespace(max_entry_slippage_bps=40.0),
        risk=SimpleNamespace(min_stop_atr_multiple=2.5),
    )
    th = us.ScreenThresholds.from_config(config)
    widest = widest_reachable_stop_atr_multiple(2.5)
    assert widest == pytest.approx(2.5)
    assert th.max_atr_fraction == pytest.approx(1.0 / widest)
    # At the ceiling the widest stop sits at exactly zero, so the ceiling
    # itself must FAIL, not pass.
    assert 1.0 - widest * th.max_atr_fraction == pytest.approx(0.0)


def test_the_screen_ceiling_and_the_midday_clamp_read_the_same_multiple():
    """Board item 185, finding 8. The two sides used to resolve the base stop
    multiple by DIFFERENT routes — the screen straight off
    `config.risk.min_stop_atr_multiple`, the midday clamp off the live
    `ConstructorConfig` that `pipeline.build_constructor_config` builds with
    its own (different) fallback — and so they agreed only because both landed on the class defaults.

    The pipeline now hands the screen the SAME live `ConstructorConfig` the
    clamp reads. This pins that: a divergence has to fail here rather than
    pass silently.
    """
    from src.portfolio_constructor import (
        ConstructorConfig,
        widest_reachable_stop_atr_multiple,
    )

    cfg = ConstructorConfig()
    # What the midday clamp computes, from the live constructor config.
    clamp_multiple = widest_reachable_stop_atr_multiple(cfg.min_stop_atr_multiple)
    config = SimpleNamespace(
        universe_screen=UniverseScreenConfig(),
        execution=SimpleNamespace(max_entry_slippage_bps=40.0),
        risk=SimpleNamespace(min_stop_atr_multiple=cfg.min_stop_atr_multiple),
    )
    th = us.ScreenThresholds.from_config(config, cfg)
    assert th.max_atr_fraction == pytest.approx(1.0 / clamp_multiple)

    # And the wiring is real: both live call sites pass the constructor's
    # own config object, so this cannot be satisfied by coincidence.
    import re
    from pathlib import Path

    # Moved to `src/pipeline_admission.py` by step 6 of
    # docs/PIPELINE_SPLIT_PLAN.md (board item 210).
    src = Path(us.__file__).resolve().parent / "pipeline_admission.py"
    calls = re.findall(
        r"ScreenThresholds\.from_config\(\s*([^)]*?)\)",
        src.read_text(encoding="utf-8"),
    )
    assert calls, "no ScreenThresholds.from_config call found in pipeline_admission.py"
    for call in calls:
        assert "_constructor_cfg_or_none" in call, call

    # A moved base multiple must move BOTH sides together.
    moved = ConstructorConfig(min_stop_atr_multiple=4.0)
    moved_config = SimpleNamespace(
        universe_screen=UniverseScreenConfig(),
        execution=SimpleNamespace(max_entry_slippage_bps=40.0),
        risk=SimpleNamespace(min_stop_atr_multiple=4.0),
    )
    moved_th = us.ScreenThresholds.from_config(moved_config, moved)
    moved_clamp = widest_reachable_stop_atr_multiple(moved.min_stop_atr_multiple)
    assert moved_clamp == pytest.approx(4.0)
    assert moved_th.max_atr_fraction == pytest.approx(1.0 / moved_clamp)


def test_the_volatility_ceiling_is_not_an_appetite_number():
    """Board item 185 STAYS OPEN. 1/3.00 is the point where the widest
    legitimate stop lands at or below zero — arithmetic non-degeneracy, not
    a statement about how volatile a name this desk will hold. It is far
    past anything the desk has recorded (max observed ATR14/price 8.11%,
    median 2.97% [measured 2026-09-30, n=46 constructor stop lines across
    every retained production log 2026-08-31 to 2026-09-30]), so it has
    never bound and would not. This test exists so that a future reader who
    finds 33.3% does not mistake it for an answered question.
    """
    assert TH.max_atr_fraction == pytest.approx(1.0 / 2.5)
    max_observed_atr_fraction = 0.0811
    assert max_observed_atr_fraction < TH.max_atr_fraction


# ---------------------------------------------------------- size/sector ----


def test_small_company_is_refused():
    result = us.screen_symbol("ACME", _sources(profile={"market_cap_usd": 29e6, "sector": "Industrials"}), TH)
    assert result.failures == ["company_too_small"]


def test_desk_size_floor_refuses_a_micro_cap_and_admits_a_small_cap():
    """Owner 2026-10-18: the first filter removes micro-caps. The desk's own
    floor is the S&P SmallCap 600 addition floor, US$1.2B; a $39M company
    (BNAI on 16 Oct) is refused, a $1.25B one passes."""
    from dataclasses import replace

    th = replace(TH, min_market_cap_usd=UniverseScreenConfig().min_market_cap_usd)
    micro = us.screen_symbol("ACME", _sources(profile={"market_cap_usd": 39e6, "sector": "Technology"}), th)
    small = us.screen_symbol("ACME", _sources(profile={"market_cap_usd": 1.25e9, "sector": "Technology"}), th)
    assert micro.failures == ["company_too_small"]
    assert small.passed


def test_unknown_company_size_is_refused():
    result = us.screen_symbol("ACME", _sources(profile={"market_cap_usd": None, "sector": "Industrials"}), TH)
    assert result.failures == ["market_cap_unknown"]


def test_unresolved_sector_is_refused():
    result = us.screen_symbol("ACME", _sources(profile={"market_cap_usd": 1e9, "sector": "Unknown"}), TH)
    assert result.failures == ["unresolved_sector"]


def test_unreadable_profile_is_inconclusive_not_a_failure():
    sources = _sources()
    sources.get_profile = lambda s: None
    result = us.screen_symbol("ACME", sources, TH)
    assert result.inconclusive


# ------------------------------------------------------------- takeover ----


def test_pending_takeover_is_refused():
    filings = [("DEFM14A", "2026-08-01", "", LOCATOR), ("8-K", "2026-07-01", "1.01,9.01")]
    result = us.screen_symbol("ACME", _sources(filings=filings, role="target"), TH)
    assert result.failures == ["pending_takeover"]


def test_a_terminated_deal_is_no_longer_pending():
    filings = [("SC 14D9/A", "2026-06-01", ""), ("8-K", "2026-07-15", "1.02")]
    assert us.check_takeover(filings) == ([], {})


def test_no_sec_issuer_means_takeover_status_unknown():
    result = us.screen_symbol("ACME", _sources(filings=None), TH)
    assert result.failures == ["takeover_status_unknown"]


def test_no_earnings_date_requirement_exists():
    # Nothing in the screen reads an earnings date; a clean stock with no
    # earnings information at all passes.
    assert "earnings" not in " ".join(us.PLAIN_REASON)
    assert us.screen_symbol("ACME", _sources(), TH).passed


# ----------------------------------------------------------- thresholds ----


def test_thresholds_are_read_from_existing_desk_numbers():
    config = SimpleNamespace(
        universe_screen=UniverseScreenConfig(),
        execution=SimpleNamespace(max_entry_slippage_bps=40.0),
        risk=SimpleNamespace(min_stop_atr_multiple=2.5),
    )
    th = us.ScreenThresholds.from_config(config)
    assert th.min_price_usd == 5.0
    assert th.min_market_cap_usd == 1_200_000_000
    assert th.hold_sessions == 10
    assert th.max_atr_fraction == pytest.approx(1.0 / 2.5)
    assert th.min_history_bars == 201


def test_screen_code_default_is_off_and_deployed_on():
    # Code default stays off; the deployed setting is ON since the owner's
    # 2026-10-14 approval of the morning score-order send.
    assert UniverseScreenConfig().enabled is False
    import yaml
    from pathlib import Path

    settings = yaml.safe_load((Path(__file__).resolve().parents[1] / "config" / "settings.yaml").read_text())
    assert settings["universe_screen"]["enabled"] is True


# -------------------------------------------------------- state machine ----


def _fail(symbol="ACME", code="spread_too_wide"):
    return us.ScreenResult(symbol=symbol, failures=[code])


def _pass(symbol="ACME"):
    return us.ScreenResult(symbol=symbol, failures=[], measured={"last_price": 50})


def test_candidate_that_passes_is_added():
    state = us.empty_state()
    event = us.apply_result(state, _pass(), today=TODAY, held=set())
    assert event["action"] == "added"
    assert state["admitted"]["ACME"]["status"] == "active"


def test_fail_once_flags_fail_twice_consecutively_removes():
    state = us.empty_state()
    us.apply_result(state, _pass(), today=TODAY - timedelta(days=14), held=set())
    assert us.apply_result(state, _fail(), today=TODAY - timedelta(days=7), held=set())["action"] == "flagged"
    assert state["admitted"]["ACME"]["status"] == "flagged"
    assert us.apply_result(state, _fail(), today=TODAY, held=set())["action"] == "removed"
    assert "ACME" not in state["admitted"]
    assert state["removed"]["ACME"]["reason"] == "spread_too_wide"


def test_a_second_failure_in_the_same_week_does_not_count():
    state = us.empty_state()
    us.apply_result(state, _pass(), today=TODAY - timedelta(days=14), held=set())
    us.apply_result(state, _fail(), today=TODAY - timedelta(days=1), held=set())
    assert us.apply_result(state, _fail(), today=TODAY, held=set()) is None
    assert state["admitted"]["ACME"]["status"] == "flagged"


def test_a_pass_between_failures_clears_the_flag():
    state = us.empty_state()
    us.apply_result(state, _pass(), today=TODAY - timedelta(days=21), held=set())
    us.apply_result(state, _fail(), today=TODAY - timedelta(days=14), held=set())
    assert us.apply_result(state, _pass(), today=TODAY - timedelta(days=7), held=set())["action"] == "cleared"
    assert us.apply_result(state, _fail(), today=TODAY, held=set())["action"] == "flagged"
    assert "ACME" in state["admitted"]


def test_a_held_name_is_never_removed():
    state = us.empty_state()
    us.apply_result(state, _pass(), today=TODAY - timedelta(days=14), held=set())
    us.apply_result(state, _fail(), today=TODAY - timedelta(days=7), held=set())
    event = us.apply_result(state, _fail(), today=TODAY, held={"ACME"})
    assert event["action"] == "removal_deferred_held"
    assert "ACME" in state["admitted"]


def test_delisted_is_removed_immediately_without_a_flag():
    state = us.empty_state()
    us.apply_result(state, _pass(), today=TODAY - timedelta(days=1), held=set())
    event = us.apply_result(state, _fail(code="asset_not_found"), today=TODAY, held=set())
    assert event["action"] == "removed"
    assert "ACME" not in state["admitted"]


def test_delisted_but_held_is_kept_and_reported():
    state = us.empty_state()
    us.apply_result(state, _pass(), today=TODAY - timedelta(days=1), held=set())
    event = us.apply_result(state, _fail(code="asset_not_tradable"), today=TODAY, held={"ACME"})
    assert event["action"] == "removal_deferred_held"
    assert "ACME" in state["admitted"]


def test_an_unreadable_screen_changes_nothing():
    state = us.empty_state()
    us.apply_result(state, _pass(), today=TODAY - timedelta(days=14), held=set())
    before = json.dumps(state, sort_keys=True)
    assert us.apply_result(state, _fail(code="market_data_unavailable"), today=TODAY, held=set()) is None
    assert json.dumps(state, sort_keys=True) == before


def test_store_round_trips(tmp_path):
    store = us.UniverseStore(tmp_path)
    state = us.empty_state()
    us.apply_result(state, _pass(), today=TODAY, held=set())
    store.save(state)
    assert store.load()["admitted"]["ACME"]["admitted_on"] == TODAY.isoformat()


# ------------------------------------------------------------ run_screen ----


def _run(state, assets, *, sources=None, configured=(), held=(), confirm=None, deadline=None, today=TODAY, bars=None):
    return us.run_screen(
        state,
        assets=assets,
        sources=sources or _sources(),
        get_bars_batch=lambda chunk: {s: (bars or _good_bars()) for s in chunk},
        th=TH,
        today=today,
        held=held,
        configured=configured,
        deadline=deadline if deadline is not None else time.monotonic() + 60,
        batch_size=2,
        confirm_missing_asset=confirm,
    )


def _asset(symbol, **kw):
    return {**GOOD_ASSET, "symbol": symbol, **kw}


def test_run_admits_passing_candidates_and_never_screens_configured_ones():
    state = us.empty_state()
    run = _run(state, [_asset("AAA"), _asset("SPY"), _asset("BBB", exchange="OTC")], configured=["SPY"])
    assert set(state["admitted"]) == {"AAA"}
    assert "SPY" not in state["screened"] and "SPY" not in state["admitted"]
    assert state["screened"]["BBB"]["failures"] == ["unsupported_exchange"]
    assert [e["action"] for e in run.events] == ["added"]


def test_run_screens_each_symbol_once_per_week():
    state = us.empty_state()
    _run(state, [_asset("AAA")])
    calls = []
    sources = _sources()
    original = sources.get_profile
    sources.get_profile = lambda s: calls.append(s) or original(s)
    _run(state, [_asset("AAA")], sources=sources, today=TODAY)
    assert calls == []
    _run(state, [_asset("AAA")], sources=sources, today=TODAY + timedelta(days=7))
    assert calls == ["AAA"]


def test_absence_from_the_list_alone_never_removes():
    state = us.empty_state()
    _run(state, [_asset("AAA")])
    _run(state, [], today=TODAY + timedelta(days=1))
    assert "AAA" in state["admitted"]


def test_confirmed_delisting_removes_on_the_next_run_not_the_next_week():
    state = us.empty_state()
    _run(state, [_asset("AAA")])
    run = _run(state, [], confirm=lambda s: None, today=TODAY + timedelta(days=1))
    assert "AAA" not in state["admitted"]
    assert run.events[0]["action"] == "removed"
    assert run.events[0]["reasons"] == ["asset_not_found"]


def test_the_deadline_stops_the_pass_and_the_rest_resume_later():
    state = us.empty_state()
    run = _run(state, [_asset("AAA"), _asset("BBB")], deadline=time.monotonic() - 1)
    assert run.deadline_hit
    assert state["admitted"] == {}
    _run(state, [_asset("AAA"), _asset("BBB")])
    assert set(state["admitted"]) == {"AAA", "BBB"}


def test_a_failed_bar_batch_is_inconclusive():
    state = us.empty_state()
    run = us.run_screen(
        state,
        assets=[_asset("AAA")],
        sources=_sources(),
        get_bars_batch=lambda chunk: (_ for _ in ()).throw(RuntimeError("down")),
        th=TH,
        today=TODAY,
        held=(),
        configured=(),
        deadline=time.monotonic() + 60,
        batch_size=5,
    )
    assert run.inconclusive == 1
    assert state["admitted"] == {} and "AAA" not in state["screened"]


# ------------------------------------------------------- pipeline wiring ----


def _fake_listing(**records):
    from decimal import Decimal

    from src.data.nasdaq_listing import Listing, ListingRecord

    base = {"ACME": ListingRecord("ACME", "Acme", Decimal("5e9"), False, "Industrials")}
    base.update(records)
    return Listing({k: v for k, v in base.items() if v is not None})


@pytest.fixture(autouse=True)
def _side_door_listing(monkeypatch):
    """The side door reads size, sector and fund type from the Nasdaq listing,
    never the network in a test: one stand-in listing per test. A fund's
    Yahoo category read answers blank (the fund stays undecided)."""
    monkeypatch.setattr("src.data.nasdaq_listing.load_listing", lambda *, fetch: _fake_listing())
    monkeypatch.setattr("src.universe_daily._yahoo_reader", lambda: lambda symbol: {})


def _pipeline(tmp_path, *, enabled=True):
    pipeline = build_pipeline(broker=MagicMock(), market=MagicMock(), sec_form4_provider=MagicMock())
    pipeline.config = SimpleNamespace(
        trading=SimpleNamespace(universe=["SPY"], lookback_days=120),
        smart_money=SimpleNamespace(
            max_external_candidates=3,
            min_external_history_days=20,
            min_external_price_usd=5.0,
            min_external_avg_dollar_volume_usd=10_000_000,
            request_timeout_s=15,
        ),
        universe_screen=UniverseScreenConfig(enabled=enabled, data_dir=str(tmp_path)),
        execution=SimpleNamespace(max_entry_slippage_bps=40.0),
        risk=SimpleNamespace(min_stop_atr_multiple=2.5, max_target_horizon_sessions=60),
        nominations=SimpleNamespace(max_per_seat_per_run=3),
    )
    # Item 165: sessions_held now comes from `broker.trading_sessions_held`
    # (holiday-aware). None of these tests span a market holiday, so
    # delegating the mock to the real weekday counter reproduces the same
    # numbers as the plain weekday count used to give directly.
    from src.trading_calendar import trading_sessions_held as _weekday_sessions_held

    pipeline.broker.trading_sessions_held.side_effect = _weekday_sessions_held
    pipeline.broker.get_asset_record.return_value = GOOD_ASSET
    pipeline.broker.get_transient_equity_eligibility.return_value = {
        "eligible": True,
        "reason": "eligible",
        "name": "Acme",
        "exchange": "nyse",
    }
    pipeline.market.get_ohlcv.return_value = _good_bars()
    pipeline.market.get_company_profile.return_value = {
        "market_cap_usd": 5e9,
        "sector_raw": "Industrials",
        "quote_type": "EQUITY",
    }
    pipeline.sec_form4_provider.listed_map.return_value = {"111111": {"ACME": "NYSE"}}
    pipeline.sec_form4_provider._get.return_value.json.return_value = {}  # no filings at all
    return pipeline


# EDGAR header of a tender offer in which ACME (CIK 111111) is the target.
_TENDER_HEADER = "SUBJECT COMPANY:\n\tCENTRAL INDEX KEY: 0000111111\nFILED BY:\n\tCENTRAL INDEX KEY: 0000222222\n"
_SUBMISSIONS = {
    "filings": {
        "recent": {
            "form": ["SC 14D9"],
            "filingDate": ["2026-09-01"],
            "items": [""],
            "accessionNumber": ["0001193125-26-000001"],
            "primaryDocument": ["d1.htm"],
        }
    }
}


def _sec_get(url, *, params, deadline):
    out = MagicMock()
    out.json.return_value = _SUBMISSIONS
    out.text = _TENDER_HEADER
    return out


def test_side_door_runs_the_screen_when_on(tmp_path, monkeypatch):
    pipeline = _pipeline(tmp_path)
    monkeypatch.setattr("src.pipeline_admission._get_sector", lambda s: "Industrials")
    pipeline.sec_form4_provider._get.side_effect = _sec_get  # ACME is the SUBJECT COMPANY
    ok, reason, details = pipeline.admission._evaluate_external_admission_gates("ACME")
    assert (ok, reason) == (False, "pending_takeover")
    pipeline.broker.get_transient_equity_eligibility.assert_not_called()


def test_side_door_reads_company_size_from_the_listing_not_yahoo(tmp_path, monkeypatch):
    """Yahoo says $5B; the listing has no size: the listing decides, so the
    name is dropped as size unknown and Yahoo is never asked."""
    from src.data.nasdaq_listing import ListingRecord

    no_size = ListingRecord("ACME", "Acme", None, False, "Industrials")
    monkeypatch.setattr("src.data.nasdaq_listing.load_listing", lambda *, fetch: _fake_listing(ACME=no_size))
    pipeline = _pipeline(tmp_path)
    ok, reason, _ = pipeline.admission._evaluate_external_admission_gates("ACME")
    assert ok is False
    assert reason == "market_cap_unknown"
    pipeline.market.get_company_profile.assert_not_called()


def test_fund_name_backstop_refuses_and_never_admits():
    """A leveraged or inverse NAME is refused before its category is known;
    a plain name decides nothing (no category stays inconclusive)."""
    missing = {"quote_type": "ETF", "fund_category_not_listed": True}
    for name in (
        "ProShares UltraPro QQQ",
        "ProShares UltraShort S&P500",
        "Direxion Daily Semiconductor Bull 3X Shares",
        "Direxion Daily S&P 500 Bear 1X Shares",
        "ProShares Short QQQ -1X",
        "MicroSectors FANG+ 2X Leveraged ETN",
        "AXS 1.5X PYPL Bull Daily ETF",
        "Tuttle Capital Daily Inverse Cramer",
        "Daily Leveraged Equity Shares",
    ):
        failures, measured = us.check_profile(missing, TH, name)
        assert failures == ["leveraged_or_inverse_fund"], name
        assert measured["leveraged_by_name"] is True
    for name in ("SPDR S&P 500 ETF Trust", "Invesco QQQ Trust", "iShares Russell 2000 ETF", "Daily Journal Corp"):
        assert us.check_profile(missing, TH, name) == (["fund_category_not_listed"], {}), name
    # The listing's own name is checked too (the broker name may be terse).
    assert us.check_profile({**missing, "name": "Bull 3X Shares"}, TH, "TQQQ")[0] == ["leveraged_or_inverse_fund"]
    # A stored plain category passes a plain-named fund.
    failures, measured = us.check_profile({"quote_type": "ETF", "category": "Large Blend"}, TH, "SPDR S&P 500 ETF")
    assert failures == [] and measured["sector"] == "Large Blend"


def test_side_door_keeps_the_fund_flag(tmp_path, monkeypatch):
    """A fund reaching the side door is judged as a fund, never dropped as a
    stock of unknown size."""
    from src.data.nasdaq_listing import ListingRecord

    fund = ListingRecord("ACME", "Acme Fund", None, True, None)
    monkeypatch.setattr("src.data.nasdaq_listing.load_listing", lambda *, fetch: _fake_listing(ACME=fund))
    pipeline = _pipeline(tmp_path)
    ok, reason, _ = pipeline.admission._evaluate_external_admission_gates("ACME")
    assert ok is False
    assert reason == "fund_category_not_listed"


def test_nomination_door_uses_the_screen(tmp_path, monkeypatch):
    pipeline = _pipeline(tmp_path)
    monkeypatch.setattr("src.pipeline_admission._get_sector", lambda s: "Industrials")
    pipeline.broker.get_asset_record.return_value = {**GOOD_ASSET, "exchange": "OTC"}
    admitted, details = pipeline.admission._admit_nominated_external_symbols(["acme"])
    assert admitted == set()
    pipeline.broker.get_asset_record.return_value = GOOD_ASSET
    admitted, details = pipeline.admission._admit_nominated_external_symbols(["acme"])
    assert admitted == {"ACME"}
    assert details["ACME"]["screen"] == "universe_screen"


def _purchase(days_ago):
    from src.util.time import et_today

    return SimpleNamespace(
        symbol="ACME",
        transaction_code="P",
        admission_eligible=True,
        transaction_value_usd=500_000,
        accession_number="0001-26-000001",
        actor="Director",
        known_at="2026-09-01T12:00:00Z",
        disclosure_date=et_today() - timedelta(days=days_ago),
    )


def test_form4_door_runs_the_screen_and_its_age_gate(tmp_path, monkeypatch):
    pipeline = _pipeline(tmp_path)
    monkeypatch.setattr("src.pipeline_admission._get_sector", lambda s: "Industrials")
    admitted, details = pipeline.admission._admit_transient_smart_money_symbols([_purchase(10)])
    assert admitted == {"ACME"}
    assert details["ACME"]["screen"] == "universe_screen"
    # A purchase disclosed ~a year ago (the RSG case) no longer admits.
    assert pipeline.admission._admit_transient_smart_money_symbols([_purchase(364)])[0] == set()


def test_screened_universe_is_empty_when_off(tmp_path):
    pipeline = _pipeline(tmp_path, enabled=False)
    state = us.empty_state()
    us.apply_result(state, _pass("AAA"), today=TODAY, held=set())
    us.UniverseStore(tmp_path).save(state)
    assert pipeline.admission._admit_screened_universe_symbols([]) == ([], {})


def test_evening_pass_records_changes_and_morning_shows_them_once(tmp_path, monkeypatch):
    pipeline = _pipeline(tmp_path)
    pipeline.db = MagicMock()
    pipeline.broker.list_assets.return_value = [GOOD_ASSET]
    pipeline.broker.get_positions.return_value = []
    pipeline.market.get_ohlcv_batch.side_effect = lambda chunk, days: {s: _good_bars() for s in chunk}
    monkeypatch.setattr("src.pipeline_admission._get_sector", lambda s: "Industrials")
    summary = pipeline.admission._run_universe_screen("evening-x")
    assert summary["passed"] == 1
    kinds = [c.kwargs["kind"] for c in pipeline.db.insert_specialist_evidence.call_args_list]
    assert kinds.count("universe_change") == 1 and "universe_screen_run" in kinds

    result = {}
    pipeline.admission._attach_universe_changes(result)
    assert [e["action"] for e in result["universe_changes"]["events"]] == ["added"]
    again = {}
    pipeline.admission._attach_universe_changes(again)
    assert again["universe_changes"]["events"] == []


def test_morning_message_lists_changes_below_the_pnl_block():
    from src.notifier import format_session_result

    result = {
        "status": "ok",
        "run_id": "morning-x",
        "universe_changes": {
            "events": [
                {"symbol": "AAA", "action": "added", "reasons": ["passed"]},
                {"symbol": "BBB", "action": "removed", "reasons": ["pending_takeover"]},
            ],
            "admitted_count": 1,
            "flagged_count": 0,
        },
    }
    text = format_session_result("morning", result, 12.0)
    assert "Stock list changed" in text
    assert "BBB removed — being taken over" in text
    lines = text.splitlines()
    pnl_at = next(i for i, l in enumerate(lines) if "P&L" in l or "profit" in l.lower())
    universe_at = next(i for i, l in enumerate(lines) if "Stock list changed" in l)
    assert pnl_at < universe_at


def test_morning_message_is_silent_about_the_screen_when_it_is_off():
    from src.notifier import format_session_result

    text = format_session_result("morning", {"status": "ok", "run_id": "m"}, 1.0)
    assert "Stock list" not in text


# ------------------------------------------------------- pipeline wiring ----


def test_screened_universe_comes_from_the_nightly_record_not_the_weekly_rotation(tmp_path):
    # Owner ruling 2026-10-14: the morning reads last night's passers in score
    # order (tests/test_morning_ranked_admission.py); the weekly store's
    # admitted set is no longer rotated three at a time into the session.
    pipeline = _pipeline(tmp_path)
    state = us.empty_state()
    for sym in ("AAA", "BBB", "CCC", "DDD", "EEE"):
        us.apply_result(state, _pass(sym), today=TODAY, held=set())
    us.UniverseStore(tmp_path).save(state)
    pipeline.broker.is_trading_day.return_value = True
    pipeline.broker.get_session_close.return_value = None
    with pytest.raises(FileNotFoundError):
        pipeline.admission._admit_screened_universe_symbols([SimpleNamespace(symbol="EEE")])
