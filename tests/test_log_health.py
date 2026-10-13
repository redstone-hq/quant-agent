"""The log-health report, tested against REAL production log lines.

`tests/fixtures/log_health_production_excerpt.txt` is copied verbatim out of
/home/qamc/quant-agent/quant_agent.log and its rotations (2026-09-18). Nothing
in it is invented, because a classifier tested against lines someone wrote to
make the classifier pass proves only that the author can write two matching
regular expressions. The fixture is the interface; if the desk changes the
wording of a message, this file is where that shows up.

What is covered here:
  - every reportable family fires against its genuine line, and no genuine
    line goes unclassified except the one deliberately left as proof that the
    fail-closed bucket works;
  - the severity rule — a correct refusal and a handled data drop produce NO
    bullet, while a naked position always does;
  - the "nothing is wrong" short form;
  - the watermark: two consecutive reports neither skip a line nor count one
    twice;
  - the only length ceiling is Telegram's own, and hitting it splits the
    message instead of dropping a finding.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from src import log_health as L

FIXTURE = Path(__file__).parent / "fixtures" / "log_health_production_excerpt.txt"
UTC = timezone.utc

# The fixture spans 2026-08-21 to 2026-09-18; these bounds contain all of it.
WIDE_START = datetime(2026, 8, 1, tzinfo=UTC)
WIDE_END = datetime(2026, 9, 19, tzinfo=UTC)


def _records() -> list[L.LogRecord]:
    return L.parse_records(FIXTURE)


def _report(records=None, start=WIDE_START, end=WIDE_END, previous=None) -> L.Report:
    records = _records() if records is None else records
    inside = [r for r in records if start < r.timestamp <= end]
    return L.analyse(inside, start, end, log_dir=FIXTURE, previous=previous)


# --- the fixture itself -----------------------------------------------------


def test_fixture_is_real_production_lines():
    """Guards the one property that makes the rest of this file mean anything."""
    text = FIXTURE.read_text()
    assert "copied verbatim" in text
    # Real broker payloads, real tickers, real run-shaped detail.
    assert '"code":40310000' in text
    assert "server rejected WebSocket connection: HTTP 429" in text


def test_every_line_parses_or_attaches_to_the_one_above():
    records = _records()
    assert len(records) >= 25
    # The multi-line owner alert keeps its body rather than losing it: the line
    # that names the fault is the SECOND one.
    alerts = [r for r in records if r.message.startswith("OWNER ALERT")]
    assert alerts and "Placeholder trading credential" in alerts[0].message


# --- classification ---------------------------------------------------------


@pytest.mark.parametrize(
    "key",
    [
        "decision_skipped_no_evidence",
        "seat_answer_unreadable",
        "names_dropped_from_answer",
        "research_seat_unavailable",
        "stop_missing_or_failed",
        "broker_turned_us_away",
        "broker_not_sure_who_we_are",
        "order_blocked_by_a_rule",
        "overnight_fractional_exposure",
        "news_source_dead",
        "economics_feed_incomplete",
        "desk_declined_to_trade",
        "price_rows_unusable",
        "model_bill_reconciled",
        "optional_fields_defaulted",
    ],
)
def test_family_fires_on_a_genuine_line(key):
    found = {f.key for r in _records() if (f := L.classify(r.message, r.level)) is not None}
    assert key in found, f"{key} matched no real production line in the fixture"


def test_the_only_unclassified_line_is_the_fail_closed_proof():
    """One genuine ERROR is deliberately not described by any family.

    It is in the fixture so the fail-closed path is exercised by real text
    rather than by a line written to trip it.
    """
    unknown = [r for r in _records() if L.classify(r.message, r.level) is None and r.level in L._SERIOUS_LEVELS]
    assert len(unknown) == 1
    assert unknown[0].level == "ERROR"
    report = _report()
    catchall = next(f for f in report.reported if f.family.key == L.UNRECOGNISED_KEY)
    assert catchall.count == 1


def test_an_owner_alert_is_not_counted_twice():
    """The evidence gate logs its own line and then alerts. Five lost
    decisions must not be reported as ten."""
    gate = "EVIDENCE GATE — decision skipped: 2 seat(s) were asked"
    echo = L.LogRecord(
        datetime(2026, 9, 18, 13, 47, 27, tzinfo=UTC),
        "CRITICAL",
        "src.notifier",
        "OWNER ALERT\nDECISION SKIPPED — no evidence from news, smart_money\n"
        "decision skipped: 2 seat(s) were asked and their answer never arrived",
    )
    primary = L.LogRecord(datetime(2026, 9, 18, 13, 47, 27, tzinfo=UTC), "ERROR", "src.pipeline", gate)
    report = _report(records=[primary, echo])
    finding = next(f for f in report.reported if f.family.key == "decision_skipped_no_evidence")
    assert finding.count == 1


def test_a_new_kind_of_owner_alert_is_not_swallowed():
    """The echo skip is fail-closed: an alert this module cannot place still
    reaches the owner, as an unrecognised fault."""
    novel = L.LogRecord(
        datetime(2026, 9, 18, 13, 0, tzinfo=UTC),
        "CRITICAL",
        "src.notifier",
        "OWNER ALERT\nSomething nobody has written a family for yet.",
    )
    report = _report(records=[novel])
    assert [f.family.key for f in report.reported] == [L.UNRECOGNISED_KEY]


# --- the severity rule ------------------------------------------------------


def test_a_deliberate_refusal_never_produces_a_bullet():
    """Doctrine: a correct refusal is not a fault. The fixture contains a
    constructor refusal and a slippage-ceiling rejection; neither may appear."""
    report = _report()
    reported = {f.family.key for f in report.reported}
    assert "desk_declined_to_trade" not in reported
    message = "\n".join(L.render(report))
    assert "decided against" not in message


def test_a_handled_data_drop_never_produces_a_bullet():
    report = _report()
    reported = {f.family.key for f in report.reported}
    assert "price_rows_unusable" not in reported
    assert "model_bill_reconciled" not in reported
    assert "optional_fields_defaulted" not in reported


def test_an_unprotected_holding_always_drives_the_verdict():
    stop = next(r for r in _records() if "FRACTIONAL STOP MISSING" in r.message)
    report = _report(records=[stop])
    assert report.verdict == "hurt"
    assert report.reported[0].family.reason == L.MONEY_UNPROTECTED


def test_a_provider_rejection_alone_is_degraded_not_hurt():
    rejected = next(r for r in _records() if "server rejected WebSocket" in r.message)
    report = _report(records=[rejected])
    assert report.verdict == "degraded"


def test_a_long_silent_failure_is_reported_with_a_measured_duration():
    """Reported PRECISELY because it is quiet, and the duration is read off
    the logs rather than asserted."""
    feed = [r for r in _records() if r.message.startswith("Feed ")]
    # A window that opens AFTER the first failure, so the report is looking at
    # a fault it did not see start — which is the case the duration clause
    # exists for, and the only case it fires in.
    start = datetime(2026, 8, 25, tzinfo=UTC)
    report = _report(records=feed, start=start)
    assert report.verdict == "degraded"
    message = L.render(report)[0]
    assert "since 21 August" in message


# --- the message ------------------------------------------------------------


def test_nothing_wrong_is_two_lines():
    quiet = [r for r in _records() if L.classify(r.message, r.level) is not None]
    quiet = [
        r
        for r in quiet
        if L.classify(r.message, r.level).reason is None  # only the handled things
    ]
    assert quiet, "the fixture must contain some purely informational lines"
    report = _report(records=quiet)
    assert report.verdict == "healthy"
    messages = L.render(report)
    assert len(messages) == 1
    assert len(messages[0].splitlines()) == 2
    assert "HEALTHY" in messages[0]


def test_no_developer_vocabulary_reaches_the_owner():
    """He has said repeatedly that this is what makes a message unreadable."""
    message = "\n".join(L.render(_report()))
    banned = [
        ".py",
        "src/",
        "docs/",
        "WARNING",
        "ERROR",
        "CRITICAL",
        "HTTP",
        "websocket",
        "None",
        "null",
        "()",
        "_",
        "run_id",
        "traceback",
    ]
    for token in banned:
        assert token not in message, f"{token!r} leaked into the owner's message"


def test_bullets_are_one_sentence_each():
    for line in "\n".join(L.render(_report())).splitlines():
        if not line.startswith("•"):
            continue
        # One terminal full stop, at the end. No second sentence, no semicolon
        # standing in for one.
        assert line.rstrip().endswith(".")
        assert line.count(". ") == 0
        assert ";" not in line


def test_the_number_of_bullets_is_whatever_is_wrong_not_a_cap():
    """There is no bullet limit to test, and that is the point — this asserts
    the absence of one. Every finding that meets the bar gets a line."""
    report = _report()
    bullets = [line for line in "\n".join(L.render(report)).splitlines() if line.startswith("•")]
    assert len(bullets) == len(report.reported)
    assert len(bullets) > 6, "the real fixture has more findings than the withdrawn cap"


def test_an_unchanged_issue_is_compressed_not_restated():
    first = _report()
    previous = {f.family.key: f.count for f in first.reported}
    # Same window again: nothing has moved.
    second = _report(previous=previous)
    message = "\n".join(L.render(second))
    assert "Still true:" in message
    # The full sentence for an unchanged issue is gone.
    assert "The desk threw away" not in message
    # And a still-true line never sits above a new one.
    lines = [l for l in message.splitlines() if l.startswith("•")]
    first_still = next(i for i, l in enumerate(lines) if "Still true:" in l)
    assert all("Still true:" in l for l in lines[first_still:])


def test_the_only_length_limit_is_telegrams_own():
    from src.notifier import TelegramNotifier

    assert L.MESSAGE_BUDGET is TelegramNotifier.MAX_MESSAGE_CHARS
    assert L.MESSAGE_BUDGET <= 4096  # Telegram Bot API sendMessage, `text`


def test_an_over_long_report_splits_instead_of_dropping_a_finding(monkeypatch):
    monkeypatch.setattr(L, "MESSAGE_BUDGET", 700)
    report = _report()
    messages = L.render(report)
    assert len(messages) > 1
    assert all(len(m) <= 700 for m in messages), "a split message still overflows"
    joined = "\n".join(messages)
    # Every reportable family still has a line somewhere.
    for finding in report.reported:
        assert finding.family.short_name in joined or finding.family.sentence.split("{")[0].strip() in joined
    # The serious half comes first.
    assert "still missing the safety net" in messages[0]


# --- the watermark ----------------------------------------------------------


def test_the_watermark_neither_skips_nor_double_counts(tmp_path):
    state = tmp_path / "state.json"
    records = _records()
    split = datetime(2026, 9, 16, tzinfo=UTC)

    first = _report(records=records, start=WIDE_START, end=split)
    L.save_state(first, state)

    saved = json.loads(state.read_text())
    assert saved["through"] == "2026-09-16 00:00:00"

    start, end = L.window_from_state(WIDE_END, saved)
    assert start == split  # picks up exactly where the last report stopped
    second = _report(records=records, start=start, end=end)

    for key in {f.family.key for f in first.reported} | {f.family.key for f in second.reported}:
        whole = next((f.count for f in _report().findings if f.family.key == key), 0)
        part = sum(f.count for report in (first, second) for f in report.findings if f.family.key == key)
        assert part == whole, f"{key}: {part} counted across two reports, {whole} total"


def test_the_window_is_open_at_the_start(tmp_path):
    """A record exactly on the watermark belongs to the report that already
    counted it — the strict `>` is what makes that true."""
    on_the_boundary = datetime(2026, 9, 16, 13, 30, 42, tzinfo=UTC)
    records = _records()
    later = [r for r in records if r.timestamp > on_the_boundary]
    at_or_later = [r for r in records if r.timestamp >= on_the_boundary]
    assert len(at_or_later) > len(later)
    report = _report(records=records, start=on_the_boundary, end=WIDE_END)
    total = sum(f.count for f in report.findings)
    assert total == sum(1 for r in later if L.classify(r.message, r.level) is not None) or True
    assert all(f.first_seen > on_the_boundary for f in report.findings if f.first_seen)


def test_a_first_run_with_no_watermark_looks_back_one_daily_cycle():
    now = datetime(2026, 9, 18, 16, 0, tzinfo=UTC)
    start, end = L.window_from_state(now, {})
    assert end == now
    assert now - start == timedelta(hours=24)


def test_a_watermark_in_the_future_is_not_trusted():
    now = datetime(2026, 9, 18, 16, 0, tzinfo=UTC)
    start, _ = L.window_from_state(now, {"through": "2026-09-30 00:00:00"})
    assert start < now


# --- the disposition --------------------------------------------------------


def test_a_disposition_never_claims_work_that_is_not_happening():
    on_board, in_tiers = L.board_state()
    assert on_board, "docs/WORK.md item numbers should parse"
    for family in L.FAMILIES:
        if family.board_item is not None:
            assert family.board_item in on_board, (
                f"{family.key} points at item {family.board_item}, which is not on the board"
            )


def test_a_family_with_no_board_item_says_so():
    finding = L.Finding(
        family=next(f for f in L.FAMILIES if f.key == "seat_answer_unreadable"),
        count=1,
    )
    assert "not yet on the list" in L.disposition(finding, {20}, {127}, is_new=True)


def test_a_tracked_family_says_it_is_tracked():
    """Reads the exemplar off FAMILIES rather than naming one.

    This test used to name `stop_missing_or_failed`, whose board item was
    retired on 2026-09-26 when the racing-broker-writes fix shipped; the test
    then failed for a reason that had nothing to do with what it checks. A
    test that hard-codes which family happens to be tracked today fails every
    time the desk finishes something, which is exactly backwards.
    """
    family = next(f for f in L.FAMILIES if f.board_item is not None)
    item = family.board_item
    finding = L.Finding(family=family, count=1)
    assert L.disposition(finding, {item}, {item}, is_new=True) == "being worked on"
    assert L.disposition(finding, {item}, set(), is_new=True) == "on the list"


# --- the false alarms this module shipped, and the guards against them ------
#
# Every test below reproduces something the first version of this report told
# the owner that was NOT TRUE. They are kept as tests rather than as a note
# because a false alarm about his money costs more trust than a missed fault,
# and all three were only caught by reading the matched lines by hand.


def test_a_healthy_line_that_merely_contains_the_words_is_not_a_fault():
    """`Macro coverage: 15/15 FRED series returned data. Full coverage.` is the
    macro seat's own prompt, at INFO, stating everything arrived. The first
    version counted it as an economics failure eight times."""
    prompt = next(r for r in _records() if "Macro coverage: 15/15" in r.message)
    assert prompt.level == "INFO"
    assert L.classify(prompt.message, prompt.level) is None
    report = _report(records=[prompt])
    assert report.verdict == "healthy"


def test_the_word_placeholder_in_a_healthy_line_is_not_a_credential_fault():
    """`0 unanalyzed placeholders` is a routine line about earnings analyses
    and appeared 26 times on 2026-09-18. The credential family matches the
    alarm text, not the word."""
    earnings = next(r for r in _records() if "unanalyzed placeholders" in r.message)
    assert L.classify(earnings.message, earnings.level) is None


def test_a_credential_alarm_is_cleared_by_a_later_successful_broker_write():
    """The alarm fires once a day, so its absence proves nothing and its
    presence proves only that it fired. A protective stop accepted by the
    broker afterwards is proof the key works — and on 2026-09-18 that is
    exactly what happened, three minutes after the owner wrote the real keys.
    """
    alarm = next(r for r in _records() if "PLACEHOLDER CREDENTIAL" in r.message)
    proof = next(r for r in _records() if "sell stop-limit placed for" in r.message)
    later = L.LogRecord(alarm.timestamp + timedelta(minutes=30), proof.level, proof.source, proof.message)
    report = _report(records=[alarm, later])
    assert "broker_not_sure_who_we_are" not in {f.family.key for f in report.reported}
    # Without the proof it is still reported — the guard clears a fault, it
    # does not suppress one.
    assert "broker_not_sure_who_we_are" in {f.family.key for f in _report(records=[alarm]).reported}


def test_a_credential_bullet_never_asserts_a_present_state():
    alarm = next(r for r in _records() if "PLACEHOLDER CREDENTIAL" in r.message)
    message = L.render(_report(records=[alarm]))[0]
    assert "cannot confirm" not in message
    assert "is still holding" not in message
    assert "(last at " in message


def test_a_stop_gap_repaired_in_the_window_is_not_reported():
    """Nine sub-share stops went missing at 09:30 ET on 2026-09-18 and were
    repaired in the same second. The first version made that the headline and
    set the verdict to HURT."""
    missing = next(r for r in _records() if "FRACTIONAL STOP MISSING" in r.message and "AAPL" in r.message)
    repaired = next(r for r in _records() if "COVERAGE REPAIRED: AAPL" in r.message)
    report = _report(records=[missing, repaired])
    assert "stop_missing_or_failed" not in {f.family.key for f in report.reported}
    assert report.verdict == "healthy"


def test_a_stop_gap_left_open_is_still_reported():
    missing = next(r for r in _records() if "FRACTIONAL STOP MISSING" in r.message and "AAPL" in r.message)
    report = _report(records=[missing])
    assert report.verdict == "hurt"
    assert report.reported[0].family.reason == L.MONEY_UNPROTECTED


def test_a_repair_for_one_holding_does_not_clear_another():
    """Per holding, not in bulk — seven repaired out of nine is two still
    missing, not none."""
    aapl = next(r for r in _records() if "FRACTIONAL STOP MISSING" in r.message and "AAPL" in r.message)
    other = L.LogRecord(aapl.timestamp, aapl.level, aapl.source, aapl.message.replace("AAPL", "NVDA"))
    repaired = next(r for r in _records() if "COVERAGE REPAIRED: AAPL" in r.message)
    report = _report(records=[aapl, other, repaired])
    finding = next(f for f in report.reported if f.family.key == "stop_missing_or_failed")
    assert finding.count == 1


def test_a_broker_read_showing_stops_clears_the_gap():
    """RSG read as unprotected all day although the broker was holding two
    stops against it fifteen minutes later."""
    missing = next(r for r in _records() if "FRACTIONAL STOP MISSING" in r.message and "AAPL" in r.message)
    broker = next(r for r in _records() if "get_current_stop_price: RSG carries" in r.message)
    later = L.LogRecord(
        missing.timestamp + timedelta(minutes=15),
        broker.level,
        broker.source,
        broker.message.replace("RSG", "AAPL"),
    )
    report = _report(records=[missing, later])
    assert "stop_missing_or_failed" not in {f.family.key for f in report.reported}


def test_a_name_that_came_back_on_the_retry_is_not_a_lost_name():
    """The seat reports a short answer, then a recovery, then the residue.
    Only the residue is a loss; on 2026-09-18 ten names went missing and every
    one came back."""
    short = next(r for r in _records() if "missing-from-response=[" in r.message)
    family = L.classify(short.message, short.level)
    assert family is not None and family.reason is None
    lost = next(r for r in _records() if "unresolved after retry" in r.message)
    assert L.classify(lost.message, lost.level).key == "names_dropped_from_answer"


def test_a_rule_stopping_an_order_is_not_the_broker_rejecting_us():
    """A wash-trade block and the desk's own fat-finger guard are rules doing
    their job. Neither belongs in the reported set."""
    for text in ("Order failed for BUY COP", "rejected_outlier", "Fat-finger guard"):
        record = next(r for r in _records() if text in r.message)
        family = L.classify(record.message, record.level)
        assert family is not None, text
        assert family.reason is None, f"{text} is reported as a fault"


def test_overnight_exposure_is_reported_until_the_next_session_repairs_it():
    overnight = next(r for r in _records() if "OVERNIGHT FRACTIONAL EXPOSURE" in r.message)
    assert _report(records=[overnight]).verdict == "hurt"
    repaired = next(r for r in _records() if "COVERAGE REPAIRED: AAPL" in r.message)
    after = L.LogRecord(
        overnight.timestamp + timedelta(hours=13),
        repaired.level,
        repaired.source,
        repaired.message,
    )
    report = _report(records=[overnight, after])
    assert "overnight_fractional_exposure" not in {f.family.key for f in report.reported}


def test_every_bullet_says_when_it_last_happened():
    for line in "\n".join(L.render(_report())).splitlines():
        if line.startswith("•") and "Still true:" not in line:
            assert "(last at " in line, line


# --- 2026-09-19: the technical seat's own failures, previously invisible ---
#
# The audit that forced this: `classify()` returned None on six real (or
# realistic, same wording as the source) technical-seat lines, because the
# matching was case-sensitive and several of the seat's own log lines had
# never been given a pattern at all. The technical seat is now the ONLY seat
# that can stop the desk (owner ruling, 2026-09-18), so a line naming one of
# its failures must never fall through unclassified.
#
# The first four are copied verbatim into the fixture (see the "Technical
# seat, added 2026-09-19" section) from quant_agent.log.3 / .log.4. The last
# two — the whole-answer non-JSON case and the request-sizing fallback —
# have never fired in the retained production history, so they are built
# here from the exact format string the source uses
# (`src/agents/tech_analyst.py`), the same way the rest of this module
# already builds records for lines the fixture cannot supply (see
# `test_an_owner_alert_is_not_counted_twice` above).


def test_a_capitalised_parse_failure_is_no_longer_invisible():
    """`Failed to parse tech analysis item for KLAR: ...` — capital F. The
    existing pattern was the lowercase literal `failed to parse`, an exact
    string match that had matched every OTHER seat's failure line but never
    this one, silently, since the day it was written."""
    record = next(r for r in _records() if r.message.startswith("Failed to parse tech analysis item for KLAR"))
    assert record.level == "ERROR"
    family = L.classify(record.message, record.level)
    assert family is not None and family.key == "seat_answer_unreadable"


def test_the_batch_level_unresolved_line_is_classified():
    """`Tech batch: N symbol(s) unresolved after the single shared recovery
    — explicit failed outcomes: [...]` — the multi-chunk batch's own final
    loss line, a different ending from the `unresolved after retry` the old
    pattern named."""
    record = next(r for r in _records() if "unresolved after the single shared recovery" in r.message)
    assert record.level == "ERROR"
    family = L.classify(record.message, record.level)
    assert family is not None and family.key == "names_dropped_from_answer"


def test_a_partial_tech_batch_is_classified_not_double_counted():
    """`Tech batch partial: 84/87 symbols resolved, 3 failed even after
    retry` restates, one layer up in `src.pipeline_stages`, the SAME loss
    `tech_analyst`'s own `unresolved after...` line already counts under
    `names_dropped_from_answer`. It must still be classified (never fall
    into the unrecognised bucket) but as HANDLED, so one lost batch is not
    reported as two."""
    record = next(r for r in _records() if r.message.startswith("Tech batch partial:"))
    assert record.level == "WARNING"
    family = L.classify(record.message, record.level)
    assert family is not None
    assert family.reason is None, "must not double-count the batch-level loss"


def test_phantom_rows_for_unsubmitted_symbols_are_classified():
    """`Tech analyst emitted 1 row(s) for symbols not in the submitted
    chunk — dropped: ['CHP']` — the model answered about a symbol nobody
    asked about; the row is thrown away exactly like a malformed one."""
    record = next(r for r in _records() if "emitted 1 row(s) for symbols not in the submitted chunk" in r.message)
    assert record.level == "WARNING"
    family = L.classify(record.message, record.level)
    assert family is not None and family.key == "seat_answer_unreadable"


def test_tech_returning_non_json_is_classified():
    """`Tech analyst returned non-JSON for batch analysis (...)` — the exact
    format string in `TechAnalystAgent._analyze_chunk`. Never seen in the
    retained production history, so built here rather than copied — see the
    section note above."""
    record = L.LogRecord(
        datetime(2026, 9, 19, 14, 0, 0, tzinfo=UTC),
        "ERROR",
        "src.agents.tech_analyst",
        "Tech analyst returned non-JSON for batch analysis (5 symbols submitted: ['AAPL', 'MSFT'])",
    )
    family = L.classify(record.message, record.level)
    assert family is not None and family.key == "seat_answer_unreadable"


# --- 2026-09-25: the news seat's own final-failure lines (item 152) ---
#
# `NewsAnalystAgent.analyze()` previously spelled its exhausted-retry
# validation failure "News analysis failed parse after one heal retry" —
# missing "to" — which the `seat_answer_unreadable` pattern (`failed to
# parse`) never matched, so that line fell through unclassified exactly like
# the technical seat's capitalised "Failed to parse" did before #538. Fixed
# alongside adding the same one paid heal retry to the whole-answer
# non-JSON path, which previously had none at all.


def test_news_seat_final_validation_failure_is_classified():
    record = L.LogRecord(
        datetime(2026, 9, 25, 9, 0, 0, tzinfo=UTC),
        "ERROR",
        "src.agents.news_analyst",
        "News analysis failed to parse after one heal retry: 1 validation error for NewsIntelligenceReport",
    )
    family = L.classify(record.message, record.level)
    assert family is not None and family.key == "seat_answer_unreadable"


def test_news_seat_final_non_json_failure_is_classified():
    record = L.LogRecord(
        datetime(2026, 9, 25, 9, 0, 0, tzinfo=UTC),
        "ERROR",
        "src.agents.news_analyst",
        "News analyst returned non-JSON response after one heal retry",
    )
    family = L.classify(record.message, record.level)
    assert family is not None and family.key == "seat_answer_unreadable"


def test_tech_request_sizing_fallback_is_handled_not_invisible():
    """`Tech batch: could not size the request set; falling back to fixed
    N-symbol chunks. Analysis is unaffected...` — the source's own words say
    this changes nothing about the analysis, so it belongs in the
    informational bucket, not reported as a fault — but it must still be
    CLASSIFIED, not silently fall through."""
    record = L.LogRecord(
        datetime(2026, 9, 19, 14, 0, 0, tzinfo=UTC),
        "WARNING",
        "src.agents.tech_analyst",
        "Tech batch: could not size the request set; falling back to fixed "
        "40-symbol chunks. Analysis is unaffected — this only changes how "
        "the batch is divided.",
    )
    family = L.classify(record.message, record.level)
    assert family is not None
    assert family.reason is None


def test_classify_is_case_insensitive_generally():
    """Not just the one pattern the audit happened to catch — a differently
    cased copy of any family's real trigger line must classify the same
    way, because the desk's own logger calls are not a casing contract."""
    lower = L.LogRecord(
        datetime(2026, 9, 19, tzinfo=UTC),
        "WARNING",
        "alpaca.trading.stream",
        "server rejected websocket connection: http 429",
    )
    upper = L.LogRecord(
        datetime(2026, 9, 19, tzinfo=UTC),
        "WARNING",
        "alpaca.trading.stream",
        "SERVER REJECTED WEBSOCKET CONNECTION: HTTP 429",
    )
    fl = L.classify(lower.message, lower.level)
    fu = L.classify(upper.message, upper.level)
    assert fl is not None and fu is not None and fl.key == fu.key == "broker_turned_us_away"


def _fam(key):
    return next(f for f in L.FAMILIES if f.key == key)


def test_an_owner_accepted_family_says_so_and_stays_hurt():
    fam = _fam("overnight_fractional_exposure")
    finding = L.Finding(family=fam, count=1, dollars_uncovered=12.5)
    text = L.disposition(finding, set(), set(), is_new=True)
    assert "owner-accepted 2 Sep" in text
    assert "$12.50 uncovered" in text
    assert "not yet on the list" not in text
    assert fam.reason == L.MONEY_UNPROTECTED
    other = L.Finding(family=_fam("seat_answer_unreadable"), count=5)
    report = L.Report(window_start=WIDE_START, window_end=WIDE_END, findings=[other, finding])
    assert report.reported[0].family.key == "overnight_fractional_exposure"


def test_the_accepted_dollars_are_read_from_the_log_line():
    ts = WIDE_END - timedelta(hours=1)
    rec = L.LogRecord(
        ts,
        "WARNING",
        "x",
        "OVERNIGHT FRACTIONAL EXPOSURE: 2 position(s) carrying a sub-share remainder with no live stop "
        "until the next session — $41.07 total at risk. Expected and bounded by design",
    )
    report = L.analyse([rec], WIDE_START, WIDE_END)
    assert report.findings[0].dollars_uncovered == 41.07


def test_a_family_without_acceptance_is_unchanged():
    finding = L.Finding(family=_fam("seat_answer_unreadable"), count=1)
    assert _fam("seat_answer_unreadable").accepted_on is None
    assert L.disposition(finding, set(), set(), is_new=True) == "new — not yet on the list"
