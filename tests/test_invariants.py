"""System invariants — properties that must hold regardless of session,
input shape, or host timezone. These catch regressions that unit tests
miss because they only assert behavior one rule at a time.

Scope (P1b):
  1. Orders can't breach hard risk caps — even with pathological PM output.
  2. Non-trading days block entry (morning, midday, evening, preprocess).
  3. Reruns on the same ET date don't double-count (idempotent writes).
  4. Partial-fill / canceled / rejected orders don't pollute PM memory or
     calibration — executed-trade predicate gates everything.
  5. ET/UTC boundary: a trade logged at 00:30 UTC is attributed to the ET
     trading-day that was "yesterday" in UTC.
"""

from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest

from src.config import RiskConfig
from src.models import Position, TradeDecision
from src.pipeline import HARD_BLOCK_RULES, TradingPipeline
from src.pipeline_context import RunContext
from src.risk.rules import RiskRuleEngine
from src.storage.db import Database
from src.trading_calendar import ET, UTC
from tests.session_clock import todays_session_stamp
from tests.pipeline_factory import build_pipeline


def _risk_config() -> RiskConfig:
    return RiskConfig(
        max_position_pct=15.0,
        max_total_position_pct=100.0,
        require_stop_loss=True,
    )


# ---------------------------------------------------------------------------
# Invariant 1: Hard risk caps are unbreakable.
# Whatever PM / RM produce, the hard filter must reject BUYs that exceed caps.
# ---------------------------------------------------------------------------
def test_invariant_orders_cannot_breach_position_cap():
    engine = RiskRuleEngine(_risk_config())
    decision = TradeDecision(
        action="BUY",
        symbol="NVDA",
        allocation_pct=25.0,  # 25% — breaches 15% position cap
        entry_price=100.0,
        stop_loss=95.0,
        take_profit=110.0,
        reasoning="pathological oversized",
    )
    violations = engine.check(
        decision=decision,
        positions=[],
        total_value=100_000.0,
    )
    rule_names = {v.rule for v in violations}
    assert "max_position_pct" in rule_names
    assert "max_position_pct" in HARD_BLOCK_RULES


def test_invariant_hard_risk_stage_drops_breaching_buy():
    """Full-stack: even if PM emits a breaching BUY, the stage strips it."""
    engine = RiskRuleEngine(_risk_config())
    pipeline = build_pipeline(risk_engine=engine)
    pipeline.config = MagicMock()
    pipeline.config.trading.universe = ["NVDA"]

    bad = TradeDecision(
        action="BUY",
        symbol="NVDA",
        allocation_pct=25.0,  # over 15% cap
        entry_price=100.0,
        stop_loss=95.0,
        take_profit=110.0,
        reasoning="pathological",
    )
    ok = TradeDecision(
        action="BUY",
        symbol="AAPL",
        allocation_pct=5.0,
        entry_price=180.0,
        stop_loss=170.0,
        take_profit=200.0,
        reasoning="fine",
    )
    allowed, _violations, blocked = pipeline.risk_gate._filter_hard_risk_decisions(
        [bad, ok],
        positions=[],
        total_value=100_000.0,
    )
    allowed_symbols = {d.symbol for d in allowed}
    assert "NVDA" not in allowed_symbols
    assert "AAPL" in allowed_symbols
    assert any("NVDA" in msg for msg in blocked)


# ---------------------------------------------------------------------------
# Invariant 1b (Stage 1 — QAMC provider/model plumbing): hard risk filtering
# must be structurally unreachable from provider/LLM config. Garbage or
# missing `config.llm` must not change, weaken, or crash the hard-risk gate —
# proving the new explicit-provider/model config Stage 1 added has no path
# into deterministic risk decisions.
# ---------------------------------------------------------------------------
def test_invariant_hard_risk_gate_unaffected_by_garbage_llm_config():
    """Same scenario as test_invariant_hard_risk_stage_drops_breaching_buy,
    except `pipeline.config.llm` is a plain string (not even a mock) —
    if `_filter_hard_risk_decisions` or `RiskRuleEngine.check()` dereferenced
    `config.llm`/`config.provider` ANYWHERE, this would raise AttributeError
    instead of gating correctly."""
    engine = RiskRuleEngine(_risk_config())
    pipeline = build_pipeline(risk_engine=engine)
    pipeline.config = MagicMock()
    pipeline.config.trading.universe = ["NVDA"]
    # Deliberately not a MagicMock — any attribute access raises immediately,
    # unlike a MagicMock which would silently auto-vivify and mask the bug.
    pipeline.config.llm = "not-a-config-object-and-has-no-provider-field"

    bad = TradeDecision(
        action="BUY",
        symbol="NVDA",
        allocation_pct=25.0,  # over 15% cap
        entry_price=100.0,
        stop_loss=95.0,
        take_profit=110.0,
        reasoning="pathological",
    )
    ok = TradeDecision(
        action="BUY",
        symbol="AAPL",
        allocation_pct=5.0,
        entry_price=180.0,
        stop_loss=170.0,
        take_profit=200.0,
        reasoning="fine",
    )
    allowed, _violations, blocked = pipeline.risk_gate._filter_hard_risk_decisions(
        [bad, ok],
        positions=[],
        total_value=100_000.0,
    )
    allowed_symbols = {d.symbol for d in allowed}
    assert "NVDA" not in allowed_symbols
    assert "AAPL" in allowed_symbols
    assert any("NVDA" in msg for msg in blocked)


#: Keyword-only constructor arguments `RiskRuleEngine` may accept BESIDES
#: its one config object, each with the reason it is not a hole in this
#: invariant. Nothing reaches this list without being ACCOUNT STATE — never
#: config, never anything an LLM authored or a provider supplied.
#:
#:   portfolio_vol_provider — docs/WORK.md item 32, owner call 2026-09-11.
#:     Supplies the realized daily volatility of the book the desk is
#:     actually holding, so the daily circuit breaker can measure a loss
#:     against that instead of a frozen percentage of equity. Derived from
#:     the live position list and real market price history for those
#:     symbols; no model output, no provider config, and a failed read falls
#:     back to the fixed percentage rather than disabling the breaker.
_ENGINE_ALLOWED_KEYWORD_ARGS = {"portfolio_vol_provider"}


def test_invariant_risk_rule_engine_never_reads_llm_or_provider_config():
    """RiskRuleEngine is constructed from RiskConfig ALONE — no LLMConfig,
    no provider config — so the explicit provider/model fields Stage 1 added
    to LLMConfig structurally cannot reach deterministic risk math.

    Widened 2026-09-11 to permit ACCOUNT-STATE keyword arguments on an
    explicit allowlist (`_ENGINE_ALLOWED_KEYWORD_ARGS`), and TIGHTENED at the
    same time: the positional signature is still exactly `(self, config)`, so
    a second config object cannot be slipped in, every extra argument must be
    keyword-only and listed above with a reason, and no argument name may
    look like model/provider plumbing however it is passed.
    """
    import inspect
    from src.risk.rules import RiskRuleEngine as _Engine

    sig = inspect.signature(_Engine.__init__)
    params = sig.parameters

    positional = [name for name, p in params.items() if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)]
    assert positional == ["self", "config"], (
        "RiskRuleEngine must take exactly one positional config object "
        "(RiskConfig); widening this to also accept llm/provider config "
        "would create a path from Stage 1's provider plumbing into hard "
        "risk math"
    )
    assert not any(p.kind in (p.VAR_POSITIONAL, p.VAR_KEYWORD) for p in params.values()), (
        "*args/**kwargs would let anything at all reach the risk engine"
    )

    extra = set(params) - {"self", "config"}
    assert extra <= _ENGINE_ALLOWED_KEYWORD_ARGS, (
        f"RiskRuleEngine grew constructor argument(s) "
        f"{sorted(extra - _ENGINE_ALLOWED_KEYWORD_ARGS)} that are not on the "
        f"account-state allowlist. Add it to _ENGINE_ALLOWED_KEYWORD_ARGS "
        f"WITH a written reason it is account state rather than config, or "
        f"do not pass it into deterministic risk math."
    )
    for name in extra:
        assert params[name].kind == params[name].KEYWORD_ONLY, (
            f"`{name}` must be keyword-only so it can never be passed by position where a config object is expected"
        )
        assert params[name].default is None, (
            f"`{name}` must default to None: an engine built with a config "
            f"alone — which is how ~50 fixtures in this repo and any caller "
            f"without a database build it — must behave exactly as before"
        )
    forbidden = ("llm", "model", "provider_order", "api_key", "prompt")
    for name in params:
        if name in _ENGINE_ALLOWED_KEYWORD_ARGS:
            # `portfolio_vol_provider` contains "provider"; the check below
            # is about MODEL providers, so match on the narrower tokens.
            continue
        assert not any(token in name for token in forbidden), (
            f"`{name}` names model/provider plumbing — it must not reach deterministic risk math"
        )


# ---------------------------------------------------------------------------
# Invariant 2: Non-trading days short-circuit every entry point.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "method_name",
    [
        "run_morning",
        "run_midday",
        "run_evening",
        "run_earnings_preprocess",
        "run_intra_check",
    ],
)
def test_invariant_non_trading_day_blocks_every_entry_point(method_name, tmp_path):
    pipeline = build_pipeline(db=Database(str(tmp_path / "t.db")))
    pipeline.db.initialize()
    pipeline.broker = MagicMock()
    pipeline.broker.is_trading_day.return_value = False  # market closed
    pipeline.earnings_provider = MagicMock()
    pipeline.config = MagicMock()
    pipeline.config.trading.universe = []

    result = getattr(pipeline, method_name)()
    assert result["status"] == "market_holiday", f"{method_name} should short-circuit on non-trading days"
    # Nothing should hit the broker beyond the calendar probe.
    pipeline.broker.get_account.assert_not_called()
    pipeline.broker.submit_order.assert_not_called()


# ---------------------------------------------------------------------------
# Invariant 3: Idempotent daily writes.
# Evening-snapshot paths use INSERT OR REPLACE on the ET date key, so a rerun
# overwrites rather than appending.
# ---------------------------------------------------------------------------
def test_invariant_insert_daily_pnl_is_idempotent_per_et_date(tmp_path):
    db = Database(str(tmp_path / "t.db"))
    db.initialize()

    db.insert_daily_pnl("2026-04-17", 100_000.0, 500.0, 0.5)
    db.insert_daily_pnl("2026-04-17", 100_000.0, 500.0, 0.5)  # same key, rerun
    db.insert_daily_pnl("2026-04-17", 101_000.0, 1500.0, 1.5)  # overwrite

    rows = db.get_daily_pnl(limit=10)
    same_date_rows = [r for r in rows if r["date"] == "2026-04-17"]
    # INSERT OR REPLACE on a UNIQUE/PK date column → exactly one row per date.
    assert len(same_date_rows) == 1
    # Last write wins.
    assert same_date_rows[0]["daily_pnl"] == 1500.0


def test_invariant_evening_snapshot_is_idempotent(tmp_path):
    """save_evening_snapshot on the same ET date must not duplicate rows."""
    db = Database(str(tmp_path / "t.db"))
    db.initialize()

    for _ in range(3):
        db.save_evening_snapshot(
            date="2026-04-17",
            total_value=100_000.0,
            daily_pnl=500.0,
            daily_return_pct=0.5,
            tomorrow_outlook="stable",
            lessons="fine",
            suggested_actions=[],
            risk_rating="low",
            tomorrow_bias="neutral",
            tomorrow_conviction="medium",
            tomorrow_key_risks=[],
            sell_decisions_assessment="",
        )

    rows = db.get_daily_pnl(limit=10)
    assert sum(1 for r in rows if r["date"] == "2026-04-17") == 1
    insights = db.get_recent_insights(limit=10)
    assert sum(1 for i in insights if i["date"] == "2026-04-17") == 1


# ---------------------------------------------------------------------------
# Invariant 4: Unfilled / canceled / rejected orders don't pollute memory.
# get_symbol_last_buy and compute_trade_calibration must honor the predicate.
# ---------------------------------------------------------------------------
def test_invariant_unfilled_buys_are_invisible_to_pm_memory(tmp_path):
    db = Database(str(tmp_path / "t.db"))
    db.initialize()

    # Legacy — NULL fill_status, treated as filled for back-compat.
    db.insert_trade("NVDA", "BUY", 10, 100.0, "legacy", "r1", stop_loss=90.0)
    # Submitted-only, never filled — MUST NOT count as a current buy.
    db.insert_trade(
        "NVDA",
        "BUY",
        20,
        105.0,
        "submitted",
        "r2",
        broker_order_id="ord-submit",
        fill_status="submitted",
        stop_loss=90.0,
    )
    # Fully canceled with zero partial fill — invisible.
    db.insert_trade(
        "NVDA",
        "BUY",
        15,
        103.0,
        "canceled",
        "r3",
        broker_order_id="ord-cancel",
        fill_status="submitted",
        stop_loss=90.0,
    )
    db.update_trade_fill("ord-cancel", fill_status="canceled", fill_qty=0.0, fill_price=0.0)
    # Rejected — invisible.
    db.insert_trade(
        "NVDA",
        "BUY",
        12,
        104.0,
        "rejected",
        "r4",
        broker_order_id="ord-reject",
        fill_status="submitted",
        stop_loss=90.0,
    )
    db.update_trade_fill("ord-reject", fill_status="rejected", fill_qty=0.0, fill_price=0.0)

    last = db.get_symbol_last_buy("NVDA")
    # Newest visible row is the legacy NULL one — everything else either
    # never filled or was fully canceled/rejected with zero qty.
    assert last is not None
    assert last["reasoning"] == "legacy"


def test_invariant_partial_fill_on_canceled_order_preserved(tmp_path):
    """A BUY that partially filled before being canceled created real
    exposure. It must still be visible via the `fill_qty > 0` branch of
    the executed-trade predicate."""
    db = Database(str(tmp_path / "t.db"))
    db.initialize()

    db.insert_trade(
        "AAPL", "BUY", 10, 180.0, "partial", "r1", broker_order_id="ord-part", fill_status="submitted", stop_loss=90.0
    )
    db.update_trade_fill(broker_order_id="ord-part", fill_status="canceled", fill_qty=3.0, fill_price=180.5)

    row = db.get_symbol_last_buy("AAPL")
    assert row is not None
    assert row["fill_qty"] == 3.0
    assert row["fill_status"] == "canceled"


def test_invariant_calibration_excludes_unfilled_orders(tmp_path):
    """compute_trade_calibration must match the PM-memory predicate."""
    db = Database(str(tmp_path / "t.db"))
    db.initialize()

    # Three legitimate closed pairs (needed: n_closed >= 3).
    for sym, entry, exit_, days_back in (
        ("NVDA", 100.0, 110.0, (10, 5)),
        ("JPM", 180.0, 195.0, (7, 2)),
        ("MSFT", 300.0, 310.0, (12, 3)),
    ):
        db.insert_trade(
            sym, "BUY", 10, entry, "x", "r1", broker_order_id=f"buy-{sym}", fill_status="filled", stop_loss=90.0
        )
        db.conn.execute(
            "UPDATE trades SET timestamp = datetime('2026-10-25 14:00:00', ?) WHERE broker_order_id=?",
            (f"-{days_back[0]} days", f"buy-{sym}"),
        )
        db.insert_trade(sym, "SELL", 10, exit_, "x", "r2", broker_order_id=f"sell-{sym}", fill_status="filled")
        db.conn.execute(
            "UPDATE trades SET timestamp = datetime('2026-10-25 14:00:00', ?) WHERE broker_order_id=?",
            (f"-{days_back[1]} days", f"sell-{sym}"),
        )
    db.conn.commit()

    # Poisoning pair — rejected order that should NOT pollute calibration.
    db.insert_trade(
        "TSLA", "BUY", 10, 250.0, "x", "r1", broker_order_id="buy-bad", fill_status="submitted", stop_loss=90.0
    )
    db.update_trade_fill("buy-bad", fill_status="rejected", fill_qty=0.0, fill_price=0.0)
    db.insert_trade("TSLA", "SELL", 10, 200.0, "x", "r2", broker_order_id="sell-bad", fill_status="submitted")
    db.update_trade_fill("sell-bad", fill_status="rejected", fill_qty=0.0, fill_price=0.0)

    stats = db.compute_trade_calibration(lookback_days=30)
    # 3 legitimate wins, rejected pair excluded → 100% win-rate, n=3.
    assert stats["n"] == 3
    assert stats["win_rate_pct"] == 100.0


# ---------------------------------------------------------------------------
# Invariant 5: ET-trading-day attribution across UTC midnight.
# A trade logged at 00:30 UTC belongs to the ET trading-day that was
# "yesterday" in UTC (22:30 ET previous calendar day).
# ---------------------------------------------------------------------------
def test_invariant_utc_midnight_boundary_attributes_to_et_trading_day(tmp_path):
    """SQLite stores trades as naive-UTC timestamps; today_only must use ET
    day bounds so a late-evening ET trade isn't mis-attributed to next day."""
    db = Database(str(tmp_path / "t.db"))
    db.initialize()

    # Insert a trade at 03:00 UTC on 2026-04-18 (= 23:00 ET on 2026-04-17).
    # Manually set the timestamp so we're not at the mercy of wall clock.
    db.insert_trade(
        "NVDA", "BUY", 10, 100.0, "boundary", "r1", broker_order_id="ord-boundary", fill_status="filled", stop_loss=90.0
    )
    db.conn.execute(
        "UPDATE trades SET timestamp = '2026-04-18 03:00:00' WHERE broker_order_id = 'ord-boundary'",
    )
    db.conn.commit()

    # From the perspective of an ET-2026-04-17 query window, this trade
    # IS in today. Freeze ET "today" to 2026-04-17.
    from datetime import date, datetime as _dt

    class _FrozenDT(_dt):
        @classmethod
        def now(cls, tz=None):
            frozen = _dt(2026, 4, 17, 20, 0, 0, tzinfo=UTC)
            if tz is None:
                return frozen.replace(tzinfo=None)
            return frozen.astimezone(tz)

    with patch("src.trading_calendar.datetime", _FrozenDT):
        todays = db.get_trades(today_only=True)

    # The boundary trade (03:00 UTC = 23:00 ET prev-day) is inside ET 2026-04-17.
    boundary_ids = [t["broker_order_id"] for t in todays]
    assert "ord-boundary" in boundary_ids


def test_invariant_session_date_key_stable_across_host_tz():
    """From host TZ=SGT perspective, 2026-04-18 09:00 SGT is still 2026-04-17 ET.
    session_date_key must return '2026-04-17' regardless of host TZ."""
    from src.trading_calendar import session_date_key

    # 2026-04-18 01:00 UTC == 2026-04-17 21:00 ET
    boundary = datetime(2026, 4, 18, 1, 0, 0, tzinfo=UTC)
    assert session_date_key(boundary) == "2026-04-17"

    # 2026-04-18 04:00 UTC == 2026-04-18 00:00 ET
    past_et_midnight = datetime(2026, 4, 18, 4, 0, 0, tzinfo=UTC)
    assert session_date_key(past_et_midnight) == "2026-04-18"


# ---------------------------------------------------------------------------
# Invariant 6 (2026-08-19 three-defect forensic fix): the SGOV/deployable-
# liquidity fix, the intraday opportunity scan, and the Tech batch-loss fix
# must each leave the accepted safety boundaries exactly where they were.
# These are deliberately cross-cutting: the per-defect suites prove the
# behavior changed correctly, these prove the boundaries did NOT.
# ---------------------------------------------------------------------------


def test_invariant_alpaca_remains_paper_only():
    """No change in this tranche may make a live-trading config loadable."""
    from src.config import AlpacaConfig

    base = {
        "api_key": "k",
        "secret_key": "s",
        "base_url": "https://paper-api.alpaca.markets",
    }
    with pytest.raises(ValueError, match="paper must be true"):
        AlpacaConfig(**base, paper=False)
    assert AlpacaConfig(**base, paper=True).paper is True


def test_invariant_cash_only_no_margin_still_enforced_on_deployable_cash():
    """The cash_only rule must still hard-block a BUY that exceeds available
    cash — and now it evaluates against the truthful deployable figure, so
    SGOV's parked value can no longer be what makes a BUY 'affordable'."""
    cfg = RiskConfig(
        max_position_pct=100.0,
        max_total_position_pct=100.0,
        require_stop_loss=True,
        allow_margin=False,
    )
    engine = RiskRuleEngine(cfg)
    decision = TradeDecision(
        action="BUY",
        symbol="NVDA",
        allocation_pct=50.0,  # $5,000 of a $10k book
        entry_price=100.0,
        stop_loss=95.0,
        take_profit=110.0,
        reasoning="more than deployable cash covers",
    )
    violations = engine.check(
        decision=decision,
        positions=[],
        total_value=10_000.0,
        cash=145.0,  # the forensic's real deployable figure
    )
    assert "cash_only" in {v.rule for v in violations}
    assert "cash_only" in HARD_BLOCK_RULES


def test_invariant_intraday_scan_cannot_bypass_the_deterministic_gate():
    """The intraday scan must route through the SAME RiskStage the morning
    run uses — it must never call ExecutionStage directly. A RiskStage
    early-exit (deterministic gate block / RM rejection) must stop the scan
    before any order is submitted."""
    import tempfile
    from pathlib import Path as _Path
    from types import SimpleNamespace
    from src.config import IntradayScanConfig

    p = build_pipeline(
        news_provider=MagicMock(),
        news_analyst=MagicMock(),
        earnings_provider=MagicMock(),
        earnings_analyst=MagicMock(),
        broker=MagicMock(),
        db=MagicMock(),
        market=MagicMock(),
        macro_store=MagicMock(),
        news_store=MagicMock(),
        tech_store=MagicMock(),
        tech_analyst=MagicMock(),
        decision_stage=MagicMock(),
        risk_stage=MagicMock(),
        execution_stage=MagicMock(),
    )
    p.config = SimpleNamespace(
        trading=SimpleNamespace(universe=["AAPL"], lookback_days=100),
        storage=SimpleNamespace(
            db_path=str(_Path(tempfile.mkdtemp()) / "t.db"),
        ),
        intraday_scan=IntradayScanConfig(enabled=True),
        event_risk=SimpleNamespace(horizon_days=10),
    )
    p._atr_for_symbol = MagicMock(return_value=2.0)  # the scan ranks by move / own ATR
    p.broker.get_intraday_snapshots.return_value = {
        # `last_trade_at`/`session_bar_at` are board item 120: a payload
        # with no timestamps is correctly not-today and buys no paid
        # look, so a fixture that means "this traded today" says so.
        "AAPL": {"last_price": 110.0, "prev_close": 100.0, "last_trade_at": todays_session_stamp()},
    }
    p.db.get_trades.return_value = []
    p.market.get_ohlcv.return_value = [MagicMock()]
    from tests.test_intraday_scan import _todays_macro_state
    from src.trading_calendar import et_today

    p.macro_store.load_last_state.return_value = _todays_macro_state()
    from src.models import MacroNarrative, NewsIntelligenceReport

    p.news_store.load_daily_report.return_value = NewsIntelligenceReport(
        macro_narrative=MacroNarrative(
            last_updated=str(et_today()),
            era_themes=["test"],
            current_regime="risk-on, test",
        ),
        state_changes=[],
        stock_news={},
        pm_briefing="test",
        market_sentiment="bullish",
        confidence="medium",
    ).model_dump()
    p.tech_store.load.return_value = {}
    p.tech_store.compute_ages.return_value = {}

    from src.models import TechAnalysisResult, TechReasoningChain

    analysis = TechAnalysisResult(
        symbol="AAPL",
        rating="buy",
        conviction="high",
        entry_price=100.0,
        stop_loss=95.0,
        reference_target=115.0,
        support_levels=[95.0],
        resistance_levels=[115.0],
        setup_type="range",
        expected_horizon_sessions=10,
        reasoning_chain=TechReasoningChain(
            trend="x",
            momentum="x",
            volatility="x",
            volume="x",
            support_resistance="x",
        ),
        reasoning="test",
        thesis_invalid_if="closes below support",
    )
    p.tech_analyst.analyze_batch.return_value = (
        {"AAPL": analysis},
        MagicMock(
            user_message="m", raw_text="{}", tokens_used=1, input_tokens=1, output_tokens=1, cost_usd=0.0, model="t"
        ),
    )
    p.decision_stage.run.side_effect = lambda ctx: setattr(
        ctx,
        "portfolio_decision",
        SimpleNamespace(decisions=[SimpleNamespace(action="BUY", symbol="AAPL")]),
    )
    # Deterministic gate blocks everything — RiskStage's existing early-exit.
    p.risk_stage.run.return_value = {
        "status": "hard_risk_block",
        "orders": [],
        "reason": "cash_only",
    }

    ctx = RunContext.start("intra_check")
    with patch("src.pipeline_intraday.compute_indicators", return_value=MagicMock()):
        result = p._run_intraday_opportunity_scan(ctx)

    assert result["status"] == "hard_risk_block"
    p.risk_stage.run.assert_called_once_with(ctx)
    p.execution_stage.run.assert_not_called(), ("a deterministic-gate block must stop the scan before execution")


def test_invariant_intraday_scan_reuses_shared_stages_not_its_own_chain():
    """Structural guarantee: `_run_intraday_opportunity_scan` must drive the
    pipeline's shared DecisionStage/RiskStage/ExecutionStage objects —
    the same instances run_morning uses — rather than constructing its own
    decision path that could drift from the audited one."""
    import inspect

    # The scan is a thin concurrency-guard wrapper delegating to a body;
    # inspect both so the invariant can't be dodged by moving code between
    # them.
    from tests.intraday_sources import scan_source

    src = scan_source("_run_intraday_opportunity_scan", "_intraday_opportunity_scan_body", "_intraday_scan_call")
    assert "self.decision_stage.run(ctx)" in src
    assert "self.risk_stage.run(ctx)" in src
    assert "self.execution_stage.run(ctx)" in src
    # ...and must not re-implement PM/RM calls itself.
    assert "portfolio_manager.decide" not in src
    assert "risk_manager.review" not in src


def test_invariant_intraday_scan_adds_no_shorting_or_margin_path():
    """The intraday path must express bearish views only through the
    already-approved inverse ETFs in the configured universe — never by
    emitting a short/sell-to-open action or enabling margin."""
    from tests.intraday_sources import scan_source

    src = scan_source(
        "_run_intraday_opportunity_scan",
        "_intraday_opportunity_scan_body",
        "_intraday_scan_mover_candidates",
        "_intraday_held_tech_symbols",
    )
    for forbidden in ("sell_short", "short_sell", "allow_margin", "SHORT"):
        assert forbidden not in src, f"intraday scan must not reference {forbidden}"
    # Candidates come from the configured universe only — no ad-hoc symbols.
    assert "self.config.trading.universe" in src


def test_no_test_file_defines_the_same_test_twice():
    """A duplicate test name silently shadows the earlier copy.

    Found 2026-10-01: `tests/test_rr_gates_retired.py` defined
    `test_risk_seat_not_told_thin_ratio_was_paid_for_in_size` twice, so one
    copy never ran. Nothing failed and nothing warned — the protection was
    simply absent while the suite reported green, which is the same shape as
    a recording that records nothing. Python rebinds the name, pytest
    collects only the survivor, and a copy-paste during an edit is all it
    takes. This reads the abstract syntax tree rather than the collected
    suite, because the whole point is to see the definition pytest will
    throw away.
    """
    import ast
    import collections
    from pathlib import Path

    root = Path(__file__).resolve().parent
    duplicates = []
    for path in sorted(root.glob("test_*.py")):
        try:
            tree = ast.parse(path.read_text())
        except SyntaxError:  # a file that cannot parse fails elsewhere
            continue
        counts = collections.Counter(
            node.name
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test_")
        )
        duplicates.extend(
            f"{path.name}: {name} defined {count} times" for name, count in sorted(counts.items()) if count > 1
        )

    assert not duplicates, (
        "these test names are defined more than once in one file, so every "
        "copy but the last is collected by nobody and guards nothing: " + "; ".join(duplicates)
    )
