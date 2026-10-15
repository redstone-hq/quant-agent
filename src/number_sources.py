"""Fail the build when a new trade-governing number arrives with no source.

THE DEFECT. The desk's hardest standing rule (`docs/OUTCOME.md`, and the top
of `docs/WORK.md`) is that a number which governs a trade must be read off the
instrument, derived from something that is itself read off the instrument, or
carry a written derivation at its definition site. Approval does not make a
flat number non-arbitrary, and neither does backtesting it into place. On
2026-09-11 an audit listed about twenty such numbers; on 2026-09-14 every one
was re-checked and every one was still live; the finding was filed as "an
inventory, not an item / never re-audit" and nothing was assigned. A week
later they were all still live and the count had grown.

That is the real defect: **there was no mechanical check of any kind**, so
the next invented number got in for free.

WHAT THIS MODULE DOES. It enumerates every numeric DEFINITION SITE inside a
declared scope, and requires each one to carry an entry in a checked-in
ledger (`config/number_ledger.yaml`) saying where the number came from. A new
number in scope with no ledger entry fails `pytest`, which is the check
branch protection requires.

WHAT IT DOES NOT DO, SAID FIRST BECAUSE IT IS THE HONEST FRAMING. This gate
tests that a justification EXISTS, in a shape a reader can open. It does not
and cannot test that the justification is TRUE. That distinction is not
academic: the entry this module was built around was written on shipping day,
was the most scrutinised line in the ledger, and was **false in four places**
— it claimed `min_position_risk_pct`'s 0.50% was absent from `settings.yaml`
(it is at `config/settings.yaml:751`), absent from every document (it is the
owner-ratified envelope row at `docs/OUTCOME.md:84`), absent from any
ratification record (`75c02335`, 2026-08-27), and existing in one place while
the ledger itself listed it twice. Every one of those was a one-minute grep.
So the rules below are built to make a claim CHEAP TO FALSIFY rather than to
adjudicate it: `source` must be a URL or a `file:line` a reader can open, and
`arbitrary` must carry the open question and its cost rather than being the
quiet default.

WHY A REGISTRY AND NOT AN ANNOTATION. A required comment marker (`# source:`)
was the cheaper design and was rejected: the failure this closes is that
nobody remembers the rule, and a marker is only present if somebody
remembered to type it — the check would have to accept its absence as "not a
trade number" and would therefore catch nothing. The ledger inverts that. The
scanner decides what is in scope from the code's own structure; the author
cannot opt out by staying quiet, only by writing an entry somebody reviews.

SCOPE, AND THE RULE BEHIND IT. Scope was a hand-kept file list with no stated
admission rule, which meant nothing distinguished a module that belongs from
one that does not — `src/execution/cash_sweep.py` was in and
`src/data/technical.py`, which defines the ATR period every stop multiple in
the ledger is a multiple of, was out. The rule is now written down:

  IN SCOPE: every module on the path from a seat's verdict to a broker order
  — the risk engine, the constructor, ranking, nomination, the rotation and
  cash decisions, the execution modules that price or gate an order, and the
  INDICATOR AND LEVEL modules whose outputs stops and sizes are computed
  from. A unit is in scope wherever its multiplier is.

  OUT OF SCOPE: fetching, caching, persistence, reporting, notification and
  LLM plumbing. A wrong number there degrades data or messaging, which the
  seats already see as missing evidence, and which is a different failure.

That rule is still prose, so it is backed mechanically by
`scripts/unscoped_number_guard.py`: the same scanner is run over every
`src/**.py` NOT in scope, on this tree and on `origin/main`, and the build
fails if this change RAISES the count. Nothing is stored. A new module-level
numeric constant in an unscoped file therefore cannot arrive silently — it
must come into scope with a ledger entry. (This is also the answer to
`stop_repair.py`: it defines no module-level numeric constant at all, so
there is nothing there for either check to see.)

Inside a scoped file the structural rules are:
  * (a) module-level UPPER_CASE names bound to a number;
  * (b) numeric defaults on fields of a class whose name ends in `Config`;
  * (c) numeric defaults on function and method PARAMETERS
    (`max_pct: float = 5.0`), id `module.Qual.name(param)`;
  * (d) numeric attributes on ANY class, annotated or not
    (`_MAX_REPLACEMENT_HOPS = 8` inside the broker class), id
    `module.Qual.attr`; and
  * (e) inline MULTIPLIER/DIVISOR literals in the band `FACTOR_BAND`
    ([0.5, 2.0), excluding +-1): `round(price * 0.995, 2)`,
    `deficit * 1.02`, `price * (1.01 if cover else 0.99)`. Id
    `module.Qual.function:factor[N]`, N counting such literals in that
    function in source order.

Rules (c)-(e) were added 2026-09-19 because (a)/(b) left live trade numbers
invisible: the queued-earnings weight cap and the correlated-cluster cap were
parameter defaults, the 3% stop-limit buffer was a class attribute, and board
item 138's 0.5%/1% order-price buffers were inline arithmetic. (c)-(e) apply
only to scoped modules, not to `src/config/__init__.py`'s named classes and not to the
unscoped sentinel, whose count stays defined as module-level constants.

Why (e) is a band and not "every literal". Every literal operand of
arithmetic in scope was listed on 2026-09-19: about seventy, and outside the
band they are unit conversions (10_000 bps, 365 days, 60 s, 1_000_000), float
epsilons and query paddings; inside it, every one was an order-price or
sizing margin. A literal whose other operand is also a literal (`365 * 5`) is
constant arithmetic, not a margin, and is skipped. Renumbering is a feature:
adding a factor above an existing one in the same function shifts N and
fails the gate, which puts the neighbouring entries back in front of a
reviewer.

Rule (b) predates (d) and is kept because `*Config` is this codebase's
settled name for "the tunables". Rule (d) widens it to every class; it did
not bury the signal, because result/DTO dataclasses (`GrossCeilingOutcome`,
`PortfolioVolEstimate`, `SizingDecision`) default their numbers to zero,
which is never a site. Measured 2026-09-19: rule (d) found 19 sites in
scope, one of them a DTO counter (`AgentResult.provider_requests = 1`).

"Bound to a number" means bound to a number however it is spelled. A default
written as a NAME (`min_position_risk_pct: float = STARTER_POSITION_RISK_PCT`)
or as constant arithmetic (`5 * 366`) used to return None from `_numeric` and
vanish — four in-scope fields were in exactly that state, and pointing such a
name at an unscoped module was a one-line way to make any number disappear.
Names are now resolved against the module's own constants and against the
repo modules it imports them from, and constant arithmetic is folded.

Numeric leaves inside tuple, list and dict literals are sites too. That is
not completeness for its own sake: `src.risk.gross_ladder.GROSS_LADDER`
holds trade-governing rungs as a tuple of pairs.

SEVEN THINGS THE LEDGER IS CHECKED FOR:

  1. COVERAGE — every in-scope site has an entry. A new number fails.
  2. VALUE — the entry's recorded value equals the live literal, AND, where
     the field is reachable from `config/settings.yaml`, equals the DEPLOYED
     value there too. Without the second half the ledger pinned the code
     default while the desk traded the YAML: `risk.max_position_risk_pct`
     could go 5 -> 10 with this gate silent. 52 of the entries route that
     way (verified by resolving each `AppConfig` section to its class).
  3. GROUNDS — `sourced`/`instrument` require a `source` that is a URL or a
     `file:line`, because prose is not falsifiable by a non-author;
     `derived` requires naming its base; `arbitrary` requires a note, the
     open question in answerable form, and what it costs while unanswered.
  4. BASE DRIFT — a `derived` entry records `base_value`, the value its base
     held when the derivation was written. If the base later moves, the two
     disagree and the build fails. This is the *sourced once, unsourced
     later* class.
  5. RATCHET, DOWN-ONLY — the `arbitrary` count may fall and may stay level;
     it may not RISE. The reference is the same count on `origin/main`,
     computed from the trunk's own ledger every time the guard runs, so the
     guard stores nothing and there is no ceiling anybody can raise. It was
     an equality against a stored sum until 2026-10-05, and that sum was
     moved UP by appending a positive delta to make a change green. Moving a
     constant into an unscoped file to drop its row is rule 6's job, not
     this one's.
  6. UNSCOPED SENTINEL — `scripts/unscoped_number_guard.py`, above.
  7. CITATIONS RESOLVE — every `path:line` an entry cites must exist and
     the line must be inside the file. It cannot check that a citation
     SAYS what the entry claims, but it catches one nobody opened. It
     caught an invented document path during this gate's own rework.

WHAT THIS STRUCTURALLY CANNOT CATCH, stated here so the module is never cited
as if it covered more:

  * A WRONG source. This is the big one and it has already happened; see the
    second paragraph. Nothing here reads a citation and checks it is true.
  * A number outside scope that the sentinel's count ratchet lets through
    because something else in an unscoped file was deleted in the same
    commit.
  * A citation to a symbol that still exists but no longer means what the row
    says. Rule 7 resolves `path::Symbol` and `path@`text`` citations and
    rejects bare `path:line` ones (board item 225); it cannot judge whether
    the symbol is the right one.
  * A number computed at run time from live inputs, or a `default_factory`
    whose number lives in a function body.
  * Inline literals OUTSIDE rule (e)'s band or shape. Measured 2026-09-19:
    comparison thresholds (`abs(volume_change_pct) > 50`), additive offsets,
    divisors like the `/ 10.0` in `src/data/levels.py`'s level strength
    (board item 148), fallback arguments (`_risk_number(x, 25.0)`,
    `kwargs.get(k, 25.0)`), and keyword literals passed at a call site
    (`lookback_days=5`) are all invisible. Catching them by value needs a
    list of "boring" numbers, which is itself an arbitrary list; the honest
    fix per site is to hoist the literal to a named constant, which (a)
    then sees.
  * A number in prompt PROSE. Measured 2026-09-18: ~1,825 numeric tokens
    across `config/prompts/*.md`, overwhelmingly dates and list numbering.
    That is board item 105 and it is not solvable this way.
  * The `arbitrary` entries themselves. The gate holds the boundary; it does
    not retroactively source anything.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from src.storage_write_index import written_columns
from src.feature_flags import config_modules  # where the config lives
from src.number_site_scan import (  # noqa: F401 -- re-exported, lifted verbatim
    CONFIG_CLASS_SUFFIX,
    FACTOR_BAND,
    NEUTRAL_VALUES,
    NumberSite,
    _factor_operands,
    _bound_name_values,
    _field_default,
    _imported_constants,
    _leaves,
    _module_constants,
    _numeric,
    _own_nodes,
    _qualified_scopes,
    _scan_extended_shapes,
)

#: Repository root, resolved from this file rather than the cwd so the check
#: behaves the same under pytest, a git hook and a direct run.
REPO_ROOT = Path(__file__).resolve().parent.parent

#: The ledger. Data, not code: it holds no value the desk trades on, only a
#: record of where each traded value is supposed to have come from.
LEDGER_PATH = REPO_ROOT / "config" / "number_ledger.yaml"

#: Deployed overrides. Every `src.config.*Config.<field>` ledger row is also
#: checked against this file, because this is the value the desk trades.
SETTINGS_PATH = REPO_ROOT / "config" / "settings.yaml"

#: The modules on the path from a verdict to a broker order; see
#: src/number_scope.py (data only, re-exported here for every reader).
from src.number_scope import SCOPED_PATHS, py_universe  # noqa: E402,F401


#: `src/config/__init__.py` holds every seat's settings in one file, most of them
#: nothing to do with a trade (LLM cost circuits, Telegram retries, evolution
#: bookkeeping). Scoping the whole file would bury the signal, so the
#: trade-governing classes are named. Same suffix rule applies inside them.
SCOPED_CONFIG_CLASSES: tuple[str, ...] = (
    "RiskConfig",
    "ExecutionConfig",
    "CashSweepConfig",
    "CashReserveConfig",
    "IntradayScanConfig",
    "SmartMoneyConfig",
    "NominationConfig",
    "EventRiskConfig",
    "UniverseScreenConfig",
)


#: Classifications a ledger entry may carry.
#:   instrument       — read off the instrument at run time or fixed by an
#:                      external spec the desk does not choose (a broker tick
#:                      size, an exchange rule, a statute). Requires a
#:                      falsifiable `source`.
#:   sourced          — a cited derivation or published source written down.
#:                      Requires a falsifiable `source`.
#:   derived          — computed from another ledger entry. Requires
#:                      `derived_from` and `base_value`.
#:   arbitrary        — live, governs trades, and nothing backs it. This is a
#:                      DEBT INSTRUMENT, not a status: it requires `note`,
#:                      `open_question` and `cost_while_unanswered`, and it is
#:                      ratcheted. It must never be the cheapest field to
#:                      write, which is what it was.
#:   not-trade-governing — in scope structurally, does not reach a trade
#:                      decision. Requires `note` saying why.
#:   owner-ruled      — the owner decided this VALUE and the decision is
#:                      dated and recorded. A decision, not a measurement and
#:                      not a debt. Requires `ruled_on` (YYYY-MM-DD) and
#:                      `ruling_record` (where it is written down) and
#:                      `ruling_summary` (what was decided, in words).
VALID_STATUSES: frozenset[str] = frozenset(
    {
        "instrument",
        "sourced",
        "derived",
        "arbitrary",
        "not-trade-governing",
        "owner-ruled",
    }
)

#: Fields every `arbitrary` entry must carry. `docs/OUTCOME.md`'s outcome-3
#: clause already demands all of this; the schema used to require none of it,
#: which made the honest-but-unsourced status the cheapest one to write and
#: put the incentive exactly backwards.
ARBITRARY_REQUIRED_FIELDS: tuple[str, ...] = (
    "note",
    "open_question",
    "cost_while_unanswered",
)

#: Ratchet, checked for EQUALITY. See rule 5 in the module docstring: as a
#: ceiling this was gameable by deleting a row. It is no longer a literal
#: anybody edits. It is the SUM of the per-change deltas recorded in
#: config/number_ledger_history.yaml, one appended entry per change, each
#: carrying the reason that change was made -- so the number cannot drift
#: from its own record, and the record cannot be skipped.
#:
#: There is no stored count and no history file. Both were deleted: a summed
#: append-only record is stored bookkeeping, and the sum could be moved UP by
#: appending a positive delta, which is what open change 1430 did on
#: 2026-10-05 when it hit 131 against 127 and appended +4. A ratchet that
#: accepts a positive delta is not a ratchet. The grounds for every past move
#: remain readable in git at the commit that deleted the file, and each row's
#: own grounds live in its ledger entry.
from src.number_ledger_counts import (  # noqa: E402,F401 -- lifted verbatim
    LEDGER_RELATIVE,
    count_arbitrary,
    ARBITRARY_ALLOWLIST,
    ROUTELESS_ALLOWLIST,
    ratchet_violations,
    read_allowlist,
    route_ratchet_violations,
)


#: Item 90's three states. A trade-governing number must sit in exactly one
#: of them: (1) SOURCED OR MEASURED -- the `sourced`, `derived` and
#: `instrument` statuses; (2) RATIFIED AS A STRUCTURAL BOUND with the reason
#: recorded -- also carried by `sourced`, whose `source` is then the
#: ratification; (3) UNSOURCEABLE TODAY, with a named RECORDING already built
#: or specified that would settle it, and that recording as its closing
#: condition -- an `arbitrary` row carrying `settles_by`.
#:
#: The fourth state is the defect: an `arbitrary` row with no `settles_by`,
#: i.e. a live number nothing in this desk will ever answer. Before this
#: block existed the schema could not tell state 3 from the defect, so the
#: classification could only be produced by hand-reading 135 rows -- and
#: anything that relies on remembering to re-read them slips.
SETTLEMENT_ROUTE_KINDS: frozenset[str] = frozenset({"recording", "ratified-bound", "measurement"})

#: `built` = the recording exists and is accumulating now. `specified` = it is
#: written down in enough detail to build, and `where` points at that writing.
SETTLEMENT_ROUTE_STATES: frozenset[str] = frozenset({"built", "specified"})

#: Fields a `settles_by` block must carry. `records` says WHAT is written
#: down; `closes_when` says what reading it would have to show for the row to
#: leave `arbitrary`. A route with no closing condition is a wish.
SETTLEMENT_ROUTE_FIELDS: tuple[str, ...] = (
    "kind",
    "state",
    "where",
    "records",
    "closes_when",
)

#: Shortest `records` / `closes_when` worth the name. A one-word route is the
#: same failure as the one-word `note` the arbitrary schema already bars.
MIN_ROUTE_PROSE_CHARS = 40


#: The `(table, column)` pairs real code WRITES, resolved from source at
#: check time by `src.storage_write_index`. A settlement route in state
#: `built` has to name where its evidence lands, and this is what makes the
#: claim falsifiable. The earlier version matched the bare column name as a
#: WORD inside three hardcoded storage files, which passed a column no write
#: ever names, refused a genuine write living elsewhere, and never compared
#: the table at all. See that module for the identity it resolves and for the
#: one dynamic shape it accepts as a deliberate, strict proxy.


def settlement_route_problem(entry: dict[str, Any]) -> str | None:
    """Why this entry's `settles_by` is not a route, or None if it is one."""
    route = entry.get("settles_by")
    if route is None:
        return "no `settles_by` block"
    if not isinstance(route, dict):
        return "`settles_by` is not a mapping"
    missing = [f for f in SETTLEMENT_ROUTE_FIELDS if not str(route.get(f, "")).strip()]
    if missing:
        return f"`settles_by` is missing {', '.join(missing)}"
    if route["kind"] not in SETTLEMENT_ROUTE_KINDS:
        return f"`settles_by.kind` is {route['kind']!r}; expected one of {sorted(SETTLEMENT_ROUTE_KINDS)}"
    if route["state"] not in SETTLEMENT_ROUTE_STATES:
        return f"`settles_by.state` is {route['state']!r}; expected one of {sorted(SETTLEMENT_ROUTE_STATES)}"
    for field in ("records", "closes_when"):
        if len(str(route[field]).strip()) < MIN_ROUTE_PROSE_CHARS:
            return (
                f"`settles_by.{field}` is under {MIN_ROUTE_PROSE_CHARS} "
                f"characters, which is not a route anybody can act on"
            )
    if route["state"] == "built":
        writes = route.get("writes")
        if not isinstance(writes, list) or not writes:
            return (
                "`settles_by.state` is `built` but the route names no "
                "`writes:` list. A BUILT recording has to say which fields "
                "carry its evidence, or nobody can tell a recording that is "
                "collecting from one that is silently collecting nothing"
            )
        written = written_columns()
        for target in writes:
            if not isinstance(target, str) or "." not in target:
                return f"`settles_by.writes` entry {target!r} is not a `<table>.<column>` name"
            table, field = (part.strip() for part in target.rsplit(".", 1))
            if (table.lower(), field) in written:
                continue
            elsewhere = sorted({t for t, c in written if c == field})
            hint = (
                f" The column name is written, but to {', '.join(elsewhere)} -- not to {table!r}."
                if elsewhere
                else " No INSERT or UPDATE anywhere names that column."
            )
            return (
                f"`settles_by.writes` names {target!r} but no INSERT or "
                f"UPDATE in the tree writes column {field!r} of table "
                f"{table!r}.{hint} A settlement route pointing at a field "
                f"nothing writes can never close"
            )
    return None


def classification(
    ledger: dict[str, dict[str, Any]],
) -> dict[str, list[str]]:
    """Item 90's classification, produced FROM the ledger, never by hand.

    Keys: `sourced_or_measured`, `ratified_bound`, `recording_named`,
    `unclassified` and `not_trade_governing`. `unclassified` is the remaining
    work: live numbers in none of the three states.
    """
    out: dict[str, list[str]] = {
        "sourced_or_measured": [],
        "ratified_bound": [],
        "recording_named": [],
        "unclassified": [],
        "not_trade_governing": [],
    }
    for site_id, entry in sorted(ledger.items()):
        status = entry.get("status")
        if status == "not-trade-governing":
            out["not_trade_governing"].append(site_id)
        elif status in {"derived", "instrument", "measurement"}:
            out["sourced_or_measured"].append(site_id)
        elif status == "owner-ruled":
            out["ratified_bound"].append(site_id)
        elif status == "sourced":
            text = str(entry.get("source", "")).lower()
            key = "ratified_bound" if "ratif" in text else "sourced_or_measured"
            out[key].append(site_id)
        elif status == "arbitrary":
            key = "unclassified" if settlement_route_problem(entry) else "recording_named"
            out[key].append(site_id)
    return out


#: Paths under `src/` the unscoped sentinel does not count: generated code and
#: vendored trees have no author to ask.
UNSCOPED_SENTINEL_EXCLUDE: tuple[str, ...] = ("src/frontend",)


@dataclass
class LedgerProblem:
    """One reason the build should fail, in the words a reviewer needs."""

    kind: str
    site_id: str
    detail: str

    def __str__(self) -> str:  # pragma: no cover - diagnostics only
        return f"[{self.kind}] {self.site_id}: {self.detail}"


def _scan_module(
    path: Path,
    rel: str,
    config_classes: tuple[str, ...] | None,
    root: Path | None = None,
) -> list[NumberSite]:
    """Sites in one file.

    `config_classes` None means "any class whose name ends in `Config`" — the
    rule for a scoped module. A tuple means only those names, which is how
    `src/config/__init__.py` is handled without pulling in the LLM and Telegram
    settings that share the file.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    module = rel[: -len(".py")].replace("/", ".")
    if module.endswith(".__init__") or (config_classes and module.startswith("src.config.")):
        module = (
            "src.config" if config_classes else module[: -len(".__init__")]
        )  # the package IS the module; src/config/* sections are re-exported by it
    sites: list[NumberSite] = []

    local = _module_constants(tree)
    names = dict(local)
    if root is not None:
        # Imported names do not shadow the module's own.
        for key, value in _imported_constants(tree, root).items():
            names.setdefault(key, value)

    # (a) module-level UPPER_CASE constants.
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
            for name_node, value_node in _bound_name_values(target, node.value):
                name = name_node.id
                if not (name.isupper() or (name.startswith("_") and name.lstrip("_").isupper())):
                    continue
                for site_id, value, lineno in _leaves(value_node, f"{module}.{name}", names, local):
                    if value not in NEUTRAL_VALUES:
                        sites.append(NumberSite(site_id, rel, lineno, value))

    # (b) numeric defaults on `*Config` class fields.
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        if config_classes is None:
            if not node.name.endswith(CONFIG_CLASS_SUFFIX):
                continue
        elif node.name not in config_classes:
            continue
        for body_node in node.body:
            if not isinstance(body_node, ast.AnnAssign):
                continue
            if not isinstance(body_node.target, ast.Name):
                continue
            default = _field_default(body_node)
            if default is None:
                continue
            base = f"{module}.{node.name}.{body_node.target.id}"
            for site_id, value, lineno in _leaves(default, base, names, local):
                if value not in NEUTRAL_VALUES:
                    sites.append(NumberSite(site_id, rel, lineno or body_node.lineno, value))

    if config_classes is None:
        sites.extend(_scan_extended_shapes(tree, module, rel, names, local))

    return sites


def _scoped_files(root: Path) -> list[Path]:
    """Every file `SCOPED_PATHS` names, raising if one has vanished."""
    files: list[Path] = []
    for entry in SCOPED_PATHS:
        target = root / entry
        if target.is_dir():
            found = sorted(target.rglob("*.py"))
        elif target.is_file():
            found = [target]
        else:
            # A scoped path that no longer exists is a finding in itself:
            # scope silently narrowing is how this check would rot.
            raise FileNotFoundError(f"SCOPED_PATHS entry does not exist: {entry}")
        files.extend(found)
    return files


def collect_sites(repo_root: Path | None = None) -> list[NumberSite]:
    """Every in-scope numeric definition site in the tree, sorted by id."""
    root = repo_root or REPO_ROOT
    sites: list[NumberSite] = []

    for file_path in _scoped_files(root):
        if file_path.name == "__init__.py" and file_path.stat().st_size == 0:
            continue
        sites.extend(_scan_module(file_path, str(file_path.relative_to(root)), None, root))

    for config_module in config_modules(root):  # src/config/__init__.py or src/config/*
        rel = str(config_module.relative_to(root))
        sites.extend(_scan_module(config_module, rel, SCOPED_CONFIG_CLASSES, root))

    return sorted(sites, key=lambda s: s.site_id)


def collect_unscoped_sites(repo_root: Path | None = None) -> list[NumberSite]:
    """The same scan over every `src/**.py` that is NOT in scope.

    This is the sentinel behind the scope rule. Scope is a reviewed list, and
    a list has no mechanical guarantee of completeness — a test can pin that
    it does not shrink but nothing pins that it is whole. Counting what is
    outside it turns "somebody should widen scope" into a build failure the
    day a new unscoped constant appears.
    """
    root = repo_root or REPO_ROOT
    in_scope = {p.resolve() for p in _scoped_files(root)}
    in_scope.update(p.resolve() for p in config_modules(root))

    sites: list[NumberSite] = []
    for file_path in py_universe(root):
        rel = str(file_path.relative_to(root))
        if file_path.resolve() in in_scope:
            continue
        if any(rel.startswith(prefix) for prefix in UNSCOPED_SENTINEL_EXCLUDE):
            continue
        try:
            sites.extend(_scan_module(file_path, rel, (), root))
        except SyntaxError:  # pragma: no cover - a broken file fails elsewhere
            continue
    return sorted(sites, key=lambda s: s.site_id)


def load_ledger(path: Path | None = None) -> dict[str, dict[str, Any]]:
    """The ledger as `{site_id: entry}`. Raises on a duplicate id."""
    ledger_path = path or LEDGER_PATH
    raw = yaml.safe_load(ledger_path.read_text(encoding="utf-8")) or {}
    entries = raw.get("numbers") or []
    out: dict[str, dict[str, Any]] = {}
    for entry in entries:
        site_id = entry.get("id")
        if not site_id:
            raise ValueError(f"ledger entry with no id: {entry!r}")
        if site_id in out:
            raise ValueError(f"duplicate ledger id: {site_id}")
        out[site_id] = entry
    return out


from src.number_deployed_values import (  # noqa: E402,F401 -- lifted verbatim
    _appconfig_sections,
    deployed_values,
)


#: A `source` a non-author can open in under a minute: a URL, or a repo path
#: with a line number. Prose is not falsifiable — the four false claims in the
#: entry this gate was built around were all prose, and all were one grep from
#: being disproved.
def _is_falsifiable_source(text: str) -> bool:
    if re.search(r"https?://\S+", text):
        return True
    return bool(re.search(r"\b[\w./-]+\.(?:py|yaml|yml|md|json|toml)(?:::[A-Za-z_]|@`)", text))


from src.ledger_citations import broken_citations  # noqa: E402,F401  (rule 7)


def audit(repo_root: Path | None = None, ledger_path: Path | None = None) -> list[LedgerProblem]:
    """Every reason the build should fail. Empty means the boundary holds."""
    root = repo_root or REPO_ROOT
    sites = collect_sites(root)
    ledger = load_ledger(ledger_path)
    by_id = {site.site_id: site for site in sites}
    problems: list[LedgerProblem] = []

    try:
        deployed = deployed_values(root)
    except FileNotFoundError:  # pragma: no cover - fixture trees have no settings
        deployed = {}

    # 1. COVERAGE. A number in scope with no entry is the whole point.
    for site in sites:
        if site.site_id not in ledger:
            problems.append(
                LedgerProblem(
                    "unsourced",
                    site.site_id,
                    f"new trade-governing number {site.value!r} at "
                    f"{site.path}:{site.lineno} with no entry in "
                    f"config/number_ledger.yaml. Say where it came from, or "
                    f"record it as arbitrary with its open question and cost.",
                )
            )

    # A ledger entry with no site is stale bookkeeping, and left alone it
    # would let coverage look complete while the file drifted.
    for site_id in ledger:
        if site_id not in by_id:
            problems.append(
                LedgerProblem(
                    "orphan",
                    site_id,
                    "ledger entry no longer matches any definition site — the "
                    "number was renamed, moved or deleted. Remove the entry or "
                    "point it at the new site. If the number moved OUT of "
                    "scope, it is still live: bring the file into scope rather "
                    "than dropping the row.",
                )
            )

    for site_id, entry in ledger.items():
        site = by_id.get(site_id)
        status = entry.get("status")

        if status not in VALID_STATUSES:
            problems.append(
                LedgerProblem(
                    "bad-status",
                    site_id,
                    f"status {status!r} is not one of {sorted(VALID_STATUSES)}",
                )
            )
            continue

        # 2. VALUE. The recorded value must still be the live one, so that
        #    changing a number puts its justification back in front of the
        #    person changing it.
        if site is not None:
            recorded = entry.get("value")
            if recorded is None or abs(float(recorded) - site.value) > 1e-12:
                problems.append(
                    LedgerProblem(
                        "value-drift",
                        site_id,
                        f"live value {site.value!r} but the ledger records "
                        f"{recorded!r}. If the number changed on purpose, "
                        f"re-read its justification and update the entry.",
                    )
                )

        # 2b. DEPLOYED VALUE. The ledger pins the code default; the desk
        #     trades the YAML. Both must agree or the ledger is fiction.
        if site_id in deployed and entry.get("value") is not None:
            if abs(deployed[site_id] - float(entry["value"])) > 1e-12:
                problems.append(
                    LedgerProblem(
                        "deployed-drift",
                        site_id,
                        f"config/settings.yaml deploys "
                        f"{deployed[site_id]!r} but the ledger records "
                        f"{entry['value']!r}. The ledger describes the code "
                        f"default; this is the number the desk actually "
                        f"trades. Re-read the justification against the "
                        f"deployed value.",
                    )
                )

        # 3. GROUNDS.
        if status in ("sourced", "instrument"):
            source = str(entry.get("source") or "").strip()
            if not source:
                problems.append(
                    LedgerProblem(
                        "no-source",
                        site_id,
                        f"status {status!r} requires a `source:` saying where the number was read from.",
                    )
                )
            elif not _is_falsifiable_source(source):
                problems.append(
                    LedgerProblem(
                        "unfalsifiable-source",
                        site_id,
                        "`source:` is prose with nothing a non-author can "
                        "open. Give a URL, or a repo `path:line` pointing at "
                        "the derivation. The flagship entry of this ledger was "
                        "false in four places and every claim was one grep "
                        "from being disproved.",
                    )
                )
        if status == "not-trade-governing" and not str(entry.get("note") or "").strip():
            problems.append(
                LedgerProblem(
                    "no-note",
                    site_id,
                    "status 'not-trade-governing' requires a `note:` saying why "
                    "this number cannot reach a trade decision.",
                )
            )
        if status == "owner-ruled":
            ruled_on = str(entry.get("ruled_on") or "").strip()
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", ruled_on):
                problems.append(
                    LedgerProblem(
                        "no-ruling-date",
                        site_id,
                        "status 'owner-ruled' requires `ruled_on:` as a "
                        "YYYY-MM-DD date. A ruling nobody can date is a claim, "
                        "not a decision.",
                    )
                )
            if not str(entry.get("ruling_summary") or "").strip():
                problems.append(
                    LedgerProblem(
                        "no-ruling-summary",
                        site_id,
                        "status 'owner-ruled' requires `ruling_summary:` stating "
                        "in plain words what the owner decided.",
                    )
                )
            if not str(entry.get("ruling_record") or "").strip():
                problems.append(
                    LedgerProblem(
                        "no-ruling-record",
                        site_id,
                        "status 'owner-ruled' requires `ruling_record:` saying where the ruling is written down.",
                    )
                )
        if status == "arbitrary":
            for field in ARBITRARY_REQUIRED_FIELDS:
                if not str(entry.get(field) or "").strip():
                    problems.append(
                        LedgerProblem(
                            "incomplete-debt",
                            site_id,
                            f"status 'arbitrary' requires `{field}:`. An "
                            f"arbitrary number is a debt, not a status: it "
                            f"must state the open question in a form somebody "
                            f"could answer, and what the desk pays while it is "
                            f"unanswered (docs/OUTCOME.md, outcome 3). It must "
                            f"not be the cheapest entry to write.",
                        )
                    )

        # 4. BASE DRIFT. The class from the brief: sourced once, unsourced
        #    later because what it was derived from moved underneath it.
        if status == "derived":
            base_id = entry.get("derived_from")
            if not base_id:
                problems.append(
                    LedgerProblem(
                        "no-base",
                        site_id,
                        "status 'derived' requires `derived_from:` naming the ledger entry this was computed from.",
                    )
                )
                continue
            if "base_value" not in entry:
                problems.append(
                    LedgerProblem(
                        "no-base-value",
                        site_id,
                        "status 'derived' requires `base_value:` — the value "
                        "the base held when this derivation was written. "
                        "Without it a moving base is undetectable.",
                    )
                )
                continue
            base_site = by_id.get(base_id)
            if base_site is None:
                problems.append(
                    LedgerProblem(
                        "unknown-base",
                        site_id,
                        f"derived_from {base_id!r} is not a definition site in "
                        f"scope. A derivation can only be tracked against a "
                        f"number this check can see.",
                    )
                )
                continue
            if abs(float(entry["base_value"]) - base_site.value) > 1e-12:
                problems.append(
                    LedgerProblem(
                        "base-drift",
                        site_id,
                        f"derived from {base_id} when it was "
                        f"{entry['base_value']!r}; it is now {base_site.value!r}. "
                        f"The derivation no longer describes the live geometry, "
                        f"so this number is arbitrary again. Re-derive it, or "
                        f"reclassify it honestly.",
                    )
                )

    # Rules 5 and 6 are properties of THE ledger and THE tree, not of any
    # ledger: a synthetic fixture holding two numbers is not evidence that
    # the desk's arbitrary count moved. They are skipped for a fixture tree so
    # that every OTHER rule stays testable in isolation.
    if ledger_path is not None and ledger_path != LEDGER_PATH:
        return sorted(problems, key=lambda p: (p.kind, p.site_id))

    # 5. RATCHET, KEYED ON ROW IDENTITY, measured against the trunk at check
    #    time. A count is a proxy and cannot tell DODGING (a row the trunk
    #    sources, downgraded here to `arbitrary` to avoid the work) from
    #    DISCOVERY (a sweep registering a number the ledger never held). The
    #    first is refused absolutely; the second is admitted only with a
    #    route to settlement. Leaving `arbitrary` is always allowed, and a
    #    net-zero swap -- one row dodging down while another is sourced up --
    #    no longer passes, because the dodging row is named.
    for site_id, detail in ratchet_violations(ledger, read_allowlist(ARBITRARY_ALLOWLIST)):
        problems.append(LedgerProblem("ratchet", site_id, detail))

    # 8. SETTLEMENT ROUTE, board item 90's half two. An `arbitrary` row is
    #    only tolerable as item 90's state 3 -- unsourceable today, with a
    #    named recording that WOULD settle it. A malformed route is a hard
    #    failure (it reads as an answer and is not one); a missing route is
    #    counted and ratcheted, because 135 of them exist and deleting them
    #    is not the fix.
    routeless: list[str] = []
    for site_id, entry in sorted(ledger.items()):
        if entry.get("status") != "arbitrary":
            if entry.get("settles_by") is not None:
                problems.append(
                    LedgerProblem(
                        "settlement-route",
                        site_id,
                        "carries `settles_by` but is not `arbitrary`. A route "
                        "to an answer is for a number that has none; a sourced "
                        "number states its source instead.",
                    )
                )
            continue
        why = settlement_route_problem(entry)
        if why is None:
            continue
        if entry.get("settles_by") is None:
            routeless.append(site_id)
            continue
        problems.append(
            LedgerProblem(
                "settlement-route",
                site_id,
                f"{why}. Either make the route real or remove it: a route "
                f"that cannot be acted on is worse than an honest blank, "
                f"because the count stops showing the work as outstanding.",
            )
        )
    # Identity-keyed against a committed shrink-only allow-list; no trunk read.
    for site_id, detail in route_ratchet_violations(ledger, read_allowlist(ROUTELESS_ALLOWLIST)):
        problems.append(LedgerProblem("route-ratchet", site_id, detail))

    # 7. CITATIONS RESOLVE. Cheap, and aimed squarely at the failure that
    #    made this gate's own flagship entry false in four places.
    for site_id, why, cite in broken_citations(ledger, root):
        problems.append(
            LedgerProblem(
                "broken-citation",
                site_id,
                f"cites {cite} - {why}. A citation nobody can open is not a "
                f"source, and an invented one is worse than none.",
            )
        )

    # 6. UNSCOPED SENTINEL lives in scripts/unscoped_number_guard.py: it
    #    compares against origin/main at check time and stores no count.

    return sorted(problems, key=lambda p: (p.kind, p.site_id))


def main() -> int:  # pragma: no cover - CLI convenience
    problems = audit()
    if not problems:
        sites = collect_sites()
        unscoped = collect_unscoped_sites()
        print(
            f"number ledger: {len(sites)} sites in scope, all accounted for; {len(unscoped)} unscoped constants watched"
        )
        return 0
    for problem in problems:
        print(problem)
    print(f"\n{len(problems)} problem(s). See src/number_sources.py for the rules.")
    return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
