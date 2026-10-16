"""Phase 10.1 — one failing leg must die alone.

`RiskVerdict.approved` was a single bool for the whole plan, and
`RiskModification` could retune a symbol but not refuse one. So the only way
the risk manager could say "not this trade" was to say "not any trade".

Production evidence, run `run-64290730` (2026-09-01 morning): the verdict
rejected the entire plan citing XLE alone — constructed R/R 1.18, under the
1.5 floor — and CHPX died with it at R/R 3.03, a different sector and an
unrelated technical thesis. Zero trades that morning. CHPX was never judged;
it was standing next to XLE.

`rejected_symbols` gives the verdict a per-symbol outcome, so one failing leg
no longer takes the batch with it.

Owner ruling 2026-09-24 (final) went further: the risk seat has NO whole-batch
veto at all. `approved=False` is a no-op for batch rejection — recorded, then
the plan proceeds. The seat reduces risk ONLY by dropping a NEW entry
(`rejected_symbols`), shrinking (`scale_all_buys` to 0.0, `modifications`), and
never by stopping the plan. A protective exit (SELL/COVER) or existing holding
can never be dropped or blocked by the seat. The hard limits stay enforced by
the deterministic gate, before and after scaling. The second half of this file
pins that ruling.

No threshold moves here.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from src.agents.base import AgentResult
from src.agents.risk_manager import RiskManagerAgent
from src.models import (
    PortfolioDecision,
    ReasoningChain,
    RiskReasoningChain,
    RiskVerdict,
    EntrySymbolRejection,
    TradeDecision,
)
from src.pipeline_context import RunContext
from src.pipeline_stages import RiskStage

PROMPT_PATH = Path(__file__).resolve().parent.parent / "config" / "prompts" / "risk_manager.md"
from tests.pipeline_factory import build_pipeline

# The two legs of run-64290730, with the geometry that produced each R/R.
XLE_RR = "constructed R/R 1.18:1 is below the 1.5 floor and PM named no catalyst"
CHPX_RR = "R/R 3.03:1"


def _rc() -> RiskReasoningChain:
    return RiskReasoningChain(
        rr_audit="x",
        signal_fidelity="x",
        correlation_check="x",
        event_risk="x",
        sizing_sanity="x",
        overall="x",
    )


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


def _xle() -> TradeDecision:
    """R/R (90.11 - 87.40) / (87.40 - 85.10) = 1.18 — under the floor."""
    return TradeDecision(
        action="BUY",
        symbol="XLE",
        allocation_pct=5.0,
        entry_price=87.40,
        stop_loss=85.10,
        take_profit=90.11,
        reasoning="energy rotation",
        thesis_invalid_if="closes below support",
    )


def _chpx() -> TradeDecision:
    """R/R (28.55 - 24.00) / (24.00 - 22.50) = 3.03 — comfortably passing."""
    return TradeDecision(
        action="BUY",
        symbol="CHPX",
        allocation_pct=6.0,
        entry_price=24.00,
        stop_loss=22.50,
        take_profit=28.55,
        reasoning="breakout, unrelated thesis",
        thesis_invalid_if="closes below support",
    )


def _stage_pipeline(*, verdict, decisions):
    """A pipeline stubbed just far enough for RiskStage.run() to reach the
    verdict-application block, with an HONEST pass-through hard-risk filter:
    it returns whatever it is handed. Any symbol missing at the end was
    removed by the code under test, not by a mock."""
    from src.pipeline import TradingPipeline

    rm_result = MagicMock()
    rm_result.used_fallback = False
    rm_result.raw_text = "{}"
    pipeline = build_pipeline(
        db=MagicMock(),
        _sweeper=MagicMock(return_value=None),
        _filter_supported_symbols=MagicMock(return_value=(decisions, [])),
        _refuse_queued_earnings_buys=MagicMock(return_value=decisions),
        _filter_hard_risk_decisions=MagicMock(
            side_effect=lambda d, *a, **kw: (list(d), [], []),
        ),
        risk_manager=MagicMock(),
    )
    pipeline.risk_manager.review.return_value = (verdict, rm_result)
    return pipeline


def _ctx(decisions) -> RunContext:
    ctx = RunContext.start("morning")
    ctx.decision_id = f"{ctx.run_id}-dec-000001"
    ctx.total_value = 100_000.0
    ctx.last_equity = 100_000.0
    ctx.cash = 50_000.0
    ctx.portfolio_decision = PortfolioDecision(
        reasoning_chain=_pm_rc(),
        decisions=decisions,
        portfolio_view="test",
    )
    # Owner ruling 2026-10-13: a judgement sale survives RiskStage only when
    # the holding FAILS the entry bar (src/exits/same_rules_gate.py). These
    # tests are about the AI Risk seat, so every proposed sale is given an
    # opposed news seat and the gate lets it through.
    from tests.test_sell_same_rules import opposed_for_exits

    ctx.seat_verdicts = opposed_for_exits(decisions)
    return ctx


def _symbols(ctx) -> list[str]:
    return [d.symbol for d in ctx.portfolio_decision.decisions]


def _events(pipeline) -> list[tuple[str, str, str, str]]:
    """(symbol, stage, outcome, reason) for every pipeline_event written."""
    out = []
    for call in pipeline.db.insert_specialist_evidence.call_args_list:
        kwargs = call.kwargs
        if kwargs.get("kind") != "pipeline_event":
            continue
        payload = json.loads(kwargs["evidence_json"])
        out.append(
            (
                kwargs.get("symbol"),
                payload.get("stage"),
                payload.get("outcome"),
                payload.get("reason"),
            )
        )
    return out


# ---------------------------------------------------------------------------
# The defect itself
# ---------------------------------------------------------------------------


def test_refusing_xle_no_longer_kills_chpx():
    """run-64290730, reproduced. One leg fails its own R/R floor; the other
    is in a different sector on an unrelated thesis and passes. Before this
    change the only lever available took both."""
    decisions = [_xle(), _chpx()]
    verdict = RiskVerdict(
        approved=True,
        reasoning_chain=_rc(),
        reason_category="rr_fail",
        rejected_symbols=[{"symbol": "XLE", "reason": XLE_RR, "category": "other"}],
        reasoning=f"XLE refused on R/R. CHPX stands on its own at {CHPX_RR}.",
    )
    pipeline = _stage_pipeline(verdict=verdict, decisions=decisions)
    ctx = _ctx(decisions)

    result = RiskStage(pipeline=pipeline).run(ctx)

    # None == "carry on to execution". A dict would be a terminal refusal.
    assert result is None
    assert _symbols(ctx) == ["CHPX"], "the passing leg must survive a refusal aimed at a different symbol"


def test_the_refused_symbol_carries_its_own_reason_not_the_runs():
    """The audit trail has to answer 'why did THIS name die', per name."""
    decisions = [_xle(), _chpx()]
    verdict = RiskVerdict(
        approved=True,
        reasoning_chain=_rc(),
        reason_category="rr_fail",
        rejected_symbols=[{"symbol": "XLE", "reason": XLE_RR, "category": "other"}],
        reasoning="run-level narrative that is NOT the per-symbol reason",
    )
    pipeline = _stage_pipeline(verdict=verdict, decisions=decisions)

    RiskStage(pipeline=pipeline).run(_ctx(decisions))

    risk_events = [e for e in _events(pipeline) if e[1] == "risk"]
    assert ("XLE", "risk", "rejected", XLE_RR) in risk_events
    assert not [e for e in risk_events if e[0] == "CHPX" and e[2] == "rejected"]

    # And a durable per-symbol evidence row, the same way a modification gets one.
    rejection_rows = [
        c.kwargs for c in pipeline.db.insert_specialist_evidence.call_args_list if c.kwargs.get("kind") == "rejection"
    ]
    assert len(rejection_rows) == 1
    assert rejection_rows[0]["symbol"] == "XLE"
    assert rejection_rows[0]["scope"] == "symbol"
    assert XLE_RR in rejection_rows[0]["evidence_json"]


def test_every_leg_refused_individually_still_ends_the_run():
    """Per-symbol refusal is not a way to trade something. When every leg is
    refused on its own merits the run is over — but each symbol carries its
    own reason rather than one shared sentence about a different symbol."""
    decisions = [_xle(), _chpx()]
    verdict = RiskVerdict(
        approved=True,
        reasoning_chain=_rc(),
        reason_category="rr_fail",
        rejected_symbols=[
            {"symbol": "XLE", "reason": XLE_RR, "category": "other"},
            {"symbol": "CHPX", "reason": "stop sits inside the daily range", "category": "other"},
        ],
        reasoning="both refused, separately",
    )
    pipeline = _stage_pipeline(verdict=verdict, decisions=decisions)

    result = RiskStage(pipeline=pipeline).run(_ctx(decisions))

    assert result["status"] == "rejected"
    assert result["orders"] == []
    assert XLE_RR in result["reason"]
    assert "stop sits inside the daily range" in result["reason"]


def test_refusal_naming_a_symbol_outside_the_plan_is_a_noop():
    decisions = [_chpx()]
    verdict = RiskVerdict(
        approved=True,
        reasoning_chain=_rc(),
        reason_category="rr_fail",
        rejected_symbols=[{"symbol": "XLE", "reason": XLE_RR, "category": "other"}],
        reasoning="XLE is not in this plan",
    )
    pipeline = _stage_pipeline(verdict=verdict, decisions=decisions)
    ctx = _ctx(decisions)

    assert RiskStage(pipeline=pipeline).run(ctx) is None
    assert _symbols(ctx) == ["CHPX"]


# ---------------------------------------------------------------------------
# Owner ruling 2026-09-24 (final): the risk seat has NO whole-batch veto.
#
# `approved=False` is a NO-OP for batch rejection — recorded in the durable
# trail, then the plan proceeds. All risk reduction comes from three levers:
# drop a NEW entry (`rejected_symbols`), shrink (`scale_all_buys` down to 0.0,
# and per-trade `modifications`). A protective exit (SELL/COVER) or existing
# holding can NEVER be dropped or blocked by the seat, under any path.
# ---------------------------------------------------------------------------


def _sell(symbol: str = "HELD") -> TradeDecision:
    """A protective exit PM proposed — a SELL that must always reach execution."""
    return TradeDecision(
        action="SELL",
        symbol=symbol,
        allocation_pct=100.0,
        entry_price=50.0,
        stop_loss=0.0,
        take_profit=0.0,
        reasoning="thesis played out; take profit",
    )


def _cover(symbol: str = "SHRT") -> TradeDecision:
    """A protective COVER — closing a short — that must always reach execution."""
    return TradeDecision(
        action="COVER",
        symbol=symbol,
        allocation_pct=100.0,
        entry_price=50.0,
        stop_loss=0.0,
        take_profit=0.0,
        reasoning="short thesis done; cover",
    )


def _veto_ignored_events(pipeline):
    return [e for e in _events(pipeline) if e[2] == "batch_veto_ignored"]


def _exit_protected_events(pipeline):
    return [e for e in _events(pipeline) if e[2] == "exit_refusal_ignored"]


def test_approved_false_no_longer_rejects_the_batch():
    """Ruling test 1 — approved=False does NOT reject the batch. The seat's
    named entry is dropped; the unrelated entry proceeds; the flag is recorded
    as batch_veto_ignored, not enforced."""
    decisions = [_xle(), _chpx()]
    verdict = RiskVerdict(
        approved=False,
        reasoning_chain=_rc(),
        reason_category="correlation_risk",
        rejected_symbols=[{"symbol": "XLE", "reason": XLE_RR, "category": "other"}],
        reasoning="uneasy about the book, dropping XLE",
    )
    pipeline = _stage_pipeline(verdict=verdict, decisions=decisions)
    ctx = _ctx(decisions)

    result = RiskStage(pipeline=pipeline).run(ctx)

    assert result is None, "the batch is NOT rejected; it flows to execution"
    assert _symbols(ctx) == ["CHPX"], "only the seat's named entry is dropped"
    assert len(_veto_ignored_events(pipeline)) == 1, "the veto flag is recorded, not enforced"
    assert _veto_ignored_events(pipeline)[0][0] is None, "recorded as a run-level event"


def test_approved_false_alone_lets_the_whole_batch_through():
    """approved=False with no drops and no scaling is a pure no-op: every
    proposed entry proceeds."""
    decisions = [_xle(), _chpx()]
    verdict = RiskVerdict(
        approved=False,
        reasoning_chain=_rc(),
        reasoning="the whole plan feels aggressive but I name nothing",
    )
    pipeline = _stage_pipeline(verdict=verdict, decisions=decisions)
    ctx = _ctx(decisions)

    result = RiskStage(pipeline=pipeline).run(ctx)

    assert result is None
    assert _symbols(ctx) == ["XLE", "CHPX"], "nothing is dropped by a bare veto flag"
    assert len(_veto_ignored_events(pipeline)) == 1


def test_scale_all_buys_zero_is_advisory_and_does_not_stop_new_buying():
    """Board items 134 + 162 (owner ruling 2026-09-25, reaffirming 2026-09-19).
    scale_all_buys is ADVISORY on entries: even 0.0 — the old full-entry-side
    stop — no longer drops or resizes any new BUY/SHORT. Every entry survives at
    its proposed size, the proposed exit still flows, and the seat's exposure
    concern is recorded as a `scale_advisory` event per entry (never a drop)."""
    decisions = [_xle(), _chpx(), _sell("HELD")]
    verdict = RiskVerdict(
        approved=False,
        reasoning_chain=_rc(),
        scale_all_buys=0.0,
        reasoning="stop all new buying in this regime",
    )
    pipeline = _stage_pipeline(verdict=verdict, decisions=decisions)
    ctx = _ctx(decisions)

    result = RiskStage(pipeline=pipeline).run(ctx)

    assert result is None, "not a rejection — the exit must still flow to execution"
    # Entries are neither dropped nor resized; the SELL flows too.
    assert _symbols(ctx) == ["XLE", "CHPX", "HELD"]
    by_sym = {d.symbol: d for d in ctx.portfolio_decision.decisions}
    assert by_sym["XLE"].allocation_pct == 5.0
    assert by_sym["CHPX"].allocation_pct == 6.0
    # The concern is recorded per entry; no scale-driven DROP exists any more.
    advis = [e for e in _events(pipeline) if e[2] == "scale_advisory"]
    assert {e[0] for e in advis} == {"XLE", "CHPX"}
    assert not [e for e in _events(pipeline) if e[2] == "scaled_out"]


def test_a_proposed_protective_sell_is_never_dropped_by_the_seat():
    """Ruling test 3 — a SELL named in rejected_symbols is a protective exit
    and can NEVER be dropped/blocked; it is kept and the attempt is recorded."""
    decisions = [_xle(), _sell("HELD")]
    verdict = RiskVerdict(
        approved=False,
        reasoning_chain=_rc(),
        rejected_symbols=[
            {"symbol": "XLE", "reason": XLE_RR, "category": "other"},
            {"symbol": "HELD", "reason": "seat wrongly tries to cancel the exit", "category": "other"},
        ],
        reasoning="drop XLE; also (wrongly) tries to cancel the HELD exit",
    )
    pipeline = _stage_pipeline(verdict=verdict, decisions=decisions)
    ctx = _ctx(decisions)

    result = RiskStage(pipeline=pipeline).run(ctx)

    assert result is None
    assert "HELD" in _symbols(ctx), "the protective SELL is NEVER dropped"
    assert _symbols(ctx) == ["HELD"], "XLE (a new entry) is dropped; the SELL survives"
    prot = _exit_protected_events(pipeline)
    assert [e[0] for e in prot] == ["HELD"], "the ignored exit-refusal is recorded"
    assert not [e for e in _events(pipeline) if e[0] == "HELD" and e[2] == "rejected"], (
        "the exit must never carry a 'rejected' event"
    )


def test_a_proposed_cover_is_never_dropped_by_the_seat():
    """Ruling test 3 (COVER variant) — a COVER is a protective exit too."""
    decisions = [_cover("SHRT"), _chpx()]
    verdict = RiskVerdict(
        approved=False,
        reasoning_chain=_rc(),
        rejected_symbols=[{"symbol": "SHRT", "reason": "seat tries to cancel the cover", "category": "other"}],
        reasoning="tries to cancel the cover",
    )
    pipeline = _stage_pipeline(verdict=verdict, decisions=decisions)
    ctx = _ctx(decisions)

    result = RiskStage(pipeline=pipeline).run(ctx)

    assert result is None
    assert set(_symbols(ctx)) == {"SHRT", "CHPX"}, "COVER kept; unrelated entry proceeds"
    assert [e[0] for e in _exit_protected_events(pipeline)] == ["SHRT"]


def test_correlation_cluster_is_handled_by_dropping_the_named_names():
    """Ruling test 4 — a correlation cluster is handled by dropping/shrinking
    the correlated NAMES; an unrelated name in the same batch still trades."""
    decisions = [_xle(), _chpx()]  # treat XLE+one more as the cluster
    unrelated = TradeDecision(
        action="BUY",
        symbol="AAPL",
        allocation_pct=4.0,
        entry_price=200.0,
        stop_loss=190.0,
        take_profit=230.0,
        reasoning="unrelated tech name",
        thesis_invalid_if="closes below support",
    )
    decisions.append(unrelated)
    verdict = RiskVerdict(
        approved=False,
        reasoning_chain=_rc(),
        reason_category="correlation_risk",
        rejected_symbols=[
            {"symbol": "XLE", "reason": "energy cluster leg 1", "category": "other"},
            {"symbol": "CHPX", "reason": "energy cluster leg 2", "category": "other"},
        ],
        reasoning="thin the energy cluster; AAPL is unrelated",
    )
    pipeline = _stage_pipeline(verdict=verdict, decisions=decisions)
    ctx = _ctx(decisions)

    result = RiskStage(pipeline=pipeline).run(ctx)

    assert result is None
    assert _symbols(ctx) == ["AAPL"], "the cluster names drop; the unrelated name trades"


def test_hard_gate_still_enforced_after_scale():
    """Ruling test 5 — the deterministic hard gate (gross/exposure) still runs
    AFTER scaling and can still block. Unchanged backstop; the seat's flag
    never touched it."""
    decisions = [_xle(), _chpx()]
    verdict = RiskVerdict(
        approved=True,
        reasoning_chain=_rc(),
        scale_all_buys=0.5,
        reasoning="trim the entry side",
    )
    pipeline = _stage_pipeline(verdict=verdict, decisions=decisions)
    pipeline.risk_gate._persist_hard_risk_block = MagicMock()
    calls = {"n": 0}

    def _filter(d, *a, **kw):
        calls["n"] += 1
        if calls["n"] >= 2:
            # Post-scale call: gross ceiling breached -> block everything.
            return [], [], ["max_gross_exposure breach after scaling"]
        return list(d), [], []

    pipeline.risk_gate._filter_hard_risk_decisions = MagicMock(side_effect=_filter)

    result = RiskStage(pipeline=pipeline).run(_ctx(decisions))

    assert pipeline.risk_gate._filter_hard_risk_decisions.call_count == 2, "the hard gate re-runs after scaling"
    assert result == {
        "status": "hard_risk_block",
        "orders": [],
        "reason": "max_gross_exposure breach after scaling",
    }


def test_scale_all_buys_does_not_resize_the_survivors():
    """Board items 134 + 162. A per-symbol refusal still drops its name (that
    lever is unchanged), but scale_all_buys is advisory on entries: the survivor
    is NOT resized — it keeps its proposed size and the scale concern is recorded
    against it instead."""
    decisions = [_xle(), _chpx()]
    verdict = RiskVerdict(
        approved=True,
        reasoning_chain=_rc(),
        reason_category="rr_fail",
        rejected_symbols=[{"symbol": "XLE", "reason": XLE_RR, "category": "other"}],
        scale_all_buys=0.5,
        reasoning="XLE refused; scale flagged, advisory only",
    )
    pipeline = _stage_pipeline(verdict=verdict, decisions=decisions)
    ctx = _ctx(decisions)

    assert RiskStage(pipeline=pipeline).run(ctx) is None
    survivors = ctx.portfolio_decision.decisions
    assert [d.symbol for d in survivors] == ["CHPX"]
    assert survivors[0].allocation_pct == pytest.approx(6.0)  # unchanged, not 3.0
    advis = [e for e in _events(pipeline) if e[2] == "scale_advisory"]
    assert {e[0] for e in advis} == {"CHPX"}


def test_an_empty_verdict_behaves_exactly_as_before():
    """Every historical verdict, and any model that never emits the field,
    must replay with byte-identical behaviour."""
    decisions = [_xle(), _chpx()]
    verdict = RiskVerdict(
        approved=True,
        reasoning_chain=_rc(),
        reasoning="clean",
    )
    assert verdict.rejected_symbols == []
    pipeline = _stage_pipeline(verdict=verdict, decisions=decisions)
    ctx = _ctx(decisions)

    assert RiskStage(pipeline=pipeline).run(ctx) is None
    assert _symbols(ctx) == ["XLE", "CHPX"]


# ---------------------------------------------------------------------------
# Schema: a refusal must never be lost to a formatting slip
# ---------------------------------------------------------------------------


def test_a_single_bare_object_is_normalized_into_a_refusal():
    """Dropping a malformed refusal is fail-OPEN — the refused name would
    trade. A single object that still names a symbol and its category is
    wrapped into the list instead."""
    verdict = RiskVerdict(
        approved=True,
        reasoning_chain=_rc(),
        reasoning="r",
        rejected_symbols={"symbol": "xle", "reason": "R/R below floor", "category": "rr_fail"},
    )
    assert verdict.rejections_by_symbol() == {"XLE": "R/R below floor"}


@pytest.mark.parametrize(
    "raw",
    (
        [{"reason": "no symbol named"}],
        [{"symbol": ""}],
        7,
        # Owner ruling 2026-10-16: every refusal names its own category. A
        # shorthand that cannot carry one fails the verdict closed rather
        # than refusing on an unknown cause.
        ["xle"],
        "XLE",
        {"XLE": "R/R below floor"},
        [{"symbol": "XLE", "reason": "no category"}],
    ),
)
def test_a_refusal_naming_no_symbol_fails_the_verdict_closed(raw):
    """We know a refusal was intended and cannot tell which name it was for.
    Failing the whole verdict closed refuses everything — the conservative
    direction — instead of silently trading a name that was refused."""
    with pytest.raises(Exception) as exc:
        RiskVerdict(
            approved=True,
            reasoning_chain=_rc(),
            reasoning="r",
            rejected_symbols=raw,
        )
    assert any(e["loc"][0] == "rejected_symbols" for e in exc.value.errors())


def test_rejections_by_symbol_keeps_the_first_reason_per_name():
    verdict = RiskVerdict(
        approved=True,
        reasoning_chain=_rc(),
        reasoning="r",
        rejected_symbols=[
            EntrySymbolRejection(symbol="XLE", reason="first", category="other"),
            EntrySymbolRejection(symbol="xle", reason="second", category="other"),
        ],
    )
    assert verdict.rejections_by_symbol() == {"XLE": "first"}


# ---------------------------------------------------------------------------
# Repair path: a refusal is decision-bearing, not prose
# ---------------------------------------------------------------------------

_BASE_VERDICT = {
    "approved": True,
    "reasoning_chain": {
        "rr_audit": "XLE 1.18 below floor; CHPX 3.03 clean.",
        "signal_fidelity": "Both align with Tech.",
        "correlation_check": "No cluster — different sectors.",
        "event_risk": "No fetched date inside the window for either.",
    },
    "modifications": [],
    "rejected_symbols": [{"symbol": "XLE", "reason": XLE_RR, "category": "other"}],
    "scale_all_buys": 1.0,
    "reason_category": "rr_fail",
    "reasoning": "XLE refused; CHPX proceeds.",
}


def _complete(payload: dict) -> dict:
    """The same verdict with the two omitted narrative fields supplied —
    a legitimate schema repair and nothing more."""
    out = json.loads(json.dumps(payload))
    out["reasoning_chain"]["sizing_sanity"] = "CHPX size proportional to R/R."
    out["reasoning_chain"]["overall"] = "One refusal, one trade."
    return out


def _rm() -> RiskManagerAgent:
    return RiskManagerAgent.__new__(RiskManagerAgent)


def _run_review(agent, first: str, repaired: str | None):
    """Drive `review()` with a canned first response and repair response."""
    calls: list[str] = []

    def fake_execute(user_message, **kwargs):
        calls.append(user_message)
        raw = first if len(calls) == 1 else repaired
        return AgentResult(raw_text=raw or "", tokens_used=0, model="t", user_message=user_message)

    agent.run = lambda **kw: fake_execute("first")  # type: ignore[assignment]
    agent._execute = fake_execute  # type: ignore[assignment]
    verdict, _ = agent.review(
        portfolio_decision=PortfolioDecision(
            reasoning_chain=_pm_rc(),
            targets=[],
            portfolio_view="v",
        ),
        positions=[],
        macro_summary={},
        rule_violations=[],
    )
    return verdict, calls


def test_repair_preserving_the_refusal_is_accepted():
    agent = _rm()
    verdict, calls = _run_review(
        agent,
        json.dumps(_BASE_VERDICT),
        json.dumps(_complete(_BASE_VERDICT)),
    )
    assert verdict is not None
    assert verdict.rejections_by_symbol() == {"XLE": XLE_RR}
    assert len(calls) == 2, "one first call plus one bounded repair"


def test_repair_that_drops_the_refusal_fails_closed():
    """A repair that quietly reinstates a refused symbol has re-decided which
    trades die. That is not schema completion."""
    tampered = _complete(_BASE_VERDICT)
    tampered["rejected_symbols"] = []
    agent = _rm()
    verdict, _ = _run_review(agent, json.dumps(_BASE_VERDICT), json.dumps(tampered))
    assert verdict is None


def test_repair_that_adds_a_refusal_fails_closed():
    tampered = _complete(_BASE_VERDICT)
    tampered["rejected_symbols"].append({"symbol": "CHPX", "reason": "changed my mind", "category": "other"})
    agent = _rm()
    verdict, _ = _run_review(agent, json.dumps(_BASE_VERDICT), json.dumps(tampered))
    assert verdict is None


def test_repair_that_rewrites_a_refusal_reason_fails_closed():
    tampered = _complete(_BASE_VERDICT)
    tampered["rejected_symbols"][0]["reason"] = "a different reason entirely"
    agent = _rm()
    verdict, _ = _run_review(agent, json.dumps(_BASE_VERDICT), json.dumps(tampered))
    assert verdict is None


def test_malformed_refusal_skips_the_repair_call_entirely():
    """A validation failure rooted in `rejected_symbols` cannot be repaired
    without the model re-deciding — fail closed without spending the call."""
    broken = json.loads(json.dumps(_BASE_VERDICT))
    broken["rejected_symbols"] = [{"reason": "names no symbol"}]
    agent = _rm()
    verdict, calls = _run_review(agent, json.dumps(broken), json.dumps(_BASE_VERDICT))
    assert verdict is None
    assert len(calls) == 1, "no repair reprompt for a decision-bearing failure"


# ---------------------------------------------------------------------------
# The exit path gets the same split
# ---------------------------------------------------------------------------


def _exit_pipeline(verdict):
    """Mirrors tests/test_phase3_exit_rework.py's `_risk_pipeline`, kept local
    so the Phase 10.1 behaviour reads in one file."""
    from src.pipeline import TradingPipeline

    p = build_pipeline(db=MagicMock(), broker=MagicMock(), risk_manager=MagicMock())
    p.broker.get_current_stop_price.return_value = None
    p._atr_for_symbol = MagicMock(return_value=2.0)
    p._build_portfolio_heat = MagicMock(return_value=None)
    p.risk_manager.review.return_value = (
        verdict,
        MagicMock(
            user_message="u",
            raw_text="r",
            model="m",
            tokens_used=1,
            input_tokens=1,
            output_tokens=1,
            cost_usd=0.0,
        ),
    )
    return p


def _position(symbol):
    from src.models import Position

    return Position(
        symbol=symbol,
        qty=10,
        avg_entry=100.0,
        current_price=110.0,
        market_value=1100.0,
        unrealized_pnl=100.0,
        sector="Technology",
    )


def _two_exits():
    from src.models import PositionAction, PositionReasoningChain, PositionReview

    return PositionReview(
        reasoning_chain=PositionReasoningChain(
            macro_continuity_check="stable",
            thesis_progress_check="broken",
            thesis_integrity_check="invalidation hit",
            winners_discipline_check="n/a",
            session_disposition_check="midday",
            execution_rationale="exit",
        ),
        actions=[
            PositionAction(action="SELL", symbol="AAA", reason="thesis_invalid triggered"),
            PositionAction(action="SELL", symbol="BBB", reason="thesis_invalid triggered"),
        ],
        overall_assessment="two exits",
        risk_level="moderate",
    )


def _objection_rows(pipeline):
    """`exit_refusal` rows carrying the advisory-objection code, by symbol."""
    import json

    from src.risk.exit_refusal import CODE_AI_RISK_OBJECTION, EXIT_REFUSAL_KIND

    rows = {}
    for c in pipeline.db.insert_specialist_evidence.call_args_list:
        if c.kwargs.get("kind") != EXIT_REFUSAL_KIND:
            continue
        payload = json.loads(c.kwargs["evidence_json"])
        if payload["code"] == CODE_AI_RISK_OBJECTION:
            rows[c.kwargs["symbol"]] = payload
    return rows


def test_a_per_symbol_refusal_is_advisory_on_that_exit():
    # 2026-09-19 decision: the seat may not block an exit. A per-name
    # rejection now proceeds and leaves that name's own objection row.
    verdict = RiskVerdict(
        approved=True,
        reasoning_chain=_rc(),
        rejected_symbols=[{"symbol": "AAA", "reason": "invalidation not confirmed", "category": "other"}],
        reasoning="BBB may exit",
    )
    pipeline = _exit_pipeline(verdict)

    vetoed, returned = pipeline._risk_review_exits(
        _two_exits(),
        [_position("AAA"), _position("BBB")],
        run_id="r1",
        total_value=100_000.0,
    )

    assert vetoed == set()
    assert returned is verdict
    rows = _objection_rows(pipeline)
    assert set(rows) == {"AAA"}
    assert rows["AAA"]["dropped"] is False
    assert rows["AAA"]["detail"] == "invalidation not confirmed", (
        "the objected exit must record ITS OWN reason, not the run narrative"
    )


def test_book_level_reject_holds_no_exit_and_records_each():
    verdict = RiskVerdict(
        approved=False,
        reasoning_chain=_rc(),
        reasoning="drawdown state — hold everything",
    )
    pipeline = _exit_pipeline(verdict)

    vetoed, _ = pipeline._risk_review_exits(
        _two_exits(),
        [_position("AAA"), _position("BBB")],
        run_id="r1",
        total_value=100_000.0,
    )

    assert vetoed == set()
    assert set(_objection_rows(pipeline)) == {"AAA", "BBB"}


# ---------------------------------------------------------------------------
# The model has to know the field exists
# ---------------------------------------------------------------------------


def test_prompt_teaches_the_field_and_the_book_wide_handling():
    """A schema the model does not know about produces malformed responses,
    which fail closed and trade nothing — the exact outcome being removed."""
    text = PROMPT_PATH.read_text()
    assert "`rejected_symbols`" in text
    assert '"rejected_symbols"' in text, "the JSON example must carry the field"
    # Book-wide risk is handled by acting on the NAMES, not by a batch stop.
    assert "act on the NAMES, not the batch" in text


def test_prompt_states_the_seat_has_no_batch_veto():
    text = PROMPT_PATH.read_text()
    lowered = text.lower()
    # The seat must be told it has no whole-batch veto.
    assert "no veto" in lowered or "no `approved: false` lever" in text
    assert "does not stop the plan" in lowered or "never stops the plan" in lowered
    assert "Err on the side of capital preservation" in text
    # A suspected engine-missed hard rule is routed through rejected_symbols.
    assert "engine missed" in lowered and "rejected_symbols" in text
    # Protective exits / holdings are explicitly out of the seat's reach.
    assert "never" in lowered and "protective exit" in lowered
