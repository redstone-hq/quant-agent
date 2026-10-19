"""A held name is in the buy-eligible universe on every run (19 Oct 2026).

An insider-admitted name bought in the morning run was dropped from the
buy-eligible set on every later run, because only the morning run fills
today's admissions. It then failed the entry-eligibility rule ("R3 not
BUY-eligible") and was proposed for an automatic close ten minutes after it
was bought. The fix is where the universe is assembled: configured names,
today's admissions and every held name. The entry rule itself is unchanged.
No model is called."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from src.agents.portfolio_manager.candidate_ranking import CandidateRanking
from src.models import Position, TradeDecision
from src.pipeline_admission import AdmissionService
from src.universe_membership import buy_eligible_universe
from src.pipeline_stages import RunContext
from src.stage_decision import build_pm_decide_inputs
from tests.test_reviewer_same_rules_evidence import _assembled, _pipeline

HELD = "ZZZH"  # held, admitted on an earlier run, not configured
STRANGER = "ZZZX"  # not held, not admitted, not configured


def _held(symbol):
    return Position(
        symbol=symbol,
        qty=10.0,
        avg_entry=10.0,
        current_price=10.0,
        market_value=100.0,
        unrealized_pnl=0.0,
        sector="Basic Materials",
    )


def _later_run_ctx(session):
    ctx = RunContext.start(session)
    assert ctx.admitted_symbols == set()  # later runs start with no admissions
    ctx.positions = [_held(HELD)]
    ctx.total_value = 100_000.0
    ctx.last_equity = 100_000.0
    ctx.cash = 50_000.0
    ctx.deployable_cash = 50_000.0
    return ctx


def _allowed(session):
    pipeline = _pipeline(_assembled({}))
    pipeline.config.trading.universe = ["ZZZC"]
    return build_pm_decide_inputs(pipeline, _later_run_ctx(session)).pm_decide_kwargs["allowed_buy_symbols"]


def _eligibility(allowed):
    analyses = [SimpleNamespace(symbol=s, rating="strong_buy", risk_reward=None) for s in (HELD, STRANGER)]
    return CandidateRanking.candidate_eligibility(
        None,
        analyses=analyses,
        evidence_registry={},
        allowed_buy_symbols=allowed,
        active_state_changes="",
        held_directions={HELD: "long"},
    )


def test_held_unconfigured_unadmitted_name_is_eligible_on_intraday_and_midday_runs():
    for session in ("intraday", "midday"):
        allowed = _allowed(session)
        assert HELD in allowed
        assert "ZZZC" in allowed
        assert STRANGER not in allowed
        verdicts = _eligibility(allowed)
        assert "R3 not BUY-eligible" not in verdicts[HELD]
        # The entry rule still stands for a name that is none of the three.
        assert "R3 not BUY-eligible" in verdicts[STRANGER]


def test_symbol_guard_lets_a_held_name_be_added_to_and_still_refuses_a_stranger():
    svc = AdmissionService(config=MagicMock(), broker=None, market=None, journal=MagicMock())
    svc.config.trading.universe = ["ZZZC"]
    analyses = [SimpleNamespace(symbol=s) for s in (HELD, STRANGER)]
    decisions = [
        TradeDecision(
            symbol=s,
            action="BUY",
            quantity=1,
            reasoning="x",
            confidence=0.7,
            allocation_pct=1.0,
            entry_price=10.0,
            stop_loss=9.0,
            take_profit=12.0,
        )
        for s in (HELD, STRANGER)
    ]
    kept, blocked = svc._filter_supported_symbols(decisions, analyses, [_held(HELD)])
    assert [d.symbol for d in kept] == [HELD]
    assert any(STRANGER in reason for reason in blocked)


def test_one_assembly_point_unions_configured_admitted_and_held():
    got = buy_eligible_universe([" zzzc "], {"zzza"}, [_held(HELD), {"symbol": "zzzd"}])
    assert got == {"ZZZC", "ZZZA", HELD, "ZZZD"}
