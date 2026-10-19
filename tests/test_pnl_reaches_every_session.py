"""The P&L figure must reach the message whenever the account was read —
and the sentence that replaces it must be TRUE when it did not.

Owner, 2026-09-23, verbatim: "why can't I see the P&L?" That morning's
message said:

    📈 Today's P&L: not available
    📊 Total P&L: not available
       This message was built without an account read, so there is no figure
       yet.

and then, four lines lower, printed the book it had just read. Two separate
defects in one block:

  1. The morning trading session held the account snapshot and simply never
     put the P&L keys on its result — only the position-review and
     intra-check happy paths did. Every other exit (no_trades,
     pm_agent_failure, paid_analysis_suspended, executed) dropped them.
  2. The explanation was asserted from the ABSENCE of those keys, so it
     claimed a cause it could not know. An explanation the code cannot
     prove is itself a defect (owner, standing).

These tests pin both: the figure travels with the account read, and the
sentence follows from the recorded reason, never from a guess.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from src import trader_feed
from tests.pipeline_factory import build_pipeline


def _pipeline_with_snapshot(total_value, last_equity, total=(None, None, None)):
    """A pipeline that has taken one account read and nothing else."""
    p = build_pipeline(_total_pnl_since_reset=lambda tv: total)
    p._record_account_snapshot(total_value, last_equity)
    return p


# --- 1. A trading session renders REAL figures -------------------------


def test_trading_session_result_carries_the_pnl_it_already_read():
    """The exact shape of the 2026-09-23 morning message: a `no_trades`
    result, which carried no P&L key at all."""
    p = _pipeline_with_snapshot(
        10_041.44,
        10_000.0,
        total=(74.90, 0.749, "2026-08-14"),
    )
    result = {"status": "no_trades", "orders": [], "run_id": "run-fccb2026"}

    p._attach_pnl(result)

    assert result["daily_pnl"] == pytest.approx(41.44)
    assert result["daily_return_pct"] == pytest.approx(0.4144, rel=1e-3)
    assert result["total_pnl"] == pytest.approx(74.90)
    assert "pnl_unavailable_reason" not in result


def test_trading_session_message_shows_the_figure_not_not_available():
    p = _pipeline_with_snapshot(
        10_041.44,
        10_000.0,
        total=(74.90, 0.749, "2026-08-14"),
    )
    result = {"status": "no_trades", "orders": [], "run_id": "r"}
    p._attach_pnl(result)

    lines = trader_feed._pnl_section_lines(result)

    assert "+$41.44" in lines[0]
    assert "not available" not in "\n".join(lines)
    assert not any("without an account read" in ln for ln in lines)


def test_the_dated_total_label_still_renders():
    """'Total P&L' stays dated — a bare "total" would read as "since the
    account began", which the 2026-09-02 liquidation makes untrue."""
    p = _pipeline_with_snapshot(
        10_041.44,
        10_000.0,
        total=(74.90, 0.749, "2026-08-14"),
    )
    result = {"status": "executed", "orders": [], "run_id": "r"}
    p._attach_pnl(result)

    lines = trader_feed._pnl_section_lines(result)

    assert "Total P&L since 2026-08-14:" in lines[1]


def test_a_figure_the_body_already_set_is_never_overwritten():
    """The position-review/intra happy paths compute it themselves; the
    attach must not become a second, competing basis."""
    p = _pipeline_with_snapshot(99_999.0, 1.0, total=(1.0, 1.0, "2026-01-01"))
    result = {"status": "ok", "daily_pnl": 5.0, "daily_return_pct": 0.5}

    p._attach_pnl(result)

    assert result["daily_pnl"] == 5.0
    assert "total_pnl" not in result


# --- 2. No snapshot: an honest reason, matching the actual cause -------


def test_a_run_that_never_read_the_account_says_exactly_that():
    p = build_pipeline(_last_account_snapshot=None)
    result = {"status": "market_holiday", "orders": [], "run_id": "r"}

    p._attach_pnl(result)
    lines = trader_feed._pnl_section_lines(result)

    assert result["pnl_unavailable_reason"] == "ended_before_account_read"
    assert "This run ended before the account was read" in lines[-1]


def test_account_read_but_no_prior_close_does_not_claim_a_missing_read():
    """The broker answered; what it did not give is a usable prior close.
    Claiming "no account read" here is the false-cause defect again."""
    p = _pipeline_with_snapshot(10_000.0, 0.0, total=(None, None, None))
    result = {"status": "executed", "orders": [], "run_id": "r"}

    p._attach_pnl(result)
    lines = trader_feed._pnl_section_lines(result)

    assert result["pnl_unavailable_reason"] == "no_prior_close"
    assert "prior-day close" in lines[-1]
    assert "without an account read" not in lines[-1]


def test_the_premarket_reader_still_says_without_an_account_read():
    """The one mode where that sentence is true keeps it — set by the
    session itself, not inferred by the renderer."""
    result = {
        "status": "preprocessed",
        "run_id": "r",
        "pnl_unavailable_reason": "no_account_read",
    }

    lines = trader_feed._pnl_section_lines(result)

    assert "without an account read" in lines[-1]


def test_an_unlabelled_result_claims_no_cause_at_all():
    """No recorded reason means the code knows nothing about why — so it
    says nothing about why."""
    lines = trader_feed._pnl_section_lines({"status": "something", "run_id": "r"})

    assert lines[-1].strip() == "No P&L figure was recorded with this message."


# --- 3. Wrappers actually wire it in -----------------------------------


@pytest.mark.parametrize(
    "wrapper,body_name,kwargs",
    [
        ("run_morning", "_run_morning_body", {}),
        ("run_position_review", "_run_position_review_body", {"session_type": "midday"}),
        ("run_intra_check", "_run_intra_check_body", {}),
    ],
)
def test_every_session_wrapper_attaches_the_pnl(wrapper, body_name, kwargs):
    """The defect was per-return-path, so it is fixed at the wrapper: any
    exit the body takes carries the figure out."""
    p = build_pipeline(
        _total_pnl_since_reset=lambda tv: (74.90, 0.749, "2026-08-14"),
        _attach_evidence_freshness=lambda result: None,
        _attach_universe_changes=lambda result: None,
        _persist_session_report=lambda mode, result: None,
        _persist_intra_check_report=lambda result: None,
    )

    def _body(*a, **kw):
        p._record_account_snapshot(10_041.44, 10_000.0)
        return {"status": "paid_analysis_suspended", "run_id": "r"}

    setattr(p, body_name, _body)

    result = getattr(p, wrapper)(**kwargs)

    assert result["daily_pnl"] == pytest.approx(41.44)
    assert result["total_pnl_since"] == "2026-08-14"


def test_earnings_preprocess_names_a_failed_account_read():
    """Pre-market now reads the account (19 Oct); a failed read is named."""
    broker = MagicMock()
    broker.get_account.side_effect = ConnectionError("down")
    p = build_pipeline(broker=broker, _run_earnings_preprocess_body=lambda: {"status": "nothing_new", "count": 0})

    result = p.run_earnings_preprocess()

    assert result["pnl_unavailable_reason"] == "account_read_failed:ConnectionError"


# --- 4. The evening block is untouched ---------------------------------


def test_evening_block_still_uses_its_own_4pm_figure():
    """`_evening_pnl_block` exists because the shared renderer would show
    the after-hours-contaminated real-time figure. Nothing here may pull
    the evening message onto the shared path."""
    from src.notifier import format_session_result

    result = {
        "status": "analyzed",
        "run_id": "r",
        "daily_pnl": 1200.0,
        "total_value": 101_200.0,
        "pnl_4pm": -500.0,
        "equity_close": 100_500.0,
        "analysis": {"risk_rating": "low"},
    }

    msg = format_session_result("evening", result, 10.0)

    assert "4pm close" in msg
    assert "+$1,200" not in msg
    assert "No P&L figure was recorded" not in msg
