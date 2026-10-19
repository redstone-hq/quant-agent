"""Owner health report labels for four real lines seen on 19 Oct 2026."""

from src import log_health as L


def test_extra_company_row_is_ignored_not_paid_and_lost():
    msg = "Tech analyst emitted 1 row(s) for symbols not in the submitted chunk — dropped: ['ZZZ']"
    family = L.classify(msg, "WARNING")
    assert family is not None and family.key == "extra_company_in_answer_ignored"
    assert family.reason is L.HANDLED
    assert "paid" not in family.sentence and "thrown away" not in family.sentence


def test_partial_status_is_worded_as_partial_answer():
    msg = "Morning research degraded: tech | full status={'macro': 'ok', 'tech': 'partial', 'news': 'ok'}"
    family = L.classify(msg, "ERROR")
    assert family is not None and family.key == "research_seat_partial"
    assert "could not be reached" not in family.sentence
    failed = L.classify(msg.replace("'partial'", "'failed'"), "ERROR")
    assert failed is not None and failed.key == "research_seat_unavailable"


def test_grounding_refusal_is_recognised_as_working():
    msg = (
        "Portfolio decision failed deterministic grounding: ZZZ: technical stance "
        "'strong_buy' does not support the proposed sell"
    )
    family = L.classify(msg, "ERROR")
    assert family is not None and family.key == "safety_check_refused_idea"
    assert family.reason is L.HANDLED


def test_stop_out_record_disagreement_is_recognised():
    a = "stop-out reconcile: ZZZ stop_out_pnl_unmatched — no matching sell found"
    b = "stop-out reconcile: ZZZ stop_out_ledger_short — RECORDS DISAGREE — the broker holds more"
    for msg in (a, b):
        family = L.classify(msg, "ERROR")
        assert family is not None and family.key == "records_disagree_with_broker"
