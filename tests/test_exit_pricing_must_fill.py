"""Every ordinary decided exit is a plain DAY MARKET order, measured at submit.

2026-10-09 09:32 ET: SELL limits on NET, AAPL and VLO, priced at the last-trade
mark less 0.5%, sat unfilled for the whole wait and were cancelled with the
stops restored -- decided exits that never happened. These pin the fix: no
limit on an ordinary SELL or COVER, the live bid/ask recorded beside the order,
a failed quote read never blocking the exit, and the emergency de-lever's own
live-bid pricer left exactly as it was.
"""

from unittest.mock import MagicMock, patch

from src.exit_quote import read_exit_quote
from src.models import PortfolioDecision, Position, TradeDecision
from src.pipeline_context import RunContext
from src.pipeline_stages import ExecutionStage
from tests.fakes.held_book import hold
from tests.test_pipeline_stages import _mock_stage_seam, _mock_stop_seam, _pm_rc, _sell


def _pipeline(position, order_id):
    pipeline = MagicMock()
    _mock_stop_seam(pipeline.broker)
    hold(pipeline.broker, {position.symbol: position.qty})  # the broker holds what the exit sells or covers
    _mock_stage_seam(pipeline)
    pipeline.broker.submit_order.return_value = {"id": order_id, "status": "accepted", "symbol": position.symbol}
    pipeline.broker.wait_for_order_terminal.return_value = "filled"
    pipeline.broker.get_latest_quote.return_value = {"bid_price": 99.0, "ask_price": 101.0}
    pipeline._order_accepted.return_value = True
    pipeline._format_qty = lambda q: str(q)
    pipeline._full_sell_qty = lambda q: q
    pipeline.db = MagicMock()
    pipeline._refresh_account_state.return_value = (
        {"cash": 20_000.0, "portfolio_value": 60_000.0},
        [position],
        {position.symbol: position.current_price},
    )
    return pipeline


def _ctx(position, decision):
    ctx = RunContext.start("morning")
    ctx.cash, ctx.total_value, ctx.last_equity = 10_000.0, 50_000.0, 50_000.0
    ctx.positions = [position]
    ctx.portfolio_decision = PortfolioDecision(reasoning_chain=_pm_rc(), decisions=[decision], portfolio_view="t")
    ctx.symbols_bars = {}
    return ctx


LONG = Position(
    symbol="NET",
    qty=10.0,
    avg_entry=300.0,
    current_price=349.27,
    market_value=3492.7,
    unrealized_pnl=0.0,
    sector="Technology",
)
SHORT = Position(
    symbol="TSLA",
    qty=-20.0,
    avg_entry=200.0,
    current_price=210.0,
    market_value=-4200.0,
    unrealized_pnl=0.0,
    sector="Consumer Cyclical",
)
COVER = TradeDecision(
    action="COVER",
    symbol="TSLA",
    allocation_pct=100.0,
    entry_price=200.0,
    stop_loss=220.0,
    take_profit=180.0,
    reasoning="cover",
)


def _run(position, decision, order_id):
    pipeline = _pipeline(position, order_id)
    with patch("src.stage_execution_parts.sell_loop._record_pipeline_event") as events:
        ExecutionStage(pipeline=pipeline).run(_ctx(position, decision))
    return pipeline, events


def _order_event(events):
    return next(c.kwargs for c in events.call_args_list if c.args[3:5] == ("order", "submitted"))


def test_decided_sell_is_a_market_order_with_its_quote_recorded():
    pipeline, events = _run(LONG, _sell("NET"), "sell-1")
    kwargs = pipeline.broker.submit_order.call_args.kwargs
    assert kwargs["side"] == "sell" and kwargs["limit_price"] is None
    event = _order_event(events)
    assert (event["quote_bid"], event["quote_ask"]) == (99.0, 101.0)


def test_decided_cover_is_a_market_order_with_its_quote_recorded():
    pipeline, events = _run(SHORT, COVER, "cover-1")
    kwargs = pipeline.broker.submit_order.call_args.kwargs
    assert kwargs["side"] == "buy" and kwargs["limit_price"] is None
    event = _order_event(events)
    assert (event["quote_bid"], event["quote_ask"]) == (99.0, 101.0)


def test_a_failed_quote_read_still_sells():
    pipeline = _pipeline(LONG, "sell-1")
    pipeline.broker.get_latest_quote.side_effect = RuntimeError("feed down")
    with patch("src.stage_execution_parts.sell_loop._record_pipeline_event") as events:
        ExecutionStage(pipeline=pipeline).run(_ctx(LONG, _sell("NET")))
    pipeline.broker.submit_order.assert_called_once()
    assert pipeline.broker.submit_order.call_args.kwargs["limit_price"] is None
    event = _order_event(events)
    assert (event["quote_bid"], event["quote_ask"]) == (None, None)


def test_read_exit_quote_never_raises_and_drops_unusable_sides():
    broker = MagicMock()
    broker.get_latest_quote.return_value = {"bid_price": 0, "ask_price": "101.5"}
    assert read_exit_quote(broker, "NET") == {"bid": None, "ask": 101.5}
    broker.get_latest_quote.side_effect = RuntimeError("feed down")
    assert read_exit_quote(broker, "NET") == {"bid": None, "ask": None}


def test_emergency_delever_keeps_its_live_bid_pricer():
    from src.delever.ladder import DeleverLadder

    broker = MagicMock()
    broker.get_latest_quote.return_value = {"bid_price": 99.0, "ask_price": 101.0}
    ladder = DeleverLadder(broker=broker)
    assert ladder._live_delever_price("NET", "sell") == (99.0, 100.0)
    assert ladder._live_delever_price("NET", "buy") == (101.0, 100.0)
