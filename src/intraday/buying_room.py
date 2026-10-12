"""src.intraday.buying_room -- is there room to open any position before the paid PM call?

Owner ruling 2026-10-12 (approved): no paid portfolio-manager buy call when
the account has no room to open any new position. Measured that day: 17 of
19 paid intraday PM calls answered "no room" at the 2.0x gross ceiling.

Nothing here is a new number. The headroom is the SAME figure execution sizes
new entries against and the PM prompt prints (`_entry_deployment_budget`,
§11.2 ladder, or settled cash with margin off). The smallest worthwhile
position is the owner's minimum risk floor (`risk.min_position_risk_pct`)
at the candidate's own stop, the stop the constructor would ship
(`entry_stop_preview`), through the one shared `min_risk_shortfall`. A
candidate with no computable stop cannot make the call worthwhile.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from src.risk.min_risk import min_risk_shortfall

logger = logging.getLogger(__name__)

#: `ctx.pm_skip_reason` for a check whose PM call was skipped for no room;
#: the decision stage records it as a deliberate skip, never a PM failure,
#: and still runs the below-entry-bar sale.
NO_BUYING_ROOM = "intraday_no_buying_room"


def _num(value) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _held_same_side_usd(positions, symbol: str, direction: str) -> float:
    """Market value already held on this side; the floor reads the WHOLE position."""
    for p in positions or []:
        get = p.get if isinstance(p, dict) else lambda k, _p=p: getattr(_p, k, None)
        if str(get("symbol") or "").upper() != symbol:
            continue
        mv = _num(get("market_value"))
        if mv is None:
            return 0.0
        if (mv > 0 and direction == "long") or (mv < 0 and direction == "short"):
            return abs(mv)
    return 0.0


@dataclass(frozen=True)
class BuyingRoom:
    headroom_usd: float
    limit: str
    ladder_backed: bool
    floor_pct: float
    #: the §11.2 gross ceiling as a multiple of equity; None when unresolved
    #: (the headroom is then settled cash)
    ceiling_x: float | None = None
    #: one row per mover considered: symbol, direction, min_notional_usd (None = no stop)
    candidates: list[dict] = field(default_factory=list)

    @property
    def has_room(self) -> bool:
        return any(row["fits"] for row in self.candidates)

    def as_record(self) -> dict:
        return {
            "pm_call_skipped": NO_BUYING_ROOM,
            "limit": self.limit,
            "headroom_usd": round(self.headroom_usd, 2),
            "gross_ceiling_x": self.ceiling_x if self.ladder_backed else None,
            "min_position_risk_pct": self.floor_pct,
            "candidates_considered": [
                {k: row[k] for k in ("symbol", "direction", "min_notional_usd")} for row in self.candidates
            ],
        }


def assess_buying_room(pipeline, ctx, analyses, mover_symbols) -> BuyingRoom:
    """Headroom and, per mover, the smallest position that clears the floor."""
    from src.pipeline_gross_ceiling import _session_gross_ceiling
    from src.pipeline_sizing import _entry_deployment_budget, _min_position_risk_pct
    from src.pipeline_stage_helpers import _macro_regime

    headroom, ladder_backed, note = _entry_deployment_budget(
        pipeline, ctx, ctx.positions, ctx.total_value, ctx.deployable_cash
    )
    headroom = float(headroom)
    floor = _min_position_risk_pct(pipeline)
    equity = _num(ctx.total_value)
    regime = _macro_regime(getattr(ctx, "macro_analysis", None))
    movers = {str(s).upper() for s in mover_symbols}
    rows: list[dict] = []
    for analysis in analyses:
        symbol = str(analysis.symbol).upper()
        if symbol not in movers:
            continue
        direction = "short" if analysis.rating in ("sell", "strong_sell") else "long"
        try:
            preview = pipeline.portfolio_constructor.entry_stop_preview(analysis, direction, regime=regime)
        except Exception as exc:  # noqa: BLE001 -- no stop = cannot make the call worthwhile
            logger.warning("buying room: entry/stop preview failed for %s: %s", symbol, exc)
            preview = None
        min_notional = None
        if isinstance(preview, tuple) and len(preview) == 3:
            check = min_risk_shortfall(
                position_notional=None, entry=preview[0], stop=preview[1], equity=equity, floor_pct=floor
            )
            min_notional = check.min_notional
        needed = None
        if min_notional is not None:
            needed = max(0.0, min_notional - _held_same_side_usd(ctx.positions, symbol, direction))
        # A short does not draw on the settled-cash fallback (D11); a gross
        # ladder budget it consumes like a long.
        fits = needed is not None and (
            (headroom > 0 and needed <= headroom) or (direction == "short" and not ladder_backed)
        )
        rows.append(
            {
                "symbol": symbol,
                "direction": direction,
                "min_notional_usd": None if needed is None else round(needed, 2),
                "fits": fits,
            }
        )
    ceiling = _session_gross_ceiling(pipeline, ctx)
    ceiling_x = _num(getattr(ceiling, "ceiling_x", None)) if ceiling is not None else None
    return BuyingRoom(headroom, note, bool(ladder_backed), floor, ceiling_x, rows)


def no_room_sentence(record: dict) -> str:
    """The owner's plain sentence for a check whose paid PM call was skipped."""
    headroom = record.get("headroom_usd")
    head = f"${headroom:,.0f}" if isinstance(headroom, (int, float)) else "unknown"
    ceiling = record.get("gross_ceiling_x")
    where = f"under the {ceiling:g}x limit" if isinstance(ceiling, (int, float)) else "of settled cash"
    needs = [
        c["min_notional_usd"]
        for c in record.get("candidates_considered") or []
        if isinstance(c.get("min_notional_usd"), (int, float))
    ]
    tail = f"; the smallest needed ${min(needs):,.0f}" if needs and min(needs) > 0 else ""
    if not needs and record.get("candidates_considered"):
        tail = "; no mover had a readable stop"
    return f"No paid buy check: no room to open even a minimum-size trade (headroom {head} {where}{tail})."
