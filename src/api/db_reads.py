"""Read-only SQLite access for the Mission Control API's history routes.

CRITICAL SAFETY INVARIANT: this module never imports or instantiates
`src.storage.db.Database` (the trading process's read/WRITE class). Instead
every function here opens its own independent connection with SQLite's
`mode=ro` URI flag — a structural, OS-enforced guarantee that even a future
bug that accidentally added an INSERT/UPDATE/DELETE call in this file would
fail loudly at the database layer rather than silently corrupt trading
state. Every query below is a plain parameterized `SELECT`; nothing here
ever calls `.commit()` or executes DDL/DML.

The trading process runs in WAL mode (`src/storage/db.py` sets
`PRAGMA journal_mode=WAL` at `initialize()`), which is exactly what makes a
second, independent read-only connection to the same file safe to use
concurrently with the writer — no locking coordination needed here.

Two functions (`session_prefixes_logged_on`, `get_recent_daily_pnl`) are a
shared interface also imported by `src.api.routes_live` (built by another
worker against these exact names/signatures) — do not rename or change
their signatures.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from src.api.deps import get_db_path
from src.api.holding_entry_evidence import entry_evidence
from src.data_paths import alerting_dir
from src.trading_calendar import et_today

ET = ZoneInfo("America/New_York")


def _connect() -> sqlite3.Connection:
    path = get_db_path()
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=2000")
    return conn


def _sqlite_utc_timestamp(when: datetime) -> str:
    """Format a datetime the same way SQLite stores `datetime('now')`.

    Mirrors `src/storage/db.py:Database._sqlite_utc_timestamp` exactly —
    trades/agent_logs timestamps are naive UTC strings, so converting ET
    day boundaries into this format is what lets an ET-day filter work
    regardless of host timezone.
    """
    from datetime import UTC

    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return when.astimezone(UTC).replace(tzinfo=None).strftime("%Y-%m-%d %H:%M:%S")


def _et_day_utc_bounds(trading_day: date | None = None) -> tuple[str, str]:
    """UTC timestamp bounds [start, end) for an ET trading-day date.

    Mirrors `src/storage/db.py:Database._et_day_utc_bounds` exactly.
    """
    day = trading_day or et_today()
    start_et = datetime.combine(day, time.min, tzinfo=ET)
    end_et = start_et + timedelta(days=1)
    return _sqlite_utc_timestamp(start_et), _sqlite_utc_timestamp(end_et)


def _executed_trade_predicate() -> str:
    """SQL predicate for trades that executed at least some quantity.

    Mirrors `src/storage/db.py:Database._executed_trade_predicate` exactly.
    """
    return "((fill_status IS NULL AND action != 'HOLD') OR fill_status = 'filled' OR COALESCE(fill_qty, 0) > 0)"


def is_executed_trade(row: dict) -> bool:
    """Python-side equivalent of `_executed_trade_predicate()` for callers
    that already have a trades row in hand (e.g. `get_run_funnel`'s
    route-layer grouping) rather than filtering via SQL. Keep the two
    definitions in sync — this mirrors the SQL predicate exactly."""
    fill_status = row.get("fill_status")
    action = row.get("action")
    fill_qty = row.get("fill_qty") or 0
    return (fill_status is None and action != "HOLD") or fill_status == "filled" or fill_qty > 0


def get_alert_channel_health() -> dict:
    """Can this desk still reach the operator? — read-only.

    Delegates to `src.alert_watchdog.read_health`, which opens its own
    `mode=ro` connection exactly like `_connect()` above. One implementation
    of "what does the check history say", shared by the sessions that write
    it and the API that renders it; a second copy here would be free to
    disagree with the writer and report health that nothing measured.

    Never raises: a database that predates the `alert_channel_checks` table,
    or no config at all, degrades to `status: unknown` — explicitly not `ok`.
    """
    try:
        from src.alert_watchdog import read_health

        return read_health(get_db_path()).to_dict()
    except Exception as exc:  # noqa: BLE001
        from src.alert_watchdog import AlertChannelHealth

        return AlertChannelHealth("unknown", error=str(exc)).to_dict()


def get_llm_circuit_health() -> dict:
    """Read decision-path/cost-circuit health without acquiring write access."""

    conn = None
    try:
        conn = _connect()
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        result = {
            "available": "llm_circuit_state" in tables,
            "suspended": None,
            "suspension_class": None,
            "hold_scope": None,
            "requires_operator_reset": False,
            "auto_rearm": False,
            "trigger": None,
            "trigger_code": None,
            "suspended_at": None,
            "daily_cost_usd": None,
            "daily_limit_usd": None,
            "active_quota_holds": [],
            "recent_recovery": None,
            "recent_pm_status": None,
            "recent_pm_run_id": None,
            "recent_pm_timestamp": None,
            "recent_agent_statuses": {},
        }
        if "llm_circuit_state" in tables:
            state = conn.execute(
                "SELECT suspended, trigger_code, trigger_detail, suspended_at, "
                "daily_limit_usd "
                "FROM llm_circuit_state WHERE singleton=1"
            ).fetchone()
            if state:
                result.update(
                    available=True,
                    suspended=bool(state["suspended"]),
                    suspension_class="hard" if state["suspended"] else None,
                    hold_scope="global" if state["suspended"] else None,
                    requires_operator_reset=bool(state["suspended"]),
                    trigger=state["trigger_detail"],
                    trigger_code=state["trigger_code"],
                    suspended_at=state["suspended_at"],
                    daily_limit_usd=state["daily_limit_usd"],
                )
            else:
                result["available"] = False
        if "llm_quota_holds" in tables:
            current_day = et_today().isoformat()
            holds = [
                dict(row)
                for row in conn.execute(
                    "SELECT scope, day, trigger_code, trigger_detail, run_id, mode, "
                    "agent_name, session_cost_usd, daily_cost_usd, daily_limit_usd, "
                    "created_at FROM llm_quota_holds WHERE active=1 AND day=? "
                    "ORDER BY CASE scope WHEN 'day' THEN 1 WHEN 'mode_day' THEN 2 "
                    "ELSE 3 END, id",
                    (current_day,),
                ).fetchall()
            ]
            result["active_quota_holds"] = holds
            global_day_hold = next(
                (hold for hold in holds if hold["scope"] == "day"),
                None,
            )
            representative = global_day_hold or (holds[0] if holds else None)
            if not result["suspended"] and representative is not None:
                result.update(
                    suspended=bool(global_day_hold),
                    suspension_class="quota",
                    hold_scope=representative["scope"],
                    requires_operator_reset=False,
                    auto_rearm=representative["scope"] in {"day", "mode_day"},
                    trigger=representative["trigger_detail"],
                    trigger_code=representative["trigger_code"],
                    suspended_at=representative["created_at"],
                    daily_limit_usd=(
                        representative["daily_limit_usd"]
                        if representative["daily_limit_usd"] is not None
                        else result["daily_limit_usd"]
                    ),
                )
            recovery = conn.execute(
                "SELECT scope, day, trigger_code, mode, released_at, release_reason "
                "FROM llm_quota_holds WHERE active=0 AND released_at IS NOT NULL "
                "ORDER BY released_at DESC, id DESC LIMIT 1"
            ).fetchone()
            if recovery is not None:
                result["recent_recovery"] = dict(recovery)
        # Accounting failures use an atomic sidecar because SQLite itself may
        # be the failed component.  The API is a separate process, so reading
        # only llm_circuit_state could otherwise report OK while every trading
        # process is correctly fail-closed on this durable marker.
        db_file = Path(get_db_path())
        emergency_latch = db_file.with_name(f"{db_file.name}.llm-circuit-unavailable")
        if emergency_latch.exists():
            try:
                payload = json.loads(emergency_latch.read_text(encoding="utf-8"))
                detail = str(payload.get("error") or "persistent accounting failure")
            except Exception as exc:
                detail = f"durable accounting-failure latch is unreadable: {type(exc).__name__}: {str(exc)[:200]}"
            result.update(
                available=False,
                suspended=True,
                suspension_class="hard",
                hold_scope="global",
                requires_operator_reset=True,
                auto_rearm=False,
                trigger_code="circuit_infrastructure_unavailable",
                trigger=f"cost-circuit infrastructure unavailable: {detail}",
            )
        if "llm_budget_days" in tables:
            row = conn.execute(
                "SELECT baseline_cost_usd + incremental_cost_usd AS cost FROM llm_budget_days WHERE day=?",
                (et_today().isoformat(),),
            ).fetchone()
            if row:
                result["daily_cost_usd"] = row["cost"]
        if "agent_logs" in tables:
            utc_start, utc_end = _et_day_utc_bounds()
            columns = {row[1] for row in conn.execute("PRAGMA table_info(agent_logs)").fetchall()}
            status_expr = "status" if "status" in columns else "NULL"
            row = conn.execute(
                f"SELECT run_id, timestamp, {status_expr} AS status "
                "FROM agent_logs WHERE agent_name='portfolio_manager' "
                "AND timestamp >= ? AND timestamp < ? "
                "ORDER BY timestamp DESC, id DESC LIMIT 1",
                (utc_start, utc_end),
            ).fetchone()
            if row:
                result.update(
                    recent_pm_status=row["status"],
                    recent_pm_run_id=row["run_id"],
                    recent_pm_timestamp=row["timestamp"],
                )
            for agent_name in (
                "portfolio_manager",
                "risk_manager",
                "position_reviewer",
                "evening_analyst",
            ):
                latest = conn.execute(
                    f"SELECT run_id, timestamp, {status_expr} AS status "
                    "FROM agent_logs WHERE agent_name=? "
                    "AND timestamp >= ? AND timestamp < ? "
                    "ORDER BY timestamp DESC, id DESC LIMIT 1",
                    (agent_name, utc_start, utc_end),
                ).fetchone()
                if latest:
                    result["recent_agent_statuses"][agent_name] = {
                        "status": latest["status"],
                        "run_id": latest["run_id"],
                        "timestamp": latest["timestamp"],
                    }
        return result
    finally:
        if conn is not None:
            conn.close()


def get_trades(
    symbol: str | None = None,
    run_id: str | None = None,
    decision_id: str | None = None,
    today_only: bool = False,
    executed_only: bool = False,
    limit: int = 100,
) -> list[dict]:
    """SELECT * FROM trades with optional AND-ed filters, newest first.

    `today_only` is implemented via the same ET-day-to-UTC-bounds math as
    `Database.get_trades` (correct, not skipped) — see `_et_day_utc_bounds`.
    """
    conditions: list[str] = []
    params: list = []
    if symbol:
        conditions.append("symbol = ?")
        params.append(symbol)
    if run_id:
        conditions.append("run_id = ?")
        params.append(run_id)
    if decision_id:
        conditions.append("decision_id = ?")
        params.append(decision_id)
    if today_only:
        start_utc, end_utc = _et_day_utc_bounds()
        conditions.append("timestamp >= ? AND timestamp < ?")
        params.extend([start_utc, end_utc])
    if executed_only:
        conditions.append(_executed_trade_predicate())
    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    conn = None
    try:
        conn = _connect()
        rows = conn.execute(
            f"SELECT * FROM trades {where} ORDER BY timestamp DESC, id DESC LIMIT ?",
            (*params, limit),
        ).fetchall()
        return [dict(row) for row in rows]
    except sqlite3.Error:
        return []
    finally:
        if conn is not None:
            conn.close()


#: Mirrors `_POSITION_OPEN_ACTIONS` / `_POSITION_EXIT_ACTIONS` /
#: `_POSITION_EXIT_PREFIXES` in src/storage/db.py exactly — see that module
#: for why each entry is here (SWEEP_BUY/SWEEP_SELL are deliberately absent,
#: the cash-sweep vehicle has no thesis to chain a position around; the COVER
#: family joined on 2026-08-31 when shorts started chaining exactly as longs
#: do). Duplicated rather than imported: this file must never import the
#: write-capable `Database` class (see module docstring and
#: tests/test_api_safety.py) — position_id ITSELF is read straight off the
#: row (already persisted by the writer), this vocabulary is only needed here
#: to work out whether a chain has gone flat yet.
_POSITION_OPEN_ACTIONS = frozenset({"BUY", "SHORT"})
_POSITION_EXIT_ACTIONS = frozenset(
    {
        "EMERGENCY_SELL",
        "EMERGENCY_COVER",
        "FORCE_DELEVER",
        "REDUCE",
        "TAKE_PROFIT",
        "STOP_OUT",
        "TRAIL_STOP",
        # RECONCILED_EXIT (item 173(a)): a broker-side exit the reconciler wrote
        # back but could not prove was a protective stop. The writer counts it as
        # a real closed lot (_EITHER_SIDE_EXIT_ACTIONS in src/storage/db.py), so
        # the reader MUST subtract it too — otherwise a position the broker
        # actually closed reports "open" forever in get_position_history.
        "RECONCILED_EXIT",
    }
)
_POSITION_EXIT_PREFIXES = ("SELL", "PARTIAL_SELL", "COVER", "PARTIAL_COVER")


def get_position_history(position_id: str) -> dict | None:
    """Ordered chain of every trade sharing one `position_id`: the entry
    (first row — a BUY or a SHORT, carrying the thesis reasoning and stop),
    each
    interim review decision (everything between entry and exit — a
    TRAIL_STOP adjustment, a partial REDUCE/TAKE_PROFIT trim, each with its
    own `reasoning`), the exit (final row, only once the chain has actually
    gone flat), realized P&L, and hold duration.

    No new writes: every field here already lives on the `trades` rows this
    reads (see `Database.insert_trade` / `_assign_position_ids` in
    src/storage/db.py, which minted/inherited `position_id` and stamped
    `realized_pnl` at write time) — this function only orders and sums them.

    Returns None when no trade carries this position_id — the route turns
    that into a 404 rather than a fabricated empty chain.
    """
    conn = None
    try:
        conn = _connect()
        rows = conn.execute(
            "SELECT * FROM trades WHERE position_id = ? ORDER BY timestamp, id",
            (position_id,),
        ).fetchall()
        if not rows:
            return None
        rows = [dict(r) for r in rows]
        entry = rows[0]

        # Same net-qty accounting `_assign_position_ids` uses to decide when
        # a chain closes (an entry — BUY or SHORT — adds executed qty, a
        # recognized exit-family action subtracts it; a TRAIL_STOP only
        # counts once actually FILLED) — reused here only to answer "is this
        # chain flat yet", not to re-derive any id. A short's qty counts up
        # and down exactly as a long's does; direction is never negated here.
        net = 0.0
        for r in rows:
            if not is_executed_trade(r):
                continue
            action = (r.get("action") or "").upper()
            qty = float(r.get("fill_qty") or r.get("qty") or 0)
            if action in _POSITION_OPEN_ACTIONS:
                net += qty
                continue
            if action not in _POSITION_EXIT_ACTIONS and not action.startswith(_POSITION_EXIT_PREFIXES):
                continue
            if action == "TRAIL_STOP":
                status = str(r.get("fill_status") or "").lower()
                filled = status == "filled" or (status == "" and float(r.get("fill_qty") or 0) > 0)
                if not filled:
                    continue
            net -= qty
        is_closed = net <= 1e-6

        exit_row = rows[-1] if (is_closed and len(rows) > 1) else None
        interim = rows[1:-1] if exit_row is not None else rows[1:]

        exit_family_rows = [r for r in rows[1:] if (r.get("action") or "").upper() not in _POSITION_OPEN_ACTIONS]
        priced = [r for r in exit_family_rows if r.get("realized_pnl") is not None]
        realized_total = sum(r["realized_pnl"] for r in priced) if priced else None
        unpriced_executed = any(r.get("realized_pnl") is None and is_executed_trade(r) for r in exit_family_rows)
        realized_partial = bool(priced) and unpriced_executed

        hold_days = None
        try:
            end_row = exit_row if exit_row is not None else rows[-1]
            start = datetime.fromisoformat(str(entry["timestamp"]).replace(" ", "T"))
            end = datetime.fromisoformat(str(end_row["timestamp"]).replace(" ", "T"))
            hold_days = round((end - start).total_seconds() / 86400.0, 2)
        except (TypeError, ValueError, KeyError):
            hold_days = None

        return {
            "position_id": position_id,
            "symbol": entry.get("symbol"),
            "status": "closed" if exit_row is not None else "open",
            "entry": entry,
            "interim": interim,
            "exit": exit_row,
            "realized_pnl_total": realized_total,
            "realized_pnl_partial": realized_partial,
            "hold_days": hold_days,
            "trade_count": len(rows),
        }
    except sqlite3.Error:
        return None
    finally:
        if conn is not None:
            conn.close()


def _cost_rows_to_total(rows) -> float | None:
    """Any-null-means-None convention, matching `Database.sum_session_cost`."""
    if not rows:
        return None
    if any(r["cost_usd"] is None for r in rows):
        return None
    return sum(float(r["cost_usd"]) for r in rows)


def _canonical_run_cost(conn: sqlite3.Connection, run_id: str, agent_cost_rows) -> float | None:
    """Prefer the exact mandatory-circuit ledger over agent-log attribution.

    A paid call can settle successfully and then the enclosing stage can stop
    before its merged ``agent_logs`` row is written.  Summing those rows would
    report a precise-looking partial cost.  The circuit session is the shared
    accounting authority and already includes every settled provider call.
    Legacy databases/runs without that row retain the prior agent-log behavior.
    """
    try:
        row = conn.execute(
            "SELECT actual_cost_usd, costs_exact FROM llm_budget_sessions WHERE run_id = ?",
            (run_id,),
        ).fetchone()
    except sqlite3.OperationalError:
        row = None
    if row is None:
        return _cost_rows_to_total(agent_cost_rows)
    if not bool(row["costs_exact"]):
        return None
    return float(row["actual_cost_usd"])


def get_recent_runs(limit: int = 20) -> list[dict]:
    """Per-run_id summary from agent_logs, newest-last-activity first.

    total_cost_usd follows Database.sum_session_cost's any-unknown-means-
    unknown convention: if any agent_logs row for that run_id has a NULL
    cost_usd, the reported total for that run is None (never a misleading
    partial sum).
    """
    conn = None
    try:
        conn = _connect()
        rows = conn.execute(
            "SELECT run_id, MIN(timestamp) as first_timestamp, "
            "MAX(timestamp) as last_timestamp, COUNT(*) as agent_count, "
            "MAX(decision_id) as decision_id FROM agent_logs "
            "GROUP BY run_id ORDER BY MAX(timestamp) DESC LIMIT ?",
            (limit,),
        ).fetchall()
        out: list[dict] = []
        for row in rows:
            d = dict(row)
            run_id = d["run_id"] or ""
            d["session_prefix"] = run_id.rsplit("-", 1)[0] if "-" in run_id else run_id
            cost_rows = conn.execute(
                "SELECT cost_usd FROM agent_logs WHERE run_id = ?",
                (d["run_id"],),
            ).fetchall()
            d["total_cost_usd"] = _canonical_run_cost(
                conn,
                d["run_id"],
                cost_rows,
            )
            out.append(d)
        return out
    except sqlite3.Error:
        return []
    finally:
        if conn is not None:
            conn.close()


def get_run_detail(run_id: str) -> dict:
    """All agent_logs + trades rows for one run_id, plus derived summary
    fields. Returns empty lists / None fields (not an exception) when the
    run_id matches nothing — the route layer decides whether that's a 404.
    """
    conn = None
    try:
        conn = _connect()
        agent_logs = [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM agent_logs WHERE run_id = ? ORDER BY timestamp",
                (run_id,),
            ).fetchall()
        ]
        trades = [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM trades WHERE run_id = ? ORDER BY timestamp",
                (run_id,),
            ).fetchall()
        ]
        decision_id = None
        for log in agent_logs:
            if log.get("decision_id"):
                decision_id = log["decision_id"]
                break
        cost_rows = conn.execute(
            "SELECT cost_usd FROM agent_logs WHERE run_id = ?",
            (run_id,),
        ).fetchall()
        total_cost_usd = _canonical_run_cost(conn, run_id, cost_rows)
        return {
            "agent_logs": agent_logs,
            "trades": trades,
            "decision_id": decision_id,
            "total_cost_usd": total_cost_usd,
        }
    except sqlite3.Error:
        return {"agent_logs": [], "trades": [], "decision_id": None, "total_cost_usd": None}
    finally:
        if conn is not None:
            conn.close()


def get_decision_detail(decision_id: str) -> dict:
    """The PM/RM agent_logs rows and all trades tied to one decision_id.

    `hard_risk_block` (Stage 2 Checkpoint C) is the forensic `agent_logs`
    row `TradingPipeline._persist_hard_risk_block` writes with the
    sentinel `agent_name="risk_gate"` when the deterministic hard-risk
    gate blocked every candidate before `risk_manager` was ever called —
    `risk_manager` stays `None` in exactly that case, since no LLM call
    happened. It is `None` for every ordinary (RM-reached) decision.
    """
    conn = None
    try:
        conn = _connect()
        pm_row = conn.execute(
            "SELECT * FROM agent_logs WHERE decision_id = ? AND agent_name = 'portfolio_manager' LIMIT 1",
            (decision_id,),
        ).fetchone()
        rm_row = conn.execute(
            "SELECT * FROM agent_logs WHERE decision_id = ? AND agent_name = 'risk_manager' LIMIT 1",
            (decision_id,),
        ).fetchone()
        hard_risk_block_row = conn.execute(
            "SELECT * FROM agent_logs WHERE decision_id = ? AND agent_name = 'risk_gate' LIMIT 1",
            (decision_id,),
        ).fetchone()
        trades = [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM trades WHERE decision_id = ? ORDER BY timestamp",
                (decision_id,),
            ).fetchall()
        ]
        return {
            "portfolio_manager": dict(pm_row) if pm_row is not None else None,
            "risk_manager": dict(rm_row) if rm_row is not None else None,
            "hard_risk_block": dict(hard_risk_block_row) if hard_risk_block_row is not None else None,
            "trades": trades,
        }
    except sqlite3.Error:
        return {
            "portfolio_manager": None,
            "risk_manager": None,
            "hard_risk_block": None,
            "trades": [],
        }
    finally:
        if conn is not None:
            conn.close()


def get_recent_agent_logs(agent_name: str, limit: int = 20) -> list[dict]:
    conn = None
    try:
        conn = _connect()
        rows = conn.execute(
            "SELECT * FROM agent_logs WHERE agent_name = ? ORDER BY timestamp DESC LIMIT ?",
            (agent_name, limit),
        ).fetchall()
        return [dict(row) for row in rows]
    except sqlite3.Error:
        return []
    finally:
        if conn is not None:
            conn.close()


def get_recent_insights(limit: int = 7) -> list[dict]:
    conn = None
    try:
        conn = _connect()
        rows = conn.execute(
            "SELECT * FROM insights ORDER BY date DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(row) for row in rows]
    except sqlite3.Error:
        return []
    finally:
        if conn is not None:
            conn.close()


def get_watchlist_candidates(lookback_days: int = 30) -> list[dict]:
    """Symbols the evening analyst has repeatedly flagged as `add`/`watch`
    for universe expansion — same canonical output as
    `TradingPipeline._build_watchlist_candidates` (Stage 2 Checkpoint C
    candidates gap).

    Computed from the read-only `get_recent_insights` query above plus the
    pure aggregator in `src.watchlist_candidates` — `TradingPipeline`/
    trading-execution code is never imported into `src/api`.
    """
    rows = get_recent_insights(limit=lookback_days + 5)
    if not rows:
        return []
    from src.watchlist_candidates import build_watchlist_candidates

    return build_watchlist_candidates(rows, lookback_days)


def get_recent_daily_pnl(limit: int = 30) -> list[dict]:
    """SELECT * FROM daily_pnl ORDER BY date DESC LIMIT ?.

    Shared name/signature — imported directly by `src.api.routes_live`.
    """
    conn = None
    try:
        conn = _connect()
        rows = conn.execute(
            "SELECT * FROM daily_pnl ORDER BY date DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(row) for row in rows]
    except sqlite3.Error:
        return []
    finally:
        if conn is not None:
            conn.close()


def get_earliest_daily_pnl() -> dict | None:
    """SELECT * FROM daily_pnl ORDER BY date ASC LIMIT 1 — the oldest row
    this table actually has.

    Same query and same "never reconstructed from an archive" posture as
    `Database.get_earliest_daily_pnl` (src/storage/db.py), which the
    Telegram feed's own "Total P&L since <date>" line already uses
    (`TradingPipeline._total_pnl_since_reset`). Duplicated here rather than
    imported — `src/api` is forbidden by a ratified structural guardrail
    (tests/test_api_safety.py) from importing the trading/risk stack — the
    same reason `get_recent_daily_pnl` above re-states its own SQL instead
    of calling the Database class.

    Used by `routes_live.get_account` for the dashboard's own total-P&L
    figure, read-only, on the same `mode=ro` connection as every other
    query in this module. Returns `None` on any read failure — never a
    fabricated baseline.
    """
    conn = None
    try:
        conn = _connect()
        row = conn.execute(
            "SELECT * FROM daily_pnl ORDER BY date ASC LIMIT 1",
        ).fetchone()
        return dict(row) if row else None
    except sqlite3.Error:
        return None
    finally:
        if conn is not None:
            conn.close()


def get_margin_interest_daily_all() -> list[dict]:
    """`SELECT * FROM margin_interest_daily ORDER BY date ASC` — every
    persisted daily-accrual ESTIMATE row, oldest first. The bucketing in
    `src.margin_interest.bucket_estimate_rows` does its own windowing (this
    week / current month / up to 6 months / all-time), so this returns the
    full table rather than filtering here — the table only ever holds rows
    from the day this tracker started persisting forward, never more than a
    cockpit poll needs. Returns `[]` on any read failure, including the
    table not existing yet (a fresh deploy before the tracker's first
    morning run) — that degrades to `source="no_data"` in
    `bucket_estimate_rows`, never a fabricated zero.
    """
    conn = None
    try:
        conn = _connect()
        rows = conn.execute(
            "SELECT date, debit_balance, rate_pct, daily_usd, days_charged, "
            "period_usd, source FROM margin_interest_daily ORDER BY date ASC",
        ).fetchall()
        return [dict(row) for row in rows]
    except sqlite3.Error:
        return []
    finally:
        if conn is not None:
            conn.close()


def session_prefixes_logged_on() -> list[str]:
    """Distinct run_id prefixes (e.g. "run", "midday") logged in agent_logs
    today (ET trading day). Mirrors `Database.session_prefixes_logged_on`'s
    default-today behavior.

    Shared name — imported directly by `src.api.routes_live`'s /health
    route, which calls this expecting it may legitimately return empty.
    MUST NEVER RAISE: every failure mode collapses to `[]`.
    """
    try:
        start_utc, end_utc = _et_day_utc_bounds()
        conn = _connect()
        try:
            rows = conn.execute(
                "SELECT DISTINCT run_id FROM agent_logs WHERE timestamp >= ? AND timestamp < ?",
                (start_utc, end_utc),
            ).fetchall()
        finally:
            conn.close()
        prefixes: set[str] = set()
        for r in rows:
            rid = r[0] or ""
            prefixes.add(rid.rsplit("-", 1)[0] if "-" in rid else rid)
        return sorted(prefixes)
    except Exception:
        return []


def get_specialist_evidence(
    run_id: str | None = None,
    decision_id: str | None = None,
    symbol: str | None = None,
    scope: str | None = None,
    agent_name: str | None = None,
    kind: str | None = None,
) -> list[dict]:
    """SELECT * FROM specialist_evidence with optional AND-ed filters
    (Stage 4). Same shape/limits-free convention as get_trades — callers
    scope with run_id/symbol so result sets stay small (one run's worth
    of specialist calls, at most tens of rows)."""
    conditions: list[str] = []
    params: list = []
    if run_id:
        conditions.append("run_id = ?")
        params.append(run_id)
    if decision_id:
        conditions.append("decision_id = ?")
        params.append(decision_id)
    if symbol:
        conditions.append("symbol = ?")
        params.append(symbol)
    if scope:
        conditions.append("scope = ?")
        params.append(scope)
    if agent_name:
        conditions.append("agent_name = ?")
        params.append(agent_name)
    if kind:
        conditions.append("kind = ?")
        params.append(kind)
    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    conn = None
    try:
        conn = _connect()
        rows = conn.execute(
            f"SELECT * FROM specialist_evidence {where} ORDER BY id",
            params,
        ).fetchall()
        return [dict(row) for row in rows]
    except sqlite3.Error:
        return []
    finally:
        if conn is not None:
            conn.close()


def get_run_candidates(run_id: str) -> list[str]:
    """Distinct symbols "considered" in a run (Stage 4 per-candidate view):
    the union of every symbol-scoped `specialist_evidence` row and every
    `trades` row for this run_id. Natural-scope union, not a re-derived
    universe/watchlist concept (see get_watchlist_candidates, a distinct
    Stage 2 feature over `insights`, not this table)."""
    conn = None
    try:
        conn = _connect()
        ev_rows = conn.execute(
            "SELECT DISTINCT symbol FROM specialist_evidence "
            "WHERE run_id = ? AND scope = 'symbol' AND symbol IS NOT NULL",
            (run_id,),
        ).fetchall()
        trade_rows = conn.execute(
            "SELECT DISTINCT symbol FROM trades WHERE run_id = ?",
            (run_id,),
        ).fetchall()
        symbols = {r[0] for r in ev_rows} | {r[0] for r in trade_rows}
        return sorted(symbols)
    except sqlite3.Error:
        return []
    finally:
        if conn is not None:
            conn.close()


def get_run_funnel(run_id: str) -> dict:
    """Raw rows for Stage 6's decision-funnel aggregation: every
    `specialist_evidence` row for this run (both symbol- and run-scoped —
    the route layer separates them), every `trades` row, whether the
    forensic `agent_name="risk_gate"` hard-risk-block row is present (same
    sentinel as `RunDetailResponse.hard_risk_block_recorded`), and the
    run's first `agent_logs` timestamp. Pure read aggregation over
    existing Stage-4 tables — the route layer (`routes_evidence.py`) does
    all typed validation/grouping, this function only fetches."""
    conn = None
    try:
        conn = _connect()
        evidence_rows = [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM specialist_evidence WHERE run_id = ? ORDER BY id",
                (run_id,),
            ).fetchall()
        ]
        trade_rows = [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM trades WHERE run_id = ? ORDER BY timestamp",
                (run_id,),
            ).fetchall()
        ]
        hard_risk_block = (
            conn.execute(
                "SELECT 1 FROM agent_logs WHERE run_id = ? AND agent_name = 'risk_gate' LIMIT 1",
                (run_id,),
            ).fetchone()
            is not None
        )
        ts_row = conn.execute(
            "SELECT MIN(timestamp) as first_ts FROM agent_logs WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        first_timestamp = ts_row["first_ts"] if ts_row else None
        # A hard-risk-block run (blocked before risk_manager ever ran) can
        # legitimately have zero specialist_evidence rows and zero trades,
        # and conversely a run can have specialist_evidence with no
        # agent_logs row pinned to that exact run_id in a test fixture —
        # "run exists at all" must come from any of the three tables, never
        # from having candidates, or the exact "why no trade?" state this
        # endpoint exists to surface would 404 instead of rendering.
        run_exists = bool(evidence_rows) or bool(trade_rows) or first_timestamp is not None
        return {
            "specialist_evidence": evidence_rows,
            "trades": trade_rows,
            "hard_risk_block": hard_risk_block,
            "first_timestamp": first_timestamp,
            "run_exists": run_exists,
        }
    except sqlite3.Error:
        return {
            "specialist_evidence": [],
            "trades": [],
            "hard_risk_block": False,
            "first_timestamp": None,
            "run_exists": False,
        }
    finally:
        if conn is not None:
            conn.close()


def get_journal_dates(limit: int = 60) -> list[str]:
    """ET trading-day dates with any journal-worthy activity (Stage 5),
    newest first: the union of `insights.date` (evening reflection ran),
    `daily_pnl.date` (an equity snapshot was recorded) — both already
    ET-day-keyed strings (see `Database.save_evening_snapshot` /
    `session_date_key`), so no UTC->ET conversion is needed for those two —
    and every ET calendar date that has at least one recorded run
    (`agent_logs`, converted from its naive-UTC `timestamp`).

    The run-derived third source is not optional (2026-08-21 Mission
    Control correctness finding): a day with real runs/candidates but
    neither an evening reflection nor an equity snapshot yet was
    previously invisible here even though `/journal/{date}` already
    rendered it correctly once selected directly — a trading run must
    stay discoverable in history regardless of whether evening reflection
    or the daily equity snapshot ran."""
    conn = None
    try:
        conn = _connect()
        dates: set[str] = set()
        rows = conn.execute("SELECT date FROM insights UNION SELECT date FROM daily_pnl").fetchall()
        dates.update(r[0] for r in rows if r[0])

        run_rows = conn.execute(
            "SELECT MIN(timestamp) as first_ts FROM agent_logs WHERE run_id IS NOT NULL GROUP BY run_id"
        ).fetchall()
        for r in run_rows:
            ts = r["first_ts"]
            if not ts:
                continue
            try:
                naive_utc = datetime.strptime(ts, "%Y-%m-%d %H:%M:%S")
            except ValueError:
                continue
            dates.add(naive_utc.replace(tzinfo=UTC).astimezone(ET).date().isoformat())

        return sorted(dates, reverse=True)[:limit]
    except sqlite3.Error:
        return []
    finally:
        if conn is not None:
            conn.close()


def get_journal_day(date_str: str) -> dict:
    """Everything Mission Control has about one ET trading day (Stage 5
    journal): the evening reflection, the equity snapshot, every run's
    summary, every trade, and the union of candidate symbols considered
    that day. Read-only aggregation over existing/Stage-4 tables — no new
    authoritative state. Returns all-empty fields (not an exception) when
    the date has no data; the route layer decides whether that's a 404."""
    try:
        day = date.fromisoformat(date_str)
    except (TypeError, ValueError):
        return {
            "daily_pnl": None,
            "insights": None,
            "runs": [],
            "trades": [],
            "candidates": [],
        }
    conn = None
    try:
        conn = _connect()
        start_utc, end_utc = _et_day_utc_bounds(day)

        daily_pnl_row = conn.execute(
            "SELECT * FROM daily_pnl WHERE date = ?",
            (date_str,),
        ).fetchone()
        insights_row = conn.execute(
            "SELECT * FROM insights WHERE date = ?",
            (date_str,),
        ).fetchone()

        run_rows = conn.execute(
            "SELECT run_id, MIN(timestamp) as first_timestamp, "
            "MAX(timestamp) as last_timestamp, COUNT(*) as agent_count, "
            "MAX(decision_id) as decision_id FROM agent_logs "
            "WHERE timestamp >= ? AND timestamp < ? "
            "GROUP BY run_id ORDER BY MAX(timestamp)",
            (start_utc, end_utc),
        ).fetchall()
        runs: list[dict] = []
        for r in run_rows:
            d = dict(r)
            run_id = d["run_id"] or ""
            d["session_prefix"] = run_id.rsplit("-", 1)[0] if "-" in run_id else run_id
            cost_rows = conn.execute(
                "SELECT cost_usd FROM agent_logs WHERE run_id = ?",
                (d["run_id"],),
            ).fetchall()
            d["total_cost_usd"] = _canonical_run_cost(
                conn,
                d["run_id"],
                cost_rows,
            )
            runs.append(d)

        trade_rows = conn.execute(
            "SELECT * FROM trades WHERE timestamp >= ? AND timestamp < ? ORDER BY timestamp",
            (start_utc, end_utc),
        ).fetchall()
        trades = [dict(r) for r in trade_rows]

        candidate_rows = conn.execute(
            "SELECT DISTINCT symbol FROM specialist_evidence "
            "WHERE timestamp >= ? AND timestamp < ? "
            "AND scope = 'symbol' AND symbol IS NOT NULL",
            (start_utc, end_utc),
        ).fetchall()
        candidates = sorted({r[0] for r in candidate_rows} | {t["symbol"] for t in trades})

        return {
            "daily_pnl": dict(daily_pnl_row) if daily_pnl_row is not None else None,
            "insights": dict(insights_row) if insights_row is not None else None,
            "runs": runs,
            "trades": trades,
            "candidates": candidates,
        }
    except sqlite3.Error:
        return {
            "daily_pnl": None,
            "insights": None,
            "runs": [],
            "trades": [],
            "candidates": [],
        }
    finally:
        if conn is not None:
            conn.close()


def get_research_day(date_str: str) -> dict:
    """One consistent read-only snapshot for the Research Intelligence desk.

    Unlike the older convenience readers, this function does not conflate a
    SQLite failure with a genuinely quiet day: `read_error` is explicit and
    sanitized.  Raw agent rows are returned only to the typed route projection;
    that projection deliberately excludes prompts and `full_response`.
    """
    try:
        day = date.fromisoformat(date_str)
    except (TypeError, ValueError):
        return {"invalid_date": True, "read_error": None}

    conn = None
    try:
        conn = _connect()
        start_utc, end_utc = _et_day_utc_bounds(day)
        agent_logs = [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM agent_logs WHERE timestamp >= ? AND timestamp < ? ORDER BY timestamp, id",
                (start_utc, end_utc),
            ).fetchall()
        ]
        evidence = [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM specialist_evidence WHERE timestamp >= ? AND timestamp < ? ORDER BY timestamp, id",
                (start_utc, end_utc),
            ).fetchall()
        ]
        trades = [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM trades WHERE timestamp >= ? AND timestamp < ? ORDER BY timestamp, id",
                (start_utc, end_utc),
            ).fetchall()
        ]
        daily_pnl = conn.execute(
            "SELECT * FROM daily_pnl WHERE date = ?",
            (date_str,),
        ).fetchone()
        insights = conn.execute(
            "SELECT * FROM insights WHERE date = ?",
            (date_str,),
        ).fetchone()
        return {
            "invalid_date": False,
            "read_error": None,
            "agent_logs": agent_logs,
            "specialist_evidence": evidence,
            "trades": trades,
            "daily_pnl": dict(daily_pnl) if daily_pnl is not None else None,
            "insights": dict(insights) if insights is not None else None,
        }
    except sqlite3.Error:
        return {
            "invalid_date": False,
            "read_error": "research data unavailable",
            "agent_logs": [],
            "specialist_evidence": [],
            "trades": [],
            "daily_pnl": None,
            "insights": None,
        }
    finally:
        if conn is not None:
            conn.close()


def _escape_like(term: str) -> str:
    """Escape SQLite LIKE metacharacters in a user-supplied search term so
    it's matched literally (paired with `ESCAPE '\\'` below). This is what
    keeps the search endpoint from ever needing (or accepting) arbitrary
    SQL — the term is always a bound parameter, never interpolated into
    the query text; this only prevents a literal `%`/`_` in the SEARCH
    TERM from acting as an unintended wildcard."""
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def search(q: str, limit: int = 50) -> dict:
    """Forensic search (Stage 5) over trades + agent_logs via parameterized
    `LIKE` matching only — every value is a bound parameter; no query text
    is ever built from `q`. Returns `{"trades": [...], "agent_logs": [...]}`,
    each newest-first and independently capped at `limit`."""
    term = (q or "").strip()
    if not term:
        return {"trades": [], "agent_logs": []}
    like = f"%{_escape_like(term)}%"
    conn = None
    try:
        conn = _connect()
        trade_rows = conn.execute(
            "SELECT * FROM trades WHERE symbol LIKE ? ESCAPE '\\' "
            "OR reasoning LIKE ? ESCAPE '\\' "
            "ORDER BY timestamp DESC LIMIT ?",
            (like, like, limit),
        ).fetchall()
        agent_rows = conn.execute(
            "SELECT * FROM agent_logs WHERE agent_name LIKE ? ESCAPE '\\' "
            "OR model LIKE ? ESCAPE '\\' OR output_summary LIKE ? ESCAPE '\\' "
            "OR input_summary LIKE ? ESCAPE '\\' "
            "ORDER BY timestamp DESC LIMIT ?",
            (like, like, like, like, limit),
        ).fetchall()
        return {
            "trades": [dict(r) for r in trade_rows],
            "agent_logs": [dict(r) for r in agent_rows],
        }
    except sqlite3.Error:
        return {"trades": [], "agent_logs": []}
    finally:
        if conn is not None:
            conn.close()


def list_meta_periods() -> list[dict]:
    """Existence-only summary of each `data/evolution/{period}/` directory.

    Never parses digest.json/reflection.json/proposed_edits.json contents —
    those are LLM-generated free text, not this layer's concern. Returns
    `[]` if `data/evolution/` doesn't exist at all.
    """
    base = Path("data/evolution")
    if not base.is_dir():
        return []
    out: list[dict] = []
    for entry in sorted(base.iterdir()):
        if not entry.is_dir():
            continue
        out.append(
            {
                "period": entry.name,
                "has_digest": (entry / "digest.json").is_file(),
                "has_reflection": (entry / "reflection.json").is_file(),
                "has_proposed_edits": (entry / "proposed_edits.json").is_file(),
            }
        )
    return out


# ---------------------------------------------------------------------------
# Conviction ledger (spec §9.5) — the analyst scorecard's only data source.
#
# The ledger writes into the EXISTING `specialist_evidence` table (no new
# store): `kind='seat_stance'` for the side each analyst took on an idea, and
# `kind='conviction_credit'` for the scored outcome once the position closed.
# Both are read here through the same independent `mode=ro` connection every
# other function in this module uses.
#
# NOTHING is scored here. The realized `r_multiple` and the alignment
# decision (`side`) are computed once by the ledger layer at close time and
# persisted; this function only parses the stored JSON and signs the R by the
# side that was already decided. There is no conviction weight to apply —
# credit is raw signed R (owner decision, 2026-08-31). `src/api` deliberately does not import
# `src.conviction_ledger`, because that module imports `src.risk.rules` and
# `tests/test_api_safety.py` forbids any `src.risk` import from this package —
# see docs/architecture/MISSION_CONTROL_API.md.
#
# A database written before the ledger existed simply has no rows of these
# kinds and reads back as an empty list — not an error.
# ---------------------------------------------------------------------------

CONVICTION_CREDIT_KIND = "conviction_credit"
SEAT_STANCE_KIND = "seat_stance"


def _parse_evidence_payload(raw: str | None) -> dict | None:
    try:
        parsed = json.loads(raw or "")
    except (TypeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


def get_conviction_ledger() -> dict:
    """Every persisted conviction-credit row, plus the stances behind them.

    Returns `{"read_error": str | None, "credits": [...], "stances": [...]}`.
    `read_error` is explicit and sanitized so a SQLite failure is never
    presented as a desk that simply has not scored anything yet — the same
    distinction `get_research_day` draws.

    Rows whose `evidence_json` will not parse, or that carry no numeric
    `r_multiple`, are skipped rather than defaulted to zero: a malformed row
    is absence of evidence, not evidence of a break-even call.

    `credit` is DERIVED here — `+r_multiple` for a supporter, `-r_multiple`
    for an opposer — rather than read from the stored `credit` field, and the
    reason matters. Rows written before 2026-08-31 stored a
    conviction-weighted credit; the weight was removed by owner decision that
    day. Deriving from the stored unweighted `r_multiple` and `side` is exact,
    so every row read here means the same thing regardless of when it was
    written, with no migration and no mixing of two scales in one series.
    Mirrors `Database.get_conviction_credits`, which derives it identically.
    """
    conn = None
    try:
        conn = _connect()
        credit_rows = conn.execute(
            "SELECT agent_name, symbol, timestamp, decision_id, evidence_json "
            "FROM specialist_evidence WHERE kind = ? ORDER BY timestamp, id",
            (CONVICTION_CREDIT_KIND,),
        ).fetchall()
        stance_rows = conn.execute(
            "SELECT agent_name, symbol, decision_id, evidence_json FROM specialist_evidence WHERE kind = ? ORDER BY id",
            (SEAT_STANCE_KIND,),
        ).fetchall()
    except sqlite3.Error:
        return {
            "read_error": "conviction ledger unavailable",
            "credits": [],
            "stances": [],
        }
    finally:
        if conn is not None:
            conn.close()

    credits: list[dict] = []
    for row in credit_rows:
        payload = _parse_evidence_payload(row["evidence_json"])
        if payload is None:
            continue
        try:
            r_multiple = float(payload["r_multiple"])
        except (KeyError, TypeError, ValueError):
            continue
        side = str(payload.get("side") or "")
        credits.append(
            {
                "analyst": str(payload.get("seat") or row["agent_name"] or ""),
                "symbol": str(payload.get("symbol") or row["symbol"] or ""),
                "side": side,
                "stance": str(payload.get("stance") or ""),
                "conviction": str(payload.get("conviction") or ""),
                "r_multiple": r_multiple,
                "credit": round(r_multiple if side == "supported" else -r_multiple, 4),
                # The ledger stamps `resolved_at` from the closing trade; fall
                # back to the evidence row's own timestamp rather than dropping
                # a scored call out of the series for want of a date.
                "resolved_at": str(payload.get("resolved_at") or row["timestamp"] or ""),
                "position_id": payload.get("position_id"),
                "decision_id": payload.get("decision_id") or row["decision_id"],
                "direction": str(payload.get("direction") or "long"),
                "nominated": bool(payload.get("nominated")),
            }
        )

    stances: list[dict] = []
    for row in stance_rows:
        payload = _parse_evidence_payload(row["evidence_json"])
        if payload is None:
            continue
        stances.append(
            {
                "analyst": str(payload.get("seat") or row["agent_name"] or ""),
                "symbol": str(payload.get("symbol") or row["symbol"] or ""),
                "decision_id": row["decision_id"],
                "stance": str(payload.get("stance") or ""),
                "conviction": str(payload.get("conviction") or ""),
                "nominated": bool(payload.get("nominated")),
                "observation": str(payload.get("observation") or ""),
            }
        )

    return {"read_error": None, "credits": credits, "stances": stances}


def get_holding_why(symbol: str) -> dict | None:
    """Everything `src.api.holding_why.build_holding_why` needs for one
    symbol, or None when the desk has no entry row for it at all.

    Scoped deliberately narrowly: the ENTRY trade (the most recent
    `BUY`/`SHORT`), that entry run's symbol-scoped evidence, every later
    `review_metrics` row for the same position, and the trades written
    against the position after the entry. Reading the whole symbol's
    evidence history instead would pull in every prior run that looked at
    the name and never traded it — which is exactly the repetition the
    holding view exists to remove.
    """
    conn = None
    try:
        conn = _connect()
        entry = conn.execute(
            "SELECT * FROM trades WHERE symbol = ? AND action IN ('BUY', 'SHORT') "
            "ORDER BY timestamp DESC, id DESC LIMIT 1",
            (symbol,),
        ).fetchone()
        if entry is None:
            return None
        entry = dict(entry)
        # Including the run that OPENED the position: see
        # `src/api/holding_entry_evidence.py` for why an add's own run is
        # not enough to say who raised the name.
        evidence = entry_evidence(conn, symbol, entry)
        interim: list[dict] = []
        position_id = entry.get("position_id")
        if position_id:
            interim = [
                dict(row)
                for row in conn.execute(
                    "SELECT * FROM trades WHERE position_id = ? AND id != ? ORDER BY timestamp, id",
                    (position_id, entry.get("id")),
                ).fetchall()
            ]
        return {"entry": entry, "evidence": evidence, "interim": interim}
    except sqlite3.Error:
        return None
    finally:
        if conn is not None:
            conn.close()


#: Item 211. The coverage watchdog's on-box state file, which is where
#: `src.coverage_watchdog.claim_typed_alert` durably records every repeat
#: alert it declined to resend. Spelled out here rather than imported so
#: this module keeps its "no trading-module imports" isolation invariant
#: (tests/test_api_safety.py); it is the same literal path
#: `src.coverage_watchdog.STATE_PATH` builds, and a test pins the two
#: together so a move cannot silently blank this endpoint.
SUPPRESSION_STATE_PATHS = (
    alerting_dir() / "coverage_heartbeat.json",
    alerting_dir() / "deploy_drift.json",
)


def get_suppressed_alerts(limit: int = 50) -> dict:
    """Item 211 — what the desk decided NOT to say, and why it is not lost.

    Suppression is only honest if the suppressed thing stays readable. Two
    records feed this, both already written by the suppressing code; this
    function invents nothing and aggregates nothing it cannot point at.

    * `deferred_suspensions` — `suspend_alert_deferred` rows in
      `llm_circuit_events`, one per self-clearing latch whose owner page was
      held back until it outlived the circuit's own self-clear window.
    * `suppressed_repeats` — the `suppressed_alerts` block in each of the
      watchdog's two state files (stop-coverage and deploy-drift), per
      alert TYPE, per ET day.
    """

    out: dict = {
        "deferred_suspensions": [],
        "deferred_available": False,
        "suppressed_repeats": {},
        "suppression_state_available": False,
    }
    conn = None
    try:
        conn = _connect()
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        if "llm_circuit_events" in tables:
            out["deferred_available"] = True
            rows = conn.execute(
                "SELECT trigger_code, detail, run_id, created_at "
                "FROM llm_circuit_events WHERE event_type='suspend_alert_deferred' "
                "ORDER BY id DESC LIMIT ?",
                (max(1, int(limit)),),
            ).fetchall()
            out["deferred_suspensions"] = [
                {
                    "trigger_code": r["trigger_code"],
                    "detail": r["detail"],
                    "run_id": r["run_id"],
                    "created_at": r["created_at"],
                }
                for r in rows
            ]
    except Exception:
        pass
    finally:
        if conn is not None:
            conn.close()

    cleaned: dict = {}
    for state_file in SUPPRESSION_STATE_PATHS:
        try:
            raw = json.loads(state_file.read_text())
        except (OSError, ValueError):
            continue
        if not isinstance(raw, dict):
            continue
        out["suppression_state_available"] = True
        log = raw.get("suppressed_alerts")
        if isinstance(log, dict):
            for kind, entry in log.items():
                if not isinstance(entry, dict):
                    continue
                events = entry.get("events")
                cleaned[str(kind)] = {
                    "day": entry.get("day"),
                    "count": int(entry.get("count") or 0),
                    "events": [e for e in events if isinstance(e, dict)][-max(1, int(limit)) :]
                    if isinstance(events, list)
                    else [],
                }
    out["suppressed_repeats"] = cleaned
    return out


# --- item 211: the backlog the global mute swallowed -----------------------
#
# The owner muted every desk alert on 2026-09-30 (docs/WORK.md item 211).
# Until 2026-10-01 the mute dropped each message BEFORE anything was written
# down, so the swallowed messages of that first day do not exist anywhere and
# cannot be recovered. From the commit that added the `status="muted"` branch
# in `src/notifier.py` onward, every dropped message lands in `notifier_sends`
# with its kind, text, symbols and timestamp. This read turns that record into
# the one thing the owner actually asked for: would turning alerts back on
# flood him again, and did he miss anything about unprotected money.
#
# It un-mutes nothing, sends nothing and reads nothing but the database.

#: When the muted-message record began, from the commit that added the
#: recording branch (`0de4c45`, authored 2026-10-01 02:25:46 -0400). Stored as
#: UTC because `notifier_sends.timestamp` is written by `datetime('now')`.
MUTE_RECORD_BEGINS_AT = "2026-10-01T06:25:46+00:00"

#: The day the owner muted the desk (docs/WORK.md item 211). Anything the mute
#: dropped between this date and `MUTE_RECORD_BEGINS_AT` was never recorded.
MUTE_BEGAN_ON = "2026-09-30"


#: Every `notifier_sends.status` that means the owner did not receive the
#: message. Kept as one tuple so the surface and the counters cannot disagree
#: about what "undelivered" means; the query selects by exclusion of 'sent',
#: so a status added later still reaches the owner, merely under "muted".
_UNDELIVERED_REASONS = ("muted", "filtered", "failed")


from src.api.muted_helpers import _et_day, _headline, _muted_symbols  # noqa: E402,F401
from src.api.live_risk_message import _live_risk_headlines, is_live_risk_message  # noqa: E402,F401


def get_muted_backlog() -> dict:
    """Item 211 — what the global mute has swallowed, grouped to be judged.

    Grouping answers the owner's two real questions rather than counting.
    `by_kind` and `by_day` answer "would I be flooded again"; `live_risk`
    answers "did I miss anything that mattered" and is a LIST, never folded
    into either total, because one unprotected position is not one message.

    TWO REASONS, ONE BACKLOG (PR #978). A row is here because the global
    mute swallowed it (`muted`) or because the per-category mute dropped it
    as operational (`filtered`). Both are messages the owner did not get,
    so both belong on the one surface he reads; `reason`, `muted_total` and
    `filtered_total` keep "deliberately filtered" distinguishable from
    "muted" rather than folding them together.

    `coverage_complete` is False whenever the record begins after the mute
    did, which it does; the gap is stated in the payload so no caller can
    present a partial list as the whole story.

    DELIBERATELY UNCAPPED. An earlier draft read only the most recent 200
    rows, which would have let a flood of ordinary messages push live-risk
    ones out of the list and out of the counts — burying the important ones
    in volume, the exact failure that made the owner mute the desk. The
    record holds one row per message the mute dropped and nothing else, so
    reading all of it is cheap; a cap here would be a silent lie.
    """

    out: dict = {
        "record_available": False,
        "record_begins_at": MUTE_RECORD_BEGINS_AT,
        "mute_began_on": MUTE_BEGAN_ON,
        "coverage_complete": False,
        "coverage_gap": (
            "Alerts were muted on "
            + MUTE_BEGAN_ON
            + ", but the desk only began recording dropped messages on "
            + MUTE_RECORD_BEGINS_AT[:10]
            + ". Anything the mute dropped before then was never written "
            "down and cannot be shown here."
        ),
        "total": 0,
        "muted_total": 0,
        "filtered_total": 0,
        "failed_total": 0,
        "live_risk_total": 0,
        "by_kind": [],
        "by_day": [],
        "live_risk": [],
        "oldest": None,
        "newest": None,
    }
    conn = None
    try:
        conn = _connect()
        exists = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='notifier_sends'").fetchone()
        if not exists:
            return out
        out["record_available"] = True
        rows = conn.execute(
            # Read what was NOT delivered, rather than enumerating the
            # ways a message can fail to be delivered. The enumeration has
            # drifted twice: 'filtered' was missing until PR #978, and
            # 'failed' — the status the transport writes when the send
            # itself errors — was missing until this change, so an owner
            # alert the desk tried and failed to deliver appeared on no
            # surface at all while its caller discarded the False. Telegram
            # is muted, so this page is the owner's only channel.
            "SELECT kind, text, detail, timestamp, status FROM notifier_sends "
            "WHERE status <> 'sent' ORDER BY timestamp DESC"
        ).fetchall()
    except Exception:
        return out
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass

    rows = list(rows)
    if not rows:
        return out

    by_kind: dict[str, dict[str, int]] = {}
    by_day: dict[str, dict[str, int]] = {}
    stamps: list[str] = []
    for kind, text, detail, stamp, status in rows:
        kind = str(kind or "unknown")
        stamp = str(stamp or "")
        status = str(status or "muted")
        reason = status if status in _UNDELIVERED_REASONS else "muted"
        stamps.append(stamp)
        live = is_live_risk_message(text)
        day = _et_day(stamp)
        for bucket, key in ((by_kind, kind), (by_day, day)):
            slot = bucket.setdefault(
                key,
                {
                    "count": 0,
                    "live_risk_count": 0,
                    **{f"{r}_count": 0 for r in _UNDELIVERED_REASONS},
                },
            )
            slot["count"] += 1
            slot[f"{reason}_count"] += 1
            if live:
                slot["live_risk_count"] += 1
        out["total"] += 1
        out[f"{reason}_total"] += 1
        if live:
            out["live_risk_total"] += 1
            out["live_risk"].append(
                {
                    "timestamp": stamp,
                    "day": day,
                    "kind": kind,
                    "reason": reason,
                    "symbols": _muted_symbols(detail),
                    "headline": _headline(text),
                }
            )

    out["by_kind"] = [{"kind": k, **v} for k, v in sorted(by_kind.items(), key=lambda kv: (-kv[1]["count"], kv[0]))]
    out["by_day"] = [{"day": d, **v} for d, v in sorted(by_day.items(), reverse=True)]
    if stamps:
        out["oldest"] = min(stamps)
        out["newest"] = max(stamps)
    return out


def get_target_scorecard() -> dict:
    """Entry take-profit target scorecard (RECORD ONLY; owner ruling 2026-10-09).

    Wraps `target_scorecard` on the same independent `mode=ro` connection every
    other read here uses. A SQLite failure is an explicit `read_error`, never an
    empty scorecard.
    """
    from src.storage.analytics.target_scorecard import target_scorecard

    conn = None
    try:
        conn = _connect()
        result = target_scorecard(conn)
    except sqlite3.Error:
        return {"read_error": "target scorecard unavailable"}
    finally:
        if conn is not None:
            conn.close()
    result["read_error"] = None
    return result
