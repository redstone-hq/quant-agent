"""Sanity tests for PM's multi-layer memory: position history + L3 trajectories."""

from src.pipeline_risk_gate import RiskGate
from datetime import timedelta
import textwrap

import pytest
from unittest.mock import MagicMock, patch

from src.agents.portfolio_manager import PortfolioManagerAgent
from src.cost_circuit import PaidAnalysisSuspended
from src.data.macro_store import MacroStore
from src.data.news_store import NewsStore
from src.data.tech_store import TechStore
from src.models import Position
from src.util.time import et_today
from tests.pipeline_factory import build_pipeline


from tests.sector_run_helper import prun, tech_buy_analyses as _tech_buy_analyses  # noqa: E402


@pytest.fixture(autouse=True)
def _window_open_for_mechanics(monkeypatch):
    """These tests exercise grouping mechanics with clock-relative rows; the
    clean-record window itself is pinned in test_memory_windows_clean_record."""
    for mod in ("blocked", "exits"):
        m = f"src.prompt_facts.review.{mod}"
        monkeypatch.setattr(f"{m}.CLEAN_RECORD_DATE", "2000-01-01", raising=False)
        monkeypatch.setattr(f"{m}.CLEAN_RECORD_START_UTC", "2000-01-01T00:00:00", raising=False)


def _pos(symbol="NVDA"):
    return Position(
        symbol=symbol,
        qty=10,
        avg_entry=195,
        current_price=200,
        market_value=2000,
        unrealized_pnl=50,
        sector="Technology",
    )


def test_pm_user_message_renders_position_history_block():
    with patch("anthropic.Anthropic"):
        agent = PortfolioManagerAgent(api_key="test", model="claude-opus-4-6")
        msg = agent.build_user_message(
            analyses=[],
            positions=[_pos("NVDA")],
            macro_analysis=None,
            cash_balance=5000.0,
            total_value=10000.0,
            position_history={
                "NVDA": {
                    "entry_date": "2026-04-15",
                    "entry_price": 192.0,
                    "entry_reasoning": "AI capex supercycle + MACD bullish",
                    "days_held": 3,
                    "tech_history": [
                        {"date": "2026-04-15", "rating": "buy", "conviction": "high", "risk_reward": 2.8},
                        {"date": "2026-04-16", "rating": "buy", "conviction": "high", "risk_reward": 2.6},
                        {"date": "2026-04-17", "rating": "buy", "conviction": "medium", "risk_reward": 2.4},
                    ],
                },
            },
        )
        # Entry context surfaced
        assert "entry 2026-04-15" in msg
        assert "held 3d" in msg
        assert "AI capex supercycle" in msg
        # Tech history trail rendered
        assert "Tech history (last 3d):" in msg
        assert "buy(h)" in msg and "buy(m)" in msg


def test_pm_user_message_renders_weekly_narrative_and_trajectory():
    with patch("anthropic.Anthropic"):
        agent = PortfolioManagerAgent(api_key="test", model="claude-opus-4-6")
        msg = agent.build_user_message(
            analyses=[],
            positions=[],
            macro_analysis=None,
            cash_balance=5000.0,
            total_value=10000.0,
            weekly_narrative="- 2026-04-11: +0.8% (moderate) — Risk-on confirmed",
            macro_trajectory="- 2026-04-11: risk-on (medium) → target 75%",
            active_state_changes="- [2026-04-12] Iran ceasefire holds → XOM, CVX",
        )
        assert "## Portfolio Narrative (last 7 trading days)" in msg
        assert "Risk-on confirmed" in msg
        assert "## Macro Regime Trajectory (last 7 days)" in msg
        assert "risk-on (medium) → target 75%" in msg
        assert "## Active News State Changes" in msg
        assert "Iran ceasefire holds" in msg


def test_pm_gracefully_handles_missing_memory_layers():
    """When layers are empty, PM still produces a valid prompt with fallback text."""
    with patch("anthropic.Anthropic"):
        agent = PortfolioManagerAgent(api_key="test", model="claude-opus-4-6")
        msg = agent.build_user_message(
            analyses=[],
            positions=[],
            macro_analysis=None,
            cash_balance=0.0,
            total_value=0.0,
        )
        # The sections still appear but with fallback messages
        assert "No prior narrative yet" in msg
        assert "No prior snapshots yet" in msg
        assert "none surfaced" in msg
        # New self-calibration sections also have fallbacks
        assert "Risk Manager Verdicts" not in msg
        assert "no prior PM decisions on record" in msg
        assert "no projection available" in msg


def test_pm_renders_rm_verdicts_and_own_history():
    """PM sees RM's recent scale_all_buys + its own prior decisions."""
    with patch("anthropic.Anthropic"):
        agent = PortfolioManagerAgent(api_key="test", model="claude-opus-4-6")
        msg = agent.build_user_message(
            analyses=[],
            positions=[],
            macro_analysis=None,
            cash_balance=5000.0,
            total_value=10000.0,
            pm_recent_decisions=(
                "- 2026-04-16: BUY NVDA 12%; BUY AMD 10%\n"
                "    sizing: high conviction stacked on AI thesis\n"
                "- 2026-04-17: BUY GOOGL 12%; SELL AAPL 100%"
            ),
        )
        # PM's own decisions surfaced
        assert "## Your Recent Decisions" in msg
        assert "BUY NVDA 12%" in msg
        assert "high conviction stacked on AI thesis" in msg


def test_pm_renders_projected_book_preview():
    """PM sees sector concentration preview before writing decisions."""
    with patch("anthropic.Anthropic"):
        agent = PortfolioManagerAgent(api_key="test", model="claude-opus-4-6")
        msg = agent.build_user_message(
            analyses=[],
            positions=[],
            macro_analysis=None,
            cash_balance=5000.0,
            total_value=10000.0,
            projected_portfolio=(
                "- Current: 60% net invested · sectors: Technology 30%, Financial Services 20%\n"
                "- If you allocate 5% to each of 3 BUY-rated candidate(s) (NVDA, AMD, JPM):\n"
                "    → 75% net invested · sectors: Technology 40%, Financial Services 25%\n"
                "    ⚠ Sectors near/over 35% cap: Technology"
            ),
        )
        assert "## Projected Book Preview" in msg
        assert "Current: 60% net invested" in msg
        assert "Technology 40%" in msg
        assert "Sectors near/over 35% cap" in msg


# === Pipeline builder smoke tests ===


def test_handle_ex_dividends_lowers_stop_day_before(tmp_path):
    """Ex-div tomorrow → stop adjusted down by div amount. Idempotent same-day."""
    from datetime import timedelta
    from unittest.mock import MagicMock
    from src.pipeline import TradingPipeline
    from src.storage.db import Database
    from src.util.time import et_today

    db = Database(str(tmp_path / "t.db"))
    db.initialize()

    pipeline = build_pipeline(db=db, market=MagicMock(), broker=MagicMock())

    # Position: held JPM, current price $200, existing stop at $185
    jpm = Position(
        symbol="JPM",
        qty=50,
        avg_entry=180,
        current_price=200,
        market_value=10000,
        unrealized_pnl=1000,
        sector="Financial Services",
    )
    pipeline.market.get_upcoming_ex_dividend.return_value = {
        "date": et_today() + timedelta(days=1),  # tomorrow → adjust
        "amount": 1.20,
    }
    pipeline.broker.get_current_stop_price.return_value = 185.00
    # audit round 2: ex-div shifts EVERY stop down (preserving per-lot
    # levels) instead of a consolidating replace.
    pipeline.broker.shift_stops_down.return_value = {
        "id": "shift-JPM",
        "status": "accepted",
        "symbol": "JPM",
        "shifted": 1,
        "total": 1,
    }
    pipeline.broker.is_trading_day.side_effect = lambda d: d.weekday() < 5

    orders = pipeline._handle_ex_dividends([jpm], run_id="r1")
    assert len(orders) == 1
    # Every stop shifted down by the dividend
    args, _ = pipeline.broker.shift_stops_down.call_args
    assert args[0] == "JPM"
    assert args[1] == 1.20

    # Running again same day → idempotent (no broker call repeat)
    pipeline.broker.shift_stops_down.reset_mock()
    orders_2 = pipeline._handle_ex_dividends([jpm], run_id="r2")
    assert orders_2 == []
    pipeline.broker.shift_stops_down.assert_not_called()


def test_handle_ex_dividends_skips_when_ex_div_is_today(tmp_path):
    """Ex-div TODAY means the gap already happened — too late to adjust."""
    from unittest.mock import MagicMock
    from src.pipeline import TradingPipeline
    from src.storage.db import Database
    from src.util.time import et_today

    db = Database(str(tmp_path / "t.db"))
    db.initialize()
    pipeline = build_pipeline(db=db, market=MagicMock(), broker=MagicMock())

    p = Position(
        symbol="JPM",
        qty=50,
        avg_entry=180,
        current_price=200,
        market_value=10000,
        unrealized_pnl=1000,
        sector="Financial Services",
    )
    pipeline.market.get_upcoming_ex_dividend.return_value = {
        "date": et_today(),  # today → no action
        "amount": 1.20,
    }
    orders = pipeline._handle_ex_dividends([p], run_id="r1")
    assert orders == []
    pipeline.broker.replace_stop_loss.assert_not_called()


def test_run_intra_check_ok_when_within_loss_budget(tmp_path):
    """Intra check at -1% daily: no action, status 'ok'."""
    from unittest.mock import MagicMock
    from src.pipeline import TradingPipeline
    from src.storage.db import Database
    from src.risk.rules import RiskRuleEngine
    from src.config import RiskConfig

    db = Database(str(tmp_path / "t.db"))
    db.initialize()

    pipeline = build_pipeline(
        db=db,
        risk_engine=RiskRuleEngine(
            RiskConfig(
                max_position_pct=20,
                max_total_position_pct=90,
                require_stop_loss=False,
            )
        ),
        broker=MagicMock(),
    )
    pipeline.broker.is_trading_day.return_value = True
    pipeline.broker.get_account.return_value = {
        "portfolio_value": 99_000.0,
        "last_equity": 100_000.0,
        "cash": 10_000.0,
    }
    pipeline.broker.get_positions.return_value = []
    pipeline.cost_circuit = MagicMock()
    pipeline.cost_circuit.activate_session.return_value = {"suspended": True}
    pipeline.cost_circuit.require_paid_analysis.side_effect = PaidAnalysisSuspended(
        "prelatched",
        {"suspended": True},
    )
    pipeline._run_intraday_opportunity_scan = MagicMock(
        side_effect=lambda _ctx: pipeline._require_paid_analysis("intraday_tech_analyst")
    )

    result = pipeline.run_intra_check()
    assert result["status"] == "ok"
    assert result["intraday_scan"]["status"] == "paid_analysis_suspended"
    pipeline.broker.submit_order.assert_not_called()
    pipeline.cost_circuit.activate_session.assert_called_once()
    # The gate now names the seat's model when it is a $0 route, so the
    # circuit can admit it under a money-cap-only suspension (free-route rule).
    pipeline.cost_circuit.require_paid_analysis.assert_called_once()
    assert pipeline.cost_circuit.require_paid_analysis.call_args.args == ("intraday_tech_analyst",)


def test_vol_adjusted_sizing_caps_qty_on_wide_stops():
    """SQQQ-style wide-stop name gets fewer shares than raw allocation would
    suggest — but only when the geometry is genuinely extreme, not merely
    because of a stale hardcoded percentage (item 22, fixed 2026-09-03).

    This exercises the real `_qty_by_risk_budget` helper (not a re-derived
    copy of its arithmetic) against a stub pipeline whose
    `config.risk.max_position_risk_pct` is the ratified 5.0 — the same value
    `TradingPipeline.__init__` reads for the constructor's own sizing, so
    this independent recheck agrees with it by construction rather than by
    coincidence.
    """
    from types import SimpleNamespace
    from src.pipeline_stages import _qty_by_risk_budget

    pipeline = SimpleNamespace(
        config=SimpleNamespace(risk=SimpleNamespace(max_position_risk_pct=5.0)),
    )
    total_value = 100_000
    entry = 50.0
    alloc_pct = 5.0
    qty_by_alloc = int((total_value * alloc_pct / 100) / entry)  # 100

    def qty_by_risk(stop_price):
        return _qty_by_risk_budget(
            pipeline,
            total_value=total_value,
            sizing_price=entry,
            stop_price=stop_price,
            is_short=False,
            fractional=False,
        )

    # Pre-fix regression case: an 8% stop used to sit right at the edge of
    # the stale 0.5% budget. Under the real 5% envelope it isn't close —
    # alloc is still the binding constraint, and the trade is NOT silently
    # shrunk the way it used to be.
    wide_stop = 46.0  # 8% below entry
    assert min(qty_by_alloc, qty_by_risk(wide_stop)) == 100

    # A tighter, narrow-stop scenario: risk budget allows far more shares
    # than the allocation asks for; alloc is still the binding constraint.
    narrow_stop = 49.0  # 2% below entry
    assert min(qty_by_alloc, qty_by_risk(narrow_stop)) == 100

    # The mechanism is still a genuine independent safety recheck, not a
    # rubber stamp. At MATCHED percentages (alloc == risk budget, both 5%)
    # the risk leg can never bind on a valid long — its stop must sit
    # strictly between 0 and entry, so risk-per-share can never exceed
    # entry, and the two arithmetically cross only when risk-per-share
    # exceeds entry. That is the correctly-sized common case this fix
    # restores (both cases above). The recheck's real job is catching
    # geometry that drifts AFTER allocation was decided — e.g. a much
    # larger allocation (high conviction) paired with an abnormally wide
    # stop: 20% allocation = 400 shares, but a $20/share stop against the
    # 5%-of-equity budget ($5,000) allows only 250 — the independent
    # recheck still caps it below what allocation alone would buy.
    large_alloc_qty = int((total_value * 20.0 / 100) / entry)  # 400
    wide_stop_for_large_alloc = entry - 20.0  # $20/share risk
    assert (
        min(
            large_alloc_qty,
            qty_by_risk(wide_stop_for_large_alloc),
        )
        == 250
    )


def test_pm_memory_builders_warn_on_corrupt_json(caplog):
    """Each of the 4 PM memory builders that parse stored JSON must log
    a WARN on parse failure, not silently swallow.

    Pre-fix: a single corrupt row in insights / agent_logs vanished
    from PM's view with no log line, leaving operator unaware that PM
    saw fewer lessons / verdicts than the DB actually held. The fix
    surfaces every parse failure so a recurring corruption pattern
    (e.g. truncated rows from a kill-mid-write) is visible in logs.
    """
    import logging
    from unittest.mock import MagicMock
    from src.pipeline import TradingPipeline

    pipeline = build_pipeline(db=MagicMock())

    corrupt_insights_row = {
        "date": "2026-05-10",
        "missed_opportunities_json": "{not valid json",
        "buy_grades_json": "[also not valid",
    }
    corrupt_agent_row = {
        "timestamp": "2026-05-10T20:30:00",
        "full_response": "{also bad",
    }
    pipeline.db.get_recent_insights.return_value = [corrupt_insights_row]
    pipeline.db.get_recent_agent_outputs.return_value = [corrupt_agent_row]

    with caplog.at_level(logging.WARNING, logger="src.pipeline"):
        pipeline._build_recent_missed_lessons()
        pipeline._build_recent_loss_pits()
        pipeline._build_pm_recent_decisions(limit=3)

    warns = [r.message for r in caplog.records if r.levelno == logging.WARNING]
    # All 4 builders should have surfaced the parse failure.
    assert any("recent_missed_lessons" in m for m in warns), warns
    assert any("recent_loss_pits" in m for m in warns), warns
    assert any("pm_recent_decisions" in m for m in warns), warns


def test_build_recent_sells_joins_current_prices(tmp_path):
    """_build_recent_sells_for_grading pulls SELL trades and computes pct move."""
    from unittest.mock import MagicMock
    from src.pipeline import TradingPipeline
    from src.storage.db import Database

    db = Database(str(tmp_path / "t.db"))
    db.initialize()
    # Insert a SELL trade 1 day ago
    db.insert_trade("NVDA", "SELL", 10, 500.0, "thesis break", "r1")
    db.conn.execute("UPDATE trades SET timestamp = datetime('now', '-1 day') WHERE symbol='NVDA'")
    db.conn.commit()
    # Old trade (5 days) — should NOT appear (outside 2d window)
    db.insert_trade("AAPL", "SELL", 10, 200.0, "old", "r2")
    db.conn.execute("UPDATE trades SET timestamp = datetime('now', '-5 days') WHERE symbol='AAPL'")
    db.conn.commit()

    # Mock broker to return a specific current price
    pipeline = build_pipeline(db=db, broker=MagicMock())
    pipeline.broker.get_latest_price = MagicMock(return_value=530.0)

    out = pipeline._build_recent_sells_for_grading(lookback_days=2)
    assert len(out) == 1
    row = out[0]
    assert row["symbol"] == "NVDA"
    assert row["sell_price"] == 500.0
    assert row["current_price"] == 530.0
    # (530/500 - 1) * 100 = 6.0 — stock moved UP after we sold → premature exit
    assert row["pct_move_since_sell"] == 6.0


def test_build_recent_sells_includes_reduce_action(tmp_path):
    """Midday reviewer trims are persisted with action='REDUCE'. They must
    flow into sell_grades_json so evening grading covers partial exits, not
    just full SELLs."""
    from unittest.mock import MagicMock
    from src.pipeline import TradingPipeline
    from src.storage.db import Database

    db = Database(str(tmp_path / "t.db"))
    db.initialize()
    db.insert_trade("GOOGL", "REDUCE", 5, 320.0, "trim half", "r1")
    db.conn.execute("UPDATE trades SET timestamp = datetime('now', '-1 day') WHERE symbol='GOOGL'")
    db.conn.commit()

    pipeline = build_pipeline(db=db, broker=MagicMock())
    pipeline.broker.get_latest_price = MagicMock(return_value=327.0)

    out = pipeline._build_recent_sells_for_grading(lookback_days=2)
    assert len(out) == 1
    assert out[0]["symbol"] == "GOOGL"
    assert out[0]["sell_price"] == 320.0


def test_pm_renders_structured_evening_tilt():
    """PM surfaces tomorrow_bias + conviction + key_risks from evening insights."""
    import json

    with patch("anthropic.Anthropic"):
        agent = PortfolioManagerAgent(api_key="test", model="claude-opus-4-6")
        msg = agent.build_user_message(
            analyses=[],
            positions=[],
            macro_analysis=None,
            cash_balance=5000.0,
            total_value=10000.0,
            yesterday_insights={
                "date": "2026-04-17",
                "tomorrow_outlook": "Defensive bias; FOMC minutes risk.",
                "lessons": "Don't chase.",
                "risk_rating": "elevated",
                "suggested_actions": ["Raise cash to 25%"],
                "tomorrow_bias": "bearish",
                "tomorrow_conviction": "high",
                "tomorrow_key_risks": json.dumps(["FOMC minutes 2pm", "VIX > 20"]),
            },
        )
        assert "bias=bearish" in msg
        assert "conviction=high" in msg
        assert "FOMC minutes 2pm" in msg
        assert "VIX > 20" in msg


def test_evening_report_parses_structured_fields():
    """EveningReport accepts and defaults the new structured fields."""
    from src.models import EveningReport, EveningReasoningChain

    rc = EveningReasoningChain(
        performance_attribution="x",
        outlook_retrospection="x",
        thesis_health_review="x",
        decision_quality_review="x",
        calibration_meta="x",
        market_regime_read="x",
        tomorrow_preparation="x",
    )
    # Defaults kick in when optional fields are omitted
    r = EveningReport(
        reasoning_chain=rc,
        daily_summary="x",
        lessons="x",
        tomorrow_outlook="x",
        risk_rating="low",
    )
    assert r.tomorrow_bias == "neutral"
    assert r.tomorrow_conviction == "medium"
    assert r.tomorrow_key_risks == []
    assert r.sell_grades == []
    assert r.buy_grades == []

    # Explicit values parse
    r2 = EveningReport(
        reasoning_chain=rc,
        daily_summary="x",
        lessons="x",
        tomorrow_outlook="x",
        risk_rating="high",
        tomorrow_bias="bearish",
        tomorrow_conviction="high",
        tomorrow_key_risks=["FOMC", "NVDA earnings"],
    )
    assert r2.tomorrow_bias == "bearish"
    assert r2.tomorrow_key_risks == ["FOMC", "NVDA earnings"]


def test_save_and_load_insights_roundtrip_structured_fields(tmp_path):
    """db.save_insights persists the new columns; get_recent_insights returns them."""
    from src.storage.db import Database
    import json

    db = Database(str(tmp_path / "t.db"))
    db.initialize()
    db.save_insights(
        date="2026-04-17",
        tomorrow_outlook="Watch FOMC.",
        lessons="Don't chase.",
        suggested_actions=["raise cash"],
        risk_rating="elevated",
        tomorrow_bias="bearish",
        tomorrow_conviction="high",
        tomorrow_key_risks=["FOMC 2pm", "VIX > 20"],
    )
    rows = db.get_recent_insights(limit=1)
    assert len(rows) == 1
    row = rows[0]
    assert row["tomorrow_bias"] == "bearish"
    assert row["tomorrow_conviction"] == "high"
    assert json.loads(row["tomorrow_key_risks"]) == ["FOMC 2pm", "VIX > 20"]


def test_risk_verdict_accepts_reason_category():
    """RiskVerdict parses the new reason_category enum; default is 'clean'."""
    from src.models import RiskVerdict, RiskReasoningChain

    def _rc():
        return RiskReasoningChain(
            rr_audit="x",
            signal_fidelity="x",
            correlation_check="x",
            event_risk="x",
            sizing_sanity="x",
            overall="x",
        )

    # Default when field omitted
    v1 = RiskVerdict(approved=True, reasoning_chain=_rc(), reasoning="fine")
    assert v1.reason_category == "clean"

    # All enum values parse
    for cat in (
        "oversized",
        "rr_fail",
        "concentration",
        "correlation_risk",
        "event_risk",
        "macro_misalign",
        "data_degraded",
        "signal_fidelity",
        "other",
        "clean",
    ):
        v = RiskVerdict(approved=True, reasoning_chain=_rc(), reasoning="x", reason_category=cat)
        assert v.reason_category == cat

    # Unknown category rejected
    import pytest

    with pytest.raises(Exception):  # pydantic ValidationError
        RiskVerdict(approved=True, reasoning_chain=_rc(), reasoning="x", reason_category="weird")


def test_pm_decisions_builder_parses_own_history(tmp_path):
    import json
    from src.pipeline import TradingPipeline
    from src.storage.db import Database

    db = Database(str(tmp_path / "t.db"))
    db.initialize()
    db.insert_agent_log(
        agent_name="portfolio_manager",
        run_id="p1",
        input_summary="",
        input_message="",
        output_summary="1 trade",
        full_response=json.dumps(
            {
                "reasoning_chain": {
                    "sizing_logic": "high conviction on AI capex",
                    "continuity_check": "consistent with 5-day risk-on narrative",
                },
                "decisions": [
                    {
                        "action": "BUY",
                        "symbol": "NVDA",
                        "allocation_pct": 8.0,
                        "entry_price": 195,
                        "stop_loss": 186,
                        "take_profit": 215,
                        "reasoning": "...",
                    },
                ],
            }
        ),
        model="gpt-5.4",
        tokens_used=100,
    )
    db.conn.execute(
        "UPDATE agent_logs SET timestamp = datetime('now', '-1 day') WHERE agent_name = 'portfolio_manager'"
    )
    db.conn.commit()

    pipeline = build_pipeline(db=db)
    out = pipeline._build_pm_recent_decisions(limit=3)
    assert "BUY NVDA 8.0%" in out
    assert "high conviction on AI capex" in out
    assert "consistent with 5-day risk-on narrative" in out


def test_pm_decisions_builder_tags_the_unit_on_targets_schema(tmp_path):
    """Phase 2's `targets` schema sizes by `risk_allocation_pct`; older rows
    carry the legacy `target_weight_pct`. Reading only the legacy field
    rendered every current size as '?' (live output stopped emitting
    `target_weight_pct` on 2026-08-28) -- a flip-flop check that cannot see
    the size is not a check. Fixed 2026-09-02; this test is the regression
    guard the fix shipped without. Pins all three branches: risk-tagged,
    legacy-weight-tagged, and the true-unknown fallback when neither field
    is present.
    """
    import json
    from src.pipeline import TradingPipeline
    from src.storage.db import Database

    db = Database(str(tmp_path / "t.db"))
    db.initialize()
    db.insert_agent_log(
        agent_name="portfolio_manager",
        run_id="p1",
        input_summary="",
        input_message="",
        output_summary="targets",
        full_response=json.dumps(
            {
                "targets": [
                    {"symbol": "NVDA", "risk_allocation_pct": 1.2, "conviction": "high"},
                    {"symbol": "JPM", "target_weight_pct": 5.0, "conviction": "medium"},
                    {"symbol": "XLE", "conviction": "low"},
                ],
            }
        ),
        model="gpt-5.4",
        tokens_used=100,
    )
    db.conn.execute(
        "UPDATE agent_logs SET timestamp = datetime('now', '-1 day') WHERE agent_name = 'portfolio_manager'"
    )
    db.conn.commit()

    pipeline = build_pipeline(db=db)
    out = pipeline._build_pm_recent_decisions(limit=3)
    assert "NVDA→1.2%r(h)" in out
    assert "JPM→5.0%w(m)" in out
    assert "XLE→?(l)" in out


def test_pm_renders_weight_pct():
    """Each position line shows weight_pct; drift flag appears on concentrated winners."""
    with patch("anthropic.Anthropic"):
        agent = PortfolioManagerAgent(api_key="test", model="claude-opus-4-6")
        # 15% weight + 25% P&L → drifted concentration
        drifted = Position(
            symbol="NVDA",
            qty=10,
            avg_entry=100,
            current_price=150,
            market_value=1500,
            unrealized_pnl=500,
            sector="Technology",
        )
        # 14% weight but only 2% P&L → NOT drift (was sized that way)
        big_but_not_drift = Position(
            symbol="MSFT",
            qty=10,
            avg_entry=140,
            current_price=140,
            market_value=1400,
            unrealized_pnl=28,
            sector="Technology",
        )
        # 6% normal
        small = Position(
            symbol="JPM",
            qty=5,
            avg_entry=120,
            current_price=120,
            market_value=600,
            unrealized_pnl=0,
            sector="Financial Services",
        )
        msg = agent.build_user_message(
            analyses=[],
            positions=[drifted, big_but_not_drift, small],
            macro_analysis=None,
            cash_balance=6500.0,
            total_value=10000.0,
        )
        assert "Weight: 15.0%" in msg
        assert "Weight: 14.0%" in msg
        assert "Weight: 6.0%" in msg
        # The drift flag was deleted 2026-10-09: no line carries it.
        assert "DRIFT" not in msg


def test_queued_earnings_buys_are_refused_not_capped():
    from src.models import TradeDecision
    from src.pipeline import TradingPipeline

    decisions = [
        TradeDecision(
            action="BUY",
            symbol="NVDA",
            allocation_pct=12.0,
            entry_price=100,
            stop_loss=95,
            take_profit=110,
            reasoning="high conviction",
        ),
        TradeDecision(
            action="BUY",
            symbol="MSFT",
            allocation_pct=8.0,
            entry_price=400,
            stop_loss=380,
            take_profit=430,
            reasoning="moderate",
        ),
        TradeDecision(
            action="SELL",
            symbol="AAPL",
            allocation_pct=100,
            entry_price=200,
            stop_loss=0,
            take_profit=0,
            reasoning="exit",
        ),
    ]
    # NVDA has a just-filed 10-Q with no analysis yet; MSFT is fully analyzed
    earnings_results = [
        {"symbol": "NVDA", "queued": True, "analysis": None, "form_type": "10-Q", "filing_date": "2026-04-18"},
        {"symbol": "MSFT", "queued": False, "analysis": {"investment_implications": {}}},
    ]
    out = RiskGate._refuse_queued_earnings_buys(decisions, earnings_results)
    msft = next(d for d in out if d.symbol == "MSFT")
    aapl = next(d for d in out if d.symbol == "AAPL")
    # Board item 186 (2026-10-01): an unread filing is an UNCONVICTED earnings
    # seat, and doctrine refuses an entry the seats are not right about, so the
    # BUY is gone rather than shrunk to a 5%-of-book figure nothing sourced.
    assert [d.symbol for d in out if d.action == "BUY"] == ["MSFT"]
    assert msft.allocation_pct == 8.0  # untouched (no queued flag)
    assert aapl.allocation_pct == 100  # SELL untouched


def test_trade_calibration_matches_fifo_and_buckets(tmp_path):
    """BUYs → SELLs are FIFO matched; win rate + avg return + buckets reported."""
    from src.storage.db import Database

    db = Database(str(tmp_path / "t.db"))
    db.initialize()

    # Large winner: buy 100 @ 100 (entry = $10k), sell 100 @ 115 → +15%
    db.insert_trade("NVDA", "BUY", 100, 100, "large", "r1", stop_loss=90.0)
    db.conn.execute(
        "UPDATE trades SET timestamp = datetime('2026-11-05 14:00:00', '-15 days') WHERE symbol='NVDA' AND action='BUY'"
    )
    db.conn.commit()
    db.insert_trade("NVDA", "SELL", 100, 115, "exit", "r2")
    db.conn.execute(
        "UPDATE trades SET timestamp = datetime('2026-11-05 14:00:00', '-5 days') WHERE symbol='NVDA' AND action='SELL'"
    )
    db.conn.commit()

    # Medium loser: buy 100 @ 60 (entry = $6k), sell 100 @ 54 → -10%
    db.insert_trade("XOM", "BUY", 100, 60, "medium", "r3", stop_loss=90.0)
    db.conn.execute(
        "UPDATE trades SET timestamp = datetime('2026-11-05 14:00:00', '-20 days') WHERE symbol='XOM' AND action='BUY'"
    )
    db.conn.commit()
    db.insert_trade("XOM", "SELL", 100, 54, "stop", "r4")
    db.conn.execute(
        "UPDATE trades SET timestamp = datetime('2026-11-05 14:00:00', '-10 days') WHERE symbol='XOM' AND action='SELL'"
    )
    db.conn.commit()

    # Small winner: buy 10 @ 200 (entry = $2k), sell 10 @ 220 → +10%
    db.insert_trade("JPM", "BUY", 10, 200, "small", "r5", stop_loss=90.0)
    db.conn.execute(
        "UPDATE trades SET timestamp = datetime('2026-11-05 14:00:00', '-8 days') WHERE symbol='JPM' AND action='BUY'"
    )
    db.conn.commit()
    db.insert_trade("JPM", "SELL", 10, 220, "target", "r6")
    db.conn.execute(
        "UPDATE trades SET timestamp = datetime('2026-11-05 14:00:00', '-1 day') WHERE symbol='JPM' AND action='SELL'"
    )
    db.conn.commit()

    stats = db.compute_trade_calibration(lookback_days=45)
    # 3 closed trades, 2 winners → 66.7% win rate
    assert stats["n"] == 3
    assert stats["win_rate_pct"] == 66.7
    # Avg return = (15 + -10 + 10) / 3 = 5.0
    assert abs(stats["avg_return_pct"] - 5.0) < 0.01
    # Buckets
    by_size = stats["by_size"]
    assert by_size["large (≥$10k)"]["n"] == 1
    assert by_size["large (≥$10k)"]["avg_return_pct"] == 15.0
    assert by_size["medium ($5-10k)"]["n"] == 1
    assert by_size["small (<$5k)"]["n"] == 1


def test_trade_calibration_returns_empty_below_threshold(tmp_path):
    """With <3 closed trades, stats are {} to avoid misleading PM on noise."""
    from src.storage.db import Database

    db = Database(str(tmp_path / "t.db"))
    db.initialize()
    db.insert_trade("NVDA", "BUY", 10, 100, "x", "r1", stop_loss=90.0)
    db.insert_trade("NVDA", "SELL", 10, 105, "x", "r2")
    # Clean-record start (owner ruling 2026-10-09): date the rows after it, in order.
    db.conn.execute("UPDATE trades SET timestamp = datetime('2026-10-13 14:00:00', '+' || id || ' seconds')")
    db.conn.commit()
    stats = db.compute_trade_calibration(lookback_days=45)
    assert stats == {}


def test_pm_renders_calibration_section():
    """PM prompt shows the calibration note when provided."""
    with patch("anthropic.Anthropic"):
        agent = PortfolioManagerAgent(api_key="test", model="claude-opus-4-6")
        msg = agent.build_user_message(
            analyses=[],
            positions=[],
            macro_analysis=None,
            cash_balance=5000.0,
            total_value=10000.0,
            calibration_note=("- Overall (last 45d): 12 closed BUYs, win rate 58%, avg return +3.20%, avg hold 6.4d"),
        )
        assert "## Trade Calibration" in msg
        assert "win rate 58%" in msg
        assert "avg return +3.20%" in msg


def test_earnings_record_failure_abandons_after_max_attempts(tmp_path):
    """Third failure flips `abandoned=True` and returns True from record_failure."""
    from src.data.earnings import EarningsDataProvider, EarningsReport

    provider = EarningsDataProvider(data_dir=str(tmp_path / "earnings"))
    report = EarningsReport(
        symbol="NVDA",
        form_type="10-Q",
        filing_date="2026-04-15",
        filing_path="",
        analysis_path=None,
        text_excerpt="",
        is_new=True,
    )

    # Attempt 1 + 2: not abandoned yet
    assert provider.record_failure(report, max_attempts=3) is False
    assert provider.record_failure(report, max_attempts=3) is False
    entry_before = provider.manifest["NVDA_10-Q"]
    assert entry_before["failed_attempts"] == 2
    assert entry_before.get("abandoned") is not True

    # Attempt 3 triggers abandonment
    assert provider.record_failure(report, max_attempts=3) is True
    entry_after = provider.manifest["NVDA_10-Q"]
    assert entry_after["failed_attempts"] == 3
    assert entry_after["abandoned"] is True

    # confirm_filing resets the counter (so a successful retry wipes the state)
    report2 = EarningsReport(
        symbol="NVDA",
        form_type="10-Q",
        filing_date="2026-04-15",
        filing_path="/path/to/10q.html",
        analysis_path="/path/to/analysis.md",
        text_excerpt="...",
        is_new=True,
    )
    provider.confirm_filing(report2)
    assert provider.manifest["NVDA_10-Q"]["failed_attempts"] == 0
    assert (
        "abandoned" not in provider.manifest["NVDA_10-Q"] or provider.manifest["NVDA_10-Q"].get("abandoned") is not True
    )


def test_queued_earnings_refusal_noop_when_nothing_queued():
    from src.models import TradeDecision
    from src.pipeline import TradingPipeline

    decisions = [
        TradeDecision(
            action="BUY",
            symbol="NVDA",
            allocation_pct=12.0,
            entry_price=100,
            stop_loss=95,
            take_profit=110,
            reasoning="x",
        ),
    ]
    # Only fully-analyzed entries
    earnings_results = [{"symbol": "NVDA", "queued": False, "analysis": {"investment_implications": {}}}]
    out = RiskGate._refuse_queued_earnings_buys(decisions, earnings_results)
    assert out[0].allocation_pct == 12.0


def _projection_pipeline(target_pct: float | None = None):
    """A bare pipeline for `_build_projected_portfolio`.

    Spec §12.2/§12.3: the overweight threshold is read from the risk engine's
    `max_sector_pct` instead of the hardcoded `35` this preview used to carry
    — a fourth sector number, unrelated to config and already stale against
    the 40 it was shadowing. Passing `target_pct=None` leaves no risk engine
    at all, which is how the fallback is exercised.
    """
    from types import SimpleNamespace
    from src.pipeline import TradingPipeline

    pipeline = build_pipeline()
    prun(pipeline).symbol_sectors = {"NVDA": "Technology", "AMD": "Technology", "AAPL": "Technology"}
    if target_pct is not None:
        pipeline.risk_engine = SimpleNamespace(
            config=SimpleNamespace(max_sector_pct=target_pct),
        )
    return pipeline


def test_projected_portfolio_does_not_warn_below_the_configured_target(tmp_path):
    """With the production §12.3 target of 75 and candidates whose own stops
    keep them small, nothing is flagged — the preview must draw the line the
    rest of the system draws, not one of its own.

    REWRITTEN for item 221. The threshold is now applied to the HELD book
    (30% Tech, under 75), which is measured, instead of to a projected book,
    which cannot be computed before the PM writes its targets. The wide
    stops here (entry 100 / stop 50) imply a 10% ceiling per name — a cap on
    one name, never summed into a projected mix.
    """
    pipeline = _projection_pipeline(target_pct=75.0)
    positions = [
        Position(
            symbol="MSFT",
            qty=10,
            avg_entry=400,
            current_price=400,
            market_value=3000,
            unrealized_pnl=0,
            sector="Technology",
        ),
    ]
    wide = {"NVDA": 50.0, "AMD": 50.0, "AAPL": 50.0}
    with patch("src.execution.broker._get_sector"):
        out = pipeline._build_projected_portfolio(
            positions, _tech_buy_analyses(wide), total_value=10000, run=prun(pipeline)
        )
    assert "Technology long 30%" in out
    assert "concentration target" not in out
    # Wide stops imply small ceilings: 5% risk budget x 100/50 = 10%.
    assert _preview_ceilings(out) == {"NVDA": 10.0, "AMD": 10.0, "AAPL": 10.0}
    # Each sector's share OF THE CANDIDATE SET: measured, not projected.
    assert "Technology 3 of 3 (100% of the candidate set" in out


def test_projected_portfolio_short_does_not_shrink_the_long_side(tmp_path):
    """Spec §12.2. A held SHORT used to be summed SIGNED into its sector, so
    it made the LONG side look smaller in the very preview whose job is to
    surface concentration.

    Here 30% held Tech LONG and 20% held Tech SHORT: the long line must
    still read 30, and the short must appear as its own 20, not as -20
    netted off the long. Item 221: there is no projected line any more, so
    the held measurement is the whole claim.
    """
    pipeline = _projection_pipeline(target_pct=40.0)
    positions = [
        Position(
            symbol="MSFT",
            qty=10,
            avg_entry=400,
            current_price=400,
            market_value=3000,
            unrealized_pnl=0,
            sector="Technology",
        ),
        Position(
            symbol="INTC",
            qty=-20,
            avg_entry=100,
            current_price=100,
            market_value=-2000,
            unrealized_pnl=0,
            sector="Technology",
        ),
    ]
    with patch("src.execution.broker._get_sector"):
        out = pipeline._build_projected_portfolio(
            positions, _tech_buy_analyses(), total_value=10000, run=prun(pipeline)
        )
    assert "Technology long 30%" in out, "the short must not net off the longs"
    assert "Technology short 20%" in out
    assert "Technology -" not in out


# === Board item 221 — the preview sizes from each candidate's own stop ===


def _sizing_pipeline(target_pct: float = 75.0):
    """A projection pipeline carrying the constructor the pipeline would
    really build orders with, so the preview reads the SAME sizing dials."""
    from types import SimpleNamespace

    pipeline = _projection_pipeline(target_pct=target_pct)
    pipeline.portfolio_constructor = SimpleNamespace(
        cfg=SimpleNamespace(risk_budget_pct=5.0, max_position_pct=65.0),
    )
    return pipeline


def _preview_ceilings(out: str) -> dict[str, float]:
    """The per-candidate stop-implied CEILINGS the preview printed, by
    symbol. A ceiling on one name, never a projected weight."""
    import re

    line = next(ln for ln in out.splitlines() if "OWN stop distance" in ln)
    return {m.group(1): float(m.group(2)) for m in re.finditer(r"([A-Z]+) stop -[\d.]+% → ≤(\d+)%", line)}


def test_preview_ceiling_is_the_constructors_own_stop_implied_cap():
    """ITEM 221 criterion 1. Deliberately UNEQUAL stop distances; the
    ceiling the preview states for each name must equal the constructor's
    own stop-implied cap for that name, to the digit. It is a cap on ONE
    name — the preview never sums these into a projected mix, because the
    weight each name actually gets depends on a PM target that does not
    exist yet."""
    from src.risk.constants import risk_budget_allocation_pct

    stops = {"NVDA": 90.0, "AMD": 85.0, "AAPL": 80.0}
    pipeline = _sizing_pipeline()
    with patch("src.execution.broker._get_sector"):
        out = pipeline._build_projected_portfolio([], _tech_buy_analyses(stops), total_value=10000, run=prun(pipeline))
    shown = _preview_ceilings(out)
    assert set(shown) == {"NVDA", "AMD", "AAPL"}
    for sym, stop in stops.items():
        expected = risk_budget_allocation_pct(
            entry_price=100.0,
            stop_price=stop,
            total_value=10000.0,
            risk_budget_pct=5.0,
        )
        assert expected is not None
        assert shown[sym] == pytest.approx(round(expected), abs=0)
    # Unequal stops MUST give unequal sizes; equal sizes here would mean the
    # flat slice survived under another name.
    assert len(set(shown.values())) == 3


def test_preview_ceiling_is_not_independent_of_the_stop():
    """ITEM 221 criterion 2. The same candidate, same entry, same book, only
    the stop moved: if the number the preview states does not move with it,
    a flat per-candidate size is still in the path somewhere."""
    pipeline = _sizing_pipeline()
    sizes = []
    for stop in (90.0, 80.0):
        with patch("src.execution.broker._get_sector"):
            out = pipeline._build_projected_portfolio(
                [], _tech_buy_analyses({"NVDA": stop, "AMD": stop, "AAPL": stop}), total_value=10000, run=prun(pipeline)
            )
        sizes.append(_preview_ceilings(out)["NVDA"])
    assert sizes[0] != sizes[1], (
        "the preview's number for a candidate is independent of its stop — the flat per-candidate size is back"
    )
    # Wider stop, lower ceiling: the direction matters, not just change.
    assert sizes[1] < sizes[0]


def test_preview_ceiling_is_clamped_to_something_the_desk_could_reach():
    """ITEM 221, adversary 2026-10-01. An UNCLAMPED stop-implied ceiling
    prints figures like 247% beside a sector label, which is the rejected
    225% arithmetic in another costume: prose saying "not a weight" is no
    defence against a reader who joins the composition line to the ceiling
    line. Nothing printed may exceed the single-name notional ceiling."""
    pipeline = _sizing_pipeline()
    tight = {"NVDA": 98.0, "AMD": 97.0, "AAPL": 99.0}
    with patch("src.execution.broker._get_sector"):
        out = pipeline._build_projected_portfolio([], _tech_buy_analyses(tight), total_value=10000, run=prun(pipeline))
    ceilings = _preview_ceilings(out)
    assert ceilings == {"NVDA": 65.0, "AMD": 65.0, "AAPL": 65.0}
    assert max(ceilings.values()) <= 65.0


def test_preview_dials_come_from_the_constructors_own_defaults():
    """ITEM 221 criterion 2, adversary 2026-10-01. The preview's two sizing
    dials must not be flat literals at the call site: the number-source
    scanner reads definition sites, not positional call arguments, so a
    literal here is a tracked number that became untracked and can desync
    from the constructor the next time the default moves."""
    import ast
    import inspect
    from src.portfolio_constructor import ConstructorConfig
    from src.prompt_facts.projected import PromptProjected

    body = inspect.getsource(PromptProjected._build_projected_portfolio)
    tree = ast.parse(textwrap.dedent(body))
    dial = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "_dial")
    assert not [
        n
        for n in ast.walk(dial)
        if isinstance(n, ast.Constant)
        and isinstance(n.value, (int, float))
        and not isinstance(n.value, bool)
        and n.value != 0
    ], "a flat sizing literal is back in the preview's dial reader"
    pipeline = build_pipeline()
    prun(pipeline).symbol_sectors = {}
    with patch("src.execution.broker._get_sector"):
        out = pipeline._build_projected_portfolio(
            [], _tech_buy_analyses({"NVDA": 50.0, "AMD": 50.0, "AAPL": 50.0}), total_value=10000, run=prun(pipeline)
        )
    # No constructor attached, so the fallback runs: it must reproduce
    # ConstructorConfig's OWN declared defaults.
    expected = ConstructorConfig().risk_budget_pct * 100 / 50
    assert _preview_ceilings(out)["NVDA"] == pytest.approx(round(expected))
    assert f"{ConstructorConfig().max_position_pct:.0f}% single-name" in out


def test_preview_states_each_sectors_share_of_the_candidate_set():
    """ITEM 221, ruling 2026-10-01. Six candidates in one sector against an
    empty book must not read as a bare count: the PM's ordering and dropping
    decision needs a measured forward fact, and the share OF THE CANDIDATE
    SET is one — true of what is on offer, no projection, no threshold."""
    pipeline = _sizing_pipeline()
    analyses = _tech_buy_analyses({"NVDA": 90.0, "AMD": 90.0, "AAPL": 90.0})
    prun(pipeline).symbol_sectors = {
        "NVDA": "Technology",
        "AMD": "Technology",
        "AAPL": "Energy",
    }
    with patch("src.execution.broker._get_sector"):
        out = pipeline._build_projected_portfolio([], analyses, total_value=10000, run=prun(pipeline))
    assert "Technology 2 of 3 (67% of the candidate set: NVDA, AMD)" in out
    assert "Energy 1 of 3 (33% of the candidate set: AAPL)" in out
    # No threshold, no warning level on the candidate composition.
    assert "⚠" not in out


def test_preview_claims_no_projected_sector_weight():
    """ITEM 221, the ruling of 2026-10-01. The preview must not state what
    the candidate set would WEIGH. It cannot know: the constructor sizes at
    `min(PM target, stop-implied ceiling, ...)` and the PM's target is made
    AFTER reading this. A projected mix — flat-sliced or stop-maximised —
    is a fiction the PM then corrects the real book against."""
    pipeline = _sizing_pipeline()
    with patch("src.execution.broker._get_sector"):
        out = pipeline._build_projected_portfolio(
            [], _tech_buy_analyses({"NVDA": 90.0, "AMD": 85.0, "AAPL": 80.0}), total_value=10000, run=prun(pipeline)
        )
    assert "CANNOT tell you what these candidates would weigh" in out
    assert "If you allocate" not in out
    assert "→ " not in out.split("Per candidate")[0], "a projected book line is back"
    assert "NOT a weight" in out


def test_preview_and_constructor_share_ONE_sizing_definition():
    """ITEM 221. Not two implementations that happen to agree today: the
    constructor and the preview must resolve to the same function object."""
    import src.portfolio_constructor as pc
    import src.risk.constants as rc

    assert pc.risk_budget_allocation_pct is rc.risk_budget_allocation_pct
    import ast
    import inspect
    from src.prompt_facts.projected import PromptProjected

    params = inspect.signature(
        PromptProjected._build_projected_portfolio,
    ).parameters
    assert "default_buy_pct" not in params, "the flat per-candidate preview size is back in the preview path"
    # And no flat per-candidate constant survives in the body either: the
    # only `default_buy_pct` left in the module is the historical note in
    # the docstring, which is prose, not code.
    body = inspect.getsource(PromptProjected._build_projected_portfolio)
    tree = ast.parse(textwrap.dedent(body))
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)} | {
        n.arg for n in ast.walk(tree) if isinstance(n, ast.arg)
    }
    assert "default_buy_pct" not in names
    assert "risk_budget_allocation_pct" in names


def test_preview_names_a_candidate_it_cannot_size_rather_than_assuming_one():
    """ITEM 221. Unusable stop geometry is reported, never back-filled with
    an assumed size — inventing one is the defect being fixed."""
    from src.risk.constants import risk_budget_allocation_pct

    assert (
        risk_budget_allocation_pct(
            entry_price=100.0,
            stop_price=100.0,
            total_value=10000.0,
            risk_budget_pct=5.0,
        )
        is None
    )


# === MacroStore history ===


def test_macro_store_save_appends_to_history(tmp_path):
    store = MacroStore(data_dir=str(tmp_path / "macro"))
    store.save_last_state(
        {
            "regime": "risk-on",
            "confidence": "high",
            "equity_outlook": "bullish",
            "summary": "day1",
            "position_guidance": {"target_invested_pct": 75},
        }
    )
    # Simulate next day
    from src.data.macro_store import MacroStore as _M

    store2 = _M(data_dir=str(tmp_path / "macro"))
    store2.save_last_state(
        {
            "regime": "transitional",
            "confidence": "medium",
            "equity_outlook": "neutral",
            "summary": "day2",
            "position_guidance": {"target_invested_pct": 60},
        }
    )
    hist = store2.load_history(days=7)
    # Both entries keyed by et_today() — one row only if same ET date.
    assert len(hist) >= 1
    # Latest snapshot must be the "day2" call
    assert hist[-1]["regime"] == "transitional"


def test_macro_store_load_history_falls_back_to_last_state(tmp_path):
    """Historical file missing but last_state.json exists → returns 1-element list."""
    store = MacroStore(data_dir=str(tmp_path / "macro"))
    store.save_last_state(
        {
            "regime": "risk-on",
            "confidence": "high",
            "equity_outlook": "bullish",
            "summary": "x",
            "position_guidance": {"target_invested_pct": 70},
        }
    )
    # Delete the history file to simulate legacy state
    store.history_path.unlink()
    history = store.load_history(days=7)
    assert len(history) == 1
    assert history[0]["regime"] == "risk-on"


# === NewsStore recent_state_changes ===


def test_news_store_recent_state_changes_dedupe_by_event(tmp_path):
    store = NewsStore(data_dir=str(tmp_path / "news"))
    from datetime import date
    import json

    # Write 3 days of reports all mentioning the same HIGH event
    for days_ago in (5, 3, 1):
        d = et_today() - timedelta(days=days_ago)
        day_dir = store.data_dir / str(d)
        day_dir.mkdir(parents=True, exist_ok=True)
        (day_dir / "full_report.json").write_text(
            json.dumps(
                {
                    "state_changes": [
                        {
                            "event": "Iran ceasefire holds",
                            "conviction": "high",
                            "affected_symbols": ["XOM", "CVX"],
                            "previous_state": "conflict",
                            "new_state": "ceasefire",
                            "market_impact": "bearish energy",
                        }
                    ],
                }
            )
        )

    # And one different HIGH event only today
    today_dir = store.data_dir / str(et_today())
    today_dir.mkdir(parents=True, exist_ok=True)
    (today_dir / "full_report.json").write_text(
        json.dumps(
            {
                "state_changes": [
                    {
                        "event": "Fed signals pause",
                        "conviction": "high",
                        "affected_symbols": ["JPM"],
                        "previous_state": "cutting",
                        "new_state": "holding",
                        "market_impact": "banks bullish",
                    },
                    {"event": "Minor noise", "conviction": "low"},  # skipped
                ],
            }
        )
    )

    changes = store.recent_state_changes(lookback_days=14, limit=10)
    events = [c["event"] for c in changes]
    # Iran event collapsed into ONE entry with first_seen 5d ago
    iran = [c for c in changes if c["event"] == "Iran ceasefire holds"]
    assert len(iran) == 1
    assert iran[0]["first_seen_date"] == str(et_today() - timedelta(days=5))
    # Fed event present
    assert "Fed signals pause" in events
    # Low conviction filtered out
    assert "Minor noise" not in events


# === DB helpers ===


def test_db_get_symbol_last_buy_returns_most_recent_buy(tmp_path):
    from src.storage.db import Database

    db = Database(str(tmp_path / "t.db"))
    db.initialize()
    # Insert an OLD buy then a newer one; last_buy should return the newer
    db.insert_trade("NVDA", "BUY", 5, 180, "first entry", "r-1", stop_loss=90.0)
    # Force timestamp backdate
    db.conn.execute("UPDATE trades SET timestamp = datetime('now', '-10 days') WHERE reasoning='first entry'")
    db.conn.commit()
    db.insert_trade("NVDA", "BUY", 5, 195, "second entry, 3/4 aligned", "r-2", stop_loss=90.0)
    last = db.get_symbol_last_buy("NVDA")
    assert last is not None
    assert last["price"] == 195
    assert "second entry" in last["reasoning"]


def test_db_get_recent_insights_returns_newest_first(tmp_path):
    from src.storage.db import Database

    db = Database(str(tmp_path / "t.db"))
    db.initialize()
    db.save_insights(
        date="2026-04-15", tomorrow_outlook="O15", lessons="L15", suggested_actions="[]", risk_rating="low"
    )
    db.save_insights(
        date="2026-04-17", tomorrow_outlook="O17", lessons="L17", suggested_actions="[]", risk_rating="moderate"
    )
    db.save_insights(
        date="2026-04-16", tomorrow_outlook="O16", lessons="L16", suggested_actions="[]", risk_rating="low"
    )
    rows = db.get_recent_insights(limit=7)
    assert [r["date"] for r in rows] == ["2026-04-17", "2026-04-16", "2026-04-15"]


# === thesis_invalid_if: dedicated field survives DB + position_history
# (2026-09-03) ===
#
# Companion to the builder-level tests in test_portfolio_constructor.py.
# Those prove the field is set correctly on TradeDecision; these prove it
# then survives (a) a round trip through the `trades` table and (b)
# `TradingPipeline._build_position_history`'s read-back — the two places a
# restart or a later pipeline run would otherwise have to recover it from
# scratch, at which point only the truncated `reasoning` text remains.

_LONG_CONDITION_FOR_DB = (
    "Invalid if the stock closes below its rising 50-day moving average "
    "on a weekly closing basis for two consecutive weeks, or if the next "
    "earnings report shows gross margin compression greater than 300 "
    "basis points year-over-year, or if a director sells more than 2% of "
    "their disclosed stake outside a pre-scheduled 10b5-1 plan."
)
assert len(_LONG_CONDITION_FOR_DB) > 280  # blows past the old truncation point


def test_db_insert_trade_round_trips_thesis_invalid_if(tmp_path):
    """A trades-table column, not just an in-memory field: the condition
    must still be there after `Database` re-reads its own row."""
    from src.storage.db import Database

    db = Database(str(tmp_path / "t.db"))
    db.initialize()
    db.insert_trade(
        "NVDA",
        "BUY",
        10,
        200.0,
        "AI capex supercycle",
        "r-1",
        thesis_invalid_if=_LONG_CONDITION_FOR_DB,
        stop_loss=90.0,
    )
    last = db.get_symbol_last_buy("NVDA")
    assert last is not None
    assert last["thesis_invalid_if"] == _LONG_CONDITION_FOR_DB


def test_db_insert_trade_thesis_invalid_if_defaults_to_none(tmp_path):
    """Every legacy call site that never passes it gets NULL, not a
    fabricated value — same discipline as the conviction-ledger columns."""
    from src.storage.db import Database

    db = Database(str(tmp_path / "t.db"))
    db.initialize()
    db.insert_trade("AAPL", "BUY", 5, 180.0, "legacy caller", "r-1", stop_loss=90.0)
    last = db.get_symbol_last_buy("AAPL")
    assert last is not None
    assert last["thesis_invalid_if"] is None


def _pipeline_for_position_history():
    from unittest.mock import MagicMock
    from src.pipeline import TradingPipeline

    pipeline = build_pipeline(db=MagicMock(), tech_store=MagicMock())
    pipeline.tech_store.get_history.return_value = []
    return pipeline


def test_build_position_history_carries_full_thesis_invalid_if():
    """The real gap this whole change closes: `entry_reasoning` truncates
    to 280 chars, but `thesis_invalid_if` in the same history block must
    not — it comes from the dedicated `trades.thesis_invalid_if` column,
    not by slicing `reasoning`."""
    pipeline = _pipeline_for_position_history()
    pipeline.db.get_symbol_last_buy.return_value = {
        "timestamp": "2026-08-20 09:31:00",
        "price": 200.0,
        "reasoning": "AI capex supercycle " * 20,  # long enough to truncate
        "thesis_invalid_if": _LONG_CONDITION_FOR_DB,
    }
    history = pipeline._build_position_history([_pos("NVDA")])
    assert "NVDA" in history
    entry = history["NVDA"]
    assert len(entry["entry_reasoning"]) <= 280
    assert entry["thesis_invalid_if"] == _LONG_CONDITION_FOR_DB


def test_build_position_history_thesis_invalid_if_none_for_legacy_row():
    """A pre-migration `trades` row (or any BUY that stated no condition)
    must not fabricate a value — None, matching the DB column's own
    default."""
    pipeline = _pipeline_for_position_history()
    pipeline.db.get_symbol_last_buy.return_value = {
        "timestamp": "2026-08-20 09:31:00",
        "price": 200.0,
        "reasoning": "legacy row, no thesis_invalid_if column at write time",
        # No "thesis_invalid_if" key at all — mirrors a real legacy dict.
    }
    history = pipeline._build_position_history([_pos("NVDA")])
    assert history["NVDA"]["thesis_invalid_if"] is None
