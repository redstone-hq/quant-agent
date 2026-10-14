"""Idempotent order submission: deterministic `client_order_id` derivation
and the duplicate-key read-back decisions, kept apart from the broker adapter.

The adapter in `src/execution/broker.py` imports these and passes in its own
client and its `PROTECTIVE_ORDER_HOLDS_SHARES_STATUSES` vocabulary (that set
stays in broker.py beside the sets it extends, so this module never imports
the adapter and there is no import cycle).
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Idempotent order submission — one deterministic `client_order_id` per INTENT.
#
# WHY. Before this, every order was a fresh request with no deduplication key
# (`client_order_id` appeared nowhere in src/). Alpaca treats
# `client_order_id` as the order's idempotency key: a second POST carrying an
# id that an ACTIVE order already holds is refused with HTTP 422
# "client_order_id must be unique" (Alpaca's own troubleshooting guide,
# https://alpaca.markets/learn/how-to-fix-common-trading-api-errors-at-alpaca).
# Without a key, an HTTP timeout AFTER the broker accepted the order is
# indistinguishable from a rejection, and a resubmission in that window is a
# SECOND real position. The orphan sweep in src/pipeline_stages.py is
# after-the-fact recovery; this is prevention.
#
# THE KEY IS DERIVED, NEVER GENERATED. It is built only from stable facts of
# the intent — purpose, symbol, side, ET session date, quantity, price — so
# the SAME intent retried produces the SAME key (the broker refuses the
# duplicate) while a genuinely NEW intent (other side, other day, other size,
# a ratcheted stop trigger, a stop instead of an entry) produces a different
# one. No timestamp, random value or counter: any of those would make every
# retry look new and defeat the whole purpose.
#
# LENGTH LIMIT — cited, not chosen. Alpaca publishes two figures for this
# field: the Trading API reference for POST /v2/orders
# (https://docs.alpaca.markets/reference/postorder) says "<= 128 characters";
# the Broker API orders reference in Alpaca's own docs repository
# (https://github.com/alpacahq/alpaca-docs/blob/master/content/api-references/broker-api/trading/orders.md)
# says "<= 48 characters". The stricter of the two published limits is
# honoured so the key is valid under BOTH documents. Neither page specifies
# an allowed character set, so the key uses only characters Alpaca already
# accepts in this request's other fields: the symbol's own letters, digits
# and '.', plus '-' and hex digits.
#
# HASH WIDTH — derived, not chosen. When the natural key is longer than the
# limit, the readable prefix (purpose-symbol-date-) is kept and the remainder
# of the budget is filled with the SHA-256 hex digest of the full natural key;
# the digest width is simply whatever the limit leaves after the prefix.
# ---------------------------------------------------------------------------
_CLIENT_ORDER_ID_MAX_LEN = 48
# Verbatim error text from the Alpaca guide cited above.
_CLIENT_ORDER_ID_DUPLICATE_TEXT = "client_order_id must be unique"


def _session_date_key() -> str:
    """ET trading-day key 'YYYY-MM-DD' — the desk's shared per-day key."""
    from src.trading_calendar import session_date_key

    return session_date_key()


def _client_order_id(
    *,
    purpose: str,
    symbol: str,
    side: str,
    session_date: str,
    qty: float,
    price: float | None,
    supersedes: str | None = None,
) -> str:
    """Deterministic idempotency key for ONE order intent (see block above).

    `purpose` discriminates the intent class: 'ENT' entry/exit order,
    'STP' protective stop-MARKET, 'STL' the stop-LIMIT safety fallback.
    `price` is the limit (entry) or trigger (stop); None for a market order.

    `supersedes` is the broker id of a DYING order (`pending_cancel`, or
    any other status outside `PROTECTIVE_ORDER_HOLDS_SHARES_STATUSES`) that
    still holds this intent's plain key. Alpaca's uniqueness rule is among
    ACTIVE orders only ("use a unique client_order_id for each active
    order", its troubleshooting guide), so in practice the plain key is
    refused only while the prior stop is still in flight; once it is
    terminal the key is free and the plain key is accepted outright.
    Folding the dying order's id into the key lets the replacement go in
    without waiting for the cancel to complete.

    KNOWN LIMITATION (adversary round 3, 2026-10-01): this does NOT give
    "never two live stops". If the replacement's POST is accepted but times
    out, and the prior stop's cancel completes before the retry, the retry
    derives the PLAIN key (nothing holds it any more), is accepted with no
    422, and a second live stop rests. Proven by
    test_KNOWN_LIMITATION_retry_after_cancel_completes_leaves_two_live_stops.
    """
    import hashlib

    side_token = side.lower().replace("_", "")
    price_token = "mkt" if price is None else repr(float(price))
    natural = f"{purpose}-{symbol}-{side_token}-{session_date}-{repr(float(qty))}-{price_token}"
    if supersedes:
        natural += f"-s{supersedes}"
    if len(natural) <= _CLIENT_ORDER_ID_MAX_LEN:
        return natural
    prefix = f"{purpose}-{symbol}-{session_date}-"
    digest = hashlib.sha256(natural.encode("ascii", "replace")).hexdigest()
    return (prefix + digest)[:_CLIENT_ORDER_ID_MAX_LEN]


def _is_duplicate_client_order_id_rejection(exc: BaseException) -> bool:
    """True when the broker refused because THIS intent's order already exists.

    That reply is the idempotency guard WORKING, not a submission failure:
    Alpaca answers a duplicate `client_order_id` with HTTP 422 and the
    message "client_order_id must be unique" (source cited in the block
    above). Both the code and the text are required so a different 422
    (bad parameters) is never mistaken for "already exists".
    """
    return getattr(exc, "status_code", None) == 422 and _CLIENT_ORDER_ID_DUPLICATE_TEXT in str(exc).lower()


# The set for a THIRD question, asked only by the idempotent stop-submit
# path: "does this order still hold the shares, so that submitting another
# stop over it would be a SECOND sell?" (adversary round 3, 2026-10-01).
# It is wider than `PROTECTIVE_ORDER_ALIVE_STATUSES` because that set was
# built for an AGED order-book read ("is this resting order coverage?") and
# deliberately excludes states that still hold the shares:
#   `pending_replace` — the desk amends stops IN PLACE via
#                       `replace_order_by_id`; a stop read back mid-amend
#                       is still the live stop, not a dead one;
#   `stopped`         — Alpaca: "a trade is guaranteed for the order ...
#                       but has not yet occurred" — the shares are being
#                       sold by THIS order;
#   `calculated`      — Alpaca: "completed for the day (either filled or
#                       done for day), settlement pending" — already sold;
#   `filled`          — already sold; a stop-MARKET placed through its
#                       trigger can come back `filled` on the placement
#                       response itself, and retrying that is a fresh sell.
# `pending_cancel`, `canceled`, `expired`, `rejected` stay OUT: those hold
# nothing and may be superseded. The shared set is NOT mutated: its two
# existing readers are correct for their own questions.
# (Defined in src/execution/broker.py beside the sets it extends; passed in
# here as `holds_shares_statuses` so this module never imports the adapter.)


def _is_dead_stop_result(result: object, holds_shares_statuses: frozenset) -> bool:
    """True when a stop-placement result dict names a status under which the
    order holds NO shares (outside `PROTECTIVE_ORDER_HOLDS_SHARES_STATUSES`:
    `pending_cancel`, `canceled`, `expired`, `rejected`, ...), so a retry is
    safe. A `pending_replace`, `stopped`, `calculated` or `filled` result is
    NOT dead: retrying over it would sell the shares a second time. A result
    that carries no status at all is not judged here — the id/kill-switch
    checks around the call sites own that case.
    """
    if not isinstance(result, dict):
        return False
    status = result.get("status")
    if not isinstance(status, str) or not status:
        return False
    return status.lower() not in holds_shares_statuses


def _is_no_such_order(exc: BaseException) -> bool:
    """True when a read-back by key got the broker's own "no order holds this
    key" answer: Alpaca replies HTTP 404 ("order not found") to
    GET /v2/orders:by_client_order_id for an unknown key. Any other failure
    is a failed read, never proof that nothing was placed."""
    return getattr(exc, "status_code", None) == 404


def _existing_order_for_client_id(client, client_order_id: str, duplicate_exc: BaseException | None = None):
    """Fetch the order the broker holds under `client_order_id`.

    With `duplicate_exc`: called after `_is_duplicate_client_order_id_rejection`
    — the broker has just asserted the order EXISTS, so the only honest
    outcomes are "here it is" or an exception that says it exists but
    could not be read back (never a quiet None, which a caller would
    read as "nothing was placed" — the exact mistake the key prevents).

    Without it (a submit that raised with no refusal, or the orphan sweep):
    nothing has been asserted, so a failed or empty read-back propagates
    unchanged and the caller decides what it means.
    """
    try:
        return client.get_order_by_client_id(client_order_id)
    except Exception as lookup_exc:  # noqa: BLE001
        if duplicate_exc is None:
            raise
        raise RuntimeError(
            f"broker reports an order already exists for "
            f"client_order_id={client_order_id!r} but it could not be "
            f"read back ({lookup_exc}); treat as PLACED, not rejected"
        ) from duplicate_exc


def _submit_stop_request_idempotent(
    client,
    build_request,
    *,
    purpose: str,
    alpaca_symbol: str,
    side: str,
    session_date: str,
    qty: float,
    price: float,
    holds_shares_statuses: frozenset,
):
    """Submit ONE protective-stop request under its idempotency key and
    return the order that genuinely rests for it.

    Adversary finding 2026-10-01: a duplicate-key 422 read back WITHOUT
    inspecting status counted a `pending_cancel` stop as live protection
    — the exact case `_restore_stop_orders` hits, since it resubmits the
    same qty/trigger/side moments after cancelling, while the cancel is
    still in flight and the key is therefore still held (Alpaca's rule
    is uniqueness among ACTIVE orders; a terminal order frees its key,
    so a fully `canceled` stop never produces this 422 at all). So:

    * duplicate 422 -> read the existing order back and INSPECT it;
    * status in `holds_shares_statuses` (resting,
      placement-pending, mid-amend `pending_replace`, executing
      `stopped`, or already executed) -> that order holds the shares;
      return it and NEVER place a second stop over it;
    * any other status (`pending_cancel`, `canceled`, `expired`,
      `rejected`, ...) -> holds nothing: derive a superseding key from
      the dying order's id (see `_client_order_id`) and submit a
      genuinely NEW stop. A dead replacement chains the same way; the
      loop ends because every hop names a distinct dead order and a
      never-seen key is accepted outright.

    What this does NOT guarantee: see the KNOWN LIMITATION in
    `_client_order_id` — a timed-out replacement retried after the
    prior stop's cancel completes is accepted as a second live stop.

    Every non-duplicate exception propagates untouched so the caller's
    own classification (unsupported-type fallback, held_for_orders,
    terminal rejections) is unchanged.
    """
    supersedes: str | None = None
    seen_dead: set[str] = set()
    while True:
        client_order_id = _client_order_id(
            purpose=purpose,
            symbol=alpaca_symbol,
            side=side,
            session_date=session_date,
            qty=qty,
            price=price,
            supersedes=supersedes,
        )
        request = build_request(client_order_id)
        try:
            return client.submit_order(request)
        except Exception as exc:  # noqa: BLE001
            if not _is_duplicate_client_order_id_rejection(exc):
                raise
            existing = _existing_order_for_client_id(
                client,
                client_order_id,
                exc,
            )
        status = str(getattr(existing.status, "value", existing.status)).lower()
        if status in holds_shares_statuses:
            # The guard working: this exact stop already rests at the
            # broker (a retry after a timed-out POST) — or is being
            # amended / is executing, which equally holds the shares.
            logger.info(
                "protective stop already rests at broker for %s %s "
                "qty=%s @ %s (client_order_id=%s, status=%s) — "
                "returning it instead of submitting a duplicate.",
                purpose,
                alpaca_symbol,
                qty,
                price,
                client_order_id,
                status,
            )
            return existing
        dead_id = str(existing.id)
        if dead_id in seen_dead:
            raise RuntimeError(
                f"broker keeps returning dead order {dead_id} "
                f"(status={status}) for client_order_id="
                f"{client_order_id!r}; refusing to report it as a "
                f"placed stop"
            )
        seen_dead.add(dead_id)
        logger.warning(
            "protective stop key %s for %s is held by order %s whose "
            "status is %s — that order is NOT protection; submitting a "
            "new stop under a superseding key.",
            client_order_id,
            alpaca_symbol,
            dead_id,
            status,
        )
        supersedes = dead_id


def _submit_entry_request_idempotent(client, request, *, client_order_id: str, side: str, qty, symbol: str):
    """Submit ONE entry/exit request under its key. A duplicate-key refusal is
    the guard working: the EXISTING order is read back and returned instead of
    a second trade. Every other failure propagates to the caller unchanged."""
    try:
        return client.submit_order(request)
    except Exception as exc:  # noqa: BLE001
        if not _is_duplicate_client_order_id_rejection(exc):
            raise
        order = _existing_order_for_client_id(client, client_order_id, exc)
        logger.info(
            "Order already exists at broker for %s %s %s (client_order_id=%s) "
            "— returning the existing order %s instead of submitting a duplicate.",
            side,
            qty,
            symbol,
            client_order_id,
            order.id,
        )
        return order
