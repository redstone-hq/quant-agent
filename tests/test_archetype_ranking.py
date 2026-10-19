"""Archetype step C (owner archetype 2026-10-19): the ranking counts agreeing seats.

A seat's self-declared conviction (low/medium/high) is a number nobody can
check, so it must never rank. The score is the count of seats agreeing in the
trade direction, every seat weighted equally (the published prior for a
near-zero record: James-Stein shrinkage to the group mean; DeMiguel-Garlappi-
Uppal 2009). Macro's read is market-wide, so it is not counted. The score
basis moved, so the swap brake restarts unmeasured and rotation refuses.
"""

from __future__ import annotations

import json
from datetime import date

from src.models import AnalystVerdict, VerdictEvidence
from src.rotation import evaluate_rotation
from src.rotation_parts.score_noise import SCORE_BASIS, evidence_key, measure_score_noise
from src.verdicts import rank_verdicts


def _v(seat: str, symbol: str, conviction: str, magnitude: float | None = None) -> AnalystVerdict:
    return AnalystVerdict(
        seat=seat,
        symbol=symbol,
        direction="bullish",
        magnitude=magnitude,
        conviction=conviction,
        evidence=[VerdictEvidence(label="filing", value=1.0, as_of=date(2026, 10, 14))],
        invalidation="x",
    )


def test_more_agreeing_seats_outrank_fewer_high_conviction_seats():
    wide = [_v("technical", "ZZZA", "low", 0.5), _v("news", "ZZZA", "low"), _v("earnings", "ZZZA", "low")]
    loud = [_v("technical", "ZZZB", "high", 1.0), _v("smart_money", "ZZZB", "high")]
    ranked = rank_verdicts([*loud, *wide])
    assert [(c.symbol, c.score) for c in ranked] == [("ZZZA", 3.0), ("ZZZB", 2.0)]


def test_stated_conviction_does_not_change_the_order_or_the_score():
    def _book(conviction: str) -> list[tuple[str, float]]:
        verdicts = [
            _v("technical", "ZZZA", conviction, 0.5),
            _v("news", "ZZZA", conviction),
            _v("technical", "ZZZB", "medium", 0.5),
            _v("earnings", "ZZZB", "medium"),
            _v("technical", "ZZZC", "high", 0.5),
        ]
        return [(c.symbol, c.score) for c in rank_verdicts(verdicts)]

    books = {c: _book(c) for c in ("low", "medium", "high")}
    assert books["low"] == books["medium"] == books["high"]
    assert books["low"] == [("ZZZA", 2.0), ("ZZZB", 2.0), ("ZZZC", 1.0)]


def test_macro_does_not_add_to_any_names_count():
    base = [_v("technical", "ZZZA", "medium", 0.5), _v("technical", "ZZZB", "medium", 0.5), _v("news", "ZZZB", "low")]
    without = [(c.symbol, c.score) for c in rank_verdicts(base)]
    with_macro = [(c.symbol, c.score) for c in rank_verdicts([*base, _v("macro", "ZZZA", "high")])]
    assert with_macro == without == [("ZZZB", 2.0), ("ZZZA", 1.0)]


def test_swap_brake_fails_safe_on_the_new_basis():
    assert SCORE_BASIS == "agreeing_seat_count"
    held = rank_verdicts([_v("technical", "ZZZH", "low", 0.5)])[0]
    new = rank_verdicts([_v("technical", "ZZZN", "high", 1.0), _v("news", "ZZZN", "high")])[0]
    # An old-basis score never pairs with a new-basis one: the brake is unmeasured.
    new_key = evidence_key(held)
    old = json.loads(new_key)
    old["score_basis"] = "conviction_only"
    old_key = json.dumps(old, sort_keys=True)
    rows = [
        ("2026-10-16 14:00:00", "r1", "ZZZH", 0.0, old_key),
        ("2026-10-19 14:00:00", "r2", "ZZZH", 1.0, new_key),
    ]
    brake = measure_score_noise(rows)
    assert brake.value is None and brake.pairs == 0

    def _run(noise):
        return evaluate_rotation(
            ranked=[new, held],
            blocked={},
            held_symbols={"ZZZH"},
            headroom_pct=0.1,
            floor_pct=0.5,
            noise_brake=noise,
        )

    unmeasured = _run(brake.value)
    assert unmeasured.opportunity is None
    assert unmeasured.refusal.point == "score_noise_unmeasured"
    # Even once a brake is measured, the plain count refuses the swap until
    # seat weights are measured.
    measured = _run(0.25)
    assert measured.opportunity is None
    assert measured.refusal.point == "awaiting_measured_seat_weights"
