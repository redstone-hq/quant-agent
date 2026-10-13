"""The hermetic CLOSE session on a FULL, LEVERED book that the ladder says
must shrink — judged on what the desk sells, how much, and what protection
is left standing.

tests/test_e2e_morning_session.py drives a morning on an empty book;
tests/test_e2e_morning_protection.py judges the buy it makes. This file
drives `TradingPipeline(config).run_close()` on a book that is ALREADY
over its gross-exposure ceiling once the de-levering ladder
(`src/risk/rules.py::resolve_gross_ceiling`, owner-ratified 2026-09-01)
is read off the account's own drawdown — the same production objects,
the same seat seam, the same rehearsal broker stand-in and the same
socket-level network wall.

The reviewer seat says HOLD on every name, so nothing a model says can
reduce the book: what is sold is decided by the INPUTS, and every
expectation below is derived from those inputs, never read back from the
run:

  the ladder   the equity curve carries a prior high of PEAK_EQUITY and
               today's equity is E, so the drawdown is (E/PEAK - 1): in
               the -15% to -20% band the ceiling is 1.0x, and the book's
               gross of 1.41x equity is over it by exactly OVER dollars;
  the cut      biggest-loser-first (the ratified ordering with no fresh
               seat read): the one name with negative unrealised P&L is
               sold WHOLE (owner ruling 2026-10-09: never part of a
               position), and no other name is touched;
  exposure     gross exposure after the sale is at or under the ceiling —
               and never above it again at any later point in the session;
  protection   the sold name's resting stop is cleared before the sale
               and exactly one new GTC stop rests behind the residual, at
               the cleared stop's level only if a residual remains (a
               whole sale leaves none, so none rests); every untouched name keeps
               exactly the one stop it started with; the broker-truth
               coverage audit reports no gap;
  the wall     no socket left the box and no order left the stand-in.

The stand-in's book does not move on a fill, so this file gives it the
one piece of broker arithmetic it lacks — a filled sell reduces the
position and raises cash — attached from the outside the way
`ops/rehearsal/broker_amend.py` attaches the amend endpoint. Nothing the
desk decides is modelled; only what a broker's ledger does after a fill.

NOT COVERED: the morning lane's conviction-ordered cut (it needs a fresh
per-seat read and a PM book); the ROTATION path — a held name judged
below the entry bar and sold to fund a new name — which is a Portfolio
Manager decision rendered into its prompt, so no deterministic input
drives it; a trim that the stand-in cannot fill in whole; shorts; the
cash-sweep vehicle; anything a model seat says — every seat is scripted.
"""

from __future__ import annotations

import json
import math
import time
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from src.models import (
    MacroNarrative,
    NewsIntelligenceReport,
    OHLCV,
    RiskReasoningChain,
    RiskVerdict,
)
from src.models.positions import (
    PositionAction,
    PositionReasoningChain,
    PositionReview,
)
import tests.test_e2e_morning_session as morning
from tests.test_e2e_morning_session import (
    SESSION_AT,
    _build_config,
    _earnings_feed_stub,
    _macro_feed_stub,
    _market_stub,
    _news_feed_stub,
    _scripted_model_seats,
)

CLOSE_AT = SESSION_AT.replace(hour=15, minute=30)
ENTRY_AT = CLOSE_AT - timedelta(days=14)
PRICE = 100.0  # every name marks here today
N_BARS = 160

#: symbol -> (qty, avg entry, resting stop). Entered two weeks ago.
#: XLF is the one loser (entry above today's mark); XLE the one winner;
#: XLV flat. Every stop sits below the mark and short of the +1R
#: breakeven trigger (price < entry + (entry - stop)), so neither the
#: alignment exit nor the ratchet has anything to say.
BOOK: dict[str, tuple[float, float, float]] = {
    "XLF": (40.0, 101.0, 95.0),
    "XLE": (40.0, 98.0, 93.0),
    "XLV": (40.0, 100.0, 95.0),
}
HELD_VALUE = sum(qty * PRICE for qty, _, _ in BOOK.values())  # 12,000
CASH = -3_500.0  # borrowed: the book is on margin
EQUITY = CASH + HELD_VALUE  # 8,500
PEAK_EQUITY = 10_200.0  # the high-water mark on the curve
DRAWDOWN_PCT = (EQUITY / PEAK_EQUITY - 1.0) * 100.0  # -16.67%


def _ladder_ceiling_x(drawdown_pct: float) -> float:
    """The ratified ladder, restated from config/settings.yaml so the
    expectation does not read the code it is checking."""
    assert -20.0 < drawdown_pct <= -15.0, drawdown_pct
    return 1.0


CEILING_X = _ladder_ceiling_x(DRAWDOWN_PCT)
CEILING_USD = CEILING_X * EQUITY  # 8,500
OVER = HELD_VALUE - CEILING_USD  # 3,500
LOSER = min(BOOK, key=lambda s: BOOK[s][0] * (PRICE - BOOK[s][1]))  # XLF
LOSER_QTY, _, LOSER_STOP = BOOK[LOSER]
# The cut sells the loser WHOLE (owner ruling 2026-10-09), even though
# OVER is smaller than its value: no residual is left to re-protect.
EXPECTED_SELL_QTY = float(LOSER_QTY)
RESIDUAL_QTY = LOSER_QTY - EXPECTED_SELL_QTY  # 0


def _bars() -> list[OHLCV]:
    """A straight climb into PRICE, so the close sits above every chart
    mark and no alignment reading can call the move over."""
    bars = []
    d = CLOSE_AT.date() - timedelta(days=int(N_BARS * 1.5) + 2)
    i = 0
    while len(bars) < N_BARS:
        d += timedelta(days=1)
        if d.weekday() >= 5:
            continue
        close = PRICE - 15.0 * (N_BARS - 1 - i) / (N_BARS - 1)
        bars.append(
            OHLCV(
                date=d,
                open=round(close - 0.1, 2),
                high=round(close + 0.4, 2),
                low=round(close - 0.4, 2),
                close=round(close, 2),
                volume=1_000_000,
            )
        )
        i += 1
    assert abs(bars[-1].close - PRICE) < 1e-9 and bars[-1].date < CLOSE_AT.date()
    return bars


def _scripts() -> dict[str, dict]:
    review = PositionReview(
        reasoning_chain=PositionReasoningChain(
            macro_continuity_check="x",
            thesis_progress_check="x",
            thesis_integrity_check="x",
            winners_discipline_check="x",
            session_disposition_check="x",
            execution_rationale="x",
        ),
        actions=[PositionAction(action="HOLD", symbol=s, reason="thesis intact") for s in BOOK],
        overall_assessment="scripted",
        risk_level="low",
    )
    rm = RiskVerdict(
        approved=True,
        modifications=[],
        reasoning="approved",
        reasoning_chain=RiskReasoningChain(
            rr_audit="x",
            signal_fidelity="x",
            correlation_check="x",
            event_risk="x",
            sizing_sanity="x",
            overall="x",
        ),
    )
    news = NewsIntelligenceReport(
        macro_narrative=MacroNarrative(
            last_updated=str(CLOSE_AT.date()),
            era_themes=["synthetic"],
            current_regime="risk-on",
        ),
        state_changes=[],
        stock_news={},
        pm_briefing="stub",
        market_sentiment="neutral",
        confidence="medium",
    )
    return {
        "position": json.loads(review.model_dump_json()),
        "risk": json.loads(rm.model_dump_json()),
        "news": json.loads(news.model_dump_json()),
    }


def _market(bars: list[OHLCV]):
    m = _market_stub()
    m.get_ohlcv.side_effect = lambda symbol, lookback_days=120: list(bars)
    m.get_ohlcv_batch.side_effect = lambda symbols, lookback_days=120: {s: list(bars) for s in symbols}
    m.get_upcoming_ex_dividend.return_value = None
    m.get_valuation_metrics.return_value = None
    return m


def _seed_profiles(tmp_path: Path) -> None:
    """A warm identity cache, as production has (see the protection test)."""
    from src.data.company import CompanyProfile, CompanyProfileStore

    store = CompanyProfileStore(cache_path=str(tmp_path / "data" / "company_profiles.json"))
    for symbol in BOOK:
        payload = CompanyProfile(symbol=symbol, name=f"Synthetic {symbol}").as_dict()
        payload["_fetched_at"] = time.time()
        store._cache[symbol] = payload
    store._save()


def _seed_book_and_curve(db) -> None:
    """The entry rows the desk wrote two weeks ago, and the equity curve
    whose high-water mark sets today's drawdown."""
    for symbol, (qty, entry, stop) in BOOK.items():
        row_id = db.insert_trade(
            symbol,
            "BUY",
            qty,
            entry,
            "synthetic entry",
            "seed-entry",
            stop_loss=stop,
            take_profit=entry + 10.0,
            fill_status="filled",
            setup_type="range",
            expected_horizon_sessions=60,
            thesis_invalid_if=f"closes below {stop}",
        )
        db.conn.execute(
            "UPDATE trades SET timestamp=? WHERE id=?",
            (ENTRY_AT.strftime("%Y-%m-%d %H:%M:%S"), row_id),
        )
    db.conn.commit()
    day = ENTRY_AT.date()
    for value in (PEAK_EQUITY - 200.0, PEAK_EQUITY, PEAK_EQUITY - 600.0):
        db.insert_daily_pnl(str(day), value, 0.0, 0.0)
        day += timedelta(days=1)


def _give_fill_ledger(trading):
    """A filled SELL reduces the stand-in's position and raises its cash —
    broker arithmetic the stand-in lacks, nothing the desk decides."""
    snap = trading._snapshot
    real_submit = trading.submit_order

    def _submit(request):
        answer = real_submit(request)
        order = trading._orders[answer.id]
        if order.status == "filled" and order.side == "sell":
            for p in snap.positions:
                if p["symbol"] == order.symbol:
                    p["qty"] = float(p["qty"]) - order.qty
                    p["market_value"] = p["qty"] * float(p["current_price"])
                    p["unrealized_pnl"] = p["qty"] * (float(p["current_price"]) - float(p["avg_entry"]))
            snap.cash += order.qty * float(answer.filled_avg_price)
            snap.positions[:] = [p for p in snap.positions if p["qty"] > 0]
        return answer

    trading.submit_order = _submit
    return trading


def _run_close(tmp_path: Path, monkeypatch):
    from ops.rehearsal.broker import BrokerSnapshot, install_rehearsal_broker
    from ops.rehearsal.clock import frozen_clock
    from ops.rehearsal.network_wall import no_network
    from ops.rehearsal.runner import _sentinel_credentials
    from src.agents.base import reset_route_breakers
    from src.trading_calendar import ET

    bars = _bars()
    reset_route_breakers()
    monkeypatch.chdir(tmp_path)
    _seed_profiles(tmp_path)
    config = _build_config(tmp_path)
    now = CLOSE_AT.replace(tzinfo=ET)
    trace: list = []
    monkeypatch.setattr(morning, "_scripted_answers", _scripts)
    attempts: list[str] = []
    gross_seen: list[float] = []
    with (
        no_network(attempts),
        _sentinel_credentials(),
        patch("src.pipeline.MarketDataProvider", return_value=_market(bars)),
        patch("src.pipeline.MacroDataProvider", return_value=_macro_feed_stub()),
        patch("src.pipeline.NewsDataProvider", return_value=_news_feed_stub()),
        patch("src.pipeline.EarningsDataProvider", return_value=_earnings_feed_stub()),
        frozen_clock(now, run_id="e2e-close-delever"),
        _scripted_model_seats(trace),
    ):
        from src.pipeline import TradingPipeline

        pipeline = TradingPipeline(config)
        _seed_book_and_curve(pipeline.db)
        snapshot = BrokerSnapshot(
            as_of=now.date(),
            cash=CASH,
            portfolio_value=EQUITY,
            last_equity=EQUITY,
            positions=[
                {
                    "symbol": s,
                    "qty": qty,
                    "avg_entry": entry,
                    "current_price": PRICE,
                    "market_value": qty * PRICE,
                    "unrealized_pnl": qty * (PRICE - entry),
                    "sector": "ETF",
                }
                for s, (qty, entry, _) in BOOK.items()
            ],
            prices={s: PRICE for s in BOOK},
            standing_stops={s: stop for s, (_, _, stop) in BOOK.items()},
        )
        trading = _give_fill_ledger(
            install_rehearsal_broker(pipeline.broker, snapshot, now=now),
        )
        from types import SimpleNamespace

        symbols_of = pipeline.broker._data_client._symbols
        pipeline.broker._data_client.get_stock_latest_trade = lambda request: {
            sym: SimpleNamespace(price=PRICE, timestamp=now) for sym in symbols_of(request)
        }
        pipeline.broker.get_intraday_snapshots = lambda symbols, *a, **k: {
            s: {"last_price": PRICE, "last_trade_at": now} for s in symbols
        }
        # Gross exposure at EVERY read of the book the session makes.
        real_positions = pipeline.broker.get_positions

        def _positions(*a, **k):
            out = real_positions(*a, **k)
            gross_seen.append(sum(abs(float(p.market_value)) for p in out))
            return out

        pipeline.broker.get_positions = _positions
        result = pipeline.run_close()
    return result, trace, trading, attempts, gross_seen


def _resting_sell_stops(trading, symbol: str) -> list:
    return [
        o
        for o in trading._orders.values()
        if o.symbol == symbol
        and o.side == "sell"
        and "stop" in o.order_type
        and o.status in ("pre_existing", "new", "accepted")
    ]


def test_close_on_a_levered_book_in_drawdown_sells_the_loser_down_to_the_ladder(
    tmp_path,
    monkeypatch,
):
    assert math.isclose(HELD_VALUE / EQUITY, 1.4117647, abs_tol=1e-6)
    result, trace, trading, attempts, gross_seen = _run_close(tmp_path, monkeypatch)

    # the wall
    assert attempts == [], f"the session tried to leave the box: {attempts}"
    assert result["status"] == "reviewed", {k: v for k, v in result.items() if k in ("status", "error", "orders")}
    assert [k for _, k in trace].count("position") >= 1, trace

    # the ladder, as the session read it
    lev = result["leverage"]
    assert math.isclose(lev["drawdown_pct"], DRAWDOWN_PCT, abs_tol=0.01), lev
    assert lev["ceiling_x"] == CEILING_X, lev

    # the cut: one sale, the loser, sold whole
    sells = [o for o in trading.submitted if o.side == "sell" and "stop" not in o.order_type]
    assert [(o.symbol, o.qty, o.status) for o in sells] == [(LOSER, EXPECTED_SELL_QTY, "filled")], [
        o.as_plain() for o in trading.submitted
    ]
    sale = sells[0]
    assert sale.order_type == "limit" and sale.limit_price == PRICE, sale.as_plain()
    buys = [o for o in trading.submitted if o.side == "buy"]
    assert buys == [], [o.as_plain() for o in buys]

    # exposure actually reduced, and stayed under the ceiling afterwards
    assert gross_seen and gross_seen[0] == HELD_VALUE, gross_seen
    after_sale = HELD_VALUE - EXPECTED_SELL_QTY * PRICE
    assert after_sale <= CEILING_USD + 1e-6, (after_sale, CEILING_USD)
    assert gross_seen[-1] == after_sale, gross_seen
    assert all(g <= HELD_VALUE for g in gross_seen), gross_seen
    assert min(gross_seen) == after_sale, gross_seen
    assert trading._snapshot.cash == CASH + EXPECTED_SELL_QTY * PRICE

    # protection: the sold name's stop cleared before the sale, one new
    # stop behind the residual at the cleared level; the others untouched
    assert trading.cancelled == [f"pre-existing-stop-{LOSER}"], trading.cancelled
    loser_stops = _resting_sell_stops(trading, LOSER)
    assert RESIDUAL_QTY == 0
    assert loser_stops == [], [s.as_plain() for s in loser_stops]
    for symbol, (qty, _, stop) in BOOK.items():
        if symbol == LOSER:
            continue
        stops = _resting_sell_stops(trading, symbol)
        assert [(s.qty, s.stop_price, s.status) for s in stops] == [(qty, stop, "pre_existing")], [
            s.as_plain() for s in stops
        ]
    assert result["stop_coverage_gaps"] == [], result["stop_coverage_gaps"]
