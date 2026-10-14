#!/bin/bash
# Fire main.py --mode $1 only when the US/Eastern wall clock is inside the
# window for that mode, it's a weekday (ET), and the mode has not already
# completed during this ET session date.
#
# Called from launchd every 30 minutes (StartInterval=1800). All three plists
# share this script so the system fires at correct US market times regardless
# of the host's timezone — survives the user flying across continents.
#
# Usage: run_if_et_window.sh <earnings_preprocess|morning|intra_check|intra_safety|midday|close|evening>
#        run_if_et_window.sh morning --operator-rerun "reason"

set -eu

MODE="${1:-}"
if [[ -z "$MODE" ]]; then
    echo "usage: $0 <earnings_preprocess|morning|intra_check|intra_safety|midday|close|evening>" >&2
    exit 2
fi

# Explicit same-day morning rerun for engineering verification. This skips
# ONLY the once-per-day marker below: weekday/window, cross-mode lock, timeout,
# Python's Paper-only configuration, paid-analysis circuit, deterministic risk,
# and broker protections all remain authoritative. Requiring a reason makes the
# exceptional run visible and discourages this from becoming a second schedule.
OPERATOR_RERUN=0
OPERATOR_RERUN_REASON=""
if [[ "${2:-}" == "--operator-rerun" ]]; then
    OPERATOR_RERUN=1
    OPERATOR_RERUN_REASON="${3:-}"
    if [[ "$MODE" != "morning" ]]; then
        echo "--operator-rerun is supported only for morning" >&2
        exit 2
    fi
    if [[ -z "$OPERATOR_RERUN_REASON" || "$OPERATOR_RERUN_REASON" == *$'\n'* || "$OPERATOR_RERUN_REASON" == *$'\r'* || ${#OPERATOR_RERUN_REASON} -gt 200 ]]; then
        echo "--operator-rerun requires a single-line reason of 1-200 characters" >&2
        exit 2
    fi
    if [[ -n "${4:-}" ]]; then
        echo "--operator-rerun accepts exactly one quoted reason" >&2
        exit 2
    fi
elif [[ -n "${2:-}" ]]; then
    echo "unknown option: ${2}" >&2
    exit 2
fi

# PROJECT_ROOT resolution, in order of preference:
#   1. PROJECT_ROOT_OVERRIDE env var (set by the plist when launchd invokes us)
#   2. Derive from this script's location, IF the script lives in `<repo>/scripts/`
#      and `<repo>/src/` exists (manual invocation from a freshly-cloned repo)
#   3. Fail loudly — anywhere else (e.g. the installed copy under
#      ~/Library/Application Support/quant-agent/) the plist MUST inject
#      PROJECT_ROOT_OVERRIDE; running without it is a misconfiguration.
if [[ -n "${PROJECT_ROOT_OVERRIDE:-}" ]]; then
    PROJECT_ROOT="${PROJECT_ROOT_OVERRIDE}"
else
    _SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    if [[ -d "${_SCRIPT_DIR}/../src" && -f "${_SCRIPT_DIR}/../main.py" ]]; then
        PROJECT_ROOT="$(cd "${_SCRIPT_DIR}/.." && pwd)"
    else
        echo "PROJECT_ROOT_OVERRIDE not set and cannot derive from script location." >&2
        echo "Re-run scripts/install_plists.sh to inject PROJECT_ROOT_OVERRIDE into the plist." >&2
        exit 2
    fi
fi
PYTHON="${PYTHON_OVERRIDE:-${PROJECT_ROOT}/.venv/bin/python}"
# `timeout` is on PATH on Linux (coreutils). On macOS, install via `brew install
# coreutils` and either put $(brew --prefix coreutils)/libexec/gnubin on PATH or
# set TIMEOUT_OVERRIDE=/opt/homebrew/bin/timeout.
TIMEOUT="${TIMEOUT_OVERRIDE:-timeout}"
LAST_RUN_DIR="${LAST_RUN_DIR_OVERRIDE:-${HOME}/.cache/quant-agent}"
MIN_GAP_SEC="${MIN_GAP_SEC_OVERRIDE:-3600}"  # don't fire same mode twice within an hour
SESSION_LOCK_DIR="${LAST_RUN_DIR}/active-session.lock"
# Session run ceiling, enforced below by `timeout`. This is the ONE place
# the number is written; the stale-lock ceiling is derived from it rather
# than restated, because the two drifting apart is what makes a stale lock
# either block a legitimate session or clear a live one.
SESSION_RUN_TIMEOUT_SEC="${SESSION_RUN_TIMEOUT_SEC_OVERRIDE:-1200}"
SESSION_RUN_KILL_AFTER_SEC="${SESSION_RUN_KILL_AFTER_SEC_OVERRIDE:-30}"
# Stale-lock cleanup ceiling. DEFECT 9 (adversary round 3): this was a
# hand-written 1800 sitting directly on the duplicate-stop hazard -- too
# low and a still-running session's lock is cleared, so a second session
# starts and both place protective stops on the same shares; too high and
# a crashed session blocks the next legitimate one for the whole window.
# It is no longer a number at all. The hard upper bound on a live session
# is exactly what this script enforces one screen below: SIGTERM at
# SESSION_RUN_TIMEOUT_SEC, SIGKILL SESSION_RUN_KILL_AFTER_SEC later. A
# process alive past their sum cannot exist, and nothing shorter is safe.
# That holds ONLY because the owner stamp is rewritten immediately before
# `timeout` starts (see FAULT 1 at the run site): age and kill are counted
# from the same instant. If that re-stamp is ever removed, this derivation
# becomes too tight by the length of the prelude and must not stand.
SESSION_LOCK_MAX_AGE_SEC="${SESSION_LOCK_MAX_AGE_SEC_OVERRIDE:-$(( SESSION_RUN_TIMEOUT_SEC + SESSION_RUN_KILL_AFTER_SEC ))}"

mkdir -p "$LAST_RUN_DIR"

# === ET weekday + hour (independent of host TZ) ===
ET_DOW="${ET_DOW_OVERRIDE:-$(TZ="America/New_York" date +%u)}"    # 1=Mon ... 7=Sun
ET_HOUR="${ET_HOUR_OVERRIDE:-$(TZ="America/New_York" date +%H)}"
ET_MIN="${ET_MIN_OVERRIDE:-$(TZ="America/New_York" date +%M)}"
ET_TOTAL_MIN=$((10#$ET_HOUR * 60 + 10#$ET_MIN))  # 10# forces base-10 (leading zeros)
ET_DATE="${ET_DATE_OVERRIDE:-$(TZ="America/New_York" date +%Y-%m-%d)}"

# Weekend short-circuit — US markets closed
if [[ "$ET_DOW" -gt 5 ]]; then
    exit 0
fi

# Load the desk's secrets into this shell's environment. Defined before the
# window lookup because the session-calendar command needs the broker keys too,
# and run again at the fire site: one copy of this logic, two callers.
load_desk_environment() {
    # Load API keys from .env — single source of truth for secrets.
    # Keeps the plist files free of keys (they're world-readable in
    # ~/Library/LaunchAgents; .env is chmod 600). Key rotation then means
    # editing one file, not five.
    if [[ -f "${PROJECT_ROOT}/.env" ]]; then
        # shellcheck disable=SC1091
        set -a
        source "${PROJECT_ROOT}/.env"
        set +a
    fi

    # systemd-delivered broker credentials WIN over .env, and must be applied AFTER
    # it — .env carries the placeholder the credential gateway substitutes on REST,
    # so sourcing it later would silently put the stand-in back. PR #458 shipped the
    # delivery half (SetCredential= stand-in, LoadCredential= real file) and nothing
    # read $CREDENTIALS_DIRECTORY, so a delivered key was ignored: the desk still
    # reported PLACEHOLDER CREDENTIAL with both files present and correct.
    # Absent directory or absent file is the normal, supported state — the gateway
    # keeps REST working and the placeholder alert says the socket cannot authenticate.
    if [[ -n "${CREDENTIALS_DIRECTORY:-}" ]]; then
        if [[ -r "${CREDENTIALS_DIRECTORY}/alpaca_api_key" ]]; then
            ALPACA_API_KEY="$(<"${CREDENTIALS_DIRECTORY}/alpaca_api_key")"
            export ALPACA_API_KEY
        fi
        if [[ -r "${CREDENTIALS_DIRECTORY}/alpaca_secret_key" ]]; then
            ALPACA_SECRET_KEY="$(<"${CREDENTIALS_DIRECTORY}/alpaca_secret_key")"
            export ALPACA_SECRET_KEY
        fi
    fi
}

# === Window per mode (minutes past ET midnight) ===
# THIS WRAPPER HOLDS NO MINUTES. The schedule has one copy, in Python:
# `PHASE_OFFSETS` in src/trading_calendar.py states each phase as minutes from
# the day's real open or close, and `python -m src.session_calendar <mode>
# <ET date>` resolves it for this date against the broker's exchange calendar,
# printing `LO HI` (ET minutes of day) or `closed`. On a normal day that is the
# familiar table (morning 09:30-12:00, midday 13:00-14:30, close 15:30-16:00 ...);
# on a 13:00 early close midday becomes 10:00-11:30 and close 12:30-13:00; on a
# holiday it is `closed` and nothing runs. Only the first tick of a day asks the
# broker; the answer is kept in $LAST_RUN_DIR/session-<date>.
# intra_safety is intra_check's twin (same work, same hours) and the command
# knows that; it is deliberately not a mode of the paid-session count table.
case "$MODE" in
    earnings_preprocess|morning|intra_check|intra_safety|midday|close|evening) ;;
    *) echo "unknown mode: $MODE" >&2; exit 2 ;;
esac

# Bound on the one calendar read. Only the first tick of the day can pay it.
SESSION_CAL_TIMEOUT_SEC="${SESSION_CAL_TIMEOUT_SEC_OVERRIDE:-60}"

session_calendar_cmd() {
    # Runs in a subshell: own cwd (the command imports from the project) and own
    # copy of the secrets, neither leaking into the rest of the script.
    (
        cd "$PROJECT_ROOT" &&
            load_desk_environment &&
            exec "$TIMEOUT" --kill-after=5 "$SESSION_CAL_TIMEOUT_SEC" \
                "$PYTHON" -m src.session_calendar "$@"
    )
}

session_calendar_failed() {
    # No usable answer (python missing, crashed, or printing nonsense). There is
    # no fallback number in the shell: the phase itself is Python and would fail
    # the same way, so the tick fails loudly instead.
    echo "[$(date '+%Y-%m-%d %H:%M:%S %Z')] ${MODE} not run: session calendar command failed: $1" >&2
    exit 1
}

# SESSION_WINDOW_OVERRIDE ("LO HI" or "closed") stands in for the command, the
# way ET_HOUR_OVERRIDE stands in for the clock: for tests, which have no broker.
CAL_REASON=""
CAL_STATUS=0
if [[ -n "${SESSION_WINDOW_OVERRIDE:-}" ]]; then
    WINDOW="$SESSION_WINDOW_OVERRIDE"
else
    CAL_ERR_FILE="$(mktemp "${LAST_RUN_DIR}/session-cal-err.XXXXXX")"
    if WINDOW="$(session_calendar_cmd "$MODE" "$ET_DATE" --cache-dir "$LAST_RUN_DIR" 2>"$CAL_ERR_FILE")"; then
        CAL_STATUS=0
    else
        CAL_STATUS=$?
    fi
    CAL_REASON="$(tail -n 1 "$CAL_ERR_FILE" 2>/dev/null || true)"
    rm -f "$CAL_ERR_FILE"
    WINDOW="$(printf '%s\n' "$WINDOW" | tail -n 1)"
    case "$CAL_STATUS" in
        0) ;;
        3)
            # Calendar unreadable: the command printed the normal-day window.
            echo "[$(date '+%Y-%m-%d %H:%M:%S %Z')] ${MODE}: session calendar unavailable (${CAL_REASON:-no reason given}); using the normal-day window ${WINDOW}" >&2
            ;;
        124|137)
            # The read hung. The normal-day window needs no broker, so ask for that.
            if WINDOW="$(session_calendar_cmd "$MODE" "$ET_DATE" --normal-day 2>/dev/null)"; then
                WINDOW="$(printf '%s\n' "$WINDOW" | tail -n 1)"
            else
                session_calendar_failed "read timed out (status ${CAL_STATUS}) and the normal-day window could not be read"
            fi
            echo "[$(date '+%Y-%m-%d %H:%M:%S %Z')] ${MODE}: session calendar unavailable (read timed out after ${SESSION_CAL_TIMEOUT_SEC}s); using the normal-day window ${WINDOW}" >&2
            ;;
        *) session_calendar_failed "status ${CAL_STATUS}: ${CAL_REASON:-no message}" ;;
    esac
fi

if [[ "$WINDOW" == "closed" ]]; then
    exit 0  # the exchange is shut today: no phase runs
fi
if [[ "$WINDOW" =~ ^([0-9]+)\ ([0-9]+)$ ]]; then
    LO="${BASH_REMATCH[1]}"
    HI="${BASH_REMATCH[2]}"
else
    session_calendar_failed "unreadable answer '${WINDOW}'"
fi

if [[ "$ET_TOTAL_MIN" -lt "$LO" || "$ET_TOTAL_MIN" -gt "$HI" ]]; then
    exit 0
fi

# === Last-run guard — don't fire more than once per window ===
# Exception: intra_check is stateless and designed to fire on every 30-min
# launchd tick during market hours. All of its actions (fill reconcile,
# stop-coverage repair, force_delever, P&L read) are idempotent — firing
# multiple times per day produces identical outcomes. The once-per-day guard
# is therefore skipped and no last-run file is written for it.
# See the longer note at the session-lock exemption below, and board item 128.
LAST_FILE="${LAST_RUN_DIR}/last-${MODE}"
NOW_UNIX="${NOW_UNIX_OVERRIDE:-$(date +%s)}"
if [[ "$MODE" != "intra_check" && "$MODE" != "intra_safety" && "$OPERATOR_RERUN" -ne 1 && -f "$LAST_FILE" ]]; then
    LAST_VALUE="$(cat "$LAST_FILE" 2>/dev/null || echo 0)"
    LAST_DATE="${LAST_VALUE%% *}"
    # Primary guard: never fire the same mode twice in the same ET session date.
    # This is stricter than a simple min-gap and matches the once-per-session
    # contract for morning/midday/evening/preprocess/close windows.
    if [[ "$LAST_DATE" == "$ET_DATE" ]]; then
        exit 0
    fi

    # Legacy compatibility: old guard files stored just a unix timestamp.
    if [[ "$LAST_VALUE" =~ ^[0-9]+$ ]]; then
        GAP=$((NOW_UNIX - LAST_VALUE))
        if [[ "$GAP" -lt "$MIN_GAP_SEC" ]]; then
            exit 0
        fi
    fi
fi

# === Cross-mode session lock ===
# launchd owns one plist per mode, so overlapping windows can otherwise run
# concurrently (e.g. a long morning LLM call while midday fires). Keep one
# Python trading session active at a time; stale lock cleanup handles crashes.
#
# intra_check is INTENTIONALLY exempt so it fires on every 30-min tick
# during 09:30-16:00 ET regardless of what else is running. Mirrors its
# exemption from the last-run guard above. All its actions are idempotent
# (fill reconcile, stop-coverage repair, force_delever, P&L read, bounded
# scan), so frequent execution is safe. Without the exemption a long
# morning/midday run would hold the lock and starve intra_check for hours,
# blocking these frequent safety checks.
LOCK_ACQUIRED=0
LOCK_OWNER_FILE="${SESSION_LOCK_DIR}/owner"

acquire_session_lock() {
    if [[ "$MODE" == "intra_check" || "$MODE" == "intra_safety" ]]; then
        return 0
    fi

    if mkdir "$SESSION_LOCK_DIR" 2>/dev/null; then
        LOCK_ACQUIRED=1
        echo "${MODE} ${ET_DATE} ${NOW_UNIX} $$" > "$LOCK_OWNER_FILE"
        return 0
    fi

    LOCK_VALUE=""
    LOCK_TS=""
    if [[ -f "$LOCK_OWNER_FILE" ]]; then
        LOCK_VALUE="$(cat "$LOCK_OWNER_FILE" 2>/dev/null || true)"
        read -r _LOCK_MODE _LOCK_DATE LOCK_TS _LOCK_PID <<< "$LOCK_VALUE"
    fi

    if [[ "$LOCK_TS" =~ ^[0-9]+$ ]]; then
        LOCK_AGE=$((NOW_UNIX - LOCK_TS))
        if [[ "$LOCK_AGE" -gt "$SESSION_LOCK_MAX_AGE_SEC" ]]; then
            echo "[$(date '+%Y-%m-%d %H:%M:%S %Z')] Removing stale quant-agent session lock (${LOCK_AGE}s old): ${LOCK_VALUE}" >&2
            rm -f "$LOCK_OWNER_FILE"
            rmdir "$SESSION_LOCK_DIR" 2>/dev/null || true
            if mkdir "$SESSION_LOCK_DIR" 2>/dev/null; then
                LOCK_ACQUIRED=1
                echo "${MODE} ${ET_DATE} ${NOW_UNIX} $$" > "$LOCK_OWNER_FILE"
                return 0
            fi
        fi
    fi

    echo "[$(date '+%Y-%m-%d %H:%M:%S %Z')] Skipping ${MODE}: another quant-agent session is active (${LOCK_VALUE:-unknown})" >&2
    exit 0
}

release_session_lock() {
    if [[ "$LOCK_ACQUIRED" -eq 1 ]]; then
        rm -f "$LOCK_OWNER_FILE"
        rmdir "$SESSION_LOCK_DIR" 2>/dev/null || true
    fi
}

acquire_session_lock
trap release_session_lock EXIT INT TERM

# === All checks passed — fire ===
if [[ "$OPERATOR_RERUN" -eq 1 ]]; then
    echo "${ET_DATE} ${NOW_UNIX} $$ ${OPERATOR_RERUN_REASON}" >> "${LAST_RUN_DIR}/operator-reruns.log"
    echo "[$(date '+%Y-%m-%d %H:%M:%S %Z')] Firing ${MODE} operator rerun (ET ${ET_DATE} ${ET_HOUR}:${ET_MIN}, weekday ${ET_DOW}; reason: ${OPERATOR_RERUN_REASON})"
else
    echo "[$(date '+%Y-%m-%d %H:%M:%S %Z')] Firing ${MODE} (ET ${ET_DATE} ${ET_HOUR}:${ET_MIN}, weekday ${ET_DOW})"
fi
cd "$PROJECT_ROOT"

# API keys and broker credentials (see load_desk_environment above).
load_desk_environment

# Best-effort Telegram push from BASH. RC5 (2026-07-16): when `timeout`
# SIGTERM/SIGKILLs python, the finally-block notifier never runs — 13
# straight days of morning kills produced ZERO failure notifications.
# Python cannot be trusted to report its own violent death; the wrapper can.
notify_telegram() {
    local text="$1"
    # audit round 2 (#44): match the SAME kill-switch spellings python's
    # notifier accepts (notifier.py: "1"/"true"/"yes", case-insensitive;
    # "on" added as a harmless superset). Previously only the literal "1"
    # muted the bash-side KILLED push, so TELEGRAM_DISABLED=true silenced
    # python but the wrapper still fired on violent kills. tr-based
    # lowercasing keeps macOS bash 3.2 compatibility.
    case "$(printf '%s' "${TELEGRAM_DISABLED:-}" | tr '[:upper:]' '[:lower:]' | xargs)" in
        1|true|yes|on) return 0 ;;
    esac
    [[ -z "${TELEGRAM_BOT_TOKEN:-}" || -z "${TELEGRAM_CHAT_ID:-}" ]] && return 0
    curl -sS --max-time 10 \
        "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
        --data-urlencode "chat_id=${TELEGRAM_CHAT_ID}" \
        --data-urlencode "text=${text}" >/dev/null 2>&1 || true
}

# External dead-man's switch (CLAUDE.md wishlist): if HEALTHCHECKS_URL is
# set in .env, ping it on success and <url>/fail on failure. An absent ping
# then alerts from OUTSIDE the host — the only coverage for total host
# death / evening itself not firing.
ping_healthcheck() {
    local suffix="${1:-}"
    [[ -z "${HEALTHCHECKS_URL:-}" ]] && return 0
    curl -fsS --max-time 10 --retry 2 "${HEALTHCHECKS_URL}${suffix}" >/dev/null 2>&1 || true
}

# FAULT 1 (adversary round 4). The lock age is compared against the kill
# this script performs, so the two must be measured from the SAME instant.
# The lock was stamped with NOW_UNIX, taken near the top of the script --
# before the environment load, the credential reads and the healthcheck
# curls -- while `timeout` only starts counting here. A live session's true
# maximum lock age was therefore the prelude PLUS the kill, which is
# strictly more than the ceiling, and the next tick would have cleared the
# lock of a session still running: two sessions, two protective stops on the
# same shares, and nothing in this desk reconciles a duplicate.
#
# The boundary is fixed, not padded. The owner stamp is rewritten with the
# clock reading at the instant `timeout` starts, so lock age and the kill
# now count from the same zero and the derived ceiling is exact. No padding
# number is chosen and nothing is measured by guess. A crash during the
# prelude still leaves the earlier stamp, which only ever makes the lock
# look OLDER and so clears sooner -- the safe direction. The PID is
# refreshed with it so the owner line keeps naming the process that holds
# the lock.
if [[ "$LOCK_ACQUIRED" -eq 1 ]]; then
    echo "${MODE} ${ET_DATE} $(date +%s) $$" > "$LOCK_OWNER_FILE"
fi

# The session's hard deadline, handed to Python as an absolute epoch so the
# timeout above stays the ONE place the ceiling is written. Taken at the
# instant `timeout` starts counting (same zero as the lock stamp above): the
# no-print wait (src/price_feed_preflight.py) budgets against it so the
# process is never TERMed mid-submit. Absent (a manual run), Python waits
# not at all -- it fails closed, never open.
SESSION_DEADLINE_EPOCH="$(( $(date +%s) + SESSION_RUN_TIMEOUT_SEC ))"
export SESSION_DEADLINE_EPOCH

if "$TIMEOUT" --kill-after="$SESSION_RUN_KILL_AFTER_SEC" "$SESSION_RUN_TIMEOUT_SEC" "$PYTHON" main.py --mode "$MODE"; then
    # intra_check is intentionally guard-less (see last-run guard block above) —
    # we don't write the marker for it, so the next 30-min tick can fire freely.
    if [[ "$MODE" != "intra_check" ]]; then
        echo "${ET_DATE} ${NOW_UNIX}" > "$LAST_FILE"
    fi
    # audit round 2 (#43): do NOT success-ping for intra_check. All six
    # modes share one HEALTHCHECKS_URL, and intra_check's ~13 OK ticks/day
    # (was 14 until the timer moved to :15,45 on 2026-09-17; 13 is what the
    # cost circuit recorded on every trading day 2026-09-21..25, and
    # tests/test_systemd_units.py derives the count from this window and
    # that timer rather than trusting this comment)
    # would pin the shared check green even when morning/evening silently
    # die — defeating the dead-man's switch this ping exists for. Failure
    # pings (below) still fire for ALL modes, intra_check included, so a
    # crashing intra_check tick is still visible externally.
    if [[ "$MODE" != "intra_check" ]]; then
        ping_healthcheck
    fi
    exit 0
else
    STATUS=$?
fi

# 75 = main.py's OWNER_STOP_EXIT: the owner's Stop is in force, the desk is
# off and main.py did nothing (resting entries cancelled, stops kept). Not a
# failure, not a run: no last-run marker (a Start later today may still run
# this session), no healthcheck ping, no push.
if [[ "$STATUS" -eq 75 ]]; then
    echo "[$(date '+%Y-%m-%d %H:%M:%S %Z')] ${MODE} not run: owner Stop in force" >&2
    exit 0
fi

echo "[$(date '+%Y-%m-%d %H:%M:%S %Z')] ${MODE} failed with status ${STATUS}; not updating last-run guard" >&2
# Bash-side push ONLY for violent deaths (124=timeout, 137=SIGKILL,
# 143=SIGTERM) — those skip python's finally-block notifier entirely. For
# ordinary non-zero exits python already pushed its own FAILED message;
# pushing again here would just teach the operator to ignore duplicates.
if [[ "$STATUS" -eq 124 || "$STATUS" -eq 137 || "$STATUS" -eq 143 ]]; then
    notify_telegram "🛑 KILLED: quant-agent ${MODE} (status ${STATUS}) on ${ET_DATE} — python got no chance to notify. Next tick retries (morning resumes from the decision checkpoint if one was written)."
fi
ping_healthcheck "/fail"
exit "$STATUS"
