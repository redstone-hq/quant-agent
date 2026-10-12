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
from typing import Any, Iterable

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
