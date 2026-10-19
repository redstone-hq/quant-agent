"""Guards for the owner-facing defects under `docs/WORK.md` item 89 that
can mislead a trading decision — not the thirteen clarity ones.

The owner has a standing preference against test-writing. These exist
anyway, and only these, because each one pins a message that could have
moved real money: a plan discarded with no notification, a header that
disagreed with the list beneath it, an entry reason reported as missing
while it sat in the records, and a rule cited by a number whose text said
the opposite.

Every test here drives the real formatter over a real SQLite file. No
broker is involved; where one would be, the check is on `isinstance`, not
on truthiness, because ~58 tests in this suite drive a MagicMock broker
and a MagicMock is always truthy.
"""

import json
import sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo

from src import trader_feed
from tests.test_trader_feed import (
    _agent_log,
    _evidence,
    _make_db,
    _pin_clock,
    _trade,
)

_ET = ZoneInfo("America/New_York")
# Deliberately not intra_check's hourly-checkpoint minute and not 9:30 —
# see `tests/test_trader_feed.py`'s `_QUIET_TICK_TIME` for why every
# "sends nothing" assertion must pin the clock. Do not unfreeze this.
_QUIET_TICK_TIME = datetime(2026, 9, 17, 10, 45, tzinfo=_ET)
_DECISION_TIME = datetime(2026, 9, 17, 9, 40, tzinfo=_ET)


def _pipeline_event(db, run_id, symbol, payload):
    """One `pipeline_event` evidence row, exactly as
    `src.pipeline_stages._record_pipeline_event` writes it."""
    _evidence(db, run_id, "pipeline", "pipeline_event", payload, symbol=symbol)


def _position(db, symbol, qty=10, entry=100.0, price=110.0):
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO positions"
        "(symbol, qty, avg_entry, current_price, market_value, unrealized_pnl) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (symbol, qty, entry, price, qty * price, qty * (price - entry)),
    )
    conn.commit()
    conn.close()


# --- defect 6: a whole trade plan discarded, and no message sent at all ---


def test_a_plan_the_constructor_dropped_is_reported_not_silently_lost(
    tmp_path,
    monkeypatch,
):
    """Item 89 defect 6. The constructor ended a plan on size alone before
    any order existed. It left a durable per-symbol row and reached NO
    message: no trade row, no execution skip, no risk verdict, so the
    session read "orders: 0" with nothing to explain it and the owner
    could not know a decision had been made."""
    db = _make_db(tmp_path, monkeypatch)
    _pin_clock(monkeypatch, _DECISION_TIME)
    run = "run-silent-drop"
    _agent_log(db, run, "portfolio_manager", "1 target")
    _evidence(
        db,
        run,
        "portfolio_manager",
        "proposed_order",
        {"action": "BUY", "symbol": "NVDA", "allocation_pct": 0.2},
        symbol="NVDA",
    )
    _pipeline_event(
        db,
        run,
        "NVDA",
        {
            "stage": "deterministic_gate",
            "outcome": "blocked",
            "reason": "constructor_refused",
            "refusal": "delta_below_min_trade_weight",
            "detail": (
                "the desk decided to open this but the position it asked for "
                "was 0.20% of the account, and the desk does not place a new "
                "trade smaller than 0.50% of the account."
            ),
        },
    )

    msg = trader_feed.format_session_result(
        "morning",
        {"status": "no_trades", "run_id": run, "orders": []},
        12.0,
    )

    # NOT TAKEN, not BLOCKED / FAILED (2026-09-23). The minimum trade size
    # is one of the three cases the owner named when he said a normal
    # operating state must not be reported as an error: the desk declined
    # this on a standing rule, nothing broke, and the header says NO TRADE.
    # What must NOT change is that the drop still reaches the message at
    # all, with its reason — that is what this test was written for.
    assert "<b>🚫 NOT TAKEN</b>" in msg
    assert msg.splitlines()[0].endswith("· NO TRADE")
    assert "NVDA" in msg
    assert "Stopped by the desk before an order was placed" in msg
    assert "0.20% of the account" in msg
    # The internal refusal code is data, not language.
    assert "delta_below_min_trade_weight" not in msg


def test_a_dropped_plan_with_no_recorded_reason_says_so_and_invents_nothing(
    tmp_path,
    monkeypatch,
):
    db = _make_db(tmp_path, monkeypatch)
    _pin_clock(monkeypatch, _DECISION_TIME)
    run = "run-drop-no-reason"
    _agent_log(db, run, "portfolio_manager", "1 target")
    _pipeline_event(
        db,
        run,
        "AMD",
        {
            "stage": "deterministic_gate",
            "outcome": "blocked",
            "reason": "constructor_dropped",
            "detail": "",
        },
    )

    msg = trader_feed.format_session_result(
        "morning",
        {"status": "no_trades", "run_id": run, "orders": []},
        9.0,
    )

    assert "did not record why" in msg


def test_a_data_fault_is_not_reported_as_a_dropped_plan(tmp_path, monkeypatch):
    """`unmeasurable` is not a judgement on a trade — it is an input the
    desk failed to obtain, and it pages the owner down its own path. It
    must not appear as though the desk decided something."""
    db = _make_db(tmp_path, monkeypatch)
    _pin_clock(monkeypatch, _DECISION_TIME)
    run = "run-fault"
    _agent_log(db, run, "portfolio_manager", "1 target")
    _pipeline_event(
        db,
        run,
        "TSLA",
        {
            "stage": "deterministic_gate",
            "outcome": "unmeasurable",
            "reason": "data_fault",
            "fault": "no_atr",
            "detail": "no volatility reading available",
        },
    )

    msg = trader_feed.format_session_result(
        "morning",
        {"status": "no_trades", "run_id": run, "orders": []},
        9.0,
    )

    assert "Stopped by the desk before an order was placed" not in msg


def test_a_dropped_plan_does_not_make_a_quiet_half_hour_tick_speak(
    tmp_path,
    monkeypatch,
):
    """The owner's silence rule is ratified: one message an hour for
    oversight, and a quiet half-hour tick sends NOTHING. A constructor
    refusal may only add lines to a message that was already going out —
    if it causes one, the fix is worse than the defect it closes."""
    db = _make_db(tmp_path, monkeypatch)
    _pin_clock(monkeypatch, _QUIET_TICK_TIME)
    run = "run-quiet"
    _pipeline_event(
        db,
        run,
        "NVDA",
        {
            "stage": "deterministic_gate",
            "outcome": "blocked",
            "reason": "constructor_refused",
            "refusal": "delta_below_min_trade_weight",
            "detail": "below the minimum trade size",
        },
    )

    msg = trader_feed.format_session_result(
        "intra_check",
        {
            "status": "ok",
            "run_id": run,
            "intraday_scan": {"status": "no_opportunity", "run_id": run},
        },
        3.0,
    )

    assert msg is None


# --- defect 5: raw internal tokens printed to the owner word for word ---


def test_an_order_that_never_filled_is_explained_in_words_not_a_status_token(
    tmp_path,
    monkeypatch,
):
    db = _make_db(tmp_path, monkeypatch)
    _pin_clock(monkeypatch, _DECISION_TIME)
    run = "run-stalled"
    _agent_log(db, run, "portfolio_manager", "1 order")
    _trade(db, run, "AMD", "BUY", qty=3, price=550.0, status="canceled")

    msg = trader_feed.format_session_result(
        "morning",
        {"status": "no_trades", "run_id": run, "orders": []},
        11.0,
    )

    assert "the order was cancelled before anything filled" in msg
    assert "status: canceled" not in msg


def test_an_unrecognised_status_is_described_never_pasted_or_guessed_at():
    """Inventing a friendly meaning for a token nobody mapped would be
    inventing a trading fact. Saying there is no plain wording for it is
    the honest answer.

    2026-09-24: the raw token must still be KEPT, not dropped — the same
    treatment `notifier.humanize_status` already gives an unmapped status
    ("its own code for it, kept for the record, is ..."). Dropping it here
    made an unmapped state unrecoverable from the message; the fix is to
    label it as the desk's own code, never to paraphrase it as a fact.
    """
    text = trader_feed._order_end_plain("some_new_alpaca_state")
    assert "some_new_alpaca_state" in text
    assert "no plain wording" in text


def test_the_underlying_fault_text_is_kept_but_labelled_as_machine_output():
    text = trader_feed._machine_detail("KeyError: 'targets'")
    assert "KeyError: 'targets'" in text
    assert "Machine fault text" in text


def test_an_unmapped_fill_state_keeps_its_raw_token_too():
    """Same defect as `_order_end_plain` above, in the sibling helper shown
    ALONGSIDE an order line: dropping the broker's own token for a state
    nobody mapped made it unrecoverable from the message."""
    text = trader_feed._fill_state_plain("some_new_alpaca_fill_state")
    assert "some_new_alpaca_fill_state" in text
    assert "not recorded in plain words" in text


def test_a_resting_partially_filled_order_is_not_reported_as_failed():
    """2026-09-24: `_trade_reached_broker` only recognised
    filled/submitted/pending_submit as "reached the broker" — a live,
    working `partially_filled` order (the remainder still resting) fell
    into the same bucket as a genuine terminal failure, so
    `_outcome_word` reported a false FAILED for an order that was in fact
    live and protected."""
    assert trader_feed._trade_reached_broker("partially_filled") is True
    assert trader_feed._trade_reached_broker("accepted") is True
    assert trader_feed._trade_reached_broker("new") is True
    assert trader_feed._trade_reached_broker("held") is True
    assert trader_feed._trade_reached_broker("pending_new") is True
    # Genuine terminal failures must still be treated as not-reached.
    assert trader_feed._trade_reached_broker("canceled") is False
    assert trader_feed._trade_reached_broker("rejected") is False


# --- defect 2: a header computed independently of the list beneath it ---


def test_the_review_header_count_matches_the_positions_it_lists(
    tmp_path,
    monkeypatch,
):
    """Item 89 defect 2. `run_position_review` reports `positions` as the
    count from the START of the session, before its own exits ran and
    before the closing broker re-sync; the HELD block reads the book
    AFTER all of it. On any session that sold something the header
    asserted one number over a list naming a different set."""
    db = _make_db(tmp_path, monkeypatch)
    _pin_clock(monkeypatch, _DECISION_TIME)
    run = "run-review"
    _agent_log(db, run, "position_reviewer", "reviewed")
    for symbol in ("AAPL", "MSFT"):
        _position(db, symbol)

    msg = trader_feed.format_session_result(
        "midday",
        {
            "status": "reviewed",
            "run_id": run,
            "orders": [],
            # Three at the start of the session; one was sold.
            "positions": 3,
            "review": {"risk_level": "moderate", "actions": []},
        },
        20.0,
    )

    held_block = msg.split("HELD (")[1]
    assert held_block.startswith("2)")
    assert "2 position(s) held now" in msg
    # The pre-session number is not discarded; it is explained.
    assert "3 at the start of this session" in msg
    assert "3 position(s)" not in msg


def test_the_review_header_says_nothing_extra_when_nothing_changed(
    tmp_path,
    monkeypatch,
):
    db = _make_db(tmp_path, monkeypatch)
    _pin_clock(monkeypatch, _DECISION_TIME)
    run = "run-review-quiet"
    _agent_log(db, run, "position_reviewer", "reviewed")
    _position(db, "AAPL")

    msg = trader_feed.format_session_result(
        "midday",
        {
            "status": "reviewed",
            "run_id": run,
            "orders": [],
            "positions": 1,
            "review": {"risk_level": "low", "actions": []},
        },
        20.0,
    )

    assert "1 position(s) held now" in msg
    assert "at the start of this session" not in msg


# --- defect 4: a rule cited by number whose text says the opposite ---


def test_no_spec_section_number_is_cited_in_the_refusal_the_owner_reads():
    """Item 89 defect 4. The agreement-net refusal told the owner "§9.4
    refuses a net at or below zero". `docs/QAMC_REMEDIATION_SPEC.md` §9.4
    is about agreement EARNING SIZE, states no refusal, and the sizing
    half it does state was retired on 2026-09-14. A section number in
    owner-facing prose is a promise about a separate document that
    nothing checks, so the rule is now stated rather than cited."""
    import inspect

    from src.portfolio_constructor import PortfolioConstructor

    source = inspect.getsource(PortfolioConstructor._plan_risk_targets)
    refusal_strings = [line for line in source.splitlines() if "net at or below" in line or "position nobody backs" in line]
    assert refusal_strings, "the agreement-net refusal text moved; re-point this guard"
    for line in refusal_strings:
        assert "§9.4" not in line
        assert "§" not in line


def test_the_dropped_plan_sentence_names_no_internal_bookkeeping():
    import inspect

    from src.portfolio_constructor import PortfolioConstructor

    source = inspect.getsource(PortfolioConstructor._construct_orders_impl)
    assert "No existing position to record as a HOLD" not in source


# --- defect 3: an entry reason reported as unavailable while it is on file ---


def test_the_entry_reason_for_an_overnight_position_is_read_from_its_entry_row(
    tmp_path,
    monkeypatch,
):
    """Item 89 defect 3 (and item 104's eighth prompt defect — one root
    cause). The reviewer's only source for an entry thesis was a lookup
    bounded to TODAY, so every position held overnight rendered
    "unavailable". The reason was never missing; it was never searched
    for."""
    from src.agents.position_reviewer import PositionReviewerAgent
    from src.models import Position

    position = Position(
        symbol="RSG",
        qty=12,
        avg_entry=240.0,
        current_price=252.0,
        market_value=3024.0,
        unrealized_pnl=144.0,
        sector="Industrials",
    )
    agent = PositionReviewerAgent.__new__(PositionReviewerAgent)
    prompt = PositionReviewerAgent.build_user_message(
        agent,
        positions=[position],
        macro_summary={},
        cash_balance=1000.0,
        total_value=10000.0,
        # Bounded to today, and this position was not opened today.
        morning_trades=[],
        entry_context={
            "RSG": {
                "symbol": "RSG",
                "action": "BUY",
                "reasoning": "Cascade Investment open-market purchase",
            },
        },
    )

    assert "Cascade Investment open-market purchase" in prompt
    assert "position opened before today" not in prompt


def test_a_genuinely_blank_entry_reason_is_reported_as_not_recorded(
    tmp_path,
    monkeypatch,
):
    from src.agents.position_reviewer import PositionReviewerAgent
    from src.models import Position

    position = Position(
        symbol="RSG",
        qty=12,
        avg_entry=240.0,
        current_price=252.0,
        market_value=3024.0,
        unrealized_pnl=144.0,
        sector="Industrials",
    )
    agent = PositionReviewerAgent.__new__(PositionReviewerAgent)
    prompt = PositionReviewerAgent.build_user_message(
        agent,
        positions=[position],
        macro_summary={},
        cash_balance=1000.0,
        total_value=10000.0,
        morning_trades=[],
        entry_context={"RSG": {"symbol": "RSG", "action": "BUY", "reasoning": ""}},
    )

    assert "not recorded on this position's entry row" in prompt
    assert "do not invent one" in prompt


def test_the_entry_lookup_the_reviewer_is_given_is_not_bounded_to_today():
    """The fix is the LOOKUP, not the wording. `get_symbol_last_buy` is
    the existing date-unrestricted query — the same one the evening
    thesis-health context and the cockpit's "why do we hold this"
    endpoint already use — so no second lookup was written."""
    import inspect

    from src.storage.db import Database

    source = inspect.getsource(Database.get_symbol_last_buy)
    assert "today_only" not in source
    assert "_et_day_utc_bounds" not in source


def test_the_reviewer_reads_levels_from_live_data_not_from_the_entry_row():
    """Scope guard. The entry row also carries the stop and the target
    PINNED at entry; the take-profit became revisable on 2026-09-18, so
    letting a pinned level win over the live one would change what the
    reviewer DECIDES, not what the owner is told. `entry_context` must
    answer only "why is this held"."""
    import inspect

    from src.agents.position_reviewer import PositionReviewerAgent

    source = inspect.getsource(PositionReviewerAgent.build_user_message)
    assert 'entry_row.get("reasoning")' in source
    assert 'entry_row.get("stop_loss")' not in source
    assert 'entry_row.get("take_profit")' not in source
