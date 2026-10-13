#!/usr/bin/env bash
# Merge a PR and deploy it to the box in ONE action, because doing them as two
# steps is how the box ends up behind.
#
# WHY THIS EXISTS. On 2026-09-12 eleven PRs were merged and the production
# checkout stayed on an earlier commit for hours. Nothing was broken: merging
# had simply been reported as shipping, three separate times, because the
# second step lived only in somebody's memory. The deploy-drift check that
# would have caught it ran Mon-Fri and every one of those merges landed on a
# Saturday (that timer is now daily — see quant-agent-drift-check.timer).
#
# The safety net catching it the next morning is not the fix. The fix is that
# there is no gap to catch: one command, or the deploy did not happen.
#
# USAGE
#   scripts/merge_and_deploy.sh <pr-number>
#   scripts/merge_and_deploy.sh --deploy-only     # box is behind, no PR to merge
#
# Deliberately NOT automatic on merge (no CI deploy hook): a deploy restarts
# the live API and this desk trades real orders on a paper account. It stays an
# action a person takes knowingly. What it must not stay is an action a person
# has to REMEMBER.
set -euo pipefail

# ONE DEPLOY AT A TIME. On 2026-10-13 two deploys started within a minute of
# each other: the second rebuilt the dashboard's work folder while the first
# was mid-build, so the first reported "npm run build failed" and "DEPLOY DID
# NOT LAND". A second caller now waits for the first to finish (each deploy is
# idempotent, so the waiter then re-deploys whatever trunk holds by then).
DEPLOY_LOCK="${QAMC_DEPLOY_LOCK:-/tmp/qamc-deploy.lock}"
exec 9>>"${DEPLOY_LOCK}"
if ! flock -n 9; then
  echo "==> another deploy is running; waiting for it to finish" >&2
  flock 9
fi

DEPLOY_ROOT=/home/qamc/quant-agent
API_UNIT=quant-agent-api.service
QAMC_UID="$(id -u qamc)"
# Where the repo keeps its units and where systemd actually reads them from.
# Installing a unit is a `cp`: a merged change to a .service/.timer, or a
# brand-new timer, lands in git and is NOT seen by systemd until it is copied
# here, `daemon-reload`ed and `enable`d (item 122). Before this the deploy
# only restarted the API unit, so unit changes shipped in name only.
UNIT_SRC="${DEPLOY_ROOT}/scripts/systemd"
UNIT_DIR=/home/qamc/.config/systemd/user
PAUSED_UNITS="${UNIT_SRC}/paused_units.yaml"

qamc_systemctl() {
  sudo -n -u qamc "XDG_RUNTIME_DIR=/run/user/${QAMC_UID}" systemctl --user "$@"
}

# True when the unit is on the declared paused list (scripts/systemd/
# paused_units.yaml). A paused unit is installed but deliberately NOT
# enabled; the deploy must not silently switch it back on.
unit_is_paused() {
  # Read as root: the operator cannot read the qamc-owned deploy root, and a
  # plain [[ -r ]] there answered "no paused list", i.e. "enable everything".
  if ! sudo -n test -r "${PAUSED_UNITS}"; then
    echo "PAUSED LIST UNREADABLE: ${PAUSED_UNITS} - refusing to enable units blind" >&2
    exit 1
  fi
  sudo -n grep -Eq "^[[:space:]]*-?[[:space:]]*unit:[[:space:]]*${1}[[:space:]]*$" \
    "${PAUSED_UNITS}"
}

# The deploy root is owned by qamc and is not readable by the operator, so a
# plain "${UNIT_SRC}"/*.timer glob matched NOTHING here and every new unit was
# skipped without a word. List the files as root instead, and refuse to treat
# "found none" as "nothing to install".
unit_sources() {
  local found
  found="$(sudo -n find "${UNIT_SRC}" -maxdepth 1 -type f \( -name '*.service' -o -name '*.timer' \) | sort)"
  if [[ -z "${found}" ]]; then
    echo "NO UNIT FILES FOUND in ${UNIT_SRC} - refusing to treat that as nothing to install" >&2
    exit 1
  fi
  echo "${found}"
}

install_units() {
  local src name changed=0
  local unit_list
  unit_list="$(unit_sources)" || exit 1
  for src in ${unit_list}; do
    name="$(basename "${src}")"
    # `cmp` exits non-zero for a missing target too, so a brand-new unit
    # takes the same path as a changed one. Idempotent: an unchanged unit
    # is left alone and triggers no reload.
    if sudo -n cmp -s "${src}" "${UNIT_DIR}/${name}"; then
      continue
    fi
    # Show what is being replaced BEFORE replacing it. A box copy that was
    # hand-edited is evidence the unit-drift check would otherwise report;
    # an unconditional overwrite would destroy it unread (docs/board_notes/
    # on item 122), so the diff goes into this run's output first.
    if sudo -n test -e "${UNIT_DIR}/${name}"; then
      echo "==> ${name} differs from the installed copy:"
      sudo -n diff -u "${UNIT_DIR}/${name}" "${src}" || true
    fi
    echo "==> installing ${name} into ${UNIT_DIR}"
    sudo -n install -o qamc -g qamc -m 0644 "${src}" "${UNIT_DIR}/${name}"
    changed=1
  done
  # daemon-reload + enable are REQUIRED for a unit change to take effect:
  # systemd keeps running the copy it parsed at the last reload, and a unit
  # file that is present but not enabled never fires on its own.
  if (( changed )); then
    echo "==> systemctl --user daemon-reload (unit files changed)"
    qamc_systemctl daemon-reload
  fi
  local unit_list
  unit_list="$(unit_sources)" || exit 1
  for src in ${unit_list}; do
    name="$(basename "${src}")"
    # Only units that declare an [Install] section can be enabled; the
    # oneshot services behind the timers deliberately carry none.
    sudo -n grep -q '^\[Install\]' "${src}" || continue
    if unit_is_paused "${name}"; then
      echo "==> ${name} is on paused_units.yaml — leaving it disabled"
      continue
    fi
    if qamc_systemctl is-enabled --quiet "${name}" 2>/dev/null; then
      continue
    fi
    # Timers get --now so a newly added schedule starts ticking today
    # rather than at the next login. Services are only enabled: the API is
    # restarted below and the rest are timer-driven oneshots.
    if [[ "${name}" == *.timer ]]; then
      echo "==> enabling ${name} (--now)"
      qamc_systemctl enable --now "${name}"
    else
      echo "==> enabling ${name}"
      qamc_systemctl enable "${name}"
    fi
  done
}

# Long-running desk services other than the API, derived from the unit files
# the deploy installs: a .service that is not oneshot and has no sibling
# .timer. Nothing here names a unit, so a new long-running service is picked
# up without editing this script. Timer-driven oneshots load fresh code on
# every run and need no restart. Paused units are left alone.
long_running_units() {
  local src name type unit_list names
  unit_list="$(unit_sources)" || exit 1
  names="$(for src in ${unit_list}; do basename "${src}"; done)"
  for src in ${unit_list}; do
    name="$(basename "${src}")"
    [[ "${name}" == *.service ]] || continue
    [[ "${name}" == "${API_UNIT}" ]] && continue
    grep -qxF "${name%.service}.timer" <<<"${names}" && continue
    unit_is_paused "${name}" && continue
    type="$(sudo -n grep -m1 -E '^Type=' "${src}" | cut -d= -f2 | tr -d '[:space:]')"
    [[ "${type:-simple}" == "oneshot" ]] && continue
    echo "${name}"
  done
}

# THE DASHBOARD NEVER BLOCKS OR HALF-APPLIES A TRADING DEPLOY.
#
# The dashboard bundle (src/api/static_cockpit/) is gitignored build output:
# committing it let frontend source merge without a rebuilt bundle, and a
# panel never appeared live. So every deploy builds it -- but in this order:
#
#   1. BUILD FIRST, off to the side: the target commit's frontend/ is
#      exported (git archive) into COCKPIT_WORK and built there, before the
#      live checkout is touched. The current bundle is copied aside too.
#   2. Checkout, units and restart run exactly as they did before the build
#      existed, whatever the build did.
#   3. A good build is swapped in. A failed one (no network for npm ci, wrong
#      node, a compile error) leaves the PREVIOUS bundle serving -- restored
#      from the copy if the checkout removed it, as the first deploy after
#      untracking does -- and the run ends with one loud line and exit
#      status 3: trading code deployed, dashboard stale. The previous
#      bundle's BUILT_FROM_COMMIT (or its absence) stays, so the served-
#      commit check below reports it stale rather than current.
COCKPIT_DIR="${DEPLOY_ROOT}/src/api/static_cockpit"
COCKPIT_STAMP=BUILT_FROM_COMMIT
# A sibling of the deploy root: same filesystem, so the swap is a rename,
# and outside the checkout, so git never sees it. Wiped at each build; on a
# failure it keeps build.log until the next deploy.
COCKPIT_WORK="${DEPLOY_ROOT}-cockpit-build"
DASHBOARD_PROBLEM=""

qamc_sh() {
  sudo -n -H -u qamc env ROOT="${DEPLOY_ROOT}" WORK="${COCKPIT_WORK}" \
    LIVE="${COCKPIT_DIR}" STAMP="${COCKPIT_STAMP}" "$@" bash -euo pipefail -c "${QAMC_SCRIPT}"
}

# Step 1. Never exits the script: any failure only sets DASHBOARD_PROBLEM.
build_cockpit() {
  local target="$1"
  echo "==> building the dashboard for ${target} off to the side (as qamc)"
  QAMC_SCRIPT='
    rm -rf "${WORK}"
    mkdir -p "${WORK}/src"
    fail() { echo "$1" > "${WORK}/reason"; exit 1; }
    # Copy the bundle being served now aside, so a checkout that removes it
    # can never leave the dashboard 404ing.
    if [[ -d "${LIVE}" ]]; then cp -a "${LIVE}" "${WORK}/prev"; fi
    git -C "${ROOT}" archive "${TARGET}" frontend | tar -x -C "${WORK}/src" \
      || fail "could not export frontend/ at ${TARGET}"
    cd "${WORK}/src/frontend"
    need="$(tr -d "[:space:]" < .nvmrc)"; need="${need%%.*}"
    have="$(node -v 2>/dev/null || echo none)"
    [[ "${have}" == "v${need}."* ]] \
      || fail "wrong node version (found ${have}, need ${need}.x)"
    npm ci --no-audit --no-fund > "${WORK}/build.log" 2>&1 \
      || fail "npm ci failed (log: ${WORK}/build.log)"
    npm run build -- --outDir "${WORK}/out" --emptyOutDir >> "${WORK}/build.log" 2>&1 \
      || fail "npm run build failed (log: ${WORK}/build.log)"
    [[ -s "${WORK}/out/index.html" ]] || fail "the build wrote no index.html"
    echo "${TARGET}" > "${WORK}/out/${STAMP}"
  '
  if ! qamc_sh TARGET="${target}"; then
    DASHBOARD_PROBLEM="$(sudo -n cat "${COCKPIT_WORK}/reason" 2>/dev/null || echo "dashboard build failed")"
    echo "==> dashboard build FAILED (${DASHBOARD_PROBLEM}); the trading deploy continues" >&2
  fi
}

# Step 3. Swap a good build in, or make sure the previous bundle still
# serves. Never exits the script.
install_cockpit() {
  if [[ -z "${DASHBOARD_PROBLEM}" ]]; then
    QAMC_SCRIPT='
      if [[ -d "${LIVE}" ]]; then mv "${LIVE}" "${WORK}/old"; fi
      mv "${WORK}/out" "${LIVE}"
      rm -rf "${WORK}"
    '
    if qamc_sh; then
      echo "==> dashboard bundle swapped in"
      return 0
    fi
    DASHBOARD_PROBLEM="the built bundle could not be swapped in"
  fi
  QAMC_SCRIPT='
    if [[ ! -d "${LIVE}" && -d "${WORK}/prev" ]]; then cp -a "${WORK}/prev" "${LIVE}"; fi
    [[ -d "${LIVE}" ]]
  '
  if qamc_sh; then
    echo "==> previous dashboard bundle left in place"
  else
    echo "==> NO dashboard bundle could be kept: /cockpit/ will 404 until a build succeeds" >&2
  fi
}

deploy() {
  echo "==> fetching origin/main into ${DEPLOY_ROOT}"
  sudo -n -H -u qamc git -C "${DEPLOY_ROOT}" fetch origin main
  local target name
  target="$(sudo -n -H -u qamc git -C "${DEPLOY_ROOT}" rev-parse origin/main)"
  # Before the live checkout is touched; never stops the deploy.
  build_cockpit "${target}"
  # The production checkout runs on a DETACHED HEAD on purpose — it is pinned
  # to an exact commit, not tracking a branch. `git pull` there fails with a
  # confusing "you are not currently on a branch", which is its own small trap.
  echo "==> checking out origin/main (detached, as production always runs)"
  sudo -n -H -u qamc git -C "${DEPLOY_ROOT}" checkout --detach origin/main
  install_cockpit
  # Units first, so the restart below picks up an API unit change too.
  install_units
  echo "==> restarting ${API_UNIT}"
  qamc_systemctl restart "${API_UNIT}"
  # Every other long-running unit (e.g. the owner switch) keeps the code it
  # imported at start, so it must be restarted too or a merge never goes live.
  local long_running
  long_running="$(long_running_units)" || exit 1
  for name in ${long_running}; do
    echo "==> restarting ${name}"
    qamc_systemctl restart "${name}"
  done

  local deployed expected built
  deployed="$(sudo -n -H -u qamc git -C "${DEPLOY_ROOT}" rev-parse HEAD)"
  expected="$(git ls-remote origin refs/heads/main | cut -f1)"
  if [[ "${deployed}" != "${expected}" ]]; then
    echo "DEPLOY DID NOT LAND: box at ${deployed}, origin/main is ${expected}" >&2
    exit 1
  fi
  echo "==> deployed ${deployed}"
  # Served-commit check: the dashboard is current only if its stamp names
  # the commit the box now runs.
  built="$(sudo -n cat "${COCKPIT_DIR}/${COCKPIT_STAMP}" 2>/dev/null || true)"
  if [[ "${built}" != "${deployed}" && -z "${DASHBOARD_PROBLEM}" ]]; then
    DASHBOARD_PROBLEM="served bundle is from '${built:-unknown}', not ${deployed}"
  fi
}

# Exit status 3 = trading code deployed, dashboard stale. Called last.
finish() {
  if [[ -n "${DASHBOARD_PROBLEM}" ]]; then
    echo "DASHBOARD NOT UPDATED: ${DASHBOARD_PROBLEM}; trading code deployed" >&2
    exit 3
  fi
  exit 0
}

if [[ "${1:-}" == "--deploy-only" ]]; then
  deploy
  finish
fi

PR="${1:?usage: merge_and_deploy.sh <pr-number> | --deploy-only}"

# Read the state BEFORE merging, so "I merged it" can be told apart from
# "somebody merged it days ago". Without this, a mistyped PR number that
# happens to name an already-merged PR sails through the check below and
# deploys whatever origin/main holds right now — including work being
# deliberately held back.
was="$(gh pr view "${PR}" --json state -q .state)"
if [[ "${was}" == "MERGED" ]]; then
  echo "PR #${PR} was ALREADY merged before this run — refusing." >&2
  echo "      Use --deploy-only if you meant to push origin/main to the box." >&2
  exit 1
fi

echo "==> merging PR #${PR}"
# NO `--delete-branch`. The repository already has `delete_branch_on_merge`
# enabled, so GitHub removes the head branch itself and the flag adds only a
# LOCAL delete — which fails whenever any worktree still has that branch
# checked out, the normal state here because agents leave worktrees behind.
# Under `set -euo pipefail` that failure killed the run AFTER the squash had
# landed on GitHub but BEFORE `deploy` ran, so origin/main moved and the box
# silently stayed behind while the output read like a failure. Dropping the
# redundant flag removes that abort at its source and keeps gh's exit status
# meaning something, rather than swallowing every gh error to tolerate one.
# Stale local branches are cleaned up separately, not from the deploy path.
gh pr merge "${PR}" --squash

# Verify rather than assume. A squash-merge can be refused (branch behind,
# a check still pending) and gh's exit status is not a reliable proxy.
state="$(gh pr view "${PR}" --json state -q .state)"
if [[ "${state}" != "MERGED" ]]; then
  echo "PR #${PR} is ${state}, not MERGED — nothing deployed." >&2
  exit 1
fi

deploy

# `deploy` pins the box to whatever origin/main is at the moment it looks,
# which is a moving target: branch protection is off and sessions merge in
# parallel, so another PR can land between this merge and that checkout.
# Assert that THIS PR's merge commit is actually an ancestor of what the box
# now runs, so the closing line cannot claim a deploy of code that did not
# reach it.
merge_sha="$(gh pr view "${PR}" --json mergeCommit -q .mergeCommit.oid)"
deployed_sha="$(sudo -n -H -u qamc git -C "${DEPLOY_ROOT}" rev-parse HEAD)"
sudo -n -H -u qamc git -C "${DEPLOY_ROOT}" fetch -q origin "${merge_sha}" 2>/dev/null || true
if ! sudo -n -H -u qamc git -C "${DEPLOY_ROOT}" merge-base --is-ancestor \
       "${merge_sha}" "${deployed_sha}" 2>/dev/null; then
  echo "PR #${PR} merged as ${merge_sha}, but the box is on ${deployed_sha}," >&2
  echo "      which does not contain it. Re-run --deploy-only." >&2
  exit 1
fi
echo "==> PR #${PR} merged AND deployed"
finish
