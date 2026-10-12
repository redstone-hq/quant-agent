"""Idle-cash sweep (SGOV parking) invariants.

The sweep vehicle is CASH-EQUIVALENT everywhere:
  1. Hidden from LLM views (split_positions) and excluded from
     net-exposure math.
  2. force_delever liquidates it FIRST (tier -1, before real longs).
  3. _reconcile_stop_coverage never flags it (deliberately stopless).
  4. fund_buys releases parked cash before the BUY phase; park_excess
     parks only cash above reserve + open-BUY holds.
  5. Disabled / unconfigured / MagicMock'd pipelines are structural no-ops.

Cash-equivalent DOES mean spendable this session, but only once the sale
actually fills. Verified Alpaca semantics (2026-08-19): `cash` is credited
as soon as a SELL fills — settlement at T+1 gates only withdrawal/transfer
and the non-marginable (crypto) figure — so a filled SGOV liquidation
genuinely funds an equity BUY the same session.

The real defect was never the crediting; it was assuming the sale filled.
`fund_buys` now reports the CONFIRMED rise in raw broker cash rather than
the notional of the order it submitted, and `_compute_deployable_cash`
(cash + convertible sweep value, never a margin buying-power field) is the
planning figure PM/RM/the pre-trade gate see. ExecutionStage's raw-cash
recheck remains the final authority.
"""

from tests.fakes.held_book import hold
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import src.execution.cash_sweep as cash_sweep_module
from src.config import CashSweepConfig
from tests.pipeline_factory import build_pipeline


@pytest.fixture(autouse=True)
def _fast_funding_budgets(monkeypatch):
    """Shrink the funding-confirmation budgets so tests never real-sleep.

    Production values (180s terminal wait, 30s cash-settle poll) exist for
    the 2026-08-19 slow-fill incident; with MagicMock brokers the poll loop
    would otherwise spin the full window in tests that exercise the
    unconfirmed path."""
    monkeypatch.setattr(cash_sweep_module, "_FUND_TERMINAL_TIMEOUT_S", 0.05)
    monkeypatch.setattr(cash_sweep_module, "_FUND_CASH_SETTLE_TIMEOUT_S", 0.05)
    monkeypatch.setattr(cash_sweep_module, "_FUND_CASH_SETTLE_POLL_S", 0.01)


from src.execution.cash_sweep import CashSweeper
from src.models import Position, TradeDecision
from src.pipeline import TradingPipeline
from src.pipeline_context import RunContext
from src.risk.rules import RiskRuleEngine
from src.config import RiskConfig


SGOV = Position(
    symbol="SGOV",
    qty=800,
    avg_entry=100.5,
    current_price=100.6,
    market_value=80_480,
    unrealized_pnl=80,
    sector="Unknown",
)
NVDA = Position(
    symbol="NVDA", qty=10, avg_entry=900, current_price=950, market_value=9_500, unrealized_pnl=500, sector="Technology"
)


def _sweep_pipeline(enabled=True):
    pipeline = build_pipeline(broker=MagicMock(), db=MagicMock())
    pipeline.config = SimpleNamespace(
        cash_sweep=CashSweepConfig(
            enabled=enabled,
            symbol="SGOV",
        ),
        risk=RiskConfig(
            max_position_pct=20,
            max_total_position_pct=90,
            require_stop_loss=True,
            allow_margin=False,
        ),
    )
    pipeline.cash_sweeper = CashSweeper(pipeline=pipeline)
    pipeline.risk_engine = RiskRuleEngine(pipeline.config.risk)
    return pipeline


# ---------- views ----------


def test_split_positions_hides_vehicle():
    p = _sweep_pipeline()
    investable, parked = p.cash_sweeper.split_positions([SGOV, NVDA])
    assert [x.symbol for x in investable] == ["NVDA"]
    assert parked is not None and parked.symbol == "SGOV"
    assert p.cash_sweeper.parked_value([SGOV, NVDA]) == SGOV.market_value


def test_split_positions_passthrough_when_disabled():
    p = _sweep_pipeline(enabled=False)
    investable, parked = p.cash_sweeper.split_positions([SGOV, NVDA])
    assert investable == [SGOV, NVDA]
    assert parked is None
    assert p._sweeper() is None


def test_sweeper_none_for_bare_new_pipeline():
    """__new__-built pipelines (no cash_sweeper attr) degrade to disabled."""
    pipeline = build_pipeline()
    assert pipeline._sweeper() is None


def test_magicmock_config_reads_as_disabled():
    """MagicMock auto-attrs are truthy — enabled() must use `is True`."""
    pipeline = build_pipeline()
    pipeline.config = MagicMock()
    pipeline.cash_sweeper = CashSweeper(pipeline=pipeline)
    assert pipeline.cash_sweeper.enabled() is False
    assert pipeline._sweeper() is None


# ---------- risk filter: parked value is cash, not exposure ----------


def _buy(symbol="AAPL", alloc=10.0):
    return TradeDecision(
        action="BUY",
        symbol=symbol,
        allocation_pct=alloc,
        entry_price=100.0,
        stop_loss=95.0,
        take_profit=120.0,
        reasoning="test",
        thesis_invalid_if="closes below support",
    )


def test_filter_does_not_itself_credit_parked_value_as_cash():
    """The gate must take the `cash` figure it is GIVEN and not inflate it
    further. Crediting the sweep vehicle is the caller's job
    (`_compute_deployable_cash`), done once; the gate double-counting it
    on top would re-introduce approvals execution cannot fund. Here the
    caller passed a deliberately un-credited $1k, so a $10k BUY must be
    blocked."""
    p = _sweep_pipeline()
    # $100k book: $9.5k NVDA, $80.5k SGOV, $1k deployable cash. 10% BUY = $10k.
    allowed, _, blocked = p.risk_gate._filter_hard_risk_decisions(
        [_buy(alloc=10.0)],
        [SGOV, NVDA],
        total_value=100_000.0,
        cash=1_000.0,
    )
    assert allowed == [], "SGOV's value must not fund a BUY the gate approves"
    assert any("cash" in r for r in blocked)


def test_filter_blocks_same_buy_when_sweep_disabled():
    p = _sweep_pipeline(enabled=False)
    allowed, _, blocked = p.risk_gate._filter_hard_risk_decisions(
        [_buy(alloc=10.0)],
        [SGOV, NVDA],
        total_value=100_000.0,
        cash=1_000.0,
    )
    assert allowed == []
    assert any("cash" in r for r in blocked)


def test_filter_excludes_vehicle_from_net_exposure():
    """80% parked + 9.5% stock must not trip the 90% net-exposure cap for a
    new BUY — parked cash is not market exposure."""
    p = _sweep_pipeline()
    allowed, _, blocked = p.risk_gate._filter_hard_risk_decisions(
        [_buy(alloc=15.0)],
        [SGOV, NVDA],
        total_value=100_000.0,
        cash=20_000.0,
    )
    assert [d.symbol for d in allowed] == ["AAPL"], blocked


# ---------- force_delever: vehicle first ----------


def test_force_delever_sells_vehicle_before_real_longs():
    p = _sweep_pipeline()
    p.broker.submit_order.return_value = {"id": "o1", "status": "accepted"}
    p.broker.wait_for_order_terminal.return_value = "filled"
    p.broker.snapshot_protective_stops.return_value = (True, [])
    p.broker.cancel_snapshotted_stops.return_value = MagicMock(cleared=True)
    p.broker.get_account.return_value = {
        "cash": 100.0,
        "portfolio_value": 90_000.0,
        "last_equity": 90_000.0,
    }
    p.broker.get_positions.return_value = []
    p.broker.get_order_fill_info.return_value = {"fill_qty": 800, "status": "filled"}

    ctx = RunContext.start("morning")
    ctx.cash = -500.0
    loser = Position(
        symbol="LOSER", qty=5, avg_entry=300, current_price=250, market_value=1_250, unrealized_pnl=-250, sector="Tech"
    )
    ctx.positions = [loser, SGOV]

    hold(p.broker, {x.symbol: x.qty for x in ctx.positions})  # what the broker holds when the exit is re-sized
    p._force_delever(ctx)
    first = p.broker.submit_order.call_args_list[0].kwargs
    assert first["symbol"] == "SGOV"  # parked cash first, not the loser


# ---------- stop-coverage audit exemption ----------


def test_reconcile_stop_coverage_skips_vehicle():
    p = _sweep_pipeline()
    p.broker.get_positions.return_value = [SGOV]
    p.db.get_pending_protection_restores.return_value = []
    # No stops exist for SGOV; without the exemption this would be a gap.
    p.broker.snapshot_protective_stops.return_value = (True, [])
    gaps = p._reconcile_stop_coverage()
    assert gaps == []
    p.broker.snapshot_protective_stops.assert_not_called()


# ---------- fund_buys ----------


def _funding_pipeline():
    p = _sweep_pipeline()
    p._submit_protected_sell = MagicMock(
        return_value=(
            {"id": "sell-1", "status": "accepted"},
            {"symbol": "SGOV", "order_id": "sell-1"},
        )
    )
    p._finalize_pending_protections = MagicMock()
    p.broker.get_account.return_value = {
        "cash": 50_000.0,
        "portfolio_value": 100_000.0,
    }
    p.broker.get_positions.return_value = [NVDA]
    return p


# ---------- park_excess ----------


def _parking_pipeline(cash=90_000.0, total=100_000.0, pending=0.0):
    p = _sweep_pipeline()
    p.broker.get_account.return_value = {"cash": cash, "portfolio_value": total}
    p.broker.get_positions.return_value = [NVDA]
    p.broker.open_buy_notional.return_value = pending
    p.broker.get_latest_price.return_value = 100.60
    p.broker.submit_order.return_value = {"id": "buy-1", "status": "accepted"}
    p.db.insert_trade.return_value = 42
    p._order_accepted = MagicMock(return_value=True)
    return p


# ---------- config ----------


def test_cash_sweep_config_defaults_disabled():
    cfg = CashSweepConfig()
    assert cfg.enabled is False
    assert cfg.symbol == "SGOV"


def test_cash_sweep_config_uppercases_symbol():
    assert CashSweepConfig(symbol=" bil ").symbol == "BIL"


# ---------- session integration: reviewer never sees the vehicle; midday parks ----------


# ---------- defect (e): capped per-symbol news must not spend a slot on the ----------
# ---------- parked cash-equivalent vehicle, in ANY of the three same-day    ----------
# ---------- session paths (midday, close, evening)                         ----------
#
# 2026-08-31 fix: `run_position_review` (shared by run_midday/run_close) built
# its `held_symbols` list for `_run_news_update` straight from the broker's
# UNFILTERED position snapshot — the SGOV/cash-sweep vehicle rode along and
# could consume one of `config.news.per_symbol_max_symbols`' capped slots that
# would otherwise go to a real position. `run_evening` already filtered via
# `sweeper.split_positions` before this point; midday/close did not. Both now
# go through the single shared `TradingPipeline._news_held_symbols` helper —
# same mechanism (`CashSweeper.split_positions`, keyed on `cash_sweep.symbol`,
# "SGOV" by default) evening already used, lifted so the three paths cannot
# drift apart again.


def _position_review_fixture(tmp_path):
    """Same fixture as test_position_review_hides_vehicle_and_parks_at_end,
    factored out so both run_midday and run_close can drive it."""
    from src.models import PositionReview, PositionReasoningChain
    from src.storage.db import Database

    db = Database(str(tmp_path / "t.db"))
    db.initialize()

    p = _sweep_pipeline()
    p.db = db
    p.broker.is_trading_day.return_value = True
    p.broker.get_session_close = MagicMock(return_value=None)
    p.broker.get_account.return_value = {
        "cash": 85_000.0,
        "portfolio_value": 100_000.0,
        "last_equity": 100_000.0,
    }
    p.broker.get_positions.return_value = [SGOV, NVDA]
    p.broker.open_buy_notional.return_value = 0.0
    p.broker.get_latest_price.return_value = 100.60
    p.broker.submit_order.return_value = {"id": "sweep-1", "status": "accepted"}
    p.broker.snapshot_protective_stops.return_value = (True, [])
    p.macro = MagicMock()
    p.macro.get_macro_summary.return_value = {}
    p.macro_store = MagicMock()
    p.macro_store.load_last_state.return_value = None
    p.config.llm = MagicMock()
    p.config.llm.position_reviewer_model = "test-model"
    p._handle_ex_dividends = MagicMock(return_value=[])
    p._run_news_update = MagicMock(return_value=(None, None))
    p._load_earnings_analyses = MagicMock(return_value=(None, []))
    p._midday_execute_llm_actions = MagicMock(return_value=[])
    p._reconcile_stop_coverage = MagicMock(return_value=[])
    p.risk_engine = MagicMock()
    p.position_reviewer = MagicMock()
    p.position_reviewer.review.return_value = (
        PositionReview(
            reasoning_chain=PositionReasoningChain(
                macro_continuity_check="x",
                thesis_progress_check="x",
                thesis_integrity_check="x",
                winners_discipline_check="x",
                session_disposition_check="x",
                execution_rationale="x",
            ),
            actions=[],
            overall_assessment="stable",
            risk_level="low",
        ),
        MagicMock(
            user_message="m",
            raw_text="{}",
            tokens_used=1,
            input_tokens=1,
            output_tokens=1,
            cost_usd=0.0,
            model="test-model",
            requested_provider="anthropic",
            requested_model="test-model",
            actual_provider="anthropic",
            used_fallback=False,
            prompt_version="test-version",
            latency_s=0.1,
            finish_reason=None,
            truncated=False,
        ),
    )
    return p


def test_midday_news_held_symbols_excludes_sweep_vehicle(tmp_path):
    """run_midday (session_type='midday') must not hand SGOV to
    _run_news_update — it would consume one of the capped per-symbol news
    slots a real position should get."""
    p = _position_review_fixture(tmp_path)

    result = p.run_midday()

    assert result["status"] == "reviewed"
    held = p._run_news_update.call_args.kwargs["held_symbols"]
    assert held == ["NVDA"]
    assert "SGOV" not in held


def test_close_news_held_symbols_excludes_sweep_vehicle(tmp_path):
    """Same defect, same fix, the OTHER session_type run_position_review
    serves — close (15:30 ET) shares the exact same held_symbols block as
    midday, so both must be verified independently rather than assuming
    fixing one fixed the other."""
    p = _position_review_fixture(tmp_path)

    result = p.run_close()

    assert result["status"] == "reviewed"
    held = p._run_news_update.call_args.kwargs["held_symbols"]
    assert held == ["NVDA"]
    assert "SGOV" not in held


def test_evening_news_held_symbols_excludes_sweep_vehicle():
    """run_evening already filtered its LLM-facing `positions` view before
    this fix; this asserts the news call specifically still excludes SGOV
    now that it goes through the same shared `_news_held_symbols` helper as
    midday/close, not a second hand-rolled filter that could drift."""
    from src.models import EveningReport
    from tests.test_bugfixes import _valid_evening_rc

    p = _sweep_pipeline()
    p.broker.is_trading_day.return_value = True
    p.broker.get_account.return_value = {
        "cash": 15_000.0,
        "portfolio_value": 100_000.0,
        "last_equity": 99_000.0,
    }
    p.broker.get_positions.return_value = [SGOV, NVDA]
    p.broker.get_recent_daily_closes.return_value = []
    p.macro = MagicMock()
    p.macro.get_macro_summary.return_value = {}
    p.macro_store = MagicMock()
    p.macro_store.load_last_state.return_value = None
    p.news_store = MagicMock()
    p.news_store.load_daily_report.return_value = None
    p.config.llm = MagicMock()
    p.config.llm.evening_analyst_model = "test-model"
    p._run_news_update = MagicMock(return_value=(None, None))
    p._load_earnings_analyses = MagicMock(return_value=([], []))
    p._build_recent_sells_for_grading = MagicMock(return_value=[])
    p._build_recent_buys_for_grading = MagicMock(return_value=[])
    p._build_recent_outlook_calibration = MagicMock(return_value={"samples": [], "n": 0})
    p._build_weekly_narrative = MagicMock(return_value="")
    p._build_active_state_changes = MagicMock(return_value="")
    p._reconcile_fills = MagicMock()
    p.evening_analyst = MagicMock()
    p.evening_analyst.analyze.return_value = (
        EveningReport(
            reasoning_chain=_valid_evening_rc(),
            daily_summary="Up",
            lessons="n/a",
            tomorrow_outlook="Watch",
            risk_rating="low",
        ),
        MagicMock(
            user_message="m",
            raw_text="{}",
            tokens_used=1,
            input_tokens=1,
            output_tokens=1,
            cost_usd=0.0,
            model="test-model",
        ),
    )

    p.run_evening()

    held = p._run_news_update.call_args.kwargs["held_symbols"]
    assert held == ["NVDA"]
    assert "SGOV" not in held


def test_risk_stage_rm_view_excludes_vehicle():
    """Review finding: RM (the veto layer) must see parked T-bills as cash,
    not as an 84%-of-book position — otherwise PM and RM get contradictory
    views of the same dollars in the same run and RM's veto acts on the
    corrupted one."""
    from src.pipeline_stages import RiskStage
    from src.models import PortfolioDecision, ReasoningChain, RiskVerdict, RiskReasoningChain

    p = _sweep_pipeline()
    p.market = MagicMock()
    p.market.get_ohlcv.return_value = []
    p.admission = MagicMock(_filter_supported_symbols=MagicMock(side_effect=lambda d, a, pos: (d, [])))
    p.risk_manager = MagicMock()
    p.risk_manager.review.return_value = (
        RiskVerdict(
            approved=True,
            modifications=[],
            reasoning="ok",
            reasoning_chain=RiskReasoningChain(
                rr_audit="x",
                signal_fidelity="x",
                correlation_check="x",
                event_risk="x",
                sizing_sanity="x",
                overall="x",
            ),
        ),
        MagicMock(user_message="m", raw_text="{}", tokens_used=1, input_tokens=1, output_tokens=1, cost_usd=0.0),
    )
    p.db = MagicMock()
    p.risk_gate._refuse_queued_earnings_buys = MagicMock(side_effect=lambda d, e, **kw: d)
    p.risk_gate._filter_hard_risk_decisions = MagicMock(side_effect=lambda d, *a, **k: (d, [], []))
    p.config.llm = MagicMock()
    p.config.llm.risk_manager_model = "test-model"
    p.config.trading = MagicMock()
    p.config.trading.lookback_days = 120

    from src.pipeline_context import RunContext

    ctx = RunContext.start("morning")
    ctx.positions = [SGOV, NVDA]
    ctx.total_value = 100_000.0
    ctx.last_equity = 100_000.0
    ctx.cash = 10_000.0
    ctx.deployable_cash = 10_000.0
    ctx.portfolio_decision = PortfolioDecision(
        reasoning_chain=ReasoningChain(
            macro_filter="x",
            news_check="x",
            earnings_check="x",
            signal_conflicts="x",
            sizing_logic="x",
            portfolio_balance="x",
            cash_target="x",
        ),
        decisions=[_buy(alloc=5.0)],
        portfolio_view="v",
    )
    ctx.symbols_bars = {}
    ctx.data_status = {}

    RiskStage(pipeline=p).run(ctx)

    rm_seen = p.risk_manager.review.call_args.kwargs["positions"]
    assert [x.symbol for x in rm_seen] == ["NVDA"], "RM must not see SGOV"
    # but the HARD filter received the RAW list (it still needs to find the
    # vehicle to exclude it from net-exposure math)
    filter_positions = p.risk_gate._filter_hard_risk_decisions.call_args_list[0].args[1]
    assert any(x.symbol == "SGOV" for x in filter_positions)

    # 2026-08-19 SGOV/deployable-liquidity forensic: RM must receive
    # ctx.deployable_cash untouched (10,000), NEVER inflated by SGOV's
    # $80,480 market value (which the old `ctx.cash + parked_value` credit
    # would have produced: 10,000 + 80,480 = 90,480). The parked value is
    # still surfaced, but only via the separate `reserve_balance` kwarg.
    assert p.risk_manager.review.call_args.kwargs["cash"] == 10_000.0
    assert p.risk_manager.review.call_args.kwargs["reserve_balance"] == SGOV.market_value
    # and the hard gate (both pre- and post-RM calls) must be checked
    # against deployable_cash too, never ctx.cash + SGOV.
    for call in p.risk_gate._filter_hard_risk_decisions.call_args_list:
        assert call.kwargs["cash"] == 10_000.0


def test_decision_stage_pm_view_gets_deployable_cash_not_sgov_inflated():
    """2026-08-19 SGOV/deployable-liquidity forensic, PM side: the exact
    incident this regresses is PM being told ~$10K was "cash" (really
    ~$145 deployable + ~$9.8K parked SGOV), sizing BUYs against the
    inflated figure that execution could never actually fund. PM must now
    receive ctx.deployable_cash verbatim as cash_balance, with SGOV's
    value surfaced only informationally via reserve_balance."""
    from src.pipeline_stages import DecisionStage
    from src.pipeline_context import RunContext

    p = _sweep_pipeline()
    p.db = MagicMock()
    p.db.get_latest_insights.return_value = None
    p._compute_recent_performance = MagicMock(return_value={})
    p._build_position_history = MagicMock(return_value={})
    p._build_weekly_narrative = MagicMock(return_value="")
    p._build_macro_trajectory = MagicMock(return_value="")
    p._build_active_state_changes = MagicMock(return_value="")
    p._build_rm_recent_verdicts = MagicMock(return_value="")
    p._build_pm_recent_decisions = MagicMock(return_value="")
    p._build_projected_portfolio = MagicMock(return_value="")
    p._build_calibration_note = MagicMock(return_value="")
    p._build_macro_tech_alignment = MagicMock(return_value="")
    p._build_recent_missed_lessons = MagicMock(return_value="")
    p._build_recent_loss_pits = MagicMock(return_value="")
    p._build_pm_facts = MagicMock(return_value=MagicMock())
    p.portfolio_manager = MagicMock()
    # Early-return path: DecisionStage bails right after `decide()` when
    # portfolio_decision is falsy, so the assertion below doesn't need to
    # mock the constructor / evidence-persistence tail.
    p.portfolio_manager.decide.return_value = (
        None,
        MagicMock(
            user_message="m",
            raw_text="{}",
            tokens_used=1,
            input_tokens=1,
            output_tokens=1,
            cost_usd=0.0,
            model="test-model",
        ),
    )

    ctx = RunContext.start("morning")
    ctx.positions = [SGOV, NVDA]
    ctx.analyses = []
    ctx.total_value = 100_000.0
    ctx.last_equity = 100_000.0
    ctx.cash = 145.0
    ctx.deployable_cash = 145.0  # the incident's real deployable figure

    DecisionStage(pipeline=p).run(ctx)

    kwargs = p.portfolio_manager.decide.call_args.kwargs
    assert kwargs["cash_balance"] == 145.0, "PM must size against real deployable cash, not cash + SGOV"
    assert kwargs["reserve_balance"] == SGOV.market_value
    pm_positions = p.portfolio_manager.decide.call_args.kwargs["positions"]
    assert [x.symbol for x in pm_positions] == ["NVDA"], "PM must not see SGOV as a position"


# ---------- decision-time vs execution-time cash coherence ----------


def test_approved_buys_are_not_designed_around_unusable_liquidity():
    """The 2026-08-19 incident's defining symptom, regressed end-to-end:
    the deterministic gate APPROVED BUYs at decision time, then execution's
    own raw-cash recheck skipped every one of them, because the two were
    comparing against different money. The gate was crediting SGOV's
    ~$9.8K market value as spendable cash; execution (correctly) was not.

    The gate now evaluates the same truthful figure execution will see, so
    a BUY it approves is one execution can actually fund — and a BUY that
    can't be funded is blocked UP FRONT with a visible reason, rather than
    approved and then silently skipped at the broker.

    Note what is deliberately NOT changed: execution's raw-cash recheck
    remains the final authority. This test asserts the two agree, never
    that the recheck was relaxed."""
    p = _sweep_pipeline()
    # The incident's book: ~$145 truly deployable, ~$9,855 parked in SGOV.
    parked = Position(
        symbol="SGOV",
        qty=98,
        avg_entry=100.5,
        current_price=100.6,
        market_value=9_855.0,
        unrealized_pnl=10.0,
        sector="Unknown",
    )
    deployable_cash = 145.0
    total_value = 10_000.0

    # PM proposes a 20% BUY ($2,000) — comfortably covered by "cash" under
    # the old SGOV-crediting view ($145 + $9,855 = $10,000), not remotely
    # covered by what execution can actually spend.
    allowed, _violations, blocked = p.risk_gate._filter_hard_risk_decisions(
        [_buy(symbol="AAPL", alloc=20.0)],
        [parked],
        total_value=total_value,
        cash=deployable_cash,
    )

    assert allowed == [], "the gate must not approve a BUY that execution's cash recheck would then skip"
    assert any("cash" in r for r in blocked), "the block must be visible and attributed to cash, not silent"

    # And the complement: a BUY that DOES fit real deployable cash is
    # approved — the fix must not have simply blocked everything.
    small_allowed, _v, small_blocked = p.risk_gate._filter_hard_risk_decisions(
        [_buy(symbol="AAPL", alloc=1.0)],
        [parked],  # $100 of $10k book
        total_value=total_value,
        cash=deployable_cash,
    )
    assert [d.symbol for d in small_allowed] == ["AAPL"], small_blocked


# ---------- Alpaca account-field semantics (2026-08-19 Blocker 1) ----------
#
# Verified against Alpaca's official documentation:
#   - `cash` is credited as soon as a SELL FILLS ("The cash is updated post
#     the SELL trade is filled, but the cash_withdrawable and
#     cash_transferable are updated post T+1"). So a filled SGOV
#     liquidation DOES fund an equity BUY in the same session.
#   - `non_marginable_buying_power` is the settled/non-margin (crypto)
#     figure and LAGS a same-day equity sale by one business day.
#   - Every Alpaca account is a margin account; at this account's equity the
#     multiplier is 2, so `buying_power`/`regt_buying_power` are ~2x equity
#     and represent BORROWED capacity.


def test_deployable_cash_is_raw_cash_plus_convertible_sweep():
    """Deployable = cash + sweep value. Both are owned assets, so the sum
    can never exceed equity and never implies margin."""
    p = _sweep_pipeline()
    assert p._compute_deployable_cash(145.0, [SGOV, NVDA]) == 145.0 + SGOV.market_value


def test_deployable_cash_ignores_sweep_when_disabled():
    p = _sweep_pipeline(enabled=False)
    assert p._compute_deployable_cash(145.0, [SGOV, NVDA]) == 145.0


def test_deployable_cash_never_uses_margin_buying_power_fields():
    """Regression on the no-leverage boundary: whatever Alpaca reports as
    `buying_power` / `regt_buying_power` (2x equity on a margin account,
    which is every Alpaca account) must never influence sizing."""
    import inspect

    src = inspect.getsource(TradingPipeline._compute_deployable_cash)
    body = src.split('"""')[-1]  # ignore the explanatory docstring
    for forbidden in ("buying_power", "regt_buying_power", "multiplier"):
        assert forbidden not in body, f"deployable cash must never be derived from {forbidden}"


def test_deployable_cash_never_exceeds_owned_assets():
    """No-leverage invariant stated numerically: deployable is bounded by
    cash + the sweep vehicle, so it cannot reach into margin."""
    p = _sweep_pipeline()
    cash, equity = 145.0, 10_000.0
    deployable = p._compute_deployable_cash(cash, [SGOV, NVDA])
    assert deployable == cash + SGOV.market_value
    # SGOV ($80,480 in this fixture) + cash is what's owned; the point is
    # that a 2x margin figure (2 * equity) is never reachable.
    assert deployable < 2 * (cash + SGOV.market_value + NVDA.market_value)


def test_deployable_cash_fails_closed_on_non_finite_cash():
    p = _sweep_pipeline()
    assert p._compute_deployable_cash(float("nan"), [SGOV]) == 0.0


# ---------- sweep retired (owner mandate 2026-09-17: fully invested) ----------
#
# `cash_sweep.enabled: false` turns every other hook inert — fund_buys no
# longer releases the vehicle, park_excess no longer buys it and
# split_positions stops hiding it. SGOV bought before the switch must not be
# left as a stranded, stopless, thesis-less holding: it is sold whole at the
# start of the next market-hours session.


def _retired_pipeline():
    p = _sweep_pipeline(enabled=False)
    p._submit_protected_sell = MagicMock(
        return_value=(
            {"id": "release-1", "status": "accepted"},
            {"symbol": "SGOV", "order_id": "release-1"},
        )
    )
    p._finalize_pending_protections = MagicMock()
    p.broker.get_positions.return_value = [SGOV, NVDA]
    return p


def test_retired_sweep_releases_whole_held_vehicle_into_cash():
    p = _retired_pipeline()

    order = p.cash_sweeper.release_retired_vehicle(run_id="run-x")

    assert order == {"id": "release-1", "status": "accepted"}
    kwargs = p._submit_protected_sell.call_args.kwargs
    assert kwargs["symbol"] == "SGOV"
    assert kwargs["qty"] == SGOV.qty  # the whole position, not a slice
    assert kwargs["position_qty_before_sell"] == SGOV.qty
    assert kwargs["label"] == "SWEEP_SELL"  # ledger isolation holds
    trade = p.db.insert_trade.call_args.kwargs
    assert trade["action"] == "SWEEP_SELL" and trade["symbol"] == "SGOV"
    assert trade["run_id"] == "run-x"
    p._finalize_pending_protections.assert_called_once()


def test_retired_release_is_noop_while_sweep_enabled():
    p = _retired_pipeline()
    p.config.cash_sweep = CashSweepConfig(enabled=True, symbol="SGOV")
    assert p.cash_sweeper.release_retired_vehicle(run_id="r") is None
    p._submit_protected_sell.assert_not_called()


def test_retired_release_is_noop_when_nothing_held():
    p = _retired_pipeline()
    p.broker.get_positions.return_value = [NVDA]
    assert p.cash_sweeper.release_retired_vehicle(run_id="r") is None
    p._submit_protected_sell.assert_not_called()


def test_retired_release_survives_broker_failure():
    p = _retired_pipeline()
    p.broker.get_positions.side_effect = ConnectionError("down")
    p._release_retired_cash_park("r")  # must not raise
    p._submit_protected_sell.assert_not_called()


def test_reconcile_stop_coverage_still_skips_vehicle_awaiting_release():
    """Held SGOV after the switch is awaiting release, not naked — no red
    banner, and no stop-repair attempt on a SWEEP_BUY opening row."""
    p = _sweep_pipeline(enabled=False)
    p.broker.get_positions.return_value = [SGOV]
    p.db.get_pending_protection_restores.return_value = []
    p.broker.snapshot_protective_stops.return_value = (True, [])
    assert p._reconcile_stop_coverage() == []
    p.broker.snapshot_protective_stops.assert_not_called()


def test_disabled_sweep_releases_held_sgov_on_next_session(tmp_path):
    """(b) End-to-end through run_midday with the sweep OFF and SGOV still
    held: the session releases it (one full SWEEP_SELL) and parks nothing."""
    p = _position_review_fixture(tmp_path)
    p.config.cash_sweep = CashSweepConfig(enabled=False, symbol="SGOV")
    p._submit_protected_sell = MagicMock(
        return_value=(
            {"id": "release-1", "status": "accepted"},
            {"symbol": "SGOV", "order_id": "release-1"},
        )
    )
    p._finalize_pending_protections = MagicMock()

    result = p.run_midday()

    assert result["status"] == "reviewed"
    sells = [c.kwargs for c in p._submit_protected_sell.call_args_list if c.kwargs.get("symbol") == "SGOV"]
    assert len(sells) == 1
    assert sells[0]["qty"] == SGOV.qty and sells[0]["label"] == "SWEEP_SELL"
    assert not any(o.get("action") == "SWEEP_BUY" for o in result["orders"])
    p.broker.submit_order.assert_not_called()  # no parking buy
