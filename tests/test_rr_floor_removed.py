"""The 1.5 reward:risk floor is deleted, and the AI is no longer told names
passed it (owner 2026-10-15: made-up number, dead since 2026-09-17)."""

import inspect

import src.risk.constants as constants
from src.agents.portfolio_manager import PortfolioManagerAgent
from src.agents.portfolio_manager.candidate_ranking import CandidateRanking


def test_reward_risk_floor_constant_no_longer_exists():
    assert not hasattr(constants, "REWARD_RISK_FLOOR")


def test_no_rr_floor_parameter_survives_on_the_pm_entry_points():
    for fn in (
        PortfolioManagerAgent.decide,
        PortfolioManagerAgent.rank_candidates,
        CandidateRanking.candidate_eligibility,
    ):
        assert "rr_floor" not in inspect.signature(fn).parameters, fn


def test_candidate_text_sent_to_the_ai_does_not_claim_an_rr_floor_check():
    source = inspect.getsource(CandidateRanking._render_candidate_ranking)
    assert "R/R at or above" not in source
    assert "the floor" not in source
