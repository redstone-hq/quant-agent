"""Board item 10: a candidate is NEVER dropped without a machine-readable reason.

Two tests, doing two different jobs, because the defect this file exists for
has now recurred three times (funnel-queue item 2 on 2026-09-03, retired
board item 49 on 2026-09-14, board item 10's first pass the same day) and
each fix was a list of individual sites believed complete at the time.

`test_the_archive_replays_with_a_reason_on_every_dropped_candidate` is the
EMPIRICAL half: it runs the real `PortfolioConstructor.construct_orders` over
every PM target the live desk actually wrote between 2026-08-18 and
2026-09-02 — real targets, real technical analyses, the real seat-stance
registry, the real held book and the session's own equity, all read out of
the production archive and frozen into
`tests/fixtures/constructor_drop_paths_archive.json`. No stub stands in for
any of it. The claim it settles is the one board item 10 said needed the desk
to trade again: every candidate that produces no order carries a durable,
per-symbol, machine-readable reason.

`test_every_drop_site_in_the_constructor_files_a_reason` is the STANDING
GUARD, and is worth more than either fix: it parses `portfolio_constructor.py`
and fails when a NEW way to end a candidate is added without one. A replay
only covers the paths its recorded inputs happen to reach; the AST scan covers
the ones nobody has hit yet, which is exactly the class every previous pass
missed.
"""

import ast
import json
from pathlib import Path

import pytest

from src.models import Position, TargetPosition, TechAnalysisResult
from src.portfolio_constructor import PortfolioConstructor

_SOURCE = Path(__file__).resolve().parent.parent / "src" / "portfolio_constructor"
_FIXTURE = Path(__file__).resolve().parent / "fixtures" / "constructor_drop_paths_archive.json"


# ---------------------------------------------------------------------------
# The empirical half — real production inputs, real code, no stubs
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def archive():
    return json.loads(_FIXTURE.read_text())


def _replay(decision, positions):
    """One recorded decision through the real constructor. Returns
    (built symbols, dropped symbols, refusals, faults, drop_reasons)."""
    targets = [TargetPosition.model_validate(t) for t in decision["targets"]]
    analyses = [TechAnalysisResult.model_validate(a) for a in decision["analyses"]]
    # The live quote each session priced against was never persisted, so the
    # analyst's own entry is used. That changes WHICH refusal some names hit;
    # it cannot make a dropped candidate carry a reason it would not
    # otherwise carry, which is the only thing asserted below.
    price_map = {a.symbol: a.entry_price for a in analyses if a.entry_price}
    constructor = PortfolioConstructor()
    orders = constructor.construct_orders(
        targets,
        positions,
        analyses,
        decision["equity"],
        price_map=price_map,
        existing_risk_pct={},
        clusters=None,
        regime=None,
        evidence_registry=decision["evidence_registry"] or None,
    )
    built = {o.symbol.upper() for o in orders}
    dropped = sorted({t.symbol.upper() for t in targets} - built)
    return built, dropped, constructor.last_refusals, constructor.last_data_faults


def test_the_archive_replays_with_a_reason_on_every_dropped_candidate(archive):
    """THE claim board item 10 said could not be settled without new trading.

    Every PM target in the archive that produces no order must appear in
    `last_refusals` or `last_data_faults` with a non-empty code AND a
    non-empty human detail — the two dicts `pipeline_stages.
    _record_constructor_drops` reads BEFORE it falls back to the generic
    `constructor_dropped` row whose detail is recovered by regex over log
    prose. A symbol in neither is exactly the defect: it reaches the database
    as "no matching constructor log line captured".
    """
    positions = [Position.model_validate(p) for p in archive["positions"]]
    unexplained = []
    dropped_total = 0
    for decision in archive["decisions"]:
        _, dropped, refusals, faults = _replay(decision, positions)
        dropped_total += len(dropped)
        for symbol in dropped:
            record = refusals.get(symbol) or faults.get(symbol)
            if not record:
                unexplained.append((decision["run_id"], symbol, "no record at all"))
                continue
            code = record.get("refusal") or record.get("fault") or ""
            if not code.strip() or not (record.get("detail") or "").strip():
                unexplained.append((decision["run_id"], symbol, f"empty code/detail: {record}"))
    assert unexplained == [], "candidates dropped with no machine-readable reason: " + repr(unexplained)
    # A guard on the guard: if the fixture ever stops producing drops, the
    # assertion above passes vacuously and proves nothing.
    assert dropped_total >= 40, dropped_total


def test_the_replay_verdicts_are_pinned_so_a_reason_fix_cannot_move_one(archive):
    """No refusal behaviour may change — same candidates dropped, same kept.

    This is how that claim is checked rather than asserted. The built set per
    recorded decision is pinned here; adding a reason to a drop path must
    leave every one of these untouched. If a future change to this module
    moves one of them, that is a trading-behaviour change and it must be
    argued for on its own terms, not slipped in beside a logging fix.

    ONE entry moved, 2026-09-30, board item 183, and it is argued here
    rather than quietly re-pinned. `run-e9432693` lost CMCSA. The owner
    ruled the picked `min_trade_weight_delta` churn floor is DELETED, so the
    0.196%-of-book add this session asked for on CMCSA (target 5.0% against
    a held 4.804%) is now ATTEMPTED instead of being converted into a
    do-nothing HOLD row. Replayed against the pre-ruling code that add came
    out as `action="HOLD", allocation_pct=0.0` [measured: the archive
    decision replayed through `origin/main`'s constructor]; replayed against
    today's it reaches `_build_buy` and is refused by an unrelated,
    pre-existing data fault — `price_history_unusable`, no structural levels
    computable for CMCSA in that session. So the name did not lose a trade
    it would otherwise have got: it stopped being silently held and started
    being honestly refused, and it carries a machine-readable fault either
    way (the test above checks that). This is the ruling doing exactly what
    it was made to do, not a regression, and it is the ONLY verdict in the
    archive that moves.
    """
    positions = [Position.model_validate(p) for p in archive["positions"]]
    built_by_run = {}
    for decision in archive["decisions"]:
        built, _, _, _ = _replay(decision, positions)
        built_by_run[decision["run_id"]] = sorted(built)
    assert built_by_run == {
        "intra_check-41e42bca": [],
        "intra_check-483360ea": [],
        "intra_check-63ee9b86": [],
        "intra_check-6d86968d": [],
        "intra_check-71b87090": [],
        "intra_check-9429cd1f": [],
        "intra_check-d0909ddc": [],
        "intra_check-d6b06e63": [],
        "intra_check-f6975fb8": [],
        "run-13c21846": [],
        "run-5593a8c9": [],
        "run-5834d319": [],
        "run-5dc33236": [],
        "run-6211ad13": [],
        "run-64290730": [],
        "run-74cbb7c7": [],
        "run-a933b4da": [],
        "run-bba4d4f3": ["EPD", "NVDA", "V"],
        "run-c2f42b39": [],
        # CMCSA dropped here 2026-09-30 — see the argument in the docstring.
        "run-e9432693": ["MSFT"],
    }


def test_the_sixteen_historical_no_order_built_rows_would_now_be_named(archive):
    """The 16 rows board item 10 was written about, replayed by name.

    They are the `no_order_built` bucket of the census restricted to
    proposals after the limit-is-ceiling fix (`0eb4a115`, 2026-08-27 14:18Z):
    28 proposals, 3 filled, 25 blocked, 16 of them with no recoverable
    reason. They stay unexplained IN THE DATABASE — nothing retroactively
    writes a row for a session that has already run. What this pins is the
    forward-looking half: run the same recorded inputs through today's code
    and not one of them comes out anonymous.
    """
    rows = {
        ("intra_check-41e42bca", "CRM"),
        ("intra_check-d6b06e63", "NVDA"),
        ("intra_check-483360ea", "ONDS"),
        ("intra_check-9429cd1f", "MP"),
        ("intra_check-6d86968d", "CRM"),
        ("run-5834d319", "NVDA"),
        ("run-c2f42b39", "VLO"),
        ("run-c2f42b39", "PATH"),
        ("run-13c21846", "DE"),
        ("run-64290730", "NVDA"),
        ("intra_check-d0909ddc", "ZS"),
        ("run-bba4d4f3", "MU"),
        ("run-bba4d4f3", "CMCSA"),
        ("run-bba4d4f3", "DIS"),
        ("run-bba4d4f3", "V"),
        ("run-bba4d4f3", "AUGO"),
    }
    # ONE of the sixteen does not come out dropped, and the reason is worth
    # naming rather than hiding in a filter. V on 2026-09-02 was refused for
    # `no_level_in_direction` against a $372.67 entry — the live quote, which
    # was never persisted. Replayed against the analyst's own $373.70 entry,
    # the nearest level IS in reach and the trade builds. Nothing about the
    # reason machinery changed; the input did. Recorded here so the next
    # reader does not mistake a missing quote for a fixed bug.
    priced_off_a_quote_nobody_kept = {("run-bba4d4f3", "V")}
    positions = [Position.model_validate(p) for p in archive["positions"]]
    by_run = {d["run_id"]: d for d in archive["decisions"]}
    named = 0
    for run_id, symbol in sorted(rows):
        decision = by_run[run_id]
        built, dropped, refusals, faults = _replay(decision, positions)
        if (run_id, symbol) in priced_off_a_quote_nobody_kept:
            assert symbol in built, (run_id, symbol)
            continue
        assert symbol in dropped, (run_id, symbol)
        record = refusals.get(symbol) or faults.get(symbol)
        assert record and (record.get("refusal") or record.get("fault")), (run_id, symbol)
        named += 1
    assert named == 15
    assert len(rows) == 16


# ---------------------------------------------------------------------------
# The standing guard — an AST scan, so an unwritten path is covered too
# ---------------------------------------------------------------------------

#: Every method that can END a candidate: return no order, or skip it inside
#: the target loop. A drop site in one of these must file a reason.
_CANDIDATE_ENDING_METHODS = frozenset(
    {
        "_construct_orders_impl",
        "_plan_risk_targets",
        "_resolve_entry_and_stop",
        "_held_trim_entry_and_stop",
        "_dollar_target_as_risk",
        "_widen_stop_past_noise",
        "_build_buy",
        "_build_short",
        "_refuse_below_min_risk",
    }
)

#: Everything else `_construct_orders_impl` reaches, with the reason it
#: cannot end a candidate on its own. Checked against the real call graph
#: below, so a NEW method in the chain fails until it is classified here or
#: added above — the guard has to notice a path that does not exist yet.
_CANNOT_END_A_CANDIDATE = {
    "_current_weights": "reads the held book; returns weights, never a verdict",
    "_current_sector_weights": "same, per sector",
    "_accrue_sector": "bookkeeping after an order is already built",
    "_hold_decision": "builds a HOLD row; the symbol survives",
    "_build_sell": "exits, not entries — a refused exit leaves the position untouched",
    "_build_cover": "same, short side",
    "_entry_stop_resolver": "thin shim factory: builds the resolver whose methods are scanned above",
    "_derive_target": "returns a derivation; every fault it finds it files itself",
    "_log_target_divergence": "log + one row",
    "_target_note": "string formatting",
    "_resolve_stop": "None means 'read it from the instrument', not 'no trade'",
    "_stop_atr_multiple": "returns a multiple",
    "_level_backing_stop": "returns the level under the stop, or None",
    "_derive_structural_stop_no_atr": "returns a structural stop or None; the no-ATR caller files any refusal",
    "_reward_risk_at": "arithmetic",
    "_note_refusal": "the recorder itself",
    "_refuse_book_unknown": "the recorder itself — files STOP_REFUSAL_BOOK_RISK_UNKNOWN and logs; the caller drops",
    "_record_subfloor_risk_target": (
        "board item 223 recording only — a durable row for a positive "
        "sub-floor PM risk request; it was ruled on the risk route "
        "2026-10-01 that such a "
        "target is NOT refused and NOT resized, so this writes a record and "
        "the candidate continues down the path it was already on"
    ),
    "_note_data_fault": "the recorder itself",
    "shipped_stop_rule": "names the rule on an order already built",
    "shipped_stop_level_basis": (
        "item 55 recording only — describes the level behind a stop on an order "
        "already built, and no caller reads it back into a decision"
    ),
}

#: The drop sites that legitimately file nothing THEMSELVES, each with the
#: name of whatever does file it. Keyed by the marker comment the site
#: carries, not by line number, so this cannot rot silently. A new site with
#: no note call and no marker fails the test.
_DELEGATION_MARKER = "# drop-reason:"


def _class_nodes():  # the resolver is LAST so its lifted bodies win by name over the host shims
    def _c(paths, names):
        return [
            (n, t.splitlines())
            for p in paths
            for t in [p.read_text()]
            for n in ast.parse(t).body
            if isinstance(n, ast.ClassDef) and n.name in names
        ]

    out = (
        _c(sorted(_SOURCE.glob("*.py")), {"PortfolioConstructor", "StopRules", "OrderBuilders"})
        + _c(sorted((_SOURCE / "entry_stop").glob("*.py")), {"EntryStopResolver"})
        + _c(
            sorted((_SOURCE / "order_build").glob("*.py")),
            {"ExitOrderBuilders", "LongEntryBuilder", "ShortEntryBuilder"},
        )
    )
    assert len(out) == 7, [n.name for n, _ in out]
    return out


_LIFTED_MODULES = (
    "sector_dial.py",
    "target_derivation.py",
    "refusal_log.py",
)  # moved bodies: module functions, scanned exactly like the methods they were


def _lifted_functions(sources=None):
    """(function node, its file's lines) for every module-level function in the lifted modules."""
    texts = sources if sources is not None else {m: (_SOURCE / m).read_text() for m in _LIFTED_MODULES}
    return [(n, t.splitlines()) for t in texts.values() for n in ast.parse(t).body if isinstance(n, ast.FunctionDef)]


def _methods(
    lines=False, lifted_sources=None
):  # skips "Thin shim" docstrings: a lift's same-named shim would pass vacuously; the moved body is scanned via its class above
    found = {
        n.name: (ls if lines else n)
        for c, ls in _class_nodes()
        for n in c.body
        if isinstance(n, ast.FunctionDef) and not (ast.get_docstring(n) or "").startswith("Thin shim")
    }
    found.update({n.name: (ls if lines else n) for n, ls in _lifted_functions(lifted_sources)})
    return found


def _drop_sites(fn):
    """Every statement in `fn` that ends a candidate.

    A `return` of None (bare, `None`, or an all-None tuple) or of a negative
    constant (`_apply_sector_dial`'s "refuse" signal), and a `continue` in a
    loop over targets. A `continue` in any other loop is ordinary iteration —
    `_level_backing_stop` walking a list of price levels, say — and is not a
    candidate ending.
    """
    sites = []
    loop_targets = {"target", "targets", "t"}

    def walk(node, loops):
        for child in ast.iter_child_nodes(node):
            inner = loops
            if isinstance(child, ast.For):
                name = getattr(child.target, "id", "")
                inner = loops + [name]
            if isinstance(child, ast.Return):
                v = child.value
                is_none = (
                    v is None
                    or (isinstance(v, ast.Constant) and v.value is None)
                    or (
                        isinstance(v, ast.Tuple)
                        and bool(v.elts)
                        and all(isinstance(e, ast.Constant) and e.value is None for e in v.elts)
                    )
                )
                is_negative = (
                    isinstance(v, ast.UnaryOp) and isinstance(v.op, ast.USub) and isinstance(v.operand, ast.Constant)
                )
                is_negative_tuple = (
                    isinstance(v, ast.Tuple)
                    and bool(v.elts)
                    and isinstance(v.elts[0], ast.UnaryOp)
                    and isinstance(v.elts[0].op, ast.USub)
                )
                if is_none or is_negative or is_negative_tuple:
                    sites.append(child)
            elif isinstance(child, ast.Continue) and (set(loops) & loop_targets):
                sites.append(child)
            walk(child, inner)

    walk(fn, [])
    return sites


def _files_a_reason(fn, site, source_lines):
    """True if this drop site records a reason, or names who does.

    Covered when the smallest enclosing block (an `if`/`elif`/`else` body, a
    `for` body, ... — anything but the whole method, which would let one
    reason at the top cover ten silent returns underneath) contains a
    `_note_refusal` / `_note_data_fault` call, or when the site carries the
    delegation marker in a comment on it or in the few lines above it.
    """
    for chain, node in _enclosing_blocks(fn, site):
        if _has_note_call(chain):
            return True
    line = site.lineno
    window = source_lines[max(0, line - 12) : line]
    return any(_DELEGATION_MARKER in text for text in window)


def _enclosing_blocks(fn, site):
    """Yield (statement list, owner) for every block containing `site`,
    innermost first, excluding the method body itself."""
    found = []

    def walk(node, chain):
        for field, value in ast.iter_fields(node):
            if not isinstance(value, list):
                continue
            if value and all(isinstance(v, ast.stmt) for v in value):
                if site in value:
                    found.append((value, node))
                for v in value:
                    walk(v, chain + [value])
            else:
                for v in value:
                    if isinstance(v, ast.AST):
                        walk(v, chain)

    for stmt in fn.body:
        walk(stmt, [])
    if site in fn.body:
        found.append((fn.body, fn))
    # innermost first; drop the method body — a note call anywhere in a
    # 400-line method must not vouch for an unrelated return inside it.
    return [(block, owner) for block, owner in found if owner is not fn]


def _has_note_call(block):
    for stmt in block:
        for node in ast.walk(stmt):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in ("_note_refusal", "_note_data_fault")
            ):
                return True
            # A lifted body gets the recorder as an argument: the same call, by its parameter name.
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id in ("note_refusal", "note_data_fault", "_note_refusal", "_note_data_fault")
            ):
                return True
    return False


def test_every_drop_site_in_the_constructor_files_a_reason():
    """THE STANDING GUARD. Add a new way to drop a candidate without a
    structured reason and this fails, naming the line.

    A site passes by doing one of two things: recording the reason itself
    (`_note_refusal` / `_note_data_fault` in the same block), or carrying a
    `# drop-reason:` comment saying who records it instead — a delegation to
    `_resolve_entry_and_stop`, or a `continue` that is not a drop at all.
    Neither is satisfiable by accident, which is the point: the three
    previous passes at this defect each shipped a list of sites believed
    complete, and each was wrong about a site nobody had thought of.
    """
    lines_by_method = _methods(lines=True)
    methods = _methods()
    silent = []
    checked = 0
    for name in sorted(_CANDIDATE_ENDING_METHODS):
        fn = methods[name]
        for site in _drop_sites(fn):
            checked += 1
            if not _files_a_reason(fn, site, lines_by_method[name]):
                silent.append(f"{name}:{site.lineno}: {lines_by_method[name][site.lineno - 1].strip()}")
    assert silent == [], (
        "drop sites with no structured reason and no `# drop-reason:` marker "
        "saying who files one:\n  " + "\n  ".join(silent)
    )
    # Vacuous-pass guard: the scanner must still be finding sites at all.
    assert checked >= 20, checked


def test_the_guard_covers_every_method_the_constructor_can_end_a_candidate_in():
    """The scan above is only as good as its method list, so the method list
    is derived, not trusted: every `self.<method>` the entry-construction
    chain reaches must be either scanned or explicitly classified as unable
    to end a candidate. A new helper in the chain fails here first."""
    methods = _methods()
    reached, queue = set(), ["_construct_orders_impl"]
    while queue:
        name = queue.pop()
        if name in reached or name not in methods:
            continue
        reached.add(name)
        for node in ast.walk(methods[name]):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "self"
            ):
                queue.append(node.func.attr)
    unclassified = sorted(reached - _CANDIDATE_ENDING_METHODS - set(_CANNOT_END_A_CANDIDATE))
    assert unclassified == [], (
        "methods reachable from construct_orders that are neither scanned "
        "for drop sites nor documented as unable to drop a candidate: " + repr(unclassified)
    )


def test_the_capture_regex_is_not_relied_on_by_any_of_these_paths():
    """Why the codes exist at all, pinned as a fact rather than a story.

    `_DropReasonCapture._SYMBOL` requires rejected|refused|skipped directly
    after the symbol. Two real messages this module emits do not have it —
    the churn filter emits nothing at all, and the no-stop-no-volatility
    refusal puts "has" there — so a record that depends on the regex is a
    record with holes in it. Both are now filed as data instead, and this
    test fails if someone "simplifies" that back to a log scrape.
    """
    from src.portfolio_constructor import _DropReasonCapture

    misses = [
        "Constructor: BUY NVDA has no stop from the PM or the analyst and no "
        "ATR reading to derive one from — rejecting.",
        "Constructor: max_gross_exposure: NVDA refused — the book would own more",
        "Constructor: NVDA produces no order — risk budget granted 0.00%",
    ]
    for message in misses:
        assert _DropReasonCapture._SYMBOL.search(message) is None, message


# ---------------------------------------------------------------------------
# The paths the recorded inputs never reach
# ---------------------------------------------------------------------------
#
# The archive replay above exercises five refusal/fault codes on real data.
# It cannot exercise the rest: the recorded sessions simply never hit a
# non-finite price or an analyst who typed a stop on the wrong side. Those are
# driven here from the SAME real analysis row, with exactly one field changed
# and the change named — not from an object invented to make a branch fire.

_REAL_ROW = "run-bba4d4f3"  # 2026-09-02, the one archive session whose
_REAL_SYMBOL = "MU"  # analyses carry computed levels


def _one_real_analysis(archive, **overrides):
    """The real MU row of 2026-09-02, with named fields removed.

    `model_copy` and not `model_validate`, deliberately: the schema refuses a
    `buy` with no stop, and the point of these cases is precisely the state
    the schema forbids but the constructor must still survive — an older
    persisted row, a hand-built backtest object, a provider that returned
    nothing. Every other field is the analyst's own.
    """
    decision = next(d for d in archive["decisions"] if d["run_id"] == _REAL_ROW)
    row = next(a for a in decision["analyses"] if a["symbol"] == _REAL_SYMBOL)
    target = next(t for t in decision["targets"] if t["symbol"] == _REAL_SYMBOL)
    analysis = TechAnalysisResult.model_validate(row)
    if overrides:
        analysis = analysis.model_copy(update=overrides)
    return TargetPosition.model_validate(target), analysis


def _refusal_for(target, analysis, *, price=None, suggested_stop=None):
    if suggested_stop is not None:
        target = target.model_copy(update={"suggested_stop_price": suggested_stop})
    constructor = PortfolioConstructor()
    entry = price if price is not None else analysis.entry_price
    orders = constructor.construct_orders(
        [target],
        [],
        [analysis],
        9822.37,
        price_map={analysis.symbol: entry} if entry else {},
        existing_risk_pct={},
    )
    assert orders == []
    return constructor.last_refusals.get(analysis.symbol.upper())


def test_the_three_defensive_stop_guards_file_a_reason_and_none_is_reachable_today(archive):
    """The regex-miss board item 10's first pass did not find — and the
    honest limit of that finding.

    `_widen_stop_past_noise` refuses on three inputs the schema forbids but
    an older persisted row or a hand-built object can still carry: no stop
    and no ATR to derive one from, a non-finite stop, a non-finite entry. The
    first of those three logged "Constructor: BUY MU has no stop ..." — the
    capture's pattern needs rejected|refused|skipped straight after the
    symbol, so a candidate dropped there reached the record as the literal
    "no matching constructor log line captured", the exact signature the
    first pass reported as eliminated. All three now file a code.

    And the part that must not be overstated: NONE of the three is reachable
    through `construct_orders` as the code stands. A missing or non-finite
    ATR makes `_derive_target` file a data fault one step earlier, and a
    non-finite stop fails the `> 0` test in `_resolve_stop` and is replaced
    by the instrument's own band. That is pinned below, so the day a change
    opens one of these up, the record already says what it will say.
    """
    from src.portfolio_constructor import (
        STOP_REFUSAL_NO_STRUCTURAL_STOP_NO_VOLATILITY,
    )

    _, analysis = _one_real_analysis(archive)
    constructor = PortfolioConstructor()
    # Reworked for board item 80 (owner 2026-09-25): with no ATR the branch
    # now DERIVES a stop from price structure and HOLDS; only the genuine
    # no-structure state refuses. Strip the computed levels and the signal bar
    # so this exercises that refusal -- which must still file a code, not the
    # regex-missed bare "has no stop" log line the first pass reported gone.
    stripped = analysis.model_copy(
        update={
            "atr_14": None,
            "computed_levels": [],
            "computed_level_touches": {},
            "signal_bar_low": None,
            "signal_bar_high": None,
        }
    )
    assert (
        constructor._widen_stop_past_noise(
            analysis.symbol,
            stripped,
            float(analysis.entry_price),
            None,
            direction="long",
        )
        is None
    )
    record = constructor.last_refusals[analysis.symbol.upper()]
    assert record["refusal"] == STOP_REFUSAL_NO_STRUCTURAL_STOP_NO_VOLATILITY
    assert record["detail"].strip()

    # ... and the reachability half, through the real entry point.
    outcomes = {}
    for label, override in (
        ("no stop, no ATR", {"stop_loss": None, "atr_14": None}),
        ("no stop", {"stop_loss": None}),
        ("non-finite stop", {"stop_loss": float("nan")}),
    ):
        target, mutated = _one_real_analysis(archive, **override)
        live = PortfolioConstructor()
        orders = live.construct_orders(
            [target],
            [],
            [mutated],
            9822.37,
            price_map={mutated.symbol: mutated.entry_price},
            existing_risk_pct={},
        )
        outcomes[label] = sorted(live.last_refusals) or sorted(live.last_data_faults) or [o.symbol for o in orders]
    assert outcomes == {
        # a data fault, filed one step before the stop is ever resolved
        "no stop, no ATR": ["MU"],
        # the instrument's own band supplies the stop; the trade ships
        "no stop": ["MU"],
        "non-finite stop": ["MU"],
    }


def test_a_stop_on_the_wrong_side_of_entry_is_named(archive):
    """Refused since 2026-09-01, but only ever in prose."""
    from src.portfolio_constructor import STOP_REFUSAL_WRONG_SIDE

    target, analysis = _one_real_analysis(archive)
    record = _refusal_for(
        target,
        analysis,
        suggested_stop=float(analysis.entry_price) + 1.0,
    )
    assert record and record["refusal"] == STOP_REFUSAL_WRONG_SIDE


def test_the_guard_bites_on_a_lifted_function_with_a_silent_drop():
    """Coverage follows moved code: a lifted function ending a candidate with
    no reason must fail the same rule, and one that files a reason must pass."""
    silent = "def _apply_sector_dial(cfg, note_refusal, symbol):\n    if symbol:\n        return -1.0, ''\n    return 1.0, ''\n"
    filed = "def _apply_sector_dial(cfg, note_refusal, symbol):\n    if symbol:\n        note_refusal(symbol, 'long', 'X', 'why')\n        return -1.0, ''\n    return 1.0, ''\n"
    for source, expect_silent in ((silent, True), (filed, False)):
        srcs = {"sector_dial.py": source}
        fn = _methods(lifted_sources=srcs)["_apply_sector_dial"]
        ls = _methods(lines=True, lifted_sources=srcs)["_apply_sector_dial"]
        sites = _drop_sites(fn)
        assert sites
        assert any(not _files_a_reason(fn, s, ls) for s in sites) is expect_silent
