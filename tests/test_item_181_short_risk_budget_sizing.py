"""item 181: a SHORT's RISK-BUDGET divisor must be the today print, never a
stale higher analyst entry.

docs/WORK.md item 181. `sizing_price` in the submit loop is
`max(today_print, approved_entry)` — deliberately conservative on the
ALLOCATION path in both directions (a higher divisor means fewer shares,
either less capital on a buy or a smaller short). But the RISK-BUDGET path
divides `budget / abs(sizing_price - stop)`, and for a SHORT
(`risk_per_share = stop - price`) a HIGHER price NARROWS that spread. When
the analyst's `entry` sits above today's `print`, using the entry for the
risk-budget divisor understates `risk_per_share` and INFLATES `qty_by_risk`,
letting the short exceed its ratified risk budget by roughly
`(stop - print) / (stop - entry)`. It is bounded by the allocation-path
`min()` cap, so it is not unbounded, but it is a real, untested overshoot on
the risk-budget path specifically.

This test drives the REAL `ExecutionStage` submit loop (same harness
pattern as `tests/test_item_120_buy_price_sizing.py`), spies on
`_qty_by_risk_budget` to capture the `sizing_price` it is actually called
with, and shows that a SHORT with `entry_price` above the today print must
be risk-sized off the print — not the entry.
"""

from datetime import datetime
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

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
    """A MagicMock pipeline whose broker returns a REAL today last-trade
    LivePrice (so the sizing gate uses it), a crossable quote, and accepts
    orders — enough to drive the real SHORT submit loop."""
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
    pipeline.broker.submit_order.return_value = {
        "id": "o1",
        "status": "accepted",
    }
    # D6 borrow gate: pass it so a SHORT actually reaches the real
    # submit-loop sizing code below, instead of being skipped before it.
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
    """Drive the real ExecutionStage submit loop, capturing the sizing_price
    actually handed to the risk-budget sizer (the risk-budget divisor)."""
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


def test_short_risk_budget_sizes_off_the_print_not_a_stale_higher_entry(monkeypatch):
    """The exact item 181 defect: analyst entry (104, inside the 5%
    stale-entry band around the print) sits above today's print (100); the
    risk-budget divisor must still be the print, or `risk_per_share` is
    understated and `qty_by_risk` is inflated beyond the ratified budget."""
    pipeline = _exec_pipeline_with_print(100.0)
    short = TradeDecision(
        action="SHORT",
        symbol="TSLA",
        allocation_pct=10,
        entry_price=104.0,
        stop_loss=120.0,
        take_profit=88.0,
        reasoning="short new name, stale higher entry",
    )
    captured = _run_exec(pipeline, short, monkeypatch)
    # BUG (pre-fix): sizing_price == 104.0 (the entry), risk_per_share == 16,
    # understating the correct 20 -> qty_by_risk 25% over the ratified budget.
    # The item 181 guarantee STANDS and is now stronger (2026-10-04): the
    # divisor is the marketable floor actually submitted, which is derived
    # from the today print and is never the stale analyst entry. Sizing
    # against the submitted limit is what makes the realised fill-to-stop
    # risk equal the risk the share count was computed from — see
    # tests/test_risk_sized_against_submitted_limit.py.
    assert captured["limit_price"] is None  # a plain market order (2026-10-09)
    assert captured["sizing_price"] == 99.0  # the live bid the short sells at
    assert captured["sizing_price"] < 100.0  # never the 104 analyst entry
    assert captured["stop_price"] == 120.0
    risk_per_share = abs(captured["sizing_price"] - captured["stop_price"])
    assert risk_per_share > 20.0  # not 16.0 (stop - stale entry)


def test_short_risk_budget_unaffected_when_entry_is_at_or_below_the_print(monkeypatch):
    """No regression: when the entry does not sit above the print,
    `max(print, entry)` already equals the print, so the risk-budget
    divisor is unchanged."""
    pipeline = _exec_pipeline_with_print(100.0)
    short = TradeDecision(
        action="SHORT",
        symbol="TSLA",
        allocation_pct=10,
        entry_price=98.0,
        stop_loss=120.0,
        take_profit=80.0,
        reasoning="short new name, entry below print",
    )
    captured = _run_exec(pipeline, short, monkeypatch)
    # Now anchored to the submitted marketable floor (itself computed off
    # the print), so the entry still cannot reach the divisor.
    assert captured["limit_price"] is None
    assert captured["sizing_price"] == 99.0  # the live bid


def test_buy_risk_budget_still_sizes_off_the_max_conservative_divisor(monkeypatch):
    """The BUY path must be untouched by the item 181 fix: risk-budget
    sizing still uses the same conservative `max(print, entry)` divisor
    (higher entry WIDENS risk_per_share for a buy, which is already the
    conservative direction)."""
    pipeline = _exec_pipeline_with_print(100.0)
    buy = TradeDecision(
        action="BUY",
        symbol="TSLA",
        allocation_pct=10,
        entry_price=103.0,
        stop_loss=94.0,
        take_profit=130.0,
        reasoning="buy new name, entry above print",
    )
    captured = _run_exec(pipeline, buy, monkeypatch)
    # 2026-10-04: the BUY divisor is the submitted ceiling — the WORST price
    # this order can actually fill at. `max(print, entry)` was a price the
    # order could never pay, so it was never the right divisor; the ceiling
    # is, and both directions use the same rule.
    assert captured["limit_price"] is None
    assert captured["sizing_price"] == 101.0  # the live ask the buy pays
    assert captured["sizing_price"] > 100.0  # above the print, as a buy must be
