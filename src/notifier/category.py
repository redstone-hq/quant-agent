"""Per-category Telegram mute: category vocabulary, fail-closed resolution and the drop recorder.

Re-applied from PR #978 (which edited the former src/notifier.py) onto the package
layout. `TelegramNotifier` in transport.py calls into this module.
"""

from __future__ import annotations

import os
from pathlib import Path

from src.notifier.base import logger
from src.notifier.markup import _dedupe_symbols

#: Message CATEGORY — the axis the per-category mute (`TELEGRAM_RISK_ONLY`)
#: routes on. Deliberately NOT inferred from message text: a keyword guess
#: on prose is exactly the kind of classifier that silently starts dropping
#: a money-at-risk alarm the day someone rewords it. Every send site either
#: declares its category or inherits one from an EXPLICIT `kind` mapping
#: below; anything else fails CLOSED and is delivered.
CATEGORY_RISK = "risk"
CATEGORY_OPERATIONAL = "operational"


class SuppressedSend:
    """A deliberate suppression — NOT a delivery failure.

    `send()` historically returned a bool, and every caller reads False as
    "Telegram did not take the message, try again". A mute or a category
    filter is a settled outcome: retrying cannot change it, and a caller
    that loops "until it succeeds" loops forever. This sentinel stays falsy
    (so no existing truthiness check changes meaning) while letting a caller
    that cares ask `was_suppressed(outcome)` and stop.
    """

    __slots__ = ()

    def __bool__(self) -> bool:
        return False

    def __repr__(self) -> str:  # pragma: no cover - diagnostics only
        return "SUPPRESSED"


#: Singleton returned by `send()` when the desk dropped the message on
#: purpose (TELEGRAM_DISABLED, or TELEGRAM_RISK_ONLY + operational).
SUPPRESSED = SuppressedSend()


def was_suppressed(outcome: object) -> bool:
    """True when a send outcome is a deliberate drop, not a failed send."""
    return outcome is SUPPRESSED


#: `kind` -> category, for the kinds that are routine BY CONSTRUCTION (a
#: scheduled session summary, the P&L document, the hourly intraday check).
#: `generic` and `owner_alert` are absent on purpose: both carry a mix of
#: naked-position alarms and operational faults, so they are classified at
#: the CALL SITE via the `category=` argument, and fail closed to risk when
#: a site has not been classified.
_KIND_CATEGORY: dict[str, str] = {
    "morning": CATEGORY_OPERATIONAL,
    "midday": CATEGORY_OPERATIONAL,
    "close": CATEGORY_OPERATIONAL,
    "evening": CATEGORY_OPERATIONAL,
    "intra_check": CATEGORY_OPERATIONAL,
    # intra_safety is intra_check's free twin (src/session_calendar.py aliases
    # it). Its summary is routine by construction: main.py sends the
    # naked-position alarm FIRST as its own `no_stop_at_all` owner alert
    # (send_naked_position_alert) and only then the summary under
    # kind=args.mode. Same shape as intra_check above. Live log Fri 16 Oct:
    # 13 "nothing to report" summaries were paged as CRITICAL.
    "intra_safety": CATEGORY_OPERATIONAL,
    "earnings_preprocess": CATEGORY_OPERATIONAL,
}


def resolve_category(category: str | None, kind: str | None) -> str:
    """Decide whether a message is money-at-risk or operational.

    FAILS CLOSED. An unset, unrecognised or malformed `category`, on a
    `kind` with no explicit mapping, resolves to `CATEGORY_RISK` and is
    therefore DELIVERED under risk-only. Silence is the dangerous failure
    here, not noise: the whole reason the owner muted the channel was
    operational noise, but the thing that must never be lost is "nothing is
    protecting this position".
    """
    value = str(category or "").strip().lower()
    if value == CATEGORY_OPERATIONAL:
        return CATEGORY_OPERATIONAL
    if value == CATEGORY_RISK:
        return CATEGORY_RISK
    if value:
        # An unknown string is a bug at the call site, not a licence to
        # drop the message.
        logger.warning(
            "notifier: unknown message category %r (kind=%s) — treating as money-at-risk and delivering",
            category,
            kind,
        )
        return CATEGORY_RISK
    return _KIND_CATEGORY.get(str(kind or ""), CATEGORY_RISK)


def _risk_only_declared_default() -> bool:
    """`notifications.risk_only` from config/settings.yaml: the DECLARED state of the mute.

    Reads only that one key, NOT through `load_config`: importing src.config pulls in
    trading code, which the notifier (and scripts/telegram_test.py) must not load, and
    would add an import cycle. The env var still wins (see `resolve_risk_only`); the
    feature registry fails the build if this declaration drifts. Never raises: a
    notifier that cannot read config must still send.
    """
    try:
        import yaml

        path = Path(__file__).resolve().parents[2] / "config" / "settings.yaml"
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return bool((data.get("notifications") or {}).get("risk_only", False))
    except Exception:  # noqa: BLE001
        return False


def resolve_risk_only() -> bool:
    """The env var wins when set to a value this understands, including an
    explicit off; otherwise the declared config default applies."""
    env = os.getenv("TELEGRAM_RISK_ONLY", "").strip().lower()
    if env in ("1", "true", "yes"):
        return True
    if env in ("0", "false", "no"):
        return False
    return _risk_only_declared_default()


def filtered_by_category(
    self,
    *,
    kind: str,
    category: str | None,
    text: str,
    run_id: str | None = None,
    symbols: list[str] | None = None,
) -> bool:
    """True when risk-only mode drops this message. Records the drop.

    A dropped alarm that leaves no trace is indistinguishable from an
    alarm that never fired, so the drop lands in `notifier_sends` with
    its own status — `filtered`, distinct from both `sent` and the
    hard mute's `muted` — exactly as the `muted` path does.
    """
    if not getattr(self, "risk_only", False):
        return False
    if resolve_category(category, kind) != CATEGORY_OPERATIONAL:
        return False
    joined = ", ".join(_dedupe_symbols(symbols or []))
    try:
        self._record_send(
            kind=kind,
            status="filtered",
            run_id=run_id,
            text=text,
            detail=(
                "dropped by TELEGRAM_RISK_ONLY (operational category)" + (f"; symbols: {joined}" if joined else "")
            ),
            strict=True,
        )
    except Exception as exc:  # noqa: BLE001
        # Fail closed, the principle this whole switch rests on. An
        # unrecordable drop is a message that exists nowhere: not in
        # Telegram, not in `notifier_sends`, so not on the dashboard
        # either. Deliver it instead — noise the owner can mute beats
        # an alarm nobody can find.
        logger.warning(
            "notifier: could not record a category drop (%s); delivering the message instead: %s",
            kind,
            exc,
        )
        return False
    return True
