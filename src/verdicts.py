"""Phase 13 — ranking analyst verdicts, every seat counted equally.

`docs/QAMC_REMEDIATION_SPEC.md` §13: the Portfolio Manager's rules gate and
ceiling but never ORDER. Read as code on run-64290730 (item 18, 2026-09-03)
they took 59 analysed names to 12 eligible and stopped — no tiebreaker, so
the effective choice fell to whatever the model defaulted toward. This module
is the missing ordering step, over the shared `AnalystVerdict` shape.

Pure logic only: no I/O, no LLM calls, no broker access — same posture and
same reason as `src/nominations.py`. Which candidates are ELIGIBLE is not
decided here; `PortfolioManagerAgent.candidate_eligibility` applies the
desk's gates, and this module orders only what it is handed.

**The score (archetype step C, 2026-10-19).** The COUNT of seats that agree
in the trade direction. A seat's declared conviction (low/medium/high) is NOT
part of the score any more: it is a number the model asserts about itself,
nobody has checked whether its "high" beats its "medium", and an unverifiable
number must never rank (owner archetype 2026-10-19, step 3; the n06 score that
summed it at 0 / 0.5 / 1 is retired). Each seat that leans counts exactly 1 --
equal weights, the published prior for a near-zero record (James-Stein /
Efron-Morris shrinkage to the group mean; DeMiguel-Garlappi-Uppal 2009). A
per-seat weight may come back only from this desk's own measured record.

Macro is NOT counted (`UNCOUNTED_SEATS`). Its read is market-wide, not
per-name: a sector row covers every name in the sector and, with no row, the
broad `equity_outlook` is cast onto every name alike, so counting it would add
the same vote to every name it reaches. The archetype has macro tilt size,
not rank. Its verdict is still carried on the candidate (and still splits a
group whose directions disagree, unchanged) -- it only adds nothing to `score`.

`AnalystVerdict.magnitude` (Technical's rating rung; every other seat states
none) is NOT added to the score either; it only BREAKS TIES between equal
counts (strong over plain, in either direction), then `level_touches`, then
`symbol` -- unchanged.

No min-max normalisation across the candidate set: the input is absolute,
so a candidate's score does not move when a different peer joins the list —
which is what makes the order reproducible from the verdicts alone.

**Every seat counts equally (n06, 2026-10-15; replaces the 2026-09-03
per-seat prior).** The score is a SUM across seats, and it is only ever
used by ORDER (which name ranks first) and by GAPS between two sums (the
rotation margin). Multiplying every seat by one common number changes
neither, so the only information a per-seat weight can carry is the RATIO
between seats. No such ratio has been measured — the literature gives a
direction only (cross-sectional momentum and post-earnings drift are the
strongest-replicated effects; macro turning-point calls are weak out of
sample), never a number — so a ratio would be invented. With no measured
ratio, the rule needs no constant: each seat's contribution is its own
`score_verdict`, added once. Sizing (`src/risk/rules.py`) was never
affected. A per-seat ratio may come back only from this desk's own
resolved calls, measured, not chosen.

**2026-09-13 (retired item 31).** What the (since deleted) seat weight multiplied changed for
four of the five seats. `score_verdict` was `magnitude + conviction`, and
news, macro and smart_money were each deriving `magnitude` from the very
field they also reported as `conviction` — so those seats fed one signal in
twice, at three different unsourced spacings, before that weight ever saw
them. Those tables are deleted; every seat but Technical now carries
`NO_STATED_STRENGTH` on any call, so what each of those
four seats contributes is exactly the seat's stated conviction. Technical keeps a real
gradient, because its rating rungs are a strength it actually states.

**2026-09-26 (item 65, retired).** `NO_STATED_STRENGTH` was the literal
`0.0` until this date, and the magnitude term below summed it like any other
seat's number. Verified before changing it: a sum is additively neutral to a
zero, no consumer of `magnitude` averages or thresholds it, so the ORDER this
module produced was never affected and no money moved on the placeholder.
What WAS wrong is that `components["magnitude"]` was reported — to the
Portfolio Manager's own prompt — as a strength "summed over N seats" when
four of five could not contribute to it, and nothing in the types stopped the
next consumer from reading four placeholders as four measured zeros. The
constant is now `None`, the term below sums only over the seats that
actually state a strength, and `RankedCandidate` names both sets. The
arithmetic is unchanged by construction and pinned by test; the honesty of
what it claims to be is not.

The decision that goes with it, recorded rather than left open: direction
plus confidence is everything news, macro, smart_money and earnings can say.
See `src/models.py::NO_STATED_STRENGTH` for the per-seat re-check and the two
conditions that would reopen it.

**2026-09-13, same day, ON REVIEW BEFORE MERGE — the aggregation is a SUM,
not an average, and that is the load-bearing change.** See `rank_verdicts`
for the argument. In one line: an average of seat scores made a second,
fully AGREEING seat lower a candidate's rank, which contradicts this desk's
own stated edge.

**2026-09-04 audit (real-data fix #2).** Ties used to break on `symbol`
alone. `score_verdict` only ever landed on {0.5, 1.0, 1.5, 2.0} when a single
seat was live (magnitude in {0, 0.5, 1} in practice, conviction in
{0, 0.5, 1}; since n06 it is conviction alone), so ties were the COMMON case, not the exception — one real
production day had 9 of 12 eligible names tied, another 23 of 33 — and
breaking on `symbol` gave every tied ticker earlier in the alphabet a real,
systematic, permanent, undisclosed edge that had nothing to do with the
call's quality.

Fix: before falling back to `symbol`, break ties on `risk_reward` — the
reward:risk ratio each seat ALREADY computes and attaches as evidence
(`VerdictEvidence(label="risk_reward", value=...)`, see
`TechAnalysisResult.risk_reward` / `.to_verdict`). This is real information
about the candidate already sitting on the verdict at ranking time, costs no
new call, and a higher reward:risk on an otherwise-tied score is a
defensible reason to prefer one candidate over another (the same ratio this
desk already gates entries on elsewhere). A verdict with no `risk_reward`
evidence (missing entry/stop/target geometry) sorts as if it were 0 for this
purpose — never crashes the ranking, never wins a tie it has no evidence
for. Only once BOTH the composite score and the risk_reward tiebreak are
equal does `symbol` still decide the order — at that point the two
candidates are equivalent on every signal this module has, and a
deterministic (not random) final order is still needed for reproducibility;
that residual is not the systematic bias being fixed here, because nothing
distinguishes the two by then anyway.

**2026-10-10: reward:risk REMOVED from the ordering (owner ruling 9 Oct).**
The ratio's reward side is the entry take-profit target, which is not a sell
rule (the desk never exits on it) and is unscored until 12 Oct; its risk
side is a model-written stop. An unverifiable number must never rank or size
a trade, so ties on score now fall straight to `level_touches_tiebreak`,
then `symbol`. Ranking on the target is to be revisited only once that score
exists. `components["risk_reward_tiebreak"]` is still computed and recorded
(the `candidate_ranked` pipeline event carries it) but no longer orders
anything.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from src.models import AnalystVerdict
from src.risk.constants import reward_risk_floor_applies

__all__ = [
    "CONVICTION_SCORE",
    "RANKING_SIGNALS",
    "UNCOUNTED_SEATS",
    "RankedCandidate",
    "conviction_score",
    "score_verdict",
    "rank_verdicts",
    "risk_reward_of",
    "level_touches_of",
]

#: Ordinal encoding of the desk's own conviction scale onto 0..1. Equal
#: spacing — nothing measured justifies anything else. NOT read by the score
#: since 2026-10-19 (a self-declared number must never rank); kept for the
#: callers that still describe a seat's stated conviction.
CONVICTION_SCORE: dict[str, float] = {"low": 0.0, "medium": 0.5, "high": 1.0}

#: What the score is: the count of seats agreeing in the trade direction
#: (2026-10-19). Conviction is not read; the rating rung only breaks ties.
RANKING_SIGNALS: tuple[str, ...] = ("agreeing_seats",)

#: Seats whose verdict never adds to the count: macro's read is market-wide,
#: cast onto every name alike, so it tilts size and never ranks (archetype
#: 2026-10-19, step 3).
UNCOUNTED_SEATS: frozenset[str] = frozenset({"macro"})


def conviction_score(conviction: str) -> float:
    return CONVICTION_SCORE.get(conviction, 0.0)


def score_verdict(verdict: AnalystVerdict) -> float:
    """One verdict's contribution to the score: 1 if it leans, else 0.

    A directional verdict from a counted seat adds exactly one agreeing seat;
    a neutral verdict, or one from an `UNCOUNTED_SEATS` seat, adds nothing.
    Declared conviction is deliberately not read (2026-10-19): a
    self-declared number nobody can check must never rank. The rating rung
    breaks ties instead -- see `rank_verdicts`.
    """
    if verdict.direction == "neutral" or verdict.seat in UNCOUNTED_SEATS:
        return 0.0
    return 1.0


def risk_reward_of(verdict: AnalystVerdict) -> float | None:
    """The `risk_reward` value already carried in `verdict.evidence`, if any.

    Reads the evidence item a seat's own `to_verdict()` attaches
    (`VerdictEvidence(label="risk_reward", value=...)`, see
    `TechAnalysisResult.risk_reward`) rather than recomputing anything — this
    module never derives new geometry, it only orders what it is handed.
    Returns None when the seat didn't attach one (missing entry/stop/target
    geometry), which the tiebreak below treats as the lowest possible value
    rather than a crash.
    """
    for item in verdict.evidence:
        if item.label == "risk_reward" and item.value is not None:
            return float(item.value)
    return None


def level_touches_of(verdict: AnalystVerdict) -> float | None:
    """The `stop_side_level_touches` value already carried in
    `verdict.evidence`, if any.

    `TechAnalysisResult.to_verdict()` attaches this: the sum, over the
    computed structural levels on the RISK side of the analyst's entry
    (below entry for a long, above it for a short — never the overhead
    side a breakout is deliberately not judged on), of how many prior
    pivots `find_structural_levels` clustered into each one
    (`computed_level_touches`, the same count `PortfolioConstructor.
    _level_backing_stop` already trusts to decide whether a stop earns the
    tight-stop exemption). Real, already-measured chart structure — not
    derived here, not invented.

    Only the technical seat attaches this today (it is the only seat with a
    chart), so this is None for every other seat's verdict.

    Summed unweighted below, like every other signal since per-seat
    weights were deleted (n06, 2026-10-15). Because it is summed, not
    averaged, across whatever seats attach it, the total would double the
    day a second seat ever reports its own chart structure under the same
    label.
    """
    for item in verdict.evidence:
        if item.label == "stop_side_level_touches" and item.value is not None:
            return float(item.value)
    return None


@dataclass
class RankedCandidate:
    """One symbol's place in the order, with the arithmetic that put it there."""

    symbol: str
    direction: str
    score: float
    verdicts: list[AnalystVerdict] = field(default_factory=list)
    components: dict[str, float] = field(default_factory=dict)
    #: Seats that DID report on this symbol and came back with no lean.
    #: They contribute nothing to `score` (there is nothing to contribute),
    #: but they are not nothing to a reader: "macro was consulted and its
    #: own sector rows contradicted each other" is a different state from
    #: "macro never looked at this name," and before 2026-09-13 the two were
    #: indistinguishable downstream because neutral verdicts were dropped
    #: silently. See `rank_verdicts`.
    neutral_seats: list[str] = field(default_factory=list)
    #: Of the seats that DID lean, the ones that stated a rating rung of
    #: their own and therefore feed `rung_tiebreak`.
    strength_seats: list[str] = field(default_factory=list)
    #: The complement: seats that leaned but have no strength scale at all
    #: (news, macro, smart_money, earnings — see
    #: `src/models.py::NO_STATED_STRENGTH`). They are ABSENT from the
    #: strength term, not present at zero, and they still count as one
    #: agreeing seat in `score` (macro excepted, `UNCOUNTED_SEATS`).
    no_strength_seats: list[str] = field(default_factory=list)
    #: Summed rating-rung strength (strong over plain). Orders candidates that
    #: tie on `score`; never part of `score` itself (n06).
    rung_tiebreak: float = 0.0

    @property
    def seats(self) -> list[str]:
        return [v.seat for v in self.verdicts]


def rank_verdicts(
    verdicts: list[AnalystVerdict],
    *,
    real_reward_risk: dict[str, float | None] | None = None,
    setup_types: dict[str, str | None] | None = None,
) -> list[RankedCandidate]:
    """Order candidates highest composite first; ties on symbol.

    **2026-09-11, docs/WORK.md item 1(d).** Two changes to the reward:risk
    ordering key, both about making it carry a REAL number or none at all:

    `real_reward_risk` (keyed by upper-case symbol) is
    `PortfolioConstructor.real_reward_risk_preview`'s output — the ratio
    measured from this specific trade's own real support (the stop that will
    actually ship) and its own real resistance (the derived structural
    target). Supplied, it replaces the analyst's guessed figure carried on
    the verdict. This is where the removed hard floor went: a weak range
    payoff is no longer refused, it is ranked below a better one on its own
    real number.

    `setup_types` (same keying) marks Type B / breakout candidates, whose
    reward:risk key is dropped entirely rather than set to anything. A
    breakout has no overhead level to measure a reward against, so it has no
    comparable number — and being scored 0 for not having one would
    reintroduce the same penalty at the ranking level that item 1(d) just
    removed at the gate. An absent key is simply absent.

    **2026-10-10:** that ratio is now RECORDED ONLY — it no longer orders
    candidates (owner ruling 9 Oct; see the module docstring).

    **THE SCORE IS A SUM (since 2026-10-19 a COUNT) ACROSS SEATS, NOT AN AVERAGE —
    changed 2026-09-13 on adversarial review, before merge. Read this before
    changing it back.**

    It was an average. An average made a second, fully AGREEING seat LOWER a
    candidate's rank. Reproduced against the code that day: a technical
    `strong_buy` at high conviction alone scores magnitude 1.0 + conviction
    1.0 = 2.0. Add a smart-money `actionable` verdict — the strongest thing
    that seat can say, agreeing on direction — and the average of the two
    seats' magnitudes fell to 0.8, scoring 1.8. Agreement was DILUTIVE.

    That is not a tuning problem, it is the wrong shape. It contradicts the
    desk's own stated edge — breadth x consistency x asymmetry — and
    `docs/OUTCOME.md` §9.4's "agreement earns size". An average answers "how
    enthusiastic is the average seat covering this name", a quantity the edge
    statement never names and nobody asked for. What the edge names is
    BREADTH: how much independently-sourced, same-direction evidence this
    candidate has. A sum measures that; an average measures it out.

    Why a sum is the doctrine-clean fix and not just a different arbitrary
    choice (`qamc-no-arbitrary-numbers-principle`):

      * It introduces NO number. It deletes a divisor. Every constant in the
        arithmetic afterwards is a count of 1 per agreeing seat (2026-10-19;
        the conviction ordinal it used to sum is retired).
      * It is monotone by construction. Every term is a
        non-negative (0 or 1), so an added seat can only ADD.
        "Agreement can only add" stops being a property to test for and
        becomes a property of the arithmetic.
      * Consistency is already enforced upstream of it, not inside it: a
        group whose directions disagree is dropped whole, below. So a sum can
        never aggregate disagreement into a bigger number.

    Two consequences worth stating plainly rather than discovering later:

      * The score is no longer bounded at 2.0 and no longer comparable to a
        score recorded before 2026-09-13. It remains absolute (no
        cross-candidate normalisation), so a candidate's score still does not
        move when a peer joins the run.
      * Coverage now moves the score. A name carrying a live earnings filing
        and a confirmed smart-money flow outranks an otherwise identical name
        with only a chart, and it drops back when that coverage expires.
        That is the intended reading of breadth, and it is what
        `src/rotation.py`'s relative margin will compare — the margin is a
        ratio, so it is unaffected by the change of scale, but it IS affected
        by coverage decay on a held name. Called out in `docs/WORK.md`.

    The recorded reward:risk component below stays a MEAN, deliberately. It
    aggregates several seats' estimates of ONE quantity in a real unit; two
    seats both reading 2.0 do not make 4.0. Evidence adds, measurements
    average.

    A neutral verdict has no lean and cannot be summed into anything, so it
    does not enter the score — but as of 2026-09-13 it is no longer dropped
    without trace: the seat's name is recorded on
    `RankedCandidate.neutral_seats`, so a reader can tell "this seat looked
    and came back with nothing" from "this seat never looked". A symbol whose
    seats disagree on direction is NOT ranked here — that is a conflict for
    §9.3/§9.4 to adjudicate, and ordering it would hide the disagreement
    inside a number. It is dropped with the reason recorded on the caller's
    side (`candidate_eligibility`), never silently.

    **One verdict per (symbol, seat).** A caller CAN hand this two verdicts
    from the same seat on the same symbol in one run — e.g. earnings, if two
    filings for one ticker were both analysed the same session (a real,
    if uncommon, case: nothing upstream enforces one-filing-per-symbol-per-
    run). Aggregating per seat already assumes one vote per
    seat — silently
    letting a duplicate through would double that seat's vote without
    anyone deciding it should count twice. Found and fixed 2026-09-03 by an
    adversarial review before this shipped: caught via the concrete case of
    two earnings verdicts pushing earnings' effective weight from 50% to
    66.7% of a two-seat group. LAST ONE WINS per (symbol, seat), input
    order — the same "last-wins per symbol" convention already used for
    the evidence registry (`PortfolioManagerAgent._earnings_stance_rows`),
    applied one level finer (per seat, not just per symbol).
    """
    by_symbol: dict[str, dict[str, AnalystVerdict]] = {}
    neutral_by_symbol: dict[str, dict[str, AnalystVerdict]] = {}
    for verdict in verdicts:
        # Same last-wins-per-(symbol, seat) rule on both sides, so a seat
        # that reported twice cannot appear as both a scorer and a no-lean.
        target = neutral_by_symbol if verdict.direction == "neutral" else by_symbol
        target.setdefault(verdict.symbol.upper(), {})[verdict.seat] = verdict

    ranked: list[RankedCandidate] = []
    for symbol, seat_verdicts in by_symbol.items():
        group = list(seat_verdicts.values())
        directions = {v.direction for v in group}
        if len(directions) != 1:
            continue
        # SUM, not mean — see the docstring. Adding an agreeing seat adds a
        # non-negative term and can never reduce the total.
        #
        # Every counted seat adds exactly 1 (2026-10-19): the score is the
        # number of seats agreeing in this direction. Declared conviction is
        # not read; macro is not counted (`UNCOUNTED_SEATS`). The rating rung
        # is summed only over the seats that state one and is a tiebreak.
        stated = [v for v in group if v.magnitude is not None]
        rung = sum(v.magnitude for v in stated)
        agreeing = sum(score_verdict(v) for v in group)
        components = {"agreeing_seats": round(agreeing, 4)}
        # RECORDED ONLY since 2026-10-10 (owner ruling 9 Oct): never added
        # into `score` and no longer used to order candidates — the target
        # it is built on is not a sell rule and is unscored. Kept because
        # the `candidate_ranked` pipeline event records it. Weighted the
        # same way as the score's own inputs (every seat equally).
        #
        # Which number: the desk's own REAL, structure-derived ratio when
        # the caller supplies one (item 1(d)), otherwise the figure each
        # seat attached to its verdict. None — a breakout, or a candidate
        # with no measurable geometry — means the key is ABSENT, which is
        # different from zero.
        if setup_types is not None and not reward_risk_floor_applies(setup_types.get(symbol)):
            risk_reward: float | None = None
        elif real_reward_risk is not None and symbol in real_reward_risk:
            raw = real_reward_risk.get(symbol)
            risk_reward = None if raw is None else float(raw)
        else:
            rr_values = [risk_reward_of(v) for v in group]
            rr_stated = [rr for rr in rr_values if rr is not None]
            risk_reward = sum(rr_stated) / len(rr_stated) if rr_stated else None
        if risk_reward is not None:
            components["risk_reward_tiebreak"] = round(risk_reward, 4)
        # Second-stage tiebreak (item 141, 2026-09-20; second since
        # 2026-10-10): summed
        # `stop_side_level_touches` evidence — unweighted, like every
        # summed signal above. Unlike `risk_reward`, an absence
        # here is a real zero (no computed structural levels on the risk
        # side of the chart at all) — not a quantity
        # `reward_risk_floor_applies` deliberately withholds for a whole
        # setup type — so there is no "absent means neutral" case to
        # handle: a candidate that attaches nothing here simply sorts
        # behind one that attaches something, on real information either
        # way.
        level_touches_total = sum(lt or 0.0 for lt in (level_touches_of(v) for v in group))
        components["level_touches_tiebreak"] = round(level_touches_total, 4)
        ranked.append(
            RankedCandidate(
                symbol=symbol,
                direction=directions.pop(),
                score=components["agreeing_seats"],
                verdicts=sorted(group, key=lambda v: v.seat),
                components=components,
                neutral_seats=sorted(neutral_by_symbol.get(symbol, {})),
                strength_seats=sorted(v.seat for v in group if v.magnitude is not None),
                no_strength_seats=sorted(v.seat for v in group if v.magnitude is None),
                rung_tiebreak=round(rung, 4),
            )
        )
    # Highest score first; on a tie, strongest summed rating rung (strong
    # over plain, buy or sell side alike); then highest summed `level_touches`
    # next — real, already-measured chart structure (item 141,
    # 2026-09-20); `symbol` is the final, deterministic-but-arbitrary
    # stabiliser only reached once every real signal is equal.
    #
    # reward:risk is deliberately NOT a key (removed 2026-10-10, owner
    # ruling 9 Oct): its reward side is the entry take-profit target, which
    # is not a sell rule and is unscored until 12 Oct, over a model-written
    # stop. An unverifiable number must never rank or size a trade. Ranking
    # on the target is to be revisited only once that score exists.
    ranked.sort(
        key=lambda c: (
            -c.score,
            -c.rung_tiebreak,
            -c.components.get("level_touches_tiebreak", 0.0),
            c.symbol,
        )
    )
    return ranked
