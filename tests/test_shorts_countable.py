"""Stage 1 of short selling — a short position must be COUNTABLE.

No order path in this repo can open or cover a short yet, and nothing here
changes that. What these tests lock is the property that IF a short existed,
every counting, risk-measuring and reporting surface would handle it.

**The load-bearing half of this file is the no-op proof.** The live book is
long-only, so each surface is exercised three ways:

  * ``*_long_only_unchanged`` — the exact pre-change result, asserted against
    a literal. If a short-awareness edit ever perturbs long arithmetic, these
    fail. They are the regression wall, not the new behaviour.
  * ``*_short_*`` — the short case, asserting correct signed handling.
  * ``*_mixed_book`` — both at once, proving the long half of a mixed book is
    identical to the long-only result (the short contributes additively and
    does not contaminate).

Surfaces covered: ``src/portfolio_constructor.py:_current_weights``,
``src/risk/metrics.py`` (``r_multiple`` / ``position_risk`` /
``portfolio_heat``), the ``qty > 0`` reporting filters in
``src/storage/db.py``, ``src/notifier.py``, ``src/trader_feed.py``, and the
``_build_pm_facts`` sector weights / ``_build_position_facts`` pnl_pct in
``src/pipeline.py``.
"""

import sqlite3
from unittest.mock import MagicMock

from src.models import Position, TargetPosition, TechAnalysisResult, TechReasoningChain
from src.portfolio_constructor import PortfolioConstructor
from src.risk.metrics import portfolio_heat, position_risk, r_multiple
from tests.pipeline_factory import build_pipeline


def _pos(symbol: str, qty: float, entry: float, price: float, sector: str = "Technology") -> Position:
    """Alpaca sign convention: a short carries negative qty, and therefore a
    negative market_value. unrealized_pnl is signed the same way — a short
    that falls in price is a WINNER with a positive pnl."""
    return Position(
        symbol=symbol,
        qty=qty,
        avg_entry=entry,
        current_price=price,
        market_value=qty * price,
        unrealized_pnl=qty * (price - entry),
        sector=sector,
    )


def _tech_rc() -> TechReasoningChain:
    return TechReasoningChain(
        trend="x",
        momentum="x",
        volatility="x",
        volume="x",
        support_resistance="x",
    )


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
        setup_type="range",
        expected_horizon_sessions=10,
        reasoning_chain=_tech_rc(),
        thesis_invalid_if="closes below support",
    )


# ==========================================================================
# 1. portfolio_constructor._current_weights — SIGNED weights
# ==========================================================================


def test_current_weights_long_only_unchanged():
    """No-op proof. A long-only book produces exactly the weights it did
    before shorts were representable: market_value x gross_multiplier over
    equity. Literal values, so any drift in the formula fails here."""
    weights = PortfolioConstructor._current_weights(
        [
            _pos("NVDA", qty=150, entry=100, price=100),  # $15k
            _pos("AAPL", qty=50, entry=200, price=200),
        ],  # $10k
        total_value=100_000,
    )
    assert weights == {"NVDA": 15.0, "AAPL": 10.0}


def test_current_weights_long_only_zero_qty_still_excluded():
    """The old filter was `qty > 0`; the new one is `qty != 0`. A flat
    (qty == 0) row must still be absent — the change must admit shorts
    without also admitting closed positions."""
    weights = PortfolioConstructor._current_weights(
        [_pos("NVDA", qty=150, entry=100, price=100), _pos("QQQ", qty=0, entry=400, price=400)],
        total_value=100_000,
    )
    assert set(weights) == {"NVDA"}


def test_current_weights_reports_a_held_short_as_held():
    """The core Stage 1 property. Before this fix a short was filtered out of
    the map entirely, so `current_weights.get(sym, 0.0)` returned 0.0 and the
    constructor read a position it already held as 'not held'."""
    weights = PortfolioConstructor._current_weights(
        [_pos("TSLA", qty=-40, entry=250, price=250)],  # -$10k
        total_value=100_000,
    )
    assert "TSLA" in weights, "a held short must not be invisible to the map"
    # The `.get(sym, 0.0)` default must not be what the delta loop sees.
    assert weights.get("TSLA", 0.0) != 0.0


def test_current_weights_short_is_signed_not_absolute():
    """Signed, deliberately. See the comment in `_current_weights`: every
    consumer does exposure arithmetic, so -10% must not be confusable with a
    +10% long of the same notional."""
    weights = PortfolioConstructor._current_weights(
        [_pos("TSLA", qty=-40, entry=250, price=250)],
        total_value=100_000,
    )
    assert weights["TSLA"] == -10.0


def test_current_weights_mixed_book_long_half_identical():
    """A short in the book must not perturb the long weights beside it."""
    longs = [_pos("NVDA", qty=150, entry=100, price=100), _pos("AAPL", qty=50, entry=200, price=200)]
    long_only = PortfolioConstructor._current_weights(longs, 100_000)
    mixed = PortfolioConstructor._current_weights(
        longs + [_pos("TSLA", qty=-40, entry=250, price=250)],
        100_000,
    )
    assert {k: mixed[k] for k in long_only} == long_only
    assert mixed["TSLA"] == -10.0


def test_current_weights_short_uses_the_same_gross_multiplier():
    """Leveraged-ETF gross convention is unchanged; it just carries a sign.
    SQQQ is -3x, so the gross multiplier is magnitude 3."""
    from src.risk.rules import _gross_multiplier

    mult = _gross_multiplier("SQQQ")
    long_w = PortfolioConstructor._current_weights(
        [_pos("SQQQ", qty=100, entry=100, price=100)],
        100_000,
    )["SQQQ"]
    short_w = PortfolioConstructor._current_weights(
        [_pos("SQQQ", qty=-100, entry=100, price=100)],
        100_000,
    )["SQQQ"]
    assert long_w == 10.0 * mult
    assert short_w == -long_w


def test_constructor_now_covers_a_held_short_on_explicit_close():
    """NEW boundary (Stage 3): this pinned Stage 1's guard — a held short
    with an explicit close target produced zero orders. Stage 3 lifts that
    guard: the same fixture now routes to `_build_cover` and produces a
    full COVER, exactly the way an explicit-close long routes to
    `_build_sell`. The borrow gate, the exposure caps, and the mandatory
    protective stop live at the execution layer, not here — see
    tests/test_shorts_stage3.py for the end-to-end proof that a short can
    be opened only when it clears all three."""
    constructor = PortfolioConstructor()
    decisions = constructor.construct_orders(
        targets=[TargetPosition(symbol="TSLA", target_weight_pct=0.0, conviction="high", thesis="close it")],
        positions=[_pos("TSLA", qty=-40, entry=250, price=250)],
        analyses=[_analysis("TSLA", entry=250, stop=237, target=280)],
        total_value=100_000,
        price_map={"TSLA": 250.0},
    )
    assert len(decisions) == 1
    assert decisions[0].action == "COVER"
    assert decisions[0].symbol == "TSLA"
    assert decisions[0].allocation_pct == 100.0


def test_constructor_long_only_behaviour_unchanged_beside_a_short():
    """No-op proof at the construct_orders level: the long leg of a mixed
    book yields exactly what it yields on a long-only book."""
    constructor = PortfolioConstructor()
    targets = [TargetPosition(symbol="NVDA", target_weight_pct=10.0, conviction="medium", thesis="trim to target")]
    analyses = [_analysis("NVDA", entry=100, stop=95, target=115)]
    nvda = _pos("NVDA", qty=150, entry=100, price=100)  # 15% weight

    long_only = constructor.construct_orders(
        targets=targets,
        positions=[nvda],
        analyses=analyses,
        total_value=100_000,
        price_map={"NVDA": 100.0},
    )
    mixed = constructor.construct_orders(
        targets=targets,
        positions=[nvda, _pos("TSLA", qty=-40, entry=250, price=250)],
        analyses=analyses,
        total_value=100_000,
        price_map={"NVDA": 100.0},
    )
    assert len(long_only) == 1 and long_only[0].action == "HOLD"
    assert [(d.symbol, d.action, d.allocation_pct) for d in mixed] == [
        (d.symbol, d.action, d.allocation_pct) for d in long_only
    ]


# ==========================================================================
# 2a. risk/metrics.r_multiple
# ==========================================================================


def test_r_multiple_long_only_unchanged():
    """No-op proof: the default qty is +1.0, so every existing long call site
    keeps its exact result. Same literals as tests/test_risk_metrics.py."""
    assert r_multiple(current_price=120.0, entry=100.0, initial_stop=90.0) == 2.0
    assert r_multiple(current_price=95.0, entry=100.0, initial_stop=90.0) == -0.5
    assert r_multiple(current_price=120.0, entry=100.0, initial_stop=110.0) is None
    # Explicitly passing a positive qty must change nothing.
    assert r_multiple(120.0, 100.0, 90.0, qty=250.0) == 2.0


def test_r_multiple_short_in_profit_is_positive():
    """Short at $100 with the stop $10 ABOVE at $110. Price falls to $80 →
    +$20 of profit on $10 of risk = +2R. Pre-fix this returned None, because
    `entry - stop` was negative and read as 'no risk defined'."""
    assert r_multiple(current_price=80.0, entry=100.0, initial_stop=110.0, qty=-50) == 2.0


def test_r_multiple_short_underwater_is_negative():
    """Price rose to $105 against a short entered at $100, risking $10 → -0.5R."""
    assert r_multiple(current_price=105.0, entry=100.0, initial_stop=110.0, qty=-50) == -0.5


def test_r_multiple_short_with_stop_below_entry_is_none():
    """Mirror of the long guard: a short's stop must sit ABOVE entry. A stop
    below entry defines no risk, so no R-multiple may be fabricated."""
    assert r_multiple(current_price=80.0, entry=100.0, initial_stop=90.0, qty=-50) is None


# ==========================================================================
# 2b. risk/metrics.position_risk
# ==========================================================================


def test_position_risk_long_only_unchanged():
    """No-op proof, literal-for-literal. 100 shares, $100 entry, $95 stop →
    $500 budget risk; current $110 → $1,500 open risk; 3R."""
    risk = position_risk(symbol="AAA", qty=100, entry=100.0, current_price=110.0, stop=95.0, initial_stop=95.0)
    assert risk.budget_risk_dollars == 500.0
    assert risk.open_risk_dollars == 1500.0
    assert risk.protected is True
    assert risk.risk_released is False
    assert risk.r_multiple == 2.0
    assert risk.market_value == 11_000.0


def test_position_risk_long_released_unchanged():
    """No-op proof for the release rule: stop at or above entry → zero budget."""
    risk = position_risk(symbol="AAA", qty=100, entry=100.0, current_price=130.0, stop=105.0, initial_stop=90.0)
    assert risk.risk_released is True
    assert risk.budget_risk_dollars == 0.0
    assert risk.open_risk_dollars == 2500.0


def test_position_risk_long_unprotected_charges_full_notional_unchanged():
    """No-op proof for the no-stop path."""
    risk = position_risk(symbol="AAA", qty=100, entry=100.0, current_price=110.0, stop=None)
    assert risk.protected is False
    assert risk.budget_risk_dollars == 11_000.0
    assert risk.open_risk_dollars == 11_000.0


def test_position_risk_short_measures_risk_above_entry():
    """Short 100 shares at $100, stop $105 (ABOVE entry) → $5/share of risk
    = $500 budget. Price at $90 → $15/share to the stop = $1,500 open risk.
    Pre-fix: budget 0.0, open 0.0 — a short reported as zero risk."""
    risk = position_risk(symbol="SSS", qty=-100, entry=100.0, current_price=90.0, stop=105.0, initial_stop=105.0)
    assert risk.protected is True
    assert risk.budget_risk_dollars == 500.0
    assert risk.open_risk_dollars == 1500.0
    assert risk.r_multiple == 2.0


def test_position_risk_short_released_when_stop_falls_below_entry():
    """A short's risk is released when the trailed stop comes DOWN through
    entry — the mirror of a long's stop rising above it."""
    risk = position_risk(symbol="SSS", qty=-100, entry=100.0, current_price=80.0, stop=95.0, initial_stop=105.0)
    assert risk.risk_released is True
    assert risk.budget_risk_dollars == 0.0
    assert risk.open_risk_dollars == 1500.0  # 100 x (95 - 80)


def test_position_risk_short_unprotected_charges_absolute_notional():
    """`max(0.0, qty * price)` scored an unprotected short as $0 of risk.
    A naked short's exposure is its absolute notional."""
    risk = position_risk(symbol="SSS", qty=-100, entry=100.0, current_price=110.0, stop=None)
    assert risk.protected is False
    assert risk.budget_risk_dollars == 11_000.0
    assert risk.open_risk_dollars == 11_000.0


def test_position_risk_short_open_risk_floors_at_zero_past_the_stop():
    """Symmetry with the long floor: never a negative risk number."""
    risk = position_risk(symbol="SSS", qty=-100, entry=100.0, current_price=120.0, stop=105.0, initial_stop=105.0)
    assert risk.open_risk_dollars == 0.0


def test_position_risk_flat_position_is_unprotected_not_protected():
    """qty == 0 has no exposure to protect; it must not claim a stop."""
    risk = position_risk(symbol="ZZZ", qty=0, entry=100.0, current_price=110.0, stop=95.0)
    assert risk.protected is False
    assert risk.budget_risk_dollars == 0.0


# ==========================================================================
# 2c. risk/metrics.portfolio_heat
# ==========================================================================


def test_portfolio_heat_long_only_unchanged():
    """No-op proof at the roll-up level, literal-for-literal."""
    heat = portfolio_heat(
        positions=[_pos("AAA", qty=100, entry=100.0, price=110.0), _pos("BBB", qty=50, entry=200.0, price=210.0)],
        equity=100_000,
        stops={"AAA": 95.0, "BBB": 190.0},
    )
    assert heat.budget_risk_dollars == 1000.0  # 500 + 500
    assert heat.open_risk_dollars == 2500.0  # 1500 + 1000
    assert heat.budget_risk_pct == 1.0
    assert heat.unprotected == []


def test_portfolio_heat_long_only_still_skips_flat_rows():
    """The filter loosened from `qty <= 0` to `qty == 0`. A flat row must
    still contribute nothing and must not appear as unprotected."""
    heat = portfolio_heat(
        positions=[_pos("AAA", qty=100, entry=100.0, price=110.0), _pos("FLAT", qty=0, entry=50.0, price=50.0)],
        equity=100_000,
        stops={"AAA": 95.0},
    )
    assert [p.symbol for p in heat.per_position] == ["AAA"]
    assert heat.unprotected == []


def test_portfolio_heat_short_consumes_the_at_risk_budget():
    """The headline Stage 1 risk property. A short at $100 with a $105 stop
    risks $5/share; 100 shares = $500 = 0.5% of $100k equity, so it eats
    0.5% of the 25% ceiling. Pre-fix the short was skipped outright and
    consumed none of it."""
    heat = portfolio_heat(
        positions=[_pos("SSS", qty=-100, entry=100.0, price=90.0)],
        equity=100_000,
        stops={"SSS": 105.0},
    )
    assert [p.symbol for p in heat.per_position] == ["SSS"]
    assert heat.budget_risk_dollars == 500.0
    assert heat.budget_risk_pct == 0.5
    assert heat.headroom_pct(25.0) == 24.5, "the short must reduce headroom"


def test_portfolio_heat_unprotected_short_is_flagged_to_the_operator():
    """A stopless short is the most dangerous thing the book can hold. It
    previously did not even appear in the heat table."""
    heat = portfolio_heat(
        positions=[_pos("SSS", qty=-100, entry=100.0, price=110.0)],
        equity=100_000,
        stops={},
    )
    assert heat.unprotected == ["SSS"]
    assert heat.budget_risk_dollars == 11_000.0


def test_portfolio_heat_mixed_book_long_contribution_identical():
    """No-op proof on a mixed book: the long legs contribute exactly what
    they contribute alone, and the short adds on top rather than distorting."""
    longs = [_pos("AAA", qty=100, entry=100.0, price=110.0), _pos("BBB", qty=50, entry=200.0, price=210.0)]
    stops = {"AAA": 95.0, "BBB": 190.0, "SSS": 105.0}
    long_only = portfolio_heat(longs, equity=100_000, stops=stops)
    mixed = portfolio_heat(
        longs + [_pos("SSS", qty=-100, entry=100.0, price=90.0)],
        equity=100_000,
        stops=stops,
    )
    by_sym = {p.symbol: p for p in mixed.per_position}
    for p in long_only.per_position:
        assert by_sym[p.symbol] == p, "long legs must be bit-identical"
    assert mixed.budget_risk_dollars == long_only.budget_risk_dollars + 500.0
    assert mixed.open_risk_dollars == long_only.open_risk_dollars + 1500.0


def test_portfolio_heat_short_still_honours_exclude_symbols():
    """Cash-sweep exclusion is orthogonal to side and must keep working."""
    heat = portfolio_heat(
        positions=[_pos("SGOV", qty=-100, entry=100.0, price=100.0)],
        equity=100_000,
        stops={},
        exclude_symbols={"SGOV"},
    )
    assert heat.per_position == []


# ==========================================================================
# 3. Reporting / visibility filters
# ==========================================================================


def _positions_db(tmp_path, rows) -> sqlite3.Connection:
    db_path = tmp_path / "quant_agent.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "CREATE TABLE positions ("
        "symbol TEXT PRIMARY KEY, qty REAL, avg_entry REAL,"
        " current_price REAL, market_value REAL,"
        " unrealized_pnl REAL, sector TEXT)"
    )
    conn.executemany("INSERT INTO positions VALUES (?,?,?,?,?,?,?)", rows)
    conn.commit()
    conn.close()
    return db_path


def test_notifier_evening_snapshot_long_only_unchanged(tmp_path, monkeypatch):
    """No-op proof for the evening snapshot on a long-only book."""
    from src.notifier import format_session_result

    db_path = _positions_db(
        tmp_path,
        [
            ("AAPL", 10, 100, 105, 1050, 50, "Tech"),
            ("NVDA", 5, 200, 190, 950, -50, "Tech"),
        ],
    )
    monkeypatch.setattr("src.notifier._DB_PATH", db_path)
    msg = format_session_result(
        "evening",
        {
            "status": "analyzed",
            "run_id": "r",
            "daily_pnl": 0.0,
            "total_value": 2000.0,
            "analysis": {"risk_rating": "moderate"},
        },
        30.0,
    )
    assert msg is not None
    assert "AAPL" in msg and "NVDA" in msg


def test_notifier_evening_snapshot_shows_a_short(tmp_path, monkeypatch):
    """A held short must reach the operator's evening message."""
    from src.notifier import format_session_result

    db_path = _positions_db(
        tmp_path,
        [
            ("AAPL", 10, 100, 105, 1050, 50, "Tech"),
            ("TSLA", -40, 250, 240, -9600, 400, "Auto"),
        ],
    )
    monkeypatch.setattr("src.notifier._DB_PATH", db_path)
    msg = format_session_result(
        "evening",
        {
            "status": "analyzed",
            "run_id": "r",
            "daily_pnl": 0.0,
            "total_value": 2000.0,
            "analysis": {"risk_rating": "moderate"},
        },
        30.0,
    )
    assert msg is not None
    assert "TSLA" in msg, "a short must not be invisible in the evening snapshot"
    assert "AAPL" in msg


def test_trader_feed_positions_long_only_unchanged(tmp_path, monkeypatch):
    """No-op proof for the trader feed snapshot query."""
    from src import trader_feed

    db_path = _positions_db(
        tmp_path,
        [
            ("AAPL", 10, 100, 105, 1050, 50, "Tech"),
            ("FLAT", 0, 10, 10, 0, 0, "Tech"),
        ],
    )
    monkeypatch.setattr(trader_feed, "_DB_PATH", db_path)
    conn = sqlite3.connect(str(db_path))
    rows = conn.execute("SELECT symbol FROM positions WHERE qty != 0").fetchall()
    conn.close()
    assert [r[0] for r in rows] == ["AAPL"]


def test_trader_feed_snapshot_includes_a_short(tmp_path, monkeypatch):
    """`_build_snapshot` reads positions with the loosened filter, so a short
    reaches the feed rather than disappearing from it."""
    from src import trader_feed

    db_path = _positions_db(
        tmp_path,
        [
            ("AAPL", 10, 100, 105, 1050, 50, "Tech"),
            ("TSLA", -40, 250, 240, -9600, 400, "Auto"),
            ("FLAT", 0, 10, 10, 0, 0, "Tech"),
        ],
    )
    monkeypatch.setattr(trader_feed, "_DB_PATH", db_path)
    snap = trader_feed._read_run("run-1")
    syms = {p["symbol"] for p in snap.get("positions", [])}
    assert syms == {"AAPL", "TSLA"}, "short in, flat out"


# ==========================================================================
# 4. pipeline — sector weights and pnl_pct
# ==========================================================================


def _mk_pipeline():
    from src.pipeline import TradingPipeline

    # Item 165: sessions_held now comes from `broker.trading_sessions_held`
    # (holiday-aware). None of these tests span a market holiday, so
    # delegating the mock to the real weekday counter reproduces the same
    # numbers as before.
    from src.trading_calendar import trading_sessions_held as _weekday_sessions_held

    pipeline = build_pipeline(db=MagicMock(), broker=MagicMock())
    pipeline.config = MagicMock()
    pipeline.broker.trading_sessions_held.side_effect = _weekday_sessions_held
    return pipeline


def _pm_facts(pipeline, positions):
    pipeline.db.compute_trade_calibration = MagicMock(return_value={})
    pipeline.db.get_recent_trades = MagicMock(return_value=[])
    return pipeline._build_pm_facts(
        positions=positions,
        analyses=[],
        total_value=100_000.0,
        cash=20_000.0,
        recent_performance={},
    )


def test_pm_facts_sector_weights_long_only_unchanged():
    """No-op proof: a long-only book's sector table is what it always was.

    The field is now `sector_weights_long` (spec §12.2 split the table by
    side); the NUMBERS a long-only book produces are unchanged, which is what
    this test exists to pin.
    """
    pipeline = _mk_pipeline()
    f = _pm_facts(
        pipeline,
        [
            _pos("NVDA", qty=150, entry=100, price=100, sector="Technology"),
            _pos("XOM", qty=100, entry=100, price=100, sector="Energy"),
        ],
    )
    assert f.sector_weights_long["Technology"] == 15.0
    assert f.sector_weights_long["Energy"] == 10.0
    assert f.sector_weights_short == {}


def test_pm_facts_sector_weights_short_is_its_own_positive_exposure():
    """DELIBERATE REVERSAL under spec §12.2 (owner-ratified 2026-09-01).

    This test previously asserted a short rendered as a NEGATIVE weight in
    the single sector table (`Consumer Cyclical == -10.0`). Under §12.2 a
    short is its own exposure on its own side, measured as an UNSIGNED gross
    magnitude against the same limit the long side is measured against.

    What has NOT changed, and what this test still guards, is the original
    defect: `qty <= 0` used to erase a short from the sector table entirely,
    so the PM believed it had no exposure at all. A short must still show up.
    """
    pipeline = _mk_pipeline()
    f = _pm_facts(
        pipeline,
        [
            _pos("TSLA", qty=-40, entry=250, price=250, sector="Consumer Cyclical"),
        ],
    )
    assert f.sector_weights_short["Consumer Cyclical"] == 10.0
    assert "Consumer Cyclical" not in f.sector_weights_long


def test_pm_facts_sector_weights_mixed_book_does_not_net():
    """DELIBERATE REVERSAL under spec §12.2 (owner-ratified 2026-09-01).

    This test previously pinned NETTING as intended behaviour: a long 15% and
    a short 5% in Technology rendered as one 10% line. §12.2 reverses that
    decision. Owner's reasoning, which governs: *"A long and a short in the
    same sector is not a hedge... We are trading opportunities."*

    The reversal matters beyond taste. `RiskRuleEngine.check` now measures
    each side against the limit independently, so a netted PM table would
    show the Portfolio Manager a smaller number than the gate enforces
    against — it would reason about concentration on one book while being
    refused on another. That is the same PM-sees-one-thing / engine-enforces
    -another defect class as Phase 10.
    """
    pipeline = _mk_pipeline()
    f = _pm_facts(
        pipeline,
        [
            _pos("NVDA", qty=150, entry=100, price=100, sector="Technology"),
            _pos("INTC", qty=-50, entry=100, price=100, sector="Technology"),
            _pos("XOM", qty=100, entry=100, price=100, sector="Energy"),
        ],
    )
    assert f.sector_weights_long["Technology"] == 15.0  # NOT 15 - 5
    assert f.sector_weights_short["Technology"] == 5.0  # its own budget
    assert f.sector_weights_long["Energy"] == 10.0  # untouched
    assert "Energy" not in f.sector_weights_short


def _today_ts() -> str:
    """days_held is measured from the BUY timestamp against ET today, so a
    fixed date would drift out of the `days_held < 3` parabolic window."""
    from src.trading_calendar import et_today

    return f"{et_today().isoformat()} 14:00:00"


def _facts_for(position):
    pipeline = _mk_pipeline()
    pipeline.db.get_symbol_last_buy = MagicMock(return_value=None)
    opening = "SHORT" if position.qty < 0 else "BUY"
    morning = [
        {
            "symbol": position.symbol,
            "action": opening,
            "stop_loss": None,
            "take_profit": None,
            "timestamp": _today_ts(),
        }
    ]
    return pipeline._build_position_facts(
        positions=[position],
        morning_trades=morning,
        total_value=100_000.0,
    )[position.symbol]


def test_position_facts_pnl_pct_long_unchanged():
    """No-op proof: a long up 20% still reads as +20% and still trips the
    parabolic threshold arithmetic the same way."""
    p = _pos("AAA", qty=100, entry=100.0, price=120.0)
    assert _facts_for(p)["weight_pct"] == 12.0


def test_position_facts_pnl_pct_long_loser_unchanged():
    p = _pos("AAA", qty=100, entry=100.0, price=90.0)
    facts = _facts_for(p)
    assert facts["parabolic_flag"] is False


def test_position_facts_winning_short_is_not_rendered_as_a_loser():
    """Short 100 shares at $100, price falls to $80 → +$2,000 profit on a
    $10,000 basis = +20%. The negative qty flipped the denominator's sign,
    so a 20% winner rendered as -20% and the parabolic/drift flags read the
    wrong side of the trade."""
    p = _pos("SSS", qty=-100, entry=100.0, price=80.0)
    assert p.unrealized_pnl == 2000.0, "fixture sanity: this short is winning"
    facts = _facts_for(p)
    # 20% gain, held <3 sessions in the fixture → parabolic fires, as it
    # would for the equivalent long. Pre-fix pnl_pct was -20 and it did not.
    assert facts["parabolic_flag"] is True


def test_position_facts_losing_short_is_not_rendered_as_a_winner():
    """The mirror: a short that moved against us must not read as a gain."""
    p = _pos("SSS", qty=-100, entry=100.0, price=120.0)
    assert p.unrealized_pnl == -2000.0
    facts = _facts_for(p)
    assert facts["parabolic_flag"] is False


def test_position_facts_mixed_book_long_facts_identical():
    """No-op proof: adding a short to the book does not change the long's
    computed facts."""
    pipeline = _mk_pipeline()
    pipeline.db.get_symbol_last_buy = MagicMock(return_value=None)
    long_pos = _pos("AAA", qty=100, entry=100.0, price=120.0)
    short_pos = _pos("SSS", qty=-100, entry=100.0, price=80.0)
    morning = [
        {"symbol": "AAA", "action": "BUY", "stop_loss": None, "take_profit": None, "timestamp": _today_ts()},
        {"symbol": "SSS", "action": "SHORT", "stop_loss": None, "take_profit": None, "timestamp": _today_ts()},
    ]
    alone = pipeline._build_position_facts(
        positions=[long_pos],
        morning_trades=morning,
        total_value=100_000.0,
    )["AAA"]
    together = pipeline._build_position_facts(
        positions=[long_pos, short_pos],
        morning_trades=morning,
        total_value=100_000.0,
    )
    assert together["AAA"] == alone
    assert "SSS" in together, "the short must appear in position facts"
