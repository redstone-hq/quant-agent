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
        # THIS run's record: the manager ran and evaluated every name here.
        held_bar_record={
            **{str(v.symbol).upper(): () for v in verdicts},
            **{str(k).upper(): _g.parse_blocked_reasons(r) for k, r in (last_blocked or {}).items()},
        },
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
    import ast

    # Formatting-proof: the gate is CALLED on this run's views.
    gate_calls = [
        n
        for n in ast.walk(ast.parse(src))
        if isinstance(n, ast.Call) and getattr(n.func, "id", None) == "judge_holding"
    ]
    assert any(
        isinstance(a, ast.Call) and getattr(a.func, "id", None) == "views_from_pipeline"
        for c in gate_calls
        for a in c.args
    )
    # Coordinator 14 Oct defect 3: the false-claim check stays as EVIDENCE
    # (with the owner alert); nothing drops a sale on it any more.
    assert "_record_claim_evidence(" in src
    assert "holding_discipline_claim_check(" in src
    assert "reject_false_claim" not in src
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
    fake = SimpleNamespace(_db=db, _todays_technical_reads=lambda positions: [])
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
    # Adversary 14 Oct point 5: the AI Risk seat keeps its exit role.
    assert "self._risk_review_exits(" in src


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


# The half-hour check culls on every buy rule except timing (owner Option A,
# 14 Oct): a neutral chart (technical not confirming) is KEPT; a name-level
# seat turned against the holding is culled; a passing name is kept.
def test_half_hour_check_uses_every_rule_but_timing():
    from src.pipeline_rotation_exec import _same_rules_opportunity

    verdicts = list(_passing("KEEP")) + [_v("technical", "SOFT", "neutral"), _v("news", "SOFT", "bullish")]
    verdicts += [_v("technical", "GONE", "neutral"), _v("news", "GONE", "bearish")]
    pipeline, ctx = _pipeline_ctx(verdicts)
    positions = [SimpleNamespace(symbol=s, qty=5) for s in ("KEEP", "SOFT", "GONE")]
    opp = _same_rules_opportunity(pipeline, ctx, None, positions)
    assert opp is not None and opp.tier == "ineligible_hold"
    assert [s for s, _ in opp.ineligible_candidates] == ["GONE"]


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
    ctx.held_bar_record = {str(v.symbol).upper(): () for v in seat_verdicts}
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
    result, ctx = _risk_stage_run(decisions, (_v("news", "ETN", "bullish"),), protected=True)
    assert result is not None and result["status"] == "rejected"
    assert "no technical read" in result["reason"]
    assert _symbols(ctx) == []


# (e) at the call site: bought today -> never sold by judgement, even when a
# buy of it would now be refused.
def test_e_risk_stage_refuses_pm_sell_of_name_bought_today():
    from tests.test_item96_morning_exit_contradiction import _sell

    fails = (_v("technical", "ETN", "bearish"), _v("news", "ETN", "bearish"))
    decisions = [_sell("ETN", "Bearish news.")]
    result, ctx = _risk_stage_run(decisions, fails, protected=True, trades_today=[{"symbol": "ETN", "action": "BUY"}])
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
    # The substantiation pass is injected; a MagicMock would replace the
    # review with a mock. Pass the review through unchanged so the SELL
    # reaches the same-rules gate, which is the subject here.
    kw["substantiate_exit_triggers"] = lambda review, **_k: review
    kw["position_reviewer"].review.return_value = (
        _review("AAA", "SELL", "medium-grade adverse headline"),
        # The session logs every field of the reviewer's call record; the
        # shared helper's MagicMock carries them all.
        kw["position_reviewer"].review.return_value[1],
    )
    with pytest.raises(_Reached):
        PositionReviewSession(**kw).run("close")
    handed = kw["midday_execute_llm_actions"].call_args.args[1]
    assert [a.action for a in handed.actions] == ["HOLD"]


# Half-hour check at the call site (owner Option A, 14 Oct): a holding a
# name-level seat turned against is closed even though the PM surfaced
# nothing; a neutral-chart holding and a passing holding are kept.
def test_half_hour_rotation_closes_holding_that_fails_the_held_bar(tmp_path):
    from src.pipeline_context import RunContext
    from src.pipeline_rotation_exec import _apply_rotation_execution
    from tests.test_rotation_execute import HISTORY, _buy_new, _decision, _pos, _precheck
    from tests.test_rotation_execute import _pipeline as _rot_pipeline

    pipeline, _db, _probe = _rot_pipeline(tmp_path, precheck=_precheck(None))
    pipeline.portfolio_manager.last_blocked = {}
    ctx = RunContext(run_id="run-1", session="morning")
    ctx.seat_verdicts = [_v("technical", "OLD", "neutral"), _v("news", "OLD", "bearish"), *_passing("KEEP")]
    ctx.seat_verdicts += [_v("technical", "NEUT", "neutral"), _v("news", "NEUT", "bullish")]
    # The manager ran this run: its record, in each holding's direction.
    ctx.held_bar_record = {"OLD": ("R2 neutral rating",), "KEEP": (), "NEUT": ("R2 neutral rating",)}
    decision = _decision(_buy_new())
    _apply_rotation_execution(pipeline, ctx, decision, [_pos("OLD"), _pos("KEEP"), _pos("NEUT")], HISTORY)
    assert [t.symbol for t in decision.targets if t.is_close] == ["OLD"]


# ---------------------------------------------------------------------------
# v2 + adversary (14 Oct): this run's record, classified refusals, per-stock
# known/unknown readings, protective class, held direction.
# ---------------------------------------------------------------------------
import re as _re

import pytest as _pytest

from src.exits import same_rules_gate as _g


def _all_known(symbol, tech="bullish"):
    return (
        _v("technical", symbol, tech),
        _v("news", symbol, "bullish"),
        _v("earnings", symbol, "bullish"),
        _v("smart_money", symbol, "bullish"),
        _v("macro", symbol, "bullish"),
    )


def _held_views(symbol, verdicts, record, **kw):
    pipeline, ctx = _pipeline_ctx(verdicts)
    ctx.held_bar_record = record
    for key, value in kw.items():
        setattr(ctx, key, value)
    return _g.views_from_pipeline(pipeline, ctx, symbol, is_short=False)


def test_t1_skipped_manager_stale_last_blocked_never_sells():
    pipeline, ctx = _pipeline_ctx(_passing("ACME"), last_blocked={"ACME": "['R2 neutral rating']"})
    ctx.held_bar_record = None  # the manager was skipped this run
    verdict = _g.judge_holding(_g.views_from_pipeline(pipeline, ctx, "ACME", is_short=False))
    assert not verdict.sell_allowed
    assert verdict.basis == _g.BASIS_EVIDENCE_MISSING


def test_t2_record_is_built_from_the_main_decision_in_the_holding_direction():
    positions = [SimpleNamespace(symbol="ACME", qty=10), SimpleNamespace(symbol="SHRT", qty=-5)]
    analyses = [SimpleNamespace(symbol="ACME", rating="buy"), SimpleNamespace(symbol="SHRT", rating="buy")]
    blocked = {"ACME": "['R5 net evidence -1 if long — no rung']", "SHRT": "[]"}
    record = _g.build_held_bar_record(positions, analyses, blocked)
    assert record["ACME"] == ("R5 net evidence -1 if long — no rung",)
    assert record["SHRT"][0].startswith("R2 rating buy does not point the holding's way (short)")


@_pytest.mark.parametrize(
    "code",
    [
        "risk_budget_exhausted",
        "gross_exposure_ceiling_refused",
        "below_owner_min_risk",
        "position_sized_to_zero",
        "book_risk_unknown",
        "target_not_above_entry",
        "no_structural_target",
        "dollar_target_no_stop",
    ],
)
def test_t3_account_level_refusal_never_sells(code):
    record = {"ACME": (f"R6 constructor refused [{code}] — detail",)}
    verdict = _g.judge_holding(_held_views("ACME", _all_known("ACME"), record))
    assert not verdict.sell_allowed, verdict.detail


def test_t3_unclassified_refusal_is_unknown_and_never_sells():
    record = {"ACME": ("R6 constructor refused [brand_new_code] — detail",)}
    verdict = _g.judge_holding(_held_views("ACME", _all_known("ACME"), record))
    assert not verdict.sell_allowed
    assert verdict.basis == _g.BASIS_EVIDENCE_MISSING


def _refusal_constants():
    root = Path(__file__).resolve().parents[1]
    pattern = _re.compile(
        r"^(STOP_REFUSAL_\w+|TRIM_REFUSAL_\w+|CONSTRUCTOR_(?!REFUSED_EVENT_REASON)\w+|REFUSAL_\w+|FAULT_\w+)"
        r"\s*(?::[^=]+)?=\s*\"([a-z_]+)\"",
        _re.M,
    )
    files = (
        "src/portfolio_constructor/config.py",
        "src/data/levels.py",
        "src/portfolio_constructor/entry_stop/resolver.py",
    )
    found = {}
    for rel in files:
        for name, value in pattern.findall((root / rel).read_text()):
            found[f"{rel}:{name}"] = value
    return found


def test_t5_every_refusal_code_is_classified_exactly_once():
    constants = _refusal_constants()
    assert len(constants) >= 40, constants  # the enumeration itself works
    sets = (_g.COUNTS_CODES, _g.NEVER_CODES, _g.UNKNOWN_CODES)
    for where, code in constants.items():
        hits = sum(code in s for s in sets)
        assert hits == 1, f"{where}={code!r} is in {hits} classification sets"


def test_t5_empty_stop_backstop_is_its_own_unknown_code():
    from src.portfolio_constructor.entry_stop.resolver import STOP_REFUSAL_EMPTIED_UNRECORDED

    assert _g.classify_refusal(STOP_REFUSAL_EMPTIED_UNRECORDED) == _g.CLASS_UNKNOWN
    assert _g.classify_refusal("stop_on_wrong_side_of_entry") == _g.CLASS_UNKNOWN
    assert _g.classify_refusal("no_valid_stop_against_entry") == _g.CLASS_COUNTS


def test_t6_stale_earnings_stance_is_dropped_before_any_rule_reads_it():
    verdicts = (*_all_known("ACME")[:2], _v("earnings", "ACME", "bearish"), *_all_known("ACME")[3:])
    fresh = _g.judge_holding(_held_views("ACME", verdicts, {"ACME": ()}))
    assert fresh.sell_allowed  # a fresh bearish earnings seat opposes
    stale = _g.judge_holding(
        _held_views("ACME", verdicts, {"ACME": ()}, evidence_stale_sources={"ACME": frozenset({"earnings"})})
    )
    assert not stale.sell_allowed


def test_t8_ok_but_empty_feed_is_unknown_and_never_sells():
    # News status "ok", but no seat other than technical said anything on ACME.
    record = {"ACME": ()}
    verdict = _g.judge_holding(_held_views("ACME", (_v("technical", "ACME", "bullish"),), record))
    assert not verdict.sell_allowed
    assert verdict.basis == _g.BASIS_EVIDENCE_MISSING


def test_r5_counts_unless_a_seat_feeding_the_sum_has_no_reading():
    """The evidence sum is unknown only when a seat feeding it has no reading
    for the name this run (`failed_evidence_seats`, the one rule the half-hour
    check uses; owner-ruling test `test_net_evidence_below_bar_still_sells_
    when_every_seat_read`). A seat that ran and holds no view on the name adds
    nothing to the sum, which is a reading."""
    record = {"ACME": ("R5 net evidence +0 if long — no rung",)}
    partial = (_v("technical", "ACME", "bullish"), _v("news", "ACME", "bullish"))
    outage = _held_views("ACME", partial, record, data_status={"tech": "ok", "news": "ok", "macro": "failed"})
    assert not _g.judge_holding(outage).sell_allowed, "a seat outage never sells"
    read = _held_views("ACME", partial, record, data_status={"tech": "ok", "news": "ok", "macro": "ok"})
    assert _g.judge_holding(read).sell_allowed
    assert _g.judge_holding(_held_views("ACME", _all_known("ACME"), record)).sell_allowed


def test_r3_not_on_the_buy_list_never_sells():
    record = {"ACME": ("R3 not BUY-eligible",)}
    assert not _g.judge_holding(_held_views("ACME", _all_known("ACME"), record)).sell_allowed


def test_t11_short_with_buy_rating_fails_r2_in_the_holding_direction():
    positions = [SimpleNamespace(symbol="SHRT", qty=-5)]
    record = _g.build_held_bar_record(positions, [SimpleNamespace(symbol="SHRT", rating="buy")], {"SHRT": "[]"})
    pipeline, ctx = _pipeline_ctx(_all_known("SHRT"))
    ctx.held_bar_record = record
    verdict = _g.judge_holding(_g.views_from_pipeline(pipeline, ctx, "SHRT", is_short=True))
    assert verdict.sell_allowed
    assert "(short)" in verdict.detail


@_pytest.mark.parametrize("basis", ["structural_level_broken", "thesis_invalid_if_triggered"])
def test_t13_protective_sale_is_never_gated(basis):
    views = _g.HoldingViews(
        symbol="ACME", is_short=False, seat_verdicts=_all_known("ACME"), eligibility_reasons=(), protection_basis=basis
    )
    verdict = _g.judge_holding(views)
    assert verdict.sell_allowed and verdict.basis == _g.BASIS_PROTECTIVE


def test_t14_failed_bar_fetch_is_not_protective():
    views = _g.HoldingViews(
        symbol="ACME",
        is_short=False,
        seat_verdicts=_all_known("ACME"),
        eligibility_reasons=(),
        protection_basis="chart_unreadable",
    )
    assert not _g.judge_holding(views).sell_allowed


def test_t15_a_gate_decided_sale_is_not_dropped_by_a_second_hold_check():
    from src.exits_parts.midday_state import MiddayLoop

    loop = MiddayLoop(
        owner=None,
        positions=[],
        run_id="r",
        metric_deltas=None,
        risk_vetoed_symbols=None,
        already_trimmed=set(),
        acted_today=[],
        same_rules_allowed=frozenset({"ACME"}),
    )
    assert loop.gate_decided("acme", "SELL")
    assert not loop.gate_decided("ACME", "TRAIL_STOP")
    assert not loop.gate_decided("OTHER", "SELL")


# ---------------------------------------------------------------------------
# Change 1 completion (14 Oct): carried refusals, the one view builder's
# freshness rule and held-name macro view, macro in the gate, failed fetch.
# ---------------------------------------------------------------------------


def test_t4_a_refusal_carried_from_an_earlier_run_is_not_this_runs_record():
    constructor = SimpleNamespace(last_refusals={"ACME": {"refusal": "agreement_net_at_or_below_zero"}})
    carried = _g.set_aside_carried_refusals(constructor)
    assert constructor.last_refusals == {}
    constructor.last_refusals["NEW"] = {"refusal": "risk_budget_exhausted"}  # this run's preview
    this_run = dict(constructor.last_refusals)
    _g.restore_carried_refusals(constructor, carried)
    assert "ACME" not in this_run
    # Restored, so the durable filing still happens exactly once.
    assert constructor.last_refusals == {
        "ACME": {"refusal": "agreement_net_at_or_below_zero"},
        "NEW": {"refusal": "risk_budget_exhausted"},
    }
    src = (ROOT / "src" / "stage_decision.py").read_text()
    aside, snapshot, restore = (
        src.index("set_aside_carried_refusals(_constructor)"),
        src.index("constructor_refusals_by_symbol = {"),
        src.index("restore_carried_refusals(_constructor, _carried_refusals)"),
    )
    assert aside < snapshot < restore


def _earnings_item(filing_date):
    from tests.test_analyst_verdict import _earnings

    raw = _earnings(sentiment="bearish").model_dump(mode="json")
    raw["filing_date"] = filing_date
    return {"symbol": "AAPL", "filing_date": filing_date, "analysis": raw}


def test_t6_buy_side_view_builder_drops_a_stale_earnings_stance():
    from src.agents.portfolio_manager import PortfolioManagerAgent
    from src.agents.portfolio_manager.evidence_prompting import earnings_filing_is_stale
    from src.trading_calendar import et_today

    def build(item):
        return PortfolioManagerAgent._collect_seat_verdicts(
            analyses=[], news_intel=None, macro_analysis=None, earnings_analyses=[item], smart_money_findings=None
        )

    assert [v.seat for v in build(_earnings_item(et_today().isoformat()))] == ["earnings"]
    assert [v.seat for v in build(_earnings_item("2026-01-02"))] == []
    assert earnings_filing_is_stale("") and earnings_filing_is_stale(None)


def test_t9_macro_view_reaches_a_holding_the_scan_did_not_read():
    from src.agents.portfolio_manager import PortfolioManagerAgent
    from tests.test_macro_double_count import Position, _energy_row, _macro_analysis

    macro = _macro_analysis(equity_outlook="bullish", sector_guidance=[_energy_row()])
    positions = [
        Position(
            symbol="XOM",
            qty=10,
            avg_entry=100.0,
            current_price=104.0,
            market_value=1040.0,
            unrealized_pnl=40.0,
            sector="Energy",
        )
    ]
    verdicts = PortfolioManagerAgent._collect_seat_verdicts(
        analyses=[],
        news_intel=None,
        macro_analysis=macro.model_dump(),
        earnings_analyses=[],
        smart_money_findings=None,
        positions=positions,
    )
    assert [v.symbol for v in verdicts if v.seat == "macro"] == ["XOM"]


def _sector_macro(symbol, direction):
    return SimpleNamespace(
        seat="macro",
        symbol=symbol,
        direction=direction,
        conviction="medium",
        invalidation="the sector call reverses",
        evidence=[SimpleNamespace(label="sector_stance:Energy", text="a checkable observed fact")],
    )


def test_t10_sector_macro_opposition_sells_only_when_every_name_seat_is_known():
    names = (
        _v("technical", "ACME", "bullish"),
        _v("news", "ACME", "neutral"),
        _v("earnings", "ACME", "neutral"),
        _v("smart_money", "ACME", "neutral"),
    )
    known = _g.judge_holding(_held_views("ACME", (*names, _sector_macro("ACME", "bearish")), {"ACME": ()}))
    assert known.sell_allowed and "macro opposed" in known.detail
    missing = _g.judge_holding(_held_views("ACME", (*names[:3], _sector_macro("ACME", "bearish")), {"ACME": ()}))
    assert not missing.sell_allowed


def test_t14_failed_bar_fetch_reads_chart_unreadable_not_no_chart_level():
    from unittest.mock import MagicMock

    from src.exits.structural_protection import StructuralProtection

    market = MagicMock()
    market.get_ohlcv.side_effect = RuntimeError("feed down")
    db = MagicMock()
    db.get_recent_holding_protection_breaks.return_value = []
    reader = StructuralProtection(
        voice_structural_protection_break=MagicMock(),
        config=SimpleNamespace(trading=SimpleNamespace(lookback_days=250)),
        db=db,
        market=market,
        risk_engine=None,
    )
    check = reader._structural_protection_for_holding(
        symbol="ACME", thesis_invalid_if=None, entry_price=100.0, stop_loss=90.0, is_short=False, run_id="r"
    )
    assert check.protected is False and check.basis == "chart_unreadable"
    assert not _g.is_protective(check.basis)
    assert not _g.is_protective("no_chart_level")


# (t16) SAFETY, coordinator 14 Oct defect 1: the reviewer's gate reads
# protection the way the morning path does, so an UNPROTECTED holding's sale
# is never blocked for missing evidence; a failed bar fetch is not protective.
def test_t16_reviewer_never_blocks_an_unprotected_holdings_sale():
    from src.sessions.position_review_session import PositionReviewSession

    calls = []

    def _read(**kw):
        calls.append(kw)
        return SimpleNamespace(protected=False, basis="structural_level_broken", detail="closed below the level twice")

    def _run(read, history):
        db = _DB()
        fake = SimpleNamespace(
            _db=db,
            _structural_protection_for_holding=read,
            _build_position_history=history,
            _todays_technical_reads=lambda positions: [],
        )
        return PositionReviewSession._gate_reviewer_sales(
            fake,
            _review("AAPL", "SELL", "the level broke"),
            [SimpleNamespace(symbol="AAPL", qty=10)],
            run_id="r1",
            news_intel=None,
            news_ok=False,  # evidence missing: the bar cannot be read
            macro_analysis=None,
            earnings_analyses=[],
        )

    gated = _run(_read, lambda positions: {"AAPL": {"stop_loss": 90.0, "entry_price": 100.0}})
    assert gated.actions[0].action == "SELL"
    assert calls and calls[0]["symbol"] == "AAPL" and calls[0]["stop_loss"] == 90.0

    unreadable = _run(
        lambda **kw: SimpleNamespace(protected=False, basis="chart_unreadable", detail="no bars"),
        lambda positions: {},
    )
    assert unreadable.actions[0].action == "HOLD"


# ---------------------------------------------------------------------------
# Owner ruling Option A (14 Oct 2026): a HELD name is kept while it passes
# every buy rule EXCEPT timing; it is sold by judgement only when its chart
# turns broken or against the holding. A neutral chart / rating never sells.
# ---------------------------------------------------------------------------


def _held_verdict(symbol, rating, *, qty, seats="bullish", protection_basis=None, blocked=None):
    """Gate verdict for a holding, through this run's record as built."""
    tech = {"neutral": "neutral", "sell": "bearish", "buy": "bullish"}[rating]
    verdicts = [_v("technical", symbol, tech)] + [
        _v(seat, symbol, seats) for seat in ("news", "earnings", "smart_money", "macro")
    ]
    positions = [SimpleNamespace(symbol=symbol, qty=qty)]
    analyses = [SimpleNamespace(symbol=symbol, rating=rating)]
    record = _g.build_held_bar_record(positions, analyses, blocked if blocked is not None else {symbol: "[]"})
    pipeline, ctx = _pipeline_ctx(verdicts)
    ctx.held_bar_record = record
    views = _g.views_from_pipeline(pipeline, ctx, symbol, is_short=qty < 0, protection_basis=protection_basis)
    return _g.judge_holding(views)


def test_option_a_held_long_neutral_rating_intact_structure_no_timing_is_kept():
    # The manager stops at R2 for a neutral rating ("R2 neutral rating"): kept.
    unread = _held_verdict("ACME", "neutral", qty=10, blocked={"ACME": "['R2 neutral rating']"})
    assert not unread.sell_allowed, unread.detail
    # The manager read R3-R6 in the holding's direction and they pass: kept.
    from src.agents.portfolio_manager.candidate_ranking import HELD_NEUTRAL_R2

    read = _held_verdict("ACME", "neutral", qty=10, blocked={"ACME": str([HELD_NEUTRAL_R2])})
    assert not read.sell_allowed and read.basis == BASIS_PASSES_BAR, read.detail


def test_option_a_held_long_sell_rating_is_sold():
    verdict = _held_verdict("ACME", "sell", qty=10)
    assert verdict.sell_allowed and verdict.basis == BASIS_FAILS_BAR
    assert "R2 rating sell does not point the holding's way (long)" in verdict.detail


def test_option_a_held_long_broken_structure_is_sold():
    verdict = _held_verdict("ACME", "neutral", qty=10, protection_basis="structural_level_broken")
    assert verdict.sell_allowed and verdict.basis == _g.BASIS_PROTECTIVE


def test_option_a_held_short_buy_rating_is_covered_and_neutral_is_kept():
    covered = _held_verdict("SHRT", "buy", qty=-5, seats="bearish")
    assert covered.sell_allowed and "(short)" in covered.detail
    kept = _held_verdict("SHRT", "neutral", qty=-5, seats="bearish", blocked={"SHRT": "['R2 neutral rating']"})
    assert not kept.sell_allowed, kept.detail


def test_option_a_held_neutral_still_fails_the_other_buy_rules():
    # Every buy rule except timing still applies: a name-level seat opposed sells.
    verdicts = [_v("technical", "ACME", "neutral"), _v("news", "ACME", "bearish")]
    verdicts += [_v(s, "ACME", "bullish") for s in ("earnings", "smart_money", "macro")]
    views = _g.HoldingViews(symbol="ACME", is_short=False, seat_verdicts=tuple(verdicts), eligibility_reasons=())
    assert _g.judge_holding(views).sell_allowed
    # ...and R5, read in the holding's direction with every seat known, sells.
    from src.agents.portfolio_manager.candidate_ranking import HELD_NEUTRAL_R2

    blocked = {"ACME": str([HELD_NEUTRAL_R2, "R5 net evidence -1 if long — no rung"])}
    assert _held_verdict("ACME", "neutral", qty=10, blocked=blocked).sell_allowed


def test_option_a_new_buy_still_needs_timing_confirmed():
    from src.agents.portfolio_manager import PortfolioManagerAgent
    from src.risk.conviction_bar import own_bar_block_reason

    analyses = [SimpleNamespace(symbol="ACME", rating="neutral"), SimpleNamespace(symbol="HELD", rating="neutral")]
    eligibility = PortfolioManagerAgent.candidate_eligibility(
        analyses=analyses,
        evidence_registry={},
        allowed_buy_symbols={"ACME", "HELD"},
        active_state_changes="",
        held_directions={"HELD": "long"},
    )
    assert eligibility["ACME"] == ["R2 neutral rating"]  # not held: a new buy, refused on R2
    assert eligibility["HELD"][0].startswith("R2 neutral rating")  # adding to a holding is a buy: refused too
    assert "R5 net evidence +0 if long — no rung" in eligibility["HELD"]  # read in the holding's direction
    neutral_chart = [_v("technical", "ACME", "neutral"), _v("news", "ACME", "bullish")]
    assert "does not confirm timing" in own_bar_block_reason(neutral_chart, direction="bullish")


# ---------------------------------------------------------------------------
# No paid manager answer (owner 12 Oct 2026): the held-direction record comes
# from the deterministic prompt assembly, and the sale goes THROUGH the gate.
# ---------------------------------------------------------------------------


def _no_manager_run(*, pm_skip_reason=None):
    """The real DecisionStage with a PM that gives no answer (or is skipped).
    Its assembly (`build_user_message`) leaves what a real one would for two
    holdings: OLD, whose chart read turned AGAINST it (R7 opposition), and
    KEEP, rated NEUTRAL with its chart level intact and every other seat
    backing it (`HELD_NEUTRAL_R2`, R3-R6 passed in the holding's direction)."""
    from unittest.mock import MagicMock, patch

    from src.agents.portfolio_manager import PortfolioManagerAgent
    from src.agents.portfolio_manager.candidate_ranking import HELD_NEUTRAL_R2
    from src.risk.conviction_bar import own_bar_opposition_reason
    from src.rotation import RotationOpportunity, RotationPrecheck
    from tests import test_earn_its_place_without_pm as free

    opposed = (own_bar_opposition_reason([_v("technical", "OLD", "bearish")], direction="bullish"),)
    precheck = RotationPrecheck(
        opportunity=RotationOpportunity(
            new_symbol=None,
            new_score=None,
            held_symbol="OLD",
            held_score=None,
            tier="ineligible_hold",
            reasons=opposed,
            ineligible_candidates=(("OLD", opposed), ("KEEP", (HELD_NEUTRAL_R2,))),
        ),
        headroom_pct=5.0,
        ceiling_pct=25.0,
        floor_pct=0.5,
        held_below_entry_bar=("KEEP", "OLD"),
        held_examined=("KEEP", "OLD"),
    )
    pipeline = free._pipeline(precheck)
    pm = pipeline.portfolio_manager
    assemble = pm.build_user_message.side_effect

    def _assemble(**kwargs):
        pm.last_blocked = {"OLD": str(list(opposed)), "KEEP": str([HELD_NEUTRAL_R2])}
        return assemble(**kwargs)

    pm.build_user_message.side_effect = _assemble
    verdicts = [_v("technical", "OLD", "bearish"), *_all_known("KEEP", tech="neutral")]
    seen = {}
    real_judge = _g.judge_holding

    def _judge(views):
        verdict = real_judge(views)
        seen[verdict.symbol] = verdict
        return verdict

    with (
        patch.object(PortfolioManagerAgent, "_collect_seat_verdicts", MagicMock(return_value=verdicts)),
        patch("src.exits.same_rules_gate.judge_holding", _judge),
    ):
        ctx, _ = free._run_recorded(
            pipeline, {"tech": "ok", "news": "ok", "macro": "ok"}, pm_skip_reason=pm_skip_reason
        )
    return pipeline, ctx, seen


def test_no_manager_answer_still_sells_a_below_bar_holding_through_the_gate():
    pipeline, ctx, seen = _no_manager_run()
    assert pipeline.portfolio_manager.decide.call_count == 1  # it answered nothing
    assert ctx.held_bar_record is not None, "the record does not wait on the paid answer"
    assert seen["OLD"].basis == BASIS_FAILS_BAR and seen["OLD"].sell_allowed
    assert ctx.rotation is not None and ctx.rotation["held_symbol"] == "OLD"


def test_no_manager_answer_keeps_a_neutral_rated_structure_intact_holding():
    _, ctx, seen = _no_manager_run()
    assert ctx.held_bar_record["KEEP"] == (), "R3-R6 passed in the holding's direction"
    assert seen["KEEP"].basis == BASIS_PASSES_BAR and not seen["KEEP"].sell_allowed
    assert ctx.rotation["held_symbol"] != "KEEP"


def test_a_skipped_manager_judges_the_same_way_with_no_paid_call():
    pipeline, ctx, seen = _no_manager_run(pm_skip_reason="intraday_no_movers")
    assert pipeline.portfolio_manager.decide.call_count == 0
    assert seen["OLD"].sell_allowed and not seen["KEEP"].sell_allowed
    assert ctx.rotation["held_symbol"] == "OLD"
