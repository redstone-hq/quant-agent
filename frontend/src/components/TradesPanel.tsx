import { useMemo, useState } from "react";
import { Badge, TextInput } from "@tremor/react";
import { legacyCreateColumnHelper as createColumnHelper, type LegacyColumnDef } from "@tanstack/react-table/legacy";
import { TradeItem } from "../api/client";
import { fmtMoney, fmtNum, fmtTime, pnlClass } from "../lib/format";
import { displayFillPrice, displayFillQty } from "../lib/tradeFillDisplay";
import { Panel, StateMessage } from "./ui/Panel";
import { DataTable } from "./ui/DataTable";

const columnHelper = createColumnHelper<TradeItem>();

/** Reasoning strings can run long (a full LLM decision paragraph) — truncate
 * in the cell and reveal the rest on click, same disclosure idiom
 * AgentPromptViewer.tsx uses for full prompt/response text. A button (not
 * the row) owns the click so this never fights the table's own onRowClick —
 * mirrors the Symbol column's `event.stopPropagation()` above. */
const REASONING_PREVIEW_CHARS = 70;

/** requested_risk_pct/allocated_risk_pct (conviction ledger, spec §7.2) are
 * plain non-negative percentages of equity — not a gain/loss, so unlike
 * fmtPct these never take a "+" sign. Pinned at ENTRY only: null on every
 * interim/exit row and on any row written before PR #159, rendered as a
 * plain dash rather than "—%" or a fabricated 0%, same convention
 * CandidateRail.tsx uses for the sibling pm_risk_allocation_pct field. */
function RiskPctCell({ value }: { value: number | null | undefined }) {
  if (value === null || value === undefined) return <span className="text-dim">—</span>;
  return <span>{fmtNum(value)}%</span>;
}

function ReasoningCell({ text }: { text: string | null }) {
  const [open, setOpen] = useState(false);
  if (!text) return <span className="text-dim">—</span>;
  if (text.length <= REASONING_PREVIEW_CHARS) {
    return <span className="block max-w-[22rem] whitespace-normal font-sans">{text}</span>;
  }
  return (
    <button
      type="button"
      onClick={(event) => {
        event.stopPropagation();
        setOpen((v) => !v);
      }}
      aria-expanded={open}
      className="block max-w-[22rem] whitespace-normal text-left font-sans hover:text-accent"
    >
      {open ? text : `${text.slice(0, REASONING_PREVIEW_CHARS)}…`}
      <span className="ml-1 text-dim">{open ? "(show less)" : "(show more)"}</span>
    </button>
  );
}

export function TradeTable({
  trades,
  onInspect,
  onSelectSymbol,
  scrollX,
}: {
  trades: TradeItem[];
  /** Size the sixteen columns to their content and scroll sideways
   * instead of squeezing them into the panel — see DataTable's `scrollX`.
   * Off for this table's other consumers (JournalPanel,
   * CandidateDetailModal), which are not in a narrow dock panel. */
  scrollX?: boolean;
  onInspect?: (trade: TradeItem) => void;
  /** Symbol-cell-specific click: charts the symbol in place, same as
   * PositionsPanel's/OrdersPanel's onSelectSymbol. Deliberately separate
   * from onInspect above (which opens the trade's Candidate Detail modal
   * on the rest of the row) — clicking just the SYMBOL must never open a
   * modal. Optional and omitted by JournalPanel/CandidateDetailModal's
   * read-only uses of this table, where the symbol stays plain text. */
  onSelectSymbol?: (symbol: string) => void;
}) {
  const columns = useMemo(
    () => [
      columnHelper.accessor("timestamp", { header: "Time", cell: (info) => fmtTime(info.getValue()) }),
      columnHelper.accessor("symbol", {
        header: "Symbol",
        cell: (info) =>
          onSelectSymbol ? (
            <button
              type="button"
              className="font-bold text-accent hover:underline"
              onClick={(event) => {
                event.stopPropagation();
                onSelectSymbol(info.getValue());
              }}
            >
              {info.getValue()}
            </button>
          ) : (
            <span className="font-bold text-accent">{info.getValue()}</span>
          ),
      }),
      columnHelper.accessor("action", { header: "Action", cell: (info) => <Badge color={info.getValue().includes("BUY") ? "emerald" : info.getValue().includes("SELL") ? "rose" : "slate"} size="xs">{info.getValue()}</Badge> }),
      columnHelper.accessor("reasoning", { header: "Reasoning", cell: (info) => <ReasoningCell text={info.getValue()} /> }),
      columnHelper.accessor("fill_status", { header: "Fill status", cell: (info) => <Badge color={String(info.getValue()).includes("fill") ? "emerald" : String(info.getValue()).includes("reject") ? "rose" : "slate"} size="xs">{info.getValue() || "unfilled"}</Badge> }),
      columnHelper.accessor("fill_qty", { header: "Filled qty", cell: (info) => displayFillQty(info.row.original) }),
      columnHelper.accessor("fill_price", { header: "Fill price", cell: (info) => displayFillPrice(info.row.original) }),
      columnHelper.accessor("realized_pnl", { header: "Realized P&L", cell: (info) => <span className={pnlClass(info.getValue())}>{info.getValue() === null || info.getValue() === undefined ? "—" : fmtMoney(info.getValue())}</span> }),
      columnHelper.accessor("stop_loss", { header: "Recorded stop", cell: (info) => fmtMoney(info.getValue()) }),
      columnHelper.accessor("take_profit", { header: "Take profit", cell: (info) => fmtMoney(info.getValue()) }),
      columnHelper.accessor("run_id", { header: "Run", cell: (info) => info.getValue() || "—" }),
      columnHelper.accessor("decision_id", { header: "Decision", cell: (info) => info.getValue() || "—" }),
      // Conviction ledger (spec §7.2, PR #159) — recorded at entry to
      // explain why a trade was sized the way it was; previously captured
      // for internal record-keeping only and invisible anywhere a human
      // could see it. Read-only surfacing, same absence convention as the
      // columns above: a dash, never a fabricated default.
      columnHelper.accessor("conviction", { header: "Conviction", cell: (info) => info.getValue() || "—" }),
      columnHelper.accessor("requested_risk_pct", { header: "Requested risk", cell: (info) => <RiskPctCell value={info.getValue()} /> }),
      columnHelper.accessor("allocated_risk_pct", { header: "Allocated risk", cell: (info) => <RiskPctCell value={info.getValue()} /> }),
      columnHelper.accessor("decision_model", { header: "Decision model", cell: (info) => info.getValue() || "—" }),
    ] as LegacyColumnDef<TradeItem, unknown>[],
    [onSelectSymbol]
  );
  return <DataTable data={trades} columns={columns} getRowId={(trade) => String(trade.id)} initialSorting={[{ id: "timestamp", desc: true }]} onRowClick={onInspect} scrollX={scrollX} />;
}

export function TradesPanel({
  trades,
  error,
  loading,
  onInspect,
  onSelectSymbol,
  fit,
  scrollX,
}: {
  fit?: boolean;
  /** Board item 103: this blotter is the one panel the owner asked to
   * keep BOTH its own scrollbars, because grown to fit it ran to roughly
   * six thousand pixels of page. The pane around it supplies the
   * scrollbars; this flag stops the table pinning itself to the panel
   * width so the horizontal one has something to scroll. */
  scrollX?: boolean;
  trades: TradeItem[];
  error: string | null;
  loading: boolean;
  onInspect?: (trade: TradeItem) => void;
  onSelectSymbol?: (symbol: string) => void;
}) {
  const [query, setQuery] = useState("");
  const filtered = useMemo(() => {
    const needle = query.trim().toUpperCase();
    return needle
      ? trades.filter((trade) => trade.symbol.includes(needle) || (trade.run_id || "").toUpperCase().includes(needle))
      : trades;
  }, [trades, query]);
  const status = error ? "degraded" : loading ? "loading" : "ok";
  return (
    <Panel
      fit={fit}
      title="Recent trades"
      subtitle="Recorded stop is the execution record, not proof of a live broker stop. Select a linked trade for protection evidence."
      status={status}
      actions={<TextInput value={query} onValueChange={setQuery} placeholder="Symbol or run" className="w-48 !bg-panel-alt !ring-border" />}
    >
      {error && <StateMessage text={`Could not load trades: ${error}`} error />}
      {!error && trades.length === 0 && <StateMessage text="No trades recorded yet." />}
      {!error && trades.length > 0 && filtered.length === 0 && <StateMessage text="No trades match this filter." />}
      {!error && filtered.length > 0 && (
        <TradeTable trades={filtered} onInspect={onInspect} onSelectSymbol={onSelectSymbol} scrollX={scrollX} />
      )}
    </Panel>
  );
}
