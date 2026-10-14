import os
import subprocess
import sys
from pathlib import Path

import pytest

from src.trading_calendar import SESSION_WINDOWS

ROOT = Path(__file__).resolve().parents[1]
WRAPPER_NAME = "run_if_et_window.sh"


@pytest.fixture(autouse=True)
def _normal_day_window_unless_the_test_sets_one(monkeypatch):
    """Give every wrapper launch a normal-day window, without a broker.

    The wrapper no longer holds a window table: it asks `python -m
    src.session_calendar` for the day's window. These tests run a fake python
    and have no broker, so each launch gets `SESSION_WINDOW_OVERRIDE` set to the
    derived normal-day minutes for its mode (intra_safety shares intra_check's).
    A test that sets the variable itself, even to an empty string, is left alone.
    """
    real_run = subprocess.run

    def run(cmd, *args, env=None, **kwargs):
        if env is not None and len(cmd) >= 3 and str(cmd[1]).endswith(WRAPPER_NAME):
            mode = "intra_check" if cmd[2] == "intra_safety" else cmd[2]
            if "SESSION_WINDOW_OVERRIDE" not in env and mode in SESSION_WINDOWS:
                lo, hi = SESSION_WINDOWS[mode]
                env = env | {"SESSION_WINDOW_OVERRIDE": f"{lo} {hi}"}
        return real_run(cmd, *args, env=env, **kwargs)

    monkeypatch.setattr(subprocess, "run", run)


def _write_executable(path: Path, content: str) -> None:
    path.write_text(content)
    path.chmod(0o755)


def test_run_if_et_window_only_records_successful_runs(tmp_path):
    script = Path(__file__).resolve().parents[1] / "scripts" / "run_if_et_window.sh"
    project_root = tmp_path / "project"
    project_root.mkdir()
    (project_root / ".env").write_text("")

    last_run_dir = tmp_path / "cache"
    timeout_bin = tmp_path / "timeout"
    python_bin = tmp_path / "fake-python"

    _write_executable(
        timeout_bin,
        '#!/bin/bash\nshift 2\nexec "$@"\n',
    )
    _write_executable(
        python_bin,
        "#!/bin/bash\nexit 1\n",
    )

    env = os.environ | {
        "PROJECT_ROOT_OVERRIDE": str(project_root),
        "PYTHON_OVERRIDE": str(python_bin),
        "TIMEOUT_OVERRIDE": str(timeout_bin),
        "LAST_RUN_DIR_OVERRIDE": str(last_run_dir),
        "ET_DOW_OVERRIDE": "1",
        "ET_HOUR_OVERRIDE": "08",
        "ET_MIN_OVERRIDE": "30",
        "ET_DATE_OVERRIDE": "2026-04-17",
        "NOW_UNIX_OVERRIDE": "1234567890",
    }

    failed = subprocess.run(
        ["bash", str(script), "earnings_preprocess"],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert failed.returncode == 1
    assert not (last_run_dir / "last-earnings_preprocess").exists()

    _write_executable(
        python_bin,
        "#!/bin/bash\nexit 0\n",
    )
    succeeded = subprocess.run(
        ["bash", str(script), "earnings_preprocess"],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert succeeded.returncode == 0
    assert (last_run_dir / "last-earnings_preprocess").read_text().strip() == "2026-04-17 1234567890"


def test_run_if_et_window_fires_once_per_et_session_date(tmp_path):
    script = Path(__file__).resolve().parents[1] / "scripts" / "run_if_et_window.sh"
    project_root = tmp_path / "project"
    project_root.mkdir()
    (project_root / ".env").write_text("")

    last_run_dir = tmp_path / "cache"
    timeout_bin = tmp_path / "timeout"
    python_bin = tmp_path / "fake-python"
    counter_file = tmp_path / "invocations.txt"

    _write_executable(
        timeout_bin,
        '#!/bin/bash\nshift 2\nexec "$@"\n',
    )
    _write_executable(
        python_bin,
        f'#!/bin/bash\necho run >> "{counter_file}"\nexit 0\n',
    )

    base_env = os.environ | {
        "PROJECT_ROOT_OVERRIDE": str(project_root),
        "PYTHON_OVERRIDE": str(python_bin),
        "TIMEOUT_OVERRIDE": str(timeout_bin),
        "LAST_RUN_DIR_OVERRIDE": str(last_run_dir),
        "ET_DOW_OVERRIDE": "1",
        "ET_DATE_OVERRIDE": "2026-04-17",
    }

    first = subprocess.run(
        ["bash", str(script), "morning"],
        env=base_env
        | {
            "ET_HOUR_OVERRIDE": "09",
            "ET_MIN_OVERRIDE": "30",
            "NOW_UNIX_OVERRIDE": "1234567890",
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert first.returncode == 0

    second = subprocess.run(
        ["bash", str(script), "morning"],
        env=base_env
        | {
            "ET_HOUR_OVERRIDE": "11",
            "ET_MIN_OVERRIDE": "00",
            "NOW_UNIX_OVERRIDE": "1234573290",
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert second.returncode == 0
    assert counter_file.read_text().splitlines() == ["run"]


def test_operator_rerun_bypasses_only_same_day_morning_guard(tmp_path):
    script = Path(__file__).resolve().parents[1] / "scripts" / "run_if_et_window.sh"
    project_root = tmp_path / "project"
    project_root.mkdir()
    (project_root / ".env").write_text("")
    last_run_dir = tmp_path / "cache"
    last_run_dir.mkdir()
    (last_run_dir / "last-morning").write_text("2026-08-26 1000\n")
    counter_file = tmp_path / "operator-rerun.txt"
    timeout_bin = tmp_path / "timeout"
    python_bin = tmp_path / "fake-python"
    _write_executable(timeout_bin, '#!/bin/bash\nshift 2\nexec "$@"\n')
    _write_executable(
        python_bin,
        f'#!/bin/bash\necho "$@" >> "{counter_file}"\nexit 0\n',
    )
    env = os.environ | {
        "PROJECT_ROOT_OVERRIDE": str(project_root),
        "PYTHON_OVERRIDE": str(python_bin),
        "TIMEOUT_OVERRIDE": str(timeout_bin),
        "LAST_RUN_DIR_OVERRIDE": str(last_run_dir),
        "ET_DOW_OVERRIDE": "3",
        "ET_HOUR_OVERRIDE": "10",
        "ET_MIN_OVERRIDE": "15",
        "ET_DATE_OVERRIDE": "2026-08-26",
        "NOW_UNIX_OVERRIDE": "2000",
    }

    result = subprocess.run(
        [
            "bash",
            str(script),
            "morning",
            "--operator-rerun",
            "verify consolidated Tech recovery",
        ],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    assert counter_file.read_text().strip() == "main.py --mode morning"
    assert (last_run_dir / "last-morning").read_text().strip() == "2026-08-26 2000"
    audit = (last_run_dir / "operator-reruns.log").read_text()
    assert "verify consolidated Tech recovery" in audit
    assert "operator rerun" in result.stdout


def test_operator_rerun_rejects_missing_reason_and_other_modes(tmp_path):
    env = _base_env(tmp_path, "exit 0")
    script = Path(__file__).resolve().parents[1] / "scripts" / "run_if_et_window.sh"
    missing_reason = subprocess.run(
        ["bash", str(script), "morning", "--operator-rerun"],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert missing_reason.returncode == 2

    other_mode = subprocess.run(
        ["bash", str(script), "evening", "--operator-rerun", "not allowed"],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert other_mode.returncode == 2


def test_operator_rerun_still_obeys_morning_window(tmp_path):
    env = _base_env(tmp_path, "exit 0") | {
        "ET_HOUR_OVERRIDE": "12",
        "ET_MIN_OVERRIDE": "30",
    }
    script = Path(__file__).resolve().parents[1] / "scripts" / "run_if_et_window.sh"
    result = subprocess.run(
        ["bash", str(script), "morning", "--operator-rerun", "too late"],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    assert not (tmp_path / "cache" / "operator-reruns.log").exists()


def test_run_if_et_window_intra_check_fires_every_tick(tmp_path):
    """intra_check is a stateless circuit breaker — must fire on every 30-min
    launchd tick inside market hours, ignoring the once-per-day guard used by
    morning / midday / close / evening / earnings_preprocess."""
    script = Path(__file__).resolve().parents[1] / "scripts" / "run_if_et_window.sh"
    project_root = tmp_path / "project"
    project_root.mkdir()
    (project_root / ".env").write_text("")

    last_run_dir = tmp_path / "cache"
    timeout_bin = tmp_path / "timeout"
    python_bin = tmp_path / "fake-python"
    counter_file = tmp_path / "intra-invocations.txt"

    _write_executable(
        timeout_bin,
        '#!/bin/bash\nshift 2\nexec "$@"\n',
    )
    _write_executable(
        python_bin,
        f'#!/bin/bash\necho tick >> "{counter_file}"\nexit 0\n',
    )

    base_env = os.environ | {
        "PROJECT_ROOT_OVERRIDE": str(project_root),
        "PYTHON_OVERRIDE": str(python_bin),
        "TIMEOUT_OVERRIDE": str(timeout_bin),
        "LAST_RUN_DIR_OVERRIDE": str(last_run_dir),
        "ET_DOW_OVERRIDE": "1",  # Monday
        "ET_DATE_OVERRIDE": "2026-04-20",
    }

    # Fire four times across the market day. Each must succeed + increment counter.
    for hh, mm, ts in (("09", "30", "1"), ("11", "30", "2"), ("14", "00", "3"), ("15", "45", "4")):
        result = subprocess.run(
            ["bash", str(script), "intra_check"],
            env=base_env
            | {
                "ET_HOUR_OVERRIDE": hh,
                "ET_MIN_OVERRIDE": mm,
                "NOW_UNIX_OVERRIDE": ts,
            },
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, f"{hh}:{mm} should have fired: {result.stderr}"

    assert counter_file.read_text().splitlines() == ["tick", "tick", "tick", "tick"]
    # Critical: intra_check must NOT write a last-run marker (would re-introduce
    # the once-per-day cap it's explicitly exempt from).
    assert not (last_run_dir / "last-intra_check").exists()


def test_run_if_et_window_serializes_non_intra_modes(tmp_path):
    """The cross-mode session lock must serialize the heavyweight LLM
    sessions (morning/midday/close/evening/earnings_preprocess) so they
    never run concurrently. Use midday vs a held morning lock to verify."""
    script = Path(__file__).resolve().parents[1] / "scripts" / "run_if_et_window.sh"
    project_root = tmp_path / "project"
    project_root.mkdir()
    (project_root / ".env").write_text("")

    last_run_dir = tmp_path / "cache"
    lock_dir = last_run_dir / "active-session.lock"
    lock_dir.mkdir(parents=True)
    (lock_dir / "owner").write_text("morning 2026-04-20 1000 12345")

    timeout_bin = tmp_path / "timeout"
    python_bin = tmp_path / "fake-python"
    counter_file = tmp_path / "blocked-midday.txt"

    _write_executable(timeout_bin, '#!/bin/bash\nshift 2\nexec "$@"\n')
    _write_executable(
        python_bin,
        f'#!/bin/bash\necho fired >> "{counter_file}"\nexit 0\n',
    )

    result = subprocess.run(
        ["bash", str(script), "midday"],
        env=os.environ
        | {
            "PROJECT_ROOT_OVERRIDE": str(project_root),
            "PYTHON_OVERRIDE": str(python_bin),
            "TIMEOUT_OVERRIDE": str(timeout_bin),
            "LAST_RUN_DIR_OVERRIDE": str(last_run_dir),
            "ET_DOW_OVERRIDE": "1",
            "ET_HOUR_OVERRIDE": "13",
            "ET_MIN_OVERRIDE": "00",
            "ET_DATE_OVERRIDE": "2026-04-20",
            "NOW_UNIX_OVERRIDE": "1010",
        },
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    assert not counter_file.exists()
    assert "another quant-agent session is active" in result.stderr


def test_run_if_et_window_intra_check_bypasses_session_lock(tmp_path):
    """intra_check is the stateless circuit breaker — it MUST fire on every
    30-min tick during 09:30-16:00 ET regardless of what else is running.
    A held lock from a long morning/midday must not block it; otherwise the
    flash-crash protection goes silent during exactly the windows when an
    adverse move would be most damaging. Mirrors its exemption from the
    last-run guard."""
    script = Path(__file__).resolve().parents[1] / "scripts" / "run_if_et_window.sh"
    project_root = tmp_path / "project"
    project_root.mkdir()
    (project_root / ".env").write_text("")

    last_run_dir = tmp_path / "cache"
    lock_dir = last_run_dir / "active-session.lock"
    lock_dir.mkdir(parents=True)
    (lock_dir / "owner").write_text("morning 2026-04-20 1000 12345")

    timeout_bin = tmp_path / "timeout"
    python_bin = tmp_path / "fake-python"
    counter_file = tmp_path / "intra-fired.txt"

    _write_executable(timeout_bin, '#!/bin/bash\nshift 2\nexec "$@"\n')
    _write_executable(
        python_bin,
        f'#!/bin/bash\necho fired >> "{counter_file}"\nexit 0\n',
    )

    result = subprocess.run(
        ["bash", str(script), "intra_check"],
        env=os.environ
        | {
            "PROJECT_ROOT_OVERRIDE": str(project_root),
            "PYTHON_OVERRIDE": str(python_bin),
            "TIMEOUT_OVERRIDE": str(timeout_bin),
            "LAST_RUN_DIR_OVERRIDE": str(last_run_dir),
            "ET_DOW_OVERRIDE": "1",
            "ET_HOUR_OVERRIDE": "09",
            "ET_MIN_OVERRIDE": "30",
            "ET_DATE_OVERRIDE": "2026-04-20",
            "NOW_UNIX_OVERRIDE": "1010",
        },
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert counter_file.exists(), "intra_check must fire even when lock is held"
    assert counter_file.read_text().strip() == "fired"
    # The held lock must remain — intra_check never touches it.
    assert (lock_dir / "owner").read_text() == "morning 2026-04-20 1000 12345"


@pytest.mark.parametrize("hh,mm", [("10", "15"), ("13", "45")])
def test_run_if_et_window_intra_check_bypasses_session_lock_at_new_ticks(
    tmp_path,
    hh,
    mm,
):
    """Pin for the 2026-09-17 schedule split: quant-agent-intra_check.timer
    now fires at :15/:45 instead of the shared :00/:30 tick (see
    tests/test_systemd_units.py). This wrapper script has no idea which
    wall-clock minute invoked it — the exemption is a MODE check, not a
    minute check — but the exemption is exactly what the schedule move
    depends on staying intact: intra_check must still fire, and still
    leave any held lock untouched, at the minutes it now actually runs on.
    """
    script = Path(__file__).resolve().parents[1] / "scripts" / "run_if_et_window.sh"
    project_root = tmp_path / "project"
    project_root.mkdir()
    (project_root / ".env").write_text("")

    last_run_dir = tmp_path / "cache"
    lock_dir = last_run_dir / "active-session.lock"
    lock_dir.mkdir(parents=True)
    (lock_dir / "owner").write_text("midday 2026-04-20 1000 12345")

    timeout_bin = tmp_path / "timeout"
    python_bin = tmp_path / "fake-python"
    counter_file = tmp_path / "intra-fired-new-tick.txt"

    _write_executable(timeout_bin, '#!/bin/bash\nshift 2\nexec "$@"\n')
    _write_executable(
        python_bin,
        f'#!/bin/bash\necho fired >> "{counter_file}"\nexit 0\n',
    )

    result = subprocess.run(
        ["bash", str(script), "intra_check"],
        env=os.environ
        | {
            "PROJECT_ROOT_OVERRIDE": str(project_root),
            "PYTHON_OVERRIDE": str(python_bin),
            "TIMEOUT_OVERRIDE": str(timeout_bin),
            "LAST_RUN_DIR_OVERRIDE": str(last_run_dir),
            "ET_DOW_OVERRIDE": "1",
            "ET_HOUR_OVERRIDE": hh,
            "ET_MIN_OVERRIDE": mm,
            "ET_DATE_OVERRIDE": "2026-04-20",
            "NOW_UNIX_OVERRIDE": "1010",
        },
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert counter_file.exists(), (
        "intra_check must still fire at its new :15/:45 ticks even when another session holds the lock"
    )
    # The held lock must remain untouched — intra_check never takes it.
    assert (lock_dir / "owner").read_text() == "midday 2026-04-20 1000 12345"


def test_run_if_et_window_intra_check_skips_outside_window(tmp_path):
    """intra_check window is 09:30-16:00 ET. Outside, it must not fire."""
    script = Path(__file__).resolve().parents[1] / "scripts" / "run_if_et_window.sh"
    project_root = tmp_path / "project"
    project_root.mkdir()
    (project_root / ".env").write_text("")

    last_run_dir = tmp_path / "cache"
    timeout_bin = tmp_path / "timeout"
    python_bin = tmp_path / "fake-python"
    counter_file = tmp_path / "out-of-window.txt"

    _write_executable(timeout_bin, '#!/bin/bash\nshift 2\nexec "$@"\n')
    _write_executable(
        python_bin,
        f'#!/bin/bash\necho fired >> "{counter_file}"\nexit 0\n',
    )

    base_env = os.environ | {
        "PROJECT_ROOT_OVERRIDE": str(project_root),
        "PYTHON_OVERRIDE": str(python_bin),
        "TIMEOUT_OVERRIDE": str(timeout_bin),
        "LAST_RUN_DIR_OVERRIDE": str(last_run_dir),
        "ET_DOW_OVERRIDE": "1",
        "ET_DATE_OVERRIDE": "2026-04-20",
        "NOW_UNIX_OVERRIDE": "1",
    }

    # Pre-market 08:00: outside window → skip
    r1 = subprocess.run(
        ["bash", str(script), "intra_check"],
        env=base_env | {"ET_HOUR_OVERRIDE": "08", "ET_MIN_OVERRIDE": "00"},
        capture_output=True,
        text=True,
        check=False,
    )
    assert r1.returncode == 0

    # Post-close 16:30: outside window → skip
    r2 = subprocess.run(
        ["bash", str(script), "intra_check"],
        env=base_env | {"ET_HOUR_OVERRIDE": "16", "ET_MIN_OVERRIDE": "30"},
        capture_output=True,
        text=True,
        check=False,
    )
    assert r2.returncode == 0

    # Neither tick should have exec'd the fake-python
    assert not counter_file.exists()


# ============================================================================
# RC5 (2026-07-16): bash-side kill notification + external dead-man ping.
# When `timeout` SIGTERM/SIGKILLs python, the in-process finally-block
# notifier never runs — 13 straight days of morning kills produced zero
# failure pushes. The wrapper must report violent deaths itself, and ping
# HEALTHCHECKS_URL so an EXTERNAL monitor sees liveness.
# ============================================================================


def _base_env(tmp_path, python_body: str) -> dict:
    project_root = tmp_path / "project"
    project_root.mkdir(exist_ok=True)
    (project_root / ".env").write_text("")
    timeout_bin = tmp_path / "timeout"
    python_bin = tmp_path / "fake-python"
    curl_log = tmp_path / "curl.log"
    curl_bin = tmp_path / "curl"
    _write_executable(timeout_bin, '#!/bin/bash\nshift 2\nexec "$@"\n')
    _write_executable(python_bin, f"#!/bin/bash\n{python_body}\n")
    _write_executable(curl_bin, f'#!/bin/bash\necho "$@" >> {curl_log}\nexit 0\n')
    env = os.environ | {
        "PROJECT_ROOT_OVERRIDE": str(project_root),
        "PYTHON_OVERRIDE": str(python_bin),
        "TIMEOUT_OVERRIDE": str(timeout_bin),
        "LAST_RUN_DIR_OVERRIDE": str(tmp_path / "cache"),
        "ET_DOW_OVERRIDE": "1",
        "ET_HOUR_OVERRIDE": "08",
        "ET_MIN_OVERRIDE": "30",
        "ET_DATE_OVERRIDE": "2026-07-16",
        "NOW_UNIX_OVERRIDE": "1234567890",
        "PATH": f"{tmp_path}:{os.environ['PATH']}",  # fake curl first
    }
    return env


def _run(script_env, mode="earnings_preprocess"):
    script = Path(__file__).resolve().parents[1] / "scripts" / "run_if_et_window.sh"
    return subprocess.run(["bash", str(script), mode], env=script_env, capture_output=True, text=True, check=False)


def test_wrapper_notifies_telegram_on_timeout_kill(tmp_path):
    env = _base_env(tmp_path, "exit 124")
    env |= {"TELEGRAM_BOT_TOKEN": "tok", "TELEGRAM_CHAT_ID": "42"}
    result = _run(env)
    assert result.returncode == 124
    log = (tmp_path / "curl.log").read_text()
    assert "sendMessage" in log
    assert "KILLED" in log


def test_wrapper_does_not_duplicate_python_notifier_on_plain_failure(tmp_path):
    """Ordinary non-zero exits: python's own finally-block already pushed —
    a second bash push would train the operator to ignore duplicates."""
    env = _base_env(tmp_path, "exit 1")
    env |= {"TELEGRAM_BOT_TOKEN": "tok", "TELEGRAM_CHAT_ID": "42"}
    result = _run(env)
    assert result.returncode == 1
    log_file = tmp_path / "curl.log"
    assert not log_file.exists() or "sendMessage" not in log_file.read_text()


def test_wrapper_respects_telegram_kill_switch(tmp_path):
    # audit round 2 (#44): the wrapper must honour every spelling python's
    # notifier accepts ("1"/"true"/"yes", case-insensitive) — previously
    # only the literal "1" muted the bash-side KILLED push.
    for i, disabled in enumerate(("1", "true", "YES", " on ")):
        sub = tmp_path / f"case{i}"
        sub.mkdir()
        env = _base_env(sub, "exit 137")
        env |= {"TELEGRAM_BOT_TOKEN": "tok", "TELEGRAM_CHAT_ID": "42", "TELEGRAM_DISABLED": disabled}
        _run(env)
        log_file = sub / "curl.log"
        assert not log_file.exists() or "sendMessage" not in log_file.read_text(), (
            f"TELEGRAM_DISABLED={disabled!r} must mute the bash-side push"
        )


def test_wrapper_pings_healthchecks_on_success_and_fail(tmp_path):
    env = _base_env(tmp_path, "exit 0")
    env |= {"HEALTHCHECKS_URL": "https://hc-ping.example/uuid-1"}
    assert _run(env).returncode == 0
    assert "https://hc-ping.example/uuid-1" in (tmp_path / "curl.log").read_text()

    second = tmp_path / "second"
    second.mkdir()
    env2 = _base_env(second, "exit 124")
    env2 |= {"HEALTHCHECKS_URL": "https://hc-ping.example/uuid-1"}
    _run(env2)
    assert "https://hc-ping.example/uuid-1/fail" in (second / "curl.log").read_text()


# --- early closes and holidays: the window comes from the day's real session ---


def _day_env(tmp_path, cli_body, *, hh, mm, date="2026-11-27", dow="5", override=""):
    """A wrapper environment whose fake python answers the calendar command with
    `cli_body` and appends a line to `runs.txt` for every OTHER launch (the phase)."""
    project_root = tmp_path / "project"
    project_root.mkdir(exist_ok=True)
    (project_root / ".env").write_text("")
    timeout_bin = tmp_path / "timeout"
    python_bin = tmp_path / "fake-python"
    _write_executable(timeout_bin, '#!/bin/bash\nshift 2\nexec "$@"\n')
    _write_executable(
        python_bin,
        f'#!/bin/bash\nif [[ "$1" == "-m" && "$2" == "src.session_calendar" ]]; then\n{cli_body}\nfi\n'
        f'echo ran >> "{tmp_path / "runs.txt"}"\nexit 0\n',
    )
    return os.environ | {
        "PROJECT_ROOT_OVERRIDE": str(project_root),
        "PYTHON_OVERRIDE": str(python_bin),
        "TIMEOUT_OVERRIDE": str(timeout_bin),
        "LAST_RUN_DIR_OVERRIDE": str(tmp_path / "cache"),
        "ET_DOW_OVERRIDE": dow,
        "ET_DATE_OVERRIDE": date,
        "ET_HOUR_OVERRIDE": hh,
        "ET_MIN_OVERRIDE": mm,
        "NOW_UNIX_OVERRIDE": "1000",
        "SESSION_WINDOW_OVERRIDE": override,
    }


def _launch(env, mode):
    return subprocess.run(
        ["bash", str(ROOT / "scripts" / WRAPPER_NAME), mode], env=env, capture_output=True, text=True, check=False
    )


def _ran(tmp_path) -> bool:
    return (tmp_path / "runs.txt").exists()


def test_early_close_midday_fires_at_ten_and_not_at_one(tmp_path):
    late = _launch(_day_env(tmp_path, "exit 9", hh="13", mm="00", override="600 690"), "midday")
    assert late.returncode == 0 and not _ran(tmp_path), "13:00 is after the 13:00 close: must not fire"
    early = _launch(_day_env(tmp_path, "exit 9", hh="10", mm="00", override="600 690"), "midday")
    assert early.returncode == 0 and _ran(tmp_path), "10:00 is inside the early-close midday window"


def test_early_close_close_review_fires_in_the_last_half_hour(tmp_path):
    late = _launch(_day_env(tmp_path, "exit 9", hh="15", mm="30", override="750 780"), "close")
    assert late.returncode == 0 and not _ran(tmp_path), "15:30 is long after the early close"
    early = _launch(_day_env(tmp_path, "exit 9", hh="12", mm="30", override="750 780"), "close")
    assert early.returncode == 0 and _ran(tmp_path)


def test_holiday_exits_zero_without_running_the_phase(tmp_path):
    result = _launch(
        _day_env(tmp_path, "exit 9", hh="10", mm="00", date="2026-11-26", dow="4", override="closed"), "morning"
    )
    assert result.returncode == 0
    assert not _ran(tmp_path)


def test_holiday_answer_from_the_calendar_command_exits_zero(tmp_path):
    env = _day_env(tmp_path, "echo closed; exit 0", hh="10", mm="00", date="2026-11-26", dow="4")
    assert _launch(env, "intra_check").returncode == 0
    assert not _ran(tmp_path)


def test_weekend_never_asks_the_calendar(tmp_path):
    asked = tmp_path / "asked.txt"
    env = _day_env(
        tmp_path, f'echo asked >> "{asked}"; echo "570 960"; exit 0', hh="10", mm="00", date="2026-11-28", dow="6"
    )
    assert _launch(env, "morning").returncode == 0
    assert not asked.exists() and not _ran(tmp_path)


def test_calendar_unavailable_uses_the_normal_day_window_and_says_so_once(tmp_path):
    body = 'echo "exchange calendar unreadable" >&2; echo "780 870"; exit 3'
    result = _launch(_day_env(tmp_path, body, hh="13", mm="00"), "midday")
    assert result.returncode == 0 and _ran(tmp_path), "the normal-day midday window (13:00-14:30) applies"
    said = [line for line in result.stderr.splitlines() if "calendar unavailable" in line]
    assert len(said) == 1, result.stderr
    assert "780-870" in said[0] or "780 870" in said[0]


def test_calendar_unavailable_outside_the_normal_day_window_does_not_fire(tmp_path):
    body = 'echo "exchange calendar unreadable" >&2; echo "780 870"; exit 3'
    result = _launch(_day_env(tmp_path, body, hh="10", mm="00"), "midday")
    assert result.returncode == 0 and not _ran(tmp_path)


def test_calendar_command_timing_out_falls_back_to_the_normal_day_window(tmp_path):
    body = 'if [[ " $* " == *" --normal-day "* ]]; then echo "780 870"; exit 0; fi; exit 124'
    result = _launch(_day_env(tmp_path, body, hh="13", mm="00"), "midday")
    assert result.returncode == 0 and _ran(tmp_path)
    assert len([ln for ln in result.stderr.splitlines() if "calendar unavailable" in ln]) == 1, result.stderr


@pytest.mark.parametrize("body", ["exit 1", "echo garbage; exit 0", "exit 0"])
def test_calendar_command_with_no_usable_answer_fails_the_tick_loudly(tmp_path, body):
    result = _launch(_day_env(tmp_path, body, hh="13", mm="00"), "midday")
    assert result.returncode != 0
    assert not _ran(tmp_path)
    assert "session calendar" in result.stderr


def test_wrapper_with_the_real_command_reads_the_early_close_from_the_day_cache(tmp_path):
    """No fake answer here: the real `python -m src.session_calendar` reads a
    seeded per-date file (a broker is impossible, so a miss would fail)."""
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / "session-2026-11-27").write_text("570 780\n")
    (cache / "session-2026-11-26").write_text("closed\n")
    body = f'exec "{sys.executable}" "$@"'

    def env_for(hh, mm, date="2026-11-27", dow="5"):
        return _day_env(tmp_path, body, hh=hh, mm=mm, date=date, dow=dow) | {"PYTHONPATH": str(ROOT)}

    assert _launch(env_for("13", "00"), "midday").returncode == 0
    assert not _ran(tmp_path), "13:00 on the half day is after the close"
    assert _launch(env_for("10", "00"), "midday").returncode == 0
    assert _ran(tmp_path), "10:00 on the half day is inside midday"
    (tmp_path / "runs.txt").unlink()
    assert _launch(env_for("10", "00", date="2026-11-26", dow="4"), "morning").returncode == 0
    assert not _ran(tmp_path), "the holiday runs nothing"
