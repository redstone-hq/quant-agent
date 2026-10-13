"""How much paid-model credit is left, and is that enough for tomorrow?

On 2026-09-29 the single OpenRouter balance ran out and every paid seat was
refused at once. This module is the early warning, read by the dashboard's
/health payload and spoken by every out-of-credit message. It reuses the
desk's own spend record (`llm_budget_days`, written by the cost circuit) and
invents nothing. It makes NO network call of its own.

WHERE THE BALANCE COMES FROM, in order:
  1. `data/openrouter_balance.json` -- the provider's own figure (OpenRouter
     GET /api/v1/credits: total_credits - total_usage), written by
     `src.openrouter_balance.record_openrouter_balance()` from the twice-daily
     pricing-refresh timer. That fetch goes through cost_table, the
     module that already reaches openrouter.ai (replay-seam guard). When the
     provider refuses the call the timer logs the reason at ERROR, no file is
     written, and step 2 applies. Spend recorded after the snapshot day is
     subtracted so the figure keeps falling between refreshes.
  2. DERIVED: the recorded top-up (`llm_cost_circuit.openrouter_topup_usd`
     and `_date`) minus the desk's recorded spend from that date on.
The payload says which one it used (`source`). A snapshot file that exists
but cannot be read is reported as `unknown` naming the file -- it does not
quietly fall through to the derived figure.

THE TRIGGER IS MEASURED, NOT PICKED. One full trading day costs what the
desk's own record says it cost; the worst recorded full day is the yardstick.
The warning fires when the balance is below TWO of those: the balance seen
now must pay for the day in progress AND leave one more full session.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from datetime import datetime, timedelta, timezone

from src.trading_calendar import utc_now
from pathlib import Path

import yaml

from src.openrouter_balance import OPENROUTER_BALANCE_PATH as SNAPSHOT_PATH

logger = logging.getLogger(__name__)


def _day_costs(conn: sqlite3.Connection) -> dict[str, float]:
    return {
        r[0]: float(r[1])
        for r in conn.execute("SELECT day, baseline_cost_usd + incremental_cost_usd FROM llm_budget_days")
    }


#: The pricing-refresh timer (scripts/systemd/quant-agent-pricing-refresh.timer)
#: fires at 06:30 and 18:30 ET, so a healthy snapshot is never more than 12h
#: old. Older than that means a refresh was missed.
#: Owner ruling 2026-10-09 ("Yes, $3, and that is that."): the low-credit alert
#: fires once when the OpenRouter balance is at or below this many dollars.
LOW_CREDIT_ALERT_USD = 3.0
REFRESH_INTERVAL_HOURS = 12.0
#: Window for the average: the last N days on which the desk actually spent.
AVG_WINDOW_DAYS = 7


def _snapshot_age_hours(snapshot: dict | None, now: datetime | None) -> float | None:
    try:
        fetched = datetime.fromisoformat(str(snapshot["fetched_at"]))
        if fetched.tzinfo is None:
            fetched = fetched.replace(tzinfo=timezone.utc)
        return max((now or utc_now()) - fetched, timedelta(0)).total_seconds() / 3600
    except (KeyError, TypeError, ValueError):
        return None


def _age_text(hours: float) -> str:
    return f"{hours:.0f}h" if hours < 48 else f"{hours / 24:.0f}d"


def compute_state(
    day_costs: dict[str, float],
    *,
    snapshot: dict | None,
    topup_usd: float | None,
    topup_date: str | None,
    now: datetime | None = None,
) -> dict:
    """Pure: spend record + balance source in, plain-English state out.

    The average is over the last AVG_WINDOW_DAYS days the desk actually spent
    (zero-spend and shutdown days are skipped, so they cannot flatter the
    runway); the line says how many days that was."""
    spend_days = {d: c for d, c in day_costs.items() if c > 0}
    if not spend_days:
        return {
            "status": "unknown",
            "message": "AI credit UNKNOWN: no paid-model spend recorded yet, so the remaining credit cannot be timed.",
        }
    worst = max(spend_days.values())
    window = [spend_days[d] for d in sorted(spend_days)[-AVG_WINDOW_DAYS:]]
    mean = sum(window) / len(window)

    age_hours = None
    if snapshot and snapshot.get("remaining_usd") is not None:
        source = "provider"
        since = str(snapshot["as_of_day"])
        remaining = float(snapshot["remaining_usd"]) - sum(c for d, c in day_costs.items() if d > since)
        age_hours = _snapshot_age_hours(snapshot, now)
    elif topup_usd is not None and topup_date:
        source = "derived"
        remaining = float(topup_usd) - sum(c for d, c in day_costs.items() if d >= topup_date)
    else:
        return {
            "status": "unknown",
            "message": "AI credit UNKNOWN: the provider gave no balance and no top-up is recorded.",
        }

    remaining = max(remaining, 0.0)
    trigger = LOW_CREDIT_ALERT_USD
    days_left = remaining / mean if mean else None
    stale = source == "provider" and (age_hours is None or age_hours > REFRESH_INTERVAL_HOURS)
    if source == "derived":
        tail = " · estimated from last top-up minus recorded spend"
    elif age_hours is None:
        tail = " · balance snapshot age UNKNOWN, STALE"
    else:
        tail = f" · balance {_age_text(age_hours)} old" + (", STALE (refresh missed)" if stale else "")
    low = remaining <= trigger
    line = (
        f"AI credit: ${remaining:.2f} left · avg ${mean:.2f}/day over last {len(window)} trading days run"
        f" · ~{days_left:.0f} days{tail}"
    )
    if low:
        line = "LOW " + line + ". Top up OpenRouter now."
    return {
        "status": "low" if low else "ok",
        "source": source,
        "message": line,
        "remaining_usd": round(remaining, 2),
        "worst_day_usd": round(worst, 3),
        "mean_day_usd": round(mean, 3),
        "avg_window_days": len(window),
        "warn_below_usd": round(trigger, 2),
        "days_left": round(days_left, 1) if days_left is not None else None,
        "snapshot_age_hours": round(age_hours, 1) if age_hours is not None else None,
        "stale": stale,
    }


def read_state(db_path: str, *, topup_usd, topup_date, snapshot_path: Path = SNAPSHOT_PATH) -> dict:
    """Never raises: a failed read is `unknown`, never `ok`."""
    try:
        if snapshot_path.exists():
            try:
                snapshot = json.loads(snapshot_path.read_text())
            except (OSError, ValueError) as exc:
                return {
                    "status": "unknown",
                    "message": f"AI credit UNKNOWN: the provider balance snapshot {snapshot_path} exists but cannot be read.",
                    "error": str(exc),
                }
        else:
            snapshot = None
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            costs = _day_costs(conn)
        finally:
            conn.close()
        return compute_state(costs, snapshot=snapshot, topup_usd=topup_usd, topup_date=topup_date)
    except Exception as exc:  # noqa: BLE001
        return {"status": "unknown", "message": "AI credit UNKNOWN: the credit check could not run.", "error": str(exc)}


UNKNOWN_LINE = "AI credit UNKNOWN: the balance could not be read."


def balance_line() -> str:
    """THE one wording for the balance, called by every site that reports a
    credit refusal and by the dashboard. Unreadable reads as "balance
    unknown" -- never blank, never $0.00. Never raises."""
    try:
        # Read the YAML directly: src.config sits ABOVE this module (it pulls in
        # src.agents.base, which reports refusals through here), so importing
        # it would close an import cycle.
        raw = yaml.safe_load(Path("config/settings.yaml").read_text()) or {}
        cc = raw.get("llm_cost_circuit") or {}
        state = read_state(
            raw["storage"]["db_path"],
            topup_usd=cc.get("openrouter_topup_usd"),
            topup_date=cc.get("openrouter_topup_date"),
        )
        return state["message"]
    except Exception:  # noqa: BLE001
        return UNKNOWN_LINE


ALERT_STATE_PATH = Path("data/llm_balance_alert_state.json")


def alert_on_state_change(state: dict | None = None, *, path: Path = ALERT_STATE_PATH, send=None) -> bool:
    """Send ONE Telegram alert when the state turns low or unknown; stay quiet
    while it stays there. The last-alerted state is persisted, and cleared when
    the state recovers to ok so the next drop alerts again. Returns whether an
    alert went out. Never raises."""
    try:
        if state is None:
            raw = yaml.safe_load(Path("config/settings.yaml").read_text()) or {}
            cc = raw.get("llm_cost_circuit") or {}
            state = read_state(
                raw["storage"]["db_path"],
                topup_usd=cc.get("openrouter_topup_usd"),
                topup_date=cc.get("openrouter_topup_date"),
            )
        status = state.get("status", "unknown")
        try:
            last = json.loads(path.read_text()).get("status")
        except (OSError, ValueError, AttributeError):
            last = None
        if status == last:
            return False
        sent = False
        if status in ("low", "unknown"):
            if send is None:
                # The sender is passed in by the one send seam; importing the
                # notifier here would close an import cycle through it.
                logger.error("AI-credit alert has no sender; not recorded, retried on next send")
                return False
            sent = bool(send(state.get("message", UNKNOWN_LINE)))
            if not sent:
                return False  # not recorded: try again next morning
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"status": status}))
        return sent
    except Exception:  # noqa: BLE001
        from src.sentinel.counted import record_swallowed_here

        record_swallowed_here("llm_balance_runway.alert_on_state_change", log=logger)
        logger.exception("AI-credit state alert failed")
        return False


_checking = threading.local()


def check_balance_on_send(*, fetch=None, path: Path = ALERT_STATE_PATH, send=None) -> bool:
    """Fetch the OpenRouter balance fresh and run `alert_on_state_change` on it.

    Called from the one Telegram send seam, so the $3 alert is checked on every
    outbound message and sent once per state change. A fetch failure is logged
    with its traceback and never blocks the message. Re-entrant calls (the
    alert itself goes out through the same seam) are skipped. Never raises."""
    if getattr(_checking, "active", False):
        return False
    if fetch is None:
        from src.openrouter_balance import fetch_openrouter_balance as fetch
    _checking.active = True
    try:
        try:
            remaining = float(fetch()["remaining_usd"])
        except Exception:  # noqa: BLE001 - logged below, never blocks a send
            from src.sentinel.counted import record_swallowed_here

            record_swallowed_here("llm_balance_runway.check_balance_on_send", log=logger)
            logger.error("AI-credit balance fetch failed on send", exc_info=True)
            return False
        low = remaining <= LOW_CREDIT_ALERT_USD
        message = f"AI credit: ${remaining:.2f} left" + (". Top up OpenRouter now." if low else "")
        if low:
            message = "LOW " + message
        state = {"status": "low" if low else "ok", "message": message, "remaining_usd": round(remaining, 2)}
        return alert_on_state_change(state, path=path, send=send)
    finally:
        _checking.active = False
