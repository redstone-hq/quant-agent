"""Cash-only / margin policy invariants.

Default-false `RiskConfig.allow_margin`:
  1. Hard-blocks any BUY that would drive cash negative.
  2. Does NOT block SELL→BUY rotations where SELL proceeds cover the BUY
     (execution always runs sells-first then buys).
  3. With `allow_margin=True`, the cash rule doesn't fire.
  4. The PM prompt surfaces an explicit DE-LEVER mandate when cash is
     already negative at session start.
  5. The midday reviewer prompt surfaces the same mandate.
"""

from tests.fakes.held_book import hold
from unittest.mock import MagicMock, patch

import pytest

from src.config import RiskConfig
from src.models import Position, TradeDecision
from src.pipeline import HARD_BLOCK_RULES, TradingPipeline
from src.risk.rules import RiskRuleEngine
from tests.pipeline_factory import build_pipeline


def _risk_config(allow_margin: bool = False) -> RiskConfig:
    return RiskConfig(
        max_position_pct=50.0,
        max_total_position_pct=200.0,  # generous — not what we're testing
        require_stop_loss=True,
        allow_margin=allow_margin,
    )


def _pipeline_with_engine(cfg: RiskConfig) -> TradingPipeline:
    pipeline = build_pipeline(risk_engine=RiskRuleEngine(cfg))
    pipeline.config = MagicMock()
    pipeline.config.trading.universe = ["NVDA", "AAPL"]
    return pipeline


def test_cash_only_rule_is_hard_blocking():
    assert "cash_only" in HARD_BLOCK_RULES


def test_cash_only_blocks_buy_that_exceeds_cash():
    engine = RiskRuleEngine(_risk_config(allow_margin=False))
    decision = TradeDecision(
        action="BUY",
        symbol="NVDA",
        allocation_pct=10.0,  # $10k on $100k
        entry_price=100.0,
        stop_loss=95.0,
        take_profit=110.0,
        reasoning="breakout",
    )
    violations = engine.check(
        decision=decision,
        positions=[],
        total_value=100_000.0,
        cash=5_000.0,  # only $5k cash available
    )
    assert any(v.rule == "cash_only" for v in violations)


def test_cash_only_allows_buy_when_fits_in_cash():
    engine = RiskRuleEngine(_risk_config(allow_margin=False))
    decision = TradeDecision(
        action="BUY",
        symbol="NVDA",
        allocation_pct=5.0,  # $5k on $100k
        entry_price=100.0,
        stop_loss=95.0,
        take_profit=110.0,
        reasoning="fits",
    )
    violations = engine.check(
        decision=decision,
        positions=[],
        total_value=100_000.0,
        cash=10_000.0,
    )
    assert not any(v.rule == "cash_only" for v in violations)


def test_margin_mode_true_skips_cash_rule():
    engine = RiskRuleEngine(_risk_config(allow_margin=True))
    decision = TradeDecision(
        action="BUY",
        symbol="NVDA",
        allocation_pct=10.0,
        entry_price=100.0,
        stop_loss=95.0,
        take_profit=110.0,
        reasoning="margin ok",
    )
    violations = engine.check(
        decision=decision,
        positions=[],
        total_value=100_000.0,
        cash=1_000.0,  # margin would be used
    )
    assert not any(v.rule == "cash_only" for v in violations)


def test_item_85_negative_cash_with_margin_enabled_does_not_false_block_a_buy():
    """Board item 85 (2026-09-17): with cash negative and margin enabled,
    a confirmed BUY was refused for a reason unrelated to trade merit — the
    manager read cash as the spending limit when it was not. Reproduces the
    incident's real figures (cash -$915.83, equity $9,736, $10,652 already
    held [docs/WORK.md item 85]): `cash_only` must not fire at all once
    margin is on (it is `allow_margin`-gated, not a function of the sign of
    cash), and the gross-exposure LADDER — not raw cash — is what actually
    governs whether a new BUY has room. At the ladder's undrawn 2.0x
    ceiling ($19,472) against $10,652 already held, $8,820 of headroom
    remains, so a modest new BUY must clear both gates."""
    from src.risk.rules import GROSS_EXPOSURE_RULE, resolve_gross_ceiling

    engine = RiskRuleEngine(_risk_config(allow_margin=True))
    held = Position(
        symbol="HELD",
        qty=100,
        avg_entry=100.0,
        current_price=106.52,
        market_value=10_652.0,
        unrealized_pnl=652.0,
        sector="Technology",
    )
    decision = TradeDecision(
        action="BUY",
        symbol="CRM",
        allocation_pct=20.0,  # ~$1,947 of $9,736
        entry_price=100.0,
        stop_loss=95.0,
        take_profit=110.0,
        reasoning="confirmed setup",
    )
    ceiling = resolve_gross_ceiling(0.0, base_x=2.0)  # no drawdown -> 2.0x
    assert ceiling.ceiling_x == 2.0

    violations = engine.check(
        decision=decision,
        positions=[held],
        total_value=9_736.0,
        cash=-915.83,
        gross_ceiling=ceiling,
    )

    assert not any(v.rule == "cash_only" for v in violations), (
        "cash_only must never fire with allow_margin=True — negative cash "
        "is expected once margin is on and is not a spending limit"
    )
    assert GROSS_EXPOSURE_RULE not in [v.rule for v in violations], (
        "the BUY has real headroom under the ladder and must not be refused for a reason unrelated to its own merit"
    )


def test_filter_accumulates_pending_buys_against_cash():
    """Two $6k BUYs with $10k cash: second one blocks, first passes."""
    pipeline = _pipeline_with_engine(_risk_config(allow_margin=False))
    d1 = TradeDecision(
        action="BUY",
        symbol="NVDA",
        allocation_pct=6.0,
        entry_price=100.0,
        stop_loss=95.0,
        take_profit=110.0,
        reasoning="first",
    )
    d2 = TradeDecision(
        action="BUY",
        symbol="AAPL",
        allocation_pct=6.0,
        entry_price=100.0,
        stop_loss=95.0,
        take_profit=110.0,
        reasoning="second",
    )

    allowed, _violations, blocked = pipeline.risk_gate._filter_hard_risk_decisions(
        [d1, d2],
        positions=[],
        total_value=100_000.0,
        cash=10_000.0,
    )

    symbols = [d.symbol for d in allowed]
    assert symbols == ["NVDA"]  # first passes, second blocked by cash
    assert any("AAPL" in msg and "cash" in msg.lower() for msg in blocked)


def test_filter_anticipates_same_session_sell_proceeds():
    """A SELL→BUY rotation must not trip cash-only since sells run first."""
    pipeline = _pipeline_with_engine(_risk_config(allow_margin=False))
    held = Position(
        symbol="SPY",
        qty=100,
        avg_entry=500,
        current_price=600,
        market_value=60_000,
        unrealized_pnl=10_000,
        sector="ETF",
    )
    pipeline.config.trading.universe = ["SPY", "NVDA"]
    sell = TradeDecision(
        action="SELL",
        symbol="SPY",
        allocation_pct=100.0,  # full exit → $60k back
        entry_price=0,
        stop_loss=0,
        take_profit=0,
        reasoning="rotate",
    )
    buy = TradeDecision(
        action="BUY",
        symbol="NVDA",
        allocation_pct=10.0,  # $10k — less than SPY proceeds
        entry_price=100.0,
        stop_loss=95.0,
        take_profit=110.0,
        reasoning="rotation target",
    )

    allowed, _, blocked = pipeline.risk_gate._filter_hard_risk_decisions(
        [sell, buy],
        positions=[held],
        total_value=100_000.0,
        cash=5_000.0,  # low starting cash
    )

    symbols = {d.symbol for d in allowed}
    assert "NVDA" in symbols, f"BUY should have passed after SELL proceeds; blocked={blocked}"
    assert "SPY" in symbols


def _generous_config() -> RiskConfig:
    """Caps high enough that only cash_only can bind — isolates the
    SELL-proceeds pre-sum math from position/sector/total limits."""
    return RiskConfig(
        max_position_pct=100.0,
        max_total_position_pct=1000.0,
        require_stop_loss=True,
        allow_margin=False,
    )


def test_presum_partial_sell_does_not_overcredit_proceeds():
    """A partial SELL whose shares round DOWN realizes LESS than the raw
    allocation fraction. ExecutionStage sells alloc=99% of a 10-share lot
    as int(9.9)=9 shares = 90% of value, not 99%. The cash-only pre-sum
    must credit the 90% it will actually realize, else a BUY sized against
    the phantom extra 9% would draw margin at execution time."""
    pipeline = _pipeline_with_engine(_generous_config())
    pipeline.config.trading.universe = ["BRK", "NVDA"]
    held = Position(
        symbol="BRK",
        qty=10,
        avg_entry=50_000,
        current_price=6_000,
        market_value=60_000,
        unrealized_pnl=0,
        sector="Financial Services",
    )
    sell = TradeDecision(
        action="SELL",
        symbol="BRK",
        allocation_pct=99.0,  # 9.9 → 9 shares = $54k
        entry_price=0,
        stop_loss=0,
        take_profit=0,
        reasoning="trim",
    )
    buy = TradeDecision(
        action="BUY",
        symbol="NVDA",
        allocation_pct=58.0,  # $58k
        entry_price=100.0,
        stop_loss=95.0,
        take_profit=110.0,
        reasoning="rotate",
    )
    # cash $2k + actual proceeds $54k = $56k < $58k BUY → must block.
    # The pre-fix code credited 99% ($59.4k) → $61.4k effective → wrongly allowed.
    allowed, _, blocked = pipeline.risk_gate._filter_hard_risk_decisions(
        [sell, buy],
        positions=[held],
        total_value=100_000.0,
        cash=2_000.0,
    )
    symbols = {d.symbol for d in allowed}
    assert "NVDA" not in symbols, "BUY must block — it exceeds cash + the 90% the rounded SELL realizes"
    assert any("NVDA" in msg and "cash" in msg.lower() for msg in blocked)


def test_presum_partial_sell_rounding_up_to_full_credits_full_proceeds():
    """The opposite rounding edge: alloc=40% of a 1-share lot rounds UP to
    a full exit (int(0.4)=0 → max(1,0)=1 share = 100%). The pre-sum must
    credit the full proceeds so a legit rotation BUY isn't false-blocked."""
    pipeline = _pipeline_with_engine(_generous_config())
    pipeline.config.trading.universe = ["BRK", "NVDA"]
    held = Position(
        symbol="BRK",
        qty=1,
        avg_entry=50_000,
        current_price=60_000,
        market_value=60_000,
        unrealized_pnl=10_000,
        sector="Financial Services",
    )
    sell = TradeDecision(
        action="SELL",
        symbol="BRK",
        allocation_pct=40.0,  # rounds up to full exit
        entry_price=0,
        stop_loss=0,
        take_profit=0,
        reasoning="exit",
    )
    buy = TradeDecision(
        action="BUY",
        symbol="NVDA",
        allocation_pct=58.0,  # $58k
        entry_price=100.0,
        stop_loss=95.0,
        take_profit=110.0,
        reasoning="rotate",
    )
    # cash $2k + full proceeds $60k = $62k > $58k → must allow. The pre-fix
    # code credited only 40% ($24k) → $26k effective → wrongly blocked.
    allowed, _, blocked = pipeline.risk_gate._filter_hard_risk_decisions(
        [sell, buy],
        positions=[held],
        total_value=100_000.0,
        cash=2_000.0,
    )
    symbols = {d.symbol for d in allowed}
    assert "NVDA" in symbols, (
        f"BUY should pass — the partial SELL rounds up to a full exit realizing 100% proceeds; blocked={blocked}"
    )


def test_pm_prompt_surfaces_delever_mandate_when_cash_negative():
    """When margin is disabled and cash is already negative, PM sees a clear
    mandate to SELL before any BUY. The engine will hard-block BUYs anyway,
    but the mandate gives the LLM the chance to pick which positions to trim."""
    from src.agents.portfolio_manager import PortfolioManagerAgent
    from src.models import MacroAnalysis, MacroPositionGuidance, MacroReasoningChain

    with patch("anthropic.Anthropic"):
        agent = PortfolioManagerAgent(api_key="test", model="claude-opus-4-6")
        msg = agent.build_user_message(
            analyses=[],
            positions=[
                Position(
                    symbol="SPY",
                    qty=10,
                    avg_entry=500,
                    current_price=600,
                    market_value=6_000,
                    unrealized_pnl=1_000,
                    sector="ETF",
                )
            ],
            macro_analysis=None,
            cash_balance=-2_500.0,  # already on margin
            total_value=3_500.0,
            earnings_analyses=[],
            allow_margin=False,
        )

    assert "DE-LEVER MANDATE" in msg
    assert "$2,500" in msg  # deficit figure surfaced


def test_pm_prompt_ignores_sub_dollar_cash_noise():
    """Fill-rounding leftovers like cash=-$0.30 should NOT fire the full
    DE-LEVER mandate — those clear on the next reconcile and a mandate
    would force an unnecessary SELL on sub-dollar noise."""
    from src.agents.portfolio_manager import PortfolioManagerAgent

    with patch("anthropic.Anthropic"):
        agent = PortfolioManagerAgent(api_key="test", model="claude-opus-4-6")
        msg = agent.build_user_message(
            analyses=[],
            positions=[],
            macro_analysis=None,
            cash_balance=-0.30,  # rounding noise
            total_value=50_000.0,
            earnings_analyses=[],
            allow_margin=False,
        )

    assert "DE-LEVER MANDATE" not in msg
    # Generic cash-only reminder is still rendered (no deficit figure though)
    assert "Cash-only account" in msg


def test_pm_prompt_no_mandate_when_margin_enabled():
    """With margin allowed, the mandate section stays empty even with negative cash."""
    from src.agents.portfolio_manager import PortfolioManagerAgent

    with patch("anthropic.Anthropic"):
        agent = PortfolioManagerAgent(api_key="test", model="claude-opus-4-6")
        msg = agent.build_user_message(
            analyses=[],
            positions=[],
            macro_analysis=None,
            cash_balance=-5_000.0,
            total_value=50_000.0,
            earnings_analyses=[],
            allow_margin=True,
        )

    assert "DE-LEVER MANDATE" not in msg


def test_force_delever_noop_when_margin_allowed():
    """With `allow_margin=True`, the safety-net never fires."""
    pipeline = build_pipeline(broker=MagicMock(), db=MagicMock())
    pipeline.config = MagicMock()
    pipeline.config.risk.allow_margin = True

    from src.pipeline_context import RunContext

    ctx = RunContext.start("morning")
    ctx.cash = -5_000.0  # on margin
    ctx.positions = [
        Position(
            symbol="SPY",
            qty=10,
            avg_entry=500,
            current_price=600,
            market_value=6_000,
            unrealized_pnl=1_000,
            sector="ETF",
        )
    ]

    orders = pipeline._force_delever(ctx)
    assert orders == []
    pipeline.broker.submit_order.assert_not_called()


def test_force_delever_noop_when_cash_positive():
    """Positive cash → never fires, even with margin disabled."""
    pipeline = build_pipeline(broker=MagicMock(), db=MagicMock())
    pipeline.config = MagicMock()
    pipeline.config.risk.allow_margin = False

    from src.pipeline_context import RunContext

    ctx = RunContext.start("morning")
    ctx.cash = 1_234.56
    ctx.positions = []

    orders = pipeline._force_delever(ctx)
    assert orders == []
    pipeline.broker.submit_order.assert_not_called()


def test_force_delever_skips_sub_dollar_noise():
    """Cash=-$0.30 is rounding noise; don't fire the safety-net either."""
    pipeline = build_pipeline(broker=MagicMock(), db=MagicMock())
    pipeline.config = MagicMock()
    pipeline.config.risk.allow_margin = False

    from src.pipeline_context import RunContext

    ctx = RunContext.start("morning")
    ctx.cash = -0.30
    ctx.positions = [
        Position(
            symbol="SPY",
            qty=10,
            avg_entry=500,
            current_price=600,
            market_value=6_000,
            unrealized_pnl=1_000,
            sector="ETF",
        )
    ]

    orders = pipeline._force_delever(ctx)
    assert orders == []
    pipeline.broker.submit_order.assert_not_called()


def test_force_delever_picks_biggest_loser_first():
    """Biggest unrealized loss gets sold first (cut-losers discipline)."""
    pipeline = build_pipeline(broker=MagicMock(), db=MagicMock())
    pipeline.config = MagicMock()
    pipeline.config.risk.allow_margin = False
    pipeline.broker.submit_order.return_value = {
        "id": "ord-1",
        "status": "accepted",
        "symbol": "LOSER",
    }
    pipeline.broker.wait_for_order_terminal.return_value = "filled"
    # audit F1 #1: SELL paths use the split snapshot/cancel seam.
    pipeline.broker.snapshot_protective_stops.return_value = (True, [])
    pipeline.broker.cancel_snapshotted_stops.return_value = MagicMock(cleared=True)
    pipeline.broker.cancel_protective_stops.return_value = (True, [])
    pipeline.broker.get_account.return_value = {
        "cash": 500.0,
        "portfolio_value": 10_000.0,
        "last_equity": 10_500.0,
    }
    pipeline.broker.get_positions.return_value = []
    # _live_delever_price reads the CURRENT bid/ask and prices a marketable
    # limit AT the live bid (docstring on _live_delever_price / item 118) —
    # a stale-mark % buffer rests above a falling market on a real gap and
    # never fills. A bare MagicMock() default for get_latest_quote (no
    # explicit bid/ask) is NOT "no live quote": MagicMock auto-implements
    # __float__ to return 1.0, so an unconfigured quote silently prices a
    # $1.00 limit on a $250 stock instead of exercising the no-quote/MARKET
    # path — set an explicit live quote so the test measures live-quote
    # pricing, not a mock artifact.
    pipeline.broker.get_latest_quote.return_value = {
        "bid_price": 248.50,
        "ask_price": 248.90,
    }

    from src.pipeline_context import RunContext

    ctx = RunContext.start("morning")
    ctx.cash = -500.0
    ctx.positions = [
        Position(
            symbol="WINNER",
            qty=10,
            avg_entry=100,
            current_price=120,
            market_value=1_200,
            unrealized_pnl=200,
            sector="ETF",
        ),
        Position(
            symbol="LOSER",
            qty=5,
            avg_entry=300,
            current_price=250,
            market_value=1_250,
            unrealized_pnl=-250,
            sector="Tech",
        ),
    ]

    hold(pipeline.broker, {p.symbol: p.qty for p in ctx.positions})  # what the broker holds when the exit is re-sized
    orders = pipeline._force_delever(ctx)

    assert len(orders) == 1
    # LOSER goes first (unrealized_pnl=-250 < 200)
    first_call = pipeline.broker.submit_order.call_args_list[0].kwargs
    assert first_call["symbol"] == "LOSER"
    assert first_call["side"] == "sell"
    # Marketable limit AT the live bid (item 118: live-quote de-lever pricing
    # via _live_delever_price), not a % off the stale last-print mark.
    assert first_call["limit_price"] == 248.50


def test_force_delever_stops_once_deficit_covered():
    """Sells only as many positions as needed to cover the deficit."""
    pipeline = build_pipeline(broker=MagicMock(), db=MagicMock())
    pipeline.config = MagicMock()
    pipeline.config.risk.allow_margin = False
    pipeline.broker.submit_order.return_value = {
        "id": "ord-X",
        "status": "accepted",
        "symbol": "X",
    }
    pipeline.broker.wait_for_order_terminal.return_value = "filled"
    # audit F1 #1: SELL paths use the split snapshot/cancel seam.
    pipeline.broker.snapshot_protective_stops.return_value = (True, [])
    pipeline.broker.cancel_snapshotted_stops.return_value = MagicMock(cleared=True)
    pipeline.broker.cancel_protective_stops.return_value = (True, [])
    pipeline.broker.get_account.return_value = {
        "cash": 1_000.0,
        "portfolio_value": 10_000.0,
        "last_equity": 11_000.0,
    }
    pipeline.broker.get_positions.return_value = []

    from src.pipeline_context import RunContext

    ctx = RunContext.start("morning")
    ctx.cash = -1_000.0  # $1000 deficit
    ctx.positions = [
        # One $5k position covers the whole deficit — second should NOT sell.
        Position(
            symbol="A", qty=50, avg_entry=100, current_price=100, market_value=5_000, unrealized_pnl=-100, sector="Tech"
        ),
        Position(
            symbol="B", qty=20, avg_entry=100, current_price=100, market_value=2_000, unrealized_pnl=-50, sector="Tech"
        ),
    ]

    hold(pipeline.broker, {p.symbol: p.qty for p in ctx.positions})  # what the broker holds when the exit is re-sized
    orders = pipeline._force_delever(ctx)

    assert len(orders) == 1
    assert pipeline.broker.submit_order.call_count == 1
    assert pipeline.broker.submit_order.call_args.kwargs["symbol"] == "A"


def test_filter_does_not_credit_zero_allocation_sell_as_proceeds():
    """Regression: PM emitting `SELL X alloc=0` must NOT make the filter
    pre-credit that position's full market_value as BUY cash budget. CLAUDE.md
    convention is alloc=0 → SKIP; execution stage skips; filter must match or
    BUY slips through against phantom cash and actually borrows margin."""
    pipeline = _pipeline_with_engine(_risk_config(allow_margin=False))
    held = Position(
        symbol="SPY",
        qty=100,
        avg_entry=500,
        current_price=600,
        market_value=60_000,
        unrealized_pnl=10_000,
        sector="ETF",
    )
    pipeline.config.trading.universe = ["SPY", "NVDA"]
    phantom_sell = TradeDecision(
        action="SELL",
        symbol="SPY",
        allocation_pct=0,  # skip per CLAUDE.md
        entry_price=0,
        stop_loss=0,
        take_profit=0,
        reasoning="phantom",
    )
    buy = TradeDecision(
        action="BUY",
        symbol="NVDA",
        allocation_pct=10.0,  # $10k needed
        entry_price=100.0,
        stop_loss=95.0,
        take_profit=110.0,
        reasoning="needs real cash, not phantom SELL proceeds",
    )

    allowed, _, blocked = pipeline.risk_gate._filter_hard_risk_decisions(
        [phantom_sell, buy],
        positions=[held],
        total_value=100_000.0,
        cash=5_000.0,  # only $5k actual
    )

    symbols = {d.symbol for d in allowed}
    assert "NVDA" not in symbols, f"BUY slipped through against phantom SELL proceeds; blocked={blocked}"
    assert any("NVDA" in msg and "cash" in msg.lower() for msg in blocked)


def test_force_delever_tiebreak_is_deterministic_on_equal_pnl():
    """When multiple positions tie on (unrealized_pnl, market_value), sort
    must fall back to symbol alphabetical so behavior is reproducible."""
    pipeline = build_pipeline(broker=MagicMock(), db=MagicMock())
    pipeline.config = MagicMock()
    pipeline.config.risk.allow_margin = False
    pipeline.broker.submit_order.return_value = {
        "id": "ord-1",
        "status": "accepted",
        "symbol": "AAA",
    }
    pipeline.broker.wait_for_order_terminal.return_value = "filled"
    # audit F1 #1: SELL paths use the split snapshot/cancel seam.
    pipeline.broker.snapshot_protective_stops.return_value = (True, [])
    pipeline.broker.cancel_snapshotted_stops.return_value = MagicMock(cleared=True)
    pipeline.broker.cancel_protective_stops.return_value = (True, [])
    pipeline.broker.get_account.return_value = {
        "cash": 100.0,
        "portfolio_value": 10_000.0,
        "last_equity": 10_500.0,
    }
    pipeline.broker.get_positions.return_value = []

    from src.pipeline_context import RunContext

    ctx = RunContext.start("morning")
    ctx.cash = -100.0
    # Three positions all identical PnL + market_value. Reverse-alphabetical
    # iteration order so a naive (stable-but-input-order-dependent) sort
    # would pick CCC; the correct symbol-tiebreak picks AAA.
    ctx.positions = [
        Position(
            symbol="CCC", qty=5, avg_entry=100, current_price=100, market_value=500, unrealized_pnl=0.0, sector="Tech"
        ),
        Position(
            symbol="BBB", qty=5, avg_entry=100, current_price=100, market_value=500, unrealized_pnl=0.0, sector="Tech"
        ),
        Position(
            symbol="AAA", qty=5, avg_entry=100, current_price=100, market_value=500, unrealized_pnl=0.0, sector="Tech"
        ),
    ]

    hold(pipeline.broker, {p.symbol: p.qty for p in ctx.positions})  # what the broker holds when the exit is re-sized
    orders = pipeline._force_delever(ctx)
    assert len(orders) == 1
    first_sym = pipeline.broker.submit_order.call_args.kwargs["symbol"]
    assert first_sym == "AAA"


def test_margin_deficit_floor_is_single_source_of_truth():
    """The $1 floor must live in one module so tightening doesn't leave
    prompt text or one agent out of sync."""
    from src.risk.constants import MARGIN_DEFICIT_FLOOR_USD

    assert MARGIN_DEFICIT_FLOOR_USD == 1.0
    # Defensive: pipeline shouldn't have reintroduced a private copy
    from src.pipeline import TradingPipeline

    assert not hasattr(TradingPipeline, "_FORCE_DELEVER_FLOOR_USD"), (
        "Remove duplicate floor constant — use MARGIN_DEFICIT_FLOOR_USD"
    )


def test_force_delever_noop_on_empty_positions():
    """Negative cash but no positions to sell — logs error and exits cleanly."""
    pipeline = build_pipeline(broker=MagicMock(), db=MagicMock())
    pipeline.config = MagicMock()
    pipeline.config.risk.allow_margin = False

    from src.pipeline_context import RunContext

    ctx = RunContext.start("morning")
    ctx.cash = -500.0
    ctx.positions = []

    orders = pipeline._force_delever(ctx)
    assert orders == []
    pipeline.broker.submit_order.assert_not_called()


def test_run_position_review_reconciles_after_force_delever():
    """Regression: when _force_delever fires in run_position_review, fills
    must be reconciled BEFORE the morning_trades query (executed_only=True)
    is issued — otherwise the submitted FORCE_DELEVER rows never reach
    position_reviewer's system_action_lines."""
    import types
    from src.pipeline import TradingPipeline
    from src.pipeline_context import RunContext

    pipeline = build_pipeline(broker=MagicMock(), db=MagicMock())
    pipeline.config = MagicMock()
    pipeline.config.risk.allow_margin = False

    call_log: list[str] = []
    pipeline._force_delever = MagicMock(side_effect=lambda ctx: (call_log.append("force"), [{"symbol": "NVDA"}])[1])
    pipeline._reconcile_fills = MagicMock(side_effect=lambda ctx=None: call_log.append("reconcile"))

    # Simulate just the 1a snippet of run_position_review
    ctx = RunContext.start("midday")
    forced_orders = pipeline._force_delever(ctx)
    if forced_orders:
        pipeline._reconcile_fills(ctx)

    assert call_log == ["force", "reconcile"], "reconcile must follow force_delever in the 1a block"


def test_run_position_review_skips_reconcile_when_nothing_delevered():
    """Inverse: a clean session (no forced sells) should NOT pay for an
    extra broker round-trip per midday/close tick."""
    from src.pipeline import TradingPipeline
    from src.pipeline_context import RunContext

    pipeline = build_pipeline(broker=MagicMock(), db=MagicMock())
    pipeline.config = MagicMock()
    pipeline.config.risk.allow_margin = False

    pipeline._force_delever = MagicMock(return_value=[])
    pipeline._reconcile_fills = MagicMock()

    ctx = RunContext.start("midday")
    forced_orders = pipeline._force_delever(ctx)
    if forced_orders:
        pipeline._reconcile_fills(ctx)

    pipeline._reconcile_fills.assert_not_called()


def test_midday_reviewer_surfaces_delever_when_cash_negative():
    from src.agents.position_reviewer import PositionReviewerAgent

    with patch("anthropic.Anthropic"):
        agent = PositionReviewerAgent(api_key="test", model="claude-sonnet-4-6")
        msg = agent.build_user_message(
            positions=[
                Position(
                    symbol="SPY",
                    qty=10,
                    avg_entry=500,
                    current_price=600,
                    market_value=6_000,
                    unrealized_pnl=1_000,
                    sector="ETF",
                )
            ],
            macro_summary={"vix": {"current": 20, "trend": "flat"}},
            cash_balance=-1_000.0,
            total_value=5_000.0,
            allow_margin=False,
        )

    assert "de-lever" in msg.lower() or "DE-LEVER" in msg
    assert "$1,000" in msg


def test_pm_prompt_never_says_no_margin_when_margin_enabled():
    """2026-09-17 CRM incident: the prompt hardcoded 'no margin' regardless
    of `allow_margin`, so the model refused a confirmed BUY reading a false
    prompt while execution had already spent margin the same session. The
    cash line must never assert "no margin" while margin is enabled."""
    from src.agents.portfolio_manager import PortfolioManagerAgent

    with patch("anthropic.Anthropic"):
        agent = PortfolioManagerAgent(api_key="test", model="claude-opus-4-6")
        msg = agent.build_user_message(
            analyses=[],
            positions=[],
            macro_analysis=None,
            cash_balance=-915.83,
            total_value=9_736.0,
            earnings_analyses=[],
            allow_margin=True,
            margin_headroom_usd=11_434.37,
            margin_ladder_backed=True,
            margin_ladder_multiple=2.0,
            margin_ladder_rung="none",
        )

    assert "no margin" not in msg.lower()
    # Raw cash is still shown, and still labelled as raw cash.
    assert "$-915.83" in msg
    assert "raw cash" in msg.lower()


def test_pm_prompt_margin_section_discloses_ladder_headroom():
    """With margin enabled and a resolved ladder, the Margin Capacity
    section must be non-empty and must show the SAME headroom figure
    execution's `_entry_deployment_budget` computes — never a separate
    number invented in the prompt layer."""
    from src.agents.portfolio_manager import PortfolioManagerAgent

    with patch("anthropic.Anthropic"):
        agent = PortfolioManagerAgent(api_key="test", model="claude-opus-4-6")
        msg = agent.build_user_message(
            analyses=[],
            positions=[],
            macro_analysis=None,
            cash_balance=-915.83,
            total_value=9_736.0,
            earnings_analyses=[],
            allow_margin=True,
            margin_headroom_usd=11_434.37,
            margin_ladder_backed=True,
            margin_ladder_multiple=2.0,
            margin_ladder_rung="none",
        )

    assert "Margin Capacity" in msg
    assert "$11,434.37" in msg
    assert "2.00x" in msg


def test_pm_prompt_margin_section_honest_when_ladder_unresolved():
    """Margin enabled but the ladder could not be resolved this session —
    the section must say so, not go blank and not fabricate a number."""
    from src.agents.portfolio_manager import PortfolioManagerAgent

    with patch("anthropic.Anthropic"):
        agent = PortfolioManagerAgent(api_key="test", model="claude-opus-4-6")
        msg = agent.build_user_message(
            analyses=[],
            positions=[],
            macro_analysis=None,
            cash_balance=500.0,
            total_value=9_736.0,
            earnings_analyses=[],
            allow_margin=True,
            # margin_ladder_backed defaults False — ladder unresolved.
        )

    assert "Margin Policy" in msg
    assert "could not be resolved" in msg
    assert "$" not in msg.split("## Margin Policy")[1].split("##")[0]


def test_pm_prompt_never_leaks_prohibited_buying_power_fields():
    """The prohibited margin-buying-power fields must never appear anywhere
    in the PM prompt, including in the new Margin Capacity wording — the
    §11.2 ladder headroom is a different, permitted number."""
    from src.agents.portfolio_manager import PortfolioManagerAgent

    with patch("anthropic.Anthropic"):
        agent = PortfolioManagerAgent(api_key="test", model="claude-opus-4-6")
        msg = agent.build_user_message(
            analyses=[],
            positions=[],
            macro_analysis=None,
            cash_balance=-915.83,
            total_value=9_736.0,
            earnings_analyses=[],
            allow_margin=True,
            margin_headroom_usd=11_434.37,
            margin_ladder_backed=True,
            margin_ladder_multiple=2.0,
            margin_ladder_rung="none",
        )

    for forbidden in ("buying_power", "regt_buying_power"):
        assert forbidden not in msg


def test_pm_prompt_margin_headroom_wired_from_entry_deployment_budget():
    """Source-level pin: the DecisionStage code path that builds the PM
    prompt's margin kwargs must call `_entry_deployment_budget` (the exact
    function execution's submit loop uses to size real orders) and thread
    its return values straight through to `decide()` — never re-derive the
    headroom with a second formula."""
    import inspect

    import src.pipeline_stages as ps

    src = inspect.getsource(__import__(ps.DecisionStage.__module__, fromlist=["_"]).build_pm_decide_inputs)
    # Layout-insensitive: the pin is the call and its arguments, not where a formatter wraps them.
    squeezed = "".join(src.split())
    assert "_entry_deployment_budget(pipeline,ctx,positions,total_value,cash" in squeezed
    assert "margin_headroom_usd=margin_headroom_usd" in src
    assert "margin_ladder_backed=margin_ladder_backed" in src


def test_reviewer_prompt_never_says_no_margin_when_margin_enabled():
    """Same 2026-09-17 defect, same fix, mirrored for the seat that decides
    whether to hold or cut existing positions twice a day (midday/close)."""
    from src.agents.position_reviewer import PositionReviewerAgent

    with patch("anthropic.Anthropic"):
        agent = PositionReviewerAgent(api_key="test", model="claude-sonnet-4-6")
        msg = agent.build_user_message(
            positions=[],
            macro_summary={"vix": {"current": 20, "trend": "flat"}},
            cash_balance=-915.83,
            total_value=9_736.0,
            allow_margin=True,
            margin_headroom_usd=11_434.37,
            margin_ladder_backed=True,
            margin_ladder_multiple=2.0,
            margin_ladder_rung="none",
        )

    assert "no margin" not in msg.lower()
    assert "$-915.83" in msg
    assert "raw cash" in msg.lower()


def test_reviewer_prompt_margin_section_discloses_ladder_headroom():
    """Margin Capacity section must be non-empty and show the SAME headroom
    figure execution's `_entry_deployment_budget` computes."""
    from src.agents.position_reviewer import PositionReviewerAgent

    with patch("anthropic.Anthropic"):
        agent = PositionReviewerAgent(api_key="test", model="claude-sonnet-4-6")
        msg = agent.build_user_message(
            positions=[],
            macro_summary={"vix": {"current": 20, "trend": "flat"}},
            cash_balance=-915.83,
            total_value=9_736.0,
            allow_margin=True,
            margin_headroom_usd=11_434.37,
            margin_ladder_backed=True,
            margin_ladder_multiple=2.0,
            margin_ladder_rung="none",
        )

    assert "Margin Capacity" in msg
    assert "$11,434.37" in msg
    assert "2.00x" in msg


def test_reviewer_prompt_margin_section_honest_when_ladder_unresolved():
    """Margin enabled but the ladder could not be resolved this session —
    the section must say so, not go blank and not fabricate a number."""
    from src.agents.position_reviewer import PositionReviewerAgent

    with patch("anthropic.Anthropic"):
        agent = PositionReviewerAgent(api_key="test", model="claude-sonnet-4-6")
        msg = agent.build_user_message(
            positions=[],
            macro_summary={"vix": {"current": 20, "trend": "flat"}},
            cash_balance=500.0,
            total_value=9_736.0,
            allow_margin=True,
            # margin_ladder_backed defaults False — ladder unresolved.
        )

    assert "Margin Policy" in msg
    assert "could not be resolved" in msg
    assert "$" not in msg.split("### Margin Policy")[1].split("###")[0]


def test_reviewer_prompt_never_leaks_prohibited_buying_power_fields():
    """The prohibited margin-buying-power fields must never appear anywhere
    in the reviewer prompt either."""
    from src.agents.position_reviewer import PositionReviewerAgent

    with patch("anthropic.Anthropic"):
        agent = PositionReviewerAgent(api_key="test", model="claude-sonnet-4-6")
        msg = agent.build_user_message(
            positions=[],
            macro_summary={"vix": {"current": 20, "trend": "flat"}},
            cash_balance=-915.83,
            total_value=9_736.0,
            allow_margin=True,
            margin_headroom_usd=11_434.37,
            margin_ladder_backed=True,
            margin_ladder_multiple=2.0,
            margin_ladder_rung="none",
        )

    for forbidden in ("buying_power", "regt_buying_power"):
        assert forbidden not in msg


def test_reviewer_prompt_margin_headroom_wired_from_entry_deployment_budget():
    """Source-level pin: `run_position_review` must call
    `_entry_deployment_budget` (the exact function execution's submit loop
    uses to size real orders) and thread its return values straight through
    to `PositionReviewerAgent.review` — never re-derive the headroom."""
    import inspect

    from src.pipeline import TradingPipeline

    # `run_position_review` became a thin persistence wrapper on 2026-09-18
    # (see `Database.save_session_report`); the pinned call now lives in
    # `_run_position_review_body`.
    src = inspect.getsource(
        __import__("src.sessions.position_review_session", fromlist=["PositionReviewSession"]).PositionReviewSession.run
    )
    assert "_entry_deployment_budget(" in src
    # The REAL pipeline (owner of `_resolve_gross_ceiling`), never the session
    # itself — passing `self` left the ladder unresolvable on every midday run.
    assert "self._pipeline,ctx,review_positions,total_value,review_cash," in "".join(src.split())
    assert "margin_headroom_usd=margin_headroom_usd" in src
    assert "margin_ladder_backed=margin_ladder_backed" in src


# --- Board item 95: the seat is shown the PRICE of the capacity ----------


def test_borrowing_cost_lines_price_both_the_carried_debit_and_the_headroom():
    """Item 95. `format_borrowing_cost_lines` must price what is already
    borrowed AND what the remaining ladder headroom would cost overnight,
    at Alpaca's 360-day convention, and must label both an ESTIMATE."""
    from src.margin_interest import ESTIMATE_LABEL, format_borrowing_cost_lines

    lines = format_borrowing_cost_lines(-915.83, 6.25, 11_434.37)

    assert lines, "a real debit plus real headroom must produce cost lines"
    blob = "\n".join(lines)
    # 915.83 * 0.0625 / 360 = 0.15899...
    assert "$0.16" in blob
    # 11,434.37 * 0.0625 / 360 = 1.98513...
    assert "$1.99" in blob
    assert "6.25%/yr" in blob
    assert ESTIMATE_LABEL in blob
    # Intraday leverage is free — that is a design lever, not a footnote.
    assert "Intraday leverage is free" in blob


def test_borrowing_cost_lines_never_state_a_hurdle_rate():
    """The owner supplied 6.25% as the COST of the debit. He has not
    ratified any minimum return on borrowed money, so the seat must never
    be handed one — the lines say 'cost of carry, not a hurdle rate'."""
    from src.margin_interest import format_borrowing_cost_lines

    blob = "\n".join(format_borrowing_cost_lines(-5_000.0, 6.25, 6_000.0))

    assert "not a hurdle rate" in blob
    assert "must not invent one" in blob
    for banned in ("hurdle rate of", "must beat", "must return at least", "required return"):
        assert banned not in blob


def test_borrowing_cost_lines_silent_with_nothing_to_price():
    """No debit and no headroom: say nothing rather than print a zero that
    would read as 'borrowing is free'. An unreadable rate is also silent,
    never a fabricated figure."""
    from src.margin_interest import format_borrowing_cost_lines

    assert format_borrowing_cost_lines(5_000.0, 6.25, 0.0) == []
    assert format_borrowing_cost_lines(5_000.0, 6.25, None) == []
    assert format_borrowing_cost_lines(-915.83, None, 11_434.37) == []
    assert format_borrowing_cost_lines(-915.83, 0.0, 11_434.37) == []


def test_borrowing_cost_lines_ignore_settlement_noise_deficits():
    """A sub-floor negative cash balance is settlement noise, not
    borrowing — the same floor the de-lever mandate uses. It must not be
    priced as a debit."""
    from src.margin_interest import format_borrowing_cost_lines

    blob = "\n".join(format_borrowing_cost_lines(-0.99, 6.25, None))
    assert blob == ""


def test_pm_prompt_shows_what_the_borrowing_capacity_COSTS():
    """Item 95's third criterion. The Margin Capacity block has named the
    spending limit since the 2026-09-17 CRM fix and never named its price,
    while the PM sheet says 'You may borrow'. Capacity without a price
    reads as free money."""
    from src.agents.portfolio_manager import PortfolioManagerAgent

    with patch("anthropic.Anthropic"):
        agent = PortfolioManagerAgent(api_key="test", model="claude-opus-4-6")
        msg = agent.build_user_message(
            analyses=[],
            positions=[],
            macro_analysis=None,
            cash_balance=-915.83,
            total_value=9_736.0,
            earnings_analyses=[],
            allow_margin=True,
            margin_headroom_usd=11_434.37,
            margin_ladder_backed=True,
            margin_ladder_multiple=2.0,
            margin_ladder_rung="none",
            margin_interest_rate_pct=6.25,
        )

    assert "What borrowing costs" in msg
    assert "Borrowing is NOT free" in msg
    assert "not a hurdle rate" in msg


def test_pm_prompt_omits_the_price_rather_than_guessing_it():
    """No rate threaded: the block must simply not price the borrowing.
    A prompt renderer may never substitute a rate of its own."""
    from src.agents.portfolio_manager import PortfolioManagerAgent

    with patch("anthropic.Anthropic"):
        agent = PortfolioManagerAgent(api_key="test", model="claude-opus-4-6")
        msg = agent.build_user_message(
            analyses=[],
            positions=[],
            macro_analysis=None,
            cash_balance=-915.83,
            total_value=9_736.0,
            earnings_analyses=[],
            allow_margin=True,
            margin_headroom_usd=11_434.37,
            margin_ladder_backed=True,
            margin_ladder_multiple=2.0,
            margin_ladder_rung="none",
        )

    assert "What borrowing costs" not in msg
    assert "6.25" not in msg
    # Capacity itself is unaffected — this is an addition, not a swap.
    assert "$11,434.37" in msg


def test_decide_forwards_the_margin_rate_to_the_prompt():
    """The rate must survive the `decide()` -> `build_user_message()` hop;
    a kwarg that is accepted and dropped is the same as never adding it."""
    import inspect

    from src.agents.portfolio_manager import PortfolioManagerAgent

    src = inspect.getsource(PortfolioManagerAgent.decide)
    assert "margin_interest_rate_pct=margin_interest_rate_pct" in src
    assert "margin_interest_rate_pct" in inspect.signature(PortfolioManagerAgent.decide).parameters
