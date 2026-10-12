import { useEffect, useState } from "react";
import { Table, TableBody, TableCell, TableHead, TableHeaderCell, TableRow } from "@tremor/react";
import { api, type TradeReviewResponse, type TradeReviewSlice } from "../../api/client";
import { Panel, StateMessage } from "../ui/Panel";
import { fmtMoney, pnlClass } from "../../lib/format";
import {
  WINDOW_LABELS,
  fmtHold,
  fmtOptR,
  fmtOptUsd,
  fmtP,
  fmtProfitFactor,
  winRateText,
} from "./tradeReviewModel";

function Histogram({ buckets }: { buckets: { label: string; count: number }[] }) {
  const max = Math.max(1, ...buckets.map((b) => b.count));
  return (
    <div className="flex flex-col gap-1" data-testid="r-histogram">
      {buckets.map((b) => (
        <div key={b.label} className="flex items-center gap-2 text-[0.78rem]">
          <span className="w-24 shrink-0 font-mono text-dim">{b.label}</span>
          <div className="h-3 flex-1 rounded bg-panel-alt">
            <div className="h-3 rounded bg-accent" style={{ width: `${(b.count / max) * 100}%` }} />
          </div>
          <span className="w-6 text-right font-mono">{b.count}</span>
        </div>
      ))}
    </div>
  );
}

function SliceTable({ title, rows }: { title: string; rows: TradeReviewSlice[] }) {
  return (
    <div>
      <h4 className="m-0 mb-1 text-[0.72rem] font-semibold uppercase tracking-wide text-dim">{title}</h4>
      {rows.length === 0 ? (
        <p className="m-0 text-dim">No trades.</p>
      ) : (
        <Table className="w-full text-[0.78rem]">
          <TableHead>
            <TableRow className="text-left text-dim">
              <TableHeaderCell>Slice</TableHeaderCell>
              <TableHeaderCell>N</TableHeaderCell>
              <TableHeaderCell>Win rate</TableHeaderCell>
              <TableHeaderCell>Avg R</TableHeaderCell>
              <TableHeaderCell>P&amp;L</TableHeaderCell>
            </TableRow>
          </TableHead>
          <TableBody>
            {rows.map((r) => (
              <TableRow key={r.label}>
                <TableCell>{r.label}</TableCell>
                <TableCell>{r.n}</TableCell>
                <TableCell>{r.win_rate_pct === null ? "—" : `${r.win_rate_pct.toFixed(1)}%`}</TableCell>
                <TableCell>
                  {fmtOptR(r.expectancy_r)}
                  {r.r_unknown ? ` (${r.r_unknown} R unknown)` : ""}
                </TableCell>
                <TableCell className={pnlClass(r.total_pnl_usd)}>{fmtMoney(r.total_pnl_usd)}</TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      )}
    </div>
  );
}

export function TradeReviewView({ data }: { data: TradeReviewResponse }) {
  const w = data.windows!;
  const q = data.quality!;
  const today = data.today!;
  return (
    <div className="flex flex-col gap-3" data-testid="trade-review">
      <p className="m-0 text-[0.78rem] text-dim">
        {data.caveat} Clean record only; {data.excluded_pre_clean_record_n ?? 0} earlier trade(s) set aside, not deleted.
      </p>

      <section>
        <h3 className="m-0 text-sm font-semibold">Today</h3>
        <p className="m-0">
          <span className={pnlClass(today.pnl_usd)}>{fmtOptUsd(today.pnl_usd)}</span> · {today.wins} wins · {today.losses} losses ·
          win rate {today.win_rate_pct === null ? "no trades" : `${today.win_rate_pct.toFixed(1)}% of ${today.n}`}
        </p>
      </section>

      <section className="overflow-x-auto">
        <Table className="w-full text-[0.78rem]">
          <TableHead>
            <TableRow className="text-left text-dim">
              <TableHeaderCell>Window</TableHeaderCell>
              <TableHeaderCell>N</TableHeaderCell>
              <TableHeaderCell>Win rate</TableHeaderCell>
              <TableHeaderCell>Largest win</TableHeaderCell>
              <TableHeaderCell>Avg win</TableHeaderCell>
              <TableHeaderCell>Largest loss</TableHeaderCell>
              <TableHeaderCell>Avg loss</TableHeaderCell>
              <TableHeaderCell>Profit factor</TableHeaderCell>
              <TableHeaderCell>Expectancy</TableHeaderCell>
              <TableHeaderCell>Avg hold</TableHeaderCell>
              <TableHeaderCell>R unknown</TableHeaderCell>
            </TableRow>
          </TableHead>
          <TableBody>
            {WINDOW_LABELS.map(({ key, label }) => {
              const x = w[key];
              return (
                <TableRow key={key}>
                  <TableCell>{label}</TableCell>
                  <TableCell>{x.n}</TableCell>
                  <TableCell>{winRateText(x)}</TableCell>
                  <TableCell>{fmtOptR(x.largest_winner_r)} / {fmtOptUsd(x.largest_winner_usd)}</TableCell>
                  <TableCell>{fmtOptR(x.average_winner_r)} / {fmtOptUsd(x.average_winner_usd)}</TableCell>
                  <TableCell>{fmtOptR(x.largest_loser_r)} / {fmtOptUsd(x.largest_loser_usd)}</TableCell>
                  <TableCell>{fmtOptR(x.average_loser_r)} / {fmtOptUsd(x.average_loser_usd)}</TableCell>
                  <TableCell>{fmtProfitFactor(x.profit_factor)}</TableCell>
                  <TableCell>{fmtOptR(x.expectancy_r)}</TableCell>
                  <TableCell>{fmtHold(x.avg_hold_seconds)}</TableCell>
                  <TableCell>{x.r_unknown}</TableCell>
                </TableRow>
              );
            })}
          </TableBody>
        </Table>
      </section>

      <section>
        <h3 className="m-0 mb-1 text-sm font-semibold">Result in R (N = {data.n_closed})</h3>
        <Histogram buckets={data.r_histogram ?? []} />
      </section>

      <section>
        <h3 className="m-0 mb-1 text-sm font-semibold">Risk</h3>
        <p className="m-0">
          Drawdown {data.drawdown_r?.toFixed(2)}R · longest losing streak {data.longest_losing_streak} · cumulative{" "}
          {fmtOptR(data.cumulative_r?.length ? data.cumulative_r[data.cumulative_r.length - 1].cumulative_r : null)}
        </p>
      </section>

      <section data-testid="quality">
        <h3 className="m-0 mb-1 text-sm font-semibold">Quality (N = {q.n})</h3>
        <p className="m-0">
          Mean R / stdev R: {q.score === null ? "—" : q.score.toFixed(2)} · p-value (mean R above zero): {fmtP(q.p_value)}
        </p>
        <p className="m-0 text-dim">{q.caveat}</p>
      </section>

      <section className="grid grid-cols-1 gap-3 md:grid-cols-3">
        <SliceTable title="By side" rows={data.slices?.by_side ?? []} />
        <SliceTable title="By entry time (ET, 30 min)" rows={data.slices?.by_entry_time_et ?? []} />
        <SliceTable title="By hold length" rows={data.slices?.by_hold ?? []} />
      </section>

      <section>
        <h3 className="m-0 mb-1 text-sm font-semibold">Last {data.last_trades?.length ?? 0} closed trades</h3>
        {(data.last_trades ?? []).length === 0 ? (
          <p className="m-0 text-dim">No closed trades on the clean record yet.</p>
        ) : (
          <Table className="w-full text-[0.78rem]">
            <TableHead>
              <TableRow className="text-left text-dim">
                <TableHeaderCell>Symbol</TableHeaderCell>
                <TableHeaderCell>Side</TableHeaderCell>
                <TableHeaderCell>P&amp;L</TableHeaderCell>
                <TableHeaderCell>R</TableHeaderCell>
                <TableHeaderCell>Hold</TableHeaderCell>
                <TableHeaderCell>Closed</TableHeaderCell>
              </TableRow>
            </TableHead>
            <TableBody>
              {data.last_trades!.map((t) => (
                <TableRow key={t.position_id}>
                  <TableCell className="font-bold">{t.symbol}</TableCell>
                  <TableCell>{t.side}</TableCell>
                  <TableCell className={pnlClass(t.pnl)}>{fmtOptUsd(t.pnl)}</TableCell>
                  <TableCell>{t.r === null ? "R unknown" : fmtOptR(t.r)}</TableCell>
                  <TableCell>{fmtHold(t.hold_seconds)}</TableCell>
                  <TableCell>{t.closed_at}</TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        )}
      </section>
    </div>
  );
}

export function TradeReviewPanel() {
  const [data, setData] = useState<TradeReviewResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    let cancelled = false;
    api
      .tradeReview()
      .then((d) => {
        if (!cancelled) setData(d);
      })
      .catch((e: Error) => {
        if (!cancelled) setError(e.message);
      });
    return () => {
      cancelled = true;
    };
  }, []);
  const failed = error ?? (data?.state === "error" ? data.read_error : null);
  return (
    <Panel
      title="Trade Review"
      subtitle="Closed trades since the clean record began, in R. Display only."
      status={failed ? "error" : data ? "ok" : "loading"}
    >
      {failed && <StateMessage text={`Could not load trade review: ${failed}`} error />}
      {!failed && !data && <StateMessage text="Loading trade review…" />}
      {!failed && data?.state === "ok" && <TradeReviewView data={data} />}
    </Panel>
  );
}
