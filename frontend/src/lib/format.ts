export function fmtMoney(v: number | null | undefined): string {
  if (v === null || v === undefined) return "—";
  const sign = v < 0 ? "-" : "";
  return `${sign}$${Math.abs(v).toLocaleString(undefined, {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  })}`;
}

export function fmtMoneyCompact(v: number | null | undefined): string {
  if (v === null || v === undefined) return "—";
  const sign = v < 0 ? "-" : "";
  const abs = Math.abs(v);
  if (abs >= 1000) return `${sign}$${(abs / 1000).toFixed(1)}k`;
  return `${sign}$${abs.toFixed(0)}`;
}

export function fmtPct(v: number | null | undefined): string {
  if (v === null || v === undefined) return "—";
  return `${v >= 0 ? "+" : ""}${v.toFixed(2)}%`;
}

export function fmtNum(v: number | null | undefined, digits = 2): string {
  if (v === null || v === undefined) return "—";
  return Number(v).toLocaleString(undefined, { maximumFractionDigits: digits });
}

// The ONE clock stamp for any "as of / updated / last" label: date AND time,
// so a reader cannot mistake this morning for three days ago. Never call
// the browser time-only formatter (tests/test_owner_timestamps_carry_date.py).
export function fmtClock(d: Date): string {
  return d.toLocaleString(undefined, {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

export function fmtTime(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = new Date(iso.endsWith("Z") || iso.includes("+") ? iso : iso + "Z");
  if (isNaN(d.getTime())) return iso;
  return d.toLocaleString(undefined, {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

// Mirrors src/api/db_reads.py::is_executed_trade exactly — a TradeItem row
// that actually executed some quantity, not just an attempted/HOLD entry.
export function isExecutedTrade(t: { fill_status?: string | null; action?: string | null; fill_qty?: number | null }): boolean {
  const fillQty = t.fill_qty || 0;
  return (t.fill_status == null && t.action !== "HOLD") || t.fill_status === "filled" || fillQty > 0;
}

// QAMC's trading-day boundaries are US/Eastern regardless of where the
// operator's browser sits — journal/{date} buckets sessions by ET calendar
// day (docs/architecture/MISSION_CONTROL_API.md's Stage 5 entry). "Today"
// for that purpose must be computed in ET explicitly, not the browser's
// local timezone, which could disagree with the backend near a midnight
// boundary (an operator in a non-US timezone would otherwise ask for the
// wrong day's journal entirely, not just a display glitch).
export function todayEtDate(): string {
  return new Intl.DateTimeFormat("en-CA", {
    timeZone: "America/New_York",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).format(new Date());
}

export function pnlClass(v: number | null | undefined): string {
  if (v === null || v === undefined) return "";
  return v > 0 ? "text-pos" : v < 0 ? "text-neg" : "";
}

// ET calendar-day key ("YYYY-MM-DD") for a UTC timestamp/Date — same
// convention src/api/db_reads.py's ET-day math uses server-side. Shared by
// the Cockpit's today's-runs timeline (App.tsx) and the price chart's
// "bars run through <date>, today's live price is separate" staleness
// label (PriceChartPanel.tsx) so both derive "today" the same way.
export function etDateKey(input: string | Date): string {
  const d = typeof input === "string" ? new Date(input.endsWith("Z") || input.includes("+") ? input : input + "Z") : input;
  if (isNaN(d.getTime())) return "";
  return new Intl.DateTimeFormat("en-CA", { timeZone: "America/New_York", year: "numeric", month: "2-digit", day: "2-digit" }).format(d);
}
