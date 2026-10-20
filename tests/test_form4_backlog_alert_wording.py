"""Owner wording of the pre-open insider-filing alert (20 Oct 12:02 UTC)."""

import pytest

from src.research_continuity.form4_backlog import Form4BacklogRecorder
from src.util.time import et_today


class _Journal:
    def persist_evidence(self, **_):
        pass


CODES = ("days_not_queried", "edgar_total_unreadable", "scan_cap_reached")


def _refresh(**overrides) -> dict:
    base = {
        "watched_read_through": et_today().isoformat(),
        "watched_pending_filings": 0,
        "watched_unchecked_names": [],
        "watched_drain_ran": True,
        "watched_names": 3,
        "watched_names_read_through": 3,
        "discovery_cap_reached": True,
        "edgar_coverage": {
            "known": True,
            "verified": False,
            "reasons": list(CODES),
            "edgar_total": 1238,
            "enumerated": 1196,
            "days_queried": 6,
            "days_with_total": 5,
            "days_in_window": 366,
        },
    }
    base.update(overrides)
    return base


def _alert(monkeypatch, refresh: dict) -> str:
    import src.notifier as notifier

    sent: list[str] = []
    monkeypatch.setattr(notifier, "send_owner_alert", lambda text, **_: sent.append(text))
    Form4BacklogRecorder(journal=_Journal())._alert_form4_backlog_before_open(refresh)
    assert len(sent) == 1
    return sent[0]


def test_no_reason_code_appears_in_owner_text(monkeypatch):
    text = _alert(monkeypatch, _refresh(watched_names=82, watched_names_read_through=82))
    for code in CODES:
        assert code not in text
    assert "no usable count for 1 of the 6 days" in text


def test_all_watched_read_does_not_say_did_not_finish(monkeypatch):
    text = _alert(monkeypatch, _refresh(watched_names=82, watched_names_read_through=82))
    assert "did not finish" not in text
    assert text.startswith("All 82 companies the desk watches had every insider filing read")
    assert "1196 of 1238" in text
    assert "6 most recent days of a 366-day look-back window" in text


@pytest.mark.parametrize(
    "overrides",
    [
        {"watched_names_read_through": 2},
        {"watched_pending_filings": 2},
        {"watched_unchecked_names": ["ZZZA"]},
        {"watched_read_through": ""},
    ],
)
def test_watched_unread_still_says_did_not_finish(monkeypatch, overrides):
    text = _alert(monkeypatch, _refresh(**overrides))
    assert text.startswith("Insider-filing check did not finish this morning")
    assert "without complete insider evidence" in text
