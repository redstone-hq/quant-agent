"""Entries are plain DAY market orders, sized against the quote side they pay.

Owner ruling 2026-10-09 (paper): market orders on everything. What this file
pins, against src/stage_execution_parts/entry_order_pricing.py and the
rotation projection that mirrors it:

* a BUY carries no limit (the broker then sends a MarketOrderRequest, DAY);
* the RISK divisor is the live ASK for a BUY and the live BID for a SHORT;
* no usable quote side refuses the name with `no_price` — it never sizes
  off the print or the analyst entry;
* the share count the risk budget allows, priced at the ask, never carries
  more than the risk that budget granted the name;
* the limit path is still reachable behind the one switch;
* the rotation projection divides by the same quote side and refuses the
  same way.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.pipeline_sizing import _qty_by_risk_budget, _risk_budget_pct
from src.stage_execution_parts import entry_order_pricing as pricing
from src.stage_execution_parts.entry_order_type import entry_order_type, entry_orders_are_market
from src.stage_execution_parts.state import SKIP, EntryLeg, EntryRun

PRINT = 100.0  # the today print the allocation path sizes off
ENTRY = 100.0  # the analyst's entry
ASK = 100.40
BID = 99.60


def _pipeline(order_type=None):
    execution = SimpleNamespace(fractional_share_decimals=4)
    if order_type is not None:
        execution.entry_order_type = order_type
    return SimpleNamespace(
        broker=SimpleNamespace(),
        config=SimpleNamespace(execution=execution, risk=SimpleNamespace(max_position_risk_pct=2.0)),
    )


def _run(pipeline):
    return EntryRun(
        pipeline=pipeline,
        ctx=SimpleNamespace(),
        submit_queue=[],
        deferred_far_through=set(),
        original_entry_count=1,
        budget_is_gross=True,
        total_value=100_000.0,
    )


def _leg(is_short, ask, bid):
    action = "SHORT" if is_short else "BUY"
    decision = SimpleNamespace(symbol="XYZ", action=action, entry_price=ENTRY)
    return EntryLeg(decision, is_short, 0, PRINT, ask, bid)


@pytest.fixture
def refusals(monkeypatch):
    seen = []
    monkeypatch.setattr(
        pricing, "_record_execution_skip", lambda p, c, sym, why, detail: seen.append((sym, why, detail))
    )
    return seen


def test_the_switch_defaults_to_market_and_only_the_word_limit_turns_it_off():
    assert entry_order_type(_pipeline()) == "market"
    assert entry_orders_are_market(_pipeline("limit")) is False
    assert entry_orders_are_market(_pipeline("LIMIT ")) is False
    assert entry_orders_are_market(_pipeline("marketable")) is True
    assert entry_orders_are_market(SimpleNamespace()) is True


def test_a_buy_is_a_market_order_sized_against_the_ask(refusals):
    limit, sizing, risk = pricing.price_entry(_run(_pipeline()), _leg(False, ASK, BID), ENTRY, PRINT, PRINT)
    assert limit is None, "a market BUY carries no limit"
    assert risk == ASK, "the risk budget divides by the ask the BUY pays"
    assert sizing == ASK, "the allocation divisor rises to the ask (fewer shares), never falls"
    assert refusals == []


def test_a_short_is_a_market_order_whose_risk_divides_by_the_bid(refusals):
    limit, sizing, risk = pricing.price_entry(_run(_pipeline()), _leg(True, ASK, BID), ENTRY, PRINT, PRINT)
    assert limit is None
    assert risk == BID, "the risk budget divides by the bid the SHORT sells at"
    assert sizing == PRINT, "a short's allocation never divides by a below-market number"
    assert refusals == []


@pytest.mark.parametrize("ask", [None, 0.0, -1.0, float("nan"), float("inf"), True, "100.4"])
def test_a_buy_with_no_usable_ask_is_refused_never_sized_off_the_print(refusals, ask):
    out = pricing.price_entry(_run(_pipeline()), _leg(False, ask, BID), ENTRY, PRINT, PRINT)
    assert out is SKIP
    assert refusals == [("XYZ", "no_price", pricing.no_price_detail("BUY", False))]
    assert "ask" in refusals[0][2] and "never the print" in refusals[0][2]


def test_a_short_with_no_usable_bid_is_refused(refusals):
    out = pricing.price_entry(_run(_pipeline()), _leg(True, ASK, None), ENTRY, PRINT, PRINT)
    assert out is SKIP
    assert refusals[0][1] == "no_price" and "bid" in refusals[0][2]


def test_risk_at_the_ask_never_exceeds_the_risk_the_budget_granted():
    pipeline = _pipeline()
    total = 100_000.0
    stop = 97.0
    _, _, risk_price = pricing.price_entry(_run(pipeline), _leg(False, ASK, BID), ENTRY, PRINT, PRINT)
    qty = _qty_by_risk_budget(
        pipeline, total_value=total, sizing_price=risk_price, stop_price=stop, is_short=False, fractional=False
    )
    granted = total * _risk_budget_pct(pipeline) / 100
    assert qty > 0
    assert qty * (ASK - stop) <= granted, "risk at the ask overshoots what the budget granted"
    # Sized off the print instead, the same budget overshoots once filled at the ask:
    qty_off_print = _qty_by_risk_budget(
        pipeline, total_value=total, sizing_price=PRINT, stop_price=stop, is_short=False, fractional=False
    )
    assert qty_off_print * (ASK - stop) > granted


def test_the_limit_path_is_reachable_behind_the_switch(monkeypatch, refusals):
    monkeypatch.setattr(pricing, "entry_limit_from_quote", lambda run, leg, lp, sp: (100.40, 100.40))
    limit, sizing, risk = pricing.price_entry(_run(_pipeline("limit")), _leg(False, ASK, BID), ENTRY, PRINT, PRINT)
    assert limit == 100.40 and risk == 100.40 and sizing == 100.40


def _rotation_kwargs(monkeypatch, rot, *, quote, action="BUY"):
    seen = {}
    monkeypatch.setattr(rot, "_projected_post_sale_book", lambda *a, **k: ([], 100_000.0))
    monkeypatch.setattr(rot, "_live_fill_price", lambda p, s: PRINT)
    monkeypatch.setattr(rot, "sizing_price_or_refusal", lambda *a: (PRINT, None, None))
    monkeypatch.setattr(rot, "read_exit_quote", lambda broker, symbol: quote)
    monkeypatch.setattr(rot, "_fractional_sizing_allowed", lambda *a, **k: False)
    monkeypatch.setattr(rot, "_size_shares", lambda p, raw, fractional: float(int(raw)))

    def _risk(pipeline, *, total_value, sizing_price, stop_price, is_short, fractional):
        seen["risk_divisor"] = sizing_price
        return 200.0

    monkeypatch.setattr(rot, "_qty_by_risk_budget", _risk)
    monkeypatch.setattr(rot, "_projected_post_sale_cash", lambda *a, **k: 50_000.0)
    monkeypatch.setattr(rot, "_entry_deployment_budget", lambda *a: (50_000.0, True, "test"))
    monkeypatch.setattr(rot, "_single_name_execution_cap", lambda *a: 50_000.0)
    decision = SimpleNamespace(symbol="XYZ", action=action, entry_price=ENTRY, allocation_pct=20.0, stop_loss=92.0)
    return seen, dict(
        rotation={"held_symbol": "OLD"},
        buy_decision=decision,
        positions=[],
        total_value=100_000.0,
        rotation_sell=None,
    )


def test_the_rotation_projection_divides_risk_by_the_same_ask(monkeypatch):
    from src import pipeline_rotation_exec as rot

    seen, kwargs = _rotation_kwargs(monkeypatch, rot, quote={"bid": BID, "ask": ASK})
    clearance, why, detail = rot._rotation_buy_leg_projected_refusal(_pipeline(), SimpleNamespace(), **kwargs)
    assert why is None, (why, detail)
    assert seen["risk_divisor"] == ASK


def test_the_rotation_projection_divides_a_short_by_the_bid(monkeypatch):
    from src import pipeline_rotation_exec as rot

    seen, kwargs = _rotation_kwargs(monkeypatch, rot, quote={"bid": BID, "ask": ASK}, action="SHORT")
    rot._rotation_buy_leg_projected_refusal(_pipeline(), SimpleNamespace(), **kwargs)
    assert seen["risk_divisor"] == BID


def test_the_rotation_projection_refuses_without_a_quote_side(monkeypatch):
    from src import pipeline_rotation_exec as rot

    seen, kwargs = _rotation_kwargs(monkeypatch, rot, quote={"bid": BID, "ask": None})
    clearance, why, detail = rot._rotation_buy_leg_projected_refusal(_pipeline(), SimpleNamespace(), **kwargs)
    assert clearance is None and why == "no_price"
    assert "ask" in detail and "never the print" in detail
    assert "risk_divisor" not in seen
