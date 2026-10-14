"""Owner decision 14 Oct 2026: a judgement sale of a LOSING stock with no chart
level under its stop (`no_chart_level`) must pass the buy-rule check (every buy
rule except timing) before it sells; if the holding still passes, it is held and
its stop caps the loss. A structure break confirmed on its closes is a chart
fact and still sells ungated."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.exits import same_rules_gate as g
from src.models.positions import PositionAction, PositionReasoningChain, PositionReview


def _v(seat, symbol, direction):
    return SimpleNamespace(
        seat=seat,
        symbol=symbol,
        direction=direction,
        invalidation="closes below the breakout level",
        evidence=[object()],
    )


def _passing(symbol):
    return tuple(_v(seat, symbol, "bullish") for seat in ("technical", "news", "earnings", "smart_money", "macro"))


def _views(symbol, basis, **kw):
    kw.setdefault("eligibility_reasons", ())
    return g.HoldingViews(symbol=symbol, is_short=False, seat_verdicts=_passing(symbol), protection_basis=basis, **kw)


class _DB:
    def get_trades(self, symbol=None, limit=50, today_only=False):
        return []

    def record_intraday_evaluation(self, **kw):
        pass


def test_a_no_chart_level_loser_that_passes_the_bar_is_held():
    verdict = g.judge_holding(_views("ACME", "no_chart_level"))
    assert verdict.sell_allowed is False
    assert verdict.basis == g.BASIS_PASSES_BAR


def test_a_pm_path_no_chart_level_loser_that_passes_the_bar_is_held():
    pipeline = SimpleNamespace(db=_DB())
    ctx = SimpleNamespace(
        seat_verdicts=list(_passing("ACME")),
        data_status={"tech": "ok", "news": "ok"},
        evidence_registry={},
        held_bar_record={"ACME": ()},
    )
    views = g.views_from_pipeline(pipeline, ctx, "ACME", is_short=False, protection_basis="no_chart_level")
    verdict = g.judge_holding(views)
    assert verdict.sell_allowed is False and verdict.basis == g.BASIS_PASSES_BAR


def test_a_reviewer_cut_of_a_no_chart_level_loser_is_turned_into_hold():
    chain = PositionReasoningChain(**{f: "x" for f in PositionReasoningChain.model_fields})
    review = PositionReview(
        reasoning_chain=chain,
        overall_assessment="test",
        risk_level="low",
        actions=[PositionAction(action="SELL", symbol="ACME", reason="losing, no level")],
    )
    gated, verdicts = g.gate_review_actions(review, {"ACME": _views("ACME", "no_chart_level")})
    assert gated.actions[0].action == "HOLD"
    assert verdicts[0].sell_allowed is False


def test_a_no_chart_level_loser_that_fails_r2_still_sells():
    positions = [SimpleNamespace(symbol="ACME", qty=10)]
    record = g.build_held_bar_record(positions, [SimpleNamespace(symbol="ACME", rating="sell")], {"ACME": "[]"})
    verdict = g.judge_holding(_views("ACME", "no_chart_level", eligibility_reasons=record["ACME"]))
    assert verdict.sell_allowed is True and verdict.basis == g.BASIS_FAILS_BAR


@pytest.mark.parametrize("basis", ["structural_level_broken", "thesis_invalid_if_triggered"])
def test_b_confirmed_structure_break_still_sells_ungated(basis):
    # Even with the bar passing AND today's trade record unreadable, a
    # confirmed break sells: it is a chart fact, not a judgement.
    verdict = g.judge_holding(_views("ACME", basis, bought_today=None))
    assert verdict.sell_allowed is True and verdict.basis == g.BASIS_PROTECTIVE


@pytest.mark.parametrize("basis", ["no_chart_level", "chart_unreadable", "some_future_basis"])
def test_only_confirmed_breaks_are_protective(basis):
    assert not g.is_protective(basis)
    assert not g.is_protective(None)
