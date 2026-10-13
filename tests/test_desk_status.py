"""On-demand desk status (`scripts/desk_status.py` + `trader_feed.format_desk_status`).

The point of the feature is that the owner sees the REAL message format
with current data. So these tests hold the two properties that make that
true and safe: it renders from live broker state without ordering or
paying for anything, and a field it genuinely cannot know says
"not available" rather than "OK" or zero.
"""

import sqlite3
from src.storage.db import Database
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from src import trader_feed

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

_ET = ZoneInfo("America/New_York")
_WHEN = datetime(2026, 9, 17, 22, 12, tzinfo=_ET)

_ACCOUNT = {
    "cash": 1000.0,
    "portfolio_value": 101_234.56,
    "last_equity": 100_000.00,
    "non_marginable_buying_power": 1000.0,
}


class _Pos:
    """Duck-types the broker's `Position` for the two attributes the
    renderer reads; deliberately not the pydantic model, so a schema
    change there cannot silently make this test meaningless."""

    def __init__(self, symbol, qty, market_value):
        self.symbol = symbol
        self.qty = qty
        self.avg_entry = 10.0
        self.current_price = 11.0
        self.market_value = market_value
        self.unrealized_pnl = 1.0


def _empty_db(tmp_path, monkeypatch):
    db = tmp_path / "quant_agent.db"
    _d = Database(str(db))
    _d.initialize()  # the real schema, not a hand-built copy
    _d.close()
    monkeypatch.setattr(trader_feed, "_DB_PATH", db)
    return db


@pytest.fixture()
def rendered(tmp_path, monkeypatch):
    _empty_db(tmp_path, monkeypatch)
    monkeypatch.setattr(trader_feed, "et_now", lambda: _WHEN)
    return trader_feed.format_desk_status(
        _ACCOUNT,
        [_Pos("AAPL", 10, 2000.0), _Pos("NVDA", -5, -900.0), _Pos("SGOV", 3, 300.0)],
        0.4,
    )


def test_renders_the_shared_desk_check_format_from_live_broker_state(rendered):
    header = rendered.splitlines()[0]
    # Same header the scheduled top-of-hour message uses, 12-hour clock
    # with the timezone kept (owner's wording rule).
    assert header.startswith("🕐 DESK CHECK · ")
    assert "10:12 PM ET" in header
    assert "22:12" not in header
    # Equity and P&L are the BROKER's, computed from the account dict —
    # +$1,234.56 on a $100,000 prior close.
    assert "+$1,234.56" in rendered
    assert "+1.23%" in rendered
    # Sweep vehicles are excluded from "positions held", exactly as the
    # scheduled path excludes them; the short counts.
    assert "💼 Positions held: 2" in rendered


def test_unknown_fields_say_not_available_and_never_zero_or_ok(rendered):
    """The two things an on-demand read genuinely cannot know. The
    scheduled path prints "Stop coverage: OK" because it has actually
    audited; printing that here would be a lie, and printing 0 gaps would
    be the same lie in numeric form."""
    assert "🛡️ Stop coverage: not available" in rendered
    assert "Stop coverage: OK" not in rendered
    assert "🔎 Movers scanned: not available" in rendered
    assert "Movers scanned: 0" not in rendered
    assert "Signals this hour" not in rendered


def test_format_holds_no_copy_of_the_wording(tmp_path, monkeypatch):
    """`format_desk_status` must DRIVE the shared formatter, not
    reimplement it — otherwise the owner's on-demand message drifts from
    the scheduled ones as those are changed."""
    _empty_db(tmp_path, monkeypatch)
    monkeypatch.setattr(trader_feed, "et_now", lambda: _WHEN)
    seen = {}

    def _spy(result, nested, elapsed, **kwargs):
        seen.update(kwargs)
        seen["result"] = result
        return "SENTINEL"

    monkeypatch.setattr(trader_feed, "_format_hourly_desk_check", _spy)
    assert trader_feed.format_desk_status(_ACCOUNT, [], 0.1) == "SENTINEL"
    assert seen["snap"]["positions"] == []
    assert seen["result"]["run_id"] is None


def test_hourly_desk_check_defaults_are_unchanged(tmp_path, monkeypatch):
    """The new keyword arguments must not alter the scheduled path: with
    none of them passed, the formatter still reads its own snapshot and
    still prints the audited "OK"."""
    _empty_db(tmp_path, monkeypatch)
    monkeypatch.setattr(trader_feed, "et_now", lambda: _WHEN)
    msg = trader_feed._format_hourly_desk_check(
        {"run_id": "run-x", "daily_pnl": None},
        None,
        1.0,
    )
    assert "Covering the last hour" in msg
    assert "on demand" not in msg
    assert "🛡️ Stop coverage: OK" in msg


def _load_script():
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "desk_status_script",
        PROJECT_ROOT / "scripts" / "desk_status.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _StubBroker:
    """Records every method touched, so the test can prove the command
    only ever performed read-only broker calls."""

    calls: list[str] = []

    def __init__(self, *args, **kwargs):
        type(self).calls = []

    def __getattr__(self, name):
        def _recorder(*args, **kwargs):
            type(self).calls.append(name)
            if name == "get_account":
                return _ACCOUNT
            if name == "get_positions":
                return [_Pos("AAPL", 10, 2000.0)]
            raise AssertionError(f"desk_status must not call broker.{name}")

        return _recorder


def test_dry_run_prints_and_sends_nothing_and_places_no_order(
    tmp_path,
    monkeypatch,
    capsys,
):
    _empty_db(tmp_path, monkeypatch)
    monkeypatch.setattr(trader_feed, "et_now", lambda: _WHEN)
    monkeypatch.setenv("ALPACA_API_KEY", "test-key")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "test-secret")
    monkeypatch.setenv("FRED_API_KEY", "test-fred")
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-router")
    monkeypatch.setenv("GOOGLE_API_KEY", "test-google")

    import src.execution.broker as broker_module
    import src.notifier as notifier_module

    monkeypatch.setattr(broker_module, "AlpacaBroker", _StubBroker)

    def _explode(*args, **kwargs):
        raise AssertionError("desk_status must not send in --dry-run")

    monkeypatch.setattr(notifier_module.TelegramNotifier, "send", _explode)

    db = tmp_path / "quant_agent.db"
    before = (db.stat().st_mtime_ns, db.stat().st_size)

    script = _load_script()
    assert script.main(["--dry-run"]) == 0

    # Writes nothing: the database is byte-for-byte untouched, so a
    # scheduled session running at the same moment cannot be disturbed.
    assert (db.stat().st_mtime_ns, db.stat().st_size) == before

    out = capsys.readouterr().out
    assert out.splitlines()[0].startswith("🕐 DESK CHECK · ")
    assert "not available" in out
    # Read-only in the strong sense: only the two GET-shaped broker reads.
    assert sorted(_StubBroker.calls) == ["get_account", "get_positions"]


def test_broker_failure_refuses_to_send_rather_than_invent_a_pnl(
    tmp_path,
    monkeypatch,
):
    _empty_db(tmp_path, monkeypatch)
    monkeypatch.setenv("ALPACA_API_KEY", "test-key")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "test-secret")
    monkeypatch.setenv("FRED_API_KEY", "test-fred")
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-router")
    monkeypatch.setenv("GOOGLE_API_KEY", "test-google")

    import src.execution.broker as broker_module
    import src.notifier as notifier_module

    class _DeadBroker:
        def __init__(self, *args, **kwargs):
            pass

        def get_account(self):
            raise RuntimeError("alpaca unreachable")

    monkeypatch.setattr(broker_module, "AlpacaBroker", _DeadBroker)

    def _explode(*args, **kwargs):
        raise AssertionError("must not send on a broker failure")

    monkeypatch.setattr(notifier_module.TelegramNotifier, "send", _explode)

    script = _load_script()
    assert script.main([]) == 3
