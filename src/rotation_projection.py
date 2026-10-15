"""Projected post-sale book, cash and entry cost for rotation (lifted VERBATIM from pipeline_rotation_exec)."""

from __future__ import annotations


class ProjectionRefused(Exception):
    """An exit being projected has no usable live quote side.

    Every ordinary exit is a plain DAY MARKET order (src/exit_quote.py), so a
    SELL fills at the bid and a COVER at the ask. Without that side the
    proceeds are unknown, and the projection refuses with this reason rather
    than credit a guessed price.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _exit_fill_side(decision, symbol: str, quotes) -> tuple[bool, float]:
    """`(covering, price)` — the live side a market exit fills at: bid for a
    SELL, ask for a COVER. Raises `ProjectionRefused` when that side is
    missing, non-positive or non-finite."""
    from src.stage_execution_parts.entry_order_pricing import quote_side_paid

    covering = str(getattr(decision, "action", "") or "").upper() == "COVER"
    quote = (quotes or {}).get(symbol) or {}
    # quote_side_paid returns the bid when its first argument is True.
    price = quote_side_paid(not covering, quote.get("bid"), quote.get("ask"))
    if price is None:
        side = "ask" if covering else "bid"
        action = "COVER" if covering else "SELL"
        raise ProjectionRefused(
            f"no live {side} for {symbol}: a {action} is a market order that fills at the {side}, "
            f"so its proceeds cannot be projected without it, and the projection refuses rather than guess"
        )
    return covering, price


def _projected_sale_qty(decision, position) -> float:
    """How many shares `decision` will actually take off `position`.

    Mirrors `ExecutionStage._run_session`'s own SELL sizing exactly —
    whole-share flooring on a whole-share position, the `>= position` full-
    exit promotion, the `allocation_pct == 0` ambiguity skip — because a
    projection that sized a sale differently from the loop that places it
    would be describing a book that never exists. Returns 0.0 for anything
    the loop would skip.
    """
    try:
        held_qty = float(getattr(position, "qty", 0.0) or 0.0)
    except (TypeError, ValueError):
        return 0.0
    # The action and the position's SIGN have to agree, exactly as the two
    # execution loops require. The SELL loop refuses a SELL on a short
    # (`existing[0].qty <= 0: continue`) and the COVER loop refuses a COVER
    # on a long — so a projection that closed either one would remove
    # exposure the real session keeps, and a book that keeps a losing
    # position the projection dropped is more negative than the projection
    # said. Both directions are unsafe; both are refused here.
    #
    # A blanket `abs()` was the over-correction of the opposite bug, where
    # sizing off the SIGNED quantity made every COVER a no-op.
    covering = str(getattr(decision, "action", "") or "").upper() == "COVER"
    if covering and held_qty >= 0:
        return 0.0  # a COVER on a long: the COVER loop skips it
    if not covering and held_qty <= 0:
        return 0.0  # a SELL on a short: the SELL loop skips it
    held_qty = abs(held_qty)
    if held_qty <= 0:
        return 0.0
    try:
        pct = float(getattr(decision, "allocation_pct", 0.0) or 0.0)
    except (TypeError, ValueError):
        return 0.0
    if pct == 0:
        # The loop logs this as ambiguous and skips it, so nothing is sold.
        return 0.0
    if 0 < pct < 100:
        qty = held_qty * (pct / 100.0)
        if float(held_qty).is_integer():
            qty = max(1.0, float(int(qty)))
        if qty <= 0:
            return 0.0
        return min(qty, held_qty)
    return held_qty


def _projected_post_sale_book(positions, total_value: float, sell_decisions: list, cover_decisions: list, *, quotes):
    """The held book and the equity as they will be once THIS SESSION'S
    exits have gone through — the state the downstream gates will actually
    read, projected before any of them has been submitted.

    Board item 39. The gates that can refuse a rotation's replacement BUY
    are measured from the book the desk will be holding once the sale has
    gone through, not the one it holds while proposing it: the deployment
    budget reads the remaining positions' gross exposure and the remaining
    settled cash, and the sizing reads the equity those are measured
    against. A check fed PRE-sale state is blind to the state change it
    depends on, which is exactly why attempt 2 on this item was unsafe by
    construction, and why this builds the post-sale book instead of
    re-implementing the gates against the pre-sale one.

    **Exactly the exits it is handed are applied, and no others.** The
    rotation gate hands it ONE decision — the rotation's own close — and
    relies on a fresh `_refresh_account_state()` read for everything else
    the session has already done. Projecting the other exits would be
    guessing at fills nobody controls; measuring them is free, because the
    rotation's close is ordered last.

    Returns `(projected_positions, equity_for_weights)`.

      * `equity_for_weights` is `total_value` LESS the concession each exit
        gives up against its mark: the distance from the mark to the LIVE
        quote side the market exit fills at (bid for a SELL, ask for a
        COVER), read from `quotes` — `{symbol: {"bid": ..., "ask": ...}}` as
        `src.exit_quote.read_exit_quote` returns it. A missing side raises
        `ProjectionRefused`; no fixed offset stands in for it. A sale is otherwise mark-to-market neutral: it
        converts a marked position into the cash that position was already
        marked at, so the book's composition changes and its total does
        not. The concession is the one real equity effect of executing, it
        is knowable, and it is subtracted rather than ignored.
        **Direction, measured rather than assumed (adversary review,
        2026-09-23).** `config/settings.yaml` ships `allow_margin: true`, so
        `_entry_deployment_budget` returns `ceiling_x * equity - held_gross`
        and never reads cash at all. The order ceiling therefore falls by
        between 0.65 and 2.0 times any reduction in equity (the §11.2 rung
        and `max_position_pct: 65`), while the replacement's estimated cost
        falls only by the allocation percentage of it. Subtracting the
        concession TIGHTENS this gate; leaving it out loosens it. An
        earlier version of this docstring asserted the opposite and was
        wrong — it reasoned about the settled-cash branch, which the
        shipped configuration does not execute.

    Cover decisions are applied the same way: a COVER closes a short, which
    also removes that name from the held book.
    """
    by_symbol = {}
    for decision in list(sell_decisions or []) + list(cover_decisions or []):
        symbol = str(getattr(decision, "symbol", "") or "").strip().upper()
        if symbol:
            by_symbol.setdefault(symbol, decision)

    projected = []
    concession = 0.0
    for position in positions or []:
        symbol = str(getattr(position, "symbol", "") or "").strip().upper()
        decision = by_symbol.get(symbol)
        if decision is None:
            projected.append(position)
            continue
        held_qty = float(getattr(position, "qty", 0.0) or 0.0)
        sold = _projected_sale_qty(decision, position)
        if sold <= 0:
            projected.append(position)
            continue
        try:
            price = float(getattr(position, "current_price", 0.0) or 0.0)
        except (TypeError, ValueError):
            price = 0.0
        _covering, fill = _exit_fill_side(decision, symbol, quotes)
        if price > 0:
            # What the market exit gives up against the mark: the distance
            # to the live side it fills at. Counted as a cost in either
            # direction, so the projection never credits a quote above the
            # mark as a gain.
            concession += abs(fill - price) * abs(sold)
        remaining = abs(held_qty) - abs(sold)
        if remaining <= 0 or held_qty == 0:
            continue  # position gone
        projected.append(_scaled_position(position, remaining / abs(held_qty)))

    return projected, max(0.0, float(total_value) - concession)


def _scaled_position(position, remaining_fraction: float):
    """A copy of `position` holding `remaining_fraction` of what it holds.

    Used only for a PARTIAL exit. Quantity and market value scale by the
    fraction, because selling half a position leaves half of it on the
    books, and `gross_exposure` — which is what the deployment budget
    measures the remaining book with — reads market value. The P&L fields
    are scaled with them for consistency of the object rather than because
    any gate now reads them: the projected account day-change that used to
    read `unrealized_intraday_pnl` went with the account-level loss halt
    (PR #584, retired-ok). Any field this does not know about is carried
    through unchanged.

    `copy.copy` rather than a constructor call: `Position` is not stable
    across this repo's fixtures (several tests use simple stand-ins), and a
    projection helper must not be the thing that decides what a position
    class looks like.
    """
    import copy

    clone = copy.copy(position)
    for field_name in ("qty", "market_value", "unrealized_intraday_pnl", "unrealized_pnl", "cost_basis"):
        value = getattr(clone, field_name, None)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        try:
            setattr(clone, field_name, float(value) * remaining_fraction)
        except Exception:  # noqa: BLE001
            # A frozen or property-backed field: leave it. The numerator
            # reads `unrealized_intraday_pnl`, and a field that could not
            # be scaled down is left at its FULL value, which overstates
            # the remaining book rather than understating it.
            continue
    return clone


def _projected_post_sale_cash(cash: float, positions, sell_decisions, cover_decisions, *, quotes) -> float:
    """Settled cash once this session's exits have gone through — a LOWER
    bound, deliberately.

    Called with the rotation's own close and nothing else — every other
    exit is already reflected in the `cash` this is handed, because that
    number comes from a broker read taken after they ran.

    A SELL adds quantity x the LIVE BID; a COVER SPENDS quantity x the LIVE
    ASK to buy the borrowed shares back, so it is subtracted. Both are market
    orders (src/exit_quote.py) and fill at that side; `quotes` is
    `{symbol: {"bid": ..., "ask": ...}}`, and a missing side raises
    `ProjectionRefused` rather than guessing the proceeds. The cash sweep is not modelled at
    all: it can only liquidate the park INTO cash, never out of it, so
    leaving it out can only understate what is deployable. Understating
    refuses a rotation that would have worked; overstating sells a position
    to fund an order that is then refused.

    This is live on the settled-cash branch of `_entry_deployment_budget`
    only — the ladder branch compares gross exposure and never reads cash.
    That branch is reached whenever the gross ceiling cannot be resolved,
    and whenever `allow_margin` is set back to false.
    """
    try:
        cash = float(cash or 0.0)
    except (TypeError, ValueError):
        cash = 0.0
    by_symbol = {}
    for decision in list(sell_decisions or []) + list(cover_decisions or []):
        symbol = str(getattr(decision, "symbol", "") or "").strip().upper()
        if symbol:
            by_symbol.setdefault(symbol, decision)
    for position in positions or []:
        symbol = str(getattr(position, "symbol", "") or "").strip().upper()
        decision = by_symbol.get(symbol)
        if decision is None:
            continue
        sold = _projected_sale_qty(decision, position)
        if sold <= 0:
            continue
        covering, fill = _exit_fill_side(decision, symbol, quotes)
        cash += (-1.0 if covering else 1.0) * fill * abs(sold)
    return cash


def _projected_entry_cost(decision, equity: float, *, budget_is_gross: bool) -> float:
    """What an entry earlier in the same session will take out of the
    deployment pool before the rotation's own buy reaches it.

    A SHORT is never SIZED by the entry budget (D11), but it still DRAWS
    the pool whenever that pool is the ladder's gross headroom rather than
    settled cash — the submit loop's own rule is
    `if budget_is_gross or not is_short: entry_budget -= estimated_cost`,
    because a short occupies gross exactly as a long does. Excluding it
    outright over-stated the pool by the whole short, which is the unsafe
    direction: the projection clears, reality refuses, and the position has
    already been sold.

    The charge is the full allocation, which is an UPPER bound on the real
    draw (the submit loop takes `min(qty_by_alloc, qty_by_risk)` and every
    later adjustment moves the quantity down, and it only draws at all once
    the broker accepts). Over-charging shrinks the pool, which refuses a
    rotation that would have worked — the side to be wrong on.
    """
    if str(getattr(decision, "action", "") or "").upper() == "SHORT" and not budget_is_gross:
        return 0.0
    try:
        allocation_pct = float(getattr(decision, "allocation_pct", 0.0) or 0.0)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, float(equity) * allocation_pct / 100.0)
