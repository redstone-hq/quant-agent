"""Pins the docs/WORK.md item 18b measurement so it cannot rot silently.

No LLM call: everything here is the frozen run-64290730 fixture run through
the desk's own stated rules. If a rule, a config number or the fixture
changes, these fail and the write-up gets re-derived rather than believed.
"""

import re
from pathlib import Path

import pytest
import yaml

from ops.model_policy import scenarios as S
from ops.model_policy.deterministic_selection import (
    MAX_POSITION_RISK_PCT,
    CONVICTION_BANDS,
    CONVICTION_SCORE,
    RANKING_SIGNALS,
    catalyst_symbols,
    evaluate,
    rank_eligible,
    summarise,
)

# The level-less 2026-09-01 day this audit's numbers were measured on.
# `pm_selection` itself moved to run-bba4d4f3 on 2026-09-14; this file
# stays pinned to the day its write-up describes.
_LEVEL_LESS = S.load_frozen_selection(S.LEVEL_LESS_SELECTION_FIXTURE)

_SETTINGS = yaml.safe_load((Path(__file__).resolve().parent.parent / "config" / "settings.yaml").read_text())


@pytest.fixture(scope="module")
def rows():
    return evaluate(
        _LEVEL_LESS.raw,
        _LEVEL_LESS.analyses,
        _LEVEL_LESS.positions,
        _LEVEL_LESS.news,
    )


def test_constants_match_production_config():
    """The audit must gate on the desk's numbers, not a second opinion."""
    assert MAX_POSITION_RISK_PCT == float(_SETTINGS["risk"]["max_position_risk_pct"])
    # The agreement sizing ladder is retired (2026-09-14) — the audit must
    # not carry a private copy of one either.
    assert "agreement_ceiling_pct" not in _SETTINGS["risk"]


def test_conviction_bands_match_the_live_prompt():
    """The audit sized its replay off bands the desk stopped using.

    `CONVICTION_BANDS` said high 1.5-3.0 / medium 1.0-2.0 from before
    2026-09-10, while `config/prompts/portfolio_manager.md` has said
    2.0-4.0 / 1.0-2.5 since — so both the replay's `max_risk_pct` and the
    `CONVICTION_SCORE` ranking signal read off band tops that no longer
    existed. Found 2026-09-14. Parsing the sheet rather than pinning a
    second copy of the numbers is the only version of this that cannot rot:
    if the wording moves so these lines stop matching, this fails closed.
    """
    prompt = (Path(__file__).resolve().parent.parent / "config" / "prompts" / "portfolio_manager.md").read_text()
    pattern = r"^-\s+(High|Moderate|Low) conviction[^:]*:\s*([\d.]+)-([\d.]+)%"
    found = {
        {"High": "high", "Moderate": "medium", "Low": "low"}[m.group(1)]: (float(m.group(2)), float(m.group(3)))
        for m in re.finditer(pattern, prompt, re.M)
    }
    assert found == {"high": (2.0, 4.0), "medium": (1.0, 2.5), "low": (0.5, 1.0)}
    assert CONVICTION_BANDS == found


def test_catalyst_parsing_reads_only_the_arrow_list():
    text = (
        "- [2026-08-27] Nvidia revenue forecast of 70% growth → NVDA, SMH, AMD\n"
        "- [2026-08-31] Anthropic signs deal with Lambda → NVDA\n"
    )
    # "Nvidia" and "Anthropic" appear in prose and must NOT become catalysts.
    assert catalyst_symbols(text) == {"NVDA", "SMH", "AMD"} | {"NVDA"}


def test_rules_admit_twentyfive_names_and_rank_none_of_them(rows):
    """THE finding. The rules gate and ceiling; they never choose.

    **12 -> 25, 2026-09-11 (docs/WORK.md item 1(d)).** Removing the
    reward:risk floor from R4 more than doubles the eligible set on this
    exact real day — 13 of the 25 were being refused for a thin-but-real
    payoff alone. The finding itself is unchanged and, if anything, sharper:
    the rules now permit twenty-five names and still choose none of them."""
    summary = summarise(rows)
    assert summary["analysed"] == 59
    assert summary["eligible"] == 25
    assert summary["clears_rr_floor_alone"] == [
        "AAPL",
        "CHPX",
        "CMCSA",
        "COP",
        "CRM",
        "CVX",
        "DE",
        "DIS",
        "FLNC",
        "JNJ",
        "JPM",
        "KO",
        "MSFT",
        "MU",
        "NKE",
        "NUE",
        "NVDA",
        "PATH",
        "PFE",
        "RSG",
        "SLB",
        "TSM",
        "V",
        "VLO",
        "XLE",
    ]
    # The catalyst door is closed on this day: MSFT and TSM used to enter
    # through it on a sub-1.5 analyst ratio; with the 1.5 floor deleted
    # (2026-10-15) they clear alone like every other measurable name.
    assert summary["enters_via_catalyst_door"] == []
    # 47.24% against a 25% total-risk budget. NEW as of item 1(d) and worth
    # stating plainly: the eligible set no longer fits at once, so the
    # RISK BUDGET — not the reward:risk floor — is now what forces a choice
    # between permitted names. `allocate_risk_budget` rations it; the rule
    # set still names no single pick.
    #
    # 48.0 -> 47.24 on 2026-09-14, items 30/57: the agreement ceiling was
    # derived from the envelope rather than hand-typed, so its first rung
    # became 2.236% instead of 3.0%. It bound on very little — 0.76 points
    # across 25 names.
    #
    # 56.49 -> 60.0 on 2026-09-14: the graduated agreement ceiling is
    # RETIRED (owner decision). Measured on this exact real day, it had been
    # capping the theoretical maximum risk of 6 of the 25 eligible
    # candidates, worth 3.51 of 60.0 points in aggregate. That is a cap on
    # what the rules PERMIT, not on what the PM asked for: over the archived
    # sized targets of 2026-08-28..2026-09-02 the ladder capped ZERO real
    # requests, because every rung sat above every ask.
    #
    # 47.24 -> 56.49 on 2026-09-14: `CONVICTION_BANDS` was still the pre-
    # 2026-09-10 sheet (high 1.5-3.0 / medium 1.0-2.0) and is now the live one
    # (2.0-4.0 / 1.0-2.5). The eligible SET does not move — conviction only
    # sizes — and neither does the ranking below, because min-max
    # normalisation is unchanged by an affine rescale of the encoding. What
    # moves is how far past the budget the admitted names ask, which makes the
    # finding stronger, not different.
    assert summary["total_max_risk_pct"] == 64.0  # 60.0 -> 64.0 2026-10-15: MSFT, TSM no longer starter-capped
    assert summary["total_max_risk_pct"] > 25.0
    assert summary["rules_name_a_single_pick"] is False


def test_subfloor_catalyst_door_is_reachable_only_by_news_covered_names(rows):
    """Why the door skews famous: state-change rows are written about the
    names the wires cover, so the sub-floor exception is available to
    mega-caps and effectively nobody else."""
    by_door = {r["symbol"] for r in rows if r["eligible"] and r["subfloor_catalyst"]}
    # **Narrowed 2026-09-11 (item 1(d)).** The door is only reachable at all
    # now by a name whose payoff is UNMEASURABLE — a thin-but-real one walks
    # in the front. On this day that leaves MSFT and TSM; NVDA, the name this
    # whole line of work was written about, no longer needs the door.
    assert by_door == set()
    # Both mega-caps the benchmark's `familiarity_bias` check USED to
    # penalise are ADMITTED by the desk's own rules — which is why that check
    # is a weight-0 diagnostic as of 2026-09-14 rather than a pass mark.
    for symbol in ("NVDA", "MSFT"):
        row = next(r for r in rows if r["symbol"] == symbol)
        assert row["eligible"] is True
    assert (
        next(r for r in rows if r["symbol"] == "MSFT")["max_risk_pct"] == 2.5
    )  # was 0.5 via the deleted catalyst starter cap


def test_three_of_the_five_qualified_shorts_are_refused_by_the_net_rule(rows):
    """GEV/UNH/NEE clear the retired 1.5 floor and are still refused
    deterministically: the §9.4 signed score nets a bullish earnings stance
    off the bearish technical one. The benchmark USED to fault a model for
    passing them over — it credited picks production would never place. Fixed
    2026-09-14: `takes_an_eligible_short` counts only NKE and FLNC."""
    refused = {}
    for symbol in ("GEV", "UNH", "NEE"):
        row = next(r for r in rows if r["symbol"] == symbol)
        assert row["rr"] >= 1.5
        assert row["eligible"] is False
        assert any(b.startswith("R5") for b in row["blocked_by"]), row["blocked_by"]
        refused[symbol] = row["net_sources"]
    assert refused == {"GEV": -1, "UNH": 0, "NEE": 0}
    # NKE and FLNC survive, so `takes_an_eligible_short` remains satisfiable.
    for symbol in ("NKE", "FLNC"):
        assert next(r for r in rows if r["symbol"] == symbol)["eligible"] is True


def test_block_reason_census(rows):
    census = {}
    for row in rows:
        for reason in row["blocked_by"]:
            key = re.match(r"R\d", reason).group(0)
            census[key] = census.get(key, 0) + 1
    # R4 41 -> 20 (item 1(d)): 21 of the 41 R4 blocks on this day were the
    # reward:risk floor refusing a measurable payoff. What remains is the
    # unmeasurable half, which still fails closed.
    assert census == {"R2": 21, "R4": 20, "R5": 14}


# --------------------------------------------------------------------------
# The equal-weight composite ranking. NOT WIRED INTO PRODUCTION — these tests
# pin an audit artefact, not desk behaviour.
# --------------------------------------------------------------------------


def test_conviction_score_is_read_off_the_desks_own_bands():
    """No invented encoding: the ordinal comes from CONVICTION_BANDS."""
    assert CONVICTION_SCORE == {name: band[1] for name, band in CONVICTION_BANDS.items()}
    # The live sheet's band tops as of 2026-09-10, not a second copy of them:
    # `test_conviction_bands_match_the_live_prompt` above is what ties these
    # to the prompt, and this only asserts the encoding is the band tops.
    assert CONVICTION_SCORE == {"high": 4.0, "medium": 2.5, "low": 1.0}


def test_ranking_uses_three_independent_signals_at_equal_weight():
    """Ratified default is EQUAL weight. Derived/collinear fields are out:
    max_risk_pct re-imports the R/R gate, so scoring it would double-count."""
    assert RANKING_SIGNALS == ("rr", "net_sources", "conviction_score")


def test_equal_weight_ranking_order_is_pinned(rows):
    """THE deterministic output on run-64290730. If this moves, the write-up
    in docs/WORK.md item 18 is stale and must be re-derived."""
    ranked = rank_eligible(rows)
    assert len(ranked) == 25
    assert [r["symbol"] for r in ranked] == [
        "VLO",
        "XLE",
        "NUE",
        "NVDA",
        "AAPL",
        "JPM",
        "COP",
        "JNJ",
        "CHPX",
        "RSG",
        "KO",
        "V",
        "PATH",
        "DIS",
        "SLB",
        "CMCSA",
        "DE",
        "NKE",
        "MSFT",
        "TSM",
        "MU",
        "FLNC",
        "CVX",
        "PFE",
        "CRM",
    ]
    assert [r["composite_score"] for r in ranked] == [
        2.3674,
        1.9848,
        1.8977,
        1.7424,
        1.7386,
        1.6477,
        1.6402,
        1.5795,
        1.5,
        1.3902,
        1.3788,
        1.3674,
        1.3523,
        1.3371,
        1.3371,
        1.3068,
        1.2614,
        1.2159,
        1.1742,
        1.1667,
        1.1477,
        1.0492,
        1.0,
        0.9205,
        0.5341,
    ]


def test_ranking_components_are_normalised_and_sum_to_the_score(rows):
    ranked = rank_eligible(rows)
    for row in ranked:
        for name in RANKING_SIGNALS:
            assert 0.0 <= row["score_components"][name] <= 1.0
        assert row["composite_score"] == pytest.approx(sum(row["score_components"].values()), abs=1e-4)
    # Each signal spans the full 0..1 range: none is constant across the set,
    # so all three actually contribute spread.
    for name in RANKING_SIGNALS:
        values = [r["score_components"][name] for r in ranked]
        assert min(values) == 0.0 and max(values) == 1.0


def test_ranking_scores_only_eligible_names_and_is_pure(rows):
    ranked = rank_eligible(rows)
    assert all(r["eligible"] for r in ranked)
    # GEV/UNH/NEE are refused upstream and must never receive a score.
    scored = {r["symbol"] for r in ranked}
    assert scored.isdisjoint({"GEV", "UNH", "NEE"})
    # Pure: re-running on the same rows gives an identical order, and the
    # input rows are not mutated with score fields.
    assert [r["symbol"] for r in rank_eligible(rows)] == [r["symbol"] for r in ranked]
    assert all("composite_score" not in r for r in rows)


def test_ranking_handles_an_empty_eligible_set():
    assert rank_eligible([{"symbol": "X", "eligible": False}]) == []
