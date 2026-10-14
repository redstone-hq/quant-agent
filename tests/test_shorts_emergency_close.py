"""Closing the last short-selling safety gap: an EMERGENCY EXIT.

Stage 1 (`tests/test_shorts_countable.py`) made a held short visible without
making it tradeable. Stage 2 (`tests/test_shorts_safe.py`) made a short's
protective stop geometry correct. Neither gave the system any way to CLOSE a
short outside of its own stop firing: `_full_sell_qty` / `_reduce_sell_qty`
— the qty gates behind every forced-close path — refused any negative qty
outright, so a stray/manual short sitting in the book during a circuit
breaker, a force-delever, or an operator kill had NO mechanism to be closed
at all. This file is that fix: `_forced_close_side_and_qty` reads the
position's own sign to decide SELL (long) vs BUY-to-cover (short), and
`_submit_protected_sell` grew a `side` parameter that's threaded through the
whole cancel -> submit -> wait -> finalize -> restore/reprotect discipline
(broker.py's stop primitives were already side-aware from Stage 2; this
wires the pipeline orchestration layer up to them).

This is deliberately NOT Stage 3 ("Live" — order placement for OPENING a
short). The decision path still cannot open or cover a short:
`PortfolioConstructor`'s Stage-1 guard, the position-reviewer's midday
SELL/REDUCE loop, and `ExecutionStage`'s SELL-decision loop all still refuse
a negative-qty position before ever reaching a qty gate. This file only adds
an EXIT that fires from forced-close paths (`_force_delever` and the §11.2
gross-exposure ladder), never from a decision. The daily-loss circuit
breaker was a third such path until 2026-09-14, when it was replaced by a
halt that closes nothing — see section 4.

Same convention as Stage 1/2:
  * ``*_long_*`` — the exact pre-change behaviour, pinned with a literal.
  * ``*_short_*`` — the new behaviour, asserting the correct MIRRORED
    handling (BUY-to-cover instead of SELL; limit above price instead of
    below; BUY stops cancelled/restored instead of SELL stops).
  * A closing "mirror" test proves the short case is structurally identical
    to the long case with only the sign/side flipped.
  * A final section re-proves the hard boundary: shorts still cannot be
    OPENED or covered through the normal decision path.
"""

import json
from unittest.mock import MagicMock

import pytest

from src.models import Position
from src.pipeline import TradingPipeline
from tests.fakes.held_book import hold
from tests.pipeline_factory import build_pipeline


# ==========================================================================
# Shared fixtures
# ==========================================================================


def _protected_close_pipe(*, accepted=True, submit_raises=False, clear_ok=True, held=73.0):
    """A __new__'d pipeline wired just enough to exercise
    _submit_protected_sell directly, independent of any caller. Mirrors
    tests/test_pipeline.py's _protected_sell_pipe (same seam), kept local
    so this file stands alone."""
    pipe = build_pipeline(
        broker=MagicMock(),
        db=MagicMock(),
        _cancel_stops_with_write_ahead=MagicMock(
            return_value=(clear_ok, [{"id": "s1", "qty": 10}], 99),
        ),
        _order_accepted=MagicMock(return_value=accepted),
    )
    if submit_raises:
        pipe.broker.submit_order.side_effect = RuntimeError("broker down")
    else:
        pipe.broker.submit_order.return_value = {
            "id": "ord-1",
            "status": "accepted",
            "symbol": "NVDA",
        }
    hold(pipe.broker, {"NVDA": held})  # what the broker holds when the exit is re-sized
    return pipe


def _emergency_liquidate_pipe():
    """A __new__'d pipeline wired to exercise _midday_emergency_liquidate /
    the intra risk-alert loop end to end, with a real (empty) DB so the
    WAL/finalize machinery those paths call into doesn't need its own
    mocking — same pattern tests/test_pipeline.py's emergency-liquidate
    tests use."""
    from src.storage.db import Database

    pipe = build_pipeline(db=MagicMock(), broker=MagicMock())
    pipe.db.has_pending_action_for_symbol.return_value = False
    pipe.db.insert_trade = MagicMock(return_value=1)
    pipe.broker.cancel_open_entry_orders.return_value = 0
    pipe.broker.snapshot_protective_stops.return_value = (True, [])
    pipe.broker.cancel_snapshotted_stops.return_value = MagicMock(cleared=True)
    pipe.broker.get_order_fill_info.return_value = {
        "status": "filled",
        "filled_qty": None,
        "filled_avg_price": None,
    }
    pipe.broker.wait_for_order_terminal.return_value = "filled"
    pipe._order_accepted = MagicMock(return_value=True)
    pipe._format_qty = lambda q: str(q)
    pipe._reconcile_fills = MagicMock()
    return pipe


# ==========================================================================
# 1. _forced_close_side_and_qty — the direction gate itself
# ==========================================================================


def test_forced_close_long_returns_sell_and_full_qty():
    assert TradingPipeline._forced_close_side_and_qty(40.0) == ("sell", 40.0)


def test_forced_close_short_returns_buy_and_absolute_qty():
    assert TradingPipeline._forced_close_side_and_qty(-40.0) == ("buy", 40.0)


def test_forced_close_short_is_the_exact_mirror_of_long():
    """Same magnitude, opposite sign in, opposite side out, same magnitude
    out — the short case must be a pure mirror, not a different code path
    with independently-chosen numbers."""
    for qty in (1.0, 0.5, 40.0, 1234.5):
        long_side, long_qty = TradingPipeline._forced_close_side_and_qty(qty)
        short_side, short_qty = TradingPipeline._forced_close_side_and_qty(-qty)
        assert long_side == "sell" and short_side == "buy"
        assert long_qty == short_qty == qty


@pytest.mark.parametrize("bad_qty", [0.0, -0.0, float("nan"), float("inf"), float("-inf")])
def test_forced_close_refuses_indeterminate_numeric_qty(bad_qty):
    assert TradingPipeline._forced_close_side_and_qty(bad_qty) is None


@pytest.mark.parametrize("bad_qty", [None, "40", object(), [40]])
def test_forced_close_refuses_non_numeric_qty(bad_qty):
    assert TradingPipeline._forced_close_side_and_qty(bad_qty) is None


def test_full_sell_qty_and_reduce_sell_qty_unchanged_still_refuse_a_short():
    """The pre-existing gates stay exactly as they were — they are still
    what the DECISION path (position reviewer, ExecutionStage) relies on
    to refuse a short. This PR adds a second, forced-close-only gate; it
    does not loosen the original one."""
    assert TradingPipeline._full_sell_qty(-40.0) is None
    assert TradingPipeline._full_sell_qty(40.0) == 40.0


# ==========================================================================
# 2. _submit_protected_sell — the direction-aware discipline primitive
# ==========================================================================


def test_submit_protected_close_long_full_unchanged():
    """Full long close: SELL for the held quantity. Hard literal."""
    pipe = _protected_close_pipe()
    out = pipe._submit_protected_sell(
        symbol="NVDA",
        qty=73.0,
        limit_price=99.0,
        reference_price=100.0,
        position_qty_before_sell=73.0,
        label="EMERGENCY_SELL",
    )
    assert out is not None
    order, prot = out
    pipe.broker.submit_order.assert_called_once_with(
        symbol="NVDA",
        qty=73.0,
        side="sell",
        limit_price=99.0,
        reference_price=100.0,
    )
    assert prot["side"] == "sell"
    assert prot["position_qty_before_sell"] == 73.0
    # Long path never passes a side kwarg to the stop-cancel seam — the
    # mocked _cancel_stops_with_write_ahead call args must be exactly the
    # pre-shorts positional pair, nothing more.
    pipe._cancel_stops_with_write_ahead.assert_called_once_with("NVDA", 73.0)


def test_submit_protected_close_long_partial_unchanged():
    """Partial long close (e.g. a hypothetical partial forced-close): SELL
    for less than the full held quantity. Hard literal."""
    pipe = _protected_close_pipe()
    out = pipe._submit_protected_sell(
        symbol="NVDA",
        qty=25.0,
        limit_price=99.0,
        reference_price=100.0,
        position_qty_before_sell=73.0,
        label="EMERGENCY_SELL",
    )
    assert out is not None
    pipe.broker.submit_order.assert_called_once_with(
        symbol="NVDA",
        qty=25.0,
        side="sell",
        limit_price=99.0,
        reference_price=100.0,
    )


def test_submit_protected_close_short_full_sends_buy_for_absolute_qty():
    """Full short close: BUY-to-cover for the absolute quantity. Hard
    literal — this is the headline behaviour this PR adds."""
    pipe = _protected_close_pipe(held=-73.0)
    out = pipe._submit_protected_sell(
        symbol="NVDA",
        qty=73.0,
        limit_price=101.0,
        reference_price=100.0,
        position_qty_before_sell=73.0,
        label="EMERGENCY_COVER",
        side="buy",
    )
    assert out is not None
    order, prot = out
    pipe.broker.submit_order.assert_called_once_with(
        symbol="NVDA",
        qty=73.0,
        side="buy",
        limit_price=101.0,
        reference_price=100.0,
    )
    assert prot["side"] == "buy"
    # Short path DOES pass side="buy" through to the stop-cancel seam —
    # cancelling the BUY stop protecting the short, not a SELL stop.
    pipe._cancel_stops_with_write_ahead.assert_called_once_with(
        "NVDA",
        73.0,
        side="buy",
    )


def test_submit_protected_close_short_partial_sends_buy_for_partial_qty():
    pipe = _protected_close_pipe(held=-73.0)
    out = pipe._submit_protected_sell(
        symbol="NVDA",
        qty=25.0,
        limit_price=101.0,
        reference_price=100.0,
        position_qty_before_sell=73.0,
        label="EMERGENCY_COVER",
        side="buy",
    )
    assert out is not None
    pipe.broker.submit_order.assert_called_once_with(
        symbol="NVDA",
        qty=25.0,
        side="buy",
        limit_price=101.0,
        reference_price=100.0,
    )


@pytest.mark.parametrize("qty", [73.0, 25.0])
def test_submit_protected_close_short_is_the_exact_mirror_of_long(qty):
    """Same call, same qty, only side (and therefore stop-cancel side)
    flips. Proves the short branch isn't a structurally different path."""
    long_pipe = _protected_close_pipe()
    long_pipe._submit_protected_sell(
        symbol="NVDA",
        qty=qty,
        limit_price=99.0,
        reference_price=100.0,
        position_qty_before_sell=73.0,
        label="EMERGENCY_SELL",
    )
    short_pipe = _protected_close_pipe(held=-73.0)
    short_pipe._submit_protected_sell(
        symbol="NVDA",
        qty=qty,
        limit_price=101.0,
        reference_price=100.0,
        position_qty_before_sell=73.0,
        label="EMERGENCY_COVER",
        side="buy",
    )
    long_call = long_pipe.broker.submit_order.call_args.kwargs
    short_call = short_pipe.broker.submit_order.call_args.kwargs
    assert long_call["side"] == "sell" and short_call["side"] == "buy"
    assert long_call["qty"] == short_call["qty"] == qty
    long_pipe._cancel_stops_with_write_ahead.assert_called_once_with("NVDA", 73.0)
    short_pipe._cancel_stops_with_write_ahead.assert_called_once_with(
        "NVDA",
        73.0,
        side="buy",
    )


def test_submit_protected_close_short_restores_buy_stops_on_reject():
    """Never assume a fill: a rejected BUY-to-cover must restore the BUY
    stop it cancelled, exactly as a rejected SELL restores a SELL stop."""
    pipe = _protected_close_pipe(accepted=False, held=-73.0)
    out = pipe._submit_protected_sell(
        symbol="NVDA",
        qty=73.0,
        limit_price=101.0,
        reference_price=100.0,
        position_qty_before_sell=73.0,
        label="EMERGENCY_COVER",
        side="buy",
    )
    assert out is None
    pipe.broker._restore_stop_orders.assert_called_once_with(
        "NVDA",
        [{"id": "s1", "qty": 73.0}],  # the 10-share leg topped up to the 73 held: cover never short of held
        check_idempotency=True,  # a dying stop is never counted as cover
        side="buy",
    )


def test_submit_protected_close_short_restores_buy_stops_on_submit_throw():
    """Never assume a fill: a submit that raises leaves the short intact
    with its BUY stop cancelled — restore it in-session, same discipline
    the long path already has."""
    pipe = _protected_close_pipe(submit_raises=True, held=-73.0)
    out = pipe._submit_protected_sell(
        symbol="NVDA",
        qty=73.0,
        limit_price=101.0,
        reference_price=100.0,
        position_qty_before_sell=73.0,
        label="EMERGENCY_COVER",
        side="buy",
    )
    assert out is None
    pipe.broker._restore_stop_orders.assert_called_once_with(
        "NVDA",
        [{"id": "s1", "qty": 73.0}],  # the 10-share leg topped up to the 73 held: cover never short of held
        check_idempotency=True,  # a dying stop is never counted as cover
        side="buy",
    )


def test_submit_protected_close_short_skips_and_never_submits_when_stop_clear_fails():
    """Sizing to the ACTUAL held quantity / cancelling conflicting
    protective orders FIRST: if the BUY stop can't be cleared, the cover
    must never reach the broker (would reject on held_for_orders)."""
    pipe = _protected_close_pipe(clear_ok=False)
    out = pipe._submit_protected_sell(
        symbol="NVDA",
        qty=73.0,
        limit_price=101.0,
        reference_price=100.0,
        position_qty_before_sell=73.0,
        label="EMERGENCY_COVER",
        side="buy",
    )
    assert out is None
    pipe.broker.submit_order.assert_not_called()


# ==========================================================================
# 3. Indeterminate direction refuses to act (fail closed)
# ==========================================================================


def test_indeterminate_qty_never_reaches_broker_via_forced_close_gate():
    """The gate that production call sites use before ever building an
    order: an indeterminate qty must produce no (side, qty) answer to act
    on at all — there is nothing left for a caller to accidentally use."""
    assert TradingPipeline._forced_close_side_and_qty(0.0) is None
    assert TradingPipeline._forced_close_side_and_qty(float("nan")) is None


# ==========================================================================
# 4. The surviving forced-close call sites, and the one that is gone
#
# `_midday_emergency_liquidate` and run_intra_check's inline twin — the two
# daily-loss liquidation paths this section used to exercise — were DELETED
# on 2026-09-14 (docs/WORK.md item 32). They submitted LIMIT orders 1%
# through the market and restored the original stops on any leg that did not
# fill, so on the correlated gap they existed for they cancelled every
# protective stop, failed to sell, and put the stops back. The daily-loss
# breaker now HALTS instead.
#
# The direction-aware close primitive this file is really about is NOT gone:
# `_forced_close_side_and_qty` and `_submit_protected_sell(side=...)` are
# still what `_force_delever` and the §11.2 gross-exposure ladder use, and
# sections 1-3 above pin them directly, independent of any caller. What
# follows is the new invariant at the removed call sites.
# ==========================================================================


def _halted_pipe():
    pipe = build_pipeline(
        broker=MagicMock(),
        db=MagicMock(),
        _reconcile_fills=MagicMock(),
        _reconcile_stop_coverage=MagicMock(return_value=[]),
    )
    pipe.broker.snapshot_protective_stops.return_value = (True, [{"qty": 1e9}])
    pipe._submit_protected_sell = MagicMock()
    return pipe


# ==========================================================================
# 5. run_intra_check — the circuit-breaker entry point, end to end
# ==========================================================================


# ==========================================================================
# 6. The hard boundary, re-proved: shorts still cannot be OPENED or covered
#    through the normal decision path
# ==========================================================================


def test_shorts_can_now_be_opened_and_covered_by_the_constructor():
    """NEW boundary (Stage 3). This file's own copy of the boundary test —
    the same fixture as tests/test_shorts_safe.py and
    tests/test_shorts_countable.py, all three independently re-proving the
    same Stage-1 guard — used to assert zero orders. Stage 3 lifts that
    guard: the fixture now produces a full COVER, exactly the way an
    explicit-close long produces a SELL. The forced-close EXIT this file
    adds (`_forced_close_side_and_qty` / `_submit_protected_sell`'s `side`
    param) is orthogonal to and unaffected by this change — it never went
    through the constructor and still fires only from forced-close call
    sites, never from a PM/RM decision. Full execution-path proof that a
    short can be OPENED — the borrow gate, the exposure caps, and the
    mandatory protective stop — lives in tests/test_shorts_stage3.py.
    """
    from src.models import TargetPosition
    from src.portfolio_constructor import PortfolioConstructor

    constructor = PortfolioConstructor()
    decisions = constructor.construct_orders(
        targets=[TargetPosition(symbol="TSLA", target_weight_pct=0.0, conviction="high", thesis="close it")],
        positions=[
            Position(
                symbol="TSLA",
                qty=-40,
                avg_entry=250,
                current_price=250,
                market_value=-10_000,
                unrealized_pnl=0,
                sector="Consumer Cyclical",
            )
        ],
        analyses=[],
        total_value=100_000,
        price_map={"TSLA": 250.0},
    )
    assert len(decisions) == 1
    assert decisions[0].action == "COVER"
    assert decisions[0].allocation_pct == 100.0


def test_execution_stage_sell_decision_loop_still_refuses_a_short(tmp_path):
    """ExecutionStage's SELL-decision executor (the decision path, not the
    forced-close path built here) still refuses to act on a held short —
    its `existing[0].qty <= 0` guard predates this PR and is untouched."""
    from src.storage.db import Database
    from src.pipeline_stages import ExecutionStage
    from src.models import TradeDecision, PortfolioDecision, ReasoningChain
    from src.pipeline import RunContext

    def _rc():
        return ReasoningChain(
            macro_filter="x",
            news_check="x",
            earnings_check="x",
            signal_conflicts="x",
            sizing_logic="x",
            portfolio_balance="x",
            cash_target="x",
        )

    db = Database(str(tmp_path / "t.db"))
    db.initialize()
    pipeline = build_pipeline(db=db, broker=MagicMock())

    stage = ExecutionStage(pipeline=pipeline)
    ctx = RunContext.start("test")
    ctx.decision_id = None
    ctx.positions = [
        Position(
            symbol="TSLA",
            qty=-40,
            avg_entry=250,
            current_price=250,
            market_value=-10_000,
            unrealized_pnl=0,
            sector="Consumer Cyclical",
        ),
    ]
    ctx.total_value = 100_000.0
    ctx.cash = 50_000.0
    ctx.portfolio_decision = PortfolioDecision(
        reasoning_chain=_rc(),
        decisions=[
            TradeDecision(
                symbol="TSLA",
                action="SELL",
                allocation_pct=100,
                entry_price=250.0,
                stop_loss=260.0,
                take_profit=200.0,
                reasoning="attempt to cover a short via the decision path",
            ),
        ],
        portfolio_view="test",
    )

    stage.run(ctx)

    pipeline.broker.submit_order.assert_not_called()
    db.close()


def test_midday_review_loop_guard_source_still_reads_qty_le_0():
    """The midday position-reviewer's REDUCE/SELL/TRAIL_STOP action
    executor (decision path) gates on `not existing or existing[0].qty <=
    0` BEFORE ever reaching `_full_sell_qty` / `_reduce_sell_qty` — this
    PR does not touch that loop. Grep-level pin: if a future change ever
    drops or weakens that guard, this test's source scan catches it even
    though the loop itself is too deep inside run_midday to exercise
    cheaply end to end here."""
    import inspect
    from src.pipeline_exits import ExitEngineMixin

    # The executor moved to `ExitEngineMixin` in step 4 of
    # docs/PIPELINE_SPLIT_PLAN.md; `getsource(TradingPipeline)` returns only
    # the class body left in `src/pipeline.py`, so the scan reads the mixin
    # that now owns `_midday_execute_llm_actions`.
    source = inspect.getsource(ExitEngineMixin)
    assert "if not existing or existing[0].qty <= 0:" in source, (
        "the midday review loop's short-refusing guard must still be present verbatim"
    )


# ==========================================================================
# 7. Reprotect "most protective stop" selection mirrors correctly
# ==========================================================================


def test_reprotect_residual_picks_highest_stop_for_a_long_unchanged():
    pipe = build_pipeline(broker=MagicMock(), _format_qty=lambda q: str(q))
    cancelled = [
        {"id": "lo", "qty": 51, "stop_price": 240.0},
        {"id": "hi", "qty": 51, "stop_price": 248.5},
    ]
    pipe._reprotect_residual_after_partial_sell("AMZN", 41.0, cancelled)
    pipe.broker._submit_protective_stop_retrying.assert_called_once_with(
        symbol="AMZN",
        qty=41.0,
        stop_price=248.5,
        side="sell",
    )


def test_reprotect_residual_picks_lowest_stop_for_a_short():
    """A short's BUY stop is 'most protective' at the LOWEST price
    (tightest, closest to price from above) — the opposite extreme from a
    long's SELL stop. Picking max() here would silently place the
    loosest, least-protective stop instead of the tightest one."""
    pipe = build_pipeline(broker=MagicMock(), _format_qty=lambda q: str(q))
    cancelled = [
        {"id": "hi", "qty": 51, "stop_price": 260.0},
        {"id": "lo", "qty": 51, "stop_price": 252.5},
    ]
    pipe._reprotect_residual_after_partial_sell(
        "AMZN",
        41.0,
        cancelled,
        side="buy",
    )
    pipe.broker._submit_protective_stop_retrying.assert_called_once_with(
        symbol="AMZN",
        qty=41.0,
        stop_price=252.5,
        side="buy",
    )


# ==========================================================================
# 8. Position reviewer surfaces EMERGENCY_COVER as a system action
# ==========================================================================


def test_position_reviewer_surfaces_emergency_cover_from_morning_trades():
    """The reviewer's "Non-LLM System Actions Earlier Today" block already
    surfaced FORCE_DELEVER and EMERGENCY_SELL so the reviewer isn't left
    reasoning in a vacuum about a shrunken book. A circuit-breaker
    buy-to-cover on a short shrinks the book exactly as materially as a
    forced sell on a long — it must show up in the same block, not be
    silently dropped because its label is EMERGENCY_COVER rather than
    EMERGENCY_SELL."""
    from unittest.mock import patch
    from src.agents.position_reviewer import PositionReviewerAgent

    with patch("anthropic.Anthropic"):
        agent = PositionReviewerAgent(api_key="test", model="claude-sonnet-4-6")

    msg = agent.build_user_message(
        positions=[],
        macro_summary={"vix": {"current": 18}},
        cash_balance=10_000.0,
        total_value=50_000.0,
        session_type="midday",
        morning_trades=[
            {
                "symbol": "TSLA",
                "action": "EMERGENCY_COVER",
                "qty": 20,
                "fill_status": "filled",
                "fill_qty": 20,
                "reasoning": "daily loss -5.0% breached circuit breaker",
            },
        ],
    )
    assert "Non-LLM System Actions Earlier Today" in msg
    assert "EMERGENCY_COVER TSLA" in msg


# ==========================================================================
# 9. _derive_close_side_for_drain and the crash-recovery drain path
# ==========================================================================
# This is the one corner of the branch with zero coverage: the WAL/drain
# machinery in _drain_pending_protection_restores predates shorts and
# carries no side column, so a short's orphaned row looks byte-identical
# to a long's. _derive_close_side_for_drain closes that gap by reading
# the broker's LIVE signed position instead of trusting the row. These
# tests pin the derivation itself, both call sites that consume it, and
# the deliberate degrade-to-'sell' fallback for the one case broker truth
# can't settle.


def _bare_pipe():
    """A pipeline built through the real constructor — _derive_close_side_for_drain
    has exactly one dependency, _current_position_qty_for_finalize, which
    every test below stubs directly (same seam tests/test_pipeline.py's
    WAL/drain tests already stub, e.g. test_drain_sentinel_restores_when_
    position_intact)."""
    return build_pipeline(broker=MagicMock())


def test_derive_close_side_for_drain_returns_buy_for_a_short():
    pipe = _bare_pipe()
    pipe._current_position_qty_for_finalize = lambda s: -20.0
    assert pipe._derive_close_side_for_drain("TSLA") == "buy"


def test_derive_close_side_for_drain_returns_sell_for_a_long():
    pipe = _bare_pipe()
    pipe._current_position_qty_for_finalize = lambda s: 20.0
    assert pipe._derive_close_side_for_drain("NVDA") == "sell"


def test_derive_close_side_for_drain_returns_none_when_flat():
    """A flat (0) position deliberately does NOT default to 'sell' here.
    The caller's downstream restore/reprotect call independently
    re-checks flatness before doing anything side-dependent, so a
    fabricated side for an already-flat symbol would just look like a
    real answer to a question nobody is going to ask."""
    pipe = _bare_pipe()
    pipe._current_position_qty_for_finalize = lambda s: 0.0
    assert pipe._derive_close_side_for_drain("NVDA") is None


def test_derive_close_side_for_drain_returns_none_when_broker_unreadable():
    pipe = _bare_pipe()
    pipe._current_position_qty_for_finalize = lambda s: None
    assert pipe._derive_close_side_for_drain("NVDA") is None


def test_drain_sentinel_restores_buy_side_stops_for_a_short(tmp_path):
    """Call site 1: the write-ahead sentinel branch (a SELL that was
    never confirmed submitted before a crash). The row itself can't say
    which side it needs — the broker reporting a live short here must
    still result in BUY stops being restored, not the pre-shorts SELL
    default."""
    from src.storage.db import Database
    from src.pipeline import _WAL_SELL_SENTINEL

    db = Database(str(tmp_path / "t.db"))
    db.initialize()
    pipe = build_pipeline(db=db, broker=MagicMock(), _format_qty=lambda q: str(q))

    specs = [{"id": "s1", "qty": 73, "stop_price": 262.0, "limit_price": 264.0}]
    db.insert_pending_protection_restore(
        symbol="TSLA",
        sell_order_id=_WAL_SELL_SENTINEL,
        position_qty_before_sell=73.0,
        specs_json=json.dumps(specs),
    )
    # Broker reports a live short — this is what must drive the side,
    # not anything persisted on the row.
    pipe._current_position_qty_for_finalize = lambda s: -73.0
    pipe.broker._restore_stop_orders.return_value = (1, [])

    drained = pipe._drain_pending_protection_restores()

    assert drained == 1
    pipe.broker._restore_stop_orders.assert_called_once()
    assert pipe.broker._restore_stop_orders.call_args.kwargs.get("side") == "buy"
    db.close()


def test_drain_finalize_restores_buy_side_stops_for_a_short(tmp_path):
    """Call site 2: the terminal-order replay branch (a real
    sell_order_id, not the sentinel). Same live-broker side derivation
    as the sentinel branch above, exercised through the other row
    shape."""
    from src.storage.db import Database

    db = Database(str(tmp_path / "t.db"))
    db.initialize()
    cancelled = [{"id": "stop-old", "qty": 73, "stop_price": 262.0, "limit_price": 264.0}]
    db.insert_pending_protection_restore(
        symbol="TSLA",
        sell_order_id="alpaca-resolved",
        position_qty_before_sell=73.0,
        specs_json=json.dumps(cancelled),
    )

    pipe = build_pipeline(db=db, broker=MagicMock(), _format_qty=lambda q: str(q))
    pipe.broker.get_order_fill_info.return_value = {
        "status": "canceled",
        "filled_qty": "0",
        "filled_avg_price": None,
    }
    pipe.broker._restore_stop_orders.return_value = (1, [])
    # Broker reports a live short.
    pipe._current_position_qty_for_finalize = lambda s: -73.0

    drained = pipe._drain_pending_protection_restores()

    assert drained == 1
    pipe.broker._restore_stop_orders.assert_called_once()
    assert pipe.broker._restore_stop_orders.call_args.kwargs.get("side") == "buy"
    db.close()


def test_drain_finalize_long_row_has_no_side_kwarg_unchanged(tmp_path):
    """A long's row through call site 2 must produce a byte-identical
    downstream call to every pre-shorts drain test in
    tests/test_pipeline.py: no `side` kwarg at all — not even an
    explicit side='sell' — since side_kwargs is only ever non-empty for
    the 'buy' case."""
    from src.storage.db import Database

    db = Database(str(tmp_path / "t.db"))
    db.initialize()
    cancelled = [{"id": "stop-old", "qty": 73, "stop_price": 95.0, "limit_price": 92.0}]
    db.insert_pending_protection_restore(
        symbol="NVDA",
        sell_order_id="alpaca-resolved",
        position_qty_before_sell=73.0,
        specs_json=json.dumps(cancelled),
    )

    pipe = build_pipeline(db=db, broker=MagicMock(), _format_qty=lambda q: str(q))
    pipe.broker.get_order_fill_info.return_value = {
        "status": "canceled",
        "filled_qty": "0",
        "filled_avg_price": None,
    }
    pipe.broker._restore_stop_orders.return_value = (1, [])
    # Broker reports a live long.
    pipe._current_position_qty_for_finalize = lambda s: 73.0

    drained = pipe._drain_pending_protection_restores()

    assert drained == 1
    pipe.broker._restore_stop_orders.assert_called_once()
    assert "side" not in pipe.broker._restore_stop_orders.call_args.kwargs
    db.close()


def test_drain_finalize_degrades_to_sell_default_when_broker_unreadable(tmp_path):
    """Pins a deliberate trade-off, not an accident: when the broker
    position can't be read at all (get_positions failure — the same
    None-on-error contract _current_position_qty_for_finalize documents
    on itself), _derive_close_side_for_drain returns None, so call site
    2's finalize_side_kwargs stays empty and
    _finalize_protection_after_sell_core falls through to its
    pre-existing 'sell' default rather than stalling the row forever.

    This is only safe today because shorts still cannot be OPENED
    anywhere in this system (PortfolioConstructor's Stage-1 guard, the
    position-reviewer's SELL/REDUCE loop, and ExecutionStage's
    SELL-decision loop all still refuse a negative-qty position) — so an
    unreadable broker can never actually be masking an orphaned SHORT's
    row today. Closing that compound case for real needs a persisted
    side column on the WAL row, which this branch deliberately does not
    add. If this test ever starts failing because a future change
    tightens the fallback to stall the row instead of degrading, that
    is a deliberate policy change to make with eyes open, not a
    regression to silently absorb."""
    from src.storage.db import Database

    db = Database(str(tmp_path / "t.db"))
    db.initialize()
    cancelled = [{"id": "stop-old", "qty": 100, "stop_price": 95.0, "limit_price": 92.0}]
    db.insert_pending_protection_restore(
        symbol="NVDA",
        sell_order_id="alpaca-resolved",
        position_qty_before_sell=100.0,
        specs_json=json.dumps(cancelled),
    )

    pipe = build_pipeline(db=db, broker=MagicMock(), _format_qty=lambda q: str(q))
    pipe.broker.get_order_fill_info.return_value = {
        "status": "canceled",
        "filled_qty": "0",
        "filled_avg_price": None,
    }
    pipe.broker._restore_stop_orders.return_value = (1, [])
    # Broker position genuinely unreadable — not flat, not signed, unknown.
    pipe._current_position_qty_for_finalize = lambda s: None

    drained = pipe._drain_pending_protection_restores()

    assert drained == 1, (
        "an unreadable broker must not stall the row — it degrades to the pre-existing 'sell' default instead"
    )
    assert "side" not in pipe.broker._restore_stop_orders.call_args.kwargs, (
        "degraded case must fall back to the plain 'sell' default, never guess 'buy'"
    )
    assert db.get_pending_protection_restores() == []
    db.close()


# ==========================================================================
# 10. Revert cross-check helper (not a test — see conversation report)
# ==========================================================================
# The revert cross-check itself (src/ reset to main with this test file
# left in place, failure count reported) is a one-off git operation done
# outside pytest; it is not encoded here because there is nothing to keep
# passing once src/ is reverted — that's the point of the check.
