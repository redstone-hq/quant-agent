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
from src.models.positions import PositionAction, PositionReview

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


def _review(symbol, action, reason):
    return PositionReview(
        overall_assessment="test",
        risk_level="low",
        actions=[PositionAction(action=action, symbol=symbol, reason=reason)],
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
    review = _review("XYZ", "TRAIL_STOP", "tighten")
    review.actions[0] = review.actions[0].model_copy(update={"new_stop_price": 10.0})
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
