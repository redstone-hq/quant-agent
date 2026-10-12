"""One definition per quantity, for quantities more than one subsystem needs.

Why this module exists
----------------------
Several numbers in this system were computed independently in two or three
places and disagreed with each other — the trading engine sized against one
figure while Mission Control printed another under the same word. Measured
examples that motivated this file:

- "deployable cash": the engine used `cash + parked sweep value`, the API
  used `max(cash - reserve, 0)`. Same name, opposite adjustments, 1.64x
  apart on the same book. The operator was reading a number no part of the
  engine had ever used.
- "% deployed": the risk gate measured signed, leverage-weighted net
  exposure; the dashboard summed long + hedge market value raw and drew the
  result against the ENGINE's ceiling — 46% vs 22% on the same book.
- "20-day average dollar volume": three definitions (fixed /20, /surviving
  bars, and a 21-bar mean), 5.26% apart with a single halted session in the
  window — enough to flip an admission gate.

The rule this module encodes: **a quantity has exactly one definition, and
every caller routes through it.** Thresholds stay with the callers — two
gates legitimately compare the same measure against different numbers.
Duplicating the MEASURE is what drifts.

Structural constraints
----------------------
This module is deliberately dependency-free (stdlib only). It must stay
importable by `src/api/`, which a ratified guardrail
(`tests/test_api_safety.py`) forbids from importing `src.pipeline`,
`src.pipeline_stages` or `src.risk`. Extracting the pure measurement
functions to a module outside `src.risk` is the seam `routes_live.py`'s
§11.2 note already named as the precondition for the dashboard reporting
exposure at all. Nothing here reads config, touches the broker, or makes a
decision — it is arithmetic with an agreed meaning.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Iterable

from src.stop_price_classification import classify_stop_price

__all__ = [
    "ETF_LEVERAGE",
    "AVG_DOLLAR_VOLUME_WINDOW",
    "effective_multiplier",
    "gross_multiplier",
    "inverse_etf_symbols",
    "deployable_cash",
    "sweep_reserve_usd",
    "cash_above_reserve",
    "net_exposure_usd",
    "net_exposure_pct",
    "dollar_volumes",
    "avg_dollar_volume",
    "collapse_stances",
]


def _finite(value: Any) -> float | None:
    """`float(value)` when it is a real, finite number — otherwise None.

    Booleans are rejected: `True` is not a market value, and Python would
    otherwise silently treat it as 1.0.
    """
    if isinstance(value, bool) or value is None:
        return None
    if not isinstance(value, (int, float)):
        return None
    out = float(value)
    return out if math.isfinite(out) else None


def _field(item: Any, name: str) -> Any:
    """Read `name` off an object attribute or a mapping key.

    Callers span dataclass-ish `Position` objects (engine side) and plain
    dicts (the API's `read_positions()` payload). One accessor keeps one
    definition serving both rather than growing a second, dict-shaped copy.
    """
    if isinstance(item, dict):
        return item.get(name)
    return getattr(item, name, None)


# ---------------------------------------------------------------------------
# 1. Cash liquidity — what the desk can actually put to work
# ---------------------------------------------------------------------------


def deployable_cash(cash: Any, parked_sweep_value: Any = 0.0) -> float:
    """Cash QAMC can deploy into equities WITHOUT borrowing.

    Raw broker `cash` plus the market value of the cash-equivalent sweep
    vehicle. Both components are assets the account already owns, so the sum
    can never exceed equity and never creates leverage. NOTE (item 190): the
    automatic pre-BUY liquidation of the vehicle no longer exists, so the
    parked component is owned but is not converted for you. See `TradingPipeline._compute_deployable_cash` for
    the Alpaca account-field semantics this rests on (verified 2026-08-19);
    that method is the engine's caller, not a second definition.

    This is the number Portfolio Manager sizing, the Risk Manager audit and
    the pre-trade cash gate all use — so it is also the number the operator
    must see under the word "deployable". A more conservative
    reserve-adjusted figure exists (`cash_above_reserve`) and is reported
    beside it under its own name.

    Fails closed: non-finite cash is 0.0, an unknowable sweep value adds
    nothing rather than inflating.
    """
    base = _finite(cash)
    if base is None:
        return 0.0
    parked = _finite(parked_sweep_value)
    if parked is None or parked <= 0:
        parked = 0.0
    return base + parked


def sweep_reserve_usd(total_value: Any, reserve_pct: Any) -> float:
    """The cash-sweep reserve floor in dollars: `total_value * pct / 100`.

    A SWEEP MECHANIC, not a risk limit: it was the cushion the end-of-session
    parking step left behind so an ordinary settlement or fee did not overdraw
    the account between sessions. It does not reduce what the desk may deploy
    today, which is why subtracting it from "deployable" produced a number no
    part of the engine ever used. Both bookend sweep operations were removed
    in item 190.

    Returns 0.0 for a non-positive/non-finite book value or percentage.
    """
    total = _finite(total_value)
    pct = _finite(reserve_pct)
    if total is None or total <= 0 or pct is None or pct <= 0:
        return 0.0
    return total * pct / 100.0


def cash_above_reserve(cash: Any, reserve_usd: Any) -> float:
    """Raw cash left after the sweep reserve floor, never below zero.

    The conservative companion to `deployable_cash`, kept under its own
    name because it answers a different question: how much RAW cash is
    spendable right now without selling the sweep vehicle first. Useful to
    show; wrong to label "deployable".
    """
    base = _finite(cash)
    if base is None:
        return 0.0
    reserve = _finite(reserve_usd)
    if reserve is None or reserve <= 0:
        reserve = 0.0
    return max(base - reserve, 0.0)


# ---------------------------------------------------------------------------
# 2. Market exposure — leverage-aware, signed
# ---------------------------------------------------------------------------

# Leveraged/inverse ETF multipliers for effective exposure calculation.
# Negative = inverse/short (hedge-like against the underlying index).
# New funds added here are picked up automatically by every consumer:
# the risk engine's multipliers, the gross ceiling's bearish test, and the
# API's display labeling (`inverse_etf_symbols()`). Nothing hand-maintains
# a second copy of this table.
ETF_LEVERAGE: dict[str, float] = {
    "SH": -1.0,  # -1x S&P 500
    "SDS": -2.0,  # -2x S&P 500
    "PSQ": -1.0,  # -1x Nasdaq 100
    "SQQQ": -3.0,  # -3x Nasdaq 100
    "DRAM": 1.0,  # 1x (normal ETF, no adjustment)
    "SMH": 1.0,
}


def effective_multiplier(symbol: str) -> float:
    """Signed exposure multiplier (negative for inverse ETFs).

    Used for net directional exposure — hedges cancel out.
    """
    return ETF_LEVERAGE.get(symbol, 1.0)


def gross_multiplier(symbol: str) -> float:
    """Unsigned leverage magnitude.

    Used for per-symbol and per-sector size limits where direction doesn't matter
    (a 3x ETF still consumes 3x notional regardless of long/short bias).
    """
    return abs(ETF_LEVERAGE.get(symbol, 1.0))


def inverse_etf_symbols() -> frozenset[str]:
    """The inverse/bearish funds in the universe, DERIVED from ETF_LEVERAGE.

    Previously hand-copied into `src/api/deps.py`, whose comment admitted it
    had to be kept in sync by hand while this table's comment promised new
    funds were "picked up automatically". Both cannot be true. Derived, so
    only one of them has to be.
    """
    return frozenset(sym for sym, mult in ETF_LEVERAGE.items() if mult < 0)


def net_exposure_usd(positions: Iterable[Any], *, cash_park_symbol: str | None = None) -> float:
    """Signed, leverage-weighted net exposure in DOLLARS.

    `sum(market_value * effective_multiplier(symbol))`. A hedge SUBTRACTS
    (that is what makes it a hedge) and a leveraged fund counts at its
    multiple. This is what the risk engine's `max_total_position_pct` rule
    measures, and therefore what a "% deployed" gauge drawn against that
    ceiling must measure.

    The cash park is excluded when `cash_park_symbol` is given — parked cash
    is not a position. The engine's own callers usually split it out
    upstream and pass None; the API passes the configured symbol because its
    positions payload is unsplit.

    Non-finite market values are skipped rather than poisoning the total
    with NaN (`unmeasurable_gross_symbols` in src/risk/rules.py is the
    engine's separate guard against acting on a total that silently
    excluded them).
    """
    park = (cash_park_symbol or "").strip().upper()
    total = 0.0
    for p in positions or []:
        symbol = str(_field(p, "symbol") or "").strip().upper()
        if park and symbol == park:
            continue
        market_value = _finite(_field(p, "market_value"))
        if market_value is None:
            continue
        total += market_value * effective_multiplier(symbol)
    return total


def net_exposure_pct(net_usd: Any, equity: Any) -> float | None:
    """Net exposure as a percentage of equity: `abs(net_usd) / equity * 100`.

    Absolute value, matching the risk rule: a book that is net SHORT 40% is
    just as exposed as one that is net long 40%, and the ceiling bounds
    both. Returns None when equity is non-positive or unusable — never 0.0,
    which would read on a dashboard as "flat" rather than "unknown".
    """
    net = _finite(net_usd)
    eq = _finite(equity)
    if net is None or eq is None or eq <= 0:
        return None
    return abs(net) / eq * 100.0


# ---------------------------------------------------------------------------
# 3. Average dollar volume — the liquidity admission measure
# ---------------------------------------------------------------------------

# The window every consumer uses. Three call sites previously used 20, 20
# and 21 bars; the 21 was the ordinary-monthly-window constant leaking into
# a measure whose name, config key and log lines all say 20.
AVG_DOLLAR_VOLUME_WINDOW = 20


def dollar_volumes(bars: Iterable[Any], window: int = AVG_DOLLAR_VOLUME_WINDOW) -> list[float]:
    """Per-session `close * volume` over the trailing `window` sessions.

    A HALTED session (zero volume) is a real session with zero dollar
    volume and is kept as 0.0. Dropping it — as one of the three previous
    implementations did, dividing by the surviving count — inflates the
    average in exactly the direction that wrongly ADMITS an illiquid symbol
    through a liquidity gate.

    A session whose close or volume is not a real, finite, non-negative
    number is UNKNOWABLE rather than zero, so it is dropped entirely and
    reduces the coverage count the caller can check via `len()`. The strict
    numeric test also keeps test MagicMocks (which answer `float()` with
    1.0) from smuggling phantom volume into the total.
    """
    bars_list = list(bars or [])
    if window > 0:
        bars_list = bars_list[-window:]
    out: list[float] = []
    for bar in bars_list:
        close = _finite(_field(bar, "close"))
        volume = _finite(_field(bar, "volume"))
        if close is None or volume is None:
            continue
        if close < 0 or volume < 0:
            continue
        out.append(close * volume)
    return out


def avg_dollar_volume(
    bars: Iterable[Any],
    window: int = AVG_DOLLAR_VOLUME_WINDOW,
    min_bars: int | None = None,
) -> float | None:
    """Mean daily dollar volume over the trailing `window` sessions.

    One definition, three thresholds: callers compare the result against
    their own limit ($10M for external-symbol admission, $5M for the
    top-mover digest, none for the tech-analyst prompt). Different
    thresholds for different purposes are correct; different MEASURES were
    not.

    `min_bars` is the coverage floor — how many usable sessions the window
    must contain for the answer to mean anything. Defaults to the full
    window (an admission gate should not judge liquidity off three bars);
    callers that deliberately tolerate short history pass a smaller number.
    Returns None when coverage is short — never 0.0, which a `< threshold`
    comparison would read as "definitely illiquid" instead of "unknown".

    A window in which NOTHING traded is likewise None, not 0.0. One halted
    session among twenty is a real zero and belongs in the average (that is
    the whole point of keeping it above); twenty consecutive halted sessions
    is not a liquidity measurement, it is a missing volume feed — no listed
    equity trades $0 for a month and stays listed. Reporting a data gap as a
    hard zero would be fabricating a measurement, which every other
    unavailable number in this system refuses to do.
    """
    values = dollar_volumes(bars, window=window)
    required = window if min_bars is None else min_bars
    if required < 1:
        required = 1
    if len(values) < required:
        return None
    if not any(v > 0 for v in values):
        return None
    return sum(values) / len(values)


def collapse_stances(values: Iterable[Any]) -> str | None:
    """Reduce several per-source stance labels to one, or `None` if there is
    nothing usable to reduce.

    One definition: this used to live only as
    `PortfolioManagerAgent._collapse_stances`, which `build_evidence_registry`
    calls to fold `(item.sentiment for item in items)` down to one stance per
    symbol. Phase 13's news verdict (`src/models.py::news_verdict_for_symbol`)
    needs the SAME reduction — multiple `StockNewsItem`s per symbol collapsed
    to one `AnalystVerdict.direction` — and `src/models.py` cannot import
    `src.agents.portfolio_manager` (that module already imports `src.models`,
    so the reverse would be circular). Moving the arithmetic here, dependency-
    free and upstream of both, lets `PortfolioManagerAgent._collapse_stances`
    become a thin wrapper instead of a second definition that could drift
    from this one.

    Case- and whitespace-normalized; "none"/"n/a"/"na"/"unknown"/
    "unavailable"/"not_available" are treated as absent. A single surviving
    value is returned verbatim (whatever vocabulary it came from — the
    caller may not use bullish/bearish/neutral, e.g. `TechAnalysisResult`
    ratings). Multiple surviving values are only resolved to "bullish" or
    "bearish" when EVERY value is drawn from the matching polarity set below;
    any other disagreement (including a directional value alongside
    "neutral") returns "mixed" — an unresolved split, not invented agreement.
    """
    cleaned = {
        str(value).strip().lower().replace(" ", "_") for value in values if value is not None and str(value).strip()
    }
    cleaned -= {"none", "n/a", "na", "unknown", "unavailable", "not_available"}
    if not cleaned:
        return None
    if len(cleaned) == 1:
        return next(iter(cleaned))
    positive = {"strong_buy", "buy", "bullish", "positive", "risk_on", "overweight", "favorable"}
    negative = {"strong_sell", "sell", "bearish", "negative", "risk_off", "underweight", "unfavorable"}
    if cleaned <= positive:
        return "bullish"
    if cleaned <= negative:
        return "bearish"
    if cleaned <= {"neutral", "mixed"}:
        return "neutral" if cleaned == {"neutral"} else "mixed"
    return "mixed"


# ---------------------------------------------------------------------------
# Moved here verbatim so src/api can import them (this module is the stdlib-only seam).
# ---------------------------------------------------------------------------


def r_multiple(
    current_price: float,
    entry: float,
    initial_stop: float,
    qty: float = 1.0,
) -> float | None:
    """Profit in units of the risk originally taken, or None if undefined.

    `initial_stop` is the stop the position was opened with. Returns None when
    any input is missing/non-finite or when the initial stop was not on the
    losing side of entry — a non-positive denominator means no risk was
    defined at entry and an R-multiple would be a fabrication.

    `qty` supplies only the SIDE. A negative qty is a short: risk per share is
    `stop - entry` (the stop sits ABOVE entry) and profit accrues as price
    falls, so both numerator and denominator flip. Defaults to +1.0 so every
    existing long call site is unchanged.
    """
    cur = _finite(current_price)
    ent = _finite(entry)
    stop = _finite(initial_stop)
    if cur is None or ent is None or stop is None:
        return None
    side = -1.0 if (_finite(qty) or 1.0) < 0 else 1.0
    risk_per_share = side * (ent - stop)
    if risk_per_share <= 0:
        return None
    return round(side * (cur - ent) / risk_per_share, 2)


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


def _split_chain(rows: list[dict]) -> tuple[list[dict], list[dict], str | None]:
    """(opening rows, exit rows, direction) of one position chain. Split out of
    `summarize_closed_position` unchanged, only to keep each function simple."""
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
    return opens, exits, direction


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
    opens, exits, direction = _split_chain(rows)
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


def recorded_initial_stop(row: dict | None) -> float:
    """The stop AT ENTRY, even after later write-backs of the live level.

    Prefers `initial_stop_loss` when that column is present on the row.
    A present-but-empty value is 0 — it must not fall back to `stop_loss`,
    because after a write-back that column is the live level, and a row
    that opened with no stop must not mint an entry bet from a later
    repair. Test doubles that omit the key entirely still fall back to
    `stop_loss` (legacy shape, entry == live). Never invents a price.
    """
    if not row:
        return 0.0
    if "initial_stop_loss" in row:
        return classify_stop_price(row.get("initial_stop_loss"))[1]
    return classify_stop_price(row.get("stop_loss"))[1]
