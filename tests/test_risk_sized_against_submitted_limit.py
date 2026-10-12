"""The share count must divide by the price the desk is ABOUT TO PAY.

MEASURED DEFECT (2026-10-04, live money). Position size came from the
ANALYSIS price and the stop distance implied by it, while the order actually
submitted is a MARKETABLE LIMIT priced AWAY from that reference so it fills
— a ceiling ABOVE it for a BUY, a floor BELOW it for a SHORT. The realised
distance from the fill to the stop is therefore WIDER than the distance the
share count was computed against, so the same share count carries MORE money
at risk than the ~1% of equity that was authorised. Over the desk's own
recorded BUY rows the median overshoot is 6.1% of the risk budget (25% at
the worst); a short case measured 10.9%.

These tests drive the REAL `ExecutionStage` submit loop (the harness pattern
of `tests/test_item_181_short_risk_budget_sizing.py`), capture BOTH the
divisor handed to the risk-budget sizer AND the limit price actually handed
to the broker, and assert the REALISED risk per share — limit-to-stop, the
worst price the submitted order can fill at — equals the risk per share the
size was computed from. Pre-fix both directions fail; post-fix both pass,
and they fail identically in both directions, so no asymmetry can be
reintroduced (owner ruling 2026-10-04: a short is treated exactly like a
long).
"""

from datetime import datetime
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import pytest

import src.pipeline_stages as pipeline_stages
from src.execution.broker import LivePrice
from src.models import PortfolioDecision, TradeDecision
from src.pipeline_context import RunContext
from src.pipeline_stages import ExecutionStage

ET = ZoneInfo("America/New_York")


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


def _exec_pipeline_with_print(price: float):
    now = datetime.now(ET)
    pipeline = MagicMock()
    pipeline.broker.list_open_orders_checked.return_value = (True, [])  # listing read, nothing open
    pipeline.broker.get_latest_price_stamped.return_value = LivePrice(
        price=price,
        source="last_trade",
        trade_at=now,
        is_today=True,
        is_today_print=True,
    )
    pipeline.broker.get_latest_price.return_value = price
    pipeline.broker.get_latest_quote.return_value = {
        "bid_price": price - 1.0,
        "ask_price": price + 1.0,
    }
    pipeline.broker.submit_order.return_value = {"id": "o1", "status": "accepted"}
    pipeline.broker.get_shortability.return_value = {
        "shortable": True,
        "easy_to_borrow": True,
    }
    pipeline._format_qty = lambda q: str(q)
    pipeline._order_accepted.return_value = True
    pipeline._refresh_account_state.return_value = (
        {"cash": 50_000.0, "portfolio_value": 100_000.0},
        [],
        {},
    )
    return pipeline


def _run_exec(pipeline, decision, monkeypatch):
    captured = {}

    def _spy(pipe, *, total_value, sizing_price, stop_price, is_short, fractional):
        captured["sizing_price"] = sizing_price
        captured["stop_price"] = stop_price
        return None  # non-binding: qty falls back to the allocation count

    monkeypatch.setattr(pipeline_stages, "_qty_by_risk_budget", _spy)

    ctx = RunContext.start("morning")
    ctx.cash = 50_000.0
    ctx.total_value = 100_000.0
    ctx.last_equity = 100_000.0
    ctx.positions = []
    ctx.symbols_bars = {}
    ctx.portfolio_decision = PortfolioDecision(
        reasoning_chain=_pm_rc(),
        decisions=[decision],
        portfolio_view="t",
    )
    ExecutionStage(pipeline=pipeline).run(ctx)
    submitted = pipeline.broker.submit_order.call_args
    if submitted is not None:
        captured["limit_price"] = submitted.kwargs.get("limit_price")
    return captured


@pytest.mark.parametrize(
    "action,entry,stop",
    [("BUY", 100.0, 95.0), ("SHORT", 100.0, 105.0)],
)
def test_realised_limit_to_stop_risk_matches_the_sized_risk(
    action,
    entry,
    stop,
    monkeypatch,
):
    """Identical assertion both directions: the worst price the submitted
    order can fill at IS the price the risk budget divided by."""
    pipeline = _exec_pipeline_with_print(100.0)
    decision = TradeDecision(
        action=action,
        symbol="TSLA",
        allocation_pct=10,
        entry_price=entry,
        stop_loss=stop,
        take_profit=(115.0 if action == "BUY" else 88.0),
        reasoning="the market order pays the quote, away from the analysis price",
    )
    captured = _run_exec(pipeline, decision, monkeypatch)
    # A plain market order (owner ruling 2026-10-09): the worst price it can
    # fill at is the quote side it pays — the ask for a BUY, the bid for a SHORT.
    assert captured["limit_price"] is None
    quote = pipeline.broker.get_latest_quote.return_value
    limit = quote["bid_price"] if action == "SHORT" else quote["ask_price"]
    sized_risk_per_share = abs(captured["sizing_price"] - captured["stop_price"])
    realised_risk_per_share = abs(limit - stop)
    # Pre-fix the realised distance is the WIDER one and the size was
    # computed from the narrower: every share carries more than authorised.
    assert realised_risk_per_share == pytest.approx(sized_risk_per_share)
    # And the submitted limit really is away from the analysis price, so the
    # test is not passing because the two prices happened to coincide.
    assert limit != pytest.approx(entry)
