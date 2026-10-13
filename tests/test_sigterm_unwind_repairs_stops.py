"""A kill between the buys and their stops must not leave a buy naked.

Execution sends every buy first and places stops in a second loop; the run
wrapper SIGTERMs a slow session and SIGKILLs it 30s later. The SIGTERM unwind
now runs the broker-truth stop-coverage repair BEFORE it settles anything, in
every session that can buy (morning and the intraday scan).
"""

from __future__ import annotations

import logging
import signal
from unittest.mock import MagicMock, patch

import pytest

from src.sessions.termination import SessionTerminated
from src.intraday.session import IntradaySession
from tests.pipeline_factory import build_pipeline


def _morning_pipeline(calls, *, repair_fails_on_kill=False):
    p = build_pipeline()
    p._is_trading_day = lambda: True
    p._kill_switch_halt_result = lambda run_id: None
    p._activate_cost_session = lambda run_id, mode: None
    p._install_sigterm_unwind = lambda context: None
    p._restore_sigterm = lambda prior: calls.append("restore")

    def reconcile():
        calls.append("session-start reconcile")
        return []

    def add_missing():
        calls.append("kill repair")
        if repair_fails_on_kill:
            raise RuntimeError("broker 503")
        return []

    def killed(run_id):
        raise SessionTerminated("morning: SIGTERM from the run wrapper")

    p._reconcile_stop_coverage = reconcile
    p._add_missing_stops = add_missing
    p._release_retired_cash_park = killed
    p._discharge_deferred_gross_ceiling = lambda ctx: calls.append("discharge")
    p._reconcile_fills = lambda ctx: calls.append("settle")
    p._sync_positions_from_broker = lambda: calls.append("sync")
    return p


def test_morning_kill_repairs_stops_before_any_settle_step():
    calls: list[str] = []
    p = _morning_pipeline(calls)
    with pytest.raises(SessionTerminated):
        p.run_morning()
    # Session-start repair, then the kill's repair, THEN the settle steps.
    assert calls == [
        "session-start reconcile",
        "kill repair",
        "discharge",
        "settle",
        "sync",
        "restore",
    ]


def test_a_failed_repair_is_loud_and_the_unwind_still_runs(caplog):
    calls: list[str] = []
    p = _morning_pipeline(calls, repair_fails_on_kill=True)
    with caplog.at_level(logging.ERROR, logger="src.pipeline"), pytest.raises(SessionTerminated):
        p.run_morning()
    assert calls[2:] == ["discharge", "settle", "sync", "restore"]
    failed = [r for r in caplog.records if "repair FAILED" in r.getMessage()]
    assert failed and failed[0].exc_info is not None, "failure must carry its traceback"


class _AddOnlyBroker(MagicMock):
    """Fails the test on ANY cancel or replace: the kill path must only add."""

    def __getattr__(self, name):
        if "cancel" in name or "replace" in name:
            raise AssertionError(f"kill path touched broker.{name}")
        return super().__getattr__(name)


def _coverage_pipeline(resting):
    broker = _AddOnlyBroker()
    broker.get_positions.return_value = [MagicMock(symbol="AAA", qty=12.0)]
    broker.snapshot_protective_stops.return_value = (True, resting)
    broker.get_latest_price.return_value = 100.0
    broker._submit_protective_stop_retrying.return_value = {"id": "s-1"}
    db = MagicMock()
    db.get_pending_protection_restores.return_value = []
    db.get_symbols_with_open_ledger_qty.return_value = {"AAA": 12.0}
    db.get_symbol_last_buy.return_value = {"stop_loss": 90.0}
    p = build_pipeline(broker=broker, db=db)
    p.broker, p.db = broker, db
    p.cash_sweeper = None
    return p


@pytest.mark.parametrize(
    ("resting", "placed"),
    [
        ([{"qty": 12.0, "stop_price": 90.0}], 0),  # a stop rests: never a second
        ([], 1),  # naked: covered once
    ],
)
def test_the_kill_repair_reads_the_broker_and_never_duplicates(resting, placed):
    p = _coverage_pipeline(resting)
    owed_levels = MagicMock(side_effect=AssertionError("owed-level replace ran"))
    with (
        patch("time.sleep"),
        patch("src.pipeline_protection._market_is_open_now", return_value=True),
        patch("src.pipeline_protection.drain_owed_stop_levels", owed_levels),
        patch("src.execution.pending_stop_drain.drain_safely", owed_levels),
    ):
        p._repair_stops_on_kill("test")
    assert p.broker._submit_protective_stop_retrying.call_count == placed
    assert not owed_levels.called


def test_the_intraday_scan_installs_the_unwind_and_repairs_on_a_real_sigterm():
    calls: list[str] = []
    p = build_pipeline()
    p._add_missing_stops = lambda: calls.append("repair") or []

    def body():
        calls.append("body")
        signal.raise_signal(signal.SIGTERM)
        calls.append("survived")  # pragma: no cover — the handler raises

    p._run_intra_check_body = body
    before = signal.getsignal(signal.SIGTERM)
    with pytest.raises(SessionTerminated):
        p.run_intra_check()
    assert calls == ["body", "repair"]
    assert signal.getsignal(signal.SIGTERM) == before, "prior handler restored"


def test_the_intraday_session_itself_installs_repairs_and_restores():
    calls: list[str] = []

    def body():
        calls.append("body")
        raise SessionTerminated("intra_check: SIGTERM from the run wrapper")

    session = IntradaySession(
        run_intra_check_body=body,
        install_sigterm_unwind=lambda ctx: calls.append(f"install:{ctx}") or "prior",
        repair_stops_on_kill=lambda ctx: calls.append(f"repair:{ctx}"),
        restore_sigterm=lambda prior: calls.append(f"restore:{prior}"),
        state=MagicMock(),
    )
    with pytest.raises(SessionTerminated):
        session.run_intra_check()
    assert calls == [
        "install:intra_check",
        "body",
        "repair:intra_check",
        "restore:prior",
    ]
