"""Coverage for the verdict, insider and market-context number slice."""

from __future__ import annotations

from src.number_ledger_counts import ratchet_violations, route_ratchet_violations
from src.number_sources import collect_sites, load_ledger, settlement_route_problem


TARGET_PATHS = {
    "src/models/analysis.py",
    "src/data/insider_signal.py",
    "src/data/context.py",
}

EXPECTED_STATUS = {
    "src.data.insider_signal._WEIGHTS[INDETERMINATE]": "arbitrary",
    "src.data.insider_signal._WEIGHTS[OPPORTUNISTIC]": "not-trade-governing",
    "src.data.insider_signal.InsiderSignalClass.weight": "not-trade-governing",
    "src.data.insider_signal.InsiderSignalThresholds.cadence_max_gap_dispersion": "derived",
    "src.data.insider_signal.InsiderSignalThresholds.cadence_max_mean_gap_days": "derived",
    "src.data.insider_signal.InsiderSignalThresholds.cadence_min_mean_gap_days": "derived",
    "src.data.insider_signal.InsiderSignalThresholds.calendar_routine_years": "derived",
    "src.data.insider_signal.InsiderSignalThresholds.min_cadence_trades": "derived",
    "src.data.context._SLOPE_LOOKBACK": "arbitrary",
    "src.data.context._CONSOLIDATION_WINDOW": "arbitrary",
}


def test_the_three_newly_scoped_modules_have_exact_ledger_coverage() -> None:
    sites = [site for site in collect_sites() if site.path in TARGET_PATHS]
    assert len(sites) == len(EXPECTED_STATUS)
    assert {site.site_id for site in sites} == set(EXPECTED_STATUS)

    ledger = load_ledger()
    for site in sites:
        assert ledger[site.site_id]["status"] == EXPECTED_STATUS[site.site_id]
        assert float(ledger[site.site_id]["value"]) == site.value


def test_every_new_arbitrary_row_has_an_actionable_route_not_a_fourth_state() -> None:
    ledger = load_ledger()
    arbitrary = {site_id: ledger[site_id] for site_id, status in EXPECTED_STATUS.items() if status == "arbitrary"}

    assert len(arbitrary) == 3
    for site_id, entry in arbitrary.items():
        assert "unsettled" not in entry, site_id
        assert settlement_route_problem(entry) is None, site_id

    # These rows pass
    # the existing identity-keyed ratchets because each is explicit and routed,
    # not because a count, history file or no-route exemption was introduced.
    assert ratchet_violations(arbitrary, set(arbitrary)) == []
    assert route_ratchet_violations(arbitrary, set()) == []
