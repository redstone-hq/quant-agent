"""The daily universe screen is scheduled Mon-Fri at 03:00 ET (owner ruling 2026-10-13):
after the previous close, five hours before the desk's day starts at 08:00 ET."""

from __future__ import annotations

import re
from pathlib import Path

UNITS = Path(__file__).resolve().parent.parent / "scripts" / "systemd"


def _text(name: str) -> str:
    return (UNITS / name).read_text()


def test_timer_fires_weekdays_at_three_am_new_york_time():
    m = re.search(r"^OnCalendar=(.+)$", _text("quant-agent-universe-daily.timer"), re.M)
    assert m is not None
    assert m.group(1) == "Mon..Fri 03:00 America/New_York"


def test_timer_is_persistent():
    assert "Persistent=true" in _text("quant-agent-universe-daily.timer")


def test_service_runs_the_universe_screen_module():
    text = _text("quant-agent-universe-daily.service")
    assert "exec /home/qamc/quant-agent/.venv/bin/python -m src.universe_daily" in text
    # The broker keys arrive as systemd credentials, never from .env.
    assert "LoadCredential=alpaca_api_key:" in text
    assert "LoadCredential=alpaca_secret_key:" in text
    assert "WorkingDirectory=/home/qamc/quant-agent" in text


def test_no_other_timer_shares_the_fire_time():
    for t in UNITS.glob("*.timer"):
        if t.name == "quant-agent-universe-daily.timer":
            continue
        for line in t.read_text().splitlines():
            assert not (line.startswith("OnCalendar=") and "03:00" in line), t.name
