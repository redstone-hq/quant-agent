"""A credential-gateway / LLM-provider outage must not reach the broker.

`docs/WORK.md`'s commissioning checklist requires objective evidence that
"failure of OneCLI or Mission Control fails safely and does not create a
path to unauthorized live trading." The Mission Control half of that is
covered in `tests/test_api_contract.py`. This file covers the trading-engine
half, which the OneCLI dependency newly makes relevant: when the credential
gateway is down, every LLM call raises after its retry budget
(`BaseAgent._execute` re-raises the primary error when no failover
succeeds), and the question is what the pipeline does with that.

The invariant asserted here is deliberately the blunt one — **no order is
submitted** — because that is the property that actually matters for
safety, and it holds however the failure is reported upward.

These are behaviour tests only: nothing in `src/` is changed by this file,
and none of them assert a *new* semantic. They pin the existing fail-closed
behaviour so a future refactor cannot quietly turn an agent outage into a
session that trades on partial reasoning.

Harness mirrors `tests/test_pipeline.py` (same patch stack, same mock
shapes); the config fixture is duplicated locally rather than promoted to
conftest so this file's failure modes stay self-contained.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from src.models import (
    PortfolioDecision,
    RiskVerdict,
    TargetPosition,
    TechAnalysisResult,
)
from src.pipeline import TradingPipeline

# Reuse test_pipeline.py's model stubs rather than re-deriving them — they
# already track the real pydantic shapes, and a second copy would drift.
from tests.test_pipeline import (  # noqa: E402
    _macro_stub,
    _mock_agent_result,
    _news_stub,
    _pm_rc,
    _risk_rc,
    _today_snapshot,
    _trc,
)


class CredentialGatewayDown(RuntimeError):
    """Stands in for what a dead OneCLI gateway surfaces as.

    In the real deployment the SDK raises a connection/proxy error that
    `BaseAgent._execute` retries and then re-raises. The exact type is not
    the point — the point is that an agent call raises rather than
    returning a decision.
    """


@pytest.fixture
def mock_config():
    cfg = MagicMock()
    cfg.api_keys.anthropic = "test-key"
    cfg.api_keys.fred = "fred-key"
    cfg.api_keys.alpaca_key = "alp-key"
    cfg.api_keys.alpaca_secret = "alp-secret"
    cfg.alpaca.paper = True
    for agent in (
        "tech_analyst",
        "news_analyst",
        "macro_analyst",
        "earnings_analyst",
        "portfolio_manager",
        "risk_manager",
        "position_reviewer",
        "evening_analyst",
    ):
        setattr(cfg.llm, f"{agent}_model", "openai/gpt-5.5")
    cfg.llm.max_tokens = 4096
    cfg.risk.max_position_pct = 20
    cfg.risk.max_total_position_pct = 90
    cfg.risk.max_sector_pct = 40
    cfg.risk.require_stop_loss = True
    cfg.trading.universe = ["SPY"]
    cfg.trading.lookback_days = 120
    cfg.storage.db_path = ":memory:"
    return cfg


def _tech_analysis() -> TechAnalysisResult:
    return TechAnalysisResult(
        symbol="SPY",
        rating="buy",
        entry_price=507.0,
        reference_target=545.0,
        stop_loss=490.0,
        support_levels=[490.0],
        resistance_levels=[545.0],
        # Python-set in production; the constructor derives the take-profit
        # from these and refuses without them (2026-09-01).
        computed_levels=[490.0, 545.0],
        atr_14=17.0 / 3.5,
        setup_type="range",
        expected_horizon_sessions=60,
        reasoning="Bullish",
        reasoning_chain=_trc(),
        thesis_invalid_if="closes below support",
    )


def _pm_decision() -> PortfolioDecision:
    return PortfolioDecision(
        reasoning_chain=_pm_rc(),
        targets=[
            TargetPosition(
                symbol="SPY",
                target_weight_pct=20.0,
                conviction="high",
                thesis="Buy",
                thesis_invalid_if="closes below support",
            )
        ],
        portfolio_view="Bullish",
    )


def _risk_verdict() -> RiskVerdict:
    return RiskVerdict(
        approved=True,
        modifications=[],
        reasoning="Approved",
        reasoning_chain=_risk_rc(),
    )


def _wire_happy_path(mocks, tmp_path, cfg):
    """Everything green except whatever the caller then breaks."""
    (
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
    ) = mocks

    cfg.storage.db_path = str(tmp_path / "test.db")

    mock_ta = MagicMock()
    mock_ta.analyze_batch.return_value = ({"SPY": _tech_analysis()}, _mock_agent_result())
    mock_ta_cls.return_value = mock_ta

    mock_pm = MagicMock()
    mock_pm.decide.return_value = (_pm_decision(), _mock_agent_result())
    mock_pm_cls.return_value = mock_pm

    mock_rm = MagicMock()
    mock_rm.review.return_value = (_risk_verdict(), _mock_agent_result())
    mock_rm_cls.return_value = mock_rm

    mock_market = MagicMock()
    mock_market.get_ohlcv.return_value = [
        MagicMock(date="2026-08-11", open=503, high=510, low=500, close=507, volume=1_000_000)
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
    mock_broker.get_intraday_snapshots.return_value = {"SPY": _today_snapshot(507.0)}
    mock_broker.get_account.return_value = {"cash": 10000.0, "portfolio_value": 10000.0}
    mock_broker.get_positions.return_value = []
    mock_broker.submit_order.return_value = {"id": "order-1", "status": "accepted", "symbol": "SPY"}
    mock_broker_cls.return_value = mock_broker

    mock_maa = MagicMock()
    mock_maa.analyze.return_value = (_macro_stub(), _mock_agent_result())
    mock_maa_cls.return_value = mock_maa

    mock_na = MagicMock()
    # NewsAnalystAgent.analyze() -> tuple[NewsIntelligenceReport | None,
    # AgentResult]. A real report, not None: since docs/WORK.md item 20, a
    # None here is `data_status["news"] = "parse_error"` and the evidence
    # gate refuses the decision, so the CONTROL case would stop trading and
    # prove nothing (mirrors tests/test_pipeline.py's fixtures).
    mock_na.analyze.return_value = (_news_stub(), _mock_agent_result())
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

    return mock_broker, mock_pm, mock_rm


def _run_morning(cfg):
    """Run a morning session, tolerating an abort-by-exception.

    Whether the pipeline swallows the agent failure into a non-executed
    result or lets it propagate is an implementation detail; both are
    fail-closed. The assertion that matters is made by the caller on the
    broker mock.
    """
    try:
        return TradingPipeline(cfg).run_morning()
    except Exception as exc:  # noqa: BLE001 — aborting IS a safe outcome
        return {"status": f"aborted:{type(exc).__name__}"}


_PATCHES = (
    patch("src.pipeline.AlpacaBroker"),
    patch("src.pipeline.EarningsDataProvider"),
    patch("src.pipeline.EarningsAnalystAgent"),
    patch("src.pipeline.NewsDataProvider"),
    patch("src.pipeline.NewsAnalystAgent"),
    patch("src.pipeline.MacroAnalystAgent"),
    patch("src.pipeline.MacroDataProvider"),
    patch("src.pipeline.MarketDataProvider"),
    patch("src.pipeline.RiskManagerAgent"),
    patch("src.pipeline.PortfolioManagerAgent"),
    patch("src.pipeline.TechAnalystAgent"),
    patch("src.stage_morning_research.compute_indicators"),
    patch("src.data.technical.compute_indicators"),
)


@pytest.fixture
def wired(mock_config, tmp_path):
    """Start every collaborator patched and green; yield the seams a test
    wants to break, then tear the patches down."""
    started = [p.start() for p in _PATCHES]
    try:
        (
            mock_broker_cls,
            mock_edp_cls,
            mock_ea_cls,
            mock_ndp_cls,
            mock_na_cls,
            mock_maa_cls,
            mock_macro_cls,
            mock_market_cls,
            mock_rm_cls,
            mock_pm_cls,
            mock_ta_cls,
            _ci_stages,
            _ci,
        ) = started
        broker, pm, rm = _wire_happy_path(
            (
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
            ),
            tmp_path,
            mock_config,
        )
        yield mock_config, broker, pm, rm, mock_ta_cls.return_value
    finally:
        for p in _PATCHES:
            p.stop()


# ---------------------------------------------------------------------------


def test_happy_path_control_actually_trades(wired):
    """Control case. Without this, every assertion below could pass simply
    because the harness never reaches the broker at all."""
    cfg, broker, _pm, _rm, _ta = wired
    result = _run_morning(cfg)
    assert result["status"] == "executed"
    broker.submit_order.assert_called_once()


def test_portfolio_manager_outage_submits_no_orders(wired):
    """The PM is where a proposal comes from. If the credential gateway is
    down it raises, and nothing may be sent to the broker on its behalf."""
    cfg, broker, pm, _rm, _ta = wired
    pm.decide.side_effect = CredentialGatewayDown("gateway unreachable")

    result = _run_morning(cfg)
    assert result["status"] != "executed"
    broker.submit_order.assert_not_called()


def test_risk_manager_outage_submits_no_orders(wired):
    """The AI Risk Manager sits between the PM proposal and execution. Its
    unavailability must not be read as 'no objection' — uncertainty fails
    closed (CLAUDE.md hard boundary)."""
    cfg, broker, _pm, rm, _ta = wired
    rm.review.side_effect = CredentialGatewayDown("gateway unreachable")

    result = _run_morning(cfg)
    assert result["status"] != "executed"
    broker.submit_order.assert_not_called()


def test_total_llm_outage_submits_no_orders(wired):
    """Every agent down at once — what a dead OneCLI gateway actually looks
    like, since all nine agents route through the same credential path."""
    cfg, broker, pm, rm, ta = wired
    outage = CredentialGatewayDown("gateway unreachable")
    ta.analyze_batch.side_effect = outage
    pm.decide.side_effect = outage
    rm.review.side_effect = outage

    result = _run_morning(cfg)
    assert result["status"] != "executed"
    broker.submit_order.assert_not_called()


def test_broker_outage_submits_no_orders(wired):
    """The other half of the dependency: credentials fine, broker down.
    Sizing has no account snapshot to work from, so nothing may be sent."""
    cfg, broker, _pm, _rm, _ta = wired
    broker.get_account.side_effect = CredentialGatewayDown("broker unreachable")

    result = _run_morning(cfg)
    assert result["status"] != "executed"
    broker.submit_order.assert_not_called()
