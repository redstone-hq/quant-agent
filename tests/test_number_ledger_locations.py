"""Every ledger row's `site` must be the file that really defines its number."""

from pathlib import Path

from src import number_sources


def test_every_row_site_is_the_file_that_defines_the_number():
    ledger = number_sources.load_ledger()
    live = {s.site_id: s.path for s in number_sources.collect_sites()}
    wrong = {i: (e.get("site"), live[i]) for i, e in ledger.items() if i in live and e.get("site") != live[i]}
    assert not wrong, f"ledger rows whose site is not where the number lives (ledger, live): {wrong}"


def test_every_row_site_file_exists_and_every_row_has_a_live_definition():
    ledger = number_sources.load_ledger()
    live = {s.site_id for s in number_sources.collect_sites()}
    root = Path(number_sources.__file__).resolve().parent.parent
    missing = [i for i, e in ledger.items() if not (root / str(e.get("site"))).is_file()]
    orphans = [i for i in ledger if i not in live]
    assert not missing, f"site file missing: {missing}"
    assert not orphans, f"row has no definition site anywhere: {orphans}"
