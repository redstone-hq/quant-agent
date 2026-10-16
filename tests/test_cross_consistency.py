"""Layer 2 — does the ONE implementation produce the RIGHT number on the book
that used to break it?

What this file used to be, and why it changed
---------------------------------------------
The 2026-09-01 survey found six defects behind 4,110 passing tests. Not one of
them was a component computing its own answer wrongly; every component was
self-consistent and self-tested. They were SEAM defects — two components each
computing the same named quantity a different way, judged against a shared
threshold, with no test in the repo comparing them.

So this file was written to compare implementation against implementation, and
it FAILED on purpose: that failure was its validation. The consolidation has
now landed. Each of those quantities has exactly one definition, and comparing
a function to itself proves nothing — a test that passes no matter what the
code does is worse than no test, because it reads as coverage.

What it is now
--------------
The valuable asset here is `tests/book_fixtures.py`: books built so a NAIVE and
a CORRECT implementation return DIFFERENT numbers, with the arithmetic worked
out by hand in each fixture's docstring. This file is the conformance layer
over those fixtures. Every test below pins the EXACT number the single source
must produce on the adversarial book — the net-short book, the leveraged
inverse book, the winning short, the halted session — and states the wrong
answer the pre-fix implementation gave beside it.

That earns its keep in two directions:

  * Mutate a single source and these fail, because they assert values rather
    than agreement.
  * Change a fixture's numbers without updating its docstring and these fail
    too, which keeps the shared fixture module honest for every other test
    that adopts it.

Where a REAL seam still exists it is still compared, and there is exactly one
left worth the name: `src/risk/rules.py` and `src/quantities.py` each carry a
net-exposure implementation and CANNOT be merged, because a ratified guardrail
(`tests/test_api_safety.py`) forbids `src/api/` from importing `src.risk`. The
cockpit's gauge is drawn against the engine's ceiling, so the two must agree —
see Quantity 5, which also documents the one place they are deliberately NOT
interchangeable.

Deliberately NOT duplicated here
--------------------------------
Three sibling files already exercise the consolidated functions against their
own hand-rolled books, and this file does not restate them:

  * `tests/test_one_definition_per_quantity.py` — book exposure, position
    weight and P&L percent, plus AST guards against a second definition.
  * `tests/test_single_definition_quantities.py` — deployable cash, "%
    deployed", dollar volume and the inverse-ETF roster, engine vs API.
  * `tests/test_atr_is_wilder_everywhere.py` — the ATR consolidation in full.

What this file adds is those functions measured on the SHARED hostile books,
including cases none of the three cover: a book that is net short, and a book
whose only leveraged holding is a -3x inverse ETF held long.
"""

from __future__ import annotations

import pytest

from src.quantities import (
    avg_dollar_volume,
    net_exposure_pct,
    net_exposure_usd,
)
from src.risk.metrics import unrealized_pnl_pct
from src.risk.rules import book_exposure, position_weight_pct
from tests.book_fixtures import (
    ALL_BOOKS,
    SWEEP_SYMBOL,
    Book,
    halted_session_bars,
    leveraged_inverse_book,
    net_short_book,
    ordinary_long_book,
    sweep_parked_book,
    trending_bars,
    winning_short_book,
)

# How close a computed value must land to the figure the fixture docstring
# works out by hand. Not zero: independent float paths and a 2dp rounding
# inside one implementation legitimately differ in the last place. Half a
# percentage point is far below every divergence the survey measured and far
# above float noise.
ABS_TOL_PCT = 0.5
REL_TOL = 0.005


def _naive_cash_complement_pct(book: Book) -> float:
    """The PRE-FIX PM figure: `(total_value - cash) / total_value * 100`.

    Kept as a transcription of code that no longer exists, so the tests below
    can state what the wrong answer was rather than merely asserting the right
    one. Equity is `cash + sum(market_value)` and a held short's market value
    is NEGATIVE, so a short made the book look LESS deployed to the seat that
    then deployed more. It also counts the parked sweep vehicle as invested.
    """
    return (book.total_value - book.cash) / book.total_value * 100


# ===========================================================================
# Quantity 1 — "how invested is the book".
#
# One source: `src/risk/rules.py::book_exposure`, which reports the question
# three ways because there are genuinely three questions. `deployed` is the
# cash-complement measure and the only one comparable to macro's
# `target_invested_pct`; `net` is signed and leverage-aware; `gross` is
# unsigned and leverage-aware and is what the §11.2 ceiling caps.
# ===========================================================================

#: (deployed_pct, net_pct, gross_pct) — worked out by hand in each fixture's
#: docstring in `tests/book_fixtures.py`. If a fixture's numbers change, these
#: must change with them, which is the point.
_EXPECTED_EXPOSURE = {
    "ordinary_long": (60.0, 60.0, 60.0),
    "leveraged_inverse": (60.0, -100.0, 140.0),
    "net_short": (44.4444, -44.4444, 44.4444),
    "winning_short": (65.0, -15.0, 65.0),
    "sweep_parked": (46.0, 46.0, 46.0),
}


@pytest.mark.parametrize("factory", ALL_BOOKS, ids=lambda f: f.__name__)
def test_book_exposure_matches_the_arithmetic_each_fixture_documents(factory):
    """The single source, pinned to the hand-computed answer on every book.

    `leveraged_inverse` is the case no other suite covers: 60% deployed,
    -100% net and 140% gross from ONE portfolio, 80pp apart. Before the fix
    the PM was shown 60 and the risk gate enforced 100 and both were judged
    against the same target.
    """
    book = factory()
    exposure = book_exposure(
        book.positions,
        book.total_value,
        cash_park_symbol=SWEEP_SYMBOL,
    )
    want_deployed, want_net, want_gross = _EXPECTED_EXPOSURE[book.name]

    assert exposure.deployed_pct == pytest.approx(want_deployed, abs=1e-3), (
        f"{book.name}: deployed% is {exposure.deployed_pct:.4f}, the fixture docstring works out {want_deployed}"
    )
    assert exposure.net_pct == pytest.approx(want_net, abs=1e-3), (
        f"{book.name}: net% is {exposure.net_pct:.4f}, expected {want_net} "
        "(SIGNED — negative means net short, and the sign is load-bearing)"
    )
    assert exposure.gross_pct == pytest.approx(want_gross, abs=1e-3), (
        f"{book.name}: gross% is {exposure.gross_pct:.4f}, expected "
        f"{want_gross} (unsigned, leverage-weighted — the §11.2 ceiling)"
    )


@pytest.mark.parametrize(
    "factory",
    [net_short_book, winning_short_book, sweep_parked_book],
    ids=lambda f: f.__name__,
)
def test_the_pre_fix_cash_complement_is_still_wrong_on_these_books(factory):
    """States the defect this quantity was consolidated to remove.

    On these three books `(total_value - cash)` and the deployed measure give
    materially different answers, and the pre-fix one is the wrong answer:

        net_short      -44.4% vs  44.4%  — the SIGN flips, and a PM told the
                                            book is -44% invested against a
                                            70% target buys into a short book
        winning_short   20.0% vs  65.0%  — a short read as capital RETURNED
                                            to the pile rather than put to work
        sweep_parked    60.0% vs  46.0%  — parked cash counted as exposure

    If `book_exposure` ever regresses to the cash complement, this fails.
    """
    book = factory()
    naive = _naive_cash_complement_pct(book)
    deployed = book_exposure(
        book.positions,
        book.total_value,
        cash_park_symbol=SWEEP_SYMBOL,
    ).deployed_pct

    assert abs(naive - deployed) > ABS_TOL_PCT, (
        f"\n  {book.name} — {book.trap}"
        f"\n      (total_value - cash) / total_value   {naive:>10.4f}%"
        f"\n      book_exposure(...).deployed_pct      {deployed:>10.4f}%"
        "\n  These agreeing means the deployed measure has regressed to the "
        "cash\n  complement, and this fixture no longer separates them."
    )


def test_the_control_book_hides_the_exposure_defect():
    """Proof of WHY 4,110 tests missed this — and a guard on the fixtures.

    On a long-only unlevered book with no sweep holding, all four measures
    coincide exactly. Any test written against such a book passes against the
    correct implementation and against every broken one. If this ever fails,
    the control fixture has stopped being a control and everything above
    proves less than it claims.
    """
    book = ordinary_long_book()
    exposure = book_exposure(
        book.positions,
        book.total_value,
        cash_park_symbol=SWEEP_SYMBOL,
    )
    assert exposure.deployed_pct == pytest.approx(60.0, abs=1e-9)
    assert exposure.net_pct == pytest.approx(exposure.deployed_pct, abs=1e-9)
    assert exposure.gross_pct == pytest.approx(exposure.deployed_pct, abs=1e-9)
    assert _naive_cash_complement_pct(book) == pytest.approx(exposure.deployed_pct, abs=1e-9)


# ===========================================================================
# Quantity 2 — position weight (the share of the book one symbol occupies).
#
# One source: `src/risk/rules.py::weight_pct_of` / `position_weight_pct`.
# Signed, gross-levered.
# ===========================================================================


def test_position_weight_is_gross_levered_on_the_inverse_etf():
    """`Weight: 18.0% ... DRIFT` and `drift-flagged: 0`, three lines apart.

    SQQQ is 40% of this book by market value and -3x levered, so its gross
    weight is 120%. The pre-fix PM prompt rendered the gross weight on the
    position line and counted drift against the raw 40%, in a prompt whose own
    header states "All weights are GROSS-leverage weights". A 12% drift
    threshold is crossed by one number and not the other.
    """
    book = leveraged_inverse_book()
    position = book.by_symbol("SQQQ")
    weight = position_weight_pct(position, book.total_value)
    raw = position.market_value / book.total_value * 100

    assert raw == pytest.approx(40.0, abs=1e-9), "fixture drifted"
    assert weight == pytest.approx(120.0, abs=1e-9), (
        f"SQQQ is 40% of the book by market value and -3x levered; its gross weight is 120%, not {weight:.1f}%"
    )


def test_position_weight_is_signed_on_a_held_short():
    """The sign is not decoration — it keeps drift a long-side question.

    A winning short's |market_value| SHRINKS toward zero, so it cannot drift
    into an oversized position the way an appreciating long can. TSLA here is
    short $40,000 against $100,000 of equity: -40.0%, not +40.0%.
    """
    book = winning_short_book()
    short = position_weight_pct(book.by_symbol("TSLA"), book.total_value)
    long = position_weight_pct(book.by_symbol("AAPL"), book.total_value)

    assert short == pytest.approx(-40.0, abs=1e-9), f"a held short's weight must be negative; got {short:.1f}%"
    assert long == pytest.approx(25.0, abs=1e-9)


def test_the_constructor_and_the_risk_rules_weigh_a_position_the_same():
    """A REAL two-implementation comparison that still exists.

    `PortfolioConstructor._current_weights` computes weights for the
    rebalancing comparison; `position_weight_pct` is what the risk rules and
    the PM prompt use. Nothing forces them to agree structurally, and the
    constructor reading a 3x SQQQ's raw 40% as its target is what turned a
    restated weight into a 67% SELL nobody asked for.
    """
    from src.portfolio_constructor import PortfolioConstructor

    book = leveraged_inverse_book()
    weights = PortfolioConstructor._current_weights(book.positions, book.total_value)
    for position in book.positions:
        assert weights[position.symbol] == pytest.approx(position_weight_pct(position, book.total_value)), (
            f"{position.symbol}: the constructor weighs it "
            f"{weights[position.symbol]:.2f}% and the risk rules weigh it "
            f"{position_weight_pct(position, book.total_value):.2f}%"
        )
    assert weights["SQQQ"] == pytest.approx(120.0)


# ===========================================================================
# Quantity 3 — unrealized P&L percent.
#
# One source: `src/risk/metrics.py::unrealized_pnl_pct`, denominator
# `abs(avg_entry * qty)`, returning None rather than 0.0 when unknowable.
# ===========================================================================

#: symbol -> (expected pct, what the pre-fix `cost_basis > 0` guard printed)
_EXPECTED_PNL_PCT = {
    "TSLA": (20.0, 0.0),
    "AAPL": (66.6667, 66.6667),
}


@pytest.mark.parametrize("symbol", sorted(_EXPECTED_PNL_PCT))
def test_pnl_percent_is_measured_on_the_absolute_cost_basis(symbol):
    """A winning short rendered `+$10000.00 (+0.0%)` — one line, two claims.

    TSLA here is short 200 @ $250 now trading at $200: +$10,000 of profit on a
    signed cost basis of -$50,000. A bare `pnl / cost` reports -20.0% and
    reads as a loser; the `if cost > 0` guard some call sites used instead
    substitutes 0.0, which is the line above. The absolute basis gives +20.0%.
    """
    book = winning_short_book()
    want, pre_fix = _EXPECTED_PNL_PCT[symbol]
    got = unrealized_pnl_pct(book.by_symbol(symbol))

    assert got is not None, f"{symbol}: P&L% is unknowable on a complete book"
    assert got == pytest.approx(want, abs=1e-3), (
        f"{symbol}: P&L% is {got:.4f}%, expected {want}% (the pre-fix implementation printed {pre_fix}%)"
    )


def test_two_positions_that_made_the_same_dollars_report_the_same_way():
    """A sanity anchor that does not depend on the implementation's shape.

    The long and the short in this book each made exactly $10,000. Their P&L
    percentages differ only because their cost bases differ ($15,000 against
    $50,000). Whatever the implementation, neither may report 0.0% while
    holding a $10,000 gain, and neither may report a negative percentage.
    """
    book = winning_short_book()
    for position in book.positions:
        assert position.unrealized_pnl == pytest.approx(10_000.0), "fixture drifted"
        pct = unrealized_pnl_pct(position)
        assert pct is not None and pct > 0.0, (
            f"{position.symbol} holds a $10,000 gain but its P&L% renders as "
            f"{pct} — a `cost_basis > 0` guard swallowed a short's negative "
            f"basis (signed cost = {position.avg_entry * position.qty:,.0f})"
        )
    # Smaller basis, same dollars, larger percentage. Ordering, not a value.
    assert unrealized_pnl_pct(book.by_symbol("AAPL")) > unrealized_pnl_pct(book.by_symbol("TSLA"))


# ===========================================================================
# Quantity 4 — deployable cash.
#
# One source: `src/quantities.py::deployable_cash` — `cash + parked sweep
# value`, the money the account already owns. The API published
# `max(cash - reserve, 0)` under the same word.
# ===========================================================================


def test_deployable_cash_on_the_sweep_book_is_cash_plus_parked(monkeypatch):
    """The engine sizes trades against one figure; the operator saw another.

    On this book: cash $40,000, $14,000 parked in the sweep vehicle, equity
    $100,000, 5% reserve. Deployable is $54,000. The number the dashboard used
    to print under that word was $35,000 — the reserve-adjusted figure, which
    is a different question and is still reported under its own name.
    """
    from src.api import routes_live

    book = sweep_parked_book()
    parked = book.by_symbol(SWEEP_SYMBOL).market_value

    monkeypatch.setattr(routes_live, "get_cash_sweep_enabled", lambda: True)
    monkeypatch.setattr(routes_live, "get_cash_sweep_symbol", lambda: SWEEP_SYMBOL)
    monkeypatch.setattr(routes_live, "get_cash_reserve_pct", lambda: 5.0)
    monkeypatch.setattr(
        routes_live,
        "read_positions",
        lambda *a, **k: {
            "error": None,
            "positions": [
                {
                    "symbol": p.symbol,
                    "market_value": p.market_value,
                    "is_cash_equivalent": p.symbol == SWEEP_SYMBOL,
                }
                for p in book.positions
            ],
        },
    )

    liquidity = routes_live._compute_liquidity(book.cash, book.total_value)

    assert parked == pytest.approx(14_000.0), "fixture drifted"
    assert liquidity.deployable_cash is not None, "API produced no deployable_cash"
    assert liquidity.deployable_cash == pytest.approx(book.cash + parked, rel=REL_TOL), (
        f"deployable_cash is {liquidity.deployable_cash:,.0f}; the engine sizes "
        f"against cash + parked sweep value = {book.cash + parked:,.0f}"
    )
    assert liquidity.deployable_cash == pytest.approx(54_000.0, rel=REL_TOL)

    # The conservative figure survives, under its own name and only there.
    assert liquidity.cash_above_reserve == pytest.approx(35_000.0, rel=REL_TOL)
    assert liquidity.deployable_cash != liquidity.cash_above_reserve


# ===========================================================================
# Quantity 5 — net exposure: the gauge and the ceiling it is drawn against.
#
# TWO implementations, and they must stay two. `src/risk/rules.py` cannot be
# imported by `src/api/` — `tests/test_api_safety.py` ratifies that — so
# `src/quantities.py::net_exposure_usd/_pct` exists to serve the same measure
# to the read surface. This is the one live seam left in this file.
# ===========================================================================


@pytest.mark.parametrize("factory", ALL_BOOKS, ids=lambda f: f.__name__)
def test_the_cockpit_gauge_and_the_engine_ceiling_measure_the_same_magnitude(factory):
    """A gauge whose needle and whose redline come from different definitions
    tells the operator the book is safe while the engine is refusing trades.
    This is the one defect a human sees directly.

    Compared LIKE FOR LIKE. `max_total_position_pct` compares
    `abs(book_exposure(...).net_pct)` — see the `abs()` at the rule — and
    `net_exposure_pct` applies that same `abs()` internally. Comparing the
    signed value to the magnitude fails on every net-short book while both
    sides are correct; see the test below, which pins that difference on
    purpose.
    """
    book = factory()
    engine_signed = book_exposure(
        book.positions,
        book.total_value,
        cash_park_symbol=SWEEP_SYMBOL,
    ).net_pct
    served = net_exposure_pct(
        net_exposure_usd(book.positions, cash_park_symbol=SWEEP_SYMBOL),
        book.total_value,
    )

    assert served is not None, f"{book.name}: the gauge produced no percentage"
    assert abs(engine_signed) == pytest.approx(served, abs=1e-3), (
        f"\n  net exposure disagrees across the two implementations."
        f"\n  book: {book.name} — {book.trap}"
        f"\n      risk/rules.py::book_exposure  |net_pct|  {abs(engine_signed):>10.4f}%"
        f"\n      quantities.py::net_exposure_pct         {served:>10.4f}%"
        "\n  These cannot be merged — src/api may not import src.risk — so the "
        "cockpit's\n  needle and the ceiling it is drawn against must be kept "
        "in step by this test."
    )


@pytest.mark.parametrize("factory", [leveraged_inverse_book, net_short_book], ids=lambda f: f.__name__)
def test_the_signed_and_the_magnitude_forms_are_deliberately_different(factory):
    """Not drift — a difference that must survive, so it is pinned here.

    `book_exposure(...).net_pct` is SIGNED: a negative reading says "net
    short" out loud, and the macro advisory needs that direction because
    erasing it was the defect there. `net_exposure_pct` is a MAGNITUDE,
    matching the rule it feeds: a book 150% net short is as far over a
    magnitude ceiling as one 150% net long.

    Both books here are net short, so the two forms differ by exactly twice
    the magnitude. Anyone tempted to "fix" that disagreement should read this
    test first.
    """
    book = factory()
    signed = book_exposure(
        book.positions,
        book.total_value,
        cash_park_symbol=SWEEP_SYMBOL,
    ).net_pct
    magnitude = net_exposure_pct(
        net_exposure_usd(book.positions, cash_park_symbol=SWEEP_SYMBOL),
        book.total_value,
    )

    assert signed < 0, f"{book.name} is a net-short fixture; net_pct must be negative"
    assert magnitude is not None and magnitude > 0
    assert magnitude == pytest.approx(-signed, abs=1e-3)


# ===========================================================================
# Quantity 6 — average dollar volume.
#
# One source: `src/quantities.py::avg_dollar_volume`. Twenty sessions, a
# halted zero-volume session KEPT as a real zero.
# ===========================================================================

#: `halted_session_bars()` — 20 sessions, one of them halted. Worked out from
#: the fixture: sum(close x volume) over the trailing 20, divided by 20.
_HALTED_FIXTURE_ADV_USD = 118_604_333.33  # 30 consecutive days, all inside one calendar month


def _adv_dropping_the_halt(bars) -> float:
    """The PRE-FIX volume-stats definition: divide by the SURVIVING count.

    Dropping the halt inflates the average in exactly the direction that
    wrongly ADMITS an illiquid symbol through a liquidity gate. On this
    fixture it reads 5.26% high.
    """
    recent = bars  # the fixture sits inside one calendar month
    values = [float(b.close) * float(b.volume) for b in recent if float(b.volume) > 0]
    return sum(values) / len(values)


def test_a_halted_session_is_kept_as_a_real_zero():
    """The exchange published a bar and the volume was genuinely zero.

    A halted day is not missing data. Keeping it is the conservative reading
    and the only one an admission gate may use.
    """
    bars = halted_session_bars()
    measured = avg_dollar_volume(bars)
    dropped = _adv_dropping_the_halt(bars)

    assert measured == pytest.approx(_HALTED_FIXTURE_ADV_USD, rel=REL_TOL), (
        f"average dollar volume is {measured:,.0f} on the halted-session "
        f"fixture; twenty sessions with the halt kept as zero gives "
        f"{_HALTED_FIXTURE_ADV_USD:,.0f}"
    )
    assert measured < dropped
    assert dropped / measured - 1 == pytest.approx(0.0345, abs=5e-4), (
        "the fixture no longer separates the two definitions — the halt must land inside the trailing 20-session window"
    )


def test_both_real_consumers_report_the_pinned_number():
    """The top-mover digest and the tech-analyst context block, on one tape.

    They differed in two ways at once before the fix: 20 bars versus 21, and a
    halted session dropped versus kept. Both now route through the single
    definition, so this asserts the VALUE each publishes rather than merely
    that they match each other.
    """
    from src.data.context import compute_market_context
    from src.pipeline_prompt_facts import _missed_ops_quality_metrics

    bars = halted_session_bars()
    digest_millions, _, _ = _missed_ops_quality_metrics(bars, lookback_days=20)
    context = compute_market_context(bars)

    assert digest_millions is not None, "digest produced no dollar volume"
    assert context is not None and context.avg_dollar_volume_20d is not None

    assert digest_millions * 1_000_000 == pytest.approx(_HALTED_FIXTURE_ADV_USD, rel=REL_TOL), (
        f"pipeline.py::_missed_ops_quality_metrics reports "
        f"${digest_millions:,.2f}M, expected "
        f"${_HALTED_FIXTURE_ADV_USD / 1e6:,.2f}M"
    )
    assert context.avg_dollar_volume_20d == pytest.approx(_HALTED_FIXTURE_ADV_USD, rel=REL_TOL), (
        f"data/context.py::compute_market_context reports "
        f"{context.avg_dollar_volume_20d:,.0f}, expected "
        f"{_HALTED_FIXTURE_ADV_USD:,.0f}"
    )


# ===========================================================================
# Quantity 7 — average true range.
#
# One source: `src/data/technical.py::atr_series` (Wilder, via `ta`).
# `data/context.py` imports it and no longer has a private version — the
# `_atr_series` this file used to reach for is gone.
# ===========================================================================

_ATR_PERIOD = 14


def _simple_moving_average_atr(bars, period: int = _ATR_PERIOD) -> float:
    """The PRE-FIX `context.py` definition, transcribed: a FLAT average.

    True ranges convolved with a flat `period`-wide kernel is a simple moving
    average, not an ATR. It drops a volatility shock off a cliff exactly
    `period` bars later, where Wilder's recursion bleeds it off geometrically.
    Measured across 101 symbols and 973 real sessions the two disagreed by a
    mean absolute 7.05%.
    """
    true_ranges = []
    for i, bar in enumerate(bars):
        if i == 0:
            true_ranges.append(bar.high - bar.low)
            continue
        prev_close = bars[i - 1].close
        true_ranges.append(
            max(
                bar.high - bar.low,
                abs(bar.high - prev_close),
                abs(bar.low - prev_close),
            )
        )
    window = true_ranges[-period:]
    return sum(window) / len(window)


def test_the_analyst_and_the_risk_path_read_one_wilder_atr():
    """ATR sets stop distance, and stop distance sets position size.

    `trending_bars()` carries a deliberate volatility burst in its middle
    third, which is the only condition under which the two smoothings are
    measurably apart — on flat data they converge and the defect is invisible.
    The burst puts them 11.1% apart here, so this fixture actually separates
    them.
    """
    from src.data.context import compute_market_context
    from src.data.technical import atr_series, compute_indicators

    bars = trending_bars()
    series = atr_series(bars)
    assert series.size, "fixture produced no ATR series"
    wilder = float(series[-1])
    flat = _simple_moving_average_atr(bars)

    # The fixture still separates the two smoothings. Without this the rest of
    # the test would pass against either implementation.
    divergence = abs(flat - wilder) / wilder * 100
    assert divergence > 5.0, (
        f"the flat kernel and Wilder's are only {divergence:.2f}% apart on "
        "these bars — the volatility burst has gone out of the fixture and "
        "this test can no longer tell them apart"
    )

    indicators = compute_indicators("TEST", bars)
    assert indicators.atr_14 == pytest.approx(round(wilder, 2), abs=1e-9), (
        f"the risk path's atr_14 is {indicators.atr_14}, the shared Wilder series ends at {wilder:.4f}"
    )

    context = compute_market_context(bars)
    assert context is not None and context.atr_pct is not None
    expected_pct = round(wilder / bars[-1].close * 100.0, 2)
    flat_pct = round(flat / bars[-1].close * 100.0, 2)
    assert context.atr_pct == pytest.approx(expected_pct, abs=0.01), (
        "\n  average true range disagrees across implementations."
        f"\n      data/context.py::atr_pct                  {context.atr_pct:>10.2f}%"
        f"\n      data/technical.py::atr_series (Wilder)    {expected_pct:>10.2f}%"
        f"\n      a flat kernel over the same bars would be {flat_pct:>10.2f}%"
        "\n  Two ATRs is two different books, not two views of one."
    )
    assert context.atr_pct != pytest.approx(flat_pct, abs=1e-9), "context.py is reporting the flat-kernel average again"
