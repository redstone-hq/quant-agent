"""Bounded entry re-peg — the money path, tested from both ends.

A re-peg is an Alpaca order REPLACEMENT, and a replacement mints a NEW
order id: the id we were tracking is dead the instant the PATCH is
accepted. Every test here exists because getting one of those transitions
wrong costs real (paper) money in one of two ways — an untracked working
order, or the same idea bought twice.

The bias throughout is asymmetric on purpose. Where a branch is ambiguous
the correct behaviour is DO NOTHING (leave the order working at its
current price), never DO IT AGAIN. Several tests below assert exactly
that: no replacement was attempted.

No network. The broker is a MagicMock in every test; `submit_order`,
`replace_order_by_id` and friends are never reachable.
"""

import pytest
from unittest.mock import MagicMock

from src.config import ExecutionConfig
from src.models import PortfolioDecision, ReasoningChain, TradeDecision
from src.pipeline_context import RunContext
from src.pipeline_stages import (
    ExecutionStage,
    _WAL_REPEG_SENTINEL,
    _repeg_entry_order,
    _repeg_settings,
)
from src.storage.db import Database


# --------------------------------------------------------------------------
# harness
# --------------------------------------------------------------------------

REFERENCE = 100.0  # verified reference price at submission
CEILING_40BPS = 100.40  # REFERENCE * (1 + 40/10_000)


@pytest.fixture(autouse=True)
def limit_entries(monkeypatch):
    """Entries default to plain market orders (owner ruling 2026-10-09); this
    test exercises the marketable-limit path behind `execution.entry_order_type`."""
    monkeypatch.setattr("src.stage_execution_parts.entry_order_pricing.entry_orders_are_market", lambda _p: False)


@pytest.fixture
def db(tmp_path):
    database = Database(str(tmp_path / "repeg.db"))
    database.initialize()
    yield database
    database.close()


def _exec_cfg(**overrides) -> ExecutionConfig:
    base = dict(
        max_entry_slippage_bps=40.0,
        repeg_enabled=True,
        repeg_poll_seconds=1.0,
    )
    base.update(overrides)
    return ExecutionConfig(**base)


def _pipeline(db, cfg: ExecutionConfig | None = None):
    """A pipeline double with a REAL database — the WAL is the point."""
    pipeline = MagicMock()
    pipeline.broker.list_open_orders_checked.return_value = (True, [])  # listing read, nothing open
    pipeline.db = db
    pipeline.config.execution = cfg if cfg is not None else _exec_cfg()
    broker = pipeline.broker
    # `_TERMINAL_ORDER_STATES` is read off the broker; a MagicMock attribute
    # would make every `in` check raise.
    from src.execution.broker import AlpacaBroker

    broker._TERMINAL_ORDER_STATES = AlpacaBroker._TERMINAL_ORDER_STATES
    broker._ORDER_REPLACEABLE_STATES = AlpacaBroker._ORDER_REPLACEABLE_STATES
    broker._ORDER_PRE_EXCHANGE_STATES = AlpacaBroker._ORDER_PRE_EXCHANGE_STATES
    # Default: the venue has acknowledged the order (`new`) by the time the
    # rest window ends, so the reprice gate is open. Tests of the gate
    # itself override both of these.
    broker.wait_for_order_terminal.return_value = "new"
    broker.wait_for_order_at_exchange.return_value = "new"
    broker.get_order_fill_info.return_value = {
        "status": "new",
        "filled_qty": 0.0,
        "filled_avg_price": 0.0,
    }
    broker.get_latest_quote.return_value = {"ask_price": 100.30, "bid_price": 100.20}
    broker.cancel_entry_order.return_value = True
    return pipeline


def _ctx() -> RunContext:
    ctx = RunContext.start("morning")
    ctx.decision_id = "run-x-dec-repeg"
    return ctx


def _spec(db, *, order_id="ord-1", limit_price=100.10, qty=10):
    """A working entry order plus the trades row that tracks it."""
    row_id = db.insert_trade(
        symbol="NVDA",
        action="BUY",
        qty=qty,
        price=limit_price,
        reasoning="repeg test",
        run_id="run-x",
        broker_order_id=order_id,
        fill_status="submitted",
        stop_loss=90.0,
    )
    return {
        "symbol": "NVDA",
        "order_id": order_id,
        "stop_price": 95.0,
        "qty": qty,
        "reference_price": REFERENCE,
        "limit_price": limit_price,
        "trade_row_id": row_id,
    }


def _tracked_order_id(db, row_id) -> str:
    row = db.execute(
        "SELECT broker_order_id FROM trades WHERE id = ?",
        (row_id,),
    ).fetchone()
    return row[0]


def _replace_returns(*ids):
    """Successive replacement responses, each minting the next id."""
    seq = [{"id": i, "status": "accepted"} for i in ids]
    return MagicMock(side_effect=seq)


# --------------------------------------------------------------------------
# off by default
# --------------------------------------------------------------------------


def test_repeg_is_off_by_default():
    """A fresh ExecutionConfig must not chase anything."""
    assert ExecutionConfig().repeg_enabled is False


def test_disabled_makes_no_broker_calls(db):
    pipeline = _pipeline(db, _exec_cfg(repeg_enabled=False))
    spec = _spec(db)

    order_id, carried = _repeg_entry_order(pipeline, _ctx(), spec)

    assert (order_id, carried) == ("ord-1", 0.0)
    pipeline.broker.replace_entry_limit.assert_not_called()
    pipeline.broker.wait_for_order_terminal.assert_not_called()
    assert db.get_pending_repegs() == []


def test_magicmock_config_never_reads_as_enabled():
    """~58 pipeline tests pass a MagicMock config whose attributes are all
    truthy. A MagicMock must never authorise replacing live orders."""
    pipeline = MagicMock()
    pipeline.broker.list_open_orders_checked.return_value = (True, [])  # listing read, nothing open
    assert _repeg_settings(pipeline) is None


def test_settings_fall_back_to_safe_values_on_garbage(db):
    pipeline = _pipeline(db)
    pipeline.config.execution = MagicMock()
    pipeline.config.execution.repeg_enabled = True
    pipeline.config.execution.repeg_poll_seconds = None
    pipeline.config.execution.max_entry_slippage_bps = -3

    poll, bps = _repeg_settings(pipeline)

    assert poll == 5.0 and bps == 40.0


def test_the_deleted_attempt_cap_key_is_rejected_loudly():
    """`repeg_max_attempts` is gone. A settings file still carrying it must
    fail at load, not silently load a ladder length nothing reads."""
    with pytest.raises(ValueError, match="repeg_max_attempts"):
        ExecutionConfig(repeg_enabled=True, repeg_max_attempts=3)


# --------------------------------------------------------------------------
# the happy path
# --------------------------------------------------------------------------


def test_clean_repeg_to_fill(db):
    """Limit below the ceiling, market away, ONE replacement, then a fill."""
    pipeline = _pipeline(db)
    spec = _spec(db, limit_price=100.10)
    pipeline.broker.replace_entry_limit = _replace_returns("ord-2")
    # rest → still working; after the replacement → filled.
    pipeline.broker.wait_for_order_terminal.side_effect = ["accepted", "filled"]

    order_id, carried = _repeg_entry_order(pipeline, _ctx(), spec)

    assert order_id == "ord-2"
    assert carried == 0.0
    pipeline.broker.replace_entry_limit.assert_called_once()
    args, kwargs = pipeline.broker.replace_entry_limit.call_args
    assert args[0] == "ord-1"
    # Straight to the ceiling — the decisive price, crossing the 100.30 ask.
    assert args[1] == pytest.approx(CEILING_40BPS)
    assert kwargs["qty"] == 10  # explicit, not defaulted


def test_new_order_id_is_written_to_the_trades_row(db):
    """If the trades row keeps the dead id, reconciliation follows a corpse."""
    pipeline = _pipeline(db)
    spec = _spec(db)
    pipeline.broker.replace_entry_limit = _replace_returns("ord-2")
    pipeline.broker.wait_for_order_terminal.side_effect = ["accepted", "filled"]

    _repeg_entry_order(pipeline, _ctx(), spec)

    assert _tracked_order_id(db, spec["trade_row_id"]) == "ord-2"


def test_wal_row_is_cleared_after_a_successful_repeg(db):
    pipeline = _pipeline(db)
    spec = _spec(db)
    pipeline.broker.replace_entry_limit = _replace_returns("ord-2")
    pipeline.broker.wait_for_order_terminal.side_effect = ["accepted", "filled"]

    _repeg_entry_order(pipeline, _ctx(), spec)

    assert db.get_pending_repegs() == []


def test_order_that_fills_before_the_first_attempt_is_never_replaced(db):
    pipeline = _pipeline(db)
    spec = _spec(db)
    pipeline.broker.wait_for_order_terminal.return_value = "filled"

    order_id, carried = _repeg_entry_order(pipeline, _ctx(), spec)

    assert (order_id, carried) == ("ord-1", 0.0)
    pipeline.broker.replace_entry_limit.assert_not_called()


# --------------------------------------------------------------------------
# ONE reprice — not a ladder (2026-09-12 rebuild)
# --------------------------------------------------------------------------


def test_a_stalling_order_gets_exactly_one_reprice(db):
    """A market that keeps running must produce ONE replace, not a chain —
    Alpaca's community practice is a single decisive replace, and every
    extra one is another `pending_replace` window to get stuck in."""
    pipeline = _pipeline(db)
    spec = _spec(db, limit_price=100.00)
    pipeline.broker.replace_entry_limit = _replace_returns("ord-2", "ord-3", "ord-4")
    pipeline.broker.get_latest_quote.side_effect = [
        {"ask_price": 100.10},
        {"ask_price": 100.20},
        {"ask_price": 100.30},
    ]
    # Still working after the reprice — a ladder would go round again.
    pipeline.broker.wait_for_order_terminal.return_value = "new"

    order_id, _ = _repeg_entry_order(pipeline, _ctx(), spec)

    assert pipeline.broker.replace_entry_limit.call_count == 1
    assert order_id == "ord-2"
    assert _tracked_order_id(db, spec["trade_row_id"]) == "ord-2"
    assert spec["attempted_prices"] == [pytest.approx(CEILING_40BPS)]
    assert spec["repeg_outcome"] == "replaced"
    # No second replace means nothing to sequence: the old confirm gate is
    # not on the path at all.
    pipeline.broker.await_replacement_confirmed.assert_not_called()


def test_the_one_reprice_crosses_the_market(db):
    """Priced THROUGH the ask, not a small increment above the old limit."""
    pipeline = _pipeline(db)
    spec = _spec(db, limit_price=100.00)
    pipeline.broker.get_latest_quote.return_value = {"ask_price": 100.25}
    pipeline.broker.replace_entry_limit = _replace_returns("ord-2")

    _repeg_entry_order(pipeline, _ctx(), spec)

    price = pipeline.broker.replace_entry_limit.call_args[0][1]
    assert price >= 100.25, "did not cross the ask"
    assert price > 100.00 + 0.01, "a one-cent nudge is the ladder we retired"
    assert price <= CEILING_40BPS + 1e-9


# --------------------------------------------------------------------------
# bound 2: the slippage ceiling
# --------------------------------------------------------------------------


def test_ceiling_clamps_the_repeg_price(db):
    """The ask is beyond the ceiling; the one reprice goes to the ceiling and
    NOT past it — the ceiling is absolute, never exceeded to force a fill."""
    pipeline = _pipeline(db, _exec_cfg())
    spec = _spec(db, limit_price=100.10)
    pipeline.broker.get_latest_quote.return_value = {"ask_price": 105.00}
    pipeline.broker.replace_entry_limit = _replace_returns("ord-2")

    order_id, _ = _repeg_entry_order(pipeline, _ctx(), spec)

    # One replacement, priced AT the ceiling — and never a second.
    assert pipeline.broker.replace_entry_limit.call_count == 1
    assert pipeline.broker.replace_entry_limit.call_args[0][1] == pytest.approx(CEILING_40BPS)
    assert order_id == "ord-2"


@pytest.mark.parametrize("ask", [100.05, 100.20, 100.39, 100.40, 100.41, 180.00])
def test_never_prices_above_the_ceiling_whatever_the_ask(db, ask):
    pipeline = _pipeline(db)
    spec = _spec(db, limit_price=100.00)
    pipeline.broker.get_latest_quote.return_value = {"ask_price": ask}
    pipeline.broker.replace_entry_limit = _replace_returns("ord-2")

    _repeg_entry_order(pipeline, _ctx(), spec)

    prices = [c[0][1] for c in pipeline.broker.replace_entry_limit.call_args_list]
    assert len(prices) == 1
    assert prices[0] <= CEILING_40BPS + 1e-9


def test_an_order_already_at_the_ceiling_is_left_alone(db):
    """The post-PR-#111 normal case: the submitted limit IS the ceiling."""
    pipeline = _pipeline(db)
    spec = _spec(db, limit_price=CEILING_40BPS)

    order_id, carried = _repeg_entry_order(pipeline, _ctx(), spec)

    assert (order_id, carried) == ("ord-1", 0.0)
    pipeline.broker.replace_entry_limit.assert_not_called()
    pipeline.broker.wait_for_order_terminal.assert_not_called()


def test_market_that_came_back_to_us_is_not_chased(db):
    pipeline = _pipeline(db)
    spec = _spec(db, limit_price=100.30)
    pipeline.broker.get_latest_quote.return_value = {"ask_price": 100.05}

    order_id, _ = _repeg_entry_order(pipeline, _ctx(), spec)

    assert order_id == "ord-1"
    pipeline.broker.replace_entry_limit.assert_not_called()
    assert spec["repeg_outcome"] == "market_within_limit"


def test_market_order_has_no_limit_to_walk(db):
    pipeline = _pipeline(db)
    spec = _spec(db)
    spec["limit_price"] = None

    assert _repeg_entry_order(pipeline, _ctx(), spec) == ("ord-1", 0.0)
    pipeline.broker.replace_entry_limit.assert_not_called()


def test_missing_reference_price_disables_the_chase(db):
    """No reference ⇒ no ceiling ⇒ no bound ⇒ no re-peg."""
    pipeline = _pipeline(db)
    spec = _spec(db)
    spec["reference_price"] = None

    assert _repeg_entry_order(pipeline, _ctx(), spec) == ("ord-1", 0.0)
    pipeline.broker.replace_entry_limit.assert_not_called()


# --------------------------------------------------------------------------
# partial fills — the over-buy footgun
# --------------------------------------------------------------------------


def test_partially_filled_order_is_never_replaced(db):
    """Replacing a partly-executed order is how one idea gets bought twice."""
    pipeline = _pipeline(db)
    spec = _spec(db, qty=10)
    pipeline.broker.get_order_fill_info.return_value = {
        "status": "partially_filled",
        "filled_qty": 4.0,
        "filled_avg_price": 100.05,
    }

    order_id, carried = _repeg_entry_order(pipeline, _ctx(), spec)

    assert order_id == "ord-1"  # the working remainder, untouched
    assert carried == 0.0  # nothing superseded — same order id
    pipeline.broker.replace_entry_limit.assert_not_called()
    assert db.get_pending_repegs() == []


def test_fill_landing_inside_the_replace_window_cancels_the_replacement(db):
    """THE RACE: zero fill when we looked, a fill by the time the PATCH
    applied. The ancestor's shares are real and invisible to the new order,
    so the replacement is killed and the fill is carried into the stop."""
    pipeline = _pipeline(db, _exec_cfg())
    spec = _spec(db, limit_price=100.00, qty=10)
    pipeline.broker.replace_entry_limit = _replace_returns("ord-2")
    pipeline.broker.get_order_fill_info.side_effect = [
        {"status": "new", "filled_qty": 0.0},  # pre-replace
        {"status": "partially_filled", "filled_qty": 6.0},  # ancestor, after
    ]

    order_id, carried = _repeg_entry_order(pipeline, _ctx(), spec)

    assert order_id == "ord-2"
    assert carried == 6.0
    pipeline.broker.cancel_entry_order.assert_called_once_with("ord-2")
    # Chase stops dead — no second attempt.
    assert pipeline.broker.replace_entry_limit.call_count == 1


def test_raced_fill_still_repoints_the_trades_row(db):
    pipeline = _pipeline(db)
    spec = _spec(db, limit_price=100.00)
    pipeline.broker.replace_entry_limit = _replace_returns("ord-2")
    pipeline.broker.get_order_fill_info.side_effect = [
        {"status": "new", "filled_qty": 0.0},
        {"status": "partially_filled", "filled_qty": 6.0},
    ]

    _repeg_entry_order(pipeline, _ctx(), spec)

    assert _tracked_order_id(db, spec["trade_row_id"]) == "ord-2"
    assert db.get_pending_repegs() == []


# --------------------------------------------------------------------------
# a rejected replacement
# --------------------------------------------------------------------------


def test_replacement_rejected_because_the_order_already_filled(db):
    """The broker refuses; the ORIGINAL id stays authoritative and we stop."""
    pipeline = _pipeline(db, _exec_cfg())
    spec = _spec(db, limit_price=100.00)
    pipeline.broker.replace_entry_limit.return_value = {
        "id": None,
        "status": "replace_rejected",
        "detail": "order is not cancelable",
    }
    pipeline.broker.resolve_replacement_chain.return_value = "ord-1"

    order_id, carried = _repeg_entry_order(pipeline, _ctx(), spec)

    assert (order_id, carried) == ("ord-1", 0.0)
    assert _tracked_order_id(db, spec["trade_row_id"]) == "ord-1"
    assert db.get_pending_repegs() == []
    assert pipeline.broker.replace_entry_limit.call_count == 1


def test_lost_response_whose_patch_actually_landed_adopts_the_real_id(db):
    """A timeout is not a rejection. The broker is asked, not assumed."""
    pipeline = _pipeline(db)
    spec = _spec(db, limit_price=100.00)
    pipeline.broker.replace_entry_limit.return_value = {
        "id": None,
        "status": "replace_rejected",
        "detail": "read timeout",
    }
    pipeline.broker.resolve_replacement_chain.return_value = "ord-99"

    order_id, _ = _repeg_entry_order(pipeline, _ctx(), spec)

    assert order_id == "ord-99"
    assert _tracked_order_id(db, spec["trade_row_id"]) == "ord-99"


def test_unreadable_broker_after_a_failed_replace_leaves_the_wal_standing(db):
    """Unknown outcome ⇒ hand it to the session-start drain, chase no more."""
    pipeline = _pipeline(db)
    spec = _spec(db, limit_price=100.00)
    pipeline.broker.replace_entry_limit.return_value = {"id": None, "status": "replace_rejected"}
    pipeline.broker.resolve_replacement_chain.return_value = None

    order_id, _ = _repeg_entry_order(pipeline, _ctx(), spec)

    assert order_id == "ord-1"
    rows = db.get_pending_repegs()
    assert len(rows) == 1
    assert rows[0]["new_order_id"] == _WAL_REPEG_SENTINEL


def test_a_wal_write_failure_forbids_the_replacement(db):
    """No durable intent ⇒ no crash-safe window ⇒ do not open one."""
    pipeline = _pipeline(db)
    spec = _spec(db, limit_price=100.00)
    failing = MagicMock(wraps=db)
    failing.insert_pending_repeg.side_effect = RuntimeError("disk full")
    pipeline.db = failing

    order_id, _ = _repeg_entry_order(pipeline, _ctx(), spec)

    assert order_id == "ord-1"
    pipeline.broker.replace_entry_limit.assert_not_called()


# --------------------------------------------------------------------------
# broker read failures never escalate
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "attr",
    [
        "wait_for_order_terminal",
        "wait_for_order_at_exchange",
        "get_order_fill_info",
        "get_latest_quote",
    ],
)
def test_a_raising_broker_read_leaves_the_order_working(db, attr):
    pipeline = _pipeline(db)
    spec = _spec(db, limit_price=100.00)
    # Make the exchange-ack wait reachable: the rest window ends pre-venue.
    pipeline.broker.wait_for_order_terminal.return_value = "accepted"
    getattr(pipeline.broker, attr).side_effect = RuntimeError("alpaca 500")

    assert _repeg_entry_order(pipeline, _ctx(), spec) == ("ord-1", 0.0)
    pipeline.broker.replace_entry_limit.assert_not_called()


def test_a_missing_quote_stops_the_chase(db):
    pipeline = _pipeline(db)
    spec = _spec(db, limit_price=100.00)
    pipeline.broker.get_latest_quote.return_value = {"ask_price": None}

    assert _repeg_entry_order(pipeline, _ctx(), spec) == ("ord-1", 0.0)
    pipeline.broker.replace_entry_limit.assert_not_called()


# --------------------------------------------------------------------------
# crash between replace and record — the drain
# --------------------------------------------------------------------------


def _pipeline_with_drain(db):
    """A real `_drain_pending_repegs` bound to a fake pipeline."""
    from src.pipeline import TradingPipeline

    p = MagicMock()
    p.db = db
    p._drain_pending_repegs = TradingPipeline._drain_pending_repegs.__get__(p, MagicMock)
    p._delete_repeg_row = TradingPipeline._delete_repeg_row.__get__(p, MagicMock)
    return p


def test_crash_between_replace_and_record_is_recovered(db):
    """SIGKILL after the PATCH landed: the WAL row is all that survives."""
    row_id = db.insert_trade(
        symbol="NVDA",
        action="BUY",
        qty=10,
        price=100.10,
        reasoning="crash",
        run_id="run-x",
        broker_order_id="ord-1",
        fill_status="submitted",
        stop_loss=90.0,
    )
    db.insert_pending_repeg(
        trade_row_id=row_id,
        symbol="NVDA",
        old_order_id="ord-1",
        new_order_id=_WAL_REPEG_SENTINEL,
        run_id="run-x",
    )
    p = _pipeline_with_drain(db)
    p.broker.resolve_replacement_chain.return_value = "ord-2"

    assert p._drain_pending_repegs() == 1
    assert _tracked_order_id(db, row_id) == "ord-2"
    assert db.get_pending_repegs() == []


def test_crash_where_the_patch_never_landed_clears_cleanly(db):
    row_id = db.insert_trade(
        symbol="NVDA",
        action="BUY",
        qty=10,
        price=100.10,
        reasoning="crash",
        run_id="run-x",
        broker_order_id="ord-1",
        fill_status="submitted",
        stop_loss=90.0,
    )
    db.insert_pending_repeg(
        trade_row_id=row_id,
        symbol="NVDA",
        old_order_id="ord-1",
        new_order_id=_WAL_REPEG_SENTINEL,
    )
    p = _pipeline_with_drain(db)
    p.broker.resolve_replacement_chain.return_value = "ord-1"  # never replaced

    assert p._drain_pending_repegs() == 1
    assert _tracked_order_id(db, row_id) == "ord-1"
    assert db.get_pending_repegs() == []


def test_drain_leaves_the_row_when_the_broker_cannot_be_read(db):
    row_id = db.insert_trade(
        symbol="NVDA",
        action="BUY",
        qty=10,
        price=100.10,
        reasoning="crash",
        run_id="run-x",
        broker_order_id="ord-1",
        fill_status="submitted",
        stop_loss=90.0,
    )
    db.insert_pending_repeg(
        trade_row_id=row_id,
        symbol="NVDA",
        old_order_id="ord-1",
        new_order_id=_WAL_REPEG_SENTINEL,
    )
    p = _pipeline_with_drain(db)
    p.broker.resolve_replacement_chain.return_value = None

    assert p._drain_pending_repegs() == 0
    assert len(db.get_pending_repegs()) == 1
    assert _tracked_order_id(db, row_id) == "ord-1"


def test_drain_recovers_a_crash_after_the_id_was_known(db):
    """Crash between `resolve_pending_repeg` and the trades-row repoint."""
    row_id = db.insert_trade(
        symbol="NVDA",
        action="BUY",
        qty=10,
        price=100.10,
        reasoning="crash",
        run_id="run-x",
        broker_order_id="ord-1",
        fill_status="submitted",
        stop_loss=90.0,
    )
    db.insert_pending_repeg(
        trade_row_id=row_id,
        symbol="NVDA",
        old_order_id="ord-1",
        new_order_id="ord-2",
    )
    p = _pipeline_with_drain(db)

    assert p._drain_pending_repegs() == 1
    assert _tracked_order_id(db, row_id) == "ord-2"
    p.broker.resolve_replacement_chain.assert_not_called()


def test_drain_is_idempotent(db):
    row_id = db.insert_trade(
        symbol="NVDA",
        action="BUY",
        qty=10,
        price=100.10,
        reasoning="crash",
        run_id="run-x",
        broker_order_id="ord-1",
        fill_status="submitted",
        stop_loss=90.0,
    )
    db.insert_pending_repeg(
        trade_row_id=row_id,
        symbol="NVDA",
        old_order_id="ord-1",
        new_order_id=_WAL_REPEG_SENTINEL,
    )
    p = _pipeline_with_drain(db)
    p.broker.resolve_replacement_chain.return_value = "ord-2"

    p._drain_pending_repegs()
    p._drain_pending_repegs()

    assert _tracked_order_id(db, row_id) == "ord-2"
    assert db.get_pending_repegs() == []


def test_drain_does_not_clobber_a_newer_repeg(db):
    """A stale WAL row replayed after the chain moved on must be inert."""
    row_id = db.insert_trade(
        symbol="NVDA",
        action="BUY",
        qty=10,
        price=100.10,
        reasoning="stale",
        run_id="run-x",
        broker_order_id="ord-3",
        fill_status="submitted",
        stop_loss=90.0,
    )
    db.insert_pending_repeg(
        trade_row_id=row_id,
        symbol="NVDA",
        old_order_id="ord-1",
        new_order_id="ord-2",
    )
    p = _pipeline_with_drain(db)

    p._drain_pending_repegs()

    assert _tracked_order_id(db, row_id) == "ord-3"


def test_drain_is_a_noop_with_an_empty_queue(db):
    p = _pipeline_with_drain(db)
    assert p._drain_pending_repegs() == 0
    p.broker.resolve_replacement_chain.assert_not_called()


# --------------------------------------------------------------------------
# db layer
# --------------------------------------------------------------------------


def test_repoint_is_guarded_on_the_old_id(db):
    row_id = db.insert_trade(
        symbol="NVDA",
        action="BUY",
        qty=1,
        price=1.0,
        reasoning="r",
        run_id="run-x",
        broker_order_id="ord-1",
        stop_loss=90.0,
    )
    assert db.repoint_trade_broker_order_id(row_id, old_order_id="ord-1", new_order_id="ord-2") == 1
    # Replay: the row no longer holds ord-1, so nothing is touched.
    assert db.repoint_trade_broker_order_id(row_id, old_order_id="ord-1", new_order_id="ord-9") == 0
    assert _tracked_order_id(db, row_id) == "ord-2"


def test_pending_repeg_crud_roundtrip(db):
    rid = db.insert_pending_repeg(
        trade_row_id=7,
        symbol="NVDA",
        old_order_id="a",
        new_order_id=_WAL_REPEG_SENTINEL,
        run_id="run-x",
    )
    rows = db.get_pending_repegs()
    assert len(rows) == 1 and rows[0]["trade_row_id"] == 7
    assert db.resolve_pending_repeg(rid, "b") == 1
    assert db.get_pending_repegs()[0]["new_order_id"] == "b"
    assert db.delete_pending_repeg(rid) == 1
    assert db.get_pending_repegs() == []


def test_prune_pending_repegs_drops_only_stale_rows(db):
    fresh = db.insert_pending_repeg(trade_row_id=1, symbol="NVDA", old_order_id="a", new_order_id="b")
    stale = db.insert_pending_repeg(trade_row_id=2, symbol="AMD", old_order_id="c", new_order_id="d")
    db.execute(
        "UPDATE pending_repegs SET created_at = datetime('now', '-40 days') WHERE id = ?",
        (stale,),
    )
    db.conn.commit()

    assert db.prune_pending_repegs(keep_days=30) == 1
    assert [r["id"] for r in db.get_pending_repegs()] == [fresh]


def test_prune_pending_repegs_refuses_to_wipe_the_queue(db):
    with pytest.raises(ValueError):
        db.prune_pending_repegs(keep_days=0)


def test_pending_repegs_table_and_index_exist(db):
    names = {r[0] for r in db.execute("SELECT name FROM sqlite_master").fetchall()}
    assert "pending_repegs" in names
    assert "idx_pending_repegs_created_at" in names


# --------------------------------------------------------------------------
# broker primitives
# --------------------------------------------------------------------------


def _broker():
    from src.execution.broker import AlpacaBroker

    b = AlpacaBroker.__new__(AlpacaBroker)
    b.client = MagicMock()
    return b


def test_replace_entry_limit_returns_the_new_id_and_passes_qty():
    b = _broker()
    b.client.replace_order_by_id.return_value = MagicMock(id="ord-2", status="accepted")

    out = b.replace_entry_limit("ord-1", 100.404, qty=10)

    assert out["id"] == "ord-2" and out["replaces"] == "ord-1"
    assert out["limit_price"] == 100.40  # quantized to the tick
    req = b.client.replace_order_by_id.call_args[0][1]
    assert req.qty == 10 and req.limit_price == 100.40


def test_replace_entry_limit_reports_a_broker_refusal_without_raising():
    b = _broker()
    b.client.replace_order_by_id.side_effect = Exception("order is not cancelable")

    out = b.replace_entry_limit("ord-1", 100.40, qty=10)

    assert out == {"id": None, "status": "replace_rejected", "detail": "order is not cancelable"}


def test_replace_entry_limit_never_sends_an_unquotable_price():
    b = _broker()
    assert b.replace_entry_limit("ord-1", float("nan"))["id"] is None
    assert b.replace_entry_limit("ord-1", 0.0)["id"] is None
    b.client.replace_order_by_id.assert_not_called()


def test_replace_entry_limit_treats_a_missing_id_as_a_rejection():
    b = _broker()
    b.client.replace_order_by_id.return_value = MagicMock(id="", status="accepted")
    assert b.replace_entry_limit("ord-1", 100.40)["id"] is None


def test_resolve_replacement_chain_walks_to_the_live_order():
    b = _broker()
    orders = {
        "a": MagicMock(status="replaced", replaced_by="b"),
        "b": MagicMock(status="replaced", replaced_by="c"),
        "c": MagicMock(status="accepted", replaced_by=None),
    }
    b.client.get_order_by_id.side_effect = lambda oid: orders[oid]

    assert b.resolve_replacement_chain("a") == "c"


def test_resolve_replacement_chain_returns_the_input_when_never_replaced():
    b = _broker()
    b.client.get_order_by_id.return_value = MagicMock(status="accepted", replaced_by=None)
    assert b.resolve_replacement_chain("a") == "a"


def test_resolve_replacement_chain_returns_none_on_a_failed_read():
    """None means UNKNOWN. It must never be confused with 'never replaced'."""
    b = _broker()
    b.client.get_order_by_id.side_effect = Exception("503")
    assert b.resolve_replacement_chain("a") is None


def test_resolve_replacement_chain_refuses_to_walk_forever():
    b = _broker()
    b.client.get_order_by_id.return_value = MagicMock(status="replaced", replaced_by="loop-next")
    # Every hop points somewhere new-looking; the hop cap must stop it.
    b.client.get_order_by_id.side_effect = lambda oid: MagicMock(status="replaced", replaced_by=oid + "x")
    assert b.resolve_replacement_chain("a") is None


def test_cancel_entry_order_reports_failure_instead_of_raising():
    b = _broker()
    b.client.cancel_order_by_id.side_effect = Exception("nope")
    assert b.cancel_entry_order("ord-2") is False


# --------------------------------------------------------------------------
# protection must cover every filled share in the chain
# --------------------------------------------------------------------------


def _protection_broker(filled_qty):
    b = _broker()
    b.wait_for_order_terminal = MagicMock(return_value="filled")
    b.get_order_fill_info = MagicMock(
        return_value={
            "status": "filled",
            "filled_qty": filled_qty,
            "filled_avg_price": 100.0,
        }
    )
    b._submit_stop_limit_order = MagicMock(return_value={"id": "stop-1"})
    return b


def test_stop_covers_shares_filled_under_a_superseded_order_id():
    """The ancestor's fill is invisible to the new order. It is still ours."""
    b = _protection_broker(filled_qty=4.0)

    b.place_entry_protection("NVDA", "ord-2", 95.0, requested_qty=10, superseded_filled_qty=6.0)

    assert b._submit_stop_limit_order.call_args.kwargs["qty"] == 10.0


def test_protection_is_placed_when_only_the_superseded_order_filled():
    """The raced-partial case: the replacement filled nothing, the ancestor
    filled everything. Without the carry this position goes naked."""
    b = _protection_broker(filled_qty=0.0)

    out = b.place_entry_protection("NVDA", "ord-2", 95.0, requested_qty=10, superseded_filled_qty=6.0)

    assert out == {"id": "stop-1"}
    assert b._submit_stop_limit_order.call_args.kwargs["qty"] == 6.0


def test_protection_default_is_unchanged_for_non_repeg_callers():
    b = _protection_broker(filled_qty=7.0)
    b.place_entry_protection("NVDA", "ord-1", 95.0, requested_qty=10)
    assert b._submit_stop_limit_order.call_args.kwargs["qty"] == 7.0


def test_zero_everywhere_places_no_stop():
    b = _protection_broker(filled_qty=0.0)
    assert b.place_entry_protection("NVDA", "ord-1", 95.0, requested_qty=10, superseded_filled_qty=0.0) is None
    b._submit_stop_limit_order.assert_not_called()


# --------------------------------------------------------------------------
# end to end through ExecutionStage
# --------------------------------------------------------------------------


def _rc():
    return ReasoningChain(
        macro_filter="m",
        news_check="n",
        earnings_check="e",
        signal_conflicts="s",
        sizing_logic="z",
        portfolio_balance="b",
        cash_target="c",
    )


def _stage_pipeline(db, cfg):
    pipeline = MagicMock()
    pipeline.broker.list_open_orders_checked.return_value = (True, [])  # listing read, nothing open
    pipeline.db = db
    pipeline.config.execution = cfg
    pipeline.broker.get_latest_price.return_value = 100.0
    pipeline.broker.get_latest_quote.return_value = {"ask_price": 100.02}
    pipeline.broker.submit_order.return_value = {
        "id": "ord-1",
        "status": "accepted",
        "pending_stop_price": 95.0,
    }
    pipeline._format_qty = lambda q: str(q)
    pipeline._order_accepted.return_value = True
    pipeline._refresh_account_state.return_value = (
        {"cash": 50_000.0, "portfolio_value": 100_000.0},
        [],
        {},
    )
    from src.execution.broker import AlpacaBroker

    pipeline.broker._TERMINAL_ORDER_STATES = AlpacaBroker._TERMINAL_ORDER_STATES
    pipeline.broker._ORDER_REPLACEABLE_STATES = AlpacaBroker._ORDER_REPLACEABLE_STATES
    pipeline.broker._ORDER_PRE_EXCHANGE_STATES = AlpacaBroker._ORDER_PRE_EXCHANGE_STATES
    pipeline.broker.wait_for_order_at_exchange.return_value = "new"
    return pipeline


def _stage_ctx():
    ctx = RunContext.start("morning")
    ctx.cash = 50_000.0
    ctx.total_value = 100_000.0
    ctx.last_equity = 100_000.0
    ctx.positions = []
    ctx.decision_id = "run-x-dec-e2e"
    ctx.portfolio_decision = PortfolioDecision(
        reasoning_chain=_rc(),
        portfolio_view="t",
        decisions=[
            TradeDecision(
                action="BUY",
                symbol="NVDA",
                allocation_pct=10,
                entry_price=100.0,
                stop_loss=95.0,
                take_profit=115.0,
                reasoning="e2e",
            )
        ],
    )
    ctx.symbols_bars = {}
    return ctx


def test_execution_stage_protects_the_repegged_order_id(db):
    """The stage must hand the FINAL id — not the dead one — to protection."""
    pipeline = _stage_pipeline(db, _exec_cfg())
    # Force room to chase: submitted limit below the ceiling.
    pipeline.broker.get_latest_quote.side_effect = [
        {"ask_price": None},  # submission: no usable ask
        {"ask_price": 100.30},  # re-peg: market has moved
    ]
    pipeline.broker.wait_for_order_terminal.return_value = "accepted"
    pipeline.broker.get_order_fill_info.return_value = {
        "status": "accepted",
        "filled_qty": 0.0,
    }
    pipeline.broker.replace_entry_limit.return_value = {
        "id": "ord-2",
        "status": "accepted",
    }

    ExecutionStage(pipeline=pipeline).run(_stage_ctx())

    kwargs = pipeline.broker.place_entry_protection.call_args.kwargs
    assert kwargs["order_id"] == "ord-2"
    assert kwargs["superseded_filled_qty"] == 0.0


def test_execution_stage_is_untouched_when_the_flag_is_off(db):
    pipeline = _stage_pipeline(db, _exec_cfg(repeg_enabled=False))

    ExecutionStage(pipeline=pipeline).run(_stage_ctx())

    pipeline.broker.replace_entry_limit.assert_not_called()
    kwargs = pipeline.broker.place_entry_protection.call_args.kwargs
    assert kwargs["order_id"] == "ord-1"
    assert kwargs["superseded_filled_qty"] == 0.0


def test_a_repeg_that_explodes_still_protects_the_original_order(db):
    """Protection is not optional. A broken chase must not skip the stop."""
    pipeline = _stage_pipeline(db, _exec_cfg())
    pipeline.broker.get_latest_quote.side_effect = [
        {"ask_price": None},
        {"ask_price": 100.30},
    ]
    pipeline.broker.wait_for_order_terminal.side_effect = RuntimeError("boom")
    pipeline.broker.replace_entry_limit.side_effect = AssertionError("must not replace after a failed read")

    ExecutionStage(pipeline=pipeline).run(_stage_ctx())

    kwargs = pipeline.broker.place_entry_protection.call_args.kwargs
    assert kwargs["order_id"] == "ord-1"


# ==========================================================================
# 2026-09-11 — the stalling entry: reprice, sequence, bound, then TELL HIM
#
# docs/WORK.md funnel item 3: 6 of 68 historical proposals were limit orders
# that never filled because price drifted away, and were correctly cancelled
# by price protection. Working as designed, and a lost opportunity every
# time — the owner's requirement is that such an order never simply dies
# with no trace. These tests cover the three things that changed: the
# one-replace-at-a-time gate Alpaca actually requires, the wall-clock bound,
# and the exhaustion alert.
# ==========================================================================


def _alerting_pipeline(db, cfg=None):
    """A pipeline whose replacement confirmations succeed, as a live broker's
    would. `await_replacement_confirmed` is set EXPLICITLY rather than left
    as an auto-MagicMock so a reader can see which way it is answering."""
    pipeline = _pipeline(db, cfg)
    pipeline.broker.await_replacement_confirmed.return_value = True
    return pipeline


@pytest.fixture
def alerts(monkeypatch):
    """Capture owner alerts instead of sending them."""
    sent: list[str] = []

    def _capture(text, **kwargs):
        sent.append(text)
        return True

    import src.notifier as notifier

    monkeypatch.setattr(notifier, "send_owner_alert", _capture)
    return sent


# --------------------------------------------------------------------------
# the reprice happens AFTER the wait, never before
# --------------------------------------------------------------------------


def test_the_order_is_left_to_work_before_any_reprice(db, alerts):
    """The cheapest re-peg is the one never sent: rest first, replace after."""
    pipeline = _alerting_pipeline(db, _exec_cfg())
    spec = _spec(db, limit_price=100.00)
    pipeline.broker.replace_entry_limit = _replace_returns("ord-2")

    _repeg_entry_order(pipeline, _ctx(), spec)

    names = [c[0] for c in pipeline.broker.mock_calls]
    assert "wait_for_order_terminal" in names
    assert "replace_entry_limit" in names
    assert names.index("wait_for_order_terminal") < names.index("replace_entry_limit"), (
        "replaced the order before letting it work"
    )


def test_a_fast_filling_order_triggers_none_of_this(db, alerts):
    """The overwhelmingly common case: it fills in the wait. No replacement,
    no quote call, no alert."""
    pipeline = _alerting_pipeline(db)
    spec = _spec(db, limit_price=100.00)
    pipeline.broker.wait_for_order_terminal.return_value = "filled"

    order_id, carried = _repeg_entry_order(pipeline, _ctx(), spec)

    assert (order_id, carried) == ("ord-1", 0.0)
    pipeline.broker.replace_entry_limit.assert_not_called()
    pipeline.broker.await_replacement_confirmed.assert_not_called()
    assert alerts == []


# --------------------------------------------------------------------------
# (b) the exchange must have the order before a replace is attempted
# --------------------------------------------------------------------------


@pytest.mark.parametrize("stuck_status", ["accepted", "pending_new"])
def test_an_order_the_exchange_has_not_acknowledged_is_not_repriced(
    db,
    alerts,
    stuck_status,
):
    """Alpaca rejects a replace on an order it has accepted but the venue
    has not ("order isn't sent to exchange yet"). Sending one anyway is the
    open-market defect this rebuild removes: no replace, reason recorded."""
    pipeline = _alerting_pipeline(db)
    spec = _spec(db, limit_price=100.00)
    pipeline.broker.wait_for_order_terminal.return_value = stuck_status
    pipeline.broker.wait_for_order_at_exchange.return_value = stuck_status
    pipeline.broker.replace_entry_limit = _replace_returns("ord-2")

    order_id, carried = _repeg_entry_order(pipeline, _ctx(), spec)

    assert (order_id, carried) == ("ord-1", 0.0)
    pipeline.broker.replace_entry_limit.assert_not_called()
    assert db.get_pending_repegs() == []  # no WAL row was even opened
    assert spec["repeg_outcome"] == "not_at_exchange"
    assert spec["attempted_prices"] == []
    # The honest reason is on the evidence stream, not just in a log line.
    rows = db.execute("SELECT evidence_json FROM specialist_evidence WHERE symbol = 'NVDA'").fetchall()
    assert any("repeg_not_at_exchange" in r[0] and stuck_status in r[0] for r in rows)
    # The reprice function itself never pages — the end-of-session cancel
    # alert (with this reason in it) is the single message the owner gets.
    assert alerts == []


def test_an_unknown_status_is_treated_as_not_at_exchange(db, alerts):
    """No positive evidence the venue has it ⇒ no replace. Silence and a
    None read are not 'safe to send'."""
    pipeline = _alerting_pipeline(db)
    spec = _spec(db, limit_price=100.00)
    pipeline.broker.wait_for_order_terminal.return_value = None
    pipeline.broker.wait_for_order_at_exchange.return_value = None

    _repeg_entry_order(pipeline, _ctx(), spec)

    pipeline.broker.replace_entry_limit.assert_not_called()
    assert spec["repeg_outcome"] == "not_at_exchange"


def test_an_order_acknowledged_late_but_inside_the_window_is_repriced(db, alerts):
    """The rest window ends with the order still `accepted`; the exchange
    then acknowledges it inside the ack window. It gets its single reprice."""
    pipeline = _alerting_pipeline(db)
    spec = _spec(db, limit_price=100.00)
    pipeline.broker.wait_for_order_terminal.return_value = "accepted"
    pipeline.broker.wait_for_order_at_exchange.return_value = "new"
    pipeline.broker.replace_entry_limit = _replace_returns("ord-2")

    order_id, _ = _repeg_entry_order(pipeline, _ctx(), spec)

    assert order_id == "ord-2"
    assert pipeline.broker.replace_entry_limit.call_count == 1
    pipeline.broker.wait_for_order_at_exchange.assert_called_once()
    names = [c[0] for c in pipeline.broker.mock_calls]
    assert names.index("wait_for_order_at_exchange") < names.index("replace_entry_limit")


def test_the_ack_window_is_not_paid_when_the_venue_already_has_it(db, alerts):
    """`new` at the end of the rest window ⇒ straight to the reprice; the
    second window exists only for the pre-venue case."""
    pipeline = _alerting_pipeline(db)
    spec = _spec(db, limit_price=100.00)
    pipeline.broker.wait_for_order_terminal.return_value = "new"
    pipeline.broker.replace_entry_limit = _replace_returns("ord-2")

    _repeg_entry_order(pipeline, _ctx(), spec)

    pipeline.broker.wait_for_order_at_exchange.assert_not_called()
    assert pipeline.broker.replace_entry_limit.call_count == 1


def test_a_fill_that_lands_during_the_ack_wait_is_not_repriced(db, alerts):
    pipeline = _alerting_pipeline(db)
    spec = _spec(db, limit_price=100.00)
    pipeline.broker.wait_for_order_terminal.return_value = "accepted"
    pipeline.broker.wait_for_order_at_exchange.return_value = "filled"

    order_id, _ = _repeg_entry_order(pipeline, _ctx(), spec)

    assert order_id == "ord-1"
    pipeline.broker.replace_entry_limit.assert_not_called()
    assert alerts == []


def test_the_ack_wait_is_bounded_by_the_configured_window(db, alerts):
    pipeline = _alerting_pipeline(db, _exec_cfg(repeg_poll_seconds=2.5))
    spec = _spec(db, limit_price=100.00)
    pipeline.broker.wait_for_order_terminal.return_value = "accepted"
    pipeline.broker.wait_for_order_at_exchange.return_value = "accepted"

    _repeg_entry_order(pipeline, _ctx(), spec)

    kwargs = pipeline.broker.wait_for_order_at_exchange.call_args.kwargs
    assert kwargs["timeout_seconds"] == pytest.approx(2.5)


# --------------------------------------------------------------------------
# the reprice function never pages by itself any more
# --------------------------------------------------------------------------


def test_a_replaced_but_still_unfilled_order_does_not_page_here(db, alerts):
    """ "Repricing exhausted" and "cancelled at end of session" are the same
    event now, and the cancel path owns the one alert. Paging here as well
    would send two messages for one dead order."""
    pipeline = _alerting_pipeline(db)
    spec = _spec(db, limit_price=100.00)
    pipeline.broker.replace_entry_limit = _replace_returns("ord-2")
    pipeline.broker.wait_for_order_terminal.return_value = "new"

    _repeg_entry_order(pipeline, _ctx(), spec)

    assert alerts == []
    # ...but everything the alert will need is on the spec.
    assert spec["attempted_prices"] == [pytest.approx(CEILING_40BPS)]
    assert spec["ceiling"] == pytest.approx(CEILING_40BPS)
    assert spec["repeg_outcome"] == "replaced"


def test_a_market_that_never_moved_against_us_does_not_alert(db, alerts):
    """Nothing was tried, so nothing is exhausted."""
    pipeline = _alerting_pipeline(db)
    spec = _spec(db, limit_price=100.30)
    pipeline.broker.get_latest_quote.return_value = {"ask_price": 100.05}

    _repeg_entry_order(pipeline, _ctx(), spec)

    pipeline.broker.replace_entry_limit.assert_not_called()
    assert alerts == []


def test_a_partial_fill_does_not_alert(db, alerts):
    """Shares were acquired and the stop covers them. Not a dead order."""
    pipeline = _alerting_pipeline(db)
    spec = _spec(db, qty=10)
    pipeline.broker.get_order_fill_info.return_value = {
        "status": "partially_filled",
        "filled_qty": 4.0,
    }

    _repeg_entry_order(pipeline, _ctx(), spec)

    assert alerts == []


def test_an_order_already_at_its_ceiling_does_not_alert(db, alerts):
    pipeline = _alerting_pipeline(db)
    spec = _spec(db, limit_price=CEILING_40BPS)

    _repeg_entry_order(pipeline, _ctx(), spec)

    assert alerts == []


def test_a_broken_alert_channel_cannot_break_the_cancel_path(db, monkeypatch):
    """An alerting bug must not break the execution path it reports on."""
    import src.notifier as notifier
    from src.pipeline_stages import _alert_owner_entry_cancelled

    monkeypatch.setattr(
        notifier,
        "send_owner_alert",
        MagicMock(side_effect=RuntimeError("telegram down")),
    )
    pipeline = _alerting_pipeline(db, _exec_cfg())
    spec = _spec(db, limit_price=100.00)
    spec["attempted_prices"] = [CEILING_40BPS]
    spec["repeg_outcome"] = "replaced"

    _alert_owner_entry_cancelled(
        pipeline,
        spec,
        {"order_id": "ord-2", "status": "canceled"},
    )  # must not raise


# --------------------------------------------------------------------------
# stream trouble must not double-place
# --------------------------------------------------------------------------


def test_a_dead_stream_during_the_wait_places_nothing(db, alerts):
    """PR #287's own fallback is inside `wait_for_order_terminal`; if even
    that raises, the chase stops without sending anything."""
    pipeline = _alerting_pipeline(db)
    spec = _spec(db, limit_price=100.00)
    pipeline.broker.wait_for_order_terminal.side_effect = RuntimeError("stream and REST both gone")

    order_id, carried = _repeg_entry_order(pipeline, _ctx(), spec)

    assert (order_id, carried) == ("ord-1", 0.0)
    pipeline.broker.replace_entry_limit.assert_not_called()
    assert alerts == []


# ==========================================================================
# 2026-09-12 — (c) an unfilled entry is cancelled at the END OF ITS SESSION,
# not left resting until 16:00 ET, and the owner is told, once, with the
# prices that were tried.
# ==========================================================================


def _cancel_broker(*, statuses, filled_qty, cancel_raises=None):
    """A REAL AlpacaBroker with a mocked client: the terminal wait answers
    `statuses` in order (pre-cancel wait, then post-cancel wait)."""
    b = _broker()
    b.wait_for_order_terminal = MagicMock(side_effect=list(statuses))
    b.get_order_fill_info = MagicMock(
        return_value={
            "status": statuses[-1],
            "filled_qty": filled_qty,
            "filled_avg_price": 100.0,
        }
    )
    b._submit_stop_limit_order = MagicMock(return_value={"id": "stop-1"})
    if cancel_raises is not None:
        b.client.cancel_order_by_id.side_effect = cancel_raises
    return b


def test_an_unfilled_entry_is_cancelled_at_the_end_of_its_session():
    b = _cancel_broker(statuses=["new", "canceled"], filled_qty=0.0)
    seen = []

    out = b.place_entry_protection(
        "NVDA",
        "ord-1",
        95.0,
        requested_qty=10,
        on_unfilled_cancel=seen.append,
    )

    assert out is None
    b.client.cancel_order_by_id.assert_called_once_with("ord-1")
    assert seen == [{"order_id": "ord-1", "status": "canceled", "filled_qty": 0.0}]
    b._submit_stop_limit_order.assert_not_called()


def test_the_cancel_is_reported_even_when_the_last_status_was_pre_exchange():
    """The open-market case: never acknowledged, never repriced, cancelled."""
    b = _cancel_broker(statuses=["accepted", "canceled"], filled_qty=0.0)
    seen = []

    b.place_entry_protection(
        "NVDA",
        "ord-1",
        95.0,
        requested_qty=10,
        on_unfilled_cancel=seen.append,
    )

    assert len(seen) == 1 and seen[0]["order_id"] == "ord-1"


def test_a_partial_fill_is_protected_and_does_not_page_as_cancelled():
    """Shares were acquired: the stop covers them and the remainder is
    converged exactly as before. Not a dead order — no cancel alert."""
    b = _cancel_broker(statuses=["partially_filled", "canceled"], filled_qty=4.0)
    seen = []

    out = b.place_entry_protection(
        "NVDA",
        "ord-1",
        95.0,
        requested_qty=10,
        on_unfilled_cancel=seen.append,
    )

    assert out == {"id": "stop-1"}
    assert b._submit_stop_limit_order.call_args.kwargs["qty"] == 4.0
    assert seen == []


def test_a_fill_under_a_superseded_id_is_not_reported_as_unfilled():
    """The raced-partial case: the replacement filled nothing but the
    ancestor did. That is a position with a stop, not a cancelled entry."""
    b = _cancel_broker(statuses=["new", "canceled"], filled_qty=0.0)
    seen = []

    out = b.place_entry_protection(
        "NVDA",
        "ord-2",
        95.0,
        requested_qty=10,
        superseded_filled_qty=6.0,
        on_unfilled_cancel=seen.append,
    )

    assert out == {"id": "stop-1"}
    assert seen == []


def test_a_normal_fast_fill_cancels_nothing_and_pages_nothing():
    b = _cancel_broker(statuses=["filled"], filled_qty=10.0)
    seen = []

    b.place_entry_protection(
        "NVDA",
        "ord-1",
        95.0,
        requested_qty=10,
        on_unfilled_cancel=seen.append,
    )

    b.client.cancel_order_by_id.assert_not_called()
    assert seen == []
    assert b._submit_stop_limit_order.call_args.kwargs["qty"] == 10.0


def test_a_cancel_the_broker_refused_is_not_reported_as_cancelled():
    """Honesty: if the cancel did not land the order may still be working,
    and telling the owner it was cancelled would be a lie."""
    b = _cancel_broker(
        statuses=["new", "new"],
        filled_qty=0.0,
        cancel_raises=RuntimeError("alpaca 500"),
    )
    seen = []

    b.place_entry_protection(
        "NVDA",
        "ord-1",
        95.0,
        requested_qty=10,
        on_unfilled_cancel=seen.append,
    )

    assert seen == []


def test_a_raising_cancel_callback_cannot_break_protection():
    b = _cancel_broker(statuses=["new", "canceled"], filled_qty=0.0)

    out = b.place_entry_protection(
        "NVDA",
        "ord-1",
        95.0,
        requested_qty=10,
        on_unfilled_cancel=MagicMock(side_effect=RuntimeError("boom")),
    )

    assert out is None


def test_the_cancel_is_never_sent_twice():
    """One cancel, then a wait for it to land — never a second cancel on
    the same id, whatever the post-cancel status read says."""
    b = _cancel_broker(statuses=["new", "pending_cancel"], filled_qty=0.0)

    b.place_entry_protection("NVDA", "ord-1", 95.0, requested_qty=10)

    assert b.client.cancel_order_by_id.call_count == 1


# --------------------------------------------------------------------------
# the alert: content, and the whole thing end to end through the stage
# --------------------------------------------------------------------------


def _stage_with_cancelling_protection(db, cfg, *, info):
    """The stage pipeline, with `place_entry_protection` standing in for the
    real one by invoking the cancel callback the way the real one does."""
    pipeline = _stage_pipeline(db, cfg)

    def _protect(**kwargs):
        cb = kwargs.get("on_unfilled_cancel")
        if cb is not None:
            cb(info)
        return None

    pipeline.broker.place_entry_protection = MagicMock(side_effect=_protect)
    # A zero-fill read, as the real broker would report after the cancel —
    # otherwise the auto-MagicMock reads as "shares filled, no stop" and the
    # separate naked-position alert fires too.
    pipeline.broker.get_order_fill_info.return_value = {
        "status": "canceled",
        "filled_qty": 0.0,
    }
    return pipeline


def test_the_cycle_end_cancel_pages_the_owner_with_what_was_tried(db, alerts):
    """The full story in one message: repriced once to the ceiling, still
    unfilled, CANCELLED (not left working), nothing resubmitted."""
    pipeline = _stage_with_cancelling_protection(
        db,
        _exec_cfg(),
        info={"order_id": "ord-2", "status": "canceled", "filled_qty": 0.0},
    )
    pipeline.broker.get_latest_quote.side_effect = [
        {"ask_price": None},  # submission: no usable ask
        {"ask_price": 100.30},  # reprice: market has moved away
    ]
    pipeline.broker.wait_for_order_terminal.return_value = "new"
    pipeline.broker.get_order_fill_info.return_value = {
        "status": "new",
        "filled_qty": 0.0,  # working, unfilled, at the venue
    }
    pipeline.broker.replace_entry_limit.return_value = {
        "id": "ord-2",
        "status": "accepted",
    }

    ExecutionStage(pipeline=pipeline).run(_stage_ctx())

    assert len(alerts) == 1, "exactly one standalone alert, not two"
    text = alerts[0]
    assert "NVDA" in text
    assert "ENTRY DID NOT FILL" in text
    assert "cancelled at the end of its session" in text
    assert "repriced ONCE" in text
    assert "$100.00 → $100.40" in text  # submitted limit → the one reprice
    assert "$100.40" in text  # the ceiling
    assert "ord-2" in text and "CANCELLED" in text
    assert "NOT left working" in text
    assert "will not resubmit" in text
    assert "next session" in text  # the derivation, in plain words
    # The reprice function itself did not also page.
    assert pipeline.broker.replace_entry_limit.call_count == 1


def test_the_cycle_end_cancel_pages_even_with_repricing_off(db, alerts):
    """Production today: repeg off. The cancel still fires and the owner is
    still told — and told that no reprice was attempted, and why."""
    pipeline = _stage_with_cancelling_protection(
        db,
        _exec_cfg(repeg_enabled=False),
        info={"order_id": "ord-1", "status": "canceled", "filled_qty": 0.0},
    )

    ExecutionStage(pipeline=pipeline).run(_stage_ctx())

    assert len(alerts) == 1
    assert "switched off" in alerts[0]
    assert "ord-1" in alerts[0] and "CANCELLED" in alerts[0]
    pipeline.broker.replace_entry_limit.assert_not_called()


def test_the_cancel_alert_names_the_unacknowledged_reason(db, alerts):
    """Open-market case end to end: the venue never acknowledged the order,
    so no reprice went out, and the alert says exactly that."""
    pipeline = _stage_with_cancelling_protection(
        db,
        _exec_cfg(),
        info={"order_id": "ord-1", "status": "canceled", "filled_qty": 0.0},
    )
    pipeline.broker.get_latest_quote.side_effect = [
        {"ask_price": None},
        {"ask_price": 100.30},
    ]
    pipeline.broker.wait_for_order_terminal.return_value = "pending_new"
    pipeline.broker.wait_for_order_at_exchange.return_value = "pending_new"

    ExecutionStage(pipeline=pipeline).run(_stage_ctx())

    pipeline.broker.replace_entry_limit.assert_not_called()
    assert len(alerts) == 1
    assert "had not acknowledged" in alerts[0]
    assert "NOT attempted" in alerts[0]
    assert "$100.00" in alerts[0]  # the only price ever on the book


def test_a_stage_with_a_filled_entry_pages_nothing(db, alerts):
    pipeline = _stage_pipeline(db, _exec_cfg())
    pipeline.broker.place_entry_protection.return_value = {"id": "stop-1"}
    pipeline.broker.wait_for_order_terminal.return_value = "filled"

    ExecutionStage(pipeline=pipeline).run(_stage_ctx())

    assert alerts == []


# --------------------------------------------------------------------------
# a websocket outage degrades to the fallback — never a double action
# --------------------------------------------------------------------------


def test_a_dead_stream_and_dead_rest_places_nothing_and_cancels_nothing(db, alerts):
    """If even the REST fallback inside the waits raises, the reprice stops
    without sending anything, protection is still called exactly once, and
    the reprice path issues no cancel of its own."""
    pipeline = _stage_pipeline(db, _exec_cfg())
    pipeline.broker.get_latest_quote.side_effect = [
        {"ask_price": None},
        {"ask_price": 100.30},
    ]
    pipeline.broker.wait_for_order_terminal.side_effect = RuntimeError("stream and REST both gone")
    pipeline.broker.wait_for_order_at_exchange.side_effect = RuntimeError("stream and REST both gone")

    ExecutionStage(pipeline=pipeline).run(_stage_ctx())

    pipeline.broker.replace_entry_limit.assert_not_called()
    pipeline.broker.cancel_entry_order.assert_not_called()
    pipeline.broker.submit_order.assert_called_once()
    pipeline.broker.place_entry_protection.assert_called_once()
    assert pipeline.broker.place_entry_protection.call_args.kwargs["order_id"] == "ord-1"


# --------------------------------------------------------------------------
# board item 197 — the bound has a side
# --------------------------------------------------------------------------

FLOOR_40BPS = round(REFERENCE * (1 - 40 / 10_000.0), 2)


def _short_spec(db, *, limit_price):
    spec = _spec(db, limit_price=limit_price)
    spec["side"] = "sell_short"
    return spec


def test_short_entry_repegs_DOWN_to_the_floor_never_up(db):
    """A short sale fills at the bid, so its slippage bound is BELOW the
    reference. The pre-197 code built one ceiling above it and would have
    walked a short's limit away from any fill."""
    pipeline = _pipeline(db)
    spec = _short_spec(db, limit_price=99.90)
    # Bid is below our 99.90 offer, so the resting short is not marketable.
    pipeline.broker.get_latest_quote.return_value = {
        "ask_price": 99.60,
        "bid_price": 99.50,
    }
    pipeline.broker.replace_entry_limit = _replace_returns("ord-2")
    pipeline.broker.wait_for_order_terminal.side_effect = ["accepted", "filled"]

    order_id, _ = _repeg_entry_order(pipeline, _ctx(), spec)

    assert order_id == "ord-2"
    new_limit = pipeline.broker.replace_entry_limit.call_args[0][1]
    assert new_limit == pytest.approx(FLOOR_40BPS)
    # The load-bearing assertion: the limit moved TOWARD the market, not away.
    assert new_limit < spec["limit_price"]
    assert new_limit < REFERENCE
    assert spec["ceiling"] == pytest.approx(FLOOR_40BPS)


def test_short_limit_already_at_the_floor_has_no_room(db):
    """The room test must invert too, or a short sitting at its bound reads
    as having room and a short far from it reads as already there."""
    pipeline = _pipeline(db)
    spec = _short_spec(db, limit_price=FLOOR_40BPS)

    order_id, carried = _repeg_entry_order(pipeline, _ctx(), spec)

    assert (order_id, carried) == ("ord-1", 0.0)
    assert spec["repeg_outcome"] == "no_room"
    pipeline.broker.replace_entry_limit.assert_not_called()


def test_short_already_marketable_is_left_alone(db):
    """Bid at or above the short's offer means it can fill as it stands."""
    pipeline = _pipeline(db)
    spec = _short_spec(db, limit_price=99.90)
    pipeline.broker.get_latest_quote.return_value = {
        "ask_price": 100.10,
        "bid_price": 99.95,
    }

    order_id, _ = _repeg_entry_order(pipeline, _ctx(), spec)

    assert order_id == "ord-1"
    assert spec["repeg_outcome"] == "market_within_limit"
    pipeline.broker.replace_entry_limit.assert_not_called()


def test_buy_side_bound_is_unchanged_by_the_side_fix(db):
    """Regression guard: the long path must still build a ceiling ABOVE."""
    pipeline = _pipeline(db)
    spec = _spec(db, limit_price=100.10)
    pipeline.broker.replace_entry_limit = _replace_returns("ord-2")
    pipeline.broker.wait_for_order_terminal.side_effect = ["accepted", "filled"]

    _repeg_entry_order(pipeline, _ctx(), spec)

    assert spec["ceiling"] == pytest.approx(CEILING_40BPS)
    assert pipeline.broker.replace_entry_limit.call_args[0][1] > REFERENCE
