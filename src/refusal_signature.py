"""Escalate when the desk refuses every idea for the SAME reason, session
after session — the shape of a jammed gate, not of a quiet market.

THE GAP THIS CLOSES (docs/WORK.md item 59)
------------------------------------------------------------------------
Every individual empty day is already reported honestly and distinctly:
`no_trades`, `no_orders` and `buys_unfunded` are three separate status
words for three different kinds of nothing (`src/pipeline.py`,
`src/notifier.py`), and after 2026-09-13 every single-day path by which the
desk can produce nothing has an alarm behind it (docs/INCIDENT_HISTORY.md,
"can the desk still die quietly?"). Nothing watched the SEQUENCE. A gate
defect that refused every candidate would tell the truth each morning,
never raise its voice, and stay invisible for as long as it lasted — and
the measured record shows 6 zero-fill sessions out of 11 in the window the
census examined, so a run of empty days is the ORDINARY state here, not an
exotic one.

WHY THIS INTRODUCES NO DAY COUNT
------------------------------------------------------------------------
The tempting alarm — "N identical empty days" — was ruled out on the spot:
there is no honest way to read N off anything. N would be invented, and an
invented number is exactly what this desk refuses to ship
(docs/WORK.md, the no-arbitrary-numbers principle).

A jam and a quiet market are separable by SHAPE instead. A quiet market
kills different candidates for different reasons: this one has no usable
structure, that one's reward:risk is thin, another is too young to
measure. A jammed gate kills EVERY candidate with the SAME reason, and
goes on doing it while the candidates underneath it change. So the trigger
is stated entirely in terms the evidence answers by itself:

    every candidate, in every consecutive no-entry session back to the
    last session that ended any other way, was refused by ONE reason —
    the same one — while the set of candidates was NOT the same set each
    time.

There is no threshold in that sentence. The run of sessions is bounded by
the data (it ends at the last session that did something else). "The
candidate set was not the same set each time" is what forces more than one
session to be involved — you cannot observe "the reason did not vary" from
a single observation, and you cannot observe "the input varied" from
identical inputs. Nothing here is tuned, and nothing was fitted to the
desk's own history.

WHAT COUNTS AS "THE REASON"
------------------------------------------------------------------------
The durable `pipeline_event` rows `src/pipeline_stages.py` writes for every
candidate (read today by `scripts/blocked_proposals_census.py` and by the
evening blocked-proposals digest in `src/pipeline.py`). Each carries
`stage`, `outcome`, `reason`, and — for the two paths that record a refusal
AS DATA rather than recovering it from a log line — a named `refusal` or
`fault` code beside a human detail string.

Two normalisations make those keys comparable ACROSS symbols, and both are
mechanical, not judgemental:

  * the candidate's own ticker is removed from the text (several reasons
    are the constructor's own log line, which opens with the symbol), and
  * every numeric literal is replaced by a placeholder, so "reward:risk
    1.12 below the floor" and "reward:risk 1.31 below the floor" are one
    reason, which they are.

Both can only ever MERGE two reasons that are really the same rule. They
can never split one rule into two — so the failure mode of this normaliser
is a missed alarm, never a false one. Where a reason mentions a SECOND
symbol (sector crowding names the position it collides with), the keys stay
distinct and this check stays quiet. That is a known and deliberate
false-negative, recorded here rather than papered over.

The key is taken from the LAST event recorded for that candidate in that
run, not from an allow-list of "refusal" outcomes. An allow-list rots the
moment a new gate is added; "what killed it last" cannot.

WHAT IS NOT A REFUSAL (2026-09-22, the false alert this closes)
------------------------------------------------------------------------
"What killed it last" is only true of an event that KILLED it. Two kinds of
last event kill nothing, and reading either as a refusal is how the owner
came to be told that the desk had refused every idea for the same reason
across six intraday ticks that never ran a gate at all:

  * a last event whose outcome is in `NOT_A_REFUSAL_OUTCOMES` did not turn
    an idea down. Either it records the candidate ARRIVING somewhere with
    nothing yet ruled (`UNDECIDED_OUTCOMES` — the desk found it, a
    specialist looked at it, a seat nominated it), or something DID rule and
    ruled that no new entry was right (`NO_ENTRY_DECIDED_OUTCOMES` — the
    portfolio manager holding a position it still rates, a nomination that
    duplicates an analysis this run already has, an exit). Such a candidate
    is dropped from the run's evidence; a run left with none of them is
    absent from the session list entirely. The second of those two is the
    fully-invested book, and it is not a defect: a decision to hold is the
    gate working.
  * a run the desk's OWN report row says stopped before the decision stage
    (`NON_DECIDING_STATUSES`: the cost circuit suspending paid analysis, the
    evidence gate skipping, a crash on the way in) is not evidence about the
    gate in either direction. It is stepped over: it neither joins a streak
    nor breaks one, because a jam does not clear just because one tick was
    switched off, and a switched-off tick is not proof of a jam either.
  * a last event of `specialist|failed` (`NOT_A_REFUSAL_STAGE_OUTCOMES`,
    2026-09-24) is a seat that could not ANALYZE the candidate — a bar fetch
    exception, no bars returned, a batch response left unresolved — not a
    seat that formed an opinion and turned it down. This one is scoped by
    STAGE as well as outcome, because "failed" is a real refusal at other
    stages (`risk|failed`, `funding|failed`, `portfolio_manager|failed`) and
    excluding it everywhere would hide a jam sitting behind one of those.
    Unlike the run-level rule just above, this fires even when the run's OWN
    status is a deciding one (`no_trades`, `no_orders`): a data outage can
    starve every specialist while the run still finishes as a normal, decided
    tick, and it is the candidate-level event, not the run's status word,
    that was reading as a refusal.

The three are separate rules on purpose, and none subsumes another. The
17:15–19:45 ticks of 2026-09-22 are caught by the first (their only events
were `opportunity|discovered`); the 15:15 tick the same afternoon was, at the
time this docstring was first written, caught only by the second, because its
overall run status happened to land on a non-deciding word — but a
`specialist|failed` streak sitting inside runs that finish `no_trades` or
`no_orders` (a deciding status, by design) passed both of the first two rules
and still misread as a jam. The third rule closes that gap directly at the
event, rather than depending on the run's status word to catch it.

CADENCE, AND WHY IT CANNOT PAGE A PAUSED DESK
------------------------------------------------------------------------
At most one alert per trading day while the condition holds — docs/WORK.md
item 41's existing ruling, already used by `src/coverage_watchdog.py`. No
new cadence is invented here.

And the streak must be CURRENT: the newest session in it has to fall on the
most recent completed trading day, judged by the same
`trading_day.most_recent_trading_day` the stop watchdog uses. The
trading timers were paused on 2026-09-03. A paused desk runs no sessions,
so its newest session is not on the most recent trading day, so this check
is silent — a deliberately paused desk is not a defect. It re-arms itself
with no flag to flip the moment sessions resume, because that is the same
moment the newest session becomes current again.

WHERE IT RUNS
------------------------------------------------------------------------
From `scripts/alert_heartbeat.py`, beside the coverage watchdog: the one
unit that fires daily whether or not the trading timers are enabled. It is
a READER — one read-only SQLite connection, one owner alert, no writes to
the trading database and no broker orders of any kind.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import tempfile
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from src.trading_day import most_recent_trading_day
from src.trading_calendar import ET, utc_now
from src.data_paths import alerting_dir, db_path

logger = logging.getLogger(__name__)

#: Same database every session writes its evidence to.
DB_PATH = db_path()

#: On-box record, gitignored like its siblings under data/alerting/.
STATE_PATH = alerting_dir() / "refusal_signature.json"

#: Actions that mean the session actually entered something. A SELL is an
#: exit and does not make a day non-empty for this purpose — the item is
#: about the desk never taking a NEW idea.
ENTRY_ACTIONS = ("BUY", "SHORT")

#: `outcome` values that mean the candidate SURVIVED that stage rather than
#: being killed by it. A session where any candidate's terminal event is one
#: of these did not refuse every idea, whatever the trades table says (the
#: cash-sweep vehicle fills without ever being a BUY/SHORT row — measured on
#: the 2026-09-02 close run, the only real session whose evidence this check
#: can still read). Kept deliberately small and positive: an outcome nobody
#: has thought of yet reads as a refusal, which can only ever LENGTHEN a
#: streak that still has to be monomorphic over changing candidates before
#: anything is sent.
SURVIVED_OUTCOMES = frozenset(
    {
        "submitted",
        "filled",
        "allowed",
        "approved",
        "placed",
        "buy_submitted",
        "funded",
        # 2026-09-23 audit additions, both of them "an entry went ahead" words
        # that were reading as refusals because they were simply never listed:
        #   risk|modified        — the risk manager RESIZED the entry and let it
        #                          through, which is `approved` with a haircut.
        #   execution|safety_net — the catch-up reprice inside the entry ceiling,
        #                          written only on the path where the order then
        #                          proceeds.
        "modified",
        "safety_net",
    }
)

#: `outcome` values that record a candidate ARRIVING somewhere rather than a
#: stage ruling on it — a beginning, not an end. A run that stops before the
#: next stage leaves one of these as the candidate's last event, and reading
#: it as a terminal outcome is how the owner was told on 2026-09-22 that the
#: desk had "refused" six intraday ticks' worth of movers whose only recorded
#: event was that the desk had FOUND them (`opportunity|discovered` — SNDK
#: moved 6.69% past the 3% discovery threshold and nothing then ran).
#:
#: This is a SET, not a special case for one string, because `opportunity`
#: is not the only stage with that character and `opportunity` is not
#: uniformly of it: the same stage also writes `already_covered`, which IS a
#: terminal disposition ("we hold it already"), while `specialist|evaluated`
#: — a different stage entirely — is just as much a beginning as
#: `discovered` is. So the property belongs to the OUTCOME word, which is
#: what says whether anything ruled, and not to the stage it was written at.
#:
#: A candidate whose last event is one of these has no terminal outcome at
#: all, and is dropped from that run's evidence. It is not counted as
#: refused and it is not counted as survived; the desk simply never got as
#: far as having an opinion about it.
UNDECIDED_OUTCOMES = frozenset(
    {
        "discovered",  # opportunity — a mover or prefilter hit was noticed
        "evaluated",  # specialist  — a seat analysed the chart
        "nominated",  # opportunity — a research seat put the name forward
        "admitted",  # opportunity — smart-money/Form 4 widened eligibility
        "proposed",  # portfolio_manager — a target was put up
        "attempted",  # funding — a cash sweep is in flight
        "not_decided",  # evidence_gate — the gate says so in the word itself
        "protective_sell_cancelled",  # scale_in — a bookkeeping step mid-add
        # Board item 193 — the measured cancel-to-rearm window. Same character as
        # `protective_sell_cancelled`: it records how long the held position was
        # naked, it rules on nothing, and a candidate whose last event is one of
        # these was neither refused nor survived.
        "unprotected_window_closed",
        "unprotected_window_still_open",
    }
)

#: Outcomes where something DID rule, and ruled that no new entry was the
#: right answer — for reasons that are not a gate turning an idea down.
#:
#: This is the third category, and it exists because the other two cannot
#: hold `held_unchanged` without breaking something:
#:
#:  * It is not a REFUSAL. The portfolio manager holds the position, still
#:    rates it, and deliberately left it out of the target list because the
#:    right action was none. That is the gate WORKING. Reading it as a
#:    refusal is why a book at 1.99x against a 2.0x ceiling — this desk,
#:    today, with twelve `held_unchanged` rows and no orders — was about to
#:    be reported to the owner as a jammed gate.
#:  * It is not a SURVIVAL either, and putting it in `SURVIVED_OUTCOMES` was
#:    the tempting one-word fix. It would have been worse than the bug. A
#:    single surviving candidate ENDS the streak outright, so on a fully
#:    invested book — where every session carries held names — this alarm
#:    could never fire again, including on a gate that really was stuck. A
#:    full book is exactly when a jam is hardest to see by eye.
#:
#: So a candidate that ends here is dropped from the run's evidence, the
#: same mechanical treatment an undecided one gets and for a different
#: reason: it is a decision, but it is not a decision ABOUT a gate refusing
#: an idea. Twelve held names and nothing else means the run is absent and
#: nothing is sent; twelve held names beside five new candidates all killed
#: by one stuck rule still leaves those five, monomorphic, and the alarm
#: still fires. That is the whole point of choosing this category.
NO_ENTRY_DECIDED_OUTCOMES = frozenset(
    {
        "held_unchanged",  # portfolio_manager — holds it, left it out on purpose
        "already_covered",  # opportunity — the nomination matched an analysis
        #   this same run already has; a dedupe, not a verdict
        "not_required",  # funding — no cash sweep was needed to proceed
        "exited",  # position_management — a SELL. This module already
        #   holds that an exit does not make a day non-empty
        #   (see ENTRY_ACTIONS); it is equally not a refusal
        #   of a new idea.
        "stop_out_gap_unexplained",  # reconciliation — a finding about a
        #   position that is already closed, not a candidate
    }
)

#: The union, which is what the loader actually applies: every outcome word
#: that is NOT the desk turning an idea down. Everything outside it reads as
#: a refusal, which is still the conservative default the module was built
#: on — an outcome nobody has classified can only ever lengthen a streak
#: that must still be monomorphic over a CHANGING candidate set.
NOT_A_REFUSAL_OUTCOMES = UNDECIDED_OUTCOMES | NO_ENTRY_DECIDED_OUTCOMES

#: `(stage, outcome)` pairs excluded by WHICH STAGE wrote them, not by the
#: outcome word alone — unlike `NOT_A_REFUSAL_OUTCOMES` above, which is
#: deliberately outcome-only. "failed" cannot be added there: `risk|failed`
#: (src/pipeline_stages.py, the risk manager's output was unparseable),
#: `funding|failed`, `portfolio_manager|failed` and `macro_parse|failed` are
#: all real stages that gate an idea, and excluding "failed" everywhere would
#: hide a genuine jam sitting behind one of them.
#:
#: `specialist|failed` is different in kind: every site that writes it
#: (`src/pipeline.py` ~16441, ~16448, ~16548; `src/pipeline_stages.py` ~5673,
#: ~6046) fires when a seat could not even ANALYZE the candidate — a bar
#: fetch raised, no bars came back, or a batch response left the symbol
#: unresolved after its bounded retry. None of those is the specialist
#: forming an opinion and turning the idea down; the seat never got that
#: far. 2026-09-22's own incident record above already says as much: "the
#: 15:15 tick ... ran its specialists first and one of them failed, leaving
#: a last event that does read as a disposition" — a data-fetch fault, read
#: as a verdict.
#:
#: Left unfixed, a multi-session DATA OUTAGE (a bad market-data feed, say)
#: that still lets each run finish as `no_trades` or `no_orders` writes
#: `specialist|failed` as the terminal event for every candidate in every
#: session — monomorphic across a changing candidate set, which is exactly
#: this detector's trigger shape, and the owner is told the desk "refused
#: every idea" when nothing was ever put in front of a decision at all.
NOT_A_REFUSAL_STAGE_OUTCOMES = frozenset(
    {
        ("specialist", "failed"),
    }
)

#: SEAT ACCEPTANCE (the enabler job). `agent_logs.status` records only that
#: the provider call RETURNED; it has never recorded whether the seat could
#: USE the answer. These two words are that second, missing fact, and they
#: are deliberately a CLOSED pair so no unregistered word can enter the
#: column the way unregistered `outcome` words have corrupted refusal
#: counting before (see NOT_A_REFUSAL_OUTCOMES above).
#:
#: They are NOT pipeline-event `outcome` words and must never be added to the
#: sets above: those classify what a STAGE ruled about a candidate, while
#: these classify what a SEAT did with its own model's reply.
SEAT_ACCEPTED = "accepted"
SEAT_REFUSED = "refused"
SEAT_ACCEPTANCE_WORDS = frozenset({SEAT_ACCEPTED, SEAT_REFUSED})

#: The refusal REASON codes the instrumented call sites can write. Every one
#: already existed in the desk's vocabulary before this column did — they are
#: the `status` literals and `pipeline_event` reasons those same sites
#: already assign on their refusal path. Nothing here is a new word.
SEAT_REFUSAL_REASONS = frozenset(
    {
        "agent_failure",  # smart_money / risk status literal
        "failed",  # data_status["tech"] / evening literal
        "no_valid_grounded_decision",  # pipeline_event portfolio_manager|failed
        "risk_manager_unparseable_output",  # pipeline_event risk|failed
        "position_review_parse_error",  # review_log_kwargs status literal
        "evening_parse_error",  # _ev_log_kwargs status literal
        # Board item 188, second criterion. The three DECISION seats each have
        # several distinct ways their own acceptance gate can reject an answer,
        # and the per-seat word above collapses them all into "it failed". These
        # are the gates' OWN machine-readable reasons, carried on
        # `AgentResult.gate_reason` by the seat that refused, so a per-model
        # usable-answer rate can also say WHAT the small free model gets wrong.
        # Recording vocabulary only — nothing branches on these words.
        "pm_parse_error",  # PM: body was not a decision object
        "pm_schema_error",  # PM: object failed PortfolioDecision
        "pm_grounding_error",  # PM: targets not grounded in evidence
        "pm_repair_changed_decision",  # PM: repair re-decided, failed closed
        "risk_non_json",  # RM: body was not JSON
        "risk_decision_field_validation_failure",  # RM: bad decision-bearing field
        "risk_repair_changed_decision",  # RM: repair re-decided, failed closed
        "risk_repair_not_object",  # RM: repair returned a non-object
        "risk_repair_schema_error",  # RM: repaired body failed the schema
        "risk_parse_exception",  # RM: parse raised
        "review_non_json",  # PR: body was not JSON
        "review_not_object",  # PR: body was JSON but not an object
        "review_schema_validation_failure",  # PR: failed PositionReview schema
    }
)


#: The desk's OWN recorded words for a run that stopped before the decision
#: stage. This detector's premise is "the candidates varied, the outcome did
#: not, therefore the gate is jammed" — and that premise is only about runs
#: that actually ran the gate. A run the cost circuit suspended, or one the
#: evidence gate skipped, or one that crashed on the way in, is not evidence
#: about the gate in either direction, so it is EXCLUDED from the streak: it
#: neither joins it nor breaks it.
#:
#: Every word here is a status literal the pipeline returns
#: (`src/pipeline.py`), read back from the run's own report row rather than
#: inferred from the shape of its evidence. `hard_risk_block`,
#: `no_trades`, `no_orders`, `buys_unfunded`, `intraday_no_trades`,
#: `intraday_scan_no_opportunity`, `executed` and `reviewed` are all
#: deliberately ABSENT: each of those is a run that reached a decision, and
#: a jam has to be able to hide inside them or this check has no job.
NON_DECIDING_STATUSES = frozenset(
    {
        "paid_analysis_suspended",
        "evidence_gate_skip",
        "intraday_scan_crashed",
        "intraday_scan_out_of_credit",
        "intraday_analysis_error",
        "intraday_scan_disabled",
        "intraday_scan_lock_contended",
        "broker_error",
        "analysis_error",
        "provider_error",
        "fetch_error",
        "no_data",
        "kill_switch_halted",
        "market_holiday",
        "early_close",
        "disabled",
        "error",
    }
)

#: The portfolio manager's semantic failures are all spelled `pm_<something>`
#: (`src/agents/portfolio_manager.py`'s `_semantic_failure`). They mean the
#: PM never produced a decision to gate, so they are non-deciding for the
#: same reason as the words above, listed as a prefix because the set of them
#: grows with the PM's own vocabulary and a missed one would be read here as
#: a refusal.
NON_DECIDING_STATUS_PREFIX = "pm_"


def is_non_deciding(status: str) -> bool:
    """True when the desk's own recorded status says this run never reached
    the decision stage, so it is not evidence about the gate."""
    word = str(status or "").strip().lower()
    if not word:
        return False
    return word in NON_DECIDING_STATUSES or word.startswith(NON_DECIDING_STATUS_PREFIX)


#: Any numeric literal, including a signed decimal or an exponent.
_NUMBER = re.compile(r"[-+]?\d[\d,]*(?:\.\d+)?(?:[eE][-+]?\d+)?")

_WHITESPACE = re.compile(r"\s+")


# ---------------------------------------------------------------------------
# the signature of one refusal
# ---------------------------------------------------------------------------


def normalise(text: Any, symbol: str = "") -> str:
    """Strip the candidate's own ticker and every number out of a reason.

    Both substitutions can only merge two texts that describe the same
    rule; neither can split one rule into two. See the module docstring.
    """
    out = str(text or "").strip().lower()
    ticker = str(symbol or "").strip().lower()
    if ticker:
        out = re.sub(rf"\b{re.escape(ticker)}\b", "&", out)
    out = _NUMBER.sub("#", out)
    return _WHITESPACE.sub(" ", out).strip()


def signature_key(payload: dict, symbol: str = "") -> str:
    """The comparable identity of one candidate's terminal outcome.

    `refusal` and `fault` are the two NAMED codes the constructor records
    as data rather than recovering from a log line; they are included
    verbatim (they are codes, not prose) so two different rules never
    collapse into one key just because their prose normalises alike.
    """
    stage = str(payload.get("stage") or "")
    outcome = str(payload.get("outcome") or "")
    code = str(payload.get("refusal") or payload.get("fault") or "")
    return "|".join(
        (
            stage,
            outcome,
            normalise(payload.get("reason"), symbol),
            code,
            normalise(payload.get("detail"), symbol),
        )
    )


# ---------------------------------------------------------------------------
# sessions, read out of the evidence stream
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SessionShape:
    """One run's candidates and how each of them ended."""

    run_id: str
    trading_day: str  # ET calendar date, YYYY-MM-DD
    last_seen: datetime  # newest event in the run, UTC
    placed_entry: bool
    keys_by_symbol: dict[str, str] = field(default_factory=dict)
    outcomes_by_symbol: dict[str, str] = field(default_factory=dict)
    #: The status the run recorded for ITSELF in its own report row, or ""
    #: when no report row was found (an old run, or a fresh database with no
    #: report tables at all).
    disposition: str = ""

    @property
    def candidates(self) -> frozenset[str]:
        return frozenset(self.keys_by_symbol)

    @property
    def reached_decision(self) -> bool:
        """Whether this run is evidence about the gate at all.

        Unknown reads as YES. The failure direction is deliberate: an
        unrecognised status keeps the run in the streak, where it still has
        to be monomorphic over a CHANGING candidate set before anything is
        sent, whereas treating the unknown as an abort would let a genuine
        jam be silenced by a status nobody has taught this module yet.
        """
        return not is_non_deciding(self.disposition)

    @property
    def any_survived(self) -> bool:
        """At least one candidate got through — so "every idea was refused"
        is simply false for this session, whatever the trades table shows."""
        return any(outcome in SURVIVED_OUTCOMES for outcome in self.outcomes_by_symbol.values())

    @property
    def distinct_keys(self) -> frozenset[str]:
        return frozenset(self.keys_by_symbol.values())

    @property
    def is_monomorphic(self) -> bool:
        """Every candidate this session considered died the same way."""
        return len(self.distinct_keys) == 1


@dataclass(frozen=True)
class RefusalSignatureStatus:
    """`should_alert` is the only field callers act on."""

    trading_day: str | None = None  # the day judged (ET)
    streak: list[SessionShape] = field(default_factory=list)
    #: Runs stepped over because the desk's own record says they never
    #: reached the decision stage. Reported, never counted as refusals.
    skipped: list[SessionShape] = field(default_factory=list)
    key: str | None = None  # the one unvarying reason, if any
    current: bool = False  # newest streak session is that day
    inputs_varied: bool = False  # the candidate sets were not all equal
    db_error: str | None = None
    already_alerted_for_day: bool = False

    @property
    def is_jammed(self) -> bool:
        """The full trigger, in one place and with no number in it."""
        return bool(self.key) and self.current and self.inputs_varied

    @property
    def should_alert(self) -> bool:
        return self.is_jammed and not self.already_alerted_for_day

    @property
    def symbols(self) -> list[str]:
        seen: set[str] = set()
        for session in self.streak:
            seen |= set(session.candidates)
        return sorted(seen)


def _utc_now() -> datetime:
    """Seam for tests — real code never patches `datetime` itself."""
    return utc_now()


def _parse_iso(stamp: str) -> datetime | None:
    try:
        when = datetime.fromisoformat(str(stamp))
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return when.astimezone(timezone.utc)


def _connect_ro(path: str) -> sqlite3.Connection:
    """Read-only by OS enforcement — this module never writes the trading DB."""
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=2000")
    return conn


def run_dispositions(conn: sqlite3.Connection) -> dict[str, str]:
    """Each run's own recorded verdict on itself, keyed by run_id.

    Two tables carry it, and both are the desk's own record rather than
    anything this module infers:

      * `intra_check_reports` — one row per run_id. Its top-level `status`
        describes the TICK (almost always "ok": the deterministic loss check
        did run), so the scan's own nested `intraday_scan.status` is
        preferred where present, because that is the half that either
        reached a decision or did not.
      * `session_reports` — the morning/midday/close/evening rows, whose
        top-level `status` is the run's outcome word.

    A missing table, a missing row or unreadable JSON all yield no entry,
    and a run with no entry is treated as having reached a decision (see
    `SessionShape.reached_decision`).
    """
    out: dict[str, str] = {}
    for sql, nested in (
        ("SELECT run_id, payload_json FROM intra_check_reports", True),
        ("SELECT run_id, payload_json FROM session_reports", False),
    ):
        try:
            rows = conn.execute(sql).fetchall()
        except Exception:  # noqa: BLE001 — table absent on a fresh database
            continue
        for row in rows:
            run_id = str(row["run_id"] or "")
            if not run_id:
                continue
            try:
                payload = json.loads(row["payload_json"] or "{}")
            except (TypeError, ValueError):
                continue
            if not isinstance(payload, dict):
                continue
            status = str(payload.get("status") or "")
            if nested:
                scan = payload.get("intraday_scan")
                if isinstance(scan, dict) and scan.get("status"):
                    status = str(scan.get("status"))
            if status:
                out.setdefault(run_id, status)
    return out


def load_sessions(
    db_path: str | Path | None = None,
) -> tuple[list[SessionShape], str | None]:
    """Every run that considered at least one candidate, oldest first.

    A run with no symbol-scoped `pipeline_event` row at all considered
    nothing — an intraday tick that found no mover, an evening review — and
    is absent from this list entirely. It neither joins a streak nor breaks
    one: "the desk considered nothing" is item 11's question, already
    alarmed elsewhere, and is not this check's business.
    """
    path = str(db_path) if db_path is not None else str(DB_PATH)
    try:
        conn = _connect_ro(path)
    except Exception as exc:  # noqa: BLE001
        return [], f"database unreadable: {exc}"
    try:
        rows = conn.execute(
            "SELECT run_id, symbol, timestamp, evidence_json "
            "FROM specialist_evidence "
            "WHERE kind='pipeline_event' AND symbol IS NOT NULL "
            "ORDER BY id",
        ).fetchall()
        entered = {
            str(r["run_id"])
            for r in conn.execute(
                "SELECT DISTINCT run_id FROM trades WHERE run_id IS NOT NULL AND UPPER(action) IN (?, ?)",
                ENTRY_ACTIONS,
            ).fetchall()
            if r["run_id"]
        }
        dispositions = run_dispositions(conn)
    except Exception as exc:  # noqa: BLE001
        return [], f"query failed (table missing on a fresh database?): {exc}"
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass

    keys: dict[str, dict[str, str]] = {}
    outcomes: dict[str, dict[str, str]] = {}
    newest: dict[str, datetime] = {}
    order: list[str] = []
    for row in rows:
        run_id = str(row["run_id"] or "")
        symbol = str(row["symbol"] or "").strip().upper()
        if not run_id or not symbol:
            continue
        try:
            payload = json.loads(row["evidence_json"] or "{}")
        except (TypeError, ValueError):
            continue
        if not isinstance(payload, dict):
            continue
        if run_id not in keys:
            keys[run_id] = {}
            outcomes[run_id] = {}
            order.append(run_id)
        # Rows arrive in id order, so the LAST write for a symbol wins —
        # that is the outcome the candidate actually ended on. Unless that
        # last write is one of NOT_A_REFUSAL_OUTCOMES — the candidate
        # arriving somewhere with nothing ruled yet, or something ruling
        # that no new entry was the right answer. Either way the candidate
        # carries no refusal, and any earlier outcome it had is discarded
        # with it, because the evidence stream only ever moves a candidate
        # FORWARD.
        outcome = str(payload.get("outcome") or "")
        stage = str(payload.get("stage") or "")
        if outcome in NOT_A_REFUSAL_OUTCOMES or (stage, outcome) in NOT_A_REFUSAL_STAGE_OUTCOMES:
            keys[run_id].pop(symbol, None)
            outcomes[run_id].pop(symbol, None)
        else:
            keys[run_id][symbol] = signature_key(payload, symbol)
            outcomes[run_id][symbol] = outcome
        when = _parse_iso(row["timestamp"])
        if when is not None and when > newest.get(run_id, datetime.min.replace(tzinfo=timezone.utc)):
            newest[run_id] = when

    sessions: list[SessionShape] = []
    for run_id in order:
        when = newest.get(run_id)
        if when is None:
            continue
        if not keys[run_id]:
            # Nothing this run touched ended on a refusal — every
            # candidate is either still undecided or was settled without a
            # new entry being turned down. Same treatment as a run with no
            # symbol-scoped event at all: absent from this list, joining no
            # streak and breaking none. This is the fully-invested book:
            # twelve held names, no orders, and nothing to report.
            continue
        sessions.append(
            SessionShape(
                run_id=run_id,
                trading_day=when.astimezone(ET).date().isoformat(),
                last_seen=when,
                placed_entry=run_id in entered,
                keys_by_symbol=dict(keys[run_id]),
                outcomes_by_symbol=dict(outcomes[run_id]),
                disposition=dispositions.get(run_id, ""),
            )
        )
    sessions.sort(key=lambda s: s.last_seen)
    return sessions, None


def streak_and_skipped(
    sessions: list[SessionShape],
) -> tuple[list[SessionShape], list[SessionShape]]:
    """`(streak, skipped)` — the unvarying-refusal run, and the runs passed
    over on the way because they never reached a decision.

    The streak is the consecutive newest sessions that ALL refused every
    candidate for the same single reason. Empty when the newest deciding
    session is not of that shape — which is the common, healthy case.

    Walks backwards from the newest session and stops at the first one that
    ends any other way: a session that entered something, a session where
    any candidate survived to a positive terminal outcome, a session whose
    candidates died for more than one reason, or a session whose one reason
    is a different reason. No length is imposed; the data ends the walk.

    A session the desk's own record says never reached the decision stage is
    neither joined nor broken on — it is stepped over and remembered in
    `skipped`. Stepping over rather than breaking is the point: a jam does
    not stop being a jam because the cost circuit suspended one tick in the
    middle of it, and a suspended tick is not evidence that the jam cleared.
    """
    streak: list[SessionShape] = []
    skipped: list[SessionShape] = []
    key: str | None = None
    for session in reversed(sessions):
        if not session.reached_decision:
            skipped.append(session)
            continue
        if session.placed_entry or session.any_survived or not session.is_monomorphic:
            break
        only = next(iter(session.distinct_keys))
        if key is None:
            key = only
        elif only != key:
            break
        streak.append(session)
    streak.reverse()
    skipped.reverse()
    return streak, skipped


# ---------------------------------------------------------------------------
# on-box state — one alert per trading day
# ---------------------------------------------------------------------------


def load_state(path: Path | None = None) -> dict[str, Any]:
    try:
        raw = json.loads((path or STATE_PATH).read_text())
    except (OSError, ValueError):
        raw = None
    if not isinstance(raw, dict):
        raw = {}
    raw.setdefault("alerted_for_day", None)
    raw.setdefault("last_result", None)
    raw.setdefault("updated_at", None)
    return raw


def save_state(state: dict[str, Any], path: Path | None = None) -> bool:
    """Atomic write, same shape as the sibling watchdogs. Never raises."""
    target = path or STATE_PATH
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(dir=str(target.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as handle:
                json.dump(state, handle, indent=2, sort_keys=True)
                handle.write("\n")
            os.replace(tmp_name, target)
        except BaseException:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise
    except OSError:
        return False
    return True


# ---------------------------------------------------------------------------
# the check
# ---------------------------------------------------------------------------


def check_refusal_signature(
    *,
    now: datetime | None = None,
    broker: Any = None,
    db_path: str | Path | None = None,
    state_path: Path | None = None,
) -> RefusalSignatureStatus:
    """Read the evidence stream, decide, persist the once-per-day marker.

    Never raises, never writes to the trading database, never touches the
    broker beyond the calendar lookup `most_recent_trading_day` already
    does for the coverage watchdog.
    """
    moment = now or _utc_now()
    state = load_state(state_path)
    day = most_recent_trading_day(moment, broker).isoformat()

    sessions, db_error = load_sessions(db_path)
    streak, skipped = streak_and_skipped(sessions)
    key = next(iter(streak[-1].distinct_keys)) if streak else None
    # CURRENT: the newest session in the streak is the most recent trading
    # day's. A paused desk fails this and stays silent.
    current = bool(streak) and streak[-1].trading_day == day
    # The input changed and the outcome did not. This is also what forces
    # more than one session into the streak: identical sets cannot vary.
    sets = {s.candidates for s in streak}
    inputs_varied = len(sets) > 1

    status = RefusalSignatureStatus(
        trading_day=day,
        streak=streak,
        skipped=skipped,
        key=key,
        current=current,
        inputs_varied=inputs_varied,
        db_error=db_error,
        already_alerted_for_day=(state.get("alerted_for_day") == day),
    )
    if status.should_alert:
        state["alerted_for_day"] = day
    state["last_result"] = {
        "trading_day": day,
        "streak_runs": [s.run_id for s in streak],
        "skipped_runs": [s.run_id for s in skipped],
        "key": key,
        "current": current,
        "inputs_varied": inputs_varied,
        "db_error": db_error,
    }
    state["updated_at"] = moment.replace(microsecond=0).isoformat()
    save_state(state, state_path)
    return status


#: Board item 89 defect 5, applied here (2026-09-18). The owner was sent
#: `portfolio_manager|omitted|candidate_not_selected_for_target||` — a
#: pipe-joined internal key, with two empty trailing fields, as the answer
#: to "why did the desk refuse everything". Every entry below is what a key
#: MEANS in the words a person would use; the key itself never reaches the
#: message except labelled as machine output, and an unrecognised key is
#: DESCRIBED rather than pasted through or guessed at — the same rule
#: `_ORDER_END_WORDS` in `src/trader_feed.py` follows.
#:
#: Keyed on (stage, outcome, code) — the three parts of the signature that
#: are internal spellings rather than prose, so a new gate's wording cannot
#: silently fall out of this map while its code stays the same.
_PLAIN_BY_SIGNATURE: dict[tuple[str, str, str], str] = {
    # The historical key, kept so an OLD stored session still renders in
    # English. It cannot be produced any more (board item 133 replaced it).
    ("portfolio_manager", "omitted", ""): (
        "the portfolio manager simply did not pick the name, and was never "
        "asked to say why — so this is not really a reason at all. The desk "
        "no longer records refusals this way"
    ),
}


def _plain_key(key: str) -> str:
    """One reader-facing sentence for a signature key.

    Never returns the raw key on its own, and never invents a meaning for
    one it does not recognise.
    """
    parts = str(key or "").split("|")
    if len(parts) < 4:
        return _describe_or_admit("", "")
    stage, outcome, reason, code = parts[0], parts[1], parts[2], parts[3]
    detail = parts[4] if len(parts) > 4 else ""
    known = _PLAIN_BY_SIGNATURE.get((stage, outcome, code))
    if known:
        return known
    if stage == "portfolio_manager" and code:
        # The portfolio manager's own grounds live in one place, beside the
        # vocabulary it emits them from, so a new ground gets its wording
        # there rather than needing a second edit here. Only when there IS a
        # code: with none, `plain_reason` has nothing to look up and would
        # answer "no plain wording" over a `reason` field that is sitting
        # right there, which is the bug being fixed below.
        from src.pm_accounting import plain_reason

        return plain_reason(code)
    if code:
        return f"the desk recorded a reason it has no plain wording for (its internal name for it is '{code}')"
    return _describe_or_admit(reason, detail)


def _describe_or_admit(reason: str, detail: str) -> str:
    """Quote the describable field, or admit there is none.

    A signature key is `stage|outcome|reason|code|detail`, and the plain
    wording used to be looked for in `code` ALONE. When a gate records its
    grounds in `reason` instead and leaves `code` empty — which is what
    every `opportunity` event does — the owner was told "the desk recorded a
    reason it has no plain wording for" while the words `intraday_move_
    threshold` sat unread two fields to the left (2026-09-22).

    The text is the desk's internal spelling, so it is quoted and attributed
    rather than passed off as English: this module still never invents a
    meaning for a token it does not know. The admission sentence now means
    only what it says — that the key carries no describable field at all.
    """
    text = str(reason or "").strip() or str(detail or "").strip()
    if not text:
        return "the desk recorded a reason it has no plain wording for"
    return f"the desk has no plain wording for this one, and recorded it in its own words as '{text}'"


def _human_reason(session: SessionShape) -> str:
    """The one reason, in plain English, with the machine key labelled.

    Both halves are kept. The sentence is what the owner reads; the key is
    often the only thing an engineer can grep for, and dropping it would
    trade one kind of unreadable message for another.
    """
    key = next(iter(session.distinct_keys), "")
    if not key:
        return "unknown — the desk recorded no reason at all"
    return (
        f"{_plain_key(key)}.\n"
        f"(The desk's internal name for that, kept for the record and not "
        f"something you need to act on: {key})"
    )


def alert_text(status: RefusalSignatureStatus) -> str:
    """Severity in the leading word, never colour alone (Rex is red-green
    colour blind; `src/notifier.py` convention)."""
    sessions = status.streak
    lines = []
    for session in sessions:
        lines.append(
            f"  {session.trading_day} ({session.run_id}): "
            f"{len(session.candidates)} candidate(s) reached a decision, all "
            "refused — " + ", ".join(sorted(session.candidates))
        )
    skipped = ""
    if status.skipped:
        words = sorted({s.disposition for s in status.skipped if s.disposition})
        skipped = (
            f"\n{len(status.skipped)} other run(s) are NOT counted above: the "
            "desk's own record says they stopped before the decision stage"
            + (f" ({', '.join(words)})" if words else "")
            + ", so they say nothing about the gate either way.\n"
        )
    return (
        "🔴 THE DESK HAS REFUSED EVERY IDEA FOR THE SAME REASON\n"
        f"Across the last {len(sessions)} session(s) that reached a decision "
        "on at least one candidate, every candidate that reached a decision "
        "was refused, and every refusal carried the SAME reason — while the "
        "candidates themselves changed. That is the shape of a jammed gate, "
        "not of a quiet market: a quiet market kills different names for "
        "different reasons.\n\n"
        f"The one reason: {_human_reason(sessions[-1]) if sessions else 'unknown'}\n\n"
        + "\n".join(lines)
        + "\n"
        + skipped
        + "\n"
        "Nothing has been changed, placed or cancelled. This is not a count "
        "of empty days — an empty day is normal here and no number of them "
        "would trigger this on its own. What triggered it is that the reason "
        "never varied while the input did, in runs that actually ran the "
        "gate.\n"
        "This message repeats at most once per trading day while the "
        "condition holds, and stops by itself the moment one candidate is "
        "refused for a different reason or any entry is placed."
    )


def status_line(status: RefusalSignatureStatus) -> str:
    """One journal line for the heartbeat unit."""
    if status.db_error:
        return f"refusal_signature: could NOT read the evidence ({status.db_error})"
    if not status.streak:
        return (
            "refusal_signature: OK — the most recent session that reached a "
            "decision either entered something or refused its candidates for "
            "more than one reason"
        )
    if not status.current:
        newest = status.streak[-1].trading_day
        return (
            f"refusal_signature: an unvarying refusal run ends {newest}, but "
            f"the most recent trading day is {status.trading_day} — the desk "
            "is not running sessions, not alerting"
        )
    if not status.inputs_varied:
        return (
            "refusal_signature: one session refused everything for one "
            "reason; the candidate set has not changed yet, so nothing is "
            "shown to be unvarying"
        )
    if status.already_alerted_for_day:
        return (
            f"refusal_signature: STILL JAMMED over {len(status.streak)} "
            f"session(s); already alerted for {status.trading_day}"
        )
    return (
        f"refusal_signature: JAMMED — {len(status.streak)} consecutive "
        f"session(s) refused every candidate for one unvarying reason"
    )
