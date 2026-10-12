"""Phase 9 §9.5 — the conviction ledger: who took which side, and what it cost.

`docs/QAMC_REMEDIATION_SPEC.md` §9.5 asks for "who nominated it, who
confirmed, who dissented and on what grounds". §9.1/§9.2 already record the
first half: every raw nomination is persisted as a `pipeline_event` evidence
row carrying seat, conviction and observation. What was missing was the other
three quarters — the JOIN from a nomination to the decision and trade it
became, an explicit record of the seats that argued the OTHER way, and any
scoring of either against a realized outcome.

This module is the pure-logic half of that, and pure logic ONLY: no I/O, no
DB handle, no LLM calls, no broker access. Same posture and same reason as
`src/nominations.py` — the scoring and aggregation rules are the part worth
testing, and they should be testable without constructing a `Database` or a
`TradingPipeline`. The persistence side lives on `Database`
(`record_seat_stances`, `resolve_conviction_ledger`, `get_conviction_credits`).

**Advisory only.** Nothing here is read by the trading decision chain. No
function in this module is called from sizing, risk allocation, or order
construction, and none of them may become so without a separate ratified
decision — the spec is explicit that the ledger exists to make the desk
legible to the operator, not to change what the desk trades.

Five ideas:

**A stance is a side taken.** A seat that rated a symbol bullish while the
desk went long SUPPORTED the trade; one that rated it bearish OPPOSED it. A
neutral seat took no side and is not scored — crediting "no view" either way
would let a seat accumulate a record by abstaining. Alignment is decided by
`src/risk/rules.py::stance_is_aligned`, the same vocabulary the §9.4
agreement refusal and `validate_grounding` already share; a second, divergent
notion of "aligned" here would let the ledger and the sizing gate disagree
about identical evidence.

**Credit is raw signed R. Nothing weights it.** A supporter of a trade that
made +2R scores +2R; an opposer of that same trade scores -2R. If the trade
had instead lost 1R, the opposer would score +1R — being right to argue
against a loser is the whole point of recording dissent. `r_multiple`
(`src/risk/metrics.py`) is the measure: profit in units of the risk originally
taken, against the stop the position was OPENED with.

This reverses an earlier design in which credit was multiplied by the seat's
own declared conviction (high 1.0 / medium 0.6 / low 0.3). **Owner decision,
2026-08-31**, for two reasons that live here so nobody reinstates the weight:

1. **It is circular.** The ledger exists to discover whether an analyst's
   declared confidence predicts anything. Multiplying its credit by its own
   confidence assumes that answer and bakes it into the measurement. This desk
   has already seen high-conviction trades underperform low-conviction ones at
   small sample (see `_CONVICTION_OUTCOME_MIN_N` in `src/storage/analytics/calibration.py`);
   under weighting that finding would have been hidden inside the score.
2. ~~**It double-counts.** A confident call already earns a larger position
   through the §9.4 agreement ceiling (retired 2026-09-14), and a larger position already produces
   a proportionally larger R.~~ **WRONG, corrected 2026-09-02.** This was
   false when it was written and is still false. §9.4 never read a conviction:
   `count_aligned_sources` / `count_opposing_sources` collapse each seat to
   `stance_is_aligned`, a pure polarity test, and `signed_source_score` nets
   them at unit weight. A `high`-conviction bullish stance and a `low`-
   conviction one are the same `+1`. Nothing in the sizing path has ever
   multiplied by a declared confidence, so weighting the credit here would
   have charged confidence ONCE, not twice — the objection does not hold.

**The decision does not change.** Reason 1 stands on its own and is sufficient:
a measurement that multiplies a seat's credit by that seat's own confidence
assumes the answer it exists to discover. There is now also a second, harder
reason — there is no sample to derive a weight FROM. Deriving one needs
`_CONVICTION_OUTCOME_MIN_N` (20, `src/storage/analytics/calibration.py`) resolved calls per seat;
the book has 7 closed equity round-trips and every one carries conviction NULL.
So: no conviction weighting, for one valid reason plus insufficient sample.

A confidence weight could legitimately be introduced later — but only one
DERIVED from an analyst's own measured history, never one chosen up front.
Deriving it needs the breakdown below to exist first, which is the point of it.
The same rule now binds the §9.4 sizing path: `src/risk/rules.py::SEAT_WEIGHT`
pins every seat at unit magnitude and `tests/test_signed_dissent.py` fails if
anyone introduces a per-seat weight while the sample is still missing.

**Confidence is reported, not applied.** The declared conviction is still
recorded on every credit row; it simply stops multiplying anything. Instead,
`aggregate_seat_records` breaks each analyst's record down BY the confidence
it declared (`SeatRecord.by_confidence`): resolved calls, calls right, average
win, average loss and cumulative total, separately for each level that analyst
used. "Does this analyst's high confidence earn more?" becomes something a
reader can see, rather than something this module asserts.

**Direction is arithmetic, never sign convention.** A short is scored exactly
as a long is. A short that made money is a WIN and a POSITIVE number; a short
that lost money is a LOSS and a NEGATIVE number; an analyst that argued FOR a
profitable short is credited and one that argued AGAINST it is charged —
identically to a long. Direction changes only how profit is computed from
prices (`r_multiple` takes the side from a signed qty), never the sign of a
credit, never the wording, never which side of zero anything lands on. Nothing
here — or anywhere a human reads this output — inverts, negates or
special-cases a short. **Owner decision, 2026-08-31.**

**No thresholds.** `aggregate_seat_records` returns raw counts and raw
averages with no minimum-sample gate, deliberately. Owner decision: return
the numbers and let the reader judge whether four resolved calls mean
anything, rather than hiding them behind a threshold this desk has no
evidence for.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field

from src.risk.rules import stance_is_aligned

__all__ = [
    "CONFIDENCE_ORDER",
    "DEFAULT_CONVICTION",
    "SeatStance",
    "SeatCredit",
    "ConfidenceRecord",
    "SeatRecord",
    "ClosedPosition",
    "normalize_seat",
    "normalize_conviction",
    "score_position",
    "aggregate_seat_records",
    "summarize_closed_position",
]


#: There is deliberately NO weight table here. Credit is raw signed R and a
#: declared conviction multiplies nothing — see the module docstring for the
#: owner's two reasons (2026-08-31). Reinstating a weight would need a NEW
#: owner decision AND a scale derived from measured history, not chosen.

#: The declared-confidence levels `Nomination.conviction` uses, in the order a
#: reader expects to see them. Only an ordering for display: an analyst that
#: declares something outside this list still gets its own breakdown row,
#: sorted after these, rather than being folded into one of them.
CONFIDENCE_ORDER: tuple[str, ...] = ("high", "medium", "low")

#: Recorded when a seat took a side but declared no conviction — every seat
#: that did not NOMINATE the symbol is in this case, since a stance in the
#: evidence registry carries a direction but no strength. It is a LABEL, not a
#: multiplier: it decides which breakdown row the call is reported under and
#: nothing else.
DEFAULT_CONVICTION = "medium"

#: Nomination seat names (`_collect_seat_nominations`) → evidence-registry
#: source names (`PortfolioManagerAgent.build_evidence_registry`). The same
#: desk seat is called `news_analyst` on the nomination side and `news` on the
#: registry side; the ledger has to hold both against one identity or a seat's
#: nominations and its stances score as two different analysts.
_SEAT_ALIASES: dict[str, str] = {
    "news_analyst": "news",
    "macro_analyst": "macro",
    "earnings_analyst": "earnings",
    "tech_analyst": "technical",
    "technical_analyst": "technical",
    "smart_money_analyst": "smart_money",
}


def normalize_seat(seat: str) -> str:
    """One canonical name per desk seat, whichever side of the pipeline
    supplied it. Unknown names pass through lowercased rather than being
    dropped — a new seat should appear in the ledger under its own name, not
    vanish from it."""
    key = str(seat or "").strip().lower()
    return _SEAT_ALIASES.get(key, key)


def normalize_conviction(conviction: str | None) -> str:
    """The declared confidence, lowercased, or `DEFAULT_CONVICTION` when the
    seat declared none. Purely a label for the per-confidence breakdown — it
    scales no number. An unrecognized word is kept as itself rather than
    collapsed into a known level, so a new conviction vocabulary shows up in
    the breakdown instead of silently landing in the "medium" bucket."""
    key = str(conviction or "").strip().lower()
    return key or DEFAULT_CONVICTION


@dataclass(frozen=True)
class SeatStance:
    """One seat's recorded side on one idea, at the moment the desk decided.

    `stance` is the canonical evidence-registry stance ("buy", "bearish",
    "underweight", ...). `conviction` is what the seat DECLARED when it
    nominated the symbol, or `DEFAULT_CONVICTION` when it only rated a symbol
    someone else raised. `nominated` distinguishes "asked the desk to look at
    this" from "was asked about it and answered" — §9.5 wants both, and they
    are not the same claim.
    """

    seat: str
    symbol: str
    stance: str
    conviction: str = DEFAULT_CONVICTION
    nominated: bool = False
    observation: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "seat", normalize_seat(self.seat))
        object.__setattr__(self, "symbol", str(self.symbol or "").strip().upper())
        object.__setattr__(self, "stance", str(self.stance or "").strip().lower())


@dataclass(frozen=True)
class SeatCredit:
    """One seat's scored outcome on one resolved idea.

    `credit` is the raw signed score: `+r_multiple` for a supporter,
    `-r_multiple` for an opposer, and nothing multiplies it. `r_multiple` is
    the realized R of the trade itself, identical across every seat on the
    same position and unsigned by side, so `credit` is always recoverable
    from `(r_multiple, side)` alone.

    `conviction` is what the seat DECLARED. It is carried for reporting — it
    is what `aggregate_seat_records` breaks the record down BY — and it scales
    nothing. There is no `weight` field: the one that used to sit here was
    removed by owner decision on 2026-08-31 (module docstring).

    `direction` records whether the desk went long or short. It is
    descriptive: the sign of `credit` means the same thing either way.
    """

    seat: str
    symbol: str
    side: str  # "supported" | "opposed"
    stance: str
    conviction: str
    r_multiple: float
    credit: float
    resolved_at: str = ""
    position_id: str | None = None
    decision_id: str | None = None
    direction: str = "long"
    nominated: bool = False


@dataclass(frozen=True)
class ConfidenceRecord:
    """One seat's record over only the calls it made at ONE declared
    confidence — the replacement for the conviction weight.

    Same five figures as the seat's overall record, restricted to a single
    declared level. Nothing here is compared, ranked or scored against
    another level: the point is to SHOW whether an analyst's high-confidence
    calls actually earn more than its low-confidence ones, not to decide it.
    """

    conviction: str
    resolved_calls: int
    calls_right: int
    avg_win: float | None
    avg_loss: float | None
    cumulative_credit: float

    @property
    def win_rate_pct(self) -> float | None:
        if self.resolved_calls <= 0:
            return None
        return round(self.calls_right / self.resolved_calls * 100, 2)


@dataclass(frozen=True)
class SeatRecord:
    """One seat's aggregate record. Raw numbers, no gate — see module docstring.

    `cumulative` is the seat's running credit total after each resolved call,
    oldest first, as (resolved_at, cumulative_credit) pairs — the profit
    series §9.5 asks for. `peak` is the highest that series ever reached
    INCLUDING the zero it starts from, so a seat whose every call lost money
    is in drawdown from 0.0 rather than from its first (negative) point.

    `by_confidence` splits the same calls by the confidence the seat DECLARED
    on each one, ordered `CONFIDENCE_ORDER` first and anything else after.
    Only levels this seat actually used appear — an empty row for a level it
    never declared would read as a record of zero rather than as no record.
    """

    seat: str
    resolved_calls: int
    calls_right: int
    avg_win: float | None
    avg_loss: float | None
    cumulative_credit: float
    cumulative: list[tuple[str, float]] = field(default_factory=list)
    peak: float = 0.0
    current_drawdown: float = 0.0
    by_confidence: list[ConfidenceRecord] = field(default_factory=list)

    @property
    def win_rate_pct(self) -> float | None:
        """Share of resolved calls that scored positive, or None with no calls.
        A zero-R call (the trade closed exactly at entry) counts as resolved
        and not-right, which is the honest reading: the seat's side made no
        money."""
        if self.resolved_calls <= 0:
            return None
        return round(self.calls_right / self.resolved_calls * 100, 2)


@dataclass(frozen=True)
class ClosedPosition:
    """A position chain that has gone flat, reduced to what scoring needs.

    Produced by `summarize_closed_position` from one `position_id`'s trades
    rows. `initial_stop` is the stop the position was OPENED with — the
    R-multiple denominator — and is None when no entry row recorded one, in
    which case the position is not scorable and is skipped rather than
    scored against a fabricated risk.
    """

    position_id: str
    symbol: str
    direction: str
    decision_id: str | None
    entry_price: float
    exit_price: float
    initial_stop: float | None
    qty: float
    opened_at: str
    closed_at: str


def score_position(
    *,
    symbol: str,
    direction: str,
    r_multiple: float,
    stances: Iterable[SeatStance],
    position_id: str | None = None,
    decision_id: str | None = None,
    resolved_at: str = "",
) -> list[SeatCredit]:
    """Credit every seat that took a side on `symbol`, given the realized R.

    Supporters of the direction actually taken score `+r_multiple`; opposers
    score `-r_multiple`. Raw and unweighted: the seat's declared conviction is
    carried onto the row but multiplies nothing (module docstring, owner
    decision 2026-08-31). A trade that lost therefore pays its dissenters,
    which is the asymmetry §9.5 exists to capture.

    `direction` selects which stances count as support and nothing else. A
    short is scored identically to a long — `r_multiple` is already computed
    with the position's own side, so a profitable short arrives here as a
    POSITIVE R and its supporters are credited positively, exactly as they
    would be on a profitable long. No sign is flipped for direction here or
    anywhere downstream.

    Seats whose stance is neither bullish nor bearish (`neutral`, an empty
    string, an unrecognized word) took no side and produce no credit row.
    Deterministic: output is sorted by (seat, symbol) so two identical inputs
    in a different order persist identical rows.
    """
    wants_bullish = str(direction or "long").strip().lower() != "short"
    out: list[SeatCredit] = []
    for stance in stances:
        if stance.symbol and stance.symbol != str(symbol).strip().upper():
            continue
        supported = stance_is_aligned(
            stance.seat,
            symbol,
            stance.stance,
            wants_bullish=wants_bullish,
        )
        opposed = stance_is_aligned(
            stance.seat,
            symbol,
            stance.stance,
            wants_bullish=not wants_bullish,
        )
        if supported == opposed:
            # Neither (neutral / unknown stance) or — impossible with the
            # current disjoint vocabularies — both. No side taken, no credit.
            continue
        signed_r = r_multiple if supported else -r_multiple
        out.append(
            SeatCredit(
                seat=stance.seat,
                symbol=str(symbol).strip().upper(),
                side="supported" if supported else "opposed",
                stance=stance.stance,
                conviction=normalize_conviction(stance.conviction),
                r_multiple=round(float(r_multiple), 4),
                credit=round(signed_r, 4),
                resolved_at=resolved_at,
                position_id=position_id,
                decision_id=decision_id,
                direction="short" if not wants_bullish else "long",
                nominated=bool(stance.nominated),
            )
        )
    return sorted(out, key=lambda c: (c.seat, c.symbol))


def aggregate_seat_records(credits: Iterable[SeatCredit]) -> dict[str, SeatRecord]:
    """Per-seat record over resolved calls: counts, averages, profit series,
    drawdown from the seat's own peak.

    Pure: takes already-scored credits and returns arithmetic. No I/O, no
    thresholds, no minimum sample size — `resolved_calls` is returned raw so
    the reader can judge whether the rest of the row means anything.

    Ordering of the cumulative series is by (`resolved_at`, `position_id`,
    `symbol`), so it is stable and reproducible regardless of the order rows
    came back from storage. `avg_loss` is returned NEGATIVE (the mean of the
    losing credits as they were scored), not as a magnitude.

    `by_confidence` carries the same five figures per declared confidence
    level — the reporting that replaced the conviction weight. It is a split
    of the same rows, so its `resolved_calls` sum to the seat's own and its
    `cumulative_credit` sum to the seat's total.
    """
    by_seat: dict[str, list[SeatCredit]] = {}
    for credit in credits:
        by_seat.setdefault(credit.seat, []).append(credit)

    records: dict[str, SeatRecord] = {}
    for seat, rows in by_seat.items():
        ordered = sorted(
            rows,
            key=lambda c: (c.resolved_at or "", c.position_id or "", c.symbol),
        )
        wins = [c.credit for c in ordered if c.credit > 0]
        losses = [c.credit for c in ordered if c.credit < 0]
        by_conviction: dict[str, list[SeatCredit]] = {}
        for credit in ordered:
            by_conviction.setdefault(
                normalize_conviction(credit.conviction),
                [],
            ).append(credit)
        running = 0.0
        peak = 0.0
        series: list[tuple[str, float]] = []
        for credit in ordered:
            running += credit.credit
            series.append((credit.resolved_at or "", round(running, 4)))
            peak = max(peak, running)
        records[seat] = SeatRecord(
            seat=seat,
            resolved_calls=len(ordered),
            calls_right=len(wins),
            avg_win=round(sum(wins) / len(wins), 4) if wins else None,
            avg_loss=round(sum(losses) / len(losses), 4) if losses else None,
            cumulative_credit=round(running, 4),
            cumulative=series,
            peak=round(peak, 4),
            current_drawdown=round(max(0.0, peak - running), 4),
            by_confidence=[
                _confidence_record(level, by_conviction[level])
                for level in sorted(by_conviction, key=_confidence_sort_key)
            ],
        )
    return records


def _confidence_sort_key(conviction: str) -> tuple[int, str]:
    """`CONFIDENCE_ORDER` first, in that order; anything else after, by name."""
    try:
        return (CONFIDENCE_ORDER.index(conviction), "")
    except ValueError:
        return (len(CONFIDENCE_ORDER), conviction)


def _confidence_record(conviction: str, rows: list[SeatCredit]) -> ConfidenceRecord:
    wins = [c.credit for c in rows if c.credit > 0]
    losses = [c.credit for c in rows if c.credit < 0]
    return ConfidenceRecord(
        conviction=conviction,
        resolved_calls=len(rows),
        calls_right=len(wins),
        avg_win=round(sum(wins) / len(wins), 4) if wins else None,
        avg_loss=round(sum(losses) / len(losses), 4) if losses else None,
        cumulative_credit=round(sum(c.credit for c in rows), 4),
    )


def _executed_qty(row: dict) -> float:
    """Shares this row actually moved. Prefers the reconciled fill over the
    requested quantity, mirroring `compute_trade_calibration`."""
    try:
        fill = float(row.get("fill_qty") or 0)
    except (TypeError, ValueError):
        fill = 0.0
    if fill > 0:
        return fill
    try:
        return float(row.get("qty") or 0)
    except (TypeError, ValueError):
        return 0.0


def _executed_price(row: dict) -> float:
    try:
        fill = float(row.get("fill_price") or 0)
    except (TypeError, ValueError):
        fill = 0.0
    if fill > 0:
        return fill
    try:
        return float(row.get("price") or 0)
    except (TypeError, ValueError):
        return 0.0


#: Opening actions, mapped to the direction the position carries. Both sides
#: are live: `_assign_position_ids` (src/storage/db.py) mints a chain on a
#: SHORT exactly as it does on a BUY, and a COVER retires it exactly as a SELL
#: retires a long (owner decision, 2026-08-31 — shorts count identically to
#: longs). The `qty` this function returns is NEGATIVE for a short, which is
#: the only place direction is expressed: it is what tells `r_multiple` which
#: side the position was, so a profitable short comes back as a positive R.
_OPEN_ACTIONS: dict[str, str] = {"BUY": "long", "SHORT": "short"}


def summarize_closed_position(rows: list[dict]) -> ClosedPosition | None:
    """Reduce one position chain's trades rows to a scorable round trip.

    `rows` are every `trades` row sharing one `position_id`, oldest first,
    each a dict with at least symbol/action/qty/price/fill_qty/fill_price/
    fill_status/stop_loss/decision_id/timestamp.

    Returns None — never a guess — when the chain is not scorable:
      - it never opened (no BUY/SHORT row),
      - it is still open (executed exit quantity has not retired the entries),
      - entry or exit price is missing or non-positive,
      - or the exit rows moved no shares (a placed-but-never-filled
        TRAIL_STOP is protection sitting there, not an exit — the same
        distinction `_is_filled_trail_stop` already draws).

    A chain with no `stop_loss` on any entry row IS returned, with
    `initial_stop=None`; the caller skips it rather than inventing a
    denominator, and the distinction is worth preserving for the caller's
    own reporting.
    """
    from src.execution.stop_records import recorded_initial_stop

    opens: list[dict] = []
    exits: list[dict] = []
    direction: str | None = None
    for row in rows:
        action = str(row.get("action") or "").upper()
        if action in _OPEN_ACTIONS:
            if direction is None:
                direction = _OPEN_ACTIONS[action]
            opens.append(row)
        elif action == "TRAIL_STOP":
            # Only a FILLED trail stop closed anything.
            status = str(row.get("fill_status") or "").lower()
            filled = status == "filled" or (status == "" and _executed_qty(row) > 0)
            if filled and _executed_qty(row) > 0:
                exits.append(row)
        elif action:
            exits.append(row)

    if not opens or direction is None or not exits:
        return None

    open_qty = sum(_executed_qty(r) for r in opens)
    exit_qty = sum(_executed_qty(r) for r in exits)
    if open_qty <= 0 or exit_qty <= 0:
        return None
    if exit_qty + 1e-6 < open_qty:
        return None  # still open — outcome is not known yet

    def _vwap(chain: list[dict]) -> float:
        total_qty = sum(_executed_qty(r) for r in chain)
        if total_qty <= 0:
            return 0.0
        return sum(_executed_qty(r) * _executed_price(r) for r in chain) / total_qty

    entry_price = _vwap(opens)
    exit_price = _vwap(exits)
    if entry_price <= 0 or exit_price <= 0:
        return None

    initial_stop: float | None = None
    for row in opens:
        stop = recorded_initial_stop(row)
        if stop > 0:
            initial_stop = stop
            break

    decision_id = next(
        (row.get("decision_id") for row in opens if row.get("decision_id")),
        None,
    )
    position_id = next(
        (row.get("position_id") for row in rows if row.get("position_id")),
        "",
    )
    return ClosedPosition(
        position_id=str(position_id or ""),
        symbol=str(opens[0].get("symbol") or "").strip().upper(),
        direction=direction,
        decision_id=decision_id,
        entry_price=entry_price,
        exit_price=exit_price,
        initial_stop=initial_stop,
        qty=open_qty if direction == "long" else -open_qty,
        opened_at=str(opens[0].get("timestamp") or ""),
        closed_at=str(exits[-1].get("timestamp") or ""),
    )
