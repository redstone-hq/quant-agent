"""The owner Stop gate's decision: database intent only, no broker credentials.

Why this module exists: every desk unit carries
``ExecCondition=scripts/owner_stop_gate.sh``. Until 2026-10-13 that script ran
``main._owner_stop_in_force``, which calls ``load_config`` -- and
``load_config`` resolves the systemd-delivered broker credentials. systemd 255
advertises ``CREDENTIALS_DIRECTORY`` to ``ExecCondition=`` but does not create
it (only ``ExecStart=`` gets it), so ``load_config`` raised
``CredentialDeliveryError`` on every unit start since the gate went in
(10 Oct, #1672), the catch-all turned that into "no Stop", and the gate never
decided anything. Deciding Stop needs two things -- the database path and the
owner intents in it -- so that is all this reads.

What stays in ``ExecStart``: cancelling resting UNFILLED entries needs the
broker, so it stays in ``main._exit_if_owner_stopped`` (via
``owner_intents.honour_stop``), where credentials exist. A gate that simply
skipped every unit while stopped would mean no process ever runs that sweep,
so while a sweep is still OWED for the Stop in force (see
``owner_intents.stop_sweep_owed``) the gate lets the session units -- the ones
whose ``ExecStart`` reaches that path -- run; they sweep and exit 75 without
doing any work. Every other unit is skipped.

"Could not decide" is a named, durable fault (``owner_stop_gate.undecided``):
an ERROR line in ``quant_agent.log`` that the log health report surfaces, plus
a ``disagreed`` row on the reconciliation channel. The unit then RUNS, as
before: a broken gate must never switch the desk's exits off, and every
session unit re-checks Stop inside main.py with credentials.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

import yaml

logger = logging.getLogger("src.owner_stop_gate")

RUN = 0
STOP = 75  # main.OWNER_STOP_EXIT (os.EX_TEMPFAIL); the shell script matches it
UNDECIDED = 3
UNDECIDED_SITE = "owner_stop_gate.undecided"

#: Units whose ExecStart reaches main.py's one-shot Stop check
#: (``scripts/run_if_et_window.sh <mode>`` / ``run_daily_export.sh``) and so
#: can run the credentialed sweep. tests/test_owner_stop_gate.py checks this
#: against scripts/systemd so a new session unit cannot be missed silently.
SWEEP_UNITS = frozenset(
    {
        "quant-agent-morning.service",
        "quant-agent-midday.service",
        "quant-agent-close.service",
        "quant-agent-evening.service",
        "quant-agent-intra_check.service",
        "quant-agent-intra_safety.service",
        "quant-agent-earnings_preprocess.service",
        "quant-agent-daily.service",
    }
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent


class GateUndecided(RuntimeError):
    """The gate could not read the database path or the owner intents."""


def gate_db_path(config_arg) -> str:
    """``storage.db_path`` from settings.yaml, read WITHOUT credential resolution."""
    path = Path(config_arg)
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    with open(path) as fh:
        raw = yaml.safe_load(fh) or {}
    db = (raw.get("storage") or {}).get("db_path")
    if not isinstance(db, str) or not db.strip() or "${" in db:
        raise GateUndecided(f"storage.db_path not a plain path in {path}: {db!r}")
    return db


def decide(config_arg, unit: str = "unit") -> int:
    """RUN, STOP or UNDECIDED for one unit start. Never raises; never touches the broker."""
    from src.owner_flags import read_flags
    from src.owner_intents import intake, stop_sweep_owed

    try:
        db = gate_db_path(config_arg)
        intake(db)
        flags = read_flags(db)
        if flags.unknown and not flags.stopped:
            raise GateUndecided("owner intents unreadable (UNKNOWN) and no saved Stop")
        if flags.unknown:
            _record_undecided(unit, GateUndecided("owner intents unreadable; last saved copy says Stop"))
        if not flags.stopped:
            return RUN
        if unit in SWEEP_UNITS and stop_sweep_owed(db):
            logger.warning("%s: owner Stop in force, resting-entry sweep still owed: running it to sweep", unit)
            return RUN
        return STOP
    except Exception as exc:  # noqa: BLE001 - recorded below as a named durable fault
        _record_undecided(unit, exc)
        return UNDECIDED


def _record_undecided(unit: str, exc: BaseException) -> None:
    from src.sentinel.counted import record_swallowed

    logger.error("owner Stop gate could not decide for %s; running the unit: %s: %s", unit, type(exc).__name__, exc)
    record_swallowed(UNDECIDED_SITE, exc, log=logger, unit=unit)


def _log_to_desk_file() -> None:
    """ERROR and above into quant_agent.log, in main.py's line format, for the log health report."""
    target = os.environ.get("OWNER_STOP_GATE_LOG") or str(PROJECT_ROOT / "quant_agent.log")
    handler = logging.FileHandler(target, delay=True)
    handler.setLevel(logging.WARNING)
    handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
    root = logging.getLogger()
    root.addHandler(handler)
    root.setLevel(logging.INFO)


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    config = argv[0] if argv else os.environ.get("OWNER_STOP_GATE_CONFIG", "config/settings.yaml")
    unit = argv[1] if len(argv) > 1 else "unit"
    try:
        _log_to_desk_file()
    except Exception:  # noqa: BLE001 - stderr still reaches the journal
        logging.basicConfig(level=logging.INFO)
    return decide(config, unit)


if __name__ == "__main__":
    sys.exit(main())
