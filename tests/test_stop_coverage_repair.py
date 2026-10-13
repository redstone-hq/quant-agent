"""Naked-position stop repair — longs AND shorts.

The BUY-attached OTO stop inherited the parent's DAY tif and was expired by
the broker at 16:00 ET, so positions bought in the morning sat unprotected
overnight. The primary fix places a GTC stop post-fill; this reconciler is the
belt that (a) repairs anything the old bug left naked and (b) covers a crash
between an entry fill and the stop placement.

Repair uses the stop level RECORDED ON THE LAST OPENING ROW — BUY for a long,
SHORT for a short — the reviewed intent, not an invented one. A long's
sell-stop at/above the live price, or a short's buy-stop at/below it, would
fire instantly and turn a janitor into an exit decision, so those are refused.

A test that only checks "the gap is flagged" is not enough: a BUY-only lookup
or a sell-only place path can still leave a short naked while the long tests
stay green. The short cases below require the SHORT row's stop and side="buy".
"""

from unittest.mock import MagicMock, patch

import pytest

from src.storage.db import Database
from tests.pipeline_factory import build_pipeline


def _pipeline(
    held_qty=31.0,
    covered=0.0,
    buy_stop=158.75,
    price=165.0,
    *,
    symbol="VST",
    last_buy=None,
):
    p = build_pipeline(broker=MagicMock(), db=MagicMock())
    p.broker.get_positions.return_value = [
        MagicMock(symbol=symbol, qty=held_qty),
    ]
    p.broker.snapshot_protective_stops.return_value = (
        True,
        ([{"qty": covered, "stop_price": 158.0}] if covered else []),
    )
    p.broker.get_latest_price.return_value = price
    p.db.get_pending_protection_restores.return_value = []
    if last_buy is not None:
        p.db.get_symbol_last_buy.side_effect = last_buy
    else:
        p.db.get_symbol_last_buy.return_value = {"stop_loss": buy_stop}
    p.cash_sweeper = None
    return p


def _opening_lookup(**stops_by_action):
    """Return a last-open callable that only answers the actions it was given.

    A BUY-only half-fix fails the short tests because `action='SHORT'` returns
    None. A SHORT-only table cannot accidentally feed the long path.
    """

    def _lookup(symbol, include_in_flight=False, action="BUY"):
        stop = stops_by_action.get(action)
        if stop is None:
            return None
        return {"stop_loss": stop, "action": action, "symbol": symbol}

    return _lookup


def test_naked_long_is_repaired_from_the_recorded_buy_stop():
    """Spec §11.1 guard 1 extends to the repair belt: it now places the stop
    through the same retrying+fallback machinery the entry path uses
    (`_submit_protective_stop_retrying`), not a bare single-shot submit."""
    p = _pipeline()
    p.broker._submit_protective_stop_retrying.return_value = {"id": "stop-1"}
    gaps = p._reconcile_stop_coverage()
    assert len(gaps) == 1 and gaps[0]["repaired"] is True
    kwargs = p.broker._submit_protective_stop_retrying.call_args.kwargs
    assert kwargs["symbol"] == "VST"
    assert kwargs["qty"] == 31.0  # the whole uncovered position
    assert kwargs["stop_price"] == 158.75  # the level PM/RM actually approved
    assert kwargs.get("limit_price") is None  # stop-MARKET: no limit
    assert kwargs["side"] == "sell"


def test_partial_coverage_repairs_only_the_uncovered_shares():
    p = _pipeline(held_qty=31.0, covered=20.0)
    p.broker._submit_protective_stop_retrying.return_value = {"id": "stop-1"}
    gaps = p._reconcile_stop_coverage()
    assert gaps[0]["repaired"] is True
    assert p.broker._submit_protective_stop_retrying.call_args.kwargs["qty"] == 11.0


def test_repair_of_a_fractional_gap_that_only_partially_covers_keeps_escalating():
    """The retrying call fell back to a whole-share floor stop (the §11.1
    open question about a broker that won't carry a fractional-qty stop,
    resolved unfavourably) — real progress, but `repaired` must stay False
    so this pass keeps escalating rather than going quiet on a real gap."""
    p = _pipeline(held_qty=12.3456, covered=0.0)
    p.broker._submit_protective_stop_retrying.return_value = {
        "id": "stop-1",
        "covered_qty": 12.0,
        "uncovered_qty": 0.3456,
    }
    gaps = p._reconcile_stop_coverage()
    assert gaps[0]["repaired"] is False


def test_repair_refuses_a_stop_at_or_above_the_live_price():
    """Recorded stop $158.75 but the stock is now $150 — placing it would fire
    instantly. That's an exit decision; flag, don't act."""
    p = _pipeline(price=150.0)
    gaps = p._reconcile_stop_coverage()
    assert gaps[0]["repaired"] is False
    p.broker._submit_protective_stop_retrying.assert_not_called()


def test_repair_skipped_when_the_buy_row_has_no_stop():
    p = _pipeline(buy_stop=0.0)
    gaps = p._reconcile_stop_coverage()
    assert gaps[0]["repaired"] is False
    p.broker._submit_protective_stop_retrying.assert_not_called()


def test_repair_failure_still_reports_the_gap():
    """`_submit_protective_stop_retrying` never raises — it exhausts its own
    retries and reports failure as a `None` return, not an exception. This
    was `side_effect = RuntimeError` against the old bare single-shot call;
    the new boundary reports the same real-world failure as `None`."""
    p = _pipeline()
    p.broker._submit_protective_stop_retrying.return_value = None
    gaps = p._reconcile_stop_coverage()
    assert len(gaps) == 1 and gaps[0]["repaired"] is False  # no raise


def test_covered_long_needs_no_repair():
    p = _pipeline(covered=31.0)
    assert p._reconcile_stop_coverage() == []
    p.broker._submit_protective_stop_retrying.assert_not_called()


def test_fractional_remainder_on_a_short_is_repaired_with_a_buy_stop():
    """The session sweep has two repair call sites: whole-gap and the
    fractional remainder. Naked-short tests only hit the first. Dropping
    ``is_short`` on the fractional caller would read the BUY row, place a
    SELL stop against a short, and stamp repaired=True (silent). This is
    that branch: GTC buy-stop intact, sub-share remainder missing, market
    open."""
    p = _pipeline(
        held_qty=-40.4,
        covered=40.0,
        price=200.0,
        symbol="TSLA",
        last_buy=_opening_lookup(BUY=180.0, SHORT=220.0),
    )
    p.broker._submit_protective_stop_retrying.return_value = {"id": "buy-stop-frac"}
    with patch("src.pipeline_protection._market_is_open_now", return_value=True):
        gaps = p._reconcile_stop_coverage()
    assert len(gaps) == 1 and gaps[0]["repaired"] is True
    assert gaps[0]["coverage"] == "fractional_replaced"
    kwargs = p.broker._submit_protective_stop_retrying.call_args.kwargs
    assert kwargs["side"] == "buy"
    assert kwargs["stop_price"] == 220.0
    assert abs(kwargs["qty"] - 0.4) < 1e-9
    assert p.db.get_symbol_last_buy.call_args.kwargs.get("action") == "SHORT"


def test_naked_short_is_repaired_from_the_recorded_short_stop():
    """The load-bearing short case: uncovered short → BUY stop at the SHORT
    row's recorded level. A BUY-only lookup returns None here, so a half-fix
    that only un-skips shorts cannot pass."""
    p = _pipeline(
        held_qty=-40.0,
        covered=0.0,
        price=200.0,
        symbol="TSLA",
        last_buy=_opening_lookup(SHORT=220.0),
    )
    p.broker._submit_protective_stop_retrying.return_value = {"id": "buy-stop-1"}
    gaps = p._reconcile_stop_coverage()
    assert len(gaps) == 1 and gaps[0]["repaired"] is True
    kwargs = p.broker._submit_protective_stop_retrying.call_args.kwargs
    assert kwargs["symbol"] == "TSLA"
    assert kwargs["qty"] == 40.0
    assert kwargs["stop_price"] == 220.0
    assert kwargs.get("limit_price") is None  # stop-MARKET: no limit
    assert kwargs["side"] == "buy"
    p.broker.snapshot_protective_stops.assert_called_with("TSLA", side="buy")
    p.db.get_symbol_last_buy.assert_called()
    assert p.db.get_symbol_last_buy.call_args.kwargs.get("action") == "SHORT"


def test_short_repair_does_not_use_a_stale_buy_stop():
    """A prior long on the same ticker must not donate its sell-stop to a
    later short. The BUY row's $80 would sit *below* $200 and, placed as a
    buy-stop, would fire immediately — or as a sell-stop would be the wrong
    side. Repair must read the SHORT row."""
    p = _pipeline(
        held_qty=-10.0,
        covered=0.0,
        price=200.0,
        symbol="TSLA",
        last_buy=_opening_lookup(BUY=80.0, SHORT=220.0),
    )
    p.broker._submit_protective_stop_retrying.return_value = {"id": "buy-stop-1"}
    gaps = p._reconcile_stop_coverage()
    assert gaps[0]["repaired"] is True
    kwargs = p.broker._submit_protective_stop_retrying.call_args.kwargs
    assert kwargs["side"] == "buy"
    assert kwargs["stop_price"] == 220.0
    assert kwargs["stop_price"] != 80.0


def test_buy_only_lookup_cannot_repair_a_short():
    """Half-fix tripwire: un-skipping shorts while still reading action=BUY
    finds no row and must leave the gap flagged, not invent a level."""
    p = _pipeline(
        held_qty=-40.0,
        covered=0.0,
        price=200.0,
        symbol="TSLA",
        last_buy=_opening_lookup(BUY=180.0),
    )
    gaps = p._reconcile_stop_coverage()
    assert len(gaps) == 1 and gaps[0]["repaired"] is False
    p.broker._submit_protective_stop_retrying.assert_not_called()


def test_short_repair_refuses_a_stop_at_or_below_the_live_price():
    """Recorded buy-stop $180 but the stock is now $200 — placing it would
    cover the short instantly. That's an exit decision; flag, don't act."""
    p = _pipeline(
        held_qty=-40.0,
        covered=0.0,
        price=200.0,
        symbol="TSLA",
        last_buy=_opening_lookup(SHORT=180.0),
    )
    gaps = p._reconcile_stop_coverage()
    assert gaps[0]["repaired"] is False
    p.broker._submit_protective_stop_retrying.assert_not_called()


def test_short_repair_skipped_when_the_short_row_has_no_stop():
    p = _pipeline(
        held_qty=-40.0,
        covered=0.0,
        price=200.0,
        symbol="TSLA",
        last_buy=_opening_lookup(SHORT=0.0),
    )
    gaps = p._reconcile_stop_coverage()
    assert gaps[0]["repaired"] is False
    p.broker._submit_protective_stop_retrying.assert_not_called()


def test_long_path_ignores_a_short_row_on_the_same_symbol():
    """Mirror of the stale-BUY case: a currently-held long must not pick up
    a later SHORT row's stop above the tape."""
    p = _pipeline(
        held_qty=10.0,
        covered=0.0,
        price=150.0,
        symbol="NVDA",
        last_buy=_opening_lookup(BUY=140.0, SHORT=170.0),
    )
    p.broker._submit_protective_stop_retrying.return_value = {"id": "sell-stop-1"}
    gaps = p._reconcile_stop_coverage()
    assert gaps[0]["repaired"] is True
    kwargs = p.broker._submit_protective_stop_retrying.call_args.kwargs
    assert kwargs["side"] == "sell"
    assert kwargs["stop_price"] == 140.0


def test_get_symbol_last_buy_default_does_not_see_a_short_row(tmp_path):
    """PM-memory callers keep the BUY-only contract. A SHORT on the same
    ticker is a different position and must not overwrite last-buy."""
    db = Database(str(tmp_path / "t.db"))
    db.initialize()
    db.insert_trade(
        symbol="TSLA",
        action="BUY",
        qty=5,
        price=180.0,
        reasoning="old long",
        run_id="r0",
        stop_loss=160.0,
        fill_status="filled",
    )
    db.insert_trade(
        symbol="TSLA",
        action="SHORT",
        qty=8,
        price=200.0,
        reasoning="new short",
        run_id="r1",
        stop_loss=220.0,
        fill_status="filled",
    )
    last_buy = db.get_symbol_last_buy("TSLA")
    last_short = db.get_symbol_last_buy("TSLA", action="SHORT")
    assert last_buy is not None and last_buy["stop_loss"] == 160.0
    assert last_buy["action"] == "BUY"
    assert last_short is not None and last_short["stop_loss"] == 220.0
    assert last_short["action"] == "SHORT"
    assert db.get_symbol_last_buy("TSLA", action="COVER") is None


def test_get_symbol_last_buy_short_include_in_flight(tmp_path):
    """Same-session in-flight SHORT is the row repair wants, mirroring the
    BUY audit-round-2 belt."""
    db = Database(str(tmp_path / "t.db"))
    db.initialize()
    db.insert_trade(
        symbol="TSLA",
        action="SHORT",
        qty=8,
        price=190.0,
        reasoning="old short",
        run_id="r0",
        stop_loss=210.0,
        fill_status="filled",
    )
    db.insert_trade(
        symbol="TSLA",
        action="SHORT",
        qty=8,
        price=200.0,
        reasoning="today",
        run_id="r1",
        stop_loss=222.0,
        broker_order_id="s9",
        fill_status="submitted",
    )
    strict = db.get_symbol_last_buy("TSLA", action="SHORT")
    in_flight = db.get_symbol_last_buy(
        "TSLA",
        include_in_flight=True,
        action="SHORT",
    )
    assert strict["stop_loss"] == 210.0
    assert in_flight["stop_loss"] == 222.0


def test_naked_short_repair_through_a_real_short_row(tmp_path):
    """End-to-end: the pipeline's last_buy lambda actually queries action=
    SHORT on a real Database. Mocking get_symbol_last_buy.return_value would
    let a BUY-only SQL still look repaired."""
    db = Database(str(tmp_path / "t.db"))
    db.initialize()
    db.insert_trade(
        symbol="TSLA",
        action="SHORT",
        qty=40,
        price=200.0,
        reasoning="opened short",
        run_id="r1",
        stop_loss=220.0,
        fill_status="filled",
    )
    p = build_pipeline(broker=MagicMock(), db=db)
    p.broker.get_positions.return_value = [MagicMock(symbol="TSLA", qty=-40.0)]
    p.broker.snapshot_protective_stops.return_value = (True, [])
    p.broker.get_latest_price.return_value = 200.0
    p.broker._submit_protective_stop_retrying.return_value = {"id": "buy-stop-1"}
    p.cash_sweeper = None
    gaps = p._reconcile_stop_coverage()
    assert len(gaps) == 1 and gaps[0]["repaired"] is True
    kwargs = p.broker._submit_protective_stop_retrying.call_args.kwargs
    assert kwargs["side"] == "buy"
    assert kwargs["stop_price"] == 220.0
    assert kwargs["qty"] == 40.0


def test_one_arg_last_buy_cannot_repair_a_short_from_a_stale_long_stop():
    """A last_buy that does not accept action= must fail closed, not fall
    back to a long's stop and place it as a buy-stop on a short. A $270
    long stop against a $200 short would pass the short-side price guard
    (it sits above the tape) and stamp the gap repaired."""
    from src.execution.stop_repair import repair_stop_coverage

    broker = MagicMock()
    broker.get_latest_price.return_value = 200.0

    def _one_arg(_symbol):
        return {"stop_loss": 270.0}

    out = repair_stop_coverage(
        broker=broker,
        last_buy=_one_arg,
        symbol="TSLA",
        uncovered_qty=40.0,
        is_short=True,
    )
    assert out is False
    broker._submit_protective_stop_retrying.assert_not_called()


def test_repair_stop_coverage_refuses_to_guess_direction():
    """Direction is not a default. Omitting is_short must fail closed
    rather than silently take the long path."""
    from src.execution.stop_repair import repair_stop_coverage

    broker = MagicMock()
    with pytest.raises(TypeError):
        repair_stop_coverage(
            broker=broker,
            last_buy=lambda s, action="BUY": {"stop_loss": 140.0},
            symbol="NVDA",
            uncovered_qty=10.0,
        )
    broker._submit_protective_stop_retrying.assert_not_called()


# ---------------------------------------------------------------------------
# Price provenance (2026-09-17)
#
# The wrong-side test above decides whether putting a stop back would fire it
# instantly — i.e. sell the position at market. It is only as good as the
# price it runs against, and the price reader used to throw away both the
# trade time and whether a trade happened at all: yesterday's last print on a
# thin name, and an unconfirmed quote midpoint, arrived looking exactly like
# a live price. Against a stale number the wrong-side test can read "safe"
# while today's real price is already through the stop.
# ---------------------------------------------------------------------------


def _stamped(price, *, source="last_trade", is_today=True, is_today_print=True):
    from src.execution.broker import LivePrice

    return LivePrice(
        price=price,
        source=source,
        trade_at=None,
        is_today=is_today,
        is_today_print=is_today_print,
    )


def test_repair_uses_a_today_trade_print_when_one_is_available():
    p = _pipeline()
    p.broker.get_latest_price_stamped.return_value = _stamped(165.0)
    p.broker._submit_protective_stop_retrying.return_value = {"id": "stop-1"}
    gaps = p._reconcile_stop_coverage()
    assert len(gaps) == 1 and gaps[0]["repaired"] is True
    assert p.broker._submit_protective_stop_retrying.call_args.kwargs["stop_price"] == 158.75


def test_repair_refuses_a_price_that_is_not_from_today():
    """A stop placed off a prior session's print could fire immediately. The
    gap stays flagged for the next sweep — the same outcome this belt already
    produces for every other unverifiable input."""
    p = _pipeline()
    p.broker.get_latest_price_stamped.return_value = _stamped(
        165.0,
        is_today=False,
        is_today_print=False,
    )
    gaps = p._reconcile_stop_coverage()
    assert len(gaps) == 1 and gaps[0]["repaired"] is not True
    p.broker._submit_protective_stop_retrying.assert_not_called()


def test_repair_refuses_a_quote_midpoint_because_the_tape_never_traded_there():
    p = _pipeline()
    p.broker.get_latest_price_stamped.return_value = _stamped(
        165.0,
        source="quote_mid",
        is_today=True,
        is_today_print=False,
    )
    gaps = p._reconcile_stop_coverage()
    assert len(gaps) == 1 and gaps[0]["repaired"] is not True
    p.broker._submit_protective_stop_retrying.assert_not_called()


# ---------------------------------------------------------------------------
# item 132: a stale last_trade must not shadow a live minute/session bar.
# `get_latest_price_stamped` only ever stamps `is_today_print` for a real
# `last_trade` — never for today's still-forming minute/session bar, even
# though `src.data.live_price.resolve_live_price` (the same freshness
# resolver the research path trusts) treats either as a legitimate today
# print. Before this fix, a name with a stale last_trade but a live minute
# bar refused here regardless — an avoidable no-print refusal and a false
# owner alarm.
# ---------------------------------------------------------------------------


def _today_snapshot(**overrides):
    """An intraday-snapshot payload with a today session_bar_at.

    `session_bar_at` is dated (not timed), so date-equality against the
    real ET "now" is enough to make it today's regardless of what wall-clock
    hour the test happens to run at — no need to fake the 09:30 ET bound
    that `resolve_live_price` applies to point-in-time candidates.
    """
    from src.trading_calendar import et_now

    payload = {
        "last_price": None,
        "last_trade_at": None,
        "minute_close": None,
        "minute_bar_at": None,
        "session_open": 164.0,
        "session_close": None,
        "session_bar_at": et_now(),
    }
    payload.update(overrides)
    return payload


def test_stale_last_trade_is_repaired_off_a_live_today_bar():
    """A stale last_trade alone would refuse (see test above), but a today
    session bar sitting in the same snapshot is a real print and must let
    the repair through."""
    p = _pipeline()
    p.broker.get_latest_price_stamped.return_value = _stamped(
        165.0,
        is_today=False,
        is_today_print=False,
    )
    p.broker.get_intraday_snapshots.return_value = {
        "VST": _today_snapshot(session_open=164.0),
    }
    p.broker._submit_protective_stop_retrying.return_value = {"id": "stop-1"}
    gaps = p._reconcile_stop_coverage()
    assert len(gaps) == 1 and gaps[0]["repaired"] is True
    # Repaired against the recorded stop level, not the fallback price.
    assert p.broker._submit_protective_stop_retrying.call_args.kwargs["stop_price"] == 158.75


def test_no_last_trade_and_no_today_bar_still_refuses():
    """No today print anywhere — genuinely unverifiable — must still refuse,
    exactly as before this fix."""
    p = _pipeline()
    p.broker.get_latest_price_stamped.return_value = _stamped(
        165.0,
        is_today=False,
        is_today_print=False,
    )
    p.broker.get_intraday_snapshots.return_value = {"VST": {}}
    gaps = p._reconcile_stop_coverage()
    assert len(gaps) == 1 and gaps[0]["repaired"] is not True
    p.broker._submit_protective_stop_retrying.assert_not_called()


# ---------------------------------------------------------------------------
# a failed SESSION-HOURS re-placement has to reach the owner from the session
# ---------------------------------------------------------------------------
# On 2026-09-18 09:30:45 ET the fractional coverage repair refused NET
# (held 3.4785, covered 3.0000) and RSG (held 9.2860, covered 9.0000) and
# logged "this is case (b) and it alerts." It alerted nobody: the only code
# that could page lived in the standalone coverage watchdog, and no watchdog
# process ran that day. A failed session-hours repair falls back to
# 'partial' whenever any whole share is still covered, so it never reached
# the NO-STOP-AT-ALL escalation either. The owner sees this desk only
# through Telegram; NET's 0.4785 shares sat unprotected for 15 minutes and
# he was never told.


def _fractional_pipeline(symbol, held, covered, price, *, buy_stop=1.0):
    p = _pipeline(
        held_qty=held,
        covered=covered,
        price=price,
        symbol=symbol,
        buy_stop=buy_stop,
    )
    p.broker.get_positions.return_value = [
        MagicMock(symbol=symbol, qty=held, current_price=price),
    ]
    p.broker._submit_protective_stop_retrying.return_value = None
    return p


@pytest.fixture
def shared_marker(tmp_path, monkeypatch):
    """Point the once-a-day placement-failure marker at a scratch file."""
    from src import coverage_watchdog

    path = tmp_path / "coverage_heartbeat.json"
    monkeypatch.setattr(coverage_watchdog, "STATE_PATH", path)
    return path


def _run(p):
    with (
        patch("src.notifier.send_owner_alert") as send,
        patch("src.pipeline_protection._market_is_open_now", return_value=True),
        patch("src.trader_feed._profiles", return_value={}),
    ):
        gaps = p._reconcile_stop_coverage()
    return gaps, send


def test_failed_session_hours_repair_alerts_the_owner_from_the_session(
    shared_marker,
):
    p = _fractional_pipeline("NET", 3.4785, 3.0, 334.0, buy_stop=300.0)
    gaps, send = _run(p)
    assert gaps[0]["coverage"] == "partial"  # unchanged classification
    assert gaps[0]["session_repair_failed"] is True
    assert send.call_count == 1, "the session that saw it must be the one that tells him"
    text = send.call_args.args[0]
    assert "COULD NOT PUT THE PROTECTIVE STOP BACK" in text
    assert "NET" in text and "holding 3.4785" in text
    assert "stop covers 3" in text
    assert "unprotected" in text
    assert send.call_args.kwargs["symbols"] == ["NET"]


def test_the_overnight_fractional_lapse_still_says_nothing(shared_marker):
    """Owner-ratified, bounded, happens every night — it must stay silent."""
    p = _fractional_pipeline("NET", 3.4785, 3.0, 334.0, buy_stop=300.0)
    with (
        patch("src.notifier.send_owner_alert") as send,
        patch("src.pipeline_protection._market_is_open_now", return_value=False),
    ):
        gaps = p._reconcile_stop_coverage()
    assert gaps[0]["coverage"] == "fractional_overnight"
    assert "session_repair_failed" not in gaps[0]
    send.assert_not_called()


def test_the_same_position_is_not_paged_twice_in_one_day(shared_marker):
    p = _fractional_pipeline("NET", 3.4785, 3.0, 334.0, buy_stop=300.0)
    _gaps, first = _run(p)
    assert first.call_count == 1
    _gaps, second = _run(_fractional_pipeline("NET", 3.4785, 3.0, 334.0, buy_stop=300.0))
    second.assert_not_called()


def test_a_second_position_failing_later_is_not_swallowed(shared_marker):
    """The trap `should_alert_repair_failure`'s docstring warns about, one
    level down: a per-DAY marker claimed by NET at 09:30 would silence RSG."""
    _gaps, first = _run(_fractional_pipeline("NET", 3.4785, 3.0, 334.0, buy_stop=300.0))
    assert first.call_count == 1
    _gaps, later = _run(_fractional_pipeline("RSG", 9.2860, 9.0, 217.0, buy_stop=200.0))
    assert later.call_count == 1
    assert "RSG" in later.call_args.args[0]


def test_the_standalone_watchdog_and_the_session_share_one_marker(shared_marker):
    """Both processes can see the identical condition; the owner hears once."""
    from src import coverage_watchdog

    _gaps, first = _run(_fractional_pipeline("NET", 3.4785, 3.0, 334.0, buy_stop=300.0))
    assert first.call_count == 1
    assert coverage_watchdog.claim_repair_failure_alert(["NET"]) == []
    assert coverage_watchdog.claim_repair_failure_alert(["RSG"]) == ["RSG"]


def test_a_failure_that_actually_landed_does_not_page(shared_marker):
    """2026-09-16: two processes ran the same BRK-B repair 5 ms apart and
    submitted DAY orders 23 ms apart; the loser's retry loop reported
    FAILURE on an order that had landed. The broker is re-read right before
    the alert goes out, so the owner is not told about a failure that
    succeeded. The duplicate-submission race itself is a separate defect
    and is NOT addressed here."""
    p = _fractional_pipeline("BRK-B", 1.4393, 1.0, 505.0, buy_stop=460.0)
    reads = {"n": 0}

    def _snapshot(symbol, side="sell", **_kw):
        reads["n"] += 1
        if reads["n"] == 1:  # the survey pass: really short
            return True, [{"qty": 1.0, "stop_price": 501.06}]
        # by the time we would alert, the sibling process's order is resting
        return True, [
            {"qty": 1.0, "stop_price": 501.06},
            {"qty": 0.4393, "stop_price": 501.06},
        ]

    p.broker.snapshot_protective_stops.side_effect = _snapshot
    gaps, send = _run(p)
    assert gaps[0]["session_repair_failed"] is True  # the gap is still reported
    send.assert_not_called()
    assert reads["n"] >= 2, "the broker must be re-read before paging"


# ---------------------------------------------------------------------------
# a protective stop that FIRED and did NOT FILL
# ---------------------------------------------------------------------------
# The desk's protective stops rest at the broker as stop-LIMIT orders with a
# 3% buffer, and `STOP_LIMIT_BUFFER_PCT`'s own comment states the trade-off:
# "on gaps beyond 3% the limit won't fill and the position stays open until a
# session can act." Nothing could see that state. The coverage sweep lists
# orders with status=OPEN and counts an elected-but-unfilled stop's shares as
# covered, so the one routine whose job is restoring lapsed protection looks
# at a blown-through stop and correctly-by-its-own-logic does nothing. The
# only code that compared price to stop clamped a negative gap to zero and
# reported it as "near".
#
# These tests pin the DETECTION only. Nothing here may sell, cancel or
# replace an order: an exit decision on an unfilled stop is an owner-level
# change and is deliberately not made.


def _elected_pipeline(symbol, held, price, stop, *, covered=None):
    """A position the sweep considers FULLY covered, whose stop the tape has
    already passed. Fully covered on purpose — that is the blind spot."""
    qty = abs(held) if held > 0 else held
    p = _pipeline(held_qty=abs(held), price=price, symbol=symbol)
    p.broker.get_positions.return_value = [
        MagicMock(symbol=symbol, qty=qty, current_price=price),
    ]
    p.broker.snapshot_protective_stops.return_value = (
        True,
        [{"qty": abs(held) if covered is None else covered, "stop_price": stop}],
    )
    return p


def test_a_long_whose_stop_fired_without_filling_is_detected_and_reported(
    shared_marker,
):
    p = _elected_pipeline("VST", 31.0, price=150.0, stop=158.0)
    gaps, send = _run(p)
    # The sweep still says coverage is fine — it is, by share count.
    assert gaps == []
    assert send.call_count == 1, "the session that saw it must be the one that tells him"
    text = send.call_args.args[0]
    assert "A PROTECTIVE STOP FIRED AND DID NOT FILL" in text
    assert "VST" in text and "holding 31" in text
    assert "$158.00 fired and did not fill" in text
    assert "$8.00 past it" in text
    assert "nothing standing watch" in text
    assert send.call_args.kwargs["symbols"] == ["VST"]


def test_a_short_whose_buy_stop_fired_without_filling_is_detected(shared_marker):
    p = _elected_pipeline("VST", -10.0, price=170.0, stop=158.0)
    _gaps, send = _run(p)
    assert send.call_count == 1
    text = send.call_args.args[0]
    assert "A PROTECTIVE STOP FIRED AND DID NOT FILL" in text
    assert "$12.00 past it" in text


def test_a_merely_tight_stop_is_not_reported_as_unfilled(shared_marker):
    """Price ABOVE a long's stop: the order has not been elected at all."""
    p = _elected_pipeline("VST", 31.0, price=160.0, stop=158.0)
    _gaps, send = _run(p)
    send.assert_not_called()


def test_price_exactly_at_the_trigger_is_not_reported(shared_marker):
    """Strict inequality, so the float-equality case resolves towards
    silence rather than towards a tolerance nobody chose."""
    p = _elected_pipeline("VST", 31.0, price=158.0, stop=158.0)
    _gaps, send = _run(p)
    send.assert_not_called()


def test_an_elected_unfilled_stop_is_not_detected_while_the_market_is_shut(
    shared_marker,
):
    p = _elected_pipeline("VST", 31.0, price=150.0, stop=158.0)
    with (
        patch("src.notifier.send_owner_alert") as send,
        patch("src.pipeline_protection._market_is_open_now", return_value=False),
    ):
        p._reconcile_stop_coverage()
    send.assert_not_called()


def test_detection_places_cancels_and_replaces_nothing(shared_marker):
    p = _elected_pipeline("VST", 31.0, price=150.0, stop=158.0)
    _gaps, _send = _run(p)
    p.broker._submit_protective_stop_retrying.assert_not_called()
    p.broker.cancel_protective_stops.assert_not_called()
    p.broker.replace_stop_loss.assert_not_called()
    p.broker.close_position.assert_not_called()
    p.broker.submit_order.assert_not_called()


def test_the_same_blown_through_stop_is_not_paged_twice_in_one_day(shared_marker):
    _gaps, first = _run(_elected_pipeline("VST", 31.0, price=150.0, stop=158.0))
    assert first.call_count == 1
    _gaps, second = _run(_elected_pipeline("VST", 31.0, price=150.0, stop=158.0))
    second.assert_not_called()


def test_a_second_name_blowing_through_later_is_not_swallowed(shared_marker):
    _gaps, first = _run(_elected_pipeline("VST", 31.0, price=150.0, stop=158.0))
    assert first.call_count == 1
    _gaps, later = _run(_elected_pipeline("NET", 12.0, price=300.0, stop=334.0))
    assert later.call_count == 1
    assert "NET" in later.call_args.args[0]


def test_the_two_conditions_do_not_silence_each_other(shared_marker):
    """PR #514's placement-failure marker and this one share the state file
    and the trading-day key, but not the identity. One key would let a stop
    that could not be PLACED silence a stop that did not FILL on the same
    name, which is not what "do not double-alert" means."""
    from src import coverage_watchdog

    _gaps, first = _run(_elected_pipeline("VST", 31.0, price=150.0, stop=158.0))
    assert first.call_count == 1
    assert coverage_watchdog.claim_elected_unfilled_alert(["VST"]) == []
    assert coverage_watchdog.claim_repair_failure_alert(["VST"]) == ["VST"]


def test_the_worst_elected_trigger_is_the_one_reported(shared_marker):
    """A position can carry several stops. The distance reported is to the
    trigger the tape is furthest past — read off the orders, not chosen."""
    p = _elected_pipeline("VST", 31.0, price=150.0, stop=158.0)
    p.broker.snapshot_protective_stops.return_value = (
        True,
        [{"qty": 20.0, "stop_price": 155.0}, {"qty": 11.0, "stop_price": 160.0}],
    )
    _gaps, send = _run(p)
    assert "$160.00 fired and did not fill" in send.call_args.args[0]
    assert "$10.00 past it" in send.call_args.args[0]


# ---------------------------------------------------------------------------
# the evening report must stop merging "tight" with "blown through"
# ---------------------------------------------------------------------------


def _evening_pipeline(qty, price, stop, atr=2.0):
    p = build_pipeline(broker=MagicMock())
    p.db = MagicMock()
    p.broker.get_current_stop_price.return_value = stop
    p._sweep_symbol = lambda: None
    p._atr_for_symbol = lambda _sym: atr
    return p, [MagicMock(symbol="VST", qty=qty, current_price=price)]


def test_evening_reports_a_blown_through_stop_as_its_own_state():
    p, positions = _evening_pipeline(31.0, price=150.0, stop=158.0)
    rows = p._evening_stop_proximity(positions)
    assert len(rows) == 1
    assert rows[0]["status"] == "through", (
        "a stop the tape has passed without filling is not the same fact as a stop that is merely close"
    )
    assert rows[0]["through"] == pytest.approx(8.0)


def test_evening_still_reports_a_genuinely_tight_stop_as_near():
    p, positions = _evening_pipeline(31.0, price=159.0, stop=158.0)
    rows = p._evening_stop_proximity(positions)
    assert rows[0]["status"] == "near"
    assert rows[0]["gap"] == pytest.approx(1.0)


def test_the_evening_feed_spells_out_a_blown_through_stop():
    from src.trader_feed import _append_evening_watchlist

    lines: list[str] = []
    _append_evening_watchlist(
        lines,
        {
            "stop_proximity": [
                {
                    "symbol": "VST",
                    "status": "through",
                    "price": 150.0,
                    "stop": 158.0,
                    "through": 8.0,
                    "atr": 2.0,
                }
            ]
        },
        {},
    )
    text = "\n".join(lines)
    assert "fired and did not fill" in text
    assert "$158.00" in text and "$8.00 past it" in text
    assert "nothing standing watch over them" in text
    assert "inside one ordinary day's move" not in text


# ---------------------------------------------------------------------------
# WHICH SWEEP IS ENTITLED TO PAGE
# ---------------------------------------------------------------------------
# Measured, production log, all retained rotations (2026-08-21..2026-09-23):
# the `no_trade_print_today` refusal occurred 6 times on 4 sessions (NET and
# RSG 09-18; BRK-B, NUE and RSG 09-21; RSG 09-23), every one between
# 13:30:43 and 13:30:45 UTC, and every one resolved in the same session. On
# 2026-09-23 the owner was paged in red at 13:30:45 and told to place a stop
# by hand; the desk placed it itself at 13:45:45 and never said so.
#
# The refusal is correct and is NOT under test here. What is under test is
# that the FIRST attempt, seconds after the bell, is not the one that pages,
# that a genuinely stuck position still does, and that a red alarm which
# clears says so.


def _no_print_pipeline(symbol, held, covered, price, *, buy_stop):
    """A fractional gap whose repair refuses because the name has not
    printed today — the exact 2026-09-23 RSG state."""
    from src.execution.broker import LivePrice

    p = _fractional_pipeline(symbol, held, covered, price, buy_stop=buy_stop)
    p.broker.get_latest_price_stamped.return_value = LivePrice(
        price=price,
        source="last_trade",
        trade_at=None,
        is_today=False,
        is_today_print=False,
    )
    return p


def test_the_bell_adjacent_no_print_refusal_does_not_page(shared_marker):
    """43 seconds after the open a name that has not printed yet is an
    expected state, not a placement failure. The whole-share leg is still
    standing watch and the desk's own next pass resolves it."""
    p = _no_print_pipeline("RSG", 22.5862, 22.0, 213.79, buy_stop=213.33)
    gaps, send = _run(p)
    assert gaps[0]["awaiting_first_print"] is True
    assert gaps[0]["session_repair_failed"] is False
    assert gaps[0]["coverage"] == "partial", (
        "the shortfall is real and must stay visible in the banner; only the interruption is withheld"
    )
    send.assert_not_called()


def test_the_repair_is_still_attempted_on_the_quiet_pass(shared_marker):
    """docs/INCIDENT_HISTORY.md 2026-09-18 rejected DEFERRING THE RETRY.
    That ruling stands: only the page waits."""
    p = _no_print_pipeline("RSG", 22.5862, 22.0, 213.79, buy_stop=213.33)
    _run(p)
    assert p.broker.get_latest_price_stamped.called, "the pass must still have tried to price and place the stop"


def test_a_broker_rejection_still_pages_on_the_first_attempt(shared_marker):
    """2026-09-16 BRK-B: retries exhausted at the broker. Nothing about the
    tape resolves that, so it is a fault on sight and the grace must not
    reach it."""
    p = _fractional_pipeline("BRK-B", 1.4393, 1.0, 505.0, buy_stop=460.0)
    gaps, send = _run(p)
    assert gaps[0]["session_repair_failed"] is True
    assert send.call_count == 1
    assert "COULD NOT PUT THE PROTECTIVE STOP BACK" in send.call_args.args[0]


def test_a_no_print_refusal_with_nothing_covered_pages_immediately(
    shared_marker,
):
    """The grace leans entirely on the durable whole-share leg holding the
    position while the tape catches up. With zero coverage there is no leg
    to lean on and the reason for the refusal stops mattering."""
    p = _no_print_pipeline("RSG", 0.5862, 0.0, 213.79, buy_stop=213.33)
    gaps, send = _run(p)
    assert gaps[0].get("awaiting_first_print") is not True
    assert send.call_count == 1


def test_a_name_that_never_printed_all_session_pages_after_the_close(
    shared_marker,
):
    """Where the quiet state ENDS. A sliver that waited for a first print all
    day and never got a stop is NOT the ratified overnight lapse, and filing
    it as one would turn the bell-adjacent silence into a suppression."""
    session = _no_print_pipeline("RSG", 22.5862, 22.0, 213.79, buy_stop=213.33)
    _gaps, send = _run(session)
    send.assert_not_called()

    after_close = _no_print_pipeline("RSG", 22.5862, 22.0, 213.79, buy_stop=213.33)
    with (
        patch("src.notifier.send_owner_alert") as shut,
        patch("src.pipeline_protection._market_is_open_now", return_value=False),
        patch("src.trader_feed._profiles", return_value={}),
    ):
        gaps = after_close._reconcile_stop_coverage()
    assert gaps[0]["coverage"] != "fractional_overnight"
    assert gaps[0]["never_printed_today"] is True
    assert gaps[0]["session_repair_failed"] is True
    assert shut.call_count == 1
    assert "COULD NOT PUT THE PROTECTIVE STOP BACK" in shut.call_args.args[0]


def test_a_repaired_name_is_not_reported_after_the_close(shared_marker):
    """The ordinary case: it waited at the bell, the next pass placed the
    stop, and the evening must say nothing about it."""
    session = _no_print_pipeline("RSG", 22.5862, 22.0, 213.79, buy_stop=213.33)
    _run(session)

    repaired = _fractional_pipeline("RSG", 22.5862, 22.0, 213.79, buy_stop=213.33)
    repaired.broker._submit_protective_stop_retrying.return_value = {
        "id": "ord-1",
        "status": "accepted",
    }
    _gaps, _send = _run(repaired)

    after_close = _no_print_pipeline("RSG", 22.5862, 22.0, 213.79, buy_stop=213.33)
    with (
        patch("src.notifier.send_owner_alert") as shut,
        patch("src.pipeline_protection._market_is_open_now", return_value=False),
    ):
        gaps = after_close._reconcile_stop_coverage()
    assert gaps[0]["coverage"] == "fractional_overnight"
    shut.assert_not_called()


# ---------------------------------------------------------------------------
# the retraction — the half that did not exist
# ---------------------------------------------------------------------------


def _repairing_pipeline(symbol, held, covered, price, *, buy_stop):
    p = _fractional_pipeline(symbol, held, covered, price, buy_stop=buy_stop)
    p.broker._submit_protective_stop_retrying.return_value = {
        "id": "ord-1",
        "status": "accepted",
    }
    return p


def test_a_red_alarm_that_clears_tells_the_owner_it_cleared(shared_marker):
    """2026-09-23: paged at 13:30:45, repaired by the desk at 13:45:45,
    never retracted. The owner spent the day holding an instruction to place
    by hand a stop that already existed."""
    _gaps, first = _run(_fractional_pipeline("BRK-B", 1.4393, 1.0, 505.0, buy_stop=460.0))
    assert first.call_count == 1
    assert "COULD NOT PUT THE PROTECTIVE STOP BACK" in first.call_args.args[0]

    _gaps, second = _run(_repairing_pipeline("BRK-B", 1.4393, 1.0, 505.0, buy_stop=460.0))
    assert second.call_count == 1, "the all-clear must reach the same channel"
    text = second.call_args.args[0]
    assert "THE PROTECTIVE STOP IS BACK" in text
    assert "BRK-B" in text
    assert second.call_args.kwargs["symbols"] == ["BRK-B"]


def test_the_once_a_day_cap_does_not_swallow_the_all_clear(shared_marker):
    """The placement-failure marker is claimed for the day by the alarm. The
    retraction must not be gated on it — that is exactly how it would be
    swallowed — so it carries its own marker."""
    from src import coverage_watchdog

    _gaps, _first = _run(_fractional_pipeline("BRK-B", 1.4393, 1.0, 505.0, buy_stop=460.0))
    assert coverage_watchdog.claim_repair_failure_alert(["BRK-B"]) == [], (
        "precondition: the failure is already claimed for today"
    )
    _gaps, second = _run(_repairing_pipeline("BRK-B", 1.4393, 1.0, 505.0, buy_stop=460.0))
    assert second.call_count == 1
    assert "THE PROTECTIVE STOP IS BACK" in second.call_args.args[0]


def test_the_all_clear_is_sent_once_per_symbol_per_day(shared_marker):
    _gaps, _first = _run(_fractional_pipeline("BRK-B", 1.4393, 1.0, 505.0, buy_stop=460.0))
    _gaps, second = _run(_repairing_pipeline("BRK-B", 1.4393, 1.0, 505.0, buy_stop=460.0))
    assert second.call_count == 1
    _gaps, third = _run(_repairing_pipeline("BRK-B", 1.4393, 1.0, 505.0, buy_stop=460.0))
    third.assert_not_called()


def test_a_routine_repair_nobody_was_alarmed_about_stays_silent(shared_marker):
    """Every fractional position is re-covered at the open every single day.
    If that sent an all-clear the channel would carry one per position per
    morning, which is the noise the alarm design forbids."""
    _gaps, send = _run(_repairing_pipeline("AAPL", 9.7630, 9.0, 330.0, buy_stop=315.0))
    send.assert_not_called()


def test_the_failure_claim_is_not_released_by_the_all_clear(shared_marker):
    """Both alarm bodies promise "at most once per trading day". Releasing
    the claim on resolution would make that sentence false and would let a
    flapping name send two messages a cycle."""
    from src import coverage_watchdog

    _gaps, _first = _run(_fractional_pipeline("BRK-B", 1.4393, 1.0, 505.0, buy_stop=460.0))
    _gaps, _second = _run(_repairing_pipeline("BRK-B", 1.4393, 1.0, 505.0, buy_stop=460.0))
    assert coverage_watchdog.claim_repair_failure_alert(["BRK-B"]) == []
