"""PortfolioConstructor — target state → concrete orders."""

from src.models import Position, TargetPosition, TechAnalysisResult, TechReasoningChain
from src.portfolio_constructor import PortfolioConstructor, ConstructorConfig


def _tech_rc() -> TechReasoningChain:
    """Minimal valid 5-step CoT — every field is `min_length=1`-enforced
    after the PR #89 audit fix, so test fixtures must populate them."""
    return TechReasoningChain(
        trend="x",
        momentum="x",
        volatility="x",
        volume="x",
        support_resistance="x",
    )


def _pos(symbol: str, qty: float, avg_entry: float, current_price: float, sector: str = "Technology") -> Position:
    return Position(
        symbol=symbol,
        qty=qty,
        avg_entry=avg_entry,
        current_price=current_price,
        market_value=qty * current_price,
        unrealized_pnl=(current_price - avg_entry) * qty,
        sector=sector,
    )


def _analysis(
    symbol: str,
    entry: float,
    stop: float,
    target: float,
    horizon: int = 60,
    atr: float | None = None,
) -> TechAnalysisResult:
    """A realistic analyst result — including the two fields production sets
    in Python rather than asking the model for.

    `atr_14` and `computed_levels` are attached by `TechAnalystAgent` after
    parsing (from the indicators and from `find_structural_levels` over the
    full history). A fixture without them is not a thing the pipeline can
    produce, and since 2026-09-01 the constructor derives the take-profit
    from `computed_levels` and refuses without them.

    The ATR is set just inside the noise band so the structural stop is left
    alone — these tests are about sizing, not about stop widening. The long
    horizon is not decoration: reaching a target W away from a stop R away
    needs sqrt(sessions) >= 1.5 * W/R once the stop is held at 2.25 ATRs
    (the coefficient is `min_stop_atr_multiple` x setup scaler divided by
    `MAX_REACH_ATR_MULTIPLE`: 2.5 x 0.90 = 2.25, and 2.25 / 1.5 = 1.5). It
    was 0.9 at the 1.5 base (1.35 / 1.5) and 2.3 while the floor was 3.0
    base / 1.15 range, which is why the fixture horizons here are
    far longer than the geometry now requires. They are left long on
    purpose — a fixture that only just clears a gate stops testing sizing
    the moment the gate moves again.
    """
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
        atr_14=(entry - stop) / 3.5 if atr is None else atr,
        setup_type="range",
        expected_horizon_sessions=horizon,
        reasoning_chain=_tech_rc(),
        thesis_invalid_if="closes below support",
    )


def test_construct_orders_opens_new_position():
    """Target on a symbol not currently held → BUY for the full target weight."""
    constructor = PortfolioConstructor()
    targets = [TargetPosition(symbol="NVDA", target_weight_pct=8.0, conviction="high", thesis="AI")]
    analyses = [_analysis("NVDA", entry=100, stop=92, target=120)]
    price_map = {"NVDA": 100.0}

    decisions = constructor.construct_orders(
        targets=targets,
        positions=[],
        analyses=analyses,
        total_value=100_000,
        price_map=price_map,
    )
    assert len(decisions) == 1
    d = decisions[0]
    assert d.action == "BUY"
    assert d.symbol == "NVDA"
    assert d.entry_price == 100.0
    assert d.stop_loss == 92.0
    assert d.take_profit == 120.0
    # allocation bounded by risk budget: $100k × 0.5% = $500 at risk, $8/share →
    # 100 shares max; 100 shares × $100 = $10k = 10% weight. Target was 8%, so
    # alloc stays at 8 (under the cap).
    assert d.allocation_pct == 8.0


def test_construct_orders_trims_to_target_weight():
    """Held at 15% weight, target 10% → kept whole (owner ruling 2026-10-09).

    A held position is kept at its size or sold whole; the planner never
    sells part of it. The refusal is recorded by name.
    """
    constructor = PortfolioConstructor()
    # $15k position on $100k equity = 15% weight
    positions = [_pos("NVDA", qty=150, avg_entry=100, current_price=100)]
    targets = [TargetPosition(symbol="NVDA", target_weight_pct=10.0, conviction="medium", thesis="trim to target")]
    analyses = [_analysis("NVDA", entry=100, stop=95, target=115)]

    decisions = constructor.construct_orders(
        targets=targets,
        positions=positions,
        analyses=analyses,
        total_value=100_000,
        price_map={"NVDA": 100.0},
    )
    assert len(decisions) == 1
    d = decisions[0]
    assert d.action == "HOLD"
    assert constructor.last_refusals["NVDA"]["refusal"] == "held_partial_trim_refused"


def test_construct_orders_closes_at_zero_target():
    """target_weight_pct=0 on a held symbol → full-close SELL (alloc=100)."""
    constructor = PortfolioConstructor()
    positions = [_pos("AAPL", qty=50, avg_entry=180, current_price=200)]
    targets = [TargetPosition(symbol="AAPL", target_weight_pct=0.0, conviction="low", thesis="close — thesis broken")]

    decisions = constructor.construct_orders(
        targets=targets,
        positions=positions,
        analyses=[],
        total_value=100_000,
    )
    assert len(decisions) == 1
    assert decisions[0].action == "SELL"
    assert decisions[0].allocation_pct == 100.0


def test_construct_orders_nudges_a_tiny_delta_when_measurable():
    """Owner ruling 2026-09-30 (board item 183): the flat 0.5%-of-book churn
    floor (`min_trade_weight_delta`) is DELETED. Held at 8.1%, target 8.2% —
    a 0.1% delta that used to be silently converted into a HOLD — is now a
    real nudge the desk attempts, given something to size it from."""
    constructor = PortfolioConstructor()
    positions = [_pos("NVDA", qty=81, avg_entry=100, current_price=100)]  # 8.1%
    targets = [TargetPosition(symbol="NVDA", target_weight_pct=8.2, conviction="high", thesis="keep")]

    decisions = constructor.construct_orders(
        targets=targets,
        positions=positions,
        analyses=[_analysis("NVDA", entry=100, stop=92, target=140)],
        total_value=100_000,
        price_map={"NVDA": 100.0},
    )
    assert len(decisions) == 1
    assert decisions[0].action == "BUY"
    assert abs(decisions[0].allocation_pct - 0.1) < 1e-6


def test_construct_orders_exact_zero_delta_still_holds():
    """A target that asks for EXACTLY what is already held is not a
    request to trade at all — the floating-point-noise guard that replaced
    `min_trade_weight_delta` still records a HOLD for audit continuity, the
    same as before the floor was removed. This is not the arbitrary floor
    coming back: 8.1% == 8.1% has no delta to nudge."""
    constructor = PortfolioConstructor()
    positions = [_pos("NVDA", qty=81, avg_entry=100, current_price=100)]  # 8.1%
    targets = [TargetPosition(symbol="NVDA", target_weight_pct=8.1, conviction="high", thesis="keep")]

    decisions = constructor.construct_orders(
        targets=targets,
        positions=positions,
        analyses=[],
        total_value=100_000,
        price_map={"NVDA": 100.0},
    )
    assert len(decisions) == 1
    assert decisions[0].action == "HOLD"


def test_construct_orders_risk_budget_caps_buy_size():
    """Wide-stop name: the single-name risk budget caps below the target weight.

    Pinned at 0.5% explicitly rather than relying on the default, which is now
    the owner-ratified 5% envelope (2026-08-27). What this test is for is the
    capping MECHANISM on a legacy notional target, not the size of the budget.
    """
    from src.portfolio_constructor import ConstructorConfig

    constructor = PortfolioConstructor(ConstructorConfig(risk_budget_pct=0.5))
    # Target 10% on $100k = $10k = 100 shares @ $100.
    # Stop 80 → risk_per_share = $20. Risk budget $500 / $20 = 25 shares max
    # → 25 × $100 = $2500 = 2.5% weight cap.
    targets = [TargetPosition(symbol="NVDA", target_weight_pct=10.0, conviction="high", thesis="deep stop")]
    analyses = [_analysis("NVDA", entry=100, stop=80, target=140)]

    decisions = constructor.construct_orders(
        targets=targets,
        positions=[],
        analyses=analyses,
        total_value=100_000,
        price_map={"NVDA": 100.0},
    )
    assert len(decisions) == 1
    # Capped from 10 → 2.5
    assert abs(decisions[0].allocation_pct - 2.5) < 0.05


def test_construct_orders_orders_sells_before_buys():
    """Rotation: a close + a new open → SELL returned first so cash refreshes."""
    constructor = PortfolioConstructor()
    positions = [_pos("AAPL", qty=50, avg_entry=180, current_price=200)]
    targets = [
        TargetPosition(symbol="AAPL", target_weight_pct=0.0, conviction="low", thesis="close"),
        TargetPosition(symbol="NVDA", target_weight_pct=8.0, conviction="high", thesis="open"),
    ]
    analyses = [_analysis("NVDA", entry=100, stop=92, target=120)]

    decisions = constructor.construct_orders(
        targets=targets,
        positions=positions,
        analyses=analyses,
        total_value=100_000,
        price_map={"AAPL": 200.0, "NVDA": 100.0},
    )
    assert len(decisions) == 2
    assert decisions[0].action == "SELL"
    assert decisions[0].symbol == "AAPL"
    assert decisions[1].action == "BUY"
    assert decisions[1].symbol == "NVDA"


def test_construct_orders_orders_buys_by_weight_descending():
    """BUYs should be prioritized by larger target weight under cash rationing."""
    constructor = PortfolioConstructor()
    targets = [
        TargetPosition(symbol="AAPL", target_weight_pct=3.0, conviction="medium", thesis="smaller"),
        TargetPosition(symbol="NVDA", target_weight_pct=8.0, conviction="high", thesis="larger"),
    ]
    analyses = [
        _analysis("AAPL", entry=200, stop=160, target=260),
        _analysis("NVDA", entry=100, stop=92, target=120),
    ]

    decisions = constructor.construct_orders(
        targets=targets,
        positions=[],
        analyses=analyses,
        total_value=100_000,
        price_map={"AAPL": 200.0, "NVDA": 100.0},
    )

    assert [d.symbol for d in decisions] == ["NVDA", "AAPL"]


def test_construct_orders_uses_suggested_stop_when_provided():
    """PM override: target.suggested_stop_price wins over TA's stop."""
    constructor = PortfolioConstructor()
    targets = [
        TargetPosition(
            symbol="NVDA", target_weight_pct=5.0, conviction="medium", thesis="tighter stop", suggested_stop_price=89.0
        )
    ]
    # Low ATR so the PM's tighter stop sits OUTSIDE the noise band and is
    # left alone — this test is about stop precedence, not stop widening.
    analyses = [_analysis("NVDA", entry=100, stop=85, target=125, atr=0.7)]

    decisions = constructor.construct_orders(
        targets=targets,
        positions=[],
        analyses=analyses,
        total_value=100_000,
        price_map={"NVDA": 100.0},
    )
    assert len(decisions) == 1
    assert decisions[0].stop_loss == 89.0  # PM's override, not TA's 85


def test_construct_orders_rejects_buy_without_price_reference():
    """No market_price AND no TA analysis → constructor skips the BUY."""
    constructor = PortfolioConstructor()
    targets = [TargetPosition(symbol="UNKNOWN", target_weight_pct=5.0, conviction="medium", thesis="blind buy")]

    decisions = constructor.construct_orders(
        targets=targets,
        positions=[],
        analyses=[],
        total_value=100_000,
        price_map={},  # no price for UNKNOWN
    )
    # No price, no analysis → can't construct → empty result
    assert decisions == []


def test_construct_orders_rejects_buy_when_no_structural_stop_supplied():
    """No suggested stop, no TA analysis → no structural stop is available,
    so the BUY is rejected outright.

    Previously (before 2026-08-27) this fell back to entry × (1 - fallback_pct).
    That ATR/percent fallback family was deleted on purpose — a stop nobody
    derived from the chart can't be risk-sized honestly. Renamed from
    `test_construct_orders_falls_back_to_fallback_stop_when_no_hint`.
    """
    constructor = PortfolioConstructor()
    targets = [TargetPosition(symbol="NVDA", target_weight_pct=5.0, conviction="medium", thesis="no TA")]

    decisions = constructor.construct_orders(
        targets=targets,
        positions=[],
        analyses=[],  # NO analysis
        total_value=100_000,
        price_map={"NVDA": 100.0},
    )
    # No structural stop from any source → the BUY is dropped, not sized
    # against an invented one.
    assert decisions == []


def test_resolve_stop_returns_none_when_no_structural_stop_supplied():
    """When `analysis.stop_loss` is None and `target.suggested_stop_price`
    is unset, `_resolve_stop` returns None — no structural stop, no trade.

    Previously (before 2026-08-27) this asserted a volatility-aware
    `entry − 2*ATR` fallback when `analysis.atr_14` was available. That
    fallback was deleted on purpose: it let PortfolioConstructor invent a
    stop nobody derived from the chart. Renamed from
    `test_resolve_stop_atr_fallback_when_llm_stop_missing`. We test
    `_resolve_stop` directly with a SimpleNamespace stand-in to bypass the
    model validator (which now requires a real stop_loss for actionable
    ratings anyway).
    """
    from types import SimpleNamespace

    constructor = PortfolioConstructor()
    target = TargetPosition(
        symbol="NVDA",
        target_weight_pct=5.0,
        conviction="medium",
        thesis="no structural stop",
    )
    # ATR is retained only as a noise-band input elsewhere; it no longer
    # feeds a stop fallback here.
    fake_analysis = SimpleNamespace(stop_loss=None, atr_14=8.0)
    stop = constructor._resolve_stop(target, fake_analysis, entry_price=100.0)
    assert stop is None


def test_resolve_stop_llm_stop_wins_over_atr():
    """LLM-supplied stop_loss takes precedence over ATR fallback."""
    from types import SimpleNamespace

    constructor = PortfolioConstructor(config=ConstructorConfig())
    target = TargetPosition(
        symbol="NVDA",
        target_weight_pct=5.0,
        conviction="medium",
        thesis="x",
    )
    fake_analysis = SimpleNamespace(stop_loss=90.0, atr_14=8.0)
    stop = constructor._resolve_stop(target, fake_analysis, entry_price=100.0)
    assert stop == 90.0


def test_resolve_stop_returns_none_when_neither_stop_nor_atr_available():
    """When neither LLM stop, ATR, nor a suggested stop is available,
    `_resolve_stop` returns None — there's no hardcoded % to fall through to
    anymore.

    Previously (before 2026-08-27) this asserted a fall-through to the
    hardcoded 5% fallback — same as the pre-audit behaviour. That fallback
    was deleted on purpose. Renamed from
    `test_resolve_stop_falls_through_to_pct_when_no_atr`.
    """
    from types import SimpleNamespace

    constructor = PortfolioConstructor()
    target = TargetPosition(
        symbol="NVDA",
        target_weight_pct=5.0,
        conviction="medium",
        thesis="x",
    )
    fake_analysis = SimpleNamespace(stop_loss=None, atr_14=None)
    stop = constructor._resolve_stop(target, fake_analysis, entry_price=100.0)
    assert stop is None


def test_construct_orders_empty_targets_returns_empty():
    constructor = PortfolioConstructor()
    assert (
        constructor.construct_orders(
            targets=[],
            positions=[],
            analyses=[],
            total_value=100_000,
        )
        == []
    )


def test_construct_orders_skips_sell_when_position_market_value_is_nan():
    """Broker price glitch can produce qty>0 with market_value=NaN
    (current_price came back NaN, then qty * NaN = NaN). Without this
    guard, current_pct = NaN / total_value * 100 = NaN, the partial
    fraction math is NaN, alloc becomes NaN, and a NaN allocation_pct
    gets sent to the broker. R4 audit finding — pin the guard."""
    constructor = PortfolioConstructor()
    nan_position = Position(
        symbol="GLITCH",
        qty=100,
        avg_entry=100.0,
        current_price=float("nan"),
        market_value=float("nan"),  # broker glitch
        unrealized_pnl=0.0,
        sector="Technology",
    )
    targets = [
        TargetPosition(
            symbol="GLITCH",
            target_weight_pct=5.0,
            conviction="medium",
            thesis="trim to 5%",
        )
    ]

    decisions = constructor.construct_orders(
        targets=targets,
        positions=[nan_position],
        analyses=[],
        total_value=100_000,
    )
    # The SELL is dropped — no NaN-tainted orders leak to the broker.
    sells = [d for d in decisions if d.action == "SELL"]
    assert sells == []


def test_resolve_stop_returns_none_and_the_funnel_reads_one_from_the_instrument(caplog):
    """A missing stop is no longer a refusal (docs/WORK.md item 54,
    2026-09-12). `_resolve_stop` still returns None when neither the PM nor
    the analyst typed one — it only ranks the two typed sources — but it
    logs that the stop WILL be read from the instrument, and
    `_widen_stop_past_noise` then places it at the ATR noise band (or the
    signal bar's edge when that is wider). Nothing is a flat percentage:
    the pre-2026-08-27 `entry * 0.95` tier stays gone.

    Previously this pinned "no structural stop → None → the BUY is
    rejected", the Phase 1 rule replaced when sourced research showed no
    published method refuses a trade for lack of a level under it.
    """
    import logging
    from types import SimpleNamespace

    constructor = PortfolioConstructor()
    target = TargetPosition(
        symbol="MICRO",
        target_weight_pct=3.0,
        conviction="low",
        thesis="too volatile",
    )
    fake_analysis = SimpleNamespace(
        stop_loss=None,
        atr_14=2.0,
        setup_type="range",
        expected_horizon_sessions=20,
        computed_levels=[],
        signal_bar_low=None,
    )

    with caplog.at_level(logging.INFO):
        stop = constructor._resolve_stop(target, fake_analysis, entry_price=100.0)
    assert stop is None, "nothing typed: `_resolve_stop` ranks typed sources only"
    assert any("read from the instrument" in r.message and target.symbol in r.message for r in caplog.records)
    placed = constructor._widen_stop_past_noise(
        "MICRO",
        fake_analysis,
        100.0,
        None,
        direction="long",
        target_price=110.0,
    )
    band = 100.0 - constructor._stop_atr_multiple() * 2.0
    assert placed is not None and abs(placed - band) < 1e-9


def test_resolve_stop_returns_none_when_genuinely_no_stop_information():
    """A brand-new symbol with no volatility history and no LLM-supplied
    stop still resolves to None — there is no naive-percent fallback left
    to catch it.

    Previously (before 2026-08-27) this asserted the 5% fallback WAS the
    right answer when there was genuinely no volatility info. That
    fallback was deleted on purpose. Renamed from
    `test_resolve_stop_uses_pct_fallback_when_atr_truly_unavailable`.
    """
    from types import SimpleNamespace

    constructor = PortfolioConstructor()
    target = TargetPosition(
        symbol="NEWIPO",
        target_weight_pct=3.0,
        conviction="low",
        thesis="x",
    )
    fake_analysis = SimpleNamespace(stop_loss=None, atr_14=None)
    stop = constructor._resolve_stop(target, fake_analysis, entry_price=100.0)
    assert stop is None


# ---------------------------------------------------------------------------
# Board item 80 (2026-09-25, REWORKED per owner ruling). A missing ATR is
# NEVER a reason to skip protection or to size off an unverifiable typed
# number. When there is no volatility reading the constructor DERIVES the
# protective stop from price structure that survives on the analysis object
# (computed levels, else the signal/prior bar) and HOLDS the position; it
# refuses the ONE name only when no structural level is readable at all, or
# the only readable level sits past the desk's stop-distance sanity bound
# (skip on RISK). The earlier item-80 pass, which refused a typed stop
# outright, was overruled: "there are always levels, even from a few days
# ago, and there are other ways of setting a stop."
# ---------------------------------------------------------------------------


def _no_atr_analysis(**overrides):
    """A no-ATR analysis shell for the structural-fallback tests."""
    from types import SimpleNamespace

    base = dict(
        atr_14=None,
        setup_type="breakout",
        signal_bar_low=None,
        signal_bar_high=None,
        computed_levels=[],
        computed_level_touches={},
        expected_horizon_sessions=20,
        reference_target=None,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def test_no_atr_derives_structural_stop_from_computed_level_and_holds():
    """A verified computed level below entry with no ATR yields a stop placed
    one buffer below the level, and the position is HELD (not refused). On the
    first item-80 pass this same input was refused outright."""
    constructor = PortfolioConstructor()
    analysis = _no_atr_analysis(
        computed_levels=[95.0],
        computed_level_touches={95.0: 5},
    )
    buffer = constructor.cfg.structural_stop_buffer_pct
    result = constructor._widen_stop_past_noise(
        "ACME",
        analysis,
        100.0,
        96.0,
        direction="long",
        target_price=None,
    )
    assert result is not None, "a missing ATR must not skip protection"
    assert abs(result - 95.0 * (1.0 - buffer)) < 1e-9
    assert constructor.last_refusals == {}
    # The derived structural read, never the unverifiable typed 96.0.
    assert abs(result - 96.0) > 0.5


def test_no_atr_falls_back_to_prior_bar_low_when_no_verified_level():
    """With no computed level on the protective side (the only one sits above
    a long's entry) but a signal-bar low present, the stop is read from the
    prior bar and held. Since 2026-10-14 any real level below entry would
    anchor the stop at any touch count, so "no level" means none below."""
    constructor = PortfolioConstructor()
    analysis = _no_atr_analysis(
        computed_levels=[105.0],
        computed_level_touches={105.0: 2},
        signal_bar_low=97.0,
    )
    buffer = constructor.cfg.structural_stop_buffer_pct
    result = constructor._widen_stop_past_noise(
        "ACME",
        analysis,
        100.0,
        None,
        direction="long",
        target_price=None,
    )
    assert result is not None
    assert abs(result - 97.0 * (1.0 - buffer)) < 1e-9
    assert constructor.last_refusals == {}


def test_no_atr_short_derives_from_resistance_and_holds():
    """The short side is mirrored: resistance above entry with no ATR places
    the stop one buffer above the level, and holds."""
    constructor = PortfolioConstructor()
    analysis = _no_atr_analysis(
        computed_levels=[105.0],
        computed_level_touches={105.0: 6},
    )
    buffer = constructor.cfg.structural_stop_buffer_pct
    result = constructor._widen_stop_past_noise(
        "TSLA",
        analysis,
        100.0,
        104.0,
        direction="short",
        target_price=None,
    )
    assert result is not None
    assert abs(result - 105.0 * (1.0 + buffer)) < 1e-9
    assert constructor.last_refusals == {}


def test_no_atr_no_readable_structure_refuses_per_name():
    """No ATR AND no structural level (no computed level, no signal bar): the
    genuine skip-correct case. Refuses THIS one name with its own code, and an
    ATR-present name in the same batch is untouched — never a book-wide halt."""
    from src.portfolio_constructor import (
        STOP_REFUSAL_NO_STRUCTURAL_STOP_NO_VOLATILITY,
    )

    constructor = PortfolioConstructor()
    bare = _no_atr_analysis()  # no levels, no bar
    has_atr = _no_atr_analysis(atr_14=8.0)
    refused = constructor._widen_stop_past_noise(
        "OUTAGE",
        bare,
        100.0,
        90.0,
        direction="long",
        target_price=None,
    )
    kept = constructor._widen_stop_past_noise(
        "HEALTHY",
        has_atr,
        100.0,
        50.0,
        direction="long",
        target_price=None,
    )
    assert refused is None
    assert kept == 50.0, "the ATR-present name is unaffected by the other's refusal"
    assert set(constructor.last_refusals) == {"OUTAGE"}
    assert constructor.last_refusals["OUTAGE"]["refusal"] == (STOP_REFUSAL_NO_STRUCTURAL_STOP_NO_VOLATILITY)


def test_no_atr_wide_structural_stop_is_held_not_refused_on_width():
    """Board item 185. The no-ATR path used to refuse a structural stop more
    than 50% of entry away, borrowing the midday typo guard's flat fraction.
    That fraction is gone, and re-picking a width bound with no volatility
    reading to read it from is what doctrine bars — so the stop is HELD, as
    board item 56 route (c) already ruled for the width refusal further down
    the same method: a wide stop is answered by sizing down, not by a
    refusal here."""
    constructor = PortfolioConstructor()
    # 40.0 below a 100 entry -> ~60% away, past the old 50% bound.
    analysis = _no_atr_analysis(
        computed_levels=[40.0],
        computed_level_touches={40.0: 6},
    )
    result = constructor._widen_stop_past_noise(
        "FAR",
        analysis,
        100.0,
        41.0,
        direction="long",
        target_price=None,
    )
    assert result is not None
    assert "FAR" not in constructor.last_refusals


def test_widest_reachable_stop_atr_multiple_is_computed_not_chosen():
    """Board item 185. The widest stop the desk can place is the owner-ruled
    base (2.5 ATR, ruling 2026-10-04) -- no setup or regime scaler since
    2026-10-09 -- and `_stop_atr_multiple` returns exactly that."""
    import pytest
    from src.portfolio_constructor import (
        ConstructorConfig,
        widest_reachable_stop_atr_multiple,
    )

    cfg = ConstructorConfig()
    widest = widest_reachable_stop_atr_multiple(cfg.min_stop_atr_multiple)
    assert widest == pytest.approx(2.5)
    assert widest_reachable_stop_atr_multiple() == pytest.approx(widest)
    assert PortfolioConstructor()._stop_atr_multiple() == pytest.approx(widest)


def test_no_atr_nothing_typed_derives_and_holds():
    """Nothing typed AND no ATR is no longer an automatic refusal: with a
    readable level the stop is derived from structure and the position held.
    (Superseded the old `no_stop_and_no_volatility` refusal for this input.)"""
    constructor = PortfolioConstructor()
    analysis = _no_atr_analysis(
        computed_levels=[95.0],
        computed_level_touches={95.0: 5},
    )
    result = constructor._widen_stop_past_noise(
        "BARE",
        analysis,
        100.0,
        None,
        direction="long",
        target_price=None,
    )
    assert result is not None
    assert constructor.last_refusals == {}


def test_typed_stop_with_atr_present_is_unchanged_by_item_80():
    """Regression guard: with ATR present, a typed stop already outside the
    noise band still ships exactly as before (STOP_RULE_OUTSIDE_BAND), with
    no refusal recorded. Item 80 touched ONLY the no-ATR branch."""
    from types import SimpleNamespace

    constructor = PortfolioConstructor()
    analysis = SimpleNamespace(
        atr_14=8.0,
        setup_type="breakout",
        signal_bar_low=None,
        signal_bar_high=None,
        computed_levels=[],
        expected_horizon_sessions=20,
        reference_target=None,
    )
    result = constructor._widen_stop_past_noise(
        "NVDA",
        analysis,
        100.0,
        50.0,
        direction="long",
        target_price=None,
    )
    assert result == 50.0
    assert constructor.last_refusals == {}


def test_construct_orders_rejects_buy_when_no_reference_target_supplied():
    """New coverage (2026-08-27): a structural stop is present but the
    Tech Analyst supplied no `reference_target` → the BUY is rejected.
    Targets are no longer synthesized from `entry * (1 + 2*stop_gap_pct)`;
    a missing target now means no trade, same as a missing stop.
    """
    from types import SimpleNamespace

    constructor = PortfolioConstructor()
    targets = [TargetPosition(symbol="NVDA", target_weight_pct=5.0, conviction="medium", thesis="no target")]
    # A stop is present (so this isn't the missing-stop rejection path) but
    # reference_target is None.
    fake_analysis = SimpleNamespace(
        symbol="NVDA",
        stop_loss=95.0,
        atr_14=None,
        reference_target=None,
    )

    decisions = constructor.construct_orders(
        targets=targets,
        positions=[],
        analyses=[fake_analysis],
        total_value=100_000,
        price_map={"NVDA": 100.0},
    )
    assert decisions == []


def test_current_weights_applies_gross_multiplier_for_inverse_etfs():
    """The constructor must agree with RiskRuleEngine.check on what
    a 20% target means for inverse / leveraged ETFs. Pre-fix the
    constructor used raw market_value/total_value while engine used
    gross multiplier — same $10K SQQQ saw 10% in constructor, 30%
    gross at the engine. That mismatch caused every leveraged-ETF
    target at the ceiling to hard-block at the engine while the
    constructor saw no need to trim.

    Now both use gross. PM's target_weight_pct=20 on SQQQ means
    20% GROSS exposure (≈ 6.67% raw notional)."""
    from src.portfolio_constructor import PortfolioConstructor

    # 3 positions: SPY (raw=1x), SDS (inverse -2x → gross 2x), SQQQ (inverse -3x → gross 3x).
    spy = Position(
        symbol="SPY",
        qty=100,
        avg_entry=500.0,
        current_price=500.0,
        market_value=50000.0,
        unrealized_pnl=0.0,
        sector="Broad",
    )
    sds = Position(
        symbol="SDS",
        qty=200,
        avg_entry=50.0,
        current_price=50.0,
        market_value=10000.0,
        unrealized_pnl=0.0,
        sector="Broad",
    )
    sqqq = Position(
        symbol="SQQQ",
        qty=100,
        avg_entry=100.0,
        current_price=100.0,
        market_value=10000.0,
        unrealized_pnl=0.0,
        sector="Broad",
    )

    total_value = 200000.0
    weights = PortfolioConstructor._current_weights(
        [spy, sds, sqqq],
        total_value=total_value,
    )

    # SPY: market_value 50000, gross_mul 1.0 → 50000/200000 * 100 = 25%
    assert abs(weights["SPY"] - 25.0) < 1e-6
    # SDS: market_value 10000, gross_mul 2.0 → 10000 * 2 / 200000 * 100 = 10%
    assert abs(weights["SDS"] - 10.0) < 1e-6
    # SQQQ: market_value 10000, gross_mul 3.0 → 10000 * 3 / 200000 * 100 = 15%
    assert abs(weights["SQQQ"] - 15.0) < 1e-6


def test_current_weights_zero_or_negative_total_value_returns_empty():
    """Sanity: NaN / 0 / negative total_value still short-circuits to
    empty dict. The fix didn't change this guardrail."""
    from src.portfolio_constructor import PortfolioConstructor

    pos = Position(
        symbol="SPY",
        qty=100,
        avg_entry=500.0,
        current_price=500.0,
        market_value=50000.0,
        unrealized_pnl=0.0,
        sector="Broad",
    )
    assert PortfolioConstructor._current_weights([pos], total_value=0) == {}
    assert PortfolioConstructor._current_weights([pos], total_value=-100) == {}


def test_risk_budget_cap_carries_provenance_note_for_rm():
    """2026-08-20 veto forensic: the RM read 'PM said 15%, order says
    10.65%' as plan incoherence and issued a full-plan rejection —
    nobody had told it the constructor's risk budget capped the size.
    A capped BUY's reasoning must carry the [constructor: ...] note."""
    from src.models import TargetPosition, TechAnalysisResult

    from src.portfolio_constructor import ConstructorConfig

    constructor = PortfolioConstructor(ConstructorConfig(risk_budget_pct=0.5))
    target = TargetPosition(
        symbol="XLE",
        target_weight_pct=15.0,
        conviction="high",
        thesis="Energy geopolitical tailwind.",
        thesis_invalid_if="",
        catalyst="",
    )
    # Wide stop: entry 100, stop 90 -> risk 10/share. 0.5% risk budget on
    # 10_000 equity = $50 -> 5 shares -> $500 = 5% alloc, well under 15%.
    analysis = _analysis("XLE", entry=100.0, stop=90.0, target=130.0)
    decisions = constructor.construct_orders(
        targets=[target],
        positions=[],
        analyses=[analysis],
        total_value=10_000.0,
        price_map={"XLE": 100.0},
    )

    assert len(decisions) == 1
    d = decisions[0]
    assert d.allocation_pct < 15.0
    assert "[constructor:" in d.reasoning
    assert "not PM inconsistency" in d.reasoning


def test_uncapped_buy_has_no_provenance_note():
    from src.models import TargetPosition, TechAnalysisResult

    constructor = PortfolioConstructor()
    target = TargetPosition(
        symbol="XLF",
        target_weight_pct=5.0,
        conviction="medium",
        thesis="Financials steepener.",
        thesis_invalid_if="",
        catalyst="",
    )
    # Stop 12% away: a 5% weight risks 0.60%, above the 0.5% floor and under any cap.
    analysis = _analysis("XLF", entry=100.0, stop=88.0, target=125.0)
    decisions = constructor.construct_orders(
        targets=[target],
        positions=[],
        analyses=[analysis],
        total_value=10_000.0,
        price_map={"XLF": 100.0},
    )

    assert len(decisions) == 1
    assert "[constructor:" not in decisions[0].reasoning


def test_dropped_target_reason_is_captured_not_silently_lost():
    """Funnel-queue item 2 (2026-09-03) reproduction: a target the
    constructor drops before ever building an order used to leave NOTHING
    recoverable outside the log — `blocked_proposals_census.py` counted 19
    of these across 2026-08-18..09-02 as `no_order_built`, its largest
    unexplained bucket, and the module docstring on that script says the
    constructor's own reason "is only ever logger.info/logger.warning
    text — never persisted to a table."

    Same fixture as `test_construct_orders_rejects_buy_when_no_structural_stop_supplied`
    (no analysis at all → no structural stop → the BUY is dropped). The
    fix under test is `PortfolioConstructor.last_drop_reasons`: the real
    log line the constructor already emits for the drop, captured onto the
    instance so a caller (`pipeline_stages.DecisionStage`) can persist a
    terminal per-symbol evidence row instead of nothing. This test is
    scoped to the constructor's own contract — the DecisionStage
    integration (the actual DB write) is exercised by the pipeline-level
    fixtures, not re-derived here.
    """
    constructor = PortfolioConstructor()
    targets = [TargetPosition(symbol="NVDA", target_weight_pct=5.0, conviction="medium", thesis="no TA")]

    decisions = constructor.construct_orders(
        targets=targets,
        positions=[],
        analyses=[],  # NO analysis
        total_value=100_000,
        price_map={"NVDA": 100.0},
    )

    assert decisions == []
    # The old failure mode: nothing whatsoever survives the call. The fix:
    # the dropped symbol's real reason is on the instance afterward.
    assert "NVDA" in constructor.last_drop_reasons
    reason = constructor.last_drop_reasons["NVDA"]
    assert "NVDA" in reason
    # 2026-09-12: "no analysis at all" is a DATA fault, not a trade the
    # constructor judged — the line says UNMEASURABLE, never "rejected",
    # and the fault is on `last_data_faults` for DecisionStage to file
    # under its own name (see tests/test_target_derivation.py).
    assert "UNMEASURABLE" in reason
    assert "rejected" not in reason
    assert constructor.last_data_faults["NVDA"]["fault"] == "analysis_missing"

    # A second call must not leak the first call's reasons onto a run that
    # dropped nothing — each call's capture is fresh, not cumulative.
    clean_analysis = _analysis("XLF", entry=100.0, stop=88.0, target=125.0)
    clean_target = TargetPosition(symbol="XLF", target_weight_pct=5.0, conviction="medium", thesis="clean")
    constructor.construct_orders(
        targets=[clean_target],
        positions=[],
        analyses=[clean_analysis],
        total_value=10_000.0,
        price_map={"XLF": 100.0},
    )
    assert constructor.last_drop_reasons == {}


# === thesis_invalid_if: dedicated field, not lossy text (2026-09-03) ===
#
# `TargetPosition.thesis_invalid_if` used to reach `TradeDecision` ONLY as
# text appended to `reasoning` — "(invalid if: ...)" / "(thesis_invalid_if:
# ...)" — and `reasoning` is truncated to 500 chars at every builder site
# below, then AGAIN to 280 chars by `TradingPipeline._build_position_
# history`. A long condition could silently fall past either cut. These
# tests prove the new `TradeDecision.thesis_invalid_if` field survives
# completely intact regardless of what happens to `reasoning`.

_LONG_INVALID_IF = (
    "This thesis is invalidated if the stock closes below the rising "
    "50-day simple moving average on a weekly closing basis for two "
    "consecutive weeks, OR if the company's next quarterly earnings "
    "report shows gross margin compression of more than 300 basis points "
    "year-over-year, OR if the sector rotation model flips this GICS "
    "sub-industry from leadership to laggard status for more than ten "
    "consecutive trading sessions, OR if a director or the CEO sells "
    "more than 2% of their disclosed beneficial ownership stake outside "
    "of a pre-scheduled 10b5-1 trading plan in a single reported window."
)
# Long enough to survive nothing: it blows past both the 500-char builder
# truncation AND the 280-char position-history truncation.
assert len(_LONG_INVALID_IF) > 500


def _extract_embedded_invalid_if(reasoning: str) -> str | None:
    """Mirrors how a downstream reader would regex the OLD embedded marker
    back out of `reasoning` — used here only to demonstrate that path is
    lossy once truncation has already run over it."""
    import re

    m = re.search(r"\((?:invalid if|thesis_invalid_if): (.*)\)\s*$", reasoning)
    return m.group(1) if m else None


def test_buy_thesis_invalid_if_survives_full_length_unlike_embedded_reasoning():
    constructor = PortfolioConstructor()
    target = TargetPosition(
        symbol="NVDA",
        target_weight_pct=8.0,
        conviction="high",
        thesis="AI leadership",
        thesis_invalid_if=_LONG_INVALID_IF,
    )
    analyses = [_analysis("NVDA", entry=100, stop=92, target=120)]
    decisions = constructor.construct_orders(
        targets=[target],
        positions=[],
        analyses=analyses,
        total_value=100_000,
        price_map={"NVDA": 100.0},
    )
    assert len(decisions) == 1
    d = decisions[0]
    assert d.action == "BUY"
    # The new field: complete, untruncated.
    assert d.thesis_invalid_if == _LONG_INVALID_IF

    # The old path really is lossy: `reasoning` is capped at 500 chars, so
    # the embedded marker's payload — appended AFTER the cut — never makes
    # it into the string a regex could extract at all.
    assert len(d.reasoning) <= 500 + 200  # + cap_note/target_note headroom
    embedded = _extract_embedded_invalid_if(d.reasoning)
    assert embedded is None or embedded != _LONG_INVALID_IF


def test_short_thesis_invalid_if_survives_full_length_unlike_embedded_reasoning():
    constructor = PortfolioConstructor()
    target = TargetPosition(
        symbol="TSLA",
        direction="short",
        target_weight_pct=5.0,
        conviction="high",
        thesis="overvalued",
        thesis_invalid_if=_LONG_INVALID_IF,
    )
    analysis = TechAnalysisResult(
        symbol="TSLA",
        rating="sell",
        entry_price=250.0,
        stop_loss=280.0,
        reference_target=200.0,
        reasoning="test",
        support_levels=[200.0],
        resistance_levels=[280.0],
        # 420 is a distant computed ceiling: a short needs a level ABOVE
        # entry to have a stop rest on ("no floor, no trade", 2026-09-12);
        # it is nowhere near the 280 stop, so the stop stays unbacked.
        computed_levels=[200.0, 420.0],
        computed_level_touches={200.0: 5, 420.0: 5},
        setup_type="range",
        expected_horizon_sessions=60,
        reasoning_chain=_tech_rc(),
        atr_14=(280.0 - 250.0) / 3.5,
        thesis_invalid_if="closes below support",
    )
    decisions = constructor.construct_orders(
        targets=[target],
        positions=[],
        analyses=[analysis],
        total_value=100_000,
        price_map={"TSLA": 250.0},
    )
    assert len(decisions) == 1
    d = decisions[0]
    assert d.action == "SHORT"
    assert d.thesis_invalid_if == _LONG_INVALID_IF
    embedded = _extract_embedded_invalid_if(d.reasoning)
    assert embedded is None or embedded != _LONG_INVALID_IF


def test_sell_thesis_invalid_if_survives_full_length_unlike_embedded_reasoning():
    constructor = PortfolioConstructor()
    positions = [_pos("AAPL", qty=50, avg_entry=180, current_price=200)]
    target = TargetPosition(
        symbol="AAPL",
        target_weight_pct=0.0,
        conviction="low",
        thesis="close — thesis broken",
        thesis_invalid_if=_LONG_INVALID_IF,
    )
    decisions = constructor.construct_orders(
        targets=[target],
        positions=positions,
        analyses=[],
        total_value=100_000,
    )
    assert len(decisions) == 1
    d = decisions[0]
    assert d.action == "SELL"
    assert d.thesis_invalid_if == _LONG_INVALID_IF
    # reasoning is no longer capped, so the full condition is stored in it too.
    assert _LONG_INVALID_IF in d.reasoning


def test_hold_and_no_condition_leave_the_field_none():
    """No stated condition → the field is None, not an empty string, on
    every action — matching the conviction-ledger fields' own discipline.

    target_weight_pct matches the held 8.1% exactly (no floor involved,
    board item 183): this is a HOLD because nothing was asked to change,
    not because a small delta was refused."""
    constructor = PortfolioConstructor()
    positions = [_pos("NVDA", qty=81, avg_entry=100, current_price=100)]
    target = TargetPosition(symbol="NVDA", target_weight_pct=8.1, conviction="high", thesis="keep")
    decisions = constructor.construct_orders(
        targets=[target],
        positions=positions,
        analyses=[],
        total_value=100_000,
        price_map={"NVDA": 100.0},
    )
    assert len(decisions) == 1
    assert decisions[0].action == "HOLD"
    assert decisions[0].thesis_invalid_if is None


# --------------------------------------------------------------------------
# `real_reward_risk_preview` — 2026-09-04 fix (audit finding).
#
# The PM's own eligibility gate used to read `TechAnalysisResult.
# risk_reward`, real arithmetic but over the analyst's own GUESSED target,
# never checked against structure. This method gives that earlier gate the
# SAME derived-target, noise-floor-widened number `construct_orders` gates
# on, by calling the same `_derive_target` / `_widen_stop_past_noise`
# rather than a second copy of the logic. These tests hand-verify the
# arithmetic on the same entry/stop/ATR fixture shape `_analysis` above
# uses, with `computed_levels` chosen to land exactly on a directly
# reachable structural level — no measured-move sqrt term to hand-check.
# --------------------------------------------------------------------------


def _structured_analysis(
    symbol: str,
    *,
    entry: float,
    stop: float,
    model_target: float,
    computed_levels: list[float],
    rating: str = "buy",
    horizon: int = 60,
) -> TechAnalysisResult:
    """Same ATR convention as `_analysis`: `(entry - stop) / 3.5` sits the
    unwidened stop just outside the noise band (§12.1's `OUTSIDE_BAND`
    path), so the stop the preview measures against is `stop`, unchanged.
    `model_target` never affects the derivation — see `derive_structural_
    target` — it only sets `TechAnalysisResult.risk_reward`, the self-
    reported figure this fix stops trusting.
    """
    is_short = rating in ("sell", "strong_sell")
    return TechAnalysisResult(
        symbol=symbol,
        rating=rating,
        conviction="medium",
        entry_price=entry,
        stop_loss=stop,
        reference_target=model_target,
        support_levels=[stop],
        resistance_levels=[model_target],
        computed_levels=computed_levels,
        atr_14=abs(entry - stop) / 3.5,
        setup_type="range",
        expected_horizon_sessions=horizon,
        reasoning="test",
        reasoning_chain=_tech_rc(),
        thesis_invalid_if="closes below support",
    )


def test_real_preview_reports_the_real_number_the_model_overstates():
    """Model claims R/R 10.0 (target $150 off a $5 stop). The nearest REAL
    structural level above entry is $103 — reward $3 / risk $5 = 0.60.

    **Changed 2026-09-11 (docs/WORK.md item 1(d)).** The preview used to
    return None here — not because 0.60 was unmeasurable but because the
    preview itself refused anything under the floor, so "thin payoff" and
    "no payoff arithmetic" arrived at the caller as the same answer. They
    are now distinguished: the real 0.60 comes back and does its work in
    the ranking, and only a genuinely unmeasurable geometry returns None."""
    analysis = _structured_analysis(
        "NVDA",
        entry=100.0,
        stop=95.0,
        model_target=150.0,
        computed_levels=[95.0, 103.0],
    )
    assert analysis.risk_reward == 10.0  # the self-reported figure — overstated
    constructor = PortfolioConstructor()
    real_rr = constructor.real_reward_risk_preview(analysis, "long")
    assert real_rr == 0.6


def test_real_preview_includes_a_candidate_the_model_understates():
    """Model claims R/R 0.8 (target $104 off a $5 stop) — sub-floor.
    The nearest REAL structural level above entry is $108, directly
    reachable — reward $8 / risk $5 = 1.60, clearing the 1.5 floor. The
    self-reported number would have pruned this at the OLD gate; the real
    one must keep it."""
    analysis = _structured_analysis(
        "GEV",
        entry=100.0,
        stop=95.0,
        model_target=104.0,
        computed_levels=[95.0, 108.0],
    )
    assert analysis.risk_reward == 0.8  # the self-reported figure — understated
    constructor = PortfolioConstructor()
    real_rr = constructor.real_reward_risk_preview(analysis, "long")
    assert real_rr == 1.6


def test_real_preview_mirrors_the_shorts_construction_would_take():
    """Direction-aware, same as every other §12.1 rule: a short's nearest
    real level is BELOW entry. Entry $100 / stop $105 (risk $5) / nearest
    computed level $92 (reward $8) — R/R 1.60, clears the floor — while the
    model's own guessed target of $99 would self-report as sub-floor
    (R/R 0.2)."""
    analysis = _structured_analysis(
        "TSLA",
        entry=100.0,
        stop=105.0,
        model_target=99.0,
        computed_levels=[105.0, 92.0],
        rating="sell",
    )
    assert analysis.risk_reward == 0.2
    constructor = PortfolioConstructor()
    real_rr = constructor.real_reward_risk_preview(analysis, "short")
    assert real_rr == 1.6


def test_real_preview_is_none_with_no_computed_structure():
    """A fixture with no `computed_levels` (e.g. a pre-§12.1 recording, or
    a name with too little price history to compute structure) refuses —
    same fail-closed contract as `derive_structural_target` itself, never
    silently permitted through."""
    analysis = _analysis("NVDA", entry=100, stop=95, target=150)
    analysis.computed_levels = []
    constructor = PortfolioConstructor()
    assert constructor.real_reward_risk_preview(analysis, "long") is None
