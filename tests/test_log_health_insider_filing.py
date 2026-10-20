"""The pre-open insider-filing alert is a recognised fault, not an unknown one.

Live defect, 20 Oct 2026 08:55 ET: the health message said "DEGRADED — faults
this health check does not recognise"; the unrecognised line was the 08:02
ERROR below. Fixtures are the real log wording (both before and after the
20 Oct rewording in the alert builder).
"""

from src import log_health as L
from src.research_continuity.form4_backlog import _alert_text

COVERAGE = (
    "Insider-filing coverage this morning: read 400 of 1292 filings the service "
    "reported, covering the 3 most recent days of a 14-day look-back window."
)
OLD_WORDING = (
    "PRE-OPEN: Insider-filing check did not finish this morning: the morning read "
    "stopped at its own limit before finishing. Until the check is complete, the desk "
    "still makes its trading decisions but without complete insider evidence, and each "
    "decision records that. Existing positions and their stops are unaffected. " + COVERAGE
)
NEW_WORDING = "PRE-OPEN: " + _alert_text(
    False, 41, ["the morning read stopped at its own limit before finishing"], COVERAGE
)
ALL_READ_WORDING = "PRE-OPEN: " + _alert_text(True, 41, ["the market-wide scan stopped at its own limit"], COVERAGE)


def test_insider_filing_alert_is_classified_not_unrecognised():
    for msg in (OLD_WORDING, NEW_WORDING, ALL_READ_WORDING, COVERAGE):
        family = L.classify(msg, "ERROR")
        assert family is not None, msg
        assert family.key == "insider_filing_read_incomplete"
        assert family.key != L.UNRECOGNISED_KEY
        assert family.reason is L.SILENTLY_FAILING


def test_family_is_in_plain_words():
    family = L.classify(NEW_WORDING, "ERROR")
    assert family is not None
    for token in ("Form 4", "EDGAR", "form4", "_"):
        assert token not in family.sentence and token not in family.short_name
