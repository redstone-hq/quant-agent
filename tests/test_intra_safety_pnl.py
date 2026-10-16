"""The half-hourly intra_safety alert must show real P&L, not "not available" (checklist m03)."""

from unittest.mock import MagicMock

from src.intraday.safety import IntradaySafety
from src.notifier.session import format_session_result
from src.pipeline_parts.morning_helpers import _attach_pnl, _record_account_snapshot


class _Host:
    """Stands in for the pipeline the real wiring hands the part."""

    _last_account_snapshot = None

    def _total_pnl_since_reset(self, total_value):
        return (total_value - 100000.0, (total_value - 100000.0) / 1000.0, "2026-09-02")


class _State:
    def __init__(self, host):
        self.host = host

    def get(self, k):
        return getattr(self.host, k, None)

    def set(self, k, v):
        setattr(self.host, k, v)


def _build(broker):
    host = _Host()
    kwargs = dict(
        broker=broker,
        is_trading_day=lambda: True,
        kill_switch_halt_result=lambda run_id: None,
        run_intra_safety_preamble=lambda run_id: ([], ""),
        state=_State(host),
    )
    # The pre-fix class has no P&L collaborators; pass them only when it accepts them.
    kwargs.update(
        attach_pnl=lambda r: _attach_pnl(host, r),
        record_account_snapshot=lambda t, e: _record_account_snapshot(host, t, e),
    )
    return IntradaySafety(**kwargs)


def test_safety_result_carries_real_pnl():
    broker = MagicMock()
    broker.get_account.return_value = {"portfolio_value": 101500.0, "last_equity": 101000.0, "cash": 5.0}
    result = _build(broker).run_intra_safety()
    assert result["daily_pnl"] == 500.0
    assert result["total_pnl"] == 1500.0
    text = format_session_result("intra_safety", result, 1.0) or ""
    assert "No P&L figure was recorded" not in text
    assert "not available" not in text


def test_failed_account_read_gives_reason_code_not_generic_fallback():
    broker = MagicMock()
    broker.get_account.side_effect = RuntimeError("down")
    result = _build(broker).run_intra_safety()
    assert result["pnl_unavailable_reason"] == "ended_before_account_read"
    assert "No P&L figure was recorded" not in (format_session_result("intra_safety", result, 1.0) or "")
