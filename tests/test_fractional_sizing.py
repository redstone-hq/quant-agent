"""Spec §11.1 — fractional shares are ON, and the three guards that let them be.

WHAT THIS PINS

Whole-share rounding is a silent, constant tax on every position this desk
opens: the ratified example is a 6% target that arrived as 3.84% of the book.
`test_whole_share_rounding_tax_is_the_thing_being_removed` reproduces exactly
that arithmetic and fails if flooring ever comes back.

Turning it on is only acceptable because three guards bound the one real
risk — that the protective stop, which is now a SEPARATE order placed after
the fill, never lands at all:

  guard 1  the stop retries IMMEDIATELY and hard, in the same call
  guard 2  a stop that still fails ALERTS THE OWNER, not a log line
  guard 3  the 30-minute sweep reports NO STOP AT ALL as a different and
           worse condition than STOP PRESENT BUT MIS-SIZED

and because eligibility FAILS CLOSED: a symbol is sized fractionally only
when the broker itself confirms `fractionable`. Unknown, unreadable or
un-askable all mean whole shares. Fractional-by-assumption is the state this
must never reach.

HYBRID FRACTIONAL STOPS (section 7, added 2026-09-01) extend those three
guards rather than replacing them. The broker was probed against the live
paper account: it refuses a fractional GTC order outright and accepts a
fractional DAY one. So a fractional position is covered by TWO orders — a
durable GTC stop over the whole shares and a DAY stop over the sub-share
remainder — and the DAY stop lapses at every close by design.

That makes the ALERTING the load-bearing part, not the placement. A lapsed
overnight DAY stop is expected and must stay silent; the same stop missing
during market hours, or a missing whole-share GTC leg at any hour, must
still alert. Section 7 pins all three from both sides — including the case
that must alert, so the suppression cannot degrade into "never alert about
fractional".
"""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

import functools

from src.execution.broker import AlpacaBroker
from src.models import PortfolioDecision, ReasoningChain, TradeDecision
from src.pipeline import TradingPipeline
from src.pipeline_context import RunContext
from src.pipeline_stages import ExecutionStage, _size_shares
from tests.pipeline_factory import build_pipeline


# ==========================================================================
# Fixtures
# ==========================================================================


def _rc() -> ReasoningChain:
    return ReasoningChain(
        macro_filter="m",
        news_check="n",
        earnings_check="e",
        signal_conflicts="s",
        sizing_logic="z",
        portfolio_balance="b",
        cash_target="c",
    )


def _pipeline(
    *,
    live_price: float,
    cash: float = 1_000_000.0,
    fractional_enabled: bool = True,
    fractionable: dict | Exception | None = None,
    decimals: int = 4,
) -> MagicMock:
    """An ExecutionStage pipeline with the §11.1 knobs made real.

    `pipeline.config` on a bare MagicMock answers every attribute with a
    truthy mock, which would make `fractional_enabled` look permanently ON
    and `fractional_share_decimals` un-intable. Both are set explicitly here
    so each test states the configuration it is actually testing.
    """
    pipeline = MagicMock()
    pipeline.broker.list_open_orders_checked.return_value = (True, [])  # listing read, nothing open
    pipeline.broker.get_latest_price.return_value = live_price
    pipeline.broker.get_latest_quote.return_value = {"bid_price": live_price, "ask_price": live_price}
    pipeline._format_qty = lambda q: str(q)
    pipeline._order_accepted.return_value = True
    pipeline._refresh_account_state.return_value = (
        {"cash": cash, "portfolio_value": 10_000.0},
        [],
        {},
    )
    pipeline.config.execution.fractional_enabled = fractional_enabled
    pipeline.config.execution.fractional_share_decimals = decimals
    if isinstance(fractionable, Exception):
        pipeline.broker.get_fractionability.side_effect = fractionable
    else:
        pipeline.broker.get_fractionability.return_value = fractionable
    pipeline.broker.submit_order.return_value = {
        "id": "ord-1",
        "status": "accepted",
    }
    return pipeline


def _ctx(decisions, *, cash=1_000_000.0, total_value=10_000.0) -> RunContext:
    ctx = RunContext.start("morning")
    ctx.cash = cash
    ctx.total_value = total_value
    ctx.last_equity = total_value
    ctx.positions = []
    ctx.decision_id = "run-x-dec-frac"
    ctx.portfolio_decision = PortfolioDecision(
        reasoning_chain=_rc(),
        decisions=decisions,
        portfolio_view="t",
    )
    # No bars: the entry ATR stop floor needs them, and this file is about
    # share COUNTS, not stop placement.
    ctx.symbols_bars = {}
    return ctx


#: The ratified example, made arithmetic. A $10,000 book, a 6% target and a
#: $384 share price want 1.5625 shares. Floored to whole shares that is ONE
#: share — $384, or 3.84% of the book. 6% asked for, 3.84% delivered.
_PRICE = 384.0
_ALLOC_PCT = 6.0
_EXACT_SHARES = 1.5625


def _buy(symbol="V", price=_PRICE, alloc=_ALLOC_PCT, stop=364.0):
    """A BUY whose risk-based size is deliberately LARGER than its
    allocation-based size, so allocation is the binding constraint and the
    number under test is unambiguous."""
    return TradeDecision(
        action="BUY",
        symbol=symbol,
        allocation_pct=alloc,
        entry_price=price,
        stop_loss=stop,
        take_profit=price * 1.3,
        reasoning="fractional sizing",
    )


def _submitted_qty(pipeline) -> float:
    return pipeline.broker.submit_order.call_args.kwargs["qty"]


# ==========================================================================
# 1. Exact sizing — the tax, and its removal
# ==========================================================================


def test_exact_fractional_sizing_delivers_the_requested_risk_share():
    pipeline = _pipeline(
        live_price=_PRICE,
        fractionable={"fractionable": True, "reason": "fractionable", "symbol": "V"},
    )

    orders = ExecutionStage(pipeline=pipeline).run(_ctx([_buy()]))

    assert len(orders) == 1
    assert _submitted_qty(pipeline) == pytest.approx(_EXACT_SHARES)
    # The point of the whole phase: the book gets the weight it asked for.
    delivered_pct = _submitted_qty(pipeline) * _PRICE / 10_000 * 100
    assert delivered_pct == pytest.approx(_ALLOC_PCT)


def test_whole_share_rounding_tax_is_the_thing_being_removed():
    """The regression guard §11.1 exists for. With fractional OFF this is the
    OLD behaviour, and the numbers are the ratified ones: 6% requested,
    3.84% delivered — a 36% haircut nobody chose. If flooring ever comes
    back on the fractional path, `test_exact_fractional_sizing...` above
    starts producing THIS number and fails."""
    pipeline = _pipeline(live_price=_PRICE, fractional_enabled=False)

    orders = ExecutionStage(pipeline=pipeline).run(_ctx([_buy()]))

    assert len(orders) == 1
    assert _submitted_qty(pipeline) == 1.0
    delivered_pct = _submitted_qty(pipeline) * _PRICE / 10_000 * 100
    assert delivered_pct == pytest.approx(3.84)
    assert delivered_pct < _ALLOC_PCT
    # The flag is a switch, not a suggestion: nothing was even asked of the
    # broker, so turning it off cannot cost an asset-directory call either.
    pipeline.broker.get_fractionability.assert_not_called()


def test_a_sub_one_share_position_is_taken_not_skipped():
    """Under whole-share sizing a position worth less than one share rounded
    to zero and was recorded as a `qty_zero` skip. Under exact sizing it is a
    legitimate, correctly-weighted position — and the funding preflight has
    to agree, or the BUY dies before the submit loop ever sees it."""
    pipeline = _pipeline(
        live_price=2_000.0,
        fractionable={"fractionable": True, "reason": "fractionable"},
    )
    ctx = _ctx([_buy(symbol="BRK-B", price=2_000.0, alloc=5.0, stop=1_900.0)])

    orders = ExecutionStage(pipeline=pipeline).run(ctx)

    assert len(orders) == 1
    assert _submitted_qty(pipeline) == pytest.approx(0.25)
    assert ctx.execution_skips == []


def test_a_9_9k_account_sizes_a_200_dollar_stock_without_rounding_to_zero():
    """Funnel census item 5 (docs/WORK.md): 3 of 68 proposals were recorded
    as `qty_zero` against "a ~$9.9k account and $200+ share prices" after
    fractional sizing was already enabled. Root-caused: every BUY on a
    broker-confirmed-fractionable symbol already produces a non-zero share
    count at this scale — `_fractional_sizing_allowed` is checked BEFORE
    `_size_shares` floors anything, and flooring to
    `fractional_share_decimals` (4dp) only reaches zero below a 0.0001-share
    raw quantity, far under any allocation this desk sizes. This test pins
    the account/price numbers from the census itself so the exact scenario
    that was reported dead stays alive. The residual ways `qty_zero` can
    still fire — a SHORT (a borrowed share cannot be fractional,
    `test_short_entries_are_always_whole_share`) or a symbol the broker does
    not confirm fractionable (`test_non_fractionable_symbol_falls_back_to_whole_shares`)
    — are both deliberate, documented exclusions, not this defect."""
    pipeline = _pipeline(
        live_price=205.0,
        fractionable={"fractionable": True, "reason": "fractionable"},
    )
    # `_pipeline`'s default `_refresh_account_state` hardcodes a $10,000
    # book; pin it to the census's own $9,900 so the executed quantity
    # reflects the actual scenario under test, not the fixture default.
    pipeline._refresh_account_state.return_value = (
        {"cash": 1_000_000.0, "portfolio_value": 9_900.0},
        [],
        {},
    )
    ctx = _ctx(
        [_buy(symbol="COST", price=205.0, alloc=1.0, stop=195.0)],
        total_value=9_900.0,
    )

    orders = ExecutionStage(pipeline=pipeline).run(ctx)

    assert len(orders) == 1
    # Floored to the configured 4dp, per `_size_shares` — never rounded up.
    assert _submitted_qty(pipeline) == pytest.approx(0.4829)
    assert _submitted_qty(pipeline) > 0
    assert ctx.execution_skips == []


def test_a_fractional_entry_is_not_re_pegged():
    """Alpaca's replace endpoint types qty as an int. Truncating would SHRINK
    a position the risk math already sized, so the replacement is refused and
    the original order stays authoritative."""
    with patch("src.execution.broker.TradingClient"):
        broker = AlpacaBroker("k", "s", paper=True)

    out = broker.replace_entry_limit("ord-1", 101.0, qty=1.5625)

    assert out["id"] is None
    assert out["status"] == "replace_unsupported_fractional_qty"
    broker.client.replace_order_by_id.assert_not_called()


def test_fractional_sizing_floors_it_never_rounds_up():
    """Rounding UP would spend more risk budget than the sizing math allowed.
    At 2 decimals, 1.5625 shares must become 1.56 — not 1.57."""
    pipeline = _pipeline(
        live_price=_PRICE,
        decimals=2,
        fractionable={"fractionable": True, "reason": "fractionable"},
    )

    ExecutionStage(pipeline=pipeline).run(_ctx([_buy()]))

    assert _submitted_qty(pipeline) == pytest.approx(1.56)


# ==========================================================================
# 2. Eligibility fails CLOSED
# ==========================================================================


def test_non_fractionable_symbol_falls_back_to_whole_shares():
    pipeline = _pipeline(
        live_price=_PRICE,
        fractionable={"fractionable": False, "reason": "not_fractionable", "symbol": "V"},
    )

    ExecutionStage(pipeline=pipeline).run(_ctx([_buy()]))

    assert _submitted_qty(pipeline) == 1.0


def test_unknown_fractionable_flag_falls_back_to_whole_shares():
    """The asset record came back but carries no `fractionable` field at all.
    "Absent" is not "true"."""
    pipeline = _pipeline(
        live_price=_PRICE,
        fractionable={"fractionable": False, "reason": "fractionable_unknown", "symbol": "V"},
    )

    ExecutionStage(pipeline=pipeline).run(_ctx([_buy()]))

    assert _submitted_qty(pipeline) == 1.0


def test_failed_fractionable_lookup_falls_back_to_whole_shares():
    """The lookup RAISED. A fractional order on a name that cannot take one
    is rejected outright by the broker, turning an approved trade into no
    trade — so an unanswerable question means whole shares, every time."""
    pipeline = _pipeline(
        live_price=_PRICE,
        fractionable=RuntimeError("alpaca 500"),
    )

    orders = ExecutionStage(pipeline=pipeline).run(_ctx([_buy()]))

    assert len(orders) == 1, "a broken lookup must not cost the trade"
    assert _submitted_qty(pipeline) == 1.0


def test_short_entries_are_always_whole_share():
    """A borrowed share cannot be fractional, so this is not a policy choice
    to expose — the broker is not even asked."""
    pipeline = _pipeline(
        live_price=200.0,
        fractionable={"fractionable": True, "reason": "fractionable"},
    )
    pipeline.broker.get_shortability.return_value = {
        "shortable": True,
        "easy_to_borrow": True,
        "reason": "eligible",
    }
    short = TradeDecision(
        action="SHORT",
        symbol="TSLA",
        allocation_pct=7.0,
        entry_price=200.0,
        stop_loss=210.0,
        take_profit=170.0,
        reasoning="short it",
    )

    ExecutionStage(pipeline=pipeline).run(_ctx([short]))

    qty = _submitted_qty(pipeline)
    assert float(qty).is_integer()
    pipeline.broker.get_fractionability.assert_not_called()


# --- the broker-side gate on its own ---------------------------------------


def _broker_with_asset(asset) -> AlpacaBroker:
    with patch("src.execution.broker.TradingClient"):
        broker = AlpacaBroker("k", "s", paper=True)
    if isinstance(asset, Exception):
        broker.client.get_asset.side_effect = asset
    else:
        broker.client.get_asset.return_value = asset
    return broker


def test_get_fractionability_reports_true_only_when_the_broker_says_so():
    broker = _broker_with_asset({"fractionable": True})
    assert broker.get_fractionability("MSFT")["fractionable"] is True


@pytest.mark.parametrize(
    "asset, reason",
    [
        ({"fractionable": False}, "not_fractionable"),
        ({}, "fractionable_unknown"),
        (RuntimeError("boom"), "asset_lookup_failed"),
    ],
)
def test_get_fractionability_fails_closed(asset, reason):
    broker = _broker_with_asset(asset)
    result = broker.get_fractionability("XYZ")
    assert result["fractionable"] is False
    assert result["reason"] == reason


def test_get_fractionability_is_cached_per_symbol():
    broker = _broker_with_asset({"fractionable": True})
    broker.get_fractionability("MSFT")
    broker.get_fractionability("MSFT")
    assert broker.client.get_asset.call_count == 1


# --- the quantizer on its own ----------------------------------------------


@pytest.mark.parametrize(
    "raw, fractional, expected",
    [
        (1.5625, False, 1.0),
        (1.5625, True, 1.5625),
        (0.99999999, True, 0.9999),  # floors, never rounds up
        (0.99999999, False, 0.0),  # ...and whole-share mode still says zero
        (3.0, True, 3.0),
        (float("nan"), True, 0.0),
        (float("inf"), True, 0.0),
        (-4.0, True, 0.0),
    ],
)
def test_size_shares_quantization(raw, fractional, expected):
    pipeline = MagicMock()
    pipeline.broker.list_open_orders_checked.return_value = (True, [])  # listing read, nothing open
    pipeline.config.execution.fractional_share_decimals = 4
    assert _size_shares(pipeline, raw, fractional=fractional) == pytest.approx(expected)


# ==========================================================================
# 3. Guard 1 — the stop retries immediately and hard
# ==========================================================================


def _protection_broker(*, filled_qty: float, stop_results: list) -> AlpacaBroker:
    """A broker whose entry filled `filled_qty` and whose stop submissions
    play out `stop_results` in order (an Exception is raised, a dict is
    returned)."""
    with patch("src.execution.broker.TradingClient"):
        broker = AlpacaBroker("k", "s", paper=True)
    broker.wait_for_order_terminal = MagicMock(return_value="filled")
    broker.get_order_fill_info = MagicMock(
        return_value={"filled_qty": filled_qty},
    )

    def _submit(**kwargs):
        outcome = stop_results.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return {**outcome, "_qty": kwargs["qty"]}

    broker._submit_stop_limit_order = MagicMock(side_effect=_submit)
    return broker


def test_stop_placement_retries_immediately_and_the_retry_protects_the_position():
    """Guard 1. The first submit fails; the retry happens inside the SAME
    call — not queued, not deferred to the next sweep — and the position ends
    the call protected."""
    broker = _protection_broker(
        filled_qty=10.0,
        stop_results=[RuntimeError("429 rate limited"), {"id": "stop-1"}],
    )

    with patch("src.execution.broker.time.sleep") as sleep:
        out = broker.place_entry_protection("NVDA", "e1", 95.0, requested_qty=10)

    assert out is not None and out["id"] == "stop-1"
    assert broker._submit_stop_limit_order.call_count == 2
    assert out["_qty"] == 10.0, "the retry must cover the WHOLE fill"
    # The retry is immediate: sub-second, not a sweep interval away.
    assert sleep.call_args.args[0] <= 1.0


def test_stop_placement_gives_up_after_a_bounded_number_of_attempts():
    """Bounded, because a failure that survives the burst is a REJECTION and
    retrying a rejection forever only delays the alert that is the real
    remedy."""
    broker = _protection_broker(
        filled_qty=10.0,
        stop_results=[RuntimeError("nope")] * 10,
    )

    with patch("src.execution.broker.time.sleep"):
        out = broker.place_entry_protection("NVDA", "e1", 95.0, requested_qty=10)

    assert out is None
    assert broker._submit_stop_limit_order.call_count == 3


def test_a_terminal_broker_rejection_does_not_burn_the_retry_burst():
    """Board item 129: a 400/404/422 (bad price, unsupported qty, closed
    venue) will fail identically on every attempt — it is the broker's
    considered answer, not a blip. It must be reported after ONE attempt,
    not retried three times and only THEN reported, which used to delay the
    exact owner alert this ceiling was written to deliver promptly."""
    rejection = RuntimeError("unsupported qty")
    rejection.status_code = 422
    broker = _protection_broker(
        filled_qty=10.0,
        stop_results=[rejection, {"id": "should-not-be-reached"}],
    )

    with patch("src.execution.broker.time.sleep") as sleep:
        out = broker.place_entry_protection("NVDA", "e1", 95.0, requested_qty=10)

    assert out is None
    assert broker._submit_stop_limit_order.call_count == 1, (
        "a terminal rejection must not spend the retry budget it cannot use"
    )
    sleep.assert_not_called()


def test_a_transient_failure_with_no_status_code_still_retries_the_full_burst():
    """The other half of item 129's fix: an exception that carries no HTTP
    status (a dropped connection, a bare timeout) is exactly the case the
    ceiling was designed for, and must still spend the whole burst."""
    broker = _protection_broker(
        filled_qty=10.0,
        stop_results=[RuntimeError("connection reset")] * 10,
    )

    with patch("src.execution.broker.time.sleep"):
        out = broker.place_entry_protection("NVDA", "e1", 95.0, requested_qty=10)

    assert out is None
    assert broker._submit_stop_limit_order.call_count == 3


def test_a_fractional_fill_is_protected_by_a_hybrid_gtc_plus_day_pair():
    """§11.1 HYBRID FRACTIONAL STOPS — the open question, now answered.

    The broker refuses a fractional GTC outright and accepts a fractional
    DAY, so a 12.3456-share position is covered by TWO orders at the same
    stop price: a durable GTC over the 12 whole shares and a DAY over the
    0.3456 remainder. Nothing is uncovered while the session is open, so
    guard 2 must stay silent (`uncovered_qty == 0`).

    This REPLACES the old "three doomed attempts at the exact fractional qty,
    then fall back to whole shares" pin. Those three attempts are now known
    to be three guaranteed rejections, each one costing ~2 seconds with the
    position open and unprotected."""
    broker = _protection_broker(
        filled_qty=12.3456,
        stop_results=[{"id": "stop-frac"}, {"id": "stop-whole"}],
    )

    with patch("src.execution.broker.time.sleep"):
        out = broker.place_entry_protection("NVDA", "e1", 95.0, requested_qty=13)

    # No attempt is wasted on the exact fractional qty any more.
    assert broker._submit_stop_limit_order.call_count == 2
    qtys = [c.kwargs["qty"] for c in broker._submit_stop_limit_order.call_args_list]
    assert qtys[0] == pytest.approx(0.3456), "DAY remainder first so GTC cannot starve it"
    assert qtys[1] == 12.0, "the durable GTC leg covers the whole shares"

    assert out["id"] == "stop-whole", "the durable leg is the id carried forward"
    assert out["hybrid"] is True
    assert out["gtc_qty"] == 12.0
    assert out["day_qty"] == pytest.approx(0.3456)
    assert out["covered_qty"] == pytest.approx(12.3456)
    assert out["uncovered_qty"] == 0.0


def test_a_position_under_one_share_gets_a_day_stop_and_no_gtc_leg():
    """There is no whole part to place a GTC over, so the sub-share position
    carries a DAY stop ONLY. It is fully covered while the session is open —
    which is the entire reason the desk can hold a $900 name at all on a
    ~$10k account."""
    broker = _protection_broker(
        filled_qty=0.6,
        stop_results=[{"id": "stop-frac"}],
    )

    with patch("src.execution.broker.time.sleep"):
        out = broker.place_entry_protection("NVDA", "e1", 95.0, requested_qty=1)

    assert broker._submit_stop_limit_order.call_count == 1
    assert broker._submit_stop_limit_order.call_args.kwargs["qty"] == pytest.approx(0.6)
    assert out["gtc_qty"] == 0.0
    assert out["day_qty"] == pytest.approx(0.6)
    assert out["uncovered_qty"] == 0.0


def test_the_fractional_leg_is_day_and_the_whole_leg_is_gtc_at_the_broker():
    """The tif is derived from the QUANTITY inside `_submit_stop_limit_order`,
    not chosen by a caller — so no path can forget the broker's rule and
    submit a fractional GTC that is refused while the code believes a stop
    was placed. This asserts against the actual request objects."""
    from alpaca.trading.enums import TimeInForce
    from alpaca.trading.requests import StopOrderRequest

    with patch("src.execution.broker.TradingClient") as tc_cls:
        client = MagicMock()
        client.submit_order.return_value = MagicMock(
            id="s",
            status="new",
            symbol="NVDA",
        )
        tc_cls.return_value = client
        broker = AlpacaBroker("k", "s", paper=True)
        broker.wait_for_order_terminal = MagicMock(return_value="filled")
        broker.get_order_fill_info = MagicMock(
            return_value={"filled_qty": 12.3456},
        )
        with patch("src.execution.broker.time.sleep"):
            broker.place_entry_protection("NVDA", "e1", 95.0, requested_qty=13)

    reqs = [c.args[0] for c in client.submit_order.call_args_list]
    assert len(reqs) == 2
    assert all(isinstance(r, StopOrderRequest) for r in reqs)  # primary = stop-MARKET
    frac, whole = reqs
    assert float(frac.qty) == pytest.approx(0.3456)
    assert frac.time_in_force == TimeInForce.DAY  # the only tif the broker takes
    assert float(whole.qty) == 12.0
    assert whole.time_in_force == TimeInForce.GTC  # durable, survives 16:00 ET
    # Both legs sit at the SAME trigger — a remainder stopped somewhere else
    # would be a second, unreviewed risk decision.
    assert float(whole.stop_price) == float(frac.stop_price) == 95.0


def test_a_failed_day_leg_still_leaves_the_whole_shares_durably_covered():
    """Guard 2's partial-cover alert survives the hybrid. If the DAY leg is
    refused, the GTC leg still stands watch over the whole shares and the
    sub-share remainder is REPORTED, never swallowed."""
    broker = _protection_broker(
        filled_qty=12.3456,
        stop_results=[RuntimeError("day leg refused")] * 3 + [{"id": "stop-whole"}],
    )

    with patch("src.execution.broker.time.sleep"):
        out = broker.place_entry_protection("NVDA", "e1", 95.0, requested_qty=13)

    assert out["id"] == "stop-whole"
    assert out["covered_qty"] == 12.0
    assert out["uncovered_qty"] == pytest.approx(0.3456)
    assert out["day_qty"] == 0.0


# ==========================================================================
# 4. Guard 2 — a stop that never lands ALERTS THE OWNER
# ==========================================================================


def _entry_spec(**kw) -> dict:
    spec = {
        "symbol": "NVDA",
        "side": "buy",
        "order_id": "e1",
        "stop_price": 95.0,
        "qty": 10,
        "reference_price": 100.0,
        "limit_price": 100.0,
        "trade_row_id": 1,
    }
    spec.update(kw)
    return spec


def _run_protection_phase(pipeline, spec) -> None:
    """Drive ExecutionStage as far as the post-fill protection loop by
    letting a real BUY flow through it with the broker's protection call
    stubbed to whatever the test wants."""
    pipeline.broker.submit_order.return_value = {
        "id": spec["order_id"],
        "status": "accepted",
        "symbol": spec["symbol"],
        "side": "buy",
        "pending_stop_price": spec["stop_price"],
    }
    decision = TradeDecision(
        action="BUY",
        symbol=spec["symbol"],
        allocation_pct=10.0,
        entry_price=100.0,
        stop_loss=spec["stop_price"],
        take_profit=130.0,
        reasoning="protection test",
    )
    ExecutionStage(pipeline=pipeline).run(_ctx([decision]))


def test_a_stop_that_fails_every_retry_raises_an_owner_alert():
    """Guard 2, and the difference between a brief gap and an indefinite one.
    Asserted on the ALERT — a log line is explicitly not acceptable here."""
    pipeline = _pipeline(live_price=100.0, fractional_enabled=False)
    pipeline.broker.place_entry_protection.return_value = None
    pipeline.broker.get_order_fill_info.return_value = {"filled_qty": 7.0}

    with patch("src.notifier.send_owner_alert") as alert:
        _run_protection_phase(pipeline, _entry_spec())

    alert.assert_called_once()
    body = alert.call_args.args[0]
    assert "NO STOP AT ALL" in body
    assert "NVDA" in body
    assert alert.call_args.kwargs["symbols"] == ["NVDA"]


def test_a_partially_covering_stop_also_raises_an_owner_alert():
    pipeline = _pipeline(live_price=100.0, fractional_enabled=False)
    pipeline.broker.place_entry_protection.return_value = {
        "id": "stop-whole",
        "covered_qty": 12.0,
        "uncovered_qty": 0.3456,
    }

    with patch("src.notifier.send_owner_alert") as alert:
        _run_protection_phase(pipeline, _entry_spec())

    alert.assert_called_once()
    body = alert.call_args.args[0]
    assert "PARTIALLY COVERS" in body
    assert "0.3456" in body


def test_a_protected_position_does_not_wake_the_owner():
    pipeline = _pipeline(live_price=100.0, fractional_enabled=False)
    pipeline.broker.place_entry_protection.return_value = {"id": "stop-1"}

    with patch("src.notifier.send_owner_alert") as alert:
        _run_protection_phase(pipeline, _entry_spec())

    alert.assert_not_called()


def test_an_entry_that_filled_nothing_does_not_wake_the_owner():
    """`place_entry_protection` returns None for a zero fill too. There is no
    position, so there is nothing unprotected — and waking a human for a BUY
    that simply did not fill is how guard 2 gets switched off."""
    pipeline = _pipeline(live_price=100.0, fractional_enabled=False)
    pipeline.broker.place_entry_protection.return_value = None
    pipeline.broker.get_order_fill_info.return_value = {"filled_qty": 0.0}

    with patch("src.notifier.send_owner_alert") as alert:
        _run_protection_phase(pipeline, _entry_spec())

    alert.assert_not_called()


# ==========================================================================
# 5. Guard 3 — the sweep separates "no stop" from "mis-sized"
# ==========================================================================


def _sweep_pipeline(positions, covered_by_symbol: dict) -> MagicMock:
    pipeline = MagicMock()
    pipeline.broker.list_open_orders_checked.return_value = (True, [])  # listing read, nothing open
    pipeline.broker.get_positions.return_value = positions
    pipeline.db.get_pending_protection_restores.return_value = []
    pipeline._sweeper.return_value = None
    pipeline._repair_stop_coverage.return_value = False

    def _snapshot(symbol, side="sell"):
        qty = covered_by_symbol.get(symbol, 0.0)
        return True, ([{"qty": qty}] if qty else [])

    pipeline.broker.snapshot_protective_stops.side_effect = _snapshot
    # The escalation is the behaviour under test — bind the REAL one, or the
    # MagicMock silently absorbs the call and every assertion below passes
    # for the wrong reason.
    pipeline._alert_owner_no_stop = TradingPipeline._alert_owner_no_stop
    return pipeline


def _held(symbol: str, qty: float) -> MagicMock:
    position = MagicMock()
    position.symbol = symbol
    position.qty = qty
    return position


def test_sweep_distinguishes_no_stop_at_all_from_a_mis_sized_stop():
    """Guard 3. Two positions, two different conditions, and they must not
    read as one count of 'gaps' — a position stopped at the wrong size still
    has a broker order standing watch; a position with none has nothing."""
    pipeline = _sweep_pipeline(
        [_held("NAKED", 10.0), _held("SHORTSTOP", 10.0)],
        {"NAKED": 0.0, "SHORTSTOP": 4.0},
    )

    with patch("src.notifier.send_owner_alert"):
        gaps = TradingPipeline._reconcile_stop_coverage(pipeline)

    by_symbol = {g["symbol"]: g for g in gaps}
    assert by_symbol["NAKED"]["coverage"] == "none"
    assert by_symbol["SHORTSTOP"]["coverage"] == "partial"


def test_sweep_says_no_stop_at_all_in_words_not_just_a_field(caplog):
    pipeline = _sweep_pipeline([_held("NAKED", 10.0)], {"NAKED": 0.0})

    with caplog.at_level("WARNING"), patch("src.notifier.send_owner_alert"):
        TradingPipeline._reconcile_stop_coverage(pipeline)

    assert "NO STOP AT ALL" in caplog.text
    assert "STOP MIS-SIZED" not in caplog.text


def test_sweep_says_mis_sized_in_words_for_a_partially_covered_position(caplog):
    pipeline = _sweep_pipeline([_held("SHORTSTOP", 10.0)], {"SHORTSTOP": 4.0})

    with caplog.at_level("WARNING"), patch("src.notifier.send_owner_alert"):
        TradingPipeline._reconcile_stop_coverage(pipeline)

    assert "STOP MIS-SIZED" in caplog.text
    assert "NO STOP AT ALL" not in caplog.text


def test_sweep_alerts_the_owner_only_for_a_position_with_no_stop_at_all():
    """Escalation is reserved for the worse condition. A mis-sized stop is
    real but bounded and rides the session banner; alerting on both is how a
    channel gets tuned out."""
    pipeline = _sweep_pipeline(
        [_held("NAKED", 10.0), _held("SHORTSTOP", 10.0)],
        {"NAKED": 0.0, "SHORTSTOP": 4.0},
    )

    with patch("src.notifier.send_owner_alert") as alert:
        TradingPipeline._reconcile_stop_coverage(pipeline)

    alert.assert_called_once()
    body = alert.call_args.args[0]
    assert "NO STOP AT ALL" in body
    assert "NAKED" in body
    assert "SHORTSTOP" not in body


def test_sweep_does_not_alert_when_the_auto_repair_closed_the_gap():
    pipeline = _sweep_pipeline([_held("NAKED", 10.0)], {"NAKED": 0.0})
    pipeline._repair_stop_coverage.return_value = True

    with patch("src.notifier.send_owner_alert") as alert:
        TradingPipeline._reconcile_stop_coverage(pipeline)

    alert.assert_not_called()


# ==========================================================================
# 6. Guard 3, second half — the sweep's finding reaches the operator
# ==========================================================================


def test_an_ordinary_intra_tick_is_still_silent():
    """14 ticks a day. Breaking that silence casually is what makes the
    channel worthless when it matters."""
    from src.notifier import format_session_result

    assert (
        format_session_result(
            "intra_check",
            {"status": "ok", "run_id": "r", "positions": 3},
            2.0,
        )
        is None
    )


def test_an_intra_tick_that_found_a_coverage_gap_breaks_the_silence():
    """This is the mode whose normal tick sends NOTHING, so before §11.1 the
    30-minute sweep's worst finding was reported to a log file and nowhere
    else. The gap is what breaks the silence, and it names the condition."""
    from src.notifier import format_session_result

    msg = format_session_result(
        "intra_check",
        {
            "status": "ok",
            "run_id": "r",
            "positions": 3,
            "stop_coverage_gaps": [
                {"symbol": "NAKED", "held_qty": 10.0, "covered_qty": 0.0, "coverage": "none"},
            ],
        },
        2.0,
    )

    assert msg is not None
    assert "NO STOP AT ALL" in msg
    assert "NAKED" in msg


# ==========================================================================
# 7. Guard 1, extended — the repair belt retries too
#
# `_repair_stop_coverage` (the 30-minute sweep's auto-repair) originally
# called the broker's bare single-shot `_submit_stop_limit_order` directly —
# no retry burst, and no whole-share fallback for a fractional
# `uncovered_qty`, which this repair is reached with whenever the gapped
# position is itself fractional. That is the same gap guard 1 exists to
# close on the entry path, left open on the belt that is supposed to be its
# backstop. Fixed by routing through `_submit_protective_stop_retrying`.
# ==========================================================================


def _repair_pipeline(*, stop_loss=140.0, live_price=150.0) -> MagicMock:
    pipeline = MagicMock()
    pipeline.broker.list_open_orders_checked.return_value = (True, [])  # listing read, nothing open
    pipeline.db.get_symbol_last_buy.return_value = {"stop_loss": stop_loss}
    pipeline.broker.get_latest_price.return_value = live_price
    pipeline.broker.get_latest_quote.return_value = {"bid_price": live_price, "ask_price": live_price}
    return pipeline


def test_repair_retries_a_transient_failure_in_band():
    """A transient failure on the repair belt now clears inside the same
    ~2-second burst instead of costing the position a full 30-minute cycle."""
    pipeline = _repair_pipeline()
    pipeline.broker._submit_protective_stop_retrying.return_value = {"id": "r1"}

    out = TradingPipeline._repair_stop_coverage(
        pipeline,
        "NVDA",
        10.0,
        is_short=False,
    )

    assert out is True
    pipeline.broker._submit_protective_stop_retrying.assert_called_once_with(
        symbol="NVDA",
        qty=10.0,
        stop_price=140.0,
        side="sell",
    )


def test_repair_exhausted_reports_unrepaired():
    pipeline = _repair_pipeline()
    pipeline.broker._submit_protective_stop_retrying.return_value = None

    out = TradingPipeline._repair_stop_coverage(
        pipeline,
        "NVDA",
        10.0,
        is_short=False,
    )

    assert out is False


def test_repair_of_a_fractional_gap_that_only_partially_covers_keeps_escalating():
    """The retry machinery fell back to a whole-share floor stop — real
    progress, but a sub-share sliver is still gapped. `repaired` must stay
    False so THIS pass keeps escalating; the next sweep's fresh broker
    snapshot reclassifies the symbol 'partial' on its own."""
    pipeline = _repair_pipeline()
    pipeline.broker._submit_protective_stop_retrying.return_value = {
        "id": "r1",
        "covered_qty": 12.0,
        "uncovered_qty": 0.3456,
    }

    out = TradingPipeline._repair_stop_coverage(
        pipeline,
        "NVDA",
        12.3456,
        is_short=False,
    )

    assert out is False, "a real but partial cover must not read as repaired"
    pipeline.broker._submit_protective_stop_retrying.assert_called_once_with(
        symbol="NVDA",
        qty=12.3456,
        stop_price=140.0,
        side="sell",
    )


def test_short_repair_places_a_buy_stop_above_the_tape():
    """The wrapper must pass is_short through: a SHORT's recorded stop sits
    above the live price, and the protective order is a BUY stop-MARKET."""
    pipeline = _repair_pipeline(stop_loss=160.0, live_price=150.0)
    pipeline.broker._submit_protective_stop_retrying.return_value = {"id": "r1"}

    out = TradingPipeline._repair_stop_coverage(
        pipeline,
        "TSLA",
        10.0,
        is_short=True,
    )

    assert out is True
    pipeline.broker._submit_protective_stop_retrying.assert_called_once_with(
        symbol="TSLA",
        qty=10.0,
        stop_price=160.0,
        side="buy",
    )
    assert pipeline.db.get_symbol_last_buy.call_args.kwargs.get("action") == "SHORT"


# ==========================================================================
# 7. Spec §11.1 HYBRID FRACTIONAL STOPS — the whole/fractional split, the
#    re-placement path, and THE ALERTING DISTINCTION.
#
# The broker was probed against the live paper account on 2026-09-01:
#   * a fractional GTC order is refused ("fractional orders must be DAY
#     orders", 42210000); a fractional TRAILING stop is refused at any tif;
#   * STOP/DAY, STOP_LIMIT/DAY and LIMIT/DAY are accepted fractional;
#   * whole-share GTC stops are unaffected.
#
# So a sub-share remainder cannot carry a durable stop, and its DAY stop
# lapses every night by design. The single most important property of this
# feature is therefore NOT that the stop gets placed — it is that the
# alerting can tell an expected nightly lapse apart from a real failure. An
# alert that fires every night on a healthy system is worse than no alert:
# it trains the owner to swipe away the one message that must never be
# ignored. These tests pin that distinction from both sides.
# ==========================================================================

from src.pipeline import _classify_coverage_gap, _split_protective_qty  # noqa: E402


# --- the split itself -------------------------------------------------------


@pytest.mark.parametrize(
    "qty, whole, frac",
    [
        (12.3456, 12.0, 0.3456),  # ordinary fractional position
        (0.6, 0.0, 0.6),  # under one share — no GTC leg exists at all
        (10.0, 10.0, 0.0),  # whole — must stay on the untouched GTC path
        (1.0, 1.0, 0.0),
        (0.0, 0.0, 0.0),
        (-4.5, 4.0, 0.5),  # a short's signed qty is a magnitude here
    ],
)
def test_whole_fractional_split(qty, whole, frac):
    w, f = _split_protective_qty(qty)
    assert w == pytest.approx(whole)
    assert f == pytest.approx(frac)


def test_float_noise_does_not_mint_a_phantom_sub_share_leg():
    """A fill of 7.000000000000001 shares is SEVEN shares. Splitting a
    1e-15 'remainder' off it would submit a second, absurd DAY order and
    make a healthy whole-share position look permanently fractional."""
    assert _split_protective_qty(7.000000000000001) == (7.0, 0.0)
    assert _split_protective_qty(10.5 - 10.0 + 10.0) == (10.0, 0.5)


# --- the classifier ---------------------------------------------------------


def test_classifier_names_a_sub_share_only_gap_fractional():
    """12 whole shares still covered by their durable GTC stop, 0.3456
    missing. The durable leg is intact, so this is the fractional case —
    NOT 'partial', which would red-banner it."""
    coverage, uncovered = _classify_coverage_gap(held=12.3456, covered=12.0)
    assert coverage == "fractional"
    assert uncovered == pytest.approx(0.3456)


def test_classifier_names_a_sub_one_share_position_fractional_not_none():
    """0.6 shares with zero coverage. A bare `covered <= 0` test calls this
    NO STOP AT ALL — the owner-escalating condition — which would fire the
    pager every single night on a 60-cent-ish remainder."""
    coverage, uncovered = _classify_coverage_gap(held=0.6, covered=0.0)
    assert coverage == "fractional"
    assert uncovered == pytest.approx(0.6)


def test_classifier_still_says_none_when_the_whole_share_leg_is_gone():
    """12.3456 held, NOTHING covered. The durable GTC leg is missing, so
    this is case (c) and it must never be softened by the fractional path."""
    coverage, _ = _classify_coverage_gap(held=12.3456, covered=0.0)
    assert coverage == "none"


def test_classifier_still_says_partial_when_the_whole_share_leg_is_short():
    """Only 3 of 12 whole shares covered. The remainder is the least of the
    problems here."""
    coverage, _ = _classify_coverage_gap(held=12.3456, covered=3.0)
    assert coverage == "partial"


def test_classifier_is_unchanged_for_whole_share_positions():
    assert _classify_coverage_gap(held=10.0, covered=0.0)[0] == "none"
    assert _classify_coverage_gap(held=10.0, covered=4.0)[0] == "partial"


# --- the sweep, with the clock controlled ----------------------------------


def _hybrid_sweep_pipeline(positions, covered_by_symbol: dict, *, repair: bool = True) -> MagicMock:
    pipeline = MagicMock()
    pipeline.broker.list_open_orders_checked.return_value = (True, [])  # listing read, nothing open
    pipeline.broker.get_positions.return_value = positions
    pipeline.db.get_pending_protection_restores.return_value = []
    pipeline._sweeper.return_value = None
    pipeline._repair_stop_coverage.return_value = repair

    def _snapshot(symbol, side="sell"):
        qty = covered_by_symbol.get(symbol, 0.0)
        return True, ([{"qty": qty}] if qty else [])

    pipeline.broker.snapshot_protective_stops.side_effect = _snapshot
    # Bind the REAL escalation, or the MagicMock silently absorbs the call
    # and every assertion below passes for the wrong reason.
    pipeline._alert_owner_no_stop = TradingPipeline._alert_owner_no_stop
    return pipeline


def _priced(symbol: str, qty: float, price: float = 900.0) -> MagicMock:
    position = MagicMock()
    position.symbol = symbol
    position.qty = qty
    position.current_price = price
    return position


def _sweep(pipeline, *, market_open: bool):
    with patch("src.pipeline_protection._market_is_open_now", return_value=market_open):
        return TradingPipeline._reconcile_stop_coverage(pipeline)


# --- case (a): the expected overnight lapse --------------------------------


def test_a_lapsed_overnight_fractional_stop_does_not_alert_the_owner():
    """CASE (a), AND THE MOST IMPORTANT TEST IN THIS FILE.

    The market is shut. The 12 whole shares are still covered by their
    durable GTC stop; the 0.3456 remainder lost its DAY stop at 16:00 ET
    exactly as designed. This happens to every fractional position every
    single night. If it pages the owner, the feature has destroyed the
    credibility of guard 2 and the desk is worse off than before."""
    # repair=False models reality: a DAY order into a shut market cannot be
    # placed. A fixture that pretends the repair succeeds would mask exactly
    # the regression this test exists to catch.
    pipeline = _hybrid_sweep_pipeline(
        [_priced("NVDA", 12.3456)],
        {"NVDA": 12.0},
        repair=False,
    )

    with patch("src.notifier.send_owner_alert") as alert:
        gaps = _sweep(pipeline, market_open=False)

    alert.assert_not_called()
    assert gaps[0]["coverage"] == "fractional_overnight"


def test_a_lapsed_overnight_fractional_stop_is_not_repaired_into_a_shut_market():
    """A DAY order submitted after the close is a rejection at best and a
    surprise queued order at worst. The next session's sweep owns it."""
    pipeline = _hybrid_sweep_pipeline(
        [_priced("NVDA", 12.3456)],
        {"NVDA": 12.0},
        repair=False,
    )

    with patch("src.notifier.send_owner_alert"):
        _sweep(pipeline, market_open=False)

    pipeline._repair_stop_coverage.assert_not_called()


def test_a_sub_one_share_position_overnight_does_not_alert_either():
    """0.6 shares, zero coverage, market shut. This is the case the old
    classifier called NO STOP AT ALL — the owner-escalating condition — and
    it would have fired nightly for as long as the position was held."""
    pipeline = _hybrid_sweep_pipeline(
        [_priced("TINY", 0.6)],
        {"TINY": 0.0},
        repair=False,
    )

    with patch("src.notifier.send_owner_alert") as alert:
        gaps = _sweep(pipeline, market_open=False)

    alert.assert_not_called()
    assert gaps[0]["coverage"] == "fractional_overnight"


def test_the_overnight_exposure_is_reported_as_a_number_not_a_reassurance():
    """The owner accepted this exposure on condition it be observable: 'a
    number he can look at beats a guarantee he has to trust'. 0.3456 shares
    of a $900 name is $311.04."""
    pipeline = _hybrid_sweep_pipeline(
        [_priced("NVDA", 12.3456, price=900.0)],
        {"NVDA": 12.0},
        repair=False,
    )

    with patch("src.notifier.send_owner_alert"):
        gaps = _sweep(pipeline, market_open=False)

    assert gaps[0]["uncovered_qty"] == pytest.approx(0.3456)
    assert gaps[0]["unprotected_value"] == pytest.approx(311.04, abs=0.01)


def test_the_overnight_exposure_reaches_the_session_alert():
    """Reported where the owner actually reads, not only in a log file —
    and NOT as a red banner, because nothing here needs doing."""
    from src.notifier import format_session_result

    body = format_session_result(
        "evening",
        {
            "status": "ok",
            "run_id": "r",
            "stop_coverage_gaps": [
                {
                    "symbol": "NVDA",
                    "held_qty": 12.3456,
                    "covered_qty": 12.0,
                    "coverage": "fractional_overnight",
                    "uncovered_qty": 0.3456,
                    "unprotected_value": 311.04,
                }
            ],
        },
        2.0,
    )

    assert "311.04" in body
    assert "NVDA" in body
    assert "NO STOP AT ALL" not in body
    assert "STOP MIS-SIZED" not in body


def test_an_expected_overnight_lapse_does_not_break_intra_check_silence():
    """intra_check ticks 14 times a day and is silent by design. A routine
    fractional re-placement must not be what breaks that silence."""
    from src.notifier import format_session_result

    assert (
        format_session_result(
            "intra_check",
            {
                "status": "ok",
                "run_id": "r",
                "positions": 3,
                "stop_coverage_gaps": [
                    {"symbol": "NVDA", "held_qty": 12.3456, "covered_qty": 12.0, "coverage": "fractional_replaced"},
                ],
            },
            2.0,
        )
        is None
    )


# --- case (b): a placement failure during session hours --------------------


def test_a_missing_fractional_stop_during_session_hours_is_repaired():
    """CASE (b). The same shortfall, but the market is OPEN — the remainder
    should be covered right now. This is also the start-of-session
    re-placement path: the sweep is what puts the DAY stop back."""
    pipeline = _hybrid_sweep_pipeline(
        [_priced("NVDA", 12.3456)],
        {"NVDA": 12.0},
        repair=True,
    )

    with patch("src.notifier.send_owner_alert") as alert:
        gaps = _sweep(pipeline, market_open=True)

    pipeline._repair_stop_coverage.assert_called_once()
    assert pipeline._repair_stop_coverage.call_args.args[0] == "NVDA"
    assert pipeline._repair_stop_coverage.call_args.args[1] == pytest.approx(0.3456)
    assert gaps[0]["coverage"] == "fractional_replaced"
    alert.assert_not_called()


def test_a_fractional_stop_that_cannot_be_re_placed_in_hours_still_alerts():
    """CASE (b), the failing half. The repair did not land and the market is
    open — this is a real placement failure and it alerts exactly as guard 2
    always has. THIS is the assertion that proves the overnight suppression
    is not just 'never alert about fractional'."""
    pipeline = _hybrid_sweep_pipeline(
        [_priced("TINY", 0.6)],
        {"TINY": 0.0},
        repair=False,
    )

    with patch("src.notifier.send_owner_alert") as alert:
        gaps = _sweep(pipeline, market_open=True)

    alert.assert_called_once()
    assert "NO STOP AT ALL" in alert.call_args.args[0]
    assert gaps[0]["coverage"] == "none"


def test_a_partly_covered_fractional_gap_in_hours_falls_back_to_the_banner():
    """Repair failed but the durable GTC leg is still standing watch over 12
    shares. Guard 3's existing ladder is unchanged: some coverage banners,
    it does not escalate."""
    pipeline = _hybrid_sweep_pipeline(
        [_priced("NVDA", 12.3456)],
        {"NVDA": 12.0},
        repair=False,
    )

    with patch("src.notifier.send_owner_alert") as alert:
        gaps = _sweep(pipeline, market_open=True)

    alert.assert_not_called()
    assert gaps[0]["coverage"] == "partial"


# --- case (c): the whole-share GTC leg is missing --------------------------


def test_a_missing_whole_share_gtc_leg_alerts_even_overnight():
    """CASE (c). 12.3456 held with NOTHING covered, market shut. The durable
    leg is the one that is supposed to survive the night; its absence is
    never the expected state and must never be suppressed by the overnight
    rule."""
    pipeline = _hybrid_sweep_pipeline(
        [_priced("NVDA", 12.3456)],
        {"NVDA": 0.0},
        repair=False,
    )

    with patch("src.notifier.send_owner_alert") as alert:
        gaps = _sweep(pipeline, market_open=False)

    alert.assert_called_once()
    assert "NO STOP AT ALL" in alert.call_args.args[0]
    assert gaps[0]["coverage"] == "none"


def test_a_short_whole_share_gtc_leg_banners_even_overnight():
    """Case (c)'s milder half: 3 of 12 whole shares covered, market shut.
    Still a real gap, still reported, still not softened."""
    pipeline = _hybrid_sweep_pipeline(
        [_priced("NVDA", 12.3456)],
        {"NVDA": 3.0},
        repair=False,
    )

    with patch("src.notifier.send_owner_alert"):
        gaps = _sweep(pipeline, market_open=False)

    assert gaps[0]["coverage"] == "partial"


def test_a_whole_share_position_is_unaffected_by_any_of_this():
    """Every short, and every long while `fractional_enabled` is off. A
    naked whole-share position alerts at 3am exactly as it did before."""
    pipeline = _hybrid_sweep_pipeline(
        [_priced("NVDA", 10.0)],
        {"NVDA": 0.0},
        repair=False,
    )

    with patch("src.notifier.send_owner_alert") as alert:
        gaps = _sweep(pipeline, market_open=False)

    alert.assert_called_once()
    assert gaps[0]["coverage"] == "none"


# --- the market-hours discriminator itself ---------------------------------


def test_market_hours_check_fails_toward_open():
    """Getting this wrong in the 'shut' direction SUPPRESSES a real naked-
    position alert, which is the one failure this desk cannot absorb.
    Getting it wrong the other way costs a redundant banner. So anything
    unknown answers OPEN."""
    from src.pipeline import _market_is_open_now

    broker = MagicMock()
    broker.list_open_orders_checked.return_value = (True, [])  # listing read, nothing open
    broker.get_session_close.side_effect = RuntimeError("calendar down")
    with patch("src.pipeline_protection.et_now", side_effect=RuntimeError("clock down")):
        assert _market_is_open_now(broker) is True


def test_market_hours_check_respects_an_early_close():
    """A half-day closes at 13:00 ET. A DAY stop lapses THEN, not at 16:00,
    so a 13:30 sweep must call the market shut or it would report every
    fractional position as a failure on Thanksgiving Friday."""
    from datetime import datetime
    from src.pipeline import _market_is_open_now
    from src.trading_calendar import ET

    broker = MagicMock()
    broker.list_open_orders_checked.return_value = (True, [])  # listing read, nothing open
    broker.get_session_close.return_value = datetime(2026, 11, 27, 13, 0, tzinfo=ET)
    now = datetime(2026, 11, 27, 13, 30, tzinfo=ET)
    with patch("src.pipeline_protection.et_now", return_value=now):
        assert _market_is_open_now(broker) is False


# --- the OTHER re-placement paths must not destroy the hybrid pair ---------


def test_a_trailing_stop_ratchet_re_places_the_hybrid_pair_not_one_day_order():
    """`replace_stop_loss` cancels a position's stops and re-places coverage
    for the whole quantity. On a fractional position a single order is
    necessarily fractional, therefore necessarily DAY, therefore gone at the
    close — the position would silently LOSE its durable GTC leg and the next
    overnight sweep would correctly page the owner about it. A trailing-stop
    ratchet must not be able to manufacture a nightly false alarm."""
    from alpaca.trading.enums import TimeInForce

    with patch("src.execution.broker.TradingClient") as tc_cls:
        client = MagicMock()
        client.submit_order.return_value = MagicMock(
            id="s",
            status="new",
            symbol="NVDA",
        )
        tc_cls.return_value = client
        broker = AlpacaBroker("k", "s", paper=True)
        broker._list_open_stop_orders_by_side = MagicMock(return_value=([], []))
        broker.get_positions = MagicMock(
            return_value=[
                MagicMock(symbol="NVDA", qty=12.3456),
            ]
        )
        broker.get_latest_price = MagicMock(return_value=200.0)
        broker.replace_stop_loss("NVDA", 150.0)

    reqs = [c.args[0] for c in client.submit_order.call_args_list]
    assert len(reqs) == 2, "the hybrid pair, not one collapsed order"
    assert float(reqs[0].qty) == pytest.approx(0.3456)
    assert reqs[0].time_in_force == TimeInForce.DAY
    assert float(reqs[1].qty) == 12.0
    assert reqs[1].time_in_force == TimeInForce.GTC


def test_a_whole_share_trailing_ratchet_is_still_one_gtc_order():
    """Every short, and every long while `fractional_enabled` is off."""
    from alpaca.trading.enums import TimeInForce

    with patch("src.execution.broker.TradingClient") as tc_cls:
        client = MagicMock()
        client.submit_order.return_value = MagicMock(
            id="s",
            status="new",
            symbol="NVDA",
        )
        tc_cls.return_value = client
        broker = AlpacaBroker("k", "s", paper=True)
        broker._list_open_stop_orders_by_side = MagicMock(return_value=([], []))
        broker.get_positions = MagicMock(
            return_value=[
                MagicMock(symbol="NVDA", qty=12.0),
            ]
        )
        broker.get_latest_price = MagicMock(return_value=200.0)
        broker.replace_stop_loss("NVDA", 150.0)

    reqs = [c.args[0] for c in client.submit_order.call_args_list]
    assert len(reqs) == 1
    assert float(reqs[0].qty) == 12.0
    assert reqs[0].time_in_force == TimeInForce.GTC


def test_a_partial_sell_reprotects_a_fractional_residual_as_a_hybrid_pair():
    """Same hazard on the partial-exit path: trimming 5 shares off 12.3456
    leaves a 7.3456 residual, and re-protecting it with one fractional order
    would leave the whole residual DAY-only."""
    pipeline = build_pipeline(broker=MagicMock(), db=None)
    pipeline.broker._list_open_sell_stop_orders.return_value = []
    pipeline._format_qty = lambda q: str(q)
    # Reprotect submits through the desk's ONE protective submit, so bind
    # the real thing over a mocked raw order call: that is what actually
    # exercises the whole-share/sliver leg split this test is about.
    pipeline.broker._submit_stop_limit_order.return_value = {
        "id": "leg",
        "status": "accepted",
    }
    pipeline.broker._stop_placer = functools.partial(AlpacaBroker._stop_placer, pipeline.broker)
    pipeline.broker._submit_stop_leg_retrying = functools.partial(
        AlpacaBroker._submit_stop_leg_retrying, pipeline.broker
    )
    pipeline.broker._submit_protective_stop_retrying = functools.partial(
        AlpacaBroker._submit_protective_stop_retrying, pipeline.broker
    )

    cancelled = [{"id": "s1", "qty": 12.3456, "stop_price": 90.0, "limit_price": 88.0}]
    assert (
        pipeline._reprotect_residual_after_partial_sell(
            "NVDA",
            7.3456,
            cancelled,
        )
        is True
    )

    qtys = [c.kwargs["qty"] for c in pipeline.broker._submit_stop_limit_order.call_args_list]
    # DAY sliver FIRST, then the whole-share GTC leg: MEASURED 2026-09-16
    # (BRK-B) the GTC hold reserved the position and Alpaca refused the
    # sub-share DAY remainder with held_for_orders when it went second.
    assert qtys[0] == pytest.approx(0.3456)
    assert qtys[1] == 7.0


def test_a_hybrid_pair_is_all_or_nothing_when_a_leg_is_rejected():
    """A half-placed pair is the worst outcome available: the caller's
    rollback believes nothing landed, while the coverage sweep sees a
    mis-sized stop. If the second leg is rejected, the first that already
    landed is cancelled and the failure propagates."""
    with patch("src.execution.broker.TradingClient"):
        broker = AlpacaBroker("k", "s", paper=True)
    broker.client = MagicMock()
    broker._submit_stop_limit_order = MagicMock(
        side_effect=[{"id": "leg-day"}, RuntimeError("gtc leg rejected")],
    )

    with pytest.raises(RuntimeError):
        broker._submit_stop_legs(symbol="NVDA", qty=12.3456, stop_price=90.0)

    broker.client.cancel_order_by_id.assert_called_once_with("leg-day")


def test_held_for_orders_on_day_sliver_is_covered_when_broker_already_holds_it():
    """2026-09-16 BRK-B: morning + intra_check both repaired the 0.4393 DAY
    remainder. The second hit held_for_orders / insufficient qty because
    the first already reserved the shares. Treat the live covering stop as
    success — do not leave the remainder flagged uncovered."""
    with patch("src.execution.broker.TradingClient"):
        broker = AlpacaBroker("k", "s", paper=True)
    live = SimpleNamespace(id="day-live", qty=0.4393, stop_price=485.0, limit_price=470.0)
    broker._submit_stop_limit_order = MagicMock(
        side_effect=RuntimeError(
            "insufficient qty available for order (requested: 0.4393, available: 0) held_for_orders"
        ),
    )
    broker._list_open_protective_stop_orders = MagicMock(return_value=[live])
    with patch("src.execution.broker.time.sleep"):
        out = broker._submit_stop_leg_retrying(
            symbol="BRK-B",
            qty=0.4393,
            stop_price=485.0,
            side="sell",
            leg="DAY fractional",
        )
    assert out is not None
    assert out["id"] == "day-live"
    assert out["already_live"] is True
