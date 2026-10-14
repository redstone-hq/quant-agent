# Research Findings — What Actually Works

**Date:** 2026-08-27
**Purpose:** establish which signals have evidence behind them before building on them, rather than reinventing techniques the literature has already tested and, in several cases, already falsified.

> Read the caveats. Two of the most attractive ideas here are substantially
> weaker than their headline numbers suggest, and one widely-cited result
> appears to be an artefact of the model already knowing the answer.

---

## 1. Insider transactions (Form 4)

### What predicts returns

**Routine versus opportunistic is the whole ballgame.** Cohen, Malloy & Pomorski, *Decoding Inside Information* (Journal of Finance, 2012): more than half of insider trades are "routine" — the same insider trading in the same calendar month for three or more consecutive years — and these carry **zero predictive power**. Removing them leaves opportunistic trades generating roughly **82bps/month** value-weighted abnormal return.
<https://papers.ssrn.com/sol3/papers.cfm?abstract_id=1692517>

**Cluster buying replicates well.** Alldredge & Blank (Journal of Financial Research 42(2), 2019; SSRN 2781761). What the abstract states (the SSRN and Wiley pages returned HTTP 403 on 2026-09-19, so this is the abstract as quoted in search results, not the paper): about 23% of insider purchases occur on the **same day** as another insider purchase at the same company, and clustered purchases are followed by abnormal returns **in excess of 2% over the subsequent month**. CORRECTED 2026-09-19: this line previously said "within ~2 days", "~2.1%/month" and "about 0.9pp above solitary buys". Those three figures are not in the abstract; they appear in a secondary summary (IBKR Campus, *What Corporate Insider Buying Can Tell Investors*) and could not be checked against the paper. The desk's research-defined cluster is therefore the abstract's same-day measure (`src/data/smart_money_cluster.py::insider_purchase_clusters`, board item 124). Kang, Kim & Wang: 3.8% versus 2.0% over 21 trading days, widening at 90 days (not re-checked 2026-09-19).
<https://onlinelibrary.wiley.com/doi/10.1111/jfir.12172>

**Role matters, and not as expected.** CFO purchases outperform CEO purchases — CFOs see the numbers first. Trades by "star" CEOs carry no signal.

**Size relative to holdings, not absolute size.** Scott & Xu, *Some Insider Sales Are Positive Signals* (Financial Analysts Journal 60(3), 2004): 512,133 combined transactions, 80,742 company-quarters, 1987–2002, sorted by "shares traded as a percentage of shares owned" into bands of under 10%, 10–50% and over 50%. Size- and B/P-adjusted quarterly excess returns — sales over 100,000 shares: −0.06%, +0.08%, **−0.81%** (only the over-50% band is significant); sales under 100,000 shares: **+0.68%**, **+0.44%**, +0.06% — so a proportionally small sale is a mildly *positive* signal, not merely a neutral one. Purchases scale the same way: +0.38%, **+1.06%**, **+1.42%**; initial purchases, where no prior holding exists, earn an insignificant +0.10%. This is measurement, not assertion. Their ratio is a *net, per-stock-quarter* one over a six-month formation window, so its band returns do not transfer to a single-filing admission gate.
<https://rpc.cfainstitute.org/research/financial-analysts-journal/2004/some-insider-sales-are-positive-signals>

QAMC parses `sharesOwnedFollowingTransaction` from every Form 4 it downloads, so the ratio needs no new data source (verified 2026-09-13 against a live EDGAR daily index: 77 of 77 open-market P/S rows carried the field). It is computed for buys and sells, reported on every observation, and **gates nothing.** Note carefully what the paper does and does not license as a cutoff. The only significance boundary its prose marks is 50%, not 10%: *"The group of stocks with net total sales exceeding 100,000 shares had an average excess return of −0.55 percent, but of that group, those stocks for which shares sold accounted for more than half of shares owned had average excess return of −1.17 percent. Excess returns on stocks with the same level of shares sold but a lower percentage of holdings were negative but statistically insignificant."* And the low band is not noise but signal with the opposite sign: *"Small sales that represented small percentages of shares owned not only did not predict poor performance but were associated with significantly positive abnormal returns."* A classifier label meaning "no predictive power" is therefore the wrong home for it, and this desk removed the cutoff on 2026-09-13 rather than move it.

**Decay.** Roughly 25% of the abnormal return accrues within five days, ~50% within a month, with some persistence to six months.

**Concentration.** The effect is strongest in small and micro caps, where analyst coverage is thin and price discovery is slower.

### The caveat that matters most

**Ozlen & Batumoglu, *The Death of Insider Trading Alpha* (December 2025)** find that **70–80% of the price move occurs between the transaction date and the filing becoming public.** Form 4s may lag two business days. A system acting on filings is collecting the tail.
<https://papers.ssrn.com/sol3/papers.cfm?abstract_id=5966834>

Also relevant: **10b5-1 plans are not a clean noise filter.** For high-value sales, planned and discretionary transactions show similar opportunism, and the 2022 SEC reform did not reduce abnormal returns on insider selling.

**Conclusion for QAMC:** treat insider flow as a **tilt, not a primary alpha engine**. `src/data/smart_money.py` already records `lag_days`, so this can be measured on our own data rather than taken on faith.

### Transaction codes

| Keep | Exclude |
|---|---|
| **P** — open-market purchase (highest signal) | **A** — grants and awards |
| **S** — open-market sale | **M** — option exercise (no new cash risked) |
| | **F** — shares withheld for tax (automatic) |
| | **G** — bona-fide gift (no directional signal) |
| | **D**, **X** — dispositions to issuer, expirations |

QAMC already filters to P/S and to non-derivative rows (`src/data/smart_money.py:390-398`). That part is correct.

### What to implement, ranked

1. **Routine versus opportunistic classification** — same insider, same calendar month, three consecutive years. Drop routine entirely. Pure Python; highest value per line of code in the whole system.
   **Built and finished** on branch `feat/insider-signal-filter` (`f3aeba4`
   + `866e423`, `src/data/insider_signal.py`, plus a 2026-08-28 finishing
   pass that moved every threshold into `SmartMoneyConfig`; PR opened
   against `main`, not yet merged or deployed — see `docs/WORK.md` "Landed"
   and `docs/STATE.md`). The 10b5-1 caveat below was followed rather than
   the common folk rule: the flag never marks a large sale routine on its
   own, only ever supports a routine label for a sale that is already
   proportionally small. Re-measured on the live cache 2026-08-28: 57.3% of
   open-market P/S rows routine (2,742 rows), consistent with the original
   56.2%-of-2,188 measurement a day earlier, but the calendar-month test
   again contributed zero of those matches — the multi-year history it
   needs still does not exist in production, so the measured split remains
   driven entirely by the proportional-sale rules, not the strongest,
   best-evidenced rule. Only 1 of 413 buy-side rows was routine at all
   (a $0-price transaction), which is why re-running `fetch()` before/after
   on the real cache showed zero symbols changing admission status today
   even though 94 symbols' entire dollar volume is now correctly
   down-weighted to $0 in the analyst's ranking sum — see `docs/WORK.md`
   "Landed" for the full before/after.
2. **Cluster confirmation** — shipped 2026-09-19 (board item 124) as a deterministic fact and a medium-to-high conviction lift only. Ranking clusters above solitary buys was deliberately NOT done: a cluster must not raise the sort priority of, or admit, any symbol; admission belongs to the universe screen.
3. **Purchase as a percentage of the insider's existing holdings** — the raw fields are already captured, the ratio simply is not computed.
4. **Role weighting** — upweight CFO and non-celebrity officers.
5. **Small/mid-cap tilt.**
6. **Measure our own filing lag** before trusting any of the above.

---

## 2. News

### The headline result does not survive scrutiny

Lopez-Lira & Tang, *Can ChatGPT Forecast Stock Price Movements?* (2023) reported that LLM headline sentiment predicted next-day returns (~0.21% per sentiment unit, concentrated in small caps).
<https://arxiv.org/abs/2304.07619>

A 2025 replication, *Detecting Lookahead Bias in LLM Forecasts*, re-ran the test in a strict post-training-cutoff window and found the effect **largely explained by the model having memorised what happened.** Contamination, not forecasting skill.
<https://arxiv.org/pdf/2512.23847>

### What does survive: novelty

Across the more careful literature, **novelty predicts returns better than sentiment or polarity.** Recycled commentary carries little signal however strongly worded; a genuinely new fact moves prices. *Buy the Rumor, Sell the News: When Is News Priced In?* studies absorption rates across 1.68M events.
<https://arxiv.org/html/2608.14014>

This is a direct instruction for QAMC: score **"is this new?"** rather than **"is this positive?"** — and novelty is an embedding comparison against a rolling buffer, not a model's judgment.

### The volume problem: use a cascade

Reading every article is infeasible and unnecessary. The established pattern:

1. **Cheap deterministic stage** — entity-tag, embed, deduplicate and novelty-score against a rolling 48–72 hour per-ticker buffer. Near-duplicates (wire reprints, the same story across outlets) are merged or dropped before any model sees them.
2. **Expensive stage on the residual only** — typically a handful of genuinely novel, entity-relevant items per day. These justify fetching the full article and using a real model to extract **structured facts** (event type, direction, magnitude) rather than a sentiment scalar.

This is likely **cheaper than the current design**, which spends attention on ~50 truncated blurbs, most of them duplicates or irrelevant.

---

## 3. Macro — the LLM is not earning its seat

For regime classification from yield curve, VIX and credit spreads, the evidence does **not** support LLMs adding value over rules. *When Valid Signals Fail* found numeric macro features dominated, with LLM-only signals underperforming in high-volatility regimes — precisely when the classification matters most.
<https://arxiv.org/pdf/2604.10996>

**Where LLMs do have published support: Federal Reserve communication tone.** Hawkish/dovish scoring of FOMC statements and minutes correlates with fed-funds-futures moves and sometimes carries unpriced residual information.
<https://www.kansascityfed.org/documents/5642/rwp20-14dohsongyang.pdf>

**Recommendation:** make regime classification deterministic Python; point the model at FOMC text specifically. Narrower, better-evidenced, and cheaper than the current arrangement.

---

## 4. Earnings — a documented signal already sitting in the pipeline

**Cohen, Malloy & Nguyen, *Lazy Prices*** (NBER): year-over-year **changes in 10-K language** — particularly risk factors, litigation and executive-team sections — predict returns at roughly **188bps/month gross (~22%/year)**. Notably there is *no* announcement-day effect: the market is inattentive and the return accrues slowly, which suits a swing horizon.
<https://www.nber.org/system/files/working_papers/w25084/w25084.pdf>

**This requires no LLM at all.** It is a text similarity diff — cosine or Jaccard on filing sections.

**QAMC already downloads and stores full 10-K/10-Q text** (`src/data/earnings.py`). The raw material for one of the better-documented anomalies in the literature is already on disk and unused.

Where an LLM does beat dictionary methods: **hedging and evasiveness detection in earnings-call Q&A** (not prepared remarks). QAMC has no transcript source today.

---

## 5. Failure modes to design against

**Training-data contamination is the dominant risk in this field.** The model has seen both the original news and the retrospective commentary explaining why the stock moved. Any backtest of an LLM signal inside its training window is close to meaningless.

**Consequence for the backtester (spec Phase 7):** it must enforce strict post-training-cutoff evaluation windows and record which model and cutoff each test used. Without that, it will produce beautiful results that mean nothing — which is worse than having no backtester, because it manufactures false confidence.

Also: **anomaly decay after publication** (McLean & Pontiff, 2016) — published edges shrink as capital chases them. And **backtest overfitting**, compounded by the many prompt and threshold degrees of freedom an LLM pipeline offers.

---

## 6. Candidate data sources

| Need | Options |
|---|---|
| Earnings calendar | API Ninjas (free tier), Financial Modeling Prep (free tier), EODHD (cheap) |
| Analyst estimates and revisions | EODHD Calendar API — consensus history at 7/30/60/90-day lookbacks plus revision counts |
| Call transcripts | API Ninjas (free key, back to 2005), Alpha Vantage, Finnhub (premium) |
| News beyond RSS | GDELT (free, unlimited, research-grade — needs real engineering), Polygon (per-ticker sentiment on free tier), NewsData.io, Mediastack |

---

## 7. Support and resistance — does a level actually hold?

**Measured on our own data 2026-09-02.** `src/data/levels.py` finds where price
repeatedly stopped, and every stop and target now traces to it. What it never
did was say whether a given level is *likely to hold*: its `strength` field is a
recency-weighted touch count on a scale nobody calibrated. `src/data/level_quality.py`
measures that, and `scripts/level_quality_report.py` reproduces every number below.
Nothing is wired into sizing, stops or targets — a test (`TestNotWiredIntoTrading`)
fails if any module under `src/` imports it.

### What the literature says

Garzarelli, Cristelli, Pompa, Zaccaria & Pietronero, *Memory effects in stock price
dynamics* (Scientific Reports 4:4487, 2014), LSE tick data: the probability price
bounces off a level RISES with the number of times it has already bounced there,
and the effect disappears on shuffled surrogates.
<https://www.nature.com/articles/srep04487>

Chung & Bellotti (arXiv:2101.07410, 2021) replicate the touch-count effect and add
explicit decay — level strength falls with age. Neither paper publishes a decay rate
that transfers to daily US equity bars.

**Neither paper uses volume.** In the 2014 paper the word appears once, in an
unrelated context. No study found in review offers measured evidence that
high-volume levels hold more often than low-volume ones.

### Method

*Bin width* is `delta = mean(|x_t - x_(t-1)|)` over the series at the sampling scale
in use — both papers' resolution. It self-scales across instruments and price regimes,
so there is no per-symbol tuning and no fixed percentage to go stale. It is also
invariant under permutation of the increments, which makes the arithmetic surrogate an
*exact* match on resolution rather than an approximate one.

*Score* is a Beta-Bernoulli posterior, `P(bounce | b_prev) = (n + 1) / (N + 2)` under a
Beta(1,1) prior, pooled across levels and symbols by prior-touch count. A uniform prior
is the right one because the question under test is precisely whether the rate departs
from a coin flip.

*Control* is the same estimator on surrogates built by shuffling the series' own
returns. That keeps the volatility, fat tails and skew and destroys only the ordering —
which is the only thing a level can be made of.

### The data we actually have

Historical bars come from **yfinance** (`src/data/market.py::get_ohlcv`); Alpaca's IEX
feed is the *fallback* when yfinance returns empty, not the primary source. The series
is **completed bars only**: while the market is open it ends at the previous session;
after the 16:00 ET close it includes today (see `docs/INCIDENT_HISTORY.md`,
2026-09-14 — before that fix it never included today at all). Vendor
history caps, measured 2026-09-02:

| Sampling | History available | Bars/symbol | Universe total |
|---|---|---|---|
| Daily | to 1993 (SPY); `trading.lookback_days: 1800` uses ~5y | 1,254 median at 5y | 121,698 |
| 1 hour | 730 days (vendor cap) | 5,066 | 490,646 (100/101 symbols) |
| 5 / 15 min | 60 days (vendor cap) | 4,609 | 461,302 |
| 1 min | 7 days (vendor cap) | ~2,366 | not measured |

The published studies used tick data at second-scale sampling. We have nothing
comparable, and the question of whether the estimator survives the coarser sampling
had to be answered before anything was built on it.

### The validation gate — the result

101 symbols, daily bars, five years (2021-09-02 to 2026-09-02), arithmetic surrogates
(band identical by construction at $3.1694), five surrogates per series:

| Prior touches | REAL bounces/arrivals | REAL P(bounce) [95%] | SHUFFLED P(bounce) [95%] |
|---|---|---|---|
| 0 | 1878 / 3636 | **0.516** [0.500, 0.533] | 0.471 [0.465, 0.478] |
| 1 | 895 / 1767 | **0.507** [0.483, 0.530] | 0.486 [0.476, 0.497] |
| 2 | 459 / 844 | **0.544** [0.510, 0.577] | 0.495 [0.479, 0.510] |
| 3 | 244 / 439 | **0.556** [0.509, 0.602] | 0.506 [0.483, 0.528] |
| 4 | 129 / 222 | **0.580** [0.515, 0.644] | 0.484 [0.452, 0.516] |
| 5+ | 200 / 310 | **0.644** [0.590, 0.696] | 0.505 [0.470, 0.539] |

Pooled: real **0.5271** against shuffled **0.4805**, separation **+0.0467**. Slope
**+0.0249** P(bounce) per prior touch on real series against **+0.0049** on shuffled.
Stable across seeds 0 / 7 / 13 (separation +0.0467 / +0.0412 / +0.0459; the real slope
is identical, the real series being the same each time).

**The published pattern reproduces in direction and shape — real rises with prior
touches, shuffled stays flat near a coin flip — and is far weaker in magnitude than
the tick-data papers report.** Bounce probability runs 0.52 to 0.64, not "well above
0.5" throughout. No parameter was adjusted to obtain the separation: the estimator has
one scale parameter and it is computed from the data.

At **1 hour** the same shape holds (real 0.461 to 0.594, slope +0.0265; shuffled flat,
slope +0.0051; separation +0.0388). At **5 minutes** it does NOT: the level shift
survives (real 0.5175 against shuffled 0.4753) but the *rise* vanishes — real slope
+0.0028 against a shuffled +0.0035. Whatever produces the touch-count effect on our
data is not visible at 5-minute sampling over 60 days.

### What was changed after seeing a result — stated plainly

The first configuration run did **not** show the separation, and two settings were
changed afterwards. Both changes are recorded here because a reader has to be able to
judge them:

1. **Surrogate mode: log returns to arithmetic differences.** With log-return
   surrogates the control's band came out $1.89 against the real series' $0.82 — the
   control was being measured at 2.3x coarser resolution than the thing it controlled,
   which is not a control. Arithmetic differences make the band identical by
   construction. This is a fairness fix to the control, not a setting on the estimator.
2. **History window: full vendor history to five years.** `trading.lookback_days: 1800`
   is what the desk actually fetches, and a single `delta` spanning 1993 to 2026 is
   meaningless — $3.17 is most of a day's range now and several months of range in
   1995. Five years matches the configured lookback.

Under the original configuration (30+ years, log surrogates, five symbols) real and
shuffled both rose with prior touches and the pooled separation was +0.0011 — no
finding. **A reader who thinks either change was self-serving should treat the result
as unestablished.** Nothing about the estimator itself was touched: it has one scale
parameter and that parameter is computed from the data.

### Two further caveats that matter

**The shuffle control does not isolate levels specifically.** It rules out the
distribution of returns as the explanation. It does not separate level memory from
ordinary volatility clustering or short-horizon autocorrelation, both of which the
surrogate also destroys. The measurement establishes that ordering matters; it does
not prove that *levels* are the mechanism.

**High prior-touch buckets are right-censored upward.** A penetration ends a level, so
a level never broken within the sample contributes bounces and no penetration. That
bias is real and is not corrected — the shuffled control is censored identically, which
is what makes the comparison, not either column alone, carry the finding. Reading the
real 5+ figure of 0.644 as a standalone hold probability would overstate it.

### Recency decay — not fittable at the scale the desk trades

Fitted on our own episodes as `p(age) = 0.5 + (p0 - 0.5) * 2^(-age/H)`, against a
constant-probability null, with the half-life reported only when the extra parameter
clears the chi-square 95% threshold at 1 df *and* lands inside the span of data.

- **Daily (7,218 episodes): NOT FITTABLE.** Likelihood ratio 0.00 on both age
  definitions. There is no age effect in the daily sample at all.
- **Hourly (20,178 episodes): NOT FITTABLE.** Level age fits the wrong way round
  (P(bounce) 0.069 at age zero — a level that *strengthens* with age, which is not the
  claim); time-since-last-touch pins on the grid's one-bar floor.
- **5-minute (18,578 episodes): fittable.** Half-life 19.7 bars (~98 minutes) from
  P(bounce) 0.599 at age zero; 14.1 bars (~70 minutes) on time-since-last-touch.

**Decay stays out.** The desk's levels are daily, and the daily sample supports no rate.
The papers' anchors (tick effect dying between 90 and 180 seconds; daily FX levels
persisting about five business days) are recorded here for sanity only and are
deliberately not imported — importing one would be inventing a market-structure
constant, which is exactly the failure mode this desk has ruled out.

### Volume-at-price — recorded, never scored

The standard profile is computed on the same `delta` bins: point of control is the
maximum-volume bin, and the value area expands one bin at a time toward the larger
neighbour until 70% of volume is enclosed. **The 70% is a normal-distribution
convention, not an empirically optimised figure.** Each bar's volume is spread
uniformly across the bins its range covers — an assumption, and the standard one,
because the alternative is intraday data we do not have at daily scale.

These fields ride along on every `LevelQualityRecord` and enter **no** probability.
The reasoning is stated in the module and repeated here because it is the part most
likely to be forgotten: the published mechanism for why levels exist at all is stacked
resting **limit orders** at a price, and traded volume is a *proxy* for that mechanism —
possibly a poor one, since a price where enormous volume traded is a price where those
resting orders were consumed. **The volume terms are mechanism-motivated, not
evidence-backed.** Recording them is what makes "does volume add anything over touch
count?" answerable on our own book later. Scoring them today would be inventing a
constant. A test (`TestVolumeIsNeverScored`) fails if volume ever moves a probability.

### What this does NOT license

Nothing about sizing. The measured edge is small, the mechanism is not isolated, and
the calibration is fitted on the same history any backtest would score against. Wiring
level quality into sizing, stop placement or the R/R gate is a separate decision on
separate evidence, and it has not been made.

### What changed in `levels.py` because of this measurement (2026-09-02)

`src/data/levels.py`'s `strength` field — the score that selects which 6 levels
per side the Tech Analyst is shown — weighted each touch by `0.5 ** (age_sessions
/ 252)`, a one-year half-life picked for being round and never measured. It is
now touch count alone, discounted by distance exactly as before:
`strength = touches / (1 + distance_pct / 10)`. This acts on the "no age effect"
finding above; it does not wire `level_quality.py` itself into the trading path —
nothing under `src/` imports that module, `TestNotWiredIntoTrading` still passes,
and sizing, stops, targets and the R/R gate are unchanged.

Checked on the full 101-symbol universe (same-day bars) before deciding: removing
the recency term changes the top-6 selection on at least one side for 66 of the 99
symbols that had any levels, and moves 27.1% of the 1,020 side-slots compared (6
slots x 170 sides with a candidate level). That is not a no-op — the Tech Analyst
has been shown a systematically different set of levels than the data supports for
as long as the invented half-life stood. A second alternative — scoring by the
measured pooled bounce probability per touch count instead of raw touch count —
was checked against the same universe and moves the selection even more (33.9% of
slots, 71/99 symbols), because that curve is non-monotonic at low touch counts and
pools everything past 5 touches into one number for posterior-sample-size reasons
unrelated to level quality. Touch count was kept for being simpler and for being
the less disruptive of the two changes, not merely the more convenient one.

The touch-count finding should be treated as promising, not settled, here exactly
as it is above: two settings were changed after an initial run found nothing, and
the shuffle control does not isolate levels from ordinary volatility clustering.

### The stop-honouring threshold, ratified 2026-09-03

Phase 12.1 (docs/QAMC_REMEDIATION_SPEC.md §12.1, "What this does NOT license"
above, and docs/WORK.md's tracked decision) asked the question this section
explicitly declined to answer: how many prior touches should a level need
before a stop resting on it is trusted enough to be honoured however tight?
That is a decision about wiring level quality into stops — separate evidence,
separate decision, made here rather than left in the measurement above.

**SUPERSEDED 2026-10-14 — the touch bar is deleted.** A 148-stock study found a long stop just beyond a real level is hit 26.5% of the time against 30.3% with no level at the same distance (-4.11 pp, t -12.5), and extra touches add nothing (-0.3 pp per touch, t -1.6); any real computed level now backs a stop at any touch count. The original decision follows for the record.

**Decided (superseded): 5 touches** (`risk.min_level_touches_for_stop_honor`, wired in
`PortfolioConstructor._level_backing_stop`). Below the table, restated with
the CI overlap made explicit — the thing that actually decides where the
line goes:

| Prior touches | REAL P(bounce) [95%] | SHUFFLED P(bounce) [95%] | CIs overlap? |
|---|---|---|---|
| 0 | 0.516 [0.500, 0.533] | 0.471 [0.465, 0.478] | No — but only 1 touch above "no structure" |
| 1 | 0.507 [0.483, 0.530] | 0.486 [0.476, 0.497] | Yes (0.483-0.497) |
| 2 | 0.544 [0.510, 0.577] | 0.495 [0.479, 0.510] | Touching (real floor 0.510 = shuffled ceiling 0.510) |
| 3 | 0.556 [0.509, 0.602] | 0.506 [0.483, 0.528] | Yes (0.509-0.528) |
| 4 | 0.580 [0.515, 0.644] | 0.484 [0.452, 0.516] | Touching (0.515 vs 0.516) |
| 5+ | 0.644 [0.590, 0.696] | 0.505 [0.470, 0.539] | **No — 0.05 clear gap** |

5+ is the only bucket where the real and shuffled 95% intervals do not
overlap or touch at all — every other bucket's separation could plausibly be
sampling noise around a coin flip, given the interval widths actually
measured (small per-bucket sample sizes: n=222 to n=439 for buckets 2-4,
against 310 for 5+, so the non-monotonic overlap pattern across 2/3/4 is
itself evidence of noise at that resolution, not a reason to pick 3 or 4).
0 touches also does not overlap, but a 0-touch "level" is not structure by
this system's own definition (`MIN_TOUCHES = 2` in `find_structural_levels`)
and is excluded from consideration on that basis alone, not on the strength
of its separation.

Recency was deliberately NOT added as a second gate: the "Recency decay"
subsection above found no age effect at daily scale at all (likelihood
ratio 0.00 on both age definitions) and stated plainly that importing an
untested decay rate would be "inventing a market-structure constant." A
5-touch level defended three years ago is trusted exactly as much as one
defended last week, on the same evidence that says touch count matters and
age does not.

**What this does not change:** `find_structural_levels`' `MIN_TOUCHES = 2`
in `src/data/levels.py` is untouched — that constant decides whether a level
is shown to the analyst and eligible as a target at all, a question §12.1's
own spec text already treated as settled and separate from "trusted enough
for a tight stop." A level with 2, 3 or 4 touches still exists, still ranks
by touch-count `strength`, and can still anchor a derived target
(`TechAnalysisResult.computed_levels`, consumed by
`derive_structural_target`) — none of that reads
`computed_level_touches` or the new threshold. Only the stop-honouring
exemption in `_level_backing_stop` is gated by it. A level below the bar
does not make the trade untradeable — the stop falls back to the
pre-existing ATR-floor widening logic, the same fallback an unbacked stop
has always used.

**Status:** ratified, not merely proposed — the owner's standing instruction
was "go with whatever the research says," and 5 is what the measured
separation supports. Revisable the same way every other placeholder
threshold on this desk is, on new evidence, not a re-guess.

---

## 8. Drawdown alarms — what the literature gives you, and what it does not (2026-09-11)

> **SUPERSEDED 2026-09-20 — READ THIS FIRST. The mechanism this section
> derives no longer exists.** The owner removed the entire account-level
> loss alarm — the daily circuit breaker and the 5-day / 20-day
> rolling-return brakes — because it kept being shown able to fire on
> ordinary market fluctuation, and ruled that per-position stop-losses are
> the desk's loss protection (`docs/INCIDENT_HISTORY.md`, 2026-09-20,
> retired board item 32). Everything below describes how its thresholds
> were derived while it existed, and is kept because the NEGATIVE result is
> still true and still worth not re-inventing: **the literature does not
> give you a drawdown-alarm threshold.** Do not read this section as a
> recipe for bringing the alarm back. Bringing it back at all is an owner
> decision, not a research one.

Recorded here because the useful result is a NEGATIVE one, and a negative
result is exactly the kind of thing that gets quietly re-invented as a
confident number by the next session that looks at this.

**Context.** The desk's three loss alarms (daily circuit breaker, 5-day and
20-day rolling-return brakes) were each a fixed percentage of equity. That
basis assumes stationarity, which markets do not have, so it was replaced
with a basis relative to the recent realized volatility of the book the
desk actually holds — measured from its holdings' real market price
history. (An earlier version of the same change measured the ACCOUNT's own
equity curve and was rejected the same day: a post-reset account ramping
from cash barely moves, so it would have measured artificially low and set
the alarms artificially tight.) See `docs/INCIDENT_HISTORY.md`,
2026-09-11.

### What IS in the literature, and is used

- **Drawdown magnitude scales with the square root of the window length.**
  Van Hemert, Ganz, Harvey et al., *Drawdowns*, Journal of Portfolio
  Management, 2020. This is what lets one sensitivity govern all three
  windows: `threshold(T) = sensitivity x sigma_daily x sqrt(T)`. It was
  already cited in this codebase before this change and is unchanged by it.
- **Measuring risk relative to trailing realized volatility** is ordinary
  practice, not a novelty — a ~20-trading-day realized-volatility window is
  a well-established convention. Robert Carver's systematic-trading risk
  writing is the usual accessible reference for the general approach.

### What is NOT in the literature, searched for specifically and not found

**There is no citable, published, industry-standard number for how many
multiples of recent volatility should trip a drawdown alarm.** This was
searched for directly on 2026-09-11 rather than assumed. The published work
covers how drawdowns SCALE and how volatility is MEASURED; the trigger
level is a risk-appetite choice each desk makes, and the numbers that
circulate informally are not traceable to a result.

Consequences, applied:

- The sensitivity is shipped as an explicitly provisional value, labelled
  as such in `src/risk/constants.py`, `src/config.py`,
  `config/settings.yaml`, `docs/WORK.md` and its test module. It is NOT
  presented as researched.
- It was FIRST set to 6.7 by day-one continuity against the
  previously-shipped thresholds, so the change in basis would not smuggle
  in a change in severity. Making that measurement is what showed 6.7
  meant the daily breaker only fired on a ~6.7-sigma session — a
  crash-grade event, i.e. effectively dormant. **The owner then set it to
  3.0 (2026-09-11): a risk-appetite decision, reversible, and still not a
  researched number.** Roughly a 3% daily loss on a ~1%/session book.
- **Do not "find" a citation for it later.** If a future session believes it
  has one, check that the source actually gives a trigger multiple for a
  drawdown alarm and is not a volatility-TARGETING paper (a different
  technique, and one this desk has separately rejected — `docs/OUTCOME.md`).

### One real measurement made in support of it

The account's own equity curve could not supply a reference volatility (the
live `daily_pnl` table was read on 2026-09-11 and holds exactly one row,
the 2026-09-02 reset) — and, as above, it should not be asked to. This
measurement was made over real market data from this desk's own configured
101-symbol universe: trailing-20-session realized daily volatility of
equal-weight baskets the size this desk runs.

**It has since become the live mechanism, not a proxy for it.** The same
measurement now runs against the ACTUAL current holdings at their ACTUAL
weights, every session. The figures below are what generic baskets of this
size look like, retained for scale:

| Basket size | Median 20-session daily volatility | 60-session |
|---|---|---|
| 5 names | 1.04% | 1.23% |
| 8 names | 0.86% | 1.07% |
| 12 names | 0.80% | 0.99% |

Spread across draws was roughly 0.55%-1.7%. For scale, SPY over the same
window measured 0.54% and the median single name 1.57%. ~1.0% per session
is the figure the threshold illustrations elsewhere are quoted against.

**Note what this means for a partly-invested book.** Weights are fractions
of equity and are deliberately not rescaled to sum to 1, so a 30%-invested
book measures roughly 30% of the figures above and gets a proportionally
tighter alarm. That is intended: a third of the book at risk should not be
allowed the same loss as all of it.

---

## 9. ATR bands — what a volatility multiple is, and what the literature will not tell you (2026-09-26)

Added for board item 143: this file had no entry at all for the ATR
multiples that govern the desk's noise band and stop floor, so every one of
them was unsourceable from the desk's own research file. Recorded here is
what a literature pass on 2026-09-26 actually found — including where it
found nothing.

**Do not re-derive the desk's own work from this section.** The two
underived `1.0` multiples and the split between them are board item 70
(`docs/board_notes/`, "item 70"); the 1.5 → 2.5 ATR stop-floor
re-derivation and its explicit warning not to conflate a fixed entry stop
with a trailing stop is `docs/INCIDENT_HISTORY.md`, 2026-09-10; the
owner's ratification of the 2.5 floor and the three regime scales as
appetite-with-values-unchanged is `docs/INCIDENT_HISTORY.md`, 2026-09-25
(item 184 retired). Read those first.

### What IS published

- **ATR itself, and its conventional period, are Wilder's.** J. Welles
  Wilder, *New Concepts in Technical Trading Systems* (1978), introduced
  Average True Range specifically so a volatility measure would not miss
  gap and limit moves that a plain high-low range ignores. Wilder names
  **7 and 14** periods in the book; no derivation of 14 is given by him or
  by the reference sources that carry it forward
  ([StockCharts ChartSchool, ATR](https://chartschool.stockcharts.com/table-of-contents/technical-indicators-and-overlays/technical-indicators/average-true-range-atr-and-average-true-range-percent-atrp);
  [Macroption, "ATR period"](https://www.macroption.com/atr-period/),
  fetched 2026-09-26, which states plainly: *"There is no best method and
  no best period."*). **So the ATR period this desk multiplies everything
  by is a convention, not a measurement** — and the ledger already watches
  it (item 90).
- **A trailing ATR band of 3.0 x ATR(22) is a named, attributable
  default**, not a folk number: the Chandelier Exit, Chuck LeBeau,
  popularised in Alexander Elder's *Come Into My Trading Room* (2002)
  ([StockCharts ChartSchool, Chandelier Exit](https://chartschool.stockcharts.com/table-of-contents/technical-indicators-and-overlays/technical-overlays/chandelier-exit),
  fetched 2026-09-26). The same page gives **no justification for 3.0**
  and explicitly says to vary it — *"Volatile stocks may require a higher
  multiplier to reduce whipsaws. Relatively dull stocks may need a lower
  multiplier to increase sensitivity"* — illustrating 5.0 on a volatile
  name. It states no numeric bounds.

### What is NOT published, searched for specifically on 2026-09-26

**There is no published measurement of the ATR multiple at which an
adverse move stops being noise.** That is the exact quantity the desk's
noise band bounds, and searching for it directly returns only practitioner
blog posts that contradict each other — 1x described as triggering "on
noise about half the time", 2x as the filter threshold, envelopes quoted
at 1-2 and stop rails at 2.5-3, with no measurement, sample or method
behind any of the splits. None of it is citable under this desk's rule and
none of it is recorded here as a finding. **The honest state is: searched,
nothing published answers it.**

What this means in practice:

- The 2-3 x ATR magnitude is **corroborated as conventional** by two
  independent named sources (Wilder-derived practice, LeBeau's Chandelier).
  That is a range, and **choosing a value inside it is owner appetite, not
  research.** This section picks nothing and recommends changing nothing.
- The corroboration is for a **trailing** band off a running extreme.
  `docs/INCIDENT_HISTORY.md`, 2026-09-10 already warns against reading it
  as support for a fixed distance from a static entry. That warning stands
  and this section does not weaken it.
- A multiple for the *noise band* — a different question from either — has
  no source at all. Item 70 remains open on exactly that basis.

### Does the band's sqrt(sessions_held) widening match the tape? Measured 2026-10-04 — NO, and nothing replaces it

`src/risk/exit_guard.py::noise_band_atr` widened the band as
`ATR * sqrt(sessions_held)` with no cap, so a position held 60 sessions must
move about 7.8 ATR against entry before a discretionary sale is allowed. The
sqrt shape was adopted by analogy with random-walk dispersion and never
measured; the cap was never derived because picking one is barred.

`ops/research/noise_band_holding_scaling.py` measures the claim the band
actually makes — that an adverse excursion from entry, in ATRs, marks a
finished trend, and that the excursion size which marks it grows with holding
length. Source: the committed public daily-bar panel
(`ops/model_policy/fixtures/yf_daily_bars_pm_public_day_2026-09-14.json.gz`,
101 symbols, roughly five years of sessions ending 2026-09-14). For each
symbol, holding length and entry bar it takes the adverse move
`(close[entry] - close[entry+h]) / ATR14[entry]` when positive, and the
forward return over the next 10, 20 or 60 sessions minus that symbol's own
in-sample mean forward return, so the common equity drift cannot decide the
answer. Thresholds are discovered on the first 60% of each series and judged
on the held-out 40%; a returns-shuffled control per symbol gives the
no-structure baseline. Between 25,882 and 33,520 in-sample observations stand
behind every cell.

**Finding 1 — the band's premise fails even before the scaling question.**
In no real-data cell does mean excess forward return fall as the adverse
excursion grows. At every holding length the gradient runs the other way, and
it steepens with holding length: at h = 60 and a 20-session forward horizon,
the smallest-adverse decile averages -0.14% excess and the largest (left edge
7.84 ATR) averages +4.14%. The shuffled control shows no such gradient
(+0.36% to +0.95%, unordered), so this is structure in the tape rather than
the return distribution. A larger adverse move is therefore not evidence the
trend is over; if anything the tape says the opposite.

**Finding 2 — no cap is derivable, and none is proposed.** Because the
relationship never turns the way the band assumes, the measurement produces no
excursion size above which a trend is finished, at any holding length — so
there is no point at which such a size stops growing, which is what an honest
cap would represent. No value in `exit_guard.py` was changed by this work.

**Outcome, 2026-10-09 — the gate is REMOVED.** On Finding 1 the owner ruled
that no discretionary sale is refused because of the price the desk paid
("sell anything below the bar, always"). The entry-anchored midday gate, its
constant `NOISE_BAND_ATR_MULTIPLE`, its sqrt widening and its midday
recording were deleted together, so a losing holding the reviewer wants to
sell is no longer held back by its distance from entry. The trailing band
(1.25 ATR, off the current price) and the structural-protection fallback
multiple (flat, off the running extreme) are different quantities and were
not changed.

**What a different answer would have looked like.** Had the sqrt widening been
right, each decile table would show a region of negative excess forward return
beginning at a left edge that rises with holding length roughly as
sqrt(h) — 1.0, 1.41, 2.24, 3.16, 4.47, 6.32, 7.75 at h = 1, 2, 5, 10, 20, 40,
60 — and the discovered threshold would separate forward returns out of
sample. The estimator can return such values: in the shuffled control at
h = 60, forward 10, it returned 6.46 ATR. It is not pinned at zero or at
"none" by construction.

**The caveat that bounds Finding 1.** The panel is a set of symbols that
survived the whole window, so post-drawdown rebounds in it are overstated.
That is a reason not to trade the reversal gradient, not a reason to doubt the
null: survivorship inflates the rebound, and the band's premise needs the
opposite sign.

---

## 10. Trailing stops — the one genuinely academic result, and its limits (2026-09-26)

**Do not re-derive the desk's own work from this section.** The six
unsourced trail constants, the target that never reaches the broker and the
"trail sits too loose" finding are board item 75 (`docs/board_notes/`,
"item 75"); the minimum-ratchet enforcement is `docs/WORK.md`'s retired
item 108 and `docs/INCIDENT_HISTORY.md`, 2026-09-25; the range-setup
ratchet-before-target change is `docs/INCIDENT_HISTORY.md`, 2026-09-25
(item 142 retired).

### What IS published

- **Stop-loss rules are not free, and whether they add value depends
  entirely on the return process.** Kaminski & Lo, *When do stop-loss rules
  stop losses?*, Journal of Financial Markets 18(C), 234-254 (2014);
  working paper SIFR No. 63
  ([abstract, fetched 2026-09-26](https://ideas.repec.org/p/hhs/sifrwp/0063.html);
  full text fetched 2026-09-26 from
  [smallake.kr mirror](https://www.smallake.kr/wp-content/uploads/2017/02/When_Do_Stop-Loss_Rules_Stop_Losses.pdf)).
  Their result: **under the Random Walk Hypothesis the "stopping premium"
  is always negative** — a 0/1 stop-loss rule always *lowers* expected
  return on an IID process. Under momentum or regime-switching it can be
  positive. This is a conditional result, not an endorsement.
- **Their empirical scan deliberately reports a RANGE, and says why.**
  Stop thresholds were varied **from -1.5 to -0.5 standard deviations**
  from the mean at the relevant frequency, re-entry thresholds from -0.5 to
  +1.0 sd, across daily/weekly/monthly/quarterly frequencies, on daily
  US futures data January 1993 - November 2011 (stocks, with long-term
  bond futures as the stop-loss asset). Their stated reason for scanning
  rather than reporting one number: *"To avoid data selection bias, we
  review a large range of stops to demonstrate how the performance depends
  on threshold choices."* **Reported effect in one calibration: +1.5%
  return, -5% volatility, Sharpe up as much as 20%.**
- **The frequency finding is the part most relevant to this desk.** They
  find short-horizon stop policies carry **negative** stopping premiums
  over large parameter ranges, and that policies at frequencies **above
  one month** perform better and can achieve positive premiums. They also
  find the **exit threshold matters more than the re-entry threshold** for
  variation in results.
- **One student-thesis replication exists and its result is a wide band,
  not a point.** Snorrason & Yusupov, *Performance of Stop-loss Rules vs.
  Buy-and-Hold Strategy*, Lund University master essay NEKM01, Spring 2009
  ([fetched 2026-09-26](https://lup.lub.lu.se/student-papers/record/1474565/file/2435595.pdf)).
  OMX Stockholm 30 constituents, daily data January 1998 - April 2009,
  quarterly holding periods, stop levels swept **5% to 55%**. Highest
  average quarterly return (1.7%) at a **20%** trailing level, highest
  cumulative return (74%) at **15%**; the **5%** trailing level was the
  only setting that underperformed buy-and-hold (-0.1% average, -8.1%
  cumulative). **Read the size of that sweep before reading the winners:
  the 15-20% peak is the top of a 5-55% grid on one index over one period,
  which is the shape of an in-sample optimum, and the authors set the
  starting date arbitrarily by design.** It is recorded here as evidence
  that *too tight* is a real and measurable failure mode, not as a source
  for a trail distance.
- **The optimal-trailing-stop problem has been solved analytically, and
  the answer is not a number.** Leung & Zhang, *Optimal trading with a
  trailing stop*, arXiv:1701.03960v2 (2019)
  ([fetched 2026-09-26](https://arxiv.org/pdf/1701.03960)). Under a general
  linear diffusion they solve the optimal liquidation and entry timing
  given a pre-specified percentage drawdown stop, illustrated on an
  exponential Ornstein-Uhlenbeck model. **The output is a trading region
  conditional on a fitted price model — it does not yield a trailing
  distance for a real equity without first committing to that model**,
  which is a fitting exercise this desk has ruled out (`docs/OUTCOME.md`,
  "No fitting, only reading").

### What is NOT published, searched for specifically on 2026-09-26

- **No published source gives a minimum ratchet increment** — how far a
  trailing stop must move before moving it is worth the whipsaw. Searched
  directly; the returns describe *mechanisms* (per-tick, step-size,
  time-interval) with no measurement of any step size. The desk's own
  minimum-ratchet value is therefore still appetite, and
  `docs/INCIDENT_HISTORY.md`, 2026-09-25 records it as ratified rather than
  sourced. Nothing found here changes that.
- **No published source gives a trail-adjustment cooldown.** Same search,
  same result: nothing.
- **Picking a trail multiple inside the corroborated 2-3 x ATR band
  (section 9) is owner appetite, not research.** No source found narrows
  it, and this section does not.

---

## 11. Profit-taking — the evidence runs against preset targets, and it is about direction, not size (2026-09-26)

The desk deleted its automatic take-profit trim on 2026-09-12 and made the
trailing stop the only exit rule; the reasons, the n=1 tuning history and
the owner's ruling are in `docs/INCIDENT_HISTORY.md`, 2026-09-12 and are
not restated here. What was missing was any published evidence either way.
**The literature pass finds evidence pointing the same direction as that
decision — but it is weaker and narrower than the decision, and must not be
cited as having proved it.**

### What IS published

- **Investors who sell winners and keep losers are, on average, wrong —
  measured.** Terrance Odean, *Are Investors Reluctant to Realize Their
  Losses?*, Journal of Finance 53(5), 1775-1798 (1998)
  ([full text fetched 2026-09-26](https://faculty.haas.berkeley.edu/odean/papers%20current%20versions/areinvestorsreluctant.pdf)).
  10,000 discount-brokerage accounts. Table VI, excess returns over the
  CRSP value-weighted index following the sale of a realized winner versus
  a paper loser that was held:

  | Horizon | Winners sold | Losers kept | Difference (p) |
  |---|---|---|---|
  | 84 trading days | +0.47% | -0.56% | **+1.03%** (0.002) |
  | 252 trading days | +2.35% | -1.06% | **+3.41%** (0.001) |
  | 504 trading days | +6.45% | +2.87% | **+3.58%** (0.014) |

  Odean's own framing: the behaviour is *"not justified by subsequent
  portfolio performance."* He ties the 1-year result to Jegadeesh &
  Titman's momentum horizon and notes DeBondt & Thaler's reversals at
  3-5 years, i.e. **the result is horizon-bounded and reverses eventually.**
- **Read the limit of that result honestly.** It measures the *selection*
  of which position to close, over horizons of 84-504 trading days, in
  retail accounts. It is **not** a test of a fixed-percentage profit trim,
  it does not measure partial exits, and it says nothing about position
  size. It is evidence that closing a winner because it is a winner is a
  documented, costly bias — which is the class the deleted trim belonged
  to — and nothing more.
- **Capping the upside of a trend-following position provably changes the
  shape of its payoff.** Dao, Nguyen, Deremble, Lempérière, Bouchaud &
  Potters, *Tail protection for long investors: Trend convexity at work*,
  arXiv:1607.02410 (2016)
  ([fetched 2026-09-26](https://arxiv.org/pdf/1607.02410)). Trend
  performance is shown to come from the gap between long-term and
  short-term realized variance, giving trend strategies positive convexity
  in the trend signal. Their §2.4 treats capping: an extreme cap (position
  = ±1 on the sign of the trend) turns the parabolic payoff into a
  **piece-wise linear** one, and softer caps interpolate between the two —
  *"the positive convexity of this curve is a generic property of trend
  following strategies."* **What this does and does not say:** it is about
  capping POSITION SIZE in a systematic trend strategy, not about a
  discretionary profit target, and it finds convexity survives capping in
  reduced form. Cite it for the mechanism — truncation flattens the right
  tail — never as a measurement of what a profit target costs this desk.

### What is NOT published, searched for specifically on 2026-09-26

**There is no citable study of scaling out versus holding a full position
to a trailing exit.** Searched directly; every result was practitioner
education material, and the most honest of those says the answer must be
measured in your own trade history rather than assumed. **No number, no
range, nothing to record but the absence.** In particular nothing found
supports any specific trim fraction or any specific gain trigger — the two
numbers the deleted rule contained.

Board item 75's four-way exit comparison (sell all at target; sell half and
trail; target tightens the trail; today's desk) is a MEASUREMENT proposal,
not a research question, and this pass does not answer it or license it.

---

## 12. Pacing — the literature is about portfolio rebalancing horizons, not per-position deadlines (2026-09-26)

"Pacing" here means the desk's judgement of whether a position is
progressing fast enough against the horizon pinned at entry.

**Do not re-derive the desk's own work from this section.** The
calendar-days-versus-sessions defect is `docs/INCIDENT_HISTORY.md`,
2026-09-20 (item 91 retired); its siblings, including the removal of the
`1/3`-of-horizon pace floor by owner ruling on 2026-09-25, are board item
165 in `docs/WORK.md`.

### What IS published

- **Momentum's profitable horizon is measured, and the measurement is a
  broad plateau, not a point.** Jegadeesh & Titman, *Returns to Buying
  Winners and Selling Losers*, Journal of Finance 48(1), 65-91 (1993)
  ([full text fetched 2026-09-26](https://www.bauer.uh.edu/rsusmel/phd/jegadeesh-titman93.pdf)).
  NYSE and AMEX, 1965-1989, a **16-cell grid of 3/6/9/12-month formation x
  3/6/9/12-month holding**; every cell is positive, the strongest being
  12-month formation / 3-month holding at **1.31% per month**. Their own
  headline caveat, in the abstract: the abnormal returns of the first year
  **dissipate in the following two years** — and one panel notes the
  cumulative first-12-month return dissipating almost entirely by month 24.
- **What that licenses, exactly.** It establishes that a continuation edge
  has a finite life measured in months and then decays. It is a
  **cross-sectional portfolio rebalancing frequency** — how often a ranked
  basket is re-formed — and every cell of the grid works. It is **not** a
  per-position deadline, and it gives no basis for a per-name horizon.

### What is NOT published, searched for specifically on 2026-09-26

**Nothing published gives a per-position horizon, a pace threshold, or a
time-stop length for a discretionary multi-day equity trade.** Searching
for "time stop" / "time-based exit" empirical tests returns practitioner
material (a widely-repeated Curtis Faith 80-day Donchian time exit, and
general claims that time exits are robust because they resist curve
fitting) with no fetched study behind the numbers. The nearest academic
neighbour is Kaminski & Lo's frequency result (section 10) — which
concerns the *frequency at which a stop rule is evaluated*, not how long a
position may take — and it points the other way from a short clock: their
short-horizon policies carried negative stopping premiums.

Consequences, stated plainly:

- **The horizon pinned at entry is not a researched number and this pass
  does not make it one.** Any pace threshold built on it is appetite.
- The owner's 2026-09-25 removal of the `1/3` pace floor — on the grounds
  that it was a made-up clock stacked on a guessed horizon — is
  **consistent with what the literature does and does not support.** That
  consistency is recorded; it is not a claim the ruling was derived from
  research.

---

## 13. Ranking granularity — the finance literature is silent, the evaluation literature is not (2026-09-26)

"Ranking granularity" here means: the desk's composite conviction score is
coarse enough that candidates tie frequently, and what happens after a tie
is decided by a tiebreak rule rather than by the score.

**Do not re-derive the desk's own work from this section.** The measured
tie rates on this desk's own technical reads (64% of reads sharing a
composite score; 9 of 12 names tied one day, 23 of 33 another), the
alphabetical fallback, and the three-stage tiebreak that replaced it are
`docs/INCIDENT_HISTORY.md`, 2026-09-20 (item 141 retired). The backtester's
alphabetical rationing when the risk budget binds is board item 64; the
four seats with no strength scale of their own are board item 65. The
rotation margin's own unidentifiability finding — that only four distinct
ratios occur across the whole band, so the margin sits inside the score's
same-day noise — is the item 39(a) work in `docs/INCIDENT_HISTORY.md`,
2026-09-23 ("the pruning mechanism watched the wrong dial"), and is the
closest thing the desk has to a measurement of its own score's resolution.

### What IS published — but not in finance

**Searched on 2026-09-26 for published work on score granularity, tie rates
and tie-breaking in stock ranking or portfolio selection: nothing usable.**
The finance results returned were about the ranking signals themselves, not
about the resolution of the score or what ties do to the selection. The one
finance-adjacent tie convention found is a patent claim describing breaking
a portfolio-selection tie by market capitalisation — a convention in a
filing, not a measurement, and not recorded here as evidence.

The question *is* addressed, rigorously, in the recommender-systems and
information-retrieval evaluation literature, which is a genuinely different
field and must be labelled as such:

- Guo, Chen, Zhu & Li, *Tie Handling Is Part of the Evaluation Protocol: An
  Order-Invariance Audit for Tie-Heavy Recommender Scores*,
  arXiv:2609.26977v1, 22 September 2026
  ([fetched 2026-09-26](https://arxiv.org/html/2609.26977v1)). Their
  central finding: **when scores are coarse, the tie-breaking rule becomes
  an undeclared ranking feature and dominates the reported result.** On one
  audited dataset every row contained an exact tie, **99.94%** contained a
  tie crossing the top-10 boundary, and NDCG@10 moved from **0.8474 to
  0.1702** purely by changing the tie-break order; a finer-grained
  residualized score on the same data moved by **0.0004**. Their
  recommendation is disclosure and sensitivity testing — report tie
  prevalence, state the tie-break rule explicitly, and test the metric
  under permuted tie order.
- Corroborating prevalence from IR: ties are common enough to be a standard
  evaluation hazard — 77% of TREC Web runs contained ties, in 72% of
  rankings [reported via search, 2026-09-26, primary source not fetched —
  treat as unverified].

### What this does and does not license

- **It does not give a granularity target.** There is no published number
  for how many distinct score levels a ranking needs, in any field found.
  Choosing one is appetite, or a measurement this desk would have to make
  on its own scores.
- **It does license one cheap, non-numeric practice**, already partly
  matching what the desk was pushed into by item 141: the tie-break rule is
  part of the ranking and should be stated, and tie prevalence should be
  reported rather than left implicit. Note that item 141's own write-up
  records the opposite state today — **neither the touch-count tiebreak nor
  the reward:risk tiebreak is rendered into the PM's prompt or any
  owner-facing surface** — and flags it as an unresolved gap. The published
  evidence above is consistent with that gap mattering. It does not measure
  how much it matters here, and no such measurement is claimed.
- **The field gap is real and is not hand-waved away.** These are
  recommender and IR metrics, not portfolio returns. The transferable claim
  is the mechanism — coarse scores make the tiebreak decisive — not any of
  the magnitudes.

---

## Summary of what to build, in order

| Priority | Item | Type |
|---|---|---|
| 1 | Routine/opportunistic insider filter | Python — built on branch, not merged/deployed |
| 2 | Lazy Prices 10-K year-over-year diff | Python |
| 3 | News cascade: dedup → novelty score → LLM on the residual | Python + LLM |
| 4 | Deterministic macro regime; LLM confined to FOMC text | Python + LLM |
| 5 | Insider purchase as % of holdings, role weighting, cap tilt | Python |
| 6 | Post-cutoff discipline in the backtester | Process |
| 7 | Level-quality measurement — built, measured, `levels.py` strength now touch-count-based; `level_quality.py` itself NOT wired; stop-honouring touch bar (5) ratified 2026-09-03 | Python |
| 8 | Drawdown-alarm basis — volatility-relative, shipped 2026-09-11; sqrt(time) scaling research-grounded, trigger sensitivity provisional (no published number exists) | Python |
| — | ATR bands, trailing, profit-taking, pacing, ranking granularity (sections 9-13) — literature recorded 2026-09-26, **no build and no number licensed**; every value in these areas remains appetite or an open board item | Research |

Items 1, 2 and 5 need no new data source and no model spend.
