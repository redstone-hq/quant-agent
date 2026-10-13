"""The trend exit at the 30-minute tick, on DAILY closes only (owner ruling 2026-10-09 21:02 ET).

Pins: one pass per SESSION at the first in-session tick, on the prior close;
the 16:00 tick does no trend work; only deferred/error sales are retried
(a refusal is a decision);
the trail skips a name being sold; only the completed-bar date and the
completed-bar feed are read; the sale goes through the review's guarded path.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import date, datetime
from types import SimpleNamespace
from unittest.mock import MagicMock

from src.exits_parts.midday_state import SKIP
from src.intraday import trend_exit_tick
from src.intraday.tick_trail import trail_on_tick
from src.intraday.trend_exit_tick import OUTCOME_REFUSED, OUTCOME_SOLD, trend_exit_on_tick, wire_trend_exit
from src.trading_calendar import ET
from tests.pipeline_factory import build_pipeline
from tests.test_phase3_exit_rework import _position

EXIT = SimpleNamespace(exit_cleared=True, status="EXIT", reason="trend over")
HOLD = SimpleNamespace(exit_cleared=False, status="HOLD", reason="intact")


class _Desk:
    """In-memory marker store and recording fakes for `trend_exit_on_tick`."""

    def __init__(self, verdict=EXIT, sells=True):
        self.open_orders: tuple = (True, [])
        self.final: set = set()
        self.raises: Exception | None = None
        self.markers: dict = {}
        self.verdict = verdict
        self.sells = sells
        self.verdict_reads: list = []
        self.executed: list = []
        self.order_listings = 0

    def orders(self):
        self.order_listings += 1
        return self.open_orders

    def read(self, symbol):
        return self.markers.get(symbol)

    def write(self, symbol, *, bar_date, outcome, run_id=None):
        self.markers[symbol] = {"bar_date": bar_date, "outcome": outcome}

    def cached(self, **kw):
        self.verdict_reads.append(kw["symbol"])
        return self.verdict

    def execute(self, firing, *, run_id, position_facts):
        self.executed.append([p.symbol for p in firing])
        if self.raises is not None:
            raise self.raises
        return [{"symbol": p.symbol} for p in firing] if self.sells else []

    def tick(self, positions, session, run_id="r"):
        return trend_exit_on_tick(
            positions=positions,
            run_id=run_id,
            session_date=session,
            completed_bar_date="prior-close",
            read_marker=self.read,
            write_marker=self.write,
            alignment_exit_cached=self.cached,
            position_facts_for=lambda due: {},
            execute_guarded_exits=self.execute,
            open_orders_checked=self.orders,
            final_today=lambda positions: self.final & {p.symbol for p in positions},
        )


def test_fires_once_per_session_not_again_later_same_day():
    desk = _Desk()
    first = desk.tick([_position("AAA")], "2026-10-09", run_id="t0945")
    assert first["sold"] == ["AAA"]
    assert desk.markers["AAA"] == {"bar_date": "2026-10-09", "outcome": OUTCOME_SOLD}
    later = desk.tick([_position("AAA")], "2026-10-09", run_id="t1015")
    assert later["checked"] == [] and desk.verdict_reads == ["AAA"] and len(desk.executed) == 1
    desk.tick([_position("AAA")], "2026-10-12", run_id="next_day")
    assert desk.verdict_reads == ["AAA", "AAA"]


def test_hold_is_not_rechecked_in_the_same_session():
    desk = _Desk(verdict=HOLD)
    desk.tick([_position("AAA")], "2026-10-09")
    desk.tick([_position("AAA")], "2026-10-09")
    assert desk.verdict_reads == ["AAA"] and desk.executed == []


def test_refused_is_not_retried_but_deferred_and_error_are():
    """(c) A refusal is a decision; only a sale that failed or was put off is retried."""
    desk = _Desk(sells=False)
    first = desk.tick([_position("AAA")], "2026-10-09", run_id="t0930")
    assert first["refused"] == ["AAA"] and desk.markers["AAA"]["outcome"] == OUTCOME_REFUSED
    desk.sells = True
    second = desk.tick([_position("AAA")], "2026-10-09", run_id="t1000")
    assert second["checked"] == [] and desk.executed == [["AAA"]]
    for outcome in (trend_exit_tick.OUTCOME_DEFERRED, trend_exit_tick.OUTCOME_ERROR):
        desk.markers["BBB"] = {"bar_date": "2026-10-09", "outcome": outcome}
        assert desk.tick([_position("BBB")], "2026-10-09")["sold"] == ["BBB"], outcome


def test_tick_after_the_pass_with_nothing_retryable_lists_no_orders():
    """(d) Nothing retryable this session: no verdict read, no open-order listing call."""
    desk = _Desk()
    desk.tick([_position("AAA"), _position("BBB")], "2026-10-09", run_id="t0930")
    listings = desk.order_listings
    later = desk.tick([_position("AAA"), _position("BBB")], "2026-10-09", run_id="t1000")
    assert later["checked"] == [] and desk.order_listings == listings == 1


@contextmanager
def _lock():
    yield True


def test_trend_runs_before_trail_and_sold_name_is_not_trailed():
    calls: list = []

    def trend(positions, *, run_id, total_value=None):
        calls.append("trend")
        return {"status": "ran", "sold": ["AAA"], "refused": [], "checked": ["AAA", "BBB"], "no_trail": ["AAA"]}

    def trail(ready, *, run_id):
        calls.append(("trail", [p.symbol for p in ready]))
        return []

    summary = trail_on_tick(
        positions=[_position("AAA"), _position("BBB")],
        run_id="r",
        apply_deterministic_trails=trail,
        atr_for_symbol=lambda s: 2.0,
        process_lock=_lock,
        blocking_owner_session=lambda: None,
        trend_exit=trend,
    )
    assert calls == ["trend", ("trail", ["BBB"])]
    assert summary["trend_exit"]["sold"] == ["AAA"]


def _clock(monkeypatch, *args):
    monkeypatch.setattr("src.trading_calendar.et_now", lambda: datetime(*args, tzinfo=ET))


def _wired_pipeline(monkeypatch, completed=date(2026, 10, 9), patch_bar=True):
    p = build_pipeline(broker=MagicMock())
    p.broker.list_open_orders_checked.return_value = (True, [])
    p._record_alignment_reading = MagicMock()
    _clock(monkeypatch, 2026, 10, 12, 10, 0)  # Monday, in session
    if patch_bar:
        monkeypatch.setattr("src.trading_calendar.last_completed_bar_date", lambda *a, **k: completed)
    return p


def test_close_tick_does_no_pass_and_sends_nothing(monkeypatch):
    """(a) The 16:00 ET tick reads no verdict, lists no orders and sells nothing."""
    p = _wired_pipeline(monkeypatch, patch_bar=False)
    _clock(monkeypatch, 2026, 10, 8, 16, 0)
    p._alignment_exit_cached = MagicMock(return_value=EXIT)
    p._midday_execute_llm_actions = MagicMock(return_value=[{"symbol": "AAA"}])
    run = wire_trend_exit(lambda name: getattr(p, name, None))
    result = run([_position("AAA")], run_id="t1600", total_value=10_000.0)
    assert p._alignment_exit_cached.call_count == 0 and p._midday_execute_llm_actions.call_count == 0
    assert p.broker.list_open_orders_checked.call_count == 0 and result["sold"] == []
    from src.storage.trades import trend_tick_store

    assert trend_tick_store.get(p.db._trades(), "AAA") is None


def test_close_tick_does_not_use_up_next_mornings_pass(monkeypatch):
    """(b) After the 16:00 tick, the next session's first tick still runs the pass on the prior close."""
    p = _wired_pipeline(monkeypatch, patch_bar=False)
    p._alignment_exit_cached = MagicMock(return_value=HOLD)
    run = wire_trend_exit(lambda name: getattr(p, name, None))
    _clock(monkeypatch, 2026, 10, 8, 16, 0)
    run([_position("AAA")], run_id="t1600", total_value=10_000.0)
    _clock(monkeypatch, 2026, 10, 9, 9, 30)
    result = run([_position("AAA")], run_id="t0930", total_value=10_000.0)
    assert result["checked"] == ["AAA"] and result["bar_date"] == "2026-10-08"
    _clock(monkeypatch, 2026, 10, 9, 10, 0)
    assert run([_position("AAA")], run_id="t1000", total_value=10_000.0)["checked"] == []


def test_sale_goes_through_the_guarded_review_path(monkeypatch):
    p = _wired_pipeline(monkeypatch)
    p._alignment_exit_for_holding = MagicMock(return_value=EXIT)
    gates = MagicMock(return_value=SKIP)
    monkeypatch.setattr("src.pipeline_exits.midday_pre_gates", gates)
    run = wire_trend_exit(lambda name: getattr(p, name, None))
    result = run([_position("AAA")], run_id="tick-1", total_value=10_000.0)
    gates.assert_called_once()
    assert gates.call_args.args[2] == "SELL" and gates.call_args.args[3] == "AAA"
    # The gate refused it: no order, marked refused (not retried), and the chart was read once (memo).
    assert result["refused"] == ["AAA"] and p._alignment_exit_for_holding.call_count == 1
    assert p.broker.submit_order.call_count == 0
    from src.storage.trades import trend_tick_store

    assert trend_tick_store.get(p.db._trades(), "AAA")["outcome"] == OUTCOME_REFUSED


def test_only_the_completed_daily_bar_is_read(monkeypatch):
    p = _wired_pipeline(monkeypatch, completed=date(2026, 10, 9))
    p.market = MagicMock()
    p.market.get_ohlcv.return_value = []
    run = wire_trend_exit(lambda name: getattr(p, name, None))
    result = run([_position("AAA")], run_id="tick-1", total_value=10_000.0)
    assert result["bar_date"] == "2026-10-09"
    called = {c[0].split(".")[0] for c in p.market.method_calls}
    assert called <= {"get_ohlcv"}, called
    assert trend_exit_tick.OUTCOME_HOLD == "hold" and result["refused"] == [] and result["sold"] == []


def test_trend_failure_is_recorded_durably_and_trail_still_runs(monkeypatch):
    p = _wired_pipeline(monkeypatch)
    p._alignment_exit_cached = MagicMock(side_effect=RuntimeError("feed down"))
    recorded = MagicMock()
    monkeypatch.setattr(trend_exit_tick, "record_site", recorded)
    trailed: list = []
    summary = trail_on_tick(
        positions=[_position("AAA")],
        run_id="r",
        apply_deterministic_trails=lambda ready, run_id: trailed.extend(p.symbol for p in ready) or [],
        atr_for_symbol=lambda s: 2.0,
        process_lock=_lock,
        blocking_owner_session=lambda: None,
        trend_exit=wire_trend_exit(lambda name: getattr(p, name, None)),
    )
    assert summary["trend_exit"]["status"] == "error" and trailed == ["AAA"]
    recorded.assert_called_once()


def _trail_with(desk, positions, bar="2026-10-09"):
    trailed: list = []

    def trend(positions, *, run_id, total_value=None):
        try:
            return desk.tick(positions, bar, run_id=run_id)
        except trend_exit_tick.SellPathError as exc:
            return {"status": "error", "no_trail": exc.no_trail}

    summary = trail_on_tick(
        positions=positions,
        run_id="r",
        apply_deterministic_trails=lambda ready, run_id: trailed.extend(p.symbol for p in ready) or [],
        atr_for_symbol=lambda s: 2.0,
        process_lock=_lock,
        blocking_owner_session=lambda: None,
        trend_exit=trend,
    )
    return summary, trailed


def test_open_market_sell_means_no_second_sale_and_stop_still_trails():
    desk = _Desk()
    review_sale = SimpleNamespace(symbol="AAA", side=SimpleNamespace(value="sell"), order_type="market")
    resting_stop = SimpleNamespace(symbol="BBB", side="sell", order_type="stop")
    desk.open_orders = (True, [review_sale, resting_stop])
    summary, trailed = _trail_with(desk, [_position("AAA"), _position("BBB")])
    assert desk.executed == [["BBB"]], "a resting STOP does not defer; a working market sell does"
    assert summary["trend_exit"]["deferred"] == ["AAA"] and trailed == ["AAA"]
    assert desk.markers["AAA"]["outcome"] == trend_exit_tick.OUTCOME_DEFERRED
    desk.open_orders = (True, [])
    assert desk.tick([_position("AAA")], "2026-10-09")["sold"] == ["AAA"]


def test_unreadable_order_listing_defers_every_firing_holding():
    desk = _Desk()
    desk.open_orders = (False, [])
    assert desk.tick([_position("AAA")], "2026-10-09")["deferred"] == ["AAA"] and desk.executed == []


def test_sell_path_exception_marks_error_and_skips_trail_then_retries_after_order_check():
    desk = _Desk()
    desk.raises = RuntimeError("broker timeout after submit")
    summary, trailed = _trail_with(desk, [_position("AAA"), _position("BBB")])
    assert summary["trend_exit"]["no_trail"] == ["AAA", "BBB"] and trailed == []
    assert desk.markers["AAA"]["outcome"] == trend_exit_tick.OUTCOME_ERROR
    desk.raises = None
    desk.open_orders = (True, [SimpleNamespace(symbol="AAA", side="sell", order_type="market")])
    retry = desk.tick([_position("AAA"), _position("BBB")], "2026-10-09")
    assert retry["deferred"] == ["AAA"] and retry["sold"] == ["BBB"]


def test_wired_sell_path_exception_is_recorded_and_not_trailed(monkeypatch):
    p = _wired_pipeline(monkeypatch)
    p._alignment_exit_for_holding = MagicMock(return_value=EXIT)
    p._midday_execute_llm_actions = MagicMock(side_effect=RuntimeError("raised after submit"))
    recorded = MagicMock()
    monkeypatch.setattr(trend_exit_tick, "record_site", recorded)
    trailed: list = []
    summary = trail_on_tick(
        positions=[_position("AAA")],
        run_id="r",
        apply_deterministic_trails=lambda ready, run_id: trailed.extend(p.symbol for p in ready) or [],
        atr_for_symbol=lambda s: 2.0,
        process_lock=_lock,
        blocking_owner_session=lambda: None,
        trend_exit=wire_trend_exit(lambda name: getattr(p, name, None)),
    )
    assert summary["trend_exit"]["status"] == "error" and summary["trend_exit"]["no_trail"] == ["AAA"]
    assert trailed == [] and recorded.call_count == 1
    from src.storage.trades import trend_tick_store

    assert trend_tick_store.get(p.db._trades(), "AAA")["outcome"] == trend_exit_tick.OUTCOME_ERROR


def test_refusal_that_cannot_clear_today_is_final_for_the_session():
    desk = _Desk(sells=False)
    desk.final = {"AAA"}
    first = desk.tick([_position("AAA"), _position("BBB")], "2026-10-09")
    assert first["refused_final"] == ["AAA"] and first["refused"] == ["BBB"]
    desk.tick([_position("AAA"), _position("BBB")], "2026-10-09")
    assert desk.executed == [["AAA", "BBB"]]


def test_outside_session_does_nothing():
    desk = _Desk()
    result = desk.tick([_position("AAA")], None)
    assert result["status"] == "outside_session" and desk.verdict_reads == [] and desk.markers == {}


def test_an_order_being_cancelled_is_not_a_sale_in_flight():
    from types import SimpleNamespace as NS

    from src.intraday.trend_exit_tick import working_exit_symbols

    orders = [
        NS(symbol="AAPL", side="sell", order_type="market", status="pending_cancel"),
        NS(symbol="MSFT", side="sell", order_type="market", status="canceled"),
        NS(symbol="NVDA", side="sell", order_type="market", status="new"),
        NS(symbol="TSLA", side="sell", order_type="stop", status="new"),
    ]
    assert working_exit_symbols(orders) == {"NVDA"}


def test_wired_same_day_guard_checks_a_short_against_its_short_opening(monkeypatch):
    """A short opened today is final today: the guard must ask about its SHORT
    opening, not a BUY it never had (which would wrongly read 'not today')."""
    from types import SimpleNamespace

    from src import trading_calendar as cal
    from src.intraday import trend_exit_tick as tick

    asked = []

    def opened_today(symbol, *, is_short=False):
        asked.append((symbol, is_short))
        return is_short  # the short was opened today; there is no BUY today

    db = SimpleNamespace(_trades=lambda: None, get_trades=lambda **kw: [])
    parts = {
        "db": db,
        "broker": SimpleNamespace(list_open_orders_checked=lambda: []),
        "_alignment_exit_cached": lambda **kw: None,
        "_midday_execute_llm_actions": lambda *a, **kw: [],
        "_build_position_facts": lambda *a, **kw: {},
        "_symbols_already_trimmed_today": lambda: set(),
        "_position_opened_today": opened_today,
        "_build_review_metric_deltas": lambda *a, **kw: {},
    }
    captured = {}
    monkeypatch.setattr(cal, "in_regular_session", lambda now: True)
    monkeypatch.setattr(tick, "trend_exit_on_tick", lambda **kw: captured.update(kw) or {})

    run = tick.wire_trend_exit(parts.get)
    run([], run_id="r1")
    short = SimpleNamespace(symbol="XYZ", qty=-10)

    assert captured["final_today"]([short]) == {"XYZ"}
    assert asked == [("XYZ", True)]
