"""Two deploys must never run at once: a second caller waits on the first.

2026-10-13: two deploy watchers started the script within a minute of each
other; the second rebuilt the dashboard work folder under the first, which then
reported a failed dashboard build and a deploy that did not land.
"""

from __future__ import annotations

import fcntl
import os
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "merge_and_deploy.sh"


def test_a_second_deploy_waits_while_another_holds_the_lock(tmp_path):
    lock = tmp_path / "deploy.lock"
    with open(lock, "a") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        env = dict(os.environ, QAMC_DEPLOY_LOCK=str(lock))
        # It must block at the lock, before any deploy step; timeout ends it.
        run = subprocess.run(
            ["timeout", "3", "bash", str(SCRIPT), "--deploy-only"],
            env=env,
            capture_output=True,
            text=True,
        )
    assert run.returncode == 124, run.stderr[-500:]
    assert "another deploy is running" in run.stderr
    assert "deployed" not in run.stdout
