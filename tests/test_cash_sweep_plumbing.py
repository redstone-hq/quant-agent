"""Cash-sweep plumbing: fund what will be SPENT, and never place a token order.

The owner's decision is that the sweep STAYS — a sell-before-buy is instant
and near-frictionless, so parking idle cash in SGOV is worth the round trip
*when the round trip buys something*. These tests pin the three ways the
plumbing made it buy nothing.

1. OVER-FUNDING. The sweep preflight sized the funding sale off
   `allocation_pct`; the submit loop then spent `min(alloc, risk_budget)`.
   Whenever the §11.1 vol-adjusted budget bound — the ordinary case — the
   difference was liquidated out of the yield vehicle and re-parked by the
   session bookend minutes later. Production ledger:

       2026-08-27 13:35:43  SWEEP_SELL  $3,422.61
       2026-08-27 13:36:36  SWEEP_BUY   $1,007.60     (53 seconds later)
       2026-08-31 19:21:58  SWEEP_SELL    $503.47
       2026-08-31 19:22:03  SWEEP_BUY     $806.40     (5 seconds later)

   Two crossings of the spread for no position. A SHORT's notional was
   folded into the same total even though a short never draws on
   `available_cash` — that funding is waste by construction.

2. TOKEN ORDERS. The execution-time cash clamp is the LAST resize. Fixed
   2026-09-24: it used to refuse a resize under the flat $500
   `min_order_usd` floor — an arbitrary number, not a broker minimum, and
   Alpaca charges no stock commission — so a real, small residual trade was
   refused as "too small to bother". With fractional sizing on, a $3.11
   residue now buys 0.0311 shares and the order goes out, same as a whole-
   share residue under the old floor now places its whole shares instead of
   being refused.

3. UNCONFIRMED PROCEEDS. `fund_buys` returns 0.0 when it cannot confirm the
   cash landed, but it has already refreshed `ctx` from the broker. The
   caller adopted that refresh only on the success path, so an unconfirmed
   attempt left the BUY loop clamping against a PRE-SALE cash reading.
"""

import pytest
from unittest.mock import MagicMock

from src.execution.cash_sweep import CashSweeper
from src.models import PortfolioDecision, ReasoningChain, TradeDecision
from src.pipeline_context import RunContext
from src.pipeline_stages import ExecutionStage


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


def _pipeline(live_price=100.0, cash=50_000.0, *, fractional=False, min_risk_pct=0.5, equity=100_000.0):
    """ExecutionStage harness. Config stays a MagicMock (the stage reads many
    attributes); only the leaves these tests depend on are pinned to real
    values, because a MagicMock leaf silently reads as "not a number"."""
    pipeline = MagicMock()
    pipeline.broker.get_latest_price.return_value = live_price
    pipeline.broker.get_latest_quote.return_value = {"bid_price": live_price, "ask_price": live_price}
    pipeline._format_qty = lambda q: str(q)
    pipeline._order_accepted.return_value = True
    pipeline._refresh_account_state.return_value = (
        {"cash": cash, "portfolio_value": equity},
        [],
        {},
    )
    pipeline.config.execution.fractional_enabled = fractional
    # The owner's minimum risk per position (owner rule 2026-08-27) is ON,
    # at its ratified 0.5%: the clamp tests below size their fixtures (a
    # $10,000 book, a $50 stop) so every clamped order still clears it.
    pipeline.config.risk.min_position_risk_pct = min_risk_pct
    pipeline.config.execution.fractional_share_decimals = 4
    pipeline.broker.get_fractionability.return_value = {"fractionable": True} if fractional else {"fractionable": False}
    return pipeline


def _ctx(decisions, cash=50_000.0, equity=100_000.0) -> RunContext:
    ctx = RunContext.start("morning")
    ctx.cash = cash
    ctx.total_value = equity
    ctx.last_equity = equity
    ctx.positions = []
    ctx.decision_id = "run-x-dec-abc123"
    ctx.portfolio_decision = PortfolioDecision(
        reasoning_chain=_rc(),
        decisions=decisions,
        portfolio_view="t",
    )
    ctx.symbols_bars = {}
    return ctx


def _install_sweeper(pipeline, *, freed=0.0, on_call=None):
    """Install a REAL CashSweeper (the call site isinstance-checks it) whose
    `fund_buys` is replaced by a spy. Returns the recording dict."""
    sweeper = CashSweeper(pipeline=pipeline)
    recorded: dict = {"calls": []}

    def _spy(ctx, planned_notional):
        recorded["calls"].append(planned_notional)
        recorded["planned"] = planned_notional
        if on_call is not None:
            on_call(ctx)
        return freed

    sweeper.fund_buys = _spy
    pipeline._sweeper = lambda: sweeper
    return recorded


def _events(pipeline) -> list[tuple]:
    """(stage, outcome, reason) for every persisted pipeline_event."""
    out = []
    for call in pipeline.db.insert_specialist_evidence.call_args_list:
        payload = call.kwargs.get("evidence_json") or ""
        out.append((call.kwargs.get("kind"), payload))
    return out


# ---------------------------------------------------------------------------
# Defect 1 — funding is sized to what will be SPENT, not what was planned.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Defect 2 — fixed 2026-09-24: the cash clamp used to refuse a resize under
# the flat $500 `min_order_usd` floor. That floor was an arbitrary round
# number (config/number_ledger.yaml), not a broker minimum, and Alpaca
# charges no stock commission — so a real, small residual trade was being
# refused on a false "too small to bother" basis. The clamp now places
# whatever the raw cash actually funds, however small, and only refuses when
# that is a genuine zero.
# ---------------------------------------------------------------------------


def test_fractional_clamp_to_a_token_order_is_refused_below_min_risk():
    """$3.11 of raw cash buys 0.0311 shares under fractional sizing, which
    risks ~0.0002% of a $100k book at its stop. Not refused for being under
    the deleted $500 notional floor -- refused because it is under the
    owner's 0.5% minimum risk per position (owner rule 2026-08-27)."""
    pipeline = _pipeline(live_price=100.0, cash=3.11, fractional=True, min_risk_pct=0.5)
    pipeline.broker.submit_order.return_value = {"id": "o1", "status": "accepted"}
    ctx = _ctx(
        [
            TradeDecision(
                action="BUY",
                symbol="XLE",
                allocation_pct=10,
                entry_price=100.0,
                stop_loss=95.0,
                take_profit=115.0,
                reasoning="approved, then starved of cash",
            )
        ],
        cash=3.11,
    )

    orders = ExecutionStage(pipeline=pipeline).run(ctx)

    assert orders == []
    pipeline.broker.submit_order.assert_not_called()
    assert [s["reason"] for s in ctx.execution_skips] == ["below_owner_min_risk"]


def test_clamp_still_places_a_meaningful_partial_order():
    """A partial funding fill preserving a smaller real position is the
    behaviour the resize exists for — unaffected by the floor's removal."""
    # A $10,000 book and a $50 stop: the clamped order still risks at
    # least the owner's 0.5% minimum per position (rule 2026-08-27), and a
    # 5% risk budget ($500 / $50 = 10 shares) never binds before the cash.
    pipeline = _pipeline(live_price=100.0, cash=750.0, fractional=True, equity=10_000.0)
    pipeline.config.risk.max_position_risk_pct = 5.0
    pipeline.broker.submit_order.return_value = {"id": "o1", "status": "accepted"}
    ctx = _ctx(
        [
            TradeDecision(
                action="BUY",
                symbol="XLE",
                allocation_pct=10,
                entry_price=100.0,
                stop_loss=50.0,
                take_profit=115.0,
                reasoning="approved, partially funded",
            )
        ],
        cash=750.0,
        equity=10_000.0,
    )

    orders = ExecutionStage(pipeline=pipeline).run(ctx)

    assert len(orders) == 1
    assert pipeline.broker.submit_order.call_args.kwargs["qty"] == 7.5
    assert ctx.execution_skips == []


def test_whole_share_clamp_also_places_the_small_order():
    """The removal is not fractional-only. Four whole shares at $100 is
    $400 — well under the old $500 floor — and is now placed rather than
    refused."""
    # A $10,000 book and a $50 stop: the clamped order still risks at
    # least the owner's 0.5% minimum per position (rule 2026-08-27), and a
    # 5% risk budget ($500 / $50 = 10 shares) never binds before the cash.
    pipeline = _pipeline(live_price=100.0, cash=499.0, fractional=False, equity=10_000.0)
    pipeline.config.risk.max_position_risk_pct = 5.0
    pipeline.broker.submit_order.return_value = {"id": "o1", "status": "accepted"}
    ctx = _ctx(
        [
            TradeDecision(
                action="BUY",
                symbol="XLE",
                allocation_pct=10,
                entry_price=100.0,
                stop_loss=50.0,
                take_profit=115.0,
                reasoning="approved, starved",
            )
        ],
        cash=499.0,
        equity=10_000.0,
    )

    orders = ExecutionStage(pipeline=pipeline).run(ctx)

    assert len(orders) == 1
    assert pipeline.broker.submit_order.call_args.kwargs["qty"] == 4
    assert ctx.execution_skips == []


def test_min_risk_floor_falls_back_to_the_ratified_value_not_zero():
    """An unreadable `risk.min_position_risk_pct` must not silently become
    "no floor" -- that is the defect, not the fallback. Replaced the deleted
    $500 `min_order_usd` fallback test (owner rule 2026-08-27)."""
    from src.pipeline_stages import _min_position_risk_pct
    from src.risk.constants import STARTER_POSITION_RISK_PCT

    broken = MagicMock()  # config.risk.min_position_risk_pct is a Mock
    assert _min_position_risk_pct(broken) == STARTER_POSITION_RISK_PCT
    assert _min_position_risk_pct(None) == STARTER_POSITION_RISK_PCT

    configured = MagicMock()
    configured.config.risk.min_position_risk_pct = 0.75
    assert _min_position_risk_pct(configured) == 0.75


# ---------------------------------------------------------------------------
# Defect 3 — unconfirmed proceeds.
# ---------------------------------------------------------------------------


def test_unconfirmed_funding_is_governed_by_raw_cash_not_refused():
    """`fund_buys` returning 0.0 means the proceeds could not be confirmed.
    The BUY loop must then be governed by raw cash — $174.96 of raw cash
    against a $10,000 approved order sizes down to what that cash actually
    buys (1.7496 shares). Fixed 2026-09-24: this used to be refused outright
    as under the flat $500 floor; it is now placed at the raw-cash size
    instead, since the resize itself (not the floor) is what keeps this
    from becoming a $10,000 unfunded position."""
    # A $10,000 book and a $50 stop: the clamped order still risks at
    # least the owner's 0.5% minimum per position (rule 2026-08-27), and a
    # 5% risk budget ($500 / $50 = 10 shares) never binds before the cash.
    pipeline = _pipeline(live_price=100.0, cash=174.96, fractional=True, equity=10_000.0)
    pipeline.config.risk.max_position_risk_pct = 5.0
    pipeline.broker.submit_order.return_value = {"id": "o1", "status": "accepted"}
    _install_sweeper(pipeline, freed=0.0)
    ctx = _ctx(
        [
            TradeDecision(
                action="BUY",
                symbol="XLE",
                allocation_pct=10,
                entry_price=100.0,
                stop_loss=50.0,
                take_profit=115.0,
                reasoning="approved, funding unconfirmed",
            )
        ],
        cash=174.96,
        equity=10_000.0,
    )

    orders = ExecutionStage(pipeline=pipeline).run(ctx)

    assert len(orders) == 1
    assert pipeline.broker.submit_order.call_args.kwargs["qty"] == pytest.approx(1.7496)
    assert ctx.execution_skips == []
