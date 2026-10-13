"""The deploy restarts every long-running desk unit, not just the API.

2026-10-13: only the API unit was restarted, so a merged change to the owner
switch (Type=simple) never went live until a reboot. The restart set is now
derived from the installed unit files.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

SCRIPT = (Path(__file__).resolve().parent.parent / "scripts" / "merge_and_deploy.sh").read_text(encoding="utf-8")


def _fn(name: str) -> str:
    m = re.search(rf"^{name}\(\) \{{\n.*?^\}}\n", SCRIPT, re.S | re.M)
    assert m, name
    return m.group(0)


def _restart_set(tmp_path: Path, units: dict[str, str]) -> list[str]:
    src = tmp_path / "units"
    src.mkdir()
    for fname, body in units.items():
        (src / fname).write_text(body)
    (src / "paused_units.yaml").write_text("paused: []\n")
    prog = "\n".join(
        [
            'sudo() { shift; "$@"; }',
            f'API_UNIT=quant-agent-api.service; UNIT_SRC="{src}"; PAUSED_UNITS="{src}/paused_units.yaml"',
            _fn("unit_is_paused"),
            _fn("unit_sources"),
            _fn("long_running_units"),
            "long_running_units",
        ]
    )
    out = subprocess.run(["bash", "-c", prog], capture_output=True, text=True, check=True).stdout
    return out.split()


def _svc(kind: str) -> str:
    return f"[Service]\nType={kind}\nExecStart=/bin/true\n"


def test_new_simple_unit_restarted_and_oneshot_not(tmp_path) -> None:
    got = _restart_set(
        tmp_path,
        {
            "quant-agent-api.service": _svc("simple"),
            "quant-agent-owner-switch.service": _svc("simple"),
            "quant-agent-brand-new.service": _svc("notify"),
            "quant-agent-job.service": _svc("oneshot"),
            "quant-agent-job.timer": "[Timer]\nOnCalendar=daily\n",
            "quant-agent-timered-simple.service": _svc("simple"),
            "quant-agent-timered-simple.timer": "[Timer]\nOnCalendar=daily\n",
        },
    )
    assert sorted(got) == ["quant-agent-brand-new.service", "quant-agent-owner-switch.service"]


def test_deploy_restarts_api_first_then_the_derived_set() -> None:
    deploy = _fn("deploy")
    assert deploy.index('restart "${API_UNIT}"') < deploy.index("long_running_units")
    assert 'restart "${name}"' in deploy
