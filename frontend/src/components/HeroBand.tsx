import {
  Badge,
  Card,
  Grid,
  Metric,
  ProgressBar,
  SparkAreaChart,
  Text,
  Title,
} from "@tremor/react";
import { AccountResponse, MacroBroaderContext, PositionItem } from "../api/client";
import { fmtClock, fmtMoney, fmtMoneyCompact, fmtPct, pnlClass } from "../lib/format";
import { marginInterestCompactLabel } from "./MarginInterestPanel";
import { LevelBar } from "./ui/Meter";
import { Pill } from "./ui/Pill";

function equityHistorySeries(account: AccountResponse | null): { date: string; equity: number }[] {
  if (!account?.history?.length) return [];
  return [...account.history]
    .reverse()
    .map((p) => ({ date: p.date, equity: p.equity_close ?? p.total_value ?? 0 }))
    .filter((p) => p.equity > 0);
}

/** "as of HH:MM" for a regime reading's age — naive timestamps (no
 * trailing Z/offset) are UTC, same convention lib/format.ts::fmtTime
 * already uses everywhere else a run timestamp is displayed. */
function regimeAge(asOf: string | null): string | null {
  if (!asOf) return null;
  const d = new Date(asOf.endsWith("Z") || asOf.includes("+") ? asOf : `${asOf}Z`);
  if (isNaN(d.getTime())) return null;
  return `as of ${fmtClock(d)}`;
}

/* Item 7 (cockpit trader rework): "Market Regime" used to be a permanent
 * empty state ("Awaiting macro evidence") whenever funnel-of-the-moment
 * carried no macro context — including the common case of a midday/close
 * position-review run with a perfectly good morning regime reading a few
 * hours old. App.tsx's `latestRegime` now looks across all of today's
 * already-fetched runs for the most recent real regime and passes it
 * here, with the reading's own age — so this shows the last known regime
 * WITH its age, and renders nothing at all (reclaiming the space rather
 * than reserving a placeholder) only on a day with truly no regime
 * evidence yet. */
function RegimeCard({ regime }: { regime: { macro: MacroBroaderContext; asOf: string | null } | null }) {
  if (!regime?.macro.regime) return null;
  const macro = regime.macro;
  const age = regimeAge(regime.asOf);

  const outlookTone =
    macro.equity_outlook === "bullish" ? "text-pos" : macro.equity_outlook === "bearish" ? "text-neg" : "text-dim";
  const confidenceTone = macro.confidence === "high" ? "pos" : macro.confidence === "medium" ? "warn" : "dim";

  return (
    <Card decoration="top" decorationColor="violet" className="!bg-panel !p-3.5 !ring-border h-full">
      <div className="flex items-start justify-between gap-2">
        <Text className="uppercase tracking-wide">Market regime</Text>
        {age && <span className="text-[length:var(--fs-micro)] text-dim whitespace-nowrap">{age}</span>}
      </div>
      <div className="mt-2 flex items-center gap-2 flex-wrap">
        <Pill text={macro.regime} />
        <span className={`text-sm font-bold tracking-wide ${outlookTone}`}>
          {(macro.equity_outlook || "unknown").toUpperCase()}
        </span>
      </div>
      {macro.confidence && (
        <div className="mt-3">
          <div className="mb-1 flex items-center justify-between text-xs text-dim">
            <span>Confidence</span>
            <Badge color={macro.confidence === "high" ? "emerald" : macro.confidence === "medium" ? "amber" : "slate"} size="xs">
              {macro.confidence}
            </Badge>
          </div>
          <LevelBar level={macro.confidence} tone={confidenceTone} />
        </div>
      )}
      {macro.summary && <Text className="mt-3 leading-snug line-clamp-2">{macro.summary}</Text>}
    </Card>
  );
}

/* Portfolio-exposure card — net exposure gauge plus the Long/Hedge/Short/
 * Liquidity breakdown. Extracted so the page layout (variant="page", iPad
 * header) and the redesigned Account dockview panel (variant="panel")
 * render exactly the same card from the same server-computed numbers, with
 * no second copy to drift. All figures come in already computed by
 * HeroBand — nothing is re-derived here (the defect that made this gauge
 * and its own ceiling disagree by 13.6 points came from re-derivation). */
function ExposureCard({
  deployedLabel,
  riskDeployedPct,
  maxTotalPct,
  longMv,
  hedgeMv,
  shortMv,
  cashMv,
}: {
  deployedLabel: string;
  riskDeployedPct: number | null;
  maxTotalPct: number | null;
  longMv: number;
  hedgeMv: number;
  shortMv: number;
  cashMv: number;
}) {
  return (
    <Card decoration="top" decorationColor="cyan" className="!bg-panel !p-3.5 !ring-border h-full">
      <div className="flex items-start justify-between gap-3">
        <div>
          <Text className="uppercase tracking-wide">Portfolio exposure</Text>
          <Title
            className="mt-1 font-mono !text-2xl text-ink"
            title="Net exposure: holdings after hedges are netted off, leveraged funds at their true multiple. Same measure as the ceiling beside it."
          >
            {deployedLabel} net exposure
          </Title>
        </div>
        {maxTotalPct !== null && <Badge color="slate">ceiling {maxTotalPct.toFixed(0)}%</Badge>}
      </div>
      <ProgressBar value={riskDeployedPct ?? 0} color="cyan" className="mt-3" />
      <Grid numItems={shortMv !== 0 ? 4 : 3} className="mt-3 gap-2">
        <div><Text className="text-xs uppercase">Long</Text><Metric className="font-mono text-base text-pos">{fmtMoneyCompact(longMv)}</Metric></div>
        <div><Text className="text-xs uppercase">Hedge</Text><Metric className="font-mono text-base text-hedge">{fmtMoneyCompact(hedgeMv)}</Metric></div>
        {shortMv !== 0 && (
          <div><Text className="text-xs uppercase">Short</Text><Metric className="font-mono text-base text-neg">{fmtMoneyCompact(shortMv)}</Metric></div>
        )}
        <div><Text className="text-xs uppercase">Liquidity</Text><Metric className="font-mono text-base text-ink">{fmtMoneyCompact(cashMv)}</Metric></div>
      </Grid>
    </Card>
  );
}

/* One tile in the panel's PRIMARY STAT ROW — the full-width, equally
 * weighted row of headline glances (NLV, day P&L, total P&L, interest/day)
 * that replaced the old three-full-width-rows-hugging-the-left layout
 * (owner 2026-09-23: "weirdly horizontally dead spaced ... hard to find,
 * the data I wanna see"). The row itself uses an auto-fit grid so the
 * tiles fill the panel's real width and reflow as it is resized, rather
 * than leaving a dead band on the right. */
function StatTile({
  label,
  value,
  valueClass = "text-ink",
  sub,
  badge,
  tooltip,
}: {
  label: string;
  value: string;
  valueClass?: string;
  sub?: React.ReactNode;
  badge?: React.ReactNode;
  tooltip?: string;
}) {
  return (
    <div
      className="flex min-w-0 flex-col rounded-lg border border-border bg-panel-alt px-3 py-2"
      title={tooltip}
    >
      <div className="flex items-center gap-1.5">
        <span className="label-xs truncate">{label}</span>
        {badge}
      </div>
      <div className={`mt-0.5 font-mono text-[length:var(--fs-stat)] font-semibold leading-tight tabular-nums ${valueClass}`}>
        {value}
      </div>
      {sub != null && <div className="mt-0.5 text-[length:var(--fs-meta)] text-dim tabular-nums">{sub}</div>}
    </div>
  );
}

export function HeroBand({
  account,
  accountError,
  positions,
  regime,
  collapsed = false,
  variant = "page",
}: {
  account: AccountResponse | null;
  accountError: string | null;
  positions: PositionItem[];
  /** Last known regime reading across today's runs, with its age — see
   * App.tsx's `latestRegime`. Null on a day with no regime evidence yet. */
  regime: { macro: MacroBroaderContext; asOf: string | null } | null;
  /* Item 6 (cockpit trader rework): on iPad this is still compact header
   * chrome (Holdings is a header strip there). On desktop the same facts
   * live in the Account Dockview panel — collapsed is unused there; the
   * panel itself is what the operator resizes. */
  collapsed?: boolean;
  variant?: "page" | "panel";
}) {
  const isPanel = variant === "panel";
  if (!account) {
    return (
      <Card className={`${isPanel ? "" : "mx-3 mt-1 "}!w-auto !bg-panel !p-2 !ring-border text-center`}>
        <Text>{accountError ? `Account unavailable: ${accountError}` : "Loading account…"}</Text>
      </Card>
    );
  }

  const unrealized = positions
    .filter((p) => !p.is_cash_equivalent)
    .reduce((sum, p) => sum + (p.unrealized_pnl || 0), 0);
  const liquidity = account.liquidity;
  const total = account.portfolio_value || 0;
  // longMv / hedgeMv / shortMv are DISPLAY sums only — a breakdown of where
  // the money sits, using `direction` for exactly the labeling it is
  // documented for. `shortMv` is its own bucket (docs/WORK.md item 176):
  // before that fix every short position was labeled "long" here too, so
  // its negative market_value was counted inside `longMv`, silently
  // UNDERSTATING the Long tile by the short's magnitude (a $268 short read
  // as if it made Long $268 smaller) with no Short tile to show it in.
  // `cashMv`'s residual (total minus everything identified) already came
  // out to the SAME number either way — algebraically, subtracting an
  // unlabeled short from `total - longMv - hedgeMv` and subtracting a
  // labeled short via a dedicated `- shortMv` term cancel out identically
  // — so this fix does not change the Liquidity figure, only which tile a
  // short's value is honestly shown in.
  const longMv = positions.filter((p) => p.direction === "long").reduce((sum, p) => sum + (p.market_value || 0), 0);
  const hedgeMv = positions
    .filter((p) => p.direction === "bearish_hedge")
    .reduce((sum, p) => sum + (p.market_value || 0), 0);
  const shortMv = positions
    .filter((p) => p.direction === "short")
    .reduce((sum, p) => sum + (p.market_value || 0), 0);
  const cashMv = Math.max(total - longMv - hedgeMv - shortMv, 0);
  // "% deployed" comes from the SERVER, computed by the same function the
  // risk engine's max_total_position_pct rule uses. This used to be
  // (longMv + hedgeMv) / total — hedges ADDED instead of netted, leverage
  // ignored — and was then drawn against the engine's ceiling fetched from
  // /account, so the bar and its fill came from different definitions and
  // read 13.6 percentage points apart. Null means "not measurable", which
  // is rendered as such rather than as a confident 0%.
  const riskDeployedPct = account.exposure?.net_exposure_pct ?? null;
  const deployedLabel = riskDeployedPct === null ? "—" : `${riskDeployedPct.toFixed(0)}%`;
  const maxTotalPct = account.risk_limits?.max_total_position_pct ?? null;
  const history = equityHistorySeries(account);

  // Total P&L since the board's own tracked start (server-computed,
  // src/api/routes_live.py — see AccountResponse.total_pnl) and margin
  // interest (src/margin_interest.py via account.margin_interest) — the
  // two owner-requested top-left figures that were missing entirely
  // (owner, 2026-09-23): day P&L and unrealized already showed above.
  // `null` renders as "—" in both spots rather than a fabricated number.
  const totalPnlLabel = account.total_pnl === null
    ? "Total P&L: —"
    : `Total ${fmtMoney(account.total_pnl)} (${fmtPct(account.total_pnl_pct)})${account.total_pnl_since ? ` since ${account.total_pnl_since}` : ""}`;
  const marginInterest = marginInterestCompactLabel(account.margin_interest);

  if (collapsed) {
    return (
      <div className={`${isPanel ? "" : "mx-3 mt-1 "}flex min-w-0 flex-wrap items-center gap-x-3 gap-y-0.5 overflow-x-hidden rounded-lg border border-border bg-panel px-3 py-1`}>
        <span className="flex items-baseline gap-2">
          <span className="label-xs">NLV</span>
          <span className="font-mono text-[length:var(--fs-stat)] font-semibold tabular-nums text-ink">
            {fmtMoney(account.portfolio_value)}
          </span>
        </span>
        {/* Item 11: the equity curve now lives here too, not only behind
            "Show full header" — this collapsed line is what a trader sees
            by default, so "+$X today and nothing else" needed fixing in
            the state that's actually on screen most of the time. */}
        {history.length > 1 && (
          <SparkAreaChart
            data={history}
            index="date"
            categories={["equity"]}
            colors={["cyan"]}
            className="h-5 w-16"
            showGradient
          />
        )}
        <span className={`font-mono text-[length:var(--fs-body)] font-semibold tabular-nums ${pnlClass(account.daily_pnl)}`}>
          {fmtMoney(account.daily_pnl)} ({fmtPct(account.daily_pnl_pct)}) today
        </span>
        <span
          className={`font-mono text-[length:var(--fs-meta)] tabular-nums ${pnlClass(account.total_pnl)}`}
          title={account.total_pnl_since ? `Since the clean record: ${account.total_pnl_since}` : undefined}
        >
          {totalPnlLabel}
        </span>
        <span className="text-[length:var(--fs-meta)] text-dim" title={marginInterest.note}>
          {marginInterest.text}
        </span>
        {liquidity && (
          <span className="text-[length:var(--fs-meta)] text-dim" title="Cash plus the parked sweep vehicle — the figure sizing uses">
            <span className="label-xs">Deployable</span>{" "}
            <span className="font-mono font-semibold tabular-nums text-ink">{fmtMoneyCompact(liquidity.deployable_cash)}</span>
          </span>
        )}
        <span className="text-[length:var(--fs-meta)] text-dim">
          {deployedLabel} net exposure
          {maxTotalPct !== null ? ` / ${maxTotalPct.toFixed(0)}% ceiling` : ""}
        </span>
        {regime?.macro.regime && (
          <span className="flex items-center gap-1.5">
            <Pill text={regime.macro.regime} />
            <span
              className={`text-[length:var(--fs-meta)] font-bold tracking-wide ${
                regime.macro.equity_outlook === "bullish" ? "text-pos" : regime.macro.equity_outlook === "bearish" ? "text-neg" : "text-dim"
              }`}
            >
              {(regime.macro.equity_outlook || "unknown").toUpperCase()}
            </span>
            {regimeAge(regime.asOf) && <span className="text-[length:var(--fs-micro)] text-dim">{regimeAge(regime.asOf)}</span>}
          </span>
        )}
        {accountError && (
          <Badge color="amber" size="xs" className="ml-auto">
            stale
          </Badge>
        )}
      </div>
    );
  }

  /* ==== Account Dockview panel (desktop) — redesigned 2026-09-23 ====
   *
   * Owner, live, verbatim: "Can you redesign that panel? It's just weirdly
   * horizontally dead spaced ... it's just hard to read and hard to find,
   * the data I wanna see." The three sub-sections used to stack as
   * full-width rows whose content hugged the left third — a tall panel
   * with a dead band down the right, and the margin-interest figure buried
   * at the very bottom where he could not find it.
   *
   * The fix is two grids that USE THE FULL WIDTH:
   *   1. PRIMARY STAT ROW — NLV, day P&L, total P&L since inception, and
   *      interest/day as four equally weighted tiles in an auto-fit grid.
   *      This one row answers both complaints: it fills the horizontal
   *      space, and it lifts interest/day from the bottom to a headline
   *      glance beside the P&L figures the owner named.
   *   2. SECONDARY GRID — the exposure gauge and the regime card side by
   *      side (auto-fit), compact, below the headline.
   *
   * Every honest datum and caveat is preserved: the ESTIMATE badge on
   * interest stays VISIBLE (not tooltip-only), the real "$0.00/day" zero
   * state and the "not available" degraded state both render as text, and
   * total P&L still degrades to "—" rather than fabricating a number. The
   * borrowed/annual/rate ESTIMATE detail continues below in
   * MarginInterestStrip; this row is the glance, that strip is the detail.
   */
  if (isPanel) {
    const mi = account.margin_interest;
    const cumulative = mi && !mi.error ? mi.cumulative : null;
    // Interest tile — owner ask 2026-09-24: replace the per-day figure
    // with THIS WEEK's cumulative running total, mirroring
    // MarginInterestStrip's own rules so the two never disagree. A
    // null/errored estimate or missing cumulative degrades to "not
    // available" (never a fabricated $0.00); "no data yet" is its own
    // honest state, not a zero; a genuinely nonzero, unconfirmed total
    // carries a small "est." badge — never a paragraph.
    const interestUnavailable = !mi || mi.error || !cumulative;
    let interestValue: string;
    let interestBadge: React.ReactNode = null;
    let interestSub: React.ReactNode = null;
    if (interestUnavailable) {
      interestValue = "not available";
    } else if (cumulative!.source === "no_data") {
      interestValue = "no data yet";
      interestSub = "Tracking starts today";
    } else {
      interestValue = fmtMoney(cumulative!.this_week_usd);
      if (cumulative!.is_estimate && cumulative!.this_week_usd !== 0) {
        interestBadge = (
          <Badge color="amber" size="xs">
            est.
          </Badge>
        );
      }
      interestSub = `${cumulative!.current_month_label}: ${fmtMoney(cumulative!.current_month_usd)}`;
    }

    return (
      <div className="min-w-0 overflow-x-hidden">
        <div
          className="grid gap-2 [grid-template-columns:repeat(auto-fit,minmax(9rem,1fr))]"
          aria-label="Account headline"
        >
          <StatTile
            label="Net liquidation value"
            value={fmtMoney(account.portfolio_value)}
            badge={accountError ? <Badge color="amber" size="xs">stale</Badge> : undefined}
            tooltip="Total account equity — cash plus the market value of every position."
          />
          <StatTile
            label="P&L today"
            value={fmtMoney(account.daily_pnl)}
            valueClass={pnlClass(account.daily_pnl)}
            sub={<span className={pnlClass(account.daily_pnl_pct)}>{fmtPct(account.daily_pnl_pct)}</span>}
            tooltip={`${fmtMoney(unrealized)} unrealized`}
          />
          <StatTile
            label="Total P&L"
            value={account.total_pnl === null ? "—" : fmtMoney(account.total_pnl)}
            valueClass={pnlClass(account.total_pnl)}
            sub={
              account.total_pnl === null
                ? undefined
                : `${fmtPct(account.total_pnl_pct)}${account.total_pnl_since ? ` since ${account.total_pnl_since}` : ""}`
            }
            tooltip={account.total_pnl_since ? `Since the clean record: ${account.total_pnl_since}` : undefined}
          />
          <StatTile
            label="Interest this week"
            value={interestValue}
            valueClass={interestUnavailable ? "text-dim" : "text-ink"}
            badge={interestBadge}
            sub={interestSub}
            tooltip={marginInterest.note}
          />
        </div>

        {history.length > 1 && (
          <SparkAreaChart
            data={history}
            index="date"
            categories={["equity"]}
            colors={["cyan"]}
            className="mt-2 h-8"
            showGradient
          />
        )}

        <div className="mt-2 grid gap-2 [grid-template-columns:repeat(auto-fit,minmax(15rem,1fr))]">
          <ExposureCard
            deployedLabel={deployedLabel}
            riskDeployedPct={riskDeployedPct}
            maxTotalPct={maxTotalPct}
            longMv={longMv}
            hedgeMv={hedgeMv}
            shortMv={shortMv}
            cashMv={cashMv}
          />
          <RegimeCard regime={regime} />
        </div>
      </div>
    );
  }

  return (
    /* Vertical-space reallocation pass, 2026-09-11: outer mt-3 -> mt-2,
       same low-risk inter-section trim as the other stacked chrome
       sections (HoldingsStrip/LiquidityPanel/TodaySessionsStrip/
       DecisionStateBanner) — internal mt-3 spacing inside this component
       is untouched. */
    <div className={`mx-3 mt-2 grid grid-cols-1 gap-3 ${regime?.macro.regime ? "lg:grid-cols-[1.2fr_1fr_1fr]" : "lg:grid-cols-[1.2fr_1fr]"}`}>
      <Card
        decoration="top"
        decorationColor={accountError ? "amber" : "cyan"}
        className="!bg-panel !p-3.5 !ring-border h-full"
      >
        <div className="flex items-start justify-between gap-3">
          <div>
            <Text className="uppercase tracking-wide">Net liquidation value</Text>
            <Metric className="mt-1 font-mono !text-2xl tabular-nums text-ink">{fmtMoney(account.portfolio_value)}</Metric>
          </div>
          {accountError && <Badge color="amber">stale</Badge>}
        </div>
        <div className="mt-1 flex flex-wrap items-center gap-x-3 text-sm">
          <span className={`font-mono font-semibold tabular-nums ${pnlClass(account.daily_pnl)}`}>
            {fmtMoney(account.daily_pnl)} ({fmtPct(account.daily_pnl_pct)}) today
          </span>
          <span className={`font-mono tabular-nums ${pnlClass(unrealized)}`}>{fmtMoney(unrealized)} unrealized</span>
        </div>
        <div className="mt-1 flex flex-wrap items-center gap-x-3 text-xs">
          <span
            className={`font-mono font-semibold tabular-nums ${pnlClass(account.total_pnl)}`}
            title={account.total_pnl_since ? `Since the clean record: ${account.total_pnl_since}` : undefined}
          >
            {totalPnlLabel}
          </span>
          <span className="text-dim" title={marginInterest.note}>
            {marginInterest.text}
          </span>
        </div>
        {history.length > 1 && (
          <SparkAreaChart
            data={history}
            index="date"
            categories={["equity"]}
            colors={["cyan"]}
            className="mt-2 h-8"
            showGradient
          />
        )}
        <div className="mt-2 flex flex-wrap gap-x-4 gap-y-1 text-xs text-dim">
          <span>
            Deployable{" "}
            <strong
              className="font-mono text-ink"
              title="Cash plus the sweep vehicle the engine liquidates on demand — the figure position sizing actually uses."
            >
              {fmtMoneyCompact(liquidity?.deployable_cash)}
            </strong>
          </span>
          <span>
            Sweep parked{" "}
            <strong className="font-mono text-ink">
              {liquidity?.sweep_enabled
                ? `${fmtMoneyCompact(liquidity.sweep_parked_value)} ${liquidity.sweep_symbol || ""}`
                : "disabled"}
            </strong>
          </span>
        </div>
      </Card>

      <ExposureCard
        deployedLabel={deployedLabel}
        riskDeployedPct={riskDeployedPct}
        maxTotalPct={maxTotalPct}
        longMv={longMv}
        hedgeMv={hedgeMv}
        shortMv={shortMv}
        cashMv={cashMv}
      />

      <RegimeCard regime={regime} />
    </div>
  );
}
