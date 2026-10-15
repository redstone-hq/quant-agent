"""Reads the measured score-noise brake from the live journal.

Kept apart from `score_noise` on purpose: `src.rotation` re-exports that
module and the read-only `src/api` imports `src.rotation`, so the journal
import here must not be reachable from it. Only the pipeline calls this.
"""

from __future__ import annotations

from src.rotation_parts.score_noise import (
    ScoreNoiseBrake,
    _ROTATION_ROWS_SQL,
    measure_score_noise,
    observations_from_rows,
)
from src.storage.analytics.calibration import CLEAN_RECORD_START_UTC
from src.storage.event_journal import PIPELINE_EVENT_KIND


def measure_score_noise_from_db(conn) -> ScoreNoiseBrake:
    """Read-only measurement over the live `specialist_evidence` table."""
    since = CLEAN_RECORD_START_UTC.replace("T", " ")
    rows = conn.execute(_ROTATION_ROWS_SQL, (PIPELINE_EVENT_KIND, since)).fetchall()
    return measure_score_noise(observations_from_rows(rows))
