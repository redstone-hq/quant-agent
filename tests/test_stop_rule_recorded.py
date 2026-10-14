"""`stop_basis` was blank on every trade: record the rule that set the stop.

`shipped_stop_rule` returned a name only for a stop on a computed structural
level and None for every other stop, so every ATR-band stop (all of them so far)
recorded None. It now names the rule, with the rule names the stop code already
uses, and an explicit "not determined" value instead of a silent None.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import src.entry_record as entry_record
from src.entry_record import STOP_BASIS_UNKNOWN, insert_pending_entry
from src.portfolio_constructor.config import (
    STOP_RULE_ATR_BAND,
    STOP_RULE_LEVEL_HONOURED,
    STOP_RULE_OUTSIDE_BAND,
    STOP_RULE_PRIOR_BAR_NO_ATR,
    STOP_RULE_SIGNAL_BAR,
    ConstructorConfig,
)
from src.portfolio_constructor.stops import STOP_RULE_UNKNOWN, StopRules


def test_the_two_unknown_values_are_the_same_string():
    assert STOP_RULE_UNKNOWN == STOP_BASIS_UNKNOWN


@pytest.fixture
def rules():
    cfg = ConstructorConfig()
    return StopRules(read_cfg=lambda: cfg, entry_stop_resolver=lambda: None)


def _analysis(atr=2.0, **extra):
    base = dict(atr_14=atr, signal_bar_low=None, signal_bar_high=None, computed_levels=[], computed_level_touches={})
    base.update(extra)
    return SimpleNamespace(**base)


# entry 100, ATR 2, band 2.5 x ATR = 5: the long band edge is 95.0, the short's is 105.0


def test_atr_band_stop_records_the_band_rule(rules):
    # typed 98 sat inside the band and nothing computed backs it, so it shipped at the band edge 95.0
    assert rules.shipped_stop_rule(_analysis(), 100.0, 95.0, "long") == STOP_RULE_ATR_BAND
    assert rules.shipped_stop_rule(_analysis(), 100.0, 105.0, "short") == STOP_RULE_ATR_BAND


def test_stop_already_outside_the_band_records_that(rules):
    assert rules.shipped_stop_rule(_analysis(), 100.0, 90.0, "long") == STOP_RULE_OUTSIDE_BAND
    assert rules.shipped_stop_rule(_analysis(), 100.0, 110.0, "short") == STOP_RULE_OUTSIDE_BAND


def test_signal_bar_stop_records_the_signal_bar_rule(rules):
    # the signal bar low 93 is past the band edge 95, so the bar decides
    assert rules.shipped_stop_rule(_analysis(signal_bar_low=93.0), 100.0, 93.0, "long") == STOP_RULE_SIGNAL_BAR


def test_level_backed_stop_keeps_its_level_rule(rules, monkeypatch):
    monkeypatch.setattr(rules, "_level_backing_stop", lambda *a, **k: 97.0)
    assert rules.shipped_stop_rule(_analysis(), 100.0, 97.0, "long") == STOP_RULE_LEVEL_HONOURED


def test_no_atr_stop_read_from_the_prior_bar_records_that(rules):
    buffer_pct = rules.cfg.structural_stop_buffer_pct
    stop = round(96.0 * (1.0 - buffer_pct), 2)
    analysis = _analysis(atr=None, signal_bar_low=96.0)
    assert rules.shipped_stop_rule(analysis, 100.0, stop, "long") == STOP_RULE_PRIOR_BAR_NO_ATR


@pytest.mark.parametrize("stop", [98.0, float("nan"), None])
def test_a_stop_no_rule_explains_is_explicitly_unknown_never_none(rules, stop):
    assert rules.shipped_stop_rule(_analysis(), 100.0, stop, "long") == STOP_BASIS_UNKNOWN


def _decision(**overrides):
    fields = dict(
        symbol="ABCD",
        action="BUY",
        reasoning="r",
        take_profit=None,
        setup_type="breakout",
        structural_ceiling=True,
        stop_level_basis=None,
        conviction="HIGH",
        requested_risk_pct=0.5,
        allocated_risk_pct=0.4,
        thesis_invalid_if="x",
    )
    fields.update(overrides)
    return SimpleNamespace(**fields)


def test_entry_row_stores_the_rule_and_never_a_blank(monkeypatch):
    monkeypatch.setattr(entry_record, "resolve_entry_pins", lambda *a, **k: (None, None, None))
    stored = []
    for rule in (STOP_RULE_ATR_BAND, None):
        db = MagicMock()
        insert_pending_entry(
            db=db,
            decision=_decision(stop_rule=rule),
            add_prep=None,
            is_short=False,
            qty=3,
            executed_price=10.0,
            run_id="run",
            stop_price=9.0,
            decision_id=7,
            entry_analysis=SimpleNamespace(atr_14=0.3, expected_horizon_sessions=5),
            decision_model="model",
        )
        stored.append(
            [c.kwargs["stop_basis"] for c in db.method_calls if "stop_basis" in c.kwargs][0],
        )
    assert stored == [STOP_RULE_ATR_BAND, STOP_BASIS_UNKNOWN]
