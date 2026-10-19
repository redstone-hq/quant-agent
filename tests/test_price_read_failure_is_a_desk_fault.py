"""A FAILED price read is never "no price".

Owner ruling 2026-10-08: "if price can't be read, that's a fundamental
absolute deal breaker". The broker read is retried; when it still fails it
raises `PriceReadFailed`. On the entry path the failure is classified by one
read of the session's reference symbol: reference reads -> that one name is
refused (`price_read_failed`), others trade; reference fails too -> the feed
is down, no new entries for the rest of the session (`price_feed_unreadable`),
recorded durably. Absence stays a per-name `no_sizing_print`. Existing stops
are never touched by any of this.

Owner ruling, same day: "this is a swing trading desk, not scalping. The desk
can keep re-asking until a price is received. It should not be hammered
because that will get us banned." A name with no today print YET waits; ONE
batched request per ask covers every waiting name, on a rising backoff, until
it prints (then it is sized through the normal entry path) or the pass's own
slot ends (then it is skipped under `no_print_by_window_end`). The ask count
is bounded by the slot and logged.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import pytest

import src.sizing_refusal as sizing_refusal
from tests.test_broker_market_data import _broker, _price_client
from src.config.risk_adjuncts import CashSweepConfig
from src.execution import price_read
from src.execution.broker import LivePrice
from src.models import PortfolioDecision, ReasoningChain, TradeDecision
from src.pipeline_context import RunContext
from src.price_feed_preflight import (
    preflight_price_feed,
    price_feed_session_start,
    reference_symbol,
    wait_for_today_prints,
)
from src.refusal_errors import PriceReadFailed
from src.sizing_refusal import (
    NO_PRINT_BY_WINDOW_END,
    NO_SIZING_PRINT,
    PRICE_FEED_UNREADABLE,
    PRICE_READ_FAILED,
)
from src.stage_execution import ExecutionStage

ET = ZoneInfo("America/New_York")
REF = CashSweepConfig().symbol  # the session's reference symbol: the cash vehicle


@pytest.fixture(autouse=True)
def _instant_reads(monkeypatch):
    """Stubbed reads take no time, so the slot bound admits an ask whenever
    any slot is left; the test that measures the bound restores the real one."""
    import src.price_feed_preflight as preflight

    monkeypatch.setattr(preflight, "read_worst_case_s", lambda: 0.0)


def _print(price: float) -> LivePrice:
    return LivePrice(price=price, source="last_trade", trade_at=datetime.now(ET), is_today=True, is_today_print=True)


# --- the read layer: retry, then FAILURE is typed and ABSENCE stays None ----


def test_error_then_success_on_retry_returns_the_price(monkeypatch):
    waits: list[float] = []
    calls = iter([ConnectionError("blip"), _print(101.0)])

    def read_once(_symbol):
        step = next(calls)
        if isinstance(step, Exception):
            raise step
        return step

    got = price_read.read_price_with_retry(read_once, "NVDA", sleep=waits.append)
    assert got.price == 101.0
    assert len(waits) == 1 and waits[0] == price_read.BACKOFF_BASE_S


def test_persistent_error_raises_typed_failure_with_its_cause():
    def read_once(_symbol):
        raise TimeoutError("feed down")

    with pytest.raises(PriceReadFailed) as err:
        price_read.read_price_with_retry(read_once, "NVDA", sleep=lambda _s: None)
    assert isinstance(err.value.__cause__, TimeoutError)
    assert "NVDA" in str(err.value)


def test_absence_is_returned_without_a_retry():
    reads: list[str] = []

    def read_once(symbol):
        reads.append(symbol)
        return None

    assert price_read.read_price_with_retry(read_once, "NVDA", sleep=lambda _s: None) is None
    assert reads == ["NVDA"]


# --- the real entry stage: one name vs the desk -----------------------------


def _decision(symbol: str, action: str = "BUY") -> TradeDecision:
    long = action == "BUY"
    return TradeDecision(
        action=action,
        symbol=symbol,
        allocation_pct=10,
        entry_price=100.0,
        stop_loss=94.0 if long else 106.0,
        take_profit=118.0 if long else 88.0,
        reasoning="new name",
    )


def _pipeline(stamped_reader, positions=None):
    pipeline = MagicMock()
    pipeline.broker.list_open_orders_checked.return_value = (True, [])  # listing read, nothing open
    pipeline.broker.get_latest_price_stamped = stamped_reader
    pipeline.broker.get_latest_price = lambda s: stamped_reader(s).price
    pipeline.broker.get_session_open.return_value = datetime.now(ET) - timedelta(hours=1)  # arrival print is in-session
    pipeline.broker.get_latest_quote.return_value = {"bid_price": 99.0, "ask_price": 101.0}
    pipeline.broker.get_intraday_snapshots = lambda syms: {}
    pipeline.broker.submit_order.return_value = {"id": "o1", "status": "accepted"}
    pipeline._format_qty = lambda q: str(q)
    pipeline._order_accepted.return_value = True
    pipeline._refresh_account_state.return_value = (
        {"cash": 50_000.0, "portfolio_value": 100_000.0},
        positions or [],
        {},
    )
    return pipeline


def _run(pipeline, decisions, monkeypatch, positions=None, session="midday"):
    skips: list[tuple[str, str]] = []
    import src.stage_execution as stage_execution
    import src.price_feed_preflight as preflight

    def keep(_p, _c, sym, reason, *_a, **_k):
        skips.append((sym, reason))

    import src.stage_entry_preflight as entry_preflight

    monkeypatch.setattr(stage_execution, "_record_execution_skip", keep)
    monkeypatch.setattr(preflight, "_record_execution_skip", keep)
    monkeypatch.setattr(entry_preflight, "_record_execution_skip", keep)
    # No real sleeping in the suite; a test that measures the wait replaces these.
    if preflight._sleep is __import__("time").sleep:
        monkeypatch.setattr(preflight, "_sleep", lambda _s: None)
        monkeypatch.setattr(preflight, "slot_seconds_left", lambda _ctx: 60.0)
        monkeypatch.setattr(pipeline.broker, "read_latest_trade_prints", lambda _s: {}, raising=False)
    ctx = RunContext.start(session)
    ctx.cash, ctx.total_value, ctx.last_equity = 50_000.0, 100_000.0, 100_000.0
    ctx.positions, ctx.symbols_bars = positions or [], {}
    rc = ReasoningChain(
        macro_filter="x",
        news_check="x",
        earnings_check="x",
        signal_conflicts="x",
        sizing_logic="x",
        portfolio_balance="x",
        cash_target="x",
    )
    ctx.portfolio_decision = PortfolioDecision(reasoning_chain=rc, decisions=decisions, portfolio_view="t")
    ExecutionStage(pipeline=pipeline).run(ctx)
    return skips


def _failing_only(bad: set[str]):
    def reader(symbol):
        if symbol in bad:
            raise PriceReadFailed(f"{symbol}: price read failed on all attempts")
        return _print(100.0)

    return reader


def _submitted(pipeline) -> set[str]:
    return {c.kwargs.get("symbol") or c.args[0] for c in pipeline.broker.submit_order.call_args_list}


@pytest.mark.parametrize("action", ["BUY", "SHORT"])
def test_one_name_failing_while_the_reference_reads_refuses_only_that_name(action, monkeypatch):
    rows: list[dict] = []
    monkeypatch.setattr(sizing_refusal, "record_guarded_outcome", lambda **kw: rows.append(kw))
    pipeline = _pipeline(_failing_only({"BAD"}))
    skips = _run(pipeline, [_decision("GOOD", action), _decision("BAD", action)], monkeypatch)
    assert ("BAD", PRICE_READ_FAILED) in skips
    price_reasons = {PRICE_READ_FAILED, PRICE_FEED_UNREADABLE, NO_SIZING_PRINT}
    assert not [r for sym, r in skips if sym == "GOOD" and r in price_reasons]
    assert "BAD" not in _submitted(pipeline)
    if action == "BUY":  # a SHORT dies later on the MagicMock borrow gate, not on price
        assert "GOOD" in _submitted(pipeline)
    assert sizing_refusal.price_feed_fault(pipeline) is None
    # The failed name was counted durably through the house ledger helper.
    assert [r["where"] for r in rows] == ["price_feed.single_name"]
    assert isinstance(rows[0]["exc"], PriceReadFailed)


def test_reference_failing_too_halts_every_further_entry_this_session(monkeypatch):
    # The reference (the cash vehicle) reads at the preflight; then BAD fails
    # and the reference fails too when it is read to classify BAD.
    state = {"reads": 0, "ref_reads": 0}

    def reader(symbol):
        state["reads"] += 1
        state["ref_reads"] += symbol == REF
        if symbol == "BAD" or (symbol == REF and state["ref_reads"] > 1):
            raise PriceReadFailed(f"{symbol}: price read failed on all attempts")
        return _print(100.0)

    pipeline = _pipeline(reader)
    skips = _run(pipeline, [_decision("GOOD"), _decision("BAD"), _decision("LATER")], monkeypatch)
    assert ("BAD", PRICE_FEED_UNREADABLE) in skips
    assert ("LATER", PRICE_FEED_UNREADABLE) in skips
    assert pipeline.broker.submit_order.call_count <= 1  # GOOD may have gone before the fault
    fault = sizing_refusal.price_feed_fault(pipeline)
    assert fault and fault.startswith(PRICE_FEED_UNREADABLE)
    # Later entries in the SAME session are refused before any read happens.
    reads_before = state["reads"]
    price, why, detail = sizing_refusal.sizing_price_or_refusal(lambda _p, _s: 100.0, pipeline, "ANOTHER", "buy")
    assert (price, why) == (None, PRICE_FEED_UNREADABLE) and detail == fault
    assert state["reads"] == reads_before


def test_absence_is_a_per_name_skip_and_other_names_still_trade(monkeypatch):
    stale = LivePrice(price=100.0, source="quote_mid", trade_at=datetime.now(ET), is_today=True, is_today_print=False)

    def reader(symbol):
        return stale if symbol == "THIN" else _print(100.0)

    pipeline = _pipeline(reader)
    skips = _run(pipeline, [_decision("GOOD"), _decision("THIN")], monkeypatch)
    # The batched re-ask (stubbed to answer nothing) runs out the slot; the
    # name is then skipped with that reason, never as a read FAILURE.
    assert ("THIN", NO_PRINT_BY_WINDOW_END) in skips
    assert ("THIN", PRICE_READ_FAILED) not in skips
    assert "GOOD" in _submitted(pipeline)
    assert sizing_refusal.price_feed_fault(pipeline) is None


def test_reference_failing_at_preflight_places_no_entries_and_records_why(monkeypatch):
    # The reference is the cash vehicle; it fails at the preflight and NO
    # approved name is read for sizing afterwards.
    reads: list[str] = []

    def reader(symbol):
        reads.append(symbol)
        if symbol == REF:
            raise PriceReadFailed(f"{REF}: price read failed on all attempts")
        return _print(100.0)

    pipeline = _pipeline(reader)
    skips = _run(pipeline, [_decision("GOOD"), _decision("ALSO")], monkeypatch)
    assert ("GOOD", PRICE_FEED_UNREADABLE) in skips and ("ALSO", PRICE_FEED_UNREADABLE) in skips
    assert reads == [REF]
    pipeline.broker.submit_order.assert_not_called()
    fault = sizing_refusal.price_feed_fault(pipeline)
    assert fault and REF in fault and "preflight" in fault
    # Existing protection is untouched: nothing cancelled, replaced or moved.
    for name in ("cancel_order", "replace_order", "cancel_all_orders", "amend_stop"):
        getattr(pipeline.broker, name).assert_not_called()


def test_a_rejected_first_entry_with_nothing_held_skips_only_that_name(monkeypatch):
    """The old defect: with nothing held the FIRST approved entry was the
    reference, so one ticker Alpaca rejects halted every entry. Now only that
    ticker is skipped and the rest proceed."""
    pipeline = _pipeline(_failing_only({"BAD"}))
    skips = _run(pipeline, [_decision("BAD"), _decision("GOOD"), _decision("ALSO")], monkeypatch)
    assert ("BAD", PRICE_READ_FAILED) in skips
    assert not [r for sym, r in skips if r == PRICE_FEED_UNREADABLE]
    assert sizing_refusal.price_feed_fault(pipeline) is None
    assert {"GOOD", "ALSO"} <= _submitted(pipeline) and "BAD" not in _submitted(pipeline)


def test_reference_is_the_cash_vehicle_never_a_held_or_approved_name():
    pipeline = SimpleNamespace(
        broker=SimpleNamespace(get_latest_price_stamped=lambda s: None),
        price_feed_fault="price_feed_unreadable: yesterday",
        db=None,
    )
    ctx = SimpleNamespace(
        positions=[{"symbol": "HELD"}], portfolio_decision=SimpleNamespace(decisions=[_decision("NEW")])
    )
    assert price_feed_session_start(pipeline, ctx) is ctx
    assert pipeline.price_feed_fault is None
    assert pipeline.price_feed_reference == REF
    # An operator-configured vehicle is the one used.
    configured = SimpleNamespace(config=SimpleNamespace(cash_sweep=SimpleNamespace(symbol="BIL")))
    assert reference_symbol(configured) == "BIL"
    # Absence at the reference (None) is the feed answering: entries proceed.
    decisions = [_decision("NEW")]
    assert preflight_price_feed(pipeline, ctx, decisions) is decisions
    # A failing name is never classified against itself.
    assert sizing_refusal.reference_read_ok(pipeline, exclude=(REF,)) is None
    assert sizing_refusal.reference_read_ok(pipeline) is True


# --- no print YET: wait, re-ask in ONE batch, size when it prints ----------


def _quote_only() -> LivePrice:
    """A quote with no today print, stamped at call time (never import time)."""
    return LivePrice(price=100.0, source="quote_mid", trade_at=datetime.now(ET), is_today=True, is_today_print=False)


def _waiting_rig(monkeypatch, prints_on_ask: int | None, *, slot_s: float = 60.0):
    """A pipeline whose stamped read has no today print for THIN/ALSO until
    the batched re-ask number `prints_on_ask` answers (never, when None).
    Returns (pipeline, batch_calls, sleeps)."""
    import src.price_feed_preflight as preflight

    state = {"printed": set()}
    batch_calls: list[list[str]] = []
    sleeps: list[float] = []

    def stamped(symbol):
        return _print(100.0) if symbol in state["printed"] or symbol == "GOOD" else _quote_only()

    def batch(symbols):
        batch_calls.append(list(symbols))
        if prints_on_ask is not None and len(batch_calls) >= prints_on_ask:
            state["printed"].update(symbols)
            return {s: _print(100.0) for s in symbols}
        return {}

    pipeline = _pipeline(stamped)
    monkeypatch.setattr(pipeline.broker, "read_latest_trade_prints", batch, raising=False)
    monkeypatch.setattr(preflight, "_sleep", sleeps.append)
    monkeypatch.setattr(preflight, "slot_seconds_left", lambda _ctx: slot_s)
    return pipeline, batch_calls, sleeps


def test_a_waiting_name_that_prints_on_the_second_ask_is_sized_and_submitted(monkeypatch):
    pipeline, batch_calls, sleeps = _waiting_rig(monkeypatch, prints_on_ask=2)
    skips = _run(pipeline, [_decision("GOOD"), _decision("THIN")], monkeypatch, session="morning")
    assert {"GOOD", "THIN"} <= _submitted(pipeline)
    assert not [r for sym, r in skips if sym == "THIN"]
    assert batch_calls == [["THIN"], ["THIN"]]
    # First ask immediate, then the ledgered backoff base before the second.
    assert sleeps == [price_read.BACKOFF_BASE_S]


def test_backoff_rises_and_caps_at_the_ledgered_ceiling(monkeypatch):
    pipeline, batch_calls, sleeps = _waiting_rig(monkeypatch, prints_on_ask=5)
    _run(pipeline, [_decision("THIN")], monkeypatch, session="morning")
    assert len(batch_calls) == 5 and "THIN" in _submitted(pipeline)
    base, cap = price_read.BACKOFF_BASE_S, price_read.BACKOFF_MAX_S
    assert sleeps == [base, base * 2, min(base * 4, cap), min(base * 8, cap)]


def test_one_batched_request_per_ask_covers_every_waiting_name(monkeypatch):
    pipeline, batch_calls, _ = _waiting_rig(monkeypatch, prints_on_ask=1)
    _run(pipeline, [_decision("THIN"), _decision("ALSO")], monkeypatch, session="morning")
    assert batch_calls == [["THIN", "ALSO"]]  # one request, both names, not one per name
    assert {"THIN", "ALSO"} <= _submitted(pipeline)


def test_a_name_that_never_prints_is_skipped_at_the_slot_end_with_the_reason(monkeypatch):
    import math

    pipeline, batch_calls, sleeps = _waiting_rig(monkeypatch, prints_on_ask=None, slot_s=60.0)
    skips = _run(pipeline, [_decision("THIN")], monkeypatch, session="morning")
    assert ("THIN", NO_PRINT_BY_WINDOW_END) in skips
    assert ("THIN", NO_SIZING_PRINT) not in skips
    pipeline.broker.submit_order.assert_not_called()
    # Bounded: never more asks than the slot allows at the backoff ceiling,
    # and the programmed waits never exceed the slot.
    ceiling = math.ceil(60.0 / price_read.BACKOFF_MAX_S)
    assert 1 < len(batch_calls) <= 1 + ceiling  # one immediate, the rest on backoff
    assert sum(sleeps) <= 60.0
    assert max(sleeps) == price_read.BACKOFF_MAX_S


def test_a_zero_slot_asks_nothing_and_skips_with_the_reason(monkeypatch):
    pipeline, batch_calls, sleeps = _waiting_rig(monkeypatch, prints_on_ask=None, slot_s=0.0)
    skips = _run(pipeline, [_decision("THIN")], monkeypatch, session="close")
    assert ("THIN", NO_PRINT_BY_WINDOW_END) in skips
    assert batch_calls == [] and sleeps == []


def test_no_ask_starts_without_one_reads_worst_case_left_in_the_slot(monkeypatch):
    """The whole wait is bounded by the slot: an ask is admitted only while
    the slot still holds one read's worst case (every attempt to the HTTP
    timeout plus the backoff), so it can never run into the next pass."""
    import src.price_feed_preflight as preflight

    monkeypatch.setattr(preflight, "read_worst_case_s", price_read.read_worst_case_s)
    worst = price_read.read_worst_case_s()
    # Room for the immediate ask, not for the backoff wait plus another.
    pipeline, batch_calls, sleeps = _waiting_rig(
        monkeypatch, prints_on_ask=None, slot_s=worst + price_read.BACKOFF_BASE_S / 2
    )
    skips = _run(pipeline, [_decision("THIN")], monkeypatch, session="morning")
    assert batch_calls == [["THIN"]] and sleeps == []
    assert ("THIN", NO_PRINT_BY_WINDOW_END) in skips


def test_a_malformed_batch_answer_records_every_waiting_name_and_keeps_the_printed(monkeypatch):
    """A non-dict answer never drops a name silently: names that printed on an
    earlier ask are kept, and every name still waiting gets a skip row."""
    import src.price_feed_preflight as preflight

    answers = iter([{"EARLY": _print(100.0)}, ["not", "a", "map"]])
    broker = SimpleNamespace(read_latest_trade_prints=lambda _s: next(answers))
    monkeypatch.setattr(preflight, "_sleep", lambda _s: None)
    monkeypatch.setattr(preflight, "slot_seconds_left", lambda _ctx: 600.0)
    early, late = _decision("EARLY"), _decision("LATE")
    printed, skipped = wait_for_today_prints(SimpleNamespace(broker=broker), SimpleNamespace(), [early, late])
    assert printed == [early]
    assert [(d.symbol, r) for d, r, _x in skipped] == [("LATE", PRICE_READ_FAILED)]
    assert "not a price map" in skipped[0][2]


def _failing_batch_rig(monkeypatch, *, bad: set[str], feed_goes_down: bool):
    """A batched read that raises whenever it is asked for a name in `bad`
    (or always once the feed is down); the reference reads until then."""
    import src.price_feed_preflight as preflight

    state = {"down": False, "printed": {"GOOD"}}
    batch_calls: list[list[str]] = []

    def stamped(symbol):
        if state["down"]:
            raise PriceReadFailed(f"{symbol}: price read failed on all attempts")
        return _print(100.0) if symbol in state["printed"] else _quote_only()

    def batch(symbols):
        batch_calls.append(list(symbols))
        if feed_goes_down:
            state["down"] = True
        if state["down"] or bad & set(symbols):
            raise PriceReadFailed(f"{symbols}: price read failed on all attempts")
        state["printed"].update(symbols)
        return {s: _print(100.0) for s in symbols}

    pipeline = _pipeline(stamped)
    monkeypatch.setattr(pipeline.broker, "read_latest_trade_prints", batch, raising=False)
    monkeypatch.setattr(preflight, "_sleep", lambda _s: None)
    monkeypatch.setattr(preflight, "slot_seconds_left", lambda _ctx: 60.0)
    return pipeline, batch_calls


def test_one_bad_symbol_in_the_batch_skips_only_that_name(monkeypatch):
    """A failed batch is not the feed: the reference still reads, so each
    waiting name is re-asked alone and only the one that fails is skipped."""
    pipeline, batch_calls = _failing_batch_rig(monkeypatch, bad={"BAD"}, feed_goes_down=False)
    skips = _run(pipeline, [_decision("GOOD"), _decision("THIN"), _decision("BAD")], monkeypatch, session="morning")
    assert sizing_refusal.price_feed_fault(pipeline) is None
    assert ("BAD", PRICE_READ_FAILED) in skips
    assert not [r for sym, r in skips if sym in ("GOOD", "THIN")]
    assert {"GOOD", "THIN"} <= _submitted(pipeline) and "BAD" not in _submitted(pipeline)
    # One batch for both, then one name per ask -- never a burst.
    assert batch_calls[0] == ["THIN", "BAD"]
    assert all(len(c) == 1 for c in batch_calls[1:3])


def test_a_failed_batch_with_the_reference_also_failing_is_the_desk_fault(monkeypatch):
    rows: list[dict] = []
    monkeypatch.setattr(sizing_refusal, "record_guarded_outcome", lambda **kw: rows.append(kw))
    pipeline, _ = _failing_batch_rig(monkeypatch, bad=set(), feed_goes_down=True)
    skips = _run(pipeline, [_decision("GOOD"), _decision("THIN")], monkeypatch, session="morning")
    assert ("THIN", PRICE_FEED_UNREADABLE) in skips
    assert sizing_refusal.price_feed_fault(pipeline)
    # Declared by the reference classification after the failed batch.
    assert "price_feed.batched_reask" in [r["where"] for r in rows]
    pipeline.broker.submit_order.assert_not_called()  # no new entries this session


def test_slot_end_is_the_earliest_of_window_next_tick_and_session_deadline(monkeypatch):
    from datetime import timedelta
    from src.config.llm_cost import INTRA_CHECK_TICK_MINUTES
    from src.price_feed_preflight import POST_WAIT_RESERVE_S, slot_seconds_left
    from src.trading_calendar import SESSION_WINDOWS

    close_end = SESSION_WINDOWS["close"][1]
    at = datetime(2026, 10, 8, close_end // 60, close_end % 60, tzinfo=ET)
    mid = datetime(2026, 10, 8, 10, 0, tzinfo=ET)
    # A deadline far past every slot: the window and the tick still bind.
    monkeypatch.setenv("SESSION_DEADLINE_EPOCH", str(at.timestamp() + 86400))
    ten_before = at - timedelta(minutes=10)
    assert slot_seconds_left(SimpleNamespace(session="close"), now=ten_before) == 600.0
    tick_s = INTRA_CHECK_TICK_MINUTES * 60
    assert slot_seconds_left(SimpleNamespace(session="morning"), now=mid) == tick_s
    assert slot_seconds_left(SimpleNamespace(session="close"), now=at) == 0.0
    assert slot_seconds_left(SimpleNamespace(session="nonesuch"), now=mid) == 0.0
    # The wrapper's 1200s run ceiling, counted from now: the hard kill less
    # the measured post-wait time binds, well inside the 30-minute tick.
    deadline = mid.timestamp() + 1200
    monkeypatch.setenv("SESSION_DEADLINE_EPOCH", str(deadline))
    budget = slot_seconds_left(SimpleNamespace(session="morning"), now=mid)
    assert budget == 1200 - POST_WAIT_RESERVE_S
    assert mid.timestamp() + budget <= deadline - POST_WAIT_RESERVE_S


@pytest.mark.parametrize("raw", [None, "", "not-a-number", "nan", "inf"])
def test_no_readable_session_deadline_means_no_wait_at_all(monkeypatch, raw):
    """Fail CLOSED: a manual run or a test has no deadline, so the slot is zero
    -- never the old 30-minute or an unbounded wait."""
    from src.price_feed_preflight import slot_seconds_left

    if raw is None:
        monkeypatch.delenv("SESSION_DEADLINE_EPOCH", raising=False)
    else:
        monkeypatch.setenv("SESSION_DEADLINE_EPOCH", raw)
    mid = datetime(2026, 10, 8, 10, 0, tzinfo=ET)
    assert slot_seconds_left(SimpleNamespace(session="morning"), now=mid) == 0.0


@pytest.mark.parametrize("share_of_one_read", [None, 0.0, 0.5])
def test_a_near_or_absent_deadline_starts_no_ask_and_skips_every_name(monkeypatch, share_of_one_read):
    """Deadline (less the measured post-wait time) closer than one read's
    worst case, or absent: not one ask is started, and every waiting name is
    skipped with the slot-end reason rather than sized or left hanging."""
    import src.price_feed_preflight as preflight

    mid = datetime(2026, 10, 8, 10, 0, tzinfo=ET)
    if share_of_one_read is None:
        monkeypatch.delenv("SESSION_DEADLINE_EPOCH", raising=False)
    else:
        left_s = share_of_one_read * preflight.read_worst_case_s()
        monkeypatch.setenv("SESSION_DEADLINE_EPOCH", str(mid.timestamp() + preflight.POST_WAIT_RESERVE_S + left_s))
    monkeypatch.setattr(preflight, "et_now", lambda: mid)
    asks: list[list[str]] = []
    pipeline = SimpleNamespace(
        broker=SimpleNamespace(read_latest_trade_prints=lambda symbols: asks.append(list(symbols)) or {})
    )
    monkeypatch.setattr(preflight, "_sleep", lambda _s: pytest.fail("no wait may be spent"))
    printed, skipped = preflight.wait_for_today_prints(
        pipeline, SimpleNamespace(session="morning"), [_decision("THIN"), _decision("ALSO")]
    )
    assert asks == [] and printed == []
    assert sorted((d.symbol, reason) for d, reason, _detail in skipped) == [
        ("ALSO", NO_PRINT_BY_WINDOW_END),
        ("THIN", NO_PRINT_BY_WINDOW_END),
    ]


# --- no reference available: the desk-level row is still written ----------


def test_unclassifiable_failure_still_records_a_desk_level_row(monkeypatch):
    rows: list[dict] = []
    monkeypatch.setattr(sizing_refusal, "record_guarded_outcome", lambda **kw: rows.append(kw))

    def reader(_p, _s):
        raise sizing_refusal.SizingPriceUnavailable("NVDA: stamped read failed")

    price, why, _ = sizing_refusal.sizing_price_or_refusal(reader, None, "NVDA", "buy")
    assert (price, why) == (None, PRICE_READ_FAILED)
    assert [r["where"] for r in rows] == ["price_feed.desk_unclassified", "price_feed.single_name"]


# --- the real AlpacaBroker batched read ------------------------------------


def test_broker_batched_read_is_one_request_and_keeps_only_today_prints():
    from datetime import timedelta

    b = _broker()
    client = MagicMock()
    client.get_stock_latest_trade.return_value = {
        "AAA": SimpleNamespace(price=10.0, timestamp=datetime.now(ET)),
        "BBB": SimpleNamespace(price=20.0, timestamp=datetime.now(ET) - timedelta(days=3)),
        "CCC": SimpleNamespace(price=0, timestamp=None),
    }
    b._data_client = client
    got = b.read_latest_trade_prints(["AAA", "BBB", "CCC"])
    assert client.get_stock_latest_trade.call_count == 1
    assert set(got) == {"AAA"} and got["AAA"].is_today_print and got["AAA"].price == 10.0


def test_a_stub_broker_has_nothing_to_reask_so_absence_stands(monkeypatch):
    pipeline = _pipeline(lambda s: _quote_only())
    del pipeline.broker.read_latest_trade_prints  # this stub declares no batched reader
    assert not hasattr(pipeline.broker, "read_latest_trade_prints")
    import src.price_feed_preflight as preflight

    monkeypatch.setattr(preflight, "_sleep", lambda _s: None)
    monkeypatch.setattr(preflight, "slot_seconds_left", lambda _ctx: 60.0)
    skips = _run(pipeline, [_decision("THIN")], monkeypatch)
    assert ("THIN", NO_SIZING_PRINT) in skips


# --- the real AlpacaBroker read: retried, then typed ------------------------


def test_latest_price_raises_typed_failure_when_the_data_api_raises(monkeypatch):
    """A market-data outage is retried, then raised TYPED -- never "no price"."""
    from src.execution import price_read
    from src.refusal_errors import PriceReadFailed

    waits = []
    monkeypatch.setattr(price_read, "_sleep", waits.append)
    b = _broker()
    b._data_client = _price_client(raises=ConnectionError("data.alpaca.markets down"))
    with pytest.raises(PriceReadFailed) as err:
        b.get_latest_price("NVDA")
    assert isinstance(err.value.__cause__, ConnectionError)
    assert b._data_client.get_stock_latest_trade.call_count == 1 + price_read.MAX_RETRIES
    assert len(waits) == price_read.MAX_RETRIES


# --- the stop repair keeps its old worst case; unexpected errors are loud ----


def test_stop_repair_reads_once_per_its_own_attempt_never_nested_retries(monkeypatch):
    """The repair loop IS the retry (2 tries): each read inside it is single-
    attempt, so protecting naked shares is never slowed by a nested retry."""
    import src.execution.exit_path_records as exit_path_records
    from src.execution.stop_repair_price import read_repair_price

    monkeypatch.setattr(exit_path_records, "record_stop_repair_refusal", lambda *a, **k: None)
    calls: list[str] = []
    sleeps: list[float] = []

    def once(symbol):
        calls.append(symbol)
        raise ConnectionError("feed timed out")

    broker = SimpleNamespace(
        get_latest_price_stamped=lambda s: price_read.read_price_with_retry(once, s, sleep=sleeps.append),
        get_intraday_snapshots=lambda syms: {},
    )
    _stamped, price, error = read_repair_price(
        broker,
        "NAKED",
        stop_price=90.0,
        uncovered_qty=5,
        is_short=False,
        caller="t",
        db=None,
        outcome={},
        resting_stops=[],
        rec={"held_qty": 5, "covered_qty": 0},
        live_price_cls=LivePrice,
    )
    assert calls == ["NAKED", "NAKED"] and sleeps == []
    assert price is None and isinstance(error, PriceReadFailed)
    # Outside the repair the same read still retries per the ledgered policy.
    with pytest.raises(PriceReadFailed):
        price_read.read_price_with_retry(once, "X", sleep=sleeps.append)
    assert len(sleeps) == price_read.MAX_RETRIES


def test_an_unexpected_sizing_read_error_is_refused_and_recorded_loudly(monkeypatch):
    import src.pipeline_stages as stages

    rows: list[tuple] = []
    monkeypatch.setattr(stages, "record_swallowed", lambda p, where, exc, **ctx: rows.append((where, exc, ctx)))

    def broken(_symbol):
        raise TypeError("client bug")

    pipeline = SimpleNamespace(broker=SimpleNamespace(get_latest_price_stamped=broken))
    with pytest.raises(sizing_refusal.SizingPriceUnavailable):
        stages._today_sizing_price(pipeline, "NVDA")
    assert [(w, type(e)) for w, e, _c in rows] == [("sizing_price.stamped", TypeError)]
    # A typed read failure is the classified path: refused, not a swallow row.
    rows.clear()
    pipeline.broker.get_latest_price_stamped = _failing_only({"NVDA"})
    with pytest.raises(sizing_refusal.SizingPriceUnavailable):
        stages._today_sizing_price(pipeline, "NVDA")
    assert rows == []
