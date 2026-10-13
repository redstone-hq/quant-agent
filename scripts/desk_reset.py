#!/usr/bin/env python3
"""Daily desk reset — flatten the paper book and clear contaminated history.

Deleting trading history is NOT a routine daily wipe. Since 9 Oct 2026 the
owner's ruling is to exclude history rather than delete it, so every
--execute needs the owner's explicit yes, recorded by passing
--owner-approved; without it the tool refuses. It is dry-run by default,
it refuses to touch a live account, it backs up before it deletes, and it
prints a before/after summary you can paste into a log.

    # See exactly what would happen (default — touches nothing):
    ./scripts/desk_reset.py

    # Do it:
    ./scripts/desk_reset.py --execute --owner-approved

WHAT IT DOES
    1. Proves the account is a PAPER account (four independent checks,
       see `assert_paper_account`). Any doubt aborts.
    2. Refuses to run while the market is open, or while one of the desk's
       own session windows is live (`--allow-market-open` /
       `--allow-session-window` to override, deliberately separate flags).
    3. Backs up the SQLite database and a JSON snapshot of the broker book
       to a timestamped directory. Non-negotiable; there is no --no-backup.
    4. Flattens the book: cancels every open order, liquidates every
       position (Alpaca's DELETE /v2/positions?cancel_orders=true).
    5. Clears the contaminated trading history from an explicit table
       ALLOWLIST. Never drops tables, never touches files, never touches
       anything under git, docs/, or the incident history.

WHAT IT NEVER TOUCHES
    Price/pricing caches, company profiles, news/macro/earnings/technical
    stores, checkpoints, the OpenRouter pricing cache — all of it is on
    disk, expensive to rebuild, and this tool does not delete files at all.
    It only DELETEs rows from the tables named in `TABLE_POLICY`.

    The LLM cost ledger (`agent_logs`) and the cost-circuit tables are KEPT:
    they are spend accounting, not trading decisions, and `src/token_budget.py`
    fits its per-model size estimates from `agent_logs` (MIN_SAMPLES = 8).
    Wiping them would reset the desk's spend record and its budget fits.

MARKET-HOURS TRADE-OFF (read this once)
    Alpaca queues non-extended-hours orders submitted after 16:00 ET for
    release the next trading day
    (https://docs.alpaca.markets/us/docs/orders-at-alpaca). Liquidation via
    DELETE /v2/positions is a market order, so:

      - Reset with the market CLOSED  -> the book is *scheduled* flat, not
        flat. The sells fill at the next open, i.e. right on top of the
        09:30 morning session.
      - Reset with the market OPEN    -> the fills are immediate and real,
        but the desk's own timers are live and `intra_check` covers the
        whole 09:30-16:00 session, so something can trade against you
        mid-reset.

    There is no window that is both "market open" and "no desk session
    window active". The safe procedure is therefore: stop the desk's
    timers, reset during regular hours with --allow-market-open, confirm
    flat, restart the timers. Running it after the close is supported and
    is the default-legal path, but you must keep the morning timer masked
    until the queued sells have filled. The tool prints this each run.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# --------------------------------------------------------------------------
# constants — the safety surface
# --------------------------------------------------------------------------

# alpaca-py's BaseURL.TRADING_PAPER / TRADING_LIVE. Duplicated as plain
# strings so the guardrail does not depend on the SDK enum staying put.
PAPER_HOST = "paper-api.alpaca.markets"
LIVE_HOST = "api.alpaca.markets"
LIVE_PROBE_URL = f"https://{LIVE_HOST}/v2/account"

# Alpaca paper account numbers carry a "PA" prefix. This is an OBSERVED
# convention, not a documented field: GET /v2/account has no paper/live
# boolean (verified against docs.alpaca.markets/us/reference/getaccount-1
# on 2026-09-02 — the field list has no such flag; paper and live are
# separate hosts, not an account property). The repo's own broker fixtures
# already assume this shape (tests/test_export_alpaca_trades.py: "PA9XXXXX").
# We fail CLOSED on it: an account number we cannot positively identify as
# paper stops the run.
PAPER_ACCOUNT_PREFIX = "PA"

# Rows here are the contaminated record: they were produced by, or describe,
# decisions made under the reward:risk geometry defect. All of them go.
CLEAR_TABLES = (
    "trades",  # the trade ledger itself
    "positions",  # local mirror of the book
    "daily_pnl",  # the equity curve
    "insights",  # evening lessons grading those trades
    "intraday_evaluations",  # per-symbol evaluation ledger + cooldowns
    "pending_protection_restores",  # write-ahead queue -> dead order ids
    "pending_repegs",  # write-ahead queue -> dead order ids
    "pending_stop_amends",  # write-ahead queue -> dead order ids
    "protection_restore_wal_audit",  # audit trail of the cleared restore queue
    "order_attempts",  # every order the old book tried to place
    "trade_refusals",  # decision ledger: what the old pipeline refused
    "alignment_exit_readings",  # per-symbol exit readings on the old book
    "soft_exit_heal_restores",  # exit-protection repairs on the old book
    "trend_exit_tick_checks",  # per-symbol exit state on the old book
    "realised_sector_weights",  # sector weights derived from the old book
    "realised_risk_budget",  # risk budget derived from the old book
    "evening_reports",  # evening report grading the old trades
    "session_reports",  # session report on the old book
    "intra_check_reports",  # intraday report on the old book
)

# Rows here are NOT trading decisions. Keeping them costs nothing and
# deleting them would destroy spend accounting or alerting health.
KEEP_TABLES = (
    "agent_logs",  # LLM cost + token ledger (token_budget fits from this)
    "alert_channel_checks",  # alerting health history
    "llm_budget_days",
    "llm_budget_sessions",
    "llm_budget_reservations",
    "llm_circuit_state",
    "llm_circuit_events",
    "llm_quota_holds",
    "margin_interest_daily",  # locally computed, cannot be fetched again; exclude, don't delete
    "reconciliation_runs",  # broker-vs-local comparison history; exclude, don't delete
    "llm_route_events",  # LLM routing/spend telemetry, like agent_logs
    "notifier_sends",  # delivery log of the alert channel (alerting health)
    "fred_fetch_coverage_runs",  # data-provider coverage, not a decision
    "intraday_symbol_health",  # data-feed miss counters + alert timing
    "owner_intents",  # owner instructions: not ours to delete
    "sqlite_sequence",  # sqlite-managed; handled surgically below
)

EVIDENCE_TABLE = "specialist_evidence"

# The nuanced one. The counts in `specialist_evidence` are real, but the
# PM/RM rows carry reasons derived from a reward:risk number that was half
# invented, so only what is CLEARLY worth keeping survives: paid specialist
# market observation. Everything else — decision rows, seat stances,
# pipeline telemetry — is dropped.
EVIDENCE_KEEP_KINDS = frozenset(
    {
        "analysis",  # tech / earnings / macro / news specialist output
        "finding",  # smart-money findings
        "admission",  # smart-money watchlist admissions
        "scan_summary",  # smart-money scan coverage
        "coverage",  # macro/news provider coverage records
    }
)

# Named only so the dry-run can explain WHY each is going, instead of
# lumping them into "everything else".
EVIDENCE_DROP_REASONS = {
    "target": "PM price/stop targets — the R:R geometry defect lives here",
    "proposed_order": "PM proposed orders — sized off the same R:R",
    "reasoning": "PM run-level reasoning about those orders",
    "verdict": "RM verdicts on those orders",
    "modification": "RM modifications to those orders",
    "review_metrics": "position-reviewer metrics on positions being flattened",
    "execution_skip": "execution skips for orders that no longer exist",
    "seat_stance": "per-seat stances feeding the contaminated decision",
    "nomination_summary": "pipeline nomination counts for dead runs",
    "pipeline_event": "pipeline telemetry for dead runs",
    "agent_failure": "agent failures inside dead runs",
    "rejection": "rejections recorded against dead proposals",
}

TABLE_POLICY = {t: "clear" for t in CLEAR_TABLES}
TABLE_POLICY.update({t: "keep" for t in KEEP_TABLES})
TABLE_POLICY[EVIDENCE_TABLE] = "partial"

# AUTOINCREMENT tables whose sqlite_sequence counter is reset when the table
# is fully emptied, so a fresh desk starts at id 1.
_AUTOINCREMENT_TABLES = frozenset(
    {
        "trades",
        "agent_logs",
        "specialist_evidence",
        "pending_protection_restores",
        "pending_repegs",
        "intraday_evaluations",
    }
)


class ResetRefused(RuntimeError):
    """Raised when a guardrail says no. Never caught inside this module."""


# --------------------------------------------------------------------------
# bootstrap (same shape as scripts/export_alpaca_trades.py)
# --------------------------------------------------------------------------


def _load_env_file() -> None:
    """Best-effort .env loader. Existing environment wins."""
    env_path = PROJECT_ROOT / ".env"
    if not env_path.exists():
        return
    try:
        for raw in env_path.read_text().splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            if line.startswith("export "):
                line = line[len("export ") :].lstrip()
            k, v = line.split("=", 1)
            k = k.strip()
            v = v.strip().strip('"').strip("'")
            if k and k not in os.environ:
                os.environ[k] = v
    except Exception:
        pass  # caller fails at the credential check instead


def _utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


# --------------------------------------------------------------------------
# guardrail 1 — paper account (the one that matters)
# --------------------------------------------------------------------------


def resolve_endpoint(client) -> str:
    """The base URL the SDK client will actually hit, as a plain string.

    alpaca-py stores `_base_url` as a `BaseURL` enum (a str subclass), but
    `url_override=` can put a bare string there. Both are handled; anything
    unreadable returns "" and the caller treats that as "cannot confirm".
    """
    raw = getattr(client, "_base_url", None)
    if raw is None:
        return ""
    return str(getattr(raw, "value", raw))


def probe_live_endpoint(api_key: str, secret_key: str, timeout: float = 10.0) -> dict:
    """Ask the LIVE host whether these credentials are live credentials.

    Read-only: a single GET /v2/account against api.alpaca.markets. Paper
    keys are rejected there (401/403), which is the answer we want. A 200
    means the credentials in use are LIVE credentials and the run must not
    proceed regardless of what the config says.

    Returns {"status": int|None, "authenticated": bool, "error": str|None}.
    A network failure is INCONCLUSIVE, not proof of anything — the caller
    warns and relies on the other three checks.
    """
    try:
        import requests

        resp = requests.get(
            LIVE_PROBE_URL,
            headers={
                "APCA-API-KEY-ID": api_key,
                "APCA-API-SECRET-KEY": secret_key,
            },
            timeout=timeout,
        )
        return {
            "status": resp.status_code,
            "authenticated": resp.status_code == 200,
            "error": None,
        }
    except Exception as exc:  # noqa: BLE001 — any transport failure is inconclusive
        return {"status": None, "authenticated": False, "error": str(exc)}


def assert_paper_account(
    *,
    config_paper,
    config_base_url: str,
    endpoint: str,
    account: dict | None,
    live_probe: dict | None = None,
) -> list[str]:
    """Fail closed unless every available signal says PAPER.

    Four independent signals, because any single one can be wrong or
    bypassed:

      1. `alpaca.paper` in settings.yaml is exactly True.
      2. `alpaca.base_url` in settings.yaml names the paper host.
      3. The SDK client's RESOLVED base URL is the paper host — this catches
         a `url_override=` or a client built with paper=False that the
         config never saw.
      4. The account's own number carries the paper prefix.

    Plus an optional fifth: if a live-host probe authenticated, the
    credentials are live credentials and nothing else matters.

    Returns the list of confirmations on success. Raises ResetRefused
    with every failing reason on failure.
    """
    failures: list[str] = []
    passed: list[str] = []

    if config_paper is not True:
        failures.append(
            f"settings.yaml alpaca.paper is {config_paper!r}, not True — this configuration is not a paper account"
        )
    else:
        passed.append("config alpaca.paper is True")

    host_cfg = (config_base_url or "").strip().lower()
    if not host_cfg:
        failures.append("settings.yaml alpaca.base_url is empty — cannot confirm the venue")
    elif LIVE_HOST in host_cfg and PAPER_HOST not in host_cfg:
        failures.append(f"settings.yaml alpaca.base_url points at the LIVE host: {config_base_url!r}")
    elif PAPER_HOST not in host_cfg:
        failures.append(f"settings.yaml alpaca.base_url is not the paper host {PAPER_HOST}: {config_base_url!r}")
    else:
        passed.append(f"config alpaca.base_url is {PAPER_HOST}")

    host_live = (endpoint or "").strip().lower()
    if not host_live:
        failures.append(
            "could not read the broker client's resolved base URL — refusing "
            "to guess which venue it would trade against"
        )
    elif PAPER_HOST not in host_live:
        failures.append(f"the broker client resolves to {endpoint!r}, which is NOT {PAPER_HOST} — refusing")
    else:
        passed.append(f"broker client resolves to {endpoint}")

    number = str((account or {}).get("account_number") or "").strip()
    if not number:
        failures.append(
            "the broker returned no account_number — cannot confirm this is a "
            "paper account, refusing (GET /v2/account carries no paper flag, "
            "so the account number is the only account-level signal there is)"
        )
    elif not number.upper().startswith(PAPER_ACCOUNT_PREFIX):
        failures.append(
            f"account_number {number!r} does not start with "
            f"{PAPER_ACCOUNT_PREFIX!r} — Alpaca paper accounts do; refusing "
            "rather than liquidating an account we cannot prove is paper"
        )
    else:
        passed.append(f"account_number {number} carries the paper prefix")

    if live_probe is not None:
        if live_probe.get("authenticated"):
            failures.append(
                f"these credentials AUTHENTICATED against the live host "
                f"{LIVE_HOST} (HTTP {live_probe.get('status')}) — they are LIVE "
                "credentials; refusing"
            )
        elif live_probe.get("error"):
            passed.append(
                f"live-host probe inconclusive (network: {live_probe['error'][:80]}) — relying on the checks above"
            )
        else:
            passed.append(
                f"live host {LIVE_HOST} rejected these credentials "
                f"(HTTP {live_probe.get('status')}) — they are not live keys"
            )

    if failures:
        raise ResetRefused(
            "REFUSING TO RESET — this does not look like a paper account.\n" + "\n".join(f"  - {f}" for f in failures)
        )
    return passed


# --------------------------------------------------------------------------
# guardrail 2 — market hours / desk session windows
# --------------------------------------------------------------------------


def check_trading_window(
    *,
    market_open: bool | None,
    allow_market_open: bool,
    allow_session_window: bool,
    now=None,
) -> dict:
    """Refuse to fight the market or the desk's own scheduler.

    `market_open` is the broker clock's `is_open` (None when unavailable).
    Session windows come from src.trading_calendar, the repo's single source
    of truth for "is a desk session live right now".
    """
    from src import trading_calendar as tc

    when = now if now is not None else tc.et_now()
    active = [mode for mode in tc.SESSION_WINDOWS if tc.in_session_window(mode, when)]

    refusals: list[str] = []
    warnings: list[str] = []

    if market_open is True and not allow_market_open:
        refusals.append(
            "the market is OPEN. Flattening mid-session fights the desk's own "
            "scheduler and can race a live order. Pass --allow-market-open "
            "only after stopping the desk timers."
        )
    elif market_open is None:
        warnings.append("could not read the broker clock — market-open state unknown")

    if active and not allow_session_window:
        refusals.append(
            "desk session window(s) currently active: "
            + ", ".join(f"{m} ({tc.format_window(m)})" for m in active)
            + ". The scheduler may be mid-run. Pass --allow-session-window to override."
        )

    if market_open is False:
        warnings.append(
            "the market is CLOSED: liquidating market orders are NOT eligible "
            "for extended hours and will be QUEUED for release at the next "
            "market open (docs.alpaca.markets/us/docs/orders-at-alpaca). The "
            "book will read 'flat' only after that open — keep the morning "
            "timer masked until the queued sells have filled."
        )

    return {
        "et_now": str(when),
        "market_open": market_open,
        "active_session_windows": active,
        "refusals": refusals,
        "warnings": warnings,
    }


# --------------------------------------------------------------------------
# database inspection + backup
# --------------------------------------------------------------------------


def _connect(db_path: Path, *, read_only: bool) -> sqlite3.Connection:
    if read_only:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=10.0)
    else:
        conn = sqlite3.connect(str(db_path), timeout=10.0)
    conn.row_factory = sqlite3.Row
    # Explicit transaction control: clear_database owns its BEGIN/COMMIT and
    # must not race sqlite3's implicit-transaction machinery.
    conn.isolation_level = None
    conn.execute("PRAGMA busy_timeout=10000")
    return conn


def list_tables(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").fetchall()
    return [r[0] for r in rows]


def _count(conn: sqlite3.Connection, table: str, where: str = "", params=()) -> int:
    sql = f'SELECT COUNT(*) FROM "{table}"'
    if where:
        sql += f" WHERE {where}"
    return int(conn.execute(sql, params).fetchone()[0])


def plan_database(conn: sqlite3.Connection, *, evidence_mode: str) -> dict:
    """Work out, without changing anything, exactly which rows would go.

    Tables this tool has never heard of are reported under `unknown`; the
    caller must refuse to run (see assert_all_classified).
    """
    present = list_tables(conn)
    clear: list[dict] = []
    keep: list[dict] = []
    unknown: list[dict] = []

    for table in present:
        policy = TABLE_POLICY.get(table)
        total = _count(conn, table)
        if policy == "clear":
            clear.append({"table": table, "rows": total, "deleting": total})
        elif policy == "keep":
            keep.append({"table": table, "rows": total})
        elif policy is None:
            unknown.append({"table": table, "rows": total})

    evidence = None
    if EVIDENCE_TABLE in present:
        evidence = plan_evidence(conn, evidence_mode=evidence_mode)

    return {
        "tables_present": present,
        "clear": clear,
        "keep": keep,
        "unknown": unknown,
        "evidence": evidence,
        "missing_expected": [t for t in TABLE_POLICY if t not in present and t != "sqlite_sequence"],
    }


def plan_evidence(conn: sqlite3.Connection, *, evidence_mode: str) -> dict:
    """Per-kind breakdown of specialist_evidence and what happens to it."""
    rows = conn.execute(f'SELECT kind, COUNT(*) AS n FROM "{EVIDENCE_TABLE}" GROUP BY kind ORDER BY n DESC').fetchall()
    kept, dropped = [], []
    for r in rows:
        kind, n = r["kind"], int(r["n"])
        if evidence_mode == "all":
            kept.append({"kind": kind, "rows": n, "reason": "--evidence=all"})
        elif evidence_mode == "none":
            dropped.append({"kind": kind, "rows": n, "reason": "--evidence=none"})
        elif kind in EVIDENCE_KEEP_KINDS:
            kept.append({"kind": kind, "rows": n, "reason": "paid specialist market observation"})
        else:
            dropped.append(
                {
                    "kind": kind,
                    "rows": n,
                    "reason": EVIDENCE_DROP_REASONS.get(
                        kind,
                        "UNRECOGNISED kind — dropped by the keep-list policy",
                    ),
                }
            )
    return {
        "mode": evidence_mode,
        "total": sum(int(r["n"]) for r in rows),
        "keep": kept,
        "drop": dropped,
        "keep_rows": sum(k["rows"] for k in kept),
        "drop_rows": sum(d["rows"] for d in dropped),
    }


def check_live_checkpoints(data_dir: Path, now: float | None = None) -> list[str]:
    """Warn about a post-PM checkpoint the resume lane could still act on.

    `src/decision_checkpoint.py` persists a decided-but-not-yet-risk-reviewed
    plan and will re-offer it on the next tick for MAX_AGE_MINUTES. A
    checkpoint younger than that describes the PRE-reset book, so a reset run
    inside the morning window can be followed by the desk resuming a plan for
    positions that no longer exist.

    This is a WARNING, not a deletion: the tool does not delete files, and the
    right fix is operator-side (let it expire, or remove it deliberately).
    """
    try:
        from src.decision_checkpoint import MAX_AGE_MINUTES
    except Exception:  # noqa: BLE001 — advisory only
        return []
    ckpt_dir = data_dir / "checkpoints"
    if not ckpt_dir.is_dir():
        return []
    cutoff = (now if now is not None else time.time()) - MAX_AGE_MINUTES * 60
    fresh = [p for p in sorted(ckpt_dir.glob("*.json")) if p.stat().st_mtime >= cutoff]
    if not fresh:
        return []
    return [
        f"{len(fresh)} decision checkpoint(s) younger than {MAX_AGE_MINUTES:g} min "
        f"({', '.join(p.name for p in fresh)}) — the desk's zero-LLM resume lane "
        "can still re-offer a plan built on the PRE-reset book. Let them expire "
        "or remove them before the next session; this tool does not delete files."
    ]


def backup_database(db_path: Path, dest: Path) -> dict:
    """Consistent online copy via sqlite3's backup API (WAL-safe)."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    src = _connect(db_path, read_only=True)
    try:
        out = sqlite3.connect(str(dest))
        try:
            src.backup(out)
        finally:
            out.close()
    finally:
        src.close()
    return {"path": str(dest), "bytes": dest.stat().st_size}


def assert_all_classified(plan: dict) -> None:
    """Refuse if the database holds a table this tool has no policy for.

    A new table must be classified in TABLE_POLICY before a reset may run;
    silently skipping it would leave contaminated rows (or delete valuable
    ones) without anyone deciding.
    """
    names = sorted(e["table"] for e in plan["unknown"])
    if names:
        raise ResetRefused(
            "REFUSING: table(s) not classified in TABLE_POLICY: "
            + ", ".join(names)
            + ". Add each to CLEAR_TABLES or KEEP_TABLES in scripts/desk_reset.py."
        )


def clear_database(conn: sqlite3.Connection, plan: dict, *, evidence_mode: str) -> dict:
    """Apply the plan. One transaction: all of it lands or none of it does."""
    assert_all_classified(plan)
    deleted: dict[str, int] = {}
    conn.execute("BEGIN IMMEDIATE")
    try:
        fully_emptied: list[str] = []
        for entry in plan["clear"]:
            table = entry["table"]
            cur = conn.execute(f'DELETE FROM "{table}"')
            deleted[table] = cur.rowcount if cur.rowcount is not None else entry["rows"]
            fully_emptied.append(table)

        ev = plan.get("evidence")
        if ev is not None and evidence_mode != "all":
            if evidence_mode == "none":
                cur = conn.execute(f'DELETE FROM "{EVIDENCE_TABLE}"')
                fully_emptied.append(EVIDENCE_TABLE)
            else:
                keep_kinds = sorted(EVIDENCE_KEEP_KINDS)
                placeholders = ",".join("?" for _ in keep_kinds)
                cur = conn.execute(
                    f'DELETE FROM "{EVIDENCE_TABLE}" WHERE kind NOT IN ({placeholders})',
                    keep_kinds,
                )
            deleted[EVIDENCE_TABLE] = cur.rowcount if cur.rowcount is not None else ev["drop_rows"]

        # Restart AUTOINCREMENT ids only for tables we fully emptied — a
        # partially-cleared table must keep its counter or new ids collide
        # with surviving rows.
        seq_targets = [t for t in fully_emptied if t in _AUTOINCREMENT_TABLES]
        if seq_targets and "sqlite_sequence" in plan["tables_present"]:
            placeholders = ",".join("?" for _ in seq_targets)
            conn.execute(
                f"DELETE FROM sqlite_sequence WHERE name IN ({placeholders})",
                seq_targets,
            )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return deleted


# --------------------------------------------------------------------------
# broker side
# --------------------------------------------------------------------------


def _order_summary(order) -> dict:
    def g(name, default=None):
        v = getattr(order, name, default)
        return str(getattr(v, "value", v)) if v is not None else None

    return {
        "id": g("id"),
        "symbol": g("symbol"),
        "side": g("side"),
        "type": g("order_type") or g("type"),
        "qty": g("qty"),
        "limit_price": g("limit_price"),
        "stop_price": g("stop_price"),
        "status": g("status"),
        "time_in_force": g("time_in_force"),
        "submitted_at": g("submitted_at"),
    }


def read_book(client) -> dict:
    """Read-only snapshot of positions, open orders, account and clock."""
    from alpaca.trading.enums import QueryOrderStatus
    from alpaca.trading.requests import GetOrdersRequest

    account = client.get_account()
    acct = {
        k: str(getattr(account, k, "") or "")
        for k in (
            "account_number",
            "id",
            "status",
            "cash",
            "equity",
            "portfolio_value",
            "long_market_value",
            "short_market_value",
            "buying_power",
            "non_marginable_buying_power",
            "multiplier",
            "pattern_day_trader",
        )
    }

    positions = []
    for p in client.get_all_positions():
        positions.append(
            {
                "symbol": str(p.symbol),
                "qty": str(p.qty),
                "side": str(getattr(getattr(p, "side", None), "value", getattr(p, "side", ""))),
                "avg_entry_price": str(p.avg_entry_price),
                "current_price": str(getattr(p, "current_price", "")),
                "market_value": str(p.market_value),
                "unrealized_pl": str(getattr(p, "unrealized_pl", "")),
            }
        )

    # Alpaca's default page size for GET /v2/orders is 50. The report and the
    # backup snapshot should not silently truncate at 50 open orders, so ask
    # for the maximum outright.
    orders = [
        _order_summary(o)
        for o in client.get_orders(
            filter=GetOrdersRequest(status=QueryOrderStatus.OPEN, limit=500),
        )
    ]

    market_open = None
    try:
        market_open = bool(client.get_clock().is_open)
    except Exception:
        market_open = None

    return {
        "account": acct,
        "positions": positions,
        "orders": orders,
        "market_open": market_open,
    }


def flatten_book(client, *, settle_seconds: float, poll_seconds: float = 2.0) -> dict:
    """Cancel every open order, liquidate every position.

    `close_all_positions(cancel_orders=True)` is Alpaca's
    DELETE /v2/positions?cancel_orders=true: it cancels open orders BEFORE
    liquidating, which is required — a resting protective stop reserves the
    shares and a naive sell would be rejected for insufficient quantity.

    The follow-up `cancel_orders()` sweeps anything that appeared in the
    gap (a stop that triggered while the liquidation was in flight).
    """
    result: dict = {"close_all": [], "errors": [], "cancelled_after": None}

    try:
        responses = client.close_all_positions(cancel_orders=True) or []
        for r in responses:
            # ClosePositionResponse carries order_id directly; `body` is the
            # full Order and is the fallback for older SDK shapes.
            oid = getattr(r, "order_id", None) or getattr(
                getattr(r, "body", None),
                "id",
                "",
            )
            result["close_all"].append(
                {
                    "symbol": str(getattr(r, "symbol", "")),
                    "status": str(getattr(r, "status", "")),
                    "order_id": str(oid or ""),
                }
            )
    except Exception as exc:  # noqa: BLE001
        result["errors"].append(f"close_all_positions: {exc}")

    deadline = time.monotonic() + max(0.0, settle_seconds)
    while time.monotonic() < deadline:
        try:
            if not client.get_all_positions():
                break
        except Exception:  # noqa: BLE001 — transient read, keep polling
            pass
        time.sleep(poll_seconds)

    try:
        cancelled = client.cancel_orders() or []
        result["cancelled_after"] = len(cancelled)
    except Exception as exc:  # noqa: BLE001
        result["errors"].append(f"cancel_orders: {exc}")

    return result


# --------------------------------------------------------------------------
# rendering
# --------------------------------------------------------------------------


def _rule(title: str = "") -> str:
    return f"\n{'=' * 78}\n{title}\n{'=' * 78}" if title else "=" * 78


def render_plan(book: dict, plan: dict, window: dict, paths: dict, *, execute: bool, checks: list[str]) -> str:
    out: list[str] = []
    mode = "EXECUTE" if execute else "DRY RUN — nothing will be changed"
    out.append(_rule(f"DESK RESET — {mode}"))

    out.append("\nPAPER-ACCOUNT CHECKS (all must pass)")
    for c in checks:
        out.append(f"  OK   {c}")

    out.append("\nTIMING")
    out.append(f"  ET now                : {window['et_now']}")
    out.append(f"  Market open           : {window['market_open']}")
    out.append("  Active desk windows   : " + (", ".join(window["active_session_windows"]) or "none"))
    for w in window["warnings"]:
        out.append(f"  NOTE  {w}")

    broker_skipped = not book["account"]
    acct = book["account"]
    out.append("\nACCOUNT BEFORE")
    if broker_skipped:
        out.append("  (broker not contacted — --skip-broker)")
    else:
        out.append(f"  Account number        : {acct.get('account_number')}")
        out.append(f"  Equity                : {acct.get('equity')}")
        out.append(f"  Cash                  : {acct.get('cash')}")
        out.append(f"  Long market value     : {acct.get('long_market_value')}")
        out.append(f"  Short market value    : {acct.get('short_market_value')}")

    out.append(f"\nPOSITIONS TO CLOSE ({len(book['positions'])})")
    if broker_skipped:
        out.append("  (broker not contacted — the book is NOT being flattened)")
    elif not book["positions"]:
        out.append("  (none — book already flat)")
    for p in book["positions"]:
        out.append(
            f"  {p['symbol']:<8} qty {p['qty']:>10}  @ {p['avg_entry_price']:>10}"
            f"  mkt {p['market_value']:>12}  uPL {p['unrealized_pl']:>10}"
        )

    out.append(f"\nOPEN ORDERS TO CANCEL ({len(book['orders'])})")
    if broker_skipped:
        out.append("  (broker not contacted)")
    elif not book["orders"]:
        out.append("  (none)")
    for o in book["orders"]:
        out.append(
            f"  {o['symbol']:<8} {str(o['side']):<5} {str(o['type']):<12}"
            f" qty {str(o['qty']):>8}  stop {str(o['stop_price']):>10}"
            f"  {o['status']}  [{o['id']}]"
        )

    out.append("\nDATABASE — TABLES TO CLEAR")
    for e in plan["clear"]:
        out.append(f"  CLEAR  {e['table']:<32} {e['deleting']:>8} rows")
    if not plan["clear"]:
        out.append("  (none present)")

    ev = plan.get("evidence")
    if ev:
        out.append(
            f"\nDATABASE — {EVIDENCE_TABLE} (mode: {ev['mode']}) "
            f"{ev['total']} rows -> keep {ev['keep_rows']}, delete {ev['drop_rows']}"
        )
        for k in ev["keep"]:
            out.append(f"  KEEP   {k['kind']:<24} {k['rows']:>8}  {k['reason']}")
        for d in ev["drop"]:
            out.append(f"  DELETE {d['kind']:<24} {d['rows']:>8}  {d['reason']}")

    out.append("\nDATABASE — TABLES KEPT UNTOUCHED")
    for e in plan["keep"]:
        out.append(f"  KEEP   {e['table']:<32} {e['rows']:>8} rows")
    for e in plan["unknown"]:
        out.append(
            f"  KEEP?  {e['table']:<32} {e['rows']:>8} rows  <- UNKNOWN to this tool; kept. Add it to TABLE_POLICY."
        )
    if plan["missing_expected"]:
        out.append("  NOTE   expected tables absent from this DB: " + ", ".join(plan["missing_expected"]))

    out.append("\nNOT TOUCHED AT ALL (this tool never deletes files)")
    out.append("  data/pricing_cache.json          price history cache")
    out.append("  data/openrouter_pricing_cache.json  model pricing cache")
    out.append("  data/company_profiles.json       company profiles")
    out.append("  data/news, data/macro, data/earnings, data/tech, data/smart_money")
    out.append("  data/checkpoints, data/board, docs/, and everything under git")

    out.append("\nBACKUP")
    out.append(f"  Directory             : {paths['backup_dir']}")
    out.append(f"  Database copy         : {paths['db_backup']}")
    out.append("  Book snapshot         : book_before.json / book_after.json")
    out.append("  Manifest              : reset_manifest.json")

    if not execute:
        out.append(_rule())
        out.append("DRY RUN — nothing above has happened.")
        out.append("Re-run with --execute to do it.")
    return "\n".join(out)


def render_summary(before: dict, after: dict, deleted: dict, flatten: dict, paths: dict) -> str:
    out = [_rule("AFTER")]
    a0, a1 = before["account"], after["account"]
    out.append(f"  Equity        : {a0.get('equity')}  ->  {a1.get('equity')}")
    out.append(f"  Cash          : {a0.get('cash')}  ->  {a1.get('cash')}")
    out.append(f"  Positions     : {len(before['positions'])}  ->  {len(after['positions'])}")
    out.append(f"  Open orders   : {len(before['orders'])}  ->  {len(after['orders'])}")
    if after["positions"]:
        out.append("  NOT FLAT — remaining: " + ", ".join(f"{p['symbol']}x{p['qty']}" for p in after["positions"]))
        out.append("  If the market was closed this is EXPECTED: the liquidating orders are queued for the next open.")
    if flatten.get("errors"):
        for e in flatten["errors"]:
            out.append(f"  BROKER ERROR  {e}")
    out.append("\n  Rows deleted:")
    for table, n in sorted(deleted.items()):
        out.append(f"    {table:<32} {n:>8}")
    out.append(f"\n  Backup: {paths['backup_dir']}")
    return "\n".join(out)


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="desk_reset.py",
        description="Flatten the paper book and clear contaminated history. Dry-run by default.",
    )
    p.add_argument("--config", default=str(PROJECT_ROOT / "config" / "settings.yaml"))
    p.add_argument("--db", default=None, help="Override the SQLite path (default: storage.db_path)")
    p.add_argument("--backup-root", default=None, help="Where timestamped backups go (default: <db dir>/resets)")
    p.add_argument("--execute", action="store_true", help="Actually do it. Without this the tool only prints a plan.")
    p.add_argument(
        "--owner-approved",
        action="store_true",
        help="Required with --execute: records the owner's explicit yes to deleting history.",
    )
    p.add_argument("--yes", action="store_true", help="Skip the interactive confirmation (for scripted use).")
    p.add_argument("--allow-market-open", action="store_true", help="Proceed even though the market is open.")
    p.add_argument(
        "--allow-session-window", action="store_true", help="Proceed even though a desk session window is active."
    )
    p.add_argument(
        "--evidence",
        choices=("analysis-only", "none", "all"),
        default="analysis-only",
        help="specialist_evidence policy (default: analysis-only — "
        "keep paid specialist observation, drop decision rows)",
    )
    p.add_argument("--skip-broker", action="store_true", help="Database-only reset; do not touch the broker.")
    p.add_argument("--skip-db", action="store_true", help="Broker-only flatten; leave the database alone.")
    p.add_argument("--no-live-probe", action="store_true", help="Skip the read-only live-host credential probe.")
    p.add_argument(
        "--settle-seconds", type=float, default=20.0, help="How long to wait for liquidations to fill (default 20)."
    )
    return p


def _resolve_db_path(args, cfg) -> Path:
    if args.db:
        return Path(args.db).expanduser().resolve()
    raw = Path(cfg.storage.db_path)
    return (raw if raw.is_absolute() else PROJECT_ROOT / raw).resolve()


def run(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.execute and not args.owner_approved:
        print(
            "REFUSING: --execute deletes trading history, which needs the owner's explicit yes. "
            "Re-run with --owner-approved once the owner has approved this reset.",
            file=sys.stderr,
        )
        return 6
    _load_env_file()

    # Loaded lazily: `--skip-broker --db <path>` needs neither the broker
    # config nor the API-key validation it performs, and should stay usable
    # on a machine that has only a copy of the database.
    cfg = None
    if args.db is None or not args.skip_broker:
        from src.config import load_config

        cfg = load_config(Path(args.config))

    db_path = _resolve_db_path(args, cfg)
    if not args.skip_db and not db_path.exists():
        print(f"ERROR: database not found: {db_path}", file=sys.stderr)
        return 2

    stamp = _utc_stamp()
    backup_root = Path(args.backup_root) if args.backup_root else db_path.parent / "resets"
    backup_dir = backup_root / stamp
    paths = {
        "backup_dir": str(backup_dir),
        "db_backup": str(backup_dir / db_path.name),
    }

    # ---- broker: build the client and prove it is paper BEFORE anything ----
    book = {"account": {}, "positions": [], "orders": [], "market_open": None}
    client = None
    checks: list[str] = []

    if not args.skip_broker:
        api_key = os.environ.get("ALPACA_API_KEY", "")
        secret_key = os.environ.get("ALPACA_SECRET_KEY", "")
        if not api_key or not secret_key:
            print("ERROR: ALPACA_API_KEY / ALPACA_SECRET_KEY not set", file=sys.stderr)
            return 2

        from alpaca.trading.client import TradingClient

        client = TradingClient(api_key, secret_key, paper=bool(cfg.alpaca.paper))

        book = read_book(client)
        probe = None if args.no_live_probe else probe_live_endpoint(api_key, secret_key)
        try:
            checks = assert_paper_account(
                config_paper=cfg.alpaca.paper,
                config_base_url=cfg.alpaca.base_url,
                endpoint=resolve_endpoint(client),
                account=book["account"],
                live_probe=probe,
            )
        except ResetRefused as exc:
            print(f"\n{exc}\n", file=sys.stderr)
            return 3
    else:
        checks = ["broker skipped (--skip-broker): no account to verify"]

    # ---- timing guardrail ----
    window = check_trading_window(
        market_open=book["market_open"],
        allow_market_open=args.allow_market_open,
        allow_session_window=args.allow_session_window,
    )
    window["warnings"].extend(check_live_checkpoints(db_path.parent))

    # ---- database plan ----
    plan = {"tables_present": [], "clear": [], "keep": [], "unknown": [], "evidence": None, "missing_expected": []}
    if not args.skip_db:
        conn = _connect(db_path, read_only=True)
        try:
            plan = plan_database(conn, evidence_mode=args.evidence)
        finally:
            conn.close()
        try:
            assert_all_classified(plan)
        except ResetRefused as exc:
            print(f"\n{exc}\n", file=sys.stderr)
            return 5

    print(render_plan(book, plan, window, paths, execute=args.execute, checks=checks))

    if window["refusals"]:
        # A dry run is harmless, so it always completes and simply reports
        # that the same invocation with --execute would be refused. Only the
        # real thing is blocked.
        label = "REFUSING TO RUN" if args.execute else "WOULD REFUSE (dry run)"
        sys.stdout.flush()  # keep the plan above the refusal when redirected
        print(f"\n{label}:", file=sys.stderr)
        for r in window["refusals"]:
            print(f"  - {r}", file=sys.stderr)
        if args.execute:
            return 4

    if not args.execute:
        return 0

    if not args.yes and sys.stdin.isatty():
        reply = input("\nType 'reset' to proceed: ").strip().lower()
        if reply != "reset":
            print("Aborted.")
            return 1
    elif not args.yes and not sys.stdin.isatty():
        print("ERROR: --execute without a TTY requires --yes", file=sys.stderr)
        return 2

    # ---- backup FIRST. Always. ----
    backup_dir.mkdir(parents=True, exist_ok=True)
    manifest: dict = {
        "timestamp_utc": stamp,
        "db_path": str(db_path),
        "evidence_mode": args.evidence,
        "window": window,
        "paper_checks": checks,
    }
    (backup_dir / "book_before.json").write_text(json.dumps(book, indent=2))
    if not args.skip_db:
        manifest["db_backup"] = backup_database(db_path, backup_dir / db_path.name)
        manifest["plan"] = plan
        print(f"\nBacked up database -> {manifest['db_backup']['path']} ({manifest['db_backup']['bytes']} bytes)")

    # ---- flatten ----
    flatten: dict = {"close_all": [], "errors": [], "cancelled_after": None}
    if not args.skip_broker:
        print("Flattening the book ...")
        flatten = flatten_book(client, settle_seconds=args.settle_seconds)
        manifest["flatten"] = flatten

    # ---- did the flatten actually work? ----
    # The local ledger is the only thing linking a still-open position to its
    # history. Clearing it after a flatten that did not land would leave the
    # broker holding positions nothing on this box can explain, so the clear
    # is abandoned and the operator gets told exactly why. The backup and both
    # book snapshots are already on disk either way.
    abort_reason = None
    if not args.skip_broker:
        residual = client.get_all_positions()
        if flatten["errors"]:
            abort_reason = "the broker reported errors during the flatten: " + "; ".join(flatten["errors"])
        elif residual and book["market_open"] is True:
            abort_reason = (
                f"{len(residual)} position(s) still open after "
                f"{args.settle_seconds:g}s with the market OPEN — the "
                "liquidation did not complete"
            )

    if abort_reason:
        after = read_book(client)
        (backup_dir / "book_after.json").write_text(json.dumps(after, indent=2))
        manifest["aborted"] = abort_reason
        (backup_dir / "reset_manifest.json").write_text(json.dumps(manifest, indent=2))
        print(f"\nDATABASE NOT CLEARED — {abort_reason}", file=sys.stderr)
        print(
            "  The book was NOT left clean, so the trade ledger that explains "
            "it has been kept.\n  Fix the broker side, then re-run. Backup: "
            f"{backup_dir}",
            file=sys.stderr,
        )
        return 5

    # ---- clear ----
    deleted: dict[str, int] = {}
    if not args.skip_db:
        conn = _connect(db_path, read_only=False)
        try:
            deleted = clear_database(conn, plan, evidence_mode=args.evidence)
        finally:
            conn.close()
        manifest["deleted"] = deleted
        print(f"Cleared {sum(deleted.values())} rows across {len(deleted)} tables.")

    after = read_book(client) if client is not None else book
    (backup_dir / "book_after.json").write_text(json.dumps(after, indent=2))
    manifest["book_after_summary"] = {
        "positions": len(after["positions"]),
        "orders": len(after["orders"]),
        "equity": after["account"].get("equity"),
        "cash": after["account"].get("cash"),
    }
    (backup_dir / "reset_manifest.json").write_text(json.dumps(manifest, indent=2))

    print(render_summary(book, after, deleted, flatten, paths))
    return 0


if __name__ == "__main__":
    sys.exit(run())
