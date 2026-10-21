"""Who is the target in an SEC takeover filing: bidder, target, or unknown.

The universe screen drops a name whose latest takeover filing is still
open (`src/universe_screen.py::pending_takeover`). The filing's form alone
cannot say which side filed it: a bidder files SC TO-T about its target,
and both sides of a merger file DEFM14A. This module reads the role.

Role rules (reviewed design, 2026-10-12):

* **Header roles.** EDGAR's SGML header names the target under
  ``SUBJECT COMPANY`` and the bidder under ``FILED BY``. A CIK that is the
  subject is the target; one that appears only as the filer is the bidder.
* **Issuer self-tenders.** On an offeror-filed form (the Schedule TO
  family: SC TO-I, SC TO-T, SC TO-C; and SC 13E3) a company that is both
  the subject and the ONLY filer is tendering for its own shares (a
  Dutch-auction buyback, an employee option exchange) — nobody is taking
  it over. That is ``issuer_tender``, never pending. When other parties
  file beside the issuer (a going-private SC 13E3 with a buyer group) it
  stays the target. On a target-filed form (SC 14D9) filer == subject is
  the target answering a bid, so it stays the target.
* **Self-filed proxies** (DEFM14A / PREM14A and the 14C variants) carry no
  subject/filer split. The target is the company whose holders get the
  Rule 14a-21(c) "golden parachute" advisory vote (Item 402(t) of
  Regulation S-K). The WHOLE proxy document is searched for ``14a-21``,
  ``402(t)`` or ``golden parachute``; present means target, absent means
  not the target.
* **Joint proxies** put both companies' votes in one document, so the
  golden-parachute vote attributes no role. Those are ``undetermined``:
  the screen neither drops nor admits the name.
* Any SEC read failure raises; the screen records the name as
  ``takeover_lookup_failed`` (fail closed), as it already does.

Every fetch goes through the injected ``get`` callable, which in production
is the desk's existing SEC client (same rate limiter, same User-Agent).
Nothing here opens a connection of its own.
"""

from __future__ import annotations

import html
import logging
import re
from dataclasses import dataclass
from typing import Any, Callable

logger = logging.getLogger(__name__)

SEC_ARCHIVES = "https://www.sec.gov/Archives/edgar/data"

#: Role values returned to the screen.
TARGET = "target"
BIDDER = "bidder"
NOT_TARGET = "not_target"
ISSUER_TENDER = "issuer_tender"
UNDETERMINED = "undetermined"
ROLES = frozenset({TARGET, BIDDER, NOT_TARGET, ISSUER_TENDER, UNDETERMINED})

#: Forms the OFFEROR files (Schedule TO family). SC 13E3 is deliberately
#: absent: a Schedule 13E-3 is by definition a going-private transaction
#: (SEC Rule 13e-3), so even an issuer-only header pins the price to the
#: deal and the stock must stay excluded as a pending takeover. EDGAR's form manual: SC TO-I is an issuer tender offer,
#: SC TO-T a third-party one, SC TO-C a written communication relating to
#: an issuer OR third-party tender offer. When the issuer is the sole filer
#: and the subject, it is tendering for its own shares.
OFFEROR_FORMS = frozenset({"SC TO-I", "SC TO-T", "SC TO-C"})

#: Self-filed merger proxies and information statements: no subject/filer
#: split in the header, so the role comes from the golden-parachute vote.
PROXY_FORMS = frozenset({"PREM14A", "DEFM14A", "PREM14C", "DEFM14C"})

_SECTION_RE = re.compile(r"^\s*(SUBJECT COMPANY|FILED BY|FILER)\s*:", re.IGNORECASE)
_CIK_RE = re.compile(r"CENTRAL INDEX KEY:\s*(\d+)", re.IGNORECASE)
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")
# Non-breaking / en / figure hyphens that proxies use inside "14a-21".
_HYPHENS = "‐‑‒–—−"
_VOTE_RE = re.compile(r"14a-21|402\(t\)|golden parachute", re.IGNORECASE)
_JOINT_RE = re.compile(r"joint proxy statement", re.IGNORECASE)


@dataclass(frozen=True)
class FilingRef:
    """What the role reader needs to locate one filing on EDGAR."""

    cik: str
    accession: str
    primary_doc: str
    form: str

    @property
    def folder(self) -> str:
        return f"{int(self.cik)}/{self.accession.replace('-', '')}"

    def locator(self) -> dict[str, str]:
        """JSON-safe form, as the daily cache stores filing rows."""
        return {"cik": self.cik, "accession": self.accession, "primary_doc": self.primary_doc, "form": self.form}

    @classmethod
    def of(cls, ref: Any) -> "FilingRef":
        if isinstance(ref, cls):
            return ref
        if isinstance(ref, dict):
            return cls(
                cik=_norm_cik(ref.get("cik")),
                accession=str(ref.get("accession") or ""),
                primary_doc=str(ref.get("primary_doc") or ""),
                form=str(ref.get("form") or ""),
            )
        raise TypeError(f"not a filing locator: {ref!r}")


def _norm_cik(value: Any) -> str:
    text = re.sub(r"\D", "", str(value or ""))
    return str(int(text)) if text else ""


def parse_header_roles(header_text: str) -> tuple[set[str], set[str]]:
    """(subject CIKs, filer CIKs) from an EDGAR SGML header.

    ``FILED BY`` and ``FILER`` both count as filers; the header itself
    distinguishes a bidder (``FILED BY`` beside a ``SUBJECT COMPANY``) from
    a self-filing (``FILER`` with no subject).
    """
    text = html.unescape(_TAG_RE.sub("", str(header_text or "")))
    subjects: set[str] = set()
    filers: set[str] = set()
    current: set[str] | None = None
    for line in text.splitlines():
        section = _SECTION_RE.match(line)
        if section:
            current = subjects if section.group(1).upper() == "SUBJECT COMPANY" else filers
            continue
        if current is None:
            continue
        found = _CIK_RE.search(line)
        if found:
            current.add(_norm_cik(found.group(1)))
    return subjects, filers


def header_role(header_text: str, cik: Any, form: str = "") -> str:
    """``target`` / ``bidder`` for a header with a subject; ``issuer_tender``
    when this CIK is the subject and sole filer of an offeror-filed form
    (`OFFEROR_FORMS`); ``self`` for a self-filing by this CIK alone;
    ``undetermined`` otherwise."""
    subjects, filers = parse_header_roles(header_text)
    mine = _norm_cik(cik)
    base = str(form or "").strip().upper().removesuffix("/A")
    if mine and mine in subjects:
        if base in OFFEROR_FORMS and filers == {mine}:
            return ISSUER_TENDER
        return TARGET
    if subjects:
        return BIDDER if mine in filers else UNDETERMINED
    if filers == {mine}:
        return "self"
    return UNDETERMINED


def document_text(raw: str | bytes) -> str:
    """Searchable plain text of an EDGAR HTML or text document."""
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="replace")
    text = html.unescape(_TAG_RE.sub(" ", raw))
    for hyphen in _HYPHENS:
        text = text.replace(hyphen, "-")
    return _WS_RE.sub(" ", text.replace("\xa0", " "))


def has_golden_parachute_vote(doc_text: str) -> bool:
    """True when the proxy carries the Rule 14a-21(c) advisory vote."""
    return _VOTE_RE.search(doc_text) is not None


def is_joint_proxy(doc_text: str) -> bool:
    return _JOINT_RE.search(doc_text) is not None


def proxy_role(doc: str | bytes) -> str:
    """Role of a self-filed merger proxy's filer, from the whole document."""
    text = document_text(doc)
    if not has_golden_parachute_vote(text):
        return NOT_TARGET
    if is_joint_proxy(text):
        return UNDETERMINED
    return TARGET


class TakeoverRoleReader:
    """Resolve a takeover filing's role with one or two EDGAR reads.

    ``get(url) -> str | bytes`` is the only way out; it must raise on
    failure. Results are memoised per accession for the reader's life.
    """

    def __init__(self, get: Callable[[str], str | bytes], *, archives_url: str = SEC_ARCHIVES):
        self._get = get
        self._archives = archives_url.rstrip("/")
        self._seen: dict[str, str] = {}

    def header_url(self, ref: FilingRef) -> str:
        return f"{self._archives}/{ref.folder}/{ref.accession}-index-headers.html"

    def document_url(self, ref: FilingRef) -> str:
        return f"{self._archives}/{ref.folder}/{ref.primary_doc}"

    def role(self, ref: FilingRef | dict) -> str:
        ref = FilingRef.of(ref)
        key = f"{ref.cik}:{ref.accession}"  # both sides of one deal share the accession
        if key in self._seen:
            return self._seen[key]
        header = self._get(self.header_url(ref))
        if isinstance(header, bytes):
            header = header.decode("utf-8", errors="replace")
        base = ref.form.strip().upper().removesuffix("/A")
        found = header_role(header, ref.cik, ref.form)
        if found == "self":
            if base in PROXY_FORMS and ref.primary_doc:
                found = proxy_role(self._get(self.document_url(ref)))
            else:
                found = UNDETERMINED
        logger.info("takeover role %s for CIK %s %s %s", found, ref.cik, ref.form, ref.accession)
        self._seen[key] = found
        return found


def screen_readers(sec: Any, deadline: float, listed: dict | None = None) -> tuple[Callable, Callable]:
    """(get_filings, get_role) for `ScreenSources`, on the desk's existing
    SEC client (`SECForm4Provider`: its rate limiter, its configured
    User-Agent). The issuer's submissions JSON gives the rows; the role
    reader then costs one header read for the one open takeover filing,
    plus the document for a self-filed proxy. Every read raises on failure
    and the screen records takeover_lookup_failed. `listed` is the cached
    CIK -> {ticker: exchange} map; read once here when not handed in."""
    if listed is None:
        listed = sec.listed_map(deadline)

    def get_filings(symbol: str):
        wanted = str(symbol or "").strip().upper()
        cik = next((c for c, tickers in listed.items() if wanted in tickers), None)
        if cik is None:
            return None  # no SEC issuer: takeover_status_unknown
        payload = sec._get(f"{sec.submissions_url}/CIK{int(cik):010d}.json", params=None, deadline=deadline).json()
        return filing_rows(payload, cik)

    reader = TakeoverRoleReader(
        lambda url: sec._get(url, params=None, deadline=deadline).text, archives_url=sec.archives_url
    )
    return get_filings, reader.role


def filing_rows(payload: dict, cik: Any) -> list[tuple[str, str, str, dict[str, str]]]:
    """Screen rows ``(form, filing_date, items, locator)`` from a submissions
    JSON payload (``filings.recent``). Mirrors the three-column reader in
    ``src/data/smart_money.py::recent_filings`` plus a JSON-safe locator
    (the daily cache stores rows as JSON)."""
    filings = payload.get("filings", {}) if isinstance(payload, dict) else {}
    recent = filings.get("recent", {}) if isinstance(filings, dict) else {}
    if not isinstance(recent, dict):
        return []
    forms = recent.get("form", []) or []
    dates = recent.get("filingDate", []) or []
    n = len(forms)
    items = (recent.get("items", []) or []) + [""] * n
    accessions = (recent.get("accessionNumber", []) or []) + [""] * n
    docs = (recent.get("primaryDocument", []) or []) + [""] * n
    rows = []
    for i, (form, filed) in enumerate(zip(forms, dates)):
        form_text = str(form or "").strip()
        ref = FilingRef(
            cik=_norm_cik(cik), accession=str(accessions[i] or ""), primary_doc=str(docs[i] or ""), form=form_text
        )
        rows.append((form_text, str(filed or "").strip()[:10], str(items[i] or ""), ref.locator()))
    return rows
