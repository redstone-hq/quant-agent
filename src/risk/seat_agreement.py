"""Seat agreement — spec §9.4 "agreement earns size".

Which evidence seats point with or against a proposed direction, and the
refusal rule on the net score. The signed score itself (`signed_source_score`),
`SEAT_WEIGHT` stays in `src/risk/rules.py`:
the number ledger pins it there by module id.

Bodies moved VERBATIM from `src/risk/rules.py` (AST-identical to the
originals; `tests/test_risk_rules_parts_boundary.py` is the witness that this
part builds and runs alone). `src/risk/rules.py` keeps the engine, every
ledger-pinned number and the re-export mirror, so every existing
`from src.risk.rules import X` keeps resolving.
"""

from src.quantities import effective_multiplier as _effective_multiplier


# --- Spec §9.4 "agreement earns size" -------------------------------------
#
# Since 2026-09-02 the quantity in play is a SIGNED SUM, not a headcount:
# `signed_source_score` nets opposed seats off aligned ones. Since
# 2026-09-14 it no longer earns SIZE at all — `agreement_refuses_trade`
# turns it into one yes/no refusal and nothing else (see that function for
# why the graduated ceiling was retired). The two counts below are still
# computed and still reported — a reader wants "2 for, 1 against", not only
# "net +1" — but neither of them sizes anything.
#
# Shared polarity vocabulary. `PortfolioManagerAgent.validate_grounding`
# (src/agents/portfolio_manager.py) uses this to decide whether a
# provenance claim's stance "supports" a target's direction; the
# constructor's agreement refusal (`src/portfolio_constructor.py`) uses the
# SAME rule to count how many of the canonical evidence registry's
# independent sources are directionally aligned with what the PM is
# actually proposing. One definition, two consumers, by design — a second,
# divergent notion of "aligned" here would let the ceiling and the
# grounding gate disagree about identical evidence.
_BULLISH_STANCES = frozenset(
    {
        "strong_buy",
        "buy",
        "bullish",
        "positive",
        "risk_on",
        "overweight",
        "favorable",
    }
)
_BEARISH_STANCES = frozenset(
    {
        "strong_sell",
        "sell",
        "bearish",
        "negative",
        "risk_off",
        "underweight",
        "unfavorable",
    }
)


def stance_is_aligned(source: str, symbol: str, stance: str, *, wants_bullish: bool) -> bool:
    """True when `stance` (a canonical registry stance for `source` on
    `symbol`) points the direction `wants_bullish` asks for.

    Carries the one twist `validate_grounding` has always applied: a
    risk-off MACRO stance supports owning an INVERSE ETF, so macro's
    polarity is flipped for a symbol with a negative effective multiplier
    — the rating still describes the ETF's own price, which moves
    opposite the index it inverts.
    """
    stance_is_bullish = stance in _BULLISH_STANCES
    stance_is_bearish = stance in _BEARISH_STANCES
    if source == "macro" and _effective_multiplier(symbol) < 0:
        stance_is_bullish, stance_is_bearish = stance_is_bearish, stance_is_bullish
    return stance_is_bullish if wants_bullish else stance_is_bearish


def _count_sources(
    symbol: str,
    sources: dict[str, str],
    *,
    wants_bullish: bool,
    ignored_sources: frozenset[str] | set[str] | None,
) -> int:
    ignored = ignored_sources or frozenset()
    return sum(
        1
        for source, stance in sources.items()
        if source not in ignored and stance_is_aligned(source, symbol, stance, wants_bullish=wants_bullish)
    )


def count_aligned_sources(
    symbol: str,
    sources: dict[str, str],
    direction: str,
    *,
    ignored_sources: frozenset[str] | set[str] | None = None,
) -> int:
    """The deterministic "agreement count": how many independent seats (of
    technical/news/earnings/macro/smart_money) recorded a stance for
    `symbol` that points the same way as `direction` ("long" wants
    bullish, "short" wants bearish).

    `sources` must be one symbol's slice of the canonical evidence
    registry (`PortfolioManagerAgent.build_evidence_registry`) — ALL
    current coverage, not just what a target's own `provenance` list
    happens to cite. That distinction is the point: this count is what
    earns size, so it has to come from evidence the PM cannot selectively
    quote from, not from the PM's own (possibly incomplete) claims about
    itself.

    `ignored_sources` names the seats whose stance for THIS symbol is too
    stale to earn size — currently only `earnings`, no longer current
    (superseded by a newer periodic report, or past the next report's
    statutory deadline) by
    `PortfolioManagerAgent.stale_evidence_sources`. It is a REMOVAL from the
    tally, never an addition — but it removes the seat from BOTH sides, so
    dropping an opposing stance can lift a refusal as well as dropping an
    aligned one can lower a ceiling. A stale
    stance stays in the registry (it is still real coverage, and the PM may
    still cite it) — it simply stops being paid for.
    """
    return _count_sources(
        symbol,
        sources,
        wants_bullish=(direction != "short"),
        ignored_sources=ignored_sources,
    )


def count_opposing_sources(
    symbol: str,
    sources: dict[str, str],
    direction: str,
    *,
    ignored_sources: frozenset[str] | set[str] | None = None,
) -> int:
    """How many independent seats took the side OPPOSITE `direction`.

    The exact mirror of `count_aligned_sources`: on a long it counts bearish
    stances, on a short bullish ones, through the same `stance_is_aligned`
    vocabulary (inverse-ETF macro flip included). Neutral and mixed stances
    are in NEITHER count — a seat with no view took no side.

    Reported on its own (PM prompt, constructor log, order note) as well as
    consumed by `signed_source_score`, because "2 for, 1 against" and
    "net +1" are different facts about the same evidence and a reader wants
    both. The SIZING consumer is the score, never this count alone.
    """
    return _count_sources(
        symbol,
        sources,
        wants_bullish=(direction == "short"),
        ignored_sources=ignored_sources,
    )


def agreement_refuses_trade(score: int) -> bool:
    """Does the net evidence REFUSE this trade outright? Not a ceiling.

    True for a signed source score at or below zero — a name with no net
    evidence for the direction proposed, or with net dissent against it. The
    caller maps that to `SizeOverride.no_trading()`: the target is dropped,
    no order is built, and anything already held is left exactly where it is
    (refusing to BUY is not a decision to SELL).

    False for any net score of 1 or more, and that is the WHOLE of what
    agreement does to size now. There is no rung, no ladder and no per-score
    number: a target that clears the refusal is bounded by the ratified
    per-trade envelope (`RiskConfig.max_position_risk_pct`) and by the
    portfolio budget allocator, exactly like every other target.

    **Why the graduated ceiling was retired (owner decision, 2026-09-14.)**
    Until this change §9.4 scaled permitted risk by the net seat count as
    `max_position_risk_pct x sqrt(n / 5)`. The square-root law is the
    statistics of averaging INDEPENDENT estimates, and this desk's seats are
    not independent: technical, news, earnings, macro and smart_money read
    overlapping evidence (the same tape, the same bars, the same filings)
    and several are the same underlying model behind different prompts. The
    archetype's one precondition is unmet, so the schedule could not be
    justified at any slope — and no honest correlation haircut exists to
    replace it with, only an invented number.

    Secondarily, a graduated ceiling cannot tell "the seats disagreed" from
    "the seats had nothing to look at". A thinly-covered name and a
    contested one arrive at the same low net score and were sized the same.
    That is the identical defect already fixed in the rotation rule (retired
    board item 66), and the refusal below is the only place the distinction
    is safe to act on: at or below zero the desk declines, and above zero it
    does not pretend to grade.

    What agreement still does, unchanged: it ORDERS which candidates get
    funded first, through `src/verdicts.py::rank_verdicts` and
    `allocate_risk_budget`'s `priority` (retired board item 49). Agreement
    earns the QUEUE POSITION. It no longer sets the size.
    """
    return score <= 0
