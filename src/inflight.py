"""What is being built RIGHT NOW, read off GitHub — never typed in by hand.

Why this exists
---------------
The owner's words: "I'm kind of clueless unless you tell me stuff, everything
is in the background." The status board shows what is decided and what needs
him. It did not show what is actually under construction, so the only way he
learned that was by asking — the exact gap the board exists to close.

The honest source of truth for "in flight" is the repository itself: an OPEN
PULL REQUEST is work that exists, is not merged, and is not yet real. A
hand-maintained list would rot the way every other hand-maintained status
document here has rotted, so this reads the open pull requests from GitHub
and derives everything it shows from that.

How it reads GitHub, and why not `gh`
-------------------------------------
The repository is public. GitHub's REST API answers read-only requests for a
public repository with NO credential at all, so this uses plain HTTPS from
the standard library: no `gh` login, no token on the account that trades
(putting a GitHub credential on that account is an owner decision, not a
convenience for a status page — see the `pr_merged` rule in
`scripts/status_board.py`, which says the same). A token IS honoured if one
is present in the environment (`GH_TOKEN` / `GITHUB_TOKEN`), which only
raises the rate limit; nothing here needs it.

Unauthenticated reads are rate-limited per address, so every request is
CONDITIONAL: the last ETag GitHub returned is sent back, and a "304 Not
Modified" answer — which GitHub does not count against the limit — reuses
the body already held. In steady state, re-reading an unchanged set of pull
requests costs nothing.

How it degrades
---------------
Loudly, and never into an empty list. "Could not read what is in flight" and
"nothing is in flight" are different facts and the page must never confuse
them: a silent empty list that really means "I could not look" is the
failure class this desk keeps finding. So the result carries either the
items it read or a plain-English reason it could not read them, and the
renderer says which. A partially readable pull request (the list came back
but its own status did not) is still listed, with its status reported as
unreadable rather than guessed.

Who reads the output
--------------------
The owner, on a phone. So: no branch names, no commit hashes, no CI jargon.
Each item is the pull request's title — written by the engineering side, in
plain words — plus, in words, whether it is still being written, being
tested, waiting to be merged, or blocked. The pull request NUMBER is kept:
it is the one handle both sides already use to refer to a change.
"""

from __future__ import annotations

import html
import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from src.trading_calendar import utc_now
from typing import Any, Callable
from zoneinfo import ZoneInfo
from src.time_format import fmt_time_12h

REPO = "redstone-hq/quant-agent"
API = "https://api.github.com"
ET = ZoneInfo("America/New_York")

#: Seconds allowed per HTTP call. The board page is served synchronously,
#: so a hanging GitHub must fail fast rather than hold the page; the value
#: matches the timeout the board server already uses for its own live probe.
_TIMEOUT_S = 5

#: (status, headers, body) — the shape the injectable `fetch` returns, so
#: tests can hand in canned answers without a network.
Response = tuple[int, dict[str, str], bytes]
Fetch = Callable[[str, dict[str, str]], Response]

#: url -> (etag, decoded body). Module-level on purpose: the board server is
#: one long-lived process, and this is what makes repeated reads free.
_etag_cache: dict[str, tuple[str, Any]] = {}


# --------------------------------------------------------------------------
# the data
# --------------------------------------------------------------------------


@dataclass
class InFlightItem:
    number: int
    title: str
    opened_at: datetime
    updated_at: datetime
    draft: bool = False
    #: GitHub's own summary of whether the change can go in: "clean",
    #: "dirty" (conflict), "blocked" (a required check or review is
    #: outstanding), "behind", "unstable", "unknown" (still being worked
    #: out). None when the per-item read failed — reported, never guessed.
    mergeable_state: str | None = None
    #: (name, status, conclusion) per check run. None when unreadable; an
    #: empty tuple means "read fine, no checks have run".
    checks: tuple[tuple[str, str, str | None], ...] | None = None
    #: Plain-English reason the item's own status could not be read.
    detail_problem: str | None = None

    @property
    def stage(self) -> str:
        """Where this change is, in words a trader can act on.

        Derived from what GitHub reports, in the order a reader cares
        about: a draft is not ready regardless of its tests; running tests
        outrank finished ones; a failure outranks a conflict, because the
        failure is what somebody is fixing first.
        """
        if self.draft:
            return "still being written; not ready to go in yet"
        if self.checks is None and self.mergeable_state is None:
            return "its status could not be read just now"
        if self.checks is not None:
            if any(status != "completed" for _n, status, _c in self.checks):
                return "being tested now"
            if any(conc in ("failure", "timed_out", "cancelled", "action_required") for _n, _s, conc in self.checks):
                return "tests failed; being fixed before it can go in"
        state = self.mergeable_state
        if state == "dirty":
            return "blocked: it clashes with work already merged and needs untangling first"
        if self.checks is not None and not self.checks:
            return "no tests have run on it yet"
        if state == "clean":
            return "tests passed; ready to go in, waiting to be merged"
        if state == "behind":
            return "tests passed; waiting to be merged, needs the latest changes pulled in first"
        if state == "blocked":
            return "tests passed; waiting on a review before it can go in"
        if state == "unstable":
            return "some tests failed; being looked at"
        if state is None and self.checks is not None:
            return "tests passed; whether it can go in could not be read"
        return "its status is still being worked out"


@dataclass
class OpenPR:
    """One open pull request, read for what it CLAIMS rather than for CI.

    `read_in_flight` above answers "what is being built"; this answers "what
    does this change say about itself" — the title, the description, and the
    files it touches — which is what a rule about review evidence has to look
    at. Same reader, same credential-free path, one module.

    `files` and `work_md_patch` are None when the follow-up read failed. That
    is deliberately distinct from "touches no files": a caller enforcing a
    rule must never treat a failed read as proof of a violation.
    """

    number: int
    title: str
    body: str
    files: tuple[str, ...] | None = None
    #: The unified diff of docs/WORK.md in this change, when it touches it.
    work_md_patch: str | None = None
    detail_problem: str | None = None


@dataclass
class InFlight:
    """Everything the page needs, plus an honest account of how it was got.

    Exactly one of these is true: `problem` is None and `items` is what
    GitHub reported at `read_at`; or `problem` says why nothing could be
    read, and `items` is meaningless (and rendered as such).
    """

    items: list[InFlightItem] = field(default_factory=list)
    read_at: datetime | None = None
    problem: str | None = None

    @property
    def readable(self) -> bool:
        return self.problem is None


#: What `render_in_flight` shows when nobody tried to read GitHub at all —
#: an offline preview build, or a test. Distinct from a failed attempt.
NOT_ATTEMPTED = InFlight(problem="this page was built without asking GitHub")


# --------------------------------------------------------------------------
# reading GitHub
# --------------------------------------------------------------------------


def _http_get(url: str, headers: dict[str, str]) -> Response:
    """One conditional GET. Never raises for an HTTP error status: the
    status code is returned and the caller decides what it means."""
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT_S) as resp:
            return resp.status, {k.lower(): v for k, v in resp.headers.items()}, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, {k.lower(): v for k, v in exc.headers.items()}, exc.read()


def _get_json(url: str, fetch: Fetch) -> Any:
    """GET `url` as JSON through the ETag cache. Raises RuntimeError with a
    plain-English message on anything that is not a usable answer."""
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "quant-agent-status-board",
    }
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    cached = _etag_cache.get(url)
    if cached:
        headers["If-None-Match"] = cached[0]
    try:
        status, resp_headers, body = fetch(url, headers)
    except Exception as exc:  # noqa: BLE001 - a sentence, never a stack trace
        raise RuntimeError(f"GitHub could not be reached ({type(exc).__name__})") from exc
    if status == 304 and cached:
        return cached[1]
    if status == 403 or status == 429:
        raise RuntimeError("GitHub refused the read: the request limit for this machine is used up for now")
    if status != 200:
        raise RuntimeError(f"GitHub answered with status {status}")
    try:
        data = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise RuntimeError("GitHub's answer could not be decoded") from exc
    etag = resp_headers.get("etag")
    if etag:
        _etag_cache[url] = (etag, data)
    return data


def _when(iso: str | None) -> datetime:
    if not iso:
        return datetime.fromtimestamp(0, tz=timezone.utc)
    return datetime.fromisoformat(iso.replace("Z", "+00:00"))


def read_in_flight(fetch: Fetch = _http_get, repo: str = REPO, now: datetime | None = None) -> InFlight:
    """Read every open pull request and, for each, whether it can go in and
    how its tests stand. Never raises.

    One request lists the open pull requests. Two more per item read its
    mergeability (the list endpoint does not carry it) and its check runs.
    If the LIST cannot be read, nothing is known and `problem` says so. If
    only an item's detail cannot be read, the item is still listed with its
    status marked unreadable — a change that exists must not vanish because
    one follow-up call failed.
    """
    read_at = now or utc_now()
    try:
        raw = _get_json(f"{API}/repos/{repo}/pulls?state=open&per_page=100", fetch)
    except RuntimeError as exc:
        return InFlight(problem=str(exc))
    if not isinstance(raw, list):
        return InFlight(problem="GitHub's answer was not a list of changes")

    items: list[InFlightItem] = []
    for p in raw:
        try:
            number = int(p["number"])
            item = InFlightItem(
                number=number,
                title=str(p.get("title") or f"change {number}"),
                opened_at=_when(p.get("created_at")),
                updated_at=_when(p.get("updated_at")),
                draft=bool(p.get("draft")),
            )
        except (KeyError, TypeError, ValueError):
            continue
        head_sha = ((p.get("head") or {}).get("sha")) or ""
        try:
            detail = _get_json(f"{API}/repos/{repo}/pulls/{number}", fetch)
            state = detail.get("mergeable_state") if isinstance(detail, dict) else None
            # "unknown" is GitHub's own "not worked out yet" and is kept as
            # the string, distinct from None, which means WE could not read it.
            item.mergeable_state = str(state) if state else None
            if head_sha:
                runs = _get_json(f"{API}/repos/{repo}/commits/{head_sha}/check-runs", fetch)
                item.checks = (
                    tuple(
                        (str(r.get("name", "")), str(r.get("status", "")), r.get("conclusion"))
                        for r in (runs.get("check_runs") or [])
                    )
                    if isinstance(runs, dict)
                    else None
                )
            else:
                item.checks = None
        except RuntimeError as exc:
            item.detail_problem = str(exc)
        items.append(item)
    items.sort(key=lambda i: i.updated_at, reverse=True)
    return InFlight(items=items, read_at=read_at)


#: docs/WORK.md is the backlog. A change that edits this line is a change
#: that closes a board item, whatever its description says.
WORK_MD = "docs/WORK.md"
RETIRED_LINE = "Retired item numbers"


def read_open_pull_requests(
    fetch: Fetch = _http_get,
    repo: str = REPO,
) -> tuple[list[OpenPR], str | None]:
    """Every open pull request with its description and the files it touches.

    Returns `(items, problem)`. Exactly one is meaningful: a problem string
    means GitHub could not be read and the list says nothing — the caller
    must not read an empty list as "no pull requests". Nothing here raises.
    """
    try:
        raw = _get_json(f"{API}/repos/{repo}/pulls?state=open&per_page=100", fetch)
    except RuntimeError as exc:
        return [], str(exc)
    if not isinstance(raw, list):
        return [], "GitHub's answer was not a list of changes"

    out: list[OpenPR] = []
    for p in raw:
        if not isinstance(p, dict):
            continue
        try:
            number = int(p["number"])
        except (KeyError, TypeError, ValueError):
            continue
        item = OpenPR(number=number, title=str(p.get("title") or ""), body=str(p.get("body") or ""))
        try:
            files = _get_json(f"{API}/repos/{repo}/pulls/{number}/files?per_page=100", fetch)
        except RuntimeError as exc:
            item.detail_problem = str(exc)
            out.append(item)
            continue
        if isinstance(files, list):
            names: list[str] = []
            for f in files:
                if not isinstance(f, dict):
                    continue
                name = str(f.get("filename") or "")
                names.append(name)
                if name == WORK_MD:
                    item.work_md_patch = str(f.get("patch") or "")
            item.files = tuple(names)
        else:
            item.detail_problem = "GitHub's answer was not a list of files"
        out.append(item)
    return out, None


# --------------------------------------------------------------------------
# rendering
# --------------------------------------------------------------------------

#: The rendered section is fenced so the board SERVER can replace it with a
#: fresh read at request time without touching the rest of the page — see
#: `refresh_in_flight_html` and src/api/server.py.
START_MARK = "<!-- in-flight:start -->"
END_MARK = "<!-- in-flight:end -->"


def _esc(s: Any) -> str:
    return html.escape(str(s), quote=True)


def _stamp(t: datetime) -> str:
    return t.astimezone(ET).strftime("%A %-d %B %Y &middot; %H:%M ET")


def _opened(t: datetime, now: datetime) -> str:
    """ "opened 2026-10-04 1:05 PM ET". Every timestamp carries a date
    so the owner can scroll back hours later and know when something was
    opened (item 231)."""
    local = t.astimezone(ET)
    return f"opened {fmt_time_12h(local)}"


def render_in_flight(inf: InFlight, now: datetime | None = None, last_good: InFlight | None = None) -> str:
    """The section body: what is under construction, or an explicit account
    of why that could not be read. Wrapped in `START_MARK`/`END_MARK`.

    `last_good`, when given alongside a failed read, is the most recent
    SUCCESSFUL read: its items are shown, dated, under the failure notice —
    an old list with its time on it beats no list, and it beats an empty
    one that reads as "nothing in flight".
    """
    now = now or utc_now()
    parts: list[str] = [START_MARK]
    if not inf.readable:
        parts.append(
            '<div class="note inflight-unread"><b>Could not read what is in '
            f"flight.</b> {_esc(inf.problem)}. This does not mean nothing is "
            "in flight &mdash; it means the page could not look.</div>"
        )
        if last_good is not None and last_good.readable and last_good.read_at:
            parts.append(f'<p class="lede">The last successful read, at {_stamp(last_good.read_at)}, showed:</p>')
            parts.append(_rows(last_good.items, now))
    else:
        assert inf.read_at is not None
        parts.append(
            f'<p class="lede inflight-stamp">Read from GitHub at '
            f"{_stamp(inf.read_at)}. Anything that changed after that is not "
            "here yet.</p>"
        )
        if not inf.items:
            parts.append('<div class="note">Nothing is in flight: no change is open and unmerged right now.</div>')
        else:
            parts.append(_rows(inf.items, now))
    parts.append(END_MARK)
    return "\n".join(parts)


def _rows(items: list[InFlightItem], now: datetime) -> str:
    rows = []
    for it in items:
        tail = it.stage
        if it.detail_problem:
            tail = f"{tail} ({_esc(it.detail_problem)})"
        rows.append(
            '<div class="ol ol-inhand ol-inflight">'
            f'<span class="q-n">PR {it.number}</span>'
            f"<span>{_esc(it.title)} "
            f"<em>&mdash; {tail}; {_opened(it.opened_at, now)}.</em>"
            "</span></div>"
        )
    return "\n".join(rows)


def refresh_in_flight_html(page: str, fresh: str) -> str:
    """Swap the fenced section in a rendered page for `fresh`. A page with
    no fence (an older build) is returned untouched."""
    start = page.find(START_MARK)
    end = page.find(END_MARK, start)
    if start == -1 or end == -1:
        return page
    return page[:start] + fresh + page[end + len(END_MARK) :]
