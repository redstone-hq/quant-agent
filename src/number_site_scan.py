"""Numeric definition-site scanner, lifted verbatim from `src/number_sources.py`.

Pure functions over a parsed module: given a syntax tree and the names it
binds, they return the `NumberSite`s the ledger must account for (rules
(a)-(e) of `src.number_sources`). Nothing here reads the ledger, the settings
file, the repository scope list or the ratchet history; every input arrives
as an argument. `src.number_sources` re-exports every name so all existing
imports keep working unchanged and `_scan_module` stays where it was.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path


#: Config classes inside scoped files whose numeric field defaults are sites.
#: Matched by SUFFIX, so a new `FooConfig` is covered the day it is written.
CONFIG_CLASS_SUFFIX = "Config"


#: Zero alone is excluded as a definition site: it is the empty/neutral
#: default on a result field and the bottom of an ordinal scale, and no
#: reviewer has anything to say about it.
#:
#: 1.0 USED TO BE EXCLUDED HERE, justified as "the identity, not a setting"
#: with the claim that every audited number sat outside the set. That claim
#: was false and the exclusion hid real settings:
#: `absolute_min_stop_atr_multiple = 1.0` (in both `RiskConfig` and
#: `ConstructorConfig`), `min_target_atr_multiple = 1.0`,
#: `breakout_projection_atr_multiple = 1.0`, `NOISE_BAND_ATR_MULTIPLE = 1.0`
#: and `CashReserveConfig.pct = 1.0`. One ATR is not an identity — it
#: is the hard floor under every stop this desk sets.
NEUTRAL_VALUES: frozenset[float] = frozenset({0.0})


@dataclass(frozen=True)
class NumberSite:
    """One numeric definition site the ledger must account for."""

    site_id: str
    path: str
    lineno: int
    value: float

    def __str__(self) -> str:  # pragma: no cover - diagnostics only
        return f"{self.site_id} = {self.value!r}  ({self.path}:{self.lineno})"


def _numeric(node: ast.AST, names: dict[str, float] | None = None) -> float | None:
    """The literal value of `node`, or None if it is not a number.

    Handles the unary minus that `ast` represents as an operator rather than
    as part of the constant, so `-20.0` is one site and not a miss.

    `names` maps module-level constant names to their values (the module's own
    plus the ones it imports from other repo modules). Without it, a default
    bound to a NAME rather than to a literal returns None and the number
    vanishes from the gate entirely — which was a one-line way to hide any
    number by pointing a scoped field at an unscoped module. Constant
    arithmetic (`5 * 366`) is folded for the same reason.
    """
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        if isinstance(node.value, bool):
            return None
        return float(node.value)
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
        inner = _numeric(node.operand, names)
        return None if inner is None else -inner
    if names and isinstance(node, ast.Name):
        return names.get(node.id)
    if isinstance(node, ast.BinOp):
        left = _numeric(node.left, names)
        right = _numeric(node.right, names)
        if left is None or right is None:
            return None
        try:
            if isinstance(node.op, ast.Add):
                return left + right
            if isinstance(node.op, ast.Sub):
                return left - right
            if isinstance(node.op, ast.Mult):
                return left * right
            if isinstance(node.op, ast.Div):
                return left / right
            if isinstance(node.op, ast.Pow):
                return float(left**right)
        except (ZeroDivisionError, OverflowError, ValueError):
            return None
    return None


def _module_constants(tree: ast.Module) -> dict[str, float]:
    """Module-level names bound to a plain number, in definition order.

    Deliberately shallow: a name bound to a call, a container or a comprehension
    is not resolved, because the point is to follow the one-hop indirection an
    author reaches for, not to evaluate the module.
    """
    out: dict[str, float] = {}
    for node in tree.body:
        targets: list[ast.expr]
        if isinstance(node, ast.Assign):
            targets = list(node.targets)
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
        else:
            continue
        if node.value is None:
            continue
        for target in targets:
            for name, value_node in _bound_name_values(target, node.value):
                value = _numeric(value_node, out)
                if value is not None:
                    out[name.id] = value
    return out


def _bound_name_values(target: ast.AST, value: ast.AST) -> list[tuple[ast.Name, ast.AST]]:
    """Names bound directly to numeric-shaped values by one assignment.

    Python permits constants to be destructured in one statement, for example
    ``_WEEK, _MONTH = 5, 21``.  Treating the entire target as non-name made
    every such value invisible both to the module-constant resolver and to
    rule (a)'s source scan.  Pair only equal-length tuple/list shapes; starred
    or dynamically sized unpacking is intentionally not evaluated.
    """
    if isinstance(target, ast.Name):
        return [(target, value)]
    if isinstance(target, (ast.Tuple, ast.List)) and isinstance(value, (ast.Tuple, ast.List)):
        if len(target.elts) != len(value.elts):
            return []
        pairs: list[tuple[ast.Name, ast.AST]] = []
        for child_target, child_value in zip(target.elts, value.elts):
            pairs.extend(_bound_name_values(child_target, child_value))
        return pairs
    return []


def _imported_constants(tree: ast.Module, root: Path) -> dict[str, float]:
    """Numeric constants this module imports by name from other repo modules.

    `from src.risk.constants import STARTER_POSITION_RISK_PCT` makes that
    number the live default of a scoped field, so the gate has to see it
    whether or not `src/risk/constants.py` is itself in scope. That is the
    whole evasion: point the name somewhere unscoped and the number is gone.
    """
    out: dict[str, float] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom) or not node.module:
            continue
        if not node.module.startswith("src."):
            continue
        source = root / (node.module.replace(".", "/") + ".py")
        if not source.is_file():
            continue
        try:
            constants = _module_constants(ast.parse(source.read_text(encoding="utf-8")))
        except SyntaxError:  # pragma: no cover - a broken file fails elsewhere
            continue
        for alias in node.names:
            if alias.name in constants:
                out[alias.asname or alias.name] = constants[alias.name]
    return out


def _leaves(
    node: ast.AST,
    prefix: str,
    names: dict[str, float] | None = None,
    local: dict[str, float] | None = None,
) -> list[tuple[str, float, int]]:
    """Every numeric leaf under `node`, with a stable path-qualified id.

    A bare literal yields one leaf. A tuple, list or dict literal yields one
    leaf per numeric element, keyed by index, by its literal key, or by the
    explicit name used as its key, so `GROSS_LADDER`'s second rung is
    addressable as `...GROSS_LADDER[1][1]` and
    `_WEIGHTS[INDETERMINATE]` does not collide with the other named keys in
    the same mapping.

    `local` names the constants defined in this same file. A leaf that is
    just one of those names is NOT a second site — it is one number with two
    names, and ledgering it twice is how the flagship entry came to say a
    number existed in one place while the ledger listed it in two. A name
    bound to a number from ANOTHER module is still a site, because that is
    where the number enters this file.
    """
    if local and isinstance(node, ast.Name) and node.id in local:
        return []
    value = _numeric(node, names)
    if value is not None:
        return [(prefix, value, getattr(node, "lineno", 0))]

    out: list[tuple[str, float, int]] = []
    if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        for index, element in enumerate(node.elts):
            out.extend(_leaves(element, f"{prefix}[{index}]", names, local))
    elif isinstance(node, ast.Dict):
        for key, element in zip(node.keys, node.values):
            if isinstance(key, ast.Constant):
                label = f"[{key.value!r}]"
            elif isinstance(key, ast.Name):
                label = f"[{key.id}]"
            else:
                label = "[?]"
            out.extend(_leaves(element, f"{prefix}{label}", names, local))
    return out


def _field_default(node: ast.AnnAssign) -> ast.AST | None:
    """The default expression of an annotated class field, or None.

    Covers a bare default (`x: float = 2.5`), a dataclass `field(default=...)`
    and a pydantic `Field(2.5, ...)` / `Field(default=2.5)`. A
    `default_factory` is deliberately NOT followed: the number then lives in
    a function body, which is out of scope and honestly declared as such.
    """
    if node.value is None:
        return None
    if isinstance(node.value, ast.Call):
        for keyword in node.value.keywords:
            if keyword.arg == "default":
                return keyword.value
        if node.value.args:
            return node.value.args[0]
        return None
    return node.value


#: Rule (e)'s band. A literal in [0.5, 2.0), other than 1.0, used as a
#: multiplier or divisor is a price or size scaled by a policy margin — a
#: limit 0.5% through the market, a 2% proceeds cushion, a 1.25 ATR noise
#: floor (that noise floor was 1.25 when the band was drawn; it is 1.0 ATR
#: by owner ruling since 2026-10-09). The band is a CLASSIFIER, not a trade number: it was chosen by
#: listing every literal operand of arithmetic in scope on 2026-09-19 (about
#: seventy) and finding that everything outside it is a unit conversion
#: (10_000 bps, 365 days, 60 s, 1_000_000), a float epsilon, or a query
#: padding, while everything inside it is an order-price or sizing margin.
#: 2.0 itself is excluded because halving/doubling is overwhelmingly an
#: identity of the arithmetic (a midpoint) rather than a chosen margin.
FACTOR_BAND: tuple[float, float] = (0.5, 2.0)


def _qualified_scopes(tree: ast.Module):
    """Yield `(node, qualname)` for every function and class in the module.

    `qualname` is the dotted chain of enclosing class and function names,
    the same path a reader would use to find the definition.
    """

    def visit(node: ast.AST, prefix: str):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                qual = f"{prefix}.{child.name}" if prefix else child.name
                yield child, qual
                yield from visit(child, qual)
            else:
                yield from visit(child, prefix)

    yield from visit(tree, "")


def _factor_operands(node: ast.AST) -> list[ast.AST]:
    """The operand itself, or both branches of `(a if cond else b)`."""
    if isinstance(node, ast.IfExp):
        return _factor_operands(node.body) + _factor_operands(node.orelse)
    return [node]


def _own_nodes(func: ast.AST):
    """Every node in `func`'s body that is not inside a nested def or class.

    A nested function's literals belong to that function's own qualname, so
    an edit inside one never renumbers the other's sites.
    """
    stack = list(ast.iter_child_nodes(func))
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
            continue
        yield node
        stack.extend(ast.iter_child_nodes(node))


def _scan_extended_shapes(
    tree: ast.Module,
    module: str,
    rel: str,
    names: dict[str, float],
    local: dict[str, float],
) -> list[NumberSite]:
    """Rules (c), (d) and (e): the shapes rules (a)/(b) cannot see.

    Applied only inside a scoped module — never to `src/config/__init__.py`'s named
    classes and never to the unscoped sentinel, whose count is defined as
    module-level constants and would otherwise jump for no reason.
    """
    sites: list[NumberSite] = []
    for node, qual in _qualified_scopes(tree):
        # (c) numeric defaults on function and method parameters.
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            args = node.args
            positional = list(args.posonlyargs) + list(args.args)
            pairs = list(zip(positional[len(positional) - len(args.defaults) :], args.defaults))
            pairs += [(arg, default) for arg, default in zip(args.kwonlyargs, args.kw_defaults) if default is not None]
            for arg, default in pairs:
                base = f"{module}.{qual}({arg.arg})"
                for site_id, value, lineno in _leaves(default, base, names, local):
                    if value not in NEUTRAL_VALUES:
                        sites.append(NumberSite(site_id, rel, lineno or node.lineno, value))

            # (e) inline multiplier/divisor literals in the near-one band.
            ordinal = 0
            found: list[tuple[int, int, float]] = []
            for inner in _own_nodes(node):
                if not isinstance(inner, ast.BinOp) or not isinstance(inner.op, (ast.Mult, ast.Div)):
                    continue
                for side, other in ((inner.left, inner.right), (inner.right, inner.left)):
                    if _numeric(other) is not None:
                        # Constant arithmetic (`365 * 5`) is one folded number,
                        # not a margin applied to a price.
                        continue
                    for operand in _factor_operands(side):
                        value = _numeric(operand)
                        # +-1 is the identity or a sign flip, never a margin.
                        if value is None or abs(value) == 1.0:
                            continue
                        low, high = FACTOR_BAND
                        if low <= abs(value) < high:
                            found.append((operand.lineno, operand.col_offset, value))
            for lineno, _col, value in sorted(found):
                sites.append(NumberSite(f"{module}.{qual}:factor[{ordinal}]", rel, lineno, value))
                ordinal += 1

        # (d) numeric attributes on any class. A `*Config` class's annotated
        # fields are rule (b) already; its un-annotated attributes are not,
        # so those are picked up here too.
        if isinstance(node, ast.ClassDef):
            is_config = node.name.endswith(CONFIG_CLASS_SUFFIX)
            for body_node in node.body:
                if isinstance(body_node, ast.AnnAssign):
                    if is_config or not isinstance(body_node.target, ast.Name):
                        continue
                    default = _field_default(body_node)
                    targets = [body_node.target]
                elif isinstance(body_node, ast.Assign):
                    default = body_node.value
                    targets = [t for t in body_node.targets if isinstance(t, ast.Name)]
                else:
                    continue
                if default is None:
                    continue
                for target in targets:
                    base = f"{module}.{qual}.{target.id}"
                    for site_id, value, lineno in _leaves(default, base, names, local):
                        if value not in NEUTRAL_VALUES:
                            sites.append(NumberSite(site_id, rel, lineno or body_node.lineno, value))
    return sites
