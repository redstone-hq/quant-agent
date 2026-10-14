"""src.cost_circuit.free_route -- the one rule that lets a $0 route run while a money cap is tripped.

Owner ruling 2026-10-01: every position must justify its place several times a
day. The tech analyst that does that runs on a $0 free tier, so a tripped DAILY
or SESSION money cap -- which exists to stop spending money -- must not stop it.

The rule, applied identically at every authorization boundary (`require_paid_
analysis`, `begin_call`, `before_provider_attempt`):

- A call is admitted under suspension only when (1) the route it is about to
  use is priced exactly $0 in `src.cost_table.PRICING` (no new cost signal is
  invented -- the pinned free-tier row is the only source), and (2) EVERY
  active stop that applies to this run is a money cap (`daily_cost_limit` /
  `session_cost_limit`) and the hard latch is not set.
- Any other trigger -- call-count limit, provider-attempt limit, unknown cost,
  infrastructure/emergency latch, any hard latch -- still refuses everything.
  The call-count backstop is what bounds risk if the $0 price ever becomes
  wrong, so it keeps biting on every call.
- A paid route is never admitted, so a free attempt that fails over to a paid
  fallback is refused at `before_provider_attempt` and the seat is recorded
  lost (PaidAnalysisSuspended), not silently skipped.

Owner ruling 2026-10-14: the end-of-day ("close") review of held stocks runs
even after the day's AI budget is spent (12 Oct: cap tripped 13:19 ET, close
made 0 paid calls; 9/13/14 Oct the close cost $0.0072/$0.0068/$0.0034). The
sessions in `DAILY_CAP_EXEMPT_MODES` are not stopped by the DAILY cap only --
see `daily_cap_applies`. Their own session cost cap, the call-count backstop
and every hard latch still bind, which is what bounds the exemption.
"""

from __future__ import annotations

import logging
import sqlite3

logger = logging.getLogger(__name__)

MONEY_CAP_TRIGGERS = frozenset({"daily_cost_limit", "session_cost_limit"})

DAILY_CAP_EXEMPT_MODES = frozenset({"close"})


def daily_cap_applies(mode: str | None) -> bool:
    """False only for a session mode the owner exempted from the DAILY money cap."""

    return mode not in DAILY_CAP_EXEMPT_MODES


def is_free_route(model: str | None) -> bool:
    """True only when `model` has an explicit all-zero row in the price table.

    Reads `PRICING` directly (no network resolve): an unpriced or unknown model
    is treated as paid, so this fails closed.
    """

    if not model:
        return False
    try:
        from src.cost_table import PRICING

        row = PRICING.get(model)
    except Exception:  # noqa: BLE001 -- unreadable price table means "not provably free"
        logger.warning("free-route check could not read the price table for %r; treating as paid", model)
        return False
    if not row:
        return False
    try:
        return float(row["input"]) == 0.0 and float(row["output"]) == 0.0
    except (KeyError, TypeError, ValueError):
        return False


def money_cap_only_trigger(
    conn: sqlite3.Connection,
    *,
    day: str,
    run_id: str,
    mode: str,
) -> str | None:
    """Return the money-cap trigger code when the ONLY active stops are money caps.

    Returns None when nothing is suspended, when the hard latch is set, or when
    any applicable quota hold carries a non-money trigger (e.g. the call-count
    limit for this session).
    """

    hard = conn.execute("SELECT suspended FROM llm_circuit_state WHERE singleton=1").fetchone()
    if hard is None or int(hard["suspended"] or 0):
        return None
    rows = conn.execute(
        "SELECT trigger_code FROM llm_quota_holds WHERE active=1 AND day=? AND ("
        "scope='day' OR (scope='mode_day' AND scope_key=?) OR "
        "(scope='session' AND scope_key=?)) "
        "ORDER BY CASE scope WHEN 'day' THEN 1 WHEN 'mode_day' THEN 2 ELSE 3 END, id",
        (day, f"{day}:{mode}", run_id),
    ).fetchall()
    codes = [str(r["trigger_code"]) for r in rows]
    if not codes or any(code not in MONEY_CAP_TRIGGERS for code in codes):
        return None
    return codes[0]


def admit_free_route_locked(
    conn: sqlite3.Connection,
    *,
    day: str,
    run_id: str,
    mode: str,
    agent_name: str,
    model: str | None,
    boundary: str,
    log: bool = True,
) -> bool:
    """True when a suspended circuit may still admit this $0 call; logs one line when it does."""

    if not is_free_route(model):
        return False
    trigger = money_cap_only_trigger(conn, day=day, run_id=run_id, mode=mode)
    if trigger is None:
        return False
    if not log:
        return True
    logger.warning(
        "cost-circuit admitted $0 route %s for %s under %s suspension (%s)",
        model,
        agent_name,
        trigger,
        boundary,
    )
    return True
