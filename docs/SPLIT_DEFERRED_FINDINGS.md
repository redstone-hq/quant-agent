# Findings parked during the rebuild

Bugs noticed while moving code, deliberately NOT fixed inside a split: a
behaviour change hidden in a verbatim move is unreviewable. Each entry says
what is wrong, where, and what would prove a fix. Work them after the
structure is sound, hardest-wearing first.

### Owner-alert senders that bypass the retry funnel (CLOSED 2026-10-04)
## Owner-alert senders that bypass the retry funnel
## TRIAGE 2026-10-05 (every entry re-checked against main, not against its heading)

Pile A, ALREADY FIXED, proved in code: four.
- Undefined names: `scripts/check_undefined_names.py` re-run, 601 files, 0 findings (re-run 2026-10-05 on 3a08c4d1); wired in `.github/workflows/test.yml`.
- Cost-circuit mixins: `src/cost_circuit/` holds no mixin; the circuit class inherits nothing; the boundary test file exists.
- Midnight clock: no local-day or UTC-day-as-exchange-day use left in `src/` (the only `utcnow` sites subtract two timestamps); the single clock and the test guard exist. The note's single-clock guard test file still does not exist, as already recorded below.
- Broker read error as "no stop": FIXED only by this change, see WRONG below.

Re-triage 2026-10-05 (each claim checked against 3a08c4d1): owner-alert senders LIVE (scripts only, not money, see its section); "Real boundaries" STALE; undefined names FIXED; cost-circuit mixins FIXED; midnight clock FIXED (its guard-test claim FALSE, see below); stop read FIXED. One genuinely live entry.

Pile B, STILL LIVE: zero as of the second 2026-10-05 pass (the owner-alert senders below are CLOSED; the `PromptFactsReviewMixin` line is STALE, see WRONG).
- ~~Owner-alert senders~~ CLOSED 2026-10-05: all script sends now go through `deliver_with_retry`; `tests/test_scripts_send_through_funnel.py` is ABSOLUTE over `scripts/` (probe exempt by identity). The earlier count of 8 was wrong: `check_item_deployment.py` was a ninth. Earlier text: root cause is that ops scripts build their own `TelegramNotifier()` and send once with no retry; the funnel guard looks for private messengers and `src/` bare sends, not this (about 13 sites, 2026-10-05 count; the guard's reach over `scripts/` was not traced). About 4-6 hours [estimate: one funnel call per site plus a scripts-scope guard]. Not money.
- ~~`PromptFactsReviewMixin`~~ CLOSED on main: no class of that name exists; `hold_prompt_facts_review` installs per-name delegates that build a `PromptFactsReview` per call; the one hole left was a COUNT-keyed guard, closed 2026-10-05 (below).

Pile C, NEEDS A LIVE RUN: zero. Nothing here is unmeasurable offline.

Write-ups found WRONG or stale on 2026-10-05:
- "Broker read error ... ALREADY FIXED / no caller reads the broker stop outside `read_stop`" was FALSE. `src/execution/pending_stop_drain.py` called the raw read, and an unreadable answer became "nothing resting, placing protection is safe", so the drain applied an owed level over a possibly TIGHTER live stop, moving a protective stop the way that loses more. Its docstring claimed it used the escalating read; the existing test pinned the defect as "behaviour unchanged". Fixed at the cause in this change: the drain now uses `read_stop`, and an unreadable stop keeps the owed row and applies nothing. Witness `tests/test_pending_stop_drain_unreadable.py`; the unreadable case fails on the old code.
- "Real boundaries still owed" / Pile B's `PromptFactsReviewMixin` line (written the same night) was STALE: all three named pieces are held parts on main with identity guards (`PortfolioConstructor.__bases__ == (object,)`; `PortfolioManagerAgent` MRO is seat, `LiveLimitPrompt`, `BaseAgent`; no `Review` class in `TradingPipeline.__mro__`). The ONE live hole was `tests/test_boundary_harness.py` keying the composed-pipeline-mixin set on a COUNT (`len(MIXINS) == 8`), which let a one-for-one swap compose a NEW mixin onto `TradingPipeline` unnoticed. Closed 2026-10-05: the set is now named by identity and compared against `origin/main` at check time (shrink-only, REFUSES when the trunk is unreadable); `test_swapping_one_mixin_for_another_is_caught` proves the swap is refused.
- "Owner-alert senders": "no guard forbids it" was stale. `tests/test_owner_alert_funnel_guard.py` and `tests/test_no_side_door_owner_alert_send.py` forbid a new bare send or messenger in `src/`. The six `src/` constructions are a probe default, default arguments, a document upload and the scheduler's routine report, not alert bypasses. The live remainder is the scripts.

## Owner-alert senders that bypass the retry funnel -- LIVE in `scripts/` only (verdict 2026-10-05, counted by SEND)

CLOSED 2026-10-05 (second pass). REPRODUCED first: the funnel guard passed on trunk (rc 0) while NINE bare `notifier.send(...)` calls stood under `scripts/` -- the eight below plus `check_item_deployment.py`, which the count missed; all nine grandfathered by rule 3's trunk delta. Root cause: each script sent once through `TelegramNotifier.send`, which has no retry and records no counted undelivered row. Rebuilt: every script send is `deliver_with_retry(notifier, text, kind=<script name>)` (same backoff policy and `owner_alert_undelivered` row as `send_owner_alert`; no P&L header, which belongs on money alerts only; category left unclassified exactly as before, so nothing is newly filtered). Guarded ABSOLUTELY by `tests/test_scripts_send_through_funnel.py`: under `scripts/` the only direct `.send(` is the manual probe, by identity, with no trunk read to go stale. Rule 3 of the funnel guard stays a delta for `src/`, `ops/` and the root; the three routine-report sends in `main.py` and the scheduler are unchanged.

The retry-and-record discipline was opt-in: it lived in `send_owner_alert`, so any code that built its own `TelegramNotifier` and called `send` got one attempt and left no trace of the loss.

NOTE ON THE TWO SIDES (reconciled by reading the code, 2026-10-05): the paragraph below was measured on trunk BEFORE this change and is true of trunk: there `TelegramNotifier.send` is one attempt. This change makes `send` itself the funnel (`src/notifier/send_funnel.py`, absent on trunk), so once merged the eight `scripts/` direct sends listed below get the retry and the counted undelivered row, and are no longer bypasses.
COUNT BY SEND, NEVER BY CONSTRUCTION, AND NEVER FROM A GREP OF MENTIONS: earlier counts (6, 16, 3, 5) were mentions. Read on 3a08c4d1: `src/` has zero alert bypasses (below, true). `scripts/` has 8 real `notifier.send(...)` calls on alert or report text with no retry (`TelegramNotifier.send` has no retry; the funnel `send_owner_alert` does): `check_stored_targets`, `check_unit_drift`, `check_board_hygiene`, `check_deploy_drift`, `refresh_pricing`, `desk_status`, `log_health_report`, and the failure alert in `alert_heartbeat` (the same file's other sends do use the funnel). `telegram_test.py` is a manual probe, not counted. The funnel guard passes because its direct-send rule is a delta against trunk, so these are grandfathered. Not money or live risk (ops monitors; a lost send is silent, not a trade effect). Size 4-6 hours [estimate: one funnel call per site, plus a scripts-scope absolute rule]; left, not fixed here.

REPRODUCED 2026-10-04 on main before changing anything: of the six `TelegramNotifier()` constructions in `src/`, the two cost-circuit ones and the ledger one already route through `deliver_with_outcome` (`src/cost_circuit/alert_outcome.py`), the watchdog one is a probe and the pipeline one is a CSV `send_document` -- but `src/scheduler.py` and `main.py` send the session summary and the startup message through a bare `send`, which retried nothing and recorded no undelivered row. The earlier sizing counted constructions; the bypass is the bare `send`, and the `scripts/` constructions share it.

CLOSED at the cause, not detected: `send` IS the funnel. One attempt is `TelegramNotifier.send_once` and the public `send` runs it through `deliver_with_outcome` (retry, then one counted `owner_alert_undelivered` row), so there is nothing left to bypass -- a caller cannot skip a method by calling that same method, wherever it built the notifier, `scripts/` included. No guard is needed and none was added. Both live in `src/notifier/send_funnel.py` (the body moved out of `transport.py`, which was at its size ceiling) and are bound onto the class.

A rehearsal drop now returns `SUPPRESSED` rather than `False`: settled, never retried, never recorded as an undelivered alert -- the same state a `TELEGRAM_DISABLED` drop has always had. `SuppressedSend` is falsy, so a caller testing the result is unaffected; two tests asserting `is False` were updated to say suppressed.

PROVEN TO BITE: `tests/test_send_is_the_funnel.py` builds a notifier directly and fails on main (one attempt, no durable row) and passes here (three attempts, one counted undelivered row), with a delivered send still attempted once and a deliberate suppression settled without retry.

NOT COVERED, deliberately: `send_document` (the P&L CSV export) and `probe` keep their single attempt -- neither carries an owner alert, and a retried file upload is a different question.

CORRECTED. This entry counted CONSTRUCTIONS of a `TelegramNotifier`, which is not the defect; the defect is SENDING around the funnel, and the two measurements differ. It also said "no guard forbids it", which was false: `scripts/owner_alert_funnel_guard.py` has existed and refuses three shapes of bypass — a file that builds the Bot API URL itself, a class outside `src/notifier/` exposing its own `send`, and a direct `notifier.send(...)` call the trunk does not already have.

Re-measured 2026-10-05 against `origin/main` by reading each site, not by grepping mentions: every owner-facing ALERT in `src/` goes through `send_owner_alert` or `send_owner_alert_with_outcome`, so the retry and the counted `owner_alert_undelivered` row apply to all of them. Zero alert bypasses remain. Three direct `TelegramNotifier.send` calls do exist outside the funnel — the scheduler's end-of-session summary, and `main.py`'s startup ping and one-shot session summary — and all three are routine reports rather than alarms, carry `CATEGORY_OPERATIONAL` or a `kind`, and still write their own `notifier_sends` row inside `send`. They are left as they are; the alert funnel's P&L header and alert retry do not belong on a routine report. There is no `build_default_notifier` on the trunk.

What was genuinely open, and is now fixed: the guard scanned `src/`, `ops/` and `scripts/` only, so the repository ROOT — where `main.py`, the live entry point, sends to the owner twice — was never measured at all, and a new bypass written there would have been reported clean. The guard now scans tracked top-level `.py`/`.sh` files as well, keyed on the funnel module's own path rather than on any count of permitted sites. Proved by planting a direct send in `main.py` (guard exits 1, naming it) and removing it (exits 0); `tests/test_owner_alert_funnel_guard.py` pins the root in the measurement itself.

## Real boundaries still owed (CLOSED 2026-10-05; the eight pipeline mixins are a separate, un-sized rebuild)

VERDICT 2026-10-05: STALE (re-grepped on 3a08c4d1: no `PromptFactsReviewMixin`; PM seat holds parts). Nothing in this entry is owed. What IS still a mixin is the eight composed onto `TradingPipeline` (risk gate, delever, intraday, prompt facts, research continuity, protection, exits, admission). That is the pipeline rebuild itself, never part of this entry. SIZED 2026-10-05 (not started, too big for one change): 8 mixins on `TradingPipeline` on 672b79e9, each to become a held part the way the PM seat's were (#1227 shape: one part built at import, collaborators read from the host per call, a bare-class boundary test each), about one change per mixin, 2-4 hours each [estimate from the four PM-seat conversions]; the identity guard above means the set can only shrink.

The position builder, the portfolio-manager seat and the prompt-facts review
chunk are under the ceiling but are not separable pieces. Same treatment.

STILL OPEN, measured 2026-10-02: `tests/test_boundary_harness.py` passes 12
tests but covers only the pipeline mixins; mixins remain in
`src/pipeline_prompt_facts_review.py` and
`src/agents/portfolio_manager/prompt_evidence.py`.

DONE 2026-10-04 for `PromptEvidenceMixin`: `PortfolioManagerAgent` no longer inherits it. `hold_prompt_evidence` builds one `PromptEvidence` and installs classmethod delegates on the agent (61 class-level test call sites counted, all still resolving, none rewritten); `tests/test_portfolio_manager_parts_boundary.py` builds and runs the part on a bare class. Three mixins remain on the seat (`DecisionGroundingMixin`, `RotationSectionMixin`, `CandidateRankingMixin`) plus `PromptFactsReviewMixin`.

DONE 2026-10-04 for the other three PM-seat mixins (#1227): `PortfolioManagerAgent(LiveLimitPrompt, BaseAgent)` is the whole MRO; `hold_candidate_ranking`, `hold_rotation_section` and `hold_decision_grounding` each build ONE part at import and hand its collaborators in live (`live_body` re-reads the agent class per call; `_macro_parse_failures` through `_HostState`; the alias table through `LiveMapping`). Witness `tests/test_portfolio_manager_parts_boundary.py::test_part_is_built_held_and_run_on_a_bare_class_with_no_agent` builds each part on a bare class with no agent, runs it, and asserts a collaborator swapped AFTER construction is the one that runs. No mixin class remains under `src/agents/portfolio_manager/` (`ranking.py` 46 lines, `rotation_section.py` 37, `grounding.py` 71 are holders, not mixins).

VERIFIED 2026-10-04 (isolation pass, re-run against main): 44 passed across `tests/test_portfolio_manager_parts_boundary.py` + `tests/test_boundary_harness.py`; MRO measured as `PortfolioManagerAgent, LiveLimitPrompt, BaseAgent, ABC, object`.

DONE 2026-10-04 for `PromptFactsReviewMixin` -- the LAST piece; this section is CLOSED. `PromptFactsMixin` (and so `TradingPipeline`) no longer inherits it: `src/prompt_facts/review/held.py` holds `PromptFactsReview`, a standalone part built with collaborators read from the seat at call time (`PromptFactsReview(db=..., broker=..., ...)`), whose five `_review_*` builders and ten shims moved AST-identical from the mixin (15 of 16 bodies; `_build_post_exit_reality` is a collaborator of the grading part, so the part reads it off the seat at the start of the call and stores it, never reaching back during execution). Each collaborator is read from the seat ONCE at the start of each delegated call (in `review_of(host)`), passed to `PromptFactsReview` constructor, and stored as an instance attribute; during body execution, collaborators are read from the part instance, not the seat. `hold_prompt_facts_review` (`src/pipeline_prompt_facts_review.py`, applied as the class decorator on `PromptFactsMixin`) installs one same-named delegate per review name, each of which calls `review_of(self)` to rebuild the part with current collaborators, then forwards to that part; nothing is cached on the instance. Witnesses: `tests/test_prompt_facts_parts_boundary.py` (built alone, run, collaborator swapped after construction is the one that runs) and `tests/test_boundary_harness.py` (no `Review` class in `TradingPipeline.__mro__`, held once per instance, swap on the pipeline reaches the part). Also DONE by then, not recorded above: the three remaining portfolio-manager mixins (`DecisionGroundingMixin`, `RotationSectionMixin`, `CandidateRankingMixin`) -- the seat holds all four parts via `hold_*` (`src/agents/portfolio_manager/__init__.py`, `held_part.py`) and its MRO is `PortfolioManagerAgent, LiveLimitPrompt, BaseAgent`. No mixin remains on either seat. **Removed 2026-10-16:** the post-exit reality block (`_build_post_exit_reality`, its `post_exit_reality` summary key, the reviewer prompt section and the `build_post_exit_reality` collaborator) is deleted: it fed the desk's own past exit outcomes back as an instruction (REBUILD_SPECIFICATION rule 3.14 forbids fitting to the desk's past record), its 2/12/120/5%/50% were made-up, and it mis-scored short covers. Mentions of it above are history.

(Before this change landed the fourth piece stood as REMAINING on main: `PromptFactsReviewMixin` was 100 lines of per-call shims over the five parts under `src/prompt_facts/review/` lifted by #1261, still inherited by `PromptFactsMixin`; the hold conversion below closes it.)

VERDICT 2026-10-02 (isolation pass): REPRODUCED (structural debt). Mixins remain: `PromptFactsReviewMixin` (`src/pipeline_prompt_facts_review.py`) and in `src/agents/portfolio_manager/`: `DecisionGroundingMixin`, `RotationSectionMixin`, `CandidateRankingMixin`, `PromptEvidenceMixin`; `tests/test_boundary_harness.py` passes (with the other two files run, 20 passed) but only covers pipeline mixins. The note named three pieces; the portfolio-manager seat actually has four mixins. Rebuild size: medium-large, five pieces.

---

# Resolved history

These findings have been addressed through code fixes or established as not defects. Preserved here for completeness and context.

### Nothing in the build checks for undefined names (CLOSED 2026-10-04)

A bare `_log` in the take-profit refusal branches raised `NameError` in a cold money path (fixed by #1102; `tests/test_take_profit_refusal_names.py` goes red on all three cases if a bare `_log.error` is put back). The class is now closed mechanically, not by care.

CLOSED: `scripts/check_undefined_names.py` (stdlib `symtable`, no new dependency) resolves every name in `src/`, `scripts/` and `main.py` and runs in the test workflow (`.github/workflows/test.yml`), so an undefined name fails the build before it can ship.

VERIFIED 2026-10-04 (isolation pass, fresh worktree off main b3734403, no code change): the guard exits 0 with `525 files checked, 0 finding(s)`, and no module under `src/` carries a star import, so nothing in the money paths is skipped. It is PROVEN TO BITE by `tests/test_undefined_names_guard.py`: a bare `_log.error` inside a function is reported, defining the name clears it, and a helper left behind by a lifted method body is reported in a method -- the exact shape of the known lift-the-body trap. A deliberate syntax error introduced into `src/pipeline_exits.py` also failed the guard (1 finding, exit 1) and the tree was restored green.

Blind spots, unchanged and accepted: attribute names, dynamically created names (`globals()[...]`, `setattr`), modules with a star import (skipped, currently none in `src/`), and module-level reads before binding.

The earlier text claimed "there is no ruff, flake8 or pyflakes ... and no script in `scripts/` resolves names, so the next one ships the same way", and sized the remaining work at 250-350 lines. Both were already false when this section was last read: the guard and its workflow edit had landed. The section was stale, not open.

### The cost circuit is eleven mixins, not eleven modules (CLOSED 2026-10-04)

Eleven of the circuit's nineteen pieces were mixin groups: smaller files, not
boundaries, buildable only as one object with one shared state.

CLOSED 2026-10-02 (fourth and fifth instalments): all eleven are standalone classes under `src/cost_circuit/parts/` with no base class and keyword-only collaborators, HELD as instances on `LLMCostCircuitBreaker` (`_hold_parts`, wired by `assembly.py`); every `breaker_*.py` shim is deleted and the class inherits from nothing. Three collaborators are handed in live rather than snapshotted (`notifier`, `_connect`, the owner-notify sentinel); see docs/ARCHITECTURE.md.

VERIFIED 2026-10-04 twice (main f85da78d, then 976ed3e9 in a fresh checkout, no code change either time): `tests/test_cost_circuit_parts_boundary.py` 38 passed; it CONSTRUCTS all eleven parts from stubs with no breaker behind them, exercises each, asserts `LLMCostCircuitBreaker.__mro__ == (LLMCostCircuitBreaker, object)`, asserts each former shim module raises `ModuleNotFoundError`, and asserts the live-collaborator contract. No `Mixin` name survives under `src/cost_circuit/`. Moved out of the open list because it sat there after being closed.

An earlier 2026-10-02 verdict judged the parts still inherited as shims; that was the one real gap and the hold conversion closed it.

### The midnight clock bug (CLOSED -- both halves, 2026-10-04)

Between 00:00 and roughly 00:16 Eastern, tests compare an exchange trading day
against the runner's local day and fail. Seven in the holding-discipline
intraday file, thirteen more in the cost circuit. They pass again at 00:20 ET.
Verified twice on 2026-10-02. This is the third or fourth instance of the same
class. It costs a full test round every time it fires and it reds every open
change at once, which is why it goes first.

DONE 2026-10-02: the holding-discipline half. Root cause was a module-level
`str(et_today())` stamped when the file is COLLECTED, compared against an
`et_today()` read when the test RUNS -- a suite that crosses ET midnight
between the two compares two different exchange days. Reproduced on demand by
shifting the clock (collect 23:58 ET, run 00:05 ET): 6 failed before the fix,
26 passed after, at the same simulated instant. The stamp is now read at run
time. A mechanical guard, `tests/test_no_local_day_as_exchange_day.py`, now
fails on `date.today()`, a naive `datetime.now()`, a UTC calendar day used as
a day, and an import-time clock stamp in a test, with a shrink-only baseline
of the offenders that already existed.

CLOSED 2026-10-04: the cost-circuit half. Its cause (Python ET day vs SQLite's own `'now'`) was already removed by the single clock in `src/cost_circuit/clock.py`; re-measured with the clock pinned to 00:01, 00:05 and 00:15 ET and with the real UTC day differing from the ET day: 0 failures in 180 tests. NOTE (corrected 2026-10-05, second pass): `tests/test_cost_circuit_single_clock.py` was cited as the guard and does not exist under that name, but the claim that NO test stops a second clock was WRONG. `scripts/one_clock_guard.py` (run by `tests/test_one_clock_per_decision.py`, 6 tests) is ABSOLUTE: a module that derives a day in Python and also dates rows with SQL `'now'` is refused, with `src/cost_circuit/clock.py` the single exemption by identity. Re-run on 672b79e9: rc 0. The defect is closed and guarded; only the cited filename was wrong. The measured result (0 failures in 180 tests) stands, but the guard implementation differs from what is documented. Earlier text kept below for history: the cost-circuit half. The original entry named thirteen failing tests but provides no test names. Reproduction attempts on 2026-10-02 failed by three methods: clock simulation with Python and SQLite moved together showed zero failures; CI runs covering 00:00-00:16 ET showed no cost-circuit test failures; and direct inspection identified no named failure. This entry must not be treated as a known defect until a specific failing test is named and reproduced.

FIXED 2026-10-04 -- cost-circuit half. The circuit already derives its ET day from one clock and the test helpers pin it; driven at 23:58 ET and 00:05 ET with BOTH the circuit clock and SQLite's own `'now'` moved together, all 139 cost-circuit tests pass, so no failing cost-circuit test exists to fix. The same sweep found the class still alive in production at two sites: the stream-handshake attempt budget keyed its day on the runner's local day (`date.today()`, four methods) and the credential-placeholder once-a-day marker keyed on the UTC day, so both rolled at the wrong hour. Both now ask `src.trading_calendar` (`session_date_key` / `et_today`), the same answer the first half used. Proof: `tests/test_exchange_day_in_stream_budget_and_credentials.py` (3 tests) drives 23:58 ET and 00:05 ET and fails on the old code (3 failed) and passes now. Test-file offenders of the same shape remain tracked by the shrink-only guard, not fixed here.

VERDICT 2026-10-02 (isolation pass): holding-discipline half -- ALREADY FIXED (module-level stamp now read at run time; `tests/test_no_local_day_as_exchange_day.py` guard passes, no import-time `str(et_today())` stamp remains in that file). Cost-circuit half -- NOT REPRODUCED (carried forward from the earlier investigation; not redone). The note names no test, so it should not be treated as a known defect.

### A broker read error reads as "no stop to adjust" -- FIXED

The stop read used to return nothing on ANY error, and the protection path
read nothing as "no stop" and skipped. Now there are three answers (found,
none, unreadable) in `src/execution/stop_read.py`, and unreadable is
escalated (owner ruling 2026-10-02): two retries with a short pause, then the
broker's full open-orders list, then the protection paths (ex-dividend shift
and deterministic trail) re-place the stop through coverage repair, which is
idempotent because the order key has landed; a stop that really was there may
be duplicated, which the owner accepted. A durable row and an owner alert say
what the desk actually did (placed, tried and failed, or only reported).
Reporting-only reads (prompt, evening proximity, reconcile) place nothing. The
prompt states "stop could not be read, do NOT treat as unprotected". A
non-numeric broker answer is unreadable: the real read only returns a number or
nothing, so anything else is a broken answer. Proven through the ex-dividend
caller: every read failing places the stop once; failing twice then
succeeding places nothing. All seven callers use it; the watchdog reconcile now
passes the database it was already given.

VERDICT 2026-10-02 (isolation pass): ALREADY FIXED. Fixed by #1085 (c1fdebc8), `src/execution/stop_read.py`. Proof: `tests/test_stop_read_unknown.py` plus `tests/test_evidence_gate.py` pass (149 passed); a grep finds no caller reading the broker stop outside `read_stop`; seven call sites (pipeline_exits x2, pipeline, pipeline_prompt_facts x2, pipeline_protection, stop_records) use it. Not reverted-to-prove: the pre-fix source was not re-run.
