"""Hermetic ROTATION: the desk sells a held name to fund a new one, same session.

Same harness as tests/test_e2e_morning_session.py (read it first) and the
close-session file tests/test_e2e_close_existing_book.py, whose book-seeding
and expectations-from-inputs style this follows. `run_morning()` runs on a
book that ALREADY holds one long with its protective stop resting, and
almost no cash, so a new buy can only be paid for with the held name's
sale proceeds. The portfolio-manager seat (scripted) asks for the new name
at a target weight and closes the held name (weight 0); everything after
the wire is production: constructor, risk engine, Risk Manager, execution.

Doctrine asserted (never read back from the run):
  * the held name is sold WHOLE, and its resting stop is cleared first;
  * the sale comes before the buy (the proceeds fund it);
  * the buy costs more than the cash the account started with, and no more
    than cash plus the sale proceeds;
  * the buy is sized by the target weight of the portfolio, whole shares;
  * exactly ONE GTC SELL stop rests against the new name, whole size, below
    its entry, and none rests against the sold name;
  * no gap in broker-truth stop coverage, and no socket left the box.

The held name's chart is AGAINST it (technical rating sell): owner Option A
(14 Oct 2026) keeps a name that passes every buy rule but timing, so a
neutral or unreadable holding is never sold by judgement. The same-rules gate
lets this sale through because the holding fails a rule a buy would fail.

NOT COVERED: the desk's AUTOMATIC below-bar rotation tier (the PM asks for
this rotation; the desk's own culling is not driven); partial fills;
shorts; a refused buy leg; anything a model seat says beyond this script.
"""

from __future__ import annotations

import math
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

import tests.test_e2e_morning_session as morning
from src.models import (
    AnalystProvenance,
    PortfolioDecision,
    ReasoningChain,
    TargetPosition,
    TechAnalysisResult,
    TechReasoningChain,
)
from tests.test_e2e_morning_protection import _seed_company_profile_cache
from tests.test_e2e_morning_session import (
    BARS,
    LAST_CLOSE,
    RANGE_HIGH,
    RANGE_LOW,
    SESSION_AT,
    _build_config,
    _earnings_feed_stub,
    _macro_feed_stub,
    _market_stub,
    _news_feed_stub,
    _scripted_model_seats,
)

NEW, OLD = "SPY", "XLE"  # synthetic series (different sectors: Broad, Energy)
HELD_QTY = 50.0
ENTRY = 95.0
HELD_STOP = 92.0
CASH = 100.0  # far less than the new position costs
TARGET_WEIGHT_PCT = 20.0
OPEN_ID = f"pre-existing-stop-{OLD}"


def _answers(close_held: bool = True) -> dict:
    """The morning script, with a held name that the PM closes and a new
    name it opens at TARGET_WEIGHT_PCT."""
    base = morning._scripted_answers()
    new_tech = base["tech"]["results"][0]
    old_tech = TechAnalysisResult(
        symbol=OLD,
        rating="sell" if close_held else "buy",
        entry_price=LAST_CLOSE,
        # A sell read sits its target below the entry and its stop above it.
        reference_target=RANGE_LOW if close_held else RANGE_HIGH,
        stop_loss=round(RANGE_HIGH + 1.0, 2) if close_held else round(RANGE_LOW - 1.0, 2),
        support_levels=[RANGE_LOW],
        resistance_levels=[RANGE_HIGH],
        setup_type="range",
        expected_horizon_sessions=60,
        reasoning="range exhausted, chart turned against the holding",
        thesis_invalid_if="closes below support",
        reasoning_chain=TechReasoningChain(
            trend="x",
            momentum="x",
            volatility="x",
            volume="x",
            support_resistance="x",
        ),
    )
    import json

    base["tech"] = {"results": [new_tech, json.loads(old_tech.model_dump_json(exclude_none=True))]}
    prov = [
        AnalystProvenance(
            source="technical",
            observed_stance="buy",
            relationship="supports",
            evidence="rating buy, trend up",
        )
    ]
    pm = PortfolioDecision(
        reasoning_chain=ReasoningChain(
            macro_filter="x",
            news_check="x",
            earnings_check="x",
            signal_conflicts="x",
            sizing_logic="x",
            portfolio_balance="x",
            cash_target="x",
        ),
        targets=[
            TargetPosition(
                symbol=NEW,
                target_weight_pct=TARGET_WEIGHT_PCT,
                conviction="high",
                thesis="synthetic",
                thesis_invalid_if="closes below support",
                provenance=prov,
            ),
            TargetPosition(
                symbol=OLD,
                target_weight_pct=0.0,
                conviction="high",
                thesis="rotated out to fund the stronger name",
                thesis_invalid_if="closes below support",
                provenance=[
                    AnalystProvenance(
                        source="technical",
                        observed_stance="sell",
                        relationship="supports",
                        evidence="rating sell, chart against the holding",
                    )
                ],
            ),
        ],
        portfolio_view="rotate",
    )
    base["portfolio"] = json.loads(pm.model_dump_json())
    if not close_held:
        base["portfolio"]["targets"] = base["portfolio"]["targets"][:1]
    return base


def _seed_open_position(db) -> None:
    from datetime import timedelta

    row_id = db.insert_trade(
        OLD,
        "BUY",
        HELD_QTY,
        ENTRY,
        "synthetic range entry",
        "seed-entry",
        stop_loss=HELD_STOP,
        take_profit=RANGE_HIGH,
        fill_status="filled",
        setup_type="range",
        expected_horizon_sessions=60,
        thesis_invalid_if=f"closes below {HELD_STOP}",
    )
    db.conn.execute(
        "UPDATE trades SET timestamp=? WHERE id=?",
        ((SESSION_AT - timedelta(days=14)).strftime("%Y-%m-%d %H:%M:%S"), row_id),
    )
    db.conn.commit()


def _seed_company_profile_cache_for(tmp_path: Path, symbol: str) -> None:
    """Warm the identity cache for the held name too (see the helper this
    extends: a cold entry sends the store to the network, which the wall
    journals)."""
    import time

    from src.data.company import CompanyProfile, CompanyProfileStore

    store = CompanyProfileStore(cache_path=str(tmp_path / "data" / "company_profiles.json"))
    payload = CompanyProfile(symbol=symbol, name="Synthetic Held ETF").as_dict()
    payload["_fetched_at"] = time.time()
    store._cache[symbol] = payload
    store._save()


def _run_rotation(tmp_path: Path, monkeypatch, *, close_held: bool = True):
    from ops.rehearsal.broker import BrokerSnapshot, install_rehearsal_broker
    from ops.rehearsal.broker_amend import give_amend_endpoint
    from ops.rehearsal.clock import frozen_clock
    from ops.rehearsal.network_wall import no_network
    from ops.rehearsal.runner import _sentinel_credentials
    from src.agents.base import reset_route_breakers
    from src.trading_calendar import ET

    reset_route_breakers()
    monkeypatch.chdir(tmp_path)
    _seed_company_profile_cache(tmp_path)
    _seed_company_profile_cache_for(tmp_path, OLD)
    config = _build_config(tmp_path)
    config.trading.universe = [NEW, OLD]
    now = SESSION_AT.replace(tzinfo=ET)
    answers = _answers(close_held)
    monkeypatch.setattr(morning, "_scripted_answers", lambda: answers)
    trace: list = []
    attempts: list[str] = []
    with (
        no_network(attempts),
        _sentinel_credentials(),
        patch("src.pipeline.MarketDataProvider", return_value=_market_stub()),
        patch("src.pipeline.MacroDataProvider", return_value=_macro_feed_stub()),
        patch("src.pipeline.NewsDataProvider", return_value=_news_feed_stub()),
        patch("src.pipeline.EarningsDataProvider", return_value=_earnings_feed_stub()),
        frozen_clock(now, run_id="e2e-rotation"),
        _scripted_model_seats(trace),
    ):
        from src.pipeline import TradingPipeline

        pipeline = TradingPipeline(config)
        _seed_open_position(pipeline.db)
        value = CASH + HELD_QTY * LAST_CLOSE
        snapshot = BrokerSnapshot(
            as_of=now.date(),
            cash=CASH,
            portfolio_value=value,
            last_equity=value,
            positions=[
                {
                    "symbol": OLD,
                    "qty": HELD_QTY,
                    "avg_entry": ENTRY,
                    "current_price": LAST_CLOSE,
                    "market_value": HELD_QTY * LAST_CLOSE,
                    "unrealized_pnl": HELD_QTY * (LAST_CLOSE - ENTRY),
                    "sector": "Energy",
                }
            ],
            prices={NEW: LAST_CLOSE, OLD: LAST_CLOSE},
            standing_stops={OLD: HELD_STOP},
        )
        trading = give_amend_endpoint(
            install_rehearsal_broker(pipeline.broker, snapshot, now=now),
        )
        symbols_of = pipeline.broker._data_client._symbols
        pipeline.broker._data_client.get_stock_latest_trade = lambda request: {
            s: SimpleNamespace(price=LAST_CLOSE, timestamp=now) for s in symbols_of(request)
        }
        pipeline.broker.get_intraday_snapshots = lambda symbols, *a, **k: {
            s: {"last_price": LAST_CLOSE, "last_trade_at": now} for s in symbols
        }
        result = pipeline.run_morning()
    return result, trace, trading, attempts


def _assert_rotation(result: dict, trading, attempts: list) -> None:
    assert attempts == [], f"the session tried to leave the box: {attempts}"
    plain = [o.as_plain() for o in trading.submitted]
    assert result["status"] == "executed", ({k: result.get(k) for k in ("status", "error", "execution_skips")}, plain)
    sells = [o for o in trading.submitted if o.side == "sell" and "stop" not in o.order_type]
    buys = [o for o in trading.submitted if o.side.endswith("buy")]
    stops = [o for o in trading.submitted if o.order_type == "stop"]

    assert [(o.symbol, float(o.qty)) for o in sells] == [(OLD, HELD_QTY)], plain
    assert sells[0].status == "filled", plain
    assert OPEN_ID in trading.cancelled, (
        f"the held name's resting stop must be cleared for the sale: {trading.cancelled}"
    )
    assert [o.symbol for o in buys] == [NEW], plain
    buy = buys[0]
    assert buy.status == "filled", buy.as_plain()
    # A plain DAY market order (owner ruling 2026-10-09), sized against the
    # ask the rehearsal broker quotes: the snapshot price, zero spread.
    assert str(buy.order_type).lower() == "market" and buy.limit_price is None, buy.as_plain()
    buy_price = LAST_CLOSE
    assert trading.submitted.index(sells[0]) < trading.submitted.index(buy), (
        f"the sale must come first; its proceeds fund the buy: {plain}"
    )

    proceeds = HELD_QTY * LAST_CLOSE
    cost = float(buy.qty) * buy_price
    assert cost > CASH, f"buy {cost} was affordable without the sale: {plain}"
    assert cost <= CASH + proceeds, f"buy {cost} exceeds cash + proceeds"
    total = CASH + proceeds
    expected = math.floor(total * TARGET_WEIGHT_PCT / 100.0 / buy_price)
    assert float(buy.qty) == float(expected), (
        f"{TARGET_WEIGHT_PCT}% of {total:,.2f} at {buy_price} -> {expected} whole shares; desk bought {buy.qty}"
    )

    assert [o.symbol for o in stops] == [NEW], f"exactly one stop, against the new name: {plain}"
    stop = stops[0]
    assert stop.side == "sell" and float(stop.qty) == float(buy.qty), stop.as_plain()
    assert str(stop.time_in_force).lower() == "gtc", stop.as_plain()
    assert 0 < stop.stop_price < buy_price, stop.as_plain()
    assert trading.submitted.index(stop) > trading.submitted.index(buy), plain
    resting = [
        o
        for o in trading._orders.values()
        if o.side == "sell" and "stop" in o.order_type and o.status in ("pre_existing", "new", "accepted")
    ]
    assert [(o.symbol, float(o.qty)) for o in resting] == [(NEW, float(buy.qty))], (
        f"after the rotation one stop rests, on the new name only: {[o.as_plain() for o in resting]}"
    )
    assert result["stop_coverage_gaps"] == [], result["stop_coverage_gaps"]


def test_held_name_is_sold_to_fund_the_new_one_which_ends_with_one_stop(
    tmp_path,
    monkeypatch,
):
    result, trace, trading, attempts = _run_rotation(tmp_path, monkeypatch)
    _assert_rotation(result, trading, attempts)


def test_the_rotation_check_fails_when_the_sale_is_withheld(
    tmp_path,
    monkeypatch,
):
    """Sensitivity: the PM never closes the held name, so nothing funds the
    buy; the same checker must REJECT that run."""
    result, trace, trading, attempts = _run_rotation(
        tmp_path,
        monkeypatch,
        close_held=False,
    )
    with pytest.raises(AssertionError):
        _assert_rotation(result, trading, attempts)
    assert not [o for o in trading.submitted if o.side == "sell" and "stop" not in o.order_type]
