import { useMemo } from "react";
import { Badge, Callout } from "@tremor/react";
import { legacyCreateColumnHelper as createColumnHelper, type LegacyColumnDef } from "@tanstack/react-table/legacy";
import { PositionItem } from "../api/client";
import { fmtClock, fmtMoney, fmtNum, pnlClass } from "../lib/format";
import { Panel, StateMessage } from "./ui/Panel";
import { DataTable } from "./ui/DataTable";

const columnHelper = createColumnHelper<PositionItem>();

/* Totals row (owner request, 2026-09-18: "make it noticeable").
 *
 * Only the columns where addition means something get a number.
 *
 * - Qty, Market value: added straight across every row, which is what
 *   those columns are — a count of shares held and dollars of exposure.
 * - Day P&L, Unrealized P&L: added across the DIRECTIONAL rows only,
 *   because cash parking already renders as "—"/"excluded" in those two
 *   columns. A total that quietly included what the rows above exclude
 *   would not be the total of what is on screen.
 * - Avg entry, Price: left BLANK. They are per-share prices of different
 *   companies; adding them produces a number that means nothing, and a
 *   share-weighted average across unrelated instruments means nothing
 *   either. Nothing is invented to fill the cell.
 * - Symbol, Role, Sector: not numbers. Symbol carries the row's label.
 *
 * The note under the table states all of that in words, including any
 * position the desk has no day figure for, so a reader never has to
 * guess what went into a total.
 */
function positionTotals(positions: PositionItem[]) {
  const directional = positions.filter((position) => !position.is_cash_equivalent);
  const dayRows = directional.filter((position) => position.unrealized_intraday_pnl !== null);
  const sum = (values: (number | null)[]) =>
    values.reduce<number>((total, value) => total + (value ?? 0), 0);
  return {
    count: positions.length,
    directionalCount: directional.length,
    missingDayFigures: directional.length - dayRows.length,
    qty: sum(positions.map((position) => position.qty)),
    marketValue: sum(positions.map((position) => position.market_value)),
    dayPnl: dayRows.length ? sum(dayRows.map((position) => position.unrealized_intraday_pnl)) : null,
    unrealizedPnl: directional.length
      ? sum(directional.map((position) => position.unrealized_pnl))
      : null,
  };
}

export function PositionsPanel({
  positions,
  error,
  loading,
  updatedAt,
  onSelectSymbol,
  onInspectSymbol,
}: {
  positions: PositionItem[];
  error: string | null;
  loading: boolean;
  updatedAt?: Date | null;
  /** Ticker-cell-only: charts the symbol, never opens a popup. */
  onSelectSymbol?: (symbol: string) => void;
  /** Row-body handler (everything except the ticker cell): opens the
   * same candidate/trade detail modal Orders opens, resolved by symbol
   * (see inspectPositionSymbol in App.tsx). Owner-reversed 2026-09-25 —
   * a row click used to only re-chart, matching onSelectSymbol above;
   * now it matches OrdersPanel's onInspect instead. */
  onInspectSymbol?: (symbol: string) => void;
}) {
  const everLoaded = Boolean(updatedAt);
  const status = error ? (everLoaded ? "stale" : "error") : loading ? "loading" : "ok";
  const columns = useMemo(
    () => [
      columnHelper.accessor("symbol", {
        header: "Symbol",
        cell: (info) =>
          onSelectSymbol ? (
            // stopPropagation mirrors OrdersPanel's symbol cell exactly:
            // the ticker must always chart-only, even though the row
            // itself now opens the detail popup below (onInspectSymbol).
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
      columnHelper.accessor("direction", {
        header: "Role",
        cell: (info) => {
          const value = info.getValue();
          const color = info.row.original.is_cash_equivalent
            ? "slate"
            : value === "bearish_hedge"
              ? "fuchsia"
              : value === "short"
                ? "rose"
                : "emerald";
          return (
            <Badge color={color} size="xs">
              {info.row.original.is_cash_equivalent ? "cash parking" : value.replace(/_/g, " ")}
            </Badge>
          );
        },
      }),
      columnHelper.accessor("qty", { header: "Qty", cell: (info) => fmtNum(info.getValue()) }),
      columnHelper.accessor("avg_entry", { header: "Avg entry", cell: (info) => fmtMoney(info.getValue()) }),
      columnHelper.accessor("current_price", { header: "Price", cell: (info) => fmtMoney(info.getValue()) }),
      columnHelper.accessor("market_value", { header: "Market value", cell: (info) => fmtMoney(info.getValue()) }),
      columnHelper.accessor("unrealized_intraday_pnl", {
        header: "Day P&L",
        cell: (info) => info.row.original.is_cash_equivalent ? "—" : <span className={pnlClass(info.getValue())}>{fmtMoney(info.getValue())}</span>,
      }),
      columnHelper.accessor("unrealized_pnl", {
        header: "Unrealized P&L",
        cell: (info) => info.row.original.is_cash_equivalent ? "excluded" : <span className={pnlClass(info.getValue())}>{fmtMoney(info.getValue())}</span>,
      }),
      columnHelper.accessor("sector", { header: "Sector", cell: (info) => info.getValue() || "—" }),
    ] as LegacyColumnDef<PositionItem, unknown>[],
    [onSelectSymbol]
  );

  const totals = useMemo(() => positionTotals(positions), [positions]);
  // Keyed by COLUMN ID so the row survives the reordering and resizing
  // this table already supports — see DataTable's `footer`.
  const footer = useMemo(
    () => ({
      symbol: (
        <span className="uppercase tracking-wide">
          Total · {totals.count} {totals.count === 1 ? "position" : "positions"}
        </span>
      ),
      qty: fmtNum(totals.qty),
      market_value: fmtMoney(totals.marketValue),
      unrealized_intraday_pnl:
        totals.dayPnl === null ? (
          <span className="text-dim">not available</span>
        ) : (
          <span className={pnlClass(totals.dayPnl)}>{fmtMoney(totals.dayPnl)}</span>
        ),
      unrealized_pnl:
        totals.unrealizedPnl === null ? (
          <span className="text-dim">not available</span>
        ) : (
          <span className={pnlClass(totals.unrealizedPnl)}>{fmtMoney(totals.unrealizedPnl)}</span>
        ),
    }),
    [totals],
  );

  return (
    <Panel
      title="Positions"
      subtitle="Directional holdings and cash parking are identified separately. Select a symbol to inspect it."
      status={status}
      staleSince={updatedAt}
    >
      {error && !everLoaded && <StateMessage text={`Positions read failed: ${error}`} error />}
      {!error && positions.length === 0 && <StateMessage text="No open positions." />}
      {error && everLoaded && (
        <Callout title="Last known positions" color="amber" className="mb-3 !bg-panel-alt">
          As of {(updatedAt ? fmtClock(updatedAt) : undefined) || "an earlier fetch"}; fresh fetch failed ({error}).
        </Callout>
      )}
      {positions.length > 0 && (
        // pb-5 (20px): real breathing room below the table's last row,
        // inside the card body. panel-body's own py-3 (12px, index.css)
        // applies evenly top/bottom at the CARD edge, but the ring-bordered
        // DataTable wrapper sat flush against that bottom padding with
        // nothing beyond it, reading as cramped. Owner-requested 2026-09-11;
        // real CSS padding, not synthetic empty rows.
        <div className="pb-5">
          <DataTable
            data={positions}
            columns={columns}
            getRowId={(position) => position.symbol}
            initialSorting={[{ id: "market_value", desc: true }]}
            onRowClick={onInspectSymbol ? (position) => onInspectSymbol(position.symbol) : undefined}
            // Same disclosure chevron + row hover-highlight OrdersPanel uses
            // (showRowChevron doc in DataTable.tsx): the row (as opposed to
            // the ticker cell) now opens the detail popup, same as Orders.
            showRowChevron
            resizable
            reorderable
            storageKey="positions-columns"
            footer={footer}
          />
          {/* Says in words what each total does and does not include, so
              no figure in the row above needs interpreting. */}
          <p className="mt-2 font-sans text-[0.78rem] leading-relaxed text-dim">
            The total row adds the quantity, market value and profit-and-loss columns.
            {totals.count !== totals.directionalCount
              ? " Cash parking is left out of both profit-and-loss totals, exactly as it is in the rows above."
              : ""}
            {totals.missingDayFigures > 0
              ? ` ${totals.missingDayFigures} ${
                  totals.missingDayFigures === 1 ? "position has" : "positions have"
                } no day profit-and-loss recorded and ${
                  totals.missingDayFigures === 1 ? "is" : "are"
                } not in that total.`
              : ""}{" "}
            Average entry and price are left blank: they are per-share prices of different
            companies, so there is no meaningful figure to add them into.
          </p>
        </div>
      )}
    </Panel>
  );
}
