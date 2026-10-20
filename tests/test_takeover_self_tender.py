"""An issuer's own tender offer (buyback, option exchange) is not a takeover.

Hermetic: every SEC read is a recorded header below, shaped like EDGAR's
``<accession>-index-headers.html``. On an offeror-filed form (Schedule TO
family, SC 13E3) the issuer being both SUBJECT COMPANY and the only FILED
BY party means it is tendering for its own shares. The old rule (subject
=> target, whoever filed) read every such buyback as an open takeover
that no Form 25 / 15 / 8-K 2.01 ever closes, and refused the name for
years (DVA 2020 Dutch auction, CLS 2015 issuer bid, ARQT / ATEN / MRNA
option exchanges, measured on the 2026-10-19 screen).
"""

from __future__ import annotations

from src import universe_screen as us
from src.data import sec_filings as sf
from tests.test_universe_screen_takeover import _reader, _ref, _screen

ISSUER = "0000444444"
BIDDER = "0000222222"
BUYER_GROUP = "0000555555"


def _header(form: str, subject: str, filers: list[str]) -> str:
    blocks = [
        f"""SUBJECT COMPANY:

	COMPANY DATA:
		COMPANY CONFORMED NAME:			ISSUER CORP
		CENTRAL INDEX KEY:			{subject}

	FILING VALUES:
		FORM TYPE:		{form}
"""
    ]
    for cik in filers:
        blocks.append(
            f"""FILED BY:

	COMPANY DATA:
		COMPANY CONFORMED NAME:			PARTY {cik}
		CENTRAL INDEX KEY:			{cik}

	FILING VALUES:
		FORM TYPE:		{form}
"""
        )
    body = "\n".join(blocks)
    return f"""<html><pre>
ACCESSION NUMBER:		0001193125-26-000009
CONFORMED SUBMISSION TYPE:	{form}

{body}</pre></html>"""


def _run(form: str, header: str, extra_rows=()):
    reader, _ = _reader({"-index-headers.html": header})
    rows = [(form, "2020-01-15", "", _ref(ISSUER, form=form).locator()), *extra_rows]
    return _screen(rows, reader)


# ------------------------------------------------- (a) issuer alone ----


def test_issuer_sole_filer_of_schedule_to_is_an_issuer_tender():
    for form in ("SC TO-I", "SC TO-C", "SC TO-T/A", "SC 13E3/A"):
        header = _header(form, ISSUER, [ISSUER])
        assert sf.header_role(header, ISSUER, form) == sf.ISSUER_TENDER, form
        # The form must reach the decision: without it the old answer stands.
        assert sf.header_role(header, ISSUER) == sf.TARGET, form


def test_a_self_tender_communication_is_not_pending():
    result = _run("SC TO-C", _header("SC TO-C", ISSUER, [ISSUER]))
    assert result.failures == []


def test_an_issuer_amended_schedule_to_is_not_pending():
    result = _run("SC TO-T/A", _header("SC TO-T/A", ISSUER, [ISSUER]))
    assert result.failures == []


# ------------------------------------------- (b) third-party bidder ----


def test_a_third_party_tender_for_the_issuer_is_still_pending():
    result = _run("SC TO-T", _header("SC TO-T", ISSUER, [BIDDER]))
    assert result.failures == ["pending_takeover"]
    assert result.measured["takeover_filing"] == "2020-01-15"


# -------------------------------------------- (c) target's own 14D9 ----


def test_the_targets_own_14d9_response_is_still_pending():
    header = _header("SC 14D9", ISSUER, [ISSUER])
    assert sf.header_role(header, ISSUER, "SC 14D9") == sf.TARGET
    assert _run("SC 14D9", header).failures == ["pending_takeover"]


# ------------------------------------- (d) 13E3 with a buyer group ----


def test_a_going_private_13e3_with_a_buyer_group_is_still_pending():
    header = _header("SC 13E3", ISSUER, [ISSUER, BUYER_GROUP])
    assert sf.header_role(header, ISSUER, "SC 13E3") == sf.TARGET
    assert _run("SC 13E3", header).failures == ["pending_takeover"]


# ---------------------------------- (e) self-tender, then silence ----


def test_a_self_tender_followed_by_years_of_routine_filings_is_not_pending():
    routine = [
        ("10-K", "2021-02-20", "", None),
        ("8-K", "2023-05-01", "2.02,9.01", None),
        ("10-Q", "2026-08-01", "", None),
    ]
    result = _run("SC TO-C", _header("SC TO-C", ISSUER, [ISSUER]), routine)
    assert result.failures == []
    assert "takeover_filing" not in result.measured
