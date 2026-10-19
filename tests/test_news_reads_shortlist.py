"""The news seat reads the whole shortlist (2026-10-19 fix).

19 Oct morning: the chart seat rated ~21 names buy, the news seat saw 8. Two
chosen caps did it — 15 names fetched, and 15 per-symbol headlines TOTAL
across all names, newest first — and nothing said why a name had no news.
Doctrine: every specialist looks at the same shortlist, long and short alike.

Pins: every shortlisted name is fetched (held, chart-rated buy OR sell, rest);
one busy name cannot starve another; each name's window starts at the
previous session's close; a name with no headline of its own carries a reason.

Hermetic: ZZZ tickers, no network, no model.
"""

import hashlib
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import src.data.news as news_mod
from src.data.news import NewsDataProvider, NewsItem
from tests.test_screened_names_reach_thesis_seats import _per_symbol_names, _run_morning

SHORTLIST = [f"ZZZ{chr(ord('A') + i)}" for i in range(25)]  # ZZZA..ZZZY
RATINGS = {"ZZZV": "strong_sell", "ZZZW": "buy", "ZZZX": "sell", "ZZZY": "strong_buy"}


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch):
    monkeypatch.setattr(news_mod.time, "sleep", lambda _seconds: None)


def _title(symbol: str, i: int) -> str:
    # Distinct words per headline so the dedup stage keeps them apart.
    words = [hashlib.sha1(f"{symbol}-{i}-{k}".encode()).hexdigest()[:10] for k in range(5)]
    return f"{symbol} " + " ".join(words)


def _item(source: str, title: str, minutes_ago: int, summary: str = "") -> NewsItem:
    published = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
    return NewsItem(title=title, summary=summary, source=source, published=published, link="")


def _provider(per_feed):
    """A provider whose general wires and per-symbol feeds come from `per_feed`
    (source name -> list of items, or an Exception to raise)."""
    provider = NewsDataProvider(feeds={"Wire": "https://example.invalid/wire"})
    calls: list[tuple[str, datetime]] = []

    def fetch(name, url, cutoff):
        calls.append((name, cutoff))
        got = per_feed.get(name, [])
        if isinstance(got, Exception):
            raise got
        return list(got)

    provider._fetch_feed = MagicMock(side_effect=fetch)
    return provider, calls


def test_whole_25_name_shortlist_is_fetched_held_then_chart_rated_then_rest(monkeypatch):
    captured = _run_morning(
        monkeypatch,
        positions=[SimpleNamespace(symbol="zzzz", qty=2)],
        screened=SHORTLIST,
        ratings=RATINGS,
    )
    names = _per_symbol_names(captured)
    assert names[0] == "ZZZZ", names
    # Buy and sell ratings alike, in shortlist order, before the unrated rest.
    assert names[1:5] == ["ZZZV", "ZZZW", "ZZZX", "ZZZY"], names
    assert set(SHORTLIST) <= set(names), sorted(set(SHORTLIST) - set(names))

    provider, calls = _provider({})
    provider.fetch_news(symbols=captured["held"] + captured["candidates"])
    fetched = [n for n, _ in calls if n.startswith("Yahoo Finance (")]
    assert fetched == [f"Yahoo Finance ({s})" for s in names]
    assert len(fetched) >= 1 + len(SHORTLIST)


def test_a_name_with_20_recent_headlines_does_not_starve_the_others():
    feeds = {
        "Wire": [_item("Wire", _title("WIRE", i), minutes_ago=1) for i in range(60)],
        "Yahoo Finance (ZZZA)": [_item("Yahoo Finance (ZZZA)", _title("ZZZA", i), minutes_ago=2) for i in range(20)],
    }
    for sym in ("ZZZB", "ZZZC", "ZZZD"):
        feeds[f"Yahoo Finance ({sym})"] = [
            _item(f"Yahoo Finance ({sym})", _title(sym, i), minutes_ago=30) for i in range(3)
        ]
    provider, _ = _provider(feeds)
    items, coverage = provider.fetch_news(symbols=["ZZZA", "ZZZB", "ZZZC", "ZZZD"])
    text = provider.format_for_prompt(items, max_items=50)
    for i in range(20):
        assert _title("ZZZA", i) in text, f"ZZZA headline {i} cut"
    for sym in ("ZZZB", "ZZZC", "ZZZD"):
        for i in range(3):
            assert _title(sym, i) in text, f"{sym} headline {i} starved"
    # General wires keep their own bound.
    assert sum(1 for i in range(60) if _title("WIRE", i) in text) == 50
    assert getattr(coverage, "per_symbol_gaps", None) == {}


def test_each_names_window_starts_at_the_previous_session_close(monkeypatch):
    today = date(2026, 10, 20)  # a Tuesday
    monkeypatch.setattr(news_mod, "et_now", lambda: datetime(2026, 10, 20, 8, 0, tzinfo=timezone.utc))
    prev_close = datetime(2026, 10, 19, 20, 0, tzinfo=timezone.utc)
    broker = SimpleNamespace(
        get_session_open=lambda d: (
            prev_close - timedelta(hours=6, minutes=30) if d == today - timedelta(days=1) else None
        ),
        get_session_close=lambda d: prev_close if d == today - timedelta(days=1) else None,
    )
    provider, calls = _provider({})
    provider.session_broker = lambda: broker
    provider.fetch_news(symbols=["ZZZA"])
    cutoffs = {name: cutoff for name, cutoff in calls}
    assert cutoffs["Yahoo Finance (ZZZA)"] == prev_close


def test_a_name_with_no_headline_of_its_own_carries_a_recorded_reason():
    shared = "ZZZM wins a large contract from a federal agency today"
    feeds = {
        "Wire": [_item("Wire", shared, minutes_ago=5, summary="long wire summary " * 5)],
        "Yahoo Finance (ZZZK)": [_item("Yahoo Finance (ZZZK)", _title("ZZZK", 0), minutes_ago=5)],
        "Yahoo Finance (ZZZF)": RuntimeError("HTTP Error 503"),
        "Yahoo Finance (ZZZN)": [],
        "Yahoo Finance (ZZZM)": [_item("Yahoo Finance (ZZZM)", shared, minutes_ago=6)],
    }
    provider, _ = _provider(feeds)
    _items, coverage = provider.fetch_news(symbols=["ZZZK", "ZZZF", "ZZZN", "ZZZM"])
    gaps = getattr(coverage, "per_symbol_gaps", {})
    assert "ZZZK" not in gaps
    assert gaps.get("ZZZF", "").startswith("fetch failed: HTTP Error 503"), gaps
    assert gaps.get("ZZZN", "").startswith("no headlines since"), gaps
    assert gaps.get("ZZZM", "").startswith("headlines merged"), gaps
    # The seat reads these: they are in the coverage line put in its prompt.
    described = coverage.describe()
    for sym in ("ZZZF", "ZZZN", "ZZZM"):
        assert f"{sym}: " in described
