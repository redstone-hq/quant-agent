"""The rehearsal broker stand-in honours an in-place amend, and cannot be
silently under-implemented again.

Before this, the stand-in had no `replace_order_by_id`. The desk's preferred
stop-ratchet path (amend in place) got an AttributeError, classified it as
"no broker answer", cancelled nothing and moved nothing — and the rehearsal
reported PASS. Every rehearsal ever run had therefore exercised the stop
ratchet zero times. Offline: the production broker object runs over the
stand-in exactly as a rehearsal wires it.
"""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from ops.rehearsal.broker import (
    BrokerSnapshot,
    RehearsalDataClient,
    RehearsalTradingClient,
    install_rehearsal_broker,
)
from ops.rehearsal.stand_in import (
    AmendRefused,
    StandInGap,
    assert_stand_in_answered,
)
from src.trading_calendar import ET

NOW = datetime(2026, 10, 1, 11, 0, tzinfo=ET)
SYMBOL = "ZZZ"


def _snapshot(qty=10.0, stop=100.0, price=112.0):
    return BrokerSnapshot(
        as_of=NOW.date(),
        cash=5_000.0,
        portfolio_value=6_120.0,
        last_equity=6_000.0,
        positions=[
            {
                "symbol": SYMBOL,
                "qty": qty,
                "avg_entry": 95.0,
                "current_price": price,
                "market_value": qty * price,
                "unrealized_pnl": (price - 95.0) * qty,
            }
        ],
        prices={SYMBOL: price},
        standing_stops={SYMBOL: stop},
    )


def _open_stops(trading):
    from alpaca.trading.enums import QueryOrderStatus
    from alpaca.trading.requests import GetOrdersRequest

    orders = trading.get_orders(filter=GetOrdersRequest(status=QueryOrderStatus.OPEN, symbols=[SYMBOL], nested=True))
    return [o for o in orders if "stop" in str(o.order_type)]


@patch("src.execution.broker.TradingClient")
@patch("src.sector_reference._get_sector", return_value="Technology")
def test_rehearsal_moves_a_stop_by_in_place_amend(_sector, _tc):
    """Acceptance (a): the stop MOVES, by amend, with the old order NOT
    cancelled — the production `replace_stop_loss` over the stand-in."""
    from src.execution.broker import AlpacaBroker

    broker = AlpacaBroker(api_key="t", secret_key="t", paper=True)
    trading = install_rehearsal_broker(broker, _snapshot(), now=NOW)
    before = _open_stops(trading)
    assert [o.stop_price for o in before] == [100.0]
    old_id = before[0].id

    out = broker.replace_stop_loss(SYMBOL, 105.0)

    after = _open_stops(trading)
    print(
        f"\nSTOP BEFORE: {old_id} @ ${before[0].stop_price:.2f} "
        f"-> AFTER: {after[0].id} @ ${after[0].stop_price:.2f}; "
        f"cancelled={trading.cancelled}; amended={trading.amended}"
    )
    assert out is not None and out["amend_status"] == "accepted"
    assert len(after) == 1, "exactly one open stop at every instant"
    assert after[0].stop_price == 105.0 and after[0].id != old_id
    assert after[0].qty == 10.0 and after[0].order_type == "stop"
    assert trading.cancelled == [], "an amend never cancels"
    assert trading.get_order_by_id(old_id).status == "replaced"
    assert [a[:2] for a in trading.amended] == [(old_id, after[0].id)]
    assert trading.unsupported_calls == []
    assert assert_stand_in_answered(trading).startswith("every broker call")


def test_amend_of_a_dead_order_is_refused_like_the_broker():
    trading = RehearsalTradingClient(_snapshot(), now=NOW)
    (stop,) = _open_stops(trading)
    trading.cancel_order_by_id(stop.id)
    with pytest.raises(AmendRefused, match="not resting .canceled.; 422"):
        trading.replace_order_by_id(stop.id, SimpleNamespace(stop_price=105.0))
    assert trading.get_order_by_id(stop.id).status == "canceled"


def test_open_filter_hides_replaced_orders_but_all_shows_them():
    from alpaca.trading.enums import QueryOrderStatus
    from alpaca.trading.requests import GetOrdersRequest

    trading = RehearsalTradingClient(_snapshot(), now=NOW)
    (stop,) = _open_stops(trading)
    trading.replace_order_by_id(stop.id, SimpleNamespace(stop_price=103.0))
    everything = trading.get_orders(filter=GetOrdersRequest(status=QueryOrderStatus.ALL, symbols=[SYMBOL]))
    assert sorted(str(o.status) for o in everything) == ["new", "replaced"]
    assert [o.stop_price for o in _open_stops(trading)] == [103.0]


def test_unimplemented_broker_call_fails_loudly_and_is_journalled():
    """Acceptance (b): a call the stand-in cannot answer raises, is recorded,
    and the post-session check voids the run naming it."""
    trading = RehearsalTradingClient(_snapshot(), now=NOW)
    data = RehearsalDataClient(_snapshot())
    with pytest.raises(StandInGap, match="does not implement `get_watchlists`"):
        trading.get_watchlists()
    with pytest.raises(StandInGap, match="does not implement `get_news`"):
        data.get_news(None)
    # The desk catches broad exceptions on its money paths, so the raise
    # alone would be swallowed; the journal is what voids the run.
    assert trading.unsupported_calls == ["get_watchlists"]
    with pytest.raises(StandInGap) as exc:
        assert_stand_in_answered(trading, data)
    print(f"\nVOID: {exc.value}")
    assert "RehearsalTradingClient.get_watchlists" in str(exc.value)
    assert "RehearsalDataClient.get_news" in str(exc.value)
    assert "no verdict" in str(exc.value)
    # Python's own protocol lookups are untouched, so copying still works.
    with pytest.raises(AttributeError):
        trading.__deepcopy__


def test_designed_refusals_also_void_the_run():
    """`get_asset` and `close_position` answer the desk with a refusal the real
    broker would not give; both are gaps, not soft answers."""
    trading = RehearsalTradingClient(_snapshot(), now=NOW)
    with pytest.raises(Exception):
        trading.get_asset(SYMBOL)
    with pytest.raises(Exception):
        trading.close_position(SYMBOL)
    with pytest.raises(StandInGap, match="2 broker call"):
        assert_stand_in_answered(trading)


def test_run_cli_exits_2_and_says_void_on_a_stand_in_gap(capsys, tmp_path):
    """Acceptance (b), at the command line: the rehearsal does not carry on."""
    from ops.rehearsal import run as cli

    gap = StandInGap("1 broker call(s) the stand-in could not answer: RehearsalTradingClient.replace_order_by_id")
    with (
        patch("ops.rehearsal.isolation.Sandbox.prepare", return_value=MagicMock()),
        patch("ops.rehearsal.runner.run_rehearsal", side_effect=gap),
    ):
        code = cli.main(["--source-db", str(tmp_path / "x.db"), "--sandbox", str(tmp_path)])
    out = capsys.readouterr().out
    print(out)
    assert code == 2
    assert "REHEARSAL VOID — StandInGap" in out
    assert "could not answer: RehearsalTradingClient.replace_order_by_id" in out
