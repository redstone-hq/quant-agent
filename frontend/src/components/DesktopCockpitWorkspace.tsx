import { useCallback, useEffect, useRef, useState } from "react";
import { DockviewReact, type DockviewApi, type DockviewReadyEvent, type IDockviewPanelProps } from "dockview-react";
import "dockview-react/dist/styles/dockview.css";
import { useCockpitWorkspace } from "../context/CockpitWorkspaceContext";
import { useSupportWorkspace } from "../context/SupportWorkspaceContext";
import { useModalActions } from "../context/ModalContext";
import { CandidateRail } from "./CandidateRail";
import { PriceChartPanel } from "./PriceChartPanel";
import { ChartSymbolBar } from "./ChartSymbolBar";
import { PositionsPanel } from "./PositionsPanel";
import { HoldingsStrip } from "./HoldingsStrip";
import { HeroBand } from "./HeroBand";
import { LiquidityStrip } from "./LiquidityPanel";
import { MarginInterestStrip } from "./MarginInterestPanel";
import { TodaySessionsStrip } from "./TodaySessionsStrip";
import { DecisionStateBanner } from "./DecisionStateBanner";
import {
  BOTTOMS_FLOOR_PX,
  CHART_FLOOR_PX,
  COCKPIT_LAYOUT_KEY,
  HOLDINGS_DEFAULT_PX,
  HOLDINGS_FLOOR_PX,
  clearPersistedFit,
  clearPersistedGrowth,
  nextBottomsSashState,
  readPersistedFit,
  readPersistedGrowth,
  sashSitsBetween,
  writePersistedFit,
  writePersistedGrowth,
} from "../lib/cockpitSash";
import { OrdersPanel } from "./OrdersPanel";
import { TradesPanel } from "./TradesPanel";
import { WhyPanel } from "./WhyPanel";
import { RunsPanel } from "./RunsPanel";
import { DirectionalBiasPanel } from "./DirectionalBiasPanel";
import { MissedOpportunitiesPanel } from "./MissedOpportunitiesPanel";
import { TradeReviewPanel } from "./TradeReview/TradeReviewPanel";
import { SearchPanel } from "./SearchPanel";
import { HealthPanel } from "./HealthPanel";
import { Panel, StateMessage } from "./ui/Panel";

// Scroll policy lives with the PANEL, not with the slot it happens to
// occupy (owner correction 2026-09-17: every panel here is draggable, so
// "the bottom two" stops being true the moment he rearranges anything).
//
// Panels listed here are sized BY THEIR CONTENT: no internal vertical
// scrollbar, the row they sit in grows to fit them, and the page scrolls
// instead — which is exactly what the owner asked for ("can't you just
// make the page bigger and get rid of the scroll bars"). Drag one of
// these to the top of the workspace and it keeps behaving this way.
//
// Everything NOT listed keeps a normal internal scrollbar. Holdings and
// Positions are deliberately absent: they are lists that keep growing as
// he holds more names, and he said explicitly that their scrollbars are
// fine and easy to read. Chart is absent for a different reason — it is
// a canvas that fills whatever box it is given and has no natural
// content height to fit to.
//
// A THIRD policy joined those two on 2026-09-18 (owner request,
// board item 103): "both". Vertical and horizontal scrollbars, internal
// to that one panel. It exists because Trades is not shaped like the
// other tables — it carries sixteen columns (time, symbol, action, a
// full reasoning paragraph, fill status, filled qty, fill price,
// realized P&L, recorded stop, take profit, run, decision, conviction,
// requested risk, allocated risk, decision model). Fitting all of that
// to the panel's width crushed every column to a few characters, and
// fitting it to its HEIGHT grew the page to roughly six thousand pixels.
// Scrolled in both directions, the columns render at their natural
// widths and the row stays inside the panel.
//
// Expressed as a per-panel policy, not a check for the name "trades",
// so moving Trades anywhere in the workspace takes its scrolling with
// it and any future panel can opt in by naming its policy here.
type PaneScroll = "fit" | "scroll-y" | "scroll-both";

/** Scroll policy per panel id. Anything unlisted is "scroll-y" — a
 * normal internal vertical scrollbar, which is what Holdings, Positions
 * and the rest keep (owner is happy with those; do not change them). */
const PANE_SCROLL: Record<string, PaneScroll> = {
  orders: "fit",
  candidates: "fit",
  why: "fit",
  trades: "scroll-both",
};

const paneScroll = (id: string): PaneScroll => PANE_SCROLL[id] ?? "scroll-y";

/** Panels sized BY THEIR CONTENT — the set the workspace measures and
 * grows a row for. Derived from PANE_SCROLL so the policy is declared in
 * exactly one place. */
const FIT_PANELS = new Set(
  Object.entries(PANE_SCROLL)
    .filter(([, policy]) => policy === "fit")
    .map(([id]) => id),
);

/** Wrapper classes for a pane whose panel scrolls internally. */
const SCROLL_PANE = "h-full min-w-0 overflow-x-hidden overflow-y-auto p-2";
/** Wrapper classes for a pane that scrolls in BOTH directions. `h-full`
 * (not `min-h-full`) so the pane is the panel's box and the vertical
 * scrollbar is the panel's own; `overflow-auto` so the horizontal one
 * appears exactly when the table is genuinely wider than the panel and
 * not before. */
const SCROLL_BOTH_PANE = "h-full min-w-0 overflow-auto p-2";
/** Wrapper classes for a content-sized pane. `min-h-full` so a short
 * panel still paints the full group background; no `overflow-y-auto`, so
 * its scrollHeight reports the height the row actually needs. */
const FIT_PANE = "min-h-full min-w-0 overflow-x-hidden overflow-y-visible p-2";

/** One pane wrapper, whose scrolling comes from the panel's own policy in
 * PANE_SCROLL above rather than from each call site repeating a class
 * string. `data-fit-pane` is the marker the auto-fit measurement below
 * looks for, so it is set by the same switch — a "fit" panel can never be
 * left unmeasured (or a scrolling one measured) by someone editing one of
 * the two and forgetting the other. */
function Pane({ id, extra, children }: { id: string; extra?: string; children: React.ReactNode }) {
  const policy = paneScroll(id);
  const base = policy === "fit" ? FIT_PANE : policy === "scroll-both" ? SCROLL_BOTH_PANE : SCROLL_PANE;
  return (
    <div data-fit-pane={policy === "fit" ? "true" : undefined} className={extra ? `${base} ${extra}` : base}>
      {children}
    </div>
  );
}

// Item 2 of cockpit pass 3: the middle column of the bottom row used to
// be deliberately empty by default — "free for him to populate" in the
// owner's own words, not a panel we picked for him. Built from the same
// approved Panel/StateMessage primitives every other pane in this
// workspace uses (no bespoke graphic), reusing the existing ●/◐/■/○
// glyph vocabulary (see ui/Panel.tsx's StateMessage) rather than
// inventing a new icon for "nothing here yet."
//
// v6 update: the owner asked to get that column's width back for
// Positions/Orders instead, so buildDefaultLayout no longer places this
// panel — see the STORAGE_KEY comment below. The component stays defined
// and registered in COMPONENTS regardless: a saved layout can still carry
// a "workspaceSlot" entry (e.g. one written under this key by an earlier
// build before the code caught up, or before a future default change),
// and dockview's fromJSON throws on a panel id with no registered
// component. Keeping this around is what makes loading such a layout safe
// rather than a hard failure. It is retained purely for that
// compatibility — it is not placed by default any more.
function WorkspaceSlotPane() {
  return (
    <Pane id="workspaceSlot">
      <Panel title="Workspace">
        <StateMessage
          hero
          glyph="○"
          text="Empty by default — drag any panel's tab here to fill this column, or use “Reset layout” to restore the default."
        />
      </Panel>
    </Pane>
  );
}

// Item 1 of the cockpit trader rework: Positions is the panel a trader
// lands on, not one tab among several — see PositionsPane below and
// buildDefaultLayout's placement of it as the leftmost, active-by-default
// group.
function PositionsPane() {
  const state = useSupportWorkspace();
  return (
    <Pane id="positions">
      <PositionsPanel
        positions={state.positions}
        error={state.positionsError}
        loading={state.positionsLoading}
        updatedAt={state.positionsUpdatedAt}
        onSelectSymbol={state.onSelectPositionSymbol}
        onInspectSymbol={state.onInspectPositionSymbol}
      />
    </Pane>
  );
}

function HoldingsPane() {
  const state = useSupportWorkspace();
  return (
    <Pane id="holdings">
      <HoldingsStrip
        positions={state.positions}
        error={state.positionsError}
        updatedAt={state.positionsUpdatedAt}
        onSelectSymbol={state.onSelectPositionSymbol}
        onInspectSymbol={state.onInspectPositionSymbol}
        variant="panel"
      />
    </Pane>
  );
}

function AccountPane() {
  const support = useSupportWorkspace();
  const cockpit = useCockpitWorkspace();
  return (
    <Pane id="account">
      <HeroBand
        account={support.account}
        accountError={support.accountError}
        positions={support.positions}
        regime={cockpit.regime}
        variant="panel"
      />
      <LiquidityStrip
        account={support.account}
        accountError={support.accountError}
        positions={support.positions}
        variant="panel"
      />
      <MarginInterestStrip
        account={support.account}
        accountError={support.accountError}
        variant="panel"
      />
    </Pane>
  );
}

function SessionsPane() {
  const cockpit = useCockpitWorkspace();
  return (
    <Pane id="sessions">
      <TodaySessionsStrip
        runs={cockpit.todaysRuns}
        funnels={cockpit.todaysFunnels}
        trades={cockpit.todaysTrades}
        loading={cockpit.loading}
        error={cockpit.error}
        selectedRunId={cockpit.selectedRunId}
        autoFollow={cockpit.autoFollow}
        onSelect={cockpit.onSelectSession}
        onFollowLatest={cockpit.onFollowLatest}
        onSelectTrade={cockpit.onSelectTrade}
        variant="panel"
      />
      <DecisionStateBanner
        funnel={cockpit.funnel}
        trades={cockpit.todaysTrades}
        loading={cockpit.loading}
        error={cockpit.error}
        updatedAt={cockpit.updatedAt}
        variant="panel"
      />
    </Pane>
  );
}

function CandidatesPane() {
  const state = useCockpitWorkspace();
  return <Pane id="candidates"><CandidateRail fit funnel={state.funnel} loading={state.loading} error={state.error} updatedAt={state.updatedAt} selectedSymbol={state.chartSymbol} onSelectSymbol={state.onSelectSymbol} /></Pane>;
}

// Above the candles: company name + ticker + Lifecycle. Holding figures
// stay on Positions and on the chart's own entry/stop lines. The decision
// one-liner lives inside Lifecycle, not as a second strip here.
function ChartPane() {
  const state = useCockpitWorkspace();
  // Read-only broker positions/orders/trades, sourced from the same
  // SupportWorkspace state PositionsPane/OrdersPane render — needed so the
  // chart's average-entry (entryPriceLine) and protective-stop
  // (positionStopLine) reference lines actually have data to draw from in
  // the desktop Dockview workspace, not just the mobile/iPad pane.
  const support = useSupportWorkspace();
  const { openCandidateDetail } = useModalActions();
  const candidate = state.funnel?.candidates.find((item) => item.symbol === state.chartSymbol);
  return (
    <div className="flex h-full min-w-0 flex-col overflow-hidden px-1 py-0">
      <ChartSymbolBar
        symbol={state.chartSymbol}
        previousSymbol={state.previousChartSymbol}
        onGoBack={state.onGoBackSymbol}
        canOpenLifecycle={!!(candidate && state.funnel)}
        onOpenLifecycle={() => {
          if (candidate && state.funnel) openCandidateDetail(state.funnel.run_id, candidate.symbol);
        }}
      />
      {/* `overflow-hidden` on the column plus `min-h-0` here so the chart
          shrinks to the panel's real height instead of growing a nested
          scrollbar against the identity row. */}
      <div className="min-h-0 flex-1">
        <PriceChartPanel
          symbol={state.chartSymbol}
          trades={state.chartTrades}
          positions={support.positions}
          openOrders={support.openOrders}
          positionTrades={support.trades}
          onUserInteraction={state.onChartInteraction}
        />
      </div>
    </div>
  );
}

function OrdersPane() {
  const state = useSupportWorkspace();
  return <Pane id="orders"><OrdersPanel fit orders={state.orders} error={state.ordersError} loading={state.ordersLoading} status={state.orderStatus} onStatusChange={state.onOrderStatusChange} onInspect={state.onInspectOrder} onSelectSymbol={state.onSelectPositionSymbol} trades={state.trades} /></Pane>;
}

// Trades is the one panel on the "both scrollbars" policy — see
// PANE_SCROLL above for why (sixteen columns, and ~6,000px of page when
// it was grown to fit). No `data-fit-pane` marker and no `fit` on the
// panel: this pane must NOT be measured and grown, it must stay inside
// its box and scroll. `scrollX` lets its table size columns to their
// content instead of being squeezed into the panel width, which is what
// gives the horizontal scrollbar something to scroll.
function TradesPane() {
  const state = useSupportWorkspace();
  return <Pane id="trades"><TradesPanel scrollX trades={state.trades} error={state.tradesError} loading={state.tradesLoading} onInspect={state.onInspectTrade} onSelectSymbol={state.onSelectPositionSymbol} /></Pane>;
}

// The owner's own proposal, approved 2026-09-18: a tab beside Candidates
// that answers "why do we hold this" for whatever symbol was last
// clicked, anywhere in the cockpit. It reads the already-fetched answer
// off CockpitWorkspaceContext rather than fetching for itself — App.tsx
// fetches on every chart-symbol change so the content is already here
// when he switches to the tab, exactly as he asked.
function WhyPane() {
  const state = useCockpitWorkspace();
  return (
    <Pane id="why">
      <WhyPanel
        fit
        symbol={state.chartSymbol}
        why={state.holdingWhy}
        error={state.holdingWhyError}
        loading={state.holdingWhyLoading}
      />
    </Pane>
  );
}

function RunsPane() { const state = useSupportWorkspace(); return <Pane id="runs"><RunsPanel runs={state.runs} error={state.runsError} loading={state.runsLoading} /></Pane>; }
function BiasPane() { return <Pane id="bias"><DirectionalBiasPanel /></Pane>; }
function ReviewPane() { return <Pane id="review"><TradeReviewPanel /></Pane>; }
// Was wired to a callback that conditionally opened the candidate-detail
// modal depending on which session happened to be selected in the
// Sessions strip — unrelated to the missed-opportunity row being clicked
// — so the identical click did two different things with no on-screen
// explanation. Routed to the same modal-free callback PositionsPane uses
// above: chart the symbol, open nothing (governing principle, App.tsx's
// chartPositionSymbol).
function MissedPane() { const state = useSupportWorkspace(); return <Pane id="missed"><MissedOpportunitiesPanel onSelectSymbol={state.onSelectPositionSymbol} /></Pane>; }

// Item 13 (cockpit trader rework): System and Search — named by the owner
// as "not trading" — used to each be their own top-level tab in this
// workspace's chart-group tab strip. Folded into one Diagnostics tab
// instead of standing on their own; same two panels, just one click away
// together rather than two clicks apart.
function DiagnosticsPane() {
  const state = useSupportWorkspace();
  return (
    <Pane id="diagnostics" extra="flex flex-col gap-3">
      <HealthPanel health={state.health} error={state.healthError} />
      <SearchPanel onSelectSymbol={state.onSelectPositionSymbol} />
    </Pane>
  );
}

const COMPONENTS: Record<string, React.FunctionComponent<IDockviewPanelProps>> = {
  positions: PositionsPane,
  holdings: HoldingsPane,
  account: AccountPane,
  sessions: SessionsPane,
  candidates: CandidatesPane,
  chart: ChartPane,
  orders: OrdersPane,
  trades: TradesPane,
  why: WhyPane,
  runs: RunsPane,
  bias: BiasPane,
  review: ReviewPane,
  missed: MissedPane,
  diagnostics: DiagnosticsPane,
  workspaceSlot: WorkspaceSlotPane,
};

// Bumped v8 -> v9 (owner lock): remaining page chrome — NLV/performance,
// liquidity, sessions, decision banner — leaves the header and becomes
// Dockview panels (Account, Sessions) beside Holdings above the chart.
// v8 blobs would keep those sections as fixed top-of-page strips.
//
// Bumped v7 -> v8 (owner lock): Holdings leaves the fixed header and
// becomes a Dockview panel above the chart — movable, dockable, resizable
// like Positions/Orders. The chart-vs-bottoms sash is two-way (borrow from
// the row above or the row below, then grow the page only after the
// bottoms floor). v7 blobs would keep Holdings out of the workspace.
//
// Bumped v6 -> v7 (hierarchy pass): default bottoms height drops from
// the 340 "header + 5 rows" start to the 260 floor so the chart row is
// the largest region on first load / Reset layout. v6 blobs would
// otherwise keep the shorter chart. Reset layout still restores this
// shape; the sash and rearrange behavior are unchanged.
//
// Bumped v5 -> v6 (owner request, direct): the bottom row's middle
// "Workspace" slot — genuinely empty by default, see WorkspaceSlotPane
// above — was eating roughly a third of the bottom row's width while
// showing nothing. The owner asked for a plain two-column bottom row
// instead (Positions | Orders, split evenly) so those two panels get that
// width back; buildDefaultLayout no longer adds a workspaceSlot panel at
// all. As with every previous bump, a v5 layout persisted from
// localStorage is not being migrated or read under this key — it simply
// stops being looked up, which is the point: without bumping the key,
// every browser that already has a saved v5 layout (three columns, empty
// middle) would keep opening to that shape and never see the new
// two-column default. WorkspaceSlotPane's component registration is kept
// regardless (see its comment above) purely so a layout blob that does
// reference "workspaceSlot" can still be restored via fromJSON without
// throwing on an unknown component id.
//
// Bumped v4 -> v5 (cockpit pass 3, item 2): the default workspace shape
// changed from one row of three columns (Positions | Chart | Orders) to
// two rows — a full-width Chart row on top, then a Positions/free/Orders
// three-column row below (see buildDefaultLayout). A v4 layout persisted
// from localStorage is still a perfectly valid dockview shape (nothing
// referenced by it was removed), so this bump exists purely to change
// what EVERY BROWSER sees as the default on first load / after "Reset
// layout" — without it, anyone who already has a saved v4 layout would
// keep seeing the old one-row shape and never notice the new default
// exists. Existing saved layouts are otherwise untouched: dockview state
// is genuinely additive/positional, not a fixed schema, so a v4 blob
// would have loaded fine under this key too. (v3 -> v4 / v2 -> v3
// history: the Decision Room panel was removed entirely — its content
// lives in Lifecycle (summarizeDecision) and the Research Desk
// DecisionDeltaPanel; Positions
// moved from a background tab inside the chart group to its own leftmost,
// active-by-default group; Liquidity left the workspace entirely for a
// compact header row — item 9; System+Search merged into one Diagnostics
// panel — item 13.)
const STORAGE_KEY = COCKPIT_LAYOUT_KEY;

/** Panels added to the cockpit AFTER a layout was already saved.
 *
 * Dockview restores exactly what was serialised, so a brand-new panel
 * would simply never appear for anyone with a saved arrangement — which
 * is everyone who has used the cockpit. The alternative fix is bumping
 * COCKPIT_LAYOUT_KEY, and that throws the owner's own arrangement away;
 * he rearranges these panels deliberately and has asked us not to keep
 * resetting things that work. So instead each late panel names the
 * neighbour it should sit beside, and onReady adds it as a background tab
 * in that group if the restored layout has no panel with its id. Falls
 * back to the chart group when the named neighbour was itself closed. */
const LATE_PANELS: { id: string; title: string; beside: string }[] = [
  { id: "why", title: "Why", beside: "candidates" },
  { id: "review", title: "Trade Review", beside: "bias" },
];

// Item 2 of cockpit pass 3, revised per direct owner request ("a better
// DEFAULT, not a lock" — every panel below stays exactly as
// movable/dockable/resizable as it always was; this only changes what a
// brand-new session (or "Reset layout") starts from). Originally: chart
// alone across the top, then a three-column row underneath with a
// genuinely empty middle column for the owner to fill himself. He later
// asked for that middle column back as width for Positions and Orders
// instead — chart full width on top because it's the first thing he
// looks at, then just two columns underneath, Positions left and Orders
// right, split evenly so each gets real horizontal room. He can still
// rearrange everything himself via the tabs; this only changes the
// starting shape.
function buildDefaultLayout(api: DockviewApi) {
  // Top row: the chart, full width — nothing else in this row.
  // minimumWidth is dockview's floor for a column, kept low so a panel
  // can still be dragged beside the chart at a normal desktop width
  // rather than only when the window is extra-wide.
  api.addPanel({ id: "chart", component: "chart", title: "Chart", minimumWidth: 80, minimumHeight: CHART_FLOOR_PX });
  // The non-trading analysis panels ride along as background tabs on the
  // chart group (unchanged from before this pass) rather than moving to
  // the bottom row — they pair conceptually with "studying a symbol/run",
  // not with the Positions/Orders tables.
  for (const [id, title] of [["missed", "Missed"], ["runs", "Runs"], ["bias", "Directional Bias"], ["review", "Trade Review"], ["diagnostics", "Diagnostics"]]) {
    api.addPanel({ id, component: id, title, position: { referencePanel: "chart", direction: "within" }, inactive: true });
  }

  // Top row: Holdings | Account | Sessions — real Dockview panels, not
  // header chrome — so the operator can move/dock/resize them H and V
  // like Positions/Orders. Short default so the chart stays the stage.
  api.addPanel({
    id: "holdings",
    component: "holdings",
    title: "Holdings",
    position: { referencePanel: "chart", direction: "above" },
    initialHeight: HOLDINGS_DEFAULT_PX,
    minimumHeight: HOLDINGS_FLOOR_PX,
    minimumWidth: 80,
  });
  api.addPanel({
    id: "account",
    component: "account",
    title: "Account",
    position: { referencePanel: "holdings", direction: "right" },
    initialHeight: HOLDINGS_DEFAULT_PX,
    minimumHeight: HOLDINGS_FLOOR_PX,
    minimumWidth: 80,
  });
  api.addPanel({
    id: "sessions",
    component: "sessions",
    title: "Sessions",
    position: { referencePanel: "account", direction: "right" },
    initialHeight: HOLDINGS_DEFAULT_PX,
    minimumHeight: HOLDINGS_FLOOR_PX,
    minimumWidth: 80,
  });

  // Bottom row: Positions (left) | Orders (right) — a real second row
  // (direction: "below"), not more tabs folded into the chart group, so
  // it gets its own genuine height AND its own independent resize handle
  // against the chart row above it — this is dockview's own native
  // sibling-panel resize sash (--dv-sash-color/--dv-active-sash-color in
  // styles/index.css), not a manual CSS hack; dragging the thin bar
  // between the chart row and this row resizes both, with a proper
  // resize cursor and highlight on hover, exactly like any other dockview
  // split. Two columns only, no fixed pixel width on either side: leaving
  // both without an initialWidth lets dockview split the row evenly
  // between them.
  //
  // Owner override 2026-09-10 ("let's make the chart bigger, which means
  // I sacrifice the positions and orders panel"): initialHeight dropped
  // from 480 to 230 — a real but deliberately modest default (enough for
  // a table header plus ~2-3 data rows; see PositionsPanel/DataTable's
  // own row height, nothing new introduced here) so the chart row above
  // gets the majority of whatever height the workspace box actually has
  // by default. This is a STARTING size only, per dockview's own resize
  // sash above — the operator can drag it taller at any time, and that
  // choice persists via the existing onDidLayoutChange localStorage save
  // below, same as every other panel arrangement in this workspace.
  //
  // Fix (owner bug report, 2026-09-10, round 3): the sash above had NO
  // floor at all — dragging the chart bigger could crush this row to
  // ~90px, just the Panel title bar with a single data row requiring the
  // panel's own internal scrollbar to see a second one. minimumHeight is
  // dockview's own native per-panel constraint (Constraints in
  // gridview/gridviewPanel.d.ts, accepted directly by api.addPanel — see
  // AddPanelOptions & Partial<Constraints> in dockview/options.d.ts), so
  // the sash itself simply stops/clamps at the floor rather than fighting
  // a separate CSS min-height against dockview's own layout math.
  // Calculated, not guessed, from DataTable.tsx's own Tailwind classes:
  // each row (header or data) is `px-2 py-2` (16px padding) on `text-sm`
  // (20px line-height) = 36px/row, exact. Non-table chrome stacked above
  // that inside this same row: dockview's own tab strip
  // (--dv-tabs-and-actions-container-height: 34px, styles/index.css,
  // exact) + Panel.tsx's `.panel-head` title/subtitle bar (~57px,
  // estimated from its own py-2.5 padding + two wrapped text lines, not
  // measured live) + `.panel-body`'s `py-3` padding (24px, exact) = ~115px
  // fixed overhead. 115 + 36 * 4 (header + 3 full data rows) = 259,
  // rounded to 260 — enough that the row can never be dragged below
  // showing the table header plus a genuine 3 data rows, which is what
  // was actually missing (today's 230 default lands closer to header +
  // 2 rows once this same chrome is accounted for, which is why the bug
  // was reachable at all). This does mean the resting default nudges up
  // slightly from 230 to 260 on any layout that was sitting below the new
  // floor — the tradeoff for a floor that's actually enough to read a
  // second/third position without scrolling.
  // Owner override 2026-09-11 (vertical-space reallocation pass): the
  // owner reversed the 2026-09-10 "sacrifice positions/orders for a
  // bigger chart" trade — that 230 default sat BELOW the 260
  // minimumHeight floor computed above (dockview clamps it up to the
  // floor on layout, so 230 never actually rendered), and the floor
  // itself only fit header+3 rows with no room for the 20px bottom
  // padding added in an earlier fix that same day. Raised to 340 — floor
  // math (115px fixed chrome + 36px/row) puts that at header + 5 full
  // rows + the 20px padding with room to spare, so the panel starts
  // comfortably past "3+ rows" rather than exactly at it. This is a
  // STARTING size only (see minimumHeight/dockview sash comments below);
  // the operator can still drag it to any size, which persists as
  // before.
  api.addPanel({
    id: "positions",
    component: "positions",
    title: "Positions",
    position: { referencePanel: "chart", direction: "below" },
    initialHeight: BOTTOMS_FLOOR_PX,
    minimumHeight: BOTTOMS_FLOOR_PX,
    minimumWidth: 80,
  });
  api.addPanel({ id: "candidates", component: "candidates", title: "Candidates", position: { referencePanel: "positions", direction: "within" }, inactive: true });
  // The owner's own proposal, approved 2026-09-18. It goes in THIS group
  // — the one that already carries Candidates — because that is where he
  // asked for it, beside the panels he studies a name with rather than
  // beside the blotters. Inactive by default: it is a tab he switches to,
  // and it is already populated by the time he does (see WhyPane).
  api.addPanel({ id: "why", component: "why", title: "Why", position: { referencePanel: "candidates", direction: "within" }, inactive: true });
  api.addPanel({ id: "orders", component: "orders", title: "Orders", position: { referencePanel: "positions", direction: "right" }, minimumHeight: BOTTOMS_FLOOR_PX, minimumWidth: 80 });
  api.addPanel({ id: "trades", component: "trades", title: "Trades", position: { referencePanel: "orders", direction: "within" }, inactive: true });

  api.getPanel("positions")?.api.setActive();
}

// Owner bug report, 2026-09-11 ("dragging the chart bigger just stops
// working"): the workspace box below used to have a DEFINITE, fixed
// total height (the calc() in WORKSPACE_DEFAULT_HEIGHT). Dockview-react
// fills whatever box it's given — it watches its own container with an
// internal ResizeObserver and re-lays-out to fill it (dockview-core's
// `disableAutoResizing` option, default off); it has no mechanism to grow
// that container itself, only to redistribute the space already inside
// it. So once the Positions/Orders row hit its minimumHeight floor (see
// buildDefaultLayout above), the chart/positions sash had nowhere left to
// take space FROM — the drag kept firing mouse events but dockview
// silently had nothing to give, so it looked like the drag "stopped
// working" past that point.
//
// First fix attempt (didn't work, root cause found 2026-09-11 round 3):
// watched dockview's sash for `.dv-minimum` and grew the box while that
// class was present. Traced live against dockview-core's actual
// `updateSashEnablement()` (splitview implementation): for a two-view
// vertical split, the class applied when the LOWER view (Positions/
// Orders) is pinned at its own minimumSize and the upper view (chart)
// still has room is `.dv-maximum`, not `.dv-minimum` — `.dv-minimum` is
// the class for the opposite edge (chart pinned at ITS floor). The
// listener was watching a class that is never set in this drag
// direction, so `atFolor` was permanently false and growth never fired.
// That's a real dockview-core behavior (verified by reading
// `updateSashEnablement` in dockview-core's built output, not assumed),
// but relying on it also means silently re-deriving dockview's internal
// multi-view collapse/expand algorithm for every layout shape a user can
// rearrange into — fragile even with the correct class name.
//
// Fix (this pass): drop class-watching entirely. Track the mouse
// directly during a sash drag and compute the needed growth purely from
// cursor position — see the mousedown/mousemove handlers below. This
// sidesteps dockview's internal clamp state altogether: growth is a pure
// function of "how far past the natural (ungrown) floor-line has the
// mouse gone," independent of whatever class dockview-core happens to
// apply this version or that layout shape.
//
// The box rests at exactly WORKSPACE_DEFAULT_HEIGHT. Positions/Orders'
// own minimumHeight (260, set in buildDefaultLayout) still does the
// actual floor-holding inside dockview; this code only ever ADDS height
// on top of the resting default, never removes any, so it cannot itself
// crush anything below that floor. The visible effect: past the old
// ceiling, the box keeps growing 1:1 with the drag instead of clamping,
// which pushes the footer down and requires page scroll — the explicit
// trade the owner asked for.
//
// Round-4 fix (owner bug report, 2026-09-11, "the direct sash stopped
// responding"): the trigger line below used to be computed as
// wrapper-bottom minus a single hard-coded 260px constant (one
// Positions/Orders row's floor). That only holds for the DEFAULT
// side-by-side layout — the moment Positions/Orders end up STACKED (two
// floors, ~520px reserved, not 260px), the line sat ~260px below the
// sash's real, already-maxed position, so a drag starting right at the
// visible sash needed ~260px of dead motion before growth ever kicked
// in — reading exactly as "the sash doesn't respond," while a drag that
// wandered far enough down (past Positions, into/under Orders) crossed
// the erroneous line anyway and did grow the chart. Fixed in the
// mousedown handler below by reading the sash's own live position
// instead of assuming a fixed reserved-floor constant.
const WORKSPACE_DEFAULT_HEIGHT = "max(760px, calc(100vh - var(--chrome-h) + 32px))";

/** The slice of a dockview group this file actually uses. dockview-react
 * exports the concrete class, but only these members are needed and
 * typing them here keeps the resize bookkeeping below readable. */
type DockviewGroupLike = {
  activePanel?: { id: string } | null;
  element?: HTMLElement;
  api: { height: number; setSize: (size: { height?: number; width?: number }) => void };
};

/** True content height of a fit pane: its children plus its own padding.
 *
 * NOT `pane.offsetHeight` — the pane is `min-h-full`, so it stretches to
 * whatever height the row already has and measuring it can only ever say
 * "this fits", never "this row is now far bigger than it needs to be".
 * Measured consequence of getting this wrong: switching to a tab with a
 * long table grew the row to 6000px and switching away never gave it
 * back. */
function fitContentHeight(pane: HTMLElement): number {
  const style = window.getComputedStyle(pane);
  let total = parseFloat(style.paddingTop || "0") + parseFloat(style.paddingBottom || "0");
  for (const child of Array.from(pane.children)) {
    if (!(child instanceof HTMLElement)) continue;
    const childStyle = window.getComputedStyle(child);
    total += child.offsetHeight + parseFloat(childStyle.marginTop || "0") + parseFloat(childStyle.marginBottom || "0");
  }
  return Math.ceil(total);
}

/** See LATE_PANELS. A no-op on a freshly built default layout, where
 * buildDefaultLayout has already placed every panel. */
function addLatePanels(api: DockviewApi) {
  for (const late of LATE_PANELS) {
    if (api.getPanel(late.id)) continue;
    const reference = api.getPanel(late.beside) ?? api.getPanel("chart");
    if (!reference) continue;
    api.addPanel({
      id: late.id,
      component: late.id,
      title: late.title,
      position: { referencePanel: reference.id, direction: "within" },
      inactive: true,
    });
  }
}

export function DesktopCockpitWorkspace() {
  const apiRef = useRef<DockviewApi | null>(null);
  const wrapperRef = useRef<HTMLDivElement | null>(null);
  // Extra pixels grown on top of WORKSPACE_DEFAULT_HEIGHT when the bottoms
  // row is already on its floor and the operator keeps dragging the sash
  // down. Persisted: without this, reload restored dockview proportions
  // into the default-height box and the split snapped back.
  const [growth, setGrowth] = useState(() => readPersistedGrowth());
  const growthRef = useRef(growth);
  // Pixels added on top of `growth` purely so content-sized panels (see
  // FIT_PANELS) are not clipped. Deliberately NOT persisted: it is
  // re-derived from whatever the panels currently hold, so it follows the
  // data rather than a stale saved number.
  const autoFitRef = useRef(readPersistedFit());
  // True for the duration of a sash/edge drag. Auto-fit must not run
  // while the operator is dragging: mid-gesture the box is briefly
  // resized before the rows are, and a fit panel clipped for that one
  // frame would be "corrected" by adding height that the finished
  // gesture never asked for (measured: dragging the chart back up left
  // 250px of page height behind). It runs once on release instead.
  const draggingRef = useRef(false);
  // reconcileFit is defined below the drag handlers that need to call it
  // on release; a ref keeps that one-directional without reordering the
  // whole component.
  const reconcileFitRef = useRef<(() => void) | null>(null);
  /** Set below once the row helpers exist; the drag handlers are defined
   * first and need the floor of the row a given group sits in. */
  const rowFloorRef = useRef<(groupEl: HTMLElement | undefined) => number>(() => 0);
  /** Content floor each row had at the last reconcile, so auto-fit can
   * tell "the content shrank" from "the operator made this row bigger". */
  const lastFloorRef = useRef(new Map<number, number>());
  const applyHeight = useCallback(() => {
    const el = wrapperRef.current;
    if (!el) return;
    const extra = Math.round(growthRef.current + autoFitRef.current);
    el.style.height = extra > 0 ? `calc(${WORKSPACE_DEFAULT_HEIGHT} + ${extra}px)` : "";
  }, []);
  const applyGrowth = useCallback((px: number) => {
    growthRef.current = px;
    applyHeight();
  }, [applyHeight]);
  /* Push the new box height into dockview SYNCHRONOUSLY.
   *
   * Measured 2026-09-17: dockview watches its container with a
   * ResizeObserver, which fires on a later frame. Growing the box and
   * then immediately calling setSize on a group therefore raced — the
   * observer arrived afterwards and re-spread the new height across every
   * row in proportion, so a 250px drag on the chart's bottom edge grew
   * the chart by 99px and quietly inflated rows nobody was dragging.
   * `api.layout(w, h)` is dockview's own entry point for "the container
   * is now this size"; calling it first means the subsequent setSize
   * calls are the last word. */
  const relayout = useCallback(() => {
    const el = wrapperRef.current;
    const api = apiRef.current;
    if (!el || !api) return;
    api.layout(el.clientWidth, el.clientHeight);
  }, []);
  useEffect(() => {
    applyGrowth(growth);
  }, [applyGrowth, growth]);
  const dragRef = useRef<{
    startY: number;
    startGrowth: number;
    startChartH: number;
    startBottomsH: number;
    bottomsMin: number;
    others: { group: DockviewGroupLike; height: number }[];
  } | null>(null);
  const edgeDragRef = useRef<{
    startY: number;
    startGrowth: number;
    bottom: DockviewGroupLike[];
    bottomHeight: number;
    others: { group: DockviewGroupLike; height: number }[];
  } | null>(null);

  /** Every group except the ones a drag is explicitly resizing, with the
   * height it had when the drag started.
   *
   * Why this exists (measured 2026-09-17, before/after heights logged from
   * a scripted drag): the chart-vs-bottoms handler below sets the chart
   * and bottoms heights directly, and dockview's splitview balances the
   * books by taking the difference out of whatever OTHER rows exist. On
   * the default layout, dragging the chart's bottom edge down 250px
   * collapsed the Holdings/Account/Sessions row from 201px to 108px and
   * then to its 88px floor — and dragging back up did not give it back.
   * So "make the chart bigger downward" silently ate the top row instead
   * of making the page taller, which is a large part of why the owner
   * reports that downward resizing does not work. Re-asserting these
   * snapshots after each setSize pins the uninvolved rows. */
  const snapshotOthers = (api: DockviewApi | null, involved: DockviewGroupLike[]) => {
    // Groups that sit SIDE BY SIDE share one row height — Positions and
    // Orders are the obvious pair. Pinning one of them while resizing the
    // other is a contradiction the splitview resolves by dumping the
    // difference on some unrelated row (measured: it inflated the chart
    // without limit until the workspace was thousands of pixels tall). So
    // a row is excluded as soon as ANY group in it is being resized.
    const rowOf = (group: DockviewGroupLike) => {
      const box = group.element?.getBoundingClientRect();
      return box ? `${Math.round(box.top)}:${Math.round(box.bottom)}` : "";
    };
    const involvedRows = new Set(involved.map(rowOf));
    return ((api?.groups ?? []) as unknown as DockviewGroupLike[])
      .filter((group) => !involved.includes(group) && !involvedRows.has(rowOf(group)))
      .map((group) => ({ group, height: group.api.height }));
  };

  const restoreOthers = (others: { group: DockviewGroupLike; height: number }[]) => {
    for (const entry of others) {
      if (Math.abs(entry.group.api.height - entry.height) > 1) entry.group.api.setSize({ height: entry.height });
    }
  };

  const reset = useCallback(() => {
    localStorage.removeItem(STORAGE_KEY);
    clearPersistedGrowth();
    clearPersistedFit();
    autoFitRef.current = 0;
    setGrowth(0);
    applyGrowth(0);
    const api = apiRef.current;
    if (!api) return;
    [...api.panels].forEach((panel) => api.removePanel(panel));
    buildDefaultLayout(api);
  }, [applyGrowth]);
  const onReady = useCallback((event: DockviewReadyEvent) => {
    apiRef.current = event.api;
    try {
      const saved = localStorage.getItem(STORAGE_KEY);
      if (saved) event.api.fromJSON(JSON.parse(saved));
    } catch { localStorage.removeItem(STORAGE_KEY); }
    if (!event.api.panels.length) buildDefaultLayout(event.api);
    else addLatePanels(event.api);
    event.api.onDidLayoutChange(() => {
      try { localStorage.setItem(STORAGE_KEY, JSON.stringify(event.api.toJSON())); } catch { /* UI-only best effort */ }
    });
  }, []);

  // Capture-phase pointerdown on the chart-vs-Positions sash only. Native
  // dockview keeps the top-row-vs-chart sash (and any other vertical
  // split). This handler is two-way: up borrows from the chart to grow
  // bottoms, down shrinks bottoms to the floor then grows the page. Extra
  // page height is written on the wrapper DOM during the gesture so
  // mouse-up cannot snap the sash back.
  useEffect(() => {
    const wrapper = wrapperRef.current;
    if (!wrapper) return;

    const onPointerMove = (e: PointerEvent) => {
      const drag = dragRef.current;
      if (!drag) return;
      const chartGroup = apiRef.current?.getPanel("chart")?.group;
      const bottomsGroup = apiRef.current?.getPanel("positions")?.group;
      if (!chartGroup || !bottomsGroup) return;
      const next = nextBottomsSashState(
        { growth: drag.startGrowth, chartH: drag.startChartH, bottomsH: drag.startBottomsH },
        e.clientY - drag.startY,
        CHART_FLOOR_PX,
        drag.bottomsMin,
      );
      // Height goes on the DOM node immediately. Pushing it through React
      // state mid-drag re-renders the box at the *previous* growth and
      // dockview's ResizeObserver then snaps the sash back on mouse-up.
      applyGrowth(next.growth);
      relayout();
      chartGroup.api.setSize({ height: Math.max(CHART_FLOOR_PX, next.chartH) });
      bottomsGroup.api.setSize({ height: Math.max(drag.bottomsMin, next.bottomsH) });
      // Pin every other row at the height it had when the drag started —
      // see snapshotOthers. Without this the drag quietly steals from the
      // top row instead of growing the page.
      restoreOthers(drag.others);
    };
    const onPointerUp = () => {
      dragRef.current = null;
      draggingRef.current = false;
      document.removeEventListener("pointermove", onPointerMove);
      setGrowth(growthRef.current);
      writePersistedGrowth(growthRef.current);
      reconcileFitRef.current?.();
    };
    const onPointerDown = (e: PointerEvent) => {
      if (e.button !== 0) return;
      const target = e.target as HTMLElement | null;
      const sash = target?.closest<HTMLElement>(
        ".dv-split-view-container.dv-vertical > .dv-sash-container > .dv-sash"
      );
      if (!sash) return;
      const chartGroup = apiRef.current?.getPanel("chart")?.group as { element?: HTMLElement; api: { height?: number } } | undefined;
      const bottomsGroup = apiRef.current?.getPanel("positions")?.group as { element?: HTMLElement; api: { height?: number } } | undefined;
      const chartEl = chartGroup?.element;
      const bottomsEl = bottomsGroup?.element;
      if (!chartEl || !bottomsEl) return;
      const sashBox = sash.getBoundingClientRect();
      const chartBox = chartEl.getBoundingClientRect();
      const bottomsBox = bottomsEl.getBoundingClientRect();
      // Only the chart-vs-Positions sash. The top-row-vs-chart sash
      // (Holdings/Account/Sessions above) stays native dockview so it
      // can borrow from the row above.
      if (!sashSitsBetween((sashBox.top + sashBox.bottom) / 2, chartBox.bottom, bottomsBox.top)) return;
      e.stopPropagation();
      e.preventDefault();
      draggingRef.current = true;
      dragRef.current = {
        startY: e.clientY,
        startGrowth: growthRef.current,
        startChartH: chartGroup?.api.height ?? CHART_FLOOR_PX,
        startBottomsH: bottomsGroup?.api.height ?? BOTTOMS_FLOOR_PX,
        // A content-sized panel's floor is its content, not the generic
        // table floor: without this the row visibly collapses under the
        // drag and is then snapped back by auto-fit on release.
        bottomsMin: Math.max(BOTTOMS_FLOOR_PX, rowFloorRef.current(bottomsEl)),
        others: snapshotOthers(apiRef.current, [
          chartGroup as unknown as DockviewGroupLike,
          bottomsGroup as unknown as DockviewGroupLike,
        ]),
      };
      document.addEventListener("pointermove", onPointerMove);
      document.addEventListener("pointerup", onPointerUp, { once: true });
    };

    wrapper.addEventListener("pointerdown", onPointerDown, true);
    return () => {
      wrapper.removeEventListener("pointerdown", onPointerDown, true);
      document.removeEventListener("pointermove", onPointerMove);
      document.removeEventListener("pointerup", onPointerUp);
    };
  }, [applyGrowth, relayout]);

  /** The workspace's rows, top to bottom. Two groups sitting side by
   * side (Positions and Orders, say) are one row and share one height. */
  const groupRows = useCallback((api: DockviewApi) => {
    const byRow = new Map<string, { top: number; groups: DockviewGroupLike[] }>();
    for (const group of (api.groups ?? []) as unknown as DockviewGroupLike[]) {
      const box = group.element?.getBoundingClientRect();
      if (!box) continue;
      const key = `${Math.round(box.top)}:${Math.round(box.bottom)}`;
      const row = byRow.get(key);
      if (row) row.groups.push(group);
      else byRow.set(key, { top: box.top, groups: [group] });
    }
    return [...byRow.values()].sort((a, b) => a.top - b.top);
  }, []);

  /** Height a row must keep so that every content-sized panel currently
   * visible in it is shown in full, or 0 when the row holds none. */
  const rowContentFloor = useCallback((row: { groups: DockviewGroupLike[] }) => {
    let floor = 0;
    for (const group of row.groups) {
      const activeId = group.activePanel?.id;
      if (!activeId || !FIT_PANELS.has(activeId)) continue;
      const pane = [...(group.element?.querySelectorAll<HTMLElement>("[data-fit-pane='true']") ?? [])]
        .find((candidate) => candidate.offsetParent !== null && candidate.clientHeight > 0);
      if (!pane) continue;
      const box = pane.closest<HTMLElement>(".dv-content-container");
      const groupHeight = group.element?.getBoundingClientRect().height ?? 0;
      const chrome = box ? groupHeight - box.clientHeight : 0;
      floor = Math.max(floor, fitContentHeight(pane) + chrome);
    }
    return floor;
  }, []);

  /* Grow (or give back) exactly the height the content-sized panels need.
   *
   * This is the half of the owner's "make the page bigger and get rid of
   * the scroll bars" request that the panel library genuinely cannot do
   * for us: dockview lays its groups out inside a box of definite height
   * and only ever redistributes the space already in that box — it has no
   * notion of a panel sized by its own content, and its
   * `.dv-content-container` clips anything taller. So a no-internal-
   * scrollbar panel means measuring the content ourselves and making both
   * the row and the workspace box that tall.
   *
   * Every row not being grown is snapshotted and restored (see
   * snapshotOthers), so fitting Orders never silently shrinks the chart
   * or Holdings. Shrinking back is the same step with the sign flipped,
   * floored at the row's own minimum, so closing out orders returns the
   * page height instead of leaving a permanent gap. */
  const reconcileFit = useCallback(() => {
    if (draggingRef.current) return;
    // Before dockview has built its groups there is nothing to measure,
    // and treating that as "no fit panels" wiped the saved auto-fit
    // height on every reload (measured: the restored layout came back
    // ~270px short and the rows collapsed toward their floors).
    const api = apiRef.current;
    if (!api?.groups.length) return;

    // Rows, top to bottom. Groups side by side share one height, so the
    // unit of resizing here is the ROW, never the individual group.
    const rows = groupRows(api);
    if (!rows.length) return;

    // Auto-fit is DIFFERENTIAL: it reacts to the content changing size,
    // never to a row simply looking roomy.
    //
    // This is what lets "sized by its content" and "sized by hand" live on
    // the same panel, which they otherwise cannot (one wants the height
    // the data implies, the other wants the height the operator chose).
    // Content sets a FLOOR — drag a content-sized panel shorter than its
    // rows and it springs back, because the alternative is the internal
    // scrollbar the owner asked us to remove. Above that floor the
    // operator's own height is his, and height only comes back when the
    // content itself shrinks (an order closes, he switches to a shorter
    // tab). Measured before this rule existed: dragging the bottom edge
    // down was undone on release, because the reconciler treated the room
    // the operator had just asked for as slack to reclaim.
    // Rows are remembered by their position from the top, not by which
    // panels they hold: switching a tab is precisely the case where the
    // floor changes and the row must give height back, so a key that
    // changes with the tab would forget exactly when it matters.
    const current = rows.map((row) => row.groups[0].api.height);
    const floors = rows.map((row) => rowContentFloor(row));
    const steps = rows.map((_row, index) => {
      const floor = floors[index];
      if (floor <= 0) return 0;
      if (floor - current[index] > 2) return floor - current[index];
      const previous = lastFloorRef.current.get(index);
      if (previous === undefined || previous - floor <= 2) return 0;
      // Give back only what the content released, and never go below it.
      return -Math.min(previous - floor, current[index] - floor);
    });
    rows.forEach((_, index) => {
      if (floors[index] > 0) lastFloorRef.current.set(index, floors[index]);
    });
    const grow = Math.max(...steps, 0);
    const shrink = Math.min(...steps, 0);
    const step = grow > 2 ? grow : shrink < -2 ? Math.max(shrink, -autoFitRef.current) : 0;
    if (!step) return;

    // Every row gets an explicit height, and they sum to exactly the new
    // box height, so dockview has nothing left to redistribute on its own
    // — which is what stopped the earlier version from settling (a shrink
    // would be spread proportionally over rows nobody had touched, and
    // the restored layout drifted a little further on every reload).
    const desired = rows.map((_, index) =>
      steps[index] !== 0 ? Math.max(BOTTOMS_FLOOR_PX, current[index] + step) : current[index],
    );
    autoFitRef.current = Math.max(0, autoFitRef.current + step);
    applyHeight();
    relayout();
    rows.forEach((row, index) => {
      if (Math.abs(row.groups[0].api.height - desired[index]) > 1) row.groups[0].api.setSize({ height: desired[index] });
    });
    writePersistedFit(autoFitRef.current);
  }, [applyHeight, relayout]);
  reconcileFitRef.current = reconcileFit;
  rowFloorRef.current = (groupEl) => {
    const api = apiRef.current;
    if (!api || !groupEl) return 0;
    const box = groupEl.getBoundingClientRect();
    const key = `${Math.round(box.top)}:${Math.round(box.bottom)}`;
    const row = groupRows(api).find((candidate) => {
      const first = candidate.groups[0].element?.getBoundingClientRect();
      return first ? `${Math.round(first.top)}:${Math.round(first.bottom)}` === key : false;
    });
    return row ? rowContentFloor(row) : 0;
  };

  // Re-measure whenever a fit panel's content changes size (a fill lands,
  // an order closes, the operator switches tab or drags a panel somewhere
  // else), and once after any dockview layout change.
  useEffect(() => {
    const wrapper = wrapperRef.current;
    if (!wrapper) return;
    let frame = 0;
    const schedule = () => {
      if (frame) return;
      frame = window.requestAnimationFrame(() => {
        frame = 0;
        reconcileFit();
      });
    };
    const observer = new ResizeObserver(schedule);
    const observed = new Set<HTMLElement>();
    const attach = () => {
      for (const pane of wrapper.querySelectorAll<HTMLElement>("[data-fit-pane='true']")) {
        const content = pane.firstElementChild;
        if (content instanceof HTMLElement && !observed.has(content)) {
          observed.add(content);
          observer.observe(content);
        }
      }
      schedule();
    };
    attach();
    const mutations = new MutationObserver(attach);
    mutations.observe(wrapper, { childList: true, subtree: true });
    const onResize = () => schedule();
    window.addEventListener("resize", onResize);
    return () => {
      if (frame) window.cancelAnimationFrame(frame);
      observer.disconnect();
      mutations.disconnect();
      window.removeEventListener("resize", onResize);
    };
  }, [reconcileFit]);

  /* Bottom-edge resize: the missing "expand downward".
   *
   * A dockview sash only exists BETWEEN two groups, so the bottom-most
   * row has no handle under it and nothing to drag — to make it taller
   * you had to grab the sash above it and pull up. That is the whole of
   * "I can only expand upward", and it is the container's doing, not a
   * missing library feature: the workspace box is pinned to the viewport
   * height and dockview cannot grow its own container. This grip sits on
   * that bottom edge and grows the box itself, handing the extra height
   * to the bottom row and letting the page scroll. */
  const onEdgePointerDown = useCallback((event: React.PointerEvent<HTMLDivElement>) => {
    if (event.button !== 0) return;
    const api = apiRef.current;
    const wrapper = wrapperRef.current;
    if (!api || !wrapper) return;
    const wrapperBottom = wrapper.getBoundingClientRect().bottom;
    const groups = (api.groups ?? []) as unknown as DockviewGroupLike[];
    const bottom = groups.filter((group) => {
      const box = group.element?.getBoundingClientRect();
      return box ? Math.abs(box.bottom - wrapperBottom) <= 12 : false;
    });
    if (!bottom.length) return;
    event.preventDefault();
    draggingRef.current = true;
    edgeDragRef.current = {
      startY: event.clientY,
      startGrowth: growthRef.current,
      bottom,
      bottomHeight: bottom[0].api.height,
      others: snapshotOthers(api, bottom),
    };

    const onMove = (moveEvent: PointerEvent) => {
      const drag = edgeDragRef.current;
      if (!drag) return;
      // Down grows the page; up gives it back, but never past the
      // viewport-fitting default — below that the internal sashes are
      // the right tool, and a page shorter than the window is not a
      // thing the owner asked for.
      const delta = Math.max(moveEvent.clientY - drag.startY, -drag.startGrowth);
      applyGrowth(drag.startGrowth + delta);
      relayout();
      for (const group of drag.bottom) {
        group.api.setSize({ height: Math.max(BOTTOMS_FLOOR_PX, drag.bottomHeight + delta) });
      }
      restoreOthers(drag.others);
    };
    const onUp = () => {
      edgeDragRef.current = null;
      draggingRef.current = false;
      window.removeEventListener("pointermove", onMove);
      window.removeEventListener("pointerup", onUp);
      window.removeEventListener("pointercancel", onUp);
      setGrowth(growthRef.current);
      writePersistedGrowth(growthRef.current);
      reconcileFitRef.current?.();
    };
    window.addEventListener("pointermove", onMove);
    window.addEventListener("pointerup", onUp);
    window.addEventListener("pointercancel", onUp);
  }, [applyGrowth, relayout]);

  return (
    // (pb-6 was the original fix for the row butting against the footer
    // with no breathing room; superseded below.)
    // Vertical-space reallocation pass (owner-authorized overshoot,
    // 2026-09-11): pb-6 (24px, "double the pb-3 convention" per the
    // original comment below) was dead space between this box and the
    // footer, with nothing rendered in it. Dropped to pb-2 (8px, real
    // breathing room but not a wasted 24px) — the 16px reclaimed here,
    // plus 16px reclaimed from the footer's own py-4 -> py-2 in App.tsx,
    // is added straight into the box's own height formula below instead
    // of staying an unaccounted-for page margin.
    <div className="px-3 pb-1">
      <div className="mb-0.5 flex items-center justify-end">
        <button type="button" onClick={reset} className="text-xs text-accent underline">Reset layout</button>
      </div>
      {/* Item 2 of cockpit pass 3 (SUPERSEDED AGAIN, owner override
          2026-09-10, round 2): the resize-y box above (a manual CSS
          `resize: vertical` corner grip) was itself the regression this
          replaces — confirmed live: "difficult to find, tiny corner grip,
          not a proper draggable divider," and its height formula
          (max(560px, 100vh-chrome) + a flat 480px for Positions/Orders)
          ALWAYS exceeded the viewport by construction, forcing the whole
          page to scroll just to reach the workspace, let alone resize it.
          Two changes, together:
          1. The resize-y/overflow-auto/max-h corner-grip hack is gone
             entirely. The chart-vs-Positions/Orders split is now handled
             by dockview's OWN native sibling-panel resize sash (see
             buildDefaultLayout's "positions" panel above,
             direction: "below") — a proper draggable divider with a
             resize cursor and an accent highlight on hover
             (--dv-sash-color/--dv-active-sash-color, styles/index.css),
             not a hand-rolled mechanism. This is strictly more robust
             than continuing to patch the manual approach, and it's what
             the library already does for free between real sibling rows.
          2. The box's total height is viewport-bounded
             (100vh - var(--chrome-h), the same --chrome-h budget
             TopStrip..DecisionStateBanner above already measures live —
             see App.tsx's chromeRef) so the box's own bottom edge lines
             up with the browser window's bottom edge instead of
             guaranteed-overflowing it, with a min-h floor (640px) so a
             day with an unusually tall chrome stack still leaves enough
             room for both a genuinely readable chart and a usable
             Positions/Orders row rather than crushing both to nothing —
             on a short viewport this floor can still require some
             scroll, which is the honest tradeoff for never rendering
             either panel unusably small.
          This still MUST be a definite `height` (not `min-height`) for
          the same reason as before: dockview-react's root renders
          `height: 100%` down its own wrapper chain, which only resolves
          against an ancestor with a definite (not min-) height. The
          `max(...)` CSS function (not Tailwind's arbitrary-value min-h
          alongside a separate h-, which cannot express "whichever of
          these two is bigger" in one definite value) is what makes both
          the floor and the viewport-bound behave as ONE definite
          height.

          Vertical-space reallocation pass (owner-authorized overshoot,
          2026-09-11): at a typical ~1000-1100px viewport the 640px floor
          was the effective governor (100vh - chrome-h regularly landed
          below it), and Positions/Orders needed internal scrolling to
          show 2 rows while real whitespace sat unused below this box and
          above the footer. Two changes together, matching the footer/
          wrapper-padding reclaim above:
          1. Floor raised 640 -> 760 (+120px) — deliberately well past
             "just enough," per the owner's explicit go-further-then-trim
             instruction, rather than another few-pixel nudge.
          2. +32px added to the viewport-bound term — the exact pb-6->
             pb-2 (16px, this file) and footer py-4->py-2 (16px, App.tsx)
             reclaim, folded in here so it isn't left an unaccounted-for
             page margin.
          Both numbers are deliberately generous; this was NOT re-measured
          against a live-rendered page (no browser access from this
          pass) — see the worked pixel math in the accompanying report,
          and dial back the floor first if it overshoots in practice. The
          640px floor's own honest scroll tradeoff (comment above) still
          applies at the new 760px value on short viewports.

          Sash-past-the-floor pass (owner bug report, 2026-09-11, round 2):
          this height was still a hard ceiling — see the
          WORKSPACE_DEFAULT_HEIGHT block comment above for the mechanism
          and the fix. `h-[...]` below keeps providing the resting default
          (still a definite height, still viewport-bound, unchanged from
          the pass above) via Tailwind/CSS as before; the inline `style`
          only ever ADDS to it once `growth` is nonzero, and inline
          `style` always wins the cascade over the class regardless of
          specificity, so there's no fight between the two. */}
      <div
        ref={wrapperRef}
        className="h-[max(760px,calc(100vh-var(--chrome-h)+32px))] rounded-lg border border-border overflow-hidden"
      >
        <DockviewReact
          className="dockview-theme-qamc"
          onReady={onReady}
          components={COMPONENTS}
          disableDnd={false}
          disableFloatingGroups={false}
          dndEdges={{
            size: { value: 100, type: "pixels" },
            activationSize: { value: 20, type: "pixels" },
          }}
        />
      </div>
      {/* The bottom edge, made draggable — see onEdgePointerDown. Styled
          to match dockview's own sashes (--dv-sash-color, accent on
          hover/drag) so it reads as the same kind of handle as the ones
          between rows, with a short centre bar as the visible grip. */}
      <div
        role="separator"
        aria-orientation="horizontal"
        aria-label="Drag to make the workspace taller"
        title="Drag down to make the workspace taller — the page scrolls"
        onPointerDown={onEdgePointerDown}
        className="group flex h-3 cursor-ns-resize touch-none select-none items-center justify-center"
      >
        <div className="h-1 w-16 rounded-full bg-border group-hover:bg-accent" />
      </div>
    </div>
  );
}
