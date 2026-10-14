"""The no-ATR structural stop recording: rows land, and nothing moves."""

from __future__ import annotations

import logging

import pytest

from src.portfolio_constructor import no_atr_stop_record as rec
from src.portfolio_constructor.config import ConstructorConfig
from src.portfolio_constructor.stops import StopRules


class _Analysis:
    def __init__(self, **kw):
        self.symbol = kw.get("symbol", "TEST")
        self.computed_levels = kw.get("computed_levels", [])
        self.computed_level_touches = kw.get("computed_level_touches", {})
        self.computed_level_zones = kw.get("computed_level_zones", {})
        self.signal_bar_low = kw.get("signal_bar_low")
        self.signal_bar_high = kw.get("signal_bar_high")


def _rows(caplog):
    return rec.rows_from(r.getMessage() for r in caplog.records)


def _rules():
    cfg = ConstructorConfig()
    return StopRules(read_cfg=lambda: cfg, entry_stop_resolver=lambda: None), cfg


def test_zone_halfwidth_is_measured_or_honestly_absent():
    a = _Analysis(computed_level_zones={100.0: [99.0, 101.0]})
    assert rec.zone_halfwidth(a, 100.0) == (1.0, True)
    assert rec.zone_halfwidth(a, 55.0) == (None, False)
    assert rec.zone_halfwidth(_Analysis(), 100.0) == (None, False)


def test_tier_one_placement_is_unchanged_and_counted(caplog):
    rules, cfg = _rules()
    a = _Analysis(
        computed_levels=[90.0],
        computed_level_touches={90.0: 2},
        computed_level_zones={90.0: [89.5, 90.5]},
    )
    with caplog.at_level(logging.INFO):
        out = rules._derive_structural_stop_no_atr(a, 100.0, False)
    assert out is not None
    level, stop, _rule = out
    assert level == 90.0
    # The shipping stop is exactly the buffer past the level: untouched.
    assert stop == pytest.approx(90.0 * (1.0 - cfg.structural_stop_buffer_pct))
    rows = _rows(caplog)
    assert len(rows) == 1
    assert rows[0]["outcome"] == rec.OUTCOME_LEVEL
    assert rows[0]["level_zone_halfwidth_measured"] is True
    assert rows[0]["level_zone_halfwidth"] == pytest.approx(0.5)
    assert rows[0]["candidate_levels"] == 1
    # Touches are recorded as data; no minimum exists since the touch bar
    # was deleted, so the row carries no minimum-touches field at all.
    assert rows[0]["level_touches"] == 2
    assert "min_touches_required" not in rows[0]


def test_tier_two_placement_is_unchanged_and_counted(caplog):
    rules, cfg = _rules()
    a = _Analysis(signal_bar_low=95.0)
    with caplog.at_level(logging.INFO):
        out = rules._derive_structural_stop_no_atr(a, 100.0, False)
    assert out is not None
    assert out[0] == 95.0
    assert out[1] == pytest.approx(95.0 * (1.0 - cfg.structural_stop_buffer_pct))
    rows = _rows(caplog)
    assert [r["outcome"] for r in rows] == [rec.OUTCOME_PRIOR_BAR]
    # No measured span for a bar edge, and the row says so rather than guessing.
    assert rows[0]["level_zone_halfwidth_measured"] is False


def test_ran_and_found_nothing_still_writes_a_row(caplog):
    rules, _cfg = _rules()
    with caplog.at_level(logging.INFO):
        out = rules._derive_structural_stop_no_atr(_Analysis(), 100.0, False)
    assert out is None
    rows = _rows(caplog)
    assert [r["outcome"] for r in rows] == [rec.OUTCOME_NONE]


def test_never_reached_writes_no_row(caplog):
    with caplog.at_level(logging.INFO):
        pass
    assert _rows(caplog) == []


def test_summarise_holds_no_state_and_invents_no_number():
    empty = rec.summarise([])
    assert empty["branch_entries"] == 0
    assert empty["placements"] == 0
    assert empty["narrowest_halfwidth_seen"] is None
    assert empty["mean_buffer_in_halfwidths"] is None
    rows = [
        {
            "outcome": rec.OUTCOME_LEVEL,
            "level_zone_halfwidth": 0.5,
            "level_zone_halfwidth_measured": True,
            "buffer_in_level_halfwidths": 0.9,
        },
        {"outcome": rec.OUTCOME_NONE, "level_zone_halfwidth": None, "level_zone_halfwidth_measured": False},
    ]
    out = rec.summarise(rows)
    assert out["branch_entries"] == 2
    assert out["placements"] == 1
    assert out["halfwidth_readings"] == 1
    assert out["mean_buffer_in_halfwidths"] == pytest.approx(0.9)
    # Calling twice must give the same answer: nothing accumulates.
    assert rec.summarise(rows) == out


def test_module_keeps_no_counters():
    for name, value in vars(rec).items():
        if name.startswith("_") or name in {"ROW_TAG"}:
            continue
        assert not isinstance(value, (int, float, list, dict, set)), name
