"""Daily record-only universe screen (src/universe_daily.py). Alpaca, Yahoo
and SEC are all fakes: nothing here touches the network."""

from __future__ import annotations

import json
from src import universe_daily as ud
from tests.test_universe_screen import GOOD_ASSET, TH, TODAY, _bars, _good_bars

PROFILE = {"market_cap_usd": 5e9, "sector": "Industrials", "quote_type": "EQUITY"}


def _asset(symbol, **over):
    return {**GOOD_ASSET, "symbol": symbol, "name": f"{symbol} Corp. Common Stock", **over}


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def _run(
    assets,
    tmp_path,
    *,
    bars=None,
    profile=PROFILE,
    filings=(),
    deadline=1e9,
    clock=None,
    cache=None,
    batch=2,
    after_bars=None,
    yahoo=None,
    get_role=None,
    get_filings_override=None,
):
    calls = {"bars": [], "profile": [], "filings": [], "yahoo": []}

    def get_bars(chunk):
        if after_bars is not None:
            after_bars()
        calls["bars"].append(list(chunk))
        return {s: (bars or {}).get(s, _good_bars()) for s in chunk}

    def get_profile(s):
        calls["profile"].append(s)
        return profile(s) if callable(profile) else profile

    def get_filings(s):
        calls["filings"].append(s)
        return list(filings)

    run = ud.run_daily_record(
        assets,
        get_bars_batch=get_bars,
        get_profile=get_profile,
        get_filings=get_filings_override or get_filings,
        get_role=get_role or (lambda ref: "target"),
        th=TH,
        today=TODAY,
        deadline=deadline,
        batch_size=batch,
        cache=cache or ud.ReadCache(tmp_path),
        clock=clock or Clock(),
        get_yahoo=None if yahoo is None else (lambda s: calls["yahoo"].append(s) or yahoo(s)),
    )
    return run, calls


def test_every_name_gets_a_record_with_its_failing_check(tmp_path):
    assets = [
        _asset("GOOD"),
        _asset("DEAD", status="inactive"),
        _asset("PENY"),
        _asset("TINY"),
        _asset("DEAL"),
    ]
    run, calls = _run(
        assets,
        tmp_path,
        bars={"PENY": _bars(300, price=2.0, rng=0.0005)},
        profile=lambda s: {**PROFILE, "market_cap_usd": 1e6} if s == "TINY" else PROFILE,
        filings=[],
    )
    rec = run.records
    assert set(rec) == {"GOOD", "DEAD", "PENY", "TINY", "DEAL"}
    assert rec["GOOD"]["status"] == "passed"
    assert rec["DEAD"] == {"symbol": "DEAD", "status": "dropped", "failures": ["asset_inactive"], "measured": {}}
    assert rec["PENY"]["failures"] == ["price_below_minimum"]
    assert rec["TINY"]["failures"] == ["company_too_small"]
    # The dead name never cost a bar read; bars came many symbols per request.
    assert "DEAD" not in sum(calls["bars"], [])
    assert max(len(c) for c in calls["bars"]) == 2
    summary = run.summary()
    assert summary["names_listed"] == 5
    assert summary["passed"] == 2
    assert summary["dropped_by_first_reason"] == {
        "asset_inactive": 1,
        "company_too_small": 1,
        "price_below_minimum": 1,
    }
    # Every name here has a Nasdaq sector, so the bounded Yahoo second pass
    # (funds with no category, stocks with no sector) is due for none of them.
    assert summary["yahoo_second_pass"] == {"names": 0, "calls": 0, "failures": 0, "cache_hits": 0}


def test_pending_takeover_is_recorded(tmp_path):
    run, _ = _run(
        [_asset("DEAL")],
        tmp_path,
        filings=[("DEFM14A", "2026-09-01", "", {"cik": "1", "accession": "0000000000-26-000001"})],
    )
    assert run.records["DEAL"]["failures"] == ["pending_takeover"]


def test_bar_outage_is_inconclusive_not_dropped(tmp_path):
    def boom(chunk):
        raise RuntimeError("alpaca down")

    run = ud.run_daily_record(
        [_asset("AAA")],
        get_bars_batch=boom,
        get_profile=lambda s: PROFILE,
        get_filings=lambda s: [],
        th=TH,
        today=TODAY,
        deadline=1e9,
        batch_size=50,
        cache=ud.ReadCache(tmp_path),
        clock=Clock(),
    )
    assert run.records["AAA"]["status"] == "inconclusive"
    assert run.summary()["dropped"] == 0


def test_time_limit_marks_every_unreached_name(tmp_path):
    clock = Clock()
    seen = []

    def get_profile(s):
        seen.append(s)
        clock.now = 10.0  # the first network read uses up the budget
        return PROFILE

    assets = [_asset(s) for s in ("AAA", "BBB", "CCC")]
    run, _ = _run(assets, tmp_path, profile=get_profile, deadline=5.0, clock=clock)
    statuses = {s: r["status"] for s, r in run.records.items()}
    assert statuses["AAA"] == "passed"
    assert statuses["BBB"] == statuses["CCC"] == "unreached"
    assert run.records["BBB"]["reason"] == "unreached: time limit"
    assert run.summary()["unreached"] == 2
    assert run.deadline_hit


def test_filings_are_cached_within_the_week_and_profile_never_is(tmp_path):
    cache = ud.ReadCache(tmp_path)
    _, first = _run([_asset("AAA")], tmp_path, cache=cache)
    cache.save()
    assert "profile" not in json.loads((tmp_path / "daily_cache.json").read_text())
    run, second = _run([_asset("AAA")], tmp_path, cache=ud.ReadCache(tmp_path))
    assert first["profile"] == ["AAA"] and first["filings"] == ["AAA"]
    assert second["profile"] == ["AAA"] and second["filings"] == []
    assert run.records["AAA"]["status"] == "passed"
    assert "profile_read_on" not in run.records["AAA"]


def test_stale_cache_is_used_after_the_time_limit(tmp_path):
    cache = ud.ReadCache(tmp_path)
    cache.put("filings", "AAA", [], TODAY.replace(day=1))
    clock = Clock()

    def spend_budget():
        clock.now = 10.0

    run, calls = _run([_asset("AAA")], tmp_path, cache=cache, deadline=5.0, clock=clock, after_bars=spend_budget)
    assert run.records["AAA"]["status"] == "passed"
    assert run.records["AAA"]["filings_read_on"] == TODAY.replace(day=1).isoformat()
    # Without a cache, the same late name is unreached rather than read.
    clock.now = 0.0
    late, _ = _run([_asset("BBB")], tmp_path, deadline=5.0, clock=clock, after_bars=spend_budget)
    assert late.records["BBB"]["status"] == "unreached"


def test_write_record_is_durable_and_marks_the_day(tmp_path):
    run, _ = _run([_asset("AAA"), _asset("DEAD", tradable=False)], tmp_path)
    assert not ud.already_recorded(tmp_path, TODAY)
    summary = ud.write_record(tmp_path, run)
    assert ud.already_recorded(tmp_path, TODAY)
    lines = (tmp_path / "daily" / f"{TODAY.isoformat()}.jsonl").read_text().splitlines()
    rows = [json.loads(line) for line in lines]
    assert [r["symbol"] for r in rows] == ["AAA", "DEAD"]
    assert rows[1]["failures"] == ["asset_not_tradable"]
    on_disk = json.loads((tmp_path / "daily" / f"{TODAY.isoformat()}.summary.json").read_text())
    assert on_disk["names_listed"] == summary["names_listed"] == 2


class _BrokerStub:
    def __init__(self, fail: bool = False):
        self.fail = fail
        self.chunks: list[list[str]] = []

    def get_bars_batch(self, symbols, lookback_days, on_page=None):
        self.chunks.append(list(symbols))
        if on_page:
            on_page()
        if self.fail:
            raise RuntimeError("429 Too Many Requests")
        return {s: _good_bars() for s in symbols}


class _YahooStub:
    def __init__(self):
        self.bar_downloads = 0

    def get_ohlcv_batch(self, symbols, lookback_days):
        self.bar_downloads += 1
        return {}


def _run_with(broker, tmp_path):
    batches = ud._CountedBatches(broker, 120)
    run = ud.run_daily_record(
        [_asset("AAA"), _asset("BBB"), _asset("CCC")],
        get_bars_batch=batches,
        get_profile=lambda s: PROFILE,
        get_filings=lambda s: [],
        th=TH,
        today=TODAY,
        deadline=1e9,
        batch_size=2,
        cache=ud.ReadCache(tmp_path),
        clock=Clock(),
    )
    return run, batches


def test_run_uses_the_broker_batch_and_makes_no_yahoo_bar_download(tmp_path):
    yahoo, broker = _YahooStub(), _BrokerStub()
    run, batches = _run_with(broker, tmp_path)
    assert broker.chunks == [["AAA", "BBB"], ["CCC"]]
    assert batches.calls == 2
    assert yahoo.bar_downloads == 0
    assert set(run.records) == {"AAA", "BBB", "CCC"}


def test_rate_limited_chunk_is_recorded_unavailable_per_name(tmp_path):
    run, _ = _run_with(_BrokerStub(fail=True), tmp_path)
    for name in ("AAA", "BBB", "CCC"):
        assert run.records[name]["status"] == "inconclusive"
        assert "market_data_unavailable" in run.records[name]["failures"]


# --- Nasdaq listing as the profile source ---------------------------------

from src.data import nasdaq_listing as nl  # noqa: E402

_FIX = __import__("pathlib").Path(__file__).parent / "fixtures" / "nasdaq_listing"


def _fixture_listing():
    def fetch(url, headers):
        return json.loads((_FIX / ("stocks.json" if url == nl.STOCKS_URL else "etf.json")).read_text())

    return nl.load_listing(fetch=fetch)


def _canon(raw):
    return raw or "Unknown"


def test_profile_reader_reads_the_listing_not_the_network():
    reader = ud._profile_reader(_fixture_listing(), _canon)
    aapl = reader("AAPL")
    assert aapl["quote_type"] == "EQUITY" and aapl["market_cap_usd"] == 3e12 and aapl["sector"] == "Technology"
    assert reader("BRK-B")["market_cap_usd"] == 9e11
    assert reader("NOT-LISTED") is None


def test_funds_are_inconclusive_not_admitted_and_not_dropped(tmp_path):
    reader = ud._profile_reader(_fixture_listing(), _canon)
    run, _ = _run([_asset("SPY")], tmp_path, profile=reader)
    row = run.records["SPY"]
    assert row["status"] == "inconclusive" and row["failures"] == ["fund_category_not_listed"]


def test_listing_unavailable_is_inconclusive_and_day_is_not_recorded(tmp_path):
    reader = ud._profile_reader(None, _canon)
    cache = ud.ReadCache(tmp_path)
    run, _ = _run([_asset("AAA"), _asset("BBB")], tmp_path, profile=reader, cache=cache)
    for name in ("AAA", "BBB"):
        assert run.records[name]["status"] == "inconclusive"
        assert run.records[name]["failures"] == ["profile_unavailable"]
    assert "profile" not in cache.data
    run.listing_unavailable = True
    ud.write_record(tmp_path, run)
    assert not ud.already_recorded(tmp_path, TODAY)
    run.listing_unavailable = False
    ud.write_record(tmp_path, run)
    assert ud.already_recorded(tmp_path, TODAY)


# --------------------------------------------------------------------------
# Yahoo gap: bounded second pass (owner ruling 2026-10-10). yfinance is a fake.
# --------------------------------------------------------------------------

FUND = {"quote_type": "ETF", "fund_category_not_listed": True}
NO_SECTOR = {**PROFILE, "sector": "Unknown"}


def _fund_run(tmp_path, info, **kw):
    return _run([_asset("SPY")], tmp_path, profile=FUND, yahoo=info, **kw)


def test_fund_with_large_blend_category_is_admitted(tmp_path):
    run, calls = _fund_run(tmp_path, lambda s: {"category": "Large Blend"})
    row = run.records["SPY"]
    assert row["status"] == "passed" and row["measured"]["sector"] == "Large Blend"
    assert calls["yahoo"] == ["SPY"]
    assert run.summary()["yahoo_second_pass"] == {"names": 1, "calls": 1, "failures": 0, "cache_hits": 0}


def test_fund_with_leveraged_category_is_refused(tmp_path):
    run, _ = _fund_run(tmp_path, lambda s: {"category": "Trading--Leveraged Equity"})
    row = run.records["SPY"]
    assert row["status"] == "dropped" and row["failures"] == ["leveraged_or_inverse_fund"]


def test_yahoo_failure_is_inconclusive_never_dropped_or_admitted(tmp_path):
    def boom(s):
        raise RuntimeError("yahoo down")

    for info in (boom, lambda s: {}, lambda s: {"category": "  "}):
        run, _ = _fund_run(tmp_path / str(id(info)), info)
        row = run.records["SPY"]
        assert row["status"] == "inconclusive" and row["failures"] == ["profile_unavailable"]
    run, _ = _run([_asset("BRKB")], tmp_path / "stock", profile=NO_SECTOR, yahoo=boom)
    assert run.records["BRKB"]["status"] == "inconclusive"
    # A raised read keeps Yahoo's own error in the record, not just in a log line.
    assert run.records["BRKB"]["reason"] == "Yahoo read failed: RuntimeError: yahoo down"
    assert run.records["BRKB"]["failures"] == ["profile_unavailable"]
    assert run.summary()["yahoo_second_pass"]["failures"] == 1


def test_first_rate_limit_stops_every_further_yahoo_read(tmp_path):
    class YFRateLimitError(Exception):
        pass

    def limited(s):
        raise YFRateLimitError("Too Many Requests. Rate limited.")

    assets = [_asset("SPY"), _asset("QQQ"), _asset("IWM")]
    run, calls = _run(assets, tmp_path, profile=FUND, yahoo=limited)
    assert calls["yahoo"] == ["IWM"]  # one call, no retry, nothing after it
    for name in ("SPY", "QQQ", "IWM"):
        row = run.records[name]
        assert row["status"] == "unreached" and row["failures"] == [ud.YAHOO_LIMITED]
    summary = run.summary()
    assert summary["passed"] == 0 and summary["dropped"] == 0 and summary["unreached"] == 3
    assert summary["yahoo_second_pass"] == {"names": 3, "calls": 1, "failures": 1, "cache_hits": 0}


def test_unknown_nasdaq_sector_gets_its_yahoo_sector(tmp_path):
    run, calls = _run([_asset("BRKB")], tmp_path, profile=NO_SECTOR, yahoo=lambda s: {"sector": "Financial Services"})
    row = run.records["BRKB"]
    assert row["status"] == "passed" and row["measured"]["sector"] == "Financial Services"
    assert calls["filings"] == ["BRKB"]  # the takeover check still ran


def test_no_yahoo_call_for_a_name_that_failed_another_check(tmp_path):
    assets = [_asset("PENY"), _asset("TINY"), _asset("DEAL")]
    run, calls = _run(
        assets,
        tmp_path,
        bars={"PENY": _bars(300, price=2.0, rng=0.0005)},
        profile=lambda s: {**NO_SECTOR, "market_cap_usd": 1e6} if s == "TINY" else NO_SECTOR,
        filings=[("DEFM14A", "2026-09-01", "", {"cik": "1", "accession": "0000000000-26-000001"})],
        yahoo=lambda s: {"sector": "Industrials"},
    )
    assert calls["yahoo"] == []
    assert run.records["DEAL"]["failures"] == ["unresolved_sector", "pending_takeover"]
    assert run.records["DEAL"]["measured"]["sector"] == "Unknown"
    assert run.records["TINY"]["status"] == "dropped"


def test_yahoo_reads_are_cached_for_the_week(tmp_path):
    cache = ud.ReadCache(tmp_path)
    assets = [_asset("SPY"), _asset("QQQ")]
    _, first = _run(assets, tmp_path, profile=FUND, yahoo=lambda s: {"category": "Large Blend"}, cache=cache)
    assert first["yahoo"] == ["QQQ", "SPY"]
    run, second = _run(assets, tmp_path, profile=FUND, yahoo=lambda s: {"category": "Large Blend"}, cache=cache)
    assert second["yahoo"] == []
    assert run.summary()["yahoo_second_pass"] == {"names": 2, "calls": 0, "failures": 0, "cache_hits": 2}
