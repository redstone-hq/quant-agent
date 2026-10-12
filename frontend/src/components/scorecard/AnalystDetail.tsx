/* Section (c) — one analyst opened.
 *
 * Four stacked views of the same analyst, in the order a reader needs them:
 *
 *   1. Running profit, full size — `lightweight-charts`, a baseline series
 *      split at zero so the part above the line and the part below it are
 *      drawn differently, with the zero line itself labelled "break even" and
 *      an arrow marker at every crossing carrying the words "into profit" /
 *      "into loss".
 *   2. How far below its own best — `recharts`, an area that hangs DOWNWARD
 *      from zero. Depth is the "how far"; the caption under it carries the
 *      "for how long".
 *   3. How sure it said it was — a plain table splitting the same settled
 *      calls by the confidence the analyst declared on each. This is the
 *      replacement for the conviction weight the ledger used to apply (owner
 *      decision, 2026-08-31): credit is the same amount at every confidence
 *      level, so whether confidence is worth anything is SHOWN here instead
 *      of being asserted by a multiplier.
 *   4. Month by month — `recharts`, a floating waterfall: each bar spans from
 *      last month's total to this month's, so its length is that month's own
 *      contribution and its position is the running total.
 *
 * Recharts is used DIRECTLY, never through Tremor.
 *
 * READING WITHOUT COLOUR. Position relative to a drawn zero line does all the
 * work in all three. Every figure is explicitly signed, every direction
 * carries ▲/▼, gains are solid and losses outlined, and the crossing markers
 * spell out which way they went. Colour never states anything on its own.
 */

import { useEffect, useLayoutEffect, useRef, useState } from "react";
import {
  createChart,
  LineStyle,
  type IChartApi,
  type IPriceLine,
  type ISeriesApi,
  type UTCTimestamp,
} from "lightweight-charts";
import {
  Area,
  AreaChart,
  Bar,
  BarChart,
  Cell,
  LabelList,
  ReferenceLine,
  XAxis,
  YAxis,
} from "recharts";
import type { AnalystScorecardItem } from "../../api/client";
import {
  belowBestSeries,
  dayLabel,
  longestSpellBelowBest,
  monthLabel,
  profitSeries,
  signedMoney,
  toDollars,
  trendGlyph,
  waterfall,
  zeroCrossings,
} from "./scorecardModel";

/** Container width, measured when the browser can measure. Falls back to a
 * fixed width under jsdom (no ResizeObserver), which is what lets these
 * charts render in a component test at all. */
function useMeasuredWidth(fallback = 720): [React.RefObject<HTMLDivElement>, number] {
  const ref = useRef<HTMLDivElement>(null);
  const [width, setWidth] = useState(fallback);
  useLayoutEffect(() => {
    const el = ref.current;
    if (!el || typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver((entries) => {
      const w = entries[0]?.contentRect.width;
      if (w && w > 0) setWidth(w);
    });
    observer.observe(el);
    return () => observer.disconnect();
  }, []);
  return [ref, width];
}

function rgb(name: string): string {
  if (typeof getComputedStyle !== "function") return "rgb(128, 128, 128)";
  const raw = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  return raw ? `rgb(${raw.split(/\s+/).join(", ")})` : "rgb(128, 128, 128)";
}

function RunningProfitChart({
  item,
  dollarsPerCall,
}: {
  item: AnalystScorecardItem;
  dollarsPerCall: number;
}) {
  const containerRef = useRef<HTMLDivElement>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const seriesRef = useRef<ISeriesApi<"Baseline"> | null>(null);
  // The break-even line is recreated whenever the data changes; without
  // holding on to the previous one, every re-render stacks another "$0" label
  // on the axis (seen and fixed during browser verification).
  const zeroLineRef = useRef<IPriceLine | null>(null);

  useEffect(() => {
    const el = containerRef.current;
    if (!el) return;
    const chart = createChart(el, {
      width: el.clientWidth || 720,
      height: 220,
      layout: {
        background: { color: "transparent" },
        textColor: rgb("--c-ink-dim"),
        fontSize: 11,
        // The library's own logo is drawn INSIDE the plot at bottom-left,
        // where it sat on top of the line at its origin — the first settled
        // call, which is exactly the point a reader looks for. Turned off
        // here and credited in the panel footer instead, which is what the
        // option exists for.
        attributionLogo: false,
      },
      grid: {
        vertLines: { color: rgb("--c-border"), style: LineStyle.Dotted },
        horzLines: { color: rgb("--c-border"), style: LineStyle.Dotted },
      },
      rightPriceScale: { borderColor: rgb("--c-border") },
      timeScale: { borderColor: rgb("--c-border"), fixLeftEdge: true, fixRightEdge: true },
      crosshair: { horzLine: { visible: false } },
      handleScroll: false,
      handleScale: false,
    });
    const series = chart.addBaselineSeries({
      baseValue: { type: "price", price: 0 },
      topLineColor: rgb("--c-green"),
      topFillColor1: "rgba(52, 211, 153, 0.28)",
      topFillColor2: "rgba(52, 211, 153, 0.02)",
      bottomLineColor: rgb("--c-red"),
      bottomFillColor1: "rgba(251, 90, 90, 0.02)",
      bottomFillColor2: "rgba(251, 90, 90, 0.28)",
      lineWidth: 2,
      priceLineVisible: false,
      lastValueVisible: false,
      // The axis must read in the same signed dollars as the rest of the
      // page. Left at the library default it prints "800.00", a bare number
      // in no stated unit, which is exactly the kind of figure the reader
      // would have to ask about.
      priceFormat: { type: "custom", minMove: 1, formatter: (price: number) => signedMoney(price) },
    });
    chartRef.current = chart;
    seriesRef.current = series;

    const observer =
      typeof ResizeObserver !== "undefined"
        ? new ResizeObserver((entries) => {
            const w = entries[0]?.contentRect.width;
            if (w && w > 0) chart.applyOptions({ width: w });
          })
        : null;
    observer?.observe(el);
    return () => {
      observer?.disconnect();
      chart.remove();
      chartRef.current = null;
      seriesRef.current = null;
    };
  }, []);

  useEffect(() => {
    const series = seriesRef.current;
    const chart = chartRef.current;
    if (!series || !chart) return;
    const points = profitSeries(item, dollarsPerCall);
    series.setData(points.map((p) => ({ time: p.time as UTCTimestamp, value: p.dollars })));
    series.setMarkers(
      zeroCrossings(points).map((crossing) => ({
        time: crossing.time as UTCTimestamp,
        position: crossing.direction === "into profit" ? ("belowBar" as const) : ("aboveBar" as const),
        shape: crossing.direction === "into profit" ? ("arrowUp" as const) : ("arrowDown" as const),
        color: crossing.direction === "into profit" ? rgb("--c-green") : rgb("--c-red"),
        text: crossing.direction,
      })),
    );
    if (zeroLineRef.current) series.removePriceLine(zeroLineRef.current);
    zeroLineRef.current = series.createPriceLine({
      price: 0,
      color: rgb("--c-ink-faint"),
      lineWidth: 1,
      lineStyle: LineStyle.Dashed,
      axisLabelVisible: true,
      title: "break even",
    });
    chart.timeScale().fitContent();
  }, [item, dollarsPerCall]);

  return <div ref={containerRef} data-testid="running-profit-chart" className="w-full" />;
}

function BelowBestStrip({
  item,
  dollarsPerCall,
}: {
  item: AnalystScorecardItem;
  dollarsPerCall: number;
}) {
  const [ref, width] = useMeasuredWidth();
  const data = belowBestSeries(item, dollarsPerCall).map((point, index) => ({ ...point, index }));
  const worst = Math.min(0, ...data.map((d) => d.belowBest));
  return (
    <div ref={ref} className="w-full">
      <AreaChart width={width} height={110} data={data} margin={{ top: 6, right: 8, bottom: 4, left: 8 }}>
        <ReferenceLine y={0} stroke={rgb("--c-ink-faint")} strokeDasharray="2 3" />
        <XAxis dataKey="index" hide />
        <YAxis
          domain={[Math.min(worst * 1.15, -1), 0]}
          width={58}
          tick={{ fill: rgb("--c-ink-faint"), fontSize: 10 }}
          tickFormatter={(v: number) => signedMoney(v)}
        />
        <Area
          type="stepAfter"
          dataKey="belowBest"
          stroke={rgb("--c-amber")}
          strokeWidth={1.4}
          fill={rgb("--c-amber")}
          fillOpacity={0.18}
          isAnimationActive={false}
          dot={false}
        />
      </AreaChart>
    </div>
  );
}

function MonthlyWaterfall({
  item,
  dollarsPerCall,
}: {
  item: AnalystScorecardItem;
  dollarsPerCall: number;
}) {
  const [ref, width] = useMeasuredWidth();
  const steps = waterfall(item, dollarsPerCall);
  const values = steps.flatMap((s) => s.span);
  const min = Math.min(0, ...values);
  const max = Math.max(0, ...values);
  const pad = (max - min || 1) * 0.15;
  return (
    <div ref={ref} className="w-full">
      <BarChart width={width} height={200} data={steps} margin={{ top: 22, right: 8, bottom: 4, left: 8 }}>
        <ReferenceLine y={0} stroke={rgb("--c-ink")} strokeWidth={1} />
        <XAxis
          dataKey="label"
          tick={{ fill: rgb("--c-ink-dim"), fontSize: 10 }}
          tickLine={false}
          axisLine={{ stroke: rgb("--c-border") }}
        />
        <YAxis
          domain={[min - pad, max + pad]}
          width={62}
          tick={{ fill: rgb("--c-ink-faint"), fontSize: 10 }}
          tickFormatter={(v: number) => signedMoney(v)}
        />
        <Bar dataKey="span" isAnimationActive={false}>
          {steps.map((step) => (
            <Cell
              key={step.month}
              // Solid for a month that made money, outlined for one that lost
              // it — shape, so the two never rely on being told apart by hue.
              fill={step.change >= 0 ? rgb("--c-green") : "transparent"}
              stroke={step.change >= 0 ? rgb("--c-green") : rgb("--c-red")}
              strokeWidth={1.6}
            />
          ))}
          <LabelList
            dataKey="change"
            position="top"
            fill={rgb("--c-ink-dim")}
            fontSize={10}
            formatter={(v: number) => `${trendGlyph(v)} ${signedMoney(v)}`}
          />
        </Bar>
      </BarChart>
    </div>
  );
}

/** How confidently the analyst spoke, and what each level was worth.
 *
 * This section is the replacement for the conviction weight the ledger used
 * to apply (removed 2026-08-31 by owner decision). Credit is now the same
 * amount whatever the analyst declared, so the question "is this one worth
 * more when it sounds sure?" is answered by showing the records side by side
 * rather than by a multiplier deciding it in advance. Nothing here ranks the
 * levels against each other.
 *
 * Rendered as a list, not table markup — `componentPolicy.test.ts` bans
 * hand-written tables outright, and the cockpit's `DataTable` is a
 * virtualised component built for hundreds of rows, not for the two or three
 * confidence levels one analyst has ever used.
 */
function ConfidenceBreakdown({
  item,
  dollarsPerCall,
}: {
  item: AnalystScorecardItem;
  dollarsPerCall: number;
}) {
  if (item.by_confidence.length === 0) return null;
  return (
    <ul className="m-0 list-none space-y-2 p-0" data-testid="by-confidence">
      {item.by_confidence.map((row) => {
        const total = row.cumulative_credit * dollarsPerCall;
        const win = toDollars(row.avg_win, dollarsPerCall);
        const loss = toDollars(row.avg_loss, dollarsPerCall);
        return (
          <li
            key={row.conviction}
            className="rounded-lg border border-border bg-panel-alt px-3 py-2"
          >
            <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
              <span className="text-[length:var(--fs-body)] font-semibold text-ink">
                {row.conviction} confidence
              </span>
              <span className="font-mono text-[length:var(--fs-meta)] text-dim">
                {row.calls_right} of {row.resolved_calls} settled calls made money
                {row.hit_rate_pct === null ? "" : ` (${Math.round(row.hit_rate_pct)}%)`}
              </span>
              <span className="ml-auto font-mono text-[length:var(--fs-body)] font-semibold text-ink">
                <span aria-hidden="true">{trendGlyph(total)}</span> {signedMoney(total)}
              </span>
            </div>
            <div className="mt-0.5 text-[length:var(--fs-meta)] text-dim">
              Typical win{" "}
              <span className="font-mono text-ink">
                {win === null ? "no wins yet" : signedMoney(win)}
              </span>
              , typical loss{" "}
              <span className="font-mono text-ink">
                {loss === null ? "no losses yet" : signedMoney(loss)}
              </span>
              .
            </div>
          </li>
        );
      })}
    </ul>
  );
}

export function AnalystDetail({
  item,
  dollarsPerCall,
}: {
  item: AnalystScorecardItem;
  dollarsPerCall: number;
}) {
  const total = item.cumulative_credit * dollarsPerCall;
  const below = item.below_best * dollarsPerCall;
  const spell = longestSpellBelowBest(item);
  const peak = item.peak * dollarsPerCall;

  return (
    <section className="flex flex-col gap-5" data-testid="analyst-detail">
      <header className="flex flex-wrap items-baseline gap-x-4 gap-y-1">
        <h3 className="m-0 text-[length:var(--fs-subhead)] font-semibold text-ink">{item.analyst}</h3>
        <p className="m-0 text-[length:var(--fs-meta)] text-dim">
          {item.calls_right} of {item.resolved_calls} settled calls made money. Running total{" "}
          <span aria-hidden="true">{trendGlyph(total)}</span>{" "}
          <strong className="font-mono text-ink">{signedMoney(total)}</strong>.
        </p>
      </header>

      <div>
        <h4 className="m-0 mb-1 text-[length:var(--fs-body)] font-semibold text-ink">
          What this analyst&rsquo;s opinions have been worth, one settled call at a time
        </h4>
        <p className="m-0 mb-2 text-[length:var(--fs-meta)] leading-snug text-dim">
          The line starts at zero on this analyst&rsquo;s first settled call and moves by the amount that
          call earned or cost. Above the marked break-even line is money made; below it is money lost.
          Arrows mark each time it crossed from one to the other.
        </p>
        <RunningProfitChart item={item} dollarsPerCall={dollarsPerCall} />
      </div>

      <div>
        <h4 className="m-0 mb-1 text-[length:var(--fs-body)] font-semibold text-ink">
          How far below its own best it has fallen, and for how long
        </h4>
        <p className="m-0 mb-2 text-[length:var(--fs-meta)] leading-snug text-dim">
          The best running total this analyst has ever reached is{" "}
          <strong className="font-mono text-ink">{signedMoney(peak)}</strong>. This strip hangs downward
          from that best: the deeper it goes, the further below its own high-water mark the analyst had
          slipped at that moment.{" "}
          {below > 0 ? (
            <>
              It is <strong className="font-mono text-ink">{signedMoney(-below)}</strong> below its best
              right now, and has been for {item.calls_since_peak}{" "}
              {item.calls_since_peak === 1 ? "settled call" : "settled calls"} — since{" "}
              {dayLabel(item.below_best_since)}.
            </>
          ) : (
            <>It is at its own best right now.</>
          )}{" "}
          The longest it has ever stayed below a previous best is {spell}{" "}
          {spell === 1 ? "settled call" : "settled calls"}.
        </p>
        <BelowBestStrip item={item} dollarsPerCall={dollarsPerCall} />
      </div>

      <div>
        <h4 className="m-0 mb-1 text-[length:var(--fs-body)] font-semibold text-ink">
          When it said it was sure, and when it hedged
        </h4>
        <p className="m-0 mb-2 max-w-[75ch] text-[length:var(--fs-meta)] leading-snug text-dim">
          This analyst says how strongly it holds each view. That does{" "}
          <strong className="text-ink">not</strong> change what a call is worth: every settled call is
          credited or charged the same amount whether it was stated confidently or hedged. Its record is
          simply split up here by what it said, so you can judge for yourself whether this analyst is worth
          more when it sounds sure. The counts are raw — a perfect record over two calls is not a perfect
          record over fifty.
        </p>
        <ConfidenceBreakdown item={item} dollarsPerCall={dollarsPerCall} />
      </div>

      <div>
        <h4 className="m-0 mb-1 text-[length:var(--fs-body)] font-semibold text-ink">Month by month</h4>
        <p className="m-0 mb-2 text-[length:var(--fs-meta)] leading-snug text-dim">
          Each bar starts where the previous month ended, so its length is what that month alone added or
          took away. Bars that made money are filled in; bars that lost money are hollow outlines. Every
          bar is labelled with its signed amount.
        </p>
        <MonthlyWaterfall item={item} dollarsPerCall={dollarsPerCall} />
        <ul className="mt-2 grid list-none gap-x-6 gap-y-1 p-0 text-[length:var(--fs-micro)] text-dim sm:grid-cols-2">
          {waterfall(item, dollarsPerCall).map((step) => (
            <li key={step.month} className="font-mono">
              <span aria-hidden="true">{trendGlyph(step.change)}</span> {monthLabel(step.month)}:{" "}
              {signedMoney(step.change)} ({step.callsRight} of {step.resolvedCalls} right) — running total{" "}
              {signedMoney(step.total)}
            </li>
          ))}
        </ul>
      </div>
    </section>
  );
}
