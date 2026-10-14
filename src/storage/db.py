import json
import logging
import math
import sqlite3
import threading
import uuid
from datetime import date, datetime, time, timedelta

from src.storage.schema import DatabaseSchema
from src.storage.sector_weights import (
    record_realised_sector_weights as _record_realised_sector_weights,
)
from src.storage.analytics import TradeAnalytics
from src.storage.break_records import BreakRecords, build_break_records
from src.storage.conviction_ledger_store import (
    ConvictionLedgerStore,
    build_conviction_ledger_store,
)
from src.storage.seat_stances import read_seat_stances
from src.storage.target_revisions import (
    TargetRevisionRecords,
    build_target_revision_records,
)
from src.storage.trades import TradeLedger
from src.storage.trades.ledger import (  # re-export mirror: defined there, still importable from here
    _trail_stop_reduced_position,
    _new_position_id,
    _LONG_EXIT_ACTIONS,
    _LONG_EXIT_PREFIXES,
    _SHORT_EXIT_ACTIONS,
    _SHORT_EXIT_PREFIXES,
    _EITHER_SIDE_EXIT_ACTIONS,
    _POSITION_EXIT_ACTIONS,
    _POSITION_EXIT_PREFIXES,
    _is_position_exit_action,
    _exit_action_side,
    _row_counts_as_executed,
    _assign_position_ids,
    _EXIT_TRIGGER_CATEGORIES,
    _UNCATEGORISED_EXIT,
    _categorize_exit_reason,
    _NON_POSITIONAL_ACTIONS,
    _is_exit_family_for_decision_linking,
    _resolve_decision_id_status,
    _extract_pm_targets,
    _find_pm_target_for_symbol,
)
from src.storage.analytics.calibration import (  # re-export mirror: defined there, still importable from here
    _CONVICTION_OUTCOME_MIN_N,
    _POSITION_OPEN_ACTIONS,
    _is_filled_trail_stop,
)
from src.util.time import ET, UTC, et_today
from src.trading_calendar import sql_utc_stamp, sql_utc_stamp_ago, utc_now

logger = logging.getLogger(__name__)


class Database:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self.conn: sqlite3.Connection | None = None
        self._lock = threading.Lock()

    def initialize(self):
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        # WAL allows concurrent readers alongside the writer — avoids occasional
        # "database is locked" when parallel agent threads each insert logs.
        # No-op for :memory: databases (stays "memory" journal).
        try:
            self.conn.execute("PRAGMA journal_mode=WAL")
        except sqlite3.DatabaseError:
            pass
        # synchronous=NORMAL is the trading-appropriate fsync mode under
        # WAL: WAL file is synced on every commit, main DB is synced at
        # checkpoint. SQLite default (FULL) syncs both on every commit
        # which is overkill for our workload (15-25 trades / day; agent
        # logs are best-effort observability — losing the last few rows
        # on a hard power loss would be acceptable). NORMAL also reduces
        # commit latency that becomes noticeable during evening's
        # multi-write transaction. Safe under WAL because corruption
        # requires both a hard power loss AND a torn write to the WAL
        # itself (extremely rare).
        try:
            self.conn.execute("PRAGMA synchronous=NORMAL")
        except sqlite3.DatabaseError:
            pass
        # busy_timeout — the default is 0 (raise OperationalError instantly
        # on any lock contention). At 09:30 ET, the morning session and
        # intra_check fire simultaneously; intra_check is exempt from the
        # bash-level session lock (CLAUDE.md "Cross-mode session lock" —
        # intra is the flash-crash circuit breaker and must run every tick).
        # Both Python processes contend at the SQLite WAL level. The
        # threading.Lock above serializes within a single process but does
        # nothing across processes. A 5000ms wait window covers the
        # observed worst-case WAL→checkpoint stall (~1-2s on a busy day)
        # plus headroom. Set BEFORE _create_tables so the CREATE statements
        # also benefit if a concurrent reader is active during first init.
        try:
            self.conn.execute("PRAGMA busy_timeout=5000")
        except sqlite3.DatabaseError:
            pass
        self._create_tables()

    def _locked_write(self, do, *, label: str = "write"):
        """Run a write closure under the process lock with bounded retry on
        cross-process SQLite lock contention.

        busy_timeout (5s) only covers the lock-WAIT; a WAL checkpoint stall
        longer than that surfaces as `OperationalError: database is locked`
        AFTER the wait expires. The bare execute path would then either raise
        (trade / recovery-queue inserts) or silently lose the row
        (agent_logs). intra_check is explicitly exempt from the cross-mode
        session lock (CLAUDE.md), so it WILL write concurrently with a long
        morning — the in-process threading.Lock serializes only within THIS
        process; this retry is what protects the write across processes.

        ~1.55s of extra backoff on top of the 5s busy_timeout; if still
        locked after that, re-raise (a stuck DB is a real problem worth
        surfacing, not silently dropping).
        """
        import time as _time

        last_exc: sqlite3.OperationalError | None = None
        for attempt in range(5):
            try:
                with self._lock:
                    return do()
            except sqlite3.OperationalError as exc:
                msg = str(exc).lower()
                if "locked" not in msg and "busy" not in msg:
                    raise
                last_exc = exc
                logger.warning(
                    "DB %s contended (attempt %d/5): %s — retrying",
                    label,
                    attempt + 1,
                    exc,
                )
                _time.sleep(0.05 * (2**attempt))  # 0.05,0.1,0.2,0.4,0.8s
        logger.error("DB %s still locked after retries — giving up: %s", label, last_exc)
        raise last_exc

    def _create_tables(self):
        """Thin shim: body moved verbatim to src/storage/schema/manager.py (built per call)."""
        return DatabaseSchema(conn=self.conn)._create_tables()

    def _migrate(self):
        """Thin shim: body moved verbatim to src/storage/schema/manager.py (built per call)."""
        return DatabaseSchema(conn=self.conn)._migrate()

    def execute(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        with self._lock:
            return self.conn.execute(sql, params)

    def save_evening_snapshot(
        self,
        *,
        date: str,
        total_value: float,
        daily_pnl: float,
        daily_return_pct: float,
        equity_close: float | None = None,
        tomorrow_outlook: str,
        lessons: str,
        suggested_actions,
        risk_rating: str,
        tomorrow_bias: str = "neutral",
        tomorrow_conviction: str = "medium",
        tomorrow_key_risks=(),
        sell_decisions_assessment: str = "",
        sell_grades=(),
        buy_grades=(),
        missed_opportunities=(),
        thesis_updates=(),
        selection_rules=(),
        discipline_notes=(),
        previous_outlook_assessment: str = "",
    ) -> None:
        """Atomically write the evening's daily_pnl + insights rows.

        Phase 4 #5: transaction boundary. These two writes are two sides
        of the same fact ("here's today's P&L; here's the narrative I
        wrote about it") — if the process crashes between them, next
        morning's PM reads inconsistent state. Doing both in one BEGIN /
        COMMIT prevents that split-brain.

        All writes happen under the same _lock acquisition, matching the
        pattern used by the single-write insert methods. Callers should
        treat this as the sanctioned way to persist evening output.

        sell_grades / buy_grades are stored as JSON-serialized lists
        (list[dict] or list[Pydantic]). `_build_sell_calibration_summary`
        aggregates them into counts for position_reviewer's prompt.

        thesis_updates / selection_rules / discipline_notes are the
        "Structured lesson categories" (list[str]) — defect (d) fix.
        Previously declared on EveningReport and asked for in the evening
        prompt, but dropped here before ever reaching disk.
        `portfolio_manager.build_user_message` reads them back the next
        session via `yesterday_insights`. previous_outlook_assessment is
        evening's prose self-grade of its own PRIOR night's forecast — kept
        for the audit trail; no prompt currently reads it back.
        """
        import json

        def _to_json_list(val) -> str:
            if isinstance(val, str):
                return val or "[]"
            if not val:
                return "[]"
            out = []
            for item in val:
                if hasattr(item, "model_dump"):
                    out.append(item.model_dump())
                elif isinstance(item, dict):
                    out.append(item)
            return json.dumps(out)

        actions_json = json.dumps(suggested_actions) if isinstance(suggested_actions, list) else suggested_actions
        risks_json = (
            json.dumps(list(tomorrow_key_risks)) if not isinstance(tomorrow_key_risks, str) else tomorrow_key_risks
        )
        sell_grades_json = _to_json_list(sell_grades)
        buy_grades_json = _to_json_list(buy_grades)
        missed_opportunities_json = _to_json_list(missed_opportunities)

        def _to_json_str_list(val) -> str:
            # thesis_updates/selection_rules/discipline_notes are
            # list[str], not list[dict-like] — `_to_json_list` above would
            # silently drop plain strings (it only keeps items with
            # `.model_dump()` or `isinstance(item, dict)`). Mirrors the
            # existing tomorrow_key_risks handling below.
            if isinstance(val, str):
                return val or "[]"
            if not val:
                return "[]"
            return json.dumps([str(item) for item in val])

        thesis_updates_json = _to_json_str_list(thesis_updates)
        selection_rules_json = _to_json_str_list(selection_rules)
        discipline_notes_json = _to_json_str_list(discipline_notes)
        with self._lock:
            try:
                self.conn.execute("BEGIN")
                # Upsert that PRESERVES a previously-stored equity_close when
                # this write carries None — a documented same-day evening re-run
                # whose portfolio_history fetch failed must not wipe the 4pm
                # close captured by the first run. COALESCE keeps the old value.
                self.conn.execute(
                    "INSERT INTO daily_pnl "
                    "(date, total_value, daily_pnl, daily_return_pct, equity_close) "
                    "VALUES (?, ?, ?, ?, ?) "
                    "ON CONFLICT(date) DO UPDATE SET "
                    "total_value=excluded.total_value, "
                    "daily_pnl=excluded.daily_pnl, "
                    "daily_return_pct=excluded.daily_return_pct, "
                    "equity_close=COALESCE(excluded.equity_close, daily_pnl.equity_close)",
                    (date, total_value, daily_pnl, daily_return_pct, equity_close),
                )
                self.conn.execute(
                    "INSERT OR REPLACE INTO insights "
                    "(date, tomorrow_outlook, lessons, suggested_actions, risk_rating, "
                    "tomorrow_bias, tomorrow_conviction, tomorrow_key_risks, "
                    "sell_decisions_assessment, sell_grades_json, buy_grades_json, "
                    "missed_opportunities_json, thesis_updates_json, "
                    "selection_rules_json, discipline_notes_json, "
                    "previous_outlook_assessment) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        date,
                        tomorrow_outlook,
                        lessons,
                        actions_json,
                        risk_rating,
                        tomorrow_bias,
                        tomorrow_conviction,
                        risks_json,
                        sell_decisions_assessment or "",
                        sell_grades_json,
                        buy_grades_json,
                        missed_opportunities_json,
                        thesis_updates_json,
                        selection_rules_json,
                        discipline_notes_json,
                        previous_outlook_assessment or "",
                    ),
                )
                self.conn.commit()
            except Exception:
                self.conn.rollback()
                raise

    # ---- bounded entry re-peg write-ahead queue -------------------------
    #
    # Written BEFORE the replace PATCH, resolved after. See the
    # `pending_repegs` DDL for why the window needs a durable record at all.

    @staticmethod
    def _executed_trade_predicate() -> str:
        """SQL predicate for trades that executed at least some quantity."""
        return "((fill_status IS NULL AND action != 'HOLD') OR fill_status = 'filled' OR COALESCE(fill_qty, 0) > 0)"

    @staticmethod
    def _sqlite_utc_timestamp(when: datetime) -> str:
        """SQLite's stored format for `when`; the body lives in src.trading_calendar."""
        return sql_utc_stamp(when)

    @classmethod
    def _et_day_utc_bounds(cls, trading_day: date | None = None) -> tuple[str, str]:
        """UTC timestamp bounds [start, end) for an ET trading-day date."""
        day = trading_day or et_today()
        start_et = datetime.combine(day, time.min, tzinfo=ET)
        end_et = start_et + timedelta(days=1)
        return cls._sqlite_utc_timestamp(start_et), cls._sqlite_utc_timestamp(end_et)

    def record_alignment_exit_reading(
        self,
        *,
        symbol: str,
        verdict,
        run_id: str | None = None,
        is_short: bool | None = None,
        session_date: str | None = None,
        not_evaluated_reason: str | None = None,
    ) -> bool:
        """Record one open position's alignment-exit reading for this run.

        ITEM 75 RECORDING, RECORDING ONLY, and it decides nothing. Read the
        `alignment_exit_readings` note in `_migrate` for why it exists (a
        trim fraction cannot be derived from a population the desk never
        kept) and for the hard limit on its use: nothing may read it back
        into a sizing, stop or exit decision, and it may NEVER be swept for
        the trim fraction that would have performed best.

        `verdict` is the `AlignmentExitCheck` the exit ALREADY produced for
        this symbol this run — passed in rather than recomputed, so this
        write cannot disagree with the decision the desk acted on. EVERY
        open position is written EVERY run, fired or not: the whole point
        is the sessions the exit does NOT fire, which leave no trace today.

        THIS RECORDING BUYS NO DATA TO FILL ITSELF. A chart read is a live
        `yfinance` download (`market.get_ohlcv`, uncached), so a position
        the scan did not already evaluate is written with `verdict=None`
        and an explicit `not_evaluated_reason`, leaving every reading
        column NULL. A later reader needs those rows to know its own
        denominator, and must not read a NULL reading as an intact chart.

        Unknown stays NULL. A HOLD with nothing given up carries no
        distance, and an UNPARSEABLE read carries no distance and no band;
        neither is filled with a zero or an assumed value, because "price
        is exactly at the mark", "the chart was intact" and "the chart
        could not be read" are three different facts.

        Idempotent per run per symbol (UNIQUE on `run_id, symbol`): a
        second write for the same pair replaces the first rather than
        double-counting a position.
        """
        sym = (symbol or "").strip().upper()
        if not sym or (verdict is None and not not_evaluated_reason):
            return False

        def _num(x):
            try:
                v = float(x)
            except (TypeError, ValueError):
                return None
            return v if math.isfinite(v) else None

        last_mark = getattr(verdict, "last_mark", None)
        marks = getattr(verdict, "marks", None) or ()
        sessions = getattr(verdict, "sessions_since_mark_lost", None)
        try:
            sessions = int(sessions) if sessions is not None else None
        except (TypeError, ValueError):
            sessions = None
        period = getattr(verdict, "thesis_ma_period", None)
        try:
            period = int(period) if period is not None else None
        except (TypeError, ValueError):
            period = None
        try:
            with self._lock:
                self.conn.execute(
                    "INSERT OR REPLACE INTO alignment_exit_readings ("
                    "  timestamp, run_id, session_date, symbol, is_short,"
                    "  status, code, breach_atrs, band_atrs,"
                    "  sessions_since_mark_lost, last_mark_price,"
                    "  last_mark_source, marks_count, thesis_ma_period,"
                    "  thesis_ma_kind, not_evaluated_reason"
                    ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        sql_utc_stamp(),
                        run_id or None,
                        session_date or str(et_today()),
                        sym,
                        None if is_short is None else int(bool(is_short)),
                        getattr(verdict, "status", None),
                        getattr(verdict, "code", None),
                        _num(getattr(verdict, "breach_atrs", None)),
                        _num(getattr(verdict, "band_atrs", None)),
                        sessions,
                        _num(getattr(last_mark, "price", None)),
                        getattr(last_mark, "source", None),
                        len(marks) if verdict is not None else None,
                        period,
                        (getattr(verdict, "thesis_ma_kind", "") or "") or None,
                        not_evaluated_reason or None,
                    ),
                )
                self.conn.commit()
            return True
        except Exception as e:  # noqa: BLE001 — a recording never blocks a trade
            logger.warning(
                "alignment-exit reading for %s was not recorded (%s)",
                sym,
                e,
            )
            return False

    def record_soft_exit_heal_restores(
        self,
        *,
        observations,
        run_id: str | None = None,
        session_date: str | None = None,
        dropped: int = 0,
    ) -> int:
        """Record what the mechanical soft-exit heal did. Returns rows written.

        ITEM 78 RECORDING, RECORDING ONLY, and it decides nothing: these
        rows may never be read back into a trading decision nor swept for
        a threshold. Unknown stays NULL throughout. One row per DISTINCT
        identity, `occurrences` counting how many times it was seen; see
        `src/soft_exit_restore_buffer.py`.
        """
        rows = []
        first = True
        for obs in list(observations or []):
            if not isinstance(obs, dict):
                continue
            sym = obs.get("symbol")
            sym = sym.strip().upper() if isinstance(sym, str) and sym.strip() else None
            blank = obs.get("blank_found")
            healed = obs.get("healed")
            src = obs.get("source")
            rows.append(
                (
                    sql_utc_stamp(),
                    run_id or None,
                    session_date or str(et_today()),
                    sym,
                    None if blank is None else int(bool(blank)),
                    None if healed is None else int(bool(healed)),
                    src if isinstance(src, str) and src.strip() else None,
                    int(dropped) if first else None,
                    max(1, int(obs.get("occurrences") or 1)),
                )
            )
            first = False
        if not rows:
            return 0
        try:
            with self._lock:
                self.conn.executemany(
                    "INSERT INTO soft_exit_heal_restores ("
                    "  timestamp, run_id, session_date, symbol,"
                    "  blank_found, healed, source, dropped_before, occurrences"
                    ") VALUES (?,?,?,?,?,?,?,?,?)",
                    rows,
                )
                self.conn.commit()
            return len(rows)
        except Exception as e:  # noqa: BLE001 — a recording never blocks a trade
            logger.warning("soft-exit heal restores were not recorded (%s)", e)
            return 0

    #: The unit and denominator every `realised_sector_weights` row is
    #: expressed in, stored on the row itself. Same unit and same
    #: denominator as item 222's single-name ceiling; not a second
    #: convention.
    REALISED_SECTOR_WEIGHT_DENOMINATOR = (
        "percent of total account equity, raw position notional, before the gross multiplier"
    )

    record_realised_sector_weights = _record_realised_sector_weights

    def insert_agent_log(
        self,
        agent_name: str,
        run_id: str,
        input_summary: str,
        output_summary: str,
        full_response: str,
        model: str,
        tokens_used: int,
        input_message: str = "",
        input_tokens: int | None = None,
        output_tokens: int | None = None,
        cost_usd: float | None = None,
        provider_requests: int | None = None,
        requested_provider: str | None = None,
        requested_model: str | None = None,
        actual_provider: str | None = None,
        prompt_version: str | None = None,
        latency_s: float | None = None,
        status: str | None = None,
        finish_reason: str | None = None,
        truncated: bool | None = None,
        decision_id: str | None = None,
        acceptance: str | None = None,
        acceptance_reason: str | None = None,
        telemetry: str | None = None,
    ):
        """`model` remains the ACTUAL responding model (Stage 0.5 contract —
        unchanged). The Stage 1 kwargs below are additive and all default to
        None so every pre-Stage-1 caller keeps working unmodified; omitting
        them persists NULL, never a fabricated value."""

        def _do():
            self.conn.execute(
                """INSERT INTO agent_logs (agent_name, run_id, input_summary, input_message,
                   output_summary, full_response, model, tokens_used,
                   input_tokens, output_tokens, cost_usd,
                   provider_requests,
                   requested_provider, requested_model, actual_provider,
                   prompt_version, latency_s, status, finish_reason, truncated,
                   decision_id, acceptance, acceptance_reason, telemetry)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    agent_name,
                    run_id,
                    input_summary,
                    input_message,
                    output_summary,
                    full_response,
                    model,
                    tokens_used,
                    input_tokens,
                    output_tokens,
                    cost_usd,
                    provider_requests,
                    requested_provider,
                    requested_model,
                    actual_provider,
                    prompt_version,
                    latency_s,
                    status,
                    finish_reason,
                    None if truncated is None else int(truncated),
                    decision_id,
                    acceptance,
                    acceptance_reason,
                    telemetry,
                ),
            )
            self.conn.commit()

        self._locked_write(_do, label="insert_agent_log")

    def insert_specialist_evidence(
        self,
        *,
        run_id: str,
        agent_name: str,
        kind: str,
        scope: str,
        evidence_json: str,
        symbol: str | None = None,
        decision_id: str | None = None,
    ) -> int:
        """Persist one already-VALIDATED structured evidence row (Stage 4).

        Purely additive/observational — see the table's CREATE comment.
        Callers (pipeline_stages.py) are expected to wrap this in their own
        try/except so a persistence hiccup here can never affect the
        research/decision/risk flow it's recording; this method itself does
        not swallow errors (matches every other insert_* method's contract),
        it just never touches trading-critical state.
        """

        def _do():
            cur = self.conn.execute(
                "INSERT INTO specialist_evidence "
                "(run_id, decision_id, agent_name, kind, scope, symbol, evidence_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (run_id, decision_id, agent_name, kind, scope, symbol, evidence_json),
            )
            self.conn.commit()
            return cur.lastrowid or 0

        return self._locked_write(_do, label="insert_specialist_evidence")

    def count_paid_seat_heals_today(self, seat: str, *, trading_day: date | None = None) -> int | None:
        """How many PAID research heals this seat has already had today (ET).

        The in-context counter `RunContext.heal_paid_retries` says "at most
        one paid retry per seat per session", but a RunContext lives for ONE
        tick and `intra_check` fires every 30 minutes from 09:30 to 16:00 ET
        — fourteen ticks, each with its own fresh counter. For a seat that
        expires because the wire moved, the wire is still moved on the next
        tick, so the in-context cap would have allowed the desk to buy the
        same seat back fourteen times in a day and call that "one retry".
        Nothing caught it before because the heal path was unreachable for
        expired seats at all (see `evidence_gate.HEALABLE_CATEGORIES`).

        The durable heal rows are the only cross-tick memory the heal path
        has, so the day cap is read back from them. Row volume is a handful
        per day; parsing in Python avoids depending on the JSON1 extension.
        Returns None — NOT 0 — when the read fails, so the caller can tell
        "nothing spent today" apart from "I could not find out". Those need
        different answers and a shared 0 forced one global policy on both.
        `trading_day` exists so the ET-day boundary itself is testable; the
        default is today.
        """
        import json as _json

        want = str(seat or "").strip()
        if not want:
            return 0
        start, end = self._et_day_utc_bounds(trading_day)
        try:
            with self._lock:
                rows = self.conn.execute(
                    "SELECT evidence_json FROM specialist_evidence "
                    "WHERE kind = 'seat_heal' AND timestamp >= ? AND timestamp < ?",
                    (start, end),
                ).fetchall()
        except Exception as e:  # noqa: BLE001
            logger.warning("count_paid_seat_heals_today failed: %s", e)
            return None
        count = 0
        for row in rows:
            try:
                payload = _json.loads(row[0])
            except Exception:  # noqa: BLE001
                continue
            if not isinstance(payload, dict):
                continue
            if str(payload.get("seat") or "") != want:
                continue
            if payload.get("paid_retry") is True:
                count += 1
        return count

    def record_acted_exit_trigger(
        self,
        *,
        run_id: str,
        payload_json: str,
        symbol: str,
    ) -> int:
        """Persist WHAT authorised a sell-side action the desk submitted.

        Board item 74. The trades table carries only free-text `reasoning`,
        so nothing durable said which `exit_trigger` fired or which record
        it rested on — and a later session therefore could not tell a
        second cut on the SAME event from one on a new event. This row is
        that memory. Append-only, read back by
        `get_acted_exit_triggers_today`; the trading path never mutates it.
        """
        from src.risk.spent_trigger import ACTED_TRIGGER_KIND

        return self.insert_specialist_evidence(
            run_id=run_id,
            agent_name="position_reviewer",
            kind=ACTED_TRIGGER_KIND,
            scope="symbol",
            symbol=symbol,
            evidence_json=payload_json,
        )

    def get_acted_exit_triggers_today(
        self,
        *,
        trading_day: date | None = None,
    ) -> list[dict] | None:
        """Every sell-side action submitted today (ET), with its trigger.

        Returns None — NOT [] — when the read fails, so the caller can tell
        "nothing acted today" apart from "I could not find out". Those need
        different answers: the first permits the cut, the second is
        uncertainty and fails OPEN without pretending to have checked.
        """
        import json as _json
        from src.risk.spent_trigger import ACTED_TRIGGER_KIND

        start, end = self._et_day_utc_bounds(trading_day)
        try:
            with self._lock:
                rows = self.conn.execute(
                    "SELECT evidence_json FROM specialist_evidence "
                    "WHERE kind = ? AND timestamp >= ? AND timestamp < ? "
                    "ORDER BY id ASC",
                    (ACTED_TRIGGER_KIND, start, end),
                ).fetchall()
        except Exception as e:  # noqa: BLE001
            logger.warning("get_acted_exit_triggers_today failed: %s", e)
            return None
        out: list[dict] = []
        for row in rows:
            try:
                payload = _json.loads(row[0])
            except Exception:  # noqa: BLE001
                continue
            if isinstance(payload, dict):
                out.append(payload)
        return out

    def latest_news_analysis_today(
        self,
        *,
        trading_day: date | None = None,
    ) -> str | None:
        """The newest news-seat answer the desk PAID for today (ET), as JSON.

        Why this exists, 2026-09-23. The intraday carry-forward reads the
        news seat from `data/news/<ET day>/full_report.json`, which only the
        three scheduled sessions ever write. A paid heal
        (`TradingPipeline._try_one_paid_research_retry`) writes its answer to
        `specialist_evidence` and nowhere else, so every later tick re-loaded
        the SUPERSEDED morning file, re-expired the seat, and — since the
        per-ET-day cap landed — then refused to buy it again. The desk paid
        for a fresher read at 10:00 and ran the rest of the day with the
        news seat unset. Production: 8 paid news heals on 2026-09-18, each
        ~30 minutes apart, every one discarded by the next tick.

        The row this reads is not heal-specific and deliberately so: the
        ordinary morning/midday/evening reads write the SAME
        `agent_name='news_analyst', kind='analysis', scope='run'` row
        (`src/pipeline_stages.py`). "Newest such row today" therefore means
        "the freshest paid news read the desk holds", which is a property of
        the evidence, not of how it was bought. On a tick with no heal it
        returns the same content the file holds, so the caller's merge is a
        no-op.

        The ET-day bound is the SAME bound the dated report directory
        already has — no clock and no new lifetime; see
        `src.evidence_kind.news_reuse`, which still decides expiry.

        Returns None — not "" — when there is no such row or the read fails,
        so the caller falls back to the file rather than treating a sick
        forensic store as "the desk has no news".
        """
        start, end = self._et_day_utc_bounds(trading_day)
        try:
            with self._lock:
                row = self.conn.execute(
                    "SELECT evidence_json FROM specialist_evidence "
                    "WHERE kind = 'analysis' AND agent_name = 'news_analyst' "
                    "AND scope = 'run' AND timestamp >= ? AND timestamp < ? "
                    "ORDER BY timestamp DESC, id DESC LIMIT 1",
                    (start, end),
                ).fetchone()
        except Exception as e:  # noqa: BLE001
            logger.warning("latest_news_analysis_today failed: %s", e)
            return None
        if not row or not row[0]:
            return None
        return str(row[0])

    # --- Conviction ledger (spec §9.5) -----------------------------------
    #
    # §9.1/§9.2 already persisted every raw nomination as a `pipeline_event`
    # evidence row (outcome='nominated', carrying seat/conviction/observation)
    # — but with decision_id NULL, because `RunContext.decision_id` is not
    # minted until DecisionStage, which runs AFTER the nomination responder
    # pass. Provenance existed; the JOIN did not, so no nomination could be
    # traced to the trade it became or scored against an outcome.
    #
    # Three additions close that, all inside the existing
    # `specialist_evidence` table (no new store — the table already carries
    # per-symbol, per-decision evidence and is already pruned, indexed on
    # decision_id, and documented as non-authoritative forensic display):
    #
    #   1. `link_nominations_to_decision` back-fills decision_id onto this
    #      run's nomination rows once DecisionStage has minted one.
    #   2. `record_seat_stances` writes kind='seat_stance' — every seat's
    #      side per idea, dissent as well as support.
    #   3. `resolve_conviction_ledger` scores closed positions into
    #      kind='conviction_credit' rows, read back by
    #      `get_conviction_credits` with no recomputation.
    #
    # ALL of it is bookkeeping. Nothing in this section is read by the
    # trading decision chain, and the writes are best-effort at every call
    # site (`.claude/rules/trading-core.md`: a forensic-persistence failure
    # must never relax or alter a deterministic decision).

    NOMINATION_KIND = "pipeline_event"
    SEAT_STANCE_KIND = "seat_stance"

    def link_nominations_to_decision(self, *, run_id: str, decision_id: str) -> int:
        """Back-fill `decision_id` onto this run's unjoined nomination rows.

        Spec §9.5. Targets exactly the rows `_record_pipeline_event` wrote
        with outcome='nominated' for `run_id` that carry no decision_id yet —
        never any other evidence row, and never one already joined (so a
        retry, or a second call in the same run, is a no-op). Returns the
        number of rows updated.

        A run has one PM call and therefore one decision_id, which is what
        makes the back-fill unambiguous: `trades.decision_id` +
        `trades.symbol` then join straight to the nomination that raised the
        symbol. Rows from a run whose PM never produced a decision stay NULL,
        which is correct — there was no decision for them to point at.

        Deliberately NOT a change to when nominations are recorded. Moving
        the nomination write after DecisionStage would put a forensic
        concern inside the decision path and reorder evidence relative to the
        responder pass that consumes it; an UPDATE afterwards touches nothing
        the pipeline reads.
        """
        if not run_id or not decision_id:
            return 0

        def _do():
            cur = self.conn.execute(
                "UPDATE specialist_evidence SET decision_id = ? "
                "WHERE run_id = ? AND decision_id IS NULL "
                "AND kind = ? AND agent_name = 'pipeline' "
                "AND json_extract(evidence_json, '$.outcome') = 'nominated'",
                (decision_id, run_id, self.NOMINATION_KIND),
            )
            self.conn.commit()
            return cur.rowcount or 0

        return self._locked_write(_do, label="link_nominations_to_decision")

    def get_nominations_for_decision(self, decision_id: str) -> list[dict]:
        """Every nomination row now joined to `decision_id`, oldest first.

        The read side of the join: given a trade's decision_id, which seats
        nominated which symbols in the run that produced it.
        """
        if not decision_id:
            return []
        with self._lock:
            rows = self.conn.execute(
                "SELECT symbol, evidence_json, run_id, timestamp "
                "FROM specialist_evidence "
                "WHERE decision_id = ? AND kind = ? AND agent_name = 'pipeline' "
                "AND json_extract(evidence_json, '$.outcome') = 'nominated' "
                "ORDER BY timestamp, id",
                (decision_id, self.NOMINATION_KIND),
            ).fetchall()
        return [dict(r) for r in rows]

    def get_rotation_sell_symbols_today(self) -> set[str]:
        """Every symbol the rotation CLOSED on today's exchange day.

        Read side of the `rotation` / `sell_submitted` pipeline event the
        execution stage writes the moment a rotation SELL is broker-
        accepted. The desk's BUY-side anti-churn guard reads this so a name
        sold at 10:00 for failing the entry bar is not bought back at
        11:00. Exchange-day bounds, the same ones `get_trades(today_only=
        True)` uses — no new window, no cooldown.
        """
        start_utc, end_utc = self._et_day_utc_bounds()
        with self._lock:
            rows = self.conn.execute(
                "SELECT DISTINCT symbol FROM specialist_evidence "
                "WHERE kind = ? AND agent_name = 'pipeline' "
                "AND symbol IS NOT NULL "
                "AND json_extract(evidence_json, '$.stage') = 'rotation' "
                "AND json_extract(evidence_json, '$.outcome') = "
                "'sell_submitted' "
                "AND timestamp >= ? AND timestamp < ?",
                (self.NOMINATION_KIND, start_utc, end_utc),
            ).fetchall()
        return {str(r[0]).strip().upper() for r in rows if r[0]}

    def record_seat_stances(
        self,
        *,
        run_id: str,
        decision_id: str,
        stances,
    ) -> int:
        """Persist each seat's side on each idea — dissent as well as support.

        `stances` is an iterable of `src.conviction_ledger.SeatStance`. One
        row per (symbol, seat), `agent_name` set to the canonical seat name so
        the ledger reads per analyst. Returns the number of rows written.

        The stance itself is NOT re-derived here: it is the canonical
        evidence-registry stance the §9.4 agreement gate already counts
        (`PortfolioManagerAgent.build_evidence_registry`), so what the ledger
        scores and what sizing counted are the same fact.
        """
        import json as _json

        written = 0
        for stance in stances or []:
            payload = {
                "seat": stance.seat,
                "symbol": stance.symbol,
                "stance": stance.stance,
                "conviction": stance.conviction,
                "nominated": bool(stance.nominated),
                "observation": stance.observation,
            }
            self.insert_specialist_evidence(
                run_id=run_id,
                decision_id=decision_id,
                agent_name=stance.seat,
                kind=self.SEAT_STANCE_KIND,
                scope="symbol",
                symbol=stance.symbol,
                evidence_json=_json.dumps(payload, sort_keys=True),
            )
            written += 1
        return written

    def get_seat_stances(
        self,
        *,
        decision_id: str,
        symbol: str | None = None,
    ) -> list:
        """Reconstruct `SeatStance` objects for one decision — body now
        `src.storage.seat_stances.read_seat_stances`, shared with the
        conviction ledger store so the two reads cannot drift apart."""
        return read_seat_stances(
            self.conn,
            self._lock,
            decision_id=decision_id,
            symbol=symbol,
        )

    @property
    def conviction(self) -> ConvictionLedgerStore:
        """Per-call construction so a collaborator swapped after __init__ is still reached."""
        return build_conviction_ledger_store(conn=self.conn, lock=self._lock)

    # --- Position-reviewer memory (spec Phase 3.2 / audit §1.5) -----------
    #
    # `_build_own_recent_decisions` replays past ACTIONS and explicitly drops
    # HOLDs, so the reviewer rebuilt its view of every position from scratch
    # twice a day with no idea what it had measured six hours earlier. On
    # 2026-08-26 it sold EPD for "not progressing" when progress had risen
    # 16% -> 20% and distance-to-stop had improved since its own midday read.
    # These two methods give the seat a memory of its own numbers.

    POSITION_REVIEW_METRIC_KIND = "review_metrics"

    def save_position_review_metrics(
        self,
        *,
        run_id: str,
        symbol: str,
        metrics_json: str,
    ) -> int:
        """Snapshot one position's deterministic metrics for the next review."""
        return self.insert_specialist_evidence(
            run_id=run_id,
            agent_name="position_reviewer",
            kind=self.POSITION_REVIEW_METRIC_KIND,
            scope="symbol",
            symbol=symbol.upper(),
            evidence_json=metrics_json,
        )

    def get_prior_position_review_metrics(
        self,
        symbols,
        *,
        exclude_run_id: str | None = None,
    ) -> dict[str, dict]:
        """Most recent prior metric snapshot per symbol, as {symbol: row}.

        `exclude_run_id` drops the current run's own rows so a re-entrant or
        retried review compares against the LAST session, never against the
        snapshot it just wrote. Each returned row carries `evidence_json` and
        `timestamp`; parsing is the caller's job so a single malformed blob
        cannot take down the read.
        """
        wanted = [str(s).strip().upper() for s in symbols if str(s).strip()]
        if not wanted:
            return {}
        placeholders = ",".join("?" for _ in wanted)
        sql = (
            "SELECT symbol, evidence_json, timestamp, run_id FROM specialist_evidence "
            f"WHERE agent_name='position_reviewer' AND kind=? AND symbol IN ({placeholders})"
        )
        params: list = [self.POSITION_REVIEW_METRIC_KIND, *wanted]
        if exclude_run_id:
            sql += " AND run_id != ?"
            params.append(exclude_run_id)
        sql += " ORDER BY timestamp DESC, id DESC"
        with self._lock:
            rows = self.conn.execute(sql, tuple(params)).fetchall()
        latest: dict[str, dict] = {}
        for row in rows:
            row = dict(row)
            # Rows arrive newest-first, so the first sighting of a symbol wins.
            latest.setdefault(row["symbol"], row)
        return latest

    def get_latest_symbol_evidence(self, kind: str, symbols) -> dict[str, dict]:
        """Newest `specialist_evidence` row of `kind` per symbol, as
        {symbol: row}. Generic twin of `get_prior_position_review_metrics`
        for the stop-side records in `src/execution/exit_path_records.py`,
        which compare a new reason against the last one on file."""
        wanted = [str(s).strip().upper() for s in symbols if str(s).strip()]
        if not wanted:
            return {}
        placeholders = ",".join("?" for _ in wanted)
        sql = (
            "SELECT symbol, evidence_json, timestamp, run_id FROM specialist_evidence "
            f"WHERE kind=? AND symbol IN ({placeholders}) "
            "ORDER BY timestamp DESC, id DESC"
        )
        with self._lock:
            rows = self.conn.execute(sql, (kind, *wanted)).fetchall()
        latest: dict[str, dict] = {}
        for row in rows:
            row = dict(row)
            latest.setdefault(row["symbol"], row)
        return latest

    # --- Holding-discipline structural-protection memory (spec item 25,
    # owner refinements 2026-09-04) ------------------------------------
    #
    # A thesis/level break only lifts protection once it shows up on the
    # DAILY CLOSE of two CONSECUTIVE TRADING DAYS — see
    # `src.risk.exit_guard.check_structural_protection`'s "CONFIRMATION
    # GATE" docstring for why (a same-day intrabar wick or a one-day
    # "spring" false-breakdown must not read as a real break). That
    # function is pure and holds no state itself; this is the one place
    # across days that "was this position's thesis/level basis already
    # broken on its last completed close" is remembered, following the same
    # read/write shape as `save_position_review_metrics` /
    # `get_prior_position_review_metrics` just above, keyed additionally by
    # `bar_date` — the date of the CLOSE the read was judged against, not
    # the wall-clock time the pipeline happened to run — so that several
    # intraday pipeline cycles re-reading the SAME day's close never get
    # miscounted as two separate confirming days.

    HOLDING_PROTECTION_BREAK_KIND = BreakRecords.HOLDING_PROTECTION_BREAK_KIND
    DELEVER_CEILING_STATE_KIND = BreakRecords.DELEVER_CEILING_STATE_KIND
    TARGET_LEVEL_BREAK_KIND = BreakRecords.TARGET_LEVEL_BREAK_KIND
    TARGET_REVISION_KIND = TargetRevisionRecords.TARGET_REVISION_KIND

    @property
    def breaks(self) -> BreakRecords:
        """Build the cross-session record store from current collaborators."""
        return build_break_records(conn=self.conn, lock=self._lock)

    @property
    def target_revisions(self) -> TargetRevisionRecords:
        """Build the target-revision store from current collaborators."""
        return build_target_revision_records(conn=self.conn, lock=self._lock)

    def save_holding_protection_break(
        self,
        *,
        run_id: str,
        symbol: str,
        raw_broken: bool,
        bar_date: str,
        close: float | None = None,
        basis: str | None = None,
        detail: str | None = None,
    ) -> int:
        """Compatibility facade; the implementation belongs to `breaks`."""
        return self.breaks.save_holding_protection_break(
            run_id=run_id,
            symbol=symbol,
            raw_broken=raw_broken,
            bar_date=bar_date,
            close=close,
            basis=basis,
            detail=detail,
        )

    def get_prior_holding_protection_break(
        self,
        symbols,
        *,
        today_bar_date: str,
        exclude_run_id: str | None = None,
    ) -> dict[str, bool]:
        """Compatibility facade; the implementation belongs to `breaks`."""
        return self.breaks.get_prior_holding_protection_break(
            symbols,
            today_bar_date=today_bar_date,
            exclude_run_id=exclude_run_id,
        )

    def get_recent_holding_protection_breaks(
        self,
        symbol: str,
        *,
        before_bar_date: str,
        exclude_run_id: str | None = None,
        limit: int = 30,
    ) -> list[dict]:
        """Compatibility facade; the implementation belongs to `breaks`."""
        return self.breaks.get_recent_holding_protection_breaks(
            symbol,
            before_bar_date=before_bar_date,
            exclude_run_id=exclude_run_id,
            limit=limit,
        )

    def save_delever_ceiling_state(
        self,
        *,
        run_id: str,
        over_ceiling: bool,
    ) -> int:
        """Compatibility facade; the implementation belongs to `breaks`."""
        return self.breaks.save_delever_ceiling_state(
            run_id=run_id,
            over_ceiling=over_ceiling,
        )

    def get_last_delever_over_ceiling(
        self,
        *,
        exclude_run_id: str | None = None,
    ) -> bool | None:
        """Compatibility facade; the implementation belongs to `breaks`."""
        return self.breaks.get_last_delever_over_ceiling(
            exclude_run_id=exclude_run_id,
        )

    def save_target_level_break(
        self,
        *,
        run_id: str,
        symbol: str,
        raw_broken: bool | None,
        bar_date: str,
        raw_reach: bool | None = None,
        raw_wall: bool | None = None,
    ) -> int:
        """Compatibility facade; the implementation belongs to `breaks`."""
        return self.breaks.save_target_level_break(
            run_id=run_id,
            symbol=symbol,
            raw_broken=raw_broken,
            bar_date=bar_date,
            raw_reach=raw_reach,
            raw_wall=raw_wall,
        )

    def get_prior_target_level_break(
        self,
        symbols,
        *,
        today_bar_date: str,
        exclude_run_id: str | None = None,
        flag: str = "raw_broken",
    ) -> dict[str, bool]:
        """Compatibility facade; the implementation belongs to `breaks`."""
        return self.breaks.get_prior_target_level_break(
            symbols,
            today_bar_date=today_bar_date,
            exclude_run_id=exclude_run_id,
            flag=flag,
        )

    def record_target_revision(
        self,
        *,
        run_id: str,
        symbol: str,
        code: str,
        seat: str,
        evidence: str,
        detail: str = "",
        trigger: str = "",
        prior_price: float | None = None,
        new_price: float | None = None,
        basis: str = "",
        level_used: float | None = None,
        evidence_id: int | None = None,
        applied: bool = False,
    ) -> int:
        """Compatibility facade; implementation belongs to `target_revisions`."""
        return self.target_revisions.record_target_revision(
            run_id=run_id,
            symbol=symbol,
            code=code,
            seat=seat,
            evidence=evidence,
            detail=detail,
            trigger=trigger,
            prior_price=prior_price,
            new_price=new_price,
            basis=basis,
            level_used=level_used,
            evidence_id=evidence_id,
            applied=applied,
        )

    def get_target_revisions(
        self,
        symbols,
        *,
        limit: int = 200,
    ) -> dict[str, list[dict]]:
        """Compatibility facade; implementation belongs to `target_revisions`."""
        return self.target_revisions.get_target_revisions(symbols, limit=limit)

    def record_intraday_evaluation(
        self,
        *,
        symbol: str,
        run_id: str,
        status: str,
        detail: str = "",
        ref_price: float | None = None,
    ) -> None:
        """Upsert one (symbol, run_id) evaluation row.

        `ref_price` is the live price the mover scan ranked the name at; the
        next scan this session ranks the name by its move since that price
        (`get_session_reference_price`). A later write for the same run with
        no price (an exit-path row) keeps the price already stored.
        """

        def _do():
            self.conn.execute(
                "INSERT INTO intraday_evaluations(symbol, run_id, status, detail, ref_price) "
                "VALUES (?, ?, ?, ?, ?) ON CONFLICT(symbol, run_id) DO UPDATE SET "
                "status=excluded.status, detail=excluded.detail, "
                "ref_price=COALESCE(excluded.ref_price, intraday_evaluations.ref_price)",
                (symbol.upper(), run_id, status, detail, ref_price),
            )
            self.conn.commit()

        self._locked_write(_do, label="record_intraday_evaluation")

    def get_session_reference_price(self, symbol: str) -> float | None:
        """The price recorded at this name's latest intraday look THIS session, else None.

        "This session" is the current US/Eastern trading day. A row with no
        stored price (written before the column existed, or by an exit path)
        is not a reference; the caller then ranks the name from the prior
        close, exactly as for a name the desk has not looked at today.
        """
        session_start = sql_utc_stamp(datetime.combine(et_today(), time.min, tzinfo=ET))
        with self._lock:
            row = self.conn.execute(
                "SELECT ref_price FROM intraday_evaluations "
                "WHERE symbol=? AND timestamp >= ? AND ref_price IS NOT NULL "
                "ORDER BY timestamp DESC, id DESC LIMIT 1",
                (symbol.upper(), session_start),
            ).fetchone()
        return float(row[0]) if row is not None else None

    # --- Intraday snapshot health (2026-09-10) --------------------------
    #
    # `get_intraday_snapshots` cannot tell its caller "this symbol is
    # broken" from "this symbol simply didn't move" — both come back as an
    # all-None dict for that symbol (see that function's docstring). Left
    # alone, a persistently bad ticker is silently and permanently excluded
    # from every 30-minute scan with zero owner visibility — the same shape
    # of gap the BRK-B bug exposed, just smaller in blast radius now that
    # one bad symbol no longer kills the whole scan.
    #
    # ALERT_THRESHOLD = 3 consecutive misses (~90 minutes at this scan's
    # 30-minute cadence). Mirrors this codebase's own established pattern
    # for "rule out one noisy reading before acting on it" (the holding-
    # discipline structural-protection break above requires 2 CONSECUTIVE
    # daily closes, not one) — a single miss is routinely a transient API
    # gap that resolves on its own next tick; three in a row within the
    # same trading session is not noise.
    #
    # ALERT_COOLDOWN_HOURS = 24: once flagged, re-alert at most once a day
    # while the symbol stays broken, rather than every 30-minute tick for a
    # problem the owner has already been told about. This departs from
    # `maybe_alert_data_quality`'s deliberately-not-deduplicated design
    # (that alert fires once per SESSION, 5-6 times a day; this one runs on
    # a scan that ticks every 30 minutes, so undeduplicated would be a
    # dozen-plus repeats of the same fact before the trading day is half
    # over) — the goal here is "cannot go unnoticed for days," not "must
    # never repeat," and a daily reminder already satisfies that.
    INTRADAY_SNAPSHOT_ALERT_THRESHOLD = 3
    INTRADAY_SNAPSHOT_ALERT_COOLDOWN_HOURS = 24.0

    def record_intraday_symbol_snapshot_result(
        self,
        symbol: str,
        *,
        ok: bool,
    ) -> dict:
        """Update one symbol's consecutive-miss streak; report whether this
        call should trigger an owner alert (crossed the threshold, and no
        alert fired within the cooldown window).

        Returns {"consecutive_misses": int, "should_alert": bool}. Never
        raises past `_locked_write`'s own retry/re-raise contract — a bug
        here must not be able to break the scan it is monitoring.
        """
        symbol = symbol.upper()
        result: dict = {"consecutive_misses": 0, "should_alert": False}

        def _do():
            if ok:
                self.conn.execute(
                    "INSERT INTO intraday_symbol_health(symbol, consecutive_misses) "
                    "VALUES (?, 0) ON CONFLICT(symbol) DO UPDATE SET "
                    "consecutive_misses=0",
                    (symbol,),
                )
                self.conn.commit()
                return
            row = self.conn.execute(
                "SELECT consecutive_misses, last_alert_at FROM intraday_symbol_health WHERE symbol=?",
                (symbol,),
            ).fetchone()
            misses = (row["consecutive_misses"] if row else 0) + 1
            last_alert_at = row["last_alert_at"] if row else None
            should_alert = misses >= self.INTRADAY_SNAPSHOT_ALERT_THRESHOLD
            if should_alert and last_alert_at:
                from datetime import timezone

                try:
                    last_dt = datetime.fromisoformat(last_alert_at).replace(tzinfo=timezone.utc)
                    cutoff = utc_now() - timedelta(
                        hours=self.INTRADAY_SNAPSHOT_ALERT_COOLDOWN_HOURS,
                    )
                    should_alert = last_dt <= cutoff
                except ValueError:
                    # Unparseable timestamp: fail toward alerting rather than
                    # silently swallowing a real, ongoing problem.
                    should_alert = True
            self.conn.execute(
                "INSERT INTO intraday_symbol_health"
                "(symbol, consecutive_misses, last_alert_at) VALUES (?, ?, ?) "
                "ON CONFLICT(symbol) DO UPDATE SET consecutive_misses=excluded.consecutive_misses"
                + (", last_alert_at=?" if should_alert else ""),
                (symbol, misses, last_alert_at) + ((sql_utc_stamp(),) if should_alert else ()),
            )
            self.conn.commit()
            result["consecutive_misses"] = misses
            result["should_alert"] = should_alert

        self._locked_write(_do, label="record_intraday_symbol_snapshot_result")
        return result

    def session_prefixes_logged_on(self, trading_day: date | None = None) -> set[str]:
        """Set of session run_id PREFIXES that produced agent_logs on the given
        ET trading day (default today).

        run_id is formatted '{prefix}-{8hex}' where prefix is 'run' for the
        morning session and the session name otherwise (midday / close /
        evening / intra_check / earnings_preprocess / meta — see
        RunContext.start). A session that ran its LLM work leaves >=1 row; a
        session that silently never fired leaves none. Used by the evening
        dead-man's-switch check to detect a missing session — the one failure
        mode push-on-completion observability structurally cannot see.
        """
        start_utc, end_utc = self._et_day_utc_bounds(trading_day)
        with self._lock:
            rows = self.conn.execute(
                "SELECT DISTINCT run_id FROM agent_logs WHERE timestamp >= ? AND timestamp < ?",
                (start_utc, end_utc),
            ).fetchall()
        prefixes: set[str] = set()
        for r in rows:
            rid = r[0] or ""
            prefixes.add(rid.rsplit("-", 1)[0] if "-" in rid else rid)
        return prefixes

    def agent_names_logged_on(self, run_id_prefix: str, trading_day: date | None = None) -> set[str]:
        """Distinct agent_name values logged on the given ET trading day for
        run_ids starting with `run_id_prefix` (e.g. 'run-' for morning).

        RC5 (2026-07-16): the prefix check above can't tell a COMPLETED
        morning from one killed mid-flight — research rows land before the
        kill, so 'run' shows present while PM/RM never ran. This lets the
        dead-man's check ask "did the pipeline actually reach the decision
        stage?"
        """
        start_utc, end_utc = self._et_day_utc_bounds(trading_day)
        with self._lock:
            rows = self.conn.execute(
                "SELECT DISTINCT agent_name FROM agent_logs WHERE timestamp >= ? AND timestamp < ? AND run_id LIKE ?",
                (start_utc, end_utc, f"{run_id_prefix}%"),
            ).fetchall()
        return {r[0] for r in rows if r[0]}

    def sum_session_cost(self, run_id: str) -> tuple[float | None, int]:
        """Total cost + per-call count for a session's run_id.

        Returns (cost_usd_or_none, num_calls). cost is None when ANY
        agent in the session had an unknown-model cost — better to
        flag the gap than report a partial sum that looks correct.
        """
        with self._lock:
            rows = self.conn.execute(
                "SELECT cost_usd FROM agent_logs WHERE run_id = ?",
                (run_id,),
            ).fetchall()
        if not rows:
            return (None, 0)
        if any(r[0] is None for r in rows):
            # Partial coverage — return None so caller renders '$?.??'
            # rather than a misleading sum-of-known-only.
            return (None, len(rows))
        return (sum(float(r[0]) for r in rows), len(rows))

    def get_agent_logs(self, run_id: str) -> list[dict]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM agent_logs WHERE run_id = ? ORDER BY timestamp", (run_id,)
            ).fetchall()
        return [dict(row) for row in rows]

    def prune_agent_logs(self, keep_days: int = 730) -> int:
        """Delete agent_logs rows older than keep_days. Returns count deleted.

        Default is 2 years — long enough for quarter-over-quarter learning loops
        on what decisions worked while still bounding table size. agent_logs.full_response
        runs ~20-40KB per row with ~15-25 rows/day, so 730 days is ~200-300MB total.
        """
        if keep_days <= 0:
            raise ValueError(f"prune_agent_logs: keep_days must be > 0, got {keep_days}")
        with self._lock:
            cursor = self.conn.execute(
                "DELETE FROM agent_logs WHERE timestamp < ?",
                (sql_utc_stamp_ago(days=keep_days),),
            )
            self.conn.commit()
            return cursor.rowcount or 0

    def prune_specialist_evidence(self, keep_days: int = 730) -> int:
        """Delete specialist_evidence rows older than keep_days. Returns
        count deleted.

        Stage 4 (QAMC) added this table alongside agent_logs without a
        retention path — an ordinary trading day inserts a dozen-plus rows
        (per-symbol tech/earnings, run-scoped macro/news/PM-reasoning/RM-
        verdict, per-symbol PM-target/proposed-order/RM-modification) with
        no cap, on what's meant to be a long-running VPS-deployed bot.
        Default matches prune_agent_logs's 730-day (2 year) retention since
        this table is forensic-display detail for the same agent calls.
        """
        if keep_days <= 0:
            raise ValueError(f"prune_specialist_evidence: keep_days must be > 0, got {keep_days}")
        with self._lock:
            cursor = self.conn.execute(
                "DELETE FROM specialist_evidence WHERE timestamp < ?",
                (sql_utc_stamp_ago(days=keep_days),),
            )
            self.conn.commit()
            return cursor.rowcount or 0

    def prune_notifier_sends(self, keep_days: int = 730) -> int:
        """Delete notifier_sends rows older than keep_days. Returns count
        deleted.

        NOT wired into any scheduled maintenance pass as of 2026-09-18 —
        see the PR that introduced this table. `notifier_sends`'s closest
        siblings by shape and purpose, `session_reports` / `evening_reports`
        / `intra_check_reports`, have NO pruning at all today, so there is
        no single established convention for "how long does a rendered
        report live" to match. Rather than invent a fresh number, this
        reuses `prune_agent_logs`/`prune_specialist_evidence`'s already-
        ratified 730-day (2 year) figure, on the same reasoning they give:
        this table is forensic detail for the same session runs those
        tables cover, one-plus rows per run on a long-running bot with no
        cap otherwise. Flagged to the owner as an open call, not a decision
        made unilaterally: he may prefer to match the report tables (no
        pruning) instead, or set a rotation once one exists for them.
        """
        if keep_days <= 0:
            raise ValueError(f"prune_notifier_sends: keep_days must be > 0, got {keep_days}")
        with self._lock:
            cursor = self.conn.execute(
                "DELETE FROM notifier_sends WHERE timestamp < ?",
                (sql_utc_stamp_ago(days=keep_days),),
            )
            self.conn.commit()
            return cursor.rowcount or 0

    def insert_daily_pnl(
        self,
        date: str,
        total_value: float,
        daily_pnl: float,
        daily_return_pct: float,
        equity_close: float | None = None,
    ):
        with self._lock:
            # COALESCE preserves a previously-stored equity_close when this
            # write carries None (e.g. an LLM-failed evening re-run on a day the
            # first run already captured the 4pm close).
            self.conn.execute(
                """INSERT INTO daily_pnl
                   (date, total_value, daily_pnl, daily_return_pct, equity_close)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(date) DO UPDATE SET
                     total_value=excluded.total_value,
                     daily_pnl=excluded.daily_pnl,
                     daily_return_pct=excluded.daily_return_pct,
                     equity_close=COALESCE(excluded.equity_close, daily_pnl.equity_close)""",
                (date, total_value, daily_pnl, daily_return_pct, equity_close),
            )
            self.conn.commit()

    def insert_margin_interest_daily(
        self,
        date: str,
        debit_balance: float,
        rate_pct: float,
        daily_usd: float,
        days_charged: int,
        period_usd: float,
        source: str = "estimate",
    ) -> None:
        """One row per trading day the margin-interest tracker ran — the
        only historical record of the desk's overnight debit balance (see
        the table's own comment in `initialize()`). A same-day re-run
        replaces its own row (`ON CONFLICT(date) DO UPDATE`), same
        convention as `insert_daily_pnl`."""
        with self._lock:
            self.conn.execute(
                """INSERT INTO margin_interest_daily
                   (date, debit_balance, rate_pct, daily_usd, days_charged,
                    period_usd, source)
                   VALUES (?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(date) DO UPDATE SET
                     debit_balance=excluded.debit_balance,
                     rate_pct=excluded.rate_pct,
                     daily_usd=excluded.daily_usd,
                     days_charged=excluded.days_charged,
                     period_usd=excluded.period_usd,
                     source=excluded.source""",
                (date, debit_balance, rate_pct, daily_usd, days_charged, period_usd, source),
            )
            self.conn.commit()

    def insert_fred_fetch_coverage_run(self, row: dict) -> None:
        """Board item 187 record: one row per morning FRED fetch. See the
        table comment in `initialize()`; `row` comes from
        `src.data.fetch_coverage_record.build_row`."""
        cols = (
            "run_id",
            "series_configured",
            "series_succeeded",
            "series_failed",
            "series_not_attempted",
            "releases_configured",
            "releases_succeeded",
            "releases_from_cache",
            "releases_failed",
            "full_coverage",
        )
        with self._lock:
            self.conn.execute(
                f"INSERT INTO fred_fetch_coverage_runs ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)})",
                tuple(row[c] for c in cols),
            )
            self.conn.commit()

    def backfill_margin_interest_daily(
        self,
        rows: list,
        dry_run: bool = True,
    ) -> dict:
        """Write reconstructed HISTORICAL `margin_interest_daily` rows —
        owner ask 2026-09-24: no historical daily debit balance was ever
        persisted before #637, so the "all-time" cumulative view can only
        see days since that PR merged. `rows` is a list of
        `src.margin_interest.BackfilledDayEstimate` (or anything with the
        same attributes); see that module for how they were reconstructed
        (replayed from the broker's own activity ledger — there is no
        historical cash/positions endpoint to read this from directly).

        SAFE TO RE-RUN: a date already holding a row from the LIVE tracker
        (`source` `'estimate'` or `'broker_actual'`, written by
        `notifier._persist_margin_interest_daily` every morning) is left
        alone — a backfilled reconstruction must never overwrite a day the
        live tracker actually measured, no matter how many times this
        runs. A date already holding a PRIOR backfill row (`source`
        `'estimate_backfill'`) is safely re-upserted with this run's
        (possibly refined) figures, same idempotency contract as
        `insert_margin_interest_daily`'s own `ON CONFLICT`. A brand-new
        date is inserted.

        `dry_run=True` (the default) computes and returns counts without
        writing anything — same posture as `backfill_position_ids`/
        `scripts/backfill_position_ids.py`. Returns
        `{"inserted": n, "skipped_live_row": n, "total": n}`.
        """
        inserted = 0
        skipped_live_row = 0
        with self._lock:
            for row in rows:
                d = row.trading_day.isoformat() if hasattr(row.trading_day, "isoformat") else str(row.trading_day)
                existing = self.conn.execute(
                    "SELECT source FROM margin_interest_daily WHERE date = ?",
                    (d,),
                ).fetchone()
                if existing is not None and existing[0] != "estimate_backfill":
                    skipped_live_row += 1
                    continue
                inserted += 1
                if dry_run:
                    continue
                self.conn.execute(
                    """INSERT INTO margin_interest_daily
                       (date, debit_balance, rate_pct, daily_usd, days_charged,
                        period_usd, source)
                       VALUES (?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(date) DO UPDATE SET
                         debit_balance=excluded.debit_balance,
                         rate_pct=excluded.rate_pct,
                         daily_usd=excluded.daily_usd,
                         days_charged=excluded.days_charged,
                         period_usd=excluded.period_usd,
                         source=excluded.source
                       WHERE margin_interest_daily.source = 'estimate_backfill'""",
                    (d, row.debit_balance, row.rate_pct, row.daily_usd, row.days_charged, row.period_usd, row.source),
                )
            if not dry_run:
                self.conn.commit()
        return {
            "inserted": inserted,
            "skipped_live_row": skipped_live_row,
            "total": len(rows),
        }

    def backfill_equity_close(self, date: str, equity_close: float) -> bool:
        """Fill in a still-NULL equity_close on an existing daily_pnl row.

        Self-heal for the API-lag gap: portfolio_history doesn't have a
        trading day's official close yet at the 20:00 ET evening run (it
        lands hours later), so equity_close is stored NULL that night — but
        it's available by the FOLLOWING evening's lookback fetch. Only
        touches rows that are still NULL; never overwrites an already-
        captured close. Returns True if a row was updated.
        """
        with self._lock:
            cursor = self.conn.execute(
                "UPDATE daily_pnl SET equity_close = ? WHERE date = ? AND equity_close IS NULL",
                (equity_close, date),
            )
            self.conn.commit()
            return cursor.rowcount > 0

    def save_evening_report(self, *, date: str, run_id: str | None, payload: dict) -> None:
        """Store the evening run's own output for later re-rendering.

        `payload` is the result dict `run_evening` returns — the very
        object the evening Telegram formatter is handed. It is stored
        verbatim as JSON: no field is defaulted, renamed or filled in
        here, because a reader must be able to tell "the run did not
        produce this" from "the run produced zero". `default=str` is the
        last-resort escape for a value that is not JSON-native; it never
        substitutes for a missing value.

        The book is captured alongside it because `trader_feed._read_run`
        reads the `positions` table UNSCOPED (current holdings, not the
        run's), so a re-render weeks later would otherwise show today's
        book under last month's date.

        Keyed by trading day, matching daily_pnl/insights: a documented
        same-day evening re-run replaces the row and the run_id of the
        run that actually produced the stored numbers travels with it.
        """
        import json

        payload_json = json.dumps(payload, default=str)
        with self._lock:
            positions = [
                dict(row)
                for row in self.conn.execute(
                    "SELECT symbol, qty, avg_entry, current_price, market_value, "
                    "unrealized_pnl FROM positions WHERE qty != 0 "
                    "ORDER BY ABS(market_value) DESC"
                ).fetchall()
            ]
            positions_json = json.dumps(positions, default=str)
            self.conn.execute(
                """INSERT INTO evening_reports
                   (date, run_id, payload_json, positions_json)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(date) DO UPDATE SET
                     run_id=excluded.run_id,
                     payload_json=excluded.payload_json,
                     positions_json=excluded.positions_json,
                     timestamp=?""",
                (date, run_id, payload_json, positions_json, sql_utc_stamp()),
            )
            self.conn.commit()

    def _trades(self) -> TradeLedger:
        """Per-call construction so a collaborator swapped after __init__ is still reached."""
        return TradeLedger(
            conn=self.conn,
            lock=self._lock,
            locked_write=self._locked_write,
            executed_trade_predicate=self._executed_trade_predicate,
            sqlite_utc_timestamp=self._sqlite_utc_timestamp,
            et_day_utc_bounds=self._et_day_utc_bounds,
        )

    def insert_trade(
        self,
        symbol: str,
        action: str,
        qty: float,
        price: float,
        reasoning: str,
        run_id: str,
        stop_loss: float = 0,
        take_profit: float = 0,
        broker_order_id: str | None = None,
        fill_status: str | None = None,
        decision_id: str | None = None,
        expected_horizon_sessions: int | None = None,
        setup_type: str | None = None,
        conviction: str | None = None,
        requested_risk_pct: float | None = None,
        allocated_risk_pct: float | None = None,
        decision_model: str | None = None,
        thesis_invalid_if: str | None = None,
        structural_ceiling: bool | None = None,
        entry_atr: float | None = None,
        stop_basis: str | None = None,
        stop_level_basis: str | None = None,
        filled_at: str | None = None,
    ) -> int:
        """Thin shim: lifted into TradeLedger (db rebuild instalment 3); built per call."""
        return self._trades().insert_trade(
            symbol,
            action,
            qty,
            price,
            reasoning,
            run_id,
            stop_loss,
            take_profit,
            broker_order_id,
            fill_status,
            decision_id,
            expected_horizon_sessions,
            setup_type,
            conviction,
            requested_risk_pct,
            allocated_risk_pct,
            decision_model,
            thesis_invalid_if,
            structural_ceiling,
            entry_atr,
            stop_basis,
            stop_level_basis,
            filled_at=filled_at,
        )

    def insert_trade_refusal(self, **kwargs) -> int | None:
        """Thin shim: lifted into TradeLedger (db rebuild instalment 3); built per call."""
        return self._trades().insert_trade_refusal(**kwargs)

    def get_trade_refusals(
        self,
        *,
        refusal: str | None = None,
        limit: int = 500,
    ) -> list[dict]:
        """Thin shim: lifted into TradeLedger (db rebuild instalment 3); built per call."""
        return self._trades().get_trade_refusals(refusal=refusal, limit=limit)

    def update_open_stop_loss(
        self,
        symbol: str,
        new_stop_price: float,
        *,
        action: str | None = None,
    ) -> bool:
        """Thin shim: lifted into TradeLedger (db rebuild instalment 3); built per call."""
        return self._trades().update_open_stop_loss(symbol, new_stop_price, action=action)

    def _resolve_new_row_position_id(
        self,
        symbol: str,
        action: str,
        *,
        qty: float,
        fill_status: str | None,
        fill_qty: float | None,
        timestamp: str | None = None,
    ) -> str | None:
        """Thin shim: lifted into TradeLedger (db rebuild instalment 3); built per call."""
        return self._trades()._resolve_new_row_position_id(
            symbol, action, qty=qty, fill_status=fill_status, fill_qty=fill_qty, timestamp=timestamp
        )

    def confirm_trade_submitted(
        self,
        row_id: int,
        broker_order_id: str | None,
    ) -> int:
        """Thin shim: lifted into TradeLedger (db rebuild instalment 3); built per call."""
        return self._trades().confirm_trade_submitted(row_id, broker_order_id)

    def repoint_trade_broker_order_id(
        self,
        row_id: int,
        *,
        old_order_id: str,
        new_order_id: str,
    ) -> int:
        """Thin shim: lifted into TradeLedger (db rebuild instalment 3); built per call."""
        return self._trades().repoint_trade_broker_order_id(
            row_id, old_order_id=old_order_id, new_order_id=new_order_id
        )

    def mark_trade_submit_failed(self, row_id: int) -> int:
        """Thin shim: lifted into TradeLedger (db rebuild instalment 3); built per call."""
        return self._trades().mark_trade_submit_failed(row_id)

    def update_trade_fill(
        self,
        broker_order_id: str,
        fill_status: str,
        fill_qty: float | None = None,
        fill_price: float | None = None,
        filled_at: str | None = None,
    ) -> int:
        """Thin shim: lifted into TradeLedger (db rebuild instalment 3); built per call."""
        return self._trades().update_trade_fill(broker_order_id, fill_status, fill_qty, fill_price, filled_at)

    def regroup_position_ids(self, symbol: str) -> set[str] | None:
        """Re-derive one symbol's stored position ids from the fill walk (TradeLedger.regroup_position_ids)."""
        return self._trades().regroup_position_ids(symbol)

    def backfill_fill_times(self, updates: list[tuple[int, str, str]]) -> set[str]:
        """Broker fill times plus the regroup, one locked write (TradeLedger.backfill_fill_times)."""
        return self._trades().backfill_fill_times(updates)

    def _realized_pnl_through_trade(self, symbol: str, through_id: int) -> float | None:
        """Thin shim: lifted into TradeLedger (db rebuild instalment 3); built per call."""
        return self._trades()._realized_pnl_through_trade(symbol, through_id)

    def get_symbols_with_open_ledger_qty(self) -> dict[str, float]:
        """Thin shim: lifted into TradeLedger (db rebuild instalment 3); built per call."""
        return self._trades().get_symbols_with_open_ledger_qty()

    def get_known_broker_order_ids(self, symbol: str) -> set[str]:
        """Thin shim: lifted into TradeLedger (db rebuild instalment 3); built per call."""
        return self._trades().get_known_broker_order_ids(symbol)

    def insert_stop_out_trade(
        self,
        *,
        symbol: str,
        qty: float,
        price: float,
        broker_order_id: str,
        filled_at: str | None,
        run_id: str | None = None,
        action: str = "STOP_OUT",
        reasoning: str | None = None,
    ) -> tuple[int, bool]:
        """Thin shim: lifted into TradeLedger (db rebuild instalment 3); built per call."""
        return self._trades().insert_stop_out_trade(
            symbol=symbol,
            qty=qty,
            price=price,
            broker_order_id=broker_order_id,
            filled_at=filled_at,
            run_id=run_id,
            action=action,
            reasoning=reasoning,
        )

    def get_unreconciled_orders(self, run_id: str | None = None) -> list[dict]:
        """Thin shim: lifted into TradeLedger (db rebuild instalment 3); built per call."""
        return self._trades().get_unreconciled_orders(run_id)

    def get_orphaned_pending_submits(
        self,
        min_age_seconds: int = 120,
    ) -> list[dict]:
        """Thin shim: lifted into TradeLedger (db rebuild instalment 3); built per call."""
        return self._trades().get_orphaned_pending_submits(min_age_seconds)

    def has_pending_action_for_symbol(
        self,
        symbol: str,
        action: str,
        today_only: bool = True,
    ) -> bool:
        """Thin shim: lifted into TradeLedger (db rebuild instalment 3); built per call."""
        return self._trades().has_pending_action_for_symbol(symbol, action, today_only)

    def insert_pending_protection_restore(
        self,
        *,
        symbol: str,
        sell_order_id: str,
        position_qty_before_sell: float,
        specs_json: str,
        run_id: str | None = None,
        side: str | None = None,
    ) -> int:
        """Thin shim: lifted into TradeLedger (db rebuild instalment 3); built per call."""
        return self._trades().insert_pending_protection_restore(
            symbol=symbol,
            sell_order_id=sell_order_id,
            position_qty_before_sell=position_qty_before_sell,
            specs_json=specs_json,
            run_id=run_id,
            side=side,
        )

    def get_protection_restore_wal_audit(self) -> list[dict]:
        """Thin shim: lifted into TradeLedger (db rebuild instalment 3); built per call."""
        return self._trades().get_protection_restore_wal_audit()

    def get_pending_protection_restores(self) -> list[dict]:
        """Thin shim: lifted into TradeLedger (db rebuild instalment 3); built per call."""
        return self._trades().get_pending_protection_restores()

    def delete_pending_protection_restore(self, row_id: int) -> int:
        """Thin shim: lifted into TradeLedger (db rebuild instalment 3); built per call."""
        return self._trades().delete_pending_protection_restore(row_id)

    def update_pending_protection_restore(
        self,
        row_id: int,
        *,
        sell_order_id: str | None = None,
        position_qty_before_sell: float | None = None,
        specs_json: str | None = None,
        side: str | None = None,
    ) -> int:
        """Thin shim: lifted into TradeLedger (db rebuild instalment 3); built per call."""
        return self._trades().update_pending_protection_restore(
            row_id,
            sell_order_id=sell_order_id,
            position_qty_before_sell=position_qty_before_sell,
            specs_json=specs_json,
            side=side,
        )

    def update_pending_protection_restore_specs(
        self,
        row_id: int,
        specs_json: str,
    ) -> int:
        """Thin shim: lifted into TradeLedger (db rebuild instalment 3); built per call."""
        return self._trades().update_pending_protection_restore_specs(row_id, specs_json)

    def insert_pending_repeg(
        self,
        *,
        trade_row_id: int | None,
        symbol: str,
        old_order_id: str,
        new_order_id: str,
        run_id: str | None = None,
    ) -> int:
        """Thin shim: lifted into TradeLedger (db rebuild instalment 3); built per call."""
        return self._trades().insert_pending_repeg(
            trade_row_id=trade_row_id,
            symbol=symbol,
            old_order_id=old_order_id,
            new_order_id=new_order_id,
            run_id=run_id,
        )

    def get_pending_repegs(self) -> list[dict]:
        """Thin shim: lifted into TradeLedger (db rebuild instalment 3); built per call."""
        return self._trades().get_pending_repegs()

    def resolve_pending_repeg(self, row_id: int, new_order_id: str) -> int:
        """Thin shim: lifted into TradeLedger (db rebuild instalment 3); built per call."""
        return self._trades().resolve_pending_repeg(row_id, new_order_id)

    def delete_pending_repeg(self, row_id: int) -> int:
        """Thin shim: lifted into TradeLedger (db rebuild instalment 3); built per call."""
        return self._trades().delete_pending_repeg(row_id)

    def prune_pending_repegs(self, keep_days: int = 30) -> int:
        """Thin shim: lifted into TradeLedger (db rebuild instalment 3); built per call."""
        return self._trades().prune_pending_repegs(keep_days)

    def get_trades(
        self, symbol: str | None = None, limit: int = 100, today_only: bool = False, executed_only: bool = False
    ) -> list[dict]:
        """Thin shim: lifted into TradeLedger (db rebuild instalment 3); built per call."""
        return self._trades().get_trades(symbol, limit, today_only, executed_only)

    def _accumulate_excursions(self, position) -> None:
        """Thin shim: lifted into TradeLedger (db rebuild instalment 3); built per call."""
        return self._trades()._accumulate_excursions(position)

    def record_overnight_gap(
        self,
        symbol: str,
        prev_close: float,
        open_price: float,
        session_date: str,
    ) -> bool:
        """Thin shim: lifted into TradeLedger (db rebuild instalment 3); built per call."""
        return self._trades().record_overnight_gap(symbol, prev_close, open_price, session_date)

    def _accumulate_level_distances(self, position) -> None:
        """Thin shim: lifted into TradeLedger (db rebuild instalment 3); built per call."""
        return self._trades()._accumulate_level_distances(position)

    def sync_positions(self, positions) -> None:
        """Thin shim: lifted into TradeLedger (db rebuild instalment 3); built per call."""
        return self._trades().sync_positions(positions)

    def update_open_take_profit(
        self,
        symbol: str,
        new_target: float,
        *,
        action: str | None = None,
    ) -> bool:
        """Thin shim: lifted into TradeLedger (db rebuild instalment 3); built per call."""
        return self._trades().update_open_take_profit(symbol, new_target, action=action)

    def prune_trades(self, keep_days: int = 365 * 5) -> int:
        """Thin shim: lifted into TradeLedger (db rebuild instalment 3); built per call."""
        return self._trades().prune_trades(keep_days)

    def prune_pending_protection_restores(self, keep_days: int = 30) -> int:
        """Thin shim: lifted into TradeLedger (db rebuild instalment 3); built per call."""
        return self._trades().prune_pending_protection_restores(keep_days)

    def backfill_position_ids(self, *, dry_run: bool = False) -> dict:
        """Thin shim: lifted into TradeLedger (db rebuild instalment 3); built per call."""
        return self._trades().backfill_position_ids(dry_run=dry_run)

    def backfill_conviction_ledger(self, *, dry_run: bool = False) -> dict:
        """Thin shim: lifted into TradeLedger (db rebuild instalment 3); built per call."""
        return self._trades().backfill_conviction_ledger(dry_run=dry_run)

    def _analytics(self) -> TradeAnalytics:
        """Per-call construction so a collaborator swapped after __init__ is still reached."""
        return TradeAnalytics(conn=self.conn, lock=self._lock, executed_trade_predicate=self._executed_trade_predicate)

    def get_evening_report(self, date: str | None = None) -> dict | None:
        """Thin shim: lifted into TradeAnalytics (db rebuild instalment 2); built per call."""
        return self._analytics().get_evening_report(date)

    def _positions_snapshot_json(self) -> str:
        """The `positions` table, verbatim, as JSON — same query
        `save_evening_report` uses. Shared so morning/midday/close/
        intra_check capture the book in exactly the same shape.
        """
        import json

        with self._lock:
            positions = [
                dict(row)
                for row in self.conn.execute(
                    "SELECT symbol, qty, avg_entry, current_price, market_value, "
                    "unrealized_pnl FROM positions WHERE qty != 0 "
                    "ORDER BY ABS(market_value) DESC"
                ).fetchall()
            ]
        return json.dumps(positions, default=str)

    def save_session_report(self, *, mode: str, date: str, run_id: str | None, payload: dict) -> None:
        """Store one morning/midday/close result dict, verbatim, for replay.

        Same contract as `save_evening_report`: `payload` is stored exactly
        as the run produced it (no field defaulted or filled in), keyed by
        (date, mode) so a same-day re-run of the same session replaces its
        row rather than accumulating. The book is captured alongside it for
        the same reason evening's is — a replay weeks later must not print
        today's holdings under an old date.
        """
        import json

        payload_json = json.dumps(payload, default=str)
        positions_json = self._positions_snapshot_json()
        with self._lock:
            self.conn.execute(
                """INSERT INTO session_reports
                   (date, mode, run_id, payload_json, positions_json)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(date, mode) DO UPDATE SET
                     run_id=excluded.run_id,
                     payload_json=excluded.payload_json,
                     positions_json=excluded.positions_json,
                     timestamp=?""",
                (date, mode, run_id, payload_json, positions_json, sql_utc_stamp()),
            )
            self.conn.commit()

    def last_fresh_seat_reads(self, seats=None) -> dict:
        """Thin shim: lifted into TradeAnalytics (db rebuild instalment 2); built per call."""
        return self._analytics().last_fresh_seat_reads(seats)

    def get_session_report(self, mode: str, date: str | None = None) -> dict | None:
        """Thin shim: lifted into TradeAnalytics (db rebuild instalment 2); built per call."""
        return self._analytics().get_session_report(mode, date)

    def save_intra_check_report(self, *, run_id: str, date: str, payload: dict) -> None:
        """Store one intra_check tick's result dict, verbatim, for replay.

        Keyed by run_id, not date: intra_check fires roughly every 30
        minutes through the session, and a date-keyed "replace" row would
        keep only the last tick — see the table's own comment in the
        schema. Every tick gets its own durable row.
        """
        import json

        payload_json = json.dumps(payload, default=str)
        positions_json = self._positions_snapshot_json()
        with self._lock:
            self.conn.execute(
                """INSERT INTO intra_check_reports
                   (run_id, date, payload_json, positions_json)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(run_id) DO UPDATE SET
                     payload_json=excluded.payload_json,
                     positions_json=excluded.positions_json,
                     timestamp=?""",
                (run_id, date, payload_json, positions_json, sql_utc_stamp()),
            )
            self.conn.commit()

    def get_intra_check_report(self, run_id: str | None = None, date: str | None = None) -> dict | None:
        """Thin shim: lifted into TradeAnalytics (db rebuild instalment 2); built per call."""
        return self._analytics().get_intra_check_report(run_id, date)

    def get_earliest_daily_pnl(self) -> dict | None:
        """Thin shim: lifted into TradeAnalytics (db rebuild instalment 2); built per call."""
        return self._analytics().get_earliest_daily_pnl()

    def get_clean_record_baseline_rows(self) -> tuple[dict | None, dict | None]:
        return self._analytics().get_clean_record_baseline_rows()

    def get_daily_pnl(self, limit: int = 30, before_date: str | None = None) -> list[dict]:
        """Thin shim: lifted into TradeAnalytics (db rebuild instalment 2); built per call."""
        return self._analytics().get_daily_pnl(limit, before_date)

    def save_insights(
        self,
        date: str,
        tomorrow_outlook: str,
        lessons: str,
        suggested_actions: str,
        risk_rating: str,
        tomorrow_bias: str = "neutral",
        tomorrow_conviction: str = "medium",
        tomorrow_key_risks: list | str = (),
        sell_decisions_assessment: str = "",
    ):
        import json

        actions_json = json.dumps(suggested_actions) if isinstance(suggested_actions, list) else suggested_actions
        risks_json = (
            json.dumps(list(tomorrow_key_risks)) if not isinstance(tomorrow_key_risks, str) else tomorrow_key_risks
        )
        with self._lock:
            self.conn.execute(
                """INSERT OR REPLACE INTO insights
                   (date, tomorrow_outlook, lessons, suggested_actions, risk_rating,
                    tomorrow_bias, tomorrow_conviction, tomorrow_key_risks,
                    sell_decisions_assessment)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    date,
                    tomorrow_outlook,
                    lessons,
                    actions_json,
                    risk_rating,
                    tomorrow_bias,
                    tomorrow_conviction,
                    risks_json,
                    sell_decisions_assessment or "",
                ),
            )
            self.conn.commit()

    def get_symbol_last_buy(self, symbol: str, include_in_flight: bool = False, *, action: str = "BUY") -> dict | None:
        """Thin shim: lifted into TradeAnalytics (db rebuild instalment 2); built per call."""
        return self._analytics().get_symbol_last_buy(symbol, include_in_flight, action=action)

    def get_position_open_timestamp(self, buy_row: dict | None) -> str | None:
        """Thin shim: lifted into TradeAnalytics (db rebuild instalment 2); built per call."""
        return self._analytics().get_position_open_timestamp(buy_row)

    def get_position_open_row(self, buy_row: dict | None) -> dict | None:
        """Thin shim: lifted into TradeAnalytics (db rebuild instalment 2); built per call."""
        return self._analytics().get_position_open_row(buy_row)

    def get_position_chain_rows(self, buy_row: dict | None) -> list[dict] | None:
        """Thin shim: every executed row of `buy_row`'s position chain (TradeAnalytics)."""
        return self._analytics().get_position_chain_rows(buy_row)

    def get_recent_insights(self, limit: int = 7) -> list[dict]:
        """Thin shim: lifted into TradeAnalytics (db rebuild instalment 2); built per call."""
        return self._analytics().get_recent_insights(limit)

    def get_proposal_funnel_rows(self, since_ts: str) -> dict[str, list[dict]]:
        """Thin shim: lifted into TradeAnalytics (db rebuild instalment 2); built per call."""
        return self._analytics().get_proposal_funnel_rows(since_ts)

    def compute_trade_calibration(self, lookback_days: int = 45) -> dict:
        """Thin shim: lifted into TradeAnalytics (db rebuild instalment 2); built per call."""
        return self._analytics().compute_trade_calibration(lookback_days)

    def get_recent_agent_outputs(self, agent_name: str, limit: int = 5, before_date: str | None = None) -> list[dict]:
        """Thin shim: lifted into TradeAnalytics (db rebuild instalment 2); built per call."""
        return self._analytics().get_recent_agent_outputs(agent_name, limit, before_date)

    def get_latest_insights(self, before_date: str | None = None) -> dict | None:
        """Thin shim: lifted into TradeAnalytics (db rebuild instalment 2); built per call."""
        return self._analytics().get_latest_insights(before_date)

    def close(self):
        if self.conn:
            self.conn.close()
