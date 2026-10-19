"""n06 (2026-10-15): every AI seat's vote counts the same in the ranking.

The ranking score is a sum read only by order and by gaps, so a common
multiplier changes nothing and only seat RATIOS carry information. No ratio
has been measured, so the per-seat weights are deleted and the rule needs no
constant. The swap brake's evidence key carries the score basis, so a score
recorded under the old weights never pairs with a new one.

2026-10-19 (archetype step C): each counted seat adds exactly 1 -- the score
is the count of agreeing seats, conviction unread, macro uncounted because
its read is market-wide.
"""

from __future__ import annotations

import json
from datetime import date

import src.verdicts as verdicts
from src.models import AnalystVerdict, VerdictEvidence
from src.rotation_parts.score_noise import SCORE_BASIS, evidence_key, measure_score_noise
from src.verdicts import rank_verdicts, score_verdict

SEATS = ("technical", "earnings", "news", "smart_money", "macro")


def _v(seat: str, symbol: str, direction: str, conviction: str, magnitude: float | None = None) -> AnalystVerdict:
    return AnalystVerdict(
        seat=seat,
        symbol=symbol,
        direction=direction,
        magnitude=magnitude,
        conviction=conviction,
        evidence=[VerdictEvidence(label="filing", value=1.0, as_of=date(2026, 10, 14))],
        invalidation="x",
    )


def test_the_seat_weight_table_no_longer_exists():
    assert not hasattr(verdicts, "SEAT_WEIGHT")
    assert not hasattr(verdicts, "seat_weight")
    assert not hasattr(verdicts, "_DEFAULT_SEAT_WEIGHT")


def test_two_seats_with_opposite_verdicts_of_equal_magnitude_cancel():
    """A bullish technical and a bearish news read weigh the same: per-name,
    each scores identically, so neither outranks the other on seat identity
    alone."""
    bull = _v("technical", "AAA", "bullish", "high", magnitude=1.0)
    bear = _v("news", "BBB", "bearish", "low")
    ranked = {c.symbol: c.score for c in rank_verdicts([bull, bear])}
    assert ranked["AAA"] - ranked["BBB"] == 0.0
    # Signed sum of the two votes is zero.
    assert score_verdict(bull) - score_verdict(bear) == 0.0


def test_every_seat_alone_scores_the_same_for_the_same_reading():
    counted = [seat for seat in SEATS if seat != "macro"]
    scores = {rank_verdicts([_v(seat, "XLE", "bullish", "medium")])[0].score for seat in counted}
    assert scores == {1.0}
    # Macro's market-wide read is not counted (2026-10-19).
    assert rank_verdicts([_v("macro", "XLE", "bullish", "medium")])[0].score == 0.0


def test_ranking_order_equals_the_unweighted_sum():
    verdicts_in = [
        _v("technical", "AAA", "bullish", "medium", magnitude=0.5),
        _v("macro", "AAA", "bullish", "high"),
        _v("earnings", "BBB", "bullish", "high"),
        _v("smart_money", "CCC", "bullish", "high"),
        _v("macro", "CCC", "bullish", "high"),
        _v("technical", "DDD", "bullish", "low", magnitude=1.0),
    ]
    by_symbol: dict[str, float] = {}
    for v in verdicts_in:
        by_symbol[v.symbol] = by_symbol.get(v.symbol, 0.0) + score_verdict(v)
    rung: dict[str, float] = {}
    for v in verdicts_in:
        rung[v.symbol] = rung.get(v.symbol, 0.0) + (v.magnitude or 0.0)
    # Equal counts fall to the summed rating rung, then symbol (unchanged).
    expected = sorted(by_symbol, key=lambda s: (-round(by_symbol[s], 4), -rung[s], s))
    ranked = rank_verdicts(verdicts_in)
    assert [c.symbol for c in ranked] == expected
    assert {c.symbol: c.score for c in ranked} == {s: round(t, 4) for s, t in by_symbol.items()}


def test_three_agreeing_lower_rated_seats_now_outrank_one_technical_read():
    """Under the deleted weights a lone technical 2.0 read scored 2.4 and
    beat macro high + smart money high + news medium (0.8 + 0.8 + 0.5 = 2.1).
    Counted (2026-10-19), smart money and news are 2 agreeing seats -- macro
    is uncounted -- and still rank first over one technical read."""
    ranked = rank_verdicts(
        [
            _v("technical", "AAA", "bullish", "high", magnitude=1.0),
            _v("macro", "CCC", "bullish", "high"),
            _v("smart_money", "CCC", "bullish", "high"),
            _v("news", "CCC", "bullish", "medium"),
        ]
    )
    assert [(c.symbol, c.score) for c in ranked] == [("CCC", 2.0), ("AAA", 1.0)]


def test_the_evidence_key_names_the_score_basis():
    [c] = rank_verdicts([_v("technical", "AAA", "bullish", "high", magnitude=1.0)])
    key = json.loads(evidence_key(c))
    assert key["score_basis"] == SCORE_BASIS == "agreeing_seat_count"


def test_an_old_basis_key_never_pairs_with_a_new_one():
    [c] = rank_verdicts([_v("technical", "AAA", "bullish", "high", magnitude=1.0)])
    new_key = evidence_key(c)
    old = json.loads(new_key)
    del old["score_basis"]
    old_key = json.dumps(old, sort_keys=True)  # exactly what a pre-n06 row recorded
    assert old_key != new_key
    rows = [
        ("2026-10-13T14:00:00", "run-old", "AAA", 2.4, old_key),
        ("2026-10-16T14:00:00", "run-new", "AAA", 2.0, new_key),
    ]
    brake = measure_score_noise(rows)
    assert brake.pairs == 0
    assert brake.value is None  # UNMEASURED until new-basis pairs exist
    rows.append(("2026-10-17T14:00:00", "run-new2", "AAA", 2.1, new_key))
    assert measure_score_noise(rows).pairs == 1
