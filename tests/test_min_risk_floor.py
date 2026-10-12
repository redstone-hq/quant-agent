"""Owner rule 2026-08-27 (owner-ruled): risk per new position is 0.5-5% of
equity; below 0.5% the desk does not trade.

The allocator applied the floor to the GRANT, but every shrink after it (the
gross-exposure ceiling, the execution cash re-size) could cut an entry below
it and still let it out, and a short add was gated on a flat $500 notional
instead (an arbitrary number: Alpaca takes fractional orders from $1, no
commission). These pin the floor as a refusal re-checked after the shrinks,
judged on risk, and never applied to a close.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from src.models import Position
from src.portfolio_constructor import PortfolioConstructor
from src.portfolio_constructor.config import STOP_REFUSAL_BELOW_OWNER_MIN_RISK
from src.risk.rules import GrossCeiling
from src.stage_execution import ExecutionStage
from tests.test_risk_based_sizing import EQUITY, _analysis, _risk_target
from tests.test_scale_in import _ctx, _pipeline, _short_cop, _short_cop_position, _shortable


def _ceiling(usd: float) -> GrossCeiling:
    x = usd / EQUITY
    return GrossCeiling(ceiling_x=x, base_x=x, drawdown_pct=None, alert_owner=False, rung="test", reason="test")


def _construct(ceiling_usd: float):
    constructor = PortfolioConstructor()
    decisions = constructor.construct_orders(
        targets=[_risk_target("NVDA", 2.0)],
        positions=[],
        analyses=[_analysis("NVDA", entry=100, stop=90, target=140)],
        total_value=EQUITY,
        price_map={"NVDA": 100.0},
        gross_ceiling=_ceiling(ceiling_usd),
    )
    return decisions, constructor.drain_refusals()


def test_entry_shrunk_by_gross_ceiling_below_floor_is_refused_with_reason():
    # $2,000 of headroom at a 10% stop risks 0.2% of $100k: under the floor.
    decisions, refusals = _construct(2_000.0)
    assert [d for d in decisions if d.symbol == "NVDA" and d.action == "BUY"] == []
    assert refusals["NVDA"]["refusal"] == STOP_REFUSAL_BELOW_OWNER_MIN_RISK
    assert "0.50% minimum" in refusals["NVDA"]["detail"]


def test_entry_shrunk_by_gross_ceiling_at_or_above_floor_passes():
    # $6,000 of headroom at a 10% stop risks 0.6%: at/above the floor.
    decisions, refusals = _construct(6_000.0)
    buys = [d for d in decisions if d.symbol == "NVDA" and d.action == "BUY"]
    assert len(buys) == 1 and 0 < buys[0].allocation_pct <= 6.0
    assert "NVDA" not in refusals


def test_close_is_never_refused_by_the_floor():
    constructor = PortfolioConstructor()
    held = Position(
        symbol="NVDA",
        qty=1.0,
        avg_entry=100.0,
        current_price=100.0,
        market_value=100.0,
        unrealized_pnl=0.0,
        sector="Technology",
    )
    decisions = constructor.construct_orders(
        targets=[_risk_target("NVDA", 0.0)],
        positions=[held],
        analyses=[_analysis("NVDA", entry=100, stop=99, target=140)],
        total_value=EQUITY,
        price_map={"NVDA": 100.0},
        gross_ceiling=_ceiling(1.0),
    )
    assert [d.action for d in decisions if d.symbol == "NVDA"] == ["SELL"]
    assert constructor.drain_refusals().get("NVDA", {}).get("refusal") != STOP_REFUSAL_BELOW_OWNER_MIN_RISK


def test_short_add_of_300_dollars_at_or_above_floor_is_not_skipped():
    """$300 add (3 sh at $100) on a 10-share short with a ~$8-10 stop
    distance risks over 1% of a $10k book: under the deleted $500 floor,
    above the owner's 0.5% minimum risk, so it must go through."""
    held = [_short_cop_position(qty=-10.0)]
    pipeline = _shortable(_pipeline(positions=held))
    pipeline._refresh_account_state.return_value = ({"cash": 10_000.0, "portfolio_value": 10_000.0}, held, {})
    stop = {"id": "bstop-cop", "qty": 10, "stop_price": 108.0}
    pipeline.broker.snapshot_protective_stops.return_value = (True, [stop])
    pipeline.broker.cancel_snapshotted_stops.return_value = MagicMock(cleared=True)
    pipeline.broker.wait_for_order_terminal.side_effect = lambda order_id, *a, **k: (
        "canceled" if "stop" in str(order_id) else "filled"
    )
    pipeline.broker.submit_order.return_value = {
        "id": "sell-cop",
        "status": "accepted",
        "symbol": "COP",
        "pending_stop_price": 110.0,
    }
    pipeline.broker.place_entry_protection.return_value = {"id": "bstop-new"}
    pipeline.db.insert_pending_protection_restore.return_value = 7
    pipeline.db.insert_trade.return_value = 1
    ctx = _ctx([_short_cop()], positions=held)
    ctx.total_value = 10_000.0
    ctx.cash = 10_000.0
    with patch("src.pipeline_stages._size_shares", return_value=3.0):
        ExecutionStage(pipeline=pipeline).run(ctx)
    assert [s["reason"] for s in ctx.execution_skips] == []
    pipeline.broker.submit_order.assert_called()
