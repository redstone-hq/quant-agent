# QAMC Mission Control — cockpit frontend

React + TypeScript + Vite + Tailwind, plus TradingView Lightweight Charts
for the price panel. This is the source for the cockpit served by
`src/api/server.py`'s `/cockpit` mount (`src/api/static_cockpit/`).

Authorized in `AGENTS.md`'s shipped-tranche acceptance contract ("Final
Mission Control direction" / Workstream B) as a bounded, in-repo frontend replacement for the legacy
`/ui` dashboard — evaluated against React+Vite+Tailwind+TradingView
Lightweight Charts (the original QAMC design direction, see the Stage 0
donor inventory in Git history) rather than continuing the vanilla-JS
prototype that preceded this build (preserved in Git history at commit
`73c68bf`, with its Stage 6 acceptance evidence likewise preserved in
Git history — its information architecture carried over into
this rebuild, its vanilla-JS implementation did not).

## Deployment model — built at deploy, never committed

The compiled output (`../src/api/static_cockpit/`) is plain static
HTML/CSS/JS served by the same `StaticFiles` mount `/ui` already uses. It
is **build output and is gitignored**: committing it let frontend changes
merge without a rebuilt bundle, so a panel never appeared live.

- **Deploy** (`scripts/merge_and_deploy.sh`) runs `npm ci` + `npm run build`
  as `qamc` after checkout and BEFORE the restart, into a temp folder that is
  swapped in only on success; a failed build aborts the deploy with the old
  services and old bundle still running. The bundle carries a
  `BUILT_FROM_COMMIT` stamp the deploy checks against the checkout.
- **Branch preview** (`ops/preview/branch_preview.py`) builds the same way at
  startup and shows the build error at `/cockpit/` if it fails.
- **CI** (`frontend` job in `.github/workflows/test.yml`) builds, runs
  `npm test`, and runs the bundle checks in `tests/test_api_safety.py`.
- **Node** is pinned in `.nvmrc` (and `engines`); the build refuses any other
  version. There is no new server, service, or backend dependency.

## Commands

```bash
npm ci            # first time / after a dependency change (lockfile-exact)
npm run dev       # dev server at :5173, proxies API calls to the local
                   # read-only Mission Control API on :8800 (see vite.config.ts)
npm run build     # type-checks, then builds into src/api/static_cockpit/
npm run preview   # serve the built output locally for a final check
```

## Structure

- `src/api/client.ts` — typed fetch wrappers mirroring
  `src/api/schemas.py`'s Pydantic response models exactly (field-for-field
  — keep the two in sync by hand when the backend contract changes).
- `src/components/` — one file per cockpit panel/card, plus the modal
  shell used for run/candidate drill-down.
- `src/App.tsx` — layout + polling orchestration (mirrors the legacy
  dashboards' `REFRESH_MS` polling posture — no websocket, no new
  transport).

## Desktop workspace: how panels are sized

The desktop cockpit is a Dockview workspace laid out inside a box whose
height is pinned to the viewport. Dockview redistributes the space inside
that box; it cannot grow the box itself. Two consequences, and what the
code does about them:

- **A sash only exists between two rows.** The bottom-most row has nothing
  under it to drag, which is why panels could only ever be expanded
  upwards. `DesktopCockpitWorkspace` adds a grip on the workspace's own
  bottom edge that grows the box and hands the extra height to the bottom
  row; the page scrolls. Extra height is persisted, so a reload keeps it.
- **Scroll policy belongs to the panel, not to the slot.** Panels are
  draggable, so "the bottom ones scroll differently" is not a stable rule.
  `FIT_PANELS` lists the panels that are sized by their content: no
  internal vertical scrollbar, the row grows to fit them, and the page
  scrolls. Everything else keeps a normal internal scrollbar. Both
  behaviours travel with the panel wherever it is dragged.

Content sets a floor, not a fixed height: a content-sized panel can be
dragged taller and keeps that height, and only gives height back when its
content itself shrinks.

Blotter column widths are stored as fractions of the table width that sum
to 1, never as pixels. `table-layout: fixed` does not clamp a table to its
specified width — the used width is the larger of the specified width and
the sum of the column widths — so pixel widths summed past the panel and
the trailing columns were clipped out of reach.

## Boundaries this frontend must not cross

Same as every other Mission Control surface (`docs/architecture/
MISSION_CONTROL_API.md`, `docs/OUTCOME.md`): read-only, no broker-write
controls (no PAUSE/KILL/trade buttons — those remain explicitly out of
scope), no fabricated data, no second trading-memory system. It consumes
only the existing GET-only API; any new backend field/endpoint it needs
is added there under the same isolation invariants, never inline here.
