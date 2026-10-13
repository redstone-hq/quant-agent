"""Board items 119 + 187 — the shared macro fetch budget is SHARED.

Lifted wholesale out of `tests/test_macro_feed_resilience.py` when the
fair-share arithmetic became its own service (`src/data/macro_fetch_budget.py`).
These are the END-TO-END reproductions: a real `MacroDataProvider` against a
mocked FRED, proving the policy still holds through the provider. The
service's own construction tests live in `tests/test_macro_fetch_budget.py`
and import no provider at all.
"""

import time
from unittest.mock import MagicMock, patch

import pandas as pd

from src.data.macro import MacroCoverage, MacroDataProvider


def _series(values, start="2026-08-01", freq="B"):
    return pd.Series(values, index=pd.date_range(start, periods=len(values), freq=freq))


# ===========================================================================
# Board items 119 + 187 — the shared budget is shared, not first-come
#
# Measured defect (retained journald 2026-09-01..09-26, `_UID=1001`): 91
# "skipping <series> without an attempt" lines against 12 observation errors
# and 9 metadata timeouts, and the skip count sorted by the series' POSITION
# in CONFIGURED_SERIES (last two 13 times each, first five never). That is a
# first-come-first-served budget starving its own tail, not a provider
# outage. Each series is now reserved an equal share of the ceiling and no
# series, retry or metadata lookup may spend another series' turn.
# ===========================================================================


def _hang_for_the_socket_timeout(_series_id, **_kwargs):
    """Simulate a hung FRED socket: consume exactly the timeout the caller
    set, then fail the way a real read timeout fails. This is what makes the
    test measure BUDGET POLICY rather than mock bookkeeping — a series that
    is allowed 15 s of clock really takes 15 s of clock."""
    import socket as _socket

    timeout = _socket.getdefaulttimeout()
    time.sleep(timeout if timeout else 0.01)
    raise TimeoutError("The read operation timed out")


class _FakeMonotonic:
    """A monotonic clock the module under test consults instead of the wall.

    `src.data.macro` reads exactly two things from `time`: `monotonic()` for
    its deadline arithmetic and `sleep()` for retry backoff. Substituting
    this object for the module's `time` makes the whole budget calculation
    virtual, so the result is arithmetic about deadlines rather than a race
    against a loaded CI runner. Freezing the desk clock does not fit here:
    it pins the CALENDAR instant, and this budget is a monotonic DURATION
    with no calendar in it.
    """

    def __init__(self, start: float = 10_000.0):
        self._t = float(start)

    def monotonic(self) -> float:
        return self._t

    def sleep(self, seconds) -> None:
        self._t += max(0.0, float(seconds))


def _hang_against(clock):
    """The hang helper above, spending the FAKE clock instead of the real one."""

    def _hang(_series_id, **_kwargs):
        import socket as _socket

        timeout = _socket.getdefaulttimeout()
        clock.sleep(timeout if timeout else 0.01)
        raise TimeoutError("The read operation timed out")

    return _hang


@patch("src.data.macro.Fred")
def test_every_required_series_is_attempted_even_when_every_call_hangs(mock_fred_cls):
    """THE reproduction for board item 119. Every configured series must
    reach the wire inside the existing ceiling, even in the worst case the
    budget allows — every single call hanging for its whole allowance.

    Pre-fix this fails: the first series is handed `min(request_timeout_s,
    whole remaining budget)`, hangs for all of it, and the tail of
    CONFIGURED_SERIES is skipped without an attempt. That is exactly the
    production shape, where ICSA and BAMLC0A0CM (the last two) were skipped
    13 times each and the first five were never skipped once.

    Scaled down 60x from the shipped 90 s; the policy under test is a ratio,
    not an absolute. The budget is spent on a virtual monotonic clock, so the
    test costs no real clock at all and cannot be reddened by a loaded runner
    — the thing being asserted is deadline arithmetic, not wall time.
    """
    from src.data.macro import CONFIGURED_SERIES

    clock = _FakeMonotonic()
    mock = MagicMock()
    mock.get_series.side_effect = _hang_against(clock)
    mock.get_series_info.side_effect = _hang_against(clock)
    mock_fred_cls.return_value = mock

    provider = MacroDataProvider(
        api_key="test-key",
        request_timeout_s=1.0,
        max_retries=2,
        retry_backoff_base_s=0.0,
        retry_backoff_max_s=0.0,
        retry_backoff_jitter_s=0.0,
        breaker_after_failed_series=1,
        total_fetch_deadline_s=1.5,
    )
    # The deadline arithmetic lives in the budget service, which takes its
    # clock by value — so the virtual clock is handed to it directly rather
    # than patched into a module it reads at call time.
    provider.budget.monotonic = clock.monotonic
    with patch("src.data.macro.time", clock):
        start = clock.monotonic()
        provider.get_macro_summary()
        spent = clock.monotonic() - start

    asked = {call.args[0] for call in mock.get_series.call_args_list}
    assert asked == set(CONFIGURED_SERIES), (
        "every configured FRED series must be ASKED inside the ceiling; "
        f"never asked: {sorted(set(CONFIGURED_SERIES) - asked)}"
    )
    # And the ceiling still holds — the guarantee is fairness INSIDE the
    # existing budget, not a longer one. This is the budget the code BELIEVES
    # it spent, read off the same clock the code does its deadline arithmetic
    # on, so it is the ceiling itself and not a wall-clock allowance padded
    # for machine load.
    # The epsilon absorbs float accumulation from summing ~30 allowances on
    # the virtual clock (measured 5.5e-12 s of drift); it is arithmetic
    # error, not a tolerance for machine load.
    assert spent <= provider.total_fetch_deadline_s + 1e-6, (
        f"fair-share fetch spent {spent:.6f}s of budget against a {provider.total_fetch_deadline_s}s ceiling"
    )


@patch("src.data.macro.Fred")
def test_one_slow_series_no_longer_starves_the_tail_of_the_list(mock_fred_cls):
    """The production shape in miniature: the FIRST series hangs and every
    later one is healthy. Pre-fix the hang ate the whole budget and the tail
    was never asked; now the hang costs only its own share."""
    from src.data.macro import CONFIGURED_SERIES

    first = CONFIGURED_SERIES[0]

    def _first_hangs(series_id, **_kwargs):
        if series_id == first:
            return _hang_for_the_socket_timeout(series_id)
        return _series([1.0, 2.0])

    mock = MagicMock()
    mock.get_series.side_effect = _first_hangs
    mock.get_series_info.return_value = None
    mock_fred_cls.return_value = mock

    provider = MacroDataProvider(
        api_key="test-key",
        request_timeout_s=1.0,
        max_retries=2,
        retry_backoff_base_s=0.0,
        retry_backoff_max_s=0.0,
        retry_backoff_jitter_s=0.0,
        breaker_after_failed_series=1,
        total_fetch_deadline_s=1.5,
    )
    provider.get_macro_summary()

    coverage = provider.last_coverage
    assert coverage.succeeded == len(CONFIGURED_SERIES) - 1, coverage.describe()
    assert [f.series_id for f in coverage.failed] == [first]
    assert coverage.not_attempted == [], "no series may be left un-attempted when only one series is slow"


@patch("src.data.macro.Fred")
def test_metadata_never_spends_an_observation_it_cannot_replace(mock_fred_cls):
    """A `/fred/series` metadata lookup is a freshness LABEL. In three
    retained production runs (2026-09-17, 09-19, 09-22) the last log line
    before the skip cascade was a metadata call timing out for 15 s — a
    label spending the observations the seat is made of. Metadata may now
    only draw on the surplus above the waiting series' reserves, so a
    metadata storm costs freshness detail and never coverage."""
    from src.data.macro import CONFIGURED_SERIES

    mock = MagicMock()
    mock.get_series.side_effect = lambda sid, **kw: _series([1.0, 2.0])
    mock.get_series_info.side_effect = _hang_for_the_socket_timeout
    mock_fred_cls.return_value = mock

    provider = MacroDataProvider(
        api_key="test-key",
        request_timeout_s=1.0,
        max_retries=2,
        retry_backoff_base_s=0.0,
        retry_backoff_max_s=0.0,
        retry_backoff_jitter_s=0.0,
        breaker_after_failed_series=1,
        total_fetch_deadline_s=1.5,
    )
    provider.get_macro_summary()

    coverage = provider.last_coverage
    assert coverage.status == "ok", "hanging METADATA must not cost a single observation: " + coverage.describe()
    assert coverage.succeeded == len(CONFIGURED_SERIES)


@patch("src.data.macro.Fred")
def test_an_unattempted_series_is_named_as_unattempted_not_merely_failed(mock_fred_cls):
    """Board item 119's visibility half. A series that never reached FRED
    must say so IN THOSE WORDS, separately from one that was asked and
    failed — and nothing may stand in for it."""
    from src.data.macro import NOT_ATTEMPTED_REASON

    mock = MagicMock()
    mock_fred_cls.return_value = mock

    provider = MacroDataProvider(api_key="test-key")
    provider.budget.deadline = time.monotonic() - 1.0  # ceiling already exhausted

    result = provider.get_vix()

    assert result["current"] is None
    mock.get_series.assert_not_called()

    failure = provider._run_failed[-1]
    assert failure.series_id == "VIXCLS"
    assert failure.reason == NOT_ATTEMPTED_REASON
    assert failure.reason != "fetch_deadline_exceeded", "never-asked and asked-then-timed-out are different defects"

    coverage = MacroCoverage(
        configured=15,
        succeeded=14,
        failed=[failure],
    )
    assert [f.series_id for f in coverage.not_attempted] == ["VIXCLS"]
    described = coverage.describe()
    assert "NOT ATTEMPTED AT ALL: VIXCLS" in described
    assert "no request" in described


@patch("src.data.macro.Fred")
def test_an_unattempted_series_carries_no_value_stale_or_otherwise(mock_fred_cls):
    """Nothing stale may read as fresh. An un-attempted series returns an
    EMPTY series and is recorded EMPTY with a reason that names the cause —
    it is never back-filled from the previous run, the cache, or a
    neighbouring series."""
    from src.data.macro import FRESHNESS_EMPTY

    mock = MagicMock()
    mock.get_series.side_effect = lambda sid, **kw: _series([11.0, 12.0])
    mock_fred_cls.return_value = mock

    provider = MacroDataProvider(api_key="test-key")
    # A real, fresh reading lands first.
    good = provider.get_vix()
    assert good["current"] == 12.0

    # Then the ceiling is exhausted before the next turn.
    provider.budget.deadline = time.monotonic() - 1.0
    provider._series_info_cache = {}
    starved = provider.get_vix()

    assert starved["current"] is None, "a starved series must not reuse a prior value"
    freshness = provider._run_freshness["VIXCLS"]
    assert freshness.status == FRESHNESS_EMPTY
    assert "not attempted" in freshness.detail


@patch("src.data.macro.Fred")
def test_the_per_series_reserve_is_derived_not_chosen(mock_fred_cls):
    """The reservation is arithmetic over two numbers that already exist —
    the ceiling in force and the length of CONFIGURED_SERIES — not a new
    constant anybody picked. Pinned mechanically so adding a sixteenth
    series re-derives the share instead of silently under-reserving."""
    from src.data.macro import CONFIGURED_SERIES

    mock_fred_cls.return_value = MagicMock()
    provider = MacroDataProvider(api_key="test-key", total_fetch_deadline_s=90.0)

    assert provider.budget.per_series_reserve_s == 90.0 / len(CONFIGURED_SERIES)
    # Tight by construction: every series spending its whole share is
    # exactly the ceiling, which is what makes "all fifteen are attempted"
    # true in the worst case rather than merely likely.
    assert provider.budget.per_series_reserve_s * len(CONFIGURED_SERIES) == provider.total_fetch_deadline_s

    wider = MacroDataProvider(api_key="test-key", total_fetch_deadline_s=180.0)
    assert wider.budget.per_series_reserve_s == 2 * provider.budget.per_series_reserve_s
