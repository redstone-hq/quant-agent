"""Schema cluster lifted VERBATIM from src/storage/db.py (instalment 1 of the db rebuild).

Owns table creation and the irreversible, append-only migration ladder.
Standalone: the only collaborator is the open sqlite3 connection, passed
keyword-only, so it builds and runs with no Database/TradingPipeline behind
it (tests/boundary_harness.py). Database keeps same-named thin shims that
construct this per call.

Migration steps are NEVER reordered, renumbered or altered here: production
databases have already applied them.
"""

from __future__ import annotations

import logging
import sqlite3

from src.storage.account_epoch import ensure_account_epoch
from src.storage.schema.owner_intent_tables import apply as _owner_intents
from src.storage.schema.pending_stop_amend_tables import ensure_pending_stop_amend_table
from src.storage.schema.trend_tick_tables import ensure_trend_tick_table
from src.storage.schema.prune_indexes import ensure_prune_indexes
from src.storage.schema.sentinel_tables import ensure_sentinel_tables
from src.storage.schema.soft_exit_restore_occurrences_migration import (
    ensure_soft_exit_restore_occurrences,
)
from src.storage.schema.trade_refusal_tables import ensure_trade_refusal_table

logger = logging.getLogger(__name__)


class DatabaseSchema:
    """Creates tables and applies the migration ladder on one connection."""

    def __init__(self, *, conn: sqlite3.Connection):
        self.conn = conn

    def _create_tables(self):
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL,
                action TEXT NOT NULL,
                qty REAL NOT NULL,
                price REAL NOT NULL,
                reasoning TEXT,
                run_id TEXT,
                broker_order_id TEXT,
                fill_status TEXT,                      -- submitted | filled | canceled | rejected | expired | done_for_day | NULL(legacy)
                fill_qty REAL,                         -- actual qty filled (may differ from requested)
                fill_price REAL,                       -- actual avg fill price
                realized_pnl REAL,                     -- average-cost realized P&L for confirmed exits
                fill_reconciled_at TEXT,               -- when we confirmed the terminal status
                timestamp TEXT NOT NULL DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS positions (
                symbol TEXT PRIMARY KEY,
                qty REAL NOT NULL,
                avg_entry REAL NOT NULL,
                current_price REAL NOT NULL,
                market_value REAL NOT NULL,
                unrealized_pnl REAL NOT NULL,
                sector TEXT,
                updated_at TEXT NOT NULL DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS agent_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                agent_name TEXT NOT NULL,
                run_id TEXT NOT NULL,
                input_summary TEXT,
                input_message TEXT,
                output_summary TEXT,
                full_response TEXT,
                model TEXT,
                tokens_used INTEGER,
                -- Per-call cost tracking (added 2026-05-13). NULL when the
                -- agent's model isn't in src.cost_table.PRICING or when
                -- the SDK didn't return usage data. tokens_used is kept
                -- for backward-compat readers; the input/output split is
                -- the authoritative source for cost recomputation if
                -- pricing changes after-the-fact.
                input_tokens INTEGER,
                output_tokens INTEGER,
                cost_usd REAL,
                -- Actual provider HTTP requests represented by this logical
                -- row. Tech chunk aggregation can be >1; legacy rows are
                -- NULL and readers conservatively count them as one.
                provider_requests INTEGER,
                timestamp TEXT NOT NULL DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS daily_pnl (
                date TEXT PRIMARY KEY,
                total_value REAL NOT NULL,
                daily_pnl REAL NOT NULL,
                daily_return_pct REAL NOT NULL,
                equity_close REAL,
                timestamp TEXT NOT NULL DEFAULT (datetime('now'))
            );

            -- One row per trading day the margin-interest tracker actually
            -- ran (owner ask, 2026-09-24: a cumulative this-week/month/
            -- all-time view replacing the old per-day/per-year figures).
            -- `period_usd` is what that night's carry cost (already
            -- multiplied by `days_charged` for a weekend/holiday carry —
            -- see `src.margin_interest.MarginInterestEstimate.period_usd`);
            -- `debit_balance`/`rate_pct`/`days_charged` are kept alongside
            -- for audit, not for re-derivation. `source` is 'estimate'
            -- (our own daily-accrual formula; paper trading has never
            -- posted a real INT activity as of 2026-09-24) or
            -- 'broker_actual' if that ever changes. This table is the ONLY
            -- historical record of the desk's overnight debit balance —
            -- `daily_pnl` never stored cash/debit — so an accurate
            -- ESTIMATE-path "all-time" total can only ever cover days from
            -- here forward; see `src.margin_interest.bucket_estimate_rows`.
            CREATE TABLE IF NOT EXISTS margin_interest_daily (
                date TEXT PRIMARY KEY,
                debit_balance REAL NOT NULL,
                rate_pct REAL NOT NULL,
                daily_usd REAL NOT NULL,
                days_charged INTEGER NOT NULL DEFAULT 1,
                period_usd REAL NOT NULL,
                source TEXT NOT NULL DEFAULT 'estimate',
                timestamp TEXT NOT NULL DEFAULT (datetime('now'))
            );

            -- Board item 187: one row per morning-open FRED fetch, written by
            -- the pipeline from the coverage objects the fetch itself
            -- returned (nothing re-derived from log text). `series_failed`
            -- are series that WERE asked and failed (with reason);
            -- `series_not_attempted` never reached the wire. They are kept
            -- apart because "never attempted" is the item's complaint.
            -- `releases_*` is the event-calendar half; NULL there means that
            -- coverage object was not available, NOT zero. The calendar
            -- provider cannot tell "asked and failed" from "deadline hit
            -- before the ask", so its failures are recorded with their own
            -- reason text and are never claimed as either.
            CREATE TABLE IF NOT EXISTS fred_fetch_coverage_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT,
                recorded_at TEXT NOT NULL DEFAULT (datetime('now')),
                series_configured INTEGER,
                series_succeeded INTEGER,
                series_failed TEXT,
                series_not_attempted TEXT,
                releases_configured INTEGER,
                releases_succeeded INTEGER,
                releases_from_cache INTEGER,
                releases_failed TEXT,
                full_coverage INTEGER NOT NULL
            );

            -- The evening run's OWN OUTPUT — the exact result dict the
            -- evening Telegram formatter (src/trader_feed.py
            -- `_format_evening`) was handed, plus the book as the
            -- positions table held it at that moment.
            --
            -- Why this exists (2026-09-18): `run_evening` computed
            -- stop_coverage_gaps / stop_proximity / earnings_proximity /
            -- total_pnl / risk_capital_dollars from live broker state,
            -- handed them to the notifier, and discarded them. daily_pnl
            -- and insights persist the P&L and the narrative; nothing
            -- persisted the report's own inputs, so last night's message
            -- could not be re-read or reviewed without paying for a fresh
            -- pipeline run. Keyed by trading day like daily_pnl/insights
            -- (one authoritative report per day, a same-day re-run
            -- replaces it) with the producing run_id carried alongside.
            --
            -- Purely a forensic/re-render record: nothing in the trading
            -- path reads it, so losing this table costs no trading
            -- behaviour. A partially-populated payload stays partial —
            -- readers must render the gap as unavailable, never fill it.
            CREATE TABLE IF NOT EXISTS evening_reports (
                date TEXT PRIMARY KEY,
                run_id TEXT,
                payload_json TEXT NOT NULL,
                positions_json TEXT,
                timestamp TEXT NOT NULL DEFAULT (datetime('now'))
            );

            -- Same rationale, extended to morning/midday/close (2026-09-18
            -- gap sweep): `run_morning` and `run_position_review` compute
            -- `leverage` (the §11.2 gross-exposure ceiling snapshot) and
            -- `stop_coverage_gaps` (the broker-truth stop audit) fresh every
            -- call, hand them to the notifier, and drop them — neither has
            -- any other durable home (unlike PM/RM reasoning and orders,
            -- already persisted via `specialist_evidence`/`trades`). One row
            -- per trading day per mode ('morning', 'midday', 'close'): a
            -- same-day re-run replaces it, same convention as daily_pnl/
            -- insights/evening_reports.
            CREATE TABLE IF NOT EXISTS session_reports (
                date TEXT NOT NULL,
                mode TEXT NOT NULL,
                run_id TEXT,
                payload_json TEXT NOT NULL,
                positions_json TEXT,
                timestamp TEXT NOT NULL DEFAULT (datetime('now')),
                PRIMARY KEY (date, mode)
            );

            -- intra_check fires roughly every 30 minutes through the
            -- session (not once/day like the table above), so a same-day
            -- "replace" key would keep only the last tick and silently
            -- drop every earlier one — including the one tick that caught
            -- a coverage gap before a later, uneventful tick overwrote it.
            -- Keyed by run_id instead: every tick gets its own durable row.
            CREATE TABLE IF NOT EXISTS intra_check_reports (
                run_id TEXT PRIMARY KEY,
                date TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                positions_json TEXT,
                timestamp TEXT NOT NULL DEFAULT (datetime('now'))
            );
            CREATE INDEX IF NOT EXISTS idx_intra_check_reports_date
                ON intra_check_reports(date);

            -- Every outgoing Telegram message the desk attempted (2026-09-18,
            -- owner-raised gap): a successful send used to leave no record
            -- at all, only failures and rehearsal-suppressions. `text` is
            -- the rendered message body actually handed to Telegram (or,
            -- for a rehearsal, what WOULD have been); `status` distinguishes
            -- 'sent' / 'failed' / 'suppressed'; `detail` carries the
            -- (redacted) failure reason and is NULL otherwise. `kind` is a
            -- short caller tag (a session mode like 'morning', or
            -- 'owner_alert', 'document', ...) so "the last message of each
            -- kind" is one query. `src/notifier.py::TelegramNotifier._record_send`
            -- is the only writer, and creates this table itself too (a
            -- notifier call can happen before any `Database` is
            -- constructed, e.g. the live-scheduler startup ping) — this
            -- declaration exists so the schema is discoverable in the one
            -- place every other table lives, and so `Database` callers
            -- (pruning, tooling) don't need to know that quirk.
            CREATE TABLE IF NOT EXISTS notifier_sends (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                kind TEXT NOT NULL,
                status TEXT NOT NULL,
                run_id TEXT,
                text TEXT NOT NULL,
                detail TEXT,
                timestamp TEXT NOT NULL DEFAULT (datetime('now'))
            );
            CREATE INDEX IF NOT EXISTS idx_notifier_sends_kind_ts
                ON notifier_sends(kind, timestamp);

            CREATE TABLE IF NOT EXISTS insights (
                date TEXT PRIMARY KEY,
                tomorrow_outlook TEXT,
                lessons TEXT,
                suggested_actions TEXT,
                risk_rating TEXT,
                tomorrow_bias TEXT DEFAULT 'neutral',
                tomorrow_conviction TEXT DEFAULT 'medium',
                tomorrow_key_risks TEXT DEFAULT '[]',
                sell_decisions_assessment TEXT DEFAULT '',
                sell_grades_json TEXT DEFAULT '[]',
                buy_grades_json TEXT DEFAULT '[]',
                missed_opportunities_json TEXT DEFAULT '[]',
                -- Evening feedback-loop fields (defect (d) fix): produced by
                -- the LLM every night but previously dropped before storage.
                -- thesis_updates/selection_rules/discipline_notes are the
                -- "Structured lesson categories" — portfolio_manager reads
                -- them back the next session (Step 6 holding discipline +
                -- new-position selection). previous_outlook_assessment is
                -- evening's self-grade of its own prior-night forecast; kept
                -- for the audit trail, no downstream prompt consumer.
                thesis_updates_json TEXT DEFAULT '[]',
                selection_rules_json TEXT DEFAULT '[]',
                discipline_notes_json TEXT DEFAULT '[]',
                previous_outlook_assessment TEXT DEFAULT '',
                timestamp TEXT NOT NULL DEFAULT (datetime('now'))
            );

            -- Stage 4 (QAMC): additive, non-authoritative persistence of the
            -- already-VALIDATED structured evidence each specialist/decision
            -- agent produces (never raw LLM prose — see docs/architecture/
            -- MISSION_CONTROL_API.md). Lets Mission Control show per-candidate
            -- fidelity without the client ever re-parsing agent_logs.full_response.
            -- Purely a forensic display cache: losing this table has zero
            -- effect on trading (nothing here is read by the trading pipeline).
            CREATE TABLE IF NOT EXISTS specialist_evidence (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL,
                decision_id TEXT,               -- set only for PM/RM evidence rows
                agent_name TEXT NOT NULL,       -- macro_analyst | news_analyst | tech_analyst
                                                 -- | earnings_analyst | portfolio_manager | risk_manager
                kind TEXT NOT NULL,             -- analysis | reasoning | target | proposed_order
                                                 -- | verdict | modification
                scope TEXT NOT NULL,            -- 'run' (broader/non-symbol-specific) | 'symbol'
                symbol TEXT,                    -- NULL for scope='run'
                evidence_json TEXT NOT NULL,    -- model_dump_json() of the validated Pydantic object
                timestamp TEXT NOT NULL DEFAULT (datetime('now'))
            );

            -- Orphaned protective stops awaiting follow-up restore.
            -- Written by _finalize_protection_after_sell when the lingering
            -- SELL couldn't be cancelled cleanly (or didn't reach terminal
            -- after cancel). Drained at the start of every session: each
            -- row's sell_order_id is re-queried; if now terminal, we
            -- finalize protection from the persisted specs and delete the
            -- row. Without persistence, the bail branches' "next session
            -- reconcile rebuilds coverage" promise was a lie — _reconcile_fills
            -- only updates fill columns, not stop coverage.
            CREATE TABLE IF NOT EXISTS pending_protection_restores (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL,
                sell_order_id TEXT NOT NULL,
                position_qty_before_sell REAL NOT NULL,
                specs_json TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                run_id TEXT
            );

            -- Board item 193. `pending_protection_restores.id` is a SHARED
            -- AUTOINCREMENT: the scale-in cancel path and the protected-sell
            -- exit path both draw ids from it, and every row is DELETED once
            -- discharged. The id ceiling is therefore not a count of anything,
            -- and an id spent by the other writer used to be indistinguishable
            -- from a scale-in cancel that filed no event. This table is the
            -- attribution: one never-deleted row per id handed out, written at
            -- the single insert choke point, so a future gap between the id
            -- ceiling and the scale-in cancel events is read off the record
            -- instead of argued about. Bookkeeping only - nothing reads it to
            -- decide anything, and failing to write it never blocks the
            -- protective WAL row it describes.
            CREATE TABLE IF NOT EXISTS protection_restore_wal_audit (
                row_id INTEGER PRIMARY KEY,
                symbol TEXT NOT NULL,
                sell_order_id TEXT NOT NULL,
                position_qty_before_sell REAL,
                side TEXT,
                run_id TEXT,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            );


            -- Bounded entry re-peg write-ahead queue. An Alpaca order
            -- replacement MINTS A NEW ORDER ID: the moment the PATCH is
            -- accepted, `trades.broker_order_id` is stale and points at a
            -- dead order. A crash in that window would leave the broker
            -- holding a working order nothing in this system knows about.
            -- So the intent is written HERE first, with new_order_id set to
            -- the _WAL_PENDING_ sentinel, and drained at session start:
            -- the drain re-reads the OLD id, follows Alpaca's `replaced_by`
            -- link to whatever the replacement became, and repoints the
            -- trades row. Same shape as pending_protection_restores above.
            CREATE TABLE IF NOT EXISTS pending_repegs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                trade_row_id INTEGER,
                symbol TEXT NOT NULL,
                old_order_id TEXT NOT NULL,
                new_order_id TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                run_id TEXT
            );

            -- Explicit intraday evaluation ledger: one row per name the
            -- desk looked at, whatever the outcome. `ref_price` is the live
            -- price the mover scan ranked the name at when the PM saw it;
            -- the next scan this session ranks the name by its move since
            -- that price (NULL on exit-path rows and pre-2026-10-13 rows).
            CREATE TABLE IF NOT EXISTS intraday_evaluations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL,
                run_id TEXT NOT NULL,
                status TEXT NOT NULL,
                detail TEXT,
                ref_price REAL,
                timestamp TEXT NOT NULL DEFAULT (datetime('now')),
                UNIQUE(symbol, run_id)
            );
            CREATE INDEX IF NOT EXISTS idx_intraday_evaluations_symbol_time
                ON intraday_evaluations(symbol, timestamp);

            -- Per-symbol intraday-snapshot health. A symbol Alpaca cannot
            -- return snapshot data for (bad/delisted ticker, a one-off API
            -- gap) used to fail identically to "this stock just didn't move
            -- today" — see `get_intraday_snapshots`: both collapse to an
            -- all-None dict for that symbol, so the scan silently, forever,
            -- excludes it with zero owner visibility (2026-09-10 finding,
            -- the residual gap the BRK-B fix did not close). This table
            -- lets the scan tell "quiet" from "broken" by counting
            -- CONSECUTIVE misses, and remembers the last time it alerted so
            -- a known, still-unresolved problem doesn't re-page every tick.
            CREATE TABLE IF NOT EXISTS intraday_symbol_health (
                symbol TEXT PRIMARY KEY,
                consecutive_misses INTEGER NOT NULL DEFAULT 0,
                last_alert_at TEXT
            );
        """)
        # Its own statement, outside the script above, so the clock-guard allowlist entry
        # for that script is unchanged; this table carries no SQL clock default.
        self.conn.executescript("""
            -- One row per order a delivered message listed as "placed,
            -- waiting to fill" (owner ruling 5b, 13 Oct): Telegram's
            -- `message_id`, the exact delivered line, and the whole
            -- delivered text, so the fill can EDIT that same message
            -- instead of sending a new one. `message_text` is kept current
            -- across edits so a second fill in the same message builds on
            -- the first. `edited_at` NULL until the "filled" edit lands.
            -- Like `notifier_sends`, `src/notifier/send_log.py` also
            -- creates it (a send can precede any `Database`).
            CREATE TABLE IF NOT EXISTS placed_order_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                trade_id INTEGER NOT NULL,
                broker_order_id TEXT NOT NULL,
                message_id INTEGER NOT NULL,
                line_text TEXT NOT NULL,
                message_text TEXT NOT NULL,
                edited_at TEXT,
                timestamp TEXT NOT NULL,
                UNIQUE (broker_order_id, message_id)
            );
            CREATE INDEX IF NOT EXISTS idx_placed_order_messages_order
                ON placed_order_messages(broker_order_id);
        """)
        self.conn.commit()
        self._migrate()
        # After the migrations: the views select `trades.*` / `daily_pnl.*`, and the
        # no-cut first epoch row is written only when the table is first created.
        ensure_account_epoch(self.conn)
        # The cost circuit also initializes itself independently because it
        # must work before the main Database object exists. Creating the same
        # additive schema here makes read-only API health available after any
        # normal DB initialization and keeps migrations explicit.
        from src.cost_circuit import ensure_cost_circuit_schema

        try:
            ensure_cost_circuit_schema(self.conn)
            self.conn.commit()
        except Exception:
            # Cost-accounting corruption must suspend paid analysis, but must
            # not prevent construction of the main DB used by broker stop/fill
            # reconciliation and deterministic loss protection.  The breaker
            # independently retries initialization and persists its emergency
            # fail-closed marker.
            self.conn.rollback()
            logger.critical(
                "Cost-circuit schema initialization failed; paid analysis will "
                "fail closed while non-LLM safety remains available",
                exc_info=True,
            )

    def _migrate(self):
        """Add columns that may be missing in older databases.

        Each ALTER is independent and wrapped in try/except so one partial
        migration (e.g., stop_loss added but take_profit ALTER crashed on the
        prior run) can still be recovered by the next startup. The old pattern
        of bundling both ALTERs under a single 'if stop_loss not in columns'
        guard would permanently skip take_profit if it wasn't added together.
        """
        import logging as _logging

        _log = _logging.getLogger(__name__)

        def _ensure_column(table: str, column: str, ddl: str) -> None:
            try:
                cursor = self.conn.execute(f"PRAGMA table_info({table})")
                existing = {row[1] for row in cursor.fetchall()}
                if column in existing:
                    return
                self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")
                self.conn.commit()
                _log.info("Schema migration: added %s.%s", table, column)
            except Exception as e:
                # Don't bring down initialization on a migration hiccup — the
                # table is still usable with the old schema, just missing this
                # one column. Caller will see reduced functionality, not a crash.
                _log.error("Schema migration failed for %s.%s: %s", table, column, e)

        _ensure_column("agent_logs", "input_message", "input_message TEXT DEFAULT ''")
        # Today's official regular-session (4pm) close equity, captured from
        # Alpaca portfolio_history — enables true close-to-close evening P&L
        # instead of the close-to-8pm-AH broker diff. NULL for legacy rows.
        _ensure_column("daily_pnl", "equity_close", "equity_close REAL")
        _ensure_column("trades", "stop_loss", "stop_loss REAL DEFAULT 0")
        # Frozen copy of the entry stop. `stop_loss` is written back when
        # the live protective level moves (trail / replace / repair / rearm
        # / ex-div); R-multiple and the Type A breakeven ratchet still need
        # the original bet. NULL on legacy rows that have never been
        # written back — those still have entry == stop_loss.
        _ensure_column("trades", "initial_stop_loss", "initial_stop_loss REAL")
        # Pin the entry stop on legacy opening rows that still have one.
        # Safe because this desk never wrote stop_loss back before this
        # column existed — the value still on the row IS the entry. Rows
        # that opened with no stop stay NULL so a later write-back cannot
        # mint an R-multiple denominator.
        try:
            self.conn.execute(
                "UPDATE trades SET initial_stop_loss = stop_loss "
                "WHERE initial_stop_loss IS NULL "
                "AND COALESCE(stop_loss, 0) > 0 "
                "AND UPPER(action) IN ('BUY', 'SHORT')"
            )
            self.conn.commit()
        except Exception as e:  # noqa: BLE001
            _log.error("Schema migration backfill for trades.initial_stop_loss failed: %s", e)
        _ensure_column("trades", "take_profit", "take_profit REAL DEFAULT 0")
        # Frozen copy of the ENTRY take-profit, mirroring `initial_stop_loss`
        # above. `take_profit` became mutable when a seat's flag could trigger
        # a RE-DERIVATION of the target (`src.risk.target_revision`), and the
        # original must survive that for two independent reasons:
        #
        #   1. `thesis_progress_pct` and `pace` are measured against this
        #      PINNED number, never the live one. The target is the
        #      DENOMINATOR of progress, so raising it mechanically lowers
        #      progress AND pace — both of which are in
        #      `exit_guard._HIGHER_IS_BETTER`. A revision that moved them
        #      would land in `MetricDeltas.worsened`, clear `net_improved`,
        #      and switch OFF `veto_contradicted_exit` — i.e. good news would
        #      unlock a "this position is stalling" SELL.
        #   2. Without the original there is no way to ever grade whether
        #      revising targets helps or hurts.
        _ensure_column("trades", "initial_take_profit", "initial_take_profit REAL")
        # Pin the entry target on legacy opening rows. Safe on exactly the
        # same grounds as the `initial_stop_loss` backfill above: before this
        # column existed `take_profit` was written ONLY by `insert_trade` and
        # never written back, so the value still on the row IS the entry
        # derivation. Rows that opened with no target stay NULL so a later
        # revision cannot mint a progress denominator out of nothing.
        try:
            self.conn.execute(
                "UPDATE trades SET initial_take_profit = take_profit "
                "WHERE initial_take_profit IS NULL "
                "AND COALESCE(take_profit, 0) > 0 "
                "AND UPPER(action) IN ('BUY', 'SHORT')"
            )
            self.conn.commit()
        except Exception as e:  # noqa: BLE001
            _log.error(
                "Schema migration backfill for trades.initial_take_profit failed: %s",
                e,
            )
        # Phase 3.1 — the thesis horizon and setup type PINNED AT ENTRY.
        # `pace` used to be measured against `avg_hold_days` from the system's
        # OWN rolling 30-day realized-trade calibration (~2.0 days), so selling
        # quickly shrank the average, which made every remaining position look
        # stalled, which drove more selling. A self-tightening noose. The
        # horizon must come from the analyst's stated thesis at entry and never
        # be recomputed. NULL on legacy rows — those positions get no pace
        # figure at all rather than a fabricated one.
        _ensure_column(
            "trades",
            "expected_horizon_sessions",
            "expected_horizon_sessions INTEGER",
        )
        _ensure_column("trades", "setup_type", "setup_type TEXT")
        # Item 82 (2026-09-25): the MEASURED half of construction's own
        # breakout verdict, pinned at ENTRY alongside `setup_type` above.
        # `setup_type` is only the analyst's raw label; construction's real
        # verdict is `reward_risk_floor_applies(setup_type,
        # structural_ceiling=...)` — True (breakout, no ratio, no progress/
        # pace) whenever EITHER the label says "breakout" OR this is False.
        # `False` = the desk's own level computation found nothing overhead in
        # the trade's direction (`derivation.level_used is None`); `True` = a
        # ceiling was found. Stored as 0/1. NULL on every legacy row and on any
        # non-entry / legacy caller that never pins it — readers fall back to
        # the label alone (the conservative side), exactly as before this
        # column existed. See `TradeDecision.structural_ceiling` in models.py
        # and `src.risk.constants.is_trend_trade`.
        _ensure_column("trades", "structural_ceiling", "structural_ceiling INTEGER")
        ensure_trade_refusal_table(conn=self.conn)
        # --- Item 75 evidence: the alignment-exit reading for EVERY open
        # position EVERY session, INCLUDING the sessions it does not fire.
        # RECORDING ONLY (2026-10-01).
        #
        # Board item 75 (automatic PARTIAL profit-taking — trimming part of
        # a position rather than selling all of it) is blocked because
        # nobody can say what fraction to trim without inventing it. Two
        # derivations were attempted on 2026-10-01 and both failed; the
        # reasons are in `docs/BOARD_NOTES.md` (item 75) and the item bars a
        # third. The question that has to be answered first is empirical:
        # do positions pass through a DURABLE intermediate band of
        # weakening before their trend ends, or do they fall straight
        # through? Today only a FIRING alignment exit leaves any trace, so
        # the population that would answer it — positions that weakened and
        # recovered — is invisible. These rows are that population.
        #
        # One row per open position per run. The reading is the one
        # `src.risk.alignment_exit.check_alignment_exit` ALREADY computes;
        # nothing here recomputes it a second way, and nothing here changes
        # when the exit fires or what it does.
        #
        # WHAT THIS DATA MAY BE USED FOR: reading, once, whether a durable
        # intermediate band of weakening exists at all.
        #
        # WHAT IT MAY NOT BE USED FOR: nothing may read it back into a
        # sizing, stop or exit decision, and it may NEVER be swept for the
        # trim fraction that would have performed best on these rows. That
        # is fitting a number to this desk's own trading record, which
        # doctrine bars outright ("no fitting, only reading") and which
        # item 75's own last criterion bars by name. The bar holds however
        # much data accumulates.
        #
        # NO CLASSIFICATION AND NO BAND EDGE IS STORED. "Weakening",
        # "durable" and "recovered" each need a cutoff and a horizon nobody
        # can source today, so only the RAW distance is kept, in the name's
        # own ATR, alongside which of the exit's conditions were satisfied.
        # A later reader states its own cutoff and applies it to these
        # numbers, which were never rounded to one. Unknown is NULL — never
        # a computed or assumed substitute, which is why an UNPARSEABLE
        # read still writes a row (with `breach_atrs` NULL) rather than
        # being dropped: "could not read the chart" and "the chart was
        # intact" are different facts.
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS alignment_exit_readings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                run_id TEXT,
                session_date TEXT,
                symbol TEXT NOT NULL,
                is_short INTEGER,
                status TEXT,
                code TEXT,
                breach_atrs REAL,
                band_atrs REAL,
                sessions_since_mark_lost INTEGER,
                last_mark_price REAL,
                last_mark_source TEXT,
                marks_count INTEGER,
                thesis_ma_period INTEGER,
                thesis_ma_kind TEXT,
                not_evaluated_reason TEXT,
                UNIQUE (run_id, symbol)
            )
            """
        )
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_alignment_exit_readings_symbol_ts "
            "ON alignment_exit_readings (symbol, session_date)"
        )
        _ensure_column(
            "alignment_exit_readings",
            "not_evaluated_reason",
            "not_evaluated_reason TEXT",
        )
        # Item 78 evidence, RECORDING ONLY: nothing may read these rows
        # back into a trading decision and they may never be swept for a
        # threshold. Unknown stays NULL throughout. Why the table exists,
        # and why rows are deduplicated with a count, is documented in
        # src/storage/schema/soft_exit_restore_occurrences_migration.py.
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS soft_exit_heal_restores (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                run_id TEXT,
                session_date TEXT,
                symbol TEXT,                 -- NULL when the payload had none
                blank_found INTEGER,         -- 1/0: falsifier blank on entry
                healed INTEGER,              -- 1/0: heal filled it
                source TEXT,                 -- NULL unless healed
                dropped_before INTEGER,      -- identities the cap refused
                occurrences INTEGER          -- times this identity was seen
            )
            """
        )
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_soft_exit_heal_restores_date "
            "ON soft_exit_heal_restores (session_date, symbol)"
        )
        ensure_soft_exit_restore_occurrences(self.conn)  # see that module
        # --- Item 224 evidence: the REALISED sector mix of the orders the
        # constructor actually built, one row per run.
        # RECORDING ONLY (2026-10-01).
        #
        # Item 221 removed the pre-decision sector PROJECTION, because the
        # size of each candidate depends on a portfolio-manager decision
        # that does not exist when the preview is built, so no honest
        # projection was possible. What CAN be stated honestly is what the
        # mix turned out to be once the constructor had finished sizing.
        # Nobody recorded that, so concentration could not be judged after
        # the fact at all. These rows are that record.
        #
        # UNIT AND DENOMINATOR, stated explicitly because this desk has
        # already been bitten by unstated units: every weight is PERCENT OF
        # TOTAL ACCOUNT EQUITY, raw position notional, BEFORE the gross
        # multiplier — the same unit and the same denominator item 222
        # settled on for the single-name ceiling (see
        # `SINGLE_NAME_BINDING_SENTENCE` in `src/portfolio_constructor.py`).
        # No second convention was invented. `denominator` carries that
        # statement on every row, and `total_value` carries the equity the
        # weights are a share of at the moment the constructor sized, so a
        # later reader never has to guess which account state produced them.
        #
        # WHAT THIS DATA MAY BE USED FOR: reading, after the fact, what the
        # realised sector mix of a session's orders actually was.
        #
        # WHAT IT MAY NOT BE USED FOR: nothing may read it back into a
        # sizing, ordering or refusal decision, and it may NEVER be swept
        # for the sector cap that would have performed best on these rows.
        # That is fitting a number to this desk's own trading record, which
        # doctrine bars outright ("no fitting, only reading"). The bar holds
        # however much data accumulates.
        #
        # ENTRY ORDERS ONLY in the weights. A BUY or a SHORT puts exposure
        # ON, and its `allocation_pct` is that exposure in the unit above; a
        # SELL or a COVER takes exposure OFF and its `allocation_pct` is a
        # reduction, so adding the two would produce a number that is not a
        # weight of anything. The reducing orders are COUNTED
        # (`reducing_orders_built`) so the row never implies the session
        # built only entries.
        #
        # UNKNOWN SECTOR STAYS NULL inside the JSON (never "other"), but
        # New recordings are never NULL: [] means no orders. Legacy NULLs
        # stay unknown; triggers reject future NULL writes and backfills.
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS realised_sector_weights (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                run_id TEXT,
                session_date TEXT,
                weights_json TEXT NOT NULL,
                denominator TEXT NOT NULL,
                total_value REAL,
                entry_orders_built INTEGER,
                reducing_orders_built INTEGER,
                unknown_sector_orders INTEGER,
                UNIQUE (run_id)
            )
            """
        )
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_realised_sector_weights_date ON realised_sector_weights (session_date)"
        )
        from src.storage.schema.realised_sector_weights_migration import ensure_not_null

        ensure_not_null(self.conn)
        _ensure_column("insights", "tomorrow_bias", "tomorrow_bias TEXT DEFAULT 'neutral'")
        _ensure_column("insights", "tomorrow_conviction", "tomorrow_conviction TEXT DEFAULT 'medium'")
        _ensure_column("insights", "tomorrow_key_risks", "tomorrow_key_risks TEXT DEFAULT '[]'")
        _ensure_column("insights", "sell_decisions_assessment", "sell_decisions_assessment TEXT DEFAULT ''")
        # Phase 3: fill reconciliation — tells memory readers which 'trades'
        # rows actually executed vs which were just submitted. Legacy rows
        # default to NULL and are treated as 'filled' by the calibration
        # query (backward compat — those predate the reconciliation path).
        _ensure_column("trades", "broker_order_id", "broker_order_id TEXT")
        _ensure_column("trades", "fill_status", "fill_status TEXT")
        _ensure_column("trades", "fill_qty", "fill_qty REAL")
        _ensure_column("trades", "fill_price", "fill_price REAL")
        _ensure_column("trades", "fill_reconciled_at", "fill_reconciled_at TEXT")
        # When the shares actually moved, as the BROKER stamped it (its order's
        # `filled_at`, naive UTC) - never when the desk noticed. The ONLY clock
        # the open-R lot walk and the position grouper order fills by
        # (src/trade_actions.py `fill_time`). NULL on rows written before it
        # existed until scripts/backfill_fill_times.py fills them from Alpaca.
        _ensure_column("trades", "filled_at", "filled_at TEXT")
        # Evening v2 structured per-trade grades. Stored as JSON arrays so
        # position_reviewer can aggregate counts (correct/premature/wrong)
        # without parsing prose. NULL for pre-v2 rows → treated as [].
        _ensure_column("insights", "sell_grades_json", "sell_grades_json TEXT")
        _ensure_column("insights", "buy_grades_json", "buy_grades_json TEXT")
        # Phase-1 evening-upgrade: structured missed_opportunities persist
        # here so next-day PM's L3d memory + quarterly meta-reflection's
        # theme_coverage_report can aggregate without re-running the LLM.
        # NULL for pre-upgrade rows → downstream readers default to [].
        _ensure_column(
            "insights",
            "missed_opportunities_json",
            "missed_opportunities_json TEXT DEFAULT '[]'",
        )
        # Per-call LLM cost tracking (2026-05-13). input_tokens /
        # output_tokens stored separately so cost can be recomputed if
        # pricing changes; cost_usd is the snapshot at insert time.
        # cost_usd is REAL (not cent integers) — SQLite handles small
        # floats fine and per-call costs span 4 orders of magnitude
        # ($0.0001 / macro to $1.00+ / tech full chunk).
        _ensure_column("agent_logs", "input_tokens", "input_tokens INTEGER")
        _ensure_column("agent_logs", "output_tokens", "output_tokens INTEGER")
        _ensure_column("agent_logs", "cost_usd", "cost_usd REAL")
        _ensure_column("agent_logs", "provider_requests", "provider_requests INTEGER")
        # Stage 1 (QAMC provider/model/correlation plumbing). All nullable —
        # legacy rows read back as NULL/None, never a fabricated value (per
        # DECISION #12 / ACCEPTANCE_CRITERIA "unknown stays unknown"). Sourced
        # from the matching new AgentResult fields (src/agents/base.py); see
        # docs/architecture/MODEL_PROVIDER_ARCHITECTURE.md "Required contract".
        _ensure_column("agent_logs", "requested_provider", "requested_provider TEXT")
        _ensure_column("agent_logs", "requested_model", "requested_model TEXT")
        # `model` (existing column, corrected Stage 0.5) already holds the
        # ACTUAL model; actual_provider is its provider, derived the same way.
        _ensure_column("agent_logs", "actual_provider", "actual_provider TEXT")
        # sha256(system_prompt)[:12] at call time — a cheap "did the prompt
        # text change" signal, not a semantic version.
        _ensure_column("agent_logs", "prompt_version", "prompt_version TEXT")
        _ensure_column("agent_logs", "latency_s", "latency_s REAL")
        # 'success' | 'fallback' | 'failed'. NULL for pre-Stage-1 rows.
        _ensure_column("agent_logs", "status", "status TEXT")
        # finish_reason/truncated were already computed on AgentResult
        # (Stage 0 audit F-2: computed but never persisted) — closing that
        # gap here costs nothing extra since the values already exist.
        _ensure_column("agent_logs", "finish_reason", "finish_reason TEXT")
        _ensure_column("agent_logs", "truncated", "truncated INTEGER")
        # Decision-level correlation (links a portfolio_manager/risk_manager
        # agent_logs row to the trades row(s) its decision produced). NULL
        # for every other agent and for all pre-Stage-1 rows.
        _ensure_column("agent_logs", "decision_id", "decision_id TEXT")
        # Did the SEAT accept this answer, and if not why — the fact `status`
        # never carried (`status` only ever meant "the call returned"). NULL
        # on every legacy row and on any site not yet instrumented; readers
        # must treat NULL as unknown, never as accepted. Board item 188:
        # for the three DECISION seats `acceptance_reason` now carries the
        # gate's OWN machine-readable word (pm_/risk_/review_ prefixed) when
        # the gate named one, so the refusal says which way the answer was
        # unusable. RECORDING ONLY: nothing may read either column back into
        # a sizing, stop, exit or routing decision, and neither may be swept
        # for an optimal threshold — they exist to make a model's
        # usable-answer rate computable from the desk's own rows.
        _ensure_column("agent_logs", "acceptance", "acceptance TEXT")
        _ensure_column("agent_logs", "acceptance_reason", "acceptance_reason TEXT")
        # Whether the provider's answer carried usage information:
        # complete / no_cost / no_usage. NULL on every legacy row and where no
        # request was made; NULL is unknown, never "complete" and never free.
        _ensure_column("agent_logs", "telemetry", "telemetry TEXT")
        _ensure_column("trades", "decision_id", "decision_id TEXT")
        _ensure_column("trades", "realized_pnl", "realized_pnl REAL")
        # Stage 3 (shorts): which side the protective stop being restored
        # belongs to. NULL for every row written before this migration —
        # `_derive_close_side_for_drain` / `_drain_pending_protection_restores`
        # (src/pipeline.py) treat NULL exactly as the old code always did
        # (the documented long-assuming "sell" fallback), so an existing
        # in-flight row is unaffected; only NEWLY WRITTEN rows carry a real
        # value. See `insert_pending_protection_restore`.
        _ensure_column("pending_protection_restores", "side", "side TEXT")
        # Phase 6 (QAMC remediation spec §6.2a/e): links every trade against
        # a symbol to the position it belongs to (a BUY from flat mints a new
        # id; SELL/REDUCE/TRAIL_STOP/etc inherit it until flat — see
        # `_assign_position_ids`), and classifies WHY an exit happened into a
        # countable category (see `_categorize_exit_reason`). Both NULL on
        # legacy rows until `scripts/backfill_position_ids.py` (or
        # `Database.backfill_position_ids`) runs — never guessed at
        # migration time, only ever computed from real trade history.
        _ensure_column("trades", "position_id", "position_id TEXT")
        _ensure_column("trades", "exit_reason_category", "exit_reason_category TEXT")
        # Conviction ledger (QAMC remediation spec §7.2): "Log each trade's
        # allocated risk against its realized outcome. If the desk's
        # conviction predicts results, conviction-weighted sizing amplifies
        # the edge. If it does not, flat sizing is superior — and that must
        # be discovered from data, not assumed." Pinned at ENTRY (BUY/SHORT)
        # only, mirroring how expected_horizon_sessions/setup_type are
        # pinned rather than recomputed — see `TradeDecision` in models.py
        # for what each figure means and `ExecutionStage` (pipeline_stages.py)
        # for where it's written. NULL on every legacy row, and on any new
        # entry built from a legacy notional (target_weight_pct-only) target
        # that carried no risk-based plan — never fabricated at migration
        # time or by the backfill (scripts/backfill_conviction_ledger.py),
        # only ever pinned from a real PM decision.
        # --- Stop-floor evidence (board item: minimum stop width) --------
        # The desk's minimum stop width has been re-argued six times in five
        # weeks and every pass ended in "owner appetite", because the desk
        # cannot check its own floor against its own trades: it never
        # recorded the ATR the stop was measured in, how far the trade went
        # against it before resolving, or whether a real level or the ATR
        # band set the stop. These three columns plus the `realized_pnl` /
        # `exit_reason_category` already on the row are exactly what that
        # check needs.
        #
        # WHAT THIS DATA MAY BE USED FOR: showing whether the ratified floor
        # was ever VIOLATED in practice — i.e. whether trades that went on to
        # resolve well were stopped out by a floor that sat inside their
        # ordinary adverse excursion. A falsification test of the current
        # number.
        #
        # WHAT IT MAY NOT BE USED FOR: deriving, tuning, or optimising a
        # multiplier. Doctrine bars fitting a number to this desk's history
        # ("no fitting, only reading"); a floor swept for the value that
        # would have maximised past outcomes is a fitted number no matter how
        # much data backs it. The floor stays read from published doctrine
        # and from the instrument in front of the desk.
        #
        # `entry_atr` — ATR(14) at the moment of entry, in price units.
        # NULL on every legacy row and every non-entry row.
        _ensure_column("trades", "entry_atr", "entry_atr REAL")
        # `stop_basis` — the constructor's own stop rule string (see
        # `src/portfolio_constructor.py::STOP_RULE_*`), which already
        # distinguishes a stop honoured at a COMPUTED structural level from
        # one set by the ATR band. Stored verbatim rather than as a boolean
        # so the absolute-floor and outside-band cases stay distinguishable.
        _ensure_column("trades", "stop_basis", "stop_basis TEXT")
        # `max_adverse_excursion` — the worst price the position reached
        # against its entry while open, in price units, accumulated by
        # `_accumulate_excursions` from each session's position snapshot.
        # A snapshot-frequency floor on the true MAE, never an overstatement:
        # intraday spikes between snapshots are missed, so a reading that
        # says the floor WAS violated is trustworthy while one that says it
        # was not is only "not observed". Any reader must carry that caveat.
        _ensure_column("trades", "max_adverse_excursion", "max_adverse_excursion REAL")
        # `max_favourable_excursion` — the best price the position reached IN
        # ITS FAVOUR against its entry while open, in price units, the exact
        # mirror of `max_adverse_excursion` and accumulated by the same
        # `_accumulate_excursions` call from the same snapshot. It is the
        # other half of the falsification question and cannot be recovered
        # afterwards either: without it, a stop-out recorded alongside a wide
        # adverse excursion cannot be told apart from one that first ran a
        # long way in the desk's favour and gave it all back. Carries the
        # SAME snapshot-frequency caveat (a floor on the true MFE, never an
        # overstatement) and the SAME hard limit on use — falsification of
        # the ratified floor only, never a value to optimise a multiplier
        # against. NULL on every legacy row and every non-entry row.
        _ensure_column(
            "trades",
            "max_favourable_excursion",
            "max_favourable_excursion REAL",
        )
        # `max_adverse_overnight_gap` — SHORT-SIDE GAP EVIDENCE, the worst
        # ADVERSE overnight gap (session open minus the prior session's
        # close, in price units, positive = against the short) observed on
        # any session this short was held. Its sibling
        # `overnight_gap_sessions` counts the sessions on which a gap was
        # actually observed, so a NULL/absent reading is distinguishable
        # from "held, and never gapped against".
        #
        # WHY IT EXISTS (item 186): the short-side sizing haircut this
        # column was added to inform was DELETED by owner ruling 2026-10-04
        # ("a short should be treated the same as a long"). The column stays
        # because what a short suffers overnight is still worth recording —
        # the desk has never recorded it. Bars are fetched live and discarded;
        # no OHLCV table exists. This column, joined to the `entry_atr` and
        # `initial_stop_loss` already pinned on the same opening row, is the
        # evidence that would let the question ever be settled.
        #
        # WHAT IT MAY NOT BE USED FOR: the same hard limit the
        # `max_adverse_excursion` note above states. RECORDING ONLY. Nothing
        # reads it back into a sizing, stop or exit decision, and it may not
        # be swept for the multiple that would have been optimal — doctrine
        # bars fitting a number to this desk's own history. Carries the same
        # snapshot-frequency caveat: a session on which no snapshot ran
        # contributes nothing, so the stored figure is a FLOOR on the worst
        # adverse gap, never an overstatement.
        _ensure_column(
            "trades",
            "max_adverse_overnight_gap",
            "max_adverse_overnight_gap REAL",
        )
        _ensure_column(
            "trades",
            "overnight_gap_sessions",
            "overnight_gap_sessions INTEGER",
        )
        # `last_overnight_gap_date` — the session date of the most recently
        # recorded gap, so a second sync in the same session cannot count
        # the same gap twice. Idempotence by date, not by call count.
        _ensure_column(
            "trades",
            "last_overnight_gap_date",
            "last_overnight_gap_date TEXT",
        )
        # --- Item 55 evidence: WHAT the stop was based on, and what the
        # market then did with that level. RECORDING ONLY (2026-10-01).
        #
        # Board item 55 ("what IS a structural level — how many bars make a
        # swing point, how wide is a level's zone?") has been argued and
        # re-measured repeatedly and never closed, because three
        # measurements of the SAME baseline disagreed. A quantity that
        # unstable cannot govern money, and no further argument fixes it:
        # the desk has never recorded what its own stops were standing on,
        # so it cannot look. These columns are that record.
        #
        # WHAT THIS DATA MAY BE USED FOR: showing that the CURRENT
        # definition is WRONG — that level-backed stops fared no differently
        # from stops with nothing behind them, that a given zone width or
        # pivot window predicted nothing. A falsification.
        #
        # WHAT IT MAY NOT BE USED FOR: picking a better pivot window or zone
        # width by trying candidates against these rows. That is fitting a
        # number to this desk's own history, which doctrine bars outright
        # ("no fitting, only reading"), and it is barred here however much
        # data accumulates.
        #
        # NO CLASSIFICATION IS STORED. "Respected", "pierced and recovered"
        # and "broken outright" all need a cutoff nobody can source today,
        # so only RAW DISTANCES are kept and the classification is derived
        # later by a reader who states its own cutoff. Unknown is NULL.
        #
        # `stop_level_basis` — JSON written at entry by
        # `PortfolioConstructor.shipped_stop_level_basis` (see
        # `src.data.levels.describe_stop_level_basis` for every field): the
        # level's price and kind, its touch count, the pivot window and
        # confirmation span in force, the zone's edges and width, and the
        # signed stop-to-level and entry-to-level distances. Written for
        # trades with NO level behind the stop too (`level_backed: false`),
        # because that is the control group. NULL on legacy rows, non-entry
        # rows, and any entry whose analysis could not produce an honest
        # record.
        _ensure_column("trades", "stop_level_basis", "stop_level_basis TEXT")
        # `level_max_penetration` — the furthest price ever travelled BEYOND
        # the far edge of that level's zone while the position was open, in
        # price units, monotonic (only ever widens) and never negative. Zero
        # or NULL means the zone's far edge was never exceeded in any
        # snapshot. Carries the SAME snapshot-frequency caveat as
        # `max_adverse_excursion`: a floor on the true penetration, so a
        # reading that says the level WAS exceeded is trustworthy while one
        # that says it was not is only "not observed".
        _ensure_column(
            "trades",
            "level_max_penetration",
            "level_max_penetration REAL",
        )
        # `level_closest_approach` — the SMALLEST distance ever seen between
        # price and the NEAR edge of the zone, monotonic downwards, signed:
        # positive means price never reached the zone, zero or negative
        # means it entered. Together with `level_max_penetration` this
        # separates "never came near it", "entered the zone", and "went
        # clean through it" without anyone having to name a tolerance.
        _ensure_column(
            "trades",
            "level_closest_approach",
            "level_closest_approach REAL",
        )
        _ensure_column("trades", "requested_risk_pct", "requested_risk_pct REAL")
        _ensure_column("trades", "allocated_risk_pct", "allocated_risk_pct REAL")
        _ensure_column("trades", "conviction", "conviction TEXT")
        _ensure_column("trades", "decision_model", "decision_model TEXT")
        # Thesis invalidation, as a real column (2026-09-03). Mirrors the
        # conviction-ledger columns immediately above: pinned at ENTRY
        # (BUY/SHORT) only, from `TradeDecision.thesis_invalid_if` (see that
        # field in models.py for the full history — it used to live ONLY as
        # truncated text inside `reasoning`, which could silently lose a
        # long analyst-stated falsifier condition before a later holding-
        # discipline check ever saw it). NULL on every legacy row and on any
        # entry whose target carried no stated condition — never fabricated,
        # only ever pinned from a real PM decision.
        _ensure_column("trades", "thesis_invalid_if", "thesis_invalid_if TEXT")
        # decision_id_status: the honest-absence label for `decision_id` on
        # an exit-family row, mirroring Phase 3.1's pace/pace_status pattern
        # (see `_resolve_decision_id_status` above `class Database`). ALWAYS
        # derived inside `insert_trade`/`insert_stop_out_trade`, never
        # accepted as an argument — same discipline as position_id/
        # exit_reason_category. NULL on legacy rows until
        # `scripts/backfill_conviction_ledger.py` runs; unlike position_id,
        # this backfill is NEVER ambiguous — every insert_trade/insert_
        # stop_out_trade call site in this codebase is enumerated, so a
        # legacy exit-family row's decision_id NULL-ness is a known fact,
        # not a gap to guess at.
        _ensure_column("trades", "decision_id_status", "decision_id_status TEXT")
        # The idempotency key an entry is submitted under, written on the
        # pending_submit row before the submit; the orphan sweep reads the
        # order back by it. NULL on rows written before it existed.
        _ensure_column("trades", "idempotency_key", "idempotency_key TEXT")
        # Defect (d) fix: evening's structured feedback-loop fields. The LLM
        # has produced these every night since the 2026-04 value-lens
        # upgrade, but `save_evening_snapshot` had no parameter for any of
        # them, so they were dropped before ever reaching disk. NULL/'[]' on
        # legacy rows — portfolio_manager's reader treats an empty list as a
        # labelled absence, never a fabricated lesson.
        _ensure_column("insights", "thesis_updates_json", "thesis_updates_json TEXT DEFAULT '[]'")
        _ensure_column("insights", "selection_rules_json", "selection_rules_json TEXT DEFAULT '[]'")
        _ensure_column("insights", "discipline_notes_json", "discipline_notes_json TEXT DEFAULT '[]'")
        _ensure_column(
            "insights",
            "previous_outlook_assessment",
            "previous_outlook_assessment TEXT DEFAULT ''",
        )
        # The price a mover was ranked at when the desk looked (2026-10-13,
        # replaces the deleted 3h re-look cooldown). NULL on older rows; the
        # scan then ranks the name from the prior close, as if never looked at.
        _ensure_column("intraday_evaluations", "ref_price", "ref_price REAL")
        # codex r7 P1 #3: pending_protection_restores table for older DBs
        # that pre-date the orphaned-stop-restore queue. Idempotent.
        try:
            self.conn.execute("""
                CREATE TABLE IF NOT EXISTS pending_protection_restores (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol TEXT NOT NULL,
                    sell_order_id TEXT NOT NULL,
                    position_qty_before_sell REAL NOT NULL,
                    specs_json TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT (datetime('now')),
                    run_id TEXT
                )
            """)
            self.conn.commit()
        except Exception as e:
            _log.error("Schema migration failed for pending_protection_restores: %s", e)

        # Bounded entry re-peg queue for older DBs. Idempotent, mirrors the
        # pending_protection_restores migration above.
        try:
            self.conn.execute("""
                CREATE TABLE IF NOT EXISTS pending_repegs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    trade_row_id INTEGER,
                    symbol TEXT NOT NULL,
                    old_order_id TEXT NOT NULL,
                    new_order_id TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT (datetime('now')),
                    run_id TEXT
                )
            """)
            self.conn.commit()
        except Exception as e:
            _log.error("Schema migration failed for pending_repegs: %s", e)

        # Stage 4 (QAMC): specialist_evidence table for older DBs that
        # pre-date it. Idempotent, mirrors the pending_protection_restores
        # pattern above.
        try:
            self.conn.execute("""
                CREATE TABLE IF NOT EXISTS specialist_evidence (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL,
                    decision_id TEXT,
                    agent_name TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    scope TEXT NOT NULL,
                    symbol TEXT,
                    evidence_json TEXT NOT NULL,
                    timestamp TEXT NOT NULL DEFAULT (datetime('now'))
                )
            """)
            self.conn.commit()
        except Exception as e:
            _log.error("Schema migration failed for specialist_evidence: %s", e)
        ensure_prune_indexes(self.conn)
        _owner_intents(self.conn)  # idempotent, commits
        # Sentinel seams + owed stop amends; idempotent
        ensure_sentinel_tables(conn=self.conn)
        ensure_pending_stop_amend_table(conn=self.conn)
        ensure_trend_tick_table(conn=self.conn)  # trend exit at the tick; idempotent
