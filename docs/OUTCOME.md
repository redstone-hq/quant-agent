# QAMC Product Outcome

This file states the result QAMC is trying to achieve. It is intentionally less prescriptive than the architecture and roadmap: Claude Code should use it to challenge whether the current plan is actually the best way to reach the outcome.

## Outcome

## This is a trading desk, not a retirement portfolio

**Owner mandate, 2026-10-09 — every risk, sizing, exit and capital question is
shaped by these two goals, in this order: preserve capital; make money daily,
or weekly when the market does not offer daily opportunities.** His words:
*"Preserve capital, make money daily or weekly. Whatever is more efficient,
whatever is more possible. Given the trading climate, you can't force the
market."* And: *"This isn't a retirement fund. This is a make money now trading
desk!!"*

- **Not reckless — adaptive.** The desk trades what the market is giving it
  now: bull, bear or range. It reads the regime and trades accordingly; it
  never forces a trade the market is not offering.
- **Capital preservation comes first, and it does not mean parking money.**
  *"Capital preservation does not equal put it all into bonds or ETFs, making
  three percent a year."* Preservation means bounding each loss and cutting
  losers fast, never sitting out in low-yield holdings.
- **Aggressive, never gambling:** high-probability wins; take the profit and
  move on. The desk is constantly looking for new opportunities, and the
  universe it searches (more equities, ETFs) should widen.
- **Taking profit early is regime-dependent:** wrong in a trending market,
  right only in a sideways one. ATR and the existing math decide which regime
  a name is in — not a fixed rule.
- **Never sell part of a winner.** The whole position is sold and the desk
  moves on; no free-ride remainder. This holds for cuts forced by the
  drawdown leverage ladder too (owner, 9 Oct: *"You always sell losers first,
  and you don't break the rule, sell part of a winner."*): it sells whole
  losing positions, biggest loss first, and only when no loser is left the
  weakest-rated winner, whole (owner, 12 Oct). Selling whole may take the
  book below the new limit; that is accepted.
- **Cancel, wait for the broker to confirm, then sell (owner, 13 Oct).** A
  sale that must first cancel its protective stop waits until the broker
  reports every cancelled stop dead; a few minutes without a stop while a
  cancel completes is accepted, and the 30 Sep "never cancel, amend in place"
  doctrine no longer binds. The sale is exactly the intended size or nothing,
  with a per-symbol reason (`cancel_unconfirmed`, `cancel_read_failed`,
  `shares_reserved`, `position_disagrees`, `stop_filled_in_part`). A sale
  whose cancel is not confirmed inside the pass's one wait is PUT OFF (14
  Oct): nothing is sent that pass, and the run that decided it owns it. Every
  session run ends in one seam that re-checks its put-off sales — legs
  terminal, position re-read, the sale sized to what is held now, priced as a
  plain exit — bounded by the run's own window end or its unit's hard kill.
  Past that bound nothing is sold, the write-ahead row puts the stop back once
  the broker confirms, one alert names the stock, and its decider decides
  again at its next run. The trend tick defers a put-off sale to its next
  tick. Stops put back after a sale that did not go are sized up to what is
  held, never placed over or counted from a stop still dying.
- **No risk-tolerance number is asked of the owner.** *"I don't want to make
  up numbers."* Every risk number is derived from the two goals above plus
  each name's own volatility, or adopted from published practice; a limit that
  cannot bind is deleted, not tuned.
- **No small desk trials for settled questions.** *"Something that makes sense
  today won't make sense tomorrow or in the next hour."* A question already
  settled in published practice adopts the standard; a regime-dependent idea
  is not fitted from a few weeks of one market.

**Owner correction, 2026-09-01. Read this before applying any received wisdom
about portfolio construction.** Much of the risk framework in this repo was
written using the vocabulary of long-horizon investing, and several rules quietly
inherited that frame's goals. They are not this project's goals.

His words: *"This isn't my 401k / RRSP. This is leverage trading for profit. If
one sector is hot and we're looking at a short-term horizon, I don't see a
problem with that... This is a trading desk, not a long-term retirement desk."*

**What follows from that, concretely:**

- **Sector diversification is not a goal here.** Spreading across sectors
  protects a decades-long compounding portfolio from a sector's structural
  decline. That risk is irrelevant over a multi-day hold. Concentration in a
  hot sector is a legitimate and often correct trade. A sector limit's ONLY
  defensible job here is bounding correlated blow-up risk — one shock taking
  several positions at once — and it should be sized for that, not for
  diversification. **The 40% target was a retirement-portfolio number; it was
  revisited on those grounds and moved to a 75% target / 90% absolute
  ceiling (`docs/WORK.md` spec §12.3; `config/settings.yaml`
  `max_sector_pct` / `max_sector_hard_pct`).**
- **A long and a short in the same sector are NOT a hedge.** They are two
  separate opportunity trades that happen to share a sector label. Treating
  them as offsetting imports a portfolio-construction assumption that does not
  hold for a desk trading opportunities. See the signed-vs-gross exposure
  decision in `docs/WORK.md`.
- **Holding period is an OUTPUT, not a setting.** A position is held while its
  thesis is progressing and the capital cannot do better elsewhere — judged on
  ATR, velocity, realised progress toward target, and opportunity cost. A fixed
  horizon is a retirement-frame artefact. Owner: *"depending on the stock's
  performance is how long we should be holding the stock."* Where the system
  currently uses a stated `expected_horizon_sessions` as an input to arithmetic,
  that is a modelling convenience, not a strategy, and it is now known to
  distort reward:risk (see below).
- **Idle cash is a cost, not safety.** Preservation is not the objective;
  risk-adjusted return is.

**None of this loosens a risk limit.** Stops, per-trade risk, total risk
budget and drawdown de-levering all exist to stop the account being
destroyed, and they stay. The owner removed the correlated-group (cluster)
limits on 15 Oct 2026 and nothing replaced them. What changes is that rules
justified by *diversification* rather than by *survival* must re-earn their
place on trading-desk grounds.

**Why this is written down.** Rules built on the retirement frame have already
cost real money here. On 2026-09-01 the desk reviewed 38 qualified signals and
placed zero trades, and the deepest cause was two individually sensible rules
that were never checked against each other: stops are widened to a minimum of
3x ATR, while reward:risk must clear 1.5. Over a ~15-session hold a stock
travels roughly 3.9 ATR, so the best achievable ratio is ~1.29 — below the
floor, always. SLB that morning scored 1.28 against a geometric maximum of
1.29. **The analyst was producing the best number arithmetic allows and the
system rejected it.** `config/settings.yaml` even documents the mechanism next
to the setting — "a widened stop lowers reward:risk because the target does not
move" — and the floor was set anyway.


Build an autonomous AI-assisted **Alpaca trading system** whose purpose is to **make money**.

QAMC is a systematic trading desk, not an experiment in model quality. Every design decision serves risk-adjusted return; nothing here is justified by being interesting.

**The edge, stated so it can be tested:**

> **Breadth x consistency x asymmetry.** Underwrite the full liquid universe daily across technicals, fundamentals, news, macro and insider flow — coverage no individual could sustain. Act identically every time, long or short. Risk a bounded fraction of equity per idea, cut losers at pre-defined structure, and let winners run. Be right slightly more often than not, and make materially more when right than is lost when wrong.

This is a hypothesis until the measured win rate, average win/loss ratio and expectancy confirm it. Measuring it is a first-class requirement, not a reporting nicety.

**Horizon:** swing — days to weeks.

**Capital is to be deployed.** Idle cash is a cost, not a safety measure. **The desk stays 100% invested (owner mandate, 2026-09-17): nothing sits in T-bills or anything else that only yields interest, because cash earning less than inflation is a loss and the desk can always go long or short.** Macro informs direction, never how much capital sits idle; the only cash is the small execution reserve, and any other undeployed cash must be explained. Capital preservation is achieved by **bounding the size of each loss**, never by declining to participate.

**Risk envelope (owner-ratified 2026-08-27):**

| Parameter | Value |
|---|---|
| Max risk per trade | 5% of equity — a ceiling, not a target |
| Min risk per trade | 0.5% — below this, do not trade. Judged on the whole holding (held plus new); an order is refused, never sized up to reach it (owner ruling 2026-10-12: "we also must filter out small trade churn") |
| Max total at risk | 25% of equity, plain stop-distance risk |
| Position count | Not fixed. Determined dynamically by the risk budget. |

**Correction, 2026-09-18:** the 0.5% minimum was reported internally as unsourced — existing only as a dataclass default, absent from config and from any ratification record. That report was wrong. Verify directly: `config/settings.yaml` (`risk.min_position_risk_pct: 0.5`), the row above (this file, owner-ratified 2026-08-27), and `src/risk/constants.py` (`STARTER_POSITION_RISK_PCT`, whose own docstring names it the same field). Check a documented claim against the file before repeating it.

Correlated-group limits were removed by the owner on 15 Oct 2026 and nothing replaced them. Risk is released as trades prove themselves: once a position's trailing stop sits at or above entry it stops consuming budget, so the book expands when the desk is right and contracts when it is not.

**Conviction is expressed as risk allocation, not as percent-of-portfolio notional.** The specialist team decides how much conviction an idea carries; deterministic code converts that into a share count using the analyst's stop. A wider stop yields a smaller position, never a tighter stop.

QAMC is being validated first in Alpaca Paper. Paper is the current execution environment and safety authorization, not the product identity. If the system earns progression to live capital, the same decision, risk, execution, position-management, journaling and observability architecture should carry forward without a paper-to-live redesign.

The system should run largely unattended while giving the operator a browser/iPad Dashboard that makes the trading process understandable rather than opaque.

**Directional neutrality is a product requirement, not a promise of constant activity.** QAMC should not structurally depend on rising equity markets to have an opportunity set. Within the instruments and risk architecture actually supported by the project, it should be able to express bullish, bearish or neutral/cash views and evaluate missed opportunities in both directions. **Direct short selling is authorized** (owner ratification, 2026-08-27) and is to be implemented against an Alpaca margin account, with borrow-availability checks and risk treatment appropriate to unbounded downside. Until it ships, bearish expression runs through the approved inverse ETFs already in the universe — which requires the Portfolio Manager to be told those instruments are bearish, something the codebase does not currently convey.

Options and theta strategies remain outside the accepted architecture.

The operator should be able to understand, without reading raw logs:
- current account/equity/P&L/positions/orders/trades and system health;
- current directional posture and whether apparent cash is raw cash, sweep-parked liquidity or risk exposure;
- what candidates the system considered;
- what specialist agents concluded and where they disagreed;
- what the Portfolio Manager proposed;
- what the AI Risk Manager changed or rejected;
- what deterministic Python ultimately allowed or blocked;
- why an active session produced no trade when candidates existed;
- what actually executed versus what was proposed;
- which model/provider actually answered, its cost/latency/tokens, and whether fallback occurred;
- what happened on prior days through a useful journal and forensic search;
- what meaningful bullish or bearish opportunities the system missed;
- whether model/prompt choices appear to add measurable value over time.

## The desk archetype: specialists, weighed, then one head trader

**Owner, 2026-10-19: this is the picture every rule must follow.** When a
smaller rule is unclear, derive it from this section; do not ask the owner
to settle a detail this section already settles. His words: each agent "is
supposed to be a specialist in its own right. But some agents' decisions
weigh heavier than others ... there's a synergy there that takes place of
all the minds."

1. **One shared shortlist.** Ideas come from the market scan, the
   permanent list, held positions and seat nominations. Every specialist
   looks at the SAME shortlist; a name no specialist examined cannot be
   judged, and a scan whose names no specialist reads is a defect.
2. **Specialists, each an expert in one thing.** Chart (setup and timing),
   earnings (the business), news (the reason to act now), insider and
   congressional buying (where informed money goes), macro (the backdrop).
   Each gives its own view and how sure it is, and may say "I don't know".
3. **Views are weighed, not counted as votes.** For a days-to-weeks hold
   the chart carries the most weight; a fresh news or earnings catalyst is
   the "why now"; macro is the backdrop that tilts size and direction but
   rarely blocks a name, because single stocks can buck the market. A strong
   chart plus a fresh catalyst is a tradeable idea even when macro is
   lukewarm. The weights are NOT picked: they come from each seat's measured
   record against chance (the voters-vs-random scoring) and are re-measured.
   This is a READING, not FITTING (see "No arbitrary numbers"): the record is
   re-read every session over a rolling window, never calibrated once and
   held, and with a thin record every seat stays at the group average (the
   published shrinkage standard: James-Stein, DeMiguel 2009 equal weights).
   A seat earns more weight only when its record beats random picks on the
   same shortlist, including shorts.
4. **The head trader decides.** The planning AI hears every view, settles
   disagreements, and must answer the strongest dissent in writing when it
   overrules it. That combining step is the synergy. Specialists do not
   debate freely among themselves: free AI debate drifts to agreement and
   costs a call per round. Disagreement is surfaced to the head trader, and
   the risk seat is the independent challenger.
5. **Risk is independent.** It sizes every trade from the stock's own
   volatility and the account's limits, caps total exposure, and can shrink
   or veto a trade. It never picks stocks.
6. **Both directions.** A short is judged exactly like a long.
7. **Then deterministic execution, protection and review.** Orders, stops,
   the half-hourly checks and the reviews run as fixed code and AI
   reviewers; every outcome feeds the seats' measured records, which feed
   the weights in step 3.

## No arbitrary numbers, ever

**Owner correction, 2026-09-04, restated after recurring on the same
pattern multiple times in one night — read this before setting or
defending any threshold, cap, or rule in this codebase.** See
`docs/WORK.md`'s session-start section for the current short version; this
is the fuller statement of the same principle.

**PARTIALLY ENFORCED SINCE 2026-09-18.** Until then this section was
honour-system prose and the numbers it forbids accumulated under it. There is
now a check — `src/number_sources.py`, failing through
`tests/test_number_sources.py` — that requires a numeric definition site
inside a declared scope to carry an entry in `config/number_ledger.yaml`
recording where the number came from. 324 sites are in scope (179 before
2026-09-19's board item 130 admitted `src/execution/broker.py`,
`src/coverage_watchdog.py`, `src/pipeline.py` and `src/agents`, 226 before
the scanner learned the same day to see function-parameter defaults,
numeric attributes on any class, and near-one inline price/size multipliers
such as `price * 0.995`); 136 distinct numbers are recorded as having
nothing behind them [measured 2026-10-01 from the ledger itself, the figure
the ratchet test enforces]. The 146 this file carried was stale long before
board item 186: the live count was 137 before that item deleted the
queued-earnings clamp's 5 and 136 after.

**What the mechanism actually does, stated exactly, because an authority file
must not claim more than the code does.** It is a COVERAGE and CONSISTENCY
check over a declared scope, not a proof that any number is sourced.

  * Scope is a hand-written file list plus a stated admission rule (every
    module from a verdict to a broker order). A second check counts
    module-level numeric constants in the `src/` files OUTSIDE that list and
    fails if the count rises, so a new one cannot arrive silently — but the
    list itself is still reviewed by a person, not derived.
  * It pins the CODE DEFAULT. For 52 of these sites the value the desk
    actually trades comes from `config/settings.yaml`. Since 2026-09-18 the
    deployed value is checked against the ledger too; before that,
    `risk.max_position_risk_pct: 5` could have been edited to `10` with the
    check silent.
  * It cannot tell a TRUE justification from a plausible one. This is not
    hypothetical. The ledger's own flagship entry, written on shipping day
    about the 0.50% minimum-risk floor, was false in four places — it said
    the number was absent from `settings.yaml`, absent from every document
    and absent from any ratification record, when it is at
    `config/settings.yaml:751`, is the owner-ratified row at line 84 of THIS
    file, and was ratified on 2026-08-27. Every claim was one grep from being
    disproved. `source` must now be a URL or a `path:line` for that reason.
  * It sees a number only in one of five SHAPES: a module-level constant, a
    `*Config` field, a function-parameter default, a class attribute, or a
    multiplier/divisor literal between 0.5 and 2 (excluding 1). A threshold
    in a comparison (`> 50`), an additive offset, a divisor like the
    `/ 10.0` in the level-strength formula, a fallback argument, or a
    keyword literal at a call site is still invisible. Hoisting such a
    literal to a named constant is what makes it visible.
  * It does not source any of the 146. That is `docs/WORK.md` item 90's open
    half, and every one of them is the owner's to move, not an agent's.

Read `src/number_sources.py`'s docstring before adding a constant; the
ledger's header states what each classification requires. `arbitrary` is now
the most expensive entry to write rather than the cheapest: it requires a
note, the open question in answerable form, and what the desk pays while it
is unanswered. That is three of the five things outcome 3 below demands of an
open number. The schema does NOT yet require the other two — what was already
searched and ruled out, and what evidence would settle it. Outcome 3's rule
that a citation "is a URL a later reader can open and check" is what the
`source` requirement below now enforces mechanically.

## No feature can be silently off, ever

**ENFORCED SINCE 2026-09-19**, after an audit found `congress_enabled`
(`SmartMoneyConfig`, shipped 2026-09-04, #271, deliberately off by default)
had never been switched on in the five weeks since, while three owner-facing
surfaces — the Telegram smart-money label, the pre-market refresh log line,
and `docs/qamc_trading_desk_workflow.html` — kept describing congressional
data as running. A switch being off is not itself a defect; nothing checking
whether its declared state still matched its real one, or whether it existed
at all outside its own field definition, was the defect.

`src/feature_flags.py`, failing through `tests/test_feature_flags.py`
(same `pytest` job as the number ledger above — no new required CI check
name), requires every boolean field on every `src.config.*Config` class to
carry an entry in `config/feature_flags.yaml` recording its EFFECTIVE value
(`config/settings.yaml` layered over the pydantic default, resolved the same
way `src.config.load_config` builds `AppConfig`), whether that value was
chosen deliberately, and why. As of this writing 18 switches are declared;
where git history and the code's own comments recorded no reason, the entry
says "reason not recorded" rather than inventing one.

**What it does not do, same caveat as the number ledger above.** It is a
coverage and consistency check, not a proof any `reason` is true, and it
cannot read a Telegram label, a log line, or an HTML page and check that it
agrees with a switch's real state — that mismatch is still a human
documentation pass. Read `src/feature_flags.py`'s docstring for the exact
scope rule and the tri-state (`bool | None`) blind spot it flags a sentinel
against.

Every constant that governs a real trade decision — a stop distance, a
holding period, a risk percentage, a tolerance band, the retired reward:risk
reference a range trade's payoff is still measured against for ranking
(no longer a gate anywhere, never a size cap, never applied to a breakout,
and self-flagged in `src/risk/constants.py` as the last flat number still
standing) — must be READ FROM THE INSTRUMENT IN FRONT OF YOU: its volatility, its
structure, its confirmed price action, or the trade's own claim recorded at
entry. It must never be a flat calendar count, a round percentage, or a
number chosen because it "sounds prudent."

**Corrected 2026-09-12 — this clause used to also permit "or from this
desk's own measured track record", and that permission was wrong.** Fitting
a threshold to past outcomes is not the opposite of an arbitrary number; it
is an arbitrary number with a backtest stapled to it. The owner's reasoning,
and it is decisive: markets change, regimes change, and a black swan is
precisely the event no history contains. A rule tuned to what already
happened breaks at the moment it matters most.

The distinction to apply:

  * **READING** — the number comes from what is in front of you right now and
    is re-read every session, so a regime change UPDATES it rather than
    invalidating it. This stock's ATR today. This chart's levels today. This
    trade's own pinned horizon. Seat weights read from each seat's record
    against chance over a rolling window, re-measured every session and shrunk
    to the group average when the record is thin, are a READING (desk
    archetype, step 3). All acceptable.
  * **FITTING** — the number is calibrated from what happened before, then
    held fixed. **Not acceptable, and no amount of backtesting makes it so.**

This desk has already been bitten by exactly this: `pace` once measured a
position against the desk's own rolling average holding period, so every
early sale shrank the average, made every surviving position look stalled,
and drove more early sales. A self-tightening noose, and the single largest
identified P&L defect in the system. That is what fitting to your own record
does even when the arithmetic is correct.

**When a number cannot be read from present data, do not fit one and do not
quietly leave the feature switched off.** Off is a decision too, and an
unowned one rots: nobody stated why, nobody owns it, nobody revisits it.
There are exactly three permitted outcomes, in this order:

  1. **REFORMULATE the rule so it needs no constant.** "Is the trend over?"
     needs a number; "does the last higher low still hold?" does not, and the
     chart supplies the level. Always try this first.
  2. **Find the number in published research and cite the fetched source
     beside it.** A citation is not a number that sounds authoritative — it
     is a URL a later reader can open and check.
  3. **If neither: a written, owned item on the board.** It must state the
     exact open question in answerable form, where the current value actually
     came from (inherited / convention / invented / a platform default), what
     was already searched and ruled out so the next person does not repeat
     the work, what evidence would settle it, and what it costs the desk
     while it stays unanswered.

Outcome 3 is **not** "leave it off and move on", and it is **not** "ask the
owner". Market-structure questions go to published research or stay open as
investigation; only money, mandate and risk-appetite questions are his. A
feature left switched off with an unanswered question behind it is an
abandoned problem wearing the appearance of discipline. An item may be
deleted when its question is ANSWERED — never when it is merely declined.

**A number does not become non-arbitrary because it was previously
approved.** This applies to Claude's own reasoning as much as to the
code: on the same night this was written down, an earlier flat 5%
risk-per-trade figure was defended as correct on the grounds that the
owner had ratified it weeks before — that is the identical mistake in a
different location, just wearing a sign-off instead of a comment. Approving
the wrong SHAPE of a rule does not fix it.

This has recurred enough times to name the shape: a flat "protect for 5
days" holding rule, later replaced with a real price-level-break check; a
same-day break trigger that had to be corrected to a two-trading-day
closing confirmation once it was pointed out that a one-day dip which
reclaims its level is a well-known reversal pattern, not evidence of a
breakdown; an ATR-based tolerance reused for a new purpose at the wrong
tightness because nobody checked whether the number fit the new job; and
the 5%-risk defense above. Treat a new instance of this shape as the same
recurring bug, not a fresh question — and see `docs/RESEARCH_FINDINGS.md`
for what in this codebase has actually been measured versus merely
asserted. If a real number cannot yet be derived from data or a measured
record, mark it explicitly as provisional — never let it read as settled.

**Exits, specifically (owner decision, 2026-09-12).** Profit-taking is
trailing-stop-driven and nothing else: the reward side of a trade cannot be
predetermined because the holding period is unknown, so a preset profit
target — sell a fixed fraction at a fixed gain, decided in advance with no
reference to what the instrument is doing — is rejected as a class, exactly
as reward:risk was rejected as a universal entry gate. The 30%/15%
automatic take-profit trim inherited from upstream (tuned on one GOOGL
trade) was deleted under this rule; `tests/test_pipeline.py::
test_no_fixed_gain_automatic_profit_trim_exists` keeps it out.

## An unverifiable number must never rank or size a trade

**Owner decision, 2026-09-12.** Proposals compete for a finite risk budget. A
model that supplies a confident number scores better than one that admits
uncertainty — so if an unverifiable number is allowed to influence ranking or
position size, **the desk selects FOR fabrication.** The capital flows to the
least honest proposal, and a genuine opportunity loses to an invented one.
That is not noise; it is a bias with a direction, and it compounds every
session.

The rule: **anything that ranks or sizes must be computed by this desk from
the instrument** — its price, its volatility, its levels. A number the model
asserts and nothing can check may inform a human-readable explanation. It may
never compete for capital.

The corollary matters as much. **Requiring a number the analyst cannot know
does not produce a refusal — it produces an invention**, and an invented
horizon is indistinguishable downstream from a real one. So *"I could not
build a proposal: insufficient data"* must be a first-class, recordable
answer at every seat. A schema that has no way to say "I don't know" is
asking to be lied to.

## Missing data is a defect in the step that should have produced it

**Owner standing rule, 2026-09-17. Broader than any one field.** If a
required piece of data is missing anywhere end-to-end in the desk process,
the root cause is that the step that should have produced it did not. Find
why. Fix that step so it actually produces the data. Never invent the
missing value. Never make skip / drop / ignore-and-continue the permanent
product.

A quarantine that drops one name so the rest of the book can trade is a
patch. It stays labelled temporary until the producing step fills. The
current instance is a blank "I'll sell if". The rule is not limited to that
field.

This is the same bias as the unverifiable-number rule above, applied to
absence: a seat that cannot say the thing, and a pipeline that then skips
the blank, selects for silent omission the same way a required unverifiable
number selects for invention. Both are forbidden. *"I don't know"* remains
a first-class, recordable answer. Skipping the name is not how that answer
ships — heal, re-ask the seat, and if it still cannot produce the field,
refuse that name with a durable reason. That refuse is last-resort, not the
product.

## Prompt text is code that can rot

**Found twice live, 2026-09-17.** The desk's own rules are asserted in prose
inside the model prompts. Nothing ties that prose to the code it describes,
and nothing notices when the behaviour changes underneath it. Two live
instances were found the same day: a rule cited to the owner by number whose
text says the opposite of what it was cited for, and a reviewer exemption
decided off a classification the rest of the path disagrees with.

Treat prompt text as code. A rule stated in a prompt needs the same thing a
constant needs: something mechanical that fails when the prose and the
behaviour disagree. Rewording a prompt is a behaviour change, not
documentation.

**Measured wider, 2026-09-18.** Seventeen false statements were found in the
three seats that pick and size trades, eight of them able to change a trade.
Two facts came out of trying to design a check for it, and both point away
from the obvious answer. First, scanning prompt text for numbers does not
work — about 1,825 number-like tokens, mostly dates and list numbering.
Second, and decisive: neither confirmed defect lived in a prompt FILE at all.
Both were strings the code assembles as it runs, so a file scanner would have
caught neither.

**The check that works is at the DELETION site, not the reading site.** When a
mechanism is removed, search for its name across every prompt, every assembled
string and every comment, and fail if it still appears. The flagship case was a
deleted mechanism still described in prose with no number in it — nothing that
inspects the prose for suspicious content could ever have found it, because
the prose was not suspicious. Only the deletion knew.

**And the rot is not confined to prompts.** The same day, a code comment was
found reasoning at length about a reward:risk floor that the same file
declares dead two hundred lines above and that the constants module marks
inert. A comment is the cheapest place for a deleted rule to keep living,
because nothing ever executes it. A related trap sits one step further out: a
number can outlive its own derivation silently. A stop scaler was derived as
"the tightest value that keeps the stop outside the measured noise band" on a
base that was later changed, and at the new base that constraint no longer
binds — the number survived, its justification did not, and nothing announced
that it had become arbitrary again. When a base or an input changes, re-check
every number that was derived FROM it, not just the ones that reference it.

## A returned value that nobody catches is a check that does not exist

**Found 2026-09-18.** Two reconciliation routines computed real answers —
which positions the broker had closed behind the desk's back, and which
unprotected positions had just been re-protected — and every one of their five
call sites called them as bare statements, keeping nothing. What proved it was
an unfinished pattern rather than a decision was the third sibling beside
them, whose return IS captured and threaded into the session result, and which
is the only reason a missing stop reaches the owner at all.

State this kind of gap precisely or it misleads. Both routines logged what
they did, so the information existed; what it could not do was reach a session
result, a message, or any test that reads one. That is a REPORTING gap, not a
detection gap, and calling it silent would have put the fix in the wrong
place. The pattern to distrust on sight: a function that returns a value being
invoked as a statement.

## Anything that costs money to produce gets kept

**Owner ruling, 2026-09-18.** Keep anything a model said, any decision and
the reason behind it, and any number the broker gave at a moment in time —
write each once, never edit it afterward. Do not keep anything the code can
recompute from those: two stored versions of the same fact that can drift
apart and disagree is its own defect, not a safety margin.

Reason: a paid seat forms a judgement and today that judgement is discarded,
so the next run pays again for a sentence the desk already owns. Worse,
grading the desk's own reasoning after the fact is impossible unless the
reasoning sits beside what actually happened next. This is doctrine, not an
open item — apply it whenever a run's persistence is being designed or
reviewed. See item 116 (`docs/WORK.md`) for where it is not yet applied.

## A finding with no owner and no due date will be lost

**Measured, 2026-09-17.** An audit that produces an inventory rather than
items does not get worked. The ~30 unsourced trade-governing numbers were
catalogued on 2026-09-11 and filed as "inventory, not an item" with a note
saying "never re-audit". Nothing was assigned, nothing had a date, and all
thirty were still live and still governing trades a week later.

The rule: an audit ends in numbered board items, or in a mechanical check
that fails the build. A finding with no mechanical surface is a finding that
will be lost. This is the same lesson as the status board itself — everything
mechanically enforced holds; everything relying on someone remembering slips.

## Prove a path has succeeded once before optimising it

**Measured, 2026-09-17.** Six separate passes optimised the TIMING of the
live-fill websocket, which had never authenticated once in any session since
it was built. A 100% failure rate is not a race condition. Before any work to
make a path faster or more reliable, prove it has succeeded at least once.

The adversary review missed it for a related reason: it was briefed on the
timing question and answered only the claim as filed, so the existence
question was never asked. **Brief the adversary with the existence question,
not just the design question.**

## Confirm whose retry loop you are tuning before you tune it

**Same six pull requests as above, a second lesson.** All six adjusted this
desk's own settings for how the fill-notification connection retries. None of
them could have worked: the installed broker library retries a failed
connection from inside its OWN internal loop, on a flat 10-millisecond sleep
with no backoff and no attempt limit, and none of this desk's configuration
reaches that loop at all. On 2026-09-15 that produced 32,896 connection
attempts in a single day, the large majority rejected by the broker's own
rate limit. The eventual fix (2026-09-18) did not tune the vendor's loop —
it bounded it from outside, capping how many attempts the process allows per
connection and per day regardless of what the library does internally.

Before adjusting the timing of any retry, back-off or reconnect behaviour,
first establish whose loop is actually running: this desk's own code, or a
third-party library's, called from inside a method this desk does not
control. A fix applied to the wrong owner changes nothing and looks like it
should have worked, which is worse than an obvious no-op.

## Verify the single load-bearing claim of every agent report

**Standing rule, reinforced 2026-09-17.** Roughly one agent report in three
contains something that falls apart under checking. On 2026-09-17 two claims
reached the owner unchecked and both were wrong: "the status board is
effectively full" (it was 30,700 of 100,000 bytes) and "147 websocket
failures today" (about 45 — the 147 counted log lines, several per failure).

Check the one assertion the conclusion rests on, cheaply and adversarially,
before it reaches the owner. If a report says tests pass, check the count. If
it says X is the cause, confirm X produces the symptom. If it says something
never worked, find the counter-example first.

## A gating review must block the merge, not race it

**Standing rule, added 2026-09-20 after PR #560/#561.** A review whose job is
to stop a merge has to be a precondition of that merge, not a parallel
process that might finish first. #560 (the auto-fix loop's unattended
session) was supposed to be blocked pending a qamc-adversary review of its
permission sandbox; instead the review ran alongside CI and this repo's
standing merge-when-green auto-merge, and auto-merge won — #560 was live on
`main` before the review that was meant to gate it had an answer. The review
then found real bypasses in the merged design (see
`docs/INCIDENT_HISTORY.md`, 2026-09-20), and the PR was reverted the same day
before anything was ever installed or run. Nothing here should be read as
walking back merge-when-green for ordinary work — that policy is unconditional
and correct for the common case. It means: when a change is explicitly
marked as needing a gating review before merge, that requirement has to be
wired so the merge cannot complete first, not left as a step that merely
runs in parallel and hopes to win.

## There is no such thing as a quiet market

Across a universe of a hundred-plus names, something is always moving. **"It
was a quiet market" is a cover story for "our filters rejected everything"** —
a statement about this desk, not about the market.

Legitimate zero-trade days exist: a market-wide halt, a latched circuit
breaker, risk rules correctly refusing in a genuine crisis. Every one of them
is a *nameable event*. None of them is "quiet".

So a day with no trades must always name why, per candidate, and "nothing
looked good" is not a reason — it is a hundred separate refusals, each with a
cause. If those causes cannot be produced, the defect is in the recording, not
in the market.

## Adopt the archetype, never the constants that ship with it

Where a rule is needed, start from the recognised version of it — as it is
actually defined in the trading literature and as it is actually implemented
in the standard technical-analysis libraries and platforms. Code is
unambiguous about what a rule computes where prose is not, and a library
*declining* to implement something is information too.

**But separate the shape from the settings.** The archetype is usually sound
and widely agreed; the specific constants attached to it in any given
implementation are usually convention with no derivation behind them.
Adopting the shape of a published rule is legitimate. Adopting its default
numbers because they came in the box is the arbitrary-number failure in
borrowed clothing.

The worked case: the Chandelier trailing exit is textbook and stays. The
"22-day" ATR that travels with it in every charting platform traces to one
site's note that there are 22 trading days in a month — a calendar
coincidence, not a market-structure derivation, and not something its author
ever specified.

No single source is gospel. The literature, the reference implementations,
and what can be read off the instrument itself agreeing is the most
confidence available — and it is enough to act on.

## Check what the platform already solved, before tuning your own workaround

**Owner correction, 2026-09-10.** A sibling mistake to "no arbitrary
numbers," worth naming on its own: sometimes the number is the wrong thing
to be arguing about at all, because the mechanism it is tuning was never
the right mechanism.

`wait_for_order_terminal` asked "has this order filled yet?" once a second
in a loop, for up to a fixed timeout, before deciding to give up on it.
When that timeout turned out to be too short (real trades cancelled while
still working), the instinct — mine, initially — was to find a better
number for it: 15, then 30, then a researched 90. **The owner's question
cut underneath all three:** *"I doubt the majority of people using this API
just set up a simple timer like it's 1992."* He was right, and it took one
search to confirm it, not deep investigation — Alpaca's own documentation
names its real-time `trade_updates` websocket as the recommended way to
know about a fill, specifically instead of polling the REST endpoint. A
fill is reported the instant it happens; there was never a number of
seconds that makes "ask once a second and hope" correct, because the
premise was wrong, not the tuning.

**The general lesson, for this desk and for any future one built the same
way:** before adding a timeout, a retry count, a polling interval, or any
other made-up-feeling number around a THIRD-PARTY API's behavior, check
whether that API's own documentation already describes the intended
mechanism for the problem being solved. A polling loop around a fill,
a price, a fund transfer, an order status — these are common enough
integration problems that a serious broker/data API has almost always
already published the real answer (a websocket stream, a webhook, a
callback), and it is usually a single documentation search away, not a
research project. Reach for a fixed-interval poll only after confirming
the platform genuinely offers nothing better, and say so explicitly in the
code when that is the finding, so the next person does not re-litigate it
from scratch.

**What this does NOT mean:** the timeout does not disappear. It becomes
the ceiling for the case the better mechanism cannot be used at all (the
websocket connection itself failed) — a real, still-necessary number, just
demoted from "the primary detection method" to "the fallback's safety
net." See `src/execution/broker.py::wait_for_order_terminal` for the
shipped shape of this: try the real-time mechanism first, fall back to the
old polling behavior only on a genuine connection failure, never silently
lose the old reliability guarantee while gaining the new speed one.

**AND THEN THE MECHANISM DID NOT WORK — the part that matters most
(2026-09-17).** The websocket described above has never once authenticated
on this host, in any session, since it was built on 2026-09-10. Every fill
the desk has ever confirmed was confirmed by the REST polling path this
section demotes. Two independent blockers were confirmed before the
switch-off, so this was never a tuning problem: the trading process holds
placeholder Alpaca credentials that a local injecting proxy substitutes on
outbound REST only, and the installed `alpaca-py` stream is built on
`websockets.legacy`, which has no proxy support at all; separately, Alpaca
authenticates the stream with an in-band websocket MESSAGE rather than a
handshake header, which a header-injecting gateway cannot supply either.
The socket was therefore switched OFF by configuration
(`execution.fill_stream_enabled`) and the REST path became the mechanism.

**Superseded 2026-09-18:** the real credential was delivered and the socket
was switched back ON in `config/settings.yaml` (commit a938e13ef, #517;
recorded in `config/feature_flags.yaml`). The desk log shows it
authenticating live (2026-09-30: "trade_updates websocket authenticated").
The code default stays off as the fallback if credentials regress.

**The lesson this adds to the one above, and it is the sharper of the two:
the API's documented mechanism is the right answer only once it is
observed working in YOUR deployment.** "The vendor recommends this" is a
reason to build it; it is not evidence that it runs. The failure was
invisible for a week because the fallback was good — the desk kept trading
correctly while logging ~150 auth failures a day that nobody read, and the
only cost was a bounded slice of every fill window spent on a handshake
that could never complete. A degraded path that still works is the hardest
kind of breakage to notice, so the fix shipped an ALERT on fill
confirmation actually degrading (an unconfirmed order outcome, or the
desk's records disagreeing with the broker's), and deliberately NOT on the
socket being off — that is now the intended configuration, and paging on
it would just move the noise into Telegram.

## Execution-environment principle

Paper and live operation share one trading architecture. No agent, portfolio-construction, risk, position-management, reflection or Dashboard semantics should become easier, looser, or materially different merely because the current broker account is Paper.

Environment-specific differences belong at the broker/configuration boundary: credentials, endpoint selection, account identity and genuine execution-mechanics differences such as simulated versus real fills/slippage. Live activation, if later authorized, should therefore be a focused operational/risk authorization change rather than a rewrite of the trading system.

## Terminology

- **QAMC / Mission Control** refers to the whole product/system.
- **Dashboard** refers specifically to the browser/iPad read-side UI and the parallel frontend/UX workstream.
- **Core recovery** refers to trading/backend deployment and natural-validation work.

This distinction is intentional so future sessions do not mistake the Dashboard for the whole Mission Control project.

## Dashboard product direction

The QAMC Dashboard is intended to feel like a **real trading cockpit**, not a vertically stacked database/log viewer.

`docs/visual/MISSION_CONTROL_VISION_BOARD.png` is the durable product reference for layout, information hierarchy and donor direction. It is not merely historical inspiration. Product work should actively preserve the strongest ideas already captured there while correcting any semantic or data-truth problems discovered later.

The cockpit should be dense but legible, desktop-first and strong on iPad, with:
- a compact account/status strip;
- watchlist/candidate context;
- chart-led market context where authoritative data supports it;
- a visually prominent **Specialists → Portfolio Manager → AI Risk → deterministic gate → execution** chain;
- positions/orders/trades as supporting state rather than the whole product;
- structured journal, investigation and learning views that explain decisions and missed opportunities.

### Private Research Desk principle

QAMC is built for **one operator**, not customers. The Research/Intelligence experience should therefore optimize for usefulness and willingness to read it every day, not corporate polish.

The voice should feel like a sharp internal trading desk: candid, compact, substantive, occasionally dry or irreverent when the evidence earns it. Avoid generic AI prose, filler, repeated conclusions, forced jokes, fake quotes and performative cleverness. **Say everything useful. Nothing merely decorative.**

Brevity must not become thinness. Each useful research item should give enough evidence, interpretation and consequence to answer: what happened, what changed, why it matters now, what conflicts with it, and what the PM/Risk implication is.

Use visual structure to reduce reading effort where it helps: signal agreement/conflict, what changed, why now, evidence chips, important tension, compact chart context, and clearly separated Read / PM / Risk consequences. Do not mechanically put every device on every card.

The default composition should be deliberately designed, visually balanced and easy to scan. Important stories may dominate while supporting material recedes. Docking/resizing is operator personalization on top of a strong default layout, not a substitute for design.

Raw JSON/logs remain secondary evidence drill-down. They are never the primary reading experience.

### Design-donor decisions to preserve

The vision board intentionally uses donors for proven interaction/design ideas while keeping `quant-agent` as the authoritative trading engine.

**OpenTradex — shell/layout donor**
- Keep/adapt: resizable panes, terminal-style layout, compact top account bar, reusable public UI components, and the run-control visual concept where it remains read-only/safe.
- Do not adopt: its trading engine/gateway, command-chat control path, or internal data models.

**Oralexa — agent/decision visualization donor**
- Keep/adapt as first-class QAMC concepts: **agent cards**, disagreement/signal-fusion presentation (the dissent the head trader answers, never a free AI debate), Portfolio Manager decision cards, scoreboards where backed by real metrics, and bias/self-correction presentation for the Learning Center.
- The agent cards should make each specialist's stance, important evidence, disagreements and confidence/calibration visible without requiring raw-log reading.
- The chain should visually show how specialist views combine into PM intent, how Risk modifies/rejects it, what deterministic Python does next, and what actually reaches the broker.
- Do not adopt Oralexa mock-data layers or its trading logic.

**TradingView Lightweight Charts — charting donor**
- Use/adapt for chart-led context: candlesticks, indicators, volume, trade markers and streaming updates where the existing QAMC data contracts can support them truthfully.
- Charts are context for the decision process, not a replacement trading engine or source of fabricated signals.

### Agent-card principle

Agent cards are a core interaction pattern, not decorative status tiles.

A useful card should answer, at a glance:
- what this agent currently believes;
- the strongest evidence behind the view;
- what would invalidate/change the view where available;
- whether it agrees or conflicts with other agents;
- any genuine confidence/calibration signal that exists in authoritative data.

Do not invent pseudo-confidence percentages or arbitrary gauges merely to make a card look complete. If confidence is not authoritative, show the actual qualitative state/evidence rather than manufacturing precision.

### Decision-chain principle

The operator should be able to follow one compact graphical story:

**candidate/opportunity → specialist cards → disagreements/signal fusion → PM proposal → Risk approval/modification/rejection → deterministic gate → execution → position/exit result.**

The cockpit should emphasize changes and deltas: what PM proposed, what Risk changed, what deterministic Python blocked, and what actually executed.

### Journal Day page

The structured Journal Day mockup on the vision board is an accepted product direction and should not be replaced by an endless chronological event dump.

A day page should compress the session into a useful operator narrative such as:
1. **Market Thesis**
2. **Watchlist / Candidates**
3. **Agent Analysis** — compact specialist cards/table
4. **Disagreements**
5. **Portfolio Manager Proposal**
6. **Risk Review**
7. **Proposed → Executed Difference**
8. **Trades**
9. **Daily Result**
10. **Lesson Learned**
11. **Tomorrow / what to monitor**

The detailed event/log stream can remain available for forensics, but it is not the primary journal experience.

### Required screen states

The Dashboard should deliberately handle the major workflow states shown in the vision board rather than treating them as incidental variations:
- normal/no-trade;
- proposed trade;
- rejected/modified trade;
- executed trade;
- learning/reflection.

Each state must remain truthful when data is absent, partial or degraded; an empty or no-trade state should look intentional rather than broken.

### UX principles

- **Truth before decoration** — candidate/run attribution, capital semantics, execution state and agent provenance must be correct.
- **Clarity first** — the important trading state is immediately visible.
- **Transparency always** — reasoning, disagreement, vetoes and deterministic blocks are inspectable.
- **Explanation before action** — especially for no-trade and rejected-trade states.
- **Graphical synthesis before text dumping** — use cards, chains, charts, funnels and deltas to summarize; preserve drill-down for detail.
- **Human in control** — without making the Dashboard part of the trading-critical path.
- **Maximum useful reuse, minimum custom infrastructure** — adapt the donor ideas and existing product before inventing new durable systems.
- **Desktop + iPad first** — phone is secondary to a strong cockpit experience on the operator's primary surfaces.

### Professional visual-composition standard

Functional correctness is necessary but not sufficient. The Dashboard must also satisfy basic professional interface-design fundamentals in the rendered product.

- **Coherent hierarchy:** typography, scale, weight and placement must make the most important trading state visually dominant. Brand/header, primary metrics, section titles, labels, body text and metadata should form a deliberate readable type hierarchy rather than unrelated font sizes.
- **Readable typography:** routine labels and explanatory text must remain comfortably legible on desktop and iPad; small text must not be used merely to make dense panels fit.
- **Proportion:** a component must visually justify the space allocated to it. Important graphics such as risk deployment, decision flow and agent analysis should scale to their containers rather than appearing as tiny islands inside large empty rectangles.
- **Intentional whitespace:** empty space should create hierarchy and breathing room, not dominate the page because fixed containers remain large when content is absent.
- **Adaptive sparse states:** no-candidate, no-position and low-information states should collapse, rebalance or repurpose space so the cockpit remains composed and useful instead of becoming a sea of empty panels.
- **Grid and rhythm:** columns, card edges, baselines, gaps and repeated structures should align to a coherent layout/spacing system. Unequal proportions should be intentional and reflect information importance.
- **Information density:** the cockpit should use its canvas efficiently. Avoid both extremes: cramped micro-text and oversized containers with very little content.
- **Designed surfaces, not generic breakpoints:** desktop and iPad should each look deliberately composed, not simply like the same grid squeezed or stretched.
- **Visual acceptance is empirical:** implementation is not complete merely because it builds, tests pass or nothing overlaps. Rendered screenshots must be inspected at target desktop and iPad sizes against the vision board and professional trading-dashboard standards; obvious hierarchy, typography, proportion, balance or empty-state defects remain product bugs.

The target is a credible professional trading cockpit, not a technically correct dashboard shell containing miniature widgets.

The visual reference is directional, not blanket feature authorization. Mockup concepts that conflict with current safety boundaries — including direct trade controls or other write paths — remain unimplemented unless separately authorized. Exception, owner authorisation 2026-10-09: the owner authorised Stop / Freeze / Start dashboard buttons. Freeze (ruled ~23:20 UTC that day) keeps the desk running and protecting what it holds — stops, trails and exits all work — but opens nothing new; it is a flag the desk reads at its single broker door, never a call, and is also the future Sentinel's inbound seam (docs/FUTURE.md). Its rule is built into the owner flag gate (src/execution/owner_flags_gate.py); the Stop and Start buttons and their API are later changes. Do not fabricate unsupported data merely to match a mockup.

## MVP lifecycle principle

QAMC should reach a safe, observable deployed baseline and then **start real-market validation in Alpaca Paper promptly**. Paper evidence is not the reward after polish; it is the evidence needed to decide what should be improved next and whether the system could eventually justify live-capital authorization.

The expected sequence is:

**functional foundation → integrated verification → VPS deployment → runtime commissioning → Paper validation → observe/evaluate natural sessions → iterative agent/code/dashboard improvement → separate live-capital authorization if earned**.

Before Paper validation starts, the product needs enough observability to understand account state, decisions, execution, health and history. It does **not** need every desirable reasoning refinement, benchmark, chart or UX improvement.

After validation starts, engineering should use observed trading behaviour and operator experience to prioritize work: weak evidence, poor decisions, excessive vetoes, execution problems, missing telemetry, confusing Dashboard views, missed opportunities in either direction, model cost/latency and measurable out-of-sample performance.

Now that the validation run is active, visual/product convergence is valid engineering work when the running Dashboard materially fails the intended operator experience. Functional correctness alone is not sufficient acceptance for a Dashboard redesign.

## Hard outcome constraints

These are not implementation suggestions; they define the currently authorized safe system:
- Alpaca **Paper is the only currently authorized execution environment** until a separate future live-capital authorization;
- the trading architecture must remain environment-neutral: no paper-only shortcuts or separate paper-specific decision/risk path;
- `yebof/quant-agent` remains the authoritative trading engine unless the operator explicitly changes that project premise;
- deterministic Python and broker protections remain final safety/execution authority and fail closed;
- Dashboard/read-side failure must not stop trading or weaken broker protection;
- UI/search/journal state must not become a second authoritative trading-memory system;
- no secrets or fake production trading state exposed to the UI;
- directional capability must remain inside the supported instrument/risk contracts and must not bypass deterministic safety;
- keep the system small enough to understand, operate and evaluate rather than turning it into a bespoke platform.

A submit whose reply is lost (a timeout or dropped connection, no HTTP status) proves nothing either way, so the desk asks the broker for the order by its own idempotency key; any reply the broker did return, and every other error, propagates unchanged. Found: it is placed, and the order takes the normal accepted path (an exit keeps its stops cleared and is finalized on its fill). Not found or the read failed: the outcome is UNKNOWN, never "not placed" — an exit puts protection back only over what is held now and alerts the owner, and an entry stays pending with its key stored on the row, so the next session's orphan sweep finds it by that key on either side (a short entry is a sell order).

## Every money number is expressed against the stock's own movement

**Owner ruling, 2026-10-04:** *"The 2.5 is not a made up number, it is
specific to its own stock move. It's the way we should be handling
everything."*

A value scaled to the instrument is NOT an arbitrary constant, because the
instrument supplies the scale. 2.5 times a stock's own typical daily move is
automatically wide on a volatile name and tight on a quiet one, so there is no
single "right" number to go and measure, and six attempts to measure one all
failed for that reason.

The converse is the design rule: a flat percentage is the same mistake as a
flat dollar amount. It means one thing on a sleepy name and something else on
a jumpy one. When a flat constant governs money, the fix is usually to
re-express it as a multiple of that stock's own behaviour rather than to hunt
for a better constant.

Account-level and book-level ceilings are the exception. There is no single
instrument to scale them to, so they remain owner rulings and are filed as
such, not as unsourced numbers.

## The protective stop width is settled at 2.5 ATR

**Owner ruling, 2026-10-04:** the answer is 2.5 times the stock's own typical
daily move. It reopens only on evidence from live paper trading that it is
doing real damage - not on a backtest, not on a re-derivation, and not on a
percentile of past trades.

Where a support or resistance level backs the stop, that level is honoured
however tight, down to the absolute floor of one times the daily move. The
2.5 applies only when nothing computed backs the stop.

Do not route this to the owner again. It has been reopened six times.

**Owner ruling, 2026-10-09:** a stop is never moved closer than one normal day's move (the stock's own 14-day ATR), so a trailing stop's noise band is exactly 1.0 ATR.

## A short is treated exactly like a long

**Owner ruling, 2026-10-04:** *"A short is not riskier than long... ATR
measurements should be the same. Support and resistance measurements should be
the same. A short should be treated the same as a long, no different math no
different behavior."*

The 1.5 overnight gap haircut that sized every short smaller is DELETED, not
set to a neutral value - a neutral dial is something a later change re-tunes.
Sizing, stop distance and level construction run byte-identical arithmetic in
both directions.

Two supports for this, beyond the ruling itself. Measured over 17,252 daily
bars across 38 symbols: overnight up-gaps are not systematically worse than
down-gaps, with only 22 of 38 symbols showing a fatter up-tail, so the premise
the haircut rested on is not in the data. And the unlimited-loss argument
assumes a name that can run away overnight, which the universe filter exists
to exclude.

The borrow gate stays: the desk still refuses to short what the broker will
not lend. That is a gate, not arithmetic.

An overnight gap is not a risk appetite question and must never be put to the
owner as one. The market is closed, nothing can be done, and everyone
understands that.

## The stock swap must beat the score's own noise

Owner decision 2026-10-15: every holding must keep earning its place, so the stock-swap rule stays (switched off); its made-up 25% brake is replaced by the desk's own measured score noise on unchanged names, no swap while that is unmeasured, and a trading-cost brake comes after the paper reset.

## Design freedom

Everything else is challengeable during discovery and post-validation iteration.

Existing architecture, donor choices, stage boundaries, data presentation, component structure, sequencing and implementation techniques are prior proposals—not instructions to preserve merely because they already exist in Git. The explicit donor/product principles above, however, represent desired product outcomes and should not be silently discarded merely because later work focused on semantic correctness.

Claude Code is expected to inspect the actual repository and challenge whether implementation choices still provide the simplest, safest and most effective path to the outcome. Material changes to accepted safety/product boundaries still require reconciliation and approval before implementation.
