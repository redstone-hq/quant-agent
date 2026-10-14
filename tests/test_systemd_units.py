"""The schedule, under version control — and the check that keeps it there.

THE DEFECT. QAMC's schedule *is* its systemd user units. On 2026-08-31 the
production box ran eleven services and ten timers; the repository tracked
six services and four timers. The seven units that run the trading day and
serve Mission Control — api, morning, midday, intra_check, close, evening,
earnings_preprocess — existed nowhere but on one machine. Had the box been
lost they would have been rebuilt from memory.

The tracked copies were not trustworthy either. `quant-agent-daily.service`
declared `WorkingDirectory=/home/yebo/quant-agent` and ran
`/home/yebo/quant-agent/scripts/run_daily_export.sh` — a path inherited from
the upstream fork this project came from, which exists on no QAMC machine.
Installing the repository's own copy would have broken the daily export on
contact. Nothing compared the two, so nobody knew.

TWO HALVES, BOTH NEEDED. `check_deploy_drift.py` compares COMMITS, so it is
satisfied the moment the checkout sits on origin/main — while the units
systemd actually loaded are whatever was last `cp`'d into
~/.config/systemd/user. git never sees an install.

  - This file is the CI half. It cannot see the box; it gates the
    repository so the tracked units stay installable and stay honest. The
    `/home/yebo` defect is a test here, by name.
  - `scripts/check_unit_drift.py` is the box half. It cannot see a pull
    request; it compares installed against tracked on a daily timer and
    alerts. Its behaviour is tested at the bottom of this file.

Neither half subsumes the other. CI stops the repository regressing; the
timer stops the box diverging.

Nothing here touches the network, the production box, or systemd itself.
"""

from __future__ import annotations

import re
import stat
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SYSTEMD_DIR = PROJECT_ROOT / "scripts" / "systemd"
SCRIPTS_DIR = PROJECT_ROOT / "scripts"

# The one account QAMC is deployed under. `/home/yebo` is the upstream
# fork's path and must never reappear in a unit.
DEPLOY_USER = "qamc"
DEPLOY_ROOT = f"/home/{DEPLOY_USER}/quant-agent"

SERVICES = sorted(SYSTEMD_DIR.glob("*.service"))
TIMERS = sorted(SYSTEMD_DIR.glob("*.timer"))
ALL_UNITS = sorted(SERVICES + TIMERS)

# A `.service` with no paired `.timer` must be a long-running daemon that
# enables itself through `[Install]`. Anything else is a unit nothing can
# ever start.
_HOME_PATH = re.compile(r"/home/([A-Za-z0-9_.-]+)")

# Credential shapes that must never appear inline in a tracked unit.
_SECRET_SHAPES = (
    (re.compile(r"\b\d{6,}:[A-Za-z0-9_-]{30,}\b"), "Telegram bot token"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "AWS access key id"),
    (re.compile(r"\bsk-[A-Za-z0-9]{20,}\b"), "OpenAI-style secret key"),
    (re.compile(r"\bghp_[A-Za-z0-9]{36}\b"), "GitHub personal access token"),
    (re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"), "Slack token"),
)
# `Environment=KEY=VALUE` whose KEY looks like a credential.
_CREDENTIAL_KEY = re.compile(
    r"(TOKEN|SECRET|PASSWORD|PASSWD|API_?KEY|ACCESS_?KEY|CHAT_?ID|CREDENTIAL)",
    re.IGNORECASE,
)


def parse_unit(path: Path) -> dict[str, list[str]]:
    """`SECTION.Key -> [values]`. Not configparser: systemd allows a key to
    repeat (two `OnCalendar=` lines is the normal way to say "twice a day")
    and configparser would silently keep only the last."""
    parsed: dict[str, list[str]] = {}
    section = ""
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith(";"):
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1]
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        parsed.setdefault(f"{section}.{key.strip()}", []).append(value.strip())
    return parsed


# ===========================================================================
# 1. The repository must describe the machine it is deployed to
# ===========================================================================


def test_the_units_directory_is_not_empty():
    """A guard on every parametrization below: `glob` returning nothing
    would make each of them vacuously pass."""
    assert SERVICES, "no .service files found — the gates below prove nothing"
    assert TIMERS, "no .timer files found — the gates below prove nothing"


@pytest.mark.parametrize("unit", ALL_UNITS, ids=lambda p: p.name)
def test_no_unit_carries_a_foreign_home_path(unit: Path):
    """THE `/home/yebo` DEFECT, as a test.

    `quant-agent-daily.service` shipped with `/home/yebo/quant-agent` — the
    upstream fork's path — while the box ran `/home/qamc/quant-agent`.
    Installing the tracked copy would have broken the daily P&L export, and
    the repository had no way to notice.
    """
    foreign = sorted({user for user in _HOME_PATH.findall(unit.read_text()) if user != DEPLOY_USER})
    assert not foreign, (
        f"{unit.name} references /home/{', /home/'.join(foreign)}; QAMC is "
        f"deployed at {DEPLOY_ROOT}. A unit carrying another account's path "
        f"is broken the moment it is installed."
    )


@pytest.mark.parametrize("service", SERVICES, ids=lambda p: p.name)
def test_every_service_runs_from_the_deploy_root(service: Path):
    working_dir = parse_unit(service).get("Service.WorkingDirectory")
    assert working_dir, f"{service.name} declares no WorkingDirectory"
    assert working_dir[0] == DEPLOY_ROOT, (
        f"{service.name} has WorkingDirectory={working_dir[0]}, expected {DEPLOY_ROOT}"
    )


@pytest.mark.parametrize("service", SERVICES, ids=lambda p: p.name)
def test_every_service_execstart_exists_in_the_repo(service: Path):
    """A unit whose ExecStart names a repo script that no longer exists is a
    rename away from a silent, permanent failure. Matched by basename: units
    carry absolute deploy paths that exist neither in a checkout nor in CI.
    """
    exec_start = parse_unit(service).get("Service.ExecStart")
    assert exec_start, f"{service.name} declares no ExecStart"
    for token in exec_start[0].split():
        if token.endswith((".py", ".sh")):
            candidate = SCRIPTS_DIR / Path(token).name
            assert candidate.is_file(), (
                f"{service.name} runs {token}, but scripts/{candidate.name} does not exist in the repository"
            )


@pytest.mark.parametrize("timer", TIMERS, ids=lambda p: p.name)
def test_every_timer_has_the_service_it_activates(timer: Path):
    service = SYSTEMD_DIR / f"{timer.stem}.service"
    assert service.is_file(), f"{timer.name} would activate {service.name}, which is not tracked"


@pytest.mark.parametrize("timer", TIMERS, ids=lambda p: p.name)
def test_every_timer_is_installable_and_scheduled(timer: Path):
    parsed = parse_unit(timer)
    assert parsed.get("Timer.OnCalendar"), f"{timer.name} declares no OnCalendar — it would never fire"
    assert parsed.get("Install.WantedBy"), (
        f"{timer.name} has no [Install] WantedBy, so `systemctl --user "
        f"enable` has nothing to link and the timer never starts"
    )


@pytest.mark.parametrize("service", SERVICES, ids=lambda p: p.name)
def test_every_service_is_reachable(service: Path):
    """A service is started by a paired activator unit or by `[Install]`.
    A service with neither is dead code that looks like infrastructure —
    which is worse than absent, because it reads as covered.

    A `.path` unit activates its service exactly as a `.timer` does — it
    watches a file instead of a clock. The status board is triggered that
    way, so accepting only `.timer` here would fail a unit that is in fact
    reachable.
    """
    activators = [SYSTEMD_DIR / f"{service.stem}{ext}" for ext in (".timer", ".path")]
    has_activator = any(a.is_file() for a in activators)
    declares_install = bool(parse_unit(service).get("Install.WantedBy"))
    assert has_activator or declares_install, (
        f"{service.name} has no paired .timer or .path activator and no "
        f"[Install] WantedBy — nothing on the box would ever start it"
    )


@pytest.mark.parametrize("unit", ALL_UNITS, ids=lambda p: p.name)
def test_no_unit_contains_an_inline_secret(unit: Path):
    """Units are world-readable on the box and public in git. Credentials
    reach them through `EnvironmentFile=` or a wrapper that sources `.env`,
    never inline. This is the by-hand scrub of 2026-08-31, mechanised.
    """
    text = unit.read_text()
    for pattern, label in _SECRET_SHAPES:
        assert not pattern.search(text), (
            f"{unit.name} appears to contain a {label} inline. Route it "
            f"through EnvironmentFile= or an .env-sourcing wrapper."
        )
    parsed = parse_unit(unit)
    for section_key, values in parsed.items():
        if not section_key.endswith(".Environment"):
            continue
        for value in values:
            name = value.partition("=")[0]
            assert not _CREDENTIAL_KEY.search(name), (
                f"{unit.name} sets Environment={name}=... inline. A "
                f"credential-shaped variable belongs in .env, not in a "
                f"tracked unit."
            )


@pytest.mark.parametrize("unit", ALL_UNITS, ids=lambda p: p.name)
def test_every_unit_describes_itself(unit: Path):
    description = parse_unit(unit).get("Unit.Description")
    assert description and description[0], (
        f"{unit.name} has no Description; `systemctl --user list-timers` would show it as a bare filename"
    )


# ===========================================================================
# 2. The production units the repository was missing
# ===========================================================================
#
# Capturing them once fixes nothing on its own — a later refactor could
# delete one and CI would stay green. These name what the box actually runs.

# The six ET-windowed trading sessions, self-gating through one wrapper.
SESSION_MODES = (
    "morning",
    "midday",
    "intra_check",
    "close",
    "evening",
    "earnings_preprocess",
)


@pytest.mark.parametrize("mode", SESSION_MODES)
def test_every_trading_session_has_a_tracked_service_and_timer(mode: str):
    """These seven units ran the trading day from a single machine's
    ~/.config until 2026-08-31."""
    service = SYSTEMD_DIR / f"quant-agent-{mode}.service"
    timer = SYSTEMD_DIR / f"quant-agent-{mode}.timer"
    assert service.is_file(), f"{service.name} is not tracked"
    assert timer.is_file(), f"{timer.name} is not tracked"


@pytest.mark.parametrize("mode", SESSION_MODES)
def test_every_session_service_self_gates_through_the_window_wrapper(mode: str):
    """The sessions do not each carry their own schedule. Every one fires on
    a 30-minute timer and `run_if_et_window.sh` decides whether this is that
    session's ET window — one place where the trading calendar lives.
    """
    service = SYSTEMD_DIR / f"quant-agent-{mode}.service"
    exec_start = parse_unit(service)["Service.ExecStart"][0]
    assert exec_start.endswith(f"run_if_et_window.sh {mode}"), (
        f"{service.name} runs {exec_start!r}; expected the shared window wrapper invoked with the {mode!r} mode"
    )


def test_the_mission_control_api_is_tracked_and_self_starting():
    """The API has no timer: it is a long-running daemon, enabled through
    `[Install] WantedBy=default.target` and restarted on failure."""
    service = SYSTEMD_DIR / "quant-agent-api.service"
    assert service.is_file(), "quant-agent-api.service is not tracked"
    parsed = parse_unit(service)
    assert parsed["Service.Type"] == ["simple"]
    assert parsed["Service.Restart"] == ["always"]
    assert parsed["Install.WantedBy"] == ["default.target"]


def test_the_daily_export_no_longer_points_at_the_upstream_fork():
    """Regression pin for the exact defect. Kept separate from the
    parametrized `/home/` gate so a failure names the unit that had it."""
    text = (SYSTEMD_DIR / "quant-agent-daily.service").read_text()
    assert "/home/yebo" not in text
    assert f"ExecStart={DEPLOY_ROOT}/scripts/run_daily_export.sh" in text


# ===========================================================================
# 3. The box half — scripts/check_unit_drift.py
# ===========================================================================
#
# Every case below builds two throwaway directories and compares them. The
# real /home/qamc paths are never read.

UNIT_BODY = """\
[Unit]
Description=example

[Service]
Type=oneshot
WorkingDirectory=/home/qamc/quant-agent
ExecStart=/home/qamc/quant-agent/scripts/run_daily_export.sh
"""

TIMER_BODY = """\
[Unit]
Description=example timer

[Timer]
OnCalendar=*-*-* 08:50 America/New_York

[Install]
WantedBy=timers.target
"""

# A `.path` unit activates its paired service on a filesystem event instead
# of a clock. It enables into `paths.target.wants`, NOT `timers.target.wants`
# — the enable/active semantics the checker must get right for this suffix.
PATH_BODY = """\
[Unit]
Description=example path watcher

[Path]
PathChanged=/home/qamc/quant-agent/docs/WORK.md

[Install]
WantedBy=paths.target
"""


def _make_box(
    tmp_path: Path, repo: dict[str, str], installed: dict[str, str], enabled: tuple[str, ...] = ()
) -> tuple[Path, Path]:
    """Build a fake checkout and a fake ~/.config/systemd/user."""
    repo_root = tmp_path / "checkout"
    repo_units = repo_root / "scripts" / "systemd"
    repo_units.mkdir(parents=True)
    for name, body in repo.items():
        (repo_units / name).write_text(body)

    units_dir = tmp_path / "systemd-user"
    units_dir.mkdir()
    for name, body in installed.items():
        (units_dir / name).write_text(body)
    for name in enabled:
        # A unit enables into `<its WantedBy target>.wants`, so a `.path`
        # lands in `paths.target.wants` and a `.timer` in
        # `timers.target.wants`. Read the target from the unit itself with
        # the checker's own parser rather than assuming `timers.target`.
        from scripts.check_unit_drift import parse_wanted_by

        targets = parse_wanted_by(units_dir / name) or ["timers.target"]
        wants = units_dir / f"{targets[0]}.wants"
        wants.mkdir(exist_ok=True)
        (wants / name).symlink_to(units_dir / name)
    return repo_root, units_dir


def _report(tmp_path, repo, installed, enabled=()):
    from scripts.check_unit_drift import build_report

    repo_root, units_dir = _make_box(tmp_path, repo, installed, enabled)
    return build_report(str(repo_root), str(units_dir))


def test_identical_units_are_in_sync(tmp_path):
    report = _report(
        tmp_path,
        {"a.service": UNIT_BODY, "a.timer": TIMER_BODY},
        {"a.service": UNIT_BODY, "a.timer": TIMER_BODY},
        enabled=("a.timer",),
    )
    assert report.has_drift is False
    assert report.untracked == []
    assert report.modified == []
    assert report.undeployed == []
    assert report.not_enabled == []


def test_a_unit_hand_added_on_the_box_is_reported_as_untracked(tmp_path):
    """THE CASE THAT MATTERS. A unit created directly in
    ~/.config/systemd/user is invisible to every commit-based check this
    desk runs: the checkout can sit exactly on origin/main while systemd
    runs something nobody has ever reviewed."""
    report = _report(
        tmp_path,
        {"a.service": UNIT_BODY},
        {"a.service": UNIT_BODY, "rogue.service": UNIT_BODY},
    )
    from scripts.check_unit_drift import format_alert

    assert report.untracked == ["rogue.service"]
    assert report.has_drift is True
    assert "rogue.service" in format_alert(report)


def test_a_unit_edited_in_place_on_the_box_is_reported_as_modified(tmp_path):
    report = _report(
        tmp_path,
        {"a.service": UNIT_BODY},
        {"a.service": UNIT_BODY.replace("oneshot", "simple")},
    )
    assert report.modified == ["a.service"]
    assert report.has_drift is True


def test_a_comment_only_change_still_counts_as_drift(tmp_path):
    """Byte-for-byte, deliberately. Deploys here are a literal `cp`, and a
    box copy whose rationale comments no longer match the repository is a
    copy nobody can reason about."""
    report = _report(
        tmp_path,
        {"a.service": "# why this exists\n" + UNIT_BODY},
        {"a.service": UNIT_BODY},
    )
    assert report.modified == ["a.service"]


def test_a_tracked_unit_never_copied_to_the_box_is_reported(tmp_path):
    report = _report(
        tmp_path,
        {"a.service": UNIT_BODY, "b.service": UNIT_BODY},
        {"a.service": UNIT_BODY},
    )
    assert report.undeployed == ["b.service"]
    assert report.has_drift is True


def test_an_installed_but_unenabled_timer_is_reported(tmp_path):
    """The quietest failure of the four: the unit is present, correct, and
    identical to the repository — and never fires, because nothing linked it
    into timers.target.wants."""
    report = _report(
        tmp_path,
        {"a.service": UNIT_BODY, "a.timer": TIMER_BODY},
        {"a.service": UNIT_BODY, "a.timer": TIMER_BODY},
        enabled=(),
    )
    assert report.not_enabled == [("a.timer", "timers.target")]
    assert report.has_drift is True


def test_enablement_is_not_reported_for_units_that_are_not_installed(tmp_path):
    """An undeployed unit is already one finding. Reporting it a second time
    as 'not enabled' is the same fact twice, which is how an operator learns
    to skim."""
    report = _report(
        tmp_path,
        {"a.timer": TIMER_BODY},
        {},
    )
    assert report.undeployed == ["a.timer"]
    assert report.not_enabled == []


def test_a_oneshot_service_with_no_install_section_is_not_expected_enabled(
    tmp_path,
):
    """The six session services are started by their timers and declare no
    `[Install]`. Demanding a `.wants` link for them would alarm forever."""
    report = _report(
        tmp_path,
        {"a.service": UNIT_BODY},
        {"a.service": UNIT_BODY},
    )
    assert report.not_enabled == []
    assert report.has_drift is False


def test_a_dangling_wants_symlink_reads_as_not_enabled(tmp_path):
    from scripts.check_unit_drift import build_report

    repo_root, units_dir = _make_box(
        tmp_path,
        {"a.timer": TIMER_BODY},
        {"a.timer": TIMER_BODY},
    )
    wants = units_dir / "timers.target.wants"
    wants.mkdir()
    (wants / "a.timer").symlink_to(units_dir / "gone.timer")
    report = build_report(str(repo_root), str(units_dir))
    assert report.not_enabled == [("a.timer", "timers.target")]


# --- .path units (item 123) -------------------------------------------------
# A tracked `.path` unit (quant-agent-status-board.path) replaces the
# status-board timer. Before item 123 the checker's suffix set was
# (".service", ".timer") and every bucket keyed off it, so a `.path` unit
# could drift on the box — untracked, modified, undeployed or not-enabled —
# and no bucket would ever catch it. These four cases prove each bucket now
# sees it, and that its `paths.target` enablement is handled like a timer's.


def test_a_tracked_path_unit_edited_in_place_is_reported_as_modified(tmp_path):
    """Was invisible before item 123: `.path` was not in the suffix set, so
    the modified bucket never compared its bytes."""
    report = _report(
        tmp_path,
        {"a.service": UNIT_BODY, "a.path": PATH_BODY},
        {"a.service": UNIT_BODY, "a.path": PATH_BODY.replace("docs/WORK.md", "docs/OTHER.md")},
    )
    assert report.modified == ["a.path"]
    assert report.has_drift is True


def test_a_tracked_path_unit_never_copied_to_the_box_is_reported(tmp_path):
    report = _report(
        tmp_path,
        {"a.service": UNIT_BODY, "a.path": PATH_BODY},
        {"a.service": UNIT_BODY},
    )
    assert report.undeployed == ["a.path"]
    assert report.has_drift is True


def test_an_installed_but_unenabled_path_unit_is_reported(tmp_path):
    """The quiet failure for a `.path` unit: present, correct, byte-identical
    — and never firing, because nothing linked it into paths.target.wants.
    It enables through paths.target, not timers.target."""
    report = _report(
        tmp_path,
        {"a.service": UNIT_BODY, "a.path": PATH_BODY},
        {"a.service": UNIT_BODY, "a.path": PATH_BODY},
        enabled=(),
    )
    assert report.not_enabled == [("a.path", "paths.target")]
    assert report.has_drift is True


def test_an_enabled_identical_path_unit_is_in_sync(tmp_path):
    """The clean case, and the one that proves `enabled=` routes a `.path`
    into paths.target.wants: an installed, byte-identical, enabled `.path`
    unit is not drift."""
    report = _report(
        tmp_path,
        {"a.service": UNIT_BODY, "a.path": PATH_BODY},
        {"a.service": UNIT_BODY, "a.path": PATH_BODY},
        enabled=("a.path",),
    )
    assert report.not_enabled == []
    assert report.modified == []
    assert report.undeployed == []
    assert report.has_drift is False


def test_a_path_unit_hand_added_on_the_box_is_reported_as_untracked(tmp_path):
    report = _report(
        tmp_path,
        {"a.service": UNIT_BODY},
        {"a.service": UNIT_BODY, "rogue.path": PATH_BODY},
    )
    assert report.untracked == ["rogue.path"]
    assert report.has_drift is True


# --- suffix derivation, not a hand-maintained tuple (item 123) -------------
# Before item 123's second half, the four buckets above only ever saw
# `.service`/`.timer`/`.path` because `UNIT_SUFFIXES` was a hardcoded tuple
# `list_units` filtered by. A newly-tracked unit type (a `.socket`, say)
# would have been silently invisible to every bucket until someone
# remembered to edit that tuple by hand. These prove the suffix set is now
# DERIVED from the files actually tracked in scripts/systemd/, so a new
# unit type is picked up with no code change.

SOCKET_BODY = """\
[Unit]
Description=example socket

[Socket]
ListenStream=/run/example.sock

[Install]
WantedBy=sockets.target
"""


def test_a_newly_tracked_unit_type_is_picked_up_without_touching_the_tuple(
    tmp_path,
):
    """`.socket` is not in `UNIT_SUFFIXES` and this test never edits it --
    if the suffix set were still hand-maintained, a.socket would be
    invisible to every bucket. It is not: tracking it makes the box's
    missing copy an `undeployed` finding automatically."""
    from scripts.check_unit_drift import UNIT_SUFFIXES

    assert ".socket" not in UNIT_SUFFIXES
    report = _report(
        tmp_path,
        {"a.service": UNIT_BODY, "a.socket": SOCKET_BODY},
        {"a.service": UNIT_BODY},
    )
    assert report.undeployed == ["a.socket"]
    assert report.has_drift is True


def test_an_installed_but_untracked_new_suffix_type_is_reported_as_untracked(
    tmp_path,
):
    """The mirror case: a `.socket` hand-added on the box, with no `.socket`
    ever tracked in the repository, must still be caught -- the derivation
    is bounded by real systemd unit types, not just by what happens to be
    on disk already."""
    report = _report(
        tmp_path,
        {"a.service": UNIT_BODY},
        {"a.service": UNIT_BODY, "rogue.socket": SOCKET_BODY},
    )
    assert report.untracked == ["rogue.socket"]
    assert report.has_drift is True


def test_a_non_unit_file_in_the_repo_directory_is_never_treated_as_a_unit(
    tmp_path,
):
    """`paused_units.yaml` lives in the same directory as the units it
    describes. Deriving suffixes from "whatever is in this directory"
    without bounding to real systemd unit types would make `.yaml` a
    tracked "unit type" and misreport every install that lacks one."""
    report = _report(
        tmp_path,
        {"a.service": UNIT_BODY, "paused_units.yaml": "paused_units: []\n"},
        {"a.service": UNIT_BODY},
    )
    assert report.repo_units == ["a.service"]
    assert report.has_drift is False


def test_suffixes_are_derived_not_read_from_the_hand_tuple(tmp_path):
    """Direct proof at the function level: `observed_unit_suffixes` reads
    the directories, not `UNIT_SUFFIXES`."""
    from scripts.check_unit_drift import observed_unit_suffixes

    repo_root, units_dir = _make_box(
        tmp_path,
        {"a.service": UNIT_BODY, "a.socket": SOCKET_BODY},
        {},
    )
    suffixes = observed_unit_suffixes(repo_root / "scripts" / "systemd", units_dir)
    assert suffixes == (".service", ".socket")


def test_observed_suffixes_include_a_type_seen_only_on_the_box(tmp_path):
    """The union, not just the repo side: a unit type that exists only
    installed on the box (never tracked) must still be observed, or it
    could never be reported `untracked`."""
    from scripts.check_unit_drift import observed_unit_suffixes

    repo_root, units_dir = _make_box(
        tmp_path,
        {"a.service": UNIT_BODY},
        {"a.service": UNIT_BODY, "rogue.socket": SOCKET_BODY},
    )
    suffixes = observed_unit_suffixes(repo_root / "scripts" / "systemd", units_dir)
    assert suffixes == (".service", ".socket")


def test_the_hand_maintained_tuple_still_agrees_with_the_real_repository():
    """`UNIT_SUFFIXES` is now only a sanity backstop, not the source of
    truth -- but it must still describe reality. This runs against the
    real, live scripts/systemd/, not a fixture: if it ever drifts from what
    is actually tracked, that is worth a human noticing, not a silent
    behaviour change hidden behind the derived set."""
    from scripts.check_unit_drift import UNIT_SUFFIXES, observed_unit_suffixes

    assert set(observed_unit_suffixes(SYSTEMD_DIR)) == set(UNIT_SUFFIXES)


def test_wants_directories_are_not_mistaken_for_units(tmp_path):
    """`timers.target.wants` and the symlinks inside it live in the same
    directory as the units. Counting a symlink as an installed unit would
    report permanent phantom drift."""
    report = _report(
        tmp_path,
        {"a.timer": TIMER_BODY},
        {"a.timer": TIMER_BODY},
        enabled=("a.timer",),
    )
    assert report.installed_units == ["a.timer"]
    assert report.has_drift is False


def test_non_unit_files_are_ignored(tmp_path):
    report = _report(
        tmp_path,
        {"a.service": UNIT_BODY, "README.md": "notes"},
        {"a.service": UNIT_BODY, "a.service.bak": UNIT_BODY},
    )
    assert report.repo_units == ["a.service"]
    assert report.installed_units == ["a.service"]
    assert report.has_drift is False


# --- exit codes and the alert path -----------------------------------------


def test_missing_systemd_directory_is_an_operator_problem_not_drift(tmp_path):
    from scripts.check_unit_drift import build_report, main

    repo_root, _ = _make_box(tmp_path, {"a.service": UNIT_BODY}, {})
    report = build_report(str(repo_root), str(tmp_path / "nope"))
    assert report.checked is False
    assert report.has_drift is False
    assert (
        main(
            [
                "--repo-path",
                str(repo_root),
                "--units-path",
                str(tmp_path / "nope"),
                "--no-telegram",
            ]
        )
        == 3
    )


def test_missing_repo_unit_directory_is_an_operator_problem(tmp_path):
    from scripts.check_unit_drift import build_report

    report = build_report(str(tmp_path / "nope"), str(tmp_path))
    assert report.checked is False
    assert "repo unit directory not found" in report.error


def test_main_exits_zero_when_in_sync(tmp_path, capsys):
    from scripts.check_unit_drift import main

    repo_root, units_dir = _make_box(
        tmp_path,
        {"a.service": UNIT_BODY},
        {"a.service": UNIT_BODY},
    )
    code = main(
        [
            "--repo-path",
            str(repo_root),
            "--units-path",
            str(units_dir),
            "--no-telegram",
        ]
    )
    assert code == 0
    assert "in sync" in capsys.readouterr().out


def test_main_exits_one_and_names_every_bucket_on_drift(tmp_path, capsys):
    from scripts.check_unit_drift import main

    repo_root, units_dir = _make_box(
        tmp_path,
        {"a.service": UNIT_BODY, "gone.service": UNIT_BODY},
        {"a.service": UNIT_BODY.replace("oneshot", "simple"), "rogue.service": UNIT_BODY},
    )
    code = main(
        [
            "--repo-path",
            str(repo_root),
            "--units-path",
            str(units_dir),
            "--no-telegram",
        ]
    )
    out = capsys.readouterr().out
    assert code == 1
    assert "rogue.service" in out
    assert "a.service" in out
    assert "gone.service" in out
    # The operator must be told what to do about it.
    assert "cp scripts/systemd/*" in out


def _write_paused(repo_root: Path, units: tuple[str, ...]) -> None:
    """Write a minimal `paused_units.yaml` naming `units`, in the same shape
    as the real seed file at `scripts/systemd/paused_units.yaml`."""
    lines = ["paused_units:"]
    for name in units:
        lines.append(f"  - unit: {name}")
        lines.append('    since: "2026-09-14"')
        lines.append('    reason: "test fixture"')
    (repo_root / "scripts" / "systemd" / "paused_units.yaml").write_text("\n".join(lines) + "\n")


def test_a_paused_unlisted_timer_is_not_reported_and_alarms_nothing(tmp_path):
    """PAUSED + NOT ENABLED — the case the whole feature exists for. A
    timer named on the paused list, installed but not enabled, must not be
    counted as drift and must not exit non-zero."""
    repo_root, units_dir = _make_box(
        tmp_path,
        {"a.service": UNIT_BODY, "a.timer": TIMER_BODY},
        {"a.service": UNIT_BODY, "a.timer": TIMER_BODY},
        enabled=(),
    )
    _write_paused(repo_root, ("a.timer",))
    from scripts.check_unit_drift import build_report, format_alert, format_ok

    report = build_report(str(repo_root), str(units_dir))
    assert report.not_enabled == []
    assert report.paused == [("a.timer", "timers.target")]
    assert report.has_drift is False
    rendered = format_ok(report)
    assert "Deliberately paused (not an alarm):" in rendered
    assert "a.timer -> timers.target" in rendered
    # format_alert must never be reached on a clean run — assert its own
    # rendering separately, so a change to has_drift's wiring cannot hide
    # a broken template behind an untested code path.
    assert "Deliberately paused (not an alarm):" in format_alert(report)


def test_a_paused_timer_that_was_re_enabled_is_reported_as_drift(tmp_path):
    """PAUSED + ENABLED — someone re-enabled the unit without updating the
    list. The list and the box disagree, and that disagreement is itself
    the finding."""
    repo_root, units_dir = _make_box(
        tmp_path,
        {"a.service": UNIT_BODY, "a.timer": TIMER_BODY},
        {"a.service": UNIT_BODY, "a.timer": TIMER_BODY},
        enabled=("a.timer",),
    )
    _write_paused(repo_root, ("a.timer",))
    from scripts.check_unit_drift import build_report, format_alert

    report = build_report(str(repo_root), str(units_dir))
    assert report.paused == []
    assert report.paused_but_enabled == [("a.timer", "timers.target")]
    assert report.has_drift is True
    assert "a.timer" in format_alert(report)


def test_an_unlisted_unenabled_timer_still_alarms_as_before(tmp_path):
    """UNLISTED + NOT ENABLED — the ordinary drift case, unaffected by the
    paused list existing at all (here the list is empty)."""
    repo_root, units_dir = _make_box(
        tmp_path,
        {"a.service": UNIT_BODY, "a.timer": TIMER_BODY},
        {"a.service": UNIT_BODY, "a.timer": TIMER_BODY},
        enabled=(),
    )
    _write_paused(repo_root, ())
    from scripts.check_unit_drift import build_report

    report = build_report(str(repo_root), str(units_dir))
    assert report.not_enabled == [("a.timer", "timers.target")]
    assert report.paused == []
    assert report.has_drift is True


def test_a_paused_list_entry_naming_an_untracked_unit_is_reported(tmp_path):
    """An error IN THE LIST, not on the box: the paused list names a unit
    that scripts/systemd/ does not track at all. Silently ignoring it would
    let the list rot exactly the way the drift check itself was built to
    stop the box from rotting."""
    repo_root, units_dir = _make_box(
        tmp_path,
        {"a.service": UNIT_BODY, "a.timer": TIMER_BODY},
        {"a.service": UNIT_BODY, "a.timer": TIMER_BODY},
        enabled=("a.timer",),
    )
    _write_paused(repo_root, ("nonexistent.timer",))
    from scripts.check_unit_drift import build_report, format_alert

    report = build_report(str(repo_root), str(units_dir))
    assert report.paused_unknown == ["nonexistent.timer"]
    assert report.has_drift is True
    assert "nonexistent.timer" in format_alert(report)


def test_no_paused_units_file_means_nothing_is_paused(tmp_path):
    """The common case on a fresh checkout of a box with nothing paused:
    absence of the file is not an error."""
    report = _report(
        tmp_path,
        {"a.service": UNIT_BODY, "a.timer": TIMER_BODY},
        {"a.service": UNIT_BODY, "a.timer": TIMER_BODY},
        enabled=("a.timer",),
    )
    assert report.paused == []
    assert report.paused_but_enabled == []
    assert report.paused_unknown == []
    assert report.paused_list_error is None
    assert report.has_drift is False


def test_the_seeded_paused_list_only_names_units_tracked_in_the_repo():
    """The real seed file, not a fixture: every unit it names must actually
    exist in scripts/systemd/, or the drift check would report the desk's
    own paused-unit bookkeeping as broken on day one.

    An empty list is valid — that is the fully-running desk. The file must
    still exist and parse as a list; any name that IS present must be
    tracked.
    """
    import yaml

    from scripts.check_unit_drift import PAUSED_UNITS_FILENAME

    seed = SYSTEMD_DIR / PAUSED_UNITS_FILENAME
    assert seed.is_file(), f"{seed} is missing"
    data = yaml.safe_load(seed.read_text()) or {}
    entries = data.get("paused_units") or []
    assert isinstance(entries, list), (
        "paused_units.yaml must declare paused_units as a list (empty means the desk is fully running)"
    )
    tracked = {p.name for p in ALL_UNITS}
    for entry in entries:
        name = entry["unit"]
        assert name in tracked, f"paused_units.yaml names {name!r}, which is not tracked in {SYSTEMD_DIR}"


def test_the_alert_is_pushed_through_the_existing_notifier(tmp_path):
    """Same `TelegramNotifier` as check_deploy_drift.py and the cost
    circuit. No new sender, no new credential path."""
    from scripts.check_unit_drift import main

    repo_root, units_dir = _make_box(
        tmp_path,
        {},
        {"rogue.service": UNIT_BODY},
    )
    notifier = MagicMock()
    notifier.enabled = True
    with patch("src.notifier.TelegramNotifier", return_value=notifier):
        code = main(
            [
                "--repo-path",
                str(repo_root),
                "--units-path",
                str(units_dir),
            ]
        )
    assert code == 1
    notifier.send.assert_called_once()
    assert "rogue.service" in notifier.send.call_args[0][0]


def test_no_alert_is_pushed_when_in_sync(tmp_path):
    from scripts.check_unit_drift import main

    repo_root, units_dir = _make_box(
        tmp_path,
        {"a.service": UNIT_BODY},
        {"a.service": UNIT_BODY},
    )
    notifier = MagicMock()
    notifier.enabled = True
    with patch("src.notifier.TelegramNotifier", return_value=notifier):
        assert (
            main(
                [
                    "--repo-path",
                    str(repo_root),
                    "--units-path",
                    str(units_dir),
                ]
            )
            == 0
        )
    notifier.send.assert_not_called()


def test_a_disabled_notifier_degrades_to_printing(tmp_path, capsys):
    """It must still exit 1. The finding is real whether or not the desk can
    currently shout about it."""
    from scripts.check_unit_drift import main

    repo_root, units_dir = _make_box(
        tmp_path,
        {},
        {"rogue.service": UNIT_BODY},
    )
    notifier = MagicMock()
    notifier.enabled = False
    with patch("src.notifier.TelegramNotifier", return_value=notifier):
        code = main(
            [
                "--repo-path",
                str(repo_root),
                "--units-path",
                str(units_dir),
            ]
        )
    assert code == 1
    notifier.send.assert_not_called()
    assert "Telegram not configured" in capsys.readouterr().err


# --- the unit that schedules the check -------------------------------------


def test_the_unit_drift_check_is_itself_scheduled_on_the_box():
    """A check nobody runs is a check that does not exist."""
    service = SYSTEMD_DIR / "quant-agent-unit-drift.service"
    timer = SYSTEMD_DIR / "quant-agent-unit-drift.timer"
    assert service.is_file() and timer.is_file()
    parsed = parse_unit(service)
    # Exit 1 is a finding, not a crash — same contract as the deploy-drift
    # unit. Exit 3 (missing directory) must still mark the unit failed.
    assert parsed["Service.SuccessExitStatus"] == ["0 1"]
    assert parsed["Service.ExecStart"] == [f"{DEPLOY_ROOT}/scripts/run_unit_drift_check.sh"]


def test_the_unit_drift_check_runs_every_day_not_only_weekdays():
    """Units get hand-added at weekends too, and this desk's recorded blind
    spot is precisely the weekend (the pricing cache went stale over one).
    The check costs no network and no model call."""
    on_calendar = parse_unit(SYSTEMD_DIR / "quant-agent-unit-drift.timer")["Timer.OnCalendar"]
    assert on_calendar == ["*-*-* 08:50 America/New_York"]
    assert not any("Mon" in entry for entry in on_calendar)


def test_the_unit_drift_wrapper_sources_env_and_is_executable():
    """Redundant with the parametrized gate in test_alert_heartbeat.py, and
    kept anyway: that gate proves the rule holds across all units, this
    names the file when it is this one that broke."""
    wrapper = SCRIPTS_DIR / "run_unit_drift_check.sh"
    assert wrapper.is_file()
    assert 'source "${PROJECT_ROOT}/.env"' in wrapper.read_text()
    assert wrapper.stat().st_mode & stat.S_IXUSR


# ===========================================================================
# 4. The 2026-09-17 defect — six timers on one tick
# ===========================================================================
#
# All six session timers carried OnCalendar=*:0/30, so every one fired in
# the same second, every half hour. Measured that day: morning and
# intra_check raced at 09:30, and a session's own stop-coverage reconcile
# and intra_check's own stop-coverage reconcile ran ~90ms apart at 17:00:42
# UTC. intra_check moved to *:15,45 — still a 30-minute cadence, inside the
# same ET window, just off the shared tick. Nothing else moved.

NON_INTRA_SESSION_MODES = tuple(m for m in SESSION_MODES if m != "intra_check")


def _fire_minutes(on_calendar: str) -> set[int]:
    """Minutes-past-the-hour an OnCalendar value fires on, for the two
    shapes this repository's session timers use (`*:MM/STEP` and
    `*:MM,MM`). Not a general OnCalendar parser — just enough to prove two
    timers never share a tick.
    """
    assert on_calendar.startswith("*:"), f"unrecognised OnCalendar shape for this helper: {on_calendar!r}"
    spec = on_calendar[2:]
    if "/" in spec:
        start, step = spec.split("/")
        return set(range(int(start), 60, int(step)))
    return {int(part) for part in spec.split(",")}


def test_fire_minutes_helper_matches_systemd_on_the_two_shapes_in_use():
    """Pins the test helper itself against the two literal strings the repo
    uses, so a mistake in `_fire_minutes` fails loudly here rather than by
    silently passing the disjointness tests below for the wrong reason."""
    assert _fire_minutes("*:0/30") == {0, 30}
    assert _fire_minutes("*:15,45") == {15, 45}


def test_intra_check_timer_moved_off_the_shared_tick():
    on_calendar = parse_unit(SYSTEMD_DIR / "quant-agent-intra_check.timer")["Timer.OnCalendar"]
    assert on_calendar == ["*:15,45"], (
        "intra_check must fire at :15/:45 — off the :00/:30 tick every other session timer shares"
    )


@pytest.mark.parametrize("mode", NON_INTRA_SESSION_MODES)
def test_the_other_session_timers_keep_the_shared_half_hour_tick(mode: str):
    """Only intra_check moves. The rest still fire together and rely on
    run_if_et_window.sh's cross-mode session lock to serialize, exactly as
    before."""
    on_calendar = parse_unit(SYSTEMD_DIR / f"quant-agent-{mode}.timer")["Timer.OnCalendar"]
    assert on_calendar == ["*:0/30"]


def test_coverage_sweep_timer_also_keeps_the_shared_tick():
    """This unit is not a session mode and is not a party to
    run_if_et_window.sh's session lock at all, so moving intra_check off
    the shared tick does not touch its collision with morning/midday/
    close/evening/earnings_preprocess — that is fixed instead by
    `trading_session_lock_held()` in src/coverage_watchdog.py (see
    tests/test_coverage_watchdog.py), not by a schedule change.
    """
    on_calendar = parse_unit(SYSTEMD_DIR / "quant-agent-coverage-sweep.timer")["Timer.OnCalendar"]
    assert on_calendar == ["*:0/30"]


def test_morning_and_intra_check_can_no_longer_land_on_the_same_tick():
    """THE 2026-09-17 DEFECT, pinned by name: this exact pair raced at
    09:30 because both fired on *:0/30."""
    morning = _fire_minutes(parse_unit(SYSTEMD_DIR / "quant-agent-morning.timer")["Timer.OnCalendar"][0])
    intra = _fire_minutes(parse_unit(SYSTEMD_DIR / "quant-agent-intra_check.timer")["Timer.OnCalendar"][0])
    assert morning.isdisjoint(intra)


@pytest.mark.parametrize("mode", NON_INTRA_SESSION_MODES)
def test_intra_check_shares_no_tick_with_any_other_session_timer(mode: str):
    intra = _fire_minutes(parse_unit(SYSTEMD_DIR / "quant-agent-intra_check.timer")["Timer.OnCalendar"][0])
    other = _fire_minutes(parse_unit(SYSTEMD_DIR / f"quant-agent-{mode}.timer")["Timer.OnCalendar"][0])
    assert intra.isdisjoint(other), f"intra_check and {mode} still share a tick — the exact 2026-09-17 defect"


# === Board item 177, the CADENCE third ================================
#
# The cadence decides how many PAID intraday ticks there can be, and it is
# the one part of item 177 with three separate definition sites:
#
#   1. `src/config.INTRA_CHECK_TICK_MINUTES` — the ledgered constant the
#      cost circuit derives its transient-latch cooldown from.
#   2. `src/scheduler.TradingScheduler._build_intra_check_trigger` — the
#      APScheduler trigger, used by `--mode live` only.
#   3. `scripts/systemd/quant-agent-intra_check.timer` + the ET window in
#      `scripts/run_if_et_window.sh` — WHAT PRODUCTION ACTUALLY RUNS.
#
# Sites 1 and 2 were already pinned to each other
# (`tests/test_cost_circuit.py::
# test_the_tick_spacing_this_module_pins_matches_the_scheduler`). Site 3
# was pinned only for its MINUTES (`tests/test_trader_feed.py`, the hourly
# checkpoint), never for its SPACING — so the production cadence could be
# halved or doubled, the cost circuit's cooldown would go on being derived
# from a constant that no longer described production, and nothing would
# fail. That is the gap these tests close; they change no schedule.
#
# Verified against the box on 2026-09-26: `systemctl --user list-timers`
# shows quant-agent-intra_check.timer last fired 06:45 and next fires
# 07:15, i.e. production runs the timer, not the APScheduler trigger.


def _intra_check_et_window_minutes() -> tuple[int, int]:
    """The intra_check ET window the wrapper enforces on a normal day. The
    wrapper holds no minutes of its own; it asks `src.session_calendar`, whose
    normal-day table is derived from the one set of phase offsets."""
    from src.trading_calendar import SESSION_WINDOWS

    return SESSION_WINDOWS["intra_check"]


def _cyclic_gaps(minutes: set[int]) -> set[int]:
    ordered = sorted(minutes)
    return {(ordered[(i + 1) % len(ordered)] - m) % 60 for i, m in enumerate(ordered)}


def test_the_production_timer_spacing_is_the_ledgered_cadence_constant():
    """The gap between two consecutive PRODUCTION intraday ticks must be
    exactly the constant the cost circuit reasons about. Until this test
    the constant was pinned only to the APScheduler trigger, which
    production does not use."""
    from src.config import INTRA_CHECK_TICK_MINUTES

    minutes = _fire_minutes(parse_unit(SYSTEMD_DIR / "quant-agent-intra_check.timer")["Timer.OnCalendar"][0])
    assert _cyclic_gaps(minutes) == {INTRA_CHECK_TICK_MINUTES}, (
        "quant-agent-intra_check.timer no longer ticks every "
        f"{INTRA_CHECK_TICK_MINUTES} minutes — the cost circuit's "
        "cooldown and every per-tick spend estimate are derived from that "
        "spacing"
    )


def test_the_production_and_live_mode_cadences_cannot_silently_diverge():
    """`--mode live` and the box must agree on how often the paid tick
    fires. They deliberately land on different minutes (:15/:45 vs
    :00/:30, the 2026-09-17 fix); they must not disagree on the SPACING,
    because that is what decides how many paid ticks a day can hold."""
    from src.scheduler import TradingScheduler

    timer_gaps = _cyclic_gaps(
        _fire_minutes(parse_unit(SYSTEMD_DIR / "quant-agent-intra_check.timer")["Timer.OnCalendar"][0])
    )

    def _field(trigger, name: str) -> int:
        return int(str(next(f for f in trigger.fields if f.name == name)))

    fired = sorted(
        _field(t, "hour") * 60 + _field(t, "minute") for t in TradingScheduler._build_intra_check_trigger().triggers
    )
    scheduler_gaps = {b - a for a, b in zip(fired, fired[1:])}
    assert scheduler_gaps == timer_gaps, (
        "the live-mode trigger and the production timer now tick at "
        f"different spacings ({scheduler_gaps} vs {timer_gaps})"
    )


def test_the_number_of_paid_intraday_ticks_a_day_is_derived_not_asserted():
    """How many paid ticks a day can exist is a spending fact (board item
    177). It is the product of the timer's minutes and the wrapper's ET
    window, and nothing anywhere computed it. Measured against the desk's
    own cost circuit on 2026-09-26: exactly 13 intra_check sessions were
    recorded on each of 2026-09-21..25, the days since the timer moved to
    :15/:45, and 14 on each day before it — both reproduced here from the
    two files rather than typed in."""
    lo, hi = _intra_check_et_window_minutes()
    minutes = _fire_minutes(parse_unit(SYSTEMD_DIR / "quant-agent-intra_check.timer")["Timer.OnCalendar"][0])
    ticks = [m for m in range(lo, hi + 1) if m % 60 in minutes]
    assert ticks, "the intra_check timer fires nowhere inside its own window"
    assert len(ticks) == 13, (
        f"the production intraday cadence now allows {len(ticks)} paid "
        "ticks a day, not the 13 the desk's recorded spend was measured "
        "on — re-measure the per-day cost before landing this"
    )
    # The wrapper's own comment quotes a tick count to the operator; it must
    # not outlive the schedule it describes.
    text = (SCRIPTS_DIR / "run_if_et_window.sh").read_text()
    quoted = re.search(r"intra_check's ~(\d+) OK ticks/day", text)
    assert quoted, "the wrapper no longer states its tick count"
    assert int(quoted.group(1)) == len(ticks), (
        "run_if_et_window.sh tells the operator a tick count the schedule no longer produces"
    )
