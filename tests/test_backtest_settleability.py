"""Settleability tests for the backtest engine, split out of test_backtest."""


def test_contested_day_needs_two_competing_candidates():
    """A lone candidate cut by held risk BINDS the budget but is not
    contested: there is no second name for ticker spelling to order it
    against, and production would have trimmed it identically."""
    from src.backtest.engine import _tie_break_arbitrated
    from src.risk.budget import RiskRequest, allocate_risk_budget

    lone = allocate_risk_budget(
        [RiskRequest("ZZZZ", 5.0)],
        existing_pct={"HELD": 24.0},
        ceiling_pct=25.0,
        floor_pct=0.5,
    )
    assert _tie_break_arbitrated(lone, 1) is False

    pair = allocate_risk_budget(
        [RiskRequest("AAAA", 5.0), RiskRequest("ZZZZ", 5.0)],
        existing_pct={"HELD": 22.0},
        ceiling_pct=25.0,
        floor_pct=0.5,
    )
    assert _tie_break_arbitrated(pair, 2) is True


def test_contested_run_is_reported_as_a_non_result():
    """The tool must disqualify its own output, not leave it to a reader."""
    from src.backtest.metrics import format_settleability_verdict

    bad = format_settleability_verdict(
        contested_budget_days=7,
        binding_budget_days=9,
        entry_days=10,
    )
    assert "NON-RESULT" in bad
    assert "ALPHABETICALLY" in bad

    good = format_settleability_verdict(
        contested_budget_days=0,
        binding_budget_days=3,
        entry_days=10,
    )
    assert "NON-RESULT" not in good
    assert "SETTLEABLE" in good


def test_ab_table_refuses_a_contested_comparison():
    from src.backtest.metrics import Metrics, format_ab_table

    m = Metrics(
        trade_count=4,
        win_rate_pct=50.0,
        avg_win=10.0,
        avg_loss=-5.0,
        avg_win_loss_ratio=2.0,
        expectancy_dollars=2.5,
        expectancy_r=0.2,
        avg_hold_days=3.0,
        max_drawdown_pct=1.0,
        max_drawdown_dollars=100.0,
        total_return_pct=1.0,
        final_equity=101000.0,
    )
    contested = format_ab_table(
        "A",
        m,
        "B",
        m,
        binding_budget_days_a=5,
        binding_budget_days_b=5,
        entry_days_a=10,
        entry_days_b=10,
        contested_budget_days_a=0,
        contested_budget_days_b=4,
    )
    assert "NON-RESULT" in contested
    clean = format_ab_table(
        "A",
        m,
        "B",
        m,
        binding_budget_days_a=1,
        binding_budget_days_b=1,
        entry_days_a=10,
        entry_days_b=10,
        contested_budget_days_a=0,
        contested_budget_days_b=0,
    )
    assert "NON-RESULT" not in clean
