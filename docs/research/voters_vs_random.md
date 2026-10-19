# Do the AI voters pick better than chance?

Record only. Changes no trade. Script: `scripts/score_voters_vs_random.py`.

## What it measures

For every session since the clean-record start (2026-10-12 13:30 UTC):

- **Each voter's own list** — a voter is compared only with random draws from
  the stocks THAT voter answered about that session, never a union list.
  Technical analyst: every symbol it rated. A decision seat: every symbol it
  recorded a stance on (the list it was shown is not stored, so this is the
  answered list and the output says so). `combined_buys`: graded on the
  technical analyst's rated list that session, because nothing is bought
  without a chart reading (the desk archetype in docs/OUTCOME.md).
- **Voters and directions** — the technical analyst's rating, each decision
  seat's recorded stance (`seat_technical`, `seat_macro`, `seat_earnings`,
  `seat_news`, `seat_smart_money`) and `combined_buys` (filled BUY trades). The
  stance words are the desk's own sets (`_BULLISH_STANCES` / `_BEARISH_STANCES`
  in `src/risk/seat_agreement.py`): buy-side words are a long pick, sell-side
  words (`sell`, `strong_sell`, `bearish`, `underweight`, ...) a short pick
  scored on the negated forward return. Neutral is no pick. Random draws take
  the same number of longs and shorts from the same list.
- **Conviction** — the technical analyst and the seats record `conviction`
  (high / medium / low); results are also split by it. `combined_buys` records
  none, and the output says so.
- **Outcome** — close-to-close return from that session's close over fixed
  windows (default 5, 10, 21 sessions). Daily closes come from the desk's own
  Alpaca market-data client, read-only (the database stores no daily bars).
  A window that has not yet matured is reported as "no matured sessions yet",
  never filled in.
- **Random baseline** — 1,000 draws per session of the same number of stocks
  from the voter's own list (`numpy.random.Generator.permuted`, without
  replacement).
- **Per-session percentile** — share of random draws the voter's average
  return beats (ties count half).
- **Pooled percentile and permutation p-value** — the voter's average over
  sessions against the 1,000 pooled random averages;
  `p = (1 + draws at or above the voter) / (1 + 1000)`. The permutation is
  the test; no distributional assumption.
- **Error range** — the excess over random is reported with a 95% chance
  band: the 2.5th to 97.5th percentile of the 1,000 random pooled means
  (`numpy.percentile`, no extra statistics), minus their mean. An excess inside
  the band is indistinguishable from luck. The percentile method was chosen
  over `scipy.stats.bootstrap` because scipy is not a project dependency.
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

- Seat stances are recorded only for the stocks the portfolio manager considered, and a seat list is its answered list, not its shown list, so a seat result still credits the nomination step.
- Read no seat weight from this until the chance band excludes zero over many sessions.
