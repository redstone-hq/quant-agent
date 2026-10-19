"""Names the candidate ranking already blocked carry the BLOCK's own reason.

19 Oct 2026 defect: 41 names the chart seat rated neutral (ranking rule R2)
were shown to the owner as passed for "no level on the chart", because the
PM was asked to restate a reason for every analysed name and picked the
wrong code. Blocked names are now recorded mechanically from the block.
"""

from __future__ import annotations

from src.models import CandidateRejection
from src.pm_accounting import (
    OUTCOME_NOT_SELECTED,
    REASON_RANKING_BLOCKED,
    REASON_REJECTED,
    account_for_candidates,
    plain_reason,
)
from tests.test_pm_candidate_accounting import _analysis, _decision


def _by_symbol(result):
    return {a.symbol: a for a in result.accounted}


def test_neutral_r2_blocked_name_gets_the_r2_reason_not_the_pms_wrong_code():
    wrong = CandidateRejection.model_validate(
        {"symbol": "ZZZA", "code": "no_readable_structure", "detail": "technical neutral"},
    )
    result = account_for_candidates(
        analyses=[_analysis("ZZZA")],
        decision=_decision([wrong]),
        positions=[],
        blocked={"ZZZA": ["R2 neutral rating"]},
    )
    row = _by_symbol(result)["ZZZA"]
    assert result.unaccounted == []
    assert row.outcome == OUTCOME_NOT_SELECTED
    assert row.reason == REASON_RANKING_BLOCKED
    assert row.code == "ranking_r2"
    assert "R2 neutral rating" in row.note
    assert "chart reading was neutral" in plain_reason(row.code)
    assert "no level" not in plain_reason(row.code)


def test_blocked_name_needs_no_pm_reason_so_no_reask():
    result = account_for_candidates(
        analyses=[_analysis("ZZZB")],
        decision=_decision(),
        positions=[],
        blocked={"ZZZB": ["R2 neutral rating"]},
    )
    assert result.unaccounted == []
    assert _by_symbol(result)["ZZZB"].code == "ranking_r2"


def test_unblocked_names_still_use_the_pms_own_pass_reason():
    stated = CandidateRejection.model_validate(
        {"symbol": "ZZZC", "code": "event_risk", "detail": "earnings tomorrow"},
    )
    result = account_for_candidates(
        analyses=[_analysis("ZZZC"), _analysis("ZZZD")],
        decision=_decision([stated]),
        positions=[],
        blocked={"ZZZD": ["R3 not BUY-eligible"]},
    )
    rows = _by_symbol(result)
    assert rows["ZZZC"].reason == REASON_REJECTED and rows["ZZZC"].code == "event_risk"
    assert rows["ZZZD"].code == "ranking_r3"


def test_every_ranking_rule_code_has_owner_wording():
    for code in ("ranking_r2", "ranking_r3", "ranking_r6", "ranking_r7", "ranking_blocked"):
        assert "no plain wording" not in plain_reason(code)


def test_unknown_block_text_falls_back_to_generic_code():
    result = account_for_candidates(
        analyses=[_analysis("ZZZE")],
        decision=_decision(),
        positions=[],
        blocked={"ZZZE": ["something new"]},
    )
    assert _by_symbol(result)["ZZZE"].code == "ranking_blocked"
