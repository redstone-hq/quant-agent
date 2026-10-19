"""Broker-fill reconciliation: entry fills, orphaned pending submits, stop-out fills and the surfacing of what the reconciler found.

Lifted verbatim out of `ProtectionMixin` (src/pipeline_protection.py) as a
standalone class: every collaborator is an explicit keyword-only
constructor argument, so it is built and exercised without a pipeline.
The mixin keeps a thin method of the same name that builds this object
and calls it, so every existing caller and patch target is unchanged.
"""

import logging
from src.sentinel.reconciliation import record_reconciliation
from src.protection.fill_reconciler_records import record_fill_pass
from src.storage.db import Database
from src.trade_actions import broker_fill_time
from src.pipeline_context import RunContext
import json as _json

#: Logs under `src.pipeline`, as the bodies did before the move;
#: binding the name rather than `__name__` keeps log records byte-identical.
logger = logging.getLogger("src.pipeline")

from src.protection.fill_reconciler_helpers import _finite_float_or_none, _reconciled_exit_action  # noqa: F401


class FillReconciler:
    """Broker-fill reconciliation: entry fills, orphaned pending submits, stop-out fills and the surfacing of what the reconciler found."""

    def __init__(self, *, broker, db, config, flag_stop_out_anomaly, format_qty, parse_broker_fill_timestamp) -> None:
        self.broker = broker
        self.db = db
        self.config = config
        self._flag_stop_out_anomaly = flag_stop_out_anomaly
        self._format_qty = format_qty
        self._parse_broker_fill_timestamp = parse_broker_fill_timestamp

    @staticmethod
    def _order_accepted(order: dict, symbol: str, side: str) -> bool:
        """Returns True iff the order payload looks like a live broker order.

        Used before appending to the trades audit log so we don't record
        phantom fills. Alpaca can return an error-shaped dict (missing id, or
        status like 'rejected' / 'expired'); recording those as BUY / SELL
        would make the audit log diverge from broker reality.
        """
        if not order or not order.get("id"):
            logger.error(
                "%s %s: broker returned no order id (payload=%s) — skipping audit",
                side.upper(),
                symbol,
                order,
            )
            return False
        status = (order.get("status") or "").lower()
        if status in ("rejected", "canceled", "cancelled", "expired", "error"):
            logger.error(
                "%s %s: broker rejected order (status=%s) — skipping audit",
                side.upper(),
                symbol,
                status,
            )
            return False
        return True

    def _reconcile_fills(self, ctx: RunContext | None = None) -> None:
        """Update trade rows' fill_status by asking the broker for terminal info.

        Phase 3 groundwork: decouples "we submitted an order" from "the order
        actually filled." Readers (compute_trade_calibration, get_symbol_last_buy,
        recent_sells) filter on fill_status so a limit order that never crossed
        doesn't pollute PM memory or calibration stats.

        A `partially_filled` (or any non-terminal working) order whose broker
        snapshot already shows shares filled has its ACTUAL filled qty/avg
        price recorded while it stays 'submitted', so downstream position /
        cash / calibration see the executed portion immediately instead of
        waiting for a terminal status that may never arrive (item 102). The
        absolute cumulative snapshot is written each pass, so a later partial
        or terminal pass never double-counts the same shares.

        Scoped to a single run_id when ctx is provided — we don't want to
        retroactively flip stale submissions from previous days. Alpaca
        purges order history after a few days; unreconciled-and-unreachable
        orders stay at 'submitted' and are effectively treated as filled by
        the legacy-compat NULL-or-filled filter, which is a tolerable
        failure mode.
        """
        run_id = ctx.run_id if ctx is not None else None
        try:
            rows = self.db.get_unreconciled_orders(run_id=run_id)
            record_fill_pass(self.db, "fills.db_lookup")
        except Exception as e:
            record_fill_pass(self.db, "fills.db_lookup", e)
            return
        if not rows:
            return
        terminal_ok = {"filled"}
        terminal_fail = {"canceled", "cancelled", "expired", "rejected", "done_for_day"}

        def _record_broker_event(row: dict, status: str, fill_qty, fill_price) -> None:
            import json

            try:
                requested = float(row.get("qty") or 0)
                actual = float(fill_qty or 0)
                action = str(row.get("action") or "")
                event_run_id = row.get("run_id") or (ctx.run_id if ctx else None)
                if not event_run_id:
                    return
                if actual > 0:
                    outcome = "filled" if requested <= 0 or actual + 1e-9 >= requested else "partially_filled"
                else:
                    outcome = status
                payload = {
                    "stage": "order",
                    "outcome": outcome,
                    "reason": "broker_reconciliation",
                    "broker_status": status,
                    "broker_order_id": row.get("broker_order_id"),
                    "fill_qty": actual or None,
                    "fill_price": fill_price,
                }
                self.db.insert_specialist_evidence(
                    run_id=event_run_id,
                    agent_name="pipeline",
                    kind="pipeline_event",
                    scope="symbol",
                    symbol=row.get("symbol"),
                    decision_id=row.get("decision_id"),
                    evidence_json=json.dumps(payload, sort_keys=True),
                )
                if actual > 0 and action not in {"BUY", "SWEEP_BUY", "HOLD"}:
                    self.db.insert_specialist_evidence(
                        run_id=event_run_id,
                        agent_name="pipeline",
                        kind="pipeline_event",
                        scope="symbol",
                        symbol=row.get("symbol"),
                        decision_id=row.get("decision_id"),
                        evidence_json=_json.dumps(
                            {
                                "stage": "position_management",
                                "outcome": "exited"
                                if requested <= 0 or actual + 1e-9 >= requested
                                else "partially_exited",
                                "reason": action.lower(),
                                "broker_status": status,
                                "fill_qty": actual,
                                "fill_price": fill_price,
                            },
                            sort_keys=True,
                        ),
                    )
                record_fill_pass(self.db, "fills.lifecycle_evidence")
            except Exception as e:  # evidence is never trading authority
                record_fill_pass(self.db, "fills.lifecycle_evidence", e)

        for row in rows:
            order_id = row.get("broker_order_id")
            if not order_id:
                continue
            try:
                info = self.broker.get_order_fill_info(order_id)
                record_fill_pass(self.db, "fills.broker_lookup", context={"order": order_id})
            except Exception as e:
                record_fill_pass(self.db, "fills.broker_lookup", e, context={"order": order_id})
                continue
            if info is None:
                continue
            status = info.get("status") or ""
            fill_qty = info.get("filled_qty") or None
            fill_price = info.get("filled_avg_price") or None
            # The broker's own fill time, never the time we noticed: the clock
            # the lot walk orders fills by (None when the broker reported none).
            filled_at = broker_fill_time(info.get("filled_at"))  # full precision
            if status in terminal_ok:
                self.db.update_trade_fill(
                    broker_order_id=order_id,
                    fill_status="filled",
                    fill_qty=fill_qty,
                    fill_price=fill_price,
                    filled_at=filled_at,
                )
                # Owner ruling 5b: edit the message that listed this order as
                # placed. The fill above has committed; this only starts a
                # background edit and can never raise into reconciliation.
                try:
                    from src.notifier.fill_line import note_fill

                    note_fill(self.db, order_id)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("fill edit for %s not started: %s", order_id, exc)
                _record_broker_event(row, status, fill_qty, fill_price)
                logger.info(
                    "Reconciled %s: filled (qty=%s, avg=$%s)",
                    order_id,
                    fill_qty,
                    fill_price,
                )
            elif status in terminal_fail:
                self.db.update_trade_fill(
                    broker_order_id=order_id,
                    fill_status=status,
                    fill_qty=fill_qty,
                    fill_price=fill_price,
                    filled_at=filled_at,
                )
                _record_broker_event(row, status, fill_qty, fill_price)
                if fill_qty and float(fill_qty) > 0:
                    logger.warning(
                        "Reconciled %s: terminal status=%s with partial fill (qty=%s, avg=$%s)",
                        order_id,
                        status,
                        fill_qty,
                        fill_price,
                    )
                else:
                    logger.warning("Reconciled %s: did NOT fill (status=%s)", order_id, status)
            elif str(status).lower() == "partially_filled":
                # A genuine partial: the broker reports shares filled on an
                # order that is still working. RECORD the actually-filled
                # qty/avg price now so position, cash and calibration see
                # reality — but KEEP fill_status 'submitted' so
                # get_unreconciled_orders re-picks the row and the eventual
                # terminal transition still lands (item 102). We write the
                # broker's ABSOLUTE cumulative snapshot (filled_qty /
                # filled_avg_price), never a delta, so re-seeing the same
                # partial, a growing partial, or the final terminal 'filled'
                # can never double-count the same shares: every downstream
                # consumer reads the row's absolute fill_qty once, and
                # realized_pnl is recomputed from scratch on each write.
                #
                # Match the status string EXACTLY (not "any non-terminal")
                # so an unstubbed / garbage broker snapshot can't be misread
                # as a partial. Both numeric fields are coerced to a finite
                # float or None before they touch the DB: fill_price is a
                # nullable column, so a broker that reports filled_qty before
                # a numeric avg price records the qty with a null price now
                # and backfills the price on a later pass (the row stays
                # 'submitted'). Never bind a non-numeric value.
                partial = _finite_float_or_none(fill_qty)
                price = _finite_float_or_none(fill_price)
                if partial is not None and partial > 0:
                    prev = _finite_float_or_none(row.get("fill_qty")) or 0.0
                    self.db.update_trade_fill(
                        broker_order_id=order_id,
                        fill_status="submitted",
                        fill_qty=partial,
                        fill_price=price,
                        filled_at=filled_at,
                    )
                    # Only emit lifecycle evidence / log on a genuine INCREASE
                    # in filled shares, so repeated partial passes over an
                    # unchanged fill don't spam PM memory with duplicate events.
                    if partial > prev + 1e-9:
                        _record_broker_event(row, status, partial, price)
                        logger.info(
                            "Reconciled %s: partial fill recorded "
                            "(status=%s, qty=%s, avg=%s); order stays open "
                            "for the remainder",
                            order_id,
                            status,
                            partial,
                            price,
                        )

    def _reconcile_orphan_pending_submits(self) -> int:
        """Resolve BUY write-ahead orphans (audit F4).

        A crash between broker.submit_order() returning and
        confirm_trade_submitted() landing leaves a 'pending_submit' row
        with broker_order_id=NULL while the broker may actually hold (and
        fill) the order. Nothing swept these, so the fill went untracked
        forever — position/cash drift. For each orphan:

          - exactly ONE broker order matching symbol+side+qty → adopt its
            id (confirm_trade_submitted); _reconcile_fills then resolves
            the fill normally.
          - broker query FAILED (list_recent_orders → None) → leave the
            row; retry next session. NEVER mark submit_failed on a
            transient API failure (review #2): a real / already-filled
            BUY would be silently dropped.
          - query OK + ZERO matching orders → the submit never landed;
            mark submit_failed.
          - AMBIGUOUS (>1 candidate) → do NOT guess. Adopting the wrong
            order would mis-track real money — leave the row pending and
            ERROR-log for manual reconciliation.

        Best-effort and self-contained: any per-row failure is logged and
        skipped, never breaks the session. Called once per session at
        entry, beside _drain_pending_protection_restores.
        """
        from datetime import datetime, timedelta, timezone

        try:
            rows = self.db.get_orphaned_pending_submits()
            record_fill_pass(self.db, "orphan.db_read")
        except Exception as exc:
            record_fill_pass(self.db, "orphan.db_read", exc)
            return 0
        if not rows:
            return 0
        resolved = 0
        # Generous lookback — Alpaca submitted_at vs our insert timestamp
        # plus any clock skew. A day covers every realistic crash-restart.
        from src.trading_calendar import utc_now

        after = utc_now() - timedelta(hours=24)
        for row in rows:
            row_id = row["id"]
            symbol = row["symbol"]
            try:
                want_qty = float(row.get("qty") or 0)
            except (TypeError, ValueError):
                want_qty = 0.0
            # A SHORT entry is a sell order. A row carrying its order's own
            # idempotency key is read back by that key (one exact answer);
            # rows written before the key was stored keep the side search.
            side = "sell" if str(row.get("action") or "").upper() == "SHORT" else "buy"
            key = row.get("idempotency_key")
            try:
                if key:
                    candidates = self.broker.list_recent_orders(symbol, side, after, key=key)
                else:
                    candidates = self.broker.list_recent_orders(symbol, side, after)
                record_fill_pass(self.db, "orphan.broker_query", context={"symbol": symbol, "row": row_id})
            except Exception as exc:
                record_fill_pass(self.db, "orphan.broker_query", exc, context={"symbol": symbol, "row": row_id})
                continue
            if candidates is None:
                # Query FAILED (not "no such order"). Marking
                # submit_failed here would discard a possibly-real /
                # already-filled BUY. Leave the row for next session.
                logger.warning(
                    "orphan-sweep: broker order query unavailable for %s "
                    "row %d — leaving pending_submit for next session "
                    "(NOT marking submit_failed on a transient failure)",
                    symbol,
                    row_id,
                )
                continue
            matches = [c for c in candidates if c.get("id") and abs(float(c.get("qty") or 0) - want_qty) < 1e-6]
            if len(matches) == 1:
                bid = matches[0]["id"]
                try:
                    self.db.confirm_trade_submitted(row_id, broker_order_id=bid)
                    resolved += 1
                    logger.warning(
                        "orphan-sweep: adopted broker order %s for %s row %d "
                        "(BUY write-ahead survived a crash) — _reconcile_fills "
                        "will resolve its fill",
                        bid,
                        symbol,
                        row_id,
                    )
                    record_fill_pass(self.db, "orphan.adopt", context={"symbol": symbol, "row": row_id})
                except Exception as exc:
                    record_fill_pass(self.db, "orphan.adopt", exc, context={"symbol": symbol, "row": row_id})
            elif not matches:
                try:
                    self.db.mark_trade_submit_failed(row_id)
                    resolved += 1
                    logger.warning(
                        "orphan-sweep: no broker order matches %s row %d "
                        "(qty=%.4f) — submit never landed; marked "
                        "submit_failed",
                        symbol,
                        row_id,
                        want_qty,
                    )
                    record_fill_pass(self.db, "orphan.mark_failed", context={"symbol": symbol, "row": row_id})
                except Exception as exc:
                    record_fill_pass(self.db, "orphan.mark_failed", exc, context={"symbol": symbol, "row": row_id})
            else:
                logger.error(
                    "orphan-sweep: %d ambiguous broker orders for %s row %d "
                    "(qty=%.4f) — NOT guessing (mis-adoption mis-tracks "
                    "money); leaving pending_submit for manual review",
                    len(matches),
                    symbol,
                    row_id,
                    want_qty,
                )
        if resolved:
            logger.info("orphan-sweep: resolved %d pending_submit row(s)", resolved)
        return record_reconciliation(db=self.db, kind="orphan_submits", result=resolved)

    @staticmethod
    def _parse_broker_fill_timestamp(filled_at: str | None) -> str | None:
        """Convert a broker `filled_at` ISO-8601 string to the naive-UTC
        `trades.timestamp` format (`Database._sqlite_utc_timestamp`).

        Backdating a stop-out row to when it ACTUALLY filled (rather than
        to whenever this reconciler happened to notice) is what makes
        `compute_trade_calibration`'s hold-days and win/loss dating
        measure the real
        exit instead of the detection lag. This is safe to do: the FIFO
        cost-basis walk in `_realized_pnl_through_trade` orders by `id`,
        not `timestamp`, so backdating this column can never corrupt a
        realized_pnl computation — id order already reflects insertion
        order, which is always AFTER every row it needs to net against.

        Returns None (→ `insert_stop_out_trade` falls back to "now") when
        the broker didn't report a fill time or the string doesn't parse —
        never raises, never guesses a fake time.
        """
        if not filled_at:
            return None
        try:
            from datetime import datetime as _dt

            dt = _dt.fromisoformat(filled_at)
        except (TypeError, ValueError):
            return None
        return Database._sqlite_utc_timestamp(dt)

    def _flag_stop_out_anomaly(
        self,
        *,
        run_id: str | None,
        symbol: str,
        outcome: str,
        detail: str,
        **extra,
    ) -> None:
        """Write a `specialist_evidence` flag for a stop-out reconciliation
        anomaly — mirrors `_reconcile_fills`'s `_record_broker_event` shape
        so ops tooling that already reads `kind='pipeline_event'` rows sees
        this the same way. Always ALSO logged at ERROR: the whole point of
        "fail loud" is that this must not depend on anyone going looking in
        the evidence table (2026-08-28 ONDS/CCJ sat silent for a full
        trading day before anyone noticed realized_pnl was NULL)."""
        import json

        logger.error("stop-out reconcile: %s %s — %s", symbol, outcome, detail)
        if not run_id:
            return
        try:
            payload = {
                "stage": "reconciliation",
                "outcome": outcome,
                "reason": "stop_out_reconciler",
                "detail": detail,
                **extra,
            }
            self.db.insert_specialist_evidence(
                run_id=run_id,
                agent_name="pipeline",
                kind="pipeline_event",
                scope="symbol",
                symbol=symbol,
                evidence_json=json.dumps(payload, sort_keys=True, default=str),
            )
            record_fill_pass(self.db, "flag_anomaly", context={"symbol": symbol})
        except Exception as exc:  # noqa: BLE001 — evidence is never trading authority
            record_fill_pass(self.db, "flag_anomaly", exc, context={"symbol": symbol})

    def _reconcile_stop_out_fills(self, run_id: str | None = None) -> list[dict]:
        """Write back exits the broker made unilaterally that the ledger
        never heard about — closing the 2026-08-28 ONDS/CCJ accounting gap.

        WHAT HAPPENED: ONDS (17 sh @ 8.53, bought 2026-08-27) and CCJ (2 sh
        @ 107.465, bought 2026-08-27) were both closed by their broker-
        resident GTC protective stop-limit order on 2026-08-28 — ONDS at
        7.93 (realized -$10.20), CCJ at 102.955 (realized -$9.02). The
        `positions` table (a derived snapshot of `AlpacaBroker.get_positions`
        via `_sync_positions_from_broker` / `Database.sync_positions`) correctly went to
        zero for both. The `trades` table did not: no SELL/exit row was
        ever written, and the original BUY rows sat forever at
        `realized_pnl IS NULL`. Across the whole ledger, `realized_pnl` was
        set on exactly 4 of 36 trades — every one an exit the system itself
        had submitted (SELL / REDUCE / TRAIL_STOP / SWEEP_SELL all call
        `insert_trade` at submission time, and `_reconcile_fills` /
        `update_trade_fill` fill in `realized_pnl` once the broker confirms
        the fill). A protective stop is different: `place_entry_protection`,
        `_repair_stop_coverage`, and `shift_stops_down` all place a REAL
        order at the broker, but none of them ever write that order into
        `trades` — there was no row for `_reconcile_fills` to find, so a
        stop-out was invisible to the ledger by construction, not by bug in
        the reconciliation LOOP itself.

        Why this matters more than a bookkeeping nit: every exit the ledger
        DOES record is one the system chose; every exit it misses is one
        the market forced. Those are not a random sample of trades — a
        protective stop only fires on a LOSS. Silently dropping stop-outs
        biases every realized-P&L figure upward and starves
        `compute_trade_calibration` / the position reviewer / Phase 7
        measurement of exactly the outcomes most worth learning from.

        HOW THIS DETECTS IT (broker-truth diff, not a stop-order allowlist):
        compare what the ledger BELIEVES it holds per symbol
        (`Database.get_symbols_with_open_ledger_qty` — BUY/SWEEP_BUY minus
        every other executed exit) against what the broker ACTUALLY shows
        (`AlpacaBroker.get_positions`). Whenever the ledger claims more
        shares than the broker has, something closed part or all of that
        position without telling the ledger. For each such symbol, ask the
        broker directly for filled SELL orders since the reconciliation
        lookback window (`ReconciliationConfig.stop_out_lookback_days`) and
        record any whose broker_order_id the ledger has never seen — this
        catches the ORIGINAL entry-protection stop, a coverage-repair
        replacement, an ex-dividend-shifted stop, or any other broker-side
        SELL this process placed but never logged, without needing to
        enumerate every code path that can place one.

        WHAT COUNTS AS A MISSED SELL (2026-10-19 XOM phantom): only a sell
        that filled AFTER the current position opened (the ledger's own
        earliest BUY of that position chain), and only the one set of WHOLE
        broker orders whose total equals the gap exactly (1e-6). Otherwise
        nothing is written and the owner is paged with the candidates. A
        ledger SHORT of the broker is paged too, never passed silently.

        Scoped to LONGS only (a positive ledger/broker qty gap): a short's
        protective stop is a BUY-to-cover, which is deliberately deferred —
        no order path in this repo can open a short's exit position yet
        that this reconciler would need to untangle from a BUY-to-cover
        stop (see shorts-safe's staged rollout). Flagged, not silently
        skipped, if a short ever does show a mismatch (see below).

        Idempotent by construction: `Database.insert_stop_out_trade` keys
        on `broker_order_id` under the same lock as the check, so however
        many of the 5 session entry points (morning / intra_check / midday
        / close / evening) run this, and however many times each does, a
        given stop-out fill is written exactly once.

        FAIL LOUD, NEVER GUESS: when a gap is found but the broker's own
        order history doesn't explain it (query failure, or genuinely no
        matching filled SELL inside the lookback window), this does NOT
        invent a price or silently move on — it logs at ERROR and writes a
        `specialist_evidence` flag an operator can find. Same discipline
        for a recorded stop-out whose `realized_pnl` comes back NULL
        because the ledger's own BUY history can't cover the exited
        quantity (`_realized_pnl_through_trade` already refuses to guess
        there) — the row is still written (never dropped), just flagged.

        Returns a list of `{symbol, ledger_qty, broker_qty, matched,
        recorded}` dicts describing what this pass found, for the caller /
        tests to inspect. Every branch is defensive: a broker or DB failure
        on one symbol is logged and skipped, never aborts the pass for the
        rest of the book.
        """
        reco_cfg = getattr(getattr(self, "config", None), "reconciliation", None)
        if reco_cfg is None:
            # No config attached (unit-test pipelines built via
            # TradingPipeline.__new__, or a settings.yaml genuinely missing
            # the section before ReconciliationConfig's default_factory
            # applies) — mirrors _force_delever's same defensive bail.
            return []
        lookback_days = reco_cfg.stop_out_lookback_days

        try:
            ledger_qty = self.db.get_symbols_with_open_ledger_qty()
            record_fill_pass(self.db, "stop_out.ledger_qty")
        except Exception as exc:  # noqa: BLE001
            record_fill_pass(self.db, "stop_out.ledger_qty", exc)
            return []
        if not ledger_qty:
            return []

        try:
            broker_positions = self.broker.get_positions()
            record_fill_pass(self.db, "stop_out.broker_positions")
        except Exception as exc:  # noqa: BLE001
            record_fill_pass(self.db, "stop_out.broker_positions", exc)
            return []
        broker_qty: dict[str, float] = {}
        for p in broker_positions or []:
            symbol = getattr(p, "symbol", None)
            if not symbol:
                continue
            try:
                broker_qty[symbol] = float(getattr(p, "qty", 0) or 0)
            except (TypeError, ValueError):
                continue

        from datetime import datetime, timedelta, timezone

        from src.trading_calendar import utc_now

        after = utc_now() - timedelta(days=lookback_days)

        results: list[dict] = []
        for symbol, ledger_open in ledger_qty.items():
            if ledger_open <= 1e-6:
                continue  # ledger already believes it's flat — nothing to reconcile
            held = broker_qty.get(symbol, 0.0)
            gap = ledger_open - held
            if gap < -1e-6:
                # The ledger is SHORT of the broker: the broker holds more
                # than the desk recorded. Not something this reconciler can
                # write back (it only records sells), but never silent — on
                # 2026-10-19 a phantom write-back left XOM at 31 vs the
                # broker's 36 and this branch passed it without a word.
                self._alert_ledger_short(run_id, symbol, ledger_open, held)
                results.append(
                    {"symbol": symbol, "ledger_qty": ledger_open, "broker_qty": held, "matched": False, "recorded": 0}
                )
                continue
            if gap <= 1e-6:
                continue  # ledger and broker agree within the qty tolerance

            try:
                known_ids = self.db.get_known_broker_order_ids(symbol)
                record_fill_pass(self.db, "stop_out.known_ids", context={"symbol": symbol})
            except Exception as exc:  # noqa: BLE001
                record_fill_pass(self.db, "stop_out.known_ids", exc, context={"symbol": symbol})
                continue
            try:
                fills = self.broker.list_filled_sell_orders(symbol, after=after)
                record_fill_pass(self.db, "stop_out.fill_query", context={"symbol": symbol})
            except Exception as exc:  # noqa: BLE001
                record_fill_pass(self.db, "stop_out.fill_query", exc, context={"symbol": symbol})
                continue
            if fills is None:
                # Query FAILED (not "no fills") — same None-means-retry
                # contract as list_recent_orders. Leave the gap for the
                # next reconciliation pass rather than concluding anything.
                logger.warning(
                    "stop-out reconcile: broker order query unavailable for "
                    "%s (ledger=%.4f, broker=%.4f) — leaving the gap for "
                    "the next pass",
                    symbol,
                    ledger_open,
                    held,
                )
                continue

            # A sell the ledger missed can only be one that closed THIS
            # position, so it must have filled after this position opened
            # (its own earliest BUY since the ledger was last flat). On
            # 2026-10-19 a 24 Aug desk sale from before the current XOM
            # position was written back as if it had closed it. Unknown open
            # time -> nothing is eligible -> the owner is paged below.
            opened_at = self._current_position_opened_at(symbol)
            new_fills = [
                f
                for f in fills
                if f.get("id") and f["id"] not in known_ids and _filled_after(f.get("filled_at"), opened_at)
            ]
            if not new_fills:
                self._flag_stop_out_anomaly(
                    run_id=run_id,
                    symbol=symbol,
                    outcome="stop_out_gap_unexplained",
                    detail=(
                        f"ledger believes {ledger_open:.4f} sh open, broker "
                        f"shows {held:.4f}, but no untracked filled SELL "
                        f"order filled after this position opened "
                        f"({opened_at or 'open time unknown'}) in the last "
                        f"{lookback_days} day(s) — recording nothing rather than guessing"
                    ),
                    ledger_qty=ledger_open,
                    broker_qty=held,
                    lookback_days=lookback_days,
                )
                # PAGE the owner. Until 2026-09-17 this wrote an ERROR line
                # and an evidence flag and nothing else, which is the same
                # silence that let the 2026-08-28 ONDS/CCJ stop-outs sit
                # unnoticed for a trading day. The desk's record and the
                # broker's record disagree and no sale explains it: that is
                # fill confirmation having failed somewhere upstream, and it
                # is the owner's P&L that is wrong because of it.
                try:
                    from src.notifier import alert_records_disagree_with_broker

                    alert_records_disagree_with_broker(
                        symbol,
                        desk_qty=ledger_open,
                        broker_qty=held,
                        lookback_days=lookback_days,
                    )
                    record_fill_pass(self.db, "stop_out.disagree_alert", context={"symbol": symbol})
                except Exception as exc:  # noqa: BLE001
                    record_fill_pass(self.db, "stop_out.disagree_alert", exc, context={"symbol": symbol})
                results.append(
                    {
                        "symbol": symbol,
                        "ledger_qty": ledger_open,
                        "broker_qty": held,
                        "matched": False,
                        "recorded": 0,
                    }
                )
                continue

            # Write back only WHOLE broker orders whose total closes the
            # measured gap exactly; anything else is the owner's call.
            chosen = _whole_orders_closing_gap(new_fills, gap)
            if chosen is None:
                self._alert_gap_without_exact_fit(run_id, symbol, ledger_open, held, new_fills, opened_at)
                results.append(
                    {"symbol": symbol, "ledger_qty": ledger_open, "broker_qty": held, "matched": False, "recorded": 0}
                )
                continue

            recorded = 0
            for fill in chosen:
                # item 173(a): record the action the broker fill actually was,
                # never a blanket STOP_OUT. The broker reports each fill's
                # order_type; a market/limit sell must not be attributed to a
                # protective stop, and a fill whose type doesn't prove it was a
                # stop is recorded as an unattributed reconciled exit.
                action = _reconciled_exit_action(fill.get("order_type"))
                try:
                    row_id, created = self.db.insert_stop_out_trade(
                        symbol=symbol,
                        qty=fill["qty"],
                        price=fill["price"],
                        broker_order_id=fill["id"],
                        filled_at=broker_fill_time(fill.get("filled_at")),
                        run_id=run_id,
                        action=action,
                    )
                    record_fill_pass(
                        self.db, "stop_out.record_fill", context={"symbol": symbol, "order": fill.get("id")}
                    )
                except Exception as exc:  # noqa: BLE001
                    record_fill_pass(
                        self.db, "stop_out.record_fill", exc, context={"symbol": symbol, "order": fill.get("id")}
                    )
                    continue
                if not created:
                    # Another session's pass already recorded this exact
                    # broker order — expected under the idempotency
                    # contract, not an error.
                    continue
                recorded += 1
                # Read back THE ROW JUST WRITTEN by its id. The newest row by
                # timestamp is the wrong row whenever the broker fill is older
                # than the latest ledger row (2026-10-19 XOM: mislabelled).
                realized = None
                for r in self.db.get_trades(symbol=symbol, limit=-1):
                    if r.get("id") == row_id:
                        realized = r.get("realized_pnl")
                        break
                logger.warning(
                    "EXIT RECORDED (%s): %s %s sh @ $%.4f (order %s, "
                    "type=%s, realized_pnl=%s) — broker-initiated exit "
                    "written back to the ledger by the reconciler",
                    action,
                    symbol,
                    self._format_qty(fill["qty"]),
                    fill["price"],
                    fill["id"],
                    fill.get("order_type") or "unknown",
                    "unknown" if realized is None else f"${realized:.2f}",
                )
                if realized is None:
                    self._flag_stop_out_anomaly(
                        run_id=run_id,
                        symbol=symbol,
                        outcome="stop_out_pnl_unmatched",
                        detail=(
                            f"order {fill['id']} recorded ({fill['qty']} sh "
                            f"@ ${fill['price']:.4f}) but realized_pnl could "
                            f"not be computed — the ledger's own BUY history "
                            f"doesn't cover this exit quantity; needs manual "
                            f"review, not a guessed number"
                        ),
                        broker_order_id=fill["id"],
                        qty=fill["qty"],
                        price=fill["price"],
                    )
            results.append(
                {
                    "symbol": symbol,
                    "ledger_qty": ledger_open,
                    "broker_qty": held,
                    "matched": True,
                    "recorded": recorded,
                }
            )
        return record_reconciliation(db=self.db, kind="stop_out_fills", result=results, run_id=run_id)

    def _current_position_opened_at(self, symbol: str) -> str | None:
        """When the position the ledger holds in `symbol` opened, as fixed-width UTC text, or None if unknown.

        The ledger's own record: the earliest executed BUY of the current
        position chain (`get_position_open_row`, the same lookup the trail
        uses), timed by its broker fill, else by when it was written. None
        when the chain is unknown — the caller then treats no sale as
        eligible, never a guessed date.
        """
        try:
            open_row = self.db.get_position_open_row(self.db.get_symbol_last_buy(symbol))
            record_fill_pass(self.db, "stop_out.position_open", context={"symbol": symbol})
        except Exception as exc:  # noqa: BLE001
            record_fill_pass(self.db, "stop_out.position_open", exc, context={"symbol": symbol})
            return None
        if not open_row:
            return None
        return broker_fill_time(open_row.get("filled_at") or open_row.get("timestamp"))

    def _page_owner(self, symbol: str, outcome: str, body: str, run_id: str | None, **evidence) -> None:
        """Flag the evidence row and page the owner through the existing owner-alert path."""
        self._flag_stop_out_anomaly(run_id=run_id, symbol=symbol, outcome=outcome, detail=body, **evidence)
        try:
            from src.notifier import send_owner_alert

            send_owner_alert(body, symbols=[symbol])
            record_fill_pass(self.db, f"stop_out.{outcome}_alert", context={"symbol": symbol})
        except Exception as exc:  # noqa: BLE001
            record_fill_pass(self.db, f"stop_out.{outcome}_alert", exc, context={"symbol": symbol})

    def _alert_ledger_short(self, run_id: str | None, symbol: str, ledger_open: float, held: float) -> None:
        """Page: the broker holds MORE shares than the desk recorded."""
        fq = self._format_qty
        body = (
            "RECORDS DISAGREE — the broker holds more than the desk recorded\n"
            f"{symbol}: the desk's records say {fq(ledger_open)} share(s); the broker shows {fq(held)}, "
            f"{fq(held - ledger_open)} more. Nothing was written back or changed. "
            f"Treat the desk's profit-and-loss for {symbol} as unreliable until this is resolved."
        )
        self._page_owner(symbol, "stop_out_ledger_short", body, run_id, ledger_qty=ledger_open, broker_qty=held)

    def _alert_gap_without_exact_fit(self, run_id, symbol, ledger_open, held, candidates, opened_at) -> None:
        """Page: unrecorded broker sales exist, but no single set of whole orders equals the gap."""
        fq = self._format_qty
        listed = "; ".join(
            f"{fq(f.get('qty'))} sh @ ${float(f.get('price') or 0):.2f} on {broker_fill_time(f.get('filled_at'))}"
            for f in candidates
        )
        body = (
            "RECORDS DISAGREE — unrecorded broker sales do not add up to the difference\n"
            f"{symbol}: the desk's records say {fq(ledger_open)} share(s); the broker shows {fq(held)}, "
            f"a gap of {fq(ledger_open - held)}. Unrecorded broker sales since this position opened "
            f"({opened_at}): {listed}. No single set of these whole orders equals the gap exactly, "
            "so nothing was written back."
        )
        self._page_owner(
            symbol,
            "stop_out_gap_no_exact_fit",
            body,
            run_id,
            ledger_qty=ledger_open,
            broker_qty=held,
            candidates=[f.get("id") for f in candidates],
        )

    def _surface_reconcile_outcomes(
        self,
        reco_results: list[dict] | None = None,
        drained_count: list[dict] | None = None,
        *,
        run_id: str | None = None,
    ) -> None:
        """Route dropped reconciler return values to the owner feed.

        Item 101: both `_reconcile_stop_out_fills` (returns a per-symbol list
        of what it wrote back) and `_drain_pending_protection_restores`
        (returns one record per re-protected naked position; this parameter keeps
        its old `drained_count` name because callers pass it by keyword) do their write-back
        silently — every call site discarded the return value, so a
        broker-side stop-out reached the owner NOWHERE and a re-protection
        was equally invisible. This is the single surfacing point the call
        sites feed those return values into.

        Does NOT change the reconciliation logic: it only reads what already
        happened and pages the owner through the SAME `send_owner_alert` path
        the unexplained-gap branch already uses (`alert_records_disagree_
        with_broker`). A recorded stop-out is a real forced-loss exit, so per
        the alert-design rule it gets its own standalone message rather than a
        bundled session line.

        Never raises — a surfacing fault must not break the trading path it
        reports on, matching `send_owner_alert`'s own contract.
        """
        try:
            from src.notifier import (
                alert_positions_reprotected,
                alert_stop_out_recorded,
            )

            if drained_count:
                try:
                    alert_positions_reprotected(list(drained_count))
                    record_fill_pass(self.db, "surface.reprotect_alert")
                except Exception as exc:  # noqa: BLE001
                    record_fill_pass(self.db, "surface.reprotect_alert", exc)

            for res in reco_results or []:
                if not (res.get("matched") and res.get("recorded")):
                    continue
                symbol = res.get("symbol")
                if not symbol:
                    continue
                # Pull the rows this pass just wrote so the page carries the
                # WHY (qty / price / realized P&L) rather than only a count.
                # `insert_stop_out_trade` stamps each row with action
                # 'STOP_OUT' and this run_id, so filtering on both isolates
                # exactly what THIS pass recorded for THIS symbol — never an
                # older stop-out from a previous session/run.
                try:
                    rows = self.db.get_trades(symbol=symbol, limit=50)
                    record_fill_pass(self.db, "surface.trade_lookup", context={"symbol": symbol})
                except Exception as exc:  # noqa: BLE001
                    record_fill_pass(self.db, "surface.trade_lookup", exc, context={"symbol": symbol})
                    continue
                surfaced = 0
                for r in rows:
                    if surfaced >= int(res.get("recorded") or 0):
                        break
                    if r.get("action") != "STOP_OUT":
                        continue
                    if run_id is not None and r.get("run_id") != run_id:
                        continue
                    try:
                        alert_stop_out_recorded(
                            symbol=symbol,
                            qty=r.get("fill_qty", r.get("qty")),
                            price=r.get("fill_price", r.get("price")),
                            realized_pnl=r.get("realized_pnl"),
                        )
                        surfaced += 1
                        record_fill_pass(self.db, "surface.stop_out_alert", context={"symbol": symbol})
                    except Exception as exc:  # noqa: BLE001
                        record_fill_pass(self.db, "surface.stop_out_alert", exc, context={"symbol": symbol})
            record_fill_pass(self.db, "surface.outer")
        except Exception as exc:  # noqa: BLE001
            record_fill_pass(self.db, "surface.outer", exc)


def _filled_after(raw_fill_time, opened_at: str | None) -> bool:
    """True only when the broker fill is provably later than the position's open (both fixed-width UTC text)."""
    filled = broker_fill_time(raw_fill_time)
    return bool(opened_at and filled and filled > opened_at)


def _whole_orders_closing_gap(fills: list[dict], gap: float) -> list[dict] | None:
    """The ONE set of whole broker orders whose quantities sum to `gap` (within 1e-6), else None.

    None when no set fits or more than one distinct set does — either way the
    write-back would be a guess. Standard-library `itertools.combinations`.
    """
    from itertools import combinations

    fits: list[tuple] = []
    for size in range(1, len(fills) + 1):
        for combo in combinations(fills, size):
            if abs(sum(float(f.get("qty") or 0) for f in combo) - gap) <= 1e-6:
                fits.append(combo)
                if len(fits) > 1:
                    return None
    return list(fits[0]) if fits else None
