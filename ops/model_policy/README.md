# Model policy tooling

> **OWNER DECISION 2026-09-15 — NO MORE TEST-ENVIRONMENT WORK.** The owner is no longer interested in model tests, practice days or benchmark rigs: too many flaws, too much setup, too many resources for too little. The paper desk runs LIVE and problems are fixed from what live sessions show. Do not propose, build, repair or run tests/benchmarks/practice days unless the owner explicitly asks. Current seats: PM `openai/gpt-5.5`; analysts `gemini-3.5-flash-lite` (Google direct, free); risk manager and position reviewer unchanged. Desk switched back on 2026-09-15.


Three commands. Everything here is read-only with respect to trading: no
order is placed, no broker state is touched, no credential is held.

| Command | What it answers |
|---|---|
| `benchmark_models.py` | which model each agent seat should use |
| `verify_pricing.py` | are the pinned OpenRouter rates still correct |
| `../commissioning/verify_commissioning.py` | is the deployed config actually on the accepted policy |

The resulting policy and its evidence live in
[`docs/architecture/MODEL_ROUTING_POLICY.md`](../../docs/architecture/MODEL_ROUTING_POLICY.md).

## Which exams may run

`fixture_policy.py` is the single gate: an exam fixture may hold RAW PUBLIC
FACTS ONLY (source + fetch date per data section), never a desk recording or
an agent output — see its module docstring for the full rule and
`benchmark_models.py`'s `refusal_reason` for how a scenario is refused
before any paid call. As of 2026-09-14:

- **Runnable:** `earnings_filing`, `smart_money_form4` (real SEC EDGAR
  fetches), `tech_batch` (real yfinance bars; `analyze_batch`'s own
  defaults cover the missing prior-ratings/prior-macro agent outputs),
  `macro_stress` (real FRED series, fetched through the OneCLI credential
  gateway so no key value ever enters this process), `news_intel` (real
  RSS wires, fetched live at fixture-build time). The last two grade
  schema/rule compliance only — real market conditions and real news have
  no engineered correct answer to grade judgement against.
- **Runnable, PM seat:** `pm_public_day` — the trade-picking seat needs
  analyst-seat OUTPUT (analyses, macro/news/earnings/smart-money), which the
  raw-facts-only rule above bans outright. `fixture_policy.py` gained two
  narrow provenance `kind`s for this: `fresh_analyst_output` (a section is
  agent output, but computed FRESH by today's agent classes over an
  already-checked raw-facts fixture — model/route/timestamp/source_fixture
  recorded and the source_fixture recursively checked) and
  `synthetic_account_state` (a labelled synthetic starting account — cash,
  no positions — since a real account is desk state). Both still ban desk
  sources. `ops/model_policy/build_pm_public_day_fixture.py` builds
  `fixtures/pm_public_day_pm_input.json` by running the real tech/macro/
  news/earnings/smart-money agents over the google-direct route with
  `gemini-3.5-flash-lite` only (free tier — never OpenRouter or any other
  paid call). macro/news read the existing `fred_macro_2026-09-14.json` /
  `rss_feeds_2026-09-14.json` fixtures (neither scales with the trading
  universe); tech/earnings/smart-money read three dedicated real-day-scale
  fixtures built fresh for this exam (2026-09-14 OWNER RULING — the old
  5-actionable/2-neutral, 1-earnings-read, 3-day-insider-window fixture
  didn't match a real desk day): `yf_daily_bars_pm_public_day_2026-09-14`
  (yfinance bars for the full live 101-symbol universe,
  `config/settings.yaml` `trading.universe`), `sec_10q10k_pm_public_day_2026-09-14`
  (real 10-Q/10-K HTML for every universe symbol with a filing inside the
  live 45-day earnings window, `EarningsDataProvider`'s default) and
  `sec_form4_pm_public_day_2026-09-14` (real market-wide SEC Form 4
  disclosures via the live 365-day-configured insider window — actual
  coverage achieved is a real but partial 7 days, stated plainly in that
  fixture's `_exam.actual_coverage`; SEC's own resumable discovery builds
  full-year coverage over many daily refreshes in production, not one
  session). The rebuilt fixture measures 62 analyses (15 actionable / 47
  neutral), 49/49 earnings analyses and 8 smart-money findings over 40
  reconstructed observations — `test_fixture_scale_matches_live_derived_values`
  (`tests/test_pm_public_day_scenario.py`) pins the universe size and the
  earnings/insider windows to the same live config/code these fixtures were
  built from, so a future edit can't silently shrink the exam again. Every
  memory/history input `PortfolioManagerAgent.decide()` takes
  (weekly_narrative, position_history, calibration_note, ...) is left at
  its own documented empty default, matching what a real first session on a
  flat account would show — see the fixture's `_provenance` block and
  `pm_public_day`'s `description` in `scenarios.py` for the full input
  inventory. Grades live grounding, whether every opened target is in the
  desk's own pre-decision eligible set (`candidate_eligibility`, reused
  directly, not re-derived), and `reasoning_chain` schema completeness —
  no judgement answer key, opt-in like `pm_selection`.
- **Blocked** (see each `Scenario.blocked_reason` for the exact citation):
  `risk_rr_breach`, `risk_drawdown_discipline`, `tech_batch_full`.
- **Quarantined** (fixture is a desk recording, kept not deleted):
  `pm_selection`, `midday_exit`.
- **No exam at all:** `evening_analyst`, `meta_reflector` — see
  `NO_EXAM_SEATS` in `scenarios.py`.

## Benchmarking

```bash
.venv/bin/python ops/model_policy/benchmark_models.py --from-onecli --repeats 2 \
  --budget-usd <USD>
```

**The benchmark has its own budget.** `--budget-usd` is required for any run
that calls a model and has no default: the person running it sets it. It
becomes the session spend cap of the benchmark's own cost-circuit breaker,
and the call-count backstop is sized from the planned trial count. Both are
set on a copy of the `llm_cost_circuit` config; the live desk's caps in
`config/settings.yaml` are untouched. Before the first paid call the run
prints the trial count, a per-model estimate (live OpenRouter pricing x the
largest token counts committed results have measured for each scenario) and
the budget, and refuses to start if the estimate exceeds the budget unless
`--allow-partial` is passed. The budget must be at or below the live daily
cap (`LLMCostCircuitConfig` rejects a session cap above it); a larger budget
is refused before any paid call. It also refuses if the live **daily** cap —
which the benchmark still shares, because its spend lands on the same day
row — has less headroom than the run plans to spend. When the budget is
reached mid-run the remaining trials are recorded `skipped_budget` and the
table shows `NOT RUN`, never `0.00`. Caps are checked on settled spend before
each call, so a run can overshoot its budget by at most one call.
`--report` and `--merge-out` spend nothing and need no budget.

`.env` is loaded automatically when the API-key variables are unset, so no
`set -a && . ./.env` step is needed; the values are OneCLI placeholders and
are never printed.

**Every trial runs under the same explicit reasoning and output-format
settings as the live desk** (2026-09-14, see `docs/INCIDENT_HISTORY.md`):
`reasoning: {"effort": config.llm.reasoning_effort}` (default `"medium"`,
OpenRouter's own documented default — see
https://openrouter.ai/docs/use-cases/reasoning-tokens) and, for any agent
with a known result schema, a strict `response_format` json_schema (see
https://openrouter.ai/docs/features/structured-outputs). This is inherited
automatically because the benchmark drives the same `BaseAgent` subclasses
the live pipeline uses — there is no separate benchmark-only code path for
it — and each row in the results file records which `reasoning_effort` /
`structured_output` value, and which `provider`, actually went out with that
trial. To test a model over Google AI Studio direct instead of OpenRouter —
the route 7 live seats actually use — prefix its id with `google-direct:`
(e.g. `google-direct:gemini-3.5-flash-lite`); Google's own OpenAI-compat
endpoint gets the same two settings via its documented `reasoning_effort`/
`response_format` fields (see `docs/INCIDENT_HISTORY.md`'s 2026-09-14
follow-up).

This drives the **real** agent classes (`src/agents/*`) with the **real**
prompts (`config/prompts/*.md`) over frozen inputs — real public-source raw
facts (SEC EDGAR, yfinance, FRED, live RSS wires) recomputed by today's code
for the runnable exams listed above, synthetic-but-forced-arithmetic for the
still-blocked ones, and `pm_selection`'s real recorded session — and
grades each result with deterministic Python assertions in `scenarios.py`.

That choice is the point of the harness. The question is not "can this
model write JSON" — it is "does its output survive `analyze_batch` /
`decide` / `review`, including `parse_json()`'s candidate scan, the
per-entry isolation that drops malformed rows, and every Pydantic
validator in `src/models.py`". Only the production call path answers that.
The sweep has already caught a model whose only flaw was emitting
`market_sentiment="neutral_to_defensive"` — plausible prose, and the entire
news report is discarded before PM ever sees it.

Grading never asks whether a model shares an opinion about the market.
Every scenario is built so the correct answer follows from arithmetic the
prompt already states: `risk_rr_breach` contains a range BUY at 0.42R that is
also the largest line in the plan at 18% of the book (the pair is the
finding — the desk has had no universal reward:risk floor since 2026-09-11,
`docs/WORK.md` item 1(d)), `pm_constrained` cannot fund new weight without
trimming, `risk_drawdown_discipline` has a BUY sized at the full base while
`in_drawdown=true` requires it halved.

> **`midday_exit` REBUILT 2026-10-02 — and it inverted its own verdict
> before that.** Its `acts_on_broken_thesis` check (weight 0.35) REWARDED a
> SELL/REDUCE on a position whose only fault was sitting 0.25 ATRs from its
> stop. `config/prompts/position_reviewer.md:307-312` says the opposite in
> terms: "`to_stop` is ADVISORY DISTANCE, never a trigger: only the broker
> fills stops" — a rule added after the logged GS 2026-05-18 loss, where the
> desk sold at +0.4%-to-stop "before the gap", no gap came, and the stock
> ran. `:61` then lists the only classes of new information an exit may rest
> on, and proximity is not among them. So the exam marked a model DOWN for
> obeying the desk and UP for breaking it: `google/gemini-2.5-flash-lite`
> scored 1.00 twice by answering REDUCE and naming "close to its hard stop"
> as the reason, while `google/gemini-3.5-flash-lite` scored 0.65 three times
> for answering HOLD and restating the rule. **Every committed
> `midday_exit` score in `results/` was earned against that inverted rubric
> and certifies nothing about this seat** — including the `quality_min 1.0`
> the incumbent's gate currently reads. The rebuilt scenario grades
> `holds_on_stop_proximity_alone` (0.35) instead, on the recorded 2026-09-02
> book, and is QUARANTINED rather than blocked: its inputs are the live
> position book and the upstream seats' outputs, which is desk-recorded data
> that `fixture_policy` refuses outright. It cannot be run until the owner
> sets `DESK_DATA_TRUSTED_FROM` (with the `docs/INCIDENT_HISTORY.md`
> justification that `tests/test_fixture_policy.py` requires) AND the
> agent-output ban is reconciled with the fact that this seat's real
> production inputs ARE other seats' outputs. No synthetic substitute exists:
> a position book cannot be fetched from SEC or yfinance.

> **STALE 2026-09-20.** `risk_drawdown_discipline` grades against a rule that
> no longer exists: the account-level loss alarms, including the
> `in_drawdown` BUY-halving, were retired in full on the owner's instruction
> (`docs/INCIDENT_HISTORY.md`, board item 32). The scenario is `default=False`
> and does not run, so nothing is being graded wrongly today — but it must be
> rewritten or deleted before it is ever enabled.

### Scoping a re-run to one seat

A prompt or input change to a single agent invalidates that agent's rows
and nothing else. Re-run the seat rather than the sweep:

```bash
.venv/bin/python ops/model_policy/benchmark_models.py --from-onecli \
  --models google/gemini-2.5-flash-lite deepseek/deepseek-v4-pro-0813 \
  --scenario risk_rr_breach --scenario risk_drawdown_discipline \
  --repeats 3 --budget-usd <USD> --out results/rm-rerun-<date>.json
```

`tests/test_model_routing_policy.py` reads every file in `results/` and
requires each decision seat's configured model to carry a `quality_min` of
1.00 at its own scenario, so a seat whose evidence has gone stale fails the
suite rather than drifting unnoticed.

`risk_drawdown_discipline` is `default=False` — it informs the risk seat
only and would otherwise be paid for on every candidate in a full sweep.

### `pm_selection` — the one scenario built from real data

Every other scenario asks whether a model applies a rule. `pm_selection`
asks a different question: **given a real day's evidence, does it pick the
candidates the evidence supports, or the tickers it already knows?**

```bash
.venv/bin/python ops/model_policy/benchmark_models.py --from-onecli \
  --scenario pm_selection --models openai/gpt-5.5 --repeats 2 \
  --budget-usd <USD> --out results/pm-selection-<date>.json
```

`pm_production_scale` cannot answer that, and it was never meant to:
`_PM_PRODUCTION_ANALYSES` loops over a ticker list and hands every one of
its 30 candidates the same `buy`/`medium` analysis at entry 100 / stop 94 /
target 112, so any five of them score identically. Its checks count targets
and never record which symbols were chosen, so the committed results cannot
tell a considered selection from an arbitrary one. That scenario is a valid
robustness test and is left untouched; this is the missing measurement, not
a replacement.

The fixture (`fixtures/run_bba4d4f3_pm_input.json`) is a verbatim pull of
production run `run-bba4d4f3` (2026-09-02) from the read-only Mission Control
API: 64 technical reads — the API's own rows, 63 of them carrying
`computed_levels` — 5 held positions, the session's macro, news and earnings
evidence, and the real BUY-eligibility universe, with account, memory and
evening insights read out of the run's recorded PM prompt. Nothing is rounded
or tidied, and the candidates are in the run's own presentation order rather
than sorted. Its `_provenance.fidelity` block is measured, not asserted:
rendered through today's `build_user_message`, 12 of 22 shared sections are
byte-identical to the recorded prompt and all 64 technical rows match it on
rating, conviction, entry, stop and target. It lists what is absent (PMFacts,
portfolio heat, company profiles, proposal conversion — all computed live;
the smart-money findings, which would have required inventing SEC source
URLs) and what that changes for a model.

**Why this day (2026-09-14, `docs/WORK.md` item 72).** The scenario used to
replay `run-64290730` (2026-09-01), where none of the 59 rows carried
computed levels. The live admission gate reads a STRUCTURAL reward:risk built
from those levels, so on that day it could measure no name at all — and,
measured, the live gate handed those ratios admits 12 names where this grader
admitted 25. On `run-bba4d4f3` all 34 actionable candidates (14 breakout /
20 range) have a computable structural ratio, and the live gate and the
grader admit the identical 25. `tests/test_pm_selection_scenario.py` pins
both facts so the scenario cannot slide back to a level-less day. The old
file stays only because three older audits pin their numbers to it.

The admitted set comes from `deterministic_selection.evaluate`, the desk's
own current admission rules: 25 of the 64 read names, exactly one of them a
short (FLNC). Ten bearish names were on offer; the other nine — including
UNH, the one short the live PM proposed — are refused on net evidence (a record
of the pre-archetype rules: the net-vote admission was retired 2026-10-19). The
live PM proposed nine targets and the risk seat approved them, yet the funnel
recorded zero proposed orders and zero fills; why is not established by the
pull, and nothing here grades against the live output. The grader stopped
using the retired `analyst reward/risk >= 1.5` floor on 2026-09-14 (PM-gate
item 8): `rr_floor_discipline` and the old majority bar were deleted, and
`familiarity_bias` is a weight-0 diagnostic because AAPL, MSFT and NVDA all
have a read and are all admitted by the desk's own rules.

**It measures quality of selection. It does not measure profitability** —
nobody knows which of these picks would have made money, and no check here
pretends otherwise. `familiarity_bias` reports the share of picks that are
famous as a number on every run, passing or failing; read it as a rate
across models and repeats, not as a verdict on one run — and note that it
scores nothing, deliberately.

`default=False`, opt-in like `pm_production_scale`. Rendered through today's
`build_user_message` the prompt is 87,247 characters (measured 2026-09-14;
the live session's recorded prompt was 214,529, most of the gap being
renderer changes since that run). The old `run-64290730` fixture renders at
87,119 characters through the same code, so the earlier 194,173 figure here
was a 2026-09-01 measurement that the renderer has since overtaken. Token
count for the new fixture has not been measured.

`tests/test_pm_selection_scenario.py` drives the grader with hand-built
decisions and keeps it honest without spending anything.

Useful flags:

```bash
# one model, one scenario — the fast loop when adding a scenario
... --models qwen/qwen3.7-flash --scenario risk_rr_breach --budget-usd <USD>

# add a scenario without re-paying for the whole sweep
... --scenario midday_exit --budget-usd <USD> --out results/sweep-midday.json
... --report results/sweep.json results/sweep-midday.json --merge-out results/merged.json
```

Cost: a full sweep is `models x scenarios x repeats` real LLM calls. The
2026-08-12 slate (12 models, 6 scenarios, 2 repeats) came to a few dollars,
almost all of it the `openai/gpt-5.5` baseline rows.

`--repeats 2` is the minimum worth trusting. LLM output varies run to run,
and the aggregate reports `quality_min` alongside the mean precisely so a
model that averages well by alternating between excellent and unusable is
visible as such.

### Credentials

Nothing here holds a real key. The benchmark sends the same
`placeholder-managed-by-onecli` stand-in the commissioning preflight uses
and lets the OneCLI gateway substitute the real value server-side, so a
results file or a benchmark log cannot contain a credential.
`--from-onecli` resolves the gateway wiring the way
`ops/commissioning/verify_commissioning.py` does; on the runtime account
the process environment already carries it and the flag is unnecessary.

### Adding a scenario

Add a `Scenario` to `SCENARIOS` in `scenarios.py` with an `invoke` that
calls the agent's real public entry point and a `grade` that returns
weighted `Check`s. Two rules learned the hard way:

- **Calibrate before trusting it.** Run the new scenario against the
  baseline AND against a deliberately weak model. If both score 1.00 the
  scenario measures nothing; if both score 0.00 the bug is in the grader.
  Both happened on the first pass here.
- **Grade the instruction, not your own preference.** An early check
  required `thesis_invalid_if` on every symbol and marked both models
  down — but `config/prompts/tech_analyst.md` explicitly says to leave it
  empty on `neutral`. The models were right and the rubric was wrong.

## Pricing provenance

```bash
.venv/bin/python ops/model_policy/verify_pricing.py
```

`src/cost_table.py:_PRICING_OPENROUTER` is hand-copied from OpenRouter's
catalog, and a hand-copied rate goes stale silently — producing a cost
report that looks confident and is wrong, which is worse than `$?.??`.
This re-reads the live catalog and exits non-zero on any drift.

Why a separate table at all, rather than LiteLLM: LiteLLM keys models by
bare vendor id (`gpt-5.5`), so an OpenRouter id (`openai/gpt-5.5`) missed
every lookup — under the commissioned baseline **every agent call logged
`$?.??`**, i.e. the deployment had no cost telemetry. And where LiteLLM
does carry a match, its rate is the vendor's *direct* API price, which is
not what routed traffic costs.
