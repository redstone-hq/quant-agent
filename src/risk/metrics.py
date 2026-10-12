"""Deterministic risk arithmetic — R-multiple, per-position risk, portfolio heat.

Phase 2 of `docs/QAMC_REMEDIATION_SPEC.md`, and audit findings §1.3 / §1.4 in
`docs/AGENT_ROLE_AUDIT.md`. Three quantities the desk has been reasoning about
in prose for months without any code ever computing them.

**Budget risk** — what a position costs against its own cost basis if the stop
is hit: `abs(qty) x max(0, entry - stop)` for a long, `abs(qty) x max(0, stop -
entry)` for a short. This is the number the owner-ratified 25%
at-risk ceiling is defined against, and it is the one that gets *released*:
once a trailing stop sits on the side of entry that can no longer lose money
— at or above for a long, at or below for a short — its budget risk is zero
and it stops consuming the book's risk budget (spec §2.3). The book therefore
expands while trades work and contracts while they do not, with nobody
choosing a position count (§2.4).

**Open risk** — what the book loses from TODAY's price if every stop fires at
once: `abs(qty) x max(0, current_price - stop)` for a long, mirrored for a short.
This is audit §1.3's "portfolio
heat". It is a different number from budget risk and neither substitutes for the
other: a winner 40% above entry with its stop still below entry has released no
budget but carries a great deal of open risk.

**R-multiple** — profit in units of the risk originally taken:
`(current - entry) / (entry - initial_stop)`. Measured against the stop the
position was OPENED with, never the trailed one, because the denominator is the
bet that was actually made. `thesis_progress_pct` measures distance to target,
which is a different question and does not normalise for how much was risked
(audit §1.4).

A position with no stop at all is not a zero-risk position. `protected=False`
charges its full notional to the risk budget, because that is what an
unprotected position can actually lose. Cash-equivalent sweep holdings (SGOV)
are deliberately stopless and must be excluded by the caller rather than
counted as unprotected — see `exclude_symbols`.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

__all__ = [
    "PositionRisk",
    "PortfolioHeat",
    "BookRiskPct",
    "full_value_charge",
    "r_multiple",
    "position_risk",
    "portfolio_heat",
    "format_heat_block",
    "unrealized_pnl_pct",
]


def _finite(value: object) -> float | None:
    """Coerce to a finite float, or None. Broker snapshots carry NaN."""
    try:
        out = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def unrealized_pnl_pct(position) -> float | None:
    """Unrealized P&L as a percent of the position's ABSOLUTE cost basis.

    **The single definition. Every seat that renders a P&L percent calls
    this one function** — position_reviewer's position block, the PM's
    position lines, `_build_position_facts`
    and `_build_thesis_health_context`. Five call sites previously carried
    two different denominators.

    The denominator is `abs(avg_entry * qty)`, and the `abs()` is the whole
    point. `qty` is NEGATIVE for a held short (Alpaca's convention, mirrored
    throughout this codebase), so a signed `avg_entry * qty` cost basis is
    negative for every short. Two failure modes followed from that, and both
    were live:

    - A bare `pnl / cost` FLIPS the sign of every short: a winning short
      (price fell, `unrealized_pnl > 0`) rendered as a negative percentage
      and read as a loser.
    - The `if cost > 0` guard that some call sites used instead of `abs()`
      does not fix that — it silently substitutes **zero**. A short
      -100 @ $110 now trading at $100 is +$1,000 of profit and rendered
      `P&L: $1000.00 (+0.0%)`: a line that contradicts itself, on one line,
      in the PM's own prompt.

    The sign the reader needs comes from `unrealized_pnl` alone; the
    denominator's job is the MAGNITUDE of capital committed.

    Returns `None` — never `0.0` — when the percent is genuinely unknowable
    (no entry, no quantity, a non-finite broker snapshot). Callers render
    "N/A"/"unknown" rather than a confident zero, and comparisons against a
    threshold must treat None as "not flagged", not as "below".
    """
    entry = _finite(getattr(position, "avg_entry", None))
    qty = _finite(getattr(position, "qty", None))
    pnl = _finite(getattr(position, "unrealized_pnl", None))
    if entry is None or qty is None or pnl is None:
        return None
    cost = abs(entry * qty)
    if not cost or not math.isfinite(cost):
        return None
    return pnl / cost * 100


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
class PositionRisk:
    """One position's contribution to the book's risk."""

    symbol: str
    qty: float
    entry: float
    current_price: float
    stop: float | None
    initial_stop: float | None
    #: Charged against the 25% at-risk ceiling. Zero once the stop is at or
    #: above entry; full notional when the position carries no stop at all.
    budget_risk_dollars: float
    #: Loss from today's price if the stop fired now (audit §1.3 heat).
    open_risk_dollars: float
    #: True when a stop is known and sits on the no-longer-losing side of
    #: entry (at/above for a long, at/below for a short) — risk released.
    risk_released: bool
    #: False when no stop is known. Full notional is charged to the budget.
    protected: bool
    r_multiple: float | None
    #: True when the charge above could not be computed from real numbers —
    #: no finite current price, or no finite entry. A missing price is
    #: UNKNOWN, never $0: while any holding is unknown the constructor refuses
    #: every risk-ADDING order (exits and stops are never blocked).
    risk_unknown: bool = False

    @property
    def market_value(self) -> float:
        return self.qty * self.current_price


def full_value_charge(qty: object, current_price: object, entry: object = None) -> tuple[float, bool]:
    """Worst-case budget charge — the whole position at market — and whether it is UNKNOWN.

    `(abs(qty) * price, False)` when a finite positive price exists. With no
    price the charge cannot be measured: `(abs(qty) * entry, True)` when an
    entry exists (a stand-in, still flagged unknown), else `(0.0, True)`.
    Never `(0.0, False)` for a held position with no price.
    """
    shares = abs(_finite(qty) or 0.0)
    cur = _finite(current_price)
    if cur is not None and cur > 0:
        return round(shares * cur, 2), False
    ent = _finite(entry)
    return (round(shares * ent, 2) if ent is not None and ent > 0 else 0.0), True


def position_risk(
    symbol: str,
    qty: float,
    entry: float,
    current_price: float,
    stop: float | None,
    initial_stop: float | None = None,
) -> PositionRisk:
    """Risk arithmetic for a single position, long or short.

    `stop` is the LIVE stop (broker truth, already trailed). `initial_stop` is
    the level the position was opened with; it defaults to `stop` so callers
    that only know one level still get a defensible R-multiple denominator.

    A negative `qty` is a short (Alpaca's own sign convention, passed through
    untouched by `broker.get_positions`). Its stop sits ABOVE entry, so risk
    per share is `stop - entry` and open risk per share is `stop - current`.
    Every dollar figure below is computed as `abs(qty) * per-share risk`, which
    reduces to the pre-existing long arithmetic exactly when qty > 0.
    """
    qty_f = _finite(qty) or 0.0
    # A missing price is UNKNOWN, never $0 (defect 2026-10-09): `or 0.0`
    # here charged an unprotected holding with no price at $0 — the riskiest
    # reading scored as the safest. The 0.0 placeholders below are never
    # charged as if real; `risk_unknown` says so.
    entry_raw = _finite(entry)
    cur_raw = _finite(current_price)
    price_unknown = cur_raw is None or cur_raw <= 0
    entry_unknown = entry_raw is None or entry_raw <= 0
    entry_f = 0.0 if entry_unknown else float(entry_raw)  # type: ignore[arg-type]
    cur_f = 0.0 if price_unknown else float(cur_raw)  # type: ignore[arg-type]
    stop_f = _finite(stop) if stop is not None else None
    if stop_f is not None and stop_f <= 0:
        stop_f = None
    init_f = _finite(initial_stop) if initial_stop is not None else None
    if init_f is not None and init_f <= 0:
        init_f = None
    if init_f is None:
        init_f = stop_f

    # +1 long, -1 short. `side` flips every per-share distance below; `shares`
    # is the unsigned share count that turns a per-share risk into dollars.
    side = -1.0 if qty_f < 0 else 1.0
    shares = abs(qty_f)

    protected = stop_f is not None and qty_f != 0
    if not protected:
        # No stop is not zero risk. The whole position is exposed, so the whole
        # notional is charged to the budget and to heat. Silently scoring this
        # as 0.0 would make an unprotected book look like the safest one. A
        # short's notional is its absolute exposure — max(0.0, qty * price)
        # scored every short as riskless.
        notional, unknown = full_value_charge(qty_f, cur_f, entry_f)
        return PositionRisk(
            symbol=symbol,
            qty=qty_f,
            entry=entry_f,
            current_price=cur_f,
            stop=None,
            initial_stop=init_f,
            budget_risk_dollars=notional,
            open_risk_dollars=notional,
            risk_released=False,
            protected=False,
            r_multiple=(r_multiple(cur_f, entry_f, init_f, qty_f) if init_f and not price_unknown else None),
            risk_unknown=unknown,
        )

    assert stop_f is not None  # narrowed by `protected`
    # Released = the stop can no longer lose money against entry. For a long
    # that is stop >= entry; for a short it is stop <= entry.
    released = entry_f > 0 and side * (stop_f - entry_f) >= 0
    if entry_unknown:
        # No entry means no entry-to-stop distance: charge the worst case,
        # the whole position at market. Unknown only if the price is too.
        budget, unknown = full_value_charge(qty_f, cur_f, entry_f)
    else:
        budget = 0.0 if released else max(0.0, shares * side * (entry_f - stop_f))
        unknown = price_unknown
    # No price: today's loss-to-stop cannot be measured; carry the budget
    # figure rather than a confident $0, and flag the row unknown.
    open_r = budget if price_unknown else max(0.0, shares * side * (cur_f - stop_f))
    return PositionRisk(
        symbol=symbol,
        qty=qty_f,
        entry=entry_f,
        current_price=cur_f,
        stop=stop_f,
        initial_stop=init_f,
        budget_risk_dollars=round(budget, 2),
        open_risk_dollars=round(open_r, 2),
        risk_released=released,
        protected=True,
        r_multiple=(r_multiple(cur_f, entry_f, init_f, qty_f) if init_f and not price_unknown else None),
        risk_unknown=unknown,
    )


@dataclass(frozen=True)
class PortfolioHeat:
    """Book-level roll-up of `PositionRisk`."""

    equity: float
    per_position: list[PositionRisk] = field(default_factory=list)

    @property
    def budget_risk_dollars(self) -> float:
        return round(sum(p.budget_risk_dollars for p in self.per_position), 2)

    @property
    def open_risk_dollars(self) -> float:
        return round(sum(p.open_risk_dollars for p in self.per_position), 2)

    @property
    def budget_risk_pct(self) -> float:
        """At-risk % of equity — the figure the 25% ceiling bounds."""
        if self.equity <= 0:
            return 0.0
        return round(self.budget_risk_dollars / self.equity * 100, 2)

    @property
    def open_risk_pct(self) -> float:
        if self.equity <= 0:
            return 0.0
        return round(self.open_risk_dollars / self.equity * 100, 2)

    @property
    def unprotected(self) -> list[str]:
        return sorted(p.symbol for p in self.per_position if not p.protected)

    @property
    def unknown_risk(self) -> list[str]:
        """Holdings whose charge could not be measured (no price). Buys wait on these."""
        return sorted(p.symbol for p in self.per_position if p.risk_unknown)

    @property
    def released(self) -> list[str]:
        return sorted(p.symbol for p in self.per_position if p.risk_released)

    def headroom_pct(self, ceiling_pct: float) -> float:
        """Budget still available under `ceiling_pct`, floored at zero."""
        return round(max(0.0, ceiling_pct - self.budget_risk_pct), 2)


def portfolio_heat(
    positions,
    equity: float,
    stops: dict[str, float] | None = None,
    initial_stops: dict[str, float] | None = None,
    exclude_symbols: set[str] | frozenset[str] | None = None,
) -> PortfolioHeat:
    """Roll `positions` (anything with symbol/qty/avg_entry/current_price) up.

    `stops` maps symbol → live stop price; a symbol absent from the map is
    treated as unprotected. `exclude_symbols` drops cash-equivalent sweep
    holdings, which are deliberately stopless and are not risk positions.
    """
    stops = stops or {}
    initial_stops = initial_stops or {}
    excluded = {s.upper() for s in (exclude_symbols or set())}
    equity_f = _finite(equity) or 0.0
    rows: list[PositionRisk] = []
    for p in positions:
        symbol = str(getattr(p, "symbol", "") or "").upper()
        if not symbol or symbol in excluded:
            continue
        qty = _finite(getattr(p, "qty", 0.0)) or 0.0
        # A short carries a negative qty (Alpaca convention) and real risk.
        # Skipping qty <= 0 exempted every short from the at-risk ceiling.
        if qty == 0:
            continue
        # Caught PER POSITION (defect 2026-10-09): one holding whose stop or
        # price could not be read used to raise out of the whole roll-up, the
        # book's risk became None, and the 25% ceiling was skipped for every
        # trade. Now only that holding degrades — charged at its full market
        # value, the worst case — and the ceiling is still enforced.
        try:
            rows.append(
                position_risk(
                    symbol=symbol,
                    qty=qty,
                    entry=getattr(p, "avg_entry", None),
                    current_price=getattr(p, "current_price", None),
                    stop=stops.get(symbol),
                    initial_stop=initial_stops.get(symbol),
                )
            )
        except Exception as exc:  # noqa: BLE001 — degrade one row, never the book
            rows.append(_full_value_row(symbol, qty, p, exc))
    return PortfolioHeat(equity=max(0.0, equity_f), per_position=rows)


def _full_value_row(symbol: str, qty: float, position: object, exc: BaseException) -> PositionRisk:
    """The worst-case row for a holding whose risk could not be computed."""
    cur = _finite(getattr(position, "current_price", None))
    entry = _finite(getattr(position, "avg_entry", None))
    charge, unknown = full_value_charge(qty, cur, entry)
    logger.error(
        "risk budget: %s risk could not be read (%s) — charged at full market value $%.2f%s",
        symbol,
        exc,
        charge,
        " (price UNKNOWN: new buys refused until it is read)" if unknown else "",
    )
    return PositionRisk(
        symbol=symbol,
        qty=qty,
        entry=entry or 0.0,
        current_price=cur or 0.0,
        stop=None,
        initial_stop=None,
        budget_risk_dollars=charge,
        open_risk_dollars=charge,
        risk_released=False,
        protected=False,
        r_multiple=None,
        risk_unknown=unknown,
    )


class BookRiskPct(dict):
    """`{symbol: budget risk % of equity}` that also carries which holdings are UNKNOWN.

    A plain dict cannot say "this book has a holding whose risk could not be
    measured", so that fact used to be dropped between the heat roll-up and
    the constructor. The unknown set travels WITH the numbers it qualifies:
    no caller can take one without the other. `unknown` non-empty means the
    constructor refuses every risk-adding order (exits and stops pass).
    """

    def __init__(self, data=(), *, unknown: frozenset[str] | set[str] = frozenset()) -> None:
        super().__init__(data)
        self.unknown: frozenset[str] = frozenset(str(s).upper() for s in unknown)


def format_heat_block(
    heat: PortfolioHeat,
    ceiling_pct: float,
    *,
    title: str = "Portfolio Risk (deterministic, computed in Python)",
) -> str:
    """Render heat for an agent prompt. Facts only — no instruction text."""
    if not heat.per_position:
        return f"## {title}\n- No risk-bearing positions. Full {ceiling_pct:.0f}% risk budget is available.\n"
    lines = [
        f"## {title}",
        f"- At-risk (vs entry, consumes the budget): "
        f"${heat.budget_risk_dollars:,.0f} = {heat.budget_risk_pct:.2f}% of "
        f"equity | ceiling {ceiling_pct:.0f}% | headroom "
        f"{heat.headroom_pct(ceiling_pct):.2f}%",
        f"- Open risk (loss from today's price if every stop fired): "
        f"${heat.open_risk_dollars:,.0f} = {heat.open_risk_pct:.2f}% of equity",
    ]
    if heat.released:
        lines.append(
            f"- Risk RELEASED (stop can no longer lose vs entry, consumes no budget): {', '.join(heat.released)}"
        )
    if heat.unprotected:
        lines.append(f"- ⚠️ UNPROTECTED (no stop found — charged at full notional): {', '.join(heat.unprotected)}")
    if heat.unknown_risk:
        lines.append(
            f"- RISK UNKNOWN (no usable price — never scored as $0; new buys refused until read): "
            f"{', '.join(heat.unknown_risk)}"
        )
    if getattr(heat, "unreadable", None):
        lines.append(heat.unreadable_note())
    lines.append("- Per position: symbol | at-risk $ | % equity | R-multiple")
    for p in sorted(heat.per_position, key=lambda x: -x.budget_risk_dollars):
        pct = (p.budget_risk_dollars / heat.equity * 100) if heat.equity > 0 else 0.0
        r_str = f"{p.r_multiple:+.2f}R" if p.r_multiple is not None else "R n/a"
        flag = ""
        if p.risk_released:
            flag = " (released)"
        elif not p.protected:
            flag = " (UNPROTECTED)"
        lines.append(f"  - {p.symbol}: ${p.budget_risk_dollars:,.0f} | {pct:.2f}% | {r_str}{flag}")
    return "\n".join(lines) + "\n"
