"""The drawdown ladder cuts WHOLE positions: losers first, then the weakest winner.

Owner rulings: 2026-10-09 22:33 ET — "You always sell losers first, and you
don't break the rule, sell part of a winner." 2026-10-12 — with no losers
left, sell the winner the desk rates weakest, whole ("1a").

Before this change the last cut was sized to the exact breach, so the ladder
sold PART of a position — a partial sale of a winner whenever the last name
cut was one.
"""

import random
from types import SimpleNamespace
from unittest.mock import MagicMock

from src.delever.trims import DeleverTrims
from src.models import Position
from src.risk.rules import GrossCeilingOutcome, apply_gross_ceiling, resolve_gross_ceiling

EQUITY = 10_000.0
BASE_X = 2.0


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


def _cuts(outcome):
    return [(t.symbol, t.action, t.allocation_pct) for t in outcome.trims]


def _one_x():
    ceiling = resolve_gross_ceiling(-16.0, base_x=BASE_X)
    assert ceiling.ceiling_x == 1.0  # $10k on $10k equity
    return ceiling


def _half_x():
    ceiling = resolve_gross_ceiling(-25.0, base_x=BASE_X)
    assert ceiling.ceiling_x == 0.5  # $5k on $10k equity
    return ceiling


def test_a_breach_smaller_than_one_loser_sells_that_whole_loser():
    # $11k gross against $10k: $1k over. LOSER is $6k and down $1k.
    book = [_pos("LOSER", 60, 120.0, 100.0), _pos("WIN", 50, 90.0, 100.0)]
    out = apply_gross_ceiling([], book, EQUITY, _one_x())
    assert _cuts(out) == [("LOSER", "SELL", 100.0)]
    assert "loser" in out.trims[0].reasoning.lower()
    assert "Drawdown cut #1" in out.trims[0].reasoning


def test_two_losers_biggest_loser_goes_first_and_both_go_whole():
    # $12k gross against $5k: $7k over. BIG is down $2k, SMALL down $500.
    book = [
        _pos("SMALL", 40, 112.5, 100.0),
        _pos("BIG", 40, 150.0, 100.0),
        _pos("WIN", 40, 97.5, 100.0),
    ]
    out = apply_gross_ceiling([], book, EQUITY, _half_x())
    assert _cuts(out) == [("BIG", "SELL", 100.0), ("SMALL", "SELL", 100.0)]
    assert "Drawdown cut #2" in out.trims[1].reasoning


def test_no_losers_sells_the_weakest_rated_winner_whole():
    # $12k gross against $10k: $2k over, every name a winner.
    book = [
        _pos("STRONG", 50, 80.0, 100.0),
        _pos("WEAK", 50, 98.0, 100.0),
        _pos("MID", 20, 75.0, 100.0),
    ]
    rated = {"STRONG": (2, 0), "WEAK": (0, 0), "MID": (2, 1)}
    out = apply_gross_ceiling([], book, EQUITY, _one_x(), conviction_rank=rated)
    assert _cuts(out) == [("WEAK", "SELL", 100.0)]
    assert "weakest-rated winner" in out.trims[0].reasoning


def test_with_no_fresh_rating_the_smallest_gain_goes_first_whole():
    book = [_pos("A", 50, 80.0, 100.0), _pos("B", 50, 98.0, 100.0), _pos("C", 20, 75.0, 100.0)]
    out = apply_gross_ceiling([], book, EQUITY, _one_x())
    assert _cuts(out) == [("B", "SELL", 100.0)]


def test_a_conviction_dead_winner_never_jumps_ahead_of_a_loser():
    # The rating ranks winners only; every loser goes first.
    book = [_pos("LOSER", 60, 101.0, 100.0), _pos("DEAD", 60, 90.0, 100.0)]
    rated = {"DEAD": (0, 0), "LOSER": (2, 0)}
    out = apply_gross_ceiling([], book, EQUITY, _one_x(), conviction_rank=rated)
    assert _cuts(out) == [("LOSER", "SELL", 100.0)]


def test_shorts_mirror_the_rule_and_are_covered_whole():
    # Two shorts: SQUEEZED is $1.5k against us, DRIFT $200 in our favour.
    book = [_pos("SQUEEZED", -60, 75.0, 100.0), _pos("DRIFT", -50, 104.0, 100.0)]
    out = apply_gross_ceiling([], book, EQUITY, _one_x())
    assert _cuts(out) == [("SQUEEZED", "COVER", 100.0)]
    # No losing short left: the winning short is covered whole too.
    book = [_pos("A", -60, 101.0, 100.0), _pos("B", -50, 104.0, 100.0)]
    out = apply_gross_ceiling([], book, EQUITY, _one_x())
    assert _cuts(out) == [("A", "COVER", 100.0)]


def test_the_ladder_never_sells_any_position_partially():
    rng = random.Random(20261013)
    for _ in range(300):
        book = [_pos(f"S{i}", rng.choice([-1, 1]) * rng.randint(1, 80), rng.uniform(50, 150), 100.0) for i in range(6)]
        ceiling = resolve_gross_ceiling(rng.choice([-9.0, -16.0, -25.0]), base_x=BASE_X)
        out = apply_gross_ceiling([], book, EQUITY, ceiling)
        assert all(t.allocation_pct == 100.0 for t in out.trims), _cuts(out)
        if out.trims:
            # The cut stops once the book is at or under the ceiling.
            sold = {t.symbol for t in out.trims}
            left = sum(abs(p.market_value) for p in book if p.symbol not in sold)
            assert left <= out.ceiling_usd + 1e-6
            last = out.trims[-1].symbol
            before_last = left + abs(next(p.market_value for p in book if p.symbol == last))
            assert before_last > out.ceiling_usd + 1e-6, "sold one name more than the ruling allows"


def test_a_whole_cut_reaches_the_broker_as_the_whole_held_quantity():
    held = _pos("LOSER", 37, 120.0, 100.0)
    submit = MagicMock(return_value=({"id": "o1"}, {}))
    trims = DeleverTrims(
        alert_owner_delever_incomplete=MagicMock(),
        compute_deployable_cash=MagicMock(return_value=0.0),
        finalize_pending_protections=MagicMock(),
        format_qty=str,
        full_sell_qty=lambda q: q,
        live_delever_price=MagicMock(return_value=(None, None)),
        record_delever_shortfall=MagicMock(),
        resolve_gross_ceiling=MagicMock(),
        submit_protected_sell=submit,
        broker=MagicMock(),
        db=MagicMock(),
    )
    outcome = apply_gross_ceiling([], [held, _pos("WIN", 70, 90.0, 100.0)], EQUITY, _one_x())
    assert isinstance(outcome, GrossCeilingOutcome)
    ctx = SimpleNamespace(positions=[held], total_value=EQUITY, run_id="t")
    trims._submit_gross_ceiling_trims(ctx, _one_x(), outcome)
    assert submit.call_count == 1
    assert submit.call_args.kwargs["qty"] == 37
    assert submit.call_args.kwargs["position_qty_before_sell"] == 37
