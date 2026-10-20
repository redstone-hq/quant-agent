from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import UUID

import pytest
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame
from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.trading.requests import LimitOrderRequest

from ops.rehearsal.broker_cassette import (
    BrokerCassette,
    BrokerCassetteError,
    BrokerReplayViolation,
    CassetteMismatch,
    MissingRecordedBrokerCall,
    RecordedBrokerError,
    RecordingBrokerClient,
    ReplayBrokerCassette,
    UnusedRecordedBrokerCalls,
    assert_broker_uses_replay,
    install_replay_broker_cassette,
)
from ops.rehearsal.public_bundle import assert_public_safe
from src.execution.broker import AlpacaBroker
from src.execution.broker_parts.stop_amend_pure import (
    _is_terminal_broker_rejection,
)
from src.execution.broker_parts.stop_rejections import (
    _is_held_for_orders_error,
)
from src.execution.broker_parts.submission_rejection import (
    _is_terminal_submission_rejection,
)
from src.execution.order_idempotency import (
    _is_duplicate_client_order_id_rejection,
)


ACCOUNT_NUMBER = "PASECONDARY123456"
ACCOUNT_ID = UUID("11111111-1111-4111-8111-111111111111")
ORDER_ID = UUID("00000000-0000-4000-8000-000000000002")
MISSING_ORDER_ID = UUID("00000000-0000-4000-8000-000000000003")
ACTIVITY_ID = "20261005000000001::abcd"


@dataclass
class _OrderRequest:
    symbol: str
    client_order_id: str


class _FakeAPIError(RuntimeError):
    status_code = 404


class _TradingClient:
    def get_account(self):
        return SimpleNamespace(
            account_number=ACCOUNT_NUMBER,
            id=ACCOUNT_ID,
            cash="8000.00",
            portfolio_value="10000.00",
            last_equity="9900.00",
            non_marginable_buying_power="7500.00",
        )

    def submit_order(self, *, order_data):
        return SimpleNamespace(
            id=ORDER_ID,
            client_order_id=order_data.client_order_id,
            symbol=order_data.symbol,
            status="accepted",
        )

    def get_order_by_id(self, order_id):
        assert order_id == ORDER_ID
        return SimpleNamespace(id=ORDER_ID, symbol="SPY", status="filled")

    def get_activities(self):
        return [SimpleNamespace(id=ACTIVITY_ID, activity_type="FILL")]

    def get_missing_order(self, order_id):
        raise _FakeAPIError(f"order {order_id} does not exist")

    def get_all_positions(self):
        return [
            SimpleNamespace(
                symbol="SPY",
                qty="2",
                avg_entry_price="500",
                current_price="510",
                market_value="1020",
                unrealized_pl="20",
                unrealized_intraday_pl="5",
            )
        ]


def _record_full_cassette():
    cassette = BrokerCassette()
    client = RecordingBrokerClient(_TradingClient(), cassette, "trading")
    account = client.get_account()
    order = client.submit_order(order_data=_OrderRequest("SPY", "qamc-capture-client-order-raw"))
    client.get_order_by_id(order.id)
    client.get_activities()
    with pytest.raises(_FakeAPIError):
        client.get_missing_order(MISSING_ORDER_ID)
    client.get_all_positions()
    return cassette.to_payload(), account, order


def test_capture_tokenizes_identifiers_and_replay_is_strict_and_shape_compatible(monkeypatch):
    payload, captured_account, captured_order = _record_full_cassette()

    # Recording is observational: callers still receive the real SDK objects.
    assert captured_account.account_number == ACCOUNT_NUMBER
    assert captured_order.id == ORDER_ID

    published = json.dumps(payload, sort_keys=True)
    for raw in (
        ACCOUNT_NUMBER,
        str(ACCOUNT_ID),
        str(ORDER_ID),
        str(MISSING_ORDER_ID),
        ACTIVITY_ID,
        "qamc-capture-client-order-raw",
    ):
        assert raw not in published
    assert "<QAMC:account:0001>" in published
    assert "<QAMC:order_id:0001>" in published
    assert "<QAMC:activity_id:0001>" in published
    assert "<QAMC:client_order_id:0001>" in published
    assert_public_safe(
        payload,
        secrets=("secondary-api-key", "secondary-secret-key"),
        account_ids=(ACCOUNT_NUMBER, str(ACCOUNT_ID)),
    )

    # Public cassettes are loaded from JSON, not passed as Python objects.
    replay = ReplayBrokerCassette(json.loads(json.dumps(payload)))
    client = replay.client("trading")
    broker = AlpacaBroker.__new__(AlpacaBroker)
    broker.client = client
    broker._shortable_cache = {}
    broker._fractionable_cache = {}
    broker._trading_day_cache = {}
    broker._session_open_cache = {}
    broker._sessions_held_cache = {}
    monkeypatch.setattr(
        "src.execution.broker._sector_reference._get_sector",
        lambda _symbol: "ETF",
    )
    account = broker.get_account()
    order = client.submit_order(order_data=_OrderRequest("SPY", "different-generated-client-order"))
    fetched = client.get_order_by_id(order.id)
    activities = client.get_activities()

    assert account["cash"] == 8000.0
    assert account["non_marginable_buying_power"] == 7500.0
    assert order.id == "<QAMC:order_id:0001>"
    assert order.client_order_id == "<QAMC:client_order_id:0001>"
    assert fetched.id == order.id
    assert activities[0].id == "<QAMC:activity_id:0001>"
    with pytest.raises(RecordedBrokerError) as error:
        client.get_missing_order(UUID("00000000-0000-4000-8000-000000000004"))
    assert error.value.status_code == 404
    assert str(MISSING_ORDER_ID) not in str(error.value)

    # The revived SimpleNamespace objects work through the actual AlpacaBroker
    # account and position seams; no SDK-only fake model path is introduced.
    positions = broker.get_positions()
    assert positions[0].symbol == "SPY"
    assert positions[0].unrealized_pnl == 20.0
    replay.assert_consumed()


def test_replay_rejects_out_of_order_extra_and_unused_calls():
    payload, _, _ = _record_full_cassette()

    out_of_order = ReplayBrokerCassette(payload)
    with pytest.raises(CassetteMismatch):
        out_of_order.client("trading").get_activities()

    unused = ReplayBrokerCassette(payload)
    with pytest.raises(UnusedRecordedBrokerCalls):
        unused.assert_consumed()

    # A one-entry cassette proves that any call after the recording is exhausted
    # fails rather than silently reaching a network client or returning None.
    one_call = BrokerCassette()
    RecordingBrokerClient(_TradingClient(), one_call, "trading").get_account()
    exhausted = ReplayBrokerCassette(one_call.to_payload())
    exhausted.client("trading").get_account()
    with pytest.raises(MissingRecordedBrokerCall):
        exhausted.client("trading").get_account()


def test_replay_remembers_a_swallowed_mismatch():
    payload, _, _ = _record_full_cassette()
    replay = ReplayBrokerCassette(payload)

    with pytest.raises(CassetteMismatch):
        replay.client("trading").get_activities()

    with pytest.raises(BrokerReplayViolation, match="violated its recording"):
        replay.assert_consumed()


def test_installer_replaces_both_clients_and_refuses_trade_updates():
    payload, _, _ = _record_full_cassette()
    broker = AlpacaBroker.__new__(AlpacaBroker)
    broker._fill_stream_enabled = False
    broker.api_key = "would-be-live-key"
    broker.secret_key = "would-be-live-secret"
    broker.client = object()
    broker._data_client = None

    replay = install_replay_broker_cassette(broker, payload)

    assert "both Alpaca SDK clients" in assert_broker_uses_replay(broker, replay)
    assert broker.client is not None
    assert broker._data_client is not None

    enabled = AlpacaBroker.__new__(AlpacaBroker)
    enabled._fill_stream_enabled = True
    with pytest.raises(BrokerCassetteError, match="trade_updates are not recorded"):
        install_replay_broker_cassette(enabled, payload)


def test_two_sdk_clients_share_one_global_order():
    request = StockBarsRequest(
        symbol_or_symbols=["SPY"],
        timeframe=TimeFrame.Day,
        start=datetime(2026, 10, 1, tzinfo=timezone.utc),
        end=datetime(2026, 10, 2, tzinfo=timezone.utc),
    )
    cassette = BrokerCassette()
    trading = RecordingBrokerClient(_TradingClient(), cassette, "trading")
    market_data = RecordingBrokerClient(
        SimpleNamespace(get_stock_bars=lambda request: {"SPY": [request]}),
        cassette,
        "stock_historical_data",
    )
    trading.get_account()
    market_data.get_stock_bars(request)

    payload = json.loads(json.dumps(cassette.to_payload()))
    out_of_order = ReplayBrokerCassette(payload)
    with pytest.raises(CassetteMismatch):
        out_of_order.client("stock_historical_data").get_stock_bars(request)

    replay = ReplayBrokerCassette(payload)
    replay.client("trading").get_account()
    replay.client("stock_historical_data").get_stock_bars(request)
    replay.assert_consumed()


def test_real_alpaca_request_model_has_stable_json_call_shape():
    request = LimitOrderRequest(
        symbol="SPY",
        qty=1,
        side=OrderSide.BUY,
        time_in_force=TimeInForce.DAY,
        limit_price=500,
        client_order_id="capture-generated-id",
    )
    cassette = BrokerCassette()
    recorder = RecordingBrokerClient(
        SimpleNamespace(
            submit_order=lambda *, order_data: SimpleNamespace(
                id=ORDER_ID,
                client_order_id=order_data.client_order_id,
            )
        ),
        cassette,
        "trading",
    )
    recorder.submit_order(order_data=request)

    replay = ReplayBrokerCassette(json.loads(json.dumps(cassette.to_payload())))
    replay.client("trading").submit_order(
        order_data=LimitOrderRequest(
            symbol="SPY",
            qty=1,
            side=OrderSide.BUY,
            time_in_force=TimeInForce.DAY,
            limit_price=500,
            client_order_id="replay-generated-id",
        )
    )
    replay.assert_consumed()


def test_response_serialization_failure_never_turns_success_into_a_retry_signal():
    class UnsupportedResponse:
        __slots__ = ()

    answer = UnsupportedResponse()
    cassette = BrokerCassette()
    recorder = RecordingBrokerClient(SimpleNamespace(submit_order=lambda: answer), cassette, "trading")

    assert recorder.submit_order() is answer
    with pytest.raises(RuntimeError, match="cannot export cassette"):
        cassette.to_payload()


def test_broken_exception_property_is_not_silently_erased():
    class BrokenError(RuntimeError):
        @property
        def message(self):
            raise RuntimeError("broken SDK error property")

    class FailingClient:
        def submit_order(self):
            raise BrokenError("broker rejected order")

    recorder = RecordingBrokerClient(FailingClient(), BrokerCassette(), "trading")
    with pytest.raises(RuntimeError, match="broken SDK error property"):
        recorder.submit_order()


def _round_tripped_error(message: str, status_code: int, code: int):
    class APIErrorStandIn(RuntimeError):
        def __init__(self):
            super().__init__(message)
            self.status_code = status_code
            self.code = code

    class FailingClient:
        def submit_order(self):
            raise APIErrorStandIn()

    cassette = BrokerCassette()
    recorder = RecordingBrokerClient(FailingClient(), cassette, "trading")
    with pytest.raises(APIErrorStandIn):
        recorder.submit_order()
    replay = ReplayBrokerCassette(json.loads(json.dumps(cassette.to_payload())))
    with pytest.raises(RecordedBrokerError) as error:
        replay.client("trading").submit_order()
    replay.assert_consumed()
    return error.value


def test_replayed_errors_preserve_every_current_rest_classifier_surface():
    duplicate = _round_tripped_error("client_order_id must be unique", 422, 42210000)
    assert str(duplicate) == "client_order_id must be unique"
    assert duplicate.message == str(duplicate)
    assert duplicate.status_code == 422
    assert duplicate.status == 422
    assert duplicate.code == 42210000
    assert duplicate.error_type.endswith("APIErrorStandIn")
    assert _is_duplicate_client_order_id_rejection(duplicate)
    assert _is_terminal_submission_rejection(duplicate)

    held = _round_tripped_error("insufficient qty held_for_orders", 403, 40310000)
    assert _is_held_for_orders_error(held)

    missing = _round_tripped_error("invalid symbol", 404, 40410000)
    assert _is_terminal_broker_rejection(missing)
    assert "invalid symbol" in str(missing).lower()
