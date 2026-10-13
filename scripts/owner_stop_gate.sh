#!/usr/bin/env bash
# Owner Stop gate -- the ONE ExecCondition= every desk unit in scripts/systemd
# carries (owner ruling 2026-10-09: Stop = the desk is OFF; nothing reads,
# nothing runs, no AI; positions and the protective stops at the broker kept).
#
# systemd reads ExecCondition= exit codes as: 0 = run the unit, 1..254 = skip
# it cleanly (the unit is NOT marked failed), 255 or a signal = fail. So:
#   exit 1 -> owner Stop in force: the unit is skipped
#   exit 0 -> no Stop: the unit runs
#
# The decision is src/owner_stop_gate.py: owner-intent pickup and the Stop read
# from the database ALONE. It loads no broker credentials: systemd 255 does not
# materialise CREDENTIALS_DIRECTORY for ExecCondition=, and the old call into
# main._owner_stop_in_force (load_config + credentials) therefore raised on
# every start and the gate never decided anything (10-13 Oct 2026). The
# credentialed cancel of resting UNFILLED entries is done by the owner switch
# when it records the Stop, with main._exit_if_owner_stopped as the backstop.
#
# Anything the gate cannot settle runs the unit, matching
# src.owner_flags.stop_in_force: an UNKNOWN read is not a Stop, so a broken gate
# can never silently switch the desk's exits off. It is no longer silent: the
# Python side records owner_stop_gate.undecided (ERROR in quant_agent.log and a
# counted reconciliation row) and a crash before that is logged here.
#
# Not set -e: every failure must fall through to "run".
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
PYTHON="${PYTHON_OVERRIDE:-${PROJECT_ROOT}/.venv/bin/python}"
TIMEOUT="${TIMEOUT_OVERRIDE:-/usr/bin/timeout}"
CONFIG="${OWNER_STOP_GATE_CONFIG:-config/settings.yaml}"
UNIT="${1:-unit}"
# No broker call any more: one sqlite pickup and read. 60s is the
# owner-visible delay ceiling on a unit start, not a tuned value.
GATE_TIMEOUT_SEC="${OWNER_STOP_GATE_TIMEOUT_SEC:-60}"

cd "$PROJECT_ROOT" || exit 0

"$TIMEOUT" "$GATE_TIMEOUT_SEC" "$PYTHON" -m src.owner_stop_gate "$CONFIG" "$UNIT" >/dev/null
STATUS=$?

if [[ "$STATUS" -eq 75 ]]; then
    echo "[$(date '+%Y-%m-%d %H:%M:%S %Z')] ${UNIT} skipped: owner Stop in force, the desk is off" >&2
    exit 1
fi
if [[ "$STATUS" -ne 0 ]]; then
    MSG="owner Stop gate could not decide for ${UNIT} (status ${STATUS}); running the unit"
    echo "[$(date '+%Y-%m-%d %H:%M:%S %Z')] ${MSG}" >&2
    if [[ "$STATUS" -ne 70 ]]; then
        # Python never reached its own recorder (missing, import error,
        # timeout): put the ERROR line where the log health report reads.
        echo "$(date -u '+%Y-%m-%d %H:%M:%S'),000 [ERROR] src.owner_stop_gate: ${MSG}" >>"${OWNER_STOP_GATE_LOG:-${PROJECT_ROOT}/quant_agent.log}" 2>/dev/null || true
    fi
fi
exit 0
