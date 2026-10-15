"""Phase 9 — bounded nomination collection, cross-seat dedupe, and ranking.

`docs/QAMC_REMEDIATION_SPEC.md` §9.1/§9.2: before this, Technical was the
only seat that could originate a trade idea; every other seat could only
rate what Technical had already picked. A nomination lets News, Earnings
or Macro ask the desk to look at a symbol Technical did not already cover
— but the ask has to be bounded, or "any seat can nominate" degenerates
into "every seat re-originates the whole universe".

This module is pure logic only: no I/O, no LLM calls, no broker/market
access. That is deliberate — it keeps the cap/dedupe/ranking rules unit
testable without constructing a `TradingPipeline`. The deterministic
external-symbol admission gates (broker eligibility, price, liquidity,
history, sector — shared with the smart-money transient-admission lane)
live on `AdmissionService` (`_evaluate_external_admission_gates`),
since they need live broker/market access; this module only decides
WHICH symbols are worth gating, and in what order.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from src.models import Nomination

#: Ordinal ranking for conviction comparisons — higher sorts first.
_CONVICTION_RANK = {"high": 3, "medium": 2, "low": 1}


def _conviction_rank(conviction: str) -> int:
    return _CONVICTION_RANK.get(conviction, 0)


@dataclass
class NominationCandidate:
    """One symbol after cross-seat merge — the unit the responder pass acts on.

    `conviction` is the HIGHEST conviction any nominating seat assigned
    (spec: rank by conviction, and a symbol two seats both like should not
    be punished for one seat hedging lower). `seats` and `observations`
    preserve every nominator so the conviction ledger records who asked
    and why, not just the merged verdict.
    """

    symbol: str
    conviction: str
    seats: list[str] = field(default_factory=list)
    observations: dict[str, str] = field(default_factory=dict)  # seat -> observation


def _cap_per_seat(nominations: list[Nomination], cap: int) -> list[Nomination]:
    """Deterministic per-seat truncation: highest conviction first, then
    alphabetically by symbol. Applied BEFORE cross-seat dedupe (spec D3) —
    a seat that nominates 5 names when the cap is 3 only ever contributes
    its best 3, regardless of the order the LLM happened to list them in.
    """
    ranked = sorted(
        nominations,
        key=lambda n: (-_conviction_rank(n.conviction), n.symbol),
    )
    return ranked[:cap]


def _merge_across_seats(
    nominations_by_seat: dict[str, list[Nomination]],
    *,
    max_per_seat: int,
) -> dict[str, NominationCandidate]:
    """Per-seat cap, then cross-seat merge, in a fixed seat order.

    Sorted seat names so merge order never depends on the caller's dict
    construction order — seats collected as {"macro_analyst": [...],
    "news_analyst": [...]} must merge exactly as the reverse order does.
    """
    merged: dict[str, NominationCandidate] = {}
    for seat in sorted(nominations_by_seat):
        capped = _cap_per_seat(nominations_by_seat[seat] or [], max_per_seat)
        for nomination in capped:
            symbol = nomination.symbol.strip().upper()
            if not symbol:
                continue
            candidate = merged.get(symbol)
            if candidate is None:
                candidate = NominationCandidate(symbol=symbol, conviction=nomination.conviction)
                merged[symbol] = candidate
            if seat not in candidate.seats:
                candidate.seats.append(seat)
            candidate.observations[seat] = nomination.observation
            if _conviction_rank(nomination.conviction) > _conviction_rank(candidate.conviction):
                candidate.conviction = nomination.conviction

    return merged


def select_nominations(
    nominations_by_seat: dict[str, list[Nomination]],
    *,
    max_per_seat: int,
) -> list[NominationCandidate]:
    """Apply the per-seat cap, dedupe across seats, then rank (no global cap).

    Deterministic: the same nominations in a different input order — either
    the seat-dict's own order or the order within one seat's list — always
    produce the same selection, because both the per-seat cap and the final
    ranking sort on stable, order-independent keys (conviction, seat count,
    symbol) rather than list position, and seats are iterated in a fixed
    (sorted) order during the merge.

    Ranking: highest conviction first, then the number
    of independent nominating seats (more agreement outranks one seat's
    say-so), then alphabetically by symbol. No randomness anywhere.
    """
    merged = _merge_across_seats(nominations_by_seat, max_per_seat=max_per_seat)
    ranked = sorted(
        merged.values(),
        key=lambda c: (-_conviction_rank(c.conviction), -len(c.seats), c.symbol),
    )
    return ranked


def measure_cap_demand(
    nominations_by_seat: dict[str, list[Nomination]],
    *,
    max_per_seat: int,
) -> dict:
    """What the per-seat nomination cap actually costs on this run.

    The ledger row for `nominations.max_per_seat_per_run` (3) is an
    unsourced round number that cannot be settled by argument: the open question is whether
    offered demand ever REACHES the cap, and what gets discarded when it
    does. The evidence already persisted (`raw_by_seat`, plus the symbols
    that survived) cannot answer that, because the surviving set is taken
    after the external-admission gate too — a name missing from it may
    have been refused by the gate rather than cut by a cap.

    So this records the cut itself, at the only place it happens: per
    seat, how many were offered, how many the per-seat cap kept and which
    symbols it dropped with the conviction each carried; then, after the
    cross-seat merge, how many distinct names stood. Pure measurement — nothing here changes what is selected.
    A run of sessions where `per_seat_bound` is ever true is evidence for
    the cap. The daily AI spend cap, checked before every call, bounds total spend.
    """
    per_seat: dict[str, dict] = {}
    for seat in sorted(nominations_by_seat):
        offered = list(nominations_by_seat[seat] or [])
        kept = _cap_per_seat(offered, max_per_seat)
        kept_symbols = {n.symbol.strip().upper() for n in kept}
        dropped = [n for n in offered if n.symbol.strip().upper() not in kept_symbols]
        per_seat[seat] = {
            "offered": len(offered),
            "kept": len(kept),
            "dropped": [
                {"symbol": n.symbol.strip().upper(), "conviction": n.conviction}
                for n in sorted(dropped, key=lambda n: n.symbol)
            ],
            "bound": len(offered) > max_per_seat,
        }

    merged = _merge_across_seats(nominations_by_seat, max_per_seat=max_per_seat)
    ranked = sorted(
        merged.values(),
        key=lambda c: (-_conviction_rank(c.conviction), -len(c.seats), c.symbol),
    )
    return {
        "caps": {"max_per_seat": max_per_seat},
        "per_seat": per_seat,
        "per_seat_bound": sorted(s for s, d in per_seat.items() if d["bound"]),
        "distinct_after_merge": len(ranked),
    }
