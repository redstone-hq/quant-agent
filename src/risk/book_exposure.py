"""One definition of how invested the book is, and of a position's weight.

Gross exposure, peak-to-trough drawdown, deployed/net/gross of the book,
and the leverage-aware weight of one position. The ceiling and the ladder
that ACT on these numbers stay in `src/risk/rules.py` with their
ledger-pinned constants.

Bodies moved VERBATIM from `src/risk/rules.py` (AST-identical to the
originals; `tests/test_risk_rules_parts_boundary.py` is the witness that this
part builds and runs alone). `src/risk/rules.py` keeps the engine, every
ledger-pinned number and the re-export mirror, so every existing
`from src.risk.rules import X` keeps resolving.
"""

import logging
import math
from dataclasses import dataclass, field

from src.quantities import (
    effective_multiplier as _effective_multiplier,
    gross_multiplier as _gross_multiplier,
)

logger = logging.getLogger(__name__)


def _positive_float(value, default: float = 0.0) -> float:
    """Coerce a config value to a usable positive float, or `default`.

    Same Mock-safety posture `pipeline.py::_risk_setting` already takes: a
    MagicMock config fixture auto-creates a child mock for any attribute
    access, and `mock > 0` raises rather than returning False. A ceiling that
    cannot be read is INERT (default 0.0 switches the rule off) rather than
    blocking. Production config always carries the validated field, so this
    path is a test/misconfiguration guard only.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    value = float(value)
    if not math.isfinite(value) or value <= 0:
        return default
    return value


def peak_to_trough_pct(
    equity_history,
    current_equity: float | None,
) -> float | None:
    """Peak-to-trough drawdown in percent (<= 0), or None if unmeasurable.

    `equity_history` is any iterable of past equity values (order irrelevant
    — a high-water mark does not care). `current_equity` is today's live
    equity and is included in the peak, so a book making new highs reads 0.0
    rather than a stale negative.

    This is the desk's only measure of drawdown. The rolling 5-day /
    20-day return brakes that used to sit beside it ("has our recent edge
    degraded, so halve new BUYs") were removed on 2026-09-20 by owner
    instruction; this one answers "how far are we off the high-water mark,
    so how much may the book own", and it sets the ceiling.

    Guard 2 (2026-09-02 operational safety guard): a NaN/inf equity reading
    can NEVER win the `max()` below and become the high-water mark. Another
    system's documented failure mode is an incremental "if new > hwm: hwm =
    new" tracker, where a single NaN reading poisons `hwm` permanently —
    every later comparison against NaN is False, so it never updates again
    and the breaker is silently disabled for good. This function structurally
    cannot do that: the peak is a fresh `max()` over a filtered list on
    EVERY call, never a variable carried between calls, so a bad reading on
    one call cannot contaminate the next. A non-finite entry is dropped
    before `max()` ever sees it (`math.isfinite` below) rather than being
    excluded by a comparison a NaN could silently fail.

    Guard 3 (2026-09-18): **an equity curve with no usable PRIOR reading is
    unmeasurable, not a zero drawdown.** Until this guard, a history that
    was empty — or whose every entry had been dropped as non-finite — left
    `current_equity` alone in the list, so it became its own high-water mark
    and the function returned a confident `0.0`. `resolve_gross_ceiling`
    reads `0.0` as "inside the no-de-levering band" and holds the loosest
    cap, which means a wiped or unreadable `daily_pnl` table silently
    disabled the desk's only automatic seller AND looked, in every log line
    and every owner-facing message, exactly like a book at record highs.
    Losing the data and making new highs are opposite states and must not
    produce the same output.

    The boundary is deliberately zero prior readings, not a chosen minimum
    number of days. Zero is the line between *measured* and *unmeasured* —
    it is not a number anyone picked, and this file's standing rule forbids
    inventing one. Whether a SHORT-but-non-empty curve (two or three days
    after a reset) is long enough to carry a meaningful high-water mark is a
    real, separate question with a real answer somewhere in the desk's own
    data; it is NOT answered here, and no `min_history=N` default is
    smuggled in as if it had been.
    """
    history_values = []
    dropped_non_finite = 0
    for raw in list(equity_history or []):
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            continue
        value = float(raw)
        if not math.isfinite(value):
            # Counted, not just skipped: a dropped entry could have been
            # the TRUE peak, and silently proceeding as if it never existed
            # is exactly the "NaN reaching a comparison silently passes"
            # failure mode this guard exists to avoid. The log line below
            # is the guard — it does not change what gets returned, it
            # makes sure the loss of data is never invisible.
            dropped_non_finite += 1
            continue
        if value > 0:
            history_values.append(value)
    if dropped_non_finite:
        logger.warning(
            "peak_to_trough_pct: dropped %d non-finite equity reading(s) "
            "(NaN/inf) rather than letting one win max() as a fabricated "
            "high-water mark. Alpaca has been observed to return NaN "
            "portfolio_value during market-open glitches. "
            "The remaining %d historical "
            "reading(s) still went into this call's peak.",
            dropped_non_finite,
            len(history_values),
        )
    if not (
        isinstance(current_equity, (int, float))
        and not isinstance(current_equity, bool)
        and math.isfinite(float(current_equity))
        and float(current_equity) > 0
    ):
        return None
    if not history_values:
        # Guard 3. Fail LOUD and UNMEASURABLE rather than quietly confident.
        logger.warning(
            "peak_to_trough_pct: no usable PRIOR equity reading (history "
            "empty or every entry unusable) — returning UNMEASURABLE rather "
            "than 0.0%%. Today's own equity is not a high-water mark: "
            "reporting 0.0%% here would make a wiped equity curve "
            "indistinguishable from a book at record highs and would hold "
            "the de-levering ladder at its loosest cap.",
        )
        return None
    peak = max(history_values + [float(current_equity)])
    if peak <= 0:
        return None
    return round((float(current_equity) - peak) / peak * 100, 2)


def unmeasurable_gross_symbols(
    positions,
    *,
    cash_park_symbol: str | None = None,
) -> list[str]:
    """Held symbols whose market value cannot be trusted for gross math.

    Alpaca has been observed returning NaN `market_value` during market-open
    glitches. `NaN > ceiling` is False, so an unguarded comparison switches
    the ceiling OFF on exactly the broken-snapshot day it matters most —
    the same failure the sector and single-name caps were hardened against
    (2026-07-16 audit). Callers fail closed on a non-empty result.
    """
    park = (cash_park_symbol or "").strip().upper()
    bad: list[str] = []
    for p in positions or []:
        symbol = str(getattr(p, "symbol", "") or "").strip().upper()
        if park and symbol == park:
            continue
        market_value = getattr(p, "market_value", 0.0)
        if market_value is None or not math.isfinite(float(market_value)):
            bad.append(symbol or "?")
    return sorted(bad)


def gross_exposure(positions, *, cash_park_symbol: str | None = None) -> float:
    """Gross exposure in DOLLARS: long market value + |short market value|.

    Leverage multiples count, the same convention `sector_side_gross` and
    the single-name cap already use: a 3x fund consumes 3x its sticker
    notional whichever way it points.

    **The cash park does not count as exposure.** The sweep vehicle
    (`cash_sweep.symbol`, SGOV by default) is parked cash, not a position —
    it is already treated as cash-equivalent by the risk engine, by every
    LLM-facing view, by the stop-coverage audit and by `_force_delever`,
    which liquidates it first. Counting it here would consume the entire
    leverage allowance doing nothing. Callers pass the symbol from config;
    it is never hardcoded.

    Non-finite values are skipped — `unmeasurable_gross_symbols` is the
    caller's guard against acting on a total that silently excluded them.
    """
    park = (cash_park_symbol or "").strip().upper()
    total = 0.0
    for p in positions or []:
        symbol = str(getattr(p, "symbol", "") or "").strip().upper()
        if park and symbol == park:
            continue
        market_value = getattr(p, "market_value", 0.0) or 0.0
        try:
            market_value = float(market_value)
        except (TypeError, ValueError):
            continue
        if not math.isfinite(market_value):
            continue
        total += abs(market_value) * _gross_multiplier(symbol)
    return total


# --- One definition of "how invested is the book" ------------------------
#
# THE DEFECT THIS REPLACES (measured 2026-09-01, two implementations run on
# one book): `$50k AAPL long + $20k SQQQ` against a 60% target, $100k equity.
#
#   PM was told   `total_value - cash`            -> 70% -> "10pp OVER target"
#   RM was told   `abs(sum(mv * signed_mult))`    -> 10% -> "50pp UNDER target,
#                                                            do NOT scale down"
#
# Same book, same target, opposite sign, and each seat acted on its own
# number. `book_exposure` is now the only place either question is answered.
#
# WHY `deployed_pct` IS THE ONE COMPARED TO THE INVESTED TARGET, and not
# the signed leverage-aware number:
#
#  1. The target is DEFINED as the complement of cash. It used to be macro's
#     `target_invested_pct` (0-100, "sums to ~100" with its cash number);
#     since the owner mandate of 2026-09-17 it is the fixed
#     `DESK_INVESTED_TARGET_PCT` below. Either way it is a capital-deployment
#     target.
#  2. The consequence the gap exists to fix is idle cash — PMFacts renders it
#     as "the single largest P&L drag (idle cash in a rising market)" and
#     routes it to the `cash_target` step. Leverage does not make a dollar
#     less idle: $20k in a 3x fund is $20k of cash put to work, not $60k.
#  3. The signed measure has no honest comparison to a 0-100 target. A book
#     fully deployed in a 3x inverse ETF reads -300%, i.e. "375pp under a 75%
#     target, deploy more" — with no cash to deploy. A long $50k / short $50k
#     book reads 0% and asks for more of the money it has already spent.
#  4. The leverage-and-direction question is ALREADY answered, deterministically
#     and ENFORCED rather than advised, by `gross_exposure` + the §11.2
#     `apply_gross_ceiling`. It does not need
#     `macro_exposure_deviation` to answer it a second time, badly.
#
# `deployed_usd` sums |market_value|, which also repairs a quieter defect in
# the PM's old `total_value - cash`: equity is `cash + sum(market_value)`, and
# a held short's `market_value` is NEGATIVE, so a short made the book look
# LESS deployed to the PM, which then deployed more. Shorting is capital put
# to work, not capital returned to the pile.
#
# `net_usd` is kept and REPORTED rather than discarded, and it carries no
# `abs()`. The old `abs(existing_net + pending)` made a net-SHORT book read as
# positively invested — long and short of the same size were literally the
# same number. A negative `net_pct` now says "net short" out loud.
#
# The cash-sweep vehicle is not exposure in ANY of the three, matching
# `gross_exposure`, `_force_delever` and every LLM-facing position view.


def book_one_session_drift_pp(positions, equity: float, atr_for_symbol) -> float | None:
    """How far today's book can move in one session, in points of equity.

    The threshold for the `deployment_gap` advisory, measured from the book
    itself rather than typed: the sum over held positions of (deployed weight
    in % of equity) x (that stock's ATR(14) / price). `atr_for_symbol` is the
    desk's existing ATR reader (`src.data.technical.atr_for_symbol` via the
    pipeline), re-read every session. A gap smaller than this is inside the
    book's own day-to-day noise; a gap bigger than it is real idle cash.

    None ("drift unmeasured") when there is no ATR reader, no equity, or any
    held position's ATR or price is unavailable — a partial sum would
    understate the drift, and no fallback number is substituted. An empty
    book measures exactly 0.
    """
    try:
        eq = float(equity or 0.0)
    except (TypeError, ValueError):
        return None
    if atr_for_symbol is None or not math.isfinite(eq) or eq <= 0:
        return None
    drift = 0.0
    for p in positions or []:
        symbol = str(getattr(p, "symbol", "") or "").strip().upper()
        try:
            market_value = float(getattr(p, "market_value", 0.0) or 0.0)
            price = float(getattr(p, "current_price", 0.0) or 0.0)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(market_value) or not math.isfinite(price) or price <= 0:
            return None
        atr = atr_for_symbol(symbol)
        if atr is None or not math.isfinite(atr) or atr <= 0:
            return None
        drift += abs(market_value) / eq * 100 * (atr / price)
    return drift


@dataclass(frozen=True)
class BookExposure:
    """One book, measured three ways that can no longer drift apart.

    `deployed` — capital committed, unsigned, NO leverage multiple. The
        cash-complement measure, and the ONLY one comparable to the
        invested target (`DESK_INVESTED_TARGET_PCT`).
    `net` — signed and leverage-aware. Direction of the book. Negative means
        net short. Hedges cancel, which is the point of this one.
    `gross` — unsigned and leverage-aware. What the §11.2 ceiling caps.
    """

    equity: float
    deployed_usd: float
    net_usd: float
    gross_usd: float

    def _pct(self, usd: float) -> float:
        if not self.equity or self.equity <= 0 or not math.isfinite(self.equity):
            return 0.0
        return usd / self.equity * 100

    @property
    def deployed_pct(self) -> float:
        return self._pct(self.deployed_usd)

    @property
    def net_pct(self) -> float:
        return self._pct(self.net_usd)

    @property
    def gross_pct(self) -> float:
        return self._pct(self.gross_usd)


def book_exposure(
    positions,
    equity: float,
    *,
    cash_park_symbol: str | None = None,
    pending_deployed_usd: float = 0.0,
    pending_net_usd: float = 0.0,
    pending_gross_usd: float = 0.0,
) -> BookExposure:
    """The single source for "how invested is this book".

    `pending_*` are approved-but-unexecuted orders from the same batch, so a
    pre-trade gate can ask the question about the book it is ABOUT to hold.
    They are passed already-summed by the caller because the accumulation has
    to interleave with the per-decision approval loop.

    Non-finite `market_value` is skipped rather than poisoning the total to
    NaN — same convention as `gross_exposure`; `unmeasurable_gross_symbols`
    is the caller's guard against acting on a total that quietly excluded a
    position.
    """
    park = (cash_park_symbol or "").strip().upper()
    deployed = 0.0
    net = 0.0
    for p in positions or []:
        symbol = str(getattr(p, "symbol", "") or "").strip().upper()
        if park and symbol == park:
            continue
        market_value = getattr(p, "market_value", 0.0) or 0.0
        try:
            market_value = float(market_value)
        except (TypeError, ValueError):
            continue
        if not math.isfinite(market_value):
            continue
        deployed += abs(market_value)
        net += market_value * _effective_multiplier(symbol)
    return BookExposure(
        equity=float(equity or 0.0),
        deployed_usd=deployed + float(pending_deployed_usd or 0.0),
        net_usd=net + float(pending_net_usd or 0.0),
        gross_usd=gross_exposure(positions, cash_park_symbol=cash_park_symbol) + float(pending_gross_usd or 0.0),
    )


# --- One definition of a position's WEIGHT -------------------------------
#
# GROSS-leverage weight, signed. `market_value x |leverage| / equity`.
#
# THE DEFECT THIS REPLACES (measured 2026-09-01): the PM prompt states "All
# weights are GROSS-leverage weights", renders a position line reading
# `Weight: 18.0% ... DRIFT` from the gross number, and three lines later
# renders `drift-flagged: 0` from a raw one that never crossed the 12%
# threshold. One prompt, two weights, contradicting each other in view of the
# model that has to act on them.
#
# GROSS is the convention because it is the one the ENGINE enforces: the
# `max_position_pct` hard cap, the sector budgets and the constructor's
# current-weight comparison are all gross. Rendering a raw weight to the PM
# made it restate a 3x SQQQ's 6% raw as its target, which the constructor read
# as "cut 18% down to 6%" and turned into a 67% SELL nobody asked for.
#
# SIGNED, not absolute: a held short's weight is negative, which is how the
# drift heuristic stays a long-side question. A winning short's |market_value|
# SHRINKS toward zero, so it cannot drift into an oversized position the way
# an appreciating long can.


def weight_pct_of(market_value: float, symbol: str, equity: float) -> float:
    """Signed GROSS-leverage weight of `market_value` held in `symbol`.

    Takes a dollar figure rather than a position so the pre-trade gate can ask
    it about a projected `held + pending + new` exposure, not only about what
    is already on the books.
    """
    try:
        mv = float(market_value or 0.0)
        eq = float(equity or 0.0)
    except (TypeError, ValueError):
        return 0.0
    if not eq or eq <= 0 or not math.isfinite(eq) or not math.isfinite(mv):
        return 0.0
    return mv * _gross_multiplier(str(symbol or "").strip().upper()) / eq * 100


def position_weight_pct(position, equity: float) -> float:
    """`weight_pct_of` for a held position. The form most callers want."""
    return weight_pct_of(
        getattr(position, "market_value", 0.0) or 0.0,
        getattr(position, "symbol", "") or "",
        equity,
    )
