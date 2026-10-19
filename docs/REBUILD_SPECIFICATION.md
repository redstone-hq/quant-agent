# QAMC Rebuild Specification — required behaviour, derived from owner rulings

**Status: compiled 2026-10-01 from the repository's own record. The code is being
discarded; this document is what must survive it.** Seat doctrine superseded
2026-10-19: wherever a row below counts seats, lets one seat halt, or bars a
sole backer, the governing rule is `docs/OUTCOME.md`, "The desk archetype: specialists, weighed, then one head trader" (owner, 2026-10-19). Rows marked SUPERSEDED are kept only so the citation trail stays readable.

Every requirement below is numbered and traceable. The citation column names
the document and the dated entry (or board item) it was taken from, never a
code location, so the document outlives the code. Owner words are quoted
verbatim where the record holds them; paraphrase is marked `[paraphrase]`.
Where two rulings conflict or a later one overrode an earlier one, section 9
says so with dates. Nothing here was invented to fill a gap; a gap is marked
`GAP`.

Authority order used: `docs/OUTCOME.md` (doctrine) > `docs/INCIDENT_HISTORY.md`
and `docs/board_notes/` (dated rulings) > `config/number_ledger.yaml` (numbers
and their derivations) > `docs/WORK.md` (open board).

---

## 0. What the desk is

| # | Requirement | Owner words / source | Cite |
|---|---|---|---|
| 0.1 | The desk is a leveraged short-horizon trading desk whose purpose is to make money. It is not a retirement portfolio and must not inherit portfolio-construction goals. | *"This isn't my 401k / RRSP. This is leverage trading for profit. If one sector is hot and we're looking at a short-term horizon, I don't see a problem with that... This is a trading desk, not a long-term retirement desk."* (2026-09-01) | OUTCOME, "This is a trading desk" |
| 0.2 | Horizon is swing: days to weeks. Holding period is an OUTPUT of the position's behaviour, never a setting. | *"depending on the stock's performance is how long we should be holding the stock."* | OUTCOME, Outcome §; WORK "RESOLVED 2026-09-25 — the mandate is SWING" |
| 0.3 | The edge is breadth x consistency x asymmetry: underwrite the full liquid universe daily across technicals, fundamentals, news, macro and insider flow; act identically long or short; risk a bounded fraction per idea; cut losers at structure; let winners run. This is a hypothesis and measuring win rate, win/loss ratio and expectancy is a first-class requirement. | [paraphrase of the OUTCOME edge statement] | OUTCOME, "The edge" |
| 0.4 | Directional neutrality: the desk must be able to express long, short and neutral views. Direct short selling is authorised (ratified 2026-08-27) on a margin account with borrow checks. Options and theta strategies are outside the architecture. | ratification record | OUTCOME, "Directional neutrality" |
| 0.5 | Shorts carry the same limits as longs. No short-only caps. | *"Shorts can have the same [limits] as longs."* (2026-09-17) | INCIDENT_HISTORY 2026-09-17 "shorts carry the same limits" |
| 0.6 | Inverse ETFs stay in the tradeable universe. | *"they have their uses they can still be useful for some situations."* (2026-08-30) | INCIDENT_HISTORY 2026-08-30 |
| 0.7 | Paid news sources are refused permanently; free sources only. | *"not worth paying for. There has to be other sources available."* (2026-08-30) | INCIDENT_HISTORY 2026-08-30 |
| 0.8 | Fractional shares are allowed, gated on the broker confirming the instrument is fractionable (fail closed). | *"if the gap is brief upon entry, then it's irrelevant to eliminate that option."* (2026-09-01) | INCIDENT_HISTORY 2026-09-01 "Fractional shares are IN" |
| 0.9 | Paper and live share one architecture; nothing may be looser because the account is paper. Live activation is an authorisation change, not a rewrite. | [paraphrase] | OUTCOME, "Execution-environment principle" |
| 0.10 | A feature is shipped finished and switched on, not behind a default-off flag "to be safe". A disabled feature is unvalidated code. | [paraphrase of the 2026-08-29 overrule] | INCIDENT_HISTORY 2026-08-29 "short selling ships finished and enabled" |

---

## 1. Entry rules — what must be true before the desk buys (or shorts)

| # | Requirement | Owner words / source | Cite |
|---|---|---|---|
| 1.1 | SUPERSEDED 2026-10-19 by the desk archetype (OUTCOME): views are weighed by measured record and one head trader decides; there is no green-light or agreement count. Historical text: no single seat may green-light a name; entry requires multi-seat agreement. | *"Nothing can green light a name on its own. This is a trading desk with multiple agents. This isn't one guy in a room."* (2026-09-25) | board_notes item-109 |
| 1.2 | SUPERSEDED 2026-10-19 by the desk archetype (OUTCOME): a holding is kept while the chart is not against it and every written objection is answered; an opposing seat is a written objection the head trader must answer, not a block. Historical text: all five seats must be right to ENTER and to STAY. | *"all five seats must be right to ENTER and to STAY"* — recorded as the owner's standing doctrine in the adversary review | board_notes item-219, adversary review |
| 1.3 | SUPERSEDED 2026-10-19 by the desk archetype (OUTCOME): the chart carries the most weight but halts nothing alone; only the independent risk seat can shrink or veto. Historical text: only the technical seat may stop the desk. | *"only technical analysis can stop the desk"* (2026-09-18) | board_notes item-020 |
| 1.4 | Macro is one weighted data point per name, sign-symmetric (helps a long or a short equally), weighted by its MEASURED strength; it is never a standalone go/no-go. | *"Macro means macro. It is one data point. Depending on how strong the data point is — like a black swan, or war, or calamity — the data point should be weighted."* · *"I don't understand why long or short would matter. It's a data point... it can be measured and weighted depending on if it's positive or negative."* (2026-09-25) | board_notes item-109 |
| 1.5 | Conviction outranks balance. The desk never buys or sizes UP to diversify, to balance sectors, to improve "the shape of the book", or to use spare borrowing room. Undeployed margin is an acceptable outcome when nothing clears the bar. | [paraphrase; ruling 2026-09-25] | INCIDENT_HISTORY 2026-09-25 "conviction outranks balance" |
| 1.6 | Every seat must state, at the moment of its call, what would prove it wrong (a falsifiable invalidation). "I don't know / insufficient data" is a first-class recordable answer; a schema with no way to say it "is asking to be lied to". | owner decision 2026-09-12 | OUTCOME, "An unverifiable number must never rank or size"; board_notes item-099 |
| 1.7 | Missing required data is a defect in the step that should have produced it. Never invent the value; never make skip/drop/continue the permanent product. Refusing the name with a durable reason is last resort. | owner standing rule 2026-09-17 | OUTCOME, "Missing data is a defect" |
| 1.8 | Anything that ranks or sizes a trade must be computed by the desk from the instrument (price, volatility, levels). A number a model asserts and nothing can check may explain, never compete for capital. | owner decision 2026-09-12 | OUTCOME, "An unverifiable number must never rank or size" |
| 1.9 | Reward:risk is NOT an entry gate and NOT a score input. A preset target is a made-up number, so a ratio built on it may at most break ties within a tier. A breakout is not measured on reward:risk at all. | invented reward:risk floors retired by owner 2026-09-17; decision 2026-10-01 | OUTCOME "Exits, specifically"; INCIDENT_HISTORY 2026-10-01 item 208(a); SAFETY_BOUNDARIES |
| 1.10 | SUPERSEDED 2026-10-19 by the desk archetype (OUTCOME): views are weighed, not counted, with weights from each seat's measured record (James-Stein shrinkage to the group mean, starting equal); risk sizes from volatility and limits. Historical text: per-seat sizing weights refused; one seat = one vote. The "agreement sizing ladder" (shrinking size by number of agreeing seats) was retired because the seats read overlapping information and are not independent. | decision 2026-09-14 | INCIDENT_HISTORY 2026-09-14 "agreement sizing ladder RETIRED" |
| 1.11 | A "three strikes" rule refusing repeat ideas is refused; conversion rate measured the desk's own plumbing, not the stocks. | decision 2026-09-14 | INCIDENT_HISTORY 2026-09-14 item 10 |
| 1.12 | An entry order is a limit (a ceiling, not a price). A displayed quote through the ceiling is a fact about the feed, not a decision about the trade; no "price ran away by X%" skip rule. | decision 2026-09-30 | INCIDENT_HISTORY 2026-09-30 item 183 |
| 1.13 | A stop may claim to be "level-backed" only when the level's measured zone is strictly narrower than the trade's own stop distance. | owner ruling 2026-10-01 | INCIDENT_HISTORY 2026-10-01 top entry |

`GAP 1.A` — CLOSED 2026-10-19: the 25 Sep role-based conviction bar ("technical = timing veto; no seat opposed") is superseded by the desk archetype in OUTCOME and must not be built.

`GAP 1.B` — A ruling to refuse a buy whose reward-to-risk is bad (2026-10-01, "parity, trial, must record every refusal") is referenced in session memory only; no repository record found. It would sit in tension with 1.9. Re-confirm before building.

---

## 2. Exit and culling rules — when a holding must be sold

| # | Requirement | Owner words / source | Cite |
|---|---|---|---|
| 2.1 | **Every position must continually earn its place. A holding that fails the desk's own fresh-entry bar is SOLD — unconditionally: not gated on the book being full, not gated on a replacement existing.** | *"Yes, hundred percent sell whatever it's true. This is survival of the fittest and cut the losses fast... Every position needs to justify its reason to be there multiple times a day."* (2026-10-01) · *"it has to earn its right to be there"* | board_notes item-219; item-227 |
| 2.2 | The bar re-tested on a holding is the SAME entry bar a fresh buy would face. No grace period, cooldown, minimum holding time or score margin may be added to it. | ruling 2026-10-01 | board_notes item-219 |
| 2.3 | A holding may be sold on a seat's read only if that read was refreshed THIS session. There is NO age cutoff — a cutoff would be an invented number. The unit of work is a per-seat "refreshed this session" stamp, not a clock. | ruling 2026-10-01 | board_notes item-219 "Evidence-freshness"; item-227 |
| 2.4 | Intra-session ordering: protection first; then refresh evidence on held names; then test and sell the failures; then buy with the cash including what the sell freed. | ordering ruling 2026-10-01 (recorded, NOT built) | board_notes item-219 "NOT BUILT HERE" |
| 2.5 | Culled SHORTS must be covered the same way; this was not built because the cover path was never proven safe. The rebuild must treat long and short culls identically. | board gap, 2026-10-01 | board_notes item-219 C6 |
| 2.6 | **A winner is sold only when structure, volatility (ATR) and a moving-average cross AGREE the trend is over. Never on one signal. Never at a pre-set price.** A price target is a made-up number. | *"exit on ALIGNMENT, never on a target"* (ruling 2026-09-30) [recorded as ruling text, not verbatim speech] | INCIDENT_HISTORY 2026-09-30 RULING |
| 2.7 | Profit-taking is trailing-stop-driven and nothing else. A preset fixed-fraction-at-fixed-gain trim is rejected as a class. | owner decision 2026-09-12 | OUTCOME "Exits, specifically"; INCIDENT_HISTORY 2026-09-12 |
| 2.8 | Protection changes are one-way: a stop may only tighten. Every candidate trail must sit strictly between the live stop and current price. A take-profit target may never gate a trail. | decision 2026-10-01 item 212 | INCIDENT_HISTORY 2026-10-01 item 212 |
| 2.9 | Selling to free capital (replacement-funded rotation) is a separate tier and DOES require that capital is constrained and that a target replacement exists; it must stay OFF while its margin is unsourced. | ruling 2026-10-01 | board_notes item-219 |
| 2.10 | A sell whose stated reason is provably false against the desk's own record is stopped and the owner told. | item 25, 2026-09-04 | INCIDENT_HISTORY 2026-09-04 item 25 |
| 2.11 | A swap rule may not sell a healthy position merely because the specialists who liked it moved on; something about the position must have got worse. | item 66, 2026-09-14 | INCIDENT_HISTORY 2026-09-14 item 66 |
| 2.12 | Pruning is conviction-based, not P&L-based. | [paraphrase; owner ratified 2026-09-23] | board_notes item-219; WORK funnel queue |
| 2.13 | Anti-churn needs no number: the sell side refuses a symbol bought today; the mirror refuses a buy of a symbol sold for bar-failure today. No cooldown period. | ordering ruling 2026-10-01 (recorded, NOT built) | board_notes item-219 |

---

## 3. Risk rules — enforced by code vs advisory; per-name vs global

| # | Requirement | Owner words / source | Cite |
|---|---|---|---|
| 3.1 | Deterministic code and broker-side protection are the final authority. The AI risk seat is advisory/challenge logic, never a replacement for hard rules. Provider, model or prompt changes cannot bypass hard-risk behaviour. | accepted contract | SAFETY_BOUNDARIES §1-3, 7 |
| 3.2 | Hard limits are enforced by code. Over GUIDELINES the risk seat may only RESIZE a name, never veto the plan. | ruling 2026-09-19, ratified 2026-09-25 | INCIDENT_HISTORY 2026-09-25 item 162 |
| 3.3 | The risk seat may never make a trade BIGGER. It may widen a stop only if the widened stop stays outside the ATR noise band; a refused edit refuses the edit, not the ticket. | items 155, 2026-09-26; SAFETY_BOUNDARIES | INCIDENT_HISTORY 2026-09-26 item 155 |
| 3.4 | Conviction is expressed as RISK allocation, converted deterministically into shares using the analyst's stop. A wider stop yields a smaller position, never a tighter stop. | ratified 2026-08-27 | OUTCOME "Risk envelope" |
| 3.5 | **Risk is per-name, never a global dial.** Risk limits are read off each stock's behaviour and seat conviction; a global risk constant is a defect to be replaced by a per-name read, not a value to ratify. The appetite questions previously routed to the owner are WITHDRAWN. | ruling 2026-09-30 [paraphrase from ledger notes] | number_ledger notes on max_portfolio_risk_pct, sector ceiling; board_notes item-090 |
| 3.6 | Every position carries a protective stop at the broker at all times; a missing stop is repaired and the owner told. Adding to a winner: cancel the stop, confirm gone, buy, replace one stop over the whole holding, under a durable write-ahead record. | 2026-09-15; item 111 | INCIDENT_HISTORY 2026-09-15; 2026-09-19 item 111 |
| 3.7 | Primary protective stops are stop-MARKET (ruling 2026-09-25). A limit stop that cannot fill into a gap is not protection. | ruling referenced 2026-09-25 (its own entry not found — `GAP 3.A`) | INCIDENT_HISTORY 2026-09-26 "Worth carrying" |
| 3.8 | There is NO account-level loss halt. Per-stock stops are the loss defence; the gradual gross-exposure de-levering ladder stays. | *"I'm starting to think that I'm fine with the stops on the individual stocks and I do not want a nuclear option so remove the whole secondary halt on portfolio completely because there's too many things you keep finding where a slight normal fluctuation in the market can liquidate or halt everything — that's too dangerous to leave — the proper stop losses should be enough."* (2026-09-20) | INCIDENT_HISTORY 2026-09-20 item 32 |
| 3.9 | Whole-book liquidation is forbidden (it would cancel every stop, fail to sell into a gap, then re-place). | owner-merged deletion 2026-09-14 | INCIDENT_HISTORY 2026-09-20 item 32 |
| 3.10 | The desk stays 100% invested — long or short — subject to 1.5. Idle cash is a cost; nothing sits in T-bills; macro informs direction, never how much sits idle. Any undeployed cash must be explained. | owner mandate 2026-09-17 | OUTCOME "Capital is to be deployed" |
| 3.11 | Correlated names consume one bet's budget. A long and a short in the same sector are NOT a hedge; sector exposure is tracked per side, not netted. | OUTCOME 2026-09-01 | OUTCOME "This is a trading desk"; INCIDENT_HISTORY 2026-09-01 |
| 3.12 | A sector limit's only job is bounding correlated blow-up, not diversification. | OUTCOME 2026-09-01 | OUTCOME |
| 3.13 | Risk uncertainty fails CLOSED on entries: a dead or unparseable AI Risk seat produces no entry orders. On SELL/REDUCE/COVER only, AI Risk uncertainty fails OPEN so risk-reducing exits remain available; deterministic Python protections remain final authority. Mission Control / API failure has zero effect on trading or protection. A COVER is never blocked by AI Risk uncertainty. | SAFETY_BOUNDARIES | SAFETY_BOUNDARIES §4-6, caveats |
| 3.14 | Numbers that govern money are read from the instrument (ATR, structure, confirmed price action, the trade's own recorded claim) — never a flat count, round percent, or "sounds prudent" number. Fitting to the desk's own past record is forbidden (markets change; a black swan is the event no history contains). Approval does not make a flat number non-arbitrary. | owner correction 2026-09-04; corrected 2026-09-12 | OUTCOME "No arbitrary numbers, ever" |
| 3.15 | When a number cannot be read: (1) reformulate the rule to need no constant; (2) cite published research with an openable URL; (3) file an owned, dated item stating the question, provenance, what was ruled out, what evidence settles it, and the cost meanwhile. Never "leave it off and move on"; never "ask the owner" except for money, mandate and risk appetite. | OUTCOME | OUTCOME "No arbitrary numbers" |
| 3.16 | Every money-governing number must carry a recorded derivation that a reader can open (URL or document reference), checked mechanically at build time. A deployed value must match the recorded one. | OUTCOME 2026-09-18 | OUTCOME; number_ledger header |

---

## 4. What the desk must TELL the owner, and how

| # | Requirement | Owner words / source | Cite |
|---|---|---|---|
| 4.1 | Every failure alerts in its OWN message, never bundled into a run summary; severity is carried in TEXT, never colour; deliberately not deduplicated — a still-broken seat keeps alerting. | owner rule 2026-09-02 | WORK "Alert design" |
| 4.2 | Every sale states the entry rule the holding now fails — on the order, the alert and the run detail. | ruling 2026-10-01 | board_notes item-219 |
| 4.3 | Report the TRUE state, never an error: a false or stale alert is a root-cause defect in timing, never patched in the alert; a full book is reported as a normal state showing what was reviewed. | [paraphrase of 2026-09-23 rulings] | INCIDENT_HISTORY 2026-09-23 "jam alarm", "never once seen the end of a morning report" |
| 4.4 | Silence alarm: if the whole desk goes quiet for about one hour, the owner is told. | *every hour the desk sits silent is an hour of open positions nobody is watching* [paraphrase] (2026-09-03) | INCIDENT_HISTORY 2026-09-03 silence-alarm |
| 4.5 | A bad analyst seat (data feed failure) pages the owner on its own, every time. | 2026-08-29/09-02 | INCIDENT_HISTORY |
| 4.6 | A missing or un-restored protective stop, an unconfirmed order outcome, or a desk/broker record disagreement pages the owner in plain words. A socket being off is NOT paged. | 2026-09-17/18 | OUTCOME "AND THEN THE MECHANISM DID NOT WORK"; SAFETY_BOUNDARIES |
| 4.7 | The nightly unprotected-position figure is reported every night (the one thing the owner explicitly asked to keep). | 2026-09-30 | INCIDENT_HISTORY 2026-09-30 "nightly unprotected figure" |
| 4.8 | Positions the broker closed behind the desk's back, and positions re-protected, reach the owner. | item 101, 2026-09-25 | INCIDENT_HISTORY 2026-09-25 item 101 |
| 4.9 | The dashboard must let the owner understand, without logs: account/equity/P&L/positions/orders; directional posture and whether cash is cash or exposure; candidates considered; what each seat concluded and where they disagreed; what the manager proposed; what risk changed; what code blocked; why a session produced no trade; what executed vs proposed; which model answered and its cost; prior days; missed opportunities both ways; whether model choices add value. | OUTCOME | OUTCOME "The operator should be able to understand" |
| 4.10 | Owner-facing time is Eastern; convert before quoting. | WORK | WORK "Engineering setup" |
| 4.11 | A stock the desk could not read must not vanish silently; its refusal is stated where the owner looks. | item 158, 2026-09-26 | INCIDENT_HISTORY 2026-09-26 item 158 |

`GAP 4.A` — "Voice the WHY of every trade: every buy/sell/hold states its real reason (why / when / how much); a bare-number, news-only or macro-only reason is a defect" is held in session memory as an owner ruling; no verbatim record found in `docs/`. 4.2 is the only repository-recorded fragment. Re-confirm before building.

---

## 5. What the desk must NEVER do

| # | Requirement | Cite |
|---|---|---|
| 5.1 | Never trade live capital without explicit authorisation; paper only until then. | SAFETY_BOUNDARIES §1 |
| 5.2 | Never liquidate the whole book; never halt the desk on an account-level loss trigger. | 3.8, 3.9 |
| 5.3 | Never sell on a pre-set price target, never on one signal alone. | 2.6 |
| 5.4 | Never take an automatic fixed-gain profit trim. | 2.7 |
| 5.5 | Never buy or size up to diversify, balance, or fill capacity. | 1.5 |
| 5.6 | Never let a seat-asserted, uncheckable number rank or size a trade. | 1.8 |
| 5.7 | Never invent a missing value; never make skipping the permanent product. | 1.7 |
| 5.8 | Never loosen a stop (except the one re-audited widening in 3.3, which refuses a stop inside the noise band). | 2.8 |
| 5.9 | Never leave a position without a broker-side stop; never let a repair put back a stop the desk deliberately cancelled to sell. | 3.6; INCIDENT_HISTORY 2026-09-26 item 127 |
| 5.10 | Never fit a threshold to the desk's own track record. | 3.14 |
| 5.11 | Never pay for a news source. | 0.7 |
| 5.12 | Never add a short-only cap. | 0.5 |
| 5.13 | Never let the risk seat veto a whole plan over an advisory limit, or make any trade bigger. | 3.2, 3.3 |
| 5.14 | Never ship a feature default-off "to be safe"; never let a switch's declared state drift from its real state. | 0.10; OUTCOME "No feature can be silently off" |
| 5.15 | Never bundle failures into a summary, never signal severity by colour, never deduplicate a live failure. | 4.1 |
| 5.16 | Never poll a third-party API on a made-up timer when its documentation names the real mechanism — and never trust that mechanism until it is observed working in this deployment. | OUTCOME "the owner's question cut underneath" (*"I doubt the majority of people using this API just set up a simple timer like it's 1992."*) |
| 5.17 | Never discard anything that cost money to produce: persist every model answer, every decision and its reason, every broker number at a moment in time — written once, never edited; never store what code can recompute. | OUTCOME "Anything that costs money to produce gets kept" (owner ruling 2026-09-18) |
| 5.18 | Never store a real broker account id or real ticker-and-price desk output in the public repository. | WORK; this brief |

---

## 6. Money-governing numbers — value, derivation, soundness

The old build's number ledger holds 336 rows [measured 2026-10-01 from the
ledger]: 28 sourced, 6 instrument-read, 55 derived, 133 arbitrary, 114
not-trade-governing. **The desk's own convention records an owner-ratified
appetite number as `arbitrary` with the ratification in its note** — ratified
is not sourced. Below are the ones that govern money. "Sound" means a
derivation a reader can open and check; "Appetite" means the owner ratified
the value without a measurement; "Arbitrary" means nothing is behind it.

| # | Number | Value | Derivation | Verdict | Cite |
|---|---|---|---|---|---|
| 6.1 | Max risk per trade | 5% of equity, a ceiling not a target | Owner-ratified 2026-08-27. No measurement. The owner's own later reasoning (3.14) names defending it as "the identical mistake wearing a sign-off". | Appetite | OUTCOME risk envelope; ledger |
| 6.2 | Min risk per trade | 0.5% | Owner-ratified 2026-08-27 (a prior claim that it was unsourced was false in four places). | Appetite | OUTCOME "Correction 2026-09-18" |
| 6.3 | Max total at risk, correlation-adjusted | 25% of equity | Ratified 2026-08-27, re-ratified 2026-09-25 by adversary delegation. Survives the 2026-09-30 per-name ruling only as aggregate rationing. | Appetite | ledger |
| 6.4 | Max risk share in one correlation cluster | 40% | Ratified 2026-09-25 (delegated). Unsourced. | Appetite | ledger |
| 6.5 | Sector soft target / hard ceiling | 75% / 90% (hard = 1.5 x soft, terminal max 90) | Moved from 40% on 2026-09-01 on the "not a retirement portfolio" reasoning; the 1.5 multiple is in the ratified spec; 90 was "chosen when built". | Appetite (multiple is spec-sourced) | OUTCOME; ledger |
| 6.6 | Single-name notional ceiling | 65% of equity | Owner override 2026-09-11. Unsourced. | Appetite | ledger; INCIDENT_HISTORY 2026-09-11 |
| 6.7 | Gross exposure ceiling | 2.0x | Ratified 2026-09-01. Unsourced. | Appetite | ledger |
| 6.8 | De-levering ladder | -8% -> 1.5x, -15% -> 1.0x, -20% -> 0.5x peak-to-trough | Ratified 2026-09-25 as the deliberate never-liquidate loss defence. Rungs unsourced. | Appetite | ledger |
| 6.9 | Ladder owner-alert level | -10% | Cited to a published rule, with three recorded differences. | Sound | ledger |
| 6.10 | Min stop distance where no level backs the stop | 2.5 ATR | Re-derived 2026-09-10 "from doctrine not our own data"; the sourcing claim was WITHDRAWN 2026-09-30; ratified as appetite 2026-09-25; reformulation to need no constant is filed. Known to collide with any reward:risk floor (a ~15-session hold travels ~3.9 ATR, so 3x ATR made 1.5 R:R geometrically impossible). | Appetite, flagged for reformulation | board_notes item-090, item-199; OUTCOME |
| 6.11 | Absolute stop floor | 1.0 ATR | No derivation. | Arbitrary | ledger |
| 6.12 | Level counts as real for a tight stop | 5 touches | In-repo measured study (real vs shuffled bounce probability over 101 symbols, 5 years). | Sound (measured) | INCIDENT_HISTORY 2026-09-25 items 182/184/186 |
| 6.13 | Level-backed stop validity | level zone strictly narrower than stop distance | Ruled 2026-10-01; needs no constant. | Sound (threshold-free) | INCIDENT_HISTORY 2026-10-01 |
| 6.14 | Range-trade ratchets | breakeven at +1R; lock +1R at +2R | Described as "owner-ratified" in the 2026-10-01 item 212 decision; the ledger records both as arbitrary with a convention citation (R-multiple literature) but no point-value source. | Appetite / convention — CONFLICT noted 9.8 | INCIDENT_HISTORY 2026-10-01 item 212; ledger |
| 6.15 | Chandelier trail multiple | 3.0 ATR | Published default (LeBeau), URL cited. | Sound | ledger |
| 6.16 | Trailing-stop noise band | 1.25 ATR (trail) / 1.0 ATR (exit guard) | Settings called it measured; 2026-09-14 verification found nothing measured. Its width roughly equals the stop width, so broker stops do almost all real exits. | Arbitrary | ledger |
| 6.17 | Minimum ratchet | 2% above live stop | Mirrors an old reviewer rule of thumb. | Arbitrary | ledger |
| 6.18 | Trail-tighten cooldown | 4 calendar days | Forensic reason only. | Arbitrary | ledger |
| 6.19 | Structural stop buffer (no ATR available) | 0.5% past the level | Added for item 80; no source. | Arbitrary | ledger |
| 6.20 | Short gap-risk sizing haircut | 1.5x | Unsourced. | Arbitrary | ledger |
| 6.21 | Max entry slippage | 40 bps | Unsourced; after item 183 it is the only thing bounding what an entry pays. | Arbitrary | ledger; INCIDENT_HISTORY 2026-09-30 |
| 6.22 | Minimum order notional | $500 | Round number. Algebra checked 2026-10-01: the effective weight threshold drifts inversely with account size — "its own defect". Ruled 2026-10-01: not deleted, not ratified, to be replaced by a per-name read. | Arbitrary, ruled a defect | ledger |
| 6.23 | Longest hold | 60 sessions | Ratified 2026-09-25 as appetite; "not to be routed to the owner as a dial". Conflicts with 0.2 unless treated as an outer sanity bound. | Appetite | ledger |
| 6.24 | Swing pivot window | 3 bars either side (so 7 bars minimum for a reading) | Searched extensively; every candidate source ruled out. | Arbitrary (honestly recorded) | ledger |
| 6.25 | Exit-guard noise floors | four round numbers in four units | None read off the name's own dispersion. | Arbitrary | ledger |
| 6.26 | Stop-break confirmation | two trading-day CLOSING confirmation | Corrected from same-day; a one-day dip that reclaims its level is a known reversal pattern. | Sound (reasoned, not measured) | OUTCOME "recurred enough times to name the shape" |
| 6.27 | Model-call runaway cap | 40 calls per session | Set from real data (worst complete session 14); owner instruction 2026-09-03 to reconfirm after live sessions. | Sound (measured, provisional) | WORK "RECONFIRM AFTER A FEW DAYS LIVE" |
| 6.28 | Silence alarm | about one hour, desk-wide | Owner instruction 2026-09-03. | Appetite (owner-set) | INCIDENT_HISTORY 2026-09-03 |
| 6.29 | Rotation ranked-margin tier margin | 25% | Unproposed; tier stays OFF. | Arbitrary, switched off | board_notes item-219 |

Tally of the 29 listed: **7 sound** (6.9, 6.12, 6.13, 6.15, 6.26, 6.27 and the
1.5 sector multiple inside 6.5), **11 owner appetite without measurement**
(6.1-6.8, 6.10, 6.23, 6.28), **11 arbitrary** (6.11, 6.14 in part, 6.16-6.22,
6.24, 6.25, 6.29). Under ruling 3.5 every appetite number is a global dial and
therefore, in the owner's own words, a defect to be replaced by a per-name
read — the rebuild should carry them only as outer sanity bounds and build the
per-name reads.

---

## 7. Process rulings the rebuild must preserve

| # | Requirement | Owner words / source | Cite |
|---|---|---|---|
| 7.1 | Nothing waits on the owner except money, mandate, risk appetite and public disclosure. The adversary stands in for him; the orchestrator decides and records the decision and reason before building on it. | *"I don't want you waiting on me on anything. You have the adversary in my place. Just make sure it gets documented."* (2026-09-18) | WORK "To be decided by the orchestrator" |
| 7.2 | Prompt text is code: a rule stated in a prompt needs a mechanical check that fails when prose and behaviour disagree; when a mechanism is deleted, search every prompt, assembled string and comment for its name. | 2026-09-17/18 | OUTCOME "Prompt text is code that can rot" |
| 7.3 | An audit ends in numbered items with owners and dates, or in a mechanical check — never an inventory. | 2026-09-17 | OUTCOME "A finding with no owner" |
| 7.4 | Prove a path has succeeded at least once before optimising it; confirm whose retry loop is running before tuning it. | 2026-09-17 | OUTCOME |
| 7.5 | No model comparison until the job board is clean; nobody proposes the run or its spend to him. No test-environment work unless he asks. | owner rulings 2026-09-13, 2026-09-15 | WORK "DECIDE BY 2026-10-31" |
| 7.6 | Deploy any time during beta; auto-fix after each health report on his allowance; adversary on every change. | owner 2026-09-19 | [session memory; repo record not located — `GAP 7.A`] |

---

## 8. UNRATIFIED — assumptions inherited from the old build that the new one must not blindly copy

No owner ruling was found behind any of these. Each is a question for the
rebuild, not a requirement.

| # | Inherited assumption | Why it is suspect | Cite |
|---|---|---|---|
| U1 | Five analyst seats exactly (technical, news, earnings, macro, smart-money), one vote each at hard +/-1. SUPERSEDED 2026-10-19: each seat states a view and how sure it is (or "I don't know"), weighed by measured record, never a hard vote (OUTCOME, desk archetype). | Count is a product of what was built; no ruling fixes it. | board_notes item-109 |
| U2 | Net-evidence gate: a name is refused below net +1 and net evidence caps size. SUPERSEDED 2026-10-19: no net-vote gate; the head trader decides on weighed views and risk alone may veto (OUTCOME, desk archetype). | Decided by orchestrator 2026-10-01, not owner-ruled. | INCIDENT_HISTORY 2026-10-01 item 208(a) |
| U3 | Seat weights in the ranking composite. | Published weights, unverified; 70% of technical reads land on one score so ranking fell to alphabetical. | board_notes item-157 (per memory); WORK item 141 |
| U4 | Sole-backer rules of any kind. SUPERSEDED 2026-10-19: weighed views and the head trader's written decision replace them (OUTCOME, desk archetype). | board_notes item-109 |
| U5 | Six scheduled sessions per day (morning, half-hourly intra-check, midday, close, evening, earnings pre-process) at fixed clock ticks. | Schedule is an artefact; 2.1 demands the bar be tested "multiple times a day" but fixes no count. | WORK; board_notes item-219 |
| U6 | Reward:risk tiebreak within a rank tier. **Removed from the live build 2026-10-10** (owner ruling 9 Oct); ties now fall to level touches, then symbol; the ratio is still recorded. | Rests on a target, which the owner ruled is a made-up number. | INCIDENT_HISTORY 2026-10-01 item 208(a) |
| U7 | Entry orders cancelled after 90 seconds unfilled. | No source; alert on cancel is conditional. | INCIDENT_HISTORY 2026-09-30 item 183 |
| U8 | Three-attempt stop-placement retry burst. | Platform convention. | INCIDENT_HISTORY 2026-09-25 item 129 |
| U9 | Every number in 6.11, 6.16-6.22, 6.24, 6.25. | Recorded arbitrary. | ledger |
| U10 | The dormant fill websocket and REST-polling fallback design. | Vendor mechanism never authenticated here; keep only what is observed working. | OUTCOME |
| U11 | Insider-cluster admission screen (2 owners, $5 minimum price, etc.). | Round numbers, no ruling. | ledger SmartMoneyConfig rows |
| U12 | Fallback and de-lever stops carry a 3% buffer. | Survives only because primary stops moved to stop-market. | INCIDENT_HISTORY 2026-09-26 |
| U13 | Congressional-trading feed weighting. | Owner ruled on the evidence 2026-09-20 (entry located but not read here); treat the current weighting as unverified. | INCIDENT_HISTORY 2026-09-20 |
| U14 | Regime stop scales 1.2 / 1.1 / 0.95. | Ratified as appetite 2026-09-25 by delegation; no measurement. | INCIDENT_HISTORY 2026-09-30 item 199 context |
| U15 | The LLM seat architecture itself (manager seat, risk seat, reviewer seat) and the incumbent model. | The 2026-10-31 decision is pending; owner said no comparison until the board is clean. | WORK |

---

## 9. Conflicts and overrides between rulings (explicit, dated)

| # | Earlier | Later | Resolution |
|---|---|---|---|
| 9.1 | 100% invested, idle cash is a cost (2026-09-17). | Conviction outranks balance; undeployed margin acceptable (2026-09-25). | Later subordinates earlier: deploy fully INTO conviction only. 3.10 reads with 1.5. |
| 9.2 | Board item 75: send a profit target to the broker, allow profit-taking as a sell reason. | Exit on alignment, never on a target (2026-09-30). | Item 75 retired unbuilt; building it is now the defect. |
| 9.3 | "Nothing can green light a name on its own" (2026-09-25). | Same-day build excludes only technical from sole backing. | SETTLED 2026-10-19 by the desk archetype (OUTCOME): no green-light and no sole-backer rule; views are weighed and the head trader decides. |
| 9.4 | Exit on alignment, never on one signal (2026-09-30). | Unconditional sale on failing the entry bar (2026-10-01), where one seat's opposition can trigger it. | Argued NOT a conflict: alignment bars a price TARGET; the bar-failure sale is a conviction failure re-running entry gates. Recorded 2026-10-01. |
| 9.5 | "Numbers may come from this desk's own measured track record" (pre 2026-09-12). | Fitting to own record forbidden (2026-09-12). | Later wins; the earlier permission was declared wrong. Carve-out 2026-10-19: re-measuring each seat's ranking weight from its record against chance (James-Stein shrinkage to the group mean) is a reading the desk archetype requires, not a forbidden fit (OUTCOME). |
| 9.6 | Sector cap 40% (retirement frame). | 75% / 90% (2026-09-01). | Later wins. |
| 9.7 | Account-level loss halt, 5/20-day brakes, emergency sell-all. | Removed entirely (2026-09-20). | Later wins; ladder kept. |
| 9.8 | Range ratchets described as "owner-ratified" (item 212, 2026-10-01). | Ledger records the same numbers as arbitrary with no point source. | Unresolved record disagreement; treat as appetite at best. |
| 9.9 | Fractional shares excluded (original). | Reversed and built (2026-09-01). | Later wins. |
| 9.10 | Risk envelope as fixed percentages (2026-08-27, re-ratified 2026-09-25). | Risk is per-name, never a global dial (2026-09-30). | Later wins in principle; the numbers survive only as aggregate bounds pending per-name reads. |
| 9.11 | Short selling and congressional feed shipped behind flags. | Flags rejected (2026-08-29) / feed switched on (2026-09-20). | Later wins: features ship on. |
| 9.12 | 60-session longest hold ratified (2026-09-25). | Holding period is an output, never a setting (2026-09-01, reaffirmed 2026-09-25). | Treat 60 as an outer sanity bound only. |

---

## 10. Gaps honestly left open

- `GAP 1.A`, `1.B`, `4.A`, `7.A`: rulings held in session memory without a repository record. Re-confirm with the owner before they become requirements.
- `GAP 3.A`: the stop-MARKET ruling of 2026-09-25 is referenced by a later entry; its own entry was not located.
- The moving-average-cross exit demanded by 2.6 was verified absent from the old code on 2026-09-30; the rebuild builds it fresh and no parameters for it have been ruled.
- "How old is too old" for a seat read (2.3) is the owner's appetite question, left open on purpose.
- Which model runs the decision seat is pending (decision due 2026-10-31, moves rather than forces).
