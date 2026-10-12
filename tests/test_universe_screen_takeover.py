"""Takeover role: bidder vs target, closed and terminated deals, proxies.

Hermetic. Every SEC read is a recorded snippet below; nothing touches the
network. The old rule (any latest takeover-form filing without a later
8-K 1.02 is pending) fails every test here that names a role or a close.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from src import universe_screen as us
from src.data import sec_filings as sf
from tests.test_universe_screen import TH, _sources

# ----------------------------------------------------------- snippets ----
# Shape of EDGAR's `<accession>-index-headers.html` SGML header for a
# third-party tender offer: the target under SUBJECT COMPANY, the bidder
# under FILED BY. CIKs are fictitious.

TENDER_HEADER = """<html><pre>
<ACCEPTANCE-DATETIME>20260901163012
ACCESSION NUMBER:		0001193125-26-000001
CONFORMED SUBMISSION TYPE:	SC TO-T
PUBLIC DOCUMENT COUNT:		12
FILED AS OF DATE:		20260901

SUBJECT COMPANY:

	COMPANY DATA:
		COMPANY CONFORMED NAME:			TARGET CORP
		CENTRAL INDEX KEY:			0000111111
		STANDARD INDUSTRIAL CLASSIFICATION:	SERVICES [7370]

	FILING VALUES:
		FORM TYPE:		SC TO-T
		SEC ACT:		1934 Act

FILED BY:

	COMPANY DATA:
		COMPANY CONFORMED NAME:			BIDDER INC
		CENTRAL INDEX KEY:			0000222222

	FILING VALUES:
		FORM TYPE:		SC TO-T
</pre></html>"""

# A self-filed merger proxy: one FILER, no SUBJECT COMPANY.
SELF_HEADER = """<html><pre>
ACCESSION NUMBER:		0001193125-26-000002
CONFORMED SUBMISSION TYPE:	DEFM14A
FILER:

	COMPANY DATA:
		COMPANY CONFORMED NAME:			SELF CORP
		CENTRAL INDEX KEY:			0000333333
</pre></html>"""

# Proxy body with the Rule 14a-21(c) advisory vote, as a target's proxy
# carries it (hyphen is the non-breaking one proxies often use).
TARGET_PROXY = """<html><body><p>THE MERGER</p>
<p>Proposal 2: to approve, on a non&#8209;binding advisory basis, the
compensation that may be paid or become payable to the named executive
officers of Self Corp in connection with the merger, as required by
Rule 14a&#8209;21(c) under the Exchange Act (the &ldquo;golden parachute
compensation&rdquo;).</p></body></html>"""

# A bidder's own proxy: holders vote on issuing shares, no golden-parachute
# vote anywhere in the document.
BIDDER_PROXY = """<html><body><p>Proposal 1: to approve the issuance of
shares of common stock to the stockholders of Target Corp pursuant to the
merger agreement.</p><p>Proposal 2: adjournment.</p></body></html>"""

# A joint proxy (both companies' holders vote in one document): the
# parachute vote belongs to one side but the document attributes none.
JOINT_PROXY = """<html><body><p>JOINT PROXY STATEMENT/PROSPECTUS</p>
<p>Valaris stockholders: advisory vote on golden parachute compensation
under Item 402(t) of Regulation S-K.</p>
<p>Transocean shareholders: approval of the share issuance.</p></body></html>"""


def _ref(cik, form="SC TO-T", accession="0001193125-26-000001", doc="d1.htm"):
    return sf.FilingRef(cik=cik, accession=accession, primary_doc=doc, form=form)


def _reader(pages: dict[str, str]):
    """A role reader whose only SEC is the recorded `pages` (by URL suffix)."""
    calls = []

    def get(url):
        calls.append(url)
        for suffix, body in pages.items():
            if url.endswith(suffix):
                return body
        raise RuntimeError(f"unrecorded SEC read: {url}")

    reader = sf.TakeoverRoleReader(get)
    return reader, calls


def _screen(filings, reader):
    sources = _sources(filings=filings)
    sources.get_role = reader.role
    return us.screen_symbol("ACME", sources, TH)


# ------------------------------------------------------------- header ----


def test_header_roles_split_subject_from_filer():
    assert sf.parse_header_roles(TENDER_HEADER) == ({"111111"}, {"222222"})
    assert sf.header_role(TENDER_HEADER, "0000111111") == sf.TARGET
    assert sf.header_role(TENDER_HEADER, 222222) == sf.BIDDER
    assert sf.header_role(SELF_HEADER, "333333") == "self"


def test_a_bidder_is_not_pending():
    reader, calls = _reader({"-index-headers.html": TENDER_HEADER})
    result = _screen([("SC TO-T", "2026-09-01", "", _ref("222222"))], reader)
    assert result.failures == []
    assert len(calls) == 1  # the header alone decides; no document read


def test_a_target_is_pending():
    reader, _ = _reader({"-index-headers.html": TENDER_HEADER})
    result = _screen([("SC TO-T", "2026-09-01", "", _ref("111111"))], reader)
    assert result.failures == ["pending_takeover"]
    assert result.measured["takeover_filing"] == "2026-09-01"


# ------------------------------------------------------ closed / ended ----


@pytest.mark.parametrize(
    "closing",
    [
        ("25", "2026-09-20", ""),
        ("25-NSE", "2026-09-20", ""),
        ("15-12B", "2026-09-25", ""),
        ("8-K", "2026-09-19", "2.01,9.01"),
    ],
)
def test_a_closed_deal_is_not_pending(closing):
    reader, calls = _reader({})
    filings = [("SC TO-T", "2026-09-01", "", _ref("111111")), closing]
    assert us.check_takeover(filings, reader.role) == ([], {})
    assert calls == []  # a closed deal never costs a SEC read


def test_a_terminated_deal_is_not_pending():
    reader, _ = _reader({})
    filings = [("DEFM14A", "2026-08-01", "", _ref("333333", "DEFM14A")), ("8-K", "2026-08-30", "1.02")]
    assert us.check_takeover(filings, reader.role) == ([], {})


def test_a_new_deal_after_an_old_close_is_pending_again():
    reader, _ = _reader({"-index-headers.html": TENDER_HEADER})
    filings = [
        ("SC TO-T", "2026-03-01", "", _ref("111111")),
        ("25", "2026-04-01", ""),
        ("SC 14D9", "2026-09-01", "", _ref("111111", "SC 14D9", "0001193125-26-000009")),
    ]
    assert us.check_takeover(filings, reader.role) == (["pending_takeover"], {"takeover_filing": "2026-09-01"})


# ---------------------------------------------------------- proxies ------


def test_a_self_filed_proxy_with_the_parachute_vote_is_the_target():
    reader, calls = _reader({"-index-headers.html": SELF_HEADER, "/d1.htm": TARGET_PROXY})
    result = _screen([("DEFM14A", "2026-09-01", "", _ref("333333", "DEFM14A", "0001193125-26-000002"))], reader)
    assert result.failures == ["pending_takeover"]
    assert len(calls) == 2  # header, then the whole document


def test_a_self_filed_proxy_without_the_vote_is_not_pending():
    reader, _ = _reader({"-index-headers.html": SELF_HEADER, "/d1.htm": BIDDER_PROXY})
    result = _screen([("DEFM14A", "2026-09-01", "", _ref("333333", "DEFM14A", "0001193125-26-000002"))], reader)
    assert result.failures == []


def test_a_joint_proxy_is_inconclusive_never_dropped_never_admitted():
    reader, _ = _reader({"-index-headers.html": SELF_HEADER, "/d1.htm": JOINT_PROXY})
    result = _screen([("DEFM14A", "2026-09-01", "", _ref("333333", "DEFM14A", "0001193125-26-000002"))], reader)
    assert result.failures == ["takeover_role_undetermined"]
    assert result.inconclusive and not result.permanent
    assert us.plain_reasons(result.failures) == "role undetermined"


def test_vote_search_covers_every_spelling():
    assert sf.proxy_role("... Item 402(t) of Regulation S&#8209;K ...") == sf.TARGET
    assert sf.proxy_role("... GOLDEN&nbsp;PARACHUTE ...") == sf.TARGET
    assert sf.proxy_role("<p>Rule 14a‑21(c)</p>".encode()) == sf.TARGET  # bytes, non-breaking hyphen
    assert sf.proxy_role("<p>share issuance only</p>") == sf.NOT_TARGET


# ---------------------------------------------------------- failures -----


def test_a_failed_sec_read_fails_closed():
    reader, _ = _reader({})  # every read raises
    result = _screen([("SC TO-T", "2026-09-01", "", _ref("111111"))], reader)
    assert result.failures == ["takeover_lookup_failed"]
    assert result.inconclusive


def test_role_is_read_once_per_filing():
    reader, calls = _reader({"-index-headers.html": TENDER_HEADER})
    row = ("SC TO-T", "2026-09-01", "", _ref("111111"))
    us.check_takeover([row], reader.role)
    us.check_takeover([row], reader.role)
    assert len(calls) == 1


def test_submissions_rows_carry_a_locator():
    payload = {
        "filings": {
            "recent": {
                "form": ["SC 14D9", "8-K"],
                "filingDate": ["2026-09-01", "2026-08-01"],
                "items": ["", "1.01"],
                "accessionNumber": ["0001193125-26-000003", "0001193125-26-000004"],
                "primaryDocument": ["d14d9.htm", "d8k.htm"],
            }
        }
    }
    rows = sf.filing_rows(payload, "0000111111")
    assert rows[0][:3] == ("SC 14D9", "2026-09-01", "")
    locator = rows[0][3]
    assert locator == {
        "cik": "111111",
        "accession": "0001193125-26-000003",
        "primary_doc": "d14d9.htm",
        "form": "SC 14D9",
    }
    assert json.loads(json.dumps(rows))[0][3] == locator  # survives the daily JSON cache
    ref = sf.FilingRef.of(locator)
    reader = sf.TakeoverRoleReader(lambda url: TENDER_HEADER)
    assert reader.header_url(ref).endswith("/111111/000119312526000003/0001193125-26-000003-index-headers.html")
    assert reader.document_url(ref).endswith("/111111/000119312526000003/d14d9.htm")
    assert reader.role(locator) == sf.TARGET


def test_a_row_without_a_locator_or_reader_is_undetermined():
    # The form alone never decides (the old rule did, and dropped bidders).
    assert us.check_takeover([("DEFM14A", "2026-09-01", "")]) == (
        ["takeover_role_undetermined"],
        {"takeover_filing": "2026-09-01"},
    )
    assert us.check_takeover([("DEFM14A", "2026-09-01", "", {"cik": "1"})]) == (
        ["takeover_role_undetermined"],
        {"takeover_filing": "2026-09-01"},
    )


# ------------------------------------------------- daily reader, wired ----

SUBMISSIONS = {
    "filings": {
        "recent": {
            "form": ["SC TO-T", "8-K"],
            "filingDate": ["2026-09-01", "2026-08-01"],
            "items": ["", "1.01"],
            "accessionNumber": ["0001193125-26-000001", "0001193125-26-000004"],
            "primaryDocument": ["d1.htm", "d8k.htm"],
        }
    }
}


class _Response:
    def __init__(self, body):
        self._body = body

    def json(self):
        return self._body

    @property
    def text(self):
        return self._body


class _StubSEC:
    """Stands in for `SECForm4Provider`: the reads it answers are recorded."""

    submissions_url = "https://sec.invalid/submissions"
    archives_url = "https://sec.invalid/Archives/edgar/data"
    urls: list[str] = []

    def __init__(self, **_):
        pass

    def listed_map(self, deadline):
        return {"222222": {"BIDR": "NYSE"}, "111111": {"TGT": "NYSE"}}

    def _get(self, url, *, params, deadline):
        type(self).urls.append(url)
        if url.endswith(".json"):
            return _Response(SUBMISSIONS)
        if url.endswith("-index-headers.html"):
            return _Response(TENDER_HEADER)
        raise RuntimeError(f"unrecorded SEC read: {url}")


def test_daily_reader_admits_the_bidder_and_drops_the_target(monkeypatch, tmp_path):
    import src.data.smart_money as sm
    from src import universe_daily as ud
    from tests.test_universe_daily import _asset, _run

    monkeypatch.setattr(sm, "SECForm4Provider", _StubSEC)
    _StubSEC.urls = []
    get_filings, get_role = ud._filings_reader(SimpleNamespace(smart_money=SimpleNamespace()), deadline=1e9)
    run, _ = _run(
        [_asset("BIDR"), _asset("TGT"), _asset("NONE")], tmp_path, get_filings_override=get_filings, get_role=get_role
    )
    assert run.records["BIDR"]["status"] == "passed"
    assert run.records["BIDR"]["failures"] == []
    assert run.records["TGT"]["failures"] == ["pending_takeover"]
    assert run.records["NONE"]["failures"] == ["takeover_status_unknown"]
    assert [u for u in _StubSEC.urls if u.endswith("-index-headers.html")] == [
        "https://sec.invalid/Archives/edgar/data/222222/000119312526000001/0001193125-26-000001-index-headers.html",
        "https://sec.invalid/Archives/edgar/data/111111/000119312526000001/0001193125-26-000001-index-headers.html",
    ]


# --------------------------------------------------- live targets note ----
# ROKU, D and TECH were flagged by the old rule on 2026-10-12 (owner's
# list). No recorded SEC snippet for them exists in this repo and tests
# never hit SEC, so the check is by rule, not by data:
#   * a target that answers a tender offer files SC 14D9 as SUBJECT
#     COMPANY, and the bidder's SC TO-T names it as SUBJECT COMPANY --
#     both paths return `target` (test_a_target_is_pending);
#   * a target that files its own DEFM14A carries the Rule 14a-21(c)
#     golden-parachute vote -- `target`
#     (test_a_self_filed_proxy_with_the_parachute_vote_is_the_target);
#   * only an all-stock JOINT proxy lands in `undetermined`, which is
#     recorded inconclusive -- never admitted, never dropped.
# So a genuine target stays flagged. The names that move are the BIDDERS
# (header: FILED BY only, or a self-filed proxy with no parachute vote),
# which are now admitted when every other check passes
# (test_daily_reader_admits_the_bidder_and_drops_the_target).
