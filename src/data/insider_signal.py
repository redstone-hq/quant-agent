"""Routine-versus-opportunistic classification for SEC Form 4 transactions.

Evidence basis: ``docs/RESEARCH_FINDINGS.md`` section 1, which follows Cohen,
Malloy & Pomorski, *Decoding Inside Information* (JF 2012). More than half of
Form 4 activity is **routine** — the same insider trading in the same calendar
month year after year — and routine trades carry no predictive power. Removing
them is what leaves the ~82bps/month opportunistic residual.

Three deliberate departures from the folk version of this filter, all of them
taken from ``RESEARCH_FINDINGS.md`` rather than from intuition:

1. **A 10b5-1 checkbox is NOT on its own a routine marker.** The research
   document is explicit: "10b5-1 plans are not a clean noise filter. For
   high-value sales, planned and discretionary transactions show similar
   opportunism, and the 2022 SEC reform did not reduce abnormal returns on
   insider selling." So the flag only ever *supports* a routine label for a
   disposition that is already proportionally immaterial; a large sale is
   never demoted for being planned. The flag is still recorded in the reason
   text so an operator can see it was considered.

2. **Compensation codes need no branch here.** ``SECForm4Provider`` already
   restricts parsing to non-derivative ``P``/``S`` rows, so option exercises
   (``M``), awards (``A``), tax withholding (``F``), gifts (``G``) and
   dispositions to the issuer (``D``/``X``) never reach this module.
   ``_non_open_market`` remains as a contract guard, not as a live filter.

3. **Sell size relative to the holding is REPORTED, never a cutoff.** This
   module previously relabelled a small proportional sale ROUTINE, assigned
   weight 0.0 and dropped it out of the ranking. That was removed on
   2026-09-13; transaction size cannot establish whether a sale is routine.

   `holdings_fraction` now reports only the arithmetic visible in one Form 4
   row: shares transacted divided by that insider's reconstructed holding
   before the transaction. The accompanying bands remain legacy descriptive
   buckets and gate nothing. They are not Scott & Xu evidence: that paper's
   ratio is a net, per-stock-quarter figure over a six-month formation window
   against holdings aggregated across insiders. One Form 4 row is not that
   object, so none of the paper's return findings belongs in a per-row detail
   or prompt. Item 90 records the unsupported bucket edges and the route to
   remove them while retaining the exact ratio.

The classifier is pure Python, deterministic, and makes no model call.

The rule is the per-trade version in Cohen, Malloy & Pomorski (2012), NBER
w16454 Table A3 (p.11). An insider-stock pair is classifiable only when the
insider traded that stock at least once in each of the 3 preceding calendar
years (footnote 9: the paper tested 1 to 5 years with similar results; 3 is
its own main specification). A classifiable trade is routine when the insider
also traded that stock in the same calendar month in each of those years,
otherwise opportunistic. Direction is not split: the paper says "a trade".
A pair without that history is indeterminate ("insufficient history").

The one number (calendar-routine years) is an operator-tunable setting, not a fixed number in this
file — the owner rejects hardcoded thresholds on sight, and a threshold able
to change classification output is exactly the kind of number that belongs
in config. Defaults live on `SmartMoneyConfig` in `src/config.py` (fields
prefixed `insider_`) and arrive here as an `InsiderSignalThresholds`,
constructed once by the caller (`SECForm4Provider.__init__`) from the loaded
`AppConfig`, mirroring how `SECForm4Provider` already receives every other
smart-money threshold as an explicit constructor argument rather than
reading config itself. Passing `thresholds=None` (the default, and every
call site in this module's own tests) falls back to
`InsiderSignalThresholds()`, whose field defaults equal the values this
module used before 2026-08-28 — so unconfigured behaviour is unchanged.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date

OPPORTUNISTIC = "opportunistic"
ROUTINE = "routine"
INDETERMINATE = "indeterminate"

# Sort order only, best first. Cohen, Malloy & Pomorski (2012) give no weight to
# a trade they could not classify, so there is no numeric weight: opportunistic,
# then indeterminate, then routine. Mirrors the analyst's _SIGNAL_CLASS_RANK
# (src/data must not import src/agents).
SIGNAL_CLASS_ORDER = (OPPORTUNISTIC, INDETERMINATE, ROUTINE)


def signal_class_sort_key(label: str) -> int:
    """Position in SIGNAL_CLASS_ORDER; an unlabelled row sorts as indeterminate."""
    return SIGNAL_CLASS_ORDER.index(label if label in SIGNAL_CLASS_ORDER else INDETERMINATE)


# Roles that carry published signal. CFO purchases beat CEO purchases; a
# reporting owner with no officer/director/10% standing is a weaker source.
_INSIDE_ROLE_MARKERS = ("officer", "director", "tenpercentowner")


@dataclass(frozen=True)
class InsiderSignalThresholds:
    """The classifier's operator-tunable numbers, in one place.

    Field defaults are the values this module hardcoded before 2026-08-28;
    changing behaviour now requires an explicit `SmartMoneyConfig` override
    (`config/settings.yaml` -> `smart_money.insider_*`), not a code edit. See
    the corresponding fields on `SmartMoneyConfig` in `src/config.py` for the
    evidence basis behind each default — kept in sync by
    `SECForm4Provider.__init__`, the only production call site that builds
    one of these from loaded config.
    """

    calendar_routine_years: int = 3


_DEFAULT_THRESHOLDS = InsiderSignalThresholds()


@dataclass(frozen=True)
class InsiderPriorTrade:
    """One earlier open-market trade by the same insider in the same issuer."""

    transaction_date: date
    direction: str


class InsiderHistory:
    """Per-(insider, issuer) prior open-market trades.

    Intentionally a plain in-memory index rather than a database read: the
    classifier must stay free, deterministic and unit-testable.
    """

    def __init__(
        self,
        trades: Mapping[tuple[str, str], Sequence[InsiderPriorTrade]] | None = None,
    ):
        self._trades: dict[tuple[str, str], list[InsiderPriorTrade]] = {}
        for key, values in (trades or {}).items():
            actor_cik, symbol = key
            normalized = (str(actor_cik or "").strip(), str(symbol or "").strip().upper())
            if not normalized[0] or not normalized[1]:
                continue
            self._trades[normalized] = sorted(
                values,
                key=lambda item: item.transaction_date,
            )

    def __bool__(self) -> bool:
        return bool(self._trades)

    def as_mapping(self) -> dict[tuple[str, str], list[InsiderPriorTrade]]:
        """Copy of the index, for callers merging in another source."""
        return {key: list(values) for key, values in self._trades.items()}

    def prior_trades(
        self,
        actor_cik: str,
        symbol: str,
        *,
        direction: str | None = None,
        before: date,
    ) -> list[InsiderPriorTrade]:
        key = (str(actor_cik or "").strip(), str(symbol or "").strip().upper())
        return [
            trade
            for trade in self._trades.get(key, ())
            if (direction is None or trade.direction == direction) and trade.transaction_date < before
        ]

    @classmethod
    def from_observations(cls, observations) -> "InsiderHistory":
        """Build an index from anything exposing the Form 4 row attributes."""
        grouped: dict[tuple[str, str], list[InsiderPriorTrade]] = {}
        for row in observations or []:
            actor_cik = str(getattr(row, "actor_cik", "") or "").strip()
            symbol = str(getattr(row, "symbol", "") or "").strip().upper()
            direction = str(getattr(row, "direction", "") or "").strip()
            transaction_date = getattr(row, "transaction_date", None)
            if not actor_cik or not symbol or not isinstance(transaction_date, date):
                continue
            if direction not in {"buy", "sell"}:
                continue
            grouped.setdefault((actor_cik, symbol), []).append(
                InsiderPriorTrade(transaction_date=transaction_date, direction=direction)
            )
        return cls(grouped)


@dataclass(frozen=True)
class InsiderSignalClass:
    """A label, the machine-readable rule that produced it, and the why."""

    label: str
    reason: str
    detail: str

    @classmethod
    def of(cls, label: str, reason: str, detail: str) -> "InsiderSignalClass":
        return cls(label=label, reason=reason, detail=detail)


def _sell_fraction(shares: float | None, post_shares: float | None) -> float | None:
    """Fraction of the insider's pre-transaction holding that was disposed."""
    if shares is None or post_shares is None or shares <= 0:
        return None
    pre_shares = shares + post_shares
    if pre_shares <= 0:
        return None
    return shares / pre_shares


def _buy_fraction(shares: float | None, post_shares: float | None) -> float | None:
    """Purchase size as a fraction of the holding the insider already had.

    The descriptive denominator is the insider's *pre*-transaction holding —
    ``post - shares`` for an acquisition. A non-positive denominator means the
    insider held nothing beforehand, so no ratio exists: ``None`` is returned
    and the band says so rather than pretending the fraction is infinite.
    """
    if shares is None or post_shares is None or shares <= 0:
        return None
    pre_shares = post_shares - shares
    if pre_shares <= 0:
        return None
    return shares / pre_shares


def holdings_fraction(observation) -> tuple[float | None, bool]:
    """Trade size relative to the insider's own holding, and a no-prior flag.

    Reported for both directions and for every row, independently of which
    classification rule fired: the exact ratio is descriptive context even
    when the label was decided by something else. ``(None, False)`` means the
    filing did not carry enough to compute one. The flag is True only for a
    purchase by an insider who held nothing beforehand: no ratio exists, and
    that is a distinct fact from a missing one. No bucket is derived.

    Sells use the pre-transaction holding ``shares + post_transaction_shares``
    as the denominator; buys use ``post_transaction_shares - shares``. Both
    reconstruct the same quantity — what the insider held before acting —
    from the two Form 4 numbers when both are present.
    """
    if str(getattr(observation, "stream", "") or "") != "insider":
        return None, False
    direction = str(getattr(observation, "direction", "") or "")
    shares = getattr(observation, "shares", None)
    post_shares = getattr(observation, "post_transaction_shares", None)
    if direction == "sell":
        return _sell_fraction(shares, post_shares), False
    if direction == "buy":
        fraction = _buy_fraction(shares, post_shares)
        no_prior = (
            fraction is None
            and shares is not None
            and post_shares is not None
            and shares > 0
            and post_shares - shares <= 0
        )
        return fraction, no_prior
    return None, False


def _has_inside_role(roles) -> bool:
    for role in roles or ():
        collapsed = str(role or "").strip().lower().replace(" ", "").replace("_", "")
        if any(marker in collapsed for marker in _INSIDE_ROLE_MARKERS):
            return True
    return False


def _history_verdict(
    prior: Sequence[InsiderPriorTrade],
    transaction_date: date,
    years: int,
) -> bool | None:
    """CMP 2012 Table A3: None = not classifiable, True = routine, False = opportunistic."""
    seen = {(trade.transaction_date.year, trade.transaction_date.month) for trade in prior}
    seen_years = {year for year, _ in seen}
    offsets = range(1, years + 1)
    if any(transaction_date.year - offset not in seen_years for offset in offsets):
        return None
    return all((transaction_date.year - offset, transaction_date.month) in seen for offset in offsets)


def classify_transaction(
    observation,
    history: InsiderHistory | None = None,
    thresholds: InsiderSignalThresholds | None = None,
) -> InsiderSignalClass:
    """Label one Form 4 row routine or opportunistic, with the reason.

    Rules are evaluated in precedence order and the first match wins, so the
    reason on the result is always the single rule that decided it.

    ``thresholds`` carries every operator-tunable number this classifier
    uses; ``None`` (the default) falls back to ``InsiderSignalThresholds()``,
    whose field defaults are this module's pre-2026-08-28 hardcoded values.
    """
    thresholds = thresholds or _DEFAULT_THRESHOLDS
    if str(getattr(observation, "stream", "") or "") != "insider":
        return InsiderSignalClass.of(
            INDETERMINATE,
            "not_form4",
            "Not an SEC Form 4 row; the routine test does not apply.",
        )

    code = str(getattr(observation, "transaction_code", "") or "").upper()
    direction = str(getattr(observation, "direction", "") or "")
    if code not in {"P", "S"}:
        return InsiderSignalClass.of(
            ROUTINE,
            "non_open_market_code",
            f"Transaction code {code or 'missing'!r} is not an open-market "
            "purchase or sale (grants, option exercises, tax withholding and "
            "gifts carry no directional signal).",
        )

    shares = getattr(observation, "shares", None)
    price = getattr(observation, "price_per_share", None)
    value = getattr(observation, "transaction_value_usd", None)
    if value is None or shares is None:
        return InsiderSignalClass.of(
            INDETERMINATE,
            "incomplete_amounts",
            "Filing omits share count or transaction value; routine status cannot be established.",
        )
    if price is not None and price <= 0:
        return InsiderSignalClass.of(
            ROUTINE,
            "zero_price_transaction",
            "Reported at a zero price, so no capital was risked or realised at market.",
        )

    transaction_date = getattr(observation, "transaction_date", None)
    actor_cik = str(getattr(observation, "actor_cik", "") or "").strip()
    symbol = str(getattr(observation, "symbol", "") or "").strip().upper()
    prior: list[InsiderPriorTrade] = []
    if history and isinstance(transaction_date, date) and actor_cik and symbol:
        # Both directions: the paper's rule is on "a trade", not a buy or a sale.
        prior = history.prior_trades(actor_cik, symbol, before=transaction_date)

    routine = (
        _history_verdict(prior, transaction_date, thresholds.calendar_routine_years)
        if isinstance(transaction_date, date)
        else None
    )
    years = thresholds.calendar_routine_years
    if routine is None:
        return InsiderSignalClass.of(
            INDETERMINATE,
            "insufficient_history",
            f"Fewer than {years} preceding calendar years of this insider's trades in {symbol or 'this stock'}; "
            "Cohen/Malloy/Pomorski (2012) cannot classify the trade as routine or opportunistic.",
        )
    if routine:
        return InsiderSignalClass.of(
            ROUTINE,
            "calendar_routine",
            f"Same insider traded {symbol} in {transaction_date:%B} in each "
            f"of the {years} preceding years — a routine trader under "
            "Cohen/Malloy/Pomorski, which carries no predictive power.",
        )

    is_10b5_1 = getattr(observation, "is_10b5_1", None)
    if direction == "sell":
        fraction = _sell_fraction(shares, getattr(observation, "post_transaction_shares", None))
        if fraction is None:
            return InsiderSignalClass.of(
                INDETERMINATE,
                "unknown_holding",
                "Post-transaction holding is missing, so the sale cannot be sized against the insider's position.",
            )
        planned = (
            " The 10b5-1 flag is set but is deliberately not treated as a "
            "noise marker: planned and discretionary high-value sales show "
            "similar opportunism."
            if is_10b5_1
            else ""
        )
        # No cutoff. The sale has the full history and is not in the same
        # calendar month each year (Cohen/Malloy/Pomorski), so it is discretionary in the only sense this taxonomy
        # defines. The raw per-row holding ratio is descriptive context; it
        # carries no Scott/Xu return inference because the units differ.
        return InsiderSignalClass.of(
            OPPORTUNISTIC,
            "discretionary_sale",
            f"Discretionary sale of {fraction:.1%} of the insider's holding, not in the same "
            f"calendar month as in each of the {years} preceding years.{planned}",
        )

    role_note = (
        ""
        if _has_inside_role(getattr(observation, "actor_roles", None))
        else " Reporting owner holds no officer, director or 10% role, so the "
        "signal is weaker than a named-officer purchase."
    )
    # Reported, never gated on: this is the arithmetic from the present Form 4
    # row, not a transfer of an aggregate study's return finding.
    buy_fraction, buy_no_prior = holdings_fraction(observation)
    if buy_fraction is not None:
        size_note = f" Adds {buy_fraction:.1%} to the insider's existing holding."
    elif buy_no_prior:
        size_note = " The insider held none of this stock beforehand, so there is no size-relative-to-holdings ratio."
    else:
        size_note = ""
    return InsiderSignalClass.of(
        OPPORTUNISTIC,
        "opportunistic_purchase",
        f"Discretionary open-market purchase of ${value:,.0f} not in the same calendar month "
        f"as in each of the {years} preceding years.{size_note}{role_note}",
    )
