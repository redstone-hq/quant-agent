"""The pruning lines say what was DONE with a holding put up to be cut.

Live defect, 20 Oct 2026 09:41 ET: the morning message said UBER was "up to be
cut", that "whatever this frees stays in the book as cash", and "Not cut this
pass: none" — on a session where the planning AI kept UBER and sold nothing
(the only order was a BRZE buy). The pre-check row is a proposal; the
session's orders are the outcome, and the lines must read the outcome.
"""

from src.rotation_parts.reporting import ROTATION_HOLDING_BELOW_BAR, sold_symbols
from src.rotation_parts.reporting_lines import owner_precheck_lines, pruning_pass_lines


def _record() -> dict:
    return {
        "outcome": ROTATION_HOLDING_BELOW_BAR,
        "headroom_pct": 3.0,
        "ceiling_pct": 10.0,
        "floor_pct": 1.0,
        "execute_enabled": True,
        "ranked_margin_enabled": False,
        "binding": "",
        "held_symbol": "UBER",
        "held_reasons": "R2 rating below bar",
        "tier": "ineligible_hold",
        "held_examined": "UBER,AAPL",
        "held_examined_count": 2,
        "held_below_entry_bar": "UBER",
        "held_below_entry_bar_count": 1,
    }


def test_proposal_kept_says_kept_and_claims_no_freed_cash():
    sold = sold_symbols({"pm_orders": [{"symbol": "BRZE", "action": "BUY"}], "trades": []})
    text = "\n".join(owner_precheck_lines(_record(), sold=sold))
    assert "UBER was put up to be cut" in text
    assert "The planning AI kept UBER; nothing was sold." in text
    assert "frees" not in text and "freed" not in text

    pruning = "\n".join(pruning_pass_lines(_record(), sold=sold))
    assert "Put up to be cut: UBER" in pruning
    assert "The planning AI kept UBER; nothing was sold." in pruning
    assert "Not cut this pass: UBER." in pruning


def test_proposal_sold_says_sold():
    sold = sold_symbols({"pm_orders": [{"symbol": "UBER", "action": "SELL"}], "trades": []})
    text = "\n".join(owner_precheck_lines(_record(), sold=sold))
    assert "UBER was sold this session" in text
    assert "kept UBER" not in text

    pruning = "\n".join(pruning_pass_lines(_record(), sold=sold))
    assert "UBER was sold this session" in pruning
    assert "Not cut this pass: none." in pruning


def test_unknown_outcome_claims_neither():
    text = "\n".join(owner_precheck_lines(_record()) + pruning_pass_lines(_record()))
    assert "kept UBER" not in text and "was sold this session" not in text
    assert "frees" not in text
    assert "recorded with the session's orders" in text


def test_session_message_reads_the_outcome_off_the_orders():
    from src.trader_feed.decision import _append_rotation

    lines: list[str] = []
    _append_rotation(
        lines,
        {"rotation": _record(), "pm_orders": [{"symbol": "BRZE", "action": "BUY"}], "trades": []},
    )
    text = "\n".join(lines)
    assert "The planning AI kept UBER; nothing was sold." in text
    assert "frees" not in text
