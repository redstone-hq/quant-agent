"""Board item 164 — gates in the PM / risk path that changed or discarded a
decision while leaving only a log line, or nothing.

The owner's ruling: keep every decision and its reason. Each test here pins
ONE durable record, written into the desk's existing per-symbol evidence
stream (`specialist_evidence`: the `pipeline_event` rows, or the exit path's
own `exit_refusal` rows), and fails if the line that writes it is removed.

Nothing here tests a gate's DECISION — every one of these gates already had
its own behavioural tests, and none of them changed. The assertions that a
size or a survivor list is what it was are there only to prove the record
describes what actually happened.
"""

from __future__ import annotations
from src.pipeline_risk_gate import RiskGate

import json
from unittest.mock import MagicMock, patch

import pytest

from src.models import (
    PortfolioDecision,
    Position,
    RiskModification,
    RiskVerdict,
    TradeDecision,
)
from src.pipeline_stages import DecisionStage, RiskStage

from tests.test_risk_verdict_per_symbol import (
    _chpx,
    _ctx,
    _exit_pipeline,
    _position,
    _rc,
    _stage_pipeline,
    _two_exits,
    _xle,
)


def _events(pipeline) -> list[tuple[str | None, dict]]:
    """(symbol, payload) for every pipeline_event written."""
    out = []
    for call in pipeline.db.insert_specialist_evidence.call_args_list:
        kw = call.kwargs
        if kw.get("kind") != "pipeline_event":
            continue
        out.append((kw.get("symbol"), json.loads(kw["evidence_json"])))
    return out


def _real_mods(pipeline):
    """Undo the harness stub: run the REAL modification applier."""
    from src.pipeline import TradingPipeline

    pipeline.risk_gate._apply_risk_modifications = RiskGate._apply_risk_modifications.__get__(pipeline.risk_gate)
    return pipeline


def _verdict(mods, **over) -> RiskVerdict:
    base = dict(
        approved=True,
        reasoning_chain=_rc(),
        reason_category="oversized",
        modifications=mods,
        reasoning="run-level narrative",
    )
    base.update(over)
    return RiskVerdict(**base)


# ---------------------------------------------------------------------------
# src/pipeline.py — `_apply_risk_modifications`
# ---------------------------------------------------------------------------


def test_a_schema_invalid_risk_edit_records_the_dropped_trade():
    """The edit fails the order schema, so the trade is DROPPED (unchanged
    behaviour). It used to be in neither `rejected_mods` nor any event."""
    decisions = [_xle(), _chpx()]
    verdict = _verdict(
        [
            RiskModification(
                symbol="XLE",
                field="allocation_pct",
                original_value=5.0,
                new_value=150.0,
                reason="cut the energy leg",
            )
        ]
    )
    pipeline = _real_mods(_stage_pipeline(verdict=verdict, decisions=decisions))
    ctx = _ctx(decisions)

    assert RiskStage(pipeline=pipeline).run(ctx) is None
    assert [d.symbol for d in ctx.portfolio_decision.decisions] == ["CHPX"]

    dropped = [p for s, p in _events(pipeline) if s == "XLE" and p["outcome"] == "dropped"]
    assert len(dropped) == 1, _events(pipeline)
    row = dropped[0]
    assert row["stage"] == "risk"
    assert row["gate"] == "rm_modification_schema_invalid"
    assert row["field"] == "allocation_pct"
    assert row["before"] == 5.0 and row["requested"] == 150.0
    assert row["seat_reason"] == "cut the energy leg"
    assert "DROPPED" in row["reason"] and "allocation_pct" in row["reason"]


def test_an_edit_to_an_unknown_field_is_recorded_as_not_applied():
    decisions = [_chpx()]
    verdict = _verdict(
        [
            RiskModification(
                symbol="CHPX",
                field="conviction",
                original_value=2.0,
                new_value=1.0,
                reason="lower the conviction",
            )
        ]
    )
    pipeline = _real_mods(_stage_pipeline(verdict=verdict, decisions=decisions))

    assert RiskStage(pipeline=pipeline).run(_ctx(decisions)) is None

    events = _events(pipeline)
    not_applied = [p for s, p in events if s == "CHPX" and p["outcome"] == "modification_not_applied"]
    assert len(not_applied) == 1
    assert not_applied[0]["gate"] == "rm_modification_unknown_field"
    assert not_applied[0]["field"] == "conviction"
    assert "NOT APPLIED" in not_applied[0]["reason"]
    # And the leg's own risk event no longer claims it was modified.
    final = [p for s, p in events if s == "CHPX" and p["stage"] == "risk" and p["outcome"] in ("approved", "modified")]
    assert [p["outcome"] for p in final] == ["approved"]


def test_an_edit_naming_a_symbol_outside_the_plan_is_recorded_run_scoped():
    """No decision to edit, so nothing changed — recorded, and filed
    run-scoped so the jam detector does not count a phantom candidate."""
    decisions = [_chpx()]
    verdict = _verdict(
        [
            RiskModification(
                symbol="ZZZ",
                field="allocation_pct",
                original_value=4.0,
                new_value=2.0,
                reason="halve ZZZ",
            )
        ]
    )
    pipeline = _real_mods(_stage_pipeline(verdict=verdict, decisions=decisions))

    assert RiskStage(pipeline=pipeline).run(_ctx(decisions)) is None

    rows = [(s, p) for s, p in _events(pipeline) if p["outcome"] == "modification_not_applied"]
    assert len(rows) == 1
    symbol, payload = rows[0]
    assert symbol is None
    assert payload["symbol_named"] == "ZZZ"
    assert payload["gate"] == "rm_modification_no_matching_decision"
    assert "halve ZZZ" in payload["reason"]


# ---------------------------------------------------------------------------
# src/pipeline_stages.py — the per-symbol `risk` event
# ---------------------------------------------------------------------------


def test_the_risk_event_carries_the_seats_own_reason_and_the_change():
    decisions = [_xle(), _chpx()]
    verdict = _verdict(
        [
            RiskModification(
                symbol="XLE",
                field="allocation_pct",
                original_value=5.0,
                new_value=3.0,
                reason="energy already heavy in the book",
            )
        ]
    )
    pipeline = _real_mods(_stage_pipeline(verdict=verdict, decisions=decisions))

    assert RiskStage(pipeline=pipeline).run(_ctx(decisions)) is None

    risk = {s: p for s, p in _events(pipeline) if p["stage"] == "risk" and p["outcome"] in ("approved", "modified")}
    assert risk["XLE"]["outcome"] == "modified"
    assert "energy already heavy in the book" in risk["XLE"]["reason"]
    assert risk["XLE"]["changes"] == {"allocation_pct": [5.0, 3.0]}
    assert risk["CHPX"]["outcome"] == "approved"
    assert risk["CHPX"]["reason"] != risk["XLE"]["reason"]
    for payload in risk.values():
        assert payload["reason"] != "risk_manager_verdict"


def test_a_rejected_edit_no_longer_reads_as_modified():
    """Guard 1b reverts an enlarging BUY edit; the leg ships unchanged, so
    its risk event must say approved, not modified."""
    decisions = [_chpx()]
    verdict = _verdict(
        [
            RiskModification(
                symbol="CHPX",
                field="allocation_pct",
                original_value=6.0,
                new_value=9.0,
                reason="size it up",
            )
        ]
    )
    pipeline = _real_mods(_stage_pipeline(verdict=verdict, decisions=decisions))
    ctx = _ctx(decisions)

    assert RiskStage(pipeline=pipeline).run(ctx) is None
    assert ctx.portfolio_decision.decisions[0].allocation_pct == 6.0

    outcomes = [p["outcome"] for s, p in _events(pipeline) if s == "CHPX" and p["stage"] == "risk"]
    assert "modification_rejected" in outcomes
    assert "modified" not in outcomes
    assert "approved" in outcomes


def test_approved_false_drops_named_entry_and_records_the_ignored_veto():
    """Owner ruling 2026-09-24 (final): there is no book veto. approved=False is
    a no-op recorded as `batch_veto_ignored`; the seat's named entry is dropped
    with its OWN reason, and the unrelated entry proceeds."""
    decisions = [_xle(), _chpx()]
    verdict = RiskVerdict(
        approved=False,
        reasoning_chain=_rc(),
        reason_category="correlation_risk",
        rejected_symbols=[{"symbol": "XLE", "reason": "XLE-specific reason", "category": "other"}],
        reasoning="the book is one energy cluster",
    )
    pipeline = _stage_pipeline(verdict=verdict, decisions=decisions)
    ctx = _ctx(decisions)

    result = RiskStage(pipeline=pipeline).run(ctx)

    assert result is None, "the batch is NOT rejected"
    assert [d.symbol for d in ctx.portfolio_decision.decisions] == ["CHPX"]
    rejected = {s: p for s, p in _events(pipeline) if p["stage"] == "risk" and p["outcome"] == "rejected"}
    assert rejected["XLE"]["reason"] == "XLE-specific reason"
    assert "CHPX" not in rejected, "the unrelated leg is never refused"
    ignored = [p for _s, p in _events(pipeline) if p["stage"] == "risk" and p["outcome"] == "batch_veto_ignored"]
    assert len(ignored) == 1, "the approved=False flag is recorded, not enforced"


# ---------------------------------------------------------------------------
# src/pipeline.py — the queued-earnings refusal
# ---------------------------------------------------------------------------


def _earnings_pipeline(decisions):
    from src.pipeline import TradingPipeline

    pipeline = _stage_pipeline(verdict=_verdict([]), decisions=decisions)
    pipeline.risk_gate._refuse_queued_earnings_buys = RiskGate._refuse_queued_earnings_buys
    return pipeline


def test_the_queued_earnings_gate_records_every_refusal():
    """Board item 186 (2026-10-01): the gate no longer CUTS a BUY, so the
    `modified` row this file used to pin cannot occur. What must still be
    durable is the stand-down itself, with the size that was asked for and
    the missing-evidence reason it was refused on, and the stage's own
    return contract is asserted exactly as it was before."""
    buy = TradeDecision(
        action="BUY",
        symbol="CHPX",
        allocation_pct=8.0,
        entry_price=24.0,
        stop_loss=22.5,
        take_profit=28.55,
        reasoning="t",
        thesis_invalid_if="closes below support",
    )
    pipeline = _earnings_pipeline([buy])
    ctx = _ctx([buy])
    ctx.earnings_results = [{"symbol": "CHPX", "queued": True}]

    result = RiskStage(pipeline=pipeline).run(ctx)
    assert result is None, result
    assert "CHPX" not in [d.symbol for d in ctx.portfolio_decision.decisions]

    rows = [p for s, p in _events(pipeline) if s == "CHPX" and p.get("gate") == "queued_earnings_unread_filing"]
    assert len(rows) == 1
    assert rows[0]["outcome"] == "blocked"
    assert rows[0]["before_allocation_pct"] == 8.0
    assert rows[0]["after_allocation_pct"] == 0.0
    assert "Unread filing" in rows[0]["detail"]


def test_the_queued_earnings_gate_is_silent_on_a_read_filing():
    buy = TradeDecision(
        action="BUY",
        symbol="CHPX",
        allocation_pct=2.0,
        entry_price=24.0,
        stop_loss=22.5,
        take_profit=28.55,
        reasoning="t",
        thesis_invalid_if="closes below support",
    )
    pipeline = _earnings_pipeline([buy])
    ctx = _ctx([buy])
    ctx.earnings_results = [
        {"symbol": "CHPX", "queued": False, "analysis": {"x": 1}},
    ]

    RiskStage(pipeline=pipeline).run(ctx)

    assert "CHPX" in [d.symbol for d in ctx.portfolio_decision.decisions]
    assert not [p for s, p in _events(pipeline) if s == "CHPX" and p.get("gate") == "queued_earnings_unread_filing"]


# ---------------------------------------------------------------------------
# src/agents/portfolio_manager.py — targets dropped after the model answered
# ---------------------------------------------------------------------------


@patch("anthropic.Anthropic")
def test_decide_reports_every_target_it_drops_with_gate_and_reason(mock_cls):
    from src.agents.portfolio_manager import (
        CONFLICT_UNADJUDICATED_STATUS,
        PortfolioManagerAgent,
    )
    from tests.test_conflict_adjudication import (
        _analysis,
        _buy_target,
        _pm_response,
    )

    malformed = {"symbol": "MSFT", "target_weight_pct": 30, "thesis": "x"}
    response_text = _pm_response(
        [_buy_target("NVDA", conflict_source="macro"), _buy_target("AAPL", conflict_source=None), malformed],
        conflicts="AAPL: available=technical=buy. Conflict: none. Resolution: n/a.",
    )
    mock_client = MagicMock()
    mock_response = MagicMock()
    mock_response.content = [MagicMock(text=response_text)]
    mock_response.usage.input_tokens = 500
    mock_response.usage.output_tokens = 200
    mock_client.messages.create.return_value = mock_response
    mock_cls.return_value = mock_client

    agent = PortfolioManagerAgent(api_key="test", model="claude-opus-4-6-20250725")
    decision, result = agent.decide(
        analyses=[_analysis("NVDA"), _analysis("AAPL"), _analysis("MSFT")],
        positions=[],
        macro_analysis=None,
        cash_balance=50_000,
        total_value=100_000,
        allowed_buy_symbols={"NVDA", "AAPL", "MSFT"},
    )

    assert decision is not None, result.semantic_error
    assert {t.symbol for t in decision.targets} == {"AAPL"}
    by_symbol = {d["symbol"]: d for d in agent.last_dropped_targets}
    assert set(by_symbol) == {"NVDA", "MSFT"}
    assert by_symbol["NVDA"]["gate"] == CONFLICT_UNADJUDICATED_STATUS
    assert by_symbol["NVDA"]["unaddressed_sources"] == ["macro"]
    assert by_symbol["MSFT"]["gate"] == "pm_target_malformed"
    assert "target_weight_pct" in by_symbol["MSFT"]["reason"]


def _decision_stage_pipeline(*, decision, dropped, constructor=None):
    from tests.test_pm_candidate_accounting import _pipeline, _pm_result

    p = _pipeline([(decision, _pm_result()), (decision, _pm_result())])
    p.portfolio_manager.last_dropped_targets = dropped
    if constructor is not None:
        p.portfolio_constructor = constructor
    return p


def _run_decision_stage(pipeline, positions=None):
    """Mirrors `tests/test_pm_candidate_accounting._run_stage`, but keeps
    run-scoped rows too."""
    captured_all: list[tuple[str | None, dict]] = []

    def _cap(db, **kw):
        if kw.get("kind") == "pipeline_event":
            captured_all.append((kw.get("symbol"), json.loads(kw["evidence_json"])))

    with patch("src.pipeline_stages._persist_evidence", _cap):
        from src.pipeline_context import RunContext
        from tests.test_pm_candidate_accounting import _analysis

        ctx = RunContext.start("intra_check")
        ctx.positions = list(positions or [])
        ctx.analyses = [_analysis("AAPL")]
        ctx.macro_analysis = None
        ctx.total_value = 100_000.0
        ctx.last_equity = 100_000.0
        ctx.cash = 50_000.0
        ctx.deployable_cash = 50_000.0
        ctx.admitted_symbols = set()
        try:
            DecisionStage(pipeline=pipeline).run(ctx)
        except Exception:
            # Construction/execution are not stood up past what each test
            # needs; every record under test is written before that.
            pass
    return captured_all


def test_decision_stage_persists_each_pm_dropped_target():
    from tests.test_pm_candidate_accounting import _decision

    dropped = [
        {
            "symbol": "NVDA",
            "gate": "conflict_unadjudicated",
            "intent": "buy",
            "unaddressed_sources": ["macro"],
            "reason": "NVDA target (buy) DROPPED",
        },
        {"symbol": None, "index": 2, "gate": "pm_target_malformed", "reason": "targets entry at index 2 DROPPED"},
    ]
    pipeline = _decision_stage_pipeline(decision=_decision(), dropped=dropped)

    rows = [(s, p) for s, p in _run_decision_stage(pipeline) if p.get("outcome") == "target_dropped"]

    assert len(rows) == 2
    nvda = next(p for s, p in rows if s == "NVDA")
    assert nvda["stage"] == "portfolio_manager"
    assert nvda["gate"] == "conflict_unadjudicated"
    assert nvda["reason"] == "NVDA target (buy) DROPPED"
    run_scoped = next(p for s, p in rows if s is None)
    assert run_scoped["gate"] == "pm_target_malformed"
    assert run_scoped["index"] == 2


# ---------------------------------------------------------------------------
# src/portfolio_constructor.py — a side flip collapsed to a close-only leg
# ---------------------------------------------------------------------------


def test_the_constructor_notes_a_refused_side_flip():
    from src.models import TargetPosition
    from src.portfolio_constructor import PortfolioConstructor

    held = Position(
        symbol="AAPL",
        qty=50,
        avg_entry=100.0,
        current_price=100.0,
        market_value=5_000.0,
        unrealized_pnl=0.0,
        sector="Technology",
    )
    target = TargetPosition.model_validate(
        {
            "symbol": "AAPL",
            "target_weight_pct": 5.0,
            "direction": "short",
            "conviction": "medium",
            "thesis": "flip to short",
        }
    )
    constructor = PortfolioConstructor()

    orders = constructor.construct_orders(
        targets=[target],
        positions=[held],
        analyses=[],
        total_value=100_000.0,
        price_map={"AAPL": 100.0},
    )

    assert [o.action for o in orders] == ["SELL"]  # close only, unchanged
    flip = constructor.last_side_flips["AAPL"]
    assert flip["held_weight_pct"] == pytest.approx(5.0)
    assert flip["requested_weight_pct"] == pytest.approx(-5.0)
    assert flip["emitted_weight_pct"] == 0.0


def test_decision_stage_persists_a_refused_side_flip():
    from src.models import TargetPosition
    from tests.test_pm_candidate_accounting import _chain

    decision = PortfolioDecision(
        reasoning_chain=_chain(),
        targets=[
            TargetPosition.model_validate(
                {
                    "symbol": "AAPL",
                    "target_weight_pct": 5.0,
                    "direction": "short",
                    "conviction": "medium",
                    "thesis": "flip to short",
                    "thesis_invalid_if": "reclaims the high",
                }
            )
        ],
        portfolio_view="flip",
    )
    constructor = MagicMock()
    constructor.real_reward_risk_preview.return_value = {}
    constructor.last_refusals = {}
    constructor.drain_refusals.return_value = {}
    constructor.drain_data_faults.return_value = {}
    constructor.last_drop_reasons = {}
    constructor.construct_orders.return_value = [
        TradeDecision(
            action="SELL",
            symbol="AAPL",
            allocation_pct=100.0,
            entry_price=0.0,
            stop_loss=0.0,
            take_profit=0.0,
            reasoning="close",
        )
    ]
    constructor.last_side_flips = {
        "AAPL": {
            "held_weight_pct": 5.0,
            "requested_weight_pct": -5.0,
            "emitted_weight_pct": 0.0,
        }
    }
    pipeline = _decision_stage_pipeline(
        decision=decision,
        dropped=[],
        constructor=constructor,
    )
    held = Position(
        symbol="AAPL",
        qty=50,
        avg_entry=100.0,
        current_price=100.0,
        market_value=5_000.0,
        unrealized_pnl=0.0,
        sector="Technology",
    )

    rows = [
        p
        for s, p in _run_decision_stage(pipeline, positions=[held])
        if s == "AAPL" and p.get("reason") == "side_flip_refused"
    ]

    assert constructor.construct_orders.called, "harness never reached construction"
    assert len(rows) == 1
    assert rows[0]["outcome"] == "modified"
    assert rows[0]["held_weight_pct"] == 5.0
    assert rows[0]["requested_weight_pct"] == -5.0
    assert rows[0]["emitted_weight_pct"] == 0.0


# ---------------------------------------------------------------------------
# src/pipeline.py — an APPROVED exit review
# ---------------------------------------------------------------------------


def _exit_rows(pipeline) -> dict[str, dict]:
    out = {}
    for call in pipeline.db.insert_specialist_evidence.call_args_list:
        kw = call.kwargs
        if kw.get("kind") == "exit_refusal":
            out[kw["symbol"]] = json.loads(kw["evidence_json"])
    return out


def test_an_approved_exit_review_leaves_a_per_symbol_record():
    verdict = RiskVerdict(
        approved=True,
        reasoning_chain=_rc(),
        reasoning="both exits are sound",
    )
    pipeline = _exit_pipeline(verdict)

    vetoed, _ = pipeline._risk_review_exits(
        _two_exits(),
        [_position("AAA"), _position("BBB")],
        run_id="r1",
        total_value=100_000.0,
    )

    assert vetoed == set()
    rows = _exit_rows(pipeline)
    assert set(rows) == {"AAA", "BBB"}
    for row in rows.values():
        assert row["code"] == "ai_risk_approved"
        assert row["dropped"] is False
        assert row["layer"] == "ai_risk"
        assert "both exits are sound" in row["detail"]


def test_the_exits_let_through_beside_a_veto_are_recorded_too():
    verdict = RiskVerdict(
        approved=True,
        reasoning_chain=_rc(),
        rejected_symbols=[{"symbol": "AAA", "reason": "invalidation not confirmed", "category": "other"}],
        reasoning="BBB may exit",
    )
    pipeline = _exit_pipeline(verdict)

    vetoed, _ = pipeline._risk_review_exits(
        _two_exits(),
        [_position("AAA"), _position("BBB")],
        run_id="r1",
        total_value=100_000.0,
    )

    assert vetoed == set()
    rows = _exit_rows(pipeline)
    assert rows["AAA"]["code"] == "ai_risk_objection_advisory"
    assert rows["AAA"]["dropped"] is False
    assert rows["BBB"]["code"] == "ai_risk_approved"
    assert rows["BBB"]["dropped"] is False
