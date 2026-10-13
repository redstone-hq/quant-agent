"""src.cost_circuit.parts.episode_wording -- Episode coalescing and self-clear-window wording for the cost circuit.

Bodies moved verbatim from the former src/cost_circuit/breaker_wording.py shim (originally src/cost_circuit.py); held by LLMCostCircuitBreaker.
Every collaborator is an explicit keyword-only constructor argument.
"""

from __future__ import annotations
import sqlite3
from typing import Any, Callable, TypeVar
from src.cost_circuit.classification import _SELF_CLEARING_HARD_TRIGGERS
from src.cost_circuit.clock import _et_day_and_utc_bounds, _sql_stamp


class EpisodeWording:
    def __init__(self, *, config) -> None:
        self.config = config

    def _self_clear_window_minutes(self) -> float:
        """The circuit's OWN self-clear timing, not a number chosen here.

        `_auto_clear_transient_latch_locked` will not retire a transient
        latch until `transient_latch_cooldown_minutes` of wall clock have
        passed. That is already the desk's ratified answer to "how long
        before this stops being a blip", so the durability threshold for
        PAGING the owner is the same quantity read from the same config
        field. Change one and both move together, which is the point.
        """
        return float(getattr(self.config, "transient_latch_cooldown_minutes", 15.0))

    def _suspension_still_inside_self_clear_window_locked(
        self,
        conn: sqlite3.Connection,
        state: dict[str, Any],
    ) -> bool:
        """True while this latch could still retire itself without a human.

        Only for `_SELF_CLEARING_HARD_TRIGGERS`. Every other code needs an
        operator reset, so there is no window it can expire inside and
        deferring its alert would only delay a page that has to happen.
        An unreadable or future `suspended_at` returns False — err towards
        telling the owner.
        """
        if str(state.get("trigger_code") or "") not in _SELF_CLEARING_HARD_TRIGGERS:
            return False
        suspended_at = state.get("suspended_at")
        if not suspended_at:
            return False
        row = conn.execute(
            "SELECT (julianday(?) - julianday(?)) * 1440.0 AS minutes",
            (_sql_stamp(), suspended_at),
        ).fetchone()
        elapsed = row["minutes"] if row is not None else None
        if elapsed is None:
            return False
        return 0.0 <= float(elapsed) < self._self_clear_window_minutes()

    def _episode_already_paged_locked(
        self,
        conn: sqlite3.Connection,
        state: dict[str, Any],
        *,
        before: str | None = None,
    ) -> bool:
        """True when the owner has ALREADY been paged for this episode.

        Item 211. The first coalescing attempt keyed suppression to the
        self-clear window, which is a DURATION and the wrong quantity: on
        production data every one of the 22 suspension episodes between 26
        and 30 Sep outlasted that window, so none of the 44 messages was
        held [measured, production `llm_circuit_events`].

        The quantity that must be coalesced is the EPISODE: one underlying
        fault latching and clearing over and over. No new duration is
        invented here. The boundary is read off what the record already
        carries -- the identity of the triggering fault (`trigger_code`)
        and the ET budget day, which is exactly the boundary
        `_episode_facts_locked` reports on and the unit the auto-clear
        allowance is already counted against. A re-latch of the same
        trigger inside the same budget day is the SAME unresolved fault,
        so the owner is told once when it starts, not once per flap.
        """
        code = str(state.get("trigger_code") or "")
        if not code:
            return False
        _, utc_start, utc_end = _et_day_and_utc_bounds()
        # `suspension_alert_state` is captured on the AUTO_RESET row, at the
        # last moment the answer to "did the SUSPENDED note actually reach
        # him" is still knowable (see `_auto_clear_transient_latch_locked`).
        # So an earlier latch of this fault that the owner really received
        # is exactly: an auto_reset today, same trigger, state 1. The latch
        # live right now has not cleared, so it cannot match its own row.
        sql = (
            "SELECT 1 FROM llm_circuit_events WHERE event_type='auto_reset' "
            "AND trigger_code=? AND suspension_alert_state=1 "
            "AND created_at BETWEEN ? AND ?"
        )
        params: list[Any] = [code, utc_start, utc_end]
        if before:
            sql += " AND created_at < ?"
            params.append(before)
        row = conn.execute(sql + " LIMIT 1", params).fetchone()
        return row is not None

    def _record_suspension_deferral_locked(
        self,
        conn: sqlite3.Connection,
        state: dict[str, Any],
        *,
        episode_paged: bool = False,
    ) -> None:
        """Write the deferral down once per latch, so it is never invisible.

        Once per latch, not once per authorization boundary: a suspended
        desk can hit this path many times a minute and a row each time is
        the same noise moved into the database.
        """
        suspended_at = state.get("suspended_at")
        window = self._self_clear_window_minutes()
        detail = (
            "owner alert held: the same trigger already paged him in "
            "this ET budget day and that episode is not yet resolved; "
            "a re-latch of one unresolved fault is not a second "
            "incident. Suspension is in force and recorded either way"
            if episode_paged
            else f"owner alert held: latch is inside its own {window:.0f}-minute "
            "self-clear window and may expire without a human; it pages "
            "if it is still suspended after that. Suspension is in force "
            "and recorded either way"
        )
        # Once per latch per REASON. A latch can be held first because it
        # is still inside its own window and then because the episode it
        # belongs to has already paged; those are different statements
        # about the desk and the record must carry both. Repeats of the
        # SAME statement are the noise this dedupe exists to stop.
        existing = conn.execute(
            "SELECT 1 FROM llm_circuit_events WHERE event_type='suspend_alert_deferred' "
            "AND created_at >= ? AND detail = ? LIMIT 1",
            (suspended_at, detail),
        ).fetchone()
        if existing is not None:
            return
        conn.execute(
            "INSERT INTO llm_circuit_events "
            "(event_type, trigger_code, detail, run_id, mode, agent_name, attempts, "
            "session_cost_usd, daily_cost_usd) VALUES "
            "('suspend_alert_deferred', ?, ?, ?, ?, 'episode_coalescing', ?, ?, ?)",
            (
                state.get("trigger_code"),
                detail,
                state.get("run_id"),
                state.get("mode"),
                int(state.get("session_attempts") or 0),
                float(state.get("session_cost_usd") or 0.0),
                float(state.get("daily_cost_usd") or 0.0),
            ),
        )

    def _episode_facts_locked(
        self,
        conn: sqlite3.Connection,
        state: dict[str, Any],
    ) -> dict[str, Any]:
        """How long this fault has been running today and how often it flapped.

        An EPISODE is every suspension and self-clear of one trigger code on
        one ET budget day. That boundary is not invented here: the auto-clear
        allowance (`max_transient_latch_auto_clears_per_day`) is already
        counted per ET day against this same table, so the day is the unit
        the circuit already reasons about a recurring fault in.
        """
        code = str(state.get("trigger_code") or "")
        if not code:
            return {}
        _, utc_start, utc_end = _et_day_and_utc_bounds()
        row = conn.execute(
            "SELECT MIN(created_at) AS first_at, COUNT(*) AS events, "
            "SUM(CASE WHEN event_type='auto_reset' THEN 1 ELSE 0 END) AS clears "
            "FROM llm_circuit_events WHERE trigger_code=? "
            "AND created_at BETWEEN ? AND ?",
            (code, utc_start, utc_end),
        ).fetchone()
        if row is None or not row["first_at"]:
            return {}
        elapsed = conn.execute(
            "SELECT (julianday(?) - julianday(?)) * 1440.0 AS minutes",
            (_sql_stamp(), row["first_at"]),
        ).fetchone()
        return {
            "episode_first_at": str(row["first_at"]),
            "episode_self_clears": int(row["clears"] or 0),
            "episode_events": int(row["events"] or 0),
            "episode_minutes": (float(elapsed["minutes"]) if elapsed and elapsed["minutes"] is not None else None),
            "episode_window_minutes": self._self_clear_window_minutes(),
        }

    @staticmethod
    def _format_episode_line(facts: dict[str, Any]) -> str:
        """One line the owner can read without opening anything."""
        if not facts.get("episode_first_at"):
            return ""
        minutes = facts.get("episode_minutes")
        clears = int(facts.get("episode_self_clears") or 0)
        window = float(facts.get("episode_window_minutes") or 0.0)
        duration = f"{float(minutes):.0f} min" if minutes is not None else "unknown"
        return (
            f"episode: running {duration} since {facts['episode_first_at']} UTC, "
            f"{clears} self-clear{'s' if clears != 1 else ''} inside it today; "
            f"reported now because it outlasted the circuit's own "
            f"{window:.0f}-minute self-clear window (shorter blips are recorded, "
            "not sent)\n"
        )

    @staticmethod
    def _format_episode_summary(facts: dict[str, Any]) -> str:
        """The closing line of an episode: how long, and how often it flapped."""
        if not facts.get("episode_first_at"):
            return ""
        minutes = facts.get("episode_minutes")
        clears = int(facts.get("episode_self_clears") or 0)
        duration = f"{float(minutes):.0f} min" if minutes is not None else "an unknown time"
        return (
            f"episode: this fault ran {duration} from "
            f"{facts['episode_first_at']} UTC and self-cleared {clears} "
            f"time{'s' if clears != 1 else ''} inside it today; the ones that "
            "cleared inside the self-clear window were recorded, not sent\n"
        )
