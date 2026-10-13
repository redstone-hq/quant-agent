"""src.intraday.gating -- the gates around the paid scan: cooldown memory, the owner-session lock,
the paid-scan slot, the single-scan process lock and snapshot-health tracking.

Bodies moved verbatim from src/pipeline_intraday.py (`IntradayMixin`), which keeps
same-named thin shims built per call. Every collaborator is an explicit keyword-only
constructor argument, so this builds and runs with no pipeline behind it. Host
attributes a body both reads and ASSIGNS go through `state` (a get/set view the shim
hands in), never a construction-time copy.
"""

import contextlib
import logging
from pathlib import Path

from src.sentinel.guarded_site import record_site

#: The moved code logged under `src.pipeline` before the move and still does;
#: binding the name rather than `__name__` keeps log records byte-identical.
logger = logging.getLogger("src.pipeline")


class IntradayGating:
    """The gates around the paid scan: cooldown memory, the owner-session lock, the paid-scan slot, the
    single-scan process lock and snapshot-health tracking. Standalone, built from explicit
    collaborators.
    """

    def __init__(
        self,
        *,
        db=None,
        config=None,
        blocking_owner_session=None,
        intra_window_remaining_s=None,
        state=None,
    ) -> None:
        self.db = db
        self.config = config
        if blocking_owner_session is not None:
            self._blocking_owner_session = blocking_owner_session  # else: this part's own body
        if intra_window_remaining_s is not None:
            self._intra_window_remaining_s = intra_window_remaining_s  # else: this part's own body
        self._state = state

    @property
    def _paid_scan_waited(self):
        return self._state.get("_paid_scan_waited")

    @_paid_scan_waited.setter
    def _paid_scan_waited(self, value) -> None:
        self._state.set("_paid_scan_waited", value)

    @property
    def _paid_scan_waited_for(self):
        return self._state.get("_paid_scan_waited_for")

    @_paid_scan_waited_for.setter
    def _paid_scan_waited_for(self, value) -> None:
        self._state.set("_paid_scan_waited_for", value)

    def _recently_intraday_evaluated(self, symbol: str, cooldown_hours: float) -> bool:
        """True when the explicit evaluation ledger says this symbol ran.

        Trades are not an evaluation ledger: PM parse failures, RM rejects,
        no-target decisions, and pre-execution errors create no trade row and
        previously bypassed cooldown, repeatedly buying the same analysis.
        """
        try:
            rows = self.db.get_recent_intraday_evaluations(
                symbol,
                cooldown_hours=cooldown_hours,
            )
        except Exception as e:  # noqa: BLE001
            record_site(self, "cooldown_ledger", e, context={"symbol": symbol}, log=logger)
            return True
        if isinstance(rows, list):
            return bool(rows)

        # Compatibility for lightweight test doubles and rolling upgrades in
        # which an older DB facade has not exposed the new ledger method yet.
        # Production Database always returns a real list above.
        try:
            legacy_rows = self.db.get_trades(symbol=symbol, limit=10)
        except Exception as exc:  # noqa: BLE001
            record_site(self, "cooldown_legacy_trades", exc, context={"symbol": symbol})
            return True
        from datetime import datetime as _dt, timedelta, timezone

        from src.trading_calendar import utc_now

        cutoff = utc_now() - timedelta(hours=cooldown_hours)
        for row in legacy_rows if isinstance(legacy_rows, list) else []:
            if not str(row.get("run_id") or "").startswith("intra_check-"):
                continue
            try:
                ts = str(row.get("timestamp") or "")
                when = (
                    _dt.fromisoformat(ts.replace("Z", "+00:00"))
                    if "T" in ts
                    else _dt.strptime(ts, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
                )
                if when.tzinfo is None:
                    when = when.replace(tzinfo=timezone.utc)
                if when >= cutoff:
                    return True
            except (TypeError, ValueError):
                continue
        return False

    def _blocking_owner_session(self) -> str | None:
        """Live wrapper owner mode that must not overlap paid discovery.

        Returns the other session's mode when it is alive, ``"unreadable"``
        when the owner file exists but cannot be trusted (fail closed), or
        None when paid discovery may run. ``intra_check`` never blocks
        itself. A dead pid or a vanished file is None — morning that
        already finished must not sleep the 09:30 scan until 10:00.
        """
        import os

        from src.trading_calendar import utc_now

        owner_path = Path.home() / ".cache" / "quant-agent" / "active-session.lock" / "owner"
        if not owner_path.exists():
            return None
        try:
            parts = owner_path.read_text().strip().split()
            owner_mode = parts[0]
            owner_ts = int(parts[2])
            owner_pid = int(parts[3])
            age = utc_now().timestamp() - owner_ts
            alive = True
            try:
                os.kill(owner_pid, 0)
            except OSError:
                alive = False
            if owner_mode == "intra_check":
                return None
            if alive and 0 <= age <= 1800:
                return owner_mode
            return None
        except (OSError, ValueError, IndexError):
            return "unreadable"

    def _intra_window_remaining_s(self) -> float:
        """Seconds left in the intra_check ET window. Calendar-bound, not invented."""
        from src.trading_calendar import SESSION_WINDOWS, _minute_of_day, et_now

        _start, end = SESSION_WINDOWS["intra_check"]
        now = et_now()
        remaining_min = end - _minute_of_day(now)
        return max(0.0, remaining_min * 60.0)

    def _await_paid_scan_slot(self, run_id: str) -> bool:
        """Wait for morning/midday/close to finish rather than skip the tick.

        Returns True when paid discovery must still be skipped (lock still
        held at window end, morning was the session waited on — see below —
        or the owner file unreadable). Returns False when the slot is free.

        Morning shares the 09:30 ``SESSION_WINDOWS`` start with this
        ``intra_check`` fire, so waiting for morning then running paid
        discovery on the SAME tick is still the 09:30 open, not a real
        INTRADAY look (item 121 — measured leftover at 09:37). Sets
        ``self._paid_scan_waited_for = "morning"`` in that case so the
        caller skips this tick instead of scanning; the first true paid
        INTRADAY look is the next existing half-hour fire, which sees
        morning's lock already released and runs immediately with no
        invented offset. Midday/close are a different cadence than this
        fire, so waiting for either and then scanning on release is still
        correct.
        """
        import time as _time

        first = True
        self._paid_scan_waited = False
        self._paid_scan_waited_for = None
        last_blocking = None
        while True:
            blocking = self._blocking_owner_session()
            if blocking is None:
                if not first:
                    if last_blocking == "morning":
                        self._paid_scan_waited_for = "morning"
                        logger.info(
                            "Intraday scan: morning released the owner lock; "
                            "this fire shares the 09:30 open with morning, "
                            "so paid discovery stays skipped this tick — "
                            "the next existing half-hour fire is the first "
                            "true INTRADAY look",
                        )
                        return True
                    self._paid_scan_waited = True
                    logger.info(
                        "Intraday scan: other session released the owner lock; "
                        "running paid discovery on this tick instead of "
                        "waiting for the next 30-minute fire",
                    )
                return False
            last_blocking = blocking
            if blocking == "unreadable":
                logger.warning(
                    "Intraday scan: could not validate active-session owner — skipping paid discovery fail-closed",
                )
                return True
            remaining = self._intra_window_remaining_s()
            if remaining <= 0:
                logger.info(
                    "Intraday scan: %s still holds the owner lock at window end; paid discovery cannot run this tick",
                    blocking,
                )
                return True
            if first:
                logger.info(
                    "Intraday scan: wrapper reports active %s session; waiting "
                    "for it to finish instead of skipping this tick",
                    blocking,
                )
                first = False
            _time.sleep(min(1.0, remaining))

    def _another_session_recently_active(self, run_id: str, within_minutes: float = 15.0) -> bool:
        """True when a DIFFERENT session currently owns the trading process.

        The 15-minute trade-row heuristic slept the 09:30 and 13:00 scans
        after morning/midday had already written fills — the owner lock is
        the in-flight signal. `within_minutes` is kept for callers but no
        longer gates a finished session.
        """
        blocking = self._blocking_owner_session()
        if blocking == "unreadable":
            return True
        return blocking is not None

    @contextlib.contextmanager
    def _intraday_scan_process_lock(self):
        """Non-blocking process-level mutex for the intraday scan.

        Yields True when this process holds the lock, False otherwise.

        Why (independent review finding, 2026-08-19): the owner-lock
        `_another_session_recently_active` guard sees a concurrent
        morning/midday/close only while that wrapper still owns the
        process. Two `intra_check` processes launched at nearly the same
        instant would both pass it — and could then size BUYs against the
        same pre-fill snapshot, breaching `max_position_pct`.

        In practice `scripts/run_if_et_window.sh` makes that impossible:
        ticks are 1800s apart and the wrapper hard-kills a run at
        `timeout --kill-after=30 1200` (~1230s), so a tick is always dead
        before the next fires. But that guarantee lives in a deployment
        config this code cannot read (the production systemd units are not
        in-repo), and it would silently disappear if the interval were ever
        shortened. A trading safety property should not depend on an
        unverifiable assumption, so this closes the class outright.

        Deliberately NOT a new service/daemon/timer — a plain advisory
        `flock` on a local file, the same idea as the wrapper's existing
        `mkdir`-based session lock. Since 2026-09-19 (board item 127) it
        also guards `intra_check`'s broker-writing preamble, and the
        standalone coverage sweep's repair pass takes the same file
        (`src.coverage_watchdog.repair_lock`). Loss protection keeps its
        exemption and never touches this. The lock is released on process exit even if we
        are SIGKILLed, so a killed run cannot wedge it.
        """
        import fcntl

        fh = None
        acquired = False
        try:
            lock_path = Path(self.config.storage.db_path).parent / ".intraday_scan.lock"
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            fh = open(lock_path, "w")
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
            except BlockingIOError:
                logger.info(
                    "Intraday scan: another process already holds the scan "
                    "lock — skipping this tick (no concurrent position sizing)",
                )
        except Exception as e:  # noqa: BLE001 — unknowable lock state must not scan
            record_site(self, "scan_lock", e, log=logger)
        try:
            # Keep the yield outside the acquisition exception handler.  An
            # exception raised by the protected scan body is injected here by
            # contextlib and must propagate to run_intra_check (not be mistaken
            # for a lock failure and replaced by "generator didn't stop after
            # throw()").
            yield acquired
        finally:
            if fh is not None:
                try:
                    fh.close()  # releases the flock
                except Exception as exc:  # noqa: BLE001
                    record_site(self, "scan_lock_release", exc)

    def _track_intraday_snapshot_ok(self, symbol: str) -> None:
        """Reset a symbol's consecutive-miss streak. Never raises — a
        monitoring bug must not be able to break the scan it watches."""
        try:
            self.db.record_intraday_symbol_snapshot_result(symbol, ok=True)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "intraday snapshot health: failed to record OK for %s",
                symbol,
                exc_info=True,
            )
            record_site(self, "snapshot_ok_record", exc, context={"symbol": symbol})

    def _track_intraday_snapshot_miss(self, symbol: str) -> None:
        """Record a missed snapshot for `symbol` and alert the owner once
        it has failed 3 consecutive ticks (~90 min) — see
        `Database.record_intraday_symbol_snapshot_result`'s docstring for
        the threshold/cooldown reasoning. Never raises."""
        try:
            result = self.db.record_intraday_symbol_snapshot_result(symbol, ok=False)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "intraday snapshot health: failed to record miss for %s",
                symbol,
                exc_info=True,
            )
            record_site(self, "snapshot_miss_record", exc, context={"symbol": symbol})
            return
        if not result.get("should_alert"):
            return
        try:
            from src import notifier as _notifier

            misses = result.get("consecutive_misses", 0)
            _notifier.send_owner_alert(
                "INTRADAY SNAPSHOT UNAVAILABLE\n"
                f"{symbol} has failed to return snapshot data for "
                f"{misses} consecutive scans (~{misses * 30} min). It is being "
                "silently excluded from intraday move detection until this "
                "resolves — check whether the ticker is still valid/tradable "
                "on Alpaca. Will not re-alert on this symbol for 24h.",
                category=_notifier.CATEGORY_OPERATIONAL,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "intraday snapshot health: alert failed for %s",
                symbol,
                exc_info=True,
            )
            record_site(self, "snapshot_miss_alert", exc, context={"symbol": symbol})
