"""Parked numbers settled 2026-10-10: the blocked-names display cap is gone."""

from tests.test_blocked_proposals import _pipeline, _target


def test_display_lists_every_repeat_blocked_name(tmp_path):
    pipeline, db = _pipeline(tmp_path)
    syms = ("AAA", "BBB", "CCC", "DDD", "EEE", "FFF", "GGG")
    for sym in syms:
        for i, day in enumerate((5, 4, 3)):
            _target(db, f"r-{sym}-{i}", f"d-{sym}-{i}", sym, days_ago=day)

    out = pipeline._build_blocked_proposals()
    lines = [ln for ln in out.split("\n") if ln.startswith("- ")]
    assert len(lines) == len(syms)
    assert all(any(ln.startswith(f"- {s}:") for ln in lines) for s in syms)
