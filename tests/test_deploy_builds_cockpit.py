"""The dashboard build never blocks or half-applies a trading deploy.

src/api/static_cockpit/ is build output and is no longer committed (frontend
source once merged without a rebuilt bundle and a panel never appeared live),
so scripts/merge_and_deploy.sh builds it. The order is the point:

- build FIRST, off to the side, before the live checkout is touched;
- a failed build never stops the checkout, units or restart;
- the previous bundle is copied aside and restored if the checkout removed it;
- a stale dashboard ends the run with a loud line and exit status 3.

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


def _index(lines: list[str], pattern: str) -> int:
    hits = [i for i, line in enumerate(lines) if re.search(pattern, line)]
    assert hits, f"no line matching {pattern!r}"
    return hits[0]


DEPLOY = _code_lines(_function_body("deploy"))
BUILD = _function_body("build_cockpit")
INSTALL = _function_body("install_cockpit")


def test_build_runs_before_the_checkout_and_the_restart() -> None:
    build = _index(DEPLOY, r"^build_cockpit ")
    checkout = _index(DEPLOY, r"git .*checkout --detach")
    install = _index(DEPLOY, r"^install_cockpit$")
    restart = _index(DEPLOY, r"qamc_systemctl restart")
    assert build < checkout < install < restart


def test_build_uses_a_separate_copy_of_the_target_commit() -> None:
    # Built from `git archive` of the target sha into the side folder, which
    # lives outside the checkout, and written to a temp outDir.
    assert re.search(r'git -C "\$\{ROOT\}" archive "\$\{TARGET\}" frontend', BUILD)
    assert re.search(r'^COCKPIT_WORK="\$\{DEPLOY_ROOT\}-cockpit-build"$', SCRIPT, re.M)
    assert re.search(r'--outDir "\$\{WORK\}/out"', BUILD)
    assert "npm ci" in BUILD


def test_build_failure_does_not_stop_checkout_or_restart() -> None:
    # The build's failure is caught and recorded, never `exit`ed on ...
    assert re.search(r"if ! qamc_sh TARGET=", BUILD)
    assert not re.search(r"^\s*exit\b", BUILD.split("if ! qamc_sh")[1], re.M)
    assert not re.search(r"^\s*exit\b", INSTALL, re.M)
    # ... and nothing between the build and the restart depends on its result.
    between = DEPLOY[_index(DEPLOY, r"^build_cockpit ") : _index(DEPLOY, r"qamc_systemctl restart")]
    assert not any("DASHBOARD_PROBLEM" in line for line in between)


def test_previous_bundle_is_copied_aside_and_restored() -> None:
    assert re.search(r'cp -a "\$\{LIVE\}" "\$\{WORK\}/prev"', BUILD)
    restore = r'if \[\[ ! -d "\$\{LIVE\}" && -d "\$\{WORK\}/prev" \]\]; then cp -a "\$\{WORK\}/prev" "\$\{LIVE\}"'
    assert re.search(restore, INSTALL)


def test_good_build_is_swapped_in_only_when_there_was_no_problem() -> None:
    assert re.search(r'if \[\[ -z "\$\{DASHBOARD_PROBLEM\}" \]\]', INSTALL)
    assert re.search(r'mv "\$\{WORK\}/out" "\$\{LIVE\}"', INSTALL)


def test_node_check_is_major_version_only_with_a_plain_message() -> None:
    assert 'need="${need%%.*}"' in BUILD
    assert "wrong node version (found ${have}, need ${need}.x)" in BUILD


def test_stale_dashboard_is_loud_and_non_zero() -> None:
    finish = _function_body("finish")
    assert "DASHBOARD NOT UPDATED: ${DASHBOARD_PROBLEM}; trading code deployed" in finish
    assert "exit 3" in finish
    # Both entry points end in finish.
    assert re.search(r"--deploy-only.*?\n\s+deploy\n\s+finish\n", SCRIPT, re.S)
    assert SCRIPT.rstrip().endswith("finish")
    # The served-commit check feeds it.
    assert any("COCKPIT_STAMP" in line for line in DEPLOY)


def test_built_bundle_is_gitignored() -> None:
    gitignore = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert "/src/api/static_cockpit/" in gitignore
