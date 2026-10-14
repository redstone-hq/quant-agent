"""Shared constants, ProbeResult, text clipping and malformed-number redaction.

Moved verbatim from the former src/notifier.py; the package re-exports it.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from src.data_paths import db_path

logger = logging.getLogger(__name__)

# Default DB path, anchored to the project root rather than CWD. The
# notifier is invoked both from launchd/systemd (which set the project
# root as WorkingDirectory) and from manual `python /abs/path/main.py`
# from somewhere else — the latter used to silently miss the cost line
# and position snapshot because `Path("data/...")` resolved relative to
# the caller's CWD.
#: Set by the rehearsal harness (`ops/rehearsal/`). When true, no operator
#: alert leaves this process — see `TelegramNotifier.send`. It is an env var
#: rather than config because it must hold for any code path that builds a
#: notifier, including ones that construct their own from `.env` directly.
_REHEARSAL_MODE = os.environ.get("QAMC_REHEARSAL") == "1"


_DB_PATH = db_path()


@dataclass(frozen=True)
class ProbeResult:
    """Outcome of `TelegramNotifier.probe()` — did the alert channel work.

    `stage` names the first thing that failed, because the three failures
    need three different repairs and "the alert didn't send" does not tell
    an operator which one he has:

      credentials — the process has no TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID
                    (or TELEGRAM_DISABLED is set). Fix the unit's env, not
                    the network.
      transport   — the POST never completed: DNS, TLS, proxy, an egress
                    rule, a timeout. Fix the box's outbound path.
      api         — Telegram answered and refused: revoked token (401),
                    wrong or deleted chat id (400 "chat not found"), bot
                    blocked by the user (403). Fix the credential or the
                    chat.
      delivered   — the message really went out. ok=True.

    `residue` means the send worked but the tidy-up delete did not, so one
    self-describing probe message is sitting in the operator's chat. Not a
    failure of the alert channel — the channel demonstrably works — but
    worth saying so nobody wonders what the stray message was.
    """

    ok: bool
    stage: str
    detail: str = ""
    residue: bool = False

    def summary(self) -> str:
        verdict = "alert channel PROVED" if self.ok else "alert channel BROKEN"
        line = f"{verdict} (stage={self.stage})"
        if self.detail:
            line += f": {self.detail}"
        if self.residue:
            line += " [probe message could not be deleted; it stays in the chat]"
        return line


# Cash-sweep parking vehicles — cash equivalents, never "deployed capital".
# The notifier reads the DB directly (it deliberately doesn't thread config
# in — see the comment at the sqlite3 connect), so it can't ask
# CashSweepConfig for the configured symbol. Cover the supported vehicles;
# an unknown custom symbol degrades to today's behaviour (counted as a
# position), which is visible rather than silent.
_SWEEP_SYMBOLS = frozenset({"SGOV", "BIL"})


def _clip_text(text: str, max_chars: int, marker: str = " …") -> str:
    """Shorten `text` to at most `max_chars`, cutting on a sentence or word
    boundary and appending `marker` — never a hard mid-word chop.

    The bug this replaces: `text[:N]` throughout this module (and
    src/trader_feed.py's own `_clip`) sliced on a raw character count with
    no regard for what was at that boundary. The operator's actual report
    was a BUY CRM alert whose rationale read "...strong heavy accumulation
    volume" and simply stopped — no ellipsis, no "see more", nothing to
    indicate the sentence had been cut at all, well below Telegram's real
    4096-char message limit.

    Preference order: the last '. '/'! '/'? ' inside the budget (reads as a
    complete thought); then the last whitespace (never split a word); a
    hard cut only when the text has no boundary at all within the budget
    (e.g. one unbroken token) — the single case this still can't avoid.
    """
    if max_chars <= 0:
        return ""
    if len(text) <= max_chars:
        return text
    if max_chars <= len(marker):
        return text[:max_chars]
    budget = max_chars - len(marker)
    window = text[:budget]

    best = -1
    for punct in (". ", "! ", "? "):
        idx = window.rfind(punct)
        if idx > best:
            best = idx
    # Trust a sentence boundary only if it doesn't throw away most of the
    # budget (an early ". " — an abbreviation, a list separator — would
    # otherwise clip far more aggressively than max_chars intends).
    if best >= budget * 0.4:
        return window[: best + 1].rstrip() + marker

    space = window.rfind(" ")
    if space > 0:
        return window[:space].rstrip() + marker

    return window.rstrip() + marker


#: Neutral stand-in for a numeric token this guard judged structurally
#: impossible (see `_find_malformed_numeric_tokens` below). Never a guessed
#: value — the desk rule is "report the true state, never fabricate", and a
#: plausible-looking replacement would itself be a fabricated number.
_MALFORMED_NUMBER_MARKER = "[number garbled — removed]"

# Scope note, so this is never confused with the REJECTED "prompt-text
# number scanner" (docs/board_notes/ item 99): that idea was scanning
# ~1,825 numeric tokens inside PROMPT INPUT (config/prompts/*.md) — mostly
# dates and list numbering, hopeless signal-to-noise, and explicitly not
# built. This is the opposite direction: it scans the LLM's OUTPUT prose
# where that prose enters an owner message (see `_redact_malformed_numbers`),
# looking only for a small number of STRUCTURALLY IMPOSSIBLE shapes, not
# "suspicious" numbers in general. It is deliberately narrow to keep the
# false-positive rate at zero on real desk language: percentages ("14.6%"),
# multipliers ("2.0x"), day ranges ("5-15"), shorthand money ("$10M"), ISO
# dates ("2026-09-24"), times ("12:51"), and share counts ("0.3155") must
# never be touched.
# A comma only counts as part of a number when a digit follows it, so a
# number can never END in a comma: in "stop covers 3, $164.61" the "3," is
# the number 3 and a list comma, not a thousands group with no digits.
_NUMERIC_TOKEN_RE = re.compile(r"-?\$?\d+(?:,\d+)*(?:\.\d+)*")


def _malformed_numeric_reason(token: str) -> str | None:
    """Return why `token` (as matched by `_NUMERIC_TOKEN_RE`) is
    structurally impossible as a number, or None if it's fine.

    Deliberately conservative: only shapes that can NEVER be a correctly
    written figure are flagged. Three checks, all direct restatements of
    the bug report ("$10,21.36") and its named siblings:

    1. More than one decimal point in one token ("12.34.56") — no numeric
       convention has two, so this is never valid.
    2. A comma-grouped integer part where a group isn't exactly 3 digits
       ("10,21" — the second group is 2 digits, not 3) — this is the exact
       shape of the reported defect. A LEADING group of 1-3 digits is fine
       ("1,021"); every group after the first must be exactly 3.
    3. A `$` amount that has a decimal point but not exactly 2 digits after
       it ("$10.5", "$10.567") — dollars-and-cents has exactly one valid
       decimal length; percentages, multipliers and share counts are NOT
       constrained this way because they aren't `$` amounts.
    """
    body = token[1:] if token.startswith("-") else token
    is_money = body.startswith("$")
    if is_money:
        body = body[1:]
    parts = body.split(".")
    if len(parts) > 2:
        return "multiple decimal points"
    integer_part = parts[0]
    decimal_part = parts[1] if len(parts) == 2 else None

    if "," in integer_part:
        groups = integer_part.split(",")
        if not groups[0] or not (1 <= len(groups[0]) <= 3) or not groups[0].isdigit():
            return "malformed thousands separator"
        for group in groups[1:]:
            if len(group) != 3 or not group.isdigit():
                return "malformed thousands separator"

    if is_money and decimal_part is not None and len(decimal_part) != 2:
        return "dollar amount without exactly 2 decimal places"

    return None


def _find_malformed_numeric_tokens(text: str) -> list[tuple[str, str]]:
    """Scan `text` for numeric tokens that are structurally impossible.

    Returns a list of (token, reason) pairs. Pure function, no I/O — see
    `_redact_malformed_numbers` for the caller that acts on the result.
    """
    found = []
    for match in _NUMERIC_TOKEN_RE.finditer(text):
        reason = _malformed_numeric_reason(match.group(0))
        if reason is not None:
            found.append((match.group(0), reason))
    return found


def _redact_malformed_numbers(text: str) -> str:
    """Replace any structurally-impossible numeric token in `text` with a
    neutral marker, so a garbled figure (e.g. "$10,21.36") can never reach
    the owner as if it were real.

    Why redact instead of anything cleverer: the desk rule (docs, 2026-09-23)
    is that a false or fabricated owner-facing number is a defect regardless
    of intent, so guessing the intended value is not on the table — this
    guard has no way to know whether "$10,21.36" meant $10.21, $1,021.36, or
    something else. Dropping the number and saying so is the only action
    that never ships a false figure. The underlying cause (whatever
    produced the malformed text) is logged here so it stays visible instead
    of silently disappearing into a redaction.

    Where it runs: ONLY where model-written prose enters an owner message,
    never on the whole outgoing message — the send chokepoint cannot tell
    desk-formatted text from model prose, and running there garbled real
    desk figures. Current insertion points: `src/trader_feed/common.py`'s
    `_clip` (every PM / risk / evening-review / thesis field the trader
    feed renders). Model prose in `src/notifier/evening.py` (suggested
    actions, tomorrow's outlook) is not yet routed through it.

    Never raises: this runs on owner-facing text, so a bug in the
    guard itself must not be able to block a real alert. On any fault the
    original text goes out unredacted — the guard failing open, not the
    channel failing closed, matches every other best-effort contract in
    this module (see `send()`'s own docstring).
    """
    try:
        found = _find_malformed_numeric_tokens(text)
        if not found:
            return text

        def _replace(match: "re.Match[str]") -> str:
            reason = _malformed_numeric_reason(match.group(0))
            return match.group(0) if reason is None else _MALFORMED_NUMBER_MARKER

        redacted = _NUMERIC_TOKEN_RE.sub(_replace, text)
        logger.warning(
            "notifier: redacted %d malformed numeric token(s) before sending "
            "(%s) — the upstream text generator produced a garbled figure; "
            "fix that, not this guard",
            len(found),
            "; ".join(f"{tok!r} ({reason})" for tok, reason in found),
        )
        return redacted
    except Exception:  # noqa: BLE001
        logger.exception("notifier: malformed-number guard itself failed; sending text unredacted")
        return text


#: Neutral stand-in for a raw exception / internal-error string this guard
#: caught before it reached the owner. Board item 89 defect 5: reason codes
#: were made plain English, but the desk's many `f"... raised: {exc}"` /
#: `f"... failed ({exc})"` internal-error strings (e.g.
#: `src/coverage_watchdog.py`'s `_scan_for_gaps`, whose `reason=` field can
#: end up inside `unreadable_stop_text()`) were never touched, so a genuine
#: `ConnectionError('timed out')` or a traceback frame could still surface
#: verbatim in a Telegram message. The FULL original text is always logged
#: (see `_redact_raw_exception_text` below) — this marker only replaces
#: what the owner sees.
