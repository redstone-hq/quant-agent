"""The ranked-margin swap's brake: the desk's own measured score noise.

Owner decision 2026-10-15 (checklist q07): the stock-swap rule stays, the
unsourced 25% relative margin goes, and a swap must instead clear how much the
ranking score moves on its own for a name about which nothing new arrived.

What is measured. Every session the rotation pre-check records the full
seat-weighted composite (`src.verdicts.rank_verdicts`, the score rotation
compares) of the weakest held name and the best new candidate, each with an
EVIDENCE KEY: the seats that covered the name plus every DATED evidence item
those seats cited (seat, label, date). Two consecutive recorded observations
of the same name whose keys are identical are a no-news pair: the same seats
looked and no new dated fact (filing, earnings, news item) reached any of
them, so any change in the score is the scoring's own jitter.

Which statistic, and why no chosen cut. The brake is the LARGEST absolute
score change seen across all no-news pairs. A mean or standard deviation
needs a chosen multiple and a quantile needs a chosen percentile; the maximum
needs neither, and it is the conservative end: no swap is triggered by a
gap the scoring has already produced on its own.

Only rows on or after the clean-record start count (owner ruling 2026-10-09:
earlier history is not evidence). With no no-news pair at all the brake is
UNMEASURED and the ranked-margin tier refuses to swap.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Iterable


@dataclass(frozen=True)
class ScoreNoiseBrake:
    """The measured brake, or the reason it could not be measured."""

    #: Largest absolute composite-score change across no-news pairs, in
    #: score units; `None` when no pair exists.
    value: float | None
    #: How many no-news pairs the value rests on.
    pairs: int
    #: How many recorded observations were read.
    observations: int

    @property
    def measured(self) -> bool:
        return self.value is not None

    def describe(self) -> str:
        if self.value is None:
            return (
                f"score-noise brake UNMEASURED: none of {self.observations} recorded "
                "score observation(s) since the clean-record start form a no-news pair"
            )
        return (
            f"score-noise brake {self.value:.4f} (largest score change on an unchanged "
            f"name, from {self.pairs} no-news pair(s))"
        )


UNMEASURED = ScoreNoiseBrake(value=None, pairs=0, observations=0)


#: How `RankedCandidate.score` is computed. Bump it whenever the score's
#: arithmetic changes, so scores from two bases never pair.
AGREEING_SEAT_COUNT_BASIS = "agreeing_seat_count"
#: Rotation refuses its like-for-like comparison while the basis is the plain
#: count (`src/rotation.py`, "awaiting_measured_seat_weights").
SCORE_BASIS = AGREEING_SEAT_COUNT_BASIS


def evidence_key(candidate) -> str:
    """The name's seat coverage plus every dated fact its seats cited.

    Undated numbers (prices, levels, ratios) are left out on purpose: they
    move with the tape every session and are the inputs the scoring is meant
    to be stable across, not news. `""` when the candidate carries nothing.

    `score_basis` names how the score itself was computed. Seat weights were
    deleted on 2026-10-15 (n06, every seat counts equally until measured records replace it), so a row
    scored under the old weights carries no basis and a new row carries
    `SCORE_BASIS`; the two keys can never be identical, so an old score is
    never paired with a new one and the brake restarts its measurement.
    Bumped 2026-10-19 from "conviction_only" to "agreeing_seat_count" when
    the score became the count of agreeing seats: a conviction-sum score
    never pairs with a count, so the brake reads unmeasured and no swap is
    made until pairs on the new basis are recorded.
    """
    if candidate is None:
        return ""
    seats = sorted({v.seat for v in getattr(candidate, "verdicts", ())} | set(getattr(candidate, "neutral_seats", ())))
    dated = sorted(
        (v.seat, item.label, item.as_of.isoformat())
        for v in getattr(candidate, "verdicts", ())
        for item in v.evidence
        if item.as_of is not None
    )
    return json.dumps({"seats": seats, "dated": dated, "score_basis": SCORE_BASIS}, sort_keys=True)


def measure_score_noise(observations: Iterable[tuple[str, str, str, float, str]]) -> ScoreNoiseBrake:
    """The brake from `(timestamp, run_id, symbol, score, evidence_key)` rows.

    Rows are ordered by timestamp; per symbol, each observation is paired
    with that symbol's previous one (one per run). A pair counts only when
    both keys are non-empty and identical.
    """
    rows = sorted(observations, key=lambda r: (r[0], r[1]))
    last: dict[str, tuple[str, float, str]] = {}
    changes: list[float] = []
    for _ts, run_id, symbol, score, key in rows:
        prev = last.get(symbol)
        if prev is not None and prev[0] == run_id:
            continue
        if prev is not None and key and prev[2] == key:
            changes.append(abs(float(score) - prev[1]))
        last[symbol] = (run_id, float(score), key)
    if not changes:
        return ScoreNoiseBrake(value=None, pairs=0, observations=len(rows))
    return ScoreNoiseBrake(value=round(max(changes), 4), pairs=len(changes), observations=len(rows))


#: The pre-check rows `_record_rotation_precheck` writes each session.
_ROTATION_ROWS_SQL = (
    "SELECT timestamp, run_id, evidence_json FROM specialist_evidence "
    'WHERE kind = ? AND timestamp >= ? AND evidence_json LIKE \'%"stage": "rotation"%\''
)


def observations_from_rows(rows) -> list[tuple[str, str, str, float, str]]:
    """Both sides' (symbol, score, key) out of stored rotation pre-check rows."""
    out: list[tuple[str, str, str, float, str]] = []
    for ts, run_id, raw in rows:
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError):
            continue
        if payload.get("stage") != "rotation":
            continue
        for side in ("held", "new"):
            symbol = payload.get(f"{side}_symbol")
            score = payload.get(f"{side}_score")
            if not symbol or isinstance(score, bool) or not isinstance(score, (int, float)):
                continue
            out.append(
                (str(ts), str(run_id), str(symbol), float(score), str(payload.get(f"{side}_evidence_key") or ""))
            )
    return out
