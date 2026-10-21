"""Tests for bugfixes identified in code review."""

from src.pipeline_risk_gate import RiskGate
import json
from datetime import date, datetime
from unittest.mock import patch, MagicMock

import pytest
from pydantic import ValidationError

from src.agents.base import AgentResult
from src.cost_circuit import PaidAnalysisSuspended
from src.pipeline import TradingPipeline
from src.risk.rules import RiskRuleEngine, RiskViolation
from src.config import RiskConfig
from src.models import (
    RiskModification,
    TechAnalysisResult,
    TradeDecision,
    Position,
    MacroAnalysis,
    MacroReasoningChain,
    MacroPositionGuidance,
    MacroSectorGuidance,
    TechReasoningChain,
    RiskVerdict,
    RiskReasoningChain,
    PositionReview,
    PositionAction,
    EveningReport,
    EveningReasoningChain,
)
from tests.pipeline_factory import build_pipeline


def _valid_evening_rc() -> EveningReasoningChain:
    """Test helper — minimal valid 7-step evening reasoning chain."""
    return EveningReasoningChain(
        performance_attribution="flat day, no major attribution",
        outlook_retrospection="n/a (no prior)",
        thesis_health_review="no theses to review (empty book)",
        decision_quality_review="no trades today",
        calibration_meta="insufficient history",
        market_regime_read="regime stable",
        tomorrow_preparation="no key events",
    )


def _trc() -> TechReasoningChain:
    """Minimal valid 5-step TA CoT — every field non-empty."""
    return TechReasoningChain(
        trend="x",
        momentum="x",
        volatility="x",
        volume="x",
        support_resistance="x",
    )


def _risk_rc() -> RiskReasoningChain:
    """Minimal valid 6-step RM CoT — every field non-empty."""
    return RiskReasoningChain(
        rr_audit="x",
        signal_fidelity="x",
        correlation_check="x",
        event_risk="x",
        sizing_sanity="x",
        overall="x",
    )


# === Fix 1: Hard risk rules actually block trades ===


@pytest.fixture
def risk_engine():
    return RiskRuleEngine(
        RiskConfig(
            max_position_pct=20,
            max_total_position_pct=90,
            require_stop_loss=True,
        )
    )


def test_total_exposure_violation(risk_engine):
    positions = [
        Position(
            symbol="AAPL",
            qty=100,
            avg_entry=180,
            current_price=190,
            market_value=19000,
            unrealized_pnl=1000,
            sector="Tech",
        ),
    ] * 5  # 5 positions at $19K each = $95K = 95% of $100K
    decision = TradeDecision(
        action="BUY",
        symbol="NVDA",
        allocation_pct=10,
        entry_price=800,
        stop_loss=750,
        take_profit=900,
        reasoning="test",
    )
    violations = risk_engine.check(
        decision=decision,
        positions=positions,
        total_value=100000,
    )
    rules = [v.rule for v in violations]
    assert "max_total_position_pct" in rules


def test_position_size_violation(risk_engine):
    decision = TradeDecision(
        action="BUY",
        symbol="SPY",
        allocation_pct=25,  # exceeds 20% limit
        entry_price=500,
        stop_loss=480,
        take_profit=530,
        reasoning="test",
    )
    violations = risk_engine.check(
        decision=decision,
        positions=[],
        total_value=100000,
    )
    rules = [v.rule for v in violations]
    assert "max_position_pct" in rules


def test_stop_loss_required_violation(risk_engine):
    decision = TradeDecision(
        action="BUY",
        symbol="SPY",
        allocation_pct=10,
        entry_price=500,
        stop_loss=0,
        take_profit=530,
        reasoning="test",
    )
    violations = risk_engine.check(
        decision=decision,
        positions=[],
        total_value=100000,
    )
    rules = [v.rule for v in violations]
    assert "require_stop_loss" in rules


def test_sell_orders_skip_risk_check(risk_engine):
    decision = TradeDecision(
        action="SELL",
        symbol="SPY",
        allocation_pct=0,
        entry_price=0,
        stop_loss=0,
        take_profit=0,
        reasoning="close",
    )
    violations = risk_engine.check(
        decision=decision,
        positions=[],
        total_value=100000,
    )
    assert violations == []


# === Fix 7: JSON parsing robustness ===


def test_parse_json_direct():
    result = AgentResult(raw_text='{"key": "value"}', tokens_used=10, model="test")
    assert result.parse_json() == {"key": "value"}


def test_parse_json_array():
    result = AgentResult(raw_text='[{"a": 1}, {"b": 2}]', tokens_used=10, model="test")
    parsed = result.parse_json()
    assert isinstance(parsed, list)
    assert len(parsed) == 2


def test_parse_json_garbage_returns_none():
    result = AgentResult(raw_text="This is not JSON at all.", tokens_used=10, model="test")
    assert result.parse_json() is None


# === Fix 6: Division by zero safety ===


def test_midday_pnl_pct_zero_qty():
    """Midday reviewer should not crash on zero qty position."""
    from src.agents.position_reviewer import PositionReviewerAgent

    with patch("anthropic.Anthropic"):
        agent = PositionReviewerAgent(api_key="test", model="claude-sonnet-4-6")
        # Should not raise ZeroDivisionError
        msg = agent.build_user_message(
            positions=[
                Position(
                    symbol="TEST",
                    qty=0,
                    avg_entry=0,
                    current_price=100,
                    market_value=0,
                    unrealized_pnl=0,
                    sector="Tech",
                )
            ],
            macro_summary={"vix": {"current": 20, "trend": "flat"}},
            cash_balance=10000,
            total_value=10000,
        )
        assert "TEST" in msg


def test_midday_reviewer_ignores_unfilled_buys_in_trade_context():
    from src.agents.position_reviewer import PositionReviewerAgent

    with patch("anthropic.Anthropic"):
        agent = PositionReviewerAgent(api_key="test", model="claude-sonnet-4-6")
        msg = agent.build_user_message(
            positions=[
                Position(
                    symbol="SPY",
                    qty=10,
                    avg_entry=500,
                    current_price=505,
                    market_value=5050,
                    unrealized_pnl=50,
                    sector="ETF",
                )
            ],
            macro_summary={"vix": {"current": 20, "trend": "flat"}},
            cash_balance=1000,
            total_value=6000,
            morning_trades=[
                {
                    "symbol": "SPY",
                    "action": "BUY",
                    "reasoning": "stale add-on that never filled",
                    "fill_status": "canceled",
                    "stop_loss": 490.0,
                    "take_profit": 520.0,
                },
                {
                    "symbol": "SPY",
                    "action": "BUY",
                    "reasoning": "core entry thesis that actually filled",
                    "fill_status": "filled",
                    "stop_loss": 480.0,
                    "take_profit": 530.0,
                },
            ],
        )

        assert "core entry thesis that actually filled" in msg
        assert "stale add-on that never filled" not in msg


def test_pm_invested_pct_zero_total():
    """Portfolio manager should not crash when total_value is 0."""
    from src.agents.portfolio_manager import PortfolioManagerAgent

    with patch("anthropic.Anthropic"):
        agent = PortfolioManagerAgent(api_key="test", model="claude-sonnet-4-6")
        msg = agent.build_user_message(
            analyses=[],
            positions=[],
            macro_analysis=None,
            cash_balance=0,
            total_value=0,
        )
        assert "0.0%" in msg


def test_trade_decision_validates_assignment():
    decision = TradeDecision(
        action="BUY",
        symbol="SPY",
        allocation_pct=10,
        entry_price=500,
        stop_loss=480,
        take_profit=530,
        reasoning="test",
    )

    with pytest.raises(ValidationError):
        decision.allocation_pct = 150


def test_pipeline_hard_risk_filter_blocks_missing_stop_loss():
    pipeline = build_pipeline(
        risk_engine=RiskRuleEngine(
            RiskConfig(
                max_position_pct=20,
                max_total_position_pct=90,
                require_stop_loss=True,
            )
        )
    )

    decisions = [
        TradeDecision(
            action="BUY",
            symbol="SPY",
            allocation_pct=10,
            entry_price=500,
            stop_loss=0,
            take_profit=530,
            reasoning="test",
        )
    ]

    allowed, violations, blocked = pipeline.risk_gate._filter_hard_risk_decisions(
        decisions,
        positions=[],
        total_value=100000,
    )

    assert allowed == []
    assert violations == []
    assert any("no stop loss" in reason for reason in blocked)


def test_pipeline_hard_risk_filter_blocks_second_same_symbol_buy():
    pipeline = build_pipeline(
        risk_engine=RiskRuleEngine(
            RiskConfig(
                max_position_pct=20,
                max_total_position_pct=90,
                require_stop_loss=True,
            )
        )
    )
    decisions = [
        TradeDecision(
            action="BUY",
            symbol="SPY",
            allocation_pct=15,
            entry_price=500,
            stop_loss=480,
            take_profit=530,
            reasoning="first leg",
        ),
        TradeDecision(
            action="BUY",
            symbol="SPY",
            allocation_pct=15,
            entry_price=500,
            stop_loss=480,
            take_profit=530,
            reasoning="duplicate leg",
        ),
    ]

    with (
        patch("src.pipeline_admission._get_sector", return_value="ETF"),
        patch("src.execution.broker._get_sector", return_value="ETF"),
    ):
        allowed, violations, blocked = pipeline.risk_gate._filter_hard_risk_decisions(
            decisions,
            positions=[],
            total_value=100000,
        )

    assert [d.reasoning for d in allowed] == ["first leg"]
    assert violations == []
    assert any("SPY would go over the" in reason for reason in blocked)


def test_pipeline_symbol_guard_blocks_off_universe_and_unanalyzed_buys():
    pipeline = build_pipeline()
    pipeline.config = MagicMock()
    pipeline.config.trading.universe = ["SPY", "QQQ"]

    decisions = [
        TradeDecision(
            action="BUY",
            symbol="TSLA",
            allocation_pct=10,
            entry_price=250,
            stop_loss=230,
            take_profit=280,
            reasoning="hallucinated",
        ),
        TradeDecision(
            action="BUY",
            symbol="QQQ",
            allocation_pct=10,
            entry_price=500,
            stop_loss=480,
            take_profit=530,
            reasoning="not analyzed",
        ),
        TradeDecision(
            action="BUY",
            symbol="SPY",
            allocation_pct=10,
            entry_price=500,
            stop_loss=480,
            take_profit=530,
            reasoning="supported",
        ),
    ]
    analyses = [
        TechAnalysisResult(
            symbol="SPY",
            rating="buy",
            entry_price=500,
            reference_target=530,
            stop_loss=480,
            support_levels=[480.0, 470.0],
            resistance_levels=[530.0],
            setup_type="range",
            expected_horizon_sessions=10,
            reasoning="supported",
            reasoning_chain=_trc(),
            thesis_invalid_if="closes below support",
        )
    ]

    allowed, blocked = pipeline.admission._filter_supported_symbols(decisions, analyses, positions=[])

    assert [d.symbol for d in allowed] == ["SPY"]
    assert any("TSLA is neither in the configured universe" in reason for reason in blocked)
    assert any("QQQ has no supporting analyst output" in reason for reason in blocked)


def test_pipeline_symbol_guard_allows_only_run_admitted_analyzed_buy():
    pipeline = build_pipeline()
    pipeline.config = MagicMock()
    pipeline.config.trading.universe = ["SPY"]
    analysis = TechAnalysisResult(
        symbol="VST",
        rating="buy",
        entry_price=150,
        reference_target=180,
        stop_loss=140,
        support_levels=[140.0, 135.0],
        resistance_levels=[180.0],
        setup_type="range",
        expected_horizon_sessions=10,
        reasoning="validated transient candidate",
        reasoning_chain=_trc(),
        thesis_invalid_if="closes below support",
    )
    decision = TradeDecision(
        action="BUY",
        symbol="VST",
        allocation_pct=5,
        entry_price=150,
        stop_loss=140,
        take_profit=180,
        reasoning="SEC-admitted and technically evaluated",
    )

    allowed, blocked = pipeline.admission._filter_supported_symbols(
        [decision],
        [analysis],
        positions=[],
        admitted_symbols={"VST"},
    )
    assert allowed == [decision]
    assert blocked == []
    assert pipeline.config.trading.universe == ["SPY"]


def test_transient_admission_rejects_unresolved_sector(monkeypatch):
    from datetime import date, timedelta
    from types import SimpleNamespace
    from src.models import OHLCV

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
    }
    pipeline.market.get_ohlcv.return_value = [
        OHLCV(
            date=date.today() - timedelta(days=30 - i),
            open=100,
            high=102,
            low=99,
            close=100,
            volume=200_000,
        )
        for i in range(30)
    ]
    monkeypatch.setattr("src.pipeline_admission._get_sector", lambda _symbol: "Unknown")
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

    assert pipeline.admission._admit_transient_smart_money_symbols(observations)[0] == set()


def test_morning_prefilter_requires_real_macd_histogram_crossover(monkeypatch):
    """A merely small MACD histogram is ordinary, not an opportunity."""
    from src.models import TechnicalIndicators

    bars = [MagicMock(close=100.0) for _ in range(30)]
    indicators = TechnicalIndicators(
        symbol="SPY",
        rsi_14=50.0,
        macd_hist=0.1,
        atr_14=10.0,
        volume_change_pct=0.0,
    )
    monkeypatch.setattr(
        "src.pipeline_risk_gate.compute_indicators",
        lambda _symbol, _bars: TechnicalIndicators(
            symbol="SPY",
            macd_hist=0.2,
        ),
    )

    assert (
        RiskGate._has_actionable_signal_fn(
            indicators,
            "SPY",
            bars,
            [],
        )
        is False
    )


def test_morning_prefilter_accepts_macd_histogram_sign_change(monkeypatch):
    from src.models import TechnicalIndicators

    bars = [MagicMock(close=100.0) for _ in range(30)]
    indicators = TechnicalIndicators(
        symbol="SPY",
        rsi_14=50.0,
        macd_hist=0.1,
        atr_14=10.0,
        volume_change_pct=0.0,
    )
    monkeypatch.setattr(
        "src.pipeline_risk_gate.compute_indicators",
        lambda _symbol, _bars: TechnicalIndicators(
            symbol="SPY",
            macd_hist=-0.2,
        ),
    )

    assert (
        RiskGate._has_actionable_signal_fn(
            indicators,
            "SPY",
            bars,
            [],
        )
        is True
    )


def test_morning_prefilter_rejects_unchanged_zero_macd_histogram(monkeypatch):
    from src.models import TechnicalIndicators

    bars = [MagicMock(close=100.0) for _ in range(30)]
    indicators = TechnicalIndicators(
        symbol="SPY",
        rsi_14=50.0,
        macd_hist=0.0,
        atr_14=10.0,
        volume_change_pct=0.0,
    )
    monkeypatch.setattr(
        "src.pipeline_risk_gate.compute_indicators",
        lambda _symbol, _bars: TechnicalIndicators(
            symbol="SPY",
            macd_hist=0.0,
        ),
    )

    assert (
        RiskGate._has_actionable_signal_fn(
            indicators,
            "SPY",
            bars,
            [],
        )
        is False
    )


def test_pipeline_drops_decision_when_risk_modification_invalid():
    """When RM proposes a mod that fails schema validation, the underlying
    decision must be DROPPED, not left at its original (un-tightened) value.
    RM's intent is always protective; if we can't apply the change, we can't
    assume the un-modified decision is safe. Pre-fix this used to silently
    execute the original allocation, dropping RM's protective intent."""
    pipeline = build_pipeline()
    decision = TradeDecision(
        action="BUY",
        symbol="SPY",
        allocation_pct=10,
        entry_price=500,
        stop_loss=480,
        take_profit=530,
        reasoning="test",
    )
    modifications = [
        RiskModification(
            symbol="SPY",
            field="allocation_pct",
            original_value=10,
            new_value=150,  # invalid: allocation_pct must be ≤ 100
            reason="bad mod",
        )
    ]

    updated, rejected = pipeline.risk_gate._apply_risk_modifications([decision], modifications)

    # The SPY decision is dropped — RM tried to change it, schema rejected
    # the change, so we don't execute the trade at all.
    assert updated == []
    assert rejected == []


def test_pipeline_drops_only_the_decision_with_bad_mod_keeps_rest():
    """A bad mod on SPY must not affect the QQQ decision — only the symbol
    whose mod failed gets dropped, the rest of the morning still executes."""
    pipeline = build_pipeline()
    spy = TradeDecision(
        action="BUY",
        symbol="SPY",
        allocation_pct=10,
        entry_price=500,
        stop_loss=480,
        take_profit=530,
        reasoning="t",
    )
    qqq = TradeDecision(
        action="BUY",
        symbol="QQQ",
        allocation_pct=8,
        entry_price=400,
        stop_loss=388,
        take_profit=420,
        reasoning="t",
    )
    modifications = [
        RiskModification(
            symbol="SPY",
            field="allocation_pct",
            original_value=10,
            new_value=150,
            reason="bad",
        ),
        # Valid mod on QQQ: tighten allocation 8 -> 5
        RiskModification(
            symbol="QQQ",
            field="allocation_pct",
            original_value=8,
            new_value=5,
            reason="tighten",
        ),
    ]

    updated, rejected = pipeline.risk_gate._apply_risk_modifications([spy, qqq], modifications)

    syms = [d.symbol for d in updated]
    assert syms == ["QQQ"]
    assert updated[0].allocation_pct == 5
    assert rejected == []


def test_risk_mod_matches_decision_symbol_case_insensitively():
    """RM-proposed modifications must match on symbol the same way
    `SymbolRejection` does elsewhere in this codebase: case-insensitively.
    `RiskModification.symbol` now carries the same normalizing field
    validator `SymbolRejection.symbol` does (2026-09-03), so an RM that
    emits a different case than the PM's original decision (e.g. "aapl" vs
    "AAPL") is normalized at construction, before it can ever reach a
    comparison — a silent non-match here means the modification is dropped
    and the trade ships UNMODIFIED, the opposite of this codebase's
    fail-closed posture for exactly this kind of mismatch. See
    `test_risk_mod_symbol_normalized_at_the_model_boundary` for why the fix
    lives at the model, not just at this one comparison site."""
    pipeline = build_pipeline()
    decision = TradeDecision(
        action="BUY",
        symbol="AAPL",
        allocation_pct=10,
        entry_price=200,
        stop_loss=190,
        take_profit=220,
        reasoning="test",
    )
    modifications = [
        RiskModification(
            symbol="aapl",  # different case than the decision's "AAPL"
            field="allocation_pct",
            original_value=10,
            new_value=5,
            reason="tighten",
        )
    ]

    updated, rejected = pipeline.risk_gate._apply_risk_modifications([decision], modifications)

    assert rejected == []
    assert len(updated) == 1
    assert updated[0].symbol == "AAPL"
    assert updated[0].allocation_pct == 5


def test_risk_mod_symbol_normalized_at_the_model_boundary():
    """A SECOND, independent case-sensitive comparison of `mod.symbol`
    exists in `pipeline_stages.py` (`decision.symbol in modified_symbols`,
    which decides whether a symbol's funnel outcome reads "modified" or
    "approved") — fixing only `_apply_risk_modifications`'s own comparison
    would have left this one still silently wrong on a case mismatch.
    Normalizing `RiskModification.symbol` at construction (matching
    `SymbolRejection`'s own convention) closes both call sites, and any
    future one, at once."""
    mod = RiskModification(
        symbol="aapl",
        field="allocation_pct",
        original_value=10,
        new_value=5,
        reason="tighten",
    )
    modified_symbols = {mod.symbol for mod in [mod]}
    assert "AAPL" in modified_symbols
    assert mod.symbol == "AAPL"


def test_risk_mod_cannot_silently_zero_a_sell_allocation():
    """2026-09-03 audit: an RM modification that zeros a SELL's
    allocation_pct must NOT be applied — that reads as "skip" at execution
    (see pipeline_stages.py's `RiskStage.run`) and a real exit vanishes with
    no distinguishable trace. Matches the two live 2026-08-24 incidents.
    The exit must still ship at its pre-modification size, and the refusal
    must be visible via the returned `rejected` list."""
    pipeline = build_pipeline()
    sell = TradeDecision(
        action="SELL",
        symbol="XLE",
        allocation_pct=100,
        entry_price=0,
        stop_loss=0,
        take_profit=0,
        reasoning="exit",
    )
    modifications = [
        RiskModification(
            symbol="XLE",
            field="allocation_pct",
            original_value=100,
            new_value=0,
            reason="RM thinks this exit is unnecessary",
        )
    ]

    updated, rejected = pipeline.risk_gate._apply_risk_modifications([sell], modifications)

    assert len(updated) == 1
    assert updated[0].symbol == "XLE"
    assert updated[0].allocation_pct == 100  # exit still ships, unmodified
    assert len(rejected) == 1
    assert rejected[0]["symbol"] == "XLE"
    assert rejected[0]["field"] == "allocation_pct"
    assert "protective exit" in rejected[0]["reason"]


def test_risk_mod_cannot_zero_a_cover_allocation():
    """Same guard, COVER side — D10 says a cover can never be blocked by the
    hard-risk gate, and it must not be cancellable through a modification
    either."""
    pipeline = build_pipeline()
    cover = TradeDecision(
        action="COVER",
        symbol="XLB",
        allocation_pct=50,
        entry_price=0,
        stop_loss=0,
        take_profit=0,
        reasoning="cover half",
    )
    modifications = [
        RiskModification(
            symbol="XLB",
            field="allocation_pct",
            original_value=50,
            new_value=0,
            reason="trim to nothing",
        )
    ]

    updated, rejected = pipeline.risk_gate._apply_risk_modifications([cover], modifications)

    assert updated[0].allocation_pct == 50
    assert len(rejected) == 1


def test_risk_mod_cannot_reduce_a_sell_below_its_intended_size():
    """Owner ruling 2026-09-24 (final): the seat may never block OR REDUCE a
    protective exit. A partial SELL trim (100% -> 50%) shrinks how much the
    desk sells to reduce risk, so it is now REVERTED — the SELL stays full and
    the refusal is visible. (Previously this trim applied; the ruling reverses
    that.)"""
    pipeline = build_pipeline()
    sell = TradeDecision(
        action="SELL",
        symbol="NET",
        allocation_pct=100,
        entry_price=0,
        stop_loss=0,
        take_profit=0,
        reasoning="exit",
    )
    modifications = [
        RiskModification(
            symbol="NET",
            field="allocation_pct",
            original_value=100,
            new_value=50,
            reason="only sell half",
        )
    ]

    updated, rejected = pipeline.risk_gate._apply_risk_modifications([sell], modifications)

    assert updated[0].allocation_pct == 100, "the exit stays at its intended size"
    assert len(rejected) == 1
    assert rejected[0]["symbol"] == "NET"
    assert "protective exit" in rejected[0]["reason"]


def test_risk_mod_can_still_increase_a_sell_allocation():
    """An UPWARD edit to an exit (selling MORE) only reduces risk, so it is
    still allowed — the guard reverts reductions only."""
    pipeline = build_pipeline()
    sell = TradeDecision(
        action="SELL",
        symbol="NET",
        allocation_pct=50,
        entry_price=0,
        stop_loss=0,
        take_profit=0,
        reasoning="exit",
    )
    modifications = [
        RiskModification(
            symbol="NET",
            field="allocation_pct",
            original_value=50,
            new_value=100,
            reason="sell all of it",
        )
    ]

    updated, rejected = pipeline.risk_gate._apply_risk_modifications([sell], modifications)

    assert updated[0].allocation_pct == 100, "selling more is allowed"
    assert rejected == []


def test_risk_mod_stop_widening_below_rr_floor_is_now_allowed():
    """**Inverted 2026-09-11, docs/WORK.md item 1(d).**

    The 2026-09-03 audit added this guard so an RM stop_loss widening could
    not ship a reward:risk the CONSTRUCTOR would have refused. The
    constructor no longer refuses on that floor, so the guard's whole
    premise — "a fresh decision would have to clear 1.5" — is void, and
    keeping it would leave a hard 1.5 floor alive in the one place nobody
    would think to look.

    The edit is applied. What still guards this path is unchanged and
    tested below: an edit that pulls the stop inside the ATR noise band is
    refused. An unmeasurable ratio is not an edit refusal."""
    pipeline = build_pipeline()
    buy = TradeDecision(
        action="BUY",
        symbol="SPY",
        allocation_pct=10,
        entry_price=500,
        stop_loss=490,
        take_profit=530,  # R/R = 3.0
        reasoning="test",
    )
    modifications = [
        RiskModification(
            symbol="SPY",
            field="stop_loss",
            original_value=490,
            new_value=470,  # R/R -> 1.0
            reason="give it more room",
        )
    ]

    updated, rejected = pipeline.risk_gate._apply_risk_modifications([buy], modifications)

    assert len(updated) == 1
    assert updated[0].stop_loss == 470
    assert rejected == []


def test_risk_mod_short_stop_widening_below_rr_floor_is_now_allowed():
    """Mirror of the BUY case for a SHORT — item 1(d) applies identically."""
    pipeline = build_pipeline()
    short = TradeDecision(
        action="SHORT",
        symbol="XLB",
        allocation_pct=10,
        entry_price=100,
        stop_loss=104,
        take_profit=88,  # R/R = 3.0
        reasoning="test",
    )
    modifications = [
        RiskModification(
            symbol="XLB",
            field="stop_loss",
            original_value=104,
            new_value=112,  # R/R -> 1.0
            reason="give it more room",
        )
    ]

    updated, rejected = pipeline.risk_gate._apply_risk_modifications([short], modifications)

    assert updated[0].stop_loss == 112
    assert rejected == []


def test_risk_mod_stop_inside_noise_band_is_rejected_when_bars_available():
    """A tightening RM edit can still be unsafe: pulling a stop inside the
    ATR noise band all but guarantees a whipsaw exit. When bars are
    available to compute a real ATR, this must be caught even though the
    edit looks "more protective" (smaller nominal risk)."""
    pipeline = build_pipeline()
    pipeline.config = MagicMock()
    pipeline.config.risk.absolute_min_stop_atr_multiple = 1.0

    buy = TradeDecision(
        action="BUY",
        symbol="SPY",
        allocation_pct=10,
        entry_price=500,
        stop_loss=480,
        take_profit=560,  # R/R healthy
        reasoning="test",
    )
    modifications = [
        RiskModification(
            symbol="SPY",
            field="stop_loss",
            original_value=480,
            new_value=498,  # 2 points from entry
            reason="tighten it up",
        )
    ]
    # Bars with a clear $20 daily true range -> ATR14 well above the $2
    # noise-band edge (1x ATR floor) the modified stop lands inside.
    from src.models import OHLCV
    from datetime import date as _date, timedelta as _td

    bars = [
        OHLCV(
            date=_date.today() - _td(days=20 - i),
            open=490,
            high=500,
            low=480,
            close=490,
            volume=1_000_000,
        )
        for i in range(20)
    ]
    symbols_bars = {"SPY": bars}

    updated, rejected = pipeline.risk_gate._apply_risk_modifications(
        [buy],
        modifications,
        symbols_bars=symbols_bars,
    )

    assert updated[0].stop_loss == 480  # reverted
    assert len(rejected) == 1
    assert "noise band" in rejected[0]["reason"]


def test_risk_mod_genuine_protective_tighten_still_applies():
    """Do not regress the happy path: RM tightening allocation size or a
    stop that stays outside the noise band and above the R/R floor must
    still apply exactly as before."""
    pipeline = build_pipeline()
    buy = TradeDecision(
        action="BUY",
        symbol="SPY",
        allocation_pct=10,
        entry_price=500,
        stop_loss=480,
        take_profit=560,
        reasoning="test",
    )
    modifications = [
        RiskModification(
            symbol="SPY",
            field="allocation_pct",
            original_value=10,
            new_value=5,
            reason="reduce size",
        ),
    ]

    updated, rejected = pipeline.risk_gate._apply_risk_modifications([buy], modifications)

    assert updated[0].allocation_pct == 5
    assert rejected == []


def test_fractional_sell_helpers_preserve_position_size():
    pipeline = build_pipeline()

    assert pipeline._full_sell_qty(0.4) == pytest.approx(0.4)
    # The 50% REDUCE sizing was deleted 2026-10-09 (whole exits only).
    assert not hasattr(type(pipeline), "_reduce_sell_qty")


def test_evening_return_pct_handles_zero_last_equity():
    """Evening must not divide-by-zero when last_equity is 0 (brand-new account)."""
    pipeline = build_pipeline(
        broker=MagicMock(),
        db=MagicMock(),
        macro=MagicMock(),
        evening_analyst=MagicMock(),
        _run_news_update=MagicMock(return_value=(None, None)),
        _load_earnings_analyses=MagicMock(return_value=([], [])),
    )
    pipeline.config = MagicMock()
    pipeline.config.llm.evening_analyst_model = "test-model"

    pipeline.broker.is_trading_day.return_value = True
    pipeline.broker.get_account.return_value = {"portfolio_value": 1000.0, "last_equity": 0.0}
    pipeline.broker.get_positions.return_value = []
    pipeline.db.get_trades.return_value = []
    pipeline.macro.get_macro_summary.return_value = {}
    pipeline.evening_analyst.analyze.return_value = (
        EveningReport(
            reasoning_chain=_valid_evening_rc(),
            daily_summary="Flat",
            lessons="n/a",
            tomorrow_outlook="Watch",
            risk_rating="low",
        ),
        AgentResult(raw_text="{}", tokens_used=10, model="test", user_message="test"),
    )

    result = pipeline.run_evening()

    # last_equity=0 → fall back to 0.0 daily_pnl rather than dividing by zero
    assert result["daily_pnl"] == 0.0
    assert result["daily_return_pct"] == 0.0


def test_evening_daily_pnl_uses_last_equity():
    """daily_pnl = total_value - last_equity (includes realized fills)."""
    pipeline = build_pipeline(
        broker=MagicMock(),
        db=MagicMock(),
        macro=MagicMock(),
        evening_analyst=MagicMock(),
        _run_news_update=MagicMock(return_value=(None, None)),
        _load_earnings_analyses=MagicMock(return_value=([], [])),
    )
    pipeline.config = MagicMock()
    pipeline.config.llm.evening_analyst_model = "test-model"

    pipeline.broker.is_trading_day.return_value = True
    pipeline.broker.get_account.return_value = {"portfolio_value": 10200.0, "last_equity": 10000.0}
    pipeline.broker.get_positions.return_value = []
    pipeline.db.get_trades.return_value = []
    pipeline.macro.get_macro_summary.return_value = {}
    pipeline.evening_analyst.analyze.return_value = (
        EveningReport(
            reasoning_chain=_valid_evening_rc(),
            daily_summary="Up",
            lessons="n/a",
            tomorrow_outlook="Watch",
            risk_rating="low",
        ),
        AgentResult(raw_text="{}", tokens_used=10, model="test", user_message="test"),
    )

    result = pipeline.run_evening()

    assert result["daily_pnl"] == 200.0
    assert result["daily_return_pct"] == pytest.approx(2.0)
    # daily_pnl computation must derive from broker.last_equity (not DB
    # lookup). Evening v2 does call db.get_daily_pnl for outlook-calibration
    # (different purpose), so we only assert the computed number here.


def test_evening_reconciles_before_loading_trade_inputs():
    pipeline = build_pipeline(
        broker=MagicMock(),
        db=MagicMock(),
        macro=MagicMock(),
        evening_analyst=MagicMock(),
        _run_news_update=MagicMock(return_value=(None, None)),
        _load_earnings_analyses=MagicMock(return_value=([], [])),
    )
    pipeline.config = MagicMock()
    pipeline.config.llm.evening_analyst_model = "test-model"

    events: list = []

    def _reconcile(*args, **kwargs):
        events.append("reconcile")

    def _get_trades(*args, **kwargs):
        events.append(("get_trades", kwargs))
        return []

    pipeline._reconcile_fills = MagicMock(side_effect=_reconcile)
    pipeline.broker.is_trading_day.return_value = True
    pipeline.broker.get_account.return_value = {"portfolio_value": 1000.0, "last_equity": 900.0}
    pipeline.broker.get_positions.return_value = []
    pipeline.db.get_trades.side_effect = _get_trades
    pipeline.db.get_latest_insights.return_value = None
    pipeline.macro.get_macro_summary.return_value = {}
    pipeline._build_recent_sells_for_grading = MagicMock(return_value=[])
    pipeline.evening_analyst.analyze.return_value = (
        EveningReport(
            reasoning_chain=_valid_evening_rc(),
            daily_summary="Up",
            lessons="n/a",
            tomorrow_outlook="Watch",
            risk_rating="low",
        ),
        AgentResult(raw_text="{}", tokens_used=10, model="test", user_message="test"),
    )

    pipeline.run_evening()

    assert events[0] == "reconcile"
    # limit 30: evening fetches headroom so the SWEEP_* filter can't shrink
    # the real-trade view below the original 20 (feat/returns-optimization)
    assert ("get_trades", {"limit": 30, "today_only": True, "executed_only": True}) in events
    assert pipeline._reconcile_fills.call_count == 2


def test_evening_persists_daily_pnl_when_analysis_raises():
    pipeline = build_pipeline(
        broker=MagicMock(),
        db=MagicMock(),
        macro=MagicMock(),
        evening_analyst=MagicMock(),
        _run_news_update=MagicMock(return_value=(None, None)),
        _load_earnings_analyses=MagicMock(return_value=([], [])),
        _build_recent_sells_for_grading=MagicMock(return_value=[]),
        _build_recent_buys_for_grading=MagicMock(return_value=[]),
        _build_recent_outlook_calibration=MagicMock(return_value={"samples": [], "n": 0}),
        _build_weekly_narrative=MagicMock(return_value=""),
        _build_active_state_changes=MagicMock(return_value=""),
        _reconcile_fills=MagicMock(),
    )
    pipeline.config = MagicMock()
    pipeline.config.llm.evening_analyst_model = "test-model"

    pipeline.broker.is_trading_day.return_value = True
    pipeline.broker.get_account.return_value = {
        "portfolio_value": 10200.0,
        "last_equity": 10000.0,
    }
    pipeline.broker.get_positions.return_value = []
    pipeline.db.get_trades.return_value = []
    pipeline.db.get_latest_insights.return_value = None
    pipeline.macro.get_macro_summary.return_value = {}
    pipeline.evening_analyst.analyze.side_effect = RuntimeError("provider down")

    result = pipeline.run_evening()

    assert result["status"] == "evening_analysis_error"
    assert result["analysis"] is None
    pipeline.db.insert_daily_pnl.assert_called_once()
    kwargs = pipeline.db.insert_daily_pnl.call_args.kwargs
    assert kwargs["total_value"] == 10200.0
    assert kwargs["daily_pnl"] == 200.0
    assert kwargs["daily_return_pct"] == pytest.approx(2.0)
    pipeline.db.save_evening_snapshot.assert_not_called()


def test_prelatched_evening_preserves_reconciliation_and_daily_pnl():
    pipeline = build_pipeline(
        broker=MagicMock(),
        db=MagicMock(),
        _drain_pending_protection_restores=MagicMock(),
        _reconcile_orphan_pending_submits=MagicMock(),
        _reconcile_stop_coverage=MagicMock(return_value=[]),
        _reconcile_fills=MagicMock(),
        _run_news_update=MagicMock(),
        _load_earnings_analyses=MagicMock(),
        evening_analyst=MagicMock(),
        cost_circuit=MagicMock(),
    )
    pipeline.cost_circuit.activate_session.return_value = {"suspended": True}
    pipeline.cost_circuit.require_paid_analysis.side_effect = PaidAnalysisSuspended(
        "prelatched",
        {"suspended": True},
    )
    pipeline.broker.is_trading_day.return_value = True
    pipeline.broker.get_account.return_value = {
        "portfolio_value": 10_200.0,
        "last_equity": 10_000.0,
    }
    pipeline.broker.get_positions.return_value = []
    pipeline.broker.get_recent_daily_closes.return_value = []

    result = pipeline.run_evening()

    assert result["status"] == "paid_analysis_suspended"
    assert result["daily_pnl"] == 200.0
    pipeline._drain_pending_protection_restores.assert_called_once()
    pipeline._reconcile_orphan_pending_submits.assert_called_once()
    pipeline._reconcile_stop_coverage.assert_called_once()
    pipeline._reconcile_fills.assert_called_once()
    pipeline.db.insert_daily_pnl.assert_called_once()
    pipeline._run_news_update.assert_not_called()
    pipeline._load_earnings_analyses.assert_not_called()
    pipeline.evening_analyst.analyze.assert_not_called()


def test_recent_sells_builder_reads_only_executed_trades():
    pipeline = build_pipeline(db=MagicMock(), broker=MagicMock())
    pipeline.db.get_trades.return_value = []

    assert pipeline._build_recent_sells_for_grading() == []
    pipeline.db.get_trades.assert_called_once_with(limit=200, executed_only=True)


# === Fix 9: get_trades today_only filter ===


def test_get_trades_today_only(tmp_path):
    from src.storage.db import Database

    db = Database(str(tmp_path / "test.db"))
    db.initialize()

    # Insert a trade with today's timestamp (default)
    db.insert_trade("SPY", "BUY", 10, 500, "test", "run-1", stop_loss=90.0)

    # Insert an old trade by manipulating timestamp
    db.conn.execute(
        "INSERT INTO trades (symbol, action, qty, price, reasoning, run_id, timestamp) VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("AAPL", "BUY", 5, 180, "old", "run-0", "2026-01-01 10:00:00"),
    )
    db.conn.commit()

    all_trades = db.get_trades()
    assert len(all_trades) == 2

    today_trades = db.get_trades(today_only=True)
    assert len(today_trades) == 1
    assert today_trades[0]["symbol"] == "SPY"


# === Fix 5: Broker limit_price None check ===


def test_broker_limit_price_none_vs_zero():
    """limit_price=None should be market order, limit_price=0.0 should NOT be."""
    from src.execution.broker import AlpacaBroker
    from alpaca.trading.requests import MarketOrderRequest, LimitOrderRequest

    with patch("src.execution.broker.TradingClient") as mock_cls:
        mock_client = MagicMock()
        mock_order = MagicMock()
        mock_order.id = "test-id"
        mock_order.status = "accepted"
        mock_order.symbol = "SPY"
        mock_client.submit_order.return_value = mock_order
        mock_cls.return_value = mock_client

        broker = AlpacaBroker(api_key="test", secret_key="test", paper=True)

        # None = market order
        broker.submit_order("SPY", 10, "buy", limit_price=None)
        req = mock_client.submit_order.call_args[0][0]
        assert isinstance(req, MarketOrderRequest)

        # 0.0 = limit order at $0 (should NOT be market order)
        broker.submit_order("SPY", 10, "buy", limit_price=0.0)
        req = mock_client.submit_order.call_args[0][0]
        assert isinstance(req, LimitOrderRequest)


# === Final review: leverage-adjusted pending investment ===


def test_hedge_nets_out_for_total_exposure():
    """Inverse ETFs are hedges: SQQQ short + SPY long should NET, not sum."""
    pipeline = build_pipeline(
        risk_engine=RiskRuleEngine(
            RiskConfig(
                max_position_pct=40,
                max_total_position_pct=50,  # tight limit
                max_sector_pct=90,  # high to not interfere
                require_stop_loss=True,
            )
        )
    )
    # SQQQ 10% raw * -3 = -30% signed (short Nasdaq via inverse 3x)
    # SPY  25% raw * +1 = +25% signed (long S&P)
    # Net exposure = |-30 + 25| = 5% << 50% → both pass as a hedge.
    decisions = [
        TradeDecision(
            action="BUY",
            symbol="SQQQ",
            allocation_pct=10,
            entry_price=20,
            stop_loss=18,
            take_profit=25,
            reasoning="hedge",
        ),
        TradeDecision(
            action="BUY",
            symbol="SPY",
            allocation_pct=25,
            entry_price=500,
            stop_loss=480,
            take_profit=530,
            reasoning="core",
        ),
    ]

    with (
        patch("src.pipeline_admission._get_sector", return_value="Broad"),
        patch("src.execution.broker._get_sector", return_value="Broad"),
    ):
        allowed, violations, blocked = pipeline.risk_gate._filter_hard_risk_decisions(
            decisions,
            positions=[],
            total_value=100000,
        )

    assert [d.symbol for d in allowed] == ["SQQQ", "SPY"]


def test_same_direction_longs_sum_for_total_exposure():
    """Two longs (no hedge) sum to net exposure and can exceed the cap."""
    pipeline = build_pipeline(
        risk_engine=RiskRuleEngine(
            RiskConfig(
                max_position_pct=40,
                max_total_position_pct=50,
                require_stop_loss=True,
            )
        )
    )
    # SPY 30% + QQQ 30% both long → net 60% > 50% → QQQ blocked
    decisions = [
        TradeDecision(
            action="BUY",
            symbol="SPY",
            allocation_pct=30,
            entry_price=500,
            stop_loss=480,
            take_profit=530,
            reasoning="core",
        ),
        TradeDecision(
            action="BUY",
            symbol="QQQ",
            allocation_pct=30,
            entry_price=400,
            stop_loss=380,
            take_profit=430,
            reasoning="also core",
        ),
    ]

    with (
        patch("src.pipeline_admission._get_sector", return_value="Broad"),
        patch("src.execution.broker._get_sector", return_value="Broad"),
    ):
        allowed, violations, blocked = pipeline.risk_gate._filter_hard_risk_decisions(
            decisions,
            positions=[],
            total_value=100000,
        )

    assert [d.symbol for d in allowed] == ["SPY"]
    assert any("Net exposure" in r for r in blocked)


# === MacroAnalysis Pydantic validation (2026-04-17 refactor) ===


def _valid_macro_payload(**overrides) -> dict:
    base = {
        "reasoning_chain": {
            "volatility_analysis": "VIX 19 falling.",
            "yield_curve_analysis": "Spread -0.2, narrowing.",
            "monetary_policy_analysis": "DFF 3.60 flat 30d.",
            "inflation_labor_credit": "Core CPI 2.8, UNRATE 4.1, HY OAS 380bps.",
            "cross_signal_synthesis": "Four of five align risk-on.",
            "sector_implications": "OW tech, financials.",
        },
        "regime": "risk-on",
        "confidence": "medium",
        "equity_outlook": "bullish",
        "regime_shift": False,
        "sector_guidance": [
            {"sector": "Technology", "stance": "overweight", "reason": "AI capex"},
        ],
        "position_guidance": {
            "reasoning": "Lean net long.",
        },
        "summary": "Moderately supportive.",
    }
    base.update(overrides)
    return base


def test_macro_analysis_accepts_full_schema():
    MacroAnalysis(**_valid_macro_payload())  # should not raise


def test_macro_analysis_requires_reasoning_chain():
    payload = _valid_macro_payload()
    del payload["reasoning_chain"]
    with pytest.raises(ValidationError):
        MacroAnalysis(**payload)


def test_macro_analysis_rejects_invalid_regime():
    with pytest.raises(ValidationError):
        MacroAnalysis(**_valid_macro_payload(regime="bullish"))  # not in Literal enum


def test_macro_analysis_maps_alias_sector():
    """Common LLM aliases ('Financials', 'Tech', etc.) are auto-mapped to canonical names
    instead of rejecting the whole analysis."""
    payload = _valid_macro_payload()
    payload["sector_guidance"] = [
        {"sector": "Financials", "stance": "overweight", "reason": "x"}  # alias of Financial Services
    ]
    ma = MacroAnalysis(**payload)
    assert ma.sector_guidance[0].sector == "Financial Services"


def test_macro_analysis_drops_unknown_sector_but_preserves_analysis():
    """Unmappable sectors are silently dropped; the rest of the analysis survives."""
    payload = _valid_macro_payload()
    payload["sector_guidance"] = [
        {"sector": "CompletelyMadeUpSectorName", "stance": "overweight", "reason": "x"},
        {"sector": "Technology", "stance": "overweight", "reason": "valid"},
    ]
    ma = MacroAnalysis(**payload)
    # Invalid item dropped, valid item kept
    assert [s.sector for s in ma.sector_guidance] == ["Technology"]


def test_macro_position_guidance_no_longer_carries_an_invested_number():
    """Owner mandate 2026-09-17: macro does not set how invested the desk is.
    A legacy snapshot still carrying the two old keys parses (they are
    ignored) and the numbers are not reachable from the model."""
    payload = _valid_macro_payload()
    payload["position_guidance"]["target_invested_pct"] = 150
    payload["position_guidance"]["cash_recommendation_pct"] = 40
    ma = MacroAnalysis(**payload)
    assert not hasattr(ma.position_guidance, "target_invested_pct")
    assert not hasattr(ma.position_guidance, "cash_recommendation_pct")
    assert ma.position_guidance.reasoning == "Lean net long."


# === Agent-coordination refactor (2026-04-17 follow-up) ===


def test_deployment_gap_emits_advisory_violation():
    """When projected invested is UNDER the fully-invested mandate by more
    than the book's own one-session drift (an empty book drifts 0pp), a
    non-blocking violation is emitted."""
    pipeline = build_pipeline(
        risk_engine=RiskRuleEngine(
            RiskConfig(
                max_position_pct=40,
                max_total_position_pct=90,
                require_stop_loss=True,
            )
        )
    )
    decisions = [
        TradeDecision(
            action="BUY",
            symbol="SPY",
            allocation_pct=40,
            entry_price=500,
            stop_loss=480,
            take_profit=530,
            reasoning="aggressive",
        ),
    ]
    with (
        patch("src.pipeline_admission._get_sector", return_value="Broad"),
        patch("src.execution.broker._get_sector", return_value="Broad"),
    ):
        allowed, violations, blocked = pipeline.risk_gate._filter_hard_risk_decisions(
            decisions,
            positions=[],
            total_value=100000,
            invested_target_pct=100,  # mandate 100%, PM projects 40%
        )

    assert [d.symbol for d in allowed] == ["SPY"]  # advisory — not blocked
    deviation_rules = [v.rule for v in violations]
    assert "deployment_gap" in deviation_rules


def test_deployment_gap_skipped_when_within_tolerance():
    """99.5% invested in SPY with ATR(14) $5 on a $500 price: the book's
    one-session drift is 99.5 x 5/500 = 0.995pp, so a 0.5pp gap is inside it."""
    pipeline = build_pipeline(
        risk_engine=RiskRuleEngine(
            RiskConfig(
                max_position_pct=40,
                max_total_position_pct=90,
                require_stop_loss=True,
            )
        )
    )
    pipeline._atr_for_symbol = lambda symbol: 5.0
    from src.models import Position

    held = [
        Position(
            symbol="SPY",
            qty=199,
            avg_entry=500,
            current_price=500,
            market_value=99_500,
            unrealized_pnl=0.0,
            unrealized_intraday_pnl=0.0,
            sector="Broad",
        )
    ]
    with (
        patch("src.pipeline_admission._get_sector", return_value="Broad"),
        patch("src.execution.broker._get_sector", return_value="Broad"),
    ):
        _, violations, _ = pipeline.risk_gate._filter_hard_risk_decisions(
            [],
            positions=held,
            total_value=100000,
            invested_target_pct=100,  # 99.5% vs 100% = -0.5pp, inside the 0.995pp drift
        )
    assert not any(v.rule == "deployment_gap" for v in violations)


# === TechAnalysisResult v2 (reasoning_chain + cross-field validator + conviction) ===


def test_tech_analysis_neutral_clears_price_fields():
    """Neutral rating must null-out price fields even if the LLM emitted stale ones."""
    r = TechAnalysisResult(
        symbol="SPY",
        rating="neutral",
        entry_price=500,
        reference_target=530,
        stop_loss=480,
        reasoning="Mixed signals",
        reasoning_chain=_trc(),
        thesis_invalid_if="closes below support",
    )
    assert r.entry_price is None
    assert r.reference_target is None
    assert r.stop_loss is None


def test_tech_analysis_buy_without_prices_rejected():
    """BUY/SELL/strong_* must carry entry + stop — validator blocks orphan actionable ratings."""
    with pytest.raises(ValidationError):
        TechAnalysisResult(
            symbol="SPY",
            rating="buy",
            reasoning="x",
            thesis_invalid_if="closes below support",
        )
    with pytest.raises(ValidationError):
        TechAnalysisResult(
            symbol="SPY",
            rating="strong_sell",
            entry_price=500,
            reasoning="x",
            thesis_invalid_if="closes below support",
        )


def test_tech_analysis_buy_stop_must_be_below_entry():
    """For BUY the protective stop must be below entry; reversed = validator error."""
    with pytest.raises(ValidationError):
        TechAnalysisResult(
            symbol="SPY",
            rating="buy",
            entry_price=500,
            stop_loss=520,  # stop ABOVE entry — wrong
            reasoning="x",
            thesis_invalid_if="closes below support",
        )
    # Reversed direction is correct for SELL
    ok = TechAnalysisResult(
        symbol="SPY",
        rating="sell",
        entry_price=500,
        stop_loss=520,  # stop ABOVE entry — correct for SELL
        reference_target=470,
        support_levels=[],
        resistance_levels=[520.0],
        setup_type="range",
        expected_horizon_sessions=10,
        reasoning="x",
        reasoning_chain=_trc(),
        thesis_invalid_if="closes below support",
    )
    assert ok.stop_loss == 520


def test_tech_analysis_conviction_defaults_to_medium():
    r = TechAnalysisResult(
        symbol="SPY",
        rating="buy",
        entry_price=500,
        stop_loss=490,
        reference_target=520,
        support_levels=[490.0],
        resistance_levels=[520.0],
        setup_type="range",
        expected_horizon_sessions=10,
        reasoning="x",
        reasoning_chain=_trc(),
        thesis_invalid_if="closes below support",
    )
    assert r.conviction == "medium"


def test_tech_analysis_rr_computed_for_buy():
    """R/R = (target - entry) / (entry - stop) for buy/strong_buy; computed, not LLM-provided."""
    r = TechAnalysisResult(
        symbol="SPY",
        rating="buy",
        entry_price=500,
        stop_loss=490,
        reference_target=525,
        support_levels=[490.0],
        resistance_levels=[525.0],
        setup_type="range",
        expected_horizon_sessions=10,
        reasoning="x",
        reasoning_chain=_trc(),
        thesis_invalid_if="closes below support",
    )
    # risk = 10, reward = 25 → 2.5
    assert r.risk_reward == 2.5


def test_tech_analysis_rr_computed_for_sell():
    """For SELL the sides flip: risk = stop - entry, reward = entry - target."""
    r = TechAnalysisResult(
        symbol="SPY",
        rating="sell",
        entry_price=500,
        stop_loss=510,
        reference_target=475,
        support_levels=[475.0],
        resistance_levels=[510.0],
        setup_type="range",
        expected_horizon_sessions=10,
        reasoning="x",
        reasoning_chain=_trc(),
        thesis_invalid_if="closes below support",
    )
    # risk = 10, reward = 25 → 2.5
    assert r.risk_reward == 2.5


def test_tech_analysis_rr_none_for_neutral_or_missing_target():
    """Neutral clears prices (validator) so R/R is None.

    A missing target on an actionable rating is kept and yields None (owner
    rule 2026-10-09: a missing target never blocks a trade; the 2026-08-27
    rejection protected nothing because take-profit is measured).
    """
    neutral = TechAnalysisResult(
        symbol="SPY",
        rating="neutral",
        entry_price=500,
        stop_loss=490,
        reference_target=525,
        reasoning="x",
        reasoning_chain=_trc(),
        thesis_invalid_if="closes below support",
    )
    assert neutral.risk_reward is None
    assert (
        TechAnalysisResult(
            symbol="SPY",
            rating="buy",
            entry_price=500,
            stop_loss=490,
            reference_target=None,
            support_levels=[490.0],
            resistance_levels=[],
            setup_type="range",
            expected_horizon_sessions=10,
            reasoning="x",
            reasoning_chain=_trc(),
            thesis_invalid_if="closes below support",
        ).risk_reward
        is None
    )


def test_tech_analysis_rr_handles_malformed_geometry():
    """Target below entry on a BUY is FLAGGED, not rejected (owner rule
    2026-10-09): the signal survives with `reference_target_wrong_side`, and
    the ratio from that malformed geometry is still None, never a number."""
    result = TechAnalysisResult(
        symbol="SPY",
        rating="buy",
        entry_price=500,
        stop_loss=490,
        reference_target=495,  # target below entry
        support_levels=[490.0],
        resistance_levels=[],
        setup_type="range",
        expected_horizon_sessions=10,
        reasoning="x",
        reasoning_chain=_trc(),
        thesis_invalid_if="closes below support",
    )
    assert result.reference_target_wrong_side is True
    assert result.risk_reward is None


def test_tech_analysis_thesis_invalid_if_required_on_actionable():
    """Actionable ratings need a real I'll-sell-if. Neutrals may omit."""
    # Recorded as a gap, not rejected, since 2026-10-12 (XOM incident).
    missing = TechAnalysisResult(
        symbol="SPY",
        rating="buy",
        entry_price=500,
        stop_loss=490,
        reference_target=525,
        support_levels=[490.0],
        resistance_levels=[525.0],
        setup_type="range",
        expected_horizon_sessions=10,
        reasoning="x",
        reasoning_chain=_trc(),
    )
    assert missing.missing_levels == ["thesis_invalid_if"]
    r = TechAnalysisResult(
        symbol="SPY",
        rating="buy",
        entry_price=500,
        stop_loss=490,
        reference_target=525,
        support_levels=[490.0],
        resistance_levels=[525.0],
        setup_type="range",
        expected_horizon_sessions=10,
        reasoning="x",
        reasoning_chain=_trc(),
        thesis_invalid_if="closes below 490",
    )
    assert r.thesis_invalid_if == "closes below 490"
    neutral = TechAnalysisResult(
        symbol="SPY",
        rating="neutral",
        reasoning="x",
        reasoning_chain=_trc(),
    )
    assert neutral.thesis_invalid_if == ""


def test_tech_analysis_rr_exposed_via_model_dump():
    """computed_field must serialize into model_dump() so downstream consumers see it."""
    r = TechAnalysisResult(
        symbol="SPY",
        rating="buy",
        entry_price=500,
        stop_loss=490,
        reference_target=525,
        support_levels=[490.0],
        resistance_levels=[525.0],
        setup_type="range",
        expected_horizon_sessions=10,
        reasoning="x",
        reasoning_chain=_trc(),
        thesis_invalid_if="closes below support",
    )
    dumped = r.model_dump()
    assert dumped.get("risk_reward") == 2.5


def test_tech_reasoning_chain_requires_all_five_fields():
    # All 5 required, any missing → ValidationError
    with pytest.raises(ValidationError):
        TechReasoningChain(trend="a", momentum="b")  # missing volatility/volume/support_resistance
    # All present → ok
    rc = TechReasoningChain(
        trend="a",
        momentum="b",
        volatility="c",
        volume="d",
        support_resistance="e",
    )
    assert rc.trend == "a"


# === RM reasoning_chain + scale_all_buys + Midday/Evening Pydantic ===


def test_risk_reasoning_chain_requires_all_six_fields():
    with pytest.raises(ValidationError):
        RiskReasoningChain(rr_audit="a")  # missing 5 others
    rc = RiskReasoningChain(
        rr_audit="a",
        signal_fidelity="b",
        correlation_check="c",
        event_risk="d",
        sizing_sanity="e",
        overall="f",
    )
    assert rc.overall == "f"


def test_risk_verdict_accepts_reasoning_chain():
    v = RiskVerdict(
        approved=True,
        reasoning_chain=RiskReasoningChain(
            rr_audit="all >= 2",
            signal_fidelity="aligned",
            correlation_check="no cluster",
            event_risk="no event",
            sizing_sanity="proportional",
            overall="clean",
        ),
        reasoning="OK",
    )
    assert v.reasoning_chain is not None
    assert v.reasoning_chain.rr_audit == "all >= 2"


def test_midday_action_trail_stop_requires_price():
    with pytest.raises(ValidationError):
        PositionAction(action="TRAIL_STOP", symbol="SPY", reason="x")  # no new_stop_price
    ok = PositionAction(action="TRAIL_STOP", symbol="SPY", reason="x", new_stop_price=500.0)
    assert ok.new_stop_price == 500.0


def _stub_reasoning_chain(**overrides):
    """Test helper — minimal valid PositionReasoningChain."""
    from src.models import PositionReasoningChain

    defaults = {
        "macro_continuity_check": "regime stable vs morning",
        "thesis_progress_check": "all positions on pace",
        "thesis_integrity_check": "no triggers firing",
        "winners_discipline_check": "no flags set",
        "session_disposition_check": "patient — nothing forcing action",
        "execution_rationale": "all HOLD; nothing to justify",
    }
    defaults.update(overrides)
    return PositionReasoningChain(**defaults)


def test_midday_review_rejects_unknown_action():
    with pytest.raises(ValidationError):
        PositionReview(
            reasoning_chain=_stub_reasoning_chain(),
            actions=[{"action": "TRIAL_STOP", "symbol": "SPY", "reason": "typo"}],  # TRIAL vs TRAIL
            overall_assessment="x",
            risk_level="moderate",
        )


def test_midday_review_accepts_full_schema():
    r = PositionReview(
        reasoning_chain=_stub_reasoning_chain(),
        actions=[
            PositionAction(action="HOLD", symbol="SPY", reason="still green"),
            PositionAction(action="TRAIL_STOP", symbol="NVDA", reason="up 12%", new_stop_price=195.0),
            PositionAction(action="SELL", symbol="AAPL", reason="thesis broke"),
        ],
        overall_assessment="Mix of hold + one trail + one cut",
        risk_level="moderate",
    )
    assert len(r.actions) == 3
    assert r.actions[1].new_stop_price == 195.0


def test_evening_report_requires_core_fields():
    # Missing reasoning_chain + lessons + tomorrow_outlook + risk_rating all fail.
    with pytest.raises(ValidationError):
        EveningReport(daily_summary="x")
    ok = EveningReport(
        reasoning_chain=_valid_evening_rc(),
        daily_summary="up 0.8%",
        lessons="be patient",
        tomorrow_outlook="watch FOMC",
        risk_rating="moderate",
        suggested_actions=["tighten IWM stop"],
        previous_outlook_assessment="yesterday's call was directionally right",
    )
    assert ok.risk_rating == "moderate"


def test_evening_report_rejects_invalid_risk_rating():
    with pytest.raises(ValidationError):
        EveningReport(
            reasoning_chain=_valid_evening_rc(),
            daily_summary="x",
            lessons="y",
            tomorrow_outlook="z",
            risk_rating="catastrophic",  # not in enum
        )


def test_data_degraded_violation_fires_at_two_failures():
    """When 2+ upstream sources fail, morning emits a non-blocking data_degraded violation."""
    from src import evidence_gate

    # Production helper, not a copy of the allow-list — the 2026-09-16
    # intra veto existed because this test asserted a local `not in
    # ("ok", "empty")` that had drifted from what Risk actually needed.
    status = {"macro": "failed", "news": "failed", "tech": "ok", "earnings": "ok"}
    degraded = [k for k, v in status.items() if evidence_gate.counts_as_degraded(v)]
    assert len(degraded) == 2  # threshold met

    status2 = {"macro": "failed", "news": "ok", "tech": "ok", "earnings": "ok"}
    degraded2 = [k for k, v in status2.items() if evidence_gate.counts_as_degraded(v)]
    assert len(degraded2) == 1  # under threshold, no advisory


def test_risk_verdict_accepts_scale_all_buys():
    from src.models import RiskVerdict

    v = RiskVerdict(approved=True, scale_all_buys=0.5, reasoning="Cut exposure", reasoning_chain=_risk_rc())
    assert v.scale_all_buys == 0.5
    # bounds
    with pytest.raises(ValidationError):
        RiskVerdict(approved=True, scale_all_buys=1.5, reasoning="x", reasoning_chain=_risk_rc())
    with pytest.raises(ValidationError):
        RiskVerdict(approved=True, scale_all_buys=-0.1, reasoning="x", reasoning_chain=_risk_rc())


def test_macro_analysis_allows_all_yfinance_sectors():
    """All 12 canonical sectors should validate, so sector_guidance covers the universe."""
    canonical = [
        "Technology",
        "Financial Services",
        "Healthcare",
        "Consumer Cyclical",
        "Consumer Defensive",
        "Energy",
        "Industrials",
        "Communication Services",
        "Utilities",
        "Basic Materials",
        "Real Estate",
        "Broad",
    ]
    for sec in canonical:
        MacroSectorGuidance(sector=sec, stance="neutral", reason="test")


def test_single_position_cap_uses_gross_leverage():
    """SQQQ 8% raw * 3x = 24% gross > max_position_pct=20 → blocks at single-position cap."""
    engine = RiskRuleEngine(
        RiskConfig(
            max_position_pct=20,
            max_total_position_pct=90,  # high, doesn't interfere
            require_stop_loss=True,
        )
    )
    decision = TradeDecision(
        action="BUY",
        symbol="SQQQ",
        allocation_pct=8,
        entry_price=30,
        stop_loss=28,
        take_profit=35,
        reasoning="hedge",
    )
    with patch("src.execution.broker._get_sector", return_value="Unknown"):
        violations = engine.check(
            decision=decision,
            positions=[],
            total_value=100000,
        )
    rules = [v.rule for v in violations]
    assert "max_position_pct" in rules


def test_insights_excludes_today(tmp_path):
    """get_latest_insights(before_date=today) should not return today's insights."""
    from src.storage.db import Database

    today = str(date.today())
    db = Database(str(tmp_path / "test.db"))
    db.initialize()

    db.save_insights(
        date=today,
        tomorrow_outlook="Today's outlook",
        lessons="Today's lesson",
        suggested_actions="[]",
        risk_rating="moderate",
    )
    db.save_insights(
        date="2026-04-01",
        tomorrow_outlook="Old outlook",
        lessons="Old lesson",
        suggested_actions="[]",
        risk_rating="low",
    )

    result = db.get_latest_insights(before_date=today)
    assert result is not None
    assert result["date"] == "2026-04-01"
    assert result["tomorrow_outlook"] == "Old outlook"


def test_sell_allocation_100_is_full_sell():
    """allocation_pct=100 for SELL should be treated as full sell, not partial."""
    pipeline = build_pipeline()
    decision = TradeDecision(
        action="SELL",
        symbol="SPY",
        allocation_pct=100,
        entry_price=0,
        stop_loss=0,
        take_profit=0,
        reasoning="exit",
    )
    # 0 < 100 < 100 is False, so it should hit the full sell branch
    assert not (0 < decision.allocation_pct < 100)


def test_macd_prefilter_normalizes_by_price():
    """MACD threshold should be relative to price, not absolute."""
    from src.models import TechnicalIndicators

    # High-price stock ($900): MACD hist 0.5 is only 0.056% → should NOT trigger
    high_price = TechnicalIndicators(symbol="COST", ma_20=900.0, macd_hist=0.5)
    pct = abs(high_price.macd_hist) / high_price.ma_20
    assert pct < 0.003  # 0.00056, triggers

    # Low-price stock ($5): MACD hist 0.5 is 10% → should NOT trigger
    low_price = TechnicalIndicators(symbol="ONDS", ma_20=5.0, macd_hist=0.5)
    pct = abs(low_price.macd_hist) / low_price.ma_20
    assert pct >= 0.003  # 0.1, does NOT trigger — correct!


# ---------------------------------------------------------------------------
# audit F2: a retryable failure status must exit non-zero so the OS-timer
# wrapper does NOT mark the slot done and the next tick retries.
# ---------------------------------------------------------------------------


def _run_main_with_result(monkeypatch, result_dict):
    """Invoke main.main() with the pipeline mocked to return result_dict.

    Returns the SystemExit raised (or None if main returned normally).
    """
    import main as main_mod

    fake_pipeline = MagicMock()
    fake_pipeline.run_morning.return_value = result_dict

    monkeypatch.setattr(main_mod, "load_config", lambda _p: MagicMock())
    monkeypatch.setattr(main_mod, "refresh_pricing", lambda: None)
    monkeypatch.setattr(main_mod, "TradingPipeline", lambda _c: fake_pipeline)
    monkeypatch.setattr(main_mod, "TelegramNotifier", lambda: MagicMock())
    # None means the caller skips the Telegram send path entirely.
    monkeypatch.setattr(
        main_mod,
        "format_session_result",
        lambda *a, **k: None,
    )
    monkeypatch.setattr("sys.argv", ["main.py", "--mode", "morning"])

    try:
        main_mod.main()
        return None
    except SystemExit as exc:
        return exc


def test_main_exits_nonzero_on_retryable_status(monkeypatch):
    """Only external-data failures retry the slot; semantic PM faults do not."""
    for status in ("broker_error", "fetch_error"):
        exc = _run_main_with_result(monkeypatch, {"status": status})
        assert exc is not None, f"{status} should raise SystemExit"
        assert exc.code == 1, f"{status} should exit code 1, got {exc.code}"

    for status in ("analysis_error", "agent_failure", "pm_parse_error"):
        exc = _run_main_with_result(monkeypatch, {"status": status})
        assert exc is None, f"{status} must not auto-repeat the paid stack"


def test_main_exits_zero_on_terminal_status(monkeypatch):
    """Terminal 'nothing to do' / success outcomes must exit 0 — otherwise
    a normal no-trade morning would loop every tick."""
    for status in ("no_trades", "executed", "market_holiday", "reviewed", "emergency_sold", "no_data"):
        exc = _run_main_with_result(monkeypatch, {"status": status})
        assert exc is None, f"{status} must NOT raise SystemExit (exited {exc})"


# === 2026-06-07 reflection P4c: trend (5-session) calibration metric ===


def test_outlook_calibration_adds_5session_trend_metric():
    """next-day hit rate is NOISE in a trend; the new 5-session forward
    (trend) hit rate is the directional scorecard. Scenario: bullish calls
    where every NEXT day is +0.2% (< 0.3 band → next-day MISS) but the
    5-session cumulative is +1.0% (> 0.75 band → trend MATCH). The fix must
    score next-day=0% but trend=100% so evening stops concluding 'too
    bullish → default neutral'."""
    pipeline = build_pipeline(db=MagicMock())
    # insights returned most-recent-first; three bullish/high calls
    pipeline.db.get_recent_insights = MagicMock(
        return_value=[
            {"date": "2026-04-15", "tomorrow_bias": "bullish", "tomorrow_conviction": "high"},
            {"date": "2026-04-14", "tomorrow_bias": "bullish", "tomorrow_conviction": "high"},
            {"date": "2026-04-13", "tomorrow_bias": "bullish", "tomorrow_conviction": "high"},
        ]
    )
    # trading days, each +0.2% (flat-positive: next-day miss, 5d sum positive)
    days = [
        "2026-04-14",
        "2026-04-15",
        "2026-04-16",
        "2026-04-17",
        "2026-04-20",
        "2026-04-21",
        "2026-04-22",
        "2026-04-23",
    ]
    pipeline.db.get_daily_pnl = MagicMock(return_value=[{"date": d, "daily_return_pct": 0.2} for d in days])

    calib = pipeline._build_recent_outlook_calibration(lookback=10)

    assert calib["n"] == 3
    # next-day: every +0.2 < 0.3 band → all miss
    assert calib["bullish_hit_rate_pct"] == 0.0
    # 5-session: cumulative 5×0.2 = 1.0 > 0.75 band → all match
    assert calib["bullish_trend_hit_rate_pct"] == 100.0
    assert calib["overall_trend_hit_rate_pct"] == 100.0
    # samples carry both metrics
    s = calib["samples"][0]
    assert s["matched"] is False
    assert s["trend_matched"] is True
    assert abs(s["fwd5_return_pct"] - 1.0) < 1e-9


def test_outlook_calibration_trend_none_when_no_forward_window():
    """A prediction at the very end of the series has no 5-session forward
    window yet → trend_matched None, trend rates None (not a false 0%)."""
    pipeline = build_pipeline(db=MagicMock())
    pipeline.db.get_recent_insights = MagicMock(
        return_value=[
            {"date": "2026-04-15", "tomorrow_bias": "bullish", "tomorrow_conviction": "high"},
        ]
    )
    # only ONE forward day exists (next-day resolves; 5-session can't)
    pipeline.db.get_daily_pnl = MagicMock(
        return_value=[
            {"date": "2026-04-16", "daily_return_pct": 0.5},
        ]
    )
    calib = pipeline._build_recent_outlook_calibration(lookback=10)
    assert calib["n"] == 1
    assert calib["samples"][0]["trend_matched"] is None
    assert calib["bullish_trend_hit_rate_pct"] is None  # no resolved window


# === 2026-06-07 CoT optimization #2: PM pre-mortem reasoning field ===


def test_pm_reasoning_chain_premortem_optional_default_and_accepts_value():
    """premortem_check follows the continuity_check pattern: optional-default
    (zero blast radius on existing logs/tests that omit it) but the prompt
    makes it mandatory. Adding it must NOT break a ReasoningChain built with
    only the 7 required fields."""
    from src.models import ReasoningChain

    rc = ReasoningChain(
        macro_filter="x",
        news_check="x",
        earnings_check="x",
        signal_conflicts="x",
        sizing_logic="x",
        portfolio_balance="x",
        cash_target="x",
    )
    assert rc.premortem_check == ""  # backward-compatible default
    rc2 = ReasoningChain(
        macro_filter="x",
        news_check="x",
        earnings_check="x",
        signal_conflicts="x",
        sizing_logic="x",
        portfolio_balance="x",
        cash_target="x",
        premortem_check="bear case: crowded beta; falsifier: MA50 break",
    )
    assert "falsifier" in rc2.premortem_check


def test_pm_prompt_example_reasoning_chain_parses_with_premortem():
    """The reasoning_chain example in portfolio_manager.md must stay parseable
    into the model (incl. the new premortem_check) — pins prompt↔schema sync."""
    import json, re
    from pathlib import Path
    from src.models import ReasoningChain

    pm_path = Path(__file__).resolve().parent.parent / "config" / "prompts" / "portfolio_manager.md"
    text = pm_path.read_text()
    # pull the first reasoning_chain object out of the fenced JSON example
    m = re.search(r'"reasoning_chain":\s*(\{.*?\n  \})', text, re.DOTALL)
    assert m, "PM prompt no longer has a reasoning_chain JSON example"
    rc = ReasoningChain(**json.loads(m.group(1)))
    assert rc.premortem_check  # the example populates it (non-empty)


# ===========================================================================
# PR #99: evening equity_close backfill — pipeline integration. The db-level
# SQL is covered in test_db.py; these pin the run_evening loop itself: the
# today_str exclusion (the only thing keeping an unsettled same-day bar out
# of a permanent NULL-only fill), the corrupt-value guard, and the per-date
# error isolation.
# ===========================================================================


def _evening_pipeline_with_closes(closes):
    """Minimal run_evening harness (mirrors the evening tests above) with a
    real get_recent_daily_closes payload so the backfill loop executes."""
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
    pipeline.config = MagicMock()
    pipeline.config.llm.evening_analyst_model = "test-model"
    pipeline.broker.is_trading_day.return_value = True
    pipeline.broker.get_account.return_value = {"portfolio_value": 10_000.0, "last_equity": 10_000.0}
    pipeline.broker.get_positions.return_value = []
    pipeline.db.get_trades.return_value = []
    pipeline.macro.get_macro_summary.return_value = {}
    pipeline.evening_analyst.analyze.return_value = (
        EveningReport(
            reasoning_chain=_valid_evening_rc(),
            daily_summary="Flat",
            lessons="n/a",
            tomorrow_outlook="Watch",
            risk_rating="low",
        ),
        AgentResult(raw_text="{}", tokens_used=10, model="test", user_message="test"),
    )
    pipeline.broker.get_recent_daily_closes.return_value = closes
    return pipeline


def test_evening_backfills_prior_dates_but_never_today():
    """[PR #99] The API-lag self-heal must backfill PRIOR dates and must
    NEVER write today's bar — today's row is owned by the 4pm-snapshot
    branch + save_evening_snapshot, even when today's bar IS present."""
    from src.trading_calendar import session_date_key

    today = session_date_key()
    closes = [("2026-01-02", 100_000.0), ("2026-01-05", 100_500.0), (today, 100_700.0)]
    pipeline = _evening_pipeline_with_closes(closes)
    pipeline.run_evening()
    called_dates = [c.args[0] for c in pipeline.db.backfill_equity_close.call_args_list]
    assert "2026-01-02" in called_dates and "2026-01-05" in called_dates
    assert today not in called_dates


def test_evening_backfill_runs_in_lag_case_without_today():
    """[PR #99] The lag case (today's bar absent — the branch that motivates
    the self-heal): every prior date is backfilled."""
    closes = [("2026-01-02", 100_000.0), ("2026-01-05", 100_500.0)]
    pipeline = _evening_pipeline_with_closes(closes)
    pipeline.run_evening()
    called_dates = [c.args[0] for c in pipeline.db.backfill_equity_close.call_args_list]
    assert called_dates == ["2026-01-02", "2026-01-05"]


def test_evening_backfill_skips_zero_and_nonfinite_values():
    """[PR #99 review] A backfilled value is permanent (NULL-only fill), so
    0.0 (pre-funding/reset), NaN (sqlite binds it as NULL → fake success
    log forever), inf, and negatives must never reach the DB."""
    closes = [
        ("2026-01-02", 0.0),
        ("2026-01-05", float("nan")),
        ("2026-01-06", float("inf")),
        ("2026-01-07", -5.0),
        ("2026-01-08", 100_500.0),  # the one legit value
    ]
    pipeline = _evening_pipeline_with_closes(closes)
    pipeline.run_evening()
    called_dates = [c.args[0] for c in pipeline.db.backfill_equity_close.call_args_list]
    assert called_dates == ["2026-01-08"]


def test_evening_backfill_one_bad_date_does_not_abort_the_rest():
    """[PR #99] A DB error on one date must not abort backfill of later
    dates (per-date try/except) nor crash the evening run."""
    closes = [("2026-01-02", 100_000.0), ("2026-01-05", 100_500.0)]
    pipeline = _evening_pipeline_with_closes(closes)
    pipeline.db.backfill_equity_close.side_effect = [RuntimeError("locked"), True]
    pipeline.run_evening()  # must not raise
    assert pipeline.db.backfill_equity_close.call_count == 2


# ===========================================================================
# PR #99: main.py crash-visibility net — early crashes (config load, missing
# config file) must produce a FAILED Telegram push and still exit non-zero.
# ===========================================================================


def test_main_pushes_failed_notification_when_config_load_crashes(monkeypatch):
    """[PR #99] A load_config crash (e.g. pydantic ValidationError) must
    produce a FAILED push (when Telegram creds are available) AND re-raise
    so the process exits non-zero."""
    import main as main_mod

    sent = []
    fake_notifier = MagicMock()
    fake_notifier.send = lambda msg, **kwargs: sent.append(msg) or True
    monkeypatch.setattr(main_mod, "TelegramNotifier", lambda: fake_notifier)
    monkeypatch.setattr(
        main_mod,
        "load_config",
        MagicMock(side_effect=RuntimeError("OPENAI_API_KEY is required")),
    )
    monkeypatch.setattr("sys.argv", ["main.py", "--mode", "morning"])

    with pytest.raises(RuntimeError, match="OPENAI_API_KEY"):
        main_mod.main()

    assert any("FAILED" in m and "OPENAI_API_KEY" in m for m in sent)


def test_main_missing_config_file_push_carries_the_path(monkeypatch, tmp_path):
    """[PR #99] The missing-config sys.exit must carry the path into the
    push — str(SystemExit(1)) is just '1', useless from a phone."""
    import main as main_mod

    sent = []
    fake_notifier = MagicMock()
    fake_notifier.send = lambda msg, **kwargs: sent.append(msg) or True
    monkeypatch.setattr(main_mod, "TelegramNotifier", lambda: fake_notifier)
    missing = str(tmp_path / "nope.yaml")
    monkeypatch.setattr("sys.argv", ["main.py", "--mode", "morning", "--config", missing])

    with pytest.raises(SystemExit) as ei:
        main_mod.main()

    assert ei.value.code != 0  # non-zero exit preserved
    assert any("FAILED" in m and "nope.yaml" in m for m in sent)


def test_main_live_mode_graceful_scheduler_exit_notifies_clearly(monkeypatch):
    """[PR #99] scheduler.start() returning gracefully must push a clear
    'scheduler_exited' status, not '⚪ live returned non-dict result'."""
    import main as main_mod

    sent = []
    fake_notifier = MagicMock()
    fake_notifier.send = lambda msg, **kwargs: sent.append(msg) or True
    monkeypatch.setattr(main_mod, "TelegramNotifier", lambda: fake_notifier)
    monkeypatch.setattr(main_mod, "load_config", lambda _p: MagicMock())
    monkeypatch.setattr(main_mod, "refresh_pricing", lambda: None)
    monkeypatch.setattr(main_mod, "TradingScheduler", lambda _c: MagicMock())
    monkeypatch.setattr("sys.argv", ["main.py", "--mode", "live"])

    main_mod.main()

    # 2026-09-17: the status header is now plain English, not the raw
    # snake_case code. 2026-09-18: the whole header is a plain sentence —
    # plain session name, plain outcome, no run identifier.
    assert any("the scheduler stopped" in m for m in sent)
    assert not any("scheduler_exited" in m for m in sent)
    assert not any("reported nothing the desk could read" in m for m in sent)


def test_risk_mod_cannot_increase_a_buy_allocation():
    """2026-09-18: `RiskModification.new_value` is unbounded and
    `TradeDecision.allocation_pct` only clamps 0-100, so nothing stopped the
    risk seat — whose job is to be MORE protective — from ENLARGING a BUY.
    On a BUY that adds to a held name the field is an increment, so an upward
    edit grows the position by more than the number reads. An increase must
    be reverted and recorded in `rejected`; the BUY ships at the
    constructor's size."""
    pipeline = build_pipeline()
    buy = TradeDecision(
        action="BUY",
        symbol="RSG",
        allocation_pct=44.23,
        entry_price=100,
        stop_loss=95,
        take_profit=115,
        reasoning="add",
    )
    modifications = [
        RiskModification(
            symbol="RSG",
            field="allocation_pct",
            original_value=44.23,
            new_value=50.0,
            reason="RM wants a bigger position",
        )
    ]

    updated, rejected = pipeline.risk_gate._apply_risk_modifications([buy], modifications)

    assert len(updated) == 1
    assert updated[0].allocation_pct == 44.23  # unchanged
    assert len(rejected) == 1
    assert rejected[0]["symbol"] == "RSG"
    assert rejected[0]["field"] == "allocation_pct"
    assert "INCREASE" in rejected[0]["reason"]


def test_risk_mod_reducing_a_buy_allocation_still_applies():
    """The guard above must not block the seat's actual job. A REDUCTION of a
    BUY's allocation_pct is applied exactly as before."""
    pipeline = build_pipeline()
    buy = TradeDecision(
        action="BUY",
        symbol="RSG",
        allocation_pct=44.23,
        entry_price=100,
        stop_loss=95,
        take_profit=115,
        reasoning="add",
    )
    modifications = [
        RiskModification(
            symbol="RSG",
            field="allocation_pct",
            original_value=44.23,
            new_value=9.23,
            reason="trim",
        )
    ]

    updated, rejected = pipeline.risk_gate._apply_risk_modifications([buy], modifications)

    assert updated[0].allocation_pct == 9.23
    assert rejected == []


def test_out_of_range_buy_allocation_still_drops_the_decision():
    """Ordering guard. The BUY-may-only-reduce check runs AFTER schema
    validation, so an out-of-range `allocation_pct` (>100) keeps hitting the
    validation branch and DROPS the decision — the stricter outcome, and the
    behaviour `test_pipeline_drops_decision_when_risk_modification_invalid`
    pins. Only values Pydantic accepts reach the reduce-only guard."""
    pipeline = build_pipeline()
    buy = TradeDecision(
        action="BUY",
        symbol="SPY",
        allocation_pct=10,
        entry_price=500,
        stop_loss=480,
        take_profit=530,
        reasoning="t",
    )
    modifications = [
        RiskModification(
            symbol="SPY",
            field="allocation_pct",
            original_value=10,
            new_value=150,
            reason="bad",
        )
    ]

    updated, rejected = pipeline.risk_gate._apply_risk_modifications([buy], modifications)

    assert updated == []  # dropped, not reverted
    assert rejected == []
