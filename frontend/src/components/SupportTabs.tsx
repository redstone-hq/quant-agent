import { useState } from "react";
import { HealthResponse, OrderItem, RunSummary, TradeItem } from "../api/client";
import { OrdersPanel } from "./OrdersPanel";
import { TradesPanel } from "./TradesPanel";
import { RunsPanel } from "./RunsPanel";
import { DirectionalBiasPanel } from "./DirectionalBiasPanel";
import { MissedOpportunitiesPanel } from "./MissedOpportunitiesPanel";
import { SearchPanel } from "./SearchPanel";
import { HealthPanel } from "./HealthPanel";

/* The cockpit's lower support area — the iPad/phone fallback for
 * everything that isn't Positions/Chart/Decision (those three now have
 * their own primary panes; see App.tsx's PaneNav). Positions and Liquidity
 * used to live here too, as two of what the owner counted as eight tabs;
 * both moved out (Positions is now a primary landing pane — item 1 of the
 * cockpit trader rework; Liquidity is now a single compact row in the
 * header — item 9). What's left is genuinely secondary, and System/Search
 * — the two tabs the owner named as "not trading" — are now folded into
 * one Diagnostics tab instead of standing on their own (item 13). */

type TabId = "orders" | "runs" | "bias" | "missed" | "diagnostics";

export function SupportTabs({
  orders,
  ordersError,
  ordersLoading,
  orderStatus,
  onOrderStatusChange,
  trades,
  tradesError,
  tradesLoading,
  runs,
  runsError,
  runsLoading,
  health,
  healthError,
  onSelectPositionSymbol,
  onInspectOrder,
  onInspectTrade,
}: {
  orders: OrderItem[];
  ordersError: string | null;
  ordersLoading: boolean;
  orderStatus: "open" | "closed" | "all";
  onOrderStatusChange: (status: "open" | "closed" | "all") => void;
  trades: TradeItem[];
  tradesError: string | null;
  tradesLoading: boolean;
  runs: RunSummary[];
  runsError: string | null;
  runsLoading: boolean;
  health: HealthResponse | null;
  healthError: string | null;
  /** Modal-free symbol click, threaded to every symbol-click affordance
   * in this fallback layout — Orders/Trades symbol cell, Missed
   * Opportunities, Search's symbol column — same callback and same rule
   * as SupportWorkspaceContext's onSelectPositionSymbol (see there for
   * the full rationale): chart the symbol, open nothing. */
  onSelectPositionSymbol?: (symbol: string) => void;
  onInspectOrder?: (order: OrderItem) => void;
  onInspectTrade?: (trade: TradeItem) => void;
}) {
  const [tab, setTab] = useState<TabId>("orders");

  const tabs: { id: TabId; label: string; badge?: number }[] = [
    { id: "orders", label: "Orders & Trades", badge: orders.length || undefined },
    { id: "runs", label: "Runs", badge: runs.length || undefined },
    { id: "bias", label: "Directional Bias" },
    { id: "missed", label: "Missed Opportunities" },
    { id: "diagnostics", label: "Diagnostics" },
  ];

  return (
    <div>
      <div className="flex w-full max-w-full items-center gap-1 overflow-x-auto mb-3 border-b border-border">
        {tabs.map((t) => (
          <button
            key={t.id}
            type="button"
            onClick={() => setTab(t.id)}
            className={`flex-shrink-0 px-3 py-2 text-[0.72rem] font-semibold uppercase tracking-wide border-b-2 -mb-px whitespace-nowrap ${
              tab === t.id ? "border-accent text-accent" : "border-transparent text-dim hover:text-ink"
            }`}
          >
            {t.label}
            {t.badge ? <span className="ml-1.5 text-dim font-normal normal-case">({t.badge})</span> : null}
          </button>
        ))}
      </div>

      {tab === "orders" && (
        <div className="grid grid-cols-1 xl:grid-cols-2 gap-3">
          <OrdersPanel
            orders={orders}
            error={ordersError}
            loading={ordersLoading}
            status={orderStatus}
            onStatusChange={onOrderStatusChange}
            onInspect={onInspectOrder}
            onSelectSymbol={onSelectPositionSymbol}
            trades={trades}
          />
          <TradesPanel trades={trades} error={tradesError} loading={tradesLoading} onInspect={onInspectTrade} onSelectSymbol={onSelectPositionSymbol} />
        </div>
      )}

      {tab === "runs" && <RunsPanel runs={runs} error={runsError} loading={runsLoading} />}
      {tab === "bias" && <DirectionalBiasPanel />}
      {tab === "missed" && <MissedOpportunitiesPanel onSelectSymbol={onSelectPositionSymbol} />}
      {tab === "diagnostics" && (
        <div className="flex flex-col gap-3">
          <HealthPanel health={health} error={healthError} />
          <SearchPanel onSelectSymbol={onSelectPositionSymbol} />
        </div>
      )}
    </div>
  );
}
