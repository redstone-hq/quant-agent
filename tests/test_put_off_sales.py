"""Refused sales: a sale put off because its stop's cancel was not confirmed in
the pass's wait is owned by the run that decided it (spec of 14 Oct 2026).

The pass sends nothing; the run re-checks its own put-off sales in ONE seam
where every session run ends, bounded by that run's existing deadline; the
trend tick defers instead; nothing hands the sale to a later safety tick.
"""

from __future__ import annotations

import inspect
import pathlib
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from tests.test_settle_legs import STOP, _pipe, _pos, _sell

ROOT = pathlib.Path(__file__).resolve().parent.parent


class _Clock:
    """Epoch clock that moves only when the seam pauses for one wait."""

    def __init__(self, t: float = 1_000_000.0):
        self.t = t
        self.pauses = 0

    def now(self) -> float:
        return self.t

    def pause(self, seconds: float) -> None:
        self.pauses += 1
        self.t += max(seconds, 0.0)


def _statuses(pipe, calls, sequence):
    """The stop leg reads each status in turn (the last one repeats)."""
    seq = list(sequence)

    def _wait(order_id, **_kw):
        calls.append(("wait", order_id))
        return seq.pop(0) if len(seq) > 1 else seq[0]

    pipe.broker.wait_for_order_terminal.side_effect = _wait


def _seam(pipe, monkeypatch, *, seconds: float, clock=None):
    """Run one session through the shared seam with a run deadline `seconds` away."""
    from src.protection import put_off_sales

    clock = clock or _Clock()
    monkeypatch.setattr(put_off_sales, "_now", clock.now)
    monkeypatch.setattr(put_off_sales, "_pause", clock.pause)
    monkeypatch.setenv("SESSION_DEADLINE_EPOCH", str(clock.t + seconds))
    pipe._alert_owner_exit_declined = MagicMock()
    pipe._finalize_pending_protections = MagicMock()
    pipe.db.get_pending_protection_restores.return_value = [
        {"id": 7, "symbol": "NVDA", "sell_order_id": "__WAL_PENDING__", "side": "sell"}
    ]
    seen = {}

    def _session():
        seen["sale"] = _sell(pipe, 100.0)
        seen["refusal"] = pipe._last_stop_clear_refusal
        return {"status": "ok"}

    result = put_off_sales.run_owning_put_offs(pipe, _session, "no_window_session")
    return result, seen


def _submits(calls):
    return [c for c in calls if c[0] == "submit"]


# a. put off, then confirmed within the run: sold exactly once, at held ----


def test_put_off_then_confirmed_within_the_run_sells_once_at_held(monkeypatch):
    pipe, calls = _pipe()
    _statuses(pipe, calls, ["pending_cancel", "canceled"])
    result, seen = _seam(pipe, monkeypatch, seconds=120)
    # The pass itself sent nothing and told its caller to skip.
    assert seen["sale"] is None and seen["refusal"] == "cancel_unconfirmed"
    assert _submits(calls) == [("submit", 100.0)]
    pipe.db.insert_trade.assert_called_once()
    assert pipe.db.insert_trade.call_args.kwargs["action"] == "SELL"
    (prots,), _ = pipe._finalize_pending_protections.call_args
    assert prots[0]["wal_row_id"] == 7 and prots[0]["specs"] == [STOP]
    assert [o["outcome"] for o in result["put_off_sales"]] == ["sold"]


# b. put off, and the run's bound passes: no sale, alert names the stock ---


def test_put_off_past_the_runs_bound_sends_nothing_and_names_the_stock(monkeypatch):
    pipe, calls = _pipe()
    _statuses(pipe, calls, ["pending_cancel"])
    result, _seen = _seam(pipe, monkeypatch, seconds=40)
    assert not _submits(calls)
    pipe.db.insert_trade.assert_not_called()
    # The write-ahead restore row stays: the restore drain puts the stop back.
    pipe.db.delete_pending_protection_restore.assert_not_called()
    pipe.db.update_pending_protection_restore.assert_not_called()
    (alert,) = pipe._alert_owner_exit_declined.call_args_list
    assert alert.args[0] == "NVDA"
    assert "not sold" in alert.kwargs["why"] and "next no_window_session run" in alert.kwargs["why"]
    assert [o["outcome"] for o in result["put_off_sales"]] == ["not_sold"]


def test_no_known_deadline_means_no_wait_and_one_alert(monkeypatch):
    pipe, calls = _pipe()
    _statuses(pipe, calls, ["pending_cancel", "canceled"])
    from src.protection import put_off_sales

    monkeypatch.delenv("SESSION_DEADLINE_EPOCH", raising=False)
    pipe._alert_owner_exit_declined = MagicMock()
    put_off_sales.run_owning_put_offs(pipe, lambda: _sell(pipe, 100.0) and {}, "no_window_session")
    assert not _submits(calls)
    assert pipe._alert_owner_exit_declined.call_count == 1


# c. trend tick: put off is DEFERRED, retried next tick --------------------


def test_trend_tick_marks_a_put_off_sale_deferred_and_retries_it(monkeypatch):
    from src.intraday import trend_exit_tick as tick
    from src.protection import put_off_sales

    markers: dict = {}
    position = _pos(100.0)

    def _execute(firing, *, run_id, position_facts):
        pipe, calls = _pipe()
        _statuses(pipe, calls, ["pending_cancel"])
        pipe._alert_owner_exit_declined = MagicMock()
        assert _sell(pipe, 100.0) is None
        return []

    def _run():
        return tick.trend_exit_on_tick(
            positions=[position],
            run_id="intra-1",
            session_date="2026-10-14",
            completed_bar_date="2026-10-13",
            read_marker=markers.get,
            write_marker=lambda s, **kw: markers.__setitem__(s, kw),
            alignment_exit_cached=lambda *a, **k: SimpleNamespace(exit_cleared=True),
            position_facts_for=lambda due: {},
            execute_guarded_exits=_execute,
            open_orders_checked=lambda: (True, []),
            final_today=lambda positions: set(),
        )

    monkeypatch.setattr(tick, "_verdict", lambda *a, **k: SimpleNamespace(exit_cleared=True))
    summary = put_off_sales.run_owning_put_offs(MagicMock(), _run, "intra_check")
    assert markers["NVDA"]["outcome"] == tick.OUTCOME_DEFERRED
    assert summary["deferred"] == ["NVDA"] and not summary["refused"]
    assert tick._due(markers["NVDA"], "2026-10-14")  # retried next tick
    assert not summary.get("put_off_sales")  # the tick owns it, not the run's re-check


# d. an unconfirmed re-price (replace) that later fills: no second sale ----


def test_a_replaced_leg_that_later_fills_is_never_sold_twice(monkeypatch):
    pipe, calls = _pipe()
    _statuses(pipe, calls, ["replaced", "filled"])
    pipe.broker.client.get_order_by_id.return_value = SimpleNamespace(filled_qty="100")
    pipe.broker.get_positions.return_value = []  # the fill took the whole position
    result, seen = _seam(pipe, monkeypatch, seconds=120)
    assert seen["sale"] is None
    assert not _submits(calls)
    pipe.db.insert_trade.assert_not_called()
    assert [o["outcome"] for o in result["put_off_sales"]] == ["not_sold"]


# e. a REST hiccup after one good read is cancel_read_failed ---------------


def test_a_rest_hiccup_after_one_good_read_is_cancel_read_failed():
    from src.execution.broker_parts.cancel_confirm import settle_legs
    from src.execution.broker_parts.order_desk import OrderDesk

    reads = iter([SimpleNamespace(status="pending_cancel")])

    def _get(_order_id):
        try:
            return next(reads)
        except StopIteration:
            raise ConnectionError("REST hiccup") from None

    desk = SimpleNamespace(client=SimpleNamespace(get_order_by_id=_get))

    def _wait(order_id, timeout_seconds):
        return OrderDesk._wait_for_order_status_via_polling(
            desk, order_id, timeout_seconds, 0.0, stop_states=frozenset({"canceled"})
        )

    broker = SimpleNamespace(wait_for_order_terminal=_wait, client=desk.client)
    assert settle_legs(broker, [{"id": "stop-1"}]).reason == "cancel_read_failed"


# f. nothing references the owed sale; one seam for both run paths ---------


def test_nothing_references_the_retired_hand_off():
    banned = ("owed" + "_sale", "OWED" + "_SALE", "owed to the next " + "safety tick")
    hits = []
    for base in ("src", "tests", "main.py"):
        paths = [ROOT / base] if base.endswith(".py") else (ROOT / base).rglob("*.py")
        for path in paths:
            text = path.read_text(encoding="utf-8", errors="replace")
            hits += [f"{path.relative_to(ROOT)}: {b}" for b in banned if b in text]
    assert not hits


@pytest.mark.parametrize("module", ["src/scheduler.py", "main.py"])
def test_every_session_run_ends_in_the_one_seam(module):
    text = (ROOT / module).read_text(encoding="utf-8")
    assert text.count("run_owning_put_offs(") == 1


def test_the_seam_waits_only_on_the_one_ceiling():
    from src.protection import put_off_sales

    src = inspect.getsource(put_off_sales)
    assert "ORDER_TERMINAL_CEILING_S" in src and "SESSION_DEADLINE_ENV" in src and "SESSION_WINDOWS" in src
