"""Scanner-shape tests split out of test_number_sources.py: what `collect_sites` sees."""

from __future__ import annotations

from src.number_sources import NEUTRAL_VALUES, _scan_module, collect_sites


def test_a_default_bound_to_a_name_is_still_a_site() -> None:
    """The one-line evasion. A `*Config` default written as a NAME rather than
    a literal used to return None from `_numeric` and disappear — so pointing
    a scoped field at a constant in an unscoped module removed any number from
    the gate. Constant arithmetic (`5 * 366`) did the same.
    """
    sites = {s.site_id: s.value for s in collect_sites()}
    assert sites["src.config.RiskConfig.max_position_risk_pct"] == 5.0
    assert sites["src.config.SmartMoneyConfig.insider_history_retention_days"] == 5 * 366


def test_the_atr_unit_is_in_scope_not_only_its_multipliers() -> None:
    """`ATR_PERIOD` was out of scope while every ATR multiple in the ledger was
    in it. Watching the multiplier and not the unit is not a boundary.
    """
    ids = {site.site_id for site in collect_sites()}
    assert "src.data.technical.ATR_PERIOD" in ids
    assert "src.data.levels.PIVOT_WINDOW" in ids
    assert "src.pipeline_stages.MAX_ENTRY_SLIPPAGE_BPS" in ids
    assert "src.verdicts.CONVICTION_SCORE['medium']" in ids


def test_one_atr_is_not_treated_as_an_identity() -> None:
    """`1.0` was blanket-excluded as "the identity, not a setting", with the
    claim that every audited number fell outside the excluded set. Measured
    false: the hard floor under every stop this desk sets is exactly 1.0 ATR.
    """
    assert 1.0 not in NEUTRAL_VALUES
    assert -1.0 not in NEUTRAL_VALUES
    ids = {site.site_id for site in collect_sites()}
    assert "src.config.RiskConfig.absolute_min_stop_atr_multiple" in ids
    assert "src.portfolio_constructor.config.ConstructorConfig.absolute_min_stop_atr_multiple" in ids
    assert "src.config.CashReserveConfig.pct" in ids


def test_numbers_inside_a_tuple_of_pairs_are_sites() -> None:
    """Trade-governing numbers can live inside a tuple of pairs (the gross
    ladder's rungs), not as bare field defaults. A scanner that only reads
    top-level defaults would not see them.
    """
    ids = {site.site_id for site in collect_sites()}
    assert "src.risk.gross_ladder.GROSS_LADDER[1][1]" in ids


def test_named_dictionary_keys_keep_distinct_number_identities() -> None:
    """A mapping keyed by explicit constants must not collapse every numeric
    leaf onto the scanner's ``[?]`` fallback identity. The source keeps the
    readable insider-class constants; the scanner owns resolving their sites.
    """
    ids = {site.site_id for site in collect_sites()}
    assert "src.data.insider_signal._WEIGHTS[OPPORTUNISTIC]" in ids
    assert "src.data.insider_signal._WEIGHTS[INDETERMINATE]" in ids
    assert "src.data.insider_signal._WEIGHTS[?]" not in ids


def test_destructured_module_constants_keep_distinct_number_identities(tmp_path) -> None:
    """Each name in a tuple assignment owns its paired literal.

    Tuple assignments were previously absent because the scanner only accepted
    a single Name on the left-hand side of a module assignment.
    """
    mod = tmp_path / "pair_mod.py"
    mod.write_text("_A, _B, _C = 5, 21, 63\n")
    sites = {s.site_id: s.value for s in _scan_module(mod, "pair_mod.py", None, tmp_path)}
    assert sites["pair_mod._A"] == 5
    assert sites["pair_mod._B"] == 21
    assert sites["pair_mod._C"] == 63


def test_result_dataclasses_are_not_sites() -> None:
    """The `*Config` rule is what keeps the signal alive. Result and DTO
    dataclasses in the same scoped files carry numeric defaults too, and
    flagging them would bury the numbers that matter — which is precisely the
    failure that made the earlier whole-codebase idea unworkable.
    """
    ids = {site.site_id for site in collect_sites()}
    assert not [i for i in ids if ".GrossCeilingOutcome." in i]
    assert not [i for i in ids if ".PortfolioVolEstimate." in i]
