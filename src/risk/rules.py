import logging
import math
import statistics
import sys
from dataclasses import dataclass, field
from src.config import RiskConfig
from src.models import TradeDecision, Position, AnalystVerdict

# The leverage table and the two multiplier functions now live in
# `src.quantities` — a dependency-free module OUTSIDE `src.risk`, so
# `src/api/` (forbidden by tests/test_api_safety.py from importing the risk
# stack) can share this one definition instead of hand-copying it. These
# aliases keep every existing `_ETF_LEVERAGE` / `_effective_multiplier` /
# `_gross_multiplier` reference in this module and its importers working,
# and bind the SAME dict object, so `patch.dict(rules._ETF_LEVERAGE, ...)`
# still reaches every consumer.
from src.quantities import (
    ETF_LEVERAGE as _ETF_LEVERAGE,
    effective_multiplier as _effective_multiplier,
    gross_multiplier as _gross_multiplier,
    net_exposure_pct,
    net_exposure_usd,
)

# The gross ceiling and its de-levering ladder live in `src.risk.gross_ladder`,
# a leaf module (it imports nothing from this one). Imported here at the TOP,
# not in the mirror block at the end, because `GrossCeiling` is used in class
# annotations below; the other names are re-exported for existing callers.
from src.risk.gross_ladder import (  # noqa: F401
    GROSS_LADDER,
    GROSS_LADDER_ALERT_PCT,
    GrossCeiling,
    resolve_gross_ceiling,
)

logger = logging.getLogger(__name__)


#: §9.4 freshness. An earnings stance older than this many days stops
#: COUNTING toward the agreement tally (it is still shown to the PM, and
#: still valid provenance — see `PortfolioManagerAgent.stale_evidence_sources`).
#:
#: Also (2026-09-02) the bound `EarningsDataProvider._get_existing_analysis`
#: (`src/data/earnings.py`) uses to decide whether a disk-cached analysis is
#: even eligible to be re-served as the CURRENT earnings view when no fresher
#: SEC filing exists — past this age that fallback returns None instead, so
#: an over-age analysis never reaches a session in the first place. That is a
#: stricter, earlier check than `stale_evidence_sources` above: this one
#: decides whether a stance is shown at all, that one decides whether a
#: stance that IS shown gets to count. Same threshold, so the two can never
#: disagree about what "too old" means.
#:
#: 90 is not a new number. `config/prompts/earnings_analyst.md` already tells
#: the seat that a filing more than 90 days old must cap its own `conviction`
#: at `low` and flag `stale_filing_<N>d`, and that past 180 days the filing
#: "should not have reached you"; `TradingPipeline._missed_ops_earnings_signal`
#: already refuses anything older than 90 days as "recent earnings evidence".
#: The seat and the reflector had a threshold; only the SIZING path did not
#: read it. Reusing 90 makes the sizing path agree with the opinion the desk
#: had already written down, rather than adding a fourth staleness rule.
EARNINGS_STANCE_MAX_AGE_DAYS = 90


#: §9.4 signed sum. Every seat enters the score at UNIT magnitude — +1 when it
#: is aligned with the proposed direction, -1 when it is opposed, 0 when it is
#: neutral or silent. Equal weighting is not a placeholder for a better number:
#: it is what published composite-index construction literally does, and what
#: Grinold & Kahn's `alpha = volatility x IC x score` reduces to when per-source
#: skill is equal.
#:
#: **Do not replace this with a per-seat weight table.** `src/conviction_ledger.py`
#: records the desk's standing rule (owner, 2026-08-31): a confidence weight may
#: only be DERIVED from an analyst's own measured history, never chosen up front,
#: and `_CONVICTION_OUTCOME_MIN_N` (`src/storage/db.py`) puts the minimum sample
#: at 20 resolved calls. The book has 7 closed equity round-trips, all carrying
#: conviction NULL — there is no sample to derive from, so any weight introduced
#: today would be a chosen one. `tests/test_signed_dissent.py` enforces this
#: mechanically rather than by memory.
SEAT_WEIGHT: int = 1


def signed_source_score(
    symbol: str,
    sources: dict[str, str],
    direction: str,
    *,
    ignored_sources: frozenset[str] | set[str] | None = None,
    non_corroborating_sources: frozenset[str] | set[str] | None = None,
) -> int:
    """`S` — the §9.4 signed sum: aligned seats minus opposed seats.

    `non_corroborating_sources` (board item 109, 2026-09-26) is the ONE-SIDED
    removal: a seat named here may not CORROBORATE the trade, but its dissent
    still counts against it. `ignored_sources` stays two-sided ("a stance too
    stale to corroborate is too stale to dissent"). The two are separate
    parameters, not one merged set, because they are separate facts with
    separate consequences, and merging them silently turned this score's one
    guarantee — that gating a stance can only ever pull the net DOWN — into a
    falsehood: dropping an OPPOSED seat RAISES the net, and a name at net 0
    (refused) becomes net +1 (traded). A fix for a double count must not
    admit trades the desk refuses today, so the broadcast-macro exclusion is
    one-sided. See `PortfolioManagerAgent.broadcast_macro_sources` for why
    that is the right side to take it off.

    `S = sum(s_i)` over the five evidence seats, where `s_i` is `+SEAT_WEIGHT`
    for a seat pointing the way `direction` proposes, `-SEAT_WEIGHT` for one
    pointing the other way, and 0 for a seat that is neutral or absent.

    Expressed as the DIFFERENCE of the two counts rather than as a second
    traversal, on purpose: the aligned and opposed counts are what the PM is
    shown and what the order note records, so the number that sizes the trade
    is arithmetically the same number the reader was given. A second walk over
    `sources` here would be a second definition of "aligned" — the exact defect
    `stance_is_aligned`'s one-vocabulary comment exists to prevent.

    WHAT THIS REPLACED (2026-09-02). §9.4 used to ceiling size on the aligned
    count alone, so a seat that stayed SILENT, a seat that rated NEUTRAL and a
    seat that ACTIVELY DISAGREED all contributed exactly zero. `three aligned`
    and `three aligned, one against` bought identical size. No published
    composite methodology counts only agreers: a disagreeing input enters a
    composite as a negative number in a signed sum, and that is what this is.
    """
    aligned_ignored = frozenset(ignored_sources or ()) | frozenset(non_corroborating_sources or ())
    return SEAT_WEIGHT * (
        count_aligned_sources(
            symbol,
            sources,
            direction,
            ignored_sources=aligned_ignored,
        )
        - count_opposing_sources(
            symbol,
            sources,
            direction,
            ignored_sources=ignored_sources,
        )
    )


#: Name of the deterministic hard-block rule this ceiling raises. Listed in
#: `HARD_BLOCK_RULES` (below in this file) — one string, two places.
GROSS_EXPOSURE_RULE = "max_gross_exposure"

#: WHICH RULES ACTUALLY STOP AN ORDER.
#:
#: Moved here from `src/pipeline.py` on 2026-09-23 (board item 162). It is
#: the ONLY code-level answer to "is this entry a hard limit or an advisory",
#: and the risk seat's renderer (`src/agents/risk_manager.py`) must be able
#: to ask it. That module cannot import `src.pipeline` — `src.pipeline`
#: imports it — so the set had to sit below both. `src.pipeline` re-exports
#: this name, so every existing `from src.pipeline import HARD_BLOCK_RULES`
#: still resolves.
#:
#: Membership is the WHOLE distinction, and it is never derivable from a
#: rule's NAME.

HARD_BLOCK_RULES = {
    "max_total_position_pct",
    "max_position_pct",
    "require_stop_loss",
    "cash_only",
    # Spec §11.2 (owner-ratified 2026-09-01). Gross exposure — long market
    # value plus absolute short market value — may not exceed the ladder-
    # resolved multiple of equity. There was NO gross-exposure ceiling in
    # this codebase before: `max_portfolio_risk_pct` bounds capital at risk
    # and `max_total_position_pct` bounds NET exposure, where a hedge
    # cancels a long. Adding this hard block is a tightening.
    "max_gross_exposure",
}


def distance_to_forced_liquidation_pct(
    gross: float,
    equity: float,
    *,
    maintenance_margin_pct: float = 25.0,
) -> float | None:
    """How far, in percent, the book could fall before the broker liquidates.

    Nothing in this codebase watched this before §11.2 — that was the gap.
    Below the maintenance requirement the broker sells, at the worst moment,
    without asking.

    Let `f` be the fractional fall in every position. Gross becomes
    `G(1-f)`, equity becomes `E - Gf`, and the broker acts when equity drops
    below `m` x gross:

        E - Gf = m x G(1 - f)   ->   f = (E - mG) / (G(1 - m))

    At the ratified 2.0x with 25% maintenance this returns 33.3%, and at
    1.5x it returns 55.6% — the two figures the §11.2 spec entry publishes,
    reproduced rather than restated.

    Returns 100.0 when the book carries no borrowing (the positions can go
    to zero before any margin call), and None when the inputs cannot support
    the arithmetic.
    """
    for value in (gross, equity, maintenance_margin_pct):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        if not math.isfinite(float(value)):
            return None
    gross = float(gross)
    equity = float(equity)
    maintenance = float(maintenance_margin_pct) / 100.0
    if equity <= 0 or not (0.0 < maintenance < 1.0):
        return None
    if gross <= 0:
        return 100.0
    fraction = (equity - maintenance * gross) / (gross * (1.0 - maintenance))
    if fraction >= 1.0:
        return 100.0
    if fraction <= 0.0:
        return 0.0
    return round(fraction * 100, 1)


# Owner mandate, 2026-09-17: "I want 100% invested. I don't want anything
# sitting in T-bills or any other positions that just yield interest." Cash
# earning less than inflation is a loss, and the desk can always go long OR
# short, so there is always something to own. This is a mandate, not a tuned
# number: it is the whole of equity. Macro no longer sets or lowers it — macro
# informs DIRECTION only. Compared against `BookExposure.deployed_pct` by the
# PM facts block and the pre-trade `deployment_gap` advisory, which reports
# UNDER-deployment only and never asks for a book to be scaled down.
DESK_INVESTED_TARGET_PCT = 100.0


@dataclass
class GrossCeilingOutcome:
    """What `apply_gross_ceiling` did, in the order it did it."""

    decisions: list = field(default_factory=list)
    #: Engine-authored SELL / COVER orders. Empty unless the HELD book alone
    #: is over the ceiling.
    trims: list = field(default_factory=list)
    #: Operator-readable lines, one per refusal or reduction, each naming the
    #: rule that fired.
    notes: list = field(default_factory=list)
    #: Symbols whose new exposure was refused outright.
    blocked: list = field(default_factory=list)
    #: {symbol: reason}, one entry per name in `blocked` — the SAME text
    #: appended to `notes`, without the "{GROSS_EXPOSURE_RULE}: {symbol}
    #: refused — " prefix. Board item 10 (2026-09-14): `notes` is relayed
    #: verbatim by `PortfolioConstructor` as `logger.warning("Constructor:
    #: %s", note)`, and every note built that way reads "Constructor:
    #: max_gross_exposure: SYMBOL refused — ..." — the rule name sits
    #: BETWEEN "Constructor:" and the symbol, which `_DropReasonCapture.
    #: _SYMBOL` requires to follow immediately. Every gross-ceiling block was
    #: therefore invisible to that regex. This field lets the constructor
    #: file a structured `_note_refusal` per blocked symbol instead of
    #: relying on the log scrape.
    blocked_detail: dict = field(default_factory=dict)
    ceiling: GrossCeiling | None = None
    ceiling_usd: float = 0.0
    held_gross: float = 0.0
    held_gross_after_exits: float = 0.0
    projected_gross: float = 0.0
    measurable: bool = True


def apply_gross_ceiling(
    decisions,
    positions,
    equity: float,
    ceiling: GrossCeiling,
    *,
    cash_park_symbol: str | None = None,
    # The SIZING gate (`PortfolioConstructor`) sets this False: shrinking an
    # order it is about to propose is its job, authoring a de-lever of the
    # held book is not. One owner for trimming — the session preamble
    # (`TradingPipeline._enforce_gross_ceiling`), which runs before any agent
    # and therefore cannot be disabled by a blank model response.
    emit_trims: bool = True,
    # Item 112 — optional per-symbol CONVICTION cut order for step 3. Maps
    # each held symbol to an ALREADY-BUILT sort key, lowest cut FIRST; this
    # function never computes conviction and never interprets the tuple
    # beyond comparing it. The caller
    # (`TradingPipeline._conviction_cut_order`) builds it from the §9.4
    # yes/no and the desk's own `rank_verdicts` candidate ordering — NOT
    # from the signed source score as a grade, whose graded use was retired
    # (board item 66; see `agreement_refuses_trade`). When None (the default,
    # and every lane with no fresh per-seat read — midday, close, intraday,
    # PM-less morning) the order is byte-identical to before: biggest loser
    # first. It changes only the ORDER trims are taken, never which book is
    # over its ceiling or the never-full-liquidation clamp.
    conviction_rank: dict[str, tuple[int, int]] | None = None,
) -> GrossCeilingOutcome:
    """Enforce the §11.2 gross-exposure ceiling, blocking BEFORE trimming.

    **The ordering is the rule, and it is not optional.** A drawdown must not
    trigger the panic-selling the ladder exists to prevent, so:

    1. Planned exits are counted first — a book already being reduced is
       judged on what it will hold, not on what it holds now.
    2. **New exposure is blocked or shrunk to fit the ceiling.** Every BUY
       and SHORT is rationed against the remaining headroom. Fixed
       2026-09-24: one shrunk to a small but nonzero size is no longer
       refused outright — it is placed at whatever headroom remains (no
       stock commission and fractional shares make a small order fine); only
       a genuine zero (`after <= 0`) is refused.
    3. **Only then**, and only if the HELD book ALONE still exceeds the
       ceiling, are trims emitted. Proposed new exposure is not an input to
       that test, structurally — so the engine can never sell something you
       own to make room for something you do not.

    **This function does not need the Portfolio Manager.** Pass `decisions=[]`
    — the case where the PM returned nothing at all, a measured failure mode
    — and steps 1 and 2 are trivially satisfied while step 3 still trims a
    book that is over its ceiling. That is the whole point of computing the
    ceiling from account state.

    Returns a `GrossCeilingOutcome`. Entry decisions are mutated in place
    (their `allocation_pct` reduced and their `reasoning` annotated) so the AI
    Risk Manager does not read deterministic arithmetic as the PM
    contradicting itself.
    """
    out = GrossCeilingOutcome(
        decisions=list(decisions or []),
        ceiling=ceiling,
    )
    positions = list(positions or [])
    park = (cash_park_symbol or "").strip().upper()

    if (
        not isinstance(ceiling, GrossCeiling)
        or _positive_float(ceiling.ceiling_x) <= 0
        or isinstance(equity, bool)
        or not isinstance(equity, (int, float))
        or not math.isfinite(float(equity))
        or float(equity) <= 0
    ):
        # No trustworthy equity figure means no trustworthy ceiling. Refuse
        # every new position and trim nothing — the same fail-closed posture
        # `RiskRuleEngine.check` takes on a `total_value <= 0` broker blip.
        out.measurable = False
        for decision in out.decisions:
            if decision.action in ("BUY", "SHORT") and decision.allocation_pct > 0:
                decision.allocation_pct = 0.0
                out.blocked.append(decision.symbol)
                detail = (
                    f"the account equity figure ({equity}) is not usable, so "
                    f"the gross-exposure ceiling cannot be computed. No new "
                    f"position opens on an unreadable account."
                )
                out.blocked_detail[decision.symbol] = detail
                out.notes.append(f"{GROSS_EXPOSURE_RULE}: {decision.symbol} refused — {detail}")
        if out.notes:
            logger.warning(
                "Gross-exposure ceiling: equity unusable (%s) — refused %d new position(s)",
                equity,
                len(out.blocked),
            )
        return out

    equity = float(equity)
    out.ceiling_usd = ceiling.ceiling_x * equity
    unmeasurable = unmeasurable_gross_symbols(
        positions,
        cash_park_symbol=park or None,
    )
    out.measurable = not unmeasurable
    out.held_gross = gross_exposure(positions, cash_park_symbol=park or None)

    positions_by_symbol = {str(getattr(p, "symbol", "") or "").strip().upper(): p for p in positions}

    # --- STEP 1: planned exits shrink the book before anything is judged ---
    exit_relief: dict[str, float] = {}
    for decision in out.decisions:
        if decision.action not in ("SELL", "COVER"):
            continue
        if decision.allocation_pct <= 0:
            continue  # allocation_pct == 0 means SKIP, not full exit
        symbol = str(decision.symbol or "").strip().upper()
        if park and symbol == park:
            continue  # unparking cash is not a reduction in exposure
        held = positions_by_symbol.get(symbol)
        if held is None:
            continue
        market_value = getattr(held, "market_value", 0.0) or 0.0
        if not math.isfinite(float(market_value)):
            continue
        position_gross = abs(float(market_value)) * _gross_multiplier(symbol)
        fraction = min(100.0, float(decision.allocation_pct)) / 100.0
        exit_relief[symbol] = max(
            exit_relief.get(symbol, 0.0),
            position_gross * fraction,
        )
    out.held_gross_after_exits = max(
        0.0,
        out.held_gross - sum(exit_relief.values()),
    )

    # --- STEP 2: BLOCK NEW EXPOSURE FIRST ---------------------------------
    headroom = max(0.0, out.ceiling_usd - out.held_gross_after_exits)
    entries = [d for d in out.decisions if d.action in ("BUY", "SHORT") and d.allocation_pct > 0]
    # Largest commitment first, symbol as a deterministic tie-break — the
    # same rationing order `construct_orders` already sorts its BUYs into,
    # so highest conviction gets the scarce headroom.
    entries.sort(key=lambda d: (-float(d.allocation_pct), str(d.symbol)))
    granted = 0.0
    for decision in entries:
        symbol = str(decision.symbol or "").strip().upper()
        multiplier = _gross_multiplier(symbol)
        wanted = equity * (float(decision.allocation_pct) / 100.0) * multiplier
        available = 0.0 if unmeasurable else max(0.0, headroom - granted)
        if wanted <= available + 1e-9:
            granted += wanted
            continue
        if unmeasurable:
            reason = (
                f"the broker returned an unusable market value for "
                f"{', '.join(unmeasurable)}, so gross exposure cannot be "
                f"measured this session"
            )
        else:
            reason = (
                f"the book would own ${out.held_gross_after_exits + granted + wanted:,.0f} "
                f"against a ${out.ceiling_usd:,.0f} ceiling "
                f"({ceiling.ceiling_x:.1f}x equity)"
            )
        before = float(decision.allocation_pct)
        # Fixed 2026-09-24: this used to refuse outright whenever
        # `available / multiplier` (the NOTIONAL the ceiling still allows)
        # was under the flat `min_order_usd` floor — an arbitrary $500 with
        # no broker minimum behind it (config/number_ledger.yaml), justified
        # by a false "pays commission" claim (Alpaca charges none). A small
        # entry is no longer refused for that reason; it is granted whatever
        # headroom is left, however small. Only a genuinely empty headroom
        # (`after <= 0` below) still refuses — that is a real "nothing to
        # buy", not an arbitrary-floor judgement call.
        # The owner's 0.5% minimum RISK per position (owner rule 2026-08-27)
        # is re-applied by the constructor after this shrink
        # (`PortfolioConstructor._refuse_below_min_risk`): this function sees
        # weights, not stops.
        #
        # Round DOWN to 2dp so the granted size can never land back above the
        # headroom that permitted it.
        after = math.floor((available / (equity * multiplier) * 100.0) * 100.0) / 100.0
        if after <= 0:
            decision.allocation_pct = 0.0
            out.blocked.append(decision.symbol)
            detail = f"{reason}, and no headroom is left under the ceiling. {ceiling.reason}"
            out.blocked_detail[decision.symbol] = detail
            out.notes.append(f"{GROSS_EXPOSURE_RULE}: {decision.symbol} refused — {detail}")
            continue
        decision.allocation_pct = after
        decision.reasoning = (
            decision.reasoning + f" [risk engine: {before:.2f}% cut to {after:.2f}% — "
            f"{GROSS_EXPOSURE_RULE} ceiling {ceiling.ceiling_x:.1f}x equity. "
            f"Deterministic, not PM inconsistency.]"
        )[:800]
        granted += equity * (after / 100.0) * multiplier
        out.notes.append(
            f"{GROSS_EXPOSURE_RULE}: {decision.symbol} cut from {before:.2f}% "
            f"to {after:.2f}% of equity — {reason}. {ceiling.reason}"
        )
    out.projected_gross = out.held_gross_after_exits + granted

    # --- STEP 3: trim ONLY if the HELD book alone is still over -----------
    #
    # `held_gross_after_exits` deliberately excludes every proposed entry, so
    # no amount of new buying can provoke a trim. If this book fits under the
    # ceiling, step 2 has already done the whole job.
    over = out.held_gross_after_exits - out.ceiling_usd
    if not emit_trims:
        return out
    if unmeasurable:
        if over > 0:
            logger.warning(
                "Gross-exposure ceiling: book may be over its %.1fx ceiling but "
                "%s returned an unusable market value — refusing to trim on a "
                "broken snapshot (new exposure is already blocked)",
                ceiling.ceiling_x,
                ", ".join(unmeasurable),
            )
        return out
    if over <= 1e-6:
        return out

    already_exiting_fully = {
        str(d.symbol or "").strip().upper()
        for d in out.decisions
        if d.action in ("SELL", "COVER") and d.allocation_pct >= 100
    }
    candidates = []
    for p in positions:
        symbol = str(getattr(p, "symbol", "") or "").strip().upper()
        if park and symbol == park:
            continue  # parked cash is not exposure; selling it frees nothing
        if symbol in already_exiting_fully:
            continue
        market_value = float(getattr(p, "market_value", 0.0) or 0.0)
        if not math.isfinite(market_value) or market_value == 0:
            continue
        position_gross = abs(market_value) * _gross_multiplier(symbol) - exit_relief.get(symbol, 0.0)
        if position_gross <= 0:
            continue
        candidates.append((p, symbol, position_gross))
    # Biggest-loser-first, largest position as tie-break, then symbol for a
    # deterministic order across runs. The SAME ordering `_force_delever`
    # already uses for the cash-only safety net — a second, divergent notion
    # of "which position goes first" is exactly the sprawl §12.2 cleaned up.
    #
    # Item 112 — when a CONVICTION cut order is supplied (the morning
    # post-decision de-lever, which has THIS session's fresh per-seat read),
    # it is the PRIMARY key: the lowest key goes first, so a conviction-dead
    # winner is sold before an intact-thesis loser. A symbol the caller did
    # not place sorts as UNREAD — bucket 1, the caller's NO-COVERAGE rung —
    # and never as bucket 0 OPPOSED: an absent key means the desk has no
    # read on the name, not that it argued against it, and the rule that a
    # data gap must not author a liquidation does not depend on which of
    # the two functions dropped the symbol. The biggest-loser trio stays as
    # the tie-break, so with no conviction_rank the order is exactly as
    # before.
    if conviction_rank is not None:
        candidates.sort(
            key=lambda item: (
                conviction_rank.get(item[1], (1, 0)),
                float(getattr(item[0], "unrealized_pnl", 0.0) or 0.0),
                -item[2],
                item[1],
            )
        )
    else:
        candidates.sort(
            key=lambda item: (
                float(getattr(item[0], "unrealized_pnl", 0.0) or 0.0),
                -item[2],
                item[1],
            )
        )
    for position, symbol, position_gross in candidates:
        if over <= 1e-6:
            break
        take = min(position_gross, over)
        # THE MINIMUM TRIM MUST NOT AMPLIFY A ROUNDING RESIDUE. The fraction
        # below is expressed to 0.1% and the pipeline then floors the order
        # to whole shares, both deliberately DOWNWARD so a trim never sells
        # the book below its ceiling. That leaves a residue of a few dollars,
        # and before this guard the residue pulled in a WHOLE EXTRA NAME at
        # the 1.0% floor — shedding up to 1% of a second position to chase a
        # $2 remainder. Under the conviction cut order that spurious trim
        # landed by construction on the HIGHEST-conviction holding still
        # standing, because the cut walks weakest-first and the strongest
        # name is what is left: the exact opposite of the ordering's purpose.
        # So once a trim has been taken, stop as soon as the minimum trim
        # would shed MORE than the breach that remains. The book is then
        # under its ceiling to within the ticket's own precision, which is
        # the same residue whole-share flooring already leaves.
        # The 1.0% below is the trim builder's own long-standing minimum
        # slice, unchanged and read here rather than re-chosen.
        if out.trims and take < position_gross * 0.01:
            logger.info(
                "Gross-exposure ceiling: $%.0f of breach remains, less than "
                "the minimum trim of %s ($%.0f) — stopping rather than selling "
                "a second name to chase a rounding residue",
                take,
                symbol,
                position_gross * 0.01,
            )
            break
        fraction_pct = min(100.0, max(1.0, round(take / position_gross * 100, 1)))
        is_short = position_side(position) == SECTOR_SIDE_SHORT
        trim = TradeDecision(
            action="COVER" if is_short else "SELL",
            symbol=position.symbol,
            allocation_pct=fraction_pct,
            entry_price=0.0,
            stop_loss=0.0,
            take_profit=0.0,
            reasoning=(
                f"Deterministic de-lever ({GROSS_EXPOSURE_RULE}): the book "
                f"already owns ${out.held_gross_after_exits:,.0f} against a "
                f"${out.ceiling_usd:,.0f} ceiling ({ceiling.ceiling_x:.1f}x "
                f"equity). {ceiling.reason} Reducing {position.symbol} by "
                f"{fraction_pct:.0f}%. New exposure was blocked first; this "
                f"trim runs only because the book is over the ceiling on its "
                f"own."
            )[:500],
        )
        out.trims.append(trim)
        out.notes.append(
            f"{GROSS_EXPOSURE_RULE}: trimming {fraction_pct:.0f}% of "
            f"{position.symbol} — the book already owns more than the "
            f"{ceiling.ceiling_x:.1f}x ceiling allows, with no new buying "
            f"involved. {ceiling.reason}"
        )
        over -= position_gross * (fraction_pct / 100.0)
    if out.trims:
        logger.warning(
            "Gross-exposure ceiling: held book $%.0f over the $%.0f ceiling "
            "(%.1fx equity) — de-levering %d position(s): %s",
            out.held_gross_after_exits,
            out.ceiling_usd,
            ceiling.ceiling_x,
            len(out.trims),
            ", ".join(t.symbol for t in out.trims),
        )
    return out


@dataclass
class RiskViolation:
    rule: str
    message: str
    value: float
    limit: float


class RiskRuleEngine:
    def __init__(self, config: RiskConfig):
        self.config = config

    def check(
        self,
        decision: TradeDecision,
        positions: list[Position],
        total_value: float,
        pending_investment: float = 0.0,
        # Spec §12.2: keyed by `(sector, side)`, not by sector alone —
        # a pending SHORT must not consume the same sector's LONG
        # budget. A plain-`str` key here is now a bug, and raises
        # nothing silently only because `.get()` on a tuple key simply
        # misses it; `accumulate_pending_sector` is the writer.
        pending_sector_investment: dict[tuple[str, str], float] | None = None,
        pending_symbol_investment: dict[str, float] | None = None,
        correlation_matrix: dict[str, dict[str, float]] | None = None,
        max_correlated_cluster_pct: float = 50.0,
        cash: float | None = None,
        pending_cash_outflow: float = 0.0,
        # --- Spec §11.2 ---------------------------------------------
        # The EXECUTION half of the gross-exposure ceiling. The sizing
        # half lives in `PortfolioConstructor`, which shrinks orders to
        # fit; this is the hard block for anything that reaches the
        # engine without that sizing (a legacy notional target, an
        # agent-authored modification, any future caller) — exactly the
        # relationship `max_position_pct` already has with its
        # constructor clamp.
        #
        # `gross_ceiling` is the ladder-resolved ceiling for this
        # session (`resolve_gross_ceiling`). None falls back to the
        # configured cap with no drawdown applied, so a caller that
        # forgets it still gets a ceiling rather than none.
        gross_ceiling: "GrossCeiling | None" = None,
        pending_gross_investment: float = 0.0,
        cash_park_symbol: str | None = None,
    ) -> list[RiskViolation]:
        # D10 (Stage 3): a COVER can never be hard-blocked, mirroring the
        # deliberate asymmetry already used for exits — entries fail
        # closed, exits fail open, because being unable to close a
        # position is strictly worse than being unable to open one. A
        # COVER is mechanically a buy at the broker, so without this it
        # would be caught by the cash_only rule below exactly like a BUY.
        if decision.action in ("SELL", "COVER"):
            return []
        # total_value <= 0 (or NaN) means we can't compute risk percentages.
        # Pre-fix the early return was `[]` which has the same shape as
        # "all checks passed" — so an Alpaca portfolio_value=0 blip during
        # market-open silently approved every BUY, bypassing cash_only /
        # max_position_pct. Emit a
        # synthetic violation in HARD_BLOCK_RULES so the pipeline filter
        # blocks the BUY instead. The empty list reserved exclusively for
        # "checked, found no violations" semantics.
        import math

        if not math.isfinite(total_value) or total_value <= 0:
            return [
                RiskViolation(
                    rule="max_total_position_pct",  # in HARD_BLOCK_RULES
                    message=(
                        f"total_value={total_value} is not a valid equity figure "
                        f"(broker glitch or fresh account) — refusing to risk-check "
                        f"BUY for {decision.symbol}; blocking until next snapshot"
                    ),
                    value=0.0,
                    limit=0.0,
                )
            ]

        # A single non-finite position market_value poisons every sum below.
        # NaN comparisons are all False, so `sector_pct > cap` and
        # `total_pct > cap` silently evaluate False — the exposure and sector
        # caps switch OFF for the whole session on exactly the broken-snapshot
        # day they matter most (2026-07-16 audit; Alpaca has been observed to
        # return NaN market_value during market-open glitches). Block instead,
        # mirroring the total_value guard above: no risk-check, no BUY.
        bad_mv = [p.symbol for p in positions if not math.isfinite(p.market_value)]
        if bad_mv:
            return [
                RiskViolation(
                    rule="max_total_position_pct",  # in HARD_BLOCK_RULES
                    message=(
                        f"non-finite market_value for {', '.join(sorted(bad_mv))} — "
                        f"exposure / sector caps cannot be computed; refusing to "
                        f"risk-check BUY for {decision.symbol}; blocking until the "
                        f"next clean snapshot"
                    ),
                    value=0.0,
                    limit=0.0,
                )
            ]

        # Non-finite cash disables the cash_only comparison the same silent
        # way a NaN market_value disabled the caps (audit round 2:
        # `NaN < 0` is False, so every BUY passed). Fail closed.
        if cash is not None and not math.isfinite(cash):
            return [
                RiskViolation(
                    rule="max_total_position_pct",  # in HARD_BLOCK_RULES
                    message=(
                        f"non-finite cash={cash} — cash_only cannot be evaluated; "
                        f"refusing to risk-check BUY for {decision.symbol}; "
                        f"blocking until the next clean snapshot"
                    ),
                    value=0.0,
                    limit=0.0,
                )
            ]

        violations = []
        is_short = decision.action == "SHORT"
        signed_mul = _effective_multiplier(decision.symbol)  # net direction
        gross_mul = _gross_multiplier(decision.symbol)  # size magnitude
        new_investment = total_value * (decision.allocation_pct / 100)
        # A SHORT moves net exposure the OPPOSITE way a BUY of the same
        # symbol would (it adds negative, not positive, directional
        # exposure) — flip the sign so rule 2 below stays correct instead
        # of reading a growing short as growing long exposure.
        signed_new = new_investment * signed_mul * (-1.0 if is_short else 1.0)
        gross_new = new_investment * gross_mul

        # 1. Single position size limit (gross — a 3x ETF consumes 3x regardless of direction)
        #
        # SHORT takes the branch below instead. This arithmetic assumes
        # `new_investment` (always a positive magnitude) moves the position
        # FURTHER in the direction `current_symbol_raw` is already signed
        # toward — true for a BUY adding to a long, wrong for a SHORT adding
        # to a short (`current_symbol_raw` is negative, so the sum OFFSETS
        # toward zero). The SHORT branch measures the same cap
        # direction-aware.
        if not is_short:
            current_symbol_raw = sum(p.market_value for p in positions if p.symbol == decision.symbol)
            current_symbol_raw += (pending_symbol_investment or {}).get(decision.symbol, 0.0)
            # `weight_pct_of` is the ONE definition of a gross-leverage
            # weight — the same function the constructor's current-weight
            # map, the PM's position lines and the PM facts drift check
            # call. Identical arithmetic to the inline form it replaces
            # (`(mv) * gross_mul / equity * 100`); routed through the shared
            # function so the cap and the numbers the PM sizes against
            # cannot drift apart again.
            position_pct = weight_pct_of(
                current_symbol_raw + new_investment,
                decision.symbol,
                total_value,
            )
            if position_pct > self.config.max_position_pct:
                violations.append(
                    RiskViolation(
                        rule="max_position_pct",
                        message=f"{decision.symbol} position would be {position_pct:.1f}% and exceed max {self.config.max_position_pct}%",
                        value=position_pct,
                        limit=self.config.max_position_pct,
                    )
                )

        # The SAME single-position cap for a short (owner decision
        # 2026-09-17: shorts carry the same limits as longs). Same rule name
        # and same limit as the long branch above, so the two can never
        # drift apart; only the measurement is direction-aware. Never reached
        # for a COVER (exempted at the top of this method).
        if is_short:
            current_short_raw = sum(p.market_value for p in positions if p.symbol == decision.symbol and p.qty < 0)
            # `pending_symbol_investment` (like `new_investment`) is always
            # an UNSIGNED dollar magnitude — see the accumulation in
            # `TradingPipeline._filter_hard_risk_decisions`, the same
            # convention a same-batch BUY already uses. `current_short_raw`
            # is the only signed term here (a short's market_value is
            # negative), so it alone needs `abs()`.
            pending_same_symbol = (pending_symbol_investment or {}).get(decision.symbol, 0.0)
            position_pct = weight_pct_of(
                abs(current_short_raw) + pending_same_symbol + new_investment,
                decision.symbol,
                total_value,
            )
            if position_pct > self.config.max_position_pct:
                violations.append(
                    RiskViolation(
                        rule="max_position_pct",
                        message=(
                            f"{decision.symbol} short would be {position_pct:.1f}% "
                            f"and exceed max {self.config.max_position_pct}%"
                        ),
                        value=position_pct,
                        limit=self.config.max_position_pct,
                    )
                )

        # 1c. Spec §11.2 — the GROSS-exposure ceiling. HARD BLOCK (in
        # HARD_BLOCK_RULES, above in this file).
        #
        # Distinct from rule 2 below in the way that matters: rule 2 measures
        # NET exposure, where a hedge cancels a long. That does not answer "how much does the book OWN", which is what
        # decides whether a 33% fall triggers a margin call. Nothing in this
        # codebase answered that question before §11.2.
        #
        # The cash park is excluded — it is parked cash, not a position, and
        # counting it would consume the whole allowance doing nothing.
        gross_ceiling_x = (
            _positive_float(gross_ceiling.ceiling_x)
            if isinstance(gross_ceiling, GrossCeiling)
            else _positive_float(getattr(self.config, "max_gross_exposure_x", None))
        )
        if gross_ceiling_x > 0:
            held_gross = gross_exposure(
                positions,
                cash_park_symbol=cash_park_symbol,
            )
            projected_gross = held_gross + pending_gross_investment + gross_new
            gross_x = projected_gross / total_value
            if gross_x > gross_ceiling_x + 1e-9:
                ladder_note = f" {gross_ceiling.reason}" if gross_ceiling is not None else ""
                violations.append(
                    RiskViolation(
                        rule=GROSS_EXPOSURE_RULE,
                        message=(
                            f"{decision.symbol} would put the book at "
                            f"{gross_x:.2f}x equity in gross exposure "
                            f"(${projected_gross:,.0f} owned against "
                            f"${total_value:,.0f} of equity), over the "
                            f"{gross_ceiling_x:.2f}x ceiling. Parked cash is not "
                            f"counted.{ladder_note}"
                        ),
                        value=round(gross_x, 4),
                        limit=round(gross_ceiling_x, 4),
                    )
                )

        # 2. Total net exposure limit — signed, so long+short hedges cancel.
        #
        # Reads the SAME `book_exposure` that PM's `invested_pct`, the PM
        # prompt's Account Status line and the `macro_exposure_deviation`
        # advisory read, so this cap can no longer be enforced against a book
        # measured differently from the one the seats were shown.
        #
        # The `abs()` here is DELIBERATE and stays: this is a magnitude
        # ceiling, and a book 150% net SHORT is as far over it as one 150%
        # net long. That is the opposite of the `abs()` removed from the
        # macro advisory, which was erasing the direction of a number whose
        # whole job was to report it. Non-finite market values are already
        # hard-blocked above, so `book_exposure`'s skip cannot hide one here.
        projected_book = book_exposure(
            positions,
            total_value,
            pending_net_usd=pending_investment + signed_new,
        )
        total_pct = abs(projected_book.net_pct)
        # Cross-check (fix/dashboard-and-duplicate-definitions): the
        # dashboard serves this same magnitude from src/quantities.py, which
        # cannot import src.risk. A guard test asserts the two agree, so the
        # bar and the ceiling it is drawn against cannot drift apart.
        if total_pct > self.config.max_total_position_pct:
            violations.append(
                RiskViolation(
                    rule="max_total_position_pct",
                    message=f"Net exposure {total_pct:.1f}% would exceed max {self.config.max_total_position_pct}%",
                    value=total_pct,
                    limit=self.config.max_total_position_pct,
                )
            )

        # 4. Stop loss required
        if self.config.require_stop_loss and decision.stop_loss <= 0:
            violations.append(
                RiskViolation(
                    rule="require_stop_loss",
                    message=f"{decision.symbol} has no stop loss set",
                    value=decision.stop_loss,
                    limit=0,
                )
            )

        # 4b. Correlation cluster (advisory) — catches the "all-AI" concentration problem
        # that sector caps miss. If the proposed BUY plus the held positions that sit in
        # the SAME correlation cluster as it together exceed max_correlated_cluster_pct,
        # flag. Cluster membership is read from the book's own correlation geometry
        # (Mantegna distance MST cut at its own largest gap) — there is no correlation
        # cutoff any more; the old 0.7 was unsourceable and is removed, not ratified.
        if correlation_matrix:
            from src.data.correlation import cluster_peers

            held_symbols = [p.symbol for p in positions]
            peers = cluster_peers(decision.symbol, held_symbols, correlation_matrix)
            if peers:
                # Apply gross multiplier consistently with sector / position
                # caps below — a 3x inverse ETF (SQQQ) in a cluster consumes
                # 3x notional, even though its directional sign cancels for
                # NET exposure (#2). Pre-fix this rule treated SQQQ as 1x
                # which silently under-counted cluster concentration.
                # The cluster must include the BUY symbol's OWN existing
                # position, not just its peers: `highly_correlated_peers`
                # (correctly) excludes the symbol itself, so an ADD to the
                # largest member of a cluster counted only the ADD's notional
                # and none of the stack already held — the concentration this
                # rule exists to catch was invisible exactly when it was worst
                # (2026-07-16 audit). A symbol is trivially correlated 1.0
                # with itself, so it belongs in its own cluster total.
                cluster_symbols = set(peers) | {decision.symbol}
                peer_value = sum(
                    p.market_value * _gross_multiplier(p.symbol) for p in positions if p.symbol in cluster_symbols
                )
                cluster_pct = (peer_value + gross_new) / total_value * 100
                if cluster_pct > max_correlated_cluster_pct:
                    violations.append(
                        RiskViolation(
                            rule="correlation_cluster",
                            message=(
                                f"{decision.symbol} + correlated holdings [{', '.join(peers)}] "
                                f"would total {cluster_pct:.0f}% of book, exceeding "
                                f"{max_correlated_cluster_pct:.0f}% cluster cap (advisory). "
                                f"One correlation cluster by the book's own structure "
                                f"(correlation-distance tree, cut at its widest gap)."
                            ),
                            value=cluster_pct,
                            limit=max_correlated_cluster_pct,
                        )
                    )

        # 4c. Cash-only policy — when allow_margin is False, no BUY may spend more
        # than the cash remaining after prior BUYs in this session. `cash` is the
        # session-start broker cash; `pending_cash_outflow` is the dollar total of
        # BUYs already allowed earlier in the same filter pass. Sector / leverage
        # multipliers don't apply here — cash is spent at gross dollar notional
        # regardless of whether the symbol is an inverse / leveraged ETF.
        #
        # SHORT is exempt: opening a short does not spend the settled-cash
        # pool this rule was written to protect — it sells borrowed shares,
        # crediting cash (against a margin requirement this codebase does
        # not model). The position, gross and net caps, not this rule, are the control
        # surface for a short (D11).
        if not self.config.allow_margin and cash is not None and not is_short:
            projected_cash = cash - pending_cash_outflow - new_investment
            if projected_cash < 0:
                violations.append(
                    RiskViolation(
                        rule="cash_only",
                        message=(
                            f"{decision.symbol} BUY for ${new_investment:,.0f} would "
                            f"spend beyond available cash (cash=${cash:,.0f}, pending "
                            f"BUYs=${pending_cash_outflow:,.0f}); margin is disabled"
                        ),
                        value=abs(projected_cash),
                        limit=max(cash - pending_cash_outflow, 0.0),
                    )
                )

        # 5. Unresolved-sector alert (no sector LIMIT any more).
        #
        # The 75% sector target / 90% sector ceiling that lived here was
        # deleted 2026-10-09: it counted sector LABELS on notional, a number
        # nothing measured. Related-stock concentration is bounded by the
        # correlation cluster cap (`src/risk/budget.py`, 40% of the total
        # risk ceiling), and every order — sector resolved or not — is still
        # bounded by `max_position_pct`, `max_gross_exposure`,
        # `max_total_position_pct` and the total risk budget, none of which
        # reads the sector. What stays is the loud-failure alert below: a
        # failed sector lookup is a data fault the desk must see.
        from src.sector_reference import _get_sector, _sector_resolution_status_for

        new_sector = _get_sector(decision.symbol)
        if new_sector == "Unknown":
            side = decision_side(decision.action)
            held_by_side = sector_side_gross(positions, include_unknown=True)
            sector_value = held_by_side.get((new_sector, side), 0.0)
            sector_value += (pending_sector_investment or {}).get(
                (new_sector, side),
                0.0,
            )
            sector_value += gross_new
            sector_pct = sector_value / total_value * 100
            side_label = "long" if side == SECTOR_SIDE_LONG else "short"

            # Loud-failure requirement (2026-09-01): a symbol resolving to
            # "Unknown" must never pass silently. Advisory (never in
            # HARD_BLOCK_RULES on its own) — mirrors the non-blocking seam
            # `data_degraded` / `correlation_coverage_gap` /
            # `pm_audit_step_missing` already use elsewhere in this
            # pipeline: it reaches the Risk Manager's prompt via
            # `rule_violations` and (src/pipeline_stages.py) sets
            # `data_status["sector"]`, which is what puts the plain
            # "degraded" line in the session output and the owner's
            # Telegram alert. A transient lookup failure and a genuinely
            # sector-less instrument are DIFFERENT conditions — one
            # self-heals on the next call, the other won't — so they get
            # different rule names and different wording rather than
            # reading the same.
            if new_sector == "Unknown":
                status = _sector_resolution_status_for(decision.symbol)
                if status == "no_sector":
                    alert_rule = "sector_unresolved_no_sector"
                    reason = (
                        f"{decision.symbol}: sector lookup succeeded but returned "
                        f"no sector — this instrument may genuinely be unclassified "
                        f"(e.g. an ETF outside the static table)."
                    )
                elif status == "lookup_failed":
                    alert_rule = "sector_unresolved_lookup_failed"
                    reason = (
                        f"{decision.symbol}: sector lookup failed or timed out "
                        f"(transient — not cached, will self-heal once the "
                        f"lookup succeeds again)."
                    )
                else:
                    alert_rule = "sector_unresolved"
                    reason = f"{decision.symbol}: sector did not resolve."
                violations.append(
                    RiskViolation(
                        rule=alert_rule,
                        message=(
                            f"{reason} Treated as constrained in the pooled 'Unknown' "
                            f"sector bucket ({sector_pct:.1f}% {side_label} of book). Its "
                            f"size is still bounded by the single-name, gross, net "
                            f"and risk-budget caps, none of which reads the sector."
                        ),
                        value=sector_pct,
                        limit=0.0,
                    )
                )

        return violations


# --- Re-export mirror ------------------------------------------------------
#
# Where each rule lives now. `src/risk/rules.py` keeps the engine, the gross
# ceiling and its ladder, and every number the ledger pins by this module's
# id; the five parts below hold the bodies, moved verbatim, each importable
# and exercisable on its own (`tests/test_risk_rules_parts_boundary.py`). The
# imports sit at the END so that `from src.risk.rules import X` and
# `patch("src.risk.rules.X")` keep working for every existing caller, and so
# that the engine above finds the names it calls. No part imports this module,
# so there is no cycle. ONE mirror block, never two.
from src.risk.sector_budget import (  # noqa: E402,F401
    SECTOR_SIDE_LONG,
    SECTOR_SIDE_SHORT,
    position_side,
    decision_side,
    sector_side_gross,
    accumulate_pending_sector,
    sector_side_weights,
)
from src.risk.seat_agreement import (  # noqa: E402,F401
    _BULLISH_STANCES,
    _BEARISH_STANCES,
    stance_is_aligned,
    _count_sources,
    count_aligned_sources,
    count_opposing_sources,
    agreement_refuses_trade,
)
from src.risk.unread_filing import (  # noqa: E402,F401
    UNREAD_FILING_REASON_PREFIX,
    unread_filing_block_reason,
)
from src.risk.conviction_bar import (  # noqa: E402,F401
    OWN_BAR_REASON_PREFIX,
    _has_supported_directional_thesis,
    _is_broadcast_macro_verdict,
    own_bar_block_reason,
    own_bar_opposition_reason,
)
from src.risk.book_exposure import (  # noqa: E402,F401
    _positive_float,
    peak_to_trough_pct,
    unmeasurable_gross_symbols,
    gross_exposure,
    deployment_gap_band_pct,
    BookExposure,
    book_exposure,
    weight_pct_of,
    position_weight_pct,
)
