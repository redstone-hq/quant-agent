"""Plain records the backtest engine carries: an open position and a closed trade, plus the slippage fill rule.

Moved out of ``engine`` unchanged so that module can stop growing; ``engine``
re-exports the names.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date


@dataclass
class _OpenPosition:
    symbol: str
    direction: str  # "long" | "short"
    signal_date: date
    entry_date: date
    entry_index: int
    entry_price: float  # fill price (slippage-adjusted)
    stop_initial: float
    stop: float  # current (possibly trailed) stop
    target: float | None
    setup_type: str
    shares: float
    risk_pct: float


@dataclass(frozen=True)
class Trade:
    """One closed round-trip. Every price here is a FILL price (slippage
    applied) except `stop_price` / `target_price`, which are the structural
    price LEVELS the trade was managed against."""

    symbol: str
    direction: str  # "long" | "short"
    signal_date: date
    entry_date: date
    entry_price: float
    stop_price: float
    target_price: float | None
    exit_date: date
    exit_price: float
    exit_reason: str  # "stop" | "horizon" | "end_of_data"
    shares: float
    risk_pct: float
    setup_type: str
    hold_days: int
    pnl: float
    r_multiple: float


def _fill_price(raw_price: float, direction: str, side: str, slippage_bps: float) -> float:
    """Apply a flat slippage assumption in the ADVERSE direction only.

    `side` is "open" (establishing the position) or "close" (exiting it).
    Buying always costs slippage; selling always gives it up. A long open
    and a short close are both buys; a long close and a short open are both
    sells.
    """
    frac = slippage_bps / 10_000.0
    buying = (direction == "long" and side == "open") or (direction == "short" and side == "close")
    return raw_price * (1 + frac) if buying else raw_price * (1 - frac)


def _close_trade(
    pos: _OpenPosition, exit_idx: int, exit_date_: date, raw_exit: float, exit_reason: str, slippage_bps: float
) -> Trade:
    fill = _fill_price(raw_exit, pos.direction, "close", slippage_bps)
    if pos.direction == "long":
        pnl = (fill - pos.entry_price) * pos.shares
    else:
        pnl = (pos.entry_price - fill) * pos.shares
    risk_per_share = abs(pos.entry_price - pos.stop_initial)
    r_multiple = pnl / (pos.shares * risk_per_share) if risk_per_share > 0 else 0.0
    return Trade(
        symbol=pos.symbol,
        direction=pos.direction,
        signal_date=pos.signal_date,
        entry_date=pos.entry_date,
        entry_price=round(pos.entry_price, 4),
        stop_price=round(pos.stop_initial, 4),
        target_price=round(pos.target, 4) if pos.target is not None else None,
        exit_date=exit_date_,
        exit_price=round(fill, 4),
        exit_reason=exit_reason,
        shares=pos.shares,
        risk_pct=round(pos.risk_pct, 4),
        setup_type=pos.setup_type,
        hold_days=exit_idx - pos.entry_index,
        pnl=round(pnl, 2),
        r_multiple=round(r_multiple, 4),
    )
