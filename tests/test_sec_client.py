"""SecClient constructed from plain collaborators: fake opener, sleep and clock.

Never imports `src.data.earnings`; no network, no real sleeping.
"""

from urllib.error import HTTPError, URLError
from urllib.request import Request

import pytest

from src.data.sec_client import SecClient, build_sec_client


def _client(opener, *, sleep=lambda _s: None, clock=None):
    kwargs = {"opener": opener, "sleep": sleep}
    if clock is not None:
        kwargs["clock"] = clock
    return build_sec_client(request=Request, http_error=HTTPError, url_error=URLError, **kwargs)


def test_collaborators_are_the_objects_handed_in():
    def opener(req, timeout):
        raise AssertionError("not called")

    def sleep(_s):
        pass

    def clock():
        return 0.0

    def now():
        return None

    client = SecClient(
        opener=opener,
        request=Request,
        http_error=HTTPError,
        url_error=URLError,
        sleep=sleep,
        clock=clock,
        now=now,
    )
    assert client._opener is opener
    assert client._sleep is sleep
    assert client._clock is clock
    assert client._now is now


def test_every_request_is_preceded_by_the_rate_limit_delay():
    from src.data.sec_client import REQUEST_DELAY

    seen = []

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b"{}"

    def opener(req, timeout):
        seen.append(("open", req.get_header("User-agent")))
        return _Resp()

    client = _client(opener, sleep=lambda s: seen.append(("sleep", s)))
    client.get("https://data.sec.gov/a")
    client.get("https://data.sec.gov/b")
    assert [e[0] for e in seen] == ["sleep", "open", "sleep", "open"]
    assert seen[0] == ("sleep", REQUEST_DELAY)


def test_cik_map_is_fetched_once_and_cached_by_the_client():
    import json

    calls = []

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return json.dumps({"0": {"ticker": "nvda", "cik_str": 1045810}}).encode()

    def opener(req, timeout):
        calls.append(req.full_url)
        return _Resp()

    client = _client(opener)
    assert client.cik_for("NVDA") == "1045810"
    assert client.cik_for("nvda") == "1045810"
    assert client.cik_for("ZZZ") is None
    assert len(calls) == 1


def test_sec_get_retries_on_429():
    from urllib.error import HTTPError
    from io import BytesIO

    call_log: list[str] = []

    def fake_urlopen(req, timeout):
        call_log.append("call")
        if len(call_log) < 3:
            raise HTTPError(req.full_url, 429, "Too Many Requests", {}, BytesIO(b""))

        # 3rd attempt succeeds
        class _Resp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return b'{"ok": true}'

        return _Resp()

    client = _client(fake_urlopen)
    out = client.get("https://data.sec.gov/x")
    assert out == b'{"ok": true}'
    assert len(call_log) == 3


def test_sec_get_retries_on_503():
    from urllib.error import HTTPError
    from io import BytesIO

    call_log: list[str] = []

    def fake_urlopen(req, timeout):
        call_log.append("call")
        if len(call_log) < 2:
            raise HTTPError(req.full_url, 503, "Service Unavailable", {}, BytesIO(b""))

        class _Resp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return b'{"ok": 1}'

        return _Resp()

    client = _client(fake_urlopen)
    out = client.get("https://data.sec.gov/x")
    assert out == b'{"ok": 1}'
    assert len(call_log) == 2


def test_sec_get_raises_after_max_retries():
    """All retries exhausted → surface the HTTPError so caller's broad
    except still logs it (rather than silently swallowing data loss)."""
    from urllib.error import HTTPError
    from io import BytesIO
    import pytest

    def fake_urlopen(req, timeout):
        raise HTTPError(req.full_url, 429, "Too Many Requests", {}, BytesIO(b""))

    client = _client(fake_urlopen)
    with pytest.raises(HTTPError):
        client.get("https://data.sec.gov/x", max_retries=2)


def test_sec_get_aborts_when_total_timeout_exceeded():
    """`SecClient.get` must abort the retry loop when total_timeout_s elapses
    even if max_retries hasn't fired. Prevents a sustained SEC 503 from
    snowballing one URL into 60+ seconds — for the 77-stock earnings
    preprocess that scales to minutes of wasted budget, pushing into the
    morning window. The check fires at the START of each iteration so a
    single in-flight urlopen + sleep already consumed counts against
    next attempt's budget.
    """
    from urllib.error import HTTPError
    from io import BytesIO
    import pytest

    # Fake wallclock so the budget check fires without real waiting.
    fake_clock = {"t": 0.0}

    call_log: list[str] = []

    def fake_urlopen(req, timeout):
        call_log.append("call")
        raise HTTPError(req.full_url, 503, "overloaded", {}, BytesIO(b""))

    client = _client(
        fake_urlopen,
        sleep=lambda s: fake_clock.__setitem__("t", fake_clock["t"] + s),
        clock=lambda: fake_clock["t"],
    )
    # total_timeout_s=2.0: attempt 1 sleeps REQUEST_DELAY≈0.12 + 1s backoff → t≈1.12.
    # Attempt 2 budget check: 1.12 < 2.0, continue, +0.12 + 2s backoff → t≈3.24.
    # Attempt 3 budget check: 3.24 > 2.0 → ABORT, raise the last HTTPError.
    # Total: 2 urlopen calls (not 3 = max_retries).
    with pytest.raises(HTTPError):
        client.get(
            "https://data.sec.gov/x",
            max_retries=3,
            total_timeout_s=2.0,
        )
    assert len(call_log) == 2, f"expected exactly 2 attempts (3rd aborted by total_timeout_s); got {len(call_log)}"
    assert fake_clock["t"] > 2.0, "fake wallclock should have ticked past the budget"


def test_get_recent_filings_tolerates_misaligned_arrays():
    """SEC submissions JSON returns parallel arrays. An upstream
    truncation could leave them desynced. Pin: zip-based iteration
    survives without IndexError when accessions / primary_docs are
    shorter than forms / dates."""
    import json as _json

    # forms=4, dates=4, accessions=2, primary_docs=2 — short tail
    payload = {
        "filings": {
            "recent": {
                "form": ["10-Q", "10-K", "8-K", "10-Q"],
                "filingDate": ["2026-04-30", "2026-04-15", "2026-04-10", "2026-03-25"],
                "accessionNumber": ["0001-23-001", "0001-23-002"],
                "primaryDocument": ["nvda-10q.html", "nvda-10k.html"],
            }
        }
    }

    def fake_urlopen(req, timeout):
        class _Resp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return _json.dumps(payload).encode()

        return _Resp()

    client = _client(fake_urlopen)
    filings = client.recent_filings("0001234567", "NVDA")
    # zip stops at shortest: only first 2 rows yield filings, of which only
    # 1 is a 10-Q/10-K (the 10-K at idx 1).
    forms_seen = [f.form_type for f in filings]
    assert "10-Q" in forms_seen
    assert "10-K" in forms_seen
    assert len(filings) == 2  # idx 0 (10-Q) + idx 1 (10-K)


def test_sec_get_does_not_retry_on_404():
    """404 means the URL is wrong (bad CIK / missing filing), not a
    transient rate-limit. Retrying wastes the budget — surface immediately."""
    from urllib.error import HTTPError
    from io import BytesIO
    import pytest

    call_log: list[str] = []

    def fake_urlopen(req, timeout):
        call_log.append("call")
        raise HTTPError(req.full_url, 404, "Not Found", {}, BytesIO(b""))

    client = _client(fake_urlopen)
    with pytest.raises(HTTPError):
        client.get("https://data.sec.gov/x")
    assert len(call_log) == 1  # no retry on 404
