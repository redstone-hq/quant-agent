## item 177

**Moved from WORK.md (2026-09-24) —** Questions: the 3 `IntradayScanConfig` ledger entries. Cadence: 3 code sites. Stop coverage is separately scheduled and free; the intra preamble (fills, stop-outs, loss check, drains) is not.

### What exists today [all measured 2026-09-26 against the production DB `/home/qamc/quant-agent/data/quant_agent.db`, read-only copy]

A paid intraday tick exists and is ON. `IntradayScanConfig.enabled` defaults False, but the deployed `config/settings.yaml` turns it on (the 2026-09-14 verification already corrected a stale comment claiming otherwise) and the cost circuit proves it: **211 `intra_check` sessions, 106 of them paid, $13.93 total — 62.8% of the desk's entire recorded model spend of $22.18** (`llm_budget_sessions`, 2026-08-25..2026-09-25). Morning, by comparison, is 31 sessions and $7.66. Per-day, intra_check outspends morning roughly eightfold.

TRIGGER. `move_threshold_pct` (flat 3% since the prior close, from one bulk snapshot call), then `cooldown_hours` (3h, same symbol), then `max_candidates_per_scan` (5 movers). Held investable names are added *on top of* the cap and consume no cooldown.

CADENCE. **Production is the systemd timer, not the APScheduler trigger** — verified on the box 2026-09-26 (`quant-agent-intra_check.timer` last fired 06:45, next 07:15). `OnCalendar=*:15,45` (moved off the shared `*:0/30` tick 2026-09-17) crossed with `run_if_et_window.sh`'s 09:30-16:00 ET window gives **13 ticks a day** — and the cost circuit records exactly 13 `intra_check` sessions on every trading day 2026-09-21..25, and 14 on every day before the timer moved. The `intra_check` mode is deliberately exempt from the once-per-day guard and the cross-mode session lock.

HELD BOOK. Measured on paid ticks joined to `specialist_evidence`: the symbol count is bimodal — 66 paid ticks analysed ≤5 symbols (movers only) and 39 analysed ≥11. **A tick carrying the held book costs 1.54x a movers-only tick** ($0.167 mean vs $0.108, n=39/66). The book stood at 10-12 names through the measured window, so held coverage roughly triples the tech payload of a capped scan.

### What the record says about each part

- **The flat trigger does not discriminate.** Across 253 `selected` rows (2026-09-02..25), the median move of a selection that produced a BUY or SHORT was **3.50%**, against **3.67%** for one that produced nothing. By bucket: 3-4% → 154 selections, 7 traded; 4-5% → 34/1; 5-6% → 25/0; 6-7% → 26/0; 7-8% → 6/2; 8-9% → 8/1. Raising the threshold would have removed the only band with volume and kept two bands that produced nothing. **Re-picking the flat number in either direction has no basis in the desk's own record.**
- **The ATR-relative form was unmeasurable.** The ledger's open question asks what move matters *relative to the name's own ATR*, and `intraday_evaluations.detail` stored `move_pct=` and nothing else — the denominator was never recorded. Fixed 2026-09-26: the row is now upserted after `compute_indicators` with `atr_pct=` and `move_atr=`, on bars the scan already paid to fetch, changing no behaviour and adding no row (`_record_intraday_trigger_atr_context`, `src/pipeline.py`). The question becomes answerable once enough rows accumulate. Coverage limit, stated: only names that already passed the flat 3% are stamped, so the record can show the trigger firing too *loosely* for a quiet name, never that it fired too tightly for a volatile one.
- **The cap binds, and binds silently.** Movers ledgered per run: 1→28 runs, 2→18, 3→16, 4→11, **5→20**. The cap of 5 is hit on 20 of 93 runs (21.5%), and the candidates dropped past it leave no record — exactly the `cost_while_unanswered` the ledger row predicted.
- **The yield.** 85 of 106 paid ticks (80%) produced no order at all. Across the whole record, paid intraday ticks are attributable to 21 new-position decisions (18 BUY + 3 SHORT), i.e. **$0.66 of model spend per position opened**. Over the retained report window, `intraday_no_trades` was 27 ticks for $4.39 and `intraday_executed` 10 ticks for $2.07.
- **Skips are already visible.** `paid_analysis_suspended`, `intraday_scan_lock_contended`, `intraday_scan_disabled`, `intraday_scan_crashed` and `intraday_scan_no_opportunity` all carry plain-English renderings in `src/notifier.py` and routing in `src/trader_feed.py`, and every return path is persisted to `intra_check_reports`. The doctrine that a budget-skipped tick must be visibly skipped is **already met**; no defect here.

### How the three parts interact — why they cannot be settled apart

The trigger sets how many movers *qualify*; the cap decides how many of those are paid for; the cadence multiplies whatever survives by 13; and the held book adds a fixed ~54% surcharge to every one of those 13 regardless of how many movers there were. So a day's paid intraday bill is roughly `13 × (movers-under-cap + held-book) × per-symbol cost`, and **the held book is the only term that is neither capped nor cooled down**. Loosening the trigger without touching the cap changes nothing (the cap already binds a fifth of the time). Tightening the trigger without touching the cadence saves little, because 13 ticks × held-book coverage is paid whether or not anything moved. Cutting the cadence is the only lever that scales all three at once — and it is also the only one that directly trades away loss-protection latency, because the same tick carries the free preamble.

### Still open, and exactly why

1. **2026-10-12 (owner rulings): ONE paid batch per check, and no paid PM call when the book has no room.** Each intraday check sends only the strongest-ranked batch of five movers (`_NAMES_PER_PAID_CALL`) to paid analysis; the rest are recorded `not_sent_weaker_rank` with their rank. The loop of further batches until the spending cap refused is deleted; the cap stays as a backstop before the one call (`budget_refused`). Before the paid portfolio-manager call, the check measures room deterministically (`src/intraday/buying_room.py`): the same headroom execution sizes against (`_entry_deployment_budget`: the 2.0x gross ladder, or settled cash with margin off) against the smallest position that clears the 0.5% minimum-risk floor at each mover's own previewed stop (`min_risk_shortfall`). No mover fits (or none has a readable stop): the PM is not called, the scan record carries status `intraday_no_buying_room` with the limit, headroom and candidates considered, the Telegram feed says "No paid buy check: no room to open even a minimum-size trade (...)", and the below-entry-bar sale still runs. Measured 2026-10-12: 17 of 19 paid intraday PM calls had answered "no room". The 2026-10-09 text below ("sent in that order until the paid-analysis budget refuses") is superseded on that point.
1. **2026-10-09: `move_threshold_pct` and `max_candidates_per_scan` are DELETED.** Movers are now ranked by move / the name's own ATR (ATR as a percent of the prior close; a name with no readable ATR is dropped as `no_atr`) and sent in that order until the paid-analysis budget (`llm_cost_circuit`) refuses (`budget_refused`). No replacement threshold or cap. `cooldown_hours` remains. Earlier text below is history.
   **The 3 `IntradayScanConfig` ledger rows.** `move_threshold_pct` cannot leave `arbitrary` yet: the measurement that would source it started on 2026-09-26 and has no rows. `max_candidates_per_scan` and `cooldown_hours` are **owner-appetite**, not research questions — see below.
2. **Every intra-preamble job on its own schedule.** **DONE 2026-10-01.** The free safety work no longer depends on the paid tick: it is one shared method with two callers — the paid `intra_check` tick (unchanged) and a new free `intra_safety` mode with its own systemd service and timer. Additive, not a move, so there is no window in which protection is not restored; both callers take the same broker-write flock and the same blocking-owner check (item 127), so concurrent firing serialises rather than races, and each tick now has two independent chances at the safety work instead of one. The new timer's interval is the already-ledgered `INTRA_CHECK_TICK_MINUTES` (the cadence this work runs on today, so latency is unchanged by construction) and its phase is the midpoint of the two phases already in use on the box, which is the only one at that interval colliding with no existing unit; both are pinned by `tests/test_intra_safety_schedule.py`. **Nothing here licenses choosing a different interval** — the input that would, the observed distribution of how long a position actually stays unprotected, cannot be computed at all yet — `pending_protection_restores` records when the intent was written but the drain deletes the row on success, so nothing records when protection came back [measured 2026-10-01 against the production DB, read-only]. A cleared-at record has to exist before an interval can be chosen on evidence. **Consequence for the rest of this item:** cutting the paid cadence is now a pure spend decision and no longer trades against loss-protection latency.
3. **A correction the ledger needs and this change could not make** (`config/number_ledger.yaml` is held by another change): the `source:` on `src.config.INTRA_CHECK_TICK_MINUTES` names `src/scheduler.py:56` as "the authority for how often the intraday control actually fires". That is **false in production** — `src/scheduler.py` is the `--mode live` path and the box runs the systemd timer. The row's status can stay `sourced`; the source text should name `scripts/systemd/quant-agent-intra_check.timer` crossed with `run_if_et_window.sh`'s window, with `src/scheduler.py` as the live-mode mirror, and cite `tests/test_systemd_units.py` for the pin that now holds all three sites together.


**Re-applied 2026-10-02 onto the split modules, with one correction.** The fingerprint now hashes only the six stored-prior-rating fields the prompt renders, plus today's date (the prompt shows the rating's age against today); the first version hashed the whole stored entry, which includes the fingerprint and verdict stored beside it, so a stored fingerprint could never equal the next run's and the cache would never have hit [verified by test: `test_a_second_identical_run_is_carried_through_the_store` fails on the old key]. The fields live on `src/models/tech_reread.py`, the wrapper on `src/agents/tech_reread.py` (a mixin, because the size baseline forbids growing the agent file). A comparison that cannot be made (unrenderable input, no bars, unreadable stored verdict) asks the seat.

### 2026-10-01 — what the paid tick actually wastes, MEASURED

The owner asked what the half-hourly `intra_check` session is losing. Measured
read-only against the production DB on 2026-10-01, across 116 recorded
half-hourly checks: 32 no-opportunity, 27 no-trades, 18 paid-analysis-suspended,
17 `evidence_gate_skip`, 9 `intraday_scan_crashed`, 1 `intraday_analysis_error`,
1 rejected, 11 executed. The session is 63% of lifetime model spend ($13.93 of
about $22) and sourced 37 of the desk's 80 trades, so the question is only about
the wasted ticks, not about the tick itself.

**ONE cause explains 9 of the 9 crashes, and it is not a defect in this repo.**
Every one of the 9 `intraday_scan_crashed` payloads carries the same error:
OpenRouter HTTP 402, "This request requires more credits, or fewer max_tokens.
You requested up to 16000 tokens, but can only afford 843/811/775". First
occurrence 2026-09-28 18:20 ET, last 2026-09-29 19:47 ET; eight of the nine fall
inside a single afternoon once the research balance ran down. The provider
refused before generating, so the refused call itself billed nothing — the loss
is the free setup work the tick does before reaching the paid call, repeated
every half hour while the balance stayed empty.

The 1 `intraday_analysis_error` is a DIFFERENT and unrelated cause: a
`pm_grounding_error` on 2026-09-25 where the PM's own output claimed news
coverage that did not exist and mislabelled a macro stance. That is the
grounding check doing its job and refusing an ungrounded decision, not a fault.

**FIXED here:** the naming. Reporting an exhausted research account as "the scan
for movers crashed" is a false statement about the desk's own state, which this
desk treats as a root-cause defect in its own right. A payment refusal out of
the scan is now reported as `intraday_scan_out_of_credit` with its own plain-words
line in the owner feed ("the research account is out of credit"), while staying
in exactly the same unhealthy, non-deciding, owner-visible class as the crash it
replaces. Nothing is swallowed, no retry/backoff/timeout was added, and no
number was introduced.

**STILL PRESENT, and deliberately not fixed here:** the tick keeps re-entering
the scan every half hour while the account is empty, doing its free setup work
and reaching a refusal each time. The cost-circuit latch that would stop that
(`provider_out_of_credit`) is self-clearing by design, so it re-arms rather than
holding. Pre-checking the remaining balance before the tick's work would need a
threshold — how little credit is too little — and this desk does not invent
numbers, so that is recorded as an owner appetite question, not picked here.

**NOT REPRODUCIBLE / not applicable:** nothing. Both causes are fully explained
by their recorded payloads.


**Moved verbatim from docs/WORK.md, 2026-10-04 (board trim). Each entry: the kept lead on the board, then the text that was moved.**

- Board line lead: `- [x] the cadence ledgered and test-covered — the "unledgered" half was STALE:`
  Moved text: `src.config.INTRA_CHECK_TICK_MINUTES` has carried `status: sourced` since the item was filed. The untested half was real and is closed here. Production runs the **systemd timer**, not the APScheduler trigger (verified on the box 2026-09-26), and nothing pinned the timer's *spacing* to the constant the cost circuit derives its cooldown from — only its minutes were pinned, for an unrelated reason. `tests/test_systemd_units.py` now derives the production cadence from `quant-agent-intra_check.timer` crossed with `run_if_et_window.sh`'s ET window, holds it equal to `INTRA_CHECK_TICK_MINUTES`, holds the live-mode trigger to the same spacing, and fails if the operator-facing tick count in the wrapper outlives the schedule (it had: it still said ~14 after the 2026-09-17 move to 13).

- Board line lead: `- [x] every intra-preamble job on its own schedule — CLOSED 2026-10-01.`
  Moved text: The free safety work (protection-restore drain, repeg drain, stop-coverage reconcile+repair, retired-cash-park release, orphan-pending-submit reconcile, fill reconcile, stop-out reconcile and the owner surfacing of what they found) was the opening block of `_run_intra_check_body` and nothing else, so it could only run when the PAID tick ran. It is now one shared method (`TradingPipeline._run_intra_safety_preamble`) with two callers: the paid tick, unchanged, and a new free `intra_safety` mode (`run_intra_safety`, `main.py --mode intra_safety`, `scripts/run_if_et_window.sh intra_safety`) with its own systemd service and timer. **Strictly additive — the paid tick lost nothing and no new unprotected window exists**: the work is the same idempotent code, both callers take the same `_intraday_scan_process_lock` flock and the same `_blocking_owner_session` check (item 127), so two units firing together serialise instead of racing, and the net effect is that every tick now gets TWO independent chances to run the safety work instead of one. **No number was invented**: the new timer's interval IS `src.config.INTRA_CHECK_TICK_MINUTES` (30, `status: sourced` in the ledger — the interval this work already runs on, so protection latency is unchanged by construction), and its phase `*:07,37:30` is the midpoint of the two phases already on the box (`*:0/30` for every other session timer, `*:15,45` for intra_check since 2026-09-17), which is the only phase at this interval that shares a second with no existing unit — `tests/test_intra_safety_schedule.py` fails the build if either the spacing stops equalling the ledgered constant or the phase ever collides. The ET window is intra_check's window copied, and the mode is deliberately NOT added to `SESSION_WINDOWS` because that table feeds the expected PAID-call count in `src/config.py` and this mode can never spend. **What is still unmeasured, stated:** nobody has ever measured how long a position actually stays unprotected — the latency from a `pending_protection_restores` row being written to the drain clearing it, and from a broker stop-out filling to the reconciler writing it back. That latency is not merely unread, it is **unrecordable today**: `pending_protection_restores` carries `created_at` and no completion column, and the drain DELETES the row on success, so the clear time is never written anywhere [measured 2026-10-01, production DB read-only: the table's columns, and 0 rows outstanding]. Choosing a different interval needs a cleared-at (or a drain-outcome row) first; until then any other interval would be a guess. The precondition this criterion exists for is now satisfied: the paid cadence can be cut without cutting loss-protection latency.

- Board line lead: `- [x] spend and actions re-measured — 2026-09-26, against the production cost ci`
  Moved text: `intra_check` is the desk's largest spender: **$13.93 of $22.18 all-time, 62.8%**, over 211 sessions of which 106 were paid, against morning's $7.66 over 31. 13 paid ticks a day since the timer moved, 14 before. A tick carrying the held book costs **1.54x** a movers-only tick ($0.167 vs $0.108 mean). **80% of paid ticks (85 of 106) produced no order**, and the whole record attributes 21 new positions to intraday discovery — **$0.66 of model spend per position opened**. The mover cap binds on 21.5% of runs and drops the excess with no record. Figures and method in `docs/board_notes/`.
