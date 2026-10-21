"""Each name's cut story is told once in the combined rotation + pruning message.

Live defect, 21 Oct 2026 1:18 pm ET: BRZE's "put up to be cut" reason and
"nothing was sold" outcome appeared in the rotation check, again in the
pruning pass, and the name was listed a third time on the "Below the desk's
own entry bar ... Not cut this pass" line.
"""

from src.rotation_parts.reporting import ROTATION_HOLDING_BELOW_BAR
from src.rotation_parts.reporting_lines import owner_precheck_lines, pruning_pass_lines


def _record(below: str = "BRZE") -> dict:
    return {
        "outcome": ROTATION_HOLDING_BELOW_BAR,
        "headroom_pct": 3.0,
        "ceiling_pct": 10.0,
        "floor_pct": 1.0,
        "execute_enabled": True,
        "held_symbol": "BRZE",
        "held_reasons": "the chart reading was neutral",
        "tier": "ineligible_hold",
        "held_examined": "AAPL,BRZE",
        "held_examined_count": 2,
        "held_below_entry_bar": below,
        "held_below_entry_bar_count": len(below.split(",")),
    }


def _message(record: dict) -> str:
    sold: set[str] = set()
    return "\n".join(owner_precheck_lines(record, sold=sold) + pruning_pass_lines(record, sold=sold))


def test_brze_cut_story_told_once_and_not_repeated_below_bar():
    text = _message(_record())
    assert text.count("put up to be cut") + text.count("Put up to be cut") == 1
    assert text.count("The planning AI kept BRZE; nothing was sold.") == 1
    assert "the chart reading was neutral" in text
    assert "Nothing outranked it" in text
    assert "Below the desk's own entry bar" not in text
    assert "Not cut this pass" not in text
    assert "None of them has fallen below" not in text
    assert "Considered and kept: AAPL" in text


def test_other_below_bar_names_still_listed_without_the_cut_name():
    rec = _record("BRZE,UBER")
    rec["held_examined"] = "AAPL,BRZE,UBER"
    rec["held_examined_count"] = 3
    text = _message(rec)
    assert "would not be bought now: UBER. Not cut this pass: UBER." in text
    assert "BRZE" not in text.split("Below the desk's own entry bar")[1]
