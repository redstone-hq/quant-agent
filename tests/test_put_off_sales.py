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


def _seam(pipe, monkeypatch, *, seconds: float, clock=None, rows=None):
    """Run one session through the shared seam with a run deadline `seconds` away."""
    from src.protection import put_off_sales

    clock = clock or _Clock()
    monkeypatch.setattr(put_off_sales, "_now", clock.now)
    monkeypatch.setattr(put_off_sales, "_pause", clock.pause)
    monkeypatch.setenv("SESSION_DEADLINE_EPOCH", str(clock.t + seconds))
    pipe._alert_owner_exit_declined = MagicMock()
    pipe._finalize_pending_protections = MagicMock()
    pipe.db.get_pending_protection_restores.return_value = (
        [{"id": 7, "symbol": "NVDA", "sell_order_id": "__WAL_PENDING__", "side": "sell"}] if rows is None else rows
    )
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
    seen: dict = {}

    def _execute(firing, *, run_id, position_facts):
        pipe, calls = _pipe()
        _statuses(pipe, calls, ["pending_cancel", "canceled"])
        pipe._alert_owner_exit_declined = MagicMock()
        assert _sell(pipe, 100.0) is None
        seen.update(pipe=pipe, calls=calls)
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
    clock = _Clock()
    monkeypatch.setattr(put_off_sales, "_now", clock.now)
    monkeypatch.setattr(put_off_sales, "_pause", clock.pause)
    monkeypatch.setenv("SESSION_DEADLINE_EPOCH", str(clock.t + 120))
    summary = put_off_sales.run_owning_put_offs(MagicMock(), _run, "no_window_session")
    assert markers["NVDA"]["outcome"] == tick.OUTCOME_DEFERRED
    assert summary["deferred"] == ["NVDA"] and not summary["refused"]
    assert tick._due(markers["NVDA"], "2026-10-14")  # retried next tick
    # The tick owns the sale: the run never sends it, but puts its stop back once the cancel confirms.
    assert _submits(seen["calls"]) == []
    assert [o["outcome"] for o in summary["put_off_sales"]] == ["put_back"]
    seen["pipe"].db.delete_pending_protection_restore.assert_called_once_with(7)


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
    assert "order_terminal_ceiling_s" in src and "SESSION_DEADLINE_ENV" in src and "SESSION_WINDOWS" in src


# g. the adversary's four defects on the first rebuild (14 Oct) ------------


def test_a_stop_put_back_before_the_recheck_does_not_lose_the_sale(monkeypatch):
    """The restore drain put the stop back first (its row is gone): the whole sale runs again, once."""
    pipe, calls = _pipe()
    _statuses(pipe, calls, ["pending_cancel", "canceled"])
    pipe.broker._restore_stop_orders.return_value = (1, [])  # already alive: the drain beat us
    result, _seen = _seam(pipe, monkeypatch, seconds=120, rows=[])  # the drain discharged the row
    assert _submits(calls) == [("submit", 100.0)]
    assert [o["outcome"] for o in result["put_off_sales"]] == ["sold"]
    # Cancelled again under a fresh write-ahead row, never sent under the old one.
    assert pipe.broker.cancel_snapshotted_stops.call_count == 2
    assert pipe.db.insert_pending_protection_restore.call_count == 2
    pipe.db.delete_pending_protection_restore.assert_any_call(7)


def _two_names(stuck_reads: list, quick_reads: list):
    """Two held names; AAA's stop never confirms, BBB's confirms on its second read."""
    pipe, calls = _pipe()
    specs = {
        "AAA": {"id": "stop-A", "qty": 100.0, "stop_price": 90.0},
        "BBB": {"id": "stop-B", "qty": 100.0, "stop_price": 90.0},
    }
    reads = {"stop-A": list(stuck_reads), "stop-B": list(quick_reads)}
    pipe.broker.snapshot_protective_stops.side_effect = lambda symbol, **_k: (True, [dict(specs[symbol])])

    def _wait(order_id, **_kw):
        calls.append(("wait", order_id))
        seq = reads[order_id]
        return seq.pop(0) if len(seq) > 1 else seq[0]

    def _submit(**kw):
        calls.append(("submit", kw["symbol"], kw["qty"]))
        return {"id": f"ord-{kw['symbol']}", "status": "accepted"}

    pipe.broker.wait_for_order_terminal.side_effect = _wait
    pipe.broker.submit_order.side_effect = _submit
    held = [_pos(100.0), _pos(100.0)]
    held[0].symbol, held[1].symbol = "AAA", "BBB"
    pipe.broker.get_positions.return_value = held
    return pipe, calls


def _sell_symbol(pipe, symbol, **kw):
    return pipe._submit_protected_sell(
        symbol=symbol, qty=100.0, limit_price=99.0, reference_price=100.0, position_qty_before_sell=100.0,
        label="SELL", **kw,
    )  # fmt: skip


def test_one_stuck_name_never_holds_up_another(monkeypatch):
    from src.protection import put_off_sales

    pipe, calls = _two_names(["pending_cancel"], ["pending_cancel", "canceled"])
    clock = _Clock()
    monkeypatch.setattr(put_off_sales, "_now", clock.now)
    monkeypatch.setattr(put_off_sales, "_pause", clock.pause)
    monkeypatch.setenv("SESSION_DEADLINE_EPOCH", str(clock.t + 120))
    pipe._alert_owner_exit_declined = MagicMock()
    pipe._finalize_pending_protections = MagicMock()

    def _session():
        assert _sell_symbol(pipe, "AAA") is None and _sell_symbol(pipe, "BBB") is None
        return {}

    result = put_off_sales.run_owning_put_offs(pipe, _session, "no_window_session")
    outcomes = {o["symbol"]: o["outcome"] for o in result["put_off_sales"]}
    assert outcomes == {"AAA": "not_sold", "BBB": "sold"}
    assert [c for c in calls if c[0] == "submit"] == [("submit", "BBB", 100.0)]
    # BBB went in the first round, not after AAA used up the run.
    assert clock.pauses >= 1 and calls.index(("submit", "BBB", 100.0)) < [c for c in calls].index(("wait", "stop-A"), 3)


def test_no_sale_starts_unless_both_of_its_waits_fit(monkeypatch):
    """25 s left: one leg wait fits, but not the sale's own wait plus its fill wait (2 x 15 s)."""
    pipe, calls = _pipe()
    _statuses(pipe, calls, ["pending_cancel", "canceled"])
    result, _seen = _seam(pipe, monkeypatch, seconds=25)
    assert not _submits(calls)
    assert [o["outcome"] for o in result["put_off_sales"]] == ["not_sold"]
    (alert,) = pipe._alert_owner_exit_declined.call_args_list
    assert "too little of the run" in alert.kwargs["why"]


def test_a_put_off_sale_is_recorded_by_its_callers_own_record(monkeypatch):
    """The decided sale's own record (decision id, event, trigger) is what the re-check writes."""
    from src.protection import put_off_sales

    pipe, calls = _pipe()
    _statuses(pipe, calls, ["pending_cancel", "canceled"])
    clock = _Clock()
    monkeypatch.setattr(put_off_sales, "_now", clock.now)
    monkeypatch.setattr(put_off_sales, "_pause", clock.pause)
    monkeypatch.setenv("SESSION_DEADLINE_EPOCH", str(clock.t + 120))
    pipe._alert_owner_exit_declined = MagicMock()
    pipe._finalize_pending_protections = MagicMock()
    book = MagicMock(side_effect=RuntimeError("ledger down"))

    def _session():
        assert _sell_symbol(pipe, "NVDA", book=book) is None
        return {}

    result = put_off_sales.run_owning_put_offs(pipe, _session, "no_window_session")
    book.assert_called_once()
    assert book.call_args.args[1] == 100.0
    pipe.db.insert_trade.assert_not_called()  # no thinner second row
    # The order is live: a failed record still finalizes its protection.
    pipe._finalize_pending_protections.assert_called_once()
    assert [o["outcome"] for o in result["put_off_sales"]] == ["sold"]


def test_forced_delever_moves_to_the_next_name_and_never_sells_both(monkeypatch):
    """A put-off name is re-decided on the spot: the next name covers the deficit; the re-check sells nothing."""
    from src.pipeline_context import RunContext
    from src.protection import put_off_sales
    from tests.pipeline_factory import build_pipeline

    p = build_pipeline()
    p.config = SimpleNamespace(risk=SimpleNamespace(allow_margin=False), cash_sweep=SimpleNamespace(enabled=False))
    p.cash_sweeper = None
    p.broker = MagicMock()
    p.broker.get_latest_quote.return_value = {"bid_price": 100.0, "ask_price": 100.2}
    p.db = MagicMock()
    p._finalize_pending_protections = MagicMock()
    p._alert_owner_force_delever_incomplete = MagicMock()
    seller = MagicMock()
    sent = []

    def _protected(**kw):
        if kw["symbol"] == "AAA":
            put_off_sales.put_off(
                put_off_sales.PutOff(sale=dict(kw, wal_row_id=9), legs=[], reason="cancel_unconfirmed", seller=seller)
            )
            return None
        sent.append(kw["symbol"])
        return {"id": "s-" + kw["symbol"], "status": "accepted"}, {"symbol": kw["symbol"], "submitted_qty": kw["qty"]}

    p._submit_protected_sell = MagicMock(side_effect=_protected)
    ctx = RunContext.start("morning")
    ctx.cash = -5_000.0
    ctx.positions = [_pos(100.0), _pos(100.0)]
    ctx.positions[0].symbol, ctx.positions[1].symbol = "AAA", "BBB"
    ctx.positions[0].unrealized_pnl = -10.0  # sold first

    clock = _Clock()
    monkeypatch.setattr(put_off_sales, "_now", clock.now)
    monkeypatch.setattr(put_off_sales, "_pause", clock.pause)
    monkeypatch.setenv("SESSION_DEADLINE_EPOCH", str(clock.t + 120))
    result = put_off_sales.run_owning_put_offs(p, lambda: {"orders": p._force_delever(ctx)}, "no_window_session")
    assert sent == ["BBB"]
    # Claimed: the re-check never sells AAA on top, but puts its stop back in this run.
    seller._submit_protected_sell.assert_not_called()
    seller._put_back.assert_called_once()
    seller._discharge_wal.assert_called_once_with("AAA", 9)
    assert [o["outcome"] for o in result["put_off_sales"]] == ["put_back"]
    p._alert_owner_force_delever_incomplete.assert_not_called()


# e. a stop that filled in part while its cancel was pending ----------------


def test_a_stop_that_sold_part_while_put_off_leaves_the_rest_sold(monkeypatch):
    """The dying stop sold 40 of 100 before its cancel confirmed: the re-check sells the 60 still held."""
    from src.protection.protected_sell import ProtectedSell

    pipe, calls = _pipe()
    _statuses(pipe, calls, ["pending_cancel", "canceled"])
    put_back = {"done": False}
    pipe.broker.client.get_order_by_id.side_effect = lambda *a, **k: SimpleNamespace(
        filled_qty="0" if put_back["done"] else "40"
    )
    original = ProtectedSell._put_back

    def _put_back(self, sale):
        put_back["done"] = True
        pipe.broker.get_positions.return_value = [_pos(60.0)]
        return original(self, sale)

    monkeypatch.setattr(ProtectedSell, "_put_back", _put_back)
    result, _seen = _seam(pipe, monkeypatch, seconds=120)
    assert _submits(calls) == [("submit", 60.0)]
    assert [o["outcome"] for o in result["put_off_sales"]] == ["sold"]


def _settled(*fills):
    from src.execution.broker_parts.cancel_confirm import LegResult, LegSettlement

    return LegSettlement(legs=tuple(LegResult(f"s{i}", "canceled", q, 0.0, side="sell") for i, q in enumerate(fills)))


@pytest.mark.parametrize(
    ("full_exit", "fills", "expected"),
    [
        (True, (40.0,), (60.0, 60.0, "")),  # a full exit sells what the stop left
        (False, (0.0,), (30.0, 100.0, "")),  # a trim keeps its own size
        (False, (40.0,), (0.0, 60.0, "stop_filled_in_part")),  # as the pass refuses it
        (True, (100.0,), (0.0, 0.0, "stop_fired")),  # nothing left to sell
    ],
)
def test_a_re_sale_is_sized_on_what_the_legs_left_held(full_exit, fills, expected):
    from src.pipeline_protection import _build_protected_sell
    from src.protection import protected_sell

    seller = _build_protected_sell(_pipe()[0])
    sale = {"position_qty_before_sell": 100.0, "qty": 100.0 if full_exit else 30.0, "side": "sell"}
    qty, held, refused = seller._resale_size({**sale, "full_exit": full_exit}, _settled(*fills))
    assert (qty, held) == expected[:2]
    assert refused == (protected_sell._OWN_REASONS[expected[2]] if expected[2] else "")


@pytest.mark.parametrize(("status", "ok"), [("accepted", True), ("flat", True), ("no_stop", False), ("unknown", False)])
def test_a_trims_put_back_counts_only_a_covered_book(monkeypatch, status, ok):
    """A kept leg that is gone or was not grown back is not put back: its write-ahead row stays."""
    from src.pipeline_protection import _build_protected_sell
    from src.protection import protected_sell

    seller = _build_protected_sell(_pipe()[0])
    monkeypatch.setattr(protected_sell, "trim_book_call", lambda _o, name, *a, **k: status)
    sale = {"symbol": "NVDA", "side": "sell", "stop_specs": [], "kept_leg": dict(STOP)}
    assert seller._put_back(sale) is ok


def test_a_claimed_sale_never_holds_the_run_open(monkeypatch):
    """Only claimed put-offs left and their legs never confirm: the run ends now;
    the restore drain puts the stop back."""
    from src.protection import put_off_sales

    pipe, calls = _pipe()
    _statuses(pipe, calls, ["pending_cancel"])
    pipe._alert_owner_exit_declined = MagicMock()
    clock = _Clock()
    monkeypatch.setattr(put_off_sales, "_now", clock.now)
    monkeypatch.setattr(put_off_sales, "_pause", clock.pause)
    monkeypatch.setenv("SESSION_DEADLINE_EPOCH", str(clock.t + 1200))

    def _session():
        assert _sell(pipe, 100.0) is None
        assert put_off_sales.claim_put_offs(["NVDA"]) == {"NVDA"}
        return {"status": "ok"}

    result = put_off_sales.run_owning_put_offs(pipe, _session, "no_window_session")
    assert clock.pauses == 0 and _submits(calls) == []
    assert [o["outcome"] for o in result["put_off_sales"]] == ["handed_back"]
    pipe._alert_owner_exit_declined.assert_not_called()
    pipe.db.delete_pending_protection_restore.assert_not_called()  # the row stays for the restore drain
