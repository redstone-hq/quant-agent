"""Tests for per-symbol news fetching (2026-08-30 owner decision).

Background: the 2026-08-29 audit (src/data/news.py "CHECKED AND REJECTED"
comment block) verified Yahoo Finance's per-symbol RSS live and working, but
deliberately left it unwired — at the full ~101-symbol trading.universe it
would be 101-202 extra requests/run to a free endpoint with no documented
rate-limit tolerance. The owner has now scoped it: held positions + this
run's admitted candidates only, hard-capped, sharing the exact same
NewsCoverage / dedup machinery the general wire feeds already use (see the
2026-08-28 coverage fix in src/data/news.py and src/data/news_dedup.py).

No network calls anywhere in this file — every fetch is monkeypatched via
NewsDataProvider._fetch_feed, the same convention tests/test_news_coverage.py
already uses.
"""

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import src.data.news as news_mod
from src.data.news import NewsDataProvider, NewsItem
from tests.pipeline_factory import build_pipeline


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch):
    """The per-symbol politeness throttle (_throttle_per_symbol) uses a
    module-level shared clock (_PER_SYMBOL_LAST_REQUEST_AT) so it rate-limits
    across every NewsDataProvider instance in the process — by design (see
    src/data/news.py), but that means a real time.sleep() here would both
    slow this suite down and let one test's timing bleed into the next.
    Neutered for every test in this file; the throttle's own logic (the
    interval math, the lock) is still exercised, just never actually waits."""
    monkeypatch.setattr(news_mod.time, "sleep", lambda _seconds: None)


def _item(title="Headline", source="X", link="", per_symbol=False, published=None):
    return NewsItem(
        title=title,
        summary="",
        source=source,
        published=published or datetime(2026, 8, 30, 12, 0, tzinfo=timezone.utc),
        link=link,
        per_symbol=per_symbol,
    )


# ===========================================================================
# Every name given is fetched (2026-10-19: the 15-name cap was removed — it
# starved the news seat of names the chart seat had rated). The throttle
# bounds the request rate; the run's shortlist bounds the count.
# ===========================================================================


def test_per_symbol_fetch_reads_every_name_given():
    # NewsDataProvider(feeds=...) falls back to the real RSS_FEEDS on a
    # falsy (empty-dict) override — `self.feeds = feeds or RSS_FEEDS` — so
    # these tests count against `provider.feeds` rather than assuming zero
    # general feeds.
    provider = NewsDataProvider()
    calls = []

    def fake_fetch(name, url, cutoff):
        calls.append(name)
        return []

    provider._fetch_feed = MagicMock(side_effect=fake_fetch)
    symbols = [f"SYM{i}" for i in range(40)]

    items, coverage = provider.fetch_news(symbols=symbols)

    per_symbol_calls = [c for c in calls if c.startswith("Yahoo Finance (SYM")]
    assert len(per_symbol_calls) == 40
    assert coverage.configured == len(provider.feeds) + 40


def test_per_symbol_disabled_makes_zero_requests_regardless_of_symbols():
    """The master switch (config.news.per_symbol_enabled) is an
    operator emergency-off."""
    provider = NewsDataProvider(per_symbol_enabled=False)
    provider._fetch_feed = MagicMock(return_value=[])

    items, coverage = provider.fetch_news(symbols=["AAPL", "MSFT"])

    assert provider._fetch_feed.call_count == len(provider.feeds)
    assert coverage.configured == len(provider.feeds)


# ===========================================================================
# Selection order — deterministic, never set-iteration.
# ===========================================================================


def test_per_symbol_fetch_preserves_caller_order():
    """The provider fetches in the caller's own order — it must not
    resort/shuffle them. Ordering
    (positions before candidates) is the caller's responsibility
    (TradingPipeline._run_news_update); the provider must not undo it."""
    provider = NewsDataProvider()
    fetched = []

    def fake_fetch(name, url, cutoff):
        fetched.append(name)
        return []

    provider._fetch_feed = MagicMock(side_effect=fake_fetch)
    # Deliberately non-alphabetical, so a hidden sorted()/set() would be caught.
    provider.fetch_news(symbols=["ZEBRA", "APPLE", "MANGO"])

    per_symbol_fetched = [n for n in fetched if n.startswith("Yahoo Finance (")]
    assert per_symbol_fetched == ["Yahoo Finance (ZEBRA)", "Yahoo Finance (APPLE)", "Yahoo Finance (MANGO)"]


def test_run_news_update_orders_held_positions_before_candidates_deterministically():
    """TradingPipeline._run_news_update builds the per-symbol list as held
    positions first, then the run's admitted candidates, deduped while
    preserving that order — never raw set iteration."""

    pipeline = build_pipeline(
        news_provider=MagicMock(), news_store=MagicMock(), news_analyst=MagicMock(), db=MagicMock()
    )
    pipeline.config = SimpleNamespace(
        trading=SimpleNamespace(universe=["SPY"]),
        news=SimpleNamespace(max_prompt_items=50),
    )
    pipeline.news_provider.fetch_news.return_value = ([], None)
    pipeline.news_provider.format_for_prompt.return_value = "no news"
    pipeline.news_provider.tag_symbol_mentions.return_value = {}
    pipeline.news_store.load_macro_narrative.return_value = None
    pipeline.news_analyst.analyze.return_value = (
        None,
        MagicMock(user_message="m"),
    )

    pipeline._run_news_update(
        "run1",
        session="morning",
        held_symbols=["msft", "aapl"],
        candidate_symbols=["AAPL", "rsg"],  # AAPL duplicated on purpose
    )

    kwargs = pipeline.news_provider.fetch_news.call_args.kwargs
    assert kwargs["symbols"] == ["MSFT", "AAPL", "RSG"]


def test_morning_research_stage_passes_held_positions_and_admitted_candidates():
    """MorningResearchStage's news branch derives the per-symbol symbol
    list from ctx.positions (held) and ctx.admitted_symbols (this run's
    candidates) — the only run-scoped 'active candidate' concept available
    before news fetches (tech/nomination candidates don't exist yet; news
    and tech run concurrently in the same fan-out)."""
    from src.pipeline_stages import MorningResearchStage
    from src.pipeline_context import RunContext

    captured = {}

    def run_news_update_fn(run_id, session="morning", universe=None, held_symbols=None, candidate_symbols=None):
        captured["held_symbols"] = held_symbols
        captured["candidate_symbols"] = candidate_symbols
        return None, None

    mock_config = MagicMock()
    mock_config.trading.universe = ["SPY"]
    mock_config.trading.lookback_days = 30

    market = MagicMock()
    market.get_ohlcv.return_value = []

    macro_store = MagicMock()
    macro_store.load_last_state.return_value = None
    news_store = MagicMock()
    news_store.load_macro_narrative.return_value = None
    macro_agent = MagicMock()
    macro_agent.analyze.return_value = (
        None,
        MagicMock(
            user_message="m",
            raw_text="{}",
            tokens_used=1,
            model="t",
            input_tokens=1,
            output_tokens=1,
            cost_usd=0.0,
        ),
    )

    stage = MorningResearchStage(
        config=mock_config,
        db=MagicMock(),
        market=market,
        macro=MagicMock(),
        news_provider=MagicMock(),
        news_store=news_store,
        macro_store=macro_store,
        tech_store=MagicMock(),
        earnings_provider=MagicMock(),
        macro_analyst=macro_agent,
        news_analyst=MagicMock(),
        tech_analyst=MagicMock(),
        earnings_analyst=MagicMock(),
        has_actionable_signal_fn=lambda *a, **kw: False,
        run_news_update_fn=run_news_update_fn,
        load_earnings_analyses_fn=lambda run_id, session, ctx=None: ([], []),
    )

    ctx = RunContext.start("morning")
    ctx.positions = [
        SimpleNamespace(symbol="msft", qty=10),
        SimpleNamespace(symbol="abt", qty=-5),  # short — still "held"
        SimpleNamespace(symbol="sgov", qty=0),  # flat — must be excluded
    ]
    ctx.admitted_symbols = {"RSG", "MSFT"}  # overlaps a held position

    stage.run(ctx)

    assert captured["held_symbols"] == ["MSFT", "ABT"]
    # Since 2026-10-19 the news seat reads the whole shortlist (held, admitted, configured).
    assert captured["candidate_symbols"] == ["MSFT", "ABT", "RSG", "SPY"]


# ===========================================================================
# Coverage — per-symbol failures use the SAME NewsCoverage, not a parallel
# reporting path (2026-08-28 fix this must not bypass).
# ===========================================================================


def test_failing_per_symbol_feed_degrades_coverage_to_partial():
    provider = NewsDataProvider(feeds={"CNBC Top News": "http://alive"})

    def fake_fetch(name, url, cutoff):
        if name == "CNBC Top News":
            return [_item(title="Fed holds rates", source="CNBC Top News")]
        if name == "Yahoo Finance (BADSYM)":
            raise Exception("HTTP Error 500: Internal Server Error")
        return [_item(title=f"{name} headline", source=name)]

    provider._fetch_feed = MagicMock(side_effect=fake_fetch)

    items, coverage = provider.fetch_news(symbols=["GOODSYM", "BADSYM"])

    assert coverage.status == "partial"
    assert coverage.configured == 3  # 1 general feed + 2 per-symbol feeds
    assert coverage.succeeded == 2
    assert coverage.failed_count == 1
    assert coverage.failed[0].name == "Yahoo Finance (BADSYM)"
    assert "500" in coverage.failed[0].reason


def test_all_per_symbol_feeds_failing_is_partial_when_general_feeds_still_succeed():
    """Every per-symbol fetch fails, but the general wires are fine — this
    must read as 'partial' (real coverage came in), not 'failed' (which
    implies nothing came back at all)."""
    provider = NewsDataProvider()

    def fake_fetch(name, url, cutoff):
        if name.startswith("Yahoo Finance ("):
            raise Exception("HTTP Error 500: Internal Server Error")
        return []

    provider._fetch_feed = MagicMock(side_effect=fake_fetch)

    items, coverage = provider.fetch_news(symbols=["A", "B"])

    assert coverage.status == "partial"
    assert coverage.succeeded == len(provider.feeds)
    assert coverage.failed_count == 2
    assert {f.name for f in coverage.failed} == {
        "Yahoo Finance (A)",
        "Yahoo Finance (B)",
    }


def test_everything_failing_general_and_per_symbol_reports_failed():
    provider = NewsDataProvider()
    provider._fetch_feed = MagicMock(side_effect=Exception("boom"))

    items, coverage = provider.fetch_news(symbols=["A", "B"])

    assert items == []
    assert coverage.status == "failed"
    assert coverage.succeeded == 0
    assert coverage.configured == len(provider.feeds) + 2


# ===========================================================================
# Dedup — per-symbol items must flow through the SAME cascade, not around it.
# ===========================================================================


def test_per_symbol_items_flow_through_the_same_dedup_as_general_items():
    """A per-symbol story that a general wire already carried must collapse
    into ONE item via the shared dedup pass (src/data/news_dedup.py) — it
    must not read as independent confirmation just because it arrived via a
    different fetch path."""
    provider = NewsDataProvider(feeds={"CNBC Top News": "http://alive"})
    same_link = "https://finance.example.com/articles/acme-beats-estimates"

    def fake_fetch(name, url, cutoff):
        if name == "CNBC Top News":
            return [
                _item(
                    title="Acme Corp beats quarterly estimates",
                    source="CNBC Top News",
                    link=same_link + "?utm_source=cnbc",
                )
            ]
        if name == "Yahoo Finance (ACME)":
            return [
                _item(
                    title="Acme Corp beats quarterly estimates",
                    source="Yahoo Finance (ACME)",
                    link=same_link + "?ref=yahoo",
                )
            ]
        return []

    provider._fetch_feed = MagicMock(side_effect=fake_fetch)

    items, coverage = provider.fetch_news(symbols=["ACME"])

    assert len(items) == 1
    assert items[0].collapsed_count == 2
    assert items[0].source_count == 2


def test_per_symbol_items_that_are_genuinely_new_survive_dedup_distinctly():
    """Sanity counterpart to the merge test above: a per-symbol story with
    no general-wire counterpart must survive as its own item, not get
    accidentally swallowed."""
    provider = NewsDataProvider(feeds={"CNBC Top News": "http://alive"})

    def fake_fetch(name, url, cutoff):
        if name == "CNBC Top News":
            return [_item(title="Fed holds rates steady", source="CNBC Top News", link="https://cnbc.example.com/fed")]
        if name == "Yahoo Finance (ACME)":
            return [
                _item(
                    title="Acme wins exclusive supply contract",
                    source="Yahoo Finance (ACME)",
                    link="https://finance.example.com/acme-contract",
                )
            ]
        return []

    provider._fetch_feed = MagicMock(side_effect=fake_fetch)

    items, coverage = provider.fetch_news(symbols=["ACME"])

    assert len(items) == 2
    titles = {i.title for i in items}
    assert "Fed holds rates steady" in titles
    assert "Acme wins exclusive supply contract" in titles


# ===========================================================================
# Zero symbols — no-op wall. General wire path is byte-identical to today.
# ===========================================================================


def test_zero_symbols_is_byte_identical_to_omitting_symbols_argument():
    provider = NewsDataProvider(feeds={"CNBC Top News": "http://alive"})
    provider._fetch_feed = MagicMock(return_value=[_item(title="Fed holds rates", source="CNBC Top News")])

    items_default, coverage_default = provider.fetch_news()
    items_empty, coverage_empty = provider.fetch_news(symbols=[])
    items_none, coverage_none = provider.fetch_news(symbols=None)

    for items in (items_default, items_empty, items_none):
        assert [i.title for i in items] == ["Fed holds rates"]

    assert coverage_default.configured == coverage_empty.configured == coverage_none.configured == 1
    assert coverage_default.succeeded == coverage_empty.succeeded == coverage_none.succeeded == 1
    # Exactly one call per fetch_news() invocation (the general feed) — no
    # per-symbol calls sneaked in on any of the three variants.
    assert provider._fetch_feed.call_count == 3


def test_zero_symbols_with_per_symbol_disabled_is_also_a_no_op():
    provider = NewsDataProvider(feeds={"CNBC Top News": "http://alive"}, per_symbol_enabled=False)
    provider._fetch_feed = MagicMock(return_value=[_item(title="Fed holds rates", source="CNBC Top News")])
    items, coverage = provider.fetch_news()
    assert coverage.configured == 1
    assert [i.title for i in items] == ["Fed holds rates"]


# ===========================================================================
# Prompt bound — max_items bounds general wires only; per-symbol items are
# window-bounded and never share a count across names (2026-10-19).
# ===========================================================================


def test_format_for_prompt_bounds_general_wires_and_keeps_every_per_symbol_item():
    items = [_item(title=f"General {i}", source="CNBC") for i in range(5)] + [
        _item(title=f"PerSym {i}", source=f"Yahoo Finance (S{i})", per_symbol=True) for i in range(5)
    ]
    text = NewsDataProvider().format_for_prompt(items, max_items=2)
    assert [t for t in (f"General {i}" for i in range(5)) if t in text] == ["General 0", "General 1"]
    assert all(f"PerSym {i}" in text for i in range(5))


# ===========================================================================
# Config <-> provider wiring.
# ===========================================================================


def test_news_config_per_symbol_defaults_match_provider_defaults():
    """NewsConfig's defaults and NewsDataProvider's own constructor defaults
    must never silently drift apart — pipeline.py always passes config
    values explicitly, but a NewsDataProvider built without a config
    (tests, scripts) falls back to its own literals."""
    from src.config import NewsConfig

    cfg = NewsConfig()
    provider = NewsDataProvider()
    assert provider.per_symbol_enabled == cfg.per_symbol_enabled
    assert provider.per_symbol_request_interval_s == pytest.approx(1.0 / cfg.per_symbol_requests_per_second)


def test_news_config_loads_custom_per_symbol_values_from_yaml(tmp_path):
    yaml_content = """
api_keys:
  anthropic: "test-key"
  fred: "fred-key"
  alpaca_key: "alpaca-key"
  alpaca_secret: "alpaca-secret"
alpaca:
  base_url: "https://paper-api.alpaca.markets"
  paper: true
llm:
  tech_analyst_model: "claude-sonnet-4-6"
  fallback_provider: "anthropic"
  fallback_model: "claude-opus-4-7"
  max_tokens: 4096
risk:
  max_position_pct: 20
  max_total_position_pct: 90
  max_sector_pct: 40
  require_stop_loss: true
trading:
  universe: ["SPY", "QQQ"]
  lookback_days: 120
  schedule:
    morning: "06:00"
    midday: "12:00"
    evening: "16:30"
storage:
  db_path: "data/quant_agent.db"
news:
  max_prompt_items: 12
  per_symbol_enabled: false
  per_symbol_requests_per_second: 1.5
"""
    config_file = tmp_path / "settings.yaml"
    config_file.write_text(yaml_content)

    from src.config import load_config, NewsConfig

    cfg = load_config(config_file)

    assert cfg.news.per_symbol_enabled is False
    assert cfg.news.per_symbol_requests_per_second == 1.5
    assert cfg.news.per_symbol_requests_per_second != NewsConfig().per_symbol_requests_per_second


def test_removed_count_caps_are_gone_from_config():
    """2026-10-19: the name-count and headline-count caps were removed; a
    leftover key would read as a live knob that no code honours."""
    from src.config import NewsConfig

    assert "per_symbol_max_symbols" not in NewsConfig.model_fields
    assert "per_symbol_max_prompt_items" not in NewsConfig.model_fields
