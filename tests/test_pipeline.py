import pytest
import json
from unittest.mock import patch, MagicMock, AsyncMock
from tests.session_clock import todays_session_stamp
from tests.fakes.held_book import hold
from src.pipeline import TradingPipeline
from src.agents.base import AgentResult
from src.cost_circuit import PaidAnalysisSuspended
from src.models import (
    TechAnalysisResult,
    PortfolioDecision,
    TradeDecision,
    RiskVerdict,
    Position,
    TargetPosition,
    MacroAnalysis,
    MacroReasoningChain,
    MacroPositionGuidance,
    NewsIntelligenceReport,
    MacroNarrative,
    PositionReview,
    PositionReasoningChain,
    PositionAction,
    ReasoningChain,
    RiskReasoningChain,
    TechReasoningChain,
)
from tests.pipeline_factory import build_pipeline
from tests.fakes.confirmed_cancel import confirm_cancels


def _news_stub():
    """A minimal, VALID NewsIntelligenceReport for pipeline fixtures.

    Required since docs/WORK.md item 20: a news seat that returns None is
    `data_status["news"] = "parse_error"`, which the evidence gate reads as
    an answer that never arrived and refuses the decision on. Fixtures that
    are testing execution/sizing/risk paths must give the seat a real
    answer, or they end up asserting against the gate instead.
    """
    return NewsIntelligenceReport(
        macro_narrative=MacroNarrative(
            last_updated="2026-04-07",
            era_themes=["AI capex"],
            current_regime="risk-on",
        ),
        state_changes=[],
        stock_news={},
        pm_briefing="stub",
        market_sentiment="neutral",
        confidence="medium",
    )


def _today_snapshot(price: float) -> dict:
    """A `get_intraday_snapshots` payload with a real print from TODAY.

    Item 120: sizing a new-name BUY now goes through
    `src.data.live_price.resolve_live_price`, which requires a fresh today
    print (or today's forming session bar) rather than the bare
    `get_latest_price` mock these fixtures used to rely on. Stamped via
    `tests.session_clock.todays_session_stamp` so it resolves as today's
    print at any hour the suite runs, not only 09:30-16:00 ET.
    """
    return {"last_price": price, "last_trade_at": todays_session_stamp()}


def _mock_stop_seam(broker, *, specs=(), snapshot_ok=True, cancel_ok=True):
    """Wire a MagicMock broker's split stop-cancel seam (audit F1 #1).

    SELL paths now call broker.snapshot_protective_stops (read) then
    broker.cancel_snapshotted_stops (mutate) via the pipeline's
    write-ahead orchestrator, not the old monolithic
    cancel_protective_stops. This sets all three consistently so a test
    can keep expressing intent as "stops present + cancel cleanly"
    (default) or a failure (snapshot_ok / cancel_ok = False).
    """
    specs = list(specs)
    broker.snapshot_protective_stops.return_value = (snapshot_ok, specs)
    broker.cancel_snapshotted_stops.return_value = MagicMock(cleared=cancel_ok, coverage_shrank=False)
    cleared = snapshot_ok and cancel_ok
    broker.cancel_protective_stops.return_value = (
        cleared,
        specs if cleared else [],
    )


def _mock_amend_seam(broker, *, specs, kept_qty, held_after, sell_status, fill_info, sell_id):
    """Wire a MagicMock broker for a TRIM: the whole-share stop is PATCHED
    down to `kept_qty` (new id, old order confirmed `replaced`), never
    cancelled; after the sell the position reads `held_after` and the live
    book is the shrunk leg, so the quantity invariant can grow it back."""
    old = specs[0]
    new_id = f"{old['id']}-replaced"
    broker.snapshot_protective_stops.side_effect = [
        (True, list(specs)),
        (True, [{**old, "id": new_id, "qty": float(kept_qty)}]),
        (True, [{**old, "id": new_id, "qty": float(held_after)}]),
    ]
    broker._amend_one_stop_price.side_effect = lambda **kw: {
        "outcome": "amended",
        "new_id": new_id,
        "new_qty": kw.get("new_qty"),
        "detail": "",
    }
    broker.wait_for_order_terminal.side_effect = lambda oid, **kw: "replaced" if oid == old["id"] else sell_status
    broker.get_order_fill_info.side_effect = lambda oid: {"status": "new"} if oid == new_id else fill_info
    broker.get_positions.return_value = [
        Position(
            symbol="NVDA",
            qty=held_after,
            avg_entry=100,
            current_price=135,
            market_value=1,
            unrealized_pnl=0,
            sector="Technology",
        )
    ]
    broker.submit_order.return_value = {
        "id": sell_id,
        "status": "rejected" if sell_status == "rejected" else "accepted",
        "symbol": "NVDA",
    }


def _qty_amends(broker):
    return [c.kwargs["new_qty"] for c in broker._amend_one_stop_price.call_args_list]


def _assert_trim_never_naked(broker, amends):
    """What every trim test protects: the stop is never cancelled, never
    restored from nothing, and no PATCH ever takes its quantity to zero."""
    broker.cancel_snapshotted_stops.assert_not_called()
    broker.cancel_protective_stops.assert_not_called()
    broker._restore_stop_orders.assert_not_called()
    assert _qty_amends(broker) == amends and min(amends) > 0


def _partial_trim(pipeline, position, *, qty, run_id, label="REDUCE"):
    """Drive the shared partial-exit path exactly as a reviewer trim does:
    protected SELL, then finalize protection on the ACTUAL residual.

    The tests below used to ride on the midday auto take-profit (deleted
    2026-09-12, owner decision — the trailing stop is the only exit rule).
    The invariant they pin — cancelled stops are restored when the sell
    fails, or re-placed on the true residual when it fills — belongs to
    `_submit_protected_sell` / `_finalize_pending_protections`, not to the
    deleted trigger, so the tests keep the invariant and lose the trigger.
    """
    del run_id  # the deleted trigger wrote its own audit row; a trim here does not
    orders: list[dict] = []
    pending: list[dict] = []
    sale = pipeline._submit_protected_sell(
        symbol=position.symbol,
        qty=qty,
        limit_price=round(position.current_price * 0.995, 2),
        reference_price=position.current_price,
        position_qty_before_sell=position.qty,
        label=label,
    )
    if sale is not None:
        order, prot = sale
        orders.append(order)
        pending.append(prot)
    pipeline._finalize_pending_protections(pending, context="test_partial_trim")
    return orders


def _mock_stage_seam(pipeline, *, specs=(), ok=True, wal_row_id=None):
    """For tests where the WHOLE pipeline is a MagicMock: ExecutionStage
    (and the other SELL paths) now obtain stops via
    pipeline._cancel_stops_with_write_ahead (audit F1 #1), so its
    3-tuple return must be stubbed directly — _mock_stop_seam only wires
    the broker, which a fully-mocked pipeline never reaches."""
    pipeline._cancel_stops_with_write_ahead.return_value = (
        ok,
        list(specs),
        wal_row_id,
    )
    # Callers unpack finalize's (ok, retry_specs) contract; default to
    # "coverage confirmed" so the full-MagicMock pipeline yields a tuple.
    pipeline._finalize_protection_after_sell.return_value = (True, [])
    # Bind the REAL protected-sell helpers onto the mock so the extracted
    # cancel→submit→accept→restore + wait→finalize discipline actually runs
    # against the mocked broker/seams (real integration, not a no-op mock).
    import types as _types
    from src.pipeline import TradingPipeline as _TP

    pipeline._submit_protected_sell = _types.MethodType(
        _TP._submit_protected_sell,
        pipeline,
    )
    pipeline._finalize_pending_protections = _types.MethodType(
        _TP._finalize_pending_protections,
        pipeline,
    )


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


def _review_rc():
    """Stub PositionReasoningChain for tests that construct PositionReview."""
    return PositionReasoningChain(
        macro_continuity_check="stable",
        thesis_progress_check="ok",
        thesis_integrity_check="no triggers",
        winners_discipline_check="no flags",
        session_disposition_check="patient",
        execution_rationale="n/a",
    )


def _macro_stub(regime="risk-on", outlook="bullish", confidence="medium", target_invested_pct=75.0, cash_rec_pct=25.0):
    """Build a valid MacroAnalysis Pydantic object for pipeline tests.

    Phase 4 #7 made MacroAnalystAgent.analyze() return MacroAnalysis
    (Pydantic) instead of dict. Tests that mock the agent must return
    the typed object so downstream consumers' attribute access works.
    """
    return MacroAnalysis(
        reasoning_chain=MacroReasoningChain(
            volatility_analysis="a",
            yield_curve_analysis="b",
            monetary_policy_analysis="c",
            inflation_labor_credit="d",
            cross_signal_synthesis="e",
            sector_implications="f",
        ),
        regime=regime,
        confidence=confidence,
        equity_outlook=outlook,
        position_guidance=MacroPositionGuidance(
            target_invested_pct=target_invested_pct,
            cash_recommendation_pct=cash_rec_pct,
            reasoning="stub",
        ),
        summary="stub macro analysis",
    )


def _mock_agent_result(raw_text="{}"):
    return AgentResult(raw_text=raw_text, tokens_used=100, model="test", user_message="test input")


@pytest.fixture
def mock_config():
    cfg = MagicMock()
    cfg.api_keys.anthropic = "test-key"
    cfg.api_keys.fred = "fred-key"
    cfg.api_keys.alpaca_key = "alp-key"
    cfg.api_keys.alpaca_secret = "alp-secret"
    cfg.alpaca.paper = True
    cfg.llm.tech_analyst_model = "claude-sonnet-4-6-20250514"
    cfg.llm.news_analyst_model = "claude-sonnet-4-6-20250514"
    cfg.llm.macro_analyst_model = "claude-sonnet-4-6-20250514"
    cfg.llm.earnings_analyst_model = "claude-opus-4-6-20250725"
    cfg.llm.portfolio_manager_model = "claude-opus-4-6-20250725"
    cfg.llm.risk_manager_model = "claude-opus-4-6-20250725"
    cfg.llm.position_reviewer_model = "claude-opus-4-6-20250725"
    cfg.llm.evening_analyst_model = "claude-opus-4-6-20250725"
    cfg.llm.max_tokens = 4096
    cfg.risk.max_position_pct = 20
    cfg.risk.max_total_position_pct = 90
    cfg.risk.max_sector_pct = 40
    cfg.risk.require_stop_loss = True
    cfg.trading.universe = ["SPY", "QQQ"]
    cfg.trading.lookback_days = 120
    cfg.storage.db_path = ":memory:"
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
def test_pipeline_morning_run_buy(
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
    mock_config,
    tmp_path,
):
    mock_config.storage.db_path = str(tmp_path / "test.db")
    mock_config.llm.earnings_analyst_model = "claude-opus-4-6-20250725"

    # Tech Analyst batch returns buy for SPY
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
        # Divisor 4.5 clears the WIDEST stop multiple these tests can hit
        # (3.0 base x 1.15 range x 1.20 risk-off = 4.14), so the structural
        # stop survives untouched whatever regime the test stubs.
        #
        # Resistance is $545, not $530, and that is load-bearing. Since
        # 2026-09-02 the constructor's 1.5 reward:risk floor is applied to
        # the SHIPPING geometry on every path, including a stop already
        # outside the ATR band — which is this one. At $530 the ratio is
        # (530-507)/(507-490) = 1.35 and the entry is refused on geometry,
        # which is not what these pipeline tests are about. At $545 it is
        # 2.24 and the constructor places the trade.
        computed_levels=[490.0, 545.0],
        atr_14=17.0 / 4.5,
        setup_type="range",
        expected_horizon_sessions=60,
        reasoning="Bullish",
        reasoning_chain=_trc(),
        thesis_invalid_if="closes below support",
    )
    mock_ta.analyze_batch.return_value = ({"SPY": spy_analysis}, _mock_agent_result())
    mock_ta_cls.return_value = mock_ta

    # Portfolio Manager emits a target (not a TradeDecision) — Phase 2:
    # the constructor derives the actual order from target + TA + live price.
    mock_pm = MagicMock()
    mock_pm.decide.return_value = (
        PortfolioDecision(
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
        ),
        _mock_agent_result(),
    )
    mock_pm_cls.return_value = mock_pm

    # Risk Manager approves
    mock_rm = MagicMock()
    mock_rm.review.return_value = (
        RiskVerdict(
            approved=True,
            modifications=[],
            reasoning="Approved",
            reasoning_chain=_risk_rc(),
        ),
        _mock_agent_result(),
    )
    mock_rm_cls.return_value = mock_rm

    # Market data
    mock_market = MagicMock()
    mock_market.get_ohlcv.return_value = [
        MagicMock(date="2026-04-07", open=503, high=510, low=500, close=507, volume=1000000)
    ]
    mock_market_cls.return_value = mock_market

    # Macro data
    mock_macro = MagicMock()
    mock_macro.get_macro_summary.return_value = {
        "vix": {"current": 18.0, "mean_5d": 17.5, "trend": "falling"},
        "treasury": {"us2y": 4.5, "us10y": 4.3, "spread_2_10": -0.2, "inverted": True},
        "fed_funds_rate": 5.25,
    }
    mock_macro_cls.return_value = mock_macro

    # Broker
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

    # Macro analyst
    mock_maa = MagicMock()
    mock_maa.analyze.return_value = (_macro_stub(regime="risk-on", outlook="bullish"), _mock_agent_result())
    mock_maa_cls.return_value = mock_maa

    # News
    mock_na = MagicMock()
    # NewsAnalystAgent.analyze() -> tuple[NewsIntelligenceReport | None,
    # AgentResult]. This used to hand back None — "the pipeline tolerates a
    # newsless run" — and that is no longer true, deliberately: since
    # docs/WORK.md item 20 shipped, a seat that was asked and whose answer
    # never arrived REFUSES the decision rather than being tolerated, so a
    # None here would make every fixture below assert against
    # `evidence_gate_skip` instead of the thing it is actually testing.
    # PM/RM are still mocked, so no production code reads the content.
    mock_na.analyze.return_value = (_news_stub(), _mock_agent_result())
    mock_na_cls.return_value = mock_na
    mock_ndp = MagicMock()
    mock_ndp.fetch_news.return_value = ([], None)  # (items, coverage) — see src/data/news.py NewsCoverage
    mock_ndp.format_for_prompt.return_value = "No news."
    mock_ndp_cls.return_value = mock_ndp

    # Earnings
    mock_ea = MagicMock()
    mock_ea.analyze_reports.return_value = []
    mock_ea_cls.return_value = mock_ea
    mock_edp = MagicMock()
    mock_edp.check_and_fetch.return_value = []
    mock_edp_cls.return_value = mock_edp

    pipeline = TradingPipeline(mock_config)
    result = pipeline.run_morning()

    assert result["status"] == "executed"
    assert len(result["orders"]) == 1
    mock_broker.submit_order.assert_called_once()


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
def test_pipeline_morning_run_persists_specialist_evidence(
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
    mock_config,
    tmp_path,
):
    """Stage 4: an ordinary morning run persists already-validated structured
    evidence into `specialist_evidence` for every specialist + PM + RM, with
    natural (symbol vs run) scope and correct run_id/decision_id correlation
    — proving evidence is written from validated model output and can be
    read back for the correct run/scope, without the API/client ever having
    to re-parse raw agent_logs.full_response text. Mirrors
    test_pipeline_morning_run_buy's fixture exactly; only the assertions
    differ."""
    import sqlite3

    mock_config.storage.db_path = str(tmp_path / "test.db")
    mock_config.llm.earnings_analyst_model = "claude-opus-4-6-20250725"

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
        # Divisor 4.5 clears the WIDEST stop multiple these tests can hit
        # (3.0 base x 1.15 range x 1.20 risk-off = 4.14), so the structural
        # stop survives untouched whatever regime the test stubs.
        computed_levels=[490.0, 545.0],
        atr_14=17.0 / 4.5,
        setup_type="range",
        expected_horizon_sessions=60,
        reasoning="Bullish",
        reasoning_chain=_trc(),
        thesis_invalid_if="closes below support",
    )
    mock_ta.analyze_batch.return_value = ({"SPY": spy_analysis}, _mock_agent_result())
    mock_ta_cls.return_value = mock_ta

    mock_pm = MagicMock()
    mock_pm.decide.return_value = (
        PortfolioDecision(
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
        ),
        _mock_agent_result(),
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
        _mock_agent_result(),
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
    mock_broker.get_intraday_snapshots.return_value = {"SPY": _today_snapshot(507.0)}
    mock_broker.get_account.return_value = {"cash": 10000.0, "portfolio_value": 10000.0}
    mock_broker.get_positions.return_value = []
    mock_broker.submit_order.return_value = {"id": "order-1", "status": "accepted", "symbol": "SPY"}
    mock_broker_cls.return_value = mock_broker

    mock_maa = MagicMock()
    mock_maa.analyze.return_value = (_macro_stub(regime="risk-on", outlook="bullish"), _mock_agent_result())
    mock_maa_cls.return_value = mock_maa

    mock_na = MagicMock()
    # NewsAnalystAgent.analyze() -> tuple[NewsIntelligenceReport | None,
    # AgentResult]. This used to hand back None — "the pipeline tolerates a
    # newsless run" — and that is no longer true, deliberately: since
    # docs/WORK.md item 20 shipped, a seat that was asked and whose answer
    # never arrived REFUSES the decision rather than being tolerated, so a
    # None here would make every fixture below assert against
    # `evidence_gate_skip` instead of the thing it is actually testing.
    # PM/RM are still mocked, so no production code reads the content.
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

    pipeline = TradingPipeline(mock_config)
    result = pipeline.run_morning()
    assert result["status"] == "executed"

    conn = sqlite3.connect(mock_config.storage.db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT agent_name, kind, scope, symbol, decision_id, evidence_json FROM specialist_evidence ORDER BY id"
    ).fetchall()
    by_key = {(r["agent_name"], r["kind"], r["symbol"]): r for r in rows}

    # Research-phase evidence: run-scoped, no decision_id (mirrors the
    # existing agent_logs.decision_id contract — never assigned to
    # research-phase rows; see docs/WORK.md Stage 4 boundary).
    macro_row = by_key[("macro_analyst", "analysis", None)]
    assert macro_row["scope"] == "run"
    assert macro_row["decision_id"] is None
    assert json.loads(macro_row["evidence_json"])["regime"] == "risk-on"

    tech_row = by_key[("tech_analyst", "analysis", "SPY")]
    assert tech_row["scope"] == "symbol"
    assert tech_row["decision_id"] is None
    assert json.loads(tech_row["evidence_json"])["rating"] == "buy"

    # Note: this fixture's `mock_na.analyze` stub returns (None, ...) — see
    # its comment above — so news_intel is None here and no news row is
    # written, same as the pre-existing test_pipeline_morning_run_buy
    # fixture, which doesn't assert on news either. News evidence
    # persistence (run-scoped, same code path as macro) is covered directly
    # in tests/test_pipeline_stages.py.

    # Decision-phase evidence: correlated to the SAME decision_id the PM/RM
    # agent_logs rows and the resulting `trades` row carry.
    decision_id = conn.execute("SELECT decision_id FROM trades WHERE symbol = 'SPY' LIMIT 1").fetchone()[0]
    assert decision_id, "expected the executed SPY trade to carry a decision_id"

    target_row = by_key[("portfolio_manager", "target", "SPY")]
    assert target_row["scope"] == "symbol"
    assert target_row["decision_id"] == decision_id
    assert json.loads(target_row["evidence_json"])["target_weight_pct"] == 20.0

    proposed_row = by_key[("portfolio_manager", "proposed_order", "SPY")]
    assert proposed_row["decision_id"] == decision_id
    assert json.loads(proposed_row["evidence_json"])["action"] == "BUY"

    reasoning_row = by_key[("portfolio_manager", "reasoning", None)]
    assert reasoning_row["scope"] == "run"
    assert reasoning_row["decision_id"] == decision_id

    verdict_row = by_key[("risk_manager", "verdict", None)]
    assert verdict_row["scope"] == "run"
    assert verdict_row["decision_id"] == decision_id
    assert json.loads(verdict_row["evidence_json"])["approved"] is True

    # No RiskModification in this fixture (approved untouched) — confirm no
    # stray modification row was fabricated.
    assert ("risk_manager", "modification", "SPY") not in by_key

    conn.close()


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
def test_pipeline_market_order_sizes_from_live_market_price(
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
    mock_config,
    tmp_path,
):
    mock_config.storage.db_path = str(tmp_path / "test.db")
    mock_config.llm.earnings_analyst_model = "claude-opus-4-6-20250725"
    mock_config.trading.universe = ["SPY"]

    mock_ta = MagicMock()
    # entry within 5% of the live market ($98 vs $100 = 2% deviation) so the
    # new deviation guard (>5% → skip) doesn't block this test. Intent of the
    # test is still exercised: limit < market → raised to market → sizing
    # uses live broker price.
    spy_analysis = TechAnalysisResult(
        symbol="SPY",
        rating="buy",
        entry_price=98.0,
        reference_target=145.0,
        stop_loss=72.0,
        support_levels=[72.0],
        resistance_levels=[145.0],
        computed_levels=[72.0, 145.0],
        atr_14=26.0 / 4.5,
        setup_type="range",
        expected_horizon_sessions=60,
        reasoning="Bullish",
        reasoning_chain=_trc(),
        thesis_invalid_if="closes below support",
    )
    mock_ta.analyze_batch.return_value = ({"SPY": spy_analysis}, _mock_agent_result())
    mock_ta_cls.return_value = mock_ta

    mock_pm = MagicMock()
    mock_pm.decide.return_value = (
        PortfolioDecision(
            reasoning_chain=_pm_rc(),
            targets=[
                TargetPosition(
                    symbol="SPY",
                    target_weight_pct=10.0,
                    conviction="high",
                    thesis="Buy",
                    thesis_invalid_if="closes below support",
                )
            ],
            portfolio_view="Bullish",
        ),
        _mock_agent_result(),
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
        _mock_agent_result(),
    )
    mock_rm_cls.return_value = mock_rm

    mock_market = MagicMock()
    mock_market.get_ohlcv.return_value = [
        MagicMock(date="2026-04-07", open=84, high=86, low=83, close=85, volume=1000000)
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
    mock_broker.get_latest_price.return_value = 100.0
    mock_broker.get_latest_quote.return_value = {"bid_price": 100.0, "ask_price": 100.0}
    mock_broker.get_intraday_snapshots.return_value = {"SPY": _today_snapshot(100.0)}
    mock_broker.get_account.return_value = {"cash": 10000.0, "portfolio_value": 10000.0}
    mock_broker.get_positions.return_value = []
    mock_broker.submit_order.return_value = {"id": "order-1", "status": "accepted", "symbol": "SPY"}
    mock_broker_cls.return_value = mock_broker

    mock_maa = MagicMock()
    mock_maa.analyze.return_value = (_macro_stub(regime="risk-on", outlook="bullish"), _mock_agent_result())
    mock_maa_cls.return_value = mock_maa

    mock_na = MagicMock()
    # NewsAnalystAgent.analyze() -> tuple[NewsIntelligenceReport | None,
    # AgentResult]. This used to hand back None — "the pipeline tolerates a
    # newsless run" — and that is no longer true, deliberately: since
    # docs/WORK.md item 20 shipped, a seat that was asked and whose answer
    # never arrived REFUSES the decision rather than being tolerated, so a
    # None here would make every fixture below assert against
    # `evidence_gate_skip` instead of the thing it is actually testing.
    # PM/RM are still mocked, so no production code reads the content.
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

    pipeline = TradingPipeline(mock_config)
    result = pipeline.run_morning()

    assert result["status"] == "executed"
    # Verify by-field rather than full-equality so optional kwargs (reference_price
    # for fat-finger guard) don't brittle-break the test.
    mock_broker.submit_order.assert_called_once()
    kw = mock_broker.submit_order.call_args.kwargs
    assert kw["symbol"] == "SPY"
    # Phase 2 sizing: PortfolioConstructor uses TA's stop (72) vs broker's
    # live market (100) → risk_per_share = $28. The ratified 5% risk budget
    # (item 22 fix: `cfg.risk.max_position_risk_pct` is an unset MagicMock
    # here, so `_risk_budget_pct` falls back to the real 5.0 default, not
    # the old stale 0.5) of $10k = $500 at-risk → qty_by_risk = 17 shares.
    # Target's 10% weight ($1000 at $100 = 10 shares) is the binding
    # constraint instead.
    assert kw["qty"] == 10
    assert kw["side"] == "buy"
    assert kw["stop_loss_price"] == 72.0


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
def test_pipeline_risk_rejected(
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
    mock_config,
    tmp_path,
):
    mock_config.storage.db_path = str(tmp_path / "test.db")
    mock_config.llm.earnings_analyst_model = "claude-opus-4-6-20250725"

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
        # Divisor 4.5 clears the WIDEST stop multiple these tests can hit
        # (3.0 base x 1.15 range x 1.20 risk-off = 4.14), so the structural
        # stop survives untouched whatever regime the test stubs.
        computed_levels=[490.0, 545.0],
        atr_14=17.0 / 4.5,
        setup_type="range",
        expected_horizon_sessions=60,
        reasoning="Bullish",
        reasoning_chain=_trc(),
        thesis_invalid_if="closes below support",
    )
    mock_ta.analyze_batch.return_value = ({"SPY": spy_analysis}, _mock_agent_result())
    mock_ta_cls.return_value = mock_ta

    mock_pm = MagicMock()
    mock_pm.decide.return_value = (
        PortfolioDecision(
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
        ),
        _mock_agent_result(),
    )
    mock_pm_cls.return_value = mock_pm

    # Risk Manager refuses the (only) proposed new entry.
    mock_rm = MagicMock()
    # Owner ruling 2026-09-24 (final): the seat has NO whole-batch veto —
    # approved=False alone no longer stops anything. The way the seat removes a
    # trade is by naming it in rejected_symbols. Here SPY is the only proposed
    # entry, so dropping it leaves nothing to execute (per-symbol drops summing
    # to empty), and no order is submitted.
    mock_rm.review.return_value = (
        RiskVerdict(
            approved=False,
            modifications=[],
            rejected_symbols=[{"symbol": "SPY", "reason": "thesis fails on the primary data"}],
            reason_category="signal_fidelity",
            reasoning="SPY refused on its own merits",
            reasoning_chain=_risk_rc(),
        ),
        _mock_agent_result(),
    )
    mock_rm_cls.return_value = mock_rm

    mock_market = MagicMock()
    mock_market.get_ohlcv.return_value = [MagicMock()]
    mock_market_cls.return_value = mock_market

    mock_macro = MagicMock()
    mock_macro.get_macro_summary.return_value = {"vix": {"current": 30.0}}
    mock_macro_cls.return_value = mock_macro

    mock_broker = MagicMock()
    mock_broker.list_open_orders_checked.return_value = (True, [])  # listing read, nothing open
    mock_broker.is_trading_day.return_value = True
    mock_broker.get_latest_price.return_value = 507.0
    mock_broker.get_latest_quote.return_value = {"bid_price": 507.0, "ask_price": 507.0}
    mock_broker.get_intraday_snapshots.return_value = {"SPY": _today_snapshot(507.0)}
    mock_broker.get_account.return_value = {"cash": 10000.0, "portfolio_value": 10000.0}
    mock_broker.get_positions.return_value = []
    mock_broker_cls.return_value = mock_broker

    # Macro analyst
    mock_maa = MagicMock()
    mock_maa.analyze.return_value = (
        _macro_stub(regime="risk-off", outlook="bearish", confidence="high"),
        _mock_agent_result(),
    )
    mock_maa_cls.return_value = mock_maa

    # News
    mock_na = MagicMock()
    # See the sibling fixtures' comment above: None is analyze()'s own real
    # Optional return, not a stand-in for a type nothing produces.
    mock_na.analyze.return_value = (_news_stub(), _mock_agent_result())
    mock_na_cls.return_value = mock_na
    mock_ndp = MagicMock()
    mock_ndp.fetch_news.return_value = ([], None)  # (items, coverage) — see src/data/news.py NewsCoverage
    mock_ndp.format_for_prompt.return_value = "No news."
    mock_ndp_cls.return_value = mock_ndp

    # Earnings
    mock_ea = MagicMock()
    mock_ea.analyze_reports.return_value = []
    mock_ea_cls.return_value = mock_ea
    mock_edp = MagicMock()
    mock_edp.check_and_fetch.return_value = []
    mock_edp_cls.return_value = mock_edp

    pipeline = TradingPipeline(mock_config)
    result = pipeline.run_morning()

    assert result["status"] == "rejected"
    mock_broker.submit_order.assert_not_called()


def test_pipeline_has_trading_day_guard():
    assert hasattr(TradingPipeline, "_is_trading_day")


def test_pipeline_morning_skips_non_trading_day():
    pipeline = build_pipeline(broker=MagicMock())
    pipeline.broker.is_trading_day.return_value = False

    result = pipeline.run_morning()

    assert result["status"] == "market_holiday"
    pipeline.broker.cancel_open_entry_orders.assert_not_called()


def test_pipeline_morning_bails_cleanly_on_broker_snapshot_failure():
    """If Alpaca's get_account / get_positions raises at the snapshot step,
    morning should return a broker_error status rather than propagate and
    leave ctx half-populated. Mirrors the existing run_intra_check guard."""
    pipeline = build_pipeline(broker=MagicMock(), morning_research_stage=MagicMock())
    pipeline.broker.is_trading_day.return_value = True
    pipeline.broker.cancel_open_entry_orders.return_value = None
    pipeline.broker.get_account.side_effect = RuntimeError("Alpaca 503")
    pipeline._reconcile_fills = MagicMock()

    result = pipeline.run_morning()

    assert result["status"] == "broker_error"
    assert "Alpaca 503" in result["error"]
    # Never got past the snapshot — no research, no decision, no execution
    pipeline.morning_research_stage.run.assert_not_called()
    # But reconcile_fills still ran in the finally block — that's correct
    pipeline._reconcile_fills.assert_called_once()


def test_pipeline_morning_early_return_still_reconciles_fills():
    """Even when research returns no analyses (early exit), the morning finally
    block must still sweep broker fills for any orders that made it out."""
    pipeline = build_pipeline(broker=MagicMock(), morning_research_stage=MagicMock(), risk_engine=MagicMock())
    pipeline.broker.is_trading_day.return_value = True
    pipeline.broker.cancel_open_entry_orders.return_value = None
    pipeline.broker.get_account.return_value = {"cash": 1000.0, "portfolio_value": 5000.0}
    pipeline.broker.get_positions.return_value = []
    pipeline._reconcile_fills = MagicMock()
    pipeline.db = MagicMock()  # a real pipeline always owns a db; the reconciler records its outcome on it

    def _populate_empty_research(ctx):
        ctx.analyses = []

    pipeline.morning_research_stage.run.side_effect = _populate_empty_research

    result = pipeline.run_morning()

    assert result["status"] == "no_data"
    pipeline._reconcile_fills.assert_called_once()


def test_pipeline_midday_skips_non_trading_day():
    pipeline = build_pipeline(broker=MagicMock())
    pipeline.broker.is_trading_day.return_value = False

    result = pipeline.run_midday()

    assert result["status"] == "market_holiday"
    pipeline.broker.get_account.assert_not_called()


def test_pipeline_midday_preserves_protective_orders():
    pipeline = build_pipeline(
        news_provider=MagicMock(),
        news_analyst=MagicMock(),
        earnings_provider=MagicMock(),
        earnings_analyst=MagicMock(),
        broker=MagicMock(),
        macro=MagicMock(),
        db=MagicMock(),
        risk_engine=MagicMock(),
    )
    pipeline.broker.is_trading_day.return_value = True
    pipeline.broker.get_account.return_value = {"cash": 1000.0, "portfolio_value": 5000.0}
    pipeline.broker.get_positions.return_value = []
    pipeline.macro.get_macro_summary.return_value = {}
    # Circuit-breaker probe runs on every position_review tick. No breach in
    # this scenario — return None so execution flows into the normal path.

    result = pipeline.run_midday()

    assert result["status"] == "reviewed"
    pipeline.broker.cancel_open_orders.assert_not_called()
    pipeline.broker.cancel_open_entry_orders.assert_not_called()


@pytest.mark.parametrize("session_type", ["midday", "close"])
def test_prelatched_position_review_preserves_deterministic_safety(session_type):
    pipeline = build_pipeline(
        broker=MagicMock(), db=MagicMock(), risk_engine=MagicMock(), position_reviewer=MagicMock()
    )
    pipeline.broker.is_trading_day.return_value = True
    pipeline.broker.get_session_close.return_value = None
    pipeline.broker.get_account.return_value = {
        "cash": 1000.0,
        "portfolio_value": 10_000.0,
        "last_equity": 10_000.0,
    }
    pipeline.broker.get_positions.return_value = []
    pipeline._drain_pending_protection_restores = MagicMock()
    pipeline._reconcile_orphan_pending_submits = MagicMock()
    pipeline._reconcile_stop_coverage = MagicMock(return_value=[])
    forced = {"id": "forced", "status": "accepted"}
    exdiv = {"id": "exdiv", "status": "accepted"}
    pipeline._force_delever = MagicMock(return_value=[forced])
    pipeline._handle_ex_dividends = MagicMock(return_value=[exdiv])
    pipeline._reconcile_fills = MagicMock()
    pipeline._run_news_update = MagicMock()
    pipeline._load_earnings_analyses = MagicMock()
    pipeline.cost_circuit = MagicMock()
    pipeline.cost_circuit.activate_session.return_value = {"suspended": True}
    pipeline.cost_circuit.require_paid_analysis.side_effect = PaidAnalysisSuspended(
        "prelatched",
        {"suspended": True},
    )

    result = pipeline.run_position_review(session_type)

    assert result["status"] == "paid_analysis_suspended"
    assert result["orders"] == [forced, exdiv]
    pipeline._drain_pending_protection_restores.assert_called_once()
    pipeline._reconcile_orphan_pending_submits.assert_called_once()
    pipeline._reconcile_stop_coverage.assert_called_once()
    pipeline._force_delever.assert_called_once()
    pipeline._handle_ex_dividends.assert_called_once()
    pipeline._run_news_update.assert_not_called()
    pipeline._load_earnings_analyses.assert_not_called()
    pipeline.position_reviewer.review.assert_not_called()


def test_paid_suspension_marks_morning_for_the_evening_dead_man_probe():
    """Item 191: `paid_analysis_suspended` is a third legit PM-less
    completion alongside `no_data` and `evidence_gate_skip` (see
    `test_evidence_gate.py`'s `write_status.assert_called_once_with(
    "morning", "evidence_gate_skip")`), so it must tell the evening
    dead-man probe the same way those two already do. Before this it
    didn't: a same-day cost-circuit trip the owner was already alerted to
    (the morning session's own "SUSPENDED" push) came back ~16h later
    relabelled "research ran, PM never did — killed mid-run?"."""
    from src import decision_checkpoint as dc

    pipeline = build_pipeline()
    with patch.object(dc, "write_status") as write_status:
        result = pipeline._paid_suspension_after_late_safety(
            "run-dd502c6f",
            session="morning",
            error=PaidAnalysisSuspended("circuit open", {"suspended": True}),
            where="post-research-circuit-open",
        )
    assert result["status"] == "paid_analysis_suspended"
    write_status.assert_called_once_with("morning", "paid_analysis_suspended")


def test_paid_suspension_does_not_mark_non_morning_sessions():
    """The evening probe's sharpened checks only ever key on 'morning' —
    marking midday/close would just be a dead write, so the guard mirrors
    `_evidence_gate_skip`'s own `if session == "morning":` exactly."""
    from src import decision_checkpoint as dc

    pipeline = build_pipeline()
    with patch.object(dc, "write_status") as write_status:
        pipeline._paid_suspension_after_late_safety(
            "midday-abc123",
            session="midday",
            error=PaidAnalysisSuspended("circuit open", {"suspended": True}),
            where="post-research-circuit-open",
        )
    write_status.assert_not_called()


def test_total_pnl_since_reset_starts_at_the_clean_record_not_the_oldest_row(tmp_path):
    """The Telegram 'total P&L' baseline is the equity going into the first
    daily_pnl row on/after the clean-record date (total_value - daily_pnl),
    verified against the previous row's close. Older rows are ignored."""
    from src.storage.db import Database

    db = Database(str(tmp_path / "t.db"))
    db.initialize()
    db.insert_daily_pnl(date="2026-09-02", total_value=9862.74, daily_pnl=44.70, daily_return_pct=0.46)
    db.insert_daily_pnl(
        date="2026-10-09", total_value=10029.62, daily_pnl=10.0, daily_return_pct=0.1, equity_close=10029.62
    )
    db.insert_daily_pnl(date="2026-10-12", total_value=9928.22, daily_pnl=-101.40, daily_return_pct=-1.0)

    pipeline = build_pipeline(db=db)

    total_pnl, total_return_pct, since = pipeline._total_pnl_since_reset(10100.00)

    baseline = 10029.62
    assert since == "Fri 9 Oct close"
    assert total_pnl == pytest.approx(10100.00 - baseline)
    assert total_return_pct == pytest.approx((10100.00 - baseline) / baseline * 100)
    db.close()


def test_total_pnl_since_reset_no_baseline_is_none_not_zero(tmp_path):
    """No `daily_pnl` row recorded yet (fresh DB) — the baseline is
    genuinely unknown, so this must say so, never fabricate a 0."""
    from src.storage.db import Database

    db = Database(str(tmp_path / "t.db"))
    db.initialize()

    pipeline = build_pipeline(db=db)

    assert pipeline._total_pnl_since_reset(9900.00) == (None, None, None)
    db.close()


def test_reprotect_residual_is_idempotent_against_existing_broker_stop():
    """Drain replay can re-fire reprotect for a row whose previous attempt
    already submitted the residual stop but failed to delete the WAL row
    (DB error / process kill between broker submit and row delete). The
    second pass must detect the live stop at the broker and skip — or it
    would double-stack stops on the same residual, doubling the exit on
    trigger. Audit 2026-05-27 added the idempotency check; this test pins
    the contract."""
    pipeline = build_pipeline(broker=MagicMock(), _format_qty=lambda q: str(q))

    # Broker already has a SELL stop at $90 on this symbol (residual of a
    # prior reprotect that survived the kill). It is a DIFFERENT order from
    # the one this run cancelled ("s1") and it is in a live state — both
    # matter since 2026-09-30: a stop is only proof of protection when it
    # can be shown not to be the one this same run just cancelled, and not
    # to be dying. Without an id and a status this order is unidentifiable,
    # and unidentifiable now means submit.
    existing = MagicMock()
    existing.stop_price = "90.00"
    existing.id = "prior-attempt-order"
    existing.status = "accepted"
    pipeline.broker._list_open_sell_stop_orders.return_value = [existing]

    cancelled = [{"id": "s1", "qty": 10, "stop_price": 90.0, "limit_price": 88.0}]
    ok = pipeline._reprotect_residual_after_partial_sell("NVDA", 10.0, cancelled)

    assert ok is True
    pipeline.broker._submit_stop_limit_order.assert_not_called()


def test_reprotect_residual_submits_when_existing_stop_has_different_price():
    """Idempotency must NOT swallow a legitimate re-protect at a DIFFERENT
    price (e.g. trailing stop was raised, original was lower). Only an
    existing stop at the same best_stop should suppress the submit."""
    pipeline = build_pipeline(broker=MagicMock(), _format_qty=lambda q: str(q))

    existing = MagicMock()
    existing.stop_price = "85.00"  # different from best_stop below
    pipeline.broker._list_open_sell_stop_orders.return_value = [existing]

    cancelled = [{"id": "s1", "qty": 10, "stop_price": 90.0, "limit_price": 88.0}]
    pipeline._reprotect_residual_after_partial_sell("NVDA", 10.0, cancelled)
    pipeline.broker._submit_protective_stop_retrying.assert_called_once_with(
        symbol="NVDA",
        qty=10.0,
        stop_price=90.0,
        side="sell",
    )


def test_reprotect_residual_picks_highest_stop_price_among_specs():
    """When multiple stops covered the original position, the re-placed stop
    on the residual qty must use the HIGHEST stop_price from the cancelled
    set — that's the most-protective price the position had pre-SELL.
    Picking the lowest would silently weaken protection on the way back."""
    pipeline = build_pipeline(broker=MagicMock(), _format_qty=lambda q: str(q))

    cancelled = [
        {"id": "stop-low", "qty": 51, "stop_price": 240.0, "limit_price": 235.0},
        {"id": "stop-high", "qty": 51, "stop_price": 248.5, "limit_price": 240.0},
        {"id": "stop-mid", "qty": 51, "stop_price": 244.0, "limit_price": 238.0},
    ]
    pipeline._reprotect_residual_after_partial_sell("AMZN", 41.0, cancelled)

    pipeline.broker._submit_protective_stop_retrying.assert_called_once_with(
        symbol="AMZN",
        qty=41.0,
        stop_price=248.5,
        side="sell",
    )


def test_reprotect_residual_skips_when_no_specs():
    """No cancelled stops → nothing to re-protect with. Helper must be a
    no-op rather than submitting a stop with no anchor price."""
    pipeline = build_pipeline(broker=MagicMock(), _format_qty=lambda q: str(q))

    pipeline._reprotect_residual_after_partial_sell("AMZN", 41.0, [])

    pipeline.broker._submit_stop_limit_order.assert_not_called()


def test_reprotect_residual_skips_when_residual_zero():
    """Full-exit path passes residual=0 — helper must skip rather than
    submitting a 0-qty stop."""
    pipeline = build_pipeline(broker=MagicMock(), _format_qty=lambda q: str(q))

    cancelled = [{"id": "stop-1", "qty": 51, "stop_price": 248.5}]
    pipeline._reprotect_residual_after_partial_sell("AMZN", 0.0, cancelled)

    pipeline.broker._submit_stop_limit_order.assert_not_called()


def test_reprotect_residual_swallows_submit_failure_with_loud_warning(caplog):
    """If the re-protect submit raises, we log loudly but don't propagate —
    the SELL itself already succeeded; failing the re-protect shouldn't
    undo that. The position is unprotected until the next session, and
    the warning needs to be loud enough that operators notice."""
    pipeline = build_pipeline(broker=MagicMock())
    pipeline.broker._submit_protective_stop_retrying.side_effect = RuntimeError("api error")
    pipeline._format_qty = lambda q: str(q)

    cancelled = [{"id": "stop-1", "qty": 51, "stop_price": 248.5}]
    # Must not raise.
    pipeline._reprotect_residual_after_partial_sell("AMZN", 41.0, cancelled)

    assert any("Re-protect failed for AMZN" in rec.message and rec.levelname == "WARNING" for rec in caplog.records)


def test_partial_trim_restores_stops_when_sell_rejected(tmp_path):
    """A trim shrinks its stop in place (100 -> 85) and the broker then
    REJECTS the sell: nothing was sold, so the shrunk stop is grown back to
    the full 100 by the quantity invariant. The stop is never cancelled, so
    the kept shares are covered at every broker call; a restore from a
    cancelled set is never needed."""
    from src.storage.db import Database

    db = Database(str(tmp_path / "t.db"))
    db.initialize()
    db.insert_trade("NVDA", "BUY", 100, 100.0, "opened", "r1", stop_loss=90.0)

    pipeline = build_pipeline(db=db, broker=MagicMock())
    cancelled = [
        {"id": "stop-old", "qty": 100, "stop_price": 95.0, "limit_price": 92.0},
    ]
    _mock_amend_seam(
        pipeline.broker,
        specs=cancelled,
        kept_qty=85,
        held_after=100,
        sell_status="rejected",
        fill_info={"status": "rejected", "filled_qty": "0", "filled_avg_price": None},
        sell_id="tp-rejected",
    )

    winner = Position(
        symbol="NVDA",
        qty=100,
        avg_entry=100,
        current_price=135,
        market_value=13500,
        unrealized_pnl=3500,
        sector="Technology",
    )

    orders = _partial_trim(pipeline, winner, qty=15.0, run_id="r2")

    assert orders == [], "rejected SELL should not be in orders list"
    _assert_trim_never_naked(pipeline.broker, [85, 100])
    pipeline.broker._submit_stop_limit_order.assert_not_called()
    db.close()


def test_full_sell_skips_residual_reprotect(tmp_path):
    """When the SELL is for the entire position, residual qty == 0 and
    re-protect must be a no-op. The whole position is being exited;
    placing a stop on 0 shares would error. Pin via the morning
    ExecutionStage path where action_label='SELL' (not PARTIAL_SELL)
    triggers full-qty exit."""
    from src.models import PortfolioDecision, TradeDecision
    from src.pipeline_context import RunContext
    from src.pipeline_stages import ExecutionStage

    pipeline = MagicMock()
    pipeline.broker.list_open_orders_checked.return_value = (True, [])  # listing read, nothing open
    pipeline.broker.get_latest_price.return_value = 100.0
    pipeline.broker.get_latest_quote.return_value = {"bid_price": 100.0, "ask_price": 100.0}
    pipeline.broker.submit_order.return_value = {
        "id": "sell-full",
        "status": "accepted",
        "symbol": "JPM",
    }
    pipeline.broker.wait_for_order_terminal.return_value = "filled"
    _mock_stop_seam(pipeline.broker, specs=[{"id": "stop-1", "qty": 10, "stop_price": 280.0, "limit_price": 275.0}])
    _mock_stage_seam(pipeline, specs=[{"id": "stop-1", "qty": 10, "stop_price": 280.0, "limit_price": 275.0}])
    # Board item 178: ExecutionStage now re-reads the account before the
    # SELL loop too, so this fixture must reflect a fresh snapshot that
    # STILL holds JPM — otherwise the SELL never fires and the "no residual
    # to protect" assertion below would pass for the wrong reason (no SELL
    # at all, rather than a full SELL leaving no residual).
    pipeline._refresh_account_state.return_value = (
        {"cash": 60_000.0, "portfolio_value": 100_500.0},
        [
            Position(
                symbol="JPM",
                qty=10.0,
                avg_entry=300.0,
                current_price=320.0,
                market_value=3_200.0,
                unrealized_pnl=200.0,
                sector="Financial",
            ),
        ],
        {},
    )
    pipeline._order_accepted.return_value = True
    pipeline._format_qty = lambda q: str(q)
    pipeline._full_sell_qty = lambda q: q
    pipeline.db = MagicMock()

    ctx = RunContext.start("morning")
    ctx.cash = 30_000.0
    ctx.total_value = 100_000.0
    ctx.last_equity = 100_000.0
    ctx.positions = [
        Position(
            symbol="JPM",
            qty=10.0,
            avg_entry=300.0,
            current_price=320.0,
            market_value=3_200.0,
            unrealized_pnl=200.0,
            sector="Financial",
        ),
    ]
    ctx.portfolio_decision = PortfolioDecision(
        reasoning_chain=_pm_rc(),
        decisions=[
            TradeDecision(
                action="SELL",
                symbol="JPM",
                allocation_pct=100,
                entry_price=300.0,
                stop_loss=280.0,
                take_profit=350.0,
                reasoning="full exit",
            ),
        ],
        portfolio_view="test",
    )
    ctx.symbols_bars = {}

    hold(pipeline.broker, {"JPM": 10.0})  # still held when the SELL is re-sized
    ExecutionStage(pipeline=pipeline).run(ctx)

    # Full SELL fired
    assert any(
        c.kwargs.get("side") == "sell" and c.kwargs.get("symbol") == "JPM"
        for c in pipeline.broker.submit_order.call_args_list
    )
    # No residual to protect — must not call _reprotect helper
    pipeline._reprotect_residual_after_partial_sell.assert_not_called()


def test_partial_trim_reprotects_residual_after_partial_trim_fills(tmp_path):
    """End-to-end happy path: a REDUCE trims 15 of 100 NVDA, the limit
    fills cleanly, and the remaining 85 shares get a fresh stop at the
    most-protective pre-existing price (95.0). PR J defers this to AFTER
    wait_for_order_terminal — so the broker's terminal fill_qty must
    show 15 (full fill of the trim qty) for the reprotect to fire on
    the expected residual."""
    from src.storage.db import Database

    db = Database(str(tmp_path / "t.db"))
    db.initialize()
    db.insert_trade("NVDA", "BUY", 100, 100.0, "opened", "r1", stop_loss=90.0)

    pipeline = build_pipeline(db=db, broker=MagicMock())
    pipeline.broker.submit_order.return_value = {
        "id": "tp-1",
        "status": "accepted",
        "symbol": "NVDA",
    }
    cancelled = [
        {"id": "stop-old-low", "qty": 100, "stop_price": 90.0, "limit_price": 87.0},
        {"id": "stop-old-high", "qty": 100, "stop_price": 95.0, "limit_price": 92.0},
    ]
    _mock_stop_seam(pipeline.broker, specs=cancelled)
    # Limit fills at exactly the trim qty.
    pipeline.broker.wait_for_order_terminal.return_value = "filled"
    confirm_cancels(pipeline.broker)  # the broker confirms the stop cancel before the trim
    pipeline.broker.get_order_fill_info.return_value = {
        "status": "filled",
        "filled_qty": "15",
        "filled_avg_price": "134.5",
    }

    winner = Position(
        symbol="NVDA",
        qty=100,
        avg_entry=100,
        current_price=135,
        market_value=13500,
        unrealized_pnl=3500,
        sector="Technology",
    )

    hold(pipeline.broker, {"NVDA": 100.0})
    orders = _partial_trim(pipeline, winner, qty=15.0, run_id="r2")

    assert len(orders) == 1
    pipeline.broker._submit_protective_stop_retrying.assert_called_once_with(
        symbol="NVDA",
        qty=85.0,
        stop_price=95.0,
        side="sell",
    )
    db.close()


def test_partial_trim_restores_originals_when_limit_does_not_fill(tmp_path):
    """The bug PR J was filed for: an accepted partial limit can later
    cancel/expire without filling. The stop was shrunk to 85 for the sell;
    post-wait, fill_qty=0 and the position is still 100, so the finalizer
    grows the SAME stop back to 100 — not a residual-shaped 85, and never a
    cancel-and-restore that leaves the kept shares naked in between."""
    from src.storage.db import Database

    db = Database(str(tmp_path / "t.db"))
    db.initialize()
    db.insert_trade("NVDA", "BUY", 100, 100.0, "opened", "r1", stop_loss=90.0)

    pipeline = build_pipeline(db=db, broker=MagicMock())
    cancelled = [
        {"id": "stop-old", "qty": 100, "stop_price": 95.0, "limit_price": 92.0},
    ]
    _mock_amend_seam(
        pipeline.broker,
        specs=cancelled,
        kept_qty=85,
        held_after=100,
        sell_status="expired",
        fill_info={"status": "expired", "filled_qty": "0", "filled_avg_price": None},
        sell_id="tp-pending",
    )

    winner = Position(
        symbol="NVDA",
        qty=100,
        avg_entry=100,
        current_price=135,
        market_value=13500,
        unrealized_pnl=3500,
        sector="Technology",
    )

    _partial_trim(pipeline, winner, qty=15.0, run_id="r2")

    _assert_trim_never_naked(pipeline.broker, [85, 100])
    pipeline.broker._submit_stop_limit_order.assert_not_called()
    db.close()


def test_finalize_protection_cancels_lingering_sell_when_status_non_terminal():
    """If wait_for_order_terminal hit its 15s ceiling without the order
    going terminal, get_order_fill_info still reports a live status like
    'new' / 'accepted' / 'pending_new'. Finalizing on that state would
    race the broker — restoring stops while the SELL is still open
    fails on held_for_orders.

    The fix forces terminal by cancelling the lingering SELL, re-reads
    fill_info post-cancel, then proceeds with normal branch logic.
    Pin the cancel call sequence + the eventual restore."""
    pipeline = build_pipeline(
        broker=MagicMock(), _format_qty=lambda q: str(q), _reprotect_residual_after_partial_sell=MagicMock()
    )

    # First read: SELL is still live ("new"). After cancel, broker
    # reports terminal "canceled" with 0 fill.
    pipeline.broker.get_order_fill_info.side_effect = [
        {"status": "new", "filled_qty": "0", "filled_avg_price": None},
        {"status": "canceled", "filled_qty": "0", "filled_avg_price": None},
    ]

    cancelled = [
        {"id": "stop-old", "qty": 100, "stop_price": 95.0, "limit_price": 92.0},
    ]

    pipeline._finalize_protection_after_sell(
        order_id="alpaca-lingering",
        symbol="NVDA",
        position_qty_before_sell=100.0,
        cancelled_specs=cancelled,
    )

    # Lingering SELL must be cancelled before finalize proceeds.
    pipeline.broker.cancel_entry_order.assert_called_once_with(
        "alpaca-lingering",
    )
    pipeline.broker.wait_for_order_terminal.assert_called_once()
    # Post-cancel fill_qty=0 → restore originals (NOT reprotect residual).
    # Non-drain finalize → check_idempotency=False.
    pipeline.broker._restore_stop_orders.assert_called_once_with(
        "NVDA",
        cancelled,
        check_idempotency=False,
    )
    pipeline._reprotect_residual_after_partial_sell.assert_not_called()


def test_finalize_protection_uses_partial_fill_after_lingering_cancel():
    """Edge case: SELL was non-terminal at wait timeout, but the cancel
    propagation captured a partial fill. The post-cancel filled_qty
    must drive the residual computation — NOT a no-fill restore."""
    pipeline = build_pipeline(
        broker=MagicMock(), _format_qty=lambda q: str(q), _reprotect_residual_after_partial_sell=MagicMock()
    )

    pipeline.broker.get_order_fill_info.side_effect = [
        {"status": "new", "filled_qty": "0", "filled_avg_price": None},
        # Cancel raced with a fill of 18 shares before fully cancelling.
        {"status": "canceled", "filled_qty": "18", "filled_avg_price": "117.5"},
    ]

    cancelled = [
        {"id": "stop-old", "qty": 100, "stop_price": 95.0, "limit_price": 92.0},
    ]

    pipeline._finalize_protection_after_sell(
        order_id="alpaca-partial-cancel",
        symbol="NVDA",
        position_qty_before_sell=100.0,
        cancelled_specs=cancelled,
    )

    pipeline.broker.cancel_entry_order.assert_called_once()
    # Residual = 100 - 18 = 82 (driven by post-cancel fill_qty)
    pipeline._reprotect_residual_after_partial_sell.assert_called_once_with(
        "NVDA",
        82.0,
        cancelled,
    )
    pipeline.broker._restore_stop_orders.assert_not_called()


def test_finalize_protection_bails_when_post_cancel_status_still_non_terminal():
    """Cancel API succeeds but propagation takes longer than the 5s
    short-wait — broker still reports `pending_cancel` (or even `new`).
    Restoring stops at this point recreates the held_for_orders conflict
    PR K was supposed to fix. Pin: bail if post-cancel status is not in
    the terminal set."""
    pipeline = build_pipeline(
        broker=MagicMock(), _format_qty=lambda q: str(q), _reprotect_residual_after_partial_sell=MagicMock()
    )

    # First read: live. After cancel + 5s wait: STILL non-terminal
    # (pending_cancel). Cancel itself didn't raise — it succeeded —
    # but propagation hasn't completed.
    pipeline.broker.get_order_fill_info.side_effect = [
        {"status": "new", "filled_qty": "0", "filled_avg_price": None},
        {"status": "pending_cancel", "filled_qty": "0", "filled_avg_price": None},
    ]

    cancelled = [{"id": "stop-old", "qty": 100, "stop_price": 95.0}]

    pipeline._finalize_protection_after_sell(
        order_id="alpaca-slow-cancel",
        symbol="NVDA",
        position_qty_before_sell=100.0,
        cancelled_specs=cancelled,
    )

    pipeline.broker.cancel_entry_order.assert_called_once()
    # Critical: restore must NOT fire — broker may still consider the
    # SELL live. Compounding with a stop submit would re-trigger
    # held_for_orders.
    pipeline.broker._restore_stop_orders.assert_not_called()
    pipeline._reprotect_residual_after_partial_sell.assert_not_called()


def test_finalize_persists_orphan_when_lingering_cancel_fails(tmp_path):
    """Codex r7 #3: when cancel raises, the bail branch must persist the
    restore intent to pending_protection_restores so a later drain can
    pick it up. Earlier versions just logged "next session reconcile
    rebuilds coverage" — but reconcile only updates fill columns, never
    actually rebuilds stop coverage. Pin: a row lands in DB with the
    right symbol + sell_order_id + specs."""
    from src.storage.db import Database

    db = Database(str(tmp_path / "t.db"))
    db.initialize()

    pipeline = build_pipeline(
        db=db, broker=MagicMock(), _format_qty=lambda q: str(q), _reprotect_residual_after_partial_sell=MagicMock()
    )

    pipeline.broker.get_order_fill_info.return_value = {
        "status": "new",
        "filled_qty": "0",
        "filled_avg_price": None,
    }
    pipeline.broker.cancel_entry_order.side_effect = RuntimeError("api timeout")

    cancelled = [
        {"id": "stop-old", "qty": 100, "stop_price": 95.0, "limit_price": 92.0},
    ]

    pipeline._finalize_protection_after_sell(
        order_id="alpaca-stuck",
        symbol="NVDA",
        position_qty_before_sell=100.0,
        cancelled_specs=cancelled,
    )

    rows = db.get_pending_protection_restores()
    assert len(rows) == 1
    row = rows[0]
    assert row["symbol"] == "NVDA"
    assert row["sell_order_id"] == "alpaca-stuck"
    assert row["position_qty_before_sell"] == 100.0
    import json as _json

    persisted_specs = _json.loads(row["specs_json"])
    assert persisted_specs[0]["stop_price"] == 95.0
    db.close()


def test_finalize_persists_orphan_when_post_cancel_status_non_terminal(tmp_path):
    """Same persistence path for the slow-cancel branch: cancel API
    succeeded but propagation didn't converge in 5s. Drain queue must
    capture the intent so we don't silently lose protection."""
    from src.storage.db import Database

    db = Database(str(tmp_path / "t.db"))
    db.initialize()

    pipeline = build_pipeline(
        db=db, broker=MagicMock(), _format_qty=lambda q: str(q), _reprotect_residual_after_partial_sell=MagicMock()
    )

    pipeline.broker.get_order_fill_info.side_effect = [
        {"status": "new", "filled_qty": "0", "filled_avg_price": None},
        {"status": "pending_cancel", "filled_qty": "0", "filled_avg_price": None},
    ]
    cancelled = [{"id": "stop-old", "qty": 100, "stop_price": 95.0}]

    pipeline._finalize_protection_after_sell(
        order_id="alpaca-slow-cancel",
        symbol="AAPL",
        position_qty_before_sell=50.0,
        cancelled_specs=cancelled,
    )

    rows = db.get_pending_protection_restores()
    assert len(rows) == 1
    assert rows[0]["sell_order_id"] == "alpaca-slow-cancel"
    db.close()


def test_drain_pending_protection_restores_replays_finalize_when_terminal(tmp_path):
    """Drain pass: row exists from a prior session's bail. SELL is now
    terminal at the broker. Drain must run finalize from persisted
    specs (which will restore the original stops since fill_qty=0)
    AND delete the row from the queue."""
    from src.storage.db import Database
    import json as _json

    db = Database(str(tmp_path / "t.db"))
    db.initialize()

    cancelled = [{"id": "stop-old", "qty": 100, "stop_price": 95.0, "limit_price": 92.0}]
    db.insert_pending_protection_restore(
        symbol="NVDA",
        sell_order_id="alpaca-resolved",
        position_qty_before_sell=100.0,
        specs_json=_json.dumps(cancelled),
    )

    pipeline = build_pipeline(
        db=db, broker=MagicMock(), _format_qty=lambda q: str(q), _reprotect_residual_after_partial_sell=MagicMock()
    )

    # Order is now terminal (canceled with no fill) — drain replays
    # finalize, which hits the no-fill branch → restore originals.
    pipeline.broker.get_order_fill_info.return_value = {
        "status": "canceled",
        "filled_qty": "0",
        "filled_avg_price": None,
    }
    # PR S: restore now returns (count, failed_specs). Full success → empty failed.
    pipeline.broker._restore_stop_orders.return_value = (1, [])

    drained = pipeline._drain_pending_protection_restores()

    assert len(drained) == 1
    pipeline.broker._restore_stop_orders.assert_called_once()
    args = pipeline.broker._restore_stop_orders.call_args
    assert args[0][0] == "NVDA"
    assert args[0][1] == cancelled
    # Row should be cleared.
    assert db.get_pending_protection_restores() == []
    db.close()


def test_intra_check_drains_orphan_restores_at_entry(tmp_path):
    """Codex r8 #2: drain must run on every session entry, not just
    morning. Pin: intra_check (every 30 min during 09:30-16:00 ET)
    runs the drain so a bail from morning can recover intra-day."""
    from src.storage.db import Database
    import json as _json

    db = Database(str(tmp_path / "t.db"))
    db.initialize()
    cancelled = [{"id": "stop-old", "qty": 100, "stop_price": 95.0}]
    db.insert_pending_protection_restore(
        symbol="NVDA",
        sell_order_id="alpaca-orphan",
        position_qty_before_sell=100.0,
        specs_json=_json.dumps(cancelled),
    )

    pipeline = build_pipeline(db=db, broker=MagicMock(), risk_engine=MagicMock())
    # Item 127: the broker-writing preamble runs only under the desk's
    # advisory flock, which lives beside the database named in config.
    from types import SimpleNamespace

    pipeline.config = SimpleNamespace(storage=SimpleNamespace(db_path=db.db_path))
    pipeline.broker.is_trading_day.return_value = True
    pipeline.broker.get_account.return_value = {
        "portfolio_value": 100_500.0,
        "last_equity": 100_000.0,
        "cash": 5000.0,
    }
    # Position still held — finalize re-queries to detect concurrent-path
    # liquidation; with NVDA still at 100 shares the restore branch fires.
    from src.models import Position

    pipeline.broker.get_positions.return_value = [
        Position(
            symbol="NVDA",
            qty=100.0,
            avg_entry=100.0,
            current_price=100.0,
            market_value=10000.0,
            unrealized_pnl=0.0,
            unrealized_intraday_pnl=0.0,
            sector="Tech",
        ),
    ]
    pipeline.broker.get_order_fill_info.return_value = {
        "status": "canceled",
        "filled_qty": "0",
        "filled_avg_price": None,
    }
    pipeline.broker._restore_stop_orders.return_value = (1, [])  # full success

    pipeline.run_intra_check()

    # Drain ran during entry → row consumed (broker said terminal).
    assert db.get_pending_protection_restores() == []
    # Drain replay → check_idempotency=True so the audit's drain-
    # narrowing race can't re-submit already-alive stops.
    pipeline.broker._restore_stop_orders.assert_called_once_with(
        "NVDA",
        cancelled,
        check_idempotency=True,
    )
    db.close()


def test_drain_narrows_row_to_failed_specs_after_partial_restore(tmp_path):
    """Codex r10: drain partial-restore must update the existing row's
    specs to ONLY the failed ones. Otherwise the next drain re-submits
    the already-alive stop spec → broker rejects on duplicate /
    held_for_orders, and the row can stay stuck forever.

    Pin: row enters drain with [spec_a, spec_b], restore lands spec_a
    only, drain narrows the row to [spec_b]. Next drain pass would
    only retry spec_b."""
    from src.storage.db import Database
    import json as _json

    db = Database(str(tmp_path / "t.db"))
    db.initialize()

    spec_a = {"id": "stop-a", "qty": 50, "stop_price": 95.0, "limit_price": 92.0}
    spec_b = {"id": "stop-b", "qty": 50, "stop_price": 96.0, "limit_price": 93.0}
    db.insert_pending_protection_restore(
        symbol="NVDA",
        sell_order_id="alpaca-orphan",
        position_qty_before_sell=100.0,
        specs_json=_json.dumps([spec_a, spec_b]),
    )

    pipeline = build_pipeline(
        db=db, broker=MagicMock(), _format_qty=lambda q: str(q), _reprotect_residual_after_partial_sell=MagicMock()
    )

    pipeline.broker.get_order_fill_info.return_value = {
        "status": "canceled",
        "filled_qty": "0",
        "filled_avg_price": None,
    }
    # 1 of 2 restored: spec_a landed, spec_b failed.
    pipeline.broker._restore_stop_orders.return_value = (1, [spec_b])

    drained = pipeline._drain_pending_protection_restores()

    # Drain returned 0 (coverage not fully rebuilt) but the row still exists,
    # narrowed to just spec_b.
    assert len(drained) == 0
    rows = db.get_pending_protection_restores()
    assert len(rows) == 1
    persisted_now = _json.loads(rows[0]["specs_json"])
    assert len(persisted_now) == 1
    assert persisted_now[0]["id"] == "stop-b", f"row should be narrowed to just the failed spec; got {persisted_now}"
    db.close()


def test_drain_does_not_narrow_row_when_no_progress(tmp_path):
    """If restore made no progress (0 of 2 succeeded, both in failed_specs),
    don't bother updating — row stays as-is for next pass. Avoid
    a no-op DB write."""
    from src.storage.db import Database
    import json as _json

    db = Database(str(tmp_path / "t.db"))
    db.initialize()

    cancelled = [
        {"id": "stop-a", "qty": 50, "stop_price": 95.0},
        {"id": "stop-b", "qty": 50, "stop_price": 96.0},
    ]
    db.insert_pending_protection_restore(
        symbol="NVDA",
        sell_order_id="alpaca-orphan",
        position_qty_before_sell=100.0,
        specs_json=_json.dumps(cancelled),
    )

    pipeline = build_pipeline(db=db, broker=MagicMock(), _format_qty=lambda q: str(q))
    pipeline.broker.get_order_fill_info.return_value = {
        "status": "canceled",
        "filled_qty": "0",
        "filled_avg_price": None,
    }
    # Total failure: 0 of 2 restored.
    pipeline.broker._restore_stop_orders.return_value = (0, cancelled)

    pipeline._drain_pending_protection_restores()

    # Row unchanged — full original specs still there.
    rows = db.get_pending_protection_restores()
    persisted_now = _json.loads(rows[0]["specs_json"])
    assert len(persisted_now) == 2
    db.close()


def test_finalize_skips_restore_when_concurrent_path_fully_exited(tmp_path):
    """intra_check is exempt from cross-mode session lock, so an
    EMERGENCY_SELL can fully liquidate the symbol while morning's SELL
    sits unfilled. After this SELL terminates with no fill, broker now
    reports 0 shares — restoring stops on a phantom position would have
    broker reject on insufficient qty → finalize bail → drain replay
    same bad math → row stuck forever. Pin: when broker shows position=0
    and our SELL had no fill, skip restore entirely and report success."""
    from src.storage.db import Database
    from unittest.mock import MagicMock

    db = Database(str(tmp_path / "t.db"))
    db.initialize()
    pipeline = build_pipeline(db=db, broker=MagicMock(), _format_qty=lambda q: str(q))

    cancelled = [{"id": "stop-old", "qty": 100, "stop_price": 95.0}]
    pipeline.broker.get_order_fill_info.return_value = {
        "status": "canceled",
        "filled_qty": "0",
        "filled_avg_price": None,
    }
    # Broker now reports position=0 (intra_check liquidated NVDA).
    pipeline.broker.get_positions.return_value = []

    ok, retry_specs = pipeline._finalize_protection_after_sell(
        order_id="alpaca-resolved",
        symbol="NVDA",
        position_qty_before_sell=100.0,
        cancelled_specs=cancelled,
    )

    assert ok is True
    assert retry_specs == []
    # Restore must NOT have been called — there's nothing to protect.
    pipeline.broker._restore_stop_orders.assert_not_called()
    # No drain row written.
    assert db.get_pending_protection_restores() == []
    db.close()


def test_finalize_clips_residual_when_concurrent_path_partially_exited(tmp_path):
    """Partial-fill branch: morning's SELL filled 30 of 100 shares
    (residual math says 70 left). But intra_check concurrently sold
    50 more — broker actually shows 20 shares. Reprotecting 70 would
    over-state by 50 → broker rejects. Pin: residual clipped to actual
    broker position (20), reprotect called with the clipped qty."""
    from src.storage.db import Database
    from src.models import Position
    from unittest.mock import MagicMock

    db = Database(str(tmp_path / "t.db"))
    db.initialize()
    pipeline = build_pipeline(db=db, broker=MagicMock(), _format_qty=lambda q: str(q))

    cancelled = [{"id": "stop-old", "qty": 100, "stop_price": 95.0}]
    pipeline.broker.get_order_fill_info.return_value = {
        "status": "filled",
        "filled_qty": "30",
        "filled_avg_price": "100.0",
    }
    # Broker reports 20 shares (intra_check took 50 more).
    pipeline.broker.get_positions.return_value = [
        Position(
            symbol="NVDA",
            qty=20.0,
            avg_entry=100.0,
            current_price=100.0,
            market_value=2000.0,
            unrealized_pnl=0.0,
            unrealized_intraday_pnl=0.0,
            sector="Tech",
        ),
    ]

    ok, _ = pipeline._finalize_protection_after_sell(
        order_id="alpaca-partial",
        symbol="NVDA",
        position_qty_before_sell=100.0,
        cancelled_specs=cancelled,
    )

    assert ok is True
    # Reprotect was called with clipped qty (20), not naive residual (70).
    pipeline.broker._submit_protective_stop_retrying.assert_called_once()
    kwargs = pipeline.broker._submit_protective_stop_retrying.call_args.kwargs
    assert kwargs["qty"] == 20.0, f"expected clipped qty=20, got {kwargs['qty']}"
    db.close()


def test_finalize_collapses_to_reprotect_when_concurrent_reduced_position_no_fill(tmp_path):
    """fill_qty=0 branch with concurrent partial reduction: our SELL
    didn't fill, but intra_check sold half the position. Original specs
    cover 100 shares; broker now has 40. Restoring all specs would
    over-state. Pin: collapse to single reprotect at most-protective
    stop_price for actual position (40 shares)."""
    from src.storage.db import Database
    from src.models import Position
    from unittest.mock import MagicMock

    db = Database(str(tmp_path / "t.db"))
    db.initialize()
    pipeline = build_pipeline(db=db, broker=MagicMock(), _format_qty=lambda q: str(q))

    cancelled = [
        {"id": "stop-a", "qty": 60, "stop_price": 94.0},
        {"id": "stop-b", "qty": 40, "stop_price": 96.0},
    ]
    pipeline.broker.get_order_fill_info.return_value = {
        "status": "canceled",
        "filled_qty": "0",
        "filled_avg_price": None,
    }
    # Broker reports 40 shares (intra_check took 60).
    pipeline.broker.get_positions.return_value = [
        Position(
            symbol="NVDA",
            qty=40.0,
            avg_entry=100.0,
            current_price=100.0,
            market_value=4000.0,
            unrealized_pnl=0.0,
            unrealized_intraday_pnl=0.0,
            sector="Tech",
        ),
    ]

    ok, _ = pipeline._finalize_protection_after_sell(
        order_id="alpaca-resolved",
        symbol="NVDA",
        position_qty_before_sell=100.0,
        cancelled_specs=cancelled,
    )

    assert ok is True
    # Should have collapsed to a SINGLE reprotect, not called restore.
    pipeline.broker._restore_stop_orders.assert_not_called()
    pipeline.broker._submit_protective_stop_retrying.assert_called_once()
    kwargs = pipeline.broker._submit_protective_stop_retrying.call_args.kwargs
    assert kwargs["qty"] == 40.0
    # Best stop_price among cancelled specs is 96.0 (most protective).
    assert kwargs["stop_price"] == 96.0
    db.close()


def test_finalize_persists_only_failed_specs_on_partial_restore(tmp_path):
    """Codex r9 #2: 1 of 2 stops restored is still partial coverage. The
    failed spec must be persisted so a later session can retry just
    that one — but NOT the spec that already restored (would create a
    duplicate at the broker, or fail on held_for_orders). Pin: the
    persisted row carries only the failed spec, not all originals."""
    from src.storage.db import Database

    db = Database(str(tmp_path / "t.db"))
    db.initialize()

    pipeline = build_pipeline(db=db, broker=MagicMock(), _format_qty=lambda q: str(q))

    spec_a = {"id": "stop-a", "qty": 50, "stop_price": 95.0, "limit_price": 92.0}
    spec_b = {"id": "stop-b", "qty": 50, "stop_price": 96.0, "limit_price": 93.0}
    cancelled = [spec_a, spec_b]

    # Order is terminal (canceled, no fill). Restore: 1 of 2 succeeds —
    # spec_a landed, spec_b failed.
    pipeline.broker.get_order_fill_info.return_value = {
        "status": "canceled",
        "filled_qty": "0",
        "filled_avg_price": None,
    }
    pipeline.broker._restore_stop_orders.return_value = (1, [spec_b])

    ok, _retry_specs = pipeline._finalize_protection_after_sell(
        order_id="alpaca-resolved",
        symbol="NVDA",
        position_qty_before_sell=100.0,
        cancelled_specs=cancelled,
    )

    assert ok is False, "partial restore must be flagged as incomplete coverage"
    rows = db.get_pending_protection_restores()
    assert len(rows) == 1
    import json as _json

    persisted = _json.loads(rows[0]["specs_json"])
    # Only spec_b (the failed one) should be in the persisted recovery —
    # NOT spec_a (already alive at broker).
    assert len(persisted) == 1
    assert persisted[0]["id"] == "stop-b"
    db.close()


def test_finalize_persists_recovery_when_restore_raises_in_non_drain_path(tmp_path):
    """Codex r9 #1: when restore raises during a normal SELL-finalize
    flow (not from drain), the bool False return is propagated but the
    SELL-path callers ignore it. Without persistence inside finalize,
    the recovery intent is silently lost. Pin: the failure branch
    writes a row when from_drain=False."""
    from src.storage.db import Database

    db = Database(str(tmp_path / "t.db"))
    db.initialize()

    pipeline = build_pipeline(db=db, broker=MagicMock(), _format_qty=lambda q: str(q))

    cancelled = [{"id": "stop-old", "qty": 100, "stop_price": 95.0}]
    pipeline.broker.get_order_fill_info.return_value = {
        "status": "canceled",
        "filled_qty": "0",
        "filled_avg_price": None,
    }
    pipeline.broker._restore_stop_orders.side_effect = RuntimeError("api 503")

    ok, _retry_specs = pipeline._finalize_protection_after_sell(
        order_id="alpaca-resolved",
        symbol="NVDA",
        position_qty_before_sell=100.0,
        cancelled_specs=cancelled,
        # from_drain defaults to False — this is the SELL-path entry case.
    )

    assert ok is False
    rows = db.get_pending_protection_restores()
    assert len(rows) == 1
    assert rows[0]["sell_order_id"] == "alpaca-resolved"
    db.close()


def test_finalize_persists_recovery_when_reprotect_raises_in_non_drain_path(tmp_path):
    """Same idea for the partial-fill branch: reprotect submit raises
    after a partial fill. Non-drain caller (e.g., morning ExecutionStage
    finalize) ignores the False bool, so finalize itself must persist."""
    from src.storage.db import Database

    db = Database(str(tmp_path / "t.db"))
    db.initialize()

    pipeline = build_pipeline(db=db, broker=MagicMock(), _format_qty=lambda q: str(q))

    cancelled = [{"id": "stop-old", "qty": 100, "stop_price": 95.0}]
    pipeline.broker.get_order_fill_info.return_value = {
        "status": "canceled",
        "filled_qty": "12",
        "filled_avg_price": "117.5",
    }
    pipeline.broker._submit_protective_stop_retrying.side_effect = RuntimeError("rejected")

    ok, _retry_specs = pipeline._finalize_protection_after_sell(
        order_id="alpaca-partial",
        symbol="NVDA",
        position_qty_before_sell=100.0,
        cancelled_specs=cancelled,
    )

    assert ok is False
    rows = db.get_pending_protection_restores()
    assert len(rows) == 1


def test_finalize_does_not_double_persist_when_called_from_drain(tmp_path):
    """Drain path's safety net: when finalize is called with
    from_drain=True, the failure branches must NOT call
    _persist_orphaned_protection_restore — the row already exists in
    DB. The drain caller uses the False return to keep the existing
    row alive instead. Pin: zero new rows after a from_drain failure."""
    from src.storage.db import Database
    import json as _json

    db = Database(str(tmp_path / "t.db"))
    db.initialize()
    cancelled = [{"id": "stop-old", "qty": 100, "stop_price": 95.0}]
    db.insert_pending_protection_restore(
        symbol="NVDA",
        sell_order_id="alpaca-orphan",
        position_qty_before_sell=100.0,
        specs_json=_json.dumps(cancelled),
    )

    pipeline = build_pipeline(db=db, broker=MagicMock(), _format_qty=lambda q: str(q))
    pipeline.broker.get_order_fill_info.return_value = {
        "status": "canceled",
        "filled_qty": "0",
        "filled_avg_price": None,
    }
    pipeline.broker._restore_stop_orders.side_effect = RuntimeError("api 503")

    ok, _retry_specs = pipeline._finalize_protection_after_sell(
        order_id="alpaca-orphan",
        symbol="NVDA",
        position_qty_before_sell=100.0,
        cancelled_specs=cancelled,
        from_drain=True,
    )

    assert ok is False
    # Still exactly 1 row (the original) — finalize did NOT persist again.
    rows = db.get_pending_protection_restores()
    assert len(rows) == 1
    db.close()


def test_drain_keeps_row_when_restore_submits_zero_stops(tmp_path):
    """Codex r8 #3: drain must not delete the recovery row if finalize
    couldn't actually rebuild coverage. Pin the no-fill branch where
    _restore_stop_orders is called but every per-spec submit fails
    (broker rejects, etc.) so it returns 0 — row must stay so a later
    session can retry."""
    from src.storage.db import Database
    import json as _json

    db = Database(str(tmp_path / "t.db"))
    db.initialize()
    cancelled = [
        {"id": "stop-old-a", "qty": 50, "stop_price": 95.0, "limit_price": 92.0},
        {"id": "stop-old-b", "qty": 50, "stop_price": 95.0, "limit_price": 92.0},
    ]
    db.insert_pending_protection_restore(
        symbol="NVDA",
        sell_order_id="alpaca-resolved",
        position_qty_before_sell=100.0,
        specs_json=_json.dumps(cancelled),
    )

    pipeline = build_pipeline(db=db, broker=MagicMock())
    pipeline.broker.get_order_fill_info.return_value = {
        "status": "canceled",
        "filled_qty": "0",
        "filled_avg_price": None,
    }
    # Broker rejects every restore attempt (e.g., a residual stop is
    # still hanging around or position isn't visible). 0 of 2 restored,
    # both specs in failed list. PR S: return is now (count, failed_specs).
    pipeline.broker._restore_stop_orders.return_value = (0, cancelled)

    drained = pipeline._drain_pending_protection_restores()

    assert len(drained) == 0
    # Row must STILL be there — coverage wasn't rebuilt.
    rows = db.get_pending_protection_restores()
    assert len(rows) == 1
    assert rows[0]["sell_order_id"] == "alpaca-resolved"
    db.close()


def test_drain_keeps_row_when_restore_raises(tmp_path):
    """Same idea, but the failure mode is _restore_stop_orders raising
    rather than returning 0. The except branch must also signal failure
    to drain so the row survives for retry."""
    from src.storage.db import Database
    import json as _json

    db = Database(str(tmp_path / "t.db"))
    db.initialize()
    cancelled = [{"id": "stop-old", "qty": 100, "stop_price": 95.0}]
    db.insert_pending_protection_restore(
        symbol="NVDA",
        sell_order_id="alpaca-resolved",
        position_qty_before_sell=100.0,
        specs_json=_json.dumps(cancelled),
    )

    pipeline = build_pipeline(db=db, broker=MagicMock())
    pipeline.broker.get_order_fill_info.return_value = {
        "status": "canceled",
        "filled_qty": "0",
        "filled_avg_price": None,
    }
    pipeline.broker._restore_stop_orders.side_effect = RuntimeError("api 503")

    drained = pipeline._drain_pending_protection_restores()

    assert len(drained) == 0
    rows = db.get_pending_protection_restores()
    assert len(rows) == 1
    db.close()


def test_drain_keeps_row_when_reprotect_raises_for_partial_fill(tmp_path):
    """Drain partial-fill branch: fill_qty=12 of 100 → reprotect on
    residual=88. If _submit_protective_stop_retrying raises (broker rejects),
    finalize returns False → drain keeps the row."""
    from src.storage.db import Database
    import json as _json

    db = Database(str(tmp_path / "t.db"))
    db.initialize()
    cancelled = [{"id": "stop-old", "qty": 100, "stop_price": 95.0}]
    db.insert_pending_protection_restore(
        symbol="NVDA",
        sell_order_id="alpaca-resolved",
        position_qty_before_sell=100.0,
        specs_json=_json.dumps(cancelled),
    )

    pipeline = build_pipeline(db=db, broker=MagicMock())
    pipeline.broker.get_order_fill_info.return_value = {
        "status": "canceled",
        "filled_qty": "12",
        "filled_avg_price": "117.5",
    }
    pipeline.broker._submit_protective_stop_retrying.side_effect = RuntimeError("api error")
    pipeline._format_qty = lambda q: str(q)

    drained = pipeline._drain_pending_protection_restores()

    assert len(drained) == 0
    rows = db.get_pending_protection_restores()
    assert len(rows) == 1
    db.close()


def test_drain_does_not_re_persist_when_called_from_drain_path(tmp_path):
    """If a row's broker state regresses to non-terminal between drain's
    own check and finalize's check (race), finalize must not call
    _persist_orphaned_protection_restore — that would create a
    duplicate row. ``from_drain=True`` guards against this."""
    from src.storage.db import Database
    import json as _json

    db = Database(str(tmp_path / "t.db"))
    db.initialize()
    cancelled = [{"id": "stop-old", "qty": 100, "stop_price": 95.0}]
    db.insert_pending_protection_restore(
        symbol="NVDA",
        sell_order_id="alpaca-resolved",
        position_qty_before_sell=100.0,
        specs_json=_json.dumps(cancelled),
    )

    pipeline = build_pipeline(db=db, broker=MagicMock())
    # Drain check sees terminal; finalize's own re-check sees non-terminal
    # (rare race). The cancel attempt then fails. Without from_drain=True
    # this would persist a SECOND row.
    pipeline.broker.get_order_fill_info.side_effect = [
        {"status": "canceled", "filled_qty": "0"},  # drain's check
        {"status": "new", "filled_qty": "0"},  # finalize's re-check (regressed)
    ]
    pipeline.broker.cancel_entry_order.side_effect = RuntimeError("api timeout")

    pipeline._drain_pending_protection_restores()

    rows = db.get_pending_protection_restores()
    # Exactly 1 row — original, NOT duplicated. (Original survives because
    # finalize returned False; new row not added because from_drain=True.)
    assert len(rows) == 1
    db.close()


def test_drain_leaves_row_when_sell_still_non_terminal(tmp_path):
    """If broker still reports the SELL as non-terminal, leave the row
    for a later drain. Otherwise we'd repeat the held_for_orders bug
    we were trying to defer past in the first place."""
    from src.storage.db import Database
    import json as _json

    db = Database(str(tmp_path / "t.db"))
    db.initialize()

    cancelled = [{"id": "stop-old", "qty": 100, "stop_price": 95.0}]
    db.insert_pending_protection_restore(
        symbol="NVDA",
        sell_order_id="alpaca-still-pending",
        position_qty_before_sell=100.0,
        specs_json=_json.dumps(cancelled),
    )

    pipeline = build_pipeline(db=db, broker=MagicMock())
    pipeline.broker.get_order_fill_info.return_value = {
        "status": "pending_cancel",
        "filled_qty": "0",
        "filled_avg_price": None,
    }
    pipeline._reprotect_residual_after_partial_sell = MagicMock()

    drained = pipeline._drain_pending_protection_restores()

    assert len(drained) == 0
    pipeline.broker._restore_stop_orders.assert_not_called()
    # Row NOT deleted — still pending.
    assert len(db.get_pending_protection_restores()) == 1
    db.close()


def test_finalize_protection_bails_when_lingering_cancel_fails():
    """If we can't even cancel the lingering SELL (API timeout etc.),
    we have no clean state to finalize from. Restoring stops anyway
    would compound the problem — broker has live SELL + about-to-be
    submitted stop on the same shares. Better to bail with a loud
    warning and let the next session's reconcile rebuild coverage."""
    pipeline = build_pipeline(
        broker=MagicMock(), _format_qty=lambda q: str(q), _reprotect_residual_after_partial_sell=MagicMock()
    )

    pipeline.broker.get_order_fill_info.return_value = {
        "status": "new",
        "filled_qty": "0",
        "filled_avg_price": None,
    }
    pipeline.broker.cancel_entry_order.side_effect = RuntimeError("api timeout")

    cancelled = [{"id": "stop-old", "qty": 100, "stop_price": 95.0}]

    pipeline._finalize_protection_after_sell(
        order_id="alpaca-stuck",
        symbol="NVDA",
        position_qty_before_sell=100.0,
        cancelled_specs=cancelled,
    )

    pipeline.broker.cancel_entry_order.assert_called_once()
    # Critical: NO restore, NO reprotect — leaving broker state alone
    # is safer than compounding the inconsistency.
    pipeline.broker._restore_stop_orders.assert_not_called()
    pipeline._reprotect_residual_after_partial_sell.assert_not_called()


def test_partial_trim_reprotects_actual_residual_on_partial_fill(tmp_path):
    """If the limit only partially fills (12 of 15), the position is
    100 - 12 = 88, NOT 100 - 15 = 85. The stop was shrunk to 85 for the
    sell; the finalizer reads the broker's position (the source of truth,
    not the submitted qty) and grows the same stop to 88 in place."""
    from src.storage.db import Database

    db = Database(str(tmp_path / "t.db"))
    db.initialize()
    db.insert_trade("NVDA", "BUY", 100, 100.0, "opened", "r1", stop_loss=90.0)

    pipeline = build_pipeline(db=db, broker=MagicMock())
    cancelled = [
        {"id": "stop-old", "qty": 100, "stop_price": 95.0, "limit_price": 92.0},
    ]
    _mock_amend_seam(
        pipeline.broker,
        specs=cancelled,
        kept_qty=85,
        held_after=88,
        sell_status="canceled",
        fill_info={"status": "canceled", "filled_qty": "12", "filled_avg_price": "117.5"},
        sell_id="tp-partial",
    )

    winner = Position(
        symbol="NVDA",
        qty=100,
        avg_entry=100,
        current_price=135,
        market_value=13500,
        unrealized_pnl=3500,
        sector="Technology",
    )

    _partial_trim(pipeline, winner, qty=15.0, run_id="r2")

    _assert_trim_never_naked(pipeline.broker, [85, 88])
    pipeline.broker._submit_protective_stop_retrying.assert_not_called()
    db.close()


def _halt_ready(pipeline, *, covered_qty=None):
    """Minimal wiring for `_halt_on_daily_loss_breach` on a __new__ stub.

    `covered_qty` is what the broker reports as stop-covered per symbol:
    None means "ask the position's own held qty", i.e. fully protected.
    """
    pipeline.db = getattr(pipeline, "db", None) or MagicMock()
    pipeline._reconcile_fills = MagicMock()
    pipeline._reconcile_stop_coverage = MagicMock(return_value=[])
    pipeline.broker.snapshot_protective_stops.side_effect = lambda sym, side="sell": (
        True,
        [{"qty": 1e9 if covered_qty is None else covered_qty}],
    )
    return pipeline


def test_pipeline_init_propagates_allow_margin_to_risk_engine():
    """Codex r11 P2: TradingPipeline.__init__ rebuilds RiskConfig for the
    deterministic engine. Previously it omitted allow_margin → engine
    defaulted to False even when settings.yaml had allow_margin=true.
    Mismatch: prompts + force_delever read config.risk.allow_margin
    directly (saw True), but the hard cash_only rule still blocked
    BUY → user opting in to margin had BUYs killed by a rule the
    agent didn't know was active.

    Pin: pipeline.risk_engine.config.allow_margin == config.risk.allow_margin."""
    from unittest.mock import patch as _patch
    from src.pipeline import TradingPipeline

    mock_config = MagicMock()
    mock_config.risk.max_position_pct = 15.0
    mock_config.risk.max_total_position_pct = 90.0
    mock_config.risk.max_sector_pct = 40.0
    mock_config.risk.require_stop_loss = True
    mock_config.risk.allow_margin = True  # ← the load-bearing field
    mock_config.alpaca.api_key = "x"
    mock_config.alpaca.secret_key = "y"
    mock_config.alpaca.paper = True
    mock_config.storage.db_path = ":memory:"
    mock_config.trading.universe = ["SPY"]
    mock_config.llm.tech_analyst_model = "claude-sonnet-4-6-20250514"
    mock_config.llm.tech_analyst_max_tokens = 8000
    mock_config.llm.macro_analyst_model = "claude-sonnet-4-6-20250514"
    mock_config.llm.news_analyst_model = "claude-sonnet-4-6-20250514"
    mock_config.llm.earnings_analyst_model = "claude-sonnet-4-6-20250514"
    mock_config.llm.portfolio_manager_model = "claude-sonnet-4-6-20250514"
    mock_config.llm.risk_manager_model = "claude-sonnet-4-6-20250514"
    mock_config.llm.position_reviewer_model = "claude-sonnet-4-6-20250514"
    mock_config.llm.evening_analyst_model = "claude-sonnet-4-6-20250514"
    mock_config.llm.meta_reflector_model = "claude-sonnet-4-6-20250514"
    mock_config.llm.get_max_tokens = MagicMock(return_value=8000)

    with (
        _patch("src.pipeline.AlpacaBroker"),
        _patch("src.pipeline.EarningsDataProvider"),
        _patch("src.pipeline.NewsDataProvider"),
        _patch("src.pipeline.MacroAnalystAgent"),
        _patch("src.pipeline.NewsAnalystAgent"),
        _patch("src.pipeline.TechAnalystAgent"),
        _patch("src.pipeline.PortfolioManagerAgent"),
        _patch("src.pipeline.RiskManagerAgent"),
        _patch("src.pipeline.EarningsAnalystAgent"),
        _patch("src.pipeline.MarketDataProvider"),
        _patch("src.pipeline.MacroDataProvider"),
    ):
        pipeline = TradingPipeline(mock_config)

    assert pipeline.risk_engine.config.allow_margin is True, (
        "settings.yaml allow_margin=True must propagate into the "
        "deterministic RiskRuleEngine; otherwise prompts say 'margin OK' "
        "while cash_only silently blocks every margin-using BUY"
    )


def test_pipeline_midday_reconciles_fills_before_reviewer_prompt(tmp_path):
    """Codex r11 P2: morning's final reconcile is run_id-scoped, so a BUY
    whose fill landed AFTER morning's wait window stays at fill_status=
    'submitted' in DB. The reviewer's executed_only=True query then
    skips that holding even though the broker shows the position —
    losing entry/stop/thesis context.

    Pin: midday must call _reconcile_fills BEFORE get_trades for the
    reviewer prompt. Use a real DB so we can verify a 'submitted' row
    actually flips to 'filled' and shows up in the reviewer's trade list."""
    from src.storage.db import Database

    db = Database(str(tmp_path / "t.db"))
    db.initialize()
    # Morning BUY: still 'submitted' from the run-id-scoped reconcile.
    db.insert_trade(
        symbol="SPY",
        action="BUY",
        qty=10.0,
        price=500.0,
        reasoning="morning entry",
        run_id="morning-r1",
        broker_order_id="alpaca-late-fill",
        fill_status="submitted",
        stop_loss=480.0,
        take_profit=540.0,
    )

    pipeline = build_pipeline(
        db=db, broker=MagicMock(), macro=MagicMock(), risk_engine=MagicMock(), position_reviewer=MagicMock()
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
    # Broker reports the late fill — our scoped reconcile in run_morning
    # didn't see it because the order_id wasn't tied to morning's run_id
    # at terminal-status time, but a fresh unscoped reconcile here will.
    pipeline.broker.get_order_fill_info.return_value = {
        "status": "filled",
        "filled_qty": "10.0",
        "filled_avg_price": "500.0",
    }
    pipeline.macro.get_macro_summary.return_value = {}
    pipeline.config = MagicMock()
    pipeline.config.llm.position_reviewer_model = "test-model"
    pipeline._handle_ex_dividends = MagicMock(return_value=[])
    pipeline._run_news_update = MagicMock(return_value=(None, None))
    pipeline._load_earnings_analyses = MagicMock(return_value=(None, []))
    pipeline._midday_execute_llm_actions = MagicMock(return_value=[])
    pipeline.position_reviewer.review.return_value = (
        PositionReview(reasoning_chain=_review_rc(), actions=[], overall_assessment="stable", risk_level="low"),
        _mock_agent_result(),
    )

    result = pipeline.run_midday()

    assert result["status"] == "reviewed"
    # The 'submitted' row must be reconciled to 'filled' BEFORE the
    # reviewer reads it — otherwise executed_only=True drops it.
    rows = db.execute("SELECT fill_status FROM trades WHERE broker_order_id = 'alpaca-late-fill'").fetchall()
    assert rows[0]["fill_status"] == "filled", (
        "morning BUY must be reconciled to 'filled' before the reviewer "
        "queries with executed_only=True; otherwise reviewer loses entry context"
    )
    # And the reviewer DID see it.
    rev_kwargs = pipeline.position_reviewer.review.call_args.kwargs
    morning_trades = rev_kwargs.get("morning_trades") or []
    assert any(t.get("symbol") == "SPY" for t in morning_trades), (
        "post-reconcile SPY BUY must surface in reviewer's morning_trades"
    )
    db.close()


def test_pipeline_midday_fetches_only_executed_morning_trades():
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
    pipeline.config = MagicMock()
    pipeline.config.llm.position_reviewer_model = "test-model"
    pipeline._handle_ex_dividends = MagicMock(return_value=[])
    pipeline._run_news_update = MagicMock(return_value=(None, None))
    pipeline._load_earnings_analyses = MagicMock(return_value=(None, []))
    pipeline._midday_execute_llm_actions = MagicMock(return_value=[])
    pipeline._reconcile_fills = MagicMock()
    pipeline.position_reviewer.review.return_value = (
        PositionReview(reasoning_chain=_review_rc(), actions=[], overall_assessment="stable", risk_level="low"),
        _mock_agent_result(),
    )

    result = pipeline.run_midday()

    assert result["status"] == "reviewed"
    # Two get_trades calls now: one for the morning_trades context (was the
    # only call before), one for _symbols_already_trimmed_today (the same-day
    # trim discipline added after the 2026-05-04 AMZN double-trim incident).
    # The first MUST still use executed_only=True so canceled morning orders
    # don't pollute the reviewer's "what trades fired" context.
    morning_call_kwargs = {
        "limit": 50,
        "today_only": True,
        "executed_only": True,
    }
    morning_calls = [c for c in pipeline.db.get_trades.call_args_list if c.kwargs == morning_call_kwargs]
    assert len(morning_calls) == 1, (
        f"morning_trades fetch must still be exactly one call with "
        f"executed_only=True; got {pipeline.db.get_trades.call_args_list}"
    )


def test_no_fixed_gain_automatic_profit_trim_exists():
    """Doctrine, mechanically enforced (owner decision 2026-09-12): the ONLY
    exit rule is the trailing stop. A preset profit target — sell a fixed
    fraction at a fixed gain, decided in advance — is rejected outright,
    the same way reward:risk was removed as a universal gate: the reward
    side of a trade cannot be predetermined because the holding period is
    unknown. The deleted `_auto_take_profit` (15% off at +30%, tuned on ONE
    GOOGL trade) is the shape this test exists to keep out.

    Static checks so a re-introduction fails at import-free test time:
      1. no TradingPipeline method named like the deleted rule;
      2. nothing under src/ submits a sell or writes a trade labelled
         TAKE_PROFIT (the label survives only on historical DB rows);
      3. no function under src/ takes a profit-trigger / trim-fraction
         parameter.
    """
    import pathlib
    import re

    names = sorted(n for n in dir(TradingPipeline) if re.search(r"take_profit|auto_tp|profit_trim|profit_target", n))
    assert names == [], f"preset profit-target hook(s) on TradingPipeline: {names}"

    src = pathlib.Path(__file__).resolve().parents[1] / "src"
    offenders: list[str] = []
    label_re = re.compile(r"""(?:label|action)\s*=\s*["']TAKE_PROFIT["']""")
    param_re = re.compile(
        r"def\s+\w+\s*\([^)]*\b(?:profit_pct_trigger|profit_trigger_pct|"
        r"trim_fraction|take_profit_pct|profit_take_pct)\b",
        re.S,
    )
    for path in sorted(src.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        rel = path.relative_to(src.parent)
        for m in label_re.finditer(text):
            offenders.append(f"{rel}: {m.group(0)}")
        for m in param_re.finditer(text):
            offenders.append(f"{rel}: {m.group(0)[:80]}")
    assert offenders == [], (
        "a fixed-gain automatic profit trim has been reintroduced — the only "
        f"exit rule is the trailing stop (owner, 2026-09-12): {offenders}"
    )


def test_midday_does_not_trim_a_big_winner_on_gain_alone():
    """Behavioural twin of the static doctrine test: a position up +60% with
    a HOLD-only review must leave the book untouched at midday. Under the
    deleted rule this position would have been trimmed 15% before the
    reviewer ever saw it."""
    pipeline = build_pipeline(
        broker=MagicMock(), macro=MagicMock(), db=MagicMock(), risk_engine=MagicMock(), position_reviewer=MagicMock()
    )
    pipeline.broker.is_trading_day.return_value = True
    pipeline.broker.get_account.return_value = {"cash": 1000.0, "portfolio_value": 17000.0}
    pipeline.broker.get_positions.return_value = [
        Position(
            symbol="NVDA",
            qty=100.0,
            avg_entry=100.0,
            current_price=160.0,
            market_value=16000.0,
            unrealized_pnl=6000.0,
            sector="Technology",
        )
    ]
    pipeline.macro.get_macro_summary.return_value = {}
    pipeline.db.get_trades.return_value = []
    pipeline.config = MagicMock()
    pipeline.config.llm.position_reviewer_model = "test-model"
    pipeline._handle_ex_dividends = MagicMock(return_value=[])
    pipeline._run_news_update = MagicMock(return_value=(None, None))
    pipeline._load_earnings_analyses = MagicMock(return_value=(None, []))
    pipeline._reconcile_fills = MagicMock()
    pipeline.position_reviewer.review.return_value = (
        PositionReview(reasoning_chain=_review_rc(), actions=[], overall_assessment="stable", risk_level="low"),
        _mock_agent_result(),
    )

    result = pipeline.run_midday()

    assert result["status"] == "reviewed"
    assert result["orders"] == []
    pipeline.broker.submit_order.assert_not_called()
    assert not any("TAKE_PROFIT" in str(c) for c in pipeline.db.insert_trade.call_args_list), (
        "a TAKE_PROFIT trade row was written by a rule that no longer exists"
    )


def test_pipeline_evening_skips_non_trading_day():
    pipeline = build_pipeline(broker=MagicMock())
    pipeline.broker.is_trading_day.return_value = False

    result = pipeline.run_evening()

    assert result["status"] == "market_holiday"
    pipeline.broker.get_account.assert_not_called()


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
def test_pipeline_buys_use_refreshed_cash_after_sell_phase(
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
    mock_config,
    tmp_path,
):
    mock_config.storage.db_path = str(tmp_path / "test.db")
    mock_config.trading.universe = ["SPY", "QQQ"]
    mock_config.risk.max_position_pct = 40
    mock_config.risk.max_sector_pct = 90

    mock_ta = MagicMock()
    qqq_analysis = TechAnalysisResult(
        symbol="QQQ",
        rating="buy",
        entry_price=100.0,
        reference_target=110.0,
        stop_loss=95.0,
        support_levels=[95.0],
        resistance_levels=[110.0],
        computed_levels=[95.0, 110.0],
        atr_14=5.0 / 3.5,
        setup_type="range",
        expected_horizon_sessions=60,
        reasoning="Bullish",
        reasoning_chain=_trc(),
        thesis_invalid_if="closes below support",
    )
    mock_ta.analyze_batch.return_value = ({"QQQ": qqq_analysis}, _mock_agent_result())
    mock_ta_cls.return_value = mock_ta

    mock_pm = MagicMock()
    # Rotation: close SPY (target=0) + open QQQ at 30% weight. Constructor
    # turns target_weight_pct=0 on a held symbol into a full-exit SELL.
    mock_pm.decide.return_value = (
        PortfolioDecision(
            reasoning_chain=_pm_rc(),
            targets=[
                TargetPosition(
                    symbol="SPY",
                    target_weight_pct=0.0,
                    conviction="medium",
                    thesis="Rotate out",
                ),
                TargetPosition(
                    symbol="QQQ",
                    target_weight_pct=15.0,
                    conviction="high",
                    thesis="Rotate in",
                    thesis_invalid_if="closes below support",
                ),
            ],
            portfolio_view="Rotate from SPY to QQQ",
        ),
        _mock_agent_result(),
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
        _mock_agent_result(),
    )
    mock_rm_cls.return_value = mock_rm

    mock_market = MagicMock()
    mock_market.get_ohlcv.return_value = [
        MagicMock(date="2026-04-07", open=98, high=102, low=97, close=100, volume=1000000)
    ]
    mock_market_cls.return_value = mock_market

    mock_macro = MagicMock()
    mock_macro.get_macro_summary.return_value = {
        "vix": {"current": 18.0, "mean_5d": 17.5, "trend": "falling"},
        "treasury": {"us2y": 4.5, "us10y": 4.3, "spread_2_10": -0.2, "inverted": True},
        "fed_funds_rate": 5.25,
    }
    mock_macro_cls.return_value = mock_macro

    spy_position = Position(
        symbol="SPY",
        qty=30.0,
        avg_entry=100.0,
        current_price=100.0,
        market_value=3000.0,
        unrealized_pnl=0.0,
        sector="ETF",
    )

    mock_broker = MagicMock()
    mock_broker.list_open_orders_checked.return_value = (True, [])  # listing read, nothing open
    mock_broker.is_trading_day.return_value = True
    mock_broker.get_latest_price.return_value = 100.0
    mock_broker.get_latest_quote.return_value = {"bid_price": 100.0, "ask_price": 100.0}
    mock_broker.get_intraday_snapshots.return_value = {"QQQ": _today_snapshot(100.0)}
    # 3 account snapshots: (1) initial pre-research, (2) board item 178's
    # ExecutionStage pre-SELL/COVER refresh (nothing has traded yet, so the
    # book is unchanged from (1)), (3) post-sell refresh. The two
    # late-breach account reads that used to sit between (1) and the old
    # (2) went with the account-level loss breaker on 2026-09-20 (retired
    # item 32). ExecutionStage's pre-BUY refresh only fires when there were
    # no sells — this test has sells, so the post-sell refresh is reused for
    # BUY sizing.
    mock_broker.get_account.side_effect = [
        {"cash": 500.0, "portfolio_value": 10000.0, "last_equity": 10000.0},
        {"cash": 500.0, "portfolio_value": 10000.0, "last_equity": 10000.0},
        {"cash": 3500.0, "portfolio_value": 10000.0, "last_equity": 10000.0},
    ]
    mock_broker.get_positions.side_effect = [
        # First entry feeds the session-entry broker-truth coverage
        # reconciler. Unchanged: item 178's new pre-SELL/COVER refresh
        # lands inside this same 5-call sequence (still pre-sale, so it
        # reads the same SPY position the run-open snapshot already
        # returned) — the existing 5 entries already cover it exactly, one
        # of which previously went unused.
        [spy_position],
        [spy_position],
        [spy_position],
        [spy_position],
        [spy_position],  # the protected sell re-reads what is held after clearing the stops
        [],
    ]
    mock_broker.wait_for_order_terminal.return_value = "filled"
    mock_broker.submit_order.side_effect = [
        {"id": "sell-1", "status": "accepted", "symbol": "SPY"},
        {"id": "buy-1", "status": "accepted", "symbol": "QQQ"},
    ]
    _mock_stop_seam(mock_broker)
    mock_broker_cls.return_value = mock_broker

    mock_maa = MagicMock()
    mock_maa.analyze.return_value = (_macro_stub(regime="risk-on", outlook="bullish"), _mock_agent_result())
    mock_maa_cls.return_value = mock_maa

    mock_na = MagicMock()
    # NewsAnalystAgent.analyze() -> tuple[NewsIntelligenceReport | None,
    # AgentResult]. This used to hand back None — "the pipeline tolerates a
    # newsless run" — and that is no longer true, deliberately: since
    # docs/WORK.md item 20 shipped, a seat that was asked and whose answer
    # never arrived REFUSES the decision rather than being tolerated, so a
    # None here would make every fixture below assert against
    # `evidence_gate_skip` instead of the thing it is actually testing.
    # PM/RM are still mocked, so no production code reads the content.
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

    pipeline = TradingPipeline(mock_config)
    # Owner 14 Oct 2026: a judgement sale of a holding read as "no chart level"
    # now sells only when it fails a buy rule, and this fixture carries no
    # technical read to judge that. This test is about buying with the cash a sale freed, so the sale is
    # the chart-CONFIRMED break, which still sells ungated.
    from dataclasses import replace

    from src.exits.structural_protection import StructuralProtection

    _read = StructuralProtection._structural_protection_for_holding

    def _confirmed_break(self, *args, **kwargs):
        check = _read(self, *args, **kwargs)
        return replace(check, basis="structural_level_broken") if check.basis == "no_chart_level" else check

    with patch.object(StructuralProtection, "_structural_protection_for_holding", _confirmed_break):
        result = pipeline.run_morning()

    assert result["status"] == "executed"
    # Global stale-entry cancel in the preamble (no symbol) exactly once;
    # audit round 2 added SYMBOL-SCOPED cancels on full-exit SELLs, which is
    # why total call_count may exceed 1.
    global_cancels = [
        c for c in mock_broker.cancel_open_entry_orders.call_args_list if not c.args and not c.kwargs.get("symbol")
    ]
    assert len(global_cancels) == 1
    mock_broker.cancel_open_orders.assert_not_called()
    assert mock_broker.wait_for_order_terminal.call_count == 1
    sell_kw = mock_broker.submit_order.call_args_list[0].kwargs
    assert sell_kw["symbol"] == "SPY"
    assert sell_kw["qty"] == 30.0
    assert sell_kw["side"] == "sell"
    assert sell_kw["limit_price"] is None  # a plain market order since 2026-10-09
    # reference_price is plumbed through for fat-finger guard; value will be
    # the position's current price at sell time.
    assert sell_kw.get("reference_price") is not None

    buy_kw = mock_broker.submit_order.call_args_list[1].kwargs
    assert buy_kw["symbol"] == "QQQ"
    # Vol-adjusted (item 22 fix: ratified 5% envelope, not the stale 0.5%):
    # equity $10k × 5% = $500 risk budget, stop 95 vs entry 100 gives $5
    # risk/share → qty_by_risk = 100, no longer binding — target weight
    # 15% of $10k / $100 = qty_by_alloc = 15 is the constraint instead.
    assert buy_kw["qty"] == 15
    assert buy_kw["side"] == "buy"
    assert buy_kw["limit_price"] is None  # a plain market order (2026-10-09)
    assert buy_kw["stop_loss_price"] == 95.0
    assert buy_kw.get("reference_price") is not None


# ============================================================================
# Same-day trim discipline — end-to-end through _midday_execute_llm_actions.
# Existing tests pin the keyword matcher and morning-trades fetch contract;
# these pin the BEHAVIOR: a SELL/REDUCE on an already-trimmed symbol with a
# soft reason must NOT reach the broker, while one with a hard trigger or
# a TRAIL_STOP must pass through. Codified after the 2026-05-04 AMZN
# 41→21→11 share double-trim incident.
# ============================================================================


def _mk_midday_pipeline(position: Position) -> TradingPipeline:
    pipeline = build_pipeline(broker=MagicMock())
    hold(pipeline.broker, {position.symbol: position.qty})
    _mock_stop_seam(pipeline.broker)
    pipeline.broker.submit_order.return_value = {
        "id": "ord-1",
        "status": "accepted",
        "symbol": position.symbol,
    }
    pipeline.broker.wait_for_order_terminal.return_value = "filled"
    pipeline.broker.replace_stop_loss.return_value = {
        "id": "stop-1",
        "status": "accepted",
    }
    pipeline.db = MagicMock()
    pipeline._format_qty = lambda q: str(q)
    pipeline._full_sell_qty = lambda q: q
    pipeline._reduce_sell_qty = lambda q: q * 0.5
    pipeline._finalize_protection_after_sell = MagicMock(return_value=(True, []))
    return pipeline


def test_midday_blocks_second_sell_on_soft_reason_for_already_trimmed_symbol():
    """First SELL on AMZN today succeeded; midday/close LLM emits another
    SELL with a soft reason (`TARGET_BREACH`, valuation stretch, etc.).
    Pin: broker.submit_order is NOT called — discipline holds."""
    position = Position(
        symbol="AMZN",
        qty=20.0,
        avg_entry=180.0,
        current_price=210.0,
        market_value=4200.0,
        unrealized_pnl=600.0,
        unrealized_intraday_pnl=0.0,
        sector="Consumer Cyclical",
    )
    pipeline = _mk_midday_pipeline(position)
    review = PositionReview(
        reasoning_chain=_review_rc(),
        actions=[
            PositionAction(
                action="SELL",
                symbol="AMZN",
                reason="TARGET_BREACH — up 16% since entry, valuation stretched",
            )
        ],
        overall_assessment="trim winner",
        risk_level="low",
    )
    orders = pipeline._midday_execute_llm_actions(
        positions=[position],
        review=review,
        run_id="r-1",
        already_trimmed_today={"AMZN"},
    )
    assert orders == []
    pipeline.broker.submit_order.assert_not_called()
    pipeline.db.insert_trade.assert_not_called()


def test_midday_allows_second_sell_on_hard_trigger_for_already_trimmed_symbol():
    """Same scenario but the LLM explicitly cites a hard trigger
    (thesis_invalid_if, HIGH state-change reversal, bearish earnings,
    daily-loss circuit breaker, stop hit). Pin:
    discipline yields, broker.submit_order IS called."""
    position = Position(
        symbol="AMZN",
        qty=20.0,
        avg_entry=180.0,
        current_price=210.0,
        market_value=4200.0,
        unrealized_pnl=600.0,
        unrealized_intraday_pnl=0.0,
        sector="Consumer Cyclical",
    )
    pipeline = _mk_midday_pipeline(position)
    review = PositionReview(
        reasoning_chain=_review_rc(),
        actions=[
            PositionAction(
                action="SELL",
                symbol="AMZN",
                reason="thesis_invalid_if triggered — guidance cut, earnings call missed",
            )
        ],
        overall_assessment="exit on broken thesis",
        risk_level="high",
    )
    orders = pipeline._midday_execute_llm_actions(
        positions=[position],
        review=review,
        run_id="r-1",
        already_trimmed_today={"AMZN"},
    )
    assert len(orders) == 1
    pipeline.broker.submit_order.assert_called_once()


def test_midday_blocks_second_reduce_on_soft_reason_for_already_trimmed_symbol():
    """REDUCE is on the same discipline as SELL — soft reason on an
    already-trimmed name must not stack a second trim."""
    position = Position(
        symbol="AMZN",
        qty=20.0,
        avg_entry=180.0,
        current_price=210.0,
        market_value=4200.0,
        unrealized_pnl=600.0,
        unrealized_intraday_pnl=0.0,
        sector="Consumer Cyclical",
    )
    pipeline = _mk_midday_pipeline(position)
    review = PositionReview(
        reasoning_chain=_review_rc(),
        actions=[
            PositionAction(
                action="SELL",
                symbol="AMZN",
                reason="momentum slowing slightly, +13% on day",
            )
        ],
        overall_assessment="trim again",
        risk_level="low",
    )
    orders = pipeline._midday_execute_llm_actions(
        positions=[position],
        review=review,
        run_id="r-1",
        already_trimmed_today={"AMZN"},
    )
    assert orders == []
    pipeline.broker.submit_order.assert_not_called()


def test_midday_allows_trail_stop_on_already_trimmed_symbol():
    """TRAIL_STOP isn't a sell of shares — adjusting the protective stop
    is fine even after a same-day trim. Pin: replace_stop_loss IS called
    regardless of trim history."""
    position = Position(
        symbol="AMZN",
        qty=20.0,
        avg_entry=180.0,
        current_price=210.0,
        market_value=4200.0,
        unrealized_pnl=600.0,
        unrealized_intraday_pnl=0.0,
        sector="Consumer Cyclical",
    )
    pipeline = _mk_midday_pipeline(position)
    review = PositionReview(
        reasoning_chain=_review_rc(),
        actions=[
            PositionAction(
                action="TRAIL_STOP",
                symbol="AMZN",
                reason="lock in some of the +16% move",
                new_stop_price=195.0,
            )
        ],
        overall_assessment="tighten stop",
        risk_level="low",
    )
    orders = pipeline._midday_execute_llm_actions(
        positions=[position],
        review=review,
        run_id="r-1",
        already_trimmed_today={"AMZN"},
    )
    assert len(orders) == 1
    pipeline.broker.replace_stop_loss.assert_called_once()


def test_midday_first_sell_of_day_on_a_soft_reason_is_now_blocked():
    """CONTRACT CHANGE, spec Phase 3.3 (2026-08-27). This test previously
    asserted the opposite — that a first sale on a soft reason passes — and
    that was the loophole.

    The hard-trigger gate applied only to a symbol's SECOND sell-side action
    in a day, so a position's FIRST sale executed on soft reasoning entirely
    unchecked. A first sale is almost every sale. Both exits the evening
    review graded "premature" on 2026-08-26 (EPD, MRVL) were first sales and
    went straight through this gap.

    Every exit must now NAME a trigger. "momentum cooling, take some off" is
    a feeling, not new information, so it is refused. The position is held
    instead, protected by its broker-resident stop.
    """
    position = Position(
        symbol="NVDA",
        qty=10.0,
        avg_entry=400.0,
        current_price=420.0,
        market_value=4200.0,
        unrealized_pnl=200.0,
        unrealized_intraday_pnl=0.0,
        sector="Technology",
    )
    pipeline = _mk_midday_pipeline(position)
    review = PositionReview(
        reasoning_chain=_review_rc(),
        actions=[
            PositionAction(
                action="SELL",
                symbol="NVDA",
                reason="momentum cooling, take some off",
            )
        ],
        overall_assessment="trim winner",
        risk_level="low",
    )
    orders = pipeline._midday_execute_llm_actions(
        positions=[position],
        review=review,
        run_id="r-1",
        already_trimmed_today=set(),  # empty — first time today
    )
    assert orders == []
    pipeline.broker.submit_order.assert_not_called()


def test_midday_first_sell_of_day_passes_when_it_names_a_trigger():
    """The gate refuses feelings, not exits. A named trigger goes through on
    the first sale exactly as before — this is the other half of 3.3, and
    without it the change would just be a blanket freeze."""
    position = Position(
        symbol="NVDA",
        qty=10.0,
        avg_entry=400.0,
        current_price=420.0,
        market_value=4200.0,
        unrealized_pnl=200.0,
        unrealized_intraday_pnl=0.0,
        sector="Technology",
    )
    pipeline = _mk_midday_pipeline(position)
    review = PositionReview(
        reasoning_chain=_review_rc(),
        actions=[
            PositionAction(
                action="SELL",
                symbol="NVDA",
                reason="thesis_invalid triggered: closed below MA50 on 2x volume",
            )
        ],
        overall_assessment="thesis broke",
        risk_level="moderate",
    )
    orders = pipeline._midday_execute_llm_actions(
        positions=[position],
        review=review,
        run_id="r-1",
        already_trimmed_today=set(),
    )
    assert len(orders) == 1
    pipeline.broker.submit_order.assert_called_once()


def test_symbols_already_trimmed_today_recognises_force_delever_action():
    """force_delever writes action='FORCE_DELEVER'. The same-day-trim
    discipline must treat it as a sell-side action so a force-deleverage
    earlier today blocks an additional REDUCE / SELL of the same symbol
    by the position reviewer."""
    pipeline = build_pipeline(db=MagicMock())
    pipeline.db.get_trades.return_value = [
        {"action": "FORCE_DELEVER", "symbol": "NVDA", "fill_status": "filled"},
        {"action": "TRAIL_STOP", "symbol": "AAPL", "fill_status": "filled"},
        {"action": "SELL", "symbol": "TSLA", "fill_status": "rejected"},
    ]
    trimmed = pipeline._symbols_already_trimmed_today()
    # NVDA (FORCE_DELEVER) blocks; AAPL (TRAIL_STOP) is not a sell-of-shares;
    # TSLA (rejected) leaves the symbol fair-game for re-attempt.
    assert trimmed == {"NVDA"}


def test_sold_out_symbol_does_not_reach_position_reviewer():
    """2026-09-17 XOM incident: XOM was fully SOLD this morning (broker no
    longer holds it — only AAPL remains) yet the reviewer returned 7 actions
    including a HOLD for XOM on a 6-... well, 1-position book in this
    reduced repro. Root cause: `_symbols_already_trimmed_today` reads
    today's trade rows with no idea which of those symbols are still held,
    so a fully-closed name rides into the "Already Trimmed Today" prompt
    section that tells the LLM to render a decision for every name in it.

    Pin: the set actually handed to the reviewer must be restricted to
    symbols still in the broker-truth position list, so a sold-out name can
    never surface in the prompt (and therefore never in a fabricated
    action) again."""
    pipeline = build_pipeline(
        broker=MagicMock(), macro=MagicMock(), db=MagicMock(), risk_engine=MagicMock(), position_reviewer=MagicMock()
    )
    pipeline.broker.is_trading_day.return_value = True
    pipeline.broker.get_account.return_value = {"cash": 1000.0, "portfolio_value": 5000.0}
    pipeline.broker.get_positions.return_value = [
        Position(
            symbol="AAPL",
            qty=10.0,
            avg_entry=200.0,
            current_price=210.0,
            market_value=2100.0,
            unrealized_pnl=100.0,
            sector="Technology",
        )
    ]
    pipeline.macro.get_macro_summary.return_value = {}
    # XOM was fully SOLD this morning — a real sell-side row exists in the
    # trades table even though the broker no longer holds any XOM shares.
    pipeline.db.get_trades.return_value = [
        {"action": "SELL", "symbol": "XOM", "fill_status": "filled"},
    ]
    pipeline.config = MagicMock()
    pipeline.config.llm.position_reviewer_model = "test-model"
    pipeline._handle_ex_dividends = MagicMock(return_value=[])
    pipeline._run_news_update = MagicMock(return_value=(None, None))
    pipeline._load_earnings_analyses = MagicMock(return_value=(None, []))
    pipeline._midday_execute_llm_actions = MagicMock(return_value=[])
    pipeline._reconcile_fills = MagicMock()
    pipeline.position_reviewer.review.return_value = (
        PositionReview(reasoning_chain=_review_rc(), actions=[], overall_assessment="stable", risk_level="low"),
        _mock_agent_result(),
    )

    result = pipeline.run_midday()

    assert result["status"] == "reviewed"
    assert result["positions"] == 1, "reviewed count must match broker holdings (1), not trade-row history"

    review_kwargs = pipeline.position_reviewer.review.call_args.kwargs
    reviewed_symbols = {p.symbol for p in review_kwargs["positions"]}
    assert reviewed_symbols == {"AAPL"}, f"sold-out XOM must not reach review_positions: got {reviewed_symbols}"
    assert review_kwargs["already_trimmed_today"] == set(), (
        "XOM was fully sold (not merely trimmed while still held) — it must "
        "not appear in the 'Already Trimmed Today' set the reviewer prompt "
        f"renders as an actionable position: got {review_kwargs['already_trimmed_today']}"
    )


def test_partially_trimmed_still_held_symbol_stays_in_discipline_set():
    """Contrast case for the fix above: a symbol that was REDUCEd (not
    fully closed) this morning and is STILL in the broker book must remain
    in already_trimmed_today — that is the discipline the set exists to
    enforce (2026-05-04 AMZN double-trim). Only a fully-closed name should
    be dropped."""
    pipeline = build_pipeline(
        broker=MagicMock(), macro=MagicMock(), db=MagicMock(), risk_engine=MagicMock(), position_reviewer=MagicMock()
    )
    pipeline.broker.is_trading_day.return_value = True
    pipeline.broker.get_account.return_value = {"cash": 1000.0, "portfolio_value": 5000.0}
    pipeline.broker.get_positions.return_value = [
        Position(
            symbol="AMZN",
            qty=21.0,
            avg_entry=100.0,
            current_price=112.0,
            market_value=2352.0,
            unrealized_pnl=252.0,
            sector="Consumer Cyclical",
        )
    ]
    pipeline.macro.get_macro_summary.return_value = {}
    pipeline.db.get_trades.return_value = [
        {"action": "REDUCE", "symbol": "AMZN", "fill_status": "filled"},
    ]
    pipeline.config = MagicMock()
    pipeline.config.llm.position_reviewer_model = "test-model"
    pipeline._handle_ex_dividends = MagicMock(return_value=[])
    pipeline._run_news_update = MagicMock(return_value=(None, None))
    pipeline._load_earnings_analyses = MagicMock(return_value=(None, []))
    pipeline._midday_execute_llm_actions = MagicMock(return_value=[])
    pipeline._reconcile_fills = MagicMock()
    pipeline.position_reviewer.review.return_value = (
        PositionReview(reasoning_chain=_review_rc(), actions=[], overall_assessment="stable", risk_level="low"),
        _mock_agent_result(),
    )

    result = pipeline.run_midday()

    assert result["status"] == "reviewed"
    review_kwargs = pipeline.position_reviewer.review.call_args.kwargs
    assert review_kwargs["already_trimmed_today"] == {"AMZN"}, (
        "a still-held, partially-trimmed symbol must remain in the "
        f"discipline set: got {review_kwargs['already_trimmed_today']}"
    )


# ============================================================================
# FORCE_DELEVER persistence + inverse-ETF deprioritization
# ============================================================================


def test_force_delever_persists_exact_action_string_to_trades_table():
    """The same-day-trim discipline filters trades by action string. If
    force_delever wrote anything other than 'FORCE_DELEVER' (e.g.,
    'force_delever' lowercased, or 'FORCE-DELEVER' hyphenated) the
    discipline would miss it and allow a same-day double-trim on a
    symbol force-sold for margin reasons. Pin the exact string."""
    from src.pipeline_context import RunContext

    pipeline = build_pipeline(broker=MagicMock())
    _mock_stop_seam(pipeline.broker)
    pipeline.broker.submit_order.return_value = {
        "id": "ord-1",
        "status": "accepted",
        "symbol": "NVDA",
    }
    pipeline.broker.wait_for_order_terminal.return_value = "filled"
    pipeline.broker.get_account.return_value = {
        "cash": 5000.0,
        "portfolio_value": 5000.0,
        "last_equity": 5000.0,
    }
    pipeline.broker.get_positions.return_value = []
    pipeline.db = MagicMock()
    pipeline._format_qty = lambda q: str(q)
    pipeline._full_sell_qty = lambda q: q
    pipeline._finalize_protection_after_sell = MagicMock(return_value=(True, []))
    pipeline.config = MagicMock()
    pipeline.config.risk.allow_margin = False

    losing_position = Position(
        symbol="NVDA",
        qty=10.0,
        avg_entry=500.0,
        current_price=400.0,
        market_value=4000.0,
        unrealized_pnl=-1000.0,
        unrealized_intraday_pnl=0.0,
        sector="Technology",
    )
    ctx = RunContext(run_id="r-1", session="morning")
    ctx.cash = -500.0  # deficit, triggers de-lever
    ctx.positions = [losing_position]

    hold(pipeline.broker, {p.symbol: p.qty for p in ctx.positions})  # the broker holds what is de-levered
    pipeline._force_delever(ctx)

    assert pipeline.db.insert_trade.called, "force_delever must persist a trade row"
    insert_kwargs = pipeline.db.insert_trade.call_args.kwargs
    assert insert_kwargs["action"] == "FORCE_DELEVER", (
        f"action string drift would break same-day-trim discipline; got {insert_kwargs['action']!r}"
    )


def test_force_delever_sells_long_before_inverse_etf_hedge():
    """Mixed account: long NVDA losing money + SH (inverse-S&P hedge)
    losing money on a rally. Naive biggest-loser-first would pick the
    one with the most negative P&L first; if that's SH, force_delever
    would cut the HEDGE and leave the long naked — opposite of risk
    reduction. The tiered sort sells longs FIRST, then inverse ETFs
    only when no longs remain or the deficit isn't cleared yet."""
    from src.pipeline_context import RunContext

    pipeline = build_pipeline(broker=MagicMock())
    _mock_stop_seam(pipeline.broker)
    pipeline.broker.submit_order.return_value = {
        "id": "ord-1",
        "status": "accepted",
    }
    pipeline.broker.wait_for_order_terminal.return_value = "filled"
    pipeline.broker.get_account.return_value = {
        "cash": 0.0,
        "portfolio_value": 6000.0,
        "last_equity": 6000.0,
    }
    pipeline.broker.get_positions.return_value = []
    pipeline.db = MagicMock()
    pipeline._format_qty = lambda q: str(q)
    pipeline._full_sell_qty = lambda q: q
    pipeline._finalize_protection_after_sell = MagicMock(return_value=(True, []))
    pipeline.config = MagicMock()
    pipeline.config.risk.allow_margin = False

    # SH (inverse hedge) is the BIGGER loser on a rally day.
    sh_hedge = Position(
        symbol="SH",
        qty=100.0,
        avg_entry=20.0,
        current_price=18.0,
        market_value=1800.0,
        unrealized_pnl=-200.0,
        unrealized_intraday_pnl=0.0,
        sector="ETF",
    )
    # NVDA long, smaller loss.
    nvda_long = Position(
        symbol="NVDA",
        qty=10.0,
        avg_entry=420.0,
        current_price=400.0,
        market_value=4000.0,
        unrealized_pnl=-200.0,  # tie on P&L
        unrealized_intraday_pnl=0.0,
        sector="Technology",
    )
    ctx = RunContext(run_id="r-1", session="morning")
    ctx.cash = -300.0  # deficit small enough that ONE position clears it
    ctx.positions = [sh_hedge, nvda_long]

    hold(pipeline.broker, {p.symbol: p.qty for p in ctx.positions})  # the broker holds what is de-levered
    pipeline._force_delever(ctx)

    # First (and only) SELL must be on the LONG (NVDA), not the HEDGE (SH).
    first_sell_kwargs = pipeline.broker.submit_order.call_args_list[0].kwargs
    assert first_sell_kwargs["symbol"] == "NVDA", (
        f"force_delever must prefer longs over inverse-ETF hedges to avoid "
        f"un-hedging the book; first sold symbol={first_sell_kwargs['symbol']!r}"
    )
    # SH must not be touched while a long was available.
    sold_symbols = [c.kwargs["symbol"] for c in pipeline.broker.submit_order.call_args_list]
    assert "SH" not in sold_symbols


# ============================================================================
# NaN market_value guard in SELL pre-sum
# ============================================================================


def test_filter_hard_risk_decisions_skips_nan_market_value_in_sell_presum(tmp_path):
    """If broker returns NaN market_value (rare market-open glitch), the
    SELL pre-sum used to add NaN to sell_proceeds → effective_cash=NaN →
    every BUY hard-rule check passed (NaN > limit is False). Pin: NaN
    SELL is dropped from the pre-sum so cash budget stays conservative
    and BUYs go through the hard-rule path on real cash, not phantom."""
    import math as _math
    from src.config import RiskConfig
    from src.risk.rules import RiskRuleEngine

    pipeline = build_pipeline(
        risk_engine=RiskRuleEngine(
            RiskConfig(
                max_position_pct=20,
                max_total_position_pct=90,
                allow_margin=False,
                require_stop_loss=True,
            )
        )
    )

    nan_position = Position(
        symbol="GLITCH",
        qty=10.0,
        avg_entry=100.0,
        current_price=float("nan"),
        market_value=float("nan"),
        unrealized_pnl=0.0,
        unrealized_intraday_pnl=0.0,
        sector="Technology",
    )
    sell = TradeDecision(
        action="SELL",
        symbol="GLITCH",
        allocation_pct=50.0,
        entry_price=0.0,
        stop_loss=0.0,
        take_profit=0.0,
        reasoning="trim half",
    )
    buy = TradeDecision(
        action="BUY",
        symbol="AAPL",
        allocation_pct=10.0,
        entry_price=180.0,
        stop_loss=170.0,
        take_profit=200.0,
        reasoning="add",
    )
    allowed, _violations, _reasons = pipeline.risk_gate._filter_hard_risk_decisions(
        decisions=[sell, buy],
        positions=[nan_position],
        total_value=10000.0,
        cash=500.0,
        invested_target_pct=None,
        correlation_matrix={},
    )
    # SELL with NaN market_value is dropped from the pre-sum, so
    # effective_cash = 500 + 0 = 500 (not NaN). The BUY for $1000
    # (10% of $10k) exceeds 500 cash → cash_only rule blocks the BUY.
    buy_in_allowed = any(d.action == "BUY" for d in allowed)
    assert not buy_in_allowed, (
        "BUY must not slip through when SELL's market_value was NaN — effective_cash should not have been NaN-poisoned"
    )
    # The SELL itself still goes through (not its job to know its proceeds
    # for cash-budget purposes; broker just executes).
    assert any(d.action == "SELL" for d in allowed)


# ---------------------------------------------------------------------------
# audit F1: protection-restore is write-ahead. The recovery row is
# persisted BEFORE cancel_protective_stops/submit (sentinel order id),
# flipped to the real id / deleted by finalize, and recovered by the
# drain pass even after a hard process kill in the cancel→finalize window.
# ---------------------------------------------------------------------------


def _wal_pipeline(db):
    from src.pipeline import TradingPipeline

    p = build_pipeline(db=db, broker=MagicMock(), _format_qty=lambda q: str(q))
    return p


def test_write_ahead_row_persisted_with_sentinel_then_cleared_on_success(tmp_path):
    from src.storage.db import Database
    from src.pipeline import _WAL_SELL_SENTINEL

    db = Database(str(tmp_path / "t.db"))
    db.initialize()
    pipe = _wal_pipeline(db)

    specs = [{"id": "s1", "qty": 10, "stop_price": 90.0, "limit_price": 88.0}]
    wal_id = pipe._write_ahead_protection_restore("NVDA", 10.0, specs)

    # Row exists BEFORE any submit, keyed by the sentinel.
    rows = db.get_pending_protection_restores()
    assert len(rows) == 1
    assert rows[0]["id"] == wal_id
    assert rows[0]["sell_order_id"] == _WAL_SELL_SENTINEL
    assert json.loads(rows[0]["specs_json"]) == specs

    # SELL filled in full → finalize success → WAL row discharged.
    pipe.broker.get_order_fill_info.return_value = {
        "status": "filled",
        "filled_qty": "10",
        "filled_avg_price": 100.0,
    }
    pipe._current_position_qty_for_finalize = lambda s: 0.0
    ok, _ = pipe._finalize_protection_after_sell(
        "ord-real",
        "NVDA",
        10.0,
        specs,
        wal_row_id=wal_id,
    )
    assert ok is True
    assert db.get_pending_protection_restores() == []


def test_write_ahead_no_row_when_nothing_was_protected(tmp_path):
    from src.storage.db import Database

    db = Database(str(tmp_path / "t.db"))
    db.initialize()
    pipe = _wal_pipeline(db)
    assert pipe._write_ahead_protection_restore("NVDA", 10.0, []) is None
    assert db.get_pending_protection_restores() == []


def test_finalize_bail_updates_wal_row_not_duplicate(tmp_path):
    """A finalize bail must UPDATE the existing write-ahead row (flip
    sentinel→real id) — never INSERT a second row alongside it."""
    from src.storage.db import Database
    from src.pipeline import _WAL_SELL_SENTINEL

    db = Database(str(tmp_path / "t.db"))
    db.initialize()
    pipe = _wal_pipeline(db)

    specs = [{"id": "s1", "qty": 5, "stop_price": 90.0, "limit_price": 88.0}]
    wal_id = pipe._write_ahead_protection_restore("NVDA", 5.0, specs)

    # Lingering non-terminal SELL + cancel raises → first bail branch
    # persists recovery intent. With wal_row_id set it must UPDATE.
    pipe.broker.get_order_fill_info.return_value = {"status": "new"}
    pipe.broker.cancel_entry_order.side_effect = RuntimeError("broker down")

    ok, _ = pipe._finalize_protection_after_sell(
        "ord-real",
        "NVDA",
        5.0,
        specs,
        wal_row_id=wal_id,
    )
    assert ok is False
    rows = db.get_pending_protection_restores()
    assert len(rows) == 1, "must update the WAL row, not duplicate it"
    assert rows[0]["id"] == wal_id
    assert rows[0]["sell_order_id"] == "ord-real"  # sentinel flipped
    assert rows[0]["sell_order_id"] != _WAL_SELL_SENTINEL


def test_drain_sentinel_restores_when_position_intact(tmp_path):
    """The crash-safety payoff: a sentinel WAL row from a killed session.
    Drain restores the original stops from the broker's live position."""
    from src.storage.db import Database
    from src.pipeline import _WAL_SELL_SENTINEL

    db = Database(str(tmp_path / "t.db"))
    db.initialize()
    pipe = _wal_pipeline(db)

    specs = [{"id": "s1", "qty": 10, "stop_price": 90.0, "limit_price": 88.0}]
    db.insert_pending_protection_restore(
        symbol="NVDA",
        sell_order_id=_WAL_SELL_SENTINEL,
        position_qty_before_sell=10.0,
        specs_json=json.dumps(specs),
    )
    # SELL never went out → position intact at 10.
    pipe._current_position_qty_for_finalize = lambda s: 10.0
    pipe.broker._restore_stop_orders.return_value = (1, [])

    drained = pipe._drain_pending_protection_restores()

    assert len(drained) == 1
    pipe.broker._restore_stop_orders.assert_called_once()
    a = pipe.broker._restore_stop_orders.call_args
    assert a[0][0] == "NVDA" and a[0][1] == specs
    assert db.get_pending_protection_restores() == []


def test_drain_sentinel_noop_when_position_flat(tmp_path):
    """SELL filled before the crash → broker shows 0 shares → nothing to
    restore; row cleared, no stop submitted."""
    from src.storage.db import Database
    from src.pipeline import _WAL_SELL_SENTINEL

    db = Database(str(tmp_path / "t.db"))
    db.initialize()
    pipe = _wal_pipeline(db)
    db.insert_pending_protection_restore(
        symbol="NVDA",
        sell_order_id=_WAL_SELL_SENTINEL,
        position_qty_before_sell=10.0,
        specs_json=json.dumps([{"id": "s1", "qty": 10, "stop_price": 90.0}]),
    )
    pipe._current_position_qty_for_finalize = lambda s: 0.0

    drained = pipe._drain_pending_protection_restores()

    assert len(drained) == 1
    pipe.broker._restore_stop_orders.assert_not_called()
    assert db.get_pending_protection_restores() == []


def test_drain_sentinel_collapses_when_position_reduced(tmp_path):
    """Partial fill before the crash → position < original coverage →
    collapse to one most-protective stop on the actual residual."""
    from src.storage.db import Database
    from src.pipeline import _WAL_SELL_SENTINEL

    db = Database(str(tmp_path / "t.db"))
    db.initialize()
    pipe = _wal_pipeline(db)
    specs = [{"id": "s1", "qty": 10, "stop_price": 90.0}]
    db.insert_pending_protection_restore(
        symbol="NVDA",
        sell_order_id=_WAL_SELL_SENTINEL,
        position_qty_before_sell=10.0,
        specs_json=json.dumps(specs),
    )
    pipe._current_position_qty_for_finalize = lambda s: 4.0
    pipe._reprotect_residual_after_partial_sell = MagicMock(return_value=True)

    drained = pipe._drain_pending_protection_restores()

    assert len(drained) == 1
    pipe._reprotect_residual_after_partial_sell.assert_called_once_with(
        "NVDA",
        4.0,
        specs,
    )
    assert db.get_pending_protection_restores() == []


def test_midday_emergency_writes_wal_before_submit_survives_submit_crash(tmp_path):
    """End-to-end of the exact codex gap: cancel succeeds, then the
    process effectively dies at submit (submit_order raises). The
    write-ahead row must already be on disk so the next session's drain
    can rebuild coverage — pre-F1 nothing was persisted here."""
    from src.storage.db import Database
    from src.pipeline import _WAL_SELL_SENTINEL
    from src.models import Position

    db = Database(str(tmp_path / "t.db"))
    db.initialize()
    pipe = _wal_pipeline(db)

    specs = [{"id": "s1", "qty": 51, "stop_price": 200.0, "limit_price": 196.0}]
    _mock_stop_seam(pipe.broker, specs=specs)
    confirm_cancels(pipe.broker)  # the broker confirms the stop cancel before the sale
    pipe.broker.submit_order.side_effect = RuntimeError("SIGKILL-ish at submit")
    pipe._full_sell_qty = lambda q: q
    pipe.db.has_pending_action_for_symbol = lambda *a, **k: False

    pos = Position(
        symbol="AMZN",
        qty=51.0,
        avg_entry=240.0,
        current_price=230.0,
        market_value=11730.0,
        unrealized_pnl=-510.0,
        sector="Consumer Cyclical",
    )
    hold(pipe.broker, {pos.symbol: pos.qty})
    # The daily-loss liquidator that used to drive this is deleted
    # (docs/WORK.md item 32). The write-ahead discipline it exercised is
    # not: `_submit_protected_sell` is the shared seam every surviving
    # forced-sell path uses (`_force_delever`, the §11.2 gross ceiling),
    # so the test drives that directly instead of through a caller.
    pipe._submit_protected_sell(
        symbol=pos.symbol,
        qty=pos.qty,
        limit_price=227.7,
        reference_price=pos.current_price,
        position_qty_before_sell=pos.qty,
        label="FORCE_DELEVER",
    )

    rows = db.get_pending_protection_restores()
    assert len(rows) == 1, (
        "write-ahead row must survive a submit-time crash so drain can recover — this is the whole point of audit F1"
    )
    assert rows[0]["symbol"] == "AMZN"
    assert rows[0]["sell_order_id"] == _WAL_SELL_SENTINEL
    assert json.loads(rows[0]["specs_json"]) == specs


# ---------------------------------------------------------------------------
# audit F1 review #1: the WAL row must be durable BEFORE the broker
# cancels the stops — snapshot (read) -> persist WAL -> cancel (mutate).
# ---------------------------------------------------------------------------


def _wal_pipe(db):
    from src.pipeline import TradingPipeline

    p = build_pipeline(db=db, broker=MagicMock(), _format_qty=lambda q: str(q))
    return p


def test_cancel_stops_with_write_ahead_persists_before_cancel(tmp_path):
    """The crux of review #1: when broker.cancel_snapshotted_stops is
    invoked, the recovery row must ALREADY be committed. Capturing DB
    state at cancel-time proves the ordering, not just the end state."""
    from src.storage.db import Database
    from src.pipeline import _WAL_SELL_SENTINEL

    db = Database(str(tmp_path / "t.db"))
    db.initialize()
    pipe = _wal_pipe(db)
    specs = [{"id": "s1", "qty": 10, "stop_price": 90.0, "limit_price": 88.0}]
    pipe.broker.snapshot_protective_stops.return_value = (True, specs)

    seen_at_cancel = {}

    def _cancel(sym, sp):
        seen_at_cancel["rows"] = db.get_pending_protection_restores()
        return MagicMock(cleared=True)

    pipe.broker.cancel_snapshotted_stops.side_effect = _cancel

    ok, out_specs, wal_id = pipe._cancel_stops_with_write_ahead("NVDA", 10.0)

    assert ok is True and out_specs == specs and wal_id is not None
    # The row existed at the moment cancel was called — true write-ahead.
    assert len(seen_at_cancel["rows"]) == 1
    assert seen_at_cancel["rows"][0]["sell_order_id"] == _WAL_SELL_SENTINEL
    assert seen_at_cancel["rows"][0]["id"] == wal_id
    # And snapshot happened before cancel (read before mutate).
    pipe.broker.snapshot_protective_stops.assert_called_once_with("NVDA")
    pipe.broker.cancel_snapshotted_stops.assert_called_once()


def test_cancel_stops_with_write_ahead_no_stops_no_row_no_cancel(tmp_path):
    """No protective stops → nothing to write-ahead, cancel never called,
    SELL still proceeds (ok=True, no wal row)."""
    from src.storage.db import Database

    db = Database(str(tmp_path / "t.db"))
    db.initialize()
    pipe = _wal_pipe(db)
    pipe.broker.snapshot_protective_stops.return_value = (True, [])

    ok, specs, wal_id = pipe._cancel_stops_with_write_ahead("NVDA", 10.0)

    assert ok is True and specs == [] and wal_id is None
    pipe.broker.cancel_snapshotted_stops.assert_not_called()
    assert db.get_pending_protection_restores() == []


def test_cancel_stops_with_write_ahead_discharges_row_on_cancel_failure(tmp_path):
    """Cancel fails (rolled back by the broker) → stops are still live,
    SELL must be skipped, and the pre-written WAL row is discharged so
    the next drain doesn't 'restore' stops that never left."""
    from src.storage.db import Database

    db = Database(str(tmp_path / "t.db"))
    db.initialize()
    pipe = _wal_pipe(db)
    specs = [{"id": "s1", "qty": 10, "stop_price": 90.0}]
    pipe.broker.snapshot_protective_stops.return_value = (True, specs)
    pipe.broker.cancel_snapshotted_stops.return_value = MagicMock(cleared=False, coverage_shrank=False)  # rolled back

    ok, out_specs, wal_id = pipe._cancel_stops_with_write_ahead("NVDA", 10.0)

    assert ok is False and out_specs == [] and wal_id is None
    # Row must NOT leak — it was discharged on the cancel-rollback.
    assert db.get_pending_protection_restores() == []


def test_cancel_stops_with_write_ahead_skips_on_snapshot_failure(tmp_path):
    from src.storage.db import Database

    db = Database(str(tmp_path / "t.db"))
    db.initialize()
    pipe = _wal_pipe(db)
    pipe.broker.snapshot_protective_stops.return_value = (False, [])

    ok, specs, wal_id = pipe._cancel_stops_with_write_ahead("NVDA", 10.0)

    assert ok is False and specs == [] and wal_id is None
    pipe.broker.cancel_snapshotted_stops.assert_not_called()
    assert db.get_pending_protection_restores() == []


def test_finalize_pending_protections_waits_finalizes_and_logs_on_failure(caplog):
    """The shared SELL-tail helper waits for terminal, finalizes on actual
    fill, and logs a warning when coverage couldn't be rebuilt (drain retries
    next session). This is the behavior the 6 SELL paths used to copy-paste."""
    import logging

    pipe = build_pipeline(
        broker=MagicMock(), _finalize_protection_after_sell=MagicMock(return_value=(False, [{"id": "s1"}]))
    )
    pending = [
        {
            "order_id": "o1",
            "symbol": "NVDA",
            "position_qty_before_sell": 5.0,
            "specs": [{"id": "s1"}],
            "wal_row_id": 7,
        }
    ]
    with caplog.at_level(logging.WARNING, logger="src.pipeline"):
        pipe._finalize_pending_protections(pending, context="TestCtx")
    pipe.broker.wait_for_order_terminal.assert_called_once_with("o1")
    pipe._finalize_protection_after_sell.assert_called_once_with(
        "o1",
        "NVDA",
        5.0,
        [{"id": "s1"}],
        wal_row_id=7,
    )
    assert any(
        "did not confirm stop coverage" in r.getMessage() and "TestCtx" in r.getMessage() for r in caplog.records
    ), f"expected a coverage-not-confirmed warning; got {[r.getMessage() for r in caplog.records]}"


def test_finalize_pending_protections_skips_wait_when_wait_false():
    """wait=False (ExecutionStage, which already waited) must not re-wait."""
    pipe = build_pipeline(broker=MagicMock(), _finalize_protection_after_sell=MagicMock(return_value=(True, [])))
    pending = [
        {
            "order_id": "o2",
            "symbol": "AAPL",
            "position_qty_before_sell": 3.0,
            "specs": [],
            "wal_row_id": None,
        }
    ]
    pipe._finalize_pending_protections(pending, context="X", wait=False)
    pipe.broker.wait_for_order_terminal.assert_not_called()
    pipe._finalize_protection_after_sell.assert_called_once()


def _protected_sell_pipe(*, accepted=True, submit_raises=False, clear_ok=True):
    """A __new__'d pipeline wired just enough to exercise _submit_protected_sell."""
    pipe = build_pipeline(
        broker=MagicMock(),
        db=MagicMock(),
        _cancel_stops_with_write_ahead=MagicMock(
            return_value=(clear_ok, [{"id": "s1", "qty": 10}], 99),
        ),
        _order_accepted=MagicMock(return_value=accepted),
    )
    if submit_raises:
        pipe.broker.submit_order.side_effect = RuntimeError("broker down")
    else:
        pipe.broker.submit_order.return_value = {"id": "ord-1", "status": "accepted", "symbol": "NVDA"}
    hold(pipe.broker, {"NVDA": 10.0})  # what the broker holds when the exit is re-sized
    return pipe


def test_submit_protected_sell_accept_returns_order_and_prot():
    pipe = _protected_sell_pipe(accepted=True)
    out = pipe._submit_protected_sell(
        symbol="NVDA",
        qty=5,
        limit_price=99.0,
        reference_price=100.0,
        position_qty_before_sell=10.0,
        label="SELL",
    )
    assert out is not None
    order, prot = out
    assert order["action"] == "SELL"  # helper tags the action
    assert prot["order_id"] == "ord-1" and prot["symbol"] == "NVDA"
    assert prot["position_qty_before_sell"] == 10.0
    assert prot["specs"] == [{"id": "s1", "qty": 10}] and prot["wal_row_id"] == 99
    pipe.broker._restore_stop_orders.assert_not_called()  # no restore on success


def test_submit_protected_sell_skips_and_does_not_submit_when_clear_fails():
    pipe = _protected_sell_pipe(clear_ok=False)
    out = pipe._submit_protected_sell(
        symbol="NVDA",
        qty=5,
        limit_price=99.0,
        reference_price=100.0,
        position_qty_before_sell=10.0,
        label="SELL",
    )
    assert out is None
    pipe.broker.submit_order.assert_not_called()  # never submit if stops aren't cleared


def test_submit_protected_sell_restores_stops_on_reject():
    pipe = _protected_sell_pipe(accepted=False)
    out = pipe._submit_protected_sell(
        symbol="NVDA",
        qty=5,
        limit_price=99.0,
        reference_price=100.0,
        position_qty_before_sell=10.0,
        label="EMERGENCY_SELL",
    )
    assert out is None
    pipe.broker._restore_stop_orders.assert_called_once_with(
        "NVDA",
        [{"id": "s1", "qty": 10}],
        check_idempotency=True,  # put back up to held; a dying stop is never alive
    )


def test_submit_protected_sell_restores_stops_on_submit_throw():
    """Unified behavior: a submit that raises leaves the position intact with
    stops cancelled — restore them in-session (previously only the since-deleted
    auto take-profit did; the other paths rode naked until next drain)."""
    pipe = _protected_sell_pipe(submit_raises=True)
    out = pipe._submit_protected_sell(
        symbol="NVDA",
        qty=5,
        limit_price=99.0,
        reference_price=100.0,
        position_qty_before_sell=10.0,
        label="FORCE_DELEVER",
    )
    assert out is None
    pipe.broker._restore_stop_orders.assert_called_once_with(
        "NVDA",
        [{"id": "s1", "qty": 10}],
        check_idempotency=True,  # put back up to held; a dying stop is never alive
    )


def test_reconcile_stop_coverage_flags_undercovered_long():
    """A held long with less open protective-stop qty than held qty is a gap."""
    from types import SimpleNamespace

    pipe = build_pipeline(broker=MagicMock(), db=MagicMock())
    pipe.db.get_pending_protection_restores.return_value = []
    pipe.broker.get_positions.return_value = [
        SimpleNamespace(symbol="NVDA", qty=10.0),
        SimpleNamespace(symbol="AAPL", qty=5.0),
    ]

    def _snap(sym, side="sell"):
        assert side == "sell", "both positions here are longs"
        if sym == "NVDA":
            return (True, [{"id": "s1", "qty": 4.0}])  # only 4 of 10 covered
        return (True, [{"id": "s2", "qty": 5.0}])  # fully covered

    pipe.broker.snapshot_protective_stops.side_effect = _snap

    gaps = pipe._reconcile_stop_coverage()
    assert len(gaps) == 1
    assert gaps[0]["symbol"] == "NVDA"
    assert gaps[0]["held_qty"] == 10.0 and gaps[0]["covered_qty"] == 4.0


def test_reconcile_stop_coverage_skips_pending_flags_neither_when_covered():
    """Symbols the drain owns (pending row) are skipped — a fully-covered
    long and a fully-covered SHORT both produce no gap.

    Pre shorts-safe (Stage 2) a short was skipped outright here (`qty <= 0`)
    with the reasoning "a SELL-stop can't protect a short" — true, but the
    fix is to check the OTHER side's stops, not to skip the position. A
    short is now read on its own side via `snapshot_protective_stops(...,
    side="buy")`."""
    from types import SimpleNamespace

    pipe = build_pipeline(broker=MagicMock(), db=MagicMock())
    pipe.db.get_pending_protection_restores.return_value = [{"symbol": "TSLA"}]
    pipe.broker.get_positions.return_value = [
        SimpleNamespace(symbol="SQQQ", qty=-2.0),  # short, fully covered
        SimpleNamespace(symbol="TSLA", qty=8.0),  # drain owns it → skip
        SimpleNamespace(symbol="MSFT", qty=2.0),  # fully covered long
    ]
    pipe.broker.snapshot_protective_stops.return_value = (True, [{"id": "s", "qty": 2.0}])

    gaps = pipe._reconcile_stop_coverage()
    assert gaps == []
    # TSLA is the drain's — never even snapshotted.
    assert pipe.broker.snapshot_protective_stops.call_count == 2
    pipe.broker.snapshot_protective_stops.assert_any_call("MSFT", side="sell")
    pipe.broker.snapshot_protective_stops.assert_any_call("SQQQ", side="buy")


def test_reconcile_stop_coverage_repairs_undercovered_short():
    """A short with a real coverage gap is reported AND repaired from the
    SHORT row — the Stage 2 'flag only' line was false once SHORT became a
    live opening action."""
    from types import SimpleNamespace

    pipe = build_pipeline(broker=MagicMock(), db=MagicMock())
    pipe.db.get_pending_protection_restores.return_value = []
    pipe.db.get_symbol_last_buy.return_value = {"stop_loss": 220.0, "action": "SHORT"}
    pipe.broker.get_positions.return_value = [
        SimpleNamespace(symbol="TSLA", qty=-40.0),
    ]
    pipe.broker.snapshot_protective_stops.return_value = (
        True,
        [{"id": "s1", "qty": 25.0}],  # only 25 of 40 covered
    )
    pipe.broker.get_latest_price.return_value = 200.0
    pipe.broker.get_latest_quote.return_value = {"bid_price": 200.0, "ask_price": 200.0}
    pipe.broker._submit_protective_stop_retrying.return_value = {"id": "buy-stop-1"}

    gaps = pipe._reconcile_stop_coverage()
    assert len(gaps) == 1
    assert gaps[0]["symbol"] == "TSLA"
    assert gaps[0]["held_qty"] == -40.0 and gaps[0]["covered_qty"] == 25.0
    assert gaps[0]["repaired"] is True
    pipe.broker.snapshot_protective_stops.assert_called_once_with("TSLA", side="buy")
    kwargs = pipe.broker._submit_protective_stop_retrying.call_args.kwargs
    assert kwargs["side"] == "buy"
    assert kwargs["qty"] == 15.0
    assert kwargs["stop_price"] == 220.0
    assert pipe.db.get_symbol_last_buy.call_args.kwargs.get("action") == "SHORT"


# === Stage 1 (QAMC provider/model plumbing) — paper/live isolation ===


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
def test_broker_paper_flag_unaffected_by_new_provider_config(
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
    mock_config,
):
    """The new explicit-provider config field must have no path into
    Alpaca paper/live selection — AlpacaBroker(paper=...) must reflect ONLY
    config.alpaca.paper, regardless of what any agent's provider is set to
    (a new config knob near AlpacaConfig is exactly the kind of thing that
    could accidentally get wired into a 'live' foot-gun if handled carelessly)."""
    mock_config.llm.tech_analyst_provider = "openrouter"
    mock_config.llm.portfolio_manager_provider = "deepseek"
    mock_config.alpaca.paper = True

    TradingPipeline(mock_config)

    _, kwargs = mock_broker_cls.call_args
    assert kwargs.get("paper") is True
    assert kwargs.get("paper") == mock_config.alpaca.paper


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
def test_broker_paper_flag_false_still_passes_through_unmodified(
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
    mock_config,
):
    """paper=False (live) must also pass through explicitly — proving the
    value isn't silently coerced by anything Stage 1 touched."""
    mock_config.alpaca.paper = False

    TradingPipeline(mock_config)

    _, kwargs = mock_broker_cls.call_args
    assert kwargs.get("paper") is False


def test_sync_positions_from_broker_writes_full_snapshot_not_a_stale_subset(tmp_path):
    """Helper used after session snapshot/execution: broker book replaces
    the local table, including names the previous snapshot never had."""
    from src.storage.db import Database

    db = Database(str(tmp_path / "pos.db"))
    db.initialize()
    db.execute(
        """INSERT INTO positions (symbol, qty, avg_entry, current_price, market_value, unrealized_pnl, sector, updated_at)
           VALUES ('ORCL', 10, 140, 145, 1450, 50, 'Tech', datetime('now'))"""
    )
    db.conn.commit()

    pipeline = build_pipeline(db=db, broker=MagicMock())
    fresh = [
        Position(
            symbol=sym,
            qty=1.0,
            avg_entry=10.0,
            current_price=11.0,
            market_value=11.0,
            unrealized_pnl=1.0,
            sector="Tech",
        )
        for sym in ("ORCL", "MSFT", "NVDA", "AAPL", "AMZN", "GOOGL")
    ]
    pipeline.broker.get_positions.return_value = fresh

    pipeline._sync_positions_from_broker()

    rows = db.execute("SELECT symbol FROM positions ORDER BY symbol").fetchall()
    assert [r["symbol"] for r in rows] == ["AAPL", "AMZN", "GOOGL", "MSFT", "NVDA", "ORCL"]
    db.close()


def test_sync_positions_from_broker_uses_provided_snapshot_without_a_broker_call(tmp_path):
    from src.storage.db import Database

    db = Database(str(tmp_path / "pos.db"))
    db.initialize()
    pipeline = build_pipeline(db=db, broker=MagicMock())
    snapshot = [
        Position(
            symbol="MSFT",
            qty=2.0,
            avg_entry=400.0,
            current_price=410.0,
            market_value=820.0,
            unrealized_pnl=20.0,
            sector="Tech",
        ),
    ]
    pipeline._sync_positions_from_broker(snapshot)
    pipeline.broker.get_positions.assert_not_called()
    rows = db.execute("SELECT symbol, qty FROM positions").fetchall()
    assert rows[0]["symbol"] == "MSFT"
    assert rows[0]["qty"] == 2.0
    db.close()


def test_sync_positions_from_broker_failure_does_not_abort():
    """A snapshot-write failure must not take down the trading session."""
    pipeline = build_pipeline(db=MagicMock(), broker=MagicMock())
    pipeline.db.sync_positions.side_effect = RuntimeError("sqlite locked")
    pipeline.broker.get_positions.return_value = []
    pipeline._sync_positions_from_broker()  # must not raise


def test_pipeline_morning_syncs_positions_at_snapshot_and_after_reconcile():
    """Morning previously never wrote the local table. Snapshot + finally
    after reconcile_fills are the two moments the book is known."""
    pipeline = build_pipeline(broker=MagicMock(), morning_research_stage=MagicMock(), risk_engine=MagicMock())
    pipeline.broker.is_trading_day.return_value = True
    pipeline.broker.cancel_open_entry_orders.return_value = None
    pipeline.broker.get_account.return_value = {"cash": 1000.0, "portfolio_value": 5000.0}
    pipeline.broker.get_positions.return_value = []
    pipeline._reconcile_fills = MagicMock()
    pipeline.db = MagicMock()  # a real pipeline always owns a db; the reconciler records its outcome on it
    pipeline._sync_positions_from_broker = MagicMock()

    def _populate_empty_research(ctx):
        ctx.analyses = []

    pipeline.morning_research_stage.run.side_effect = _populate_empty_research

    result = pipeline.run_morning()

    assert result["status"] == "no_data"
    assert pipeline._sync_positions_from_broker.call_count >= 2


def test_intra_check_reconciles_outstanding_fills(tmp_path):
    """2026-09-17 AMD incident: AMD filled at $549.11 but the trades table
    still read 'submitted' — because `run_intra_check`, the half-hourly
    tick between morning and midday, never called `_reconcile_fills` at
    all. It reconciles protective-stop coverage and broker-initiated
    stop-out fills, but neither of those asks the broker about the fate of
    an order the desk itself submitted. This is the tightest-cadence
    session and therefore the one place a stale 'submitted' row should be
    caught soonest.

    Pin: a BUY left 'submitted' from an earlier session must flip to
    'filled' (with the real fill price) by the time `run_intra_check`
    returns, using a real DB so the fill_status transition is genuine,
    not a mocked call assertion."""
    from src.storage.db import Database

    db = Database(str(tmp_path / "t.db"))
    db.initialize()
    db.insert_trade(
        symbol="AMD",
        action="BUY",
        qty=5.0,
        price=540.0,
        reasoning="morning entry",
        run_id="morning-r1",
        broker_order_id="alpaca-amd-1",
        fill_status="submitted",
        stop_loss=500.0,
        take_profit=600.0,
    )

    pipeline = build_pipeline(db=db, broker=MagicMock(), risk_engine=MagicMock())
    # Item 127: the broker-writing preamble runs only under the desk's
    # advisory flock, which lives beside the database named in config.
    from types import SimpleNamespace

    pipeline.config = SimpleNamespace(storage=SimpleNamespace(db_path=db.db_path))
    pipeline._kill_switch_path = None
    pipeline._is_trading_day = MagicMock(return_value=True)
    pipeline._activate_cost_session = MagicMock()
    pipeline._drain_pending_protection_restores = MagicMock()
    pipeline._drain_pending_repegs = MagicMock()
    pipeline._reconcile_stop_coverage = MagicMock(return_value=[])
    pipeline._reconcile_orphan_pending_submits = MagicMock()
    pipeline._reconcile_stop_out_fills = MagicMock()
    pipeline._run_intraday_opportunity_scan = MagicMock(return_value={"status": "intraday_scan_disabled"})
    pipeline._sync_positions_from_broker = MagicMock()
    pipeline.broker.get_account.return_value = {
        "cash": 1000.0,
        "portfolio_value": 5000.0,
        "last_equity": 5000.0,
    }
    pipeline.broker.get_positions.return_value = []
    # Broker truth: AMD actually filled at $549.11 — the desk's own record
    # just hasn't been told yet.
    pipeline.broker.get_order_fill_info.return_value = {
        "status": "filled",
        "filled_qty": "5.0",
        "filled_avg_price": "549.11",
    }

    result = pipeline.run_intra_check()

    assert result["status"] == "ok"
    row = db.execute("SELECT fill_status, fill_price FROM trades WHERE broker_order_id = 'alpaca-amd-1'").fetchone()
    assert row["fill_status"] == "filled", (
        "an order that filled between sessions must be reconciled on the "
        "next half-hourly run, not left reading 'submitted'"
    )
    assert float(row["fill_price"]) == 549.11
    db.close()


def test_intra_check_reconciles_rejected_and_cancelled_orders(tmp_path):
    """Same gap as the AMD fill case, for the other two terminal outcomes
    the half-hourly reconcile must also resolve: a rejected order and a
    cancelled order must not be left reading 'submitted' either."""
    from src.storage.db import Database

    db = Database(str(tmp_path / "t.db"))
    db.initialize()
    db.insert_trade(
        symbol="TSLA",
        action="BUY",
        qty=3.0,
        price=250.0,
        reasoning="rejected entry",
        run_id="morning-r1",
        broker_order_id="alpaca-tsla-1",
        fill_status="submitted",
        stop_loss=90.0,
    )
    db.insert_trade(
        symbol="NVDA",
        action="REDUCE",
        qty=2.0,
        price=120.0,
        reasoning="cancelled reduce",
        run_id="morning-r1",
        broker_order_id="alpaca-nvda-1",
        fill_status="submitted",
    )

    pipeline = build_pipeline(db=db, broker=MagicMock(), risk_engine=MagicMock())
    # Item 127: the broker-writing preamble runs only under the desk's
    # advisory flock, which lives beside the database named in config.
    from types import SimpleNamespace

    pipeline.config = SimpleNamespace(storage=SimpleNamespace(db_path=db.db_path))
    pipeline._kill_switch_path = None
    pipeline._is_trading_day = MagicMock(return_value=True)
    pipeline._activate_cost_session = MagicMock()
    pipeline._drain_pending_protection_restores = MagicMock()
    pipeline._drain_pending_repegs = MagicMock()
    pipeline._reconcile_stop_coverage = MagicMock(return_value=[])
    pipeline._reconcile_orphan_pending_submits = MagicMock()
    pipeline._reconcile_stop_out_fills = MagicMock()
    pipeline._run_intraday_opportunity_scan = MagicMock(return_value={"status": "intraday_scan_disabled"})
    pipeline._sync_positions_from_broker = MagicMock()
    pipeline.broker.get_account.return_value = {
        "cash": 1000.0,
        "portfolio_value": 5000.0,
        "last_equity": 5000.0,
    }
    pipeline.broker.get_positions.return_value = []

    def _fill_info(order_id):
        if order_id == "alpaca-tsla-1":
            return {"status": "rejected", "filled_qty": None, "filled_avg_price": None}
        if order_id == "alpaca-nvda-1":
            return {"status": "canceled", "filled_qty": None, "filled_avg_price": None}
        return None

    pipeline.broker.get_order_fill_info.side_effect = _fill_info

    result = pipeline.run_intra_check()

    assert result["status"] == "ok"
    tsla = db.execute("SELECT fill_status FROM trades WHERE broker_order_id = 'alpaca-tsla-1'").fetchone()
    nvda = db.execute("SELECT fill_status FROM trades WHERE broker_order_id = 'alpaca-nvda-1'").fetchone()
    assert tsla["fill_status"] == "rejected"
    assert nvda["fill_status"] == "canceled"
    db.close()


# === Congressional-trading owner-facing wording must track the real switch ===
# `congress_enabled` (src/config.py) was switched on 2026-09-20 per owner
# ruling (docs/INCIDENT_HISTORY.md, that date). The pre-market smart-money
# refresh log line must say so honestly instead of always claiming both
# sources ran, whichever way the switch sits.


def test_smart_money_refresh_sources_word_when_congress_off():
    from src.pipeline import _smart_money_refresh_sources_word

    assert _smart_money_refresh_sources_word(False) == "SEC Form 4 only (congressional cross-check switched off)"


def test_smart_money_refresh_sources_word_when_congress_on():
    from src.pipeline import _smart_money_refresh_sources_word

    assert _smart_money_refresh_sources_word(True) == "SEC Form 4 + congressional"


def test_smart_money_refresh_sources_word_matches_repo_default_config():
    """Revert-and-fail: if the log line goes back to a hard-coded string
    regardless of the switch, this fails against the repo's own default
    (congressional cross-check on, since 2026-09-20's owner ruling)."""
    from src.config import SmartMoneyConfig
    from src.pipeline import _smart_money_refresh_sources_word

    default_congress_enabled = SmartMoneyConfig().congress_enabled
    assert default_congress_enabled is True
    word = _smart_money_refresh_sources_word(default_congress_enabled)
    assert word == "SEC Form 4 + congressional"
