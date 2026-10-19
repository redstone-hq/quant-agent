"""Market entries size, risk and record off the ARRIVAL PRINT, never the IEX quote.

Defect 2026-10-19: a market BUY divided its risk budget by the IEX ask, which
ran 4-5% above every real trade near the open (XOM 2026-10-12 and 14), so the
buy was sized at about half its allotted risk and the trade row stored the
ask as its price. The arrival price is this session's latest trade print;
a print from before today's open refuses the name with `no_arrival_print`.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import src.pipeline_stages as pipeline_stages
import src.stage_execution as stage_execution
from src.execution.broker import LivePrice
from src.models import PortfolioDecision, ReasoningChain, TradeDecision
from src.pipeline_context import RunContext
from src.pipeline_stages import ExecutionStage
from src.stage_execution_parts import entry_order_pricing as pricing
from src.stage_execution_parts.state import SKIP, EntryLeg, EntryRun
from src.trading_calendar import ET

PRINT = 100.0  # this session's latest trade print
ASK = 105.0  # the IEX ask, 5% above the print (the XOM shape)
BID = 95.0  # the IEX bid, 5% below it


def _broker(trade_at, opened):
    live = LivePrice(price=PRINT, source="last_trade", trade_at=trade_at, is_today=True, is_today_print=True)
    return SimpleNamespace(
        get_latest_price_stamped=lambda symbol: live,
        get_session_open=lambda on_date: opened,
        get_session_close=lambda on_date: opened + timedelta(hours=6, minutes=30),
    )


def _run(broker):
    pipeline = SimpleNamespace(broker=broker, config=SimpleNamespace(execution=SimpleNamespace()))
    return EntryRun(
        pipeline=pipeline,
        ctx=SimpleNamespace(),
        submit_queue=[],
        deferred_far_through=set(),
        original_entry_count=1,
        budget_is_gross=True,
        total_value=100_000.0,
    )


def _leg(is_short):
    decision = SimpleNamespace(symbol="ZZZA", action="SHORT" if is_short else "BUY", entry_price=PRINT)
    return EntryLeg(decision, is_short, 0, PRINT, ASK, BID)


@pytest.fixture
def refusals(monkeypatch):
    seen = []
    monkeypatch.setattr(pricing, "_record_execution_skip", lambda p, c, sym, why, detail: seen.append((why, detail)))
    return seen


def _session():
    now = datetime.now(ET)
    return now, now - timedelta(hours=1)


@pytest.mark.parametrize("is_short", [False, True])
def test_a_quote_five_percent_away_sizes_off_the_print(refusals, is_short):
    now, opened = _session()
    limit, sizing, risk = pricing.price_entry(_run(_broker(now, opened)), _leg(is_short), PRINT, PRINT, PRINT)
    assert limit is None, "a market entry carries no limit"
    assert risk == PRINT, "the risk budget divides by the arrival print, never the IEX quote side"
    assert sizing == PRINT
    assert refusals == []


@pytest.mark.parametrize("is_short", [False, True])
def test_a_print_from_before_todays_open_refuses_the_name(refusals, is_short):
    _, opened = _session()
    stale = _broker(opened - timedelta(minutes=1), opened)
    out = pricing.price_entry(_run(stale), _leg(is_short), PRINT, PRINT, PRINT)
    assert out is SKIP
    assert len(refusals) == 1
    why, detail = refusals[0]
    assert why == pricing.NO_ARRIVAL_PRINT
    assert "before today's open" in detail and "refused" in detail


def test_a_quote_only_price_refuses_the_name(refusals):
    now, opened = _session()
    broker = _broker(now, opened)
    mid = LivePrice(price=PRINT, source="quote_mid", trade_at=now, is_today=True, is_today_print=False)
    broker.get_latest_price_stamped = lambda symbol: mid
    assert pricing.price_entry(_run(broker), _leg(False), PRINT, PRINT, PRINT) is SKIP
    assert refusals[0][0] == pricing.NO_ARRIVAL_PRINT


def _pipeline():
    now = datetime.now(ET)
    pipeline = MagicMock()
    pipeline.broker.list_open_orders_checked.return_value = (True, [])
    pipeline.broker.get_latest_price_stamped.return_value = LivePrice(
        price=PRINT, source="last_trade", trade_at=now, is_today=True, is_today_print=True
    )
    pipeline.broker.get_session_open.return_value = now - timedelta(hours=1)
    pipeline.broker.get_latest_price.return_value = PRINT
    pipeline.broker.get_latest_quote.return_value = {"bid_price": BID, "ask_price": ASK}
    pipeline.broker.submit_order.return_value = {"id": "o1", "status": "accepted"}
    pipeline.broker.get_shortability.return_value = {"shortable": True, "easy_to_borrow": True}
    pipeline._format_qty = lambda q: str(q)
    pipeline._order_accepted.return_value = True
    pipeline._refresh_account_state.return_value = ({"cash": 50_000.0, "portfolio_value": 100_000.0}, [], {})
    return pipeline


@pytest.mark.parametrize("action,stop,target", [("BUY", 95.0, 115.0), ("SHORT", 105.0, 88.0)])
def test_the_trade_row_records_the_arrival_print_not_the_quote(monkeypatch, action, stop, target):
    recorded = {}
    real_insert = stage_execution.insert_pending_entry

    def _spy_insert(**kwargs):
        recorded["price"] = kwargs["executed_price"]
        return real_insert(**kwargs)

    monkeypatch.setattr(stage_execution, "insert_pending_entry", _spy_insert)
    monkeypatch.setattr(pipeline_stages, "_qty_by_risk_budget", lambda *a, **k: None)
    pipeline = _pipeline()
    ctx = RunContext.start("morning")
    ctx.cash = ctx.total_value = ctx.last_equity = 100_000.0
    ctx.positions = []
    ctx.symbols_bars = {}
    rc = ReasoningChain(**{k: "x" for k in ReasoningChain.model_fields})
    decision = TradeDecision(
        action=action,
        symbol="ZZZB",
        allocation_pct=10,
        entry_price=PRINT,
        stop_loss=stop,
        take_profit=target,
        reasoning="arrival print, not the IEX quote",
    )
    ctx.portfolio_decision = PortfolioDecision(reasoning_chain=rc, decisions=[decision], portfolio_view="t")
    ExecutionStage(pipeline=pipeline).run(ctx)
    assert pipeline.broker.submit_order.call_args is not None, "the order was submitted"
    assert recorded["price"] == PRINT, "the trade row's price is the arrival print; the fill lands in fill_price"
