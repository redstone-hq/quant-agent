"""Risk-based position sizing through the constructor — spec §2.1, §2.2, §2.4.

The failure this replaces: `target_weight_pct` is risk-blind. "BUY OKLO 3%"
says nothing about what the trade costs when it is wrong — a 3% position
stopped 10% away risks 0.3% of equity, the same 3% stopped 2% away risks
0.06%. The PM was choosing the number that does not determine the loss, while
the number that does (the stop distance) was set by the technical analyst.

Under §2.1 conviction is stated as risk and the stop determines the size, so a
wider stop produces a smaller position instead of a rejected trade. That is
what removes the incentive to squeeze stops, which is what was firing exits
inside ordinary noise.
"""

from src.models import Position, TargetPosition, TechAnalysisResult, TechReasoningChain
from src.portfolio_constructor import ConstructorConfig, PortfolioConstructor


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
    entry: float,
    stop: float,
    target: float,
    horizon: int = 60,
    atr: float | None = None,
) -> TechAnalysisResult:
    """A realistic analyst result, including the fields production sets in
    Python rather than asking the model for.

    `atr_14` and `computed_levels` are attached by `TechAnalystAgent` after
    parsing; the constructor has derived the take-profit from
    `computed_levels` since 2026-09-01 and refuses without them. The default
    ATR sits just inside the noise band so the structural stop is left alone
    — these tests are about SIZING, and the widening tests below set their
    own ATR explicitly.
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
        computed_level_touches={stop: 5, target: 5},
        computed_level_bars={stop: [(stop, stop)], target: [(target, target)]},
        atr_14=(entry - stop) / 3.5 if atr is None else atr,
        setup_type="range",
        expected_horizon_sessions=horizon,
        reasoning_chain=_tech_rc(),
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


def _risk_target(symbol: str, risk_pct: float, **kw) -> TargetPosition:
    return TargetPosition(
        symbol=symbol,
        risk_allocation_pct=risk_pct,
        conviction="high",
        thesis=kw.pop("thesis", "thesis"),
        **kw,
    )


EQUITY = 100_000.0


# --------------------------------------------------------------------------
# §2.1 — the stop sets the size
# --------------------------------------------------------------------------


def test_size_derives_from_risk_and_stop_distance():
    """shares = (equity x risk_pct) / |entry - stop|.

    2% of $100k = $2,000 at risk. Entry $100, stop $90 → $10/share → 200
    shares → $20,000 → 20% of the book.
    """
    constructor = PortfolioConstructor()
    decisions = constructor.construct_orders(
        targets=[_risk_target("NVDA", 2.0)],
        positions=[],
        analyses=[_analysis("NVDA", entry=100, stop=90, target=140)],
        total_value=EQUITY,
        price_map={"NVDA": 100.0},
    )
    assert len(decisions) == 1
    assert abs(decisions[0].allocation_pct - 20.0) < 0.05


def test_a_wider_stop_yields_a_smaller_position_not_a_rejected_trade():
    """The whole point of §2.1. Same conviction, same equity, twice the stop
    distance — half the position, and both trades risk exactly 1%.

    Sized at 1% risk against 10% and 20% stops deliberately: the proportion is
    the invariant under test, and it is only observable where the 20%
    single-name notional ceiling is not the binding constraint. At this book's
    real stop distances the ceiling binds first and both positions come out
    the same size — see
    `test_the_ceiling_flattens_conviction_at_realistic_stop_distances`."""
    constructor = PortfolioConstructor()

    def alloc(stop):
        d = constructor.construct_orders(
            targets=[_risk_target("NVDA", 1.0)],
            positions=[],
            analyses=[_analysis("NVDA", entry=100, stop=stop, target=140)],
            total_value=EQUITY,
            price_map={"NVDA": 100.0},
        )
        return d[0].allocation_pct

    tight, wide = alloc(90), alloc(80)
    assert abs(tight - 10.0) < 0.05
    assert abs(wide - 5.0) < 0.05
    # Both put the same dollars at risk — that is the invariant §2.1 buys.
    assert abs(tight / 100 * EQUITY * 0.10 - 1000) < 20  # 10/100 stop gap
    assert abs(wide / 100 * EQUITY * 0.20 - 1000) < 20  # 20/100 stop gap


def test_a_position_with_no_typed_stop_is_sized_against_the_instruments_own_stop():
    """docs/WORK.md item 54 (2026-09-12): a stop is always derivable. With
    nothing typed, the stop is read from the instrument — the ATR noise
    band here (the signal bar is not set on this fixture) — and the size
    follows §2.1 from THAT distance. Until this date "no stop" was "no
    trade" (Phase 1); sourced research showed no published method refuses
    a trade for lack of a stop level, so the desk reads one and sizes down
    if it is wide."""
    constructor = PortfolioConstructor()
    analysis = _analysis("NVDA", entry=100, stop=90, target=140, atr=2.0)
    analysis = analysis.model_copy(update={"stop_loss": 0.0})
    decisions = constructor.construct_orders(
        targets=[_risk_target("NVDA", 2.0)],
        positions=[],
        analyses=[analysis],
        total_value=EQUITY,
        price_map={"NVDA": 100.0},
    )
    assert [d.action for d in decisions] == ["BUY"]
    band = 100.0 - constructor._stop_atr_multiple() * 2.0
    assert abs(decisions[0].stop_loss - round(band, 2)) < 1e-9
    # $2,000 of risk over the band's distance, as §2.1 says — not the
    # $10/share the model's (deleted) stop would have implied.
    expected_alloc = 2.0 * 100.0 / (100.0 - band)
    assert abs(decisions[0].allocation_pct - expected_alloc) < 0.05


def test_single_name_risk_is_clamped_to_the_ratified_envelope():
    """5% per trade is the owner-ratified ceiling (2026-08-27). A PM asking
    for more is clamped rather than refused — the idea is sound, the size is
    not. The schema caps at 5 too; this is the deterministic backstop."""
    constructor = PortfolioConstructor(ConstructorConfig(risk_budget_pct=3.0))
    decisions = constructor.construct_orders(
        targets=[_risk_target("NVDA", 5.0)],
        positions=[],
        # Target $160, not $140: the 30-wide stop is deliberately far
        # outside the noise band, and since 2026-09-02 that path is gated
        # on reward:risk like every other. (140-100)/30 = 1.33 is under the
        # 1.5 floor and would be refused on GEOMETRY, which is not what
        # this test is about. At $160 the ratio is 2.0 and the sizing clamp
        # is once again the only thing under test.
        analyses=[_analysis("NVDA", entry=100, stop=70, target=160)],
        total_value=EQUITY,
        price_map={"NVDA": 100.0},
    )
    # Clamped to 3% risk → 3000/30 = 100 shares → $10k → 10%. The 30%-wide
    # stop keeps the 20% single-name notional ceiling out of the way so the
    # RISK clamp is what this asserts.
    assert abs(decisions[0].allocation_pct - 10.0) < 0.05


def test_zero_risk_closes_a_held_position():
    """`risk_allocation_pct == 0` is "exit this name", and must reach the sell
    builder rather than being swallowed by the churn filter — the 2026-07-16
    audit finding, which must survive the move to risk units."""
    constructor = PortfolioConstructor()
    decisions = constructor.construct_orders(
        targets=[_risk_target("AAPL", 0.0, thesis="thesis broken")],
        positions=[_pos("AAPL", qty=50, avg_entry=180, current_price=200)],
        analyses=[],
        total_value=EQUITY,
        price_map={"AAPL": 200.0},
    )
    assert len(decisions) == 1
    assert decisions[0].action == "SELL"
    assert decisions[0].allocation_pct == 100.0


def test_a_close_does_not_need_a_price_or_a_stop():
    """Routing an exit through the pricing checks would let a missing quote
    silently cancel a decision the PM had already made."""
    constructor = PortfolioConstructor()
    decisions = constructor.construct_orders(
        targets=[_risk_target("AAPL", 0.0)],
        positions=[_pos("AAPL", qty=50, avg_entry=180, current_price=200)],
        analyses=[],
        total_value=EQUITY,
        price_map={},
    )
    assert len(decisions) == 1 and decisions[0].action == "SELL"


def test_legacy_notional_targets_still_size_the_old_way():
    """Historical agent_logs and specialist_evidence rows re-validate through
    TargetPosition (src/replay.py, the Mission Control API). A stored decision
    carrying only `target_weight_pct` must still construct."""
    constructor = PortfolioConstructor()
    decisions = constructor.construct_orders(
        targets=[TargetPosition(symbol="NVDA", target_weight_pct=12.0, conviction="high", thesis="legacy")],
        positions=[],
        analyses=[_analysis("NVDA", entry=100, stop=95, target=115)],
        total_value=EQUITY,
        price_map={"NVDA": 100.0},
    )
    # 12% at a 5% stop risks 0.6%: above the owner's 0.5% minimum risk per
    # position (owner rule 2026-08-27). 8% (0.4% risk) is now refused.
    assert len(decisions) == 1
    assert abs(decisions[0].allocation_pct - 12.0) < 0.05


# --------------------------------------------------------------------------
# §2.2 — the portfolio budget, enforced
# --------------------------------------------------------------------------

NUCLEAR = ["CCJ", "CEG", "OKLO", "VST"]


def _nuclear_setup(*symbols):
    targets = [_risk_target(s, 5.0) for s in symbols]
    analyses = [_analysis(s, entry=100, stop=90, target=140) for s in symbols]
    prices = {s: 100.0 for s in symbols}
    return targets, analyses, prices


def test_a_single_theme_cannot_take_the_whole_risk_budget():
    """Four nuclear names at 5% risk each is 20% total — under the 25%
    ceiling, and one 20% bet. The cluster cap must cut it to 10%."""
    constructor = PortfolioConstructor()
    targets, analyses, prices = _nuclear_setup(*NUCLEAR)
    decisions = constructor.construct_orders(
        targets=targets,
        positions=[],
        analyses=analyses,
        total_value=EQUITY,
        price_map=prices,
        existing_risk_pct={},
        clusters=[NUCLEAR],
    )
    # Each granted 5% risk becomes a 50% notional weight at a 10% stop gap, so
    # count orders rather than weights: only two names survive the 10% cap.
    assert {d.symbol for d in decisions} == {"CCJ", "CEG"}


def test_the_budget_gate_is_inert_when_the_caller_supplies_no_book_risk():
    """Without `existing_risk_pct`/`clusters` the constructor has no view of
    the book's risk and must not invent one. Per-position sizing still
    applies; the portfolio ceilings do not.

    Sector concentration is a SEPARATE, unrelated ceiling that also applies
    to these four same-sector names — since the 2026-09-04 single-name cap
    raise (20 -> 100) each now asks for its full 50% notional rather than
    being pre-clamped to 20%, which is enough for real, same-sector names to
    hit the (also real, unchanged) sector hard cap. That is correct
    behaviour, not this test's concern, so the sector dial is loosened here
    to isolate the risk-budget gate this test is actually about."""
    constructor = PortfolioConstructor(ConstructorConfig())
    targets, analyses, prices = _nuclear_setup(*NUCLEAR)
    decisions = constructor.construct_orders(
        targets=targets,
        positions=[],
        analyses=analyses,
        total_value=EQUITY,
        price_map=prices,
    )
    assert len(decisions) == 4


def test_held_risk_crowds_out_a_new_name_in_the_same_theme():
    constructor = PortfolioConstructor()
    decisions = constructor.construct_orders(
        targets=[_risk_target("OKLO", 5.0)],
        positions=[],
        analyses=[_analysis("OKLO", entry=100, stop=90, target=140)],
        total_value=EQUITY,
        price_map={"OKLO": 100.0},
        existing_risk_pct={"CEG": 8.0},
        clusters=[NUCLEAR],
    )
    # 10% cluster cap less CEG's 8% = 2% risk → 2000/10 = 200 sh → $20k → 20%.
    assert abs(decisions[0].allocation_pct - 20.0) < 0.05


def test_a_budget_cut_is_explained_in_the_order_reasoning():
    """The AI Risk Manager audits the constructed order against the PM's
    prose. On 2026-08-20 an unexplained deterministic cut read as "plan
    inconsistency" and drew a full-plan veto."""
    constructor = PortfolioConstructor()
    decisions = constructor.construct_orders(
        targets=[_risk_target("OKLO", 5.0)],
        positions=[],
        analyses=[_analysis("OKLO", entry=100, stop=90, target=140)],
        total_value=EQUITY,
        price_map={"OKLO": 100.0},
        existing_risk_pct={"CEG": 8.0},
        clusters=[NUCLEAR],
    )
    reasoning = decisions[0].reasoning
    assert "risk budget" in reasoning
    assert "cut from 5.00% to 2.00%" in reasoning
    assert "not PM inconsistency" in reasoning


def test_a_full_budget_denies_a_new_name_outright():
    constructor = PortfolioConstructor()
    decisions = constructor.construct_orders(
        targets=[_risk_target("NVDA", 5.0)],
        positions=[],
        analyses=[_analysis("NVDA", entry=100, stop=90, target=140)],
        total_value=EQUITY,
        price_map={"NVDA": 100.0},
        existing_risk_pct={"HELD": 25.0},
        clusters=[],
    )
    assert decisions == []


def test_raising_the_single_name_notional_cap_does_not_raise_the_total_risk_ceiling():
    """2026-09-04: step 2 of the notional-cap fix, pinned as a test.

    (Numbers restated 2026-09-11 when `max_position_pct` moved 100 -> 33 as
    a survival ceiling — the POINT of this test is unchanged and is the
    reason the restated numbers were chosen to keep the notional clamp out
    of the way: what binds here must still be the RISK ceiling.)

    `max_position_pct` moved 20 -> 100 so a tight-but-realistic stop no
    longer silently caps delivered risk. That is a NOTIONAL ceiling; the
    25% `max_portfolio_risk_pct` book-wide ceiling is a separate, RISK
    ceiling enforced earlier in the same pipeline (`allocate_risk_budget`,
    applied before the single-name notional clamp ever runs — see
    `_build_buy` in src/portfolio_constructor.py). Freeing the notional
    clamp must not free the risk one: a target that would now be large
    enough in notional terms to want more of the book still gets cut to
    whatever total risk is actually left, exactly as it did before this
    fix, at the SAME numbers.
    """
    constructor = PortfolioConstructor()
    # A tight, realistic stop (5%) and full 5% conviction ask for 100%
    # notional — reachable now, unlike before this fix — but the book
    # already carries 24% of the ratified 25% ceiling, leaving only 1%.
    decisions = constructor.construct_orders(
        targets=[_risk_target("NVDA", 5.0)],
        positions=[],
        analyses=[_analysis("NVDA", entry=100, stop=95, target=140)],
        total_value=EQUITY,
        price_map={"NVDA": 100.0},
        existing_risk_pct={"HELD": 24.0},
        clusters=[],
    )
    assert len(decisions) == 1
    # 1% of the risk budget left, at a 5% stop: $1,000 risk / $5 per share
    # -> 200 shares -> $20,000 -> 20% notional. Under the single-name
    # ceiling (33 as of the 2026-09-11 survival-ceiling fix, 65 after the
    # owner's same-day override) and the sector's 90%, so neither notional
    # ceiling is touched — the RISK ceiling is what binds, and it is
    # unmoved by either notional-cap change.
    assert abs(decisions[0].allocation_pct - 20.0) < 0.05
    assert abs(decisions[0].allocated_risk_pct - 1.0) < 0.05

    # And a fully-spent 25% budget still denies the trade outright, same as
    # test_a_full_budget_denies_a_new_name_outright above — unaffected by
    # the single-name notional cap now being 5x looser.
    denied = constructor.construct_orders(
        targets=[_risk_target("NVDA", 5.0)],
        positions=[],
        analyses=[_analysis("NVDA", entry=100, stop=95, target=140)],
        total_value=EQUITY,
        price_map={"NVDA": 100.0},
        existing_risk_pct={"HELD": 25.0},
        clusters=[],
    )
    assert denied == []


def test_released_risk_frees_budget_for_a_new_position():
    """Spec §2.3/§2.4: a position whose stop has reached entry contributes
    zero budget risk, so the book expands while trades work — with nobody
    choosing a position count. The caller computes the release
    (`src/risk/metrics.py`); the constructor simply spends what is left."""
    constructor = PortfolioConstructor()
    common = dict(
        targets=[_risk_target("NVDA", 5.0)],
        positions=[],
        # $160 for the same reason as
        # `test_single_name_risk_is_clamped_to_the_ratified_envelope`: the
        # 30-wide stop clears the noise band, and the reward:risk floor now
        # applies on that path too. This test is about the risk BUDGET.
        analyses=[_analysis("NVDA", entry=100, stop=70, target=160)],
        total_value=EQUITY,
        price_map={"NVDA": 100.0},
        clusters=[],
    )
    blocked = constructor.construct_orders(existing_risk_pct={"WINNER": 25.0}, **common)
    freed = constructor.construct_orders(existing_risk_pct={"WINNER": 0.0}, **common)
    assert blocked == []
    assert len(freed) == 1
    # 5% risk against a 30%-wide stop → $5k/$30 per share → $16.7k → 16.7%,
    # under the single-name ceiling so the released BUDGET is what is measured.
    assert abs(freed[0].allocation_pct - 16.67) < 0.05


# --------------------------------------------------------------------------
# Wiring — the gate has to reach the constructor, and the cap the PM
# --------------------------------------------------------------------------


def test_book_risk_inputs_convert_heat_into_percent_of_equity():
    """The constructor rations in % of equity; heat is measured in dollars."""
    from types import SimpleNamespace

    from src.pipeline_stages import _book_risk_inputs

    ctx = SimpleNamespace(
        facts=SimpleNamespace(
            heat=SimpleNamespace(
                per_position=[
                    SimpleNamespace(symbol="OKLO", budget_risk_dollars=2_000.0),
                    SimpleNamespace(symbol="CEG", budget_risk_dollars=500.0),
                ]
            ),
            correlation_clusters=[["OKLO", "CEG"]],
        )
    )
    existing, clusters = _book_risk_inputs(ctx, total_value=100_000.0)
    assert existing == {"OKLO": 2.0, "CEG": 0.5}
    assert clusters == [["OKLO", "CEG"]]


def test_book_risk_inputs_mark_the_book_unknown_when_heat_cannot_be_seen():
    """Defect 2026-10-09: returning None here SKIPPED the 25% ceiling for
    every trade. A heat failure, or no PM facts at all, now yields a book
    whose risk is UNKNOWN, so the constructor refuses new buys (closes,
    trims, exits and stops still pass)."""
    from types import SimpleNamespace

    from src.pipeline_stages import _book_risk_inputs

    for ctx in (SimpleNamespace(facts=None), SimpleNamespace()):
        existing, clusters = _book_risk_inputs(ctx, 100_000.0)
        assert existing == {} and existing.unknown and clusters is None
    ctx = SimpleNamespace(facts=SimpleNamespace(heat=None, correlation_clusters=[]))
    existing, clusters = _book_risk_inputs(ctx, 100_000.0)
    assert existing == {} and existing.unknown and clusters is None


def test_book_risk_inputs_return_clusters_alone_when_only_heat_fails():
    """Heat and clusters are built independently in `_book_risk_inputs`; a
    heat failure (or a book with no facts.heat at all) must not silently
    swallow clusters that DID build. This is the partial-failure input the
    2026-09-03 incident is about — the caller, not this function, is what
    has to treat a lone `existing=None` as unenforceable."""
    from types import SimpleNamespace

    from src.pipeline_stages import _book_risk_inputs

    ctx = SimpleNamespace(
        facts=SimpleNamespace(
            heat=None,
            correlation_clusters=[["OKLO", "CEG"]],
        )
    )
    existing, clusters = _book_risk_inputs(ctx, total_value=100_000.0)
    assert existing is not None and existing.unknown  # unknown, never skipped
    assert clusters == [["OKLO", "CEG"]]


def test_a_heat_failure_leaves_the_budget_unenforced_even_with_clusters():
    """2026-09-03 incident: `allocate_risk_budget` treats a missing
    `existing_pct` as an empty dict — a book with ZERO risk — not as
    "unknown". Before the fix, `clusters` being present was enough on its
    own to run the allocator, so a heat failure made the cluster cap bind
    against a held book the constructor could not actually see. The fix
    requires `existing_risk_pct` before the allocator runs at all; clusters
    alone must leave the ceilings unenforced, exactly like the both-missing
    case in `test_the_budget_gate_is_inert_when_the_caller_supplies_no_book_risk`.

    Sector dial loosened here for the same reason as that sibling test —
    it is a separate, unrelated ceiling this test is not about."""
    constructor = PortfolioConstructor(ConstructorConfig())
    targets, analyses, prices = _nuclear_setup(*NUCLEAR)
    decisions = constructor.construct_orders(
        targets=targets,
        positions=[],
        analyses=analyses,
        total_value=EQUITY,
        price_map=prices,
        existing_risk_pct=None,
        clusters=[NUCLEAR],
    )
    # All four survive: the cluster cap must NOT bind when the book's
    # existing risk is unknown. (Compare test_a_single_theme_cannot_take_
    # the_whole_risk_budget, where existing_risk_pct={} — a KNOWN empty book
    # — correctly lets the cap cut this down to two.)
    assert {d.symbol for d in decisions} == set(NUCLEAR)


def test_the_cluster_cap_is_rendered_to_the_portfolio_manager():
    """The PM is told a per-cluster cap exists; it must also be shown the
    number, or it sizes a theme blind and meets the cap as a surprise."""
    from src.pipeline_context import PMFacts

    facts = PMFacts()
    facts.correlation_coverage = True
    facts.correlation_clusters = [["CCJ", "CEG", "OKLO", "VST"]]
    facts.risk_ceiling_pct = 25.0
    facts.cluster_risk_share_pct = 40.0
    block = facts._render_correlation()
    assert "at most 10.0% of equity at risk" in block
    assert "40% of the 25.0% total" in block
    assert "CCJ / CEG / OKLO / VST" in block


def test_risk_envelope_reaches_the_constructor_from_config():
    """The ratified envelope lives in `risk:` config, not in a dataclass
    default nobody chose — which is how the 0.5% per-trade figure survived
    inherited. It was written by the UPSTREAM author (`995fcb0`, yebof,
    2026-04-18), before QAMC existed — QAMC's own history begins
    2026-08-09. It was never a QAMC decision that went stale; it was a
    default that came with the fork and that nobody had revisited."""
    from pathlib import Path

    import yaml

    from src.config import RiskConfig

    # The deployed YAML is read directly rather than through `load_config`:
    # the hermetic suite has no OPENROUTER_API_KEY, so full AppConfig
    # validation fails for reasons unrelated to the risk envelope.
    root = Path(__file__).resolve().parent.parent
    raw = yaml.safe_load((root / "config" / "settings.yaml").read_text())["risk"]
    assert raw["max_position_risk_pct"] == 5
    assert raw["min_position_risk_pct"] == 0.5
    assert raw["max_portfolio_risk_pct"] == 25
    assert raw["max_cluster_risk_share_pct"] == 40

    # And the defaults agree, so a config that omits them lands in the same
    # place rather than silently reverting to a looser envelope.
    defaults = RiskConfig(
        max_position_pct=20,
        max_total_position_pct=90,
        require_stop_loss=True,
    )
    assert defaults.max_position_risk_pct == 5.0
    assert defaults.min_position_risk_pct == 0.5
    assert defaults.max_portfolio_risk_pct == 25.0
    assert defaults.max_cluster_risk_share_pct == 40.0


def test_the_api_reports_risk_sized_targets_rather_than_dropping_them():
    """Spec §2.1 regression guard.

    `CandidateFunnelItem.pm_target_weight_pct` reads
    `TargetPosition.target_weight_pct`, which is None for every risk-sized
    target. Reporting only that field silently dropped the size of every
    target the PM sized the new way — the cockpit rendered the candidate with
    no size at all. The two fields are not interchangeable and neither is
    derivable from the other without the stop, so both are carried and the
    view shows whichever the PM actually stated.
    """
    from src.api.schemas import CandidateFunnelItem

    item = CandidateFunnelItem(
        symbol="NVDA",
        direction="bullish",
        is_bearish_hedge=False,
        reached_pm_target=True,
        pm_target_weight_pct=None,
        pm_risk_allocation_pct=2.0,
        reached_proposed_order=False,
        risk_modified=False,
        executed=False,
    )
    assert item.pm_risk_allocation_pct == 2.0
    assert item.pm_target_weight_pct is None

    # A legacy notional target still reports the way it always did.
    legacy = CandidateFunnelItem(
        symbol="NVDA",
        direction="bullish",
        is_bearish_hedge=False,
        reached_pm_target=True,
        pm_target_weight_pct=8.0,
        pm_risk_allocation_pct=None,
        reached_proposed_order=False,
        risk_modified=False,
        executed=False,
    )
    assert legacy.pm_target_weight_pct == 8.0
    assert legacy.pm_risk_allocation_pct is None


# --------------------------------------------------------------------------
# The single-name notional ceiling — where §2.1 meets `max_position_pct`
# --------------------------------------------------------------------------


def test_size_is_clamped_to_the_single_name_ceiling_not_left_to_be_blocked():
    """`max_position_pct` is a HARD BLOCK, not a trim.

    Risk-based sizing asks for `risk_pct x entry / (entry - stop)` of equity.
    A tight enough stop can still ask for more than the single-name ceiling
    (100% since 2026-09-04 — see settings.yaml's `risk.max_position_pct` for
    the full derivation) in one name. `max_position_pct` sits in
    `HARD_BLOCK_RULES`, so the risk engine does not shrink that order, it
    drops it, and the trade never happens. The constructor therefore has to
    size UNDER the ceiling itself. Mechanism test — an explicit tighter
    config is used so a realistic stop still exercises the clamp; the real
    deployed number is covered separately below.
    """
    from src.risk.rules import RiskRuleEngine
    from src.config import RiskConfig

    constructor = PortfolioConstructor(ConstructorConfig(max_position_pct=20.0))
    decisions = constructor.construct_orders(
        targets=[_risk_target("NVDA", 2.0)],
        positions=[],
        analyses=[_analysis("NVDA", entry=100, stop=95, target=140)],
        total_value=EQUITY,
        price_map={"NVDA": 100.0},
    )
    assert len(decisions) == 1
    assert decisions[0].allocation_pct <= 20.0

    # And the engine that would have blocked it now passes it.
    engine = RiskRuleEngine(
        RiskConfig(
            max_position_pct=20,
            max_total_position_pct=100,
            require_stop_loss=True,
        )
    )
    violations = engine.check(
        decisions[0],
        [],
        EQUITY,
        0.0,
        cash=EQUITY,
    )
    assert [v.rule for v in violations] == []


def test_a_clamped_size_says_so_in_the_reasoning():
    """The AI Risk Manager audits the constructed order against PM's prose.

    On 2026-08-20 an unexplained deterministic cut read to it as "plan
    inconsistency" and drew a full-plan veto, so every cut carries its
    arithmetic. A clamped position also risks LESS than the PM allocated —
    that has to be visible, not silent.
    """
    constructor = PortfolioConstructor()
    decisions = constructor.construct_orders(
        targets=[_risk_target("NVDA", 4.0)],
        positions=[],
        analyses=[_analysis("NVDA", entry=100, stop=97, target=140)],
        total_value=EQUITY,
        price_map={"NVDA": 100.0},
    )
    assert len(decisions) == 1
    assert "single-name ceiling" in decisions[0].reasoning
    assert "not PM inconsistency" in decisions[0].reasoning


def test_a_tight_stop_never_produces_an_unrepresentable_allocation():
    """Regression: `TradeDecision.allocation_pct` is bounded at 100.

    Before the clamp, 4% risk against a 3% stop computed a 133% allocation and
    raised a pydantic ValidationError *inside* `construct_orders` — an
    uncaught exception on the live decision path, which takes the session's
    order construction down rather than dropping one trade.
    """
    constructor = PortfolioConstructor()
    decisions = constructor.construct_orders(
        targets=[_risk_target("NVDA", 5.0)],
        positions=[],
        analyses=[_analysis("NVDA", entry=100, stop=98, target=140)],
        total_value=EQUITY,
        price_map={"NVDA": 100.0},
    )
    assert len(decisions) == 1
    assert 0 < decisions[0].allocation_pct <= 100.0


def test_the_ceiling_accounts_for_what_is_already_held():
    """The engine caps the POSITION, not the order, so the clamp must too.

    Mechanism test: an explicit tighter config exercises the clamp at a
    realistic held-position size (the real 100% deployed ceiling would need
    an implausibly large existing position to bind here at all)."""
    constructor = PortfolioConstructor(ConstructorConfig(max_position_pct=20.0))
    decisions = constructor.construct_orders(
        targets=[_risk_target("NVDA", 5.0)],
        positions=[_pos("NVDA", qty=150, avg_entry=100, current_price=100)],  # 15%
        analyses=[_analysis("NVDA", entry=100, stop=95, target=140)],
        total_value=EQUITY,
        price_map={"NVDA": 100.0},
    )
    assert len(decisions) == 1
    # 20% ceiling - 15% held = 5% of headroom, and not a basis point more.
    assert decisions[0].allocation_pct == 5.0


def test_a_fully_sized_name_produces_no_order_rather_than_a_zero_one():
    constructor = PortfolioConstructor(ConstructorConfig(max_position_pct=20.0))
    decisions = constructor.construct_orders(
        targets=[_risk_target("NVDA", 5.0)],
        positions=[_pos("NVDA", qty=200, avg_entry=100, current_price=100)],  # 20%
        analyses=[_analysis("NVDA", entry=100, stop=95, target=140)],
        total_value=EQUITY,
        price_map={"NVDA": 100.0},
    )
    assert decisions == []


def test_the_ceiling_no_longer_flattens_conviction_at_realistic_stop_distances():
    """2026-09-04 real-data audit, and the fix pinned as a test.

    RESTATED 2026-09-11, when `max_position_pct` moved 100 -> 33 -> 65 the
    same day (a SURVIVAL ceiling against single-name gap risk, then an
    owner risk-appetite override — see `risk.max_position_pct` in
    settings.yaml). The property this test protects is unchanged and is
    the one that matters: conviction must still CHANGE THE SIZE. At the
    old 20% ceiling it did not — low, moderate and high conviction all
    landed on the same 20% position, which destroys the entire premise of
    §2.1. At 65 the ceiling is a CEILING again rather than a flattener:
    below it conviction is fully expressed, and it only equalises trades
    that were both asking for more notional than one name may ever hold.
    """
    constructor = PortfolioConstructor()

    def alloc(risk_pct, stop):
        d = constructor.construct_orders(
            targets=[_risk_target("NVDA", risk_pct)],
            positions=[],
            analyses=[_analysis("NVDA", entry=100, stop=stop, target=140)],
            total_value=EQUITY,
            price_map={"NVDA": 100.0},
        )
        return d[0].allocation_pct

    # At a realistic stop distance (7.7%, the documented sample's own
    # measured max), the whole conviction range that fits under the ceiling
    # produces genuinely DIFFERENT positions — 0.5% risk -> ~6.5% notional,
    # 1.5% -> ~19.5%, 2.5% -> ~32.5%. This is the §2.1 property the 20%
    # ceiling used to destroy.
    low, moderate, high = alloc(0.5, 92.3), alloc(1.5, 92.3), alloc(2.5, 92.3)
    assert low < moderate < high
    assert abs(low - 0.5 / 7.7 * 100) < 0.5
    assert abs(moderate - 1.5 / 7.7 * 100) < 0.5
    assert abs(high - 2.5 / 7.7 * 100) < 0.5
    # Delivered risk below the ceiling is the risk that was ASKED FOR — the
    # ceiling is not quietly taxing ordinary trades.
    assert abs(moderate * 7.7 / 100 - 1.5) < 0.05


def test_the_survival_ceiling_caps_any_conviction_at_the_deployed_number():
    """2026-09-11. The hard ceiling no single trade may cross.

    `max_position_pct` is a SURVIVAL ceiling: it is the only parameter on
    this desk that bounds the loss when the stop does NOT fill (an
    overnight gap, a halt, a fraud disclosure). Every other risk number —
    `max_position_risk_pct`, the §9.4 agreement bands,
    `max_portfolio_risk_pct` — prices the loss assuming the stop DOES fill,
    so none of them constrain this failure mode at all.

    The property: no conviction, however high, and no stop, however tight,
    buys more than the deployed ceiling in one name.
    """
    constructor = PortfolioConstructor()

    def alloc(risk_pct, stop):
        d = constructor.construct_orders(
            targets=[_risk_target("NVDA", risk_pct)],
            positions=[],
            analyses=[_analysis("NVDA", entry=100, stop=stop, target=140)],
            total_value=EQUITY,
            price_map={"NVDA": 100.0},
        )
        return d[0].allocation_pct

    # 5% risk / 5% stop asks for 100% of equity in one name. 5% / 2.5%
    # asks for 200%. Both land on the same ceiling, and the ceiling is the
    # deployed number, not the sector cap standing in for it.
    from src.portfolio_constructor import ConstructorConfig as _CC

    ceiling = _CC().max_position_pct
    assert ceiling == 65.0
    assert alloc(5.0, 95.0) == ceiling
    assert alloc(5.0, 97.5) == ceiling
    # And the cap is what says so, in the audit trail — not the sector dial.
    d = constructor.construct_orders(
        targets=[_risk_target("NVDA", 5.0)],
        positions=[],
        analyses=[_analysis("NVDA", entry=100, stop=95, target=140)],
        total_value=EQUITY,
        price_map={"NVDA": 100.0},
    )
    assert "single-name ceiling" in d[0].reasoning


def test_the_deployed_ceiling_matches_settings_yaml():
    """The constructor default and the shipped setting are ONE number.

    `pipeline.py` wires the constructor from `risk.max_position_pct`, but
    the dataclass default is what every direct caller (and every test
    above) gets. A survival ceiling that is 33 in one file and something
    else in the other is not a ceiling — this is the drift check the
    "keep in sync" comments can only ask for.
    """
    import yaml
    from pathlib import Path
    from src.portfolio_constructor import ConstructorConfig as _CC

    settings = yaml.safe_load((Path(__file__).resolve().parent.parent / "config" / "settings.yaml").read_text())
    assert settings["risk"]["max_position_pct"] == 65
    assert _CC().max_position_pct == float(settings["risk"]["max_position_pct"])


def test_the_survival_ceiling_shrinks_trades_and_never_refuses_them():
    """The lower ceiling must not silently start producing no-trade days.

    Two floors sit below this cap and either would turn a clamp into a
    refusal if the ceiling went too low: `min_position_risk_pct` (0.5% —
    below this an idea is not worth trading) and `min_order_usd` ($500 — a
    position that small cannot pay for its own attention). At the TIGHTEST
    stop this desk permits (the level-backed exemption down to
    `absolute_min_stop_atr_multiple: 1.0`, ~2.56% of price at the median
    ATR) a 65% position still delivers ~1.66% risk and ~$65k of notional on
    this fixture, so the cap shrinks and does not refuse.
    """
    constructor = PortfolioConstructor()
    decisions = constructor.construct_orders(
        targets=[_risk_target("NVDA", 5.0)],
        positions=[],
        analyses=[_analysis("NVDA", entry=100, stop=97.44, target=140)],
        total_value=EQUITY,
        price_map={"NVDA": 100.0},
    )
    assert len(decisions) == 1
    d = decisions[0]
    assert d.allocation_pct == 65.0
    # Delivered risk = notional x stop distance. Comfortably above the
    # 0.5% floor, so nothing downstream refuses it for being immaterial.
    delivered_risk_pct = d.allocation_pct * 2.56 / 100
    assert delivered_risk_pct > 0.5
    # ...and comfortably above the $500 minimum order.
    assert EQUITY * d.allocation_pct / 100 > 500


def test_the_survival_ceiling_and_the_net_exposure_cap_do_not_collide():
    """The two caps bound different things and must not be confused.

    `max_position_pct` (65) bounds ONE name. `max_total_position_pct` (200
    since 2026-09-02, equal to `max_gross_exposure_x: 2.0` and non-binding
    by construction) bounds the book's NET exposure. Three names each at
    the single-name ceiling is 195% of equity — still a legal book under
    the net cap, but only just, so the single-name ceiling is genuinely
    the binding constraint on concentration and the net cap is not
    quietly doing its job for it.

    The engine check is the one that matters: it is a HARD BLOCK, so an
    order a basis point over is dropped, not trimmed.
    """
    from src.risk.rules import RiskRuleEngine
    from src.config import RiskConfig
    from src.models import TradeDecision

    engine = RiskRuleEngine(
        RiskConfig(
            max_position_pct=65,
            max_total_position_pct=200,
            require_stop_loss=True,
        )
    )

    def check(pct):
        return [
            v.rule
            for v in engine.check(
                TradeDecision(
                    action="BUY",
                    symbol="NVDA",
                    allocation_pct=pct,
                    entry_price=100.0,
                    stop_loss=95.0,
                    take_profit=140.0,
                    reasoning="test",
                ),
                [],
                EQUITY,
                0.0,
                cash=EQUITY * 3,
            )
        ]

    assert "max_position_pct" not in check(65.0)
    assert "max_position_pct" in check(66.0)
    # The net cap is close, but still not binding, at three full-size names.
    assert 3 * 65.0 < 200


def test_the_survival_ceiling_accounts_for_what_is_already_held():
    """The engine caps the POSITION, not the order, so the clamp must too —
    at the deployed number, not only at the tighter mechanism fixtures used
    above."""
    constructor = PortfolioConstructor()
    decisions = constructor.construct_orders(
        targets=[_risk_target("NVDA", 5.0)],
        positions=[_pos("NVDA", qty=250, avg_entry=100, current_price=100)],  # 25%
        analyses=[_analysis("NVDA", entry=100, stop=95, target=140)],
        total_value=EQUITY,
        price_map={"NVDA": 100.0},
    )
    assert len(decisions) == 1
    # 65% ceiling - 25% held = 40% of headroom, and not a basis point more.
    assert decisions[0].allocation_pct == 40.0


def test_the_ceiling_still_binds_a_genuinely_too_tight_stop():
    """The backstop still does its job. A stop tighter than the desk's real
    ATR-floor-governed range (only reachable via the level-backed exception
    down to `absolute_min_stop_atr_multiple`) is clamped far short of what
    conviction asked for — and since 2026-09-11 it is the single-name
    SURVIVAL ceiling that does the clamping, at 65, rather than the
    sector's unrelated 90% absolute cap standing in for it."""
    constructor = PortfolioConstructor()
    decisions = constructor.construct_orders(
        targets=[_risk_target("NVDA", 5.0)],
        positions=[],
        analyses=[_analysis("NVDA", entry=100, stop=97.5, target=140)],  # 2.5% stop
        total_value=EQUITY,
        price_map={"NVDA": 100.0},
    )
    assert len(decisions) == 1
    # 5% risk / 2.5% stop asks for 200% — clamped to 65, so delivered risk
    # is 1.625% (65% x 2.5%), under the 5% requested but still above the
    # 0.5% min_position_risk_pct floor.
    assert decisions[0].allocation_pct == 65.0


# --------------------------------------------------------------------------
# Stop width — the root cause behind both the sizing squeeze and noise exits
# --------------------------------------------------------------------------


def _vol_analysis(symbol, entry, stop, target, atr, setup="range", horizon=60, computed=None, touches=None, bars=None):
    """A widening fixture: the stop is NOT at a computed structural level.

    `computed_levels` defaults to the target alone, and that default is
    load-bearing. Since 2026-09-01 the take-profit is derived from these
    rather than from `reference_target`, and the reward:risk check inside
    `_widen_stop_past_noise` measures against the DERIVED number — so the
    fixture has to supply the structure the derivation reads. But since
    spec §12.1 the same field also decides whether the ATR band applies at
    all: a stop sitting at a computed level is honoured instead of widened.
    Listing the stop here would therefore silently turn every widening test
    below into a level-backed test. The stop is the ANALYST's number, with
    nothing computed under it, which is precisely the case the band exists
    for. `computed` is available for the level-backed tests, which say so
    in their own names.

    `touches` (2026-09-03, Phase 12.1) is the per-price touch count
    `_level_backing_stop` now requires before honouring a tight stop — see
    `ConstructorConfig.min_level_touches_for_stop_honor` and
    docs/RESEARCH_FINDINGS.md §7. Every price in `computed` defaults to 5
    touches (the derived bar) so existing "honoured" fixtures keep meaning
    "this level is verified enough" unless a test overrides it to exercise
    the gate itself.
    """
    from src.models import TechReasoningChain

    levels = list([target] if computed is None else computed)
    # 2026-09-12, "no floor, no trade": the constructor now refuses a long
    # with NO computed level below its entry. These fixtures deliberately
    # keep the stop unbacked, so the floor is a DISTANT shelf at half the
    # entry — far outside any level's own match zone here, so
    # it satisfies the floor requirement without backing the stop, and the
    # widening tests still test widening.
    if not any(0 < float(p) < float(entry) for p in levels):
        levels.append(round(float(entry) * 0.5, 2))
    default_touches = {price: 5 for price in levels}
    # Item 215 (2026-09-30): a level backs a stop only when the stop rests
    # inside the traded range of a bar that DREW the level, not merely inside
    # the level's zone. These fixtures carry no bar history, so each level
    # states the narrowest bar that can have drawn it — its own price. A test
    # that lists the stop in `computed` therefore still means "this stop is on
    # the level"; one that does not still means "nothing is under this stop".
    default_bars = {price: [(price, price)] for price in levels}
    if bars:
        default_bars.update(bars)
    return TechAnalysisResult(
        symbol=symbol,
        rating="buy",
        entry_price=entry,
        stop_loss=stop,
        reference_target=target,
        reasoning="test",
        support_levels=[stop],
        resistance_levels=[target],
        setup_type=setup,
        expected_horizon_sessions=horizon,
        atr_14=atr,
        computed_levels=levels,
        computed_level_touches=default_touches if touches is None else touches,
        computed_level_bars=default_bars,
        reasoning_chain=TechReasoningChain(trend="x", momentum="x", volatility="x", volume="x", support_resistance="x"),
        thesis_invalid_if="closes below support",
    )


def test_a_stop_inside_the_noise_band_is_pushed_out():
    """Measured 2026-08-27: stops sat a median 4.3% below entry against a
    median ATR of 2.56% of price — about 1.7 ATRs. That is one ordinary day's
    range, not a thesis invalidation, and it is what both fired exits inside
    noise and forced huge positions to reach any real risk."""
    constructor = PortfolioConstructor()
    decisions = constructor.construct_orders(
        targets=[_risk_target("MSFT", 1.0)],
        positions=[],
        analyses=[_vol_analysis("MSFT", 100.0, 97.6, 160.0, atr=2.35)],
        total_value=EQUITY,
        price_map={"MSFT": 100.0},
    )
    assert len(decisions) == 1
    # Every unbacked stop is 2.5 ATRs (owner ruling 2026-10-04; the 0.90
    # range scaler was removed 2026-10-09), so 2.5 x 2.35 = 5.875 below
    # entry, replacing the 2.4% structural stop.
    # (Was 0.90 x 1.5 = 1.35 ATRs = 3.1725 -> $96.83 between 2026-09-04 and
    # 2026-09-10, and 1.15 x 3.0 = 3.45 ATRs = 8.11 before that.)
    assert abs(decisions[0].stop_loss - 94.12) < 0.01


def test_a_stop_already_outside_the_noise_band_is_left_alone():
    """This only corrects stops placed inside the noise. It never pulls a
    wide structural stop tighter — structure still decides."""
    constructor = PortfolioConstructor()
    decisions = constructor.construct_orders(
        targets=[_risk_target("OKLO", 1.0)],
        positions=[],
        analyses=[_vol_analysis("OKLO", 100.0, 60.0, 200.0, atr=8.22)],
        total_value=EQUITY,
        price_map={"OKLO": 100.0},
    )
    assert decisions[0].stop_loss == 60.0


def test_widening_a_stop_into_a_sub_parity_payoff_still_ships():
    """**Inverted 2026-09-11, docs/WORK.md item 1(d).** The target still does
    not move when the stop does, so the reward:risk still falls — and it is
    still computed and logged. What changed is that a range trade is no
    longer REFUSED for it: the real ratio is a ranking signal, not a
    universal cutoff, and the PM gate has already capped a sub-floor range
    target at starter size. The stop is unchanged — item 1(d) touched no
    risk-side rule."""
    constructor = PortfolioConstructor()
    decisions = constructor.construct_orders(
        targets=[_risk_target("MSFT", 1.0)],
        positions=[],
        # Target only 4% up against the 2.25-ATR band ($5.2875 of risk):
        # 4.00 / 5.2875 = 0.7565, well under the 1.5 floor.
        # (Was 105.0 while the band was 3.45 ATR / $8.11 wide, then 104.0 at
        # the 1.35-ATR band where 4.00 / 3.1725 = 1.26. A 2.25-ATR band is
        # wider still, so 104.0 fails by an even larger margin and the
        # fixture does not need to move again.)
        analyses=[_vol_analysis("MSFT", 100.0, 97.6, 104.0, atr=2.35)],
        total_value=EQUITY,
        price_map={"MSFT": 100.0},
    )
    # 2026-10-09 (owner mandate, docs/OUTCOME.md): the 2026-10-01 parity
    # refusal is deleted. 4.00 of reward against 5.2875 of risk is 0.76 and
    # the trade SHIPS: the reward side is a forecast level the desk never
    # sells at, and the widened stop plus the risk caps bound the loss.
    assert len(decisions) == 1
    assert "MSFT" not in constructor.last_refusals
    assert decisions[0].stop_loss < 97.6


def test_no_volatility_reading_derives_structural_stop_and_holds():
    """Board item 80 (2026-09-25, REWORKED per owner ruling). With no ATR the
    stop is READ from price structure and the position HELD, not refused: a
    missing volatility reading is never a reason to skip protection. The first
    item-80 pass refused this input; the ruling overruled it ("there are always
    levels"). The unverifiable typed 97.6 does NOT set the stop — the nearest
    VERIFIED computed level below entry does, one buffer below it."""
    constructor = PortfolioConstructor()
    # A close, verified structural level below entry (the fixture leaves the
    # typed stop unbacked; `computed` supplies the level the fallback reads).
    analysis = _vol_analysis(
        "MSFT",
        100.0,
        97.6,
        160.0,
        atr=None,
        computed=[97.0],
    )
    buffer = constructor.cfg.structural_stop_buffer_pct
    stop = constructor._widen_stop_past_noise(
        "MSFT",
        analysis,
        100.0,
        97.6,
        target_price=160.0,
    )
    assert stop is not None, "a missing ATR must not skip protection"
    assert abs(stop - 97.0 * (1.0 - buffer)) < 1e-9
    assert constructor.last_refusals == {}


def test_no_volatility_reading_refuses_the_trade_outright():
    """...but the ORDER is refused, because the target is no longer the
    analyst's guess (2026-09-01). Without an ATR there is no noise floor and
    no reachable distance, so there is nothing to derive a target from — and
    a fabricated target is exactly the defect this replaced. Fail closed,
    with a named reason, rather than trade on half a chart."""
    constructor = PortfolioConstructor()
    decisions = constructor.construct_orders(
        targets=[_risk_target("MSFT", 1.0)],
        positions=[],
        analyses=[_vol_analysis("MSFT", 100.0, 97.6, 160.0, atr=None)],
        total_value=EQUITY,
        price_map={"MSFT": 100.0},
    )
    assert decisions == []


def test_wider_stops_give_conviction_room_to_change_the_size():
    """The payoff. With stops outside the noise, conviction moves the size
    again instead of every idea clamping to the same 20% position."""
    constructor = PortfolioConstructor()

    def alloc(risk):
        d = constructor.construct_orders(
            targets=[_risk_target("MSFT", risk)],
            positions=[],
            analyses=[_vol_analysis("MSFT", 100.0, 97.6, 160.0, atr=2.35)],
            total_value=EQUITY,
            price_map={"MSFT": 100.0},
        )
        return d[0].allocation_pct

    assert alloc(0.5) < alloc(1.0) < alloc(1.5)


# --------------------------------------------------------------------------
# §12.1 — a stop at a VERIFIED structural level is honoured, however tight
# --------------------------------------------------------------------------
#
# The defect: `min_stop_atr_multiple` OVERWROTE the level-derived stop
# whenever the level sat closer to entry than the band. The stop then pointed
# at nothing real, and `min_reward_risk_after_widening` was judged against
# that fabricated number. Over a ~15-session hold a stock travels ~3.9 ATR,
# so against a 3.0-ATR stop the best achievable ratio is ~1.29 against a 1.5
# floor — essentially no trade could pass. On 2026-09-01 the desk reviewed 38
# qualified signals and placed ZERO trades.
#
# "Verified" means the price came out of `find_structural_levels` and was
# attached to the analysis IN PYTHON (`computed_levels`). A level the MODEL
# asserts earns nothing — otherwise a model could buy itself an exemption
# from the noise floor by naming a number beside its stop.
#
# --- GEOMETRY REWORKED 2026-09-10, when the base floor went 1.5 -> 2.5 -----
#
# (It was reworked once before, on 2026-09-04, when the base went 3.0 -> 1.5.
# Both reworks were done the same way: every number below re-derived by hand
# from the new settings, never search-and-replaced and never copied out of a
# pytest failure message.)
#
# Shared geometry: entry $100.00, ATR $2.35, a "range" setup, so the multiple
# is 2.5 base x 0.90 (range) = 2.25 ATRs and:
#
#   band distance   2.25 x 2.35 = $5.2875  ->  band edge  $94.7125 -> $94.71
#   absolute floor  1.00 x 2.35 = $2.35    ->  hard floor $97.65   (unchanged)
#   match tolerance 0.25 x 2.35 = $0.5875                          (unchanged)
#
# THE TIGHT-STOP FIXTURE MOVED AGAIN, for the same structural reason it moved
# in 2026-09-04's rework, only in the opposite direction. The exemption
# window — the band of stop distances where being level-backed is the ONLY
# thing that decides the outcome — is [1.00, 2.25] ATRs now, i.e. $94.7125 to
# $97.65 in price terms. It was [1.00, 1.35] ATRs ($96.83 to $97.65) while the
# base was 1.5, and [1.00, 3.45] before that. So the window did not move away
# from the old $97.00 fixture; it grew, and $97.00 (1.28 ATRs) is still inside
# it. The fixture moves anyway, and the reason is the reward:risk straddle
# below rather than the band membership:
#
#   $97.00 is $3.00 of risk; the band stop is $5.2875 of risk. To make the
#   unbacked twin FAIL the 1.5 floor the reward must be under 1.5 x 5.2875 =
#   $7.93125, and to make the level-backed twin PASS it must be at least
#   1.5 x 3.00 = $4.50. That interval is $4.50–$7.93 wide, so the pair would
#   still straddle — but at a reward of, say, $4.60 the unbacked case scores
#   0.8700 and the level-backed case 1.5333, which is no longer a straddle of
#   the floor, it is one number nowhere near it. The test's whole point is
#   that the ONLY difference between the two is which stop was divided by, so
#   both ratios have to sit close to the floor on either side of it.
#
# The fixture is therefore $95.00 — $5.00 out, 5.00 / 2.35 = 2.1277 ATRs,
# inside the 2.25 band and well outside the 1.00 hard floor — with the
# derived target at $107.85 ($7.85 of reward). Worked by hand:
#
#   level-backed:  reward 7.85 / risk 5.0000  = 1.5700  -> clears 1.5, ships
#   unbacked:      reward 7.85 / risk 5.2875  = 1.4846  -> under 1.5, refused
#
#   (7.85 / 5.0000 = 1.57 exactly. 7.85 / 5.2875: 5.2875 x 1.48 = 7.8255 and
#    5.2875 x 1.4846 = 7.849714, so the quotient is 1.484633... -> 1.4846.)
#
# Those two straddle the floor deliberately — 0.07 above it and 0.015 below —
# exactly as the old $97.00/$104.60 pair straddled it at 1.5333/1.4500. The
# ONLY difference between them is which stop the division was done on, which
# is the whole of §12.1.

_ENTRY = 100.0
_ATR = 2.35
_BAND_EDGE = 94.12  # 2.5 x ATR below entry — the unconditional stop (2.25 until 2026-10-09)
_HARD_FLOOR = 97.65  # 1.00 x ATR below entry — the deterministic floor
_TIGHT_STOP = 95.00  # 2.13 x ATR out — inside the band, outside the floor
_UPPER_LEVEL = 107.85  # computed resistance; becomes the derived target


def test_a_level_backed_tight_stop_is_honoured_not_widened():
    """The whole point of §12.1. A stop 2.13 ATRs out sits inside the 2.25
    ATR band. Because a COMPUTED support level sits under it, it survives.

    Worked by hand: risk 100.00 - 95.00 = 5.00; reward 107.85 - 100.00 =
    7.85; R/R 1.5700, which clears the 1.5 floor. The unbacked twin of this
    fixture below is refused at 1.4846 on the same reward."""
    constructor = PortfolioConstructor()
    decisions = constructor.construct_orders(
        targets=[_risk_target("MSFT", 1.0)],
        positions=[],
        analyses=[
            _vol_analysis(
                "MSFT",
                _ENTRY,
                _TIGHT_STOP,
                _UPPER_LEVEL,
                atr=_ATR,
                computed=[_TIGHT_STOP, _UPPER_LEVEL],
            )
        ],
        total_value=EQUITY,
        price_map={"MSFT": _ENTRY},
    )
    assert len(decisions) == 1
    assert decisions[0].stop_loss == _TIGHT_STOP
    assert round((_UPPER_LEVEL - _ENTRY) / (_ENTRY - _TIGHT_STOP), 4) == 1.5700
    # And emphatically NOT the band edge, which is what shipped before.
    assert decisions[0].stop_loss != _BAND_EDGE


def test_an_unbacked_tight_stop_is_still_widened_to_the_band():
    """The other half, and the part that must not regress. Identical trade,
    except nothing computed sits under the $95.00 stop — the analyst simply
    placed it there. The band applies exactly as it always did, and here it
    is expensive: risk 5.2875 against reward 7.85 is R/R 1.4846. The
    level-backed twin above ships at 1.5700 on the same reward.

    **Assertion changed 2026-09-11 (item 1(d)).** Until then 1.4846 was
    fatal — the trade was refused. It is not any more; the ratio is a
    ranking signal, not a cutoff. What this test exists for is the STOP, and
    that is unchanged: an unbacked tight stop is still pushed out to the
    band."""
    constructor = PortfolioConstructor()
    decisions = constructor.construct_orders(
        targets=[_risk_target("MSFT", 1.0)],
        positions=[],
        analyses=[
            _vol_analysis(
                "MSFT",
                _ENTRY,
                _TIGHT_STOP,
                _UPPER_LEVEL,
                atr=_ATR,
                computed=[_UPPER_LEVEL],  # the stop is NOT a computed level
            )
        ],
        total_value=EQUITY,
        price_map={"MSFT": _ENTRY},
    )
    assert len(decisions) == 1
    assert decisions[0].stop_loss == _BAND_EDGE
    assert decisions[0].reward_risk == 1.34  # 7.85 / 5.875 at the 2.5-ATR band (1.48 at 2.25 until 2026-10-09)


def test_reward_risk_is_measured_against_the_stop_that_will_actually_ship():
    """The pair above IS the fix, stated as arithmetic.

    Same entry, same stop, same computed target $107.85. Against the
    fabricated band stop the ratio is 7.85 / 5.2875 = 1.4846; against the
    stop the desk will actually place it is 7.85 / 5.00 = 1.5700. Nothing
    about the trade changed — only which stop the division was performed on,
    which is the defect §12.1 removes.

    **2026-09-11 (item 1(d)):** both now SHIP — the difference the ratio
    makes is to the ranking, not to a refusal — so the assertion below is on
    the stop each path produces rather than on a None."""
    constructor = PortfolioConstructor()

    def stop_for(computed):
        return constructor._widen_stop_past_noise(
            "MSFT",
            _vol_analysis("MSFT", _ENTRY, _TIGHT_STOP, _UPPER_LEVEL, atr=_ATR, computed=computed),
            entry_price=_ENTRY,
            stop_loss=_TIGHT_STOP,
            target_price=_UPPER_LEVEL,
        )

    honoured = stop_for([_TIGHT_STOP, _UPPER_LEVEL])
    assert honoured == _TIGHT_STOP
    assert round((_UPPER_LEVEL - _ENTRY) / (_ENTRY - honoured), 4) == 1.5700

    # Unbacked: widened to the band edge. The ratio against that widened
    # stop is under 1.5 and is no longer a refusal (item 1(d)).
    assert stop_for([_UPPER_LEVEL]) == round(_ENTRY - 2.5 * _ATR, 4)
    # 2.25 x 2.35 = 5.2875 is the exact band distance the refusal used; the
    # $94.71 constant above is that same edge rounded to a shippable price.
    assert round((_UPPER_LEVEL - _ENTRY) / (2.5 * _ATR), 4) == 1.3362
    assert round(_ENTRY - 2.5 * _ATR, 2) == _BAND_EDGE


def test_a_level_backed_stop_inside_one_atr_is_floored_at_one_atr_not_the_band():
    """Beyond the literal §12.1 wording, and deliberately so.

    §12.1 argues the exemption is safe because `config/prompts/tech_analyst.md`
    forbids a stop inside 1*ATR. That is a PROMPT; Invariant 2 requires the
    deterministic layer to be the final authority and to fail closed. A real
    support level $1.00 under entry is genuine structure AND a guaranteed
    whipsaw. So it is pushed out to exactly 1x ATR — NOT to the 2.25x band,
    which is the behaviour §12.1 removed. (The gap between the two widened
    again when the base floor became 2.5, but the rule is unchanged: the
    floor is the destination, never the band.)"""
    constructor = PortfolioConstructor()
    decisions = constructor.construct_orders(
        targets=[_risk_target("MSFT", 1.0)],
        positions=[],
        analyses=[
            _vol_analysis(
                "MSFT",
                _ENTRY,
                99.0,
                _UPPER_LEVEL,
                atr=_ATR,
                computed=[99.0, _UPPER_LEVEL],
            )
        ],
        total_value=EQUITY,
        price_map={"MSFT": _ENTRY},
    )
    assert len(decisions) == 1
    assert decisions[0].stop_loss == _HARD_FLOOR
    assert decisions[0].stop_loss != _BAND_EDGE
    # The floor is configurable and is the only thing that moved the stop.
    assert _HARD_FLOOR == round(_ENTRY - 1.0 * _ATR, 2)


def test_the_absolute_floor_is_configurable_and_can_be_switched_off():
    """It is an owner-reversible addition, so it must be reachable from
    config rather than baked in. At 0 the ratified §12.1 wording applies
    literally: the level-backed stop is honoured however tight."""
    constructor = PortfolioConstructor(
        ConstructorConfig(
            absolute_min_stop_atr_multiple=0.0,
        )
    )
    assert (
        constructor._widen_stop_past_noise(
            "MSFT",
            _vol_analysis("MSFT", _ENTRY, 99.0, _UPPER_LEVEL, atr=_ATR, computed=[99.0, _UPPER_LEVEL]),
            entry_price=_ENTRY,
            stop_loss=99.0,
            target_price=_UPPER_LEVEL,
        )
        == 99.0
    )


def test_a_near_miss_outside_the_tolerance_is_not_level_backed():
    """The boundary is a boundary, not a suggestion. Since item 215
    (2026-09-30) it is the traded range of a BAR that drew the level, not
    the level's zone: the session that turned at $95.00 traded up to
    $96.00, so $95.90 is sitting on that bar and $96.10 is not, and the
    second one gets the band like any unbacked stop. The two stops and
    every other number in this fixture are unchanged — only what makes
    $95.90 count has changed, from "within 1% of the level" to "inside a
    session the market actually defended".

    Both candidate stops are deliberately INSIDE the 2.25 ATR band ($94.71)
    and outside the 1.00 ATR hard floor ($97.65) — 95.90 is 4.10 / 2.35 =
    1.74 ATRs out, 96.10 is 3.90 / 2.35 = 1.66 — so backing is the only
    thing that can decide either one. A stop outside the band is left alone
    for reasons that have nothing to do with the tolerance, and would make
    this assert nothing.

    (Was $95.10 / $95.60 either side of a 0.25 x ATR = $0.5875 tolerance.
    That tolerance was DELETED on 2026-09-13 — docs/WORK.md item 46: an ATR
    multiple cannot stay consistent with the percentage-of-price zone it
    claimed to cover, and at this fixture's own 2.35% ATR it was 0.62x as
    wide as the zone. The pair moved to straddle the real boundary; the
    fixture level, band and floor are unchanged. `$95.60` now correctly
    counts as level-backed, which is the behaviour change item 46 shipped.)
    """
    constructor = PortfolioConstructor()

    def stop_for(stop):
        return constructor._widen_stop_past_noise(
            "MSFT",
            _vol_analysis(
                "MSFT",
                _ENTRY,
                stop,
                130.0,
                atr=_ATR,
                computed=[_TIGHT_STOP, 130.0],
                bars={_TIGHT_STOP: [(_TIGHT_STOP, 96.00)]},
            ),
            entry_price=_ENTRY,
            stop_loss=stop,
            target_price=130.0,
        )

    assert stop_for(95.9) == 95.9  # gap $0.90, inside
    assert round(stop_for(96.1), 2) == _BAND_EDGE  # gap $1.10, outside


def test_a_level_the_model_asserted_does_not_earn_the_exemption():
    """The verification would be worthless if the model could write to it.

    Here `support_levels` — which the LLM emits — names $95.00 exactly, and
    the stop sits on it. `computed_levels`, which only Python writes, does
    not. The band applies, because the analyst asserting a level is not the
    system having computed one."""
    constructor = PortfolioConstructor()
    analysis = _vol_analysis("MSFT", _ENTRY, _TIGHT_STOP, 130.0, atr=_ATR, computed=[130.0])
    assert analysis.support_levels == [_TIGHT_STOP]  # the model said so
    assert _TIGHT_STOP not in analysis.computed_levels  # the chart did not
    assert (
        round(
            constructor._widen_stop_past_noise(
                "MSFT",
                analysis,
                entry_price=_ENTRY,
                stop_loss=_TIGHT_STOP,
                target_price=130.0,
            ),
            2,
        )
        == _BAND_EDGE
    )


def test_a_level_below_the_touch_bar_does_not_earn_the_exemption():
    """Phase 12.1, 2026-09-03. `min_level_touches_for_stop_honor` (5, derived
    in docs/RESEARCH_FINDINGS.md §7 from the measured touch-count table) is
    a SEPARATE gate from `find_structural_levels`' own `MIN_TOUCHES` (2):
    the level is real enough to appear in `computed_levels` and to be a
    legal target, but not yet trusted enough for a tight-stop exemption.
    Identical fixture to the flagship honoured case, except the level's
    touch count sits below the bar — the stop is widened to the band exactly
    as an unbacked stop would be."""
    constructor = PortfolioConstructor()
    decisions = constructor.construct_orders(
        targets=[_risk_target("MSFT", 1.0)],
        positions=[],
        analyses=[
            _vol_analysis(
                "MSFT",
                _ENTRY,
                _TIGHT_STOP,
                _UPPER_LEVEL,
                atr=_ATR,
                computed=[_TIGHT_STOP, _UPPER_LEVEL],
                # stop's own level: 4 touches, one under the bar
                touches={_TIGHT_STOP: 4, _UPPER_LEVEL: 5},
            )
        ],
        total_value=EQUITY,
        price_map={"MSFT": _ENTRY},
    )
    # The assertion is the STOP, not a refusal (changed 2026-09-11, item
    # 1(d) — the sub-floor ratio no longer refuses anything). A stop widened
    # to the band edge instead of honoured at $95.00 is what proves the level
    # was NOT treated as backing it.
    assert len(decisions) == 1
    assert decisions[0].stop_loss == _BAND_EDGE


def test_a_level_exactly_at_the_touch_bar_earns_the_exemption():
    """The other edge of the same gate: 5 touches — the bar itself, not one
    more — is enough."""
    constructor = PortfolioConstructor()
    decisions = constructor.construct_orders(
        targets=[_risk_target("MSFT", 1.0)],
        positions=[],
        analyses=[
            _vol_analysis(
                "MSFT",
                _ENTRY,
                _TIGHT_STOP,
                _UPPER_LEVEL,
                atr=_ATR,
                computed=[_TIGHT_STOP, _UPPER_LEVEL],
                touches={_TIGHT_STOP: 5, _UPPER_LEVEL: 5},
            )
        ],
        total_value=EQUITY,
        price_map={"MSFT": _ENTRY},
    )
    assert len(decisions) == 1
    assert decisions[0].stop_loss == _TIGHT_STOP


def test_a_level_with_no_touch_count_on_record_fails_closed():
    """A level price present in `computed_levels` but absent from
    `computed_level_touches` (an older caller, a stale fixture, a partial
    write) must NOT be honoured by default — Invariant 2 requires the
    deterministic layer to fail closed, not to assume a level is good
    because its touch count is simply missing."""
    constructor = PortfolioConstructor()
    assert (
        constructor._level_backing_stop(
            _vol_analysis("MSFT", _ENTRY, 96.0, _UPPER_LEVEL, atr=_ATR, computed=[96.0, _UPPER_LEVEL], touches={}),
            _ENTRY,
            96.0,
            is_short=False,
        )
        is None
    )


def test_a_level_on_the_wrong_side_of_entry_cannot_back_a_stop():
    """Side discipline. A long is held up by structure at or BELOW its entry;
    a short is capped by structure at or ABOVE its entry. The same level
    cannot serve both, and the check is made against THIS ENTRY rather than
    against the last close — the same re-partition `derive_structural_target`
    performs, for the same reason."""
    constructor = PortfolioConstructor()
    analysis = _vol_analysis("MSFT", _ENTRY, _TIGHT_STOP, _UPPER_LEVEL, atr=_ATR, computed=[_TIGHT_STOP, _UPPER_LEVEL])
    assert (
        constructor._level_backing_stop(
            analysis,
            _ENTRY,
            _TIGHT_STOP,
            is_short=False,
        )
        == _TIGHT_STOP
    )
    # The identical level read as a SHORT's backing: it is below entry, so it
    # cannot be what a short's stop above entry is resting on.
    assert (
        constructor._level_backing_stop(
            analysis,
            _ENTRY,
            _TIGHT_STOP,
            is_short=True,
        )
        is None
    )


# --------------------------------------------------------------------------
# §12.1 — SLB, the 2026-09-01 refusal, reproduced
# --------------------------------------------------------------------------


class TestSLBStopIsHonoured:
    """SLB, 2026-09-01 morning run: `strong_buy` / `high` conviction, entry
    $60.10, and a shipped stop of $55.50 — 7.7% — that scored reward:risk
    1.28 against a geometric maximum of ~1.29, so it was refused before
    anyone judged the trade.

    **The level data here is SYNTHETIC**, exactly as
    `tests/test_target_derivation.py::TestSLB` states for its own fixtures.
    The production bars for that session are not in this repository. These
    assert what the RULE does with a plausible chart, not what SLB's actual
    chart contained, and no claim is made about a live outcome.

    The arithmetic is pinned to that other reproduction so the two agree: the
    shipped $55.50 stop is exactly 3.45 ATRs out (the 3.0 base x 1.15 for a
    range setup), which back-solves ATR = $1.3333. The analyst's own stop is
    placed at 1.7 ATRs — the median this book's stops actually sat at,
    measured 2026-08-27 and quoted in §12.1.
    """

    ENTRY = 60.10
    ATR = 4.60 / 3.45  # ~$1.3333, back-solved from the 7.7% stop
    BAND_STOP = 55.50  # 3.45 x ATR — what the old rule shipped
    LEVEL_STOP = 57.83  # 1.70 x ATR — at a computed support shelf
    SHELF = 65.99  # computed resistance; the derived target
    HORIZON = 15  # the ~15-session hold §12.1 reasons about
    FLOOR = 1.5

    def _analysis(self, computed):
        return _vol_analysis(
            "SLB",
            self.ENTRY,
            self.LEVEL_STOP,
            self.SHELF,
            atr=self.ATR,
            horizon=self.HORIZON,
            computed=computed,
        )

    def test_the_band_stop_reproduces_the_1_28_that_was_refused(self):
        """Anchor the reproduction against the number the run produced."""
        rr = (self.SHELF - self.ENTRY) / (self.ENTRY - self.BAND_STOP)
        assert round(rr, 2) == 1.28
        assert rr < self.FLOOR
        # And the band stop is the 3.45 x ATR edge, not a chosen number.
        assert round(self.ENTRY - 3.45 * self.ATR, 2) == self.BAND_STOP

    # The 2.25-ATR band the floor produces since 2026-09-10, on this same
    # fixture: 2.25 x 1.33333 = $3.00 exactly, so the band edge is
    # 60.10 - 3.00 = $57.10 — OUTSIDE the analyst's own $57.83 stop, so an
    # unbacked $57.83 is widened to it. (It was $58.30 at the 1.35-ATR band
    # between 2026-09-04 and 2026-09-10, which sat INSIDE $57.83 and left the
    # analyst's stop alone; see the test below for what changed.)
    NEW_BAND_STOP = 56.77  # 2.5 x ATR (57.10 at 2.25 until 2026-10-09)

    def test_the_new_floor_widens_slbs_stop_but_the_trade_still_ships(self):
        """The refusal this class was written to reproduce still does not
        happen — but at a 2.5 base it is no longer because the stop is left
        alone. RENAMED AND REWORKED 2026-09-10, and the rename is the honest
        part: between 2026-09-04 and 2026-09-10 this test was called
        `..._leaves_slbs_own_stop_alone_without_any_level` and asserted
        exactly that, which a 2.25-ATR band makes false.

        The history, in one place:
          * base 3.0 (range scaler 1.15) — band 3.45 ATRs. The analyst's
            $57.83 stop was overwritten with a fabricated $55.50 and the
            trade died at R/R 1.28. That is the 2026-09-01 refusal.
          * base 1.5 (scaler 0.90) — band 1.35 ATRs = $1.80, edge $58.30.
            The analyst's stop at 1.70 ATRs was OUTSIDE the band, untouched,
            and shipped at its own geometry, R/R 2.59.
          * base 2.5 (scaler 0.90) — band 2.25 ATRs, i.e. this test. The
            analyst's stop is inside the band again and IS widened. What
            has changed since the 3.0 base is that the widened stop is no
            longer fatal: the trade still clears the reward:risk floor.

        Worked by hand:
          band distance   2.25 x 1.33333 = $3.00   -> band edge $57.10
          analyst's stop  60.10 - 57.83  = $2.27   = 1.70 ATR, tighter,
                                                     so it is widened
          shipped risk    60.10 - 57.10  = $3.00
          reward:risk     (65.99 - 60.10) / 3.00   = 5.89 / 3.00 = 1.9633
                                                   -> 1.96, clears 1.5
          (against the old 3.45-ATR band the same reward over $4.60 of risk
           was 1.28, which is the refusal.)

        So SLB depends on §12.1's level-backed exemption again to keep its
        own stop — the test below is what proves the exemption delivers it.
        """
        constructor = PortfolioConstructor()
        assert round(self.ENTRY - 2.5 * self.ATR, 2) == self.NEW_BAND_STOP
        assert self.LEVEL_STOP > self.NEW_BAND_STOP  # inside the band
        decisions = constructor.construct_orders(
            targets=[_risk_target("SLB", 1.0)],
            positions=[],
            analyses=[self._analysis([self.SHELF])],
            total_value=EQUITY,
            price_map={"SLB": self.ENTRY},
        )
        assert len(decisions) == 1
        assert decisions[0].stop_loss == self.NEW_BAND_STOP
        assert decisions[0].stop_loss != self.LEVEL_STOP
        assert round(decisions[0].reward_risk, 2) == 1.77  # at the 2.5-ATR band (1.96 at 2.25 until 2026-10-09)

    def test_a_level_backed_slb_stop_is_honoured_and_the_trade_passes(self):
        """The same trade with a computed support shelf under the stop. The
        stop is honoured at 1.7 ATRs, reward:risk is measured against it, and
        SLB becomes tradeable — the outcome §12.1 exists to produce."""
        constructor = PortfolioConstructor()
        decisions = constructor.construct_orders(
            targets=[_risk_target("SLB", 1.0)],
            positions=[],
            analyses=[self._analysis([self.LEVEL_STOP, self.SHELF])],
            total_value=EQUITY,
            price_map={"SLB": self.ENTRY},
        )
        assert len(decisions) == 1
        assert decisions[0].stop_loss == self.LEVEL_STOP
        rr = (self.SHELF - self.ENTRY) / (self.ENTRY - self.LEVEL_STOP)
        assert rr >= self.FLOOR
        # Recorded so the number is visible when the test is read, not only
        # when it fails: 1.28 refused, 2.59 taken, same chart.
        assert round(rr, 2) == 2.59

    def test_the_floor_does_not_move_to_accommodate_slb(self):
        """**Inverted 2026-09-11, docs/WORK.md item 1(d).** This used to say
        "1.5 still binds": if the computed shelf was nearer, the honoured
        stop did not rescue the trade. Nothing binds on that number any more
        for a range setup — the honest 1.06 is computed, logged, capped at
        starter size by the PM gate, and ranked below better payoffs, but it
        is not refused. §12.1's own point survives intact: the stop is
        honoured at the computed level, and the division is performed on the
        stop that ships."""
        constructor = PortfolioConstructor()
        near_shelf = 62.50  # reward $2.40 against risk $2.27 = 1.06
        decisions = constructor.construct_orders(
            targets=[_risk_target("SLB", 1.0)],
            positions=[],
            analyses=[
                _vol_analysis(
                    "SLB",
                    self.ENTRY,
                    self.LEVEL_STOP,
                    near_shelf,
                    atr=self.ATR,
                    horizon=self.HORIZON,
                    computed=[self.LEVEL_STOP, near_shelf],
                )
            ],
            total_value=EQUITY,
            price_map={"SLB": self.ENTRY},
        )
        assert len(decisions) == 1
        assert decisions[0].stop_loss == self.LEVEL_STOP
        assert decisions[0].reward_risk == 1.06


# --------------------------------------------------------------------------
# One reward:risk gate, on the geometry that ships (2026-09-02)
#
# What these pin, and why they are not a threshold change: until 2026-09-02
# `min_reward_risk_after_widening` was evaluated ONLY inside the two widening
# branches, behind unnamed early returns for "already outside the noise band"
# and "no ATR reading". A stop that was wide enough to begin with reached the
# broker with no deterministic reward:risk check at all.
#
# Measured on the pre-reset production database (data/resets/20260902T181859Z),
# 2026-08-18 to 2026-09-02: 14 of 49 constructed entry orders shipped under the
# 1.5 floor, and the floor's refusal message appears zero times in the whole
# production log history before 2026-09-02. Two reached the broker; XLE on
# 2026-08-21 FILLED 9 shares at $64.26 on a reward:risk of 0.81.
# --------------------------------------------------------------------------

# XLE, 2026-09-01, run-64290730 — the real numbers off the evidence rows.
_XLE_TA_ENTRY = 63.96  # tech_analyst snapshot price
_XLE_LIVE_ENTRY = 64.51  # what the constructor actually priced
_XLE_STOP = 61.54  # IDENTICAL in both — no stop ever moved
_XLE_TARGET = 68.00
_XLE_ATR = 1.21


def test_xle_the_1_67_versus_1_18_divergence_is_entry_drift_not_stop_geometry():
    """The rejection that started this, reproduced from production evidence.

    The Risk Manager killed the whole 2026-09-01 plan saying *"PM's reasoning
    assumes R/R 1.67 but the executed order has R/R 1.18"*. Both numbers were
    right. The STOP never moved — $61.54 on the tech analyst's row and $61.54
    on the proposed order. What moved was the ENTRY, $63.96 to $64.51, because
    the analyst measures at its snapshot price and the constructor prices the
    live market. Same trade, same stop, two entries.

    So the divergence is not a stop-geometry defect, and widening was never
    involved. It is one ratio computed at two different entries — which is why
    the division now lives in exactly one function.
    """
    from src.models import reward_to_risk

    analyst = reward_to_risk(_XLE_TA_ENTRY, _XLE_STOP, _XLE_TARGET, is_short=False)
    order = reward_to_risk(_XLE_LIVE_ENTRY, _XLE_STOP, _XLE_TARGET, is_short=False)
    assert round(analyst, 2) == 1.67
    assert round(order, 2) == 1.18
    # The stop is the one thing that is NOT different between them.
    assert analyst != order


def test_xle_a_breakout_is_no_longer_measured_against_any_reward_risk_floor():
    """**Inverted 2026-09-11, docs/WORK.md item 1(d) — and XLE is the case
    that makes the point.**

    On the day, the constructor SHIPPED this order at 1.18 and an LLM stopped
    it. 2026-09-02 made the deterministic floor run on every path, and this
    test then asserted the floor refused it. It is a BREAKOUT: there is no
    overhead level to measure the $68.00 "reward" against, and the position
    would be managed by a trailing stop with no fixed target at all
    (`src/risk/trailing.py`). So the 1.18 was a ratio against a number the
    trade was never going to trade toward, and the owner's decision is that
    no such number may block or size a trend trade. The stop ships.

    The RISK side is untouched and still the reason this fixture is precise.
    Band arithmetic since 2026-10-09: every unbacked stop is 2.5 ATRs
    (owner ruling 2026-10-04, no regime scaler), so the band edge is
    64.51 - 2.5 x 1.21 = $61.485. The $2.97 stop (2.4545 ATRs) is inside
    it and is pushed out to the band; the trade still ships."""
    constructor = PortfolioConstructor()
    assert (
        abs(
            constructor._widen_stop_past_noise(
                "XLE",
                _vol_analysis("XLE", _XLE_LIVE_ENTRY, _XLE_STOP, _XLE_TARGET, atr=_XLE_ATR, setup="breakout"),
                entry_price=_XLE_LIVE_ENTRY,
                stop_loss=_XLE_STOP,
                regime="risk-on",
                direction="long",
                target_price=_XLE_TARGET,
            )
            - (_XLE_LIVE_ENTRY - 2.5 * _XLE_ATR)
        )
        < 1e-9
    )


def test_a_wide_stop_that_clears_the_floor_still_ships_untouched():
    """The gate is a floor, not a tax. The same already-outside-the-band path
    with geometry that works returns the structural stop unchanged — the fix
    refuses trades, it never moves a stop it did not previously move."""
    constructor = PortfolioConstructor()
    # Stop $61.40 (3.11 / 1.21 = 2.570 ATR out, outside the 2.5x band,
    # edge $61.485), target $73.00 → reward 8.49 / risk 3.11 = 2.73.
    wide_stop = 61.40
    assert (
        constructor._widen_stop_past_noise(
            "XLE",
            _vol_analysis("XLE", _XLE_LIVE_ENTRY, wide_stop, 73.0, atr=_XLE_ATR, setup="breakout"),
            entry_price=_XLE_LIVE_ENTRY,
            stop_loss=wide_stop,
            regime="risk-on",
            direction="long",
            target_price=73.0,
        )
        == wide_stop
    )


def test_a_level_backed_tight_stop_is_honoured_and_a_bare_one_is_not():
    """§12.1's rule, asserted as the contrast it actually is, on ONE fixture
    pair that differs only in whether Python computed the level.

    Both stops sit at $95.00, $5.00 (5.00 / 2.35 = 2.13 ATR) under a $100
    entry — inside the 2.25x ATR band ($5.2875, edge $94.71). The
    level-backed one ships at $95.00; the bare one is pushed to the band
    edge."""
    constructor = PortfolioConstructor()

    def stop_for(computed):
        return constructor._widen_stop_past_noise(
            "MSFT",
            _vol_analysis("MSFT", _ENTRY, _TIGHT_STOP, 130.0, atr=_ATR, computed=computed),
            entry_price=_ENTRY,
            stop_loss=_TIGHT_STOP,
            target_price=130.0,
        )

    assert stop_for([_TIGHT_STOP, 130.0]) == _TIGHT_STOP  # level backs it
    assert round(stop_for([130.0]), 2) == _BAND_EDGE  # nothing does


def test_both_reward_risk_computations_agree_on_one_trade():
    """The constructor's gate and `TradeDecision.reward_risk` must be the
    same arithmetic on the same geometry, or the Risk Manager is shown a
    ratio the floor did not judge. That gap is what the 1.67-vs-1.18
    rejection actually was."""
    from src.models import reward_to_risk

    constructor = PortfolioConstructor()
    analysis = _vol_analysis("MSFT", _ENTRY, _TIGHT_STOP, _UPPER_LEVEL, atr=_ATR, computed=[_TIGHT_STOP, _UPPER_LEVEL])
    decisions = constructor.construct_orders(
        targets=[_risk_target("MSFT", 1.0)],
        positions=[],
        analyses=[analysis],
        total_value=EQUITY,
        price_map={"MSFT": _ENTRY},
    )
    assert len(decisions) == 1
    d = decisions[0]
    gate = constructor._reward_risk_at(
        d.entry_price,
        d.stop_loss,
        d.take_profit,
        False,
    )
    assert d.reward_risk == round(gate, 2)
    assert d.reward_risk == round(reward_to_risk(d.entry_price, d.stop_loss, d.take_profit, is_short=False), 2)


def test_the_shipped_order_records_the_rule_that_placed_its_stop():
    """The execution stage must be able to tell a level-honoured stop from
    any other, without recomputing levels from different bars. It reads
    `TradeDecision.stop_rule`, which the constructor sets here."""
    from src.portfolio_constructor import (
        LEVEL_BACKED_STOP_RULES,
        STOP_RULE_LEVEL_HONOURED,
    )

    constructor = PortfolioConstructor()
    backed = constructor.construct_orders(
        targets=[_risk_target("MSFT", 1.0)],
        positions=[],
        analyses=[
            _vol_analysis("MSFT", _ENTRY, _TIGHT_STOP, _UPPER_LEVEL, atr=_ATR, computed=[_TIGHT_STOP, _UPPER_LEVEL])
        ],
        total_value=EQUITY,
        price_map={"MSFT": _ENTRY},
    )
    assert backed[0].stop_rule == STOP_RULE_LEVEL_HONOURED
    assert backed[0].stop_rule in LEVEL_BACKED_STOP_RULES

    # A stop nothing computed backs: widened to the band, and the band edge
    # is not a level, so no exemption is recorded and the execution-time ATR
    # floor still applies to it.
    bare = constructor.construct_orders(
        targets=[_risk_target("MSFT", 1.0)],
        positions=[],
        analyses=[_vol_analysis("MSFT", _ENTRY, _TIGHT_STOP, 130.0, atr=_ATR, computed=[130.0])],
        total_value=EQUITY,
        price_map={"MSFT": _ENTRY},
    )
    assert bare[0].stop_rule not in LEVEL_BACKED_STOP_RULES


# --------------------------------------------------------------------------
# Fail-closed: non-finite inputs must refuse, never permit
#
# The hazard is specific and has bitten this codebase before: `nan <= 0`,
# `nan >= entry` and `nan < floor` are ALL False, so a NaN passes every
# ordering check silently and comes out the far side looking valid.
# --------------------------------------------------------------------------


def test_a_non_finite_stop_is_refused_not_passed_through():
    """Before this, a NaN stop went through `_widen_stop_past_noise`
    untouched AND through the caller's `stop_loss >= entry_price` validity
    check, reaching `TradeDecision` intact."""
    constructor = PortfolioConstructor()
    nan = float("nan")
    for bad in (nan, float("inf"), float("-inf")):
        assert (
            constructor._widen_stop_past_noise(
                "MSFT",
                _vol_analysis("MSFT", _ENTRY, 96.0, _UPPER_LEVEL, atr=_ATR),
                entry_price=_ENTRY,
                stop_loss=bad,
                target_price=_UPPER_LEVEL,
            )
            is None
        )


def test_a_non_finite_entry_price_is_refused():
    constructor = PortfolioConstructor()
    assert (
        constructor._widen_stop_past_noise(
            "MSFT",
            _vol_analysis("MSFT", _ENTRY, 96.0, _UPPER_LEVEL, atr=_ATR),
            entry_price=float("nan"),
            stop_loss=96.0,
            target_price=_UPPER_LEVEL,
        )
        is None
    )


def test_a_non_finite_target_does_not_refuse_the_stop():
    """A NaN target used to look like it cleared the 1.5 floor (`nan < 1.5`
    is False). Invented floors are gone: unmeasurable payoff is recorded,
    not refused, and the risk-side stop still ships."""
    constructor = PortfolioConstructor()
    stop = constructor._widen_stop_past_noise(
        "MSFT",
        _vol_analysis("MSFT", _ENTRY, 96.0, _UPPER_LEVEL, atr=_ATR),
        entry_price=_ENTRY,
        stop_loss=96.0,
        target_price=float("nan"),
    )
    assert stop is not None
    assert stop < _ENTRY


def test_reward_to_risk_returns_none_for_every_malformed_geometry():
    """The one definition, and its fail-closed contract."""
    from src.models import reward_to_risk

    nan, inf = float("nan"), float("inf")
    assert reward_to_risk(100.0, 90.0, 130.0, is_short=False) == 3.0
    assert reward_to_risk(100.0, 110.0, 70.0, is_short=True) == 3.0
    # Non-finite, on any leg.
    assert reward_to_risk(nan, 90.0, 130.0, is_short=False) is None
    assert reward_to_risk(100.0, nan, 130.0, is_short=False) is None
    assert reward_to_risk(100.0, 90.0, nan, is_short=False) is None
    assert reward_to_risk(inf, 90.0, 130.0, is_short=False) is None
    # Missing, non-positive, or pointing the wrong way for the side.
    assert reward_to_risk(None, 90.0, 130.0, is_short=False) is None
    assert reward_to_risk(100.0, 0.0, 130.0, is_short=False) is None
    assert reward_to_risk(100.0, 110.0, 130.0, is_short=False) is None  # stop above a long
    assert reward_to_risk(100.0, 90.0, 90.0, is_short=False) is None  # target below entry
    assert reward_to_risk(100.0, 100.0, 130.0, is_short=False) is None  # zero risk


# --------------------------------------------------------------------------
# retired board item 49 — best-ranked first, and the durable reason a
# budget-rationed name now leaves behind. Owner decision 2026-09-12.
# --------------------------------------------------------------------------


def test_constructor_spends_the_budget_in_the_ranked_order_it_is_given():
    """Two identical ideas, one budget that fits only one of them. The
    `ranking` argument — the PM's own `rank_verdicts` order — decides which,
    and reversing it reverses the outcome. Nothing else differs."""

    def _run(ranking):
        return PortfolioConstructor().construct_orders(
            targets=[_risk_target("NVDA", 5.0), _risk_target("AMD", 5.0)],
            positions=[],
            analyses=[
                _analysis("NVDA", entry=100, stop=95, target=140),
                _analysis("AMD", entry=100, stop=95, target=140),
            ],
            total_value=EQUITY,
            price_map={"NVDA": 100.0, "AMD": 100.0},
            # 20 of the 25% ceiling already spent: room for one 5% idea.
            existing_risk_pct={"HELD": 20.0},
            clusters=[],
            ranking=ranking,
        )

    nvda_first = {d.symbol: d.allocated_risk_pct for d in _run(["NVDA", "AMD"])}
    amd_first = {d.symbol: d.allocated_risk_pct for d in _run(["AMD", "NVDA"])}
    assert abs(nvda_first["NVDA"] - 5.0) < 0.05
    assert abs(amd_first["AMD"] - 5.0) < 0.05
    # The loser takes the remainder, which here is nothing at all.
    assert "AMD" not in nvda_first
    assert "NVDA" not in amd_first


def test_a_budget_rationed_name_leaves_a_durable_machine_readable_reason():
    """Before item 49 this was the one constructor drop path with no
    per-symbol record: its log line did not match `_DropReasonCapture`'s
    pattern, so the symbol reached the database as a generic
    `constructor_dropped` with "no matching constructor log line captured".
    """
    from src.portfolio_constructor import STOP_REFUSAL_BUDGET_EXHAUSTED

    constructor = PortfolioConstructor()
    decisions = constructor.construct_orders(
        targets=[_risk_target("NVDA", 5.0)],
        positions=[],
        analyses=[_analysis("NVDA", entry=100, stop=95, target=140)],
        total_value=EQUITY,
        price_map={"NVDA": 100.0},
        existing_risk_pct={"HELD": 25.0},
        clusters=[],
    )
    assert decisions == []
    refusals = constructor.drain_refusals()
    assert refusals["NVDA"]["refusal"] == STOP_REFUSAL_BUDGET_EXHAUSTED
    assert "risk budget" in refusals["NVDA"]["detail"]
    # And the log-scrape fallback now matches it too, so neither record path
    # is silent on this symbol.
    assert "NVDA" in constructor.last_drop_reasons


def test_a_budget_refusal_drops_the_target_and_never_zeroes_it():
    """A 0% target is read downstream as "sell it". Refusing to OPEN a
    position must never emit an order for the symbol at all — the target is
    dropped, and a held position is left exactly where it is."""
    constructor = PortfolioConstructor()
    decisions = constructor.construct_orders(
        targets=[_risk_target("NVDA", 5.0)],
        positions=[],
        analyses=[_analysis("NVDA", entry=100, stop=95, target=140)],
        total_value=EQUITY,
        price_map={"NVDA": 100.0},
        existing_risk_pct={"HELD": 25.0},
        clusters=[],
    )
    assert [d for d in decisions if d.symbol == "NVDA"] == []


# --------------------------------------------------------------------------
# board item 10 (2026-09-14) — the rest of the constructor's drop paths,
# found the same way item 49's was: running `_DropReasonCapture._SYMBOL`
# statically against every message the module can log ahead of a `return
# None` / `continue` that produces no order. No live data was needed; the
# regex and the message templates are both compile-time.
# --------------------------------------------------------------------------


def test_single_name_ceiling_rounding_to_zero_leaves_a_durable_reason():
    """`_build_buy` already LOGS when the single-name ceiling caps a
    request ("alloc capped by the single-name ceiling") but that message
    contains no rejected/refused/skipped, so it never reached
    `_DropReasonCapture`. When the ceiling leaves EXACTLY zero headroom the
    builder silently returns `None` with no compensating log line at all —
    unlike the ATR/no-stop path in `_resolve_entry_and_stop`, nothing else
    fires for this symbol afterward."""
    from src.portfolio_constructor import STOP_REFUSAL_SIZED_TO_ZERO

    constructor = PortfolioConstructor(ConstructorConfig(max_position_pct=5.0))
    held = _pos("NVDA", qty=50, avg_entry=100.0, current_price=100.0)  # $5,000 = 5% of $100k
    target = TargetPosition(
        symbol="NVDA",
        target_weight_pct=6.0,
        conviction="high",
        thesis="Add to an already-full single-name slot.",
    )
    decisions = constructor.construct_orders(
        targets=[target],
        positions=[held],
        analyses=[_analysis("NVDA", entry=100, stop=95, target=140)],
        total_value=EQUITY,
        price_map={"NVDA": 100.0},
    )
    assert [d for d in decisions if d.symbol == "NVDA" and d.action == "BUY"] == []
    refusals = constructor.drain_refusals()
    assert refusals["NVDA"]["refusal"] == STOP_REFUSAL_SIZED_TO_ZERO
    assert "single-name ceiling" in refusals["NVDA"]["detail"]
    assert "NVDA" in constructor.last_drop_reasons


def test_single_short_ceiling_rounding_to_zero_leaves_a_durable_reason():
    """The SHORT mirror of the test above — `_build_short`'s single-name
    ceiling (the same `max_position_pct`) has the identical silent-zero gap."""
    from src.models import TechAnalysisResult
    from src.portfolio_constructor import STOP_REFUSAL_SIZED_TO_ZERO

    constructor = PortfolioConstructor(ConstructorConfig(max_position_pct=5.0))
    held = _pos("NVDA", qty=-50, avg_entry=100.0, current_price=100.0)  # -5% (5% short)
    target = TargetPosition(
        symbol="NVDA",
        direction="short",
        target_weight_pct=6.0,
        conviction="high",
        thesis="Add to an already-full short slot.",
    )
    short_analysis = TechAnalysisResult(
        symbol="NVDA",
        rating="sell",
        entry_price=100.0,
        stop_loss=110.0,
        reference_target=60.0,
        reasoning="test",
        support_levels=[60.0],
        resistance_levels=[110.0],
        computed_levels=[60.0, 110.0],
        computed_level_touches={60.0: 5, 110.0: 5},
        computed_level_bars={60.0: [(60.0, 60.0)], 110.0: [(110.0, 110.0)]},
        atr_14=(110.0 - 100.0) / 3.5,
        setup_type="range",
        expected_horizon_sessions=60,
        reasoning_chain=_tech_rc(),
        thesis_invalid_if="closes below support",
    )
    decisions = constructor.construct_orders(
        targets=[target],
        positions=[held],
        analyses=[short_analysis],
        total_value=EQUITY,
        price_map={"NVDA": 100.0},
    )
    assert [d for d in decisions if d.symbol == "NVDA" and d.action == "SHORT"] == []
    refusals = constructor.drain_refusals()
    assert refusals["NVDA"]["refusal"] == STOP_REFUSAL_SIZED_TO_ZERO
    assert "single-name ceiling" in refusals["NVDA"]["detail"]
    assert "NVDA" in constructor.last_drop_reasons


def test_gross_exposure_ceiling_block_leaves_a_durable_reason():
    """`apply_gross_ceiling` (`src/risk/rules.py`) refuses a BUY outright and
    logs through the constructor's own `logger.warning("Constructor: %s",
    note)` relay — but every one of its notes reads "Constructor:
    max_gross_exposure: SYMBOL refused — ...", with the rule name sitting
    BETWEEN "Constructor:" and the symbol. `_DropReasonCapture._SYMBOL`
    requires the symbol to follow immediately (through at most one of
    BUY/SHORT/SELL/COVER), so this whole refusal class was invisible to it."""
    from src.risk.rules import GrossCeiling
    from src.portfolio_constructor import STOP_REFUSAL_GROSS_EXPOSURE_CEILING

    constructor = PortfolioConstructor()
    # Fixed 2026-09-24: `apply_gross_ceiling` no longer refuses an entry for
    # being under the flat $500 `min_order_usd` floor — only a genuine ZERO
    # headroom still refuses. `ceiling_x=0.001` (a $100 ceiling on $100k
    # equity) used to hit that removed floor; it is now GRANTED at a small
    # size instead, so the ceiling here is shrunk further, to a literal
    # sub-cent headroom, to still exercise the real "no headroom at all"
    # refusal this test is actually about.
    tiny_ceiling = GrossCeiling(
        ceiling_x=0.0000001,
        base_x=0.0000001,
        drawdown_pct=None,
        alert_owner=False,
        rung="test",
        reason="test-fixture ceiling",
    )  # $100k equity -> $0.01 ceiling, rounds down to a genuine zero
    decisions = constructor.construct_orders(
        targets=[_risk_target("NVDA", 5.0)],
        positions=[],
        analyses=[_analysis("NVDA", entry=100, stop=95, target=140)],
        total_value=EQUITY,
        price_map={"NVDA": 100.0},
        gross_ceiling=tiny_ceiling,
    )
    assert [d for d in decisions if d.symbol == "NVDA" and d.allocation_pct > 0] == []
    refusals = constructor.drain_refusals()
    assert refusals["NVDA"]["refusal"] == STOP_REFUSAL_GROSS_EXPOSURE_CEILING
    assert "ceiling" in refusals["NVDA"]["detail"]
    assert "NVDA" in constructor.last_drop_reasons


def test_a_small_new_position_below_min_risk_is_refused_by_name():
    """Owner ruling 2026-09-30 (board item 183): the churn filter's flat
    `min_trade_weight_delta` floor is DELETED, not resized. A brand-new
    position this small used to be dropped with neither a `TradeDecision`
    row nor a log line (nothing for the regex to miss, because nothing was
    ever logged) — it is now a real, if tiny, BUY the desk's own reasoning
    asked for."""
    constructor = PortfolioConstructor()
    target = TargetPosition(
        symbol="NVDA",
        target_weight_pct=0.1,
        conviction="low",
        thesis="A small idea, once dropped as below the deleted min trade delta.",
    )
    decisions = constructor.construct_orders(
        targets=[target],
        positions=[],
        analyses=[_analysis("NVDA", entry=100, stop=95, target=140)],
        total_value=EQUITY,
        price_map={"NVDA": 100.0},
    )
    # Owner rule 2026-08-27: a 0.1% weight at a 5% stop risks 0.005% of
    # equity, under the 0.5% minimum risk per position -- below it the desk
    # does not trade. Not refused on SIZE (the deleted delta floor), refused
    # on RISK, by name.
    from src.portfolio_constructor.config import STOP_REFUSAL_BELOW_OWNER_MIN_RISK

    assert [d for d in decisions if d.symbol == "NVDA" and d.action == "BUY"] == []
    refusals = constructor.drain_refusals()
    assert refusals["NVDA"]["refusal"] == STOP_REFUSAL_BELOW_OWNER_MIN_RISK


# --------------------------------------------------------------------------
# The regex itself, pinned. If a future call site logs a NEW drop message
# that happens to slip past `_DropReasonCapture._SYMBOL`, this at least
# proves the pattern's shape hasn't silently drifted — and documents,
# verbatim, why the messages above needed a structured fix rather than a
# wider regex (doctrine: the reason must not go on living in log text).
# --------------------------------------------------------------------------


def test_drop_capture_regex_does_not_match_the_defect_class_this_pr_fixes():
    """Run the REAL regex object (not a retyped copy) against the exact
    phrasing each fixed call site used to emit. This is the static
    verification item 10 asked for — no live data, no reproduction run."""
    from src.portfolio_constructor import _DropReasonCapture

    rx = _DropReasonCapture._SYMBOL
    previously_silent = [
        # _plan_risk_targets — the agreement-ceiling refusal (§9.4).
        "Constructor: NVDA produces no order — 1 aligned / 2 opposed = net -1 independent source(s) for this long.",
        # _build_buy / _build_short — alloc capped to zero.
        "Constructor: NVDA alloc capped by risk budget (delta 5.00% -> 0.00% at 5.0% risk budget)",
        "Constructor: NVDA alloc capped by the single-name ceiling "
        "(delta 5.00% -> 0.00%; 5.0% max position, 5.00% already held)",
        # apply_gross_ceiling, relayed through the constructor's own logger.
        "Constructor: max_gross_exposure: NVDA refused — the book would "
        "own $200,000 against a $100 ceiling (0.0x equity), and what the "
        "ceiling still allows is below the $500 minimum worth trading.",
    ]
    for message in previously_silent:
        assert rx.search(message) is None, (
            f"regex now matches a message this PR treats as structurally "
            f"silent — re-check whether the fix at that call site is still "
            f"needed: {message!r}"
        )


# --------------------------------------------------------------------------
# 2026-09-17, intra_check-44594a05: a trim of a held name the intraday scan
# did not analyse is sized from the position's LIVE broker stop.
# Live figures from that run: equity $9,694.25; AAPL 9.763 sh at $332.96,
# live stop $315.85, 1.91% equity at risk; PM asked AAPL 1.0%, NET 1.75%.
# --------------------------------------------------------------------------

_REAL_EQUITY = 9_694.25
_AAPL_QTY, _AAPL_PX, _AAPL_STOP = 9.763, 332.96, 315.85
# avg entry implied by the 1.91% budget risk shown to the PM
_AAPL_ENTRY = _AAPL_STOP + 0.0191 * _REAL_EQUITY / _AAPL_QTY


def _real_plan(live_stops):
    constructor = PortfolioConstructor()
    decisions = constructor.construct_orders(
        targets=[_risk_target("AAPL", 1.0), _risk_target("NET", 1.75)],
        positions=[_pos("AAPL", _AAPL_QTY, _AAPL_ENTRY, _AAPL_PX)],
        analyses=[_analysis("NET", entry=100, stop=90, target=140)],
        total_value=_REAL_EQUITY,
        price_map={"AAPL": _AAPL_PX, "NET": 100.0},
        existing_risk_pct={"AAPL": 1.91},
        clusters=[],
        live_stops=live_stops,
    )
    return constructor, decisions


def test_real_intra_check_plan_sells_aapl_sized_from_its_live_stop_and_buys_net():
    constructor, decisions = _real_plan({"AAPL": _AAPL_STOP})
    by_symbol = {d.symbol: d for d in decisions}
    assert by_symbol["NET"].action == "BUY"
    # Owner ruling 2026-10-09: a held position is kept whole or sold whole.
    # The lower risk number no longer sells part of AAPL; it is held.
    assert by_symbol["AAPL"].action == "HOLD"
    assert constructor.last_refusals["AAPL"]["refusal"] == "held_partial_trim_refused"
    assert "AAPL" not in constructor.last_data_faults


def test_trim_without_live_stop_is_refused_by_name_not_as_a_data_fault(caplog):
    constructor, decisions = _real_plan(None)
    assert [(d.action, d.symbol) for d in decisions] == [("BUY", "NET")]
    assert "AAPL" not in constructor.last_data_faults
    refusal = constructor.last_refusals["AAPL"]
    assert refusal["refusal"] == "trim_without_analysis_has_no_usable_live_stop"
    assert "not a market data fault" in refusal["detail"]
    assert not any("BUY AAPL" in r.getMessage() for r in caplog.records)


def test_trim_with_live_stop_on_wrong_side_is_refused_by_name():
    constructor, _ = _real_plan({"AAPL": _AAPL_PX + 1})
    assert constructor.last_refusals["AAPL"]["refusal"] == ("trim_without_analysis_has_no_usable_live_stop")


def test_trim_sized_from_live_stop_never_grows_the_position():
    """3.0% requested vs ~1.72% open risk at the live stop: no BUY, a HOLD."""
    constructor = PortfolioConstructor()
    decisions = constructor.construct_orders(
        targets=[_risk_target("AAPL", 3.0)],
        positions=[_pos("AAPL", _AAPL_QTY, _AAPL_ENTRY, _AAPL_PX)],
        analyses=[],
        total_value=_REAL_EQUITY,
        price_map={"AAPL": _AAPL_PX},
        existing_risk_pct={"AAPL": 1.91},
        clusters=[],
        live_stops={"AAPL": _AAPL_STOP},
    )
    assert [d.action for d in decisions if d.symbol == "AAPL"] == ["HOLD"]
