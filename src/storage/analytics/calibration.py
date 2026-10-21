"""Trade-calibration / reporting read cluster lifted VERBATIM from src/storage/db.py (db rebuild instalment 2).

Read-and-aggregate only: nothing here writes a row. Standalone: collaborators
are the open sqlite3 connection, the Database lock and the executed-trade SQL
predicate, all keyword-only, so it builds with no Database/TradingPipeline
behind it (tests/boundary_harness.py). Database keeps same-named thin shims
that construct this per call.

The three module-level helpers below moved with the cluster; src/storage/db.py
re-imports them so `from src.storage.db import _CONVICTION_OUTCOME_MIN_N`
keeps working (one definition, here).
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from collections.abc import Callable
from datetime import datetime, timezone
from src.trading_calendar import utc_now

from src.storage.analytics.agent_log_reads import recent_agent_outputs
from src.trade_actions import POSITION_OPEN_ACTIONS as _POSITION_OPEN_ACTIONS
from src.trade_actions import is_filled_trail_stop as _is_filled_trail_stop  # realized-exit rule, not share counting
from src.util.time import ET
from src.storage.account_epoch import CURRENT_TRADES_VIEW

logger = logging.getLogger(__name__)


#: Conviction ledger (spec §7.2) — the sample floor below which
#: `compute_trade_calibration`'s `by_conviction` / `by_allocated_risk`
#: groupings refuse to state a win rate, an average return, or any
#: comparison between buckets. The function's EXISTING top-level gate
#: (`len(closed) < 3`) governs whether `by_size`/`by_side` render at all,
#: which is fine for "does this book's win rate look reasonable" — it is
#: far too permissive for "does conviction predict outcome", a claim that
#: will steer how much risk the desk allocates to its best ideas if it
#: reaches a live prompt. Measured on real production data (2026-08-30,
#: 40 trades / 17 entries / 8 closed round-trips, 2026-08-14 through
#: 2026-08-28): the 8 closed trades split 4 high / 3 low / 1 medium
#: conviction, and the high bucket's average return was WORSE than the
#: low bucket's — noise from n=4 vs n=3, not a finding, and it would
#: invert within a handful of trades. 20 is deliberately high: a bucket
#: needs a real sample before "conviction predicts outcome" is asked of
#: it at all, and the entire book will not clear this for a long time
#: (see docs/QAMC_REMEDIATION_SPEC.md §7.2 and the conviction ledger
#: report for the exact measured figures).
_CONVICTION_OUTCOME_MIN_N = 20


#: Start of the CLEAN RECORD, as a naive-UTC timestamp string compared against
#: each position's opening-entry timestamp. Owner ruling 2026-10-09: every
#: trade before that day's fixes is NOT evidence (stops and targets were set
#: wrongly); the clean record starts at the market open of Monday 2026-10-12
#: 09:30 America/New_York (EDT, UTC-4) = 13:30 UTC. A dated ruling, not a
#: tunable: no config knob. Older rows stay in the database, only uncounted.
CLEAN_RECORD_START_UTC = "2026-10-12T13:30:00"
CLEAN_RECORD_DATE = CLEAN_RECORD_START_UTC[:10]

#: The account total's baseline rows (src/clean_record_baseline.py): the first
#: daily_pnl row on/after the start date, and the row just before it.
#: Bind parameter: the date from `pnl_start`. Defined here, beside the constant, so this
#: module never imports the baseline module (no import cycle).
CLEAN_BASELINE_FIRST_SQL = "SELECT * FROM daily_pnl WHERE date >= ? ORDER BY date ASC LIMIT 1"
CLEAN_BASELINE_PREV_SQL = "SELECT * FROM daily_pnl WHERE date < ? ORDER BY date DESC LIMIT 1"


def pnl_start(conn: sqlite3.Connection) -> tuple[str, bool]:
    """`(first daily_pnl date counted, True when an account reset sets it)` for "Total P&L since".

    The LATER of CLEAN_RECORD_START_UTC and the latest recorded account reset
    (`account_epoch`, src/storage/account_epoch.py). A reset's date is the first
    session close after it, the same cut the current-account daily_pnl view uses.
    With no reset recorded, or only one before the clean record, this is
    `(CLEAN_RECORD_DATE, False)`: nothing changes.
    """
    row = None
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='account_epoch'").fetchone():
        row = conn.execute(
            "SELECT epoch_utc, daily_pnl_from FROM account_epoch WHERE epoch_utc IS NOT NULL ORDER BY id DESC LIMIT 1"
        ).fetchone()
    if row and str(row[0]).replace(" ", "T") > CLEAN_RECORD_START_UTC:
        return row[1], True
    return CLEAN_RECORD_DATE, False


def _opened_before_clean_record(open_ts) -> bool:
    try:
        dt = datetime.fromisoformat(str(open_ts).replace(" ", "T").replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return True  # unparseable opening time cannot prove it is clean
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt < datetime.fromisoformat(CLEAN_RECORD_START_UTC)


_LONG_EXIT_ACTIONS = ("EMERGENCY_SELL", "FORCE_DELEVER", "REDUCE", "TAKE_PROFIT", "STOP_OUT", "RECONCILED_EXIT")


def conviction_positions(rows, lookback_days: int | None) -> list[dict]:
    """Group executed trade rows (chronological) into whole POSITIONS.

    RULE (one, written here): a position's conviction is the conviction on
    its OPENING entry row (first BUY for a long, first SHORT for a short).
    Adds at other levels never change it. Only FULLY CLOSED positions (net
    quantity back to zero) are returned; a partial sale is not counted yet.
    A closing exit whose realized_pnl is missing makes the position
    `pnl_known=False` (UNKNOWN) - never zero, never a win or a loss.
    SWEEP_BUY / SWEEP_SELL are deliberately NOT positions here: they are real
    broker fills, but of the cash-parking vehicle only (SGOV in production;
    src/execution/cash_sweep.py, "Ledger isolation"), never a trading
    decision. They open and close nothing in this record, and no BUY/SHORT
    row ever shares their symbol, so no decision position is left "open" by
    one.
    Positions are dated by their final exit; those older than
    `lookback_days` are dropped. Record only: nothing here drives sizing.
    """
    state: dict[str, dict] = {}
    out: list[dict] = []
    for row in rows:
        sym = row["symbol"]
        act = row["action"] or ""
        qty = float(row["fill_qty"] if row["fill_qty"] else row["qty"] or 0)
        price = float(row["fill_price"] if row["fill_price"] else row["price"] or 0)
        if qty <= 0 or price <= 0:
            continue
        pos = state.get(sym)
        if act in _POSITION_OPEN_ACTIONS:
            side = _POSITION_OPEN_ACTIONS[act]
            if pos is None or pos["side"] != side:
                pos = state[sym] = {
                    "side": side,
                    "net": 0.0,
                    "conviction": row["conviction"],
                    "open_ts": row["timestamp"],
                    "cost": 0.0,
                    "pnl": 0.0,
                    "pnl_known": True,
                }
            pos["net"] += qty
            pos["cost"] += qty * price
            continue
        if pos is None:
            continue
        is_long_exit = (
            act.startswith("SELL")
            or act.startswith("PARTIAL_SELL")
            or act in _LONG_EXIT_ACTIONS
            or _is_filled_trail_stop(row, act)
        )
        is_short_exit = act in ("COVER", "EMERGENCY_COVER") or act.startswith("PARTIAL_COVER")
        if not ((is_long_exit and pos["side"] == "long") or (is_short_exit and pos["side"] == "short")):
            continue
        pos["net"] -= qty
        if row["realized_pnl"] is None:
            pos["pnl_known"] = False
        else:
            pos["pnl"] += float(row["realized_pnl"])
        if pos["net"] > 1e-9:
            continue
        del state[sym]
        try:
            open_dt = datetime.fromisoformat(pos["open_ts"].replace(" ", "T"))
            close_dt = datetime.fromisoformat(row["timestamp"].replace(" ", "T"))
            hold_days = max(0, (close_dt - open_dt).days)
            if lookback_days is not None and (utc_now().replace(tzinfo=None) - close_dt).days > lookback_days:
                continue
        except (ValueError, TypeError, AttributeError):
            hold_days = 0
        out.append(
            {
                "symbol": sym,
                "side": pos["side"],
                "conviction": pos["conviction"],
                "pnl_known": pos["pnl_known"],
                "pnl": pos["pnl"],
                "return_pct": pos["pnl"] / pos["cost"] * 100 if pos["cost"] > 0 else 0.0,
                "hold_days": hold_days,
                "open_ts": pos["open_ts"],
            }
        )
    return out


class TradeAnalytics:
    """Reads trade/report rows and computes calibration and reporting numbers."""

    def __init__(self, *, conn: sqlite3.Connection, lock: threading.Lock, executed_trade_predicate: Callable[[], str]):
        self.conn = conn
        self._lock = lock
        self._executed_trade_predicate = executed_trade_predicate

    def get_evening_report(self, date: str | None = None) -> dict | None:
        """One stored evening report — `date`, or the most recent one.

        Returns None when there is no row, and None when the stored JSON
        cannot be parsed: an unreadable row is an absent report, and the
        caller must say so rather than render a partial guess. `positions`
        is None (not []) when the row carries no book snapshot — "not
        recorded" and "flat book" are different facts.
        """
        import json

        with self._lock:
            if date:
                row = self.conn.execute(
                    "SELECT * FROM evening_reports WHERE date = ?",
                    (date,),
                ).fetchone()
            else:
                row = self.conn.execute("SELECT * FROM evening_reports ORDER BY date DESC LIMIT 1").fetchone()
        if not row:
            return None
        record = dict(row)
        try:
            payload = json.loads(record.get("payload_json") or "")
        except (TypeError, ValueError):
            logger.error(
                "evening_reports row for %s has unreadable payload_json",
                record.get("date"),
            )
            return None
        if not isinstance(payload, dict):
            return None
        positions = None
        raw_positions = record.get("positions_json")
        if raw_positions:
            try:
                parsed = json.loads(raw_positions)
                positions = parsed if isinstance(parsed, list) else None
            except (TypeError, ValueError):
                positions = None
        return {
            "date": record.get("date"),
            "run_id": record.get("run_id"),
            "timestamp": record.get("timestamp"),
            "payload": payload,
            "positions": positions,
        }

    def last_fresh_seat_reads(self, seats=None) -> dict:
        """When was each research seat LAST actually read, and by which run?

        Reads back the stamps the runs already persisted inside
        `session_reports.payload_json` and `intra_check_reports.payload_json`
        — the store `evidence_freshness` has always used. No new table and no
        second mechanism: this is the read side of a recording that already
        exists.

        Returns `{seat: {"run_id", "mode", "at"}}` for the most recent run in
        which that seat was `refreshed_this_session`. A seat that has never
        been recorded fresh is simply absent from the result, which is what
        lets a carried seat report "age unknown" honestly instead of
        guessing one.

        Walks rows newest-first and stops as soon as every requested seat has
        an answer, so the common case touches a handful of rows. No time
        window and no cutoff: a window would be an invented number.
        """
        import json

        wanted = {str(x) for x in seats} if seats else None
        found: dict[str, dict] = {}
        rows_sql = (
            "SELECT payload_json, timestamp FROM ("
            "  SELECT payload_json, timestamp FROM session_reports"
            "  UNION ALL"
            "  SELECT payload_json, timestamp FROM intra_check_reports"
            ") ORDER BY timestamp DESC"
        )
        with self._lock:
            cursor = self.conn.execute(rows_sql)
            for row in cursor:
                try:
                    payload = json.loads(row[0] or "{}")
                except (TypeError, ValueError):
                    continue
                record = (payload or {}).get("evidence_freshness")
                if not isinstance(record, dict):
                    continue
                stamps = record.get("seat_stamps")
                if not isinstance(stamps, dict):
                    continue
                for seat, entry in stamps.items():
                    name = str(seat)
                    if name in found or not isinstance(entry, dict):
                        continue
                    if entry.get("state") != "refreshed_this_session":
                        continue
                    found[name] = {
                        "run_id": entry.get("run_id"),
                        "mode": entry.get("mode"),
                        "at": entry.get("at") or row[1],
                    }
                if wanted and wanted <= set(found):
                    break
        return found

    def get_session_report(self, mode: str, date: str | None = None) -> dict | None:
        """One stored morning/midday/close report for `mode` — `date`, or
        the most recent one. None when absent or unreadable; see
        `get_evening_report`'s docstring for why that distinction matters.
        """
        import json

        with self._lock:
            if date:
                row = self.conn.execute(
                    "SELECT * FROM session_reports WHERE date = ? AND mode = ?",
                    (date, mode),
                ).fetchone()
            else:
                row = self.conn.execute(
                    "SELECT * FROM session_reports WHERE mode = ? ORDER BY date DESC LIMIT 1",
                    (mode,),
                ).fetchone()
        if not row:
            return None
        record = dict(row)
        try:
            payload = json.loads(record.get("payload_json") or "")
        except (TypeError, ValueError):
            logger.error(
                "session_reports row for %s/%s has unreadable payload_json",
                record.get("mode"),
                record.get("date"),
            )
            return None
        if not isinstance(payload, dict):
            return None
        positions = None
        raw_positions = record.get("positions_json")
        if raw_positions:
            try:
                parsed = json.loads(raw_positions)
                positions = parsed if isinstance(parsed, list) else None
            except (TypeError, ValueError):
                positions = None
        return {
            "date": record.get("date"),
            "mode": record.get("mode"),
            "run_id": record.get("run_id"),
            "timestamp": record.get("timestamp"),
            "payload": payload,
            "positions": positions,
        }

    def get_intra_check_report(self, run_id: str | None = None, date: str | None = None) -> dict | None:
        """One stored intra_check tick.

        `run_id` selects a specific tick. Otherwise `date` (or, absent
        that, today's most recent row overall) returns the LATEST tick
        recorded for that day — the closest analogue to "what did the desk
        last see" for a session that runs many times a day. None when
        absent or unreadable.
        """
        import json

        with self._lock:
            if run_id:
                row = self.conn.execute(
                    "SELECT * FROM intra_check_reports WHERE run_id = ?",
                    (run_id,),
                ).fetchone()
            elif date:
                # rowid tie-breaks `timestamp`, which is second-resolution
                # (`datetime('now')`) — two ticks in the same second must
                # still resolve to insertion order, not an arbitrary one.
                row = self.conn.execute(
                    "SELECT * FROM intra_check_reports WHERE date = ? ORDER BY timestamp DESC, rowid DESC LIMIT 1",
                    (date,),
                ).fetchone()
            else:
                row = self.conn.execute(
                    "SELECT * FROM intra_check_reports ORDER BY timestamp DESC, rowid DESC LIMIT 1"
                ).fetchone()
        if not row:
            return None
        record = dict(row)
        try:
            payload = json.loads(record.get("payload_json") or "")
        except (TypeError, ValueError):
            logger.error(
                "intra_check_reports row for %s has unreadable payload_json",
                record.get("run_id"),
            )
            return None
        if not isinstance(payload, dict):
            return None
        positions = None
        raw_positions = record.get("positions_json")
        if raw_positions:
            try:
                parsed = json.loads(raw_positions)
                positions = parsed if isinstance(parsed, list) else None
            except (TypeError, ValueError):
                positions = None
        return {
            "date": record.get("date"),
            "run_id": record.get("run_id"),
            "timestamp": record.get("timestamp"),
            "payload": payload,
            "positions": positions,
        }

    def get_clean_record_baseline_rows(self) -> tuple[dict | None, dict | None]:
        """`(first, prev)` daily_pnl rows around the P&L start (`pnl_start`); see src/clean_record_baseline.py."""
        with self._lock:
            start, after_reset = pnl_start(self.conn)
            first = self.conn.execute(CLEAN_BASELINE_FIRST_SQL, (start,)).fetchone()
            # After a reset the previous row belongs to the old account: its close is not this account's start.
            prev = None if after_reset else self.conn.execute(CLEAN_BASELINE_PREV_SQL, (start,)).fetchone()
        return (dict(first) if first else None, dict(prev) if prev else None)

    def get_earliest_daily_pnl(self) -> dict | None:
        """The oldest row this table actually has.

        Used for the Telegram feed's "total P&L" baseline: the 2026-09-02
        book-wide liquidation archived every earlier row (see
        docs/INCIDENT_HISTORY.md), so this table's own earliest surviving
        row IS the reset baseline — never reconstructed from the archive.
        """
        with self._lock:
            row = self.conn.execute("SELECT * FROM daily_pnl ORDER BY date ASC LIMIT 1").fetchone()
        return dict(row) if row else None

    def get_daily_pnl(self, limit: int = 30, before_date: str | None = None) -> list[dict]:
        conditions = []
        params: list = []
        if before_date:
            conditions.append("date < ?")
            params.append(before_date)
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        with self._lock:
            rows = self.conn.execute(
                f"SELECT * FROM daily_pnl {where} ORDER BY date DESC LIMIT ?",
                (*params, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_symbol_last_buy(self, symbol: str, include_in_flight: bool = False, *, action: str = "BUY") -> dict | None:
        """Most recent executed opening row for a symbol.

        Default `action='BUY'` is the PM-memory contract and must not start
        returning SHORT rows — a later short on the same ticker is a different
        position, and mixing the two would feed a long's reviewers a short's
        stop. Pass `action='SHORT'` for the mirrored lookup the stop-coverage
        repair uses on an uncovered short.

        Only opening actions (`BUY` / `SHORT`) are accepted. Anything else is
        a caller bug and returns None rather than guessing.

        Submitted-but-never-filled opens must not show up in PM memory, but a
        partial fill that later ended canceled or expired still created real
        exposure and should be surfaced.

        `include_in_flight=True` also accepts fill_status in
        ('submitted', 'pending_submit') — used by the stop-coverage repair
        (audit round 2): a same-session open whose fill hasn't been reconciled
        yet is invisible under the executed predicate, so the repair either
        no-op'd or read a MONTHS-OLD prior row's stop level in exactly the
        crash/late-fill scenarios the belt exists for. An in-flight open's
        recorded stop_loss is precisely the reviewed intent the repair wants.
        PM-memory callers keep the strict default.

        LIVE-LOT RULE (2026-10-21): the row is chosen only from the position
        open NOW — `trade_actions.live_lot_rows` over this symbol's executed
        rows in `current_account_trades` (the reset-scoped view) — never from
        an old CLOSED holding and never from before the account reset. A
        ledger known to be flat returns None (callers fail safe on None); an
        UNKNOWN fill order falls back to the newest opening row in the view.
        See `live_opening_row`.
        """
        opening = (action or "BUY").upper()
        if opening not in _POSITION_OPEN_ACTIONS:
            logger.warning(
                "get_symbol_last_buy: refusing unknown opening action %r for %s",
                action,
                symbol,
            )
            return None
        with self._lock:
            return live_opening_row(
                self.conn,
                symbol,
                (opening,),
                self._executed_trade_predicate(),
                include_in_flight=include_in_flight,
            )

    def get_position_open_timestamp(self, buy_row: dict | None) -> str | None:
        """When the POSITION `buy_row` belongs to was first opened.

        `get_symbol_last_buy` returns the MOST RECENT opening row, which is
        the right answer for "what did the desk last decide about this name"
        and the wrong one for "how long has this trade been on". A scale-in
        mints no new position: `_assign_position_ids` hands every add the
        same `position_id` until net qty returns to flat. So the position's
        birthday is the EARLIEST executed opening row carrying that id, not
        the latest.

        Why this exists (measured 2026-09-30, live DB): the deterministic
        trail sliced its bars from the last buy's date, while taking the
        entry PRICE from `position.avg_entry`, which is blended across every
        add. On 2026-09-23 MRVL's position (`pos-08b43df53103`, opened
        2026-09-17) took a third add at 17:19; the 19:31 trail evaluation
        therefore saw ZERO bars "since entry" for a position that was four
        sessions old, and `src/risk/trailing.py::_swing_lows` — which needs
        `2 * PIVOT_WINDOW + 1` = 7 bars before it can confirm anything — had
        nothing to read. See `tests/test_position_open_timestamp.py`.

        This lookup makes the trail's window LONGER, and a longer window is
        not automatically safer — the PR that added this said it was, and
        that was untrue. A longer window can only raise the chandelier's
        high-water anchor, and `src/risk/trailing.py::evaluate_trailing_stop`
        REFUSES outright once the resulting candidate rises through its
        noise floor instead of falling back to a lower one, so a wider
        window can cost a tighten the narrower window took. The reason to
        do it anyway is consistency: the caller takes the entry PRICE from
        `position.avg_entry`, blended across every add, so slicing bars from
        the last add alone was incoherent by construction. Measured against
        all 21 recorded refusals on 2026-09-30, the cost is currently zero;
        see the `_swing_lows` docstring for that measurement.

        Returns the timestamp string as stored, or None when `buy_row` is
        missing or carries no `position_id` (legacy rows predating the id,
        and rows the backfill could not chain). None means "unknown", never
        a guessed date — the caller keeps its own fallback.
        """
        row = self.get_position_open_row(buy_row)
        return row["timestamp"] if row else None

    def get_position_open_row(self, buy_row: dict | None) -> dict | None:
        """The FULL row that opened the position `buy_row` belongs to.

        `get_position_open_timestamp` is this lookup's date-only form and
        now delegates here; the chain rule and every caveat in its docstring
        apply unchanged. The date was never the only thing an add corrupts:
        `take_profit` (the trail's reference target) and `initial_stop_loss`
        (the denominator of R, via `recorded_initial_stop`) are also pinned
        at entry, and reading them off the newest add lets the reference
        target sit above current price and measures R from a stop the trade
        never opened with. `setup_type` and `structural_ceiling` are pinned
        at entry the same way.

        `get_symbol_last_buy` keeps its "most recent opening row" meaning —
        PM memory and the stop-coverage repair both want the latest reviewed
        intent — so this is a separate lookup layered on top of it, not a
        change to it.

        Returns None when `buy_row` is missing, carries no `position_id`
        (legacy rows predating the column, and rows the backfill could not
        chain), or is not an opening row. None means "unknown": the caller
        FAILS CLOSED onto `buy_row` itself, which is exactly today's
        behaviour, rather than guessing a chain boundary from timestamps.
        """
        if not buy_row:
            return None
        pid = buy_row.get("position_id")
        symbol = buy_row.get("symbol")
        opening = (buy_row.get("action") or "").upper()
        if not pid or not symbol or opening not in _POSITION_OPEN_ACTIONS:
            return None
        predicate = self._executed_trade_predicate()
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM trades WHERE symbol = ? "
                "AND position_id = ? AND action = ? "
                f"AND {predicate} "
                "ORDER BY timestamp ASC, id ASC LIMIT 1",
                (symbol, pid, opening),
            ).fetchone()
        return dict(row) if row else None

    def get_position_chain_rows(self, buy_row: dict | None) -> list[dict] | None:
        """Every executed row of `buy_row`'s position chain (`quantities.select_position_chain_rows`)."""
        from src.quantities import select_position_chain_rows

        with self._lock:
            return select_position_chain_rows(self.conn, buy_row, self._executed_trade_predicate())

    def get_recent_insights(self, limit: int | None = 7, since_date: str | None = None) -> list[dict]:
        """Evening insights, newest first: the last ``limit`` rows, or every row
        dated on or after ``since_date`` when ``limit`` is None."""
        where, params = ("WHERE date >= ?", [since_date]) if since_date else ("", [])
        tail = "" if limit is None else " LIMIT ?"
        params += [] if limit is None else [limit]
        with self._lock:
            rows = self.conn.execute(
                f"SELECT * FROM insights {where} ORDER BY date DESC{tail}",
                params,
            ).fetchall()
        return [dict(r) for r in rows]

    def get_proposal_funnel_rows(self, since_ts: str) -> dict[str, list[dict]]:
        """Raw rows for the proposal→fill funnel, from `since_ts` onward.

        Returns the five `specialist_evidence` kinds that mark each stage of
        a proposal's life (`target` = PM asked, `proposed_order` = an order
        was built, `verdict` = RM ruled on the plan, `execution_skip` = the
        executor refused it, `pipeline_event` = a typed lifecycle fact such
        as the deterministic constructor dropping a target before it ever
        became a `proposed_order` row) plus the `trades` rows that carry a
        `decision_id`, which is the only join key linking an order back to
        the decision that asked for it.

        No aggregation here — the shaping lives in the caller, matching how
        `get_recent_insights` hands raw insight rows to
        `_build_recent_loss_pits`. `since_ts` is compared lexically against
        the stored 'YYYY-MM-DD HH:MM:SS' timestamps, so a bare 'YYYY-MM-DD'
        is a valid start-of-day cutoff.
        """
        with self._lock:
            evidence = self.conn.execute(
                "SELECT decision_id, kind, symbol, evidence_json, timestamp "
                "FROM specialist_evidence "
                "WHERE kind IN ('target','proposed_order','verdict',"
                "'execution_skip','pipeline_event') AND timestamp >= ? "
                "ORDER BY id",
                (since_ts,),
            ).fetchall()
            trades = self.conn.execute(
                "SELECT decision_id, symbol, action, fill_status, timestamp "
                "FROM trades WHERE decision_id IS NOT NULL "
                "AND timestamp >= ? ORDER BY id",
                (since_ts,),
            ).fetchall()
        return {
            "evidence": [dict(r) for r in evidence],
            "trades": [dict(r) for r in trades],
        }

    def compute_trade_calibration(self, lookback_days: int | None = 45) -> dict:
        """Win rate + avg realized return on round-trips that closed in the
        window — BOTH long (BUY→sell-family) and short (SHORT→cover-family).

        Matches each BUY to the next SELL-family action (SELL, PARTIAL_SELL%,
        EMERGENCY_SELL, FORCE_DELEVER, REDUCE, TAKE_PROFIT, STOP_OUT, and a
        FILLED TRAIL_STOP) for the same symbol, FIFO. Open positions are
        excluded because their outcome isn't known yet.

        Stage 3 (shorts): a SHORT opens no BUY lot, so before this fix a
        COVER closed nothing — every short round-trip was invisible to win
        rate, avg return, and avg hold days, which reach the Portfolio
        Manager as settled fact (Quantitative Facts / L2 Trade Calibration).
        That was harmless while shorts could not be opened; Stage 3 makes
        them openable, turning the gap into a live accounting hole. Mirrors
        the BUY-lot machinery with a SEPARATE FIFO queue: SHORT opens a
        short lot, matched to the next COVER-family action (COVER,
        PARTIAL_COVER%, EMERGENCY_COVER) for the same symbol. A short's
        `return_pct` is signed the OPPOSITE way a long's is — closing BELOW
        the entry is the win, closing ABOVE it is the loss — mirroring the
        same sign convention already established for `unrealized_pnl` /
        `market_value` (negative qty) and for `position_reviewer`'s P&L%.

        Bucketed by allocation size (proxy for conviction): a larger dollar
        commitment implies higher conviction when PM sized it. Lets PM see
        "my high-conviction bets have been winning / losing" without an
        explicit conviction column in trades. Also bucketed by side
        (`by_side.long` / `by_side.short`) so a mixed book's long and short
        edges can be read separately as well as combined.

        `lookback_days=None` applies no exit-age cut: the clean-record start
        (below) is then the only window.

        Returns:
            {"n": int, "win_rate_pct": float, "avg_return_pct": float,
             "avg_hold_days": float, "expectancy_pct": float,
             "avg_win_loss_ratio": float | None,
             "by_size": {
                "large": {...},  # $ entry >= 10k
                "medium": {...}, # 5-10k
                "small": {...},  # <5k
             },
             "by_side": {
                "long": {...},   # same shape as the top level, long round-trips only
                "short": {...},  # same shape, short round-trips only
             },
             "by_conviction": {
                "high": {...}, "medium": {...}, "low": {...},
                # each shaped {"n": int, "insufficient_data": True,
                # "message": "..."} below _CONVICTION_OUTCOME_MIN_N, or the
                # full _bucket_stats shape (plus "insufficient_data": False)
                # at or above it — see that constant's docstring.
             },
             "conviction_unknown_n": int,  # closed trades with no pinned
                                            # conviction (pre-ledger rows,
                                            # or not yet backfilled)
             "by_allocated_risk": {
                "high (≥3%)": {...}, "medium (1-3%)": {...},
                "low (<1%)": {...},  # same gating as by_conviction
             },
             "allocated_risk_unknown_n": int,  # closed trades with no
                                                 # allocated_risk_pct on
                                                 # record (every historical
                                                 # entry to date — see
                                                 # _CONVICTION_OUTCOME_MIN_N)
             }
            or {} when there are too few closed trades to be meaningful.
            For a book with no short round-trips, every top-level number
            here is byte-identical to the pre-Stage-3 output — the
            `expectancy_pct` / `avg_win_loss_ratio` / `by_side` /
            `by_conviction` / `by_allocated_risk` keys are pure additions,
            not changes to the existing ones.

            IMPORTANT — `by_conviction` / `by_allocated_risk` are NEVER
            rendered into any agent prompt below `_CONVICTION_OUTCOME_MIN_N`
            per bucket; see `TradingPipeline._build_calibration_note`
            (src/pipeline.py), which is the ONLY renderer of this dict into
            PM/reviewer-facing text and enforces that gate explicitly. This
            method only computes and labels; it does not decide what an
            agent sees.
        """
        with self._lock:
            # Skip orders that never executed. Legacy rows with NULL fill_status
            # pre-date reconciliation and are treated as filled for backward
            # compatibility.
            # Lots seed from FULL history; only the window bound on EXITS
            # below decides what counts as a "recent closed trade" (audit
            # round 2: windowing both sides made a SELL that closed a
            # pre-window lot FIFO-match an unrelated newer in-window BUY —
            # wrong entry price, wrong hold time, phantom remainder). Same
            # reasoning applies to the SHORT/COVER queue below.
            rows = self.conn.execute(
                "SELECT symbol, action, qty, price, timestamp, fill_qty, "
                "fill_price, fill_status, conviction, allocated_risk_pct, realized_pnl, "
                "requested_risk_pct, decision_model "
                "FROM trades "
                f"WHERE {self._executed_trade_predicate()} "
                "ORDER BY timestamp",
            ).fetchall()
        from collections import defaultdict

        # FIFO queue of open BUY lots per symbol (long side).
        open_lots: dict[str, list[dict]] = defaultdict(list)
        # FIFO queue of open SHORT lots per symbol (Stage 3, short side) —
        # a SEPARATE queue: a SHORT and a BUY on the same symbol are
        # different positions (D3's sign-crossing refusal keeps them from
        # ever being open simultaneously in practice, but keeping the
        # queues independent means this function makes no assumption about
        # that and just matches each side to its own opens).
        open_short_lots: dict[str, list[dict]] = defaultdict(list)
        closed: list[dict] = []
        for row in rows:
            sym = row["symbol"]
            act = row["action"] or ""
            # Prefer actual fill data when present; fall back to requested.
            qty = float(row["fill_qty"] if row["fill_qty"] else row["qty"] or 0)
            price = float(row["fill_price"] if row["fill_price"] else row["price"] or 0)
            ts = row["timestamp"]
            if qty <= 0 or price <= 0:
                continue
            # Conviction ledger (§7.2): pinned at entry, carried through the
            # FIFO lot so every closed record below knows the conviction /
            # risk / model that OPENED it, regardless of which exit closes
            # it or how many exits it takes. None for a legacy pre-ledger
            # BUY/SHORT — never guessed.
            entry_facts = {
                "conviction": row["conviction"],
                "allocated_risk_pct": row["allocated_risk_pct"],
                "requested_risk_pct": row["requested_risk_pct"],
                "decision_model": row["decision_model"],
            }
            if act == "BUY":
                open_lots[sym].append(
                    {
                        "qty": qty,
                        "price": price,
                        "ts": ts,
                        **entry_facts,
                    }
                )
            elif act == "SHORT":
                open_short_lots[sym].append(
                    {
                        "qty": qty,
                        "price": price,
                        "ts": ts,
                        **entry_facts,
                    }
                )
            elif (
                act.startswith("SELL")
                or act.startswith("PARTIAL_SELL")
                or act in ("EMERGENCY_SELL", "FORCE_DELEVER", "REDUCE", "TAKE_PROFIT", "STOP_OUT", "RECONCILED_EXIT")
                or _is_filled_trail_stop(row, act)
            ):
                # RECONCILED_EXIT (item 173(a)) is, like STOP_OUT, written by
                # _reconcile_stop_out_fills ONLY after the broker confirmed the
                # fill — every such row that exists is a realized close, so it
                # must retire a lot here or it leaves a phantom open lot exactly
                # like the 2026-07-16 TRAIL_STOP omission.
                # STOP_OUT (added 2026-08-28, ONDS/CCJ) is written by
                # _reconcile_stop_out_fills ONLY once the broker has already
                # confirmed the fill — unlike TRAIL_STOP, which is written
                # at placement and might never fire — so it needs no
                # analogous "_is_filled_stop_out" guard: every STOP_OUT row
                # that exists at all is, by construction, a realized exit.
                #
                # A FILLED TRAIL_STOP is a realized exit — the broker sold the
                # shares. Omitting it (2026-07-16 audit) left phantom open lots
                # for every stop-out and no close at all: LLY BUY8 → stop-filled
                # 8 → BUY6 → stop-filled 6 read as 14 shares still held and zero
                # LLY trades closed, while the position was flat. The win_rate /
                # avg_return / avg_hold_days this function produces feed PM as
                # facts and the reviewer as calibration_note — on the real
                # ledger the omission moved win_rate 22.2% → 30.0% and
                # avg_return −2.79% → −2.18% for a typical window. The
                # filled-guard: a placed-but-unfilled TRAIL_STOP is
                # protection, not an exit.
                # Close from oldest lot first
                remaining = qty
                lots = open_lots[sym]
                while remaining > 0 and lots:
                    lot = lots[0]
                    closed_qty = min(lot["qty"], remaining)
                    try:
                        buy_dt = datetime.fromisoformat(lot["ts"].replace(" ", "T"))
                        sell_dt = datetime.fromisoformat(ts.replace(" ", "T"))
                        hold_days = max(0, (sell_dt - buy_dt).days)
                    except (ValueError, TypeError):
                        hold_days = 0
                    ret_pct = (price / lot["price"] - 1) * 100 if lot["price"] > 0 else 0
                    entry_usd = closed_qty * lot["price"]
                    # Window applies to the EXIT date only: lots seed from
                    # full history (see the SELECT above), FIFO state always
                    # advances, but only exits inside the lookback count as
                    # "recent closed trades".
                    try:
                        sell_age_days = (utc_now().replace(tzinfo=None) - sell_dt).days
                    except (TypeError, ValueError, UnboundLocalError):
                        sell_age_days = 0
                    if lookback_days is not None and sell_age_days > lookback_days:
                        lot["qty"] -= closed_qty
                        remaining -= closed_qty
                        if lot["qty"] <= 1e-9:
                            lots.pop(0)
                        continue
                    closed.append(
                        {
                            "symbol": sym,
                            "side": "long",
                            "return_pct": ret_pct,
                            "hold_days": hold_days,
                            "entry_usd": entry_usd,
                            "conviction": lot.get("conviction"),
                            "allocated_risk_pct": lot.get("allocated_risk_pct"),
                            "requested_risk_pct": lot.get("requested_risk_pct"),
                            "decision_model": lot.get("decision_model"),
                            "open_ts": lot["ts"],
                        }
                    )
                    lot["qty"] -= closed_qty
                    if lot["qty"] <= 1e-9:
                        lots.pop(0)
                    remaining -= closed_qty
            elif act in ("COVER", "EMERGENCY_COVER") or act.startswith("PARTIAL_COVER"):
                # Stage 3: the short-side twin of the SELL-family block
                # above, against the SEPARATE short-lot queue. Only the
                # return sign differs — a short profits when price FALLS,
                # so closing BELOW the lot's entry price is the win.
                remaining = qty
                lots = open_short_lots[sym]
                while remaining > 0 and lots:
                    lot = lots[0]
                    closed_qty = min(lot["qty"], remaining)
                    try:
                        short_dt = datetime.fromisoformat(lot["ts"].replace(" ", "T"))
                        cover_dt = datetime.fromisoformat(ts.replace(" ", "T"))
                        hold_days = max(0, (cover_dt - short_dt).days)
                    except (ValueError, TypeError):
                        hold_days = 0
                    # Mirror of the long formula `(price / lot_price - 1) *
                    # 100`: a short's profit is (entry - exit), so its
                    # return is expressed relative to the entry the SAME
                    # way, just with entry and exit swapped.
                    ret_pct = (lot["price"] - price) / lot["price"] * 100 if lot["price"] > 0 else 0
                    entry_usd = closed_qty * lot["price"]
                    try:
                        cover_age_days = (utc_now().replace(tzinfo=None) - cover_dt).days
                    except (TypeError, ValueError, UnboundLocalError):
                        cover_age_days = 0
                    if lookback_days is not None and cover_age_days > lookback_days:
                        lot["qty"] -= closed_qty
                        remaining -= closed_qty
                        if lot["qty"] <= 1e-9:
                            lots.pop(0)
                        continue
                    closed.append(
                        {
                            "symbol": sym,
                            "side": "short",
                            "return_pct": ret_pct,
                            "hold_days": hold_days,
                            "entry_usd": entry_usd,
                            "conviction": lot.get("conviction"),
                            "allocated_risk_pct": lot.get("allocated_risk_pct"),
                            "requested_risk_pct": lot.get("requested_risk_pct"),
                            "decision_model": lot.get("decision_model"),
                            "open_ts": lot["ts"],
                        }
                    )
                    lot["qty"] -= closed_qty
                    if lot["qty"] <= 1e-9:
                        lots.pop(0)
                    remaining -= closed_qty
        # Clean record (owner ruling 2026-10-09): every outcome section below
        # (by_size, by_side, by_conviction, by_allocated_risk, overall) is
        # built from the lots opened on/after the start; earlier ones are
        # excluded here and counted once, in the position-level count below.
        closed = [c for c in closed if not _opened_before_clean_record(c["open_ts"])]
        # ONE exclusion unit across every section: whole POSITIONS (see
        # `conviction_positions`), so the owner sees how much was set aside
        # even when the clean window is too thin to render anything else.
        all_positions = conviction_positions(rows, lookback_days)
        positions = [p for p in all_positions if not _opened_before_clean_record(p["open_ts"])]
        excluded_before_clean_record_n = len(all_positions) - len(positions)
        if len(closed) < 3:
            return (
                {"excluded_before_clean_record_n": excluded_before_clean_record_n}
                if excluded_before_clean_record_n
                else {}
            )

        def _bucket_stats(bucket: list[dict]) -> dict:
            if not bucket:
                return {"n": 0}
            n = len(bucket)
            wins = sum(1 for c in bucket if c["return_pct"] > 0)
            avg_ret = sum(c["return_pct"] for c in bucket) / n
            avg_hold = sum(c["hold_days"] for c in bucket) / n
            # docs/QAMC_REMEDIATION_SPEC.md §7.3: "Surface win rate, average
            # win ÷ average loss, expectancy per trade, and average hold
            # duration." Win rate and avg hold duration already existed;
            # these two did not.
            #
            # `expectancy_pct` is mathematically identical to `avg_return_pct`
            # — sum(all trade returns) / n already equals win_rate x avg_win
            # + loss_rate x avg_loss (breakeven trades, return_pct == 0,
            # contribute 0 to both) — surfaced under its own named key
            # because the spec calls for it by name rather than asking the
            # reader to re-derive it from `avg_return_pct`.
            win_returns = [c["return_pct"] for c in bucket if c["return_pct"] > 0]
            loss_returns = [c["return_pct"] for c in bucket if c["return_pct"] < 0]
            avg_win = sum(win_returns) / len(win_returns) if win_returns else None
            avg_loss = sum(loss_returns) / len(loss_returns) if loss_returns else None
            avg_win_loss_ratio = (
                round(avg_win / abs(avg_loss), 2) if avg_win is not None and avg_loss not in (None, 0) else None
            )
            return {
                "n": n,
                "win_rate_pct": round(wins / n * 100, 1),
                "avg_return_pct": round(avg_ret, 2),
                "avg_hold_days": round(avg_hold, 1),
                "expectancy_pct": round(avg_ret, 2),
                "avg_win_loss_ratio": avg_win_loss_ratio,
            }

        large = [c for c in closed if c["entry_usd"] >= 10_000]
        medium = [c for c in closed if 5_000 <= c["entry_usd"] < 10_000]
        small = [c for c in closed if c["entry_usd"] < 5_000]
        long_closed = [c for c in closed if c["side"] == "long"]
        short_closed = [c for c in closed if c["side"] == "short"]

        def _gated_bucket_stats(bucket: list[dict], label: str) -> dict:
            """Same shape as `_bucket_stats`, but refuses to state a win
            rate / avg return / anything comparable below
            `_CONVICTION_OUTCOME_MIN_N` — see that constant's docstring for
            why this floor is deliberately much higher than the plain
            `_bucket_stats` buckets above (by_size / by_side), which state
            numbers for however many trades they have, however few.

            Below the floor: {"n": n, "insufficient_data": True, "message":
            "..."} — a running count and an explicit statement that n is
            too few to conclude anything, never a win_rate/avg_return key
            at all (not even a fabricated 0.0). At or above it: the full
            `_bucket_stats` shape plus "insufficient_data": False.
            """
            n = len(bucket)
            if n < _CONVICTION_OUTCOME_MIN_N:
                return {
                    "n": n,
                    "insufficient_data": True,
                    "message": (
                        f"only {n} closed trade(s) for {label} — too few to "
                        f"conclude anything about whether conviction "
                        f"predicts outcome (floor is {_CONVICTION_OUTCOME_MIN_N})"
                    ),
                }
            stats = _bucket_stats(bucket)
            stats["insufficient_data"] = False
            return stats

        # by_conviction / by_allocated_risk (spec §7.2) — the grouping this
        # task exists to add. `conviction` is pinned at entry on every BUY/
        # SHORT going forward (see TradeDecision / ExecutionStage); rows
        # from before that landed (or not yet run through
        # scripts/backfill_conviction_ledger.py) carry conviction=None and
        # are counted in `conviction_unknown_n` rather than silently folded
        # into one of the three real labels.
        # Position-level record (see `conviction_positions` for the one rule).
        conv_levels = ("high", "medium", "low")
        conv_known = {lv: [p for p in positions if p["conviction"] == lv and p["pnl_known"]] for lv in conv_levels}
        conv_unknown = {
            lv: sum(1 for p in positions if p["conviction"] == lv and not p["pnl_known"]) for lv in conv_levels
        }
        conviction_unknown_n = sum(1 for p in positions if not p["conviction"])

        # `allocated_risk_pct` is None for every trade built from a legacy
        # notional (target_weight_pct-only) target — which, measured
        # against real production data 2026-08-30, is EVERY entry to date
        # (risk-based sizing has been live since 2026-08-27 but no PM
        # decision has emitted risk_allocation_pct yet). Excluded from the
        # buckets below rather than coerced into one; counted honestly in
        # `allocated_risk_unknown_n`.
        risk_known = [c for c in closed if c["allocated_risk_pct"] is not None]
        by_risk_high = [c for c in risk_known if c["allocated_risk_pct"] >= 3.0]
        by_risk_medium = [c for c in risk_known if 1.0 <= c["allocated_risk_pct"] < 3.0]
        by_risk_low = [c for c in risk_known if c["allocated_risk_pct"] < 1.0]
        allocated_risk_unknown_n = len(closed) - len(risk_known)

        overall = _bucket_stats(closed)
        return {
            **overall,
            "by_size": {
                "large (≥$10k)": _bucket_stats(large),
                "medium ($5-10k)": _bucket_stats(medium),
                "small (<$5k)": _bucket_stats(small),
            },
            "by_side": {
                "long": _bucket_stats(long_closed),
                "short": _bucket_stats(short_closed),
            },
            "by_conviction": {
                lv: {
                    **_gated_bucket_stats(conv_known[lv], f"{lv} conviction"),
                    "wins": sum(1 for p in conv_known[lv] if p["pnl"] > 0),
                    "closed": len(conv_known[lv]),
                    "unknown": conv_unknown[lv],
                }
                for lv in conv_levels
            },
            "conviction_unknown_n": conviction_unknown_n,
            "clean_record_start_utc": CLEAN_RECORD_START_UTC,
            "excluded_before_clean_record_n": excluded_before_clean_record_n,
            "by_allocated_risk": {
                "high (≥3%)": _gated_bucket_stats(by_risk_high, "high allocated risk"),
                "medium (1-3%)": _gated_bucket_stats(by_risk_medium, "medium allocated risk"),
                "low (<1%)": _gated_bucket_stats(by_risk_low, "low allocated risk"),
            },
            "allocated_risk_unknown_n": allocated_risk_unknown_n,
            "lookback_days": lookback_days,
        }

    def get_recent_agent_outputs(self, agent_name: str, limit: int = 5, before_date: str | None = None) -> list[dict]:
        """Thin shim: lifted into `src.storage.analytics.agent_log_reads`,
        which also exposes the pre-cut candidate COUNT the prompt builders
        need. One definition of the ET-to-UTC predicate, there."""
        return recent_agent_outputs(
            conn=self.conn, lock=self._lock, agent_name=agent_name, limit=limit, before_date=before_date
        )

    def get_latest_insights(self, before_date: str | None = None) -> dict | None:
        if before_date:
            sql = "SELECT * FROM insights WHERE date < ? ORDER BY date DESC LIMIT 1"
            params: tuple = (before_date,)
        else:
            sql = "SELECT * FROM insights ORDER BY date DESC LIMIT 1"
            params = ()
        with self._lock:
            row = self.conn.execute(sql, params).fetchone()
        return dict(row) if row else None


def _row_key(row: dict) -> tuple[str, int]:
    return (str(row.get("timestamp") or ""), int(row.get("id") or 0))


def live_opening_row(
    conn: sqlite3.Connection,
    symbol: str,
    openings: tuple[str, ...],
    executed_predicate: str,
    *,
    include_in_flight: bool = False,
) -> dict | None:
    """Newest opening row (action in `openings`) of the position LIVE NOW, or None.

    Reads this symbol's executed rows from `current_account_trades`, so a
    pre-reset row is never seen, and keeps only the live lot group from
    `trade_actions.live_lot_rows` (the fill walk; no broker share count is
    available at the DB layer, so `held=None` lets the ledger's own net
    decide). When the ledger KNOWS the symbol is flat ([]), or the newest
    opening row of the wanted side is not in the live group, returns None: an
    old closed holding is never handed back as the current one. When the fill
    order is UNKNOWN (None) it falls back to the newest opening row in the
    view — the pre-fix answer, which the trail's ratchets-off path relies on.

    `include_in_flight=True` also accepts a submitted / pending_submit
    opening row written at or after the live group's first row — or, on a
    flat ledger, after the newest executed row — i.e. an open the fills have
    not caught up with yet. Older in-flight rows belong to a closed past.
    Caller holds the DB lock.
    """
    from src.quantities import ORDER_QTY_DECIMALS
    from src.trade_actions import live_lot_rows

    rows = [
        dict(r)
        for r in conn.execute(
            f"SELECT * FROM {CURRENT_TRADES_VIEW} WHERE symbol = ? AND {executed_predicate} "
            "ORDER BY timestamp ASC, id ASC",
            (symbol,),
        ).fetchall()
    ]
    live = live_lot_rows(rows, decimals=ORDER_QTY_DECIMALS) if rows else []
    wanted = sorted({a.upper() for a in openings})
    if live is None:
        # Fill order UNKNOWN is a designed case (the trail runs with its R
        # ratchets off), not proof of flat: keep the pre-fix answer, the newest
        # opening row, but from the reset-scoped view.
        candidates = [r for r in rows if str(r.get("action") or "").upper() in wanted]
    else:
        candidates = [r for r in live if str(r.get("action") or "").upper() in wanted]
    if include_in_flight:
        if live is None:
            admits = lambda key: True  # noqa: E731
        elif live:
            floor = min(_row_key(r) for r in live)
            admits = lambda key: key >= floor  # noqa: E731
        elif rows:
            floor = max(_row_key(r) for r in rows)
            admits = lambda key: key > floor  # noqa: E731
        else:
            admits = lambda key: True  # noqa: E731
        placeholders = ", ".join("?" for _ in wanted)
        for r in conn.execute(
            f"SELECT * FROM {CURRENT_TRADES_VIEW} WHERE symbol = ? AND action IN ({placeholders}) "
            "AND fill_status IN ('submitted', 'pending_submit')",
            (symbol, *wanted),
        ).fetchall():
            row = dict(r)
            if admits(_row_key(row)):
                candidates.append(row)
    if not candidates:
        return None
    return max(candidates, key=_row_key)
