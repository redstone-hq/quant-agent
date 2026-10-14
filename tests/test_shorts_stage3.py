"""Stage 3 of short selling — shorts can be OPENED and COVERED.

Stage 1 (`tests/test_shorts_countable.py`) made a held short COUNTABLE.
Stage 2 (`tests/test_shorts_safe.py`) made its protective-stop geometry
correct. This stage lifts the last gate: the order path itself can now open
and cover a short, subject to three things that gate it — a borrow check
(D6), two exposure caps (D9), and a mandatory protective stop that escalates
to an immediate market cover on failure (D7) — and none of them is a master
switch (D11); together they ARE the control surface.

Surfaces covered: ``src/portfolio_constructor.py`` (signed targets, sign-
crossing refusal, direction-aware stop geometry/widening, the sizing
haircut, ``_build_short``/``_build_cover``), ``src/risk/rules.py`` (the two
short exposure caps, the cover-cash exemption), and
``src/pipeline_stages.py``'s ``ExecutionStage`` (the borrow gate, side-aware
order submission, and the protection-failure escalation).
"""

from tests.fakes.held_book import hold
from unittest.mock import MagicMock

import pytest

from src.config import RiskConfig
from src.models import (
    Position,
    PortfolioDecision,
    PositionAction,
    ReasoningChain,
    TargetPosition,
    TechAnalysisResult,
    TechReasoningChain,
    TradeDecision,
)
from src.pipeline import TradingPipeline
from src.pipeline_context import RunContext
from src.pipeline_stages import ExecutionStage
from src.portfolio_constructor import PortfolioConstructor
from src.risk.rules import RiskRuleEngine
from tests.pipeline_factory import build_pipeline


# ==========================================================================
# Shared fixtures
# ==========================================================================


def _pos(symbol: str, qty: float, entry: float, price: float, sector: str = "Technology") -> Position:
    return Position(
        symbol=symbol,
        qty=qty,
        avg_entry=entry,
        current_price=price,
        market_value=qty * price,
        unrealized_pnl=qty * (price - entry),
        sector=sector,
    )


def _tech_rc() -> TechReasoningChain:
    return TechReasoningChain(
        trend="x",
        momentum="x",
        volatility="x",
        volume="x",
        support_resistance="x",
    )


def _pm_rc() -> ReasoningChain:
    return ReasoningChain(
        macro_filter="x",
        news_check="x",
        earnings_check="x",
        signal_conflicts="x",
        sizing_logic="x",
        portfolio_balance="x",
        cash_target="x",
    )


# `atr_14` and `computed_levels` are set in Python by TechAnalystAgent, never
# emitted by the model. Since 2026-09-01 the constructor derives the
# take-profit from `computed_levels` and refuses without them, so a fixture
# missing them is not a state the pipeline can reach. The default ATR sits
# just inside the noise band, leaving the structural stop alone: these are
# short-plumbing tests, not stop-widening tests.
def _long_analysis(symbol="NVDA", entry=250.0, stop=237.5, target=300.0, atr_14=None, horizon=60) -> TechAnalysisResult:
    return TechAnalysisResult(
        symbol=symbol,
        rating="buy",
        entry_price=entry,
        stop_loss=stop,
        reference_target=target,
        reasoning="test",
        support_levels=[stop],
        resistance_levels=[target],
        computed_levels=[stop, target],
        computed_level_touches={stop: 5, target: 5},
        computed_level_bars={stop: [(stop, stop)], target: [(target, target)]},
        setup_type="range",
        expected_horizon_sessions=horizon,
        reasoning_chain=_tech_rc(),
        atr_14=abs(entry - stop) / 3.5 if atr_14 is None else atr_14,
        thesis_invalid_if="closes below support",
    )


def _short_analysis(
    symbol="TSLA",
    entry=250.0,
    stop=262.5,
    target=200.0,
    atr_14=None,
    horizon=60,
    computed=None,
    touches=None,
    bars=None,
) -> TechAnalysisResult:
    """`computed_levels` deliberately carries the TARGET and not the stop.

    Since spec §12.1 that field also decides whether the ATR noise band
    applies: a stop sitting at a level the system COMPUTED is honoured
    rather than widened. Listing the stop would quietly convert the
    widening tests below into level-backed tests. Here the stop is the
    analyst's own number with nothing computed under it — the case the band
    exists for. `computed` is available for tests that want the other case.

    `touches` mirrors `_vol_analysis` in `test_risk_based_sizing.py`: data
    only, defaulting to 5 per price. No touch count gates a stop since
    2026-10-14.
    """
    levels = list([target] if computed is None else computed)
    # 2026-09-12, "no floor, no trade" — for a short the floor is OVERHEAD.
    # The constructor refuses a short with no computed level above entry,
    # so a DISTANT ceiling at 1.5x entry is added when none is listed: far
    # outside any level's own match zone here, so the stop
    # stays unbacked and the widening tests still test widening.
    if not any(float(p) > float(entry) for p in levels):
        levels.append(round(float(entry) * 1.5, 2))
    default_touches = {price: 5 for price in levels}
    # Item 215: each level states the narrowest bar that can have drawn it —
    # its own price — so listing the stop in `computed` still means "the stop
    # is ON this level" and omitting it still means "nothing is under it".
    default_bars = {price: [(price, price)] for price in levels}
    if bars:
        default_bars.update(bars)
    return TechAnalysisResult(
        symbol=symbol,
        rating="sell",
        entry_price=entry,
        stop_loss=stop,
        reference_target=target,
        reasoning="test",
        support_levels=[target],
        resistance_levels=[stop],
        computed_levels=levels,
        computed_level_touches=default_touches if touches is None else touches,
        computed_level_bars=default_bars,
        setup_type="range",
        expected_horizon_sessions=horizon,
        reasoning_chain=_tech_rc(),
        atr_14=abs(entry - stop) / 3.5 if atr_14 is None else atr_14,
        thesis_invalid_if="closes below support",
    )


def _short_target(symbol="TSLA", weight=15.0, suggested_stop=None) -> TargetPosition:
    return TargetPosition(
        symbol=symbol,
        direction="short",
        target_weight_pct=weight,
        conviction="high",
        thesis="overvalued",
        suggested_stop_price=suggested_stop,
    )


def _cfg(**kw) -> RiskConfig:
    base = dict(
        max_position_pct=20.0,
        max_total_position_pct=90.0,
        require_stop_loss=True,
        allow_margin=False,
    )
    base.update(kw)
    return RiskConfig(**base)


def _exec_pipeline() -> MagicMock:
    """A MagicMock pipeline wired with the benign defaults every
    ExecutionStage test needs — same convention as
    tests/test_pipeline_stages.py's inline setup, collected here since
    every test below needs the same baseline."""
    pipeline = MagicMock()
    pipeline._format_qty = lambda q: str(q)
    pipeline._order_accepted.return_value = True
    pipeline._full_sell_qty = TradingPipeline._full_sell_qty
    pipeline._refresh_account_state.return_value = (
        {"cash": 50_000.0, "portfolio_value": 100_000.0},
        [],
        {},
    )
    pipeline.broker.wait_for_order_terminal.return_value = "filled"
    pipeline.broker.get_shortability.return_value = {
        "shortable": True,
        "easy_to_borrow": True,
        "reason": "eligible",
    }
    return pipeline


def _ctx(decisions, positions=None, cash=50_000.0, total_value=100_000.0) -> RunContext:
    ctx = RunContext.start("morning")
    ctx.cash = cash
    ctx.total_value = total_value
    ctx.last_equity = total_value
    ctx.positions = positions or []
    ctx.portfolio_decision = PortfolioDecision(
        reasoning_chain=_pm_rc(),
        decisions=decisions,
        portfolio_view="test",
    )
    ctx.symbols_bars = {}
    return ctx


# ==========================================================================
# 1. Opening a short end to end (constructor + execution)
# ==========================================================================


def test_open_short_end_to_end_submits_sell_short_and_places_buy_stop_above_entry():
    """The headline Stage 3 property. The constructor builds a SHORT
    decision; ExecutionStage submits it with side='sell_short' and places
    the mandatory protective stop with side='sell_short' too (so
    `place_entry_protection` mirrors it to a BUY stop ABOVE entry)."""
    constructor = PortfolioConstructor()
    decisions = constructor.construct_orders(
        targets=[_short_target()],
        positions=[],
        analyses=[_short_analysis()],
        total_value=100_000,
        price_map={"TSLA": 250.0},
    )
    assert len(decisions) == 1
    decision = decisions[0]
    assert decision.action == "SHORT"
    assert decision.stop_loss > decision.entry_price

    pipeline = _exec_pipeline()
    pipeline.broker.get_latest_price.return_value = 250.0
    pipeline.broker.get_latest_quote.return_value = {"bid_price": 250.0, "ask_price": 250.0}
    pipeline.broker.submit_order.return_value = {
        "id": "short-order-1",
        "status": "accepted",
        "symbol": "TSLA",
        "side": "sell_short",
        "pending_stop_price": decision.stop_loss,
    }
    pipeline.broker.get_order_fill_info.return_value = {"filled_qty": 20.0}
    pipeline.broker.place_entry_protection.return_value = {"id": "stop-1"}

    ctx = _ctx([decision])
    orders = ExecutionStage(pipeline=pipeline).run(ctx)

    assert len(orders) == 1
    submit_kwargs = pipeline.broker.submit_order.call_args.kwargs
    assert submit_kwargs["side"] == "sell_short"
    assert submit_kwargs["stop_loss_price"] == decision.stop_loss

    pipeline.broker.place_entry_protection.assert_called_once()
    protect_kwargs = pipeline.broker.place_entry_protection.call_args.kwargs
    assert protect_kwargs["side"] == "sell_short"
    assert protect_kwargs["stop_price"] == decision.stop_loss
    assert protect_kwargs["stop_price"] > decision.entry_price, "the protective stop for a short must sit ABOVE entry"


# ==========================================================================
# 2. Covering a short — partial and full
# ==========================================================================


def test_cover_short_partial_is_refused_whole_exits_only():
    position = _pos("TSLA", qty=-40, entry=250, price=240)
    decision = TradeDecision(
        action="COVER",
        symbol="TSLA",
        allocation_pct=50.0,
        entry_price=0.0,
        stop_loss=0.0,
        take_profit=0.0,
        reasoning="trim the short",
    )
    pipeline = _exec_pipeline()
    pipeline._refresh_account_state.return_value = (
        {"cash": 50_000.0, "portfolio_value": 100_000.0},
        [position],
        {},
    )
    pipeline._submit_protected_sell.return_value = (
        {"id": "cover-1", "status": "accepted", "symbol": "TSLA", "side": "buy"},
        {
            "order_id": "cover-1",
            "symbol": "TSLA",
            "position_qty_before_sell": 40.0,
            "specs": [],
            "wal_row_id": None,
            "side": "buy",
        },
    )

    ctx = _ctx([decision], positions=[position])
    orders = ExecutionStage(pipeline=pipeline).run(ctx)

    # Owner ruling 2026-10-09: no partial covers — refused, nothing submitted.
    assert orders == []
    pipeline._submit_protected_sell.assert_not_called()


def test_cover_short_full_buys_back_everything():
    position = _pos("TSLA", qty=-40, entry=250, price=240)
    decision = TradeDecision(
        action="COVER",
        symbol="TSLA",
        allocation_pct=100.0,
        entry_price=0.0,
        stop_loss=0.0,
        take_profit=0.0,
        reasoning="close the short",
    )
    pipeline = _exec_pipeline()
    pipeline._refresh_account_state.return_value = (
        {"cash": 50_000.0, "portfolio_value": 100_000.0},
        [position],
        {},
    )
    pipeline._submit_protected_sell.return_value = (
        {"id": "cover-2", "status": "accepted", "symbol": "TSLA", "side": "buy"},
        {
            "order_id": "cover-2",
            "symbol": "TSLA",
            "position_qty_before_sell": 40.0,
            "specs": [],
            "wal_row_id": None,
            "side": "buy",
        },
    )

    ctx = _ctx([decision], positions=[position])
    orders = ExecutionStage(pipeline=pipeline).run(ctx)

    assert len(orders) == 1
    call_kwargs = pipeline._submit_protected_sell.call_args.kwargs
    assert call_kwargs["side"] == "buy"
    assert call_kwargs["qty"] == 40.0
    assert call_kwargs["label"] == "COVER"
    # No protective stop is ever placed for a COVER — it reduces risk, it
    # doesn't open any.
    pipeline.broker.place_entry_protection.assert_not_called()


# ==========================================================================
# 3. D3 — sign-crossing is refused; only the closing leg is emitted
# ==========================================================================


def test_long_to_short_target_emits_only_the_flattening_sell():
    """A held LONG with a SHORT target must not flip in one order. The
    constructor emits ONLY a full-close SELL this session."""
    constructor = PortfolioConstructor()
    decisions = constructor.construct_orders(
        targets=[
            TargetPosition(
                symbol="NVDA", direction="short", target_weight_pct=5.0, conviction="high", thesis="reversal"
            )
        ],
        positions=[_pos("NVDA", qty=150, entry=100, price=100)],  # +15% long
        analyses=[_short_analysis(symbol="NVDA")],
        total_value=100_000,
        price_map={"NVDA": 100.0},
    )
    assert len(decisions) == 1
    assert decisions[0].action == "SELL"
    assert decisions[0].allocation_pct == 100.0


def test_short_to_long_target_emits_only_the_flattening_cover():
    """The mirror: a held SHORT with a LONG target emits ONLY a full COVER."""
    constructor = PortfolioConstructor()
    decisions = constructor.construct_orders(
        targets=[
            TargetPosition(symbol="TSLA", direction="long", target_weight_pct=5.0, conviction="high", thesis="reversal")
        ],
        positions=[_pos("TSLA", qty=-40, entry=250, price=250)],  # -10% short
        analyses=[_long_analysis(symbol="TSLA")],
        total_value=100_000,
        price_map={"TSLA": 250.0},
    )
    assert len(decisions) == 1
    assert decisions[0].action == "COVER"
    assert decisions[0].allocation_pct == 100.0


# ==========================================================================
# 4. D4 / D5 — direction-aware stop geometry and widening
# ==========================================================================


def test_short_stop_at_or_below_entry_is_rejected():
    """D4: a short's stop must sit strictly ABOVE entry. A PM-suggested
    stop below entry is refused, not silently accepted."""
    constructor = PortfolioConstructor()
    decisions = constructor.construct_orders(
        targets=[_short_target(suggested_stop=240.0)],  # BELOW entry $250
        positions=[],
        analyses=[_short_analysis()],
        total_value=100_000,
        price_map={"TSLA": 250.0},
    )
    assert decisions == []


def test_long_stop_breached_by_live_price_since_analysis_is_rejected():
    """WORK.md item 8 ("stop on wrong side of entry" — 2 of 68 funnel
    refusals, DEFECT, upstream, unlocated). Traced backward from
    `STOP_REFUSAL_WRONG_SIDE` through `_resolve_stop` -> `_resolve_stop`'s
    two sources (`target.suggested_stop_price`, `analysis.stop_loss`) ->
    `TechAnalysisResult`'s own validator (`src/models.py`), which already
    guarantees a BUY's `stop_loss` sits below ITS OWN `entry_price` at
    ingestion — so the analyst never emits a self-contradictory pair.

    Measured against a real production database snapshot
    (sandbox copy of live `quant_agent.db`, 2026-08 window): the live quote
    `PortfolioConstructor` prices a NEW target off (`price_map`, filled by
    `pipeline_stages.py` from a fresh broker call AFTER macro/news/earnings/
    portfolio_manager have all already run) lands 11-108 seconds and up to
    8.3% away from the price the technical analyst's `entry_price`/
    `stop_loss` pair was computed against — real, measured drift, not a
    hypothetical. One production case (DIS) drifted -1.8% in 108s against a
    stop set only 2.2% from the analyst's entry, landing $0.48 from
    flipping outright.

    This is the mechanism: nothing is broken on either side (the analyst's
    numbers are self-consistent; the live quote is a real, fresh price) —
    the analyst's structural stop is simply a real price LEVEL, and by the
    time the constructor prices the trade off a live quote fetched later in
    the same run, ordinary price movement can have already carried the
    market through that level. Buying (or shorting) through an already-
    breached level is not a data-quality bug to fix upstream, and there is
    no sign-flip, unit-conversion or rounding defect anywhere in the traced
    chain (`_resolve_stop` -> `_widen_stop_past_noise` ->
    `_resolve_entry_and_stop`'s side check) — the refusal below IS the
    correct, working backstop for exactly this case, and this test locks
    in that the live-quote path (not just the synthetic
    `suggested_stop_price` path `test_short_stop_at_or_below_entry_is_
    rejected` above covers) refuses cleanly rather than shipping a stop
    that can no longer protect anything.
    """
    constructor = PortfolioConstructor()
    analysis = _long_analysis(symbol="NVDA", entry=250.0, stop=237.5, target=300.0)
    target = TargetPosition(
        symbol="NVDA",
        direction="long",
        target_weight_pct=5.0,
        conviction="high",
        thesis="breakout",
    )
    # Live quote fetched at construction time has already fallen THROUGH
    # the analyst's stop (237.5) — the level the setup depended on is gone.
    decisions = constructor.construct_orders(
        targets=[target],
        positions=[],
        analyses=[analysis],
        total_value=100_000,
        price_map={"NVDA": 230.0},
    )
    assert decisions == []


def test_short_stop_inside_noise_band_is_widened_upward():
    """D5: a short's stop inside `min_stop_atr_multiple` ATRs of entry is
    pushed UP (away from entry) — the mirror of a long's stop being pushed
    DOWN. Since 2026-10-09 no setup or regime scaling applies to the 2.5x
    base (owner ruling 2026-10-04) -> 2.5 x ATR(5.0) = 12.5 ->
    band edge = entry 250 + 12.5 = 262.5. The R:R at that wider stop must
    still clear the floor: reward 50.00 / risk 12.5 = 4.00.

    Band history on this one fixture: 3.45 x ATR = 17.25 -> $267.25 until
    2026-09-04; 1.35 x ATR = 6.75 -> $256.75 until 2026-09-10; 2.5 x ATR =
    12.5 -> $262.5 since, when the base went 1.5 -> 2.5 on published
    swing-trading doctrine."""
    constructor = PortfolioConstructor()
    widened = constructor._widen_stop_past_noise(
        "TSLA",
        _short_analysis(entry=250.0, stop=252.0, target=200.0, atr_14=5.0),
        entry_price=250.0,
        stop_loss=252.0,
        direction="short",
    )
    assert widened == 262.5
    assert widened > 252.0
    assert widened > 250.0


def test_short_widened_stop_below_the_reward_risk_floor_is_no_longer_rejected():
    """**Inverted 2026-09-11, docs/WORK.md item 1(d).** D5's mirroring is
    unchanged and is still what this test protects: widening a short's stop
    pushes it UP, away from entry, to the noise-band edge — 262.5 (entry
    250 + 2.5 x 5 ATR = 12.5 of risk). Against a 241 target that is reward
    9.00 / risk 12.5 = R:R 0.72.

    That 0.72 used to reject the trade. It does not any more: for a range
    setup the real ratio is a ranking signal, never a refusal or a
    size-cap, and it is mirrored for a short exactly as it is for a long. The
    STOP the function returns is the assertion now."""
    constructor = PortfolioConstructor()
    widened = constructor._widen_stop_past_noise(
        "TSLA",
        _short_analysis(entry=250.0, stop=252.0, target=241.0, atr_14=5.0),
        entry_price=250.0,
        stop_loss=252.0,
        direction="short",
    )
    assert widened == 262.5


# ==========================================================================
# 4b. §12.1 — a short's stop at a VERIFIED level is honoured, however tight
# ==========================================================================
#
# Fully mirrored from the long side (see the matching block in
# tests/test_risk_based_sizing.py). Nothing about §12.1 is long-only: the
# comparison operators and the direction of the floor are the whole of the
# difference. A long is held up by structure at or BELOW its entry; a short
# is capped by structure at or ABOVE its entry.
#
# GEOMETRY REWORKED 2026-09-10, when the base floor went 1.5 -> 2.5. (It was
# reworked once before, on 2026-09-04, when the base went 3.0 -> 1.5 and the
# range scaler 1.15 -> 0.90.) Every number recomputed by hand, mirroring the
# same rework on the long side.
#
# Shared geometry: entry $250.00, ATR $5.00, a "range" setup (2.5 base, no setup scaling
# = 2.5 ATRs), so:
#   band distance   2.5 x 5.00 = $12.5 ->  band edge   $262.5
#   absolute floor  1.00 x 5.00 = $5.00  ->  hard floor  $255.00  (unchanged)
#   match tolerance 0.25 x 5.00 = $1.25                           (unchanged)
# The computed support at $220.00 becomes the derived target (reward $30.00).
#
# The tight-stop fixture moved $256.00 -> $260.00. The exemption window — the
# stop distances where being level-backed is the ONLY thing that decides the
# outcome — is (hard floor, band edge) = ($255.00, $262.5), i.e. [1.00, 2.5]
# ATRs. It was [1.00, 1.35] ATRs ($255.00-$256.75) at the 1.5 base and
# [1.00, 3.45] before that. The old $256.00 (1.20 ATRs) is still inside the
# new window, but it now sits $5.25 from the band edge and only $1.00 from
# the hard floor, so the "tight stop the band would overwrite" it represents
# is barely distinguishable from the "tight stop the hard floor overwrites"
# case that has its own test below. $260.00 is 2.00 ATRs out — $1.25 inside
# the band edge and $5.00 clear of the hard floor — which puts it back in the
# middle of the window the way $256.00 sat in the middle of the old one.
#
# Worked by hand, on the fixed $30.00 reward:
#   honoured:  30.00 / (260.00 - 250.00) = 30.00 / 10.00 = 3.00
#   widened:   30.00 / (262.5 - 250.00) = 30.00 / 12.5 = 2.40
# Both clear the 1.5 floor (the short block has never used the long side's
# straddle design — see that block's comment for the pair that does).

_S_ENTRY = 250.0
_S_ATR = 5.0
_S_BAND_EDGE = 262.5  # 2.5 x ATR above entry — the unconditional stop
_S_HARD_FLOOR = 255.0  # 1.00 x ATR above entry — the deterministic floor
_S_TIGHT_STOP = 260.0  # 2.00 x ATR out — inside the band, outside the floor
_S_TARGET_LEVEL = 220.0  # computed support below entry; the derived target


def test_short_level_backed_tight_stop_is_honoured_not_widened():
    """§12.1, short side. A stop 2.00 ATRs above entry sits inside the 2.5
    ATR band and would have been overwritten. A COMPUTED resistance level at
    that price means it is real, so it survives.

    Worked by hand: risk 260.00 - 250.00 = $10.00 against reward 250.00 -
    220.00 = $30.00, so R/R 3.00 — comfortably over the 1.5 floor."""
    constructor = PortfolioConstructor()
    decisions = constructor.construct_orders(
        targets=[_short_target()],
        positions=[],
        analyses=[
            _short_analysis(
                entry=_S_ENTRY,
                stop=_S_TIGHT_STOP,
                target=_S_TARGET_LEVEL,
                atr_14=_S_ATR,
                computed=[_S_TARGET_LEVEL, _S_TIGHT_STOP],
            )
        ],
        total_value=100_000,
        price_map={"TSLA": _S_ENTRY},
    )
    assert len(decisions) == 1
    assert decisions[0].stop_loss == _S_TIGHT_STOP
    assert decisions[0].stop_loss != _S_BAND_EDGE


def test_short_unbacked_tight_stop_is_still_widened_to_the_band():
    """The old behaviour, intact. Same trade, but nothing computed sits above
    the $260.00 stop — the analyst simply placed it there — so the band
    applies exactly as it always did and the stop ships at $262.5 (2.25 x
    ATR above entry). Reward $30.00 over the widened risk of $12.5 is 2.40,
    which still clears the floor, so the trade ships WIDENED rather than
    being refused — the assertion is on the stop price, not on survival."""
    constructor = PortfolioConstructor()
    decisions = constructor.construct_orders(
        targets=[_short_target()],
        positions=[],
        analyses=[
            _short_analysis(
                entry=_S_ENTRY,
                stop=_S_TIGHT_STOP,
                target=_S_TARGET_LEVEL,
                atr_14=_S_ATR,
                computed=[_S_TARGET_LEVEL],  # the stop is NOT a computed level
            )
        ],
        total_value=100_000,
        price_map={"TSLA": _S_ENTRY},
    )
    assert len(decisions) == 1
    assert decisions[0].stop_loss == _S_BAND_EDGE


def test_short_reward_risk_is_measured_against_the_stop_that_will_ship():
    """The point of §12.1 on the short side. Reward $30.00 is fixed by the
    computed target; the risk is whichever stop actually ships. Honoured:
    30.00 / 10.00 = 3.00. Widened: 30.00 / 12.5 = 2.40. Both clear
    the floor here, and the honoured one is worth more — which is also what
    lets it carry a bigger position for the same risk budget.

    The GAP between the two is a function of the band width and has moved
    twice: 3.53 vs 1.74 at the 3.45-ATR band, 5.00 vs 4.44 at the 1.35-ATR
    band, 3.00 vs 2.67 now at 2.5 ATRs. §12.1's exemption exists to stop a
    fabricated band stop from destroying the ratio, so the gap is SUPPOSED to
    track how much ratio the band has to destroy. It is not a threshold."""
    constructor = PortfolioConstructor()

    def stop_for(computed):
        return constructor._widen_stop_past_noise(
            "TSLA",
            _short_analysis(
                entry=_S_ENTRY, stop=_S_TIGHT_STOP, target=_S_TARGET_LEVEL, atr_14=_S_ATR, computed=computed
            ),
            entry_price=_S_ENTRY,
            stop_loss=_S_TIGHT_STOP,
            direction="short",
            target_price=_S_TARGET_LEVEL,
        )

    honoured = stop_for([_S_TARGET_LEVEL, _S_TIGHT_STOP])
    assert honoured == _S_TIGHT_STOP
    assert round((_S_ENTRY - _S_TARGET_LEVEL) / (honoured - _S_ENTRY), 2) == 3.00

    widened = stop_for([_S_TARGET_LEVEL])
    assert widened == _S_BAND_EDGE
    assert round((_S_ENTRY - _S_TARGET_LEVEL) / (widened - _S_ENTRY), 2) == 2.4


def test_short_level_backed_stop_inside_one_atr_is_floored_at_one_atr():
    """The deterministic backstop, mirrored. §12.1's safety argument rests on
    a rule written in `config/prompts/tech_analyst.md`, and Invariant 2
    requires the deterministic layer to be the final authority. A real
    resistance level $2.00 above entry is genuine structure AND a guaranteed
    whipsaw, so the stop moves out to exactly 1x ATR — not to the 2.5x
    band. (The gap between the two has narrowed and widened again as the base
    floor moved 3.0 -> 1.5 -> 2.5, but the destination rule is unchanged.)"""
    constructor = PortfolioConstructor()
    decisions = constructor.construct_orders(
        targets=[_short_target(weight=20.0)],  # 4% floor stop: 20% weight risks 0.8%, over the owner's 0.5% floor
        positions=[],
        analyses=[
            _short_analysis(
                entry=_S_ENTRY,
                stop=252.0,
                target=_S_TARGET_LEVEL,
                atr_14=10.0,  # 1 ATR = $10 (4%): the $5 ATR floors at 2%, under the owner risk floor at the weight cap
                computed=[_S_TARGET_LEVEL, 252.0],
            )
        ],
        total_value=100_000,
        price_map={"TSLA": _S_ENTRY},
    )
    assert len(decisions) == 1
    assert decisions[0].stop_loss == _S_ENTRY + 10.0  # exactly 1x the $10 ATR
    assert decisions[0].stop_loss != _S_ENTRY + 2.5 * 10.0


def test_short_near_miss_outside_the_tolerance_is_not_level_backed():
    """Since item 215 (2026-09-30) the boundary is the traded range of a BAR
    that drew the level, not the level's zone: the session that turned at
    $260.00 traded down to $258.00, so $258.00 is sitting on it and $257.00
    is not, and the second gets the band like any unbacked stop. The two
    candidate stops and every other number here are unchanged — only what
    makes $258.00 count has changed, from "within 1% of the level" to
    "inside a session the market actually defended".

    Both candidates are deliberately INSIDE the 2.25-ATR band ($261.25) and
    outside the 1-ATR hard floor ($255.00), so level-backing is the only
    thing that can decide either one. That window is $6.25 wide at the 2.5
    base, wide enough to hold the $2.60 zone plus a candidate either side of
    it. Note the pair sits BELOW the level rather than straddling it: the
    zone's upper edge ($262.60) is past the band edge, where a stop is left
    alone for reasons unrelated to backing and the assert would say nothing.

    (Was $260.10 / $258.60 either side of a 0.25 x ATR = $1.25 tolerance.
    That tolerance was DELETED on 2026-09-13 — docs/WORK.md item 46: an ATR
    multiple cannot stay consistent with the percentage-of-price zone it
    claimed to cover. This fixture is the case that was too GENEROUS: at
    $5 ATR on a $260 level, ATR is 1.92% of price, so 0.25 ATR was 1.3x the
    zone. The fixture level, band and floor are unchanged.)"""
    constructor = PortfolioConstructor()

    def stop_for(stop):
        return constructor._widen_stop_past_noise(
            "TSLA",
            _short_analysis(
                entry=_S_ENTRY,
                stop=stop,
                target=_S_TARGET_LEVEL,
                atr_14=_S_ATR,
                computed=[_S_TARGET_LEVEL, _S_TIGHT_STOP],
                bars={_S_TIGHT_STOP: [(258.0, _S_TIGHT_STOP)]},
            ),
            entry_price=_S_ENTRY,
            stop_loss=stop,
            direction="short",
            target_price=_S_TARGET_LEVEL,
        )

    assert stop_for(258.0) == 258.0  # gap $2.00, inside the zone
    assert stop_for(257.0) == _S_BAND_EDGE  # gap $3.00, outside it


def test_short_level_the_model_asserted_does_not_earn_the_exemption():
    """`resistance_levels` is the LLM's own output and names the stop price
    exactly. `computed_levels`, which only Python writes, does not. The band
    applies — a model must not be able to buy an exemption from the noise
    floor by asserting a level beside its stop."""
    constructor = PortfolioConstructor()
    analysis = _short_analysis(
        entry=_S_ENTRY,
        stop=_S_TIGHT_STOP,
        target=_S_TARGET_LEVEL,
        atr_14=_S_ATR,
        computed=[_S_TARGET_LEVEL],
    )
    assert analysis.resistance_levels == [_S_TIGHT_STOP]  # the model said so
    assert _S_TIGHT_STOP not in analysis.computed_levels  # the chart did not
    assert (
        constructor._widen_stop_past_noise(
            "TSLA",
            analysis,
            entry_price=_S_ENTRY,
            stop_loss=_S_TIGHT_STOP,
            direction="short",
            target_price=_S_TARGET_LEVEL,
        )
        == _S_BAND_EDGE
    )


def test_short_a_level_with_few_touches_still_earns_the_exemption():
    """2026-10-14, mirrored on the short side: the resistance at $260.00
    with 4 touches backs the stop. The 5-touch bar is deleted."""
    constructor = PortfolioConstructor()
    decisions = constructor.construct_orders(
        targets=[_short_target()],
        positions=[],
        analyses=[
            _short_analysis(
                entry=_S_ENTRY,
                stop=_S_TIGHT_STOP,
                target=_S_TARGET_LEVEL,
                atr_14=_S_ATR,
                computed=[_S_TARGET_LEVEL, _S_TIGHT_STOP],
                touches={_S_TARGET_LEVEL: 5, _S_TIGHT_STOP: 4},
            )
        ],
        total_value=100_000,
        price_map={"TSLA": _S_ENTRY},
    )
    assert len(decisions) == 1
    assert decisions[0].stop_loss == _S_TIGHT_STOP


def test_short_a_level_with_five_touches_earns_the_exemption():
    constructor = PortfolioConstructor()
    decisions = constructor.construct_orders(
        targets=[_short_target()],
        positions=[],
        analyses=[
            _short_analysis(
                entry=_S_ENTRY,
                stop=_S_TIGHT_STOP,
                target=_S_TARGET_LEVEL,
                atr_14=_S_ATR,
                computed=[_S_TARGET_LEVEL, _S_TIGHT_STOP],
                touches={_S_TARGET_LEVEL: 5, _S_TIGHT_STOP: 5},
            )
        ],
        total_value=100_000,
        price_map={"TSLA": _S_ENTRY},
    )
    assert len(decisions) == 1
    assert decisions[0].stop_loss == _S_TIGHT_STOP


def test_short_a_level_below_entry_cannot_back_a_shorts_stop():
    """Side discipline, mirrored. A short's stop sits above entry, so only
    structure at or above entry can be what it rests on. The $220.00 computed
    support is a target, not a backstop."""
    constructor = PortfolioConstructor()
    analysis = _short_analysis(
        entry=_S_ENTRY,
        stop=_S_TIGHT_STOP,
        target=_S_TARGET_LEVEL,
        atr_14=_S_ATR,
        computed=[_S_TARGET_LEVEL, _S_TIGHT_STOP],
    )
    assert (
        constructor._level_backing_stop(
            analysis,
            _S_ENTRY,
            _S_TIGHT_STOP,
            is_short=True,
        )
        == _S_TIGHT_STOP
    )
    # The support below entry is never eligible, at any distance.
    assert (
        constructor._level_backing_stop(
            analysis,
            _S_ENTRY,
            _S_TARGET_LEVEL,
            is_short=True,
        )
        is None
    )


# ==========================================================================
# 5. D6 — the borrow gate: three distinct refusals
# ==========================================================================


def _borrow_gated_ctx_and_pipeline(borrow_result_or_exc):
    constructor = PortfolioConstructor()
    decisions = constructor.construct_orders(
        targets=[_short_target()],
        positions=[],
        analyses=[_short_analysis()],
        total_value=100_000,
        price_map={"TSLA": 250.0},
    )
    assert len(decisions) == 1

    pipeline = _exec_pipeline()
    pipeline.broker.get_latest_price.return_value = 250.0
    pipeline.broker.get_latest_quote.return_value = {"bid_price": 250.0, "ask_price": 250.0}
    if isinstance(borrow_result_or_exc, Exception):
        pipeline.broker.get_shortability.side_effect = borrow_result_or_exc
    else:
        pipeline.broker.get_shortability.return_value = borrow_result_or_exc
    ctx = _ctx(decisions)
    return pipeline, ctx


def test_borrow_gate_refuses_a_not_shortable_symbol():
    pipeline, ctx = _borrow_gated_ctx_and_pipeline(
        {"shortable": False, "easy_to_borrow": True, "reason": "not_shortable"},
    )
    orders = ExecutionStage(pipeline=pipeline).run(ctx)
    assert orders == []
    pipeline.broker.submit_order.assert_not_called()
    assert ctx.execution_skips[0]["reason"] == "borrow_gate"
    assert ctx.execution_skips[0]["detail"] == "not_shortable"


def test_borrow_gate_refuses_a_hard_to_borrow_symbol():
    pipeline, ctx = _borrow_gated_ctx_and_pipeline(
        {"shortable": True, "easy_to_borrow": False, "reason": "hard_to_borrow"},
    )
    orders = ExecutionStage(pipeline=pipeline).run(ctx)
    assert orders == []
    pipeline.broker.submit_order.assert_not_called()
    assert ctx.execution_skips[0]["reason"] == "borrow_gate"
    assert ctx.execution_skips[0]["detail"] == "hard_to_borrow"


def test_borrow_gate_refuses_when_flags_are_unreadable():
    """A broker/API failure must fail CLOSED, not guess the short open."""
    pipeline, ctx = _borrow_gated_ctx_and_pipeline(RuntimeError("asset API down"))
    orders = ExecutionStage(pipeline=pipeline).run(ctx)
    assert orders == []
    pipeline.broker.submit_order.assert_not_called()
    assert ctx.execution_skips[0]["reason"] == "borrow_gate"
    assert ctx.execution_skips[0]["detail"] == "asset_lookup_failed"


# ==========================================================================
# 6. D7 — protective-stop failure on a short escalates to a market cover
# ==========================================================================


def test_protective_stop_failure_on_a_short_triggers_immediate_market_cover():
    constructor = PortfolioConstructor()
    decisions = constructor.construct_orders(
        targets=[_short_target()],
        positions=[],
        analyses=[_short_analysis()],
        total_value=100_000,
        price_map={"TSLA": 250.0},
    )
    decision = decisions[0]

    pipeline = _exec_pipeline()
    pipeline.broker.get_latest_price.return_value = 250.0
    pipeline.broker.get_latest_quote.return_value = {"bid_price": 250.0, "ask_price": 250.0}
    entry_order = {
        "id": "short-order-1",
        "status": "accepted",
        "symbol": "TSLA",
        "side": "sell_short",
        "pending_stop_price": decision.stop_loss,
    }
    cover_order = {
        "id": "cover-emergency-1",
        "status": "accepted",
        "symbol": "TSLA",
        "side": "buy",
    }
    pipeline.broker.submit_order.side_effect = [entry_order, cover_order]
    pipeline.broker.get_order_fill_info.return_value = {"filled_qty": 20.0}
    # The protective stop FAILS.
    pipeline.broker.place_entry_protection.return_value = None

    ctx = _ctx([decision])
    ExecutionStage(pipeline=pipeline).run(ctx)

    assert pipeline.broker.submit_order.call_count == 2, (
        "the entry AND the emergency cover must both have been submitted"
    )
    emergency_kwargs = pipeline.broker.submit_order.call_args_list[1].kwargs
    assert emergency_kwargs["symbol"] == "TSLA"
    assert emergency_kwargs["qty"] == 20.0
    assert emergency_kwargs["side"] == "buy"


# ==========================================================================
# 7. D8 — short/long sizing PARITY (the gap haircut was deleted)
# ==========================================================================


def test_short_and_long_are_sized_identically_at_equal_risk_and_geometry():
    """Same risk allocation (0.5%), same $12.50/share stop distance, same
    entry $250 and gross multiplier (1x) for both a long and a short. Owner
    ruling 2026-10-04 deleted the short-side haircut, so direction is the
    only difference and NOTHING about the size may differ."""
    constructor = PortfolioConstructor()

    long_decisions = constructor.construct_orders(
        targets=[
            TargetPosition(
                symbol="LONGX", direction="long", risk_allocation_pct=0.5, conviction="high", thesis="breakout"
            )
        ],
        positions=[],
        analyses=[_long_analysis(symbol="LONGX", entry=250.0, stop=237.5, target=300.0)],
        total_value=100_000,
        price_map={"LONGX": 250.0},
    )
    short_decisions = constructor.construct_orders(
        targets=[
            TargetPosition(
                symbol="SHORTX", direction="short", risk_allocation_pct=0.5, conviction="high", thesis="breakdown"
            )
        ],
        positions=[],
        analyses=[_short_analysis(symbol="SHORTX", entry=250.0, stop=262.5, target=200.0)],
        total_value=100_000,
        price_map={"SHORTX": 250.0},
    )
    assert len(long_decisions) == 1 and long_decisions[0].action == "BUY"
    assert len(short_decisions) == 1 and short_decisions[0].action == "SHORT"

    long_alloc = long_decisions[0].allocation_pct
    short_alloc = short_decisions[0].allocation_pct

    # THE numeric assertion: same risk allocation, same stop distance,
    # same size. Previously this asserted long / 1.5 == 6.67.
    assert long_alloc == 10.0
    assert short_alloc == long_alloc, (
        "a short must open the SAME size as an equivalent long at the same "
        "risk allocation (owner ruling 2026-10-04: no different math)"
    )


# ==========================================================================
# 8. Shorts carry the SAME limits as longs (owner decision 2026-09-17)
# ==========================================================================


def test_single_short_cap_hard_blocks_opening_too_large_a_short():
    engine = RiskRuleEngine(_cfg(max_position_pct=10.0))
    decision = TradeDecision(
        action="SHORT",
        symbol="XYZ",
        allocation_pct=15.0,  # 15% > 10% cap
        entry_price=100.0,
        stop_loss=110.0,
        take_profit=80.0,
        reasoning="t",
    )
    violations = engine.check(
        decision=decision,
        positions=[],
        total_value=100_000,
    )
    rules = {v.rule for v in violations}
    assert "max_position_pct" in rules


def test_a_short_is_capped_exactly_where_an_equivalent_long_is():
    """Owner decision 2026-09-17: one limit, both directions. A short and a
    long of the same size against the same held position size trip the same
    rule at the same boundary — no tighter short-specific cap."""
    cap = 33.0
    engine = RiskRuleEngine(_cfg(max_position_pct=cap, max_total_position_pct=100.0))

    def _rules(action, held_qty, alloc):
        d = TradeDecision(
            action=action,
            symbol="XYZ",
            allocation_pct=alloc,
            entry_price=100.0,
            stop_loss=110.0 if action == "SHORT" else 90.0,
            take_profit=80.0 if action == "SHORT" else 120.0,
            reasoning="t",
        )
        return {
            v.rule
            for v in engine.check(
                decision=d,
                positions=[_pos("XYZ", qty=held_qty, entry=100, price=100)],
                total_value=100_000,
            )
        }

    # 20% held + 13% new = exactly the cap: both pass.
    assert "max_position_pct" not in _rules("BUY", 200, 13.0)
    assert "max_position_pct" not in _rules("SHORT", -200, 13.0)
    # 20% held + 14% new = one point over: both are blocked by the same rule.
    assert "max_position_pct" in _rules("BUY", 200, 14.0)
    assert "max_position_pct" in _rules("SHORT", -200, 14.0)


def test_many_shorts_are_not_blocked_by_a_separate_bearish_book_cap():
    """The former 20% gross-bearish cap is gone: a book already 60% short
    may add another short within the long-side limits."""
    engine = RiskRuleEngine(
        _cfg(
            max_position_pct=33.0,
            max_total_position_pct=100.0,
            max_sector_pct=100.0,  # sector crowding is a separate control
        )
    )
    positions = [
        _pos("AAA", qty=-300, entry=100, price=100),  # -30%
        _pos("BBB", qty=-300, entry=100, price=100),  # -30%
    ]
    decision = TradeDecision(
        action="SHORT",
        symbol="CCC",
        allocation_pct=10.0,
        entry_price=100.0,
        stop_loss=110.0,
        take_profit=80.0,
        reasoning="t",
    )
    violations = engine.check(
        decision=decision,
        positions=positions,
        total_value=100_000,
    )
    assert violations == []


def test_no_cap_blocks_a_cover():
    """No cap may ever block a COVER, even reducing a position that is
    ALREADY over the single-name ceiling."""
    engine = RiskRuleEngine(_cfg(max_position_pct=10.0))
    positions = [_pos("XYZ", qty=-250, entry=100, price=100)]  # -$25,000 = 25%
    decision = TradeDecision(
        action="COVER",
        symbol="XYZ",
        allocation_pct=50.0,
        entry_price=0.0,
        stop_loss=0.0,
        take_profit=0.0,
        reasoning="reduce",
    )
    violations = engine.check(
        decision=decision,
        positions=positions,
        total_value=100_000,
    )
    assert violations == []


@pytest.mark.parametrize(
    "key",
    [
        "max_single_short_pct",
        "max_gross_bearish_pct",
        "max_short_gross_pct",
    ],
)
def test_removed_short_cap_keys_raise_a_clear_error(key):
    """A settings file still carrying a removed short cap must fail loudly,
    not load silently with the operator believing the cap is in force."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError) as exc_info:
        _cfg(**{key: 10.0})
    message = str(exc_info.value)
    assert key in message
    assert "same limits as longs" in message


# ==========================================================================
# 9. D10 — a cover can never be blocked by the cash rule
# ==========================================================================


def test_cover_not_blocked_by_negative_cash_when_margin_disallowed():
    engine = RiskRuleEngine(_cfg(allow_margin=False))
    decision = TradeDecision(
        action="COVER",
        symbol="XYZ",
        allocation_pct=100.0,
        entry_price=0.0,
        stop_loss=0.0,
        take_profit=0.0,
        reasoning="close",
    )
    violations = engine.check(
        decision=decision,
        positions=[_pos("XYZ", qty=-40, entry=100, price=100)],
        total_value=100_000,
        cash=-5_000.0,
    )
    assert violations == []


def test_buy_is_still_blocked_by_negative_cash_when_margin_disallowed():
    """Contrast case, proving the COVER exemption is real and not just an
    engine-wide bypass: an ordinary BUY under the same negative-cash
    conditions is still hard-blocked exactly as before."""
    engine = RiskRuleEngine(_cfg(allow_margin=False))
    decision = TradeDecision(
        action="BUY",
        symbol="XYZ",
        allocation_pct=10.0,
        entry_price=100.0,
        stop_loss=95.0,
        take_profit=120.0,
        reasoning="t",
    )
    violations = engine.check(
        decision=decision,
        positions=[],
        total_value=100_000,
        cash=-5_000.0,
    )
    rules = {v.rule for v in violations}
    assert "cash_only" in rules


# ==========================================================================
# 10. Long-only regression proof
# ==========================================================================


def test_constructor_long_only_output_unchanged_with_no_shorts_anywhere():
    """No-op proof, literal-for-literal — same idiom as
    tests/test_shorts_countable.py's `*_long_only_unchanged` tests. A
    book with no `direction='short'` target and no short position anywhere
    produces exactly the pre-Stage-3 BUY."""
    constructor = PortfolioConstructor()
    decisions = constructor.construct_orders(
        targets=[TargetPosition(symbol="NVDA", target_weight_pct=15.0, conviction="high", thesis="add")],
        positions=[_pos("NVDA", qty=50, entry=100, price=100)],  # 5% held
        analyses=[_long_analysis(symbol="NVDA", entry=100.0, stop=95.0, target=115.0)],
        total_value=100_000,
        price_map={"NVDA": 100.0},
    )
    assert len(decisions) == 1
    d = decisions[0]
    assert d.action == "BUY"
    assert d.symbol == "NVDA"
    assert d.allocation_pct == 10.0  # 15% target - 5% held
    assert d.entry_price == 100.0
    assert d.stop_loss == 95.0
    assert d.take_profit == 115.0


def test_risk_engine_long_only_output_unchanged_with_no_shorts_anywhere():
    """No-op proof for RiskRuleEngine.check(): a plain BUY against a
    long-only book, run through the exact same `is_short=False` path the
    original single-position-size rule always used, produces the same
    violation it always did."""
    engine = RiskRuleEngine(_cfg(max_position_pct=20.0))
    decision = TradeDecision(
        action="BUY",
        symbol="XYZ",
        allocation_pct=25.0,  # 25% > 20% cap
        entry_price=100.0,
        stop_loss=95.0,
        take_profit=120.0,
        reasoning="t",
    )
    violations = engine.check(
        decision=decision,
        positions=[],
        total_value=100_000,
    )
    assert len(violations) == 1
    v = violations[0]
    assert v.rule == "max_position_pct"
    assert v.value == 25.0
    assert v.limit == 20.0

    # And the clean case: well under every cap, zero violations.
    clean = TradeDecision(
        action="BUY",
        symbol="XYZ",
        allocation_pct=5.0,
        entry_price=100.0,
        stop_loss=95.0,
        take_profit=120.0,
        reasoning="t",
    )
    assert (
        engine.check(
            decision=clean,
            positions=[],
            total_value=100_000,
        )
        == []
    )


# ==========================================================================
# 11. Gap fix — an emergency close cancels a resting entry order on EITHER
#    side. Previously EMERGENCY_COVER (closing a short) left a resting
#    SELL-to-open entry order untouched — a fill on it would re-open the
#    exact short exposure the emergency close just cleared. The mechanism
#    itself (order-type-based filtering in AlpacaBroker.cancel_open_entry_
#    orders) is unit-tested in tests/test_broker.py; these two prove the
#    pipeline actually invokes it identically on both sides.
# ==========================================================================


def test_emergency_cover_cancels_the_symbols_resting_short_entry_order():
    """EMERGENCY_COVER must cancel that symbol's own resting entry order
    exactly as EMERGENCY_SELL does for a long."""
    p = build_pipeline(broker=MagicMock(), db=MagicMock())
    p.broker.submit_order.return_value = {"id": "o1", "status": "accepted"}
    p._cancel_stops_with_write_ahead = MagicMock(return_value=(True, [], 7))

    p._submit_protected_sell(
        symbol="TSLA",
        qty=40,
        limit_price=252.5,
        reference_price=250.0,
        position_qty_before_sell=40,
        label="EMERGENCY_COVER",
        side="buy",
    )
    p.broker.cancel_open_entry_orders.assert_called_once_with(symbol="TSLA")


def test_emergency_sell_still_cancels_the_symbols_resting_long_entry_order():
    """Long-side regression proof — literal mirror of the test above. The
    pre-existing EMERGENCY_SELL behaviour is unaffected by the short-side
    fix (same assertion shape as tests/test_pipeline.py's
    test_full_exit_sell_cancels_same_symbol_entry_orders)."""
    p = build_pipeline(broker=MagicMock(), db=MagicMock())
    p.broker.submit_order.return_value = {"id": "o1", "status": "accepted"}
    p._cancel_stops_with_write_ahead = MagicMock(return_value=(True, [], 7))

    p._submit_protected_sell(
        symbol="VST",
        qty=31,
        limit_price=150.0,
        reference_price=151.0,
        position_qty_before_sell=31,
        label="EMERGENCY_SELL",
    )
    p.broker.cancel_open_entry_orders.assert_called_once_with(symbol="VST")


# ==========================================================================
# 12. Gap fix — the midday/close reviewer can COVER a short
# ==========================================================================


def _mk_review_with_action(symbol: str, action: str, reason: str):
    """Minimal review-shaped object _midday_execute_llm_actions accepts —
    same helper shape as tests/test_position_reviewer.py's
    _mk_review_with_action, kept local so this file stands alone."""
    return MagicMock(
        actions=[
            PositionAction(
                action=action,
                symbol=symbol,
                reason=reason,
            )
        ]
    )


def _midday_pipeline_with_short(symbol: str, qty: float, current_price: float):
    """Pipeline scaffold sufficient to exercise _midday_execute_llm_actions
    on a single SHORT position. Mirrors tests/test_position_reviewer.py's
    _executor_pipeline_with_position, kept local for the same reason."""
    pipeline = build_pipeline(broker=MagicMock(), db=MagicMock())
    pipeline.broker.snapshot_protective_stops.return_value = (True, [])
    pipeline.broker.cancel_snapshotted_stops.return_value = MagicMock(cleared=True)
    pipeline.broker.cancel_protective_stops.return_value = (True, [])
    pipeline.broker.submit_order.return_value = {
        "id": "cover-order",
        "status": "accepted",
        "symbol": symbol,
    }
    pipeline.broker.get_latest_price.return_value = current_price
    pipeline.broker.get_latest_quote.return_value = {"bid_price": current_price, "ask_price": current_price}
    pipeline.broker.wait_for_order_terminal.return_value = "filled"
    pipeline.broker.get_order_fill_info.return_value = {
        "status": "filled",
        "filled_qty": str(qty),
        "filled_avg_price": str(current_price),
    }
    pipeline.db.has_pending_action_for_symbol.return_value = False
    pipeline._order_accepted = MagicMock(return_value=True)
    pipeline._reprotect_residual_after_partial_sell = MagicMock()
    pipeline._format_qty = lambda q: str(q)
    return pipeline


def test_midday_reviewer_covers_a_short_end_to_end():
    """A held SHORT with a COVER action naming a hard trigger executes as a
    BUY-to-cover for the full absolute qty — the primary Gap 2 proof."""
    position = _pos("TSLA", qty=-40, entry=250.0, price=240.0)
    pipeline = _midday_pipeline_with_short("TSLA", 40.0, 240.0)
    review = _mk_review_with_action(
        "TSLA",
        "COVER",
        "thesis_invalid_if condition satisfied — guidance cut reversed the "
        "setup, bullish reversal confirmed above the defended level.",
    )

    hold(pipeline.broker, {position.symbol: position.qty})  # what the broker holds when the exit is re-sized
    orders = pipeline._midday_execute_llm_actions([position], review, run_id="r1")

    assert len(orders) == 1
    pipeline.broker.submit_order.assert_called_once()
    submit_kwargs = pipeline.broker.submit_order.call_args.kwargs
    assert submit_kwargs["side"] == "buy"
    assert submit_kwargs["qty"] == 40.0
    assert submit_kwargs["symbol"] == "TSLA"
    # A plain market buy-to-cover since 2026-10-09 (no limit to sit above).
    assert submit_kwargs["limit_price"] is None


def test_midday_cover_refused_without_named_trigger():
    """A COVER whose reason names no recognised trigger is refused exactly
    as a SELL would be — the phrase gate applies identically."""
    position = _pos("TSLA", qty=-40, entry=250.0, price=240.0)
    pipeline = _midday_pipeline_with_short("TSLA", 40.0, 240.0)
    review = _mk_review_with_action(
        "TSLA",
        "COVER",
        "price fell a lot, prudent to lock in the gain.",
    )

    orders = pipeline._midday_execute_llm_actions([position], review, run_id="r1")

    assert orders == []
    pipeline.broker.submit_order.assert_not_called()


def test_midday_cover_not_blocked_by_negative_cash():
    """D10 mirrored at the executor: _midday_execute_llm_actions carries no
    cash or exposure gate on ANY exit action, COVER included — a closing
    action must never be blockable by the cash rule (being unable to close
    is strictly worse than being unable to open). Proven here by executing
    a COVER successfully with the pipeline's account state showing negative
    cash and margin disallowed; the executor doesn't even look at it."""
    position = _pos("TSLA", qty=-40, entry=250.0, price=240.0)
    pipeline = _midday_pipeline_with_short("TSLA", 40.0, 240.0)
    pipeline.config = MagicMock()
    pipeline.config.risk.allow_margin = False
    pipeline.cash = -5_000.0  # not read anywhere on this path — that's the point
    review = _mk_review_with_action(
        "TSLA",
        "COVER",
        "thesis_invalid_if condition satisfied — guidance cut reversed the setup.",
    )

    hold(pipeline.broker, {position.symbol: position.qty})  # what the broker holds when the exit is re-sized
    orders = pipeline._midday_execute_llm_actions([position], review, run_id="r1")

    assert len(orders) == 1
    pipeline.broker.submit_order.assert_called_once()


# ==========================================================================
# 13. Gap fix — a short position reaching the reviewer carries its side
# ==========================================================================


def test_short_position_reaches_reviewer_payload_with_its_side():
    """The reviewer payload must state a held short's side explicitly so it
    cannot read a winning short as a loser. Confirms the fix already lives
    in PositionReviewerAgent.build_user_message and actually reaches the
    reviewer path this change touches (see also the fuller pnl-sign proof
    in tests/test_position_reviewer.py)."""
    from unittest.mock import patch as _patch
    from src.agents.position_reviewer import PositionReviewerAgent

    with _patch("anthropic.Anthropic"):
        agent = PositionReviewerAgent(api_key="test", model="claude-sonnet-4-6")
        msg = agent.build_user_message(
            session_type="midday",
            positions=[_pos("TSLA", qty=-40, entry=250.0, price=240.0)],
            macro_summary={"vix": {"current": 18.0}},
            cash_balance=1_000.0,
            total_value=100_000.0,
        )

    assert "[SHORT]" in msg


# ==========================================================================
# 14. Long-only regression proof — both gap fixes leave a pure-long book
#    behaving exactly as before
# ==========================================================================


def test_long_only_midday_actions_unchanged_with_no_shorts_anywhere():
    """No COVER, no short position anywhere: SELL still executes exactly as
    pre-Stage-3 (side='sell', full qty, limit 0.5% below reference)."""
    position = _pos("VST", qty=31, entry=100.0, price=150.0)
    pipeline = _midday_pipeline_with_short("VST", 31.0, 150.0)
    review = _mk_review_with_action(
        "VST",
        "SELL",
        "thesis_invalid_if condition satisfied — thesis broken on filing.",
    )

    hold(pipeline.broker, {position.symbol: position.qty})  # what the broker holds when the exit is re-sized
    orders = pipeline._midday_execute_llm_actions([position], review, run_id="r1")

    assert len(orders) == 1
    submit_kwargs = pipeline.broker.submit_order.call_args.kwargs
    assert submit_kwargs["side"] == "sell"
    assert submit_kwargs["qty"] == 31.0
    assert submit_kwargs["limit_price"] is None  # a plain market order since 2026-10-09


def test_cancel_open_entry_orders_long_only_book_cancels_only_the_buy():
    """Long-only regression proof for the broker-level fix: a book with no
    short-side orders at all cancels exactly what it always did (see
    tests/test_broker.py's fuller mixed-book proof for the mechanism)."""
    p = build_pipeline(broker=MagicMock(), db=MagicMock())
    p.broker.submit_order.return_value = {"id": "o1", "status": "accepted"}
    p._cancel_stops_with_write_ahead = MagicMock(return_value=(True, [], 7))

    p._submit_protected_sell(
        symbol="VST",
        qty=31,
        limit_price=150.0,
        reference_price=151.0,
        position_qty_before_sell=31,
        label="SELL",
    )
    p.broker.cancel_open_entry_orders.assert_called_once_with(symbol="VST")
