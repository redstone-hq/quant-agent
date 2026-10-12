/* Pure derivations for the analyst scorecard. No React, no DOM, no fetch —
 * so the arithmetic the page depends on is testable on its own, the same
 * split `funnelShared.tsx` and `buildResearchDesk.ts` already use.
 *
 * The backend returns everything in R (profit as a multiple of the risk the
 * position was opened with). Nothing in this product shows a reader the
 * letter "R": every number that reaches the screen goes through `toDollars`
 * first, using the single `risk_dollars_per_call` figure the endpoint states.
 */

import type {
  AnalystScorecardItem,
  AnalystScorecardResponse,
  ScorecardIdea,
} from "../../api/client";

/** R -> the worked-example dollars the page is written in. */
export function toDollars(r: number | null | undefined, dollarsPerCall: number): number | null {
  if (r === null || r === undefined || !Number.isFinite(r)) return null;
  return r * dollarsPerCall;
}

/** "+$210" / "-$60" / "$0". Always signed for a non-zero value, because the
 * sign is what carries the meaning when colour cannot. */
export function signedMoney(v: number | null | undefined): string {
  if (v === null || v === undefined || !Number.isFinite(v)) return "—";
  const rounded = Math.round(v);
  const sign = rounded > 0 ? "+" : rounded < 0 ? "−" : "";
  return `${sign}$${Math.abs(rounded).toLocaleString("en-US")}`;
}

/** ▲ up, ▼ down, · flat. Paired with every signed figure so direction never
 * rests on colour alone. */
export function trendGlyph(v: number | null | undefined): string {
  if (v === null || v === undefined || !Number.isFinite(v) || v === 0) return "·";
  return v > 0 ? "▲" : "▼";
}

/** "3 of 8" — the raw counts, always shown next to any percentage. Spec §9.5
 * item 8: no sample floor ever hides a score, so the counts have to be right
 * there for the reader to judge it. */
export function callCounts(item: AnalystScorecardItem): string {
  return `${item.calls_right} of ${item.resolved_calls}`;
}

export function hitRateText(item: AnalystScorecardItem): string {
  if (item.hit_rate_pct === null || item.resolved_calls === 0) return "no settled calls yet";
  return `${Math.round(item.hit_rate_pct)}%`;
}

/** "August 2026" from "2026-08". Built by hand rather than through `Date`,
 * which would shift a bare month into the viewer's timezone. */
const MONTH_NAMES = [
  "January", "February", "March", "April", "May", "June",
  "July", "August", "September", "October", "November", "December",
];

export function monthLabel(month: string): string {
  const [year, mm] = (month || "").split("-");
  const index = Number(mm) - 1;
  if (!year || Number.isNaN(index) || index < 0 || index > 11) return month || "—";
  return `${MONTH_NAMES[index]} ${year}`;
}

/** "10 June 2026" from "2026-06-10 15:00:00" (or an ISO string). */
export function dayLabel(stamp: string | null | undefined): string {
  const text = (stamp || "").trim();
  if (text.length < 10) return "—";
  const [year, mm, dd] = text.slice(0, 10).split("-");
  const index = Number(mm) - 1;
  if (!year || Number.isNaN(index) || index < 0 || index > 11) return text.slice(0, 10);
  return `${Number(dd)} ${MONTH_NAMES[index]} ${year}`;
}

// ---------------------------------------------------------------------------
// Section (a) — the two slope panels
// ---------------------------------------------------------------------------

export interface SlopeRow {
  analyst: string;
  /** Value at the earliest month both panels share. */
  from: number;
  /** Value at the latest month. */
  to: number;
  change: number;
  /** False when this analyst had settled nothing yet at the earlier date. Its
   * left-hand point is not zero, it does not exist — drawn as a single mark at
   * the right end with the month it started, never as a line down from zero
   * that would read as a collapse it never had. */
  hasFrom: boolean;
  /** The month this analyst's record starts, for the `hasFrom: false` label. */
  firstMonth: string;
}

export interface DeskSlopes {
  fromMonth: string;
  toMonth: string;
  /** Share of settled calls that made money, in percent, at each month. */
  accuracy: SlopeRow[];
  /** Running total in dollars at each month. */
  money: SlopeRow[];
  /** Analysts getting MORE accurate while LOSING money — the contrast this
   * whole section exists to surface. */
  moreAccurateButLosing: string[];
}

function atMonth<T>(
  item: AnalystScorecardItem,
  month: string,
  pick: (m: AnalystScorecardItem["monthly"][number]) => T,
  fallback: T,
): T {
  // The last month at or before `month` — an analyst that resolved nothing in
  // a given month still carries its record forward, rather than dropping to
  // zero as though its history had been erased.
  let out = fallback;
  for (const point of item.monthly) {
    if (point.month <= month) out = pick(point);
  }
  return out;
}

/** Two dates: the earliest month the desk has a full record for, and the
 * latest. With only one month of history both ends are that month, which
 * renders as a flat line — honest, and better than hiding the panel. */
export function buildDeskSlopes(
  analysts: AnalystScorecardItem[],
  months: string[],
  dollarsPerCall: number,
): DeskSlopes | null {
  const ordered = [...months].sort();
  if (ordered.length === 0 || analysts.length === 0) return null;
  const fromMonth = ordered[0];
  const toMonth = ordered[ordered.length - 1];

  const accuracy: SlopeRow[] = [];
  const money: SlopeRow[] = [];
  for (const item of analysts) {
    const firstMonth = item.monthly[0]?.month ?? toMonth;
    const hasFrom = firstMonth <= fromMonth;
    const fromHit = atMonth(item, fromMonth, (m) => m.hit_rate_pct ?? 0, 0);
    const toHit = atMonth(item, toMonth, (m) => m.hit_rate_pct ?? 0, 0);
    const fromMoney = atMonth(item, fromMonth, (m) => m.cumulative * dollarsPerCall, 0);
    const toMoney = atMonth(item, toMonth, (m) => m.cumulative * dollarsPerCall, 0);
    accuracy.push({
      analyst: item.analyst, from: fromHit, to: toHit,
      change: toHit - fromHit, hasFrom, firstMonth,
    });
    money.push({
      analyst: item.analyst, from: fromMoney, to: toMoney,
      change: toMoney - fromMoney, hasFrom, firstMonth,
    });
  }

  const moneyByName = new Map(money.map((row) => [row.analyst, row]));
  const moreAccurateButLosing = accuracy
    .filter(
      (row) =>
        row.hasFrom && row.change > 0 && (moneyByName.get(row.analyst)?.change ?? 0) < 0,
    )
    .map((row) => row.analyst);

  return { fromMonth, toMonth, accuracy, money, moreAccurateButLosing };
}

// ---------------------------------------------------------------------------
// Section (c) — one analyst opened
// ---------------------------------------------------------------------------

export interface ProfitPoint {
  /** Seconds since the epoch — lightweight-charts' numeric time, made
   * strictly increasing because the library rejects duplicate stamps and two
   * calls can settle in the same second. */
  time: number;
  dollars: number;
}

const SETTLED_STAMP = /^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2}):(\d{2})/;

/** Parse the ledger's stored timestamp to epoch seconds, treating it as UTC —
 * which is what `src/storage/db.py` writes. Returns null, never a guess, for
 * anything that does not parse. */
export function stampToEpochSeconds(stamp: string | null | undefined): number | null {
  const text = (stamp || "").trim();
  const full = SETTLED_STAMP.exec(text);
  if (full) {
    const [, y, mo, d, h, mi, s] = full;
    return Math.floor(Date.UTC(+y, +mo - 1, +d, +h, +mi, +s) / 1000);
  }
  if (/^\d{4}-\d{2}-\d{2}$/.test(text)) {
    const [y, mo, d] = text.split("-");
    return Math.floor(Date.UTC(+y, +mo - 1, +d) / 1000);
  }
  return null;
}

export function profitSeries(item: AnalystScorecardItem, dollarsPerCall: number): ProfitPoint[] {
  const out: ProfitPoint[] = [];
  let previous = -Infinity;
  item.cumulative.forEach((point, index) => {
    const parsed = stampToEpochSeconds(point.resolved_at);
    // A row with an unusable timestamp still belongs in the series — its
    // position in the sequence is real even when its date is not — so it is
    // placed one second after its predecessor rather than dropped.
    let time = parsed ?? (previous === -Infinity ? index : previous + 1);
    if (time <= previous) time = previous + 1;
    previous = time;
    out.push({ time, dollars: point.cumulative * dollarsPerCall });
  });
  return out;
}

export interface CrossingMarker {
  time: number;
  /** "into profit" | "into loss" — spelled out, never colour-only. */
  direction: "into profit" | "into loss";
}

/** Where the running total crosses zero. Marked explicitly because the zero
 * line is the one reference point on the chart that means something. */
export function zeroCrossings(series: ProfitPoint[]): CrossingMarker[] {
  const out: CrossingMarker[] = [];
  for (let i = 1; i < series.length; i += 1) {
    const before = series[i - 1].dollars;
    const after = series[i].dollars;
    if (before < 0 && after >= 0) out.push({ time: series[i].time, direction: "into profit" });
    else if (before >= 0 && after < 0) out.push({ time: series[i].time, direction: "into loss" });
  }
  return out;
}

export interface BelowBestPoint {
  time: number;
  /** Always <= 0, so the strip hangs downward from the top edge: how far
   * below its own best this analyst was at that moment, in dollars. */
  belowBest: number;
  label: string;
}

export function belowBestSeries(item: AnalystScorecardItem, dollarsPerCall: number): BelowBestPoint[] {
  const times = profitSeries(item, dollarsPerCall);
  return item.cumulative.map((point, index) => {
    const depth = Math.abs(point.below_best * dollarsPerCall);
    return {
      time: times[index]?.time ?? index,
      // `depth === 0 ? 0` rather than `-0`, which recharts renders as "-0" on
      // an axis label and reads as a tiny loss that is not there.
      belowBest: depth === 0 ? 0 : -depth,
      label: dayLabel(point.resolved_at),
    };
  });
}

/** The longest run of consecutive settled calls spent below the analyst's own
 * best — the "for how long" the drawdown strip is labelled with. */
export function longestSpellBelowBest(item: AnalystScorecardItem): number {
  let longest = 0;
  let run = 0;
  for (const point of item.cumulative) {
    if (point.below_best > 0) {
      run += 1;
      longest = Math.max(longest, run);
    } else {
      run = 0;
    }
  }
  return longest;
}

export interface WaterfallStep {
  month: string;
  label: string;
  /** This month's own contribution, in dollars. */
  change: number;
  /** Running total at the end of this month, in dollars. */
  total: number;
  /** [from, to] bar span so the bar floats between last month's total and
   * this month's — a waterfall step, not a column from zero. */
  span: [number, number];
  resolvedCalls: number;
  callsRight: number;
}

export function waterfall(item: AnalystScorecardItem, dollarsPerCall: number): WaterfallStep[] {
  let previous = 0;
  return item.monthly.map((point) => {
    const total = point.cumulative * dollarsPerCall;
    const step: WaterfallStep = {
      month: point.month,
      label: monthLabel(point.month),
      change: point.credit * dollarsPerCall,
      total,
      span: [previous, total],
      resolvedCalls: point.resolved_calls,
      callsRight: point.calls_right,
    };
    previous = total;
    return step;
  });
}

// ---------------------------------------------------------------------------
// Section (d) — one idea traced
// ---------------------------------------------------------------------------

/** The idea worth opening by default: the most recent one that had someone on
 * BOTH sides, because a trace with no disagreement shows nothing the table
 * doesn't. Falls back to the most recent idea of any kind. */
export function defaultIdea(ideas: ScorecardIdea[]): ScorecardIdea | null {
  if (ideas.length === 0) return null;
  return ideas.find((i) => i.supported.length > 0 && i.opposed.length > 0) ?? ideas[0];
}

// ---------------------------------------------------------------------------
// The live / example switch
// ---------------------------------------------------------------------------

export type ScorecardSource = "live" | "example";

/** Live only when the endpoint actually returned scored calls. An empty
 * ledger, a read error, or a response with no analysts all fall back to the
 * committed example — and the page says so, loudly, every time. */
export function isLive(data: AnalystScorecardResponse | null): boolean {
  return Boolean(
    data &&
      data.state === "populated" &&
      data.resolved_calls_total > 0 &&
      data.analysts.length > 0,
  );
}

export interface ScorecardView {
  data: AnalystScorecardResponse;
  source: ScorecardSource;
  /** Why the example is showing, in the reader's language. Null when live. */
  exampleReason: string | null;
}

export function chooseView(
  live: AnalystScorecardResponse | null,
  example: AnalystScorecardResponse,
  fetchError: string | null,
): ScorecardView {
  if (isLive(live)) return { data: live as AnalystScorecardResponse, source: "live", exampleReason: null };
  if (fetchError) {
    return {
      data: example,
      source: "example",
      exampleReason: "The scorecard could not be loaded, so this is the worked example instead.",
    };
  }
  if (live && live.state === "error") {
    return {
      data: example,
      source: "example",
      exampleReason: "The record could not be read, so this is the worked example instead.",
    };
  }
  return {
    data: example,
    source: "example",
    exampleReason:
      "No trade has been closed and scored yet, so this is a worked example showing how the page will read once there is a real record.",
  };
}
