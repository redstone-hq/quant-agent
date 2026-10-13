"""Position rules the backtest engine applies to an open position.

Moved out of ``engine`` unchanged so that module can stop growing: how much
risk budget an open position still consumes, and whether today's bar closes
it, plus the sizing formula. They read only plain inputs; ``engine`` re-exports them.
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING

from src.models import OHLCV
from src.risk.rules import _gross_multiplier

if TYPE_CHECKING:
    from src.backtest.engine import _OpenPosition


def _existing_risk_pct(pos: _OpenPosition, equity: float) -> float:
    """Risk % of equity an open position is currently consuming, based on
    its LIVE (possibly trailed) stop. Once a stop trails to or past entry,
    the position stops consuming budget — same behaviour `trailing.py`
    documents ("this position stops consuming risk budget")."""
    if pos.direction == "long":
        risk_per_share = max(0.0, pos.entry_price - pos.stop)
    else:
        risk_per_share = max(0.0, pos.stop - pos.entry_price)
    if equity <= 0:
        return 0.0
    return pos.shares * risk_per_share / equity * 100.0


def _check_exit(
    pos: _OpenPosition,
    bar: OHLCV,
    idx: int,
    max_hold_days: int,
) -> tuple[str | None, float | None]:
    """Whether today's bar closes `pos`, and at what raw (pre-slippage)
    price. Returns `(None, None)` when the position stays open.

    NO TARGET EXIT: the live desk never sells at a target; profit-taking is
    the trailing stop alone, so the backtest exits only on the (possibly
    trailed) stop or the hold horizon. `pos.target` is a reference price for
    sizing and the trailing stop, never an exit.

    GAP FILLS: a bar that opens through the stop fills at the open, not the
    stop (nothing trades at the stop price on a gap). Factored out from the
    main loop so it is directly unit-testable for both directions.
    """
    if pos.direction == "long":
        if bar.low <= pos.stop:
            # Gap through the stop: nothing trades at the stop, so fill at the open.
            return "stop", min(bar.open, pos.stop)
    else:
        if bar.high >= pos.stop:
            return "stop", max(bar.open, pos.stop)

    hold_days = idx - pos.entry_index
    if hold_days >= max_hold_days:
        return "horizon", bar.close
    return None, None


def _size_position(
    *,
    equity: float,
    granted_risk_pct: float,
    fill_entry: float,
    stop: float,
    symbol: str,
    max_position_pct: float,
) -> tuple[int, float]:
    """§2.1 formula: shares = equity x risk_pct/100 / |entry - stop|,
    clamped by the single-name notional ceiling on a GROSS-leverage basis
    (mirrors `PortfolioConstructor`'s single-name trim — see
    src/portfolio_constructor.py around the `max_position_pct` comment).
    Returns (whole shares, the risk_pct actually consumed after rounding
    down to a whole share and after any clamp)."""
    risk_per_share = abs(fill_entry - stop)
    if risk_per_share <= 0 or granted_risk_pct <= 0 or equity <= 0:
        return 0, 0.0
    risk_dollars = equity * granted_risk_pct / 100.0
    shares = risk_dollars / risk_per_share
    notional = shares * fill_entry
    gross_mul = _gross_multiplier(symbol)
    gross_notional = notional * gross_mul
    max_notional = equity * max_position_pct / 100.0
    if max_notional > 0 and gross_notional > max_notional:
        shares *= max_notional / gross_notional
    shares_int = math.floor(shares)
    if shares_int < 1:
        return 0, 0.0
    effective_risk_pct = shares_int * risk_per_share / equity * 100.0
    return shares_int, effective_risk_pct
