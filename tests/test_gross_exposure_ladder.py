"""Spec §11.2 — the gross-exposure ceiling and the de-levering ladder.

**This file is the owner's gate.** The 90% sector hard-refusal ceiling
(§12.3) was ratified CONDITIONAL on the ladder being proven to actually step
down: *"The 90% works if you've got the ladder, so ensure the ladder works."*
So these are not existence tests. They assert that the ceiling CHANGES at
every threshold, that new exposure is refused BEFORE anything is sold, that
the ladder is applied EXACTLY ONCE, and that none of it depends on the
Portfolio Manager returning a usable book.

The ratified table (owner, 2026-09-01):

    peak-to-trough drawdown   gross exposure ceiling
     0% to  -8%                2.0x
    -8% to -15%                1.5x
   -15% to -20%                1.0x
    worse than -20%            0.5x, and the owner is alerted
"""

from tests.fakes.held_book import hold
import math
import pytest

from src.models import Position, TradeDecision
from src.risk.rules import (
    GROSS_EXPOSURE_RULE,
    GROSS_LADDER,
    RiskRuleEngine,
    apply_gross_ceiling,
    distance_to_forced_liquidation_pct,
    gross_exposure,
    peak_to_trough_pct,
    resolve_gross_ceiling,
)
from src.config import RiskConfig
from tests.pipeline_factory import build_pipeline


EQUITY = 10_000.0
BASE_X = 2.0


def _position(symbol="NVDA", qty=10.0, avg_entry=100.0, current_price=100.0, sector="Technology") -> Position:
    return Position(
        symbol=symbol,
        qty=qty,
        avg_entry=avg_entry,
        current_price=current_price,
        market_value=qty * current_price,
        unrealized_pnl=qty * (current_price - avg_entry),
        sector=sector,
    )


def _buy(symbol="NVDA", alloc=10.0) -> TradeDecision:
    return TradeDecision(
        action="BUY",
        symbol=symbol,
        allocation_pct=alloc,
        entry_price=100.0,
        stop_loss=95.0,
        take_profit=115.0,
        reasoning="high conviction breakout",
    )


def _risk_config(**overrides) -> RiskConfig:
    fields = dict(
        max_position_pct=100,
        max_total_position_pct=400,
        require_stop_loss=False,
        allow_margin=True,
        max_gross_exposure_x=BASE_X,
    )
    fields.update(overrides)
    return RiskConfig(**fields)


# ===========================================================================
# THE GATE, PART 1 — the ladder STEPS. Every rung, both sides of every edge.
# ===========================================================================

#: (drawdown_pct, expected ceiling). Each boundary is probed from BOTH
#: sides, plus the boundary value itself. Ties resolve to the TIGHTER rung —
#: the deliberate fail-closed choice, pinned here so nobody "fixes" it.
LADDER_CASES = [
    #  --- rung 1: 0% to -8% -> 2.0x --------------------------------------
    (0.0, 2.0),
    (-0.01, 2.0),
    (-7.99, 2.0),
    #  --- the -8% edge ---------------------------------------------------
    (-8.0, 1.5),  # exactly on the edge: tighter rung wins
    (-8.01, 1.5),
    #  --- rung 2: -8% to -15% -> 1.5x ------------------------------------
    (-11.0, 1.5),
    (-14.99, 1.5),
    #  --- the -15% edge --------------------------------------------------
    (-15.0, 1.0),
    (-15.01, 1.0),
    #  --- rung 3: -15% to -20% -> 1.0x -----------------------------------
    (-17.5, 1.0),
    (-19.99, 1.0),
    #  --- the -20% edge --------------------------------------------------
    (-20.0, 0.5),
    (-20.01, 0.5),
    #  --- rung 4: worse than -20% -> 0.5x --------------------------------
    (-35.0, 0.5),
    (-99.0, 0.5),
]


@pytest.mark.parametrize("drawdown_pct,expected_x", LADDER_CASES)
def test_the_ladder_steps_at_every_ratified_threshold(drawdown_pct, expected_x):
    """THE GATE. The ceiling must CHANGE at each of the four thresholds.

    A test that only proved the ladder exists would pass on a constant. This
    one fails on a constant, on an off-by-one boundary, and on any rung whose
    value drifts from the ratified table.
    """
    ceiling = resolve_gross_ceiling(drawdown_pct, base_x=BASE_X)
    assert ceiling.ceiling_x == expected_x, (
        f"at {drawdown_pct}% drawdown the ceiling must be {expected_x}x, got {ceiling.ceiling_x}x"
    )


def test_the_ladder_actually_moves_rather_than_returning_one_number():
    """Guards the failure a per-case test cannot see: a ladder that is
    accidentally constant still passes every individual assertion above if
    the constant happens to be right. Four DISTINCT ceilings must exist."""
    ceilings = {resolve_gross_ceiling(dd, base_x=BASE_X).ceiling_x for dd in (-1.0, -10.0, -17.0, -25.0)}
    assert ceilings == {2.0, 1.5, 1.0, 0.5}


def test_each_step_is_strictly_tighter_than_the_one_above_it():
    """Monotonicity: a deeper drawdown may never buy a HIGHER ceiling."""
    probes = [0.0, -8.0, -15.0, -20.0, -40.0]
    ceilings = [resolve_gross_ceiling(p, base_x=BASE_X).ceiling_x for p in probes]
    assert ceilings == sorted(ceilings, reverse=True)
    assert len(set(ceilings)) == 4, "four rungs must produce four values"


def test_the_owner_is_told_from_the_sourced_ten_percent_level_onward():
    """Board item 182, 2026-09-30: the alert threshold is no longer the
    ladder's deepest rung. It is the 10% depreciation-notification level
    published in MiFID Org Regulation Article 62(1) / COBS 16A.4.3UK, so the
    owner hears at -10% and at every deeper drawdown."""
    assert resolve_gross_ceiling(-10.0, base_x=BASE_X).alert_owner is True
    assert resolve_gross_ceiling(-20.0, base_x=BASE_X).alert_owner is True
    assert resolve_gross_ceiling(-25.0, base_x=BASE_X).alert_owner is True
    assert resolve_gross_ceiling(-9.99, base_x=BASE_X).alert_owner is False
    assert resolve_gross_ceiling(-8.0, base_x=BASE_X).alert_owner is False
    assert resolve_gross_ceiling(0.0, base_x=BASE_X).alert_owner is False


def test_the_ladder_can_only_tighten_the_configured_cap_never_raise_it():
    """An operator who lowers the standing cap lowers every rung with it —
    the ladder is a `min`, not a lookup that overrides config."""
    for drawdown in (0.0, -9.0, -16.0, -30.0):
        assert resolve_gross_ceiling(drawdown, base_x=1.0).ceiling_x <= 1.0
    assert resolve_gross_ceiling(-30.0, base_x=1.0).ceiling_x == 0.5


def test_the_ratified_rungs_are_the_ones_in_the_table():
    """If someone edits GROSS_LADDER, this is what tells them the owner
    ratified these specific numbers on 2026-09-01."""
    assert GROSS_LADDER == ((-8.0, 1.5), (-15.0, 1.0), (-20.0, 0.5))


# ===========================================================================
# THE GATE, PART 2 — new exposure is blocked BEFORE anything is trimmed.
# ===========================================================================


def test_new_exposure_is_refused_and_nothing_is_sold_to_make_room():
    """A book at its ceiling refuses the next BUY. It does not sell a held
    position to fund it. This ordering is the whole reason the ladder is not
    a panic-selling mechanism."""
    positions = [_position("NVDA", qty=200.0, current_price=100.0)]  # $20k = 2.0x
    ceiling = resolve_gross_ceiling(0.0, base_x=BASE_X)
    decisions = [_buy("AMD", 10.0)]

    outcome = apply_gross_ceiling(
        decisions,
        positions,
        EQUITY,
        ceiling,
    )

    assert outcome.decisions[0].allocation_pct == 0.0, "the BUY must be refused"
    assert outcome.blocked == ["AMD"]
    assert outcome.trims == [], (
        "nothing may be sold to make room for a new position — the held book is exactly AT its ceiling, not over it"
    )


def test_a_drawdown_blocks_first_and_only_then_trims_the_excess():
    """The full ordering, in one run. The book is $20k gross on $10k equity
    (2.0x). A -16% drawdown drops the ceiling to 1.0x = $10k. So:

      1. the proposed BUY is refused outright (no headroom), and
      2. the held book, which is over on its own, is trimmed by $10k.

    Step 2 must be caused by the HELD book alone. Remove the BUY and the
    trim is identical — asserted directly below.
    """
    positions = [
        _position("NVDA", qty=100.0, current_price=100.0),  # $10k, +0
        _position("AMD", qty=100.0, current_price=100.0, avg_entry=140.0),  # $10k, -4k
    ]
    ceiling = resolve_gross_ceiling(-16.0, base_x=BASE_X)
    assert ceiling.ceiling_x == 1.0

    with_buy = apply_gross_ceiling(
        [_buy("TSLA", 5.0)],
        positions,
        EQUITY,
        ceiling,
    )
    assert with_buy.decisions[0].allocation_pct == 0.0
    assert with_buy.blocked == ["TSLA"]
    assert with_buy.trims, "a book at 2.0x under a 1.0x ceiling must de-lever"

    without_buy = apply_gross_ceiling(
        [],
        list(positions),
        EQUITY,
        ceiling,
    )
    assert [(t.symbol, t.action, t.allocation_pct) for t in without_buy.trims] == [
        (t.symbol, t.action, t.allocation_pct) for t in with_buy.trims
    ], (
        "the trim must be identical with and without a proposed BUY — if a "
        "proposal can change what gets sold, new exposure is not being "
        "blocked first"
    )
    # Biggest loser first: AMD is down $4k, NVDA is flat.
    assert without_buy.trims[0].symbol == "AMD"


def test_an_entry_that_still_fits_is_shrunk_rather_than_dropped():
    """A ceiling that only refuses produces no-trade sessions. Where headroom
    exists, the order is taken smaller."""
    positions = [_position("NVDA", qty=150.0, current_price=100.0)]  # $15k
    ceiling = resolve_gross_ceiling(0.0, base_x=BASE_X)  # $20k ceiling
    decision = _buy("AMD", 100.0)  # wants $10k

    outcome = apply_gross_ceiling(
        [decision],
        positions,
        EQUITY,
        ceiling,
    )

    assert outcome.blocked == []
    assert decision.allocation_pct == pytest.approx(50.0)  # $5k of headroom
    assert outcome.projected_gross <= outcome.ceiling_usd + 1e-6
    assert GROSS_EXPOSURE_RULE in decision.reasoning


def test_a_planned_exit_frees_headroom_before_entries_are_judged():
    """Exits count first — a book already being reduced is judged on what it
    will hold, not on what it holds now. Otherwise a legitimate rotation
    would be refused."""
    positions = [
        _position("NVDA", qty=200.0, current_price=100.0),  # $20k = the ceiling
    ]
    sell = TradeDecision(
        action="SELL",
        symbol="NVDA",
        allocation_pct=50.0,
        entry_price=0.0,
        stop_loss=0.0,
        take_profit=0.0,
        reasoning="rotate",
    )
    buy = _buy("AMD", 50.0)  # wants $5k
    ceiling = resolve_gross_ceiling(0.0, base_x=BASE_X)

    outcome = apply_gross_ceiling(
        [sell, buy],
        positions,
        EQUITY,
        ceiling,
    )

    assert buy.allocation_pct == 50.0, "the freed $10k must fund the rotation"
    assert outcome.trims == []


def test_a_remnant_below_the_old_minimum_order_is_granted_not_refused():
    """Fixed 2026-09-24: a position shrunk to near-nothing by the ceiling
    used to be refused outright as under the flat $500 notional floor — an
    arbitrary number, not a broker minimum, and Alpaca charges no stock
    commission. It is now granted at whatever headroom is left."""
    positions = [_position("NVDA", qty=199.0, current_price=100.0)]  # $19.9k
    ceiling = resolve_gross_ceiling(0.0, base_x=BASE_X)  # $20k
    decision = _buy("AMD", 20.0)

    outcome = apply_gross_ceiling(
        [decision],
        positions,
        EQUITY,
        ceiling,
    )

    # $100 of headroom left (well under the old $500 floor) — granted, not
    # refused.
    assert decision.allocation_pct == pytest.approx(1.0)
    assert outcome.blocked == []
    assert "commission" not in decision.reasoning.lower()


# ===========================================================================
# THE GATE, PART 3 — the ladder is applied EXACTLY ONCE.
# ===========================================================================


def test_gross_headroom_still_converts_to_notional_via_the_multiplier():
    """The headroom the ceiling grants is measured in GROSS, and for a
    leveraged ETF that is not the same number as the order's notional cost —
    SQQQ is 3x, so $600 of gross headroom buys a $200 order.

    Fixed 2026-09-24: that $200 order used to be refused outright as under
    the flat $500 notional floor (an arbitrary number, not a broker minimum,
    and Alpaca charges no stock commission). It is now granted at the size
    the gross/multiplier conversion gives it, same as any other entry.
    """
    positions = [_position("NVDA", qty=194.0, current_price=100.0)]  # $19.4k
    ceiling = resolve_gross_ceiling(0.0, base_x=BASE_X)  # $20k
    short = TradeDecision(
        action="SHORT",
        symbol="SQQQ",
        allocation_pct=10.0,
        entry_price=100.0,
        stop_loss=110.0,
        take_profit=80.0,
        reasoning="hedge",
    )

    outcome = apply_gross_ceiling(
        [short],
        positions,
        EQUITY,
        ceiling,
    )

    # $600 of gross headroom / 3x = a $200 order, granted despite being well
    # under the old $500 floor.
    assert short.allocation_pct == pytest.approx(2.0)
    assert outcome.blocked == []
    assert "commission" not in " ".join(outcome.notes).lower()
    assert "commission" not in short.reasoning.lower()


def test_the_ceiling_is_a_level_so_applying_it_twice_changes_nothing():
    """Double-application is a live failure mode: the PM prompt tells the
    model the ladder is the engine's arithmetic and never its own, so if the
    engine applied it twice — or the PM applied it and the engine applied it
    again — the book would de-lever twice as hard as intended.

    The defence is structural: the ceiling is a LEVEL, not a multiplier.
    Enforcing a level twice is the same level. This test is what would fail
    if anyone reintroduced a compounding factor.
    """
    positions = [_position("NVDA", qty=150.0, current_price=100.0)]  # $15k
    ceiling = resolve_gross_ceiling(-10.0, base_x=BASE_X)  # 1.5x = $15k
    decision = _buy("AMD", 100.0)

    first = apply_gross_ceiling(
        [decision],
        positions,
        EQUITY,
        ceiling,
    )
    after_first = decision.allocation_pct
    trims_first = [(t.symbol, t.allocation_pct) for t in first.trims]

    second = apply_gross_ceiling(
        [decision],
        positions,
        EQUITY,
        ceiling,
    )

    assert decision.allocation_pct == after_first, "a second pass must not shrink the order again"
    assert [(t.symbol, t.allocation_pct) for t in second.trims] == trims_first, (
        "a second pass must not trim the book again"
    )


def test_an_order_that_fits_is_never_scaled_by_the_rung():
    """The specific double-application shape to guard against: treating the
    rung as a MULTIPLIER on size rather than a ceiling on the book.

    A 1.5x rung must not become "x0.75 on every order". If it did, an order
    that comfortably fits under the ceiling would still be shrunk — and
    shrunk AGAIN on a second pass, which is the "de-levers twice as hard as
    intended" failure. The book here is at 0.5x with a 1.5x ceiling, so
    there is ample headroom and any change at all is a bug.
    """
    positions = [_position("NVDA", qty=50.0, current_price=100.0)]  # $5k
    ceiling = resolve_gross_ceiling(-10.0, base_x=BASE_X)  # 1.5x = $15k
    decision = _buy("AMD", 20.0)  # $2k, fits

    apply_gross_ceiling([decision], positions, EQUITY, ceiling)
    assert decision.allocation_pct == 20.0, (
        "an order inside the ceiling must pass through untouched — the rung "
        "is a ceiling on the book, not a multiplier on the order"
    )
    apply_gross_ceiling([decision], positions, EQUITY, ceiling)
    assert decision.allocation_pct == 20.0, "and a second pass must still leave it alone"
    assert GROSS_EXPOSURE_RULE not in decision.reasoning


def test_resolving_the_ladder_twice_yields_the_same_rung():
    """`resolve_gross_ceiling` is a pure function of drawdown. It cannot
    accumulate, because it reads no state of its own."""
    first = resolve_gross_ceiling(-16.0, base_x=BASE_X)
    second = resolve_gross_ceiling(first.drawdown_pct, base_x=BASE_X)
    third = resolve_gross_ceiling(-16.0, base_x=first.ceiling_x)
    assert first.ceiling_x == second.ceiling_x == 1.0
    # Feeding a rung back in as the base cannot step it down a second time.
    assert third.ceiling_x == 1.0


def test_a_de_levered_book_trimmed_once_is_not_trimmed_again():
    """The realistic double-application shape: the preamble trims, the book
    is re-measured, and the second pass must find nothing left to do."""
    ceiling = resolve_gross_ceiling(-16.0, base_x=BASE_X)  # 1.0x = $10k
    positions = [_position("NVDA", qty=200.0, current_price=100.0)]  # $20k

    first = apply_gross_ceiling([], positions, EQUITY, ceiling)
    assert first.trims, "the over-levered book must be trimmed once"

    # The book that now exists after that trim executes.
    sold_fraction = first.trims[0].allocation_pct / 100.0
    remaining = [
        _position("NVDA", qty=200.0 * (1 - sold_fraction), current_price=100.0),
    ]
    second = apply_gross_ceiling([], remaining, EQUITY, ceiling)
    assert second.trims == [], "the ladder must not de-lever a book that already fits its ceiling"


# ===========================================================================
# THE GATE, PART 4 — none of this may depend on the Portfolio Manager.
# ===========================================================================


def test_a_blank_portfolio_manager_session_still_de_levers():
    """**A session where the Portfolio Manager returns nothing at all, with
    the account in a drawdown deep enough to demand a lower ceiling, still
    de-levers.**

    This is not hypothetical. One candidate model was measured returning an
    empty response on 1 run in 10, truncating mid-JSON at the token limit. A
    blank PM session is a silent no-trade session — which costs a day of
    trading today, and with borrowing enabled would mean the desk stays
    levered at exactly the moment it should be shedding exposure.

    So: no decisions, no targets, no PM output of any kind. The ceiling must
    still have a correct value and the held book must still be reduced.
    """
    positions = [
        _position("NVDA", qty=120.0, current_price=100.0),  # $12k
        _position("AMD", qty=80.0, current_price=100.0, avg_entry=150.0),  # $8k
    ]
    # -16% peak-to-trough demands 1.0x. The book is at 2.0x.
    ceiling = resolve_gross_ceiling(-16.0, base_x=BASE_X)
    assert ceiling.ceiling_x == 1.0, "the ceiling must not need a PM to resolve"

    outcome = apply_gross_ceiling([], positions, EQUITY, ceiling)

    assert outcome.decisions == [], "there is nothing the PM proposed"
    assert outcome.blocked == [], "there is no new exposure to block"
    # Blocking must HOLD on such a run, not merely be vacuous. The ceiling
    # the blank session resolved refuses new exposure at the execution gate
    # exactly as it would on a session the PM answered.
    engine = RiskRuleEngine(_risk_config())
    assert GROSS_EXPOSURE_RULE in [
        v.rule
        for v in engine.check(
            decision=_buy("TSLA", 5.0),
            positions=positions,
            total_value=EQUITY,
            gross_ceiling=ceiling,
        )
    ], "the blank-PM ceiling must still refuse new exposure"
    assert outcome.trims, (
        "THE FAILURE THIS TEST EXISTS FOR: a blank PM response must not leave the desk levered through a drawdown"
    )
    freed = sum(
        abs(p.market_value) * (t.allocation_pct / 100.0)
        for t in outcome.trims
        for p in positions
        if p.symbol == t.symbol
    )
    assert outcome.held_gross - freed <= outcome.ceiling_usd + 1e-6, (
        "the trim must actually bring the book under the ceiling"
    )


def test_the_de_lever_runs_in_the_preamble_before_any_agent_is_called():
    """Wiring, not just arithmetic. A correct `apply_gross_ceiling` that is
    only ever reached from inside the decision path would still leave the
    desk levered on a blank-PM run, because the decision path returns early
    when the Portfolio Manager produces nothing.

    So the de-lever must be invoked BEFORE the decision stage in both trading
    entry points. This asserts that ordering directly against the source.
    """
    import inspect
    from src.pipeline import TradingPipeline

    # `run_morning`/`run_position_review` became thin persistence wrappers
    # on 2026-09-18 (see `Database.save_session_report`, same shape
    # `run_evening`/`_run_evening_body` already used); the preamble this
    # test pins now lives in their bodies.
    for entry_point in (
        __import__("src.sessions.morning_session", fromlist=["MorningSession"]).MorningSession.run,
        __import__("src.pipeline_parts.review", fromlist=["review"])._run_position_review_body,
    ):
        source = inspect.getsource(entry_point)
        assert "_enforce_gross_ceiling" in source, f"{entry_point.__name__} must de-lever in its preamble"

    morning = inspect.getsource(
        __import__("src.sessions.morning_session", fromlist=["MorningSession"]).MorningSession.run
    )
    assert morning.index("_enforce_gross_ceiling") < morning.index("_decision_stage"), (
        "the de-lever must run BEFORE the Portfolio Manager is called, so a "
        "blank or truncated model response cannot skip it"
    )

    # The midday/close lane has its own agent (the position reviewer) and the
    # same requirement: the ladder steps on measured drawdown, and a reviewer
    # that returns nothing must not postpone the de-lever to tomorrow.
    review = inspect.getsource(__import__("src.pipeline_parts.review", fromlist=["review"])._run_position_review_body)
    assert review.index("_enforce_gross_ceiling") < review.index("position_reviewer"), (
        "the de-lever must run BEFORE the position reviewer is called, for "
        "the same reason it runs before the Portfolio Manager"
    )


def test_the_preamble_de_lever_submits_sells_with_no_pm_decision_present():
    """The pipeline method itself, exercised against an over-levered book and
    no Portfolio Manager output whatsoever."""
    from unittest.mock import MagicMock
    from src.config import CashSweepConfig
    from src.pipeline import TradingPipeline
    from src.pipeline_context import RunContext

    pipeline = build_pipeline(db=MagicMock(), broker=MagicMock())
    pipeline.config = MagicMock()
    pipeline.config.risk = _risk_config()
    pipeline.config.cash_sweep = CashSweepConfig(enabled=False)
    # A book that fell 16% from its high: the ladder demands 1.0x.
    pipeline.db.get_daily_pnl.return_value = [{"total_value": EQUITY / 0.84}]
    pipeline.broker.get_account.return_value = {
        "cash": 0.0,
        "portfolio_value": EQUITY,
        "last_equity": EQUITY,
    }
    pipeline.broker.get_positions.return_value = []
    pipeline.cash_sweeper = None
    pipeline._compute_deployable_cash = MagicMock(return_value=0.0)
    pipeline._finalize_pending_protections = MagicMock()
    pipeline._submit_protected_sell = MagicMock(
        return_value=({"id": "order-1", "symbol": "NVDA"}, {}),
    )

    ctx = RunContext(run_id="test-run", session="morning")
    ctx.positions = [_position("NVDA", qty=200.0, current_price=100.0)]  # 2.0x
    ctx.total_value = EQUITY

    orders = pipeline._enforce_gross_ceiling(ctx)

    assert orders, "an over-levered book must be reduced with no PM involved"
    assert pipeline._submit_protected_sell.called
    assert ctx.leverage["ceiling_x"] == 1.0
    assert ctx.leverage["drawdown_pct"] == pytest.approx(-16.0, abs=0.1)
    assert ctx.leverage["distance_to_forced_liquidation_pct"] is not None
    assert "delever_incomplete" not in ctx.leverage, (
        "a de-lever that actually brought the book under the ceiling must not raise the incomplete flag"
    )


def test_a_delever_that_fails_to_clear_the_ceiling_is_flagged():
    """Reporting-only check: the trim was submitted, but the post-refresh
    book is STILL over the ceiling (a failed order, a partial fill, or
    integer-share rounding can all produce this). This must be visible on
    `ctx.leverage` — which every session-result dict already copies — even
    though nothing about the order sequence changed."""
    from unittest.mock import MagicMock
    from src.config import CashSweepConfig
    from src.pipeline import TradingPipeline
    from src.pipeline_context import RunContext

    pipeline = build_pipeline(db=MagicMock(), broker=MagicMock())
    pipeline.config = MagicMock()
    pipeline.config.risk = _risk_config()
    pipeline.config.cash_sweep = CashSweepConfig(enabled=False)
    # A book that fell 16% from its high: the ladder demands 1.0x.
    pipeline.db.get_daily_pnl.return_value = [{"total_value": EQUITY / 0.84}]
    # Post-refresh the broker still reports an over-levered book (the sell
    # only partially filled) — 1.5x against a 1.0x ceiling.
    pipeline.broker.get_account.return_value = {
        "cash": 0.0,
        "portfolio_value": EQUITY,
        "last_equity": EQUITY,
    }
    pipeline.broker.get_positions.return_value = [
        _position("NVDA", qty=150.0, current_price=100.0),
    ]
    pipeline.cash_sweeper = None
    pipeline._compute_deployable_cash = MagicMock(return_value=0.0)
    pipeline._finalize_pending_protections = MagicMock()
    pipeline._submit_protected_sell = MagicMock(
        return_value=({"id": "order-1", "symbol": "NVDA"}, {}),
    )

    ctx = RunContext(run_id="test-run", session="morning")
    ctx.positions = [_position("NVDA", qty=200.0, current_price=100.0)]  # 2.0x
    ctx.total_value = EQUITY

    orders = pipeline._enforce_gross_ceiling(ctx)

    assert orders, "the de-lever must still submit its trim"
    assert ctx.leverage["gross_x"] == pytest.approx(1.5)
    assert ctx.leverage["ceiling_x"] == 1.0
    assert ctx.leverage["delever_incomplete"] is True


def test_the_ceiling_is_computed_from_account_state_alone():
    """Signature-level proof of the decoupling: resolving the ceiling takes a
    drawdown and a configured cap. There is no parameter through which a
    Portfolio Manager decision could reach it."""
    import inspect

    parameters = set(inspect.signature(resolve_gross_ceiling).parameters)
    assert parameters == {"drawdown_pct", "base_x"}


def test_peak_to_trough_is_measured_from_the_equity_curve_not_from_agents():
    curve = [10_000.0, 12_000.0, 11_000.0]
    assert peak_to_trough_pct(curve, 9_600.0) == -20.0
    assert peak_to_trough_pct(curve, 12_000.0) == 0.0
    # A new high is not a drawdown.
    assert peak_to_trough_pct(curve, 15_000.0) == 0.0
    # No history at all is honestly unknown, never a fabricated zero.
    assert peak_to_trough_pct([], None) is None


def test_an_unknown_drawdown_holds_the_standing_cap_and_trims_nothing():
    """A fresh account with no equity history genuinely has no drawdown.
    Forcing it to the deepest rung would refuse every trade on day one and
    force-liquidate a book that never fell.

    UPDATED 2026-09-18: the CEILING is still the standing cap and nothing is
    still trimmed — but the state now alerts (see
    `test_an_unmeasurable_drawdown_alerts_the_owner_instead_of_going_quiet`).
    Holding the loosest cap was never the defect; doing it silently, so a
    wiped equity curve looked identical to a book at record highs, was."""
    ceiling = resolve_gross_ceiling(None, base_x=BASE_X)
    assert ceiling.ceiling_x == BASE_X
    assert ceiling.alert_owner is True
    assert ceiling.drawdown_pct is None


# ===========================================================================
# The measurement itself — gross, the cash park, and the margin-call distance
# ===========================================================================


def test_gross_is_longs_plus_the_magnitude_of_shorts():
    positions = [
        _position("NVDA", qty=50.0, current_price=100.0),  # +$5k long
        _position("TSLA", qty=-30.0, current_price=100.0),  # -$3k short
    ]
    assert gross_exposure(positions) == pytest.approx(8_000.0)


def test_the_cash_park_is_not_exposure():
    """SGOV is parked cash. Counting it would consume the entire leverage
    allowance doing nothing."""
    positions = [
        _position("NVDA", qty=50.0, current_price=100.0),
        _position("SGOV", qty=100.0, current_price=100.0, sector="Cash"),
    ]
    assert gross_exposure(positions, cash_park_symbol="SGOV") == pytest.approx(5_000.0)
    # And it is never sold to satisfy the ceiling.
    ceiling = resolve_gross_ceiling(-30.0, base_x=BASE_X)  # 0.5x = $5k
    outcome = apply_gross_ceiling(
        [],
        positions,
        EQUITY,
        ceiling,
        cash_park_symbol="SGOV",
    )
    assert all(t.symbol != "SGOV" for t in outcome.trims)


def test_the_park_symbol_comes_from_config_not_a_hardcoded_ticker():
    positions = [_position("BIL", qty=100.0, current_price=100.0, sector="Cash")]
    assert gross_exposure(positions, cash_park_symbol="BIL") == 0.0
    assert gross_exposure(positions, cash_park_symbol="SGOV") == pytest.approx(10_000.0)


def test_distance_to_forced_liquidation_reproduces_the_ratified_figures():
    """The §11.2 spec entry publishes ~33% at 2.0x and ~55% at 1.5x. These
    are reproduced from the maintenance-margin arithmetic, not restated."""
    equity = 9_825.0
    assert distance_to_forced_liquidation_pct(2.0 * equity, equity) == pytest.approx(33.3, abs=0.1)
    assert distance_to_forced_liquidation_pct(1.5 * equity, equity) == pytest.approx(55.6, abs=0.1)
    # An unlevered book cannot be margin-called.
    assert distance_to_forced_liquidation_pct(equity, equity) == 100.0
    assert distance_to_forced_liquidation_pct(0.0, equity) == 100.0


# ===========================================================================
# The EXECUTION gate — the same ceiling, enforced again where orders leave
# ===========================================================================


def test_the_execution_gate_hard_blocks_a_breach():
    engine = RiskRuleEngine(_risk_config())
    positions = [_position("NVDA", qty=190.0, current_price=100.0)]  # $19k
    decision = _buy("AMD", 30.0)  # +$3k -> 2.2x

    violations = engine.check(
        decision=decision,
        positions=positions,
        total_value=EQUITY,
        gross_ceiling=resolve_gross_ceiling(0.0, base_x=BASE_X),
    )

    rules = [v.rule for v in violations]
    assert GROSS_EXPOSURE_RULE in rules
    message = next(v.message for v in violations if v.rule == GROSS_EXPOSURE_RULE)
    # Operator-readable, and it names the rule and both numbers.
    assert "gross exposure" in message
    assert "2.00x ceiling" in message


def test_the_execution_gate_moves_with_the_ladder():
    """The same order passes at 2.0x and is blocked at 1.0x. If this test
    ever passes with identical outcomes at both rungs, the execution gate is
    not reading the ladder."""
    engine = RiskRuleEngine(_risk_config())
    positions = [_position("NVDA", qty=90.0, current_price=100.0)]  # $9k
    order = _buy("AMD", 20.0)  # +$2k -> 1.1x

    undrawn = engine.check(
        decision=order,
        positions=positions,
        total_value=EQUITY,
        gross_ceiling=resolve_gross_ceiling(0.0, base_x=BASE_X),
    )
    drawn = engine.check(
        decision=order,
        positions=positions,
        total_value=EQUITY,
        gross_ceiling=resolve_gross_ceiling(-16.0, base_x=BASE_X),
    )

    assert GROSS_EXPOSURE_RULE not in [v.rule for v in undrawn]
    assert GROSS_EXPOSURE_RULE in [v.rule for v in drawn]


def test_the_execution_gate_is_a_hard_block_not_an_advisory():
    from src.pipeline import HARD_BLOCK_RULES

    assert GROSS_EXPOSURE_RULE in HARD_BLOCK_RULES


def test_the_execution_gate_falls_back_to_the_configured_cap():
    """A caller that forgets to pass the ladder still gets A ceiling — never
    none."""
    engine = RiskRuleEngine(_risk_config(max_gross_exposure_x=1.0))
    positions = [_position("NVDA", qty=100.0, current_price=100.0)]  # $10k = 1.0x
    violations = engine.check(
        decision=_buy("AMD", 20.0),
        positions=positions,
        total_value=EQUITY,
    )
    assert GROSS_EXPOSURE_RULE in [v.rule for v in violations]


def test_a_short_consumes_the_ceiling_exactly_like_a_long():
    """Gross is direction-agnostic. A short is not free leverage."""
    engine = RiskRuleEngine(_risk_config(max_position_pct=100))
    positions = [_position("NVDA", qty=190.0, current_price=100.0)]
    short = TradeDecision(
        action="SHORT",
        symbol="TSLA",
        allocation_pct=30.0,
        entry_price=100.0,
        stop_loss=110.0,
        take_profit=80.0,
        reasoning="breakdown",
    )
    violations = engine.check(
        decision=short,
        positions=positions,
        total_value=EQUITY,
        gross_ceiling=resolve_gross_ceiling(0.0, base_x=BASE_X),
    )
    assert GROSS_EXPOSURE_RULE in [v.rule for v in violations]


# ===========================================================================
# Refusals must read as English, naming the rule that fired
# ===========================================================================


def test_every_refusal_says_which_rule_fired_in_plain_language():
    positions = [_position("NVDA", qty=200.0, current_price=100.0)]
    ceiling = resolve_gross_ceiling(-16.0, base_x=BASE_X)
    outcome = apply_gross_ceiling([_buy("AMD", 10.0)], positions, EQUITY, ceiling)

    assert outcome.notes, "a refusal with no explanation is not a refusal"
    for note in outcome.notes:
        assert GROSS_EXPOSURE_RULE in note, "every note names the rule by name"
        assert "below its equity high" in note, "and says, in words, why the ceiling is where it is"


def test_the_de_lever_order_explains_itself_to_the_operator():
    positions = [_position("NVDA", qty=200.0, current_price=100.0)]
    ceiling = resolve_gross_ceiling(-16.0, base_x=BASE_X)
    trim = apply_gross_ceiling([], positions, EQUITY, ceiling).trims[0]
    assert "New exposure was blocked first" in trim.reasoning
    assert "ceiling" in trim.reasoning


# ===========================================================================
# Fail-closed on a broken broker snapshot
# ===========================================================================


def test_a_nan_market_value_blocks_new_exposure_and_forbids_trimming():
    """`NaN > ceiling` is False, so an unguarded comparison would switch the
    ceiling OFF on exactly the broken-snapshot day it matters most. And
    trimming on a snapshot we cannot read is destructive — block, do not
    sell."""
    broken = _position("NVDA", qty=100.0, current_price=100.0)
    object.__setattr__(broken, "market_value", float("nan"))
    decision = _buy("AMD", 10.0)

    outcome = apply_gross_ceiling(
        [decision],
        [broken],
        EQUITY,
        resolve_gross_ceiling(0.0, base_x=BASE_X),
    )

    assert decision.allocation_pct == 0.0
    assert outcome.trims == []
    assert outcome.measurable is False


def test_an_unusable_equity_figure_refuses_every_new_position():
    decision = _buy("AMD", 10.0)
    outcome = apply_gross_ceiling(
        [decision],
        [],
        0.0,
        resolve_gross_ceiling(0.0, base_x=BASE_X),
    )
    assert decision.allocation_pct == 0.0
    assert outcome.blocked == ["AMD"]
    assert outcome.trims == []


# ===========================================================================
# THE GATE, PART 3b — exactly once ACROSS THE CHAIN, not just per call.
#
# The tests above prove one call is idempotent. These prove the two gates the
# ceiling is actually enforced at — the SIZING gate (PortfolioConstructor,
# which shrinks) and the EXECUTION gate (RiskRuleEngine, which blocks) — do
# not compound when run back-to-back on the same order, and that only ONE
# caller in the whole codebase is allowed to author a trim.
# ===========================================================================


def test_the_sizing_gate_and_the_execution_gate_do_not_compound():
    """The real chain, in order. The sizing gate shrinks an oversized entry
    to exactly the headroom the ceiling allows; the execution gate then sees
    that same order and must let it through untouched.

    Two failures are guarded here at once:
      * the execution gate REJECTING what the sizing gate just made fit —
        the ceiling would produce no-trade sessions instead of smaller ones;
      * either gate shrinking a second time — the book would de-lever twice
        as hard as the ratified rung.
    """
    positions = [_position("NVDA", qty=100.0, current_price=100.0)]  # $10k
    ceiling = resolve_gross_ceiling(-10.0, base_x=BASE_X)  # 1.5x = $15k
    assert ceiling.ceiling_x == 1.5
    decision = _buy("AMD", 100.0)  # wants $10k

    # --- gate 1: sizing. `emit_trims=False` is exactly how the constructor
    # calls it — shrinking its own proposal is its job, trimming is not.
    apply_gross_ceiling(
        [decision],
        positions,
        EQUITY,
        ceiling,
        emit_trims=False,
    )
    after_sizing = decision.allocation_pct
    assert after_sizing == pytest.approx(50.0), "shrunk to the $5k of headroom"

    # --- gate 2: execution. Same ceiling, same order, same book.
    engine = RiskRuleEngine(_risk_config())
    violations = engine.check(
        decision=decision,
        positions=positions,
        total_value=EQUITY,
        gross_ceiling=ceiling,
    )
    assert GROSS_EXPOSURE_RULE not in [v.rule for v in violations], (
        "the execution gate must not reject an order the sizing gate already fitted under the very same ceiling"
    )
    assert decision.allocation_pct == after_sizing, (
        "the execution gate BLOCKS; it must never re-size, or the two gates would compound into a double de-lever"
    )

    # --- and the sizing gate run again is still a no-op.
    apply_gross_ceiling(
        [decision],
        positions,
        EQUITY,
        ceiling,
        emit_trims=False,
    )
    assert decision.allocation_pct == after_sizing


def test_trimming_the_held_book_has_exactly_one_owner():
    """Structural, because convention does not hold and mechanism does.

    Two callers authoring de-lever orders from the same ceiling in one
    session is the compounding failure that arithmetic idempotence cannot
    catch — each would be individually correct and the book would be sold
    down twice. So: across all of `src/`, exactly ONE call to
    `apply_gross_ceiling` may leave `emit_trims` at its default of True, and
    it must be the run preamble, now in `pipeline_delever.py` (split step 3),
    which runs before any agent
    and therefore keeps working when the Portfolio Manager returns nothing.
    """
    import ast
    from pathlib import Path

    src = Path(__file__).parent.parent / "src"
    trim_owners: list[str] = []
    sizing_callers: list[str] = []
    for path in sorted(src.rglob("*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
            if name != "apply_gross_ceiling":
                continue
            emit = next(
                (kw.value for kw in node.keywords if kw.arg == "emit_trims"),
                None,
            )
            disabled = isinstance(emit, ast.Constant) and emit.value is False
            (sizing_callers if disabled else trim_owners).append(path.relative_to(src).as_posix())

    # Item 112 kept this EXACT: one call site, not one module. The conviction
    # de-lever does not call `apply_gross_ceiling` itself — it delegates to
    # `_enforce_gross_ceiling` with a cut order — precisely so a second owner
    # cannot appear. A duplicate entry here means one did.
    assert trim_owners == ["delever/enforce.py"], f"exactly one caller may author de-lever orders; found {trim_owners}"
    assert sizing_callers == ["portfolio_constructor/__init__.py"], (
        f"the sizing gate must pass emit_trims=False; found {sizing_callers}"
    )


# ===========================================================================
# The operator's view. With the ladder deliberately absent from Mission
# Control (`src/api/` may never import the risk stack — see
# tests/test_api_safety.py), the session alert is the ONLY place a human sees
# which rung is in force. It was shipped untested; these pin it.
# ===========================================================================


def test_the_session_alert_reports_the_rung_in_force():
    from src.notifier import _append_leverage_line

    lines: list[str] = []
    _append_leverage_line(
        lines,
        {
            "leverage": {
                "gross_x": 1.8,
                "ceiling_x": 1.5,
                "base_ceiling_x": 2.0,
                "drawdown_pct": -12.0,
                "distance_to_forced_liquidation_pct": 55.6,
                "alert_owner": False,
            }
        },
    )

    assert len(lines) == 1
    line = lines[0]
    assert "1.80x" in line and "1.50x" in line
    assert "56% fall to a margin call" in line  # 55.6 rendered to 0dp
    assert "12.0% below the equity high" in line
    # The de-levered STATE must be carried by the word, never by a colour
    # or an icon alone.
    assert "DE-LEVERED" in line


def test_the_margin_call_distance_label_never_reads_as_a_stop_or_gap_metric():
    """`distance_to_forced_liquidation_pct` is the BROKER'S margin-call
    distance and nothing else — it must never be worded as if it were a
    per-position stop or a gap-survival cushion, and it must never be a
    sizing/gating input (that is enforced elsewhere by the ladder using
    `drawdown_pct`, not this field). This is a mirror guard: if the wording
    in `_append_leverage_line` ever drifts, this test catches it before an
    owner-facing message does.
    """
    from src.notifier import _append_leverage_line

    lines: list[str] = []
    _append_leverage_line(
        lines,
        {
            "leverage": {
                "gross_x": 1.8,
                "ceiling_x": 1.5,
                "base_ceiling_x": 2.0,
                "drawdown_pct": -12.0,
                "distance_to_forced_liquidation_pct": 55.6,
                "alert_owner": False,
            }
        },
    )

    assert len(lines) == 1
    line = lines[0]
    # The correct wording: the broker's margin-call distance.
    assert "fall to a margin call" in line
    # Banned mislabels this exact number must never carry.
    for banned in (
        "gap survival",
        "gap-survival",
        "distance to stop",
        "distance-to-stop",
        "stop-out",
        "stop out",
    ):
        assert banned not in line.lower(), f"{banned!r} mislabels the broker margin-call distance in: {line!r}"
    assert "DE-LEVERED" in line


def test_the_alert_stays_silent_when_nothing_was_measured():
    """An omitted line is honest; an invented '1.0x' is not."""
    from src.notifier import _append_leverage_line

    for result in ({}, {"leverage": {}}, {"leverage": {"gross_x": None, "ceiling_x": 1.5}}):
        lines: list[str] = []
        _append_leverage_line(lines, result)
        assert lines == []


def test_the_deepest_rung_raises_a_separate_owner_alert():
    from src.notifier import _append_leverage_line

    lines: list[str] = []
    _append_leverage_line(
        lines,
        {
            "leverage": {
                "gross_x": 0.4,
                "ceiling_x": 0.5,
                "base_ceiling_x": 2.0,
                "drawdown_pct": -24.0,
                "distance_to_forced_liquidation_pct": 100.0,
                "alert_owner": True,
            }
        },
    )

    assert len(lines) == 2, "the -20% rung gets its own line, not a footnote"
    alert = lines[1]
    # 2026-09-30: the literal "-20%" and the "lowest rung" claim are GONE
    # from this line. The owner alert now fires at the sourced -10% MiFID
    # depreciation threshold, so a hardcoded -20% was false for any book
    # between the two. The line is assembled from the measured drawdown,
    # the alert level and the resolved rung instead.
    assert "DRAWDOWN" in alert and "24.0%" in alert
    assert "-20%" not in alert
    # The cap must be READ from the resolved ceiling, never hardcoded — a
    # literal would go stale the day the ratified ladder changes.
    assert "0.50x equity" in alert
    # And the claim must be exact: this book is UNDER its cap, so it is not
    # true that every new position is refused.
    assert "refused once the book reaches it" in alert


def test_an_unmeasurable_drawdown_alert_never_claims_a_measured_number():
    """`alert_owner` is not only the -20% rung any more, so the owner-facing
    line is chosen from the RUNG. Printing 'DRAWDOWN PAST -20%' for a state
    in which nothing was measured would tell him something specific and
    false about his own book."""
    from src.notifier import _append_leverage_line

    for rung in ("unknown", "bad_read"):
        lines: list[str] = []
        _append_leverage_line(
            lines,
            {
                "leverage": {
                    "gross_x": 1.1,
                    "ceiling_x": 2.0,
                    "base_ceiling_x": 2.0,
                    "drawdown_pct": None,
                    "rung": rung,
                    "alert_owner": True,
                }
            },
        )
        assert len(lines) == 2
        alert = lines[1]
        assert "UNMEASURABLE" in alert
        assert "-20%" not in alert, "nothing was measured; claim no number"

    # And the state must not be readable as "we are fine": say so outright.
    lines = []
    _append_leverage_line(
        lines,
        {
            "leverage": {
                "gross_x": 1.1,
                "ceiling_x": 2.0,
                "base_ceiling_x": 2.0,
                "drawdown_pct": None,
                "rung": "unknown",
                "alert_owner": True,
            }
        },
    )
    assert "NOT a book at record highs" in lines[1]


def test_a_failed_delever_gets_its_own_plain_language_alert():
    """A de-lever that runs and still leaves the book over the ceiling is
    unreportable (it only reaches the log) unless this line exists. Plain
    words for a non-developer owner: what was attempted, that it fell short,
    and the real measured number — never an internal name, a status code,
    or an invented figure."""
    from src.notifier import _append_leverage_line

    lines: list[str] = []
    _append_leverage_line(
        lines,
        {
            "leverage": {
                "gross_x": 1.5,
                "ceiling_x": 1.0,
                "base_ceiling_x": 2.0,
                "drawdown_pct": -16.0,
                "distance_to_forced_liquidation_pct": 40.0,
                "alert_owner": False,
                "delever_incomplete": True,
            }
        },
    )

    assert len(lines) == 2, "the failed de-lever gets its own line"
    alert = lines[1]
    assert "still over" in alert
    assert "1.50x" in alert and "1.00x" in alert
    # No internal names, status codes, or component jargon in an
    # owner-facing message.
    for banned in ("FORCE_DELEVER", "ceiling_x", "gross_x", "§11.2"):
        assert banned not in alert


def test_no_failed_delever_alert_when_the_book_cleared_the_ceiling():
    from src.notifier import _append_leverage_line

    lines: list[str] = []
    _append_leverage_line(
        lines,
        {
            "leverage": {
                "gross_x": 0.9,
                "ceiling_x": 1.0,
                "base_ceiling_x": 2.0,
                "drawdown_pct": -16.0,
                "distance_to_forced_liquidation_pct": 40.0,
                "alert_owner": False,
                "delever_incomplete": False,
            }
        },
    )

    assert len(lines) == 1, "no incomplete-delever line when the flag is unset"


# ===========================================================================
# GUARD 2 (2026-09-02 operational safety guard) — a non-finite equity read
# must never become the high-water mark, and must never silently pass a
# threshold comparison.
#
# The documented failure mode (another system's source): an incremental
# "if new > hwm: hwm = new" tracker lets a single NaN reading poison `hwm`
# permanently, because every later comparison against NaN is False. This
# codebase's `peak_to_trough_pct` is structurally immune to THAT shape of
# bug — the peak is a fresh max() over a filtered list on every call, never
# a variable carried between calls — but the tests below pin that property
# explicitly rather than trusting the architecture, and close two real gaps
# found while verifying it: (a) a dropped non-finite reading was silent, and
# (b) a non-finite CURRENT equity read fell through to the same "unknown
# drawdown" branch a genuinely fresh account uses, silently holding the
# loosest (standing) cap instead of halting new risk.
# ===========================================================================


def test_peak_to_trough_pct_never_lets_nan_win_the_peak():
    """The literal ask: a NaN sits among the candidates for 'peak' (as if
    one day's stored total_value were corrupted). The ladder must still
    function, using the REAL data, not the corrupted entry."""
    # The corrupted entry (float('nan')) stands where a legitimate but
    # UNREMARKABLE reading would — 150_000 is the genuine peak.
    history = [100_000.0, float("nan"), 150_000.0, 130_000.0]
    drawdown = peak_to_trough_pct(history, 120_000.0)
    assert drawdown == -20.0, "peak must be 150_000 (max of the FINITE readings), not NaN"

    ceiling = resolve_gross_ceiling(drawdown, base_x=BASE_X)
    assert ceiling.ceiling_x == 0.5
    assert ceiling.rung == "-20%"
    assert ceiling.alert_owner is True


def test_peak_to_trough_pct_nan_can_never_become_the_peak_directly():
    """Even when the NaN sits at the position that WOULD win a naive
    max() (last, or largest-looking), it still cannot win — because it is
    filtered out before max() ever runs, not compared away."""
    history = [100_000.0, float("nan")]
    # If the NaN could somehow "win" as if it were +inf, this would not be
    # None; if it silently sorted as the smallest value, the peak would
    # wrongly be 100_000 and this would compute a spurious 0.0% drawdown
    # instead of reflecting that the true peak is unknown/unverifiable
    # here. It must fall back to the one finite candidate, 100_000.
    drawdown = peak_to_trough_pct(history, 100_000.0)
    assert drawdown == 0.0


def test_peak_to_trough_pct_inf_can_never_become_the_peak_directly():
    """+inf is the sharper version of the same property: unlike NaN, `inf
    > 0` is TRUE, so a filter that only excluded non-positive values (and
    dropped the explicit `math.isfinite` check) would let +inf sail into
    `values` and WIN max() — producing peak=inf and therefore a NaN
    drawdown (`(x - inf) / inf`) silently propagated back to the caller.
    That is a mutation this suite must catch, so it is pinned directly."""
    history = [100_000.0, float("inf")]
    drawdown = peak_to_trough_pct(history, 100_000.0)
    assert drawdown == 0.0
    assert math.isfinite(drawdown)


def test_peak_to_trough_pct_logs_when_dropping_non_finite_readings(caplog):
    """'Never silently passes': a dropped NaN/inf must leave a trace, even
    though the return value is correct either way — this is the guard
    against the reading vanishing with no evidence it ever existed."""
    import logging

    with caplog.at_level(logging.WARNING, logger="src.risk.rules"):
        peak_to_trough_pct([100_000.0, float("nan"), float("inf")], 90_000.0)
    assert any("dropped" in r.message and "non-finite" in r.message for r in caplog.records)


def test_peak_to_trough_pct_no_warning_when_all_readings_are_clean(caplog):
    """Regression guard: the new logging must not fire on the ordinary
    (no corruption) path — that would turn a silent, correct case noisy."""
    import logging

    with caplog.at_level(logging.WARNING, logger="src.risk.rules"):
        peak_to_trough_pct([100_000.0, 110_000.0], 105_000.0)
    assert not any("dropped" in r.message for r in caplog.records)


def test_peak_to_trough_pct_all_history_corrupted_is_unmeasurable_not_zero(caplog):
    """CLOSES a residual this file used to pin as accepted (Guard 3,
    2026-09-18). If EVERY historical reading is non-finite, the function
    used to fall back to treating today as its own peak and return a
    confident 0.0% — indistinguishable, in the ceiling and in every
    owner-facing line, from a book at record highs. It is now UNMEASURABLE:
    losing the curve and making new highs are opposite states."""
    import logging

    with caplog.at_level(logging.WARNING, logger="src.risk.rules"):
        drawdown = peak_to_trough_pct([float("nan")] * 5, 95_000.0)
    assert drawdown is None
    assert any("dropped" in r.message for r in caplog.records)
    assert any("UNMEASURABLE" in r.message for r in caplog.records)


def test_peak_to_trough_pct_empty_history_is_unmeasurable_not_zero(caplog):
    """The safety half of Guard 3. An empty `daily_pnl` table plus a
    perfectly good current equity reading must NOT resolve to 0.0%, because
    `resolve_gross_ceiling` reads 0.0% as the loosest standing cap — so a
    data-loss event would silently disable the desk's only automatic
    seller while reporting a healthy book."""
    import logging

    with caplog.at_level(logging.WARNING, logger="src.risk.rules"):
        assert peak_to_trough_pct([], 95_000.0) is None
    assert any("UNMEASURABLE" in r.message for r in caplog.records)
    # One real prior reading is enough to measure against; no invented
    # minimum number of days stands between "measured" and "unmeasured".
    assert peak_to_trough_pct([100_000.0], 95_000.0) == -5.0


def test_an_unmeasurable_drawdown_alerts_the_owner_instead_of_going_quiet():
    """The ceiling deliberately stays at the standing cap (tightening it
    would force-liquidate a genuinely fresh account that never fell), but
    the state is no longer silent — the whole defect was that an
    unmeasurable book and a book at record highs produced identical,
    unalarming output."""
    ceiling = resolve_gross_ceiling(None, base_x=BASE_X)
    assert ceiling.rung == "unknown"
    assert ceiling.ceiling_x == BASE_X, "an unmeasured book is not trimmed"
    assert ceiling.drawdown_pct is None
    assert ceiling.alert_owner is True
    # A book genuinely at its high must NOT raise the same alarm.
    assert resolve_gross_ceiling(0.0, base_x=BASE_X).alert_owner is False


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_resolve_gross_ceiling_non_finite_drawdown_never_crosses_a_threshold(bad):
    """A NaN/inf drawdown_pct must be intercepted before the `drawdown <=
    threshold` loop, never reach it and silently evaluate False on every
    rung (which would leave the ceiling at the loosest, standing value
    while LOOKING like a real comparison happened)."""
    ceiling = resolve_gross_ceiling(bad, base_x=BASE_X)
    assert ceiling.rung == "unknown"
    assert ceiling.ceiling_x == BASE_X
    assert ceiling.drawdown_pct is None, "a non-finite input must not be echoed back as a number"


def test_insert_daily_pnl_rejects_a_nan_total_value():
    """DB-level finding, not something this guard adds: SQLite's Python
    driver silently binds float('nan') as SQL NULL, so a NaN total_value
    trips the `total_value REAL NOT NULL` constraint and insert_daily_pnl
    RAISES rather than persisting corrupted data. Pinned here because it
    is a second, independent reason a NaN can never become the stored
    high-water mark in production — belt and suspenders with
    peak_to_trough_pct's own filtering."""
    from src.storage.db import Database

    db = Database(":memory:")
    db.initialize()
    with pytest.raises(Exception):
        db.insert_daily_pnl("2026-09-02", float("nan"), 0.0, 0.0)


# --- TradingPipeline._resolve_gross_ceiling: the bad-current-read fix ------


def _pipeline_for_ceiling(**risk_overrides):
    from unittest.mock import MagicMock
    from src.pipeline import TradingPipeline
    from types import SimpleNamespace

    p = build_pipeline(db=MagicMock())
    p.config = SimpleNamespace(risk=_risk_config(**risk_overrides))
    p.db.get_daily_pnl.return_value = []
    return p


def _ctx_with_equity(total_value, *, positions=None, recent_performance=None):
    from src.pipeline_context import RunContext

    ctx = RunContext.start("morning")
    ctx.total_value = total_value
    ctx.positions = positions if positions is not None else []
    ctx.recent_performance = recent_performance or {}
    return ctx


@pytest.mark.parametrize("bad_equity", [float("nan"), float("inf"), float("-inf")])
def test_resolve_gross_ceiling_forces_floor_and_alerts_on_bad_current_equity(bad_equity):
    """The actual fix: a non-finite CURRENT equity read (Alpaca has been
    observed to return NaN portfolio_value on market-open glitches) must
    NOT fall through to the standing (loosest) cap the way a genuinely
    unmeasurable/fresh-account drawdown does. It must halt new risk (the
    floor rung) and alert the owner — never assume zero drawdown."""
    pipeline = _pipeline_for_ceiling()
    ctx = _ctx_with_equity(bad_equity)

    ceiling = pipeline._resolve_gross_ceiling(ctx)

    assert ceiling.ceiling_x == GROSS_LADDER[-1][1], "must be the ladder's own floor rung, not hardcoded"
    assert ceiling.alert_owner is True
    assert ceiling.rung == "bad_read"
    assert ceiling.drawdown_pct is None


def test_resolve_gross_ceiling_bad_read_floor_is_never_looser_than_base(bad_equity=float("nan")):
    """If an operator ever configured a base ceiling BELOW the ladder's
    floor rung, the bad-read fallback must not raise it back up."""
    pipeline = _pipeline_for_ceiling(max_gross_exposure_x=0.3)
    ctx = _ctx_with_equity(bad_equity)

    ceiling = pipeline._resolve_gross_ceiling(ctx)

    # Asserted by RUNG, not just the inequality: base_x=0.3 happens to sit
    # below the floor rung too, so an inequality alone can't tell "the
    # bad-read guard fired" apart from "it silently fell back to the
    # standing cap, which happened to already be tight enough" — the
    # exact silent-pass-through this guard exists to rule out.
    assert ceiling.rung == "bad_read"
    assert ceiling.ceiling_x <= 0.3


def test_resolve_gross_ceiling_fresh_account_is_not_punished_like_a_bad_read():
    """Regression guard for the DELIBERATE prior behaviour this guard must
    not break: a genuinely fresh account (no daily_pnl history yet) with a
    perfectly good current equity read is NOT a bad read, and must still
    resolve to the standing cap and trim nothing — exactly as
    resolve_gross_ceiling's own docstring requires.

    UPDATED 2026-09-18: the two states are still distinguished by their
    CEILING (standing cap here, floor rung on a bad read), which is the
    distinction this guard was written to protect. They are no longer
    distinguished by SILENCE: an empty equity curve now alerts too, because
    the desk cannot tell a fresh account from a wiped one, and one of those
    two means the only automatic seller cannot fire."""
    pipeline = _pipeline_for_ceiling()
    ctx = _ctx_with_equity(100_000.0)  # good read, empty history via the mock db

    ceiling = pipeline._resolve_gross_ceiling(ctx)

    assert ceiling.rung == "unknown"
    assert ceiling.rung != "bad_read"
    assert ceiling.ceiling_x == BASE_X
    assert ceiling.alert_owner is True


def test_resolve_gross_ceiling_normal_drawdown_path_still_works():
    """Regression guard: a genuinely finite equity read with real history
    still walks the ordinary ladder path (unaffected by the new branch)."""
    pipeline = _pipeline_for_ceiling()
    pipeline.db.get_daily_pnl.return_value = [
        {"total_value": 100_000.0},
        {"total_value": 150_000.0},
    ]
    ctx = _ctx_with_equity(120_000.0)

    ceiling = pipeline._resolve_gross_ceiling(ctx)

    assert ceiling.rung != "bad_read"
    assert ceiling.drawdown_pct == -20.0
    assert ceiling.ceiling_x == 0.5


# ===========================================================================
# THE GATE, PART 6 — the ladder is what BINDS at execution, not settled cash.
#
# Everything above proves the ceiling steps and that the sizing/execution
# GATES enforce it. None of it reached the last gate of all: the BUY submit
# loop in `ExecutionStage`, which clamped every entry against the broker's
# RAW CASH figure and was gated on nothing. Raw cash is at most
# `equity - gross`, so that clamp held gross under 1.0x STRUCTURALLY and the
# ceiling could never bind on the long side — `allow_margin: true` shipped
# on 2026-09-02 and changed nothing a long could do.
#
# The clamp still exists (with margin on, the broker ACCEPTS a buy beyond
# cash, so this loop is the last quantitative bound before the order leaves).
# What it clamps against is now the ladder. These tests are the owner's gate
# on that swap: the rung binds at EVERY step including the floor, a deeper
# rung deploys LESS than the settled cash alone would have allowed, one order
# cannot drain the session, a short is unchanged, and no degraded read ever
# buys more room than a good one.
# ===========================================================================

from unittest.mock import MagicMock  # noqa: E402

from src.models import PortfolioDecision, ReasoningChain  # noqa: E402
from src.pipeline_context import RunContext  # noqa: E402
from src.pipeline_stages import (  # noqa: E402
    ExecutionStage,
    _entry_deployment_budget,
    _single_name_execution_cap,
)
from src.risk.rules import GrossCeiling  # noqa: E402

EXEC_PRICE = 100.0


def _execution_pipeline(
    *, cash, equity, ceiling_x=BASE_X, rung="none", allow_margin=True, max_position_pct=100.0, park="SGOV", positions=()
):
    """A pipeline stub whose §11.2 ceiling is REAL and whose sizing is
    deterministic: whole shares at $100, so every notional below is a plain
    multiple of the price and nothing hides in a rounding."""
    pipeline = MagicMock()
    pipeline.broker.get_latest_price.return_value = EXEC_PRICE
    pipeline.broker.get_latest_quote.return_value = {"bid_price": EXEC_PRICE, "ask_price": EXEC_PRICE}
    pipeline.broker.submit_order.return_value = {"id": "ord", "status": "accepted"}
    pipeline.broker.get_shortability.return_value = {
        "shortable": True,
        "easy_to_borrow": True,
    }
    pipeline._order_accepted.return_value = True
    pipeline._format_qty = lambda q: str(q)
    # The submit loop re-reads the broker before the BUY phase and adopts
    # whatever comes back, so the held book has to come through HERE — a
    # ctx-only book would be discarded and the headroom measured against an
    # empty one.
    pipeline._refresh_account_state.return_value = (
        {"cash": cash, "portfolio_value": equity},
        list(positions),
        {},
    )
    pipeline._resolve_gross_ceiling = lambda ctx: GrossCeiling(
        ceiling_x=ceiling_x,
        base_x=BASE_X,
        drawdown_pct=None,
        alert_owner=False,
        rung=rung,
        reason="test rung",
    )
    pipeline._sweep_symbol = lambda: park
    pipeline.config = MagicMock()
    pipeline.config.risk.allow_margin = allow_margin
    pipeline.config.risk.max_position_pct = max_position_pct
    # This file is about the ceiling, not risk sizing. The owner's minimum
    # risk per position (owner rule 2026-08-27) is ON at 0.5%: `_entry`'s
    # $6 stop (6%) makes the smallest remnant below ($1,000) risk 0.60% of
    # $10,000, and a 10% risk budget never binds before the ceiling (the
    # largest order, $10,000, risks 6%).
    pipeline.config.risk.min_position_risk_pct = 0.5
    pipeline.config.risk.max_position_risk_pct = 10.0
    pipeline.config.execution.fractional_enabled = False  # whole shares
    return pipeline


def _entry(symbol="NVDA", alloc=100.0) -> TradeDecision:
    """An entry that asks for the whole book, so the BUDGET is what decides
    the size. The stop sits $6 away: wide enough that every clamped order in
    this file still clears the owner's 0.5% minimum risk per position, and
    the 10% risk budget set in `_execution_pipeline` cannot be what binds —
    this file is about the ceiling, not about risk sizing."""
    return TradeDecision(
        action="BUY",
        symbol=symbol,
        allocation_pct=alloc,
        entry_price=EXEC_PRICE,
        stop_loss=EXEC_PRICE - 6.0,
        take_profit=EXEC_PRICE + 10.0,
        reasoning="ladder execution test",
    )


def _exec_ctx(decisions, *, cash, equity, positions=()) -> RunContext:
    ctx = RunContext.start("morning")
    ctx.cash = cash
    ctx.total_value = equity
    ctx.last_equity = equity
    ctx.positions = list(positions)
    ctx.decision_id = "run-x-dec-ladder"
    ctx.symbols_bars = {}  # no bars -> no ATR stop floor
    ctx.portfolio_decision = PortfolioDecision(
        reasoning_chain=ReasoningChain(
            macro_filter="m",
            news_check="n",
            earnings_check="e",
            signal_conflicts="s",
            sizing_logic="z",
            portfolio_balance="b",
            cash_target="c",
        ),
        decisions=decisions,
        portfolio_view="t",
    )
    return ctx


def _submitted_notional(pipeline) -> float:
    return sum(call.kwargs["qty"] * EXEC_PRICE for call in pipeline.broker.submit_order.call_args_list)


def test_a_long_may_now_borrow_up_to_the_ladder_ceiling():
    """THE DEFECT, stated as behaviour. $500 of settled cash, a $4,000 book
    on $10,000 of equity, and the standing 2.0x rung: the long goes out at
    the size the ladder allows, not at the size the cash allows. Before the
    swap this order was cut to five shares."""
    held = [_position(symbol="MSFT", qty=40, current_price=100.0)]  # $4,000
    pipeline = _execution_pipeline(cash=500.0, equity=10_000.0, positions=held)
    ctx = _exec_ctx([_entry()], cash=500.0, equity=10_000.0, positions=held)

    ExecutionStage(pipeline=pipeline).run(ctx)

    assert _submitted_notional(pipeline) == 10_000.0
    assert ctx.execution_skips == []


@pytest.mark.parametrize("ceiling_x", [2.0, 1.5, 1.0, 0.5])
def test_the_deployment_budget_steps_with_every_rung_including_the_floor(ceiling_x):
    """The owner's condition, at the last gate: the ladder must actually
    step. A batch big enough to exhaust any rung is offered at each rung in
    turn; the session deploys EXACTLY the headroom that rung leaves and not
    a share more, floor rung included."""
    equity, held_gross = 10_000.0, 4_000.0
    held = [_position(symbol="MSFT", qty=40, current_price=100.0)]
    pipeline = _execution_pipeline(
        cash=500.0,
        equity=equity,
        ceiling_x=ceiling_x,
        positions=held,
    )
    ctx = _exec_ctx(
        [_entry("NVDA"), _entry("AMD"), _entry("AAPL")],
        cash=500.0,
        equity=equity,
        positions=held,
    )

    ExecutionStage(pipeline=pipeline).run(ctx)

    assert _submitted_notional(pipeline) == ceiling_x * equity - held_gross


def test_a_deeper_rung_deploys_less_than_settled_cash_alone_would_have():
    """The property the whole change exists for, and the one the old clamp
    could never have: a drawdown must be able to REDUCE deployment below
    what the cash on hand would have bought. $6,000 sits in the account and
    the floor rung refuses every dollar of it, because the book is already
    over that rung."""
    equity = 10_000.0
    held = [_position(symbol="MSFT", qty=55, current_price=100.0)]  # $5,500
    pipeline = _execution_pipeline(
        cash=6_000.0,
        equity=equity,
        ceiling_x=0.5,
        rung="-20%",
        positions=held,
    )
    ctx = _exec_ctx([_entry()], cash=6_000.0, equity=equity, positions=held)

    ExecutionStage(pipeline=pipeline).run(ctx)

    pipeline.broker.submit_order.assert_not_called()
    assert [s["reason"] for s in ctx.execution_skips] == ["insufficient_cash"]


def test_no_single_order_can_consume_the_whole_session():
    """The budget is a POOL. Without a per-order bound the first entry could
    draw the entire session's headroom, which is a concentration decision
    nobody made. `max_position_pct` is re-applied to the size EXECUTION
    chose — same idiom as the §10.3 notional floor beside it."""
    pipeline = _execution_pipeline(
        cash=500.0,
        equity=10_000.0,
        max_position_pct=20.0,
    )
    ctx = _exec_ctx([_entry()], cash=500.0, equity=10_000.0)

    ExecutionStage(pipeline=pipeline).run(ctx)

    # 20% of $10,000, not the $20,000 of 2.0x headroom an empty book leaves.
    assert _submitted_notional(pipeline) == 2_000.0


def test_a_short_is_never_sized_or_refused_by_the_budget():
    """D11 is unchanged. A short spends no settled cash and is never trimmed
    or refused by this clamp — it goes out in full against a budget of
    ZERO, exactly as it did before the swap."""
    equity = 10_000.0
    held = [_position(symbol="MSFT", qty=50, current_price=100.0)]  # $5,000
    pipeline = _execution_pipeline(
        cash=0.0,
        equity=equity,
        ceiling_x=0.5,
        positions=held,
    )
    short = TradeDecision(
        action="SHORT",
        symbol="XOM",
        allocation_pct=20.0,
        entry_price=EXEC_PRICE,
        stop_loss=EXEC_PRICE + 0.10,
        take_profit=EXEC_PRICE - 10.0,
        reasoning="short with no headroom",
    )
    ctx = _exec_ctx([short], cash=0.0, equity=equity, positions=held)

    ExecutionStage(pipeline=pipeline).run(ctx)

    sides = [c.kwargs.get("side") for c in pipeline.broker.submit_order.call_args_list]
    assert sides == ["sell_short"]
    assert _submitted_notional(pipeline) == 2_000.0
    assert ctx.execution_skips == []


def test_a_short_consumes_the_budget_for_the_long_behind_it():
    """A short OWNS gross exactly as a long does — `gross_exposure` sums the
    magnitude of both. So it must draw the pool down even though it is never
    sized by it, or a long behind it in the same batch would be sized
    against headroom the short has already spent."""
    equity = 10_000.0
    held = [_position(symbol="MSFT", qty=50, current_price=100.0)]  # $5,000
    pipeline = _execution_pipeline(
        cash=0.0,
        equity=equity,
        ceiling_x=1.0,
        positions=held,
    )
    short = TradeDecision(
        action="SHORT",
        symbol="XOM",
        allocation_pct=20.0,  # $2,000
        entry_price=EXEC_PRICE,
        stop_loss=EXEC_PRICE + 0.10,
        take_profit=EXEC_PRICE - 10.0,
        reasoning="short first",
    )
    ctx = _exec_ctx([short, _entry()], cash=0.0, equity=equity, positions=held)

    ExecutionStage(pipeline=pipeline).run(ctx)

    buys = [c.kwargs for c in pipeline.broker.submit_order.call_args_list if c.kwargs.get("side") == "buy"]
    # $5,000 of headroom at 1.0x, minus the $2,000 the short just took.
    assert len(buys) == 1
    assert buys[0]["qty"] * EXEC_PRICE == 3_000.0


def test_a_ladder_remnant_under_the_owner_min_risk_is_refused_end_to_end():
    """Ladder plus floor, through the real execution stage. The 0.5x floor
    rung leaves $200 of headroom over a $4,800 book on $10,000 of equity;
    clamped to it, the entry would risk $200 x 6% = $12, 0.12% of equity --
    under the owner's 0.5% minimum risk per position (owner rule
    2026-08-27), judged on the WHOLE position after the order. Nothing is
    placed and the skip names the floor, not the cash."""
    equity = 10_000.0
    held = [_position(symbol="MSFT", qty=48, current_price=100.0)]  # $4,800
    pipeline = _execution_pipeline(cash=6_000.0, equity=equity, ceiling_x=0.5, positions=held)
    ctx = _exec_ctx([_entry()], cash=6_000.0, equity=equity, positions=held)

    ExecutionStage(pipeline=pipeline).run(ctx)

    pipeline.broker.submit_order.assert_not_called()
    assert [s["reason"] for s in ctx.execution_skips] == ["below_owner_min_risk"]


# --- the budget itself, unit-tested at its degraded edges -----------------


def _budget_pipeline(**kwargs):
    return _execution_pipeline(cash=0.0, equity=0.0, **kwargs)


def test_the_budget_is_ladder_headroom_when_margin_is_on():
    pipeline = _budget_pipeline(ceiling_x=1.5)
    held = [_position(symbol="MSFT", qty=40, current_price=100.0)]
    budget, is_gross, note = _entry_deployment_budget(
        pipeline,
        _ctx_with_equity(10_000.0),
        held,
        10_000.0,
        250.0,
    )
    assert budget == 1.5 * 10_000.0 - 4_000.0
    assert is_gross is True
    assert "ladder headroom" in note


def test_settled_cash_still_binds_when_margin_is_off():
    """Turning the ceiling into the primary limit must not quietly hand a
    CASH-ONLY account the right to borrow. With `allow_margin` false the
    tighter of the two governs, which is what shipped before."""
    pipeline = _budget_pipeline(ceiling_x=2.0, allow_margin=False)
    budget, _is_gross, note = _entry_deployment_budget(
        pipeline,
        _ctx_with_equity(10_000.0),
        [],
        10_000.0,
        250.0,
    )
    assert budget == 250.0
    assert "margin disabled" in note


@pytest.mark.parametrize("bad_equity", [float("nan"), float("inf"), None])
def test_an_unusable_equity_read_buys_no_room_at_all(bad_equity):
    """`_resolve_gross_ceiling` already forces the FLOOR rung and alerts the
    owner on a non-finite equity read. Multiplying that rung by a NaN equity
    would produce a NaN budget, every `>` against it would be False, and the
    clamp would grant INFINITE room on exactly the broken-snapshot morning
    the guard exists for. It must fail closed instead."""
    pipeline = _budget_pipeline(ceiling_x=GROSS_LADDER[-1][1], rung="bad_read")
    bad, _is_gross, note = _entry_deployment_budget(
        pipeline,
        _ctx_with_equity(bad_equity),
        [],
        bad_equity,
        9_000.0,
    )
    good, _g, _n = _entry_deployment_budget(
        pipeline,
        _ctx_with_equity(10_000.0),
        [],
        10_000.0,
        9_000.0,
    )

    assert bad == 0.0
    assert bad <= good, "a bad read must never buy more room than a good one"
    assert "no usable equity" in note


def test_an_unreadable_ladder_falls_back_to_cash_never_to_the_standing_cap():
    """This is the LAST gate; the constructor may fall back to the standing
    cap because another gate still runs after it, and nothing runs after
    this one. An unresolvable ceiling therefore degrades to the pre-margin
    raw-cash clamp — never to 2.0x of an equity figure nobody vouched for."""
    pipeline = _budget_pipeline()
    pipeline._resolve_gross_ceiling = lambda ctx: None  # not a GrossCeiling

    budget, is_gross, note = _entry_deployment_budget(
        pipeline,
        _ctx_with_equity(10_000.0),
        [],
        10_000.0,
        250.0,
    )

    assert budget == 250.0
    assert is_gross is False
    assert "ladder unreadable" in note


def test_the_park_vehicle_is_not_charged_against_the_budget():
    """Same exclusion `gross_exposure` is given everywhere else: parked cash
    is not exposure, and counting it would consume the allowance doing
    nothing."""
    pipeline = _budget_pipeline(ceiling_x=1.0)
    parked = [_position(symbol="SGOV", qty=50, current_price=100.0)]
    budget, _is_gross, _note = _entry_deployment_budget(
        pipeline,
        _ctx_with_equity(10_000.0),
        parked,
        10_000.0,
        0.0,
    )
    assert budget == 10_000.0


@pytest.mark.parametrize("bad_park", [MagicMock(), 123, object()])
def test_an_unreadable_park_symbol_understates_headroom_rather_than_inflating(bad_park):
    """A park symbol that is not a string (an unconfigured sweeper, a stub,
    a mistyped setting) must not reach `gross_exposure` — some of these
    raise inside it, and a raise here would take the whole submit loop down.
    It degrades to "no park", which counts the vehicle as gross and
    UNDER-states the headroom: the safe side to be wrong on."""
    pipeline = _budget_pipeline(ceiling_x=1.0, park=bad_park)
    parked = [_position(symbol="SGOV", qty=50, current_price=100.0)]
    budget, _is_gross, _note = _entry_deployment_budget(
        pipeline,
        _ctx_with_equity(10_000.0),
        parked,
        10_000.0,
        0.0,
    )
    assert budget == 5_000.0


def test_a_book_already_over_its_rung_has_zero_headroom_never_negative():
    """A negative budget is not a smaller budget: it would render to the
    operator as "-$500 still deployable", and would silently absorb the
    first $500 of anything a later refresh credited."""
    pipeline = _budget_pipeline(ceiling_x=0.5)
    over = [_position(symbol="MSFT", qty=55, current_price=100.0)]  # $5,500
    budget, _is_gross, _note = _entry_deployment_budget(
        pipeline,
        _ctx_with_equity(10_000.0),
        over,
        10_000.0,
        6_000.0,
    )
    assert budget == 0.0


def test_the_single_name_execution_cap_falls_back_closed_not_open():
    pipeline = _budget_pipeline()
    pipeline.config.risk.max_position_pct = MagicMock()  # unreadable
    assert _single_name_execution_cap(pipeline, 10_000.0) == 2_000.0  # 20% default
    assert _single_name_execution_cap(pipeline, float("nan")) == 0.0


# ===========================================================================
# docs/WORK.md item 111 — a multi-symbol de-lever must never leave a symbol
# without its protective stop while the loop goes on to touch the next one.
#
# These drive the REAL `_enforce_gross_ceiling` / `_force_delever` loops, the
# REAL `_submit_protected_sell` (snapshot -> write-ahead -> cancel -> submit)
# and the REAL `_finalize_pending_protections` (wait -> finalize). Only the
# two broker-edge seams are recorded: the moment a symbol's stops are
# cancelled at the broker ("naked") and the moment finalize rebuilds its
# coverage on the actual fill ("covered"). The defect is purely the ORDER of
# those events, so recording them at the seam is the defect itself, not a
# mock of it.
# ===========================================================================


def _stop_timeline_pipeline(*, allow_margin: bool):
    from src.config import CashSweepConfig
    from src.pipeline import TradingPipeline

    events: list[tuple[str, str]] = []
    pipeline = build_pipeline(cash_sweeper=None, db=MagicMock())
    pipeline.config = MagicMock()
    pipeline.config.risk = _risk_config(allow_margin=allow_margin)
    pipeline.config.cash_sweep = CashSweepConfig(enabled=False)
    # A book that fell 16% from its high: the ladder demands 1.0x.
    pipeline.db.get_daily_pnl.return_value = [{"total_value": EQUITY / 0.84}]
    pipeline._write_ahead_protection_restore = MagicMock(return_value=1)
    pipeline._compute_deployable_cash = MagicMock(return_value=0.0)

    broker = MagicMock()
    broker.snapshot_protective_stops.side_effect = lambda symbol, **_kw: (
        True,
        [{"id": f"stop-{symbol}", "stop_price": 80.0}],
    )

    def _cancel(symbol, _specs):
        events.append(("naked", symbol))
        return MagicMock(cleared=True)

    def _submit(*, symbol, **_kw):
        events.append(("submit", symbol))
        return {"id": f"ord-{symbol}", "symbol": symbol, "status": "accepted"}

    broker.cancel_snapshotted_stops.side_effect = _cancel
    broker.submit_order.side_effect = _submit
    broker.wait_for_order_terminal.return_value = "filled"
    broker.get_account.return_value = {
        "cash": 0.0,
        "portfolio_value": EQUITY,
        "last_equity": EQUITY,
    }
    broker.get_positions.return_value = []
    pipeline.broker = broker

    def _finalize(order_id, symbol, *_a, **_kw):
        events.append(("covered", symbol))
        return True, []

    pipeline._finalize_protection_after_sell = MagicMock(side_effect=_finalize)
    return pipeline, events


def _assert_no_symbol_left_naked_while_another_is_touched(events):
    naked: set[str] = set()
    touched: list[str] = []
    for kind, symbol in events:
        if kind in ("naked", "submit"):
            others = naked - {symbol}
            assert not others, (
                f"{sorted(others)} still had NO protective stop when the "
                f"de-lever moved on to {symbol} — full timeline: {events}"
            )
            if kind == "naked":
                naked.add(symbol)
                touched.append(symbol)
        elif kind == "covered":
            naked.discard(symbol)
    assert not naked, f"{sorted(naked)} never had coverage rebuilt: {events}"
    return touched


def test_a_multi_symbol_gross_delever_restores_each_stop_before_touching_the_next():
    """Item 111. Three names over a 1.0x ceiling: every one is trimmed, and
    each must have its stop rebuilt on the actual fill BEFORE the next name's
    stops are cancelled. Before the fix, finalize ran once after the loop, so
    the first name rode naked through every later cancel and submit."""
    from src.pipeline_context import RunContext

    pipeline, events = _stop_timeline_pipeline(allow_margin=True)
    ctx = RunContext(run_id="test-run", session="morning")
    ctx.positions = [
        _position("NVDA", qty=80.0, current_price=100.0),
        _position("AMD", qty=70.0, current_price=100.0),
        _position("MSFT", qty=50.0, current_price=100.0),
    ]  # $20k gross on $10k equity = 2.0x against a 1.0x ceiling
    ctx.total_value = EQUITY

    hold(pipeline.broker, {p.symbol: p.qty for p in ctx.positions})  # what the broker holds when the exit is re-sized
    orders = pipeline._enforce_gross_ceiling(ctx)

    touched = _assert_no_symbol_left_naked_while_another_is_touched(events)
    assert len(orders) >= 2 and len(touched) >= 2, (
        f"the scenario must trim more than one name to test anything: {events}"
    )


def test_a_multi_symbol_cash_only_delever_restores_each_stop_before_touching_the_next():
    """Item 111, the `_force_delever` site, which shares the batch shape: a
    cash deficit big enough to need two sales."""
    from src.pipeline_context import RunContext

    pipeline, events = _stop_timeline_pipeline(allow_margin=False)
    ctx = RunContext(run_id="test-run", session="morning")
    ctx.cash = -1_500.0
    ctx.positions = [
        _position("NVDA", qty=10.0, current_price=100.0, avg_entry=120.0),
        _position("AMD", qty=10.0, current_price=100.0, avg_entry=110.0),
        _position("MSFT", qty=10.0, current_price=100.0, avg_entry=90.0),
    ]
    ctx.total_value = EQUITY

    hold(pipeline.broker, {p.symbol: p.qty for p in ctx.positions})  # what the broker holds when the exit is re-sized
    orders = pipeline._force_delever(ctx)

    touched = _assert_no_symbol_left_naked_while_another_is_touched(events)
    assert len(orders) == 2 and touched == ["NVDA", "AMD"], events


# ===========================================================================
# docs/WORK.md item 112 — a de-lever that leaves the book over its ceiling
# leaves a DURABLE record: before/after gross, the ceiling, each order's fate.
# Observability only — the alert flag and every order are unchanged.
# ===========================================================================


def _shortfall_rows(database):
    import json

    rows = database.conn.execute(
        "SELECT run_id, agent_name, kind, scope, symbol, evidence_json "
        "FROM specialist_evidence WHERE kind='pipeline_event'"
    ).fetchall()
    return [
        (dict(r), json.loads(r["evidence_json"]))
        for r in rows
        if json.loads(r["evidence_json"]).get("stage") == "gross_delever"
    ]


def test_a_delever_left_over_the_ceiling_writes_a_durable_shortfall_row(tmp_path):
    from src.pipeline_context import RunContext
    from src.storage.db import Database

    database = Database(str(tmp_path / "t.db"))
    database.initialize()
    try:
        pipeline, _events = _stop_timeline_pipeline(allow_margin=True)
        pipeline.db.insert_specialist_evidence.side_effect = database.insert_specialist_evidence
        # The limit never fills: the broker cancels it, and the refreshed
        # book is exactly as levered as before.
        pipeline.broker.wait_for_order_terminal.return_value = "canceled"
        pipeline.broker.get_positions.return_value = [
            _position("NVDA", qty=200.0, current_price=100.0),
        ]
        ctx = RunContext(run_id="run-112", session="morning")
        ctx.positions = [_position("NVDA", qty=200.0, current_price=100.0)]
        ctx.total_value = EQUITY

        orders = pipeline._enforce_gross_ceiling(ctx)

        assert orders, "the trim is still submitted exactly as before"
        assert ctx.leverage["delever_incomplete"] is True
        rows = _shortfall_rows(database)
        assert len(rows) == 1, rows
        meta, data = rows[0]
        assert meta["run_id"] == "run-112"
        assert meta["agent_name"] == "pipeline" and meta["scope"] == "run"
        assert data["outcome"] == "still_over_ceiling"
        assert data["gross_usd_before"] == pytest.approx(20_000.0)
        assert data["gross_x_before"] == pytest.approx(2.0)
        assert data["gross_usd_after"] == pytest.approx(20_000.0)
        assert data["gross_x_after"] == pytest.approx(2.0)
        assert data["ceiling_x"] == 1.0
        assert data["equity_before"] == pytest.approx(EQUITY)
        assert data["ceiling_usd_before"] == pytest.approx(EQUITY)
        assert data["orders"] == [
            {
                "symbol": "NVDA",
                "action": "SELL",
                "qty_submitted": 100.0,
                "broker_order_id": "ord-NVDA",
                "terminal_status": "canceled",
                "stop_coverage_confirmed": True,
            }
        ]
    finally:
        database.close()


def test_a_delever_that_cleared_the_ceiling_writes_no_shortfall_row(tmp_path):
    from src.pipeline_context import RunContext
    from src.storage.db import Database

    database = Database(str(tmp_path / "t.db"))
    database.initialize()
    try:
        pipeline, _events = _stop_timeline_pipeline(allow_margin=True)
        pipeline.db.insert_specialist_evidence.side_effect = database.insert_specialist_evidence
        pipeline.broker.get_positions.return_value = [
            _position("NVDA", qty=100.0, current_price=100.0),
        ]  # 1.0x after the fill: at the ceiling, not over it
        ctx = RunContext(run_id="run-ok", session="morning")
        ctx.positions = [_position("NVDA", qty=200.0, current_price=100.0)]
        ctx.total_value = EQUITY

        assert pipeline._enforce_gross_ceiling(ctx)
        assert "delever_incomplete" not in ctx.leverage
        assert _shortfall_rows(database) == []
    finally:
        database.close()


def test_a_failed_shortfall_write_never_breaks_the_delever():
    from src.pipeline_context import RunContext

    pipeline, _events = _stop_timeline_pipeline(allow_margin=True)
    pipeline.db.insert_specialist_evidence.side_effect = RuntimeError("disk full")
    pipeline.broker.get_positions.return_value = [
        _position("NVDA", qty=200.0, current_price=100.0),
    ]
    ctx = RunContext(run_id="run-x", session="morning")
    ctx.positions = [_position("NVDA", qty=200.0, current_price=100.0)]
    ctx.total_value = EQUITY

    assert pipeline._enforce_gross_ceiling(ctx)
    assert ctx.leverage["delever_incomplete"] is True
    assert pipeline.db.insert_specialist_evidence.called


# ===========================================================================
# docs/WORK.md item 118 — the de-lever trim must actually FILL.
#
# The trim was priced only 1% through the market (SELL @ 0.99x, COVER @
# 1.01x). On a fast or gapping day — the very conditions that trip the
# ladder — a limit only 1% through can rest unfilled, leaving the book OVER
# its gross ceiling exactly when it must shed risk. The fix prices the trim
# 3% through, matching the desk's ratified protective-exit buffer
# (AlpacaBroker.STOP_LIMIT_BUFFER_PCT), so routine gap volatility clears it.
#
# These drive the REAL `_enforce_gross_ceiling` -> `_submit_protected_sell`
# -> `_finalize_pending_protections` path. The broker seam FILLS or RESTS an
# order depending only on whether its limit is marketable against a simulated
# gapped print — the exact mechanism the defect is about.
# ===========================================================================


def _gap_fill_pipeline(
    *,
    gap_price,
    originals,
    allow_margin=True,
    live_quote="gap",
    reject_limit=False,
    reject_all=False,
):
    """Real de-lever loop against a broker that fills or rests by limit price.

    A SELL fills only when its limit is at/below the tradeable print; a
    BUY-to-cover fills only when its limit is at/above it. A MARKET order
    (limit_price is None) always fills — that is what a market order is.
    `get_positions` then reports the book that actually remains after those
    fills, which is what `_alert_owner_delever_incomplete` re-measures against
    the ceiling.

    ``live_quote`` models what `broker.get_latest_quote` returns at submit
    time — the price the de-lever now crosses instead of a fixed % of a stale
    mark:
      * "gap" (default): a fresh quote AT the gapped print (bid=ask=gap_price),
        so a marketable limit is priced at the gap and fills however far the
        name moved;
      * None: no usable quote (bid/ask None), so the de-lever falls back to a
        MARKET order.
    """
    pipeline, events = _stop_timeline_pipeline(allow_margin=allow_margin)
    submitted: dict = {}

    if live_quote == "gap":
        quote = {"bid_price": gap_price, "ask_price": gap_price}
    elif live_quote is None:
        quote = {"bid_price": None, "ask_price": None}
    else:
        quote = live_quote
    pipeline.broker.get_latest_quote.side_effect = lambda _symbol: dict(quote)

    def _submit(*, symbol, side, limit_price, qty, **_kw):
        events.append(("submit", symbol))
        oid = f"ord-{symbol}"
        # `reject_all` refuses every order (id=None) — even the market
        # escalation, the genuinely-unfillable case. `reject_limit` refuses
        # only a LIMIT order (a wide-spread quote tripping the broker's
        # fat-finger guard -> rejected_outlier, which carries no id) so the
        # de-lever must escalate to a MARKET order (limit_price is None).
        if reject_all or (reject_limit and limit_price is not None):
            return {"id": None, "status": "rejected_outlier", "symbol": symbol}
        if limit_price is None:
            fillable = True  # a MARKET order fills unconditionally
        else:
            fillable = limit_price <= gap_price if side == "sell" else limit_price >= gap_price
        submitted[oid] = {
            "symbol": symbol,
            "side": side,
            "limit_price": limit_price,
            "qty": qty,
            "fillable": fillable,
        }
        return {"id": oid, "symbol": symbol, "status": "accepted"}

    def _terminal(order_id):
        return "filled" if submitted.get(order_id, {}).get("fillable") else "canceled"

    def _positions():
        out = []
        for p in originals:
            filled = sum(o["qty"] for o in submitted.values() if o["symbol"] == p.symbol and o["fillable"])
            remaining = abs(p.qty) - filled
            if remaining <= 1e-9:
                continue
            signed = remaining if p.qty > 0 else -remaining
            out.append(_position(p.symbol, qty=signed, current_price=p.current_price))
        return out

    pipeline.broker.submit_order.side_effect = _submit
    pipeline.broker.wait_for_order_terminal.side_effect = _terminal
    pipeline.broker.get_positions.side_effect = _positions
    return pipeline, events, submitted


def test_live_delever_price_crosses_the_correct_side_and_falls_back_to_market():
    """`_live_delever_price` prices off the LIVE quote, not a stale mark:
    a SELL crosses the bid, a COVER (buy) crosses the ask, and the reference
    is the live mid so the broker's fat-finger guard passes a gapped fill.
    A missing/failed quote returns (None, ...) so the caller sends a MARKET
    order — the graceful no-quote fallback."""
    from unittest.mock import MagicMock
    from src.pipeline import TradingPipeline

    pipeline = build_pipeline(broker=MagicMock())

    # Full two-sided quote: SELL -> bid, COVER -> ask, reference -> mid.
    pipeline.broker.get_latest_quote.return_value = {
        "bid_price": 79.0,
        "ask_price": 81.0,
    }
    assert pipeline._live_delever_price("NVDA", "sell") == (79.0, 80.0)
    assert pipeline._live_delever_price("NVDA", "buy") == (81.0, 80.0)

    # One-sided quote: the missing side yields a None limit (-> MARKET) while
    # the present side still prices and references off itself.
    pipeline.broker.get_latest_quote.return_value = {
        "bid_price": 79.0,
        "ask_price": None,
    }
    assert pipeline._live_delever_price("NVDA", "sell") == (79.0, 79.0)
    assert pipeline._live_delever_price("NVDA", "buy") == (None, 79.0)

    # No quote at all -> (None, None): a MARKET order, no reference.
    pipeline.broker.get_latest_quote.return_value = {
        "bid_price": None,
        "ask_price": None,
    }
    assert pipeline._live_delever_price("NVDA", "sell") == (None, None)

    # A zero/garbage bid is treated as no bid (not a $0 sell limit).
    pipeline.broker.get_latest_quote.return_value = {
        "bid_price": 0.0,
        "ask_price": 81.0,
    }
    assert pipeline._live_delever_price("NVDA", "sell") == (None, 81.0)

    # A broker/data failure degrades to a MARKET order rather than raising.
    pipeline.broker.get_latest_quote.side_effect = RuntimeError("data down")
    assert pipeline._live_delever_price("NVDA", "sell") == (None, None)


@pytest.mark.parametrize("gap_pct", [0.03, 0.08, 0.20])
def test_a_gross_delever_sell_fills_at_any_gap_size_off_the_live_quote(gap_pct):
    """The old fixed-3%-off-stale-mark SELL limit rested ABOVE the falling
    market on any gap wider than 3% (docs/WORK.md item 118). The de-lever now
    crosses the LIVE quote: a marketable limit AT the live bid, which is the
    gapped print itself, so it fills at a 3%, 8% and 20% gap-down alike — the
    gap is already IN the quote. The stale reference is gone."""
    from src.pipeline_context import RunContext

    ref = 100.0
    gap_price = round(ref * (1 - gap_pct), 2)
    originals = [_position("NVDA", qty=200.0, current_price=ref)]  # 2.0x
    pipeline, _events, submitted = _gap_fill_pipeline(
        gap_price=gap_price,
        originals=originals,
    )
    ctx = RunContext(run_id=f"run-gap-sell-{int(gap_pct * 100)}", session="morning")
    ctx.positions = [_position("NVDA", qty=200.0, current_price=ref)]
    ctx.total_value = EQUITY

    orders = pipeline._enforce_gross_ceiling(ctx)

    assert orders, "an over-ceiling book must be trimmed with no PM involved"
    order = submitted["ord-NVDA"]
    assert order["side"] == "sell"
    assert order["limit_price"] == pytest.approx(gap_price), (
        "the SELL limit is the LIVE bid (the gapped print), not a % of the stale $100 mark"
    )
    assert order["limit_price"] <= gap_price, "a live-bid limit is marketable"
    if gap_pct > 0.03:
        assert round(ref * 0.97, 2) > gap_price, (
            "guard: on this gap the OLD fixed 3%-off-stale-mark limit ($97) "
            "sat ABOVE the print and would have rested unfilled"
        )
    assert order["fillable"] is True
    assert "delever_incomplete" not in ctx.leverage, "the trim filled, so the refreshed book is back at its ceiling"


@pytest.mark.parametrize("gap_pct", [0.03, 0.08, 0.20])
def test_a_gross_delever_covers_a_short_at_any_gap_up_off_the_live_quote(gap_pct):
    """The short-side twin. A BUY-to-cover crosses the LIVE ask (the gapped-up
    print), so it fills at a 3%, 8% and 20% gap-UP alike, where the old fixed
    3%-off-stale-mark cover limit ($103) sat BELOW a bigger gap and rested.
    The trim is a BUY (cover) of exactly half the short."""
    from src.pipeline_context import RunContext

    ref = 100.0
    gap_price = round(ref * (1 + gap_pct), 2)
    originals = [_position("TSLA", qty=-200.0, current_price=ref)]  # 2.0x gross
    pipeline, _events, submitted = _gap_fill_pipeline(
        gap_price=gap_price,
        originals=originals,
    )
    ctx = RunContext(run_id=f"run-gap-cover-{int(gap_pct * 100)}", session="morning")
    ctx.positions = [_position("TSLA", qty=-200.0, current_price=ref)]
    ctx.total_value = EQUITY

    orders = pipeline._enforce_gross_ceiling(ctx)

    assert orders
    order = submitted["ord-TSLA"]
    assert order["side"] == "buy", "covering a short is a BUY-to-cover"
    assert order["limit_price"] == pytest.approx(gap_price), (
        "the COVER limit is the LIVE ask (the gapped print), not a % of the stale $100 mark"
    )
    assert order["limit_price"] >= gap_price, "a live-ask limit is marketable"
    if gap_pct > 0.03:
        assert round(ref * 1.03, 2) < gap_price, (
            "guard: on this gap the OLD fixed 3%-off-stale-mark cover limit "
            "($103) sat BELOW the print and would have rested unfilled"
        )
    assert order["fillable"] is True
    assert order["qty"] == pytest.approx(100.0), "cover exactly half the short"
    assert "delever_incomplete" not in ctx.leverage


def test_a_gross_delever_uses_a_market_order_when_no_live_quote_is_available():
    """No usable live quote (bid/ask both None) must not leave the book over
    its ceiling. The de-lever falls back to a MARKET order — the guaranteed
    fill — rather than resting on a stale % of the mark."""
    from src.pipeline_context import RunContext

    ref = 100.0
    gap_price = 80.0  # a 20% gap-down; the market order fills regardless
    originals = [_position("NVDA", qty=200.0, current_price=ref)]  # 2.0x
    pipeline, _events, submitted = _gap_fill_pipeline(
        gap_price=gap_price,
        originals=originals,
        live_quote=None,
    )
    ctx = RunContext(run_id="run-gap-noquote", session="morning")
    ctx.positions = [_position("NVDA", qty=200.0, current_price=ref)]
    ctx.total_value = EQUITY

    orders = pipeline._enforce_gross_ceiling(ctx)

    assert orders, "no quote must still de-lever, via a market order"
    order = submitted["ord-NVDA"]
    assert order["side"] == "sell"
    assert order["limit_price"] is None, "no live quote -> a MARKET order"
    assert order["fillable"] is True, "a market order is a guaranteed fill"
    assert "delever_incomplete" not in ctx.leverage


def test_a_gross_delever_trims_down_to_the_ceiling_and_never_below_it():
    """Correct quantity and no over-trim, both directions.

    Exact case: a 2.0x book ($20k on $10k, 1.0x ceiling) is trimmed by
    exactly the 100 shares that bring it to $10k — leaving 100 at 1.0x, not a
    share less. Whole-share case: a 1.5x book ($15k) needs 50 shares but the
    trim floors to 49, leaving 101 shares (1.01x) — the desk deliberately
    rounds the trim DOWN so it can never sell the book BELOW its ceiling; the
    one-share sliver still over is reported, not silently over-corrected."""
    from src.pipeline_context import RunContext

    ref = 100.0

    # Exact: trims to the ceiling, never past it.
    exact = [_position("NVDA", qty=200.0, current_price=ref)]  # 2.0x
    pipeline, _events, submitted = _gap_fill_pipeline(gap_price=ref, originals=exact)
    ctx = RunContext(run_id="run-qty-exact", session="morning")
    ctx.positions = [_position("NVDA", qty=200.0, current_price=ref)]
    ctx.total_value = EQUITY
    assert pipeline._enforce_gross_ceiling(ctx)
    assert submitted["ord-NVDA"]["qty"] == pytest.approx(100.0), (
        "trim exactly the 100 shares that bring $20k gross to the $10k ceiling"
    )
    remaining = pipeline.broker.get_positions()
    assert remaining and remaining[0].qty == pytest.approx(100.0), (
        "100 shares left = 1.0x, exactly the ceiling and never below it"
    )
    assert "delever_incomplete" not in ctx.leverage

    # Whole-share flooring: never over-trims below the ceiling.
    frac = [_position("MSFT", qty=150.0, current_price=ref)]  # 1.5x
    pipeline2, _e2, submitted2 = _gap_fill_pipeline(gap_price=ref, originals=frac)
    ctx2 = RunContext(run_id="run-qty-floor", session="morning")
    ctx2.positions = [_position("MSFT", qty=150.0, current_price=ref)]
    ctx2.total_value = EQUITY
    assert pipeline2._enforce_gross_ceiling(ctx2)
    trimmed = submitted2["ord-MSFT"]["qty"]
    assert trimmed == pytest.approx(49.0), (
        "the trim floors to whole shares (49), never rounding UP to 50 and selling the book below its ceiling"
    )
    left = pipeline2.broker.get_positions()[0].qty
    assert left * ref >= 10_000.0, "the remaining book is never below the ceiling"


@pytest.mark.parametrize("gap_pct", [0.03, 0.08, 0.20])
def test_a_cash_only_force_delever_fills_at_any_gap_size_off_the_live_quote(gap_pct):
    """Item 118, the `_force_delever` sibling (allow_margin=False). A cash
    deficit forces a SELL; the old fixed 3%-off-stale-mark limit ($97) rested
    above the print on any gap wider than 3% and left the deficit uncleared.
    The de-lever now crosses the LIVE bid (the gapped print), so it fills at a
    3%, 8% and 20% gap-down alike — both de-lever paths behave identically."""
    from src.pipeline_context import RunContext

    ref = 100.0
    gap_price = round(ref * (1 - gap_pct), 2)
    originals = [_position("NVDA", qty=100.0, current_price=ref, avg_entry=120.0)]
    pipeline, _events, submitted = _gap_fill_pipeline(
        gap_price=gap_price,
        originals=originals,
        allow_margin=False,
    )
    ctx = RunContext(run_id=f"run-force-gap-{int(gap_pct * 100)}", session="morning")
    ctx.cash = -5_000.0  # a real margin deficit forces the sweep
    ctx.positions = [_position("NVDA", qty=100.0, current_price=ref, avg_entry=120.0)]
    ctx.total_value = EQUITY

    orders = pipeline._force_delever(ctx)

    assert orders, "a negative-cash book must be de-levered with no PM involved"
    order = submitted["ord-NVDA"]
    assert order["side"] == "sell"
    assert order["limit_price"] == pytest.approx(gap_price), (
        "the SELL limit is the LIVE bid (the gapped print), not a % of the stale $100 mark"
    )
    assert order["limit_price"] <= gap_price, "a live-bid limit is marketable"
    if gap_pct > 0.03:
        assert round(ref * 0.97, 2) > gap_price, (
            "guard: on this gap the OLD fixed 3%-off-stale-mark limit ($97) "
            "sat above the print and would have rested, leaving the deficit "
            "uncleared"
        )
    assert order["fillable"] is True


def test_a_cash_only_force_delever_uses_a_market_order_when_no_live_quote():
    """The `_force_delever` sibling of the no-quote fallback: a cash deficit
    with no usable live quote must still clear, via a MARKET order."""
    from src.pipeline_context import RunContext

    ref = 100.0
    originals = [_position("NVDA", qty=100.0, current_price=ref, avg_entry=120.0)]
    pipeline, _events, submitted = _gap_fill_pipeline(
        gap_price=80.0,
        originals=originals,
        allow_margin=False,
        live_quote=None,
    )
    ctx = RunContext(run_id="run-force-noquote", session="morning")
    ctx.cash = -5_000.0
    ctx.positions = [_position("NVDA", qty=100.0, current_price=ref, avg_entry=120.0)]
    ctx.total_value = EQUITY

    orders = pipeline._force_delever(ctx)

    assert orders, "a negative-cash book with no quote must still de-lever"
    order = submitted["ord-NVDA"]
    assert order["side"] == "sell"
    assert order["limit_price"] is None, "no live quote -> a MARKET order"
    assert order["fillable"] is True, "a market order is a guaranteed fill"


def test_a_gross_delever_escalates_to_market_when_the_marketable_limit_is_rejected():
    """The residual the adversary flagged. A wide-spread live quote (LULD
    halt-reopen, thin/inverse name in a fast market) makes the marketable limit
    deviate >20% from mid, so the broker's fat-finger guard returns
    rejected_outlier. The de-lever must NOT skip the name — it ESCALATES to a
    MARKET order, the guaranteed fill, and coverage is rebuilt on that fill."""
    from src.pipeline_context import RunContext

    ref = 100.0
    originals = [_position("NVDA", qty=200.0, current_price=ref)]  # 2.0x
    pipeline, events, submitted = _gap_fill_pipeline(
        gap_price=95.0,
        originals=originals,
        reject_limit=True,
    )
    ctx = RunContext(run_id="run-gross-escalate", session="morning")
    ctx.positions = [_position("NVDA", qty=200.0, current_price=ref)]
    ctx.total_value = EQUITY

    orders = pipeline._enforce_gross_ceiling(ctx)

    assert orders, "a rejected limit must escalate, not skip the name"
    order = submitted["ord-NVDA"]
    assert order["limit_price"] is None, "the marketable limit was rejected, so the order that landed is a MARKET order"
    assert order["side"] == "sell"
    assert order["fillable"] is True
    assert ("covered", "NVDA") in events, (
        "stop coverage must be rebuilt on the escalated fill — the "
        "cancel->submit->restore invariant holds through the escalation"
    )
    pipeline.broker._restore_stop_orders.assert_not_called()
    assert "delever_incomplete" not in ctx.leverage


def test_a_cash_only_force_delever_escalates_to_market_when_the_limit_is_rejected():
    """The `_force_delever` twin of the escalation: a rejected marketable limit
    escalates to a MARKET order rather than leaving the account on margin."""
    from src.pipeline_context import RunContext

    ref = 100.0
    originals = [_position("NVDA", qty=100.0, current_price=ref, avg_entry=120.0)]
    pipeline, events, submitted = _gap_fill_pipeline(
        gap_price=95.0,
        originals=originals,
        allow_margin=False,
        reject_limit=True,
    )
    ctx = RunContext(run_id="run-force-escalate", session="morning")
    ctx.cash = -5_000.0
    ctx.positions = [_position("NVDA", qty=100.0, current_price=ref, avg_entry=120.0)]
    ctx.total_value = EQUITY

    orders = pipeline._force_delever(ctx)

    assert orders, "a rejected limit must escalate, not skip the name"
    order = submitted["ord-NVDA"]
    assert order["limit_price"] is None, "escalated to a MARKET order"
    assert order["side"] == "sell"
    assert order["fillable"] is True
    assert ("covered", "NVDA") in events
    pipeline.broker._restore_stop_orders.assert_not_called()


def test_a_gross_delever_reports_incomplete_only_if_even_the_market_order_fails():
    """The name is reported incomplete ONLY when even the market escalation
    fails — never on the first rejection. With every order refused, nothing
    fills, the book stays over its ceiling, the stops are restored (no naked
    position), and the incompleteness flag is set."""
    from src.pipeline_context import RunContext

    ref = 100.0
    originals = [_position("NVDA", qty=200.0, current_price=ref)]  # 2.0x
    pipeline, _events, submitted = _gap_fill_pipeline(
        gap_price=95.0,
        originals=originals,
        reject_all=True,
    )
    ctx = RunContext(run_id="run-gross-incomplete", session="morning")
    ctx.positions = [_position("NVDA", qty=200.0, current_price=ref)]
    ctx.total_value = EQUITY

    orders = pipeline._enforce_gross_ceiling(ctx)

    assert orders == [], "no order was accepted, so none is recorded"
    assert submitted == {}, "neither the limit nor the market order filled"
    # On total failure the cancelled stops must be restored — no naked position.
    pipeline.broker._restore_stop_orders.assert_called()
    assert ctx.leverage.get("delever_incomplete") is True, (
        "still over the ceiling after a failed de-lever must be reported"
    )


def test_a_cash_only_force_delever_reports_incomplete_when_even_market_fails():
    """The `_force_delever` twin: when even the market escalation fails, the
    name is not silently skipped — the owner is paged that the sweep could not
    clear the margin deficit."""
    from unittest.mock import patch
    from src.pipeline_context import RunContext

    ref = 100.0
    originals = [_position("NVDA", qty=100.0, current_price=ref, avg_entry=120.0)]
    pipeline, _events, submitted = _gap_fill_pipeline(
        gap_price=95.0,
        originals=originals,
        allow_margin=False,
        reject_all=True,
    )
    ctx = RunContext(run_id="run-force-incomplete", session="morning")
    ctx.cash = -5_000.0
    ctx.positions = [_position("NVDA", qty=100.0, current_price=ref, avg_entry=120.0)]
    ctx.total_value = EQUITY

    with patch("src.notifier.send_owner_alert") as alert:
        orders = pipeline._force_delever(ctx)

    assert orders == [], "no order was accepted, so none is recorded"
    pipeline.broker._restore_stop_orders.assert_called()
    assert alert.called, (
        "a sweep that could not clear the deficit even at market must page the owner, not skip silently"
    )
    (msg,), _kw = alert.call_args
    assert "INCOMPLETE" in msg and "NVDA" in msg


def test_a_cash_only_force_delever_reports_incomplete_when_no_long_to_sell():
    """No long position to sell is itself a residual miss: the account stays on
    margin, so the owner must be paged rather than only a log line written."""
    from unittest.mock import patch
    from src.pipeline_context import RunContext

    pipeline, _events = _stop_timeline_pipeline(allow_margin=False)
    ctx = RunContext(run_id="run-force-nolong", session="morning")
    ctx.cash = -5_000.0
    ctx.positions = [_position("NVDA", qty=-50.0, current_price=100.0)]  # short only
    ctx.total_value = EQUITY

    with patch("src.notifier.send_owner_alert") as alert:
        orders = pipeline._force_delever(ctx)

    assert orders == []
    assert alert.called, "no long to sell leaves the account on margin — page it"


# ===========================================================================
# Item 112 — the incomplete-de-lever OWNER PAGE, and its state-change guard.
#
# `_alert_owner_delever_incomplete` was promoted from a one-line session
# bullet to a standalone owner page (the sibling of the force-de-lever alert).
# The page is EDGE-triggered: it fires only on the transition INTO
# still-over-ceiling, so a book that sits over the ceiling for days does not
# page every session. The flag, the log line and the shortfall record are
# unchanged — they still happen every session it is over.
# ===========================================================================


def _delever_pipeline(tmp_path):
    from unittest.mock import MagicMock
    from src.pipeline import TradingPipeline
    from src.storage.db import Database

    pipeline = build_pipeline()
    pipeline.config = MagicMock()
    db = Database(str(tmp_path / "delever.db"))
    db.initialize()
    pipeline.db = db
    return pipeline


def _over_ctx(run_id, gross_x=1.5, ceiling_x=1.0):
    from src.pipeline_context import RunContext

    ctx = RunContext(run_id=run_id, session="close")
    ctx.leverage = {"gross_x": gross_x, "ceiling_x": ceiling_x}
    return ctx


def test_incomplete_delever_pages_on_the_first_over_ceiling_session(
    tmp_path,
    monkeypatch,
):
    """The False->True edge: first session that finishes still over the
    ceiling pages the owner, in plain words, with the real measured numbers
    and no internal jargon."""
    pipeline = _delever_pipeline(tmp_path)
    import src.notifier as notifier

    sent: list[str] = []
    monkeypatch.setattr(
        notifier,
        "send_owner_alert",
        lambda text, **kw: sent.append(text) or True,
    )

    ctx = _over_ctx("s1", gross_x=1.5, ceiling_x=1.0)
    pipeline._alert_owner_delever_incomplete(ctx)

    assert ctx.leverage["delever_incomplete"] is True
    assert len(sent) == 1
    msg = sent[0]
    assert "still" in msg
    assert "1.50x" in msg and "1.00x" in msg
    for banned in ("gross_x", "ceiling_x", "§11.2", "delever_incomplete"):
        assert banned not in msg


def test_incomplete_delever_does_not_page_again_while_it_stays_over(
    tmp_path,
    monkeypatch,
):
    """A book that sits over the ceiling for consecutive sessions pages ONCE,
    on the way in — not every session. The flag is still set each time."""
    pipeline = _delever_pipeline(tmp_path)
    import src.notifier as notifier

    sent: list[str] = []
    monkeypatch.setattr(
        notifier,
        "send_owner_alert",
        lambda text, **kw: sent.append(text) or True,
    )

    for rid in ("s1", "s2", "s3"):
        ctx = _over_ctx(rid, gross_x=1.5, ceiling_x=1.0)
        pipeline._alert_owner_delever_incomplete(ctx)
        assert ctx.leverage["delever_incomplete"] is True

    assert len(sent) == 1, "over-ceiling for three sessions pages only on entry"


def test_incomplete_delever_pages_again_after_clearing_then_relapsing(
    tmp_path,
    monkeypatch,
):
    """Clearing the ceiling resets the edge: a later relapse into still-over
    is a NEW transition and pages again."""
    pipeline = _delever_pipeline(tmp_path)
    import src.notifier as notifier

    sent: list[str] = []
    monkeypatch.setattr(
        notifier,
        "send_owner_alert",
        lambda text, **kw: sent.append(text) or True,
    )

    pipeline._alert_owner_delever_incomplete(_over_ctx("s1", 1.5, 1.0))  # page
    # Cleared: under the ceiling, no page, flag not set, state recorded False.
    cleared = _over_ctx("s2", gross_x=0.9, ceiling_x=1.0)
    pipeline._alert_owner_delever_incomplete(cleared)
    assert "delever_incomplete" not in cleared.leverage
    # Relapse: over again → a fresh transition → pages again.
    pipeline._alert_owner_delever_incomplete(_over_ctx("s3", 1.6, 1.0))

    assert len(sent) == 2


def test_incomplete_delever_unmeasurable_neither_pages_nor_records_state(
    tmp_path,
    monkeypatch,
):
    """An unmeasurable book (no gross_x/ceiling_x) must not page and must not
    write a ceiling-state row — so it cannot reset the transition edge for the
    next real measurement."""
    pipeline = _delever_pipeline(tmp_path)
    import src.notifier as notifier

    sent: list[str] = []
    monkeypatch.setattr(
        notifier,
        "send_owner_alert",
        lambda text, **kw: sent.append(text) or True,
    )

    from src.pipeline_context import RunContext

    ctx = RunContext(run_id="s1", session="close")
    ctx.leverage = {"gross_x": None, "ceiling_x": None}
    pipeline._alert_owner_delever_incomplete(ctx)

    assert sent == []
    assert pipeline.db.get_last_delever_over_ceiling() is None


# ===========================================================================
# ITEM 112 — the morning de-lever cuts WEAKEST-BY-CONVICTION first.
#
# The cut order ranks SEAT CONVICTION ABOUT THE HOLDING — three buckets
# (opposed / no coverage / supported) over the RAW seat verdicts. It must
# never rank by ENTRY eligibility: the conviction bar's STAY side is
# opposition-only by owner ruling (2026-09-25), so a held name that fails the
# entry bar on soft grounds has explicitly earned its right to stay, and a
# name the desk could not read at all is not a name the desk decided against.
# ===========================================================================


OPPOSED, NO_COVERAGE, SUPPORTED = 0, 1, 2


def _win_and_loser():
    """A conviction-dead WINNER and an intact-thesis LOSER, same gross.

    Biggest-loser ordering sells the LOSER first; conviction ordering sells
    the WINNER first — so which name goes first is a clean discriminator.
    """
    winner = _position("WIN", qty=100.0, avg_entry=80.0, current_price=100.0)  # +$2k
    loser = _position("LOS", qty=100.0, avg_entry=130.0, current_price=100.0)  # -$3k
    return winner, loser


def test_conviction_rank_trims_the_weakest_thesis_before_the_biggest_loser():
    """With a cut order supplied, the OPPOSED winner is cut before the
    supported loser — the exact inversion of the default biggest-loser
    order, proven on the same book and ceiling."""
    winner, loser = _win_and_loser()
    positions = [winner, loser]
    ceiling = resolve_gross_ceiling(-10.0, base_x=BASE_X)  # 1.5x -> $15k, over $5k
    assert ceiling.ceiling_x == 1.5

    baseline = apply_gross_ceiling([], list(positions), EQUITY, ceiling)
    assert baseline.trims[0].symbol == "LOS", "default order is biggest loser first"

    convicted = apply_gross_ceiling(
        [],
        list(positions),
        EQUITY,
        ceiling,
        conviction_rank={"WIN": (OPPOSED, 0), "LOS": (SUPPORTED, 5)},
    )
    assert convicted.trims[0].symbol == "WIN", (
        "the holding a seat argues against must be trimmed before the intact-thesis loser"
    )


def test_an_unread_holding_is_never_cut_before_an_opposed_one():
    """The three buckets are ordered opposed < unread < supported. A name the
    desk could not read is not a name the desk decided against."""
    opposed = _position("OPP", qty=100.0, avg_entry=100.0, current_price=100.0)
    unread = _position("UNR", qty=100.0, avg_entry=100.0, current_price=100.0)
    ceiling = resolve_gross_ceiling(-10.0, base_x=BASE_X)
    outcome = apply_gross_ceiling(
        [],
        [opposed, unread],
        EQUITY,
        ceiling,
        conviction_rank={"OPP": (OPPOSED, 0), "UNR": (NO_COVERAGE, 0)},
    )
    assert outcome.trims[0].symbol == "OPP"


def test_an_unread_holding_is_still_cut_before_a_supported_one():
    """Absence is not protection either: a live thesis outranks no thesis."""
    unread = _position("UNR", qty=100.0, avg_entry=100.0, current_price=100.0)
    backed = _position("SUP", qty=100.0, avg_entry=100.0, current_price=100.0)
    ceiling = resolve_gross_ceiling(-10.0, base_x=BASE_X)
    outcome = apply_gross_ceiling(
        [],
        [unread, backed],
        EQUITY,
        ceiling,
        conviction_rank={"UNR": (NO_COVERAGE, 0), "SUP": (SUPPORTED, 3)},
    )
    assert outcome.trims[0].symbol == "UNR"


def _unequal_book():
    """A BIG winner and a SMALL loser — unequal gross, so the ordering is a
    real test of how much gets shed (with equal sizes any order sheds the
    same amount and the comparison proves nothing)."""
    big = _position("BIG", qty=140.0, avg_entry=80.0, current_price=100.0)  # $14k
    small = _position("SML", qty=60.0, avg_entry=130.0, current_price=100.0)  # $6k
    return big, small


def _total_shed(outcome, sizes):
    return sum(sizes[t.symbol] * t.allocation_pct / 100.0 for t in outcome.trims)


def test_one_trim_clears_the_breach_instead_of_dragging_in_a_second_name():
    """The trim fraction rounds UP to the 0.1% the ticket carries.

    Rounding to NEAREST could leave a few dollars of breach behind, which
    then pulled in a whole extra name at the 1.0% minimum trim. Under the
    conviction cut order that spurious second trim lands by construction on
    the HIGHEST-conviction holding still standing — the exact opposite of
    this build's purpose — because the cut walks weakest-first. Asserted as
    a DIRECTION, not just a bound: the supported name is not touched at all.
    """
    big, small = _unequal_book()
    sizes = {"BIG": 14_000.0, "SML": 6_000.0}
    ceiling = resolve_gross_ceiling(-16.0, base_x=BASE_X)  # 1.0x -> $10k, over $10k
    assert ceiling.ceiling_x == 1.0

    convicted = apply_gross_ceiling(
        [],
        [big, small],
        EQUITY,
        ceiling,
        conviction_rank={"BIG": (OPPOSED, 0), "SML": (SUPPORTED, 5)},
    )
    assert [t.symbol for t in convicted.trims] == ["BIG"], (
        "one trim must clear the breach; the supported name must not be dragged in by a rounding residue"
    )
    # The residue is the ticket's own 0.1% precision, rounded DOWN so a trim
    # never sells the book below its ceiling — not a second name's worth.
    assert _total_shed(convicted, sizes) == pytest.approx(10_000.0, abs=14.0)

    # Without the guard this was TWO trims, the second landing on the
    # highest-conviction holding to chase a few dollars.
    baseline = apply_gross_ceiling([], [big, small], EQUITY, ceiling)
    assert "SML" not in [t.symbol for t in convicted.trims], (
        "the supported name is untouched; the baseline order reaching it is "
        f"its own ordering, not a residue: {[t.symbol for t in baseline.trims]}"
    )


def test_conviction_order_clears_the_ceiling_and_never_empties_the_book():
    """On UNEQUAL position sizes — where ordering genuinely can change the
    total — reordering the cut still clears the breach and still leaves the
    book standing.

    It does NOT shed an identical dollar amount, and this test refuses to
    claim it does. With the rounding fixed, the residual difference is the
    0.1% granularity the trim fraction is expressed in, applied to one
    position.
    """
    big, small = _unequal_book()
    sizes = {"BIG": 14_000.0, "SML": 6_000.0}
    ceiling = resolve_gross_ceiling(-10.0, base_x=BASE_X)  # 1.5x -> $15k
    over = 20_000.0 - 15_000.0

    baseline = apply_gross_ceiling([], [big, small], EQUITY, ceiling)
    convicted = apply_gross_ceiling(
        [],
        [big, small],
        EQUITY,
        ceiling,
        conviction_rank={"BIG": (OPPOSED, 0), "SML": (SUPPORTED, 5)},
    )
    assert baseline.trims[0].symbol == "SML"
    assert convicted.trims[0].symbol == "BIG"

    shed_baseline = _total_shed(baseline, sizes)
    shed_convicted = _total_shed(convicted, sizes)
    # Each clears the breach to within the ticket's own 0.1% precision, which
    # rounds DOWN on purpose so a trim never sells below the ceiling.
    tick = 0.001 * max(sizes.values())
    assert shed_baseline == pytest.approx(over, abs=tick)
    assert shed_convicted == pytest.approx(over, abs=tick)
    # And the two orderings now agree to within that same one step — no
    # ordering drags in a whole extra name.
    assert abs(shed_convicted - shed_baseline) <= tick + 1e-6
    assert len(convicted.trims) == len(baseline.trims) == 1
    for outcome, label in ((baseline, "baseline"), (convicted, "convicted")):
        assert 20_000.0 - _total_shed(outcome, sizes) > 0, f"{label} empties the book"


def test_conviction_rank_does_not_provoke_a_trim_from_new_buys():
    """A cut order is inert on step 3's gating: a book UNDER its ceiling is
    not trimmed no matter how weak a held name's conviction is, and a
    proposed BUY still cannot cause a held-book sale."""
    winner, loser = _win_and_loser()  # $20k book
    ceiling = resolve_gross_ceiling(0.0, base_x=BASE_X)  # 2.0x -> $20k, book fits
    outcome = apply_gross_ceiling(
        [_buy("TSLA", 10.0)],
        [winner, loser],
        EQUITY,
        ceiling,
        conviction_rank={"WIN": (OPPOSED, 0), "LOS": (SUPPORTED, 5)},
    )
    assert outcome.trims == [], (
        "a book at its ceiling must not be trimmed to make room for a BUY, "
        "and a weak conviction score is not itself a reason to sell"
    )


# --------------------------------------------------------------------------
# The pipeline-level cut order.
# --------------------------------------------------------------------------


def _morning_delever_pipeline(drawdown_frac):
    """A __new__'d pipeline wired for the morning conviction/floor de-lever,
    with `drawdown_frac` the fraction the book sits below its high."""
    from unittest.mock import MagicMock
    from src.config import CashSweepConfig
    from src.pipeline import TradingPipeline

    pipeline = build_pipeline(db=MagicMock(), broker=MagicMock())
    pipeline.config = MagicMock()
    pipeline.config.risk = _risk_config()
    pipeline.config.cash_sweep = CashSweepConfig(enabled=False)
    pipeline.db.get_daily_pnl.return_value = [
        {"total_value": EQUITY / (1.0 - drawdown_frac)},
    ]
    pipeline.broker.get_account.return_value = {
        "cash": 0.0,
        "portfolio_value": EQUITY,
        "last_equity": EQUITY,
    }
    pipeline.broker.get_positions.return_value = []
    pipeline.cash_sweeper = None
    pipeline._unsettled_exit_orders = {}
    pipeline._compute_deployable_cash = MagicMock(return_value=0.0)
    pipeline._finalize_pending_protections = MagicMock()

    def _sell(**kw):
        return ({"id": f"o-{kw['symbol']}", "symbol": kw["symbol"]}, {})

    pipeline._submit_protected_sell = MagicMock(side_effect=_sell)
    return pipeline


def _verdict(symbol, seat, direction, magnitude=1.0, conviction="high"):
    from src.models import AnalystVerdict, VerdictEvidence

    return AnalystVerdict(
        seat=seat,
        symbol=symbol,
        direction=direction,
        magnitude=magnitude,
        conviction=conviction,
        evidence=[VerdictEvidence(label="close", value=100.0)],
        invalidation="the thesis breaks",
    )


def test_the_cut_order_uses_raw_seat_verdicts_not_the_entry_survivor_list():
    """DECISIVE: ranking by `last_candidate_ranking` would invert the owner's
    2026-09-25 STAY ruling — that list is the post-eligibility, post-
    conviction-bar ENTRY survivors, and a held name that fails the entry bar
    on soft grounds is dropped from it precisely because it earns its right
    to stay. The cut order must not read it at all."""
    import inspect
    from src.pipeline import TradingPipeline

    source = inspect.getsource(TradingPipeline._conviction_cut_order)
    body = source.split('"""')[-1]
    assert "last_candidate_ranking" not in body, (
        "the cut order must not read the ENTRY survivor list; rank the raw seat verdicts instead"
    )
    assert "portfolio_manager" not in body


def test_an_actively_opposed_holding_sorts_first():
    from src.pipeline_context import RunContext

    pipeline = _morning_delever_pipeline(drawdown_frac=0.10)
    ctx = RunContext(run_id="c1", session="morning")
    ctx.positions = [
        _position("OPP", qty=100.0, current_price=100.0),
        _position("SUP", qty=100.0, current_price=100.0),
    ]
    ctx.total_value = EQUITY
    ctx.evidence_registry = {
        "OPP": {"technical": "bullish", "news": "bearish"},  # a seat objects
        "SUP": {"technical": "bullish"},
    }
    order = pipeline._conviction_cut_order(ctx)
    assert order["OPP"][0] == OPPOSED
    assert order["SUP"][0] == SUPPORTED
    assert order["OPP"] < order["SUP"]


def test_a_holding_no_seat_read_is_unread_not_opposed():
    """A bar-fetch outage, or a held name outside today's technical universe,
    must NOT author a liquidation order."""
    from src.pipeline_context import RunContext

    pipeline = _morning_delever_pipeline(drawdown_frac=0.10)
    ctx = RunContext(run_id="c2", session="morning")
    ctx.positions = [
        _position("DARK", qty=100.0, current_price=100.0),
        _position("OPP", qty=100.0, current_price=100.0),
    ]
    ctx.total_value = EQUITY
    ctx.evidence_registry = {"OPP": {"technical": "bearish"}}  # DARK unread
    order = pipeline._conviction_cut_order(ctx)
    assert order["DARK"][0] == NO_COVERAGE
    assert order["OPP"][0] == OPPOSED
    assert order["OPP"] < order["DARK"], "an unread holding must never be cut before one a seat argues against"

    pipeline._enforce_gross_ceiling_by_conviction(ctx)
    sold = [c.kwargs["symbol"] for c in pipeline._submit_protected_sell.call_args_list]
    assert sold == ["OPP"]


def test_the_desk_ranking_orders_names_inside_the_supported_bucket():
    """Inside a bucket the order is `rank_verdicts` over the RAW verdicts,
    best first, counted only for the side the book carries."""
    from src.pipeline_context import RunContext

    pipeline = _morning_delever_pipeline(drawdown_frac=0.10)
    ctx = RunContext(run_id="c3", session="morning")
    ctx.positions = [
        _position("TOP", qty=100.0, current_price=100.0),
        _position("TAIL", qty=100.0, current_price=100.0),
    ]
    ctx.total_value = EQUITY
    ctx.evidence_registry = {
        "TOP": {"technical": "bullish"},
        "TAIL": {"technical": "bullish"},
    }
    ctx.seat_verdicts = [
        _verdict("TOP", "technical", "bullish", magnitude=1.0, conviction="high"),
        _verdict("TAIL", "technical", "bullish", magnitude=0.5, conviction="low"),
    ]
    order = pipeline._conviction_cut_order(ctx)
    assert order["TOP"][0] == order["TAIL"][0] == SUPPORTED
    assert order["TOP"] > order["TAIL"], "the better-ranked name is cut last"

    pipeline._enforce_gross_ceiling_by_conviction(ctx)
    sold = [c.kwargs["symbol"] for c in pipeline._submit_protected_sell.call_args_list]
    assert sold == ["TAIL"]


def test_a_ranking_for_the_other_side_is_not_conviction_in_this_position():
    """A top-ranked BEARISH read lends a LONG no ranking strength."""
    from src.pipeline_context import RunContext

    pipeline = _morning_delever_pipeline(drawdown_frac=0.10)
    ctx = RunContext(run_id="c4", session="morning")
    ctx.positions = [_position("NVDA", qty=100.0, current_price=100.0)]
    ctx.total_value = EQUITY
    ctx.evidence_registry = {"NVDA": {"technical": "bullish"}}
    ctx.seat_verdicts = [_verdict("NVDA", "news", "bearish")]
    assert pipeline._conviction_cut_order(ctx)["NVDA"][1] == 0


def test_a_stale_stance_does_not_defend_a_position_against_the_cut():
    """`signed_source_score`'s seat counts are called WITH `ignored_sources`,
    so an over-age earnings stance stops counting — exactly as it stops
    counting for the constructor that would size the trade."""
    from src.pipeline_context import RunContext

    pipeline = _morning_delever_pipeline(drawdown_frac=0.10)
    ctx = RunContext(run_id="c5", session="morning")
    ctx.positions = [_position("NVDA", qty=100.0, current_price=100.0)]
    ctx.total_value = EQUITY
    ctx.evidence_registry = {"NVDA": {"earnings": "bullish"}}

    assert pipeline._conviction_cut_order(ctx)["NVDA"][0] == SUPPORTED
    ctx.evidence_stale_sources = {"NVDA": frozenset({"earnings"})}
    assert pipeline._conviction_cut_order(ctx)["NVDA"][0] == NO_COVERAGE, (
        "a stale stance leaves the holding UNREAD, not opposed"
    )


def test_the_conviction_delever_is_a_noop_without_a_fresh_read():
    """Every PM-less lane leaves ctx.evidence_registry empty — the conviction
    pass must do nothing there, and must NOT mark the deferred ceiling paid."""
    from src.pipeline_context import RunContext

    pipeline = _morning_delever_pipeline(drawdown_frac=0.10)
    ctx = RunContext(run_id="c6", session="morning")
    ctx.positions = [_position("NVDA", qty=200.0, current_price=100.0)]  # 2.0x
    ctx.total_value = EQUITY
    ctx.gross_ceiling_deferred = True

    assert pipeline._enforce_gross_ceiling_by_conviction(ctx) == []
    assert not pipeline._submit_protected_sell.called
    assert ctx.gross_ceiling_deferred is True


# --------------------------------------------------------------------------
# The deferred debt, and the lanes that must still pay it.
# --------------------------------------------------------------------------


def test_the_margin_floor_fires_only_on_a_liquidation_proximity_breach():
    """The morning preamble floor de-levers on a genuine margin breach (book
    levered past the base cap) using the live price, but DEFERS an ordinary
    drawdown-ladder breach — recording the debt, never waiving it."""
    from src.pipeline_context import RunContext

    breach = _morning_delever_pipeline(drawdown_frac=0.0)
    ctx_a = RunContext(run_id="f1", session="morning")
    ctx_a.positions = [_position("NVDA", qty=250.0, current_price=100.0)]  # 2.5x
    ctx_a.total_value = EQUITY
    orders_a = breach._enforce_gross_ceiling(ctx_a, floor_only=True)
    assert orders_a, "a book levered past the base cap is a margin breach"
    assert breach._submit_protected_sell.called
    assert distance_to_forced_liquidation_pct(25_000.0, EQUITY) < 33.3

    soft = _morning_delever_pipeline(drawdown_frac=0.16)
    ctx_b = RunContext(run_id="f2", session="morning")
    ctx_b.positions = [_position("NVDA", qty=180.0, current_price=100.0)]  # 1.8x
    ctx_b.total_value = EQUITY
    orders_b = soft._enforce_gross_ceiling(ctx_b, floor_only=True)
    assert orders_b == [], "an ordinary ladder breach is deferred, not floored"
    assert not soft._submit_protected_sell.called
    assert ctx_b.leverage["ceiling_x"] == 1.0
    assert ctx_b.leverage["distance_to_forced_liquidation_pct"] > 33.3
    assert ctx_b.gross_ceiling_deferred is True, "deferral is recorded as a debt"


def test_a_pm_less_lane_still_enforces_the_ordinary_ceiling():
    """The ORDINARY §11.2 ceiling must be enforced on every lane, including
    the PM-less early returns and the resume lane."""
    from src.pipeline_context import RunContext

    pipeline = _morning_delever_pipeline(drawdown_frac=0.16)  # -> 1.0x ceiling
    ctx = RunContext(run_id="f3", session="morning")
    ctx.positions = [_position("NVDA", qty=180.0, current_price=100.0)]  # 1.8x
    ctx.total_value = EQUITY

    assert pipeline._enforce_gross_ceiling(ctx, floor_only=True) == []
    assert ctx.gross_ceiling_deferred is True
    assert pipeline._enforce_gross_ceiling_by_conviction(ctx) == []
    pipeline._discharge_deferred_gross_ceiling(ctx)
    sold = [c.kwargs["symbol"] for c in pipeline._submit_protected_sell.call_args_list]
    assert sold == ["NVDA"]
    assert ctx.gross_ceiling_deferred is False


def test_a_failed_discharge_leaves_the_debt_owed_not_silently_paid():
    """DECISIVE: only an enforcement that actually ran may clear the flag."""
    from unittest.mock import MagicMock
    from src.pipeline_context import RunContext

    pipeline = _morning_delever_pipeline(drawdown_frac=0.16)
    pipeline._resolve_gross_ceiling = MagicMock(side_effect=RuntimeError("broker down"))
    ctx = RunContext(run_id="f4", session="morning")
    ctx.positions = [_position("NVDA", qty=180.0, current_price=100.0)]
    ctx.total_value = EQUITY
    ctx.gross_ceiling_deferred = True

    pipeline._discharge_deferred_gross_ceiling(ctx)  # must not raise
    assert ctx.gross_ceiling_deferred is True, "a discharge that could not run must leave the ceiling recorded as owed"


def test_an_unreadable_in_flight_exit_leaves_the_debt_owed():
    """The guard that refuses to re-cut must not double as a receipt."""
    from src.pipeline_context import RunContext

    pipeline = _morning_delever_pipeline(drawdown_frac=0.16)
    pipeline._unsettled_exit_orders = {"o-1": {"symbol": "NVDA", "submitted_qty": 20.0}}
    pipeline.broker.get_order_fill_info.return_value = None  # unreadable
    ctx = RunContext(run_id="f5", session="morning")
    ctx.positions = [_position("NVDA", qty=180.0, current_price=100.0)]
    ctx.total_value = EQUITY

    assert pipeline._enforce_gross_ceiling(ctx) == []
    assert not pipeline._submit_protected_sell.called
    assert ctx.gross_ceiling_deferred is True


def test_only_one_place_may_mark_the_gross_ceiling_debt_paid():
    """Structural pin: the flag is cleared in exactly ONE assignment in
    pipeline_delever.py (split step 3) — inside the enforcement itself, after
    it has measured and
    acted. A second `= False` anywhere is how the debt gets marked paid
    without anything being enforced, which is the bug this pins."""
    import ast
    from pathlib import Path

    # Bodies moved into src/delever/: count the rule over the shim AND every part.
    root = Path(__file__).resolve().parent.parent / "src"
    sources = [root / "pipeline_delever.py", *sorted((root / "delever").glob("*.py"))]
    clears = []
    for path in sources:
        for node in ast.walk(ast.parse(path.read_text())):
            if not isinstance(node, ast.Assign):
                continue
            if not (isinstance(node.value, ast.Constant) and node.value.value is False):
                continue
            for target in node.targets:
                if isinstance(target, ast.Attribute) and target.attr == "gross_ceiling_deferred":
                    clears.append(f"{path.name}:{node.lineno}")
    assert len(clears) == 1, (
        f"exactly one place may clear the deferred gross-ceiling debt; found {len(clears)} at lines {clears}"
    )


# --------------------------------------------------------------------------
# The async-fill race: the book is not sold down twice in one session.
# --------------------------------------------------------------------------


def _fill_info(status, filled_qty):
    return {"status": status, "filled_qty": filled_qty, "filled_avg_price": 100.0}


def test_an_exit_still_working_is_netted_out_of_the_next_measure():
    """DECISIVE 2: `wait_for_order_terminal` has a 15s ceiling and returns the
    LAST KNOWN status, so a marketable limit that misses leaves the order
    working while the code re-reads the book. Its open quantity is netted out
    of the next gross measure — the later pass cuts the TRUE residual instead
    of shedding the same exposure twice, and instead of refusing outright
    (which is worst exactly here: a miss in fifteen seconds means a gap, a
    halt or a vanished book)."""
    from src.pipeline_context import RunContext

    pipeline = _morning_delever_pipeline(drawdown_frac=0.16)  # 1.0x -> $10k
    # 80 shares ($8k) are already on their way out, none filled yet.
    pipeline._unsettled_exit_orders = {
        "o-1": {"symbol": "NVDA", "submitted_qty": 80.0},
    }
    pipeline.broker.get_order_fill_info.return_value = _fill_info("new", 0.0)

    ctx = RunContext(run_id="r1", session="morning")
    # The broker has not booked the sale: the full 1.8x book comes back.
    ctx.positions = [_position("NVDA", qty=180.0, current_price=100.0)]
    ctx.total_value = EQUITY

    pipeline._enforce_gross_ceiling(ctx)
    # $18k held, $8k already working out -> $10k after exits == the ceiling.
    # Nothing more may be sold.
    assert not pipeline._submit_protected_sell.called, "exposure already on its way out must not be shed a second time"


def test_the_true_residual_is_still_cut_when_the_in_flight_exit_is_too_small():
    """Netting is not a waiver: what the working order does not cover is."""
    from src.pipeline_context import RunContext

    pipeline = _morning_delever_pipeline(drawdown_frac=0.16)  # 1.0x -> $10k
    pipeline._unsettled_exit_orders = {
        "o-1": {"symbol": "NVDA", "submitted_qty": 20.0},  # only $2k working
    }
    pipeline.broker.get_order_fill_info.return_value = _fill_info("new", 0.0)

    ctx = RunContext(run_id="r2", session="morning")
    ctx.positions = [_position("NVDA", qty=180.0, current_price=100.0)]  # $18k
    ctx.total_value = EQUITY

    pipeline._enforce_gross_ceiling(ctx)
    assert pipeline._submit_protected_sell.called, (
        "$18k held minus $2k working is $16k against a $10k ceiling — the residual must still be cut"
    )


def test_a_settled_order_drops_out_of_the_register_on_re_poll():
    """The register self-heals: re-polling a since-filled order clears it, so
    the guard is scoped to the race and does not linger."""
    from src.pipeline_context import RunContext

    pipeline = _morning_delever_pipeline(drawdown_frac=0.16)
    pipeline._unsettled_exit_orders = {
        "o-1": {"symbol": "NVDA", "submitted_qty": 80.0},
    }
    pipeline.broker.get_order_fill_info.return_value = _fill_info("filled", 80.0)

    ctx = RunContext(run_id="r3", session="morning")
    ctx.positions = [_position("NVDA", qty=180.0, current_price=100.0)]
    ctx.total_value = EQUITY

    pipeline._enforce_gross_ceiling(ctx)
    assert pipeline._unsettled_exit_orders == {}
    assert pipeline._submit_protected_sell.called, "a settled order nets nothing; the book really is over its ceiling"


def test_every_exit_path_registers_its_own_settlement():
    """Structural pin for the OTHER paths that sell: the cash-deficit safety
    net, the position reviewer and the execution stage all go through
    `_finalize_pending_protections`, so registering there covers them by
    construction rather than by remembering to flag each call site."""
    import inspect
    from src.protection.sell_finalization import SellFinalization

    source = inspect.getsource(SellFinalization._finalize_pending_protections)
    assert "_register_exit_settlement" in source, "every waited-on exit must register its settlement state centrally"


def test_a_non_terminal_wait_registers_and_a_terminal_one_clears():
    from src.pipeline import TradingPipeline

    pipeline = build_pipeline(_unsettled_exit_orders={})
    pipeline._register_exit_settlement(
        {"order_id": "o-9", "symbol": "nvda", "submitted_qty": 5.0, "terminal_status": "new"},
    )
    assert pipeline._unsettled_exit_orders["o-9"]["symbol"] == "NVDA"
    pipeline._register_exit_settlement(
        {"order_id": "o-9", "symbol": "NVDA", "submitted_qty": 5.0, "terminal_status": "filled"},
    )
    assert pipeline._unsettled_exit_orders == {}
    # A wait that RAISED records None, which is not terminal.
    pipeline._register_exit_settlement(
        {"order_id": "o-8", "symbol": "NVDA", "submitted_qty": 5.0, "terminal_status": None},
    )
    assert "o-8" in pipeline._unsettled_exit_orders


def test_the_book_is_not_sold_down_twice_across_one_morning():
    """The whole-session invariant, end to end: preamble floor, then the
    conviction pass, then the `finally` discharge, against a broker that
    books each fill. Total shed never exceeds the measured breach."""
    from src.pipeline_context import RunContext

    pipeline = _morning_delever_pipeline(drawdown_frac=0.0)  # 2.0x -> $20k
    sold_qty = {"NVDA": 0.0}
    start_qty = 250.0  # $25k, $5k over

    def _sell(**kw):
        sold_qty[kw["symbol"]] += kw["qty"]
        return ({"id": f"o-{sold_qty[kw['symbol']]}", "symbol": kw["symbol"]}, {})

    pipeline._submit_protected_sell.side_effect = _sell

    def _positions():
        return [_position("NVDA", qty=start_qty - sold_qty["NVDA"], current_price=100.0)]

    pipeline.broker.get_positions.side_effect = _positions

    ctx = RunContext(run_id="s1", session="morning")
    ctx.positions = _positions()
    ctx.total_value = EQUITY

    pipeline._enforce_gross_ceiling(ctx, floor_only=True)
    ctx.evidence_registry = {"NVDA": {"technical": "bearish"}}
    pipeline._enforce_gross_ceiling_by_conviction(ctx)
    pipeline._discharge_deferred_gross_ceiling(ctx)

    # $5k over a $20k ceiling: at most that plus one 0.1% rounding step.
    assert sold_qty["NVDA"] * 100.0 <= 5_000.0 + 0.001 * 25_000.0 + 1e-6, (
        f"the book was sold down twice: ${sold_qty['NVDA'] * 100.0:,.0f} shed against a $5,000 breach"
    )
    assert sold_qty["NVDA"] > 0, "the breach must actually be cleared"


def test_a_sigterm_unwinds_so_the_deferred_ceiling_is_still_paid():
    """The wrapper kills a slow morning with SIGTERM 30s before SIGKILL, and
    that is a documented, observed death mode for the morning tick. Python's
    default handling would end the process where it stands and the `finally`
    that pays the deferred ceiling would never run."""
    import signal
    from src.pipeline import SessionTerminated, TradingPipeline

    pipeline = build_pipeline()
    previous = pipeline._install_sigterm_unwind("test")
    try:
        assert previous is not None or signal.getsignal(signal.SIGTERM) is not None
        paid = []
        try:
            try:
                signal.raise_signal(signal.SIGTERM)
            finally:
                paid.append("discharged")
        except SessionTerminated:
            pass
        assert paid == ["discharged"], "SIGTERM must unwind through finally, not end the process"
    finally:
        pipeline._restore_sigterm(previous)


def test_a_holding_the_desk_already_decided_to_sell_is_not_cut_twice():
    """ADVERSARY FINDING (4th round). The conviction pass runs AFTER the risk
    stage, so the session's approved SELL/COVER decisions already exist. If
    the de-lever measures the held book as though those names were staying,
    it overstates the breach and reaches one extra name — and under a
    weakest-first cut the extra name is by construction among the STRONGEST
    convictions still standing, which is the exact opposite of the ordering's
    purpose. The doctrine here is the desk's own: it never sells a holding
    for a reason that is not about that holding.
    """
    from src.models import PortfolioDecision
    from src.pipeline_context import RunContext

    pipeline = _morning_delever_pipeline(drawdown_frac=0.10)  # 1.5x -> $15k
    ctx = RunContext(run_id="c-exits", session="morning")
    # $20k book against a $15k ceiling: $5k over. But the desk has already
    # decided to sell all of GOING ($5k), so the TRUE residual is zero.
    ctx.positions = [
        _position("GOING", qty=50.0, current_price=100.0),
        _position("KEEP", qty=100.0, current_price=100.0),
        _position("SPARE", qty=50.0, current_price=100.0),
    ]
    ctx.total_value = EQUITY
    ctx.evidence_registry = {
        "GOING": {"technical": "bearish"},  # opposed — cut first
        "KEEP": {"technical": "bullish"},  # supported — must survive
        "SPARE": {"technical": "bullish"},
    }
    ctx.portfolio_decision = PortfolioDecision.model_construct(
        decisions=[
            TradeDecision(
                action="SELL",
                symbol="GOING",
                allocation_pct=100.0,
                entry_price=0.0,
                stop_loss=0.0,
                take_profit=0.0,
                reasoning="thesis broke; the desk is exiting this name today",
            )
        ],
    )

    pipeline._enforce_gross_ceiling_by_conviction(ctx)
    sold = [c.kwargs["symbol"] for c in pipeline._submit_protected_sell.call_args_list]
    assert sold == [], (
        "the exit the desk already approved covers the whole breach; "
        f"nothing else may be sold, but the de-lever sold {sold}"
    )


def test_a_broadcast_macro_stance_alone_does_not_protect_a_holding():
    """Items 109 + 112. `macro weighted, never solo` (owner, 2026-09-25): a
    market-wide macro read back-filled onto a name whose sector it never
    mentioned may not be the thing that saves that name from the cut. The
    removal is ONE-SIDED — a broadcast DISSENT still counts against.
    """
    from src.pipeline_context import RunContext

    pipeline = _morning_delever_pipeline(drawdown_frac=0.10)
    ctx = RunContext(run_id="c-macro", session="morning")
    ctx.positions = [
        _position("BROAD", qty=100.0, current_price=100.0),
        _position("REAL", qty=100.0, current_price=100.0),
    ]
    ctx.total_value = EQUITY
    ctx.evidence_registry = {
        "BROAD": {"macro": "bullish"},  # broadcast only
        "REAL": {"technical": "bullish"},  # a genuine per-name read
    }
    ctx.evidence_non_corroborating_sources = {"BROAD": frozenset({"macro"})}

    order = pipeline._conviction_cut_order(ctx)
    assert order["BROAD"][0] == NO_COVERAGE, (
        "a broadcast macro stance is not per-name support and must not lift a holding into the protected bucket"
    )
    assert order["REAL"][0] == SUPPORTED
    assert order["BROAD"] < order["REAL"]

    # One-sided: a broadcast BEARISH read on a long still opposes it.
    ctx.evidence_registry["BROAD"] = {"macro": "bearish"}
    assert pipeline._conviction_cut_order(ctx)["BROAD"][0] == OPPOSED


def test_a_symbol_absent_from_the_cut_order_is_unread_not_opposed():
    """Defence in depth for the same ruling: the rule that a data gap must
    not author a liquidation cannot depend on WHICH function dropped the
    symbol, so `apply_gross_ceiling`'s own default for an unmapped name is
    the NO-COVERAGE rung, never OPPOSED."""
    missing = _position("GONE", qty=100.0, current_price=100.0)
    opposed = _position("OPP", qty=100.0, current_price=100.0)
    ceiling = resolve_gross_ceiling(-10.0, base_x=BASE_X)
    outcome = apply_gross_ceiling(
        [],
        [missing, opposed],
        EQUITY,
        ceiling,
        conviction_rank={"OPP": (OPPOSED, 0)},  # GONE deliberately absent
    )
    assert outcome.trims[0].symbol == "OPP"
