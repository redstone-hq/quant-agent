"""Late-but-sound fills path: cut real latency, encode known duration,
refuse a stale ticket if that budget is gone. Catch-up is a safety net
only. Repeg stays off.
"""

import inspect
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from src.config import ExecutionConfig
from src.execution.broker import (
    _ALPACA_STREAM_AUTH_DEADLINE_S,
    _ALPACA_STREAM_RECONNECT_MAX_S,
    TradeStreamWarmup,
)
from src.pipeline_context import RunContext
from src.pipeline_stages import (
    ExecutionStage,
    RiskStage,
    _known_entry_submit_budget_s,
    _live_fill_price,
    _pin_approved_entry_ceilings,
    _repeg_settings,
    _submit_window_overrun,
)
from src.models import PortfolioDecision, ReasoningChain, TradeDecision


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


def test_ensure_trade_updates_does_not_open_a_throwaway_socket():
    from src.execution.broker_parts.trade_stream import TradeStreamWaits

    src = inspect.getsource(TradeStreamWaits.ensure_trade_updates)
    assert "TradingStream(" not in src
    assert "thread.start" not in src
    start_src = inspect.getsource(TradeStreamWaits.start_trade_updates)
    assert "_TradeUpdatesHub" in start_src
    assert "_acquire_trade_updates_slot" in start_src
    assert _ALPACA_STREAM_AUTH_DEADLINE_S == _ALPACA_STREAM_RECONNECT_MAX_S
    assert _ALPACA_STREAM_AUTH_DEADLINE_S <= 30.0
    assert "stop()" not in src


def test_pipeline_owns_the_account_lease_beside_the_db():
    from src.pipeline import TradingPipeline

    src = inspect.getsource(TradingPipeline.__init__)
    assert "trade_updates_lease_path" in src
    assert ".trade_updates.lock" in src


def test_risk_starts_trade_updates_before_review():
    src = inspect.getsource(RiskStage._run_review)
    start_at = src.find("_start_trade_updates_early")
    review_at = src.find("risk_manager.review")
    assert start_at != -1 and review_at != -1 and start_at < review_at


def test_repeg_stays_off_by_default():
    assert ExecutionConfig().repeg_enabled is False
    pipeline = SimpleNamespace(config=SimpleNamespace(execution=ExecutionConfig()))
    assert _repeg_settings(pipeline) is None


def test_live_fill_price_never_returns_a_bar_close():
    pipeline = MagicMock()
    pipeline.broker.list_open_orders_checked.return_value = (True, [])  # listing read, nothing open
    pipeline.broker.get_latest_price.return_value = None
    assert _live_fill_price(pipeline, "AAPL") is None
    pipeline.broker.get_latest_price.return_value = 334.78
    assert _live_fill_price(pipeline, "AAPL") == 334.78


def test_approved_ceiling_is_pinned_from_live_ref_not_later_tape():
    pipeline = MagicMock()
    pipeline.broker.list_open_orders_checked.return_value = (True, [])  # listing read, nothing open
    pipeline.broker.get_latest_price.return_value = 100.0
    pipeline.config.execution = ExecutionConfig(max_entry_slippage_bps=40.0)
    ctx = RunContext.start("morning")
    decision = TradeDecision(
        symbol="AAPL",
        action="BUY",
        allocation_pct=5.0,
        entry_price=100.0,
        stop_loss=95.0,
        take_profit=110.0,
        reasoning="r",
    )
    _pin_approved_entry_ceilings(pipeline, ctx, [decision])
    assert ctx.approved_entry_ceiling["AAPL"] == 100.0 * (1 + 40.0 / 10_000.0)
    # A later last-trade must not raise the cap.
    pipeline.broker.get_latest_price.return_value = 110.0
    assert ctx.approved_entry_ceiling["AAPL"] == 100.0 * (1 + 40.0 / 10_000.0)


def test_known_budget_omits_auth_when_socket_already_started():
    pipeline = SimpleNamespace(
        broker=SimpleNamespace(trade_updates_started=lambda: True),
    )
    assert _known_entry_submit_budget_s(pipeline) == 0.0
    pipeline.broker.trade_updates_started = lambda: False
    pipeline.broker.trade_updates_lease_contended = lambda: False
    assert _known_entry_submit_budget_s(pipeline) == _ALPACA_STREAM_AUTH_DEADLINE_S


def test_known_budget_omits_auth_when_another_process_owns_the_lease():
    pipeline = SimpleNamespace(
        broker=SimpleNamespace(
            trade_updates_started=lambda: False,
            trade_updates_lease_contended=lambda: True,
        ),
    )
    assert _known_entry_submit_budget_s(pipeline) == 0.0


def test_submit_window_overrun_is_deadline_not_price():
    ctx = RunContext.start("morning")
    ctx.entry_submit_deadline_mono = time.monotonic() - 1.0
    assert _submit_window_overrun(ctx) is True
    ctx.entry_submit_deadline_mono = time.monotonic() + 30.0
    assert _submit_window_overrun(ctx) is False
    ctx.entry_submit_deadline_mono = None
    assert _submit_window_overrun(ctx) is False


def _buy_pipeline(ask, live=100.0, *, stall=False):
    pipeline = MagicMock()
    pipeline.broker.list_open_orders_checked.return_value = (True, [])  # listing read, nothing open
    pipeline.broker.get_latest_price.return_value = live
    pipeline.broker.get_latest_quote.return_value = {
        "ask_price": ask,
        "bid_price": live - 0.1,
    }
    pipeline.broker.ensure_trade_updates.return_value = TradeStreamWarmup(
        ready=not stall,
        handshake_failed=stall,
        retried=stall,
    )
    pipeline.broker.trade_updates_started.return_value = True
    pipeline.config.execution = ExecutionConfig(
        max_entry_slippage_bps=40.0,
        repeg_enabled=False,
    )
    pipeline._format_qty = lambda q: str(q)
    pipeline._order_accepted.return_value = True
    pipeline._refresh_account_state.return_value = (
        {"cash": 50_000.0, "portfolio_value": 100_000.0},
        [],
        {},
    )
    pipeline.broker.submit_order.return_value = {"id": "o1", "status": "accepted"}
    pipeline._sweeper = None
    pipeline.db = MagicMock()
    return pipeline


def _buy_ctx(entry=100.0):
    ctx = RunContext.start("morning")
    ctx.positions = []
    ctx.cash = 50_000.0
    ctx.total_value = 100_000.0
    ctx.last_equity = 100_000.0
    ctx.symbols_bars = {}
    ctx.portfolio_decision = PortfolioDecision(
        reasoning_chain=_rc(),
        decisions=[
            TradeDecision(
                symbol="AAPL",
                action="BUY",
                allocation_pct=5.0,
                entry_price=entry,
                stop_loss=95.0,
                take_profit=110.0,
                reasoning="r",
            ),
        ],
        portfolio_view="t",
    )
    return ctx


def test_a_displayed_ask_through_the_ceiling_no_longer_refuses_the_entry(limit_entries):
    """Board item 183, 2026-09-30. A 1000bp IEX ask against a 40bp ceiling
    used to refuse this BUY outright. It no longer does: the order goes at
    the ceiling, which it cannot fill through, and the far-through quote is
    a recorded fact rather than a trading decision.

    The measurement that removed the old gate: all 8 firings it had on
    record were venue noise, not a market that had run — in every one the
    reference matched the price the name was actually trading at, while the
    quoted ask sat 392-669bp above the highest price it traded anywhere in a
    +/-15 minute window.
    """
    pipeline = _buy_pipeline(ask=110.0, live=100.0, stall=False)
    ctx = _buy_ctx()
    ExecutionStage(pipeline=pipeline).run(ctx)
    reasons = [s["reason"] for s in ctx.execution_skips]
    assert "slippage_gated" not in reasons
    assert "latency_window" not in reasons
    assert pipeline.broker.submit_order.call_count == 1
    limit = pipeline.broker.submit_order.call_args.kwargs["limit_price"]
    assert limit == pytest.approx(100.0 * (1 + 40.0 / 10_000.0), abs=0.01)


def test_a_desk_stall_still_refuses_before_the_quote_is_ever_read():
    """`latency_window` survives on its own path — the submit-window
    overrun check, which runs before any quote comparison and is untouched
    by item 183. A handshake stall alone is not that overrun, so this BUY
    is submitted; the overrun case has its own test below."""
    pipeline = _buy_pipeline(ask=110.0, live=100.0, stall=True)
    ctx = _buy_ctx()
    ExecutionStage(pipeline=pipeline).run(ctx)
    reasons = [s["reason"] for s in ctx.execution_skips]
    assert "slippage_gated" not in reasons
    assert pipeline.broker.submit_order.call_count == 1


def test_overrun_refuses_inside_ceiling_as_latency_window(monkeypatch):
    pipeline = _buy_pipeline(ask=100.2, live=100.0, stall=False)
    ctx = _buy_ctx()

    def _past_deadline(_pipeline, _ctx):
        _ctx.entry_submit_budget_s = 0.1
        _ctx.entry_submit_started_mono = time.monotonic() - 10.0
        _ctx.entry_submit_deadline_mono = time.monotonic() - 5.0

    monkeypatch.setattr(
        "src.pipeline_stages._encode_entry_submit_window",
        _past_deadline,
    )
    ExecutionStage(pipeline=pipeline).run(ctx)
    reasons = [s["reason"] for s in ctx.execution_skips]
    assert "latency_window" in reasons
    assert any("latency blew the window" in (s.get("detail") or "") for s in ctx.execution_skips)
    pipeline.broker.submit_order.assert_not_called()
    assert ctx.catch_up_used.get("AAPL") is not True


def test_catch_up_is_safety_net_after_stall_when_original_would_miss(limit_entries):
    pipeline = _buy_pipeline(ask=100.2, live=100.0, stall=True)
    ctx = _buy_ctx()
    ExecutionStage(pipeline=pipeline).run(ctx)
    assert ctx.desk_latency_stall is True
    assert ExecutionConfig().repeg_enabled is False
    assert _repeg_settings(pipeline) is None
    # Safety net: original $100 would sit under the live offer; cap still covers it.
    assert ctx.catch_up_used.get("AAPL") is True
    assert pipeline.broker.submit_order.called
    submitted = pipeline.broker.submit_order.call_args
    limit = (submitted.kwargs or {}).get("limit_price")
    assert ctx.approved_entry_ceiling["AAPL"] == 100.0 * (1 + 40.0 / 10_000.0)
    if limit is not None:
        assert float(limit) <= ctx.approved_entry_ceiling["AAPL"] + 0.0001


def test_healthy_path_does_not_stamp_catch_up():
    pipeline = _buy_pipeline(ask=100.2, live=100.0, stall=False)
    ctx = _buy_ctx()
    ExecutionStage(pipeline=pipeline).run(ctx)
    assert ctx.catch_up_used.get("AAPL") is not True
    assert pipeline.broker.submit_order.called
    assert ExecutionConfig().repeg_enabled is False
    # Funding did not run; hub already started — no leftover fund-max slack.
    assert ctx.entry_submit_budget_s == 0.0
    assert ctx.entry_submit_deadline_mono is None


def test_stall_does_not_stamp_catch_up_when_original_still_fillable():
    pipeline = _buy_pipeline(ask=100.0, live=100.0, stall=True)
    ctx = _buy_ctx()
    ExecutionStage(pipeline=pipeline).run(ctx)
    assert ctx.desk_latency_stall is True
    assert ctx.catch_up_used.get("AAPL") is not True
    assert pipeline.broker.submit_order.called


def test_no_price_does_not_fall_back_to_morning_bar():
    pipeline = _buy_pipeline(ask=100.0, live=100.0, stall=False)
    pipeline.broker.get_latest_price.return_value = None
    ctx = _buy_ctx()
    bar = SimpleNamespace(close=334.0)
    ctx.symbols_bars = {"AAPL": [bar]}
    ExecutionStage(pipeline=pipeline).run(ctx)
    reasons = [s["reason"] for s in ctx.execution_skips]
    assert "no_price" in reasons
    pipeline.broker.submit_order.assert_not_called()
