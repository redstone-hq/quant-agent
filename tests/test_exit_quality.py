"""RC1 exit-quality guards (2026-07-16 forensics).

The sell autopsy found 28/53 realized exits were EARLY (stock ≥5% higher
within 20 days); 5 trail-stop fills missed an average +30.7% post-exit, and
LLY was trail-whipsawed twice identically in 4 weeks. Three deterministic
guards close the mechanical part:

  1. Noise-band clamp — a TRAIL_STOP inside 1.25×ATR14 of current price is
     rejected (routine volatility would fill it); hard-trigger reasons bypass.
  2. (Ratchet cooldown — a made-up 4-calendar-day lock — DELETED 2026-10-15;
     a tighten that clears the noise band is applied however recent the last.)
  3. Entry ATR floor — a BUY whose stop is closer than 1×ATR14 gets the stop
     widened (qty sizing compensates, per-trade $ risk unchanged).

Plus: position facts must show the LIVE broker stop (post-trail), not the
stale-wide BUY-row stop that kept the ratchet feedback going.
"""

from datetime import date as _date
from unittest.mock import MagicMock

from src.models import Position, PositionAction, PositionReview, PositionReasoningChain
from src.pipeline import TradingPipeline
from tests.pipeline_factory import build_pipeline


def _review_rc() -> PositionReasoningChain:
    return PositionReasoningChain(
        macro_continuity_check="stable",
        thesis_progress_check="on pace",
        thesis_integrity_check="intact",
        winners_discipline_check="no flags",
        session_disposition_check="patient",
        execution_rationale="n/a",
    )


def _mk_pipeline(position: Position) -> TradingPipeline:
    pipeline = build_pipeline(broker=MagicMock(), db=MagicMock())
    pipeline.broker.replace_stop_loss.return_value = {"id": "stop-1", "status": "accepted"}
    # Realistic live stop below the noise-floor tests' new stops, so the
    # min-ratchet floor (new_stop >= old_stop × 1.02) is satisfied by the
    # existing noise-band fixtures and does not shadow their assertions.
    pipeline.broker.get_current_stop_price.return_value = 330.0
    pipeline.db.get_trades.return_value = []
    pipeline._format_qty = lambda q: str(q)
    return pipeline


def _trail_review(symbol: str, new_stop: float, reason: str) -> PositionReview:
    return PositionReview(
        reasoning_chain=_review_rc(),
        actions=[PositionAction(action="TRAIL_STOP", symbol=symbol, reason=reason, new_stop_price=new_stop)],
        overall_assessment="trail",
        risk_level="low",
    )


GE = Position(
    symbol="GE",
    qty=26.0,
    avg_entry=316.0,
    current_price=360.0,
    market_value=9360.0,
    unrealized_pnl=1144.0,
    unrealized_intraday_pnl=0.0,
    sector="Industrials",
)


def test_trail_inside_noise_band_is_rejected():
    """ATR14=$8 → noise floor = 360 - 1.25*8 = $350. A $355 stop sits inside
    one day's range — keep the old stop."""
    pipeline = _mk_pipeline(GE)
    pipeline._atr_for_symbol = lambda s: 8.0
    orders = pipeline._midday_execute_llm_actions(
        positions=[GE],
        run_id="r-1",
        review=_trail_review("GE", 355.0, "TARGET_BREACH — locking in gains"),
    )
    assert orders == []
    pipeline.broker.replace_stop_loss.assert_not_called()


def test_trail_outside_noise_band_passes():
    pipeline = _mk_pipeline(GE)
    pipeline._atr_for_symbol = lambda s: 8.0
    orders = pipeline._midday_execute_llm_actions(
        positions=[GE],
        run_id="r-1",
        review=_trail_review("GE", 344.0, "TARGET_BREACH — locking in gains"),
    )
    assert len(orders) == 1
    pipeline.broker.replace_stop_loss.assert_called_once_with("GE", 344.0)


def test_trail_hard_trigger_bypasses_noise_clamp():
    """A cited hard trigger (thesis_invalid_if) may tighten into the band —
    same bypass philosophy as the SELL/REDUCE same-day-trim gate."""
    pipeline = _mk_pipeline(GE)
    pipeline._atr_for_symbol = lambda s: 8.0
    orders = pipeline._midday_execute_llm_actions(
        positions=[GE],
        run_id="r-1",
        review=_trail_review("GE", 355.0, "thesis_invalid_if triggered — guidance withdrawn"),
    )
    assert len(orders) == 1


def test_trail_unknowable_atr_degrades_open():
    """No bars → no clamp — blocking ALL trails on a data gap would leave
    runners unprotected from legitimate tightening."""
    pipeline = _mk_pipeline(GE)
    pipeline._atr_for_symbol = lambda s: None
    orders = pipeline._midday_execute_llm_actions(
        positions=[GE],
        run_id="r-1",
        review=_trail_review("GE", 355.0, "TARGET_BREACH"),
    )
    assert len(orders) == 1


def test_trail_repeat_tighten_is_not_day_locked():
    """A TRAIL_STOP that landed today no longer blocks a second soft-reason
    tighten: the made-up 4-day cooldown was deleted 2026-10-15; only the
    stock's own ATR noise band can refuse it."""
    from datetime import datetime, timezone

    pipeline = _mk_pipeline(GE)
    pipeline._atr_for_symbol = lambda s: 8.0
    pipeline.db.get_trades.return_value = [
        {
            "action": "TRAIL_STOP",
            "fill_status": "submitted",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
    ]
    orders = pipeline._midday_execute_llm_actions(
        positions=[GE],
        run_id="r-1",
        review=_trail_review("GE", 344.0, "TARGET_BREACH — trail up again"),
    )
    assert len(orders) == 1
    pipeline.broker.replace_stop_loss.assert_called_once_with("GE", 344.0)


# ---------- minimum-ratchet floor (item 108: prompt/code drift) ----------
#
# The position_reviewer prompt states `new_stop_price >= old_stop_price × 1.02`
# as a hard schema rule, but the TRAIL_STOP validation block never enforced it:
# an under-2% raise reached the broker, paying cancel/replace churn. These lock
# the floor. It is single-sourced from src.risk.trailing.min_ratchet_floor and,
# unlike the noise band, is NOT bypassable by a hard trigger.

from src.risk.trailing import min_ratchet_floor, venue_tick  # noqa: E402


def _floor_over(old_stop: float) -> float:
    return min_ratchet_floor(old_stop)


def test_trail_below_min_ratchet_floor_rejected_prior_stop_kept():
    """A raise that does not clear the live stop by one venue tick is the
    same stop price, so it is rejected and the existing broker stop is left
    untouched — protection is never removed, only left where it was."""
    pipeline = _mk_pipeline(GE)
    pipeline.broker.get_current_stop_price.return_value = 340.0
    pipeline._atr_for_symbol = lambda s: 1.0  # noise floor 358.75, irrelevant
    new_stop = 340.0 + venue_tick(340.0) / 4.0  # a quarter-tick: same price
    orders = pipeline._midday_execute_llm_actions(
        positions=[GE],
        run_id="r-1",
        review=_trail_review("GE", new_stop, "TARGET_BREACH — locking in gains"),
    )
    assert orders == []
    pipeline.broker.replace_stop_loss.assert_not_called()


def test_trail_at_or_above_min_ratchet_floor_passes():
    pipeline = _mk_pipeline(GE)
    pipeline.broker.get_current_stop_price.return_value = 340.0
    pipeline._atr_for_symbol = lambda s: 1.0
    new_stop = _floor_over(340.0) + 1.0  # comfortably above the tick floor
    orders = pipeline._midday_execute_llm_actions(
        positions=[GE],
        run_id="r-1",
        review=_trail_review("GE", new_stop, "TARGET_BREACH — locking in gains"),
    )
    assert len(orders) == 1
    pipeline.broker.replace_stop_loss.assert_called_once_with("GE", new_stop)


def test_trail_exactly_at_min_ratchet_floor_passes():
    """Boundary: exactly one tick above the live stop is accepted."""
    pipeline = _mk_pipeline(GE)
    pipeline.broker.get_current_stop_price.return_value = 340.0
    pipeline._atr_for_symbol = lambda s: 1.0
    new_stop = _floor_over(340.0)  # identical float to the code's computation
    orders = pipeline._midday_execute_llm_actions(
        positions=[GE],
        run_id="r-1",
        review=_trail_review("GE", new_stop, "TARGET_BREACH — locking in gains"),
    )
    assert len(orders) == 1
    pipeline.broker.replace_stop_loss.assert_called_once_with("GE", new_stop)


def test_trail_min_ratchet_floor_not_bypassed_by_hard_trigger():
    """Unlike the noise band, a cited hard trigger does
    NOT let an under-2% raise through — a sub-floor bump is churn regardless."""
    pipeline = _mk_pipeline(GE)
    pipeline.broker.get_current_stop_price.return_value = 340.0
    pipeline._atr_for_symbol = lambda s: 1.0
    new_stop = _floor_over(340.0) - 0.5
    orders = pipeline._midday_execute_llm_actions(
        positions=[GE],
        run_id="r-1",
        review=_trail_review("GE", new_stop, "thesis_invalid_if triggered — guidance withdrawn"),
    )
    assert orders == []
    pipeline.broker.replace_stop_loss.assert_not_called()


def test_trail_missing_old_stop_establishes_protection():
    """No readable live stop → floor cannot be computed, so a TRAIL_STOP still
    establishes protection rather than being blocked (the noise band still applies)."""
    pipeline = _mk_pipeline(GE)
    pipeline.broker.get_current_stop_price.return_value = None
    pipeline._atr_for_symbol = lambda s: None  # no noise clamp
    orders = pipeline._midday_execute_llm_actions(
        positions=[GE],
        run_id="r-1",
        review=_trail_review("GE", 345.0, "TARGET_BREACH — establishing stop"),
    )
    assert len(orders) == 1
    pipeline.broker.replace_stop_loss.assert_called_once_with("GE", 345.0)


# ---------- live stop reference in position facts ----------


def test_position_facts_prefer_live_broker_stop():
    """After a trail to $350, the BUY row still says $300 — the reviewer
    must see the live $350 (distance 2.8%), not a stale-wide 16.7%."""
    pipeline = build_pipeline(broker=MagicMock(), db=MagicMock())
    pipeline.broker.get_current_stop_price.return_value = 350.0
    pipeline.db.get_symbol_last_buy.return_value = {
        "stop_loss": 300.0,
        "take_profit": 400.0,
        "timestamp": "2026-07-01 14:00:00",
    }
    pipeline._atr_for_symbol = lambda s: 8.0
    facts = pipeline._build_position_facts(
        [GE],
        morning_trades=[],
        total_value=100_000.0,
    )
    dist = facts["GE"]["distance_to_stop_pct"]
    assert dist is not None and abs(dist - (360 - 350) / 360 * 100) < 0.01
    # vol-unit context present: (360-350)/8 = 1.25 ATRs
    assert facts["GE"]["stop_distance_atrs"] == 1.25
    assert facts["GE"]["atr_pct"] == round(8.0 / 360.0 * 100, 2)


def test_position_facts_fall_back_to_buy_row_stop():
    pipeline = build_pipeline(broker=MagicMock(), db=MagicMock())
    pipeline.broker.get_current_stop_price.return_value = None
    pipeline.db.get_symbol_last_buy.return_value = {
        "stop_loss": 300.0,
        "take_profit": 400.0,
        "timestamp": "2026-07-01 14:00:00",
    }
    pipeline._atr_for_symbol = lambda s: None
    facts = pipeline._build_position_facts(
        [GE],
        morning_trades=[],
        total_value=100_000.0,
    )
    dist = facts["GE"]["distance_to_stop_pct"]
    assert dist is not None and abs(dist - (360 - 300) / 360 * 100) < 0.01
    assert facts["GE"]["stop_distance_atrs"] is None  # unknown ≠ zero


def test_distance_to_stop_pct_is_side_aware_for_shorts():
    """A SHORT's stop sits ABOVE price. `distance_to_stop_pct` must still
    come out positive and RISE as price moves further from the stop
    (favourable for a short = price falling) — the same "higher is safer"
    meaning `_HIGHER_IS_BETTER` in `src/risk/exit_guard.py` assumes for
    every side. Before the 2026-09-18 fix the raw
    `(cur - stop_loss) / cur * 100` formula was applied unconditionally,
    which for a short is negative and moves the WRONG way: it got MORE
    negative (read as "worse") as the position got safer.
    """
    SHORT = Position(
        symbol="XYZ",
        qty=-10.0,
        avg_entry=100.0,
        current_price=90.0,
        market_value=-900.0,
        unrealized_pnl=100.0,
        unrealized_intraday_pnl=0.0,
        sector="Technology",
    )
    pipeline = build_pipeline(broker=MagicMock(), db=MagicMock())
    pipeline.broker.get_current_stop_price.return_value = None
    pipeline.db.get_symbol_last_buy.return_value = {
        "stop_loss": 110.0,
        "take_profit": 70.0,
        "timestamp": "2026-07-01 14:00:00",
    }
    pipeline._atr_for_symbol = lambda s: None

    facts_favorable = pipeline._build_position_facts(
        [SHORT],
        morning_trades=[],
        total_value=100_000.0,
    )
    dist_favorable = facts_favorable["XYZ"]["distance_to_stop_pct"]

    ADVERSE = Position(
        symbol="XYZ",
        qty=-10.0,
        avg_entry=100.0,
        current_price=105.0,
        market_value=-1050.0,
        unrealized_pnl=-50.0,
        unrealized_intraday_pnl=0.0,
        sector="Technology",
    )
    facts_adverse = pipeline._build_position_facts(
        [ADVERSE],
        morning_trades=[],
        total_value=100_000.0,
    )
    dist_adverse = facts_adverse["XYZ"]["distance_to_stop_pct"]

    # Both positive: "room left before the stop", never a negative reading.
    assert dist_favorable > 0
    assert dist_adverse > 0
    # Further from the stop (price fell to 90) must score HIGHER than
    # closer to the stop (price rose to 105) — the sign bug had this
    # backwards.
    assert dist_favorable > dist_adverse
    assert abs(dist_favorable - (110 - 90) / 90 * 100) < 0.01
    assert abs(dist_adverse - (110 - 105) / 105 * 100) < 0.01


def _pm_rc():
    from src.models import ReasoningChain

    return ReasoningChain(
        macro_filter="x",
        news_check="x",
        earnings_check="x",
        signal_conflicts="x",
        sizing_logic="x",
        portfolio_balance="x",
        cash_target="x",
    )


# ---------- entry ATR stop floor (ExecutionStage) ----------


def test_entry_stop_floor_widens_tight_stop_and_resizes():
    """Stop 0.5×ATR from entry gets widened to 1×ATR; qty_by_risk sizes
    against the wider distance so per-trade $ risk is unchanged."""
    from src.pipeline_stages import ExecutionStage
    from src.pipeline_context import RunContext
    from src.models import TradeDecision, PortfolioDecision, OHLCV, ReasoningChain

    pipeline = MagicMock()
    pipeline.broker.list_open_orders_checked.return_value = (True, [])  # listing read, nothing open
    pipeline.db.insert_trade.return_value = 7
    pipeline.broker.submit_order.return_value = {"id": "b1", "status": "accepted"}
    pipeline._order_accepted.return_value = True
    pipeline._refresh_account_state.return_value = (
        {"cash": 50_000.0, "portfolio_value": 100_000.0},
        [],
        {"NVDA": 100.0},
    )
    pipeline.broker.get_latest_price.return_value = 100.0
    # A market BUY is sized against the live ask it pays.
    pipeline.broker.get_latest_quote.return_value = {"bid_price": 99.9, "ask_price": 100.0}

    # 20 synthetic daily bars with true range ≈ $4 → ATR14 ≈ 4.
    bars = [
        OHLCV(date=_date(2026, 6, d + 1), open=100, high=102, low=98, close=100, volume=1_000_000) for d in range(20)
    ]

    ctx = RunContext.start("morning")
    ctx.positions, ctx.cash, ctx.total_value, ctx.last_equity = [], 50_000.0, 100_000.0, 100_000.0
    ctx.symbols_bars = {"NVDA": bars}
    ctx.portfolio_decision = PortfolioDecision(
        reasoning_chain=_pm_rc(),
        decisions=[
            TradeDecision(
                action="BUY",
                symbol="NVDA",
                allocation_pct=10.0,
                entry_price=100.0,
                stop_loss=98.0,  # 0.5×ATR — too tight
                take_profit=120.0,
                reasoning="test",
            )
        ],
        portfolio_view="test",
    )

    ExecutionStage(pipeline=pipeline).run(ctx)

    submit = pipeline.broker.submit_order.call_args.kwargs
    assert submit["symbol"] == "NVDA"
    assert submit["stop_loss_price"] == 96.0  # 100 - 1×ATR(=4)
    # alloc sizing binds here (10% of $100k / $100 = 100 sh; the ratified 5%
    # risk budget of $100k = $5,000 / $4 risk-per-share = 1,250 sh doesn't).
    # The floor's effect on qty shows via qty_by_risk when alloc is large;
    # what matters structurally: sizing used the WIDENED distance.
    assert submit["qty"] == 100
    # write-ahead row carries the widened stop too
    insert = pipeline.db.insert_trade.call_args_list[0].kwargs
    assert insert["stop_loss"] == 96.0


def test_entry_stop_floor_leaves_wide_stop_alone():
    from src.pipeline_stages import ExecutionStage
    from src.pipeline_context import RunContext
    from src.models import TradeDecision, PortfolioDecision, OHLCV, ReasoningChain

    pipeline = MagicMock()
    pipeline.broker.list_open_orders_checked.return_value = (True, [])  # listing read, nothing open
    pipeline.db.insert_trade.return_value = 7
    pipeline.broker.submit_order.return_value = {"id": "b1", "status": "accepted"}
    pipeline._order_accepted.return_value = True
    pipeline._refresh_account_state.return_value = (
        {"cash": 50_000.0, "portfolio_value": 100_000.0},
        [],
        {"NVDA": 100.0},
    )
    pipeline.broker.get_latest_price.return_value = 100.0
    pipeline.broker.get_latest_quote.return_value = {"bid_price": 100.0, "ask_price": 100.0}
    bars = [
        OHLCV(date=_date(2026, 6, d + 1), open=100, high=102, low=98, close=100, volume=1_000_000) for d in range(20)
    ]

    ctx = RunContext.start("morning")
    ctx.positions, ctx.cash, ctx.total_value, ctx.last_equity = [], 50_000.0, 100_000.0, 100_000.0
    ctx.symbols_bars = {"NVDA": bars}
    ctx.portfolio_decision = PortfolioDecision(
        reasoning_chain=_pm_rc(),
        decisions=[
            TradeDecision(
                action="BUY",
                symbol="NVDA",
                allocation_pct=10.0,
                entry_price=100.0,
                stop_loss=90.0,  # 2.5×ATR — fine
                take_profit=120.0,
                reasoning="test",
            )
        ],
        portfolio_view="test",
    )
    ExecutionStage(pipeline=pipeline).run(ctx)
    submit = pipeline.broker.submit_order.call_args.kwargs
    assert submit["stop_loss_price"] == 90.0


# ----------------------------------------------------------------------
# Board item 185 — the stop-sanity typo guard reads the instrument
# ----------------------------------------------------------------------


def _unprotected_pipeline() -> TradingPipeline:
    """A position with NO readable live stop, so the min-ratchet floor cannot
    run and the typo guard is the only thing between a mistyped price and the
    broker."""
    pipeline = _mk_pipeline(GE)
    pipeline.broker.get_current_stop_price.return_value = None
    pipeline._atr_for_symbol = lambda s: 8.0
    from src.portfolio_constructor import ConstructorConfig

    pipeline.portfolio_constructor = MagicMock()
    pipeline.portfolio_constructor.cfg = ConstructorConfig()
    return pipeline


def test_an_unprotected_position_is_always_left_WITH_a_stop():
    """Board item 185 + board item 80. GE at $360 with ATR14 $8: the widest
    stop this desk can place is 2.5 x ATR = $20 out, a floor of $340 (owner ruling 2026-10-04: one
    2.5 ATR width, no regime or setup scaling). A
    $300 proposal is past it and reads as a typed digit in the wrong place.

    The FIRST draft of this change REFUSED it — and the branch it runs on
    is the one where the live broker stop could not be read, i.e. exactly
    where the position may have no protection at all, so refusing ended the
    loop with the name naked. That is the owner's board-item-80 ruling
    inverted. The proposal is CLAMPED to $340 and placed instead.
    """
    pipeline = _unprotected_pipeline()
    pipeline._record_exit_refusal = MagicMock()
    orders = pipeline._midday_execute_llm_actions(
        positions=[GE],
        run_id="r-185",
        review=_trail_review("GE", 300.0, "TARGET_BREACH — locking in gains"),
    )
    # A stop IS placed, at the widest the desk's own rules can produce.
    assert len(orders) == 1
    pipeline.broker.replace_stop_loss.assert_called_once_with("GE", 340.0)
    # And the substitution is durable and per-symbol, not just a log line.
    from src.risk.exit_refusal import CODE_TRAIL_CLAMPED_TO_WIDEST

    kwargs = pipeline._record_exit_refusal.call_args.kwargs
    assert kwargs["symbol"] == "GE"
    assert kwargs["code"] == CODE_TRAIL_CLAMPED_TO_WIDEST
    assert kwargs["dropped"] is False
    assert "340" in kwargs["detail"]


def test_a_stop_the_desk_could_legitimately_place_is_left_alone():
    """The clamp is not a blanket rewrite: a proposal inside the widest
    stop, and outside the noise band, reaches the broker untouched."""
    pipeline = _unprotected_pipeline()
    orders = pipeline._midday_execute_llm_actions(
        positions=[GE],
        run_id="r-185",
        review=_trail_review("GE", 340.0, "TARGET_BREACH — locking in gains"),
    )
    assert len(orders) == 1
    pipeline.broker.replace_stop_loss.assert_called_once_with("GE", 340.0)


def test_the_clamped_stop_cannot_then_be_thrown_out_by_the_noise_band():
    """The clamped price is 2.5 x ATR below price and the noise floor is
    1.25 x ATR below it, so the clamp can never hand the noise-band clamp a
    stop it will reject — which would put the naked position back."""
    pipeline = _unprotected_pipeline()
    orders = pipeline._midday_execute_llm_actions(
        positions=[GE],
        run_id="r-185",
        # No hard trigger in the reason, so the RC1 clamps below DO run.
        review=_trail_review("GE", 10.0, "trimming risk into the close"),
    )
    assert len(orders) == 1
    pipeline.broker.replace_stop_loss.assert_called_once_with("GE", 340.0)


def test_a_readable_live_stop_is_governed_by_the_ratchet_not_the_clamp():
    """Where the live stop IS readable the min-ratchet floor is strictly
    stronger than any width bound, so the clamp must not run there as well:
    a WIDE raise over a low live stop is protection being added, and
    narrowing or refusing it would work against the raise."""
    pipeline = _mk_pipeline(GE)
    pipeline._atr_for_symbol = lambda s: 8.0
    pipeline.broker.get_current_stop_price.return_value = 300.0
    orders = pipeline._midday_execute_llm_actions(
        positions=[GE],
        run_id="r-185",
        review=_trail_review("GE", 330.0, "TARGET_BREACH — locking in gains"),
    )
    assert len(orders) == 1
    pipeline.broker.replace_stop_loss.assert_called_once_with("GE", 330.0)
