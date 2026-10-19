# Target horizon cap (60) and reach multiple (1.5): 19 Oct 2026 measurement

## Method [measured]
- The desk's own level finder, ATR and target function (live settings), plus a 2.5-ATR stop. Level-backed stops are not modelled.
- 316 symbols, entries every 10th session 2015-2025 (Yahoo daily bars), 250-session outcome, first touch, same-bar counts as the stop.
- Halves: A = 2015-19, B = 2020-25. 95% intervals from a 1000-rep bootstrap resampling symbols.
- Recovered entries matched the stored target-hit times 100%.

## Results
Sessions to target (targets hit before the stop, n = 215,645):
- Median 1-2 sessions, 75th percentile 4, 90th percentile 10-12, at every horizon and in both halves.
- Hit after session 20 / 40 / 60 at horizon 60: A 5.6% / 3.0% / 1.7% [1.4-2.1]; B 4.1% / 1.8% / 1.0% [0.9-1.2].
- Hits per 10-session bin fall smoothly (37,906, 2,608, 739, 357, 236, 194, 151 ...), no change at 60.
- The cap never clipped a horizon in the sample (horizons 5-60).
- At horizon 60 the reach test cannot fail: the finder keeps only levels within 1.5 x sqrt(60) = 11.62 ATR (largest seen 11.60), so the cap in practice only sets that window.

Hit rate by distance in measured moves (u = nearest level / (ATR x sqrt(H))), A | B:
- 0.75-0.875: .275 | .256; 0.875-1.125: .178 | .185; 1.125-1.375: .143 | .152; 1.375-1.5: .110 | .078
- 1.5-1.625: .150 | .114; 1.625-1.875: .143 | .097; 1.875-2.5: .105 | .067; 2.5+: .058 | .050
- Change across 1.5: A +0.07 [-0.04, +0.17], B +0.03 [-0.05, +0.12]; neighbouring steps A -0.01, B -0.06. No cliff at 1.5, same in both halves.
- The rule rarely bites: u >= 1.5 in 0.9% of horizon-5 entries, 0.19% at 20, 0% at 60. Bins at u >= 0.75 hold about 4% of entries, so intervals are wide.

## Outcome per number
- Target horizon cap (60): inert for targets. Its one live job (the level-finder window) cannot be tested because no level beyond 11.62 ATR is kept. If kept as an input guard, it is unmeasured.
- Reach multiple (1.5): inert and unmeasured. No value comes out of the data without choosing a quantile, which is an appetite choice. It stays owner-ruled (4 Oct 2026).
- Neither number is fitted to these results; nothing here supports a replacement value. Ledger values unchanged.

## Side finding (pre-existing, out of scope)
- The nearest level the desk targets is a median 0.48 ATR above entry in this backtest (1.56 ATR live) and 83% of backtest targets sit inside the 1-ATR noise floor; 79% are hit, mostly in 1-2 sessions.
- Target selection, not the cap or the reach rule, drives these outcomes. Nothing sells at target live.
- Unknown: the horizons analysts actually state live (no live database read).
