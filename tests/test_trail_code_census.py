"""Item 196: the per-run census of trail outcomes.

`record_trail_state_if_changed` writes nothing when a stock refuses for
the same reason twice running, so it can say WHY a stop has not moved and
never HOW OFTEN. These pin the census that answers the frequency question,
and the refusal it exists to count.
"""

import json

from src.execution.exit_path_records import (
    CENSUS_SYMBOL,
    TRAIL_CENSUS_KIND,
    record_trail_code_census,
)
from src.risk.trailing import (
    TRAIL_CODE_INSIDE_NOISE_BAND,
    evaluate_trailing_stop,
)


class _DB:
    def __init__(self):
        self.rows = []

    def insert_specialist_evidence(self, **kw):
        self.rows.append(kw)


def test_census_writes_one_row_with_every_count():
    db = _DB()
    assert record_trail_code_census(
        db,
        run_id="r1",
        counts={TRAIL_CODE_INSIDE_NOISE_BAND: 3, "trailed": 1},
    )
    assert len(db.rows) == 1
    row = db.rows[0]
    assert row["kind"] == TRAIL_CENSUS_KIND
    assert row["symbol"] == CENSUS_SYMBOL
    payload = json.loads(row["evidence_json"])
    assert payload["counts"][TRAIL_CODE_INSIDE_NOISE_BAND] == 3
    assert payload["evaluations"] == 4


def test_empty_census_writes_nothing():
    db = _DB()
    assert record_trail_code_census(db, run_id="r1", counts={}) is False
    assert (
        record_trail_code_census(
            db,
            run_id="r1",
            counts={"trailed": 0},
        )
        is False
    )
    assert db.rows == []


from datetime import date as _date

#: Every fixture bar is a real completed daily bar dated after the entry day:
#: the chandelier reads only bars dated strictly after `entry_date`.
_ENTRY_DAY = _date(2026, 1, 2)
_BAR_DAY = _date(2026, 1, 5)


class _Bar:
    def __init__(self, high, low, close):
        self.high, self.low, self.close = high, low, close
        self.date = _BAR_DAY


def test_candidate_inside_the_band_is_still_refused_outright():
    """Measured 2026-10-01 on the desk\'s 101x276 stored daily bars: over a
    14-session horizon the band-edge fallback changed 28 exits, 15 worse by
    a mean 1.44 ATR against 13 better by a mean 0.61 ATR. Refusing stands.
    """
    atr = 2.0
    bars = [_Bar(105.0, 100.0, 103.0)] * 20
    ev = evaluate_trailing_stop(
        symbol="TEST",
        setup_type="breakout",
        entry=100.0,
        reference_target=None,
        current_price=100.0,
        current_stop=90.0,
        bars=bars,
        entry_date=_ENTRY_DAY,
        atr=atr,
        qty=10,
        risk_per_share=abs(100.0 - 90.0),
    )
    # chandelier 105 - 3*2 = 99.0 sits above the one-ATR band floor 100.0 - 2.0 = 98.0
    assert ev.proposal is None
    assert ev.code == TRAIL_CODE_INSIDE_NOISE_BAND
