"""The cost circuit has no clock of its own: every stamp it stores is bound from the desk clock.

Before this test the circuit kept a second clock -- its own replaceable
``_now_utc`` plus a connection wrapper that rewrote the literal ``'now'`` in
SQL text when a test froze time -- because the tables stamped rows with
``DEFAULT (datetime('now'))``. A live database keeps those old defaults, so
the only honest proof is to build the schema under the REAL clock (the way a
deployed database already stands), freeze the desk clock afterwards, drive
the real write paths, and demand that every time column still carries the
frozen instant. With a DEFAULT doing the stamping, the event row is dated by
the OS and this fails; with every writer binding the one clock, it passes.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from types import SimpleNamespace

from src import trading_calendar
from src.cost_circuit import LLMCostCircuitBreaker, ensure_cost_circuit_schema
from src.storage.db import Database

FROZEN = datetime(2026, 10, 13, 16, 0, 0, tzinfo=timezone.utc)
FROZEN_STAMP = "2026-10-13 16:00:00"
TABLES = ("llm_budget_days", "llm_budget_sessions", "llm_circuit_state", "llm_circuit_events", "llm_quota_holds")


def _config() -> SimpleNamespace:
    return SimpleNamespace(
        enabled=True,
        session_cost_limit_usd=10.0,
        daily_cost_limit_usd=20.0,
        max_calls_per_session=1000,
        max_provider_attempts_per_call=2,
        input_chars_per_token=3.5,
    )


def _time_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    return [r[1] for r in conn.execute(f"PRAGMA table_info({table})") if r[1].endswith("_at")]


def _stored_stamps(path: str) -> dict[str, set[str]]:
    """Every non-NULL value of every *_at column, keyed table.column."""
    stamps: dict[str, set[str]] = {}
    with sqlite3.connect(path) as conn:
        for table in TABLES:
            columns = _time_columns(conn, table)
            for row in conn.execute(f"SELECT {', '.join(columns)} FROM {table}"):
                for column, value in zip(columns, row):
                    if value is not None:
                        stamps.setdefault(f"{table}.{column}", set()).add(value)
    return stamps


def test_every_stored_time_column_is_the_frozen_desk_clock(monkeypatch, tmp_path):
    path = str(tmp_path / "circuit.db")
    # Schema built under the real clock, as every deployed database was.
    db = Database(path)
    db.initialize()
    db.conn.close()
    before_freeze = _stored_stamps(path)  # the singleton state row, dated by the real clock

    monkeypatch.setattr(trading_calendar, "utc_now", lambda: FROZEN)

    circuit = LLMCostCircuitBreaker(path, _config(), SimpleNamespace(send=lambda *a, **k: True))
    circuit.activate_session("run-frozen", "morning")
    reservation = circuit.begin_call(
        agent_name="tech_analyst", model="m", system_prompt="s", user_message="u", max_output_tokens=10
    )
    circuit.before_provider_attempt(reservation, model=reservation.model)
    circuit.complete_call(reservation, 0.01)  # a settlement
    over = circuit.begin_call(
        agent_name="tech_analyst", model="m", system_prompt="s", user_message="u", max_output_tokens=10
    )
    circuit.before_provider_attempt(over, model=over.model)
    circuit.complete_call(over, 50.0)  # over the session limit: a quota hold and an event row
    assert circuit.status()["suspended"] is True

    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM llm_circuit_events").fetchone()[0] >= 1
        assert conn.execute("SELECT COUNT(*) FROM llm_quota_holds").fetchone()[0] >= 1
    written = {column: values - before_freeze.get(column, set()) for column, values in _stored_stamps(path).items()}
    written = {column: values for column, values in written.items() if values}
    expected = {
        "llm_budget_days.seeded_at",
        "llm_budget_days.updated_at",
        "llm_budget_sessions.started_at",
        "llm_budget_sessions.updated_at",
        "llm_circuit_events.created_at",
        "llm_circuit_events.recovery_alert_updated_at",
        "llm_quota_holds.alert_updated_at",
        "llm_quota_holds.recovery_alert_updated_at",
        "llm_quota_holds.created_at",
    }
    assert expected <= set(written), sorted(expected - set(written))
    wrong = {column: values for column, values in written.items() if values != {FROZEN_STAMP}}
    assert not wrong, wrong


def test_fresh_schema_has_no_time_defaults(tmp_path):
    """A fresh table carries no clock of its own; every stamp must be bound by the writer."""
    with sqlite3.connect(str(tmp_path / "fresh.db")) as conn:
        conn.row_factory = sqlite3.Row
        ensure_cost_circuit_schema(conn)
        defaults = {
            f"{table}.{r[1]}": r[4]
            for table in TABLES
            for r in conn.execute(f"PRAGMA table_info({table})")
            if r[1].endswith("_at") and r[4] is not None
        }
    assert defaults == {}, defaults
