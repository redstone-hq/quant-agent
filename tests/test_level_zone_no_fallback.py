"""No made-up 1% level width anywhere (2026-10-15, docs/NUMBER_TRIAGE.md).

A level's width is its own measured bar span; where a level has none, one
14-day ATR of the stock (owner ruling: derived, not a chosen multiplier);
with neither, nothing matches. The old `CLUSTER_TOLERANCE_PCT(_FALLBACK)`
1% band is deleted.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from src.data import levels as levels_module
from src.data.levels import _cluster, describe_stop_level_basis
from src.risk.target_revision_basis import level_backing_target

REPO = Path(__file__).resolve().parents[1]


def test_stop_basis_zone_is_the_levels_measured_zone():
    bars = {95.0: [(94.2, 96.1), (94.6, 95.8)]}
    record = describe_stop_level_basis(
        level_price=95.0,
        stop_loss=94.5,
        entry_price=100.0,
        computed_levels=[95.0],
        computed_level_bars=bars,
    )
    assert record["zone_low"] == pytest.approx(94.2)
    assert record["zone_high"] == pytest.approx(96.1)
    assert record["zone_width"] == pytest.approx(96.1 - 94.2)
    assert record["zone_source"] == "pivot_bars"
    assert record["stop_inside_zone"] is True


def test_stop_basis_without_measured_zone_invents_no_width():
    record = describe_stop_level_basis(level_price=95.0, stop_loss=94.5, entry_price=100.0)
    assert record["zone_low"] is None and record["zone_high"] is None
    assert record["zone_source"] is None
    assert record["stop_inside_zone"] is None


def test_target_outside_the_zone_but_within_one_percent_no_longer_matches():
    # Level 100, measured zone 99.8-100.3; target 100.9 is within 1% of the
    # level but outside its own zone.
    got = level_backing_target(
        stored_target=100.9,
        computed_levels=[100.0],
        computed_level_zones={100.0: (99.8, 100.3)},
        atr=5.0,
    )
    assert got is None
    inside = level_backing_target(
        stored_target=100.2,
        computed_levels=[100.0],
        computed_level_zones={100.0: (99.8, 100.3)},
    )
    assert inside == 100.0


def test_target_matching_without_zone_uses_one_atr_or_nothing():
    kw = {"stored_target": 100.9, "computed_levels": [100.0]}
    assert level_backing_target(**kw, atr=1.0) == 100.0
    assert level_backing_target(**kw, atr=0.5) is None
    assert level_backing_target(**kw) is None


def test_unmeasurable_pivot_uses_atr_adjacency_or_is_dropped():
    nan = float("nan")
    pivots = [(1, 100.0, "low", 99.5, 100.5), (2, 100.8, "low", nan, nan)]
    assert len(_cluster(pivots, 1.0)) == 1
    assert len(_cluster(pivots, 0.5)) == 2
    dropped = _cluster(pivots, None)
    assert [len(c) for c in dropped] == [1]


def test_no_cluster_tolerance_name_remains_in_code():
    assert not hasattr(levels_module, "CLUSTER_TOLERANCE_PCT")
    assert not hasattr(levels_module, "CLUSTER_TOLERANCE_PCT_FALLBACK")
    banned = {"CLUSTER_TOLERANCE_PCT", "CLUSTER_TOLERANCE_PCT_FALLBACK", "level_cluster_tolerance_pct"}
    hits = []
    for path in (REPO / "src").rglob("*.py"):
        tree = ast.parse(path.read_text(errors="replace"))
        for node in ast.walk(tree):
            name = (
                node.id
                if isinstance(node, ast.Name)
                else node.attr
                if isinstance(node, ast.Attribute)
                else node.arg
                if isinstance(node, (ast.arg, ast.keyword))
                else None
            )
            if isinstance(node, ast.ImportFrom):
                hits += [f"{path}:{a.name}" for a in node.names if a.name in banned]
            elif name in banned:
                hits.append(f"{path}:{name}")
    assert hits == []
