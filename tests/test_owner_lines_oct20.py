"""Four owner-facing lines the desk sent on 2026-10-20 that misled or read
as jargon. One test per line: each proves the new wording and that the old
wording is gone, and each fails on the code as it stood that day. Display
only — no decision, limit or number used by trading changes here.
"""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import patch

from src import log_health as L
from src import trader_feed
from src.config import RiskConfig
from src.models import TradeDecision
from src.risk import rules
from src.risk.rules import RiskRuleEngine
from tests.test_log_health import WIDE_END, WIDE_START


def _fam(key: str) -> L.FaultFamily:
    return next(f for f in L.FAMILIES if f.key == key)


# --- 1) "COST position would be 65.0% and exceed max 65.0%" ---------------


def test_single_stock_cap_breach_shows_two_figures_that_differ():
    # 65.04% rounded to one decimal equalled the 65.0% limit on 20 Oct.
    assert rules.over_limit_words("COST", "", 65.04, 65.0) == (
        "COST would go over the 65% limit for one stock (it would be 65.04%)"
    )
    # A breach by less than a hundredth shows more decimals, never a tie.
    text = rules.over_limit_words("COST", "", 65.0004, 65.0)
    assert text.endswith("(it would be 65.0004%)")
    # Beyond four decimals the figure is dropped rather than shown equal.
    assert rules.over_limit_words("COST", "", 65.000004, 65.0) == "COST would go over the 65% limit for one stock"
    # The engine itself emits the new wording and never the contradictory old one.
    engine = RiskRuleEngine(
        RiskConfig(max_position_pct=20.0, max_total_position_pct=300.0, require_stop_loss=True, allow_margin=True)
    )
    decision = TradeDecision(
        action="BUY",
        symbol="COST",
        allocation_pct=20.02,
        entry_price=100.0,
        stop_loss=90.0,
        take_profit=120.0,
        reasoning="t",
    )
    violations = engine.check(decision=decision, positions=[], total_value=100_000.0, cash=100_000.0)
    cap = next(v for v in violations if v.rule == "max_position_pct")
    assert cap.message == "COST would go over the 20% limit for one stock (it would be 20.02%)"
    assert "exceed max" not in cap.message


# --- 2) "$10,972 invested · 100% deployed, 0% cash" on a $9,796 account ---


def _positions_summary(invested: float, equity: float) -> str:
    lines: list[str] = []
    rows = [
        {"symbol": "AAPL", "market_value": invested, "unrealized_pnl": 0.0, "avg_entry": 100.0, "current_price": 100.0}
    ]
    trader_feed._append_evening_positions(lines, {"_positions": rows, "equity_close": equity}, {})
    return lines[1]


def test_invested_above_equity_says_borrowing_not_one_hundred_percent():
    line = _positions_summary(10_972.0, 9_796.0)
    assert "112% of the account invested (using $1,176 of borrowing), 0% cash" in line
    assert "100% deployed" not in line


def test_invested_within_equity_keeps_the_old_wording():
    line = _positions_summary(7_000.0, 10_000.0)
    assert "70% deployed, 30% cash" in line
    assert "borrowing" not in line


# --- 3) desk-health jargon --------------------------------------------------


def test_desk_health_says_what_is_unrecognised_and_first_time_seen_in_plain_words():
    partial = _fam("research_seat_partial")
    assert partial.sentence.format(n=1, plural="").startswith("A research AI gave a partial answer")
    assert "answered only partly" not in partial.sentence
    assert "answered only partly" not in partial.short_name

    finding = L.Finding(family=_fam("seat_answer_unreadable"), count=1)
    assert L.disposition(finding, set(), set(), is_new=True) == "first time seen, not on the list yet"
    assert "new —" not in L.disposition(finding, set(), set(), is_new=True)

    unrec = L.Finding(family=_fam(L.UNRECOGNISED_KEY), count=162, last_seen=WIDE_END - timedelta(hours=1))
    report = L.Report(
        window_start=WIDE_START,
        window_end=WIDE_END,
        findings=[unrec],
        previous={L.UNRECOGNISED_KEY: 162},
        log_read_to=WIDE_END,
    )
    text = "\n".join(L.render(report))
    still = next(l for l in text.splitlines() if l.startswith("• Still true:"))
    assert "162 fault log lines this health check cannot describe" in still
    assert "does not recognise" not in text


# --- 4) "Measured against the $507.02 actually at risk today, that is -48.41%."


def test_evening_risk_capital_line_reads_as_plain_words():
    result = {
        "status": "analyzed",
        "run_id": "r",
        "pnl_4pm": -245.45,
        "equity_close": 9_796.0,
        "risk_capital_dollars": 507.02,
        "analysis": {"risk_rating": "low", "daily_summary": "Quiet day."},
    }
    with patch.object(trader_feed.evening, "_read_run", return_value={}):
        msg = trader_feed._format_evening(result, 10.0)
    assert "Today's loss was 48% of the $507 the desk had at risk if every stop were hit." in msg
    assert "Measured against" not in msg
    assert "actually at risk" not in msg
