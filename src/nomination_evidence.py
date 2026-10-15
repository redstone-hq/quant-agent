"""One place that writes the `nomination_summary` evidence row.

The row was built three times inline in `src/stage_morning_research.py`,
once per exit path of the nomination responder pass, with the shared
fields copied each time. Copies drift, and the number ledger now depends
on this row carrying the nomination caps' demand measurement on EVERY
path -- a field present on two paths out of three measures nothing. So
the shared payload is assembled once here and each call site passes only
what is particular to its path.
"""

from __future__ import annotations

import json


def persist_nomination_summary(
    db,
    *,
    run_id: str,
    nominations_by_seat: dict,
    total_raw: int,
    max_per_seat: int,
    **particular,
) -> None:
    """Write one `nomination_summary` row for this run.

    `cap_demand` is the measurement described at `src/nominations.py`
    `measure_cap_demand` -- offered versus kept per seat, and the names
    each of the two caps dropped. It is measured here rather than at the
    caller so it cannot be recorded on some paths and not others, and it
    is recorded even when nothing was cut, because a cap that cut nothing
    is exactly as much evidence as a cap that cut something. Nothing here
    decides anything: the selection was already made before this runs.
    """
    from src.nominations import measure_cap_demand
    from src.pipeline_stages import _persist_evidence

    cap_demand = measure_cap_demand(
        nominations_by_seat,
        max_per_seat=max_per_seat,
    )
    payload = {
        "raw_nominations": total_raw,
        "raw_by_seat": {k: len(v) for k, v in nominations_by_seat.items()},
        "cap_demand": cap_demand,
    }
    payload.update(particular)
    _persist_evidence(
        db,
        run_id=run_id,
        agent_name="pipeline",
        kind="nomination_summary",
        scope="run",
        evidence_json=json.dumps(payload, sort_keys=True),
    )
