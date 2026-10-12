/* Section (b) — one row per analyst.
 *
 * Uses the cockpit's existing `DataTable`. Hand-written table markup is banned
 * outright by `componentPolicy.test.ts`, which greps the source for it — so
 * this file must not even quote the tag. The two in-cell graphics are plain
 * SVG:
 *
 *   - Typical loss vs typical win: two bars growing OUTWARD from a marked
 *     centre line, loss to the left, win to the right. Which side of the
 *     centre a bar sits on is the meaning; both bars also carry a signed
 *     dollar figure, and the loss bar is outlined while the win bar is solid.
 *     Nothing here depends on telling red from green.
 *
 *   - Running profit: a sparkline against a drawn zero line, so "above or
 *     below the line" is a position fact. Deliberately NOT lightweight-charts:
 *     that library mounts a canvas and a ResizeObserver per instance, and one
 *     per table row is both heavy and fragile inside a virtualised cell. The
 *     full-size version of this same series in section (c) IS lightweight-
 *     charts, where a real chart earns its cost.
 */

import { useMemo } from "react";
import type { LegacyColumnDef } from "@tanstack/react-table/legacy";
import type { AnalystScorecardItem } from "../../api/client";
import { DataTable } from "../ui/DataTable";
import { callCounts, hitRateText, signedMoney, toDollars, trendGlyph } from "./scorecardModel";

const BAR_W = 108;
const BAR_H = 22;
const SPARK_W = 104;
const SPARK_H = 26;

function OpposedBars({
  loss,
  win,
  scale,
  analyst,
}: {
  /** Negative dollars, or null when this analyst has never lost. */
  loss: number | null;
  /** Positive dollars, or null when this analyst has never won. */
  win: number | null;
  /** Largest magnitude across every row, so bars are comparable down the column. */
  scale: number;
  analyst: string;
}) {
  const mid = BAR_W / 2;
  const half = mid - 2;
  const lossW = loss === null ? 0 : Math.min(half, (Math.abs(loss) / scale) * half);
  const winW = win === null ? 0 : Math.min(half, (Math.abs(win) / scale) * half);
  return (
    <div className="flex items-center gap-2">
      <svg
        width={BAR_W}
        height={BAR_H}
        role="img"
        aria-label={`${analyst}: typical loss ${loss === null ? "none yet" : signedMoney(loss)}, typical win ${
          win === null ? "none yet" : signedMoney(win)
        }`}
      >
        <rect
          x={mid - lossW}
          y={5}
          width={lossW}
          height={BAR_H - 10}
          fill="none"
          stroke="rgb(var(--c-red))"
          strokeWidth={1.5}
        />
        <rect x={mid} y={5} width={winW} height={BAR_H - 10} fill="rgb(var(--c-green))" />
        <line x1={mid} y1={1} x2={mid} y2={BAR_H - 1} stroke="rgb(var(--c-ink))" strokeWidth={1} />
      </svg>
      <span className="font-mono text-[length:var(--fs-micro)] leading-tight">
        <span className="text-dim">{loss === null ? "no losses yet" : signedMoney(loss)}</span>
        <span className="text-faint"> / </span>
        <span className="text-ink">{win === null ? "no wins yet" : signedMoney(win)}</span>
      </span>
    </div>
  );
}

function ProfitSparkline({ item, dollarsPerCall }: { item: AnalystScorecardItem; dollarsPerCall: number }) {
  const values = item.cumulative.map((p) => p.cumulative * dollarsPerCall);
  if (values.length === 0) return <span className="text-faint">—</span>;
  const min = Math.min(0, ...values);
  const max = Math.max(0, ...values);
  const span = max - min || 1;
  const y = (v: number) => SPARK_H - 3 - ((v - min) / span) * (SPARK_H - 6);
  const step = values.length > 1 ? SPARK_W / (values.length - 1) : 0;
  const points = values.map((v, i) => `${(i * step).toFixed(1)},${y(v).toFixed(1)}`).join(" ");
  return (
    <svg
      width={SPARK_W}
      height={SPARK_H}
      role="img"
      aria-label={`${item.analyst} running profit, ${item.resolved_calls} settled calls, ending ${signedMoney(
        values[values.length - 1],
      )}`}
    >
      <line x1={0} y1={y(0)} x2={SPARK_W} y2={y(0)} stroke="rgb(var(--c-ink-faint))" strokeDasharray="2 3" />
      <polyline points={points} fill="none" stroke="rgb(var(--c-accent))" strokeWidth={1.4} />
      <circle cx={SPARK_W} cy={y(values[values.length - 1])} r={2.4} fill="rgb(var(--c-accent))" />
    </svg>
  );
}

export function AnalystRankedTable({
  analysts,
  dollarsPerCall,
  selected,
  onSelect,
}: {
  analysts: AnalystScorecardItem[];
  dollarsPerCall: number;
  selected: string | null;
  onSelect: (analyst: string) => void;
}) {
  const scale = useMemo(() => {
    const magnitudes = analysts.flatMap((a) => [
      Math.abs(toDollars(a.avg_win, dollarsPerCall) ?? 0),
      Math.abs(toDollars(a.avg_loss, dollarsPerCall) ?? 0),
    ]);
    return Math.max(1, ...magnitudes);
  }, [analysts, dollarsPerCall]);

  const columns = useMemo<LegacyColumnDef<AnalystScorecardItem, unknown>[]>(
    () => [
      {
        id: "analyst",
        header: "Analyst",
        accessorFn: (row) => row.analyst,
        cell: ({ row }) => (
          <span className="font-sans font-semibold text-ink">
            {row.original.analyst}
            {row.original.analyst === selected && (
              <span className="ml-2 text-[length:var(--fs-micro)] font-normal uppercase tracking-wide text-accent">
                open below
              </span>
            )}
          </span>
        ),
      },
      {
        id: "right",
        header: "Right how often",
        accessorFn: (row) => row.hit_rate_pct ?? -1,
        cell: ({ row }) => (
          <span>
            <span className="text-ink">{callCounts(row.original)}</span>
            <span className="text-faint"> · </span>
            <span className="text-dim">{hitRateText(row.original)}</span>
          </span>
        ),
      },
      {
        id: "typical",
        header: "Typical loss | typical win",
        enableSorting: false,
        cell: ({ row }) => (
          <OpposedBars
            analyst={row.original.analyst}
            loss={toDollars(row.original.avg_loss, dollarsPerCall)}
            win={toDollars(row.original.avg_win, dollarsPerCall)}
            scale={scale}
          />
        ),
      },
      {
        id: "running",
        header: "Running profit",
        enableSorting: false,
        cell: ({ row }) => <ProfitSparkline item={row.original} dollarsPerCall={dollarsPerCall} />,
      },
      {
        id: "total",
        header: "Total",
        accessorFn: (row) => row.cumulative_credit,
        cell: ({ row }) => {
          const total = row.original.cumulative_credit * dollarsPerCall;
          return (
            <span className="font-semibold">
              <span aria-hidden="true">{trendGlyph(total)}</span>{" "}
              <span className={total > 0 ? "text-pos" : total < 0 ? "text-neg" : "text-dim"}>
                {signedMoney(total)}
              </span>
            </span>
          );
        },
      },
    ],
    [dollarsPerCall, scale, selected],
  );

  return (
    <DataTable
      data={analysts}
      columns={columns}
      getRowId={(row) => row.analyst}
      initialSorting={[{ id: "total", desc: true }]}
      onRowClick={(row) => onSelect(row.analyst)}
    />
  );
}
