"""docs/WORK.md item 54, 2026-09-12 — the stop is always derivable, and the
gate is on its WIDTH, not on whether a level exists under it.

What shipped in the morning (PR #330, "no floor, no trade") was replaced the
same day on sourced research, before it ran a session:

  * No published method refuses a trade for lack of support below.
    Chandelier, Parabolic SAR, the Darvas box bottom and Kullamägi's
    entry-bar low all place a stop with no level at all
    (https://qullamaggie.com/my-3-timeless-setups-that-have-made-me-tens-of-millions/).
  * The gap branch was backwards, and it is measured: a rising window holds
    as support only ~20% of the time (Bulkowski,
    https://thepatternsite.com/GaugingGaps.html), so the level beneath a
    gap is MORE reachable, not void.
  * The rule was adversely selected: nearness to the 52-week high forecasts
    returns (George & Hwang 2004, Journal of Finance,
    https://www.bauer.uh.edu/tgeorge/papers/gh4-paper.pdf) and the rule
    refused exactly that population.
  * What published practice constrains is the stop's WIDTH; the answer to
    a wide stop is a smaller position (Van Tharp sizing), not refusal.
    AMENDED 2026-10-01 (owner ruling, board item 218): that is still the
    answer to WIDTH, and width alone. A separate rule, measured where the
    trade as a whole is accepted or declined, refuses a purchase whose
    reward is smaller than its risk. WHICH FIXTURES WERE MOVED, AND WHY,
    stated per fixture because "it went green" is not a reason:

      * `test_the_band_itself_still_ships_at_every_horizon`,
        `test_the_owners_gap_example_now_ships_and_is_answered_by_SIZE`
        and every other case in this file: UNTOUCHED. They pass for the
        original reason — a wide stop ships and is answered by size.
      * `test_a_stop_past_the_instruments_reach_now_ships`,
        `test_the_eligibility_preview_refuses_nothing_on_width` (level
        110 -> 130) and `test_the_gate_no_longer_refuses_a_short_either`
        (level 90 -> 70): MOVED, and the honest reason is that these
        three fixtures put $20 of risk against $10 of reward at a level
        INSIDE the instrument's reach, which is exactly the geometry the
        owner's 2026-10-01 ruling refuses. For that geometry the old rule
        and the new one genuinely contradict, and the owner's ruling
        wins. The fixtures were moved so the three keep testing the
        deleted WIDTH cap — the stop is still $20 wide and still past
        `horizon_reach`, which is the thing under test — instead of
        silently re-testing the new rule. Nothing was weakened: every
        assertion in them is unchanged.

Pinned here: (1) the level scan's relevance window is still read from the
instrument (kept from #330); (2) an unbacked or missing stop is read from
the instrument — the wider of the ATR noise band and the signal bar's far
edge; (3) **the width REFUSAL is GONE** (board item 56, route (c),
2026-09-26) and cannot come back silently — see
`TestTheWidthGateIsDeletedAndCannotComeBack`; (4) a listing too young to
measure is refused by its own code, first; (5) a wider stop buys a smaller
position at the same dollars of risk, and that is now the ONLY answer to
width; (6) nothing anywhere refuses for absent structure any more, and the
gap branch is gone.

**2026-09-26, docs/WORK.md item 56, route (c) — the width gate is deleted.**
The question the item narrowed to was "at what probability of being touched
inside the trade's own horizon does a stop stop being a stop". Three routes
were open; this is why the other two were rejected and this one taken.

  * Route (a), a CITED touch probability: no published work measures that
    quantity. What the stop-loss literature measures is the RETURN and
    VOLATILITY effect of a stop threshold (Acar & Toffel 2000; Han, Zhou &
    Zhu on momentum stop-losses; Kaminski & Lo), never a minimum touch
    probability below which a level stops counting as a stop. A different
    quantity, so it is not adopted.
  * Route (b), the THRESHOLD-FREE reformulation the item proposed — refuse
    when the stop's touch probability is below the target's reach
    probability on the same instrument — is NOT threshold-free. Both
    probabilities read the same ATR over the same horizon, and
    `touch_probability` is strictly decreasing in width, so the inequality
    is exactly `stop_distance > target_distance`: a reward:risk floor of
    1.0 wearing a probability costume. The desk deleted its reward:risk
    floor on 2026-09-24 (board item 81) and the owner ruled twice
    (2026-09-11, restated 2026-09-17) that a breakout setup gets no
    reward-side refusal at all. Pinned by
    `test_the_threshold_free_reformulation_is_a_reward_risk_floor`.
  * Route (c), DELETE, is what shipped, on a measurement: across 648 sized
    stops recorded in production between 2026-09-13 and 2026-09-26
    (`quant_agent.log`, the item-56 touch-probability reading) the gate
    refused ZERO trades; the widest stop it ever saw sat at 1.29 x ATR x
    sqrt(H) against its 1.5 cap; the lowest touch probability ever recorded
    was 4.0% against a gate that refuses at 1.67%. It never protected
    anything that `_plan_risk_targets` sizing down (spec 2.1) and, at the
    extreme, `position_sized_to_zero` do not already answer.

The READING survives, unchanged and still stamped on every sized stop: it
is the evidence that could one day answer the question the gate pretended
to have answered.
"""

from __future__ import annotations

import json
from datetime import date, timedelta

import pytest

from src.data import levels as levels_module
from src.data.levels import (
    MAX_HORIZON_SESSIONS,
    MAX_REACH_ATR_MULTIPLE,
    derive_structural_target,
    find_structural_levels,
    horizon_reach,
    structural_floor,
)
from src.data.technical import LONGEST_INDICATOR_WINDOW, atr_series
from src.models import OHLCV, TargetPosition, TechAnalysisResult, TechReasoningChain
from src.portfolio_constructor import (
    CONSTRUCTOR_REFUSED_EVENT_REASON,
    STOP_REFUSAL_NO_STRUCTURAL_STOP_NO_VOLATILITY,
    STOP_REFUSAL_WIDER_THAN_REACH,
    PortfolioConstructor,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _bars(prices: list[float], *, spread: float) -> list[OHLCV]:
    start = date(2024, 1, 1)
    return [
        OHLCV(date=start + timedelta(days=i), open=c, high=c + spread, low=c - spread, close=c, volume=1_000_000)
        for i, c in enumerate(prices)
    ]


def _oscillation(low: float, high: float, cycles: int, period: int = 12) -> list[float]:
    path: list[float] = []
    half = period // 2
    for _ in range(cycles):
        path += [low + (high - low) * i / half for i in range(half)]
        path += [high - (high - low) * i / half for i in range(half)]
    return path


def _rc() -> TechReasoningChain:
    return TechReasoningChain(
        trend="x",
        momentum="x",
        volatility="x",
        volume="x",
        support_resistance="x",
    )


def _analysis(
    symbol,
    *,
    entry,
    stop,
    levels,
    rating="buy",
    setup="range",
    target=None,
    atr=2.0,
    horizon=20,
    bars=None,
    bar_low=None,
    bar_high=None,
) -> TechAnalysisResult:
    """`computed_levels` is exactly `levels` — the desk's own scan output,
    attached in Python. `bars_available`, `signal_bar_low/high` are the
    other Python-set fields item 54 added; None means "not recorded"."""
    if target is None:
        target = round(entry * (0.9 if rating in ("sell", "strong_sell") else 1.1), 2)
    a = TechAnalysisResult(
        symbol=symbol,
        rating=rating,
        entry_price=entry,
        stop_loss=stop,
        reference_target=target,
        reasoning="test",
        support_levels=[stop],
        resistance_levels=[target] if target else [],
        computed_levels=list(levels),
        computed_level_touches={p: 5 for p in levels},
        setup_type=setup,
        expected_horizon_sessions=horizon,
        atr_14=atr,
        reasoning_chain=_rc(),
        thesis_invalid_if="closes below support",
    )
    a.bars_available = bars
    a.signal_bar_low = bar_low
    a.signal_bar_high = bar_high
    return a


def _target(symbol, direction="long", risk_pct=None) -> TargetPosition:
    if risk_pct is not None:
        return TargetPosition(
            symbol=symbol,
            direction=direction,
            risk_allocation_pct=risk_pct,
            conviction="high",
            thesis="t",
        )
    return TargetPosition(
        symbol=symbol,
        direction=direction,
        target_weight_pct=15.0,
        conviction="high",
        thesis="t",
    )


def _orders(constructor, analysis, direction="long", risk_pct=None, price=None):
    return constructor.construct_orders(
        targets=[_target(analysis.symbol, direction, risk_pct)],
        positions=[],
        analyses=[analysis],
        total_value=100_000,
        price_map={analysis.symbol: price or analysis.entry_price},
    )


# ---------------------------------------------------------------------------
# 1. The relevance window is read from the instrument (kept from #330)
# ---------------------------------------------------------------------------


class TestRelevanceWindowIsReadFromTheInstrument:
    def test_a_volatile_name_sees_a_shelf_a_quiet_name_cannot_reach(self):
        path = _oscillation(85.0, 100.0, cycles=4) + [100.0] * 80
        quiet_supports, _ = find_structural_levels(_bars(path, spread=0.3))
        violent_supports, _ = find_structural_levels(_bars(path, spread=4.0))
        assert not any(80 <= lv.price <= 90 for lv in quiet_supports), quiet_supports
        assert any(80 <= lv.price <= 90 for lv in violent_supports), violent_supports

    def test_the_window_is_the_target_derivations_own_reach_at_the_horizon_cap(self):
        atr = 2.35
        window = horizon_reach(atr, MAX_HORIZON_SESSIONS)
        derivation = derive_structural_target(
            entry_price=100.0,
            direction="long",
            levels=[90.0, 105.0],
            atr=atr,
            horizon_sessions=MAX_HORIZON_SESSIONS,
            setup_type="range",
        )
        assert derivation.horizon_reach == round(window, 4)
        assert window == atr * (MAX_HORIZON_SESSIONS**0.5) * MAX_REACH_ATR_MULTIPLE

    def test_the_window_is_bounded_by_the_horizon_cap_not_by_the_caller(self):
        assert horizon_reach(1.0, 100_000) == horizon_reach(1.0, MAX_HORIZON_SESSIONS)

    def test_no_volatility_reading_means_no_levels_not_a_guessed_window(self):
        bars = _bars(_oscillation(95.0, 100.0, cycles=1) + [100.0], spread=0.5)
        assert len(bars) < 14
        assert find_structural_levels(bars) == ([], [])


# ---------------------------------------------------------------------------
# 2. Nothing refuses for absent structure; the gap branch is gone
# ---------------------------------------------------------------------------


class TestNoFloorIsNotARefusal:
    def test_a_long_with_levels_only_overhead_ships_on_an_instrument_read_stop(self):
        """Two shelves overhead, nothing beneath — the case #330 refused.
        The analyst's stop has nothing computed under it, so it is widened
        to the ATR noise band, and the trade ships."""
        constructor = PortfolioConstructor()
        analysis = _analysis("NVDA", entry=100.0, stop=98.0, levels=[110.0, 120.0])
        decisions = _orders(constructor, analysis)
        assert [d.action for d in decisions] == ["BUY"]
        band = 100.0 - constructor._stop_atr_multiple() * 2.0
        assert decisions[0].stop_loss == round(band, 2)
        assert constructor.last_refusals == {}

    def test_a_short_with_levels_only_beneath_ships_the_same_way(self):
        constructor = PortfolioConstructor()
        analysis = _analysis(
            "TSLA",
            entry=100.0,
            stop=102.0,
            levels=[90.0, 80.0],
            rating="sell",
        )
        decisions = _orders(constructor, analysis, direction="short")
        assert [d.action for d in decisions] == ["SHORT"]
        band = 100.0 + constructor._stop_atr_multiple() * 2.0
        assert decisions[0].stop_loss == round(band, 2)
        assert constructor.last_refusals == {}

    def test_a_breakout_at_new_highs_with_nothing_beneath_ships(self):
        """The population George & Hwang found forecasts returns — a name
        at its highs with no floor the scan can name — is not refused."""
        constructor = PortfolioConstructor()
        analysis = _analysis(
            "OKLO",
            entry=100.0,
            stop=97.0,
            levels=[],
            setup="breakout",
            bars=LONGEST_INDICATOR_WINDOW,
        )
        # No levels at all is still `_derive_target`'s refusal (PR #326's
        # split owns that); give the chart one level OVERHEAD... no — a
        # breakout has none. Give it a level far below that backs nothing.
        analysis.computed_levels = [40.0]
        analysis.computed_level_touches = {40.0: 5}
        decisions = _orders(constructor, analysis)
        assert [d.action for d in decisions] == ["BUY"]
        assert "measured move" in decisions[0].reasoning
        assert constructor.last_refusals == {}

    def test_the_gap_branch_no_longer_exists_anywhere(self):
        """No `unfilled_gap_edge`, no `gap_edge=` on `structural_floor`, no
        gap-edge fields on the analysis. Bulkowski: a rising window holds
        as support ~20% of the time; the level beneath it is reachable."""
        assert not hasattr(levels_module, "unfilled_gap_edge")
        with pytest.raises(TypeError):
            structural_floor([50.0], 80.0, "long", gap_edge=50.0)  # type: ignore[call-arg]
        fields = TechAnalysisResult.model_fields
        assert "unfilled_up_gap_edge" not in fields
        assert "unfilled_down_gap_edge" not in fields
        assert {"signal_bar_low", "signal_bar_high", "bars_available"} <= set(fields)

    def test_the_owners_gap_example_now_ships_and_is_answered_by_SIZE(self):
        """$45-50 base, gap to $80. The pre-gap shelf IS the nearest floor
        (Bulkowski says it is the reachable one). An analyst leaning the
        stop on it asks for a stop 37% away. Until 2026-09-26 the WIDTH
        gate refused that trade; item 56 route (c) deleted the gate, so the
        trade now ships and the wide stop is paid for in shares — which is
        what published practice (Van Tharp sizing) actually prescribes and
        what the desk's own ratified §2.1 invariant already did under the
        cap. The refusal is NOT recorded, because it no longer exists."""
        pre = _oscillation(45.0, 50.0, cycles=5) + [50.0] * 10
        day_one = _bars(pre + [80.0], spread=0.6)
        supports, _ = find_structural_levels(day_one)
        shelf = structural_floor([lv.price for lv in supports], 80.0, "long")
        assert shelf is not None and 50 <= shelf <= 51
        atr = float(atr_series(day_one)[-1])
        horizon = 20
        reach = horizon_reach(atr, horizon)
        assert 80.0 - shelf > reach, "the numbers must still be the width case the old gate refused"

        constructor = PortfolioConstructor()
        analysis = _analysis(
            "GAPD",
            entry=80.0,
            stop=shelf,
            levels=[lv.price for lv in supports],
            atr=atr,
            horizon=horizon,
            setup="breakout",
            bars=LONGEST_INDICATOR_WINDOW,  # old enough: this is the width case
        )
        decisions = _orders(constructor, analysis, risk_pct=1.0)
        assert [d.action for d in decisions] == ["BUY"]
        assert STOP_REFUSAL_WIDER_THAN_REACH not in json.dumps(
            constructor.last_refusals,
            default=str,
        )
        # The wide stop is paid for in shares: risked dollars, not the
        # position, are what the desk holds constant.
        risked = decisions[0].allocation_pct / 100 * 100_000 * (80.0 - decisions[0].stop_loss) / 80.0
        assert risked == pytest.approx(1_000, rel=0.1)


# ---------------------------------------------------------------------------
# 3. The stop is always derivable
# ---------------------------------------------------------------------------


class TestTheStopIsAlwaysDerivable:
    def test_nothing_typed_means_the_band_not_a_refusal(self):
        constructor = PortfolioConstructor()
        analysis = _analysis("AMD", entry=100.0, stop=95.0, levels=[110.0])
        analysis.__dict__["stop_loss"] = None
        decisions = _orders(constructor, analysis)
        assert [d.action for d in decisions] == ["BUY"]
        band = 100.0 - constructor._stop_atr_multiple() * 2.0
        assert decisions[0].stop_loss == round(band, 2)

    def test_the_signal_bar_wins_when_it_is_wider_than_the_band(self):
        """Kullamägi's placement: under the low of the entry bar. It only
        decides when that low sits past the band — a climactic bar."""
        constructor = PortfolioConstructor()
        probe = _analysis("BAR", entry=100.0, stop=98.0, levels=[110.0])
        band_edge = 100.0 - constructor._stop_atr_multiple() * 2.0
        wide_bar = _analysis("BAR", entry=100.0, stop=98.0, levels=[110.0], bar_low=band_edge - 3.0)
        placed = constructor._widen_stop_past_noise(
            "BAR",
            wide_bar,
            100.0,
            98.0,
            direction="long",
            target_price=110.0,
        )
        assert placed == pytest.approx(band_edge - 3.0)
        narrow_bar = _analysis("BAR", entry=100.0, stop=98.0, levels=[110.0], bar_low=band_edge + 1.0)
        placed = constructor._widen_stop_past_noise(
            "BAR",
            narrow_bar,
            100.0,
            98.0,
            direction="long",
            target_price=110.0,
        )
        assert placed == pytest.approx(band_edge)

    def test_the_short_mirror_uses_the_signal_bars_high(self):
        constructor = PortfolioConstructor()
        a = _analysis("SHRT", entry=100.0, stop=102.0, levels=[90.0], rating="sell")
        band_edge = 100.0 + constructor._stop_atr_multiple() * 2.0
        a.signal_bar_high = band_edge + 2.0
        placed = constructor._widen_stop_past_noise(
            "SHRT",
            a,
            100.0,
            102.0,
            direction="short",
            target_price=90.0,
        )
        assert placed == pytest.approx(band_edge + 2.0)

    def test_no_atr_and_nothing_typed_is_still_no_stop(self):
        """The one case nothing can be read: no volatility reading. The
        caller rejects None, as before — nothing is invented from a flat
        percentage."""
        constructor = PortfolioConstructor()
        a = _analysis("NOVOL", entry=100.0, stop=95.0, levels=[110.0], atr=None)
        assert (
            constructor._widen_stop_past_noise(
                "NOVOL",
                a,
                100.0,
                None,
                direction="long",
                target_price=110.0,
            )
            is None
        )


# ---------------------------------------------------------------------------
# 4. The width gate is DELETED (board item 56, route (c), 2026-09-26) and
#    cannot come back silently
# ---------------------------------------------------------------------------


class TestTheWidthGateIsDeletedAndCannotComeBack:
    def test_a_stop_past_the_instruments_reach_now_ships(self):
        """ATR 2, horizon 20: the old reach cap was 2 x sqrt(20) x 1.5 =
        13.42 and a level-backed stop $20 away was refused as "a stop price
        cannot reach". It ships now. Nothing is recorded as a refusal."""
        constructor = PortfolioConstructor()
        # Level moved 110.0 -> 130.0 on 2026-10-01 (owner ruling, board
        # item 218). This test is about WIDTH and must stay about width:
        # $20 of risk against $10 of reward is now refused on the separate
        # parity rule, which would have made this assertion pass or fail
        # for the wrong reason. $30 of reward keeps the geometry sound so
        # the only thing under test is still the deleted width cap.
        a = _analysis("WIDE", entry=100.0, stop=80.0, levels=[80.0, 130.0])
        assert 20.0 > horizon_reach(2.0, 20), "still the width case"
        decisions = _orders(constructor, a, risk_pct=1.0)
        assert [d.action for d in decisions] == ["BUY"]
        assert constructor.last_refusals == {}

    def test_width_is_answered_by_size_at_every_width_including_past_reach(self):
        """Same dollars of risk, twice the stop distance, half the shares —
        §2.1, the Van Tharp arithmetic. This used to hold only UNDER the
        cap; with the cap gone it is the whole answer to width, and it
        keeps holding past where the gate used to refuse."""
        constructor = PortfolioConstructor()
        near = _analysis("NEAR", entry=100.0, stop=94.0, levels=[94.0, 140.0])
        far = _analysis("FAR", entry=100.0, stop=88.0, levels=[88.0, 140.0])
        past = _analysis("PAST", entry=100.0, stop=80.0, levels=[80.0, 140.0])
        assert 12.0 < horizon_reach(2.0, 20) < 20.0, "FAR must be inside the old cap and PAST outside it"
        got = {}
        for a in (near, far, past):
            d = _orders(constructor, a, risk_pct=1.0)
            assert [x.action for x in d] == ["BUY"], a.symbol
            got[a.symbol] = d[0]
        for sym, entry_stop in (("NEAR", 94.0), ("FAR", 88.0), ("PAST", 80.0)):
            d = got[sym]
            risked = d.allocation_pct / 100 * 100_000 * (100.0 - d.stop_loss) / 100.0
            assert risked == pytest.approx(1_000, abs=20), sym
        # Twice the distance, half the position — the whole mechanism.
        assert got["FAR"].allocation_pct == pytest.approx(
            got["NEAR"].allocation_pct / 2,
            abs=0.05,
        )
        assert constructor.last_refusals == {}

    def test_the_gate_no_longer_refuses_a_short_either(self):
        constructor = PortfolioConstructor()
        # Target moved 90.0 -> 70.0 on 2026-10-01 (owner ruling, board item
        # 218) for the same reason as the long case above: $20 of risk
        # against $10 of reward now fails the parity rule, and this test is
        # about the deleted WIDTH gate, not about geometry.
        a = _analysis("SHRT", entry=100.0, stop=120.0, levels=[120.0, 70.0], rating="sell")
        assert [d.action for d in _orders(constructor, a, direction="short", risk_pct=1.0)] == ["SHORT"]
        assert constructor.last_refusals == {}

    def test_the_eligibility_preview_refuses_nothing_on_width(self):
        constructor = PortfolioConstructor()
        a = _analysis("WIDE", entry=100.0, stop=80.0, levels=[80.0, 130.0])
        assert constructor.real_reward_risk_preview(a, "long") is not None
        assert constructor.last_refusals == {}

    def test_the_band_itself_still_ships_at_every_horizon(self):
        constructor = PortfolioConstructor()
        for horizon in (1, 2, 4, 10, 20, MAX_HORIZON_SESSIONS):
            a = _analysis("BAND", entry=100.0, stop=99.0, levels=[110.0], horizon=horizon, setup="breakout")
            placed = constructor._widen_stop_past_noise(
                "BAND",
                a,
                100.0,
                99.0,
                regime="risk-off",
                direction="long",
                target_price=110.0,
            )
            assert placed is not None, horizon
        assert constructor.last_refusals == {}

    # -- the "cannot come back silently" guards ---------------------------

    def test_no_live_code_emits_the_width_refusal_any_more(self):
        """The code is kept defined so old records stay attributable, and
        `scripts/blocked_proposals_census.py` must still be able to name
        it. Nothing under `src/` may EMIT it. If a future change
        reintroduces a width refusal, this fails and the reintroduction
        has to argue with board item 56 rather than slip in."""
        from pathlib import Path

        import ast

        root = Path(__file__).resolve().parents[1] / "src"
        offenders = []
        for path in root.rglob("*.py"):
            source = path.read_text()
            if "STOP_REFUSAL_WIDER_THAN_REACH" not in source:
                continue
            tree = ast.parse(source)
            assigned = {id(t) for node in ast.walk(tree) if isinstance(node, ast.Assign) for t in node.targets}
            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.Name)
                    and node.id == "STOP_REFUSAL_WIDER_THAN_REACH"
                    and id(node) not in assigned
                ):
                    offenders.append(f"{path.name}:{node.lineno}")
        assert not offenders, (
            "the deleted stop-width refusal is being emitted again; see "
            "docs/WORK.md item 56 / docs/INCIDENT_HISTORY.md 2026-09-26 "
            "before re-adding it:\n" + "\n".join(offenders)
        )

    def test_the_threshold_constant_is_gone_from_both_definition_sites(self):
        from src.config import RiskConfig
        from src.portfolio_constructor import ConstructorConfig

        assert "max_stop_width_reach_atr_multiple" not in RiskConfig.model_fields
        assert not hasattr(ConstructorConfig(), "max_stop_width_reach_atr_multiple")
        # The TARGET-side reach multiple is a different number and stays.
        assert RiskConfig.model_fields["max_target_reach_atr_multiple"].default == 1.5
        assert ConstructorConfig().max_target_reach_atr_multiple == 1.5

    def test_a_settings_file_still_carrying_the_key_fails_loudly(self):
        """`extra="ignore"` would let a stale settings.yaml load silently
        and an operator would believe a width refusal was in force."""
        from pathlib import Path

        import yaml

        from src.config import RiskConfig

        settings = Path(__file__).resolve().parents[1] / "config" / "settings.yaml"
        risk = yaml.safe_load(settings.read_text())["risk"]
        assert "max_stop_width_reach_atr_multiple" not in risk
        RiskConfig(**risk)  # the shipped file must still load
        with pytest.raises(Exception) as excinfo:
            RiskConfig(**{**risk, "max_stop_width_reach_atr_multiple": 1.5})
        assert "removed 2026-09-26" in str(excinfo.value)


# ---------------------------------------------------------------------------
# 5. Young listings — judged on stop readability, never on a bar count
#    (board item 180, owner ruling 2026-09-25: the count gate was DROPPED)
# ---------------------------------------------------------------------------


class TestYoungListingJudgedOnStopReadability:
    def test_a_short_history_name_with_a_readable_stop_is_admitted(self):
        """The owner's load-bearing requirement: a young listing is TRADEABLE
        when a defensible protective stop is readable from whatever bars
        exist. Thirty sessions, an ATR reading and levels — nothing the count
        gate would once have refused stops it now."""
        constructor = PortfolioConstructor()
        a = _analysis("DRAM", entry=59.0, stop=55.0, levels=[55.0, 65.0], bars=30)
        assert [d.action for d in _orders(constructor, a)] == ["BUY"]
        assert constructor.last_refusals == {}

    def test_a_single_bar_name_with_a_readable_stop_is_still_admitted(self):
        """The bar COUNT no longer gates at all: one completed session with a
        readable ATR-band stop and a target still ships."""
        constructor = PortfolioConstructor()
        a = _analysis("NEWCO", entry=100.0, stop=95.0, levels=[95.0, 110.0], bars=1)
        assert [d.action for d in _orders(constructor, a)] == ["BUY"]
        assert constructor.last_refusals == {}

    def test_a_bar_starved_name_is_refused_on_measurement_not_a_count(self):
        """The genuine skip case: a same-day IPO / near-zero bars yields no ATR
        reading, so the instrument cannot be measured. It is refused as a
        measurement DATA FAULT (`volatility_reading_missing`), NOT on a bar
        count -- nothing here counts sessions against a 200-bar floor."""
        constructor = PortfolioConstructor()
        a = _analysis("IPOD", entry=100.0, stop=95.0, levels=[110.0], atr=None, bars=0, bar_low=None)
        assert _orders(constructor, a) == []
        assert "IPOD" not in constructor.last_refusals
        fault = constructor.last_data_faults["IPOD"]
        assert fault["fault"] == "volatility_reading_missing"
        assert "session" not in fault["detail"] and "200" not in fault["detail"]

    def test_the_stop_readability_rule_refuses_when_no_stop_can_be_read(self):
        """The item-80 stop-readability gate in isolation: with no ATR and no
        structural level or signal-bar edge on the protective side of entry,
        the widener reads no stop from the instrument and refuses under
        `no_structural_stop_and_no_volatility_reading` -- on what the trade
        needs, never on a bar count."""
        constructor = PortfolioConstructor()
        a = _analysis("NOSTOP", entry=100.0, stop=95.0, levels=[110.0], atr=None, bars=0, bar_low=None)
        # `computed_levels` here holds only an overhead level (110), none on the
        # protective side of a long, and no signal-bar low.
        result = constructor._widen_stop_past_noise(
            "NOSTOP",
            a,
            100.0,
            95.0,
            direction="long",
            target_price=110.0,
        )
        assert result is None
        assert constructor.last_refusals["NOSTOP"]["refusal"] == STOP_REFUSAL_NO_STRUCTURAL_STOP_NO_VOLATILITY

    def test_the_count_gate_is_gone(self):
        """The removed refusal code and its method no longer exist."""
        import src.portfolio_constructor as pc

        assert not hasattr(pc, "STOP_REFUSAL_INSUFFICIENT_HISTORY")
        assert not hasattr(PortfolioConstructor, "_require_sufficient_history")

    def test_the_fields_default_to_not_recorded(self):
        """An older persisted row or a hand-built object carries None on
        all three Python-set fields; the constructor judges none of them."""
        a = _analysis("X", entry=100.0, stop=95.0, levels=[95.0])
        assert a.bars_available is None
        assert a.signal_bar_low is None and a.signal_bar_high is None


# ---------------------------------------------------------------------------
# 6. Recorded as data, read by the PM gate and the census
# ---------------------------------------------------------------------------


class TestRecordedAsData:
    def test_decision_stage_files_the_code_beside_the_reason(self, monkeypatch):
        from src import pipeline_stages

        events: list[dict] = []

        def _capture(pipeline, ctx, symbol, stage, outcome, reason="", **details):
            events.append({"symbol": symbol, "stage": stage, "outcome": outcome, "reason": reason, **details})

        monkeypatch.setattr(pipeline_stages, "_record_pipeline_event", _capture)
        constructor = PortfolioConstructor()
        constructor._note_refusal("WIDE", "long", STOP_REFUSAL_WIDER_THAN_REACH, "too wide")
        constructor._note_refusal("DRAM", "long", STOP_REFUSAL_NO_STRUCTURAL_STOP_NO_VOLATILITY, "no stop")
        refusals = constructor.drain_refusals()
        dropped = ["WIDE"]
        for sym in dropped:
            r = refusals.get(sym)
            pipeline_stages._record_pipeline_event(
                None,
                None,
                sym,
                "deterministic_gate",
                "blocked",
                CONSTRUCTOR_REFUSED_EVENT_REASON,
                refusal=r["refusal"],
                detail=r["detail"],
                targeted=True,
            )
        for sym, r in refusals.items():
            if sym not in dropped:
                pipeline_stages._record_pipeline_event(
                    None,
                    None,
                    sym,
                    "deterministic_gate",
                    "blocked",
                    CONSTRUCTOR_REFUSED_EVENT_REASON,
                    refusal=r["refusal"],
                    detail=r["detail"],
                    targeted=False,
                )
        by_symbol = {e["symbol"]: e for e in events}
        assert by_symbol["WIDE"]["refusal"] == STOP_REFUSAL_WIDER_THAN_REACH
        assert by_symbol["WIDE"]["targeted"] is True
        assert by_symbol["DRAM"]["refusal"] == STOP_REFUSAL_NO_STRUCTURAL_STOP_NO_VOLATILITY
        assert by_symbol["DRAM"]["targeted"] is False
        assert constructor.last_refusals == {}

    def test_the_census_attributes_each_code_to_its_own_bucket(self, tmp_path):
        from scripts.blocked_proposals_census import (
            _connect,
            _load_fills,
            _load_pairs,
            _load_recorded_reasons,
            _load_skips,
            _load_verdicts,
            classify,
        )
        from src.storage.db import Database

        db = Database(str(tmp_path / "census.db"))
        db.initialize()
        db.insert_specialist_evidence(
            run_id="r1",
            decision_id="d1",
            agent_name="portfolio_manager",
            kind="target",
            scope="symbol",
            symbol="WIDE",
            evidence_json=json.dumps({"symbol": "WIDE", "risk_allocation_pct": 1.0}),
        )
        db.insert_specialist_evidence(
            run_id="r1",
            decision_id="d1",
            agent_name="pipeline",
            kind="pipeline_event",
            scope="symbol",
            symbol="WIDE",
            evidence_json=json.dumps(
                {
                    "stage": "deterministic_gate",
                    "outcome": "blocked",
                    "reason": "constructor_refused",
                    "refusal": STOP_REFUSAL_WIDER_THAN_REACH,
                    "detail": "too wide",
                }
            ),
        )
        db.conn.commit()
        con = _connect(tmp_path / "census.db")
        try:
            reason = classify(
                "d1",
                "WIDE",
                ordered=_load_pairs(con, "proposed_order"),
                verdicts=_load_verdicts(con),
                skips=_load_skips(con),
                fills=_load_fills(con),
                recorded_reasons=_load_recorded_reasons(con),
            )
        finally:
            con.close()
        assert reason == f"constructor_refused:{STOP_REFUSAL_WIDER_THAN_REACH}"

    def test_pm_rule_r6_reads_the_constructors_snapshot_and_nothing_else(self):
        """R6 names a refusal the shared funnel already recorded. It does
        NOT look at the chart: a name with levels only overhead is not
        blocked when the constructor did not refuse it."""
        from src.agents.portfolio_manager import PortfolioManagerAgent

        no_floor = _analysis("NVDA", entry=100.0, stop=96.0, levels=[110.0, 120.0], setup="breakout")
        # The width refusal was deleted (item 56, 2026-09-26). Recorded
        # consequence, stated rather than hidden: `real_reward_risk_preview`
        # now records NO refusal at all for any input — the width gate was
        # its only live producer, and the remaining stop-readability refusal
        # needs a missing ATR, which the preview classifies one step earlier
        # as a DATA FAULT and returns None for. R6 itself is unchanged and
        # still reads whatever snapshot it is handed; the ENFORCING check
        # was always one stage later, in construction. So this pins the
        # wiring against a synthetic snapshot and pins the new emptiness.
        unreadable = _analysis(
            "DRAM", entry=100.0, stop=95.0, levels=[110.0], setup="breakout", atr=None, bars=0, bar_low=None
        )
        constructor = PortfolioConstructor()
        for a in (no_floor, unreadable):
            constructor.real_reward_risk_preview(a, "long")
        assert constructor.last_refusals == {}, (
            "no preview-time refusal exists any more; if one is added, wire this test back onto the live preview"
        )
        snapshot = {
            "DRAM": {
                "refusal": STOP_REFUSAL_NO_STRUCTURAL_STOP_NO_VOLATILITY,
                "direction": "long",
                "detail": "no stop can be read from the instrument",
            },
        }
        registry = {s: {"technical": "bullish", "news": "bullish"} for s in ("NVDA", "DRAM")}
        verdicts = PortfolioManagerAgent.candidate_eligibility(
            analyses=[no_floor, unreadable],
            evidence_registry=registry,
            active_state_changes="",
            allowed_buy_symbols={"NVDA", "DRAM"},
            constructor_refusals_by_symbol=snapshot,
        )
        assert not any(r.startswith("R6") for r in verdicts["NVDA"]), verdicts
        assert any(
            r.startswith("R6") and STOP_REFUSAL_NO_STRUCTURAL_STOP_NO_VOLATILITY in r for r in verdicts["DRAM"]
        ), verdicts
        # Without the snapshot R6 says nothing at all — no chart-shape rule.
        verdicts = PortfolioManagerAgent.candidate_eligibility(
            analyses=[no_floor, unreadable],
            evidence_registry=registry,
            active_state_changes="",
            allowed_buy_symbols={"NVDA", "DRAM"},
        )
        assert not any(r.startswith("R6") for v in verdicts.values() for r in v)


# ---------------------------------------------------------------------------
# docs/WORK.md item 56, 2026-09-13 — the target number and the refusal number
# are now two numbers, and a stop's width has a published reading.
# ---------------------------------------------------------------------------


class TestStopWidthReadingAndSeparation:
    """Item 56: one constant was estimating targets AND refusing trades.

    Nothing here ratifies a threshold — item 56 is still open. These pin
    (a) the two jobs are now two knobs, (b) the reading that converts a
    width into a probability is the published one and not a fitted curve,
    and (c) what the shipped multiple is actually worth.
    """

    def test_range_to_sigma_constant_is_the_gaussian_one(self):
        """`ATR_PER_SIGMA` is sqrt(8/pi) — a property of the Gaussian.

        Feller's range result, the one Parkinson (1980) builds the
        extreme-value variance estimator on. Not tunable, not fitted.
        """
        from src.data.levels import ATR_PER_SIGMA

        assert ATR_PER_SIGMA == pytest.approx((8.0 / 3.141592653589793) ** 0.5)
        assert ATR_PER_SIGMA == pytest.approx(1.5958, abs=1e-4)

    def test_touch_probability_matches_the_reflection_principle(self):
        """P(touch) = 2(1 - Phi(z)), z = width_atrs * ATR_PER_SIGMA / sqrt(H)."""
        import math

        from src.data.levels import ATR_PER_SIGMA, touch_probability

        for width, horizon in ((1.0, 1), (2.5, 20), (6.0, 60), (0.5, 5)):
            z = width * ATR_PER_SIGMA / math.sqrt(horizon)
            expected = math.erfc(z / math.sqrt(2.0))
            assert touch_probability(width, horizon) == pytest.approx(expected)

    def test_touch_probability_is_monotone_and_bounded(self):
        from src.data.levels import touch_probability

        assert touch_probability(0.0, 20) == pytest.approx(1.0)
        assert 0.0 < touch_probability(50.0, 20) < 1e-6
        widths = [0.5, 1.0, 2.0, 4.0, 8.0]
        probs = [touch_probability(w, 20) for w in widths]
        assert probs == sorted(probs, reverse=True)
        # Wider horizon, same width: MORE reachable.
        assert touch_probability(2.5, 60) > touch_probability(2.5, 5)

    def test_unreadable_inputs_return_none_rather_than_a_guess(self):
        from src.data.levels import touch_probability

        assert touch_probability(None, 20) is None
        assert touch_probability(2.5, None) is None
        assert touch_probability(2.5, 0) is None
        assert touch_probability(-1.0, 20) is None
        assert touch_probability(float("nan"), 20) is None
        assert touch_probability("wide", 20) is None

    def test_what_the_deleted_gate_was_actually_worth(self):
        """The arithmetic that made the deletion obvious, kept as a record.

        The old cap scaled with sqrt(H) exactly as the reading does, so the
        probability it refused at was the SAME at every horizon: 1.67%. It
        refused only a stop with under a 2% chance of being touched inside
        the trade — and the desk's own 2.5 x ATR fallback stop sits inside
        1.5 x ATR x sqrt(H) for every horizon of 3 sessions or more, so it
        could never refuse the desk's own fallback. Production agreed: over
        648 recorded stops (2026-09-13..2026-09-26) the LOWEST touch
        probability was 4.0% and the widest stop was 1.29 x ATR x sqrt(H).
        """
        import math

        from src.data.levels import touch_probability

        for horizon in (5, 20, 40, 60):
            p = touch_probability(1.5 * math.sqrt(horizon), horizon)
            assert p == pytest.approx(0.016681, abs=1e-5), (horizon, p)
        for horizon in range(3, 61):
            assert 2.5 <= 1.5 * math.sqrt(horizon), horizon
        assert 2.5 > 1.5 * math.sqrt(2)
        # The widest stop production ever produced (1.287 x ATR x sqrt(H))
        # still sat at more than twice the probability the gate refused at,
        # which is why 648 stops produced zero refusals.
        assert touch_probability(1.287 * math.sqrt(10), 10) > 2 * 0.016681

    def test_the_threshold_free_reformulation_is_a_reward_risk_floor(self):
        """Why route (b) was rejected — arithmetic, not opinion.

        Item 56 offered "refuse when the stop's touch probability is below
        the target's reach probability on the same instrument" as the
        threshold-free option. Both readings take the same ATR and the same
        horizon, and `touch_probability` is strictly decreasing in width,
        so the inequality reduces EXACTLY to `stop_distance >
        target_distance` — a reward:risk floor of 1.0 with no constant
        visible. The desk deleted its reward:risk floor on 2026-09-24
        (board item 81) and the owner ruled twice that a breakout setup
        gets no reward-side refusal, so adopting (b) would have
        reintroduced a refused rule while claiming to remove a number.
        """
        from src.data.levels import touch_probability

        for horizon in (5, 10, 20, 60):
            for stop_w in (0.5, 1.0, 2.0, 3.5, 6.0):
                for target_w in (0.5, 1.0, 2.0, 3.5, 6.0):
                    p_stop = touch_probability(stop_w, horizon)
                    p_target = touch_probability(target_w, horizon)
                    assert (p_stop < p_target) == (stop_w > target_w), (
                        horizon,
                        stop_w,
                        target_w,
                    )
