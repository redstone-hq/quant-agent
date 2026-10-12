"""Owner Stop reaches EVERY desk timer, not only the ones that go through main.py.

Owner ruling 2026-10-09: Stop = the desk is OFF -- nothing reads, nothing runs,
no AI; positions and the protective stops at the broker are kept. Each unit in
scripts/systemd either carries the one shared gate (scripts/owner_stop_gate.sh
as ExecCondition=, so a stopped unit is skipped, not failed) or is named below
with the reason it must keep running while Stopped.
"""

import os
import re
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from src.owner_flags import RESUME, STOP
from src.owner_intents import process_pending, raise_intent
from src.storage.schema.owner_intent_tables import apply

ROOT = Path(__file__).resolve().parent.parent
UNITS = ROOT / "scripts" / "systemd"
GATE = ROOT / "scripts" / "owner_stop_gate.sh"
GATE_LINE = "ExecCondition=/home/qamc/quant-agent/scripts/owner_stop_gate.sh %n"

# Fixed, named exemptions. Adding one is an owner-visible decision: say why the
# unit must run while the desk is Stopped.
EXEMPT = {
    # The owner's panel API: Start is pressed through it, so a Stopped desk
    # could never be started again if it were gated.
    "quant-agent-api.service": "the owner presses Start through it",
    "quant-agent-owner-switch.service": "the owner's phone records Start through it",
}


def _services():
    return sorted(UNITS.glob("*.service"))


def _service_lines(path):
    lines, in_service = [], False
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if line.startswith("["):
            in_service = line == "[Service]"
        elif in_service and line and not line.startswith("#"):
            lines.append(line)
    return lines


def test_there_are_services_to_check():
    assert len(_services()) > len(EXEMPT)


@pytest.mark.parametrize("service", _services(), ids=lambda p: p.name)
def test_every_service_carries_the_owner_stop_gate_or_is_a_named_exemption(service):
    gated = GATE_LINE in _service_lines(service)
    if service.name in EXEMPT:
        assert not gated, f"{service.name} is exempt ({EXEMPT[service.name]}) yet gated"
    else:
        assert gated, f"{service.name} does desk work but ignores owner Stop: add `{GATE_LINE}` to [Service]"


def test_every_exemption_names_a_real_unit():
    assert set(EXEMPT) <= {p.name for p in _services()}


def test_the_gate_is_executable():
    assert os.access(GATE, os.X_OK)


def _config_for(tmp_path, db):
    text = (ROOT / "config" / "settings.yaml").read_text()
    text, n = re.subn(r"(?m)^(\s+db_path:\s*).*$", rf'\g<1>"{db}"', text)
    assert n == 1, "settings.yaml no longer has exactly one db_path"
    cfg = tmp_path / "settings.yaml"
    cfg.write_text(text)
    return cfg


def _db(tmp_path, *actions):
    db = tmp_path / "intents.db"
    conn = sqlite3.connect(db)
    apply(conn)
    for action in actions:
        raise_intent(conn, action)
        process_pending(conn)
    conn.close()
    return db


def _gate(tmp_path, db, python=sys.executable):
    env = dict(os.environ)
    env.update(
        PYTHON_OVERRIDE=python,
        OWNER_STOP_GATE_CONFIG=str(_config_for(tmp_path, db)),
        # The stopped branch makes one broker call (the resting-entry sweep);
        # a dead proxy fails it at once so the test never touches the network.
        HTTPS_PROXY="http://127.0.0.1:9",
        HTTP_PROXY="http://127.0.0.1:9",
        OWNER_STOP_GATE_TIMEOUT_SEC="60",
        # The real config refuses to load with empty broker keys (and an
        # unloadable config is "cannot decide" -> run); stand-ins only.
        ALPACA_API_KEY="test-key",
        ALPACA_SECRET_KEY="test-secret",
        FRED_API_KEY="test-fred",
        GOOGLE_API_KEY="test-google",
        ANTHROPIC_API_KEY="test-anthropic",
        OPENROUTER_API_KEY="test-openrouter",
        OPENAI_API_KEY="test-openai",
    )
    return subprocess.run([str(GATE), "test.service"], env=env, capture_output=True, text=True, timeout=120)


def test_gate_skips_the_unit_while_stopped(tmp_path):
    result = _gate(tmp_path, _db(tmp_path, STOP))
    assert result.returncode == 1, result.stderr
    assert "owner Stop in force" in result.stderr


def test_gate_runs_the_unit_when_never_stopped(tmp_path):
    result = _gate(tmp_path, _db(tmp_path))
    assert result.returncode == 0, result.stderr
    assert "could not decide" not in result.stderr


def test_gate_runs_the_unit_once_start_clears_stop(tmp_path):
    result = _gate(tmp_path, _db(tmp_path, STOP, RESUME))
    assert result.returncode == 0, result.stderr
    assert "could not decide" not in result.stderr


def test_a_gate_that_cannot_decide_runs_the_unit_rather_than_silently_skipping_it(tmp_path):
    """An UNKNOWN read is not a Stop (src.owner_flags.stop_in_force): a broken
    gate must never switch the desk's exits off by skipping every unit."""
    result = _gate(tmp_path, _db(tmp_path, STOP), python=str(tmp_path / "no-such-python"))
    assert result.returncode == 0, result.stderr
    assert "could not decide" in result.stderr
