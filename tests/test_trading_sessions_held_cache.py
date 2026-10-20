"""`trading_sessions_held` memoizes successful calendar answers per (start, end).

Measured on the live desk 20 Oct: the insider-filing age gate asked the same
(start, end) pair thousands of times, each one a calendar network call and a
reconciliation row (5,466 calls vs 55 the day before; 11.6 min against a
20-min kill). A successful answer is fixed by the exchange calendar, so it is
asked of the broker once; a failed answer is never cached so an outage is
retried on the next call.
"""

from datetime import date

import pytest

from src.execution.broker_parts.account_reads import AccountReads


class _CountingClient:
    def __init__(self, sessions=3, fail=False):
        self.calls = 0
        self.sessions = sessions
        self.fail = fail

    def get_calendar(self, request):
        self.calls += 1
        if self.fail:
            raise RuntimeError("calendar outage")
        return [object()] * self.sessions


def _reads(client, cache=None):
    kwargs = {} if cache is None else {"sessions_held_cache": cache}
    return AccountReads(
        client=client,
        shortable_cache={},
        fractionable_cache={},
        trading_day_cache={},
        session_open_cache={},
        **kwargs,
    )


START, END = date(2026, 10, 1), date(2026, 10, 20)


def test_same_pair_twice_hits_client_once_with_same_answer():
    client = _CountingClient(sessions=3)
    reads = _reads(client)
    first = reads.trading_sessions_held(START, END)
    second = reads.trading_sessions_held(START, END)
    assert (first, second) == (3, 3)
    assert client.calls == 1


def test_different_pair_makes_a_new_call():
    client = _CountingClient(sessions=3)
    reads = _reads(client)
    reads.trading_sessions_held(START, END)
    reads.trading_sessions_held(date(2026, 10, 2), END)
    assert client.calls == 2


def test_failure_path_is_not_cached():
    client = _CountingClient(fail=True)
    reads = _reads(client)
    # Fallback is the weekday count; the next call must ask the client again.
    assert reads.trading_sessions_held(START, END) > 0
    assert reads.trading_sessions_held(START, END) > 0
    assert client.calls == 2


def test_broker_owned_cache_survives_per_call_instances():
    # `AlpacaBroker._account_reads` builds a fresh AccountReads per call; the
    # memo only helps the live desk if it lives in a dict the broker keeps.
    client = _CountingClient(sessions=3)
    shared: dict = {}
    assert _reads(client, shared).trading_sessions_held(START, END) == 3
    assert _reads(client, shared).trading_sessions_held(START, END) == 3
    assert client.calls == 1


def test_no_guarded_pass_row_on_cache_hit(monkeypatch):
    import src.execution.broker_parts.account_reads as mod

    rows = []
    monkeypatch.setattr(mod, "record_guarded_pass", lambda *a, **k: rows.append(a))
    reads = _reads(_CountingClient(sessions=3))
    reads.trading_sessions_held(START, END)
    reads.trading_sessions_held(START, END)
    assert len(rows) == 1


def test_end_not_after_start_is_zero_without_a_call():
    client = _CountingClient()
    assert _reads(client).trading_sessions_held(END, START) == 0
    assert client.calls == 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__]))
