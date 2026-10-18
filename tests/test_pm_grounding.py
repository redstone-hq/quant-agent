"""Deterministic PM provenance/holding boundary and production-scale context."""

import json
from datetime import date

from unittest.mock import patch

from src.agents.portfolio_manager import PortfolioManagerAgent
from src.agents.base import AgentResult
from src.models import (
    NewsIntelligenceReport,
    PortfolioDecision,
    Position,
    TechAnalysisResult,
    TechReasoningChain,
)


def _analysis(symbol: str, rating: str = "buy") -> TechAnalysisResult:
    buy = rating in {"buy", "strong_buy"}
    return TechAnalysisResult(
        symbol=symbol,
        rating=rating,
        conviction="medium",
        entry_price=100,
        stop_loss=95 if buy else 105,
        reference_target=112 if buy else 88,
        support_levels=[95] if buy else [88],
        resistance_levels=[112] if buy else [105],
        setup_type="range",
        expected_horizon_sessions=10,
        reasoning="validated production-like trend and momentum evidence",
        reasoning_chain=TechReasoningChain(
            trend="daily trend",
            momentum="momentum",
            volatility="ATR",
            volume="volume",
            support_resistance="levels",
        ),
        thesis_invalid_if="closes below support",
    )


def _decision(target: dict, conflicts: str = "Explicit source audit.") -> PortfolioDecision:
    return PortfolioDecision.model_validate(
        {
            "reasoning_chain": {
                "macro_filter": "Macro checked.",
                "news_check": "News checked.",
                "earnings_check": "Earnings checked.",
                "signal_conflicts": conflicts,
                "sizing_logic": "Sizing checked.",
                "portfolio_balance": "Book checked.",
                "cash_target": "Cash checked.",
            },
            "targets": [target],
            "portfolio_view": "Grounded target only.",
        }
    )


def test_pm_allows_explicit_disagreement_but_rejects_false_alignment():
    target = {
        "symbol": "AAPL",
        "target_weight_pct": 5,
        "conviction": "medium",
        "thesis": "PM disagrees with the bearish technical signal.",
        "provenance": [
            {
                "source": "technical",
                "observed_stance": "sell",
                "relationship": "conflicts",
                "evidence": "Catalyst outweighs trend",
            }
        ],
    }
    errors = PortfolioManagerAgent.validate_grounding(
        _decision(target),
        analyses=[_analysis("AAPL", "sell")],
        positions=[],
        news_intel=None,
        earnings_analyses=[],
        macro_analysis=None,
        total_value=100_000,
    )
    assert errors == []

    target["provenance"][0]["relationship"] = "supports"
    errors = PortfolioManagerAgent.validate_grounding(
        _decision(target),
        analyses=[_analysis("AAPL", "sell")],
        positions=[],
        news_intel=None,
        earnings_analyses=[],
        macro_analysis=None,
        total_value=100_000,
    )
    assert any("does not support" in error for error in errors)


def test_pm_grounding_allows_transient_symbol_only_when_allowlisted_and_analyzed():
    target = {
        "symbol": "VST",
        "target_weight_pct": 5,
        "conviction": "medium",
        "thesis": "Temporary SEC admission still has validated Technical support.",
        "provenance": [
            {
                "source": "technical",
                "observed_stance": "buy",
                "relationship": "supports",
                "evidence": "current-run trend",
            }
        ],
    }
    kwargs = dict(
        analyses=[_analysis("VST")],
        positions=[],
        news_intel=None,
        earnings_analyses=[],
        macro_analysis=None,
        total_value=100_000,
    )
    assert (
        PortfolioManagerAgent.validate_grounding(
            _decision(target),
            allowed_buy_symbols={"AAPL", "VST"},
            **kwargs,
        )
        == []
    )

    errors = PortfolioManagerAgent.validate_grounding(
        _decision(target),
        allowed_buy_symbols={"AAPL"},
        **kwargs,
    )
    assert any("temporary-admission allowlist" in error for error in errors)


def test_pm_grounding_rejects_allowlisted_increase_without_current_technical():
    target = {
        "symbol": "VST",
        "target_weight_pct": 5,
        "conviction": "medium",
        "thesis": "Smart Money alone cannot open the position.",
        "provenance": [
            {
                "source": "smart_money",
                "observed_stance": "bullish",
                "relationship": "supports",
                "evidence": "material purchase",
            }
        ],
    }
    from src.models import SmartMoneyFinding, SmartMoneyObservation

    observation = SmartMoneyObservation(
        symbol="VST",
        actor="Example Insider",
        direction="buy",
        transaction_date=date.today(),
        disclosure_date=date.today(),
        source_url="https://www.sec.gov/example",
        lag_days=0,
        disclosure_age_days=0,
        freshness="fresh",
        economic_role="confirmatory",
    )
    finding = SmartMoneyFinding(
        symbol="VST",
        stance="bullish",
        economic_role="confirmatory",
        summary="material purchase",
        why_now="new SEC filing",
        observations=[observation],
    )
    errors = PortfolioManagerAgent.validate_grounding(
        _decision(target),
        analyses=[],
        positions=[],
        news_intel=None,
        earnings_analyses=[],
        macro_analysis=None,
        smart_money_findings=[finding],
        total_value=100_000,
        allowed_buy_symbols={"VST"},
    )
    assert any("lacks a current-run Technical analysis" in error for error in errors)


def test_pm_rejects_invented_coverage_phantom_exit_and_unproved_ratio():
    target = {
        "symbol": "MSFT",
        "target_weight_pct": 0,
        "conviction": "low",
        "thesis": "Close because 4/4 signals aligned.",
        "provenance": [
            {
                "source": "news",
                "observed_stance": "bearish",
                "relationship": "supports",
                "evidence": "claimed headline",
            }
        ],
    }
    errors = PortfolioManagerAgent.validate_grounding(
        _decision(target, "MSFT: 4/4 aligned."),
        analyses=[_analysis("MSFT")],
        positions=[],
        news_intel=None,
        earnings_analyses=[],
        macro_analysis=None,
        total_value=100_000,
    )
    assert any("not an actual holding" in error for error in errors)
    assert any("coverage that does not exist" in error for error in errors)
    assert any("claims denominator 4" in error for error in errors)


def test_pm_cannot_bypass_grounding_with_legacy_concrete_decisions():
    decision = PortfolioDecision.model_validate(
        {
            "reasoning_chain": {
                "macro_filter": "m",
                "news_check": "n",
                "earnings_check": "e",
                "signal_conflicts": "s",
                "sizing_logic": "z",
                "portfolio_balance": "b",
                "cash_target": "c",
            },
            "decisions": [
                {
                    "action": "BUY",
                    "symbol": "AAPL",
                    "allocation_pct": 5,
                    "entry_price": 100,
                    "stop_loss": 95,
                    "take_profit": 110,
                    "reasoning": "bypass targets",
                }
            ],
            "portfolio_view": "legacy bypass",
        }
    )
    errors = PortfolioManagerAgent.validate_grounding(
        decision,
        analyses=[_analysis("AAPL")],
        positions=[],
        news_intel=None,
        earnings_analyses=[],
        macro_analysis=None,
        total_value=100_000,
    )
    assert any("only grounded targets" in error for error in errors)


def test_pm_free_text_does_not_override_structured_provenance():
    target = {
        "symbol": "AAPL",
        "target_weight_pct": 5,
        "conviction": "medium",
        "thesis": "Technical setup supports the target.",
        "provenance": [
            {
                "source": "technical",
                "observed_stance": "buy",
                "relationship": "supports",
                "evidence": "validated buy rating",
            }
        ],
    }
    errors = PortfolioManagerAgent.validate_grounding(
        _decision(target, "AAPL: technical buy and news bullish."),
        analyses=[_analysis("AAPL")],
        positions=[],
        news_intel=None,
        earnings_analyses=[],
        macro_analysis=None,
        total_value=100_000,
    )
    # Free-form prose is not machine interpreted.  The exact structured
    # provenance is the enforceable boundary and remains grounded.
    assert errors == []


def test_pm_may_truthfully_state_that_symbol_coverage_is_absent():
    target = {
        "symbol": "AAPL",
        "target_weight_pct": 5,
        "conviction": "medium",
        "thesis": "Technical setup supports the target; no news/earnings available.",
        "provenance": [
            {
                "source": "technical",
                "observed_stance": "buy",
                "relationship": "supports",
                "evidence": "validated buy rating",
            }
        ],
    }
    errors = PortfolioManagerAgent.validate_grounding(
        _decision(target, "AAPL: tech=buy, news=n/a, earnings=unavailable."),
        analyses=[_analysis("AAPL")],
        positions=[],
        news_intel=None,
        earnings_analyses=[],
        macro_analysis=None,
        total_value=100_000,
    )
    assert errors == []


def test_queued_earnings_is_absent_evidence_not_directional_none():
    """Production run-76bd4e83: ``None`` must not become stance ``'none'``.

    A just-filed placeholder has no completed earnings analysis. It stays in
    the human-readable prompt as unavailable context, but must not enter the
    authoritative provenance registry or force a false directional label.
    """
    analyses = [_analysis("AMR", "strong_buy")]
    queued_earnings = [
        {
            "symbol": "AMR",
            "queued": True,
            "form_type": "10-Q",
            "filing_date": "2026-08-07",
            "analysis": None,
        }
    ]

    assert PortfolioManagerAgent._collapse_stances([None]) is None
    assert PortfolioManagerAgent._collapse_stances(["unavailable"]) is None

    registry = PortfolioManagerAgent.build_evidence_registry(
        analyses=analyses,
        positions=[],
        news_intel=None,
        earnings_analyses=queued_earnings,
        macro_analysis=None,
    )
    assert registry == {"AMR": {"technical": "strong_buy"}}

    target = {
        "symbol": "AMR",
        "target_weight_pct": 5,
        "conviction": "medium",
        "thesis": "Current Technical strength supports a bounded starter.",
        "provenance": [
            {
                "source": "technical",
                "observed_stance": "strong_buy",
                "relationship": "supports",
                "evidence": "current-run trend",
            }
        ],
    }
    assert (
        PortfolioManagerAgent.validate_grounding(
            _decision(target),
            analyses=analyses,
            positions=[],
            news_intel=None,
            earnings_analyses=queued_earnings,
            macro_analysis=None,
            total_value=100_000,
            allowed_buy_symbols={"AMR"},
        )
        == []
    )

    target["provenance"].append(
        {
            "source": "earnings",
            "observed_stance": "none",
            "relationship": "context",
            "evidence": "not analyzed yet",
        }
    )
    errors = PortfolioManagerAgent.validate_grounding(
        _decision(target),
        analyses=analyses,
        positions=[],
        news_intel=None,
        earnings_analyses=queued_earnings,
        macro_analysis=None,
        total_value=100_000,
        allowed_buy_symbols={"AMR"},
    )
    assert any("earnings coverage that does not exist" in error for error in errors)


def test_grounding_failure_returns_original_result_without_another_llm_call(monkeypatch):
    base = {
        "reasoning_chain": {
            "macro_filter": "m",
            "news_check": "n",
            "earnings_check": "e",
            "signal_conflicts": "AAPL: technical buy and news bullish.",
            "sizing_logic": "z",
            "portfolio_balance": "b",
            "cash_target": "c",
        },
        "targets": [
            {
                "symbol": "AAPL",
                "target_weight_pct": 5,
                "conviction": "medium",
                "thesis": "Technical support is claimed, with a phantom news claim.",
                "provenance": [
                    {
                        "source": "news",
                        "observed_stance": "bullish",
                        "relationship": "supports",
                        "evidence": "phantom news",
                    }
                ],
            }
        ],
        "portfolio_view": "Selective long.",
    }
    agent = PortfolioManagerAgent.__new__(PortfolioManagerAgent)
    first = AgentResult(raw_text=json.dumps(base), tokens_used=1, model="test", user_message="input")
    monkeypatch.setattr(PortfolioManagerAgent, "run", lambda self, **kwargs: first)
    execute_calls = []
    monkeypatch.setattr(
        PortfolioManagerAgent,
        "_execute",
        lambda self, message: execute_calls.append(message),
    )
    decision, result = agent.decide(analyses=[_analysis("AAPL")], positions=[])
    assert decision is None
    assert result is first
    assert execute_calls == []


def test_production_scale_pm_prompt_and_grounding_contract():
    """Observed production scale: 30 candidates, 15 holdings, memory layers."""
    symbols = [
        "SPY",
        "QQQ",
        "IWM",
        "AAPL",
        "MSFT",
        "GOOGL",
        "AMZN",
        "NVDA",
        "META",
        "AVGO",
        "AMD",
        "ORCL",
        "MU",
        "JPM",
        "GS",
        "V",
        "MA",
        "UNH",
        "LLY",
        "XOM",
        "CVX",
        "COST",
        "WMT",
        "CAT",
        "GE",
        "BA",
        "NEE",
        "VST",
        "CEG",
        "BRK-B",
    ]
    analyses = [_analysis(symbol) for symbol in symbols]
    positions = [
        Position(
            symbol=symbol,
            qty=10,
            avg_entry=90,
            current_price=100,
            market_value=1000,
            unrealized_pnl=100,
            sector="Diversified",
        )
        for symbol in symbols[:15]
    ]
    with patch("anthropic.Anthropic"):
        agent = PortfolioManagerAgent(api_key="test", model="test")
        message = agent.build_user_message(
            analyses=analyses,
            positions=positions,
            macro_analysis={"regime": "risk_on", "equity_outlook": "bullish"},
            cash_balance=50_000,
            total_value=100_000,
            weekly_narrative="Seven-day portfolio narrative. " * 80,
            macro_trajectory="Regime trajectory evidence. " * 80,
            active_state_changes="Current state change. " * 80,
        )
    assert len(message) > 18_000
    assert all(f"- {symbol}:" in message for symbol in symbols)

    target = {
        "symbol": "BRK-B",
        "target_weight_pct": 5,
        "conviction": "medium",
        "thesis": "Technical buy supports a bounded starter.",
        "provenance": [
            {
                "source": "technical",
                "observed_stance": "buy",
                "relationship": "supports",
                "evidence": "validated buy rating",
            }
        ],
    }
    assert (
        PortfolioManagerAgent.validate_grounding(
            _decision(target),
            analyses=analyses,
            positions=positions,
            news_intel=None,
            earnings_analyses=[],
            macro_analysis={"regime": "risk_on", "equity_outlook": "bullish"},
            total_value=100_000,
        )
        == []
    )


def test_pm_rejects_uncorrelated_smart_money_as_support():
    """2026-09-11 owner redesign: smart-money support is no longer gated by
    age -- it is gated by whether it CORRELATES with at least one other
    CURRENT source. This finding is a real, structurally eligible bullish
    insider buy, but nothing else in this analysis (bearish technical,
    nothing else covering the symbol) currently agrees with it -- an
    island, not a fake calendar cutoff -- so it still cannot support the
    target on its own."""
    from src.models import SmartMoneyFinding, SmartMoneyObservation

    finding = SmartMoneyFinding(
        symbol="AAPL",
        stance="bullish",
        economic_role="actionable",
        summary="one disclosed purchase",
        why_now="new disclosure",
        observations=[
            SmartMoneyObservation(
                symbol="AAPL",
                stream="insider",
                actor="Example Member",
                direction="buy",
                transaction_date=date(2026, 6, 1),
                disclosure_date=date(2026, 7, 11),
                source_url="https://example.test/filing",
                lag_days=40,
                disclosure_age_days=20,
                freshness="stale",
                economic_role="confirmatory",
            )
        ],
    )
    target = {
        "symbol": "AAPL",
        "target_weight_pct": 5,
        "conviction": "medium",
        "thesis": "Insider buy, but nothing else currently agrees.",
        "provenance": [
            {"source": "technical", "observed_stance": "sell", "relationship": "conflicts", "evidence": "downtrend"},
            {
                "source": "smart_money",
                "observed_stance": "bullish",
                "relationship": "supports",
                "evidence": "disclosure",
            },
        ],
    }
    errors = PortfolioManagerAgent.validate_grounding(
        _decision(target),
        analyses=[_analysis("AAPL", "sell")],
        positions=[],
        news_intel=None,
        earnings_analyses=[],
        macro_analysis=None,
        smart_money_findings=[finding],
        total_value=100_000,
    )
    assert any("nothing else currently corroborating it cannot support" in error for error in errors)


def test_pm_accepts_aged_smart_money_as_support_when_it_correlates():
    """The other half of the same redesign: this insider buy is JUST AS OLD
    as the one rejected above (disclosure_age_days=20, freshness=stale) --
    but here a current technical read independently agrees with the same
    bullish direction. Correlation, not the calendar, is what makes it
    count now."""
    from src.models import SmartMoneyFinding, SmartMoneyObservation

    finding = SmartMoneyFinding(
        symbol="AAPL",
        stance="bullish",
        economic_role="actionable",
        summary="one disclosed purchase",
        why_now="new disclosure",
        observations=[
            SmartMoneyObservation(
                symbol="AAPL",
                stream="insider",
                actor="Example Member",
                direction="buy",
                transaction_date=date(2026, 6, 1),
                disclosure_date=date(2026, 7, 11),
                source_url="https://example.test/filing",
                lag_days=40,
                disclosure_age_days=20,
                freshness="stale",
                economic_role="confirmatory",
            )
        ],
    )
    target = {
        "symbol": "AAPL",
        "target_weight_pct": 5,
        "conviction": "medium",
        "thesis": "Technical trend agrees with the older insider buy.",
        "provenance": [
            {"source": "technical", "observed_stance": "buy", "relationship": "supports", "evidence": "trend"},
            {
                "source": "smart_money",
                "observed_stance": "bullish",
                "relationship": "supports",
                "evidence": "disclosure",
            },
        ],
    }
    errors = PortfolioManagerAgent.validate_grounding(
        _decision(target),
        analyses=[_analysis("AAPL", "buy")],
        positions=[],
        news_intel=None,
        earnings_analyses=[],
        macro_analysis=None,
        smart_money_findings=[finding],
        total_value=100_000,
    )
    assert not any("smart-money" in error for error in errors)


# ==========================================================================
# Stage 3 (shorts) — a short target is grounded on the SAME contract as a
# long, not exempted from it and not made impossible by it. Before this fix
# `validate_grounding` classified every non-close risk-based target as a
# BUY regardless of `direction`, so a short's correct BEARISH provenance
# marked `supports` was scored as "does not support the proposed buy" and
# unconditionally rejected — a short could never pass grounding no matter
# how well-evidenced.
# ==========================================================================


def test_pm_short_target_with_bearish_provenance_survives_grounding():
    target = {
        "symbol": "TSLA",
        "direction": "short",
        "risk_allocation_pct": 2.0,
        "conviction": "high",
        "thesis": "Breaking down below multi-month base; Tech strong_sell confirms.",
        "provenance": [
            {
                "source": "technical",
                "observed_stance": "strong_sell",
                "relationship": "supports",
                "evidence": "confirmed breakdown, stop above prior support",
            }
        ],
    }
    errors = PortfolioManagerAgent.validate_grounding(
        _decision(target),
        analyses=[_analysis("TSLA", "strong_sell")],
        positions=[],
        news_intel=None,
        earnings_analyses=[],
        macro_analysis=None,
        total_value=100_000,
    )
    assert errors == []


def test_pm_short_target_with_bullish_provenance_marked_supports_fails_grounding():
    """The mirror-image failure: a short target claiming a BULLISH stance
    'supports' it must be rejected — a short needs bearish confirmation,
    not bullish, so this is not a case of the fix being too permissive."""
    target = {
        "symbol": "TSLA",
        "direction": "short",
        "risk_allocation_pct": 2.0,
        "conviction": "high",
        "thesis": "Shorting despite an uptrend because I feel like it.",
        "provenance": [
            {
                "source": "technical",
                "observed_stance": "buy",
                "relationship": "supports",
                "evidence": "misapplied bullish trend",
            }
        ],
    }
    errors = PortfolioManagerAgent.validate_grounding(
        _decision(target),
        analyses=[_analysis("TSLA", "buy")],
        positions=[],
        news_intel=None,
        earnings_analyses=[],
        macro_analysis=None,
        total_value=100_000,
    )
    assert any("does not support" in error for error in errors)


def test_pm_short_target_without_current_technical_analysis_is_rejected():
    """A short is held to the SAME evidence requirement a long is — it must
    not be exempt from needing a current-run Technical analysis just
    because it opens exposure on the other side."""
    target = {
        "symbol": "TSLA",
        "direction": "short",
        "risk_allocation_pct": 2.0,
        "conviction": "high",
        "thesis": "Shorting on a stale prior read with no current Tech coverage.",
        "provenance": [
            {
                "source": "technical",
                "observed_stance": "strong_sell",
                "relationship": "supports",
                "evidence": "no current-run analysis actually backs this",
            }
        ],
    }
    errors = PortfolioManagerAgent.validate_grounding(
        _decision(target),
        analyses=[],
        positions=[],  # no current-run Tech at all
        news_intel=None,
        earnings_analyses=[],
        macro_analysis=None,
        total_value=100_000,
    )
    assert any("lacks a current-run Technical analysis" in error for error in errors)


def test_pm_short_target_outside_universe_is_rejected():
    """A short opening exposure is subject to the same universe/allowlist
    gate an opening BUY is — it is not a quieter way around it."""
    target = {
        "symbol": "ZZZZ",
        "direction": "short",
        "risk_allocation_pct": 2.0,
        "conviction": "high",
        "thesis": "Shorting a name outside the configured universe.",
        "provenance": [
            {
                "source": "technical",
                "observed_stance": "strong_sell",
                "relationship": "supports",
                "evidence": "breakdown",
            }
        ],
    }
    errors = PortfolioManagerAgent.validate_grounding(
        _decision(target),
        analyses=[_analysis("ZZZZ", "strong_sell")],
        positions=[],
        news_intel=None,
        earnings_analyses=[],
        macro_analysis=None,
        total_value=100_000,
        allowed_buy_symbols={"AAPL", "MSFT"},
    )
    assert any("outside the configured universe" in error for error in errors)


# --------------------------------------------------------------------------
# 2026-09-17 incident (intra_check-44594a05): a risk-based TRIM of a held
# name with no current-run Technical analysis was classified as an increase
# and failed grounding, which rejected the whole plan — including a valid,
# fully grounded new entry.
# --------------------------------------------------------------------------


def _held_long(symbol: str) -> Position:
    return Position(
        symbol=symbol,
        qty=100.0,
        avg_entry=230.0,
        current_price=229.0,
        market_value=22_900.0,
        unrealized_pnl=-100.0,
        sector="Technology",
    )


def _trim_and_open_targets() -> list[dict]:
    return [
        {
            # Funding trim: AAPL from 1.91% equity at risk down to 1.0%.
            "symbol": "AAPL",
            "risk_allocation_pct": 1.0,
            "conviction": "medium",
            "thesis": "Trim AAPL toward 1.0% risk to fund NET.",
            "provenance": [
                {
                    "source": "macro",
                    "observed_stance": "bullish",
                    "relationship": "context",
                    "evidence": "macro backdrop",
                }
            ],
        },
        {
            "symbol": "NET",
            "risk_allocation_pct": 1.75,
            "conviction": "high",
            "thesis": "Open NET on the current-run strong buy.",
            "provenance": [
                {
                    "source": "technical",
                    "observed_stance": "strong_buy",
                    "relationship": "supports",
                    "evidence": "current-run scan",
                },
                {
                    "source": "macro",
                    "observed_stance": "bullish",
                    "relationship": "supports",
                    "evidence": "macro backdrop",
                },
            ],
        },
    ]


@patch("anthropic.Anthropic")
def test_risk_trim_of_unanalysed_holding_does_not_reject_the_plan(mock_cls):
    from unittest.mock import MagicMock

    response_text = json.dumps(
        {
            "reasoning_chain": {
                "macro_filter": "checked",
                "news_check": "checked",
                "earnings_check": "checked",
                "signal_conflicts": "none",
                "sizing_logic": "checked",
                "portfolio_balance": "checked",
                "cash_target": "checked",
            },
            "targets": _trim_and_open_targets(),
            "portfolio_view": "Open NET, fund it by trimming AAPL.",
        }
    )
    mock_client = MagicMock()
    mock_response = MagicMock()
    mock_response.content = [MagicMock(text=response_text)]
    mock_response.usage.input_tokens = 500
    mock_response.usage.output_tokens = 200
    mock_client.messages.create.return_value = mock_response
    mock_cls.return_value = mock_client

    agent = PortfolioManagerAgent(api_key="test", model="claude-opus-4-6-20250725")
    decision, result = agent.decide(
        analyses=[_analysis("NET", "strong_buy")],  # intraday: movers only
        positions=[_held_long("AAPL")],
        macro_analysis={"equity_outlook": "bullish"},
        cash_balance=50_000,
        total_value=100_000,
        allowed_buy_symbols={"AAPL", "NET"},
        existing_risk_pct={"AAPL": 1.91},
    )
    assert decision is not None, f"decide() failed closed: {result.semantic_error}"
    assert {t.symbol for t in decision.targets} == {"AAPL", "NET"}


def _single_target_errors(target: dict, analyses: list) -> list[str]:
    decision = PortfolioDecision.model_validate(
        {
            "reasoning_chain": {
                "macro_filter": "m",
                "news_check": "n",
                "earnings_check": "e",
                "signal_conflicts": "s",
                "sizing_logic": "z",
                "portfolio_balance": "b",
                "cash_target": "c",
            },
            "targets": [target],
            "portfolio_view": "polarity",
        }
    )
    return PortfolioManagerAgent.validate_grounding(
        decision,
        analyses=analyses,
        positions=[_held_long("AAPL")],
        news_intel=None,
        earnings_analyses=[_earnings("AAPL", "bullish"), _earnings("NET", "bearish")],
        macro_analysis=None,
        total_value=100_000,
        existing_risk_pct={"AAPL": 1.91},
    )


def test_bullish_support_still_cannot_justify_a_full_close():
    target = dict(_real_intra_check_targets()[0], risk_allocation_pct=0.0)
    target["provenance"] = target["provenance"][:1]
    errors = _single_target_errors(target, analyses=[])
    assert any("does not support the proposed sell" in e for e in errors)


def test_bearish_support_still_cannot_justify_an_open():
    target = {
        "symbol": "NET",
        "direction": "long",
        "risk_allocation_pct": 1.75,
        "conviction": "high",
        "thesis": "Open NET.",
        "provenance": [
            {"source": "earnings", "observed_stance": "bearish", "relationship": "supports", "evidence": "bearish"}
        ],
    }
    errors = _single_target_errors(target, analyses=[_analysis("NET", "strong_buy")])
    assert any("does not support the proposed buy" in e for e in errors)


def _risk_target(symbol: str, risk: float, direction: str = "long"):
    from src.models import TargetPosition

    return TargetPosition(
        symbol=symbol,
        risk_allocation_pct=risk,
        direction=direction,
        thesis="risk target",
    )


def test_risk_target_below_current_risk_is_a_trim():
    """Keep-vs-add is `_target_intent` reading current stop-based risk
    (shipped #436). Do not reinvent a second classifier."""
    held = {"AAPL": _held_long("AAPL")}
    assert (
        PortfolioManagerAgent._target_intent(
            _risk_target("AAPL", 1.0),
            held,
            100_000,
            existing_risk_pct={"AAPL": 1.91},
        )
        == "sell"
    )


def test_risk_target_at_or_above_current_risk_stays_an_increase():
    held = {"AAPL": _held_long("AAPL")}
    for risk in (1.91, 2.5):
        assert (
            PortfolioManagerAgent._target_intent(
                _risk_target("AAPL", risk),
                held,
                100_000,
                existing_risk_pct={"AAPL": 1.91},
            )
            == "buy"
        )


def test_risk_target_fails_safe_when_current_risk_is_unknown():
    held = {"AAPL": _held_long("AAPL")}
    # No risk map at all, or no entry for this holding: today's strict rule.
    assert (
        PortfolioManagerAgent._target_intent(
            _risk_target("AAPL", 1.0),
            held,
            100_000,
            existing_risk_pct=None,
        )
        == "buy"
    )
    assert (
        PortfolioManagerAgent._target_intent(
            _risk_target("AAPL", 1.0),
            held,
            100_000,
            existing_risk_pct={"MSFT": 3.0},
        )
        == "buy"
    )


def test_risk_target_is_never_a_trim_for_a_name_not_held_or_held_other_side():
    # Not held: a lower number in a stale map must not turn an open into a trim.
    assert (
        PortfolioManagerAgent._target_intent(
            _risk_target("NET", 1.0),
            {},
            100_000,
            existing_risk_pct={"NET": 2.0},
        )
        == "buy"
    )
    # Held long, target short: a side flip is an opening, never a trim.
    held = {"AAPL": _held_long("AAPL")}
    assert (
        PortfolioManagerAgent._target_intent(
            _risk_target("AAPL", 1.0, "short"),
            held,
            100_000,
            existing_risk_pct={"AAPL": 1.91},
        )
        == "short"
    )


def test_genuine_increase_on_unanalysed_holding_still_rejected():
    """Fail-closed remainder: if this run still has no Technical for a
    hold, an increase is refused. Dropping the name is not the product —
    the scan must produce Tech. This pins the gate that remains when
    production still failed."""
    decision = PortfolioDecision.model_validate(
        {
            "reasoning_chain": {
                "macro_filter": "m",
                "news_check": "n",
                "earnings_check": "e",
                "signal_conflicts": "s",
                "sizing_logic": "z",
                "portfolio_balance": "b",
                "cash_target": "c",
            },
            "targets": [dict(_trim_and_open_targets()[0], risk_allocation_pct=2.5)],
            "portfolio_view": "increase",
        }
    )
    errors = PortfolioManagerAgent.validate_grounding(
        decision,
        analyses=[],
        positions=[_held_long("AAPL")],
        news_intel=None,
        earnings_analyses=[],
        macro_analysis={"equity_outlook": "bullish"},
        total_value=100_000,
        existing_risk_pct={"AAPL": 1.91},
    )
    assert any("lacks a current-run Technical analysis" in e for e in errors)


def test_increase_on_held_name_grounds_once_current_run_tech_exists():
    """Keep-vs-add is `_target_intent` (current risk, #436) — a request at
    or above current risk is an increase. Once Technical is in this run's
    analyses, that increase grounds. Producing Tech is the product."""
    target = dict(_trim_and_open_targets()[0], risk_allocation_pct=2.5)
    target["provenance"] = [
        {
            "source": "technical",
            "observed_stance": "buy",
            "relationship": "supports",
            "evidence": "current-run chart",
        }
    ]
    decision = PortfolioDecision.model_validate(
        {
            "reasoning_chain": {
                "macro_filter": "m",
                "news_check": "n",
                "earnings_check": "e",
                "signal_conflicts": "s",
                "sizing_logic": "z",
                "portfolio_balance": "b",
                "cash_target": "c",
            },
            "targets": [target],
            "portfolio_view": "increase a hold that now has Tech",
        }
    )
    errors = PortfolioManagerAgent.validate_grounding(
        decision,
        analyses=[_analysis("AAPL", "buy")],
        positions=[_held_long("AAPL")],
        news_intel=None,
        earnings_analyses=[],
        macro_analysis=None,
        total_value=100_000,
        existing_risk_pct={"AAPL": 1.91},
    )
    assert errors == []


# The REAL plan from intra_check-44594a05, stances as recorded in the PM
# output: a concentration trim of a bullish holding, tagged bullish
# "supports", alongside a grounded new entry.


def _earnings(symbol: str, sentiment: str) -> dict:
    return {
        "symbol": symbol,
        "filing_date": date.today().isoformat(),
        "analysis": {"investment_implications": {"sentiment": sentiment}},
    }


def _real_intra_check_targets() -> list[dict]:
    return [
        {
            "symbol": "AAPL",
            "direction": "long",
            "risk_allocation_pct": 1.0,
            "conviction": "medium",
            "thesis": "Trim AAPL toward 1.0% risk for concentration; fund NET.",
            "provenance": [
                {
                    "source": "earnings",
                    "observed_stance": "bullish",
                    "relationship": "supports",
                    "evidence": "earnings still bullish",
                },
                {
                    "source": "macro",
                    "observed_stance": "bullish",
                    "relationship": "context",
                    "evidence": "macro backdrop",
                },
            ],
        },
        {
            "symbol": "NET",
            "direction": "long",
            "risk_allocation_pct": 1.75,
            "conviction": "high",
            "thesis": "Open NET on the current-run strong buy.",
            "provenance": [
                {
                    "source": "technical",
                    "observed_stance": "strong_buy",
                    "relationship": "supports",
                    "evidence": "current-run scan",
                },
                {
                    "source": "earnings",
                    "observed_stance": "neutral",
                    "relationship": "context",
                    "evidence": "earnings neutral",
                },
                {
                    "source": "macro",
                    "observed_stance": "bullish",
                    "relationship": "context",
                    "evidence": "macro backdrop",
                },
            ],
        },
    ]
