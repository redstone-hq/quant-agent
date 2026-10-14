# Work that can ONLY be settled while the market is open

Filed 2026-10-04 at the owner's request. Several findings stalled tonight not
because they were hard but because the exchange was shut, and they were
scattered across the board, the parked-defect list and memory. This is the
one place they live.

**The distinction that matters.** Two different things get called "needs a
trading day":

- **SANDBOX-ONLY** — settled on the disposable paper account during regular
  hours. These do NOT need the desk switched on and do NOT need the owner's
  permission to trade. They can be done at any open, including an open where
  the desk stays off.
- **NEEDS THE DESK RUNNING** — only the desk's own live behaviour can answer
  it. These wait on the owner's decision to turn it on.

Keep that split. Treating a sandbox question as "blocked on the desk" is how
three of these sat unanswered for weeks.

Rules that still apply at the open: the sandbox is account-disposable and may
be freely broken, but the desk's own paper account must never be touched;
leave the sandbox with zero open orders and zero positions; never commit an
account id, key or token — this repo is public.

---

## SANDBOX-ONLY — do these at the next regular open

### 1. Does shrinking a stop's quantity release the shares for a sell? (TOP)
The load-bearing unknown behind a confirmed unprotected window. Every partial
sell cancels ALL resting protection, sells, then re-places a stop on the
remainder — so the shares being KEPT have no stop for that whole window, and
the code's own docstring says the residual "rides naked". The cancel exists
for a real reason: the broker reserves the shares behind a resting stop and
rejects a sell of them.

If an in-place amend that REDUCES the stop's quantity frees the difference to
sell, the window closes for the entire class and no cancel is needed.

Measured 2026-10-04 and NOT answered: every amend was refused
`42210000 "cannot replace order in accepted status"` — including a price-only
amend on the same order — because the market was shut.

DONE WHEN:
- [ ] A reduced-quantity amend on a resting stop is attempted at the open and
      the broker's answer recorded verbatim.
- [ ] If accepted: a sell of the released shares is attempted and the result
      recorded, settling whether shrink-then-sell works end to end.

### 2. Confirm the in-place price amend still works during open hours
The desk's whole stop-repair design rests on the broker amending in place.
That was measured once, on 2026-09-30. The sandbox's order history still
carries one `replaced` order corroborating it, but it has not been re-run.
A design resting on a single old measurement is worth re-confirming cheaply.

DONE WHEN:
- [ ] A price-only amend on a plain stop-market order succeeds at the open.

### 3. Out-of-hours amend fallback — measure the blast radius
Discovered 2026-10-04: outside regular hours the atomic amend CANNOT RUN AT
ALL, so every overnight and pre-open stop change falls back to
cancel-and-resubmit — the exact mechanism recorded as the root cause of this
desk's stop failures. How often the desk does this, and how long each window
lasts, was unmeasured: the count is now measured (first box below); the window length still is.

DONE WHEN:
- [x] The number of stop changes the desk attempts outside regular hours is
      counted from its own records.
      EVIDENCE [measured 2026-10-14, live desk records]: 0 stop changes outside market hours since 12 Oct, 3 inside.
- [ ] The cancel-to-replace gap is timed against the sandbox at a real open.

### 4. Partial fills driven by the live stream
The hermetic test covers partial fills by faking fill quantities. It does not
cover fills arriving on the broker's live stream, which is how they actually
arrive. Shorts are also uncovered.

DONE WHEN:
- [ ] A part-filled order on the sandbox, observed through the live stream,
      ends with exactly one stop sized to the shares actually held.
- [ ] The same proven for a short position.

---

## NEEDS THE DESK RUNNING — these wait on the owner

These were each judged to need a real session; a replay cannot close them,
because a replay stamps its own run id over answers recorded earlier and is
therefore not an observation of the desk.

- **Item 78** — delete the blank-falsifier isolate once the seats agree.
- **Item 187** — the chronic economic-data fetch deadline.
- **Item 201** — the remainder of the cancel-and-resubmit stop path.
- **Item 219** — the pruning pass reports nowhere the owner looks.
- **Item 226** — a payment refusal retried as if it were an outage.
- **Item 227** — a seat's read carried no record of when, or in which run.

Two more need production data rather than a live session, so they are NOT
blocked on the desk being on — only a session with production read access can
settle them:

- **Item 218** — whether any NULL evidence row is a FIRST entry (a genuine
  loss) rather than an add to a name whose row predates the column.
- **Item 90** — the settling data behind the unsourced trade-governing
  numbers.

---

## PLAN — pointing a FULL daily session at the disposable paper account

Investigation only, 2026-10-04. Nothing was run. Evidence is from the repo at
origin/main; not exercised against the broker.

**1. Credentials or code?** Credentials, plus a separate checkout. No code
names the account: the account id appears nowhere in the repo; the account is
chosen only by the key pair `ALPACA_API_KEY` / `ALPACA_SECRET_KEY` (see
`api_keys` in `config/settings.yaml`). `alpaca.base_url` is already the paper
host and `AlpacaConfig` refuses `paper: false`, so a sandbox key pair on the
same host needs no config edit. The one trap: the key pair, the Telegram
token/chat and the LLM keys all arrive from the same environment or systemd
credentials, so the sandbox keys must be supplied to a process that does NOT
inherit the desk's unit.

**2. Where writes land.** Into whichever checkout the process runs from, NOT
into a database named by config alone. `storage.db_path` is relative
(`data/quant_agent.db`), and the notifier, both watchdogs, the refusal-signature
reader and the route journal each compute their own path from their source
file's location (`Path(__file__)...`), so a second checkout with its own empty
`data/` isolates all of them. Pointing the production checkout at other keys
would NOT isolate: it would write sandbox trades into the production database.
Also under `data/`: news, macro, checkpoints, evolution, and the kill-switch
file. `QUANT_AGENT_DB_PATH` redirects only the route journal. Never run it with
`/home/qamc` as the working directory. The rehearsal rig (`ops/rehearsal`) is
frozen and walls off the network, so it cannot be reused for a live-paper run.

**3. External cost.** One session is capped by `llm_cost_circuit`: 0.90 USD per
session, 2.75 USD per day, 40 calls per session (`config/settings.yaml`). The
cap counts spend in the sandbox's own database, so a fresh checkout starts the
day at zero and the daily cap does NOT see production spend that day. The
spend lands on the same provider accounts as production (same API keys).
Production PM seat is the dominant cost (memory: ~91 percent). Kill switch:
`touch data/KILL_SWITCH` in the sandbox checkout halts every order there; it
does not stop LLM spend.

**4. Evidence supplied.** Of the 8 items in the two lists above:
- 4 sandbox-only items (1 to 4) need no session at all; a session adds nothing.
- 6 "needs the desk running" items (78, 187, 201, 219, 226, 227): a session
  could supply evidence for them only if the triggering condition occurs
  (a payment refusal, a cancel-and-resubmit stop, a pruning pass); [estimate]
  likely 3 to 4 of 6, unverified.
- 2 production-data items (218, 90) stay blocked: an empty sandbox database
  has no history to settle them.
The "about 17 blocked" figure could not be reproduced from this file; the
board was not recounted here.

**5. Not recoverable by resetting the paper account.**
- Telegram: a sandbox session with the desk's bot token messages the owner
  and may be mistaken for real; leave those variables unset.
- LLM spend: real money, up to the caps above, per session.
- Running from the production checkout or `/home/qamc`: contaminates the
  production database and its trade ledger.
- Same key pair as the desk's own paper account: the hands-off rule above is
  broken and that account's orders are cancelled.
- Provider data and news caches written to the wrong `data/` are not undone
  by an account reset.
- A session left running past the open with resting orders.

---

## BUILT AND LIVE (since 2026-10-14), NOT YET SEEN ON A REAL EARLY CLOSE — the session windows follow the calendar

This belongs here because the only honest end-to-end proof is a real early
close. The next ones on the US equities calendar are Fri 27 Nov 2026 and
Thu 24 Dec 2026 (both 13:00 ET); Thu 26 Nov and Fri 25 Dec are full holidays.

**Live since 2026-10-14.** PR #1774 was deployed 20:08 UTC. Checked on the box with the desk's keys [measured 2026-10-14]: 27 Nov reads close 12:30-13:00 ET (750-780 minutes), a Saturday reads closed, a normal day reads 09:30-16:00.

**What trunk does now.** The schedule has one copy: `PHASE_OFFSETS` in
`src/trading_calendar.py` states each phase as minutes from the day's real open
or close, and the old fixed table (`SESSION_WINDOWS`) is derived from it at
09:30/16:00. `scripts/run_if_et_window.sh` no longer holds any minutes. Each
tick it runs `python -m src.session_calendar <mode> <ET date>`, which prints the
phase's window for that date (or `closed`) from the broker's exchange calendar,
and fires the phase only inside it. The first tick of a day asks the broker and
saves the answer in a small per-date file in the wrapper's last-run directory;
later ticks of every mode read the file.

- On a normal day nothing changes: the six windows are the same minutes as before.
- On Fri 27 Nov 2026 (09:30-13:00): midday 10:00-11:30, close 12:30-13:00,
  intra_check and intra_safety 09:30-13:00, evening 17:00-19:00. Midday overlaps
  morning that day; the existing cross-mode lock makes it retry on the next tick
  inside its 90-minute window.
- On a full holiday every phase reads `closed` and the wrapper exits without
  running anything.
- If the broker calendar cannot be read and today's answer is not saved yet, the
  wrapper uses the normal-day windows and writes one journal line saying so. That
  is the old behaviour, wrong only on a half day with the broker down; the desk
  trades paper money and no refuse-to-act failure mode was added for it.
- If the command itself is missing or crashes, the tick fails loudly; the shell
  keeps no fallback numbers.

**Still on the fixed normal-day table, on purpose.** The silence and coverage
watchdogs and the paid-session budget use the table as an upper bound of
expected activity, and on an early close every real phase ends earlier than its
fixed window, so they err generous. The paid intraday tick already stops at the
real close inside Python. Not yet switched, and still open: the three
exit-adjacent deadline readers (`src/protection/put_off_sales.py`,
`src/price_feed_preflight.py`, `src/intraday/gating.py`), whose window end is
masked by the 20-minute hard kill except in a window's last 20 minutes; the
display helpers (`in_session_window`, the trader feed's midday-collision tick);
and the unused APScheduler path (`src/scheduler.py`).

**Still to prove live:** the first real early close. Check that day's journal for
the saved session file, midday firing at 10:00 and close at 12:30, and no phase
launching after 13:00.

**Do not introduce a hardcoded holiday list.** That is a time bomb wearing a
different hat; the broker calendar is the only source.


### HOW TO RUN IT — the single command, with the hazards blocked

The plan above is now buildable. Each of its five non-recoverable hazards is a
refusal in `src/sandbox_preflight.py`, not a warning in prose:

- Owner-facing Telegram: `scripts/sandbox_session.sh` unsets the bot token and
  chat id both before and after sourcing the sandbox env file and forces
  `TELEGRAM_DISABLED=1`; the preflight refuses to start if either credential is
  present, or if the mute uses a spelling the notifier ignores. The session is
  incapable of messaging the owner, not merely muted.
- Production database: the preflight refuses to run from inside the production
  checkout (`/home/qamc` by default, extendable via `QAMC_PRODUCTION_CHECKOUT`),
  refuses a `data/` that resolves outside its own checkout (so a symlink into
  production is caught), and refuses a database that no sandbox run created.
- The desk's own paper keys: the sandbox key is pinned by an allow-list of one —
  `QAMC_SANDBOX_ALPACA_KEY_SHA256` must be the SHA-256 of `ALPACA_API_KEY`. Any
  other key is refused, so neither the production key nor the desk's paper key
  can be used by accident, and no key, account id or digest of a real key is
  committed.
- Live trading: the existing paper-only lock in `AlpacaConfig` is re-asserted,
  never bypassed.
- Real LLM spend: unchanged and still real, capped by `llm_cost_circuit` as
  described above. The preflight does not and cannot make it free.

Run it from a SEPARATE checkout with its own empty `data/`:

    cp config/sandbox.env.example .env.sandbox   # fill in, never commit
    scripts/sandbox_session.sh morning

Proof lives in `tests/test_sandbox_preflight.py`: every refusal has a paired
positive case, so deleting a check reds the suite.

**Isolation is measured, not asserted.** `scripts/check_sandbox_isolation.py`
copies the tree's code to a scratch directory, gives the child a throwaway home
and working directory, runs the desk's writers under an audit hook that records
every write-capable event, and FAILS if any write lands outside the copy or the
scratch home. The modules it loads are derived by walking the tree (the root
`main.py` included); no list of writers and no path is stored. It also takes
`--guard-tree` directories that must hash identically before and after.
`tests/test_sandbox_isolation.py` proves it passes on the real tree and fails
when a writer is made to escape. Measured 2026-10-05: 486 modules (95 with a
write-capable call) loaded, 13 writes inside the checkout, 2 inside the scratch
home, none elsewhere; the production checkout's code and data hashed identically
before and after (read-only, as the `qamc` user). Writers needed no change.
Two things the measurement found: the session lock and last-run markers sit
under the HOME directory, not the checkout, so the preflight now also refuses a
home directory inside production; and `log_health` and the API server name the
production checkout as a read-only anchor (no write was observed).
