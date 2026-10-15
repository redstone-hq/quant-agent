"""Correlation matrix construction."""

from datetime import date, timedelta
from unittest.mock import patch

import pytest

from src.data.correlation import build_correlation_matrix
from src.models import OHLCV


def _bars(prices: list[float]) -> list[OHLCV]:
    start = date(2026, 1, 1)
    return [
        OHLCV(date=start + timedelta(days=i), open=p, high=p, low=p, close=p, volume=1_000_000)
        for i, p in enumerate(prices)
    ]


def test_correlation_matrix_detects_parallel_moves():
    """Two series with identical daily returns should correlate +1.0."""
    # Same multiplicative returns → corr = 1.0
    base = [
        100.0,
        101.0,
        102.0,
        103.5,
        102.0,
        104.5,
        105.0,
        106.0,
        104.0,
        107.0,
        108.5,
        109.0,
        110.0,
        108.0,
        111.0,
        112.0,
        113.0,
        111.5,
        114.0,
        115.0,
        116.0,
        117.0,
        116.0,
        118.0,
        119.0,
    ]
    parallel = [p * 2.5 for p in base]  # same pct returns, different price level
    matrix = build_correlation_matrix({"A": _bars(base), "B": _bars(parallel)})
    assert matrix["A"]["B"] == pytest.approx(1.0, abs=0.01)
    assert matrix["B"]["A"] == pytest.approx(1.0, abs=0.01)


def test_correlation_matrix_skips_sparse_symbols():
    """Symbols with <10 bars are dropped (no returns). Healthy pair with enough
    overlap (30 bars → 29 returns ≥ the 20 min_periods) does appear."""
    healthy_a = _bars([100.0 + i for i in range(30)])
    healthy_b = _bars([200.0 + i * 2 for i in range(30)])
    matrix = build_correlation_matrix(
        {
            "A": healthy_a,
            "B": healthy_b,
            "SPARSE": _bars([100.0, 101.0]),  # only 2 bars — dropped upstream
        }
    )
    assert "SPARSE" not in matrix
    assert "A" in matrix
    assert "B" in matrix["A"]  # the pair correlation exists


def test_correlation_matrix_logs_excluded_symbols(caplog):
    """When a symbol gets silently dropped (insufficient bars), the operator
    + the downstream Risk Manager need to see which holdings lost correlation
    coverage. Without this WARN log, a freshly-listed ETF could bypass the
    cluster check unnoticed — exactly the kind of silent gap the audit
    flagged after the 2026-05-11 universe expansion added CHPX (a newly
    launched ETF with very short price history).
    """
    import logging

    healthy_a = _bars([100.0 + i for i in range(30)])
    sparse_b = _bars([100.0, 101.0])  # only 2 bars
    sparse_c = _bars([200.0])  # only 1 bar
    with caplog.at_level(logging.WARNING, logger="src.data.correlation"):
        build_correlation_matrix(
            {
                "A": healthy_a,
                "BAD_B": sparse_b,
                "BAD_C": sparse_c,
            }
        )

    warning_lines = [r.message for r in caplog.records if r.levelno == logging.WARNING]
    assert any("excluded from matrix" in m for m in warning_lines), (
        f"expected a WARN log naming excluded symbols; got {warning_lines}"
    )
    # The dropped symbols must appear in the warning so they can be
    # correlated against the trade book.
    joined = " ".join(warning_lines)
    assert "BAD_B" in joined and "BAD_C" in joined
