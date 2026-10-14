"""Telegram delivery: the TelegramNotifier class.

Moved verbatim from the former src/notifier.py; the package re-exports it.
"""

from __future__ import annotations

import html
import os
import re
import requests
from typing import Any

from src.notifier.base import (
    ProbeResult,
    _REHEARSAL_MODE,
    _clip_text,
    logger,
)
from src.notifier.category import (
    filtered_by_category,
    resolve_risk_only,
)
from src.notifier.credit_line import split_credit_line
from src.notifier.owner_alert_delivery import SendResult, WireFailure
from src.notifier.send_log import record_send
from src.notifier.send_funnel import send as _public_send
from src.notifier.send_funnel import send_once as _send_once
from src.notifier.markup import (
    _escape_with_markup,
    _linkify_symbols,
)

#: Any HTML tag in text that has already been escaped: a literal '<' from a
#: caller is '&lt;' by then, so every '<' left is one of the fixed structural
#: tags (`_escape_with_markup`) or a symbol link (`_linkify_symbols`).
_TAG_RE = re.compile(r"<(/?)[a-zA-Z][^>]*>")
_TRUNCATED = "[...truncated]"


def _balanced_units(text: str, sep: str) -> list[str]:
    """Split `text` on `sep` into pieces that each carry balanced markup.

    A piece inside an open tag is joined to the piece before it, so a block
    such as `<blockquote expandable>` spanning several lines stays whole.
    """
    units: list[str] = []
    depth = 0
    for piece in text.split(sep):
        if units and depth > 0:
            units[-1] += sep + piece
        else:
            units.append(piece)
        for match in _TAG_RE.finditer(piece):
            depth = max(0, depth + (-1 if match.group(1) else 1))
    return units


def _fit_blocks(body: str, budget: int) -> str:
    """Fit escaped `body` into `budget` by dropping WHOLE blocks, never by
    cutting inside an element — so the result cannot carry an open tag.

    Order: the expandable DETAILS block first (the owner can read it in
    Mission Control), then trailing paragraphs, then trailing lines of what
    is left. Only a single balanced line that is alone over budget is cut,
    and only after its tags are removed, so the cut is on tag-free text
    (`_clip_text` never splits an entity). 13 Oct 2026: the morning report
    was cut inside `<blockquote expandable>` and Telegram refused it.
    """
    if len(body) <= budget:
        return body
    tail = "\n" + _TRUNCATED
    for sep in ("\n\n", "\n"):
        units = _balanced_units(body, sep)
        kept = [unit for unit in units if "<blockquote" not in unit] or units[:1]
        while len(kept) > 1 and len(sep.join(kept)) + len(tail) > budget:
            kept.pop()
        body = sep.join(kept)
        if len(body) + len(tail) <= budget:
            return body + tail
    return _clip_text(_TAG_RE.sub("", body), budget, marker=tail)


class TelegramNotifier:
    """Best-effort Telegram Bot API notifier.

    Reads `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` from the
    environment at construction. If either is missing, `enabled`
    stays False and every `send` call is a no-op.

    `TELEGRAM_DISABLED=1` overrides the env-var path so an operator
    can mute notifications without unsetting the bot creds.
    """

    API_URL = "https://api.telegram.org/bot{token}/sendMessage"
    API_BASE = "https://api.telegram.org/bot{token}"
    HTTP_TIMEOUT_S = 5.0
    #: Sent by `probe()` and deleted a moment later. Written to be
    #: self-explanatory on the off chance the delete fails and the operator
    #: reads it — an unexplained robot message in the alert channel is
    #: exactly the kind of thing that teaches someone to mute the channel.
    PROBE_TEXT = (
        "QAMC alerting self-test. This is the scheduled check that the alert "
        "channel still works; it deletes itself a second later. If you are "
        "reading it, only the delete failed — the channel is fine."
    )
    # Telegram hard limit is 4096; leave room for a truncation marker.
    MAX_MESSAGE_CHARS = 4000
    DEFAULT_LINK_LABEL = "🔗 Open Mission Control"

    def __init__(
        self,
        token: str | None = None,
        chat_id: str | None = None,
        mission_control_url: str | None = None,
    ):
        self.token = (token if token is not None else os.getenv("TELEGRAM_BOT_TOKEN", "")).strip()
        self.chat_id = (chat_id if chat_id is not None else os.getenv("TELEGRAM_CHAT_ID", "")).strip()
        kill_switch = os.getenv("TELEGRAM_DISABLED", "").strip().lower() in ("1", "true", "yes")
        # Kept as its own attribute: `enabled` collapses "muted on purpose"
        # and "no credentials" into one false, and probe() has to report
        # which of the two it actually is.
        self.muted = kill_switch
        # Second, independent switch: narrows the channel to money-at-risk alarms.
        # The hard mute still wins (`enabled` is already False). See category.py.
        self.risk_only = resolve_risk_only()
        self.enabled = bool(self.token and self.chat_id) and not kill_switch
        # Tap-through link target for send(). Unlike token/chat_id this is
        # NOT read from the environment — src/config.py::NotificationsConfig
        # (config/settings.yaml: notifications.mission_control_url) is the
        # source of truth, so it arrives as a constructor arg from a caller
        # holding a resolved AppConfig (main.py, TradingScheduler). An
        # empty/unset value means send() appends no link — never a broken
        # one. str(...) guards against a non-str default sneaking through
        # (e.g. a test double); production always passes a validated str.
        self.mission_control_url = str(mission_control_url or "").strip()
        if not self.enabled:
            if kill_switch:
                logger.info("TelegramNotifier: disabled via TELEGRAM_DISABLED env var")
            else:
                logger.info("TelegramNotifier: disabled (set TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID env vars to enable)")

    def _redact(self, value: object) -> str:
        """Strip the bot token AND chat id out of anything headed for the
        log or the durable send record (see `_record_send`).

        `requests` embeds the full request URL in HTTPError /
        ConnectionError messages, and ours is
        `https://api.telegram.org/bot<TOKEN>/sendMessage` — so logging
        the raw exception wrote the bot token into quant_agent.log (and
        the systemd journal) on every Telegram failure. A wrong or
        rotated token is the most likely failure, i.e. the token leaked
        exactly when the operator was most likely to share the log.

        The chat id is stripped too (2026-09-18, `_record_send`): it is not
        a secret the way the token is, but it is a stable per-operator
        identifier with no reason to sit in a table a future export or
        support conversation might carry, so it gets the same treatment
        for free here rather than a second bespoke check at the call site.

        Non-raising on purpose: this runs INSIDE the `except` blocks
        below, and `logger.warning("%s", exc)` used to defer `str(exc)`
        to logging (which absorbs its own formatting errors). Calling
        str() eagerly here would otherwise hand an exception with a
        broken __str__ a brand-new path out of a notifier that must
        never raise into trading.
        """
        try:
            text = str(value)
            if self.token:
                text = text.replace(self.token, "<redacted>")
            if self.chat_id:
                text = text.replace(self.chat_id, "<redacted>")
            return text
        except Exception:  # noqa: BLE001
            return "<unprintable error>"

    def _record_send(self, **kwargs) -> None:
        """See `send_log.record_send`."""
        record_send(self, **kwargs)

    def _safe_record_send(self, **kwargs) -> None:
        """Call `_record_send`, wrapped in its own try/except.

        `_record_send` already guards every DB failure it can anticipate
        internally, but `send()`/`send_document()` call it from inside
        their own control flow (the success path, and — for failures —
        an `except` block that must not itself raise). Doubly-defensive
        on purpose, same reasoning main.py gives for wrapping its own
        `notifier.send()` call a second time: a bug inside the recording
        path that `_record_send` did not anticipate must still be unable
        to reach the caller of `send()`/`send_document()`, which is the
        one guarantee this whole feature is not allowed to weaken.
        """
        try:
            self._record_send(**kwargs)
        except Exception as exc:  # noqa: BLE001
            logger.warning("notifier: _record_send raised unexpectedly: %s", exc)

    # The single attempt and the public funnel that wraps it both live in
    # `send_funnel` (this file is at its size ceiling). Bound here so no
    # caller can reach the wire without the retry-and-record discipline.
    send_once = _send_once
    send = _public_send

    def _post_payload(self, payload: dict) -> SendResult:
        """The one sendMessage wire call; raises `WireFailure` on any failure.

        On success it returns the `SendResult` (status code and Telegram's
        message id); a failure travels in the exception, and `send_once`
        turns it into the failed `SendResult` it returns.

        Lives here, not in `send_funnel`, because this module is the cleared
        outbound-client site: the replay guard names it as the seam for
        `requests`, and the funnel must not become a second one.

        Success is still judged by `raise_for_status()`. A refusal keeps
        Telegram's own `error_code` and `description` (its JSON body names
        the repair: "can't parse entities", "chat not found", ...) instead
        of the bare "400 Client Error" `raise_for_status` gives, and says
        whether a resend can land. `send_once` records `str(exc)`.
        """
        response = None
        try:
            response = requests.post(
                self.API_URL.format(token=self.token),
                json=payload,
                timeout=self.HTTP_TIMEOUT_S,
            )
            response.raise_for_status()
        except requests.HTTPError as exc:
            refused: Exception = exc
        except requests.RequestException as exc:
            failure = WireFailure(
                f"network error: {self._redact(exc)}",
                retryable=True,
                summary="could not reach Telegram",
            )
            refused = failure
        else:
            return self._landed(response)
        # Built and raised OUTSIDE the except block, `from None`: the
        # `requests` exception carries the token in its URL, and a chained
        # or in-flight one is printed by any traceback logged later (the
        # JSON-body guard, `logger.exception` upstream). Only the redacted
        # reason travels on.
        if not isinstance(refused, WireFailure):
            refused = self._refusal(refused, response)
        raise refused from None

    def _landed(self, response: Any) -> SendResult:
        """The result of a send Telegram accepted: its status and the new message's id."""
        result = self._json_body(response).get("result")
        message_id = result.get("message_id") if isinstance(result, dict) else None
        status = getattr(response, "status_code", None)
        return SendResult(
            True,
            status_code=status if isinstance(status, int) else None,
            message_id=message_id if isinstance(message_id, int) else None,
        )

    def _refusal(self, exc: Exception, response: Any) -> WireFailure:
        """Telegram's reason for refusing a send, from its JSON body.

        4xx is final (the same body gets the same answer) except 429, whose
        `parameters.retry_after` is Telegram's own wait; 5xx is transient.
        An unreadable status is treated as transient, as every failure was.
        """
        body = self._json_body(response) if response is not None else {}
        code = body.get("error_code")
        if not isinstance(code, int):
            code = getattr(response, "status_code", None)
        if not isinstance(code, int):
            match = re.match(r"\s*(\d{3})\b", str(exc))
            code = int(match.group(1)) if match else None
        description = body.get("description")
        reason = f"Telegram {code}: {description}" if description else self._redact(exc)
        parameters = body.get("parameters")
        retry_after = parameters.get("retry_after") if isinstance(parameters, dict) else None
        return WireFailure(
            reason,
            retryable=code is None or code == 429 or code >= 500,
            code=code,
            retry_after=float(retry_after) if isinstance(retry_after, (int, float)) else None,
            description=description if isinstance(description, str) and description else None,
        )

    def _api_url(self, method: str) -> str:
        """Bot API endpoint for `method` (sendMessage, deleteMessage, ...)."""
        return f"{self.API_BASE.format(token=self.token)}/{method}"

    def _build_payload(
        self,
        text: str,
        link_url: str | None = None,
        link_label: str | None = None,
        symbols: list[str] | None = None,
        preserve_structural_markup: bool = False,
    ) -> dict[str, Any]:
        """The exact JSON body `send()` puts on the wire.

        Factored out so `probe()` can transmit a body built by this same
        code rather than one of its own. A probe that assembled its own
        payload would prove that *some* request reaches Telegram while
        leaving the real message shape — HTML parse mode, escaping, the
        length budget — untested, which is how a self-test ends up passing
        while the thing it stands in for is broken.

        The one place a message is fitted to `MAX_MESSAGE_CHARS`: the
        credit line and the Mission Control link are protected blocks, the
        body is fitted to what is left by `_fit_blocks` (whole blocks only).
        """
        # HTML over MarkdownV2: PM/tech rationale is full of tickers with
        # underscores, "*" bullets, parentheticals, and "%" — MarkdownV2
        # demands escaping ~18 characters or Telegram rejects the entire
        # message ("can't parse entities"); HTML needs exactly '&','<','>'.
        # Escaping BEFORE the length check matters too: an unescaped '&'
        # costs 5 chars once escaped ('&amp;'), so measuring the pre-escape
        # length risks shipping something past Telegram's real 4096 cap.
        # `_escape_with_markup`, not a bare `html.escape`, ONLY when the
        # caller opted in (`preserve_structural_markup=True` — see
        # `send()`'s docstring): src/trader_feed.py embeds a fixed, small
        # set of literal structural tags (`<b>`, `<blockquote expandable>`)
        # in its plain text on purpose; no other caller does, and every
        # other caller must keep the historical "always fully escape"
        # contract.
        text, credit_line = split_credit_line(text)
        escaped = _escape_with_markup(text) if preserve_structural_markup else html.escape(text)
        credit_html = f"\n\n{html.escape(credit_line)}" if credit_line else ""

        resolved_url = link_url if link_url is not None else self.mission_control_url
        link_html = ""
        if resolved_url:
            label = link_label if link_label is not None else self.DEFAULT_LINK_LABEL
            # quote=True: this value sits inside href="...", not message
            # body text — needs '"' escaped too, not just '&','<','>'.
            safe_url = html.escape(resolved_url, quote=True)
            safe_label = html.escape(label)
            link_html = f'\n\n<a href="{safe_url}">{safe_label}</a>'

        budget = max(0, self.MAX_MESSAGE_CHARS - len(credit_html) - len(link_html))

        # Symbol links are strictly best-effort: try the linked text first;
        # if it does not fit, fit the plain escaped text instead — symbol
        # links disappear, the alert still ships, and nothing is ever cut
        # inside an anchor.
        linked = _linkify_symbols(escaped, symbols) if symbols else escaped
        body = linked if len(linked) <= budget else _fit_blocks(escaped, budget)
        final_text = body + credit_html + link_html

        return {
            "chat_id": self.chat_id,
            "text": final_text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }

    @staticmethod
    def _json_body(response: Any) -> dict[str, Any]:
        """Telegram's JSON body, or {} if it did not send parseable JSON."""
        try:
            body = response.json()
        except Exception as exc:  # noqa: BLE001 - a proxy error page is not JSON
            from src.sentinel.counted import record_swallowed

            record_swallowed("notifier.transport.json_body", exc, log=logger)
            return {}
        return body if isinstance(body, dict) else {}

    def probe(self, text: str | None = None) -> ProbeResult:
        """Prove the alert channel works, by using it.

        WHY THIS IS NOT AN ENV-VAR CHECK. "Are the credentials set" answers
        a question nobody has. The failures that actually silence this desk
        are a token that is set but revoked, a chat id that is set but wrong
        or deleted, a bot the operator blocked, and an egress rule that
        drops api.telegram.org. Every one of those passes a variable check
        and fails a send. So this sends.

        WHY IT IS STILL QUIET. The message goes out with
        `disable_notification` (delivered, no buzz) and is deleted
        immediately afterwards, so proving the channel does not spend the
        operator's attention. The Bot API lets a bot delete its own message
        for 48h, so the delete is reliable; when it is not, `residue` says
        so and the message itself explains what it is.

        Returns a ProbeResult rather than a bool: an operator needs to know
        WHICH of the four failures he has, and they need four different
        repairs.
        """
        if _REHEARSAL_MODE:
            # A rehearsal must not transmit. Reported as not-ok with its own
            # stage so a caller can tell "we did not check" apart from "we
            # checked and it is broken" — collapsing those two is the exact
            # defect this whole probe exists to remove.
            return ProbeResult(
                False,
                "rehearsal",
                "suppressed: QAMC_REHEARSAL=1, nothing sent",
            )
        if self.muted:
            # A DELIBERATE mute is not a fault. Only this process can tell
            # the two apart (it is the one holding the env), so it must say
            # which it is; reporting a choice the operator made as a broken
            # channel is a false statement about the desk's state.
            return ProbeResult(
                False,
                "muted",
                "alerts are muted on purpose: TELEGRAM_DISABLED is set in "
                "this process — nothing is broken and nothing will be sent",
            )
        if not self.enabled:
            return ProbeResult(
                False,
                "credentials",
                "this process has no TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID — an alert raised here would reach nobody",
            )

        # link_url="" — never append the Mission Control link to a probe.
        payload = self._build_payload(text or self.PROBE_TEXT, link_url="")
        # Delivered silently: the operator's phone must not buzz daily for
        # this. Deletion below removes it from the chat entirely.
        payload["disable_notification"] = True

        try:
            response = requests.post(
                self.API_URL.format(token=self.token),
                json=payload,
                timeout=self.HTTP_TIMEOUT_S,
            )
        except Exception as exc:  # noqa: BLE001
            return ProbeResult(False, "transport", self._redact(exc))

        body = self._json_body(response)
        if response.status_code >= 400 or not body.get("ok"):
            # Telegram reports refusals as HTTP 4xx with a `description`
            # ("Unauthorized", "chat not found", "bot was blocked by the
            # user"). Carry the description through — it names the repair.
            detail = body.get("description") or f"HTTP {response.status_code}"
            return ProbeResult(False, "api", self._redact(detail))

        result = body.get("result")
        message_id = result.get("message_id") if isinstance(result, dict) else None
        if message_id is None:
            # Accepted but unidentifiable. The send worked, so the channel is
            # proved; we simply cannot clean up after ourselves.
            return ProbeResult(
                True,
                "delivered",
                "Telegram accepted the probe but returned no message_id",
                residue=True,
            )

        deleted, delete_detail = self._delete_message(message_id)
        return ProbeResult(True, "delivered", delete_detail, residue=not deleted)

    def _delete_message(self, message_id: int) -> tuple[bool, str]:
        """Best-effort removal of a message this bot sent. Never raises."""
        try:
            response = requests.post(
                self._api_url("deleteMessage"),
                json={"chat_id": self.chat_id, "message_id": message_id},
                timeout=self.HTTP_TIMEOUT_S,
            )
        except Exception as exc:  # noqa: BLE001
            return False, f"delete failed: {self._redact(exc)}"
        body = self._json_body(response)
        if response.status_code >= 400 or not body.get("ok"):
            detail = body.get("description") or f"HTTP {response.status_code}"
            return False, f"delete refused: {self._redact(detail)}"
        return True, ""

    def send_document(
        self,
        csv_bytes: bytes,
        filename: str,
        caption: str = "",
        kind: str = "document",
        run_id: str | None = None,
        category: str | None = None,
    ) -> bool:
        """Send a file (e.g. CSV) via Telegram sendDocument. Best-effort.

        Recorded the same way `send()` is (see `_record_send`): the
        document's bytes are never stored (they are the P&L CSV, not
        text meant for the "what did we tell the owner" question), only
        `caption` — the text that actually appears in the chat next to
        it — plus `filename` so the record still says what went out.
        """
        if not self.enabled:
            return False
        recorded_text = f"[document: {filename}] {caption}".strip()
        if filtered_by_category(self, kind=kind, category=category, text=recorded_text, run_id=run_id):
            return False
        try:
            response = requests.post(
                f"https://api.telegram.org/bot{self.token}/sendDocument",
                data={"chat_id": self.chat_id, "caption": caption},
                files={"document": (filename, csv_bytes, "text/csv")},
                timeout=30.0,
            )
            response.raise_for_status()
            self._safe_record_send(kind=kind, status="sent", text=recorded_text, run_id=run_id)
            return True
        except Exception as exc:
            logger.warning("Telegram send_document failed: %s", self._redact(exc))
            self._safe_record_send(
                kind=kind,
                status="failed",
                text=recorded_text,
                detail=self._redact(exc),
                run_id=run_id,
            )
            return False
