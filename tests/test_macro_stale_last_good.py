"""Board item 187 — after retries are exhausted the desk acts: it serves the
last-good cached series with its age, and says so in coverage. No live calls."""

from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from src.data.macro import MacroDataProvider
from src.data.macro_series_cache import MacroSeriesCache
from src.data.fetch_coverage_record import build_row
from src.trading_calendar import ET

# A fixed ordinary Wednesday: an entry 3-5 days old then always crosses a
# publication boundary, so it is never "still current". Seeding off the real
# clock failed on Columbus Day 2026 after 16:15 ET: Friday 16:30 to Monday
# 16:30 crosses no boundary (no H.15 on the bond-market holiday), so the cache
# was served as current and the failure path never ran.
_NOW = datetime(2026, 9, 16, 12, 0, tzinfo=ET)


@pytest.fixture(autouse=True)
def _fixed_clock(monkeypatch):
    monkeypatch.setattr("src.data.macro.et_now", lambda: _NOW)


def _seed(cache, sid, kwargs, days_old):
    cache.save(sid, kwargs, [("2026-09-01", 1.0), ("2026-09-02", 2.0)], None, None, _NOW - timedelta(days=days_old))


def _provider(tmp_path, fred_cls, side_effect):
    fred = MagicMock()
    fred.get_series.side_effect = side_effect
    fred_cls.return_value = fred
    cache = MacroSeriesCache(str(tmp_path))
    return MacroDataProvider(api_key="k", max_retries=2, series_cache=cache), fred, cache


@patch("src.data.macro.time.sleep")
@patch("src.data.macro.Fred")
def test_deadline_path_retries_then_serves_last_good_with_age(fred_cls, _s, tmp_path):
    p, fred, cache = _provider(tmp_path, fred_cls, TimeoutError("fetch_deadline_exceeded"))
    _seed(cache, "VIXCLS", {}, 5)
    out = p._safe_get_series("VIXCLS")
    assert fred.get_series.call_count == 3  # retried before falling back
    assert list(out) == [1.0, 2.0]  # fallback taken
    cov = p._run_failed[0]
    assert cov.series_id == "VIXCLS"
    assert "stale_last_good_served_age_5d" in cov.reason


@patch("src.data.macro.time.sleep")
@patch("src.data.macro.Fred")
def test_no_last_good_stays_a_named_failure(fred_cls, _s, tmp_path):
    p, _f, _c = _provider(tmp_path, fred_cls, TimeoutError("fetch_deadline_exceeded"))
    out = p._safe_get_series("VIXCLS")
    assert len(out) == 0
    assert p._run_failed[0].reason == "fetch_deadline_exceeded"


@patch("src.data.macro.time.sleep")
@patch("src.data.macro.Fred")
def test_incomplete_set_is_visible_to_consumers_and_counted(fred_cls, _s, tmp_path):
    p, _f, cache = _provider(tmp_path, fred_cls, TimeoutError("fetch_deadline_exceeded"))
    _seed(cache, "VIXCLS", {}, 3)
    p._safe_get_series("VIXCLS")
    from src.data.macro import MacroCoverage

    cov = MacroCoverage(configured=p._run_configured, succeeded=p._run_succeeded, failed=list(p._run_failed))
    assert not cov.complete and cov.status == "failed"
    assert "3d old" in cov.describe()
    assert "VIXCLS" in cov.verdict_stamp()[1] and "MISSING" not in cov.verdict_stamp()[1]
    row = build_row(1, cov, None)  # the counted durable row
    assert row["full_coverage"] == 0 and "VIXCLS" in row["series_failed"]


@patch("src.data.macro.Fred")
def test_prefetch_mode_never_serves_stale(fred_cls, tmp_path):
    p, _f, cache = _provider(tmp_path, fred_cls, TimeoutError("x"))
    _seed(cache, "VIXCLS", {}, 5)
    p._prefetch_mode = True
    assert len(p._safe_get_series("VIXCLS")) == 0


def _cov(p):
    from src.data.macro import MacroCoverage

    return MacroCoverage(configured=2, succeeded=0, failed=list(p._run_failed))


@patch("src.data.macro.time.sleep")
@patch("src.data.macro.Fred")
def test_stale_served_is_not_missing_in_stamp_prompt_or_row(fred_cls, _s, tmp_path):
    p, _f, cache = _provider(tmp_path, fred_cls, TimeoutError("fetch_deadline_exceeded"))
    _seed(cache, "VIXCLS", {}, 4)
    p._safe_get_series("VIXCLS")
    cov = _cov(p)
    note = cov.verdict_stamp()[1]
    assert "STALE" in note and "VIXCLS" in note and "4d" in note
    assert "MISSING" not in note
    text = cov.describe()
    assert "MISSING (no value at all): none" in text
    assert "VIXCLS (last-good value, 4d old)" in text
    import json

    entry = json.loads(build_row(1, cov, None)["series_failed"])[0]
    assert entry["state"] == "stale" and entry["age_days"] == 4


@patch("src.data.macro.time.sleep")
@patch("src.data.macro.Fred")
def test_absent_series_is_missing_in_stamp_prompt_and_row(fred_cls, _s, tmp_path):
    p, _f, _c = _provider(tmp_path, fred_cls, TimeoutError("fetch_deadline_exceeded"))
    p._safe_get_series("VIXCLS")
    cov = _cov(p)
    note = cov.verdict_stamp()[1]
    assert "MISSING (no value): VIXCLS" in note and "STALE" not in note
    assert "MISSING (no value at all): VIXCLS" in cov.describe()
    import json

    entry = json.loads(build_row(1, cov, None)["series_failed"])[0]
    assert "state" not in entry and "age_days" not in entry
