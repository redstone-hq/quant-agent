"""Entries are plain DAY market orders; the switch, the limit path and the rotation projection.

Owner ruling 2026-10-09 (paper): market orders on everything. What this file
pins, against src/stage_execution_parts/entry_order_pricing.py and the
rotation projection that mirrors it:

* the one switch defaults to market;
* the limit path is still reachable behind the one switch;
* the rotation projection still divides by the quote side and refuses the
  same way.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.stage_execution_parts import entry_order_pricing as pricing
from src.stage_execution_parts.entry_order_type import entry_order_type, entry_orders_are_market
from src.stage_execution_parts.state import EntryLeg, EntryRun

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


# The entry loop's market-order divisors (the arrival print, never the IEX
# ask / bid) are pinned in tests/test_entry_arrival_price.py (2026-10-19).


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
