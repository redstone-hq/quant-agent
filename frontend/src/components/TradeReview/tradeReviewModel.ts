import type { TradeReviewWindow } from "../../api/client";

/** "1h 05m" / "2d 3h" / "—". Display only. */
export function fmtHold(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined || !Number.isFinite(seconds)) return "—";
  if (seconds < 3600) return `${Math.round(seconds / 60)}m`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ${String(Math.round((seconds % 3600) / 60)).padStart(2, "0")}m`;
  return `${Math.floor(seconds / 86400)}d ${Math.round((seconds % 86400) / 3600)}h`;
}

/** Profit factor is a number, "n/a" (no trades) or "no losses" — never infinity. */
export function fmtProfitFactor(v: number | string | null | undefined): string {
  if (v === null || v === undefined) return "n/a";
  return typeof v === "number" ? v.toFixed(2) : v;
}

export function fmtOptR(v: number | null | undefined): string {
  return v === null || v === undefined ? "—" : `${v > 0 ? "+" : ""}${v.toFixed(2)}R`;
}

export function fmtOptUsd(v: number | null | undefined): string {
  if (v === null || v === undefined) return "—";
  return `${v < 0 ? "-" : v > 0 ? "+" : ""}$${Math.abs(v).toFixed(2)}`;
}

export function fmtP(v: number | null | undefined): string {
  return v === null || v === undefined ? "—" : v.toFixed(3);
}

export const WINDOW_LABELS: { key: "today" | "week" | "all"; label: string }[] = [
  { key: "today", label: "Today" },
  { key: "week", label: "This week" },
  { key: "all", label: "All clean record" },
];

export function winRateText(w: Pick<TradeReviewWindow, "n" | "wins" | "win_rate_pct">): string {
  return w.win_rate_pct === null ? "no trades" : `${w.win_rate_pct.toFixed(1)}% (${w.wins} of ${w.n})`;
}
