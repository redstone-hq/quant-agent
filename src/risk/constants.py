"""Module-level risk constants shared across the pipeline + agent prompts.

Keeping these in one place avoids the failure mode where someone tightens
a threshold in one file (e.g., the force-delever trigger) but forgets the
corresponding prompt text that mentions the old number. Every code path
that cares about "is this account meaningfully on margin?" imports from
here.
"""

import math

MARGIN_DEFICIT_FLOOR_USD = 1.0
"""Minimum cash deficit (in USD) before cash-only-policy actions fire.

Below this threshold, negative cash is treated as rounding noise — fill
rounding, commission leftovers, mid-price vs fill-price micro-drift —
that clears on the next reconcile pass. Triggering a force-sell for a
$0.30 deficit would be more disruptive than the phantom margin itself.

Consumers (must stay aligned — if you edit one, verify the others):
  - `TradingPipeline._force_delever`               (hard action threshold)
  - `PortfolioManagerAgent.build_user_message`     (DE-LEVER MANDATE prompt)
  - `PositionReviewerAgent.build_user_message`     (de-lever prompt in midday/close)
"""


BREAKOUT_SETUP_TYPE = "breakout"
"""`TechAnalysisResult.setup_type` for a Type B / trend trade.

The one string this desk uses for "no overhead structure". Mirrors
`src/risk/trailing.py`, which already branches on exactly this literal
(`setup_type != "breakout"` is Type A there) — the same convention, not a
second one. Anything else, including None and an unrecognised value, is
treated as Type A / range, which is the conservative side: the reward:risk
machinery below keeps applying.
"""


def is_trend_trade(
    setup_type: str | None,
    *,
    structural_ceiling: bool | None = None,
) -> bool:
    """Is there nothing overhead that is expected to stop this trade?

    **One definition, two consumers, on purpose** (2026-09-11, docs/WORK.md
    item 1(d) plus funnel item 6). The same real-world condition decides both
    of these, and they must not be able to drift apart:

      * whether any reward:risk comparison applies at all
        (`reward_risk_floor_applies` below);
      * whether `src/data/levels.py::derive_structural_target` may use its
        ATR measured-move projection instead of refusing for want of a level.

    Two independent ways to know it, and either is sufficient:

      * **The label.** The Technical Analyst typed `setup_type="breakout"`.
        This is the desk's existing Type A/B distinction and the same literal
        `src/risk/trailing.py` already branches on.
      * **The measurement.** `structural_ceiling=False` — the desk's OWN
        level computation found no structural level in the trade's direction
        of travel, on a chart that DID yield levels elsewhere. That is a
        computed fact about this specific trade, not an assertion about it,
        and it is strictly better evidence than the label.

    `structural_ceiling=None` means "not computed at this call site", not
    "no ceiling" — so a caller that cannot measure it falls back to the label
    alone, which is the conservative side.

    Why the measurement half exists: funnel item 6 ("no structural level from
    which to derive a target") refused real trades whose charts genuinely had
    nothing overhead, purely because the analyst had not separately typed the
    word "breakout" — while the correct ATR projection for exactly that case
    already existed in the code, reachable only through the label.
    """
    if str(setup_type or "").strip().lower() == BREAKOUT_SETUP_TYPE:
        return True
    return structural_ceiling is False


def reward_risk_floor_applies(
    setup_type: str | None,
    *,
    structural_ceiling: bool | None = None,
) -> bool:
    """Does any reward:risk comparison apply to this trade at all?

    **False for a Type B / breakout trade, and that is the whole rule**
    (docs/WORK.md item 1 part (d), owner decision 2026-09-11).

    A breakout has no overhead level anyone is defending — `src/risk/
    trailing.py`'s module docstring is the desk's own statement of this, and
    the position is MANAGED accordingly: trailed from entry, with no fixed
    profit target at all. Any "reward" put in the numerator of a ratio for
    such a trade is therefore a number invented to satisfy the ratio, not a
    price the desk is trading toward. Gating on it, or sizing on it, judged
    the trade against a target its own exit management never intended to
    reach. Approval for a Type B trade rests on the RISK side alone: a real
    level-backed or ATR-derived stop, plus the multi-agent conviction and
    evidence checks that run regardless of setup type.

    True for a Type A / range trade, whose reward:risk IS real — measured
    from that specific trade's own support (risk) and resistance (reward).
    What is done with that number: ranking only — never a refusal and never
    a size cap. The 2026-10-01 parity refusal was deleted on 2026-10-09
    (owner mandate, docs/OUTCOME.md): measured that day, structural ceilings
    rejected price less often than a randomised control in both range and
    trend regimes (n~5,700), so the reward side is a forecast the desk never
    sells at. The stop and the risk caps bound the loss. An unmeasurable
    range payoff is a recorded fact / ranking hint.

    Fails to the conservative side: an unknown or missing setup type, with
    no measured ceiling fact supplied, keeps the reward:risk machinery on.

    `structural_ceiling` is the measured half — see `is_trend_trade`. A
    caller that has run the desk's own level computation for this trade can
    pass `False` when nothing sits in its direction of travel, and the
    exemption then rests on that fact rather than on the analyst's wording.
    """
    return not is_trend_trade(
        setup_type,
        structural_ceiling=structural_ceiling,
    )


STARTER_POSITION_RISK_PCT = 0.5
"""The smallest position this desk will hold, as % of equity at risk.

Not a new number: it is `RiskConfig.min_position_risk_pct`, the floor
`allocate_risk_budget` already denies requests under. Anything smaller still
consumes a book slot and needs its own stop and ongoing attention for an
immaterial payoff, and its spread/slippage cost is a large share of the whole
position (Alpaca charges no stock commission — this is not a commission
floor), so a request rationed below it is refused rather than shrunk — which
is exactly why it is also the right cap for a sub-floor catalyst trade: the
smallest size the desk can express without the idea being denied outright.

Consumers (must stay aligned — if you edit one, verify the others):
  - `RiskConfig.min_position_risk_pct`            (the budget floor)
  - `PortfolioManagerAgent.decide`                (the sub-floor cap default)
"""


def risk_budget_allocation_pct(
    *,
    entry_price: float,
    stop_price: float,
    total_value: float,
    risk_budget_pct: float,
) -> float | None:
    """How big this ONE name's OWN stop distance lets it be, as a RAW
    notional percentage of equity. `None` when the geometry cannot bound it.

    THE one definition of stop-derived size, board item 221. Three callers:
    `PortfolioConstructor._build_buy` and `._build_short`, which cap the
    PM's requested delta with it, and the PM-facing projected-portfolio
    preview (`TradingPipeline._build_projected_portfolio`), which sizes each
    candidate with it.

    The preview used to give every candidate an identical flat slice, so the
    sector mix the PM self-corrected against was a book no candidate would
    ever be given: a wide-stopped name gets far less than a flat slice and a
    tight-stopped name far more. Both directions were live — a sector the
    preview showed as crowded could be light in reality (a good name dropped
    for nothing) and one it showed as comfortable could be heavy (the
    crowding went through unflagged).

    The arithmetic below is the constructor's own, moved here verbatim and
    in the same order, so the extraction changes no traded value:

        qty_by_risk   = risk_dollars_allowed / risk_per_share
        position_$    = qty_by_risk * entry_price
        allocation_%  = position_$ / total_value * 100

    `risk_per_share` is UNSIGNED (D4: a short's stop sits ABOVE its entry).
    A short and an otherwise identical long get the SAME arithmetic: the
    short-side gap haircut was DELETED by owner ruling 2026-10-04 ("a short
    should be treated the same as a long, no different math no different
    behavior"), along with its parameter, so there is no neutral dial left
    for anyone to re-tune.
    """
    try:
        entry = float(entry_price)
        stop = float(stop_price)
        equity = float(total_value)
        budget = float(risk_budget_pct)
    except (TypeError, ValueError):
        return None
    if not (math.isfinite(entry) and math.isfinite(stop) and math.isfinite(equity) and math.isfinite(budget)):
        return None
    if equity <= 0:
        return None
    risk_per_share = abs(entry - stop)
    if risk_per_share <= 0:
        return None
    risk_dollars_allowed = equity * budget / 100
    return risk_dollars_allowed * entry / risk_per_share / equity * 100


def live_constructor_cfg_or_none(constructor):
    """The LIVE `ConstructorConfig` of `constructor`, for rules that must
    agree with the stops the desk actually places (board item 185: the
    universe screen's volatility ceiling is 1 / the widest stop this object
    can produce). `None` when no constructor has been built -- some tests
    drive a bare pipeline -- and the caller then falls back to
    `config.risk` plus the class defaults. Pure: reads one attribute.
    Moved from `TradingPipeline._constructor_cfg_or_none` (2026-10-01).
    """
    return getattr(constructor, "cfg", None)
