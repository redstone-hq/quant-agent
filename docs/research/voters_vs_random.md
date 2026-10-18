# Do the AI voters pick better than chance?

Record only. Changes no trade. Script: `scripts/score_voters_vs_random.py`.

## What it measures

For every session since the clean-record start (2026-10-12 13:30 UTC):

- **Eligible list** — every stock the technical analyst rated that session
  (it rates every candidate the desk shows the AI), plus any stock a seat or
  the desk itself touched that day.
- **Voters** — the technical analyst's rating (favoured = buy / strong_buy);
  each decision seat's recorded stance (`seat_technical`, `seat_macro`,
  `seat_earnings`, `seat_news`, `seat_smart_money`; favoured = buy /
  strong_buy / bullish); and `combined_buys`, the stocks actually bought
  (filled BUY trades).
- **Outcome** — close-to-close return from that session's close over fixed
  windows (default 5, 10, 21 sessions). Daily closes come from the desk's own
  Alpaca market-data client, read-only (the database stores no daily bars).
  A window that has not yet matured is reported as "no matured sessions yet",
  never filled in.
- **Random baseline** — 1,000 draws per session of the same number of stocks
  from the same eligible list (`numpy.random.Generator.permuted`, without
  replacement).
- **Per-session percentile** — share of random draws the voter's average
  return beats (ties count half).
- **Pooled percentile and permutation p-value** — the voter's average over
  sessions against the 1,000 pooled random averages;
  `p = (1 + draws at or above the voter) / (1 + 1000)`. The permutation is
  the test; no distributional assumption.
- **Actual exits** — each bought position's return from its buy-day close to
  the desk's real exit fill (or the latest close if still open), listed in the
  JSON for reference.

The seed is a fresh `numpy.random.SeedSequence().entropy`, saved in the JSON;
`--seed <value>` re-runs every draw exactly (PCG64 generator).

## How to run

As the desk user, from the deployed checkout, with `.env` loaded:

```
set -a; . ./.env; set +a
.venv/bin/python scripts/score_voters_vs_random.py \
  --db data/quant_agent.db --out voters_vs_random.json [--windows 5,10,21] [--seed N]
```

## Caveat: a few weeks of data has wide error bars

The standard error of an average excess return is

    SE = sigma / sqrt(n)

where `sigma` is the spread of single-pick excess returns and `n` the number of
independent picks. With daily stock moves of about 2% and 10 picks, SE is about
0.6%, so a 95% interval is about ±1.2% (`±1.96 × SE`) — wider than any edge
the desk could plausibly have. Picks made on the same day share sector and
market moves, so the effective `n` is closer to the number of sessions than to
the number of picks; treat pooled p-values as optimistic. The permutation
p-value itself has Monte Carlo error `sqrt(p(1 - p) / 1000)` (about ±0.007 at
p = 0.05).

The script prints the sample size (sessions, picks) and the p-value beside
every percentile so thin evidence stays visible.

## Known limits

- Seat stances are recorded only for the few stocks the portfolio manager
  considered, so a seat's comparison against the whole eligible list also
  credits the nomination step.
- Bearish stances (rare) are not scored; the desk is long-only in this period.
