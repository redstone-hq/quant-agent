"""Layer 3 — one definition per quantity, enforced against the AST.

Layer 2 (`tests/test_cross_consistency.py`) catches the six defects we already
know about. This file is the one that catches the SEVENTH, before anyone
measures it.

Why the AST and not grep
------------------------
The two average-true-range implementations share no name, no file, no
signature and no import. One is `_atr_series` doing `np.convolve` over true
ranges; the other is `ta.volatility.AverageTrueRange`. Searching for "atr"
finds one of them. Searching for the other finds nothing. What they have in
common is the ARITHMETIC — and arithmetic is exactly what an AST can see.

So each entry in `REGISTRY` below names a quantity, names the ONE function
sanctioned to compute it, and carries a matcher that recognises the *shape* of
the computation wherever it appears. A second site performing that shape is a
second definition, and a second definition is the defect class this file
exists to make impossible.

Precedent: `tests/test_gross_exposure_ladder.py::
test_trimming_the_held_book_has_exactly_one_owner` restricts authorship of
de-lever orders to exactly one caller by walking the AST for `Call` nodes.
This file follows that shape and generalises it from one call to a registry of
arithmetic patterns.

Keeping it trustworthy
----------------------
A guard that flags correct code is disabled within a week, and then there is
nothing. Two rules keep this one honest:

1. Every matcher is written to recognise a *wrong-shaped* computation, not
   merely a computation. `percent_of_book` does not flag every division by
   equity — the codebase has eleven legitimate ones. It flags a division by
   equity whose numerator came from `market_value` WITHOUT a leverage
   multiplier, which is what actually distinguishes the broken sites from the
   sound ones.

2. Every failure names the file, the line, the enclosing function and the
   sanctioned function that should have been called instead. A guard nobody
   can act on gets deleted.

Changing the registry is allowed and expected — consolidating a quantity into a
different owner than the one named here is a one-line edit. Making that edit
deliberately, in a reviewed diff, is the entire point.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
from pathlib import Path

SRC = Path(__file__).parent.parent / "src"
REPO = Path(__file__).parent.parent


# ---------------------------------------------------------------------------
# AST helpers.
# ---------------------------------------------------------------------------


def _name(node: ast.AST | None) -> str | None:
    """The identifier a Name or Attribute node ends in (`p.market_value` -> ...)."""
    return getattr(node, "id", None) or getattr(node, "attr", None)


def _names(node: ast.AST) -> set[str]:
    return {_name(n) for n in ast.walk(node) if isinstance(n, (ast.Name, ast.Attribute))} - {None}


def _calls(node: ast.AST) -> set[str]:
    return {_name(n.func) for n in ast.walk(node) if isinstance(n, ast.Call)} - {None}


def _taint(fn: ast.AST, seeds: set[str], rounds: int = 8) -> set[str]:
    """Local names reachable from `seeds` by assignment, to a fixed point.

    `current_symbol_raw = sum(p.market_value ...)` makes `current_symbol_raw`
    a stand-in for market value; `net_exposure = current_net + pending` makes
    it one at two removes. Without this, renaming an intermediate variable
    would hide a site from the guard — which is not a check, it is a
    formality.
    """
    tainted = set(seeds)
    for _ in range(rounds):
        before = set(tainted)
        for node in ast.walk(fn):
            if not isinstance(node, (ast.Assign, ast.AugAssign, ast.AnnAssign)):
                continue
            if node.value is None or not (_names(node.value) & tainted):
                continue
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for t in targets:
                if (n := _name(t)) is not None:
                    tainted.add(n)
        if tainted == before:
            break
    return tainted


def _parents(tree: ast.AST) -> dict[ast.AST, ast.AST]:
    return {c: n for n in ast.walk(tree) for c in ast.iter_child_nodes(n)}


def _functions(tree: ast.AST):
    for n in ast.walk(tree):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield n


def _rel(path: Path) -> str:
    return str(path.relative_to(REPO))


@dataclass(frozen=True)
class Finding:
    file: str
    line: int
    func: str
    snippet: str
    note: str = ""

    def __str__(self) -> str:
        tail = f"  [{self.note}]" if self.note else ""
        return f"{self.file}:{self.line}  in {self.func}()  {self.snippet}{tail}"


@dataclass(frozen=True)
class Quantity:
    """One named quantity that must have exactly one definition."""

    name: str
    #: What goes wrong in the live book when two definitions coexist.
    cost: str
    #: The function allowed to compute it, as "path::function — free note".
    #: Parsed by `test_every_registry_entry_names_a_real_owner`, so the
    #: path::function head must stay machine-readable; prose goes after the
    #: em dash.
    owner: str
    #: Extra (file, function) sites permitted to contain the pattern.
    allow: frozenset[tuple[str, str]] = field(default_factory=frozenset)


# ---------------------------------------------------------------------------
# Matchers. Each returns every site whose SHAPE is a second definition.
# ---------------------------------------------------------------------------

_EQUITY = {"total_value", "equity", "portfolio_value", "total_equity", "account_value"}
_MV_SEED = {"market_value", "mv"}
_LEVERAGE = {
    "_gross_multiplier",
    "_effective_multiplier",
    "gross_multiplier",
    "effective_multiplier",
    "gross_mul",
    "signed_mul",
}


def find_percent_of_book(tree: ast.AST, path: Path) -> list[Finding]:
    """A share-of-equity percentage computed WITHOUT leverage awareness.

    Two shapes are wrong:

      `<market-value-derived> / equity`  with no leverage multiplier anywhere
          in the numerator — raw notional masquerading as exposure. A 3x
          inverse ETF contributes its market value instead of three times it.

      `equity - cash`  as a measure of invested capital — which goes NEGATIVE
          on a net-short book, because short proceeds push cash above equity.

    Deliberately silent on the eleven sites that DO carry a multiplier, and on
    every division by equity whose numerator is not market value (cash
    percentage, risk dollars, daily loss) — those are different quantities that
    merely share a denominator.
    """
    out: list[Finding] = []
    for fn in _functions(tree):
        mv = _taint(fn, _MV_SEED)
        lev = _taint(fn, set(_LEVERAGE))
        for node in ast.walk(fn):
            if not isinstance(node, ast.BinOp):
                continue
            if isinstance(node.op, ast.Div):
                if _name(node.right) in _EQUITY and (_names(node.left) & mv):
                    if not (_names(node.left) & lev):
                        out.append(
                            Finding(
                                _rel(path),
                                node.lineno,
                                fn.name,
                                ast.unparse(node)[:72],
                                "raw notional — no leverage multiplier",
                            )
                        )
            elif isinstance(node.op, ast.Sub):
                if _name(node.left) in _EQUITY and ("cash" in _names(node.right)):
                    out.append(
                        Finding(
                            _rel(path),
                            node.lineno,
                            fn.name,
                            ast.unparse(node)[:72],
                            "equity - cash goes NEGATIVE on a net-short book",
                        )
                    )
    return _dedupe(out)


_ENTRY = {"avg_entry", "entry", "entry_price", "avg_entry_price"}
_QTY = {"qty", "quantity", "shares"}


def find_unsigned_cost_basis(tree: ast.AST, path: Path) -> list[Finding]:
    """`avg_entry * qty` that is not wrapped in `abs()`.

    A short carries a negative qty, so its cost basis is negative and any
    `if cost_basis > 0` guard silently returns 0.0 — a winning short renders
    as `+$1000.00 (+0.0%)`. The denominator must be the MAGNITUDE of capital
    at risk; the sign the reader needs already lives in `unrealized_pnl`.
    """
    parents = _parents(tree)
    out: list[Finding] = []
    for fn in _functions(tree):
        for node in ast.walk(fn):
            if not (isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mult)):
                continue
            sides = {_name(node.left), _name(node.right)}
            if not (sides & _ENTRY and sides & _QTY):
                continue
            parent = parents.get(node)
            if isinstance(parent, ast.Call) and _name(parent.func) == "abs":
                continue
            out.append(
                Finding(
                    _rel(path),
                    node.lineno,
                    fn.name,
                    ast.unparse(node)[:72],
                    "unsigned cost basis — a short's is NEGATIVE",
                )
            )
    return _dedupe(out)


def find_atr_definitions(tree: ast.AST, path: Path) -> list[Finding]:
    """Any function that produces an average of true ranges.

    Matches two shapes that share no identifier with each other:

      a function that derives true ranges AND averages them itself
          (`_true_ranges(...)` followed by `np.convolve` / `np.mean`), and
      a call into a library ATR (`ta.volatility.AverageTrueRange`).

    The first is a simple moving average, the second is Wilder's. They differ
    7-8% on average and 39% on the worst day measured. ATR sets stop distance
    and stop distance sets position size, so two ATRs are two books.

    Note `_ma_daily_change` in the same module also calls `np.convolve` and is NOT
    matched — it convolves closes, never true ranges.
    """
    out: list[Finding] = []
    for fn in _functions(tree):
        calls = _calls(fn)
        # Deliberately NOT matching a bare local named `tr` — too common a
        # name to carry a guard on. `_atr_series` is still matched because it
        # CALLS `_true_ranges`, and any honest re-implementation must either
        # call a true-range helper or name the concept in full.
        derives_tr = "_true_ranges" in calls or bool({"true_range", "true_ranges"} & _names(fn))
        averages = bool(calls & {"convolve", "mean", "nanmean", "average", "rolling"})
        wilder = bool(calls & {"AverageTrueRange", "average_true_range", "atr"})
        if wilder:
            out.append(Finding(_rel(path), fn.lineno, fn.name, "library ATR (Wilder)", "Wilder smoothing"))
        elif derives_tr and averages:
            out.append(
                Finding(_rel(path), fn.lineno, fn.name, "true ranges averaged in-place", "simple moving average")
            )
    return _dedupe(out)


_CLOSE = {"close", "close_attr", "closing_price"}
_VOLUME = {"volume", "vol", "vol_attr"}


def find_dollar_volume_definitions(tree: ast.AST, path: Path) -> list[Finding]:
    """`close * volume` aggregated over a bar window.

    Three live definitions were measured, differing in window length (20 bars
    versus `_W_1M` = 21) and in whether a halted zero-volume session is kept
    or dropped. A 5.26% spread — enough to flip a symbol's admission at the
    liquidity floor.
    """
    out: list[Finding] = []
    for fn in _functions(tree):
        for node in ast.walk(fn):
            if not (isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mult)):
                continue
            names = _names(node)
            if (names & _CLOSE) and (names & _VOLUME):
                out.append(Finding(_rel(path), node.lineno, fn.name, ast.unparse(node)[:72], "dollar volume"))
    return _dedupe(out)


def find_deployable_cash_definitions(tree: ast.AST, path: Path) -> list[Finding]:
    """A `deployable*` name assigned from arithmetic instead of the owner.

    Engine: `cash + parked_sweep`; API: `max(cash - reserve, 0)`.
    Measured $54,000 versus $33,000 on one book, both published under the same
    field name. Assignments of a literal default (`0.0`, `None`) are not
    definitions and are not matched.
    """
    out: list[Finding] = []
    for fn in _functions(tree):
        for node in ast.walk(fn):
            if not isinstance(node, (ast.Assign, ast.AnnAssign)) or node.value is None:
                continue
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if not any("deployable" in (_name(t) or "") for t in targets):
                continue
            value = node.value
            if isinstance(value, ast.Constant):
                continue  # a literal default, not a definition
            # Exempt ONLY a call to/alias of a NAMED owner; an alias of anything else is a second definition (slipped through twice).
            owners = {"_compute_deployable_cash", "compute_deployable_cash", "deployable_cash", "cash_above_reserve"}
            if _calls(value) & owners or (isinstance(value, (ast.Name, ast.Attribute)) and _name(value) in owners):
                continue  # delegates to / aliases the owner
            out.append(Finding(_rel(path), node.lineno, fn.name, ast.unparse(value)[:72], "second definition"))
    return _dedupe(out)


def _dedupe(findings: list[Finding]) -> list[Finding]:
    """One report per source line — a nested function is not a second site."""
    seen: set[tuple[str, int]] = set()
    out: list[Finding] = []
    for f in findings:
        if (f.file, f.line) in seen:
            continue
        seen.add((f.file, f.line))
        out.append(f)
    return out


# ---------------------------------------------------------------------------
# THE REGISTRY. One entry per quantity that must have exactly one definition.
# ---------------------------------------------------------------------------

MATCHERS = {
    "percent of book": find_percent_of_book,
    "unrealized P&L cost basis": find_unsigned_cost_basis,
    "average true range": find_atr_definitions,
    "average dollar volume": find_dollar_volume_definitions,
    "deployable cash": find_deployable_cash_definitions,
}

REGISTRY: dict[str, Quantity] = {
    "percent of book": Quantity(
        name="percent of book",
        cost=(
            "the PM is told the book is 60% invested while the risk gate enforces 100%, both against the same target"
        ),
        # Owner MOVED 2026-09-02: the leverage table and its multipliers were
        # extracted to the pure quantities module so src/api could derive its
        # inverse-ETF roster from the one table instead of hand-maintaining a
        # copy. src/risk/rules.py imports it under the old private alias, so
        # every existing call site is unchanged.
        owner="src/quantities.py::effective_multiplier — leverage-aware exposure",
        # No site may compute a raw share-of-equity. Every legitimate one
        # carries a multiplier and is therefore never matched — the allowlist
        # is empty on purpose.
        allow=frozenset(),
    ),
    "unrealized P&L cost basis": Quantity(
        name="unrealized P&L cost basis",
        cost="a winning short prints '+$1000.00 (+0.0%)'",
        owner="src/agents/position_reviewer.py::_pnl_pct — abs(avg_entry * qty)",
        allow=frozenset(),
    ),
    "average true range": Quantity(
        name="average true range",
        cost=(
            "7-8% mean divergence, 39% on the worst day; ATR sets stop distance and stop distance sets position size"
        ),
        # Owner MOVED 2026-09-02 when the fix landed: the shared Wilder
        # implementation was extracted from compute_indicators into
        # atr_series, which context.py now calls instead of keeping its own.
        # Changing an owner is a deliberate, reviewed registry edit — that is
        # the workflow, not a workaround.
        owner="src/data/technical.py::atr_series — Wilder, via `ta`",
        allow=frozenset({("src/data/technical.py", "atr_series"), ("src/data/technical.py", "compute_indicators")}),
    ),
    "average dollar volume": Quantity(
        name="average dollar volume",
        cost="5.26% spread — enough to flip a symbol's admission",
        # Owner MOVED 2026-09-02: consolidated into the pure quantities
        # module so the API can use it without importing the risk stack.
        owner="src/quantities.py::avg_dollar_volume",
        allow=frozenset({("src/quantities.py", "avg_dollar_volume"), ("src/quantities.py", "dollar_volumes")}),
    ),
    "deployable cash": Quantity(
        name="deployable cash",
        cost="$54,000 to the engine and $33,000 to the dashboard, same book",
        # Owner MOVED 2026-09-02: same reason — the dashboard must read the
        # engine's number, and src/api may not import src.pipeline.
        owner="src/quantities.py::deployable_cash",
        allow=frozenset(
            {
                ("src/quantities.py", "deployable_cash"),
                ("src/quantities.py", "cash_above_reserve"),
                ("src/quantities.py", "sweep_reserve_usd"),
            }
        ),
    ),
}


def _source_modules() -> list[Path]:
    """Every module in the package, derived — never a hard-coded file list."""
    return sorted(SRC.rglob("*.py"))


def test_the_package_scan_is_not_silently_empty():
    """A scan that matches nothing is worse than a missing test: it is green.

    If `src/` is ever restructured so the glob stops seeing the package, this
    fails by name instead of every quantity guard quietly passing on zero
    files.
    """
    modules = _source_modules()
    assert len(modules) >= 25, (
        f"the src/ scan found almost nothing ({len(modules)} modules); every "
        "one-definition guard would be passing vacuously"
    )
    parsed = sum(1 for p in modules if _safe_parse(p) is not None)
    assert parsed >= len(modules) - 1, (
        f"only {parsed} of {len(modules)} src/ modules parsed; the guards are skipping real code"
    )


def _safe_parse(path: Path):
    try:
        return ast.parse(path.read_text(), filename=str(path))
    except SyntaxError:  # pragma: no cover - a parse error is another test's job
        return None


def _definition_sites() -> dict[str, set[str]]:
    """Function name -> every repo-relative file that defines it, derived.

    This is what makes the registry move-proof. The allowlists below name a
    FILE and a FUNCTION; when a sanctioned function is moved to a different
    module, the pair stops matching even though nothing about the quantity
    changed. Looking the function up by name lets the allowlist follow the
    move — but only while the name resolves to exactly ONE file, which is
    precisely the invariant this whole file exists to enforce.
    """
    sites: dict[str, set[str]] = {}
    repo = REPO.resolve()
    for path in _source_modules():
        tree = _safe_parse(path)
        if tree is None:
            continue
        rel = str(path.resolve().relative_to(repo))
        for func in _functions(tree):
            sites.setdefault(func.name, set()).add(rel)
    return sites


_DEFINITION_SITES = _definition_sites()


def _resolve_site(file: str, func: str) -> str:
    """The file `func` actually lives in now, if the named one no longer has it."""
    where = _DEFINITION_SITES.get(func, set())
    if file in where or len(where) != 1:
        return file
    return next(iter(where))


def _resolved_pairs(pairs) -> set[tuple[str, str]]:
    return {(_resolve_site(file, func), func) for file, func in pairs}


def _scan(matcher) -> list[Finding]:
    out: list[Finding] = []
    for path in _source_modules():
        tree = _safe_parse(path)
        if tree is None:
            continue
        out.extend(matcher(tree, path))
    return out


def _violations(quantity: Quantity, findings: list[Finding]) -> list[Finding]:
    allowed = _resolved_pairs(quantity.allow)
    return [f for f in findings if (f.file, f.func) not in allowed]


def _report(quantity: Quantity, violations: list[Finding]) -> str:
    plural = "site" if len(violations) == 1 else "sites"
    lines = [
        "",
        f"  '{quantity.name}' is computed outside its sanctioned owner at {len(violations)} {plural}.",
        "  It must have exactly one definition.",
        f"  Sanctioned owner: {quantity.owner}",
        f"  What a second definition costs: {quantity.cost}",
        "",
        "  Re-implemented at:",
    ]
    lines += [f"      {v}" for v in violations]
    lines += [
        "",
        "  Call the sanctioned function instead. If this site genuinely needs a",
        "  different number, it needs a different NAME and its own registry entry",
        "  in tests/test_one_definition_guard.py — a different THRESHOLD on the",
        "  same quantity is fine and needs no change here.",
        "",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# The guard itself. One test per quantity, so a failure names the quantity.
# ---------------------------------------------------------------------------


def test_percent_of_book_has_exactly_one_definition():
    q = REGISTRY["percent of book"]
    v = _violations(q, _scan(MATCHERS["percent of book"]))
    assert not v, _report(q, v)


def test_cost_basis_is_always_unsigned():
    q = REGISTRY["unrealized P&L cost basis"]
    v = _violations(q, _scan(MATCHERS["unrealized P&L cost basis"]))
    assert not v, _report(q, v)


def test_average_true_range_has_exactly_one_definition():
    q = REGISTRY["average true range"]
    v = _violations(q, _scan(MATCHERS["average true range"]))
    assert not v, _report(q, v)


def test_average_dollar_volume_has_exactly_one_definition():
    q = REGISTRY["average dollar volume"]
    v = _violations(q, _scan(MATCHERS["average dollar volume"]))
    assert not v, _report(q, v)


def test_deployable_cash_has_exactly_one_definition():
    q = REGISTRY["deployable cash"]
    v = _violations(q, _scan(MATCHERS["deployable cash"]))
    assert not v, _report(q, v)


# ---------------------------------------------------------------------------
# The dashboard. TypeScript, so this one is textual and says so.
# ---------------------------------------------------------------------------


def test_the_dashboard_does_not_compute_its_own_percent_deployed():
    """`HeroBand.tsx` sums market values itself and draws them against the
    engine's ceiling.

    The needle and the redline come from different definitions: 46% to the
    engine, 22% to the dashboard, on one book. This is the only defect in the
    set a human sees directly.

    Honest limitation: this is a TEXT match, not an AST match — there is no
    TypeScript parser in this suite's dependencies. It recognises the specific
    shape that shipped and will not generalise the way the Python matchers do.
    """
    frontend = REPO / "frontend" / "src"
    if not frontend.exists():  # pragma: no cover - frontend is optional in CI
        import pytest

        pytest.skip("frontend/src not present")

    offenders: list[str] = []
    for path in sorted(frontend.rglob("*.tsx")):
        for i, line in enumerate(path.read_text().splitlines(), 1):
            stripped = line.strip()
            if "market_value" not in stripped:
                continue
            # A reduce() accumulating market values is the dashboard computing
            # exposure for itself rather than rendering a server-sent figure.
            # A market-value sum is only a second definition of PERCENT
            # DEPLOYED when it is divided to make a share. Summing long
            # and short notional for their own tiles is legitimate and
            # must not fire, or this guard gets switched off.
            divides = (
                "/ portfolio_value" in stripped
                or "/ (portfolio_value" in stripped
                or "/ equity" in stripped
                or "/ total_value" in stripped
            )
            if "reduce" in stripped and "sum" in stripped and divides:
                offenders.append(f"{path.relative_to(REPO)}:{i}  {stripped[:80]}")

    assert not offenders, (
        "\n  'percent deployed' is computed in the browser as well as in the "
        "engine."
        "\n  Sanctioned owner: the engine — serve the figure through the API and "
        "render it."
        "\n  What a second definition costs: the gauge's needle and its redline "
        "come\n  from different definitions (46% engine vs 22% dashboard on one "
        "book).\n"
        "\n  Computed in the browser at:\n" + "\n".join(f"      {o}" for o in offenders) + "\n"
    )


# ---------------------------------------------------------------------------
# Guards on the guard. These must pass on EVERY branch, including this one.
# ---------------------------------------------------------------------------


def _equity_arithmetic_functions() -> set[tuple[str, str]]:
    """Every function in src that divides by an equity-named quantity, DERIVED.

    This replaces a hand-kept list of "code the survey found sound". The list
    could go stale in two directions (a listed function deleted, a new sound
    function never listed). The question it answered was "does a matcher fire
    on correct equity arithmetic?", and the population of correct equity
    arithmetic is computable: it is every function whose body divides by an
    equity name. Those are exactly the sites a false positive would land on,
    including the largest one (`RiskRuleEngine.check`).
    """
    out: set[tuple[str, str]] = set()
    for path in _source_modules():
        tree = _safe_parse(path)
        if tree is None:
            continue
        rel = str(path.resolve().relative_to(REPO.resolve()))
        for fn in _functions(tree):
            if any(
                isinstance(n, ast.BinOp) and isinstance(n.op, ast.Div) and _names(n.right) & _EQUITY
                for n in ast.walk(fn)
            ):
                out.add((rel, fn.name))
    return out


def test_the_guard_stays_silent_on_code_that_is_already_sound():
    """No matcher may flag a function that divides by equity unless the
    registry sanctions it. Computed from the live source at check time.

    A guard that flags correct code is disabled within a week and then there
    is nothing, so the cost of a false positive is the whole apparatus.
    """
    sound = _equity_arithmetic_functions()
    assert sound, "no equity-dividing function found: the population scan is broken"
    sanctioned = set().union(*(_resolved_pairs(q.allow) for q in REGISTRY.values()))
    flagged = [
        f
        for matcher in MATCHERS.values()
        for f in _scan(matcher)
        if (f.file, f.func) in sound and (f.file, f.func) not in sanctioned
    ]
    assert not flagged, (
        "the guard flagged a function that does ordinary equity arithmetic "
        "and the registry does not sanction it:\n" + "\n".join(f"    {f}" for f in flagged)
    )


def test_the_equity_arithmetic_population_is_not_vacuous():
    """The scan above must keep finding the big leverage-aware functions."""
    sound = _equity_arithmetic_functions()
    assert len(sound) >= 3, sorted(sound)
    assert any(func == "check" for _, func in sound), (
        "RiskRuleEngine.check no longer divides by equity: the scan lost the largest false-positive target"
    )


def test_every_registry_entry_names_a_real_owner():
    """A registry pointing at a function that no longer exists is not a guard.

    Catches the rename that would otherwise turn an entry into decoration.
    """
    missing: list[str] = []
    for q in REGISTRY.values():
        head, _, rest = q.owner.partition("::")
        head = head.strip()
        owner_func = rest.split("—")[0].strip()
        if owner_func:
            head = _resolve_site(head, owner_func)
        owner_path = REPO / head
        if not owner_path.exists():
            missing.append(f"{q.name}: owner file {head} does not exist")
        elif owner_func:
            tree = ast.parse(owner_path.read_text(), filename=str(owner_path))
            if owner_func not in {f.name for f in _functions(tree)}:
                missing.append(f"{q.name}: owner {head}::{owner_func}() does not exist")
        for file, func in sorted(_resolved_pairs(q.allow)):
            path = REPO / file
            if not path.exists():
                missing.append(f"{q.name}: allowlisted {file} does not exist")
                continue
            tree = ast.parse(path.read_text(), filename=str(path))
            if func not in {f.name for f in _functions(tree)}:
                missing.append(f"{q.name}: allowlisted {file}::{func}() does not exist")
    assert not missing, "stale registry entries:\n    " + "\n    ".join(missing)


def test_the_matchers_still_match_something_they_are_meant_to():
    """A matcher that silently stops matching is worse than no matcher.

    Each pattern is exercised against a snippet carrying the exact defect
    shape, so a refactor that breaks the AST walk fails HERE — loudly — rather
    than turning the whole file into a green no-op.
    """
    cases = [
        (
            find_percent_of_book,
            "def f(positions, total_value):\n    return sum(p.market_value for p in positions) / total_value * 100\n",
        ),
        (
            find_unsigned_cost_basis,
            "def f(p):\n    return p.unrealized_pnl / (p.avg_entry * p.qty)\n",
        ),
        (
            find_dollar_volume_definitions,
            "def f(bars):\n    return sum(b.close * b.volume for b in bars)\n",
        ),
        (
            find_deployable_cash_definitions,
            "def f(cash, reserve):\n    deployable_cash = max(cash - reserve, 0)\n    return deployable_cash\n",
        ),
        (
            find_atr_definitions,
            "def f(bars):\n    tr = _true_ranges(bars)\n    return np.convolve(tr, kernel, mode='valid')\n",
        ),
    ]
    dead = []
    for matcher, snippet in cases:
        tree = ast.parse(snippet)
        if not matcher(tree, SRC / "synthetic.py"):
            dead.append(matcher.__name__)
    assert not dead, (
        "these matchers no longer recognise the defect they were written for, "
        "so the tests using them are passing vacuously:\n    " + "\n    ".join(dead)
    )


def test_the_matchers_do_not_fire_on_sound_code():
    """The other half: correct code must produce no findings.

    Every snippet is a real shape from the codebase that is CORRECT — a
    leverage-aware weight, an abs() cost basis, a convolve over closes rather
    than true ranges, a cash percentage. If any of these start failing, the
    guard has become a nuisance and will be deleted.
    """
    cases = [
        (
            find_percent_of_book,
            "def f(p, total_value):\n    return p.market_value * _gross_multiplier(p.symbol) / total_value * 100\n",
        ),
        (
            find_percent_of_book,
            "def f(cash, total_value):\n    return cash / total_value * 100\n",
        ),
        (
            find_percent_of_book,
            "def f(positions, total_value):\n"
            "    net = sum(p.market_value * _effective_multiplier(p.symbol) for p in positions)\n"
            "    return abs(net) / total_value * 100\n",
        ),
        (
            find_unsigned_cost_basis,
            "def f(p):\n    cost = abs(p.avg_entry * p.qty)\n    return p.unrealized_pnl / cost\n",
        ),
        (
            find_atr_definitions,
            "def f(closes, period, lookback):\n"
            "    kernel = np.ones(period) / period\n"
            "    return np.convolve(closes, kernel, mode='valid')\n",
        ),
        (
            find_deployable_cash_definitions,
            "def f(self, cash, positions):\n"
            "    deployable = self._compute_deployable_cash(cash, positions)\n"
            "    return deployable\n",
        ),
    ]
    noisy = []
    for matcher, snippet in cases:
        tree = ast.parse(snippet)
        found = matcher(tree, SRC / "synthetic.py")
        if found:
            noisy.append(f"{matcher.__name__}: {[str(f) for f in found]}")
    assert not noisy, (
        "the guard fired on code that is correct — this is how guards get switched off:\n    " + "\n    ".join(noisy)
    )
