"""The two things about a revisable take-profit that must never regress.

1. A REVISION CANNOT SWITCH OFF THE EXIT GUARD'S VETO.

   The target is the DENOMINATOR of `thesis_progress_pct`, and `pace` is
   progress over elapsed horizon. Both are in
   `src.risk.exit_guard._HIGHER_IS_BETTER`, and
   `veto_contradicted_exit` only blocks a deterioration claim while
   `MetricDeltas.net_improved` holds — i.e. something improved and NOTHING
   measurably worsened.

   So raising a target mechanically lowers progress and lowers pace, which
   lands both in `worsened`, which clears `net_improved`, which lets a
   "this position is stalling" SELL through that was previously blocked. A
   revisable target built without fixing the measurement side is a machine
   for making winners look stalled and then selling them.

   The fix chosen is to measure progress and pace against the PINNED entry
   target (`trades.initial_take_profit`) rather than the live one. These
   tests pin it end to end: the arithmetic, and the guard's actual verdict.

2. NOTHING AUTOMATICALLY EXITS AT THE TARGET, and the revision path did not
   change that. The automatic trim was deleted in PR #321;
   `tests/test_pipeline.py::test_no_fixed_gain_automatic_profit_trim_exists`
   pins the general rule, and the checks here pin it specifically for the
   revision module and for the flag schema.
"""

import ast
import inspect
import textwrap
import pathlib
import re

import pytest

from src.data.levels import CLUSTER_TOLERANCE_PCT, COVERAGE_MEASURED
from src.models import TargetRevisionFlag
from tests.pipeline_factory import build_pipeline
from src.risk import target_revision as tr
from src.risk.exit_guard import (
    _HIGHER_IS_BETTER,
    BREAK_CONFIRMATION_ATR_MULTIPLE,
    compute_deltas,
    veto_contradicted_exit,
)

STALL_REASON = "position is stalling, no progress against thesis"


def _progress(entry: float, current: float, target: float) -> float:
    """The pipeline's own progress arithmetic (`_build_position_facts`)."""
    return (current - entry) / (target - entry) * 100


def _pace(progress_pct: float, days_held: float, horizon: int) -> float:
    return progress_pct / ((days_held / horizon) * 100)


# ---------------------------------------------------------------------------
# 1. A revision cannot switch off the veto
# ---------------------------------------------------------------------------


def test_raising_the_live_target_would_worsen_both_guarded_metrics():
    """The defect itself, stated as arithmetic, so the reason for the fix
    cannot quietly stop being true.

    Good news: entry 100, target derived at 110, price runs to 108, the
    resistance the target sat on is gone and the target re-derives to 125.
    Measured against the LIVE target, the position instantly looks less
    progressed AND slower than it did an hour earlier.
    """
    entry, current_before, current_after = 100.0, 106.0, 108.0
    old_target, new_target = 110.0, 125.0
    horizon = 10

    before = _progress(entry, current_before, old_target)
    after_live = _progress(entry, current_after, new_target)
    assert after_live < before, (
        "raising the target must lower progress — if this ever stops being "
        "true the premise of this whole module has changed"
    )
    assert _pace(after_live, 5, horizon) < _pace(before, 4, horizon)

    # And both metrics are ones the guard reads directionally.
    assert "thesis_progress_pct" in _HIGHER_IS_BETTER
    assert "pace" in _HIGHER_IS_BETTER


def test_live_target_denominator_switches_the_veto_off():
    """Demonstrates the danger against the REAL guard, not a model of it.

    This is the behaviour the fix exists to prevent. If this test ever
    starts passing a veto, the guard's semantics changed and the pinning
    below needs rechecking.
    """
    entry = 100.0
    prior = {
        "thesis_progress_pct": _progress(entry, 106.0, 110.0),
        "pace": _pace(_progress(entry, 106.0, 110.0), 4, 10),
    }
    # Same position, one hour later, better price — but measured against the
    # newly RAISED target.
    current = {
        "thesis_progress_pct": _progress(entry, 108.0, 125.0),
        "pace": _pace(_progress(entry, 108.0, 125.0), 5, 10),
    }
    deltas = compute_deltas("AAPL", prior, current)
    assert deltas.worsened, "the revision should look like deterioration here"
    assert not deltas.net_improved
    assert veto_contradicted_exit("SELL", STALL_REASON, deltas) is None, (
        "with a live-target denominator the guard stops protecting the "
        "position — this is the machine for selling winners"
    )


def test_pinned_target_denominator_keeps_the_veto_on():
    """The fix. Progress and pace measured against the PINNED entry target
    are unaffected by the revision, so the same SELL stays vetoed."""
    entry, pinned = 100.0, 110.0
    prior = {
        "thesis_progress_pct": _progress(entry, 106.0, pinned),
        "pace": _pace(_progress(entry, 106.0, pinned), 4, 10),
    }
    current = {
        "thesis_progress_pct": _progress(entry, 108.0, pinned),
        "pace": _pace(_progress(entry, 108.0, pinned), 5, 10),
    }
    deltas = compute_deltas("AAPL", prior, current)
    assert not deltas.worsened
    assert deltas.net_improved
    veto = veto_contradicted_exit("SELL", STALL_REASON, deltas)
    assert veto is not None and "vetoed" in veto


def test_a_revision_alone_moves_neither_guarded_metric_at_all():
    """Stronger than "the veto survives": with the denominator pinned, a
    revision on an otherwise unchanged position produces NO delta in either
    guarded metric, so it cannot register as improvement OR deterioration.
    Prevention by construction rather than by a special case."""
    entry, pinned, price = 100.0, 110.0, 108.0
    snapshot = {
        "thesis_progress_pct": _progress(entry, price, pinned),
        "pace": _pace(_progress(entry, price, pinned), 5, 10),
    }
    # The live target moved from 110 to 125; nothing else did.
    deltas = compute_deltas("AAPL", dict(snapshot), dict(snapshot))
    assert deltas.improved == [] and deltas.worsened == []


def test_pipeline_measures_progress_against_the_pinned_target():
    """Pins the wiring, not just the arithmetic: `_build_position_facts`
    must divide by `initial_take_profit`, never by the mutable
    `take_profit`. A regression here reintroduces the defect silently."""
    from src.pipeline_prompt_facts import PromptPositionFacts
    from src.pipeline import TradingPipeline

    src = inspect.getsource(PromptPositionFacts._build_position_facts)
    assert "progress_target = float(" in src
    assert '"initial_take_profit"' in src
    assert "(cur - entry) / (progress_target - entry)" in src
    assert "(cur - entry) / (take_profit - entry)" not in src, (
        "progress is being measured against the MUTABLE target again — a "
        "revision can now switch the exit guard's veto off"
    )
    # The snapshot the next review compares against must not carry a target.
    assert "take_profit" not in TradingPipeline._REVIEW_METRIC_KEYS
    assert "entry_take_profit" not in TradingPipeline._REVIEW_METRIC_KEYS


def test_pinned_target_column_is_never_written_by_a_revision():
    """`update_open_take_profit` writes the live target only. If it ever
    touched `initial_take_profit` the pinned denominator would move with the
    revision and the guard would be exposed again."""
    from src.storage.trades.ledger import TradeLedger

    src = inspect.getsource(TradeLedger.update_open_take_profit)
    assert "SET take_profit" in src
    assert "initial_take_profit" not in src.split('"""')[2], (
        "the revision write-back must not touch the pinned entry target"
    )


def _code_without_docstrings(module) -> str:
    """The module's CODE, with every docstring removed, resolved by IMPORT.

    Replaces a `read_text().split('\"\"\"', 2)[2]` slice that assumed the
    module opened with exactly one triple-quoted docstring: reflowing the
    header or adding a second one silently changed what the guard looked at,
    and moving the module broke the path. `inspect.getsource` follows the
    symbol wherever the file goes, and the AST removes prose properly, so the
    assertions below are about code and nothing else.
    """
    tree = ast.parse(inspect.getsource(module))
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if not isinstance(body, list) or not body:
            continue
        if not isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        first = body[0]
        if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) and isinstance(first.value.value, str):
            if len(body) == 1:
                body[0] = ast.Pass()
            else:
                del body[0]
    ast.fix_missing_locations(tree)
    return ast.unparse(tree)


def test_the_docstring_stripper_actually_strips_and_keeps_the_code():
    """Canary: a stripper that returned nothing would make the guards green."""
    body = _code_without_docstrings(tr)
    assert len(body) > 500, f"the module body came back near-empty: {body!r}"
    assert "def assess_target_revision" in body, "the stripped body no longer contains the module's own functions"
    assert "Honest limitation" not in body


# ---------------------------------------------------------------------------
# 2. No automatic exit at the target
# ---------------------------------------------------------------------------


def test_revision_module_places_no_orders_and_triggers_no_exit():
    """The revision path adjusts a measurement. It must never sell, trim,
    cover or submit anything — the trailing stop remains the only automatic
    exit (PR #321, 2026-09-12)."""
    # Parsed, not grepped: every docstring in this module legitimately
    # DISCUSSES exits, so a text search would only ever find prose. What
    # matters is whether the CODE names an exit.
    tree = ast.parse(inspect.getsource(tr))
    docstrings = {
        id(node.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str)
    }
    identifiers: set[str] = set()
    literals: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            identifiers.add(node.id)
        elif isinstance(node, ast.Attribute):
            identifiers.add(node.attr)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            if id(node) not in docstrings:
                literals.add(node.value)

    forbidden_calls = {
        "submit_order",
        "place_order",
        "close_position",
        "liquidate",
        "_auto_take_profit",
        "update_open_take_profit",
        "record_target_revision",
        "get_ohlcv",
    }
    assert not (identifiers & forbidden_calls), f"the revision module reaches out: {identifiers & forbidden_calls}"
    # No exit ACTION word appears as a live string constant anywhere.
    exit_words = re.compile(r"\b(SELL|REDUCE|COVER|TARGET_BREACH|TAKE_PROFIT)\b")
    offending = sorted(lit for lit in literals if exit_words.search(lit))
    assert offending == [], f"exit action word(s) in live code: {offending}"
    # Pure: no broker, DB, market-data or LLM dependency is even imported.
    imported = {
        alias.name for node in ast.walk(tree) if isinstance(node, (ast.Import, ast.ImportFrom)) for alias in node.names
    } | {node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
    for banned in ("broker", "storage", "db", "anthropic", "openai", "agents"):
        assert not any(banned in name for name in imported), f"{banned!r} reached by {sorted(imported)}"


def test_flag_schema_cannot_carry_a_typed_target_price():
    """A revision is a RE-DERIVATION. The seat supplies evidence; the code
    supplies the number. A model-typed target would be board item 80 (stop
    provenance) reappearing on the field that feeds pace and the guard."""
    assert set(TargetRevisionFlag.model_fields) == {"symbol", "evidence"}
    # A price typed anyway is dropped, not stored, and is unreachable.
    flag = TargetRevisionFlag(
        symbol="aapl",
        evidence="gapped through and closed above 214",
        target_price=999.0,
        take_profit=999.0,
        new_target=999.0,
    )
    assert flag.model_dump() == {
        "symbol": "AAPL",
        "evidence": "gapped through and closed above 214",
    }
    for attr in ("target_price", "take_profit", "new_target"):
        assert getattr(flag, attr, "ABSENT") == "ABSENT"


# ---------------------------------------------------------------------------
# Trigger discipline — a revision needs a structural event
# ---------------------------------------------------------------------------

_COMMON = dict(
    symbol="AAPL",
    direction="long",
    entry_price=100.0,
    pinned_horizon_sessions=10,
    setup_type=None,
    levels_coverage=COVERAGE_MEASURED,
)


def test_an_opinion_is_refused_by_name_not_silently_ignored():
    # Entry 100, ATR 2.5, horizon 10: reach 11.86, noise floor 2.50 — a
    # target at 110 is exactly what the derivation produces, and today's
    # bars change nothing. Price is up but the ceiling is intact.
    out = tr.assess_target_revision(
        stored_target=110.0,
        target_level=110.0,
        levels=[110.0, 95.0],
        atr=2.5,
        close_price=104.0,
        break_seen_prior_close=False,
        **_COMMON,
    )
    assert not out.revised
    assert out.code == tr.REVISION_NO_TRIGGER
    assert out.refusal and not out.fault
    assert out.detail  # never a blank


def test_a_one_day_break_is_pending_confirmation_not_a_revision():
    """Same two-consecutive-closes gate as
    `exit_guard.check_structural_protection` — a one-day spring is not a
    break."""
    out = tr.assess_target_revision(
        stored_target=110.0,
        target_level=110.0,
        levels=[110.0, 128.0, 95.0],
        # The gap expands ATR, as a real one does.
        atr=6.0,
        # A close more than one noise band above the broken level.
        close_price=118.0,
        break_seen_prior_close=False,
        **_COMMON,
    )
    assert out.code == tr.REVISION_BREAK_PENDING_CONFIRMATION
    assert out.new_price is None


def test_a_confirmed_level_break_re_derives_on_todays_bars():
    # The owner's motivating case, end to end. Entry 100 with a target on
    # the 110 resistance; the name gaps through on earnings and closes 118
    # with ATR expanded 2.5 -> 6.0, which widens `horizon_reach` from 11.86
    # to 28.46 and brings the next level up, 128, into reach.
    out = tr.assess_target_revision(
        stored_target=110.0,
        target_level=110.0,
        levels=[110.0, 128.0, 95.0],
        atr=6.0,
        close_price=118.0,
        break_seen_prior_close=True,
        **_COMMON,
    )
    assert out.revised
    assert out.trigger == tr.TRIGGER_LEVEL_BROKEN
    assert out.new_price == pytest.approx(128.0)
    assert out.basis == "structural_level"


def test_the_re_derivation_reuses_the_pinned_horizon_and_never_recomputes():
    """`derive_structural_target` refuses without a horizon, and the horizon
    is pinned at BUY. A position with no pinned horizon is refused by name
    rather than having one guessed for it."""
    args = dict(_COMMON)
    args["pinned_horizon_sessions"] = None
    out = tr.assess_target_revision(
        stored_target=110.0,
        target_level=110.0,
        levels=[110.0, 128.0],
        atr=6.0,
        close_price=118.0,
        break_seen_prior_close=True,
        **args,
    )
    assert out.code == tr.REVISION_NO_PINNED_HORIZON
    assert out.new_price is None
    # And the module never reaches for a horizon of its own. Since item 114
    # it may re-anchor on the REMAINING horizon, but every part of that is
    # READ: the pin comes in as an argument and the sessions already used
    # come in as `sessions_held`, computed by the caller with the desk's one
    # holiday-aware counter. This module still calls no calendar, no broker
    # and no session counter itself, and still never converts a calendar day
    # into a session.
    body = _code_without_docstrings(tr)
    assert "trading_sessions_held(" not in body
    assert "trading_calendar" not in body
    assert "days_held" not in body
    assert (
        "sessions_held"
        in inspect.signature(
            tr.assess_target_revision,
        ).parameters
    )


def test_the_atr_trigger_introduces_no_new_constant():
    """ "ATR changed enough" is read off the derivation's OWN acceptance
    bounds — the noise floor and `horizon_reach`. A bare numeric literal
    used as a threshold here would be an arbitrary number on the live risk
    path (docs/WORK.md, no-arbitrary-numbers)."""
    body = _code_without_docstrings(tr)
    signature = inspect.signature(tr.stale_reach_trigger)
    defaults = {name: p.default for name, p in signature.parameters.items() if p.default is not inspect.Parameter.empty}
    # Every bound comes from src.data.levels, not from a literal here.
    from src.data import levels as lv

    assert defaults["min_target_atr_multiple"] == lv.MIN_TARGET_ATR_MULTIPLE
    assert defaults["max_reach_atr_multiple"] == lv.MAX_REACH_ATR_MULTIPLE
    assert defaults["max_horizon_sessions"] == lv.MAX_HORIZON_SESSIONS
    # No float literal other than 0/100.0-style arithmetic scaffolding.
    # Resolved through the SYMBOL, not by slicing the file between two
    # `def` strings: reordering the module no longer changes what is checked.
    body_after_helpers = _code_without_docstrings(tr.stale_reach_trigger)
    assert "def stale_reach_trigger" in body_after_helpers
    literals = re.findall(r"(?<![\w.])\d+\.\d+(?![\w.])", body_after_helpers)
    assert literals == [], f"invented threshold literal(s): {literals}"


def test_a_target_now_beyond_todays_reach_is_a_trigger():
    """Volatility collapsed, so the stored target is no longer reachable
    inside the pinned horizon — the derivation would not accept it today."""
    trigger = tr.stale_reach_trigger(
        entry_price=100.0,
        stored_target=140.0,
        atr=0.5,
        horizon_sessions=10,
    )
    assert trigger == tr.TRIGGER_TARGET_BEYOND_REACH


def test_a_target_inside_todays_noise_floor_is_no_longer_a_trigger():
    """**Inverted 2026-09-30.** This used to assert
    `TRIGGER_TARGET_INSIDE_NOISE` on the reasoning that a target inside the
    daily range is one the derivation would no longer accept.

    It would. The noise floor stopped being an acceptance test in
    `derive_structural_target` when it was found to be deleting walls and
    promoting targets past them; it only labels the result now. Re-deriving
    here returned the identical price and the outcome was
    `REVISION_NO_CHANGE` every session — a trigger whose premise was always
    false. Reach is the one remaining test and is unaffected."""
    assert (
        tr.stale_reach_trigger(
            entry_price=100.0,
            stored_target=101.0,
            atr=8.0,
            horizon_sessions=10,
        )
        == ""
    )


def test_an_unchanged_target_under_unchanged_volatility_is_not_a_trigger():
    # ATR 2.5 over 10 sessions: noise floor 2.50, reach 11.86. A target 10
    # away from entry sits inside both, which is why the derivation picked
    # it in the first place.
    assert (
        tr.stale_reach_trigger(
            entry_price=100.0,
            stored_target=110.0,
            atr=2.5,
            horizon_sessions=10,
        )
        == ""
    )


def test_a_data_fault_is_recorded_as_a_fault_not_a_refusal():
    """The desk's own split: a missing measurable input is a FAULT, a real
    geometry judgement is a REFUSAL. They must never be confused."""
    out = tr.assess_target_revision(
        stored_target=110.0,
        target_level=110.0,
        levels=[110.0],
        atr=None,
        close_price=None,
        break_seen_prior_close=True,
        **_COMMON,
    )
    assert out.code == tr.REVISION_UNMEASURABLE_INPUTS
    assert out.fault and not out.refusal


def test_a_refused_re_derivation_leaves_the_old_target_standing():
    """A trigger fired, but the close has cleared EVERY level on the chart,
    so today's bars hold nothing left in the trade's direction. The old
    target stands, under a code that does not claim the chart is empty."""
    out = tr.assess_target_revision(
        stored_target=110.0,
        target_level=110.0,
        levels=[110.0, 95.0, 90.0],
        atr=6.0,
        close_price=118.0,
        break_seen_prior_close=True,
        **_COMMON,
    )
    assert out.new_price is None
    assert out.prior_price == pytest.approx(110.0)
    assert out.code == tr.REVISION_NO_CEILING_LEFT
    assert out.trigger == tr.TRIGGER_LEVEL_BROKEN
    assert out.detail


def test_a_broken_level_is_never_handed_back_as_the_new_target():
    """`find_structural_levels` keeps returning a ceiling price has gapped
    through, and `derive_structural_target` partitions against ENTRY — so
    without `levels_still_in_the_way` the re-derivation re-picks the very
    level that just broke and the revision is a no-op in exactly the case
    it exists for."""
    surviving = tr.levels_still_in_the_way(
        computed_levels=[110.0, 128.0, 95.0],
        close_price=118.0,
        atr=6.0,
        is_short=False,
    )
    assert surviving == [128.0]
    # A short's cleared floors drop the other way: with a close of 88 and a
    # 6.0 band, 95 has been fallen through (88 <= 89) and 90 has not.
    assert tr.levels_still_in_the_way(
        computed_levels=[95.0, 90.0],
        close_price=88.0,
        atr=6.0,
        is_short=True,
    ) == [90.0]
    # Missing inputs must not silently empty the set.
    assert tr.levels_still_in_the_way(
        computed_levels=[110.0, 95.0],
        close_price=None,
        atr=6.0,
        is_short=False,
    ) == [110.0, 95.0]


def test_a_target_the_price_has_already_passed_is_refused_not_stored():
    """The honest cost of holding entry and horizon fixed: reach is measured
    from entry, so after a run the only derivable target can be behind the
    price. Refused by name; the old target stands."""
    out = tr.assess_target_revision(
        stored_target=110.0,
        target_level=110.0,
        # The 110 ceiling is broken (close 117 is a full 6.0 noise band
        # above it) so the trigger fires; 116 survives the filter and is
        # well inside reach of entry — but the close is already past it.
        levels=[110.0, 116.0],
        atr=6.0,
        close_price=117.0,
        break_seen_prior_close=True,
        **_COMMON,
    )
    assert out.code == tr.REVISION_BEHIND_PRICE
    assert out.new_price is None
    assert out.prior_price == pytest.approx(110.0)


def test_shorts_break_their_target_level_downward():
    """Direction is the mirror image, not a long-only rule with a patch."""
    assert (
        tr.target_level_broken(
            target_level=90.0,
            close_price=90.0 - 2.5 * BREAK_CONFIRMATION_ATR_MULTIPLE,
            atr=2.5,
            is_short=True,
        )
        is True
    )
    assert (
        tr.target_level_broken(
            target_level=90.0,
            close_price=90.0 - 2.5 * BREAK_CONFIRMATION_ATR_MULTIPLE,
            atr=2.5,
            is_short=False,
        )
        is False
    )
    # Unanswerable rather than "not broken" when an input is missing.
    assert (
        tr.target_level_broken(
            target_level=None,
            close_price=100.0,
            atr=2.5,
            is_short=False,
        )
        is None
    )


def test_a_measured_move_target_has_no_level_to_break():
    """`level_backing_target` returns None when nothing sits in the stored
    target's own cluster zone — correct for a measured-move target, which
    was never measured against a level."""
    assert (
        tr.level_backing_target(
            stored_target=110.0,
            computed_levels=[95.0, 128.0],
            level_cluster_tolerance_pct=CLUSTER_TOLERANCE_PCT,
        )
        is None
    )
    assert tr.level_backing_target(
        stored_target=110.0,
        computed_levels=[95.0, 110.2, 128.0],
        level_cluster_tolerance_pct=CLUSTER_TOLERANCE_PCT,
    ) == pytest.approx(110.2)


def test_every_outcome_carries_a_machine_code():
    """A refusal is a first-class outcome. No path returns a blank."""
    for kwargs in (
        dict(stored_target=None, target_level=None, levels=[], atr=2.5, close_price=104.0),
        dict(stored_target=110.0, target_level=110.0, levels=[110.0], atr=2.5, close_price=104.0),
        dict(stored_target=110.0, target_level=110.0, levels=[110.0], atr=None, close_price=None),
    ):
        out = tr.assess_target_revision(break_seen_prior_close=False, **kwargs, **_COMMON)
        assert out.code, f"blank outcome for {kwargs}"
        assert out.detail, f"blank detail for {kwargs}"


# ---------------------------------------------------------------------------
# Item 82 — target revision is ALREADY measurement-only; the analyst's
# setup_type label does not route it.
#
# An adversary flagged `assess_target_revision` as a second label-only
# money-path reader alongside the trailing reader (both persist the same row).
# It is NOT: the label reaches only `derive_structural_target`, and the funnel
# item-6 fix (2026-09-11) already made that function decide breakout-vs-range
# from the MEASURED chart (`nearest is None` on today's levels), not the word
# "breakout" — see `src/data/levels.py` ~line 1144. So a measured breakout the
# analyst mislabelled "range" ALREADY gets breakout treatment here, and no
# `structural_ceiling` needs threading through this path. These pin that: all
# three setup_type values produce a BYTE-IDENTICAL outcome, in both a revising
# and a refusing scenario. If a future change makes this path key off the
# label again, one of these fails and the drift is caught.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("setup_type", ["range", "breakout", None])
def test_item82_revision_outcome_is_independent_of_the_setup_type_label(setup_type):
    """A confirmed level break re-derives to the next level (128) regardless of
    whether the analyst typed range, breakout, or nothing — the label never
    routes the re-derivation."""
    args = dict(_COMMON)
    args["setup_type"] = setup_type
    out = tr.assess_target_revision(
        stored_target=110.0,
        target_level=110.0,
        levels=[110.0, 128.0, 95.0],
        atr=6.0,
        close_price=118.0,
        break_seen_prior_close=True,
        **args,
    )
    assert out.revised
    assert out.trigger == tr.TRIGGER_LEVEL_BROKEN
    assert out.new_price == pytest.approx(128.0)
    assert out.basis == "structural_level"


@pytest.mark.parametrize("setup_type", ["range", "breakout", None])
def test_item82_refusal_is_independent_of_the_setup_type_label(setup_type):
    """With the chart's structure all closed decisively through, the flag is
    refused under one machine code for every label — the mislabelled-range case
    is not routed to some different, softer refusal."""
    args = dict(_COMMON)
    args["setup_type"] = setup_type
    out = tr.assess_target_revision(
        stored_target=110.0,
        target_level=110.0,
        levels=[95.0, 90.0],
        atr=6.0,
        close_price=118.0,
        break_seen_prior_close=True,
        **args,
    )
    assert not out.revised
    assert out.new_price is None
    assert out.code == tr.REVISION_NO_CEILING_LEFT


# ---------------------------------------------------------------------------
# Item 114 — the ONE re-anchor: the latest close over the REMAINING horizon
#
# Every case below runs the same strong winner: entry $100, a target on the
# $110 ceiling, ATR expanded to 6.0 on a confirmed break, the close at $117,
# and levels at 110 / 116 / 130. From the pinned entry over the pinned
# 10-session horizon the reach is $28.46, so $130 is out of reach and the only
# acceptable level, $116, sits BEHIND the close — the refusal the item was
# filed about. What differs between the cases is only how much of the horizon
# the position has already spent.
# ---------------------------------------------------------------------------

_RUNNER = dict(
    stored_target=110.0,
    target_level=110.0,
    levels=[110.0, 116.0, 130.0],
    atr=6.0,
    close_price=117.0,
    break_seen_prior_close=True,
)


def _runner(**overrides):
    args = dict(_COMMON)
    args.update(_RUNNER)
    args.update(overrides)
    return tr.assess_target_revision(**args)


def test_remaining_horizon_is_read_from_the_pin_and_the_sessions_used():
    """No new number: pinned horizon minus holiday-aware sessions held."""
    assert (
        tr.remaining_horizon_sessions(
            pinned_horizon_sessions=10,
            sessions_held=4,
        )
        == 6
    )
    # Spent, and floored at zero — a position past its horizon has no reach
    # left, not negative reach.
    assert (
        tr.remaining_horizon_sessions(
            pinned_horizon_sessions=10,
            sessions_held=14,
        )
        == 0
    )
    # Unreadable inputs return None so the caller refuses rather than
    # inventing a remaining horizon.
    for bad in (
        {"pinned_horizon_sessions": None, "sessions_held": 4},
        {"pinned_horizon_sessions": 10, "sessions_held": None},
        {"pinned_horizon_sessions": "x", "sessions_held": 4},
        {"pinned_horizon_sessions": 0, "sessions_held": 4},
    ):
        assert tr.remaining_horizon_sessions(**bad) is None


def test_a_strong_winner_is_no_longer_refused_purely_for_having_run():
    """The item's own case. Half the horizon is left, a real level is still
    in the way ahead of the close, and the target extends to it."""
    out = _runner(sessions_held=5)
    assert out.revised
    assert out.new_price == pytest.approx(130.0)
    assert out.basis == tr.REANCHORED_BASIS
    assert out.level_used == pytest.approx(130.0)
    # The record says which anchor produced the number.
    assert "session(s) of the pinned horizon this position has left" in out.detail


def test_the_reanchor_is_measured_from_the_remaining_horizon_not_the_price():
    """The load-bearing distinction. Re-anchoring on the current price ALONE
    would regenerate the full 10-session reach ($28.46 from $117, i.e. up to
    $145) every time price moved, and would pick up $130 whatever the
    position's age. Measured from the REMAINING horizon the reach shrinks as
    the position spends it: with one session left it is $9.00, $130 is out of
    reach, and the refusal stands."""
    assert _runner(sessions_held=9).code == tr.REVISION_BEHIND_PRICE
    # ... and the same chart with more of the horizon left does extend, so
    # the difference is the remaining horizon and nothing else.
    assert _runner(sessions_held=5).new_price == pytest.approx(130.0)


def test_the_refusal_still_stands_when_the_remaining_horizon_is_unreadable():
    """No sessions-held count means no remaining horizon, and it is never
    assumed — the pre-existing refusal fires exactly as before."""
    out = _runner(sessions_held=None)
    assert out.code == tr.REVISION_BEHIND_PRICE
    assert out.new_price is None
    assert out.prior_price == pytest.approx(110.0)
    assert "never assumed" in out.detail


def test_a_position_that_has_spent_its_horizon_cannot_reanchor():
    out = _runner(sessions_held=10)
    assert out.code == tr.REVISION_BEHIND_PRICE
    assert out.new_price is None
    assert "no remaining horizon" in out.detail


def test_the_reanchor_refuses_a_measured_move_off_the_current_close():
    """A projection from the close exists whatever the chart looks like, so
    accepting one would be the forbidden price-chase in disguise. Only a
    structural level still in the way is accepted."""
    out = _runner(sessions_held=5, levels=[110.0, 116.0])
    assert out.code == tr.REVISION_BEHIND_PRICE
    assert out.new_price is None
    assert "not a level still in the way" in out.detail


def test_the_reanchor_never_pulls_the_target_back_toward_entry():
    """A revision may extend a target, never weaken it. Here the only level
    ahead of the close sits closer to entry than the stored target would be
    for a SHORT, so the refusal stands."""
    args = dict(_COMMON)
    args["direction"] = "short"
    args["entry_price"] = 100.0
    out = tr.assess_target_revision(
        stored_target=90.0,
        target_level=90.0,
        # Mirror image: the 90 floor is broken downward (close 83 is a full
        # noise band below it), and the only level left below the close is
        # 82.5 — ahead of the close but NOT further from entry than a stored
        # target of 90 would... it is, so use a level that is not: none
        # below the close at all leaves the measured-move path.
        levels=[90.0, 82.5],
        atr=6.0,
        close_price=83.0,
        break_seen_prior_close=True,
        sessions_held=5,
        **args,
    )
    assert out.new_price is None or out.new_price < 83.0


def test_the_structural_level_basis_string_still_matches_the_derivation():
    """The re-anchor accepts one basis and one only, and that string is
    written literally by `derive_structural_target`. If the derivation ever
    renames it, this fails rather than the re-anchor silently refusing
    everything."""
    from src.data.levels import COVERAGE_MEASURED as _COV
    from src.data.levels import derive_structural_target

    derived = derive_structural_target(
        entry_price=100.0,
        direction="long",
        levels=[110.0],
        atr=2.5,
        horizon_sessions=10,
        setup_type=None,
        levels_coverage=_COV,
    )
    assert derived.price == pytest.approx(110.0)
    assert derived.basis == tr.STRUCTURAL_LEVEL_BASIS


def test_the_reanchor_cannot_fire_without_a_structural_trigger():
    """It is a fallback INSIDE the re-derivation, not a second way in. An
    opinion with no structural event behind it is still refused up front,
    however much horizon is left."""
    out = _runner(
        sessions_held=1,
        atr=2.5,
        close_price=104.0,
        target_level=110.0,
        break_seen_prior_close=False,
        levels=[110.0, 95.0],
    )
    assert out.code == tr.REVISION_NO_TRIGGER
    assert out.new_price is None


# ---------------------------------------------------------------------------
# Trigger 3 — a wall that now stands between the entry and the stored target
#
# The mirror of TRIGGER_LEVEL_BROKEN. Same entry ($100) and same 10-session
# horizon as everything above; what differs is that the chart has grown a
# level BETWEEN the entry and the number frozen on the row.
# ---------------------------------------------------------------------------


def test_a_wall_in_front_of_the_stored_target_is_a_trigger():
    """Entry 100, stored target 110, and today's bars carry a level at 105
    that the 101 close has not cleared. The target aims past a standing
    wall, and the re-derivation returns the wall."""
    out = tr.assess_target_revision(
        stored_target=110.0,
        target_level=110.0,
        levels=[105.0, 110.0],
        atr=2.5,
        close_price=101.0,
        break_seen_prior_close=False,
        # The wall was already in the way on the PRIOR close (item 194's
        # brake); without that this is a one-session reading, which the
        # dedicated test below pins as a refusal.
        wall_seen_prior_close=True,
        **_COMMON,
    )
    assert out.trigger == tr.TRIGGER_WALL_IN_FRONT_OF_TARGET
    assert out.revised
    assert out.new_price == pytest.approx(105.0)
    assert out.basis == tr.STRUCTURAL_LEVEL_BASIS


def test_the_old_triggers_alone_would_have_missed_that_chart():
    """The load-bearing claim. On the identical inputs neither existing
    trigger can see the new wall: the level the target sat on is intact, and
    a pivot forming mid-way moves no ATR, so reach is unchanged."""
    assert (
        tr.target_level_broken(
            target_level=110.0,
            close_price=101.0,
            atr=2.5,
            is_short=False,
        )
        is False
    )
    assert (
        tr.stale_reach_trigger(
            entry_price=100.0,
            stored_target=110.0,
            atr=2.5,
            horizon_sessions=10,
        )
        == ""
    )


def test_a_target_sitting_on_its_own_wall_is_not_a_finding():
    """Strict inequality. The level AT the target is the target's own wall,
    which is the correct derivation, not something to revise."""
    out = tr.assess_target_revision(
        stored_target=110.0,
        target_level=110.0,
        levels=[110.0, 95.0],
        atr=2.5,
        close_price=101.0,
        break_seen_prior_close=False,
        **_COMMON,
    )
    assert out.code == tr.REVISION_NO_TRIGGER
    assert out.new_price is None


def test_a_wall_the_close_has_broken_through_is_not_a_wall():
    """`levels_still_in_the_way` runs first, so a level the close has
    cleared by a noise band cannot manufacture a trigger. Close 109 with
    ATR 2.5 clears 105 by a full BREAK_CONFIRMATION_ATR_MULTIPLE."""
    out = tr.assess_target_revision(
        stored_target=110.0,
        target_level=110.0,
        levels=[105.0, 110.0],
        atr=2.5,
        close_price=108.0,
        break_seen_prior_close=False,
        **_COMMON,
    )
    assert out.code == tr.REVISION_NO_TRIGGER


def test_the_wall_test_is_anchored_on_entry_not_on_the_current_price():
    """A target must never become a function of the price move. A level
    BELOW the entry is behind the position, not in front of the target, and
    no amount of price movement makes it a wall."""
    assert (
        tr.walls_between(
            stored_target=110.0,
            reference_price=100.0,
            surviving_levels=[95.0, 99.9],
            is_short=False,
        )
        == []
    )
    assert tr.walls_between(
        stored_target=110.0,
        reference_price=100.0,
        surviving_levels=[102.0, 107.0, 110.0, 115.0],
        is_short=False,
    ) == [102.0, 107.0]


def test_the_wall_test_mirrors_for_a_short():
    """A short's target sits below entry, so its wall is a floor between
    the two, and the nearest one comes first."""
    assert tr.walls_between(
        stored_target=90.0,
        reference_price=100.0,
        surviving_levels=[85.0, 93.0, 97.0, 100.0],
        is_short=True,
    ) == [97.0, 93.0]


def test_the_new_trigger_cannot_substitute_a_number_when_it_refuses():
    """`REVISION_BEHIND_PRICE` is a correct answer, not a gap to fill. The
    $110 wall still stands in front of the stored $120, but the only target
    derivable from the pinned $100 entry is that same $110, which the $113
    close has passed — and the horizon is spent, so there is nothing to
    re-anchor on either."""
    out = tr.assess_target_revision(
        stored_target=120.0,
        target_level=120.0,
        levels=[110.0, 120.0],
        atr=5.0,
        close_price=113.0,
        break_seen_prior_close=False,
        wall_seen_prior_close=True,
        sessions_held=10,
        **_COMMON,
    )
    assert out.trigger == tr.TRIGGER_WALL_IN_FRONT_OF_TARGET
    assert out.new_price is None
    assert out.code == tr.REVISION_BEHIND_PRICE


# ---------------------------------------------------------------------------
# item 194 — THE WAY IN. The re-derivation used to run only on a symbol a
# seat had raised a flag for, so a position that quietly grew a wall between
# its entry and its stored target was never re-measured. These tests pin the
# unconditional sweep: EVERY open position is adjudicated every session, and
# a seat flag now only supplies the seat label and the prose evidence.
# ---------------------------------------------------------------------------


class _SweepPos:
    def __init__(self, symbol, qty, avg_entry):
        self.symbol = symbol
        self.qty = qty
        self.avg_entry = avg_entry


class _SweepDB:
    def __init__(self):
        self.recorded = []
        self.take_profit_writes = []

    def get_symbol_last_buy(self, symbol, action=None):
        return {
            "take_profit": 100.0,
            "expected_horizon_sessions": 20,
            "setup_type": "range",
            "timestamp": "2026-09-01T00:00:00",
        }

    def get_prior_target_level_break(self, symbols, **kwargs):
        return {}

    def save_target_level_break(self, **kwargs):
        return None

    def update_open_take_profit(self, symbol, price, action=None):
        self.take_profit_writes.append((symbol, price))
        return True

    def record_target_revision(self, **kwargs):
        self.recorded.append(kwargs)
        return len(self.recorded)


class _SweepMarket:
    def set_fallback_bars(self, fn):
        pass  # the real constructor wires broker bars in

    def get_ohlcv_batch(self, symbols, lookback_days):
        # Batches fine, carries nothing — the normal, non-degraded shape
        # for these tests, so the serial-fallback record stays off.
        return {}

    def get_ohlcv(self, symbol, lookback_days):
        # A dead feed. The point of these tests is the WAY IN, not the
        # derivation arithmetic, which `assess_target_revision`'s own tests
        # above already pin; an unreadable chart must still produce a
        # durable, named outcome rather than a blank.
        return []


class _SweepBroker:
    def get_bars(self, *args, **kwargs):
        return []  # handed to the market as its fallback

    def trading_sessions_held(self, start, end):
        return 10


def _sweep_pipeline():
    import types

    p = build_pipeline(market=_SweepMarket(), broker=_SweepBroker())
    p.db = _SweepDB()  # a recording double with no initialize(); set after construction
    p.config = types.SimpleNamespace(trading=types.SimpleNamespace(lookback_days=400))
    p.risk_engine = None
    return p


class _SweepReview:
    def __init__(self, flags=()):
        self.target_revision_flags = list(flags)


def test_every_open_position_is_adjudicated_with_no_seat_flag():
    """The residue of item 194: no flag, two open positions, two durable
    outcomes — both attributed to the unconditional sweep, not to a seat."""
    p = _sweep_pipeline()
    positions = [_SweepPos("AAA", 10, 90.0), _SweepPos("BBB", 5, 40.0)]
    out = p._adjudicate_target_revision_flags(_SweepReview(), positions, run_id="r1", seat="position_reviewer")
    assert [o["symbol"] for o in out] == ["AAA", "BBB"]
    assert {o["seat"] for o in out} == {tr.SEAT_STRUCTURAL_SWEEP}
    # Every outcome is persisted: the sweep can never produce a blank.
    assert len(p.db.recorded) == 2
    assert all(o["code"] for o in out)


def test_seat_flag_is_not_duplicated_by_the_sweep():
    """A symbol a seat did raise keeps the seat's label and its evidence,
    and is adjudicated exactly once."""
    p = _sweep_pipeline()
    positions = [_SweepPos("AAA", 10, 90.0), _SweepPos("BBB", 5, 40.0)]
    flag = TargetRevisionFlag(symbol="aaa", evidence="the seat's words")
    out = p._adjudicate_target_revision_flags(_SweepReview([flag]), positions, run_id="r1", seat="position_reviewer")
    by_sym = {o["symbol"]: o for o in out}
    assert sorted(by_sym) == ["AAA", "BBB"]
    assert by_sym["AAA"]["seat"] == "position_reviewer"
    assert by_sym["AAA"]["evidence"] == "the seat's words"
    assert by_sym["BBB"]["seat"] == tr.SEAT_STRUCTURAL_SWEEP


def test_flag_on_an_unheld_symbol_still_files_not_held():
    """Widening the way in must not lose the existing finding that a seat
    flagged something the broker does not show as held."""
    p = _sweep_pipeline()
    flag = TargetRevisionFlag(symbol="ZZZ", evidence="not in the book")
    out = p._adjudicate_target_revision_flags(_SweepReview([flag]), [], run_id="r1", seat="position_reviewer")
    assert len(out) == 1
    assert out[0]["code"] == "REFUSAL_NOT_HELD"
    assert out[0]["seat"] == "position_reviewer"


def test_no_open_positions_and_no_flags_is_still_a_no_op():
    p = _sweep_pipeline()
    assert p._adjudicate_target_revision_flags(_SweepReview(), [], run_id="r1", seat="position_reviewer") == []
    assert p.db.recorded == []


# ---------------------------------------------------------------------------
# item 194 round 2 — the money fault. A wall-triggered revision can move a
# target DOWN toward entry, which used to cross a range trade from the
# below-target ratchets into the structural trail. The trail only ratchets
# toward price, so restoring the target next session does NOT give the stop
# back: oscillation accumulated tightening instead of cancelling, and the
# sweep widened that from the flagged few to the whole book. The trailing
# regime now reads the PINNED entry target, so a revision cannot ratchet a
# stop the desk would not otherwise have moved.
# ---------------------------------------------------------------------------


def test_trailing_regime_reads_the_pinned_entry_target_not_the_live_one():
    import ast
    import inspect
    from src.exits_parts.trails import _apply_deterministic_trails

    src = inspect.getsource(_apply_deterministic_trails)
    tree = ast.parse(textwrap.dedent(src))
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        for kw in node.keywords:
            if kw.arg == "reference_target":
                found.append(ast.unparse(kw.value))
    assert found, "the trail no longer passes a reference_target at all"
    for expr in found:
        assert "initial_take_profit" in expr, (
            "the trailing regime boundary must read the PINNED entry target; "
            f"it reads {expr!r}, which a target revision can move"
        )


def test_update_open_take_profit_is_not_the_trail_reference():
    """The sibling half: the revision path writes `take_profit`, and the
    column the trail now reads is explicitly not that one."""
    import inspect
    from src.storage.db import Database

    src = inspect.getsource(Database.update_open_take_profit)
    assert "initial_take_profit" not in src.split("UPDATE")[-1].split(")")[0], (
        "the revision write must never touch the pinned column"
    )


# --- round 2 faults 3, 4 and 6 --------------------------------------------


class _BatchMarket(_SweepMarket):
    def __init__(self):
        self.batch_calls = []
        self.single_calls = []

    def get_ohlcv_batch(self, symbols, lookback_days):
        self.batch_calls.append(list(symbols))
        return {}

    def get_ohlcv(self, symbol, lookback_days):
        self.single_calls.append(symbol)
        return []


def test_the_sweep_reads_bars_in_one_batch():
    """The seat flag was rationing a serial per-name fetch. Removing the
    gate without batching would have turned one or two round trips into one
    per held name."""
    p = _sweep_pipeline()
    p.market = _BatchMarket()
    positions = [_SweepPos("AAA", 10, 90.0), _SweepPos("BBB", 5, 40.0), _SweepPos("CCC", 7, 20.0)]
    p._adjudicate_target_revision_flags(_SweepReview(), positions, run_id="r1", seat="position_reviewer")
    assert p.market.batch_calls == [["AAA", "BBB", "CCC"]]


def test_one_bad_name_does_not_truncate_the_rest_of_the_book():
    """A mid-sweep failure must leave the remaining names explicitly
    unmeasured, never invisibly skipped — the work list is sorted, so the
    same tail of the book was dropped every single session, and each
    dropped name kept a stored target the record did not mark unverified."""
    p = _sweep_pipeline()
    positions = [
        _SweepPos("AAA", 10, 90.0),
        # An unreadable side: `float(qty)` sits outside every inner guard,
        # which is exactly the shape of the unexpected exception that used
        # to unwind the whole sweep to the call site's bare `return []`.
        _SweepPos("BBB", "not-a-number", 40.0),
        _SweepPos("CCC", 7, 20.0),
    ]
    out = p._adjudicate_target_revision_flags(_SweepReview(), positions, run_id="r1", seat="position_reviewer")
    assert [o["symbol"] for o in out] == ["AAA", "BBB", "CCC"], "every held name must get a row, fault or not"
    bad = [o for o in out if o["symbol"] == "BBB"][0]
    assert bad["code"] == "FAULT_POSITION_NOT_MEASURED"
    assert bad["applied"] is False
    # And it is durable: an unmeasured position is a recorded finding.
    assert any(r["code"] == "FAULT_POSITION_NOT_MEASURED" for r in p.db.recorded)


def test_an_unchanged_refusal_is_not_refiled_every_session():
    """Eleven names filing an identical recomputable refusal daily is
    storage of recomputable state; the trail's own writer dedupes the same
    way."""
    p = _sweep_pipeline()
    positions = [_SweepPos("AAA", 10, 90.0)]
    first = p._adjudicate_target_revision_flags(_SweepReview(), positions, run_id="r1", seat="position_reviewer")
    code = first[0]["code"]
    p.db.get_target_revisions = lambda symbols, **kw: {"AAA": [{"code": code}]}
    before = len(p.db.recorded)
    second = p._adjudicate_target_revision_flags(_SweepReview(), positions, run_id="r2", seat="position_reviewer")
    assert second[0]["code"] == code
    assert second[0].get("unchanged_since_last_session") is True
    assert len(p.db.recorded) == before, "the identical refusal was re-filed"


# --- round 3 faults 4 and 5 ------------------------------------------------


def test_a_failure_to_file_the_fault_row_still_does_not_truncate_the_book():
    """The per-name guard was right, but the call that files the
    not-measured row sat INSIDE the except block unguarded, so a failure
    there unwound the remaining names after all — the same sorted-tail
    truncation, one layer deeper."""
    p = _sweep_pipeline()
    real_file = p._file_target_revision

    def _file(*, code, **kw):
        if code == "FAULT_POSITION_NOT_MEASURED":
            raise RuntimeError("synthetic filing failure")
        return real_file(code=code, **kw)

    p._file_target_revision = _file
    positions = [_SweepPos("AAA", 10, 90.0), _SweepPos("BBB", "not-a-number", 40.0), _SweepPos("CCC", 7, 20.0)]
    out = p._adjudicate_target_revision_flags(_SweepReview(), positions, run_id="r1", seat="position_reviewer")
    assert [o["symbol"] for o in out] == ["AAA", "CCC"], (
        "the tail of the book was truncated by the fault-filing failure"
    )


class _NoBatchMarket(_SweepMarket):
    def get_ohlcv_batch(self, symbols, lookback_days):
        raise RuntimeError("provider cannot batch today")


def test_a_serial_bar_read_is_recorded_not_merely_logged():
    """The batch fallback degraded to exactly the old serial path with only
    a log line; somebody measuring a slow session later could not tell why.
    A degraded session is also never deduped away."""
    p = _sweep_pipeline()
    p.market = _NoBatchMarket()
    positions = [_SweepPos("AAA", 10, 90.0)]
    out = p._adjudicate_target_revision_flags(_SweepReview(), positions, run_id="r1", seat="position_reviewer")
    assert "one name at a time" in out[0]["detail"]
    assert p.db.recorded and "one name at a time" in p.db.recorded[0]["detail"]
    # Same outcome next session, but still written, because the session was
    # degraded and that is the fact being preserved.
    code = out[0]["code"]
    p.db.get_target_revisions = lambda symbols, **kw: {"AAA": [{"code": code}]}
    before = len(p.db.recorded)
    again = p._adjudicate_target_revision_flags(_SweepReview(), positions, run_id="r2", seat="position_reviewer")
    assert again[0].get("unchanged_since_last_session") is not True
    assert len(p.db.recorded) == before + 1


# ---------------------------------------------------------------------------
# item 194 — THE BRAKE. The level-broken trigger has always required the
# same condition on two consecutive completed daily closes. The reach and
# wall triggers did not, so a target sitting near either bound flipped
# session to session. These pin that all three now go through the ONE
# existing confirmation mechanism, and that it is the same mechanism and
# not a copy of it.
# ---------------------------------------------------------------------------


def test_a_wall_seen_only_today_is_refused_by_name():
    """Identical inputs to the wall trigger's own test, minus the prior
    day's agreement. The target stands, and the refusal says why."""
    out = tr.assess_target_revision(
        stored_target=110.0,
        target_level=110.0,
        levels=[105.0, 110.0],
        atr=2.5,
        close_price=101.0,
        break_seen_prior_close=False,
        wall_seen_prior_close=False,
        **_COMMON,
    )
    assert out.code == tr.REVISION_WALL_PENDING_CONFIRMATION
    assert out.new_price is None
    assert out.prior_price == pytest.approx(110.0)
    assert "prior trading day" in out.detail


def test_a_reach_breach_seen_only_today_is_refused_by_name():
    """A stored target outside today's reach, with no prior day's reading
    to agree. One session's ATR is not a structural change."""
    args = dict(_COMMON)
    out = tr.assess_target_revision(
        stored_target=200.0,
        target_level=None,
        levels=[200.0],
        atr=2.5,
        close_price=101.0,
        break_seen_prior_close=False,
        reach_seen_prior_close=False,
        **args,
    )
    assert out.code == tr.REVISION_REACH_PENDING_CONFIRMATION
    assert out.new_price is None


def test_the_same_reach_breach_confirmed_is_a_trigger():
    """The brake is a brake, not a block: the prior day's agreement lets
    exactly the same reading through."""
    out = tr.assess_target_revision(
        stored_target=200.0,
        target_level=None,
        levels=[200.0],
        atr=2.5,
        close_price=101.0,
        break_seen_prior_close=False,
        reach_seen_prior_close=True,
        **_COMMON,
    )
    assert out.trigger == tr.TRIGGER_TARGET_BEYOND_REACH


def test_all_three_brakes_are_the_same_mechanism_not_three():
    """The load-bearing claim. There is ONE definition of confirmed: the
    raw state of a completed daily close, persisted under that close's own
    bar_date, re-read on a strictly later one. No trigger carries a count
    of closes or a margin of its own."""
    flags = tr.raw_trigger_flags(
        entry_price=100.0,
        stored_target=110.0,
        target_level=110.0,
        atr=2.5,
        close_price=101.0,
        horizon_sessions=10,
        levels=[105.0, 110.0],
        is_short=False,
    )
    assert set(flags) == {"raw_broken", "raw_reach", "raw_wall"}
    assert flags["raw_broken"] is False
    assert flags["raw_reach"] is False
    assert flags["raw_wall"] is True
    src = inspect.getsource(tr)
    # No second count of closes anywhere in the module.
    assert "prior_break_streak" not in src


def test_an_unmeasurable_input_is_never_half_a_confirmation():
    """A question that cannot be asked is None, not False — a None is
    never persisted, so it can neither confirm nor deny tomorrow."""
    flags = tr.raw_trigger_flags(
        entry_price=100.0,
        stored_target=110.0,
        target_level=110.0,
        atr=None,
        close_price=None,
        horizon_sessions=10,
        levels=[105.0],
        is_short=False,
    )
    assert flags == {"raw_broken": None, "raw_reach": None, "raw_wall": None}


def test_the_brake_adds_no_number():
    """No new constant may be introduced by a brake. The module's constant
    set is unchanged from the three bars it already held."""
    consts = {
        n
        for n in dir(tr)
        if n.isupper() and isinstance(getattr(tr, n), (int, float)) and not isinstance(getattr(tr, n), bool)
    }
    assert consts == {
        "MIN_TARGET_ATR_MULTIPLE",
        "BREAKOUT_PROJECTION_ATR_MULTIPLE",
        "MAX_REACH_ATR_MULTIPLE",
        "MAX_HORIZON_SESSIONS",
        "BREAK_CONFIRMATION_ATR_MULTIPLE",
    }


# ---------------------------------------------------------------------------
# item 194 — THE VOICING. A revision changes the number the desk quotes the
# owner. It reached the dashboard and reached Telegram nowhere.
# ---------------------------------------------------------------------------


def test_a_revision_is_voiced_with_its_reason_not_just_a_number():
    from src import notifier

    lines = notifier.describe_target_revisions(
        {
            "target_revisions": [
                {
                    "symbol": "TEST",
                    "applied": True,
                    "prior_price": 110.0,
                    "new_price": 105.0,
                    "basis": tr.STRUCTURAL_LEVEL_BASIS,
                    "trigger": tr.TRIGGER_WALL_IN_FRONT_OF_TARGET,
                }
            ]
        },
        held_symbols=["TEST"],
    )
    body = "\n".join(lines)
    assert "TEST" in body
    assert "ceiling" in body  # the REASON, in plain words
    assert "$110.00" in body and "$105.00" in body
    assert "down" in body
    assert "not a sell order" in body


def test_every_trigger_the_module_can_emit_has_owner_words():
    """A revision the owner cannot read a reason for is the defect this
    fixes, so no trigger may fall through to its raw code."""
    from src.notifier import _target_revision_reason

    for code in (
        tr.TRIGGER_LEVEL_BROKEN,
        tr.TRIGGER_TARGET_BEYOND_REACH,
        tr.TRIGGER_WALL_IN_FRONT_OF_TARGET,
        tr.TRIGGER_DERIVATION_CORRECTED,
    ):
        words = _target_revision_reason(code)
        assert words and not words.startswith("trigger ")


def test_a_session_with_no_applied_revision_says_nothing():
    """Refusals are the normal outcome on most held names every session;
    voicing them all would bury the one that moved."""
    from src import notifier

    assert (
        notifier.describe_target_revisions(
            {
                "target_revisions": [
                    {"symbol": "TEST", "applied": False, "code": tr.REVISION_NO_TRIGGER},
                ]
            },
            held_symbols=["TEST"],
        )
        == []
    )
    assert notifier.describe_target_revisions({}, held_symbols=None) == []


# ---------------------------------------------------------------------------
# item 194 ROUND 3 — four defects the adversary traced in the brake itself.
# ---------------------------------------------------------------------------


def test_a_missing_level_set_cannot_persist_no_wall_as_a_fact():
    """DEFECT 1. `levels_still_in_the_way([])` is `[]` and `walls_between`
    on `[]` is a definite "no wall", so a degraded bar fetch used to write
    False and erase a genuine True recorded earlier the same day. An
    absent reading must never produce an action."""
    for levels in (None, []):
        flags = tr.raw_trigger_flags(
            entry_price=100.0,
            stored_target=110.0,
            target_level=110.0,
            atr=2.5,
            close_price=101.0,
            horizon_sessions=10,
            levels=levels,
            is_short=False,
        )
        assert flags["raw_wall"] is None, levels
    # A real level set still answers the question both ways.
    assert (
        tr.raw_trigger_flags(
            entry_price=100.0,
            stored_target=110.0,
            target_level=110.0,
            atr=2.5,
            close_price=101.0,
            horizon_sessions=10,
            levels=[105.0, 110.0],
            is_short=False,
        )["raw_wall"]
        is True
    )
    assert (
        tr.raw_trigger_flags(
            entry_price=100.0,
            stored_target=110.0,
            target_level=110.0,
            atr=2.5,
            close_price=101.0,
            horizon_sessions=10,
            levels=[110.0],
            is_short=False,
        )["raw_wall"]
        is False
    )


def test_an_unconfirmed_trigger_cannot_suppress_a_confirmed_one():
    """DEFECT 3. A confirmed wall plus a one-day reach blip used to yield
    no revision at all, and when it finally fired it fired under the reach
    trigger — so the owner was handed the reach reason for a change the
    wall caused."""
    out = tr.assess_target_revision(
        stored_target=110.0,
        target_level=110.0,
        levels=[105.0, 110.0],
        atr=2.5,
        close_price=101.0,
        break_seen_prior_close=False,
        reach_seen_prior_close=False,
        wall_seen_prior_close=True,
        **_COMMON,
    )
    assert out.trigger == tr.TRIGGER_WALL_IN_FRONT_OF_TARGET
    assert out.revised
    assert out.new_price == pytest.approx(105.0)


def test_a_pending_trigger_is_reported_as_pending_not_as_no_trigger():
    """A hold on a number the desk has stopped believing is not a clean
    bill of health, and only a chart with no trigger at all may say so."""
    out = tr.assess_target_revision(
        stored_target=110.0,
        target_level=110.0,
        levels=[105.0, 110.0],
        atr=2.5,
        close_price=101.0,
        break_seen_prior_close=False,
        wall_seen_prior_close=False,
        **_COMMON,
    )
    assert out.code == tr.REVISION_WALL_PENDING_CONFIRMATION
    out = tr.assess_target_revision(
        stored_target=110.0,
        target_level=110.0,
        levels=[110.0],
        atr=2.5,
        close_price=101.0,
        break_seen_prior_close=False,
        **_COMMON,
    )
    assert out.code == tr.REVISION_NO_TRIGGER


def test_the_confirmation_is_keyed_on_the_close_not_on_the_last_row(tmp_path):
    """DEFECT 2. Several intraday cycles can re-read one close; whichever
    ran last used to decide the flag. The reading is now the earliest row
    recorded for the latest prior bar date, and a row that does not answer
    the question is skipped rather than read as False."""
    from src.storage.db import Database

    db = Database(str(tmp_path / "t.db"))
    db.initialize()
    # Two cycles re-read the SAME prior close and disagree. The first
    # reading of that close wins, whichever ran last.
    db.save_target_level_break(
        run_id="r1",
        symbol="TEST",
        bar_date="2026-09-29",
        raw_broken=True,
        raw_wall=True,
    )
    db.save_target_level_break(
        run_id="r2",
        symbol="TEST",
        bar_date="2026-09-29",
        raw_broken=False,
        raw_wall=False,
    )
    for flag in ("raw_broken", "raw_wall"):
        got = db.get_prior_target_level_break(
            ["TEST"],
            today_bar_date="2026-09-30",
            flag=flag,
        )
        assert got.get("TEST") is True, flag
    # A degraded later cycle that could not answer the wall question must
    # not erase the answer already given for that close.
    db.save_target_level_break(
        run_id="r3",
        symbol="TEST",
        bar_date="2026-09-29",
        raw_broken=False,
        raw_wall=None,
    )
    assert (
        db.get_prior_target_level_break(
            ["TEST"],
            today_bar_date="2026-09-30",
            flag="raw_wall",
        ).get("TEST")
        is True
    )
    # Today's own close never confirms itself.
    assert (
        db.get_prior_target_level_break(
            ["TEST"],
            today_bar_date="2026-09-29",
            flag="raw_wall",
        )
        == {}
    )


def test_a_session_that_applied_nothing_still_names_a_held_target():
    """DEFECT 4. The brake makes an all-refused session the common case,
    and the owner was told nothing at all while positions sat on quoted
    targets the desk had stopped believing."""
    from src import notifier

    lines = notifier.describe_target_revisions(
        {
            "target_revisions": [
                {
                    "symbol": "TEST",
                    "applied": False,
                    "prior_price": 110.0,
                    "code": tr.REVISION_WALL_PENDING_CONFIRMATION,
                },
                {"symbol": "OTHR", "applied": False, "code": tr.REVISION_NO_TRIGGER},
            ]
        },
        held_symbols=["TEST", "OTHR"],
    )
    body = "\n".join(lines)
    assert "TEST" in body  # named, not counted
    assert "$110.00" in body  # the number he is still quoted
    assert "second day's close" in body  # why it is being held
    assert "OTHR" not in body  # a clean name is not noise
    assert "1 other position(s) measured" in body


def test_every_pending_code_the_module_can_emit_has_owner_words():
    from src.notifier import _pending_confirmation_reason

    for code in (
        tr.REVISION_BREAK_PENDING_CONFIRMATION,
        tr.REVISION_REACH_PENDING_CONFIRMATION,
        tr.REVISION_WALL_PENDING_CONFIRMATION,
    ):
        words = _pending_confirmation_reason(code)
        assert words and words != code and "close" in words
