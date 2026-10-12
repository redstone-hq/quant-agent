"""One definition per quantity — drift regression tests.

Four numbers in this system were computed independently in two or three
places and disagreed with each other. Measured on identical inputs before
the fix:

  deployable cash   engine $54,000 vs dashboard $34,100   (1.58x apart)
  "% deployed"      risk rule 40.68% vs cockpit 54.24%    (13.56pp apart)
  20-day $ volume   $11.400M / $12.000M / $11.429M        (5.26% spread)
  inverse-ETF set   agreed, but one copy was hand-maintained

Each now has exactly one implementation in `src/quantities.py`. Every test
below is written to FAIL if a second definition reappears — either
behaviourally (two real call sites stop agreeing) or structurally (the
arithmetic is retyped somewhere it shouldn't be).

The behavioural tests are the load-bearing half: they call the REAL
functions on both sides, so mutating `src/quantities.py` breaks them, and
so does any caller that stops routing through it.

Scope note, stated rather than hidden: the exposure guard covers
`src/risk/rules.py`, `src/api/` and `frontend/src/`. `src/pipeline.py`
still contains its own `sum(market_value * _effective_multiplier(...))`
expressions for pending/projected exposure; those are part of the separate
"how invested is the book" unification and are deliberately out of scope
here rather than silently excluded.
"""

from __future__ import annotations

import importlib
import re
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from src.config import CashSweepConfig, RiskConfig
from src.execution.cash_sweep import CashSweeper
from src.models import OHLCV, Position, TradeDecision
from src.pipeline import TradingPipeline, _missed_ops_quality_metrics
from src.quantities import ETF_LEVERAGE, avg_dollar_volume, inverse_etf_symbols
from src.risk.rules import RiskRuleEngine
from tests.pipeline_factory import build_pipeline

REPO_ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# Shared fixtures: one book, measured by every implementation.
# ---------------------------------------------------------------------------

CASH = 40_000.0
BOOK = [
    Position(
        symbol="SGOV",
        qty=140,
        avg_entry=100.0,
        current_price=100.0,
        market_value=14_000.0,
        unrealized_pnl=0.0,
        sector="Unknown",
    ),
    Position(
        symbol="NVDA",
        qty=100,
        avg_entry=380.0,
        current_price=400.0,
        market_value=40_000.0,
        unrealized_pnl=2_000.0,
        sector="Technology",
    ),
    Position(
        symbol="MSFT",
        qty=50,
        avg_entry=390.0,
        current_price=400.0,
        market_value=20_000.0,
        unrealized_pnl=500.0,
        sector="Technology",
    ),
    # -3x inverse ETF: a hedge SUBTRACTS from net exposure, at 3x notional.
    Position(
        symbol="SQQQ",
        qty=200,
        avg_entry=20.0,
        current_price=20.0,
        market_value=4_000.0,
        unrealized_pnl=0.0,
        sector="Unknown",
    ),
]
EQUITY = CASH + sum(p.market_value for p in BOOK)  # $118,000
RESERVE_PCT = 5.0
SWEEP_SYMBOL = "SGOV"


def _book_as_api_payload() -> dict:
    """The same book in the shape `broker_reads.read_positions()` returns."""
    return {
        "positions": [
            {
                "symbol": p.symbol,
                "qty": p.qty,
                "avg_entry": p.avg_entry,
                "current_price": p.current_price,
                "market_value": p.market_value,
                "unrealized_pnl": p.unrealized_pnl,
                "is_cash_equivalent": p.symbol == SWEEP_SYMBOL,
                "direction": (
                    "cash_equivalent"
                    if p.symbol == SWEEP_SYMBOL
                    else "bearish_hedge"
                    if p.symbol in inverse_etf_symbols()
                    else "long"
                ),
            }
            for p in BOOK
        ],
        "error": None,
    }


def _pipeline() -> TradingPipeline:
    p = build_pipeline(broker=MagicMock(), db=MagicMock())
    p.config = SimpleNamespace(
        cash_sweep=CashSweepConfig(
            enabled=True,
            symbol=SWEEP_SYMBOL,
        ),
        risk=RiskConfig(
            max_position_pct=20,
            max_total_position_pct=90,
            require_stop_loss=True,
            allow_margin=False,
        ),
    )
    p.cash_sweeper = CashSweeper(pipeline=p)
    p.risk_engine = RiskRuleEngine(p.config.risk)
    return p


@pytest.fixture
def api_routes(monkeypatch):
    """`routes_live` with config + broker reads stubbed to THIS book."""
    from src.api import routes_live

    monkeypatch.setattr(routes_live, "get_cash_sweep_enabled", lambda: True)
    monkeypatch.setattr(routes_live, "get_cash_sweep_symbol", lambda: SWEEP_SYMBOL)
    monkeypatch.setattr(routes_live, "get_cash_reserve_pct", lambda: RESERVE_PCT)
    monkeypatch.setattr(routes_live, "read_positions", _book_as_api_payload)
    return routes_live


# ---------------------------------------------------------------------------
# (1) deployable cash
# ---------------------------------------------------------------------------


def test_dashboard_deployable_equals_the_figure_the_engine_sizes_against(api_routes):
    """The operator's "Deployable" tile and the PM's sizing input are the
    same number. They were $54,000 and $34,100 on this exact book — same
    word, opposite adjustment (one ADDED the parked sweep value, the other
    SUBTRACTED a sweep-mechanics reserve)."""
    engine = _pipeline()._compute_deployable_cash(CASH, BOOK)
    dashboard = api_routes._compute_liquidity(CASH, EQUITY).deployable_cash

    assert engine == pytest.approx(54_000.0)
    assert dashboard == pytest.approx(engine), (
        "Mission Control's deployable cash has drifted from the engine's. "
        "Both must come from src.quantities.deployable_cash."
    )


def test_conservative_figure_survives_under_its_own_name(api_routes):
    """The reserve-adjusted figure is still reported — it is genuinely
    useful — but it may never occupy the word "deployable" again."""
    liq = api_routes._compute_liquidity(CASH, EQUITY)
    assert liq.reserve_usd == pytest.approx(5_900.0)  # 5% of $118k
    assert liq.cash_above_reserve == pytest.approx(34_100.0)  # the OLD "deployable"
    assert liq.deployable_cash != liq.cash_above_reserve


def test_total_liquidity_is_an_alias_not_a_second_computation(api_routes):
    """`total_liquidity` was the engine's number under a different name.
    Kept as a deprecated alias, assigned from the one source."""
    liq = api_routes._compute_liquidity(CASH, EQUITY)
    assert liq.total_liquidity == liq.deployable_cash


def test_sweep_reserve_formula_is_written_exactly_once():
    """`portfolio_value * reserve_pct / 100` existed in three files.

    The band moved out of `CashSweepConfig` to `CashReserveConfig.pct`
    2026-10-01 (board item 190) and the sweeper's own `reserve_usd` wrapper
    went with the retired feature; the shared arithmetic did not move."""
    from src.quantities import sweep_reserve_usd as _reserve

    assert _reserve(EQUITY, RESERVE_PCT) == pytest.approx(5_900.0)

    offenders = []
    for path in _python_sources():
        if path.name == "quantities.py":
            continue
        text = path.read_text()
        if re.search(r"reserve_pct\s*\]?\s*/\s*100", text) or re.search(r"max\(\s*cash\s*-\s*reserve", text):
            offenders.append(str(path.relative_to(REPO_ROOT)))
    assert not offenders, (
        "the sweep reserve / reserve-adjusted cash formula is retyped in: "
        f"{offenders}. Call src.quantities.sweep_reserve_usd / "
        "cash_above_reserve instead."
    )


# ---------------------------------------------------------------------------
# (2) net exposure — the gauge and the ceiling it is drawn against
# ---------------------------------------------------------------------------


def _rule_two_percentage() -> float:
    """The percentage `max_total_position_pct` actually judges, extracted by
    running the real rule and reading the violation it emits."""
    engine = RiskRuleEngine(
        RiskConfig(
            max_position_pct=20,
            max_total_position_pct=0.0001,  # trip it so the value is reported
            require_stop_loss=False,
            allow_margin=False,
        )
    )
    investable = [p for p in BOOK if p.symbol != SWEEP_SYMBOL]
    decision = TradeDecision(
        symbol="AAPL",
        action="BUY",
        allocation_pct=0.0,
        entry_price=100.0,
        stop_loss=95.0,
        take_profit=110.0,
        reasoning="probe measurement, allocates nothing",
    )
    violations = engine.check(
        decision=decision,
        positions=investable,
        total_value=EQUITY,
        cash=CASH,
    )
    hit = [v for v in violations if v.rule == "max_total_position_pct"]
    assert hit, "expected the net-exposure rule to report its measurement"
    return hit[0].value


def test_dashboard_percent_deployed_equals_the_engines_net_exposure(api_routes):
    """The cockpit drew its bar against the ENGINE's ceiling while filling
    it from its own definition — hedges ADDED instead of netted, leverage
    ignored. 40.68% vs 54.24% on this book."""
    rule_pct = _rule_two_percentage()
    served = api_routes._compute_exposure(EQUITY).net_exposure_pct

    # 40,000 + 20,000 - (4,000 x 3) = 48,000 on 118,000 of equity.
    assert rule_pct == pytest.approx(40.6779, abs=1e-3)
    assert served == pytest.approx(rule_pct), (
        "Mission Control's exposure gauge has drifted from the rule its "
        "ceiling comes from. Both must use src.quantities.net_exposure_*."
    )


def test_exposure_excludes_the_cash_park_like_the_engine_does(api_routes):
    """Parked cash is not a position. The engine splits it out upstream;
    the API's payload is unsplit, so it must exclude by symbol."""
    served = api_routes._compute_exposure(EQUITY)
    assert served.net_exposure_usd == pytest.approx(48_000.0)


def test_no_second_exposure_definition_in_the_api_or_the_cockpit():
    """Structural half: nothing on the read surface may re-derive an
    exposure percentage from position market values."""
    offenders = []

    api_dir = REPO_ROOT / "src" / "api"
    for path in sorted(api_dir.glob("*.py")):
        text = path.read_text()
        if re.search(r"market_value[^;\n]{0,300}\*\s*100", text):
            offenders.append(str(path.relative_to(REPO_ROOT)))

    frontend = REPO_ROOT / "frontend" / "src"
    for path in sorted(list(frontend.rglob("*.ts")) + list(frontend.rglob("*.tsx"))):
        text = path.read_text()
        # One statement that both touches market_value and produces a
        # percentage is the shape of the defect this replaces.
        if re.search(r"market_value[^;]{0,400}?\*\s*100", text, re.S):
            offenders.append(str(path.relative_to(REPO_ROOT)))

    assert not offenders, (
        "an exposure percentage is being re-derived from market values in: "
        f"{offenders}. Read AccountResponse.exposure.net_exposure_pct."
    )


def test_cockpit_reads_exposure_from_the_server():
    """Positive pin — the negative scan above would also pass if the gauge
    were simply deleted."""
    hero = (REPO_ROOT / "frontend" / "src" / "components" / "HeroBand.tsx").read_text()
    assert "account.exposure?.net_exposure_pct" in hero
    code = "\n".join(ln for ln in hero.splitlines() if not ln.strip().startswith("//"))
    assert "(longMv + hedgeMv) / total" not in code


def test_position_direction_label_computes_no_risk_math():
    """schemas.py has always claimed `direction` is "display labeling only;
    computes no exposure/risk math". The cockpit used it for exactly that.
    Pin the claim to reality on the consuming side."""
    hero = (REPO_ROOT / "frontend" / "src" / "components" / "HeroBand.tsx").read_text()
    # `direction` may still select what to DISPLAY (Long / Hedge tiles);
    # it may not appear in the statement that produces the percentage.
    pct_line = [ln for ln in hero.splitlines() if "riskDeployedPct" in ln and "=" in ln]
    assert pct_line, "expected the deployed-percentage assignment to exist"
    assert not any("direction" in ln for ln in pct_line)


# ---------------------------------------------------------------------------
# (3) 20-day average dollar volume
# ---------------------------------------------------------------------------


def _bars_with_one_halt(n: int = 25) -> list[OHLCV]:
    """`n` sessions at $12M/day, with one HALTED (zero-volume) session
    inside the trailing 20. Dropping that session instead of counting it as
    a real zero is what put the three implementations 5.26% apart."""
    bars = [
        OHLCV(date=date(2026, 8, 1) + timedelta(days=i), open=100.0, high=101.0, low=99.0, close=100.0, volume=120_000)
        for i in range(n)
    ]
    bars[-5] = OHLCV(date=bars[-5].date, open=100.0, high=100.0, low=100.0, close=100.0, volume=0)
    return bars


def test_all_three_call_sites_measure_the_same_dollar_volume(monkeypatch):
    """Nomination admission gate, top-mover digest and the tech-analyst
    prompt. Previously $11.400M / $12.000M / $11.429M on identical bars."""
    from src.data import context as context_mod
    import src.pipeline as pipeline_mod

    bars = _bars_with_one_halt()
    expected_usd = 11_400_000.0  # 19 sessions x $12M / 20 sessions

    # a) the shared definition itself
    assert avg_dollar_volume(bars) == pytest.approx(expected_usd)

    # b) the top-mover digest gate (reports millions)
    digest_m, _, _ = _missed_ops_quality_metrics(bars, 20)
    assert digest_m == pytest.approx(expected_usd / 1e6, abs=0.005)

    # c) the tech-analyst prompt context
    ctx = context_mod.compute_market_context(bars)
    assert ctx.avg_dollar_volume_20d == pytest.approx(expected_usd)

    # d) the external-symbol admission gate
    p = _pipeline()
    p.config.trading = SimpleNamespace(lookback_days=60)
    p.config.smart_money = SimpleNamespace(
        min_external_history_days=20,
        min_external_price_usd=5.0,
        min_external_avg_dollar_volume_usd=10_000_000.0,
    )
    p.broker.get_transient_equity_eligibility = MagicMock(return_value={"eligible": True})
    p.market = MagicMock()
    p.market.get_ohlcv = MagicMock(return_value=bars)
    monkeypatch.setattr("src.pipeline_admission._get_sector", lambda s: "Technology")
    ok, reason, facts = p.admission._evaluate_external_admission_gates("FAKE")
    assert ok, reason
    assert facts["avg_dollar_volume_20d_usd"] == pytest.approx(expected_usd)


def test_the_window_is_twenty_sessions_not_twenty_one():
    """The prompt-facing site averaged `_W_1M` = 21 bars — the generic
    one-month constant leaking into a measure whose name, config key and
    log lines all say 20."""
    from src.data import context as context_mod

    bars = [
        OHLCV(date=date(2026, 8, 1) + timedelta(days=i), open=100.0, high=101.0, low=99.0, close=100.0, volume=100_000)
        for i in range(30)
    ]
    # Make the 21st-from-last bar enormous. A 21-bar window sees it; the
    # correct 20-bar window does not.
    bars[-21] = OHLCV(date=bars[-21].date, open=100.0, high=100.0, low=100.0, close=100.0, volume=100_000_000)
    ctx = context_mod.compute_market_context(bars)
    assert ctx.avg_dollar_volume_20d == pytest.approx(10_000_000.0)


def test_a_halted_session_counts_as_zero_not_as_missing():
    """The conservative reading, and the only one an admission gate may
    use: dropping the halt inflates the average toward ADMITTING an
    illiquid symbol."""
    bars = _bars_with_one_halt()
    assert avg_dollar_volume(bars) == pytest.approx(11_400_000.0)
    # The dropped-survivor definition would have read $12.000M.
    assert avg_dollar_volume(bars) < 12_000_000.0


def test_a_window_where_nothing_traded_is_unknown_not_zero():
    """The other side of the same coin. One zero among twenty is a real
    halted session and belongs in the average; twenty zeros is a missing
    volume feed, and reporting that as $0 would fabricate a measurement
    (and would read to a `< threshold` gate as "definitely illiquid")."""
    dead = [
        OHLCV(date=date(2026, 8, 1) + timedelta(days=i), open=100.0, high=100.0, low=100.0, close=100.0, volume=0)
        for i in range(25)
    ]
    assert avg_dollar_volume(dead) is None
    assert avg_dollar_volume(dead, min_bars=5) is None


def test_no_second_dollar_volume_definition_anywhere():
    offenders = []
    for path in _python_sources():
        if path.name == "quantities.py":
            continue
        text = path.read_text()
        if re.search(r"close[^;\n]{0,80}\*[^;\n]{0,80}volume", text):
            offenders.append(str(path.relative_to(REPO_ROOT)))
    assert not offenders, (
        f"close x volume arithmetic outside src/quantities.py: {offenders}. "
        "Call src.quantities.avg_dollar_volume / dollar_volumes."
    )


# ---------------------------------------------------------------------------
# (4) the inverse-ETF roster
# ---------------------------------------------------------------------------


def test_api_inverse_etf_set_is_derived_not_copied():
    """rules.py promised new funds are "picked up automatically"; the API
    held a hand-maintained literal that would not have been. Adding a fund
    to the one table must reach the API with no second edit."""
    from src.api import deps

    assert deps.INVERSE_ETF_SYMBOLS == inverse_etf_symbols()

    ETF_LEVERAGE["FAKEBEAR"] = -2.0
    try:
        assert "FAKEBEAR" in inverse_etf_symbols()
        # Adding the fund to the one table is the ONLY edit needed: the API
        # picks it up on its next import, with no hand-maintained copy to
        # remember. (The API is a separate, restart-on-config-change
        # process, so import time is exactly when it would see it.)
        importlib.reload(deps)
        assert "FAKEBEAR" in deps.INVERSE_ETF_SYMBOLS
    finally:
        ETF_LEVERAGE.pop("FAKEBEAR", None)
        importlib.reload(deps)
    assert "FAKEBEAR" not in deps.INVERSE_ETF_SYMBOLS


def test_no_hardcoded_inverse_etf_roster_outside_the_leverage_table():
    """The literal set `{"SH", "SDS", "PSQ", "SQQQ"}` may exist in exactly
    one place: nowhere. It is derived."""
    offenders = []
    # The exact four-symbol roster, in any order — not `src/data/earnings.py`'s
    # broader "ETFs have no 10-Q" list, which is a different (also
    # hand-maintained, also pre-existing) set and out of scope here.
    pattern = re.compile(r"\{\s*(?:[\"'](?:SH|SDS|PSQ|SQQQ)[\"']\s*,?\s*){4}\}")
    for path in _python_sources():
        if path.name == "quantities.py":
            continue
        if pattern.search(path.read_text()):
            offenders.append(str(path.relative_to(REPO_ROOT)))
    assert not offenders, f"a hand-maintained inverse-ETF roster survives in: {offenders}"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _python_sources() -> list[Path]:
    """Every first-party Python source, excluding tests (which legitimately
    spell formulas out to pin expected values)."""
    out: list[Path] = []
    for base in ("src", "ops", "scripts"):
        root = REPO_ROOT / base
        if not root.is_dir():
            continue
        out.extend(p for p in sorted(root.rglob("*.py")) if "__pycache__" not in p.parts)
    return out


def test_disabled_sweep_does_not_inflate_the_dashboard_deployable(monkeypatch):
    """Board item 190: with the sweep RETIRED, the parked vehicle is not
    deployable — `CashSweeper.fund_buys` returns 0.0 on its first line and
    nothing else converts it to cash for the BUY phase, so the engine's
    `_compute_deployable_cash` adds 0.0. The /account view used to add it
    anyway, which would have read above the figure the PM sizes against.
    Latent only: no cash-equivalent position is held today."""
    from src.api import routes_live

    monkeypatch.setattr(routes_live, "get_cash_sweep_enabled", lambda: False)
    monkeypatch.setattr(routes_live, "get_cash_sweep_symbol", lambda: SWEEP_SYMBOL)
    monkeypatch.setattr(routes_live, "get_cash_reserve_pct", lambda: RESERVE_PCT)
    monkeypatch.setattr(routes_live, "read_positions", _book_as_api_payload)

    pipeline = _pipeline()
    pipeline.config.cash_sweep.enabled = False
    engine = pipeline._compute_deployable_cash(CASH, BOOK)

    liq = routes_live._compute_liquidity(CASH, EQUITY)

    assert engine == pytest.approx(CASH)
    assert liq.sweep_parked_value > 0, "the vehicle is still reported as held"
    assert liq.deployable_cash == pytest.approx(engine), (
        "a retired sweep's parked vehicle must not be counted as deployable"
    )
