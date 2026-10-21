"""A named-but-not-cut holding must still appear in the pruning outcome."""

from src.rotation import pruning_pass_lines

_BASE = {
    "outcome": "ok",
    "held_examined": "BRZE,CLF,COST,WMT",
    "held_examined_count": 4,
    "held_below_entry_bar": "",
}


def _kept_line(rec):
    return next(x for x in pruning_pass_lines(rec) if "Considered and kept" in x)


def test_named_but_not_cut_holding_is_kept():
    rec = {**_BASE, "held_symbol": "COST", "tier": "ranked_margin"}
    lines = pruning_pass_lines(rec)
    assert not any("Put up to be cut" in x for x in lines)
    kept = _kept_line(rec)
    for sym in ("BRZE", "CLF", "COST", "WMT"):
        assert sym in kept


def test_cut_holding_is_excluded_from_kept():
    rec = {**_BASE, "held_symbol": "COST", "tier": "ineligible_hold"}
    lines = pruning_pass_lines(rec)
    assert any("Put up to be cut: COST" in x for x in lines)
    kept = _kept_line(rec)
    assert "COST" not in kept
    assert all(s in kept for s in ("BRZE", "CLF", "WMT"))
