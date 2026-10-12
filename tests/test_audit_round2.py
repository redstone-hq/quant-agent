"""Audit round 2 — entry-order lifecycle, breaker×sweep, multi-stop handling.

The interaction findings cluster: today's entry-protection flow (PR #102)
left gaps that only show when the pieces run together.
"""

from unittest.mock import MagicMock, patch

import pytest

from src.execution.broker import AlpacaBroker
from src.models import Position
from tests.pipeline_factory import build_pipeline


def _broker(mock_tc_cls):
    mock_client = MagicMock()
    mock_tc_cls.return_value = mock_client
    b = AlpacaBroker(api_key="t", secret_key="t", paper=True)
    return b, mock_client


# ---------- still-working entries are cancelled before walking away ----------


@patch("src.execution.broker.TradingClient")
def test_entry_protection_cancels_a_still_working_entry(mock_tc_cls):
    """A DAY entry limit alive after the wait could fill hours later with no
    stop watching — the remainder must be cancelled, and whatever filled by
    then still gets its stop."""
    b, client = _broker(mock_tc_cls)
    b.wait_for_order_terminal = MagicMock(side_effect=["accepted", "canceled"])
    b.get_order_fill_info = MagicMock(return_value={"filled_qty": 4.0})
    stop_order = MagicMock(id="s1", status="new", symbol="NVDA")
    client.submit_order.return_value = stop_order

    out = b.place_entry_protection("NVDA", "e1", stop_price=90.0, requested_qty=10)

    client.cancel_order_by_id.assert_called_once_with("e1")
    assert out is not None
    req = client.submit_order.call_args[0][0]
    assert float(req.qty) == 4.0  # protect exactly what landed


@patch("src.execution.broker.TradingClient")
def test_entry_protection_terminal_zero_fill_does_not_cancel(mock_tc_cls):
    b, client = _broker(mock_tc_cls)
    b.wait_for_order_terminal = MagicMock(return_value="expired")
    b.get_order_fill_info = MagicMock(return_value={"filled_qty": 0.0})
    assert b.place_entry_protection("NVDA", "e1", 90.0) is None
    client.cancel_order_by_id.assert_not_called()


# ---------- full exits cancel the same-day resting entry BUY ----------


def test_full_exit_sell_cancels_same_symbol_entry_orders():
    p = build_pipeline(broker=MagicMock(), db=MagicMock())
    p.broker.submit_order.return_value = {"id": "o1", "status": "accepted"}
    p._cancel_stops_with_write_ahead = MagicMock(return_value=(True, [], 7))

    p._submit_protected_sell(
        symbol="VST", qty=31, limit_price=150.0, reference_price=151.0, position_qty_before_sell=31, label="SELL"
    )
    p.broker.cancel_open_entry_orders.assert_called_once_with(symbol="VST")


def test_partial_trim_keeps_its_entry_orders():
    p = build_pipeline(broker=MagicMock(), db=MagicMock())
    p.broker.submit_order.return_value = {"id": "o1", "status": "accepted"}
    p.broker.snapshot_protective_stops.return_value = (True, [])
    p._cancel_stops_with_write_ahead = MagicMock(return_value=(True, [], 7))

    p._submit_protected_sell(
        symbol="VST", qty=10, limit_price=150.0, reference_price=151.0, position_qty_before_sell=31, label="REDUCE"
    )
    p.broker.cancel_open_entry_orders.assert_not_called()
    # A trim keeps shares, so it never takes the cancel-all route either.
    p._cancel_stops_with_write_ahead.assert_not_called()


def _park_pipeline():
    from types import SimpleNamespace
    from src.config import CashSweepConfig, RiskConfig
    from src.execution.cash_sweep import CashSweeper

    p = build_pipeline(broker=MagicMock(), db=MagicMock(), risk_engine=MagicMock())
    p.config = SimpleNamespace(
        cash_sweep=CashSweepConfig(enabled=True, symbol="SGOV"),
        risk=RiskConfig(
            max_position_pct=20,
            max_total_position_pct=90,
            require_stop_loss=True,
            allow_margin=False,
        ),
    )
    p.broker.get_account.return_value = {
        "cash": 99_000.0,
        "portfolio_value": 100_000.0,
        "last_equity": 104_000.0,
    }
    p.broker.get_positions.return_value = []
    p.broker.open_buy_notional.return_value = 0.0
    p.broker.get_latest_price.return_value = 100.60
    p.broker.submit_order.return_value = {"id": "b1", "status": "accepted"}
    p.db.insert_trade.return_value = 9
    p.cash_sweeper = CashSweeper(pipeline=p)
    return p


# ---------- multi-stop: highest wins; ex-div shifts each ----------


def _stop_order(oid, stop, qty=10):
    o = MagicMock()
    o.id = oid
    o.order_type = "stop_limit"
    o.side = "sell"
    o.stop_price = stop
    o.qty = qty
    o.limit_price = stop * 0.97
    return o


@patch("src.execution.broker.TradingClient")
def test_get_current_stop_price_reports_the_highest_of_many(mock_tc_cls):
    """Per-BUY GTC stops make multi-stop positions the steady state; the
    'current stop' must be the first to trigger (highest), not whatever
    Alpaca happens to list first."""
    b, client = _broker(mock_tc_cls)
    client.get_orders.return_value = [
        _stop_order("s1", 340.0),
        _stop_order("s2", 350.0),
        _stop_order("s3", 330.0),
    ]
    assert b.get_current_stop_price("GE") == 350.0


@patch("src.execution.broker.TradingClient")
def test_shift_stops_down_preserves_per_lot_levels(mock_tc_cls):
    b, client = _broker(mock_tc_cls)
    b._list_open_sell_stop_orders = MagicMock(
        return_value=[
            _stop_order("s1", 340.0, qty=10),
            _stop_order("s2", 350.0, qty=16),
        ]
    )
    b.cancel_snapshotted_stops = MagicMock(return_value=MagicMock(cleared=True))
    b._restore_stop_orders = MagicMock(return_value=(2, []))

    out = b.shift_stops_down("GE", 0.51)

    assert out is not None and out["shifted"] == 2
    shifted_specs = b._restore_stop_orders.call_args[0][1]
    assert sorted(s["stop_price"] for s in shifted_specs) == [339.49, 349.49]
    assert sorted(s["qty"] for s in shifted_specs) == [10, 16]


# ---------- coverage repair sees the in-flight BUY ----------


def test_repair_reads_the_in_flight_buy_row(tmp_path):
    """A same-session BUY still at fill_status='submitted' is the row whose
    stop the repair wants — the strict executed predicate made the repair
    no-op (or read a months-old prior BUY) in exactly the crash/late-fill
    scenarios the belt exists for."""
    from src.storage.db import Database

    db = Database(str(tmp_path / "t.db"))
    db.initialize()
    db.insert_trade(
        symbol="NVDA",
        action="BUY",
        qty=10,
        price=100.0,
        reasoning="old entry",
        run_id="r0",
        stop_loss=80.0,
        fill_status="filled",
    )
    db.insert_trade(
        symbol="NVDA",
        action="BUY",
        qty=10,
        price=150.0,
        reasoning="today",
        run_id="r1",
        stop_loss=140.0,
        broker_order_id="b9",
        fill_status="submitted",
    )
    strict = db.get_symbol_last_buy("NVDA")
    in_flight = db.get_symbol_last_buy("NVDA", include_in_flight=True)
    assert strict["stop_loss"] == 80.0  # PM memory keeps executed-only
    assert in_flight["stop_loss"] == 140.0  # repair reads today's intent


def test_repair_reads_the_in_flight_short_row(tmp_path):
    """Mirrored belt: a same-session SHORT still at fill_status='submitted'
    is the row whose stop the short-side repair wants."""
    from src.storage.db import Database

    db = Database(str(tmp_path / "t.db"))
    db.initialize()
    db.insert_trade(
        symbol="TSLA",
        action="SHORT",
        qty=10,
        price=200.0,
        reasoning="old short",
        run_id="r0",
        stop_loss=220.0,
        fill_status="filled",
    )
    db.insert_trade(
        symbol="TSLA",
        action="SHORT",
        qty=10,
        price=210.0,
        reasoning="today",
        run_id="r1",
        stop_loss=232.0,
        broker_order_id="s9",
        fill_status="submitted",
    )
    strict = db.get_symbol_last_buy("TSLA", action="SHORT")
    in_flight = db.get_symbol_last_buy(
        "TSLA",
        include_in_flight=True,
        action="SHORT",
    )
    assert strict["stop_loss"] == 220.0
    assert in_flight["stop_loss"] == 232.0
    # Default last-buy must not start returning SHORT rows.
    assert db.get_symbol_last_buy("TSLA") is None


# ---------- round-2 backlog fixes (pipeline/data/db bucket) ----------


def test_pm_parse_failure_is_analysis_error_not_no_trades():
    """ "no_trades" masqueraded a parse failure as a deliberate hold — exit 0,
    last-run marker written, trading day silently skipped. analysis_error is
    retryable: the next tick retries (and the checkpoint resumes at RM)."""
    from src import decision_checkpoint as dc

    p = build_pipeline(
        _is_trading_day=lambda: True,
        _drain_pending_protection_restores=MagicMock(),
        _reconcile_orphan_pending_submits=MagicMock(),
        _reconcile_stop_coverage=MagicMock(return_value=[]),
        _reconcile_fills=MagicMock(),
        _force_delever=MagicMock(return_value=[]),
        broker=MagicMock(),
        risk_engine=MagicMock(),
        morning_research_stage=MagicMock(),
        decision_stage=MagicMock(),
    )
    p.broker.get_account.return_value = {
        "cash": 50_000.0,
        "portfolio_value": 100_000.0,
        "last_equity": 100_000.0,
    }
    p.broker.get_positions.return_value = []

    def _research(ctx):
        ctx.analyses = [MagicMock()]
        ctx.data_status = {"tech": "ok"}

    p.morning_research_stage.run.side_effect = _research
    p.decision_stage.run.side_effect = lambda ctx: (
        setattr(ctx, "analysis_failure_status", "pm_parse_error"),
        setattr(ctx, "analysis_failure_error", "PM returned non-JSON body"),
    )
    p._check_late_breach_and_emergency_liquidate = MagicMock(return_value=None)

    with (
        patch.object(dc, "load", return_value=None),
        patch.object(dc, "write", return_value=None),
        patch.object(dc, "write_status"),
        patch.object(dc, "mark_consumed"),
    ):
        result = p.run_morning()
    assert result["status"] == "pm_parse_error"


def test_calibration_matches_sell_to_the_true_old_lot(tmp_path):
    """Windowing BUYs alongside SELLs made a SELL that closed a pre-window
    lot FIFO-match an unrelated newer BUY — wrong entry, wrong hold time."""
    from src.storage.db import Database

    db = Database(str(tmp_path / "t.db"))
    db.initialize()
    old_ts = "2026-10-12 14:00:00"  # oldest lot; all later rows are re-dated after it below
    db.conn.execute(
        "INSERT INTO trades (symbol, action, qty, price, fill_status, timestamp) "
        "VALUES ('NVDA', 'BUY', 100, 150.0, 'filled', ?)",
        (old_ts,),
    )
    db.conn.commit()
    for i, sym in enumerate(("A", "B")):  # filler to clear the >=3 floor
        db.insert_trade(
            symbol=sym,
            action="BUY",
            qty=1,
            price=100.0,
            reasoning="x",
            run_id="r",
            fill_status="filled",
            stop_loss=90.0,
        )
        db.insert_trade(symbol=sym, action="SELL", qty=1, price=110.0, reasoning="x", run_id="r", fill_status="filled")
    db.insert_trade(symbol="NVDA", action="SELL", qty=100, price=210.0, reasoning="x", run_id="r", fill_status="filled")

    db.conn.execute(
        "UPDATE trades SET timestamp = datetime('2026-10-13 14:00:00', '+' || id || ' seconds') WHERE timestamp != ?",
        (old_ts,),
    )
    db.conn.commit()
    calib = db.compute_trade_calibration(lookback_days=30)
    nvda = [c for c in db.conn.execute("SELECT 1").fetchall()]  # keep db alive
    # The NVDA close must report the TRUE +40% vs the 60-day-old $150 lot,
    # not a phantom match. avg_return over {+10,+10,+40} = 20%.
    assert calib["n"] == 3
    assert abs(calib["avg_return_pct"] - 20.0) < 0.5


def test_missed_lessons_one_streak_is_not_recurring():
    """A single >=8% move re-emits on ~5 consecutive evenings via the rolling
    window — one episode, one symbol: NOT a recurring theme."""
    import json

    p = build_pipeline(db=MagicMock(), broker=MagicMock())
    rows = [
        {
            "date": f"2026-07-{d:02d}",
            "missed_opportunities_json": json.dumps(
                [
                    {"miss_category": "trend_timing_miss", "symbol": "SNDK", "theme_if_any": "", "lesson": "x"},
                ]
            ),
        }
        for d in (13, 14, 15)  # consecutive days = one episode
    ]
    p.db.get_recent_insights.return_value = rows
    assert p._build_recent_missed_lessons() == ""


def test_missed_lessons_two_symbols_same_theme_still_recurs():
    import json

    p = build_pipeline(db=MagicMock(), broker=MagicMock())
    p.db.get_recent_insights.return_value = [
        {
            "date": "2026-07-15",
            "missed_opportunities_json": json.dumps(
                [{"miss_category": "theme_blindspot", "symbol": "VST", "theme_if_any": "nuclear/power", "lesson": "x"}]
            ),
        },
        {
            "date": "2026-07-14",
            "missed_opportunities_json": json.dumps(
                [
                    {
                        "miss_category": "trend_timing_miss",
                        "symbol": "OKLO",
                        "theme_if_any": "nuclear/power",
                        "lesson": "y",
                    }
                ]
            ),
        },
    ]
    out = p._build_recent_missed_lessons()
    assert "nuclear/power" in out


def test_nonfinite_cash_blocks_instead_of_failing_open():
    from src.config import RiskConfig
    from src.risk.rules import RiskRuleEngine
    from src.models import TradeDecision
    from src.pipeline import HARD_BLOCK_RULES

    eng = RiskRuleEngine(
        RiskConfig(
            max_position_pct=20,
            max_total_position_pct=90,
            require_stop_loss=True,
            allow_margin=False,
        )
    )
    d = TradeDecision(
        action="BUY",
        symbol="NVDA",
        allocation_pct=10,
        entry_price=100.0,
        stop_loss=95.0,
        take_profit=120.0,
        reasoning="x",
    )
    v = eng.check(decision=d, positions=[], total_value=100_000.0, cash=float("nan"))
    assert v and any(x.rule in HARD_BLOCK_RULES for x in v)


def test_force_delever_unparks_only_what_the_deficit_needs():
    """Full-liquidating $80k of T-bills for a $500 deficit forced a pointless
    full re-park at the bookend; and the vehicle's exit is SWEEP_SELL so it
    stays out of the grading loops."""
    from types import SimpleNamespace
    from src.config import CashSweepConfig, RiskConfig
    from src.execution.cash_sweep import CashSweeper

    p = build_pipeline()
    p.config = SimpleNamespace(
        cash_sweep=CashSweepConfig(enabled=True, symbol="SGOV"),
        risk=RiskConfig(
            max_position_pct=20,
            max_total_position_pct=90,
            require_stop_loss=True,
            allow_margin=False,
        ),
    )
    p.cash_sweeper = CashSweeper(pipeline=p)
    p.broker = MagicMock()
    p.broker.get_account.return_value = {"cash": 10.0, "portfolio_value": 90_000.0}
    p.broker.get_positions.return_value = []
    # A real live quote: the partial sale sizes itself off the SELL limit the
    # order will actually rest at (board item 182 removed the flat 2%
    # cushion). Without this a MagicMock quote float()s to 1.0 and the sizing
    # would ask for one share per dollar of deficit.
    p.broker.get_latest_quote.return_value = {
        "bid_price": 100.5,
        "ask_price": 100.7,
    }
    p.db = MagicMock()
    p._submit_protected_sell = MagicMock(return_value=({"id": "s1", "status": "accepted"}, {"symbol": "SGOV"}))
    p._finalize_pending_protections = MagicMock()
    from src.pipeline_context import RunContext

    ctx = RunContext.start("morning")
    ctx.cash = -500.0
    ctx.positions = [
        Position(
            symbol="SGOV",
            qty=800,
            avg_entry=100.5,
            current_price=100.6,
            market_value=80_480,
            unrealized_pnl=80,
            sector="Unknown",
        )
    ]
    p._force_delever(ctx)
    kwargs = p._submit_protected_sell.call_args.kwargs
    assert kwargs["label"] == "SWEEP_SELL"  # ledger isolation held
    assert kwargs["qty"] <= 7  # ceil(500/100.5)=5 … not 800

    # Board item 182: the share count is the deficit divided by the price
    # floor the order carries, so it must be enough to actually clear the
    # deficit at that floor — not a padded guess, and not a share short.
    assert kwargs["qty"] * 100.5 >= 500.0

    # With NO live quote the order becomes a MARKET order, which has no price
    # floor at all — so there is no worst case to size a partial sale from,
    # and the loop sells the WHOLE position, exactly as every non-sweep
    # de-lever target already does. An intermediate draft sized this branch
    # off `mark x (1 - STOP_LIMIT_BUFFER_PCT)`; dividing by 0.97 is a 3.09%
    # pad, LARGER than the 2% cushion the change removed and borrowed from an
    # `status: arbitrary` stop-limit buffer picked for a different job.
    p.broker.get_latest_quote.return_value = {}
    p._submit_protected_sell.reset_mock()
    ctx2 = RunContext.start("morning")
    ctx2.cash = -500.0
    ctx2.positions = [
        Position(
            symbol="SGOV",
            qty=800,
            avg_entry=100.5,
            current_price=100.6,
            market_value=80_480,
            unrealized_pnl=80,
            sector="Unknown",
        )
    ]
    p._force_delever(ctx2)
    no_quote = p._submit_protected_sell.call_args.kwargs
    assert no_quote["limit_price"] is None, "no quote -> MARKET order"
    assert no_quote["qty"] == 800, "no price floor means no defensible partial size: sell the position"

    # The slice is credited at the limit it was sized off, not at the whole
    # park's market value. `market_value` is the FULL position; crediting it
    # for a slice overstated proceeds by everything left parked. Today that
    # overstatement is masked — the slice is sized to cover the deficit by
    # construction, so the break-early guard and the incompleteness alert
    # reach the same verdict either way — but the figure the alert would
    # REPORT to the owner was a phantom, and the masking disappears the
    # moment proceeds are booked from an actual filled quantity.
    p.broker.get_latest_quote.return_value = {
        "bid_price": 100.5,
        "ask_price": 100.7,
    }
    p._submit_protected_sell.reset_mock()
    p._alert_owner_force_delever_incomplete = MagicMock()
    ctx3 = RunContext.start("morning")
    ctx3.cash = -500.0
    ctx3.positions = [
        Position(
            symbol="SGOV",
            qty=800,
            avg_entry=100.5,
            current_price=100.6,
            market_value=80_480,
            unrealized_pnl=80,
            sector="Unknown",
        )
    ]
    p._force_delever(ctx3)
    slice_qty = p._submit_protected_sell.call_args.kwargs["qty"]
    assert slice_qty < 800, "this assertion is about the PARTIAL path"
    # Sized off the limit and credited at the limit: consistent, and enough.
    assert slice_qty * 100.5 >= 500.0
    assert p._alert_owner_force_delever_incomplete.call_count == 0


def test_earnings_batch_isolates_one_bad_filing():
    """audit round 2: one filing's failure (corrupt text, disk error) used to
    abort the WHOLE batch — the remaining symbols went silently unanalyzed."""
    from unittest.mock import patch as _patch
    from src.agents.earnings_analyst import EarningsAnalystAgent
    from src.data.earnings import EarningsReport

    with _patch("anthropic.Anthropic"):
        agent = EarningsAnalystAgent(api_key="k", model="claude-opus-4-7", max_tokens=1024)
    good = EarningsReport(
        symbol="AAPL",
        form_type="10-Q",
        filing_date="2026-07-10",
        filing_path="/x",
        analysis_path="/x/a.md",
        text_excerpt="",
        is_new=False,
    )
    bad = EarningsReport(
        symbol="NKE",
        form_type="10-Q",
        filing_date="2026-07-11",
        filing_path="/y",
        analysis_path="/y/a.md",
        text_excerpt="text",
        is_new=True,
    )
    with _patch.object(agent, "_analyze_one", side_effect=[RuntimeError("boom"), [{"symbol": "AAPL"}]]):
        out = agent.analyze_reports([bad, good])
    assert out == [{"symbol": "AAPL"}], "the good filing must survive the bad one"


# ---------- item 201: the ex-dividend shift amends in place ----------


def _plain_stop(oid, stop, qty=10):
    """A resting stop-MARKET order of the shape the 2026-09-30 amend
    measurement covered: simple, parentless, no legs, order_type 'stop'."""
    o = MagicMock()
    o.id = oid
    o.order_type = "stop"
    o.order_class = "simple"
    o.legs = None
    o.parent_id = None
    o.side = "sell"
    o.stop_price = stop
    o.qty = qty
    o.limit_price = None
    return o


@patch("src.execution.broker.TradingClient")
def test_shift_stops_down_amends_in_place_and_never_cancels(mock_tc_cls):
    """The protective stop must never be absent. A price-only shift is exactly
    the operation the broker amends atomically, so this path must not cancel."""
    b, client = _broker(mock_tc_cls)
    b._list_open_sell_stop_orders = MagicMock(
        return_value=[
            _plain_stop("s1", 340.0, qty=10),
            _plain_stop("s2", 350.0, qty=16),
        ]
    )
    b.cancel_snapshotted_stops = MagicMock(return_value=MagicMock(cleared=True))
    b._restore_stop_orders = MagicMock(return_value=(2, []))

    out = b.shift_stops_down("GE", 0.51)

    assert out is not None and out["shifted"] == 2 and out["mode"] == "amend"
    b.cancel_snapshotted_stops.assert_not_called()
    client.cancel_order_by_id.assert_not_called()
    b._restore_stop_orders.assert_not_called()
    amended = {c[0][0]: c[0][1].stop_price for c in client.replace_order_by_id.call_args_list}
    assert amended == {"s1": 339.49, "s2": 349.49}


@patch("src.execution.broker.TradingClient")
def test_shift_stops_down_keeps_the_fractional_hybrid_pair_as_two_stops(mock_tc_cls):
    """The §11.1 hybrid pair is a durable GTC whole-share leg plus a DAY sliver
    leg. Amending each leg's price in place cannot collapse them into one stop:
    each keeps its own id and its own qty, and no leg is re-submitted."""
    b, client = _broker(mock_tc_cls)
    b._list_open_sell_stop_orders = MagicMock(
        return_value=[
            _plain_stop("gtc-whole", 100.0, qty=12),
            _plain_stop("day-sliver", 100.0, qty=0.3456),
        ]
    )
    b.cancel_snapshotted_stops = MagicMock(return_value=MagicMock(cleared=True))
    b._restore_stop_orders = MagicMock(return_value=(2, []))

    out = b.shift_stops_down("ZZZ", 0.25)

    assert out["shifted"] == 2 and out["total"] == 2
    ids = [c[0][0] for c in client.replace_order_by_id.call_args_list]
    assert ids == ["gtc-whole", "day-sliver"]
    client.submit_order.assert_not_called()
    client.cancel_order_by_id.assert_not_called()


@patch("src.execution.broker.TradingClient")
def test_shift_stops_down_leaves_the_stop_resting_when_the_amend_is_refused(mock_tc_cls):
    """A refused amend must NOT fall through to cancel+resubmit — that would
    re-open the very unprotected window this path exists to close."""
    b, client = _broker(mock_tc_cls)
    b._list_open_sell_stop_orders = MagicMock(
        return_value=[
            _plain_stop("s1", 340.0, qty=10),
            _plain_stop("s2", 350.0, qty=16),
        ]
    )
    b.cancel_snapshotted_stops = MagicMock(return_value=MagicMock(cleared=True))
    b._restore_stop_orders = MagicMock(return_value=(2, []))
    client.replace_order_by_id.side_effect = [RuntimeError("422 refused"), MagicMock(id="s2b")]

    out = b.shift_stops_down("GE", 0.51)

    assert out["shifted"] == 1 and out["total"] == 2
    client.cancel_order_by_id.assert_not_called()
    b.cancel_snapshotted_stops.assert_not_called()


@patch("src.execution.broker.TradingClient")
def test_shift_stops_down_falls_back_for_an_unmeasured_shape(mock_tc_cls):
    """An unamendable shape (a bracket PARENT, which carries legs) sends the
    whole symbol down the legacy path, never half one way and half the other."""
    b, client = _broker(mock_tc_cls)
    parent = _stop_order("s2", 350.0, qty=16)
    parent.legs = [object()]
    b._list_open_sell_stop_orders = MagicMock(return_value=[_plain_stop("s1", 340.0, qty=10), parent])
    b.cancel_snapshotted_stops = MagicMock(return_value=MagicMock(cleared=True))
    b._restore_stop_orders = MagicMock(return_value=(2, []))

    out = b.shift_stops_down("GE", 0.51)

    assert out["mode"] == "cancel_resubmit" and out["shifted"] == 2
    client.replace_order_by_id.assert_not_called()
    b.cancel_snapshotted_stops.assert_called_once()


@patch("src.execution.broker.TradingClient")
def test_shift_stops_down_refuses_to_push_a_stop_to_zero(mock_tc_cls):
    b, client = _broker(mock_tc_cls)
    b._list_open_sell_stop_orders = MagicMock(return_value=[_plain_stop("s1", 0.40, qty=10)])
    b.cancel_snapshotted_stops = MagicMock(return_value=MagicMock(cleared=True))

    assert b.shift_stops_down("GE", 0.51) is None
    client.replace_order_by_id.assert_not_called()
    b.cancel_snapshotted_stops.assert_not_called()


# ---------- item 201 round 2: the failure branch ----------


def _replaced(oid, status="accepted"):
    r = MagicMock()
    r.id = oid
    r.status = status
    return r


class _ApiErr(Exception):
    def __init__(self, msg, status_code):
        super().__init__(msg)
        self.status_code = status_code


@patch("src.execution.broker.TradingClient")
def test_shift_amend_without_a_broker_answer_is_unknown_not_resting(mock_tc_cls):
    """A 504 or a timeout may have been applied before the answer was lost.
    Claiming the stop is 'still resting at the old price' would be a false
    statement about live protection."""
    b, client = _broker(mock_tc_cls)
    b._list_open_sell_stop_orders = MagicMock(return_value=[_plain_stop("s1", 340.0, qty=10)])
    b.cancel_snapshotted_stops = MagicMock(return_value=MagicMock(cleared=True))
    client.replace_order_by_id.side_effect = _ApiErr("gateway timeout", 504)

    out = b.shift_stops_down("GE", 0.51)

    assert out["status"] == "unknown" and out["id"] is None
    assert out["legs"][0]["outcome"] == "unknown"
    client.cancel_order_by_id.assert_not_called()


@patch("src.execution.broker.TradingClient")
def test_shift_amend_refusal_is_classified_from_the_broker_status_code(mock_tc_cls):
    b, client = _broker(mock_tc_cls)
    b._list_open_sell_stop_orders = MagicMock(return_value=[_plain_stop("s1", 340.0, qty=10)])
    client.replace_order_by_id.side_effect = _ApiErr("unprocessable", 422)

    out = b.shift_stops_down("GE", 0.51)

    assert out["status"] == "refused" and out["id"] is None
    assert out["legs"][0]["outcome"] == "refused"


@patch("src.execution.broker.TradingClient")
def test_a_partial_shift_carries_no_order_id_so_it_cannot_read_as_accepted(mock_tc_cls):
    """1-of-2 must not pass `accepted_stop_order`, or the caller writes every
    leg back at the shifted level and files a trade row for a stop that is
    still at the pre-dividend price."""
    from src.execution.stop_records import accepted_stop_order

    b, client = _broker(mock_tc_cls)
    b._list_open_sell_stop_orders = MagicMock(
        return_value=[
            _plain_stop("s1", 340.0, qty=10),
            _plain_stop("s2", 350.0, qty=16),
        ]
    )
    client.replace_order_by_id.side_effect = [_replaced("s1b"), _ApiErr("no", 422)]

    out = b.shift_stops_down("GE", 0.51)

    assert out["status"] == "partial" and out["shifted"] == 1 and out["total"] == 2
    assert accepted_stop_order(out) is False
    assert [l["outcome"] for l in out["legs"]] == ["amended", "refused"]


@patch("src.execution.broker.TradingClient")
def test_shift_requires_a_confirmed_id_and_a_live_status(mock_tc_cls):
    """'No exception' is request accepted, not stop moved."""
    b, client = _broker(mock_tc_cls)
    b._list_open_sell_stop_orders = MagicMock(
        return_value=[
            _plain_stop("s1", 340.0, qty=10),
            _plain_stop("s2", 350.0, qty=16),
        ]
    )
    client.replace_order_by_id.side_effect = [_replaced(None), _replaced("s2b", "rejected")]
    # A dead replacement is NOT evidence the original survived — the book is
    # re-read, and here it still holds the original s2.
    b._list_open_stop_orders_by_side = MagicMock(return_value=([_plain_stop("s2", 350.0, qty=16)], []))

    out = b.shift_stops_down("GE", 0.51)

    assert out["shifted"] == 0
    assert [l["outcome"] for l in out["legs"]] == ["unknown", "refused"]
    b._list_open_stop_orders_by_side.assert_called_once()


@patch("src.execution.broker.TradingClient")
def test_a_full_shift_reports_the_new_broker_ids(mock_tc_cls):
    b, client = _broker(mock_tc_cls)
    b._list_open_sell_stop_orders = MagicMock(
        return_value=[
            _plain_stop("s1", 340.0, qty=10),
            _plain_stop("s2", 350.0, qty=16),
        ]
    )
    client.replace_order_by_id.side_effect = [_replaced("s1b"), _replaced("s2b")]

    out = b.shift_stops_down("GE", 0.51)

    assert out["status"] == "accepted" and out["id"] == "s1b"
    assert [l["new_id"] for l in out["legs"]] == ["s1b", "s2b"]


@patch("src.execution.broker.TradingClient")
def test_trailing_amends_both_hybrid_legs_in_place(mock_tc_cls):
    """9 of 11 open positions are fractional and every one carries the two-leg
    hybrid pair, so a one-order-only atomic path left the trailing stop
    cancelling and resubmitting on most of the book."""
    b, client = _broker(mock_tc_cls)
    orders = [_plain_stop("gtc", 90.0, qty=12), _plain_stop("day", 90.0, qty=0.3456)]
    specs = [
        {"id": "gtc", "qty": 12, "stop_price": 90.0, "limit_price": None},
        {"id": "day", "qty": 0.3456, "stop_price": 90.0, "limit_price": None},
    ]
    client.replace_order_by_id.side_effect = [_replaced("gtc2"), _replaced("day2")]

    out = b._amend_resting_stop_price(
        symbol="ZZZ",
        live_orders=orders,
        stop_specs=specs,
        new_stop_price=95.0,
        position_qty=12.3456,
    )

    assert out is not None and out["id"] == "gtc2"
    assert [l["new_id"] for l in out["legs"]] == ["gtc2", "day2"]
    client.cancel_order_by_id.assert_not_called()
    client.submit_order.assert_not_called()


@patch("src.execution.broker.TradingClient")
def test_trailing_multi_leg_amend_with_no_answer_does_not_fall_back_to_cancel(mock_tc_cls):
    """An unknown outcome must return the 'do not cancel' channel, never the
    cancel+resubmit fallback — the amend may already have landed."""
    b, client = _broker(mock_tc_cls)
    orders = [_plain_stop("gtc", 90.0, qty=12), _plain_stop("day", 90.0, qty=0.3456)]
    specs = [
        {"id": "gtc", "qty": 12, "stop_price": 90.0, "limit_price": None},
        {"id": "day", "qty": 0.3456, "stop_price": 90.0, "limit_price": None},
    ]
    client.replace_order_by_id.side_effect = [_replaced("gtc2"), _ApiErr("timeout", 504)]

    out = b._amend_resting_stop_price(
        symbol="ZZZ",
        live_orders=orders,
        stop_specs=specs,
        new_stop_price=95.0,
        position_qty=12.3456,
    )

    # The payload must TRAVEL — a bare None threw away which leg moved, so
    # nothing could be recorded and nobody could be told.
    assert out["id"] is None and out["amend_status"] == "unknown"
    assert [l["outcome"] for l in out["legs"]] == ["amended", "unknown"]
    client.cancel_order_by_id.assert_not_called()


def test_the_shift_leg_record_is_durable_and_names_each_leg(tmp_path):
    from src.execution.exit_path_records import STOP_SHIFT_KIND, record_stop_shift_legs
    from src.storage.db import Database

    db = Database(str(tmp_path / "t.db"))
    db.initialize()
    legs = [
        {"id": "a", "qty": 12, "old_stop": 90.0, "new_stop": 89.5, "new_id": "a2", "outcome": "amended"},
        {"id": "b", "qty": 0.3456, "old_stop": 90.0, "new_stop": 89.5, "new_id": None, "outcome": "refused"},
    ]
    assert (
        record_stop_shift_legs(
            db, symbol="ZZZ", amount=0.5, mode="amend", status="partial", shifted=1, total=2, legs=legs, run_id="r1"
        )
        is True
    )
    rows = db.conn.execute("select evidence_json from specialist_evidence where kind=?", (STOP_SHIFT_KIND,)).fetchall()
    assert len(rows) == 1
    import json

    payload = json.loads(rows[0][0])
    assert payload["status"] == "partial" and payload["shifted"] == 1
    assert [l["outcome"] for l in payload["legs"]] == ["amended", "refused"]


@patch("src.execution.broker.TradingClient")
def test_a_dead_replacement_with_an_empty_book_is_reported_as_naked(mock_tc_cls):
    """Alpaca's replace is assumed to move the original to REPLACED first, so
    a dead replacement can mean there is NO stop at all. The code must read
    the book rather than assert that protection is intact."""
    b, client = _broker(mock_tc_cls)
    b._list_open_sell_stop_orders = MagicMock(return_value=[_plain_stop("s1", 340.0, qty=10)])
    client.replace_order_by_id.return_value = _replaced("s1b", "canceled")
    b._list_open_stop_orders_by_side = MagicMock(return_value=([], []))
    held = MagicMock()
    held.symbol = "GE"
    held.qty = 10
    client.get_all_positions.return_value = [held]

    out = b.shift_stops_down("GE", 0.51)

    assert out["status"] == "naked" and out["id"] is None
    assert out["legs"][0]["outcome"] == "naked"
    client.cancel_order_by_id.assert_not_called()


@patch("src.execution.broker.TradingClient")
def test_a_dead_replacement_whose_new_level_is_resting_counts_as_amended(mock_tc_cls):
    b, client = _broker(mock_tc_cls)
    b._list_open_sell_stop_orders = MagicMock(return_value=[_plain_stop("s1", 340.0, qty=10)])
    client.replace_order_by_id.return_value = _replaced("s1b", "canceled")
    b._list_open_stop_orders_by_side = MagicMock(return_value=([_plain_stop("s1c", 339.49, qty=10)], []))

    out = b.shift_stops_down("GE", 0.51)

    assert out["status"] == "accepted" and out["legs"][0]["new_id"] == "s1c"


@patch("src.execution.broker.TradingClient")
def test_a_dead_replacement_with_an_unreadable_book_is_unknown(mock_tc_cls):
    b, client = _broker(mock_tc_cls)
    b._list_open_sell_stop_orders = MagicMock(return_value=[_plain_stop("s1", 340.0, qty=10)])
    client.replace_order_by_id.return_value = _replaced("s1b", "rejected")
    b._list_open_stop_orders_by_side = MagicMock(side_effect=RuntimeError("no answer"))

    out = b.shift_stops_down("GE", 0.51)

    assert out["status"] == "unknown"


@patch("src.execution.broker.TradingClient")
def test_a_lagging_leg_is_retried_at_the_proposals_own_level(mock_tc_cls):
    """The only straddle worth healing is the one this amend just created, and
    the only level it may be healed to is the one the proposal asked for."""
    b, client = _broker(mock_tc_cls)
    orders = [_plain_stop("gtc", 90.0, qty=12), _plain_stop("day", 90.0, qty=0.3456)]
    specs = [
        {"id": "gtc", "qty": 12, "stop_price": 90.0, "limit_price": None},
        {"id": "day", "qty": 0.3456, "stop_price": 90.0, "limit_price": None},
    ]
    client.replace_order_by_id.side_effect = [
        _replaced("gtc2"),
        _ApiErr("busy", 422),
        _replaced("day2"),
    ]

    out = b._amend_resting_stop_price(
        symbol="ZZZ",
        live_orders=orders,
        stop_specs=specs,
        new_stop_price=95.0,
        position_qty=12.3456,
    )

    assert out["amend_status"] == "accepted"
    assert [c[0][1].stop_price for c in client.replace_order_by_id.call_args_list] == [95.0, 95.0, 95.0]
    client.cancel_order_by_id.assert_not_called()


@patch("src.execution.broker.TradingClient")
def test_a_leg_that_refuses_twice_is_left_straddled_not_collapsed(mock_tc_cls):
    """Per-lot levels are a design choice; pulling a laggard to another
    resting level would tighten a lot the desk chose to keep wide. Exactly one
    retry, then record and leave it."""
    b, client = _broker(mock_tc_cls)
    orders = [_plain_stop("gtc", 90.0, qty=12), _plain_stop("day", 90.0, qty=0.3456)]
    specs = [
        {"id": "gtc", "qty": 12, "stop_price": 90.0, "limit_price": None},
        {"id": "day", "qty": 0.3456, "stop_price": 90.0, "limit_price": None},
    ]
    client.replace_order_by_id.side_effect = [
        _replaced("gtc2"),
        _ApiErr("no", 422),
        _ApiErr("no", 422),
    ]

    out = b._amend_resting_stop_price(
        symbol="ZZZ",
        live_orders=orders,
        stop_specs=specs,
        new_stop_price=95.0,
        position_qty=12.3456,
    )

    assert out["amend_status"] == "partial" and out["id"] is None
    assert client.replace_order_by_id.call_count == 3
    client.cancel_order_by_id.assert_not_called()


@patch("src.execution.broker.TradingClient")
def test_an_unknown_leg_is_never_retried(mock_tc_cls):
    """The desk does not know where that stop is, so touching it again could
    move a stop it cannot see."""
    b, client = _broker(mock_tc_cls)
    orders = [_plain_stop("gtc", 90.0, qty=12), _plain_stop("day", 90.0, qty=0.3456)]
    specs = [
        {"id": "gtc", "qty": 12, "stop_price": 90.0, "limit_price": None},
        {"id": "day", "qty": 0.3456, "stop_price": 90.0, "limit_price": None},
    ]
    client.replace_order_by_id.side_effect = [_replaced("gtc2"), _ApiErr("timeout", 504)]

    out = b._amend_resting_stop_price(
        symbol="ZZZ",
        live_orders=orders,
        stop_specs=specs,
        new_stop_price=95.0,
        position_qty=12.3456,
    )

    assert out["amend_status"] == "unknown"
    assert client.replace_order_by_id.call_count == 2


@patch("src.execution.broker.TradingClient")
def test_an_empty_book_on_a_flat_position_is_not_called_naked(mock_tc_cls):
    """A replacement is also rejected when the original already triggered. The
    book is then empty because the position is gone, not unprotected."""
    b, client = _broker(mock_tc_cls)
    b._list_open_sell_stop_orders = MagicMock(return_value=[_plain_stop("s1", 340.0, qty=10)])
    client.replace_order_by_id.return_value = _replaced("s1b", "rejected")
    b._list_open_stop_orders_by_side = MagicMock(return_value=([], []))
    client.get_all_positions.return_value = []

    out = b.shift_stops_down("GE", 0.51)

    assert out["status"] == "flat"
    assert out["legs"][0]["outcome"] == "flat"
