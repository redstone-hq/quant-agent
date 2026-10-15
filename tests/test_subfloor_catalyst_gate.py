"""The sub-floor reward:risk catalyst gate (2026-09-02), and its direction
check (Phase 13 fix, 2026-09-03).

The prompt has always permitted a below-floor pick that names a catalyst.
Benchmarked 2026-09-01 on the real opportunity set of the zero-trade day
(`run-64290730`), both candidate models picked NVDA at R/R 1.03 in 9 of 9
runs and passed over GEV, which cleared the floor — WITHOUT DISOBEYING.
Every sub-floor pick named a catalyst, cut size, and stated the ratio was
below floor; the rule-compliance grader passed them all.

The first hole was that the catalyst was asserted, never checked. For a
mega-cap the news feed always carries one, so the exception was a null
constraint on exactly the names it needed to bind — and the desk's own
`active_state_changes` block was feeding the PM the catalyst it then used to
justify the exception. That half of the fix is deterministic and lives
AFTER the PM submits:
  * a sub-floor pick's `catalyst` must RESOLVE to an `active_state_changes`
    row — cited by that row's ISO date, and the row must name the symbol —
    or the target is dropped;
  * one that does resolve is capped at the smallest starter size.

The SECOND hole (this file's newer half): resolving to a row only proved
the row existed and named the symbol, never that the row's news was GOOD
news for a long or BAD news for a short. A stock sinking on genuinely bad
news could cite the very row reporting that bad news to justify a BUY.
`StateChange.symbol_direction` (src/models.py) now carries a structured
per-symbol direction — populated by the same news_analyst call that already
produces `StockNewsItem.sentiment`, no new LLM call — and the gate requires
the cited row's direction for that symbol to actually support the trade:
bullish for a long, bearish for a short. A missing or neutral direction
does not qualify either side. Real published research on rule overrides
substantiates fixing this by making the exception check SUBSTANCE, not
existence, rather than removing it: an override of a systematic rule only
improves outcomes when it carries a real quality bar.

These tests pin all three layers — existence, direction, and the
exemptions/bypasses that were deliberately closed.

**REWRITTEN 2026-09-11 — docs/WORK.md item 1(d), owner decision.** The floor
this gate was the exception to is gone, and the gate changed shape with it:

  * a Type B / breakout target never reaches this gate at all;
  * a Type A / range target with a MEASURABLE but weak ratio is now KEPT and
    capped at starter size — it used to be dropped unless a catalyst
    resolved, and the catalyst is no longer consulted for it. Every test
    below that used to assert a drop on that path now asserts the cap;
  * a Type A target with an UNMEASURABLE ratio is still dropped unless the
    catalyst resolves, direction and all. **That is the only path on which
    the catalyst machinery still binds**, so the existence/direction/recency
    tests are pinned there — they exercise exactly the same code, on the
    only case that can still reach it.

That leaves the catalyst mechanism redundant for the case it was actually
built for (a thin-but-real payoff). Flagged for the owner, not removed.
"""

import json
from datetime import date, timedelta

from unittest.mock import MagicMock, patch

import pytest

import src.agents.portfolio_manager as pm_module
from src.data.news_store import ACTIVE_STATE_CHANGE_WINDOW_DAYS
from src.agents.portfolio_manager import (
    SUBFLOOR_CATALYST_UNVERIFIED_STATUS,
    PortfolioManagerAgent,
)
from src.models import (
    PortfolioDecision,
    Position,
    TechAnalysisResult,
    TechReasoningChain,
)
from src.risk.constants import STARTER_POSITION_RISK_PCT


# The fixture block below is copied from the 2026-09-01 session, and the gate
# ages every row against the current date. Pin the session date so these tests
# keep testing the RULE instead of quietly turning into a clock test once the
# fixture rows fall out of the rolling window.
FIXTURE_SESSION_DATE = date(2026, 9, 1)


@pytest.fixture(autouse=True)
def _freeze_session_date(monkeypatch):
    monkeypatch.setattr(pm_module, "et_today", lambda: FIXTURE_SESSION_DATE)


# The block as `TradingPipeline._build_active_state_changes` renders it —
# same five events as the run-64290730 fixture
# (`ops/model_policy/fixtures/run_64290730_pm_input.json`,
# `memory.active_state_changes`), with the `(direction)` suffix the Phase 13
# fix added to each symbol. Directions are assigned to match each event's
# actual meaning: the broad selloff/crude-pressure row is bearish across the
# board; the deal/earnings-beat rows are bullish for the names they name.
ACTIVE_STATE_CHANGES = (
    "- [2026-09-01] Global bond selloff lifts yields and rising crude "
    "pressures equities to start September → SPY(bearish), QQQ(bearish), "
    "DIA(bearish), SMH(bearish), SOXX(bearish), NVDA(bearish)\n"
    "- [2026-08-31] US oil firms designated to take over Venezuelan "
    "oilfields, Exxon expanding footprint → XOM(bullish), CVX(bullish), "
    "XLE(bullish)\n"
    "- [2026-08-31] Anthropic signs $35 billion cloud deal with "
    "Nvidia-backed Lambda → NVDA(bullish)\n"
    "- [2026-08-27] Nvidia revenue forecast of 70% growth → NVDA(bullish), "
    "SMH(bullish), SOXX(bullish), AMD(bullish), AVGO(bullish), TSM(bullish)\n"
    "- [2026-08-27] Salesforce beats Q2 earnings and expands AI "
    "partnership → CRM(bullish), MSFT(bullish), GOOGL(bullish), "
    "AMZN(bullish), ORCL(bullish), ZS(bullish)"
)

# The catalyst the LIVE 2026-09-01 desk actually recorded for its NVDA
# target, read out of production `agent_logs` for decision
# `run-64290730-dec-f25433`. It is concrete, specific and entirely
# unverifiable: no state-change row mentions an SB Energy investment.
LIVE_NVDA_CATALYST = (
    "HIGH bullish stock-specific news: strategic $3B investment into SB "
    "Energy reinforces long-term AI infrastructure ecosystem lock-in."
)


def _tech_rc() -> TechReasoningChain:
    return TechReasoningChain(
        trend="x",
        momentum="x",
        volatility="x",
        volume="x",
        support_resistance="x",
    )


def _analysis(
    symbol: str,
    *,
    rating: str = "buy",
    target: float | None = 112.0,
) -> TechAnalysisResult:
    """Entry 100 / stop 95 for a long, 100 / 105 for a short.

    `target=112` gives R/R 2.4 (clears the floor); `target=104` gives 0.8
    (fails it). A `neutral` rating gives `risk_reward is None`, which the
    gate also treats as sub-floor.
    """
    buy = rating in {"buy", "strong_buy"}
    return TechAnalysisResult(
        symbol=symbol,
        rating=rating,
        conviction="medium",
        entry_price=100,
        stop_loss=95 if buy else 105,
        reference_target=target,
        support_levels=[95] if buy else [88],
        resistance_levels=[112] if buy else [105],
        setup_type="range",
        expected_horizon_sessions=10,
        reasoning="validated production-like trend and momentum evidence",
        reasoning_chain=_tech_rc(),
        thesis_invalid_if="closes below support",
    )


def _decision(targets: list[dict]) -> PortfolioDecision:
    return PortfolioDecision.model_validate(
        {
            "reasoning_chain": {
                "macro_filter": "Macro checked.",
                "news_check": "News checked.",
                "earnings_check": "Earnings checked.",
                "signal_conflicts": "None material.",
                "sizing_logic": "Sizing checked.",
                "portfolio_balance": "Book checked.",
                "cash_target": "Cash checked.",
            },
            "targets": targets,
            "portfolio_view": "Test decision.",
        }
    )


def _target(
    symbol: str,
    *,
    risk: float | None = 3.0,
    catalyst: str = "",
    direction: str = "long",
    weight: float | None = None,
) -> dict:
    row: dict = {
        "symbol": symbol,
        "conviction": "medium",
        "direction": direction,
        "thesis": f"{symbol} setup.",
        "catalyst": catalyst,
        "provenance": [
            {
                "source": "technical",
                "observed_stance": "buy" if direction == "long" else "sell",
                "relationship": "supports",
                "evidence": "current-run rating",
            }
        ],
    }
    if risk is not None:
        row["risk_allocation_pct"] = risk
    if weight is not None:
        row["target_weight_pct"] = weight
    return row


def _unmeasurable(*symbols: str) -> dict[str, float | None]:
    """The real-reward:risk map production supplies, with no computable ratio
    for these symbols.

    Post item 1(d) this is the ONLY state in which the catalyst gate still
    decides anything: a measurable-but-weak range payoff is kept and capped
    without consulting a catalyst at all, so the existence/direction/recency
    checks can only be exercised on unmeasurable geometry. Same production
    shape as before — `PortfolioConstructor.real_reward_risk_preview`
    returning None for a name whose payoff arithmetic does not resolve.
    """
    return {s.upper(): None for s in symbols}


def _apply(
    decision,
    analyses,
    *,
    positions=None,
    asc=ACTIVE_STATE_CHANGES,
    real_reward_risk_by_symbol=None,
):
    return PortfolioManagerAgent._apply_subfloor_catalyst_rule(
        decision,
        analyses=analyses,
        positions=positions or [],
        total_value=100_000,
        active_state_changes=asc,
        starter_risk_pct=STARTER_POSITION_RISK_PCT,
        real_reward_risk_by_symbol=real_reward_risk_by_symbol,
    )


def _held(symbol: str, qty: float = 10.0) -> Position:
    return Position(
        symbol=symbol,
        qty=qty,
        avg_entry=100.0,
        current_price=105.0,
        market_value=qty * 105.0,
        unrealized_pnl=50.0,
        sector="Technology",
    )


# --------------------------------------------------------------------------
# The rendered-block parser
# --------------------------------------------------------------------------


def test_parser_maps_each_row_date_to_symbols_and_their_directions():
    by_date = PortfolioManagerAgent._state_change_symbols_by_date(
        ACTIVE_STATE_CHANGES,
    )
    assert by_date["2026-09-01"] == {
        "SPY": {"bearish"},
        "QQQ": {"bearish"},
        "DIA": {"bearish"},
        "SMH": {"bearish"},
        "SOXX": {"bearish"},
        "NVDA": {"bearish"},
    }
    # Two rows share 2026-08-31, and two share 2026-08-27. Same-date rows are
    # UNIONED per symbol: a citation proves "a HIGH-conviction state change
    # affecting this symbol in this direction was recorded on this date",
    # which is the checkable claim.
    assert by_date["2026-08-31"] == {
        "XOM": {"bullish"},
        "CVX": {"bullish"},
        "XLE": {"bullish"},
        "NVDA": {"bullish"},
    }
    assert by_date["2026-08-27"] == {
        "NVDA": {"bullish"},
        "SMH": {"bullish"},
        "SOXX": {"bullish"},
        "AMD": {"bullish"},
        "AVGO": {"bullish"},
        "TSM": {"bullish"},
        "CRM": {"bullish"},
        "MSFT": {"bullish"},
        "GOOGL": {"bullish"},
        "AMZN": {"bullish"},
        "ORCL": {"bullish"},
        "ZS": {"bullish"},
    }


def test_parser_skips_rows_with_no_affected_symbols_and_unparseable_lines():
    """`_build_active_state_changes` writes an em dash when the news analyst
    attached no symbols. A market-wide row names nobody, so it can back
    nobody — and a line that does not parse must narrow what can be cited,
    never widen it or raise. `TLT` here has no `(direction)` suffix — the
    legacy/malformed shape — so it is recorded as existing but with an
    empty direction set."""
    by_date = PortfolioManagerAgent._state_change_symbols_by_date(
        "- [2026-08-30] Broad risk-off with no named exposure → —\n"
        "not a row at all\n"
        "- [2026-08-29] Missing the arrow entirely\n"
        "- [not-a-date] Something → NVDA(bullish)\n"
        "- [2026-08-28] Yields fall → TLT",
    )
    assert by_date == {"2026-08-28": {"TLT": set()}}


def test_parser_splits_on_the_last_arrow_so_event_prose_may_contain_one():
    by_date = PortfolioManagerAgent._state_change_symbols_by_date(
        "- [2026-08-31] Regime moved risk-off → risk-on overnight → SPY(bullish), QQQ(bullish)",
    )
    assert by_date == {"2026-08-31": {"SPY": {"bullish"}, "QQQ": {"bullish"}}}


def test_parser_tolerates_an_empty_block():
    assert PortfolioManagerAgent._state_change_symbols_by_date("") == {}


def test_parser_treats_unrecognized_direction_tokens_as_no_direction():
    """`_build_active_state_changes` writes `(unknown)` for a symbol with no
    recorded `symbol_direction` (older persisted reports, or a genuine
    analyst omission). That token is not one of the three valid literals,
    so it must not be silently treated as satisfying either side of the
    gate."""
    by_date = PortfolioManagerAgent._state_change_symbols_by_date(
        "- [2026-08-28] Something happened → FOO(unknown)",
    )
    assert by_date == {"2026-08-28": {"FOO": set()}}


# --------------------------------------------------------------------------
# The citation check — existence AND direction
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "catalyst,symbol,required_direction,expected",
    [
        # Right row, right symbol, right (bullish) direction for a long.
        ("2026-08-31: Anthropic/Lambda cloud deal", "NVDA", "bullish", True),
        # Same row, but a short would need bearish — this row is bullish only.
        ("2026-08-31: Anthropic/Lambda cloud deal", "NVDA", "bearish", False),
        # Same date, a symbol that row does not name.
        ("2026-08-31: Anthropic/Lambda cloud deal", "GEV", "bullish", False),
        # Right symbol, a date with no row.
        ("2026-08-26: something happened", "NVDA", "bullish", False),
        # Concrete, specific, and citing nothing — the whole failure mode.
        (LIVE_NVDA_CATALYST, "NVDA", "bullish", False),
        ("", "NVDA", "bullish", False),
        ("   ", "NVDA", "bullish", False),
        # A row whose symbols include the target, cited among other prose.
        ("Energy takeover per the 2026-08-31 state change", "XLE", "bullish", True),
        # Same row for a short — XLE is recorded bullish, not bearish.
        ("Energy takeover per the 2026-08-31 state change", "XLE", "bearish", False),
        # NVDA is recorded BEARISH on 2026-09-01 (broad selloff) — a short can
        # cite it, a long cannot.
        ("2026-09-01: bond selloff pressure", "NVDA", "bearish", True),
        ("2026-09-01: bond selloff pressure", "NVDA", "bullish", False),
    ],
)
def test_catalyst_resolution(catalyst, symbol, required_direction, expected):
    by_date = PortfolioManagerAgent._state_change_symbols_by_date(
        ACTIVE_STATE_CHANGES,
    )
    assert (
        PortfolioManagerAgent._catalyst_cites_state_change(
            catalyst,
            symbol,
            required_direction,
            by_date,
        )
        is expected
    )


def test_a_neutral_direction_does_not_qualify_either_side():
    by_date = PortfolioManagerAgent._state_change_symbols_by_date(
        "- [2026-08-20] Mixed signals, no clear read → FOO(neutral)",
    )
    assert not PortfolioManagerAgent._catalyst_cites_state_change(
        "2026-08-20: mixed signals",
        "FOO",
        "bullish",
        by_date,
    )
    assert not PortfolioManagerAgent._catalyst_cites_state_change(
        "2026-08-20: mixed signals",
        "FOO",
        "bearish",
        by_date,
    )


def test_a_row_missing_direction_data_entirely_fails_closed():
    """A legacy-format row (no `(direction)` suffix at all) proves the row
    exists and names the symbol, and nothing more. Same posture as the rest
    of this gate: missing data must never be the thing that grants
    permission."""
    by_date = PortfolioManagerAgent._state_change_symbols_by_date(
        "- [2026-08-20] Old-format row, no direction tag → FOO",
    )
    assert by_date["2026-08-20"]["FOO"] == set()
    assert not PortfolioManagerAgent._catalyst_cites_state_change(
        "2026-08-20: old format",
        "FOO",
        "bullish",
        by_date,
    )
    assert not PortfolioManagerAgent._catalyst_cites_state_change(
        "2026-08-20: old format",
        "FOO",
        "bearish",
        by_date,
    )


def test_the_live_2026_09_01_nvda_catalyst_does_not_resolve():
    """Parser still does not treat the live 2026-09-01 NVDA text as a
    resolving citation. The admission gate no longer cares: unmeasurable
    NVDA is kept at the asked size."""
    decision = _decision([_target("NVDA", risk=0.75, catalyst=LIVE_NVDA_CATALYST)])
    result = _apply(
        decision,
        [_analysis("NVDA", target=104.0)],
        real_reward_risk_by_symbol=_unmeasurable("NVDA"),
    )
    assert [t.symbol for t in result.targets] == ["NVDA"]
    assert result.targets[0].risk_allocation_pct == 0.75
    assert result.targets[0].subfloor_catalyst_verified is False


# --------------------------------------------------------------------------
# The gate is dead — unmeasurable is kept, not dropped or starter-capped
# --------------------------------------------------------------------------


def test_subfloor_pick_with_unverifiable_catalyst_is_kept_with_the_book():
    """Catalyst theater is gone. Unmeasurable NVDA stays at the asked size
    next to a measurable GEV."""
    decision = _decision(
        [
            _target("NVDA", catalyst=LIVE_NVDA_CATALYST),
            _target("GEV", risk=2.0),
        ]
    )
    result = _apply(
        decision,
        [_analysis("NVDA", target=104.0), _analysis("GEV", target=112.0)],
        real_reward_risk_by_symbol={"NVDA": None, "GEV": 2.4},
    )
    by_symbol = {t.symbol: t for t in result.targets}
    assert set(by_symbol) == {"NVDA", "GEV"}
    assert by_symbol["NVDA"].risk_allocation_pct == 3.0
    assert by_symbol["GEV"].risk_allocation_pct == 2.0


def test_subfloor_pick_with_no_catalyst_at_all_is_kept():
    decision = _decision([_target("NVDA", catalyst="")])
    result = _apply(
        decision,
        [_analysis("NVDA", target=104.0)],
        real_reward_risk_by_symbol=_unmeasurable("NVDA"),
    )
    assert [t.symbol for t in result.targets] == ["NVDA"]
    assert result.targets[0].risk_allocation_pct == 3.0


def test_subfloor_long_citing_a_genuinely_bullish_catalyst_is_not_capped():
    """A resolving citation is not a size door. Asked size stands."""
    decision = _decision(
        [
            _target("NVDA", risk=3.0, catalyst="2026-08-31 Anthropic/Lambda deal"),
        ]
    )
    result = _apply(
        decision,
        [_analysis("NVDA", target=104.0)],
        real_reward_risk_by_symbol=_unmeasurable("NVDA"),
    )
    assert [t.symbol for t in result.targets] == ["NVDA"]
    assert result.targets[0].risk_allocation_pct == 3.0
    assert result.targets[0].subfloor_catalyst_verified is False


def test_subfloor_long_citing_a_bearish_catalyst_for_the_same_symbol_is_kept():
    """Wrong-direction news is not a refuse. R/R theater is gone."""
    decision = _decision(
        [
            _target("NVDA", risk=3.0, catalyst="2026-09-01 bond selloff pressure"),
        ]
    )
    result = _apply(
        decision,
        [_analysis("NVDA", target=104.0)],
        real_reward_risk_by_symbol=_unmeasurable("NVDA"),
    )
    assert [t.symbol for t in result.targets] == ["NVDA"]
    assert result.targets[0].risk_allocation_pct == 3.0


def test_subfloor_long_citing_a_neutral_catalyst_is_kept():
    decision = _decision(
        [
            _target("ZZZZ", risk=3.0, catalyst="2026-08-20 mixed signals"),
        ]
    )
    result = _apply(
        decision,
        [_analysis("ZZZZ", target=104.0)],
        asc="- [2026-08-20] Mixed signals, no clear read → ZZZZ(neutral)",
        real_reward_risk_by_symbol=_unmeasurable("ZZZZ"),
    )
    assert [t.symbol for t in result.targets] == ["ZZZZ"]
    assert result.targets[0].risk_allocation_pct == 3.0


def test_subfloor_short_citing_a_genuinely_bearish_catalyst_is_not_capped():
    decision = _decision(
        [
            _target("NVDA", direction="short", risk=3.0, catalyst="2026-09-01 bond selloff pressure"),
        ]
    )
    result = _apply(
        decision,
        [_analysis("NVDA", rating="sell", target=96.0)],
        real_reward_risk_by_symbol=_unmeasurable("NVDA"),
    )
    assert [t.symbol for t in result.targets] == ["NVDA"]
    assert result.targets[0].risk_allocation_pct == 3.0


def test_subfloor_short_citing_a_bullish_catalyst_for_the_same_symbol_is_kept():
    decision = _decision(
        [
            _target("NVDA", direction="short", risk=3.0, catalyst="2026-08-31 Anthropic/Lambda deal"),
        ]
    )
    result = _apply(
        decision,
        [_analysis("NVDA", rating="sell", target=96.0)],
        real_reward_risk_by_symbol=_unmeasurable("NVDA"),
    )
    assert [t.symbol for t in result.targets] == ["NVDA"]
    assert result.targets[0].risk_allocation_pct == 3.0


def test_subfloor_short_citing_a_neutral_catalyst_is_kept():
    decision = _decision(
        [
            _target("ZZZZ", direction="short", risk=3.0, catalyst="2026-08-20 mixed signals"),
        ]
    )
    result = _apply(
        decision,
        [_analysis("ZZZZ", rating="sell", target=96.0)],
        asc="- [2026-08-20] Mixed signals, no clear read → ZZZZ(neutral)",
        real_reward_risk_by_symbol=_unmeasurable("ZZZZ"),
    )
    assert [t.symbol for t in result.targets] == ["ZZZZ"]
    assert result.targets[0].risk_allocation_pct == 3.0


def test_the_retired_cap_does_not_resize_a_small_unmeasurable():
    """Starter size is not assigned here. A 0.25% ask stays 0.25%."""
    decision = _decision(
        [
            _target("NVDA", risk=0.25, catalyst="2026-08-31 Anthropic/Lambda deal"),
        ]
    )
    result = _apply(
        decision,
        [_analysis("NVDA", target=104.0)],
        real_reward_risk_by_symbol=_unmeasurable("NVDA"),
    )
    assert result.targets[0].risk_allocation_pct == 0.25


def test_a_pick_sitting_exactly_on_the_floor_clears_it():
    """A ratio of exactly 1.5 is not special — matching
    `PortfolioConstructor`'s own `reward_risk < floor` rejection. Entry 100 /
    stop 95 / target 107.5 is R/R 1.50 exactly, and must trade at full size
    with no catalyst at all."""
    analysis = _analysis("GEV", target=107.5)
    assert analysis.risk_reward == 1.5
    decision = _decision([_target("GEV", risk=4.0, catalyst="")])
    result = _apply(decision, [analysis])
    assert [t.symbol for t in result.targets] == ["GEV"]
    assert result.targets[0].risk_allocation_pct == 4.0


def test_a_pick_clearing_the_floor_keeps_its_full_size():
    decision = _decision([_target("GEV", risk=4.0)])
    result = _apply(decision, [_analysis("GEV", target=112.0)])
    assert result.targets[0].risk_allocation_pct == 4.0


def test_missing_reward_risk_is_kept_at_asked_size():
    """`risk_reward` is None for a neutral rating or malformed geometry.
    That is ranking information, not a drop."""
    neutral = _analysis("NVDA", rating="neutral", target=112.0)
    assert neutral.risk_reward is None
    decision = _decision([_target("NVDA", catalyst=LIVE_NVDA_CATALYST)])
    kept = _apply(decision, [neutral]).targets
    assert [t.symbol for t in kept] == ["NVDA"]
    assert kept[0].risk_allocation_pct == 3.0
    cited = _decision(
        [
            _target("NVDA", catalyst="2026-08-31 Anthropic/Lambda deal"),
        ]
    )
    cited_kept = _apply(cited, [neutral]).targets
    assert [t.symbol for t in cited_kept] == ["NVDA"]
    assert cited_kept[0].risk_allocation_pct == 3.0


def test_real_reward_risk_by_symbol_overrides_the_self_reported_figure():
    """2026-09-04 fix. Without a real map this gate reads
    `TechAnalysisResult.risk_reward` — the analyst's own guessed-target
    arithmetic. NVDA here self-reports R/R 10.0 (target $150, off a $5
    stop) and would clear the floor unconditionally on the OLD path. With
    `real_reward_risk_by_symbol` supplied, the gate uses THAT number
    instead — 0.4 here, chosen to be under the floor — and NVDA is dropped
    exactly as an honestly sub-floor, uncatalysed pick would be."""
    analysis = _analysis("NVDA", target=150.0)
    assert analysis.risk_reward == 10.0

    # `_apply_subfloor_catalyst_rule` mutates `decision.targets` in place,
    # so each call needs its own fresh decision object.
    # OLD behaviour (no real map): self-reported 10.0 clears the floor.
    old = _apply(
        _decision([_target("NVDA", risk=4.0, catalyst="")]),
        [analysis],
    )
    assert [t.symbol for t in old.targets] == ["NVDA"]
    assert old.targets[0].risk_allocation_pct == 4.0

    # Owner 2026-09-17: the real map says 0.4 — thin but measurable. That
    # is ranking information, not a size-cap and not a drop.
    new = _apply(
        _decision([_target("NVDA", risk=4.0, catalyst="")]),
        [analysis],
        real_reward_risk_by_symbol={"NVDA": 0.4},
    )
    assert [t.symbol for t in new.targets] == ["NVDA"]
    assert new.targets[0].risk_allocation_pct == 4.0
    assert new.targets[0].subfloor_catalyst_verified is False, (
        "no catalyst was cited or checked — the flag must not claim one was"
    )


def test_real_reward_risk_by_symbol_rescues_an_understated_candidate():
    """The mirror case: GEV self-reports R/R 0.8 (sub-floor) and would be
    dropped on the OLD path with no catalyst. The real map says 1.6 —
    clears the floor — so the fixed gate must keep it at FULL size, not
    the sub-floor starter cap."""
    analysis = _analysis("GEV", target=104.0)
    assert analysis.risk_reward == 0.8

    # `_apply_subfloor_catalyst_rule` mutates `decision.targets` in place,
    # so each call needs its own fresh decision object.
    # Self-reported 0.8 is thin but measurable: kept at the asked size.
    old = _apply(
        _decision([_target("GEV", risk=4.0, catalyst="")]),
        [analysis],
    )
    assert [t.symbol for t in old.targets] == ["GEV"]
    assert old.targets[0].risk_allocation_pct == 4.0

    new = _apply(
        _decision([_target("GEV", risk=4.0, catalyst="")]),
        [analysis],
        real_reward_risk_by_symbol={"GEV": 1.6},
    )
    assert [t.symbol for t in new.targets] == ["GEV"]
    assert new.targets[0].risk_allocation_pct == 4.0  # full size, not capped


def test_a_symbol_missing_from_a_supplied_real_map_is_kept():
    """A supplied map that omits a symbol means the real number could not
    be computed. That is ranking information, not a drop."""
    analysis = _analysis("NVDA", target=150.0)
    assert analysis.risk_reward == 10.0
    decision = _decision([_target("NVDA", risk=4.0, catalyst="")])
    result = _apply(decision, [analysis], real_reward_risk_by_symbol={})
    assert [t.symbol for t in result.targets] == ["NVDA"]
    assert result.targets[0].risk_allocation_pct == 4.0


def test_real_map_end_to_end_with_portfolio_constructors_own_derivation():
    """Not a hand-picked map — the SAME `PortfolioConstructor.
    real_reward_risk_preview` production wires in. NVDA overstates (real
    R/R 0.60, computed from a $103 real level vs. its $150 guess); GEV
    understates (real R/R 1.60, computed from a $108 real level vs. its
    $104 guess). One test, both directions of the fix, no arithmetic
    duplicated from `test_portfolio_constructor.py`.

    **Updated 2026-09-11 (item 1(d)).** The preview used to return None for
    NVDA — not because the ratio was unmeasurable but because the preview
    itself refused anything under the floor. It now returns the real 0.60,
    which is the point: a weak payoff is a NUMBER the ranking can use, not
    an absence the gate reads as ineligible. NVDA is therefore kept and
    capped rather than dropped."""
    from src.portfolio_constructor import PortfolioConstructor

    def _structured(symbol, *, model_target, computed_levels):
        return TechAnalysisResult(
            symbol=symbol,
            rating="buy",
            conviction="medium",
            entry_price=100.0,
            stop_loss=95.0,
            reference_target=model_target,
            support_levels=[95.0],
            resistance_levels=[model_target],
            computed_levels=computed_levels,
            atr_14=(100.0 - 95.0) / 3.5,
            setup_type="range",
            expected_horizon_sessions=60,
            reasoning="test",
            reasoning_chain=_tech_rc(),
            thesis_invalid_if="closes below support",
        )

    overstated = _structured("NVDA", model_target=150.0, computed_levels=[95.0, 103.0])
    understated = _structured("GEV", model_target=104.0, computed_levels=[95.0, 108.0])
    assert overstated.risk_reward == 10.0
    assert understated.risk_reward == 0.8

    constructor = PortfolioConstructor()
    real_map = {
        "NVDA": constructor.real_reward_risk_preview(overstated, "long"),
        "GEV": constructor.real_reward_risk_preview(understated, "long"),
    }
    assert real_map == {"NVDA": 0.6, "GEV": 1.6}

    decision = _decision(
        [
            _target("NVDA", risk=4.0, catalyst=""),
            _target("GEV", risk=4.0, catalyst=""),
        ]
    )
    result = _apply(
        decision,
        [overstated, understated],
        real_reward_risk_by_symbol=real_map,
    )
    # Both survive at the size asked. A thin-but-real ratio is not a cap.
    by_symbol = {t.symbol: t for t in result.targets}
    assert set(by_symbol) == {"NVDA", "GEV"}
    assert by_symbol["NVDA"].risk_allocation_pct == 4.0
    assert by_symbol["GEV"].risk_allocation_pct == 4.0


def test_shorts_are_kept_on_the_same_terms_as_longs():
    """Unmeasurable shorts keep asked size. Catalyst is not a door."""
    decision = _decision(
        [
            _target("MSFT", direction="short", catalyst="no citation here"),
            _target("CRM", direction="short", catalyst="2026-08-27 Salesforce guidance cut"),
        ]
    )
    analyses = [
        _analysis("MSFT", rating="sell", target=96.0),
        _analysis("CRM", rating="sell", target=96.0),
    ]
    result = _apply(
        decision,
        analyses,
        asc="- [2026-08-27] Salesforce cuts cloud spending guidance on slowdown → CRM(bearish)",
        real_reward_risk_by_symbol=_unmeasurable("MSFT", "CRM"),
    )
    by_symbol = {t.symbol: t for t in result.targets}
    assert set(by_symbol) == {"MSFT", "CRM"}
    assert by_symbol["MSFT"].risk_allocation_pct == 3.0
    assert by_symbol["CRM"].risk_allocation_pct == 3.0


def test_a_short_citing_the_fixtures_bullish_crm_row_is_kept():
    decision = _decision(
        [
            _target("CRM", direction="short", risk=3.0, catalyst="2026-08-27 Salesforce Q2 beat"),
        ]
    )
    result = _apply(
        decision,
        [_analysis("CRM", rating="sell", target=96.0)],
        real_reward_risk_by_symbol=_unmeasurable("CRM"),
    )
    assert [t.symbol for t in result.targets] == ["CRM"]
    assert result.targets[0].risk_allocation_pct == 3.0


def test_exits_and_closes_are_exempt():
    """Mirrors §3.4 and §9.3's asymmetry: this desk must never find it harder
    to cut risk than to add it. A close carries no catalyst and its symbol's
    R/R is irrelevant."""
    decision = _decision([_target("NVDA", risk=0.0, catalyst="")])
    result = _apply(
        decision,
        [_analysis("NVDA", target=104.0)],
        positions=[_held("NVDA")],
    )
    assert [t.symbol for t in result.targets] == ["NVDA"]
    assert result.targets[0].risk_allocation_pct == 0.0


def test_an_empty_state_change_block_does_not_drop_an_unmeasurable():
    decision = _decision(
        [
            _target("NVDA", catalyst="2026-08-31 Anthropic/Lambda deal"),
        ]
    )
    result = _apply(
        decision,
        [_analysis("NVDA", target=104.0)],
        asc="",
        real_reward_risk_by_symbol=_unmeasurable("NVDA"),
    )
    assert [t.symbol for t in result.targets] == ["NVDA"]
    assert result.targets[0].risk_allocation_pct == 3.0


def test_legacy_notional_target_is_not_converted_onto_a_starter_cap():
    """A risk-unsized unmeasurable pick is kept as-is. Python does not
    assign starter size."""
    decision = _decision(
        [
            _target("NVDA", risk=None, weight=8.0, catalyst="2026-08-31 Anthropic/Lambda deal"),
        ]
    )
    result = _apply(
        decision,
        [_analysis("NVDA", target=104.0)],
        real_reward_risk_by_symbol=_unmeasurable("NVDA"),
    )
    assert [t.symbol for t in result.targets] == ["NVDA"]
    assert result.targets[0].risk_allocation_pct is None


def test_a_symbol_with_no_analysis_at_all_is_kept():
    """Missing analysis is not an R/R refuse. `validate_grounding` is a
    different check."""
    decision = _decision([_target("ZZZZ", catalyst=LIVE_NVDA_CATALYST)])
    result = _apply(decision, [_analysis("NVDA", target=112.0)])
    assert [t.symbol for t in result.targets] == ["ZZZZ"]


# --------------------------------------------------------------------------
# End to end through `decide()`
# --------------------------------------------------------------------------


def _pm_response(targets: list[dict]) -> str:
    return json.dumps(
        {
            "reasoning_chain": {
                "macro_filter": "checked",
                "news_check": "checked",
                "earnings_check": "checked",
                "signal_conflicts": "none material",
                "sizing_logic": "checked",
                "portfolio_balance": "checked",
                "cash_target": "checked",
            },
            "targets": targets,
            "portfolio_view": "One sub-floor pick, one qualifying pick.",
        }
    )


def _mock_agent(mock_cls, response_text: str) -> PortfolioManagerAgent:
    mock_response = MagicMock()
    mock_response.content = [MagicMock(text=response_text)]
    mock_response.usage.input_tokens = 500
    mock_response.usage.output_tokens = 200
    mock_client = MagicMock()
    mock_client.messages.create.return_value = mock_response
    mock_cls.return_value = mock_client
    return PortfolioManagerAgent(api_key="test", model="claude-opus-4-6-20250725")


@patch("anthropic.Anthropic")
def test_decide_keeps_an_unmeasurable_pick_next_to_a_measurable_one(mock_cls):
    """Unmeasurable NVDA is not dropped to rescue GEV."""
    agent = _mock_agent(
        mock_cls,
        _pm_response(
            [
                _target("NVDA", risk=0.75, catalyst=LIVE_NVDA_CATALYST),
                _target("GEV", risk=2.0),
            ]
        ),
    )
    decision, result = agent.decide(
        analyses=[_analysis("NVDA", target=float("nan")), _analysis("GEV", target=112.0)],
        positions=[],
        macro_analysis=None,
        cash_balance=50_000,
        total_value=100_000,
        active_state_changes=ACTIVE_STATE_CHANGES,
        allowed_buy_symbols={"NVDA", "GEV"},
    )
    assert decision is not None, f"decide() failed closed: {result.semantic_error}"
    by_symbol = {t.symbol: t for t in decision.targets}
    assert set(by_symbol) == {"NVDA", "GEV"}
    assert by_symbol["NVDA"].risk_allocation_pct == 0.75
    assert by_symbol["GEV"].risk_allocation_pct == 2.0
    assert result.semantic_status in (None, "success")


@patch("anthropic.Anthropic")
def test_decide_does_not_starter_cap_an_unmeasurable_pick(mock_cls):
    agent = _mock_agent(
        mock_cls,
        _pm_response(
            [
                _target("NVDA", risk=3.0, catalyst="2026-08-31: Anthropic/Lambda $35bn cloud deal"),
            ]
        ),
    )
    decision, _ = agent.decide(
        analyses=[_analysis("NVDA", target=float("nan"))],
        positions=[],
        macro_analysis=None,
        cash_balance=50_000,
        total_value=100_000,
        active_state_changes=ACTIVE_STATE_CHANGES,
        allowed_buy_symbols={"NVDA"},
    )
    assert decision is not None
    assert [t.symbol for t in decision.targets] == ["NVDA"]
    assert decision.targets[0].risk_allocation_pct == 3.0


@patch("anthropic.Anthropic")
def test_decide_keeps_an_unmeasurable_pick_with_the_wrong_direction_news(mock_cls):
    agent = _mock_agent(
        mock_cls,
        _pm_response(
            [
                _target("NVDA", risk=3.0, catalyst="2026-09-01: bond selloff pressure"),
            ]
        ),
    )
    decision, _ = agent.decide(
        analyses=[_analysis("NVDA", target=float("nan"))],
        positions=[],
        macro_analysis=None,
        cash_balance=50_000,
        total_value=100_000,
        active_state_changes=ACTIVE_STATE_CHANGES,
        allowed_buy_symbols={"NVDA"},
    )
    assert decision is not None
    assert [t.symbol for t in decision.targets] == ["NVDA"]
    assert decision.targets[0].risk_allocation_pct == 3.0


@patch("anthropic.Anthropic")
def test_decide_does_not_drop_on_the_retired_production_thresholds(mock_cls):
    agent = _mock_agent(
        mock_cls,
        _pm_response(
            [
                _target("NVDA", risk=3.0, catalyst=LIVE_NVDA_CATALYST),
            ]
        ),
    )
    decision, _ = agent.decide(
        analyses=[_analysis("NVDA", target=float("nan"))],
        positions=[],
        macro_analysis=None,
        cash_balance=50_000,
        total_value=100_000,
        active_state_changes=ACTIVE_STATE_CHANGES,
        allowed_buy_symbols={"NVDA"},
    )
    assert decision is not None
    assert [t.symbol for t in decision.targets] == ["NVDA"]
    assert decision.targets[0].risk_allocation_pct == 3.0


# --------------------------------------------------------------------------
# Single-definition guards
# --------------------------------------------------------------------------


def test_status_keys_are_stable_greppable_constants():
    assert SUBFLOOR_CATALYST_UNVERIFIED_STATUS == "pm_subfloor_catalyst_unverified"


def test_the_gate_reuses_the_risk_configs_own_numbers():
    """The cap must be the size the risk budget will actually grant — one
    definition, not a second opinion that can drift. (The reward:risk floor
    field this test also checked, `RiskConfig.min_reward_risk_after_widening`,
    was removed as dead — board item 81; nothing ever read it.)"""
    from src.config import RiskConfig

    fields = RiskConfig.model_fields
    assert fields["min_position_risk_pct"].default == STARTER_POSITION_RISK_PCT


# --------------------------------------------------------------------------
# Recency, and every way the inputs can be absent or unreadable
# --------------------------------------------------------------------------


def test_a_row_older_than_the_producers_own_window_cannot_be_cited():
    """The producer scans `ACTIVE_STATE_CHANGE_WINDOW_DAYS`, so it cannot
    render an older row today. The gate re-checks anyway: the age bound lives
    in one function in `pipeline.py`, and if that drifts, the thing that
    silently widens is what counts as a catalyst."""
    stale = FIXTURE_SESSION_DATE - timedelta(
        days=ACTIVE_STATE_CHANGE_WINDOW_DAYS + 1,
    )
    fresh = FIXTURE_SESSION_DATE - timedelta(
        days=ACTIVE_STATE_CHANGE_WINDOW_DAYS,
    )
    block = (
        f"- [{stale}] Ancient but still on the table → NVDA(bullish)\n"
        f"- [{fresh}] Right on the edge of the window → GEV(bullish)"
    )
    by_date = PortfolioManagerAgent._state_change_symbols_by_date(block)
    assert str(stale) not in by_date, "a stale row must not be citable"
    assert by_date[str(fresh)] == {"GEV": {"bullish"}}, "the window edge is inclusive"


def test_a_subfloor_pick_citing_a_stale_row_is_kept():
    stale = FIXTURE_SESSION_DATE - timedelta(
        days=ACTIVE_STATE_CHANGE_WINDOW_DAYS + 1,
    )
    decision = _decision([_target("NVDA", catalyst=f"{stale}: the old deal")])
    result = _apply(
        decision,
        [_analysis("NVDA", target=104.0)],
        asc=f"- [{stale}] The old deal → NVDA(bullish)",
        real_reward_risk_by_symbol=_unmeasurable("NVDA"),
    )
    assert [t.symbol for t in result.targets] == ["NVDA"]
    assert result.targets[0].risk_allocation_pct == 3.0


def test_a_future_dated_row_cannot_back_a_trade_taken_today():
    """Not hypothetical hygiene: the desk runs in ET while parts of the stack
    stamp UTC, so a row dated tomorrow is a plausible artefact. It cannot be
    what a trade taken today is reacting to."""
    ahead = FIXTURE_SESSION_DATE + timedelta(days=1)
    decision = _decision([_target("NVDA", catalyst=f"{ahead}: tomorrow's news")])
    result = _apply(
        decision,
        [_analysis("NVDA", target=104.0)],
        asc=f"- [{ahead}] Tomorrow's news → NVDA(bullish)",
        real_reward_risk_by_symbol=_unmeasurable("NVDA"),
    )
    assert [t.symbol for t in result.targets] == ["NVDA"]
    assert result.targets[0].risk_allocation_pct == 3.0


def test_an_unreadable_clock_stops_the_session(monkeypatch):
    """Fail closed and loud. An unreadable clock used to return an empty map,
    which quietly switched off the news-reason checks; it now raises so the
    session stops instead (money-path swallow removed 10 Oct)."""

    def _boom():
        raise RuntimeError("tz database unavailable")

    monkeypatch.setattr(pm_module, "et_today", _boom)
    with pytest.raises(RuntimeError, match="tz database unavailable"):
        PortfolioManagerAgent._state_change_symbols_by_date(ACTIVE_STATE_CHANGES)


def test_a_nan_reward_risk_is_treated_as_subfloor_not_as_passing():
    """Every comparison against NaN is False, so a naive `if rr < floor`
    would wave a NaN straight through, and a missing value would be the thing
    granting permission — the shape of the buying-power near-miss.

    VERIFIED REACHABLE, not hypothetical: `reference_target` is the analyst's
    guessed target and the least-validated price on the model. Pydantic
    accepts NaN for it and the rating/price consistency validator compares
    only entry against stop, so a NaN target reaches `risk_reward` intact.

    WHAT `risk_reward` DOES WITH IT CHANGED, and this test was written before
    it did. Until `models.reward_to_risk` became the one definition of this
    ratio (2026-09-02, spec 12.1b), the field returned `round(nan / 5)` — NaN
    — and the whole hazard was that `nan < floor` is False. That function now
    refuses non-finite input at the door and returns None, which the gate
    below catches with an explicit `is not None` rather than with a
    comparison NaN can defeat, and which renders to the PM prompt as
    "R/R n/a" instead of "R/R nan:1".

    So the premise assertion checks the property that actually matters — the
    ratio is NOT a usable number — rather than the historical NaN
    representation of it. Every substantive assertion below is unchanged: a
    NaN target must not buy the catalyst exception, and must not escape the
    starter-size cap.
    """
    analysis = _analysis("NVDA", target=float("nan"))
    rr = analysis.risk_reward
    assert rr is None or rr != rr, (
        f"this test is worthless unless a NaN target really does make risk_reward unusable; got {rr!r}"
    )

    dropped = _apply(
        _decision([_target("NVDA", catalyst=LIVE_NVDA_CATALYST)]),
        [analysis],
    )
    assert [t.symbol for t in dropped.targets] == ["NVDA"]
    assert dropped.targets[0].risk_allocation_pct == 3.0

    kept = _apply(
        _decision([_target("NVDA", risk=3.0, catalyst="2026-08-31 Anthropic/Lambda deal")]),
        [analysis],
    )
    assert kept.targets[0].risk_allocation_pct == 3.0


# --------------------------------------------------------------------------
# The refusal must DROP, never zero
# --------------------------------------------------------------------------


def test_refusing_to_add_to_a_held_name_drops_it_and_never_zeroes_it():
    """THE non-obvious hazard. `risk_allocation_pct=0` on a held symbol is
    read downstream as CLOSE IT, so expressing this refusal as a zero would
    silently LIQUIDATE a position we already own rather than declining to add
    to it. It does not error — it just sells. Omitting the symbol is HOLD,
    which is the only correct way to say no here."""
    decision = _decision(
        [
            _target("NVDA", risk=4.0, catalyst=LIVE_NVDA_CATALYST),
        ]
    )
    result = _apply(
        decision,
        [_analysis("NVDA", target=104.0)],
        positions=[_held("NVDA", qty=10.0)],
        real_reward_risk_by_symbol=_unmeasurable("NVDA"),
    )
    assert [t.symbol for t in result.targets] == ["NVDA"]
    assert result.targets[0].risk_allocation_pct == 4.0
    assert not any(
        t.symbol.upper() == "NVDA" and (t.risk_allocation_pct == 0 or t.target_weight_pct == 0) for t in result.targets
    ), "a zeroed target would read as a SELL of a position we still want held"


# --------------------------------------------------------------------------
# END TO END — does a verified exception actually produce an order?
#
# Until 2026-09-11 it did not, and nothing in this file noticed. The PM gate
# verified the citation and capped the size, and then
# `PortfolioConstructor._widen_stop_past_noise` refused the order on
# `min_reward_risk_after_widening` — the same floor, applied a second time one
# stage later with no knowledge that the exception had been granted. So parts
# (b) and (c) of docs/WORK.md item 1 were decorative: every verified catalyst
# produced exactly nothing.
#
# The tests below pin the whole chain, in both directions, plus the guards
# that keep the exemption narrow: it lifts the FLOOR refusal only, it is not
# model-settable, and it never rescues unmeasurable geometry.
# --------------------------------------------------------------------------


def _subfloor_analysis(symbol: str = "AAA") -> TechAnalysisResult:
    """A MEASURABLE sub-floor long: entry 100, level-backed stop at 95,
    computed resistance at 106 → real reward:risk 1.2, under the 1.5 floor
    and comfortably under the execution belt's 1.2 as well. Every price here
    is a level the system computed, so nothing is refused for missing
    structure — the only thing wrong with this trade is that the payoff is
    thin, which is precisely the case a verified catalyst is meant to permit.
    """
    return TechAnalysisResult(
        symbol=symbol,
        rating="buy",
        conviction="medium",
        entry_price=100.0,
        stop_loss=95.0,
        reference_target=106.0,
        support_levels=[95.0],
        resistance_levels=[106.0],
        computed_levels=[95.0, 106.0],
        atr_14=(100.0 - 95.0) / 3.5,
        setup_type="range",
        expected_horizon_sessions=60,
        reasoning="test",
        reasoning_chain=_tech_rc(),
        thesis_invalid_if="closes below support",
    )


def _verified_nvda_decision():
    """The PM gate's own output for a sub-floor NVDA long citing the real
    2026-08-31 bullish Lambda row. Not hand-built: run through
    `_apply_subfloor_catalyst_rule` so the flag under test can only be set
    the way production sets it."""
    analysis = _subfloor_analysis("NVDA")
    decision = _decision(
        [
            _target("NVDA", risk=4.0, catalyst="2026-08-31: Lambda cloud deal"),
        ]
    )
    # Unmeasurable is the only state that still routes through the catalyst
    # check after item 1(d) — a measurable-but-thin payoff is now kept and
    # capped without one, and marking THAT verified would record a check
    # that never ran.
    return _apply(
        decision,
        [analysis],
        real_reward_risk_by_symbol=_unmeasurable("NVDA"),
    ), analysis


def test_the_verified_flag_is_not_set_by_the_retired_gate():
    result, _ = _verified_nvda_decision()
    assert len(result.targets) == 1
    target = result.targets[0]
    assert target.subfloor_catalyst_verified is False
    assert target.risk_allocation_pct == 4.0


def test_a_target_clearing_the_floor_is_never_flagged():
    """The flag must mean "the exception was granted", not "a target went
    through the gate". A normal trade carries no exemption into the
    constructor."""
    decision = _decision([_target("GEV", risk=4.0)])
    result = _apply(decision, [_analysis("GEV", target=112.0)])
    assert len(result.targets) == 1
    assert result.targets[0].subfloor_catalyst_verified is False


def test_the_flag_cannot_be_asserted_by_the_model():
    """THE control. Prompt compliance is not a control, so the flag the
    constructor trusts must be one only Python can write. A model emitting it
    in its JSON is ignored — and the target is still judged on its catalyst,
    so it is still dropped.

    Both spellings are tried: the public read name and the private attribute
    behind it. Neither is reachable from input."""
    from src.models import TargetPosition

    for key in ("subfloor_catalyst_verified", "_subfloor_catalyst_verified"):
        row = _target("NVDA", risk=4.0, catalyst=LIVE_NVDA_CATALYST)
        row[key] = True
        assert TargetPosition(**row).subfloor_catalyst_verified is False, key

    row = _target("NVDA", risk=4.0, catalyst=LIVE_NVDA_CATALYST)
    row["subfloor_catalyst_verified"] = True

    result = _apply(
        _decision([row]),
        [_subfloor_analysis("NVDA")],
        real_reward_risk_by_symbol=_unmeasurable("NVDA"),
    )
    assert [t.symbol for t in result.targets] == ["NVDA"]
    assert result.targets[0].subfloor_catalyst_verified is False


def test_a_replayed_historical_row_cannot_claim_a_check_that_never_ran():
    """`src/replay.py` and the Mission Control API both re-validate stored PM
    output through this model. A stored row asserting the flag would be
    claiming a verification that may never have happened."""
    from src.models import TargetPosition

    row = _target("NVDA", risk=0.5, catalyst="2026-08-31: Lambda cloud deal")
    row["subfloor_catalyst_verified"] = True
    assert TargetPosition.model_validate(row).subfloor_catalyst_verified is False


def test_an_unflagged_subfloor_range_target_is_no_longer_refused():
    """**Inverted 2026-09-11 by docs/WORK.md item 1(d).** This used to assert
    the constructor refused a thin-but-real range payoff that carried no
    verified catalyst. It no longer does: the ratio is measured from this
    trade's own real support and its own real resistance, and a real
    per-trade signal is a ranking input, not a veto. The trade is built; the
    PM gate has already capped it at starter size, and
    `rank_candidates` ranks it below better payoffs.

    The stop is unchanged — item 1(d) touched no risk-side rule."""
    from src.portfolio_constructor import PortfolioConstructor
    from src.models import TargetPosition

    analysis = _subfloor_analysis()
    constructor = PortfolioConstructor()
    assert analysis.risk_reward == 1.2, "fixture must be measurably sub-floor"
    target = TargetPosition(
        symbol="AAA",
        conviction="medium",
        direction="long",
        thesis="t",
        risk_allocation_pct=0.5,
    )
    assert target.subfloor_catalyst_verified is False
    assert constructor._resolve_entry_and_stop(target, analysis, 100.0) == (
        100.0,
        95.0,
    )


def test_a_verified_exception_actually_reaches_a_buildable_entry_and_stop():
    """THE REGRESSION. This is the assertion whose absence made (b) and (c)
    inert — the PM said yes, the constructor said no, and no test asked the
    constructor."""
    from src.portfolio_constructor import PortfolioConstructor

    result, analysis = _verified_nvda_decision()
    target = result.targets[0]
    constructor = PortfolioConstructor()
    entry, stop = constructor._resolve_entry_and_stop(target, analysis, 100.0)
    assert entry == 100.0
    assert stop == 95.0, (
        "the level-backed stop must ship as-is — the exception lifts the reward:risk refusal, it does not move the stop"
    )


def test_the_exception_lifts_only_the_floor_never_unmeasurable_geometry():
    """Permission to take a poor payoff is not permission to take an unknown
    one. A NaN makes every `ratio < floor` comparison False, which is exactly
    how an unmeasurable trade would otherwise walk through a lifted floor."""
    from src.portfolio_constructor import PortfolioConstructor

    constructor = PortfolioConstructor()
    analysis = _subfloor_analysis()
    assert (
        constructor._widen_stop_past_noise(
            "AAA",
            analysis,
            100.0,
            95.0,
            direction="long",
            target_price=float("nan"),
            subfloor_catalyst_exception=True,
        )
        == 95.0
    )


def test_the_exception_does_not_rescue_a_stop_on_the_wrong_side_of_entry():
    from src.portfolio_constructor import PortfolioConstructor

    constructor = PortfolioConstructor()
    analysis = _subfloor_analysis()
    assert (
        constructor._widen_stop_past_noise(
            "AAA",
            analysis,
            100.0,
            105.0,
            direction="long",
            target_price=106.0,
            subfloor_catalyst_exception=True,
        )
        is None
    )


def test_the_built_order_carries_the_exception_to_the_execution_stage():
    """`TradeDecision.subfloor_catalyst_exception` records that Python
    verified a dated catalyst for an unmeasurable range payoff. The
    execution belt that used to read it is gone; the flag still has to
    survive construction."""
    from src.portfolio_constructor import PortfolioConstructor

    result, analysis = _verified_nvda_decision()
    target = result.targets[0]
    constructor = PortfolioConstructor()
    orders = constructor.construct_orders(
        targets=[target],
        analyses=[analysis],
        positions=[],
        total_value=100_000.0,
        price_map={"NVDA": 100.0},
    )
    buys = [o for o in orders if o.action == "BUY" and o.symbol == "NVDA"]
    assert buys, f"the verified exception produced no BUY: {orders!r}"
    assert buys[0].subfloor_catalyst_exception is False
    assert buys[0].stop_loss == 95.0


def test_an_ordinary_built_order_does_not_carry_the_exception():
    from src.portfolio_constructor import PortfolioConstructor
    from src.models import TargetPosition

    # Same shape as `_subfloor_analysis` but with the computed resistance
    # far enough out to clear the floor honestly (real R/R 2.4), so this
    # control isolates the flag and nothing else.
    analysis = TechAnalysisResult(
        symbol="GEV",
        rating="buy",
        conviction="medium",
        entry_price=100.0,
        stop_loss=95.0,
        reference_target=112.0,
        support_levels=[95.0],
        resistance_levels=[112.0],
        computed_levels=[95.0, 112.0],
        atr_14=(100.0 - 95.0) / 3.5,
        setup_type="range",
        expected_horizon_sessions=60,
        reasoning="test",
        reasoning_chain=_tech_rc(),
        thesis_invalid_if="closes below support",
    )
    target = TargetPosition(
        symbol="GEV",
        conviction="medium",
        direction="long",
        thesis="t",
        risk_allocation_pct=2.0,
    )
    constructor = PortfolioConstructor()
    orders = constructor.construct_orders(
        targets=[target],
        analyses=[analysis],
        positions=[],
        total_value=100_000.0,
        price_map={"GEV": 100.0},
    )
    buys = [o for o in orders if o.action == "BUY"]
    assert buys, f"the control trade must still build: {orders!r}"
    assert buys[0].subfloor_catalyst_exception is False


# --------------------------------------------------------------------------
# Execution-time payoff skip (owner 2026-09-17).
#
# Invented numeric belts are gone. A computed ratio cannot skip. Only a
# RANGE order whose executed prices cannot compute a payoff at all is
# refused, with reason geometry_unmeasurable — not geometry_rr.
# --------------------------------------------------------------------------


def _order(*, entry, stop, target, exception: bool, action="BUY"):
    from src.models import TradeDecision

    return TradeDecision(
        action=action,
        symbol="NVDA",
        allocation_pct=1.0,
        entry_price=entry,
        stop_loss=stop,
        take_profit=target,
        reasoning="r",
        subfloor_catalyst_exception=exception,
    )


def test_a_thin_but_measurable_executed_ratio_does_not_skip():
    from src.pipeline_stages import _execution_payoff_skip_reason

    order = _order(entry=100.0, stop=95.0, target=104.0, exception=False)
    assert order.reward_risk is not None and order.reward_risk < 1.2
    assert (
        _execution_payoff_skip_reason(
            order,
            sizing_price=101.0,
            stop_price=95.0,
            geometry_changed=True,
            is_short=False,
        )
        is None
    )


def test_an_exception_flag_does_not_reinstate_a_numeric_belt():
    from src.pipeline_stages import _execution_payoff_skip_reason

    order = _order(entry=100.0, stop=95.0, target=104.0, exception=True)
    assert (
        _execution_payoff_skip_reason(
            order,
            sizing_price=101.0,
            stop_price=95.0,
            geometry_changed=True,
            is_short=False,
        )
        is None
    )


def test_unmeasurable_executed_range_payoff_does_not_skip():
    from src.models import TradeDecision
    from src.pipeline_stages import _execution_payoff_skip_reason

    order = TradeDecision.model_construct(
        action="BUY",
        symbol="NVDA",
        allocation_pct=1.0,
        entry_price=100.0,
        stop_loss=95.0,
        take_profit=float("nan"),
        reasoning="r",
        setup_type="range",
        subfloor_catalyst_exception=True,
    )
    reason = _execution_payoff_skip_reason(
        order,
        sizing_price=100.0,
        stop_price=95.0,
        geometry_changed=True,
        is_short=False,
    )
    assert reason is None


def test_a_short_is_not_skipped_on_the_reward_side():
    from src.pipeline_stages import _execution_payoff_skip_reason

    order = _order(
        entry=100.0,
        stop=105.0,
        target=96.0,
        exception=True,
        action="SHORT",
    )
    assert (
        _execution_payoff_skip_reason(
            order,
            sizing_price=99.0,
            stop_price=105.0,
            geometry_changed=True,
            is_short=True,
        )
        is None
    )


def test_the_execution_flag_survives_a_risk_manager_modification_rebuild():
    """`TradingPipeline._apply_risk_modifications` rebuilds the decision via
    `TradeDecision(**decision.model_dump())`. The catalyst-verified flag
    must survive that round trip even though it no longer changes an
    execution belt (the belt is gone)."""
    from src.models import TradeDecision

    order = _order(entry=100.0, stop=95.0, target=106.0, exception=True)
    rebuilt = TradeDecision(**order.model_dump())
    assert rebuilt.subfloor_catalyst_exception is True


# --------------------------------------------------------------------------
# Pre-existing latent defect this fix unmasked (2026-09-11).
# --------------------------------------------------------------------------


def test_capping_a_target_does_not_unset_its_other_defaulted_fields():
    """`LLMOutputModel._explicit_null_means_absent` deletes a key so the
    declared default applies — correct on construction, DESTRUCTIVE on the
    `validate_assignment` path, where the returned dict is the instance's
    complete new state and a deleted key leaves the field genuinely unset.

    The sub-floor gate's own starter-size cap is an assignment, so capping a
    target deleted `thesis_invalid_if` and `catalyst` from it and
    `PortfolioConstructor._build_buy` then raised `AttributeError` reading
    `target.thesis_invalid_if`. It was latent on main only because the
    constructor refused every capped order earlier for an unrelated reason —
    the very refusal this branch fixes.
    """
    from src.models import TargetPosition

    target = TargetPosition(
        symbol="AAA",
        conviction="medium",
        direction="long",
        thesis="t",
        risk_allocation_pct=4.0,
    )
    target.risk_allocation_pct = STARTER_POSITION_RISK_PCT
    assert target.thesis_invalid_if == ""
    assert target.catalyst == ""
    assert target.risk_allocation_pct == STARTER_POSITION_RISK_PCT
