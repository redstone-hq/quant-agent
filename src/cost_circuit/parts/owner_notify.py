"""src.cost_circuit.parts.owner_notify -- Owner notification scans for the cost circuit (suspend / quota / recovery / resume).

Bodies moved verbatim from the former src/cost_circuit/breaker_notify.py (now held by LLMCostCircuitBreaker) (originally src/cost_circuit.py).
Every collaborator is an explicit keyword-only constructor argument.
"""

from __future__ import annotations
import logging
from typing import Any, Callable, TypeVar

from src.cost_circuit.alert_outcome import _alert_state_value, _send_alert_outcome
from src.cost_circuit.clock import _sql_stamp

logger = logging.getLogger(__name__)


class OwnerNotify:
    def __init__(
        self,
        *,
        enabled,
        infrastructure_lock,
        read_unavailable_sentinel,
        connect,
        refresh_latched_snapshot_locked,
        state_row,
        notifier,
        episode_already_paged_locked,
        suspension_still_inside_self_clear_window_locked,
        record_suspension_deferral_locked,
        episode_facts_locked,
        format_alert,
        format_quota_alert,
        format_recovery_alert,
        format_auto_reset_alert,
        notify_quota_holds_if_needed=None,
        notify_quota_recoveries_if_needed=None,
        notify_auto_resets_if_needed=None,
    ) -> None:
        self.enabled = enabled
        self._infrastructure_lock = infrastructure_lock
        self._read_unavailable_sentinel = read_unavailable_sentinel
        self._connect = connect
        self._refresh_latched_snapshot_locked = refresh_latched_snapshot_locked
        self._state_row = state_row
        self.notifier = notifier
        self._episode_already_paged_locked = episode_already_paged_locked
        self._suspension_still_inside_self_clear_window_locked = suspension_still_inside_self_clear_window_locked
        self._record_suspension_deferral_locked = record_suspension_deferral_locked
        self._episode_facts_locked = episode_facts_locked
        self.format_alert = format_alert
        self.format_quota_alert = format_quota_alert
        self.format_recovery_alert = format_recovery_alert
        self.format_auto_reset_alert = format_auto_reset_alert
        # The three scans below are themselves moved bodies; a caller passes them only
        # when they were swapped on the host (see the shim), otherwise this object's own run.
        if notify_quota_holds_if_needed is not None:
            self._notify_quota_holds_if_needed = notify_quota_holds_if_needed
        if notify_quota_recoveries_if_needed is not None:
            self._notify_quota_recoveries_if_needed = notify_quota_recoveries_if_needed
        if notify_auto_resets_if_needed is not None:
            self._notify_auto_resets_if_needed = notify_auto_resets_if_needed

    @property
    def _unavailable_sentinel(self):
        # Read live: the sentinel is installed on the host after construction.
        return self._read_unavailable_sentinel()

    def _notify_if_needed(self) -> None:
        if not self.enabled:
            return
        with self._infrastructure_lock:
            sentinel = self._unavailable_sentinel
        if sentinel is not None:
            sentinel._alert()
            return
        claimed = False
        state: dict[str, Any] = {}
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._refresh_latched_snapshot_locked(conn)
            state = self._state_row(conn)
            if int(state.get("suspended") or 0):
                episode_paged = self._episode_already_paged_locked(conn, state)
                if episode_paged or (self._suspension_still_inside_self_clear_window_locked(conn, state)):
                    # === docs/WORK.md item 208 ===
                    # Do not page yet. A latch of a self-clearing code that
                    # is still inside its OWN self-clear window has not yet
                    # been shown to be anything but a provider blip, and
                    # `_auto_clear_transient_latch_locked` may retire it
                    # without a human. Paging here and again on the clear
                    # turned one flapping fault into 44 of the 107 messages
                    # the owner received between 26 and 29 Sep [measured,
                    # production `notifier_sends`], and he muted the desk.
                    #
                    # This defers; it never cancels. `alert_state` stays 0,
                    # so the very next authorization boundary after the
                    # window expires claims and sends it, and the existing
                    # item-174 pairing reads that same 0 to suppress the
                    # matching "RESUMED" note — so a blip that self-clears
                    # inside its window is ONE recorded episode and ZERO
                    # messages, not two. Nothing is dropped: the trip event,
                    # the deferral event below and the CRITICAL log line all
                    # remain.
                    self._record_suspension_deferral_locked(
                        conn,
                        state,
                        episode_paged=episode_paged,
                    )
                else:
                    cur = conn.execute(
                        "UPDATE llm_circuit_state SET alert_state=-1, updated_at=? "
                        "WHERE singleton=1 AND suspended=1 AND (alert_state=0 OR "
                        "(alert_state=-1 AND updated_at <= ?))",
                        (_sql_stamp(), _sql_stamp(minutes_ago=2)),
                    )
                    claimed = cur.rowcount == 1
                    if claimed:
                        state = dict(state)
                        state.update(self._episode_facts_locked(conn, state))
            conn.commit()
        if claimed:
            text = self.format_alert(state)
            # A Telegram outage must not hide the shutdown from local operators.
            # The DB lease remains retryable when send() returns false.
            logger.critical("\n%s", text)
            delivered, suppressed = _send_alert_outcome(
                self.notifier,
                text,
                "cost circuit Telegram alert failed",
            )
            with self._connect() as conn:
                conn.execute(
                    "UPDATE llm_circuit_state SET alert_state=?, updated_at=? WHERE singleton=1 AND alert_state=-1",
                    (_alert_state_value(delivered, suppressed), _sql_stamp()),
                )
                conn.commit()
        self._notify_quota_holds_if_needed()
        self._notify_quota_recoveries_if_needed()
        self._notify_auto_resets_if_needed()

    def _notify_quota_holds_if_needed(self) -> None:
        while True:
            hold: dict[str, Any] | None = None
            with self._connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute(
                    "SELECT * FROM llm_quota_holds WHERE "
                    "(alert_state=0 OR (alert_state=-1 AND "
                    "alert_updated_at <= ?)) "
                    "ORDER BY id LIMIT 1",
                    (_sql_stamp(minutes_ago=2),),
                ).fetchone()
                if row is not None:
                    claimed = (
                        conn.execute(
                            "UPDATE llm_quota_holds SET alert_state=-1, "
                            "alert_updated_at=? "
                            "WHERE id=? AND (alert_state=0 OR alert_state=-1)",
                            (_sql_stamp(), row["id"]),
                        ).rowcount
                        == 1
                    )
                    if claimed:
                        hold = dict(row)
                conn.commit()
            if hold is None:
                return
            message = self.format_quota_alert(hold)
            logger.critical("\n%s", message)
            delivered, suppressed = _send_alert_outcome(
                self.notifier,
                message,
                "cost quota Telegram alert failed",
            )
            sent = delivered or suppressed  # a drop is settled, not retried
            with self._connect() as conn:
                conn.execute(
                    "UPDATE llm_quota_holds SET alert_state=?, alert_updated_at=? WHERE id=? AND alert_state=-1",
                    (_alert_state_value(delivered, suppressed), _sql_stamp(), hold["id"]),
                )
                conn.commit()
            if not sent:
                return

    def _notify_quota_recoveries_if_needed(self) -> None:
        while True:
            hold: dict[str, Any] | None = None
            with self._connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute(
                    "SELECT * FROM llm_quota_holds WHERE active=0 AND alert_state=1 AND "
                    "(recovery_alert_state=0 OR (recovery_alert_state=-1 AND "
                    "recovery_alert_updated_at <= ?)) "
                    "ORDER BY id LIMIT 1",
                    (_sql_stamp(minutes_ago=2),),
                ).fetchone()
                if row is not None:
                    claimed = (
                        conn.execute(
                            "UPDATE llm_quota_holds SET recovery_alert_state=-1, "
                            "recovery_alert_updated_at=? "
                            "WHERE id=? AND active=0 AND alert_state=1 AND "
                            "(recovery_alert_state=0 OR "
                            "recovery_alert_state=-1)",
                            (_sql_stamp(), row["id"]),
                        ).rowcount
                        == 1
                    )
                    if claimed:
                        hold = dict(row)
                conn.commit()
            if hold is None:
                return
            message = self.format_recovery_alert(hold)
            logger.info("\n%s", message)
            delivered, suppressed = _send_alert_outcome(
                self.notifier,
                message,
                "cost quota recovery Telegram alert failed",
            )
            sent = delivered or suppressed  # a drop is settled, not retried
            with self._connect() as conn:
                conn.execute(
                    "UPDATE llm_quota_holds SET recovery_alert_state=?, "
                    "recovery_alert_updated_at=? "
                    "WHERE id=? AND recovery_alert_state=-1",
                    (_alert_state_value(delivered, suppressed), _sql_stamp(), hold["id"]),
                )
                conn.commit()
            if not sent:
                return

    def _notify_auto_resets_if_needed(self) -> None:
        """Tell the owner, on the same Telegram surface as the suspension, that
        a transient latch expired on its own and the desk is trading again.

        Item 174: the suspension path reaches the owner (`_notify_if_needed`
        / the sentinel's `_alert`), but `_auto_clear_transient_latch_locked`
        used to write only an `auto_reset` DB event and a log line, so the
        owner saw "desk suspended" and never "desk back live." The
        operator's manual `reset` had the identical hole and is carried by
        this same scan ('reset' rows alongside 'auto_reset'), so a desk
        restarted by hand announces itself on the same footing as one that
        recovered on its own. This mirrors
        `_notify_quota_recoveries_if_needed` exactly -- same claim/retry state
        machine, same notifier -- so a crash between the auto-clear and the
        send, or a Telegram outage, leaves the alert pending for the next
        authorization boundary instead of dropping it silently. The DB event
        and the log line the auto-clear already writes are untouched.
        """

        while True:
            event: dict[str, Any] | None = None
            with self._connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute(
                    "SELECT * FROM llm_circuit_events "
                    "WHERE event_type IN ('auto_reset','reset') "
                    "AND (recovery_alert_state=0 OR (recovery_alert_state=-1 AND "
                    "recovery_alert_updated_at <= ?)) "
                    "ORDER BY id LIMIT 1",
                    (_sql_stamp(minutes_ago=2),),
                ).fetchone()
                if row is not None:
                    claimed = (
                        conn.execute(
                            "UPDATE llm_circuit_events SET recovery_alert_state=-1, "
                            "recovery_alert_updated_at=? "
                            "WHERE id=? AND (recovery_alert_state=0 OR "
                            "recovery_alert_state=-1)",
                            (_sql_stamp(), row["id"]),
                        ).rowcount
                        == 1
                    )
                    if claimed:
                        event = dict(row)
                conn.commit()
            if event is None:
                return
            # Item 174 pairing. A resume note is only meaningful as the answer
            # to a suspension note the owner received. When the suspension
            # alert never reached him (send failed at latch time), the
            # auto-clear has already made that alert undeliverable, so firing
            # "RESUMED" would report a recovery from an incident he was never
            # told about. From his side nothing happened; the honest output is
            # neither note. Resolve the row as unpaired (2) so it is not
            # retried forever, and keep the DB event and the log line, which
            # is where an operator reads the full history.
            with self._connect() as conn:
                episode_paged = int(
                    event.get("suspension_alert_state") or 0
                ) == 1 or self._episode_already_paged_locked(
                    conn,
                    event,
                    before=str(event.get("created_at") or ""),
                )
                live = self._state_row(conn)
            # Item 211. A clear in the MIDDLE of a live episode is not the
            # end of it: the same trigger is latched again right now, so
            # "RESUMED" would be a false statement about the desk's state.
            # Resolve it unpaired (2) so it is not retried forever; the
            # clear that genuinely ends the episode still pages.
            if int(live.get("suspended") or 0) and str(live.get("trigger_code") or "") == str(
                event.get("trigger_code") or ""
            ):
                logger.info(
                    "cost-circuit auto-reset %s: holding the owner resume "
                    "alert because the same trigger is latched again — the "
                    "episode is still running, not over",
                    event.get("id"),
                )
                with self._connect() as conn:
                    conn.execute(
                        "UPDATE llm_circuit_events SET recovery_alert_state=2, "
                        "recovery_alert_updated_at=? "
                        "WHERE id=? AND recovery_alert_state=-1",
                        (_sql_stamp(), event["id"]),
                    )
                    conn.commit()
                continue
            if not episode_paged:
                logger.info(
                    "cost-circuit auto-reset %s: suppressing the owner resume "
                    "alert because the matching suspension alert never "
                    "reached him (%s)",
                    event.get("id"),
                    event.get("detail"),
                )
                with self._connect() as conn:
                    conn.execute(
                        "UPDATE llm_circuit_events SET recovery_alert_state=2, "
                        "recovery_alert_updated_at=? "
                        "WHERE id=? AND recovery_alert_state=-1",
                        (_sql_stamp(), event["id"]),
                    )
                    conn.commit()
                continue
            # item 208: the resume note is the END of an episode, so it
            # carries how long the episode ran and how often the same fault
            # flapped inside it. Read at send time from the same table the
            # auto-clear allowance is already counted against.
            with self._connect() as conn:
                event.update(self._episode_facts_locked(conn, event))
            message = self.format_auto_reset_alert(event)
            logger.info("\n%s", message)
            delivered, suppressed = _send_alert_outcome(
                self.notifier,
                message,
                "cost-circuit auto-reset Telegram alert failed",
            )
            sent = delivered or suppressed  # a drop is settled, not retried
            with self._connect() as conn:
                conn.execute(
                    "UPDATE llm_circuit_events SET recovery_alert_state=?, "
                    "recovery_alert_updated_at=? "
                    "WHERE id=? AND recovery_alert_state=-1",
                    (_alert_state_value(delivered, suppressed), _sql_stamp(), event["id"]),
                )
                conn.commit()
            if not sent:
                return
