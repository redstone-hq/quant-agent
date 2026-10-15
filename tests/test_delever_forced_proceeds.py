"""A forced whole-position sale counts its proceeds at the live sell price (no made-up 3% markdown)."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from src.config import RiskConfig
from src.models import Position
from src.pipeline_context import RunContext
from tests.pipeline_factory import build_pipeline


def _pos(symbol, qty, price, pnl):
    return Position(
        symbol=symbol,
        qty=qty,
        avg_entry=price,
        current_price=price,
        market_value=qty * price,
        unrealized_pnl=pnl,
        sector="Tech",
    )


def test_whole_position_proceeds_equal_qty_times_quote():
    p = build_pipeline()
    p.config = SimpleNamespace(
        cash_sweep=SimpleNamespace(enabled=False, symbol="SGOV"),
        risk=RiskConfig(max_position_pct=20, max_total_position_pct=90, require_stop_loss=True, allow_margin=False),
    )
    p.broker = MagicMock()
    p.broker.get_account.return_value = {"cash": 10.0, "portfolio_value": 90_000.0}
    p.broker.get_positions.return_value = []
    p.broker.get_latest_quote.return_value = {"bid_price": 100.0, "ask_price": 100.2}
    p.db = MagicMock()
    p._submit_protected_sell = MagicMock(return_value=({"id": "s1", "status": "accepted"}, {"symbol": "A"}))
    p._finalize_pending_protections = MagicMock()
    p._alert_owner_force_delever_incomplete = MagicMock()

    ctx = RunContext.start("morning")
    # Deficit 995: qty 10 x quote 100 = 1000 covers it. A 0.97 markdown (970) would not, and
    # would force-sell the second name on top.
    ctx.cash = -995.0
    ctx.positions = [_pos("AAA", 10, 100.0, -50.0), _pos("BBB", 10, 100.0, -10.0)]
    p._force_delever(ctx)

    sold = [c.kwargs["symbol"] for c in p._submit_protected_sell.call_args_list]
    assert sold == ["AAA"], sold
    assert p._alert_owner_force_delever_incomplete.call_count == 0
