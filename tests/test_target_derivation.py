"""The take-profit is COMPUTED, not guessed (2026-09-01).

Reward:risk is `(target - entry) / (entry - stop)`. The stop has been derived
from measured volatility since 2026-08-27; the target was still the language
model's `reference_target`, so the gate divided a measurement by an opinion.
On the morning run of 2026-09-01 (`run-64290730`) that arithmetic put 30 of 38
actionable signals (79%) under the 1.5 floor before any judgement was applied,
including the two highest-conviction calls of the day, and the desk placed no
trades.

The floor is not the defect and is not touched here. These tests pin its
numerator: derived from the same bars the stop comes from, working in both
directions, and refusing BY NAME rather than falling back to a fabricated
default — a manufactured target with better provenance is still manufactured.
"""

from datetime import date, timedelta

from src.data.levels import (
    COVERAGE_MEASURED,
    COVERAGE_NO_BARS,
    COVERAGE_UNKNOWN,
    COVERAGE_UNUSABLE_BARS,
    FAULT_NO_ANALYSIS,
    FAULT_NO_ENTRY,
    FAULT_NO_STRUCTURE,
    FAULT_NO_VOLATILITY,
    MAX_HORIZON_SESSIONS,
    MIN_SCAN_BARS,
    derive_structural_target,
    find_structural_levels,
    structure_coverage,
)
from src.data.technical import ATR_PERIOD
from src.models import (
    OHLCV,
    TargetPosition,
    TechAnalysisResult,
    TechReasoningChain,
)
from src.portfolio_constructor import ConstructorConfig, PortfolioConstructor


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _bars(prices: list[float], *, spread: float = 0.4) -> list[OHLCV]:
    start = date(2024, 1, 1)
    return [
        OHLCV(
            date=start + timedelta(days=i),
            open=close,
            high=close + spread,
            low=close - spread,
            close=close,
            volume=1_000_000,
        )
        for i, close in enumerate(prices)
    ]


def _oscillation(low: float, high: float, cycles: int, period: int = 12) -> list[float]:
    """A path that repeatedly turns at `low` and `high` — the thing that makes
    a level a level rather than a coincidence."""
    path: list[float] = []
    half = period // 2
    for _ in range(cycles):
        path += [low + (high - low) * i / half for i in range(half)]
        path += [high - (high - low) * i / half for i in range(half)]
    return path


def _tech_rc() -> TechReasoningChain:
    return TechReasoningChain(
        trend="x",
        momentum="x",
        volatility="x",
        volume="x",
        support_resistance="x",
    )


def _analysis(
    *,
    symbol: str,
    rating: str,
    entry: float,
    stop: float,
    model_target: float,
    levels: list[float],
    atr: float,
    horizon: int = 20,
    setup: str = "range",
    computed: list[float] | None = None,
) -> TechAnalysisResult:
    """`levels` are the analyst's own (the validator requires at least one for
    an actionable rating); `computed` is what Python found over the full
    history. They are separate parameters because the interesting failure is
    when they DISAGREE — the model naming levels the chart does not support.
    """
    return TechAnalysisResult(
        symbol=symbol,
        rating=rating,
        entry_price=entry,
        stop_loss=stop,
        reference_target=model_target,
        reasoning="test",
        support_levels=[lv for lv in levels if lv < entry],
        resistance_levels=[lv for lv in levels if lv > entry],
        computed_levels=levels if computed is None else computed,
        atr_14=atr,
        setup_type=setup,
        expected_horizon_sessions=horizon,
        reasoning_chain=_tech_rc(),
        thesis_invalid_if="closes below support",
    )


# ---------------------------------------------------------------------------
# A computed target is produced for a realistic long AND a realistic short
# ---------------------------------------------------------------------------


class TestBothDirections:
    def test_a_long_targets_the_nearest_resistance_above_entry(self):
        """Bars in, level out. Nothing here consults a model."""
        bars = _bars(_oscillation(90.0, 110.0, cycles=20) + [96.0, 97.0, 98.0])
        supports, resistances = find_structural_levels(bars)
        levels = [lv.price for lv in (*supports, *resistances)]
        assert levels, "fixture must produce real structure"

        result = derive_structural_target(
            entry_price=98.0,
            direction="long",
            levels=levels,
            atr=2.0,
            horizon_sessions=25,
            setup_type="range",
            model_target=104.0,
        )
        assert result.price is not None
        assert result.basis == "structural_level"
        assert result.price > 98.0
        # The nearest computed level above entry, not the furthest and not
        # the model's number.
        assert result.price == min(lv for lv in levels if lv > 98.0 + 2.0)

    def test_a_short_targets_the_nearest_support_below_entry(self):
        """The mirror. A long-only implementation would be a failed one —
        15 of the 38 candidates on 2026-09-01 were bearish."""
        bars = _bars(_oscillation(90.0, 110.0, cycles=20) + [104.0, 103.0, 102.0])
        supports, resistances = find_structural_levels(bars)
        levels = [lv.price for lv in (*supports, *resistances)]
        assert levels

        result = derive_structural_target(
            entry_price=102.0,
            direction="short",
            levels=levels,
            atr=2.0,
            horizon_sessions=25,
            setup_type="range",
            model_target=96.0,
        )
        assert result.price is not None
        assert result.basis == "structural_level"
        assert result.price < 102.0
        assert result.price == max(lv for lv in levels if lv < 102.0 - 2.0)

    def test_the_two_directions_are_symmetric_about_the_same_chart(self):
        """Same levels, same volatility, same horizon — the only difference
        is which way the trade points."""
        levels = [80.0, 90.0, 110.0, 120.0]
        long_side = derive_structural_target(
            entry_price=100.0,
            direction="long",
            levels=levels,
            atr=3.0,
            horizon_sessions=25,
            setup_type="range",
        )
        short_side = derive_structural_target(
            entry_price=100.0,
            direction="short",
            levels=levels,
            atr=3.0,
            horizon_sessions=25,
            setup_type="range",
        )
        assert long_side.price == 110.0
        assert short_side.price == 90.0
        assert long_side.basis == short_side.basis == "structural_level"

    def test_the_constructor_ships_a_computed_take_profit_on_a_short(self):
        """End to end through the order path, not just the pure function."""
        constructor = PortfolioConstructor()
        analysis = _analysis(
            symbol="TSLA",
            rating="sell",
            entry=250.0,
            stop=262.5,
            model_target=150.0,
            levels=[220.0, 262.5, 300.0],
            atr=12.5 / 4.0,
            horizon=45,
        )
        decisions = constructor.construct_orders(
            targets=[
                TargetPosition(
                    symbol="TSLA",
                    direction="short",
                    target_weight_pct=15.0,
                    conviction="high",
                    thesis="overvalued",
                )
            ],
            positions=[],
            analyses=[analysis],
            total_value=100_000,
            price_map={"TSLA": 250.0},
        )
        assert len(decisions) == 1
        decision = decisions[0]
        assert decision.action == "SHORT"
        # The computed level, NOT the model's $150 guess.
        assert decision.take_profit == 220.0
        assert decision.take_profit < decision.entry_price
        assert decision.stop_loss > decision.entry_price


# ---------------------------------------------------------------------------
# Fail closed, by name — never a fabricated fallback
# ---------------------------------------------------------------------------


class TestRefusals:
    def test_too_few_bars_declines_rather_than_fabricating(self):
        """Four bars cannot produce structure. `find_structural_levels` says
        so honestly (empty lists), and the derivation must decline instead of
        inventing a default — a made-up target is the defect being removed.

        **2026-09-12:** four bars is not a chart the desk judged; it is a
        history the desk failed to obtain. So this declines as a DATA FAULT
        (`fault` set, `refusal` empty), still with no price and still
        carrying the model's guess as evidence only. (A listing merely too
        YOUNG is no longer refused on a bar count — board item 180 dropped
        that gate; a young name too bar-starved to read any stop from is
        refused later by the constructor's stop-readability rule.)"""
        bars = _bars([100.0] * 4)
        supports, resistances = find_structural_levels(bars)
        assert (supports, resistances) == ([], [])
        assert structure_coverage(bars) == COVERAGE_UNUSABLE_BARS

        result = derive_structural_target(
            entry_price=100.0,
            direction="long",
            levels=[lv.price for lv in (*supports, *resistances)],
            atr=2.0,
            horizon_sessions=20,
            setup_type="range",
            model_target=120.0,
            levels_coverage=structure_coverage(bars),
        )
        assert result.price is None
        assert result.refused
        assert result.unmeasurable
        assert result.fault == FAULT_NO_STRUCTURE
        assert result.refusal == ""
        assert "DATA FAULT" in result.detail
        assert COVERAGE_UNUSABLE_BARS in result.detail
        # The model's guess survives as evidence and is NOT promoted to the
        # answer just because nothing else was available.
        assert result.model_target == 120.0

    def test_no_volatility_reading_is_a_data_fault_not_a_refusal(self):
        """ATR is computed by the desk from its own bars; a real market
        always has volatility. Its absence is the desk's failure."""
        result = derive_structural_target(
            entry_price=100.0,
            direction="long",
            levels=[90.0, 115.0],
            atr=None,
            horizon_sessions=20,
            setup_type="range",
        )
        assert result.price is None
        assert result.fault == FAULT_NO_VOLATILITY
        assert result.refusal == ""

    def test_no_entry_price_is_a_data_fault_not_a_refusal(self):
        result = derive_structural_target(
            entry_price=None,
            direction="long",
            levels=[90.0, 115.0],
            atr=2.0,
            horizon_sessions=20,
            setup_type="range",
        )
        assert result.price is None
        assert result.fault == FAULT_NO_ENTRY
        assert result.refusal == ""

    def test_a_measured_chart_with_no_structure_trades_with_no_target(self):
        """The other half of the split, and the one that keeps this honest:
        enough clean bars for the pivot scan to run, and it found no level
        with the minimum touches within reach. That is a fact about the
        chart, not about the feed. Since 2026-10-09 (owner rule) it TRADES
        with no take-profit — never a refusal, never a fault."""
        result = derive_structural_target(
            entry_price=100.0,
            direction="long",
            levels=[],
            atr=2.0,
            horizon_sessions=20,
            setup_type="range",
            levels_coverage=COVERAGE_MEASURED,
        )
        assert result.price is None
        assert result.no_target and not result.refused
        assert result.refusal == ""
        assert result.fault == ""
        assert not result.unmeasurable
        assert "measured" in result.detail

    def test_every_non_measured_coverage_is_a_data_fault(self):
        """No bars, too few clean bars, and unknown provenance all mean
        the desk cannot claim the chart was measured. All three are
        faults; none is a refusal."""
        for coverage in (
            COVERAGE_NO_BARS,
            COVERAGE_UNUSABLE_BARS,
            COVERAGE_UNKNOWN,
        ):
            result = derive_structural_target(
                entry_price=100.0,
                direction="long",
                levels=[],
                atr=2.0,
                horizon_sessions=20,
                setup_type="range",
                levels_coverage=coverage,
            )
            assert result.fault == FAULT_NO_STRUCTURE, coverage
            assert result.refusal == "", coverage
            assert result.price is None, coverage

    def test_a_fault_and_a_refusal_never_coexist_and_never_trade(self):
        """The invariant the record depends on: a declined derivation has
        exactly ONE of `fault` / `refusal` set, and neither ever carries a
        price. If a future branch sets both, or sets a price beside
        either, the two classes have merged again."""
        cases = [
            dict(entry_price=None, atr=2.0, horizon_sessions=20, levels=[90.0]),
            dict(entry_price=100.0, atr=None, horizon_sessions=20, levels=[90.0]),
            dict(entry_price=100.0, atr=2.0, horizon_sessions=None, levels=[90.0]),
            dict(entry_price=100.0, atr=2.0, horizon_sessions=20, levels=[]),
            # (a measured empty chart is `no_target` since 2026-10-09 — it trades)
            dict(entry_price=100.0, atr=2.0, horizon_sessions=1, levels=[80.0], setup_type="breakout"),
        ]
        for case in cases:
            case.setdefault("setup_type", "range")
            result = derive_structural_target(direction="long", **case)
            assert result.price is None, case
            assert bool(result.fault) != bool(result.refusal), case

    def test_structure_coverage_is_read_from_the_bars(self):
        """The coverage states come from the history itself and from the
        scan's own minimum window — not from a chosen threshold."""
        assert structure_coverage(None) == COVERAGE_NO_BARS
        assert structure_coverage([]) == COVERAGE_NO_BARS
        assert structure_coverage(_bars([100.0] * 4)) == COVERAGE_UNUSABLE_BARS
        # The minimum is the scan's own: the ATR(14) warm-up the swing
        # detector needs plus the one bar that can confirm a turn (2026-10-15).
        assert MIN_SCAN_BARS == ATR_PERIOD + 1
        assert structure_coverage(_bars([100.0] * (MIN_SCAN_BARS - 1))) == COVERAGE_UNUSABLE_BARS
        assert structure_coverage(_bars([100.0] * MIN_SCAN_BARS)) == COVERAGE_MEASURED
        assert structure_coverage(_bars([100.0] * 40)) == COVERAGE_MEASURED
        # Enough bars arrived, but they cannot be true (high below low), so
        # cleaning leaves nothing the scan can run over: a dirty feed.
        from datetime import date, timedelta

        dirty = [
            OHLCV(date=date(2024, 1, 1) + timedelta(days=i), open=100.0, high=90.0, low=110.0, close=100.0, volume=1)
            for i in range(40)
        ]
        assert structure_coverage(dirty) == COVERAGE_UNUSABLE_BARS

    def test_no_horizon_refuses(self):
        result = derive_structural_target(
            entry_price=100.0,
            direction="long",
            levels=[90.0, 115.0],
            atr=2.0,
            horizon_sessions=None,
            setup_type="range",
        )
        assert result.refusal == "no_expected_horizon"

    def test_no_level_in_the_direction_now_earns_the_measured_move(self):
        """**Inverted 2026-09-11, docs/WORK.md funnel item 6.**

        Structure exists but none of it is overhead, and the analyst did not
        type the word "breakout". This used to be refused as "chart and read
        disagree". It is not a disagreement: the desk's OWN level computation
        succeeded (there are levels below) and found nothing in the trade's
        direction, which is a measured absence of a ceiling. The other
        reading of an empty direction — an unreadable chart — is caught one
        branch earlier as `no_structure`.

        So the ATR measured-move projection that already existed in this
        function, and was reachable only through the label, now applies on
        the real condition. 2.0 ATR x sqrt(20) x 1.0 = $8.94 -> $108.94.

        The classification is the SAME one the reward:risk exemption uses
        (`src.risk.constants.is_trend_trade`), which is the point: a trade
        with no ceiling gets a projected target AND is not judged against a
        reward:risk floor, by one definition rather than two."""
        result = derive_structural_target(
            entry_price=100.0,
            direction="long",
            levels=[80.0, 90.0],
            atr=2.0,
            horizon_sessions=20,
            setup_type="range",
        )
        assert not result.refusal
        assert result.basis == "measured_move"
        assert result.price == round(100.0 + 2.0 * (20**0.5), 2)
        assert result.level_used is None
        assert "nothing overhead is expected to stop this trade" in result.detail

    def test_an_unreadable_chart_is_still_declined_and_is_not_this_case(self):
        """The distinction funnel item 6's fix rests on. No levels AT ALL
        is not "no ceiling", and it still declines whatever the setup says
        — as a data fault when the history was too short or too dirty
        (2026-09-12), as a refusal when the chart was measured and holds
        nothing. Neither earns the measured move."""
        unusable = derive_structural_target(
            entry_price=100.0,
            direction="long",
            levels=[],
            atr=2.0,
            horizon_sessions=20,
            setup_type="breakout",
        )
        assert unusable.price is None
        assert unusable.basis != "measured_move"
        assert unusable.fault == FAULT_NO_STRUCTURE
        measured = derive_structural_target(
            entry_price=100.0,
            direction="long",
            levels=[],
            atr=2.0,
            horizon_sessions=20,
            setup_type="breakout",
            levels_coverage=COVERAGE_MEASURED,
        )
        assert measured.price is None
        assert measured.basis != "measured_move"
        assert measured.no_target and measured.refusal == ""

    def test_the_trend_classification_is_shared_with_the_reward_risk_gate(self):
        """One definition, asserted as one function. If someone adds a second
        way to decide "this trade has no ceiling", this fails first."""
        from src.risk.constants import is_trend_trade, reward_risk_floor_applies

        # The label alone.
        assert is_trend_trade("breakout") is True
        assert reward_risk_floor_applies("breakout") is False
        # The measurement alone — no label, no ceiling found.
        assert is_trend_trade("range", structural_ceiling=False) is True
        assert reward_risk_floor_applies("range", structural_ceiling=False) is False
        # A real ceiling, no label: the floor machinery still applies.
        assert is_trend_trade("range", structural_ceiling=True) is False
        assert reward_risk_floor_applies("range", structural_ceiling=True) is True
        # Unknown on both counts fails to the conservative side.
        assert is_trend_trade(None) is False
        assert reward_risk_floor_applies(None) is True

    def test_each_decline_names_a_different_thing_being_wrong(self):
        """'No trade' without a reason is what let the original defect
        survive unnoticed. A missing ATR, a missing horizon, an unreadable
        chart, a measured-but-empty chart and a projection that cannot
        clear its own noise are five different problems and must not share
        one code — across BOTH fields, since 2026-09-12 split them."""

        def _code(result):
            return result.fault or result.refusal

        codes = {
            _code(
                derive_structural_target(
                    entry_price=100.0,
                    direction="long",
                    levels=[90.0, 115.0],
                    atr=None,
                    horizon_sessions=20,
                    setup_type="range",
                )
            ),
            _code(
                derive_structural_target(
                    entry_price=100.0,
                    direction="long",
                    levels=[90.0, 115.0],
                    atr=2.0,
                    horizon_sessions=0,
                    setup_type="range",
                )
            ),
            _code(
                derive_structural_target(
                    entry_price=100.0, direction="long", levels=[], atr=2.0, horizon_sessions=20, setup_type="range"
                )
            ),
            _code(
                derive_structural_target(
                    entry_price=100.0,
                    direction="long",
                    levels=[],
                    atr=2.0,
                    horizon_sessions=20,
                    setup_type="range",
                    levels_coverage=COVERAGE_MEASURED,
                )
            ),
            # A projection too small to clear its own noise floor. (It used
            # to be "no level in the direction on a range setup"; funnel
            # item 6 made that a measured move rather than a refusal.)
            _code(
                derive_structural_target(
                    entry_price=100.0,
                    direction="long",
                    levels=[80.0],
                    atr=2.0,
                    horizon_sessions=1,
                    setup_type="breakout",
                )
            ),
        }
        assert len(codes) == 5

    def test_the_constructor_declines_the_order_when_derivation_refuses(self):
        """The analyst named levels; Python found none in the history. The
        model's word is not sufficient to open a position."""
        constructor = PortfolioConstructor()
        analysis = _analysis(
            symbol="NVDA",
            rating="buy",
            entry=100.0,
            stop=95.0,
            model_target=130.0,
            levels=[95.0, 130.0],
            computed=[],
            atr=1.4,
            horizon=30,
        )
        decisions = constructor.construct_orders(
            targets=[
                TargetPosition(
                    symbol="NVDA",
                    target_weight_pct=15.0,
                    conviction="high",
                    thesis="AI",
                )
            ],
            positions=[],
            analyses=[analysis],
            total_value=100_000,
            price_map={"NVDA": 100.0},
        )
        assert decisions == []


# ---------------------------------------------------------------------------
# A data fault is not a trade refusal (2026-09-12)
# ---------------------------------------------------------------------------
#
# The owner's point: a broken feed and a trade that failed its rules used to
# produce the same class of outcome, so nobody could tell them apart in the
# record and nobody knew how often either happened. These pin the split at
# the constructor: a fault lands on `last_data_faults` (and is filed as
# `data_fault` by `DecisionStage`, never `constructor_dropped`), a refusal
# does not; and NEITHER trades.


class TestDataFaultsAtTheConstructor:
    def _target(self, symbol="NVDA", direction="long"):
        return TargetPosition(
            symbol=symbol,
            direction=direction,
            target_weight_pct=15.0,
            conviction="high",
            thesis="t",
        )

    def test_unusable_history_is_recorded_as_a_fault_and_not_traded(self):
        constructor = PortfolioConstructor()
        analysis = _analysis(
            symbol="NVDA",
            rating="buy",
            entry=100.0,
            stop=95.0,
            model_target=130.0,
            levels=[95.0, 130.0],
            computed=[],
            atr=1.4,
            horizon=30,
        )
        analysis.levels_coverage = COVERAGE_UNUSABLE_BARS
        decisions = constructor.construct_orders(
            targets=[self._target()],
            positions=[],
            analyses=[analysis],
            total_value=100_000,
            price_map={"NVDA": 100.0},
        )
        assert decisions == []  # fail-closed, unchanged
        assert constructor.last_data_faults["NVDA"]["fault"] == FAULT_NO_STRUCTURE
        # Still captured as a drop (never silently absent), but the line
        # says what it is.
        assert "UNMEASURABLE" in constructor.last_drop_reasons["NVDA"]
        assert "rejected" not in constructor.last_drop_reasons["NVDA"]

    def test_missing_atr_is_recorded_as_a_fault(self):
        constructor = PortfolioConstructor()
        analysis = _analysis(
            symbol="NVDA",
            rating="buy",
            entry=100.0,
            stop=95.0,
            model_target=130.0,
            levels=[95.0, 130.0],
            atr=1.4,
            horizon=30,
        )
        analysis.atr_14 = None
        decisions = constructor.construct_orders(
            targets=[self._target()],
            positions=[],
            analyses=[analysis],
            total_value=100_000,
            price_map={"NVDA": 100.0},
        )
        assert decisions == []
        assert constructor.last_data_faults["NVDA"]["fault"] == FAULT_NO_VOLATILITY

    def test_no_analysis_at_all_is_recorded_as_a_fault(self):
        constructor = PortfolioConstructor()
        decisions = constructor.construct_orders(
            targets=[self._target()],
            positions=[],
            analyses=[],
            total_value=100_000,
            price_map={"NVDA": 100.0},
        )
        assert decisions == []
        assert constructor.last_data_faults["NVDA"]["fault"] == FAULT_NO_ANALYSIS

    def test_no_price_anywhere_is_recorded_as_a_fault(self):
        constructor = PortfolioConstructor()
        analysis = _analysis(
            symbol="NVDA",
            rating="buy",
            entry=100.0,
            stop=95.0,
            model_target=130.0,
            levels=[95.0, 130.0],
            atr=1.4,
            horizon=30,
        )
        analysis.entry_price = None
        decisions = constructor.construct_orders(
            targets=[self._target()],
            positions=[],
            analyses=[analysis],
            total_value=100_000,
            price_map={},  # no live quote either
        )
        assert decisions == []
        assert constructor.last_data_faults["NVDA"]["fault"] == FAULT_NO_ENTRY

    def test_a_measured_empty_chart_trades_and_is_not_a_fault(self):
        """The negative case that keeps the fault list honest: a chart the
        desk measured and found structureless is a trade judgement. It is
        dropped and its reason captured, but it must NOT appear among the
        data faults or the owner would be paged for a quiet chart."""
        constructor = PortfolioConstructor()
        analysis = _analysis(
            symbol="NVDA",
            rating="buy",
            entry=100.0,
            stop=95.0,
            model_target=130.0,
            levels=[95.0, 130.0],
            computed=[],
            atr=1.4,
            horizon=30,
        )
        analysis.levels_coverage = COVERAGE_MEASURED
        decisions = constructor.construct_orders(
            targets=[self._target()],
            positions=[],
            analyses=[analysis],
            total_value=100_000,
            price_map={"NVDA": 100.0},
        )
        # Owner rule 2026-10-09: a measured chart with no level trades on
        # its ATR stop with NO take-profit. Still not a data fault, and no
        # longer a refusal.
        assert [d.take_profit for d in decisions if d.action == "BUY"] == [None]
        assert constructor.last_data_faults == {}
        assert "NVDA" not in constructor.last_refusals

    def test_the_eligibility_preview_records_faults_too(self):
        """A symbol becomes unanalysable BEFORE the PM ever sees it: the
        eligibility preview runs the same derivation over every analysed
        name. Its faults must survive to the same record, or the quietest
        failure of all — a name that never even reaches a proposal — stays
        invisible."""
        constructor = PortfolioConstructor()
        analysis = _analysis(
            symbol="NVDA",
            rating="buy",
            entry=100.0,
            stop=95.0,
            model_target=130.0,
            levels=[95.0, 130.0],
            atr=1.4,
            horizon=30,
        )
        analysis.atr_14 = None
        assert constructor.real_reward_risk_preview(analysis, "long") is None
        assert constructor.last_data_faults["NVDA"]["fault"] == FAULT_NO_VOLATILITY
        # Drain hands them over and clears, so the next session starts clean.
        drained = constructor.drain_data_faults()
        assert drained["NVDA"]["fault"] == FAULT_NO_VOLATILITY
        assert constructor.last_data_faults == {}

    def test_a_short_faults_the_same_way(self):
        constructor = PortfolioConstructor()
        analysis = _analysis(
            symbol="TSLA",
            rating="sell",
            entry=250.0,
            stop=262.5,
            model_target=150.0,
            levels=[220.0, 262.5, 300.0],
            computed=[],
            atr=3.0,
            horizon=45,
        )
        analysis.levels_coverage = COVERAGE_NO_BARS
        decisions = constructor.construct_orders(
            targets=[self._target("TSLA", "short")],
            positions=[],
            analyses=[analysis],
            total_value=100_000,
            price_map={"TSLA": 250.0},
        )
        assert decisions == []
        assert constructor.last_data_faults["TSLA"]["fault"] == FAULT_NO_STRUCTURE
        assert constructor.last_data_faults["TSLA"]["direction"] == "short"


# ---------------------------------------------------------------------------
# The rule's two branches, and where they hand over
# ---------------------------------------------------------------------------


class TestTheRule:
    def test_a_level_inside_the_noise_floor_is_recorded_not_stepped_over(self):
        """**Inverted 2026-09-30.** This used to assert `112.0` — "skip it
        and take the next real level out".

        Skipping is what put META's target above three prior rejections.
        A level half an ATR overhead is not somewhere price already is; it
        is a wall with almost no room under it. The reward really is inside
        one session's noise, and that is now SAID (`target_inside_noise`)
        instead of being fixed by moving the target to the far side of the
        wall — which is not a better reward, only an unreachable one."""
        result = derive_structural_target(
            entry_price=100.0,
            direction="long",
            levels=[100.5, 112.0],
            atr=2.0,
            horizon_sessions=25,
            setup_type="range",
        )
        assert result.price == 100.5
        assert result.basis == "structural_level"
        assert result.target_inside_noise is True
        assert "noise floor" in result.detail

    def test_a_target_with_real_room_is_not_flagged_as_crowded(self):
        """The flag is a measurement, not a mood — it is False whenever the
        reward clears the floor, which is the ordinary case."""
        result = derive_structural_target(
            entry_price=100.0,
            direction="long",
            levels=[112.0],
            atr=2.0,
            horizon_sessions=25,
            setup_type="range",
        )
        assert result.price == 112.0
        assert result.target_inside_noise is False

    def test_a_short_does_not_step_over_a_floor_either(self):
        """Direction symmetry: the same promotion bug existed downward."""
        result = derive_structural_target(
            entry_price=100.0,
            direction="short",
            levels=[99.5, 88.0],
            atr=2.0,
            horizon_sessions=25,
            setup_type="range",
        )
        assert result.price == 99.5
        assert result.target_inside_noise is True

    def test_meta_2026_09_21_target_no_longer_clears_the_wall(self):
        """THE MEASURED CASE, from the desk's own record.

        The 2026-09-21 intraday add filled at $728.41 with ATR $21.22. The
        level scan had found resistance at $730.41 (2 touches) and $739.84
        (3 touches, including the 2026-01-29 rejection at $742.09). The old
        noise floor sat at $749.63, swallowed both, and set the target at
        $785.20 in the trade record — above both rejections and above the
        then 52-week high. Reproducing the scan from completed bars puts
        that same cluster at $784.58, which is the level used below; the
        order was built intraday against a live reference price and that
        is the whole of the difference.
        Price high was $779.82 and it never filled.

        The target must now be the first wall, $730.41, and the thin reward
        must be recorded rather than engineered away."""
        levels = [730.41, 739.84, 784.58]
        result = derive_structural_target(
            entry_price=728.41,
            direction="long",
            levels=levels,
            atr=21.22,
            horizon_sessions=12,
            setup_type="breakout",
        )
        assert result.price == 730.41
        assert result.basis == "structural_level"
        assert result.target_inside_noise is True
        # The specific harm: never again above a level price was rejected from.
        assert result.price < 739.84 < 784.58

    def test_a_projection_can_never_be_placed_beyond_a_wall(self):
        """The measured-move branch is reached only when the wall is out of
        reach, so at this module's defaults the clamp never binds. Both
        multiples are caller-supplied, though, and a projection multiple at
        or above the reach multiple would otherwise put an ATR target on the
        far side of a real level. Pinned because it is a parameter away."""
        result = derive_structural_target(
            entry_price=100.0,
            direction="long",
            levels=[80.0, 130.0],
            atr=2.0,
            horizon_sessions=25,
            setup_type="breakout",
            max_reach_atr_multiple=1.0,  # reach 10 -> wall at 130 is far
            breakout_projection_atr_multiple=5.0,  # projection 50 -> would be 150
        )
        assert result.price == 130.0

    def test_a_breakout_with_no_overhead_level_gets_a_measured_move(self):
        """No ceiling exists, so the only honest statement about where the
        instrument travels is how far it usually travels in the stated
        holding period."""
        result = derive_structural_target(
            entry_price=100.0,
            direction="long",
            levels=[85.0, 92.0],
            atr=2.0,
            horizon_sessions=25,
            setup_type="breakout",
        )
        assert result.basis == "measured_move"
        # ATR 2.0 x sqrt(25) x 1.0 = 10.0
        assert result.price == 110.0

    def test_a_measured_move_scales_with_the_square_root_of_the_horizon(self):
        """Daily ranges accumulate as a random walk. Linear ATR x N would
        overstate an N-session excursion by roughly sqrt(N), which is how a
        target becomes a fantasy while still looking arithmetic."""
        four = derive_structural_target(
            entry_price=100.0, direction="long", levels=[80.0], atr=1.0, horizon_sessions=4, setup_type="breakout"
        ).price
        sixteen = derive_structural_target(
            entry_price=100.0, direction="long", levels=[80.0], atr=1.0, horizon_sessions=16, setup_type="breakout"
        ).price
        assert four == 102.0 and sixteen == 104.0  # 2x horizon-root, not 4x

    def test_a_level_beyond_reach_hands_over_to_the_measured_move(self):
        """Resistance 40% away does not bound a 25-session trade. Nothing
        stands in the way over the hold, so travel governs — and the answer
        is the SMALLER number, which is why this is not target inflation."""
        result = derive_structural_target(
            entry_price=100.0,
            direction="long",
            levels=[140.0],
            atr=2.0,
            horizon_sessions=25,
            setup_type="range",
        )
        assert result.basis == "measured_move"
        assert result.price == 110.0
        assert result.level_used == 140.0  # the far level is still recorded

    def test_an_implausible_horizon_cannot_licence_an_arbitrary_target(self):
        result = derive_structural_target(
            entry_price=100.0,
            direction="long",
            levels=[80.0],
            atr=1.0,
            horizon_sessions=100_000,
            setup_type="breakout",
        )
        capped = derive_structural_target(
            entry_price=100.0,
            direction="long",
            levels=[80.0],
            atr=1.0,
            horizon_sessions=MAX_HORIZON_SESSIONS,
            setup_type="breakout",
        )
        assert result.price == capped.price

    def test_a_short_projection_through_zero_is_refused(self):
        """**Inputs changed 2026-09-30, assertion unchanged.** This used to
        pass `levels=[1.9]` — a support 10c under a $2.00 short. The old
        noise floor dropped it and the derivation projected $7.50 down
        through zero, so the case under test was only ever reachable
        BECAUSE a real level was being ignored. It is now targeted, which
        is the better answer and is pinned directly below.

        The through-zero refusal still matters and is still reached, on the
        chart that actually produces it: nothing in the trade's direction
        at all, so only the projection can speak."""
        result = derive_structural_target(
            entry_price=2.0,
            direction="short",
            levels=[2.6],
            atr=1.5,
            horizon_sessions=25,
            setup_type="breakout",
        )
        assert result.refusal == "projection_implausible"

    def test_a_support_under_a_penny_short_is_targeted_not_projected_past(self):
        """The half the case above used to hide: a real level 10c under the
        entry is where the short travels to, not a reason to invent $7.50 of
        downside that runs the target through zero."""
        result = derive_structural_target(
            entry_price=2.0,
            direction="short",
            levels=[1.9],
            atr=1.5,
            horizon_sessions=25,
            setup_type="breakout",
        )
        assert result.price == 1.9
        assert result.basis == "structural_level"
        assert result.target_inside_noise is True


# ---------------------------------------------------------------------------
# The model's target is evidence, not arithmetic
# ---------------------------------------------------------------------------


class TestModelTargetIsEvidence:
    def test_the_guess_is_carried_through_and_the_gap_measured(self):
        result = derive_structural_target(
            entry_price=100.0,
            direction="long",
            levels=[112.0],
            atr=2.0,
            horizon_sessions=25,
            setup_type="range",
            model_target=140.0,
        )
        assert result.price == 112.0
        assert result.model_target == 140.0
        assert result.divergence_pct == -20.0

    def test_the_guess_never_changes_the_answer(self):
        """Same chart, three different model opinions, one computed target."""
        prices = {
            derive_structural_target(
                entry_price=100.0,
                direction="long",
                levels=[112.0],
                atr=2.0,
                horizon_sessions=25,
                setup_type="range",
                model_target=guess,
            ).price
            for guess in (105.0, 140.0, None)
        }
        assert prices == {112.0}

    def test_the_order_reasoning_shows_both_numbers(self):
        """The AI Risk Manager reads `reasoning`. It must see that the
        take-profit is computed and where the analyst differed, rather than
        re-deriving the ratio in its head — the failure mode that produced
        two contradictory R/R figures in one response on 2026-08-31."""
        constructor = PortfolioConstructor()
        analysis = _analysis(
            symbol="NVDA",
            rating="buy",
            entry=100.0,
            stop=95.0,
            model_target=140.0,
            levels=[95.0, 112.0],
            atr=5.0 / 4.0,
            horizon=45,
        )
        decisions = constructor.construct_orders(
            targets=[
                TargetPosition(
                    symbol="NVDA",
                    target_weight_pct=15.0,
                    conviction="high",
                    thesis="AI",
                )
            ],
            positions=[],
            analyses=[analysis],
            total_value=100_000,
            price_map={"NVDA": 100.0},
        )
        assert len(decisions) == 1
        reasoning = decisions[0].reasoning
        assert "$112.00" in reasoning
        assert "structural level" in reasoning
        assert "$140.00" in reasoning  # the analyst's guess, kept visible


# ---------------------------------------------------------------------------
# The SLB case — the trade this change exists because of
# ---------------------------------------------------------------------------


class TestSLB:
    """SLB, 2026-09-01 morning run: `strong_buy` / `high` conviction, entry
    $60.10, stop $55.50 — a 7.7% stop — and reward:risk 1.28 against the
    model's target, so it was refused before anyone judged it.

    The level data below is SYNTHETIC. The production bars for that session
    are not in this repository, so these fixtures assert what the RULE does
    with a plausible chart, not what SLB's actual chart contained. The
    reported R/R numbers carry that caveat.
    """

    ENTRY = 60.10
    STOP = 55.50
    RISK = ENTRY - STOP  # $4.60 / share
    MODEL_TARGET = ENTRY + 1.28 * RISK  # ~$65.99, the 1.28 R/R figure
    # The stop SLB actually shipped with on 2026-09-01 was
    # `min_stop_atr_multiple` (3.0 at the time) x 1.15 (the range scaler at
    # the time) = 3.45 ATRs out, which back-solves the ATR the run must have
    # been working with. Stated, not assumed silently.
    #
    # 3.45 is deliberately kept here as a HISTORICAL constant and is not
    # re-derived from config. The floor became 1.5 base / 0.90 range on
    # 2026-09-04 (= 1.35 ATRs) and 2.5 / 0.90 on 2026-09-10 (= 2.25 ATRs),
    # but this class reproduces what that specific
    # run did, and re-deriving it from live settings would silently rewrite
    # the historical record every time the config moves. Tests that assert
    # CURRENT behaviour read the config; this one asserts the past.
    ATR = RISK / 3.45  # ~$1.33, 2.2% of price
    FLOOR = 1.5

    def _rr(self, target: float) -> float:
        return round((target - self.ENTRY) / self.RISK, 2)

    def test_the_model_target_is_the_1_28_that_was_rejected(self):
        """Anchor the reproduction: this is the number the run produced."""
        assert self._rr(self.MODEL_TARGET) == 1.28
        assert self._rr(self.MODEL_TARGET) < self.FLOOR

    def test_a_resistance_shelf_above_entry_clears_the_floor(self):
        """The case the change is for. Price topped out repeatedly at ~$67.5
        before the decline into $60; that shelf is where it travels back to,
        and it is further than the model was willing to say."""
        bars = _bars(_oscillation(58.0, 67.5, cycles=20) + [64.0, 62.0, 61.0, 60.5, 60.1])
        supports, resistances = find_structural_levels(bars)
        levels = [lv.price for lv in (*supports, *resistances)]

        result = derive_structural_target(
            entry_price=self.ENTRY,
            direction="long",
            levels=levels,
            atr=self.ATR,
            horizon_sessions=30,
            setup_type="range",
            model_target=self.MODEL_TARGET,
        )
        assert result.basis == "structural_level"
        computed_rr = self._rr(result.price)
        # Recorded so the number is visible when this test is read, not just
        # when it fails.
        assert result.price > self.MODEL_TARGET
        assert computed_rr > self._rr(self.MODEL_TARGET)
        assert computed_rr >= self.FLOOR

    def test_a_nearer_shelf_still_fails_the_floor_and_that_is_the_answer(self):
        """The honest other half. If the real chart's nearest resistance is
        $63.50, the computed target is WORSE than the model's guess and SLB
        is correctly refused. The floor does not move to accommodate it."""
        result = derive_structural_target(
            entry_price=self.ENTRY,
            direction="long",
            levels=[55.0, 63.50, 72.0],
            atr=self.ATR,
            horizon_sessions=30,
            setup_type="range",
            model_target=self.MODEL_TARGET,
        )
        assert result.price == 63.50
        assert self._rr(result.price) < self.FLOOR
        # And the disagreement is measured rather than smoothed over.
        assert result.divergence_pct is not None
        assert result.divergence_pct < 0

    def test_a_thin_computed_geometry_ships_on_the_computed_shelf(self):
        """**Inverted 2026-09-11, docs/WORK.md item 1(d).** This used to
        assert that when the stop rule and the computed target could not make
        a trade together, the trade was refused with a GEOMETRY code rather
        than a bad-guess one. The refusal is gone for a range setup: the
        honest 0.80 is computed from real structure, capped at starter size
        by the PM gate, ranked below better payoffs, and traded.

        The derivation itself is what this class exists for and is unchanged
        — the target still comes from the computed shelf, not the model's
        guess, and the divergence is still measured."""
        constructor = PortfolioConstructor(ConstructorConfig())
        # Structural stop deliberately inside the noise band, so widening
        # fires and the reward:risk gate is reached.
        #
        # Numbers reworked 2026-09-04 for the 1.5 floor (range 1.35 ATRs),
        # re-derived 2026-09-10 for the 2.5 floor (2.5 ATRs, no setup scaling since 2026-10-09).
        # Worked by hand, and both conditions still have to hold:
        #   band distance  2.5 x 1.33333 = $3.33  -> widened stop $56.77
        #   stop $59.00 is $1.10 out, INSIDE $3.33, so widening fires
        #   nearest shelf $62.50 -> reward $2.40, risk $3.33, R/R 0.72 < 1.5
        # The shelf itself does NOT have to move again. It moved $63.50 ->
        # $62.50 in the 2026-09-04 rework because the 1.35-ATR band left only
        # $1.80 of risk, against which $63.50's $3.40 of reward scored 1.89
        # and TRADED. A 2.5-ATR band is a larger denominator, so $62.50 now
        # fails by more than it did (0.80 where it was 1.33), not less — and
        # $63.50 would fail too, at 3.40 / 3.33 = 1.02. The shelf is left at
        # $62.50 so the fixture keeps failing for the same reason it has
        # since that rework, with margin rather than on a knife edge.
        analysis = _analysis(
            symbol="SLB",
            rating="buy",
            entry=self.ENTRY,
            stop=59.0,
            model_target=self.MODEL_TARGET,
            levels=[55.0, 62.50],
            atr=self.ATR,
            horizon=30,
        )
        decisions = constructor.construct_orders(
            targets=[
                TargetPosition(
                    symbol="SLB",
                    target_weight_pct=15.0,
                    conviction="high",
                    thesis="oilfield services recovery",
                )
            ],
            positions=[],
            analyses=[analysis],
            total_value=100_000,
            price_map={"SLB": self.ENTRY},
        )
        # 2026-10-09 (owner mandate, docs/OUTCOME.md): the 2026-10-01 parity
        # refusal is deleted, so the 0.80 geometry TRADES again. The
        # DERIVATION, which is what this class exists for, is asserted on the
        # shipped order: the computed shelf at $62.50 (not the model's guess)
        # and the band-edge stop at $56.77.
        assert len(decisions) == 1
        assert "SLB" not in constructor.last_refusals
        assert decisions[0].take_profit == 62.50
        assert decisions[0].stop_loss == 56.77
