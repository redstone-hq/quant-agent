"""Board item 39 — a rotation SELL may not fire ahead of a replacement BUY
that is going to be refused.

The defect these pin: the `ranked_margin` rotation tier sells a held
position purely to fund a specific replacement. Every gate that can refuse
that replacement runs LATER in `ExecutionStage._run_session` than the SELL
loop does, so the desk could end the session sold out of a position with
nothing bought in its place and only an owner alert behind it.

Two earlier fixes failed review:

  * attempt 1 closed one refusal path and turned the structural barrier in
    front of the tier into a config boolean, which is a weaker guard than
    the one it replaced;
  * attempt 2 re-ran the daily-loss gate using PRE-SALE account state. The
    real gate recomputes from `ctx.positions` AFTER the sale, on BOTH sides
    of its comparison — the numerator is the held book's own intraday
    change and the volatility-relative rung of the threshold is measured
    from the held book too — so a pre-sale check is structurally blind to
    the state change the real check depends on.

What is pinned here:

  * a winner sold out of a losing book: pre-sale the daily-loss gate
    passes, post-sale it trips, and the fix withdraws BOTH legs rather
    than selling into a buy that will be refused (the failure attempt 2
    cannot see);
  * the projection moves the THRESHOLD as well as the loss;
  * each of the four downstream refusal paths — daily-loss, no_price,
    stale_entry, qty_zero — withdraws both legs;
  * exits earlier in the SAME session are applied to the projection too;
  * the barrier in front of the sale is a `RotationClearance` object and
    not a flag: `rotation_sell_reason` raises without one, raises for one
    minted for a different pair, and raises for one missing a gate, with no
    config read anywhere in the refusal;
  * `REQUIRED_BUY_LEG_GATES` and the gate function cannot silently stop
    agreeing;
  * the flag stays OFF by default and a MagicMock config never reads as on.
"""

from __future__ import annotations

import ast
import json
import pathlib
from types import SimpleNamespace
from unittest.mock import DEFAULT, MagicMock

import pytest

from src import pipeline_stages as ps
from src import stage_execution as se
from src.config import ExecutionConfig, RiskConfig
from src.models import Position
from src.pipeline_context import RunContext
from tests.session_clock import todays_session_stamp
from src.risk.rules import RiskRuleEngine
from src.rotation import (
    REQUIRED_BUY_LEG_GATES,
    RotationClearance,
    RotationOpportunity,
    rotation_proposal_reason,
    rotation_sell_reason,
)
from src.storage.db import Database

BROKEN_DETAIL = "structural level 100.0 has closed beyond it twice"

#: The repo root, so a test that reads a shipped file works whatever
#: directory pytest was started from.
REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent


def _risk_kwargs(**overrides) -> dict:
    """A complete `RiskConfig` — every required field, no daily limit unless
    a test asks for one."""
    base = dict(
        max_position_pct=20.0,
        max_total_position_pct=90.0,
        require_stop_loss=True,
        allow_margin=False,
    )
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Fixtures — a pipeline stub with the REAL risk engine behind it.
#
# The account-level loss halt these fixtures used to drive was removed
# outright on 2026-09-23 (PR #584, owner ruling on board item 32), so the
# gate this file exercises is now the FUNDING pair — `insufficient_cash`
# and `below_min_notional`, measured by `_entry_deployment_budget` over the
# projected post-sale book. `budget_by_book` below is what makes a fixture
# able to tell a pre-sale book from a post-sale one, which is the property
# every attempt on this item has actually turned on.
# ---------------------------------------------------------------------------


def _pos(symbol, qty=10.0, price=100.0, intraday=0.0) -> Position:
    return Position(
        symbol=symbol,
        qty=qty,
        avg_entry=100.0,
        current_price=price,
        market_value=qty * price,
        unrealized_pnl=0.0,
        unrealized_intraday_pnl=intraday,
        sector="Technology",
    )


def _buy(symbol="NEW", allocation_pct=2.0, entry_price=50.0, stop_loss=45.0):
    return SimpleNamespace(
        symbol=symbol,
        action="BUY",
        allocation_pct=allocation_pct,
        entry_price=entry_price,
        stop_loss=stop_loss,
        reasoning="best idea",
    )


def _sell(symbol="OLD", allocation_pct=100.0):
    return SimpleNamespace(
        symbol=symbol,
        action="SELL",
        allocation_pct=allocation_pct,
        entry_price=0.0,
        stop_loss=0.0,
        reasoning="rotation close",
    )


def _opportunity() -> RotationOpportunity:
    return RotationOpportunity(
        new_symbol="NEW",
        new_score=4.0,
        held_symbol="OLD",
        held_score=2.0,
        tier="ranked_margin",
        shared_seats=("technical",),
        held_shared_score=2.0,
        new_shared_score=4.0,
    )


def _pipeline(tmp_path, *, positions=None, total_value=100_000.0, cash=50_000.0, prices=None):
    """A pipeline stub carrying a REAL `RiskRuleEngine`."""
    db = Database(str(tmp_path / "t.db"))
    db.initialize()
    risk_config = RiskConfig(**_risk_kwargs())
    pipeline = SimpleNamespace()
    pipeline.db = db
    pipeline.config = SimpleNamespace(
        execution=ExecutionConfig(
            rotation_enabled=True,
            rotation_ranked_margin_enabled=True,
        ),
        cash_sweep=SimpleNamespace(symbol="SGOV"),
        risk=risk_config,
    )
    pipeline.risk_engine = RiskRuleEngine(risk_config)
    pipeline._prices = prices if prices is not None else {"NEW": 50.0}

    # `_today_sizing_price` (item 120: the SIZING divisor for a rotation's
    # replacement BUY) reads `pipeline.broker.get_intraday_snapshots`
    # directly — a separate check from `_live_fill_price`, which the tests
    # below stub via `_stub_sizing`. Without a broker here it falls through
    # to `getattr(pipeline, "broker", None)` = None and every buy leg is
    # refused `no_price` regardless of what `_stub_sizing` set up. Give it a
    # real today print (via `tests.session_clock.todays_session_stamp`, so
    # it resolves at any hour) for whatever symbol is asked about, priced
    # from `pipeline._prices` when known and $50 otherwise — the tests below
    # only need this gate to pass, not a specific number, since the price
    # that actually drives sizing/deviation assertions is `_live_fill_price`.
    prices_by_symbol = dict(pipeline._prices)

    def _snapshots(symbols):
        return {
            sym: {
                "last_price": prices_by_symbol.get(sym, 50.0),
                "last_trade_at": todays_session_stamp(),
            }
            for sym in symbols
        }

    pipeline.broker = MagicMock()
    pipeline.broker.get_intraday_snapshots.side_effect = _snapshots

    # A held name's live quote straddles its mark by $0.50 a side: a market
    # SELL fills at the bid, a market COVER at the ask, and the projection
    # credits exactly that. Any other symbol keeps the MagicMock default.
    held_marks = {p.symbol: float(p.current_price) for p in positions or []}

    def _quote(symbol):
        mark = held_marks.get(symbol)
        if mark is None:
            return DEFAULT
        return {"bid_price": mark - 0.50, "ask_price": mark + 0.50}

    pipeline.broker.get_latest_quote.side_effect = _quote

    #: The gate refreshes the account before projecting, because the state
    #: this stage is handed is the research snapshot from 5-10 minutes
    #: earlier. The stub hands back exactly what the test set up, so a
    #: fixture stays in control of the numbers.
    pipeline.refreshed = []

    def _refresh():
        pipeline.refreshed.append(True)
        return (
            {"cash": pipeline.stub_cash, "portfolio_value": pipeline.stub_total_value},
            list(pipeline.stub_positions),
            {},
        )

    pipeline.stub_cash = cash
    pipeline.stub_total_value = total_value
    pipeline.stub_positions = list(positions or [])
    pipeline._refresh_account_state = _refresh
    return pipeline, db


def _ctx(positions, total_value=100_000.0, last_equity=100_000.0, rotation=True) -> RunContext:
    ctx = RunContext(run_id="run-1", session="morning")
    ctx.positions = list(positions)
    ctx.total_value = total_value
    ctx.last_equity = last_equity
    if rotation:
        ctx.rotation = {
            "held_symbol": "OLD",
            "new_symbol": "NEW",
            "tier": "ranked_margin",
            "clearance": None,
            "opportunity": _opportunity(),
            "protection_basis_text": "structural_level_broken",
            "protection_detail_text": BROKEN_DETAIL,
            "headroom_pct": 0.2,
            "ceiling_pct": 25.0,
            "floor_pct": 0.5,
            "reason": "proposal text",
        }
    return ctx


def _stub_sizing(
    monkeypatch,
    *,
    qty=40.0,
    risk_qty=40.0,
    price=50.0,
    budget=1_000_000.0,
    budget_by_book=None,
    single_name_cap=1_000_000.0,
    min_risk_pct=0.0,
):
    """Hold the price, size and funding helpers still so a test can aim at
    ONE gate at a time.

    `budget_by_book` maps a frozenset of the symbols still held to what is
    deployable over THAT book, so a test can make the funding answer depend
    on which names survive the sale — which is exactly what a pre-sale
    check cannot see, and the property every attempt on board item 39 has
    turned on.
    """

    def _budget(pipeline, ctx, positions, equity, cash):
        if budget_by_book is None:
            return budget, True, "stubbed budget"
        key = frozenset(str(getattr(p, "symbol", "")).upper() for p in (positions or []))
        if key not in budget_by_book:
            raise AssertionError(f"the budget was measured over {sorted(key)}, which the fixture did not expect")
        return budget_by_book[key], True, "stubbed budget"

    monkeypatch.setattr(ps, "_entry_deployment_budget", _budget)
    monkeypatch.setattr(
        ps,
        "_single_name_execution_cap",
        lambda pipeline, equity: single_name_cap,
    )
    monkeypatch.setattr(ps, "_min_position_risk_pct", lambda pipeline: min_risk_pct)
    monkeypatch.setattr(se, "_min_position_risk_pct", lambda pipeline: min_risk_pct)
    monkeypatch.setattr(
        ps,
        "_live_fill_price",
        lambda pipeline, symbol: price,
    )
    monkeypatch.setattr(
        ps,
        "_fractional_sizing_allowed",
        lambda pipeline, symbol, *, is_short: False,
    )
    monkeypatch.setattr(
        ps,
        "_size_shares",
        # Whole-share quantization, capped at what the test asked for, so a
        # budget the fixture shrinks actually shrinks the order.
        lambda pipeline, raw, *, fractional: min(qty, float(int(raw))),
    )
    monkeypatch.setattr(
        ps,
        "_qty_by_risk_budget",
        lambda pipeline, **kwargs: risk_qty,
    )


def _gate(pipeline, ctx, positions, *, sells=None, buys=None, cash=50_000.0, total_value=100_000.0):
    """Run the rotation's own close through `_rotation_sell_gate`, the way
    the SELL loop does — the rotation's close is ordered LAST, so by the
    time it is reached the other exits have already run and the account is
    re-read rather than projected."""
    ordered = ps._rotation_sell_last(
        list(sells) if sells is not None else [_sell()],
        ctx,
    )
    decision = ordered[-1]
    result = ps._rotation_sell_gate(
        pipeline,
        ctx,
        decision,
        list(buys) if buys is not None else [_buy()],
        positions,
        total_value,
        cash,
    )
    return result, ordered


def _events(db, run_id="run-1") -> list[dict]:
    rows = db.conn.execute(
        "SELECT evidence_json FROM specialist_evidence WHERE run_id = ? ORDER BY id",
        (run_id,),
    ).fetchall()
    return [json.loads(r["evidence_json"]) for r in rows]


def _rotation_events(db) -> list[dict]:
    return [e for e in _events(db) if e.get("stage") == "rotation"]


# ---------------------------------------------------------------------------
# The failure this closes — the one attempt 2's pre-sale check cannot see
# ---------------------------------------------------------------------------


def test_the_funding_gate_reads_the_post_sale_book_not_the_pre_sale_one(
    tmp_path,
    monkeypatch,
):
    """The defect every attempt on this item is about, on the mechanism that
    survived.

    Until 2026-09-23 this was demonstrated on the daily-loss re-check: OLD
    up on the day, the rest of the book down, and the sale removing OLD's
    gain from the very number the real post-sale check recomputes. The
    owner removed that whole alarm (PR #584), so the demonstration moves to
    the gate that is still there and still post-sale: the deployment
    budget, which `_entry_deployment_budget` measures over the remaining
    positions and their gross exposure.

    Here the PRE-sale book has room for the replacement and the POST-sale
    book does not. A check fed pre-sale state clears the sale and the desk
    is left holding neither name; this one projects the post-sale book,
    sees no room, and withdraws the close before it reaches the wire.
    """
    positions = [
        _pos("OLD", qty=10.0, price=100.0),
        _pos("KEEP", qty=10.0, price=100.0),
    ]
    _stub_sizing(
        monkeypatch,
        budget_by_book={
            # What a pre-sale check would have measured: plenty of room.
            frozenset({"OLD", "KEEP"}): 1_000_000.0,
            # What the sale actually leaves: none.
            frozenset({"KEEP"}): 0.0,
        },
    )
    pipeline, db = _pipeline(tmp_path, positions=positions)
    ctx = _ctx(positions)

    (cleared, *_rest), ordered = _gate(
        pipeline,
        ctx,
        positions,
        sells=[_sell()],
        buys=[_buy()],
    )

    assert cleared is False, "the rotation SELL must not reach the wire"
    assert ctx.rotation["withdrawn"].startswith("buy_leg_would_be_refused")
    assert ctx.rotation.get("sell_order_id") is None
    (withdrawn,) = [e for e in _rotation_events(db) if e["outcome"] == "withdrawn"]
    assert withdrawn["reason"] == "buy_leg_would_be_refused:insufficient_cash"
    skips = {s["reason"] for s in ctx.execution_skips}
    assert skips == {"insufficient_cash", "rotation_withdrawn"}


def test_the_projection_is_what_the_budget_and_the_sizing_are_measured_on(
    tmp_path,
    monkeypatch,
):
    """The post-sale book, the post-sale cash and the equity behind them all
    reach `_entry_deployment_budget` together.

    A projected position list measured against live equity, or the reverse,
    describes a book that never exists — which is the mismatch attempt 2
    shipped one layer up. This pins all three arguments at once.
    """
    seen = {}

    def _budget(pipeline, ctx, positions, equity, cash):
        seen["symbols"] = [p.symbol for p in positions]
        seen["equity"] = equity
        seen["cash"] = cash
        return 1_000_000.0, True, "stubbed budget"

    _stub_sizing(monkeypatch)
    monkeypatch.setattr(ps, "_entry_deployment_budget", _budget)
    positions = [
        _pos("OLD", qty=10.0, price=100.0),
        _pos("KEEP", qty=10.0, price=100.0),
    ]
    pipeline, _db = _pipeline(tmp_path, positions=positions)
    ctx = _ctx(positions)

    _gate(pipeline, ctx, positions, sells=[_sell()], buys=[_buy()], cash=50_000.0, total_value=100_000.0)

    assert seen["symbols"] == ["KEEP"], "OLD must be gone from the projection"
    # A sale is mark-to-market neutral apart from what the marketable limit
    # gives up: 10 shares marked at $100, sold at $99.50.
    assert seen["equity"] == pytest.approx(100_000.0 - 10 * 0.50)
    # 10 shares marked at $100, sold at the SELL loop's own 0.995 limit.
    assert seen["cash"] == pytest.approx(50_000.0 + 10 * 99.50)


# ---------------------------------------------------------------------------
# Each of the five downstream refusal paths withdraws BOTH legs
# ---------------------------------------------------------------------------

#: `below_min_notional` fixed 2026-09-24: it used to withdraw a rotation
#: whose replacement buy re-sized under the flat $500 `min_order_usd` floor
#: — an arbitrary number, not a broker minimum, and Alpaca charges no stock
#: commission — so a real, small rotation was refused as "too small to
#: bother". No code path can produce that refusal any more (see the fix
#: note on `_prevent_rotation_naked_sale` in `src/pipeline_stages.py`), so
#: it is no longer exercised by the generic "every gate withdraws"
#: parametrize below; `test_below_min_notional_no_longer_withdraws_a_small_buy`
#: covers it separately. `below_min_notional` remains a name in
#: `REQUIRED_BUY_LEG_GATES` for coverage bookkeeping (a gate that never
#: fires is still a gate that was checked) — whether to retire it outright,
#: the way `daily_loss_recheck` was retired, was left as an open decision.
_LIVE_BUY_LEG_GATES = tuple(REQUIRED_BUY_LEG_GATES)


@pytest.mark.parametrize("gate", _LIVE_BUY_LEG_GATES)
def test_every_required_gate_withdraws_both_legs(tmp_path, monkeypatch, gate):
    positions = [_pos("OLD", intraday=0.0), _pos("KEEP", intraday=0.0)]
    if gate == "no_price":
        _stub_sizing(monkeypatch)
        monkeypatch.setattr(ps, "_live_fill_price", lambda p, s: None)
    elif gate == "stale_entry":
        # The PM's entry is 40% away from the live market.
        _stub_sizing(monkeypatch, price=80.0)
    elif gate == "qty_zero":
        _stub_sizing(monkeypatch, qty=0.0)
    elif gate == "insufficient_cash":
        # 40 shares at $50 is $2,000 of notional; nothing is deployable.
        _stub_sizing(monkeypatch, budget=0.0)
    elif gate == "below_owner_min_risk":
        # 40 shares at $50 with a $45 stop risks $200, 0.20% of $100,000,
        # under the owner's 0.5% minimum risk per position (rule 2026-08-27).
        _stub_sizing(monkeypatch, min_risk_pct=0.5)
    pipeline, db = _pipeline(tmp_path, positions=positions)
    ctx = _ctx(positions)

    (cleared, *_rest), ordered = _gate(
        pipeline,
        ctx,
        positions,
        sells=[_sell()],
        buys=[_buy()],
    )

    assert cleared is False, f"{gate}: the SELL must be withdrawn"
    assert ctx.rotation.get("sell_order_id") is None
    (withdrawn,) = [e for e in _rotation_events(db) if e["outcome"] == "withdrawn"]
    assert withdrawn["reason"] == f"buy_leg_would_be_refused:{gate}"
    assert gate in {s["reason"] for s in ctx.execution_skips}


def test_below_min_notional_no_longer_withdraws_a_small_buy(tmp_path, monkeypatch):
    """Fixed 2026-09-24: enough deployable for 1 share used to be "under the
    $500 minimum worth trading" and withdrew the whole rotation. That flat
    floor no longer gates the rotation buy leg, so the same fixture that
    used to trigger `below_min_notional` now clears normally."""
    positions = [_pos("OLD", intraday=0.0), _pos("KEEP", intraday=0.0)]
    _stub_sizing(monkeypatch, budget=60.0)
    pipeline, db = _pipeline(tmp_path, positions=positions)
    ctx = _ctx(positions)

    (cleared, *_rest), ordered = _gate(
        pipeline,
        ctx,
        positions,
        sells=[_sell()],
        buys=[_buy()],
    )

    assert cleared is True, "a small, nonzero re-sized buy must clear, not withdraw"
    assert not [e for e in _rotation_events(db) if e["outcome"] == "withdrawn"]


def test_qty_zero_from_the_risk_budget_alone_also_withdraws(
    tmp_path,
    monkeypatch,
):
    """The allocation rounds to a real size but the risk budget cannot
    carry one orderable unit — the second `qty_zero` exit in the preflight,
    and a separate code path from the first."""
    _stub_sizing(monkeypatch, qty=40.0, risk_qty=0.0)
    positions = [_pos("OLD"), _pos("KEEP")]
    pipeline, db = _pipeline(tmp_path, positions=positions)
    ctx = _ctx(positions)
    (cleared, *_rest), ordered = _gate(
        pipeline,
        ctx,
        positions,
        sells=[_sell()],
        buys=[_buy()],
    )
    assert cleared is False
    (withdrawn,) = [e for e in _rotation_events(db) if e["outcome"] == "withdrawn"]
    assert withdrawn["reason"] == "buy_leg_would_be_refused:qty_zero"
    assert "risk budget" in withdrawn["detail"]


# ---------------------------------------------------------------------------
# Earlier exits in the SAME session are part of the projection
# ---------------------------------------------------------------------------


def test_the_rotations_close_is_ordered_last_among_this_sessions_exits(tmp_path):
    """Everything else this session sells goes first, so that when the
    rotation's own close is gated, the other exits are a measurement
    instead of an assumption. Their relative order is preserved."""
    positions = [_pos("OLD"), _pos("EARLY"), _pos("LATER"), _pos("KEEP")]
    _pipeline(tmp_path, positions=positions)
    ctx = _ctx(positions)
    ordered = ps._rotation_sell_last(
        [_sell("EARLY"), _sell("OLD"), _sell("LATER")],
        ctx,
    )
    assert [d.symbol for d in ordered] == ["EARLY", "LATER", "OLD"]

    # A session with no ranked-margin rotation is untouched.
    ctx.rotation = None
    same = [_sell("EARLY"), _sell("OLD")]
    assert ps._rotation_sell_last(same, ctx) is same


def test_a_partial_exit_leaves_a_proportional_share_of_its_intraday_pnl(
    tmp_path,
):
    """Selling 40% of a name leaves 60% of its intraday change on the books
    — the field the daily-loss numerator actually reads."""
    positions = [_pos("PART", qty=10.0, intraday=1000.0)]
    pipeline, _db = _pipeline(tmp_path, positions=positions)
    ctx = _ctx(positions, rotation=False)
    projected, equity = ps._projected_post_sale_book(
        positions,
        100_000.0,
        [_sell("PART", 40.0)],
        [],
        quotes={"PART": {"bid": 99.50, "ask": 100.50}},
    )
    (remaining,) = projected
    # Whole-share position: 40% of 10 floors to 4 shares sold, 6 left.
    assert remaining.qty == pytest.approx(6.0)
    assert remaining.market_value == pytest.approx(600.0)
    # The concession on the 4 shares sold: marked at $100, sold by market
    # order at the live $99.50 bid. Subtracting it TIGHTENS the funding
    # gate under the shipped `allow_margin: true` — see the docstring.
    assert equity == pytest.approx(100_000.0 - 4 * 0.50)


def test_a_full_exit_removes_the_position_entirely(tmp_path):
    positions = [_pos("GONE", qty=10.0, intraday=1000.0), _pos("KEEP")]
    pipeline, _db = _pipeline(tmp_path, positions=positions)
    ctx = _ctx(positions, rotation=False)
    projected, _equity = ps._projected_post_sale_book(
        positions,
        100_000.0,
        [_sell("GONE")],
        [],
        quotes={"GONE": {"bid": 99.50, "ask": 100.50}},
    )
    assert [p.symbol for p in projected] == ["KEEP"]


# ---------------------------------------------------------------------------
# The clean path: a clearance is minted and the sale becomes buildable
# ---------------------------------------------------------------------------


def test_a_clean_projection_mints_a_clearance_and_keeps_both_legs(
    tmp_path,
    monkeypatch,
):
    _stub_sizing(monkeypatch)
    positions = [_pos("OLD", intraday=100.0), _pos("KEEP", intraday=100.0)]
    pipeline, db = _pipeline(tmp_path, positions=positions)
    ctx = _ctx(positions)
    sell, buy = _sell(), _buy()

    (cleared, *_rest), ordered = _gate(
        pipeline,
        ctx,
        positions,
        sells=[sell],
        buys=[buy],
    )
    assert cleared is True

    clearance = ctx.rotation["clearance"]
    assert isinstance(clearance, RotationClearance)
    assert clearance.covers(held_symbol="OLD", new_symbol="NEW")
    assert set(clearance.gates_checked) >= set(REQUIRED_BUY_LEG_GATES)
    assert clearance.projected_positions == ("KEEP",)

    (cleared,) = [e for e in _rotation_events(db) if e["outcome"] == "buy_leg_cleared"]
    assert cleared["projected_positions"] == ["KEEP"]

    # And the sale's reason now builds, carrying the projected numbers.
    reason = ps._rotation_ranked_margin_sell_reason(pipeline, ctx, sell)
    assert isinstance(reason, str)
    assert "BUY pre-cleared post-sale" in reason
    assert len(reason) <= 500, "the constructor truncates at 500 characters"


def test_a_missing_buy_leg_withdraws_the_sell(tmp_path):
    positions = [_pos("OLD"), _pos("KEEP")]
    pipeline, db = _pipeline(tmp_path, positions=positions)
    ctx = _ctx(positions)
    (cleared, *_rest), ordered = _gate(
        pipeline,
        ctx,
        positions,
        sells=[_sell()],
        buys=[_buy("OTHER")],
    )
    assert cleared is False
    (withdrawn,) = [e for e in _rotation_events(db) if e["outcome"] == "withdrawn"]
    assert withdrawn["reason"] == "buy_leg_absent"


def test_a_sell_leg_the_risk_manager_already_refused_is_left_alone(tmp_path):
    """Nothing to withdraw and nothing that can go naked — the existing
    `_drop_rotation_buy_if_room_not_freed` already stops the buy."""
    positions = [_pos("OLD"), _pos("KEEP")]
    pipeline, db = _pipeline(tmp_path, positions=positions)
    ctx = _ctx(positions)
    result = ps._rotation_sell_gate(
        pipeline,
        ctx,
        _sell("SOMETHINGELSE"),
        [_buy()],
        positions,
        100_000.0,
        50_000.0,
    )
    assert result is None, "not the rotation's own close"
    assert _rotation_events(db) == []


def test_the_categorical_tier_is_untouched_by_all_of_this(tmp_path):
    """`ineligible_hold` is already live and sells a holding the desk's own
    rules say it would not buy today. That sale stands on its own and is
    not withdrawn when the replacement falls through."""
    positions = [_pos("OLD"), _pos("KEEP")]
    pipeline, db = _pipeline(tmp_path, positions=positions)
    ctx = _ctx(positions)
    ctx.rotation["tier"] = "ineligible_hold"
    result, _ordered = _gate(pipeline, ctx, positions)
    assert result is None, "the categorical tier is not gated here"
    assert _rotation_events(db) == []


def test_a_session_with_no_rotation_is_a_no_op(tmp_path):
    positions = [_pos("OLD")]
    pipeline, db = _pipeline(tmp_path, positions=positions)
    ctx = _ctx(positions, rotation=False)
    sell = _sell()
    assert ps._rotation_sell_last([sell], ctx) == [sell]
    assert (
        ps._rotation_sell_gate(
            pipeline,
            ctx,
            sell,
            [_buy()],
            positions,
            100_000.0,
            50_000.0,
        )
        is None
    )
    assert _events(db) == []


# ---------------------------------------------------------------------------
# The barrier is an object carrying evidence, not a flag
# ---------------------------------------------------------------------------


def _reason_kwargs():
    return dict(
        protection_basis="structural_level_broken",
        protection_detail=BROKEN_DETAIL,
        headroom_pct=0.2,
        ceiling_pct=25.0,
        floor_pct=0.5,
    )


def test_ranked_margin_sale_cannot_be_built_without_a_clearance():
    with pytest.raises(ValueError, match="RotationClearance"):
        rotation_sell_reason(_opportunity(), **_reason_kwargs())


@pytest.mark.parametrize("substitute", [True, 1, "yes", {"cleared": True}, object()])
def test_no_truthy_value_substitutes_for_a_clearance(substitute):
    """Board item 39, attempt 1: the guard in front of this tier was a
    config boolean, so one truthy value put a real sale on the wire. A
    truthy value is not evidence and is refused here."""
    with pytest.raises(ValueError, match="RotationClearance"):
        rotation_sell_reason(_opportunity(), clearance=substitute, **_reason_kwargs())


def _clearance(**overrides) -> RotationClearance:
    base = dict(
        held_symbol="OLD",
        new_symbol="NEW",
        gates_checked=tuple(REQUIRED_BUY_LEG_GATES),
        projected_entry_budget=12_500.0,
        projected_budget_basis="gross ladder headroom",
        projected_positions=("KEEP",),
        projected_equity=100_000.0,
    )
    base.update(overrides)
    return RotationClearance(**base)


@pytest.mark.parametrize(
    "bad",
    [
        {"held_symbol": "SOMETHINGELSE"},
        {"new_symbol": "SOMETHINGELSE"},
        {"gates_checked": ("no_price", "stale_entry", "qty_zero")},
        {"gates_checked": ()},
    ],
)
def test_a_clearance_for_a_different_sale_or_a_partial_check_is_refused(bad):
    with pytest.raises(ValueError, match="not for|RotationClearance"):
        rotation_sell_reason(_opportunity(), clearance=_clearance(**bad), **_reason_kwargs())


def test_a_real_clearance_builds_a_reason_naming_what_it_was_cleared_on():
    reason = rotation_sell_reason(_opportunity(), clearance=_clearance(), **_reason_kwargs())
    assert "ranked margin" in reason
    assert "OLD" in reason and "NEW" in reason
    assert "technical" in reason, "the like-for-like seats must be named"
    assert "gross ladder headroom" in reason and "$12500" in reason


def test_the_proposal_reason_says_the_close_is_contingent():
    """What the Risk Manager reviews. It is not an authorisation, and it
    says so in the text the reviewer reads."""
    proposal = rotation_proposal_reason(_opportunity(), **_reason_kwargs())
    assert "CONTINGENT" in proposal


def test_the_wire_barrier_refuses_and_records_when_no_clearance_exists(tmp_path):
    positions = [_pos("OLD")]
    pipeline, db = _pipeline(tmp_path, positions=positions)
    ctx = _ctx(positions)  # clearance is None — the gate never cleared it
    assert (
        ps._rotation_ranked_margin_sell_reason(
            pipeline,
            ctx,
            _sell(),
        )
        is ps._ROTATION_SELL_REFUSED
    )
    (refused,) = [e for e in _rotation_events(db) if e["outcome"] == "sell_refused"]
    assert refused["reason"] == "no_clearance"
    assert "rotation_sell_refused" in {s["reason"] for s in ctx.execution_skips}


def test_the_wire_barrier_ignores_every_non_rotation_sell(tmp_path):
    positions = [_pos("OLD")]
    pipeline, _db = _pipeline(tmp_path, positions=positions)
    ctx = _ctx(positions)
    assert (
        ps._rotation_ranked_margin_sell_reason(
            pipeline,
            ctx,
            _sell("SOMETHINGELSE"),
        )
        is None
    )
    ctx.rotation["tier"] = "ineligible_hold"
    assert (
        ps._rotation_ranked_margin_sell_reason(
            pipeline,
            ctx,
            _sell(),
        )
        is None
    )
    ctx.rotation = None
    assert (
        ps._rotation_ranked_margin_sell_reason(
            pipeline,
            ctx,
            _sell(),
        )
        is None
    )


# ---------------------------------------------------------------------------
# The list of gates and the code that checks them cannot drift apart
# ---------------------------------------------------------------------------


def test_the_gate_function_evaluates_every_required_gate(tmp_path, monkeypatch):
    _stub_sizing(monkeypatch)
    positions = [_pos("OLD"), _pos("KEEP")]
    pipeline, _db = _pipeline(tmp_path, positions=positions)
    ctx = _ctx(positions)
    clearance, reason, _detail = ps._rotation_buy_leg_projected_refusal(
        pipeline,
        ctx,
        rotation=ctx.rotation,
        buy_decision=_buy(),
        positions=positions,
        total_value=100_000.0,
        cash=50_000.0,
        rotation_sell=_sell(),
    )
    assert reason is None
    assert set(clearance.gates_checked) == set(REQUIRED_BUY_LEG_GATES), (
        "REQUIRED_BUY_LEG_GATES and the gate function have drifted apart"
    )


def _recorded_skip_reasons_outside_the_rotation_gate() -> set[str]:
    """Every literal reason code `_record_execution_skip` is called with in
    `src/pipeline_stages.py`, EXCLUDING the rotation gate's own calls.

    The exclusion is the whole point. The earlier version of this test
    searched the file for the literal `"no_price",` and the rotation gate's
    own `return None, "no_price", (...)` matched it, so the test would have
    passed even if the execution stage had never carried that reason code —
    it was reading this change's own text back to itself (adversary review,
    2026-09-23). Walking the AST for actual `_record_execution_skip` calls
    outside `_rotation_buy_leg_projected_refusal` is what makes the
    comparison mean anything.
    """
    # `ExecutionStage` moved to `src/stage_execution.py` verbatim (item 210,
    # step 10); the rotation gate stayed in `pipeline_stages.py`. Its per-name
    # phases (sizing, quote, cover/sell legs) live in `stage_execution_parts/`
    # since the 2026-10-09 split, so their skip calls are walked too.
    tree = ast.parse(
        (REPO_ROOT / "src" / "pipeline_stages.py").read_text()
        + (REPO_ROOT / "src" / "stage_execution.py").read_text()
        + "".join(p.read_text() for p in sorted((REPO_ROOT / "src" / "stage_execution_parts").glob("*.py")))
    )
    excluded = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_rotation_buy_leg_projected_refusal":
            excluded.update(id(n) for n in ast.walk(node))
    reasons = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or id(node) in excluded:
            continue
        func = node.func
        name = getattr(func, "id", None) or getattr(func, "attr", None)
        if name != "_record_execution_skip":
            continue
        # `_record_execution_skip(pipeline, ctx, symbol, reason, detail)`.
        if len(node.args) >= 4 and isinstance(node.args[3], ast.Constant):
            value = node.args[3].value
            if isinstance(value, str):
                reasons.add(value)
    return reasons


def test_required_gates_name_real_execution_skip_reasons():
    """Every required gate is a reason code the EXECUTION stage really
    records, read off its own call sites rather than off this file."""
    recorded = _recorded_skip_reasons_outside_the_rotation_gate()
    assert recorded, "the AST walk found no skip sites at all — it is broken"
    missing = [g for g in REQUIRED_BUY_LEG_GATES if g not in recorded]
    assert not missing, (
        f"{missing} are not reason codes the execution stage records; a gate "
        f"standing in front of a refusal that cannot fire always passes"
    )


def test_no_required_gate_names_a_retired_mechanism():
    """The counterpart to the test above, and the one that would have caught
    `daily_loss_recheck` staying in the list after the account-level loss
    halt was deleted. Dropping an entry from `REQUIRED_BUY_LEG_GATES` is
    otherwise mechanically silent: all three existing guards — the pin
    above, the gate function's `missing` check, and `RotationClearance.
    covers()` — are `all(gate in ...)` and see additions only."""
    from src.retired_mechanisms import load_registry

    retired = set()
    for entry in load_registry():
        retired.update(getattr(entry, "symbols", ()) or ())
    offenders = [g for g in REQUIRED_BUY_LEG_GATES if g in retired]
    assert not offenders, (
        f"{offenders} name mechanisms this desk has retired — the refusal they gate against cannot fire"
    )


# ---------------------------------------------------------------------------
# Default posture
# ---------------------------------------------------------------------------


def test_the_ranked_margin_flag_is_off_by_default_and_needs_both_switches():
    from unittest.mock import MagicMock

    assert ExecutionConfig().rotation_ranked_margin_enabled is False

    def _p(**execution):
        return SimpleNamespace(
            config=SimpleNamespace(
                execution=SimpleNamespace(**execution),
            )
        )

    assert (
        ps._rotation_ranked_margin_enabled(
            _p(rotation_enabled=True, rotation_ranked_margin_enabled=True),
        )
        is True
    )
    # Either switch off is off.
    assert (
        ps._rotation_ranked_margin_enabled(
            _p(rotation_enabled=False, rotation_ranked_margin_enabled=True),
        )
        is False
    )
    assert (
        ps._rotation_ranked_margin_enabled(
            _p(rotation_enabled=True, rotation_ranked_margin_enabled=False),
        )
        is False
    )
    # A MagicMock config must never read as "yes, close a real position".
    assert (
        ps._rotation_ranked_margin_enabled(
            SimpleNamespace(config=MagicMock()),
        )
        is False
    )
    assert (
        ps._rotation_ranked_margin_enabled(
            _p(rotation_enabled=True, rotation_ranked_margin_enabled=1),
        )
        is False
    )


def test_the_shipped_settings_leave_the_ranked_margin_tier_off():
    import yaml

    with open(REPO_ROOT / "config" / "settings.yaml") as handle:
        settings = yaml.safe_load(handle)
    assert settings["execution"]["rotation_ranked_margin_enabled"] is False


def test_the_reason_fits_the_field_it_is_truncated_into():
    """`PortfolioConstructor._build_sell` appends the thesis condition and
    truncates at 500 characters, and the LAST clause is the one naming what
    the sale was cleared on. A worst-case seat list and protection detail
    must still fit, and the builder refuses rather than be silently cut."""
    wide = RotationOpportunity(
        new_symbol="NEWLONGSYM",
        new_score=44.4444,
        held_symbol="OLDLONGSYM",
        held_score=22.2222,
        tier="ranked_margin",
        shared_seats=("technical", "fundamental", "macro", "flow", "news"),
        held_shared_score=22.2222,
        new_shared_score=44.4444,
    )
    long_detail = "structural level " + ("9" * 300)
    for clearance in (None, _clearance(held_symbol="OLDLONGSYM", new_symbol="NEWLONGSYM")):
        reason = (
            rotation_sell_reason(
                wide,
                clearance=clearance,
                protection_basis="structural_level_broken",
                protection_detail=long_detail,
                headroom_pct=0.2,
                ceiling_pct=25.0,
                floor_pct=0.5,
            )
            if clearance is not None
            else rotation_proposal_reason(
                wide,
                protection_basis="structural_level_broken",
                protection_detail=long_detail,
                headroom_pct=0.2,
                ceiling_pct=25.0,
                floor_pct=0.5,
            )
        )
        assert len(reason) <= 500, len(reason)
    # The contingency clause survives the worst case — it is the whole
    # point of the proposal text.
    assert "CONTINGENT" in rotation_proposal_reason(
        wide,
        protection_basis="structural_level_broken",
        protection_detail=long_detail,
        headroom_pct=0.2,
        ceiling_pct=25.0,
        floor_pct=0.5,
    )


def test_an_unrefreshable_account_withdraws_the_rotation(tmp_path, monkeypatch):
    """The state this stage is handed is the research snapshot from five to
    ten minutes earlier, and projecting from a stale higher equity on a
    falling tape clears sales reality would refuse. The gate refreshes
    first, and a refresh it cannot make is a refusal to act."""
    _stub_sizing(monkeypatch)
    positions = [_pos("OLD"), _pos("KEEP")]
    pipeline, db = _pipeline(tmp_path, positions=positions)

    def _boom():
        raise RuntimeError("broker unreachable")

    pipeline._refresh_account_state = _boom
    ctx = _ctx(positions)
    (cleared, *_rest), ordered = _gate(
        pipeline,
        ctx,
        positions,
        sells=[_sell()],
        buys=[_buy()],
    )
    assert cleared is False
    assert ctx.rotation["withdrawn"] == "account_refresh_failed"
    (withdrawn,) = [e for e in _rotation_events(db) if e["outcome"] == "withdrawn"]
    assert withdrawn["reason"] == "account_refresh_failed"


def test_a_cover_is_projected_as_a_real_close_not_a_no_op(tmp_path):
    """A short is carried at a NEGATIVE quantity. Sizing the projection off
    the signed number made every COVER a no-op, which left a covered short
    in the projected book contributing an intraday change that will not be
    there — a projection looser than reality."""
    short = _pos("SHRT", qty=-10.0, price=100.0, intraday=800.0)
    positions = [short, _pos("KEEP", intraday=-100.0)]
    pipeline, _db = _pipeline(tmp_path, positions=positions)
    ctx = _ctx(positions, rotation=False)
    cover = SimpleNamespace(
        symbol="SHRT",
        action="COVER",
        allocation_pct=100.0,
        entry_price=0.0,
        stop_loss=0.0,
        reasoning="cover",
    )
    projected, _equity = ps._projected_post_sale_book(
        positions,
        100_000.0,
        [],
        [cover],
        quotes={"SHRT": {"bid": 99.50, "ask": 100.50}},
    )
    assert [p.symbol for p in projected] == ["KEEP"], "the covered short must leave the projected book"
    # And covering SPENDS cash, at the live ask the market cover fills at.
    cash = ps._projected_post_sale_cash(
        50_000.0, positions, [], [cover], quotes={"SHRT": {"bid": 99.50, "ask": 100.50}}
    )
    assert cash == pytest.approx(50_000.0 - 100.50 * 10)


def test_earlier_entries_drain_the_projected_budget_first(tmp_path, monkeypatch):
    """The submit loop subtracts each order's cost from the pool as it
    walks the list, so an entry ahead of the rotation's own buy has already
    taken its share by the time the rotation's is reached — proven by
    checking the pool is actually drained, not by the (now-retired)
    `below_min_notional` refusal that used to be the visible symptom.

    Fixed 2026-09-24: $2,100 - $2,000 leaves $100, which used to re-size the
    order under the flat $500 minimum and withdraw the whole rotation. That
    floor no longer gates the buy leg, so the rotation now clears with the
    replacement re-sized down to what the drained pool leaves."""
    _stub_sizing(monkeypatch, budget=2_100.0)
    positions = [_pos("OLD"), _pos("KEEP")]
    pipeline, db = _pipeline(tmp_path, positions=positions)
    ctx = _ctx(positions)
    earlier = _buy("FIRST", allocation_pct=2.0)  # $2,000 of a $100k book
    rotation_buy = _buy()  # 40 shares at $50 = $2,000

    # Alone it fits the $2,100 pool.
    (cleared, *_rest), _ordered = _gate(
        pipeline,
        ctx,
        positions,
        buys=[rotation_buy],
    )
    assert cleared is True

    # Behind the earlier entry, only $100 of pool is left for it — still
    # clears, now re-sized rather than refused.
    ctx = _ctx(positions)
    (cleared, *_rest), _ordered = _gate(
        pipeline,
        ctx,
        positions,
        sells=[_sell()],
        buys=[earlier, rotation_buy],
    )
    assert cleared is True
    assert not [e for e in _rotation_events(db) if e["outcome"] == "withdrawn"]


# ---------------------------------------------------------------------------
# Second adversary pass — the action/sign pairing, the short's pool draw,
# the unfilled-exit assumption, and the refreshed state reaching the loop
# ---------------------------------------------------------------------------


def test_a_sell_on_a_short_is_not_projected_as_a_close(tmp_path):
    """The SELL loop refuses a SELL on a short and the COVER loop refuses a
    COVER on a long. A projection that closed either removes exposure the
    real session keeps — and a book that keeps a LOSING position the
    projection dropped is more negative than the projection said."""
    short = _pos("SHRT", qty=-10.0, price=100.0, intraday=-400.0)
    long_ = _pos("LONG", qty=10.0, price=100.0, intraday=-400.0)
    positions = [short, long_]
    pipeline, _db = _pipeline(tmp_path, positions=positions)
    ctx = _ctx(positions, rotation=False)

    # A SELL aimed at the short: the loop skips it, so must the projection.
    projected, _e = ps._projected_post_sale_book(
        positions,
        100_000.0,
        [_sell("SHRT")],
        [],
        quotes={},
    )
    assert {p.symbol for p in projected} == {"SHRT", "LONG"}

    # A COVER aimed at the long: likewise.
    cover_long = SimpleNamespace(
        symbol="LONG",
        action="COVER",
        allocation_pct=100.0,
        entry_price=0.0,
        stop_loss=0.0,
        reasoning="cover",
    )
    projected, _e = ps._projected_post_sale_book(
        positions,
        100_000.0,
        [],
        [cover_long],
        quotes={},
    )
    assert {p.symbol for p in projected} == {"SHRT", "LONG"}


def test_an_earlier_short_draws_a_gross_pool_but_not_a_cash_one():
    """`if budget_is_gross or not is_short: entry_budget -= estimated_cost`
    — a short occupies gross exactly as a long does, so it draws the ladder
    pool. It does not touch the settled-cash fallback (D11)."""
    short = _buy("SHORTED", allocation_pct=10.0)
    short.action = "SHORT"
    assert ps._projected_entry_cost(
        short,
        100_000.0,
        budget_is_gross=True,
    ) == pytest.approx(10_000.0)
    assert (
        ps._projected_entry_cost(
            short,
            100_000.0,
            budget_is_gross=False,
        )
        == 0.0
    )
    # A long draws either pool.
    assert ps._projected_entry_cost(
        _buy(allocation_pct=2.0),
        100_000.0,
        budget_is_gross=False,
    ) == pytest.approx(2_000.0)


def test_the_other_exits_are_measured_not_projected(tmp_path, monkeypatch):
    """The rotation's close goes LAST, so by the time it is gated the other
    exits have already run and the account has been re-read. Their effect
    is whatever the broker now reports — nothing about them is assumed.

    Here the refresh reports OTHEREXIT already gone. The funding gate sees
    the book the broker actually has, plus the rotation's own close, and
    counts only the rotation's own proceeds as new cash."""
    seen = {}

    def _budget(pipeline, ctx, positions, equity, cash):
        seen["symbols"] = {p.symbol for p in positions}
        seen["cash"] = cash
        return 1_000_000.0, True, "stubbed"

    _stub_sizing(monkeypatch)
    monkeypatch.setattr(ps, "_entry_deployment_budget", _budget)
    stale = [_pos("OLD"), _pos("OTHEREXIT"), _pos("KEEP")]
    # What the broker reports once the earlier exit has gone through.
    after_other_exit = [_pos("OLD"), _pos("KEEP")]
    pipeline, _db = _pipeline(tmp_path, positions=after_other_exit, cash=59_950.0)
    ctx = _ctx(stale)
    (cleared, *_rest), ordered = _gate(
        pipeline,
        ctx,
        stale,
        sells=[_sell("OTHEREXIT"), _sell("OLD")],
    )
    assert [d.symbol for d in ordered] == ["OTHEREXIT", "OLD"], "the rotation's close must be ordered last"
    assert cleared is True
    assert seen["symbols"] == {"KEEP"}, "the measured book, minus only the rotation's own close"
    # The earlier exit's proceeds are already in the refreshed cash; only
    # the rotation's own are added on top of it.
    assert seen["cash"] == pytest.approx(59_950.0 + 99.50 * 10)


def test_an_unfilled_earlier_exit_is_seen_because_it_is_measured(
    tmp_path,
    monkeypatch,
):
    """The failure a projection cannot bound: an earlier exit does not fill,
    so its gross exposure is still on the book when the funding gate runs.

    A gate that assumed every planned exit fills would credit STUCK's room
    to the replacement and clear the sale. This one re-reads the account,
    finds the position still there, and measures the budget over a book
    that still carries it — which is possible only because the rotation's
    close is ordered last."""
    stale = [_pos("OLD"), _pos("STUCK"), _pos("KEEP")]
    _stub_sizing(
        monkeypatch,
        budget_by_book={
            # If STUCK's exit had filled there would be room for the
            # replacement. It did not, so the only book this may be measured
            # over is the one that still holds it — and that book has none.
            frozenset({"KEEP"}): 1_000_000.0,
            frozenset({"STUCK", "KEEP"}): 0.0,
        },
    )
    # The broker still reports STUCK: its exit did not fill.
    pipeline, db = _pipeline(tmp_path, positions=stale)
    ctx = _ctx(stale)
    (cleared, *_rest), _ordered = _gate(
        pipeline,
        ctx,
        stale,
        sells=[_sell("STUCK"), _sell("OLD")],
    )
    assert cleared is False
    (withdrawn,) = [e for e in _rotation_events(db) if e["outcome"] == "withdrawn"]
    assert withdrawn["reason"] == "buy_leg_would_be_refused:insufficient_cash"


def test_the_replacement_buy_dies_with_the_withdrawn_close(tmp_path, monkeypatch):
    """A withdrawal must not leave the BUY behind. The constructor sized it
    on the premise that the close frees room; buying anyway would put the
    book over the risk ceiling. `ctx.rotation` is therefore MARKED
    withdrawn rather than cleared, which is the state
    `_drop_rotation_buy_if_room_not_freed` already reads as no room."""
    _stub_sizing(monkeypatch, qty=0.0)
    positions = [_pos("OLD"), _pos("KEEP")]
    pipeline, db = _pipeline(tmp_path, positions=positions)
    ctx = _ctx(positions)
    buy, other = _buy(), _buy("UNRELATED")
    (cleared, *_rest), _ordered = _gate(
        pipeline,
        ctx,
        positions,
        buys=[other, buy],
    )
    assert cleared is False
    assert isinstance(ctx.rotation, dict), "clearing it would make the buy-side guard a no-op"
    kept = ps._drop_rotation_buy_if_room_not_freed(
        pipeline,
        ctx,
        [other, buy],
        {},
    )
    assert [d.symbol for d in kept] == ["UNRELATED"]
    assert "rotation_room_not_freed" in {s["reason"] for s in ctx.execution_skips}


def test_the_refreshed_book_is_handed_back_to_the_sell_loop(
    tmp_path,
    monkeypatch,
):
    """Clearing the gate against a fresh book while the SELL loop sizes off
    a stale one would reintroduce, one call later, the divergence
    `_projected_sale_qty` exists to prevent."""
    _stub_sizing(monkeypatch)
    stale = [_pos("OLD", qty=10.0), _pos("KEEP")]
    fresh = [_pos("OLD", qty=4.0), _pos("KEEP")]
    pipeline, _db = _pipeline(tmp_path, positions=fresh)
    pipeline.stub_total_value = 99_000.0
    pipeline.stub_cash = 44_000.0
    ctx = _ctx(stale)
    (cleared, positions, total_value, cash), _ordered = _gate(
        pipeline,
        ctx,
        stale,
    )
    assert cleared is True
    assert [p.qty for p in positions] == [4.0, 10.0]
    assert total_value == 99_000.0 and cash == 44_000.0


def test_the_sell_loop_still_consults_the_wire_barrier():
    """A mechanical guard, not a promise. The barrier that stops a
    ranked-margin close reaching the broker is one `continue` in a long
    loop; if a future edit removes it, `rotation_sell_reason`'s raise is
    never reached and the object guard becomes decorative."""
    source = (REPO_ROOT / "src" / "pipeline_stages.py").read_text() + (
        REPO_ROOT / "src" / "stage_execution.py"
    ).read_text()
    assert "rotation_final_reason = _rotation_ranked_margin_sell_reason(" in source
    assert "if rotation_final_reason is _ROTATION_SELL_REFUSED:" in source
    barrier = source.index("if rotation_final_reason is _ROTATION_SELL_REFUSED:")
    assert "continue" in source[barrier : barrier + 200]


# ---------------------------------------------------------------------------
# Fourth adversary pass — the COVER loop runs after the SELL loop
# ---------------------------------------------------------------------------


def _cover(symbol, allocation_pct=100.0):
    return SimpleNamespace(
        symbol=symbol,
        action="COVER",
        allocation_pct=allocation_pct,
        entry_price=0.0,
        stop_loss=0.0,
        reasoning="cover",
    )


def test_any_pending_cover_refuses_the_rotation_outright(tmp_path, monkeypatch):
    """The COVER loop runs AFTER the SELL loop, so at gate time no cover has
    happened. It also cannot be bounded: the daily-loss NUMERATOR's worst
    case keeps a pending exit that is down, the volatility-relative
    THRESHOLD's worst case drops it (sigma is a property of price history,
    not of today's P&L sign, and the limit is strictly increasing in
    sigma), and GROSS exposure's worst case keeps every one of them. Three
    consumers, three opposite selections, no single book. So the desk
    refuses rather than guesses."""
    _stub_sizing(monkeypatch)
    positions = [
        _pos("OLD", intraday=0.0),
        _pos("SHRT", qty=-10.0, intraday=4000.0),
        _pos("KEEP", intraday=0.0),
    ]
    pipeline, db = _pipeline(tmp_path, positions=positions)
    ctx = _ctx(positions)
    cleared, *_rest = ps._rotation_sell_gate(
        pipeline,
        ctx,
        _sell(),
        [_buy()],
        positions,
        100_000.0,
        50_000.0,
        [_cover("SHRT")],
    )
    assert cleared is False
    (withdrawn,) = [e for e in _rotation_events(db) if e["outcome"] == "withdrawn"]
    assert withdrawn["reason"] == "cover_pending"
    assert "SHRT" in withdrawn["detail"]
    # Refused before the account is even re-read: nothing to measure.
    assert pipeline.refreshed == []

    # A COVER the COVER loop would itself refuse does NOT block it: it
    # takes nothing off the book, so there is nothing to bound.
    ctx = _ctx(positions)
    cleared, *_rest = ps._rotation_sell_gate(
        pipeline,
        ctx,
        _sell(),
        [_buy()],
        positions,
        100_000.0,
        50_000.0,
        [_cover("KEEP")],  # KEEP is a LONG; a COVER against it is a no-op
    )
    assert cleared is True

    # And with no cover pending the same book clears.
    ctx = _ctx(positions)
    cleared, *_rest = ps._rotation_sell_gate(
        pipeline,
        ctx,
        _sell(),
        [_buy()],
        positions,
        100_000.0,
        50_000.0,
        [],
    )
    assert cleared is True


def test_only_a_cover_that_can_move_the_book_counts_as_pending():
    """A COVER the COVER loop will itself refuse takes zero shares off the
    book, changes no intraday P&L and moves no gross — so there is nothing
    about it to bound, and counting it would refuse the rotation for a
    cause with no effect, every session the PM keeps proposing it."""
    short = _pos("SHRT", qty=-10.0)
    long_ = _pos("LONGNAME", qty=10.0)
    positions = [short, long_]
    # A real cover of a real short counts, once, in order.
    assert ps._pending_cover_symbols(
        [_cover("shrt"), _cover("SHRT")],
        positions,
    ) == ("SHRT",)
    # A COVER against a LONG: the COVER loop refuses it outright.
    assert ps._pending_cover_symbols([_cover("LONGNAME")], positions) == ()
    # A COVER against nothing held at all.
    assert ps._pending_cover_symbols([_cover("ABSENT")], positions) == ()
    # `allocation_pct == 0` is the loop's own ambiguity skip.
    assert (
        ps._pending_cover_symbols(
            [_cover("SHRT", allocation_pct=0.0)],
            positions,
        )
        == ()
    )
    assert ps._pending_cover_symbols([], positions) == ()
    assert ps._pending_cover_symbols(None, positions) == ()


def test_the_replacement_is_never_a_name_the_desk_already_holds():
    """Why two more BUY-loop refusals cannot reach a rotation's replacement
    leg. `short_add_blocked` fires on adding to an existing short and the
    scale-in cancel-confirm on adding to an existing long; both require the
    BUY's symbol to be held. `evaluate_rotation_opportunity` picks the
    replacement from `[c for c in ranked if c.symbol not in held]`, so it
    never is. This pins the premise, so that if the candidate source ever
    changes the omission is caught rather than inherited."""
    from src.rotation import evaluate_rotation_opportunity
    from src.verdicts import RankedCandidate

    ranked = [
        RankedCandidate(symbol="HELD", direction="bullish", score=9.0),
        RankedCandidate(symbol="FRESH", direction="bullish", score=4.0),
    ]
    opportunity = evaluate_rotation_opportunity(
        ranked=ranked,
        blocked={"HELD": ["R5 no rung"]},
        held_symbols={"HELD"},
        headroom_pct=0.2,
        floor_pct=0.5,
    )
    assert opportunity is not None
    assert opportunity.new_symbol == "FRESH", (
        "the best-ranked name overall is HELD, and it is excluded because "
        "it is held — the replacement is always a name the desk does not "
        "already have"
    )


def test_a_position_the_broker_no_longer_holds_is_withdrawn_with_a_reason(
    tmp_path,
    monkeypatch,
):
    """A stop filled between research and execution. The SELL loop would
    drop this silently; a candidate must not leave the pipeline without a
    durable, per-symbol reason."""
    _stub_sizing(monkeypatch)
    stale = [_pos("OLD"), _pos("KEEP")]
    pipeline, db = _pipeline(tmp_path, positions=[_pos("KEEP")])
    ctx = _ctx(stale)
    (cleared, *_rest), _ordered = _gate(pipeline, ctx, stale)
    assert cleared is False
    (withdrawn,) = [e for e in _rotation_events(db) if e["outcome"] == "withdrawn"]
    assert withdrawn["reason"] == "held_position_gone"
    assert "rotation_withdrawn" in {s["reason"] for s in ctx.execution_skips}
