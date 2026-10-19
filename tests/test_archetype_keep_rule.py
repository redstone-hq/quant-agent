"""Desk archetype step B — KEEP RULE (owner ruling 19 Oct 2026, docs/OUTCOME.md
"The desk archetype").

A holding is kept while the chart is not against it AND every opposing
name-level seat has a written answer on record. A catalyst is required at
entry only, so quiet news never sells. An unanswered objection is the
position reviewer's call (answer or sell) — never a mechanical sale. Macro
never sells. A confirmed chart break still sells ungated.

Every test here fails on the pre-archetype trunk.
"""

from types import SimpleNamespace

from src.exits import same_rules_gate as g
from tests.test_conviction_bar import _macro, _v

_OVERRULE = {"seat": "news", "objection_direction": "bearish", "decision": "overrule"}


def _views(verdicts, **kw):
    return g.HoldingViews("ACME", False, tuple(verdicts), eligibility_reasons=(), **kw)


def test_quiet_news_keeps_the_holding():
    verdict = g.judge_holding(_views([_v("technical", "ACME")]))
    assert not verdict.sell_allowed and verdict.basis == g.BASIS_PASSES_BAR


def test_chart_against_sells_even_with_an_answer_on_record():
    verdicts = [_v("technical", "ACME", direction="bearish"), _v("news", "ACME")]
    verdict = g.judge_holding(_views(verdicts, objection_answers=(_OVERRULE,)))
    assert verdict.sell_allowed and verdict.basis == g.BASIS_FAILS_BAR


def test_unanswered_objection_is_never_a_mechanical_sale_but_the_reviewer_may_sell():
    verdicts = [_v("technical", "ACME"), _v("news", "ACME", direction="bearish")]
    mechanical = g.judge_holding(_views(verdicts))
    assert not mechanical.sell_allowed and mechanical.basis == g.BASIS_OBJECTION_UNANSWERED
    reviewer = g.judge_holding(_views(verdicts), reviewer=True)
    assert reviewer.sell_allowed and reviewer.basis == g.BASIS_OBJECTION_UNANSWERED


def test_overrule_on_record_keeps_it_from_the_reviewer_too():
    verdicts = [_v("technical", "ACME"), _v("news", "ACME", direction="bearish")]
    verdict = g.judge_holding(_views(verdicts, objection_answers=(_OVERRULE,)), reviewer=True)
    assert not verdict.sell_allowed and verdict.basis == g.BASIS_PASSES_BAR


def test_macro_against_never_sells():
    verdicts = [_v("technical", "ACME"), _macro("ACME", sector_specific=True)]
    verdict = g.judge_holding(_views(verdicts), reviewer=True)
    assert not verdict.sell_allowed and verdict.basis == g.BASIS_PASSES_BAR


def test_retired_net_evidence_record_never_sells():
    views = g.HoldingViews(
        "ACME", False, (_v("technical", "ACME"),), eligibility_reasons=("R5 net evidence +0 if long — no rung",)
    )
    assert g.judge_holding(views, reviewer=True).basis == g.BASIS_PASSES_BAR


def test_confirmed_chart_break_still_sells_ungated_over_an_answer():
    verdicts = [_v("technical", "ACME"), _v("news", "ACME")]
    views = _views(verdicts, objection_answers=(_OVERRULE,), protection_basis="structural_level_broken")
    verdict = g.judge_holding(views)
    assert verdict.sell_allowed and verdict.basis == g.BASIS_PROTECTIVE


def test_answers_are_read_from_the_record(monkeypatch):

    seen = []

    def fake(db, symbol, seat, run_id=None):
        seen.append(seat)
        return _OVERRULE if seat == "news" else None

    monkeypatch.setattr(g, "objection_answer_for", fake)
    store = SimpleNamespace(get_latest_symbol_evidence=lambda kind, symbols: {})
    assert g.objection_answers_from_db(store, "ACME") == (_OVERRULE,)
    assert g.objection_answers_from_db(None, "ACME") == ()  # no evidence store: nothing on record
    assert sorted(seen) == ["earnings", "news", "smart_money"]
