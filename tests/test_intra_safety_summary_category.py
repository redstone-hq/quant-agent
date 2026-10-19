"""The standalone intra_safety session summary is routine (operational).

Naked-position alarms travel as their own `no_stop_at_all` message, sent
before the summary (main.py / src/trader_feed/naked.py), so the summary itself
is not a money-at-risk alarm. Unknown kinds must still fail closed.
"""

from src.notifier.category import CATEGORY_OPERATIONAL, CATEGORY_RISK, resolve_category
from src.notifier.sections import humanize_status


def test_intra_safety_summary_is_operational():
    assert resolve_category(None, "intra_safety") == CATEGORY_OPERATIONAL


def test_naked_alarm_kind_and_unknown_kind_stay_risk():
    assert resolve_category(None, "no_stop_at_all") == CATEGORY_RISK
    assert resolve_category(None, "ZZZ_unmapped_kind") == CATEGORY_RISK


def test_deferred_has_plain_wording():
    label = humanize_status("deferred")
    assert "no plain wording" not in label
    assert "deferred" not in label
