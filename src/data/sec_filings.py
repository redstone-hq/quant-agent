"""Who is the target in an SEC takeover filing: bidder, target, or unknown.

The universe screen drops a name whose latest takeover filing is still
open (`src/universe_screen.py::pending_takeover`). The filing's form alone
cannot say which side filed it: a bidder files SC TO-T about its target,
and both sides of a merger file DEFM14A. This module reads the role.

Role rules (reviewed design, 2026-10-12):

* **Header roles.** EDGAR's SGML header names the target under
  ``SUBJECT COMPANY`` and the bidder under ``FILED BY``. A CIK that is the
  subject is the target; one that appears only as the filer is the bidder.
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

#: Contact string SEC requires on every request. Used only by the
#: production reader; tests never touch the network.
TAKEOVER_USER_AGENT = "QAMC research qamc-research@example.invalid"

#: Role values returned to the screen.
TARGET = "target"
BIDDER = "bidder"
NOT_TARGET = "not_target"
UNDETERMINED = "undetermined"
ROLES = frozenset({TARGET, BIDDER, NOT_TARGET, UNDETERMINED})

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


def header_role(header_text: str, cik: Any) -> str:
    """``target`` / ``bidder`` for a header with a subject; ``self`` for a
    self-filing by this CIK alone; ``undetermined`` otherwise."""
    subjects, filers = parse_header_roles(header_text)
    mine = _norm_cik(cik)
    if mine and mine in subjects:
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

    def role(self, ref: FilingRef) -> str:
        key = ref.accession
        if key in self._seen:
            return self._seen[key]
        header = self._get(self.header_url(ref))
        if isinstance(header, bytes):
            header = header.decode("utf-8", errors="replace")
        found = header_role(header, ref.cik)
        if found == "self":
            base = ref.form.strip().upper().removesuffix("/A")
            if base in PROXY_FORMS and ref.primary_doc:
                found = proxy_role(self._get(self.document_url(ref)))
            else:
                found = UNDETERMINED
        logger.info("takeover role %s for CIK %s %s %s", found, ref.cik, ref.form, ref.accession)
        self._seen[key] = found
        return found


def filing_rows(payload: dict, cik: Any) -> list[tuple[str, str, str, FilingRef]]:
    """Screen rows ``(form, filing_date, items, ref)`` from a submissions
    JSON payload (``filings.recent``). Mirrors the three-column reader in
    ``src/data/smart_money.py::recent_filings`` plus the locator."""
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
        rows.append((form_text, str(filed or "").strip()[:10], str(items[i] or ""), ref))
    return rows
