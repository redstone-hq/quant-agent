## item 219

The pruning pass's owner-facing report. `src/rotation.py::pruning_pass_lines` is the single renderer; it reads only the durable row `precheck_record` writes, so the sentence and the audit trail cannot drift apart. `src/trader_feed.py::_append_rotation` renders it into the Telegram session message beside `owner_precheck_lines`; `src/api/routes_history.py::_rotation_lines` serves the identical list on the run detail and `src/api/static/app.js` renders it. Tier 2 (`ranked_margin`) stays OFF and the report says so out loud, reading the recorded switch rather than a constant, so it tells the truth if it is ever turned on. The telemetry-unavailable outcome deliberately renders NO pruning block: the pass did not run that session, and saying how many holdings it examined would be exactly the untrue owner line this item exists to remove.

### 2026-10-01 — the cull stops being gated on the book being full

OWNER RULING (verbatim): "Yes, hundred percent sell whatever it's true. This is survival of the fittest and cut the losses fast... Every position needs to justify its reason to be there multiple times a day." A holding that no longer clears the desk's own fresh-entry bar is SOLD — not gated on the book being capital-constrained, not gated on a replacement candidate existing.

MEASURED, against the production record (read-only, 2026-10-01): the categorical tier fired 8 times on 24-25 Sep and died 8 of 8 at the buy-leg precondition, recorded as `pm_did_not_target_new_candidate`. The trap was circular — the tier only ran on a full book, the prompt then told the model there was no room to buy, so the model never wrote the buy and the sell was never proposed. In 80 closed trades the desk has never sold a holding for ceasing to earn its place. Today's live row: `binding` empty, 15.09% headroom, $1,266.43 deployable against a $500 minimum, and `held_below_entry_bar` = FLNC, NOK, UPS — the desk declined to prune precisely because it had money. `src/rotation.py` previously claimed this tier "had fired zero times in the retained logs"; that was FALSE and has been corrected in place.

What changed: C1 (book capital-constrained) and C5 (the PM targeted the new candidate) are no longer preconditions for the CATEGORICAL sale; both still gate the ranked-margin tier, which sells a still-eligible name purely to fund a replacement and therefore genuinely needs them. C3 (the fresh-entry bar) is untouched — no new threshold, grace period, cooldown, minimum holding time or score margin, and `rotation_ranked_margin_enabled` stays off with its 25% margin unproposed.

C8 (structural protection already broken) — REMOVED for the categorical tier, argued not inherited. Item 25 protects a "still-eligible, thesis-intact" position; a name in `blocked` is not still-eligible, and the owner has now ruled that failing the fresh-entry bar IS a real trigger. Keeping it would have let a name fail the bar indefinitely with its stop intact and never be sold, which is the state the ruling exists to end. Protection is still measured and still stated in the sale's reason. Downstream is consistent: `holding_discipline_claim_check` blocks only a regime-flip or bearish-state-change CLAIM proven false, and a rotation reason claims neither.

C6 (LONG only) — KEPT, argued not inherited, and it is the headline gap. Two of today's three below-bar names are SHORTS (FLNC -36, UPS -17); only NOK (+100.58) would be closed by this change. This function appends a zero-size `TargetPosition`, which `_build_sell` turns into a SELL, and the SELL execution loop hard-refuses a SELL on a short; a short needs the separate COVER path, whose caps, protective BUY-stop cancel/replace and partial-fill restore have never been exercised from here. Removing the restriction today would either do nothing or put an unverified order shape on live capital with the remainder's protection unproven, which the one-way protection rule forbids. Below-bar shorts are recorded every session and skipped under their own audited reason.

Protection is one-way, unchanged: the close is an ordinary PM-shaped target, so it goes through `_build_sell`, the hard risk rules, the AI Risk Manager, the holding-discipline check and `_submit_protected_sell`, which cancels the resting stop, sells, and restores protection over the remainder under a durable pending-protection-restore WAL row. A partial fill or a failed sell leaves that WAL row open, and the rotation tier refuses outright to touch a symbol carrying one (`sell_already_in_flight_wal_row`), so no second operation can start on an unprotected remainder. Nothing in this change alters that path.

- [x] the categorical cull no longer requires a full book
- [x] the categorical cull no longer requires a replacement candidate
- [x] the false "fired zero times" claim corrected to the measured 8
- [x] every sale states the entry rule it now fails, on the order, the alert and the run detail
- [x] per-session outcome stays durable: `held_examined`, `held_below_entry_bar` and one `rotation` row per decision
- [ ] below-bar SHORTS are culled (needs the COVER leg proved safe first)

### NOT BUILT HERE — the 2026-10-01 ordering ruling

Ruled after this change was written; recorded so it is not lost, and deliberately NOT half-built:

- [ ] intra-session ordering: protection first, then refresh the evidence on held names, then test and sell the failures, then buy with the cash including what the sell just freed
- [ ] per-session evidence freshness. FINDING: it cannot be established cleanly today. The `blocked` set is rebuilt each session from whatever evidence exists, but nothing stamps a seat's read as refreshed THIS session, and the sessions differ (morning exercises every analyst seat plus the manager; the half-hourly exercises the technical seat and the manager; midday exercises news and a position review). Judging a holding on a clock rather than on a refreshed read would re-decide on hours-old evidence. The unit of work is the RECORDING — a per-seat refreshed-this-session stamp — not a rule written over the gap.
- [ ] anti-churn, buy-side mirror. It is implementable with NO new number: the sell side already refuses a symbol with a BUY recorded today (`held_symbol_bought_today`); the mirror refuses a BUY of a symbol carrying a bar-failure SELL recorded today, read from the same `get_trades(today_only=True)` state. No cooldown, no minimum holding time, no period. It is simply not in this diff.

## Adversary review of the unconditional below-bar sale (2026-10-01)

**The doctrine objection, as the adversary put it.** Dropping the
broken-protection conjunct makes this a ONE-SIGNAL exit. A HELD name is
judged opposition-only (`src/agents/portfolio_manager.py`), so a single
seat's opposition is now enough to put a position up to be sold. That
collides with the standing ruling "exit on ALIGNMENT, never on a target",
which exists precisely so that one indicator cannot close a position.

> SUPERSEDED 2026-10-19 by the desk archetype (OUTCOME): a holding is kept while the chart is not against it and every written objection is answered; an opposing seat is an objection the head trader answers, not a sale trigger. The paragraph below records the 2026-10-01 reasoning and the code as built.

**The answer, and the decision.** It ships. The owner's standing doctrine
is that all five seats must be right to ENTER and to STAY; a holding that
fails the fresh-entry bar has lost that, and the owner ruled explicitly on
2026-10-01 that such a holding is sold. Seat opposition is a CONVICTION
failure, not a price signal. The alignment ruling bars exiting on a price
TARGET — a made-up number — and this exit uses none: it re-runs the same
entry gates the desk would apply to a fresh buy. The two rulings are about
different things and do not conflict.

## Evidence-freshness: the ruling, so no number gets invented

A seat read that is stale must not be allowed to sell a position, and
there is NO age cutoff — any cutoff would be an invented number, which
this desk's doctrine bars. The three-state predicate landing on board item
227 (`stamp-seat-read-freshness`) answers the question truthfully without
arithmetic:

- REFRESHED THIS SESSION — the only state on which a holding may be SOLD
  for failing the fresh-entry bar.
- CARRIED-FORWARD — not grounds to sell. The desk does not judge that name
  this session, and says so.
- ABSENT — not grounds to sell, and never to be conflated with
  carried-forward.

Stated plainly, because it narrows the trigger on purpose: the morning run
re-reads every seat, so the full bar can be judged there; the half-hourly
check re-reads only the technical seat, so only a technical-rule failure
can sell on a half-hourly run; the midday run re-reads news and the
position review. The owner wants losers cut fast — not cut on yesterday's
reading.

NOT BUILT HERE. The predicate is being built on its own branch; this note
records the ruling so the gate is wired to it rather than to a new number.
No freshness gate of this change's own invention was added, and the entry
bar was not weakened to compensate.

## Does `run_intra_safety` reach the rotation stage? ANSWERED: no.

Measured by reading the method: `TradingPipeline.run_intra_safety`
(`src/pipeline.py`) checks the trading day and the kill switch, calls
`_run_intra_safety_preamble` and returns. That preamble does fill and
stop-out reconciliation, the protection-restore and repeg drains, the
retired-cash-park release and the orphan-pending-submit reconcile. There
is no decision stage, no portfolio stage and no rotation on that path, so
the free safety tick can never sell a holding on stale evidence. The
stale-evidence exposure is confined to the paid ticks, which is where the
item-227 predicate applies.

## Four defects found by the adversary and fixed in this change

- A below-bar holding with NO replacement candidate — the headline case
  this ruling creates — crashed the execution stage on the first use of
  the replacement symbol, before the sale was even proposed. Fixed, with
  every other reader of that symbol on the path checked and the two that
  could not tolerate it corrected.
- The desk's own record carried two statements this change made untrue
  (the notifier's claim that the alignment exit is the only thesis-based
  close, and the ranked-margin docstring's claim that the categorical tier
  only cuts names whose protection has already broken). Both corrected.
- The owner alert for an automatic close (`_alert_rotation_executed`,
  which reaches Telegram via `notifier.send_owner_alert` — the path the
  adversary could not find, because it lives at the execution site and not
  in the notifier) would have told the owner the sale freed room for a
  replacement named "None", and asserted a broken protection the tier no
  longer requires. Rewritten to state the real reason in both shapes, and
  the "sold but the replacement was not bought" page no longer fires for a
  sale that never had a replacement leg.
- Buy-side anti-churn. The SELL side already refused to rotate out of a
  name bought today; nothing stopped the re-buy, so sell-at-10:00 /
  buy-back-at-11:00 was real. The mirror now refuses a buy of a name the
  desk closed earlier the same exchange day for failing its own entry bar,
  read off the desk's own durable record. No cooldown, no holding period,
  no new number — the same exchange-day window the SELL-side guard uses.

### 2026-10-02 — the dashboard gets its own Pruning Pass panel

MEASURED: before this change the pass reached the dashboard only inside one run's detail, so the owner had to know which run to open. Now `GET /pruning-passes` (`src/api/routes_pruning.py`, read-only, `mode=ro`) lists every pass recorded on the newest day that has one, with a verdict and a reason per examined name, rendered by the "Pruning Pass" panel. A pass that cut nothing still shows what it examined. No threshold or lookback was added: the window is the newest day with a record. Limit: a name below the bar but not cut carries "the record does not say which rule held it back", because the durable row stores reasons only for the cut name. Telegram stays muted. The live-run confirmation box on the board stays OPEN until a production session is observed.

The "record does not say" gap is closed at the source: `src/rotation_dispositions.py` writes a run-scoped `rotation`/`dispositions` row recording, per below-bar name, the conviction reasons it fails on and, where the pass never reached it, "not reached: <why>". Names the pass reached and refused keep their existing `rotation`/`skipped` row, which the panel joins. A run recorded before this change says so explicitly.

## Rehearsal assessment, 2026-10-02

Evidence kind: OFFLINE REHEARSAL runs of 2026-10-02 against a snapshot of the production database. These are NOT production sessions and no box was ticked on them.
Observed runs (ops/rehearsal/run.py, replay pinned automatically, sudo-user snapshot, production file byte-identical after each):
- morning: VERDICT FAIL and "REHEARSAL VOID -- HermeticBreach": the harness blocked outbound connections to the FRED host because no FRED or news feed is recorded on this box (board item 202); 0/15 macro series and 0/20 news feeds returned data; the recording holds ONE portfolio_manager answer and the session asked twice, so every route raised "all 1 recorded response(s) were already replayed" and the session raised in the decision stage.
- midday: VERDICT PASS but "REHEARSAL VOID -- HermeticBreach" (outbound attempts to the FRED host and the Yahoo client blocked).
- intra_check: first run VOID (101 inputs absent from the recording); re-run with --allow-degraded completed, VERDICT PASS, not void, 0 trades, 8.4s, $0.00.
Row read back from the rehearsal intra_check report (sandbox database): run_id rehearsal-intra_check-20261002, evidence_freshness = None. The tick found no candidates, so no seat was read and no stamp was written; the two preceding real intra_check rows (2026-10-01) also carry none.
Last box (a real session's stored report read back carrying the pruning block on both surfaces, against a run the desk actually made): NEEDS-REAL-SESSION. The box asks for a run the desk made; a rehearsal run is by definition not one, and the morning session that runs the pass is void offline. The rendering-from-stored-run path is already covered by tests/test_pruning_pass_reaches_both_surfaces.py. Box left open.

### 2026-10-04 - a record with no examined count was rendered as an empty book

MEASURED read-only on the production record: 27 `rotation`/`precheck` rows in 27 runs (2026-09-23 to 2026-10-01), so the pass IS captured; only 3 carry `held_examined_count`, 24 predate the field; 0 `dispositions` rows (shipped after the desk's last session, never exercised). The reader coerced the absent count to 0 and said "the book is empty" for those 24. Now absent reads "not recorded" on the Telegram line, the run detail and the dashboard panel (`src/rotation_unrecorded.py`); a written zero still means empty. The last DONE WHEN box stays open: no session has run since the rendering shipped.
