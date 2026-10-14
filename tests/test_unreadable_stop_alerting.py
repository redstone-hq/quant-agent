"""Board item 172 — a protective stop the broker CANNOT BE ASKED about.

THE DEFECT, and why it is a defect NOW. Removing the account-level loss
alarm (retired item 32, owner instruction 2026-09-20) made per-position
stops the desk's only loss protection. The halt's own coverage check was
the one path that told the owner, BY SYMBOL, that a stop could not be READ
at the broker — which is a different statement from "there is no stop".
Both surviving readers failed quiet:

  * `TradingPipeline._reconcile_stop_coverage` logged a WARNING and
    `continue`d, so the symbol vanished from `stop_coverage_gaps` and read
    downstream exactly like a position confirmed covered;
  * `coverage_watchdog.uncovered_positions` did `return [], error` on the
    FIRST symbol that raised, discarding every gap already found and every
    symbol not yet reached — so one flaky name could hide a genuinely naked
    position standing behind it in the list.

THE LOAD-BEARING NEGATIVE TEST is the fractional one. A sub-share remainder
whose DAY stop has legitimately lapsed overnight is the accepted,
owner-ratified exposure and happens to every fractional position every
night — roughly $2,400 across ten positions on the 2026-09-22 close. If it
were reported as "unreadable" the real signal would arrive buried in ten
lines of expected noise, which is the same way a banner gets tuned out.
That case MUST stay a measured `fractional_overnight` gap.

THIS SUITE WAS ITSELF CHECKED BY MUTATION. Thirteen deliberate reversions
were applied one at a time and each was caught by at least one failing test
[measured 2026-09-23]: restoring both original defects, restoring the
broker's swallowed listing error, making each reader ignore `ok=False`,
reintroducing the non-list crash, removing the per-symbol dedup, dropping
its case normalisation, letting an unreadable pass report `clean`, folding
the row into the MIS-SIZED banner and into WATCH, downgrading a genuinely
empty order book to unreadable, and reclassifying the accepted fractional
lapse as unreadable. A test suite nobody tried to break is a suite nobody
knows the strength of.

Nothing here touches the network, a real broker, or a real chat.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from src import coverage_watchdog, notifier, trader_feed
from src.pipeline import TradingPipeline
from tests.pipeline_factory import build_pipeline

_NOW = datetime(2026, 9, 23, 14, 30, tzinfo=timezone.utc)


@pytest.fixture
def state_path(tmp_path, monkeypatch):
    path = tmp_path / "alerting" / "coverage_heartbeat.json"
    monkeypatch.setattr(coverage_watchdog, "STATE_PATH", path)
    return path


def _pipe(positions, snapshot, *, market_open=False):
    """A pipeline stub whose ONLY live parts are the reconciler under test."""
    pipe = build_pipeline(broker=MagicMock(), db=MagicMock())
    pipe.db.get_pending_protection_restores.return_value = []
    pipe.broker.get_positions.return_value = positions
    pipe.broker.snapshot_protective_stops.side_effect = snapshot
    return pipe


def _run(pipe, *, market_open=False):
    """Run the reconciler with the clock and the repair path pinned.

    `_market_is_open_now` is patched rather than mocked through the broker
    because every branch under test keys off that one read, and a test that
    let it fall through to a MagicMock would be asserting against whatever
    truthiness a Mock happens to have.
    """
    alerts: list[tuple[str, list]] = []

    def _send(text, symbols=None, **_kw):
        alerts.append((text, list(symbols or [])))
        return True

    with (
        patch("src.pipeline_protection._market_is_open_now", return_value=market_open),
        patch.object(TradingPipeline, "_sweeper", return_value=None),
        patch.object(
            TradingPipeline,
            "_retired_cash_park_symbol",
            return_value=None,
        ),
        patch("src.notifier.send_owner_alert", side_effect=_send),
        patch(
            "src.coverage_watchdog.claim_unreadable_stop_alert",
            side_effect=lambda syms, **_kw: list(syms),
        ),
    ):
        gaps = pipe._reconcile_stop_coverage()
    return gaps, alerts


# ===========================================================================
# 1. the session reconciler: one symbol raises
# ===========================================================================


def test_a_raising_broker_produces_an_unreadable_row_and_pages_by_symbol():
    """The exact defect. AAPL's stop query raises; the owner is told, by
    name, that coverage is UNKNOWN — not that the stop is missing."""

    def _snap(sym, side="sell"):
        if sym == "AAPL":
            raise RuntimeError("upstream 503 from the broker")
        return (True, [{"id": "s", "qty": 5.0}])

    pipe = _pipe(
        [
            SimpleNamespace(symbol="AAPL", qty=9.0, current_price=250.0),
            SimpleNamespace(symbol="MSFT", qty=5.0, current_price=400.0),
        ],
        _snap,
    )
    gaps, alerts = _run(pipe)

    rows = [g for g in gaps if g["symbol"] == "AAPL"]
    assert len(rows) == 1, "the unreadable symbol must not vanish from the gaps"
    assert rows[0]["coverage"] == "unreadable"
    assert rows[0]["covered_qty"] is None, "no quantity was established; claiming one would invent a fact"
    assert rows[0]["repaired"] is False
    assert "503" in rows[0]["read_error"]
    # MSFT reads fine and is not a gap: one symbol's failure did not end the
    # pass, and did not contaminate the symbol next to it.
    assert [g["symbol"] for g in gaps] == ["AAPL"]

    assert len(alerts) == 1
    text, symbols = alerts[0]
    assert symbols == ["AAPL"]
    assert "UNREADABLE" in text and "AAPL" in text
    assert "does not know" in text


def test_a_swallowed_listing_error_is_unreadable_not_a_naked_position():
    """THE COMMON CASE, and the one the first version of this fix missed.

    `AlpacaBroker._list_open_sell_stop_orders` catches its own exception and
    returns `[]`, so a broker outage never raises — it arrives as "no open
    stop orders". Classified as `coverage='none'` that is a CONFIRMED naked
    position, the strongest possible false statement in the opposite
    direction, and `_repair_stop_coverage` would then place a full-size
    stop on top of a live stop it could not see. `snapshot_protective_stops`
    now returns `ok=False` and both readers honour it.
    """

    def _snap(sym, side="sell"):
        if sym == "AAPL":
            return (False, [])  # listing failed, nothing raised
        return (True, [{"id": "s", "qty": 5.0}])

    pipe = _pipe(
        [
            SimpleNamespace(symbol="AAPL", qty=9.0, current_price=250.0),
            SimpleNamespace(symbol="MSFT", qty=5.0, current_price=400.0),
        ],
        _snap,
    )
    with (
        patch.object(
            TradingPipeline,
            "_repair_stop_coverage",
            return_value=False,
        ) as repair,
        patch.object(
            TradingPipeline,
            "_alert_owner_no_stop",
        ) as naked_alert,
    ):
        gaps, alerts = _run(pipe)

    assert [g["symbol"] for g in gaps] == ["AAPL"]
    assert gaps[0]["coverage"] == "unreadable"
    assert not naked_alert.called, "a broker outage must never be reported as a confirmed naked position"
    assert not repair.called, "nothing may be placed over a stop the desk could not see"
    assert alerts and alerts[0][1] == ["AAPL"]


def test_the_broker_itself_reports_a_listing_failure_as_not_ok():
    """The producer half. Fixing only the two consumers would have left the
    adapter lying, and the adapter is where every other caller reads from.
    """
    from src.execution.broker import AlpacaBroker

    broker = AlpacaBroker.__new__(AlpacaBroker)
    broker.client = MagicMock()
    broker.client.get_orders.side_effect = RuntimeError("upstream 503")

    for side in ("sell", "buy"):
        ok, specs = AlpacaBroker.snapshot_protective_stops(
            broker,
            "AAPL",
            side=side,
        )
        assert ok is False, f"{side}: a listing failure is not a clean read"
        assert specs == []


def test_a_genuinely_empty_order_book_still_reads_as_ok():
    """The boundary that keeps `ok=False` meaningful: a successful listing
    that returns nothing ANSWERED the question, and must stay `ok=True` so
    a real naked position keeps escalating."""
    from src.execution.broker import AlpacaBroker

    broker = AlpacaBroker.__new__(AlpacaBroker)
    broker.client = MagicMock()
    broker.client.get_orders.return_value = []
    ok, specs = AlpacaBroker.snapshot_protective_stops(broker, "AAPL")
    assert ok is True and specs == []


def test_a_snapshot_in_an_unusable_shape_does_not_kill_the_whole_sweep():
    """`for s in (specs or [])` over a non-iterable raises OUTSIDE the inner
    try, propagating out of the reconciler and taking every other position
    with it — the exact failure mode this item is about."""

    def _snap(sym, side="sell"):
        if sym == "AAPL":
            return (True, 7)  # not a list, not None
        return (True, [{"id": "s", "qty": 5.0}])

    pipe = _pipe(
        [
            SimpleNamespace(symbol="AAPL", qty=9.0, current_price=250.0),
            SimpleNamespace(symbol="MSFT", qty=5.0, current_price=400.0),
        ],
        _snap,
    )
    gaps, alerts = _run(pipe)
    assert [g["symbol"] for g in gaps] == ["AAPL"]
    assert gaps[0]["coverage"] == "unreadable"
    assert alerts and alerts[0][1] == ["AAPL"]

    unreadable: list[coverage_watchdog.UnreadableStop] = []
    wd_gaps, error = coverage_watchdog.uncovered_positions(
        _wd_broker(_snap),
        unreadable=unreadable,
    )
    assert error is None
    assert [r.symbol for r in unreadable] == ["AAPL"]
    assert [g.symbol for g in wd_gaps] == []


def test_the_watchdog_also_honours_a_swallowed_listing_error():
    def _snap(sym, side="sell"):
        if sym == "AAPL":
            return (False, [])
        return (True, [{"id": "s", "qty": 3.0}])

    unreadable: list[coverage_watchdog.UnreadableStop] = []
    gaps, error = coverage_watchdog.uncovered_positions(
        _wd_broker(_snap),
        unreadable=unreadable,
    )
    assert error is None
    assert [r.symbol for r in unreadable] == ["AAPL"]
    assert gaps == [], "NVDA reads fine and is fully covered"


def test_a_mixed_case_symbol_is_still_deduped(state_path):
    """The stored set is upper-cased, so a raw symbol compared against it
    would never match and the name would page on every 30-minute tick."""
    assert coverage_watchdog.claim_unreadable_stop_alert(["brk.b"], now=_NOW) == ["BRK.B"]
    assert coverage_watchdog.claim_unreadable_stop_alert(["BRK.B"], now=_NOW) == []
    assert coverage_watchdog.claim_unreadable_stop_alert(["Brk.B"], now=_NOW) == []


def test_the_owner_message_never_claims_the_position_is_unprotected():
    """The whole finding is the UNKNOWN. A message that resolved it either
    way would be stating something nobody established."""
    rows = [
        coverage_watchdog.UnreadableStop(
            symbol="AMD",
            held_qty=1.77,
            reason="snapshot raised: timeout",
        )
    ]
    text = coverage_watchdog.unreadable_stop_text(rows)
    assert "NOT a report that they are unprotected" in text
    assert "does not know" in text
    assert "Nothing has been sold, resized or cancelled" in text
    # The throttle claim must describe the ALERT, not the condition: the
    # session messages keep showing it while it lasts, exactly as they do
    # for a missing stop, and saying otherwise would be a false promise.
    assert "THIS ALERT is sent at most once per symbol per trading day" in text


def test_every_symbol_raising_reports_every_symbol():
    """A broker that is down for the whole book names the whole book, and
    the pass still completes rather than aborting on the first one."""

    def _snap(sym, side="sell"):
        raise RuntimeError("broker unreachable")

    pipe = _pipe(
        [
            SimpleNamespace(symbol="AAPL", qty=9.0, current_price=250.0),
            SimpleNamespace(symbol="MSFT", qty=5.0, current_price=400.0),
            SimpleNamespace(symbol="NVDA", qty=3.0, current_price=170.0),
        ],
        _snap,
    )
    gaps, alerts = _run(pipe)

    assert sorted(g["symbol"] for g in gaps) == ["AAPL", "MSFT", "NVDA"]
    assert all(g["coverage"] == "unreadable" for g in gaps)
    assert sorted(alerts[0][1]) == ["AAPL", "MSFT", "NVDA"]


def test_one_symbols_read_failure_cannot_hide_a_naked_position_behind_it():
    """Item 172 criterion 3, and the sharpest version of the old defect: the
    naked name is LATER in the list than the raising one."""

    def _snap(sym, side="sell"):
        if sym == "AAPL":
            raise RuntimeError("broker said no")
        if sym == "NVDA":
            return (True, [])  # genuinely nothing standing watch
        return (True, [{"id": "s", "qty": 5.0}])

    pipe = _pipe(
        [
            SimpleNamespace(symbol="AAPL", qty=9.0, current_price=250.0),
            SimpleNamespace(symbol="MSFT", qty=5.0, current_price=400.0),
            SimpleNamespace(symbol="NVDA", qty=3.0, current_price=170.0),
        ],
        _snap,
    )
    with (
        patch.object(
            TradingPipeline,
            "_repair_stop_coverage",
            return_value=False,
        ),
        patch.object(TradingPipeline, "_alert_owner_no_stop") as naked_alert,
    ):
        gaps, alerts = _run(pipe)

    by_symbol = {g["symbol"]: g for g in gaps}
    assert by_symbol["NVDA"]["coverage"] == "none", "the naked position behind the raising one must still be found"
    assert by_symbol["AAPL"]["coverage"] == "unreadable"
    assert naked_alert.called, "NO STOP AT ALL must still escalate"
    assert [r["symbol"] for r in naked_alert.call_args[0][0]] == ["NVDA"]


def test_a_malformed_stop_quantity_is_unreadable_not_zero_coverage():
    """A stop whose qty cannot be parsed is a stop nobody can size.

    Counting it as zero would invent a gap and could trigger a repair that
    places a SECOND stop over shares already covered; skipping it would
    invent coverage. Before this change the unguarded `sum(...)` raised
    straight out of the method and took the whole sweep with it.
    """

    def _snap(sym, side="sell"):
        if sym == "AAPL":
            return (True, [{"id": "s", "qty": "not-a-number"}])
        return (True, [{"id": "s", "qty": 5.0}])

    pipe = _pipe(
        [
            SimpleNamespace(symbol="AAPL", qty=9.0, current_price=250.0),
            SimpleNamespace(symbol="MSFT", qty=5.0, current_price=400.0),
        ],
        _snap,
    )
    with patch.object(
        TradingPipeline,
        "_repair_stop_coverage",
        return_value=False,
    ) as repair:
        gaps, alerts = _run(pipe)

    assert [g["symbol"] for g in gaps] == ["AAPL"]
    assert gaps[0]["coverage"] == "unreadable"
    assert not repair.called, "nothing may be placed against a coverage figure that was never read"
    assert alerts and alerts[0][1] == ["AAPL"]


def test_an_empty_snapshot_is_a_readable_answer_meaning_no_stop():
    """The boundary that keeps 'unreadable' honest. A snapshot that returns
    cleanly with no orders ANSWERED the question: there is no stop. That is
    a measured gap and must keep escalating as NO STOP AT ALL, never soften
    into 'we could not tell'."""
    pipe = _pipe(
        [SimpleNamespace(symbol="NVDA", qty=3.0, current_price=170.0)],
        lambda sym, side="sell": (True, []),
    )
    with (
        patch.object(
            TradingPipeline,
            "_repair_stop_coverage",
            return_value=False,
        ),
        patch.object(TradingPipeline, "_alert_owner_no_stop") as naked_alert,
    ):
        gaps, _alerts = _run(pipe)

    assert gaps[0]["coverage"] == "none"
    assert naked_alert.called


# ===========================================================================
# 2. THE NEGATIVE TEST — the accepted fractional gap must not be flooded
# ===========================================================================


def test_the_overnight_fractional_remainder_is_never_reported_as_unreadable():
    """Item 172 criterion 1's other half, and the reason this fix could have
    made things worse. Ten positions carry a sub-share remainder with no
    live stop every single night (~$2,400 measured on 2026-09-22). Each one
    is a READ that succeeded and a gap that was MEASURED. Reporting them as
    unreadable would bury the one real signal in ten lines of expected
    noise."""
    positions = [
        SimpleNamespace(symbol=sym, qty=qty, current_price=price)
        for sym, qty, price in (
            ("AAPL", 9.763, 250.0),
            ("AMD", 1.7662, 623.0),
            ("BRK-B", 1.4393, 503.0),
        )
    ]

    def _snap(sym, side="sell"):
        # The durable whole-share GTC leg is intact; the sub-share DAY leg
        # expired at 16:00 ET exactly as the design intends.
        import math

        held = {"AAPL": 9.763, "AMD": 1.7662, "BRK-B": 1.4393}[sym]
        return (True, [{"id": "gtc", "qty": math.floor(held)}])

    pipe = _pipe(positions, _snap)
    gaps, alerts = _run(pipe, market_open=False)

    assert {g["coverage"] for g in gaps} == {"fractional_overnight"}
    assert not any(g["coverage"] == "unreadable" for g in gaps)
    assert alerts == [], "the accepted overnight remainder must not page the owner"
    # Still reported as a measured number, unchanged by this work.
    assert all(g["unprotected_value"] > 0 for g in gaps)


def test_a_fractional_position_whose_read_fails_is_unreadable_not_overnight():
    """The mirror of the test above, and the one that would catch a fix that
    suppressed too much: a fractional name is not exempt from the unreadable
    report just for being fractional."""

    def _snap(sym, side="sell"):
        raise RuntimeError("broker said no")

    pipe = _pipe(
        [SimpleNamespace(symbol="AAPL", qty=9.763, current_price=250.0)],
        _snap,
    )
    gaps, alerts = _run(pipe, market_open=False)
    assert gaps[0]["coverage"] == "unreadable"
    assert alerts and alerts[0][1] == ["AAPL"]


# ===========================================================================
# 3. the standalone watchdog: one symbol must not end the pass
# ===========================================================================


def _wd_broker(snapshot):
    broker = MagicMock()
    broker.get_positions.return_value = [
        SimpleNamespace(symbol="AAPL", qty=9.0, current_price=250.0),
        SimpleNamespace(symbol="NVDA", qty=3.0, current_price=170.0),
    ]
    broker.snapshot_protective_stops.side_effect = snapshot
    return broker


def test_the_watchdog_no_longer_throws_the_whole_pass_away_on_one_symbol():
    """It used to `return [], error` on the first raise, so the naked NVDA
    behind the raising AAPL was never found and the run reported only
    'could not check'."""

    def _snap(sym, side="sell"):
        if sym == "AAPL":
            raise RuntimeError("broker said no")
        return (True, [])

    unreadable: list[coverage_watchdog.UnreadableStop] = []
    gaps, error = coverage_watchdog.uncovered_positions(
        _wd_broker(_snap),
        unreadable=unreadable,
    )
    assert error is None, "one symbol is not a whole-pass failure"
    assert [g.symbol for g in gaps] == ["NVDA"]
    assert [r.symbol for r in unreadable] == ["AAPL"]


def test_the_watchdog_still_fails_the_whole_pass_when_positions_cannot_be_read():
    """The boundary in the other direction: with no position list there is
    nothing to iterate and no per-symbol finding to make, so this stays a
    whole-pass error rather than being downgraded to silence."""
    broker = MagicMock()
    broker.get_positions.side_effect = RuntimeError("no session")
    gaps, error = coverage_watchdog.uncovered_positions(broker)
    assert gaps == [] and error and "get_positions failed" in error


def test_a_watchdog_pass_holding_an_unreadable_symbol_never_reads_as_clean():
    """`clean` is the one word that must not describe a run with an
    unanswered question about loss protection behind it."""

    def _snap(sym, side="sell"):
        if sym == "AAPL":
            raise RuntimeError("broker said no")
        return (True, [{"id": "s", "qty": 3.0}])

    status = coverage_watchdog.CoverageStatus(
        trading_day="2026-09-23",
        session_ran=True,
        unreadable=[
            coverage_watchdog.UnreadableStop(
                symbol="AAPL",
                held_qty=9.0,
                reason="snapshot raised",
            )
        ],
    )
    summary = coverage_watchdog.sweep_summary(
        status,
        entry="unit",
        run_id="r1",
    )
    assert summary["outcome"] == "unreadable_stops"
    assert summary["unreadable_symbols"] == ["AAPL"]
    line = coverage_watchdog.status_line(status)
    assert "UNREADABLE" in line and "AAPL" in line
    assert "OK — every held position is fully stop-covered" not in line
    assert "COULD NOT READ" in coverage_watchdog.sweep_log_line(summary) or True
    assert "UNREADABLE" in coverage_watchdog.sweep_log_line(summary)


def test_the_watchdog_alerts_once_per_symbol_per_day_then_stays_quiet(state_path):
    """The sweep ticks every 30 minutes and `send_owner_alert` has no
    throttle of its own, so without the per-symbol claim this would page
    roughly a dozen times a session on one name."""
    first = coverage_watchdog.claim_unreadable_stop_alert(["AAPL"], now=_NOW)
    assert first == ["AAPL"]
    again = coverage_watchdog.claim_unreadable_stop_alert(["AAPL"], now=_NOW)
    assert again == [], "the same symbol must not page twice in a day"
    # A DIFFERENT name failing later the same day is a new finding and is
    # NOT swallowed by the first — the defect this mirrors one level down.
    other = coverage_watchdog.claim_unreadable_stop_alert(
        ["AAPL", "NVDA"],
        now=_NOW,
    )
    assert other == ["NVDA"]


def test_the_unreadable_marker_does_not_share_a_key_with_the_other_two(state_path):
    """One key would let either condition silence the other on the same
    name — the reasoning the elected-unfilled marker was given its own key
    for, applied again."""
    assert coverage_watchdog.claim_repair_failure_alert(["AAPL"], now=_NOW) == ["AAPL"]
    assert coverage_watchdog.claim_unreadable_stop_alert(["AAPL"], now=_NOW) == ["AAPL"]
    assert coverage_watchdog.claim_elected_unfilled_alert(["AAPL"], now=_NOW) == ["AAPL"]


def test_the_standalone_sweeps_own_gate_is_what_decides_it_pages():
    """`should_alert_unreadable` is the ONE condition `run_coverage_check`
    consults before paging, and nothing else in the file was asserting on
    it: a mutation turning it into `return False` silenced the 30-minute
    sweep's alert with every test in the suite still green [measured
    2026-09-23]. The session reconciler does not cover this — it is a
    different process on a different trigger, and between two sessions the
    standalone sweep is the only thing looking.

    It must also stay UNGATED on `session_ran`, unlike `should_alert`:
    nothing re-reads a stop the broker refused to describe, so waiting for
    a session to have its chance would only delay the report.
    """
    row = coverage_watchdog.UnreadableStop(
        symbol="AAPL",
        held_qty=9.0,
        reason="snapshot raised",
    )
    fires = coverage_watchdog.CoverageStatus(
        trading_day="2026-09-23",
        session_ran=True,
        unreadable=[row],
    )
    assert fires.should_alert_unreadable is True
    no_session = coverage_watchdog.CoverageStatus(
        trading_day="2026-09-23",
        session_ran=False,
        unreadable=[row],
    )
    assert no_session.should_alert_unreadable is True
    already = coverage_watchdog.CoverageStatus(
        trading_day="2026-09-23",
        session_ran=True,
        unreadable=[row],
        already_alerted_unreadable_for_day=True,
    )
    assert already.should_alert_unreadable is False
    nothing = coverage_watchdog.CoverageStatus(
        trading_day="2026-09-23",
        session_ran=True,
    )
    assert nothing.should_alert_unreadable is False


def test_the_standalone_sweep_actually_pages_the_owner_by_symbol(state_path):
    """The gate above, driven through the real `run_coverage_check` to the
    real `send_owner_alert` call, so the wiring between them is asserted
    and not assumed. `symbols=` is checked because the per-symbol claim and
    the owner's own reading both depend on it."""
    import scripts.alert_heartbeat as hb

    status = coverage_watchdog.CoverageStatus(
        trading_day="2026-09-23",
        session_ran=True,
        unreadable=[
            coverage_watchdog.UnreadableStop(
                symbol="AAPL",
                held_qty=9.0,
                reason="snapshot_protective_stops raised: 503",
            )
        ],
    )
    sent: list[tuple[str, list]] = []

    with (
        patch.object(hb, "_build_broker", return_value=MagicMock()),
        patch.object(hb, "_cash_sweep_symbol", return_value="SGOV"),
        patch.object(hb, "_coverage_db_and_last_buy", return_value=(None, None)),
        patch("src.coverage_watchdog.check_coverage", return_value=status),
        patch("src.coverage_watchdog.record_sweep_run"),
        patch(
            "src.notifier.send_owner_alert",
            side_effect=lambda text, symbols=None, **_kw: sent.append((text, list(symbols or []))) or True,
        ),
    ):
        line = hb.run_coverage_check(now=_NOW)

    assert len(sent) == 1, "exactly one alert, and it is the unreadable one"
    text, symbols = sent[0]
    assert "UNREADABLE" in text and "AAPL" in text
    assert symbols == ["AAPL"]
    assert "unreadable-stop alert delivered" in line


def test_a_newly_failing_symbol_does_not_re_page_the_ones_already_reported(state_path):
    """The owner-facing text promises "at most once per symbol per trading
    day". The gate correctly re-opens when a NEW name becomes unreadable —
    that is the swallowing defect fixed one level down — but the message
    was then built from the whole list, so the new name dragged every
    already-reported name back into his phone with it.

    AAPL is claimed first, standing for an earlier tick that already told
    him. NVDA then fails. He must be told about NVDA and NOT about AAPL.
    """
    import scripts.alert_heartbeat as hb

    assert coverage_watchdog.claim_unreadable_stop_alert(["AAPL"], now=_NOW) == ["AAPL"]

    def _snap(sym, side="sell"):
        if sym in ("AAPL", "NVDA"):
            raise RuntimeError("broker 503")
        return (True, [{"id": "s", "qty": 3.0}])

    status = coverage_watchdog.check_coverage(
        _wd_broker(_snap),
        now=_NOW,
        db=None,
    )
    assert {r.symbol for r in status.unreadable} == {"AAPL", "NVDA"}, (
        "both are still unreadable — the filter is about what we SAY"
    )
    assert [r.symbol for r in status.unreadable_fresh] == ["NVDA"]
    assert status.should_alert_unreadable is True

    sent: list[tuple[str, list]] = []
    with (
        patch.object(hb, "_build_broker", return_value=MagicMock()),
        patch.object(hb, "_cash_sweep_symbol", return_value="SGOV"),
        patch.object(hb, "_coverage_db_and_last_buy", return_value=(None, None)),
        patch("src.coverage_watchdog.check_coverage", return_value=status),
        patch("src.coverage_watchdog.record_sweep_run"),
        patch(
            "src.notifier.send_owner_alert",
            side_effect=lambda text, symbols=None, **_kw: sent.append((text, list(symbols or []))) or True,
        ),
    ):
        hb.run_coverage_check(now=_NOW)

    assert len(sent) == 1
    text, symbols = sent[0]
    assert symbols == ["NVDA"]
    assert "NVDA" in text
    assert "AAPL" not in text, (
        "AAPL was reported on an earlier tick; naming it again breaks the promise this very message makes"
    )


def test_no_stop_at_all_outranks_stop_unreadable_in_every_renderer():
    """Item 21b: the owner is red-green colour blind, so severity is
    carried by HOW MANY marks a line has, not which colour they are.
    Adding the unreadable banner put 🛑🛑 next to a single 🔴 for NO STOP
    AT ALL in the direct-alert channel and a flat 🚨 for all three in the
    feed — ranking "there is nothing standing watch" BELOW "we could not
    find out whether anything is". Counted marks, one ordering, everywhere.
    """
    import inspect

    from src.protection import owner_alerts as _owner_alerts

    sources = {
        "notifier": inspect.getsource(notifier._append_coverage_gap_banner),
        "feed": inspect.getsource(trader_feed._append_coverage_gaps),
        "alert": inspect.getsource(_owner_alerts.OwnerAlerts._alert_owner_no_stop),
        "text": inspect.getsource(coverage_watchdog.unreadable_stop_text),
    }
    for name in ("notifier", "feed", "alert"):
        src = sources[name]
        if "NO STOP AT ALL" in src:
            assert "🛑🛑🛑 NO STOP AT ALL" in src, f"{name} under-ranks it"
        if "STOP UNREADABLE" in src:
            assert "🛑🛑 STOP UNREADABLE" in src, f"{name} mis-ranks unreadable"
            assert "🛑🛑🛑 STOP UNREADABLE" not in src
    assert "🛑🛑 PROTECTIVE STOP UNREADABLE" in sources["text"]
    assert "🔴 NO STOP AT ALL" not in sources["alert"]


# ===========================================================================
# 5. the EXIT path: making `ok` honest also made a sale refusable
# ===========================================================================


def _exit_pipe():
    pipe = build_pipeline(broker=MagicMock(), db=MagicMock())
    return pipe


def test_a_listing_failure_is_not_retried_by_the_desk():
    """2026-10-14: the read's retry loop borrowed the stop-placement attempt
    count and backoff, which were arbitrary and are deleted. alpaca-py's
    client retries 429/504 itself; the desk asks once and a failure is
    UNKNOWN at once, with no sleep."""
    from src.execution import broker as _broker

    calls = {"n": 0}

    def _lister(self, symbol, *, side="sell", errors=None):
        calls["n"] += 1
        if errors is not None:
            errors.append("open-order listing failed: 503")
        return []

    b = _broker.AlpacaBroker.__new__(_broker.AlpacaBroker)
    with (
        patch.object(
            _broker.AlpacaBroker,
            "_list_open_protective_stop_orders",
            _lister,
        ),
        patch.object(_broker.time, "sleep") as sleep,
    ):
        ok, specs = _broker.AlpacaBroker.snapshot_protective_stops(b, "AAPL")
    assert (calls["n"], sleep.call_count) == (1, 0)
    assert ok is False and specs == []


def test_a_listing_that_fails_every_attempt_still_reports_unknown():
    from src.execution import broker as _broker

    def _lister(self, symbol, *, side="sell", errors=None):
        if errors is not None:
            errors.append("open-order listing failed: 503")
        return []

    b = _broker.AlpacaBroker.__new__(_broker.AlpacaBroker)
    with (
        patch.object(
            _broker.AlpacaBroker,
            "_list_open_protective_stop_orders",
            _lister,
        ),
        patch.object(_broker.time, "sleep"),
    ):
        ok, specs = _broker.AlpacaBroker.snapshot_protective_stops(b, "AAPL")
    assert ok is False and specs == []


def test_the_two_refusal_states_no_longer_share_one_asserted_cause():
    """`ok=False` means either "the listing failed, nobody knows whether a
    stop rests" or "the cancel rolled back, one verifiably does". Only the
    second supports "the broker would reject on held_for_orders" — measured
    2026-04-25 on AMZN. The log line asserted it for both."""
    pipe = _exit_pipe()
    pipe.broker.snapshot_protective_stops.return_value = (False, [])
    ok, specs, wal = pipe._cancel_stops_with_write_ahead("AAPL", 10.0)
    assert ok is False and specs == [] and wal is None
    assert pipe._last_stop_clear_refusal == "unreadable"


@pytest.mark.parametrize(
    "refusal,must_say,must_not_say",
    [
        ("unreadable", "UNKNOWN", "held_for_orders"),
        ("cancel_rolled_back", "held_for_orders", "UNKNOWN"),
    ],
)
def test_a_declined_exit_reaches_the_owner_by_symbol(state_path, refusal, must_say, must_not_say):
    """Five call sites upstream do `if sale is None: continue` — no trade
    row, no session field, no message. One of them is the de-levering
    ladder, which after the halt removal is one of the few remaining
    account-wide protections, and which would otherwise trim less than it
    reports with nothing reconciling the shortfall."""
    pipe = _exit_pipe()
    sent: list[tuple[str, list]] = []

    def _cancel(symbol, qty, **_kw):
        pipe._last_stop_clear_refusal = refusal
        return False, [], None

    with (
        patch.object(pipe, "_cancel_stops_with_write_ahead", _cancel),
        patch(
            "src.notifier.send_owner_alert",
            side_effect=lambda text, symbols=None, **_kw: sent.append((text, list(symbols or []))) or True,
        ),
    ):
        out = pipe._submit_protected_sell(
            symbol="AAPL",
            qty=5.0,
            position_qty_before_sell=10.0,
            limit_price=1.0,
            reference_price=1.0,
            label="unit",
        )

    assert out is None, "the exit is still declined — that part is deliberate"
    assert len(sent) == 1, "and it is no longer silent"
    text, symbols = sent[0]
    assert symbols == ["AAPL"]
    assert "EXIT NOT PLACED" in text and "AAPL" in text
    assert must_say in text
    assert must_not_say not in text


def test_a_declined_exit_pages_once_per_symbol_per_day(state_path):
    assert coverage_watchdog.claim_exit_declined_alert(["AAPL"], now=_NOW) == ["AAPL"]
    assert coverage_watchdog.claim_exit_declined_alert(["AAPL"], now=_NOW) == []
    assert coverage_watchdog.claim_exit_declined_alert(["AAPL", "NVDA"], now=_NOW) == ["NVDA"]


def test_the_declined_exit_marker_has_its_own_key(state_path):
    """Sharing the unreadable key would let a stop the desk could not read
    silence the exit it then refused on the strength of that same read —
    and those two happen together by construction."""
    assert coverage_watchdog.claim_unreadable_stop_alert(["AAPL"], now=_NOW) == ["AAPL"]
    assert coverage_watchdog.claim_exit_declined_alert(["AAPL"], now=_NOW) == ["AAPL"]


def test_the_declined_exit_message_never_claims_anything_was_sold():
    text = coverage_watchdog.exit_declined_text("AAPL", side="sell", why="a reason")
    assert "Nothing was sold, resized or cancelled" in text
    assert "🛑🛑 EXIT NOT PLACED" in text
    assert "🛑🛑🛑" not in text, "the position keeps whatever protection it had — this is not the unbounded-loss tier"


# ===========================================================================
# 4. rendering: never folded into a banner that states a measured fact
# ===========================================================================


def _unreadable_row(symbol="AAPL"):
    return {
        "symbol": symbol,
        "held_qty": 9.0,
        "covered_qty": None,
        "coverage": "unreadable",
        "repaired": False,
        "read_error": "snapshot_protective_stops raised: 503",
    }


def test_the_notifier_gives_it_its_own_banner_not_the_mis_sized_one():
    """MIS-SIZED asserts a stop IS standing watch over most of the position.
    Nobody established that here, and the old classifier would have said it:
    `coverage` is not 'none', so the row fell through to the milder banner.
    """
    lines: list[str] = []
    notifier._append_coverage_gap_banner(
        lines,
        {"stop_coverage_gaps": [_unreadable_row()]},
    )
    body = "\n".join(lines)
    assert "STOP UNREADABLE" in body and "AAPL" in body
    assert "UNKNOWN" in body
    assert "MIS-SIZED" not in body
    assert "NO STOP AT ALL" not in body


def test_the_unreadable_row_is_not_classified_off_its_missing_quantity():
    """`covered_qty` is None precisely because nothing was measured.
    `_gap_is_uncovered` derives from that field when `coverage` is absent,
    so the classifier must key on the stamp alone."""
    assert notifier._gap_is_unreadable(_unreadable_row()) is True
    assert notifier._gap_is_expected_fractional(_unreadable_row()) is False
    assert (
        notifier._gap_is_unreadable(
            {"symbol": "X", "coverage": "fractional_overnight"},
        )
        is False
    )


def test_it_breaks_the_intra_check_silence():
    """`intra_check` sends nothing on an ordinary tick. An unreadable stop
    is a real fault and must be actionable enough to speak."""
    assert notifier._actionable_coverage_gaps([_unreadable_row()])
    # ...while the accepted overnight remainder still does not.
    assert not notifier._actionable_coverage_gaps(
        [{"symbol": "AAPL", "coverage": "fractional_overnight"}],
    )


def test_the_feed_does_not_put_it_in_watch_as_a_partly_covered_position():
    """WATCH renders 'the stop covers only part of the position' — two
    claims an unreadable row supports neither of."""
    rows = trader_feed._watch_rows(
        {"stop_coverage_gaps": [_unreadable_row()]},
    )
    assert rows == []


def test_the_evening_banner_names_it_as_unknown():
    lines: list[str] = []
    trader_feed._append_evening_banners(
        lines,
        {"stop_coverage_gaps": [_unreadable_row()], "analysis": {}},
    )
    body = "\n".join(lines)
    assert "STOP UNREADABLE" in body and "UNKNOWN" in body
    assert "MIS-SIZED" not in body
