"""The 25% total-risk ceiling is never skipped because the book is hard to read.

Defect found 2026-10-09: one holding whose stop/price could not be read made
the WHOLE book's risk unknown and the ceiling was bypassed for every trade; a
missing price was charged as $0; and dollar-only targets never reached the
ceiling at all. Each test below fails on the code before the fix.
"""

from types import SimpleNamespace

import src.risk.metrics as metrics
from src.models import Position, TargetPosition, TechAnalysisResult, TechReasoningChain
from src.portfolio_constructor import PortfolioConstructor

EQUITY = 100_000.0


def _analysis(symbol: str, entry: float, stop: float, target: float) -> TechAnalysisResult:
    return TechAnalysisResult(
        symbol=symbol,
        rating="buy",
        entry_price=entry,
        stop_loss=stop,
        reference_target=target,
        reasoning="test",
        support_levels=[stop],
        resistance_levels=[target],
        computed_levels=[stop, target],
        computed_level_touches={stop: 5, target: 5},
        computed_level_bars={stop: [(stop, stop)], target: [(target, target)]},
        atr_14=(entry - stop) / 3.5,
        setup_type="range",
        expected_horizon_sessions=60,
        reasoning_chain=TechReasoningChain(trend="x", momentum="x", volatility="x", volume="x", support_resistance="x"),
        thesis_invalid_if="closes below support",
    )


def _pos(symbol: str, qty: float, avg_entry: float, current_price: float) -> Position:
    return Position(
        symbol=symbol,
        qty=qty,
        avg_entry=avg_entry,
        current_price=current_price,
        market_value=qty * current_price,
        unrealized_pnl=(current_price - avg_entry) * qty,
        sector="Technology",
    )


def _risk_target(symbol: str, risk_pct: float) -> TargetPosition:
    return TargetPosition(symbol=symbol, risk_allocation_pct=risk_pct, conviction="high", thesis="thesis")


def _dollar_target(symbol: str, weight_pct: float) -> TargetPosition:
    return TargetPosition(symbol=symbol, target_weight_pct=weight_pct, conviction="high", thesis="thesis")


def test_one_unreadable_holding_is_charged_at_full_value_and_the_budget_still_binds(monkeypatch):
    real = metrics.position_risk

    def flaky(symbol, **kw):
        if symbol == "BAD":
            raise RuntimeError("stop read exploded")
        return real(symbol, **kw)

    monkeypatch.setattr(metrics, "position_risk", flaky)
    heat = metrics.portfolio_heat(
        [_pos("BAD", 250, 100.0, 100.0), _pos("OK", 10, 100.0, 100.0)],
        EQUITY,
        stops={"OK": 95.0},
    )
    rows = {r.symbol: r for r in heat.per_position}
    # Full market value, not lost, not zero; the healthy row is untouched.
    assert rows["BAD"].budget_risk_dollars == 25_000.0
    assert rows["BAD"].risk_unknown is False
    assert rows["OK"].budget_risk_dollars == 50.0

    book = metrics.BookRiskPct({r.symbol: r.budget_risk_dollars / EQUITY * 100 for r in heat.per_position})
    constructor = PortfolioConstructor()
    decisions = constructor.construct_orders(
        targets=[_risk_target("NVDA", 2.0)],
        positions=[],
        analyses=[_analysis("NVDA", 100, 95, 140)],
        total_value=EQUITY,
        price_map={"NVDA": 100.0},
        existing_risk_pct=book,
    )
    assert decisions == []
    assert constructor.drain_refusals()["NVDA"]["refusal"] == "risk_budget_exhausted"


def test_a_book_that_cannot_be_rolled_up_is_charged_at_full_value_not_skipped():
    from src.pipeline_stages import _book_risk_inputs

    ctx = SimpleNamespace(
        facts=SimpleNamespace(heat=None),
        positions=[_pos("BAD", 250, 100.0, 100.0)],
    )
    existing = _book_risk_inputs(ctx, EQUITY)
    assert existing is not None  # None used to skip the 25% ceiling outright
    assert existing["BAD"] == 25.0
    assert not existing.unknown


def test_a_missing_price_is_unknown_never_zero():
    row = metrics.position_risk("X", qty=100, entry=50.0, current_price=None, stop=None)
    assert row.risk_unknown is True
    assert row.budget_risk_dollars > 0  # was $0 under `_finite(...) or 0.0`
    heat = metrics.portfolio_heat([SimpleNamespace(symbol="X", qty=100, avg_entry=50.0, current_price=None)], EQUITY)
    assert heat.unknown_risk == ["X"]


def test_a_buy_is_refused_while_risk_is_unknown_and_an_exit_is_not():
    constructor = PortfolioConstructor()
    decisions = constructor.construct_orders(
        targets=[_risk_target("NVDA", 1.0), _risk_target("AAPL", 0.0)],
        positions=[_pos("AAPL", 10, 100.0, 100.0)],
        analyses=[_analysis("NVDA", 100, 95, 140)],
        total_value=EQUITY,
        price_map={"NVDA": 100.0, "AAPL": 100.0},
        existing_risk_pct=metrics.BookRiskPct({"X": 1.0, "AAPL": 1.0}, unknown={"X"}),
    )
    assert [d.symbol for d in decisions] == ["AAPL"]
    refusal = constructor.drain_refusals()["NVDA"]
    assert refusal["refusal"] == "book_risk_unknown"
    assert "X" in refusal["detail"]


def test_a_dollar_only_target_is_converted_to_risk_and_rationed():
    constructor = PortfolioConstructor()
    decisions = constructor.construct_orders(
        targets=[_dollar_target("NVDA", 10.0)],
        positions=[],
        analyses=[_analysis("NVDA", 100, 95, 140)],
        total_value=EQUITY,
        price_map={"NVDA": 100.0},
        existing_risk_pct=metrics.BookRiskPct({"HELD": 25.0}),
    )
    assert decisions == []  # used to be sized the old way, past a full budget
    assert constructor.drain_refusals()["NVDA"]["refusal"] == "risk_budget_exhausted"


def test_a_dollar_only_target_with_no_stop_is_refused_by_name(caplog):
    caplog.set_level("WARNING", logger="src.portfolio_constructor")
    constructor = PortfolioConstructor()
    decisions = constructor.construct_orders(
        targets=[_dollar_target("NVDA", 10.0)],
        positions=[],
        analyses=[],
        total_value=EQUITY,
        price_map={"NVDA": 100.0},
        existing_risk_pct=metrics.BookRiskPct({}),
    )
    assert decisions == []
    # The data fault upstream is already on record for NVDA, so the named
    # refusal is filed only-if-unrecorded; the log line always names it.
    assert any("NVDA dollar-only target refused" in r.getMessage() for r in caplog.records)


def test_missing_facts_or_account_value_is_an_unknown_book_never_a_skipped_ceiling():
    from src.pipeline_stages import _book_risk_inputs

    for ctx, value in (
        (SimpleNamespace(facts=None), EQUITY),
        (SimpleNamespace(facts=SimpleNamespace(heat=None)), 0.0),
    ):
        existing = _book_risk_inputs(ctx, value)
        assert existing is not None  # None used to skip the 25% ceiling outright
        assert existing.unknown


def test_a_partial_trim_of_a_held_name_is_not_refused_while_the_book_is_unknown():
    constructor = PortfolioConstructor()
    constructor.construct_orders(
        targets=[_risk_target("AAPL", 1.0), _risk_target("NVDA", 1.0)],
        positions=[_pos("AAPL", 100, 100.0, 100.0)],
        analyses=[_analysis("AAPL", 100, 95, 140), _analysis("NVDA", 100, 95, 140)],
        total_value=EQUITY,
        price_map={"AAPL": 100.0, "NVDA": 100.0},
        existing_risk_pct=metrics.BookRiskPct({"X": 1.0, "AAPL": 2.0}, unknown={"X"}),
    )
    refusals = constructor.drain_refusals()
    assert refusals.get("AAPL", {}).get("refusal") != "book_risk_unknown"  # reduces risk: passes
    assert refusals["NVDA"]["refusal"] == "book_risk_unknown"  # adds risk: refused
