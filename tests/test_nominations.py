"""Phase 9 — any research seat may nominate a candidate for Technical to
examine (`docs/QAMC_REMEDIATION_SPEC.md` §9.1/§9.2).

Covers:
  - src/models.py::Nomination + the nominations field on the three
    nominating seats' output models
  - src/nominations.py::select_nominations (per-seat cap, cross-seat
    dedupe, global cap, deterministic ranking)
  - src/pipeline.py::_evaluate_external_admission_gates (shared gate,
    used by both the pre-existing smart-money lane and the new
    nomination lane) + _admit_nominated_external_symbols
  - src/pipeline_stages.py::MorningResearchStage._run_nomination_responder_pass
    (the on-demand second Technical call)
"""

from datetime import date, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from src.agents.base import AgentResult
from src.models import (
    EarningsAnalysis,
    MacroAnalysis,
    MacroPositionGuidance,
    MacroReasoningChain,
    NewsIntelligenceReport,
    MacroNarrative,
    Nomination,
    OHLCV,
    TechAnalysisResult,
    TechReasoningChain,
)
from src.nominations import select_nominations
from src.pipeline_context import RunContext
from src.pipeline_stages import MorningResearchStage
from tests.pipeline_factory import build_pipeline


# ============================================================================
# Fixtures / builders
# ============================================================================


def _trc() -> TechReasoningChain:
    return TechReasoningChain(
        trend="x",
        momentum="x",
        volatility="x",
        volume="x",
        support_resistance="x",
    )


def _tech_result(symbol: str, rating: str = "buy") -> TechAnalysisResult:
    return TechAnalysisResult(
        symbol=symbol,
        rating=rating,
        entry_price=100,
        reference_target=120,
        stop_loss=90,
        support_levels=[90.0],
        resistance_levels=[120.0],
        setup_type="range",
        expected_horizon_sessions=10,
        reasoning="responder-covered candidate",
        reasoning_chain=_trc(),
        thesis_invalid_if="closes below support",
    )


def _bars(n: int = 30) -> list[OHLCV]:
    return [
        OHLCV(
            date=date.today() - timedelta(days=n - i),
            open=100,
            high=102,
            low=99,
            close=100,
            volume=200_000,
        )
        for i in range(n)
    ]


def _macro_analysis(nominations=None) -> MacroAnalysis:
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
        confidence="high",
        equity_outlook="bullish",
        position_guidance=MacroPositionGuidance(
            target_invested_pct=70,
            cash_recommendation_pct=30,
            reasoning="y",
        ),
        summary="z",
        nominations=nominations or [],
    )


def _news_report(nominations=None) -> NewsIntelligenceReport:
    return NewsIntelligenceReport(
        macro_narrative=MacroNarrative(
            last_updated=date.today().isoformat(),
            era_themes=["theme"],
            current_regime="steady",
        ),
        pm_briefing="brief",
        market_sentiment="bullish",
        confidence="medium",
        nominations=nominations or [],
    )


def _earnings_analysis(nominations=None) -> EarningsAnalysis:
    return EarningsAnalysis(
        symbol="AAPL",
        form_type="10-Q",
        filing_date="2026-03-15",
        revenue={"total": "$95.4B"},
        profitability={},
        cash_flow={},
        balance_sheet={},
        guidance="flat",
        data_quality="complete",
        investment_implications={
            "sentiment": "bullish",
            "conviction": "medium",
            "key_thesis": "services mix",
            "reasoning_chain": {
                "fundamental_quality": "strong",
                "growth_trajectory": "stable",
                "strategic_risks": "vision pro",
                "management_execution": "credible",
                "valuation_context": "conditional on services mix",
            },
        },
        nominations=nominations or [],
    )


def _build_stage(
    *,
    config,
    market=None,
    tech_analyst=None,
    news_intel_result=(None, None),
    macro_analysis_result=None,
    earnings_result=([], []),
    smart_money_provider=None,
    admit_smart_money_candidates_fn=None,
    admit_nominated_candidates_fn=None,
    has_actionable_signal_fn=None,
    db=None,
    tech_store=None,
):
    """Minimal MorningResearchStage builder shared across tests below."""
    market = market or MagicMock()
    tech_analyst = tech_analyst if tech_analyst is not None else MagicMock()
    db = db if db is not None else MagicMock()
    tech_store = tech_store if tech_store is not None else MagicMock()
    tech_store.load.return_value = {}
    tech_store.compute_ages.return_value = {}

    macro_agent = MagicMock()
    if macro_analysis_result is not None:
        macro_agent.analyze.return_value = macro_analysis_result
    else:
        macro_agent.analyze.return_value = (
            None,
            AgentResult(raw_text="{}", tokens_used=0, model="test", user_message="x"),
        )

    macro_store = MagicMock()
    macro_store.load_last_state.return_value = None
    news_store = MagicMock()
    news_store.load_macro_narrative.return_value = None

    has_actionable_signal_fn = has_actionable_signal_fn or (lambda *a, **kw: False)

    return MorningResearchStage(
        config=config,
        db=db,
        market=market,
        macro=MagicMock(get_macro_summary=MagicMock(return_value={})),
        news_provider=MagicMock(),
        news_store=news_store,
        macro_store=macro_store,
        tech_store=tech_store,
        earnings_provider=MagicMock(),
        macro_analyst=macro_agent,
        news_analyst=MagicMock(),
        tech_analyst=tech_analyst,
        earnings_analyst=MagicMock(),
        smart_money_provider=smart_money_provider,
        smart_money_analyst=None,
        admit_smart_money_candidates_fn=admit_smart_money_candidates_fn,
        admit_nominated_candidates_fn=admit_nominated_candidates_fn,
        has_actionable_signal_fn=has_actionable_signal_fn,
        run_news_update_fn=lambda *a, **kw: news_intel_result,
        load_earnings_analyses_fn=lambda *a, **kw: earnings_result,
    )


def _simple_config(universe, max_per_seat=3):
    return SimpleNamespace(
        trading=SimpleNamespace(universe=universe, lookback_days=30),
        smart_money=SimpleNamespace(enabled=False),
        nominations=SimpleNamespace(
            max_per_seat_per_run=max_per_seat,
        ),
    )


# ============================================================================
# D1 — Nomination model
# ============================================================================


def test_nomination_requires_non_empty_observation():
    with pytest.raises(Exception):
        Nomination(symbol="VST", conviction="high", observation="   ")


def test_nomination_normalizes_symbol_and_conviction_case():
    n = Nomination(symbol="vst", conviction="HIGH", observation="cluster insider buying")
    assert n.symbol == "VST"
    assert n.conviction == "high"


def test_report_level_nomination_field_defaults_to_empty_list():
    """Old stored decisions (no `nominations` key) must replay unchanged."""
    assert _macro_analysis().nominations == []
    assert _news_report().nominations == []
    assert _earnings_analysis().nominations == []


def test_report_drops_malformed_nomination_without_failing_whole_report():
    """A single bad nomination (empty observation) must not cost the seat
    its entire structured output for the run."""
    report = NewsIntelligenceReport(
        macro_narrative=MacroNarrative(
            last_updated=date.today().isoformat(),
            era_themes=["t"],
            current_regime="steady",
        ),
        pm_briefing="brief",
        market_sentiment="bullish",
        confidence="medium",
        nominations=[
            {"symbol": "abc", "conviction": "high", "observation": ""},
            {"symbol": "xyz", "conviction": "medium", "observation": "genuine catalyst"},
        ],
    )
    assert [n.symbol for n in report.nominations] == ["XYZ"]


# ============================================================================
# D3 — cap / dedupe / ranking (src/nominations.py, pure logic)
# ============================================================================


def _n(symbol, conviction, seat="news_analyst", observation="obs"):
    return Nomination(symbol=symbol, conviction=conviction, observation=observation)


def test_per_seat_cap_keeps_highest_conviction_then_alphabetical():
    noms = [
        _n("EEE", "low"),
        _n("AAA", "high"),
        _n("CCC", "medium"),
        _n("BBB", "high"),
        _n("DDD", "medium"),
    ]
    candidates = select_nominations(
        {"news_analyst": noms},
        max_per_seat=3,
    )
    # cap=3 keeps: AAA(high), BBB(high), then highest of the mediums (CCC
    # alphabetically before DDD) — EEE(low) and DDD(medium) are dropped.
    assert [c.symbol for c in candidates] == ["AAA", "BBB", "CCC"]


def test_cross_seat_dedupe_records_both_nominators_as_one_candidate():
    by_seat = {
        "news_analyst": [_n("VST", "medium", observation="earnings beat")],
        "macro_analyst": [_n("VST", "high", observation="sector leader")],
    }
    candidates = select_nominations(by_seat, max_per_seat=3)
    assert len(candidates) == 1
    c = candidates[0]
    assert c.symbol == "VST"
    # Merged conviction is the HIGHEST of the two seats' calls.
    assert c.conviction == "high"
    assert set(c.seats) == {"news_analyst", "macro_analyst"}
    assert c.observations["news_analyst"] == "earnings beat"
    assert c.observations["macro_analyst"] == "sector leader"


def test_ranking_orders_by_conviction_then_seat_count_then_symbol():
    by_seat = {
        "news_analyst": [
            _n("AAA", "high"),
            _n("BBB", "high"),
            _n("CCC", "medium"),
            _n("DDD", "medium"),
        ],
        "macro_analyst": [_n("BBB", "high"), _n("CCC", "medium")],
        "earnings_analyst": [_n("EEE", "low")],
    }
    candidates = select_nominations(by_seat, max_per_seat=4)
    assert len(candidates) == 5
    # BBB: high conviction + 2 seats -> ranked first.
    # AAA: high conviction + 1 seat -> second.
    # CCC: medium conviction + 2 seats -> third (beats DDD: medium/1 seat).
    assert [c.symbol for c in candidates] == ["BBB", "AAA", "CCC", "DDD", "EEE"]


def test_ranking_is_deterministic_regardless_of_input_order():
    by_seat_a = {
        "news_analyst": [_n("AAA", "high"), _n("BBB", "medium")],
        "macro_analyst": [_n("BBB", "medium"), _n("CCC", "low")],
    }
    by_seat_b = {
        "macro_analyst": [_n("CCC", "low"), _n("BBB", "medium")],
        "news_analyst": [_n("BBB", "medium"), _n("AAA", "high")],
    }
    result_a = select_nominations(by_seat_a, max_per_seat=3)
    result_b = select_nominations(by_seat_b, max_per_seat=3)
    assert [(c.symbol, c.conviction, sorted(c.seats)) for c in result_a] == [
        (c.symbol, c.conviction, sorted(c.seats)) for c in result_b
    ]


def test_empty_seats_produce_no_candidates():
    assert (
        select_nominations(
            {"news_analyst": [], "macro_analyst": [], "earnings_analyst": []},
            max_per_seat=3,
        )
        == []
    )


# ============================================================================
# D3 — shared external-admission gate (src/pipeline.py)
# ============================================================================


def _gate_pipeline(
    monkeypatch,
    *,
    sector="Utilities",
    broker_eligible=True,
    bars=None,
    min_history=20,
    min_price=5.0,
    min_dv=10_000_000,
):
    pipeline = build_pipeline(broker=MagicMock(), market=MagicMock())
    pipeline.config = SimpleNamespace(
        trading=SimpleNamespace(universe=["SPY"], lookback_days=120),
        smart_money=SimpleNamespace(
            max_external_candidates=3,
            min_external_history_days=min_history,
            min_external_price_usd=min_price,
            min_external_avg_dollar_volume_usd=min_dv,
        ),
    )
    pipeline.broker.get_transient_equity_eligibility.return_value = (
        {"eligible": True, "reason": "eligible", "name": "Vistra Corp", "exchange": "nyse"}
        if broker_eligible
        else {"eligible": False, "reason": "not_shortable_or_tradable"}
    )
    pipeline.market.get_ohlcv.return_value = bars if bars is not None else _bars(30)
    monkeypatch.setattr("src.pipeline_admission._get_sector", lambda _symbol: sector)
    return pipeline


def test_gate_rejects_broker_ineligible(monkeypatch):
    pipeline = _gate_pipeline(monkeypatch, broker_eligible=False)
    eligible, reason, details = pipeline.admission._evaluate_external_admission_gates("VST")
    assert eligible is False
    assert reason == "not_shortable_or_tradable"
    assert details == {}


def test_gate_rejects_insufficient_history(monkeypatch):
    pipeline = _gate_pipeline(monkeypatch, bars=_bars(10))
    eligible, reason, details = pipeline.admission._evaluate_external_admission_gates("VST")
    assert eligible is False
    assert reason == "insufficient_history"


def test_gate_rejects_price_below_minimum(monkeypatch):
    bars = [
        OHLCV(date=date.today() - timedelta(days=30 - i), open=2, high=2.1, low=1.9, close=2.0, volume=5_000_000)
        for i in range(30)
    ]
    pipeline = _gate_pipeline(monkeypatch, bars=bars, min_price=5.0)
    eligible, reason, details = pipeline.admission._evaluate_external_admission_gates("VST")
    assert eligible is False
    assert reason == "price_below_minimum"


def test_gate_rejects_dollar_volume_below_minimum(monkeypatch):
    bars = [
        OHLCV(date=date.today() - timedelta(days=30 - i), open=10, high=10.1, low=9.9, close=10.0, volume=1_000)
        for i in range(30)
    ]
    pipeline = _gate_pipeline(monkeypatch, bars=bars, min_dv=10_000_000)
    eligible, reason, details = pipeline.admission._evaluate_external_admission_gates("VST")
    assert eligible is False
    assert reason == "dollar_volume_below_minimum"


def test_gate_rejects_unresolved_sector(monkeypatch):
    pipeline = _gate_pipeline(monkeypatch, sector="Unknown")
    eligible, reason, details = pipeline.admission._evaluate_external_admission_gates("VST")
    assert eligible is False
    assert reason == "unresolved_sector"


def test_gate_admits_when_every_check_passes(monkeypatch):
    pipeline = _gate_pipeline(monkeypatch)
    eligible, reason, details = pipeline.admission._evaluate_external_admission_gates("VST")
    assert eligible is True
    assert reason is None
    assert details["sector"] == "Utilities"
    assert details["broker"]["eligible"] is True


def test_admit_nominated_external_symbols_uses_shared_gate(monkeypatch):
    pipeline = _gate_pipeline(monkeypatch)
    admitted, details = pipeline.admission._admit_nominated_external_symbols(["vst"])
    assert admitted == {"VST"}
    assert details["VST"]["reason"] == "nomination_external_admission"
    assert details["VST"]["sector"] == "Utilities"
    assert details["VST"]["temporary"] is True


# ============================================================================
# Smart-money lane unchanged after the gate refactor (explicit before/after)
# ============================================================================


def test_smart_money_admission_lane_behaves_identically_after_refactor(monkeypatch):
    """Same fixture shape as the pre-existing
    tests/test_bugfixes.py::test_transient_admission_requires_sec_purchase_broker_and_market_quality
    — reproduced here so Phase 9's refactor of the shared gate is proven,
    in this file, not to have changed smart-money's observable behavior."""
    pipeline = build_pipeline(broker=MagicMock(), market=MagicMock())
    pipeline.config = SimpleNamespace(
        trading=SimpleNamespace(universe=["SPY"], lookback_days=120),
        smart_money=SimpleNamespace(
            max_external_candidates=3,
            min_external_history_days=20,
            min_external_price_usd=5.0,
            min_external_avg_dollar_volume_usd=10_000_000,
        ),
    )
    pipeline.broker.get_transient_equity_eligibility.return_value = {
        "eligible": True,
        "reason": "eligible",
        "name": "Vistra Corp",
        "exchange": "nyse",
    }
    monkeypatch.setattr("src.pipeline_admission._get_sector", lambda _symbol: "Utilities")
    pipeline.market.get_ohlcv.return_value = _bars(30)
    observations = [
        SimpleNamespace(
            symbol="VST",
            transaction_code="P",
            admission_eligible=True,
            transaction_value_usd=500_000,
            accession_number="0001-26-000001",
            actor="Example Director",
            known_at="2026-08-25T12:00:00Z",
        )
    ]

    admitted, details = pipeline.admission._admit_transient_smart_money_symbols(observations)
    assert admitted == {"VST"}
    assert details["VST"]["temporary"] is True
    assert details["VST"]["reason"] == "material_sec_form4_purchase"
    assert details["VST"]["transaction_value_usd"] == 500_000
    assert details["VST"]["sector"] == "Utilities"
    assert pipeline.config.trading.universe == ["SPY"]

    # Non-purchase code still rejected.
    observations[0].transaction_code = "S"
    assert pipeline.admission._admit_transient_smart_money_symbols(observations)[0] == set()
    # Insufficient history still rejected.
    observations[0].transaction_code = "P"
    pipeline.market.get_ohlcv.return_value = _bars(10)
    assert pipeline.admission._admit_transient_smart_money_symbols(observations)[0] == set()


# ============================================================================
# D2/D4 — the responder pass itself
# ============================================================================


def test_news_nomination_for_unanalyzed_symbol_triggers_responder_call_and_reaches_pm():
    """A News nomination for a symbol Technical didn't analyse in the first
    batch results in a second Technical call covering EXACTLY that symbol,
    and the symbol reaches ctx.analyses — the exact list DecisionStage
    forwards to PortfolioManagerAgent.decide(analyses=...)."""
    config = _simple_config(universe=["SPY", "XYZ"])
    market = MagicMock()
    market.get_ohlcv.return_value = _bars(30)
    market.get_valuation_metrics.return_value = {}

    tech_analyst = MagicMock()
    xyz_result = _tech_result("XYZ")
    tech_analyst.analyze_batch.return_value = (
        {"XYZ": xyz_result},
        AgentResult(raw_text="{}", tokens_used=10, model="test", user_message="x", cost_usd=0.01),
    )

    news = _news_report(
        nominations=[
            Nomination(symbol="XYZ", conviction="high", observation="genuine catalyst: $2B contract"),
        ]
    )

    with patch("src.stage_morning_research.compute_indicators", return_value=MagicMock()):
        stage = _build_stage(
            config=config,
            market=market,
            tech_analyst=tech_analyst,
            news_intel_result=(news, None),
            has_actionable_signal_fn=lambda *a, **kw: False,  # nothing prefilters
        )
        ctx = RunContext.start("morning")
        ctx.positions = []
        result_ctx = stage.run(ctx)

    # Exactly one Technical call total (primary batch never ran — no
    # actionable signal anywhere — so this IS the responder call).
    assert tech_analyst.analyze_batch.call_count == 1
    called_symbols = [s["symbol"] for s in tech_analyst.analyze_batch.call_args.args[0]]
    assert called_symbols == ["XYZ"]

    assert any(a.symbol == "XYZ" for a in result_ctx.analyses)
    assert result_ctx.analyses == [xyz_result]


def test_no_nominations_means_no_second_technical_call():
    """D4: if there are no nominations, no second call may be made at all."""
    config = _simple_config(universe=["SPY"])
    market = MagicMock()
    market.get_ohlcv.return_value = _bars(30)
    market.get_valuation_metrics.return_value = {}

    tech_analyst = MagicMock()
    spy_result = _tech_result("SPY")
    tech_analyst.analyze_batch.return_value = (
        {"SPY": spy_result},
        AgentResult(raw_text="{}", tokens_used=10, model="test", user_message="x", cost_usd=0.01),
    )

    with patch("src.stage_morning_research.compute_indicators", return_value=MagicMock()):
        stage = _build_stage(
            config=config,
            market=market,
            tech_analyst=tech_analyst,
            news_intel_result=(_news_report(), None),
            macro_analysis_result=(
                _macro_analysis(),
                AgentResult(raw_text="{}", tokens_used=0, model="test", user_message="x"),
            ),
            earnings_result=([], []),
            has_actionable_signal_fn=lambda *a, **kw: True,  # primary batch runs
        )
        ctx = RunContext.start("morning")
        ctx.positions = []
        result_ctx = stage.run(ctx)

    # Exactly the ONE primary call — the responder pass made none.
    assert tech_analyst.analyze_batch.call_count == 1
    assert result_ctx.analyses == [spy_result]


def test_zero_nominations_full_run_produces_byte_identical_pm_inputs():
    """No-op proof: with zero nominations, ctx.analyses (PM's `analyses`
    input) and ctx.admitted_symbols (feeds PM's `allowed_buy_symbols`) are
    EXACTLY what the pre-Phase-9 pipeline would have produced — the primary
    batch's own return value, untouched."""
    config = _simple_config(universe=["SPY"])
    market = MagicMock()
    market.get_ohlcv.return_value = _bars(30)
    market.get_valuation_metrics.return_value = {}

    tech_analyst = MagicMock()
    spy_result = _tech_result("SPY")
    primary_map = {"SPY": spy_result}
    tech_analyst.analyze_batch.return_value = (
        dict(primary_map),
        AgentResult(raw_text="{}", tokens_used=10, model="test", user_message="x", cost_usd=0.02),
    )

    with patch("src.stage_morning_research.compute_indicators", return_value=MagicMock()):
        stage = _build_stage(
            config=config,
            market=market,
            tech_analyst=tech_analyst,
            news_intel_result=(_news_report(), None),
            macro_analysis_result=(
                _macro_analysis(),
                AgentResult(raw_text="{}", tokens_used=0, model="test", user_message="x"),
            ),
            earnings_result=([], []),
            has_actionable_signal_fn=lambda *a, **kw: True,
        )
        ctx = RunContext.start("morning")
        ctx.positions = []
        result_ctx = stage.run(ctx)

    assert tech_analyst.analyze_batch.call_count == 1
    assert result_ctx.analyses == list(primary_map.values())
    assert result_ctx.admitted_symbols == set()


# ============================================================================
# D3 — nomination for an out-of-universe symbol goes through the gate
# ============================================================================


def test_nominated_out_of_universe_symbol_is_gated_before_responder_call(monkeypatch):
    """An out-of-universe nomination must clear the same deterministic
    gate the smart-money lane uses before it can reach the responder call
    or ctx.admitted_symbols."""
    config = _simple_config(universe=["SPY"])
    market = MagicMock()
    market.get_ohlcv.return_value = _bars(30)
    market.get_valuation_metrics.return_value = {}

    tech_analyst = MagicMock()
    vst_result = _tech_result("VST")
    tech_analyst.analyze_batch.return_value = (
        {"VST": vst_result},
        AgentResult(raw_text="{}", tokens_used=10, model="test", user_message="x", cost_usd=0.01),
    )

    def _admit(symbols):
        assert symbols == ["VST"]
        return {"VST"}, {"VST": {"temporary": True, "reason": "nomination_external_admission", "sector": "Utilities"}}

    news = _news_report(
        nominations=[
            Nomination(symbol="VST", conviction="high", observation="cluster insider buying + earnings beat"),
        ]
    )

    with patch("src.stage_morning_research.compute_indicators", return_value=MagicMock()):
        stage = _build_stage(
            config=config,
            market=market,
            tech_analyst=tech_analyst,
            news_intel_result=(news, None),
            admit_nominated_candidates_fn=_admit,
            has_actionable_signal_fn=lambda *a, **kw: False,
        )
        ctx = RunContext.start("morning")
        ctx.positions = []
        result_ctx = stage.run(ctx)

    assert result_ctx.admitted_symbols == {"VST"}
    assert any(a.symbol == "VST" for a in result_ctx.analyses)


def test_nominated_out_of_universe_symbol_rejected_by_gate_never_reaches_responder():
    config = _simple_config(universe=["SPY"])
    market = MagicMock()
    market.get_ohlcv.return_value = _bars(30)

    tech_analyst = MagicMock()

    def _admit(symbols):
        return set(), {}  # every gate fails

    news = _news_report(
        nominations=[
            Nomination(symbol="JUNK", conviction="high", observation="thin, illiquid microcap chatter"),
        ]
    )

    with patch("src.stage_morning_research.compute_indicators", return_value=MagicMock()):
        stage = _build_stage(
            config=config,
            market=market,
            tech_analyst=tech_analyst,
            news_intel_result=(news, None),
            admit_nominated_candidates_fn=_admit,
            has_actionable_signal_fn=lambda *a, **kw: False,
        )
        ctx = RunContext.start("morning")
        ctx.positions = []
        result_ctx = stage.run(ctx)

    tech_analyst.analyze_batch.assert_not_called()
    assert result_ctx.admitted_symbols == set()
    assert result_ctx.analyses == []


def test_earnings_nomination_aggregated_across_seat_and_reaches_responder():
    """Earnings attaches nominations to EarningsAnalysis (per-filing, the
    actual LLM-validated output object — no per-run earnings container
    model exists in this codebase). A session with one new filing analyzed
    still surfaces that filing's nomination to the responder pass."""
    config = _simple_config(universe=["SPY", "AAPL"])
    market = MagicMock()
    market.get_ohlcv.return_value = _bars(30)
    market.get_valuation_metrics.return_value = {}

    tech_analyst = MagicMock()
    aapl_result = _tech_result("AAPL")
    tech_analyst.analyze_batch.return_value = (
        {"AAPL": aapl_result},
        AgentResult(raw_text="{}", tokens_used=10, model="test", user_message="x", cost_usd=0.01),
    )

    earnings_analysis = _earnings_analysis(
        nominations=[
            Nomination(symbol="AAPL", conviction="high", observation="blowout beat, Services +14%"),
        ]
    )
    earnings_results = ([], [{"symbol": "AAPL", "analysis": earnings_analysis.model_dump()}])

    with patch("src.stage_morning_research.compute_indicators", return_value=MagicMock()):
        stage = _build_stage(
            config=config,
            market=market,
            tech_analyst=tech_analyst,
            earnings_result=earnings_results,
            has_actionable_signal_fn=lambda *a, **kw: False,
        )
        ctx = RunContext.start("morning")
        ctx.positions = []
        result_ctx = stage.run(ctx)

    assert tech_analyst.analyze_batch.call_count == 1
    called_symbols = [s["symbol"] for s in tech_analyst.analyze_batch.call_args.args[0]]
    assert called_symbols == ["AAPL"]
    assert any(a.symbol == "AAPL" for a in result_ctx.analyses)


def test_symbol_already_analyzed_by_primary_batch_gets_no_responder_call():
    """A nomination for a symbol the FIRST batch already covered needs no
    second call — it's already in ctx.analyses."""
    config = _simple_config(universe=["SPY"])
    market = MagicMock()
    market.get_ohlcv.return_value = _bars(30)
    market.get_valuation_metrics.return_value = {}

    tech_analyst = MagicMock()
    spy_result = _tech_result("SPY")
    tech_analyst.analyze_batch.return_value = (
        {"SPY": spy_result},
        AgentResult(raw_text="{}", tokens_used=10, model="test", user_message="x", cost_usd=0.01),
    )

    news = _news_report(
        nominations=[
            Nomination(symbol="SPY", conviction="medium", observation="index-wide catalyst"),
        ]
    )

    with patch("src.stage_morning_research.compute_indicators", return_value=MagicMock()):
        stage = _build_stage(
            config=config,
            market=market,
            tech_analyst=tech_analyst,
            news_intel_result=(news, None),
            has_actionable_signal_fn=lambda *a, **kw: True,
        )
        ctx = RunContext.start("morning")
        ctx.positions = []
        result_ctx = stage.run(ctx)

    # Only the primary call — SPY was already analyzed, no responder call.
    assert tech_analyst.analyze_batch.call_count == 1
    assert result_ctx.analyses == [spy_result]
