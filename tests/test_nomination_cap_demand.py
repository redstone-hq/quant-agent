"""Item 90 / number ledger — the recording behind the two nomination caps.

`nominations.max_per_seat_per_run` (3) is an unsourced round number. It
cannot be settled by argument: the
open question is whether offered demand ever reaches the cap, and what
is discarded when it does. These cover `src/nominations.py`'s
`measure_cap_demand`, the recording that makes a run of real sessions
answer that, and pin that it changes nothing the desk selects.
"""

from src.models import Nomination

# --- Item 90 / number ledger: what the two nomination caps actually cut ----
#
# `nominations.max_per_seat_per_run` (3) is an
# unsourced round number whose ledger row can only be settled by a run of
# real sessions. `measure_cap_demand` is the recording that makes those
# sessions readable: it must report offered-versus-kept per seat, name what
# each cap dropped, and never change what `select_nominations` returns.


def _nom(symbol: str, conviction: str = "medium") -> Nomination:
    return Nomination(
        symbol=symbol,
        conviction=conviction,
        observation=f"{symbol} obs",
    )


def test_cap_demand_reports_no_binding_when_demand_is_under_the_seat_cap():
    from src.nominations import measure_cap_demand

    out = measure_cap_demand(
        {"news_analyst": [_nom("AAA"), _nom("BBB")]},
        max_per_seat=3,
    )
    assert out["per_seat_bound"] == []
    assert out["distinct_after_merge"] == 2
    assert out["per_seat"]["news_analyst"]["offered"] == 2
    assert out["per_seat"]["news_analyst"]["kept"] == 2
    assert out["per_seat"]["news_analyst"]["dropped"] == []
    assert out["caps"] == {"max_per_seat": 3}


def test_cap_demand_names_every_symbol_the_seat_cap_dropped():
    from src.nominations import measure_cap_demand, select_nominations

    by_seat = {
        "news_analyst": [
            _nom("AAA", "high"),
            _nom("BBB", "high"),
            _nom("CCC", "high"),
            _nom("DDD", "low"),
        ],
        "macro_analyst": [_nom("EEE", "high"), _nom("FFF", "high")],
        "earnings_analyst": [_nom("GGG", "high")],
    }
    out = measure_cap_demand(by_seat, max_per_seat=3)

    # The per-seat cap bound on exactly the seat that offered four.
    assert out["per_seat_bound"] == ["news_analyst"]
    assert out["per_seat"]["news_analyst"]["offered"] == 4
    assert out["per_seat"]["news_analyst"]["kept"] == 3
    assert out["per_seat"]["news_analyst"]["dropped"] == [
        {"symbol": "DDD", "conviction": "low"},
    ]
    assert out["per_seat"]["macro_analyst"]["dropped"] == []

    # Six distinct names survive the per-seat cap; no run-wide cap cuts them.
    assert out["distinct_after_merge"] == 6
    kept = sorted(c.symbol for c in select_nominations(by_seat, max_per_seat=3))
    assert kept == ["AAA", "BBB", "CCC", "EEE", "FFF", "GGG"]


def test_cap_demand_does_not_change_what_select_nominations_returns():
    from src.nominations import measure_cap_demand, select_nominations

    by_seat = {
        "news_analyst": [_nom("AAA", "high"), _nom("BBB"), _nom("CCC")],
        "macro_analyst": [_nom("AAA", "low"), _nom("DDD", "high")],
    }
    before = [
        c.symbol
        for c in select_nominations(
            by_seat,
            max_per_seat=2,
        )
    ]
    measure_cap_demand(by_seat, max_per_seat=2)
    after = [
        c.symbol
        for c in select_nominations(
            by_seat,
            max_per_seat=2,
        )
    ]
    assert before == after
    assert by_seat["news_analyst"][0].symbol == "AAA"
