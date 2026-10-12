"""Stage 0.5 — D-1 actual-model attribution hotfix regression tests.

Every `insert_agent_log(...)` call site used to persist `agent_logs.model`
from `config.llm.<agent>_model` — the *configured/requested* model — instead
of `AgentResult.model` — the model that *actually answered*, including
cross-provider failover (`base.py:run()` falls back to the CONFIGURED
fallback provider/model — `llm.fallback_provider`/`fallback_model`,
2026-08-31 — when the primary exhausts retries and the fallback pair differs
from the primary's; see `BaseAgent._failover_reachable`). The two values
only diverge in a failover, so every test here configures a primary model
that differs from the model embedded in the mocked `AgentResult` (a
realistic stand-in for a failover result — the literal string used,
`claude-opus-4-7`, no longer needs to match any live routing constant; it is
just illustrative here, since these tests exercise pipeline.py's
`insert_agent_log` persistence, not base.py's routing itself) and asserts
the persisted row carries the *actual* value, not the configured one.

One test per session type that reaches an `insert_agent_log` call, covering
all nine call sites (audit `docs/STAGE0_BASELINE_AUDIT.md` §9A):
  - morning session: macro, tech, news, portfolio_manager, risk_manager (5)
  - midday/close session: position_reviewer (1)
  - earnings_preprocess: earnings_analyst_preprocess (1)
  - evening: evening_analyst (1)
  - quarterly meta: meta_reflector (1)
"""

import json
from datetime import date
from unittest.mock import MagicMock, patch

from src.agents.base import AgentResult
from src.data.earnings import EarningsReport
from src.models import (
    EveningReasoningChain,
    EveningReport,
    MacroAnalysis,
    MacroNarrative,
    MacroPositionGuidance,
    MacroReasoningChain,
    NewsIntelligenceReport,
    PortfolioDecision,
    Position,
    PositionReasoningChain,
    PositionReview,
    QuarterlyMetaReflection,
    ReasoningChain,
    RiskReasoningChain,
    RiskVerdict,
    TargetPosition,
    TechAnalysisResult,
    TechReasoningChain,
)
from src.pipeline import TradingPipeline
from src.storage.db import Database
from tests.session_clock import todays_session_stamp
from tests.pipeline_factory import build_pipeline

# A stand-in "actually answered" model — always distinct from the
# "configured" model set on each test's mock_config so a passing assertion
# actually proves attribution, not coincidence. Not tied to any live routing
# constant (base.py no longer hardcodes a failover model at all — see
# llm.fallback_provider/fallback_model, 2026-08-31); these tests exercise
# pipeline.py's persistence of AgentResult.model, not base.py's routing.
_ACTUAL_FAILOVER_MODEL = "claude-opus-4-7"
_CONFIGURED_PRIMARY_MODEL = "gpt-5.5"


def _pm_rc() -> ReasoningChain:
    return ReasoningChain(
        macro_filter="x",
        news_check="x",
        earnings_check="x",
        signal_conflicts="x",
        sizing_logic="x",
        portfolio_balance="x",
        cash_target="x",
    )


def _risk_rc() -> RiskReasoningChain:
    return RiskReasoningChain(
        rr_audit="x",
        signal_fidelity="x",
        correlation_check="x",
        event_risk="x",
        sizing_sanity="x",
        overall="x",
    )


def _trc() -> TechReasoningChain:
    return TechReasoningChain(
        trend="x",
        momentum="x",
        volatility="x",
        volume="x",
        support_resistance="x",
    )


def _review_rc() -> PositionReasoningChain:
    return PositionReasoningChain(
        macro_continuity_check="stable",
        thesis_progress_check="ok",
        thesis_integrity_check="no triggers",
        winners_discipline_check="no flags",
        session_disposition_check="patient",
        execution_rationale="n/a",
    )


def _evening_rc() -> EveningReasoningChain:
    return EveningReasoningChain(
        performance_attribution="flat day, no major attribution",
        outlook_retrospection="n/a (no prior)",
        thesis_health_review="no theses to review (empty book)",
        decision_quality_review="no trades today",
        calibration_meta="insufficient history",
        market_regime_read="regime stable",
        tomorrow_preparation="no key events",
    )


def _macro_stub() -> MacroAnalysis:
    return MacroAnalysis(
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
        summary="stub macro analysis",
    )


def _agent_result(model: str) -> AgentResult:
    return AgentResult(raw_text="{}", tokens_used=100, model=model, user_message="x")


def _mock_config():
    cfg = MagicMock()
    cfg.api_keys.anthropic = "test-key"
    cfg.api_keys.fred = "fred-key"
    cfg.api_keys.alpaca_key = "alp-key"
    cfg.api_keys.alpaca_secret = "alp-secret"
    cfg.alpaca.paper = True
    cfg.llm.tech_analyst_model = _CONFIGURED_PRIMARY_MODEL
    cfg.llm.news_analyst_model = _CONFIGURED_PRIMARY_MODEL
    cfg.llm.macro_analyst_model = _CONFIGURED_PRIMARY_MODEL
    cfg.llm.earnings_analyst_model = _CONFIGURED_PRIMARY_MODEL
    cfg.llm.portfolio_manager_model = _CONFIGURED_PRIMARY_MODEL
    cfg.llm.risk_manager_model = _CONFIGURED_PRIMARY_MODEL
    cfg.llm.position_reviewer_model = _CONFIGURED_PRIMARY_MODEL
    cfg.llm.evening_analyst_model = _CONFIGURED_PRIMARY_MODEL
    cfg.llm.meta_reflector_model = _CONFIGURED_PRIMARY_MODEL
    cfg.llm.max_tokens = 4096
    cfg.risk.max_position_pct = 20
    cfg.risk.max_total_position_pct = 90
    cfg.risk.max_sector_pct = 40
    cfg.risk.require_stop_loss = True
    cfg.trading.universe = ["SPY"]
    cfg.trading.lookback_days = 120
    return cfg


@patch("src.pipeline.AlpacaBroker")
@patch("src.pipeline.EarningsDataProvider")
@patch("src.pipeline.EarningsAnalystAgent")
@patch("src.pipeline.NewsDataProvider")
@patch("src.pipeline.NewsAnalystAgent")
@patch("src.pipeline.MacroAnalystAgent")
@patch("src.pipeline.MacroDataProvider")
@patch("src.pipeline.MarketDataProvider")
@patch("src.pipeline.RiskManagerAgent")
@patch("src.pipeline.PortfolioManagerAgent")
@patch("src.pipeline.TechAnalystAgent")
@patch("src.stage_morning_research.compute_indicators")
@patch("src.data.technical.compute_indicators")
def test_morning_session_persists_actual_model_for_all_five_agents(
    mock_ci,
    mock_ci_stages,
    mock_ta_cls,
    mock_pm_cls,
    mock_rm_cls,
    mock_market_cls,
    mock_macro_cls,
    mock_maa_cls,
    mock_na_cls,
    mock_ndp_cls,
    mock_ea_cls,
    mock_edp_cls,
    mock_broker_cls,
    tmp_path,
):
    """macro / tech / news / portfolio_manager / risk_manager (audit sites
    pipeline_stages.py:280,328,483,699 and pipeline.py:4704) must each
    persist AgentResult.model, not config.llm.<agent>_model."""
    mock_config = _mock_config()
    mock_config.storage.db_path = str(tmp_path / "test.db")

    mock_ta = MagicMock()
    spy_analysis = TechAnalysisResult(
        symbol="SPY",
        rating="buy",
        entry_price=507.0,
        reference_target=545.0,
        stop_loss=490.0,
        support_levels=[490.0],
        resistance_levels=[545.0],
        # Python-set by TechAnalystAgent, never model-emitted. The
        # constructor derives the take-profit from `computed_levels`
        # (2026-09-01) and refuses without them.
        computed_levels=[490.0, 545.0],
        atr_14=17.0 / 3.5,
        setup_type="range",
        expected_horizon_sessions=60,
        reasoning="Bullish",
        reasoning_chain=_trc(),
        thesis_invalid_if="closes below support",
    )
    mock_ta.analyze_batch.return_value = (
        {"SPY": spy_analysis},
        _agent_result(_ACTUAL_FAILOVER_MODEL),
    )
    mock_ta_cls.return_value = mock_ta

    mock_pm = MagicMock()
    mock_pm.decide.return_value = (
        PortfolioDecision(
            reasoning_chain=_pm_rc(),
            targets=[
                TargetPosition(
                    symbol="SPY",
                    # Sized to clear the owner's minimum risk per position (owner rule
                    # 2026-08-27): 10% through the $490 stop risked only 0.34% of equity.
                    target_weight_pct=20.0,
                    conviction="high",
                    thesis="Buy",
                    thesis_invalid_if="closes below support",
                )
            ],
            portfolio_view="Bullish",
        ),
        _agent_result(_ACTUAL_FAILOVER_MODEL),
    )
    mock_pm_cls.return_value = mock_pm

    mock_rm = MagicMock()
    mock_rm.review.return_value = (
        RiskVerdict(
            approved=True,
            modifications=[],
            reasoning="Approved",
            reasoning_chain=_risk_rc(),
        ),
        _agent_result(_ACTUAL_FAILOVER_MODEL),
    )
    mock_rm_cls.return_value = mock_rm

    mock_market = MagicMock()
    mock_market.get_ohlcv.return_value = [
        MagicMock(date="2026-04-07", open=503, high=510, low=500, close=507, volume=1000000)
    ]
    mock_market_cls.return_value = mock_market

    mock_macro = MagicMock()
    mock_macro.get_macro_summary.return_value = {
        "vix": {"current": 18.0, "mean_5d": 17.5, "trend": "falling"},
        "treasury": {"us2y": 4.5, "us10y": 4.3, "spread_2_10": -0.2, "inverted": True},
        "fed_funds_rate": 5.25,
    }
    mock_macro_cls.return_value = mock_macro

    mock_broker = MagicMock()
    mock_broker.list_open_orders_checked.return_value = (True, [])  # listing read, nothing open
    mock_broker.is_trading_day.return_value = True
    mock_broker.get_latest_price.return_value = 507.0
    mock_broker.get_latest_quote.return_value = {"bid_price": 507.0, "ask_price": 507.0}
    mock_broker.get_intraday_snapshots.return_value = {
        "SPY": {"last_price": 507.0, "last_trade_at": todays_session_stamp()}
    }
    mock_broker.get_account.return_value = {"cash": 10000.0, "portfolio_value": 10000.0}
    mock_broker.get_positions.return_value = []
    mock_broker.submit_order.return_value = {"id": "order-1", "status": "accepted", "symbol": "SPY"}
    mock_broker_cls.return_value = mock_broker

    mock_maa = MagicMock()
    mock_maa.analyze.return_value = (_macro_stub(), _agent_result(_ACTUAL_FAILOVER_MODEL))
    mock_maa_cls.return_value = mock_maa

    mock_na = MagicMock()
    mock_na.analyze.return_value = (
        NewsIntelligenceReport(
            macro_narrative=MacroNarrative(
                last_updated="2026-04-07",
                era_themes=["AI capex"],
                current_regime="risk-on, AI-led rally",
            ),
            state_changes=[],
            stock_news={},
            pm_briefing="Bullish news",
            market_sentiment="bullish",
            confidence="medium",
        ),
        _agent_result(_ACTUAL_FAILOVER_MODEL),
    )
    mock_na_cls.return_value = mock_na
    mock_ndp = MagicMock()
    mock_ndp.fetch_news.return_value = ([], None)  # (items, coverage) — see src/data/news.py NewsCoverage
    mock_ndp.format_for_prompt.return_value = "No news."
    mock_ndp_cls.return_value = mock_ndp

    mock_ea = MagicMock()
    mock_ea.analyze_reports.return_value = []
    mock_ea_cls.return_value = mock_ea
    mock_edp = MagicMock()
    mock_edp.check_and_fetch.return_value = []
    mock_edp_cls.return_value = mock_edp

    pipeline = TradingPipeline(mock_config)
    result = pipeline.run_morning()

    assert result["status"] == "executed"

    logs = pipeline.db.get_agent_logs(result["run_id"])
    by_agent = {row["agent_name"]: row["model"] for row in logs}

    for agent_name in (
        "macro_analyst",
        "tech_analyst",
        "news_analyst_morning",
        "portfolio_manager",
        "risk_manager",
    ):
        assert agent_name in by_agent, f"expected an agent_logs row for {agent_name}"
        assert by_agent[agent_name] == _ACTUAL_FAILOVER_MODEL, (
            f"{agent_name}: expected persisted model to be the actual "
            f"responding model {_ACTUAL_FAILOVER_MODEL!r}, got {by_agent[agent_name]!r}"
        )
        assert by_agent[agent_name] != _CONFIGURED_PRIMARY_MODEL, (
            f"{agent_name}: agent_logs.model must not record the configured primary model on a failover"
        )


@patch("src.pipeline.AlpacaBroker")
@patch("src.pipeline.EarningsDataProvider")
@patch("src.pipeline.EarningsAnalystAgent")
@patch("src.pipeline.NewsDataProvider")
@patch("src.pipeline.NewsAnalystAgent")
@patch("src.pipeline.MacroAnalystAgent")
@patch("src.pipeline.MacroDataProvider")
@patch("src.pipeline.MarketDataProvider")
@patch("src.pipeline.RiskManagerAgent")
@patch("src.pipeline.PortfolioManagerAgent")
@patch("src.pipeline.TechAnalystAgent")
@patch("src.stage_morning_research.compute_indicators")
@patch("src.data.technical.compute_indicators")
def test_morning_session_decision_id_correlates_pm_rm_and_trade(
    mock_ci,
    mock_ci_stages,
    mock_ta_cls,
    mock_pm_cls,
    mock_rm_cls,
    mock_market_cls,
    mock_macro_cls,
    mock_maa_cls,
    mock_na_cls,
    mock_ndp_cls,
    mock_ea_cls,
    mock_edp_cls,
    mock_broker_cls,
    tmp_path,
):
    """Stage 1 correlation: the portfolio_manager agent_logs row, the
    risk_manager agent_logs row, and the resulting BUY's trades row must all
    share one decision_id — the minimal id needed to trace a PM proposal
    through RM review to the order/trade it produced, end-to-end through a
    real run_morning() and a real (tmp_path) SQLite DB."""
    mock_config = _mock_config()
    mock_config.storage.db_path = str(tmp_path / "test.db")

    mock_ta = MagicMock()
    spy_analysis = TechAnalysisResult(
        symbol="SPY",
        rating="buy",
        entry_price=507.0,
        reference_target=545.0,
        stop_loss=490.0,
        support_levels=[490.0],
        resistance_levels=[545.0],
        # Python-set by TechAnalystAgent, never model-emitted. The
        # constructor derives the take-profit from `computed_levels`
        # (2026-09-01) and refuses without them.
        computed_levels=[490.0, 545.0],
        atr_14=17.0 / 3.5,
        setup_type="range",
        expected_horizon_sessions=60,
        reasoning="Bullish",
        reasoning_chain=_trc(),
        thesis_invalid_if="closes below support",
    )
    mock_ta.analyze_batch.return_value = (
        {"SPY": spy_analysis},
        _agent_result(_ACTUAL_FAILOVER_MODEL),
    )
    mock_ta_cls.return_value = mock_ta

    mock_pm = MagicMock()
    mock_pm.decide.return_value = (
        PortfolioDecision(
            reasoning_chain=_pm_rc(),
            targets=[
                TargetPosition(
                    symbol="SPY",
                    # Sized to clear the owner's minimum risk per position (owner rule
                    # 2026-08-27): 10% through the $490 stop risked only 0.34% of equity.
                    target_weight_pct=20.0,
                    conviction="high",
                    thesis="Buy",
                    thesis_invalid_if="closes below support",
                )
            ],
            portfolio_view="Bullish",
        ),
        _agent_result(_ACTUAL_FAILOVER_MODEL),
    )
    mock_pm_cls.return_value = mock_pm

    mock_rm = MagicMock()
    mock_rm.review.return_value = (
        RiskVerdict(
            approved=True,
            modifications=[],
            reasoning="Approved",
            reasoning_chain=_risk_rc(),
        ),
        _agent_result(_ACTUAL_FAILOVER_MODEL),
    )
    mock_rm_cls.return_value = mock_rm

    mock_market = MagicMock()
    mock_market.get_ohlcv.return_value = [
        MagicMock(date="2026-04-07", open=503, high=510, low=500, close=507, volume=1000000)
    ]
    mock_market_cls.return_value = mock_market

    mock_macro = MagicMock()
    mock_macro.get_macro_summary.return_value = {
        "vix": {"current": 18.0, "mean_5d": 17.5, "trend": "falling"},
        "treasury": {"us2y": 4.5, "us10y": 4.3, "spread_2_10": -0.2, "inverted": True},
        "fed_funds_rate": 5.25,
    }
    mock_macro_cls.return_value = mock_macro

    mock_broker = MagicMock()
    mock_broker.list_open_orders_checked.return_value = (True, [])  # listing read, nothing open
    mock_broker.is_trading_day.return_value = True
    mock_broker.get_latest_price.return_value = 507.0
    mock_broker.get_latest_quote.return_value = {"bid_price": 507.0, "ask_price": 507.0}
    mock_broker.get_intraday_snapshots.return_value = {
        "SPY": {"last_price": 507.0, "last_trade_at": todays_session_stamp()}
    }
    mock_broker.get_account.return_value = {"cash": 10000.0, "portfolio_value": 10000.0}
    mock_broker.get_positions.return_value = []
    mock_broker.submit_order.return_value = {"id": "order-1", "status": "accepted", "symbol": "SPY"}
    mock_broker_cls.return_value = mock_broker

    mock_maa = MagicMock()
    mock_maa.analyze.return_value = (_macro_stub(), _agent_result(_ACTUAL_FAILOVER_MODEL))
    mock_maa_cls.return_value = mock_maa

    mock_na = MagicMock()
    mock_na.analyze.return_value = (
        NewsIntelligenceReport(
            macro_narrative=MacroNarrative(
                last_updated="2026-04-07",
                era_themes=["AI capex"],
                current_regime="risk-on, AI-led rally",
            ),
            state_changes=[],
            stock_news={},
            pm_briefing="Bullish news",
            market_sentiment="bullish",
            confidence="medium",
        ),
        _agent_result(_ACTUAL_FAILOVER_MODEL),
    )
    mock_na_cls.return_value = mock_na
    mock_ndp = MagicMock()
    mock_ndp.fetch_news.return_value = ([], None)  # (items, coverage) — see src/data/news.py NewsCoverage
    mock_ndp.format_for_prompt.return_value = "No news."
    mock_ndp_cls.return_value = mock_ndp

    mock_ea = MagicMock()
    mock_ea.analyze_reports.return_value = []
    mock_ea_cls.return_value = mock_ea
    mock_edp = MagicMock()
    mock_edp.check_and_fetch.return_value = []
    mock_edp_cls.return_value = mock_edp

    pipeline = TradingPipeline(mock_config)
    result = pipeline.run_morning()

    assert result["status"] == "executed"

    logs = pipeline.db.get_agent_logs(result["run_id"])
    pm_row = next(r for r in logs if r["agent_name"] == "portfolio_manager")
    rm_row = next(r for r in logs if r["agent_name"] == "risk_manager")
    macro_row = next(r for r in logs if r["agent_name"] == "macro_analyst")

    assert pm_row["decision_id"], "portfolio_manager row must carry a non-empty decision_id"
    assert pm_row["decision_id"] == rm_row["decision_id"], (
        "risk_manager's review of THIS PM proposal must share its decision_id"
    )
    # Research-phase agents are outside the PM/RM decision chain — their
    # rows correctly carry no decision_id (NULL, not a fabricated shared id).
    assert macro_row["decision_id"] is None

    trades = pipeline.db.get_trades(symbol="SPY")
    assert trades, "expected a trades row for the executed SPY BUY"
    assert all(t["decision_id"] == pm_row["decision_id"] for t in trades), (
        "every trades row this run produced must carry the same decision_id "
        "as the PM proposal / RM review that led to it"
    )


def test_position_reviewer_persists_actual_model_on_failover():
    """midday/close (audit site pipeline.py:6109) must persist
    AgentResult.model, not config.llm.position_reviewer_model."""
    pipeline = build_pipeline(
        broker=MagicMock(), macro=MagicMock(), db=MagicMock(), risk_engine=MagicMock(), position_reviewer=MagicMock()
    )
    pipeline.broker.is_trading_day.return_value = True
    pipeline.broker.get_account.return_value = {"cash": 1000.0, "portfolio_value": 5000.0}
    pipeline.broker.get_positions.return_value = [
        Position(
            symbol="SPY",
            qty=10.0,
            avg_entry=500.0,
            current_price=505.0,
            market_value=5050.0,
            unrealized_pnl=50.0,
            sector="ETF",
        )
    ]
    pipeline.macro.get_macro_summary.return_value = {}
    pipeline.db.get_trades.return_value = []
    pipeline.config = _mock_config()
    pipeline._handle_ex_dividends = MagicMock(return_value=[])
    pipeline._run_news_update = MagicMock(return_value=(None, None))
    pipeline._load_earnings_analyses = MagicMock(return_value=(None, []))
    pipeline._midday_execute_llm_actions = MagicMock(return_value=[])
    pipeline._reconcile_fills = MagicMock()
    pipeline.position_reviewer.review.return_value = (
        PositionReview(
            reasoning_chain=_review_rc(),
            actions=[],
            overall_assessment="stable",
            risk_level="low",
        ),
        _agent_result(_ACTUAL_FAILOVER_MODEL),
    )

    result = pipeline.run_midday()

    assert result["status"] == "reviewed"
    kwargs = pipeline.db.insert_agent_log.call_args.kwargs
    assert kwargs["agent_name"] == "position_reviewer"
    assert kwargs["model"] == _ACTUAL_FAILOVER_MODEL
    assert kwargs["model"] != pipeline.config.llm.position_reviewer_model


def test_earnings_preprocess_persists_actual_model_on_failover(tmp_path):
    """earnings_preprocess (audit site pipeline.py:6296) must persist
    AgentResult.model, not config.llm.earnings_analyst_model."""
    pipeline = build_pipeline(db=Database(str(tmp_path / "t.db")))
    pipeline.db.initialize()
    pipeline.broker = MagicMock()
    pipeline.broker.is_trading_day.return_value = True
    pipeline.config = _mock_config()

    new_filing = EarningsReport(
        symbol="NVDA",
        form_type="10-Q",
        filing_date="2026-04-20",
        filing_path="/tmp/nvda.html",
        analysis_path="/tmp/nvda.md",
        text_excerpt="...",
        is_new=True,
    )
    earnings_provider = MagicMock()
    earnings_provider.check_and_fetch.return_value = [new_filing]
    earnings_provider.confirm_filing.return_value = None
    pipeline.earnings_provider = earnings_provider

    earnings_analyst = MagicMock()
    agent_result = _agent_result(_ACTUAL_FAILOVER_MODEL)
    earnings_analyst.analyze_reports.return_value = [
        {
            "symbol": "NVDA",
            "is_new": True,
            "form_type": "10-Q",
            "filing_date": "2026-04-20",
            "agent_result": agent_result,
            "analysis": {"investment_implications": {"sentiment": "bullish", "conviction": "high"}},
        }
    ]
    pipeline.earnings_analyst = earnings_analyst

    result = pipeline.run_earnings_preprocess()

    assert result["status"] == "preprocessed"
    logs = pipeline.db.get_agent_logs(result["run_id"])
    row = next(r for r in logs if r["agent_name"] == "earnings_analyst_preprocess")
    assert row["model"] == _ACTUAL_FAILOVER_MODEL
    assert row["model"] != pipeline.config.llm.earnings_analyst_model
    pipeline.db.close()


def test_evening_analyst_persists_actual_model_on_failover():
    """evening (audit site pipeline.py:6668) must persist AgentResult.model,
    not config.llm.evening_analyst_model."""
    pipeline = build_pipeline(
        news_provider=MagicMock(),
        news_analyst=MagicMock(),
        earnings_provider=MagicMock(),
        earnings_analyst=MagicMock(),
        broker=MagicMock(),
        db=MagicMock(),
        macro=MagicMock(),
        evening_analyst=MagicMock(),
    )
    pipeline.config = _mock_config()

    pipeline.broker.is_trading_day.return_value = True
    pipeline.broker.get_account.return_value = {"portfolio_value": 10200.0, "last_equity": 10000.0}
    pipeline.broker.get_positions.return_value = []
    pipeline.db.get_trades.return_value = []
    pipeline.macro.get_macro_summary.return_value = {}
    pipeline.evening_analyst.analyze.return_value = (
        EveningReport(
            reasoning_chain=_evening_rc(),
            daily_summary="Up",
            lessons="n/a",
            tomorrow_outlook="Watch",
            risk_rating="low",
        ),
        _agent_result(_ACTUAL_FAILOVER_MODEL),
    )

    result = pipeline.run_evening()

    assert result["status"] != "market_holiday"
    kwargs = pipeline.db.insert_agent_log.call_args.kwargs
    assert kwargs["agent_name"] == "evening_analyst"
    assert kwargs["model"] == _ACTUAL_FAILOVER_MODEL
    assert kwargs["model"] != pipeline.config.llm.evening_analyst_model


_VALID_META_REFLECTION_JSON = json.dumps(
    {
        "period": "2026-Q1",
        "meta_reasoning_chain": {
            "performance_vs_benchmark": "Alpha -3.6% over 60 days, DD -5.2%",
            "secular_theme_audit": "nuclear/power ran 4x in missed_themes; we held 0",
            "loss_autopsy_audit": "greed_top_chasing 3x -32% alpha leak",
            "self_portrait_synthesis": (
                "conviction_calibration: HIGH 38% vs LOW 62% inverted. "
                "theme_breadth: tech-only, 4 of 6 misses in energy/materials. "
                "loss_discipline: 3 wrongs rode thesis-break trigger. "
                "execution_style: 7d avg hold vs medium-long mandate. "
                "agent_balance: news_analyst 0 HIGH state_changes on energy."
            ),
            "portrait_gap_diagnosis": (
                "Top 2 gaps: (1) theme_breadth owned by news_analyst "
                "(4 missed themes, 0 HIGH state_changes). (2) "
                "conviction_calibration owned by PM (24 pp HIGH vs LOW inversion)."
            ),
            "existing_prompt_audit": (
                "Gap 1: news_analyst.md has no energy/materials coverage rule; "
                "Learnings section empty -> append room. Gap 2: "
                "portfolio_manager.md Step 5 has sizing scale but no "
                "calibration feedback; Learnings has 1 entry on rr (different "
                "axis) -> distinct append ok."
            ),
            "prompt_edit_reasoning": ("greed_top_chasing worsened 2->3; news gap is 0 HIGH hits in 46 sessions"),
        },
        "style_self_portrait": (
            "We are trend-followers more than trend-identifiers. Strong in "
            "tech / AI themes but blind to energy + materials sectors. "
            "Losses concentrate in greed-driven entries."
        ),
        "persistent_blindspots": ["nuclear/power sector"],
        "root_cause_hypotheses": ["news prompt skews tech"],
        "theme_coverage_report": {
            "themes_caught_early": [],
            "themes_caught_late": [],
            "themes_missed_entirely": ["nuclear/power", "rare-earth"],
            "emerging_themes_to_watch": [],
            "mispricing_patterns": [],
        },
        "loss_pattern_report": {
            "top_patterns": [
                {
                    "root_cause": "greed_top_chasing",
                    "occurrences": 3,
                    "total_loss_pct": -36.0,
                    "example_trades": ["MU 2026-01 -15%"],
                    "attributable_agent": "tech_analyst",
                    "proposed_guard": (
                        "Flag entries within 2% of 20-day high without a confirming fundamental driver."
                    ),
                }
            ],
            "systemic_vs_alpha_split": "72% alpha, 28% systemic",
            "worst_single_trade": "MU -15% 2026-01",
            "corrigibility_score": "degrading",
        },
        "proposed_learnings": [
            {
                "agent_name": "tech_analyst",
                "operation": "append",
                "learning_text": (
                    "Flag entries within 2% of 20-day high unless a confirming "
                    "fundamental driver is in reasoning_chain."
                ),
                "justification": (
                    "Q1 2026 saw 3 of 5 wrongs in greed_top_chasing for -32% "
                    "alpha leak; all entered within 2% of 20-day high."
                ),
            }
        ],
        "confidence": "medium",
    }
)


def test_meta_reflector_persists_actual_model_on_failover(tmp_path):
    """quarterly meta (audit site pipeline.py:7050) must persist
    AgentResult.model, not config.llm.meta_reflector_model."""
    pipeline = build_pipeline(db=Database(str(tmp_path / "t.db")))
    pipeline.db.initialize()
    pipeline.market = MagicMock()
    pipeline.market.get_ohlcv.return_value = []
    pipeline.broker = MagicMock()
    pipeline.broker.is_last_trading_day_of_quarter.return_value = False
    pipeline.config = _mock_config()
    pipeline.meta_reflector = MagicMock()

    reflection = QuarterlyMetaReflection.model_validate(json.loads(_VALID_META_REFLECTION_JSON))
    pipeline.meta_reflector.analyze.return_value = (
        reflection,
        _agent_result(_ACTUAL_FAILOVER_MODEL),
    )

    result = pipeline.run_quarterly_meta_reflection(
        force=True,
        period_end=date(2026, 2, 15),
        evolution_root=str(tmp_path),
    )

    assert result["status"] == "reflected"
    logs = pipeline.db.get_agent_logs(result["run_id"])
    row = next(r for r in logs if r["agent_name"] == "meta_reflector")
    assert row["model"] == _ACTUAL_FAILOVER_MODEL
    assert row["model"] != pipeline.config.llm.meta_reflector_model
    pipeline.db.close()
