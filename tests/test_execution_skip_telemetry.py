"""Execution-phase BUY skips must leave durable evidence.

2026-08-19 production forensics: three risk-approved BUYs (XLE/XLF/XLI)
were skipped as unfunded on log-only `continue` statements. The DB showed
no trace, the funnel displayed `proposed_not_executed` with no reason, the
run reported status='executed' with zero orders, and the evening analyst
graded the day as a deliberate no-trade ("need more proactive idea
generation"). Every deterministic skip now appends to ctx.execution_skips
and persists an `execution_skip` evidence row; a morning whose approved
BUYs ALL died on the funding race reports terminal `buys_unfunded` without
automatically purchasing another full decision chain.
"""

import pytest
from unittest.mock import MagicMock

from src.models import PortfolioDecision, ReasoningChain, TradeDecision
from src.pipeline_context import RunContext
from src.pipeline_stages import ExecutionStage
from src.execution.cash_sweep import CashSweeper


@pytest.fixture
def limit_entries(monkeypatch):
    """Entries default to plain market orders (owner ruling 2026-10-09); this
    test exercises the marketable-limit path behind `execution.entry_order_type`."""
    monkeypatch.setattr("src.stage_execution_parts.entry_order_pricing.entry_orders_are_market", lambda _p: False)


def _rc() -> ReasoningChain:
    return ReasoningChain(
        macro_filter="m",
        news_check="n",
        earnings_check="e",
        signal_conflicts="s",
        sizing_logic="z",
        portfolio_balance="b",
        cash_target="c",
    )


def _pipeline(live_price=100.0, cash=50_000.0, equity=100_000.0):
    pipeline = MagicMock()
    pipeline.broker.get_latest_price.return_value = live_price
    # A market entry is sized against the quote side it pays (ask / bid).
    pipeline.broker.get_latest_quote.return_value = {"bid_price": live_price, "ask_price": live_price}
    pipeline._format_qty = lambda q: str(q)
    pipeline._order_accepted.return_value = True
    pipeline._refresh_account_state.return_value = (
        {"cash": cash, "portfolio_value": equity},
        [],
        {},
    )
    return pipeline


def _ctx(decisions, cash=50_000.0) -> RunContext:
    ctx = RunContext.start("morning")
    ctx.cash = cash
    ctx.total_value = 100_000.0
    ctx.last_equity = 100_000.0
    ctx.positions = []
    ctx.decision_id = "run-x-dec-abc123"
    ctx.portfolio_decision = PortfolioDecision(
        reasoning_chain=_rc(),
        decisions=decisions,
        portfolio_view="t",
    )
    ctx.symbols_bars = {}
    return ctx


def _evidence_kinds(pipeline) -> list[str]:
    return [call.kwargs.get("kind") for call in pipeline.db.insert_specialist_evidence.call_args_list]


def test_stale_entry_skip_is_recorded():
    pipeline = _pipeline(live_price=100.0)
    ctx = _ctx(
        [
            TradeDecision(
                action="BUY",
                symbol="SPY",
                allocation_pct=10,
                entry_price=80.0,
                stop_loss=72.0,
                take_profit=130.0,
                reasoning="stale entry",
            )
        ]
    )

    orders = ExecutionStage(pipeline=pipeline).run(ctx)

    assert orders == []
    assert [s["reason"] for s in ctx.execution_skips] == ["stale_entry"]
    assert ctx.execution_skips[0]["symbol"] == "SPY"
    assert "execution_skip" in _evidence_kinds(pipeline)


def test_partial_confirmed_cash_resizes_instead_of_dropping_buy():
    """A partial SGOV funding fill should preserve a smaller safe order.

    The cash figure was $145.11 (a one-share, $100 order) until the §10.3
    minimum-notional floor was re-applied after the execution-time cash
    clamp — the one resize that previously had no floor under it. The
    behaviour this test names is unchanged: a partial fill still resizes
    rather than dropping the BUY. What changed is that the resized order
    must now also be worth placing, so the scenario is stated at a size
    that clears the $500 minimum. The sub-floor case is asserted as a
    REFUSAL in tests/test_cash_sweep_plumbing.py.
    """
    pipeline = _pipeline(live_price=100.0, cash=645.11, equity=6_000.0)
    pipeline.broker.submit_order.return_value = {
        "id": "ord-partial",
        "status": "accepted",
    }
    ctx = _ctx(
        [
            TradeDecision(
                action="BUY",
                symbol="XLE",
                allocation_pct=10,
                entry_price=100.0,
                stop_loss=92.0,
                take_profit=115.0,
                reasoning="approved but unfunded",
            )
        ],
        cash=645.11,
    )
    # Owner floor: 6 shares risk $48 at the $92 stop; that must be >= 0.5% of equity.
    ctx.total_value = 6_000.0
    ctx.last_equity = 6_000.0

    orders = ExecutionStage(pipeline=pipeline).run(ctx)

    assert len(orders) == 1
    assert pipeline.broker.submit_order.call_args.kwargs["qty"] == 6
    assert ctx.execution_skips == []


def test_insufficient_cash_for_one_share_is_recorded():
    pipeline = _pipeline(live_price=100.0, cash=99.0)
    ctx = _ctx(
        [
            TradeDecision(
                action="BUY",
                symbol="XLE",
                allocation_pct=10,
                entry_price=100.0,
                stop_loss=95.0,
                take_profit=115.0,
                reasoning="approved but unfunded",
            )
        ],
        cash=99.0,
    )

    orders = ExecutionStage(pipeline=pipeline).run(ctx)

    assert orders == []
    assert [s["reason"] for s in ctx.execution_skips] == ["insufficient_cash"]
    pipeline.broker.submit_order.assert_not_called()


def test_successful_buy_records_no_skip():
    pipeline = _pipeline(live_price=100.0)
    pipeline.broker.submit_order.return_value = {
        "id": "ord-1",
        "status": "accepted",
    }
    ctx = _ctx(
        [
            TradeDecision(
                action="BUY",
                symbol="SPY",
                allocation_pct=10,
                entry_price=100.0,
                stop_loss=95.0,
                take_profit=112.0,
                reasoning="clean",
            )
        ]
    )

    orders = ExecutionStage(pipeline=pipeline).run(ctx)

    assert len(orders) == 1
    assert ctx.execution_skips == []


def test_buy_limit_crosses_offer_with_bounded_price_protection(limit_entries):
    pipeline = _pipeline(live_price=100.0)
    pipeline.broker.get_latest_quote.return_value = {
        "bid_price": 100.0,
        "ask_price": 100.10,
    }
    pipeline.broker.submit_order.return_value = {
        "id": "ord-quote",
        "status": "accepted",
    }
    ctx = _ctx(
        [
            TradeDecision(
                action="BUY",
                symbol="SPY",
                allocation_pct=10,
                entry_price=100.0,
                stop_loss=95.0,
                take_profit=112.0,
                reasoning="clean",
            )
        ]
    )

    ExecutionStage(pipeline=pipeline).run(ctx)

    limit_price = pipeline.broker.submit_order.call_args.kwargs["limit_price"]
    assert limit_price > 100.10  # marketable through the displayed ask
    # Cap raised 25bp -> 40bp on 2026-08-27 (see MAX_ENTRY_SLIPPAGE_BPS). The
    # property under test is unchanged: the limit crosses the offer and stays
    # bounded. Only the bound moved.
    assert limit_price <= 100.40


def test_evidence_failure_never_blocks_the_skip_decision():
    """Trading-core rule: forensic persistence failure must not change
    deterministic behavior — the skip still happens, the run continues."""
    pipeline = _pipeline(live_price=100.0, cash=50.0)
    pipeline.db.insert_specialist_evidence.side_effect = RuntimeError("disk full")
    ctx = _ctx(
        [
            TradeDecision(
                action="BUY",
                symbol="XLE",
                allocation_pct=10,
                entry_price=100.0,
                stop_loss=95.0,
                take_profit=115.0,
                reasoning="r",
            )
        ],
        cash=50.0,
    )

    orders = ExecutionStage(pipeline=pipeline).run(ctx)

    assert orders == []
    assert [s["reason"] for s in ctx.execution_skips] == ["insufficient_cash"]


def test_buys_unfunded_does_not_repeat_paid_stack_in_main():
    import main as main_mod

    assert "buys_unfunded" not in main_mod._RETRYABLE_RESULT_STATUSES
    assert "agent_failure" not in main_mod._RETRYABLE_RESULT_STATUSES


# ---------------------------------------------------------------------------
# The VLO no-fill, 2026-08-27 — an order that could never have filled
# ---------------------------------------------------------------------------


def test_the_limit_is_a_ceiling_not_a_haggled_price(limit_entries):
    """The VLO shape: reference $349.99, IEX ask $350.96 (28bp above it).

    The limit must be set AT the slippage ceiling, not shaved down toward the
    displayed offer. Alpaca fills a buy limit at the NBBO or better, so a
    higher limit costs nothing and a lower one just fails to fill — which is
    exactly what happened to VLO: `min(ask*1.0005, 25bp cap)` produced $350.86,
    ten cents under the market it was trying to cross.
    """
    pipeline = _pipeline(live_price=349.99)
    pipeline.config.execution.max_entry_slippage_bps = 40.0
    pipeline.broker.get_latest_quote.return_value = {
        "bid_price": 350.86,
        "ask_price": 350.96,
    }
    pipeline.broker.submit_order.return_value = {"id": "o1", "status": "accepted"}
    ctx = _ctx(
        [
            TradeDecision(
                action="BUY",
                symbol="VLO",
                allocation_pct=9,
                entry_price=349.99,
                stop_loss=335.0,
                take_profit=380.0,
                reasoning="strong_buy",
            )
        ]
    )

    ExecutionStage(pipeline=pipeline).run(ctx)

    limit_price = pipeline.broker.submit_order.call_args.kwargs["limit_price"]
    # The ceiling, not the offer: 349.99 * 1.0040.
    assert limit_price == pytest.approx(351.39, abs=0.01)
    assert limit_price > 350.96, "must clear the displayed offer to be fillable"


def test_even_a_tight_ceiling_is_not_shaved_below_the_offer(limit_entries):
    """25bp gives $350.86, BELOW the $350.96 displayed ask. The limit is
    still set at its own ceiling and still sent (board item 183): IEX is not
    the book the order fills against, and a limit at $350.86 cannot pay more
    than $350.86 whatever the displayed offer says. What must NOT happen is
    a limit shaved below its own ceiling."""
    pipeline = _pipeline(live_price=349.99)
    pipeline.config.execution.max_entry_slippage_bps = 25.0
    pipeline.broker.get_latest_quote.return_value = {
        "bid_price": 350.86,
        "ask_price": 350.96,
    }
    pipeline.broker.submit_order.return_value = {"id": "o1", "status": "accepted"}
    ctx = _ctx(
        [
            TradeDecision(
                action="BUY",
                symbol="VLO",
                allocation_pct=9,
                entry_price=349.99,
                stop_loss=335.0,
                take_profit=380.0,
                reasoning="strong_buy",
            )
        ]
    )

    ExecutionStage(pipeline=pipeline).run(ctx)

    assert pipeline.broker.submit_order.called
    limit_price = pipeline.broker.submit_order.call_args.kwargs["limit_price"]
    assert limit_price == pytest.approx(350.86, abs=0.01)
    assert ctx.execution_skips == []


def test_a_quote_far_through_the_cap_is_recorded_and_still_submitted(limit_entries):
    """Board item 183, 2026-09-30. A displayed ask 300bp through a 40bp
    ceiling used to refuse this BUY as `slippage_gated`. It no longer does.

    Every one of the 8 firings that gate had on record was the venue, not
    the market: the reference matched what the name was actually trading at
    to within a few bp, the quoted ask sat 392-669bp above the highest price
    it traded anywhere in a +/-15 minute window, and 6 of the 8 were trading
    strictly inside the ceiling they were refused against. The protection
    that remains is the one that was always doing the work — a limit at the
    ceiling cannot fill through the ceiling.
    """
    pipeline = _pipeline(live_price=100.0)
    pipeline.config.execution.max_entry_slippage_bps = 40.0
    pipeline.broker.get_latest_quote.return_value = {
        "bid_price": 102.9,
        "ask_price": 103.0,  # 300bp above reference
    }
    pipeline.broker.submit_order.return_value = {"id": "o1", "status": "accepted"}
    ctx = _ctx(
        [
            TradeDecision(
                action="BUY",
                symbol="SPY",
                allocation_pct=10,
                entry_price=100.0,
                stop_loss=95.0,
                take_profit=112.0,
                reasoning="clean",
            )
        ]
    )

    ExecutionStage(pipeline=pipeline).run(ctx)

    assert pipeline.broker.submit_order.called
    limit_price = pipeline.broker.submit_order.call_args.kwargs["limit_price"]
    assert limit_price == pytest.approx(100.4, abs=0.01)
    assert ctx.execution_skips == []


def test_the_far_through_quote_skip_is_gone_from_the_source():
    """Guard, not decoration: the deleted gate was a constant nobody could
    source, and re-introducing any multiple on the displayed quote would
    silently restore a refusal the desk's own record says was wrong 8 times
    out of 8. No `cap * <n>` or `floor / <n>` comparison may come back.
    """
    import inspect
    import re

    from src.pipeline_stages import ExecutionStage as _Stage

    src = "\n".join(line.split("#", 1)[0] for line in inspect.getsource(_Stage._run_session).splitlines())
    assert not re.search(r"cap\s*\*\s*\d", src), "an entry BUY must not be gated on a multiple of its own ceiling"
    assert not re.search(r"floor\s*/\s*\d", src), "a SHORT must not be gated on a divisor of its own floor"


# ---------------------------------------------------------------------------
# SHORT birth-pricing — fillability parity with the BUY ceiling above.
# Same `max_entry_slippage_bps` as a floor vs the bid; not a new budget.
# ---------------------------------------------------------------------------


def _short_pipeline(live_price=100.0, cash=50_000.0, *, slippage_bps=40.0):
    pipeline = _pipeline(live_price=live_price, cash=cash)
    pipeline.config.execution.max_entry_slippage_bps = slippage_bps
    pipeline.broker.get_shortability.return_value = {
        "shortable": True,
        "easy_to_borrow": True,
        "reason": "eligible",
    }
    pipeline.broker.submit_order.return_value = {
        "id": "ord-short",
        "status": "accepted",
    }
    return pipeline


def _short_decision(symbol="NKE", entry=100.0, stop=105.0, target=90.0):
    return TradeDecision(
        action="SHORT",
        symbol=symbol,
        allocation_pct=10,
        entry_price=entry,
        stop_loss=stop,
        take_profit=target,
        reasoning="short birth-pricing",
    )


def test_short_limit_is_a_floor_marketable_versus_the_bid(limit_entries):
    """The NKE shape: last ~ reference, bid a few bp below, short limit
    parked at the last — a sell limit above the bid cannot fill.

    The limit must be set AT the existing slippage floor
    (`reference * (1 - max_entry_slippage_bps/10000)`), not shaved up
    toward the displayed bid, and not at an invented 1% haircut. Alpaca
    fills a short at the NBBO or better, so a lower floor costs nothing
    extra when the bid is inside it and is what makes the order marketable.
    """
    pipeline = _short_pipeline(live_price=100.0, slippage_bps=40.0)
    pipeline.broker.get_latest_quote.return_value = {
        "bid_price": 99.90,
        "ask_price": 100.10,
    }
    ctx = _ctx([_short_decision()])

    ExecutionStage(pipeline=pipeline).run(ctx)

    limit_price = pipeline.broker.submit_order.call_args.kwargs["limit_price"]
    # The floor, not the bid: 100.0 * (1 - 40/10000) = 99.60.
    assert limit_price == pytest.approx(99.60, abs=0.01)
    assert limit_price < 99.90, "must sit at or through the bid to be fillable"
    # Not Alpaca staff's ~1% example, which would have been $99.00.
    assert limit_price == pytest.approx(100.0 * (1 - 40 / 10_000.0), abs=0.01)
    assert ctx.execution_skips == []


def test_short_uses_the_configured_bps_not_a_new_constant(limit_entries):
    """25bp floor at $100 is $99.75 — the configured bound, not 40 and not 1%."""
    pipeline = _short_pipeline(live_price=100.0, slippage_bps=25.0)
    pipeline.broker.get_latest_quote.return_value = {
        "bid_price": 99.90,
        "ask_price": 100.10,
    }
    ctx = _ctx([_short_decision()])

    ExecutionStage(pipeline=pipeline).run(ctx)

    limit_price = pipeline.broker.submit_order.call_args.kwargs["limit_price"]
    assert limit_price == pytest.approx(99.75, abs=0.01)


def test_short_records_and_still_submits_when_the_bid_is_through_the_floor(limit_entries):
    """Mirror of the BUY case. The old `bid < floor / 1.02` inherited the
    BUY multiple rather than adding a second one, and it goes for the same
    reason: a sell-short limit at the floor cannot fill below the floor, so
    refusing on this account's routinely-absurd IEX quote only loses the
    entries where the venue was wrong."""
    pipeline = _short_pipeline(live_price=100.0, slippage_bps=40.0)
    pipeline.broker.get_latest_quote.return_value = {
        "bid_price": 97.00,
        "ask_price": 97.10,  # 300bp below reference
    }
    ctx = _ctx([_short_decision()])

    ExecutionStage(pipeline=pipeline).run(ctx)

    assert pipeline.broker.submit_order.called
    limit_price = pipeline.broker.submit_order.call_args.kwargs["limit_price"]
    assert limit_price == pytest.approx(99.60, abs=0.01)
    assert ctx.execution_skips == []


def test_short_within_iex_noise_still_submits_at_the_floor(limit_entries):
    """Mirror of the BUY tight-ceiling case: bid a little through the floor
    but inside the existing 2% IEX-noise multiple is still submitted at the
    floor — it may fill against a better NBBO than IEX shows. What must NOT
    happen is a limit shaved up toward the bid, or an unbound order."""
    pipeline = _short_pipeline(live_price=100.0, slippage_bps=40.0)
    pipeline.broker.get_latest_quote.return_value = {
        "bid_price": 98.50,
        "ask_price": 98.60,
    }
    ctx = _ctx([_short_decision()])

    ExecutionStage(pipeline=pipeline).run(ctx)

    assert pipeline.broker.submit_order.called
    limit_price = pipeline.broker.submit_order.call_args.kwargs["limit_price"]
    assert limit_price == pytest.approx(99.60, abs=0.01)
    assert ctx.execution_skips == []


def test_short_still_lowers_to_market_when_quote_has_no_bid(limit_entries):
    """Degraded quote: keep the pre-existing lower-to-market SHORT path.
    Limit 101 vs last 100 is inside the 5% stale gate, so pull it down to
    last — not skip, not invent a floor without a bid."""
    pipeline = _short_pipeline(live_price=100.0, slippage_bps=40.0)
    pipeline.broker.get_latest_quote.return_value = {
        "bid_price": None,
        "ask_price": 100.10,
    }
    ctx = _ctx([_short_decision(entry=101.0, stop=106.0, target=90.0)])

    ExecutionStage(pipeline=pipeline).run(ctx)

    limit_price = pipeline.broker.submit_order.call_args.kwargs["limit_price"]
    assert limit_price == pytest.approx(100.0, abs=0.01)
    assert ctx.execution_skips == []


def test_short_stale_entry_still_skips():
    """The >5% stale-entry gate is unchanged for shorts."""
    pipeline = _short_pipeline(live_price=100.0)
    ctx = _ctx([_short_decision(entry=120.0, stop=126.0, target=90.0)])

    orders = ExecutionStage(pipeline=pipeline).run(ctx)

    assert orders == []
    assert [s["reason"] for s in ctx.execution_skips] == ["stale_entry"]
    pipeline.broker.submit_order.assert_not_called()
