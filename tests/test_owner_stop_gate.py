"""The owner Stop gate decides from the database alone (defect measured 2026-10-13).

systemd 255 advertises CREDENTIALS_DIRECTORY to ExecCondition= but does not
create it. The old gate went through load_config, which raised
CredentialDeliveryError on every unit start and fell through to "run": the
gate never decided anything. These tests run the real shell gate in exactly
that environment.
"""

import os
import re
import sqlite3
import subprocess
import sys
from pathlib import Path

from src import owner_stop_gate as gate
from src.owner_intents import RESUME, STOP, process_pending, raise_intent
from src.storage.schema.owner_intent_tables import apply

ROOT = Path(__file__).resolve().parent.parent
GATE = ROOT / "scripts" / "owner_stop_gate.sh"
BROKER_ENV = ("ALPACA_API_KEY", "ALPACA_SECRET_KEY", "APCA_API_KEY_ID", "APCA_API_SECRET_KEY")


def _db(tmp_path, *actions):
    db = tmp_path / "intents.db"
    conn = sqlite3.connect(db)
    apply(conn)
    for action in actions:
        raise_intent(conn, action)
        process_pending(conn)
    conn.close()
    return db


def _config_for(tmp_path, db):
    text = (ROOT / "config" / "settings.yaml").read_text()
    text, n = re.subn(r"(?m)^(\s+db_path:\s*).*$", rf'\g<1>"{db}"', text)
    assert n == 1, "settings.yaml no longer has exactly one db_path"
    cfg = tmp_path / "settings.yaml"
    cfg.write_text(text)
    return cfg


def _systemd_255_gate(tmp_path, db, unit="test.service"):
    """The shell gate as systemd 255 runs it: credentials advertised, not there, none in env."""
    env = {k: v for k, v in os.environ.items() if k not in BROKER_ENV}
    env.update(
        PYTHON_OVERRIDE=sys.executable,
        OWNER_STOP_GATE_CONFIG=str(_config_for(tmp_path, db)),
        OWNER_STOP_GATE_LOG=str(tmp_path / "quant_agent.log"),
        CREDENTIALS_DIRECTORY=str(tmp_path / "credentials" / "quant-agent-test.service"),
        HTTPS_PROXY="http://127.0.0.1:9",
        HTTP_PROXY="http://127.0.0.1:9",
    )
    return subprocess.run([str(GATE), unit], env=env, capture_output=True, text=True, timeout=120)


def test_missing_credentials_directory_still_decides_stop_on(tmp_path):
    result = _systemd_255_gate(tmp_path, _db(tmp_path, STOP))
    assert result.returncode == 1, result.stderr
    assert "owner Stop in force" in result.stderr
    assert "could not decide" not in result.stderr


def test_missing_credentials_directory_still_decides_stop_off(tmp_path):
    result = _systemd_255_gate(tmp_path, _db(tmp_path, STOP, RESUME))
    assert result.returncode == 0, result.stderr
    assert "could not decide" not in result.stderr


def test_the_decision_loads_no_credentials_and_no_broker(tmp_path):
    cfg = _config_for(tmp_path, _db(tmp_path, STOP))
    code = (
        "import sys; from src import owner_stop_gate as g; rc = g.decide(sys.argv[1], 'x.service'); "
        "bad = [m for m in ('src.credentials', 'src.config', 'src.execution.broker') if m in sys.modules]; "
        "print(rc, bad)"
    )
    out = subprocess.run([sys.executable, "-c", code, str(cfg)], cwd=ROOT, capture_output=True, text=True, timeout=60)
    assert out.stdout.strip() == "75 []", out.stderr


def test_every_gated_unit_is_skipped_while_stopped(tmp_path):
    cfg = _config_for(tmp_path, _db(tmp_path, STOP))
    assert gate.decide(cfg, "quant-agent-intra_safety.service") == gate.STOP
    assert gate.decide(cfg, "quant-agent-status-board.service") == gate.STOP


def test_could_not_decide_is_a_named_durable_fault_and_the_unit_runs(tmp_path, monkeypatch, caplog):
    recorded = []
    monkeypatch.setattr("src.sentinel.counted.record_swallowed", lambda where, exc, **kw: recorded.append(where))
    cfg = tmp_path / "settings.yaml"
    cfg.write_text("storage:\n  db_path: ${NOT_A_PATH}\n")
    assert gate.decide(cfg, "x.service") == gate.UNDECIDED
    assert recorded == [gate.UNDECIDED_SITE]
    assert any("owner Stop gate could not decide" in r.getMessage() and r.levelname == "ERROR" for r in caplog.records)


def test_a_gate_whose_python_never_starts_logs_the_fault_where_the_health_report_reads(tmp_path):
    env = dict(os.environ, PYTHON_OVERRIDE=str(tmp_path / "no-such-python"))
    env["OWNER_STOP_GATE_LOG"] = str(tmp_path / "quant_agent.log")
    result = subprocess.run([str(GATE), "x.service"], env=env, capture_output=True, text=True, timeout=60)
    assert result.returncode == 0
    line = (tmp_path / "quant_agent.log").read_text()
    assert re.match(
        r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d,\d{3} \[ERROR\] src\.owner_stop_gate: owner Stop gate could not decide", line
    )
