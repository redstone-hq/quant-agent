"""Owner ruling 2026-10-13: selling follows the same rules as buying.

A holding is sold BY JUDGEMENT only when it fails the entry bar a buy of it
would have to pass at that minute (`src/exits/same_rules_gate.py`). Every
test here fails on trunk 64a64d7b (the gate and its wiring do not exist
there; see the PR for the recorded run).
"""

from __future__ import annotations

import inspect
from pathlib import Path
from types import SimpleNamespace

from src.exits.same_rules_gate import (
    BASIS_BOUGHT_TODAY,
    BASIS_EVIDENCE_MISSING,
    BASIS_FAILS_BAR,
    BASIS_PASSES_BAR,
    HoldingViews,
    bought_today_from_db,
    gate_review_actions,
    judge_holding,
    views_from_pipeline,
)
from src.models.positions import PositionAction, PositionReasoningChain, PositionReview

ROOT = Path(__file__).resolve().parents[1]


def _v(seat, symbol, direction):
    return SimpleNamespace(
        seat=seat,
        symbol=symbol,
        direction=direction,
        invalidation="closes below the breakout level" if direction != "neutral" else "",
        evidence=[object()] if direction != "neutral" else [],
    )


def _passing(symbol):
    return (_v("technical", symbol, "bullish"), _v("news", symbol, "bullish"))


class _DB:
    def __init__(self, rows=(), raise_on_read=False):
        self.rows = list(rows)
        self.raise_on_read = raise_on_read
        self.evaluations = []

    def get_trades(self, symbol=None, limit=50, today_only=False):
        if self.raise_on_read:
            raise RuntimeError("db locked")
        return [r for r in self.rows if r["symbol"] == symbol]

    def record_intraday_evaluation(self, **kw):
        self.evaluations.append(kw)


def _pipeline_ctx(verdicts, *, last_blocked=None, rows=(), data_status=None):
    pipeline = SimpleNamespace(
        portfolio_manager=SimpleNamespace(last_blocked={} if last_blocked is None else last_blocked),
        db=_DB(rows),
    )
    ctx = SimpleNamespace(
        seat_verdicts=list(verdicts),
        data_status=data_status or {"tech": "ok", "news": "ok"},
        evidence_registry={},
    )
    return pipeline, ctx


def _review(symbol, action, reason, **extra):
    chain = PositionReasoningChain(**{f: "x" for f in PositionReasoningChain.model_fields})
    return PositionReview(
        reasoning_chain=chain,
        overall_assessment="test",
        risk_level="low",
        actions=[PositionAction(action=action, symbol=symbol, reason=reason, **extra)],
    )


# (a) ETN 13 Oct: PM SELL "no directional support" of a protected holding
# that still passes the entry bar is refused.
def test_a_pm_sell_of_holding_that_passes_bar_is_refused():
    pipeline, ctx = _pipeline_ctx(_passing("ETN"))
    verdict = judge_holding(views_from_pipeline(pipeline, ctx, "ETN", is_short=False))
    assert verdict.sell_allowed is False
    assert verdict.basis == BASIS_PASSES_BAR


def test_a_pm_sale_path_is_decided_by_the_gate_not_the_claim_check():
    src = (ROOT / "src" / "stage_risk.py").read_text()
    assert "judge_holding(views_from_pipeline(" in src
    assert "holding_discipline_claim_check" not in src
    assert "veto_contradicted_exit" not in src


# (b) AAPL 13 Oct: close-reviewer SELL on one medium headline while the bar
# still passes is refused (turned into a HOLD that records why).
def test_b_reviewer_sell_on_one_headline_while_bar_passes_is_refused():
    review = _review("AAPL", "SELL", "medium-grade adverse headline")
    views = {"AAPL": HoldingViews("AAPL", False, _passing("AAPL"), eligibility_reasons=())}
    gated, verdicts = gate_review_actions(review, views)
    assert gated.actions[0].action == "HOLD"
    assert "same-rules gate refused" in gated.actions[0].reason
    assert verdicts[0].basis == BASIS_PASSES_BAR


def test_b_reviewer_session_routes_sales_through_the_gate(monkeypatch):
    from src.agents.portfolio_manager import PortfolioManagerAgent
    from src.sessions.position_review_session import PositionReviewSession

    monkeypatch.setattr(
        PortfolioManagerAgent,
        "_collect_seat_verdicts",
        staticmethod(lambda **kw: [_v("news", "AAPL", "neutral")]),
    )
    db = _DB()
    fake = SimpleNamespace(_db=db)
    gated = PositionReviewSession._gate_reviewer_sales(
        fake,
        _review("AAPL", "SELL", "medium-grade adverse headline"),
        [SimpleNamespace(symbol="AAPL", qty=10)],
        run_id="r1",
        news_intel=None,
        news_ok=True,
        macro_analysis=None,
        earnings_analyses=[],
    )
    assert gated.actions[0].action == "HOLD"
    assert db.evaluations and db.evaluations[0]["status"].startswith("same_rules_gate_")
    src = inspect.getsource(PositionReviewSession.run)
    assert "self._gate_reviewer_sales(" in src
    assert "self._risk_review_exits(" not in src


# (c) A bearish news seat fails the bar (a buy would be refused), so it sells.
def test_c_bearish_news_seat_fails_bar_and_sells():
    verdicts = (_v("technical", "XYZ", "bullish"), _v("news", "XYZ", "bearish"))
    v = judge_holding(HoldingViews("XYZ", False, verdicts, eligibility_reasons=()))
    assert v.sell_allowed is True
    assert v.basis == BASIS_FAILS_BAR
    assert any("news" in r for r in v.reasons)
    # The reviewer session holds no technical read; an opposed news seat is a
    # failure no missing view can undo, so the reviewer sale still goes.
    review = _review("XYZ", "SELL", "bearish news")
    gated, _ = gate_review_actions(
        review, {"XYZ": HoldingViews("XYZ", False, (_v("news", "XYZ", "bearish"),), eligibility_reasons=None)}
    )
    assert gated.actions[0].action == "SELL"


# (d) A missing technical view gives no sale, with the reason recorded.
def test_d_missing_tech_view_gives_no_sale_and_records_reason():
    v = judge_holding(HoldingViews("XYZ", False, (_v("news", "XYZ", "bullish"),), eligibility_reasons=None))
    assert v.sell_allowed is False
    assert v.basis == BASIS_EVIDENCE_MISSING
    assert "no technical read" in v.detail
    # A failed seat that feeds R5 makes an R5-only failure unknown, not fail.
    pipeline, ctx = _pipeline_ctx(
        _passing("XYZ"),
        last_blocked={"XYZ": str(["R5 net evidence +0 if long — no rung"])},
        data_status={"tech": "ok", "news": "failed"},
    )
    v2 = judge_holding(views_from_pipeline(pipeline, ctx, "XYZ", is_short=False))
    assert v2.sell_allowed is False and v2.basis == BASIS_EVIDENCE_MISSING


# (e) A name bought today is never sold by judgement, even if it fails the bar.
def test_e_bought_today_is_not_sold_by_judgement():
    db = _DB([{"symbol": "NEW", "action": "BUY"}])
    assert bought_today_from_db(db, "NEW") is True
    fails = (_v("technical", "NEW", "bearish"), _v("news", "NEW", "bearish"))
    v = judge_holding(HoldingViews("NEW", False, fails, eligibility_reasons=(), bought_today=True))
    assert v.sell_allowed is False and v.basis == BASIS_BOUGHT_TODAY
    assert bought_today_from_db(_DB(raise_on_read=True), "NEW") is None
    unreadable = judge_holding(HoldingViews("NEW", False, fails, eligibility_reasons=(), bought_today=None))
    assert unreadable.sell_allowed is False


# (f) A stop exit still sells: mechanical exits never pass through the gate.
def test_f_stop_and_mechanical_exits_bypass_the_gate():
    review = _review("XYZ", "TRAIL_STOP", "tighten", new_stop_price=10.0)
    gated, verdicts = gate_review_actions(review, {})
    assert gated.actions[0].action == "TRAIL_STOP" and verdicts == []
    for rel in (
        "src/pipeline_delever.py",
        "src/risk/exit_guard_trend.py",
        "src/pipeline_exits.py",
        "src/stage_execution.py",
    ):
        assert "same_rules_gate" not in (ROOT / rel).read_text(), rel


# The half-hour check now culls on the FULL bar (13 Oct supersedes 25 Sep):
# a soft miss (technical not confirming) is culled, a passing name is not.
def test_half_hour_check_uses_full_bar():
    from src.pipeline_rotation_exec import _same_rules_opportunity

    verdicts = list(_passing("KEEP")) + [_v("technical", "SOFT", "neutral"), _v("news", "SOFT", "bullish")]
    pipeline, ctx = _pipeline_ctx(verdicts)
    positions = [SimpleNamespace(symbol="KEEP", qty=5), SimpleNamespace(symbol="SOFT", qty=5)]
    opp = _same_rules_opportunity(pipeline, ctx, None, positions)
    assert opp is not None and opp.tier == "ineligible_hold"
    assert [s for s, _ in opp.ineligible_candidates] == ["SOFT"]


def opposed_for_exits(decisions):
    """A news seat opposed on every judgement sale in `decisions`, so each
    holding FAILS the entry bar and the same-rules gate lets the sale through.
    Shared by older RiskStage tests whose subject is not the gate."""
    return [
        _v("news", d.symbol, "bullish" if d.action == "COVER" else "bearish")
        for d in decisions
        if d.action in ("SELL", "REDUCE", "COVER")
    ]


# ---------------------------------------------------------------------------
# Behaviour at the CALL SITES. Each test below drives the real path (RiskStage,
# the reviewer session, the half-hour rotation step) and, except (c), fails on
# trunk with an assertion because trunk lets the sale through.
# ---------------------------------------------------------------------------


def _risk_stage_run(decisions, seat_verdicts, *, protected=True, trades_today=()):
    from unittest.mock import MagicMock, patch

    from src.pipeline_stages import RiskStage
    from src.risk.exit_guard import StructuralProtectionCheck
    from tests.test_item96_morning_exit_contradiction import _ctx, _stage_pipeline

    pipeline = _stage_pipeline(decisions=decisions, metric_deltas={})
    pipeline._structural_protection_for_holding = MagicMock(
        return_value=StructuralProtectionCheck(protected=protected, basis="structural_level_intact", detail="held")
    )
    pipeline.portfolio_manager.last_blocked = {}
    pipeline.db.get_trades.return_value = [dict(r) for r in trades_today]
    ctx = _ctx(decisions)
    ctx.seat_verdicts = list(seat_verdicts)
    with patch("src.notifier.send_owner_alert"):
        result = RiskStage(pipeline=pipeline).run(ctx)
    return result, ctx


def _symbols(ctx):
    return [d.symbol for d in ctx.portfolio_decision.decisions]


# (a) ETN 13 Oct at the call site: a protected holding that still passes the
# entry bar is NOT sold on the PM's "no directional support".
def test_a_risk_stage_refuses_pm_sell_of_holding_that_passes_bar():
    from tests.test_item96_morning_exit_contradiction import _buy, _sell

    decisions = [_sell("ETN", "No directional support — exit ETN."), _buy("CHPX")]
    result, ctx = _risk_stage_run(decisions, _passing("ETN"))
    assert result is None
    assert _symbols(ctx) == ["CHPX"], "the PM SELL of a holding that passes the bar must be refused"


# (c) at the call site: a holding a buy would refuse (news seat opposed) is
# sold, and RiskStage completes with the BUY alongside it. Passes on trunk too
# (trunk sells it); it guards against the gate over-refusing.
def test_c_risk_stage_lets_pm_sell_of_holding_that_fails_bar_through():
    from tests.test_item96_morning_exit_contradiction import _buy, _sell

    decisions = [_sell("ETN", "Bearish guidance cut."), _buy("CHPX")]
    result, ctx = _risk_stage_run(decisions, (_v("technical", "ETN", "bullish"), _v("news", "ETN", "bearish")))
    assert result is None
    assert _symbols(ctx) == ["ETN", "CHPX"]


# (d) at the call site: no technical read for the holding -> no sale.
def test_d_risk_stage_refuses_pm_sell_when_tech_view_missing():
    from tests.test_item96_morning_exit_contradiction import _sell

    decisions = [_sell("ETN", "No directional support — exit ETN.")]
    result, ctx = _risk_stage_run(decisions, (_v("news", "ETN", "bullish"),), protected=False)
    assert result is not None and result["status"] == "rejected"
    assert "no technical read" in result["reason"]
    assert _symbols(ctx) == []


# (e) at the call site: bought today -> never sold by judgement, even when a
# buy of it would now be refused.
def test_e_risk_stage_refuses_pm_sell_of_name_bought_today():
    from tests.test_item96_morning_exit_contradiction import _sell

    fails = (_v("technical", "ETN", "bearish"), _v("news", "ETN", "bearish"))
    decisions = [_sell("ETN", "Bearish news.")]
    result, ctx = _risk_stage_run(decisions, fails, protected=False, trades_today=[{"symbol": "ETN", "action": "BUY"}])
    assert result is not None and result["status"] == "rejected"
    assert "bought today" in result["reason"]
    assert _symbols(ctx) == []


# (b) AAPL 13 Oct at the call site: the close reviewer's SELL on a headline,
# with no seat opposed, reaches the executor as a HOLD.
def test_b_reviewer_session_hands_executor_a_hold_not_the_sell():
    import pytest

    from src.sessions.position_review_session import PositionReviewSession
    from tests.test_midday_session_gross_ceiling import _pipeline, _Reached, _resolver, _session_kwargs

    kw = _session_kwargs(_pipeline(_resolver([])))
    kw["position_reviewer"].review.return_value = (
        _review("AAA", "SELL", "medium-grade adverse headline"),
        SimpleNamespace(used_fallback=False, raw_text="{}"),
    )
    with pytest.raises(_Reached):
        PositionReviewSession(**kw).run("close")
    handed = kw["midday_execute_llm_actions"].call_args.args[1]
    assert [a.action for a in handed.actions] == ["HOLD"]


# Half-hour check at the call site: a holding the 25 Sep STAY test keeps (the
# PM surfaced nothing) but that fails the FULL bar (technical not confirming)
# is now closed; a passing holding is not.
def test_half_hour_rotation_closes_holding_that_fails_full_bar(tmp_path):
    from src.pipeline_context import RunContext
    from src.pipeline_rotation_exec import _apply_rotation_execution
    from tests.test_rotation_execute import HISTORY, _buy_new, _decision, _pos, _precheck
    from tests.test_rotation_execute import _pipeline as _rot_pipeline

    pipeline, _db, _probe = _rot_pipeline(tmp_path, precheck=_precheck(None))
    pipeline.portfolio_manager.last_blocked = {}
    ctx = RunContext(run_id="run-1", session="morning")
    ctx.seat_verdicts = [_v("technical", "OLD", "neutral"), _v("news", "OLD", "bullish"), *_passing("KEEP")]
    decision = _decision(_buy_new())
    _apply_rotation_execution(pipeline, ctx, decision, [_pos("OLD"), _pos("KEEP")], HISTORY)
    assert [t.symbol for t in decision.targets if t.is_close] == ["OLD"]
