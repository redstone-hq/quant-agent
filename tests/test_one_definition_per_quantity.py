"""One definition per quantity — and a guard that FAILS if a second appears.

Three quantities were each computed two different ways, and both ways reached
a decision-maker. Measured on identical books before the fix:

1. "How invested is the book" — PM was told 70% ("10pp OVER target") while RM
   was told 10% ("50pp UNDER target, do NOT scale down") about ONE book
   ($50k AAPL long + $20k SQQQ, $100k equity, 60% target). Same book, same
   target, opposite sign. Two further copies of the same quantity lived in the
   PM's own prompt.
2. Position weight — the PM's position line rendered `Weight: 18.0% DRIFT`
   from a gross-leverage weight while its facts block three lines later
   rendered `drift-flagged: 0` from a raw one, in a prompt that states "All
   weights are GROSS-leverage weights".
3. Unrealized P&L% — a winning short (-100 @ $110, now $100, +$1,000) printed
   `P&L: $1000.00 (+0.0%)`, because a `cost_basis > 0` guard silently
   substituted zero for a short's negative cost basis.

The single sources are `src.risk.rules.book_exposure`,
`src.risk.rules.weight_pct_of` / `position_weight_pct`, and
`src.risk.metrics.unrealized_pnl_pct`.

`test_no_second_definition_of_*` are the anti-regression guards: they parse
every module under `src/` and fail on any INLINE recomputation of one of the
three, wherever it appears. Fixing a call site is not the fix; deleting the
duplicate is.
"""

import ast
import functools
import pathlib
from unittest.mock import MagicMock

import pytest

from tests import _shared_ast_cache

from src.agents.portfolio_manager import PortfolioManagerAgent
from src.agents.position_reviewer import PositionReviewerAgent
from src.models import Position
from src.pipeline import TradingPipeline
from src.portfolio_constructor import PortfolioConstructor
from src.risk.metrics import unrealized_pnl_pct
from src.risk.rules import book_exposure, position_weight_pct, weight_pct_of
from tests.pipeline_factory import build_pipeline

SRC = pathlib.Path(__file__).resolve().parent.parent / "src"


def _pos(symbol, qty, avg, current, sector="Technology") -> Position:
    return Position(
        symbol=symbol,
        qty=qty,
        avg_entry=avg,
        current_price=current,
        market_value=qty * current,
        unrealized_pnl=(current - avg) * qty,
        sector=sector,
    )


# --------------------------------------------------------------------------
# The guards. Each walks every `src/**/*.py` AST looking for the SHAPE of an
# inline recomputation, so a second definition is caught wherever it is
# written and whatever it is named — not only at the call sites fixed today.
# --------------------------------------------------------------------------


@functools.lru_cache(maxsize=1)
def _binops_cached() -> tuple[tuple[pathlib.Path, int, ast.BinOp, str], ...]:
    """Every (path, lineno, node, flattened_source) BinOp under src/, parsed once.

    Three tests below each need this same walk of the whole src/ tree
    (~11s on a CI runner). Recomputing it per test made this one file cost
    three times the parse for identical input; caching it for the session
    keeps every test's assertions unchanged while paying the parse once.
    Parsing itself goes through `_shared_ast_cache`, which is keyed by
    absolute file path rather than by caller, so a file already parsed by
    `test_ops_scripts_importable.py`'s whole-repo scan (same src/ tree,
    different property) is not re-read or re-parsed here either.
    """
    hits = []
    for path, source, tree in _shared_ast_cache.parse_tree(SRC):
        for node in ast.walk(tree):
            if isinstance(node, ast.BinOp):
                segment = ast.get_source_segment(source, node) or ""
                hits.append((path, node.lineno, node, " ".join(segment.split())))
    return tuple(hits)


def _binops():
    """(path, lineno, node, flattened_source) for every BinOp under src/."""
    return _binops_cached()


def _report(hits, quantity, single_source):
    return (
        f"{len(hits)} inline recomputation(s) of {quantity} found. There is "
        f"exactly ONE definition of this quantity and it is `{single_source}`; "
        f"call it instead of recomputing it. Two definitions of this number "
        f"have already reached two different decision-makers at once.\n"
        + "\n".join(f"  {p}:{n}  {s}" for p, n, s in hits)
    )


def test_no_second_definition_of_book_exposure():
    """ "How invested is the book" is `book_exposure`, and nothing else.

    Catches both historical forms: the PM's `total_value - cash` subtraction
    and the gate's `abs(<signed net>) / total_value * 100`.
    """
    hits = []
    for path, lineno, node, flat in _binops():
        # `invested = equity - cash`
        if isinstance(node.op, ast.Sub):
            left = ast.dump(node.left)
            right = ast.dump(node.right)
            if ("total_value" in left or "equity" in left) and "cash" in right:
                hits.append((path, lineno, flat))
        # `abs(net) / total_value * 100`
        if isinstance(node.op, ast.Mult) and "100" in flat and "/" in flat:
            numerator = ast.dump(node.left)
            if "total_value" in numerator and flat.startswith("abs("):
                hits.append((path, lineno, flat))
    assert not hits, _report(hits, "book exposure", "src.risk.rules.book_exposure")


def test_no_second_definition_of_position_weight():
    """A position's weight is `weight_pct_of` / `position_weight_pct`.

    Catches `<market value> [* multiplier] / <equity> * 100` in any spelling,
    including the leverage-multiplied form the engine cap used.
    """
    hits = []
    for path, lineno, node, flat in _binops():
        if not isinstance(node.op, ast.Mult) or "100" not in flat or "/" not in flat:
            continue
        if "total_value" not in flat and "equity" not in flat:
            continue
        if "market_value" in flat or "gross_mul" in flat or "_gross_multiplier" in flat:
            hits.append((path, lineno, flat))
    assert not hits, _report(
        hits,
        "position weight",
        "src.risk.rules.weight_pct_of",
    )


def test_no_second_definition_of_unrealized_pnl_pct():
    """P&L percent is `unrealized_pnl_pct`, and its denominator is absolute.

    Any division whose numerator mentions `unrealized_pnl` is a second
    definition — the denominator is exactly what keeps going wrong.
    """
    allowed = SRC / "risk" / "metrics.py"
    hits = []
    for path, lineno, node, flat in _binops():
        if path == allowed or not isinstance(node.op, ast.Div):
            continue
        if "unrealized_pnl" in ast.dump(node.left):
            hits.append((path, lineno, flat))
    assert not hits, _report(
        hits,
        "unrealized P&L percent",
        "src.risk.metrics.unrealized_pnl_pct",
    )


# --------------------------------------------------------------------------
# (1) Book exposure — both consumers, one number.
# --------------------------------------------------------------------------


def _pipeline_for_facts():
    pipeline = build_pipeline(db=MagicMock(), tech_store=MagicMock())
    pipeline.db.compute_trade_calibration.return_value = {}
    pipeline.db.get_recent_agent_outputs.return_value = []
    pipeline.tech_store.get_history.return_value = []
    return pipeline


def _gate_projected_pct(positions, total_value, target):
    """Run the real pre-trade advisory and read back the % it reported.

    Goes through `TradingPipeline._filter_hard_risk_decisions` with an empty
    decision list, which is the shortest path that still executes the
    production `deployment_gap` code (formerly `macro_exposure_deviation`).
    """
    pipeline = build_pipeline(
        _sweeper=lambda: None,
        risk_engine=MagicMock(check=MagicMock(return_value=[])),
        # ATR(14) = 1% of price for every name: a measured, small book drift.
        _atr_for_symbol=lambda symbol: {"AAPL": 1.0, "SQQQ": 0.2}.get(symbol),
    )
    _allowed, violations, _blocked = pipeline.risk_gate._filter_hard_risk_decisions(
        decisions=[],
        positions=positions,
        total_value=total_value,
        cash=0.0,
        invested_target_pct=target,
    )
    gap = [v for v in violations if v.rule == "deployment_gap"]
    return gap[0].value if gap else None


THE_MEASURED_BOOK = [
    _pos("AAPL", 500, 90.0, 100.0),  # $50k long
    _pos("SQQQ", 1000, 22.0, 20.0),  # $20k of a -3x inverse ETF
]


def test_pm_and_risk_gate_report_the_same_invested_pct():
    """The exact book that produced +10pp OVER for PM and -50pp UNDER for RM.

    The advisory only speaks when the book is more than its own one-session
    drift (here 0.7pp, from a stubbed ATR) UNDER the target, so the comparison is
    made at the 100% fully-invested mandate
    (2026-09-17), which this 70% book is 30pp under. `target=60` is covered by
    the test below, where the whole point is that the advisory now stays
    SILENT because it agrees with the PM.
    """
    total_value = 100_000.0
    facts = _pipeline_for_facts()._build_pm_facts(
        positions=THE_MEASURED_BOOK,
        analyses=[],
        total_value=total_value,
        cash=30_000.0,
        recent_performance={},
    )
    gate_pct = _gate_projected_pct(THE_MEASURED_BOOK, total_value, target=100.0)

    assert facts.invested_pct == pytest.approx(70.0)
    assert gate_pct == pytest.approx(facts.invested_pct), (
        "PM and the pre-trade gate are describing the same book with two different numbers again"
    )
    # And the direction is reported rather than erased: 50k long - 60k of
    # effective short exposure = -10k net on 100k of equity.
    assert facts.net_exposure_pct == pytest.approx(-10.0)


def test_the_gate_no_longer_contradicts_the_pm_on_the_measured_book():
    """At a 60% target the PM reads +10pp OVER — within its own band.

    The gate used to answer the same question with `abs(50k - 60k)/100k` =
    10% and fire the old `macro_exposure_deviation` at -50pp with "do NOT scale down
    the remaining BUYs". It now agrees with the PM and says nothing.
    """
    assert _gate_projected_pct(THE_MEASURED_BOOK, 100_000.0, target=60.0) is None


def test_pm_account_status_and_pm_facts_agree_within_one_prompt():
    """`Invested:` in Account Status vs `invested=` in the facts block."""
    total_value = 100_000.0
    facts = _pipeline_for_facts()._build_pm_facts(
        positions=THE_MEASURED_BOOK,
        analyses=[],
        total_value=total_value,
        cash=30_000.0,
        recent_performance={},
    )
    exposure = book_exposure(THE_MEASURED_BOOK, total_value)
    assert exposure.deployed_pct == pytest.approx(facts.invested_pct)
    assert exposure.deployed_usd == pytest.approx(70_000.0)


def test_a_net_short_book_is_not_reported_as_positively_invested():
    """The `abs()` on a signed net made short and long of one size identical.

    A book that is net SHORT must say so. `deployed` is unsigned because a
    short is capital put to WORK, but `net` carries the direction and must be
    negative here.
    """
    net_short = [
        _pos("AAPL", 100, 90.0, 100.0),  # $10k long
        _pos("TSLA", -300, 110.0, 100.0),  # $30k short
    ]
    exposure = book_exposure(net_short, 100_000.0)
    assert exposure.deployed_pct == pytest.approx(40.0)  # 10k + |−30k|
    assert exposure.net_pct == pytest.approx(-20.0)  # 10k − 30k
    assert exposure.net_pct < 0, "a net-short book must not read as net long"

    mirror_long = [
        _pos("AAPL", 100, 90.0, 100.0),
        _pos("TSLA", 300, 90.0, 100.0),  # $30k LONG instead
    ]
    mirrored = book_exposure(mirror_long, 100_000.0)
    assert mirrored.deployed_pct == pytest.approx(exposure.deployed_pct)
    assert mirrored.net_pct != pytest.approx(exposure.net_pct), (
        "long and short of the same size must be distinguishable"
    )


def test_a_short_no_longer_makes_the_book_look_less_deployed():
    """`total_value - cash` netted a short AGAINST the longs.

    Equity is `cash + sum(market_value)` and a short's market_value is
    negative, so the old subtraction reported the $40k-committed book below
    as 40k-30k = -20k of "investment" — and asked the PM to deploy more.
    """
    positions = [
        _pos("AAPL", 100, 90.0, 100.0),
        _pos("TSLA", -300, 110.0, 100.0),
    ]
    total_value = 100_000.0
    old_definition = sum(p.market_value for p in positions) / total_value * 100
    assert old_definition == pytest.approx(-20.0)
    assert book_exposure(positions, total_value).deployed_pct == pytest.approx(40.0)


def test_the_cash_park_is_not_exposure():
    positions = [_pos("AAPL", 100, 90.0, 100.0), _pos("SGOV", 200, 100.0, 100.0)]
    parked = book_exposure(positions, 100_000.0, cash_park_symbol="SGOV")
    assert parked.deployed_pct == pytest.approx(10.0)
    assert parked.net_pct == pytest.approx(10.0)


def test_pending_orders_count_toward_deployment_for_both_sides():
    """A SHORT commits capital; deployment counts it, direction subtracts it."""
    exposure = book_exposure(
        [],
        100_000.0,
        pending_deployed_usd=25_000.0,
        pending_net_usd=-25_000.0,
    )
    assert exposure.deployed_pct == pytest.approx(25.0)
    assert exposure.net_pct == pytest.approx(-25.0)


# --------------------------------------------------------------------------
# (2) Position weight — gross everywhere.
# --------------------------------------------------------------------------


def test_every_weight_consumer_uses_the_gross_multiplier():
    position = _pos("SQQQ", 500, 10.0, 12.0)
    total_value = 100_000.0
    expected = 18.0  # 500 * 12 * 3 / 100_000 * 100

    assert position_weight_pct(position, total_value) == pytest.approx(expected)
    # PortfolioConstructor's current-weight map
    weights = PortfolioConstructor._current_weights([position], total_value)
    assert weights["SQQQ"] == pytest.approx(expected)
    # PortfolioManagerAgent._target_intent's current-weight comparison:
    # a target BELOW the held gross weight must classify as a trim. At 18%
    # gross a 10% target is a sell; read raw (6%) it would look like a buy.
    from src.models import TargetPosition

    target = TargetPosition(
        symbol="SQQQ",
        target_weight_pct=10.0,
        direction="long",
        thesis="trim the leveraged sleeve",
    )
    intent = PortfolioManagerAgent._target_intent(
        target,
        {"SQQQ": position},
        total_value,
    )
    assert intent == "sell"
    # `_build_position_facts` (the position reviewer's metric line)
    pipeline = build_pipeline(db=MagicMock(), broker=MagicMock())
    pipeline.db.get_symbol_last_buy.return_value = None
    pipeline.db.get_trades.return_value = []
    pipeline.broker.get_current_stop_price.return_value = None
    pipeline._atr_for_symbol = lambda symbol: None
    facts = pipeline._build_position_facts([position], [], total_value)
    assert facts["SQQQ"]["weight_pct"] == pytest.approx(expected)


def test_position_weight_is_signed_so_a_short_is_not_a_long():
    short = _pos("TSLA", -300, 110.0, 100.0)
    assert position_weight_pct(short, 100_000.0) == pytest.approx(-30.0)
    assert weight_pct_of(-30_000.0, "TSLA", 100_000.0) == pytest.approx(-30.0)


# --------------------------------------------------------------------------
# (3) Unrealized P&L percent — absolute cost basis everywhere.
# --------------------------------------------------------------------------

WINNING_SHORT = _pos("TSLA", -100, 110.0, 100.0)  # +$1,000 profit


def test_a_winning_short_reports_a_positive_pnl_percent():
    """It printed `P&L: $1000.00 (+0.0%)` — self-contradicting on one line."""
    assert WINNING_SHORT.unrealized_pnl == pytest.approx(1000.0)
    assert unrealized_pnl_pct(WINNING_SHORT) == pytest.approx(1000 / 11000 * 100)
    assert unrealized_pnl_pct(WINNING_SHORT) > 0


def test_a_losing_short_reports_a_negative_pnl_percent():
    losing = _pos("TSLA", -100, 100.0, 110.0)  # -$1,000
    assert unrealized_pnl_pct(losing) == pytest.approx(-1000 / 10000 * 100)


def test_every_pnl_pct_consumer_renders_the_same_number():
    expected = 1000 / 11000 * 100  # +9.09%
    total_value = 100_000.0

    agent = PortfolioManagerAgent.__new__(PortfolioManagerAgent)
    pm_line = PortfolioManagerAgent.build_user_message(
        agent,
        analyses=[],
        positions=[WINNING_SHORT],
        cash_balance=50_000.0,
        total_value=total_value,
        macro_analysis=None,
    )
    assert f"({expected:+.1f}%)" in pm_line
    assert "(+0.0%)" not in pm_line

    reviewer = PositionReviewerAgent.__new__(PositionReviewerAgent)
    review_prompt = PositionReviewerAgent.build_user_message(
        reviewer,
        positions=[WINNING_SHORT],
        macro_summary={},
        cash_balance=50_000.0,
        total_value=total_value,
    )
    assert f"({expected:.1f}%)" in review_prompt

    # `_build_position_facts` feeds the same percent into the reviewer's
    # winner flags. A short held one day and up 9% is parabolic; with a
    # sign-flipped denominator it reads as -9% and never flags.
    pipeline = build_pipeline(db=MagicMock(), broker=MagicMock())
    pipeline.db.get_symbol_last_buy.return_value = {
        "timestamp": "2999-01-01 10:00:00",
        "stop_loss": 0,
    }
    pipeline.db.get_trades.return_value = []
    pipeline.broker.get_current_stop_price.return_value = None
    pipeline._atr_for_symbol = lambda symbol: None
    big_winner = _pos("TSLA", -100, 200.0, 100.0)  # +$10,000, +50%
    facts = pipeline._build_position_facts([big_winner], [], total_value)
    assert facts["TSLA"]["parabolic_flag"] is True


def test_pnl_pct_is_none_not_zero_when_unknowable():
    """None means "unknown"; 0.0 would read as "flat" and is a lie."""
    fresh = _pos("NEWCO", 0, 0.0, 100.0)
    assert unrealized_pnl_pct(fresh) is None
    assert unrealized_pnl_pct(object()) is None


# ==========================================================================
# 4. The short-side gap-risk haircut — board item 216
# ==========================================================================
#
# Execution ships `min(qty_by_alloc, qty_by_risk)`. The constructor haircut
# the allocation leg (twice, in two functions) and `_qty_by_risk_budget` in
# `src/pipeline_stages.py` haircut the risk leg with its own copy of the
# multiply and its own fallback literal. Whenever the risk leg was the
# binding one — the ordinary case, not an exotic one — the number that sized
# the live short was the execution-side copy, not the one a reader finds in
# the constructor. The three copies happened to hold the same value, so they
# agreed by configuration rather than by construction, and a change to any
# one of them was silently a half-change.
#
# The single source is `src.risk.constants.gap_adjusted_risk_per_share`,
# whose default is `SHORT_GAP_RISK_MULTIPLE_DEFAULT`. The VALUE is unchanged
# (board item 186 owns whether 1.5 is right); only the number of places that
# apply it changed.

_HAIRCUT_NAMES = {"short_gap_risk_multiple", "gap_multiple"}


def _multiplies_by_the_haircut(node) -> bool:
    """Does this node multiply something by a short gap-risk multiple?"""
    if isinstance(node, ast.AugAssign) and isinstance(node.op, ast.Mult):
        operands = [node.value]
    elif isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mult):
        operands = [node.left, node.right]
    else:
        return False
    for operand in operands:
        if isinstance(operand, ast.Attribute) and operand.attr in _HAIRCUT_NAMES:
            return True
        if isinstance(operand, ast.Name) and operand.id in _HAIRCUT_NAMES:
            return True
        if isinstance(operand, ast.Name) and operand.id == ("SHORT_GAP_RISK_MULTIPLE_DEFAULT"):
            return True
    return False


def test_no_short_gap_haircut_is_applied_anywhere():
    """ZERO places under src/ multiply by a short gap-risk multiple.

    This test used to assert exactly ONE application site. Owner ruling
    2026-10-04 deleted the haircut outright — "a short should be treated
    the same as a long, no different math no different behavior" — so the
    assertion is now zero: the constant, the config fields and the single
    application site are all gone and reintroducing any of them fails here.
    """
    offenders = []
    for path, _source, tree in _shared_ast_cache.parse_tree(SRC):
        for node in ast.walk(tree):
            if _multiplies_by_the_haircut(node):
                offenders.append(f"{path}:{getattr(node, 'lineno', '?')}")
    assert offenders == [], (
        "the short-side gap-risk haircut was DELETED by owner ruling "
        f"2026-10-04; it must not be reapplied anywhere. Found: {offenders}"
    )


def test_the_haircut_names_are_gone_from_src():
    """No constant, no helper, no config field survives the deletion."""
    import src.risk.constants as rc
    from src.config import RiskConfig
    from src.portfolio_constructor.config import ConstructorConfig

    assert not hasattr(rc, "SHORT_GAP_RISK_MULTIPLE_DEFAULT")
    assert not hasattr(rc, "gap_adjusted_risk_per_share")
    assert "short_gap_risk_multiple" not in RiskConfig.model_fields
    assert not hasattr(ConstructorConfig, "short_gap_risk_multiple")
