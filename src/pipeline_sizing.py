"""Share sizing, the risk budget, and the execution-time deployment budget.

Step 11 of `docs/PIPELINE_SPLIT_PLAN.md` (board item 210). Moved VERBATIM
out of `src/pipeline_stages.py`: fractional eligibility, share rounding,
the risk-budget ceiling, the minimum order, the payoff skip reason, the
§11.2 deployment budget and the single-name execution cap. Money-governing
code, so it moves without one character of its bodies changing; every name
is re-exported from `src.pipeline_stages` so each original import path and
each test patch target is unchanged.

`_session_gross_ceiling` lives in `src/pipeline_gross_ceiling.py` (the morning
prompt and the risk stage read it too, via `src.pipeline_stages`).

This module must not import `src.pipeline`.
"""

from __future__ import annotations

import logging
import math

#: The moved code logged under `src.pipeline_stages` before the move and
#: still does; binding the name rather than `__name__` keeps log records
#: byte-identical.
from src.pipeline_gross_ceiling import _session_gross_ceiling
from src.sentinel.guarded import record_guarded_pass

logger = logging.getLogger("src.pipeline_stages")


def _fractional_sizing_allowed(pipeline, symbol: str, *, is_short: bool) -> bool:
    """Spec §11.1 — may THIS symbol be sized in fractional shares right now?

    Two independent gates, both of which must say yes:

    1. `execution.fractional_enabled` (default True). The owner's switch, so
       the feature can be turned off without a code change.
    2. The BROKER confirms `fractionable` for the symbol. A config flag says
       what the desk wants; only the asset directory says what Alpaca will
       accept. An unknown or failed lookup is a NO — fail closed, never
       fractional-by-assumption.

    A SHORT is always whole-share regardless: a fractional share cannot be
    borrowed, so this is not a policy choice to expose.

    Any unexpected failure in here returns False. The fallback (whole shares)
    is the behaviour that shipped for months; there is no failure mode of
    this function that should be allowed to stop a trade.
    """
    if is_short:
        return False
    try:
        execution_cfg = getattr(pipeline.config, "execution", None)
        if not bool(getattr(execution_cfg, "fractional_enabled", False)):
            return False
        info = pipeline.broker.get_fractionability(symbol)
        record_guarded_pass(pipeline, "sizing.fractional_allowed")
    except Exception as exc:  # noqa: BLE001
        record_guarded_pass(
            pipeline, "sizing.fractional_allowed", exc, log=logger, context={"effect": "whole shares (fail closed)"}
        )
        return False
    if not isinstance(info, dict) or not info.get("fractionable"):
        reason = info.get("reason", "unknown") if isinstance(info, dict) else "unknown"
        logger.info(
            "fractional sizing NOT available for %s (%s) — whole shares",
            symbol,
            reason,
        )
        return False
    return True


def _size_shares(pipeline, raw_qty: float, *, fractional: bool) -> float:
    """Turn a raw, real-valued share count into an ORDERABLE quantity.

    Whole-share mode floors to an integer — the behaviour this desk has
    always had, and the silent constant tax §11.1 exists to remove (a request
    for 6% of the book delivered 3.84%).

    Fractional mode floors to `execution.fractional_share_decimals` places.
    FLOORS, never rounds: rounding up would spend a sliver more risk budget
    than the sizing math actually allowed, and a sizing rule that can exceed
    its own budget by any amount is not a budget. The residual left on the
    table is under a tenth of a cent of notional.
    """
    try:
        value = float(raw_qty)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(value) or value <= 0:
        return 0.0
    if not fractional:
        return float(int(value))
    try:
        decimals = int(
            getattr(
                getattr(pipeline.config, "execution", None),
                "fractional_share_decimals",
                4,
            )
        )
    except (TypeError, ValueError):
        decimals = 4
    decimals = min(max(decimals, 1), 9)
    scale = 10**decimals
    return math.floor(value * scale) / scale


from src.sizing_fmt_shares import _fmt_shares  # noqa: E402,F401

# Spec §11.1 vol-adjusted sizing budget: the fraction of EQUITY a single
# entry may put at risk between its fill and its stop.
#
# HISTORICAL NOTE (item 22, 2026-09-03 audit): this used to be a hardcoded
# module constant, `RISK_BUDGET_PCT = 0.5`, predating the owner-ratified 5%
# envelope (`config.risk.max_position_risk_pct`, decided 2026-08-27). Nothing
# connected the two, so this independent recheck silently re-capped almost
# every entry at ten times less risk than the constructor had already sized
# it to under the real rule — confirmed against real NVDA/ORCL/RSG rows
# risking ~$49 on a ~$9.85k book where ~$490 was ratified. Fixed by reading
# the same config the constructor reads, the same defensive way
# `TradingPipeline.__init__`'s `_risk_setting` reads it for
# `ConstructorConfig.risk_budget_pct` — see `docs/INCIDENT_HISTORY.md`, "the
# risk manager and order-construction audit". The 5.0 fallback here is the
# ratified default, not an invented one.
_DEFAULT_RISK_BUDGET_PCT = 5.0


def _risk_budget_pct(pipeline) -> float:
    """The configured §11.1 risk-budget percentage, or the ratified default.

    Same Mock-safety posture as `TradingPipeline.__init__`'s `_risk_setting`:
    a MagicMock config (common in tests) auto-creates a child attribute that
    is neither the default nor a real number, so it must be checked rather
    than trusted from a bare `getattr`.
    """
    raw = getattr(
        getattr(pipeline.config, "risk", None),
        "max_position_risk_pct",
        None,
    )
    if isinstance(raw, bool) or not isinstance(raw, (int, float)) or raw <= 0:
        return _DEFAULT_RISK_BUDGET_PCT
    return float(raw)


def _qty_by_risk_budget(
    pipeline, *, total_value: float, sizing_price: float, stop_price: float, is_short: bool, fractional: bool
) -> float | None:
    """Shares the §11.1 risk budget allows, or None when geometry is unusable.

    ONE definition, two callers — the BUY-submit loop (which sizes the real
    order) and the cash-sweep preflight (which sizes the funding sale). They
    were separate before: the preflight funded the ALLOCATION notional while
    the submit loop spent `min(alloc, risk)`, so on every session where the
    risk budget bound — the ordinary case — the sweep liquidated more of the
    vehicle than the BUYs could possibly spend and the bookend re-parked the
    difference minutes later. Production, 2026-08-27: SWEEP_SELL $3,422.61 at
    13:35:43, SWEEP_BUY $1,007.60 at 13:36:36. Two crossings of the spread,
    53 seconds apart, for nothing.

    The preflight passes the RM-approved stop; the submit loop may later
    ATR-WIDEN that stop, which only increases risk-per-share and therefore
    only shrinks the final quantity. So the preflight's answer is an upper
    bound on what will be spent — funding still errs long, never short.

    This is a genuinely independent recheck, not a rubber stamp of the
    constructor's own number — it recomputes risk dollars from the REAL
    executed stop/entry geometry (which can differ from what the constructor
    assumed, e.g. after an ATR-widened stop or a marketable-limit price move)
    against the ratified percentage, in Python, rather than trusting the
    constructor's or PM's claimed ratio. Keep the mechanism; only the stale
    percentage was wrong (item 22).
    """
    if not (stop_price > 0 and sizing_price > 0):
        return None
    # D4: geometry validity is direction-aware — a long's stop must sit
    # below its entry, a short's strictly above.
    valid_geometry = (not is_short and sizing_price > stop_price) or (is_short and stop_price > sizing_price)
    if not valid_geometry:
        return None
    # D4: unsigned everywhere.
    risk_per_share = abs(sizing_price - stop_price)
    # Owner ruling 2026-10-04: no short-side haircut. `is_short` above is
    # used only to validate stop geometry; the risk arithmetic that follows
    # is identical for both directions.
    if risk_per_share <= 0:
        return None
    risk_dollars = total_value * _risk_budget_pct(pipeline) / 100
    return _size_shares(
        pipeline,
        risk_dollars / risk_per_share,
        fractional=fractional,
    )


def _min_position_risk_pct(pipeline) -> float:
    """`risk.min_position_risk_pct`: the owner's minimum risk per position
    (owner rule 2026-08-27, 0.5% of equity) -- below it the desk does not
    trade. Same Mock-safety posture as `_risk_budget_pct`; an unreadable
    value falls back to the ratified `STARTER_POSITION_RISK_PCT`, never to
    zero, so a broken config cannot silently switch the floor off.

    Replaced the flat $500 `cash_sweep.min_order_usd` notional floor
    (deleted): an arbitrary number with no broker minimum behind it (Alpaca
    takes fractional orders from $1, no commission).
    """
    from src.risk.constants import STARTER_POSITION_RISK_PCT

    raw = getattr(
        getattr(getattr(pipeline, "config", None), "risk", None),
        "min_position_risk_pct",
        None,
    )
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return float(STARTER_POSITION_RISK_PCT)
    value = float(raw)
    if not math.isfinite(value) or value < 0:
        return float(STARTER_POSITION_RISK_PCT)
    return value


def _execution_payoff_skip_reason(
    decision,
    *,
    sizing_price: float,
    stop_price: float,
    geometry_changed: bool,
    is_short: bool,
) -> str | None:
    """Never skip on reward:risk — computed, thin, or unmeasurable.

    Owner 2026-09-17: invented R/R gates are a defect. Overnight bind:
    honesty about a payoff without a number is a recorded fact / ranking
    hint, not a refuse. Historical runs wrote `geometry_rr` (1.2 belt)
    and this helper briefly wrote `geometry_unmeasurable`; neither token
    is emitted. Arguments are accepted so callers and tests keep the
    same signature; none of them decide admission.
    """
    _ = (decision, sizing_price, stop_price, geometry_changed, is_short)
    return None


# --- Spec §11.2 — the EXECUTION-time deployment budget --------------------
#
# THE DEFECT THIS REPLACES (2026-09-02, the morning margin was switched on).
# The BUY submit loop clamped every entry against `available_cash`, seeded
# from the broker's RAW CASH figure, and that clamp was gated on NOTHING.
# Raw cash is at most `equity - gross`, so the arithmetic held gross below
# 1.0x equity STRUCTURALLY: however high `risk.max_gross_exposure_x` was
# set, a long could never cost more than settled money, and the §11.2
# ceiling could never become the binding constraint on the long side.
# `allow_margin: true` shipped that morning and changed nothing a long
# could do. Shorts were exempt (D11) and so were unaffected either way.
#
# THE CLAMP IS NOT REMOVED, and deleting it was considered and rejected.
# With margin enabled the broker ACCEPTS a buy that exceeds cash, so this
# loop is the last quantitative bound before the order leaves the building;
# with no clamp a batch of entries is bounded by nothing this side of
# Alpaca's own 4x. What changes is WHICH number is clamped against: the
# ladder-resolved gross headroom, so the de-levering ladder is the one
# number that governs how much the desk deploys.
#
# Fail-closed in all three degraded directions, because this gate is last:
#   - ladder unreadable  -> raw settled cash, i.e. exactly the pre-margin
#     behaviour. NEVER the standing 2.0x cap. The constructor may fall back
#     to the standing cap because another gate still runs after it; nothing
#     runs after this one.
#   - equity unusable    -> zero budget. `_resolve_gross_ceiling` already
#     forces the ladder's FLOOR rung on a non-finite equity read (guard 2,
#     2026-09-02) and alerts the owner; multiplying that rung by a NaN
#     equity would produce a NaN budget, every `>` comparison against it
#     would be False, and the clamp would silently grant INFINITE room on
#     precisely the broken-snapshot morning the guard exists for.
#   - park symbol unreadable -> parked cash counts as gross, which shrinks
#     the headroom rather than inflating it.
def _entry_deployment_budget(pipeline, ctx, positions, equity, cash):
    """Dollars of NEW entry notional this session may still add.

    Returns `(budget_usd, ladder_backed, note)`. `ladder_backed` says which
    of the two meanings the number carries, and the submit loop needs it:
    a ladder budget is GROSS headroom, which a short consumes as surely as
    a long does, while the cash fallback is a settled-cash pool, which a
    short does not draw on at all (D11).
    """
    from src.risk.rules import gross_exposure

    ceiling = _session_gross_ceiling(pipeline, ctx)
    if ceiling is None:
        logger.warning(
            "§11.2: the gross-exposure ceiling could not be resolved for the "
            "submit loop — falling back to the pre-margin raw-cash clamp "
            "($%.2f). Entries are bounded by settled cash this session, not "
            "by the ladder.",
            float(cash) if isinstance(cash, (int, float)) else 0.0,
        )
        usable_cash = (
            float(cash)
            if isinstance(cash, (int, float)) and not isinstance(cash, bool) and math.isfinite(float(cash))
            else 0.0
        )
        return max(0.0, usable_cash), False, "raw settled cash (ladder unreadable)"

    if (
        isinstance(equity, bool)
        or not isinstance(equity, (int, float))
        or not math.isfinite(float(equity))
        or float(equity) <= 0
    ):
        logger.warning(
            "§11.2: equity read is unusable (%r) — refusing every new entry "
            "this session rather than sizing a budget against it. The ladder "
            "is already at its floor rung (%.1fx) for the same reason.",
            equity,
            ceiling.ceiling_x,
        )
        return 0.0, True, "no usable equity read — no new entry permitted"

    equity = float(equity)
    # The park vehicle is parked cash, not exposure — the same exclusion
    # `gross_exposure` is given everywhere else it is called. An unreadable
    # symbol falls through to None, which counts the vehicle as gross and so
    # UNDER-states the headroom; that is the safe side to be wrong on.
    try:
        park = pipeline._sweep_symbol()
    except Exception as e:  # noqa: BLE001
        logger.warning("§11.2: cash-park symbol unreadable (%s) — counting parked cash as gross for the budget", e)
        park = None
    if not isinstance(park, str):
        park = None
    # No non-finite guard on the total: `gross_exposure` SKIPS a non-finite
    # `market_value` rather than propagating it, so this sum cannot come back
    # NaN. `unmeasurable_gross_symbols` is the guard against acting on a
    # total that quietly excluded a position, and it is the pre-trade gate's
    # job — a BUY carrying one is already hard-blocked before this loop.
    held_gross = gross_exposure(positions, cash_park_symbol=park)

    # Floored at zero. A book already ABOVE its rung has negative headroom,
    # and a negative budget is not a smaller budget: it would render to the
    # operator as "-$500 still deployable" and would silently eat the first
    # $500 of any credit a later refresh brought in.
    headroom = max(0.0, ceiling.ceiling_x * equity - held_gross)
    note = (
        f"§11.2 ladder headroom ${headroom:,.2f} "
        f"({ceiling.ceiling_x:.2f}x x ${equity:,.0f} equity "
        f"- ${held_gross:,.0f} held gross, rung {ceiling.rung})"
    )

    # `is True`, not `bool(...)`: a MagicMock config attribute is truthy, and
    # reading a stub as "margin enabled" would hand a test pipeline a levered
    # budget it was never meant to have. Only a real `True` unbinds cash.
    # RiskConfig.allow_margin is pydantic-typed `bool`, so production is
    # unaffected by the stricter read.
    allow_margin = (
        getattr(
            getattr(getattr(pipeline, "config", None), "risk", None),
            "allow_margin",
            False,
        )
        is True
    )
    if not allow_margin:
        usable_cash = (
            float(cash)
            if isinstance(cash, (int, float)) and not isinstance(cash, bool) and math.isfinite(float(cash))
            else 0.0
        )
        usable_cash = max(0.0, usable_cash)
        if usable_cash < headroom:
            note = (
                f"raw settled cash ${usable_cash:,.2f} (margin disabled; "
                f"tighter than the {ceiling.ceiling_x:.2f}x ladder headroom "
                f"${headroom:,.2f})"
            )
        headroom = min(headroom, usable_cash)
    return headroom, True, note


def _single_name_execution_cap(pipeline, equity: float) -> float:
    """`max_position_pct` of equity, re-applied to the size EXECUTION chose.

    Same idiom as the §10.3 minimum-notional floor a few lines below the
    clamp: the gate upstream already caps a single name, and execution only
    ever shrinks what the gate approved, so in the ordinary lane this is
    redundant. It is here because the budget above is a POOL — one order
    could otherwise draw the entire session's ladder headroom — and because
    the resume lane reaches this loop without the pre-trade gate having run.
    Redundant and local beats correct-only-if-another-file-ran.

    Falls back to the configured default (20) rather than to "no cap" when
    the setting is unreadable, and to zero on an unusable equity figure —
    the same fail-closed direction as the budget.
    """
    if (
        isinstance(equity, bool)
        or not isinstance(equity, (int, float))
        or not math.isfinite(float(equity))
        or float(equity) <= 0
    ):
        return 0.0
    raw = getattr(
        getattr(getattr(pipeline, "config", None), "risk", None),
        "max_position_pct",
        None,
    )
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        pct = 20.0
    else:
        pct = float(raw)
        if not math.isfinite(pct) or pct <= 0:
            pct = 20.0
    return float(equity) * pct / 100.0
