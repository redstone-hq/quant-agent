"""Read the desk's own application log for a window and say, in plain words,
whether the desk is healthy, degraded or hurt.

WHY THIS EXISTS. The owner reads Telegram and nothing else. Everything in
`quant_agent.log` that is not routed through an explicit owner alert reaches
nobody, and the two worst classes of fault on this desk have both been of
exactly that kind: a feed that failed every single time it was tried for a
fortnight (`Feed AP Business fetch failed: HTTP Error 403: Forbidden`,
53 occurrences, 2026-08-14 to 2026-08-28, verified in the rotated logs) and a
broker socket that was refused 11,427 times in one day
(`server rejected WebSocket connection: HTTP 429`, 2026-09-15, verified in
`quant_agent.log.3`). Neither produced a single message he saw.

THE SEVERITY RULE, AND WHY IT IS SHAPED THIS WAY.
This desk has a standing doctrine that a deliberate, correct refusal is NOT a
fault. A session that declines every candidate because the reward:risk did not
clear the bar has worked perfectly, and dressing that up as an incident trains
the owner to ignore the channel — which is the only channel he has. So a
refusal, a handled data drop, a retry that succeeded, and a cost figure that
was reconciled are all INFORMATIONAL and never produce a bullet.

But the same doctrine says a quiet day that is really a broken day must never
read as healthy. The failure mode that has actually bitten this desk is not a
loud crash; it is a warning nobody is subscribed to, repeated until it becomes
the background. So the bar for reporting is deliberately NOT "is it noisy" and
NOT a count. It is one of four conditions, each of which is a statement about
consequence rather than volume:

  COST_A_DECISION      — the desk was unable to decide something it set out to
                         decide. A skipped decision and a name that silently
                         fell out of a seat's answer are the same injury: the
                         desk acted on a view it did not actually form.
  MONEY_UNPROTECTED    — a position was left, for any length of time, without
                         the protective stop the desk believes is on it. This
                         is the only class where the loss is unbounded, so it
                         is always reported and always drives the verdict.
  PROVIDER_REJECTED_US — the broker or a data provider refused us. We cannot
                         fix it by trying harder and it invalidates the desk's
                         assumption that it can act when it decides to.
  SILENTLY_FAILING     — it has failed every time it was tried for long enough
                         that the desk's apparent health is misleading. This is
                         reported PRECISELY BECAUSE it is quiet: the longer it
                         has been true, the more the silence has been read as
                         health. Severity here is a function of duration, which
                         is measured from the logs, not assumed.

Anything that does not meet one of those four is not reported at all.

NO INVENTED LIMITS. The number of bullets is whatever the desk actually has
wrong — that is a measurement, not a setting, and if it is nine then nine is
the signal. The single length ceiling encoded here is Telegram's own published
per-message limit of 4,096 characters (Bot API `sendMessage`, `text` field),
consumed via `TelegramNotifier.MAX_MESSAGE_CHARS` so this module does not carry
a second copy of it. When a report will not fit, it SPLITS by severity — the
serious findings in the first message, the rest in a second — rather than
dropping a real fault to fit, which would reproduce the exact "looks quiet but
is not" failure the report exists to prevent.

A FALSE ALARM IS NOT A SMALL ERROR. The first version of this module shipped
three, all found by reading its matched lines against the log by hand:

  * it counted the macro seat's own prompt — an INFO line stating FULL data
    coverage — as an economics failure, because the words matched;
  * it told the owner his broker credentials were a fill-this-in stand-in,
    in the present tense, from an alarm that fired three minutes before he
    wrote the real keys and eleven minutes before the desk placed real orders
    with them;
  * it made "thirteen holdings left without their safety net" the headline of
    a morning on which nine sub-share stops went missing and every one was
    repaired, seven of them within the same second.

Three rules came out of that, and they are why this module is shaped as it is.
First, a REPORTABLE family only matches a line the desk itself logged as
WARNING or worse: text alone is not evidence that something went wrong.
Second, a family can name the thing it is ABOUT and the lines that CANCEL it,
so a fault repaired inside the window is not reported — matched per holding,
because seven repairs out of nine is two still open, not none. Third, every
bullet says when the fault last happened and never asserts a present state,
because a log line is evidence about a moment and several of these alarms are
rate-limited to once a day, which makes their absence meaningless too.

FAIL-CLOSED. Matching log text with regular expressions is matching against an
interface that can change wording without warning; a renamed message would
silently stop being counted and the report would read healthy. So every ERROR
and CRITICAL line in the window that matches no known family is itself counted
as a family (`unrecognised_faults`) and reported. The health check cannot
quietly stop working.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from src.trading_calendar import utc_now
from pathlib import Path

from src.notifier import TelegramNotifier
from src.time_format import fmt_time_12h
from src.log_markers import SWEEP_LOG_NAME
from src.health_words import (  # noqa: F401  re-exported names
    OWNER_TZ,
    _duration_words,
    _last_at_words,
    _plural,
    _time_words,
)

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent

#: Production app log and its rotations. `quant_agent.log` is the live file;
#: `.1`-`.5` are the rotations, continuous back to 2026-08-14.
DEFAULT_LOG_DIR = Path("/home/qamc/quant-agent")
LOG_BASENAME = "quant_agent.log"

#: Where the watermark lives. Keyed to the log directory so a dry run against a
#: fixture cannot advance production's watermark.
DEFAULT_STATE_PATH = PROJECT_ROOT / "data" / "log_health" / "state.json"

#: Log timestamps are UTC (the box runs UTC); the owner is ET. Every time the
#: owner is shown is converted. `docs/WORK.md`, "Engineering setup", records
#: this — quoting him a UTC time has caused confusion before.
LOG_TZ = timezone.utc

#: `2026-09-18 13:47:27,710 [ERROR] src.pipeline: text`
_LINE_RE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d{3} "
    r"\[(?P<level>[A-Z]+)\] (?P<logger>[\w.]+): (?P<msg>.*)$"
)
_SERIOUS_LEVELS = frozenset({"ERROR", "CRITICAL"})

# --- the four reporting conditions ------------------------------------------
# Ordered worst-first; the verdict and the bullet order both read off this
# order, so neither needs a rank number of its own.
COST_A_DECISION = "cost_a_decision"
MONEY_UNPROTECTED = "money_unprotected"
PROVIDER_REJECTED_US = "provider_rejected_us"
SILENTLY_FAILING = "silently_failing"
HANDLED = None  # informational — never reported

_REASON_ORDER = (
    MONEY_UNPROTECTED,
    COST_A_DECISION,
    PROVIDER_REJECTED_US,
    SILENTLY_FAILING,
)

#: A verdict of "hurt" is reserved for the two conditions that cost the desk
#: something it cannot get back — a decision it could not make, or a position
#: it did not protect. Being refused by a provider, or failing quietly, leaves
#: the desk working but not trustworthy: that is "degraded".
_HURT_REASONS = frozenset({COST_A_DECISION, MONEY_UNPROTECTED})


@dataclass(frozen=True)
class FaultFamily:
    """One recognised way this desk breaks, in the owner's words.

    `sentence` is the whole bullet, bar the disposition. It is written as a
    complete plain-English sentence with `{n}` (the count) and `{plural}` (an
    "s" when `n != 1`) so a bullet never reads like a log line. It must contain
    no file name, no function name, no field name, no status token and no run
    id — the owner has said repeatedly that developer vocabulary in a message
    aimed at him makes the message unreadable.

    `board_item` is the number of the `docs/WORK.md` item that already tracks
    this, or None. It is checked against the live board at render time: a
    family whose item has been retired must not keep telling the owner that
    work is happening.
    """

    key: str
    short_name: str
    sentence: str
    reason: str | None
    patterns: tuple[re.Pattern[str], ...]
    board_item: int | None = None
    #: True when severity depends on how long this has been failing, not on
    #: what happened in the window. The first occurrence is then looked up
    #: across the whole retained log history so the bullet can state a
    #: measured duration instead of an impression.
    measure_duration: bool = False
    #: Captures the thing the fault is ABOUT (a ticker), so a fault and its
    #: repair can be matched up per holding instead of in bulk. Without it,
    #: nine stops missing and seven repaired reads as nine unprotected
    #: holdings, which is how the first version of this report told the owner
    #: a self-healed morning was the worst thing that happened to him.
    subject: re.Pattern[str] | None = None
    #: Lines that CANCEL an occurrence. An occurrence whose subject is
    #: resolved later in the window is not reported — the desk fixed it, and
    #: a fixed fault reported as current is a false alarm.
    resolved_by: tuple[re.Pattern[str], ...] = ()
    #: ISO date on which the owner explicitly accepted this standing exposure.
    #: It is still reported while it is true (acceptance is not a fix and must
    #: not hide risk), but it reads "known, accepted <day month>", sorts after
    #: every open fault and never sets the verdict to HURT. Owner, 2026-10-13:
    #: a known, accepted exposure heading the report as HURT every morning is
    #: not true.
    accepted_on: str | None = None
    #: Captures the dollars uncovered from the log line, when it carries them.
    dollars: re.Pattern[str] | None = None
    #: The count is the number of holdings the coverage sweep's latest run
    #: line says are still open, not the number of log lines. The overnight
    #: exposure is logged ONCE an evening for the whole book, so counting lines
    #: told the owner "1 holding" on a night the sweep found 6 of 6 every 30
    #: minutes (2026-10-13). The sweep's count does not separate whole-share
    #: gaps from sub-share ones, and the sentence says so.
    count_from_sweep: bool = False


def _p(*patterns: str) -> tuple[re.Pattern[str], ...]:
    # Case-INSENSITIVE. The audit that forced this (2026-09-19): the desk's
    # own logger call spells "Failed to parse tech analysis item" with a
    # capital F, and this family's pattern was `r"failed to parse"` — an
    # exact-case match that had matched every OTHER seat's "failed to parse"
    # (lowercase, e.g. "Phase 13: macro_analysis failed to parse") but never
    # the technical seat's own per-item failures, silently, since the line
    # was first written. A log line's wording is not a contract on its
    # casing, and the technical seat is now the only one that can stop the
    # desk — a pattern that only works for the case someone happened to type
    # is exactly the fragility the fail-closed design elsewhere in this
    # module exists to catch. Reviewed against every pattern below and the
    # full 2026-08-17 to 2026-09-18 production history: none relies on case
    # to tell two DIFFERENT real conditions apart.
    return tuple(re.compile(p, re.IGNORECASE) for p in patterns)


#: A later refresh in which the same congressional source answered clears its
#: earlier failure — the saved copy is current again, so the fault is over.
_CONGRESS_SOURCE_RECOVERED = _p(
    r"^Congressional refresh: source=(\w+) outcome=(?:fetched|not_modified)",
)


# --- the families -----------------------------------------------------------
# Every pattern below was read off real production log lines (verified against
# /home/qamc/quant-agent/quant_agent.log and its rotations on 2026-09-18);
# tests/fixtures/log_health_production_excerpt.txt holds genuine copies.
FAMILIES: tuple[FaultFamily, ...] = (
    FaultFamily(
        key="decision_skipped_no_evidence",
        short_name="decisions thrown away for want of an answer",
        sentence=(
            "The desk threw away {n} trading decision{plural} because a research "
            "desk it had asked never answered in time"
        ),
        reason=COST_A_DECISION,
        patterns=_p(r"EVIDENCE GATE .*decision skipped", r"(?m)^DECISION SKIPPED\b"),
        # Item 20 (gate the decision on evidence coverage) was retired
        # 2026-10-01: the counting half shipped as a per-name record, and
        # the owner's remaining question -- whether the intraday technical
        # read may report LOST -- was answered yes, which the code already
        # did. Skipping a decision for want of an answer is now INTENDED
        # behaviour rather than a tracked defect. The REPORTING stays,
        # because how often the desk throws a decision away is a standing
        # fact worth counting, so it no longer points at a board item.
        board_item=None,
    ),
    FaultFamily(
        key="seat_answer_unreadable",
        short_name="answers that came back unreadable",
        sentence=(
            "{n} research answer{plural} came back in a form the desk could not "
            "read, so the work was paid for and thrown away"
        ),
        reason=COST_A_DECISION,
        patterns=_p(
            r"failed to parse",
            r"validation failed — attempting one repair reprompt",
            # A seat's whole answer was not JSON at all — worse than one bad
            # item, and previously unclassified for every seat that can emit
            # it (verified on tech: "Tech analyst returned non-JSON for batch
            # analysis"; macro/earnings/risk/news/portfolio_manager/etc. use
            # the same wording).
            r"returned non-json",
        ),
    ),
    FaultFamily(
        key="extra_company_in_answer_ignored",
        short_name="answers that included a company nobody asked about",
        sentence="{n} answer{plural} included a company nobody asked about; that line was ignored",
        reason=HANDLED,
        # Real line: "Tech analyst emitted 1 row(s) for symbols not in the
        # submitted chunk — dropped: ['SPX']". The answer WAS read and every
        # requested company kept; only the unrequested row was discarded, so
        # nothing was paid for and lost (it was wrongly filed under
        # unreadable answers until 19 Oct 2026).
        patterns=_p(r"emitted \d+ row\(s\) for symbols not in the submitted chunk"),
    ),
    FaultFamily(
        key="names_dropped_from_answer",
        short_name="companies quietly missing from an answer",
        sentence=(
            "{n} of the companies the desk asked about went missing from the "
            "answer it got back, so it judged without them"
        ),
        reason=COST_A_DECISION,
        # ONLY the lines that record a name the desk gave up on. The seat
        # reports a missing name THREE times on its way through a retry —
        # once when the first answer came back short, once when a
        # consolidated recovery is launched, and once if the name is still
        # missing afterwards — and only the last of those is a loss. On
        # 2026-09-18 ten names went missing and every one came back on the
        # recovery ("0 remain explicit failures"); counting the first line
        # would have reported ten companies judged blind on a day none were.
        patterns=_p(
            # Broadened from the literal `unresolved after retry` (2026-09-19):
            # the technical seat's own final-loss line now also reads
            # "unresolved after the single shared recovery" (a multi-chunk
            # batch's consolidated recovery) and "unresolved after parsing
            # (shared retry budget exhausted)" (no retry was even attempted) —
            # both real, both a name the desk judged without, and both were
            # silently unclassified because the old pattern named only one of
            # the three endings this line can have.
            r"unresolved after",
            r"[1-9]\d* remain explicit failures",
            r"dropping malformed \w+ entry",
        ),
    ),
    FaultFamily(
        key="research_seat_partial",
        short_name="a research desk that answered only partly",
        sentence=(
            "A research desk answered only partly on {n} occasion{plural}; the "
            "items it left out are counted separately where the desk named them"
        ),
        reason=COST_A_DECISION,
        # Same event as the seat's own "unresolved after ..." line, so it is
        # worded as a partial answer, never as a second outage. Matches only
        # when the single seat named as degraded has status 'partial'; any
        # other status falls through to research_seat_unavailable below.
        patterns=_p(r"Morning research degraded: (\w+) \| full status=.*'\1': 'partial'"),
    ),
    FaultFamily(
        key="research_seat_unavailable",
        short_name="a research desk that could not be reached",
        sentence=(
            "A research desk could not be reached on {n} occasion{plural} and the work had to go ahead short-handed"
        ),
        reason=COST_A_DECISION,
        # Deliberately NOT `Agent X attempt N failed ... Retrying` — that line
        # is a retry that then succeeded, and the desk's doctrine is explicit
        # that something handled correctly is informational. It is matched
        # below, among the things that never get a bullet. Only a seat that
        # was still missing when the work went ahead lands here.
        patterns=_p(r"Morning research degraded"),
    ),
    FaultFamily(
        key="stop_missing_or_failed",
        short_name="holdings still without their safety net",
        sentence=("The window ended with {n} holding{plural} still missing the safety net that limits a loss"),
        reason=MONEY_UNPROTECTED,
        # ONLY a gap that is still open when the window closes. On 2026-09-18
        # the desk found nine sub-share stops missing at 09:30 ET, repaired
        # seven within the same second and the eighth fifteen minutes later;
        # the ninth was covered by an existing whole-share stop. Reporting
        # that as nine unprotected holdings — which the first version of this
        # module did, and made its headline — is not a smaller error than
        # missing a real one. It is the same error: telling the owner
        # something about his money that is not true.
        patterns=_p(
            r"FRACTIONAL STOP MISSING",
            r"FRACTIONAL STOP RE-PLACEMENT FAILED",
            r"protective stop.*FAILED for",
            r"coverage repair FAILED",
            r"STOP RECORD MISMATCH",
        ),
        subject=re.compile(
            r"(?:MISSING DURING SESSION HOURS|RE-PLACEMENT FAILED for|"
            r"FAILED for|repair FAILED for|MISMATCH):? ([A-Z][A-Z.-]{0,5})"
        ),
        # Three kinds of proof that the gap closed, all of them the desk's
        # own words: the repair reporting success, the session confirming the
        # sub-share leg is back, and — the one that matters most — a later
        # read of the BROKER saying the holding carries stops. Without that
        # third line, RSG read as unprotected all day on 2026-09-18 although
        # the broker was holding two stops against it fifteen minutes later.
        # Caveat, stated rather than hidden: the broker read counts stops on
        # the holding, not specifically on the sub-share remainder, so it is
        # good evidence rather than proof.
        resolved_by=_p(
            r"COVERAGE REPAIRED: ([A-Z][A-Z.-]{0,5})",
            r"FRACTIONAL DAY STOP RE-PLACED: ([A-Z][A-Z.-]{0,5})",
            r"get_current_stop_price: ([A-Z][A-Z.-]{0,5}) carries [1-9]",
        ),
        # Item 127 (two desk processes racing the same broker write) was
        # retired 2026-09-26: one advisory lock now wraps the whole repair
        # pass and the half-hourly check's broker-writing preamble. The
        # REPORTING of a holding still missing its stop stays — that is a
        # standing fact about money, not a tracked defect — so it no longer
        # points at a board item.
        board_item=None,
    ),
    FaultFamily(
        key="overnight_fractional_exposure",
        short_name="sub-share remainders carrying no stop overnight",
        sentence=(
            "{n} holding{plural} went through the night with shares carrying "
            "no stop, by the coverage sweep's last count, which does not "
            "separate whole shares from the part-shares the broker will not "
            "protect overnight"
        ),
        count_from_sweep=True,
        reason=MONEY_UNPROTECTED,
        # Real, bounded (only the fraction; whole shares carry a GTC stop), and
        # NOT a bug: the broker refuses an overnight stop on a part-share, so
        # this is a standing constraint. The owner accepted it 2026-09-02. It
        # is still money with no floor under it, so it is
        # reported — but only while it is TRUE. Once the next session repairs
        # the coverage it is resolved and drops out, which is why a report
        # before the open carries it and a report after the close does not.
        patterns=_p(r"OVERNIGHT FRACTIONAL EXPOSURE"),
        resolved_by=_p(r"COVERAGE REPAIRED:"),
        accepted_on="2026-09-02",
        dollars=re.compile(r"\$([\d,]+(?:\.\d+)?) total at risk", re.IGNORECASE),
        # A standing broker constraint, not a defect anyone is fixing: the
        # broker refuses an overnight stop on a part-share. It was never
        # item 127's to close, and item 127 is retired.
        board_item=None,
    ),
    FaultFamily(
        key="broker_turned_us_away",
        short_name="the broker turning the desk away",
        sentence=("The broker turned the desk away {n} time{plural} when it tried to open its live connection"),
        reason=PROVIDER_REJECTED_US,
        patterns=_p(
            r"server rejected WebSocket connection",
            r"trade_updates websocket handshake failed",
            r"websocket did not authenticate",
            r"error during websocket communication",
            r"order-fill stream unavailable",
        ),
        # Item 86 was retired on live evidence; nothing on the board tracks
        # this family now. The reporting is unchanged — only the claim goes.
        board_item=None,
    ),
    FaultFamily(
        key="broker_not_sure_who_we_are",
        short_name="the broker unable to confirm who the desk is",
        # PAST TENSE, deliberately, and it is not a style choice. This alarm
        # is rate-limited to once a day, so a log line saying it fired says
        # nothing about whether it is still true — and on 2026-09-18 it fired
        # at 09:00 ET, three minutes before the real keys were written, after
        # which the desk placed three real orders a stand-in key could not
        # have placed. The first version of this module read that line and
        # told the owner, in the present tense, that his broker credentials
        # were broken. Every bullet now carries when the fault last happened
        # (see `_bullet`) instead of asserting a present state the log cannot
        # support.
        sentence=(
            "The desk found a fill-this-in stand-in where a real broker key should be, and said so {n} time{plural}"
        ),
        reason=PROVIDER_REJECTED_US,
        # The specific alarm text, from the two places that raise it — NOT the
        # word "placeholder", which also appears in a routine healthy line
        # about earnings analyses ("0 unanalyzed placeholders", 26 times on
        # 2026-09-18 alone) that has nothing to do with credentials.
        patterns=_p(r"Placeholder trading credential in use", r"PLACEHOLDER CREDENTIAL"),
        # A successful authenticated write to the broker AFTER the alarm is
        # proof the key works now, and it is the only proof available: the
        # alarm is rate-limited to once a day, so waiting for it to stop
        # firing tells you nothing. On 2026-09-18 it fired at 09:00 ET, the
        # owner wrote the real keys at 09:03 and 09:04, and the desk placed
        # protective stops at the broker from 09:30 — which a stand-in key
        # cannot do. Before this rule existed, that morning was reported to
        # him as broken credentials.
        resolved_by=_p(
            r"entry protection:.* placed for",
            r"COVERAGE REPAIRED:",
        ),
        # Item 86 was retired on live evidence; nothing on the board tracks
        # this family now. The reporting is unchanged — only the claim goes.
        board_item=None,
    ),
    FaultFamily(
        key="news_source_dead",
        short_name="a news source that answers nothing",
        sentence=("One of the desk's news sources has answered nothing every single time it has been asked"),
        reason=SILENTLY_FAILING,
        patterns=_p(r"^Feed .* fetch failed"),
        measure_duration=True,
    ),
    FaultFamily(
        key="economics_feed_incomplete",
        short_name="economic readings the desk could not get",
        sentence=("The desk went ahead without economic readings it had asked for on {n} occasion{plural}"),
        reason=SILENTLY_FAILING,
        patterns=_p(
            r"FRED fetch deadline",
            r"Macro event calendar PARTIAL",
            # `FAILED:` is required. Without it this matched the macro seat's
            # own PROMPT, which prints "Macro coverage: 15/15 FRED series
            # returned data. Full coverage" — a healthy line, counted as a
            # fault eight times in the retained logs before this was caught.
            r"Macro coverage: \d+/\d+ FRED series.*FAILED:",
        ),
        # Item 119 retired 2026-10-01; the FRED-fetch reliability work it
        # named now lives under item 187, so this family points there.
        board_item=187,
        measure_duration=True,
    ),
    # The congressional trading-disclosure feed. Unlike every family above,
    # these patterns were NOT read off production logs: the feed has never
    # run in production (switch off as of 2026-09-19). They are the feed's
    # own format strings, and `tests/test_congressional_incremental.py`
    # drives the real code into each failure and classifies the line it
    # actually emits, so a rewording fails a test instead of going quiet.
    # Duration is measured because the feed fails open to its saved copy:
    # a source that has been down for a week looks exactly like a quiet one.
    FaultFamily(
        key="insider_filing_read_incomplete",
        short_name="insider-filing read not finished",
        sentence=(
            "The morning read of insider filings did not finish on {n} "
            "occasion{plural}, so the desk decided with incomplete insider "
            "evidence; the owner was told at the time"
        ),
        # Same footing as the economic readings and congressional
        # disclosures the desk goes ahead without: the desk keeps working,
        # the evidence is short. Real line, 20 Oct 2026 08:02 ET, ERROR:
        # "PRE-OPEN: Insider-filing check did not finish this morning: ...".
        # It was DEGRADED under "faults this health check does not
        # recognise" until this family existed.
        reason=SILENTLY_FAILING,
        patterns=_p(
            r"^PRE-OPEN: Insider-filing check did not finish this morning",
            r"^PRE-OPEN: All \d+ companies the desk watches had every insider filing read",
            r"^PRE-OPEN: Form 4 backlog",
            r"Insider-filing coverage this morning",
        ),
    ),
    FaultFamily(
        key="congress_source_unreachable",
        short_name="a congressional trading source that could not be reached",
        sentence=("A public source of congressional trading disclosures could not be reached on {n} occasion{plural}"),
        reason=SILENTLY_FAILING,
        patterns=_p(r"^Congressional source unreachable: source=\w+"),
        subject=re.compile(r"source=(\w+)"),
        resolved_by=_CONGRESS_SOURCE_RECOVERED,
        measure_duration=True,
    ),
    FaultFamily(
        key="congress_source_unreadable",
        short_name="a congressional trading source that sent back nonsense",
        sentence=(
            "A public source of congressional trading disclosures answered with "
            "something the desk could not read on {n} occasion{plural}"
        ),
        reason=SILENTLY_FAILING,
        patterns=_p(r"^Congressional source unreadable: source=\w+"),
        subject=re.compile(r"source=(\w+)"),
        resolved_by=_CONGRESS_SOURCE_RECOVERED,
        measure_duration=True,
    ),
    FaultFamily(
        key="congress_cache_stale",
        short_name="old congressional trading disclosures used as if current",
        sentence=(
            "The desk read an old saved copy of congressional trading "
            "disclosures because no fresh one could be had, on {n} "
            "occasion{plural}"
        ),
        reason=SILENTLY_FAILING,
        patterns=_p(r"^Congressional cache served stale: source=\w+"),
        subject=re.compile(r"source=(\w+)"),
        resolved_by=_CONGRESS_SOURCE_RECOVERED,
        measure_duration=True,
    ),
    FaultFamily(
        key="unrecognised_faults",
        short_name="faults this health check does not recognise",
        sentence=(
            "The desk recorded {n} fault{plural} of a kind this health check has never seen before and cannot describe"
        ),
        reason=SILENTLY_FAILING,
        patterns=(),  # populated by exclusion — see `analyse`
    ),
    # --- informational from here down: correct behaviour, never a bullet ----
    FaultFamily(
        key="safety_check_refused_idea",
        short_name="trade ideas the safety check refused",
        sentence=(
            "The safety check refused {n} trade idea{plural} that contradicted the chart reading; it worked as intended"
        ),
        reason=HANDLED,
        patterns=_p(r"failed deterministic grounding"),
    ),
    FaultFamily(
        key="records_disagree_with_broker",
        short_name="holdings where the desk's records and the broker disagree",
        sentence=(
            "The desk's records and the broker disagreed on a holding {n} time{plural}; "
            "in the worst case the broker holds more than the desk recorded"
        ),
        reason=SILENTLY_FAILING,
        patterns=_p(r"stop-out reconcile: .*stop_out_(?:pnl_unmatched|ledger_short)"),
    ),
    FaultFamily(
        key="order_blocked_by_a_rule",
        short_name="orders a rule stopped before they reached the market",
        sentence="{n} order{plural} was stopped by a rule before it was placed",
        reason=HANDLED,
        # Both kinds observed in the retained logs are rules doing their job,
        # not faults: the broker refusing an order that would trade against
        # the desk's own resting order, and the desk's own fat-finger guard
        # refusing a stop price far off the reference. Neither cost a
        # decision, neither left money unprotected, and neither is something
        # to wake the owner for. An order failure of a kind NOT listed here
        # still reaches him — it falls into the unrecognised bucket, which is
        # the whole point of that bucket.
        patterns=_p(
            r"^Order failed for ",
            r"broker returned no order id",
            r"rejected_outlier",
        ),
    ),
    FaultFamily(
        key="desk_declined_to_trade",
        short_name="trades the desk chose not to make",
        sentence="The desk decided against {n} trade{plural} for a stated reason",
        reason=HANDLED,
        patterns=_p(
            r"Constructor: (BUY|SELL) \S+ refused",
            r"NOT SUBMITTED",
            r"Deterministic gate: BLOCKED",
            r"Fat-finger guard",
        ),
    ),
    FaultFamily(
        key="price_rows_unusable",
        short_name="price rows that arrived damaged",
        sentence=("The price service sent {n} damaged reading{plural}, which the desk spotted and discarded"),
        reason=HANDLED,
        patterns=_p(
            r"NaN OHLCV",
            r"failed to resolve even after retry",
        ),
    ),
    FaultFamily(
        key="model_bill_reconciled",
        short_name="research bills that differed from the estimate",
        sentence=("The research bill differed from the estimate {n} time{plural} and the real figure was used"),
        reason=HANDLED,
        patterns=_p(r"provider-reported cost \$"),
    ),
    FaultFamily(
        key="optional_fields_defaulted",
        short_name="optional details left blank in an answer",
        sentence=("{n} answer{plural} arrived with an optional detail left blank and were kept"),
        reason=HANDLED,
        patterns=_p(
            r"dropped explicit null/empty on defaulted field",
            r"schema could not be made strict-compatible",
            r"token-rate governor held a request",
            r"accession peek truncated",
            r"redacted and re-saved cached analysis",
            r"asserts price-derived valuation",
            r"low conviction",
            r"in-session price unavailable",
            r"cash sweep: funding sell",
            r"STOP-OUT RECORDED",
            r"COVERAGE REPAIRED",
            r"coverage repair: \S+ has no trade print",
            r"Agent \w+ attempt \d+ failed",
            # A short first answer and the retry launched to fix it. The
            # failure, if there is one, is logged separately when the retry
            # gives up; see `names_dropped_from_answer`.
            r"missing-from-response=\[",
            r"Tech batch incomplete across",
            # One broken row in the seat's answer, dropped on its own while
            # every well-formed row beside it is kept. Handled here for the
            # same reason as the short answer above: if the retry does not
            # recover the name, the loss is logged as `unresolved after retry`.
            r"Tech answer carried \d+ malformed row",
            # Handled here, not under `names_dropped_from_answer`, because it
            # is the SAME loss restated a layer up, not a second one: every
            # run where `src.pipeline_stages` logs "Tech batch partial: X/Y
            # symbols resolved, N failed even after retry" (or, on a total
            # loss, "Tech batch: all N submitted symbol(s) failed even after
            # retry"), `src.agents.tech_analyst` has already logged its own
            # "... unresolved after ..." line naming the same symbols, which
            # IS counted there. Counting both would report one lost batch as
            # two.
            r"Tech batch partial",
            r"Tech batch: all \d+ submitted symbol\(s\) failed",
            # A packing-arithmetic fallback the log line's own text says is
            # inert ("Analysis is unaffected — this only changes how the
            # batch is divided"): real production line, verified WARNING
            # level, "Tech batch: could not size the request set; falling
            # back to fixed N-symbol chunks."
            r"could not size the request set",
        ),
    ),
)

_FAMILY_BY_KEY = {f.key: f for f in FAMILIES}
UNRECOGNISED_KEY = "unrecognised_faults"


@dataclass(frozen=True)
class LogRecord:
    timestamp: datetime
    level: str
    source: str
    message: str


@dataclass
class Finding:
    family: FaultFamily
    count: int = 0
    first_seen: datetime | None = None
    last_seen: datetime | None = None
    #: Earliest occurrence anywhere in the retained history, for the families
    #: whose severity is a function of how long they have been failing.
    first_seen_ever: datetime | None = None
    #: Dollars uncovered per the most recent matching line, when it states them.
    dollars_uncovered: float | None = None

    @property
    def reportable(self) -> bool:
        return self.family.reason is not None and self.count > 0


@dataclass
class Report:
    window_start: datetime
    window_end: datetime
    findings: list[Finding] = field(default_factory=list)
    #: Keys reported in the previous report, so an unchanged issue can be
    #: compressed instead of restated in full.
    previous: dict[str, int] = field(default_factory=dict)
    #: Time of the last log line read in the window. "none since" on a bullet
    #: is measured against this, never against a deploy time.
    log_read_to: datetime | None = None

    @property
    def reported(self) -> list[Finding]:
        """Everything that meets the bar, worst open condition first; owner-
        accepted exposures after every open fault."""
        return sorted(
            (f for f in self.findings if f.reportable),
            key=lambda f: (bool(f.family.accepted_on), _REASON_ORDER.index(f.family.reason), -f.count),
        )

    @property
    def verdict(self) -> str:
        reasons = {f.family.reason for f in self.reported if not f.family.accepted_on}
        if reasons & _HURT_REASONS:
            return "hurt"
        if reasons:
            return "degraded"
        if self.reported:
            return "accepted"
        return "healthy"


# --- reading the log --------------------------------------------------------


def log_files(log_dir: Path | None = None) -> list[Path]:
    """The retained log history, OLDEST FIRST for reading.

    A path to a single file is accepted and returned as-is, so a test can point
    the whole analyser at one fixture without the fixture having to impersonate
    a production rotation scheme.
    """
    base = Path(log_dir or DEFAULT_LOG_DIR)
    if base.is_file():
        return [base]
    paths = [base / LOG_BASENAME]
    paths += [base / f"{LOG_BASENAME}.{i}" for i in range(1, 6)]
    existing = [p for p in paths if p.exists()]
    # `.5` is the oldest rotation, the bare name the newest.
    return sorted(existing, key=lambda p: p.suffix, reverse=True)


def parse_records(path: Path) -> list[LogRecord]:
    """Parse one log file. Continuation lines (a multi-line owner alert, a
    stack trace) are appended to the record they belong to rather than dropped,
    because the line that names the fault is often the second one."""
    records: list[LogRecord] = []
    try:
        text = path.read_text(errors="replace")
    except OSError as exc:  # unreadable rotation — never fatal
        logger.warning("log-health: could not read %s: %s", path.name, exc)
        return records
    for line in text.splitlines():
        match = _LINE_RE.match(line)
        if match:
            records.append(
                LogRecord(
                    timestamp=datetime.strptime(match.group("ts"), "%Y-%m-%d %H:%M:%S").replace(tzinfo=LOG_TZ),
                    level=match.group("level"),
                    source=match.group("logger"),
                    message=match.group("msg"),
                )
            )
        elif records:
            prev = records[-1]
            records[-1] = LogRecord(prev.timestamp, prev.level, prev.source, prev.message + "\n" + line)
    return records


def read_window(start: datetime, end: datetime, log_dir: Path | None = None) -> list[LogRecord]:
    """Every record with `start < timestamp <= end`.

    The bounds are half-open at the start ON PURPOSE: the watermark stores the
    timestamp of the last record consumed, so a strict `>` is what makes two
    consecutive reports neither skip a second nor count one twice.
    """
    out: list[LogRecord] = []
    for path in log_files(log_dir):
        records = parse_records(path)
        if not records:
            continue
        if records[-1].timestamp <= start:
            continue  # whole rotation predates the window
        out.extend(r for r in records if start < r.timestamp <= end)
    out.sort(key=lambda r: r.timestamp)
    return out


def _is_owner_alert_echo(record: LogRecord) -> bool:
    """True for the funnel's own CRITICAL line announcing an owner report.

    An owner report is not a fault: a real condition is counted where it is
    logged, and routine reports (half-hourly safety check, morning/midday/close
    reviews) are healthy. The source is matched as the `src.notifier` package,
    because the 1 Oct split (#1037) made the logger names `src.notifier.base`,
    `src.notifier.owner_alert_funnel` and so on.
    """
    in_notifier = record.source == "src.notifier" or record.source.startswith("src.notifier.")
    return in_notifier and record.message.startswith("OWNER ALERT")


def classify(message: str, level: str | None = None) -> FaultFamily | None:
    """Which family this line belongs to, if any.

    `level` is not optional in practice — every caller inside this module
    passes it — and it is what stops a reportable family firing on a healthy
    line. A REPORTABLE family only ever matches a line the desk itself logged
    as WARNING or worse. The defect that forced this: `Macro coverage: N/M
    FRED series` also appears inside the macro seat's own prompt, at INFO,
    stating FULL coverage, and was counted as a fault eight times. Text alone
    is not evidence that something went wrong; the desk's own severity is.

    Informational families keep matching at any level, because their whole
    job is to account for ordinary lines so they do not fall into the
    fail-closed bucket.
    """
    for family in FAMILIES:
        if family.reason is not None and level is not None and level not in _SERIOUS_LEVELS and level != "WARNING":
            continue
        for pattern in family.patterns:
            if pattern.search(message):
                return family
    return None


def _subjects(pattern: re.Pattern[str] | None, message: str) -> set[str]:
    """Every distinct thing (ticker) a line is about, via `pattern`'s group 1."""
    if pattern is None:
        return set()
    return {m.group(1) for m in pattern.finditer(message)}


def _resolved_subjects(family: FaultFamily, records: list[LogRecord], after: datetime) -> set[str]:
    out: set[str] = set()
    for record in records:
        if record.timestamp < after:
            continue
        for pattern in family.resolved_by:
            for match in pattern.finditer(record.message):
                out.add(match.group(1) if match.groups() else "")
    return out


def _is_resolved(family: FaultFamily, record: LogRecord, records: list[LogRecord]) -> bool:
    """Was this occurrence put right later in the same window?

    Matched PER SUBJECT where the family names one — nine stops missing and
    seven repaired is two still missing, not nine. Where the family names no
    subject (the overnight sub-share exposure covers the whole book at once),
    any later resolving line clears it.
    """
    subjects = _subjects(family.subject, record.message)
    cleared = _resolved_subjects(family, records, record.timestamp)
    if not subjects:
        return bool(cleared)
    return subjects.issubset(cleared)


def analyse(
    records: list[LogRecord],
    window_start: datetime,
    window_end: datetime,
    log_dir: Path | None = None,
    previous: dict[str, int] | None = None,
) -> Report:
    findings: dict[str, Finding] = {}
    resolved: dict[str, int] = {}

    def bump(family: FaultFamily, ts: datetime, message: str = "") -> None:
        finding = findings.get(family.key)
        if finding is None:
            finding = Finding(family=family)
            findings[family.key] = finding
        finding.count += 1
        if finding.first_seen is None or ts < finding.first_seen:
            finding.first_seen = ts
        if finding.last_seen is None or ts > finding.last_seen:
            finding.last_seen = ts
            if family.dollars is not None:
                m = family.dollars.search(message)
                if m:
                    finding.dollars_uncovered = float(m.group(1).replace(",", ""))

    for record in records:
        if _is_owner_alert_echo(record):
            # An owner alert is a RESTATEMENT of a condition that was already
            # logged where it happened — the evidence gate writes its own line
            # and then alerts; the credential check does the same. Counting
            # both would tell the owner five lost decisions were ten.
            #
            # Every echo is skipped, whether or not its body classifies: a
            # routine report ("nothing to report") never classifies and is
            # not a fault. Delivery failures are counted at their own site.
            continue
        family = classify(record.message, record.level)
        if family is not None:
            if family.resolved_by and _is_resolved(family, record, records):
                # The desk found this and fixed it inside the window. A repair
                # that worked is not a fault; carrying it as one is how a
                # self-healed morning became a HURT verdict.
                resolved[family.key] = resolved.get(family.key, 0) + 1
                continue
            bump(family, record.timestamp, record.message)
        elif record.level in _SERIOUS_LEVELS:
            # Fail-closed: the desk itself called this a fault, and this module
            # has no wording for it. Saying nothing would be the one outcome
            # the report exists to prevent.
            bump(_FAMILY_BY_KEY[UNRECOGNISED_KEY], record.timestamp)

    for finding in findings.values():
        if finding.family.measure_duration and finding.count:
            finding.first_seen_ever = _first_occurrence(finding.family, log_dir)
        if finding.family.count_from_sweep and finding.last_seen is not None:
            run = _sweep_still_open(records, finding.last_seen)
            if run is not None:
                # The sweep's run line is the latest evidence the condition
                # was true, so "last at" is that run, not the evening line.
                finding.count, finding.last_seen = run

    return Report(
        window_start=window_start,
        window_end=window_end,
        findings=list(findings.values()),
        previous=dict(previous or {}),
        log_read_to=max((r.timestamp for r in records), default=None),
    )


#: The coverage sweep's own run line (`sweep_log_line`): "<name> <run> (<entry>):
#: <outcome> — positions checked P, gaps F found / N still open, ...".
_SWEEP_RUN_RE = re.compile(
    re.escape(SWEEP_LOG_NAME) + r" \S+ \([^)]*\): (?P<outcome>\w+) — positions checked \d+, "
    r"gaps \d+ found / (?P<open>\d+) still open"
)


def _sweep_still_open(records: list[LogRecord], since: datetime) -> tuple[int, datetime] | None:
    """Holdings the latest sweep run at or after `since` found still open, and
    when that run logged it.

    A run that could not reach the broker read nothing, so its zero is not a
    count. None when no sweep run speaks for the moment; the caller then keeps
    the count it already has.
    """
    for record in reversed(records):
        if record.timestamp < since:
            return None
        match = _SWEEP_RUN_RE.search(record.message)
        if match and match.group("outcome") != "could_not_check":
            return int(match.group("open")), record.timestamp
    return None


def _first_occurrence(family: FaultFamily, log_dir: Path | None) -> datetime | None:
    """Earliest time this family appears anywhere in the retained history.

    Only called for the families whose severity is duration-based, so the cost
    of reading the rotations is paid only when it buys a measured number the
    owner can act on ("since the middle of August") rather than an impression.
    """
    for path in log_files(log_dir):
        for record in parse_records(path):
            if classify(record.message, record.level) is family:
                return record.timestamp
    return None


# --- the watermark ----------------------------------------------------------


def load_state(path: Path | None = None) -> dict:
    state_path = Path(path or DEFAULT_STATE_PATH)
    try:
        return json.loads(state_path.read_text())
    except (OSError, ValueError):
        return {}


def save_state(report: Report, path: Path | None = None) -> None:
    """Persist the end of the window and what was reported in it.

    `through` is the exclusive lower bound of the NEXT window. It is the end of
    the window just analysed, not "now" — so a report that ran while the desk
    was mid-session cannot skip the lines written during its own run.
    """
    state_path = Path(path or DEFAULT_STATE_PATH)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(
        json.dumps(
            {
                "through": report.window_end.astimezone(LOG_TZ).strftime("%Y-%m-%d %H:%M:%S"),
                "reported": {f.family.key: f.count for f in report.reported},
            },
            indent=2,
        )
        + "\n"
    )


def window_from_state(now: datetime, state: dict, default_lookback_hours: int = 24) -> tuple[datetime, datetime]:
    """Where the last report stopped, to now.

    The fallback matters only on the very first run, before a watermark exists.
    24 hours is not a judgement about what is interesting — it is one turn of
    the desk's own daily cycle, so the first report covers exactly one of
    everything the schedule does and nothing is quietly excluded.
    """
    raw = str(state.get("through") or "")
    try:
        start = datetime.strptime(raw, "%Y-%m-%d %H:%M:%S").replace(tzinfo=LOG_TZ)
    except ValueError:
        start = now - timedelta(hours=default_lookback_hours)
    if start >= now:
        start = now - timedelta(hours=default_lookback_hours)
    return start, now


# --- the board --------------------------------------------------------------


def board_state(work_md: Path | None = None) -> tuple[set[int], set[int]]:
    """(item numbers on the board, item numbers in the priority tiers).

    READ ONLY. The disposition on every bullet has to come from somewhere real:
    an item that is on the board is tracked, an item in the priority tiers is
    being worked on now, and a family with neither gets told the truth — that
    it is new and nobody has picked it up. Inventing a plan per run would let
    the message claim work is happening that is not.
    """
    path = Path(work_md or PROJECT_ROOT / "docs" / "WORK.md")
    try:
        text = path.read_text()
    except OSError:
        return set(), set()
    on_board = {int(m) for m in re.findall(r"^\*\*(\d+)[.:]", text, re.MULTILINE)}
    in_tiers: set[int] = set()
    for line in text.splitlines():
        if line.startswith("- **Tier "):
            in_tiers.update(int(m) for m in re.findall(r"\b(\d{1,3})\b", line))
    return on_board, in_tiers


def disposition(finding: Finding, on_board: set[int], in_tiers: set[int], is_new: bool) -> str:
    item = finding.family.board_item
    if item is not None and item in in_tiers:
        return "being worked on"
    if item is not None and item in on_board:
        return "on the list"
    if finding.family.reason is None:
        return "watched and left alone"
    if finding.family.accepted_on:
        day = datetime.fromisoformat(finding.family.accepted_on)
        text = f"known, accepted {day.day} {day:%b}"
        if finding.dollars_uncovered is not None:
            text += f", ${finding.dollars_uncovered:,.2f} uncovered"
        return text
    # No board item means nobody has picked this up. Saying anything warmer
    # would let the message claim work is happening that is not.
    return "new — not yet on the list" if is_new else "still not on the list"


# --- the message ------------------------------------------------------------

#: Telegram's own published per-message ceiling is 4,096 characters for the
#: `text` field of `sendMessage`. `TelegramNotifier.MAX_MESSAGE_CHARS` is this
#: desk's already-measured working budget beneath that (it leaves room for the
#: tap-through link the notifier appends). It is read from there rather than
#: restated, so there is exactly one copy of the only real limit in play.
MESSAGE_BUDGET = TelegramNotifier.MAX_MESSAGE_CHARS

_VERDICT_WORDS = {
    "healthy": "HEALTHY",
    "degraded": "DEGRADED",
    "hurt": "HURT",
    "accepted": "KNOWN, ACCEPTED",
}


def _window_words(report: Report) -> str:
    # Every owner-facing time carries its date (item 231): he reads this hours
    # later, so a bare "today" cannot be placed. The one shared formatter is
    # used for both ends, in the owner's local (Eastern) zone, never UTC.
    start = report.window_start.astimezone(OWNER_TZ)
    end = report.window_end.astimezone(OWNER_TZ)
    return f"{fmt_time_12h(start)} to {fmt_time_12h(end)}"


def _bullet(finding: Finding, report: Report, on_board: set[int], in_tiers: set[int]) -> str:
    sentence = finding.family.sentence.format(n=finding.count, plural=_plural(finding.count))
    # The duration clause earns its place only when the fault PREDATES this
    # report — that is what makes "it has been like this since August" worth
    # a phone screen. For something that started inside the window, the
    # "last at" clause below already says everything the log supports.
    if finding.family.measure_duration and finding.first_seen_ever and finding.first_seen_ever < report.window_start:
        sentence += f", {_duration_words(finding.first_seen_ever, report.window_end)}"
    # WHEN IT LAST HAPPENED, on every bullet. A log line is evidence that
    # something happened at a moment, never evidence that it is still true —
    # and several of these alarms are rate-limited to once a day, so their
    # absence proves nothing either. Stating the time is the only claim the
    # log actually supports; asserting a present state from a past line is
    # what told the owner his broker credentials were broken three minutes
    # after he had fixed them.
    if finding.last_seen:
        sentence += f" ({_last_at_words(finding.last_seen, report.log_read_to)})"
    is_new = finding.family.key not in report.previous
    return f"• {sentence} — {disposition(finding, on_board, in_tiers, is_new)}."


def _verdict_line(report: Report) -> str:
    word = _VERDICT_WORDS[report.verdict]
    if report.verdict == "healthy":
        return f"{word} — nothing cost the desk a decision or left a holding unprotected."
    worst = report.reported[0]
    return f"{word} — {worst.family.short_name}."


def render(report: Report, work_md: Path | None = None) -> list[str]:
    """The message(s). One normally; two only when the truth does not fit.

    The shape is the one already ratified for this desk's owner-facing
    messages (`src/trader_feed.py`): a bold title line carrying the conclusion,
    then short bullets, one sentence each. Nothing that worked gets a line.
    """
    on_board, in_tiers = board_state(work_md)
    title = f"<b>Desk health — {_window_words(report)}</b>"

    if report.verdict == "healthy":
        # Two lines, as promised. A healthy report still goes out: its absence
        # would be indistinguishable from the job not running.
        return [f"{title}\n{_verdict_line(report)}"]

    head = [title, _verdict_line(report), ""]
    entries: list[tuple[Finding, str]] = []
    for finding in report.reported:
        was = report.previous.get(finding.family.key)
        if was is None or finding.count > was:
            entries.append((finding, _bullet(finding, report, on_board, in_tiers)))
        else:
            # Unchanged since the last report: one short line, not the whole
            # bullet again. Restating an issue verbatim every twelve hours is
            # how a channel stops being read.
            # The time goes on this line too: on 2026-10-13 "Still true" stood
            # alone over 162 faults that had ended five hours earlier.
            when = f" ({_last_at_words(finding.last_seen, report.log_read_to)})" if finding.last_seen else ""
            entries.append((finding, f"• Still true: {finding.family.short_name}{when}."))

    # New issues first, then the "still true" ones — a line he has already read
    # should never sit above one he has not.
    entries.sort(key=lambda e: e[1].startswith("• Still true:"))
    body = [line for _, line in entries]
    message = "\n".join(head + body)
    if len(message) <= MESSAGE_BUDGET:
        return [message]

    # Too long for one message. Nothing is dropped — the findings are packed
    # into as many messages as they need, most serious first, so the message he
    # opens carries the worst of it. Dropping a real fault to fit a length
    # would reproduce the exact "looks quiet but is not" failure this report
    # exists to prevent.
    ordered = [line for f, line in entries if _is_open_hurt(f)]
    ordered += [line for f, line in entries if not _is_open_hurt(f)]
    return _pack(head, ordered, title)


def _is_open_hurt(finding: Finding) -> bool:
    return finding.family.reason in _HURT_REASONS and not finding.family.accepted_on


def _pack(head: list[str], bullets: list[str], title: str) -> list[str]:
    """Split `bullets` across as few messages as will hold them all."""
    messages: list[str] = []
    current_head = head
    current: list[str] = []
    for bullet in bullets:
        candidate = "\n".join(current_head + current + [bullet])
        if current and len(candidate) > MESSAGE_BUDGET:
            messages.append("\n".join(current_head + current))
            current_head = [f"{title} — continued", ""]
            current = [bullet]
        else:
            current.append(bullet)
    if current:
        messages.append("\n".join(current_head + current))
    return messages


def build_report(
    now: datetime | None = None,
    log_dir: Path | None = None,
    state_path: Path | None = None,
) -> Report:
    now = (now or utc_now()).astimezone(LOG_TZ).replace(microsecond=0)
    state = load_state(state_path)
    start, end = window_from_state(now, state)
    records = read_window(start, end, log_dir)
    return analyse(records, start, end, log_dir=log_dir, previous=state.get("reported"))
