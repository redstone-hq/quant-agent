"""src.intraday.safety -- the FREE intra-check safety pass: the reconcile-and-drain preamble and
the session that runs it alone.

Bodies moved verbatim from src/pipeline_intraday.py (`IntradayMixin`), which keeps
same-named thin shims built per call. Every collaborator is an explicit keyword-only
constructor argument, so this builds and runs with no pipeline behind it. Host
attributes a body both reads and ASSIGNS go through `state` (a get/set view the shim
hands in), never a construction-time copy.
"""

import logging

from src.pipeline_context import RunContext
from src.sentinel.guarded_site import record_site as _site

#: The moved code logged under `src.pipeline` before the move and still does;
#: binding the name rather than `__name__` keeps log records byte-identical.
logger = logging.getLogger("src.pipeline")


class IntradaySafety:
    """The FREE intra-check safety pass: the reconcile-and-drain preamble and the session that runs it
    alone. Standalone, built from explicit collaborators.
    """

    # Declared beside the audit that writes `stop_coverage_gaps`; read by
    # src/sessions/stop_audit.py and enforced by tests/test_protection_undetermined.py.
    AUDITS_BROKER_STOPS = True

    def __init__(
        self,
        *,
        db=None,
        broker=None,
        attach_pnl=None,
        record_account_snapshot=None,
        is_trading_day=None,
        kill_switch_halt_result=None,
        blocking_owner_session=None,
        drain_pending_protection_restores=None,
        drain_pending_repegs=None,
        intraday_scan_process_lock=None,
        reconcile_fills=None,
        reconcile_orphan_pending_submits=None,
        reconcile_stop_coverage=None,
        reconcile_stop_out_fills=None,
        surface_reconcile_outcomes=None,
        run_intra_safety_preamble=None,
        state=None,
    ) -> None:
        self.db = db
        self.broker = broker
        self._attach_pnl = attach_pnl
        self._record_account_snapshot = record_account_snapshot
        self._is_trading_day = is_trading_day
        self._kill_switch_halt_result = kill_switch_halt_result
        self._blocking_owner_session = blocking_owner_session
        self._drain_pending_protection_restores = drain_pending_protection_restores
        self._drain_pending_repegs = drain_pending_repegs
        self._intraday_scan_process_lock = intraday_scan_process_lock
        self._reconcile_fills = reconcile_fills
        self._reconcile_orphan_pending_submits = reconcile_orphan_pending_submits
        self._reconcile_stop_coverage = reconcile_stop_coverage
        self._reconcile_stop_out_fills = reconcile_stop_out_fills
        self._surface_reconcile_outcomes = surface_reconcile_outcomes
        if run_intra_safety_preamble is not None:
            self._run_intra_safety_preamble = run_intra_safety_preamble  # else: this part's own body
        self._state = state

    @property
    def _intra_preamble_deferred(self):
        return self._state.get("_intra_preamble_deferred")

    @_intra_preamble_deferred.setter
    def _intra_preamble_deferred(self, value) -> None:
        self._state.set("_intra_preamble_deferred", value)

    def run_intra_safety(self) -> dict:
        """The FREE safety preamble, on its own schedule (board item 177).

        Fill reconcile, stop-out reconcile, protection-restore drain and
        repeg drain cost no model spend and protect live capital. Until
        2026-10-01 they existed ONLY as the opening block of
        ``_run_intra_check_body``, so they were welded to the *paid*
        intraday tick: cutting the paid cadence would silently have cut
        the loss-protection latency with it. That coupling was the defect
        item 177 names.

        The body is unchanged and lives in ``_run_intra_safety_preamble``.
        Both entry points call that one method, so this is strictly
        ADDITIVE: the paid tick still runs the preamble exactly as before,
        and the standalone ``intra_safety`` mode gives it a second,
        independent chance every tick. There is no new window in which
        protection is not restored — the only change is that one more
        caller can reach the same idempotent work.

        Both callers take the same advisory ``_intraday_scan_process_lock``
        and the same ``_blocking_owner_session`` check (board item 127), so
        two of them firing together cannot race: whichever acquires the
        lock does the work and the other defers, which is the behaviour the
        lock was built for.

        No LLM calls, and deliberately NO cost session — this path can
        never spend, and activating one would put empty rows into the very
        ``llm_budget_sessions`` measurement item 177 reads.
        """
        result = self._run_intra_safety_body()
        # Same P&L plumbing the paid tick and midday use: the figures come from
        # this run's own account read (or carry a reason code when none happened).
        if self._attach_pnl is not None:
            self._attach_pnl(result)
        return result

    def _read_account_for_pnl(self) -> None:
        """Read the account once so the P&L block has real figures (fail-soft)."""
        if self._state is not None:
            self._state.set("_last_account_snapshot", None)
        if self.broker is None or self._record_account_snapshot is None:
            return
        try:
            account = self.broker.get_account()
            total_value = account["portfolio_value"]
            self._record_account_snapshot(total_value, account.get("last_equity", total_value))
        except Exception as exc:  # noqa: BLE001 — a P&L read must never cost the safety pass
            logger.warning("Intra safety: account read for P&L failed (non-fatal): %s", exc)

    def _run_intra_safety_body(self) -> dict:
        ctx = RunContext.start("intra_safety")
        run_id = ctx.run_id
        logger.info("=== Intra safety preamble (free): %s ===", run_id)

        if not self._is_trading_day():
            logger.info("Intra safety skipped: market closed for non-trading day")
            return {"status": "market_holiday", "run_id": run_id}

        halt = self._kill_switch_halt_result(run_id)
        if halt is not None:
            return halt

        coverage_gaps, preamble_deferred = self._run_intra_safety_preamble(run_id)
        self._intra_preamble_deferred = preamble_deferred
        self._read_account_for_pnl()
        return {
            "status": "deferred" if preamble_deferred else "ok",
            "run_id": run_id,
            "stop_coverage_gaps": coverage_gaps,
            "preamble_deferred": preamble_deferred,
        }

    def _run_intra_safety_preamble(self, run_id: str) -> tuple[list[dict], str]:
        """Run the free broker-truth safety work; return (gaps, deferred_reason).

        Called by BOTH ``_run_intra_check_body`` (the paid tick) and
        ``run_intra_safety`` (the standalone free tick). Idempotent and
        fail-soft throughout; an empty deferred reason means it ran.
        """
        coverage_gaps: list[dict] = []
        preamble_deferred = ""
        with self._intraday_scan_process_lock() as preamble_lock:
            if not preamble_lock:
                preamble_deferred = "another desk process holds the broker-write lock"
            else:
                blocking = self._blocking_owner_session()
                if blocking == "unreadable":
                    preamble_deferred = "the active-session owner file could not be read (fail closed)"
                elif blocking is not None:
                    preamble_deferred = f"a live {blocking} session owns the desk and runs this same reconcile itself"
            if preamble_deferred:
                logger.warning(
                    "Intra check: broker-writing preamble DEFERRED this tick — "
                    "%s. No drain, repair, release or reconcile ran; the next "
                    "tick re-reads the broker.",
                    preamble_deferred,
                )
            else:
                # Drain orphaned protection-restore intents — intra runs every
                # 30 min so this is the most frequent recovery opportunity for
                # bails that landed during morning. Codex r8 #2.
                drained = self._drain_pending_protection_restores()
                self._drain_pending_repegs()
                # Broker-truth coverage audit + auto-repair every tick (audit round
                # 2): an entry that fills after place_entry_protection's wait, or a
                # repair that failed once, otherwise stayed naked until the NEXT
                # session — hours. On the intra cadence the naked window is ≤30 min.
                # Read-only when coverage is fine; ~1 broker call per held long.
                # Spec §11.1 guard 3: the return value used to be DISCARDED here, so
                # the 30-minute sweep — the tightest cadence this audit runs on, and
                # the one the fractional decision leans on — was the one caller whose
                # findings never reached the operator's feed at all. Carried into the
                # result dict now, exactly as every other session already does.
                try:
                    coverage_gaps = self._reconcile_stop_coverage()
                except Exception as exc:  # noqa: BLE001
                    _site(self, "coverage_reconcile", exc, log=logger)
                else:
                    _site(self, "coverage_reconcile", log=logger)
                self._reconcile_orphan_pending_submits()  # audit F4
                # 2026-09-17 AMD incident: AMD filled at $549.11 but the trades
                # table still read 'submitted' half an hour later. The stop-coverage
                # and stop-out reconcilers below only watch protective/broker-
                # initiated exits — neither one asks the broker about the fate of an
                # order THIS pipeline submitted (a BUY/SELL/REDUCE/etc still marked
                # 'submitted' in the trades table). `run_morning` and the midday/
                # close review both call `_reconcile_fills` for exactly that reason;
                # this tick — the one that runs every ~30 minutes and is therefore
                # the tightest window available to close that gap between sessions
                # — never did. The live fill-notification websocket never
                # authenticates on this host (placeholder credential, frozen pending
                # an owner decision — see broker.py), so in production this always
                # resolves through `_reconcile_fills`'s own bounded REST lookup
                # (`broker.get_order_fill_info`), never the socket. Unscoped
                # (no run_id) so a still-'submitted' row from ANY earlier session
                # today is picked up, not just ones this tick itself created.
                #
                # Item 173(2): this runs BEFORE the stop-out reconcile below,
                # not after. A SELL this pipeline submitted but hasn't yet
                # reconciled leaves the ledger believing the position is still
                # open (get_symbols_with_open_ledger_qty ignores 'submitted'
                # rows) while the broker has already reduced it — a positive
                # gap. The stop-out reconciler can't explain that gap either,
                # because the submitted SELL's broker_order_id is already in
                # get_known_broker_order_ids, so its fill is filtered out of
                # new_fills — and it pages a false CRITICAL "records disagree
                # with broker". Reconciling fills first flips that SELL to
                # executed, the gap closes, and the stop-out check stays quiet.
                try:
                    self._reconcile_fills()
                except Exception as exc:  # noqa: BLE001
                    _site(self, "fill_reconcile", exc, log=logger)
                else:
                    _site(self, "fill_reconcile", log=logger)
                # Broker-truth EXIT audit (2026-08-28 ONDS/CCJ). intra_check fires
                # every ~30 min, so this is the tightest window this reconciler
                # runs on — a stop that fires mid-session is written back within
                # one tick instead of sitting unrecorded until the next scheduled
                # session hours later.
                reco = None
                try:
                    reco = self._reconcile_stop_out_fills(run_id)
                except Exception as exc:  # noqa: BLE001
                    _site(self, "stop_out_reconcile", exc, log=logger)
                else:
                    _site(self, "stop_out_reconcile", log=logger)
                # Item 101: surface a broker-made stop-out / re-protection to
                # owner — intra is the tightest cadence, so this is where a
                # mid-session stop-out reaches him fastest.
                self._surface_reconcile_outcomes(reco, drained, run_id=run_id)

        return coverage_gaps, preamble_deferred
