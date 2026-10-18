## item 90

**Plain language —** About thirty numbers that govern real trades were never read off anything — they were chosen because they sounded sensible. They were catalogued on 11 September and then filed as "an inventory, not a job", with a note saying never to re-audit. Nothing was assigned, nothing had a date, and a week later all thirty were still live. There is no mechanical check of any kind that would catch the next one.
**Recommendation —** Two halves. Read each number off the instrument it is meant to describe. And build a check that fails the build the next time an unsourced trading number is added, so this cannot happen again by filing.

**The second half is built, 2026-09-18, and was then torn apart and rebuilt the same day. The first half is still yours.** Every number of this kind now has to be written down in one file next to a sentence saying where it came from, and the build refuses to pass if one appears that is not. Nobody has to remember the rule: the check works out for itself which numbers count, and the only way to add one is to write the sentence. It insists on six things — that the number is written down at all; that the figure in the file is still the figure in the code, so changing a number puts the reason for it back in front of whoever is changing it; that the figure in the file is also the figure in the settings file the desk actually runs on; that a claimed source is a link or a file and line somebody else can open, not a confident paragraph; that a number with nothing behind it also states the question that would settle it and what the desk pays meanwhile; and — the one worth knowing about — that a number worked out FROM another number fails the build if that other number later moves. A figure can be perfectly well justified when it is written and quietly meaningless a month later because the thing it was measured against changed; that now shows up as a broken build instead of a respectable-looking comment.

**Why it was rebuilt, and it matters more than the check itself.** The single entry the first version held up as its showcase — the 0.50% minimum risk per trade — was wrong in four separate ways. It said the number appeared nowhere in the settings file, nowhere in any document, and in no record of you approving it. All three were false: it is in the settings file, it is in your own ratified risk table in `docs/OUTCOME.md`, and you approved it on 27 August. It also said the number existed in only one place while the same file listed it twice, and a third copy of it was invisible to the check altogether. Every one of those was a thirty-second look away and nobody looked. **So read the honest limit of this thing: it proves a reason has been WRITTEN, never that the reason is TRUE.** That is why a source must now be something openable rather than prose, and why seven corrected entries carry the correction in writing rather than being quietly tidied.

**The count.** 178 numeric sites are now watched, up from 122, after four more modules were brought in — including the ATR period, which every stop and every noise band on this desk is a multiple of, and which the first version watched all the multipliers of while ignoring the unit. **86 distinct numbers decide how this desk trades and have nothing behind them.** That is fewer than the 87 first reported despite the wider net, because mirrored numbers — the same figure written in two files — are now recorded as one number rather than two, and three turned out to be genuinely approved by you and were reclassified. Every value was left exactly as it was: changing one is your call, not an agent's.

**What it still cannot do, so it is never oversold.** It cannot tell a true source from a plausible-sounding sentence; that is the failure above and it is structural, not an oversight. The list of watched modules is written by hand — there is now a second check that counts the numbers in every module NOT on the list and fails if that count grows, so a new one cannot slip in unseen, but the list itself is still a judgement call. It cannot see numbers written into the seats' instruction sheets as prose, which is a separate item. And it does not make any of the 86 sourced — it only stops the 87th arriving unnoticed.

**One factual correction, because it was being repeated.** The story that the range stop-width scaler 0.90 was derived against a stop base of 1.5 which later became 2.5, leaving the derivation stranded, does not match git. The base went from 3.0 to 2.5 on 2026-09-10, and 0.90 was introduced by that same change — it was never derived from anything, and the code beside it says as much ("not a specific measured number"). The class of defect is real and the check now catches it; this particular example is not an instance of it.

**Moved from WORK.md (2026-09-24) —** Full account of what was built and the gate's own honest limit (it proves a justification was WRITTEN, never that it is TRUE): `docs/INCIDENT_HISTORY.md` (2026-09-18). The counts quoted there are a snapshot, already known stale by the next day — read `config/number_ledger.yaml` directly rather than trusting a number here. **Half two, STILL OPEN:** read each arbitrary entry off its instrument. Each states in the ledger the question that would settle it and what the desk pays meanwhile, which is what makes half two prioritisable rather than a list. `MAX_ARBITRARY_ENTRIES` is an EQUALITY, not a ceiling: as a ceiling it rewarded deleting a row.

**item 90, half two — the 2026-10-01 PIPELINE tranche (second tranche; routing only, no VALUE changed).** All 18 `arbitrary` rows under `src.pipeline.TradingPipeline` that carried a status and no settlement route now carry one, so the ledger's routeless count falls from 130 to 112 [measured: `src.number_sources.classification()` over `config/number_ledger.yaml`, before and after]. The `arbitrary` count is unchanged at 136, because this pass reclassified nothing — a row that cannot be settled today is still `arbitrary`; what it gains is a written statement of what WOULD settle it. Fourteen of the eighteen are the prompt-evidence windows and caps (the PM's blocked-proposal, loss-pit, missed-lesson, calibration and recent-decision memories, the position reviewer's own-decision and post-exit blocks, the risk-verdict replay, the thesis-health window and the two missed-opportunity filters): each decides what evidence a trading seat's verdict is built from, which is why none of them is `not-trade-governing`. Each failed the same two derivations, and both failures are written into the ledger rows so nobody repeats them — (1) the desk's ratified SWING mandate (`docs/OUTCOME.md:75`) deliberately names no number and holds hold-length to be an output rather than a setting, so it cannot fix a lookback; (2) every sibling evidence window in the ledger is itself `arbitrary`, so deriving one from another would relabel an unsourced number as another's child. That is the same wall the smart-money truncation caps hit on 2026-09-19 and for the same reason: nothing records how much evidence existed BEFORE the cut. Two attempts is the limit, so the RECORDING is the route.

**The recording those fourteen rows point at, specified here so it can be built from this paragraph alone.** At every prompt build of one of these blocks, write one evidence row carrying: the block's name, the session date and run kind, the number of candidate rows available BEFORE the cut, the number that survived it, the age in days of the oldest surviving row, and the identifier of the seat verdict that prompt produced. It closes when a run of sessions shows the surviving-row count beyond which the seat's verdict stops changing: the window or cap is then set at that point, read off the recording. It is explicitly NOT closed by sweeping for the window that would have traded best — that is fitting this desk's own record and it is barred. Until it is built the rows stay `arbitrary` with state `specified`, which is the honest state and not a claim that anything is collecting.

**The other four pipeline rows are routed individually, each with its own two failed derivations recorded in the row.** The sector-preview buy size (5%, which the PM reads as a projected sector mix before it writes decisions) is routed to a measurement that computes the same preview twice a session — once at the flat size and once at the size the constructor would actually give each candidate — and closes by showing whether the flat figure ever pushes a sector past a ceiling the constructor-sized preview would not. The queued-earnings weight cap (5%, the belt that holds when the PM ignores its own prompt rule) is routed to the overnight gaps that unread filings actually produce on this desk's universe, read from the price record and never from our own trades; its first derivation attempt was the 2026-09-26 item-186 pass, which correctly refused the ~5.07% earnings-day literature as the size of a MOVE rather than a share of the BOOK. The research pre-filter's 0.5 ATR moving-average spread decides which names the technical seat ever sees, so a name it drops cannot be traded that session; it is routed to a recording of the spread of every DROPPED name beside every admitted one, and it was checked for the sort-key shape that explained eight rows in the first tranche — it is a genuine magnitude, compared against a computed spread, not a position in an ordering. The trail-tightening cooldown (4 calendar days) is routed to how many sessions a tightened stop actually stays clear of the 1.25 ATR noise band, replacing the single 2026-07-16 forensic anecdote its docstring currently offers as a reason.

**What is still routeless: 112 rows.** The largest remaining clusters are the smart-money config block, the agent-result scoring fields, the smart-money analyst, the portfolio constructor and the risk config. Item 90 stays OPEN.

**Routing pass, 2026-10-01 -- the smart-money reading tranche (16 rows).** The remaining work on this item is the ROUTELESS ROWS: ledger rows that carry a status but nothing saying how the number would ever be settled. Picked as one tranche because they are judged together -- every one of them governs how insider and congressional evidence is RANKED and TRUNCATED before a seat reads it, so the same question ("does this ever bind, and does binding change the read?") covers all of them. Fourteen live in `src/agents/smart_money_analyst.py` and two in `src/data/smart_money_cluster.py`. **Routeless before: 130. After: 114.** No value was changed; this was a routing pass only.

*The finding worth knowing.* The eight ranking rows are a number that only LOOKS like a magnitude. The integers in `_ROLE_RANK`, `_FRESHNESS_RANK` and `_SIGNAL_CLASS_RANK` enter the code only as elements of a lexicographic tuple sort key (`src/agents/smart_money_analyst.py:153-155` and `174-176`); they are never summed, scaled, averaged or compared across tables. The spacing therefore algebraically cancels -- 3/2/1/0 and 100/7/2/0 rank every finding identically. What is load-bearing is the ORDER, and the one deliberate tie where an unset signal class ranks equal to `indeterminate`. So the open question those rows carried ("does the spacing ever change what the synthesis keeps?") is answered: it cannot. Their route is a ratified bound on the order, closing when post-filing behaviour by role, freshness and signal class is measured off the filings record and market prices -- never off this desk's own fills, which would be fitting.

*The six truncation caps* (synthesis symbols, actor roles, representative transactions, context characters, reason characters, finding words) each silently drop evidence with no marker. Two honest derivation attempts were made and both failed, and per doctrine there was no third. One: derive each from the synthesis prompt's token budget -- that budget is itself unledgered, and every cap is small enough relative to it that it constrains none of them. Two: derive each from the largest evidence the stream has actually carried -- nothing records a pre-truncation size anywhere, so there is no record to read. The route is therefore the recording that does not yet exist: a truncation row per bind, carrying the session, the symbol, the cap and the size before and after. It closes either by showing the cap never binds (then it governs nothing) or by setting it from the observed distribution of what the evidence carries.

*The two cluster rows.* The reserved-slot count routes to a per-session count of detected same-day purchase clusters, so slot demand becomes a series. The minimum-insiders threshold routes to a measurement with two independent legs, because its citation has never been opened: obtain a retrievable copy of Alldredge & Blank (2019) and read its own cluster definition, and separately measure the distribution of distinct-insider counts per same-day cluster across the Form 4 record the desk already pulls.

*Still open on this item:* 114 routeless rows. The largest remaining groups are the pipeline (18), the smart-money admission screen in `SmartMoneyConfig` (16), the agent-result shape (15) and the portfolio constructor (8).

**item 90, half two — the 2026-10-01 PORTFOLIO-CONSTRUCTOR tranche (fourth tranche; routing only, no VALUE changed).** All 8 `arbitrary` rows under `src.portfolio_constructor.config.ConstructorConfig` that carried a status and no settlement route now carry one, so the ledger's routeless count falls from 96 to 88 [measured: `src.number_sources.classification()` over `config/number_ledger.yaml`, before and after, on a base that already holds the smart-money reading tranche and the pipeline tranche]. Counted independently rather than taken from the brief: the brief said "about 83 remain", and 96 is what the partition actually returned, because the third tranche (the 16-row `SmartMoneyConfig` admission screen and the 15 `AgentResult` scoring fields) is still in flight and is NOT on this base — those 31 rows were deliberately left alone to avoid colliding with it. The arbitrary count did not move, because no row changed status, so `MAX_ARBITRARY_ENTRIES` is unchanged at 136 and only the routeless ratchet moved, to 88, with its own delta line.

*Why this group was taken as one tranche.* Every row in it is a multiplier or a ceiling inside the one object that turns a seat verdict into a dollar size and a stop price, so they are judged against each other: the notional ceiling and the risk envelope both bound the same position from different directions, and the four stop-width scalers all multiply the same ATR base. None of the eight could honestly be routed `not-trade-governing`; each one changes either how much is bought or where the protective stop sits.

*The shape check, done rather than assumed.* The first tranche found eight integers whose spacing cancelled because they entered only as positions in a lexicographic sort key, and the next tranche found fifteen that looked identical and were not. Each of these eight was read at its consumption site: all are multiplied into a dollar size or an ATR distance and compared against other magnitudes, so the spacing is load-bearing in every case and the sort-key shape is absent here.

*The three mandate ceilings.* The single-name notional ceiling (65) is the desk's only guard against the stop not filling at all — gap, halt, fraud, regulatory action — so it routes to a measurement of the gap-against-position distribution for the eligible universe off public daily bars, which turns an owner-stated tolerance for a single-name unstopped loss into a notional share. The sector soft target (75) routes to a measurement of the common component of same-sector drawdown, because the only survival-grounded reason this desk caps a sector is that its names move as one position; if they do not, the route says DELETE the target rather than re-pick it, since otherwise it is a diversification number the mandate rejects. The per-position risk envelope (5) does NOT route to a re-derivation at all: the 2026-09-30 ruling makes a global risk constant a defect, so it routes to a recording of whether the flat envelope ever binds a trade the per-name reading would not, with deletion as the outcome if it never does. The owner-appetite halves of the first two stay with board item 186; nothing was escalated from here.

*The four stop-width scalers, and the trap that was avoided.* The three macro-regime rungs (1.2 / 1.1 / 0.95) and the range-setup rung (0.90) all previously pointed at a maximum-adverse-excursion study over this desk's own trades, which `docs/OUTCOME.md` bars outright and which was already deleted from `min_stop_atr_multiple` for that reason. Their routes are therefore instrument-side: the regime rungs settle against a measured LAG — the realized true range following a regime classification divided by the trailing ATR reading the stop was sized from on that day, taken over the universe the desk could trade rather than the trades it made — and the range rung settles against the adverse excursion following a signal, in ATR units, with the desk's own setup classifier applied to every qualifying bar in the universe rather than to the bars the desk entered. Each of the four states the DELETE outcome as well as the set-it outcome: a rung indistinguishable from 1.0 is removed, not re-picked.

*The no-ATR structural buffer (0.5%).* It routes to a recording that writes, at every no-ATR stop placement, the level, the half-width of the price cluster the level detector already grouped to find that level, the distance the flat percentage produced, and whether the following low pierced the buffer without invalidating the level. It closes by replacing the percentage with the measured half-width; if the half-width turns out to be routinely unavailable, the percentage stays and the recording at least makes its cost a measured number.

*Values left alone, and one thing worth saying.* No value was changed and none is being challenged on its arithmetic. The one observation for the owner is that the 65% single-name ceiling and the 5% risk envelope are described in the code as bounding the same position but are never reconciled anywhere — the ledger note on the risk envelope records that a separate 20%-notional cap has been limiting real trades to about 1%, so it is not clear either of these two is the binding number in practice. That is a finding, not a fix, and it is left as one.

*Still open on this item:* 88 routeless rows on this base, fewer once the in-flight third tranche lands. The largest remaining groups are `src.config.RiskConfig` (7), `src.risk.rules` (7), `src.risk.exit_guard` (6) and `src.risk.trailing` (6). Item 90 stays OPEN.


**Routing pass, 2026-10-01 -- tranche three: the smart-money admission screen (16 rows) and the agent-result scoring fields (15 rows).** Routeless rows 114 -> 83. No value moved, no status changed, and nothing was routed to the owner. The three affordability caps (external nominations, filings per refresh, refresh deadline) record BOTH failed derivation attempts and route to a recording of pre-cap demand; the ten filings-window rows and the three tradability floors route to measurements off the filings record and market prices, never off this desk's own fills.

*The finding worth knowing.* The 15 agent-result scoring weights were checked for the sort-key shape the previous tranche found, and they DO NOT have it. `AgentResult._shape_score` SUMS the weight of every expected key present in a JSON candidate and compares those sums across candidates, and a top-level list scores the SUM of its elements -- so the spacing is load-bearing and an order-preserving relabelling would NOT be safe. The 2026-08-17/20 production collapse, 10 of 13 decision runs ending in "no trades", was exactly a spacing collision: a full PortfolioDecision scoring 40 lost to its own eight-element `targets` array at 5 apiece. The route is therefore a measurement of real multi-candidate contests, not a ratified order.

*Why the caps stopped at two attempts.* The scan already computes whether the filings cap bound and whether the refresh deadline cut it, and the pipeline writes those BOOLEANS to a log line -- but nothing persists the demand behind them. A boolean saying the cap bound cannot distinguish a true demand of 1000 filings from one of 2000, so the cap cannot be read off its own history. The recording of pre-cap demand is the route.

*Still open on this item:* 83 routeless rows. The largest remaining groups are the pipeline (18), the portfolio constructor (8), the risk rules (8), `RiskConfig` (7), the exit guard (6) and the trailing stop (6).

**Routing pass 2026-10-01, tranche five -- the live risk subsystem, 22 rows.** Everything routeless under `src/risk/` now carries a settlement route: the reward-to-risk reference point, the alignment give-back band, the exit guard's noise band and break-confirmation margin, its four per-metric noise floors, the earnings-stance age cap, all six cells of the gross de-levering ladder, the six trailing-stop constants, and the advisory correlated-cluster cap. Routeless rows 65 -> 43. No value moved, no status changed, nothing was routed to the owner as an appetite question.

How each number is CONSUMED was read rather than inferred from its shape. The four noise floors look like one family of round numbers, but each is applied on its own to its own metric in its own unit and they are never summed or compared, so their spacing carries nothing. The ladder is the opposite: the engine takes the smaller of the configured cap and every rung whose drawdown threshold the book has cleared, so each threshold is a real boundary and each rung a real multiplier of equity.

No route points at this desk's own trades. The reward-to-risk reference, the give-back band, the trailing noise band, the pivot window and the three range R-multiples all route to measurements over the universe the desk could trade -- doctrine bars fitting to its own record, and the previous tranche had to re-route four scalers for exactly this mistake.

Delete outcomes are written alongside the set-it outcomes. The minimum ratchet percentage is the likeliest deletion: the broker charges nothing to amend a resting stop, so if the recording shows it only suppressing moves the noise band would also refuse, it goes. The ladder's middle 1.0x rung is the one that could become indistinguishable from no de-levering at all -- it only bites because the configured gross cap is 2.0x today -- and it is to be deleted if that cap is ever lowered to 1.0x, never re-picked at another multiple. The exit guard's noise-band route is recorded as UNPROVEN rather than collecting: its refusal payload has produced zero observations so far, and its first closing condition is that the payload be shown to populate at all.


## item 90 — the 2026-09-30 `min_stop_atr_multiple` pass

Moved out of `docs/WORK.md` on 2026-09-30 for the 100,000-byte cap
`tests/test_status_board.py` enforces. Verbatim; nothing changed.
**2026-09-30 — `risk.min_stop_atr_multiple` (2.5): the value is UNCHANGED, the claim that it was SOURCED is withdrawn, and the reformulation is filed as item 199 rather than refused.** This constant belongs to no tranche (182 is the ladder, 183 the order gates, 185 the trailing numbers, 186 the portfolio ceilings), so it was taken here. A first pass refused it; an adversary pass found that refusal rested on a false history and wrong arithmetic, and what follows is the corrected result. Full reasoning is in `config/number_ledger.yaml` under its id rather than duplicated here. **(a) A false history is deleted from five files, not softened.** The first pass asserted a "3.0 -> 1.5 move on 2026-09-04" and built an argument on it. There was no such move: `config/settings.yaml` went 3.0 (2026-08-27) straight to 2.5 (2026-09-10) and never deployed 1.5, because the commit that carried it squashes PR #269's two legs (3.0 -> 1.5, then 1.5 -> 2.5) into one merge. The wrong date was inherited from a settings comment and then copied into four more places by a change whose purpose was removing rot; it is now corrected at the source. **(b) The ledger's own open question was doctrine-barred and is replaced.** It asked what this desk's maximum-adverse-excursion record says about the point inside the band — an MAE study over the desk's own trades is FITTING, which `docs/OUTCOME.md` bars outright, and it is how the 1.5 was produced in the first place. **(c) There is no cited band, so BOTH ends are unsupported.** The entry carries no source field and `config/settings.yaml` offers only "general swing-trading guidance" with no URL, which doctrine explicitly rejects. Searched and recorded: the pages asserting 2.5-3.0x for a fixed multi-day entry stop are vendor content rather than literature, one secondary claim points the other way at 1.5-2.0x, the corroborating Van Tharp and Chandelier figures are trailing mechanisms the settings comment already concedes, and the top search hit for the desk's own phrasing is now the desk's own PR. The quoted band (2.5-3.0) does not even match the one quoted three lines below it (2-3). 2.5 stays as the INTERIM value and is deliberately not re-picked, because with no cited band moving it is one more unsourced choice. **(d) The reformulation is NOT refused — it is specified and filed as item 199.** The first pass refused a sqrt-horizon floor claiming it pins reward:risk at exactly 1.0; that was wrong twice (the setup and regime scalers still multiply in, giving about 1.17 to 0.83, and the target and stop rules fire on opposite sides of price so they do not share a population) and is retracted. More importantly it tested the wrong reformulation: doctrine's worked example is structural, and this desk already computes levels with touch counts and already has owner-ratified machinery that reads a stop from structure. The asymmetry nobody had examined is that the 5-touch bar was measured for justifying a TIGHTER stop, where a level that fails costs a whipsaw; as a WIDENING anchor a level that fails only leaves the stop wider than needed, which under risk-based sizing costs position size and not loss. **(e) One stale constant fixed and the class closed mechanically.** `src/pipeline.py` fell back to 1.5 whenever the configured multiple was absent or not a real number — a half-landed second leg of PR #269, which is exactly the failure `scripts/definition_of_done.py` exists for. Measured: not reachable in production, and the three test modules that build a pipeline give 108 passed with the fallback at either value, so nothing depended on it. `tests/test_risk_setting_fallbacks.py` now pins all fifteen fallbacks to the DEPLOYED value in `config/settings.yaml`. **(f) The screen contradiction was FIXED ON MAIN by item 185, which landed first and went further; this branch drops its own narrower version.** This pass proposed dividing `STOP_SANITY_FLOOR_FRACTION` by the widest reachable stop multiple instead of the base, taking the ceiling from 20% to 16.67%. Item 185 instead deleted the borrowed 0.5 literal outright, so the ceiling is now `1 / widest_reachable_stop_atr_multiple(...)` = 1/3.00 = 33.3%. Main's form is kept. The FINDING survives and item 185 confirms it: dividing by the bare base was false across a band of names, and the divergence was exactly the risk-off scaler 1.20. Also withdrawn as wrong on the facts: the board's note that this "needs the owner's call because it tightens a live screen" — `universe_screen.enabled` is false, so the screen does not ship on. **(g) THE BLAST RADIUS GREW WHILE THIS PASS WAS OPEN, and that strengthens rather than weakens the interim finding.** As of 2026-09-30 this constant no longer governs only the entry stop. Through `widest_reachable_stop_atr_multiple` (2.5 x 1.00 x 1.20 = 3.00) it now also sets (1) the midday stop clamp — an over-wide proposed stop is no longer refused but CLAMPED to that multiple of the name's own live ATR14 and placed (item 80), and (2) the universe screen's volatility ceiling at 1/3.00, which decides which names are tradeable at all before any seat sees them. Moving 2.5 now moves three money decisions, not one. That is a reason to state the interim status loudly, not a derivation: composing an unsourced number into more rules removes independent literals without adding evidence for any of them. Item 185 records the same point from its own side and also stays open.

Full heading text, moved for the same reason:

**2026-09-30 — `risk.min_stop_atr_multiple` (2.5): the value is UNCHANGED, the claim that it was SOURCED is withdrawn, and the reformulation is filed as item 199 rather than refused.** This constant belongs to no tranche (182 is the ladder, 183 the order gates, 185 the trailing numbers, 186 the portfolio ceilings), so it was taken here. A first pass refused it; an adversary pass found that refusal rested on a false history and wrong arithmetic, and what follows is the corrected result. Full reasoning is in `config/number_ledger.yaml` under its id rather than duplicated here. **(a) A false history is deleted from five files, not softened.** The first pass asserted a "3.0 -> 1.5 move on 2026-09-04" and built an argument on it. There was no such move: `config/settings.yaml` went 3.0 (2026-08-27) straight to 2.5 (2026-09-10) and never deployed 1.5, because the commit that carried it squashes PR #269's two legs (3.0 -> 1.5, then 1.5 -> 2.5) into one merge. The wrong date was inherited from a settings comment and then copied into four more places by a change whose purpose was removing rot; it is now corrected at the source. **(b) The ledger's own open question was doctrine-barred and is replaced.** It asked what this desk's maximum-adverse-excursion record says about the point inside the band — an MAE study over the desk's own trades is FITTING, which `docs/OUTCOME.md` bars outright, and it is how the 1.5 was produced in the first place. **(c) There is no cited band, so BOTH ends are unsupported.** The entry carries no source field and `config/settings.yaml` offers only "general swing-trading guidance" with no URL, which doctrine explicitly rejects. Searched and recorded: the pages asserting 2.5-3.0x for a fixed multi-day entry stop are vendor content rather than literature, one secondary claim points the other way at 1.5-2.0x, the corroborating Van Tharp and Chandelier figures are trailing mechanisms the settings comment already concedes, and the top search hit for the desk's own phrasing is now the desk's own PR. The quoted band (2.5-3.0) does not even match the one quoted three lines below it (2-3). 2.5 stays as the INTERIM value and is deliberately not re-picked, because with no cited band moving it is one more unsourced choice. **(d) The reformulation is NOT refused — it is specified and filed as item 199.** The first pass refused a sqrt-horizon floor claiming it pins reward:risk at exactly 1.0; that was wrong twice (the setup and regime scalers still multiply in, giving about 1.17 to 0.83, and the target and stop rules fire on opposite sides of price so they do not share a population) and is retracted. More importantly it tested the wrong reformulation: doctrine's worked example is structural, and this desk already computes levels with touch counts and already has owner-ratified machinery that reads a stop from structure. The asymmetry nobody had examined is that the 5-touch bar was measured for justifying a TIGHTER stop, where a level that fails costs a whipsaw; as a WIDENING anchor a level that fails only leaves the stop wider than needed, which under risk-based sizing costs position size and not loss. **(e) One stale constant fixed and the class closed mechanically.** `src/pipeline.py` fell back to 1.5 whenever the configured multiple was absent or not a real number — a half-landed second leg of PR #269, which is exactly the failure `scripts/definition_of_done.py` exists for. Measured: not reachable in production, and the three test modules that build a pipeline give 108 passed with the fallback at either value, so nothing depended on it. `tests/test_risk_setting_fallbacks.py` now pins all fifteen fallbacks to the DEPLOYED value in `config/settings.yaml`. **(f) The screen contradiction was FIXED ON MAIN by item 185, which landed first and went further; this branch drops its own narrower version.** This pass proposed dividing `STOP_SANITY_FLOOR_FRACTION` by the widest reachable stop multiple instead of the base, taking the ceiling from 20% to 16.67%. Item 185 instead deleted the borrowed 0.5 literal outright, so the ceiling is now `1 / widest_reachable_stop_atr_multiple(...)` = 1/3.00 = 33.3%. Main's form is kept. The FINDING survives and item 185 confirms it: dividing by the bare base was false across a band of names, and the divergence was exactly the risk-off scaler 1.20. Also withdrawn as wrong on the facts: the board's note that this "needs the owner's call because it tightens a live screen" — `universe_screen.enabled` is false, so the screen does not ship on. **(g) THE BLAST RADIUS GREW WHILE THIS PASS WAS OPEN, and that strengthens rather than weakens the interim finding.** Detail: `docs/BOARD_NOTES.md` ("item 90 — the 2026-09-30 `min_stop_atr_multiple` pass").


**2026-09-30, third pass — the recording is COMPLETED and is now the completion criterion of items 90 and 199, replacing any further re-derivation.** The pinned-at-entry half (`entry_atr`, `initial_stop_loss`, the entry `price`, and `stop_basis` carrying the constructor's own `stop_rule`) and the resolved half (`realized_pnl`, `exit_reason_category` = `broker_stop_fill` when the broker's stop filled) were already on the `trades` row. The gap closed here is the FAVOURABLE excursion: `max_favourable_excursion`, the exact mirror of `max_adverse_excursion`, widened by the same `Database._accumulate_excursions` call inside the same `sync_positions` transaction as the snapshot it is derived from. Without it a stop-out recorded beside a wide adverse excursion cannot be told apart from one that first ran a long way in the desk's favour and gave it all back, which is the question the floor actually turns on. Nothing reads any of these columns back into a decision — no threshold, no gate, no surface — so this cannot change what the desk trades; the accumulation is swallowed on error so a recording fault can never fail a position sync. The stop's distance in ATR multiples is deliberately NOT a column: it is recomputed from entry price, entry stop and entry ATR, per the standing rule against storing what code can recompute. Both excursions are snapshot-frequency FLOORS on the true figures and legacy rows are NULL; a reader who drops either caveat is reading them wrong. Proved by `tests/test_stop_evidence_excursions.py`, including a position that opens, runs against the desk, recovers and then closes still carrying the worst excursion it reached.

**2026-09-30, second pass — the EVIDENCE the floor would need is now being recorded, and the fallback divergence is closed at its source rather than pinned by a test.** Two changes, no change to any traded number. (1) `src/pipeline.py`'s hand-copied fallback literals are now overridden by the default `RiskConfig` itself declares, so the 1.5-vs-2.5 divergence note (e) describes cannot recur for this or any other risk ceiling; an audit of every `_risk_setting` literal in the file found `min_stop_atr_multiple` to be the only mismatch, and `max_position_pct` to be the only name with no declared default (required field), so its literal stays as the genuine last resort. (2) Every closed trade now carries the four facts the desk has never recorded and therefore could never check its floor against: `trades.entry_atr` (ATR14 pinned at entry), `trades.stop_basis` (the constructor's own STOP_RULE_* string, which already separates a stop honoured at a computed level from one set by the ATR band), `trades.max_adverse_excursion` (worst against-entry price accumulated monotonically from each session's position snapshot), alongside the `realized_pnl` and `exit_reason_category` already on the row. **This does NOT reopen the doctrine-barred MAE study of note (b).** The permitted use is falsification only: showing whether the ratified floor was ever VIOLATED in practice — whether trades that went on to resolve well were stopped out by a floor sitting inside their ordinary excursion. Sweeping this record for the multiplier that would have maximised past outcomes is fitting and stays barred; the floor is still read from published doctrine and from the instrument. One caveat any reader must carry: the excursion is sampled at snapshot frequency, so it is a floor on the true MAE — a reading that says the floor WAS violated is trustworthy, one that says it was not means only "not observed". Nothing reads any of it back into a trading decision. **The next pass on this item should ask what the record now shows, not re-derive the multiple.**
## item 90 — the 2026-10-01 classification pass (mechanical, no number changed)
**Verdict: the item stays OPEN, and the honest count of remaining work is 134, not the "half done" the board carried.** What was built is the CLASSIFICATION itself, mechanically and from the ledger, plus the ratchet that keeps it honest; what was deliberately NOT done is any re-derivation, because that is the move this item has already recorded as failing every time — the desk cannot derive these numbers from data it never recorded. (a) `src/number_sources.py` gains `classification()`, which partitions all 329 ledger rows into item 90's three states — sourced or measured; ratified as a structural bound with the reason recorded; unsourceable today with a NAMED recording, built or specified, that would settle it — and a fourth bucket that is the defect, a live number in none of the three. A test pins that every row lands in exactly one bucket, so a row cannot fall through the classification unnoticed. (b) `status: arbitrary` rows may now carry `settles_by:` (`kind`, `state` built/specified, `where`, `records`, `closes_when`). That is the only way to express state 3; before this the schema could not tell "unsourceable but recorded against" from "nothing will ever answer this", which is why the classification could only be produced by hand and never stayed produced. A malformed or unactionable route is a HARD build failure, on the ground that it reads as an answer while silently removing the row from the outstanding count; a missing route is counted instead, because 134 of them exist and deleting rows is not the fix. (c) `MAX_ROUTELESS_ARBITRARY` is computed from `config/number_ledger_route_history.yaml` and checked for EQUALITY — identical in shape to `MAX_ARBITRARY_ENTRIES` and chosen for the same two reasons already learned on that ratchet: a hand-edited literal drifts from its own record, and a ceiling rewards deleting a row rather than answering it. Lowering it requires an appended negative delta naming the row and the recording, in the same commit. (d) MEASURED from the ledger 2026-10-01: 109 not trade-governing, 82 sourced or measured, 3 ratified as a bound, 1 in state 3, 134 in none of the three. The 134 are enumerated by the check, not duplicated in prose, and group as `src.config` 36, `src.agents` 30, `src.risk` 21, `src.pipeline` 18, `src.portfolio_constructor` 9, `src.execution` 8, `src.data` 5, `src.verdicts` 4, `src.pipeline_stages` 2, `src.rotation` 1. (e) The single state-3 row is `src.config.RiskConfig.min_stop_atr_multiple`, routed to the per-closed-trade excursion recording built by the 2026-09-30 pass; its value is UNCHANGED and the recording may not be optimised against, since fitting a number to this desk's own trading record is barred outright. (f) One sentinel moved for a non-trade reason and is declared: `MAX_UNSCOPED_NUMERIC_SITES` 154 -> 155 for `MIN_ROUTE_PROSE_CHARS` (40), the shortest route prose the schema accepts — it governs the ledger's own schema and no size, price, stop or exit.

## item 90 — the 2026-09-30 `min_stop_atr_multiple` pass

Moved out of `docs/WORK.md` on 2026-09-30 for the 100,000-byte cap
`tests/test_status_board.py` enforces. Verbatim; nothing changed.
**2026-09-30 — `risk.min_stop_atr_multiple` (2.5): the value is UNCHANGED, the claim that it was SOURCED is withdrawn, and the reformulation is filed as item 199 rather than refused.** This constant belongs to no tranche (182 is the ladder, 183 the order gates, 185 the trailing numbers, 186 the portfolio ceilings), so it was taken here. A first pass refused it; an adversary pass found that refusal rested on a false history and wrong arithmetic, and what follows is the corrected result. Full reasoning is in `config/number_ledger.yaml` under its id rather than duplicated here. **(a) A false history is deleted from five files, not softened.** The first pass asserted a "3.0 -> 1.5 move on 2026-09-04" and built an argument on it. There was no such move: `config/settings.yaml` went 3.0 (2026-08-27) straight to 2.5 (2026-09-10) and never deployed 1.5, because the commit that carried it squashes PR #269's two legs (3.0 -> 1.5, then 1.5 -> 2.5) into one merge. The wrong date was inherited from a settings comment and then copied into four more places by a change whose purpose was removing rot; it is now corrected at the source. **(b) The ledger's own open question was doctrine-barred and is replaced.** It asked what this desk's maximum-adverse-excursion record says about the point inside the band — an MAE study over the desk's own trades is FITTING, which `docs/OUTCOME.md` bars outright, and it is how the 1.5 was produced in the first place. **(c) There is no cited band, so BOTH ends are unsupported.** The entry carries no source field and `config/settings.yaml` offers only "general swing-trading guidance" with no URL, which doctrine explicitly rejects. Searched and recorded: the pages asserting 2.5-3.0x for a fixed multi-day entry stop are vendor content rather than literature, one secondary claim points the other way at 1.5-2.0x, the corroborating Van Tharp and Chandelier figures are trailing mechanisms the settings comment already concedes, and the top search hit for the desk's own phrasing is now the desk's own PR. The quoted band (2.5-3.0) does not even match the one quoted three lines below it (2-3). 2.5 stays as the INTERIM value and is deliberately not re-picked, because with no cited band moving it is one more unsourced choice. **(d) The reformulation is NOT refused — it is specified and filed as item 199.** The first pass refused a sqrt-horizon floor claiming it pins reward:risk at exactly 1.0; that was wrong twice (the setup and regime scalers still multiply in, giving about 1.17 to 0.83, and the target and stop rules fire on opposite sides of price so they do not share a population) and is retracted. More importantly it tested the wrong reformulation: doctrine's worked example is structural, and this desk already computes levels with touch counts and already has owner-ratified machinery that reads a stop from structure. The asymmetry nobody had examined is that the 5-touch bar was measured for justifying a TIGHTER stop, where a level that fails costs a whipsaw; as a WIDENING anchor a level that fails only leaves the stop wider than needed, which under risk-based sizing costs position size and not loss. **(e) One stale constant fixed and the class closed mechanically.** `src/pipeline.py` fell back to 1.5 whenever the configured multiple was absent or not a real number — a half-landed second leg of PR #269, which is exactly the failure `scripts/definition_of_done.py` exists for. Measured: not reachable in production, and the three test modules that build a pipeline give 108 passed with the fallback at either value, so nothing depended on it. `tests/test_risk_setting_fallbacks.py` now pins all fifteen fallbacks to the DEPLOYED value in `config/settings.yaml`. **(f) The screen contradiction was FIXED ON MAIN by item 185, which landed first and went further; this branch drops its own narrower version.** This pass proposed dividing `STOP_SANITY_FLOOR_FRACTION` by the widest reachable stop multiple instead of the base, taking the ceiling from 20% to 16.67%. Item 185 instead deleted the borrowed 0.5 literal outright, so the ceiling is now `1 / widest_reachable_stop_atr_multiple(...)` = 1/3.00 = 33.3%. Main's form is kept. The FINDING survives and item 185 confirms it: dividing by the bare base was false across a band of names, and the divergence was exactly the risk-off scaler 1.20. Also withdrawn as wrong on the facts: the board's note that this "needs the owner's call because it tightens a live screen" — `universe_screen.enabled` is false, so the screen does not ship on. **(g) THE BLAST RADIUS GREW WHILE THIS PASS WAS OPEN, and that strengthens rather than weakens the interim finding.** As of 2026-09-30 this constant no longer governs only the entry stop. Through `widest_reachable_stop_atr_multiple` (2.5 x 1.00 x 1.20 = 3.00) it now also sets (1) the midday stop clamp — an over-wide proposed stop is no longer refused but CLAMPED to that multiple of the name's own live ATR14 and placed (item 80), and (2) the universe screen's volatility ceiling at 1/3.00, which decides which names are tradeable at all before any seat sees them. Moving 2.5 now moves three money decisions, not one. That is a reason to state the interim status loudly, not a derivation: composing an unsourced number into more rules removes independent literals without adding evidence for any of them. Item 185 records the same point from its own side and also stays open.

Full heading text, moved for the same reason:

**2026-09-30 — `risk.min_stop_atr_multiple` (2.5): the value is UNCHANGED, the claim that it was SOURCED is withdrawn, and the reformulation is filed as item 199 rather than refused.** This constant belongs to no tranche (182 is the ladder, 183 the order gates, 185 the trailing numbers, 186 the portfolio ceilings), so it was taken here. A first pass refused it; an adversary pass found that refusal rested on a false history and wrong arithmetic, and what follows is the corrected result. Full reasoning is in `config/number_ledger.yaml` under its id rather than duplicated here. **(a) A false history is deleted from five files, not softened.** The first pass asserted a "3.0 -> 1.5 move on 2026-09-04" and built an argument on it. There was no such move: `config/settings.yaml` went 3.0 (2026-08-27) straight to 2.5 (2026-09-10) and never deployed 1.5, because the commit that carried it squashes PR #269's two legs (3.0 -> 1.5, then 1.5 -> 2.5) into one merge. The wrong date was inherited from a settings comment and then copied into four more places by a change whose purpose was removing rot; it is now corrected at the source. **(b) The ledger's own open question was doctrine-barred and is replaced.** It asked what this desk's maximum-adverse-excursion record says about the point inside the band — an MAE study over the desk's own trades is FITTING, which `docs/OUTCOME.md` bars outright, and it is how the 1.5 was produced in the first place. **(c) There is no cited band, so BOTH ends are unsupported.** The entry carries no source field and `config/settings.yaml` offers only "general swing-trading guidance" with no URL, which doctrine explicitly rejects. Searched and recorded: the pages asserting 2.5-3.0x for a fixed multi-day entry stop are vendor content rather than literature, one secondary claim points the other way at 1.5-2.0x, the corroborating Van Tharp and Chandelier figures are trailing mechanisms the settings comment already concedes, and the top search hit for the desk's own phrasing is now the desk's own PR. The quoted band (2.5-3.0) does not even match the one quoted three lines below it (2-3). 2.5 stays as the INTERIM value and is deliberately not re-picked, because with no cited band moving it is one more unsourced choice. **(d) The reformulation is NOT refused — it is specified and filed as item 199.** The first pass refused a sqrt-horizon floor claiming it pins reward:risk at exactly 1.0; that was wrong twice (the setup and regime scalers still multiply in, giving about 1.17 to 0.83, and the target and stop rules fire on opposite sides of price so they do not share a population) and is retracted. More importantly it tested the wrong reformulation: doctrine's worked example is structural, and this desk already computes levels with touch counts and already has owner-ratified machinery that reads a stop from structure. The asymmetry nobody had examined is that the 5-touch bar was measured for justifying a TIGHTER stop, where a level that fails costs a whipsaw; as a WIDENING anchor a level that fails only leaves the stop wider than needed, which under risk-based sizing costs position size and not loss. **(e) One stale constant fixed and the class closed mechanically.** `src/pipeline.py` fell back to 1.5 whenever the configured multiple was absent or not a real number — a half-landed second leg of PR #269, which is exactly the failure `scripts/definition_of_done.py` exists for. Measured: not reachable in production, and the three test modules that build a pipeline give 108 passed with the fallback at either value, so nothing depended on it. `tests/test_risk_setting_fallbacks.py` now pins all fifteen fallbacks to the DEPLOYED value in `config/settings.yaml`. **(f) The screen contradiction was FIXED ON MAIN by item 185, which landed first and went further; this branch drops its own narrower version.** This pass proposed dividing `STOP_SANITY_FLOOR_FRACTION` by the widest reachable stop multiple instead of the base, taking the ceiling from 20% to 16.67%. Item 185 instead deleted the borrowed 0.5 literal outright, so the ceiling is now `1 / widest_reachable_stop_atr_multiple(...)` = 1/3.00 = 33.3%. Main's form is kept. The FINDING survives and item 185 confirms it: dividing by the bare base was false across a band of names, and the divergence was exactly the risk-off scaler 1.20. Also withdrawn as wrong on the facts: the board's note that this "needs the owner's call because it tightens a live screen" — `universe_screen.enabled` is false, so the screen does not ship on. **(g) THE BLAST RADIUS GREW WHILE THIS PASS WAS OPEN, and that strengthens rather than weakens the interim finding.** Detail: `docs/BOARD_NOTES.md` ("item 90 — the 2026-09-30 `min_stop_atr_multiple` pass").


**2026-09-30, third pass — the recording is COMPLETED and is now the completion criterion of items 90 and 199, replacing any further re-derivation.** The pinned-at-entry half (`entry_atr`, `initial_stop_loss`, the entry `price`, and `stop_basis` carrying the constructor's own `stop_rule`) and the resolved half (`realized_pnl`, `exit_reason_category` = `broker_stop_fill` when the broker's stop filled) were already on the `trades` row. The gap closed here is the FAVOURABLE excursion: `max_favourable_excursion`, the exact mirror of `max_adverse_excursion`, widened by the same `Database._accumulate_excursions` call inside the same `sync_positions` transaction as the snapshot it is derived from. Without it a stop-out recorded beside a wide adverse excursion cannot be told apart from one that first ran a long way in the desk's favour and gave it all back, which is the question the floor actually turns on. Nothing reads any of these columns back into a decision — no threshold, no gate, no surface — so this cannot change what the desk trades; the accumulation is swallowed on error so a recording fault can never fail a position sync. The stop's distance in ATR multiples is deliberately NOT a column: it is recomputed from entry price, entry stop and entry ATR, per the standing rule against storing what code can recompute. Both excursions are snapshot-frequency FLOORS on the true figures and legacy rows are NULL; a reader who drops either caveat is reading them wrong. Proved by `tests/test_stop_evidence_excursions.py`, including a position that opens, runs against the desk, recovers and then closes still carrying the worst excursion it reached.

**2026-09-30, second pass — the EVIDENCE the floor would need is now being recorded, and the fallback divergence is closed at its source rather than pinned by a test.** Two changes, no change to any traded number. (1) `src/pipeline.py`'s hand-copied fallback literals are now overridden by the default `RiskConfig` itself declares, so the 1.5-vs-2.5 divergence note (e) describes cannot recur for this or any other risk ceiling; an audit of every `_risk_setting` literal in the file found `min_stop_atr_multiple` to be the only mismatch, and `max_position_pct` to be the only name with no declared default (required field), so its literal stays as the genuine last resort. (2) Every closed trade now carries the four facts the desk has never recorded and therefore could never check its floor against: `trades.entry_atr` (ATR14 pinned at entry), `trades.stop_basis` (the constructor's own STOP_RULE_* string, which already separates a stop honoured at a computed level from one set by the ATR band), `trades.max_adverse_excursion` (worst against-entry price accumulated monotonically from each session's position snapshot), alongside the `realized_pnl` and `exit_reason_category` already on the row. **This does NOT reopen the doctrine-barred MAE study of note (b).** The permitted use is falsification only: showing whether the ratified floor was ever VIOLATED in practice — whether trades that went on to resolve well were stopped out by a floor sitting inside their ordinary excursion. Sweeping this record for the multiplier that would have maximised past outcomes is fitting and stays barred; the floor is still read from published doctrine and from the instrument. One caveat any reader must carry: the excursion is sampled at snapshot frequency, so it is a floor on the true MAE — a reading that says the floor WAS violated is trustworthy, one that says it was not means only "not observed". Nothing reads any of it back into a trading decision. **The next pass on this item should ask what the record now shows, not re-derive the multiple.**
## item 90 — the 2026-10-01 sixth routing tranche (execution plumbing and the level scan)

Twelve `arbitrary` rows in `config/number_ledger.yaml` gained a `settles_by`
route. No VALUE changed and no row changed status, so the arbitrary count is
unmoved at 136; the routeless residue falls from 57 to 45 and
`MAX_ROUTELESS_ARBITRARY` moves with its own delta line.

The rows, grouped as they were routed:

- **Cash sweep (4).** `_BUY_LIMIT_PAD`, `_SELL_LIMIT_PAD`, `_FUND_BUFFER_FRAC`,
  `_FUND_BUFFER_MIN_USD`. The two pads route to a measurement of the parking
  vehicle's own quoted bid-ask spread, so a round ten basis points becomes a
  multiple of a measured spread. The two buffer rows route to a recording of
  assumed-versus-consumed cash, broker fees and sizing-to-fill drift on every
  funded buy. That is the one route in this tranche that reads the desk's own
  orders, and it is deliberately cash arithmetic rather than a study of
  returns: fitting a number to this desk's own trading record is barred by
  `docs/OUTCOME.md`, and a fee-and-drift shortfall is not a return.
- **Broker stop placement (4).** `AlpacaBroker.STOP_LIMIT_BUFFER_PCT`,
  `_STOP_PLACEMENT_MAX_ATTEMPTS` and both `_STOP_PLACEMENT_BACKOFF_S` rungs.
  The buffer routes to gap-distance and fast-session range measurements over
  the universe the desk could trade, NOT to this desk's own stop fills, which
  was the obvious and barred shape. The retry ceiling and the two delays share
  one recording: per-attempt error class, delay waited and whether the next
  attempt succeeded, which is what `docs/WORK.md` item 129 said the falsified
  "must be a rejection after three attempts" reasoning never had.
- **Level scan (3).** `PIVOT_WINDOW`, `CLUSTER_TOLERANCE_PCT`,
  `LEVEL_STRENGTH_DISTANCE_DIVISOR_PCT`. All three route to bounce-behaviour
  measurements on daily bars over the tradable universe. The pivot-window
  route is written to cover BOTH answers the desk holds today — 5 here and 3
  in the trailing-stop scan — so one measurement retires the disagreement
  instead of documenting it for a third time.
- **Technical seat (1).** `_BARS_PER_SYMBOL`. Routes to a feature-stability
  measurement across lookbacks of 20, 40, 60 and 120 bars.

**Every route states the DELETE outcome beside the set-it outcome**, because
a constant the measurement cannot distinguish from "no constant at all" should
go rather than be re-picked at another round number. In this tranche that
means: the bar cap goes if the feature set never changes with lookback; the
sweep pads go, and the leg becomes a plain marketable order, if the measured
spread never approaches them; each backoff rung goes if success on the next
attempt is independent of the delay waited; the whole retry ladder goes, with
placement failure escalating at once, if no failure class ever clears on a
retry; the stop-limit buffer and its leg go together if the fallback leg is
never taken now that primary protective stops are stop-MARKET; the clustering
step goes if bounce rate is flat in pivot separation; and the distance term
leaves the level-strength formula entirely, leaving touch count alone, if
bounce rate does not fall with distance once touch count is held fixed.

No derivation was attempted for any of the twelve, so the two-attempts limit
was not reached. Nothing here was classified `not-trade-governing`: each of
the twelve reaches a real order — the sweep constants decide how much cash is
available to fund a buy and at what price the sweep legs fill, the broker
constants decide whether and how a protective stop gets placed, and the level
and bar-cap constants decide where structure is found, which is where stops
are read from.

**Still open after the sixth tranche:** the count was reported as 45; counted
again with `src.number_sources.classification()` at the head of the seventh it
was 24, the in-flight tranches having landed in between.

## item 90 — the 2026-10-01 seventh routing tranche (every remaining `src/config.py` row)

Counted first, not taken on trust. `classification()` takes a mapping keyed by
site id, so it is fed `load_ledger()` and not the raw `numbers:` list; the
`unclassified` bucket is the routeless residue. It held 24 rows, not the 45 the
sixth tranche projected. The largest coherent group is the configuration
dataclasses: 16 of the 24 live in `src/config.py`, and all 16 are routed here.
The eight left over are four seat weights in `src.verdicts`, the rotation
margin, the short-gap multiple, the levels-degraded share and one execution
factor.

- **Cash reserve (1).** `CashReserveConfig.pct`. Recording over assumed versus
  settled order cost. Its only readers today are the two displayed liquidity
  figures, so the DELETE outcome is explicit: if no order is ever short at
  settlement over a full quarter, the band goes and the view reports raw cash.
- **Deployment gap band (1).** `DeploymentGapConfig.band_pct`. Cost arithmetic
  over this account's own fee schedule and the entry-slippage distribution
  already measured in this ledger, not an appetite question.
- **Macro event horizon (1).** `EventRiskConfig.horizon_days` (DELETED 2026-10-18 by owner ruling: scheduled releases no longer shape decisions). Pre-release
  moves across the tradable universe, and the route requires the answer in
  SESSIONS, which also settles board item 91's calendar-day defect for this
  field.
- **Entry-slippage belt (1).** `ExecutionConfig.max_entry_slippage_bps`. The
  existing measurement cannot close the row because the belt truncates its own
  tail, so the route is a recording of refused orders plus a declared, bounded
  widened subset that observes the censored tail at all.
- **Intraday scan (3).** Trigger, cooldown and per-scan cap. Settled together
  as docs/WORK.md item 177 requires. The move trigger is restated in ATR
  multiples, because a flat 3% holds a quiet name and a volatile one to the
  same bar.
- **Nomination caps (2).** One recording serves both: they are two readings of
  the same affordability question. NO DERIVATION WAS ATTEMPTED — 6 is not two
  times 3 in any defensible sense, since the desk runs more than two
  nominating seats.
- **`RiskConfig` (7).** The hard stop floor, the gross cap, the single-name
  risk envelope, the target horizon, the target reach cap, the level touch bar
  and the target-divergence warning band.

**Ratification is not a source.** Three of the seven `RiskConfig` rows carry an
owner sign-off — the gross cap (2026-09-01), the single-name envelope
(2026-08-27) and the target horizon (2026-09-25). All three stay `arbitrary`
with a route and say so in the route text. The hard stop floor's existing
justification rests on `config/prompts/tech_analyst.md`, which Invariant 2 bars
as a final authority, and its route says that too.

**No route points at this desk's own trades.** The stop floor, the gross cap,
the single-name envelope, the reach cap, the intraday trigger, the macro
horizon and the touch bar all route to measurements over the universe the desk
could trade. The touch bar's route additionally records that the in-repo
2026-09-03 table is not a source and that its reconstruction on this
checkout's 276-bar panel FAILED, the shuffled control beating real at every
touch count.

**DELETE outcomes.** The likeliest deletion is the target horizon: Rex's
2026-09-30 ruling is that the desk exits on ALIGNMENT and never on a target, so
the recording is expected to show the ceiling changing no order, and the reach
cap goes with it. Also stated: the nomination per-seat cap goes if the run
total always binds first; the intraday candidate cap goes if the risk envelope
always binds first; the belt goes if it is redundant with the universe
half-spread screen; the gross cap goes if maintenance margin binds first; the
cash reserve goes if no order is ever short; and the touch bar goes, taking the
tight-stop exemption with it, if no touch count separates real from shuffled.

**Still open on item 90 after this tranche:** 8 routeless `arbitrary` rows
remain — the four `src.verdicts` seat weights, `src.rotation.ROTATION_MARGIN_PCT`,
`src.risk.constants.SHORT_GAP_RISK_MULTIPLE_DEFAULT`,
`src.pipeline_stages.LEVELS_DEGRADED_RUN_EMPTY_SHARE` and one
`ExecutionStage._run_session` factor. No value moved and the `arbitrary` count
is unchanged at 133. The item stays OPEN.

## item 90 — the 2026-10-01 EIGHTH routing tranche (the last one; routeless reaches ZERO)

**The headline: ROUTELESS ROWS ARE NOW ZERO.** `src.number_sources.classification()`
over `config/number_ledger.yaml` returns an empty `unclassified` list. The previous
tranche reported eight rows remaining; the honest count was 24, and all 24 are
settled here. No VALUE was changed anywhere in this pass.

Counted, not inherited: `{'sourced_or_measured': 88, 'ratified_bound': 1,
'recording_named': 134, 'unclassified': 0, 'not_trade_governing': 114}`.
`MAX_ARBITRARY_ENTRIES` moves 135 -> 134 and `MAX_ROUTELESS_ARBITRARY` 24 -> 0,
each with its own delta line in its history file.

**The four seat weights, which are the shape that fooled an earlier tranche.**
`src/verdicts.py::rank_verdicts` builds `[seat_weight(v.seat) for v in group]` and
uses those weights in weighted means of magnitude, conviction and reward-to-risk,
which are then summed into one score compared ACROSS candidates. That is the
summed-and-compared shape, not the lexicographic-sort-key shape, so the spacing
between 1.2 and 0.8 is load-bearing. Each routes to the same measurement on the
same footing: the forward discriminating power of that seat's own signal on public
data over the tradable universe. The DELETE outcome is the whole dict: if no seat
separates from another beyond sampling error, the score becomes an unweighted mean.

**The one reclassification.** `LEVELS_DEGRADED_RUN_EMPTY_SHARE` (0.5) is now
`not-trade-governing`. Every consumer was read: it appears only in the
levels-coverage check, and only to choose between the red blind-spot owner alert
and the orange degraded one. The function sends the alert and returns; nothing
reads the blind/degraded lists. A wrong value changes a message's colour and
wording, not an order.

**The short-gap sizing haircut (1.5)** is routed, deliberately not re-derived: it
is board item 186's question. Its route is the measured ratio of adverse short-side
to adverse long-side overnight gap over the shortable universe.

**One `ratified-bound` route.** The per-name risk envelope is owner appetite, so its
route records the dollar loss it represents at a dated equity. It STAYS `arbitrary`
when he states it, because ratification is not a source.

**No route points at this desk's own trades.** The stop floor, the gross ceiling,
the target horizon and reach, the level-touch threshold, the intraday move
threshold, the entry-slippage ceiling, the exit limit pad, the rotation margin and
the four seat weights all route to measurements over the universe the desk could
trade. The cash-reserve and nomination/scan caps route to recordings of the desk's
own cash obligations and its own truncation demand, which are arithmetic and
counts, not studies of its returns.

**Delete outcomes are stated beside the set-it outcomes** for all 23 routed rows.
The likeliest deletions: the target-reach multiple, if it never changes a target the
horizon test did not already change; the whole seat-weight dict, if no seat
separates; and the intraday move threshold in its FLAT form either way, because one
percentage across names of different volatility is the defect regardless of level.

**ITEM 90 STAYS OPEN, and the reason is the only one left.** Routing is complete.
Half one (the gate) shipped 2026-09-18. Half two — reading each number off the
instrument it is meant to describe — is NOT done: every route here is `state:
specified`, which means the measurement or recording has been named and nobody has
run or built it yet. The item retires when those routes close, not when they exist.


**Board text moved here 2026-10-01** to bring the item inside the new
per-item byte budget. The board keeps the title; the running account of
half one and half two follows, unchanged.

**Half one, DONE:** every numeric definition site in scope must carry a `config/number_ledger.yaml` entry saying where it came from, or `pytest` fails. **Routing pass 2026-10-01:** the 16-row smart-money reading tranche (ranking tables + truncation caps + the two cluster rows) now carries settlement routes; routeless rows 130 -> 114; the ranking integers were found to cancel algebraically to a pure sort order. **Routing pass 2026-10-01, tranche three:** the 16 smart-money admission-screen rows and the 15 agent-result scoring fields now carry routes; routeless rows 114 -> 83. The scoring weights were checked for the sort-key shape and do NOT have it -- they are summed and compared across candidates, so their spacing is load-bearing. **Routing pass 2026-10-01, tranche five:** the 22 routeless rows of the live risk subsystem (exit guard, trailing stops, de-levering ladder, reward-to-risk reference, cluster cap) now carry routes; routeless rows 83 -> 43 after the in-flight tranches land. See docs/board_notes/ item 90. **Routing pass 2026-10-01, tranche seven:** the routeless residue was re-counted at 24 (not the 45 projected) and all 16 remaining `src/config.py` rows now carry routes; routeless rows 24 -> 8, `arbitrary` unchanged at 133. See docs/board_notes/ item 90.


## Item 186, 2026-10-02 — the concentration recording, built

The 2026-10-01 pass left all four portfolio/cluster ceilings in state 3 (`arbitrary` with a named settlement route) and all four routes in `state: specified`, i.e. written down but not built. Two of them are now built: `max_portfolio_risk_pct` (25) and `max_cluster_risk_share_pct` (40) are settled, if ever, by the `realised_risk_budget` row written once per run from `src/stage_decision.py`.

WHY A RECORDING AND NOT A DERIVATION. Both ceilings are global dials, and the owner's 2026-09-30 ruling says risk is read per name, never off a global constant. That rules out re-deriving a better 25 or a better 40 — a derived global dial is still a global dial — and it equally rules out DELETING them today, because they are the only thing standing between the book and unbounded correlated at-risk while the per-name read does not exist. The honest third option is to stop asserting the concentration and start observing it. The ceilings stay at their ratified values, unchanged, and the desk now writes down what its realised concentration actually was.

WHAT IS STILL NOT SETTLED. The 50% correlated-cluster advisory (`max_correlated_cluster_pct`) and the 75/90 sector pair keep their `specified` routes; both are owned by this item's measurement half, not by the recording built here. `short_gap_risk_multiple` (1.5) stays BLOCKED on stored daily bars the desk does not keep. No value in any of the four moved.

## item 90 — the 2026-10-04 route-admissibility audit (no value changed)

Every routing tranche above records, for each `arbitrary` number, what would
have to be measured before the number stops being a pick. This pass asked a
different question: can the recorded route actually settle the number, or is
the route itself barred? It is a read-only audit; it changes no value, and the
answer is reproducible by running
`scripts/measure_ledger_route_admissibility.py`.

**The structural defect, and it is not small.** Sixteen ledger rows carry TWO
`settles_by` blocks, and one carries seven. YAML keeps the last of two
identical mapping keys and discards the earlier ones without a word, so every
one of those rows has exactly one live route and at least one dead one — and
because the newer routing pass was appended ABOVE the older block, the route
that is silently discarded is the NEWER decision. Anything downstream that
loads the ledger has only ever seen the older route. The report lists all
sixteen; this pass repaired none of them, because choosing which route
survives is a routing decision per row rather than a mechanical edit.

**The single-name risk envelope cannot be settled by either route it records.**
Its live route (the older tranche-seven block) closes on the worst observed gap
multiple, and an extreme of a sample is not a reproducible quantity: it moves
every time the sample grows. Its newer, silently-dropped route closes on the
owner stating a per-name dollar loss he will accept, which the 2026-09-30
ruling withdrew along with the other risk dials. The row's own
`ruling_check_2026_10_02` paragraph already reached the right conclusion — a
per-name cap must be read off the name — and no recorded route implements it.
Status unchanged: arbitrary, now with the reason recorded.

**The hard stop floor has an admissible route that cannot be run here.** The
whipsaw-rate-against-ATR-distance measurement names no extreme and asks the
owner nothing, so neither bar touches it. It needs daily bars across a tradable
universe, and this repository commits none: every measurement script already in
`scripts/` is handed a bar file from outside the tree. The blocker is a
committed bar set, recorded here so the next pass does not rediscover it.

**The alignment give-back band contradicts itself, and the measurement wins.**
Its route closes when the give-back distributions of a resumed trend and a
finished one separate; its older `open_question` calls the same number an owner
dial that no research can settle. The 2026-09-30 withdrawal of the risk dials
decides which half is stale. The value is untouched.

**Barred routes found in total:** seven rows close on an extreme of a sample,
nine close by asking the owner for a loss or a dial. Those two counts are the
remaining debt behind the headline `arbitrary` count: a row with a barred route
is further from settled than the count suggests, because running its route
would produce a number no more defensible than the one it replaces.

---

## Findings 2026-10-04 — the minimum stop width IS the holding period, and that is why it cannot be settled as one number

**The earlier blocker was wrong.** The paragraph above records the hard stop
floor as unrunnable because "this repository commits none" of the daily bars
such a measurement needs. That is false, and it was false when written: the
bars are committed as **gzipped JSON**, not as CSV or Parquet, so a search by
the usual extensions finds nothing and reads as proof of absence. The long
fixture holds **38 symbols and 17,208 daily bars spanning 2021-09-27 to
2026-09-30** [measured: `ops/model_policy/fixtures/yf_daily_bars_2026-08-28.json.gz`].
The measurement below runs against it with no network and no broker.

**What was measured.** `ops/research/min_stop_atr_sweep.py` sweeps the stop
distance from 0.50 to 5.00 ATR in 0.25 steps, at five holding horizons, over
every bar as a candidate entry, and asks of each stop hit whether the price
then recovered above the entry inside the horizon (the stop cost money for
nothing: *regret*) or kept falling (the stop did its job: *saved*). ATR is the
desk's own Wilder-14 reading, imported rather than reimplemented. A
shuffled-ATR control runs alongside. Between **14,434 and 16,524 entry events**
per horizon [measured: sweep output]; the live-placement section — the wider of
the ATR band and the entry bar's low, which is what both call sites actually
place — is the one read below.

**Within one horizon the curves DO separate.** Net benefit per 100 entries
(saved minus regret) has a clear single peak, and the real series beats the
shuffled-ATR control at every distance, so the shape is not noise
[measured: sweep output, live-placement section].

**Across horizons the peak migrates across the entire sweep range**
[all measured, net per 100 entries, live placement]:

| horizon (sessions) | best distance | net at best | net at 2.5 (live) | net at 1.5 (prior MAE pick) |
| --- | --- | --- | --- | --- |
| 5 | 1.25 ATR | +22.6 | +9.4 | +21.2 |
| 10 | 1.75 ATR | +19.6 | +16.8 | +17.4 |
| 20 | 3.00 ATR | +17.2 | +15.8 | -0.3 |
| 40 | 4.50 ATR | +11.3 | -3.3 | -27.3 |
| 60 | 5.00 ATR (at or beyond the sweep's top) | +8.9 | -18.7 | -41.8 |

**So the answer is: not settleable as a single number on this data, and the
reason is structural rather than a shortage of bars.** The distance that wins
is a monotone function of how long the trade is meant to be held, and this
repository pins no default holding period — `expected_horizon_sessions` is
required per trade and has no fallback. Any single figure is therefore a
holding-period assumption wearing a volatility multiple's clothes.

**What this does settle, and it is not nothing.** The live 2.5 is the best
distance for a hold of roughly 15 to 20 sessions and is net-negative beyond
about 30; the 1.5 suggested by the earlier maximum-adverse-excursion work is
optimal only for a hold of about 5 sessions and is sharply net-negative at 20
sessions and beyond. Neither is wrong in isolation; they answer different
questions, and the desk has never stated which question it is asking. A
defensible route exists and does not need a picked number: **derive the
distance from each trade's own stated horizon** instead of from one global
constant. Nothing here changes a config value, a stop or any trading
behaviour — that is an owner decision, and this is the evidence for it.

### 2026-10-04: board prose relocated verbatim

**2026-09-30 — `risk.min_stop_atr_multiple` (2.5): the value is UNCHANGED, the claim that it was SOURCED is withdrawn, and the reformulation is filed as item 199 rather than refused.** Detail: `docs/board_notes/` ("item 90 — the 2026-09-30 `min_stop_atr_multiple` pass").
  - [ ] 2026-09-30, second pass: the floor's VALUE is untouched and the evidence to judge it is now recorded per closed trade (entry price, entry ATR, the entry stop and its basis, and the maximum ADVERSE and FAVOURABLE excursions, alongside the realised outcome and stop-hit category already stored; the ATR multiple is recomputed from those, not stored again), and the pipeline's stale 1.5 fallback is closed at source by reading the declared default instead of a copied literal; the record is for FALSIFICATION only (was the floor ever violated in practice) and may NOT be optimised against, so the next pass reads it rather than re-deriving a multiple. Detail: `docs/board_notes/` ("item 90 — the 2026-09-30 `min_stop_atr_multiple` pass").

### 2026-10-04: DONE WHEN criteria, full text (relocated verbatim; the board keeps the opening clause)

- [ ] 2026-10-01: the CLASSIFICATION is now mechanical and ratcheted, and it says the item is further from closing than the status field implied. `src/number_sources.py` partitions every ledger row into item 90's three states and a fourth that is the defect — a live number in none of them — and `config/number_ledger_route_history.yaml` ratchets that fourth count for EQUALITY, the same shape as `MAX_ARBITRARY_ENTRIES` and for the same reason (a hand-kept literal drifts from its record; a ceiling rewards deleting the row instead of answering it). Re-measured from the ledger on 2026-10-04 (main `2565d463`, superseding the 2026-10-01 reading of 329 rows and **134 in none of the three states**): 344 rows — 119 not trade-governing, 101 sourced or measured, 3 ratified as a bound, 121 with a named recording, and **0 in none of the three states**. An `arbitrary` row may now declare `settles_by:` (kind, state built/specified, where, what it records, what closes it); a malformed route is a hard build failure, because a route that cannot be acted on reads as an answer and quietly removes the row from the outstanding count. NO number was derived, moved or re-picked in this pass.
- [x] REMAINING WORK, named by the check rather than by prose: ROUTELESS ROWS ARE ZERO — `classification()["unclassified"]` is empty on main (re-measured 2026-10-04 on main `2565d463`: 0 routeless, 121 recording_named, 101 sourced_or_measured, 3 ratified_bound, 119 not_trade_governing, 344 rows; `MAX_ROUTELESS_ARBITRARY` = 0 since #954). The earlier "134" was the count of `arbitrary` rows, not routeless ones; the item itself stays OPEN on its arbitrary-row boxes, because a route is a way to settle a number, not a settlement.
- [ ] state 3 (a named recording) holds 121 rows re-read from `classification()` on 2026-10-04, among them `src.config.RiskConfig.min_stop_atr_multiple` (2.5) and the four ceiling rows item 186 routed; the unclassified remainder is 0, superseding both the "FIVE rows" and the 131/134 readings this line used to carry. No value moved in either pass and the floor's recording remains FALSIFICATION-only.
- [ ] THE SETTLING RECORDING IS HALF FILLING, re-measured against the production database on 2026-10-04: `entry_atr` is now non-null on the 3 entries taken after the `d9a853e7` correction (3 of 84 `trades` rows), `max_adverse_excursion` has begun accruing on 1, and `stop_basis` is non-null on 0 of 84 -- including those same three rows, whose `setup_type`, `conviction`, `requested_risk_pct` and `stop_level_basis` all filled from the SAME call. So the entry-side half is POPULATING and the stop-basis half was a SECOND silent accessor the `entry_atr` fix left behind; both reads are now loud (`src/recording_accessors.py`), so a renamed or absent field raises instead of recording NULL forever. This criterion ticks when at least one entry taken after 2026-10-04 carries a non-null `stop_basis` alongside its `entry_atr`, which is a live-session observation nobody can schedule.
- [ ] HALF TWO IS SPLIT INTO TRANCHES, EACH WITH ITS OWN CRITERIA: items 182 (de-lever ladder and alert), 183 (order-placement gates and dead cash-sweep config), 185 (trailing-stop numbers and the volatility-eligibility question) and 186 (portfolio/cluster ceilings and three owner-appetite answers) are NOT pointers — each carries DONE WHEN criteria this item does not repeat. Item 90 ticks when all four are fully ticked and no `status: arbitrary` row remains; do not re-derive a constant here that belongs to one of them. (Corrected 2026-09-30: this line used to say the four carry one word-for-word criterion, which was false.)

## item 90 — 2026-10-05 context-and-ranking coverage repair

This pass found twelve numbers that the earlier scanner could not see or had
described too generously. No numeric value or deterministic gate changed. One
unsupported study-return claim was removed from the analyst's per-filing
context because the study measured a different unit. The settlement routes
below deliberately do not ask which choice would have made the desk more
money; that would fit the desk to its own record, which is barred.

Five return horizons were written as one tuple and therefore invisible to the
scanner. They approximate one week and one, three, six and twelve months with
fixed counts of completed sessions. Holidays and month lengths make those
labels inexact. The route is to use the last completed session at or before
each real calendar boundary. That reads the interval from the calendar and the
instrument now, and removes all five fixed counts without looking at what
happened after a trade.

The two positive chart-rating weights are not measurements. They turn two
ordered labels — chart strength and confidence — into one scalar, so changing
the arbitrary spacing can change which candidate sorts first. The permitted
test is structural: enumerate every rating-and-confidence pair and repeat the
ordering under every monotone relabelling of either scale. If an order changes,
the scalar has no invariant meaning and must be removed while the two original
facts remain visible. No forward return is part of that test.

An unclassifiable insider transaction currently receives a midpoint weight.
Unknown is not evidence for halfway between routine and opportunistic. The
recording must retain why classification failed and the unweighted transaction
value before any evidence cap. It closes only when the producing step can
supply the required class, or when unknown remains an explicit category rather
than an invented numeric interpolation. Past returns may not select a weight.

The two insider-holdings band edges were copied from a published paper, but the
paper computes net stock-wide trades and holdings across insiders over six
months. This code applies the digits to one insider's one filing. Those are
different quantities, so the paper is not a source for the live use. Both edges
are now recorded as arbitrary. The paper's band-return claims have been removed
from the per-filing analyst context; the exact row-level ratio remains visible
as uncited descriptive context, and the deterministic admission and sizing
paths remain unchanged. The route records that exact ratio and every prompt
consumer, then removes the buckets while retaining the exact ratio. Forward
returns and this desk's trade outcomes may not choose an edge.

The minimum duration for a price base happened to reuse the volatility
estimator's fourteen-session period. Those are different market quantities,
and equality is not a derivation. The route is to read the start of the current
base from present price structure and containment, then remove the fixed
duration. It may not be tuned to later trade results.

The three-gap display cap also lacked a defensible route. A controlled replay
can hold the cached market context and prompt constant, render the complete gap
list and each successive truncation, and compare extracted facts and the
verdict while recording token cost. It closes only if the full list fits an
explicit prompt budget, the cap is arithmetically derived from that budget, or
the truncated form is invariant in that controlled comparison. Trading
outcomes are excluded.

**2026-10-12 liquidity-window paragraph.** The liquidity check averages a
stock's daily dollar trading volume over the last twenty sessions. Twenty is a
conventional month, not a measurement, so it is recorded as arbitrary. The
route: across every stock the desk could trade, using public daily prices,
compare averages over ten, twenty and sixty sessions against what it actually
costs to trade that stock in the following sessions (the gap between bid and
ask, and how far the price moves per dollar traded). Keep the shortest window
that ranks stocks as well as the longer ones; if all three rank stocks the same
way, the number does not matter and is deleted. Example: if a stock's ten-day
average is inflated by one earnings-day spike but its trading cost stays high,
the longer window is the better guide. This desk's own fills may never choose
the window.
