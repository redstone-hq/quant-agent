"""Read-only analyst scorecard over the conviction ledger (spec §9.5).

One GET route, `/analysts/scorecard`. It reads the ledger's persisted
`conviction_credit` and `seat_stance` evidence rows through
`db_reads.get_conviction_ledger()` — an independent SQLite `mode=ro`
connection, like every other read in this package — and projects them into
the shape the Mission Control panel renders.

**It scores nothing.** The realized `r_multiple` and the alignment call
(`side`) are decided by the ledger layer when a position closes and written
down; this module only adds them up. Concretely: no `r_multiple` is computed
here and no stance is re-classified as supporting or opposing.

**Credit is raw signed R.** There is no conviction weight to apply — it was
removed by owner decision on 2026-08-31, because weighting an analyst's credit
by its own declared confidence assumes the very thing the ledger exists to
measure, and because a confident call already earns a bigger position and so a
bigger R. What replaced it is `by_confidence`: each analyst's record split by
the confidence it declared, so a reader can see whether its confident calls
actually earn more instead of being told so by a multiplier.

**A short reads exactly like a long.** Direction is carried on an idea for
description only. A short that made money is a positive number and a win; the
analyst that argued for it is credited and the one that argued against it is
charged — the same words, the same sign, the same side of zero as a long.
Nothing in this module or the panel it feeds inverts anything on direction.

**Why the arithmetic below is not imported from the ledger.**
`src.conviction_ledger.aggregate_seat_records` produces the same per-analyst
totals, and importing it would be the obvious move — but that module imports
`src.risk.rules`, and `tests/test_api_safety.py` forbids any `src.risk` import
from `src/api/` (the Stage 2 isolation invariant recorded in
`docs/architecture/MISSION_CONTROL_API.md`). Preserving the isolation contract
wins over avoiding the duplication, so the running-total/peak/average
arithmetic is mirrored here instead. `tests/test_api_scorecard.py` carries a
parity test that compares this projection against `aggregate_seat_records`
whenever that module is importable, so the two cannot silently drift apart.

**Advisory only, and read-only twice over.** Per §9.5 item 6 no score here
feeds sizing, and per the Mission Control contract this process cannot write
anything at all. Per §9.5 item 8 there is no minimum-sample gate: raw counts
are returned for every analyst, however few calls it has resolved.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime

from fastapi import APIRouter, Query

from src.api import db_reads
from src.api.schemas import (
    AnalystScorecardItem,
    AnalystScorecardResponse,
    ScorecardConfidenceBreakdown,
    ScorecardIdea,
    ScorecardIdeaAnalyst,
    ScorecardMonthPoint,
    ScorecardPoint,
    TargetScorecardResponse,
)

router = APIRouter()

#: Default number of resolved ideas returned in the trace list, newest first.
DEFAULT_IDEA_LIMIT = 25
MAX_IDEA_LIMIT = 200

#: The notional risk per call used to express R in dollars. Stated once, here,
#: and echoed in the response so the panel never invents its own figure.
RISK_DOLLARS_PER_CALL = 100.0

#: Declared-confidence levels in the order a reader expects, mirroring
#: `src.conviction_ledger.CONFIDENCE_ORDER` (which this package may not
#: import — see the module docstring). Anything an analyst declared that is
#: not in this list sorts after it, under its own name, rather than being
#: folded into one of these.
CONFIDENCE_ORDER: tuple[str, ...] = ("high", "medium", "low")
DEFAULT_CONFIDENCE = "medium"


def _sort_key(credit: dict) -> tuple:
    """Stable ordering for one analyst's calls: oldest first.

    Mirrors `aggregate_seat_records`' key exactly — (resolved_at, position_id,
    symbol) — so a series built here and one built there cannot disagree about
    order, which would silently change every running total and peak.
    """
    return (
        credit.get("resolved_at") or "",
        credit.get("position_id") or "",
        credit.get("symbol") or "",
    )


def _month_of(resolved_at: str) -> str:
    """ "YYYY-MM" from a stored timestamp, or "" when it is unusable.

    The ledger stores whatever the closing trade row carried, which is a naive
    UTC "YYYY-MM-DD HH:MM:SS" string today. Taking the first seven characters
    avoids inventing a timezone the stored value never had; anything shorter
    than a full date is dropped rather than padded into a wrong month.
    """
    text = (resolved_at or "").strip()
    if len(text) < 7 or text[4] != "-":
        return ""
    return text[:7]


def _hit_rate(calls_right: int, resolved_calls: int) -> float | None:
    if resolved_calls <= 0:
        return None
    return round(calls_right / resolved_calls * 100, 2)


def _monthly(ordered: list[dict]) -> list[ScorecardMonthPoint]:
    """Month-by-month steps plus the running total at each month end."""
    per_month: dict[str, list[dict]] = defaultdict(list)
    for credit in ordered:
        month = _month_of(credit.get("resolved_at", ""))
        if month:
            per_month[month].append(credit)

    running = 0.0
    right_so_far = 0
    calls_so_far = 0
    points: list[ScorecardMonthPoint] = []
    for month in sorted(per_month):
        rows = per_month[month]
        step = sum(row["credit"] for row in rows)
        running += step
        calls_so_far += len(rows)
        right_so_far += sum(1 for row in rows if row["credit"] > 0)
        points.append(
            ScorecardMonthPoint(
                month=month,
                credit=round(step, 4),
                cumulative=round(running, 4),
                resolved_calls=len(rows),
                calls_right=sum(1 for row in rows if row["credit"] > 0),
                hit_rate_pct=_hit_rate(right_so_far, calls_so_far),
            )
        )
    return points


def _confidence_key(conviction: str) -> tuple[int, str]:
    try:
        return (CONFIDENCE_ORDER.index(conviction), "")
    except ValueError:
        return (len(CONFIDENCE_ORDER), conviction)


def _by_confidence(ordered: list[dict]) -> list[ScorecardConfidenceBreakdown]:
    """The same calls, split by the confidence the analyst declared on each.

    The replacement for the conviction weight: the split is REPORTED and
    nothing compares or ranks the levels. Rows sum back to the analyst's own
    totals, so a reader can check the split against the headline figure.
    """
    grouped: dict[str, list[dict]] = defaultdict(list)
    for row in ordered:
        grouped[str(row.get("conviction") or "").strip().lower() or DEFAULT_CONFIDENCE].append(row)

    out: list[ScorecardConfidenceBreakdown] = []
    for conviction in sorted(grouped, key=_confidence_key):
        rows = grouped[conviction]
        wins = [row["credit"] for row in rows if row["credit"] > 0]
        losses = [row["credit"] for row in rows if row["credit"] < 0]
        out.append(
            ScorecardConfidenceBreakdown(
                conviction=conviction,
                resolved_calls=len(rows),
                calls_right=len(wins),
                hit_rate_pct=_hit_rate(len(wins), len(rows)),
                avg_win=round(sum(wins) / len(wins), 4) if wins else None,
                avg_loss=round(sum(losses) / len(losses), 4) if losses else None,
                cumulative_credit=round(sum(row["credit"] for row in rows), 4),
            )
        )
    return out


def _analyst_item(analyst: str, credits: list[dict]) -> AnalystScorecardItem:
    ordered = sorted(credits, key=_sort_key)
    wins = [row["credit"] for row in ordered if row["credit"] > 0]
    losses = [row["credit"] for row in ordered if row["credit"] < 0]

    running = 0.0
    peak = 0.0
    #: The moment the peak was last set. None while the analyst has never been
    #: below its own best — a live peak is not a drawdown that started.
    peak_at: str | None = None
    calls_since_peak = 0
    series: list[ScorecardPoint] = []
    for row in ordered:
        running += row["credit"]
        if running >= peak:
            peak = running
            peak_at = row.get("resolved_at") or ""
            calls_since_peak = 0
        else:
            calls_since_peak += 1
        series.append(
            ScorecardPoint(
                resolved_at=row.get("resolved_at") or "",
                cumulative=round(running, 4),
                peak=round(peak, 4),
                below_best=round(max(0.0, peak - running), 4),
            )
        )

    below_best = round(max(0.0, peak - running), 4)
    return AnalystScorecardItem(
        analyst=analyst,
        resolved_calls=len(ordered),
        calls_right=len(wins),
        hit_rate_pct=_hit_rate(len(wins), len(ordered)),
        avg_win=round(sum(wins) / len(wins), 4) if wins else None,
        avg_loss=round(sum(losses) / len(losses), 4) if losses else None,
        cumulative_credit=round(running, 4),
        peak=round(peak, 4),
        below_best=below_best,
        below_best_since=peak_at if below_best > 0 else None,
        calls_since_peak=calls_since_peak if below_best > 0 else 0,
        cumulative=series,
        monthly=_monthly(ordered),
        by_confidence=_by_confidence(ordered),
    )


def _stance_reasons(stances: list[dict]) -> dict[tuple[str, str, str], str]:
    """(decision_id, symbol, analyst) -> the analyst's own stated reason.

    Used only to attach the verbatim `observation` an analyst wrote at
    decision time to its scored credit. Missing keys stay missing; nothing is
    substituted for a reason that was never recorded.
    """
    out: dict[tuple[str, str, str], str] = {}
    for stance in stances:
        key = (
            str(stance.get("decision_id") or ""),
            str(stance.get("symbol") or "").upper(),
            str(stance.get("analyst") or ""),
        )
        if stance.get("observation"):
            out[key] = str(stance["observation"])
    return out


def _ideas(credits: list[dict], stances: list[dict], limit: int) -> list[ScorecardIdea]:
    """Group scored credits back into the idea each one belongs to.

    Every credit row on the same position carries the same `r_multiple` and
    `direction`, so an idea's own result needs no recomputation — it is read
    off the first credit in the group.
    """
    reasons = _stance_reasons(stances)
    grouped: dict[tuple, list[dict]] = defaultdict(list)
    for credit in credits:
        grouped[
            (
                credit.get("position_id") or "",
                credit.get("decision_id") or "",
                credit.get("symbol") or "",
            )
        ].append(credit)

    ideas: list[ScorecardIdea] = []
    for (position_id, decision_id, symbol), rows in grouped.items():
        first = rows[0]

        def _participant(row: dict) -> ScorecardIdeaAnalyst:
            return ScorecardIdeaAnalyst(
                analyst=row["analyst"],
                side=row["side"],
                stance=row["stance"],
                conviction=row["conviction"],
                credit=row["credit"],
                nominated=row["nominated"],
                reason=reasons.get((decision_id, symbol.upper(), row["analyst"]), ""),
            )

        ideas.append(
            ScorecardIdea(
                symbol=symbol,
                direction=first.get("direction") or "long",
                position_id=position_id or None,
                decision_id=decision_id or None,
                resolved_at=first.get("resolved_at") or "",
                r_multiple=first.get("r_multiple", 0.0),
                supported=[_participant(r) for r in rows if r["side"] == "supported"],
                opposed=[_participant(r) for r in rows if r["side"] == "opposed"],
            )
        )

    ideas.sort(key=lambda idea: (idea.resolved_at, idea.symbol), reverse=True)
    return ideas[:limit]


def build_scorecard(ledger: dict, idea_limit: int = DEFAULT_IDEA_LIMIT) -> AnalystScorecardResponse:
    """Pure projection of a `get_conviction_ledger()` result. No I/O.

    Split out from the route handler so the shaping is testable without an
    ASGI client, the same way `funnelShared`/`buildResearchDesk` are on the
    frontend side.
    """
    as_of = datetime.now(UTC).isoformat()
    if ledger.get("read_error"):
        return AnalystScorecardResponse(
            as_of=as_of,
            state="error",
            read_error=str(ledger["read_error"]),
            risk_dollars_per_call=RISK_DOLLARS_PER_CALL,
        )

    credits: list[dict] = ledger.get("credits") or []
    stances: list[dict] = ledger.get("stances") or []
    if not credits:
        return AnalystScorecardResponse(
            as_of=as_of,
            state="empty",
            risk_dollars_per_call=RISK_DOLLARS_PER_CALL,
        )

    per_analyst: dict[str, list[dict]] = defaultdict(list)
    for credit in credits:
        per_analyst[credit["analyst"]].append(credit)

    analysts = [_analyst_item(name, rows) for name, rows in per_analyst.items()]
    # Ranked by money made, best first — the ordering the panel's table opens
    # in. Name breaks ties so equal totals never reorder between polls.
    analysts.sort(key=lambda a: (-a.cumulative_credit, a.analyst))

    months = sorted({month for month in (_month_of(c.get("resolved_at", "")) for c in credits) if month})

    return AnalystScorecardResponse(
        as_of=as_of,
        state="populated",
        risk_dollars_per_call=RISK_DOLLARS_PER_CALL,
        resolved_calls_total=len(credits),
        months=months,
        analysts=analysts,
        ideas=_ideas(credits, stances, idea_limit),
    )


@router.get("/analysts/scorecard", response_model=AnalystScorecardResponse)
def get_analyst_scorecard(
    idea_limit: int = Query(DEFAULT_IDEA_LIMIT, ge=1, le=MAX_IDEA_LIMIT),
) -> AnalystScorecardResponse:
    """Per-analyst record from the conviction ledger. Read-only, advisory.

    Returns HTTP 200 for all three states, including `state="error"` — the
    same typed-degraded-envelope posture `/research/daily/{date}` uses, so the
    panel can render an honest "this could not be read" instead of a blank
    page. Transport/server faults outside this known read condition remain
    ordinary 500s via the app's global handler.
    """
    return build_scorecard(db_reads.get_conviction_ledger(), idea_limit)


@router.get("/analysts/target-scorecard", response_model=TargetScorecardResponse)
def get_target_scorecard() -> TargetScorecardResponse:
    """How often price reached the entry take-profit target. Record only.

    Owner ruling 2026-10-09: the target is not a sell rule; nothing here feeds
    ranking, sizing or exits. "Reached" is a lower bound.
    """
    return TargetScorecardResponse(**db_reads.get_target_scorecard())
