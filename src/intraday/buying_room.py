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
from datetime import datetime, timezone

from src.intraday.no_room_text import no_room_sentence  # noqa: F401 -- re-exported
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


def _sym(p) -> str:
    get = p.get if isinstance(p, dict) else lambda k, _p=p: getattr(_p, k, None)
    return str(get("symbol") or "").upper()


def _post_sale_book(positions, cash, sold_symbols):
    """The book and settled cash once this check's below-entry-bar sale has gone through.

    The sold holding leaves the gross; a long's mark is added to cash. The
    mark, not the live bid, can only OVERSTATE room, and overstating room
    only means the PM is called -- the behaviour before this skip existed.
    """
    sold = {str(s).upper() for s in sold_symbols or ()}
    if not sold:
        return positions, cash
    kept, freed = [], 0.0
    for p in positions or []:
        if _sym(p) in sold:
            mv = _num(p.get("market_value") if isinstance(p, dict) else getattr(p, "market_value", None))
            freed += mv if mv is not None and mv > 0 else 0.0
            continue
        kept.append(p)
    return kept, (_num(cash) or 0.0) + freed


def below_bar_sale_symbols(pipeline, pm_decide_kwargs: dict) -> tuple[str, ...]:
    """The holding this check's PM-free below-entry-bar sale will sell, read
    BEFORE the PM decision through the same deterministic pre-check
    `below_bar_sale_without_pm` uses (no model is called). Empty when
    rotation is off or nothing is below the bar. A failing pre-check RAISES:
    the caller must not judge room on the pre-sale book."""
    from src.pipeline_rotation_exec import _rotation_execution_enabled
    from src.rotation import RotationPrecheck
    from src.rotation_dispositions import _NONE_AS_EMPTY

    if not _rotation_execution_enabled(pipeline):
        return ()
    pm = getattr(pipeline, "portfolio_manager", None)
    kwargs = dict(pm_decide_kwargs or {})
    for key, empty in _NONE_AS_EMPTY.items():
        if key in kwargs and not kwargs[key]:
            kwargs[key] = empty()
    try:
        pm.last_rotation_precheck = None
        pm.build_user_message(**kwargs)
        precheck = pm.last_rotation_precheck
    finally:
        try:
            pm.last_rotation_precheck = None
        except Exception:  # noqa: BLE001
            pass
    if not isinstance(precheck, RotationPrecheck):
        return ()
    opportunity = precheck.opportunity
    if opportunity is None or opportunity.tier != "ineligible_hold" or not opportunity.held_symbol:
        return ()
    return (str(opportunity.held_symbol).upper(),)


def assess_buying_room(pipeline, ctx, analyses, mover_symbols, sold_symbols=()) -> BuyingRoom:
    """Headroom on the post-sale book and, per mover, the smallest position that clears the floor."""
    from src.pipeline_gross_ceiling import _session_gross_ceiling
    from src.pipeline_sizing import _entry_deployment_budget, _min_position_risk_pct
    from src.pipeline_stage_helpers import _macro_regime

    positions, cash = _post_sale_book(ctx.positions, ctx.deployable_cash, sold_symbols)
    headroom, ladder_backed, note = _entry_deployment_budget(pipeline, ctx, positions, ctx.total_value, cash)
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
        needed = None
        if isinstance(preview, tuple) and len(preview) == 3:
            check = min_risk_shortfall(
                position_notional=None, entry=preview[0], stop=preview[1], equity=equity, floor_pct=floor
            )
            needed = check.min_notional
        # ONE definition of room, either side, held or not: the headroom must
        # reach this mover's own minimum position. A share already held does
        # not shrink it (a held name counted as room on ANY headroom, 13 Oct
        # 18:57 VLO/META at $186), and a short gets no settled-cash exemption.
        fits = needed is not None and headroom > 0 and needed <= headroom
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
    if sold_symbols:
        note = f"{note}; after selling {', '.join(sorted(sold_symbols))} below the entry bar"
    return BuyingRoom(headroom, note, bool(ladder_backed), floor, ceiling_x, rows)


#: the seats whose fresh read can justify a sale by judgement
_RESEARCH_SEATS = ("news_analyst", "earnings_analyst")
_BUY_CLASS = {"long": ("buy", "strong_buy"), "short": ("sell", "strong_sell")}


def _stamp(text) -> datetime | None:
    if not isinstance(text, str) or not text:
        return None
    try:
        when = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return when if when.tzinfo else when.replace(tzinfo=timezone.utc)  # SQLite and broker stamps are UTC


def judgement_sale_reasons(pipeline, ctx, analyses) -> list[str]:
    """Why a paid PM call could still SELL by judgement when no new position fits; empty when nothing can.

    Selling uses the same rules as buying (owner 2026-10-13): a holding whose
    latest chart read is no longer in its own side's buy class, a news or
    earnings read since the last PM call, or an exit fill (stop or sale)
    since the last PM call that freed cash. A record that cannot be read
    counts as a reason: the call then runs, the behaviour before this skip.
    """
    from src.trade_actions import is_position_exit_action

    reasons: list[str] = []
    rating = {str(a.symbol).upper(): a.rating for a in analyses or ()}
    for p in ctx.positions or []:
        mv = _num(p.get("market_value") if isinstance(p, dict) else getattr(p, "market_value", None))
        side = "long" if mv is not None and mv > 0 else "short" if mv is not None and mv < 0 else None
        read = rating.get(_sym(p))
        if side and read is not None and read not in _BUY_CLASS[side]:
            reasons.append(f"held {_sym(p)} chart read now {read}")
    db = pipeline.db
    try:
        row = db.execute("SELECT MAX(timestamp) FROM agent_logs WHERE agent_name = 'portfolio_manager'").fetchone()
        last_pm = _stamp(row[0] if row else None)
        if last_pm is None:
            return reasons
        research = db.execute(
            "SELECT agent_name, timestamp FROM agent_logs WHERE agent_name IN (?, ?) ORDER BY id DESC LIMIT 20",
            _RESEARCH_SEATS,
        ).fetchall()
        fills = db.execute(
            "SELECT symbol, action, COALESCE(filled_at, timestamp) FROM current_account_trades "
            "WHERE fill_status = 'filled' ORDER BY id DESC LIMIT 50"
        ).fetchall()
    except Exception as exc:  # noqa: BLE001 -- unreadable: call the PM as before
        logger.warning("buying room: judgement-sale record unreadable (%s); the PM call runs", exc)
        return [*reasons, f"judgement-sale record unreadable: {exc}"]
    reasons += [f"{seat} read at {when}" for seat, when in research if (_stamp(when) or last_pm) > last_pm]
    reasons += [
        f"{action} {symbol} filled at {when}"
        for symbol, action, when in fills
        if is_position_exit_action(action) and (_stamp(when) or last_pm) > last_pm
    ]
    return reasons
