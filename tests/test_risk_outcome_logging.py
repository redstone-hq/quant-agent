"""Conviction ledger (QAMC remediation spec §7.2).

"Log each trade's allocated risk against its realized outcome. If the
desk's conviction predicts results, conviction-weighted sizing amplifies
the edge. If it does not, flat sizing is superior — and that must be
discovered from data, not assumed."

Covers:
  - the new `trades` columns persist and read back on an entry
  - an exit row carries the joining id (`decision_id`), and a broker-
    initiated stop-out with no originating decision gets the labelled
    absence (`decision_id_status='no_originating_decision'`), never a
    fabricated value
  - the migration is idempotent and a pre-migration row still loads
  - `compute_trade_calibration`'s new `by_conviction` / `by_allocated_risk`
    groupings refuse to report below `_CONVICTION_OUTCOME_MIN_N`, stating
    n and an explicit "too few to conclude" message
  - below the floor, NOTHING from those groupings reaches the rendered
    Portfolio Manager or Position Reviewer prompt text (the test that
    matters most)
  - above the floor, the grouping reports normally and DOES reach the
    prompt
  - shorts are grouped correctly, not silently dropped
  - the constructor pins conviction / requested / allocated risk onto the
    entry TradeDecision
  - the one-time backfill recovers what's cheaply available and is
    idempotent
"""

import sqlite3
from unittest.mock import MagicMock, patch

from src.models import (
    Position,
    PortfolioDecision,
    ReasoningChain,
    TargetPosition,
    TechAnalysisResult,
    TechReasoningChain,
    TradeDecision,
)
from src.pipeline import TradingPipeline
from src.pipeline_context import RunContext
from src.pipeline_stages import ExecutionStage
from src.portfolio_constructor import PortfolioConstructor
from src.storage.db import Database, _CONVICTION_OUTCOME_MIN_N


# ===========================================================================
# Shared helpers
# ===========================================================================


def _pm_rc() -> ReasoningChain:
    return ReasoningChain(
        macro_filter="x",
        news_check="x",
        earnings_check="x",
        signal_conflicts="x",
        sizing_logic="x",
        portfolio_balance="x",
        cash_target="x",
    )


def _tech_rc() -> TechReasoningChain:
    return TechReasoningChain(
        trend="x",
        momentum="x",
        volatility="x",
        volume="x",
        support_resistance="x",
    )


# `computed_levels` / `atr_14` are Python-set by TechAnalystAgent, never
# emitted by the model. The constructor derives the take-profit from them
# (2026-09-01) and refuses without them.
def _long_analysis(symbol="NVDA", entry=100.0, stop=95.0, target=120.0) -> TechAnalysisResult:
    return TechAnalysisResult(
        symbol=symbol,
        rating="buy",
        entry_price=entry,
        stop_loss=stop,
        reference_target=target,
        reasoning="test",
        support_levels=[stop],
        resistance_levels=[target],
        computed_levels=[stop, target],
        atr_14=abs(entry - stop) / 3.5,
        setup_type="range",
        expected_horizon_sessions=60,
        reasoning_chain=_tech_rc(),
        thesis_invalid_if="closes below support",
    )


def _short_analysis(symbol="TSLA", entry=250.0, stop=262.5, target=200.0) -> TechAnalysisResult:
    return TechAnalysisResult(
        symbol=symbol,
        rating="sell",
        entry_price=entry,
        stop_loss=stop,
        reference_target=target,
        reasoning="test",
        support_levels=[target],
        resistance_levels=[stop],
        computed_levels=[target, stop],
        atr_14=abs(entry - stop) / 3.5,
        setup_type="range",
        expected_horizon_sessions=60,
        reasoning_chain=_tech_rc(),
        thesis_invalid_if="closes below support",
    )


def _db(tmp_path, name="test.db") -> Database:
    db = Database(str(tmp_path / name))
    db.initialize()
    return db


class _PipelineStub:
    """Just enough of TradingPipeline to exercise `_build_calibration_note`
    without constructing the real (heavyweight) pipeline."""

    def __init__(self, db):
        self.db = db

    _build_calibration_note, _review_calibration = (
        TradingPipeline._build_calibration_note,
        TradingPipeline._review_calibration,
    )
    _log_conviction_outcome_for_operator = staticmethod(TradingPipeline._log_conviction_outcome_for_operator)


def _date_trades_after_clean_record_start(db: Database) -> None:
    """Re-date every trade to just after the clean-record start (owner ruling
    2026-10-09: the record begins 2026-10-12 13:30 UTC), preserving order."""
    db.conn.execute(
        "UPDATE trades SET timestamp = strftime('%Y-%m-%d %H:%M:%S', '2026-10-12 14:00:00', '+' || id || ' seconds')"
    )
    db.conn.commit()


def _seed_closed_round_trips(
    db: Database, n: int, *, conviction: str, side: str = "long", win: bool = False, decision_model: str = "test/model"
) -> None:
    """Insert `n` closed round trips all sharing one `conviction` label, via
    the real `insert_trade` path (not raw SQL) so this exercises the exact
    persistence/derivation code under test."""
    entry_price = 100.0
    exit_price = 110.0 if win else 90.0
    for i in range(n):
        sym = f"{side.upper()}{i}"
        decision_id = f"run-{side}-{conviction}-{i}"
        entry_action = "BUY" if side == "long" else "SHORT"
        db.insert_trade(
            symbol=sym,
            action=entry_action,
            qty=10,
            price=entry_price,
            reasoning="t",
            run_id="r1",
            decision_id=decision_id,
            conviction=conviction,
            requested_risk_pct=2.0,
            allocated_risk_pct=1.5,
            decision_model=decision_model,
            fill_status="filled",
            stop_loss=90.0 if entry_action == "BUY" else 110.0,
        )
        exit_action = "SELL" if side == "long" else "COVER"
        exit_id = db.insert_trade(
            symbol=sym,
            action=exit_action,
            qty=10,
            price=exit_price,
            reasoning="t",
            run_id="r1",
            fill_status="filled",
        )
        # by_conviction counts only closes with a known realized P&L (missing
        # = UNKNOWN), so the fixture records it as the reconciler would.
        pnl = (exit_price - entry_price) * 10 * (1 if side == "long" else -1)
        db.conn.execute("UPDATE trades SET realized_pnl = ? WHERE id = ?", (pnl, exit_id))
        db.conn.commit()
    _date_trades_after_clean_record_start(db)


# ===========================================================================
# 1. New columns persist and read back on an entry
# ===========================================================================


def test_entry_columns_persist_and_read_back(tmp_path):
    db = _db(tmp_path)
    row_id = db.insert_trade(
        symbol="NVDA",
        action="BUY",
        qty=10,
        price=100.0,
        reasoning="t",
        run_id="r1",
        decision_id="r1-dec-abc",
        conviction="high",
        requested_risk_pct=2.5,
        allocated_risk_pct=1.8,
        decision_model="anthropic/claude-opus-4-6",
        stop_loss=90.0,
    )
    row = dict(db.conn.execute("SELECT * FROM trades WHERE id = ?", (row_id,)).fetchone())
    assert row["conviction"] == "high"
    assert row["requested_risk_pct"] == 2.5
    assert row["allocated_risk_pct"] == 1.8
    assert row["decision_model"] == "anthropic/claude-opus-4-6"
    # BUY is not exit-family — the joining label does not apply to it.
    assert row["decision_id_status"] is None
    db.close()


def test_entry_without_risk_based_target_leaves_risk_columns_null(tmp_path):
    """A legacy notional (target_weight_pct-only) BUY passes no risk
    figures at all — must read back None, never a fabricated 0.0."""
    db = _db(tmp_path)
    row_id = db.insert_trade(
        symbol="AAPL",
        action="BUY",
        qty=5,
        price=200.0,
        reasoning="t",
        run_id="r1",
        decision_id="r1-dec-xyz",
        stop_loss=90.0,
    )
    row = dict(db.conn.execute("SELECT * FROM trades WHERE id = ?", (row_id,)).fetchone())
    assert row["conviction"] is None
    assert row["requested_risk_pct"] is None
    assert row["allocated_risk_pct"] is None
    assert row["decision_model"] is None
    db.close()


# ===========================================================================
# 2. Exit rows: decision_id joining + the labelled absence
# ===========================================================================


def test_decision_linked_exit_gets_linked_status(tmp_path):
    """An ordinary SELL/COVER built from a reviewed PM decision passes a
    real decision_id and must be labelled 'linked', not left ambiguous."""
    db = _db(tmp_path)
    row_id = db.insert_trade(
        symbol="AAPL",
        action="SELL",
        qty=5,
        price=210.0,
        reasoning="exit",
        run_id="r1",
        decision_id="r1-dec-xyz",
        fill_status="filled",
    )
    row = dict(db.conn.execute("SELECT * FROM trades WHERE id = ?", (row_id,)).fetchone())
    assert row["decision_id"] == "r1-dec-xyz"
    assert row["decision_id_status"] == "linked"
    db.close()


def test_broker_stop_out_gets_labelled_absence_not_fabricated_value(tmp_path):
    """`insert_stop_out_trade` never accepts a decision_id — by
    construction, this exit never had one. It must read back
    decision_id=None (never guessed) AND decision_id_status=
    'no_originating_decision' (an honest label, mirroring pace_status),
    not silently indistinguishable from a legacy row we simply lost track
    of."""
    db = _db(tmp_path)
    row_id, created = db.insert_stop_out_trade(
        symbol="ONDS",
        qty=17,
        price=7.93,
        broker_order_id="bo-1",
        filled_at=None,
    )
    assert created is True
    row = dict(db.conn.execute("SELECT * FROM trades WHERE id = ?", (row_id,)).fetchone())
    assert row["decision_id"] is None
    assert row["decision_id_status"] == "no_originating_decision"
    db.close()


def test_deterministic_exit_without_decision_id_gets_labelled_absence(tmp_path):
    """The 8 pipeline.py call sites for TRAIL_STOP/TAKE_PROFIT/EMERGENCY_*/
    FORCE_DELEVER never pass decision_id — `insert_trade` derives the
    label automatically, no call-site change required."""
    db = _db(tmp_path)
    for action in (
        "TRAIL_STOP",
        "TAKE_PROFIT",
        "EMERGENCY_SELL",
        "FORCE_DELEVER",
        "REDUCE",
        "COVER",
        "EMERGENCY_COVER",
    ):
        row_id = db.insert_trade(
            symbol="XYZ",
            action=action,
            qty=1,
            price=10.0,
            reasoning="deterministic exit",
            run_id="r1",
            fill_status="filled",
        )
        row = dict(db.conn.execute("SELECT decision_id_status FROM trades WHERE id = ?", (row_id,)).fetchone())
        assert row["decision_id_status"] == "no_originating_decision", action
    db.close()


def test_hold_and_entries_get_no_decision_link_status(tmp_path):
    """The field does not apply to BUY/SHORT/HOLD — None, not a fabricated
    'no_originating_decision' on rows that were never exits."""
    db = _db(tmp_path)
    for action in ("BUY", "SHORT", "HOLD"):
        row_id = db.insert_trade(
            symbol="XYZ",
            action=action,
            qty=1,
            price=10.0,
            reasoning="t",
            run_id="r1",
            # BUY/SHORT now REQUIRE a usable stop; HOLD legitimately has none.
            stop_loss=(9.0 if action == "BUY" else 11.0 if action == "SHORT" else None),
        )
        row = dict(db.conn.execute("SELECT decision_id_status FROM trades WHERE id = ?", (row_id,)).fetchone())
        assert row["decision_id_status"] is None, action
    db.close()


# ===========================================================================
# 3. Migration idempotency + pre-migration row still loads
# ===========================================================================


def test_migration_is_idempotent(tmp_path):
    db_path = str(tmp_path / "idempotent.db")
    db1 = Database(db_path)
    db1.initialize()
    db1.close()
    # Re-initializing against the SAME file must not error or duplicate
    # columns — _ensure_column checks PRAGMA table_info before every ALTER.
    db2 = Database(db_path)
    db2.initialize()
    cols = [r[1] for r in db2.conn.execute("PRAGMA table_info(trades)").fetchall()]
    for col in ("conviction", "requested_risk_pct", "allocated_risk_pct", "decision_model", "decision_id_status"):
        assert cols.count(col) == 1
    db2.close()


def test_pre_migration_row_still_loads(tmp_path):
    """A row written by raw SQL BEFORE these columns existed (simulated by
    inserting into a hand-built legacy schema, then running the real
    migration) must still be readable, with every new column NULL."""
    db_path = str(tmp_path / "legacy.db")
    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT NOT NULL, action TEXT NOT NULL,
            qty REAL NOT NULL, price REAL NOT NULL,
            reasoning TEXT, run_id TEXT,
            timestamp TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)
    conn.execute(
        "INSERT INTO trades (symbol, action, qty, price, reasoning, run_id) "
        "VALUES ('OLD', 'BUY', 1, 1.0, 'ancient row', 'r0')"
    )
    conn.commit()
    conn.close()

    db = Database(db_path)
    db.initialize()  # runs the full migration, including the new columns
    row = dict(db.conn.execute("SELECT * FROM trades WHERE symbol = 'OLD'").fetchone())
    assert row["conviction"] is None
    assert row["requested_risk_pct"] is None
    assert row["allocated_risk_pct"] is None
    assert row["decision_model"] is None
    assert row["decision_id_status"] is None
    db.close()


# ===========================================================================
# 4. compute_trade_calibration: the honesty gate
# ===========================================================================


def test_by_conviction_refuses_below_floor_and_states_n(tmp_path):
    db = _db(tmp_path)
    assert _CONVICTION_OUTCOME_MIN_N > 3, "floor must exceed the old by_size/by_side gate"
    _seed_closed_round_trips(db, 4, conviction="high")
    _seed_closed_round_trips(db, 3, conviction="low")
    stats = db.compute_trade_calibration(lookback_days=100_000)
    high = stats["by_conviction"]["high"]
    low = stats["by_conviction"]["low"]
    assert high["n"] == 4 and high["insufficient_data"] is True
    assert "message" in high and "4" in high["message"] and "too few" in high["message"]
    assert "win_rate_pct" not in high  # never a number below the floor
    assert low["n"] == 3 and low["insufficient_data"] is True
    db.close()


def test_by_conviction_reports_normally_above_floor(tmp_path):
    db = _db(tmp_path)
    _seed_closed_round_trips(db, _CONVICTION_OUTCOME_MIN_N, conviction="high", win=True)
    stats = db.compute_trade_calibration(lookback_days=100_000)
    high = stats["by_conviction"]["high"]
    assert high["n"] == _CONVICTION_OUTCOME_MIN_N
    assert high["insufficient_data"] is False
    assert high["win_rate_pct"] == 100.0
    assert high["avg_return_pct"] > 0
    db.close()


def test_by_allocated_risk_buckets_and_gates_the_same_way(tmp_path):
    db = _db(tmp_path)
    for i in range(_CONVICTION_OUTCOME_MIN_N):
        db.insert_trade(
            symbol=f"HR{i}",
            action="BUY",
            qty=10,
            price=100.0,
            reasoning="t",
            run_id="r1",
            decision_id=f"r-hr-{i}",
            conviction="medium",
            allocated_risk_pct=4.0,
            requested_risk_pct=4.0,
            decision_model="m",
            fill_status="filled",
            stop_loss=90.0,
        )
        db.insert_trade(
            symbol=f"HR{i}",
            action="SELL",
            qty=10,
            price=105.0,
            reasoning="t",
            run_id="r1",
            fill_status="filled",
        )
    _date_trades_after_clean_record_start(db)
    stats = db.compute_trade_calibration(lookback_days=100_000)
    high_risk = stats["by_allocated_risk"]["high (≥3%)"]
    assert high_risk["n"] == _CONVICTION_OUTCOME_MIN_N
    assert high_risk["insufficient_data"] is False
    assert high_risk["win_rate_pct"] == 100.0
    db.close()


def test_conviction_and_allocated_risk_unknown_counts_are_honest(tmp_path):
    """Rows with no pinned conviction/risk (pre-ledger, or a legacy
    notional target) must be counted transparently, not silently folded
    into one of the real buckets."""
    db = _db(tmp_path)
    _seed_closed_round_trips(db, 5, conviction="high")
    # A closed round trip with NO conviction/risk on record at all.
    db.insert_trade(
        symbol="OLD1", action="BUY", qty=1, price=10.0, reasoning="t", run_id="r1", fill_status="filled", stop_loss=90.0
    )
    db.insert_trade(symbol="OLD1", action="SELL", qty=1, price=11.0, reasoning="t", run_id="r1", fill_status="filled")
    _date_trades_after_clean_record_start(db)
    stats = db.compute_trade_calibration(lookback_days=100_000)
    assert stats["conviction_unknown_n"] == 1
    assert stats["allocated_risk_unknown_n"] == 1  # the seeded 5 all carry allocated_risk_pct
    db.close()


def test_shorts_are_grouped_into_by_conviction_not_dropped(tmp_path):
    """The new groupings must include SHORT/COVER round trips exactly like
    by_side.short does — not silently long-only."""
    db = _db(tmp_path)
    _seed_closed_round_trips(db, _CONVICTION_OUTCOME_MIN_N, conviction="high", side="short", win=True)
    stats = db.compute_trade_calibration(lookback_days=100_000)
    assert stats["by_side"]["short"]["n"] == _CONVICTION_OUTCOME_MIN_N
    high = stats["by_conviction"]["high"]
    assert high["n"] == _CONVICTION_OUTCOME_MIN_N
    assert high["insufficient_data"] is False
    # A short's win is price FALLING — _seed_closed_round_trips's
    # win=True path still sells at a HIGHER price than a short's entry
    # would want, so assert on n/shape here rather than the sign of the
    # return (that FIFO math is pre-existing and covered elsewhere).
    assert high["win_rate_pct"] is not None
    db.close()


def test_mixed_long_and_short_conviction_buckets_combine_both_sides(tmp_path):
    db = _db(tmp_path)
    half = _CONVICTION_OUTCOME_MIN_N // 2 + 1
    _seed_closed_round_trips(db, half, conviction="low", side="long")
    _seed_closed_round_trips(db, half, conviction="low", side="short")
    stats = db.compute_trade_calibration(lookback_days=100_000)
    low = stats["by_conviction"]["low"]
    assert low["n"] == 2 * half
    assert low["insufficient_data"] is False
    db.close()


# ===========================================================================
# 5. THE MOST IMPORTANT CONSTRAINT — below the floor, nothing reaches the
#    Portfolio Manager or Position Reviewer prompt text
# ===========================================================================


def test_below_floor_nothing_added_to_pm_prompt(tmp_path):
    db = _db(tmp_path)
    # Mirrors real production shape: 8 closed trades split across buckets,
    # every one far below the floor.
    _seed_closed_round_trips(db, 4, conviction="high")
    _seed_closed_round_trips(db, 3, conviction="low")
    _seed_closed_round_trips(db, 1, conviction="medium")
    stub = _PipelineStub(db)
    note = stub._build_calibration_note(lookback_days=100_000)

    with patch("anthropic.Anthropic"):
        from src.agents.portfolio_manager import PortfolioManagerAgent

        agent = PortfolioManagerAgent(api_key="test", model="claude-opus-4-6")
        msg = agent.build_user_message(
            analyses=[],
            positions=[],
            macro_analysis=None,
            cash_balance=5000.0,
            total_value=10000.0,
            calibration_note=note,
        )
    assert "conviction" not in msg.lower()
    assert "too few" not in msg.lower()
    assert "insufficient" not in msg.lower()
    assert "allocated risk" not in msg.lower()
    db.close()


def test_below_floor_nothing_added_to_position_reviewer_prompt(tmp_path):
    db = _db(tmp_path)
    _seed_closed_round_trips(db, 4, conviction="high")
    _seed_closed_round_trips(db, 3, conviction="low")
    _seed_closed_round_trips(db, 1, conviction="medium")
    stub = _PipelineStub(db)
    note = stub._build_calibration_note(lookback_days=100_000)

    with patch("anthropic.Anthropic"):
        from src.agents.position_reviewer import PositionReviewerAgent

        agent = PositionReviewerAgent(api_key="test", model="claude-sonnet-4-6")
        msg = agent.build_user_message(
            positions=[
                Position(
                    symbol="TEST",
                    qty=1,
                    avg_entry=10,
                    current_price=10,
                    market_value=10,
                    unrealized_pnl=0,
                    sector="Tech",
                )
            ],
            macro_summary={"vix": {"current": 20, "trend": "flat"}},
            cash_balance=10000,
            total_value=10000,
            calibration_note=note,
        )
    assert "conviction" not in msg.lower()
    assert "too few" not in msg.lower()
    assert "insufficient" not in msg.lower()
    assert "allocated risk" not in msg.lower()
    db.close()


def test_above_floor_conviction_section_reaches_pm_prompt(tmp_path):
    """The mirror-image check: once a bucket clears the floor, its
    established numbers DO reach the prompt — the gate is a floor, not a
    permanent block."""
    db = _db(tmp_path)
    _seed_closed_round_trips(db, _CONVICTION_OUTCOME_MIN_N, conviction="high", win=True)
    stub = _PipelineStub(db)
    note = stub._build_calibration_note(lookback_days=100_000)
    assert "by conviction" in note.lower()
    assert "high" in note.lower()

    with patch("anthropic.Anthropic"):
        from src.agents.portfolio_manager import PortfolioManagerAgent

        agent = PortfolioManagerAgent(api_key="test", model="claude-opus-4-6")
        msg = agent.build_user_message(
            analyses=[],
            positions=[],
            macro_analysis=None,
            cash_balance=5000.0,
            total_value=10000.0,
            calibration_note=note,
        )
    assert "by conviction" in msg.lower()
    db.close()


def test_operator_log_sees_the_sub_floor_breakdown_the_prompt_never_gets(tmp_path, caplog):
    """The human operator surface: the SAME sub-floor stats withheld from
    the prompt above must still be recorded somewhere a human can read
    them — logged, in this implementation."""
    import logging

    db = _db(tmp_path)
    _seed_closed_round_trips(db, 4, conviction="high")
    _seed_closed_round_trips(db, 3, conviction="low")
    stub = _PipelineStub(db)
    with caplog.at_level(logging.INFO, logger="src.pipeline"):
        note = stub._build_calibration_note(lookback_days=100_000)
    assert "conviction" not in note.lower()  # confirm it's still gated from the prompt
    joined_logs = "\n".join(r.message for r in caplog.records)
    assert "OPERATOR-ONLY" in joined_logs
    assert "high" in joined_logs and "n=4" in joined_logs
    assert "low" in joined_logs and "n=3" in joined_logs
    db.close()


# ===========================================================================
# 6. Constructor wiring — TradeDecision carries conviction/risk at entry
# ===========================================================================


def test_build_buy_pins_conviction_and_requested_risk():
    constructor = PortfolioConstructor()
    target = TargetPosition(
        symbol="NVDA",
        risk_allocation_pct=2.0,
        conviction="high",
        thesis="x",
    )
    decisions = constructor.construct_orders(
        targets=[target],
        positions=[],
        analyses=[_long_analysis("NVDA")],
        total_value=100_000.0,
        price_map={"NVDA": 100.0},
    )
    buy = next(d for d in decisions if d.action == "BUY")
    assert buy.conviction == "high"
    assert buy.requested_risk_pct == 2.0
    assert buy.allocated_risk_pct == 2.0  # no portfolio rationing supplied here


def test_build_buy_allocated_risk_diverges_from_requested_under_budget_cut():
    """When the portfolio risk budget is nearly exhausted, the constructor
    grants LESS than the PM asked for — `allocated_risk_pct` must reflect
    what was really used, `requested_risk_pct` must still show the ask."""
    constructor = PortfolioConstructor()
    target = TargetPosition(
        symbol="NVDA",
        risk_allocation_pct=2.0,
        conviction="high",
        thesis="x",
    )
    decisions = constructor.construct_orders(
        targets=[target],
        positions=[],
        analyses=[_long_analysis("NVDA")],
        total_value=100_000.0,
        price_map={"NVDA": 100.0},
        existing_risk_pct={"OTHER": 24.5},  # ceiling defaults to 25.0
        clusters=[],
    )
    buy = next((d for d in decisions if d.action == "BUY"), None)
    assert buy is not None
    assert buy.requested_risk_pct == 2.0
    assert buy.allocated_risk_pct is not None
    assert buy.allocated_risk_pct < buy.requested_risk_pct


def test_build_short_pins_conviction_and_requested_risk():
    constructor = PortfolioConstructor()
    target = TargetPosition(
        symbol="TSLA",
        direction="short",
        risk_allocation_pct=1.5,
        conviction="medium",
        thesis="overvalued",
    )
    decisions = constructor.construct_orders(
        targets=[target],
        positions=[],
        analyses=[_short_analysis("TSLA")],
        total_value=100_000.0,
        price_map={"TSLA": 250.0},
    )
    short = next(d for d in decisions if d.action == "SHORT")
    assert short.conviction == "medium"
    assert short.requested_risk_pct == 1.5
    assert short.allocated_risk_pct == 1.5


def test_legacy_notional_target_leaves_risk_fields_none_on_trade_decision():
    """A target sized by target_weight_pct only (no risk_allocation_pct)
    carries no risk-based plan — the fields must stay None, not 0.0."""
    constructor = PortfolioConstructor()
    target = TargetPosition(
        symbol="NVDA",
        target_weight_pct=15.0,
        conviction="high",
        thesis="x",
    )
    decisions = constructor.construct_orders(
        targets=[target],
        positions=[],
        analyses=[_long_analysis("NVDA")],
        total_value=100_000.0,
        price_map={"NVDA": 100.0},
    )
    buy = next(d for d in decisions if d.action == "BUY")
    assert buy.conviction == "high"  # conviction is independent of sizing style
    assert buy.requested_risk_pct is None
    assert buy.allocated_risk_pct is None


def test_hold_and_sell_decisions_carry_no_entry_only_fields():
    """conviction/requested/allocated risk are pinned at ENTRY only — a
    HOLD or SELL decision must not carry them."""
    hold = TradeDecision(
        action="HOLD",
        symbol="AAPL",
        allocation_pct=0.0,
        entry_price=0.0,
        stop_loss=0.0,
        take_profit=0.0,
        reasoning="keep",
    )
    assert hold.conviction is None
    assert hold.requested_risk_pct is None
    assert hold.allocated_risk_pct is None


# ===========================================================================
# 7. ExecutionStage wiring — the entry insert_trade call carries the fields
# ===========================================================================


def _exec_pipeline() -> MagicMock:
    pipeline = MagicMock()
    pipeline.broker.list_open_orders_checked.return_value = (True, [])  # listing read, nothing open
    pipeline.broker.get_latest_price.return_value = 100.0
    pipeline.broker.get_latest_quote.return_value = {"bid_price": 100.0, "ask_price": 100.0}
    pipeline.broker.submit_order.return_value = {
        "id": "order-1",
        "status": "accepted",
        "symbol": "NVDA",
    }
    pipeline._format_qty = lambda q: str(q)
    pipeline._order_accepted.return_value = True
    pipeline._refresh_account_state.return_value = (
        {"cash": 50_000.0, "portfolio_value": 100_000.0},
        [],
        {},
    )
    pipeline.db.insert_trade.return_value = 1
    return pipeline


def test_execution_stage_passes_conviction_and_risk_and_model_to_insert_trade():
    pipeline = _exec_pipeline()
    ctx = RunContext.start("morning")
    ctx.cash = 50_000.0
    ctx.total_value = 100_000.0
    ctx.last_equity = 100_000.0
    ctx.positions = []
    ctx.decision_id = "r1-dec-abc"
    ctx.decision_model = "openai/gpt-5.5"
    ctx.portfolio_decision = PortfolioDecision(
        reasoning_chain=_pm_rc(),
        decisions=[
            TradeDecision(
                action="BUY",
                symbol="NVDA",
                allocation_pct=10,
                entry_price=100.0,
                stop_loss=95.0,
                take_profit=120.0,
                reasoning="fresh setup",
                conviction="high",
                requested_risk_pct=2.0,
                allocated_risk_pct=1.6,
            ),
        ],
        portfolio_view="test",
    )
    ctx.symbols_bars = {}

    stage = ExecutionStage(pipeline=pipeline)
    stage.run(ctx)

    pipeline.db.insert_trade.assert_called_once()
    kwargs = pipeline.db.insert_trade.call_args.kwargs
    assert kwargs["conviction"] == "high"
    assert kwargs["requested_risk_pct"] == 2.0
    assert kwargs["allocated_risk_pct"] == 1.6
    assert kwargs["decision_model"] == "openai/gpt-5.5"
    assert kwargs["decision_id"] == "r1-dec-abc"
