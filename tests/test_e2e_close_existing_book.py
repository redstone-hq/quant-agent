"""The hermetic CLOSE session, starting with a position already on the book.

tests/test_e2e_morning_session.py and tests/test_e2e_morning_protection.py
drive a morning session on an EMPTY book and judge the buy it makes. This
file drives `TradingPipeline(config).run_close()` on a book that already
holds one long with one resting protective stop — the same production
objects, the same seat seam, the same rehearsal broker stand-in and the
same socket-level network wall — and judges what the desk does WITH it.

The reviewer seat says HOLD in every case but one, so what the desk does
is decided by the INPUTS (the bars, the price, the resting stop), and every
expectation is derived from those inputs, never read back from the run:

  loaded        the held position is read from the broker stand-in, the
                seat sees exactly one position, and exactly one SELL stop
                rests behind it at the level that was on the book;
  exit          when the chart's own alignment reading says the move is
                over (`src/risk/alignment_exit.py`: the close sits below
                the last confirmed higher low) the desk sells the WHOLE
                position after clearing its stop; when no swing low has
                been broken it sells nothing — whatever the seat says;
  ratchet       the +1R breakeven ratchet (`src/risk/trailing.py`) moves
                the stop to the entry price when price >= entry + 1R and
                does NOT move it when price is short of +1R;
  one-way       a resting stop already ABOVE breakeven is left where it
                is when the ratchet would otherwise propose breakeven; a
                reviewer TRAIL_STOP below the resting stop is refused —
                a stop never moves in the direction that widens risk;
  the wall      no socket left the box and no order left the stand-in.

NOT COVERED: rotation / de-levering / the gross ceiling (one name, inside
every limit); the re-protect path after a PARTIAL fill (the stand-in fills
whole, so the WAL restore is driven only as far as "stop cleared before
the sell"); shorts; fractional legs; the structural / chandelier trail and
the SMA200 mark; anything a model seat says — every seat is scripted.
"""

from __future__ import annotations

import json
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
    SYMBOL,
    _build_config,
    _earnings_feed_stub,
    _macro_feed_stub,
    _market_stub,
    _news_feed_stub,
    _scripted_model_seats,
)
from tests.test_e2e_morning_protection import _seed_company_profile_cache

CLOSE_AT = SESSION_AT.replace(hour=15, minute=30)  # the close session
ENTRY_AT = CLOSE_AT - timedelta(days=14)  # position opened 2 weeks ago
QTY = 10.0
ENTRY = 95.0
INITIAL_STOP = 92.0  # R = 3.00 of initial risk
TARGET = 105.0  # the range high the entry was measured on
ONE_R_PRICE = ENTRY + (ENTRY - INITIAL_STOP)  # 98.00: the breakeven trigger
CASH = 9_050.0
N_BARS = 160


def _bars(*, end: float, crash: bool = False) -> list[OHLCV]:
    """Daily bars ending on the session's prior close.

    A straight climb into `end` (no swing low is ever confirmed, so the
    structure test has nothing broken to sell on); or, with `crash`, the
    same climb to `end + 12` with ONE three-point dip twenty sessions
    before the top — a confirmed higher low — followed by ten sessions
    straight down into `end`, which closes below that low (the test proves
    this from the inputs).
    """
    bars = []
    d = CLOSE_AT.date() - timedelta(days=int(N_BARS * 1.5) + 2)
    top = end + 12.0 if crash else end
    climb = N_BARS - 10 if crash else N_BARS
    i = 0
    while len(bars) < N_BARS:
        d += timedelta(days=1)
        if d.weekday() >= 5:
            continue
        if i < climb:
            close = top - 15.0 * (climb - 1 - i) / (climb - 1)
            if crash and i == climb - 20:
                close -= 3.0
        else:
            close = top - 12.0 * (i - climb + 1) / 10.0
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
    assert abs(bars[-1].close - end) < 1e-9 and bars[-1].date < CLOSE_AT.date()
    return bars


def _reviewer_says(actions: list[dict]) -> dict:
    review = PositionReview(
        reasoning_chain=PositionReasoningChain(
            macro_continuity_check="x",
            thesis_progress_check="x",
            thesis_integrity_check="x",
            winners_discipline_check="x",
            session_disposition_check="x",
            execution_rationale="x",
        ),
        actions=[PositionAction(**a) for a in actions],
        overall_assessment="scripted",
        risk_level="low",
    )
    return json.loads(review.model_dump_json())


def _risk_says_yes() -> dict:
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
    return json.loads(rm.model_dump_json())


def _news_says_nothing() -> dict:
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
    return json.loads(news.model_dump_json())


def _market(bars: list[OHLCV]):
    """The morning stub's shape over THESE bars, plus the two reads a held
    book makes: no dividend is pending and no valuation is wanted."""
    m = _market_stub()
    m.get_ohlcv.side_effect = lambda symbol, lookback_days=120: list(bars)
    m.get_ohlcv_batch.side_effect = lambda symbols, lookback_days=120: {s: list(bars) for s in symbols}
    m.get_upcoming_ex_dividend.return_value = None
    m.get_valuation_metrics.return_value = None
    return m


def _seed_open_position(db) -> None:
    """The opening row the desk wrote when it bought, two weeks ago."""
    row_id = db.insert_trade(
        SYMBOL,
        "BUY",
        QTY,
        ENTRY,
        "synthetic range entry",
        "seed-entry",
        stop_loss=INITIAL_STOP,
        take_profit=TARGET,
        fill_status="filled",
        setup_type="range",
        expected_horizon_sessions=60,
        thesis_invalid_if=f"closes below {INITIAL_STOP}",
    )
    db.conn.execute(
        "UPDATE trades SET timestamp=? WHERE id=?",
        (ENTRY_AT.strftime("%Y-%m-%d %H:%M:%S"), row_id),
    )
    db.conn.commit()


def _seed_technical_read(db, rating: str) -> None:
    """Today's technical read of the holding, as the half-hour scan files it
    (`specialist_evidence`, kind `analysis`) — what the same-rules gate reads
    on the close review (owner 13 Oct 2026: a sale is judged on the views a
    buy would be). Levels are the close file's constants; nothing is picked."""
    from src.models import TechAnalysisResult

    bearish = rating in ("sell", "strong_sell")
    read = TechAnalysisResult(
        symbol=SYMBOL,
        rating=rating,
        conviction="high",
        thesis_invalid_if=f"closes below {INITIAL_STOP}",
        reasoning="scripted close-session technical read",
        entry_price=ENTRY,
        stop_loss=TARGET if bearish else INITIAL_STOP,  # a sell read sits its stop at the range high
        reference_target=INITIAL_STOP if bearish else TARGET,
        setup_type="range",
        expected_horizon_sessions=60,
        support_levels=[INITIAL_STOP],
        resistance_levels=[TARGET],
        reasoning_chain={
            "trend": "down",
            "momentum": "weak",
            "volatility": "normal",
            "support_resistance": "range",
            "volume": "average",
        },
    )
    row_id = db.insert_specialist_evidence(
        run_id="seed-tech",
        agent_name="technical",
        kind="analysis",
        scope="symbol",
        symbol=SYMBOL,
        evidence_json=read.model_dump_json(),
    )
    db.conn.execute(
        "UPDATE specialist_evidence SET timestamp=? WHERE id=?",
        (CLOSE_AT.strftime("%Y-%m-%d %H:%M:%S"), row_id),
    )
    db.conn.commit()


HOLD = [{"action": "HOLD", "symbol": SYMBOL, "reason": "thesis intact"}]


def _run_close(
    tmp_path: Path,
    monkeypatch,
    *,
    bars: list[OHLCV],
    standing_stop: float = INITIAL_STOP,
    actions: list[dict] = HOLD,
    tech_rating: str | None = None,
):
    from ops.rehearsal.broker import BrokerSnapshot, install_rehearsal_broker
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
    now = CLOSE_AT.replace(tzinfo=ET)
    trace: list = []
    answers = {
        "position": _reviewer_says(actions),
        "risk": _risk_says_yes(),
        "news": _news_says_nothing(),
    }
    # The morning file's seat seam (the four provider transports on
    # `BaseAgent`), answering from THIS session's script.
    monkeypatch.setattr(morning, "_scripted_answers", lambda: answers)
    attempts: list[str] = []
    with (
        no_network(attempts),
        _sentinel_credentials(),
        patch("src.pipeline.MarketDataProvider", return_value=_market(bars)),
        patch("src.pipeline.MacroDataProvider", return_value=_macro_feed_stub()),
        patch("src.pipeline.NewsDataProvider", return_value=_news_feed_stub()),
        patch("src.pipeline.EarningsDataProvider", return_value=_earnings_feed_stub()),
        frozen_clock(now, run_id="e2e-close"),
        _scripted_model_seats(trace),
    ):
        from src.pipeline import TradingPipeline

        pipeline = TradingPipeline(config)
        _seed_open_position(pipeline.db)
        if tech_rating is not None:
            _seed_technical_read(pipeline.db, tech_rating)
        snapshot = BrokerSnapshot(
            as_of=now.date(),
            cash=CASH,
            portfolio_value=CASH + QTY * price,
            last_equity=CASH + QTY * price,
            positions=[
                {
                    "symbol": SYMBOL,
                    "qty": QTY,
                    "avg_entry": ENTRY,
                    "current_price": price,
                    "market_value": QTY * price,
                    "unrealized_pnl": QTY * (price - ENTRY),
                    "sector": "ETF",
                }
            ],
            prices={SYMBOL: price},
            standing_stops={SYMBOL: standing_stop},
        )
        # The stand-in lacks the broker's in-place amend endpoint, which
        # is the trail's preferred route; `ops/rehearsal/broker_amend.py`.
        trading = give_amend_endpoint(
            install_rehearsal_broker(pipeline.broker, snapshot, now=now),
        )
        from types import SimpleNamespace

        symbols_of = pipeline.broker._data_client._symbols
        pipeline.broker._data_client.get_stock_latest_trade = lambda request: {
            sym: SimpleNamespace(price=price, timestamp=now) for sym in symbols_of(request)
        }
        pipeline.broker.get_intraday_snapshots = lambda symbols, *a, **k: {
            s: {"last_price": price, "last_trade_at": now} for s in symbols
        }
        result = pipeline.run_close()
    return result, trace, trading, attempts


def _resting_sell_stops(trading) -> list:
    return [
        o
        for o in trading._orders.values()
        if o.symbol == SYMBOL
        and o.side == "sell"
        and "stop" in o.order_type
        and o.status in ("pre_existing", "new", "accepted")
    ]


def _closing_sells(trading) -> list:
    return [o for o in trading.submitted if o.side == "sell" and "stop" not in o.order_type]


def _assert_hermetic(result: dict, trace: list, trading, attempts: list) -> None:
    assert attempts == [], f"the session tried to leave the box: {attempts}"
    assert result["status"] == "reviewed", {k: v for k, v in result.items() if k in ("status", "error", "orders")}
    assert result.get("positions") == 1, result.get("positions")
    assert [k for _, k in trace].count("position") >= 1, trace
    assert result["stop_coverage_gaps"] == [], result["stop_coverage_gaps"]


def _assert_untouched(trading, stop: float) -> None:
    assert trading.submitted == [], [o.as_plain() for o in trading.submitted]
    assert trading.cancelled == [] and trading.amended == [], (
        trading.cancelled,
        trading.amended,
    )
    stops = _resting_sell_stops(trading)
    assert [(s.stop_price, s.qty) for s in stops] == [(stop, QTY)], [s.as_plain() for s in stops]


def _swing_lows_of(bars: list[OHLCV]) -> list[float]:
    """The confirmed swing lows the alignment exit reads, from the inputs."""
    from src.risk.trail_structure import _swing_lows

    return _swing_lows(bars)


def test_trend_intact_short_of_one_r_leaves_the_book_and_its_stop_alone(tmp_path, monkeypatch):
    bars = _bars(end=ONE_R_PRICE - 1.0)  # 97.00: below the breakeven trigger
    assert _swing_lows_of(bars) == []
    result, trace, trading, attempts = _run_close(tmp_path, monkeypatch, bars=bars)
    _assert_hermetic(result, trace, trading, attempts)
    _assert_untouched(trading, INITIAL_STOP)


def test_trend_intact_at_one_r_ratchets_the_stop_up_to_breakeven(tmp_path, monkeypatch):
    bars = _bars(end=ONE_R_PRICE + 1.0)  # 99.00: past the breakeven trigger
    assert _swing_lows_of(bars) == []
    result, trace, trading, attempts = _run_close(tmp_path, monkeypatch, bars=bars)
    _assert_hermetic(result, trace, trading, attempts)
    assert _closing_sells(trading) == [], [o.as_plain() for o in trading.submitted]
    stops = _resting_sell_stops(trading)
    assert [(s.stop_price, s.qty) for s in stops] == [(ENTRY, QTY)], (
        f"expected the +1R breakeven ratchet {INITIAL_STOP} -> {ENTRY}; "
        f"resting: {[s.as_plain() for s in stops]}; submitted: "
        f"{[o.as_plain() for o in trading.submitted]}"
    )
    assert stops[0].stop_price > INITIAL_STOP
    assert str(stops[0].time_in_force).lower() == "gtc", stops[0].as_plain()
    # Amended in place: nothing cancelled, nothing re-submitted, the old
    # order marked replaced — one open stop covered the position throughout.
    assert trading.cancelled == [] and trading.submitted == [], (
        trading.cancelled,
        [o.as_plain() for o in trading.submitted],
    )
    old_id = f"pre-existing-stop-{SYMBOL}"
    assert [(a, b, f["stop_price"]) for a, b, f in trading.amended] == [
        (old_id, stops[0].order_id, ENTRY),
    ], trading.amended
    assert trading._orders[old_id].status == "replaced"


def test_a_stop_already_above_breakeven_is_never_lowered_to_it(tmp_path, monkeypatch):
    above = ENTRY + 1.0  # 96.00 rests; ratchet would say 95.00
    bars = _bars(end=ONE_R_PRICE + 1.0)
    result, trace, trading, attempts = _run_close(
        tmp_path,
        monkeypatch,
        bars=bars,
        standing_stop=above,
    )
    _assert_hermetic(result, trace, trading, attempts)
    _assert_untouched(trading, above)


def test_a_reviewer_trail_stop_below_the_resting_stop_is_refused(tmp_path, monkeypatch):
    bars = _bars(end=ONE_R_PRICE - 1.0)
    lower = INITIAL_STOP - 2.0
    result, trace, trading, attempts = _run_close(
        tmp_path,
        monkeypatch,
        bars=bars,
        actions=[
            {
                "action": "TRAIL_STOP",
                "symbol": SYMBOL,
                "reason": "give it more room",
                "new_stop_price": lower,
            }
        ],
    )
    _assert_hermetic(result, trace, trading, attempts)
    _assert_untouched(trading, INITIAL_STOP)


def _assert_whole_position_sold_after_clearing_its_stop(trading) -> None:
    sells = _closing_sells(trading)
    assert len(sells) == 1, [o.as_plain() for o in trading.submitted]
    sell = sells[0]
    assert sell.symbol == SYMBOL and float(sell.qty) == QTY, sell.as_plain()
    assert sell.status == "filled", sell.as_plain()
    assert trading.cancelled == [f"pre-existing-stop-{SYMBOL}"], (
        f"the resting stop must be cleared before the sell: {trading.cancelled}"
    )
    assert trading.submitted.index(sell) == 0, (
        f"nothing may be submitted before the exit: {[o.as_plain() for o in trading.submitted]}"
    )


def test_a_close_below_the_last_higher_low_exits_the_whole_position(tmp_path, monkeypatch, caplog):
    import logging

    caplog.set_level(logging.INFO, logger="src.pipeline")
    bars = _bars(end=ONE_R_PRICE - 1.0, crash=True)
    lows = _swing_lows_of(bars)
    assert lows and bars[-1].close < lows[-1], (lows, bars[-1].close)
    result, trace, trading, attempts = _run_close(tmp_path, monkeypatch, bars=bars)
    _assert_hermetic(result, trace, trading, attempts)
    _assert_whole_position_sold_after_clearing_its_stop(trading)
    assert len(result["orders"]) == 1 and result["orders"][0]["status"] == "filled"
    # The seat said HOLD; the chart reading raised the exit.
    assert f"Alignment exit SELL {SYMBOL}: EXIT" in caplog.text, caplog.text[-2000:]


REVIEWER_THESIS_SELL = [
    {
        "action": "SELL",
        "symbol": SYMBOL,
        "reason": "thesis invalidated: closed below the support the entry was measured on",
        "exit_trigger": "thesis_invalid",
        "trigger_evidence": f"thesis_invalid_if was 'closes below "
        f"{INITIAL_STOP}'; the structure that held "
        f"the range is gone",
    }
]


def test_a_reviewer_sell_on_an_intact_chart_with_no_reading_against_it_is_refused(tmp_path, monkeypatch):
    """Owner Option A, 14 Oct 2026: a held name is kept while it passes every
    buy rule except timing. The chart is intact (last close above the thesis
    level) and no seat reads against the holding today, so the same-rules gate
    refuses the reviewer's SELL: unknown never sells, the stop stays."""
    bars = _bars(end=ONE_R_PRICE - 1.0)
    result, trace, trading, attempts = _run_close(tmp_path, monkeypatch, bars=bars, actions=REVIEWER_THESIS_SELL)
    _assert_hermetic(result, trace, trading, attempts)
    _assert_untouched(trading, INITIAL_STOP)


def test_a_substantiated_reviewer_sell_exits_the_whole_position(tmp_path, monkeypatch):
    """Today's technical read turned AGAINST the holding (rating sell): under
    Option A the chart is against it, a buy would be refused on it, so the
    same-rules gate lets the reviewer's SELL through and it exits whole."""
    bars = _bars(end=ONE_R_PRICE - 1.0)
    result, trace, trading, attempts = _run_close(
        tmp_path, monkeypatch, bars=bars, actions=REVIEWER_THESIS_SELL, tech_rating="sell"
    )
    _assert_hermetic(result, trace, trading, attempts)
    _assert_whole_position_sold_after_clearing_its_stop(trading)
    assert [k for _, k in trace].count("risk") >= 1, trace
