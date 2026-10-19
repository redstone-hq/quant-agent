"""The desk's OWN stated selection rules, written as plain Python and run
against a frozen `pm_selection` fixture (run-bba4d4f3 since 2026-09-14; the
item 18 audit tests still pin run-64290730). **No LLM call anywhere in here.**

Why this exists (docs/WORK.md item 18b, the owner's question): if the rules
the desk already states are complete, they determine what to buy and the
model is not needed for the selection step. If they are not complete, the
gap they leave is where an unspecified preference — "familiarity" — enters,
and the fix is to specify the missing rule rather than to instruct harder.

The rules replayed here, and where each one is stated:

  R1  current technical coverage         `portfolio_manager.md` "What to trade"
  R2  rating is actionable (not neutral) same
  R3  longs must be BUY-eligible         "Deterministic BUY Eligibility" block
  R4  (range setups only, 2026-09-11 item 1(d)) computed R/R must be
      MEASURABLE — its SIZE no longer gates — OR a catalyst resolving to a
      dated Active News
      State Change row naming the symbol (then capped 0.5% risk)
                                         Rule Priority row 7 / Step 5
  R5  RETIRED 2026-10-19 (desk archetype ruling): the net source score is
      still computed and reported, but refuses nothing, matching
      `candidate_eligibility` in production.
  R6  conviction sizing band, capped by  Step 5 "Base RISK allocation"
      max_position_risk_pct

Everything above is a GATE or a CEILING. Nothing above is a RANKING — that
is the finding, and `summarise()` reports it rather than inventing one.
"""
from __future__ import annotations

import re
import sys
from datetime import date
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent.parent
if str(_REPO) not in sys.path:  # pragma: no cover - import convenience
    sys.path.insert(0, str(_REPO))

from src.agents.portfolio_manager import PortfolioManagerAgent  # noqa: E402
from src.portfolio_constructor import PortfolioConstructor  # noqa: E402
from src.risk.constants import (  # noqa: E402
    STARTER_POSITION_RISK_PCT,
    reward_risk_floor_applies,
)
from src.risk.rules import (  # noqa: E402
    agreement_refuses_trade,
    count_aligned_sources,
    count_opposing_sources,
    signed_source_score,
)

# Read from config/settings.yaml rather than imported, because `load_config`
# validates API keys this audit does not have and does not need. Pinned by
# `tests/test_deterministic_selection.py` against the YAML so a config edit
# cannot silently desync them.
MAX_POSITION_RISK_PCT = 5.0

# **Imported, never retyped (2026-09-14).** Importing the live constant means
# a change to the cap moves this audit with it instead of leaving a stale
# duplicate behind. The reward:risk floor (1.5) was deleted 2026-10-15: no
# ratio, however thin, is compared against any number here.
SUBFLOOR_CATALYST_RISK_PCT = STARTER_POSITION_RISK_PCT
# **CORRECTED 2026-09-14.** These were `high (1.5, 3.0) / medium (1.0, 2.0)`,
# the bands `config/prompts/portfolio_manager.md` stated BEFORE 2026-09-10.
# The live sheet has said 2.0-4.0 / 1.0-2.5 / 0.5-1.0 since, so this audit had
# been sizing its replay off a prompt the desk no longer runs — and, because
# `CONVICTION_SCORE` below is read off the band tops, ranking off it too.
# Nobody had named it. `tests/test_deterministic_selection.py` now parses the
# bands straight out of the live prompt and fails if these drift again, which
# is the only version of this that holds.
CONVICTION_BANDS = {"high": (2.0, 4.0), "medium": (1.0, 2.5), "low": (0.5, 1.0)}

# `- [2026-08-27] headline → NVDA, SMH, SOXX` — the row shape Rule Priority
# row 7 resolves a sub-floor catalyst against. Only the symbol list after the
# arrow counts; a symbol merely mentioned in the headline prose does not.
_STATE_CHANGE_ROW = re.compile(r"^\s*-\s*\[(\d{4}-\d{2}-\d{2})\][^\n]*?→\s*(.+)$", re.M)


def catalyst_symbols(active_state_changes: str) -> set[str]:
    """Symbols a sub-floor pick may legally cite as its catalyst."""
    out: set[str] = set()
    for _date, tail in _STATE_CHANGE_ROW.findall(active_state_changes or ""):
        out.update(part.strip().upper() for part in tail.split(",") if part.strip())
    return out


def session_day(selection: dict) -> date:
    """The trading day a frozen selection was recorded on, from its provenance.

    A replay judges evidence age as of THAT day, never the day the replay
    runs: measured against today, the fixture's earnings views cross the
    age bound one by one and the replay drifts with the calendar (14 Oct
    2026: a 15 Jul filing turned 91 days old and the admitted set fell 25 -> 24).
    Raises when the fixture carries no session timestamp rather than
    falling back to today.
    """
    return date.fromisoformat(str(selection["_provenance"]["session_timestamp"])[:10])


def evaluate(selection: dict, analyses, positions, news_intel) -> list[dict]:
    """One row per analysed symbol: eligible or the rules that blocked it."""
    agent = PortfolioManagerAgent.__new__(PortfolioManagerAgent)
    registry = agent.build_evidence_registry(
        analyses=analyses,
        positions=positions,
        news_intel=news_intel,
        earnings_analyses=selection["earnings_analyses"],
        macro_analysis=selection["macro_analysis"],
        smart_money_findings=[],
        symbol_sectors={},
    )
    stale = agent.stale_evidence_sources(
        earnings_analyses=selection["earnings_analyses"],
        asof=session_day(selection),
    )
    allowed = {
        str(s).strip().upper()
        for s in list(selection["allowed_buy_symbols"])
        + list(selection["transient_admitted_symbols"])
        if str(s).strip()
    }
    catalysts = catalyst_symbols(selection["memory"]["active_state_changes"])
    held = {p.symbol for p in positions}
    # 2026-09-04: `rr_real` is `PortfolioConstructor.real_reward_risk_
    # preview`'s output — the SAME real derived-target, noise-floor-widened
    # reward:risk `construct_orders` gates on, and, since the same date,
    # `PortfolioManagerAgent.candidate_eligibility` (production). R4 below
    # is deliberately left gating on `rr` (the analyst's own guessed-target
    # arithmetic), NOT switched to `rr_real`: this file's frozen fixture
    # (`run_64290730_pm_input.json`, 2026-09-01) predates
    # `TechAnalysisResult.computed_levels` being populated — every row's
    # `computed_levels` is `[]` — so `derive_structural_target` refuses
    # EVERY name for lack of structure and `rr_real` is `None` across the
    # board on this fixture. Gating R4 on it here would not replay "the
    # desk's own rules more accurately"; it would replay "no fixture data",
    # and silently invalidate every number `docs/INCIDENT_HISTORY.md`
    # records for this fixture. `rr_real` is exposed per-row anyway so a
    # caller with a fixture that DOES carry `computed_levels` can see it
    # and, if desired, gate on it — see `tests/test_deterministic_
    # selection.py` and the PR that added this field for how newer
    # production runs (which do carry `computed_levels`) were used to
    # validate the real gate end to end.
    # 2026-09-14 (docs/WORK.md item 72): the level-less day is no longer what
    # `pm_selection` grades. On its replacement, run-bba4d4f3, every
    # actionable name has a non-None `rr_real`, and production
    # `candidate_eligibility` handed those ratios admits exactly the set this
    # function does (pinned in tests/test_pm_selection_scenario.py). On
    # run-64290730 it does NOT — 12 against 25 — which is why that day was
    # retired from grading.
    constructor = PortfolioConstructor()

    rows: list[dict] = []
    for a in analyses:
        symbol = a.symbol
        rr = a.risk_reward
        blocked: list[str] = []
        direction = "short" if a.rating in ("sell", "strong_sell") else "long"
        rr_real = constructor.real_reward_risk_preview(a, direction)

        if a.rating == "neutral":  # R2
            blocked.append("R2 neutral rating")
        if direction == "long" and symbol.upper() not in allowed:  # R3
            blocked.append("R3 not BUY-eligible")

        # R4. **Rewritten 2026-09-11, docs/WORK.md item 1(d)** — kept in step
        # with `PortfolioManagerAgent.candidate_eligibility`, which this
        # script exists to shadow. It no longer blocks on the SIZE of the
        # ratio, only on its ABSENCE, and it does not run at all for a Type B
        # / breakout candidate (no overhead level to measure a reward
        # against; see `src.risk.constants.reward_risk_floor_applies`).
        # `rr` is still reported per row; no number is compared against it.
        subfloor_catalyst = False
        if reward_risk_floor_applies(a.setup_type):
            if rr is None:
                if symbol.upper() in catalysts:
                    subfloor_catalyst = True
                else:
                    blocked.append(
                        "R4 R/R unmeasurable, no dated catalyst row"
                    )
            # FLAGGED, NOT FIXED (2026-09-14, verified against
            # `PortfolioManagerAgent._apply_subfloor_catalyst_rule`): live,
            # EVERY measurable sub-floor range target is capped at starter
            # size, catalyst or none. Here the cap is still conditional on a
            # catalyst, so `max_risk_pct` overstates what production would
            # grant ten of the eligible names on this fixture, and with it
            # `total_max_risk_pct` (47.24). It does not change ELIGIBILITY,
            # which is all the `pm_selection` grader consumes, so it is
            # reported rather than changed under a benchmark fix — closing it
            # re-derives numbers pinned in docs/WORK.md item 18.

        sources = registry.get(symbol, {})  # R5
        ignored = stale.get(symbol)
        net = aligned = opposed = 0
        if sources:
            aligned = count_aligned_sources(
                symbol, sources, direction, ignored_sources=ignored)
            opposed = count_opposing_sources(
                symbol, sources, direction, ignored_sources=ignored)
            net = signed_source_score(
                symbol, sources, direction, ignored_sources=ignored)
        # R5 retired 2026-10-19 (desk archetype ruling): production no longer
        # refuses on the net score, so the audit copy reports it and refuses
        # nothing on it.

        band = CONVICTION_BANDS.get(a.conviction, (0.0, 0.0))  # R6
        max_risk = min(band[1], MAX_POSITION_RISK_PCT)
        if subfloor_catalyst:
            max_risk = min(max_risk, SUBFLOOR_CATALYST_RISK_PCT)

        rows.append({
            "symbol": symbol,
            "direction": direction,
            "rating": a.rating,
            "conviction": a.conviction,
            "rr": rr,
            "rr_real": rr_real,
            "aligned": aligned,
            "opposed": opposed,
            "net_sources": net,
            "max_risk_pct": round(max_risk, 2),
            "subfloor_catalyst": subfloor_catalyst,
            "held": symbol in held,
            "eligible": not blocked,
            "blocked_by": blocked,
        })
    return rows


# --------------------------------------------------------------------------
# The missing RANKING step, at the ratified equal weight. NOT WIRED IN.
# --------------------------------------------------------------------------
# The owner-ratified architecture decision is a weighted composite score
# across signals, **starting equal-weight**. This implements exactly that and
# nothing more: no tuned weights, no new thresholds, no signal that `evaluate`
# does not already compute for every candidate.
#
# Which of `evaluate`'s per-candidate numbers are admissible as SCORE inputs:
#
#   rr                     YES  primitive, continuous
#   net_sources            YES  primitive, integer
#   conviction             YES  ordinal, and the file ALREADY carries the
#                               desk's own numeric encoding of it in
#                               CONVICTION_BANDS (low/medium/high -> 1/2/3
#                               risk-percent band tops). Not a new number.
#   aligned, opposed       NO   net_sources IS aligned - opposed; scoring all
#                               three counts the same evidence three times.
#   max_risk_pct           NO   a function of conviction and the sub-floor
#                               cap — it re-imports the R/R gate the ranking
#                               is supposed to be independent of.
#   subfloor_catalyst,     NO   booleans that restate gate outcomes, not
#   held, eligible               strength-of-candidate signals.
#
# So three independent signals, min-max normalised across the eligible set so
# they share a 0..1 scale, summed at weight 1.0 each. A signal that is
# constant across the set contributes 0 to every candidate (no spurious
# spread), which is the only defensible degenerate case.
RANKING_SIGNALS = ("rr", "net_sources", "conviction_score")

# The desk's own conviction encoding, read off CONVICTION_BANDS rather than
# chosen here, so a config change to the bands moves this with it.
CONVICTION_SCORE = {name: band[1] for name, band in CONVICTION_BANDS.items()}


def _min_max(values: list[float]) -> list[float]:
    """0..1 across the set; an all-equal signal contributes nothing."""
    lo, hi = min(values), max(values)
    if hi == lo:
        return [0.0 for _ in values]
    return [(v - lo) / (hi - lo) for v in values]


def rank_eligible(rows: list[dict]) -> list[dict]:
    """Equal-weight composite score over the eligible candidates.

    Pure and deterministic. Returns the eligible rows highest score first,
    each with its normalised components and `composite_score`. Ties break on
    symbol so the order is stable. **This is not called by any production
    code path** — see the module docstring and docs/WORK.md item 18.
    """
    eligible = [r for r in rows if r["eligible"]]
    if not eligible:
        return []

    raw = {
        "rr": [float(r["rr"] or 0.0) for r in eligible],
        "net_sources": [float(r["net_sources"]) for r in eligible],
        "conviction_score": [
            float(CONVICTION_SCORE.get(r["conviction"], 0.0)) for r in eligible
        ],
    }
    normalised = {name: _min_max(values) for name, values in raw.items()}

    scored = []
    for i, row in enumerate(eligible):
        components = {name: round(normalised[name][i], 4) for name in RANKING_SIGNALS}
        scored.append({
            **row,
            "score_components": components,
            "composite_score": round(sum(components.values()), 4),
        })
    scored.sort(key=lambda r: (-r["composite_score"], r["symbol"]))
    return scored


def summarise(rows: list[dict]) -> dict:
    eligible = [r for r in rows if r["eligible"]]
    return {
        "analysed": len(rows),
        "eligible": len(eligible),
        "clears_rr_floor_alone": sorted(
            r["symbol"] for r in eligible if not r["subfloor_catalyst"]),
        "enters_via_catalyst_door": sorted(
            r["symbol"] for r in eligible if r["subfloor_catalyst"]),
        "total_max_risk_pct": round(sum(r["max_risk_pct"] for r in eligible), 2),
        # The whole point. A ranking rule would name ONE symbol here.
        "rules_name_a_single_pick": False,
    }


def _main() -> None:  # pragma: no cover - operator entry point
    from ops.model_policy import scenarios as S

    rows = evaluate(
        S._SELECTION, S._SELECTION_ANALYSES, S._SELECTION_POSITIONS, S._SELECTION_NEWS,
    )
    summary = summarise(rows)
    print(f"analysed={summary['analysed']} eligible={summary['eligible']} "
          f"total max risk {summary['total_max_risk_pct']}% "
          f"(budget 25%) -> nothing forces a choice")
    header = (f"{'sym':<7}{'dir':<6}{'rating':<12}{'conv':<8}{'R/R':>6}"
              f"{'net':>5}{'maxrisk%':>10}  door")
    print(header)
    print("-" * len(header))
    for r in sorted((r for r in rows if r["eligible"]), key=lambda x: -(x["rr"] or 0)):
        door = "catalyst" if r["subfloor_catalyst"] else "R/R floor"
        print(f"{r['symbol']:<7}{r['direction']:<6}{r['rating']:<12}"
              f"{r['conviction']:<8}{(r['rr'] or 0):6.2f}{r['net_sources']:>5}"
              f"{r['max_risk_pct']:>10.2f}  {door}")

    ranked = rank_eligible(rows)
    print("\nEQUAL-WEIGHT COMPOSITE RANKING (not wired into production):")
    print(f"{'#':<4}{'sym':<7}{'score':>7}   " + "  ".join(
        f"{name:>16}" for name in RANKING_SIGNALS))
    for i, r in enumerate(ranked, 1):
        parts = "  ".join(f"{r['score_components'][n]:>16.4f}" for n in RANKING_SIGNALS)
        print(f"{i:<4}{r['symbol']:<7}{r['composite_score']:>7.4f}   {parts}")
    print("order: " + ", ".join(r["symbol"] for r in ranked))


if __name__ == "__main__":  # pragma: no cover
    _main()
