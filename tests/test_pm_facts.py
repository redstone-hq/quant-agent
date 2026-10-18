"""PMFacts — structured quantitative snapshot surfaced to PM."""

from unittest.mock import MagicMock, patch

from src.agents.portfolio_manager import PortfolioManagerAgent
from src.models import Position, TechAnalysisResult, TechReasoningChain
from src.pipeline_context import PMFacts
from src.storage.db import Database
from tests.pipeline_factory import build_pipeline


def _pos(symbol, qty, avg, current, sector="Technology") -> Position:
    return Position(
        symbol=symbol,
        qty=qty,
        avg_entry=avg,
        current_price=current,
        market_value=qty * current,
        unrealized_pnl=(current - avg) * qty,
        sector=sector,
    )


def _ta(symbol: str, age: int | None = None) -> TechAnalysisResult:
    t = TechAnalysisResult(
        symbol=symbol,
        rating="buy",
        entry_price=100,
        stop_loss=95,
        reference_target=110,
        support_levels=[95],
        resistance_levels=[110],
        setup_type="range",
        expected_horizon_sessions=10,
        reasoning="test",
        reasoning_chain=TechReasoningChain(
            trend="x",
            momentum="x",
            volatility="x",
            volume="x",
            support_resistance="x",
        ),
        thesis_invalid_if="closes below support",
    )
    t.signal_age_days = age
    return t


def test_pmfacts_render_produces_structured_block():
    f = PMFacts(
        closed_trades_30d=12,
        win_rate_30d_pct=58.3,
        avg_return_30d_pct=2.4,
        avg_hold_days_30d=6.1,
        invested_pct=72.0,
        cash_pct=28.0,
        position_count=8,
        # Spec §12.2 — the table is split by side and never netted.
        sector_weights_long={"Technology": 22.0, "Financial Services": 15.0},
        sector_weights_short={"Energy": 6.0},
        positions_under_5d=2,
        positions_5_to_15d=4,
        positions_over_15d=2,
        tech_signals_count=14,
        tech_signals_median_age_days=3,
        tech_signals_stale_count=2,
        rolling_5d_pct=-1.5,
        rolling_20d_pct=3.0,
    )
    rendered = f.render()
    assert "n=12" in rendered
    assert "win_rate=+58.30%" in rendered
    assert "Technology: 22.0%" in rendered
    assert "LONG side" in rendered and "SHORT side" in rendered
    assert "Energy: 6.0%" in rendered
    assert "stale(≥8d)=2" in rendered


def test_pm_facts_builder_populates_from_positions_and_calibration(tmp_path, monkeypatch):
    """_build_pm_facts reads calibration + positions + RM verdicts into PMFacts."""
    import json

    from src.storage.analytics import calibration

    # Trades are seeded relative to now; this test is about plumbing, not the clean-record cut-off.
    monkeypatch.setattr(calibration, "CLEAN_RECORD_START_UTC", "2000-01-01T00:00:00")

    db = Database(str(tmp_path / "t.db"))
    db.initialize()

    # Seed 3 closed trades (meets calibration threshold ≥3)
    db.insert_trade("NVDA", "BUY", 10, 100, "x", "r1", broker_order_id="b1", fill_status="filled", stop_loss=90.0)
    db.conn.execute("UPDATE trades SET timestamp=datetime('now', '-15 days') WHERE broker_order_id='b1'")
    db.conn.commit()
    db.insert_trade("NVDA", "SELL", 10, 115, "x", "r2", broker_order_id="s1", fill_status="filled")
    db.conn.execute("UPDATE trades SET timestamp=datetime('now', '-5 days') WHERE broker_order_id='s1'")
    db.conn.commit()
    db.insert_trade("AAPL", "BUY", 5, 200, "x", "r1", broker_order_id="b2", fill_status="filled", stop_loss=90.0)
    db.conn.execute("UPDATE trades SET timestamp=datetime('now', '-14 days') WHERE broker_order_id='b2'")
    db.conn.commit()
    db.insert_trade("AAPL", "SELL", 5, 180, "x", "r2", broker_order_id="s2", fill_status="filled")
    db.conn.execute("UPDATE trades SET timestamp=datetime('now', '-4 days') WHERE broker_order_id='s2'")
    db.conn.commit()
    db.insert_trade("JPM", "BUY", 10, 150, "x", "r1", broker_order_id="b3", fill_status="filled", stop_loss=90.0)
    db.conn.execute("UPDATE trades SET timestamp=datetime('now', '-10 days') WHERE broker_order_id='b3'")
    db.conn.commit()
    db.insert_trade("JPM", "SELL", 10, 160, "x", "r2", broker_order_id="s3", fill_status="filled")
    db.conn.execute("UPDATE trades SET timestamp=datetime('now', '-3 days') WHERE broker_order_id='s3'")
    db.conn.commit()

    # Insert 2 RM verdicts: one clean, one with scale_down
    db.insert_agent_log(
        agent_name="risk_manager",
        run_id="r1",
        input_summary="",
        input_message="",
        output_summary="",
        full_response=json.dumps({"approved": True, "scale_all_buys": 0.5, "modifications": [], "reasoning": "cut"}),
        model="x",
        tokens_used=0,
    )
    db.conn.execute("UPDATE agent_logs SET timestamp=datetime('now', '-2 days') WHERE agent_name='risk_manager'")
    db.conn.commit()

    pipeline = build_pipeline(db=db, tech_store=MagicMock())
    pipeline.tech_store.get_history.return_value = []

    positions = [
        _pos(
            "MSFT", 20, 400, 440, sector="Technology"
        ),  # weight=44%, pnl=10% (large but not drift because we want to test flag)
        _pos("XOM", 10, 100, 110, sector="Energy"),  # small position
    ]
    analyses = [_ta("NVDA", age=3), _ta("AMD", age=10), _ta("GOOGL", age=None)]

    facts = pipeline._build_pm_facts(
        positions=positions,
        analyses=analyses,
        total_value=10_000,
        cash=100,
        recent_performance={"rolling_5d_pct": -2.0, "rolling_20d_pct": 1.0},
    )

    # Calibration picked up
    assert facts.closed_trades_30d == 3

    # Book state.
    #
    # `invested_pct` is `book_exposure(...).deployed_pct` — measured from the
    # POSITIONS, not derived as `total_value - cash`. It used to be the
    # subtraction, which is a second definition of the quantity the pre-trade
    # `macro_exposure_deviation` advisory judges against the same macro
    # target (see tests/test_one_definition_per_quantity.py). The fixture's
    # cash is now the figure that actually completes the book — 9,900 of
    # positions + 100 of cash = the 10,000 of equity — so both readings
    # agree and the assertion no longer depends on which one is used.
    assert facts.invested_pct == 99.0  # (8,800 MSFT + 1,100 XOM) / 10,000
    assert facts.cash_pct == 1.0
    assert facts.position_count == 2
    assert facts.sector_weights_long.get("Technology") == 88.0  # 20*440/10000 * 100
    assert facts.sector_weights_long.get("Energy") == 11.0  # 10*110/10000 * 100
    assert facts.sector_weights_short == {}  # long-only book (spec §12.2)

    # Signal freshness
    assert facts.tech_signals_count == 3
    # Only 2 have ages (3 and 10); median = 6.5 → int(6.5) = 6
    assert facts.tech_signals_median_age_days == 6
    assert facts.tech_signals_stale_count == 1  # age 10 ≥ 8

    # System perf pass-through
    assert facts.rolling_5d_pct == -2.0


def test_pm_build_user_message_renders_facts_when_provided():
    """PM prompt surfaces the facts section under '## Quantitative Facts'."""
    facts = PMFacts(
        closed_trades_30d=8,
        win_rate_30d_pct=62.5,
        invested_pct=70.0,
        cash_pct=30.0,
        position_count=5,
        sector_weights_long={"Technology": 30.0},
    )
    with patch("anthropic.Anthropic"):
        agent = PortfolioManagerAgent(api_key="test", model="claude-opus-4-6")
        msg = agent.build_user_message(
            analyses=[],
            positions=[],
            macro_analysis=None,
            cash_balance=3000.0,
            total_value=10000.0,
            facts=facts,
        )
        assert "## Quantitative Facts" in msg
        assert "n=8" in msg
        assert "win_rate=+62.50%" in msg
        assert "Technology: 30.0%" in msg


def test_pm_build_user_message_omits_facts_section_when_none():
    """When facts is None (legacy caller), the section simply doesn't render."""
    with patch("anthropic.Anthropic"):
        agent = PortfolioManagerAgent(api_key="test", model="claude-opus-4-6")
        msg = agent.build_user_message(
            analyses=[],
            positions=[],
            macro_analysis=None,
            cash_balance=3000.0,
            total_value=10000.0,
            # no facts kwarg
        )
        assert "## Quantitative Facts" not in msg
