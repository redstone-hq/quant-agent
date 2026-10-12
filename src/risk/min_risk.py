"""The owner's minimum risk per position — ONE definition, every caller.

Owner rule 2026-08-27: risk per new position is 0.5-5% of equity, and below
the floor the desk does not trade. The floor's value is the setting
`risk.min_position_risk_pct` (`PortfolioConstructorConfig.min_risk_pct` in
the constructor); nothing here hard-codes it.

Reading of the rule (owner ruling 2026-10-12, "we also must filter out small
trade churn"): the WHOLE position after the order — held same-side size plus
this order — must risk at least the floor at its stop, not the order alone.
An add that leaves the position above the floor passes; a remnant whose whole
position is under it is refused. Per the same ruling an order is never sized
UP to reach the floor: below it, the order is refused.

Before this module the rule was computed three ways (constructor by weight,
execution by shares x stop distance, rotation by flat notional against
equity — which is only right for a 100% stop). Every caller now hands
`min_risk_shortfall` the same five facts and reads the same answer.
"""

from __future__ import annotations

import math
from dataclasses import dataclass


def _num(value) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    return value if math.isfinite(value) else None


@dataclass(frozen=True)
class MinRiskCheck:
    """One position judged against the floor.

    `risk_pct` is `None` when entry, stop, size or equity was unreadable:
    the risk is UNKNOWN, which is a refusal for a risk-adding order (a buy
    with no stop has unknown risk), never a pass.
    """

    floor_pct: float
    risk_pct: float | None
    shortfall_pct: float | None
    #: The smallest position notional (same units as `position_notional`)
    #: that clears the floor at this entry and stop; `None` if unreadable.
    min_notional: float | None

    @property
    def readable(self) -> bool:
        return self.risk_pct is not None

    @property
    def below_floor(self) -> bool:
        """Measured, and under the floor."""
        return self.shortfall_pct is not None and self.shortfall_pct > 0

    @property
    def refused(self) -> bool:
        """What a risk-adding order must do: refuse when under the floor OR
        unreadable. A floor of 0 switches the rule off."""
        return self.floor_pct > 0 and (not self.readable or self.below_floor)


def min_risk_shortfall(
    *,
    position_notional: float | None,
    entry: float | None,
    stop: float | None,
    equity: float | None,
    floor_pct: float,
    tolerance_pct: float = 0.0,
) -> MinRiskCheck:
    """How far the WHOLE position after the order falls short of the floor.

    `position_notional` is the position's size after the order at `entry`
    (held same-side + this order), in the same units as `equity` — dollars,
    or percent-of-book with `equity=100`. Risk at the stop is
    `notional x |entry - stop| / entry`, as a percent of `equity`.
    `tolerance_pct` forgives a known rounding step and nothing else.
    """
    floor = float(floor_pct)
    e, s, n, eq = _num(entry), _num(stop), _num(position_notional), _num(equity)
    if e is None or s is None or eq is None or e <= 0 or eq <= 0 or s <= 0 or s == e:
        return MinRiskCheck(floor, None, None, None)
    stop_frac = abs(e - s) / e
    min_notional = floor / 100.0 * eq / stop_frac
    if n is None or n < 0:
        return MinRiskCheck(floor, None, None, min_notional)
    risk_pct = n * stop_frac / eq * 100.0
    shortfall = max(0.0, floor - risk_pct - float(tolerance_pct))
    if shortfall <= 1e-9:
        shortfall = 0.0
    return MinRiskCheck(floor, risk_pct, shortfall, min_notional)


@dataclass(frozen=True)
class MinRiskFloor:
    """The floor plus each candidate's own entry and stop, threaded to the
    rotation pre-check so "short of cash" is judged at the candidate's real
    stop distance through `min_risk_shortfall`."""

    floor_pct: float
    equity_usd: float | None
    #: symbol (upper-case) -> (entry, stop) the constructor would ship.
    entry_stop: dict[str, tuple[float, float]]

    def check(self, symbol: str | None, position_notional: float | None) -> MinRiskCheck:
        entry, stop = self.entry_stop.get(str(symbol or "").upper(), (None, None))
        return min_risk_shortfall(
            position_notional=position_notional,
            entry=entry,
            stop=stop,
            equity=self.equity_usd,
            floor_pct=self.floor_pct,
        )
