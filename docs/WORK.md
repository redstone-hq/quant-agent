
# QAMC Current Work

## Active finish line

**Desk: ON (restarted 2026-10-09 by the owner; the 09:30 ET morning run executed).** This line is parsed by `scripts/work_queue.py`; change it, never add a second one.

**Owner-directed stabilization reset, 2026-10-06.** The Paper desk was OFF from 2026-10-06 until the owner restarted it on 2026-10-09. Do not run the secondary-account capture or build more rehearsal infrastructure as a gate to Paper beta. First correct this board's stale status, then verify the structural split and its tests, triage real defects, and resolve trade-governing arbitrary numbers by cause and impact. This is a work order, not a claim that any open item is fixed. `docs/OUTCOME.md`'s trading mandate and all deterministic protections are unchanged.

**Audit snapshot, not a standing count:** on 2026-10-06, GitHub had 0 open pull requests; the two original oversized files were 1,756 and 805 lines; this board held 20 numbered open items; the ledger classified 140 numbers as `arbitrary`, all with named settlement routes and none thereby justified. The old ranked defect list in `docs/phases.yaml` is empty, but it is NOT the current work queue. Re-measure these from GitHub, the tree, this board and the ledger before acting; do not copy an old count into a new decision.

### Session start — read this first

**STANDING PRINCIPLE — NO ARBITRARY NUMBERS, EVER.** Every trade constant comes from real data, a cited source, or the instrument itself — never a flat count, round % or a number that "sounds prudent". **Approval does not make a flat number non-arbitrary.** Mark unmeasured numbers provisional, never settled.

**STANDING PRINCIPLE — MISSING DATA IS A DEFECT IN THE PRODUCING STEP (owner 2026-09-17).** Find why a required field is blank and fix that step so it actually produces the data. Never invent. Never make skip/drop/ignore-and-continue the product. A drop-the-name quarantine is temporary. Item 78 is the current instance; the rule is not limited to it. Fuller statement: `docs/OUTCOME.md`.

**This file holds only open work.** Finished work is written up in `docs/INCIDENT_HISTORY.md` (append-only, each entry opening with one plain-language line) and then deleted here, together with its `## item N` block in `docs/board_notes/` and its NUMBER — the number alone, never a reason — added to the retired line. `tests/test_status_board.py` fails if this file passes 100,000 bytes, if one change grows it by more than the shrinking growth budget its current fullness allows (owner ruling 2026-09-17: recording a genuine new defect must not be blocked just because nothing is finished yet to prune — see `work_md_growth_budget` in `scripts/status_board.py`), if a self-declared-finished item is left sitting on the board, or if it loses an item number without retiring it. Ratified architecture decisions go in `docs/QAMC_REMEDIATION_SPEC.md` as a numbered phase, not here.

## DECISIONS PENDING — CI FAILS WHEN ONE GOES OVERDUE

**Do not delete a line to pass the build — decide it, then remove it in the SAME commit.** Format: `- [ ] DECIDE BY YYYY-MM-DD — question` (`test_no_pending_decision_is_overdue` parses it). It exists because a deferred decision was forgotten in 2026-08 and cost a zero-trade day.

**RESOLVED 2026-09-25 — the mandate is SWING (days to weeks), not a quarterly-horizon value book.** Decided by the orchestrator after an adversary run, per the 2026-09-18 ruling that this question does not wait on the owner. Reason: `docs/OUTCOME.md:75` already rules the desk's horizon "swing — days to weeks"; `config/prompts/tech_analyst.md:3` already treats its own 5-15d window as signal-validity, not holding period, with PM/position_reviewer owning the hold; holding period is an OUTPUT of thesis health, not a setting. `config/prompts/evening_analyst.md` and `config/prompts/meta_reflector.md` carried un-migrated rot from the original upstream value mandate (medium-long-term/quarterly-horizon framing) and have been rewritten to match. Detail and the exact prose diff: `docs/board_notes/` ("item 99").

**RESOLVED 2026-10-02 by the owner — which model runs the desk's trade-decision seat is CLOSED, and may not be reopened.** Owner, verbatim: "We already ran multiple, multiple tests on all of the LLMs. And that's the conclusion we came to. Close the door and move on. There's plenty of work to do." Settled state: the `portfolio_manager` seat runs `openai/gpt-5.5`, the risk seat deliberately runs a different model to keep the two independent, and the remaining seats run the cheap fast Gemini route. This line sat open for two days after he answered it, and reading it as unfinished has already cost two agent runs plus an adversary pass re-litigating a made decision, so `tests/test_model_seat_decision_closed.py` now fails the build if the question is filed again as a pending decision. Do not benchmark a seat model, do not propose a shadow or paid comparison run, and do not route cost-per-seat to him as a reason to revisit. The assembled evidence (the exam, the live per-call cost, the recommendation and what would make it wrong) is kept in `docs/INCIDENT_HISTORY.md`, together with the three findings that are worth keeping but are NOT grounds to reopen: the rubric never graded whether a pick made money, the demotions were 37 and not 17, and whether per-seat spend is recorded is unresolved and not worth resolving while this is shut.

**RECONFIRM AFTER A FEW DAYS LIVE — `max_calls_per_session: 40` (owner instruction 2026-09-03).** The cost circuit's runaway-loop defence, set from real data (worst complete session: 14 calls). A first number, not final. Once live sessions exist, re-pull `llm_budget_sessions.logical_calls`; raise it if a legitimate session gets close, never lower it on a hunch. History: `docs/INCIDENT_HISTORY.md`, "item 14".

## PM TEST GATE — GARBAGE IN, GARBAGE OUT

The PM model test means nothing until everything feeding the PM is clean; this gate blocks the model decision above. Cleared items are written up and deleted.

**The gate is EMPTY as of 2026-09-14 — every PM-gate item is closed, and the owner's condition (no model test runs while any gate item is open) is met.** Gate item 7 moved to the backlog as item 76.

<!-- END PM TEST GATE -->

**Alert design (owner rule 2026-09-02).** Every failure alerts in its OWN Telegram message, never bundled into a run summary; severity is carried in TEXT, never colour; deliberately not deduplicated — a still-broken seat keeps alerting.

**Rehearsal rig — parked, not a Paper-beta gate (owner 2026-10-06).** Do not run or expand it unless the owner asks about a concrete failure. `ops/rehearsal/` replays a session offline; it cannot validate a prompt rewrite, because it replays recorded answers into a changed prompt and passes regardless. Item 233 remains open and unproven, not silently completed.

**Production.** Mission Control is `https://ovh-vps.wallaby-bowfin.ts.net/cockpit/` (Tailscale Serve to the loopback API). **A `git checkout` on the box is not a deploy:** restart `quant-agent-api.service`, then confirm the hashed bundle filename the server returns matches the one under `src/api/static_cockpit/assets/`. Never record a production SHA here; read it: `sudo -n -u qamc git -C /home/qamc/quant-agent log --oneline -1` (and `status --porcelain` for config drift, expected empty).

**Standing cautions.** The pre-2026-08-27 LLM-spend baseline is contaminated by runaway loops — build no allocation conclusion on it. Model-market strategy (champion/challenger) is TABLED by owner decision 2026-08-27. The PM's "Proposal Conversion" block reads 21 days of history the 2026-09-02 reset erased: a quiet block means "no data yet", not "no stuck loops".

**Engineering setup.** Work as `ubuntu`, never as `qamc`. No venv in engineering checkouts: use `/home/ubuntu/projects/quant-agent/.venv/bin/python` with `PYTHONPATH` at the checkout root and the five dummy API-key env vars CI uses. Read the live box with `sudo -n -u qamc`. Log timestamps are **UTC**; the owner is **ET** — convert before quoting times to him.

**Working agreement.** Standing autonomy to execute the backlog without stopping at phase gates. Interrupt only for live-capital activation, new paid dependencies, secrets redesign, destructive infrastructure, or evidence a ratified decision was wrong. The grant covers executing, not expanding: report breakage that predates your task in plain language and let the owner decide. Never state a date or duration from impression — take it from `git log` and check the author (everything before **2026-08-09** is upstream `yebof`). The desk board (`docs/phases.yaml`, `/board`) is a source of truth; defects go on it. Orchestrate: delegate at task START (cheapest tier for docs and inventory, mid tier for bounded implementation, strongest for trading/risk logic), read diffs not whole files, never two writing agents in one worktree, and verify the single load-bearing claim of every agent report (check the test count; reproduce the cause) — a confidently wrong root cause was caught only that way on 2026-08-29.

**Beta operational ruling (owner 2026-09-19).** Deploys and changes to the live system are permitted during market hours while the desk is in beta. Automated fix-it sessions are approved to run after each health report, charged to the owner's allowance, with adversary review applied to every change.

**Operational facts.**
- Stage explicit paths; never `git add -A`. Never bare `git stash` — the ref is repo-global across worktrees; use `git stash push -m "<name>"` and pop by index, or a throwaway worktree.
- Merges go through GitHub's native merge queue (repo moved to `redstone-hq/quant-agent` on 2026-10-08): arm auto-merge with `gh pr merge --auto` and the queue re-tests and lands changes in batches. Never serialise merges by hand, never `--admin`, never force-push. Verify the settings with `gh api repos/redstone-hq/quant-agent/rulesets`.
- `gh pr view` works on this repo; if `gh pr edit` fails, use `gh api repos/redstone-hq/quant-agent/pulls/N -X PATCH` (body from a file: see `AGENTS.md`).
- Agents stall on polling loops: give every agent an explicit polling budget, or poll yourself.
- **A deploy installs the schedule.** `scripts/merge_and_deploy.sh` checks out `origin/main`, copies any changed or new `scripts/systemd/*.service`/`*.timer` into the qamc user's systemd dir, runs `daemon-reload`, enables what is not on `paused_units.yaml`, then restarts the API service (item 122 fix, 2026-09-24; before that the copy was by hand). **This is NOT silent:** `quant-agent-unit-drift.service` byte-compares every repo unit against the installed one and alerts on Telegram (it reported "in sync, 30 units" on 2026-09-18). Board item 122 carries the defect.
- **Never hand-resolve a conflict in `docs/WORK.md`, `docs/board_notes/` or `docs/INCIDENT_HISTORY.md`.** They use `scripts/resolve_doc_conflict.py` as a git merge driver (`.gitattributes` + `scripts/git_merge_driver_docs.sh`; one-time per-clone `git config` in README.md "### Install"). **Markers now mean one of two things** (changed 2026-09-23): no driver configured, or the driver REFUSED — a refusal writes markers plus a gitignored `<doc>.merge-refusal` beside the file carrying the reason, which is how you tell them apart; it used to write NOTHING and leave your own stale copy looking resolved. The resolver merges numbered ITEMS, refuses unless every item on either side survives exactly once, and stops on a number collision (a renumber, never a delete); refusing is correct. Hand-resolving is how five live items were deleted (item 68). The CLI (`--from-index`, or `--kind`/`--base`/`--ours`/`--theirs`/`--out`) works without the driver, including dry runs.

### Ordered backlog — RESUME POINT

**Priority order proposed from the owner's 2026-10-06 direction; it becomes ratified only when the owner merges this governance change. Work one slice at a time. Do not start the next merely because another is waiting on evidence.**

1. **Truth and scope (this documentation change).** Replace obsolete counts and priorities, preserve the no-restart hold, distinguish the empty historical defect list from the 20-item live board, and park item 233 without pretending its criteria passed. Do not create a new status document.
2. **DONE 2026-10-08 — structural verification (item 210).** Every plan step proven or superseded, none unfinished; written up in `docs/INCIDENT_HISTORY.md` (item 210, 2026-10-08).
3. **Safety and resource defects before cosmetic backlog work.** Triage the 20 open items into (a) actual risk/behavior defects, (b) built but awaiting live evidence, (c) owner decisions, and (d) documentation/read-side work. First inspect the documented unprotected-stop window (item 201 and the market-hours backlog), the fixed-time holiday/early-close schedule, and any CURRENT CPU/RAM/disk or paid-call runaway evidence. The 2026-10-04 disk audit (`docs/INCIDENT_HISTORY.md`, "Disk growth") found agent scratch and repeated test database copies, not production logs, as the large writers; test-copy cleanup landed, but lasting prevention of agent-scratch growth is NOT proven. Measure current usage and dispatch/cleanup practice before proposing a fix. Do not infer a live defect solely from an old note; do not restart the desk to manufacture evidence.
4. **Trade-governing numbers by causal priority (item 90, with 70, 177 and 186 where they overlap).** Start with numbers that can change an order, size, stop, exit or spend. For each, identify the producing step and missing measurement, then source, derive, reformulate/remove, or present a genuine owner risk-appetite decision. A route in the ledger is not a source, owner approval does not turn a picked number into a measurement, and relabelling is not resolution. Recompute the arbitrary count after each evidenced tranche; do not do a mass rewrite.
5. **Remaining board items.** After the higher-risk slices, work the current board by verified impact and dependencies; keep production-observation items open while the desk is held. Only then discuss a controlled Paper restart with the owner. No new capture rig, blanket test environment or speculative refactor is a restart gate.

Each slice ends with a short evidence report and its unresolved risks. Use dedicated, non-overlapping PRs and the existing required CI; no repeated CI without a changed head or a concrete failure. Keep the owner-facing board notes aligned with any item status change.

## THE FUNNEL QUEUE — why trades do not happen, ranked by measured cost

**Top of the backlog. Work the PRIORITY ORDER above; do not reorder from intuition.** The original census items (ranks 1-8) are all retired and written up; the measured census that ranked them is in `docs/INCIDENT_HISTORY.md`.

**55. What IS a structural level: swing-point bars and zone width? OPEN, filed 2026-09-13. [1 of 6 ticked; 3 boxes are constraints; BLOCKED ON A LIVE EVENT: enough positions opened and resolved under the level recording to read the desk's own record.]** Touch count is settled and pinned by a test: two touches, sourced (Tsinaslanidis 2012) — do not tighten it.

DONE WHEN:
  - [x] BUILT 2026-10-01, and it replaces the sweep below as the route: the desk RECORDS what every stop was actually based on and what the market then did with that level — `stop_level_basis` on the `trades` row (level price and kind, touch count, pivot window and confirmation span, zone edges and width, signed stop-to-level and entry-to-level distances, written for stops with NO level behind them too, which is the control), plus `level_max_penetration` and `level_closest_approach` accumulated while the position is open
  - [ ] enough positions have opened and resolved under that recording for the question to be asked of the desk's own record rather than of argument — the count that is "enough" is stated by whoever reads it, not pre-chosen here
  - [ ] the reading that closes this is a FALSIFICATION: whether level-backed stops fared any differently from stops with nothing behind them, and whether a level's touch count, confirmation span or zone width separated the outcomes at all. If they did not, the current definition is wrong and the threshold-free zone (the span of the pivot bars themselves, already written up in `docs/board_notes/`) is the replacement, because it needs no constant
  - [ ] NEVER by sweeping this record for a better pivot window or zone width — fitting a number to this desk's own trading history is barred outright, and that bar is written into `src/data/levels.py` and `src/storage/db.py` beside the recording itself
  - [ ] the two-touch minimum is left exactly as it is: sourced (Tsinaslanidis 2012, 733 US stocks / 20 years) and pinned by a test
  - [ ] SUPERSEDED, do not restart: the published-bounce-test sweep over tolerance 0.5/1/2/3/5% and window 3/5/10/25. Three measurements of the SAME eleven-position baseline returned 0, 1 and 4 level-backed stops, and the reworked clustering offered on that basis (PR 880) measured as a net LOOSENING — the opposite of its intent. It is HELD and will not be merged. A quantity that unstable cannot govern money, and a fourth re-measurement is not the answer; the recording above is
detail: docs/board_notes/item-055.md

**63. `signal_weight` cannot say "attend, sign reversed" — OPEN, from item 52. [3 of 6 ticked; 81 buildable; 84 MEASURED 2026-10-04 and ticked; 82, 86 stale.]**

DONE WHEN:
  - [x] TRACED 2026-10-01, the prerequisite above is ANSWERED and the cause is not the parser: …(rest: docs/board_notes/item-063.md)
  - [x] RECORDING BUILT 2026-10-01 and NOT YET PROVEN IN PRODUCTION: [full text: docs/board_notes/item-063.md]
  - [x] 2026-10-01, THE JOIN IS BUILT and run: `src/insider_sale_measure.py` joins each census row's reference date to the desk's own daily bars at the two forward windows the desk already measures on (the evening analyst's next-day and five-session scorecard), excluding and counting every row the bars cannot resolve rather than substituting one. [full text: docs/board_notes/item-063.md]
  - [ ] until one of those exists a sale stays NEUTRALISED at 0 and no agent picks the boundary number — the standing no-arbitrary-numbers and no-fitting rules settle that, this is not an appetite dial

(prose moved: docs/board_notes/item-063.md)
detail: docs/board_notes/item-063.md

**70. One underived `1.0` does two exit jobs — OPEN, filed 2026-09-14. [7 of 15 ticked; the two SOURCE boxes below are actionable, and neither may be closed by inventing a value; the over-refusal re-measure follows whichever change lands.]** The noise-band ATR multiple sets when an adverse move stops being noise and is reused as the margin in the structural-protection check; a separate absolute minimum stop multiple, also 1.0, sets how tight a stop may be.

DONE WHEN:
  - [ ] the noise-band ATR multiple carries a published measurement of the quantity it actually bounds — the adverse move at which a move stops being ordinary daily wobble — or a named derivation, recorded in `config/number_ledger.yaml` with that source
  - [ ] the absolute minimum stop multiple carries its OWN independent source or derivation, as a separate ledger entry: the two may not be collapsed into one shared constant just because the digits both read 1.0
  - [ ] the measured over-refusal is re-measured after whichever change lands, against the same recorded exits (today: 7 of 8 discretionary exits the reviewer approved were blocked as "too small a move")
  - [ ] neither value is retuned to make sales easier or harder in the same pass — how readily the desk should block a sale at all is the owner's appetite and is NOT this item
  - [x] 2026-09-26, PARTIAL: the SPLIT is built and the research is recorded (docs/INCIDENT_HISTORY.md, 2026-09-26). …(rest: docs/board_notes/item-070.md)
  - [x] 2026-10-04, THE LEDGER STILL RE-COLLAPSED THEM IN PROSE, AND NOW DOES NOT (`config/number_ledger.yaml`, `src.risk.exit_guard.TREND_CONFIRMING_CLOSES`; …(rest: docs/board_notes/item-070.md)
  - [x] 2026-09-30, DONE (record truth, no behaviour change): the refusal log asserted the move was inside the band without disclosing that the band width came from a floored/defaulted session count; …(rest: docs/board_notes/item-070.md)
  - [x] 2026-10-01, BOTH DERIVATION ATTEMPTS ON THE BREAK MARGIN ARE SPENT AND BOTH FAILURE REASONS ARE WRITTEN DOWN (`config/number_ledger.yaml`, `derivation_attempts` on `src.risk.exit_guard.BREAK_CONFIRMATION_ATR_MULTIPLE`). …(rest: docs/board_notes/item-070.md)
  - [x] 2026-10-01, THE SETTLEMENT RECORDING IS BUILT for the break margin (`src/risk/exit_guard.py`, `check_structural_protection`). …(rest: docs/board_notes/item-070.md)
  - [x] 2026-10-04, THE NOISE BAND'S SETTLEMENT RECORDING WAS CENSORED BY CONSTRUCTION, AND IS NOT ANY MORE (`src/pipeline_exits.py`, `src/risk/exit_guard.py`; …(rest: docs/board_notes/item-070.md)
  - [x] 2026-10-05, THE SEPARATION IS NOW GUARDED AT ALL FOUR NAMES, INCLUDING THE NEWEST (`tests/test_atr_multiple_separation.py`): `FALLBACK_PROTECTION_ATR_MULTIPLE`, split out of the noise band on 2026-10-04, was pinned only by its own call-site test and by NOTHING in the four-way separation guard, so its ledger row could have been re-tied to the noise band or re-marked sourced without any test noticing — the one layer at which a re-collapse has actually happened here before. The guard now covers all four 1.0 multiples (noise band, break margin, fallback protection margin, absolute minimum stop) for value, export, own-ledger-row and `status: arbitrary`, and a new case fails if any exit-guard row declares `derived_from` another exit-guard row, which is the collapse one layer up: deriving one would silently move the other while every value assertion still passed. Proven red by adding that `derived_from` to the fallback row and green after restoring it; 12 tests pass across the two separation files. RECORD-TRUTH ONLY — no constant moved, no ledger row changed, and nothing about when the desk sells moved.
  - [ ] STILL OPEN and NOT this item's to close: the absolute minimum stop multiple named in the second criterion above is the same number item 90 owns (min-stop ATR re-derivation), which is in flight elsewhere; it was deliberately not touched here to avoid two agents moving one live-money constant.

(prose moved: docs/board_notes/item-070.md)
detail: docs/board_notes/item-070.md

**75. Profit-taking: whole position answered by the alignment exit; residue is the partial trim and the target's one live effect — OPEN, filed 2026-09-14. [3 of 6 ticked; the rest are constraints, and BLOCKED ON A LIVE EVENT: a record of trims under the alignment exit has to accumulate before any trim fraction is derived.]**

(prose moved: docs/board_notes/item-075.md)

(prose moved: docs/board_notes/item-075.md)

DONE WHEN:
  - [x] the target's one remaining live effect is stated wherever the target is shown to the owner, and pinned by a test — …(rest: docs/board_notes/item-075.md)
  - [x] the desk records, for every open position every session, the alignment-exit reading it already computes (how far below the last mark price has closed, in that name's own ATR) EVEN WHEN it does not trigger an exit — built 2026-10-01, `alignment_exit_readings`, RECORDING ONLY and UNPROVEN until a live session writes a row
  - [x] the owner is no longer told the target does anything — done 2026-10-01, after PR 928 (item 212) merged: …(rest: docs/board_notes/item-075.md)
  - [x] that record has been read once, and the answer written into `docs/board_notes/` (item 75): …(rest: docs/board_notes/item-075.md)
  - [ ] no trim fraction is chosen before that record exists; two derivations were attempted on 2026-10-01 and both failed, and the reasons are written down in `docs/board_notes/` (item 75) so neither is retried blind
  - [ ] nothing here is fitted to the desk's own trading record, and nothing ships alone
detail: docs/board_notes/item-075.md

**78. Delete the blank-falsifier isolate once Tech and the PM demonstrably produce a real falsifier — DEFECT (patch), instance of the missing-data standing principle. [BOARD STATE: 2 of 3 ticked (the never-blank path is BUILT; the blank-rate condition CLOSED 2026-10-09, blanks are neutral-only by design); the other box need live-session proof and a live-database measurement, which the desk running again since 2026-10-09 makes actionable now.]** The isolate is live and declares itself TEMPORARY: `_isolate_empty_soft_exit_entries` (`src/pipeline_stages.py:2817`) drops any constructed BUY/SHORT whose falsifier is blank. Heal outcome now recorded durably (recording only, 2026-10-01); the "68% blank" reading was wrong: blanks occur only on neutral ratings, where the prompt asks for none (measured 2026-10-09); the isolate stays until the live-session box is ticked.

DONE WHEN:
  - [x] the never-blank path is live: a falsifier blanked by a later wipe is healed back from the sentence the model already wrote, the seat is re-asked once (paid), and a still-blank name is REFUSED before the book — never invented, and never with skip-and-continue as the product
  - [ ] LIVE-BLOCKED, the same shape item 86 was before a live log line retired it on 2026-09-26: `_isolate_empty_soft_exit_entries` (`src/pipeline_stages.py`) is deleted only once a real live session records the seats filling the box, and the item stays OPEN until a live session proves it
  - [x] CLOSED 2026-10-09, the blank-rate condition was a misreading: measured on production agent_logs since 2026-09-25, the technical seat leaves `thesis_invalid_if` blank ONLY on NEUTRAL ratings (231 of 231), where its prompt says to leave it empty, and fills it on every buy/sell/strong rating (173 of 173); the earlier 54-68% figures counted neutrals. Detail in the note.
detail: docs/board_notes/item-078.md

**90. Unsourced trade-governing numbers — the GATE now exists; re-deriving the numbers does NOT. TIER 1, PARTIALLY built 2026-09-18, item stays OPEN. [BOARD STATE: 0 of 11 ticked; the routeless-rows box is DONE (0 routeless, re-measured 2026-10-04), the remaining boxes are the arbitrary rows themselves, 134 and 142 settle only from live closed trades, which accrue now the desk is running again, the rest are status narrative; sourcing the arbitrary rows is actionable.]** Detail in the note. Half one (the gate) landed 2026-09-18; half two is untouched and NONE of the DONE WHEN boxes below is ticked.


DONE WHEN:
  - [ ] 2026-10-01, SECOND TRANCHE (pipeline): all 18 routeless `arbitrary` rows under `src.pipeline.TradingPipeline` gained a settlement route and no value changed; routeless falls 130 -> 112, `arbitrary` stays 136, and both failed derivations per row plus the recording spec the fourteen prompt-evidence windows point at are in `docs/board_notes/` item 90 — item STAYS OPEN.
  - [ ] 2026-10-01: the CLASSIFICATION is now mechanical and ratcheted, and it says the item is further from closing than the status field implied. [full text: docs/board_notes/item-090.md]
  - [x] REMAINING WORK, named by the check rather than by prose: ROUTELESS ROWS ARE ZERO — `classification()["unclassified"]` is empty on main (re-measured 2026-10-04 on main `2565d463`: [full text: docs/board_notes/item-090.md]
  - [ ] the only row in state 3 today is `src.config.RiskConfig.min_stop_atr_multiple` (2.5), whose settling recording was BUILT on 2026-09-30 (per-closed-trade entry stop, basis and excursions, `src/storage/db.py`); its value is untouched and the recording is for FALSIFICATION only.
  - [ ] state 3 (a named recording) holds 121 rows re-read from `classification()` on 2026-10-04, among them `src.config.RiskConfig.min_stop_atr_multiple` (2.5) and the four ceiling rows item 186 routed; [full text: docs/board_notes/item-090.md]
  - [ ] THE SETTLING RECORDING IS HALF FILLING, re-measured against the production database on 2026-10-04: [full text: docs/board_notes/item-090.md]
  - [ ] the four ceiling routes added on 2026-10-01 are `state: specified`, not built, and belong to item 186's tranche; they were READ and left alone here, and none of them is a study over this desk's own closed trades (the floor's route is per-closed-trade but asks only whether the floor was VIOLATED, which is falsification and not fitting — checked explicitly rather than assumed).
  - [ ] half two: every `status: arbitrary` row in `config/number_ledger.yaml` is sourced, measured, owner-ratified as appetite, or reformulated away, and `MAX_ARBITRARY_ENTRIES` — an EQUALITY, not a ceiling — reaches zero
  - [ ] HALF TWO IS SPLIT INTO TRANCHES, EACH WITH ITS OWN CRITERIA: [full text: docs/board_notes/item-090.md]
  - [ ] half one is already DONE (2026-09-18): the ledger gate exists and the build fails on an unsourced trade-governing number. Its honest limit stands recorded — it proves a reason was WRITTEN, never that the reason is TRUE — and that limit is not something this item can close.

(prose moved: docs/board_notes/item-090.md)
detail: docs/board_notes/item-090.md

**177. Paid intraday tick: trigger, cadence, held book are ONE decision, filed 2026-09-23. [3 of 4 ticked; BLOCKED ON A LIVE EVENT: enough live mover rows carrying `move_atr=` to test the ATR-relative threshold.]** The trigger decides whether a tick is paid, the cadence how many, the held book what a paid one costs [measured 09-21/22; `docs/INCIDENT_HISTORY.md`].

DONE WHEN:
  - [ ] all 3 leave `status: arbitrary`, `MAX_ARBITRARY_ENTRIES` falls by 3 — NOT MET, and now precisely blocked rather than merely unstarted. `move_threshold_pct` cannot be sourced yet: the desk's own 253 recorded selections show the flat threshold does not discriminate (median move 3.50% when a BUY/SHORT followed, 3.67% when nothing did; the 5-7% band produced zero orders from 51 selections), so re-picking it has no basis, and the ATR-relative form the ledger's own open question asks for was **unmeasurable because the denominator was never recorded**. That is fixed here — a mover's row now carries `atr_pct=` and `move_atr=` alongside `move_pct=`, from bars the scan already paid for, no behaviour changed — so the row can be sourced once the data exists. `max_candidates_per_scan` and `cooldown_hours` are owner-appetite, not research.
  - [x] the cadence ledgered and test-covered — the "unledgered" half was STALE: …(rest: docs/board_notes/item-177.md)
  - [x] every intra-preamble job on its own schedule — CLOSED 2026-10-01. …(rest: docs/board_notes/item-177.md)
  - [x] spend and actions re-measured — 2026-09-26, against the production cost circuit read-only. …(rest: docs/board_notes/item-177.md)
detail: docs/board_notes/item-177.md

**186. Risk ceilings are made-up — filed 2026-09-25. [5 of 8 ticked; BLOCKED ON A LIVE EVENT: evidence the desk does not record yet: the short-side haircut needs adverse overnight gaps on closed shorts (0 of 5 shorts ever taken carry one), `short_gap_risk_multiple` needs stored daily bars]**

DONE WHEN:
  - [x] the three already-ratified ceilings (25 / 90 / 40) stay ratified, and the remaining three are researched to a definite verdict rather than left unexamined
  - [x] WITHDRAWN 2026-09-30 — the pairwise-correlation appetite question is moot: the cutoff is removed, not set. Cluster membership is now read from the book's own correlation-distance tree (Mantegna MST cut at its widest gap), per the owner's ruling that risk tolerance is never a global dial. Still transitive, still rationing only.
  - [ ] the short-side haircut CLOSES ON RECORDED EVIDENCE, NOT ON A THIRD DERIVATION. — full text: docs/board_notes/item-186.md
  - [x] RULED OUT 2026-10-04, not pending: this was filed as an owner-appetite dial and the owner has now answered it more than once -- "if the mark — full text: docs/board_notes/item-186.md
  - [x] 2026-09-30 OWNER RULING APPLIED: risk is never a global dial, so no appetite number on this item is routed to the owner any more; each remaining ceiling is either replaced by a per-name read or recorded as blocked with its blocker named. Both previously routed questions are WITHDRAWN, not pending
  - [x] `short_gap_risk_multiple` (1.5) is DELETED, not re-derived — owner ruling 2026-10-04: "A short is not riskier than long. 1.5 is a made up number so throw that out completely... A short should be treated the same as a long, no different math no different behavior." The constant, the config fields, the ledger rows and the application site are all gone, so there is no neutral dial left to re-tune
  - [x] the queued-earnings BUY clamp (5% of the book) stops being a global share — — full text: docs/board_notes/item-186.md

  - [x] THE RECORDING IS BUILT, 2026-10-02 — — full text: docs/board_notes/item-186.md
  - [x] the portfolio and cluster ceilings (25 total at-risk, 90 terminal sector and its constructor mirror, 40 cluster share) each end in a definit — full text: docs/board_notes/item-186.md
detail: docs/board_notes/

**187. FRED fetch reliability — the chronic `fetch_deadline_exceeded` failure and required series left un-fetched — OPEN, filed 2026-09-25, carried out of item 175. Item 175 covered the weekend/holiday overdue-date roll; this is the separate, still-open half. Detail: `docs/board_notes/` ("item 187"). [BOARD STATE: 2 of 4 ticked; both open boxes read real runs (a morning open with full FRED coverage, and the deadline-exceeded rate), which exist again since the 2026-10-09 restart, so reading them is actionable.]** Every FRED failure in the retained log is `fetch_deadline_exceeded`; 4 of 12 runs reached full coverage, worst 5 of 15 [measured 09-17..23]. Owned by the approved fetch redesign.

DONE WHEN:
  - [ ] the `fetch_deadline_exceeded` rate is understood and either brought down or shown to recover cleanly inside the existing time ceiling, measured against real runs rather than a healthy mid-morning batch
  - [x] the FRED SERIES half is fixed and live: fair-share reserves plus the pre-open series cache; the deployed box ran 2026-09-30 with no series skipped.
  - [x] the EVENT-CALENDAR half is fixed here: the seven `/fred/release/dates` calls move off the trading path onto the existing pre-open prefetch timer and are served from `data/macro/release_schedule_cache.json` at the open; a release in neither cache nor wire stays a named failure and is never defaulted.
  - [ ] ONE morning open (N = 1, the number this item already stated) recorded in the production table `fred_fetch_coverage_runs` with `full_coverage = 1`: all configured series returned, `series_not_attempted` empty, and every configured release returned with `releases_from_cache` equal to `releases_configured`. Check: `SELECT * FROM fred_fetch_coverage_runs ORDER BY id DESC`. Rows exist only from the first open after this deploys; a row with `full_coverage = 0` does not count, and a missing row is not a pass.
detail: docs/board_notes/item-187.md

**201. Rest of the cancel+resubmit stop path — filed 2026-09-30. Detail: `docs/board_notes/` ("item 201"). OPEN: no production proof of a two-leg amend. [7 of 9 ticked; BLOCKED ON A LIVE EVENT: a fractional position's two hybrid stop legs both amending in place in production.]** The ex-dividend shift and the trailing re-price now share BOTH the measured-safe shape test and the failure classification, amend every resting leg in place, confirm each replacement id, and record the per-leg outcome as a durable row; a partial or an unanswered amend carries no order id, so nothing is written back and the owner is told. 2026-10-02: stop-LIMIT, bracket child and whole-share coverage repair now amend in place; a FRACTIONAL quantity change and lot consolidation still cancel, each recorded as a `stop_unprotected_window` row.

DONE WHEN:
  - [x] each remaining cancel+resubmit stop path is either converted to an in-place amend, or documented as genuinely unable to amend — converted: [full text: docs/board_notes/item-201.md]
  - [x] the two paths cannot drift on the FAILURE branch — one shared `_amend_one_stop_price` classifies every outcome as amended (confirmed id and a live status), refused (the broker ANSWERED 400/404/422 or a dead status, so the original is still resting) or unknown (no answer, so nothing is cancelled AND nothing is stated about where the stop is)
  - [x] a dead order replacement is never read as "the original is still resting" — the book is re-read and the leg is classified refused, amended, naked or unknown from what is actually there; a naked leg says UNPROTECTED, alerts the owner and leaves coverage repair to place the stop
  - [x] a straddle is healed ONLY where this desk's own amend created it — the legs a partial amend failed to move are retried EXACTLY ONCE, at that proposal's own intended level, inside the trailing gates (ratchet floor, tightening cooldown, no proposal on a stalled price). [full text: docs/board_notes/item-201.md]
  - [x] a partial shift cannot be recorded as a full one — a partial, a refusal and an unknown all return `id=None`, which `accepted_stop_order` rejects, so no level is written back and no TRAIL_STOP row is filed; the owner gets a plain-words alert and a durable row naming which legs moved
  - [ ] five things this item ASSUMES are still unverified and must not be built on as established: [full text: docs/board_notes/item-201.md]
  - [x] a naked leg does not claim a repair this session cannot make — coverage repair runs EARLIER in the same position review than the trails and the ex-dividend shift, so the code and this item both say the gap persists until the NEXT intra sweep, which is why the owner is alerted rather than merely logged
  - [x] a flat position is never reported as unprotected — a replacement rejected because the original already filled leaves an empty book, so the position is re-read and reported as flat; a still-pending replace can look the same from one read, and the code says so and deliberately reports the loud direction
  - [ ] production evidence shows a fractional position's two hybrid legs BOTH amending in place. [full text: docs/board_notes/item-201.md]

(prose moved: docs/board_notes/item-201.md)
detail: docs/board_notes/item-201.md

**208. Item 18's three residuals — filed 2026-09-30. Detail: `docs/board_notes/` (item 208). OPEN. [3 of 5 ticked; BLOCKED ON THE OWNER: a provider-side spend cap he sets or accepts, and a paid benchmark run he has forbidden unless he asks.]** One changes what the ranking seat decides, one is an account setting outside this repo, and one cannot be closed by building at all.

DONE WHEN:
  - [x] (a) DONE 2026-10-01 — decision recorded: neither joins the composite (see docs/INCIDENT_HISTORY.md 2026-10-01); originally: a recorded decision, in `docs/INCIDENT_HISTORY.md`, on whether reward:risk (today a within-tier tiebreak) and net evidence (unused) join the ratified composite score — this is engineering under doctrine, not an owner call; per-seat sizing weights stay refused either way
  - [ ] (b) the OpenRouter key carries a provider-side spend cap, or its absence is recorded as accepted — the account is outside this repo, so it closes on an observation in the provider console, never on a test
  - [ ] (c) BLOCKED and cannot close by building — the BUY-eligibility section reorder needs a paid benchmark run the owner has forbidden unless he asks for it (same blocker as items 76 and 77); it stays open and untouched until he raises it
detail: docs/board_notes/item-208.md


DONE WHEN:
  - [x] (a) DONE 2026-10-01 — gate REMOVED, not replaced; give-up and falsification recorded in `docs/INCIDENT_HISTORY.md` (2026-10-01, items 212/208); originally: a recorded decision, in `docs/INCIDENT_HISTORY.md`, on what enables a range position's structural trail once PR #853's alignment exit has landed — either the alignment reading itself replaces the target gate, or the gate is removed and the reason the entry stop alone suffices is written down.
  - [x] (b) DONE 2026-10-01 — verified live on `src/risk/trailing.py` (`reference_target` unread; Type A falls through to the same structural/chandelier candidates) and the one call site in `src/pipeline.py`; originally: the chosen answer is live for Type A entries and a range position between entry and its target is observably protected by something that reads off the instrument, not by an unsourced level.
detail: docs/board_notes/


**218. Losing geometry is REFUSED — ruled 2026-10-01; one half open. [6 of 7 ticked; BLOCKED ON A LIVE EVENT: post-ruling buys accumulating so the owner's question can be read from live data; listed in docs/MARKET_HOURS_BACKLOG.md.]** His words: "For now, let's refuse a bad risk reward ratio. …(rest: docs/board_notes/item-218.md)

DONE WHEN:
  - [x] the OWNER rules on whether an arithmetically losing entry may ship at all — ruled 2026-10-01, refusal, superseding the "wide stop is answered by smaller size, never refusal" response for this case
  - [x] the refusal is built at the accept-or-decline point on the trade's measured reward and risk, refusing outright with no resize and no change to any stop, target or trailing behaviour
  - [x] every guard test that encoded the superseded ruling is AMENDED in the same change with a comment naming the 2026-10-01 ruling, rather than deleted to go green
  - [x] the refusal is RECORDED durably per symbol with the measured ratio, so "see if that improves the desk purchases" can be answered from data rather than from impression — a `trade_refusals` table, one row per refused name, every quantity in its own column and no English in the record
  - [x] the BOOK-level effect is stated rather than denied: the refusal fires before risk rationing, so refusing one name does enlarge the survivors' share of the session at-risk budget
  - [x] evidence columns NULL on production entries [measured 2026-10-01, read-only: …(rest: docs/board_notes/item-218.md)
  - [ ] the owner's own question is answered from live data: after enough sessions under the rule, whether desk purchases actually improved — needs post-ruling buys to accumulate before anyone can say, and this item does NOT retire until that reading exists
detail: docs/board_notes/


**224. The desk records no realised sector weights, so concentration can only be guessed before the fact and never read after it -- filed 2026-10-01 from item 221. [BOARD STATE: 0 of 1 ticked; the recorder is BUILT; the POPULATING proof needs a live session, which runs again since 2026-10-09, so reading it is actionable.]** Item 221 established that the pre-decision preview cannot project a sector mix at all, because sizing depends on a PM target that does not exist when the preview is built; what the desk could record instead, and does not, is the sector weights of the orders the constructor ACTUALLY built, once per run. Without that row nobody can say afterwards whether a session concentrated the book or not. Detail in `docs/board_notes/item-221.md`.

DONE WHEN:
- [ ] one durable row per run carries the realised `(sector, side)` weights of the orders the constructor built that session, written from executable product code with its call site named, and classified POPULATING rather than UNPROVEN against a real session -- UNPROVEN 2026-10-02: the recorder is built (call site `_record_realised_sector_weights` in `DecisionStage`) but no row count has been independently read from the live store; the quoted 3 rows were not re-read by anyone else

detail: docs/board_notes/item-224.md

**228. The owner cannot see which holdings are drifting toward the chopping block -- OPEN, filed 2026-10-02; the panel is built, the live confirmation is not.** OWNER RULING 2026-10-02 (verbatim, on the rule beating any exemption): "I think the rule should win. Otherwise, things become inconsistent. But will I be able to know every day looking at the chart or some indicator that it's going on the chopping block soon? That should be for all stuff. A heads up would be nice. Uh, nothing more than that, because I still want to keep the rule of autonomy. For the desk." The margin recorder writes a run-scoped `rotation`/`margins` row each session that nothing in the trading path reads. Built beside item 219's Pruning Pass panel: `GET /chopping-block` (`src/api/routes_chopping_block.py`, read-only) lists EVERY holding the latest pass examined, healthy ones included, with whether it clears the desk's own entry bar, which way it has been moving, and the real rule it fails on. VISIBILITY ONLY: nothing in the trading path reads it, it delays and vetoes nothing, Telegram stays muted. No threshold, danger band or day-count was chosen: standing is categorical and direction is "since when, and what it was before".
DONE WHEN:
  - [x] every holding the latest pass examined is listed, healthy or below the bar, from the durable `rotation`/`precheck` rows
  - [x] each below-bar name carries the entry rules it fails on in plain words, or says the record does not hold them -- never a bare name
  - [x] direction is derived from recorded passes (slipped / recovered / steady, and since when) with no cutoff chosen
  - [x] the route is read-only and passes the dashboard-cannot-trade guard
  - [x] the margin each holding sits from failing each rule that has a distance is recorded per session (`src/rotation_margins.py`: R2 in rating steps from neutral, R5 in independent net-evidence points above failing) and shown with day-on-day direction; R3, R6 and R7 are yes-or-no and have none
  - [ ] a production session is observed writing the margins row and feeding the panel with real rows, and the margin recorder's R5 value is checked against the live eligibility reasons -- proven by test only until a production session since the 2026-10-09 restart is read
  - [x] reasons for below-bar names that were not cut come from item 219's dispositions row (PR 1108, merged 2026-10-02); proven by a writer-to-panel round-trip test
detail: docs/board_notes/item-228.md

**227. A seat's read carried no record of WHEN or in WHICH run it was taken, so "is this evidence fresh?" could only be inferred -- filed 2026-10-01. [BOARD STATE: 5 of 6 ticked; the last box needs one observed production session, which exists again since the 2026-10-09 restart, so reading it is actionable.]** The evidence gate has classified every seat as fresh / carried / absent since 2026-09-18, but the classification was stamped with nothing: no run id, no timestamp, and no age for a carried answer. That was tolerable while the disclosure only printed a line to the owner. It stopped being tolerable on 2026-10-01, when the owner ruled that any holding failing the desk's own fresh-entry bar is SOLD and that the test is re-run several times a day -- the half-hourly `intra_check` re-reads the technical seat and carries the rest, so a sell could be taken against a reading made before the market opened and nothing in the record would say so. Measured read-only against the production database 2026-10-01: `intra_check` is 63% of lifetime model spend and produced 37 of the desk's 80 trades, so this is where most decisions are taken. This item is the RECORDING, not a rule: no freshness threshold, no expiry window, no decision gated on any of it. A cutoff would be an invented number and is the owner's call, not this item's.

DONE WHEN:
- [x] every seat read carries the run id, the session mode and the timestamp of the run that produced it, written into the same `evidence_freshness` record the session and intra-check reports already persist -- no second store
- [x] one predicate answers per seat and distinguishes three states that are never collapsed: refreshed in this run, carried forward (with how old, or an honest "age unknown"), and absent -- absent is not staleness and carried is not fresh
- [x] a stamp read back under a DIFFERENT run id reports carried forward rather than fresh, because an hour later that is what it is
- [x] the storage layer can answer "when was this seat last actually read?" from the rows it already holds, so a carried seat can state its age instead of guessing it
- [x] proven by a round trip through the real storage methods and a real database file, not by a declared field
- [ ] one production session observed where a carried seat reports a real age and a refreshed seat reports this run's id -- cannot be ticked from a test
detail: docs/board_notes/item-227.md

**226. A payment refusal was retried like an outage and reported as an unbounded-cost mystery -- filed 2026-10-01. [BOARD STATE: 3 of 4 ticked; the last box: BLOCKED ON A LIVE EVENT: one observed out-of-credit refusal in production.]** Measured on the production database 2026-10-01: the paid research account ran out of credit, the provider answered HTTP 402 with a falling affordable allowance (13290, 7311, 843, 811, 775), the desk spent 12 provider attempts on `portfolio_manager` and 9 on `tech_analyst` against an account no retry could revive, and then suspended paid analysis saying "the real cost is unknown and cannot be bounded safely" when the truth was that the account was empty.

DONE WHEN:
- [x] a payment refusal is classified on the STATUS CODE (402), never on the provider wording, and is terminal on the first occurrence: no retry and no further rung of the route ladder on the same account
- [x] a 429 saying credits could not be verified keeps every retry it has today, because that one is genuinely transient
- [x] the suspension records `provider_out_of_credit` and tells the owner the account is out of credit and needs topping up, while the call is still booked as unproven cost and no spending limit moves
- [ ] one production session observed where an out-of-credit refusal produces exactly one attempt per seat and the out-of-credit wording reaches Telegram -- fixed-but-unobserved until then
detail: docs/board_notes/item-226.md


**219. The pruning pass reports nowhere the owner looks — OPEN, filed 2026-10-01; the rendering is built, the live confirmation is not. 2026-10-01: the cull itself no longer waits for a full book or a replacement (the owner ruled so), and the ordering/freshness/anti-churn ruling that followed is recorded as NOT BUILT. [BOARD STATE: 5 of 6 ticked; the last box needs a real stored session report read back on both surfaces, which exists again since the 2026-10-09 restart, so reading it is actionable.]** The rotation/pruning pass ran every session and wrote a durable `rotation`/`precheck` row, but the owner saw nothing of it on either surface he actually reads: the Telegram session message said only what the rotation PRE-CHECK concluded, and the dashboard said nothing at all, so a session that examined the whole book and kept all of it was indistinguishable from a session in which the pass never ran. Reporting only; no number that governs a buy, a sell or a size was touched.

DONE WHEN:
  - [x] the session message states that the pass ran and how many holdings it examined, read off the held set the pre-check itself received (`held_examined`), never inferred
  - [x] anything put up to be cut is reported with the CONVICTION reason it was cut on — the entry-bar reasons the holding failed — and never a profit-or-loss one
  - [x] the holdings it KEPT are named as considered and kept, so a silent pass can no longer pass for a pass that never ran
  - [x] every session says whether the score-margin tier is on or off, so the owner is never told the desk pruned more thoroughly than it did
  - [x] the dashboard renders the SAME sentences from the SAME durable row via the run detail, with no second reporting path invented
  - [ ] a real session's stored report is read back and shown carrying the block, on both surfaces, against a run the desk actually made — until then this is rendering proven only by test (2026-10-02: a stored run is now rendered through the real Telegram formatter and the dashboard reader in tests/test_pruning_pass_reaches_both_surfaces.py, which also fixed a false line telling the owner below-bar names still clear the bar; still OPEN until a production session is observed, desk is OFF)
detail: docs/board_notes/item-219.md


**232. The ledger's rewritten citations may point at the WRONG place and now read as verified -- OPEN, filed 2026-10-04. [BOARD STATE: 1 of 4 ticked. PR 1081 (fix/ledger-citations-225) MERGED 2026-10-04, so the rows to correct are on the trunk and the item is actionable.]** First measurement: 12 hand-checked citations -- 6 right, 5 wrong, 1 cannot tell; the five wrong rows and where each should point are in the note.

DONE WHEN:
- [x] the five named rows (see the note) are corrected on the trunk, each re-read against the number it justifies; PR 1081 itself merged 2026-10-04 (corrected 2026-10-04 on branch fix/item-232-citations-point-right, plus 16 more wrong rows found outward; see note)
- [x] EVERY remaining rewritten citation (not a sample) is checked by hand or by a test that compares the cited text to the row's number, and the count right / wrong / cannot-tell is recorded here -- DONE 2026-10-05 by the substantiation guard over all 297 pins (not a sample): RIGHT-by-test 236 (cited place names the row or its value), WRONG-by-test 61 (10 land on a comment/import, 51 on a symbol naming neither), CANNOT-TELL 0. Hand-read on top of the test: all 11 `source`-field pins the test flagged were WRONG and are re-pointed, plus 4 note pins that cited the cluster-cap comment in settings.yaml as the 0.5 floor. Caveat: "names the row or its value" is necessary, not sufficient, for substantiation; the 234 are not hand-confirmed.
- [x] the guard (or a second one) fails when a citation resolves to a symbol that never mentions the row's value or id, proved red against one of the five wrong rows first -- DONE 2026-10-05: every `source`-field pin is now ABSOLUTE (dead or no-mention fails outright, no trunk baseline); the test reproduces the MIN_TOUCHES row as it stood on trunk (a real function naming neither the row nor 2) and asserts red, then green on the re-pointed row. `note`-field pins stay ratcheted against trunk.
- [ ] no citation is left as cannot-tell: each is either confirmed or replaced with a source that settles it -- 2026-10-05: zero `source` pins fail; 61 `note` pins still fail the name-or-value test (the guard prints the list at run time, nothing stored). Most are usage pointers ("threaded to", "used by") that were never meant to settle the number; each still needs a read to decide re-point vs reword, and the ratchet refuses any new one.
detail: docs/board_notes/item-232.md

**233. Secondary-account capture/replay proposal — OPEN BUT PARKED by owner direction 2026-10-06; not a prerequisite to Paper beta. [BOARD STATE: 0 of 4 ticked; no capture authorized by this item while parked.]** Item 202's ninth box was deferred here; the proposed capture/replay criteria below remain unproven, not waived or silently completed. Revisit only for a concrete failure requiring reproduction and renewed owner approval. Do not use the primary account or restart the desk for this item.

DONE WHEN:
- [ ] a deliberately bounded morning capture runs against the isolated secondary Alpaca Paper account through the real provider and broker seams, never the primary account, and produces a repository-safe recording; a pre-commit safety scan rejects any credential, stable broker identifier, primary-account state or production decision content, while preserving the test-only values the real replay path consumes.
- [ ] that captured morning session replays offline through the real morning, Portfolio Manager and protection seams with the socket wall on and zero journalled outbound attempts; parallel synthetic orchestration or hand-written provider/broker answers cannot satisfy this box.
- [ ] a deliberately bounded intraday capture from the same isolated secondary Paper account replays offline through the real half-hourly review and exit seams under the same public-safety and zero-network conditions.
- [ ] both replays assert the captured verdicts, including proposals, refusals and reasons, Portfolio Manager decisions, exits and protection outcomes where present; every missing captured input stops the replay rather than being fabricated or degraded, with a red test proving each newly closed gap.
detail: docs/board_notes/item-233.md

**234. Kept shares are unprotected during a part-sale — filed 2026-10-09. IN PROGRESS (build under way).** When the desk sells part of a holding, the stop is cancelled and the kept shares carry no stop until it is replaced.

DONE WHEN:
- [ ] a part-sale leaves the kept shares under a stop at every moment, proven by a test that fails on today's trunk

**235. Entries are market orders sized off the ask/bid — filed 2026-10-09. IN PROGRESS (build under way).** Entries go in as market orders, sized off the ask for a buy and the bid for a short.

DONE WHEN:
- [ ] entry orders are market orders sized off the ask (buy) or bid (short), proven by a test

**236. The test fake broker accepts what the real broker refuses — filed 2026-10-09. OPEN.** The fake broker the tests use never refuses an order, so a change the real broker would reject passes every test.

DONE WHEN:
- [ ] the fake broker models the real broker's refusals, each with a test that a refused order is reported, not assumed filled

**Retired item numbers — never reuse.** APPEND-ONLY as of 2026-09-30 — closing an item adds ONE NEW `- retired <scheme>: N[, N, ...]` line below, in the matching scheme, and never edits an existing line; the running lists used to live on this one physical line, and even the merge driver's own union rule (`scripts/resolve_doc_conflict.py::merge_retired`) could not save it, because GitHub's own squash-merge — what actually runs when a pull request merges on GitHub.com — never invokes a local git merge driver at all. Two closures now append two different lines and merge with no conflict, by construction; no driver needed for this part. **This still takes the NUMBER ONLY — never a reason.** Every retirement's reason lives in `docs/INCIDENT_HISTORY.md`, which is append-only and merges entry-by-entry the same way. `tests/test_status_board.py` fails a change that adds a reason to any line below, or that edits an existing line instead of appending a new one. The per-item reasons this line used to carry were moved to `docs/INCIDENT_HISTORY.md` on 2026-09-26, verbatim, losing nothing. Gate item 7 was moved, not closed: it is item 76. The two numbering schemes are separate — 3 is retired in BOTH, 20 is live here, and 40, 67 and 200 never existed [verified 2026-09-18 against this file's full git history]. Residue of items 100 and 103 lives in items 106 and 115; item 89 was SHRUNK, not retired. The §11.2 ladder stays; the ladder's own unmeasurable-drawdown behaviour is a separate live question. Run `scripts/next_board_number.py` for the next free number — it reads every line below, the live board, and open pull requests; never eyeball this list. It FAILS CLOSED as of 2026-09-30: if the open-pull-request read fails for any reason it exits non-zero and prints no number at all, because it used to print a warning and a number anyway and two pull requests both claimed item 192 that way. Treat a non-zero exit as a hard stop, not a prompt to guess; `--accept-unchecked-number` is the deliberate offline opt-out and labels its answer UNCHECKED.

- retired queue: 0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 21, 22, 23, 24, 25, 26, 27, 28, 29, 30, 31, 32, 33, 34, 35, 36, 37, 38, 39, 41, 42, 43, 44, 45, 46, 47, 48, 49, 50, 51, 52, 53, 54, 56, 57, 58, 59, 60, 61, 62, 65, 66, 68, 69, 71, 72, 73, 74, 79, 80, 81, 82, 83, 84, 85, 87, 88, 89, 91, 92, 93, 94, 95, 96, 97, 98, 100, 101, 102, 103, 104, 105, 106, 108, 110, 111, 113, 114, 115, 116, 117, 118, 120, 121, 122, 123, 124, 125, 126, 127, 128, 129, 130, 131, 132, 133, 134, 135, 136, 137, 138, 139, 140, 141, 142, 143, 144, 145, 146, 148, 149, 150, 151, 153, 154, 155, 156, 158, 159, 160, 161, 162, 164, 165, 166, 167, 168, 169, 170, 171, 172, 175, 176, 178, 179, 180, 181, 184, 189
- retired gate: 1, 2, 3, 4, 5, 6, 7, 8
- retired queue: 64
- retired queue: 191
- retired queue: 163
- retired queue: 86, 173
- retired queue: 198
- retired queue: 112
- retired queue: 77
- retired queue: 152
- retired queue: 183
- retired queue: 197
- retired queue: 18
- retired queue: 192
- retired queue: 147
- retired queue: 182
- retired queue: 195
- retired queue: 109
- retired queue: 19
- retired queue: 196
- retired queue: 99
- retired queue: 157
- retired queue: 119
- retired queue: 193
- retired queue: 107
- retired queue: 185
- retired queue: 76
- retired queue: 214
- retired queue: 199
- retired queue: 212
- retired queue: 174
- retired queue: 20
- retired queue: 216
- retired queue: 217
- retired queue: 194
- retired queue: 200
- retired queue: 221
- retired queue: 223
- retired queue: 222
- retired queue: 215
- retired queue: 220
- retired queue: 190
- retired queue: 211
- retired queue: 17
- retired queue: 225
- retired queue: 203
- retired queue: 202
## Evidence-only follow-ups — reopen only on concrete production evidence

- news-narrative factual drift; `actual_provider` attribution oddity.

- retired queue: 188
- retired queue: 231
- retired queue: 210
