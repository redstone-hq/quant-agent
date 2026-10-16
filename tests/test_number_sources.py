"""The gate. A new trade-governing number with no source fails the build.

`pytest` is the check branch protection requires, so this file — not a
reporting script somebody runs by hand — is what makes the no-arbitrary-numbers
rule mechanical. Read `src/number_sources.py`'s docstring for the scope rule
and for the honest list of what this cannot catch.

The tests below are in two groups. The first is the gate itself, against the
live tree. The second proves each failure mode actually fires, using synthetic
fixtures — because a gate nobody has seen fail is indistinguishable from a
gate that passes everything, which is how the desk's `Adversary:` check became
theatre once already.

A standing caution, because it has already happened here: every rule in this
file tests that a justification EXISTS in an openable shape. None of them
tests that one is TRUE. The first version of this ledger's flagship entry was
false in four places and passed every test below.
"""

from __future__ import annotations

import re
import textwrap
from pathlib import Path

import pytest

from src.number_sources import (
    ARBITRARY_REQUIRED_FIELDS,
    NEUTRAL_VALUES,
    SCOPED_CONFIG_CLASSES,
    SCOPED_PATHS,
    audit,
    broken_citations,
    collect_sites,
    collect_unscoped_sites,
    deployed_values,
    load_ledger,
)

# --------------------------------------------------------------------------
# The gate, against the live tree.
# --------------------------------------------------------------------------


def test_every_trade_governing_number_is_accounted_for() -> None:
    """THE GATE. Every numeric definition site in scope has a ledger entry,
    the entry's value still matches the code AND the deployed YAML, every
    claimed source is openable, every arbitrary number carries its open
    question and its cost, and no derivation has outlived its base.

    If this fails on your branch you have added or moved a number that
    governs a trade. Add it to `config/number_ledger.yaml` with where it came
    from. If nothing backs it, say `arbitrary` — and note that the
    arbitrary-number ratchet is DOWN-ONLY against the trunk, so there is no
    ceiling to raise and no file to append to: a change that adds one cannot
    be made green by the build.
    """
    problems = audit()
    assert not problems, "\n".join(
        ["unsourced or unaccounted trade-governing numbers:", ""] + [f"  {p}" for p in problems]
    )


def test_the_ratified_minimum_risk_floor_is_in_scope_at_every_site() -> None:
    """The 0.50% minimum-risk floor. Every trade plan sized under it is DENIED
    OUTRIGHT rather than shrunk, and it is owner-ratified (docs/OUTCOME.md:84,
    config/settings.yaml:751, commit 75c02335 of 2026-08-27).

    It lives at THREE definition sites, and the third is the point of this
    test: `RiskConfig.min_position_risk_pct`'s default is bound to a NAME, so
    the first version of the scanner could not see it at all. The LIVE floor
    was invisible to the gate that was written around it, and the ledger
    entry that named it said the number existed in exactly one place.
    """
    ids = {site.site_id for site in collect_sites()}
    assert "src.portfolio_constructor.config.ConstructorConfig.min_risk_pct" in ids
    assert "src.risk.constants.STARTER_POSITION_RISK_PCT" in ids
    assert "src.config.RiskConfig.min_position_risk_pct" in ids


def test_an_in_file_alias_is_not_a_second_site() -> None:
    """One number with two names in the same file is one number. Ledgering it
    twice is exactly how the flagship entry came to assert that a number
    existed in one place while this file listed it in two.
    """
    ids = {site.site_id for site in collect_sites()}
    assert not [i for i in ids if "_EARNINGS_XBRL_COMPARABLE_FIELDS" in i]


def test_no_row_dodges_into_arbitrary_against_the_trunk() -> None:
    """Rule 5, against the live tree, keyed on row IDENTITY.

    It was a COUNT: the number of `arbitrary` rows could not rise above the
    trunk's. A count is a proxy and conflates two different acts. Downgrading
    a row the trunk already sources, to avoid the work, is the dodge the rule
    exists to refuse. A sweep registering a number the ledger never held, and
    saying honestly that nothing backs it, is the only way the standing order
    to drive every made-up number to zero can begin on a number nobody had
    scoped -- and the count refused that too, rewarding leaving numbers
    unscoped and invisible. A count is also satisfied by a net-zero swap.

    So each row is judged by its own id against the trunk: a non-arbitrary
    row turning `arbitrary` is refused; a row new to the ledger may enter
    `arbitrary` only carrying both `settles_by` and `open_question`.
    """
    assert not [p for p in audit() if p.kind == "ratchet"]


def test_the_ratchet_stores_nothing_and_has_no_ceiling() -> None:
    """The regression guard for the shape, not for the number. A stored
    count -- a literal, or a file of deltas that are summed -- can be edited
    upward by the change it is supposed to refuse. Neither may come back.
    """
    root = Path(__file__).resolve().parent.parent
    assert not (root / "config" / "number_ledger_history.yaml").exists(), (
        "the summed history file is stored bookkeeping; the trunk is the reference"
    )
    text = (root / "src" / "number_sources.py").read_text(encoding="utf-8")
    assert not re.search(r"MAX_ARBITRARY_ENTRIES", text), (
        "a stored ceiling is what open change 1430 raised by +4 to go green"
    )
    counts = (root / "src" / "number_ledger_counts.py").read_text(encoding="utf-8")
    assert "def read_allowlist" in counts and "def ratchet_violations" in counts


def test_a_mirrored_constant_is_one_number_not_two() -> None:
    """A mirrored constant is ONE number with two definition sites. Recording
    both as `arbitrary` would inflate the count and let the headline metric be
    improved by consolidating files rather than by sourcing anything, so a
    mirror is recorded as `derived` and its base-drift is checked.
    """
    ledger = load_ledger()
    mirrors = {
        "src.portfolio_constructor.config.ConstructorConfig.min_stop_atr_multiple": (
            "src.config.RiskConfig.min_stop_atr_multiple"
        ),
        "src.pipeline_stages.MAX_ENTRY_SLIPPAGE_BPS": "src.config.ExecutionConfig.max_entry_slippage_bps",
        "src.data.levels.MAX_REACH_ATR_MULTIPLE": "src.config.RiskConfig.max_target_reach_atr_multiple",
    }
    for site_id, base in mirrors.items():
        assert ledger[site_id]["status"] == "derived", site_id
        assert ledger[site_id]["derived_from"] == base, site_id


def test_item_138_order_price_buffers_have_one_source_each() -> None:
    """Board item 138. The order-price buffers carried three values across many
    sites — the 3% stop-limit through-buffer and the 0.5% exit offset
    (0.995 SELL / 1.005 COVER, deleted 2026-10-09). Each value must have exactly ONE `arbitrary`
    definition; every other site that prices off it is `derived` from that one
    base, so the buffer cannot silently acquire a second, divergent source of
    truth. The general gate value-matches each literal; this pins the
    consolidation itself, which is what item 138 asked for.
    """
    ledger = load_ledger()

    # The 3% stop-limit buffer was DELETED 2026-10-13 with the protective
    # stop-limit fallback (taken 0 of 60 placements). The de-lever's 0.97
    # proceeds haircut, its only derived site, now stands on its own as an
    # arbitrary row rather than pointing at a base that no longer exists.
    # The exit pads were DELETED 2026-10-09 (every ordinary exit is a plain
    # market order, src/exit_quote.py), and the rotation projection's own
    # copy went the same day: it now credits the LIVE bid / ask.
    assert "src.execution.broker.AlpacaBroker.STOP_LIMIT_BUFFER_PCT" not in ledger
    # 2026-10-15: the 0.97 haircut itself was DELETED; a whole-position sale is
    # credited qty x the live sell quote, like the partial slice.
    assert "src.delever.forced.DeleverForced._force_delever:factor[0]" not in ledger
    for gone in (
        "src.rotation_projection._projected_post_sale_cash:factor[0]",
        "src.rotation_projection._projected_post_sale_cash:factor[1]",
        "src.rotation_projection._projected_post_sale_book:factor[0]",
        "src.rotation_projection._projected_post_sale_book:factor[1]",
    ):
        assert gone not in ledger, gone

    # No SECOND arbitrary source for any of these buffer values.
    for value in (0.03, 0.995, 1.005):
        arbitrary_at_value = [
            site_id
            for site_id, entry in ledger.items()
            if entry.get("status") == "arbitrary" and entry.get("value") == value
        ]
        assert len(arbitrary_at_value) <= 1, (value, arbitrary_at_value)


def test_every_arbitrary_entry_carries_its_debt() -> None:
    """Rule 3 for `arbitrary`. It used to require nothing at all — no note, no
    owner, no date — which made the honest-but-unsourced status the cheapest
    one in the file to write. That is backwards. `docs/OUTCOME.md`'s outcome-3
    clause already demands the question and the cost; the schema now does too.
    """
    ledger = load_ledger()
    for site_id, entry in ledger.items():
        if entry.get("status") != "arbitrary":
            continue
        for field in ARBITRARY_REQUIRED_FIELDS:
            assert str(entry.get(field) or "").strip(), f"{site_id} has no {field}"


def test_every_source_is_openable_by_a_non_author() -> None:
    """Rule 3 for `sourced`/`instrument`. Prose is not falsifiable. All four
    false claims in this ledger's first flagship entry were prose, and all
    four were one grep from being disproved.
    """
    import re

    ledger = load_ledger()
    for site_id, entry in ledger.items():
        if entry.get("status") not in ("sourced", "instrument"):
            continue
        source = str(entry.get("source") or "")
        openable = re.search(r"https?://\S+", source) or re.search(
            r"\b[\w./-]+\.(?:py|yaml|yml|md|json|toml)(?:::[A-Za-z_]|@`)", source
        )
        assert openable, f"{site_id} cites prose with nothing to open"


def test_the_deployed_value_is_checked_and_not_only_the_code_default() -> None:
    """Rule 2b, and the largest blind spot the first version had. This ledger
    records the CODE DEFAULT; for a `src.config.*Config` field the number the
    desk trades comes from `config/settings.yaml`. `risk.max_position_risk_pct`
    could be edited from 5 to 10 with the gate entirely silent.
    """
    deployed = deployed_values()
    assert deployed["src.config.RiskConfig.max_position_risk_pct"] == 5.0
    ledger = load_ledger()
    routed = [k for k in deployed if k in ledger]
    assert len(routed) >= 48, f"only {len(routed)} sites routed; the mapping broke"
    for site_id in routed:
        assert abs(deployed[site_id] - float(ledger[site_id]["value"])) < 1e-12, site_id


def test_every_repo_citation_in_the_ledger_resolves() -> None:
    """Rule 7, and the cheapest possible defence against the failure that made
    this gate's own rework necessary. It cannot check that a citation SAYS what
    an entry claims — nothing can — but a path that does not exist, or a line
    past the end of a file, is a citation nobody opened.

    It earned its place immediately: it caught an invented
    `docs/FRACTIONAL_TRADING.md` in a `source` written during the rework that
    added this rule.
    """
    broken = broken_citations(load_ledger(), Path(__file__).resolve().parent.parent)
    assert not broken, "\n".join(f"  {s}: cites {c} - {w}" for s, w, c in broken)


def test_scope_has_not_silently_narrowed() -> None:
    """A scoped path or config class that stopped existing would make the gate
    pass by covering less. `collect_sites` raises on a missing path; this
    pins the scoped config classes for the same reason.
    """
    import src.config as config_module

    for name in SCOPED_CONFIG_CLASSES:
        assert hasattr(config_module, name), f"{name} left src/config.py"
    assert "src/risk" in SCOPED_PATHS
    assert "src/portfolio_constructor" in SCOPED_PATHS
    assert "src/data/technical.py" in SCOPED_PATHS
    assert "src/data/levels.py" in SCOPED_PATHS
    # Board item 130: broker.py IS the broker order.
    assert "src/execution/broker.py" in SCOPED_PATHS
    assert "src/execution/stop_repair.py" in SCOPED_PATHS
    assert "src/coverage_watchdog.py" in SCOPED_PATHS
    assert "src/pipeline.py" in SCOPED_PATHS
    assert "src/agents" in SCOPED_PATHS


# --------------------------------------------------------------------------
# Each failure mode, proven to fire.
# --------------------------------------------------------------------------


def _fixture(tmp_path: Path, module_src: str, ledger_src: str) -> tuple[Path, Path]:
    """A miniature repo: one scoped module plus a ledger, so each rule can be
    tripped in isolation without touching the real tree.

    Rules 5 (the arbitrary ratchet) and 6 (the unscoped sentinel) are
    properties of the real ledger and the real tree, so `audit` skips them for
    a fixture ledger. They are pinned against the live tree above instead.
    """
    (tmp_path / "src" / "risk").mkdir(parents=True)
    (tmp_path / "src" / "risk" / "rules.py").write_text(textwrap.dedent(module_src))
    (tmp_path / "src" / "config.py").write_text("class RiskConfig:\n    pass\n")
    for entry in SCOPED_PATHS:
        target = tmp_path / entry
        if target.exists():
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.suffix == ".py":
            target.write_text("")
        else:
            target.mkdir(parents=True, exist_ok=True)
    ledger = tmp_path / "ledger.yaml"
    ledger.write_text(textwrap.dedent(ledger_src))
    return tmp_path, ledger


def _kinds(root: Path, ledger: Path) -> set[str]:
    return {p.kind for p in audit(repo_root=root, ledger_path=ledger)}


#: A complete `arbitrary` entry, for fixtures that are testing some other rule.
_DEBT = """
            note: nothing behind it
            open_question: what heat does this account actually survive?
            cost_while_unanswered: the ceiling binds on a round number
"""


def test_a_new_number_with_no_entry_fails() -> None:
    """Rule 1, COVERAGE — the whole point of the gate."""
    root, ledger = _fixture(
        Path(pytest.importorskip("tempfile").mkdtemp()),
        "MAX_HEAT_PCT = 4.2\n",
        "numbers: []\n",
    )
    assert "unsourced" in _kinds(root, ledger)


def test_changing_a_number_without_touching_its_entry_fails() -> None:
    """Rule 2, VALUE — a number may not outlive its recorded justification."""
    root, ledger = _fixture(
        Path(pytest.importorskip("tempfile").mkdtemp()),
        "MAX_HEAT_PCT = 4.2\n",
        """
        numbers:
          - id: src.risk.rules.MAX_HEAT_PCT
            value: 3.0
            status: arbitrary
        """
        + _DEBT,
    )
    assert "value-drift" in _kinds(root, ledger)


def test_claiming_a_source_without_writing_one_fails() -> None:
    """Rule 3, GROUNDS — `sourced` is not a word you may just type."""
    root, ledger = _fixture(
        Path(pytest.importorskip("tempfile").mkdtemp()),
        "MAX_HEAT_PCT = 4.2\n",
        """
        numbers:
          - id: src.risk.rules.MAX_HEAT_PCT
            value: 4.2
            status: sourced
            source: "   "
        """,
    )
    assert "no-source" in _kinds(root, ledger)


def test_a_source_a_reader_cannot_open_fails() -> None:
    """Rule 3 again, and the rule that answers the worst finding against the
    first version of this gate. A confident paragraph passed; a paragraph is
    what was false in four places. A `source` must be a URL or a `file:line`.
    """
    root, ledger = _fixture(
        Path(pytest.importorskip("tempfile").mkdtemp()),
        "MAX_HEAT_PCT = 4.2\n",
        """
        numbers:
          - id: src.risk.rules.MAX_HEAT_PCT
            value: 4.2
            status: sourced
            source: >-
              Read off the instrument, as is well established in the
              literature, and confirmed by the desk's own measurement.
        """,
    )
    assert "unfalsifiable-source" in _kinds(root, ledger)


def test_a_source_with_a_file_and_line_passes() -> None:
    """The other half: a citation a non-author can actually open is enough.
    A rule that refused everything would be routed around inside a week.
    """
    root, ledger = _fixture(
        Path(pytest.importorskip("tempfile").mkdtemp()),
        "MAX_HEAT_PCT = 4.2\n",
        """
        numbers:
          - id: src.risk.rules.MAX_HEAT_PCT
            value: 4.2
            status: sourced
            source: the derivation at src/risk/rules.py::MAX_HEAT_PCT
        """,
    )
    assert not _kinds(root, ledger)


def test_an_arbitrary_number_with_no_open_question_fails() -> None:
    """`arbitrary` used to be the cheapest field in the ledger: no note, no
    owner, no date, no question. It must be the most expensive, because it is
    a debt the desk is carrying.
    """
    root, ledger = _fixture(
        Path(pytest.importorskip("tempfile").mkdtemp()),
        "MAX_HEAT_PCT = 4.2\n",
        """
        numbers:
          - id: src.risk.rules.MAX_HEAT_PCT
            value: 4.2
            status: arbitrary
            note: nothing behind it
        """,
    )
    assert "incomplete-debt" in _kinds(root, ledger)


def test_an_arbitrary_number_with_no_stated_cost_fails() -> None:
    """The second half of the debt: what the desk pays while the question is
    open. Without it an arbitrary entry cannot be prioritised against any
    other, which is how twenty of them sat live for a week.
    """
    root, ledger = _fixture(
        Path(pytest.importorskip("tempfile").mkdtemp()),
        "MAX_HEAT_PCT = 4.2\n",
        """
        numbers:
          - id: src.risk.rules.MAX_HEAT_PCT
            value: 4.2
            status: arbitrary
            note: nothing behind it
            open_question: what heat does this account actually survive?
        """,
    )
    assert "incomplete-debt" in _kinds(root, ledger)


def test_a_derivation_whose_base_moved_fails() -> None:
    """Rule 4, BASE DRIFT — the class the brief asked about by name.

    A number is derived from a base and both are recorded. The base then
    changes, as `min_stop_atr_multiple` did on 2026-09-10 (commit 0088328c,
    3.0 -> 2.5). The derivation no longer describes the live geometry, so the
    derived number is arbitrary again, and the build says so instead of
    letting a stale justification sit there looking respectable.
    """
    root, ledger = _fixture(
        Path(pytest.importorskip("tempfile").mkdtemp()),
        "STOP_BASE_ATR = 2.5\nSTOP_RANGE_SCALE = 0.9\n",
        """
        numbers:
          - id: src.risk.rules.STOP_BASE_ATR
            value: 2.5
            status: arbitrary
            note: the base
            open_question: what stop width does this desk's own MAE support?
            cost_while_unanswered: every unbacked stop scales off it
          - id: src.risk.rules.STOP_RANGE_SCALE
            value: 0.9
            status: derived
            derived_from: src.risk.rules.STOP_BASE_ATR
            base_value: 1.5
        """,
    )
    assert "base-drift" in _kinds(root, ledger)


def test_a_derivation_whose_base_still_holds_passes() -> None:
    """The other half of rule 4: it must not cry wolf while the base stands.
    A base-drift check that fires on a valid derivation would be routed around
    inside a week.
    """
    root, ledger = _fixture(
        Path(pytest.importorskip("tempfile").mkdtemp()),
        "STOP_BASE_ATR = 2.5\nSTOP_RANGE_SCALE = 0.9\n",
        """
        numbers:
          - id: src.risk.rules.STOP_BASE_ATR
            value: 2.5
            status: arbitrary
            note: the base
            open_question: what stop width does this desk's own MAE support?
            cost_while_unanswered: every unbacked stop scales off it
          - id: src.risk.rules.STOP_RANGE_SCALE
            value: 0.9
            status: derived
            derived_from: src.risk.rules.STOP_BASE_ATR
            base_value: 2.5
        """,
    )
    assert not _kinds(root, ledger)


def test_a_renamed_or_deleted_number_leaves_a_detectable_orphan() -> None:
    """A stale entry would let coverage look complete while the code moved."""
    root, ledger = _fixture(
        Path(pytest.importorskip("tempfile").mkdtemp()),
        "# the constant was deleted\n",
        """
        numbers:
          - id: src.risk.rules.MAX_HEAT_PCT
            value: 4.2
            status: arbitrary
        """
        + _DEBT,
    )
    assert "orphan" in _kinds(root, ledger)


def test_zero_is_not_a_site_but_one_is() -> None:
    """0 is an empty default and the bottom of an ordinal scale. 1 is not the
    identity when it is an ATR multiple, and excluding it hid the hard floor
    under every stop this desk sets.
    """
    root, ledger = _fixture(
        Path(pytest.importorskip("tempfile").mkdtemp()),
        "NEUTRAL_ZERO = 0.0\nABSOLUTE_MIN_STOP_ATR = 1.0\n",
        "numbers: []\n",
    )
    problems = audit(repo_root=root, ledger_path=ledger)
    assert [p.site_id for p in problems] == ["src.risk.rules.ABSOLUTE_MIN_STOP_ATR"]


def test_a_default_pointed_at_an_unscoped_module_does_not_vanish() -> None:
    """The evasion in miniature. `Config.floor = SOME_NAME` imported from a
    module nobody scoped used to remove the number from the gate entirely.
    """
    root = Path(pytest.importorskip("tempfile").mkdtemp())
    root, ledger = _fixture(
        root,
        "from src.hidden import HIDDEN_FLOOR\n\n\nclass RulesConfig:\n    floor: float = HIDDEN_FLOOR\n",
        "numbers: []\n",
    )
    (root / "src" / "hidden.py").write_text("HIDDEN_FLOOR = 2.75\n")
    problems = audit(repo_root=root, ledger_path=ledger)
    assert [(p.kind, p.site_id) for p in problems] == [("unsourced", "src.risk.rules.RulesConfig.floor")]


# --------------------------------------------------------------------------
# Rules (c), (d), (e): the shapes (a)/(b) could not see. 2026-09-19.
# --------------------------------------------------------------------------


def test_the_named_hidden_trade_numbers_are_now_sites() -> None:
    """Every number the 2026-09-19 brief named as invisible, by the shape it
    hid in: two parameter defaults, a class attribute, and board item 138's
    inline order-price buffers.
    """
    ids = {site.site_id for site in collect_sites()}
    # (c) parameter defaults.
    # `_clamp_queued_earnings_buys(max_pct)` was rule (c)'s original example
    # and is GONE: board item 186 (2026-10-01) deleted the 5%-of-book clamp
    # rather than re-deriving it — an unread filing refuses the BUY. Rule (c)
    # is pinned on the other parameter default it found, so the shape stays
    # covered and a new one still cannot arrive unseen.
    assert not any(i.startswith("src.pipeline_risk_gate.RiskGate._refuse_queued_earnings_buys") for i in ids)
    assert "src.agents.portfolio_manager.PortfolioManagerAgent.decide(starter_risk_pct)" in ids
    # (d) class attributes.
    assert "src.execution.broker.AlpacaBroker._MAX_REPLACEMENT_HOPS" in ids
    # `_EMERGENCY_LIMIT_CUSHION_PCT` (dead) was deleted 2026-10-09 and
    # `STOP_LIMIT_BUFFER_PCT` 2026-10-13; the attribute above proves rule (d).
    assert "src.pipeline.TradingPipeline._EMERGENCY_LIMIT_CUSHION_PCT" not in ids
    # (e) item 138: inline order-price factors. The de-lever ladder's own
    # SELL/COVER fill limits are no longer inline % literals — the emergency
    # de-lever now crosses the LIVE quote or sends a MARKET order
    # (delever-live-fill), so `_enforce_gross_ceiling` carries no factor site.
    # `_force_delever` now has exactly ONE factor site, its conservative
    # proceeds haircut: the sweep-sizing cushion that used to sit ahead of it
    # was reformulated away on 2026-09-30 (board item 182), which renumbered
    # the haircut factor[1] -> factor[0]. One site still proves rule (e).
    # 2026-10-15: the haircut factor site is gone (proceeds credited at the live quote).
    assert "src.delever.forced.DeleverForced._force_delever:factor[0]" not in ids
    assert "src.pipeline.TradingPipeline._force_delever:factor[1]" not in ids
    # The decision-path and midday exit pads were DELETED 2026-10-09: every
    # ordinary exit is now a plain market order (src/exit_quote.py), and the
    # rotation projection's own pad went the same day (it reads the live
    # quote). The de-lever haircut above still proves rule (e).
    assert "src.stage_execution.ExecutionStage._run_session:factor[0]" not in ids
    assert "src.pipeline_exits.ExitEngineMixin._midday_execute_llm_actions:factor[2]" not in ids
    assert "src.rotation_projection._projected_post_sale_book:factor[1]" not in ids


def test_a_parameter_default_is_a_site() -> None:
    """Rule (c). A cap passed nowhere and defaulted in a signature is as live
    as a module constant, and was invisible."""
    root, ledger = _fixture(
        Path(pytest.importorskip("tempfile").mkdtemp()),
        """
        def clamp(decisions, *, max_pct: float = 5.0, floor=0.0):
            return decisions


        class Engine:
            def check(self, cluster_pct: float = 50.0):
                return cluster_pct
        """,
        "numbers: []\n",
    )
    problems = audit(repo_root=root, ledger_path=ledger)
    assert sorted(p.site_id for p in problems if p.kind == "unsourced") == [
        "src.risk.rules.Engine.check(cluster_pct)",
        "src.risk.rules.clamp(max_pct)",
    ]


def test_an_attribute_on_any_class_is_a_site() -> None:
    """Rule (d). The 3% stop-limit buffer is an attribute of the broker class,
    not a `*Config` field, and no rule saw it."""
    root, ledger = _fixture(
        Path(pytest.importorskip("tempfile").mkdtemp()),
        """
        class Broker:
            STOP_LIMIT_BUFFER_PCT = 0.03
            retries: int = 0

            class Inner:
                hops = 8
        """,
        "numbers: []\n",
    )
    problems = audit(repo_root=root, ledger_path=ledger)
    assert sorted(p.site_id for p in problems if p.kind == "unsourced") == [
        "src.risk.rules.Broker.Inner.hops",
        "src.risk.rules.Broker.STOP_LIMIT_BUFFER_PCT",
    ]


def test_a_near_one_price_multiplier_is_a_site_and_unit_arithmetic_is_not() -> None:
    """Rule (e), both halves. The margins fire -- including both arms of a
    conditional and a folded `1 - 0.03` -- and the unit conversions, the
    sign flip, the epsilon and constant arithmetic do not."""
    root, ledger = _fixture(
        Path(pytest.importorskip("tempfile").mkdtemp()),
        """
        def exit_limit(price, is_cover, bps, deficit, qty):
            sell = round(price * 0.995, 2)
            ladder = price * (1.01 if is_cover else 0.99)
            stop = price * (1 - 0.03)
            cushion = deficit * 1.02
            skip = price / 1.02
            cap = price * (1 + bps / 10_000.0)
            flipped = qty * -1.0
            days = 365 * 5
            eps = qty + 1e-9
            pct = qty / price * 100.0
            half = qty / 2
            return sell, ladder, stop, cushion, skip, cap, flipped, days, eps, pct, half
        """,
        "numbers: []\n",
    )
    found = {p.site_id: p.detail for p in audit(repo_root=root, ledger_path=ledger) if p.kind == "unsourced"}
    assert sorted(found) == [f"src.risk.rules.exit_limit:factor[{n}]" for n in range(6)]
    assert "0.995" in found["src.risk.rules.exit_limit:factor[0]"]
    assert "0.97" in found["src.risk.rules.exit_limit:factor[3]"]


def test_the_new_shapes_do_not_leak_into_the_unscoped_sentinel() -> None:
    """The sentinel's count is defined as module-level constants. Rules
    (c)-(e) outside scope would have moved it by hundreds for no reason and
    turned its ceiling into noise."""
    root, ledger = _fixture(
        Path(pytest.importorskip("tempfile").mkdtemp()),
        "X = 1\n",
        "numbers: []\n",
    )
    (root / "src" / "unscoped.py").write_text("def f(a=5.0):\n    return a * 0.99\n\n\nclass K:\n    B = 3\n")
    assert collect_unscoped_sites(root) == []


def test_item_90_classification_partitions_the_whole_ledger() -> None:
    """Item 90's three states, produced FROM the ledger rather than by hand.

    The item's closing condition is that every trade-governing number sits in
    one of exactly three states: sourced or measured; ratified as a structural
    bound with the reason recorded; or unsourceable today with a NAMED
    recording that would settle it. Anything in none of the three is the
    remaining work. Hand-reading 329 rows to find out is exactly the kind of
    check this desk has watched slip, so the classification is mechanical and
    every row lands in exactly one bucket.
    """
    from src.number_sources import classification, load_ledger

    ledger = load_ledger()
    buckets = classification(ledger)
    flat = [site_id for group in buckets.values() for site_id in group]
    assert sorted(flat) == sorted(ledger), (
        "the classification must cover every ledger row exactly once; a row "
        "that falls through it is a trade number in no known state."
    )
    assert len(flat) == len(set(flat))


def test_a_settlement_route_that_cannot_be_acted_on_is_refused() -> None:
    """A malformed route reads as an answer and is not one, so it is worse
    than an honest blank: it takes the row out of the outstanding count."""
    from src.number_sources import settlement_route_problem

    good = {
        "kind": "recording",
        "state": "built",
        "where": "src/storage/db.py",
        "records": "the per-closed-trade entry stop, its basis and the "
        "maximum adverse excursion reached before the exit",
        "closes_when": "enough closed trades carry the columns to show whether the floor was ever violated in practice",
        # A BUILT recording also has to name fields the storage layer really
        # writes (2026-10-01): three recordings were found collecting nothing
        # while their columns existed, so "built" now has to be falsifiable.
        "writes": ["trades.entry_atr", "trades.max_adverse_excursion"],
    }
    assert settlement_route_problem({"settles_by": good}) is None
    for field in ("writes",):
        broken = dict(good)
        broken.pop(field)
        assert field in str(settlement_route_problem({"settles_by": broken}))
    dead = dict(good)
    dead["writes"] = ["trades.nothing_in_the_code_ever_writes_this"]
    assert "no INSERT or UPDATE" in str(settlement_route_problem({"settles_by": dead}))
    assert settlement_route_problem({}) == "no `settles_by` block"
    assert "not a mapping" in str(settlement_route_problem({"settles_by": "soon"}))
    for field in ("kind", "state", "where", "records", "closes_when"):
        broken = dict(good)
        broken.pop(field)
        assert field in str(settlement_route_problem({"settles_by": broken}))
    for field, value in (("kind", "vibes"), ("state", "someday")):
        broken = dict(good)
        broken[field] = value
        assert "expected one of" in str(settlement_route_problem({"settles_by": broken}))
    for field in ("records", "closes_when"):
        broken = dict(good)
        broken[field] = "later"
        assert "not a route" in str(settlement_route_problem({"settles_by": broken}))


def test_the_book_wide_ceilings_route_to_a_recording_not_to_the_owner() -> None:
    """Item 186's aggregate ceilings, guarded against their own failure mode.

    These rows are the ceilings that ration the whole book: total
    at-risk, the terminal sector bound and its constructor mirror. Twice now the item has tried to
    close them by asking the owner what concentration he accepts, and the
    owner ruled on 2026-09-30 that risk is read per name and never set as a
    global dial, so that question may not come back. What each row owes
    instead is a recording. This fails the build if one loses its settlement
    route or starts asking the owner for a value again.
    """
    from src.number_sources import load_ledger, settlement_route_problem

    ledger = load_ledger()
    ceilings = ("src.config.RiskConfig.max_portfolio_risk_pct",)
    for site_id in ceilings:
        if (entry := ledger[site_id])["status"] == "owner-ruled":
            continue  # a dated owner decision owes no settlement route
        # A ceiling that is a MIRROR of another ceiling owes nothing of its
        # own: it has no settlement route because a `derived` row may not
        # carry one (the validator rejects `settles_by` on any non-arbitrary
        # status), and it cannot drift away from what it mirrors because
        # `base-drift` fails the build the moment the base moves without it.
        # What this test is protecting -- that the recording is still owed by
        # somebody and the owner is still not being asked -- is therefore
        # checked on the BASE instead, which must itself be one of these
        # ceilings. Added 2026-10-02 when
        # `ConstructorConfig.max_sector_hard_pct` was recorded as the mirror of
        # `RiskConfig.SECTOR_HARD_CEILING_MAX` (src/pipeline.py:363).
        if entry.get("status") == "derived":
            base_id = entry.get("derived_from")
            assert base_id in ceilings, (
                f"{site_id} is a book-wide ceiling recorded as derived from "
                f"{base_id!r}, which is not itself one of these ceilings, so "
                "the recording this test guards would be owed by nobody."
            )
            continue
        assert settlement_route_problem(entry) is None, (
            f"{site_id} is a book-wide ceiling with no actionable recording: {settlement_route_problem(entry)}"
        )
        assert entry["settles_by"]["kind"] == "recording", (
            f"{site_id} cannot settle by anything but a recording: there is "
            "no instrument a book-wide ceiling could be read off."
        )
        question = str(entry.get("open_question", ""))
        assert "WITHDRAWN" in question, (
            f"{site_id} must say its appetite question is withdrawn, not "
            "leave it standing as though the owner still owes an answer."
        )
        lowered = question.lower()
        for phrase in ("the owner accept", "does the owner"):
            start = 0
            while (hit := lowered.find(phrase, start)) != -1:
                assert "withdrawn" in lowered[hit : hit + 160], (
                    f"{site_id} asks the owner for a concentration he "
                    "accepts without marking it withdrawn, which his "
                    "2026-09-30 ruling on global risk dials bars."
                )
                start = hit + 1
