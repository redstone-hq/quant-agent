"""Pre-market earnings message: real P&L when the account reads, the reason when it fails.

Owner Telegram 19 Oct 8:05 AM ET said "Today's P&L: not available".
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from src.trader_feed.common import _pnl_unavailable_sentence
from tests.pipeline_factory import build_pipeline


def _pipeline(broker):
    return build_pipeline(
        broker=broker,
        _run_earnings_preprocess_body=lambda: {"status": "nothing_new", "count": 0, "symbols": ["ZZZ"]},
        _total_pnl_since_reset=lambda tv: (74.90, 0.749, "2026-10-12"),
    )


def test_premarket_result_carries_real_pnl_from_account_read():
    broker = MagicMock()
    broker.get_account.return_value = {"portfolio_value": 10_041.44, "last_equity": 10_000.0}

    result = _pipeline(broker).run_earnings_preprocess()

    assert result["daily_pnl"] == pytest.approx(41.44)
    assert result["total_pnl"] == 74.90
    assert result["total_pnl_since"] == "2026-10-12"
    assert "pnl_unavailable_reason" not in result
    broker.submit_order.assert_not_called()


def test_premarket_failed_account_read_names_the_reason():
    broker = MagicMock()
    broker.get_account.side_effect = TimeoutError("boom")

    result = _pipeline(broker).run_earnings_preprocess()

    assert "daily_pnl" not in result
    assert result["pnl_unavailable_reason"] == "account_read_failed:TimeoutError"
    assert "TimeoutError" in _pnl_unavailable_sentence(result)
