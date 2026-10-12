"""One honest end-to-end morning session, hermetic, on every commit.

WHAT THIS DRIVES
----------------
`TradingPipeline(config).run_morning()` with the REAL config loader, the
REAL broker object, the REAL research / decision / risk / execution stages
and the REAL agent classes (prompt build, JSON parse, schema validation,
grounding, cost accounting). Every external dependency is replaced at its
own boundary by a deterministic stand-in:

  market data   -> synthetic OHLCV bars (real indicators are computed on them)
  macro/news/
  earnings feeds-> fixed payloads at the data-provider class seam
  broker        -> production `AlpacaBroker` whose two SDK clients are the
                   rehearsal stand-ins (`ops/rehearsal/broker.py`); nothing
                   is submitted anywhere
  model seats   -> the four provider transports on `BaseAgent` answer from a
                   script keyed by seat, the same seam the rehearsal replays
                   through; everything above the wire is production code
  clock         -> `ops/rehearsal/clock.frozen_clock`

WHAT IT ASSERTS
---------------
The SHAPE of the run, not one value: that the four stages ran, once each,
in order; that every analyst seat answered before the Portfolio Manager and
the Portfolio Manager before the Risk Manager; that the run reached
execution and left as `executed`; and that the orders the broker stand-in
received are exactly the BUYs the decision called for.

A second test removes one stage and shows the shape check fails — a test
that cannot fail when a stage is skipped proves nothing.

No network (tests/conftest.py already refuses outbound HTTP), no real
credentials (sentinel keys), no real account, synthetic prices only.
"""

from __future__ import annotations

import json
import math
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml

from src.models import (
    AnalystProvenance,
    MacroAnalysis,
    MacroNarrative,
    MacroPositionGuidance,
    MacroReasoningChain,
    NewsIntelligenceReport,
    OHLCV,
    PortfolioDecision,
    ReasoningChain,
    RiskReasoningChain,
    RiskVerdict,
    TargetPosition,
    TechAnalysisResult,
    TechReasoningChain,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SYMBOL = "SPY"  # a synthetic series; no desk output, no real price
SESSION_AT = datetime(2026, 10, 1, 10, 0)  # a Thursday; frozen for the run


# --------------------------------------------------------------------------
# Deterministic stand-ins
# --------------------------------------------------------------------------


def _synthetic_bars(n: int = 160) -> list[OHLCV]:
    """A range: repeated swing highs near RANGE_HIGH and swing lows near
    RANGE_LOW (a 24-session cycle), ending on the way up out of a low.

    The constructor refuses a BUY whose price history holds no repeated
    turning point to hang a stop and a target on, so a straight line is
    not a usable stand-in — the desk measured that itself.
    """
    bars = []
    start = SESSION_AT.date() - timedelta(days=int(n * 1.5) + 2)
    d = start
    i = 0
    while len(bars) < n:
        d += timedelta(days=1)
        if d.weekday() >= 5:
            continue
        # Phase chosen so the final bar sits ~2/3 of the way down the range
        # and rising: the next bar's phase is -0.7 rad from the trough.
        phase = 2 * math.pi * (i - (n - 1)) / 24 - 0.7 * 2 * math.pi / 24 * 0 - math.pi / 2 + 0.7
        close = RANGE_MID + RANGE_AMP * math.sin(phase)
        bars.append(
            OHLCV(
                date=d,
                open=round(close - 0.1, 2),
                high=round(close + 0.4, 2),
                low=round(close - 0.4, 2),
                close=round(close, 2),
                volume=1_000_000,
            )
        )
        i += 1
    return bars


RANGE_MID, RANGE_AMP = 100.0, 5.0
BARS = _synthetic_bars()
LAST_CLOSE = BARS[-1].close
RANGE_LOW, RANGE_HIGH = RANGE_MID - RANGE_AMP, RANGE_MID + RANGE_AMP


def _scripted_answers() -> dict[str, dict]:
    """What each seat 'says', as the JSON object its parser expects."""
    tech = TechAnalysisResult(
        symbol=SYMBOL,
        rating="buy",
        entry_price=LAST_CLOSE,
        reference_target=RANGE_HIGH,
        stop_loss=round(RANGE_LOW - 3.0, 2),
        support_levels=[RANGE_LOW],
        resistance_levels=[RANGE_HIGH],
        setup_type="range",
        expected_horizon_sessions=60,
        reasoning="synthetic uptrend",
        thesis_invalid_if="closes below support",
        reasoning_chain=TechReasoningChain(
            trend="x",
            momentum="x",
            volatility="x",
            volume="x",
            support_resistance="x",
        ),
    )
    pm = PortfolioDecision(
        reasoning_chain=ReasoningChain(
            macro_filter="x",
            news_check="x",
            earnings_check="x",
            signal_conflicts="x",
            sizing_logic="x",
            portfolio_balance="x",
            cash_target="x",
        ),
        targets=[
            TargetPosition(
                symbol=SYMBOL,
                # Sized (with the stop $3 under support) to clear the owner's minimum risk per position
                # (owner rule 2026-08-27) at this fixture's stop distance.
                target_weight_pct=20.0,
                conviction="high",
                thesis="synthetic",
                thesis_invalid_if="closes below support",
                provenance=[
                    AnalystProvenance(
                        source="technical",
                        observed_stance="buy",
                        relationship="supports",
                        evidence="rating buy, trend up",
                    )
                ],
            )
        ],
        portfolio_view="constructive",
    )
    rm = RiskVerdict(
        approved=True,
        modifications=[],
        reasoning="approved",
        reasoning_chain=RiskReasoningChain(
            rr_audit="x",
            signal_fidelity="x",
            correlation_check="x",
            event_risk="x",
            sizing_sanity="x",
            overall="x",
        ),
    )
    macro = MacroAnalysis(
        reasoning_chain=MacroReasoningChain(
            volatility_analysis="a",
            yield_curve_analysis="b",
            monetary_policy_analysis="c",
            inflation_labor_credit="d",
            cross_signal_synthesis="e",
            sector_implications="f",
        ),
        regime="risk-on",
        confidence="medium",
        equity_outlook="bullish",
        position_guidance=MacroPositionGuidance(
            target_invested_pct=75.0,
            cash_recommendation_pct=25.0,
            reasoning="stub",
        ),
        summary="stub macro",
    )
    news = NewsIntelligenceReport(
        macro_narrative=MacroNarrative(
            last_updated=str(SESSION_AT.date()),
            era_themes=["synthetic"],
            current_regime="risk-on",
        ),
        state_changes=[],
        stock_news={},
        pm_briefing="stub",
        market_sentiment="neutral",
        confidence="medium",
    )
    return {
        # Only what a model emits: the Python-set fields (computed levels,
        # ATR) are the agent's job, and sending them as null is a hygiene hit.
        "tech": {"results": [json.loads(tech.model_dump_json(exclude_none=True))]},
        "portfolio": json.loads(pm.model_dump_json()),
        "risk": json.loads(rm.model_dump_json()),
        "macro": json.loads(macro.model_dump_json()),
        "news": json.loads(news.model_dump_json()),
    }


@contextmanager
def _scripted_model_seats(trace: list):
    """Answer every seat from the script at the wire seam of `BaseAgent`.

    Same four transports the rehearsal replays (`ops/rehearsal/replay.py`),
    for the same reason: the two failover routes call the wire directly.
    """
    from src.agents.base import BaseAgent

    answers = _scripted_answers()
    names = ("_anthropic_call", "_call_openai", "_call_deepseek", "_openai_wire_call")
    original = {n: getattr(BaseAgent, n) for n in names}

    def _answer(agent, model, authorize):
        if authorize is not None:
            authorize(model)
        seat = str(getattr(agent, "name", type(agent).__name__)).lower()
        key = next((k for k in answers if k in seat), None)
        if key is None:
            raise AssertionError(f"unscripted seat asked a model: {seat}")
        trace.append(("llm", key))
        return json.dumps(answers[key]), 1000, 200, "stop", 0.001

    BaseAgent._anthropic_call = lambda self, client, model, msg, *, authorize=None: _answer(self, model, authorize)
    BaseAgent._call_openai = lambda self, msg, *, authorize=None: _answer(self, self.model, authorize)
    BaseAgent._call_deepseek = lambda self, msg, *, authorize=None: _answer(self, self.model, authorize)
    BaseAgent._openai_wire_call = lambda self, client, model, provider, msg, *, provider_order=None, authorize=None: (
        _answer(self, model, authorize)
    )
    try:
        yield
    finally:
        for n, f in original.items():
            setattr(BaseAgent, n, f)


def _market_stub():
    m = MagicMock(name="MarketDataProvider")
    bars = BARS
    m.get_ohlcv.side_effect = lambda symbol, lookback_days=120: list(bars)
    m.get_ohlcv_batch.side_effect = lambda symbols, lookback_days=120: {s: list(bars) for s in symbols}
    return m


def _macro_feed_stub():
    m = MagicMock(name="MacroDataProvider")
    m.get_macro_summary.return_value = {
        "vix": {"current": 18.0, "mean_5d": 17.5, "trend": "falling"},
        "treasury": {"us2y": 4.5, "us10y": 4.3, "spread_2_10": -0.2, "inverted": True},
        "fed_funds_rate": {"current": 5.25},
    }
    return m


def _news_feed_stub():
    m = MagicMock(name="NewsDataProvider")
    m.fetch_news.return_value = ([], None)
    m.format_for_prompt.return_value = "No news."
    return m


def _earnings_feed_stub():
    m = MagicMock(name="EarningsDataProvider")
    m.check_and_fetch.return_value = []
    return m


def _build_config(tmp_path: Path):
    """Production settings through the production loader, repointed."""
    from ops.rehearsal.runner import _sentinel_credentials
    from src.config import load_config

    raw = yaml.safe_load((PROJECT_ROOT / "config" / "settings.yaml").read_text())
    raw.setdefault("storage", {})["db_path"] = str(tmp_path / "desk.db")
    for section in ("smart_money", "universe_screen"):
        if section in raw and "data_dir" in raw[section]:
            raw[section]["data_dir"] = str(tmp_path / section)
    raw.setdefault("trading", {})["universe"] = [SYMBOL]
    raw.setdefault("execution", {})["fill_stream_enabled"] = False
    # The FRED calendars reuse this retry policy; outbound HTTP is refused
    # by conftest, so retries would only buy sleep.
    raw.setdefault("macro", {}).update(
        {
            "max_retries": 0,
            "retry_backoff_base_s": 0.001,
            "retry_backoff_max_s": 0.001,
            "retry_backoff_jitter_s": 0.0,
        }
    )
    cfg_path = tmp_path / "config" / "settings.yaml"
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(yaml.safe_dump(raw, sort_keys=False))
    with _sentinel_credentials():
        return load_config(cfg_path)


def _trace_stages(pipeline, trace: list, *, skip: str | None = None):
    """Record each stage entry in order; optionally skip one outright."""
    for attr in ("morning_research_stage", "decision_stage", "risk_stage", "execution_stage"):
        stage = getattr(pipeline, attr)
        real_run = stage.run

        def _run(ctx, _attr=attr, _real=real_run):
            trace.append(("stage", _attr))
            if _attr == skip:
                return None if _attr != "execution_stage" else []
            return _real(ctx)

        stage.run = _run


def _run_session(tmp_path, monkeypatch, *, skip: str | None = None):
    from ops.rehearsal.broker import BrokerSnapshot, install_rehearsal_broker
    from ops.rehearsal.clock import frozen_clock
    from ops.rehearsal.runner import _sentinel_credentials
    from src.agents.base import reset_route_breakers
    from src.trading_calendar import ET

    reset_route_breakers()
    monkeypatch.chdir(tmp_path)  # anything the desk writes lands here
    config = _build_config(tmp_path)
    now = SESSION_AT.replace(tzinfo=ET)
    trace: list = []

    with (
        _sentinel_credentials(),
        patch("src.pipeline.MarketDataProvider", return_value=_market_stub()),
        patch("src.pipeline.MacroDataProvider", return_value=_macro_feed_stub()),
        patch("src.pipeline.NewsDataProvider", return_value=_news_feed_stub()),
        patch("src.pipeline.EarningsDataProvider", return_value=_earnings_feed_stub()),
        frozen_clock(now, run_id="e2e-morning"),
        _scripted_model_seats(trace),
    ):
        from src.pipeline import TradingPipeline

        pipeline = TradingPipeline(config)
        snapshot = BrokerSnapshot(
            as_of=now.date(),
            cash=10_000.0,
            portfolio_value=10_000.0,
            last_equity=10_000.0,
            positions=[],
            prices={SYMBOL: LAST_CLOSE},
        )
        trading = install_rehearsal_broker(pipeline.broker, snapshot, now=now)
        # The rehearsal data client serves a price with no timestamp, and
        # production's freshness test (src/data/live_price.py) rightly
        # refuses an unstamped print as a fill reference. Stamp it with a
        # print from THIS (frozen) session; everything above the SDK client
        # stays the production broker.
        #
        # The stamp is the FROZEN instant, never a wall-clock helper:
        # `frozen_clock` rebinds `et_now` only inside `src.*`, so
        # `tests.session_clock.todays_session_stamp()` reads the REAL date.
        # With SESSION_AT pinned to 2026-10-01 that agreed with the frozen
        # run only on the day the test was written; from the next ET
        # midnight the print was judged prior-session, the buy refused as
        # unmeasurable, and the run (correctly) stopped at `no_trades`.
        # 10:00 ET sits inside the regular session, so both resolver bounds
        # hold whatever the wall clock says.
        from types import SimpleNamespace

        print_stamp = now

        def _stamped_latest_trade(request):
            return {
                sym: SimpleNamespace(price=LAST_CLOSE, timestamp=print_stamp) for sym in trading_data_symbols(request)
            }

        trading_data_symbols = pipeline.broker._data_client._symbols
        pipeline.broker._data_client.get_stock_latest_trade = _stamped_latest_trade
        pipeline.broker.get_intraday_snapshots = lambda symbols, *a, **k: {
            s: {"last_price": LAST_CLOSE, "last_trade_at": print_stamp} for s in symbols
        }
        _trace_stages(pipeline, trace, skip=skip)
        result = pipeline.run_morning()
    return result, trace, trading


def _assert_full_shape(result: dict, trace: list, trading) -> None:
    stages = [name for kind, name in trace if kind == "stage"]
    assert stages == [
        "morning_research_stage",
        "decision_stage",
        "risk_stage",
        "execution_stage",
    ], (
        f"stages ran out of order or were skipped: {stages}; result={ {k: v for k, v in result.items() if k != 'leverage'} }"
    )

    seats = [name for kind, name in trace if kind == "llm"]
    assert seats.count("portfolio") == 1, f"PM asked {seats.count('portfolio')}x: {seats}"
    assert seats.count("risk") == 1, f"RM asked {seats.count('risk')}x: {seats}"
    pm_at, rm_at = seats.index("portfolio"), seats.index("risk")
    assert pm_at < rm_at, f"Risk Manager answered before the PM: {seats}"
    analysts = {"tech", "macro", "news"}
    assert analysts <= set(seats[:pm_at]), f"an analyst seat did not answer before the PM: {seats[:pm_at]}"
    # The stage trace and the seat trace must interleave the right way:
    # every analyst inside research, the PM inside decision, the RM inside risk.
    order = [name for _, name in trace]
    assert order.index("portfolio") > order.index("decision_stage")
    assert order.index("portfolio") < order.index("risk_stage")
    assert order.index("risk") > order.index("risk_stage")
    assert order.index("risk") < order.index("execution_stage")

    assert result["status"] == "executed", (
        f"run did not reach execution: {result.get('status')} "
        f"{result.get('error', '')} skips={result.get('execution_skips')} orders={result.get('orders')}"
    )
    buys = [o for o in trading.submitted if str(o.side).lower().endswith("buy")]
    assert [o.symbol for o in buys] == [SYMBOL], (
        f"broker received {[(o.symbol, str(o.side)) for o in trading.submitted]}, decision called for a BUY of {SYMBOL}"
    )
    assert all(float(o.qty or 0) > 0 or float(o.notional or 0) > 0 for o in buys)
    assert len(result["orders"]) >= 1


def test_morning_session_runs_every_stage_in_order_and_executes_the_decision(
    tmp_path,
    monkeypatch,
):
    result, trace, trading = _run_session(tmp_path, monkeypatch)
    _assert_full_shape(result, trace, trading)


def test_the_shape_check_fails_when_the_decision_stage_is_skipped(
    tmp_path,
    monkeypatch,
):
    """Sensitivity: a run whose decision stage silently does nothing must
    be REJECTED by the same checker, or the first test proves nothing."""
    result, trace, trading = _run_session(
        tmp_path,
        monkeypatch,
        skip="decision_stage",
    )
    with pytest.raises(AssertionError) as caught:
        _assert_full_shape(result, trace, trading)
    message = str(caught.value)
    assert "skipped" in message and "risk_stage" in message, message
    assert "pm_agent_failure" in message, message  # the desk's own verdict
    assert trading.submitted == [], "a skipped decision must place nothing"
