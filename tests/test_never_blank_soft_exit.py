"""Never-blank soft-exit: stated strings survive; missing names are refused.

The #432 isolate-unknown-only gate is a temporary last-resort. The product
is: require a real thesis_invalid_if on actionable Tech and on
opens/increases before the ticket book; one mechanical heal + one paid
retry; if still incomplete, refuse that open name with
`soft-exit missing after retry`. A blank-falsifier reduction is not a
soft-exit: SELL/COVER fires only when a mechanical size-down vs the live
book is checkable AND that warrant is the named trigger. PM thesis free
text explains; it cannot create the sell. Never invent a falsifier or
catalyst string. Catalyst stays optional. Missing symbol-specific news
alone stays warn/log, not a warrant.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from pydantic import ValidationError

from src.models import (
    SOFT_EXIT_MISSING_AFTER_RETRY,
    SOFT_EXIT_UNKNOWN,
    TargetPosition,
    TechAnalysisResult,
    TradeDecision,
    missing_stated_falsifier,
    stated_soft_exit,
)
from src.pipeline_context import RunContext
from src.pipeline_stages import _isolate_empty_soft_exit_entries
from src.seat_heal import merge_retry_falsifiers, restore_stated_soft_exits


def _trc() -> dict:
    return dict(trend="t", momentum="m", volatility="v", volume="vol", support_resistance="sr")


def _tech(**over) -> dict:
    base = dict(
        symbol="SPY",
        rating="buy",
        conviction="high",
        entry_price=500.0,
        stop_loss=490.0,
        reference_target=525.0,
        support_levels=[490.0],
        resistance_levels=[525.0],
        setup_type="range",
        expected_horizon_sessions=10,
        reasoning="x",
        reasoning_chain=_trc(),
        thesis_invalid_if="closes below 490",
    )
    base.update(over)
    return base


def test_stated_falsifier_and_catalyst_survive_assignment_before_risk():
    """A later size cap must not wipe stated soft-exits to blank."""
    target = TargetPosition(
        symbol="AAPL",
        risk_allocation_pct=2.0,
        conviction="medium",
        thesis="add",
        thesis_invalid_if="closes below 191.5",
        catalyst="2026-09-16 8-K",
    )
    target.risk_allocation_pct = 0.5
    assert target.thesis_invalid_if == "closes below 191.5"
    assert target.catalyst == "2026-09-16 8-K"
    decision = TradeDecision(
        action="BUY",
        symbol="AAPL",
        allocation_pct=3.0,
        entry_price=190.0,
        stop_loss=185.0,
        take_profit=205.0,
        reasoning="stated",
        thesis_invalid_if=stated_soft_exit(target.thesis_invalid_if) or None,
    )
    plan = SimpleNamespace(
        decisions=[decision],
        targets=[target],
        constructor_dropped=[],
    )
    pipeline = SimpleNamespace(db=MagicMock())
    ctx = RunContext.start("morning")
    isolated = _isolate_empty_soft_exit_entries(pipeline, ctx, plan)
    assert isolated == []
    assert plan.decisions[0].thesis_invalid_if == "closes below 191.5"
    assert plan.targets[0].catalyst == "2026-09-16 8-K"


def test_heal_restores_stated_falsifier_null_is_not_a_delete():
    values = {"thesis_invalid_if": None, "catalyst": ""}
    raw = {"thesis_invalid_if": "daily close below 191.5", "catalyst": "8-K"}
    out, restored = restore_stated_soft_exits(values, raw)
    assert out["thesis_invalid_if"] == "daily close below 191.5"
    assert out["catalyst"] == "8-K"
    assert "thesis_invalid_if" in restored
    # Heal never invents when the seat wrote nothing.
    empty, restored_empty = restore_stated_soft_exits(
        {"thesis_invalid_if": ""},
        {"thesis_invalid_if": None},
    )
    assert empty["thesis_invalid_if"] == ""
    assert restored_empty == []


def test_empty_open_name_is_refused_with_soft_exit_missing_after_retry():
    """Last-resort refuse after heal+retry. Empty on a BUY is missing."""
    empty_buy = TradeDecision(
        action="BUY",
        symbol="MRVL",
        allocation_pct=3.0,
        entry_price=80.0,
        stop_loss=75.0,
        take_profit=90.0,
        reasoning="blank",
        thesis_invalid_if=None,
    )
    stated_buy = TradeDecision(
        action="BUY",
        symbol="AAPL",
        allocation_pct=3.0,
        entry_price=190.0,
        stop_loss=185.0,
        take_profit=205.0,
        reasoning="stated",
        thesis_invalid_if="closes below 185",
    )
    plan = SimpleNamespace(
        decisions=[empty_buy, stated_buy],
        targets=[
            TargetPosition(
                symbol="MRVL",
                risk_allocation_pct=1.0,
                thesis="retry",
                thesis_invalid_if="",
            ),
            TargetPosition(
                symbol="AAPL",
                risk_allocation_pct=1.0,
                thesis="add",
                thesis_invalid_if="closes below 185",
            ),
        ],
        constructor_dropped=[],
    )
    pipeline = SimpleNamespace(db=MagicMock())
    ctx = RunContext.start("morning")
    isolated = _isolate_empty_soft_exit_entries(pipeline, ctx, plan)
    assert isolated == ["MRVL"]
    assert [d.symbol for d in plan.decisions] == ["AAPL"]
    # Target stays on the proposal so Risk is told the name was refused,
    # not silently deleted from both lists.
    assert [t.symbol for t in plan.targets] == ["MRVL", "AAPL"]
    assert any(SOFT_EXIT_MISSING_AFTER_RETRY in str(c) for c in pipeline.db.insert_specialist_evidence.mock_calls)


def test_catalyst_stays_optional_on_a_non_zero_target():
    target = TargetPosition(
        symbol="NVDA",
        risk_allocation_pct=1.0,
        thesis="breakout",
        thesis_invalid_if="closes back inside the range",
        catalyst="",
    )
    assert target.catalyst == ""
    assert not target.missing_open_falsifier


def test_close_target_may_omit_falsifier():
    close = TargetPosition(
        symbol="AAPL",
        risk_allocation_pct=0.0,
        thesis="exit",
        thesis_invalid_if="",
    )
    assert close.is_close
    assert not close.missing_open_falsifier


def test_paid_retry_merge_does_not_change_size_or_invent_catalyst():
    original = [
        TargetPosition(
            symbol="AAPL",
            risk_allocation_pct=1.0,
            thesis="add",
            thesis_invalid_if="",
        ),
    ]
    retry = [
        {
            "symbol": "AAPL",
            "risk_allocation_pct": 5.0,
            "thesis_invalid_if": "closes below 191.5",
            "catalyst": "made-up 8-K",
        }
    ]
    merged, filled = merge_retry_falsifiers(original, retry)
    assert filled == ["AAPL"]
    assert merged[0].risk_allocation_pct == 1.0
    assert merged[0].thesis_invalid_if == "closes below 191.5"
    assert merged[0].catalyst == ""


def test_actionable_tech_without_falsifier_does_not_validate():
    # Recorded as a gap, not rejected, since 2026-10-12 (XOM incident).
    assert "thesis_invalid_if" in TechAnalysisResult(**_tech(thesis_invalid_if="")).missing_levels
    assert "thesis_invalid_if" in TechAnalysisResult(**_tech(thesis_invalid_if=SOFT_EXIT_UNKNOWN)).missing_levels
    ok = TechAnalysisResult(**_tech())
    assert stated_soft_exit(ok.thesis_invalid_if)


def test_pm_fill_retry_copies_stated_falsifier_and_never_invents():
    """One paid seat retry fills an empty open target from the retry
    payload. Size and catalyst are untouched. A still-empty retry leaves
    the name missing for the book-entry refuse."""
    from src.agents.base import AgentResult
    from src.agents.portfolio_manager import PortfolioManagerAgent
    from src.models import PortfolioDecision, ReasoningChain

    def _plan(*, falsifier: str) -> PortfolioDecision:
        return PortfolioDecision(
            reasoning_chain=ReasoningChain(
                macro_filter="m",
                news_check="n",
                earnings_check="e",
                signal_conflicts="s",
                sizing_logic="z",
                portfolio_balance="b",
                cash_target="c",
            ),
            targets=[
                TargetPosition(
                    symbol="AAPL",
                    risk_allocation_pct=1.0,
                    thesis="add",
                    thesis_invalid_if=falsifier,
                    catalyst="",
                )
            ],
            portfolio_view="v",
        )

    agent = PortfolioManagerAgent.__new__(PortfolioManagerAgent)
    first = AgentResult(
        raw_text="{}",
        tokens_used=1,
        model="test",
        user_message="original user message",
    )
    retry_payload = {
        "targets": [
            {
                "symbol": "AAPL",
                "risk_allocation_pct": 9.0,
                "thesis_invalid_if": "closes below 191.5",
                "catalyst": "invented 8-K",
            }
        ],
    }
    calls: list[tuple] = []

    def _execute(self, user_message, **kwargs):
        calls.append(
            (user_message, kwargs.get("retry_kind"), kwargs.get("optional_retry")),
        )
        import json

        return AgentResult(
            raw_text=json.dumps(retry_payload),
            tokens_used=1,
            model="test",
            user_message=user_message,
        )

    agent._execute = _execute.__get__(agent, PortfolioManagerAgent)
    filled, _ = agent._fill_missing_open_falsifiers(_plan(falsifier=""), first)
    assert [c[1] for c in calls] == ["soft_exit_fill"]
    assert calls[0][2] is True
    assert "SOFT-EXIT COMPLETION REQUIRED" in calls[0][0]
    assert filled.targets[0].thesis_invalid_if == "closes below 191.5"
    assert filled.targets[0].risk_allocation_pct == 1.0
    assert filled.targets[0].catalyst == ""

    agent2 = PortfolioManagerAgent.__new__(PortfolioManagerAgent)

    def _empty_retry(self, user_message, **kwargs):
        import json

        return AgentResult(
            raw_text=json.dumps({"targets": [{"symbol": "AAPL", "thesis_invalid_if": ""}]}),
            tokens_used=1,
            model="test",
            user_message=user_message,
        )

    agent2._execute = _empty_retry.__get__(agent2, PortfolioManagerAgent)
    still, _ = agent2._fill_missing_open_falsifiers(_plan(falsifier=""), first)
    assert still.targets[0].missing_open_falsifier
    assert still.targets[0].thesis_invalid_if == ""


def test_blank_open_target_is_refused_before_the_constructor():
    """A missing falsifier never consumes risk budget as a ticket."""
    from src.pipeline_stages import (
        _dropped_since_proposal,
        _targets_admitted_to_book,
    )

    blank = TargetPosition(
        symbol="MRVL",
        risk_allocation_pct=1.0,
        thesis="retry",
        thesis_invalid_if="",
    )
    stated = TargetPosition(
        symbol="AAPL",
        risk_allocation_pct=1.0,
        thesis="add",
        thesis_invalid_if="closes below 185",
    )
    admitted, refused = _targets_admitted_to_book([blank, stated])
    assert refused == ["MRVL"]
    assert [t.symbol for t in admitted] == ["AAPL"]
    plan = SimpleNamespace(
        decisions=[],
        targets=[blank, stated],
        constructor_dropped=["MRVL"],
    )
    assert "MRVL" in _dropped_since_proposal(plan)


def _held(symbol: str, qty: float, price: float, *, sector="Technology"):
    from src.models import Position

    return Position(
        symbol=symbol,
        qty=qty,
        avg_entry=price,
        current_price=price,
        market_value=qty * price,
        unrealized_pnl=0.0,
        sector=sector,
    )


def test_build_sell_free_text_without_checkable_size_down_is_not_a_sell():
    """Direct builder: free-text thesis and blank falsifier cannot
    create a SELL when weight is not actually down vs the live book.
    """
    from src.portfolio_constructor import PortfolioConstructor

    pos = _held("AAPL", 10, 100.0)
    target = TargetPosition(
        symbol="AAPL",
        risk_allocation_pct=1.0,
        thesis="trim to fund NET",
        thesis_invalid_if="",
    )
    assert PortfolioConstructor._build_sell(target, pos, 5.0, 5.0) is None
    assert PortfolioConstructor._build_sell(target, pos, 5.0, 8.0) is None


def test_mechanical_size_down_is_not_a_midday_hard_trigger():
    """Do not add the constructor phrase to the midday substring gate.
    A reviewer LLM could emit it with nothing behind it.
    """
    from src.pipeline_exits import _reason_cites_hard_trigger
    from src.portfolio_constructor import format_mechanical_size_down_reason

    reason = format_mechanical_size_down_reason(
        current_weight_pct=3.35,
        target_weight_pct=1.76,
        current_risk_pct=1.91,
        target_risk_pct=1.0,
    )
    assert not _reason_cites_hard_trigger(reason)


def test_fill_retry_is_not_spent_on_a_held_trim():
    """A reduction is not an open — do not spend the paid fill retry, and
    do not invent thesis_invalid_if text.
    """
    from src.agents.base import AgentResult
    from src.agents.portfolio_manager import PortfolioManagerAgent
    from src.models import PortfolioDecision, ReasoningChain

    held = _held("AAPL", 100, 230.0)
    decision = PortfolioDecision(
        reasoning_chain=ReasoningChain(
            macro_filter="m",
            news_check="n",
            earnings_check="e",
            signal_conflicts="s",
            sizing_logic="z",
            portfolio_balance="b",
            cash_target="c",
        ),
        targets=[
            TargetPosition(
                symbol="AAPL",
                risk_allocation_pct=1.0,
                thesis="trim",
                thesis_invalid_if="",
                catalyst="",
            )
        ],
        portfolio_view="v",
    )
    agent = PortfolioManagerAgent.__new__(PortfolioManagerAgent)
    first = AgentResult(raw_text="{}", tokens_used=1, model="test", user_message="original user message")
    calls: list = []

    def _execute(self, user_message, **kwargs):
        calls.append(kwargs.get("retry_kind"))
        raise AssertionError("fill retry must not run for a reduction")

    agent._execute = _execute.__get__(agent, PortfolioManagerAgent)
    filled, _ = agent._fill_missing_open_falsifiers(
        decision,
        first,
        positions=[held],
        total_value=100_000.0,
        existing_risk_pct={"AAPL": 1.91},
    )
    assert calls == []
    assert filled.targets[0].thesis_invalid_if == ""
    assert getattr(agent, "_soft_exit_retry_used", False) is False


def test_blank_falsifier_buy_from_a_risk_down_target_is_still_isolated():
    """`_target_intent` can label a lower-risk target a reduction while a
    tighter stop converts it to more shares (BUY). Book-admit may let that
    target through so a genuine trim can become a SELL; isolate still
    drops the constructed BUY. Never invents a falsifier.
    """
    from src.pipeline_stages import (
        _isolate_empty_soft_exit_entries,
        _targets_admitted_to_book,
    )
    from src.portfolio_constructor import PortfolioConstructor

    equity = 100_000.0
    held = _held("NVDA", 100, 100.0)  # 10% weight
    trim = TargetPosition(
        symbol="NVDA",
        risk_allocation_pct=1.5,
        thesis="lower risk",
        thesis_invalid_if="",
    )
    # Current stop ~$80 (2% risk on 100 shares). Fresh stop $95 is tighter,
    # so 1.5% risk sizes MORE shares than 100 — a BUY, not a SELL.
    analysis = TechAnalysisResult(
        **_tech(
            symbol="NVDA",
            entry_price=100.0,
            stop_loss=95.0,
            reference_target=140.0,
            support_levels=[95.0],
            resistance_levels=[140.0],
            computed_levels=[95.0, 140.0],
            atr_14=5.0 / 3.5,
            thesis_invalid_if="closes below 95",
        )
    )
    admitted, refused = _targets_admitted_to_book(
        [trim],
        positions=[held],
        total_value=equity,
        existing_risk_pct={"NVDA": 2.0},
    )
    assert refused == []
    assert admitted[0].thesis_invalid_if == ""
    decisions = PortfolioConstructor().construct_orders(
        targets=admitted,
        positions=[held],
        analyses=[analysis],
        total_value=equity,
        price_map={"NVDA": 100.0},
        existing_risk_pct={"NVDA": 2.0},
    )
    buys = [d for d in decisions if d.action == "BUY" and d.symbol == "NVDA"]
    assert buys, "this fixture must produce a BUY so isolate is the backstop"
    assert missing_stated_falsifier(buys[0].thesis_invalid_if)
    plan = SimpleNamespace(
        decisions=list(decisions),
        targets=[trim],
        constructor_dropped=[],
    )
    pipeline = SimpleNamespace(db=MagicMock())
    ctx = RunContext.start("morning")
    ctx.positions = [held]
    ctx.total_value = equity
    isolated = _isolate_empty_soft_exit_entries(pipeline, ctx, plan)
    assert "NVDA" in isolated
    assert not any(d.action == "BUY" for d in plan.decisions)


def test_owner_missing_data_rule_forbids_skip_as_the_product():
    """Owner 2026-09-17: skip/drop/ignore is not the permanent product.
    Isolate stays labelled TEMPORARY. The producing step must fill."""
    from pathlib import Path

    repo = Path(__file__).resolve().parents[1]
    agents = (repo / "AGENTS.md").read_text()
    assert "Never make skip/drop/ignore-and-continue the permanent product" in agents
    work = (repo / "docs" / "WORK.md").read_text()
    assert "**78." in work and "TEMPORARY" in work
    outcome = (repo / "docs" / "OUTCOME.md").read_text()
    assert "Missing data is a defect in the step that should have produced it" in outcome


# ---------------------------------------------------------------------------
# Board item 78: the heal must be recorded, and the refusal must not assert a
# retry that never happened. The isolate stays; it is the last resort, not the
# product.
# ---------------------------------------------------------------------------


def _blank_buy_plan():
    return SimpleNamespace(
        decisions=[
            TradeDecision(
                action="BUY",
                symbol="MRVL",
                allocation_pct=3.0,
                entry_price=80.0,
                stop_loss=75.0,
                take_profit=90.0,
                reasoning="blank",
                thesis_invalid_if=None,
            )
        ],
        targets=[
            TargetPosition(
                symbol="MRVL",
                risk_allocation_pct=1.0,
                thesis="t",
                thesis_invalid_if="",
            )
        ],
        constructor_dropped=[],
    )


def _evidence_payloads(db):
    import json

    out = []
    for call in db.insert_specialist_evidence.mock_calls:
        raw = call.kwargs.get("evidence_json")
        if isinstance(raw, str):
            try:
                out.append(json.loads(raw))
            except ValueError:
                pass
    return out


def test_isolate_still_exists_and_still_refuses():
    """Item 78 is NOT retired here: the last-resort isolate must stay live."""
    from src.pipeline_stages import _isolate_empty_soft_exit_entries as iso

    assert callable(iso)
    pipeline = SimpleNamespace(db=MagicMock())
    plan = _blank_buy_plan()
    assert iso(pipeline, RunContext.start("morning"), plan) == ["MRVL"]


def test_refusal_does_not_claim_a_retry_that_never_ran():
    """Untrue is a lie: with no heal record the reason must say so."""
    pipeline = SimpleNamespace(db=MagicMock())
    plan = _blank_buy_plan()
    _isolate_empty_soft_exit_entries(pipeline, RunContext.start("morning"), plan)
    rows = [p for p in _evidence_payloads(pipeline.db) if p.get("reason") == SOFT_EXIT_MISSING_AFTER_RETRY]
    assert len(rows) == 1
    assert rows[0]["heal_outcome"] == "none_recorded"
    assert "one paid retry" not in rows[0]["detail"]
    assert "no soft-exit heal was recorded" in rows[0]["detail"]


def test_refusal_quotes_the_real_heal_outcome_per_name():
    """A name whose retry was blocked by the cap says exactly that."""
    from src.seat_heal import HEAL_CAP_BLOCKED

    pipeline = SimpleNamespace(db=MagicMock())
    ctx = RunContext.start("morning")
    ctx.soft_exit_heals = {
        "MRVL": {"outcome": HEAL_CAP_BLOCKED, "detail": "spend cap refused it"},
    }
    _isolate_empty_soft_exit_entries(pipeline, ctx, _blank_buy_plan())
    rows = [p for p in _evidence_payloads(pipeline.db) if p.get("reason") == SOFT_EXIT_MISSING_AFTER_RETRY]
    assert rows[0]["heal_outcome"] == HEAL_CAP_BLOCKED
    assert "spend cap refused it" in rows[0]["detail"]


def test_nothing_is_silently_dropped_every_isolated_name_has_a_row():
    """Two blank BUYs, two durable per-name rows. No silent drop."""
    pipeline = SimpleNamespace(db=MagicMock())
    plan = _blank_buy_plan()
    plan.decisions.append(
        TradeDecision(
            action="SHORT",
            symbol="XYZ",
            allocation_pct=2.0,
            entry_price=50.0,
            stop_loss=55.0,
            take_profit=40.0,
            reasoning="blank",
            thesis_invalid_if=None,
        )
    )
    plan.targets.append(
        TargetPosition(
            symbol="XYZ",
            risk_allocation_pct=1.0,
            thesis="t",
            thesis_invalid_if="",
        )
    )
    isolated = _isolate_empty_soft_exit_entries(
        pipeline,
        RunContext.start("morning"),
        plan,
    )
    assert sorted(isolated) == ["MRVL", "XYZ"]
    named = {p.get("reason"): 0 for p in _evidence_payloads(pipeline.db)}
    rows = [p for p in _evidence_payloads(pipeline.db) if p.get("reason") == SOFT_EXIT_MISSING_AFTER_RETRY]
    assert len(rows) == 2, named
    assert sorted(plan.constructor_dropped) == ["MRVL", "XYZ"]


def test_heal_outcomes_are_drained_to_durable_rows_and_onto_ctx():
    """The heal's own outcome is machine-readable, not a log line."""
    from src.models import SOFT_EXIT_HEAL_EVENT_REASON
    from src.pipeline_stages import _record_soft_exit_heals
    from src.seat_heal import HEAL_NOT_ATTEMPTED

    agent = SimpleNamespace(
        last_soft_exit_heals={
            "MRVL": {"outcome": HEAL_NOT_ATTEMPTED, "detail": "never asked"},
        },
    )
    agent.drain_soft_exit_heals = lambda: (lambda d: (setattr(agent, "last_soft_exit_heals", {}), d)[1])(
        dict(agent.last_soft_exit_heals)
    )
    pipeline = SimpleNamespace(db=MagicMock(), portfolio_manager=agent)
    ctx = RunContext.start("morning")
    _record_soft_exit_heals(pipeline, ctx)
    assert ctx.soft_exit_heals["MRVL"]["outcome"] == HEAL_NOT_ATTEMPTED
    rows = [p for p in _evidence_payloads(pipeline.db) if p.get("reason") == SOFT_EXIT_HEAL_EVENT_REASON]
    assert len(rows) == 1
    assert rows[0]["outcome"] == HEAL_NOT_ATTEMPTED
    assert rows[0]["detail"] == "never asked"
    # Drained: a second call must not re-file a stale outcome.
    pipeline.db.reset_mock()
    _record_soft_exit_heals(pipeline, ctx)
    assert not pipeline.db.insert_specialist_evidence.mock_calls


def test_pm_records_heal_not_attempted_when_there_is_nothing_to_replay():
    """The silent bail-out now leaves a per-name machine-readable reason."""
    from src.agents.portfolio_manager import PortfolioManagerAgent
    from src.seat_heal import HEAL_NOT_ATTEMPTED

    agent = PortfolioManagerAgent.__new__(PortfolioManagerAgent)
    agent._soft_exit_retry_used = False
    agent.last_soft_exit_heals = {}
    decision = SimpleNamespace(
        targets=[
            TargetPosition(
                symbol="MRVL",
                risk_allocation_pct=1.0,
                thesis="t",
                thesis_invalid_if="",
            )
        ]
    )
    result = SimpleNamespace(user_message="")
    out, _ = agent._fill_missing_open_falsifiers(decision, result)
    heals = agent.drain_soft_exit_heals()
    assert heals["MRVL"]["outcome"] == HEAL_NOT_ATTEMPTED
    assert "NEVER ATTEMPTED" in heals["MRVL"]["detail"]
    # No falsifier was invented on the way out.
    assert out.targets[0].thesis_invalid_if in ("", None, SOFT_EXIT_UNKNOWN)
    # And drained means drained.
    assert agent.drain_soft_exit_heals() == {}


def test_pm_records_a_filled_falsifier_as_a_paid_retry_heal():
    """A healed name is recorded as healed, and is never refused."""
    from src.agents.portfolio_manager import PortfolioManagerAgent
    from src.seat_heal import HEAL_PAID_RETRY

    agent = PortfolioManagerAgent.__new__(PortfolioManagerAgent)
    agent._soft_exit_retry_used = False
    agent.last_soft_exit_heals = {}
    agent._target_intent = lambda t, *a, **k: "buy"
    agent._execute = lambda *a, **k: SimpleNamespace(
        parse_json=lambda: {
            "targets": [
                {"symbol": "MRVL", "thesis_invalid_if": "closes below 75"},
            ]
        },
    )
    decision = SimpleNamespace(
        targets=[
            TargetPosition(
                symbol="MRVL",
                risk_allocation_pct=1.0,
                thesis="t",
                thesis_invalid_if="",
            )
        ]
    )
    result = SimpleNamespace(user_message="original prompt")
    out, _ = agent._fill_missing_open_falsifiers(decision, result)
    assert out.targets[0].thesis_invalid_if == "closes below 75"
    heals = agent.drain_soft_exit_heals()
    assert heals["MRVL"]["outcome"] == HEAL_PAID_RETRY
    # A healed name reaches the isolate with a real falsifier, so the
    # isolate is unreachable for it.
    plan = SimpleNamespace(
        decisions=[
            TradeDecision(
                action="BUY",
                symbol="MRVL",
                allocation_pct=3.0,
                entry_price=80.0,
                stop_loss=75.0,
                take_profit=90.0,
                reasoning="healed",
                thesis_invalid_if="closes below 75",
            )
        ],
        targets=out.targets,
        constructor_dropped=[],
    )
    pipeline = SimpleNamespace(db=MagicMock())
    assert (
        _isolate_empty_soft_exit_entries(
            pipeline,
            RunContext.start("morning"),
            plan,
        )
        == []
    )


def test_pm_records_a_retry_that_ran_and_still_produced_nothing():
    """`failed` and `not_attempted` must not be confusable."""
    from src.agents.portfolio_manager import PortfolioManagerAgent
    from src.seat_heal import HEAL_FAILED

    agent = PortfolioManagerAgent.__new__(PortfolioManagerAgent)
    agent._soft_exit_retry_used = False
    agent.last_soft_exit_heals = {}
    agent._target_intent = lambda t, *a, **k: "buy"
    agent._execute = lambda *a, **k: SimpleNamespace(
        parse_json=lambda: {"targets": [{"symbol": "MRVL"}]},
    )
    decision = SimpleNamespace(
        targets=[
            TargetPosition(
                symbol="MRVL",
                risk_allocation_pct=1.0,
                thesis="t",
                thesis_invalid_if="",
            )
        ]
    )
    out, _ = agent._fill_missing_open_falsifiers(
        decision,
        SimpleNamespace(user_message="original prompt"),
    )
    heals = agent.drain_soft_exit_heals()
    assert heals["MRVL"]["outcome"] == HEAL_FAILED
    assert "WAS attempted" in heals["MRVL"]["detail"]


def test_mechanical_restore_outcome_is_recorded_durably():
    """Item 78: the mechanical heal writes down what it did, every time.

    RECORDING ONLY — these rows decide nothing. The test proves the write
    is reached, that a heal and a no-op are distinguishable, and that an
    unknown symbol stays NULL instead of being filled with a guess.
    """
    import sqlite3

    from src.pipeline_stages import _record_mechanical_soft_exit_restores
    from src.seat_heal import (
        drain_restore_observations,
        restore_stated_soft_exits,
    )

    ctx = RunContext.start("morning")  # opens this run's buffer

    # 1. a real heal: the canonical field was blanked, the raw still has it
    healed, restored = restore_stated_soft_exits(
        {"symbol": "mrvl", "thesis_invalid_if": None},
        {"thesis_invalid_if": "close below the 50-day"},
    )
    assert healed["thesis_invalid_if"] == "close below the 50-day"
    assert "thesis_invalid_if" in restored
    # 2. nothing to heal, and no symbol on the payload
    restore_stated_soft_exits(
        {"thesis_invalid_if": "already stated"},
        {"thesis_invalid_if": "already stated"},
    )

    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE soft_exit_heal_restores ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT, run_id TEXT,"
        " session_date TEXT, symbol TEXT, blank_found INTEGER,"
        " healed INTEGER, source TEXT, dropped_before INTEGER)"
    )
    written: dict = {}

    def _writer(*, observations, run_id=None, dropped=0):
        rows = [
            (
                o.get("symbol"),
                int(bool(o.get("blank_found"))),
                int(bool(o.get("healed"))),
                o.get("source"),
                run_id,
                dropped,
            )
            for o in observations
        ]
        conn.executemany(
            "INSERT INTO soft_exit_heal_restores"
            " (symbol, blank_found, healed, source, run_id, dropped_before)"
            " VALUES (?,?,?,?,?,?)",
            rows,
        )
        written["n"] = len(rows)
        return len(rows)

    pipeline = SimpleNamespace(
        db=SimpleNamespace(record_soft_exit_heal_restores=_writer),
    )
    _record_mechanical_soft_exit_restores(pipeline, ctx)
    assert written["n"] == 2

    rows = conn.execute(
        "SELECT symbol, blank_found, healed, source FROM soft_exit_heal_restores ORDER BY id"
    ).fetchall()
    assert rows[0] == ("MRVL", 1, 1, "raw_model_output")
    # Unknown stays NULL: no symbol on the payload, nothing healed.
    assert rows[1] == (None, 0, 0, None)

    # Drained: a second pass must not re-file the same observations.
    written.clear()
    _record_mechanical_soft_exit_restores(pipeline, RunContext.start("morning"))
    assert not written


def test_real_database_records_mechanical_restores(tmp_path):
    """The real storage method writes the row, and unknown stays NULL."""
    from src.storage.db import Database

    db = Database(str(tmp_path / "t.db"))
    db.initialize()
    n = db.record_soft_exit_heal_restores(
        observations=[
            {
                "symbol": "mrvl",
                "blank_found": True,
                "healed": True,
                "source": "raw_model_output",
            },
            {"symbol": None, "blank_found": False, "healed": False, "source": None},
        ],
        run_id="run-1",
        dropped=3,
    )
    assert n == 2
    rows = [
        tuple(r)
        for r in db.conn.execute(
            "SELECT symbol, blank_found, healed, source, run_id,"
            " dropped_before FROM soft_exit_heal_restores ORDER BY id"
        ).fetchall()
    ]
    assert rows[0] == ("MRVL", 1, 1, "raw_model_output", "run-1", 3)
    assert rows[1] == (None, 0, 0, None, "run-1", None)
