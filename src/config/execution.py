"""Execution settings: how an entry crosses the spread and how the fill socket is bounded.

Moved verbatim from src/config/__init__.py (pure move; bodies AST-identical).
"""

from pydantic import BaseModel, Field, model_validator


class ExecutionConfig(BaseModel):
    """How aggressively an entry may cross the spread.

    Separate from `RiskConfig` on purpose: this bounds EXECUTION cost, not
    position risk. A too-tight cap does not make the book safer — it silently
    stops it trading, which is what happened to VLO on 2026-08-27.
    """

    max_entry_slippage_bps: float = Field(default=40.0, gt=0, le=500)
    """Max basis points of adverse excursion from the verified reference
    price an entry limit may sit. A BUY limit is capped this far above the
    reference; a SHORT limit is floored this far below it — the same bound,
    opposite side (fillability parity, not a second risk budget). A
    displayed quote already beyond this no longer skips the entry (board
    item 183, 2026-09-30 — all 8 recorded skips were venue noise, not a
    market that had run): the order is still sent at the bound, which it
    cannot fill through, and the far-through quote is recorded as a
    `venue_quote_through_ceiling` pipeline event."""

    repeg_enabled: bool = False
    """Master switch for the single-shot entry reprice. OFF by default so
    the feature can be deployed dark: with it off, `_repeg_entry_order`
    returns the original order id untouched and not a single broker call is
    made. The owner owns this switch."""

    # `repeg_max_attempts` was DELETED 2026-09-12 (rejected loudly below if
    # still present in settings.yaml). The reprice is now exactly ONE
    # replace, by design, not by a cap set to 1: Alpaca's own community
    # practice for a fast market is a single deliberately aggressive replace
    # that crosses the market, not a ladder of nudges — and every extra
    # replace is another `pending_replace` window an order can get stuck in.
    # A knob whose only legal value is 1 would invite someone to turn it up.

    rotation_enabled: bool = False
    """Master switch for AUTOMATIC opportunity-cost rotation (Phase 14b,
    `src/rotation.py`). OFF by default so it deploys dark, exactly like
    `repeg_enabled`: with it off, the rotation comparison is still computed
    and shown to the Portfolio Manager as information (the Phase 14
    behaviour, unchanged byte for byte), and the desk never closes a
    position on its own. With it on, ONE categorically-ineligible held
    position per morning session — one that fails the desk's own entry
    rules today AND whose structural protection has already broken — is
    closed through the ordinary PM-target → constructor → Risk Manager →
    execution path, to free room for the best-ranked new candidate the PM
    itself asked to buy. Every rotation fires a standalone owner alert."""

    rotation_ranked_margin_enabled: bool = False
    """SECOND switch, board item 39. Extends automatic rotation from the
    CATEGORICAL tier to the RANKED-MARGIN tier (`src/rotation.py`: both
    sides still pass the desk's own entry gates, and the new candidate
    cleared the provisional 25% margin on the like-for-like sub-score).
    OFF by default and required IN ADDITION to `rotation_enabled`.

    Turning it on is NOT sufficient to put a ranked-margin sale on the
    wire. `rotation_sell_reason` refuses to build the sale's reason at all
    unless it is handed a `RotationClearance` — an object only
    `src/pipeline_stages.py::_rotation_buy_leg_projected_refusal` can mint,
    and only after the replacement BUY has been run through the downstream
    refusal gates against PROJECTED POST-SALE state. That guard is
    structural and unconditional: no value of this flag, and no config at
    all, can substitute for the clearance."""

    repeg_poll_seconds: float = Field(default=5.0, gt=0, le=30)
    """How long to let the working order rest before the one reprice, and —
    only if the exchange has not yet acknowledged the order by then — how
    much longer to wait for that acknowledgement before giving up on the
    reprice (a replace against an unacknowledged order is rejected by
    Alpaca). Total added latency per entry is therefore at most
    `2 * repeg_poll_seconds` plus one replace round-trip, and lands BEFORE
    `place_entry_protection`'s own fill wait, which is where an entry still
    unfilled at the end of its session is cancelled."""

    @model_validator(mode="before")
    @classmethod
    def _reject_deleted_repeg_keys(cls, data):
        # Same pattern as `RiskConfig._reject_removed_short_cap_keys`:
        # BaseModel's default `extra="ignore"` would let a settings.yaml still
        # carrying the deleted key load silently, and an operator would
        # believe a ladder length they set was in force. Fail loudly.
        if isinstance(data, dict) and "repeg_max_attempts" in data:
            raise ValueError(
                "execution.repeg_max_attempts was removed 2026-09-12: the "
                "entry reprice is a single replace by design (see "
                "ExecutionConfig). Delete the key from the settings file."
            )
        return data

    # Owner ruling 2026-10-15: whole shares only. Supersedes the 2026-09-01
    # §11.1 ratification and the 2026-09-02 acceptance of an unprotected
    # overnight sub-share remainder. See config/settings.yaml.
    fractional_enabled: bool = False
    """Master switch for exact (fractional) entry sizing. OFF by default:
    owner ruling 2026-10-15, whole shares only.

    Alpaca carries a fractional order only as DAY (never GTC, never a
    trailing stop — measured 2026-09-01), so a sub-share remainder has no
    stop overnight. With this off every new entry is floored to whole
    shares and an entry that floors to zero is skipped as `qty_zero`.

    Turning it back on needs a new owner ruling. Even when on, a symbol is
    only sized fractionally when the broker confirms `fractionable` for it
    (`get_fractionability`, which fails CLOSED)."""

    fractional_share_decimals: int = Field(default=4, ge=1, le=9)
    """Decimal places an exact share count is FLOORED to (never rounded up —
    rounding up would spend more risk budget than the sizing math allowed).
    4dp is under a tenth of a cent of notional on any price this desk
    trades, so the residual rounding tax is immaterial while the number
    stays short enough to read in a log line and in a Telegram alert."""

    fill_stream_enabled: bool = False
    """Master switch for the live `trade_updates` websocket fill feed
    (`_TradeUpdatesHub` in `src/execution/broker.py`).

    ON in `config/settings.yaml` since 2026-09-18. The field default stays
    FALSE deliberately: a config that does not mention the socket, and
    every test double that constructs this model bare, must not open one.
    `AlpacaBroker._fill_stream_enabled` defaults false for the same reason.

    The socket pushes a fill the instant it happens; with it off the desk
    only learns of a fill on its next REST poll, which
    on the bounded polling path can be up to half an hour later. The REST
    path is untouched and remains the fallback for any wait the socket
    cannot serve.

    It was OFF from 2026-09-17 because the socket had never once
    authenticated — 1,017 `failed to authenticate` occurrences across the
    retained production logs and zero successes. The cause was not the
    connection's timing (six pull requests adjusted that; none of them
    could have worked). The process held a 29-character PLACEHOLDER
    credential containing the literal word `placeholder`, and neither of
    the two mechanisms that could have substituted a real one applies to
    this socket:

      1. Alpaca authenticates the stream with an in-band websocket
         MESSAGE, not a handshake header, while the local gateway that
         substitutes the real key rewrites HTTP HEADERS — so it cannot
         reach the credential at all.
      2. The installed `alpaca-py` stream is built on `websockets.legacy`,
         which has no proxy support (that arrived in websockets 15.0, and
         only in the asyncio and sync clients), so the socket never
         traversed the gateway either.

    Real credential files were delivered 2026-09-18 and the process now
    reads them directly, so that single blocker is gone and this flag was
    the only remaining thing holding the socket shut. This flip changes no
    timeout, poll interval, retry count or ceiling, and nothing about
    credential handling.

    A refusal is now diagnosable rather than silent: the SDK throws the
    rejection payload away and raises a bare `ValueError`, which the desk
    used to log as `status=unknown` — indistinguishable from a transport
    fault, and the reason this took a fortnight to identify.
    `_install_trading_stream_auth_diagnostics` preserves Alpaca's own
    `message`/`status` and logs
    `trade_updates authentication REJECTED by broker`, with the key's
    length and first two characters only — never the value, never the
    secret. A successful handshake logs
    `trade_updates websocket authenticated`.

    CEILINGS ADDED 2026-09-18 (after the storm audit). Enabling this flag
    no longer risks an unbounded reconnect loop. The installed alpaca-py
    retries a failed handshake every 10ms with no backoff and no limit of
    its own — that is what produced 32,896 attempts and 32,666 HTTP 429
    rejections on 2026-09-15 — so `src/execution/broker.py` now enforces
    both a per-session and a per-day attempt ceiling of its own, treats a
    429 as a rate-limit stand-down rather than a transport retry, and when
    a ceiling is reached stops the socket for the day, tells the owner once
    in plain words, and leaves fills to the bounded REST path. See the
    `_STREAM_ATTEMPT_CEILING_*` constants there for each number's source."""
