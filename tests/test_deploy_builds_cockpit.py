"""The deploy builds the dashboard bundle, before the restart, and aborts on failure.

src/api/static_cockpit/ is build output and is no longer committed (frontend
source once merged without a rebuilt bundle and a panel never appeared live).
scripts/merge_and_deploy.sh must therefore build it from the checkout BEFORE
restarting anything, refuse to go on when the build fails, swap the new
bundle in only on success, and stamp the commit it was built from.

Static reading of the script, in the same style as the other deploy-script
guards: nothing here runs it.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = (REPO_ROOT / "scripts" / "merge_and_deploy.sh").read_text(encoding="utf-8")


def _function_body(name: str) -> str:
    match = re.search(rf"^{name}\(\) \{{\n(.*?)^\}}\n", SCRIPT, re.S | re.M)
    assert match, f"{name}() not found in merge_and_deploy.sh"
    return match.group(1)


def _code_lines(body: str) -> list[str]:
    return [line.strip() for line in body.splitlines() if line.strip() and not line.strip().startswith("#")]


def test_script_runs_with_errexit() -> None:
    assert "set -euo pipefail" in SCRIPT


def test_deploy_builds_after_checkout_and_before_any_restart() -> None:
    lines = _code_lines(_function_body("deploy"))

    def first(pattern: str) -> int:
        hits = [i for i, line in enumerate(lines) if re.search(pattern, line)]
        assert hits, f"deploy() has no line matching {pattern!r}"
        return hits[0]

    checkout = first(r"git .*checkout --detach")
    build = first(r"^build_cockpit$")
    units = first(r"^install_units$")
    restart = first(r"qamc_systemctl restart")
    assert checkout < build < units < restart


def test_build_failure_aborts_the_deploy() -> None:
    body = _function_body("build_cockpit")
    # The build runs under its own errexit shell, and a non-zero exit from it
    # is turned into a plain message and exit 1 -- never ignored.
    assert "bash -euo pipefail -c" in body
    assert re.search(r"if ! sudo -n -H -u qamc ", body)
    failure_branch = body[body.index("; then") :]
    assert "DEPLOY ABORTED" in failure_branch
    assert "exit 1" in failure_branch
    assert "|| true" not in body


def test_build_installs_from_lockfile_with_pinned_node() -> None:
    body = _function_body("build_cockpit")
    assert "npm ci" in body
    assert "npm run build" in body
    assert ".nvmrc" in body
    assert (REPO_ROOT / "frontend" / ".nvmrc").read_text(encoding="utf-8").strip()


def test_new_bundle_is_built_aside_and_swapped_in_only_after_success() -> None:
    lines = _code_lines(_function_body("build_cockpit"))
    build = next(i for i, line in enumerate(lines) if line.startswith("npm run build"))
    assert "--outDir" in lines[build] and "static_cockpit.new" in " ".join(lines[: build + 1])
    swap = next(i for i, line in enumerate(lines) if re.search(r'mv "\$\{new\}"', line))
    assert build < swap


def test_deploy_verifies_the_served_bundle_matches_the_checkout() -> None:
    deploy = _function_body("deploy")
    assert "COCKPIT_STAMP" in deploy
    assert "DASHBOARD DOES NOT MATCH THE CHECKOUT" in deploy
    assert 'echo "${COMMIT}" > "${new}/${STAMP}"' in _function_body("build_cockpit")


def test_built_bundle_is_gitignored() -> None:
    # The drift check reads `git status`; the build output must be invisible to it.
    gitignore = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert "/src/api/static_cockpit/" in gitignore
    assert "/src/api/.static_cockpit.*" in gitignore
