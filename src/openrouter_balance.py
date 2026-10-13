"""OpenRouter remaining credit -- the figure src/llm_balance_runway.py times.

The only outbound GET goes through `src.cost_table.http_get_json`, the module
that already reaches openrouter.ai, so this file adds no new outbound site for
the rehearsal replay (scripts/replay_outbound_guard.py, board item 202). Under
the rehearsal wall the socket is refused and that refusal is what
fetch_openrouter_balance raises, by name. Nothing returns None for "could not
read".
"""

from __future__ import annotations

import json
import logging
import math
import os
from datetime import datetime, timezone
from pathlib import Path

from src.cost_table import http_get_json

logger = logging.getLogger(__name__)

OPENROUTER_CREDITS_URL = "https://openrouter.ai/api/v1/credits"
OPENROUTER_BALANCE_PATH = Path("data/openrouter_balance.json")


class OpenRouterBalanceUnavailable(RuntimeError):
    """The provider's remaining credit could not be read. The message names
    the endpoint and the cause, so no reader can mistake "unknown" for "fine"."""


def fetch_openrouter_balance(path: Path = OPENROUTER_BALANCE_PATH) -> dict:
    """GET /credits (total_credits - total_usage), write the snapshot, return it.

    Raises OpenRouterBalanceUnavailable -- never returns a default -- when the
    key is missing, the call fails, or the body carries no finite figure. The
    snapshot day is the EXCHANGE day (src.trading_calendar.et_today), the same
    clock `llm_budget_days` is keyed on, so later spend subtracts correctly.
    """
    from src.trading_calendar import et_today, utc_now

    key = os.environ.get("OPENROUTER_API_KEY", "")
    if not key:
        raise OpenRouterBalanceUnavailable(f"OPENROUTER_API_KEY is not set, so {OPENROUTER_CREDITS_URL} was not asked")
    try:
        data = http_get_json(OPENROUTER_CREDITS_URL, {"Authorization": f"Bearer {key}"})["data"]
        remaining = float(data["total_credits"]) - float(data["total_usage"])
    except Exception as exc:
        raise OpenRouterBalanceUnavailable(
            f"{OPENROUTER_CREDITS_URL} gave no usable balance: {type(exc).__name__}: {exc}"
        ) from exc
    if not math.isfinite(remaining):
        raise OpenRouterBalanceUnavailable(f"{OPENROUTER_CREDITS_URL} returned a non-finite balance: {remaining!r}")
    snapshot = {
        "remaining_usd": remaining,
        "as_of_day": et_today().isoformat(),
        "fetched_at": utc_now().isoformat(),
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_suffix(".json.tmp")
        tmp_path.write_text(json.dumps(snapshot))
        os.replace(str(tmp_path), str(path))
    except OSError as exc:
        raise OpenRouterBalanceUnavailable(f"balance was read but could not be written to {path}: {exc}") from exc
    return snapshot


def record_openrouter_balance(path: Path = OPENROUTER_BALANCE_PATH) -> dict | str:
    """Timer entry point (scripts/refresh_pricing.py). Returns the snapshot,
    or the NAMED reason none was taken -- logged at ERROR, never swallowed."""
    try:
        return fetch_openrouter_balance(path)
    except OpenRouterBalanceUnavailable as exc:
        logger.error(
            "OpenRouter balance NOT recorded: %s. Until a snapshot is written the "
            "dashboard shows the credit as DERIVED from the last recorded top-up, "
            "not the provider's own figure.",
            exc,
        )
        return str(exc)
