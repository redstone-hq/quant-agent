# Decision-Chain Agent Audit — Findings and Decisions

Status: **accepted — externally reviewed and merged via PR #30 on 2026-08-14**. Historical: the 1.5 reward:risk floor named below was retired 2026-09-17 (reward:risk is no gate), and seat doctrine follows `docs/OUTCOME.md`, "The desk archetype: specialists, weighed, then one head trader" (owner, 2026-10-19).

Covers the five findings the 2026-08-13 adversarial audit of the nine agent
prompts deferred as "behaviour or data flow, hold for external architectural
review". The audit's text-only findings (F1, F2, F3, F7a, F9, F10) landed
earlier in `57f5b2d` and are pinned by
`tests/test_prompt_audit_2026_08_13.py`; they are not revisited here.

Read alongside `SAFETY_BOUNDARIES.md`, which is unchanged by this tranche,
and `MODEL_ROUTING_POLICY.md`, whose PM/RM routing decision intersects with
F5.

## What did and did not change

| | |
|---|---|
| **Fixed** | F6 (risk evidence completeness), F5 (PM/RM independence — RM's *information*, and after PR #30's review the *model split* too), F4 (premortem observability), F7b's detector, F8 (prior provenance) |
| **Intentionally retained** | F5's veto hierarchy (**partly superseded 2026-09-01 on new evidence — see the note under F5**), F4's permissive schema, F7b's decision not to route price data, every sizing threshold under F8 |
| **Unproven until paper trading** | whether better-informed and now model-independent RM review changes verdicts for the better; whether the inherited long bias is right for this account; whether the `pm_audit_step_missing` advisory ever fires in practice |

**No deterministic risk or execution semantics changed.** No threshold moved,
no gate was added or removed, no schema became stricter, Alpaca stays
Paper-only, and every failure path added degrades to "evidence unavailable",
never to a benign-looking value. The one new engine advisory is non-blocking
and rides the seam `data_degraded` and `correlation_coverage_gap` already use.

---

## F6 — The Risk Manager could not audit two of the rules it owns

> **Superseded in part (2026-09-20).** The drawdown-halving rule this
> finding is half about no longer exists: the owner removed the entire
> account-level loss-alarm mechanism — the daily halt and the 5d/20d
> BUY-halving brake — on 2026-09-20 (`docs/INCIDENT_HISTORY.md`, retired
> board item 32). The rolling returns still reach the Risk Manager, but
> there is no `in_drawdown` flag and nothing is halved. The holding-
> discipline half of this finding stands. Left as written below because
> this file records what was found and decided at the time.

**Finding.** `config/prompts/risk_manager.md` makes RM the reviewer of PM's
holding discipline and of the system-drawdown halving rule.
`RiskManagerAgent.build_user_message` passed the inputs for neither.
`Position` (`src/models.py`) carries no entry date, and `in_drawdown` was
computed in `DecisionStage` as a local, handed to PM, and discarded.

**Why it mattered.** PM's tiered holding discipline turns on `days_held`: a
position held under 5 days is in a protection period where only three named
triggers permit a SELL, and "the Tech rating dropped" is explicitly not one
of them. Without the age, every SELL looked equally legitimate to RM. The
drawdown rule is worse: `in_drawdown=true` requires **every new BUY halved**,
and *no deterministic code enforces it* — `src/risk/rules.py` never sees the
flag. RM was the only possible check on that rule and was blind to it. A PM
that quietly skipped the halving during a losing stretch would have been
approved by a reviewer with no way to know.

> **Superseded (2026-08-27, commit `c89e957`).** The finding above describes
> the state this F6 fix addressed at the time — RM's evidence, not the halving
> itself. The halving is no longer prompt-only or RM-dependent:
> `src/risk/rules.py::apply_drawdown_scale` now halves every BUY
> deterministically before RM ever reviews the plan, backed by a
> `drawdown_buy_cap` fail-closed hard block. See `AGENT_ROLE_AUDIT.md` §1.1
> and `QAMC_REMEDIATION_SPEC.md` Phase 2. RM's role per item 7 below shifted
> accordingly — see `config/prompts/risk_manager.md`.

**Decision.** Route the evidence. This is plumbing an existing measurement to
an existing consumer, not new authority: RM's remit already covered these
rules and its prompt already claimed the inputs.

**Change made.**
- `src/agents/risk_manager.py` — positions render `held: Nd` plus the
  discipline tier; the Account block renders `rolling_5d_pct` /
  `rolling_20d_pct` / `in_drawdown` and, when true, states what PM was
  required to do about it.
- `src/pipeline_context.py` — `position_history` and `recent_performance`
  added to `RunContext`.
- `src/pipeline_stages.py` — `DecisionStage` publishes both to ctx so RM
  audits PM against the **same snapshot PM sized from**, not one taken
  minutes later. `RiskStage` rebuilds them when ctx is empty, which is the
  RC2 resume lane (there `DecisionStage` never runs at all).
- `config/prompts/risk_manager.md` — checklist items 7 and 8, and the
  "Inputs you read" section now matches what the renderer sends.

**Evidence/tests.** `tests/test_agent_audit_2026_08_14.py::test_f6_*` — nine
tests covering the three tiers, the unknown-age path, drawdown true/false,
the ctx hand-off, the resume-lane rebuild, and a rebuild that raises.

**Remaining uncertainty.** Whether RM *acts* on the new evidence is a model
behaviour question no unit test answers. The observable to watch in paper
trading is whether `sizing_sanity` and `signal_fidelity` findings ever cite a
`held:` tier or `in_drawdown`. If they never do after a drawdown session, the
evidence is arriving and being ignored, which is a different and larger
problem than the one fixed here.

---

## F5 — The independent gate was reading PM's story first

**Finding.** PM's `reasoning_chain` was the **first** block in RM's user
message, before the account, the positions, the Tech signals, the news and
the macro data. RM also had no idea that PM calibrates its sizing against
RM's own past verdicts, nor that the two ran the same model.

**Why it mattered.** Three distinct couplings, one direction:

1. *Ordering.* RM met PM's case for the plan before a single primary number,
   then graded the narrative for internal consistency — which a coherent
   narrative passes whether or not the book is sound.
2. *The calibration loop.* PM reads RM's last 5 verdicts and their
   `reason_category` tags and pre-adjusts before RM sees anything: two
   `oversized` tags cut its base allocations 25%. So a conservative-looking
   plan may be PM anchoring on RM's history rather than expressing
   conviction, and a run of `clean` verdicts is evidence **about the loop**,
   not about the plans. RM was never told the loop existed.
3. *Shared model.* The routing policy as proposed put both seats on
   `google/gemini-2.5-flash-lite`. Whatever blind spot produced the plan was
   one the reviewer shared. **This one was subsequently fixed at the routing
   layer** — see the correction below.

**Decision.** Change what RM **knows**, never what it **may do**. Reorder the
evidence, label PM's chain as claims, and disclose both couplings to RM.
Leave every threshold alone.

(The third coupling was later removed outright rather than merely disclosed,
once PR #30's review established that the quality argument against splitting
the seats did not exist.)

**Change made.**
- `src/agents/risk_manager.py` — block order is now `proposed trades →
  account/market facts → PM's claims → the deterministic engine's findings →
  verdict`. RM forms its own read from primary data first, and the last input
  before the verdict is the one PM did not author. The chain is headed "PM's
  CLAIMS about its own plan, not evidence" with an instruction to verify
  cited numbers against the blocks above and to say so where it cannot. An
  entirely absent chain now renders as a stated finding rather than an empty
  string.
- `config/prompts/risk_manager.md` — a leading "Your independence" section
  disclosing the calibration loop and RM's model relationship to PM. That
  last paragraph is pinned to `config/settings.yaml` by
  `test_f5_rm_prompt_states_its_model_relationship_to_pm_accurately`, so the
  prompt cannot go on claiming a shared model after the seats diverge (or
  vice versa) — the stale-claim failure this whole audit is about.

**Intentionally retained.** The veto hierarchy — "veto is nuclear", prefer
`modifications`, `approved: false` only for an incoherent chain / ≥ 5 mods /
a missed hard rule. The audit flagged this as framing disagreement as nearly
forbidden. It stays, for a stated reason: a rejection kills the whole plan
and PM learns only a one-word `reason_category`, whereas `modifications`
are surgical and carry a per-symbol reason. Loosening the threshold changes
trading behaviour, and that is a paper-trading question, not a prompt edit.
The prompt now says "independence does not mean disagreeing more often" and
"`clean` on a genuinely clean plan is the correct verdict", because the
failure mode of the opposite instruction is a veto layer that manufactures
objections. Pinned by `test_f5_veto_hierarchy_is_unchanged`.

**Supersession (2026-09-01, spec Phase 10.1 — owner-ratified).** The
retained decision above rests on one sentence: *"a rejection kills the whole
plan and PM learns only a one-word `reason_category`, whereas `modifications`
are surgical and carry a per-symbol reason."* That reasoning was correct and
still is. What it did not say — because nobody had noticed it yet — is that
the two options it compares are the ONLY two the schema offered. There was no
lever between "retune this symbol's fields" and "kill everything", so the
question "can this one trade be refused?" had no expressible answer.

The evidence that did not exist on 2026-08-14 arrived on 2026-09-01. Run
`run-64290730`: the verdict rejected the whole morning citing XLE alone
(constructed R/R 1.18, under the 1.5 floor). CHPX died with it — R/R 3.03, a
different sector, an unrelated technical thesis, nothing said against it. Zero
trades. That is not the veto being used too readily; it is the veto being the
only instrument available for a per-name finding.

**What is superseded:** the claim that the veto hierarchy's two rungs are
sufficient. `RiskVerdict` gains `rejected_symbols` — a per-symbol refusal that
kills one leg and leaves the rest of the plan standing, carrying a per-symbol
`reason` into the evidence trail exactly as `modifications` already did.

**What is NOT superseded, and is now pinned by test rather than by prose:**

- *No threshold moved.* This changes the granularity of refusal and nothing
  else. `≥ 5 modifications`, the 1.5 R/R floor, `max_position_pct`, every
  short cap — all unchanged.
- *The book-level veto is untouched.* `approved: false` still refuses the
  entire plan and is evaluated first. Correlation clusters, total exposure and
  drawdown state are properties of the whole account, so when the BOOK is what
  fails, refusing everything remains correct. Only per-SYMBOL findings became
  per-symbol.
- *Independence is still about what RM knows.* The audit's core principle —
  "change what RM knows, never what it may do" — is bent here, deliberately
  and narrowly: RM may now refuse ONE trade. It gains no power to approve
  anything it could not approve before, and every new lever is strictly more
  restrictive than the alternative it replaces (a refused trade does not
  trade). The governing rule the prompt now states, in the owner's words:
  *"The batch is arbitrary — it is whatever happened to be proposed in one
  run. Judging a trade against its accidental co-passengers makes no sense.
  Judge it against what the account actually holds."*
- *"Independence does not mean disagreeing more often"* stays in the prompt,
  and `test_f5_veto_hierarchy_is_unchanged` still passes unmodified — all four
  of its anchors survive verbatim.

Explicitly rejected as a fix: detecting a rejection and re-running the plan
without the failing leg. That asks the wrong question twice and spends a
second paid session papering over a schema defect.

**Evidence/tests.** `test_f5_*` — six tests, including an explicit assertion
on the *ordering* of the five section headers — all still passing unchanged.
The supersession has its own file, `tests/test_risk_verdict_per_symbol.py`,
which reproduces the XLE/CHPX case by name and pins the book-level veto in
both directions.

**Correction (PR #30 review).** This section previously said the structural
option — a different model at the RM seat — "was measured and rejected on
quality: every alternative scored worse at that seat". **That was false.**
It repeated a claim from `MODEL_ROUTING_POLICY.md` that had read the
whole-sweep aggregate as if it were the seat's result. The committed
2026-08-12 raw results show **10 of 12 candidates scoring 1.00 on both runs
at `risk_rr_breach`**. A model that is mediocre across six roles can be
perfect at one, and the RM seat only ever runs RM.

The seat was re-measured on this branch (2026-08-14, 5 models x 2 RM
scenarios x 3 repeats, $0.157) because the old numbers were both misread
and stale — they predated the 2026-08-13 prompt cleanup and the F5/F6
changes here. Four candidates tie at 1.00 mean / 1.00 worst; a deliberately
weak control still scores 0.00, so the scenarios discriminate. With quality
tied, the structural option was taken: **`risk_manager` now runs
`qwen/qwen3-235b-a22b-2507` and PM runs `google/gemini-2.5-flash-lite`**.
It cost nothing — $0.00162 vs $0.00163 per two calls, +7.6s on a seat that
issues one call per 1200s session. Full evidence and tie-break:
`MODEL_ROUTING_POLICY.md`, "Why `risk_manager` is not on PM's model".

**Remaining uncertainty.** Smaller than it was, and still real.

The gate no longer shares PM's model, which removes a correlated failure
mode. It does not follow that RM catches more: two models can be wrong in
different ways and still both be wrong, and nothing here demonstrates a
better verdict. The evidence supports "independence was available for free",
not "independence improves decisions".

The tie itself is thin — four models each went 6-for-6, which is enough to
say no candidate is clearly better and therefore that nothing measurable was
given up, and not enough to say the four are equivalent.

Ordering and disclosure remain what they were: the cheap half. No unit test
can show that moving PM's narrative below the primary evidence changed a
verdict.

The paper-trading observable is unchanged and is now the load-bearing one:
the `reason_category` distribution. An RM whose verdicts are essentially
always `clean` while PM's plans vary is not reviewing — and that would be
true on a shared model or a split one.

---

## F4 — A mandatory audit step could disappear in silence

**Finding.** `premortem_check` and `continuity_check` are MANDATORY in
`config/prompts/portfolio_manager.md` and default to `""` in
`ReasoningChain` (`src/models.py`). PM skipping the red-team step therefore
produced a clean parse, a clean log line and a clean verdict. Compounding it,
RM's renderer emitted **seven** of the nine fields — and the two it omitted
were exactly the two that can be empty.

**Why it mattered.** The pre-mortem is the only step that argues against
today's book. Its whole purpose is to catch a systematic directional bias
that a forward-only chain cannot see — which is also F8's subject. The one
reviewer positioned to notice it was missing was the one not being shown it.
Nothing else in the system looked.

**Decision.** Make the omission loud, at the observability layer. Do **not**
make the schema strict.

**Change made.**
- `src/agents/risk_manager.py` — all nine fields render; an empty mandatory
  field renders as an explicit `[MISSING — ... Treat the audit step as NOT
  PERFORMED]` line rather than being omitted. An omitted section reads as
  "PM had nothing to say"; a marker reads as "the step did not happen".
- `src/pipeline_stages.py` — `RiskStage` raises a `pm_audit_step_missing`
  advisory, which RM's prompt already obliges it to answer in the matching
  `reasoning_chain` field. `DecisionStage`'s operator-facing log line now
  carries all nine fields.
- `config/prompts/portfolio_manager.md` / `risk_manager.md` — both ends
  describe the mechanism.

**Intentionally retained.** The permissive schema. `min_length=1` on the two
fields is the obvious fix and would break replay of every pre-2026-06 log,
which carries neither. Enforcement belongs in observability, not in making
historical data unparseable. Pinned by
`test_f4_schema_stays_backward_compatible`.

**Evidence/tests.** `test_f4_*` — seven tests: the `[MISSING]` render for
each field, all nine present, the advisory raised on omission and *not*
raised on a complete chain, the schema's backward compatibility, and the
nine-field log line.

**Remaining uncertainty.** The advisory is non-blocking by design, so a PM
that skips the step and an RM that shrugs at the advisory still produce a
tradeable plan. Whether it needs teeth depends on how often it fires, which
is a paper-trading measurement. The right escalation, if the rate is high,
is a deterministic policy decision for the reviewer — not something to add
speculatively now.

---

## F7b — Earnings valuation data: retained, not routed

**Finding.** `earnings_analyst` is graded on `valuation_context` and that
column drives its sentiment rubric, but `build_user_message` passes filing
text plus `symbol` / `form_type` / `filing_date` and nothing else. The 2026-08-13
tranche fixed the prompt (the field is now filing-grounded, with
`[UNSOURCED:no_market_data]`) and explicitly deferred the data-flow half:
should the real multiples — which exist, and already feed `tech_analyst` —
be routed here?

**Decision. No.** Two reasons, both structural rather than aesthetic:

1. **The artifact is cached for the life of the filing.** `_save_analysis`
   writes the analysis to disk once and `_load_analysis` re-serves it
   unchanged for weeks. A price-derived figure written on the filing date is
   stale the next morning and keeps arriving at PM as though current. The
   conditional reading the prompt now asks for ("the Services mix shift is
   doing the margin work; a deceleration below +10% removes it") stays true
   exactly as long as the filing does.
2. **The multiples are already read at the seat where they are fresh.**
   `trailing_pe` / `forward_pe` / `ps_ratio` are fetched each session by
   `MorningResearchStage` and passed to `tech_analyst`. Routing them into a
   cached fundamentals read would duplicate the data at the one seat that
   cannot keep it current — and would couple the earnings load to the tech
   fan-out it currently runs in parallel with.

**Change made.** No data flow. Instead:
- `src/agents/earnings_analyst.py` — `_flag_unsourced_valuation_claims`
  logs a warning when `valuation_context` asserts anything requiring a share
  price (P/E, PEG, EV/EBITDA, market cap, "trading at", "Nx forward
  earnings"). It runs on `source="cache"` too, because an invented multiple
  written before the prompt was corrected keeps reaching PM until something
  can see it. Leverage and coverage ratios are deliberately **not** matched —
  a 10-Q does disclose "net debt 2.1x EBITDA", and a detector that punished
  real filing numbers would push the agent away from citing them.
- `src/models.py` — the schema comment defined `valuation_context` as "is the
  market pricing this fairly given the above?", the exact question the seat
  cannot answer, left contradicting the prompt that had just been corrected.
- `config/prompts/earnings_analyst.md` — records why price data is withheld,
  so a later editor reads the gap as a decision rather than an oversight.

**Evidence/tests.** `test_f7b_*` — twelve cases: seven price-derived phrasings
detected, four filing-grounded statements not flagged, cache-path coverage,
and a guard that detection never rejects the analysis (a text heuristic must
not cost the only fundamentals read for that name).

**Remaining uncertainty.** The detector is a regex over prose and will not
catch a model that asserts a multiple in words it does not match. It is a
tripwire, not a proof. If paper-trading logs show it firing regularly, the
prompt is not holding and the answer is a prompt fix, not a wider regex.

---

## F8 — Three behavioural priors were stated as standing truths

**Finding.** `config/prompts/portfolio_manager.md` carries three rules
derived from a specific measured window: the deployment-gap rule ("the single
largest P&L drag"), the over-caution diagnosis ("under-owning confirmed
leaders cost ~8pts vs SPY") and the momentum-leader starter sleeve. All three
trace to forensics on the **predecessor account over Apr–Jul 2026**
(`df66ab2`, `d821ab6`). All three read as general truths about markets. All
three push the book structurally long.

**Why it mattered.** The measurements are real — that is exactly what makes
them persuasive. But Apr–Jul 2026 was one window, one account and one regime
in which being more invested paid; the same method in a regime that punished
exposure would have produced the opposite rule with equal confidence. **This
account has never traded.** The priors were inherited, not earned here, and a
model given them as facts has no way to weigh them against the account's own
record when that record starts to exist.

**Decision.** Keep all three and make them auditable. They are the best
evidence available for an account with no history of its own; deleting them
would substitute no evidence for imperfect evidence. What changes is that
they now say what they are.

**Change made.** `config/prompts/portfolio_manager.md`:
- a "Where the behavioural priors below come from" table naming the claim,
  the window and the sample behind each;
- a `[PRIOR]` tag at each use site, so a reader who reaches the rule without
  reading the table still sees it;
- an explicit precedence rule — when PMFacts carries this account's own
  outcomes (`closed_trades_30d`, non-null rolling returns), size from those
  and say so; while it reports `[UNSOURCED:no_calibration]`, act on the prior
  but name it as one; `meta_reflector` re-derives these quarterly from the
  account's own record, and when its findings disagree, the account wins;
- a note that none of the three ever overrides a hard rule;
- a narrowing of the pre-mortem framing: knowing which arm you under-write
  says which one to write properly, not which one is right, and "a red-team
  that always concludes 'size up' is not a red-team".

**Intentionally retained.** Every sizing threshold, the sleeve's 5% cap, the
deployment-gap procedure. Making a prior explicit is a statement about its
evidence, not a decision to act on it differently. Pinned by
`test_f8_no_sizing_threshold_moved`.

**Evidence/tests.** `test_f8_*` — seven tests covering the provenance block,
each named prior, the use-site tags, the precedence rule and the unchanged
thresholds.

**Remaining uncertainty.** This is the finding most dependent on paper
trading and the one this tranche can least resolve. Whether the long bias is
correct **for this account in this regime** is unknowable until the account
has its own closed trades; the change makes the question askable and the
answer auditable, nothing more. The observable is whether PM's
`reasoning_chain` starts citing this account's measured `win_rate_30d_pct`
instead of the inherited prior once `closed_trades_30d` is non-trivial. If it
keeps citing the prior after real data exists, the precedence rule is not
working and the priors should be cut rather than annotated.

**Superseded — all three priors are now cut.** The owner ruled on 2026-10-09
that trades before the 2026-10-12 open are not evidence. The deployment-gap
claim and the over-caution claim were removed on 2026-10-15. The owner ruled
on 2026-10-16 to remove the starter sleeve too; fast movers come through the
velocity filter instead. The sleeve never placed an order on its own: the
entry bar in `src/risk/conviction_bar.py` gives the Tech seat no weight in
favour of a buy. `tests/test_pm_prompt_no_prefix_priors.py` pins that none
of it returns.

---

## Cost

The initial agent-audit findings were resolved by repository/history review
without paid model calls. PR #30's external review then exposed that the RM
model-independence rationale relied on stale and misread benchmark evidence,
so the RM seat was re-measured on the current branch: 30 trials costing
**$0.157**. The broader re-review round, including the smoke trial and live
commissioning preflights, consumed **$0.2168** in model spend; `docs/WORK.md`
records that operational accounting. No paid run was used to justify F4,
F6, F7b, or F8 themselves.
