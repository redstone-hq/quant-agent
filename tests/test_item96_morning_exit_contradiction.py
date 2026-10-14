"""Item 96 — the exit-contradiction gate (`veto_contradicted_exit`, Phase 3.2)
now runs on the MORNING review path, not only on the midday/close reader.

The gap: a morning SELL/REDUCE/COVER whose stated reason is a provably-false
deterioration claim ("stalling", "not progressing") while the position's own
recorded metrics net-IMPROVED since the previous review reached the broker
unchecked, because the only wiring for that gate lived in
`TradingPipeline.run_position_review` (the intraday reader). This file drives
the real morning path — `RiskStage._run_review`, reached through
`RiskStage.run` — and pins the four behaviours that make the fix safe:

  1. a false stall claim contradicted by net-improved numbers NO LONGER
     decides the sale (owner 13 Oct 2026, "same rules as buying"): the
     same-rules gate does (`src/exits/same_rules_gate.py`) — an UNPROTECTED
     holding's sale is protective and passes; a protected one with no bar
     evidence is refused, the stop stays;
  2. a genuinely-deteriorating position (numbers agree with the stall claim)
     PASSES — the gate only fires on a contradiction, never on a real cut;
  3. an exit on NEW INFORMATION (news/earnings/regime/invalidation) PASSES
     however good the numbers look — it is not a deterioration claim;
  4. no prior snapshot → no comparison → no veto.

The harness is the honest pass-through one `test_holding_discipline_block.py`
uses: `RiskStage` is stubbed only far enough to reach the gate, the hard-risk
filter returns whatever it is handed, so a decision missing at the end was
dropped by the code under test, not by a mock. The metric-delta plumbing
(`_build_position_facts` / `_build_review_metric_deltas`, tested elsewhere
against real snapshots) is replaced by a stub returning hand-built
`MetricDeltas`, so what is exercised here is the WIRING and the real
`veto_contradicted_exit`, not the delta arithmetic.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

from src.models import (
    PortfolioDecision,
    ReasoningChain,
    RiskReasoningChain,
    RiskVerdict,
    TradeDecision,
)
from src.pipeline_context import RunContext
from src.pipeline_stages import RiskStage
from src.risk.exit_guard import StructuralProtectionCheck, compute_deltas
from tests.pipeline_factory import build_pipeline


# --- delta fixtures (real MetricDeltas, built the same way the reader does) --


def _improved_deltas(symbol: str = "ACME"):
    """Every metric that moved improved, nothing worsened → net_improved."""
    return compute_deltas(
        symbol,
        prior={"thesis_progress_pct": 10.0, "r_multiple": 0.20, "pace": 0.50},
        current={"thesis_progress_pct": 40.0, "r_multiple": 1.00, "pace": 1.20},
        prior_timestamp="2026-09-24T20:00:00Z",
    )


def _worsened_deltas(symbol: str = "ACME"):
    """The position genuinely deteriorated → NOT net_improved."""
    return compute_deltas(
        symbol,
        prior={"thesis_progress_pct": 40.0, "r_multiple": 1.00, "pace": 1.20},
        current={"thesis_progress_pct": 10.0, "r_multiple": 0.20, "pace": 0.50},
        prior_timestamp="2026-09-24T20:00:00Z",
    )


def _no_prior_deltas(symbol: str = "ACME"):
    """First look — no snapshot to compare against → has_prior False."""
    return compute_deltas(
        symbol,
        prior=None,
        current={"thesis_progress_pct": 40.0, "r_multiple": 1.00},
    )


# --- stage harness (mirrors tests/test_holding_discipline_block.py) ----------


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


def _sell(symbol: str, reasoning: str) -> TradeDecision:
    return TradeDecision(
        action="SELL",
        symbol=symbol,
        allocation_pct=100.0,
        entry_price=100.0,
        stop_loss=95.0,
        take_profit=115.0,
        reasoning=reasoning,
    )


def _buy(symbol: str) -> TradeDecision:
    return TradeDecision(
        action="BUY",
        symbol=symbol,
        allocation_pct=6.0,
        entry_price=24.00,
        stop_loss=22.50,
        take_profit=28.55,
        reasoning="unrelated breakout",
        thesis_invalid_if="closes below support",
    )


def _stage_pipeline(*, decisions, metric_deltas):
    """RiskStage stubbed just far enough to reach the exit-contradiction gate.

    `protected=False` keeps the holding-discipline claim gate (which runs
    first in the same loop) from ever firing, so a dropped SELL here was
    dropped by the item-96 gate and nothing else. The metric-delta build is
    stubbed to hand back `metric_deltas` directly.
    """
    from src.pipeline import TradingPipeline

    verdict = RiskVerdict(
        approved=True,
        reasoning_chain=_rc(),
        reason_category="clean",
        reasoning="no objection at the book level",
    )
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
        _build_active_state_changes=MagicMock(return_value=""),
        _structural_protection_for_holding=MagicMock(
            return_value=StructuralProtectionCheck(
                protected=False,
                basis="noise_band_fallback",
                detail="no qualifying structural level",
            ),
        ),
        _build_position_facts=MagicMock(return_value={}),
        _build_review_metric_deltas=MagicMock(return_value=metric_deltas),
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
    ctx.macro_analysis = {"regime": "risk-on"}
    ctx.data_status = {"macro": "ok"}
    ctx.position_history = {d.symbol: {"entry_price": 100.0, "stop_loss": 95.0} for d in decisions}
    ctx.portfolio_decision = PortfolioDecision(
        reasoning_chain=_pm_rc(),
        decisions=decisions,
        portfolio_view="test",
    )
    return ctx


def _symbols(ctx) -> list[str]:
    return [d.symbol for d in ctx.portfolio_decision.decisions]


def _events(pipeline) -> list[tuple[str, str, str, str]]:
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


def _exit_refusals(pipeline) -> list[dict]:
    out = []
    for call in pipeline.db.insert_specialist_evidence.call_args_list:
        kwargs = call.kwargs
        if kwargs.get("kind") != "exit_refusal":
            continue
        out.append(json.loads(kwargs["evidence_json"]))
    return out


# --- the four pinned behaviours ---------------------------------------------


def test_false_stall_claim_no_longer_decides_an_unprotected_holdings_sale():
    """Owner 13 Oct 2026: the same-rules gate decides every judgement sale; a
    false claim is evidence, never the decider. This holding's structure read
    is UNPROTECTED (no qualifying chart level), so the gate passes the SELL as
    a protective sale — never gated — and the unrelated BUY stands too."""
    decisions = [_sell("ACME", "Cutting ACME — it is stalling, no progress."), _buy("CHPX")]
    pipeline = _stage_pipeline(
        decisions=decisions,
        metric_deltas={"ACME": _improved_deltas()},
    )
    ctx = _ctx(decisions)

    with patch("src.notifier.send_owner_alert"):
        result = RiskStage(pipeline=pipeline).run(ctx)

    assert result is None
    assert _symbols(ctx) == ["ACME", "CHPX"], "an unprotected holding's sale is protective"
    outcomes = [e[2] for e in _events(pipeline) if e[0] == "ACME"]
    assert "same_rules_gate_protective_structure_broken" in outcomes
    assert "exit_vetoed_contradicts_own_metrics" not in outcomes, "the false claim no longer decides"
    assert _exit_refusals(pipeline) == []


def test_a_genuinely_deteriorating_position_exit_passes():
    """SAME stall wording, but the numbers AGREE — the position really did go
    backwards. The gate must NOT block a real protective exit."""
    decisions = [_sell("ACME", "Cutting ACME — it is stalling, no progress.")]
    pipeline = _stage_pipeline(
        decisions=decisions,
        metric_deltas={"ACME": _worsened_deltas()},
    )
    ctx = _ctx(decisions)

    with patch("src.notifier.send_owner_alert"):
        result = RiskStage(pipeline=pipeline).run(ctx)

    assert result is None
    assert _symbols(ctx) == ["ACME"], "a real deterioration exit must survive"
    outcomes = [e[2] for e in _events(pipeline) if e[0] == "ACME"]
    assert "exit_vetoed_contradicts_own_metrics" not in outcomes
    assert _exit_refusals(pipeline) == []


def test_a_news_thesis_exit_passes_however_good_the_numbers():
    """Exit on NEW INFORMATION, not a claim about the trajectory. Even with
    net-improved numbers this is never vetoed — the reviewer keeps full
    authority over exits on news/earnings/regime/invalidation (Phase 3.8)."""
    decisions = [
        _sell(
            "ACME",
            "Selling ACME on this morning's surprise guidance cut and CEO exit.",
        )
    ]
    pipeline = _stage_pipeline(
        decisions=decisions,
        metric_deltas={"ACME": _improved_deltas()},
    )
    ctx = _ctx(decisions)

    with patch("src.notifier.send_owner_alert"):
        result = RiskStage(pipeline=pipeline).run(ctx)

    assert result is None
    assert _symbols(ctx) == ["ACME"], "a news/thesis exit must survive"
    outcomes = [e[2] for e in _events(pipeline) if e[0] == "ACME"]
    assert "exit_vetoed_contradicts_own_metrics" not in outcomes
    assert _exit_refusals(pipeline) == []


def test_no_prior_snapshot_means_no_veto():
    """First look at the position — nothing to compare against. A stall claim
    cannot be adjudicated, so the exit passes (no wrong comparison)."""
    decisions = [_sell("ACME", "Cutting ACME — it is stalling, dead money.")]
    pipeline = _stage_pipeline(
        decisions=decisions,
        metric_deltas={"ACME": _no_prior_deltas()},
    )
    ctx = _ctx(decisions)

    with patch("src.notifier.send_owner_alert"):
        result = RiskStage(pipeline=pipeline).run(ctx)

    assert result is None
    assert _symbols(ctx) == ["ACME"]
    outcomes = [e[2] for e in _events(pipeline) if e[0] == "ACME"]
    assert "exit_vetoed_contradicts_own_metrics" not in outcomes


def test_blocking_the_only_leg_returns_the_terminal_rejected_status():
    """The same-rules gate refusing the only leg returns the same terminal
    rejected status a per-symbol refusal does. The holding is PROTECTED here
    (its chart level holds), so the gate judges the entry bar; this run holds
    no seat view and no entry-rule record for it, so the bar cannot be
    evaluated — unknown never sells: the SELL is refused and the stop stays."""
    decisions = [_sell("ACME", "Cutting ACME — it is stalling, no progress.")]
    pipeline = _stage_pipeline(
        decisions=decisions,
        metric_deltas={"ACME": _improved_deltas()},
    )
    pipeline._structural_protection_for_holding = MagicMock(
        return_value=StructuralProtectionCheck(protected=True, basis="structural_level_intact", detail="intact")
    )
    ctx = _ctx(decisions)

    with patch("src.notifier.send_owner_alert"):
        result = RiskStage(pipeline=pipeline).run(ctx)

    assert result is not None
    assert result["status"] == "rejected"
    assert result["orders"] == []
    outcomes = [e[2] for e in _events(pipeline) if e[0] == "ACME"]
    assert "same_rules_gate_evidence_missing" in outcomes
    assert "rejected" in outcomes
