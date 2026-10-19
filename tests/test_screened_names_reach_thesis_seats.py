"""Screened morning names must reach a thesis seat (2026-10-19 fix).

The nightly scan sends ~100 screened names into the morning run. The
conviction bar needs a NON-technical seat with a directional thesis, so a
screened name the chart seat likes is refused unless news or earnings read
it. 16 Oct: 100 screened, 8 chart-buy, 0 ranked. Two causes, two pins:

* the news seat chose its capped per-symbol names BEFORE the chart seat
  answered, so its slots went to the top scores whatever the chart said;
* the pre-market earnings pass checked only configured + Form-4 names, so a
  screened name's new filing (ASAN 10-Q) was downloaded and never analysed.

Hermetic: ZZZ tickers, no network, no model.
"""

from datetime import date
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import src.data.news as news_mod
import src.stage_morning_research as morning_mod
from src.data.news import NewsDataProvider
from src.storage.db import Database
from tests.pipeline_factory import build_pipeline

NEWS_CAP = 15
SCREENED = [f"ZZZ{chr(ord('A') + i)}" for i in range(20)]  # ZZZA..ZZZT, best score first
CHART_BUY = "ZZZT"  # ranked 20th: outside the cap in score order


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch):
    monkeypatch.setattr(news_mod.time, "sleep", lambda _seconds: None)


def _run_morning(monkeypatch, positions):
    from src.pipeline_context import RunContext
    from src.pipeline_stages import MorningResearchStage

    captured: dict = {}

    def run_news_update_fn(run_id, session="morning", universe=None, held_symbols=None, candidate_symbols=None):
        captured["held"] = list(held_symbols or [])
        captured["candidates"] = list(candidate_symbols or [])
        return None, None

    monkeypatch.setattr(morning_mod, "compute_indicators", lambda symbol, bars: {})
    config = MagicMock()
    config.trading.universe = []
    config.trading.lookback_days = 30
    config.universe_screen.morning_top_n = 100
    market = MagicMock()
    market.get_ohlcv.return_value = [object()]
    market.get_valuation_metrics.return_value = {}
    macro_store = MagicMock()
    macro_store.load_last_state.return_value = None
    news_store = MagicMock()
    news_store.load_macro_narrative.return_value = None
    macro_agent = MagicMock()
    macro_agent.analyze.return_value = (None, MagicMock(user_message="m", raw_text="{}", cost_usd=0.0))
    tech = MagicMock()

    def analyze_batch(symbols_data, **_kw):
        out = {}
        for s in symbols_data:
            sym = s["symbol"]
            out[sym] = SimpleNamespace(symbol=sym, rating="buy" if sym == CHART_BUY else "neutral")
        return out, None

    tech.analyze_batch.side_effect = analyze_batch
    tech.last_unreadable = {}
    tech.last_unanswered = set()
    tech_store = MagicMock()
    tech_store.compute_ages.return_value = {}

    stage = MorningResearchStage(
        config=config,
        db=MagicMock(),
        market=market,
        macro=MagicMock(),
        news_provider=MagicMock(),
        news_store=news_store,
        macro_store=macro_store,
        tech_store=tech_store,
        earnings_provider=MagicMock(),
        macro_analyst=macro_agent,
        news_analyst=MagicMock(),
        tech_analyst=tech,
        earnings_analyst=MagicMock(),
        has_actionable_signal_fn=lambda *a, **kw: False,
        run_news_update_fn=run_news_update_fn,
        load_earnings_analyses_fn=lambda run_id, session, ctx=None: ([], []),
        admit_screened_universe_fn=lambda positions: (list(SCREENED), {s: {"k": 1.0} for s in SCREENED}),
    )
    ctx = RunContext.start("morning")
    ctx.positions = positions
    ctx.admitted_symbols = set()
    stage.run(ctx)
    return captured


def _per_symbol_names(captured):
    return list(dict.fromkeys(captured["held"] + captured["candidates"]))[:NEWS_CAP]


def test_chart_buy_screened_name_is_in_the_news_seats_per_symbol_list(monkeypatch):
    captured = _run_morning(monkeypatch, positions=[])
    assert CHART_BUY in _per_symbol_names(captured)


def test_held_names_still_come_first_and_the_cap_holds(monkeypatch):
    captured = _run_morning(monkeypatch, positions=[SimpleNamespace(symbol="zzzh", qty=3)])
    assert captured["held"] == ["ZZZH"]
    names = _per_symbol_names(captured)
    assert names[0] == "ZZZH"
    assert names[1] == CHART_BUY
    # The provider's own cap still bounds what is fetched, whatever the caller passes.
    provider = NewsDataProvider(per_symbol_max_symbols=NEWS_CAP)
    calls: list[str] = []
    provider._fetch_feed = MagicMock(side_effect=lambda name, url, cutoff: calls.append(name) or [])
    provider.fetch_news(symbols=captured["held"] + captured["candidates"])
    fetched = [c for c in calls if c.startswith("Yahoo Finance (ZZZ")]
    assert len(fetched) == NEWS_CAP
    assert fetched[:2] == ["Yahoo Finance (ZZZH)", f"Yahoo Finance ({CHART_BUY})"]


@pytest.fixture
def screen_record(monkeypatch):
    """Last session's nightly screen record: present unless a test says otherwise."""
    import src.universe_daily as daily_mod

    state = {"recorded": True}
    monkeypatch.setattr(daily_mod, "_screen_day", lambda broker, today, now: date(2026, 10, 16))
    monkeypatch.setattr(daily_mod, "already_recorded", lambda data_dir, day: state["recorded"])
    return state


def test_screened_name_with_a_new_filing_is_in_the_earnings_premarket_list(tmp_path, screen_record):
    pipeline = build_pipeline(db=Database(str(tmp_path / "t.db")))
    pipeline.broker = MagicMock()
    pipeline.broker.get_positions.return_value = []
    pipeline.config = MagicMock()
    pipeline.config.trading.universe = ["ZZZC"]
    pipeline.config.smart_money.enabled = False
    pipeline.config.universe_screen.morning_top_n = 2
    pipeline.admission = SimpleNamespace(
        _admit_screened_universe_symbols=lambda positions=None: (
            ["ZZZC", "ZZZA", "ZZZB", "ZZZD"],
            {s: {"k": 1.0} for s in ["ZZZC", "ZZZA", "ZZZB", "ZZZD"]},
        )
    )
    symbols = pipeline._earnings_preprocess_symbols()
    # configured first; configured screened names do not use a rank slot; top 2 others.
    assert symbols == ["ZZZC", "ZZZA", "ZZZB"]


def test_no_screen_record_today_adds_no_screened_names(tmp_path, screen_record):
    """No complete nightly record: explicit empty result, configured list intact, ranking never read."""
    screen_record["recorded"] = False
    pipeline = build_pipeline(db=Database(str(tmp_path / "t.db")))
    pipeline.broker = MagicMock()
    pipeline.config = MagicMock()
    pipeline.config.trading.universe = ["ZZZC"]
    pipeline.config.smart_money.enabled = False
    reader = MagicMock(side_effect=AssertionError("ranking read without a record"))
    pipeline.admission = SimpleNamespace(_admit_screened_universe_symbols=reader)
    assert pipeline._earnings_preprocess_symbols() == ["ZZZC"]
    reader.assert_not_called()


def test_morning_and_earnings_pick_the_same_screened_names(tmp_path, monkeypatch, screen_record):
    """One ranking rule: the morning's admitted screened names equal the earnings pass's list."""
    import src.intraday.candidates as candidates_mod
    from src.pipeline_parts.pnl_gaps import _screened_morning_symbols
    from src.stage_morning_research import order_morning_symbols

    ranked = ["ZZZC", "ZZZA", "ZZZS", "ZZZB", "ZZZD"]
    details = {s: {"k": 1.0} for s in ranked}
    details["ZZZH"] = {"k": None}  # held passer, unscored: still admitted
    details["ZZZU"] = {"k": None, "score_reason": "no bars"}  # unscored: dropped
    recorded: list[tuple[str, str]] = []
    monkeypatch.setattr(
        candidates_mod,
        "record_dropped_name",
        lambda owner, ctx, sym, reason, **kw: recorded.append((sym, reason)),
    )
    ctx = SimpleNamespace(
        positions=[SimpleNamespace(symbol="ZZZH", qty=1)],
        admitted_symbols={"ZZZS"},  # an SEC admission: skipped, uses no rank slot
        smart_money_admissions={},
    )
    order_morning_symbols(object(), ctx, configured_symbols=["ZZZC"], ranked=ranked, screened_details=details, top_n=2)
    morning_screened = ctx.admitted_symbols - {"ZZZS"}

    pipeline = build_pipeline(db=Database(str(tmp_path / "t.db")))
    pipeline.broker = MagicMock()
    pipeline.broker.get_positions.return_value = [SimpleNamespace(symbol="ZZZH", qty=1)]
    pipeline.config = MagicMock()
    pipeline.config.universe_screen.morning_top_n = 2
    pipeline.admission = SimpleNamespace(_admit_screened_universe_symbols=lambda positions=None: (ranked, details))
    earnings_screened = _screened_morning_symbols(pipeline, {"ZZZC", "ZZZS"})

    assert earnings_screened == ["ZZZH", "ZZZA", "ZZZB"]
    assert set(earnings_screened) == morning_screened
    assert recorded == [("ZZZU", "not_sent_unscored"), ("ZZZD", "not_sent_weaker_rank")]
