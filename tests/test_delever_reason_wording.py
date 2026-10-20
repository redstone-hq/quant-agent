"""A gross-ceiling sale is explained as the borrowing limit, not a drawdown cut.

Owner read, 2026-10-20 1:01 pm ET (trades row 154): "Drawdown cut #1
(max_gross_exposure) ... inside the 8% band where no de-levering applies."
The sale was the 2.0x borrowing limit, not the drawdown ladder, and the
ladder sentence contradicted the sale it sat under. Wording only.
"""

from src.models import Position
from src.risk.rules import apply_gross_ceiling, resolve_gross_ceiling

EQUITY = 10_000.0


def _pos(symbol, qty, avg_entry, price):
    return Position(
        symbol=symbol,
        qty=qty,
        avg_entry=avg_entry,
        current_price=price,
        market_value=qty * price,
        unrealized_pnl=qty * (price - avg_entry),
        sector="Technology",
    )


def _breach_at_full_limit():
    # Ladder untouched (3.7% drawdown keeps the 2.0x limit); the book holds
    # $21k against the $20k limit, so one loser is sold whole.
    ceiling = resolve_gross_ceiling(-3.7, base_x=2.0)
    assert ceiling.ceiling_x == 2.0
    book = [_pos("LOSER", 110, 110.0, 100.0), _pos("WIN", 100, 90.0, 100.0)]
    return apply_gross_ceiling([], book, EQUITY, ceiling)


def test_sale_is_explained_as_the_borrowing_limit_not_a_drawdown_cut():
    out = _breach_at_full_limit()
    text = out.trims[0].reasoning
    assert "borrowing limit" in text
    assert "Drawdown cut" not in text
    assert "no de-levering applies" not in text
    assert "max_gross_exposure" not in text
    assert len(text) <= 500


def test_untouched_ladder_says_the_limit_stays_rather_than_no_de_levering():
    reason = resolve_gross_ceiling(-3.7, base_x=2.0).reason
    assert "stays 2.0x equity" in reason
    assert "no de-levering" not in reason
    assert "band where" not in reason


def test_notes_carry_the_same_plain_wording():
    out = _breach_at_full_limit()
    assert out.notes
    assert "drawdown cut" not in " ".join(out.notes).lower()
    assert "borrowing limit" in " ".join(out.notes)
