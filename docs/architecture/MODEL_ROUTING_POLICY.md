# Model Routing Policy — Accepted Contract

> **OWNER DECISION 2026-09-15 — NO MORE TEST-ENVIRONMENT WORK.** The owner is no longer interested in model tests, practice days or benchmark rigs: too many flaws, too much setup, too many resources for too little. The paper desk runs LIVE and problems are fixed from what live sessions show. Do not propose, build, repair or run tests/benchmarks/practice days unless the owner explicitly asks. Current seats: PM `openai/gpt-5.5`; analysts `gemini-3.5-flash-lite` (Google direct, free); risk manager and position reviewer unchanged. Desk switched back on 2026-09-15.


Status: **accepted — externally reviewed and merged via PR #30 on 2026-08-14**.

> **2026-08-31 update:** the eight `google/gemini-2.5-flash-lite` seats below
> moved off OpenRouter onto Google AI Studio DIRECT (`provider: google`,
> bare id `gemini-3.5-flash-lite`, free tier) as PRIMARY, with OpenRouter +
> the same 3.5 model as cross-provider BACKUP — see `docs/WORK.md` "NEXT UP"
> for the ratified decision and its measured facts. This was a FORCED
> migration (Google refuses 2.5 to new keys outright), not a re-benchmark:
> the evidence tables below remain the real, unmodified measurements behind
> the original 2.5 selection, and no equivalent measurement exists yet for
> 3.5 at any seat. Portfolio Manager and Risk Manager were deliberately left
> unchanged (see docs/WORK.md and `config/settings.yaml`) and still run
> `provider: openrouter`.

Supersedes the commissioning baseline recorded in `docs/STATE.md` ("every
agent on `openai/gpt-5.5`"), which was a deliberate single-variable posture
for proving the OpenRouter transport, never the intended production policy.

Read alongside `MODEL_PROVIDER_ARCHITECTURE.md`, which owns the seam this
policy is expressed through and is unchanged by it.

## The policy (as accepted 2026-08-14 — see the 2026-08-31 update above for what changed since)

Every agent seat ran `provider: openrouter`. The Portfolio Manager runs
**`openai/gpt-5.5`**, the AI Risk Manager runs
**`qwen/qwen3-235b-a22b-2507`**, and the remaining seats ran
**`google/gemini-2.5-flash-lite`** (now `provider: google` /
`gemini-3.5-flash-lite` — see the update note above).

| Seat | Model | Basis |
|---|---|---|
| `tech_analyst` | `google/gemini-2.5-flash-lite` | measured — `tech_batch`, `tech_batch_full` |
| `news_analyst` | `google/gemini-2.5-flash-lite` | measured — `news_intel` |
| `macro_analyst` | `google/gemini-2.5-flash-lite` | measured — `macro_stress` |
| `portfolio_manager` | **`openai/gpt-5.5`** | measured 2026-08-25 — `pm_constrained`, `pm_production_scale` |
| `risk_manager` | **`qwen/qwen3-235b-a22b-2507`** | measured — `risk_rr_breach`, `risk_drawdown_discipline`; **held apart from PM** |
| `position_reviewer` | `google/gemini-2.5-flash-lite` | measured — `midday_exit` |
| `earnings_analyst` | `google/gemini-2.5-flash-lite` | **by analogy** to `news_analyst` |
| `evening_analyst` | `google/gemini-2.5-flash-lite` | **by analogy** to `portfolio_manager` |
| `meta_reflector` | `google/gemini-2.5-flash-lite` | **by analogy** to `portfolio_manager` |

### Cost is not a quality signal — and was never used as one

Worth stating because a test in this repo briefly implied otherwise (an
input-price >= $0.10/M floor on the decision seats, removed at PR #30
review). Nothing in this policy infers quality from price. Every **decision
seat** assignment traces to a graded run at that seat, and the invariant the
tests now enforce is exactly that: a decision seat's model must carry a
committed `quality_min` of 1.00 at its own scenario. The three seats marked
**by analogy** above are explicit exceptions and are not represented as
directly measured. The price floor would have failed this policy — both
routed models sit at or below it — while passing any expensive model nobody
had measured.

### PM recovery re-measurement (2026-08-25)

The original PM scenario did not enforce grounded specialist provenance or
actual holdings. Once those deterministic checks were added, the configured
Gemini PM scored **0.00 on both runs**: it omitted provenance and proposed
exits for names not held. GPT-5.6 Luna was not stable under the final stricter
contract. GPT-5.5 scored **1.00/1.00 on two `pm_constrained` runs and two
`pm_production_scale` runs** (30 candidates, 15 holdings, full memory context;
43–109s, including bounded provenance-only repairs). Raw final evidence is
committed in `ops/model_policy/results/zz-pm-luna-disqualified-final-2026-08-25.json`
and `ops/model_policy/results/zz-pm-gpt55-qualified-final-2026-08-25.json`.

This changes only the PM seat. Risk routing and deterministic Python/broker
authority are unchanged.

### Shared specialist model; decision seats diverge where measured

The tranche reserves a different model for a seat only where current
measurements demonstrate the benefit. The PM now does; the other seat
assignments retain their prior evidence.
Three unmeasured seats are assigned by analogy and remain an explicit known
limitation rather than evidence of equivalence.

`risk_manager` retains its independently measured Qwen route. See
"Why `risk_manager` is not on PM's model" below.

The per-seat *structure* is what made that a one-line config edit rather
than a plumbing change, and it is what `verify_commissioning.py` pins
against.

## Shape

Explicit per-agent model mapping in `config/settings.yaml`. Nothing else.

- No router, no scoring service, no dynamic selection, no new dependency.
  The mapping is YAML read by machinery that already existed.
- No silent fallback — see "Failure behaviour", where the decision *not* to
  add one is part of this contract.
- Attribution needed no work: `AgentResult` already carries
  `requested_model` / `requested_provider` / `model` / `actual_provider` /
  `used_fallback`, and every `insert_agent_log` call site persists the
  model that actually answered. The Stage 1 seam was already built for
  per-agent divergence.
- `provider` stays explicit on every seat. OpenRouter's `vendor/model` ids
  collide with native prefixes, so `resolve_provider()` cannot infer them;
  an unset provider routes to Anthropic and fails on the session's first
  call.

## Evidence

`ops/model_policy/benchmark_models.py`, 148 graded trials on 2026-08-12:
12 models x 6 scenarios x 2 repeats, plus production-scale tech and PM cases and
two corrective re-runs, driven through the **real** agent classes with the
**real** prompts and graded by deterministic assertions. Raw results in
`ops/model_policy/results/`; reproduce the table with:

```
python ops/model_policy/benchmark_models.py --report \
  ops/model_policy/results/sweep-a.json \
  ops/model_policy/results/sweep-b.json \
  ops/model_policy/results/rerun-capfix-flash.json \
  ops/model_policy/results/rerun-capfix-pro.json \
  ops/model_policy/results/sweep-tech-full.json \
  ops/model_policy/results/rm-rerun-2026-08-14.json
```

Quality is the weighted mean of a scenario's graded checks (0–1).

The table below is the **whole-sweep** aggregate across six roles. Read it
as what it is: a summary of general fitness, useful for choosing one model
for the originally shared seats. It is **not** evidence about any individual seat, and
treating it as such is the error PR #30's review caught — see "Why
`risk_manager` is not on PM's model".

| model | mean | worst run | $/sweep |
|---|---|---|---|
| **`google/gemini-2.5-flash-lite`** | **1.00** | **1.00** | **$0.0152** † |
| `deepseek/deepseek-v4-pro-0813` | 1.00 | 1.00 | $0.0646 |
| `openai/gpt-5.5` *(baseline)* | 1.00 | 1.00 | $0.6547 |
| `z-ai/glm-5.2` | 0.97 | 0.65 | $0.0605 |
| `qwen/qwen3.7-max` | 0.97 | 0.65 | $0.2029 |
| `qwen/qwen3-235b-a22b-2507` | 0.95 | 0.85 | $0.0064 |
| `deepseek/deepseek-v4-flash-0731` | 0.91 | 0.45 | $0.0193 † |
| `qwen/qwen3.7-plus` | 0.85 | 0.20 | $0.0760 |
| `openai/gpt-5.6-luna` | 0.80 | 0.00 | $0.0102 |
| `minimax/minimax-m3` | 0.78 | 0.00 | $0.0492 |
| `qwen/qwen3.7-flash` | 0.74 | 0.00 | $0.0055 |
| `openai/gpt-5-nano` | 0.63 | 0.00 | $0.0242 |

† These two rows include the expensive 25-symbol `tech_batch_full`
scenario, which the other ten did not run. On the common six scenarios
`gemini-2.5-flash-lite` cost **$0.0079** — the figure comparable to the
baseline's $0.6547, i.e. **83x cheaper**.

Three models scored a perfect mean **and** a perfect worst run across all
six roles. For the eight seats that share a model, the choice between them
was made on latency and cost, both of which favour the selected model
decisively — see the next section. (The RM seat was decided separately and
on its own measurements; a model can be imperfect across six roles and
flawless at one.)

**`worst run` is the column that matters.** A mean hides the failure mode
that actually hurts: a model that alternates between excellent and
unparseable averages respectably and silences a session every other day.
Four of twelve candidates have a 0.00 in there — a run that produced
nothing the pipeline could use.

### Why not `deepseek-v4-pro-0813`, which also scored 1.00/1.00

Latency, and cost — **at the seats that issue many calls**. It answered in
104–246s per call across the scenarios, against 3–22s for the selected
model. `tech_analyst` alone issues five sequential calls per morning
against a 1200s session kill (see below), and PM and RM still have to run
after them. It is also 4x more expensive on the common scenarios.

That reasoning does **not** transfer to `risk_manager`, which issues
exactly one call per session — see the next section, where it was
re-evaluated on its own terms.

## Why `risk_manager` is not on PM's model

### The claim this replaces was wrong

Earlier revisions of this document said splitting PM and RM "would trade
measured quality for hypothetical independence — every alternative scored
worse at the RM seat". **That was false**, and PR #30's review caught it.
It read the whole-sweep aggregate as if it were the seat's result. The
committed 2026-08-12 raw results say otherwise: at `risk_rr_breach`,
**10 of 12 candidates scored 1.00 on both runs**. Only `gpt-5-nano`
(0.00/0.00) and `gpt-5.6-luna` (0.00/1.00) failed. There was never a
quality argument against splitting; there was a quality argument against
those two models.

A model that is mediocre *across six roles* can be perfect at *one*, and
the RM seat only ever runs RM. Aggregating across seats and then reasoning
about a single seat is the error.

### Re-measured on the current branch

Those results also predated the 2026-08-13 prompt cleanup and this branch's
F5/F6 changes to RM's prompt and inputs, so they were stale as well as
misread. Re-run 2026-08-14 — 5 models x 2 RM scenarios x 3 repeats, 30
trials, $0.157:

| model | quality mean | worst run | latency | cost / 2 calls | independent of PM |
|---|---|---|---|---|---|
| **`qwen/qwen3-235b-a22b-2507`** | **1.00** | **1.00** | **10.4s** | **$0.00162** | **yes** |
| `google/gemini-2.5-flash-lite` | 1.00 | 1.00 | 2.8s | $0.00163 | no — PM model at measurement time |
| `z-ai/glm-5.2` | 1.00 | 1.00 | 61.4s | $0.02355 | yes |
| `deepseek/deepseek-v4-pro-0813` | 1.00 | 1.00 | 113.0s | $0.02035 | yes |
| `openai/gpt-5-nano` *(control)* | 0.50 | 0.00 | 42.0s | $0.00517 | — |

```
python ops/model_policy/benchmark_models.py --report \
  ops/model_policy/results/rm-rerun-2026-08-14.json
```

`gpt-5-nano` was included deliberately as a weak control, and it failed the
same way it failed in August — omitting `RiskVerdict.reasoning`, so
`review()` returns `None` and the session has no risk verdict at all. A
four-way 1.00 tie is only meaningful if the scenarios can still produce a
0.00, and on the current branch they can.

### How the tie was broken

Quality first, then independence, then latency and cost:

1. **Quality** — four-way tie at 1.00 mean and 1.00 worst run. No candidate
   is better at this seat, so nothing is given up by choosing on another
   axis.
2. **Independence** — eliminates `gemini-2.5-flash-lite`, which is PM's
   model. The decision chain exists so that RM checks PM; running both on
   one model means a flaw in PM's reasoning is one the gate is
   systematically likely to reproduce. Three candidates remain.
3. **Latency and cost** — `qwen3-235b-a22b-2507` wins decisively among
   them: 10.4s against 61.4s and 113.0s, at roughly 1/14th the cost.

**Independence here is effectively free.** $0.00162 versus gemini's
$0.00163 per two RM calls — the input rate is actually *lower* ($0.09/M vs
$0.10/M) — and +7.6s on a session bounded at 1200s in which this seat makes
exactly one call. The projected monthly total is unchanged at the reported
precision.

### What this is not

Not a general promotion of `qwen3-235b-a22b-2507`. It scored 0.95 mean /
0.85 worst across the full six-scenario sweep and is measured **only at
this seat**. Moving it anywhere else needs its own measurement, which
`tests/test_model_routing_policy.py` now enforces for every decision seat.

Not a claim that RM will catch more. Independence removes a correlated
failure mode; it does not demonstrate a better verdict. That is a
paper-trading question — see `DECISION_CHAIN_AUDIT.md` (F5).

Not a change to what RM may DO. Veto authority, the modification hierarchy,
and every deterministic risk and execution semantic are untouched.

### What the failures actually were

Not stylistic. `gpt-5-nano` omitted `RiskVerdict.reasoning` entirely, on
both runs, so the AI Risk Manager returns `None`. `qwen3.7-flash` emitted
`market_sentiment: "neutral_to_defensive"` — plausible prose, outside the
`Literal`, and the whole news report is discarded before PM sees it.
`minimax-m3` exceeded the trial deadline on `pm_constrained`.

This is why the harness drives the real agent classes. Every one of these
survives a "does it write good JSON" review and fails in production.

### Latency decided `tech_analyst`, not quality or cost

Sessions are wrapped in `timeout --kill-after=30 1200`
(`scripts/run_if_et_window.sh:225`). `tech_analyst` issues **five
sequential** calls per morning (101 symbols at `_CHUNK_SIZE = 25`), and
`pipeline_stages.py` fans macro/news/tech/earnings across four threads —
so that chain is the morning's longest pole, with PM and RM still to run
after it.

`tech_batch_full` measures one real 25-symbol chunk:

| model | quality | cost/chunk | latency/chunk | x5 chunks | fits 1200s? |
|---|---|---|---|---|---|
| `google/gemini-2.5-flash-lite` | 1.00 | $0.0072 | **22s** | ~110s | yes |
| `deepseek/deepseek-v4-flash-0731` | 1.00 | $0.0084 | **294s**, then a >420s timeout | ~1470s | **no** |
| `openai/gpt-5.5` | not measurable — see below | | | | |

Both produce perfect output at production scale. They differ by 13x in
latency, and that alone settles the seat. The 3-symbol `tech_batch` had
`deepseek-v4-flash` at parity; only the real chunk size exposed it.

## Expected cost reduction

`python ops/model_policy/project_session_cost.py` (re-derivable, and
explicit about which inputs are measured and which are structural):

| | baseline (all `gpt-5.5`) | policy | cut |
|---|---|---|---|
| per trading day | $3.4334 | $0.2707 | 92.1% |
| per month (21 days) | **$72.10** | **$5.68** | **12.7x cheaper** |

`tech_analyst` is over half of it: five calls a day at 33,328 input tokens

> **Stale as of 2026-08-27.** The Tech Analyst prompt grew when structural
> levels (`src/data/levels.py`) and market context (`src/data/context.py`) were
> added and the raw-bar window went from 20 to 40 sessions. Measured on real
> five-year data, the per-symbol payload rose from roughly 480 to roughly 1,030
> tokens — about 2.2x — so a 30-symbol call is now in the region of 50,000
> input tokens rather than 33,328. At the gemini-2.5-flash-lite input rate that
> is approximately $0.005 per call instead of $0.003: immaterial against the
> $1.50 daily circuit limit. The figure above has NOT been re-derived with
> `ops/model_policy/project_session_cost.py`; the numbers in this note are an
> estimate from measured block sizes, not a benchmark run.
each, measured at real chunk size rather than extrapolated.

Treat the ratio as the load-bearing number. It is dominated by published
per-token rates, which are exact, and by a token profile applied
identically to both sides. The absolute dollars carry a real error bar:
QAMC has run no live sessions, so there is no `agent_logs` history to
average, and three seats' token profiles are structural estimates.

## An operational fault this exposed

**At the time of the commissioning-baseline check, QAMC could not have
completed a single `gpt-5.5` agent call at the configured ceiling.**

OpenRouter pre-authorizes worst-case output spend before starting a
request. At `max_tokens: 128000`, `gpt-5.5` reserves
`128000 x $30/M = $3.84` per call. The account then held **$2.04**
(`/api/v1/credits`: $10 granted, $7.96 used). Every call therefore returned
HTTP 402 — which `_is_retryable` correctly classifies as non-retryable, so
`_execute` failed immediately, and with no Anthropic key there was no
failover. The first agent of the first session would have failed closed.

Commissioning did not catch this because the preflight calls with
`max_tokens=512`, reserving about a cent.

The account was subsequently topped up; this is **not a current credit
blocker**. `docs/WORK.md` records the latest observed balance during the
review cycle. The finding remains relevant because it explains why the
baseline was operationally unusable at that moment and why preflight must
not be mistaken for affordability at production token ceilings.

The policy also reduces the reservation by model. Same 128k ceiling:

| model | reserved per call |
|---|---|
| `openai/gpt-5.5` | $3.8400 |
| `google/gemini-2.5-flash-lite` | $0.0512 |

That is a 75x reduction in credit reserved per call, independent of tokens
actually spent.

## Failure behaviour — and why no fallback was added

`BaseAgent._execute` retries with jittered backoff under a 480s deadline,
then attempts ONE cross-provider failover to Anthropic
(`_FALLBACK_MODEL = claude-opus-4-7`) **only if an Anthropic key exists**.
QAMC holds four credentials in OneCLI — OpenRouter, Alpaca key/secret,
FRED — and no Anthropic key. So the failover is disabled and the primary
error is re-raised.

**That is correct, and this tranche deliberately leaves it alone.** The
authorization permitted bounded escalation/fallback; adding one was
rejected because:

- a same-provider fallback answers an OpenRouter outage with another
  OpenRouter call — no independence, no benefit;
- a cross-model fallback would let a session's decisions come from a model
  the policy never selected for that seat, which is exactly the
  "unrecorded model choice" the authorization forbids;
- failing closed is the accepted deterministic-safety posture, and cost
  optimization is not a reason to soften it.

If continuity through an OpenRouter outage is wanted later, the honest
form is a second *provider*, not a second model — a separate architectural
decision with its own credential and review.

## Cost telemetry (fixed as part of this tranche)

The commissioned baseline had **no working cost telemetry**.
`src/cost_table.py` resolved prices from LiteLLM, which keys models by bare
vendor id (`gpt-5.5`); the configured OpenRouter id (`openai/gpt-5.5`)
matched nothing, `estimate_cost` returned `None`, every call persisted
`cost_usd = NULL`, and `notifier._session_cost_line` renders exactly
`"cost: $?.?? (N calls — see cost_table.py)"` in that state — against
`docs/OUTCOME.md`'s requirement that the operator see cost per call.

Three changes, all inside the existing pricing module:

1. `_PRICING_OPENROUTER` — OpenRouter's own rate for every model the policy
   routes to, so cost resolves offline and never depends on a mid-session
   network call.
2. An on-demand OpenRouter catalog resolver for ids outside that table.
   Cached on the same 24h discipline; a stale entry loses to a live fetch
   and survives only as a logged last resort.
3. `ops/model_policy/verify_pricing.py`, which re-reads the live catalog and
   exits non-zero on drift — a hand-copied rate that goes stale silently
   produces a confident wrong number, worse than `$?.??`.

LiteLLM remains the source for native vendor ids. It is not used for
OpenRouter-routed traffic, because its rates are the vendor's *direct*
prices.

## What holds this in place

| Check | Guards |
|---|---|
| `tests/test_model_routing_policy.py` | every seat explicit, every model priceable offline, **every decision seat's model carries a committed `quality_min` of 1.00 at its own scenario**, the risk seat additionally measured on `risk_drawdown_discipline`, policy materially cheaper than baseline, fail-closed with no model substitution |
| `verify_commissioning.py` (`config`) | the DEPLOYED config matches the reviewed per-seat map — the expected map is a separate copy on purpose, so editing `settings.yaml` on the runtime host fails the check |
| `verify_commissioning.py` (`preflight`) | every distinct policy model is in the catalog AND completes a real call; WARNs if OpenRouter serves a different model than requested |
| `ops/model_policy/verify_pricing.py` | pinned rates still match the live catalog |
| `ops/model_policy/benchmark_models.py` | re-derives the whole decision from scratch |

## Known limitations

1. **Shared specialist concentration.** Seven of nine seats run one model.
   `portfolio_manager` and `risk_manager` are independently measured routes, which
   addresses the case that mattered most — the gate sharing the reviewed
   party's blind spots — but the specialist seats still fail together if
   that model degrades or is withdrawn. There is no fallback, deliberately;
   the posture is fail-closed, not fail-over.

   The behavioural half of the same problem was handled separately and does
   not depend on the model split: RM reads the primary evidence before PM's
   narrative and is told that PM pre-calibrates against RM's own past
   verdicts. See `DECISION_CHAIN_AUDIT.md` (F5).

   What the split does **not** establish: that RM now catches more. It
   removes a correlated failure mode. Whether that changes verdicts is a
   paper-trading question, and the observable is the `reason_category`
   distribution.
2. **Three seats are assigned by analogy**, not measurement:
   `earnings_analyst`, `evening_analyst`, `meta_reflector`. Their nearest
   measured analogues both scored 1.00/1.00, but that is an inference.
3. **The sample is thin.** 1.00/1.00 means "no failure observed", not
   "cannot fail" — 12 runs per model in the August sweep, 6 per model in
   the RM re-run. That is thinnest exactly where it matters most: the RM
   seat's four-way tie is four models that each went 6-for-6, and a tie at
   n=6 could hide a real ordering. It is enough to establish that no
   candidate is *clearly* better and therefore that nothing measurable was
   given up by choosing on independence; it is not enough to prove the four
   are equivalent. `--repeats` deepens it when a decision needs more.
4. **The specialist/review seats other than `portfolio_manager` and
   `risk_manager` were not re-measured after the
   2026-08-13 prompt cleanup and this branch's prompt edits.** Their rows
   come from the August sweep against slightly different prompt text. The
   PM and RM seats were re-run because their prompts/inputs changed materially
   and because a decision turned on them; the specialist seats changed less and
   no decision turns on them, so re-running the full sweep was not worth
   the spend. This is a real gap, and it is the first thing to close if a
   specialist seat starts behaving oddly in paper trading.
5. **`gpt-5.5`'s production-scale tech latency was never measured** — the
   402 above made it unrunnable at 128k `max_tokens`. Its baseline column
   comes from the 3-symbol scenario.
6. **Scenarios are synthetic**, chosen so the correct answer follows from
   arithmetic the prompt already states. They test rule application, not
   market judgement, and no benchmark of this kind predicts P&L.
7. **A benchmark-local `max_tokens` cap distorted 3 trials** before it was
   found and fixed; scenarios now read the production value from
   `settings.yaml` (see `scenarios.py:production_max_tokens`). The affected
   pairs were re-run and fold in via the report merge, which supersedes an
   earlier file's pair with a later one. The correction mattered:
   `deepseek-v4-flash-0731` went 0.00 → 1.00 at the risk seat and
   `deepseek-v4-pro-0813` went 0.50 → 1.00 at the midday seat, the latter
   moving it into the perfect-score group. Neither changed the selection,
   but a table that had kept the original numbers would have been wrong
   about both.
8. **`position_reviewer`'s `midday_exit` scenario ties five candidates at
   1.00 quality** — `gemini-2.5-flash-lite`, `gpt-5.5`, `deepseek-v4-pro-0813`,
   `qwen3.7-flash` and `qwen3-235b-a22b-2507` — and so does not discriminate
   between them. `QAMC_REMEDIATION_SPEC.md` §3.5 previously stated
   `gemini-2.5-flash-lite` as "the weakest model in the stack" at this seat
   as though that were a quality finding; it is corrected there. The
   measurement above supports "passed", not "best" or "worst".

---

## Model-selection strategy (2026-08-27)

Where the money actually goes, measured over the 10 days to 2026-08-27:
**$6.73 total, $5.84 of it the Portfolio Manager — 87%.** Every other seat
combined is 13%. **This window is contaminated and is not a clean baseline**:
it includes several runaway looping incidents that burned tokens — the
reason the LLM cost circuit breaker was added — so the 87% figure is
inflated by an unknown amount and the allocation conclusion below (fix the
research desk's share of spend) is not yet established. A clean baseline
needs to be re-measured once the current tranche deploys. A production PM
run costs about **$0.22** (not the $0.46 the `pm_production_scale` benchmark
shows; that scenario runs 30 candidates and 15 holdings, heavier than a real
session). That per-run figure is not in question — only the share-of-total
conclusion drawn from the contaminated window is.

### Keep OpenRouter. Change the models.

OpenRouter passes provider inference prices through without markup; the cost
is a ~5.5% fee on credit purchases. In exchange: one API, per-seat model
switching, provider failover, and usage telemetry. Building direct
Qwen/DeepSeek/Z.ai integrations to save ~5% would be false economy at this
scale, and a self-hosted gateway makes no sense until model spend is in the
thousands per month. The cheap models worth testing are already reachable
through it — usually a one-line model-id change, which is exactly what the
per-seat structure was built for.

**Do NOT use OpenRouter's Auto Router for the decision chain.** It selects
models dynamically, which destroys the property this policy exists to
guarantee: knowing which exact model made which trading decision.

### 1. GPT-5.5 Flex — DONE (2026-08-27)

OpenRouter lists OpenAI Flex as a GPT-5.5 provider at exactly half price
($2.50/$15 input/output vs $5/$30). It is **the same model**, not a cheaper
substitute, so there is no quality question to answer: PM cost halves to
~$0.11/run. This decision does not depend on the disputed 87% figure above —
it holds regardless of the PM's exact share of spend, because it is the
identical model at half the price with no quality trade-off. The only
exposure is added latency, and the session wrapper already enforces a 1200s
kill.

**Implemented** as `llm.<agent>_provider_order` in `config/settings.yaml`,
set to `["openai/flex"]` on `portfolio_manager` and unset everywhere else.
Verified against the live catalog on 2026-08-27: all three endpoints
(`openai/flex`, `openai`, `azure`) serve the identical
`openai/gpt-5.5-20260423`, and flex reported 100% uptime over the prior day.

Three properties of the implementation are load-bearing:

- **It is an endpoint choice, not a model choice.** Nothing about which model
  answers changes, so this is outside the benchmark-and-qualify discipline the
  rest of this document describes. `LLMConfig` rejects the field on any seat
  not routed through OpenRouter, because there it would be silently ignored —
  which is how an operator comes to believe a seat is on a tier it never
  reached.
- **Fallbacks stay enabled** (`allow_fallbacks: true`, not `only`). Pinning
  `only` would fail the seat closed when the flex tier is saturated. That is
  the correct posture for a *model* substitution — an unreviewed model must
  never answer — but wrong for a price tier: the fallback endpoint serves the
  same weights, so the exposure is money, and losing a trading session to save
  $0.11 is a bad trade.
- **Cost is now taken from the provider, not the table.** Once one model id is
  served at two prices, `_PRICING_OPENROUTER` cannot be right for both — it
  over-reports on flex and under-reports on the full-price endpoint. OpenRouter
  traffic therefore sends `usage: {include: true}` and records what OpenRouter
  says it charged, falling back to the pinned estimate when no figure comes
  back. This matters beyond accuracy: the daily cost circuit spends against
  these numbers, so a 2x over-report would have starved the budget of exactly
  the headroom this change was made to free. A reported figure that diverges
  from the pinned estimate by more than 1.5x is logged — that is what a
  half-price endpoint looks like, but it also means projections built on the
  pinned table (`project_session_cost.py`, the circuit's worst-case
  reservation) no longer describe what this seat pays.

`EXPECTED_PROVIDER_ORDER` in `ops/commissioning/verify_commissioning.py` pins
the preference the same way `EXPECTED_ROUTING` pins the model: a seat that
loses it on the runtime host still runs the right model on the right provider
and looks entirely normal, while quietly costing twice as much.

### 2. Build a DISCRIMINATING PM scenario before any shootout

This is the part external analysis keeps getting wrong, and this repo has
direct evidence for it. The recommendation "require 1.00 mean and 1.00 worst
run" sounds rigorous and is not: on `midday_exit`, **five of twelve candidates
score exactly 1.00** — `gemini-2.5-flash-lite`, `gpt-5.5`,
`deepseek-v4-pro-0813`, `qwen3.7-flash` and `qwen3-235b-a22b-2507`. That is
not five equally excellent models; that is a scenario with a low ceiling.
`pm_constrained` is likely the same.

**Running a shootout against a test everybody passes is theatre.** Build a
scenario that separates candidates first — the natural material is the failure
modes this system has actually produced: a deterioration claim contradicted by
its own improving metrics, a target citing provenance that does not exist, a
macro conflict that must be adjudicated rather than logged.

#### `pm_selection` — built 2026-09-01, NOT YET RUN against any model

The scenario this section asks for now exists, from exactly the material this
section recommends: a real day the system actually produced. Since
2026-09-14 it replays production run `run-bba4d4f3` (2026-09-02) — 64
technical reads, 63 with computed levels, 34 actionable signals every one of
which has a computable structural reward:risk, and zero orders placed. It
used to replay `run-64290730`, which carried no computed levels, so the live
admission gate could measure no name on it (`docs/INCIDENT_HISTORY.md`,
2026-09-14). See `ops/model_policy/README.md` for the design and the run
command.

It measures WHICH candidates a model picks against DIFFERENTIATED evidence,
which `pm_constrained` and `pm_production_scale` cannot: `_PM_PRODUCTION_
ANALYSES` builds all 30 candidates by looping over a ticker list and handing
every one an identical `buy`/`medium` analysis at entry 100 / stop 94 /
target 112. Any five of them score the same, so what that scenario captures
when it records a choice is the model's PRIOR over tickers, not selection
skill — there is nothing in the input to be skilful about.

That distinction is the whole point of the two scenarios existing side by
side, and it is now load-bearing, because the priors turn out to differ
sharply between models. See the next section.

**No model has been benchmarked against `pm_selection` yet**, so no claim in
this document rests on it and no seat assignment changes because of it. The
grader itself is covered by `tests/test_pm_selection_scenario.py`, which
drives it with hand-built decisions and spends nothing.

It grades quality of selection. It does **not** grade profitability — nobody
has measured that for this system, and this scenario must not be cited as if
it had.

### Measured: models carry ticker priors that identical data does not override

Because `pm_production_scale` feeds 30 byte-identical candidates, whatever a
model picks there is its PRIOR. Two runs, on two different versions of the PM
prompt, record it. The universe holds 3 index ETFs (SPY/QQQ/IWM) among 30
names, so ~10% is the chance rate.

| model | index-ETF picks / total | rate | prompts covered |
| --- | --- | --- | --- |
| `openai/gpt-5.5` | **17 / 28** | **61%** | old and rewritten |
| `qwen/qwen3.7-max` | 0 / 43 | 0% | old and rewritten |
| `z-ai/glm-5.2` | 0 / 20 | 0% | old only |

Sources: `ops/model_policy/results/pm-agreement-2026-09-01.json` (16:37 UTC,
old prompt, `picks` on 15/15 trials) and the af266de gate run (21:07 UTC,
rewritten prompt). Every OTHER file in `results/` predates the `Trial.picks`
field and records only a target count, which is why a scan for this evidence
comes back looking empty — that single file is the committed exception.

**The prior survived the prompt rewrite.** `gpt-5.5` took SPY in 5 of 5 runs
on the old prompt and QQQ in 5 of 5 on the rewritten one, with IWM in 4 of 5.
It changed ticker, not habit. **Do not cite the fall in SPY frequency as
evidence that the rewrite reduced index anchoring — it did not.**

Both non-OpenAI models picked zero ETFs across 63 combined picks against ~6
expected by chance, so this is not an artefact of a small sample on either
side.

**Consequence for this document:** model choice partly determines what the
desk buys before any analysis happens. That is a seat-assignment input, not a
curiosity. But note the limit — a prior is only a defect if real evidence
fails to override it, and `pm_production_scale` cannot answer that, because
its candidates carry no evidence to override anything with. **`pm_selection`
is the scenario that can.** That is what the two exist to separate.

### 3. Then the challengers, against the strict contract

The 2026-08-25 strict PM grounding rerun covers `gpt-5.5`, `gemini-2.5-flash`
and `gpt-5.6-luna` only. **DeepSeek V4 Pro, GLM-5.3-Flash and Qwen3.8 Flash
have never been run against it** — DeepSeek's 1.00 predates the stricter
contract entirely. That gap is the actual research to do.

- **Qwen3.8 Flash** — ~$0.15/$0.47, 1M context, and **proper JSON-schema
  structured output**. For a seat whose contract is this strict, schema
  enforcement likely matters more than the last fraction of a cent.
- **GLM-5.3-Flash** — cheapest by a wide margin, but OpenRouter reports it
  supports JSON output **without enforcing JSON Schema**. Against the PM's
  provenance and grounding contract that is a plausible disqualifier, and it
  is exactly the kind of thing generic intelligence benchmarks miss.
- **DeepSeek V4 Pro 0813** — already 1.00 on the older sweep, passed over then
  for latency, not quality. Cheap to re-run.

Require the winner to shadow-run against GPT-5.5 on live paper sessions before
it is given authority. A benchmark is evidence; production agreement is proof.

### What NOT to spend on yet

SIP market data (owner declined 2026-08-27 for the paper phase — see
`docs/WORK.md`), Alpaca Elite smart routing, and a wider universe. None of
them is the binding constraint.

## 2026-09-14 analyst seat re-test

**Why.** Seat models were chosen 2026-08-12..08-31 on prompts rewritten
since; the old exams fed stale shapes and desk-recorded data.

**Rules now in force:**
- Exam fixtures = public raw facts only (SEC EDGAR, yfinance, FRED via
  OneCLI, public RSS), enforced by `ops/model_policy/fixture_policy.py`.
- Desk data refused until a reviewed trust cut-off date.
- Published OpenRouter prices decide what is worth testing; the test itself
  measures spend and picks the model; owner only rules on whether the
  winner's cost is acceptable.
- Every model runs under identical settings.

**Faults found and fixed today (all merged):**
- `smart_money` `max_tokens` 3000 truncated every answer, now 16000 (PR #411).
- Earnings prompt told models to write the UNSOURCED token into a list
  field; code discarded the whole analysis (PR #411).
- Desk sent no reasoning effort and no response format, so thinking models
  ran out of room (PR #412): reasoning effort "medium" = OpenRouter
  documented default; strict `json_schema`; same on the Google-direct route;
  free-form-map models like NewsIntelligenceReport go `strict=false` for
  all; `$ref` sibling keywords stripped. (The 2026-09-19 tech_analyst
  exception is gone: it now sends the `TechAnalystAnswer` wrapper object.)
- Answers are schema-locked, not text-repaired (2026-10-14). OpenRouter calls
  carrying a schema also send `provider.require_parameters: true`, so only
  an endpoint that honours it can serve them; a backup rung on a provider
  that is sent no schema (anything but OpenRouter and Google) is skipped for
  a seat with a `result_model`, as an absent attempt. Answers are read with
  plain `json.loads` — fence stripping, prose unwrapping, quote repair,
  fragment picking and wrong-key recovery were deleted. The tech seat turns a
  non-JSON answer into a per-symbol refusal record, re-asked once.
  NewsIntelligenceReport still goes `strict=false`: its two free-form maps
  need an answer-shape change in the news seat and its prompt.
- Benchmark could not run the live Google-direct route (PR #412 added the
  `google-direct:` prefix).
- Parallel benchmark runs sharing one DB were frozen by the cost circuit
  when one call's cost was unknown (Google free tier returns no price;
  mid-stream timeouts) — workaround used: one config copy with its own
  storage `db_path` per process. **Not fixed in code — open.**

**Results** (score % per exam: earnings / smart money / tech / news /
macro; measured test spend; 2 runs each; "-" = not completed):

| model | earnings/smart-money/tech/news/macro | spend | note |
|---|---|---|---|
| google-direct:gemini-3.5-flash-lite (current, free tier) | 100/100/88/100/100 | $0 | ≤20s per call |
| openai/gpt-5.6-luna | 100/100/88/100/100 | $0.058 | |
| google/gemini-3.5-flash-lite via OpenRouter | 100/100/65/100/100 | $0.154 | |
| meta/muse-spark-1.3 | 82/100/100/100/100 | $0.433 | |
| z-ai/glm-5.3 | 92/100/75/90/100 | $0.320 | |
| z-ai/glm-5.3-flash | 100/0/65/100/100 | $0.026 | calls up to 420s timeout |
| deepseek/deepseek-v4.1-flash | 90/50/50/50/100 | - | calls hit 420s timeout |
| deepseek/deepseek-v4-flash-0731 | 42/100/70/65/100 | - | 420s timeouts |
| qwen/qwen3.8-flash | 50/100/0/100/100 | - | 420s timeouts |
| google/gemini-2.5-flash-lite | -/-/65/-/- | - | incomplete |

Label: results are from one public-data day per exam, 2 repeats — small
sample.

**Decision (2026-09-14):** analyst seats stay on `gemini-3.5-flash-lite` via
Google direct (free); nothing tested beat it. Open question: same model
scored tech 88 direct vs 65 via OpenRouter — unexplained.

**Next step:** build the PM practice day from FRESH output of the analyst
seats (free model) on the public-data fixtures — the old PM fixtures
`run_bba4d4f3` / `run_64290730` are desk recordings and quarantined. Then PM
model test (shortlist: openai/gpt-5.5 current, anthropic/claude-opus-5,
meta/muse-spark-1.3, z-ai/glm-5.3, moonshotai/kimi-k3, z-ai/glm-5.3-flash,
qwen/qwen3.8-flash, deepseek/deepseek-v4.1-flash,
deepseek/deepseek-v4-flash-0731; check why openai/gpt-5.6-sol ($2/$10, AA
42-47) was left off). Then risk_manager and position_reviewer (they read PM
output). Then restart the paper desk. Trading timers are deliberately
paused (`scripts/systemd/paused_units.yaml`); PRs #410-#412 are merged but
NOT deployed to the production box.

**Owner's standing handoff:** the owner's private working notes for AI
sessions live on the VPS at
`/home/ubuntu/.claude/projects/-home-ubuntu/memory/` (start with
`MEMORY.md` and `qamc-resume-2026-09-13.md`).

## 2026-09-15 PM seat test on `pm_public_day` (real-day scale, public data)

- Fixture: 62 fresh analyses (gemini-3.5-flash-lite, Google direct), 49 SEC filings, 15 symbols admitted by the live gate, synthetic cash account. Gap: insider window only 7 of 365 days.
- Settings identical for all: reasoning effort medium, enforced response format, 1200s trial deadline, one cost-circuit DB per model. 2 runs each. Measured test spend $2.46 (owner cap $6).
- What the grader measures: the decision passes live grounding validation, every target is in the admitted set, and the 10-field reasoning chain is complete. It does NOT measure whether the picks are good — there is no outcome answer key.
- Passed both runs: openai/gpt-5.5 ($0.38/run, 7-8 targets), anthropic/claude-opus-5 ($0.43/run, 4-11), openai/gpt-5.6-sol ($0.10/run, 2), deepseek/deepseek-v4.1-flash ($0.01/run, 5-6, ~2 min/call), google-direct gemini-3.5-flash-lite ($0 free, 4-5, ~10s/call).
- Passed one of two: moonshotai/kimi-k3 ($0.14/run), z-ai/glm-5.3-flash ($0.007/run).
- Failed both: meta/muse-spark-1.3 and z-ai/glm-5.3 (misstated evidence counts, e.g. "claims 3/3 aligned but provenance proves 2/3" — rejected by live grounding), deepseek/deepseek-v4-flash-0731 (ungrounded targets), qwen/qwen3.8-flash (truncated at 16000 tokens).
- Open decision for the owner: rule-following narrows the PM to five reliable models; choosing among them on decision QUALITY needs outcomes (e.g. shadow-tracking picks forward), not another rules exam.

---

## `midday_exit` re-examination, 2026-10-02 — the exam is invalid, not the model

**Verdict: `gemini-3.5-flash-lite` did not review positions worse. It was
marked down for obeying the live prompt.** Cost of this investigation:
**$0.00** — no model call was made (see "why no re-run" below).

### The August scores, checked against the committed files

The summary that prompted this ("2.5 = 1.0/1.0, 3.5 = 0.325 mean / 0.0 worst,
two runs each") is **partly right and materially incomplete**:

| Model | Runs committed 2026-08-31 | Scores | Mean | Worst |
|---|---|---|---|---|
| `google/gemini-2.5-flash-lite` (OpenRouter, paid) | 4 (`merged.json`, `sweep-b.json`) | 1.00, 1.00, 1.00, 1.00 | 1.00 | 1.00 |
| `gemini-3.5-flash-lite` (Google direct, free) | 4 (`gemini35-midday-*`, `gemini35-fullsweep-*`) | 0.65, 0.65, 0.65, 0.00 | **0.4875** | 0.00 |

So "0.325 mean" is the mean of *one* of the two committed 3.5 files, not of
the evidence on disk. Across all four runs 3.5 means 0.4875. The 1.0/1.0 for
2.5 is confirmed.

The summary's other claim — that the score is "dominated by a binary
`parsed_and_grounded` check" — **is wrong for this scenario**. `midday_exit`
has no such check. `_review_grade` (`ops/model_policy/scenarios.py`) weighs:

| Check | Weight | Kind |
|---|---|---|
| `parsed` | 0.30 | schema |
| `cot_complete` | 0.10 | schema |
| `acts_on_broken_thesis` | **0.35** | judgement |
| `does_not_cut_the_winner` | 0.25 | judgement |

### Why each sub-1.0 run of 3.5 lost its point

**Three of four runs (0.65 each): lost `acts_on_broken_thesis` only.** Schema
passed, chain-of-thought passed, the winner was correctly held. The single
lost check requires AMD to be `SELL`/`REDUCE`/`TRAIL_STOP` because it sits
0.25×ATR from its stop. 3.5 answered `HOLD`, reasoning:

> "AMD's distance-to-stop is 0.4% (0.25 ATR), sitting inside the critical
> zone where broker execution is imminent; however, pre-empting the stop is
> discouraged unless a hard trigger is cited."

That is the live prompt restated. `config/prompts/position_reviewer.md`:

> "**`to_stop` is ADVISORY DISTANCE, never a trigger: only the broker fills
> stops.** 'Close to stop' or 'will gap through the stop overnight' is NOT a
> reason to SELL ahead of it — pre-empting the stop converts protection into
> a realized whipsaw (GS 2026-05-18: sold at +0.4%-to-stop 'before the gap';
> no gap came, the stock ran)."

**The grader's heaviest check rewards the exact behaviour the prompt bans,
and cites a real logged loss as the reason it is banned.** 3.5 lost 0.35 for
being right.

**The incumbent's perfect score is a score for a prompt violation.** 2.5's
winning answer:

> "AMD is underperforming significantly, is stalled, and is close to its hard
> stop. Given the risk-off macro and lack of thesis progress, a REDUCE action
> is warranted."

"Close to its hard stop" is the banned trigger, named explicitly.

**One of four runs (0.00): schema failure, not reasoning.** `parsed` was
false; the model produced 750 output tokens against a 16,000 cap, so it was
not truncated by the cap. The precise defect is **not recoverable** from the
committed record — the trial's `error` field is empty and `sample_output` is
stored clipped at 1,500 characters. What is certain is the category: it is a
formatting/validation failure, not a judgement failure. 3.5 is not alone
here — `openai/gpt-5-nano` and `deepseek-v4-pro-0813` each produced a
`parsed`-false run on this same scenario, which is the harness's known
run-to-run instability, not a property of one model.

### Why no re-run was performed

`midday_exit` is **BLOCKED** by `refusal_reason` in `scenarios.py` and the
run is refused before any call is made, on either route, paid or free:

> `REFUSED midday_exit: BLOCKED — positions, stops and entry rows are
> invented; ... The grader's main check rewards SELL/REDUCE/TRAIL_STOP on a
> position near its stop, which config/prompts/position_reviewer.md says is
> never a trigger ...`

The repo had already diagnosed this defect. Twenty repeats would have
produced twenty numbers from a grader whose heaviest check is known-wrong, at
no gain in resolution — a tighter confidence interval around an invalid
measurement. Bypassing the fixture gate to obtain them would also break the
raw-public-facts-only owner rule of 2026-09-14.

### What this does and does not establish

- **Established:** the August verdict does not support holding
  `position_reviewer` on the paid route. The quality gap it rests on is a
  grader defect in 3.5's favour.
- **Not established:** that 3.5 is *affirmatively* safe at this seat. One
  schema failure in four runs is real and its cause is unrecovered. No valid
  exam for this seat currently exists.
- **Fix size — small.** Inverting `acts_on_broken_thesis` to match the prompt
  (reward `HOLD` absent a named non-`to_stop` trigger) is a few lines. It is
  *not* sufficient on its own: the scenario stays BLOCKED for its invented
  positions and its `risk_off` macro regime, which is not a `MacroAnalysis`
  value. A valid exam needs the fixture rebuilt too.

No seat routing was changed by this investigation.

### Can the seat move pass `test_decision_seats_run_a_model_measured_at_that_seat`? No.

The gate (`tests/test_model_routing_policy.py`) requires, for
`position_reviewer|midday_exit`, a committed pair with `runs >= 2` and
`quality_min == 1.0`. **It cannot be satisfied today, and it should not be
weakened to let the seat move.** Three independent blockers:

1. **No result can be produced at all.** `midday_exit` is BLOCKED; the
   harness refuses before any call, on the free Google-direct route exactly
   as on the paid one. The block is not about cost.
2. **The key would be right, but there is nothing to key.** The gate looks up
   `f"{model}|{scenario}"` where `model` is the configured seat id —
   bare `gemini-3.5-flash-lite` on the Google-direct route. The harness keys
   results from whatever `--models` is given (`benchmark_models.py`,
   `pairs[f"{t.model}|{t.scenario}"]`), so running
   `--models gemini-3.5-flash-lite` files them under the bare id with **no
   code change needed**. The August files are keyed
   `google/gemini-3.5-flash-lite|midday_exit` because they were run over
   OpenRouter, where the vendor prefix is part of the id. That mismatch is
   real and would make the gate report "no committed benchmark result".
3. **The existing worst run is 0.00, so the gate would fail on score anyway.**
   Note the supersede rule — `_benchmark_pairs` takes later files by sorted
   filename, so `gemini35-midday-…` overrides `gemini35-fullsweep-…` and the
   effective committed pair is `runs=2, quality_min=0.0, quality_mean=0.325`.
   That is where the "0.325 / 0.0" summary comes from; it is the superseding
   file, not the whole evidence.

**Honest outcome: `gemini-3.5-flash-lite` cannot be shown to pass this seat's
gate, and the gate is right to hold.** The 0.65 runs are the grader's fault,
but the 0.00 run is a genuine schema failure on a seat that decides whether
to exit a live position, and `quality_min` exists precisely to catch "fine
most days, unparseable on the others". The unblock is to rebuild the exam —
correct `acts_on_broken_thesis` to match the prompt, replace the invented
positions, fix the `risk_off` regime — then run it free on Google direct
under the bare id. Until then the seat stays where it is on evidence, not on
preference.

*Update: the exam rebuild described above has since landed (#1160): `midday_exit` now uses a recorded book and grades the desk's own stop rule. The routing decision above is unchanged.*

## 2026-10-02 — CAPACITY (503) demotions: measured cause

Investigation of the 37 recorded demotions, asking whether the CAPACITY
refusals are the provider being full, our pacing, our prompt size, or our
credentials/tier. Source: `llm_route_events` and `agent_logs` in
`/home/ubuntu/db-backups/quant_agent_backup_20260930T130524.db` (the live
`data/quant_agent.db` in the checkout holds only 7 route rows; the backup
holds 56 route rows and 698 agent calls, 2026-08-14 to 2026-09-30). No
production behaviour was changed by this investigation.

### (a) Every 503, by provider, model, date and time

25 route rows carry a 503; **17 of them are `route_demoted`** (the rest are
the follow-on `route_switch` rows that quote the same triggering error).
All 17 demotions are the same provider and model: **Google AI Studio
DIRECT, `google/gemini-3.5-flash-lite`, route tier 1.** Zero 503s came from
OpenRouter, OpenAI or Anthropic.

The error body is identical every time:

```
InternalServerError(status=503): Error code: 503 - [{'error': {'code': 503,
 'message': 'This model is currently experiencing high demand. Spikes in
 demand are usually temporary. Please try again later.', 'status':
 'UNAVAILABLE'}}]
```

| timestamp (UTC) | seat | run |
| --- | --- | --- |
| 2026-09-24 13:33:48 | tech_analyst | run-814da6d9 |
| 2026-09-24 13:46:21 | tech_analyst | intra_check-b9c3fc60 |
| 2026-09-24 14:16:01 | tech_analyst | intra_check-80dd94c2 |
| 2026-09-24 14:18:55 | tech_analyst | intra_check-80dd94c2 |
| 2026-09-24 15:16:19 | tech_analyst | intra_check-bbfe78ab |
| 2026-09-24 15:46:34 | tech_analyst | intra_check-2043777a |
| 2026-09-24 16:16:12 | tech_analyst | intra_check-d64c00b9 |
| 2026-09-25 17:54:51 | tech_analyst | intra_check-99b6f55a |
| 2026-09-28 13:30:53 | smart_money_analyst | run-ddafe874 |
| 2026-09-28 14:15:56 | tech_analyst | intra_check-0d5f0a19 |
| 2026-09-28 14:45:57 | tech_analyst | intra_check-d41a6a1e |
| 2026-09-28 15:15:55 | tech_analyst | intra_check-4a41eb0f |
| 2026-09-28 15:45:55 | tech_analyst | intra_check-7c44c5fb |
| 2026-09-28 17:01:02 | news_analyst | midday-15761506 |
| 2026-09-28 17:46:31 | tech_analyst | intra_check-225a4448 |
| 2026-09-28 19:17:08 | tech_analyst | intra_check-e2def59b |
| 2026-09-29 14:33:34 | tech_analyst | run-dd502c6f |

Seats: tech_analyst 15, news_analyst 1, smart_money_analyst 1 — all eight
Google-primary specialist seats share one route, and the tech seat simply
calls it most.

### (b) Clustering, and our request rate against the ceiling

- **Not a burst.** The 25 rows span 16 distinct `run_id`s over four days and
  six hours of the trading day (13:00-19:00 UTC). The 2026-09-28 set lands
  at 14:15, 14:45, 15:15, 15:45, 17:46 — one per scheduled half-hourly
  `intra_check`, i.e. the FIRST call of a session, not the tail of a volley.
- **Measured rate at the moment of each refusal: 0 other logged calls in the
  preceding 60 seconds, for all 17.** Query: count of `agent_logs` rows in
  `[t-60s, t]`. Ceiling for the tier in use (owner's own AI Studio dashboard,
  2026-08-31, recorded at `_GOOGLE_TOKENS_PER_MIN` in
  `src/agents/llm_concurrency.py`): **15 RPM / 250,000 TPM / 500 RPD**, with
  our own governor set tighter at 200,000 TPM. We were at roughly 1 request
  per minute against a 15 RPM ceiling. Our pacing is not the cause.

### (c) Prompt size at the refusal

Not the cause either, and the evidence is direct: **Google successfully
served `tech_analyst` prompts up to 291,900 input tokens** (`agent_logs`,
`actual_provider = 'google'`, n=227 across all seats, median 12,180). The
prompts in flight at the 503 runs were 46k-248k input tokens — inside the
range Google served without complaint on other days. Size does not separate
the successes from the refusals. (Separately worth noting, not a 503 cause:
the tech seat's largest prompt, 326,591 tokens / 377,006 bytes, exceeds the
250k TPM window on its own and would be a 429 risk, not a 503 risk.)

### (d) Credentials / tier

The eight specialist seats run Google AI Studio **free tier**, direct,
`provider: google` in `config/settings.yaml`, key supplied as
`${GOOGLE_API_KEY}`. Google documents rate limits as **per project, not per
key**, and a limit breach as `429 RESOURCE_EXHAUSTED` — a different code from
the one we received.

### (e) The decisive discriminator

On eight of the 503 demotions the ladder's route 2 — **the same model
`gemini-3.5-flash-lite`, reached over OpenRouter instead of Google direct** —
carried the call within 10 to 25 seconds, and succeeded on **seven of the
eight**. If the cause were our prompt, our key, our tier or our pacing, the
identical prompt from the identical process over a different road would have
failed identically. It did not. (This also corrects the note in
`config/settings.yaml` that on 2026-09-22 "the same-model OpenRouter failover
went down with it" — that held for one occasion out of eight in this window,
not generally.)

### What the research says

- Google's own troubleshooting guide groups `503 UNAVAILABLE` with
  `429 RESOURCE_EXHAUSTED` as a retryable transient error and prescribes
  exponential backoff with jitter; it gives no separate cause for 503.
  <https://ai.google.dev/gemini-api/docs/troubleshooting>
- Rate limits are per project and surface as 429, not 503.
  <https://ai.google.dev/gemini-api/docs/rate-limits>
- Developers report the identical message at length, and the consistent
  finding is that it is Google's shared serving pool shedding load. Newer and
  preview model ids are hit hardest because their capacity allocation has not
  yet been scaled. **CORRECTED 2026-10-02:** this bullet previously also said
  "free, paid and enterprise keys are affected alike, and raising the tier
  does not prevent it". The first half of that is wrong — Google documents
  tier-linked sheddability and free is the most sheddable class. Only the
  second half survives. See the 2026-10-02 research section at the end of
  this file.
  <https://discuss.ai.google.dev/t/gemini-api-is-returning-a-503-unavailable-high-demand-error-continuously-for-more-than-24-hours-and-i-need-help-understanding-the-cause-and-expected-resolution-time/172445>,
  <https://discuss.ai.google.dev/t/gemini-api-returns-503-unavailable-for-all-requests-on-one-account-works-on-another-account/124079>,
  <https://kunavo.com/guides/gemini-api-model-overloaded>,
  <https://inventivehq.com/knowledge-base/gemini/gemini-503-model-is-overloaded>

### Conclusion

**Cause: provider-side capacity on Google AI Studio's shared serving pool
for `gemini-3.5-flash-lite`. Not our pacing, not our prompt size, not our
credentials.** (**CORRECTED 2026-10-02:** "or tier" was struck. Being on the
free tier does not cause the shortage, but Google documents free/Flex traffic
as the first to be shed when there is one, so our tier plausibly decides
whether *we* are the ones refused. See the 2026-10-02 research section.) Four independent measurements agree: zero concurrent
requests against a 15 RPM ceiling; prompts larger than the refused ones
succeed on the same route on other days; the code is 503 UNAVAILABLE, which
is not the 429 a quota breach produces; and the same prompt on the same
model over a second road succeeds seconds later in seven of eight cases.

**The fix at the cause is route diversity, and it is already built** —
`capacity_max_attempts()` in `src/agents/llm_attempts.py` already derives a
longer backoff budget specifically for this error, and routes 2 and 3 in
`config/settings.yaml` already provide the second road. **No new retry
wrapper is warranted, and none is proposed.** A retry wrapper would be the
wrong answer here for the opposite of the usual reason: the backoff is not
missing, and the defect it would paper over is not ours.

What remains genuinely open is **not** the 503s at all:

- The OpenRouter backup route **is in use and does avoid this failure** — it
  carried 7 of 8 same-model retries. It moves the call to a different
  serving pool, not merely to a different label.
- But OpenRouter is a **single funded account**, and that is the real
  fragility. The 2026-09-29 19:46 outage was not capacity: routes 1, 2 and 3
  all returned HTTP 402 (credits exhausted) on one account while the free
  Google road was healthy, and the whole intraday decision run was lost. The
  17 payment refusals in the demotion count are all this. Balance monitoring
  on that one account is worth more than anything done to the 503s.
- Optional and cheap, if the 503s become more frequent: Google has a second
  road of its own (Vertex AI) on a different capacity pool. Not recommended
  now — at 17 refusals across 16 sessions, every one of which was rescued by
  an existing route, the failure is already contained.

## 2026-10-02 — External research pass on the 503s (supersedes "What the research says" above)

The section above concluded that tier is irrelevant to this failure. **That
conclusion is now partly overturned.** The sweep below was done against
primary Google documentation and dated developer reports, not from recall.
Every source is dated; where a claim could not be verified at its source it
is labelled as such.

### (a) What Google's documentation says the error is

- `503 UNAVAILABLE` is documented only as a retryable transient error,
  grouped with `429 RESOURCE_EXHAUSTED`: *"If you receive an error indicating
  that you should retry your request (such as a `429 RESOURCE_EXHAUSTED` or
  `503 UNAVAILABLE`)"*, with exponential backoff prescribed and the rule
  *"Only retry on transient errors (like `429`, `408`, or `5xx`)"*.
  <https://ai.google.dev/gemini-api/docs/troubleshooting> (page footer:
  last updated **2026-10-01 UTC**).
- The error reference gives 503 as `service_unavailable` — *"The service is
  temporarily overloaded or down"*, action *"Wait and retry with exponential
  backoff"* — against 429 `rate_limit_exceeded` / `quota_exceeded`, *"You
  have exceeded the per-minute or per-second request or token limit"* /
  *"your daily quota"*. <https://ai.google.dev/gemini-api/docs/api-errors>
  (last updated **2026-09-20 UTC**).
- **The distinction is therefore formal and holds:** 429 is *our* consumption
  against *our* limit; 503 is *Google's* capacity against *everyone*. Neither
  page gives a cause for 503 beyond "overloaded", and neither mentions tier.

### (b) Free-tier shedding — THIS OVERTURNS THE EARLIER CONCLUSION

Google now documents service tiers with explicit, differing **reliability**,
and the mechanism is named:

| Tier | Pricing | Reliability (Google's own word) |
|---|---|---|
| Flex | 50% discount | **"Best-effort (Sheddable)"** |
| Standard | full price | "High / Medium-high" |
| Priority | "75–100% more than Standard" | **"High (Non-sheddable)"** |
| Batch | 50% discount | "High (for throughput)" |

*"Flex traffic is treated with lower priority. If there is a spike in
standard traffic, Flex requests may be preempted or evicted."* and *"When
Flex capacity is unavailable or the system is congested, the API will return
standard error codes: 503 Service Unavailable: The system is currently at
capacity."* — <https://ai.google.dev/gemini-api/docs/flex-inference> (last
updated **2026-09-23 UTC**).

So shedding is a real, documented, tier-linked mechanism, and the 503 we
receive is the documented symptom of being shed. The remaining question is
whether the FREE tier is sheddable. Google's search index returns, three
times and consistently, the sentence *"Free-tier requests use sheddable
capacity, while billed-tier requests are protected by critical priority"*
attributed to Google's own AI Studio / Gemini API status page,
<https://aistudio.google.com/status>. **Caveat, stated plainly: two direct
fetches of that page (2026-10-02) returned only the incident list and could
not reproduce the sentence**, so it is likely in a JS-rendered FAQ block.
Treat it as strong-but-not-source-verified. It is not contradicted anywhere.

**Where this contradicts the earlier conclusion.** The line above reading
*"free, paid and enterprise keys are affected alike, and raising the tier
does not prevent it"*, and the conclusion's *"not our credentials or tier"*,
are **wrong as written**. Being on the free tier plausibly makes us *first*
to be shed. What survives is the weaker and still-correct claim: **paying
does not make 503 go away.** Dated evidence that paying is not a cure:

- **2026-08-27 to 08-31**, `gemini-3.7-flash`, **Tier 2 paid with Priority
  tier enabled**, EU/Spain: *"This model is currently experiencing high
  demand. Spikes in demand are usually temporary."*, 8 backoff attempts over
  4–5 minutes, 0% success. Another tester in the same thread logged **973
  consecutive 503s before one success — 12 h 22 m 50 s** — and concluded
  *"available capacity can vary extremely sharply over time"*. No Google
  staff reply.
  <https://discuss.ai.google.dev/t/persistent-503-on-gemini-3-7-flash-with-priority-tier-tier-2-paid-0-success-over-multiple-retries/179804>
- **2026-05-14**, paid Pro tier: *"I am consistently receiving 503
  UNAVAILABLE errors while using the Gemini API Pro"*; Google staff replied
  2026-05-21 with a pointer to documentation and no resolution.
  <https://discuss.ai.google.dev/t/persistent-503-service-unavailable-high-demand-errors-on-paid-pro-tier/144838>

**Net: the honest position is "paying reduces the odds of being shed, and is
documented to; it does not remove the failure."** Nobody outside Google has
published a measurement of how much it reduces them, and Google does not say.

### (c) Is `-lite`, or this model id, worse?

**No external source settles this.** No published statement, from Google or
anyone else, says `-lite` variants are more capacity-constrained. The only
asymmetry documented is the opposite direction: Flash-Lite carries the
*larger* free-tier allowance (reported ~15 RPM / ~500 RPD against Flash's
5 RPM / 20 RPD). The recurring practitioner claim is that **new and preview
model ids** are hit hardest before their allocation scales — which is about
model age, not the `-lite` suffix. Our own data cannot separate the two: we
only route `gemini-3.5-flash-lite` at tier 1.

### (d) Region and endpoint

- Vertex AI is a genuinely different road: it runs on Google Cloud capacity
  and bills the same per token, and practitioners do add it as a second
  route for exactly this reason (e.g. a 2026 PR adding *"Vertex AI (global
  endpoint) as a second route to Gemini"*).
- Vertex's default **Dynamic Shared Quota** means you draw from a global
  shared pool and can be refused at low usage when the pool is busy — the
  same class of failure, not a cure.
  <https://cloud.google.com/blog/products/ai-machine-learning/reduce-429-errors-on-vertex-ai>
- **No source found claims a regional endpoint or the global endpoint fixes
  503.** Do not assume it does.

### (e) What practitioners actually report working

- **Second provider for the same model** — the single most reported working
  fix, and the one we already run. Multiple 2026 repos ship exactly our
  shape: Gemini primary, OpenRouter fallback on 503. Matches our measured
  7-of-8 rescue rate.
- **Fallback to an older/adjacent model id** — reported working **2026-08-31**
  (a tester kept `gemini-3.6-flash` in production for this reason), reported
  *not* working **April 2026** across the 2.5/3.x range when the whole pool
  was short.
- **Retries alone** — widely reported as insufficient. **2026-04-08 to 04-28**:
  5–6 attempts needed, *"the service is effectively unusable"*,
  *"basically not reliable"*, no Google acknowledgement.
  <https://discuss.ai.google.dev/t/503-this-model-is-currently-experiencing-high-demand-spikes-in-demand-are-usually-temporary-please-try-again-later/138664>
- **A free-tier trap worth knowing: 503s may still burn daily quota.** Reported
  **2026-09-23/24** (our own worst 503 day) on free tier: five 503s were
  followed by a 429 daily-quota exhaustion. Google staff replied 2026-09-29
  without answering whether failed calls count. Unresolved.
  <https://discuss.ai.google.dev/t/gemini-api-503-errors-appear-to-consume-free-tier-rpd-leading-to-429-quota-exhaustion/184644>
  **If true, aggressive retrying on the free tier makes the day worse, not
  better.** This is an argument against adding retries, consistent with the
  decision already taken.

### (f) Does a paid tier exist that removes this class of failure, and what does it cost?

Two documented products claim non-sheddable capacity:

- **Priority service tier** (Gemini Developer API). Google's own table calls
  it *"High (Non-sheddable)"* at *"75–100% more than Standard"*. Standard
  Flash-Lite is **$0.30 / $2.50** per million input/output tokens
  (<https://ai.google.dev/gemini-api/docs/pricing>, last updated
  **2026-10-01 UTC**); third-party summaries put Priority at ~1.8x, which
  would be roughly **$0.54 / $4.50**. One fetch of the pricing page read the
  Priority Flash-Lite row as $2.70 / $22.50 (9x) — **the multiplier is not
  reliably established here and must be read off the live pricing page
  before anyone budgets it.**
- **Vertex AI Provisioned Throughput** — reserved, isolated capacity,
  described as the only option that isolates you from the shared pay-as-you-go
  pool. Sold as a committed reservation (monthly/annual throughput units),
  not per token; **no credible public unit price was found, and it is
  structurally aimed at sustained high volume, which we are not.**

**Neither is free of the failure in practice:** the 2026-08-27 thread above
is a Priority-tier customer at 0% success. Priority also overflows back to
Standard once your allocation is exceeded.

### (g) Recommended fix at the cause

**No change recommended, and the earlier operational decision stands — but
for a corrected reason.**

- Route diversity remains the right fix and is already built. It is what
  practitioners report working, and it is what our own 7-of-8 rescue measures.
- **Correction to the record:** we should stop saying tier is irrelevant. Our
  free-tier traffic is, by Google's own tiering model, the most sheddable
  class of traffic there is. If 503s become frequent enough to cost a
  decision run, **the cheapest cause-level change is to put the tier-1 Google
  route on a billed key**, which moves us out of sheddable capacity. That is
  a cost question, not an engineering one, and it is not warranted at 17
  refusals across 16 sessions, every one rescued.
- **Do not add retries.** External evidence is that retries do not clear a
  shed, and the 2026-09-23/24 report suggests they may consume free-tier
  daily quota and convert a 503 day into a 429 day.
- **Priority tier and Vertex Provisioned Throughput are not recommended**:
  one is documented non-sheddable yet observed failing on a paid Priority
  account in August 2026, the other is a volume commitment we have no volume
  for.
- The real fragility named above — a single funded OpenRouter account — is
  unchanged by any of this and remains the larger exposure.

**Where nobody outside knows:** Google publishes no cause, no per-tier
shedding rate, no capacity status per model id, and no SLA for free-tier
availability. Every quantitative claim about how much paying helps is
inference, including ours.
