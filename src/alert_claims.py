"""Per-symbol, per-trading-day owner-alert claims and the watchdog state file.

Lifted verbatim out of `src/coverage_watchdog.py` (2026-10-05) so that the
broker seam can claim a page without importing the watchdog. The watchdog
imports THIS module and re-exports every name; `src/execution/scale_in.py`
imports it directly. The dependency now runs one way:

    src.execution.scale_in -> src.alert_claims <- src.coverage_watchdog -> src.execution.scale_in

A claim is reserved BEFORE the message is handed to the notifier so two
processes finding the same condition at the same moment cannot both send;
`release_typed_alert` hands a claim back when the send did not land. Nothing
declined is dropped silently: `_record_suppressed_alert` keeps the count and
the last events in the same state file, which `src/api/db_reads.py` reads.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from collections.abc import Iterable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.coverage_watchdog_records import record_watchdog_pass
from src.data_paths import alerting_dir
from src.trading_calendar import ET, utc_now

logger = logging.getLogger(__name__)


#: On-box record, gitignored like its siblings under data/alerting/.
STATE_PATH = alerting_dir() / "coverage_heartbeat.json"


def _utc_now() -> datetime:
    """Seam for tests — real code never patches `datetime` itself."""
    return utc_now()


def load_state(path: Path | None = None) -> dict[str, Any]:
    try:
        raw = json.loads((path or STATE_PATH).read_text())
    except (OSError, ValueError):
        raw = None
    if not isinstance(raw, dict):
        raw = {}
    raw.setdefault("alerted_for_day", None)
    raw.setdefault("exposure_alerted_symbols", None)
    # Kept as a truthful record of the last day a placement-failure alert
    # went out, and still written; it is no longer what SUPPRESSES one.
    # Suppression reads `repair_failure_alerted_symbols` below, which is
    # keyed per position as well as per day — see
    # `_repair_failure_alerted_symbols`.
    raw.setdefault("repair_failure_alerted_for_day", None)
    raw.setdefault("repair_failure_alerted_symbols", None)
    raw.setdefault("last_result", None)
    raw.setdefault("updated_at", None)
    return raw


def save_state(state: dict[str, Any], path: Path | None = None) -> bool:
    """Atomic write, same shape as the sibling watchdogs. Never raises."""
    target = path or STATE_PATH
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(dir=str(target.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as handle:
                json.dump(state, handle, indent=2, sort_keys=True)
                handle.write("\n")
            os.replace(tmp_name, target)
        except BaseException:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise
    except OSError:
        return False
    return True


def repair_failure_alert_day(now: datetime | None = None) -> str:
    """The ET calendar date a placement failure is filed under.

    Deliberately NOT `most_recent_trading_day`: that answers "which session
    should have re-placed a stop overnight", which at 10:00 ET is still
    yesterday. A placement that just failed is happening TODAY, and both
    paths must agree on the key or the shared marker is no marker at all.
    """
    return (now or _utc_now()).astimezone(ET).date().isoformat()


def _typed_alerted_symbols(
    state: dict[str, Any],
    day: str,
    kind: str,
) -> set[str]:
    raw = state.get(f"typed_alerted_symbols::{kind}")
    if not isinstance(raw, dict) or raw.get("day") != day:
        return set()
    return {str(sym).strip().upper() for sym in (raw.get("symbols") or []) if str(sym).strip()}


#: How many suppression records to retain per alert type. Bounds the state
#: file; the running `count` is never truncated, only the per-event list.
_SUPPRESSION_LOG_LIMIT = 50


def _record_suppressed_alert(
    state: dict[str, Any],
    kind: str,
    day: str,
    keys: Iterable[str],
) -> None:
    """Durably note an alert this helper declined to resend (item 211).

    Nothing is silently dropped: the owner not being paged a second time is
    a presentation decision, and the underlying fact still has to be
    readable afterwards or the desk has stopped reporting its true state.
    """
    log = state.get("suppressed_alerts")
    if not isinstance(log, dict):
        log = {}
    entry = log.get(kind)
    if not isinstance(entry, dict) or entry.get("day") != day:
        entry = {"day": day, "count": 0, "events": []}
    events = entry.get("events")
    if not isinstance(events, list):
        events = []
    for key in keys:
        entry["count"] = int(entry.get("count") or 0) + 1
        events.append({"key": key, "day": day})
    entry["events"] = events[-_SUPPRESSION_LOG_LIMIT:]
    log[kind] = entry
    state["suppressed_alerts"] = log


def claim_typed_alert(
    kind: str,
    symbols: Iterable[str],
    *,
    now: datetime | None = None,
    path: Path | None = None,
) -> list[str]:
    """Reserve today's `kind` alert for `symbols`; return those NOT yet
    alerted today, in the order given.

    Same contract as `claim_repair_failure_alert`, including that a state
    file that cannot be read errs towards telling the owner twice over not
    at all.
    """
    key = str(kind).strip() or "unspecified"
    day = repair_failure_alert_day(now)
    state = load_state(path)
    already = _typed_alerted_symbols(state, day, key)
    fresh = [
        sym
        for sym in dict.fromkeys(str(raw).strip().upper() for raw in symbols if str(raw).strip())
        if sym not in already
    ]
    stale = [
        sym for sym in dict.fromkeys(str(raw).strip().upper() for raw in symbols if str(raw).strip()) if sym in already
    ]
    if stale:
        _record_suppressed_alert(state, key, day, stale)
    if not fresh:
        if stale:
            save_state(state, path)
        return []
    state[f"typed_alerted_symbols::{key}"] = {
        "day": day,
        "symbols": sorted(already | set(fresh)),
    }
    save_state(state, path)
    return fresh


def release_typed_alert(
    kind: str,
    symbols: Iterable[str],
    *,
    now: datetime | None = None,
    path: Path | None = None,
) -> None:
    """Give back today's `kind` claim for `symbols`.

    `claim_typed_alert` reserves the symbol BEFORE the message is handed to
    the notifier, which is the right order -- two processes finding the same
    condition at the same moment must not both send. But the reservation is
    saved whether or not the send lands, so a muted or failed delivery used
    to burn the symbol's one page for the whole trading day and the owner
    was never told at all. `send_owner_alert` reports whether it landed;
    when it did not, the caller hands the claim back here so the next
    attempt -- the 30-minute watchdog, the next session entry -- can try
    again. Never raises: an alerting bug must not break the path it reports
    on. Releasing a claim that is not held is a no-op.
    """
    key = str(kind).strip() or "unspecified"
    try:
        day = repair_failure_alert_day(now)
        state = load_state(path)
        already = _typed_alerted_symbols(state, day, key)
        giving_back = {str(raw).strip().upper() for raw in symbols if str(raw).strip()}
        remaining = already - giving_back
        if remaining == already:
            return
        state[f"typed_alerted_symbols::{key}"] = {
            "day": day,
            "symbols": sorted(remaining),
        }
        save_state(state, path)
    except Exception as exc:  # noqa: BLE001
        record_watchdog_pass("release_typed_alert", exc)
        logger.warning(
            "release_typed_alert(%s) failed: %s — the claim stays held and "
            "today's page for those symbols will not be retried",
            key,
            exc,
        )
