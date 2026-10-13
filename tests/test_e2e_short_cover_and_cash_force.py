"""Two hermetic CLOSE sessions: covering a held short, and the cash-only
forced de-lever. Same objects, seat seam, broker stand-in and socket wall as
tests/test_e2e_close_existing_book.py, whose helpers are reused.

Every expectation comes from the rules and the inputs, never from what the
desk did on a past session:

  cover   a held SHORT (qty < 0) with a resting BUY stop above it: a
          substantiated reviewer COVER buys back the WHOLE absolute quantity
          (a BUY, never a SELL that would add to the short) after clearing
          its protective buy stop, and nothing else is submitted;
  force   with `allow_margin` false and cash below zero at session start,
          the desk sells the long deterministically to raise the deficit,
          with the seat saying HOLD — and with cash at/above zero, or margin
          allowed, it sells nothing.

NOT COVERED: partial cover, a rejected cover order, the re-protect path
after a partial fill, a multi-name sweep order (biggest-loser-first).
"""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

import tests.test_e2e_morning_session as morning
import tests.test_e2e_close_existing_book as cb
from tests.test_e2e_close_existing_book import (
    CLOSE_AT,
    ENTRY,
    ONE_R_PRICE,
    QTY,
    SYMBOL,
    _bars,
    _market,
    _news_says_nothing,
    _reviewer_says,
    _risk_says_yes,
    _seed_open_position,
)
from tests.test_e2e_morning_session import (
    _build_config,
    _earnings_feed_stub,
    _macro_feed_stub,
    _news_feed_stub,
    _scripted_model_seats,
)
from tests.test_e2e_morning_protection import _seed_company_profile_cache

HOLD = cb.HOLD
SHORT_ENTRY = 105.0
SHORT_STOP = 110.0  # buy stop above the entry


def _run(
    tmp_path, monkeypatch, *, bars, cash, qty, entry, standing_stop, actions=HOLD, allow_margin=True, seed_long=True
):
    from ops.rehearsal.broker import (
        BrokerSnapshot,
        RecordedOrder,
        install_rehearsal_broker,
    )
    from ops.rehearsal.broker_amend import give_amend_endpoint
    from ops.rehearsal.clock import frozen_clock
    from ops.rehearsal.network_wall import no_network
    from ops.rehearsal.runner import _sentinel_credentials
    from src.agents.base import reset_route_breakers
    from src.trading_calendar import ET

    price = bars[-1].close
    reset_route_breakers()
    monkeypatch.chdir(tmp_path)
    _seed_company_profile_cache(tmp_path)
    config = _build_config(tmp_path)
    config.risk.allow_margin = allow_margin
    now = CLOSE_AT.replace(tzinfo=ET)
    trace: list = []
    answers = {"position": _reviewer_says(actions), "risk": _risk_says_yes(), "news": _news_says_nothing()}
    monkeypatch.setattr(morning, "_scripted_answers", lambda: answers)
    attempts: list[str] = []
    with (
        no_network(attempts),
        _sentinel_credentials(),
        patch("src.pipeline.MarketDataProvider", return_value=_market(bars)),
        patch("src.pipeline.MacroDataProvider", return_value=_macro_feed_stub()),
        patch("src.pipeline.NewsDataProvider", return_value=_news_feed_stub()),
        patch("src.pipeline.EarningsDataProvider", return_value=_earnings_feed_stub()),
        frozen_clock(now, run_id="e2e-short-force"),
        _scripted_model_seats(trace),
    ):
        from src.pipeline import TradingPipeline

        pipeline = TradingPipeline(config)
        if seed_long:
            _seed_open_position(pipeline.db)
        mv = qty * price
        snapshot = BrokerSnapshot(
            as_of=now.date(),
            cash=cash,
            portfolio_value=cash + mv,
            last_equity=cash + mv,
            positions=[
                {
                    "symbol": SYMBOL,
                    "qty": qty,
                    "avg_entry": entry,
                    "current_price": price,
                    "market_value": mv,
                    "unrealized_pnl": qty * (price - entry),
                    "sector": "ETF",
                }
            ],
            prices={SYMBOL: price},
            standing_stops={SYMBOL: standing_stop} if qty > 0 else {},
        )
        trading = give_amend_endpoint(
            install_rehearsal_broker(pipeline.broker, snapshot, now=now),
        )
        if qty < 0:  # the stand-in only seeds SELL stops; a short rests a BUY stop
            oid = f"pre-existing-stop-{SYMBOL}"
            trading._orders[oid] = RecordedOrder(
                order_id=oid,
                symbol=SYMBOL,
                side="buy",
                qty=abs(qty),
                order_type="stop",
                limit_price=None,
                stop_price=standing_stop,
                time_in_force="gtc",
                submitted_at=now,
                status="pre_existing",
            )
        symbols_of = pipeline.broker._data_client._symbols
        pipeline.broker._data_client.get_stock_latest_trade = lambda request: {
            s: SimpleNamespace(price=price, timestamp=now) for s in symbols_of(request)
        }
        pipeline.broker.get_intraday_snapshots = lambda symbols, *a, **k: {
            s: {"last_price": price, "last_trade_at": now} for s in symbols
        }
        result = pipeline.run_close()
    return result, trace, trading, attempts


def _closing(trading, side):
    return [o for o in trading.submitted if o.side == side and "stop" not in o.order_type]


def test_a_reviewer_cover_buys_back_the_whole_short_after_clearing_its_buy_stop(tmp_path, monkeypatch):
    bars = _bars(end=ONE_R_PRICE + 1.0)  # 99.00; the short entered at 105
    short_qty = -QTY
    result, trace, trading, attempts = _run(
        tmp_path,
        monkeypatch,
        bars=bars,
        cash=10_000.0,
        qty=short_qty,
        entry=SHORT_ENTRY,
        standing_stop=SHORT_STOP,
        actions=[
            {
                "action": "COVER",
                "symbol": SYMBOL,
                "reason": "thesis invalidated: the breakdown the short was measured on has failed",
                "exit_trigger": "thesis_invalid",
                "trigger_evidence": "thesis_invalid_if was 'closes above the breakdown level'; price has reclaimed it",
            }
        ],
    )
    assert attempts == [], attempts
    buys, sells = _closing(trading, "buy"), _closing(trading, "sell")
    assert sells == [], f"a SELL aimed at a short would ADD to it: {[o.as_plain() for o in sells]}"
    assert len(buys) == 1, [o.as_plain() for o in trading.submitted]
    assert buys[0].symbol == SYMBOL and float(buys[0].qty) == QTY, buys[0].as_plain()
    assert buys[0].status == "filled", buys[0].as_plain()
    assert trading.cancelled == [f"pre-existing-stop-{SYMBOL}"], trading.cancelled
    assert trading.submitted.index(buys[0]) == 0, [o.as_plain() for o in trading.submitted]


def test_cash_only_account_in_deficit_force_sells_the_long_with_the_seat_saying_hold(tmp_path, monkeypatch):
    bars = _bars(end=ONE_R_PRICE - 1.0)  # 97.00, well above the stop
    deficit = 200.0  # equity stays inside the 2x gross ceiling
    assert QTY * bars[-1].close * 0.97 >= deficit  # one name clears it
    result, trace, trading, attempts = _run(
        tmp_path,
        monkeypatch,
        bars=bars,
        cash=-deficit,
        qty=QTY,
        entry=ENTRY,
        standing_stop=cb.INITIAL_STOP,
        allow_margin=False,
    )
    assert attempts == [], attempts
    sells = _closing(trading, "sell")
    assert len(sells) == 1, [o.as_plain() for o in trading.submitted]
    assert sells[0].symbol == SYMBOL and float(sells[0].qty) == QTY, sells[0].as_plain()
    assert trading.cancelled == [f"pre-existing-stop-{SYMBOL}"], trading.cancelled
    assert trading.submitted.index(sells[0]) == 0
    assert _closing(trading, "buy") == []


def test_the_same_deficit_with_margin_allowed_sells_nothing(tmp_path, monkeypatch):
    bars = _bars(end=ONE_R_PRICE - 1.0)
    result, trace, trading, attempts = _run(
        tmp_path,
        monkeypatch,
        bars=bars,
        cash=-200.0,
        qty=QTY,
        entry=ENTRY,
        standing_stop=cb.INITIAL_STOP,
        allow_margin=True,
    )
    assert attempts == [] and _closing(trading, "sell") == [], [o.as_plain() for o in trading.submitted]
